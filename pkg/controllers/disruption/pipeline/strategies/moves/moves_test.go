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

package moves_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	clocktesting "k8s.io/utils/clock/testing"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/conformance"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/moves"
)

const gib = 1 << 30

// configs are the generator configurations every shared check runs over: defaults, plus the non-default branches.
var configs = []struct {
	name   string
	params pipeline.Params
	opts   conformance.GeneratorOptions
}{
	{name: "random-merge"},
	{name: "random-merge", params: pipeline.Params{"maxSources": 1, "launch": false, "maxMoves": 5}},
	{name: "random-shift"},
	{name: "random-shift", params: pipeline.Params{"maxPodsPerMove": 1, "full": true}},
	{name: "greedy-shift"},
	{name: "greedy-shift", params: pipeline.Params{"maxPodsPerMove": 1}},
	// Random states rarely strand a node by the default gap, so finding nothing is allowed; TestSwapFindsExchange
	// shows the swap on a stranded state.
	{name: "swap", opts: conformance.GeneratorOptions{AllowEmpty: true}},
	{name: "swap", params: pipeline.Params{"strandingGap": 0.1}, opts: conformance.GeneratorOptions{AllowEmpty: true}},
}

func label(name string, p pipeline.Params) string {
	if len(p) == 0 {
		return name
	}
	keys := make([]string, 0, len(p))
	for k := range p {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s=%v", k, p[k])
	}
	return name + "/" + strings.Join(parts, ",")
}

func newGenerator(t testing.TB, env *pipeline.Env, name string, p pipeline.Params) pipeline.MoveGenerator {
	t.Helper()
	g, err := pipeline.NewMoveGenerator(env, pipeline.StageConfig{Name: name, Params: p})
	if err != nil {
		t.Fatalf("%s: %v", label(name, p), err)
	}
	return g
}

func TestConformance(t *testing.T) {
	for _, c := range configs {
		t.Run(label(c.name, c.params), func(t *testing.T) {
			conformance.CheckGenerator(t, func(env *pipeline.Env) pipeline.MoveGenerator {
				return newGenerator(t, env, c.name, c.params)
			}, c.opts)
		})
	}
}

func TestRegistered(t *testing.T) {
	reg := map[string]bool{}
	for _, n := range pipeline.Registered()[pipeline.StageMoves] {
		reg[n] = true
	}
	for _, n := range []string{"random-merge", "random-shift", "greedy-shift", "swap"} {
		if !reg[n] {
			t.Errorf("%s is not registered", n)
		}
	}
}

func TestParams(t *testing.T) {
	valid := []struct {
		name   string
		params pipeline.Params
	}{
		{"random-merge", nil},
		{"random-merge", pipeline.Params{"maxSources": 1, "launch": false, "maxMoves": 1}},
		{"random-shift", nil},
		{"random-shift", pipeline.Params{"maxPodsPerMove": 10, "full": true}},
		{"greedy-shift", nil},
		{"greedy-shift", pipeline.Params{"maxPodsPerMove": 1}},
		{"swap", nil},
		{"swap", pipeline.Params{"strandingGap": 0}},
		{"swap", pipeline.Params{"strandingGap": 0.99}},
	}
	for _, c := range valid {
		if _, err := pipeline.NewMoveGenerator(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: c.name, Params: c.params}); err != nil {
			t.Errorf("%s: unexpected error %v", label(c.name, c.params), err)
		}
	}
	invalid := []struct {
		name   string
		params pipeline.Params
	}{
		{"random-merge", pipeline.Params{"maxSources": 0}},
		{"random-merge", pipeline.Params{"maxSources": -2}},
		{"random-merge", pipeline.Params{"maxSources": 2.5}},
		{"random-merge", pipeline.Params{"maxMoves": 0}},
		{"random-merge", pipeline.Params{"launch": "yes"}},
		{"random-merge", pipeline.Params{"maxSource": 3}},
		{"random-shift", pipeline.Params{"maxPodsPerMove": 0}},
		{"random-shift", pipeline.Params{"full": 1}},
		{"random-shift", pipeline.Params{"maxPods": 4}},
		{"greedy-shift", pipeline.Params{"maxPodsPerMove": 0}},
		{"greedy-shift", pipeline.Params{"full": true}},
		{"swap", pipeline.Params{"strandingGap": -0.1}},
		{"swap", pipeline.Params{"strandingGap": 1}},
		{"swap", pipeline.Params{"strandingGap": 1.5}},
		{"swap", pipeline.Params{"gap": 0.3}},
	}
	for _, c := range invalid {
		if _, err := pipeline.NewMoveGenerator(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: c.name, Params: c.params}); err == nil {
			t.Errorf("%s: expected an error", label(c.name, c.params))
		}
	}
}

