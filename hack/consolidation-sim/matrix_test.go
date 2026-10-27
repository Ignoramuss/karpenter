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
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"sigs.k8s.io/karpenter/hack/consolidation-sim/sim"
)

func writeMatrix(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "matrix.yaml")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func expandMatrix(t *testing.T, body string, only ...string) ([]Cell, error) {
	t.Helper()
	m, err := loadMatrix(writeMatrix(t, body))
	if err != nil {
		return nil, err
	}
	return m.Expand(only)
}

func mustExpand(t *testing.T, body string, only ...string) []Cell {
	t.Helper()
	cells, err := expandMatrix(t, body, only...)
	if err != nil {
		t.Fatal(err)
	}
	return cells
}

func decodeCell(t *testing.T, c Cell) sim.Config {
	t.Helper()
	raw, err := json.Marshal(c.Config)
	if err != nil {
		t.Fatal(err)
	}
	var cfg sim.Config
	if err := decodeStrict(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	return cfg
}

func runIDs(cells []Cell) []string {
	ids := make([]string, len(cells))
	for i, c := range cells {
		ids[i] = c.RunID
	}
	return ids
}

func TestMatrixExpansionOrder(t *testing.T) {
	cells := mustExpand(t, `
name: order
base:
  scenario: {name: W1, nodes: 6}
  duration: 10m
  arm: B0-none
groups:
- name: first
  vary:
    seed: [2, 1]
    scenario.nodes: [6, 12]
    arm: [B0-none, B2-when-empty-or-underutilized]
- name: second
  set: {placement: most-allocated}
`)
	if len(cells) != 2*2*2+1 {
		t.Fatalf("got %d cells, want 9", len(cells))
	}
	// Paths sort as arm < scenario.nodes < seed: arm varies slowest, seed fastest, and values keep file order.
	type key struct {
		arm   string
		nodes int
		seed  int64
	}
	var want []key
	for _, arm := range []string{sim.ArmNone, sim.ArmUnderutil} {
		for _, nodes := range []int{6, 12} {
			for _, seed := range []int64{2, 1} {
				want = append(want, key{arm, nodes, seed})
			}
		}
	}
	for i, w := range want {
		cfg := decodeCell(t, cells[i])
		if got := (key{cfg.Arm.Name, cfg.Scenario.Nodes, cfg.Seed}); got != w || cells[i].Group != "first" {
			t.Errorf("cell %d is %+v in group %s, want %+v in group first", i, got, cells[i].Group, w)
		}
	}
	last := cells[len(cells)-1]
	if cfg := decodeCell(t, last); last.Group != "second" || cfg.Placement != sim.MostAllocated || cfg.Scenario.Nodes != 6 {
		t.Errorf("last cell is %+v in group %s, want group second's single cell over base", cfg, last.Group)
	}
	if !strings.HasPrefix(last.RunID, "second__base__") {
		t.Errorf("a group without vary should be described as base, got %s", last.RunID)
	}
	if got := runIDs(mustExpand(t, `
name: order
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: B0-none}
groups:
- {name: first, vary: {arm: [B0-none, B2-when-empty-or-underutilized], scenario.nodes: [6, 12], seed: [2, 1]}}
- {name: second, set: {placement: most-allocated}}
`)); !slices.Equal(got, runIDs(cells)) {
		t.Errorf("the order of vary keys in the file changed the expansion:\n%v\n%v", got, runIDs(cells))
	}
	if only := mustExpand(t, `
name: order
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: B0-none}
groups:
- {name: first, vary: {seed: [1, 2]}}
- {name: second, vary: {seed: [3]}}
`, "second"); len(only) != 1 || only[0].Group != "second" {
		t.Errorf("--only second expanded %v", runIDs(only))
	}
}

func TestMatrixDottedPaths(t *testing.T) {
	t.Run("setPath creates objects and null deletes", func(t *testing.T) {
		cfg := map[string]any{"scenario": map[string]any{"name": "W1"}}
		if err := setPath(cfg, []string{"scenario", "params", "fraction"}, json.Number("0.5")); err != nil {
			t.Fatal(err)
		}
		if err := setPath(cfg, []string{"latency", "launch", "max"}, "9s"); err != nil {
			t.Fatal(err)
		}
		want := map[string]any{
			"scenario": map[string]any{"name": "W1", "params": map[string]any{"fraction": json.Number("0.5")}},
			"latency":  map[string]any{"launch": map[string]any{"max": "9s"}},
		}
		if a, b := canonical(t, cfg), canonical(t, want); a != b {
			t.Errorf("got %s, want %s", a, b)
		}
		if err := setPath(cfg, []string{"scenario", "name"}, nil); err != nil {
			t.Fatal(err)
		}
		if _, ok := cfg["scenario"].(map[string]any)["name"]; ok {
			t.Errorf("a null value should delete the key")
		}
	})
	t.Run("setPath does not descend into a scalar", func(t *testing.T) {
		cfg := map[string]any{"scenario": "W1"}
		if err := setPath(cfg, []string{"scenario", "nodes"}, json.Number("6")); err == nil || !strings.Contains(err.Error(), "not an object") {
			t.Errorf("got %v, want a not-an-object error", err)
		}
	})
	t.Run("expansion", func(t *testing.T) {
		cells := mustExpand(t, `
name: paths
base:
  scenario: W1
  arm: {name: B3-balanced, policy: Balanced}
  duration: 10m
  latency: {launch: {min: 1s, max: 2s}}
groups:
- name: g
  set: {scenario: {nodes: 6, params: {fraction: 0.5}}, latency: {launch: {max: 3s}}}
  vary:
    arm: [B0-none]
    scenario.params.extra: [3]
    latency.podStartup: [{min: 1s, max: 4s}]
`)
		cfg := decodeCell(t, cells[0])
		if cfg.Arm.Name != sim.ArmNone || cfg.Arm.Policy != "Balanced" {
			t.Errorf("arm is %+v; a string at arm should set arm.name and keep arm.policy from base", cfg.Arm)
		}
		if cfg.Scenario.Name != sim.W1Fragmented || cfg.Scenario.Nodes != 6 {
			t.Errorf("scenario is %+v; set should merge over base's shorthand", cfg.Scenario)
		}
		if got := canonical(t, cfg.Scenario.Param); got != `{"extra":3,"fraction":0.5}` {
			t.Errorf("scenario.params is %s", got)
		}
		if l := cfg.Latency; l.Launch.Min.Duration != time.Second || l.Launch.Max.Duration != 3*time.Second || l.PodStartup.Max.Duration != 4*time.Second {
			t.Errorf("latency is %+v", l)
		}
	})
	t.Run("pipeline stage shorthand", func(t *testing.T) {
		cells := mustExpand(t, `
name: stages
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: {name: pipeline, pipeline: {seed: 7}}}
groups:
- {name: g, vary: {arm.pipeline.planner: [upstream]}}
`)
		if cfg := decodeCell(t, cells[0]); cfg.Arm.Pipeline == nil || cfg.Arm.Pipeline.Planner.Name != "upstream" || cfg.Arm.Pipeline.Seed != 7 {
			t.Errorf("arm.pipeline is %+v", cfg.Arm.Pipeline)
		}
		if !strings.Contains(cells[0].RunID, "__planner-upstream__") {
			t.Errorf("run ID %s should describe the stage as planner-upstream", cells[0].RunID)
		}
		moves := mustExpand(t, `
name: moves
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: {name: pipeline, pipeline: {moves: [upstream-multi, upstream-single]}}}
groups:
- {name: g}
`)
		if cfg := decodeCell(t, moves[0]); len(cfg.Arm.Pipeline.Moves) != 2 || cfg.Arm.Pipeline.Moves[1].Name != "upstream-single" {
			t.Errorf("arm.pipeline.moves is %+v", cfg.Arm.Pipeline.Moves)
		}
	})
	for name, tc := range map[string]struct{ body, want string }{
		"unknown path":       {`{name: g, vary: {scenario.nodez: [6]}}`, "no field scenario.nodez"},
		"path into a scalar": {`{name: g, vary: {duration.hours: [1]}}`, "no field duration.hours"},
		"runID in vary":      {`{name: g, vary: {runID: [x]}}`, "runner derives it"},
		"runID in set":       {`{name: g, set: {runID: x}}`, "runner derives it"},
		"set not an object":  {`{name: g, set: [1]}`, "must be an object"},
		"empty segment":      {`{name: g, vary: {scenario..nodes: [6]}}`, "empty segment"},
		"unknown field":      {`{name: g, set: {durration: 1h}}`, "durration"},
		"unknown arm":        {`{name: g, vary: {arm: [B9-nothing]}}`, "unknown arm"},
		"unknown strategy":   {`{name: g, set: {arm: {name: pipeline, pipeline: {planner: no-such-planner}}}}`, "no-such-planner"},
		"value not object":   {`{name: g, set: {scenario: {params: 5}}, vary: {scenario.params.x: [1]}}`, "scenario.params is 5, not an object"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := expandMatrix(t, "name: bad\nbase: {scenario: {name: W1, nodes: 6}, arm: B0-none}\ngroups:\n- "+tc.body+"\n")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("got error %v, want one mentioning %q", err, tc.want)
			}
		})
	}
}

