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

package executors_test

import (
	"context"
	"fmt"
	"math/rand"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr"
	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/cloudprovider/fake"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness/catalog"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/executors"
	"sigs.k8s.io/karpenter/pkg/operator/options"
	"sigs.k8s.io/karpenter/pkg/test"
)

// Executors run against a live cluster, so these tests host real Karpenter state on the harness and plan moves by
// hand. The shared conformance suite has no executor check (it leaves executors to the simulator), so
// TestExecutorContract runs the executor contract here over seeded generated states, for every executor this
// package registers.

var catalogTypes = sync.OnceValues(func() ([]*cloudprovider.InstanceType, error) {
	cat, err := catalog.Load(catalog.Options{MaxCPU: 16})
	if err != nil {
		return nil, err
	}
	return cat.InstanceTypes(), nil
})

// fixture is one live cluster with an event log of everything the executor did through the cluster's seams.
type fixture struct {
	t   *testing.T
	ctx context.Context
	h   *harness.Env
	up  *pipeline.Upstream
	env *pipeline.Env
	its []*cloudprovider.InstanceType
	// events logs waits, validations (with the delay asked for), steering requests and evictions, in order.
	events   []string
	refuse   sets.Set[string]
	observed []pipeline.Move
	snap     *pipeline.State
}

