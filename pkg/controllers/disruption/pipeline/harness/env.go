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

// Package harness hosts real Karpenter components (cluster state, provisioner, disruption queue) on an in-memory
// controller-runtime fake client and a fake clock. It is the substrate shared by the consolidation simulator, the
// pipeline parity harness, and the design spike.
//
// Two things keep it deterministic and faithful:
//   - Every write goes through an interceptor that names GenerateName objects from per-prefix counters (the fake
//     client otherwise appends a time-seeded random suffix) and then runs Karpenter's own state informer reconcilers
//     for the written object, so cluster state tracks the client the way the informers keep it in a real controller,
//     minus watch latency.
//   - Lists come back sorted by namespace and name (the fake tracker sorts them), so iteration order never depends on
//     Go map order on the client side.
package harness

import (
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"sort"
	"sync"
	"time"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	storagev1 "k8s.io/api/storage/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/scheme"
	clienttesting "k8s.io/client-go/testing"
	"k8s.io/client-go/tools/record"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"
	fakecr "sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	_ "sigs.k8s.io/karpenter/pkg/apis" // registers Karpenter types with the client-go scheme
	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/controllers/state/informer"
	"sigs.k8s.io/karpenter/pkg/events"
	"sigs.k8s.io/karpenter/pkg/state/cost"
)

// Epoch is the virtual start time of every environment, so timestamps never depend on the wall clock.
var Epoch = time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)

// Env is a Karpenter control plane over an in-memory API.
type Env struct {
	Client        client.Client
	Clock         *clocktesting.FakeClock
	CloudProvider cloudprovider.CloudProvider
	Cluster       *state.Cluster
	ClusterCost   *cost.ClusterCost
	Provisioner   *provisioning.Provisioner
	Queue         *disruption.Queue
	Recorder      events.Recorder

	nodeInformer      *informer.NodeController
	nodeClaimInformer *informer.NodeClaimController
	nodePoolInformer  *informer.NodePoolController

	mu         sync.Mutex
	nameCounts map[string]int
	pods       *podIndex
	// nodeClaims holds the latest stored copy of each NodeClaim by provider ID, for the status.providerID lists
	// Karpenter issues per pod event (node.GetNodeClaims), which the fake client answers by JSON round-tripping every
	// NodeClaim.
	ncMu       sync.RWMutex
	nodeClaims map[string]*v1.NodeClaim
	ncProvider map[string]string // name -> provider ID
	// OnWrite, when set, is called after every successful write and its state sync, with the object as written.
	// The simulator uses it to observe NodeClaim creation and deletion without polling.
	OnWrite func(ctx context.Context, verb string, obj client.Object)
	// OnEvict, when set, handles Eviction API calls (pods/eviction creates) in place of the fake client, which
	// enforces no PodDisruptionBudgets. It returns a 429 TooManyRequests error to refuse, as the API server does.
	OnEvict func(ctx context.Context, pod types.NamespacedName) error
}

// Options configures an Env.
type Options struct {
	CloudProvider cloudprovider.CloudProvider
	Start         time.Time
}

