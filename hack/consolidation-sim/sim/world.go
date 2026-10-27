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
	"context"
	"fmt"
	"io"
	"math/rand"
	"sort"
	"sync"
	"time"

	"github.com/go-logr/logr"
	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/awslabs/operatorpkg/status"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/cloudprovider/fake"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	"sigs.k8s.io/karpenter/pkg/controllers/nodeclaim/podevents"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/scheduling"
	"sigs.k8s.io/karpenter/pkg/test"
	"sigs.k8s.io/karpenter/pkg/utils/resources"
)

// NodePoolName is the single NodePool every run uses. One pool keeps upstream single-node consolidation
// deterministic: with several, it interleaves pools in Go map order.
const NodePoolName = "default"

type podKey = types.NamespacedName

type simNode struct {
	name       string
	entry      *catalog.Entry
	zone       string
	price      float64
	alloc      pipeline.Resources
	used       pipeline.Resources
	pods       map[podKey]bool
	launchedAt time.Time
	ready      bool
	draining   bool
	removed    bool
	taints     []corev1.Taint
	labels     map[string]string
	initial    bool
}

type podPhase int

const (
	podPending podPhase = iota
	podBound
	podTerminating
)

type simPod struct {
	key       podKey
	owner     string // deployment or job class
	job       bool
	shape     Shape
	req       pipeline.Resources
	node      string
	phase     podPhase
	ready     bool
	createdAt time.Time
	boundAt   time.Time
	lineage   int
	// replaces marks a pod created to replace an evicted one; plan fidelity counts only these.
	replaces            bool
	unschedulableMarked bool
	jobDuration         time.Duration
	dnd                 bool
	evictionCost        float64
	// planned is the node a disruption plan expected this pod's replacement to land on, carried to the replacement.
	planned string
	// steer is a preferred node from a steering executor.
	steer string
}

type deployState struct {
	spec    Deployment
	desired int
	pods    map[podKey]bool // active (pending or bound, not terminating)
	// orphans are lineages of evicted replicas awaiting a replacement, oldest first, with their planned
	// destination.
	orphans []orphan
}

type orphan struct {
	lineage int
	planned string
	steer   string
}

// Sim is one run.
type Sim struct {
	cfg  Config
	ctx  context.Context
	env  *harness.Env
	cat  *catalog.Catalog
	cp   *fake.CloudProvider
	work Workload

	events    eventHeap
	seq       int
	eventsRun int
	start     time.Time
	// disrupting is true while a disruption pass runs, including its validation waits.
	disrupting bool
	stopErr    error

	rngWork, rngSched, rngLat, rngOrder *rand.Rand

	nodes       map[string]*simNode
	pods        map[podKey]*simPod
	deploys     map[string]*deployState
	pdbs        map[string]string // deployment -> PDB name
	podSeq      map[string]int
	lineageSeq  int
	schedQueued bool

	steers       map[podKey]string
	hookMu       sync.Mutex
	hooks        []hook
	taintUpdates map[string][]corev1.Taint

	methods   []disruption.Method
	pipe      *pipeline.Method
	podEvents *podevents.Controller

	batchOpen       bool
	batchStart      time.Time
	lastUnsched     time.Time
	provisionQueued bool
	queueTicking    bool

	m         *collector
	decisions pipeline.Recorder
	cmdSeq    int
}

