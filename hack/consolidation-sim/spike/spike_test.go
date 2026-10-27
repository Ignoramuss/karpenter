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

// Package spike times the two simulator designs the plan asks to compare before choosing one (Phase 2, Tier A):
// an envtest-backed harness (the disruption suite's pattern) and an in-memory API (a controller-runtime fake client)
// hosting the same real Karpenter code. Both build the same seeded fragmented cluster and run one upstream
// consolidation cycle: candidate construction, multi-node and single-node ComputeCommands, and SimulateScheduling.
//
//	SPIKE_NODES=100,1000 SPIKE_BACKENDS=fake,envtest go test ./hack/consolidation-sim/spike -run TestSpike -v -timeout 60m
package spike

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/awslabs/operatorpkg/status"
	"github.com/go-logr/logr"
	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	coreapis "sigs.k8s.io/karpenter/pkg/apis"
	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/cloudprovider/fake"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	kube "sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	pscheduling "sigs.k8s.io/karpenter/pkg/controllers/provisioning/scheduling"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/controllers/state/informer"
	"sigs.k8s.io/karpenter/pkg/events"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/state/cost"
	"sigs.k8s.io/karpenter/pkg/test"
	"sigs.k8s.io/karpenter/pkg/test/v1alpha1"
)

type nopValidator struct{}

func (nopValidator) Validate(_ context.Context, cmd disruption.Command, _ time.Duration) (disruption.Command, error) {
	return cmd, nil
}

type harness struct {
	client      client.Client
	clock       clock.Clock
	cluster     *state.Cluster
	cp          cloudprovider.CloudProvider
	prov        *provisioning.Provisioner
	queue       *disruption.Queue
	recorder    events.Recorder
	clusterCost *cost.ClusterCost
	// syncState informs cluster state of an object written to the client; a no-op where an interceptor does it.
	syncState func(ctx context.Context, obj client.Object)
}

func newCloudProvider(t *testing.T) *fake.CloudProvider {
	cat, err := catalog.Load(catalog.Options{MaxCPU: 16})
	if err != nil {
		t.Fatal(err)
	}
	cp := fake.NewCloudProvider()
	cp.InstanceTypes = cat.InstanceTypes()
	return cp
}

func fakeHarness(ctx context.Context, t *testing.T) *harness {
	cp := newCloudProvider(t)
	e := kube.New(ctx, kube.Options{CloudProvider: cp})
	return &harness{client: e.Client, clock: e.Clock, cluster: e.Cluster, cp: cp, prov: e.Provisioner, queue: e.Queue,
		recorder: e.Recorder, clusterCost: e.ClusterCost, syncState: func(context.Context, client.Object) {}}
}

func envtestHarness(ctx context.Context, t *testing.T) (*harness, func()) {
	env := test.NewEnvironment(test.WithCRDs(coreapis.CRDs...), test.WithCRDs(v1alpha1.CRDs...))
	cp := newCloudProvider(t)
	cc := cost.NewClusterCost(ctx, cp, env.Client)
	cluster := state.NewCluster(env.Clock, env.Client, cp)
	recorder := events.NewRecorder(&record.FakeRecorder{})
	prov := provisioning.NewProvisioner(env.Client, recorder, cp, cluster, env.Clock, nil, nil, nil)
	nodeInf, ncInf, podInf := informer.NewNodeController(env.Client, cluster), informer.NewNodeClaimController(env.Client, cp, cluster, cc), informer.NewPodController(env.Client, cluster)
	h := &harness{client: env.Client, clock: env.Clock, cluster: cluster, cp: cp, prov: prov, recorder: recorder, clusterCost: cc,
		queue: disruption.NewQueue(env.Client, recorder, cluster, env.Clock, prov)}
	h.syncState = func(ctx context.Context, obj client.Object) {
		req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: obj.GetName()}}
		switch obj.(type) {
		case *corev1.Node:
			_, _ = nodeInf.Reconcile(ctx, req)
		case *v1.NodeClaim:
			_, _ = ncInf.Reconcile(ctx, req)
		case *corev1.Pod:
			_, _ = podInf.Reconcile(ctx, req)
		}
	}
	return h, func() { _ = env.Stop() }
}