// --- harness ---------------------------------------------------------------------------------------------------

func randomState(seed int64) *pipeline.State {
	return pipeline.RandomState(rand.New(rand.NewSource(seed)), pipeline.SyntheticOptions{ //nolint:gosec
		Nodes: 8 + int(seed%25), DoNotDisruptProb: 0.08, PDBProb: 0.2, NodePools: 1 + int(seed%2), NonSourceProb: 0.1})
}

func eligible(s pipeline.Snapshot) (src, dst []pipeline.NodeView) {
	for _, n := range s.Nodes() {
		if n.Source {
			src = append(src, n)
		}
		if n.Destination {
			dst = append(dst, n)
		}
	}
	return src, dst
}

// stubFeasibility is Fast from the snapshot and a scripted Full that counts its calls and checks each was reserved
// with UseFull first.
type stubFeasibility struct {
	*pipeline.FastChecker
	t      testing.TB
	budget *pipeline.SearchBudget
	// confirm scripts Full; nil rejects every move.
	confirm func(s pipeline.Snapshot, m pipeline.Move) (pipeline.Move, error)
	calls   int
	bySet   map[string]int
}

func newStub(t testing.TB, confirm func(pipeline.Snapshot, pipeline.Move) (pipeline.Move, error)) *stubFeasibility {
	return &stubFeasibility{FastChecker: &pipeline.FastChecker{}, t: t, confirm: confirm, bySet: map[string]int{}}
}

func (f *stubFeasibility) Full(_ context.Context, s pipeline.Snapshot, m pipeline.Move) (pipeline.Move, error) {
	f.calls++
	if f.budget != nil {
		if _, reserved, _, _ := f.budget.Stats(); f.calls > reserved {
			f.t.Errorf("Full call %d made with only %d simulations reserved through UseFull", f.calls, reserved)
		}
	}
	f.bySet[strings.Join(m.Sources, ",")+"|"+fmt.Sprint(m.Bindings)]++
	if f.confirm == nil {
		return m, pipeline.ErrInfeasible
	}
	return f.confirm(s, m)
}

// launchAll confirms a merge onto one large replacement, as Karpenter's simulation would when nothing else fits.
func launchAll(s pipeline.Snapshot, m pipeline.Move) (pipeline.Move, error) {
	c := m
	c.Launch = []pipeline.LaunchSpec{{NodePool: "pool-0", InstanceTypes: []string{"big"},
		Capacity: pipeline.Resources{pipeline.CPU: 1e6, pipeline.Memory: 1e6 * gib, pipeline.Pods: 1000}, Price: 0.01}}
	c.Bindings = nil
	for _, src := range m.Sources {
		for _, p := range s.Pods(src) {
			if p.Reschedulable {
				c.Bindings = append(c.Bindings, pipeline.Binding{Pod: p.Key, From: src, Target: pipeline.Target{Launch: 0}})
			}
		}
	}
	c.Upstream = "confirmed"
	return c, nil
}

type runOptions struct {
	maxAttempts, maxFull int
	feasibility          pipeline.Feasibility
	scorer               pipeline.Scorer
	noScorer             bool
}

func generate(t testing.TB, g pipeline.MoveGenerator, s pipeline.Snapshot, seed int64, o runOptions) ([]pipeline.Move, *pipeline.SearchBudget) {
	t.Helper()
	env := pipeline.SyntheticEnv(nil)
	b := pipeline.NewSearchBudget(o.maxAttempts, o.maxFull, time.Time{}, nil)
	b.Feasibility, b.Scorer = env.Feasibility, env.Scorer
	if o.feasibility != nil {
		b.Feasibility = o.feasibility
	}
	if o.scorer != nil {
		b.Scorer = o.scorer
	}
	if o.noScorer {
		b.Scorer = nil
	}
	if stub, ok := o.feasibility.(*stubFeasibility); ok {
		stub.budget = b
	}
	src, dst := eligible(s)
	out, err := g.Generate(context.Background(), s, src, dst, rand.New(rand.NewSource(seed)), b) //nolint:gosec
	if err != nil {
		t.Fatalf("%s: %v", g.Name(), err)
	}
	return out, b
}

