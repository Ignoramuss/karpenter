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

package pipeline

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	pscheduling "sigs.k8s.io/karpenter/pkg/controllers/provisioning/scheduling"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/events"
	operatorlogging "sigs.k8s.io/karpenter/pkg/operator/logging"
	"sigs.k8s.io/karpenter/pkg/state/cost"
	disruptionutils "sigs.k8s.io/karpenter/pkg/utils/disruption"
	podutils "sigs.k8s.io/karpenter/pkg/utils/pod"
	"sigs.k8s.io/karpenter/pkg/utils/resources"
)

// CommandValidationDelay mirrors the disruption package's commandValidationDelay: how long validation waits before
// re-checking a consolidation command.
const CommandValidationDelay = 15 * time.Second

// Upstream holds handles on the live Karpenter components the upstream adapters wrap, plus the inputs of the
// current disruption cycle.
type Upstream struct {
	Clock         clock.Clock
	KubeClient    client.Client
	Cluster       *state.Cluster
	Provisioner   *provisioning.Provisioner
	CloudProvider cloudprovider.CloudProvider
	Events        events.Recorder
	Queue         *disruption.Queue

	// Multi and Single are upstream's own consolidation methods. The coarse adapters call their ComputeCommands; the
	// fine adapters use their building blocks and consolidated-state tracking.
	Multi  *disruption.MultiNodeConsolidation
	Single *disruption.SingleNodeConsolidation
	// MultiValidator and SingleValidator validate commands before they run, as the methods do.
	MultiValidator  disruption.Validator
	SingleValidator disruption.Validator
	// ValidationDelay is passed to validators; CommandValidationDelay unless a harness overrides it.
	ValidationDelay time.Duration
	// ClusterCost feeds Balanced totals when the pipeline recomputes candidates between rounds; may be nil.
	ClusterCost *cost.ClusterCost
	// OrderCandidates, when set, orders candidates the pipeline fetches itself. Harnesses set it to the same
	// deterministic order they give the controller's candidates; production leaves it nil.
	OrderCandidates func([]*disruption.Candidate) []*disruption.Candidate
	// Wait blocks for d on the injected clock; executors use it for their validation wait. The simulator replaces it
	// to advance virtual time.
	Wait func(ctx context.Context, d time.Duration) error

	cycle cycleInput
}

// ShiftSourceTaintKey marks a node that shift moves are emptying, with effect PreferNoSchedule, so the scheduler
// avoids refilling it. It is experimental and deliberately not karpenter.sh/disrupted: the node is not being removed
// yet, and nothing else may treat it as disrupted.
const ShiftSourceTaintKey = "consolidation.experimental.karpenter.sh/shift-source"

func clockWait(clk clock.Clock) func(ctx context.Context, d time.Duration) error {
	return func(ctx context.Context, d time.Duration) error {
		if d <= 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-clk.After(d):
			return nil
		}
	}
}

type cycleInput struct {
	candidates []*disruption.Candidate // in the order the disruption controller passed them
	byName     map[string]*disruption.Candidate
	totals     map[string]disruption.NodePoolTotals
	budgets    map[string]int
}

// UpstreamOptions customize NewUpstream.
type UpstreamOptions struct {
	// WrapValidator, when set, wraps every validator the adapters and upstream methods use. The simulator uses it to
	// advance virtual time during the validation wait; the parity harness uses it to skip the wait.
	WrapValidator func(disruption.Validator) disruption.Validator
	// ValidationDelay overrides CommandValidationDelay when non-zero.
	ValidationDelay time.Duration
	// OrderCandidates sets Upstream.OrderCandidates.
	OrderCandidates func([]*disruption.Candidate) []*disruption.Candidate
	// Wait sets Upstream.Wait; nil waits on the clock.
	Wait func(ctx context.Context, d time.Duration) error
}

