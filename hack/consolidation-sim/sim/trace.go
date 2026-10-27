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

package sim

import (
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// traceHeader is the derived trace format hack/consolidation-sim/traces writes.
const traceHeader = "start_seconds,end_seconds,cpu_millicores,memory_mib"

// Trace scaling. traceFill is the fill W2, W3 and W5 size n nodes against. traceJitter bounds how far a replicated
// task's start moves, so the copies of one task do not all arrive in the same instant.
const (
	traceFill   = 0.7
	traceJitter = time.Minute
)

// loadTrace reads a prepared trace (see traceReplay) and scales it so its peak concurrent CPU request fills about
// n nodes of the catalog's mean allocatable CPU to traceFill: a random subset of the tasks when the trace is larger
// than that, jittered copies when it is smaller (see scaleTasks). Tasks no instance type can hold are dropped
// first: they would pend for the whole run, and count against every arm, though no arm could place them.
func loadTrace(path string, n int, rng *rand.Rand, cat catalogView) (Workload, error) {
	tasks, err := readTrace(path)
	if err != nil {
		return Workload{}, fmt.Errorf("trace %s: %w", path, err)
	}
	types := cat.Types()
	tasks = fitting(tasks, types)
	if len(tasks) == 0 {
		return Workload{}, fmt.Errorf("trace %s: no task fits any instance type in the catalog", path)
	}
	// meanCPU averages the 3.5-16 vCPU band W2, W3 and W5 size against, so n counts the same nodes in every
	// scenario; a catalog with nothing in that band falls back to the mean of all its types.
	nodeCPU := meanCPU(cat)
	if !(nodeCPU > 0) {
		nodeCPU = 0
		for _, t := range types {
			nodeCPU += float64(t.CPU) / float64(len(types))
		}
	}
	return Workload{Name: W6Trace, Tasks: scaleTasks(tasks, float64(n)*nodeCPU*traceFill, rng)}, nil
}

// readTrace parses the derived CSV strictly: the exact header, then non-negative integer rows with end > start
// and positive requests. A bad row fails the whole file rather than being skipped, so a truncated or hand-edited
// trace cannot quietly change a workload.
func readTrace(path string) ([]Task, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	r.FieldsPerRecord = 4
	r.ReuseRecord = true
	head, err := r.Read()
	if errors.Is(err, io.EOF) {
		return nil, errors.New("empty file, want the header " + traceHeader)
	}
	if err != nil {
		return nil, err
	}
	if got := strings.Join(head, ","); got != traceHeader {
		return nil, fmt.Errorf("header %q, want %q", got, traceHeader)
	}
	var tasks []Task
	for {
		rec, err := r.Read()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		t, err := parseTask(rec)
		if err != nil {
			line, _ := r.FieldPos(0)
			return nil, fmt.Errorf("line %d: %w", line, err)
		}
		tasks = append(tasks, t)
	}
	if len(tasks) == 0 {
		return nil, errors.New("no tasks after the header")
	}
	return tasks, nil
}

func parseTask(rec []string) (Task, error) {
	var v [4]int64
	for i, s := range rec {
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil || n < 0 {
			return Task{}, fmt.Errorf("%s %q is not a non-negative integer", strings.Split(traceHeader, ",")[i], s)
		}
		v[i] = n
	}
	start, end, cpu, mem := v[0], v[1], v[2], v[3]
	if end <= start {
		return Task{}, fmt.Errorf("end_seconds %d is not after start_seconds %d", end, start)
	}
	if cpu == 0 || mem == 0 {
		return Task{}, fmt.Errorf("requests must be positive, got cpu_millicores %d memory_mib %d", cpu, mem)
	}
	return Task{Start: D(time.Duration(start) * time.Second), Duration: D(time.Duration(end-start) * time.Second), Shape: Shape{CPU: cpu, Memory: mem}}, nil
}

func fitting(tasks []Task, types []typeInfo) []Task {
	var out []Task
	for _, t := range tasks {
		for _, it := range types {
			if t.Shape.CPU <= it.CPU && t.Shape.Memory <= it.Memory {
				out = append(out, t)
				break
			}
		}
	}
	return out
}

// scaleTasks resamples tasks so their peak concurrent CPU is as close to target millicores as one task allows.
// It takes as many whole copies as fit under the target, the first unchanged and the rest jittered, then a prefix
// of a random permutation of one more pass over the tasks. Adding tasks never lowers the peak, so the prefix length
// is a binary search. A prefix of a random permutation is a uniform random subset, and sizing it by the peak itself
// keeps a small cluster on target: keeping each task with probability target/peak instead overshot the bundled
// trace's peak by 30% at 20 nodes, where only about a hundred tasks remain. At least one task is kept.
func scaleTasks(tasks []Task, target float64, rng *rand.Rand) []Task {
	copies := int(target / float64(peakCPU(tasks)))
	out := make([]Task, 0, (copies+1)*len(tasks))
	for c := range copies {
		for _, t := range tasks {
			if c > 0 {
				t = jitter(t, rng)
			}
			out = append(out, t)
		}
	}
	pool := make([]Task, len(tasks))
	for i, t := range tasks {
		if copies > 0 {
			t = jitter(t, rng)
		}
		pool[i] = t
	}
	rng.Shuffle(len(pool), func(i, j int) { pool[i], pool[j] = pool[j], pool[i] })
	peak := func(k int) float64 { return float64(peakCPU(append(out[:len(out):len(out)], pool[:k]...))) }
	k := min(len(pool), sort.Search(len(pool)+1, func(k int) bool { return peak(k) >= target }))
	if k > 0 && target-peak(k-1) < peak(k)-target {
		k--
	}
	if len(out)+k == 0 {
		k = 1
	}
	out = append(out, pool[:k]...)
	// Stable, so tasks arriving together keep the order above and the seed alone decides the result.
	sort.SliceStable(out, func(i, j int) bool { return out[i].Start.Duration < out[j].Start.Duration })
	return out
}

// jitter moves a copy's start uniformly within traceJitter, never before the trace start. Its duration is kept, so
// a copy of a task capped at the window end may run up to traceJitter past it.
func jitter(t Task, rng *rand.Rand) Task {
	d := time.Duration((2*rng.Float64() - 1) * float64(traceJitter))
	t.Start = D(max(0, t.Start.Duration+d))
	return t
}

// peakCPU is the largest total CPU request of tasks running at once. A task ending at an instant frees its CPU
// before one starting then takes it, matching the half-open [start, end) a task occupies.
func peakCPU(tasks []Task) int64 {
	type edge struct {
		at    time.Duration
		delta int64
	}
	edges := make([]edge, 0, 2*len(tasks))
	for _, t := range tasks {
		edges = append(edges, edge{t.Start.Duration, t.Shape.CPU}, edge{t.Start.Duration + t.Duration.Duration, -t.Shape.CPU})
	}
	sort.Slice(edges, func(i, j int) bool {
		if edges[i].at != edges[j].at {
			return edges[i].at < edges[j].at
		}
		return edges[i].delta < edges[j].delta
	})
	var cur, peak int64
	for _, e := range edges {
		cur += e.delta
		peak = max(peak, cur)
	}
	return peak
}