// checkMove holds a move to the contract every generator promises, independently of Fast: it applies, binds no
// do-not-disrupt pod and nothing off a do-not-disrupt node, and stays within every PDB allowance.
//
//nolint:gocyclo
func checkMove(t testing.TB, s pipeline.Snapshot, m pipeline.Move) {
	t.Helper()
	if m.ID == "" || m.Generator == "" {
		t.Errorf("%s: missing ID or generator", m)
	}
	if err := (&pipeline.FastChecker{}).Fast(s, m); err != nil {
		t.Errorf("%s fails Fast: %v", m, err)
	}
	if _, err := s.Apply(m); err != nil {
		t.Errorf("%s does not apply: %v", m, err)
	}
	if !sort.SliceIsSorted(m.Bindings, func(i, j int) bool { return m.Bindings[i].Pod.String() < m.Bindings[j].Pod.String() }) {
		t.Errorf("%s: bindings are not sorted", m)
	}
	evictions := map[string]int{}
	for _, b := range m.Bindings {
		from, _ := s.Node(b.From)
		if from.DoNotDisrupt {
			t.Errorf("%s moves %s off do-not-disrupt node %s", m, b.Pod, b.From)
		}
		for _, p := range s.Pods(b.From) {
			if p.Key != b.Pod {
				continue
			}
			if p.DoNotDisrupt {
				t.Errorf("%s moves do-not-disrupt pod %s", m, b.Pod)
			}
			for _, pdb := range p.PDBs {
				evictions[pdb]++
				if allowed := s.PDBAllowed(pdb); allowed >= 0 && evictions[pdb] > allowed {
					t.Errorf("%s exceeds %s, which allows %d", m, pdb, allowed)
				}
			}
		}
	}
}

func dominant(s pipeline.Snapshot, node string) float64 {
	n, _ := s.Node(node)
	return s.Load(node).DominantShare(n.Capacity)
}

// --- random-merge ----------------------------------------------------------------------------------------------

// TestRandomMergeEmptiesSources checks every merge binds every reschedulable pod of every source, never onto a
// source, removes exactly its sources, draws at most K of them, and never repeats a source set.
func TestRandomMergeEmptiesSources(t *testing.T) {
	found := 0
	for seed := int64(1); seed <= 30; seed++ {
		s := randomState(seed)
		for _, params := range []pipeline.Params{nil, {"maxSources": 5}} {
			g := newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", params)
			ms, _ := generate(t, g, s, seed, runOptions{maxAttempts: 300})
			found += len(ms)
			k := 3
			if params != nil {
				k = 5
			}
			sets := map[string]bool{}
			for _, m := range ms {
				checkMerge(t, s, m, k)
				key := strings.Join(m.Sources, ",")
				if sets[key] {
					t.Errorf("seed %d: source set %s emitted twice", seed, key)
				}
				sets[key] = true
			}
		}
	}
	if found == 0 {
		t.Fatal("random-merge found no merges on any state")
	}
}

//nolint:gocyclo
func checkMerge(t *testing.T, s pipeline.Snapshot, m pipeline.Move, k int) {
	t.Helper()
	checkMove(t, s, m)
	if m.Kind != pipeline.Merge || len(m.Sources) < 1 || len(m.Sources) > k {
		t.Errorf("%s is not a merge of 1 to %d sources", m, k)
	}
	if !sort.StringsAreSorted(m.Sources) || !slices.Equal(m.Sources, m.Remove) {
		t.Errorf("%s: sources %v and removals %v differ or are unsorted", m, m.Sources, m.Remove)
	}
	bound := map[pipeline.PodKey]string{}
	for _, b := range m.Bindings {
		bound[b.Pod] = b.Target.Node
	}
	for _, src := range m.Sources {
		for _, p := range s.Pods(src) {
			if !p.Reschedulable {
				continue
			}
			if to, ok := bound[p.Key]; !ok || slices.Contains(m.Sources, to) {
				t.Errorf("%s leaves %s on %s unbound or bound onto a source", m, p.Key, src)
			}
		}
	}
}

