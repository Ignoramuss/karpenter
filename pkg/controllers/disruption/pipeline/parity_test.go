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
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"testing"

	"github.com/go-logr/logr"
	"github.com/samber/lo"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/cloudprovider/fake"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/test"
)

// The differential parity test (Gate 1). For each seeded cluster state it runs upstream multi-node then
// single-node consolidation the way the disruption controller does, then the pipeline with upstream adapters, and
// requires identical decisions: candidates (and their order), decision, replacement instance-type options, and
// budget consumed.
//
// Upstream is not deterministic when a state has more than one NodePool: single-node consolidation interleaves
// candidates by NodePool in Go map order (shuffleCandidates iterates lo.Keys of a map), so which pool's candidate it
// tries first is random, and not uniformly: Go starts iterating a small map at a random one of its group's 8 slots, so
// with two pools the second-inserted pool comes first only 1 time in 8. For those states the reference is the set of
// decisions upstream makes over repeated runs, grown adaptively: a pipeline decision outside the observed set sends
// upstream back for up to 256 more runs before it counts as a mismatch, which makes a false mismatch from an unseen
// ordering vanishingly rare ((7/8)^256). Single-NodePool states run upstream twice as a control and must match exactly.
//
//	PARITY_STATES=1000 PARITY_SEED=1 PARITY_OUT=/tmp/parity.jsonl go test ./pkg/controllers/disruption/pipeline -run TestParity -v -timeout 6h
//
// PARITY_POLICIES (default both) and PARITY_CATALOGS (default kwok,assorted) narrow the matrix.

type parityRecord struct {
	State    harness.ParityState `json:"state"`
	Catalog  string              `json:"catalog"`
	Upstream harness.Decision    `json:"upstream"`
	// Support is every distinct decision upstream made over the repeated runs.
	Support   []harness.Decision            `json:"support"`
	Pipelines map[string][]harness.Decision `json:"pipelines"`
	Match     map[string]bool               `json:"match"`
	// Deterministic reports that every upstream run agreed.
	Deterministic bool `json:"deterministic"`
}

// repeatRuns is how many times each arm runs on a multi-NodePool state, where upstream's order is random, and
// maxExtraRuns how many more upstream runs a decision outside the observed support may ask for.
const (
	repeatRuns   = 8
	maxExtraRuns = 256
)

func inSupport(d harness.Decision, support []harness.Decision) bool {
	return lo.ContainsBy(support, func(s harness.Decision) bool { return s.Equal(d) })
}

func addDistinct(support []harness.Decision, d harness.Decision) []harness.Decision {
	if inSupport(d, support) {
		return support
	}
	return append(support, d)
}

func envInt(name string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(name)); err == nil {
		return v
	}
	return def
}

func parityConfigs(policy v1.ConsolidationPolicy) map[string]pipeline.Config {
	fine := pipeline.DefaultConfig()
	fine.Moves = pipeline.StageList{{Name: "upstream-multi", Params: pipeline.Params{"mode": "fine"}}, {Name: "upstream-single", Params: pipeline.Params{"mode": "fine"}}}
	out := map[string]pipeline.Config{"coarse": pipeline.DefaultConfig(), "fine": fine}
	if policy == v1.ConsolidationPolicyBalanced {
		balanced := fine
		balanced.Planner = pipeline.StageConfig{Name: "balanced"}
		out["fine-balanced-planner"] = balanced
	}
	return out
}

func instanceTypes(t *testing.T, name string) []*cloudprovider.InstanceType {
	switch name {
	case "kwok":
		cat, err := catalog.Load(catalog.Options{MaxCPU: 16})
		if err != nil {
			t.Fatal(err)
		}
		return cat.InstanceTypes()
	case "assorted":
		// The disruption suite's assorted types (on-demand and spot, several zones), thinned to keep the
		// scheduler's per-simulation work comparable to the KWOK catalog.
		its := fake.InstanceTypesAssorted()
		return lo.Filter(its, func(_ *cloudprovider.InstanceType, i int) bool { return i%7 == 0 })
	}
	t.Fatalf("unknown catalog %q", name)
	return nil
}