func newFixture(t *testing.T, withPool bool) *fixture {
	t.Helper()
	its, err := catalogTypes()
	if err != nil {
		t.Fatal(err)
	}
	ctx := log.IntoContext(options.ToContext(context.Background(), test.Options()), logr.Discard())
	cp := fake.NewCloudProvider()
	cp.InstanceTypes = its
	h := harness.New(ctx, harness.Options{CloudProvider: cp})
	f := &fixture{t: t, ctx: ctx, h: h, its: its, refuse: sets.New[string]()}
	h.OnEvict = func(_ context.Context, key types.NamespacedName) error {
		if f.refuse.Has(key.Name) {
			f.record("refuse %s", key)
			return apierrors.NewTooManyRequests("Cannot evict pod as it would violate the pod's disruption budget.", 10)
		}
		f.record("evict %s", key)
		return nil
	}
	f.up = pipeline.NewUpstream(h.Clock, h.Client, h.Cluster, h.Provisioner, h.CloudProvider, h.Recorder, h.Queue, pipeline.UpstreamOptions{
		WrapValidator: func(v disruption.Validator) disruption.Validator { return recordingValidator{f: f, inner: v} },
		Wait: func(_ context.Context, d time.Duration) error {
			f.record("wait %s", d)
			h.Clock.Step(d)
			return nil
		},
	})
	f.env = &pipeline.Env{
		Clock: h.Clock, Upstream: f.up, Recorder: pipeline.NopRecorder{},
		Feasibility: &pipeline.UpstreamFeasibility{FastChecker: &pipeline.FastChecker{}, Up: f.up},
		Observer:    func(m pipeline.Move) { f.observed = append(f.observed, m) },
		Steer:       func(pod pipeline.PodKey, node string) { f.record("steer %s %s", pod, lo.CoalesceOrEmpty(node, "-")) },
	}
	if withPool {
		if err := h.Apply(ctx, harness.NodePool("pool", v1.ConsolidationPolicyWhenEmptyOrUnderutilized, "0s")); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *fixture) record(format string, args ...any) {
	f.events = append(f.events, fmt.Sprintf(format, args...))
}

// recordingValidator logs the delay each validation asks for, then validates without waiting: the state is static
// between plan and validation unless a test changes it.
type recordingValidator struct {
	f     *fixture
	inner disruption.Validator
}

func (v recordingValidator) Validate(ctx context.Context, cmd disruption.Command, d time.Duration) (disruption.Command, error) {
	v.f.record("validate %s", d)
	return v.inner.Validate(ctx, cmd, 0)
}

func (f *fixture) instanceType(name string) *cloudprovider.InstanceType {
	f.t.Helper()
	it, ok := lo.Find(f.its, func(it *cloudprovider.InstanceType) bool { return it.Name == name })
	if !ok {
		f.t.Fatalf("instance type %s not in the catalog", name)
	}
	return it
}

func (f *fixture) node(name, instanceType string, opts ...func(*harness.NodeSpec)) {
	f.t.Helper()
	it := f.instanceType(instanceType)
	off := it.Offerings.Available()[0]
	spec := harness.NodeSpec{Name: name, NodePool: "pool", InstanceType: it, Zone: off.Requirements.Get(corev1.LabelTopologyZone).Any(), CapacityType: off.CapacityType()}
	for _, o := range opts {
		o(&spec)
	}
	if _, _, err := f.h.AddNode(f.ctx, spec); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) pod(name, node, cpu string) {
	f.t.Helper()
	if _, err := f.h.AddPod(f.ctx, harness.PodSpec{Name: name, Owner: "web", Requests: harness.Requests(cpu, "256Mi"), NodeName: node,
		Labels: map[string]string{"app": "web"}}); err != nil {
		f.t.Fatal(err)
	}
}

// cycle sets up a disruption cycle the way the controller does and snapshots it for planning.
func (f *fixture) cycle() *pipeline.State {
	f.t.Helper()
	f.h.Cluster.SetSynced(true)
	cands, totals, err := f.h.SortedCandidates(f.ctx, f.up.Single.ShouldDisrupt)
	if err != nil {
		f.t.Fatal(err)
	}
	budgets, err := disruption.BuildDisruptionBudgetMapping(f.ctx, f.h.Cluster, f.h.Clock, f.h.Client, f.h.CloudProvider, f.h.Recorder, v1.DisruptionReasonUnderutilized)
	if err != nil {
		f.t.Fatal(err)
	}
	f.up.Multi.SetNodePoolTotals(totals)
	f.up.Single.SetNodePoolTotals(totals)
	f.up.SetCycle(cands, totals, budgets)
	if f.snap, err = pipeline.BuildSnapshot(f.ctx, f.up); err != nil {
		f.t.Fatal(err)
	}
	return f.snap
}

func bind(pod, from, to string) pipeline.Binding {
	return pipeline.Binding{Pod: pipeline.PodKey{Namespace: "default", Name: pod}, From: from, Target: pipeline.ExistingTarget(to)}
}

func move(id string, kind pipeline.MoveKind, bindings ...pipeline.Binding) pipeline.Scored {
	m := pipeline.Move{ID: id, Kind: kind, Bindings: bindings, Generator: "test"}
	m.Sources = sets.List(sets.New(lo.Map(bindings, func(b pipeline.Binding, _ int) string { return b.From })...))
	if kind == pipeline.Merge {
		m.Remove = append([]string(nil), m.Sources...)
	}
	m.SortBindings()
	return pipeline.Scored{Move: m}
}

// plan builds a plan on the fixture's snapshot, first checking each move is one a generator could have emitted.
func (f *fixture) plan(moves ...pipeline.Scored) pipeline.Plan {
	f.t.Helper()
	for _, m := range moves {
		if err := (&pipeline.FastChecker{}).Fast(f.snap, m.Move); err != nil {
			f.t.Fatalf("test plan move %s fails Fast feasibility: %v", m.ID, err)
		}
	}
	return pipeline.Plan{Moves: moves, Planner: "test", Snapshot: f.snap}
}

func (f *fixture) execute(name string, params pipeline.Params, p pipeline.Plan) pipeline.Report {
	f.t.Helper()
	ex, err := pipeline.NewExecutor(f.env, pipeline.StageConfig{Name: name, Params: params})
	if err != nil {
		f.t.Fatal(err)
	}
	r, err := ex.Execute(f.ctx, p)
	if err != nil {
		f.t.Fatal(err)
	}
	return r
}

// mark returns whether a node carries the shift-source taint and its since annotation.
func (f *fixture) mark(name string) (bool, string) {
	f.t.Helper()
	n := &corev1.Node{}
	if err := f.h.Client.Get(f.ctx, client.ObjectKey{Name: name}, n); err != nil {
		f.t.Fatal(err)
	}
	tainted := lo.ContainsBy(n.Spec.Taints, func(t corev1.Taint) bool {
		return t.Key == pipeline.ShiftSourceTaintKey && t.Effect == corev1.TaintEffectPreferNoSchedule
	})
	return tainted, n.Annotations[executors.ShiftSourceSinceAnnotationKey]
}

func (f *fixture) annotate(obj client.Object, key string) {
	f.t.Helper()
	if err := f.h.Client.Get(f.ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
		f.t.Fatal(err)
	}
	obj.SetAnnotations(lo.Assign(obj.GetAnnotations(), map[string]string{key: "true"}))
	if err := f.h.Client.Update(f.ctx, obj); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) doNotDisruptPod(name string) {
	f.annotate(&corev1.Pod{ObjectMeta: objectMeta("default", name)}, v1.DoNotDisruptAnnotationKey)
}

func (f *fixture) expectEvents(want ...string) {
	f.t.Helper()
	if !reflect.DeepEqual(f.events, want) {
		f.t.Errorf("events\n  got  %q\n  want %q", f.events, want)
	}
}

func expectReport(t *testing.T, r pipeline.Report, executed, aborted []string, reasons map[string]string, pods int) {
	t.Helper()
	if !reflect.DeepEqual(r.Executed, executed) || !reflect.DeepEqual(r.Aborted, aborted) || r.PodsEvicted != pods {
		t.Errorf("report executed=%v aborted=%v pods=%d, want executed=%v aborted=%v pods=%d", r.Executed, r.Aborted, r.PodsEvicted, executed, aborted, pods)
	}
	for id, want := range reasons {
		got := r.Reasons[id]
		if before, ok := strings.CutSuffix(want, "*"); ok {
			if !strings.HasPrefix(got, before) {
				t.Errorf("reason for %s %q, want prefix %q", id, got, strings.TrimSuffix(want, "*"))
			}
		} else if got != want {
			t.Errorf("reason for %s %q, want %q", id, got, want)
		}
	}
	for id, got := range r.Reasons {
		if _, ok := reasons[id]; !ok {
			t.Errorf("unexpected reason for %s: %q", id, got)
		}
	}
}

// threeNodes is the common state: node-a holds a1..a3, node-b and node-c one pod each, all with room to spare.
func threeNodes(t *testing.T) *fixture {
	f := newFixture(t, true)
	for _, n := range []string{"node-a", "node-b", "node-c"} {
		f.node(n, "m7i.xlarge")
	}
	for _, p := range []string{"a1", "a2", "a3"} {
		f.pod(p, "node-a", "250m")
	}
	f.pod("b1", "node-b", "250m")
	f.pod("c1", "node-c", "250m")
	f.cycle()
	return f
}

// since is the stamp a mark placed d after the harness epoch carries.
func since(d time.Duration) string { return harness.Epoch.Add(d).UTC().Format(time.RFC3339) }

func objectMeta(namespace, name string) metav1.ObjectMeta {
	return metav1.ObjectMeta{Namespace: namespace, Name: name}
}

//nolint:gocyclo
func TestShiftEvictEvictsPlannedPodsAndTaintsSource(t *testing.T) {
	f := threeNodes(t)
	r := f.execute(executors.ShiftEvictName, nil, f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b"))))
	expectReport(t, r, []string{"s1"}, nil, nil, 2)
	f.expectEvents("wait 15s", "evict default/a1", "evict default/a2")
	if tainted, at := f.mark("node-a"); !tainted || at != since(15*time.Second) {
		t.Errorf("node-a tainted=%v since=%q, want the shift-source taint since %s", tainted, at, since(15*time.Second))
	}
	for _, n := range []string{"node-b", "node-c"} {
		if tainted, at := f.mark(n); tainted || at != "" {
			t.Errorf("%s tainted=%v since=%q, want untouched", n, tainted, at)
		}
	}
	if len(f.observed) != 1 || f.observed[0].ID != "s1" || len(f.observed[0].Bindings) != 2 {
		t.Errorf("observer saw %v, want s1 with its two bindings", f.observed)
	}

	// Re-tainting is idempotent: the stamp keeps the time the node first became a source.
	f.events = nil
	f.execute(executors.ShiftEvictName, nil, f.plan(move("s2", pipeline.Shift, bind("a3", "node-a", "node-c"))))
	if tainted, at := f.mark("node-a"); !tainted || at != since(15*time.Second) {
		t.Errorf("node-a tainted=%v since=%q after a second shift, want the original stamp %s", tainted, at, since(15*time.Second))
	}

	// Without validation there is no wait; without tainting the source stays untouched.
	f.events = nil
	f.execute(executors.ShiftEvictName, pipeline.Params{"validate": false, "taint": false}, f.plan(move("s3", pipeline.Shift, bind("b1", "node-b", "node-c"))))
	f.expectEvents("evict default/b1")
	if tainted, _ := f.mark("node-b"); tainted {
		t.Errorf("node-b tainted with taint: false")
	}
}

func TestShiftEvictStopsAtRefusedEviction(t *testing.T) {
	f := threeNodes(t)
	f.refuse.Insert("a2")
	r := f.execute(executors.ShiftEvictName, nil, f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b"), bind("a3", "node-a", "node-b"))))
	expectReport(t, r, []string{"s1"}, nil, map[string]string{"s1": executors.ReasonPDB}, 1)
	f.expectEvents("wait 15s", "evict default/a1", "refuse default/a2")
	if tainted, _ := f.mark("node-a"); !tainted {
		t.Errorf("node-a lost its taint although a pod left it")
	}

	// Refused at the first pod: nothing moved, so the move is aborted and leaves no taint.
	g := threeNodes(t)
	g.refuse.Insert("a1")
	r = g.execute(executors.ShiftEvictName, nil, g.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b"))))
	expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonPDB}, 0)
	g.expectEvents("wait 15s", "refuse default/a1")
	if tainted, at := g.mark("node-a"); tainted || at != "" {
		t.Errorf("node-a tainted=%v since=%q after a refused move, want untouched", tainted, at)
	}
}

func TestShiftEvictCountsBudgetsAcrossMoves(t *testing.T) {
	f := newFixture(t, true)
	for _, n := range []string{"node-a", "node-b", "node-c"} {
		f.node(n, "m7i.xlarge")
	}
	f.pod("a1", "node-a", "250m")
	f.pod("b1", "node-b", "250m")
	f.pod("c1", "node-c", "250m")
	// One disruption allowed over all three replicas: each move fits the budget alone, not both together.
	if err := f.h.Apply(f.ctx, harness.PDB("web", "default", map[string]string{"app": "web"}, 1, 3, 3)); err != nil {
		t.Fatal(err)
	}
	f.cycle()
	r := f.execute(executors.ShiftEvictName, nil, f.plan(
		move("s1", pipeline.Shift, bind("a1", "node-a", "node-c")),
		move("s2", pipeline.Shift, bind("b1", "node-b", "node-c"))))
	expectReport(t, r, []string{"s1"}, []string{"s2"}, map[string]string{"s2": executors.ReasonPDB}, 1)
	f.expectEvents("wait 15s", "evict default/a1")
}

func TestShiftEvictDefersMergeFromDrainingSource(t *testing.T) {
	f := threeNodes(t)
	r := f.execute(executors.ShiftEvictName, nil, f.plan(
		move("s1", pipeline.Shift, bind("a1", "node-a", "node-b")),
		move("m1", pipeline.Merge, bind("a1", "node-a", "node-c"), bind("a2", "node-a", "node-c"), bind("a3", "node-a", "node-c"))))
	expectReport(t, r, []string{"s1"}, []string{"m1"}, map[string]string{"m1": executors.ReasonSourceDraining}, 1)
	if len(r.Commands) != 0 {
		t.Errorf("deferred merge returned %d commands", len(r.Commands))
	}
	f.expectEvents("wait 15s", "evict default/a1")
}

func TestShiftEvictReturnsValidatedMerges(t *testing.T) {
	// The merge carries no upstream command, so the executor confirms it with Full, then validates it without a
	// second wait, since the shift's wait already happened.
	f := threeNodes(t)
	r := f.execute(executors.ShiftEvictName, nil, f.plan(
		move("s1", pipeline.Shift, bind("a1", "node-a", "node-b")),
		move("m1", pipeline.Merge, bind("c1", "node-c", "node-b"))))
	expectReport(t, r, []string{"s1", "m1"}, nil, nil, 2)
	f.expectEvents("wait 15s", "evict default/a1", "validate 0s")
	expectCommand(t, r, disruption.DeleteDecision, "node-c")

	// A merge alone validates after the full delay, as upstream-queue does.
	g := threeNodes(t)
	r = g.execute(executors.ShiftEvictName, nil, g.plan(move("m1", pipeline.Merge, bind("c1", "node-c", "node-b"))))
	expectReport(t, r, []string{"m1"}, nil, nil, 1)
	g.expectEvents("validate 15s")
	expectCommand(t, r, disruption.DeleteDecision, "node-c")
}

func expectCommand(t *testing.T, r pipeline.Report, decision disruption.Decision, candidates ...string) {
	t.Helper()
	if len(r.Commands) != 1 {
		t.Fatalf("got %d commands, want 1", len(r.Commands))
	}
	cmd, ok := r.Commands[0].(disruption.Command)
	if !ok {
		t.Fatalf("command is %T, want disruption.Command", r.Commands[0])
	}
	got := lo.Map(cmd.Candidates, func(c *disruption.Candidate, _ int) string { return c.Name() })
	if cmd.Decision() != decision || !reflect.DeepEqual(got, candidates) {
		t.Errorf("command %s of %v, want %s of %v", cmd.Decision(), got, decision, candidates)
	}
}

func TestShiftEvictAbortsOnChurn(t *testing.T) {
	plan := func(f *fixture) pipeline.Plan {
		return f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b")))
	}
	// A planned pod is gone by execution.
	f := threeNodes(t)
	p := plan(f)
	if err := f.h.Client.Delete(f.ctx, &corev1.Pod{ObjectMeta: objectMeta("default", "a2")}); err != nil {
		t.Fatal(err)
	}
	r := f.execute(executors.ShiftEvictName, nil, p)
	expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonChurnPrefix + "*"}, 0)
	f.expectEvents("wait 15s")
	if tainted, _ := f.mark("node-a"); tainted {
		t.Errorf("node-a tainted by an aborted shift")
	}

	// The destination filled up during the validation wait.
	g := threeNodes(t)
	p = plan(g)
	g.up.Wait = func(_ context.Context, d time.Duration) error {
		g.record("wait %s", d)
		g.pod("big", "node-b", "3400m")
		return nil
	}
	r = g.execute(executors.ShiftEvictName, nil, p)
	expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonChurnPrefix + "insufficient capacity*"}, 0)
	g.expectEvents("wait 15s")

	// Without validation the hard constraints still hold: the vanished pod is skipped, the rest move.
	h := threeNodes(t)
	p = plan(h)
	if err := h.h.Client.Delete(h.ctx, &corev1.Pod{ObjectMeta: objectMeta("default", "a2")}); err != nil {
		t.Fatal(err)
	}
	r = h.execute(executors.ShiftEvictName, pipeline.Params{"validate": false}, p)
	expectReport(t, r, []string{"s1"}, nil, map[string]string{"s1": executors.ReasonPodsLeft}, 1)
	h.expectEvents("evict default/a1")
}