func TestRandomMergeMaxMoves(t *testing.T) {
	capped := false
	for seed := int64(1); seed <= 20; seed++ {
		s := randomState(seed)
		two, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", pipeline.Params{"maxMoves": 2}), s, seed, runOptions{maxAttempts: 2000})
		all, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", nil), s, seed, runOptions{maxAttempts: 2000})
		if len(two) > 2 || len(all) > 50 {
			t.Errorf("seed %d: %d and %d moves exceed maxMoves 2 and 50", seed, len(two), len(all))
		}
		capped = capped || len(all) > 2
	}
	if !capped {
		t.Error("no state had more than 2 merges, so the cap was never exercised")
	}
}

// twoFullNodes holds two 75%-full nodes, so neither can absorb the other: only a replacement can merge them.
func twoFullNodes() *pipeline.State {
	var nodes []pipeline.NodeView
	var pods []pipeline.PodView
	for _, name := range []string{"node-a", "node-b"} {
		nodes = append(nodes, pipeline.NodeView{Name: name, NodePool: "pool-0", Capacity: pipeline.Resources{pipeline.CPU: 4000, pipeline.Memory: 16 * gib, pipeline.Pods: 110},
			Price: 0.2, Source: true, Destination: true, DisruptionCost: 4})
		for i := range 3 {
			pods = append(pods, pipeline.PodView{Key: pipeline.PodKey{Namespace: "default", Name: fmt.Sprintf("%s-%d", name, i)}, Node: name,
				Requests: pipeline.Resources{pipeline.CPU: 1000, pipeline.Memory: gib, pipeline.Pods: 1}, EvictionCost: 1, Reschedulable: true})
		}
	}
	return pipeline.NewState(nodes, pods, map[string]pipeline.Totals{"pool-0": {TotalCost: 0.4, TotalDisruptionCost: 8}}, nil)
}

// TestRandomMergeLaunch checks the replacement path: a set whose pods fit nowhere is confirmed by Full once, and the
// confirmed move, with Karpenter's replacement, is what is emitted.
//
//nolint:gocyclo
func TestRandomMergeLaunch(t *testing.T) {
	s := twoFullNodes()
	stub := newStub(t, launchAll)
	ms, b := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", nil), s, 1, runOptions{maxAttempts: 100, feasibility: stub})
	if len(ms) != 3 {
		t.Fatalf("got %d merges, want one per source set ({a}, {b}, {a, b}): %v", len(ms), ms)
	}
	for _, m := range ms {
		checkMove(t, s, m)
		if len(m.Launch) != 1 || m.Upstream != "confirmed" {
			t.Errorf("%s is not the move Full confirmed", m)
		}
	}
	for set, n := range stub.bySet {
		if n != 1 {
			t.Errorf("Full simulated source set %s %d times", set, n)
		}
	}
	if _, full, _, _ := b.Stats(); full != 3 || stub.calls != 3 {
		t.Errorf("reserved %d and made %d Full calls, want 3", full, stub.calls)
	}

	// Without launch, or with the simulation budget spent, nothing can merge these nodes.
	stub = newStub(t, launchAll)
	ms, _ = generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", pipeline.Params{"launch": false}), s, 1, runOptions{maxAttempts: 100, feasibility: stub})
	if len(ms) != 0 || stub.calls != 0 {
		t.Errorf("launch=false: got %d moves and %d Full calls, want none", len(ms), stub.calls)
	}
	stub = newStub(t, launchAll)
	ms, _ = generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-merge", nil), s, 1, runOptions{maxAttempts: 100, maxFull: 1, feasibility: stub})
	if len(ms) != 1 || stub.calls != 1 {
		t.Errorf("maxFull=1: got %d moves and %d Full calls, want 1 and 1", len(ms), stub.calls)
	}
}

// --- random-shift ----------------------------------------------------------------------------------------------