func TestParity(t *testing.T) {
	states := envInt("PARITY_STATES", 40)
	seed0 := int64(envInt("PARITY_SEED", 1))
	policies := lo.Map(lo.Compact([]string{lo.CoalesceOrEmpty(os.Getenv("PARITY_POLICIES"), "WhenEmptyOrUnderutilized,Balanced")}), func(s string, _ int) string { return s })
	var out *json.Encoder
	if p := os.Getenv("PARITY_OUT"); p != "" {
		f, err := os.Create(p)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		out = json.NewEncoder(f)
	}
	catalogs := splitList(lo.CoalesceOrEmpty(os.Getenv("PARITY_CATALOGS"), "kwok,assorted"))
	for _, policyName := range splitList(policies[0]) {
		policy := v1.ConsolidationPolicy(policyName)
		for _, catName := range catalogs {
			its := instanceTypes(t, catName)
			t.Run(fmt.Sprintf("%s/%s", policy, catName), func(t *testing.T) {
				totals := map[string]int{}
				matches := map[string]int{}
				nondeterministic, decided := 0, 0
				for i := range states {
					seed := seed0 + int64(i)
					ctx := log.IntoContext(options.ToContext(context.Background(), test.Options()), logr.Discard())
					cp := fake.NewCloudProvider()
					cp.InstanceTypes = its
					env := harness.New(ctx, harness.Options{CloudProvider: cp})
					st, err := harness.GenerateState(ctx, env, seed, harness.StateOptions{Policy: policy, InstanceTypes: its, MinNodes: 4, MaxNodes: 40})
					if err != nil {
						t.Fatalf("seed %d: generating state, %v", seed, err)
					}
					rec := parityRecord{State: st, Catalog: catName, Pipelines: map[string][]harness.Decision{}, Match: map[string]bool{}}
					runs := lo.Ternary(st.NodePools > 1, repeatRuns, 2)
					rec.Upstream = env.UpstreamDecision(ctx)
					rec.Support = []harness.Decision{rec.Upstream}
					for range runs - 1 {
						rec.Support = addDistinct(rec.Support, env.UpstreamDecision(ctx))
					}
					if st.NodePools == 1 && len(rec.Support) > 1 {
						t.Errorf("seed %d %s: upstream disagreed with itself on a single-NodePool state: %+v", seed, policy, rec.Support)
					}
					if rec.Upstream.Kind != "no-op" {
						decided++
					}
					for name, cfg := range parityConfigs(policy) {
						ok := true
						for range lo.Ternary(st.NodePools > 1, runs, 1) {
							d := env.PipelineDecision(ctx, cfg, nil)
							rec.Pipelines[name] = append(rec.Pipelines[name], d)
							for extra := 0; st.NodePools > 1 && extra < maxExtraRuns && !inSupport(d, rec.Support); extra++ {
								rec.Support = addDistinct(rec.Support, env.UpstreamDecision(ctx))
							}
							if !inSupport(d, rec.Support) {
								ok = false
								t.Errorf("seed %d %s: pipeline %q decided %+v, upstream decided one of %+v", seed, policy, name, d, rec.Support)
							}
						}
						rec.Match[name] = ok
						totals[name]++
						if ok {
							matches[name]++
						}
					}
					if rec.Deterministic = len(rec.Support) == 1; !rec.Deterministic {
						nondeterministic++
					}
					if out != nil {
						if err := out.Encode(rec); err != nil {
							t.Fatal(err)
						}
					}
				}
				for name := range totals {
					t.Logf("PARITY policy=%s catalog=%s config=%s states=%d match=%d/%d upstream_decided=%d upstream_nondeterministic=%d",
						policy, catName, name, states, matches[name], totals[name], decided, nondeterministic)
				}
			})
		}
	}
}

func splitList(s string) []string {
	var out []string
	cur := ""
	for _, r := range s {
		if r == ',' {
			if cur != "" {
				out = append(out, cur)
			}
			cur = ""
			continue
		}
		cur += string(r)
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}
