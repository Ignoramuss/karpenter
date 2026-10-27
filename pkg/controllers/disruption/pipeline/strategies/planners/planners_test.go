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

package planners_test

import (
	"context"
	"fmt"
	"maps"
	"math"
	"math/rand"
	"slices"
	"strings"
	"testing"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/conformance"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/planners"
)

var allPlanners = []string{planners.EntropyGreedyName, planners.AnnealPlanName, planners.AnnealOnlineName, planners.SequenceName}

var defaultWeights = pipeline.Weights{Alpha: planners.DefaultAlpha, Lambda: planners.DefaultLambda, Mu: planners.DefaultMu}

func newPlanner(t testing.TB, env *pipeline.Env, name string, params pipeline.Params) pipeline.Planner {
	t.Helper()
	p, err := pipeline.NewPlanner(env, pipeline.StageConfig{Name: name, Params: params})
	if err != nil {
		t.Fatalf("%s %v: %v", name, params, err)
	}
	return p
}

func TestConformance(t *testing.T) {
	variants := map[string][]pipeline.Params{
		planners.EntropyGreedyName: {nil, {"lambda": 0.0}},
		planners.AnnealPlanName:    {nil, {"lambda": 0.1, "maxLength": 3, "steps": 150}},
		planners.AnnealOnlineName:  {nil, {"t0": 10.0, "cooling": 1.0}},
		planners.SequenceName:      {nil, {"horizon": 1}, {"lambda": 0.0, "horizon": 8}},
	}
	for _, name := range allPlanners {
		for _, params := range variants[name] {
			t.Run(fmt.Sprintf("%s/%v", name, params), func(t *testing.T) {
				conformance.CheckPlanner(t, func(env *pipeline.Env) pipeline.Planner { return newPlanner(t, env, name, params) })
			})
		}
	}
}

func TestRegistered(t *testing.T) {
	reg := map[string]bool{}
	for _, n := range pipeline.Registered()[pipeline.StagePlan] {
		reg[n] = true
	}
	for _, name := range allPlanners {
		if !reg[name] {
			t.Errorf("planner %s is not registered", name)
		}
	}
}

type weighted interface{ Weights() pipeline.Weights }
type tempered interface{ Temperature() float64 }

//nolint:gocyclo
func TestParams(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	for _, name := range allPlanners {
		p := newPlanner(t, env, name, nil)
		if got := p.(weighted).Weights(); got != defaultWeights {
			t.Errorf("%s default weights %+v, want %+v", name, got, defaultWeights)
		}
		if _, ok := p.(pipeline.Guide); !ok {
			t.Errorf("%s does not implement pipeline.Guide", name)
		}
		if _, ok := p.(pipeline.FirstValid); ok {
			t.Errorf("%s implements FirstValid; generators would stop at the first move it accepts", name)
		}
		// An explicit zero is a value, not an absent key.
		p = newPlanner(t, env, name, pipeline.Params{"alpha": 2.0, "lambda": 0.0, "mu": 0.0})
		if got, want := p.(weighted).Weights(), (pipeline.Weights{Alpha: 2}); got != want {
			t.Errorf("%s weights %+v, want %+v", name, got, want)
		}
		for _, bad := range []pipeline.Params{{"alpha": -1.0}, {"lambda": -0.5}, {"mu": -2.0}, {"bogus": 1}, {"lambda": "high"}} {
			if _, err := pipeline.NewPlanner(env, pipeline.StageConfig{Name: name, Params: bad}); err == nil {
				t.Errorf("%s accepted invalid params %v", name, bad)
			}
		}
	}
	if got := newPlanner(t, env, planners.AnnealOnlineName, nil).(tempered).Temperature(); got != planners.DefaultT0 {
		t.Errorf("anneal-online starts at temperature %v, want %v", got, planners.DefaultT0)
	}
	invalid := map[string][]pipeline.Params{
		planners.EntropyGreedyName: {{"t0": 1.0}, {"horizon": 2}},
		planners.AnnealPlanName: {{"t0": 0.0}, {"t0": -1.0}, {"cooling": 0.0}, {"cooling": 1.5}, {"steps": 0}, {"steps": 1.5},
			{"maxLength": 0}, {"horizon": 2}},
		planners.AnnealOnlineName: {{"t0": 0.0}, {"cooling": -0.5}, {"cooling": 1.01}, {"steps": 10}},
		planners.SequenceName:     {{"horizon": 0}, {"horizon": -3}, {"t0": 1.0}},
	}
	for _, name := range allPlanners {
		for _, bad := range invalid[name] {
			if _, err := pipeline.NewPlanner(env, pipeline.StageConfig{Name: name, Params: bad}); err == nil {
				t.Errorf("%s accepted invalid params %v", name, bad)
			}
		}
	}
	for name, ok := range map[string]pipeline.Params{
		planners.AnnealPlanName:   {"t0": 0.2, "cooling": 1.0, "steps": 10, "maxLength": 2},
		planners.AnnealOnlineName: {"t0": 0.2, "cooling": 0.5},
		planners.SequenceName:     {"horizon": 6},
	} {
		newPlanner(t, env, name, ok)
	}
}