func canonical(t *testing.T, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestMatrixSeedRanges(t *testing.T) {
	cells := mustExpand(t, `
name: seeds
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: B0-none}
groups:
- {name: g, vary: {seed: {from: 101, to: 110}}}
`)
	if len(cells) != 10 {
		t.Fatalf("got %d cells, want 10", len(cells))
	}
	for i, c := range cells {
		if seed := decodeCell(t, c).Seed; seed != int64(101+i) {
			t.Errorf("cell %d has seed %d, want %d", i, seed, 101+i)
		}
		if want := fmt.Sprintf("g__seed-%d__", 101+i); !strings.HasPrefix(c.RunID, want) {
			t.Errorf("run ID %s, want prefix %s", c.RunID, want)
		}
	}
	// Above 2^53 a float64 would round; json.Number keeps the literal.
	big := mustExpand(t, "name: big\nbase: {scenario: {name: W1, nodes: 6}, arm: B0-none}\ngroups:\n- {name: g, vary: {seed: [9007199254740993]}}\n")
	if seed := decodeCell(t, big[0]).Seed; seed != 9007199254740993 {
		t.Errorf("seed decoded as %d", seed)
	}
	for name, vary := range map[string]string{
		"reversed":    "{from: 5, to: 1}",
		"missing to":  "{from: 1}",
		"unknown key": "{from: 1, to: 3, step: 1}",
		"fractional":  "{from: 1.5, to: 3}",
		"overflow":    "{from: -9223372036854775808, to: 9223372036854775807}",
		"too many":    "{from: 0, to: 100000}",
		"empty list":  "[]",
		"scalar":      "7",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := expandMatrix(t, "name: bad\nbase: {scenario: {name: W1, nodes: 6}, arm: B0-none}\ngroups:\n- {name: g, vary: {seed: "+vary+"}}\n"); err == nil {
				t.Errorf("seed %s expanded without error", vary)
			}
		})
	}
}

