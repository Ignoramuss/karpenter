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

// Package pipeline decomposes consolidation into four pluggable stages and the shared services they use:
//
//  1. CandidateSelector picks source and destination nodes.
//  2. MoveGenerator proposes moves (merge, shift, right-size) from them.
//  3. Planner scores moves with the shared Potential, disruption and savings models and decides what runs, within
//     node and pod-eviction budgets.
//  4. Executor carries the plan out.
//
// Stages 1 and 2 only propose. Every strategy is registered by name, so a run selects each stage from config. The
// "upstream" adapters reproduce today's consolidation decisions exactly (see parity_test.go), and the whole method is
// gated behind the ConsolidationPipeline alpha feature gate, off by default.
//
// All randomness flows from one seeded *rand.Rand passed to each stage, and planning code reads time only from the
// injected clock, so a run is reproducible from its seed.
package pipeline

import (
	"fmt"
	"maps"
	"math"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
)

// Dimension indexes a resource in a Resources vector.
type Dimension int

const (
	CPU          Dimension = iota // millicores
	Memory                        // bytes
	Pods                          // count
	Accelerators                  // sum of extended resources (GPUs and similar), count
	NumDimensions
)

func (d Dimension) String() string {
	return [...]string{"cpu", "memory", "pods", "accelerators"}[d]
}

// Resources is a fixed-size request or capacity vector, cheap to copy and add.
type Resources [NumDimensions]float64

func (r Resources) Add(o Resources) Resources {
	for i := range r {
		r[i] += o[i]
	}
	return r
}

func (r Resources) Sub(o Resources) Resources {
	for i := range r {
		r[i] -= o[i]
	}
	return r
}

// Fits reports whether r fits within capacity on every dimension that capacity defines or r requests.
func (r Resources) Fits(capacity Resources) bool {
	for i := range r {
		if r[i] > capacity[i]+1e-9 {
			return false
		}
	}
	return true
}

// Share returns r / capacity per dimension, 0 where capacity is 0 and nothing is requested, and +Inf where something
// is requested of a dimension the node lacks.
func (r Resources) Share(capacity Resources) Resources {
	var s Resources
	for i := range r {
		switch {
		case capacity[i] > 0:
			s[i] = r[i] / capacity[i]
		case r[i] > 0:
			s[i] = math.Inf(1)
		}
	}
	return s
}

// DominantShare is the largest share across CPU, memory and accelerators. Pod count is excluded: it is a limit, not a
// resource that drives cost, and including it would make small-pod nodes look full.
func (r Resources) DominantShare(capacity Resources) float64 {
	s := r.Share(capacity)
	return math.Max(math.Max(s[CPU], s[Memory]), s[Accelerators])
}

// ResourcesFromList converts a Kubernetes resource list. Extended resources other than cpu, memory and pods are summed
// into Accelerators.
func ResourcesFromList(rl corev1.ResourceList) Resources {
	var r Resources
	for name, q := range rl {
		switch name {
		case corev1.ResourceCPU:
			r[CPU] += float64(q.MilliValue())
		case corev1.ResourceMemory:
			r[Memory] += float64(q.Value())
		case corev1.ResourcePods:
			r[Pods] += float64(q.Value())
		case corev1.ResourceEphemeralStorage:
		default:
			if strings.Contains(string(name), "/") {
				r[Accelerators] += float64(q.Value())
			}
		}
	}
	return r
}

// PodKey identifies a pod.
type PodKey struct {
	Namespace string
	Name      string
}

func (k PodKey) String() string { return k.Namespace + "/" + k.Name }

