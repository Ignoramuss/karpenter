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
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/samber/lo"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"sigs.k8s.io/karpenter/kwok/apis/v1alpha1"
	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/test"
	podutils "sigs.k8s.io/karpenter/pkg/utils/pod"
	"sigs.k8s.io/karpenter/test/pkg/debug"
	"sigs.k8s.io/karpenter/test/pkg/environment/common"
)

// Tier B (experiments/TIERB.md): one WB cell replayed on KWOK. experiments/tierb/run.sh writes the cell, the
// workload the simulator generated for it (consolidation-sim workload) and the catalog prices to a JSON file and
// points TIERB_CELL at it; without TIERB_CELL the spec skips. The spec provisions the workload at peak with
// consolidation held off (budget 0), drops every deployment to its low count at the warmup's end while opening the
// budget to 10%, and samples the cluster each minute the way the simulator does.

type tierBShape struct {
	CPU    int64 `json:"cpu"`
	Memory int64 `json:"memory"`
}

type tierBDeployment struct {
	Name     string     `json:"name"`
	Shape    tierBShape `json:"shape"`
	Schedule []struct {
		At       string `json:"at"`
		Replicas int32  `json:"replicas"`
	} `json:"schedule"`
}

type tierBCell struct {
	RunID    string          `json:"run_id"`
	Label    string          `json:"label"`
	Seed     int64           `json:"seed"`
	Nodes    int             `json:"nodes"`
	Policy   string          `json:"policy"`
	Gate     bool            `json:"gate"`
	Pipeline json.RawMessage `json:"pipeline,omitempty"`
	Warmup   string          `json:"warmup"`
	Window   string          `json:"window"`
	Workload struct {
		Name        string            `json:"name"`
		Deployments []tierBDeployment `json:"deployments"`
	} `json:"workload"`
	Prices map[string]float64 `json:"prices"`
	Out    string             `json:"out"`
}

type tierBRow struct {
	TMin                                         float64
	Nodes, ReadyNodes, Running, Pending, Evicted int
	CostPerHour                                  float64
}

// tierBEvictions counts the test pods the eviction API evicted: the API server marks each with a DisruptionTarget
// condition (reason EvictionByEvictionAPI) before deleting it, whichever controller asked.
type tierBEvictions struct {
	mu   sync.Mutex
	uids map[types.UID]struct{}
}

func (e *tierBEvictions) observe(obj any) {
	if d, ok := obj.(cache.DeletedFinalStateUnknown); ok {
		obj = d.Obj
	}
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		return
	}
	if _, ok := lo.Find(pod.Status.Conditions, func(c corev1.PodCondition) bool {
		return c.Type == corev1.DisruptionTarget && c.Status == corev1.ConditionTrue && c.Reason == "EvictionByEvictionAPI"
	}); ok {
		e.mu.Lock()
		e.uids[pod.UID] = struct{}{}
		e.mu.Unlock()
	}
}

func (e *tierBEvictions) count() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.uids)
}