func TestMatrixRunIDs(t *testing.T) {
	const a = `
name: a
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: {name: B0-none}}
groups:
- {name: g, vary: {seed: [1]}}
`
	// The same config spelled differently: shorthand, keys reordered, fields moved from base to set.
	const b = `
name: b
outDir: elsewhere
base: {duration: 10m}
groups:
- name: g
  set: {arm: B0-none, scenario: {nodes: 6, name: W1}}
  vary: {seed: [1]}
`
	idA, idB := mustExpand(t, a)[0].RunID, mustExpand(t, b)[0].RunID
	if idA != idB {
		t.Errorf("identical configs got different IDs %s and %s", idA, idB)
	}
	// Pinned: a change to the canonical form renames every finished cell, so every matrix would rerun from scratch.
	// 525c8109 starts the SHA-256 of {"arm":{"name":"B0-none"},"duration":"10m","scenario":{"name":"W1","nodes":6},"seed":1}.
	if want := "g__seed-1__525c8109"; idA != want {
		t.Errorf("run ID %s, want %s", idA, want)
	}
	if again := mustExpand(t, a)[0].RunID; again != idA {
		t.Errorf("re-expansion changed the ID from %s to %s", idA, again)
	}
	other := mustExpand(t, strings.Replace(a, "duration: 10m", "duration: 20m", 1))[0].RunID
	if other == idA || !strings.HasPrefix(other, "g__seed-1__") {
		t.Errorf("a different config got ID %s next to %s; want the same description and a different hash", other, idA)
	}

	cells := mustExpand(t, `
name: charset
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: B0-none}
groups:
- name: Exp 1/A (pilot)
  vary:
    arm: [B0-none, {name: B5-descheduler, descheduler: {threshold: 0.4, interval: 5m}}]
    scenario.params.label: ["Mixed Case/with spaces & symbols!", ""]
    budget: ["10%", "50%"]
    seed: {from: 1, to: 2}
`)
	shape := regexp.MustCompile(`^[a-z0-9.-]+__[a-z0-9.-]+(_[a-z0-9.-]+)*__[0-9a-f]{8}$`)
	charset := regexp.MustCompile(`^[a-z0-9._-]+$`)
	seen := map[string]bool{}
	for _, c := range cells {
		if !charset.MatchString(c.RunID) || !shape.MatchString(c.RunID) {
			t.Errorf("run ID %q is not <group>__<description>__<8 hex> over [a-z0-9._-]", c.RunID)
		}
		if !strings.HasPrefix(c.RunID, "exp-1-a-pilot__") {
			t.Errorf("run ID %s does not start with the sanitized group name", c.RunID)
		}
		if seen[c.RunID] {
			t.Errorf("duplicate run ID %s", c.RunID)
		}
		seen[c.RunID] = true
	}
	if want := "exp-1-a-pilot__arm-b5-descheduler_budget-10_label-mixed-case-with-spaces-symbols_seed-1__"; !strings.HasPrefix(cells[8].RunID, want) {
		t.Errorf("run ID %s, want prefix %s", cells[8].RunID, want)
	}

	for name, tc := range map[string]struct{ body, want string }{
		"duplicate config": {"- {name: g, vary: {seed: [1, 1]}}", "same config"},
		"duplicate group":  {"- {name: g}\n- {name: g}", "duplicate group"},
		"prefix collision": {"- {name: a b}\n- {name: a-b}", "both give run ID prefix a-b"},
		"unnamed group":    {"- {set: {seed: 1}}", "has no name"},
		"unusable name":    {"- {name: '!!'}", "no characters usable"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := expandMatrix(t, "name: bad\nbase: {scenario: {name: W1, nodes: 6}, arm: B0-none}\ngroups:\n"+tc.body+"\n")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("got error %v, want one mentioning %q", err, tc.want)
			}
		})
	}
}

