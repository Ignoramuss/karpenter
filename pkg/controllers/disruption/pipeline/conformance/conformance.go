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

// Package conformance is the shared suite every pipeline strategy passes. It runs strategies against seeded
// synthetic snapshots and checks the contract each stage promises:
//
//   - selectors return only eligible nodes, without duplicates, deterministically for a seed;
//   - generators emit moves that pass Fast feasibility and apply cleanly, never touch do-not-disrupt pods or exceed
//     PDB allowances, stay within the search budget, and are deterministic for a seed;
//   - planners return a subset of their input that never exceeds node or pod-eviction budgets, whose moves do not
//     conflict (or, for sequences, apply in order), deterministically for a seed; a sequence may also end chains with
//     a delete of a node its earlier shifts emptied, a move the planner builds itself;
//   - potentials satisfy Delta(s, m) == Value(s.Apply(m)) - Value(s).
//
// Executors run against a live cluster and are checked in the simulator, whose eviction path counts PDB and
// do-not-disrupt violations (a run with any is invalid).
package conformance

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"reflect"
	"sort"
	"testing"
	"time"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// Seeds is how many synthetic states each check runs.
const Seeds = 40

func state(seed int64) *pipeline.State {
	rng := rand.New(rand.NewSource(seed)) //nolint:gosec
	return pipeline.RandomState(rng, pipeline.SyntheticOptions{Nodes: 8 + int(seed%25), DoNotDisruptProb: 0.08, PDBProb: 0.2, NodePools: 1 + int(seed%2), NonSourceProb: 0.1})
}

func rng(seed int64) *rand.Rand { return rand.New(rand.NewSource(seed * 7919)) } //nolint:gosec

// CheckSelector runs the selector contract.
func CheckSelector(t testing.TB, newSelector func(env *pipeline.Env) pipeline.CandidateSelector) {
	t.Helper()
	ctx := context.Background()
	for seed := int64(1); seed <= Seeds; seed++ {
		s := state(seed)
		sel := newSelector(pipeline.SyntheticEnv(nil))
		src, dst, err := sel.Select(ctx, s, rng(seed))
		if err != nil {
			t.Fatalf("seed %d: %s: %v", seed, sel.Name(), err)
		}
		seen := map[string]bool{}
		for _, n := range src {
			if !n.Source {
				t.Errorf("seed %d: %s returned non-source %s as a source", seed, sel.Name(), n.Name)
			}
			if seen["s/"+n.Name] {
				t.Errorf("seed %d: %s returned source %s twice", seed, sel.Name(), n.Name)
			}
			seen["s/"+n.Name] = true
		}
		for _, n := range dst {
			if !n.Destination {
				t.Errorf("seed %d: %s returned non-destination %s as a destination", seed, sel.Name(), n.Name)
			}
			if seen["d/"+n.Name] {
				t.Errorf("seed %d: %s returned destination %s twice", seed, sel.Name(), n.Name)
			}
			seen["d/"+n.Name] = true
		}
		src2, dst2, _ := newSelector(pipeline.SyntheticEnv(nil)).Select(ctx, s, rng(seed))
		if !reflect.DeepEqual(names(src), names(src2)) || !reflect.DeepEqual(names(dst), names(dst2)) {
			t.Errorf("seed %d: %s is not deterministic for a seed", seed, sel.Name())
		}
	}
}

func names(ns []pipeline.NodeView) []string {
	out := make([]string, len(ns))
	for i, n := range ns {
		out[i] = n.Name
	}
	return out
}

// GeneratorOptions tune CheckGenerator.
type GeneratorOptions struct {
	// MaxAttempts and MaxFull are the search budget the generator runs under.
	MaxAttempts int
	MaxFull     int
	// AllowEmpty skips the check that the generator finds at least one move across all seeds.
	AllowEmpty bool
}