//nolint:gocyclo
func TestShiftEvictExpiresTaints(t *testing.T) {
	f := newFixture(t, true)
	for _, n := range []string{"node-a", "node-b", "node-c", "node-d", "node-e", "node-f"} {
		f.node(n, "m7i.xlarge")
		f.pod("p-"+n, n, "250m")
	}
	// node-d carries the taint without a stamp, as if a stamp were lost: it must not stay forever.
	d := &corev1.Node{}
	if err := f.h.Client.Get(f.ctx, client.ObjectKey{Name: "node-d"}, d); err != nil {
		t.Fatal(err)
	}
	d.Spec.Taints = append(d.Spec.Taints, corev1.Taint{Key: pipeline.ShiftSourceTaintKey, Effect: corev1.TaintEffectPreferNoSchedule})
	if err := f.h.Client.Update(f.ctx, d); err != nil {
		t.Fatal(err)
	}
	f.cycle()
	// t=15s: node-a and node-b become sources (node-d's unstamped taint lapses at the start).
	f.execute(executors.ShiftEvictName, nil, f.plan(
		move("s1", pipeline.Shift, bind("p-node-a", "node-a", "node-c")),
		move("s2", pipeline.Shift, bind("p-node-b", "node-b", "node-e"))))
	if tainted, _ := f.mark("node-d"); tainted {
		t.Errorf("node-d kept a taint without a stamp")
	}
	// t=6m30s: node-f becomes a source.
	f.h.Clock.Step(6 * time.Minute)
	f.execute(executors.ShiftEvictName, nil, f.plan(move("s3", pipeline.Shift, bind("p-node-f", "node-f", "node-c"))))
	// t=11m30s: node-a and node-b are past the default 10m TTL, node-f is not. A stamp that does not parse lapses too.
	f.h.Clock.Step(5 * time.Minute)
	f.annotate(&corev1.Node{ObjectMeta: objectMeta("", "node-e")}, executors.ShiftSourceSinceAnnotationKey)
	f.events = nil
	f.h.OnWrite = func(_ context.Context, verb string, obj client.Object) {
		if _, ok := obj.(*corev1.Node); ok && verb == "patch" {
			f.record("patch %s", obj.GetName())
		}
	}
	f.execute(executors.ShiftEvictName, nil, pipeline.Plan{})
	f.expectEvents("patch node-a", "patch node-b", "patch node-e")
	for _, n := range []string{"node-a", "node-b", "node-e"} {
		if tainted, at := f.mark(n); tainted || at != "" {
			t.Errorf("%s tainted=%v since=%q after its TTL, want cleared", n, tainted, at)
		}
	}
	if tainted, at := f.mark("node-f"); !tainted || at != since(6*time.Minute+30*time.Second) {
		t.Errorf("node-f tainted=%v since=%q, want kept within its TTL", tainted, at)
	}

	// A shorter TTL lapses sooner.
	f.h.Clock.Step(2 * time.Minute)
	f.execute(executors.ShiftEvictName, pipeline.Params{"taintTTL": "1m"}, pipeline.Plan{})
	if tainted, _ := f.mark("node-f"); tainted {
		t.Errorf("node-f kept its taint past a 1m TTL")
	}
}

