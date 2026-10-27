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
	"errors"
	"math/rand"
	"testing"

	"github.com/go-logr/logr"
	"github.com/samber/lo"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/cloudprovider/fake"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/conformance"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/test"
)

func liveSnapshot(t *testing.T, seed int64) (context.Context, *pipeline.Upstream, *pipeline.State) {
	ctx := log.IntoContext(options.ToContext(context.Background(), test.Options()), logr.Discard())
	cat, err := catalog.Load(catalog.Options{MaxCPU: 16})
	if err != nil {
		t.Fatal(err)
	}
	cp := fake.NewCloudProvider()
	cp.InstanceTypes = cat.InstanceTypes()
	env := harness.New(ctx, harness.Options{CloudProvider: cp})
	if _, err := harness.GenerateState(ctx, env, seed, harness.StateOptions{Policy: v1.ConsolidationPolicyWhenEmptyOrUnderutilized,
		InstanceTypes: cp.InstanceTypes, MinNodes: 6, MaxNodes: 30}); err != nil {
		t.Fatal(err)
	}
	up := pipeline.NewUpstream(env.Clock, env.Client, env.Cluster, env.Provisioner, env.CloudProvider, env.Recorder, env.Queue,
		pipeline.UpstreamOptions{WrapValidator: harness.ZeroDelay})
	cands, totals, err := env.SortedCandidates(ctx, up.Single.ShouldDisrupt)
	if err != nil {
		t.Fatal(err)
	}
	budgets, err := disruption.BuildDisruptionBudgetMapping(ctx, env.Cluster, env.Clock, env.Client, env.CloudProvider, env.Recorder, v1.DisruptionReasonUnderutilized)
	if err != nil {
		t.Fatal(err)
	}
	up.SetCycle(cands, totals, budgets)
	snap, err := pipeline.BuildSnapshot(ctx, up)
	if err != nil {
		t.Fatal(err)
	}
	return ctx, up, snap
}

// TestFastNeverAcceptsWhatFullRejectsOnResources checks Fast against Karpenter's scheduling simulation on live
// states. Disagreements on moves whose pods carry topology constraints (spread, pod affinity, anti-affinity) are
// counted and logged, not failed: Fast checks hostname anti-affinity only.
//
//nolint:gocyclo
func TestFastNeverAcceptsWhatFullRejectsOnResources(t *testing.T) {
	var checked, topology, resourceDisagree, greedy int
	for seed := int64(1); seed <= 40; seed++ {
		ctx, up, snap := liveSnapshot(t, seed)
		feas := &pipeline.UpstreamFeasibility{FastChecker: &pipeline.FastChecker{}, Up: up}
		for _, m := range conformance.ReferenceMoves(snap, rand.New(rand.NewSource(seed)), 20) { //nolint:gosec
			if feas.Fast(snap, m) != nil {
				continue
			}
			checked++
			_, err := feas.Full(ctx, snap, m)
			if err == nil {
				continue
			}
			if !errors.Is(err, pipeline.ErrInfeasible) {
				t.Fatalf("seed %d: Full errored on %s: %v", seed, m, err)
			}
			constrained := lo.SomeBy(m.Bindings, func(b pipeline.Binding) bool {
				for _, p := range snap.Pods(b.From) {
					if p.Key == b.Pod && p.Pod != nil && (len(p.Pod.Spec.TopologySpreadConstraints) > 0 || p.Pod.Spec.Affinity != nil) {
						return true
					}
				}
				return false
			})
			switch {
			case constrained:
				topology++
			case m.Kind == pipeline.Merge:
				// A merge's Full check is computeConsolidation, which places pods first-fit in its own order and may
				// miss a packing Fast found; that is a search gap, not a resource disagreement.
				greedy++
			default:
				resourceDisagree++
				t.Errorf("seed %d: Fast accepted %s but Full rejected it on resources: %v", seed, m, err)
			}
		}
	}
	t.Logf("FASTFULL checked=%d topology_disagreements=%d merge_search_gaps=%d resource_disagreements=%d", checked, topology, greedy, resourceDisagree)
	if checked == 0 {
		t.Fatal("no moves checked")
	}
}

// TestCommandMoveRoundTrip converts upstream commands to moves and confirms them back through Full, which must
// reproduce the same decision, candidates and replacement options.
//
//nolint:gocyclo
func TestCommandMoveRoundTrip(t *testing.T) {
	trips := 0
	for seed := int64(1); seed <= 30; seed++ {
		ctx, up, snap := liveSnapshot(t, seed)
		feas := &pipeline.UpstreamFeasibility{FastChecker: &pipeline.FastChecker{}, Up: up}
		for _, name := range []string{"node-000", "node-001", "node-002"} {
			c, ok := up.Candidate(name)
			if !ok {
				continue
			}
			cmd, err := up.Single.ComputeConsolidation(ctx, c)
			if err != nil || cmd.Decision() == disruption.NoOpDecision {
				continue
			}
			m := pipeline.MoveFromCommand(snap, cmd, "rt", "test", false)
			if _, err := snap.Apply(m); err != nil {
				t.Errorf("seed %d: move from %s command does not apply: %v", seed, cmd.Decision(), err)
			}
			m.Upstream = nil
			confirmed, err := feas.Full(ctx, snap, m)
			if err != nil {
				t.Errorf("seed %d: Full rejected the move from a %s command: %v", seed, cmd.Decision(), err)
				continue
			}
			uc, ok := pipeline.CommandOf(confirmed)
			if !ok {
				t.Fatalf("seed %d: confirmed move carries no command", seed)
			}
			back := uc.Command
			if back.Decision() != cmd.Decision() || !lo.ElementsMatch(back.SourceNodeNames(), cmd.SourceNodeNames()) {
				t.Errorf("seed %d: round trip changed %s %v into %s %v", seed, cmd.Decision(), cmd.SourceNodeNames(), back.Decision(), back.SourceNodeNames())
			}
			if len(cmd.Replacements) > 0 && len(back.Replacements) > 0 {
				a := lo.Map(cmd.Replacements[0].InstanceTypeOptions, func(it *cloudprovider.InstanceType, _ int) string { return it.Name })
				b := lo.Map(back.Replacements[0].InstanceTypeOptions, func(it *cloudprovider.InstanceType, _ int) string { return it.Name })
				if !lo.ElementsMatch(a, b) {
					t.Errorf("seed %d: round trip changed replacement options %v into %v", seed, a, b)
				}
			}
			trips++
		}
	}
	t.Logf("ROUNDTRIP trips=%d", trips)
	if trips == 0 {
		t.Fatal("no commands to round-trip")
	}
}