// New builds a run. decisions receives the decision log.
func New(cfg Config, decisions io.Writer) (*Sim, error) {
	cfg.Defaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	cat, err := catalog.Load(cfg.Catalog)
	if err != nil {
		return nil, err
	}
	ctx := log.IntoContext(options.ToContext(context.Background(), test.Options()), logr.Discard())
	cp := fake.NewCloudProvider()
	cp.InstanceTypes = cat.InstanceTypes()
	env := harness.New(ctx, harness.Options{CloudProvider: cp})
	s := &Sim{
		cfg: cfg, ctx: ctx, env: env, cat: cat, cp: cp,
		rngWork:  rand.New(rand.NewSource(cfg.Seed)),     //nolint:gosec
		rngSched: rand.New(rand.NewSource(cfg.Seed + 1)), //nolint:gosec
		rngLat:   rand.New(rand.NewSource(cfg.Seed + 2)), //nolint:gosec
		rngOrder: rand.New(rand.NewSource(cfg.Seed + 3)), //nolint:gosec
		nodes:    map[string]*simNode{}, pods: map[podKey]*simPod{}, deploys: map[string]*deployState{},
		pdbs: map[string]string{}, podSeq: map[string]int{}, taintUpdates: map[string][]corev1.Taint{}, steers: map[podKey]string{},
	}
	s.start = env.Clock.Now()
	s.podEvents = podevents.NewController(env.Clock, env.Client, cp)
	s.decisions = pipeline.NewJSONLRecorder(decisions)
	s.m = newCollector(s)
	env.OnWrite = s.onWrite
	env.OnEvict = s.onEvictAPI
	if s.work, err = Generate(cfg.Scenario, s.rngWork, catalogAdapter{cat}); err != nil {
		return nil, err
	}
	if err := s.setupNodePool(); err != nil {
		return nil, err
	}
	if err := s.setupMethods(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Sim) setupNodePool() error {
	policy := v1.ConsolidationPolicyWhenEmptyOrUnderutilized
	switch s.cfg.Arm.Name {
	case ArmWhenEmpty, ArmDescheduler:
		policy = v1.ConsolidationPolicyWhenEmpty
	case ArmBalanced:
		policy = v1.ConsolidationPolicyBalanced
	}
	if s.cfg.Arm.Policy != "" {
		policy = v1.ConsolidationPolicy(s.cfg.Arm.Policy)
	}
	np := harness.NodePool(NodePoolName, policy, s.cfg.ConsolidateAfter, s.cfg.Budget)
	np.Spec.Template.Spec.Requirements = []v1.NodeSelectorRequirementWithMinValues{
		{Key: catalog.InstanceFamilyLabelKey, Operator: corev1.NodeSelectorOpIn, Values: s.cat.Families()},
		{Key: v1.CapacityTypeLabelKey, Operator: corev1.NodeSelectorOpIn, Values: []string{v1.CapacityTypeOnDemand}},
	}
	return s.env.Apply(s.ctx, np)
}

// wrapValidator makes a validation wait advance virtual time: the world keeps running (pods arrive, nodes launch,
// other commands progress) while the disruption controller waits, then the real validator checks the command
// against the state that resulted.
type simValidator struct {
	s     *Sim
	inner disruption.Validator
}

func (v simValidator) Validate(ctx context.Context, cmd disruption.Command, delay time.Duration) (disruption.Command, error) {
	if delay > 0 {
		v.s.runUntil(v.s.now().Add(delay))
	}
	return v.inner.Validate(ctx, cmd, 0)
}

func (s *Sim) wrap(v disruption.Validator) disruption.Validator { return simValidator{s: s, inner: v} }

// wait is the pipeline executors' validation wait: virtual time advances while the world keeps running.
func (s *Sim) wait(_ context.Context, d time.Duration) error {
	if d > 0 {
		s.runUntil(s.now().Add(d))
	}
	return nil
}

// steer records a steering request (E2): the pod replacing this one prefers the node, as a webhook adding preferred
// node affinity would arrange.
func (s *Sim) steer(pod pipeline.PodKey, node string) {
	s.steers[podKey{Namespace: pod.Namespace, Name: pod.Name}] = node
}

// observeMove records the plan of a move a pipeline executor is about to run, for plan fidelity. Merges are also
// seen as commands when they start; a shift is seen only here.
func (s *Sim) observeMove(m pipeline.Move) {
	if m.Kind == pipeline.Shift {
		s.m.ObservePlan(m)
	}
}

// orderCandidates gives the disruption methods a seeded random candidate order. The controller's order is Go map
// order (cluster.DeepCopyNodes), effectively random per pass; a seeded permutation keeps that behavior reproducible.
func (s *Sim) orderCandidates(cands []*disruption.Candidate) []*disruption.Candidate {
	sort.Slice(cands, func(i, j int) bool { return cands[i].Name() < cands[j].Name() })
	s.rngOrder.Shuffle(len(cands), func(i, j int) { cands[i], cands[j] = cands[j], cands[i] })
	return cands
}

func (s *Sim) setupMethods() error {
	e := s.env
	if s.cfg.Arm.Name == ArmNone {
		return nil
	}
	c := disruption.MakeConsolidation(e.Clock, e.Cluster, e.Client, e.Provisioner, e.CloudProvider, e.Recorder, e.Queue)
	s.methods = []disruption.Method{disruption.NewEmptiness(c, disruption.WithValidator(s.wrap(disruption.NewEmptinessValidator(c))))}
	switch s.cfg.Arm.Name {
	case ArmWhenEmpty, ArmUnderutil, ArmBalanced:
		s.methods = append(s.methods,
			disruption.NewMultiNodeConsolidation(c, disruption.WithValidator(s.wrap(disruption.NewMultiConsolidationValidator(c)))),
			disruption.NewSingleNodeConsolidation(c, disruption.WithValidator(s.wrap(disruption.NewSingleConsolidationValidator(c)))))
	case ArmPipeline:
		cfg := *s.cfg.Arm.Pipeline
		if cfg.Seed == 0 {
			cfg.Seed = s.cfg.Seed
		}
		m, err := pipeline.NewMethod(e.Clock, e.Client, e.Cluster, e.Provisioner, e.CloudProvider, e.Recorder, e.Queue, e.ClusterCost, cfg,
			pipeline.MethodOptions{
				Upstream: pipeline.UpstreamOptions{WrapValidator: s.wrap, OrderCandidates: s.orderCandidates, Wait: s.wait},
				Recorder: s.decisions, RunID: s.cfg.RunID,
				Observer: s.observeMove,
				Steer:    s.steer,
			})
		if err != nil {
			return fmt.Errorf("building pipeline, %w", err)
		}
		s.pipe = m
		s.methods = append(s.methods, m)
	}
	return nil
}

type catalogAdapter struct{ c *catalog.Catalog }

func (a catalogAdapter) Types() []typeInfo {
	return lo.Map(a.c.Entries, func(e catalog.Entry, _ int) typeInfo {
		alloc := e.InstanceType.Allocatable()
		return typeInfo{Name: e.InstanceType.Name, CPU: alloc.Cpu().MilliValue(), Memory: alloc.Memory().Value() >> 20}
	})
}
func (a catalogAdapter) Zones() []string { return a.c.Zones }

// --- client write hooks ------------------------------------------------------------------------------------------

// onWrite runs on whatever goroutine wrote: Karpenter creates, taints and deletes from parallel goroutines. It only
// buffers what it saw; flushHooks turns the buffer into events in a sorted order on the simulator's goroutine, so
// goroutine scheduling never orders events.
func (s *Sim) onWrite(_ context.Context, verb string, obj client.Object) {
	switch o := obj.(type) {
	case *v1.NodeClaim:
		if verb == "create" || verb == "delete" {
			s.hookMu.Lock()
			s.hooks = append(s.hooks, hook{verb: verb, name: o.Name})
			s.hookMu.Unlock()
		}
	case *corev1.Node:
		if verb != "delete" {
			s.hookMu.Lock()
			s.taintUpdates[o.Name] = append([]corev1.Taint(nil), o.Spec.Taints...)
			s.hookMu.Unlock()
		}
	}
}

type hook struct{ verb, name string }

// flushHooks schedules buffered NodeClaim lifecycle events and applies buffered taint changes, in name order.
func (s *Sim) flushHooks() {
	s.hookMu.Lock()
	hooks, taints := s.hooks, s.taintUpdates
	s.hooks, s.taintUpdates = nil, map[string][]corev1.Taint{}
	s.hookMu.Unlock()
	for _, name := range sortedStrings(lo.Keys(taints)) {
		if n, ok := s.nodes[name]; ok {
			n.taints = taints[name]
		}
	}
	sort.SliceStable(hooks, func(i, j int) bool {
		if hooks[i].verb != hooks[j].verb {
			return hooks[i].verb < hooks[j].verb
		}
		return hooks[i].name < hooks[j].name
	})
	for _, h := range hooks {
		name := h.name
		switch h.verb {
		case "create":
			s.after(s.draw(s.cfg.Latency.Launch), "launch "+name, func() { s.launch(name) })
		case "delete":
			s.at(s.now(), "terminate "+name, func() { s.terminate(name) })
		}
	}
}

func (s *Sim) draw(r Range) time.Duration {
	if r.Max.Duration <= r.Min.Duration {
		return r.Min.Duration
	}
	return r.Min.Duration + time.Duration(s.rngLat.Int63n(int64(r.Max.Duration-r.Min.Duration)))
}

// --- nodes -------------------------------------------------------------------------------------------------------

// launch plays the cloud provider and the NodeClaim lifecycle controller's launch step: it picks the cheapest
// instance type compatible with the claim's requirements and requests (as the fake provider's Create does), a
// seeded zone among that type's cheapest offerings, and marks the claim Launched.
func (s *Sim) launch(name string) {
	nc := &v1.NodeClaim{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: name}, nc); err != nil {
		return
	}
	if !nc.DeletionTimestamp.IsZero() {
		s.finalizeNodeClaim(nc)
		return
	}
	if nc.Status.ProviderID != "" {
		// Already launched: an initial node, created whole.
		return
	}
	reqs := scheduling.NewNodeSelectorRequirementsWithMinValues(nc.Spec.Requirements...)
	type choice struct {
		entry *catalog.Entry
		zones []string
		price float64
	}
	var best *choice
	for i := range s.cat.Entries {
		e := &s.cat.Entries[i]
		it := e.InstanceType
		if !reqs.IsCompatible(it.Requirements, scheduling.AllowUndefinedWellKnownLabels) {
			continue
		}
		offs := it.Offerings.Available().Compatible(reqs)
		if len(offs) == 0 || !resources.Fits(nc.Spec.Resources.Requests, it.Allocatable()) {
			continue
		}
		cheapest := offs.Cheapest().Price
		zones := lo.Uniq(lo.FilterMap(offs, func(o *cloudprovider.Offering, _ int) (string, bool) {
			return o.Zone(), o.Price == cheapest
		}))
		sort.Strings(zones)
		if best == nil || cheapest < best.price || (cheapest == best.price && it.Name < best.entry.InstanceType.Name) {
			best = &choice{entry: e, zones: zones, price: cheapest}
		}
	}
	if best == nil {
		s.fail(fmt.Errorf("no instance type satisfies nodeclaim %s", name))
		return
	}
	zone := best.zones[s.rngLat.Intn(len(best.zones))]
	labels := harness.NodeLabels(NodePoolName, best.entry.InstanceType, zone, v1.CapacityTypeOnDemand, name)
	nc.Labels = lo.Assign(nc.Labels, labels)
	if !lo.Contains(nc.Finalizers, v1.TerminationFinalizer) {
		nc.Finalizers = append(nc.Finalizers, v1.TerminationFinalizer)
	}
	if err := s.env.Client.Update(s.ctx, nc); err != nil {
		s.fail(fmt.Errorf("labeling nodeclaim %s, %w", name, err))
		return
	}
	nc.Status.ProviderID = harness.ProviderID(name)
	nc.Status.Capacity = best.entry.InstanceType.Capacity
	nc.Status.Allocatable = best.entry.InstanceType.Allocatable()
	nc.StatusConditions(status.WithClock(s.env.Clock)).SetTrue(v1.ConditionTypeLaunched)
	if err := s.env.Client.Status().Update(s.ctx, nc); err != nil {
		s.fail(fmt.Errorf("launching nodeclaim %s, %w", name, err))
		return
	}
	n := &simNode{name: name, entry: best.entry, zone: zone, price: best.price, alloc: pipeline.ResourcesFromList(best.entry.InstanceType.Allocatable()),
		pods: map[podKey]bool{}, launchedAt: s.now(), labels: labels}
	s.nodes[name] = n
	s.m.nodeLaunched(n)
	s.after(s.draw(s.cfg.Latency.Registration), "register "+name, func() { s.register(name) })
}