// --- hand-built snapshots ----------------------------------------------------------------------------------------

// Every node has 1000 units of CPU and memory, and every pod requests equal amounts of both, so a node's dominant
// share is its CPU load over 1000.

// Every node costs 1/hour.
func node(name string, source bool) pipeline.NodeView {
	return pipeline.NodeView{Name: name, NodePool: "default", Capacity: pipeline.Resources{pipeline.CPU: 1000, pipeline.Memory: 1000, pipeline.Pods: 10},
		Price: 1, Source: source, Destination: true, DisruptionCost: 1}
}

func pod(name, on string, size float64, pdbs ...string) pipeline.PodView {
	return pipeline.PodView{Key: pipeline.PodKey{Namespace: "default", Name: name}, Node: on, Requests: pipeline.Resources{pipeline.CPU: size, pipeline.Memory: size, pipeline.Pods: 1},
		EvictionCost: 1, Owner: "ReplicaSet/default/" + name, Reschedulable: true, PDBs: pdbs}
}

func shift(id, p, from, to string) pipeline.Move {
	return pipeline.Move{ID: id, Kind: pipeline.Shift, Sources: []string{from}, Generator: "test",
		Bindings: []pipeline.Binding{{Pod: pipeline.PodKey{Namespace: "default", Name: p}, From: from, Target: pipeline.ExistingTarget(to)}}}
}

func score(env *pipeline.Env, s pipeline.Snapshot, moves ...pipeline.Move) []pipeline.Scored {
	out := make([]pipeline.Scored, len(moves))
	for i, m := range moves {
		out[i] = env.Scorer.Score(s, m)
	}
	return out
}

func unlimited(s pipeline.Snapshot) pipeline.Budgets {
	b := pipeline.Budgets{Nodes: map[string]int{}}
	for _, n := range s.Nodes() {
		b.Nodes[n.NodePool] = 10
	}
	return b
}

func plan(t testing.TB, p pipeline.Planner, s pipeline.Snapshot, moves []pipeline.Scored, b pipeline.Budgets, seed int64) pipeline.Plan {
	t.Helper()
	out, err := p.Plan(context.Background(), s, moves, b, rand.New(rand.NewSource(seed))) //nolint:gosec
	if err != nil {
		t.Fatalf("%s: %v", p.Name(), err)
	}
	return out
}

func ids(p pipeline.Plan) []string {
	out := make([]string, len(p.Moves))
	for i, m := range p.Moves {
		out[i] = m.ID
	}
	return out
}

// render prints everything a plan decides, rejected reasons in key order, for byte-level determinism checks.
func render(p pipeline.Plan) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s seq=%v constrained=%v\n", p.Planner, p.Sequence, p.ConstrainedByBudgets)
	for _, m := range p.Moves {
		fmt.Fprintf(&b, "%s %s %v %v %v %v %v\n", m.ID, m.Kind, m.Sources, m.Remove, m.Bindings, m.DeltaPotential, m.Energy)
	}
	for _, k := range slices.Sorted(maps.Keys(p.Rejected)) {
		fmt.Fprintf(&b, "rejected %s: %s\n", k, p.Rejected[k])
	}
	return b.String()
}