func TestExecutorsNeverEvictDoNotDisrupt(t *testing.T) {
	for _, name := range []string{executors.ShiftEvictName, executors.SteeredName, executors.MakeBeforeBreakName} {
		for _, validate := range []bool{true, false} {
			params := pipeline.Params{"validate": validate}
			t.Run(fmt.Sprintf("%s/validate=%v", name, validate), func(t *testing.T) {
				// A planned pod turned do-not-disrupt after planning: it stays, the rest of the shift goes.
				f := threeNodes(t)
				p := f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b")))
				f.doNotDisruptPod("a1")
				r := f.execute(name, params, p)
				expectReport(t, r, []string{"s1"}, nil, map[string]string{"s1": executors.ReasonDoNotDisrupt}, 1)
				if evicted := evictions(f.events); !reflect.DeepEqual(evicted, []string{"default/a2"}) {
					t.Errorf("evicted %v, want only default/a2", evicted)
				}

				// A plan that binds nothing but a do-not-disrupt pod moves nothing and taints nothing.
				g := threeNodes(t)
				p = g.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b")))
				g.doNotDisruptPod("a1")
				r = g.execute(name, params, p)
				expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonDoNotDisrupt}, 0)
				if evicted := evictions(g.events); len(evicted) > 0 {
					t.Errorf("evicted %v from a do-not-disrupt plan", evicted)
				}
				if tainted, _ := g.mark("node-a"); tainted {
					t.Errorf("node-a tainted by a shift that moved nothing")
				}

				// No pod leaves a do-not-disrupt node.
				h := threeNodes(t)
				p = h.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-c")))
				h.annotate(&corev1.Node{ObjectMeta: objectMeta("", "node-a")}, v1.DoNotDisruptAnnotationKey)
				r = h.execute(name, params, p)
				expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonDoNotDisrupt}, 0)
				if evicted := evictions(h.events); len(evicted) > 0 {
					t.Errorf("evicted %v from a do-not-disrupt node", evicted)
				}
			})
		}
	}
}