// TestRandomShiftTargetsFullerNodes checks every shift moves 1 to m pods off one source onto one destination that is
// strictly more utilized, and that no move repeats within a call.
//
//nolint:gocyclo
func TestRandomShiftTargetsFullerNodes(t *testing.T) {
	found := 0
	for seed := int64(1); seed <= 30; seed++ {
		s := randomState(seed)
		for _, maxPods := range []int{1, 4, 9} {
			ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-shift", pipeline.Params{"maxPodsPerMove": maxPods}), s, seed, runOptions{maxAttempts: 200})
			found += len(ms)
			seen := map[string]bool{}
			for _, m := range ms {
				checkMove(t, s, m)
				if m.Kind != pipeline.Shift || len(m.Sources) != 1 || len(m.Destinations()) != 1 {
					t.Fatalf("seed %d: %s is not a one-source, one-destination shift", seed, m)
				}
				if n := len(m.Bindings); n < 1 || n > maxPods {
					t.Errorf("seed %d: %s moves %d pods, want 1 to %d", seed, m, n, maxPods)
				}
				from, to := m.Sources[0], m.Destinations()[0]
				if dominant(s, to) <= dominant(s, from) {
					t.Errorf("seed %d: %s targets %s at %.3f, not fuller than %s at %.3f", seed, m, to, dominant(s, to), from, dominant(s, from))
				}
				key := fmt.Sprint(m.Bindings)
				if seen[key] {
					t.Errorf("seed %d: %s repeats an earlier move", seed, m)
				}
				seen[key] = true
			}
		}
	}
	if found == 0 {
		t.Fatal("random-shift found no shifts on any state")
	}
}

// TestRandomShiftFull checks full=true drops shifts Full rejects, and full=false never simulates.
func TestRandomShiftFull(t *testing.T) {
	for seed := int64(1); seed <= 10; seed++ {
		s := randomState(seed)
		stub := newStub(t, nil)
		ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-shift", pipeline.Params{"full": true}), s, seed, runOptions{maxAttempts: 100, feasibility: stub})
		if len(ms) != 0 {
			t.Errorf("seed %d: emitted %d shifts Full rejected", seed, len(ms))
		}
		plain := newStub(t, nil)
		ms, _ = generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "random-shift", nil), s, seed, runOptions{maxAttempts: 100, feasibility: plain})
		if plain.calls != 0 {
			t.Errorf("seed %d: full=false made %d Full calls", seed, plain.calls)
		}
		if len(ms) != stub.calls {
			t.Errorf("seed %d: full=true simulated %d shifts, but full=false emitted %d from the same draws", seed, stub.calls, len(ms))
		}
	}
}

// --- greedy-shift ----------------------------------------------------------------------------------------------

// TestGreedyShiftLowersPotential checks every emitted shift lowers the run's potential, is at least as good as every
// Fast-feasible single-pod shift of its pair, and is the only move emitted for its pair.
//
//nolint:gocyclo
func TestGreedyShiftLowersPotential(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	fast := &pipeline.FastChecker{}
	found := 0
	for seed := int64(1); seed <= 30; seed++ {
		s := randomState(seed)
		ms, _ := generate(t, newGenerator(t, env, "greedy-shift", nil), s, seed, runOptions{maxAttempts: 200})
		found += len(ms)
		pairs := map[string]bool{}
		for _, m := range ms {
			checkMove(t, s, m)
			if m.Kind != pipeline.Shift || len(m.Sources) != 1 || len(m.Destinations()) != 1 || len(m.Bindings) > 4 {
				t.Fatalf("seed %d: %s is not a one-source, one-destination shift of at most 4 pods", seed, m)
			}
			dp := env.Scorer.Score(s, m).DeltaPotential
			if dp >= 0 {
				t.Errorf("seed %d: %s has ΔΦ %.6g, want < 0", seed, m, dp)
			}
			from, to := m.Sources[0], m.Destinations()[0]
			if pairs[from+">"+to] {
				t.Errorf("seed %d: pair %s>%s emitted twice", seed, from, to)
			}
			pairs[from+">"+to] = true
			for _, p := range s.Pods(from) {
				single := pipeline.Move{Kind: pipeline.Shift, Sources: []string{from}, Bindings: []pipeline.Binding{{Pod: p.Key, From: from, Target: pipeline.ExistingTarget(to)}}}
				if fast.Fast(s, single) != nil {
					continue
				}
				if sdp := env.Scorer.Score(s, single).DeltaPotential; sdp < dp-1e-12 {
					t.Errorf("seed %d: %s has ΔΦ %.6g, but moving %s alone gives %.6g", seed, m, dp, p.Key, sdp)
				}
			}
		}
	}
	if found == 0 {
		t.Fatal("greedy-shift found no shifts on any state")
	}
}