// applyInOrder applies a plan's moves to s in order and returns the result.
func applyInOrder(t testing.TB, s pipeline.Snapshot, p pipeline.Plan) pipeline.Snapshot {
	t.Helper()
	cur := s
	for _, m := range p.Moves {
		next, err := cur.Apply(m.Move)
		if err != nil {
			t.Fatalf("%s: %s does not apply in order: %v", p.Planner, m.ID, err)
		}
		cur = next
	}
	return cur
}

// pairState is a node x at 20% holding two pods and a fuller destination d at 50%. Moving either pod to d costs more
// disruption than it lowers the potential, but once one has moved, moving the other empties x, which drops the
// potential by far more: each shift alone is uphill and the pair is downhill.
func pairState() (*pipeline.State, []pipeline.Move) {
	s := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), node("d", false)},
		[]pipeline.PodView{pod("p1", "x", 100), pod("p2", "x", 100), pod("base", "d", 500)},
		map[string]pipeline.Totals{"default": {TotalCost: 2, TotalDisruptionCost: 4}}, nil)
	return s, []pipeline.Move{shift("a", "p1", "x", "d"), shift("b", "p2", "x", "d")}
}

// --- P1 entropy-greedy -------------------------------------------------------------------------------------------

//nolint:gocyclo
func TestEntropyGreedyPlansOnlyDownhillIndependentMoves(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	planned := 0
	for seed := int64(1); seed <= 40; seed++ {
		rng := rand.New(rand.NewSource(seed)) //nolint:gosec
		s := pipeline.RandomState(rng, pipeline.SyntheticOptions{Nodes: 10 + int(seed%20), PDBProb: 0.2, DoNotDisruptProb: 0.05})
		moves := score(env, s, conformance.ReferenceMoves(s, rng, 40)...)
		p := plan(t, newPlanner(t, env, planners.EntropyGreedyName, nil), s, moves, unlimited(s), seed)
		if p.Sequence {
			t.Errorf("seed %d: entropy-greedy returned a sequence", seed)
		}
		inPlan := map[string]bool{}
		touched := map[string]string{}
		for _, m := range p.Moves {
			inPlan[m.ID] = true
			if e := defaultWeights.Energy(m); !(e < 0) {
				t.Errorf("seed %d: planned %s with energy %v", seed, m.ID, e)
			}
			for _, n := range m.Touches() {
				if by, ok := touched[n]; ok {
					t.Errorf("seed %d: %s and %s both touch %s", seed, by, m.ID, n)
				}
				touched[n] = m.ID
			}
		}
		for _, m := range moves {
			if !inPlan[m.ID] && p.Rejected[m.ID] == "" {
				t.Errorf("seed %d: %s was neither planned nor given a reason", seed, m.ID)
			}
		}
		planned += len(p.Moves)
	}
	if planned == 0 {
		t.Errorf("entropy-greedy planned nothing on any state")
	}
}

func TestEntropyGreedyRanksByEnergyPerDisruption(t *testing.T) {
	s := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), node("y", true), node("z", true), node("d", false), node("e", false)},
		[]pipeline.PodView{pod("px", "x", 100), pod("py", "y", 100), pod("pz", "z", 100)},
		map[string]pipeline.Totals{"default": {TotalCost: 5, TotalDisruptionCost: 8}}, nil)
	// Hand-scored: a saves more energy in total, b more per unit of disruption. Both land on d, so only one can run.
	moves := []pipeline.Scored{
		{Move: shift("a", "px", "x", "d"), DeltaPotential: -0.5, NormalizedDisruption: 0.4},
		{Move: shift("b", "py", "y", "d"), DeltaPotential: -0.2, NormalizedDisruption: 0.1},
		{Move: shift("c", "pz", "z", "e"), DeltaPotential: 0.1, NormalizedDisruption: 0.1},
	}
	p := plan(t, newPlanner(t, pipeline.SyntheticEnv(nil), planners.EntropyGreedyName, nil), s, moves, unlimited(s), 1)
	if got := ids(p); len(got) != 1 || got[0] != "b" {
		t.Fatalf("planned %v, want [b]", got)
	}
	if !strings.Contains(p.Rejected["a"], "conflicts with b") {
		t.Errorf("a rejected for %q, want a conflict with b", p.Rejected["a"])
	}
	if p.Rejected["c"] != "not downhill" {
		t.Errorf("c rejected for %q, want not downhill", p.Rejected["c"])
	}
	if p.ConstrainedByBudgets {
		t.Errorf("no budget bound, but the plan reports one")
	}
	if math.Abs(moves[0].Energy+0.3) > 1e-12 || math.Abs(moves[1].Energy+0.15) > 1e-12 {
		t.Errorf("input energies %v, %v, want -0.3, -0.15 recorded for the decision log", moves[0].Energy, moves[1].Energy)
	}
}