var _ = Describe("Tier B", Label(debug.NoWatch), func() {
	It("should replay a WB cell", func() {
		path := os.Getenv("TIERB_CELL")
		if path == "" {
			Skip("TIERB_CELL is not set")
		}
		cell := tierBCell{}
		Expect(json.Unmarshal(lo.Must(os.ReadFile(path)), &cell)).To(Succeed())
		warmup, window := lo.Must(time.ParseDuration(cell.Warmup)), lo.Must(time.ParseDuration(cell.Window))
		Expect(os.MkdirAll(cell.Out, 0o755)).To(Succeed())

		// Every arm runs the chart's feature gates with ConsolidationPipeline set for it, and every attempt restarts the
		// controller (TIERB_RUN_ID carries a nonce) so no cell inherits another's in-memory state.
		env.ExpectSettingsOverridden(
			corev1.EnvVar{Name: "FEATURE_GATES", Value: tierBFeatureGates(cell.Gate)},
			corev1.EnvVar{Name: "CONSOLIDATION_PIPELINE_CONFIG", Value: lo.Ternary(cell.Gate, string(cell.Pipeline), "")},
			corev1.EnvVar{Name: "LOG_LEVEL", Value: "info"},
			corev1.EnvVar{Name: "TIERB_RUN_ID", Value: fmt.Sprintf("%s@%d", cell.RunID, time.Now().UnixNano())},
		)
		karpenterPodAtStart := env.ExpectActiveKarpenterPod().Name

		// Consolidation stays off through the warmup, as in the simulator, which runs no disruption pass before it
		// ends: consolidateAfter Never keeps every node unconsolidatable, and the 0 budget is a second guard (it alone
		// would not stop the pipeline's shift moves, which remove no node).
		nodePool.Spec.Disruption.ConsolidationPolicy = v1.ConsolidationPolicy(cell.Policy)
		nodePool.Spec.Disruption.ConsolidateAfter = v1.MustParseNillableDuration("Never")
		nodePool.Spec.Disruption.Budgets = []v1.Budget{{Nodes: "0"}}
		// The simulator's mean create-to-registered time: launch 2-5 s plus registration 30-60 s
		// (hack/consolidation-sim/sim/config.go). KWOK marks a NodeClaim launched at once and creates its Node after
		// this delay, about a second faster than the simulator's mean of 48.5 s.
		Expect(unstructured.SetNestedField(nodeClass.Object, "47s", "spec", "nodeRegistrationDelay")).To(Succeed())
		nodePool.Spec.Template.Spec.Requirements = []v1.NodeSelectorRequirementWithMinValues{
			{Key: corev1.LabelOSStable, Operator: corev1.NodeSelectorOpIn, Values: []string{string(corev1.Linux)}},
			{Key: v1.CapacityTypeLabelKey, Operator: corev1.NodeSelectorOpIn, Values: []string{v1.CapacityTypeOnDemand}},
			{Key: v1alpha1.InstanceFamilyLabelKey, Operator: corev1.NodeSelectorOpIn, Values: []string{"c7i", "m7i", "r7i"}},
			{Key: v1alpha1.InstanceCPULabelKey, Operator: corev1.NodeSelectorOpLt, Values: []string{"33"}},
		}

		deployments := lo.Map(cell.Workload.Deployments, func(d tierBDeployment, _ int) *appsv1.Deployment {
			return test.Deployment(test.CreateDeploymentOptions(d.Name, d.Schedule[0].Replicas,
				fmt.Sprintf("%dm", d.Shape.CPU), fmt.Sprintf("%dMi", d.Shape.Memory)))
		})
		selector := fmt.Sprintf("%s=%s", test.DiscoveryLabel, "unspecified")

		evictions := &tierBEvictions{uids: map[types.UID]struct{}{}}
		factory := informers.NewSharedInformerFactoryWithOptions(env.KubeClient, 0,
			informers.WithTweakListOptions(func(o *metav1.ListOptions) { o.LabelSelector = selector }))
		podInformer := factory.Core().V1().Pods().Informer()
		lo.Must(podInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
			AddFunc: evictions.observe, UpdateFunc: func(_, obj any) { evictions.observe(obj) }, DeleteFunc: evictions.observe,
		}))
		stop := make(chan struct{})
		defer close(stop)
		factory.Start(stop)
		factory.WaitForCacheSync(stop)

		// The simulator creates every peak pod at t = 0 and provisions them in one batch. Here the ReplicaSets create
		// pods at the API's rate, so the NodePool is held back until every pod exists and is unschedulable; Karpenter's
		// first pass then sees the whole workload, and t = 0 is that moment.
		for _, d := range deployments {
			env.ExpectCreated(d)
		}
		peak := lo.SumBy(cell.Workload.Deployments, func(d tierBDeployment) int { return int(d.Schedule[0].Replicas) })
		Eventually(func(g Gomega) {
			pods := &corev1.PodList{}
			g.Expect(env.Client.List(env.Context, pods, client.HasLabels{test.DiscoveryLabel})).To(Succeed())
			g.Expect(lo.CountBy(pods.Items, func(p corev1.Pod) bool { return p.DeletionTimestamp == nil && podutils.FailedToSchedule(&p) })).
				To(Equal(peak))
		}).WithTimeout(5 * time.Minute).WithPolling(2 * time.Second).Should(Succeed())
		t0 := time.Now()
		env.ExpectCreated(nodeClass, nodePool)

		var rowsMu sync.Mutex
		var rows []tierBRow
		sample := func() {
			row := tierBSample(cell, nodePool.Name, selector)
			row.TMin, row.Evicted = time.Since(t0).Minutes(), evictions.count()
			rowsMu.Lock()
			rows = append(rows, row)
			rowsMu.Unlock()
		}
		end := t0.Add(warmup + window)
		samplerDone := make(chan struct{})
		go func() {
			defer GinkgoRecover()
			defer close(samplerDone)
			for i := 0; ; i++ {
				next := t0.Add(time.Duration(i) * time.Minute)
				if next.After(end) {
					return
				}
				time.Sleep(time.Until(next))
				sample()
			}
		}()

		time.Sleep(time.Until(t0.Add(warmup)))
		atDrop := tierBSample(cell, nodePool.Name, selector)
		evictionsAtDrop := evictions.count()
		karpenterPod := env.ExpectActiveKarpenterPod()
		restartsAtDrop := lo.SumBy(karpenterPod.Status.ContainerStatuses, func(s corev1.ContainerStatus) int32 { return s.RestartCount })
		poller := common.StartKarpenterMetricsPoller(env)
		dropAt := time.Now()

		stored := nodePool.DeepCopy()
		nodePool.Spec.Disruption.ConsolidateAfter = v1.MustParseNillableDuration("30s")
		nodePool.Spec.Disruption.Budgets = []v1.Budget{{Nodes: "10%"}}
		Expect(env.Client.Patch(env.Context, nodePool, client.MergeFrom(stored))).To(Succeed())
		for i, d := range deployments {
			low := cell.Workload.Deployments[i].Schedule[1].Replicas
			before := d.DeepCopy()
			d.Spec.Replicas = new(low)
			Expect(env.Client.Patch(env.Context, d, client.MergeFrom(before))).To(Succeed())
		}

		<-samplerDone
		stats := poller.Stop()
		finished := time.Now()
		karpenterPodEnd := env.ExpectActiveKarpenterPod()
		restartsAtEnd := lo.SumBy(karpenterPodEnd.Status.ContainerStatuses, func(s corev1.ContainerStatus) int32 { return s.RestartCount })
		restarted := karpenterPodEnd.Name != karpenterPod.Name || karpenterPod.Name != karpenterPodAtStart ||
			restartsAtEnd != restartsAtDrop
		// Which method evaluated decisions, and how long evaluation took. The gate swaps multi-node and single-node
		// consolidation for the pipeline (pkg/controllers/controllers.go, disruptionMethods) and leaves the other
		// methods, so a gate-on arm must show pipeline evaluations and no multi or single ones, and a gate-off arm the
		// reverse.
		evaluations, evaluationSeconds, decisions := map[string]float64{}, map[string]float64{}, map[string]float64{}
		for _, m := range env.ExpectPodMetrics() {
			ct := m.Labels["consolidation_type"]
			switch m.Name {
			case "karpenter_voluntary_disruption_decision_evaluation_duration_seconds_count":
				evaluations[ct] += m.Value
			case "karpenter_voluntary_disruption_decision_evaluation_duration_seconds_sum":
				evaluationSeconds[ct] += m.Value
			case "karpenter_voluntary_disruption_decisions_total":
				decisions[ct] += m.Value
			}
		}
		pipelineEvals := evaluations[disruption.PipelineConsolidationType.Name]
		upstreamEvals := evaluations[disruption.MultiNodeConsolidationType.Name] + evaluations[disruption.SingleNodeConsolidationType.Name]
		methodOK := lo.Ternary(cell.Gate, pipelineEvals > 0 && upstreamEvals == 0, pipelineEvals == 0 && upstreamEvals > 0)

		var csv strings.Builder
		csv.WriteString("t_min,nodes,ready_nodes,cost_per_hour,pods_running,pods_pending,evictions_cum\n")
		for _, r := range rows {
			fmt.Fprintf(&csv, "%.2f,%d,%d,%.6f,%d,%d,%d\n", r.TMin, r.Nodes, r.ReadyNodes, r.CostPerHour, r.Running, r.Pending, r.Evicted)
		}
		Expect(os.WriteFile(filepath.Join(cell.Out, "timeseries.csv"), []byte(csv.String()), 0o600)).To(Succeed())
		summary := map[string]any{
			"run_id": cell.RunID, "label": cell.Label, "seed": cell.Seed, "scenario_nodes": cell.Nodes, "policy": cell.Policy,
			"gate": cell.Gate, "warmup": cell.Warmup, "window": cell.Window,
			"started_at": t0.UTC().Format(time.RFC3339Nano), "drop_at": dropAt.UTC().Format(time.RFC3339Nano),
			"finished_at":     finished.UTC().Format(time.RFC3339Nano),
			"pending_at_drop": atDrop.Pending, "nodes_at_drop": atDrop.Nodes, "cost_at_drop": atDrop.CostPerHour,
			"karpenter_restarted": restarted, "karpenter_pod": karpenterPod.Name,
			"evictions_before_drop": evictionsAtDrop, "method_ok": methodOK,
			"valid":                       atDrop.Pending == 0 && !restarted && evictionsAtDrop == 0 && methodOK && stats.SampleCount > 0,
			"evictions":                   evictions.count() - evictionsAtDrop,
			"decision_evaluations":        evaluations,
			"decision_evaluation_seconds": evaluationSeconds,
			"decisions_by_consolidation":  decisions,
			"karpenter_avg_cpu_cores":     stats.AvgCPUCores, "karpenter_p95_cpu_cores": stats.P95CPUCores,
			"karpenter_max_cpu_cores": stats.MaxCPUCores, "karpenter_cpu_core_seconds": stats.AvgCPUCores * finished.Sub(dropAt).Seconds(),
			"karpenter_max_memory_mb": stats.MaxMemoryMB, "karpenter_p95_memory_mb": stats.P95MemoryMB,
			"karpenter_max_heap_mb": stats.MaxHeapMB, "metrics_sample_count": stats.SampleCount,
		}
		Expect(os.WriteFile(filepath.Join(cell.Out, "summary.json"), lo.Must(json.MarshalIndent(summary, "", "  ")), 0o600)).To(Succeed())
		Expect(stats.SampleCount).To(BeNumerically(">", 0), "the metrics poller collected no samples")
	})
})

