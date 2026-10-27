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

package performance

import (
	"fmt"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	corev1 "k8s.io/api/core/v1"

	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/test"
	"sigs.k8s.io/karpenter/test/pkg/debug"
	"sigs.k8s.io/karpenter/test/pkg/environment/common"
)

// Tier B of the consolidation pipeline experiments (docs/research/PLAN.md, Phase 2): the simulator's top
// configurations on KWOK, to check that the simulator's rankings survive a real API server, kube-scheduler and
// controller. Each spec enables the ConsolidationPipeline gate with an inline pipeline config, scales a diurnal-shaped
// workload out and back down by 65% (the simulator's W2), and reports consolidation the way the Balanced specs do.
// The controller's decision log comes from its debug logs (LogRecorder, "consolidation decision" lines).
//
// Run with: FOCUS="Consolidation Pipeline" TEST_SUITE=performance make e2etests, on a kind cluster created by
// make setup-kind (whose kube-scheduler carries the most-allocated profile these specs can opt into).

// pipelineConfigs are inline pipeline configs, keyed by the simulator arm they mirror.
var pipelineConfigs = map[string]string{
	// B4: the upstream adapters, which must reproduce default consolidation (B2).
	"B4-upstream-adapters": `{"moves": [{"name": "upstream-multi", "params": {"mode": "fine"}}, {"name": "upstream-single", "params": {"mode": "fine"}}]}`,
	// Heuristic generation with entropy-guided planning.
	"heuristic-entropy-greedy": `{"moves": [{"name": "upstream-multi", "params": {"mode": "fine"}}, {"name": "upstream-single", "params": {"mode": "fine"}}],
		"movesMode": "pooled", "planner": {"name": "entropy-greedy"}, "executor": {"name": "shift-evict"},
		"potential": {"name": "util-entropy"}, "search": {"maxFullSimulations": 12}}`,
	// Random generation with entropy-guided planning, the central claim (H1).
	"random-entropy-greedy": `{"candidates": {"name": "uniform-random"}, "moves": [{"name": "random-merge"}, {"name": "random-shift"}],
		"movesMode": "pooled", "planner": {"name": "entropy-greedy"}, "executor": {"name": "shift-evict"},
		"potential": {"name": "util-entropy"}, "search": {"maxFullSimulations": 12, "maxAttempts": 200}}`,
}

var _ = Describe("Performance", Label(debug.NoWatch), func() {
	Context("Consolidation Pipeline", func() {
		for _, name := range []string{"B4-upstream-adapters", "heuristic-entropy-greedy", "random-entropy-greedy"} {
			for _, schedulerName := range []string{"", "most-allocated"} {
				It(fmt.Sprintf("should consolidate a diurnal scale-down with %s (scheduler %q)", name, schedulerName), func() {
					env.ExpectSettingsOverridden(
						corev1.EnvVar{Name: "FEATURE_GATES", Value: fmt.Sprintf("%s=true", options.ConsolidationPipelineFeatureGate.Name)},
						corev1.EnvVar{Name: "CONSOLIDATION_PIPELINE_CONFIG", Value: pipelineConfigs[name]},
						corev1.EnvVar{Name: "LOG_LEVEL", Value: "debug"},
					)
					small := test.Deployment(test.CreateDeploymentOptions("small-resource-app", 500, "900m", "3100Mi"))
					large := test.Deployment(test.CreateDeploymentOptions("large-resource-app", 500, "3500m", "28Gi"))
					small.Spec.Template.Spec.SchedulerName = schedulerName
					large.Spec.Template.Spec.SchedulerName = schedulerName
					env.ExpectCreated(nodePool, nodeClass, small, large)

					report := fmt.Sprintf("pipeline_%s_%s", name, map[string]string{"": "default", "most-allocated": "most_allocated"}[schedulerName])
					scaleOut, err := ReportScaleOutWithOutput(env, "Pipeline Scale Out "+name, 1000, 15*time.Minute, report+"_scale_out")
					Expect(err).ToNot(HaveOccurred())
					Expect(scaleOut.TotalNodes).To(BeNumerically(">", 0))

					h, err := common.StartLatencyHarness(env)
					Expect(err).ToNot(HaveOccurred())
					small.Spec.Replicas = new(int32(175))
					large.Spec.Replicas = new(int32(175))
					env.ExpectUpdated(small, large)

					consolidation, err := ReportConsolidation(env, "Pipeline Consolidation "+name, 1000, 350, scaleOut.TotalNodes, 30*time.Minute)
					Expect(err).ToNot(HaveOccurred())
					result, err := h.Stop()
					Expect(err).ToNot(HaveOccurred())
					consolidation.LatencyStats = result.LatencyStats
					consolidation.Counters = result.Counters
					OutputPerformanceReport(consolidation, report+"_consolidation")
				})
			}
		}
	})
})