func evictions(events []string) []string {
	return lo.FilterMap(events, func(e string, _ int) (string, bool) { return strings.CutPrefix(e, "evict ") })
}

func TestSteeredRequestsPlannedNodes(t *testing.T) {
	plan := func(f *fixture) pipeline.Plan {
		return f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-c")))
	}
	f := threeNodes(t)
	r := f.execute(executors.SteeredName, nil, plan(f))
	expectReport(t, r, []string{"s1"}, nil, nil, 2)
	f.expectEvents("wait 15s", "steer default/a1 node-b", "evict default/a1", "steer default/a2 node-c", "evict default/a2")

	// most-allocated leaves placement to the scheduler's profile.
	g := threeNodes(t)
	g.execute(executors.SteeredName, pipeline.Params{"mode": executors.ModeMostAllocated}, plan(g))
	g.expectEvents("wait 15s", "evict default/a1", "evict default/a2")

	// A refused eviction withdraws its request.
	h := threeNodes(t)
	h.refuse.Insert("a2")
	h.execute(executors.SteeredName, nil, plan(h))
	h.expectEvents("wait 15s", "steer default/a1 node-b", "evict default/a1", "steer default/a2 node-c", "refuse default/a2", "steer default/a2 -")

	// shift-evict never steers.
	k := threeNodes(t)
	k.execute(executors.ShiftEvictName, nil, plan(k))
	k.expectEvents("wait 15s", "evict default/a1", "evict default/a2")
}