// fakeCells stands in for the subprocess: it records what was launched and succeeds or fails as told.
type fakeCells struct {
	mu       sync.Mutex
	launched []string
	exitCode int
	summary  bool
	// onLaunch runs inside each launch, before it returns.
	onLaunch func()
}

func (f *fakeCells) launch(dir string, log *os.File) (int, error) {
	f.mu.Lock()
	f.launched = append(f.launched, filepath.Base(dir))
	f.mu.Unlock()
	if _, err := fmt.Fprintln(log, "fake cell"); err != nil {
		return -1, err
	}
	if f.summary {
		if err := os.WriteFile(filepath.Join(dir, "summary.json"), []byte("{}\n"), 0o644); err != nil {
			return -1, err
		}
	}
	if f.onLaunch != nil {
		f.onLaunch()
	}
	return f.exitCode, nil
}

func (f *fakeCells) take() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := f.launched
	f.launched = nil
	slices.Sort(out)
	return out
}

func testRunner(out string, f *fakeCells) *matrixRunner {
	return &matrixRunner{name: "test", outDir: out, workers: 2, launch: f.launch, now: time.Now, stdout: io.Discard}
}

func readIndex(t *testing.T, out string) []indexEntry {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(out, "index.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var entries []indexEntry
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if line == "" {
			continue
		}
		var e indexEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			t.Fatalf("index line %q, %v", line, err)
		}
		entries = append(entries, e)
	}
	return entries
}

