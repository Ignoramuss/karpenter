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

package executors

import (
	"context"
	"fmt"
	"sort"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// shiftRun is the state shifts share within one Execute.
type shiftRun struct {
	// shifts counts the shifts that will run; destinations are their existing target nodes, which are never tainted
	// as sources even when another move in the plan drains them (a swap, or a sequence).
	shifts       int
	destinations sets.Set[string]
	// cur is the fresh snapshot, taken at the first shift, forked forward by each shift as it runs so that a later
	// move in the plan is checked against the cluster its predecessors left.
	cur pipeline.Snapshot
	// pdbUsed counts this plan's evictions per PodDisruptionBudget: cur's allowances are fixed for the snapshot, and
	// two moves that each fit a budget alone may not fit it together.
	pdbUsed map[string]int
	evicted sets.Set[pipeline.PodKey]
}

// pod finds a pod on a node in the fresh snapshot.
func (x *shiftRun) pod(node string, key pipeline.PodKey) (pipeline.PodView, bool) {
	pods := x.cur.Pods(node)
	i := sort.Search(len(pods), func(i int) bool {
		k := pods[i].Key
		return k.Namespace > key.Namespace || (k.Namespace == key.Namespace && k.Name >= key.Name)
	})
	if i < len(pods) && pods[i].Key == key {
		return pods[i], true
	}
	return pipeline.PodView{}, false
}

// overPDB reports whether evicting one more of pod's replicas would exceed a budget's allowance in the snapshot.
func (x *shiftRun) overPDB(pod pipeline.PodView) bool {
	return lo.SomeBy(pod.PDBs, func(pdb string) bool {
		allowed := x.cur.PDBAllowed(pdb)
		return allowed >= 0 && x.pdbUsed[pdb] >= allowed
	})
}

// newShiftRun counts the plan's shifts that will run and collects their destinations.
func newShiftRun(p pipeline.Plan, deferred []string) *shiftRun {
	x := &shiftRun{destinations: sets.New[string](), pdbUsed: map[string]int{}, evicted: sets.New[pipeline.PodKey]()}
	for i, m := range p.Moves {
		if m.Kind == pipeline.Shift && deferred[i] == "" {
			x.shifts++
			x.destinations.Insert(m.Destinations()...)
		}
	}
	return x
}

// shift carries out one shift: re-check it, report it to the observer, check E3's destinations, taint its sources,
// then evict its pods in binding order.
func (e *executor) shift(ctx context.Context, x *shiftRun, m pipeline.Move) (outcome, error) {
	if x.cur == nil {
		fresh, err := pipeline.BuildSnapshot(ctx, e.env.Upstream)
		if err != nil {
			return outcome{}, fmt.Errorf("building a snapshot to validate shifts, %w", err)
		}
		x.cur = fresh
	}
	planned, skipReason, abort := e.recheck(x, m)
	if abort != nil {
		return *abort, nil
	}
	e.env.Observe(planned)
	if e.makeBeforeBreak && !e.prepareDestinations(ctx, planned.Destinations()) {
		return outcome{reason: ReasonDestinationNotReady}, nil
	}
	tainted, err := e.markSources(ctx, x, planned)
	if err != nil {
		return failed("taint", err), nil
	}
	done, stop := e.evict(ctx, x, planned)
	// A stop ends the move early, so it outranks the reason any pods were skipped.
	reason := lo.CoalesceOrEmpty(stop, skipReason)
	if len(done) == 0 {
		// Nothing moved, so the move leaves no trace: the taints it placed come off again.
		e.unmarkSources(ctx, tainted)
		return outcome{reason: lo.CoalesceOrEmpty(reason, ReasonPodsLeft)}, nil
	}
	moved := planned
	moved.Bindings = done
	if next, err := x.cur.Apply(moved); err == nil {
		x.cur = next
	}
	o := outcome{executed: true, complete: reason == "" && len(done) == len(m.Bindings), reason: reason, pods: len(done)}
	if len(done) < len(m.Bindings) {
		o.evicted = done
	}
	return o, nil
}

// recheck admits the shift's bindings and, when validating, re-checks what is left with Fast on the fresh
// snapshot. It returns the move as it will run and the reason any planned pods were skipped (do-not-disrupt pods
// outrank vanished ones), or the abort when nothing can run.
func (e *executor) recheck(x *shiftRun, m pipeline.Move) (pipeline.Move, string, *outcome) {
	kept, protected, gone := e.admit(x, m)
	planned := m
	planned.Bindings = kept
	if e.validate {
		if err := e.fastCheck(x.cur, planned); err != nil {
			o := failed("churn", err)
			return planned, "", &o
		}
	}
	skipReason := ""
	switch {
	case protected > 0:
		skipReason = ReasonDoNotDisrupt
	case gone > 0:
		skipReason = ReasonPodsLeft
	}
	if len(kept) == 0 {
		return planned, "", &outcome{reason: lo.CoalesceOrEmpty(skipReason, "no bindings")}
	}
	return planned, skipReason, nil
}

func (e *executor) fastCheck(s pipeline.Snapshot, m pipeline.Move) error {
	if e.env.Feasibility != nil {
		return e.env.Feasibility.Fast(s, m)
	}
	return e.fast.Fast(s, m)
}

// admit drops the bindings no executor may carry out whatever the plan says: a pod marked do-not-disrupt, or on a
// do-not-disrupt node, in the fresh snapshot (counted as protected). Without validation it also drops bindings whose
// pod is no longer a reschedulable pod on its source, or was already evicted by this plan (counted as gone), since
// evicting by name would then hit some other pod or none. With validation those stay, so the Fast re-check fails and
// the whole move aborts, as upstream validation rejects a command on churn.
func (e *executor) admit(x *shiftRun, m pipeline.Move) (kept []pipeline.Binding, protected, gone int) {
	for _, b := range m.Bindings {
		node, nodeOK := x.cur.Node(b.From)
		pod, podOK := x.pod(b.From, b.Pod)
		switch {
		case (nodeOK && node.DoNotDisrupt) || (podOK && pod.DoNotDisrupt):
			protected++
			continue
		case e.validate:
		case !nodeOK || !podOK || !pod.Reschedulable || x.evicted.Has(b.Pod):
			gone++
			continue
		}
		kept = append(kept, b)
	}
	return kept, protected, gone
}

// prepareDestinations is E3's check before evicting into destinations: each must be in cluster state, registered,
// initialized and not marked for deletion. It then nominates them, which keeps consolidation from choosing them as
// candidates and provisioning from counting their capacity as free while the pods move.
func (e *executor) prepareDestinations(ctx context.Context, names []string) bool {
	cluster := e.env.Upstream.Cluster
	want := sets.New(names...)
	ready := map[string]string{}
	notReady := sets.New[string]()
	for sn := range cluster.Nodes() {
		if sn.Node == nil || !want.Has(sn.Name()) {
			continue
		}
		if sn.Initialized() && !sn.MarkedForDeletion() {
			ready[sn.Name()] = sn.ProviderID()
		} else {
			notReady.Insert(sn.Name())
		}
	}
	for _, n := range names {
		if _, ok := ready[n]; !ok || notReady.Has(n) {
			return false
		}
	}
	// Nominate after the iteration: NominateNodeForPod takes the cluster's write lock, which Nodes holds for reading.
	for _, n := range names {
		cluster.NominateNodeForPod(ctx, ready[n])
	}
	return true
}

// evict evicts each binding's pod through the Eviction API, in binding order, and returns the bindings whose pods
// went. It stops at the first refusal; a budget refusal, from this plan's count or the API's 429, gets ReasonPDB.
func (e *executor) evict(ctx context.Context, x *shiftRun, m pipeline.Move) ([]pipeline.Binding, string) {
	var done []pipeline.Binding
	for _, b := range m.Bindings {
		pod, ok := x.pod(b.From, b.Pod)
		if !ok || x.evicted.Has(b.Pod) {
			continue
		}
		if x.overPDB(pod) {
			return done, ReasonPDB
		}
		err := e.evictSteered(ctx, b, pod)
		if err == nil {
			for _, pdb := range pod.PDBs {
				x.pdbUsed[pdb]++
			}
			x.evicted.Insert(b.Pod)
			done = append(done, b)
			continue
		}
		if stop := stopReason(err); stop != "" {
			return done, stop
		}
	}
	return done, ""
}

// evictSteered evicts one pod, first asking, when steering, that its replacement prefer the planned node. A failed
// eviction withdraws the request: the pod stays, and a stale preference would steer the replacement of some later,
// unrelated eviction of it.
func (e *executor) evictSteered(ctx context.Context, b pipeline.Binding, pod pipeline.PodView) error {
	steered := e.steer && e.env.Steer != nil && b.Target.Launch < 0
	if steered {
		e.env.Steer(b.Pod, b.Target.Node)
	}
	err := e.evictPod(ctx, pod)
	if err != nil && steered {
		e.env.Steer(b.Pod, "")
	}
	return err
}

// stopReason classifies a failed eviction as the termination controller does: a pod already gone (404) or replaced
// (409) is skipped and returns "", a budget refusal (429) stops the move with ReasonPDB, and anything else stops it
// with the error.
func stopReason(err error) string {
	switch {
	case apierrors.IsNotFound(err) || apierrors.IsConflict(err):
		return ""
	case apierrors.IsTooManyRequests(err):
		return ReasonPDB
	}
	return "eviction: " + err.Error()
}

// evictPod creates the pod's eviction, preconditioned on the UID the fresh snapshot saw, so a pod recreated under
// the same name since is never the one evicted.
func (e *executor) evictPod(ctx context.Context, pod pipeline.PodView) error {
	meta := metav1.ObjectMeta{Name: pod.Key.Name, Namespace: pod.Key.Namespace}
	eviction := &policyv1.Eviction{ObjectMeta: meta}
	if pod.Pod != nil && pod.Pod.UID != "" {
		eviction.DeleteOptions = &metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: new(pod.Pod.UID)}}
	}
	return e.env.Upstream.KubeClient.SubResource("eviction").Create(ctx, &corev1.Pod{ObjectMeta: meta}, eviction)
}

// shiftSources returns the nodes a shift drains that are not also destinations of a shift in this plan, sorted.
func shiftSources(x *shiftRun, m pipeline.Move) []string {
	out := sets.New[string]()
	for _, b := range m.Bindings {
		if !x.destinations.Has(b.From) {
			out.Insert(b.From)
		}
	}
	return sets.List(out)
}