// NodeView is the planning view of one node.
type NodeView struct {
	Name         string
	ProviderID   string
	NodePool     string
	InstanceType string
	Zone         string
	CapacityType string
	// Capacity is the node's allocatable.
	Capacity Resources
	// Price is the node's hourly price, $/hr, 0 when unknown.
	Price  float64
	Labels map[string]string
	Taints []corev1.Taint
	// Source reports that the node passed candidate filtering for this disruption cycle (upstream's NewCandidate
	// checks and the method's ShouldDisrupt), so its pods may be moved off it.
	Source bool
	// Destination reports that pods may be planned onto the node: initialized, not marked for deletion, not tainted
	// as disrupted.
	Destination bool
	// DoNotDisrupt reports the node-level karpenter.sh/do-not-disrupt annotation.
	DoNotDisrupt bool
	// DisruptionCost is upstream's RescheduleDisruptionCost for a source: 1.0 plus the positive eviction costs of its
	// reschedulable pods. For non-sources it is the incrementally maintained cost cluster state keeps.
	DisruptionCost float64
	// Launched marks a node a move would launch, which exists only in forked snapshots.
	Launched bool
}

// PodView is the planning view of one pod.
type PodView struct {
	Key      PodKey
	Node     string
	Requests Resources
	// EvictionCost is max(0, EvictionCost(pod)), upstream's per-pod disruption cost.
	EvictionCost float64
	// Owner is the controlling owner as "Kind/namespace/name", empty for bare pods. Replicas of one owner are
	// interchangeable for plan-fidelity accounting.
	Owner  string
	Labels map[string]string
	// Reschedulable is false for pods that stay with their node (DaemonSet and mirror pods).
	Reschedulable bool
	// DoNotDisrupt reports the pod-level karpenter.sh/do-not-disrupt annotation, a hard constraint.
	DoNotDisrupt bool
	// PDBs lists the PodDisruptionBudgets selecting this pod, as namespace/name.
	PDBs []string
	// Pod is the underlying object, for constraint checks; may be nil in synthetic snapshots.
	Pod *corev1.Pod
}

// MoveKind classifies moves.
type MoveKind string

const (
	// Merge empties a set of sources into existing destinations plus at most one launched node, then removes the
	// sources. Upstream Delete and Replace are merges.
	Merge MoveKind = "merge"
	// Shift moves some pods from a source to a destination and removes nothing.
	Shift MoveKind = "shift"
	// RightSize replaces one node with a cheaper one holding the same pods.
	RightSize MoveKind = "rightsize"
)

// Target is where a pod is planned to go: an existing node by name, or a node the move launches by index.
type Target struct {
	Node   string
	Launch int // index into Move.Launch, or -1 for an existing node
}

// ExistingTarget targets an existing node.
func ExistingTarget(node string) Target { return Target{Node: node, Launch: -1} }

// Binding is one planned unbind-pod plus bind-pod.
type Binding struct {
	Pod    PodKey
	From   string
	Target Target
}

// LaunchSpec is a node a move adds.
type LaunchSpec struct {
	NodePool string
	// InstanceTypes are the acceptable instance types, cheapest first.
	InstanceTypes []string
	Zone          string
	CapacityType  string
	// Capacity and Price describe the cheapest acceptable type, which the snapshot assumes launches.
	Capacity Resources
	Price    float64
}

// Move is a composition of actions that ends in a feasible state. Bindings are kept sorted by pod key so every
// iteration over a move is deterministic.
type Move struct {
	ID       string
	Kind     MoveKind
	Sources  []string
	Bindings []Binding
	Launch   []LaunchSpec
	Remove   []string
	// Generator is the strategy that proposed the move.
	Generator string
	// Upstream carries the upstream Command a merge corresponds to, when the move came from or was confirmed by the
	// upstream scheduling simulation. Executors that use the orchestration queue need it.
	Upstream any
}

