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

package harness

import (
	"context"
	"fmt"
	"strings"

	"github.com/awslabs/operatorpkg/status"
	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/test"
)

// Apply creates an object and then writes its status, as the API server would after a controller set it.
func (e *Env) Apply(ctx context.Context, obj client.Object) error {
	st := obj.DeepCopyObject().(client.Object)
	if err := e.Client.Create(ctx, obj); err != nil {
		return fmt.Errorf("creating %T %s, %w", obj, obj.GetName(), err)
	}
	switch obj.(type) {
	case *v1.NodeClaim, *v1.NodePool, *corev1.Node, *corev1.Pod, *policyv1.PodDisruptionBudget:
		st.SetResourceVersion(obj.GetResourceVersion())
		st.SetUID(obj.GetUID())
		st.SetName(obj.GetName())
		st.SetCreationTimestamp(obj.GetCreationTimestamp())
		if err := e.Client.Status().Update(ctx, st); err != nil && !strings.Contains(err.Error(), "not found") {
			return fmt.Errorf("writing status of %T %s, %w", obj, obj.GetName(), err)
		}
		return e.Client.Get(ctx, client.ObjectKeyFromObject(obj), obj)
	}
	return nil
}

// NodePool returns a ready NodePool with the given disruption settings and no limits, named name.
func NodePool(name string, policy v1.ConsolidationPolicy, consolidateAfter string, budgets ...string) *v1.NodePool {
	np := test.NodePool(v1.NodePool{ObjectMeta: metav1.ObjectMeta{Name: name}, Spec: v1.NodePoolSpec{Disruption: v1.Disruption{
		ConsolidateAfter:    v1.MustParseNillableDuration(consolidateAfter),
		ConsolidationPolicy: policy,
		Budgets:             lo.Map(lo.Ternary(len(budgets) == 0, []string{"100%"}, budgets), func(b string, _ int) v1.Budget { return v1.Budget{Nodes: b} }),
	}}})
	np.Spec.Limits = nil
	np.CreationTimestamp = metav1.Time{}
	np.StatusConditions().SetTrue(status.ConditionReady)
	return np
}

// NodeSpec describes one node to add.
type NodeSpec struct {
	Name         string
	NodePool     string
	InstanceType *cloudprovider.InstanceType
	Zone         string
	CapacityType string
	// Uninitialized leaves the node registered but not initialized.
	Uninitialized bool
	// NotConsolidatable leaves the NodeClaim without the Consolidatable condition, as when consolidateAfter has not
	// elapsed.
	NotConsolidatable bool
	DoNotDisrupt      bool
	Taints            []corev1.Taint
}

// ProviderID is the provider ID a node of this name gets.
func ProviderID(name string) string { return "sim:///" + name }

// NodeLabels returns the labels a launched node of this type carries: every single-valued instance type requirement
// (as the fake and KWOK providers apply them), plus the offering's zone and capacity type, the NodePool and hostname.
func NodeLabels(nodePool string, it *cloudprovider.InstanceType, zone, capacityType, hostname string) map[string]string {
	labels := map[string]string{}
	for key, req := range it.Requirements {
		if req.Operator() == corev1.NodeSelectorOpIn && req.Len() == 1 {
			labels[key] = req.Any()
		}
	}
	labels[corev1.LabelInstanceTypeStable] = it.Name
	labels[corev1.LabelTopologyZone] = zone
	labels[v1.CapacityTypeLabelKey] = capacityType
	labels[v1.NodePoolLabelKey] = nodePool
	labels[corev1.LabelHostname] = hostname
	if _, ok := labels[corev1.LabelArchStable]; !ok {
		labels[corev1.LabelArchStable] = "amd64"
	}
	if _, ok := labels[corev1.LabelOSStable]; !ok {
		labels[corev1.LabelOSStable] = string(corev1.Linux)
	}
	return labels
}