// TestGreedyShiftFallbackScorer checks a budget without a Scorer falls back to util-entropy, and a potential that
// never drops yields nothing.
func TestGreedyShiftFallbackScorer(t *testing.T) {
	entropy, err := pipeline.NewUtilEntropy(pipeline.PotentialParams{})
	if err != nil {
		t.Fatal(err)
	}
	none, err := pipeline.NewPotential(pipeline.StageConfig{Name: "none"})
	if err != nil {
		t.Fatal(err)
	}
	found := 0
	for seed := int64(1); seed <= 15; seed++ {
		s := randomState(seed)
		ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "greedy-shift", nil), s, seed, runOptions{maxAttempts: 100, noScorer: true})
		found += len(ms)
		for _, m := range ms {
			if d := entropy.Delta(s, m); d >= 0 {
				t.Errorf("seed %d: %s has util-entropy ΔΦ %.6g, want < 0", seed, m, d)
			}
		}
		flat, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "greedy-shift", nil), s, seed, runOptions{maxAttempts: 100, scorer: pipeline.DefaultScorer{Potential: none}})
		if len(flat) != 0 {
			t.Errorf("seed %d: emitted %d shifts under a constant potential", seed, len(flat))
		}
	}
	if found == 0 {
		t.Fatal("greedy-shift found no shifts with the fallback scorer")
	}
}

// TestGreedyShiftFills checks the greedy fill: emptying a 20% node into a 60% one lowers util-entropy more than moving
// either of its pods alone, so the best move takes both, unless maxPodsPerMove forbids it.
func TestGreedyShiftFills(t *testing.T) {
	capacity := pipeline.Resources{pipeline.CPU: 4000, pipeline.Memory: 16 * gib, pipeline.Pods: 110}
	nodes := []pipeline.NodeView{
		{Name: "low", NodePool: "pool-0", Capacity: capacity, Price: 0.2, Source: true, Destination: true},
		{Name: "high", NodePool: "pool-0", Capacity: capacity, Price: 0.2, Source: true, Destination: true},
	}
	var pods []pipeline.PodView
	add := func(node string, n int) {
		for i := range n {
			pods = append(pods, pipeline.PodView{Key: pipeline.PodKey{Namespace: "default", Name: fmt.Sprintf("%s-%d", node, i)}, Node: node,
				Requests: pipeline.Resources{pipeline.CPU: 400, pipeline.Memory: gib / 4, pipeline.Pods: 1}, EvictionCost: 1, Reschedulable: true})
		}
	}
	add("low", 2)
	add("high", 6)
	s := pipeline.NewState(nodes, pods, nil, nil)
	for _, c := range []struct {
		maxPods, want int
	}{{4, 2}, {1, 1}} {
		ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "greedy-shift", pipeline.Params{"maxPodsPerMove": c.maxPods}), s, 1, runOptions{maxAttempts: 50})
		if len(ms) != 1 {
			t.Fatalf("maxPodsPerMove %d: got %d moves, want one for the only pair: %v", c.maxPods, len(ms), ms)
		}
		m := ms[0]
		checkMove(t, s, m)
		if m.Sources[0] != "low" || m.Destinations()[0] != "high" || len(m.Bindings) != c.want {
			t.Errorf("maxPodsPerMove %d: got %s to %v, want %d pods from low to high", c.maxPods, m, m.Destinations(), c.want)
		}
	}
}

// --- swap ------------------------------------------------------------------------------------------------------