// NewUpstream builds the handles, constructing upstream's multi-node and single-node methods the same way
// disruption.NewMethods does.
func NewUpstream(clk clock.Clock, kubeClient client.Client, cluster *state.Cluster, provisioner *provisioning.Provisioner,
	cp cloudprovider.CloudProvider, recorder events.Recorder, queue *disruption.Queue, opts UpstreamOptions) *Upstream {
	wrap := lo.Ternary(opts.WrapValidator != nil, opts.WrapValidator, func(v disruption.Validator) disruption.Validator { return v })
	c := disruption.MakeConsolidation(clk, cluster, kubeClient, provisioner, cp, recorder, queue)
	mv, sv := wrap(disruption.NewMultiConsolidationValidator(c)), wrap(disruption.NewSingleConsolidationValidator(c))
	return &Upstream{
		Clock: clk, KubeClient: kubeClient, Cluster: cluster, Provisioner: provisioner, CloudProvider: cp, Events: recorder, Queue: queue,
		Multi:           disruption.NewMultiNodeConsolidation(c, disruption.WithValidator(mv)),
		Single:          disruption.NewSingleNodeConsolidation(c, disruption.WithValidator(sv)),
		MultiValidator:  mv,
		SingleValidator: sv,
		ValidationDelay: lo.Ternary(opts.ValidationDelay > 0, opts.ValidationDelay, CommandValidationDelay),
		OrderCandidates: opts.OrderCandidates,
		Wait:            lo.Ternary(opts.Wait != nil, opts.Wait, clockWait(clk)),
	}
}

// SetCycle records the disruption controller's inputs for this cycle.
func (u *Upstream) SetCycle(candidates []*disruption.Candidate, totals map[string]disruption.NodePoolTotals, budgets map[string]int) {
	u.cycle = cycleInput{
		candidates: candidates,
		byName:     lo.SliceToMap(candidates, func(c *disruption.Candidate) (string, *disruption.Candidate) { return c.Name(), c }),
		totals:     totals,
		budgets:    budgets,
	}
}

// Candidate returns this cycle's upstream candidate for a node.
func (u *Upstream) Candidate(name string) (*disruption.Candidate, bool) {
	c, ok := u.cycle.byName[name]
	return c, ok
}

// candidatesFor returns this cycle's candidates whose names are in names, in the controller's original order.
func (u *Upstream) candidatesFor(names []string) []*disruption.Candidate {
	want := sets.New(names...)
	return lo.Filter(u.cycle.candidates, func(c *disruption.Candidate, _ int) bool { return want.Has(c.Name()) })
}

// Budgets returns a copy of this cycle's node disruption budgets.
func (u *Upstream) Budgets() map[string]int { return lo.Assign(map[string]int{}, u.cycle.budgets) }

// NodePoolTotals returns this cycle's Balanced totals.
func (u *Upstream) NodePoolTotals() map[string]disruption.NodePoolTotals { return u.cycle.totals }

// UpstreamCommand is the payload a merge carries when it corresponds to an upstream command.
type UpstreamCommand struct {
	Command disruption.Command
	// Validated reports the command already passed upstream validation (the coarse adapters validate inside
	// ComputeCommands), so the executor must not validate it again.
	Validated bool
}

// CommandOf returns the upstream command a move carries.
func CommandOf(m Move) (*UpstreamCommand, bool) {
	uc, ok := m.Upstream.(*UpstreamCommand)
	return uc, ok && uc != nil
}

// --- Snapshot construction -------------------------------------------------------------------------------------

type pdbInfo struct {
	key      string
	ns       string
	selector labels.Selector
	allowed  int
}