func TestMakeBeforeBreakChecksDestinations(t *testing.T) {
	// The destination starts deleting after validation, just before the evictions.
	f := threeNodes(t)
	f.env.Observer = func(pipeline.Move) { f.h.Cluster.MarkForDeletion(harness.ProviderID("node-b")) }
	r := f.execute(executors.MakeBeforeBreakName, nil, f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"))))
	expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonDestinationNotReady}, 0)
	f.expectEvents("wait 15s")
	if tainted, _ := f.mark("node-a"); tainted {
		t.Errorf("node-a tainted by a shift whose destination was not ready")
	}

	// An uninitialized destination is not ready either (validation off, so only E3's own check can catch it).
	g := newFixture(t, true)
	g.node("node-a", "m7i.xlarge")
	g.node("node-u", "m7i.xlarge", func(s *harness.NodeSpec) { s.Uninitialized = true })
	g.pod("a1", "node-a", "250m")
	g.cycle()
	p := pipeline.Plan{Moves: []pipeline.Scored{move("s1", pipeline.Shift, bind("a1", "node-a", "node-u"))}, Snapshot: g.snap}
	r = g.execute(executors.MakeBeforeBreakName, pipeline.Params{"validate": false}, p)
	expectReport(t, r, nil, []string{"s1"}, map[string]string{"s1": executors.ReasonDestinationNotReady}, 0)

	// A ready destination is nominated, then the pods are steered to it and evicted.
	h := threeNodes(t)
	r = h.execute(executors.MakeBeforeBreakName, nil, h.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-b"))))
	expectReport(t, r, []string{"s1"}, nil, nil, 1)
	h.expectEvents("wait 15s", "steer default/a1 node-b", "evict default/a1")
	if !h.h.Cluster.IsNodeNominated(harness.ProviderID("node-b")) {
		t.Errorf("node-b not nominated")
	}
	if h.h.Cluster.IsNodeNominated(harness.ProviderID("node-c")) {
		t.Errorf("node-c nominated although nothing moves to it")
	}
}

func TestMakeBeforeBreakDefersShiftsBehindLaunch(t *testing.T) {
	setup := func(t *testing.T) (*fixture, pipeline.Plan) {
		f := newFixture(t, true)
		f.node("node-s", "m7i.xlarge")
		f.node("node-t", "m7i.xlarge")
		f.node("node-x", "c7i.4xlarge")
		f.pod("s1", "node-s", "250m")
		f.pod("t1", "node-t", "250m")
		// x1 fits neither other node, but a cheaper type: single-node consolidation replaces node-x.
		f.pod("x1", "node-x", "6")
		snap := f.cycle()
		cand, ok := f.up.Candidate("node-x")
		if !ok {
			t.Fatal("node-x is not a candidate")
		}
		cmd, err := f.up.Single.ComputeConsolidation(f.ctx, cand)
		if err != nil {
			t.Fatal(err)
		}
		if cmd.Decision() != disruption.ReplaceDecision {
			t.Fatalf("consolidating node-x decided %s, want a replace", cmd.Decision())
		}
		replace := pipeline.Scored{Move: pipeline.MoveFromCommand(snap, cmd, "m1", "upstream-single", false)}
		return f, f.plan(replace, move("sh1", pipeline.Shift, bind("s1", "node-s", "node-t")))
	}
	f, p := setup(t)
	r := f.execute(executors.MakeBeforeBreakName, nil, p)
	expectReport(t, r, []string{"m1"}, []string{"sh1"}, map[string]string{"sh1": executors.ReasonWaitingForLaunch}, 1)
	f.expectEvents("validate 15s")
	expectCommand(t, r, disruption.ReplaceDecision, "node-x")

	// shift-evict runs both: the shift's wait covers the merge's validation.
	g, p := setup(t)
	r = g.execute(executors.ShiftEvictName, nil, p)
	expectReport(t, r, []string{"m1", "sh1"}, nil, nil, 2)
	g.expectEvents("wait 15s", "validate 0s", "evict default/s1")
}

func TestSequenceAbortsAfterFailure(t *testing.T) {
	f := threeNodes(t)
	f.refuse.Insert("a1")
	p := f.plan(move("s1", pipeline.Shift, bind("a1", "node-a", "node-c")), move("s2", pipeline.Shift, bind("b1", "node-b", "node-c")))
	p.Sequence = true
	r := f.execute(executors.ShiftEvictName, nil, p)
	expectReport(t, r, nil, []string{"s1", "s2"}, map[string]string{"s1": executors.ReasonPDB, "s2": "sequence: s1 did not complete"}, 0)
	f.expectEvents("wait 15s", "refuse default/a1")

	// A deferral is the executor's choice, not a failure: the sequence goes on past it.
	g := threeNodes(t)
	p = g.plan(
		move("s1", pipeline.Shift, bind("a1", "node-a", "node-c")),
		move("m1", pipeline.Merge, bind("a1", "node-a", "node-b"), bind("a2", "node-a", "node-b"), bind("a3", "node-a", "node-b")),
		move("s2", pipeline.Shift, bind("b1", "node-b", "node-c")))
	p.Sequence = true
	r = g.execute(executors.ShiftEvictName, nil, p)
	expectReport(t, r, []string{"s1", "s2"}, []string{"m1"}, map[string]string{"m1": executors.ReasonSourceDraining}, 2)
}