// strandedState has a CPU-stranded node (62.5% CPU, 12.5% memory), a memory-stranded node (19% CPU, 75% memory) and a
// balanced one.
func strandedState(mutate func(n *pipeline.NodeView)) *pipeline.State {
	capacity := pipeline.Resources{pipeline.CPU: 4000, pipeline.Memory: 16 * gib, pipeline.Pods: 110}
	nodes := []pipeline.NodeView{
		{Name: "node-a", NodePool: "pool-0", Capacity: capacity, Price: 0.2, Source: true, Destination: true},
		{Name: "node-b", NodePool: "pool-0", Capacity: capacity, Price: 0.2, Source: true, Destination: true},
		{Name: "node-c", NodePool: "pool-0", Capacity: capacity, Price: 0.2, Source: true, Destination: true},
	}
	if mutate != nil {
		for i := range nodes {
			mutate(&nodes[i])
		}
	}
	pod := func(name, node string, cpu, mem float64) pipeline.PodView {
		return pipeline.PodView{Key: pipeline.PodKey{Namespace: "default", Name: name}, Node: node,
			Requests: pipeline.Resources{pipeline.CPU: cpu, pipeline.Memory: mem, pipeline.Pods: 1}, EvictionCost: 1, Reschedulable: true}
	}
	pods := []pipeline.PodView{
		pod("a1", "node-a", 1500, gib), pod("a2", "node-a", 1000, gib),
		pod("b1", "node-b", 250, 6*gib), pod("b2", "node-b", 500, 6*gib),
		pod("c1", "node-c", 1000, 4*gib),
	}
	return pipeline.NewState(nodes, pods, nil, nil)
}

func skew(s pipeline.Snapshot, node string) float64 {
	n, _ := s.Node(node)
	share := s.Load(node).Share(n.Capacity)
	return math.Abs(share[pipeline.CPU] - share[pipeline.Memory])
}

func TestSwapFindsExchange(t *testing.T) {
	s := strandedState(nil)
	ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "swap", nil), s, 1, runOptions{maxAttempts: 20})
	if len(ms) != 1 {
		t.Fatalf("got %d moves, want the one swap: %v", len(ms), ms)
	}
	m := ms[0]
	checkMove(t, s, m)
	want := []pipeline.Binding{
		{Pod: pipeline.PodKey{Namespace: "default", Name: "a1"}, From: "node-a", Target: pipeline.ExistingTarget("node-b")},
		{Pod: pipeline.PodKey{Namespace: "default", Name: "b1"}, From: "node-b", Target: pipeline.ExistingTarget("node-a")},
	}
	if m.Kind != pipeline.Shift || strings.Join(m.Sources, ",") != "node-a,node-b" || fmt.Sprint(m.Bindings) != fmt.Sprint(want) {
		t.Fatalf("got %s with bindings %v, want a shift of a1 to node-b and b1 to node-a", m, m.Bindings)
	}
	after, err := s.Apply(m)
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"node-a", "node-b"} {
		if skew(after, n) >= skew(s, n) {
			t.Errorf("%s stranding went from %.3f to %.3f", n, skew(s, n), skew(after, n))
		}
	}

	for _, c := range []struct {
		name   string
		s      *pipeline.State
		params pipeline.Params
	}{
		{name: "gap above node-a's skew", s: s, params: pipeline.Params{"strandingGap": 0.6}},
		{name: "do-not-disrupt node", s: strandedState(func(n *pipeline.NodeView) { n.DoNotDisrupt = n.Name == "node-a" })},
		{name: "not a destination", s: strandedState(func(n *pipeline.NodeView) { n.Destination = n.Name != "node-b" })},
		{name: "not a source", s: strandedState(func(n *pipeline.NodeView) { n.Source = n.Name != "node-b" })},
	} {
		if got, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "swap", c.params), c.s, 1, runOptions{maxAttempts: 20}); len(got) != 0 {
			t.Errorf("%s: got %v, want no swap", c.name, got)
		}
	}
}

// --- shared ----------------------------------------------------------------------------------------------------

// TestBudgets checks every generator stops at MaxAttempts and MaxFull, reserves each Full call with UseFull, stops
// at a passed deadline, and terminates on its own cap when the budget sets no attempt limit. Seed 0 is the two-node
// state only a replacement can merge, so random-merge's Full cap is exercised too.
//
//nolint:gocyclo
func TestBudgets(t *testing.T) {
	limits := [][2]int{{1, 1}, {7, 2}, {40, 3}}
	for _, c := range configs {
		t.Run(label(c.name, c.params), func(t *testing.T) {
			for seed := int64(0); seed <= 10; seed++ {
				s := randomState(seed)
				if seed == 0 {
					s = twoFullNodes()
				}
				for _, lim := range limits {
					for _, confirm := range []func(pipeline.Snapshot, pipeline.Move) (pipeline.Move, error){nil, launchAll} {
						stub := newStub(t, confirm)
						_, b := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), c.name, c.params), s, seed, runOptions{maxAttempts: lim[0], maxFull: lim[1], feasibility: stub})
						attempts, full, _, _ := b.Stats()
						if attempts > lim[0] || full > lim[1] || stub.calls > full {
							t.Errorf("seed %d, limits %v: %d attempts, %d reserved and %d made Full calls", seed, lim, attempts, full, stub.calls)
						}
						if seed == 0 && c.name == "random-merge" && c.params == nil && full != lim[1] {
							t.Errorf("limits %v: random-merge reserved %d simulations on a state that needs one per source set", lim, full)
						}
					}
				}
				checkStops(t, newGenerator(t, pipeline.SyntheticEnv(nil), c.name, c.params), s, seed)
			}
		})
	}
}

