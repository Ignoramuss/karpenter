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
	"reflect"
	"strconv"
	"testing"
	"time"
)

const sec = microsPerSecond

// record is a task_events row with the columns the deriver reads; the rest stay empty as the trace allows.
func record(t int64, job, index, event int, cpu, mem string) []string {
	r := make([]string, eventColumns)
	r[colTime], r[colJobID], r[colTaskIndex], r[colEventType] = strconv.FormatInt(t, 10), strconv.Itoa(job), strconv.Itoa(index), strconv.Itoa(event)
	r[colCPU], r[colMemory] = cpu, mem
	return r
}

// TestDeriveRuns pins the mapping on a window of [100 s, 200 s): which runs become rows, how they are clipped and
// rounded, and how requests scale and clamp.
func TestDeriveRuns(t *testing.T) {
	d := newDeriver(100*sec, 200*sec, 32, 128, sampler{seed: 1, fraction: 1})
	events := [][]string{
		record(10*sec, 2, 0, eventSchedule, "0.01", "0.01"),   // ends before the window
		record(50*sec, 1, 0, eventSchedule, "0.0625", "0.03"), // running at the window start
		record(90*sec, 2, 0, eventKill, "", ""),
		record(95*sec, 9, 0, eventKill, "", ""),      // never scheduled: unmatched
		record(110*sec, 2, 1, eventSchedule, "", ""), // no requests
		record(115*sec, 2, 1, eventFail, "", ""),
		record(120*sec+500_000, 1, 1, eventSchedule, "0.5", "0.25"), // sub-second edges round outward
		record(130*sec+200_000, 1, 1, eventEvict, "", ""),
		record(140*sec, 1, 1, eventSchedule, "0.5", "0.25"), // rescheduled after the eviction, runs past the end
		record(150*sec, 1, 0, eventFinish, "", ""),
		record(160*sec, 3, 0, eventSchedule, "0.001", "0.0005"), // clamped to the minimums
		record(170*sec, 3, 0, eventLost, "", ""),
		record(180*sec, 3, 1, eventSchedule, "0.01", "0.01"), // zero length
		record(180*sec, 3, 1, eventFinish, "", ""),
		record(190*sec, 4, 0, eventSchedule, "0.01", "0.01"), // a second SCHEDULE closes the first run
		record(195*sec, 4, 0, eventSchedule, "0.01", "0.01"),
		record(200*sec, 4, 0, eventFinish, "", ""), // at the window end: not read
	}
	for _, e := range events {
		if d.done {
			break
		}
		if err := d.event(e); err != nil {
			t.Fatal(err)
		}
	}
	if !d.done {
		t.Fatal("an event at the window end should finish the window")
	}
	var got [][4]int64
	for _, r := range d.finish() {
		got = append(got, [4]int64{r.Start, r.End, r.CPUMillicores, r.MemoryMiB})
	}
	want := [][4]int64{
		{0, 50, 2000, 3932},
		{20, 31, 16000, 32768},
		{40, 100, 16000, 32768},
		{60, 70, minCPUMillicores, minMemoryMiB},
		{90, 95, 320, 1311},
		{95, 100, 320, 1311},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("rows\n%v\nwant\n%v", got, want)
	}
	c := d.counts
	if c.RunsInWindow != 8 || c.DroppedMissingRequests != 1 || c.DroppedZeroLength != 1 || c.RowsWritten != 6 ||
		c.RunningAtWindowStart != 1 || c.CappedAtWindowEnd != 2 || c.CPUClamped != 1 || c.MemoryClamped != 1 ||
		c.RescheduledWithoutEnd != 1 || c.UnmatchedEnds != 1 {
		t.Fatalf("counts %+v", c)
	}
}

func TestDeriveRejectsUnorderedInput(t *testing.T) {
	d := newDeriver(0, 100*sec, 32, 128, sampler{fraction: 1})
	if err := d.event(record(30*sec, 1, 0, eventSchedule, "0.1", "0.1")); err != nil {
		t.Fatal(err)
	}
	if err := d.event(record(20*sec, 1, 0, eventFinish, "", "")); err == nil {
		t.Fatal("want an error for an event earlier than the one before it")
	}
}

func TestSampler(t *testing.T) {
	keys := make([]taskKey, 20000)
	for i := range keys {
		keys[i] = taskKey{job: int64(6_000_000_000 + i/50), index: int64(i % 50)}
	}
	kept := func(s sampler) map[taskKey]bool {
		out := map[taskKey]bool{}
		for _, k := range keys {
			if s.keep(k) {
				out[k] = true
			}
		}
		return out
	}
	a, b, c := kept(sampler{seed: 1, fraction: 0.2}), kept(sampler{seed: 1, fraction: 0.2}), kept(sampler{seed: 2, fraction: 0.2})
	if !reflect.DeepEqual(a, b) {
		t.Error("the same seed kept different tasks")
	}
	if reflect.DeepEqual(a, c) {
		t.Error("different seeds kept the same tasks")
	}
	if share := float64(len(a)) / float64(len(keys)); share < 0.19 || share > 0.21 {
		t.Errorf("kept %.3f of tasks, want about 0.2", share)
	}
	if len(kept(sampler{fraction: 1})) != len(keys) {
		t.Error("fraction 1 should keep every task")
	}
}

func TestWindowLabel(t *testing.T) {
	if got := windowLabel(6*time.Hour, 3*time.Hour); got != "6h-9h" {
		t.Errorf("got %q", got)
	}
	if got := windowLabel(90*time.Minute, 30*time.Minute); got != "90m-120m" {
		t.Errorf("got %q", got)
	}
}
