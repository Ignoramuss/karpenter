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
	"errors"
	"fmt"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/samber/lo"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
)

// traceCatalog's band mean (meanCPU) is 6000m, so a node's share of a trace at traceFill is 4200m.
type traceCatalog []typeInfo

func (c traceCatalog) Types() []typeInfo { return c }
func (c traceCatalog) Zones() []string   { return []string{"test-zone-a"} }

var testTraceCatalog = traceCatalog{{Name: "t.xlarge", CPU: 4000, Memory: 16384}, {Name: "t.2xlarge", CPU: 8000, Memory: 32768}}

const testNodeShare = 6000 * traceFill

func writeTraceFile(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "trace.csv")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestTraceParse(t *testing.T) {
	path := writeTraceFile(t, traceHeader+"\n0,600,1000,2048\n30,90,250,128\n120,10800,7000,30000\n")
	tasks, err := readTrace(path)
	if err != nil {
		t.Fatal(err)
	}
	want := []Task{
		{Start: D(0), Duration: D(600 * time.Second), Shape: Shape{CPU: 1000, Memory: 2048}},
		{Start: D(30 * time.Second), Duration: D(60 * time.Second), Shape: Shape{CPU: 250, Memory: 128}},
		{Start: D(120 * time.Second), Duration: D(10680 * time.Second), Shape: Shape{CPU: 7000, Memory: 30000}},
	}
	if !reflect.DeepEqual(tasks, want) {
		t.Fatalf("parsed %+v, want %+v", tasks, want)
	}
	w, err := loadTrace(path, 1, rand.New(rand.NewSource(1)), testTraceCatalog) //nolint:gosec
	if err != nil {
		t.Fatal(err)
	}
	if w.Name != W6Trace || len(w.Tasks) == 0 || len(w.Deployments)+len(w.Jobs)+len(w.Initial) != 0 {
		t.Fatalf("workload %+v, want W6 with tasks only", w)
	}
}

func TestTraceMalformed(t *testing.T) {
	cases := map[string]struct{ body, want string }{
		"empty":          {"", "empty file"},
		"header only":    {traceHeader + "\n", "no tasks"},
		"wrong header":   {"start,end,cpu,memory\n0,1,1,1\n", "header"},
		"extra column":   {traceHeader + "\n0,1,1,1,1\n", "wrong number of fields"},
		"fractional":     {traceHeader + "\n0,1.5,1,1\n", "end_seconds \"1.5\" is not a non-negative integer"},
		"negative start": {traceHeader + "\n-1,5,1,1\n", "start_seconds \"-1\""},
		"zero length":    {traceHeader + "\n0,10,1,1\n5,5,1,1\n", "line 3: end_seconds 5 is not after start_seconds 5"},
		"zero request":   {traceHeader + "\n0,5,0,1\n", "requests must be positive"},
		"fits nothing":   {traceHeader + "\n0,5,64000,1024\n", "no task fits"},
	}
	for _, name := range sortedStrings(lo.Keys(cases)) {
		c := cases[name]
		t.Run(name, func(t *testing.T) {
			path := writeTraceFile(t, c.body)
			_, err := loadTrace(path, 10, rand.New(rand.NewSource(1)), testTraceCatalog) //nolint:gosec
			if err == nil || !strings.Contains(err.Error(), c.want) || !strings.Contains(err.Error(), path) {
				t.Fatalf("got error %v, want one naming %s and containing %q", err, path, c.want)
			}
		})
	}
	t.Run("missing file", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "absent.csv")
		if _, err := loadTrace(path, 10, rand.New(rand.NewSource(1)), testTraceCatalog); err == nil || !errors.Is(err, os.ErrNotExist) { //nolint:gosec
			t.Fatalf("got error %v, want not-exist", err)
		}
	})
}

func TestTraceDropsTasksNoTypeHolds(t *testing.T) {
	// No type has 9000m, and none has 40000 MiB.
	path := writeTraceFile(t, traceHeader+"\n0,600,1000,2048\n0,600,9000,1024\n0,600,7000,40000\n0,600,3000,30000\n")
	tasks, err := readTrace(path)
	if err != nil {
		t.Fatal(err)
	}
	got := fitting(tasks, testTraceCatalog)
	if len(got) != 2 || got[0].Shape.CPU != 1000 || got[1].Shape.CPU != 3000 {
		t.Fatalf("kept %+v, want the 1000m and 3000m tasks", got)
	}
}