// register plays kubelet and the lifecycle controller's registration and initialization: the Node appears ready and
// the claim becomes Registered and Initialized.
func (s *Sim) register(name string) {
	n, ok := s.nodes[name]
	if !ok || n.removed {
		return
	}
	nc := &v1.NodeClaim{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: name}, nc); err != nil {
		return
	}
	now := metav1.NewTime(s.now())
	labels := lo.Assign(n.labels, map[string]string{v1.NodeRegisteredLabelKey: "true", v1.NodeInitializedLabelKey: "true"})
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels, CreationTimestamp: now},
		Spec:       corev1.NodeSpec{ProviderID: nc.Status.ProviderID},
		Status: corev1.NodeStatus{Capacity: n.entry.InstanceType.Capacity, Allocatable: n.entry.InstanceType.Allocatable(),
			Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue, LastTransitionTime: now, LastHeartbeatTime: now}}},
	}
	if !nc.DeletionTimestamp.IsZero() {
		return
	}
	if err := s.env.Apply(s.ctx, node); err != nil {
		s.fail(fmt.Errorf("registering node %s, %w", name, err))
		return
	}
	nc.Labels = lo.Assign(nc.Labels, map[string]string{v1.NodeRegisteredLabelKey: "true", v1.NodeInitializedLabelKey: "true"})
	if err := s.env.Client.Update(s.ctx, nc); err != nil {
		s.fail(err)
		return
	}
	nc.Status.NodeName = name
	nc.StatusConditions(status.WithClock(s.env.Clock)).SetTrue(v1.ConditionTypeRegistered)
	nc.StatusConditions(status.WithClock(s.env.Clock)).SetTrue(v1.ConditionTypeInitialized)
	if err := s.env.Client.Status().Update(s.ctx, nc); err != nil {
		s.fail(err)
		return
	}
	n.ready = true
	s.kickScheduler()
}