func TestEntropyGreedyBudgets(t *testing.T) {
	s := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), node("y", true), node("d", false), node("e", false)},
		[]pipeline.PodView{pod("px", "x", 100, "default/pdb"), pod("py", "y", 100, "default/pdb")},
		map[string]pipeline.Totals{"default": {TotalCost: 4, TotalDisruptionCost: 6}}, map[string]int{"default/pdb": 1})
	merge := pipeline.Move{ID: "merge-x", Kind: pipeline.Merge, Sources: []string{"x"}, Remove: []string{"x"},
		Bindings: []pipeline.Binding{{Pod: pipeline.PodKey{Namespace: "default", Name: "px"}, From: "x", Target: pipeline.ExistingTarget("d")}}}
	moves := []pipeline.Scored{
		{Move: merge, DeltaPotential: -0.3, NormalizedDisruption: 0.1, NormalizedSavings: 0.25},
		{Move: shift("shift-y", "py", "y", "e"), DeltaPotential: -0.2, NormalizedDisruption: 0.1},
		{Move: shift("shift-x", "px", "x", "d"), DeltaPotential: -0.1, NormalizedDisruption: 0.1},
	}
	// No node may be removed: the merge is downhill but over budget, so the plan reports the budget as the bound.
	p := plan(t, newPlanner(t, pipeline.SyntheticEnv(nil), planners.EntropyGreedyName, nil), s, moves, pipeline.Budgets{Nodes: map[string]int{"default": 0}}, 1)
	if !strings.Contains(p.Rejected["merge-x"], "node budget") || !p.ConstrainedByBudgets {
		t.Errorf("merge-x rejected for %q, constrained %v; want the node budget", p.Rejected["merge-x"], p.ConstrainedByBudgets)
	}
	// shift-y and shift-x touch different nodes, each fits the PDB alone, and together they would exceed it.
	if got := ids(p); len(got) != 1 || got[0] != "shift-y" {
		t.Fatalf("planned %v, want [shift-y]", got)
	}
	if !strings.Contains(p.Rejected["shift-x"], "pod disruption budget") {
		t.Errorf("shift-x rejected for %q, want the PDB allowance", p.Rejected["shift-x"])
	}
	// A pod-eviction budget of zero blocks every move.
	b := pipeline.Budgets{Nodes: map[string]int{"default": 1}, PodEvictions: map[string]int{"default": 0}}
	if p := plan(t, newPlanner(t, pipeline.SyntheticEnv(nil), planners.EntropyGreedyName, nil), s, moves, b, 1); len(p.Moves) != 0 || !p.ConstrainedByBudgets {
		t.Errorf("planned %v under a zero eviction budget (constrained %v)", ids(p), p.ConstrainedByBudgets)
	}
}

// --- P2 anneal-plan ----------------------------------------------------------------------------------------------