// apply creates an object and writes its status, the way the API would after a controller set it.
func (h *harness) apply(ctx context.Context, t *testing.T, obj client.Object) {
	st := obj.DeepCopyObject().(client.Object)
	if err := h.client.Create(ctx, obj); err != nil {
		t.Fatalf("creating %T %s, %v", obj, obj.GetName(), err)
	}
	st.SetResourceVersion(obj.GetResourceVersion())
	st.SetUID(obj.GetUID())
	if err := h.client.Status().Update(ctx, st); err != nil && !strings.Contains(err.Error(), "not found") {
		t.Fatalf("status %T %s, %v", obj, obj.GetName(), err)
	}
	h.syncState(ctx, obj)
}

// populate builds n nodes of random c7i/m7i/r7i sizes, each filled to a random 20-80% of CPU by pods with random
// requests: the fragmented-start shape (W1).
func (h *harness) populate(ctx context.Context, t *testing.T, n int, seed int64) (pods int) {
	rng := rand.New(rand.NewSource(seed)) //nolint:gosec
	np := test.NodePool(v1.NodePool{ObjectMeta: metav1.ObjectMeta{Name: "default"}, Spec: v1.NodePoolSpec{Disruption: v1.Disruption{
		ConsolidateAfter: v1.MustParseNillableDuration("0s"), ConsolidationPolicy: v1.ConsolidationPolicyWhenEmptyOrUnderutilized,
		Budgets: []v1.Budget{{Nodes: "100%"}},
	}}})
	np.Spec.Limits = nil
	np.StatusConditions().SetTrue(status.ConditionReady)
	h.apply(ctx, t, np)
	its := lo.Filter(h.cp.(*fake.CloudProvider).InstanceTypes, func(it *cloudprovider.InstanceType, _ int) bool {
		c := it.Capacity[corev1.ResourceCPU]
		return c.Value() >= 2 && c.Value() <= 16
	})
	cpus := []string{"250m", "500m", "1", "2"}
	mems := []string{"512Mi", "1Gi", "2Gi", "4Gi"}
	now := metav1.NewTime(h.clock.Now())
	for i := range n {
		it := its[rng.Intn(len(its))]
		off := it.Offerings[rng.Intn(len(it.Offerings))]
		zone := off.Requirements.Get(corev1.LabelTopologyZone).Any()
		name := fmt.Sprintf("node-%05d", i)
		pid := fmt.Sprintf("fake:///spike/%05d", i)
		labels := map[string]string{
			v1.NodePoolLabelKey: np.Name, corev1.LabelInstanceTypeStable: it.Name, v1.CapacityTypeLabelKey: v1.CapacityTypeOnDemand,
			corev1.LabelTopologyZone: zone, corev1.LabelHostname: name, v1.NodeRegisteredLabelKey: "true", v1.NodeInitializedLabelKey: "true",
			corev1.LabelArchStable: "amd64", corev1.LabelOSStable: "linux",
		}
		nc := test.NodeClaim(v1.NodeClaim{ObjectMeta: metav1.ObjectMeta{Name: "nc-" + name, Labels: labels, Finalizers: []string{v1.TerminationFinalizer}},
			Status: v1.NodeClaimStatus{ProviderID: pid, NodeName: name, Capacity: it.Capacity, Allocatable: it.Allocatable()}})
		nc.CreationTimestamp = now
		for _, c := range []string{v1.ConditionTypeLaunched, v1.ConditionTypeRegistered, v1.ConditionTypeInitialized, v1.ConditionTypeConsolidatable} {
			nc.StatusConditions(status.WithClock(h.clock)).SetTrue(c)
		}
		node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels, CreationTimestamp: now}, Spec: corev1.NodeSpec{ProviderID: pid},
			Status: corev1.NodeStatus{Capacity: it.Capacity, Allocatable: it.Allocatable(),
				Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue, LastTransitionTime: now}}}}
		h.apply(ctx, t, nc)
		h.apply(ctx, t, node)
		alloc := it.Allocatable()[corev1.ResourceCPU]
		target := int64(float64(alloc.MilliValue()) * (0.2 + 0.6*rng.Float64()))
		used := int64(0)
		for j := 0; ; j++ {
			c := resource.MustParse(cpus[rng.Intn(len(cpus))])
			if used+c.MilliValue() > target {
				break
			}
			used += c.MilliValue()
			pod := test.Pod(test.PodOptions{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("p-%05d-%03d", i, j), Namespace: "default",
				OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "ReplicaSet", Name: fmt.Sprintf("rs-%03d", rng.Intn(50)), UID: types.UID(fmt.Sprintf("rs-%03d", j%50)), Controller: lo.ToPtr(true)}}},
				NodeName: name, Phase: corev1.PodRunning,
				ResourceRequirements: corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: c, corev1.ResourceMemory: resource.MustParse(mems[rng.Intn(len(mems))])}}})
			pod.CreationTimestamp = now
			h.apply(ctx, t, pod)
			pods++
		}
	}
	return pods
}