func TestParams(t *testing.T) {
	reg := pipeline.Registered()[pipeline.StageExecute]
	for _, name := range []string{executors.ShiftEvictName, executors.SteeredName, executors.MakeBeforeBreakName} {
		if !lo.Contains(reg, name) {
			t.Errorf("%s is not registered: %v", name, reg)
		}
	}
	f := newFixture(t, true)
	valid := map[string][]pipeline.Params{
		executors.ShiftEvictName:      {nil, {"validate": false, "taint": false, "taintTTL": "30s"}},
		executors.SteeredName:         {nil, {"mode": "affinity"}, {"mode": "most-allocated", "validate": false, "taintTTL": "1h"}},
		executors.MakeBeforeBreakName: {nil, {"validate": true, "taint": true, "taintTTL": "10m"}},
	}
	invalid := map[string][]pipeline.Params{
		executors.ShiftEvictName:      {{"taintTTL": "0s"}, {"taintTTL": "-1m"}, {"taintTTL": "soon"}, {"mode": "affinity"}, {"validate": "yes"}, {"bogus": 1}},
		executors.SteeredName:         {{"mode": "webhook"}, {"taintTTL": "0"}, {"bogus": true}},
		executors.MakeBeforeBreakName: {{"mode": "affinity"}, {"taintTTL": "-5s"}},
	}
	for name, cases := range valid {
		for _, params := range cases {
			if _, err := pipeline.NewExecutor(f.env, pipeline.StageConfig{Name: name, Params: params}); err != nil {
				t.Errorf("%s %v: %v", name, params, err)
			}
		}
	}
	for name, cases := range invalid {
		for _, params := range cases {
			if _, err := pipeline.NewExecutor(f.env, pipeline.StageConfig{Name: name, Params: params}); err == nil {
				t.Errorf("%s %v: no error", name, params)
			}
		}
		if _, err := pipeline.NewExecutor(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: name}); err == nil || !strings.Contains(err.Error(), "live cluster") {
			t.Errorf("%s without a cluster: %v, want a live-cluster error", name, err)
		}
	}
}

// --- contract ---------------------------------------------------------------------------------------------------

const contractSeeds = 6

// TestExecutorContract runs every executor here, under several params, over seeded generated states and plans that
// contain do-not-disrupt pods, budget-sharing moves and refused evictions. Whatever the plan, an executor never
// evicts a do-not-disrupt pod or a pod on a do-not-disrupt node, never evicts past a PodDisruptionBudget's allowance
// (the fake API enforces none, so the executor's own count must hold), evicts only planned pods and each at most
// once, accounts for every move exactly once, and gives byte-identical results for the same seed.
func TestExecutorContract(t *testing.T) {
	arms := []struct {
		name   string
		params pipeline.Params
	}{
		{executors.ShiftEvictName, nil},
		{executors.ShiftEvictName, pipeline.Params{"validate": false}},
		{executors.SteeredName, nil},
		{executors.SteeredName, pipeline.Params{"mode": executors.ModeMostAllocated, "validate": false}},
		{executors.MakeBeforeBreakName, nil},
		{executors.MakeBeforeBreakName, pipeline.Params{"validate": false, "taint": false}},
	}
	for _, arm := range arms {
		t.Run(fmt.Sprintf("%s/%v", arm.name, arm.params), func(t *testing.T) {
			ran := 0
			for seed := int64(1); seed <= contractSeeds; seed++ {
				first, n := runContract(t, arm.name, arm.params, seed)
				second, _ := runContract(t, arm.name, arm.params, seed)
				if first != second {
					t.Errorf("seed %d: not deterministic for a seed:\n%s\n---\n%s", seed, first, second)
				}
				ran += n
			}
			if ran == 0 {
				t.Errorf("no shift ran on any of %d seeds", contractSeeds)
			}
		})
	}
}