//nolint:gocyclo
func TestAnnealPlanFindsDownhillPairOfUphillMoves(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	moves := score(env, s, pair...)
	for _, m := range moves {
		if e := defaultWeights.Energy(m); !(e > 0) {
			t.Fatalf("precondition: %s alone has energy %v, want uphill", m.ID, e)
		}
	}
	if p := plan(t, newPlanner(t, env, planners.EntropyGreedyName, nil), s, moves, unlimited(s), 1); len(p.Moves) != 0 {
		t.Fatalf("precondition: entropy-greedy planned %v from uphill moves", ids(p))
	}
	for seed := int64(1); seed <= 20; seed++ {
		p := plan(t, newPlanner(t, env, planners.AnnealPlanName, nil), s, moves, unlimited(s), seed)
		if !p.Sequence || p.Planner != planners.AnnealPlanName {
			t.Fatalf("seed %d: plan is not an anneal-plan sequence: %+v", seed, p)
		}
		if len(p.Moves) != 2 {
			t.Fatalf("seed %d: planned %v, want both moves", seed, ids(p))
		}
		total := 0.0
		for _, m := range p.Moves {
			total += m.Energy
		}
		if !(total < 0) {
			t.Errorf("seed %d: sequence %v has total energy %v, want downhill", seed, ids(p), total)
		}
		// The second move is scored where it runs, after the first: there it is downhill.
		if !(p.Moves[1].Energy < 0) || !(p.Moves[0].Energy > 0) {
			t.Errorf("seed %d: energies %v, %v; want the first uphill and the second, re-scored, downhill", seed, p.Moves[0].Energy, p.Moves[1].Energy)
		}
		after := applyInOrder(t, s, p)
		if holds := after.Pods("x"); len(holds) != 0 {
			t.Errorf("seed %d: x still holds %d pods", seed, len(holds))
		}
		if again := plan(t, newPlanner(t, env, planners.AnnealPlanName, nil), s, score(env, s, pair...), unlimited(s), seed); render(again) != render(p) {
			t.Errorf("seed %d: not deterministic:\n%s\nvs\n%s", seed, render(p), render(again))
		}
	}
}

func TestAnnealPlanReturnsNothingWhenEverySequenceIsUphill(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	// At lambda 2 the disruption outweighs even emptying x.
	for seed := int64(1); seed <= 10; seed++ {
		moves := score(env, s, pair...)
		p := plan(t, newPlanner(t, env, planners.AnnealPlanName, pipeline.Params{"lambda": 2.0}), s, moves, unlimited(s), seed)
		if len(p.Moves) != 0 {
			t.Fatalf("seed %d: planned %v from uphill moves", seed, ids(p))
		}
		for _, m := range moves {
			if p.Rejected[m.ID] != "no net-downhill sequence found" {
				t.Errorf("seed %d: %s rejected for %q", seed, m.ID, p.Rejected[m.ID])
			}
		}
	}
}

func TestAnnealPlanRespectsBudgetsAcrossTheSequence(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	moves := score(env, s, pair...)
	// One eviction fits; the pair needs two, so no downhill sequence fits.
	b := pipeline.Budgets{Nodes: map[string]int{"default": 1}, PodEvictions: map[string]int{"default": 1}}
	for seed := int64(1); seed <= 10; seed++ {
		p := plan(t, newPlanner(t, env, planners.AnnealPlanName, nil), s, moves, b.Clone(), seed)
		if len(p.Moves) != 0 {
			t.Fatalf("seed %d: planned %v beyond a one-eviction budget", seed, ids(p))
		}
		if !p.ConstrainedByBudgets {
			t.Errorf("seed %d: the budget blocked the downhill second move, but the plan does not say so", seed)
		}
	}
}

// --- P3 anneal-online --------------------------------------------------------------------------------------------

func TestAnnealOnlineCoolsAcrossCalls(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	p := newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 0.1, "cooling": 0.5})
	want := 0.1
	for call := range 5 {
		if got := p.(tempered).Temperature(); math.Abs(got-want) > 1e-15 {
			t.Fatalf("call %d: temperature %v, want %v", call, got, want)
		}
		plan(t, p, s, score(env, s, pair...), unlimited(s), int64(call))
		want *= 0.5
	}
}