// CheckGenerator runs the generator contract.
//
//nolint:gocyclo
func CheckGenerator(t testing.TB, newGenerator func(env *pipeline.Env) pipeline.MoveGenerator, o GeneratorOptions) {
	t.Helper()
	if o.MaxAttempts == 0 {
		o.MaxAttempts = 200
	}
	if o.MaxFull == 0 {
		o.MaxFull = 50
	}
	ctx := context.Background()
	total := 0
	for seed := int64(1); seed <= Seeds; seed++ {
		s := state(seed)
		env := pipeline.SyntheticEnv(nil)
		gen := newGenerator(env)
		src, dst := eligible(s)
		run := func() ([]pipeline.Move, *pipeline.SearchBudget) {
			b := pipeline.NewSearchBudget(o.MaxAttempts, o.MaxFull, time.Time{}, nil)
			b.Feasibility, b.Scorer = env.Feasibility, env.Scorer
			moves, err := newGenerator(env).Generate(ctx, s, src, dst, rng(seed), b)
			if err != nil {
				t.Fatalf("seed %d: %s: %v", seed, gen.Name(), err)
			}
			return moves, b
		}
		moves, b := run()
		total += len(moves)
		attempts, full, _, _ := b.Stats()
		if attempts > o.MaxAttempts || full > o.MaxFull {
			t.Errorf("seed %d: %s exceeded its search budget: %d attempts, %d full simulations", seed, gen.Name(), attempts, full)
		}
		ids := map[string]bool{}
		for _, m := range moves {
			if m.ID == "" || ids[m.ID] {
				t.Errorf("seed %d: %s emitted a move with an empty or repeated ID %q", seed, gen.Name(), m.ID)
			}
			ids[m.ID] = true
			if err := env.Feasibility.Fast(s, m); err != nil {
				t.Errorf("seed %d: %s emitted %s, which fails Fast feasibility: %v", seed, gen.Name(), m, err)
				continue
			}
			if _, err := s.Apply(m); err != nil {
				t.Errorf("seed %d: %s emitted %s, which does not apply: %v", seed, gen.Name(), m, err)
			}
			switch m.Kind {
			case pipeline.Shift:
				if len(m.Remove) > 0 || len(m.Launch) > 0 {
					t.Errorf("seed %d: %s emitted a shift that removes or launches nodes: %s", seed, gen.Name(), m)
				}
			case pipeline.Merge, pipeline.RightSize:
				if !sameSet(m.Remove, m.Sources) {
					t.Errorf("seed %d: %s emitted a merge whose removals %v differ from its sources %v", seed, gen.Name(), m.Remove, m.Sources)
				}
			default:
				t.Errorf("seed %d: %s emitted unknown kind %q", seed, gen.Name(), m.Kind)
			}
		}
		again, _ := run()
		if !reflect.DeepEqual(moveKeys(moves), moveKeys(again)) {
			t.Errorf("seed %d: %s is not deterministic for a seed", seed, gen.Name())
		}
	}
	if total == 0 && !o.AllowEmpty {
		t.Errorf("generator found no moves on any of %d synthetic states", Seeds)
	}
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

func moveKeys(ms []pipeline.Move) []string {
	out := make([]string, len(ms))
	for i, m := range ms {
		out[i] = fmt.Sprintf("%s|%v|%v|%v", m.Kind, m.Sources, m.Remove, m.Bindings)
	}
	return out
}

func sameSet(a, b []string) bool {
	x, y := append([]string(nil), a...), append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	return reflect.DeepEqual(x, y)
}

// ReferenceMoves builds a mixed pool of feasible moves on s for planner checks: single-pod shifts to fuller nodes
// and delete-only merges of one or two sources whose pods fit elsewhere, first-fit.
func ReferenceMoves(s pipeline.Snapshot, r *rand.Rand, n int) []pipeline.Move {
	fast := &pipeline.FastChecker{}
	src, dst := eligible(s)
	var out []pipeline.Move
	for attempt := 0; attempt < 20*n && len(out) < n && len(src) > 0 && len(dst) > 1; attempt++ {
		from := src[r.Intn(len(src))]
		pods := s.Pods(from.Name)
		if len(pods) == 0 {
			continue
		}
		var m pipeline.Move
		if r.Intn(2) == 0 {
			p := pods[r.Intn(len(pods))]
			to := dst[r.Intn(len(dst))]
			if to.Name == from.Name {
				continue
			}
			m = pipeline.Move{Kind: pipeline.Shift, Sources: []string{from.Name}, Bindings: []pipeline.Binding{{Pod: p.Key, From: from.Name, Target: pipeline.ExistingTarget(to.Name)}}}
		} else {
			m = pipeline.Move{Kind: pipeline.Merge, Sources: []string{from.Name}, Remove: []string{from.Name}}
			added := map[string]pipeline.Resources{}
			ok := true
			for _, p := range pods {
				if !p.Reschedulable {
					continue
				}
				placed := false
				for _, to := range dst {
					if to.Name == from.Name {
						continue
					}
					if s.Load(to.Name).Add(added[to.Name]).Add(p.Requests).Fits(to.Capacity) {
						added[to.Name] = added[to.Name].Add(p.Requests)
						m.Bindings = append(m.Bindings, pipeline.Binding{Pod: p.Key, From: from.Name, Target: pipeline.ExistingTarget(to.Name)})
						placed = true
						break
					}
				}
				if !placed {
					ok = false
					break
				}
			}
			if !ok {
				continue
			}
		}
		m.SortBindings()
		m.ID = fmt.Sprintf("ref-%d", len(out))
		m.Generator = "reference"
		if fast.Fast(s, m) == nil {
			out = append(out, m)
		}
	}
	return out
}

// CheckPlanner runs the planner contract over reference move pools.
//
//nolint:gocyclo
func CheckPlanner(t testing.TB, newPlanner func(env *pipeline.Env) pipeline.Planner) {
	t.Helper()
	ctx := context.Background()
	for seed := int64(1); seed <= Seeds; seed++ {
		s := state(seed)
		env := pipeline.SyntheticEnv(nil)
		moves := ReferenceMoves(s, rng(seed), 30)
		scored := make([]pipeline.Scored, len(moves))
		for i, m := range moves {
			scored[i] = env.Scorer.Score(s, m)
		}
		budgets := pipeline.Budgets{Nodes: map[string]int{}, PodEvictions: map[string]int{}}
		for _, n := range s.Nodes() {
			budgets.Nodes[n.NodePool] = 1 + int(seed%3)
			budgets.PodEvictions[n.NodePool] = 5 + int(seed%7)
		}
		plan, err := newPlanner(env).Plan(ctx, s, scored, budgets.Clone(), rng(seed))
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		in := map[string]bool{}
		for _, m := range scored {
			in[m.ID] = true
		}
		left := budgets.Clone()
		touched := map[string]bool{}
		cur := pipeline.Snapshot(s)
		for _, m := range plan.Moves {
			synthesizedDelete := plan.Sequence && m.Kind == pipeline.Merge && len(m.Bindings) == 0 && len(m.Launch) == 0
			if !in[m.ID] && !synthesizedDelete {
				t.Errorf("seed %d: %s planned move %s that was not in its input", seed, plan.Planner, m.ID)
			}
			pipeline.SpendBudgets(s, m.Move, left)
			if !plan.Sequence {
				for _, n := range m.Touches() {
					if touched[n] {
						t.Errorf("seed %d: %s planned conflicting moves on node %s", seed, plan.Planner, n)
					}
					touched[n] = true
				}
			} else {
				next, err := cur.Apply(m.Move)
				if err != nil {
					t.Errorf("seed %d: %s planned a sequence that does not apply in order at %s: %v", seed, plan.Planner, m.ID, err)
					break
				}
				cur = next
			}
		}
		for pool, v := range left.Nodes {
			if v < 0 {
				t.Errorf("seed %d: %s exceeded the node budget of %s by %d", seed, plan.Planner, pool, -v)
			}
		}
		for pool, v := range left.PodEvictions {
			if v < 0 {
				t.Errorf("seed %d: %s exceeded the pod-eviction budget of %s by %d", seed, plan.Planner, pool, -v)
			}
		}
		again, _ := newPlanner(env).Plan(ctx, s, scored, budgets.Clone(), rng(seed))
		if !reflect.DeepEqual(planIDs(plan), planIDs(again)) {
			t.Errorf("seed %d: %s is not deterministic for a seed", seed, plan.Planner)
		}
	}
}

func planIDs(p pipeline.Plan) []string {
	out := make([]string, len(p.Moves))
	for i, m := range p.Moves {
		out[i] = m.ID
	}
	return out
}

// CheckPotential checks Delta against Value differences on reference moves.
func CheckPotential(t testing.TB, p pipeline.Potential) {
	t.Helper()
	for seed := int64(1); seed <= Seeds; seed++ {
		s := state(seed)
		for _, m := range ReferenceMoves(s, rng(seed), 20) {
			after, err := s.Apply(m)
			if err != nil {
				t.Fatalf("seed %d: reference move does not apply: %v", seed, err)
			}
			want := p.Value(after) - p.Value(s)
			if got := p.Delta(s, m); math.Abs(got-want) > 1e-9*math.Max(1, math.Abs(want)) {
				t.Errorf("seed %d: %s Delta(%s) = %.12f, Value difference = %.12f", seed, p.Name(), m, got, want)
			}
		}
	}
}