// overlappingTrace is n tasks that all start in the first ten minutes and run at least an hour, so they all run at
// once and the peak is exactly the sum of their requests: scaling the peak is then a matter of counting.
func overlappingTrace(n int) (string, int64) {
	var b strings.Builder
	b.WriteString(traceHeader + "\n")
	var sum int64
	for i := range n {
		cpu := []int64{250, 500, 1000, 2000}[i%4]
		start := i * 7 % 600
		fmt.Fprintf(&b, "%d,%d,%d,%d\n", start, start+3600+i*13%3600, cpu, 1024+i%3*512)
		sum += cpu
	}
	return b.String(), sum
}

func TestTraceScalesTowardNodes(t *testing.T) {
	body, sum := overlappingTrace(2000)
	path := writeTraceFile(t, body)
	in, err := readTrace(path)
	if err != nil {
		t.Fatal(err)
	}
	if peak := peakCPU(in); peak != sum {
		t.Fatalf("peak %d, want the sum of all requests %d", peak, sum)
	}
	type sig struct {
		d time.Duration
		s Shape
	}
	known := map[sig]bool{}
	for _, task := range in {
		known[sig{task.Duration.Duration, task.Shape}] = true
	}
	for _, n := range []int{40, 446, 1500} {
		t.Run(fmt.Sprintf("n=%d", n), func(t *testing.T) {
			w, err := loadTrace(path, n, rand.New(rand.NewSource(7)), testTraceCatalog) //nolint:gosec
			if err != nil {
				t.Fatal(err)
			}
			// The subset is sized to the closest prefix, so the peak misses the target by at most one task.
			target := float64(n) * testNodeShare
			got := float64(peakCPU(w.Tasks))
			if math.Abs(got-target) > 2000 {
				t.Errorf("peak %.0fm, want within one task (2000m) of %.0fm", got, target)
			}
			switch {
			case target < float64(sum)*0.9 && len(w.Tasks) >= len(in):
				t.Errorf("kept %d of %d tasks, want a subsample", len(w.Tasks), len(in))
			case target > float64(sum)*1.1 && len(w.Tasks) <= len(in):
				t.Errorf("got %d tasks from %d, want copies", len(w.Tasks), len(in))
			}
			if !sort.SliceIsSorted(w.Tasks, func(i, j int) bool { return w.Tasks[i].Start.Duration < w.Tasks[j].Start.Duration }) {
				t.Error("tasks are not ordered by start")
			}
			for _, task := range w.Tasks {
				if !known[sig{task.Duration.Duration, task.Shape}] {
					t.Fatalf("task %+v changed shape or duration", task)
				}
				if task.Start.Duration < 0 || task.Start.Duration > 600*time.Second+traceJitter {
					t.Fatalf("task %+v starts outside the trace's starts plus jitter", task)
				}
			}
		})
	}
}

func TestTraceDeterminism(t *testing.T) {
	body, _ := overlappingTrace(500)
	path := writeTraceFile(t, body)
	for _, n := range []int{20, 300} {
		load := func(seed int64) []Task {
			w, err := loadTrace(path, n, rand.New(rand.NewSource(seed)), testTraceCatalog) //nolint:gosec
			if err != nil {
				t.Fatal(err)
			}
			return w.Tasks
		}
		a, b, c := load(3), load(3), load(4)
		if !reflect.DeepEqual(a, b) {
			t.Errorf("n=%d: the same seed gave different tasks", n)
		}
		if reflect.DeepEqual(a, c) {
			t.Errorf("n=%d: different seeds gave the same tasks", n)
		}
	}
}

// TestTraceBundledFile loads the derived Google 2011 window against the real catalog, as a W6 cell does.
func TestTraceBundledFile(t *testing.T) {
	cat, err := catalog.Load(catalog.Options{MaxCPU: 32})
	if err != nil {
		t.Fatal(err)
	}
	view := catalogAdapter{cat}
	path := filepath.Join("..", "traces", "google-2011-6h-9h.csv")
	all, err := readTrace(path)
	if err != nil {
		t.Fatal(err)
	}
	largest := lo.MaxBy(all, func(a, b Task) bool { return a.Shape.CPU > b.Shape.CPU }).Shape.CPU
	for _, n := range []int{20, 200, 10000} {
		w, err := Generate(ScenarioConfig{Name: W6Trace, Nodes: n, Param: map[string]any{"trace": path}}, rand.New(rand.NewSource(1)), view) //nolint:gosec
		if err != nil {
			t.Fatal(err)
		}
		target := float64(n) * meanCPU(view) * traceFill
		got := float64(peakCPU(w.Tasks))
		if math.Abs(got-target) > float64(largest) {
			t.Errorf("n=%d: peak %.0fm, want within the largest task (%dm) of %.0fm", n, got, largest, target)
		}
		t.Logf("n=%d: %d of %d tasks, peak %.0fm for a target of %.0fm", n, len(w.Tasks), len(all), got, target)
	}
}