//nolint:gocyclo
func TestAnnealOnlineAcceptsUphillWhenHot(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	moves := score(env, s, pair[0])
	if e := defaultWeights.Energy(moves[0]); !(e > 0) {
		t.Fatalf("precondition: %s has energy %v, want uphill", moves[0].ID, e)
	}
	for seed := int64(1); seed <= 10; seed++ {
		hot := plan(t, newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 1e6}), s, moves, unlimited(s), seed)
		if got := ids(hot); len(got) != 1 || got[0] != "a" || hot.Sequence {
			t.Errorf("seed %d: hot planner planned %v (sequence %v), want the uphill move alone", seed, got, hot.Sequence)
		}
		cold := plan(t, newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 1e-9}), s, moves, unlimited(s), seed)
		if len(cold.Moves) != 0 || cold.Rejected["a"] != "rejected by the Metropolis rule" {
			t.Errorf("seed %d: cold planner planned %v, rejected %q; want the uphill move refused", seed, ids(cold), cold.Rejected["a"])
		}
	}
	// The same planner refuses the move once it has cooled.
	p := newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 1e6, "cooling": 1e-9})
	if first := plan(t, p, s, moves, unlimited(s), 1); len(first.Moves) != 1 {
		t.Fatalf("first call planned %v, want the uphill move", ids(first))
	}
	for range 3 {
		plan(t, p, s, moves, unlimited(s), 2)
	}
	if late := plan(t, p, s, moves, unlimited(s), 3); len(late.Moves) != 0 {
		t.Errorf("cooled planner still planned %v", ids(late))
	}
}

func TestAnnealOnlineTakesOneMoveWithinBudgets(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, pair := pairState()
	moves := score(env, s, pair...)
	p := plan(t, newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 1e6}), s, moves, unlimited(s), 1)
	if len(p.Moves) != 1 {
		t.Fatalf("planned %v, want exactly one move", ids(p))
	}
	b := pipeline.Budgets{Nodes: map[string]int{"default": 1}, PodEvictions: map[string]int{"default": 0}}
	p = plan(t, newPlanner(t, env, planners.AnnealOnlineName, pipeline.Params{"t0": 1e6}), s, moves, b, 1)
	if len(p.Moves) != 0 || !p.ConstrainedByBudgets {
		t.Errorf("planned %v under a zero eviction budget (constrained %v)", ids(p), p.ConstrainedByBudgets)
	}
}

// --- P4 sequence -------------------------------------------------------------------------------------------------

// chainState is x at 20% with two pods, and destinations d1 at 50% and d2 at 60%. Every single shift off x is
// uphill; shifting both to d2 empties x, and deleting x then saves its price.
func chainState() (*pipeline.State, []pipeline.Move) {
	s := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), node("d1", false), node("d2", false)},
		[]pipeline.PodView{pod("p1", "x", 100), pod("p2", "x", 100), pod("base1", "d1", 500), pod("base2", "d2", 600)},
		map[string]pipeline.Totals{"default": {TotalCost: 3, TotalDisruptionCost: 5}}, nil)
	return s, []pipeline.Move{shift("s1", "p1", "x", "d1"), shift("s2", "p2", "x", "d2"), shift("s3", "p1", "x", "d2"), shift("s4", "p2", "x", "d1")}
}