const threeCells = `
name: three
base: {scenario: {name: W1, nodes: 6}, duration: 10m, arm: B0-none}
groups:
- {name: g, vary: {seed: [1, 2, 3]}}
`

func TestMatrixSkipsDoneCells(t *testing.T) {
	cells := mustExpand(t, threeCells)
	out := t.TempDir()
	for _, c := range cells {
		if err := os.MkdirAll(filepath.Join(out, c.RunID), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// Cell 0 finished earlier; cell 1 was interrupted and left a stale file behind.
	if err := os.WriteFile(filepath.Join(out, cells[0].RunID, "summary.json"), []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(out, cells[1].RunID, "decisions.jsonl")
	if err := os.WriteFile(stale, []byte("stale\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f := &fakeCells{summary: true}
	counts, err := testRunner(out, f).run(cells)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := f.take(), []string{cells[1].RunID, cells[2].RunID}; !slices.Equal(got, want) {
		t.Errorf("launched %v, want %v", got, want)
	}
	if counts != (matrixCounts{Total: 3, Done: 3}) {
		t.Errorf("counts %+v, want all 3 done", counts)
	}
	if exists(stale) {
		t.Errorf("an interrupted attempt's files should be cleared before relaunching")
	}
	for _, c := range cells[1:] {
		var cfg sim.Config
		raw, err := os.ReadFile(filepath.Join(out, c.RunID, "cell.json"))
		if err != nil {
			t.Fatal(err)
		}
		if err := decodeStrict(raw, &cfg); err != nil || cfg.RunID != c.RunID {
			t.Errorf("cell.json of %s: runID %q, err %v", c.RunID, cfg.RunID, err)
		}
		if !exists(filepath.Join(out, c.RunID, "run.log")) {
			t.Errorf("%s has no run.log", c.RunID)
		}
	}
	if entries := readIndex(t, out); len(entries) != 2 || entries[0].Status != statusDone || entries[1].Status != statusDone || entries[0].Group != "g" {
		t.Errorf("index %+v, want two done lines", entries)
	}
	if counts, err := testRunner(out, f).run(cells); err != nil || counts.Done != 3 || len(f.take()) != 0 {
		t.Errorf("a complete matrix should launch nothing; counts %+v, err %v", counts, err)
	}
	if entries := readIndex(t, out); len(entries) != 2 {
		t.Errorf("a complete matrix should append nothing, index has %d lines", len(entries))
	}
}

func TestMatrixRetriesThenAbandons(t *testing.T) {
	cells := mustExpand(t, threeCells)[:1]
	id := cells[0].RunID
	out := t.TempDir()
	f := &fakeCells{exitCode: 3, summary: true}
	for attempt, want := range []matrixCounts{{Total: 1, Pending: 1}, {Total: 1, Failed: 1}, {Total: 1, Failed: 1}} {
		counts, err := testRunner(out, f).run(cells)
		if err != nil {
			t.Fatal(err)
		}
		if counts != want {
			t.Errorf("after invocation %d counts %+v, want %+v", attempt+1, counts, want)
		}
		if launched := f.take(); len(launched) != min(1, maxAttempts-attempt) {
			t.Errorf("invocation %d launched %v", attempt+1, launched)
		}
	}
	for n := 1; n <= maxAttempts; n++ {
		if !exists(filepath.Join(out, fmt.Sprintf("%s.failed-%d", id, n), "run.log")) {
			t.Errorf("attempt %d was not set aside with its log", n)
		}
	}
	if exists(filepath.Join(out, id)) {
		t.Errorf("a failed attempt's directory should be renamed, or the cell would look interrupted")
	}
	entries := readIndex(t, out)
	if len(entries) != 2 || entries[0].Status != statusFailed || entries[1].Status != statusAbandoned ||
		entries[0].ExitCode != 3 || entries[1].Attempt != 2 {
		t.Errorf("index %+v, want failed then abandoned with exit code 3", entries)
	}
	// Exiting 0 without a summary is a failure too.
	out = t.TempDir()
	if counts, err := testRunner(out, &fakeCells{}).run(cells); err != nil || counts.Pending != 1 || !exists(filepath.Join(out, id+".failed-1")) {
		t.Errorf("a cell that wrote no summary.json should fail; counts %+v, err %v", counts, err)
	}
}

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func TestMatrixWallBudget(t *testing.T) {
	cells := mustExpand(t, threeCells)
	clk := &fakeClock{t: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
	f := &fakeCells{summary: true, onLaunch: func() { clk.advance(2 * time.Minute) }}
	var stdout bytes.Buffer
	r := testRunner(t.TempDir(), f)
	r.workers, r.maxWall, r.now, r.stdout = 1, time.Minute, clk.now, &stdout
	counts, err := r.run(cells)
	if err != nil {
		t.Fatal(err)
	}
	if launched := f.take(); !slices.Equal(launched, []string{cells[0].RunID}) {
		t.Errorf("launched %v, want only the first cell before the budget ran out", launched)
	}
	if counts != (matrixCounts{Total: 3, Done: 1, Pending: 2}) {
		t.Errorf("counts %+v", counts)
	}
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if got := lines[len(lines)-1]; got != "matrix test: 1/3 done, 0 failed, 2 pending" {
		t.Errorf("final line %q", got)
	}
	if !strings.Contains(stdout.String(), "wall budget 1m0s spent") {
		t.Errorf("output does not say the budget ran out:\n%s", stdout.String())
	}
}

func TestMatrixLock(t *testing.T) {
	out := t.TempDir()
	held, ok, err := tryLock(filepath.Join(out, ".matrix.lock"))
	if err != nil || !ok {
		t.Fatalf("taking a free lock: ok %v, err %v", ok, err)
	}
	if held == nil {
		t.Skip("no flock on this platform")
	}
	defer held.Close()
	cells := mustExpand(t, threeCells)
	f := &fakeCells{summary: true}
	r := testRunner(out, f)
	r.maxWall = time.Nanosecond
	counts, err := r.run(cells)
	if err != nil {
		t.Fatal(err)
	}
	if launched := f.take(); len(launched) != 0 || counts.Pending != 3 {
		t.Errorf("a runner that never got the lock launched %v; counts %+v", launched, counts)
	}
}

func TestMatrixCommand(t *testing.T) {
	path := writeMatrix(t, threeCells+"- {name: h, vary: {seed: [4]}}\n")
	out := filepath.Join(t.TempDir(), "runs")
	var stdout bytes.Buffer
	if err := matrixMain([]string{"--matrix", path, "--out", out, "--dry-run", "--only", "h, g"}, &stdout); err != nil {
		t.Fatal(err)
	}
	if ids := strings.Fields(stdout.String()); len(ids) != 4 || !strings.HasPrefix(ids[0], "g__seed-1__") || !strings.HasPrefix(ids[3], "h__seed-4__") {
		t.Errorf("dry run printed %v", ids)
	}
	if exists(out) {
		t.Errorf("a dry run should write nothing")
	}
	for name, args := range map[string][]string{
		"no matrix":     {"--dry-run"},
		"bad workers":   {"--matrix", path, "--workers", "0"},
		"unknown group": {"--matrix", path, "--only", "nope", "--dry-run"},
		"missing file":  {"--matrix", path + ".missing"},
		"malformed":     {"--matrix", writeMatrix(t, "name: x\ngroups: [{name: g, vary: {seed: 5}}]\n")},
		"unknown key":   {"--matrix", writeMatrix(t, "name: x\nbase: {}\ngroups: [{name: g}]\nextra: 1\n")},
	} {
		t.Run(name, func(t *testing.T) {
			if err := matrixMain(args, io.Discard); err == nil {
				t.Errorf("%v succeeded", args)
			}
		})
	}
}

// TestMatrixSubprocess runs a tiny real matrix through the built binary, exactly as a caller would.
func TestMatrixSubprocess(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the simulator and runs two cells")
	}
	goBin, err := exec.LookPath("go")
	if err != nil {
		t.Skip("no go toolchain on PATH")
	}
	tmp := t.TempDir()
	bin := filepath.Join(tmp, "consolidation-sim")
	if out, err := exec.Command(goBin, "build", "-o", bin, ".").CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	path := filepath.Join(tmp, "tiny.yaml")
	if err := os.WriteFile(path, []byte(`
name: tiny
base: {scenario: {name: W1, nodes: 6}, duration: 10m, seed: 1}
groups:
- {name: tiny, vary: {arm: [B0-none, B2-when-empty-or-underutilized]}}
`), 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(tmp, "runs")
	matrix := func(extra ...string) string {
		t.Helper()
		args := append([]string{"matrix", "--matrix", path, "--out", out, "--workers", "2", "--max-wall", "10m", "--cell-max-wall", "5m"}, extra...)
		got, err := exec.Command(bin, args...).CombinedOutput()
		if err != nil {
			t.Fatalf("matrix %v: %v\n%s", extra, err, got)
		}
		return string(got)
	}
	finalLine := func(s string) string {
		lines := strings.Split(strings.TrimSpace(s), "\n")
		return lines[len(lines)-1]
	}
	ids := strings.Fields(matrix("--dry-run"))
	if len(ids) != 2 {
		t.Fatalf("dry run printed %v, want 2 run IDs", ids)
	}
	if got := matrix(); finalLine(got) != "matrix tiny: 2/2 done, 0 failed, 0 pending" {
		t.Fatalf("first invocation:\n%s", got)
	}
	for i, arm := range []string{sim.ArmNone, sim.ArmUnderutil} {
		dir := filepath.Join(out, ids[i])
		var sum sim.Summary
		raw, err := os.ReadFile(filepath.Join(dir, "summary.json"))
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(raw, &sum); err != nil {
			t.Fatal(err)
		}
		if sum.RunID != ids[i] || sum.Arm != arm || sum.Nodes != 6 || sum.Error != "" {
			t.Errorf("summary of %s: run %s, arm %s, nodes %d, error %q", ids[i], sum.RunID, sum.Arm, sum.Nodes, sum.Error)
		}
		for _, f := range []string{"cell.json", "run.log", "manifest.json", "decisions.jsonl"} {
			if !exists(filepath.Join(dir, f)) {
				t.Errorf("%s has no %s", ids[i], f)
			}
		}
	}
	entries := readIndex(t, out)
	if len(entries) != 2 {
		t.Fatalf("index has %d lines, want 2", len(entries))
	}
	for _, e := range entries {
		if e.Status != statusDone || e.ExitCode != 0 || e.Group != "tiny" || !slices.Contains(ids, e.RunID) {
			t.Errorf("index line %+v", e)
		}
	}
	again := matrix()
	if finalLine(again) != "matrix tiny: 2/2 done, 0 failed, 0 pending" || strings.Contains(again, "] done ") {
		t.Errorf("re-invoking a complete matrix should run nothing:\n%s", again)
	}
	if n := len(readIndex(t, out)); n != 2 {
		t.Errorf("re-invoking appended to the index, now %d lines", n)
	}
}