// addInitialNode creates a node that exists at t = 0, for fragmented starts.
func (s *Sim) addInitialNode(name string, e *catalog.Entry, zone string) error {
	if _, _, err := s.env.AddNode(s.ctx, harness.NodeSpec{Name: name, NodePool: NodePoolName, InstanceType: e.InstanceType, Zone: zone,
		CapacityType: v1.CapacityTypeOnDemand}); err != nil {
		return err
	}
	price := e.Price
	for _, o := range e.InstanceType.Offerings {
		if o.Zone() == zone {
			price = o.Price
		}
	}
	n := &simNode{name: name, entry: e, zone: zone, price: price, alloc: pipeline.ResourcesFromList(e.InstanceType.Allocatable()),
		pods: map[podKey]bool{}, launchedAt: s.now(), ready: true, initial: true,
		labels: harness.NodeLabels(NodePoolName, e.InstanceType, zone, v1.CapacityTypeOnDemand, name)}
	s.nodes[name] = n
	s.m.nodeLaunched(n)
	return nil
}

// terminate plays the termination controller: drain with PDB-respecting evictions, then delete the Node and release
// the claim's finalizer. A claim deleted before it registered is released at once.
func (s *Sim) terminate(name string) {
	nc := &v1.NodeClaim{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: name}, nc); err != nil {
		if n, ok := s.nodes[name]; ok && !n.removed {
			s.removeNode(n)
		}
		return
	}
	n, ok := s.nodes[name]
	if !ok || !n.ready {
		s.finalizeNodeClaim(nc)
		if ok {
			s.removeNode(n)
		}
		return
	}
	if n.draining {
		return
	}
	n.draining = true
	s.m.nodeDrainStarted(n)
	s.drain(name)
}