//nolint:gocyclo
func TestSequenceChainsShiftsIntoADelete(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, shifts := chainState()
	// A direct merge of x into d1 is downhill too, but less so than the chain.
	merge := pipeline.Move{ID: "m1", Kind: pipeline.Merge, Sources: []string{"x"}, Remove: []string{"x"}, Generator: "test", Bindings: []pipeline.Binding{
		{Pod: pipeline.PodKey{Namespace: "default", Name: "p1"}, From: "x", Target: pipeline.ExistingTarget("d1")},
		{Pod: pipeline.PodKey{Namespace: "default", Name: "p2"}, From: "x", Target: pipeline.ExistingTarget("d1")}}}
	moves := score(env, s, append(shifts, merge)...)
	for _, m := range moves[:len(shifts)] {
		if e := defaultWeights.Energy(m); !(e > 0) {
			t.Fatalf("precondition: %s alone has energy %v, want uphill", m.ID, e)
		}
	}
	mergeEnergy := defaultWeights.Energy(moves[len(shifts)])
	if !(mergeEnergy < 0) {
		t.Fatalf("precondition: m1 has energy %v, want downhill", mergeEnergy)
	}
	p := plan(t, newPlanner(t, env, planners.SequenceName, nil), s, moves, unlimited(s), 1)
	// s2 and s3 tie on the snapshot; s2 wins on ID, and after it s3 (to the fuller d2) beats s1.
	if got, want := ids(p), []string{"s2", "s3", "sequence-delete-x-1"}; !p.Sequence || !slices.Equal(got, want) {
		t.Fatalf("planned %v (sequence %v), want the sequence %v", got, p.Sequence, want)
	}
	del := p.Moves[2]
	if del.Kind != pipeline.Merge || del.Generator != planners.SequenceName || len(del.Bindings) != 0 || !slices.Equal(del.Remove, []string{"x"}) || !slices.Equal(del.Sources, []string{"x"}) {
		t.Errorf("delete is %s generator=%s, want a bare merge removing x", del, del.Generator)
	}
	if del.SavingsPerHour != 1 || !(del.Energy < 0) {
		t.Errorf("delete saves %v/hr at energy %v, want x's price and downhill", del.SavingsPerHour, del.Energy)
	}
	if total := p.Moves[0].Energy + p.Moves[1].Energy + del.Energy; !(total < mergeEnergy) {
		t.Errorf("chain energy %v does not beat the merge's %v", total, mergeEnergy)
	}
	if !strings.Contains(p.Rejected["m1"], "conflicts") {
		t.Errorf("m1 rejected for %q, want a conflict with the chain", p.Rejected["m1"])
	}
	after := applyInOrder(t, s, p)
	if _, ok := after.Node("x"); ok {
		t.Errorf("x survives the sequence")
	}
	if got := len(after.Pods("d2")); got != 3 {
		t.Errorf("d2 holds %d pods after the sequence, want 3", got)
	}
}

func TestSequenceDeleteIDsAreUniqueAcrossCalls(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, shifts := chainState()
	p := newPlanner(t, env, planners.SequenceName, nil)
	seen := map[string]bool{}
	for call := range 3 {
		got := plan(t, p, s, score(env, s, shifts...), unlimited(s), 1)
		if len(got.Moves) != 3 {
			t.Fatalf("call %d planned %v, want the chain", call, ids(got))
		}
		id := got.Moves[2].ID
		if seen[id] {
			t.Errorf("call %d built a delete with the repeated ID %s", call, id)
		}
		seen[id] = true
	}
}

func TestSequenceNeedsTheChainToEmptyItsSource(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	s, shifts := chainState()
	moves := score(env, s, shifts...)
	p := plan(t, newPlanner(t, env, planners.SequenceName, pipeline.Params{"horizon": 1}), s, moves, unlimited(s), 1)
	if len(p.Moves) != 0 {
		t.Fatalf("planned %v with a horizon too short to empty x", ids(p))
	}
	if !strings.Contains(p.Rejected["s2"], "does not empty it within 1 shifts") {
		t.Errorf("s2 rejected for %q, want the horizon", p.Rejected["s2"])
	}
	for _, id := range []string{"s1", "s3", "s4"} {
		if p.Rejected[id] == "" {
			t.Errorf("%s has no rejection reason", id)
		}
	}
	// The chain ends in a removal, which a node budget of zero forbids.
	b := pipeline.Budgets{Nodes: map[string]int{"default": 0}}
	p = plan(t, newPlanner(t, env, planners.SequenceName, nil), s, moves, b, 1)
	if len(p.Moves) != 0 || !p.ConstrainedByBudgets || !strings.Contains(p.Rejected["s2"], "node budget") {
		t.Errorf("planned %v (constrained %v, s2 %q) with no node budget", ids(p), p.ConstrainedByBudgets, p.Rejected["s2"])
	}
	// A do-not-disrupt pod pins x, so no chain empties it.
	pinned := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), node("d1", false), node("d2", false)},
		[]pipeline.PodView{pod("p1", "x", 100), pod("p2", "x", 100), func() pipeline.PodView { p := pod("p3", "x", 50); p.DoNotDisrupt = true; return p }(),
			pod("base1", "d1", 500), pod("base2", "d2", 600)},
		map[string]pipeline.Totals{"default": {TotalCost: 3, TotalDisruptionCost: 5}}, nil)
	if p := plan(t, newPlanner(t, env, planners.SequenceName, nil), pinned, score(env, pinned, shifts...), unlimited(pinned), 1); len(p.Moves) != 0 {
		t.Errorf("planned %v off a node with a do-not-disrupt pod", ids(p))
	}
}