// Destinations returns the existing nodes the move binds pods to, sorted.
func (m Move) Destinations() []string {
	seen := map[string]bool{}
	for _, b := range m.Bindings {
		if b.Target.Launch < 0 {
			seen[b.Target.Node] = true
		}
	}
	out := make([]string, 0, len(seen))
	for n := range seen {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// Touches returns every node the move changes, sorted: sources, destinations and removals.
func (m Move) Touches() []string {
	seen := map[string]bool{}
	for _, n := range m.Sources {
		seen[n] = true
	}
	for _, n := range m.Remove {
		seen[n] = true
	}
	for _, n := range m.Destinations() {
		seen[n] = true
	}
	out := make([]string, 0, len(seen))
	for n := range seen {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// SortBindings orders bindings by pod key.
func (m *Move) SortBindings() {
	sort.Slice(m.Bindings, func(i, j int) bool {
		if m.Bindings[i].Pod.Namespace != m.Bindings[j].Pod.Namespace {
			return m.Bindings[i].Pod.Namespace < m.Bindings[j].Pod.Namespace
		}
		return m.Bindings[i].Pod.Name < m.Bindings[j].Pod.Name
	})
}

func (m Move) String() string {
	return fmt.Sprintf("%s[%s] sources=%v remove=%v launch=%d pods=%d", m.Kind, m.ID, m.Sources, m.Remove, len(m.Launch), len(m.Bindings))
}

// Scored is a move with the shared models' assessment.
type Scored struct {
	Move
	// DeltaPotential is the change in the run's potential, normalized as the potential defines.
	DeltaPotential float64
	// Disruption is the sum of max(0, EvictionCost) over evicted pods, plus upstream's per-node base cost for each
	// removed node, matching Balanced's disruption numerator.
	Disruption float64
	// SavingsPerHour is the immediate change in hourly cost: removed node prices minus launched node prices.
	SavingsPerHour float64
	// NormalizedDisruption and NormalizedSavings divide by the NodePool totals, as Balanced does, so planner weights
	// are scale-free. Moves spanning pools use the sum of the pools' totals.
	NormalizedDisruption float64
	NormalizedSavings    float64
	// Energy is the planner's ΔE for the move, set by planners that use one.
	Energy float64
}

// Totals are a NodePool's cost and disruption totals, computed as Balanced computes them.
type Totals struct {
	TotalCost           float64
	TotalDisruptionCost float64
}

// Budgets bound what a plan may do in one cycle.
type Budgets struct {
	// Nodes is the remaining node disruption budget per NodePool for the cycle's reason.
	Nodes map[string]int
	// PodEvictions is the remaining pod-eviction budget per NodePool for the current minute; nil means unlimited.
	PodEvictions map[string]int
	// WorkloadEvictions is the remaining pod-eviction budget per workload (PodView.Owner) for the current minute;
	// nil means unlimited.
	WorkloadEvictions map[string]int
}

// Clone deep copies b so planners can spend from a copy.
func (b Budgets) Clone() Budgets {
	cp := func(m map[string]int) map[string]int {
		if m == nil {
			return nil
		}
		out := make(map[string]int, len(m))
		maps.Copy(out, m)
		return out
	}
	return Budgets{Nodes: cp(b.Nodes), PodEvictions: cp(b.PodEvictions), WorkloadEvictions: cp(b.WorkloadEvictions)}
}

// Plan is what stage 3 decides to run, in order.
type Plan struct {
	Moves []Scored
	// Sequence reports that the moves form one chain to be executed in order, aborting the rest if one fails.
	Sequence bool
	// Planner is the strategy that produced the plan.
	Planner string
	// ConstrainedByBudgets reports that some move was dropped only because a budget ran out, so the cluster should not
	// be treated as consolidated.
	ConstrainedByBudgets bool
	// Rejected maps the IDs of moves the planner declined to its reason, for the decision log.
	Rejected map[string]string
	// Snapshot is the state the plan was made on, for executors that need to confirm moves.
	Snapshot Snapshot
}

// Report is what stage 4 did.
type Report struct {
	Executed    []string // move IDs carried out (or handed to the orchestration queue)
	Aborted     []string // move IDs abandoned, with reasons in Reasons
	Reasons     map[string]string
	PodsEvicted int
	// Commands are the merges handed back to the disruption controller for the orchestration queue.
	Commands []any
}