// BuildSnapshot captures cluster state for planning. Nodes are sorted by name; candidate nodes are sources;
// initialized nodes that are not being deleted or tainted as disrupted are destinations.
//
//nolint:gocyclo
func BuildSnapshot(ctx context.Context, u *Upstream) (*State, error) {
	_, itMap, err := disruption.BuildNodePoolMap(ctx, u.KubeClient, u.CloudProvider)
	if err != nil {
		return nil, fmt.Errorf("building nodepool map, %w", err)
	}
	pdbList := &policyv1.PodDisruptionBudgetList{}
	if err := u.KubeClient.List(ctx, pdbList); err != nil {
		return nil, fmt.Errorf("listing pod disruption budgets, %w", err)
	}
	var pdbs []pdbInfo
	pdbAllowed := map[string]int{}
	for i := range pdbList.Items {
		p := &pdbList.Items[i]
		sel, err := metav1.LabelSelectorAsSelector(p.Spec.Selector)
		if err != nil {
			continue
		}
		key := p.Namespace + "/" + p.Name
		pdbs = append(pdbs, pdbInfo{key: key, ns: p.Namespace, selector: sel, allowed: int(p.Status.DisruptionsAllowed)})
		pdbAllowed[key] = int(p.Status.DisruptionsAllowed)
	}
	stateNodes := u.Cluster.DeepCopyNodes()
	sort.Slice(stateNodes, func(i, j int) bool { return stateNodes[i].Name() < stateNodes[j].Name() })
	var nodes []NodeView
	var pods []PodView
	for _, sn := range stateNodes {
		if sn.Node == nil {
			continue
		}
		name := sn.Name()
		lbls := sn.Labels()
		pool := lbls[v1.NodePoolLabelKey]
		cand, isCand := u.cycle.byName[name]
		view := NodeView{
			Name: name, ProviderID: sn.ProviderID(), NodePool: pool, InstanceType: lbls[corev1.LabelInstanceTypeStable],
			Zone: lbls[corev1.LabelTopologyZone], CapacityType: lbls[v1.CapacityTypeLabelKey],
			Capacity: ResourcesFromList(sn.Allocatable()), Labels: lbls, Taints: sn.Taints(),
			Source:       isCand,
			Destination:  sn.Initialized() && !sn.MarkedForDeletion() && !sn.Node.Spec.Unschedulable && !hasDisruptedTaint(sn.Taints()),
			DoNotDisrupt: sn.Annotations()[v1.DoNotDisruptAnnotationKey] == "true",
		}
		if isCand {
			view.Price, view.DisruptionCost = cand.Price, cand.RescheduleDisruptionCost
		} else {
			view.Price, view.DisruptionCost = disruptionutils.ResolveOfferingPrice(lbls, itMap[pool][view.InstanceType]), sn.DisruptionCost()
		}
		nodes = append(nodes, view)
		nodePods, err := sn.Pods(ctx, u.KubeClient)
		if err != nil {
			return nil, fmt.Errorf("listing pods on %s, %w", name, err)
		}
		for _, p := range nodePods {
			if podutils.IsTerminal(p) {
				continue
			}
			pods = append(pods, podViewOf(ctx, p, name, pdbs, u.Clock, u.Events))
		}
	}
	totals := map[string]Totals{}
	for pool, t := range u.cycle.totals {
		totals[pool] = Totals{TotalCost: t.TotalCost, TotalDisruptionCost: t.TotalDisruptionCost}
	}
	return NewState(nodes, pods, totals, pdbAllowed), nil
}

func hasDisruptedTaint(ts []corev1.Taint) bool {
	return lo.ContainsBy(ts, func(t corev1.Taint) bool { return t.MatchTaint(&v1.DisruptedNoScheduleTaint) })
}

// PodViewOf converts a pod bound to node, without PDB membership.
func PodViewOf(ctx context.Context, p *corev1.Pod, node string, clk clock.Clock, recorder events.Recorder) PodView {
	return podViewOf(ctx, p, node, nil, clk, recorder)
}

func podViewOf(ctx context.Context, p *corev1.Pod, node string, pdbs []pdbInfo, clk clock.Clock, recorder events.Recorder) PodView {
	pv := PodView{
		Key:           PodKey{Namespace: p.Namespace, Name: p.Name},
		Node:          node,
		Requests:      ResourcesFromList(resources.RequestsForPods(p)),
		EvictionCost:  math.Max(0, disruptionutils.EvictionCost(ctx, p)),
		Labels:        p.Labels,
		Reschedulable: podutils.IsReschedulable(p),
		DoNotDisrupt:  !podutils.IsDisruptable(p, clk, recorder),
		Pod:           p,
	}
	if ref := metav1.GetControllerOf(p); ref != nil {
		pv.Owner = ref.Kind + "/" + p.Namespace + "/" + ref.Name
	}
	for _, d := range pdbs {
		if d.ns == p.Namespace && d.selector.Matches(labels.Set(p.Labels)) {
			pv.PDBs = append(pv.PDBs, d.key)
		}
	}
	return pv
}

// --- Command <-> Move ------------------------------------------------------------------------------------------