func TestSpike(t *testing.T) {
	sizes := lo.Map(strings.Split(lo.CoalesceOrEmpty(os.Getenv("SPIKE_NODES"), "100"), ","), func(s string, _ int) int { return lo.Must(strconv.Atoi(s)) })
	backends := strings.Split(lo.CoalesceOrEmpty(os.Getenv("SPIKE_BACKENDS"), "fake"), ",")
	for _, backend := range backends {
		for _, n := range sizes {
			t.Run(fmt.Sprintf("%s/%d", backend, n), func(t *testing.T) {
				ctx := log.IntoContext(options.ToContext(context.Background(), test.Options()), logr.Discard())
				var h *harness
				switch backend {
				case "fake":
					h = fakeHarness(ctx, t)
				case "envtest":
					var stop func()
					h, stop = envtestHarness(ctx, t)
					defer stop()
				}
				start := time.Now()
				pods := h.populate(ctx, t, n, 1)
				tPopulate := time.Since(start)
				h.cluster.SetSynced(true)

				c := disruption.MakeConsolidation(h.clock, h.cluster, h.client, h.prov, h.cp, h.recorder, h.queue)
				multi := disruption.NewMultiNodeConsolidation(c, disruption.WithValidator(nopValidator{}))
				single := disruption.NewSingleNodeConsolidation(c, disruption.WithValidator(nopValidator{}))

				start = time.Now()
				cands, totals, err := disruption.GetCandidatesWithTotals(ctx, h.cluster, h.client, h.recorder, h.clock, h.cp, multi.ShouldDisrupt, multi.Class(), h.queue, h.clusterCost)
				if err != nil {
					t.Fatal(err)
				}
				tCandidates := time.Since(start)
				multi.SetNodePoolTotals(totals)
				single.SetNodePoolTotals(totals)

				budgets := func() map[string]int {
					m, err := disruption.BuildDisruptionBudgetMapping(ctx, h.cluster, h.clock, h.client, h.cp, h.recorder, multi.Reason())
					if err != nil {
						t.Fatal(err)
					}
					return m
				}
				start = time.Now()
				mcmds, err := multi.ComputeCommands(ctx, budgets(), cands...)
				if err != nil {
					t.Fatal(err)
				}
				tMulti := time.Since(start)
				h.cluster.MarkUnconsolidated()
				start = time.Now()
				scmds, err := single.ComputeCommands(ctx, budgets(), cands...)
				if err != nil {
					t.Fatal(err)
				}
				tSingle := time.Since(start)

				// Mean cost of one single-candidate scheduling simulation, the unit of work single-node
				// consolidation repeats per candidate and multi-node repeats per binary-search step.
				k := lo.Min([]int{10, len(cands)})
				start = time.Now()
				for _, cand := range cands[:k] {
					if _, err := disruption.SimulateScheduling(ctx, h.client, h.cluster, h.prov, h.clock, h.recorder,
						[]pscheduling.Options{pscheduling.IsConsolidationSimulation}, disruption.SimulationOptions{}, cand); err != nil {
						t.Fatal(err)
					}
				}
				tSim := time.Since(start) / time.Duration(lo.Max([]int{k, 1}))
				t.Logf("SPIKE backend=%s nodes=%d pods=%d candidates=%d populate=%s candidates_build=%s multi=%s(%s) single=%s(%s) simulate_one=%s",
					backend, n, pods, len(cands), tPopulate.Round(time.Millisecond), tCandidates.Round(time.Millisecond),
					tMulti.Round(time.Millisecond), describe(mcmds), tSingle.Round(time.Millisecond), describe(scmds), tSim.Round(time.Microsecond))
			})
		}
	}
}

func describe(cmds []disruption.Command) string {
	if len(cmds) == 0 {
		return "none"
	}
	return fmt.Sprintf("%s:%d", cmds[0].Decision(), len(cmds[0].Candidates))
}
