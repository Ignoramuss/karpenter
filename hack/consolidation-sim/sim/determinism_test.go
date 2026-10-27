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
	"bytes"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// TestDeterminism is Gate 2: the same seed and config must produce byte-identical decision logs and timeseries.
// Each case runs twice in one process, which is stricter than two processes: state leaking between runs through
// package-level variables would show up here too.
//
//	go test ./hack/consolidation-sim/sim -run TestDeterminism -v
//
// DETERMINISM_CASES narrows the cases by name substring.
func TestDeterminism(t *testing.T) {
	fine := pipeline.DefaultConfig()
	fine.Moves = pipeline.StageList{{Name: "upstream-multi", Params: pipeline.Params{"mode": "fine"}}, {Name: "upstream-single", Params: pipeline.Params{"mode": "fine"}}}
	cases := []Config{
		{Scenario: ScenarioConfig{Name: W1Fragmented, Nodes: 24}, Arm: ArmConfig{Name: ArmUnderutil}, Duration: D(45 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W1Fragmented, Nodes: 24}, Arm: ArmConfig{Name: ArmBalanced}, Duration: D(45 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W2Diurnal, Nodes: 16}, Arm: ArmConfig{Name: ArmUnderutil}, Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W3BatchChurn, Nodes: 16}, Arm: ArmConfig{Name: ArmUnderutil}, Duration: D(90 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W4MixedShapes, Nodes: 20}, Arm: ArmConfig{Name: ArmBalanced}, Duration: D(45 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W5Constrained, Nodes: 16}, Arm: ArmConfig{Name: ArmUnderutil}, Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W2Diurnal, Nodes: 16}, Arm: ArmConfig{Name: ArmDescheduler}, Placement: MostAllocated, Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W2Diurnal, Nodes: 16}, Arm: ArmConfig{Name: ArmUnderutil}, ScaleDown: ScaleDownDeletionCost, Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W1Fragmented, Nodes: 24}, Arm: ArmConfig{Name: ArmPipeline, Pipeline: &fine}, Duration: D(45 * time.Minute)},
		// Strategy arms: random generation with shifts, each planner family and executor.
		{Scenario: ScenarioConfig{Name: W2Diurnal, Nodes: 16}, Arm: strategyArm("random-greedy", "uniform-random",
			pipeline.StageList{{Name: "random-merge", Params: pipeline.Params{"confirm": true}}, {Name: "random-shift"}}, "entropy-greedy", nil, "shift-evict"), Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W4MixedShapes, Nodes: 20}, Arm: strategyArm("random-anneal-plan", "uniform-random",
			pipeline.StageList{{Name: "random-merge"}, {Name: "random-shift"}, {Name: "swap"}}, "anneal-plan", pipeline.Params{"temperatureUnit": "per-node", "t0": 0.3}, "shift-evict"), Duration: D(45 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W2Diurnal, Nodes: 16}, Arm: strategyArm("band-sequence-steered", "util-band",
			pipeline.StageList{{Name: "greedy-shift"}, {Name: "random-merge"}}, "sequence", nil, "steered"), Duration: D(150 * time.Minute)},
		{Scenario: ScenarioConfig{Name: W5Constrained, Nodes: 16}, Arm: strategyArm("weighted-online-mbb", "potential-weighted",
			pipeline.StageList{{Name: "random-merge"}, {Name: "random-shift"}}, "anneal-online", nil, "make-before-break"), Duration: D(150 * time.Minute)},
	}
	filter := os.Getenv("DETERMINISM_CASES")
	for i, c := range cases {
		c.Seed = int64(11 + i)
		c.RunID = "determinism"
		name := fmt.Sprintf("%s-%d-%s-%s", c.Scenario.Name, c.Scenario.Nodes, pick(c.Arm.Label != "", c.Arm.Label, c.Arm.Name), c.ScaleDown)
		if filter != "" && !strings.Contains(name, filter) {
			continue
		}
		t.Run(name, func(t *testing.T) {
			d1, ts1, s1 := runOnce(t, c)
			d2, ts2, s2 := runOnce(t, c)
			if s1.Error != "" {
				t.Fatalf("run failed: %s", s1.Error)
			}
			if !bytes.Equal(d1, d2) {
				t.Errorf("decision logs differ (%d vs %d bytes) at byte %d", len(d1), len(d2), firstDiff(d1, d2))
			}
			if !bytes.Equal(ts1, ts2) {
				t.Errorf("timeseries differ at byte %d", firstDiff(ts1, ts2))
			}
			if s1.IntegratedCost != s2.IntegratedCost || s1.Evictions != s2.Evictions {
				t.Errorf("summaries differ: cost %v vs %v, evictions %d vs %d", s1.IntegratedCost, s2.IntegratedCost, s1.Evictions, s2.Evictions)
			}
			t.Logf("DETERMINISM %s: decisions=%d bytes, evictions=%d, steady=$%.2f/hr, wall=%.1fs", name, len(d1), s1.Evictions, s1.SteadyCostPerHour, s1.WallSeconds)
		})
	}
}

func runOnce(t *testing.T, c Config) ([]byte, []byte, Summary) {
	t.Helper()
	var dec, ts bytes.Buffer
	s, err := New(c, &dec)
	if err != nil {
		t.Fatal(err)
	}
	sum, err := s.Run(&ts)
	if err != nil {
		t.Fatal(err)
	}
	return dec.Bytes(), ts.Bytes(), sum
}

func firstDiff(a, b []byte) int {
	for i := range min(len(a), len(b)) {
		if a[i] != b[i] {
			return i
		}
	}
	return min(len(a), len(b))
}

// strategyArm is a pooled pipeline arm over the util-entropy potential.
func strategyArm(label, candidates string, moves pipeline.StageList, planner string, plannerParams pipeline.Params, executor string) ArmConfig {
	return ArmConfig{Name: ArmPipeline, Label: label, Pipeline: &pipeline.Config{
		Candidates: pipeline.StageConfig{Name: candidates}, Moves: moves, MovesMode: pipeline.Pooled,
		Planner: pipeline.StageConfig{Name: planner, Params: plannerParams}, Executor: pipeline.StageConfig{Name: executor},
		Potential: pipeline.StageConfig{Name: "util-entropy"}, Search: pipeline.SearchConfig{MaxFullSimulations: 16, MaxAttempts: 300},
	}}
}