// --- all planners ------------------------------------------------------------------------------------------------

func TestDeterministic(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	for _, name := range allPlanners {
		for seed := int64(1); seed <= 15; seed++ {
			run := func() string {
				rng := rand.New(rand.NewSource(seed)) //nolint:gosec
				s := pipeline.RandomState(rng, pipeline.SyntheticOptions{Nodes: 12 + int(seed%10), PDBProb: 0.2})
				moves := score(env, s, conformance.ReferenceMoves(s, rng, 40)...)
				b := pipeline.Budgets{Nodes: map[string]int{"pool-0": 2}, PodEvictions: map[string]int{"pool-0": 8}}
				return render(plan(t, newPlanner(t, env, name, nil), s, moves, b, seed))
			}
			if a, b := run(), run(); a != b {
				t.Errorf("%s seed %d: plans differ:\n%s\nvs\n%s", name, seed, a, b)
			}
		}
	}
}

// TestEveryPlannerRefusesDoNotDisrupt feeds moves a misbehaving generator might emit, each scored as a large win.
func TestEveryPlannerRefusesDoNotDisrupt(t *testing.T) {
	pinned := node("y", true)
	pinned.DoNotDisrupt = true
	empty := node("z", true)
	empty.DoNotDisrupt = true
	dnd := pod("pd", "x", 100)
	dnd.DoNotDisrupt = true
	s := pipeline.NewState(
		[]pipeline.NodeView{node("x", true), pinned, empty, node("d", false)},
		[]pipeline.PodView{dnd, pod("py", "y", 100), pod("base", "d", 500)},
		map[string]pipeline.Totals{"default": {TotalCost: 4, TotalDisruptionCost: 6}}, nil)
	moves := []pipeline.Move{
		shift("dnd-pod", "pd", "x", "d"),
		shift("dnd-node", "py", "y", "d"),
		{ID: "dnd-delete", Kind: pipeline.Merge, Sources: []string{"z"}, Remove: []string{"z"}},
	}
	params := map[string]pipeline.Params{planners.AnnealOnlineName: {"t0": 1e6}}
	for _, name := range allPlanners {
		scored := make([]pipeline.Scored, len(moves))
		for i, m := range moves {
			scored[i] = pipeline.Scored{Move: m, DeltaPotential: -1, NormalizedSavings: 0.5}
		}
		p := plan(t, newPlanner(t, pipeline.SyntheticEnv(nil), name, params[name]), s, scored, unlimited(s), 1)
		if len(p.Moves) != 0 {
			t.Errorf("%s planned %v", name, ids(p))
		}
		for _, m := range moves {
			if !strings.Contains(p.Rejected[m.ID], "do-not-disrupt") {
				t.Errorf("%s rejected %s for %q, want do-not-disrupt", name, m.ID, p.Rejected[m.ID])
			}
		}
	}
}

func TestEveryPlannerRecordsWhyMovesWereDeclined(t *testing.T) {
	env := pipeline.SyntheticEnv(nil)
	for _, name := range allPlanners {
		for seed := int64(1); seed <= 10; seed++ {
			rng := rand.New(rand.NewSource(seed)) //nolint:gosec
			s := pipeline.RandomState(rng, pipeline.SyntheticOptions{Nodes: 15})
			moves := score(env, s, conformance.ReferenceMoves(s, rng, 30)...)
			p := plan(t, newPlanner(t, env, name, nil), s, moves, unlimited(s), seed)
			if p.Planner != name {
				t.Errorf("%s: plan names planner %q", name, p.Planner)
			}
			inPlan := map[string]bool{}
			for _, m := range p.Moves {
				inPlan[m.ID] = true
			}
			for _, m := range moves {
				if !inPlan[m.ID] && p.Rejected[m.ID] == "" {
					t.Errorf("%s seed %d: %s was neither planned nor given a reason", name, seed, m.ID)
				}
				if inPlan[m.ID] && p.Rejected[m.ID] != "" {
					t.Errorf("%s seed %d: %s was planned and rejected (%s)", name, seed, m.ID, p.Rejected[m.ID])
				}
			}
		}
	}
}