// runContract checks one seed and returns its transcript and how many shifts executed.
//
//nolint:gocyclo
func runContract(t *testing.T, name string, params pipeline.Params, seed int64) (string, int) {
	t.Helper()
	f := newFixture(t, false)
	if _, err := harness.GenerateState(f.ctx, f.h, seed, harness.StateOptions{Policy: v1.ConsolidationPolicyWhenEmptyOrUnderutilized,
		InstanceTypes: f.its, MinNodes: 6, MaxNodes: 14}); err != nil {
		t.Fatal(err)
	}
	snap := f.cycle()
	rng := rand.New(rand.NewSource(seed)) //nolint:gosec
	moves := randomShifts(snap, rng, 5)
	if len(moves) > 0 {
		// The plan contains do-not-disrupt pods: a planned pod turns do-not-disrupt after planning, and a pod the
		// snapshot already protects is bound into the first shift.
		victim := moves[rng.Intn(len(moves))].Bindings[0].Pod
		f.doNotDisruptPod(victim.Name)
		if p, ok := protectedPod(snap); ok {
			m := &moves[0].Move
			m.Bindings = append(m.Bindings, pipeline.Binding{Pod: p.Key, From: p.Node, Target: m.Bindings[0].Target})
			m.Sources = sets.List(sets.New(append(m.Sources, p.Node)...))
			m.SortBindings()
		}
		// A refused eviction, and a merge of a draining source.
		f.refuse.Insert(moves[rng.Intn(len(moves))].Bindings[0].Pod.Name)
		moves = append(moves, move("m0", pipeline.Merge, lo.Filter(moves[0].Bindings, func(b pipeline.Binding, _ int) bool { return b.From == moves[0].Sources[0] })...))
	}
	after, err := pipeline.BuildSnapshot(f.ctx, f.up)
	if err != nil {
		t.Fatal(err)
	}
	p := pipeline.Plan{Moves: moves, Planner: "test", Snapshot: snap}
	r := f.execute(name, params, p)

	// Each pod belongs to the first move that plans it; the deferred merge reuses the first shift's bindings.
	planned := map[pipeline.PodKey]string{}
	for _, m := range moves {
		for _, b := range m.Bindings {
			if _, ok := planned[b.Pod]; !ok {
				planned[b.Pod] = m.ID
			}
		}
	}
	views := map[string]pipeline.PodView{}
	for _, n := range after.Nodes() {
		for _, pv := range after.Pods(n.Name) {
			views[pv.Key.String()] = pv
		}
	}
	evicted := evictions(f.events)
	perPDB := map[string]int{}
	perMove := map[string]int{}
	seen := sets.New[string]()
	for _, key := range evicted {
		pv, ok := views[key]
		if !ok {
			t.Errorf("seed %d: evicted unknown pod %s", seed, key)
			continue
		}
		node, _ := after.Node(pv.Node)
		if pv.DoNotDisrupt || node.DoNotDisrupt {
			t.Errorf("seed %d: %s evicted do-not-disrupt pod %s (node do-not-disrupt %v)", seed, name, key, node.DoNotDisrupt)
		}
		if seen.Has(key) {
			t.Errorf("seed %d: %s evicted %s twice", seed, name, key)
		}
		seen.Insert(key)
		id, ok := planned[pv.Key]
		if !ok {
			t.Errorf("seed %d: %s evicted unplanned pod %s", seed, name, key)
		}
		perMove[id]++
		for _, pdb := range pv.PDBs {
			perPDB[pdb]++
		}
	}
	for _, pdb := range sets.List(sets.KeySet(perPDB)) {
		if allowed := after.PDBAllowed(pdb); allowed >= 0 && perPDB[pdb] > allowed {
			t.Errorf("seed %d: %s evicted %d pods under %s, which allows %d", seed, name, perPDB[pdb], pdb, allowed)
		}
	}
	accounted := append(append([]string(nil), r.Executed...), r.Aborted...)
	sort.Strings(accounted)
	ids := lo.Map(moves, func(m pipeline.Scored, _ int) string { return m.ID })
	sort.Strings(ids)
	if !reflect.DeepEqual(accounted, ids) {
		t.Errorf("seed %d: %s accounted for %v, want each of %v once", seed, name, accounted, ids)
	}
	shifts := 0
	for _, id := range r.Executed {
		if perMove[id] == 0 {
			t.Errorf("seed %d: %s reports %s executed but evicted none of its pods", seed, name, id)
		}
		shifts++
	}
	if r.PodsEvicted != len(evicted) {
		t.Errorf("seed %d: %s reports %d pods evicted, the API saw %d", seed, name, r.PodsEvicted, len(evicted))
	}

	var b strings.Builder
	fmt.Fprintf(&b, "executed=%v aborted=%v pods=%d\n", r.Executed, r.Aborted, r.PodsEvicted)
	for _, id := range sets.List(sets.KeySet(r.Reasons)) {
		fmt.Fprintf(&b, "reason %s: %s\n", id, r.Reasons[id])
	}
	for _, e := range f.events {
		fmt.Fprintln(&b, e)
	}
	for _, n := range after.Nodes() {
		tainted, at := f.mark(n.Name)
		fmt.Fprintf(&b, "%s tainted=%v since=%q nominated=%v\n", n.Name, tainted, at, f.h.Cluster.IsNodeNominated(n.ProviderID))
	}
	return b.String(), shifts
}

// randomShifts draws non-conflicting shifts of one to three reschedulable pods that pass Fast feasibility, as a
// random shift generator would.
//
//nolint:gocyclo
func randomShifts(s pipeline.Snapshot, rng *rand.Rand, n int) []pipeline.Scored {
	var src, dst []pipeline.NodeView
	for _, nv := range s.Nodes() {
		if nv.Source {
			src = append(src, nv)
		}
		if nv.Destination {
			dst = append(dst, nv)
		}
	}
	fast := &pipeline.FastChecker{}
	used := sets.New[string]()
	var out []pipeline.Scored
	for attempt := 0; attempt < 50*n && len(out) < n && len(src) > 0 && len(dst) > 0; attempt++ {
		from, to := src[rng.Intn(len(src))], dst[rng.Intn(len(dst))]
		if from.Name == to.Name || used.Has(from.Name) || used.Has(to.Name) {
			continue
		}
		pods := lo.Filter(s.Pods(from.Name), func(p pipeline.PodView, _ int) bool { return p.Reschedulable })
		if len(pods) == 0 {
			continue
		}
		rng.Shuffle(len(pods), func(i, j int) { pods[i], pods[j] = pods[j], pods[i] })
		var bindings []pipeline.Binding
		for _, p := range pods[:1+rng.Intn(min(3, len(pods)))] {
			bindings = append(bindings, pipeline.Binding{Pod: p.Key, From: from.Name, Target: pipeline.ExistingTarget(to.Name)})
		}
		m := move(fmt.Sprintf("s%d", len(out)), pipeline.Shift, bindings...)
		if fast.Fast(s, m.Move) != nil {
			continue
		}
		used.Insert(from.Name, to.Name)
		out = append(out, m)
	}
	return out
}

// protectedPod returns the first reschedulable pod, in node then pod order, that is do-not-disrupt or sits on a
// do-not-disrupt node.
func protectedPod(s pipeline.Snapshot) (pipeline.PodView, bool) {
	for _, n := range s.Nodes() {
		for _, p := range s.Pods(n.Name) {
			if p.Reschedulable && (p.DoNotDisrupt || n.DoNotDisrupt) {
				return p, true
			}
		}
	}
	return pipeline.PodView{}, false
}
