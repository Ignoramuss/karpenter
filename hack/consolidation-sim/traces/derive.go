/*
Copyright The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math"
	"sort"
	"strconv"
)

// task_events columns (schema.csv, 1-based field numbers there).
const (
	colTime      = 0
	colJobID     = 2
	colTaskIndex = 3
	colEventType = 5
	colCPU       = 9
	colMemory    = 10
	eventColumns = 13
)

// Task event types from the trace's format document.
const (
	eventSchedule = 1
	eventEvict    = 2
	eventFail     = 3
	eventFinish   = 4
	eventKill     = 5
	eventLost     = 6
)

// Requests below these are clamped up: the trace has requests that scale to a few millicores, which no real pod
// spec would carry and which let a node hold thousands of pods.
const (
	minCPUMillicores = 100
	minMemoryMiB     = 128
)

const microsPerSecond = int64(1_000_000)

// Row is one line of the derived trace: a task run inside the window, in seconds from the window start.
type Row struct {
	Start, End     int64
	CPUMillicores  int64
	MemoryMiB      int64
	runningAtStart bool
	capped         bool
}

// Counts explain every run the window saw, so the derived file can be checked against the source.
type Counts struct {
	EventsRead             int64 `json:"events_read"`
	RunsInWindow           int64 `json:"runs_in_window"`
	DroppedMissingRequests int64 `json:"dropped_missing_requests"`
	DroppedZeroLength      int64 `json:"dropped_zero_length"`
	DroppedBySample        int64 `json:"dropped_by_sample"`
	RowsWritten            int64 `json:"rows_written"`
	RunningAtWindowStart   int64 `json:"rows_running_at_window_start"`
	CappedAtWindowEnd      int64 `json:"rows_capped_at_window_end"`
	CPUClamped             int64 `json:"rows_cpu_clamped"`
	MemoryClamped          int64 `json:"rows_memory_clamped"`
	// RescheduledWithoutEnd counts SCHEDULE events for a task that was already running; the earlier run is closed
	// at the new SCHEDULE, since a task index runs at most once at a time.
	RescheduledWithoutEnd int64 `json:"rescheduled_without_end"`
	// UnmatchedEnds counts terminal events for a task with no open run: a task killed, failed or evicted while
	// still pending.
	UnmatchedEnds int64 `json:"unmatched_end_events"`
}

type taskKey struct{ job, index int64 }

type openRun struct {
	start       int64
	cpu, memory float64
	missing     bool
}

// deriver turns a time-ordered task_events stream into runs clipped to [start, end), in trace microseconds.
type deriver struct {
	start, end int64
	// cpuMillicores and memoryMiB are the absolute size of a request of 1.0, the largest machine's capacity.
	cpuMillicores, memoryMiB float64
	sample                   sampler
	open                     map[taskKey]openRun
	rows                     []Row
	counts                   Counts
	last                     int64
	done                     bool
}

func newDeriver(start, end int64, cpuCores, memoryGiB float64, s sampler) *deriver {
	return &deriver{start: start, end: end, cpuMillicores: cpuCores * 1000, memoryMiB: memoryGiB * 1024, sample: s, open: map[taskKey]openRun{}}
}

// event consumes one task_events record. It sets done at the first event at or past the window end, after which
// the stream may stop: runs still open then are capped at the window end by finish.
func (d *deriver) event(rec []string) error {
	if len(rec) != eventColumns {
		return fmt.Errorf("want %d columns, got %d", eventColumns, len(rec))
	}
	t, err := strconv.ParseInt(rec[colTime], 10, 64)
	if err != nil {
		return fmt.Errorf("time %q, %w", rec[colTime], err)
	}
	// The window logic relies on time order (each shard is sorted and shards follow each other); fail rather than
	// mis-pair events if a shard breaks that.
	if t < d.last {
		return fmt.Errorf("time %d before the previous event's %d: input is not time-ordered", t, d.last)
	}
	d.last = t
	if t >= d.end {
		d.done = true
		return nil
	}
	d.counts.EventsRead++
	et, err := strconv.Atoi(rec[colEventType])
	if err != nil {
		return fmt.Errorf("event type %q, %w", rec[colEventType], err)
	}
	if et != eventSchedule && !terminal(et) {
		return nil
	}
	job, err := strconv.ParseInt(rec[colJobID], 10, 64)
	if err != nil {
		return fmt.Errorf("job ID %q, %w", rec[colJobID], err)
	}
	index, err := strconv.ParseInt(rec[colTaskIndex], 10, 64)
	if err != nil {
		return fmt.Errorf("task index %q, %w", rec[colTaskIndex], err)
	}
	key := taskKey{job, index}
	if terminal(et) {
		r, ok := d.open[key]
		if !ok {
			d.counts.UnmatchedEnds++
			return nil
		}
		delete(d.open, key)
		d.emit(key, r, t)
		return nil
	}
	if r, ok := d.open[key]; ok {
		d.counts.RescheduledWithoutEnd++
		d.emit(key, r, t)
	}
	r := openRun{start: t}
	if rec[colCPU] == "" || rec[colMemory] == "" {
		r.missing = true
	} else {
		if r.cpu, err = strconv.ParseFloat(rec[colCPU], 64); err != nil {
			return fmt.Errorf("CPU request %q, %w", rec[colCPU], err)
		}
		if r.memory, err = strconv.ParseFloat(rec[colMemory], 64); err != nil {
			return fmt.Errorf("memory request %q, %w", rec[colMemory], err)
		}
	}
	d.open[key] = r
	return nil
}

func terminal(et int) bool {
	switch et {
	case eventEvict, eventFail, eventFinish, eventKill, eventLost:
		return true
	}
	return false
}

// emit clips a finished run to the window and keeps it if it has requests and its task is in the sample.
func (d *deriver) emit(key taskKey, r openRun, end int64) {
	if end <= d.start || r.start >= d.end {
		return
	}
	d.counts.RunsInWindow++
	if r.missing {
		d.counts.DroppedMissingRequests++
		return
	}
	if !d.sample.keep(key) {
		d.counts.DroppedBySample++
		return
	}
	s, e := max(r.start, d.start)-d.start, min(end, d.end)-d.start
	if e <= s {
		d.counts.DroppedZeroLength++
		return
	}
	// Floor the start and ceil the end so a sub-second run still lasts a second.
	row := Row{Start: s / microsPerSecond, End: (e + microsPerSecond - 1) / microsPerSecond, runningAtStart: r.start < d.start, capped: end >= d.end}
	row.CPUMillicores = int64(math.Round(r.cpu * d.cpuMillicores))
	if row.CPUMillicores < minCPUMillicores {
		row.CPUMillicores = minCPUMillicores
		d.counts.CPUClamped++
	}
	row.MemoryMiB = int64(math.Round(r.memory * d.memoryMiB))
	if row.MemoryMiB < minMemoryMiB {
		row.MemoryMiB = minMemoryMiB
		d.counts.MemoryClamped++
	}
	if row.runningAtStart {
		d.counts.RunningAtWindowStart++
	}
	if row.capped {
		d.counts.CappedAtWindowEnd++
	}
	d.counts.RowsWritten++
	d.rows = append(d.rows, row)
}

// finish caps the runs still open at the window end and returns every row, sorted so the file is the same however
// the open runs were stored.
func (d *deriver) finish() []Row {
	for key, r := range d.open {
		d.emit(key, r, d.end)
	}
	d.open = map[taskKey]openRun{}
	sort.Slice(d.rows, func(i, j int) bool {
		a, b := d.rows[i], d.rows[j]
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		if a.End != b.End {
			return a.End < b.End
		}
		if a.CPUMillicores != b.CPUMillicores {
			return a.CPUMillicores < b.CPUMillicores
		}
		return a.MemoryMiB < b.MemoryMiB
	})
	return d.rows
}

// sampler keeps a fixed fraction of tasks, chosen by a seeded hash of the task's identity rather than by a random
// stream, so the choice does not depend on event order and every run of a sampled task (after an eviction, say)
// stays in the file together.
type sampler struct {
	seed     int64
	fraction float64
}

func (s sampler) keep(k taskKey) bool {
	if s.fraction >= 1 {
		return true
	}
	h := sha256.Sum256([]byte(fmt.Sprintf("%d/%d/%d", s.seed, k.job, k.index)))
	return binary.BigEndian.Uint64(h[:8]) < uint64(math.Ldexp(s.fraction, 64))
}