// checkStops checks a generator returns without a usable attempt limit: on its own cap with an unlimited or nil
// budget, at once past the deadline, and with the context's error once it is canceled.
func checkStops(t *testing.T, g pipeline.MoveGenerator, s pipeline.Snapshot, seed int64) {
	t.Helper()
	_, b := generate(t, g, s, seed, runOptions{})
	if attempts, _, _, _ := b.Stats(); attempts > moves.DefaultMaxAttempts {
		t.Errorf("seed %d: an unlimited budget ran %d attempts, more than the %d cap", seed, attempts, moves.DefaultMaxAttempts)
	}
	src, dst := eligible(s)
	rng := func() *rand.Rand { return rand.New(rand.NewSource(seed)) } //nolint:gosec
	if _, err := g.Generate(context.Background(), s, src, dst, rng(), nil); err != nil {
		t.Errorf("seed %d: nil budget: %v", seed, err)
	}
	clk := clocktesting.NewFakeClock(time.Unix(1000, 0))
	late := pipeline.NewSearchBudget(0, 0, clk.Now().Add(-time.Second), clk)
	if ms, err := g.Generate(context.Background(), s, src, dst, rng(), late); err != nil || len(ms) != 0 {
		t.Errorf("seed %d: past the deadline got %d moves, %v", seed, len(ms), err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if ms, err := g.Generate(ctx, s, src, dst, rng(), pipeline.NewSearchBudget(10, 0, time.Time{}, nil)); len(ms) != 0 || (err != nil && !errors.Is(err, context.Canceled)) {
		t.Errorf("seed %d: with a canceled context got %d moves, %v", seed, len(ms), err)
	}
}

// TestDeterminism checks the same seed and input give byte-identical output, IDs included, and that one instance
// never reuses an ID across calls.
//
//nolint:gocyclo
func TestDeterminism(t *testing.T) {
	for _, c := range configs {
		t.Run(label(c.name, c.params), func(t *testing.T) {
			var outputs int
			for seed := int64(1); seed <= 20; seed++ {
				s := randomState(seed)
				run := func() []byte {
					ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), c.name, c.params), s, seed, runOptions{maxAttempts: 150, maxFull: 10, feasibility: newStub(t, launchAll)})
					outputs += len(ms)
					raw, err := json.Marshal(ms)
					if err != nil {
						t.Fatal(err)
					}
					return raw
				}
				if a, b := run(), run(); string(a) != string(b) {
					t.Errorf("seed %d: two runs differ:\n%s\n%s", seed, a, b)
				}
				g := newGenerator(t, pipeline.SyntheticEnv(nil), c.name, c.params)
				first, _ := generate(t, g, s, seed, runOptions{maxAttempts: 150})
				second, _ := generate(t, g, s, seed+1, runOptions{maxAttempts: 150})
				seen := map[string]bool{}
				for _, m := range append(first, second...) {
					if seen[m.ID] {
						t.Errorf("seed %d: ID %s reused across calls", seed, m.ID)
					}
					seen[m.ID] = true
				}
			}
			if outputs == 0 && !c.opts.AllowEmpty {
				t.Error("no moves on any state, so determinism was not exercised")
			}
		})
	}
	// The stranded state gives swap something to be deterministic about.
	s := strandedState(nil)
	var outs []string
	for range 2 {
		ms, _ := generate(t, newGenerator(t, pipeline.SyntheticEnv(nil), "swap", nil), s, 3, runOptions{maxAttempts: 20})
		raw, _ := json.Marshal(ms)
		outs = append(outs, string(raw))
	}
	if outs[0] != outs[1] || outs[0] == "null" {
		t.Errorf("swap on the stranded state is not deterministic or empty: %v", outs)
	}
}