// MoveFromCommand converts an upstream command into a merge. Pods the scheduling simulation left out (for example
// pods a fully blocking PDB holds in place) are first-fit onto destinations so the snapshot can apply the move.
//
//nolint:gocyclo
func MoveFromCommand(s Snapshot, cmd disruption.Command, id, generator string, validated bool) Move {
	m := Move{ID: id, Kind: Merge, Generator: generator, Upstream: &UpstreamCommand{Command: cmd, Validated: validated}}
	for _, c := range cmd.Candidates {
		m.Sources = append(m.Sources, c.Name())
		m.Remove = append(m.Remove, c.Name())
	}
	target := map[types.NamespacedName]Target{}
	for _, en := range cmd.Results.ExistingNodes {
		for _, p := range en.Pods {
			target[client.ObjectKeyFromObject(p)] = ExistingTarget(en.Name())
		}
	}
	for i, r := range cmd.Replacements {
		m.Launch = append(m.Launch, launchSpecOf(r.NodeClaim))
		for _, p := range r.Pods {
			target[client.ObjectKeyFromObject(p)] = Target{Launch: i}
		}
	}
	removing := sets.New(m.Remove...)
	added := map[string]Resources{}
	for _, src := range m.Sources {
		for _, p := range s.Pods(src) {
			if !p.Reschedulable {
				continue
			}
			t, ok := target[types.NamespacedName{Namespace: p.Key.Namespace, Name: p.Key.Name}]
			if !ok {
				t, ok = firstFit(s, p, removing, added)
				if !ok && len(m.Launch) > 0 {
					t, ok = Target{Launch: 0}, true
				}
				if !ok {
					continue
				}
			}
			if t.Launch < 0 {
				added[t.Node] = added[t.Node].Add(p.Requests)
			}
			m.Bindings = append(m.Bindings, Binding{Pod: p.Key, From: src, Target: t})
		}
	}
	m.SortBindings()
	return m
}

func firstFit(s Snapshot, p PodView, exclude sets.Set[string], added map[string]Resources) (Target, bool) {
	for _, n := range s.Nodes() {
		if !n.Destination || exclude.Has(n.Name) {
			continue
		}
		if s.Load(n.Name).Add(added[n.Name]).Add(p.Requests).Fits(n.Capacity) {
			return ExistingTarget(n.Name), true
		}
	}
	return Target{}, false
}

func launchSpecOf(nc *pscheduling.NodeClaim) LaunchSpec {
	l := LaunchSpec{NodePool: nc.NodePoolName}
	for _, it := range nc.InstanceTypeOptions {
		l.InstanceTypes = append(l.InstanceTypes, it.Name)
	}
	if zr := nc.Requirements.Get(corev1.LabelTopologyZone); zr.Len() == 1 {
		l.Zone = zr.Any()
	}
	if ct := nc.Requirements.Get(v1.CapacityTypeLabelKey); ct.Len() == 1 {
		l.CapacityType = ct.Any()
	}
	if len(nc.InstanceTypeOptions) > 0 {
		it := nc.InstanceTypeOptions[0]
		l.Capacity = ResourcesFromList(it.Allocatable())
		// The same price EstimatedSavings attributes to the replacement.
		if launchable := it.Offerings.Launchable().Compatible(nc.Requirements); len(launchable) > 0 {
			l.Price = launchable.Cheapest().Price
		}
	}
	return l
}

// --- Full feasibility ------------------------------------------------------------------------------------------

// ErrInfeasible reports that Karpenter's scheduling simulation rejected a move.
var ErrInfeasible = errors.New("infeasible")

// UpstreamFeasibility is Feasibility backed by the live cluster: Fast from the snapshot, Full from Karpenter's
// scheduler.
type UpstreamFeasibility struct {
	*FastChecker
	Up *Upstream
}