// tierBFeatureGates is the controller's current FEATURE_GATES with ConsolidationPipeline set to gate.
func tierBFeatureGates(gate bool) string {
	d := &appsv1.Deployment{}
	Expect(env.Client.Get(env.Context, types.NamespacedName{Namespace: "kube-system", Name: "karpenter"}, d)).To(Succeed())
	current, _ := lo.Find(d.Spec.Template.Spec.Containers[0].Env, func(e corev1.EnvVar) bool { return e.Name == "FEATURE_GATES" })
	gates := lo.Filter(strings.Split(current.Value, ","), func(g string, _ int) bool {
		return g != "" && !strings.HasPrefix(g, options.ConsolidationPipelineFeatureGate.Name+"=")
	})
	return strings.Join(append(gates, fmt.Sprintf("%s=%t", options.ConsolidationPipelineFeatureGate.Name, gate)), ",")
}

// tierBSample counts what the simulator's collector samples: launched NodeClaims (the instances being paid for) and
// their price, Ready nodes, and the test pods by phase.
func tierBSample(cell tierBCell, nodePoolName, selector string) tierBRow {
	row := tierBRow{}
	ncs := &v1.NodeClaimList{}
	Expect(env.Client.List(env.Context, ncs, client.MatchingLabels{v1.NodePoolLabelKey: nodePoolName})).To(Succeed())
	for _, nc := range ncs.Items {
		if !nc.StatusConditions().Get(v1.ConditionTypeLaunched).IsTrue() {
			continue
		}
		row.Nodes++
		price, ok := cell.Prices[nc.Labels[corev1.LabelInstanceTypeStable]]
		Expect(ok).To(BeTrue(), "no catalog price for NodeClaim %s's instance type %q", nc.Name, nc.Labels[corev1.LabelInstanceTypeStable])
		row.CostPerHour += price
	}
	nodes := &corev1.NodeList{}
	Expect(env.Client.List(env.Context, nodes, client.MatchingLabels{v1.NodePoolLabelKey: nodePoolName})).To(Succeed())
	row.ReadyNodes = lo.CountBy(nodes.Items, func(n corev1.Node) bool {
		c, ok := lo.Find(n.Status.Conditions, func(c corev1.NodeCondition) bool { return c.Type == corev1.NodeReady })
		return ok && c.Status == corev1.ConditionTrue
	})
	pods := &corev1.PodList{}
	Expect(env.Client.List(env.Context, pods, client.HasLabels{strings.SplitN(selector, "=", 2)[0]})).To(Succeed())
	for _, p := range pods.Items {
		if p.DeletionTimestamp != nil {
			continue
		}
		switch p.Status.Phase {
		case corev1.PodRunning:
			row.Running++
		case corev1.PodPending:
			row.Pending++
		}
	}
	return row
}