// AddNode creates a launched, registered NodeClaim and its Node, initialized unless the spec says otherwise.
func (e *Env) AddNode(ctx context.Context, spec NodeSpec) (*v1.NodeClaim, *corev1.Node, error) {
	now := metav1.NewTime(e.Clock.Now())
	labels := NodeLabels(spec.NodePool, spec.InstanceType, spec.Zone, spec.CapacityType, spec.Name)
	if !spec.Uninitialized {
		labels[v1.NodeRegisteredLabelKey] = "true"
		labels[v1.NodeInitializedLabelKey] = "true"
	}
	annotations := map[string]string{}
	if spec.DoNotDisrupt {
		annotations[v1.DoNotDisruptAnnotationKey] = "true"
	}
	pid := ProviderID(spec.Name)
	nc := test.NodeClaim(v1.NodeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: spec.Name, Labels: labels, Annotations: annotations, Finalizers: []string{v1.TerminationFinalizer}},
		Status: v1.NodeClaimStatus{ProviderID: pid, NodeName: spec.Name, Capacity: spec.InstanceType.Capacity,
			Allocatable: spec.InstanceType.Allocatable()},
	})
	nc.CreationTimestamp = now
	conds := []string{v1.ConditionTypeLaunched, v1.ConditionTypeRegistered}
	if !spec.Uninitialized {
		conds = append(conds, v1.ConditionTypeInitialized)
		if !spec.NotConsolidatable {
			conds = append(conds, v1.ConditionTypeConsolidatable)
		}
	}
	for _, c := range conds {
		nc.StatusConditions(status.WithClock(e.Clock)).SetTrue(c)
	}
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: spec.Name, Labels: labels, Annotations: annotations, CreationTimestamp: now},
		Spec:       corev1.NodeSpec{ProviderID: pid, Taints: spec.Taints},
		Status: corev1.NodeStatus{Capacity: spec.InstanceType.Capacity, Allocatable: spec.InstanceType.Allocatable(),
			Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue, LastTransitionTime: now, LastHeartbeatTime: now}}},
	}
	if err := e.Apply(ctx, nc); err != nil {
		return nil, nil, err
	}
	if err := e.Apply(ctx, node); err != nil {
		return nil, nil, err
	}
	return nc, node, nil
}

// PodSpec describes one pod to add.
type PodSpec struct {
	Name         string
	Namespace    string
	Owner        string // ReplicaSet (or, with DaemonSet set, DaemonSet) name; empty for a bare pod
	DaemonSet    bool
	Requests     corev1.ResourceList
	Labels       map[string]string
	Annotations  map[string]string
	NodeName     string // empty for a pending pod
	Priority     *int32
	Affinity     *corev1.Affinity
	Tolerations  []corev1.Toleration
	Spread       []corev1.TopologySpreadConstraint
	NodeSelector map[string]string
}

// Pod builds the pod object for a spec: running when bound, otherwise pending and marked unschedulable so the
// provisioner considers it.
func (e *Env) Pod(spec PodSpec) *corev1.Pod {
	ns := lo.CoalesceOrEmpty(spec.Namespace, "default")
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: spec.Name, Namespace: ns, Labels: spec.Labels, Annotations: spec.Annotations,
			UID: types.UID(ns + "/" + spec.Name), CreationTimestamp: metav1.NewTime(e.Clock.Now())},
		Spec: corev1.PodSpec{
			NodeName: spec.NodeName, Priority: spec.Priority, Affinity: spec.Affinity, Tolerations: spec.Tolerations,
			TopologySpreadConstraints: spec.Spread, NodeSelector: spec.NodeSelector,
			Containers: []corev1.Container{{Name: "app", Image: "app", Resources: corev1.ResourceRequirements{Requests: spec.Requests}}},
		},
	}
	if spec.Owner != "" {
		kind := lo.Ternary(spec.DaemonSet, "DaemonSet", "ReplicaSet")
		p.OwnerReferences = []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: kind, Name: spec.Owner,
			UID: types.UID(ns + "/" + kind + "/" + spec.Owner), Controller: lo.ToPtr(true), BlockOwnerDeletion: lo.ToPtr(true)}}
	}
	if spec.NodeName != "" {
		p.Status = corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{
			{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}, {Type: corev1.PodReady, Status: corev1.ConditionTrue}}}
	} else {
		p.Status = corev1.PodStatus{Phase: corev1.PodPending, Conditions: []corev1.PodCondition{
			{Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable}}}
	}
	return p
}

// AddPod creates the pod for a spec.
func (e *Env) AddPod(ctx context.Context, spec PodSpec) (*corev1.Pod, error) {
	p := e.Pod(spec)
	return p, e.Apply(ctx, p)
}

// PDB builds a PodDisruptionBudget over pods with the given labels, with its status already computed.
func PDB(name, namespace string, selector map[string]string, maxUnavailable, healthy, expected int) *policyv1.PodDisruptionBudget {
	mu := intstr.FromInt32(int32(maxUnavailable))
	allowed := lo.Max([]int{0, healthy - (expected - maxUnavailable)})
	return &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: lo.CoalesceOrEmpty(namespace, "default")},
		Spec:       policyv1.PodDisruptionBudgetSpec{MaxUnavailable: &mu, Selector: &metav1.LabelSelector{MatchLabels: selector}},
		Status: policyv1.PodDisruptionBudgetStatus{CurrentHealthy: int32(healthy), DesiredHealthy: int32(expected - maxUnavailable),
			ExpectedPods: int32(expected), DisruptionsAllowed: int32(allowed)},
	}
}

// Requests builds a resource list.
func Requests(cpu, memory string) corev1.ResourceList {
	return corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(memory)}
}