func (s *Sim) drain(name string) {
	n, ok := s.nodes[name]
	if !ok || n.removed {
		return
	}
	remaining := 0
	for _, key := range sortedKeys(n.pods) {
		p := s.pods[key]
		if p.phase == podTerminating {
			remaining++
			continue
		}
		if p.dnd && !p.job {
			// Graceful termination waits for do-not-disrupt pods; consolidation never picks such nodes.
			remaining++
			continue
		}
		if err := s.evict(p, "drain"); err != nil {
			remaining++
			continue
		}
		remaining++
	}
	if remaining > 0 {
		s.after(time.Second, "drain "+name, func() { s.drain(name) })
		return
	}
	s.finish(n)
}

func (s *Sim) finish(n *simNode) {
	node := &corev1.Node{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: n.name}, node); err == nil {
		_ = s.env.Client.Delete(s.ctx, node)
	}
	nc := &v1.NodeClaim{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: n.name}, nc); err == nil {
		s.finalizeNodeClaim(nc)
	}
	s.removeNode(n)
}

func (s *Sim) finalizeNodeClaim(nc *v1.NodeClaim) {
	if nc.DeletionTimestamp.IsZero() {
		if err := s.env.Client.Delete(s.ctx, nc); err != nil && !apierrors.IsNotFound(err) {
			s.fail(err)
			return
		}
		if err := s.env.Client.Get(s.ctx, client.ObjectKeyFromObject(nc), nc); err != nil {
			return
		}
	}
	nc.Finalizers = lo.Without(nc.Finalizers, v1.TerminationFinalizer)
	if err := s.env.Client.Update(s.ctx, nc); err != nil && !apierrors.IsNotFound(err) {
		s.fail(fmt.Errorf("releasing nodeclaim %s, %w", nc.Name, err))
	}
}

func (s *Sim) removeNode(n *simNode) {
	if n.removed {
		return
	}
	n.removed = true
	s.m.nodeRemoved(n)
	delete(s.nodes, n.name)
}

func sortedKeys(m map[podKey]bool) []podKey {
	keys := make([]podKey, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Namespace != keys[j].Namespace {
			return keys[i].Namespace < keys[j].Namespace
		}
		return keys[i].Name < keys[j].Name
	})
	return keys
}

func (s *Sim) fail(err error) {
	if s.stopErr == nil {
		s.stopErr = err
	}
}
