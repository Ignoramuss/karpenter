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

package pipeline_test

import (
	"math/rand"
	"strings"
	"testing"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/conformance"
	_ "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/all"
)

// Property tests for the shared services, plus the registry-wide conformance run: every registered strategy that
// can be built without a live cluster passes the suite for its stage.

func TestPotentialDeltaMatchesValue(t *testing.T) {
	for _, name := range pipeline.Registered()["potential"] {
		for _, params := range []pipeline.Params{nil, {"resource": "max", "stranding": 0.5}, {"resource": "mean", "weight": "uniform"}, {"resource": "cpu"}} {
			if name == "none" && params != nil {
				continue
			}
			p, err := pipeline.NewPotential(pipeline.StageConfig{Name: name, Params: params})
			if err != nil {
				t.Fatalf("%s %v: %v", name, params, err)
			}
			conformance.CheckPotential(t, p)
		}
	}
}

// TestSnapshotApply checks Apply conserves reschedulable pods, never exceeds capacity, leaves its parent unchanged,
// and rejects moves that would.
//
//nolint:gocyclo
func TestSnapshotApply(t *testing.T) {
	for seed := int64(1); seed <= 60; seed++ {
		rng := rand.New(rand.NewSource(seed)) //nolint:gosec
		s := pipeline.RandomState(rng, pipeline.SyntheticOptions{Nodes: 5 + int(seed%30)})
		before := podCount(s)
		parentLoads := loads(s)
		for _, m := range conformance.ReferenceMoves(s, rng, 25) {
			next, err := s.Apply(m)
			if err != nil {
				t.Fatalf("seed %d: reference move %s does not apply: %v", seed, m, err)
			}
			if got := podCount(next); got != before {
				t.Errorf("seed %d: %s changed the reschedulable pod count from %d to %d", seed, m, before, got)
			}
			for _, n := range next.Nodes() {
				if !next.Load(n.Name).Fits(n.Capacity) {
					t.Errorf("seed %d: %s left %s over capacity", seed, m, n.Name)
				}
			}
			for _, r := range m.Remove {
				if _, ok := next.Node(r); ok {
					t.Errorf("seed %d: %s did not remove %s", seed, m, r)
				}
			}
		}
		if after := loads(s); !equalLoads(parentLoads, after) {
			t.Errorf("seed %d: Apply mutated its parent snapshot", seed)
		}
		// Overfilling a node must fail.
		nodes := s.Nodes()
		if len(nodes) >= 2 {
			var bindings []pipeline.Binding
			for _, n := range nodes[1:] {
				for _, p := range s.Pods(n.Name) {
					bindings = append(bindings, pipeline.Binding{Pod: p.Key, From: n.Name, Target: pipeline.ExistingTarget(nodes[0].Name)})
				}
			}
			m := pipeline.Move{ID: "overfill", Kind: pipeline.Shift, Bindings: bindings}
			m.SortBindings()
			total := s.Load(nodes[0].Name)
			for _, b := range bindings {
				for _, p := range s.Pods(b.From) {
					if p.Key == b.Pod {
						total = total.Add(p.Requests)
					}
				}
			}
			if _, err := s.Apply(m); err == nil && !total.Fits(nodes[0].Capacity) {
				t.Errorf("seed %d: Apply accepted a move that overfills %s", seed, nodes[0].Name)
			}
		}
	}
}

func podCount(s pipeline.Snapshot) int {
	n := 0
	for _, node := range s.Nodes() {
		for _, p := range s.Pods(node.Name) {
			if p.Reschedulable {
				n++
			}
		}
	}
	return n
}

func loads(s pipeline.Snapshot) map[string]pipeline.Resources {
	out := map[string]pipeline.Resources{}
	for _, n := range s.Nodes() {
		out[n.Name] = s.Load(n.Name)
	}
	return out
}

func equalLoads(a, b map[string]pipeline.Resources) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// TestRegisteredStrategiesConform runs the conformance suite over every registered strategy that builds without a
// live cluster.
func TestRegisteredStrategiesConform(t *testing.T) {
	reg := pipeline.Registered()
	for _, name := range reg[pipeline.StageCandidates] {
		if _, err := pipeline.NewCandidateSelector(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: name}); err != nil {
			continue
		}
		t.Run("candidates/"+name, func(t *testing.T) {
			conformance.CheckSelector(t, func(env *pipeline.Env) pipeline.CandidateSelector {
				s, _ := pipeline.NewCandidateSelector(env, pipeline.StageConfig{Name: name})
				return s
			})
		})
	}
	for _, name := range reg[pipeline.StageMoves] {
		if _, err := pipeline.NewMoveGenerator(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: name}); err != nil {
			if !strings.Contains(err.Error(), "live cluster") {
				t.Errorf("move generator %s: %v", name, err)
			}
			continue
		}
		t.Run("moves/"+name, func(t *testing.T) {
			conformance.CheckGenerator(t, func(env *pipeline.Env) pipeline.MoveGenerator {
				g, _ := pipeline.NewMoveGenerator(env, pipeline.StageConfig{Name: name})
				return g
			}, conformance.GeneratorOptions{})
		})
	}
	for _, name := range reg[pipeline.StagePlan] {
		if _, err := pipeline.NewPlanner(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: name}); err != nil {
			continue
		}
		t.Run("plan/"+name, func(t *testing.T) {
			conformance.CheckPlanner(t, func(env *pipeline.Env) pipeline.Planner {
				p, _ := pipeline.NewPlanner(env, pipeline.StageConfig{Name: name})
				return p
			})
		})
	}
}