// New builds an Env. The cloud provider must already hold the instance types runs will use.
func New(ctx context.Context, opts Options) *Env {
	start := lo.Ternary(opts.Start.IsZero(), Epoch, opts.Start)
	e := &Env{
		Clock:         clocktesting.NewFakeClock(start),
		CloudProvider: opts.CloudProvider,
		Recorder:      events.NewRecorder(&record.FakeRecorder{}),
		nameCounts:    map[string]int{},
		pods:          newPodIndex(),
		nodeClaims:    map[string]*v1.NodeClaim{},
		ncProvider:    map[string]string{},
	}
	// A plain object tracker: the default field-managed tracker rebuilds a REST mapper on every write, which
	// dominated the spike's profile, and the simulator never uses server-side apply.
	e.Client = fakecr.NewClientBuilder().
		WithScheme(scheme.Scheme).
		WithObjectTracker(clienttesting.NewObjectTracker(scheme.Scheme, scheme.Codecs.UniversalDecoder())).
		WithStatusSubresource(&v1.NodeClaim{}, &v1.NodePool{}, &corev1.Node{}, &corev1.Pod{}).
		WithIndex(&corev1.Pod{}, "spec.nodeName", func(o client.Object) []string { return []string{o.(*corev1.Pod).Spec.NodeName} }).
		WithIndex(&corev1.Node{}, "spec.providerID", func(o client.Object) []string { return []string{o.(*corev1.Node).Spec.ProviderID} }).
		WithIndex(&storagev1.VolumeAttachment{}, "spec.nodeName", func(o client.Object) []string {
			return []string{o.(*storagev1.VolumeAttachment).Spec.NodeName}
		}).
		WithIndex(&v1.NodeClaim{}, "status.providerID", func(o client.Object) []string { return []string{o.(*v1.NodeClaim).Status.ProviderID} }).
		WithIndex(&v1.NodeClaim{}, "spec.nodeClassRef.group", func(o client.Object) []string { return []string{o.(*v1.NodeClaim).Spec.NodeClassRef.Group} }).
		WithIndex(&v1.NodeClaim{}, "spec.nodeClassRef.kind", func(o client.Object) []string { return []string{o.(*v1.NodeClaim).Spec.NodeClassRef.Kind} }).
		WithIndex(&v1.NodeClaim{}, "spec.nodeClassRef.name", func(o client.Object) []string { return []string{o.(*v1.NodeClaim).Spec.NodeClassRef.Name} }).
		WithIndex(&v1.NodePool{}, "spec.template.spec.nodeClassRef.group", func(o client.Object) []string {
			return []string{o.(*v1.NodePool).Spec.Template.Spec.NodeClassRef.Group}
		}).
		WithIndex(&v1.NodePool{}, "spec.template.spec.nodeClassRef.kind", func(o client.Object) []string {
			return []string{o.(*v1.NodePool).Spec.Template.Spec.NodeClassRef.Kind}
		}).
		WithIndex(&v1.NodePool{}, "spec.template.spec.nodeClassRef.name", func(o client.Object) []string {
			return []string{o.(*v1.NodePool).Spec.Template.Spec.NodeClassRef.Name}
		}).
		WithInterceptorFuncs(e.interceptors()).
		Build()
	e.ClusterCost = cost.NewClusterCost(ctx, e.CloudProvider, e.Client)
	e.Cluster = state.NewCluster(e.Clock, e.Client, e.CloudProvider)
	e.nodeInformer = informer.NewNodeController(e.Client, e.Cluster)
	e.nodeClaimInformer = informer.NewNodeClaimController(e.Client, e.CloudProvider, e.Cluster, e.ClusterCost)
	e.nodePoolInformer = informer.NewNodePoolController(e.Client, e.CloudProvider, e.Cluster, e.ClusterCost)
	e.Provisioner = provisioning.NewProvisioner(e.Client, e.Recorder, e.CloudProvider, e.Cluster, e.Clock, nil, nil, nil)
	e.Queue = disruption.NewQueue(e.Client, e.Recorder, e.Cluster, e.Clock, e.Provisioner)
	return e
}

// NextName returns prefix followed by a per-prefix sequence number, zero padded so names sort in creation order.
func (e *Env) NextName(prefix string) string {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.nameCounts[prefix]++
	return fmt.Sprintf("%s%04d", prefix, e.nameCounts[prefix])
}

// generatedName names a GenerateName object. Karpenter creates NodeClaims from parallel goroutines
// (workqueue.ParallelizeUntil), so a shared counter would hand out names in scheduling order. NodeClaims are named
// from a hash of their spec and labels instead, with a counter only among identical claims, which are
// interchangeable; other kinds are never created concurrently and take the plain counter.
func (e *Env) generatedName(obj client.Object) string {
	nc, ok := obj.(*v1.NodeClaim)
	if !ok {
		return e.NextName(obj.GetGenerateName())
	}
	// Requirements come from iterating a map, so canonicalize their order before hashing.
	spec := nc.Spec.DeepCopy()
	for i := range spec.Requirements {
		sort.Strings(spec.Requirements[i].Values)
	}
	sort.Slice(spec.Requirements, func(i, j int) bool {
		a, b := spec.Requirements[i], spec.Requirements[j]
		if a.Key != b.Key {
			return a.Key < b.Key
		}
		return fmt.Sprint(a.Operator, a.Values) < fmt.Sprint(b.Operator, b.Values)
	})
	raw, _ := json.Marshal(struct {
		Spec   *v1.NodeClaimSpec
		Labels map[string]string
	}{spec, nc.Labels})
	h := fnv.New32a()
	_, _ = h.Write(raw)
	return e.NextName(fmt.Sprintf("%s%08x-", obj.GetGenerateName(), h.Sum32()))
}