// Full confirms a move. A merge is simulated the way consolidation simulates candidates, and the confirmed move takes
// the simulation's placements and replacement. A shift is simulated against its destinations alone, with no new
// capacity allowed.
//
//nolint:gocyclo
func (f *UpstreamFeasibility) Full(ctx context.Context, s Snapshot, m Move) (Move, error) {
	switch m.Kind {
	case Merge, RightSize:
		cands := make([]*disruption.Candidate, 0, len(m.Sources))
		for _, src := range m.Sources {
			c, ok := f.Up.Candidate(src)
			if !ok {
				return m, fmt.Errorf("%w: %s is not a candidate this cycle", ErrInfeasible, src)
			}
			cands = append(cands, c)
		}
		cmd, err := f.Up.Single.ComputeConsolidation(ctx, cands...)
		if err != nil {
			return m, err
		}
		if cmd.Decision() == disruption.NoOpDecision {
			return m, fmt.Errorf("%w: simulation found no consolidation", ErrInfeasible)
		}
		if len(cands) > 1 && cmd.Decision() == disruption.ReplaceDecision {
			r, err := disruption.FilterOutSameInstanceType(cmd.Replacements[0], cands)
			if err != nil || r == nil || len(r.InstanceTypeOptions) == 0 {
				return m, fmt.Errorf("%w: replacement is not cheaper than keeping a source", ErrInfeasible)
			}
			cmd.Replacements[0] = r
		}
		confirmed := MoveFromCommand(s, cmd, m.ID, m.Generator, false)
		confirmed.Kind = m.Kind
		return confirmed, nil
	case Shift:
		return f.fullShift(ctx, m)
	}
	return m, fmt.Errorf("%w: unknown move kind %q", ErrInfeasible, m.Kind)
}

//nolint:gocyclo
func (f *UpstreamFeasibility) fullShift(ctx context.Context, m Move) (Move, error) {
	dests := sets.New(m.Destinations()...)
	if dests.Len() == 0 || len(m.Launch) > 0 {
		return m, fmt.Errorf("%w: a shift binds pods to existing destinations only", ErrInfeasible)
	}
	all := f.Up.Cluster.DeepCopyNodes()
	stateNodes := lo.Filter(all, func(n *state.StateNode, _ int) bool { return dests.Has(n.Name()) })
	sort.Slice(stateNodes, func(i, j int) bool { return stateNodes[i].Name() < stateNodes[j].Name() })
	sources := sets.New(lo.Map(m.Bindings, func(b Binding, _ int) string { return b.From })...)
	byKey := map[types.NamespacedName]*corev1.Pod{}
	for _, sn := range all {
		if !sources.Has(sn.Name()) {
			continue
		}
		ps, err := sn.Pods(ctx, f.Up.KubeClient)
		if err != nil {
			return m, err
		}
		for _, p := range ps {
			byKey[client.ObjectKeyFromObject(p)] = p
		}
	}
	var pods []*corev1.Pod
	for _, b := range m.Bindings {
		p, ok := byKey[types.NamespacedName{Namespace: b.Pod.Namespace, Name: b.Pod.Name}]
		if !ok {
			return m, fmt.Errorf("%w: pod %s not on %s", ErrInfeasible, b.Pod, b.From)
		}
		pods = append(pods, p.DeepCopy())
	}
	quiet := log.IntoContext(ctx, operatorlogging.NopLogger)
	uids := sets.New(lo.Map(pods, func(p *corev1.Pod, _ int) types.UID { return p.UID })...)
	sched, err := f.Up.Provisioner.NewScheduler(quiet, pods, stateNodes, uids, pscheduling.IsConsolidationSimulation)
	if err != nil {
		return m, fmt.Errorf("creating scheduler, %w", err)
	}
	results, err := sched.Solve(quiet, pods)
	if err != nil {
		return m, fmt.Errorf("scheduling pods, %w", err)
	}
	if len(results.PodErrors) > 0 || len(results.NewNodeClaims) > 0 {
		return m, fmt.Errorf("%w: pods do not fit the destinations", ErrInfeasible)
	}
	placed := map[PodKey]string{}
	for _, en := range results.ExistingNodes {
		for _, p := range en.Pods {
			placed[PodKey{Namespace: p.Namespace, Name: p.Name}] = en.Name()
		}
	}
	confirmed := m
	confirmed.Bindings = append([]Binding(nil), m.Bindings...)
	for i, b := range confirmed.Bindings {
		node, ok := placed[b.Pod]
		if !ok {
			return m, fmt.Errorf("%w: pod %s not placed", ErrInfeasible, b.Pod)
		}
		confirmed.Bindings[i].Target = ExistingTarget(node)
	}
	return confirmed, nil
}