//nolint:gocyclo
func (e *Env) interceptors() interceptor.Funcs {
	return interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if ncList, ok := list.(*v1.NodeClaimList); ok {
				lo := &client.ListOptions{}
				lo.ApplyOptions(opts)
				if lo.FieldSelector != nil && lo.LabelSelector == nil && len(lo.FieldSelector.Requirements()) == 1 {
					if pid, ok := lo.FieldSelector.RequiresExactMatch("status.providerID"); ok && pid != "" {
						ncList.Items = e.nodeClaimsByProviderID(pid)
						return nil
					}
				}
			}
			if podList, ok := list.(*corev1.PodList); ok {
				lo := &client.ListOptions{}
				lo.ApplyOptions(opts)
				if lo.FieldSelector != nil && lo.LabelSelector == nil && lo.Limit == 0 && lo.Continue == "" {
					if node, ok := lo.FieldSelector.RequiresExactMatch("spec.nodeName"); ok && len(lo.FieldSelector.Requirements()) == 1 {
						podList.Items = e.pods.list(node, lo.Namespace, lo.UnsafeDisableDeepCopy == nil || !*lo.UnsafeDisableDeepCopy)
						return nil
					}
				}
			}
			return c.List(ctx, list, opts...)
		},
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if obj.GetName() == "" && obj.GetGenerateName() != "" {
				obj.SetName(e.generatedName(obj))
			}
			if ts := obj.GetCreationTimestamp(); ts.IsZero() {
				obj.SetCreationTimestamp(metaNow(e.Clock))
			}
			if err := c.Create(ctx, obj, opts...); err != nil {
				return err
			}
			e.sync(ctx, "create", obj)
			return nil
		},
		Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			if err := c.Update(ctx, obj, opts...); err != nil {
				return err
			}
			e.sync(ctx, "update", obj)
			return nil
		},
		Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if err := c.Patch(ctx, obj, patch, opts...); err != nil {
				return err
			}
			e.sync(ctx, "patch", obj)
			return nil
		},
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if err := c.Delete(ctx, obj, opts...); err != nil {
				return err
			}
			e.sync(ctx, "delete", obj)
			return nil
		},
		SubResourceCreate: func(ctx context.Context, c client.Client, sub string, obj client.Object, subObj client.Object, opts ...client.SubResourceCreateOption) error {
			if sub == "eviction" && e.OnEvict != nil {
				if _, ok := obj.(*corev1.Pod); ok {
					return e.OnEvict(ctx, client.ObjectKeyFromObject(obj))
				}
			}
			return c.SubResource(sub).Create(ctx, obj, subObj, opts...)
		},
		SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if err := c.SubResource(sub).Update(ctx, obj, opts...); err != nil {
				return err
			}
			e.sync(ctx, "status", obj)
			return nil
		},
		SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			if err := c.SubResource(sub).Patch(ctx, obj, patch, opts...); err != nil {
				return err
			}
			e.sync(ctx, "status", obj)
			return nil
		},
	}
}

// sync runs Karpenter's informer reconciler for the written object, then the write hook.
func (e *Env) sync(ctx context.Context, verb string, obj client.Object) {
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: obj.GetName()}}
	switch obj.(type) {
	case *corev1.Node:
		_, _ = e.nodeInformer.Reconcile(ctx, req)
	case *v1.NodeClaim:
		_, _ = e.nodeClaimInformer.Reconcile(ctx, req)
		e.indexNodeClaim(ctx, req.Name)
	case *corev1.Pod:
		// What informer.PodController.Reconcile does, plus keeping the pods-by-node index current, from one Get.
		pod := &corev1.Pod{}
		if err := e.Client.Get(ctx, req.NamespacedName, pod); err != nil {
			e.pods.remove(req.NamespacedName)
			e.Cluster.DeletePod(req.NamespacedName)
		} else {
			e.pods.put(pod)
			_ = e.Cluster.UpdatePod(ctx, pod)
		}
	case *v1.NodePool:
		_, _ = e.nodePoolInformer.Reconcile(ctx, req)
	}
	if e.OnWrite != nil {
		e.OnWrite(ctx, verb, obj)
	}
}

func metaNow(clk *clocktesting.FakeClock) metav1.Time { return metav1.NewTime(clk.Now()) }

func (e *Env) indexNodeClaim(ctx context.Context, name string) {
	nc := &v1.NodeClaim{}
	err := e.Client.Get(ctx, types.NamespacedName{Name: name}, nc)
	e.ncMu.Lock()
	defer e.ncMu.Unlock()
	if old, ok := e.ncProvider[name]; ok {
		delete(e.nodeClaims, old)
		delete(e.ncProvider, name)
	}
	if err != nil || nc.Status.ProviderID == "" {
		return
	}
	e.nodeClaims[nc.Status.ProviderID] = nc
	e.ncProvider[name] = nc.Status.ProviderID
}

func (e *Env) nodeClaimsByProviderID(pid string) []v1.NodeClaim {
	e.ncMu.RLock()
	defer e.ncMu.RUnlock()
	nc, ok := e.nodeClaims[pid]
	if !ok {
		return nil
	}
	return []v1.NodeClaim{*nc.DeepCopy()}
}

// Resync reconciles cluster state for one object without a write, for objects whose state depends on another
// object that arrived later (a pod bound before its node was known, for example).
func (e *Env) Resync(ctx context.Context, obj client.Object) { e.sync(ctx, "resync", obj) }
