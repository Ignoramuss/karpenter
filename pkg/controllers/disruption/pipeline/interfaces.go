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
	"math/rand"
	"sync"
	"time"

	"k8s.io/utils/clock"
)

// Snapshot is an immutable, cheaply forkable view of cluster state used for planning.
type Snapshot interface {
	// Nodes returns every node, sorted by name.
	Nodes() []NodeView
	// Node returns one node by name.
	Node(name string) (NodeView, bool)
	// Pods returns the pods bound to a node, sorted by key.
	Pods(node string) []PodView
	// Load returns the summed requests of every pod bound to a node.
	Load(node string) Resources
	// Apply returns the snapshot after a move, copy-on-write. It fails if the move references a missing pod or node,
	// exceeds a destination's capacity, or removes a node that still holds reschedulable pods.
	Apply(m Move) (Snapshot, error)
	// Totals returns a NodePool's cost and disruption totals, fixed for the cycle the snapshot was taken in.
	Totals(nodePool string) Totals
	// Normalizer returns the fleet totals at the time the root snapshot was taken, inherited unchanged by forks, so
	// potentials normalize every fork by the same constants.
	Normalizer() Normalizer
	// PDBAllowed returns how many more pods a PodDisruptionBudget allows to be evicted, or -1 if unknown.
	PDBAllowed(pdb string) int
}

// Normalizer holds fleet-level constants from the root snapshot.
type Normalizer struct {
	TotalPrice float64
	NodeCount  int
}

// CandidateSelector is stage 1: it chooses which nodes may lose pods (sources) and which may receive them
// (destinations).
type CandidateSelector interface {
	Name() string
	Select(ctx context.Context, s Snapshot, rng *rand.Rand) (sources, destinations []NodeView, err error)
}

// MoveGenerator is stage 2: it proposes moves. It never executes anything.
type MoveGenerator interface {
	Name() string
	Generate(ctx context.Context, s Snapshot, sources, destinations []NodeView, rng *rand.Rand, b *SearchBudget) ([]Move, error)
}

// Planner is stage 3: it decides what runs, within budgets.
type Planner interface {
	Name() string
	Plan(ctx context.Context, s Snapshot, moves []Scored, budgets Budgets, rng *rand.Rand) (Plan, error)
}

// Guide is implemented by planners whose acceptance test search-guided generators consult while they search. The
// upstream multi-node binary search is the motivating case: whether it grows or shrinks its window depends on whether
// the planner would accept the move found, so generation and acceptance interleave there.
type Guide interface {
	// Accept reports whether the planner would accept the move on its own.
	Accept(ctx context.Context, s Snapshot, m Scored) bool
	// CanPass reports whether any move with these sources could pass, the upper bound used to skip a candidate before
	// simulating it (upstream's CanPassThreshold).
	CanPass(ctx context.Context, s Snapshot, sources []string) bool
}

// Executor is stage 4: it carries a plan out safely.
type Executor interface {
	Name() string
	Execute(ctx context.Context, p Plan) (Report, error)
}

// Potential is a scalar function of cluster state whose decrease consolidation pursues.
type Potential interface {
	Name() string
	Value(s Snapshot) float64
	// Delta must equal Value(s.Apply(m)) - Value(s); the property test checks it.
	Delta(s Snapshot, m Move) float64
}

// Feasibility checks whether a move can run.
type Feasibility interface {
	// Fast checks resources, taints, selectors and required node affinity, bound-volume zones, do-not-disrupt and
	// PDB allowances against the snapshot alone.
	Fast(s Snapshot, m Move) error
	// Full runs Karpenter's scheduling simulation against the live cluster and returns the move as confirmed: for a
	// merge, with the launch options and upstream command the simulation produced. Each call spends one full
	// simulation from the budget.
	Full(ctx context.Context, s Snapshot, m Move) (Move, error)
}

// Scorer fills in a move's shared assessments.
type Scorer interface {
	Score(s Snapshot, m Move) Scored
}

// SearchBudget caps work per cycle so strategies can be compared at equal compute. Copies share counters.
type SearchBudget struct {
	// MaxAttempts caps generation attempts (calls to Next); 0 means unlimited.
	MaxAttempts int
	// MaxFull caps full scheduling simulations; 0 means unlimited.
	MaxFull int
	// Deadline, when non-zero, stops search once the injected clock passes it.
	Deadline time.Time
	Clock    clock.Clock
	// Guide is the planner's acceptance test, for search-guided generators; nil accepts everything.
	Guide Guide
	// StopAtFirst asks generators to stop once they produce a move the guide accepts, as upstream does.
	StopAtFirst bool
	// Feasibility and Scorer let generators check and score candidate moves while searching.
	Feasibility Feasibility
	Scorer      Scorer

	c *budgetCounters
}

type budgetCounters struct {
	mu        sync.Mutex
	attempts  int
	full      int
	timedOut  bool
	exhausted bool
}

// NewSearchBudget returns a budget with fresh counters.
func NewSearchBudget(maxAttempts, maxFull int, deadline time.Time, clk clock.Clock) *SearchBudget {
	return &SearchBudget{MaxAttempts: maxAttempts, MaxFull: maxFull, Deadline: deadline, Clock: clk, c: &budgetCounters{}}
}

func (b *SearchBudget) counters() *budgetCounters {
	if b.c == nil {
		b.c = &budgetCounters{}
	}
	return b.c
}

func (b *SearchBudget) expired() bool {
	if b.Deadline.IsZero() || b.Clock == nil {
		return false
	}
	return b.Clock.Now().After(b.Deadline)
}

// Next records one generation attempt and reports whether another is allowed.
func (b *SearchBudget) Next() bool {
	c := b.counters()
	c.mu.Lock()
	defer c.mu.Unlock()
	if b.expired() {
		c.timedOut = true
		return false
	}
	if b.MaxAttempts > 0 && c.attempts >= b.MaxAttempts {
		c.exhausted = true
		return false
	}
	c.attempts++
	return true
}

// UseFull reserves one full simulation and reports whether it was available.
func (b *SearchBudget) UseFull() bool {
	c := b.counters()
	c.mu.Lock()
	defer c.mu.Unlock()
	if b.expired() {
		c.timedOut = true
		return false
	}
	if b.MaxFull > 0 && c.full >= b.MaxFull {
		c.exhausted = true
		return false
	}
	c.full++
	return true
}

// Stats returns attempts and full simulations used, and whether search stopped on the deadline or a cap.
func (b *SearchBudget) Stats() (attempts, full int, timedOut, exhausted bool) {
	c := b.counters()
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.attempts, c.full, c.timedOut, c.exhausted
}

// Stage names, as recorded in the decision log.
const (
	StageCandidates = "candidates"
	StageMoves      = "moves"
	StagePlan       = "plan"
	StageExecute    = "execute"
	StageCycle      = "cycle"
)

// DecisionLogSchemaVersion versions DecisionEvent; bump it on any field change, since the log is the raw data.
const DecisionLogSchemaVersion = 1

// DecisionEvent is one line of the decision log: one stage decision about one move (or one stage summary).
type DecisionEvent struct {
	SchemaVersion   int      `json:"schema_version"`
	RunID           string   `json:"run_id"`
	Cycle           int      `json:"cycle"`
	T               float64  `json:"t"` // seconds since the run started, from the injected clock
	Stage           string   `json:"stage"`
	Strategy        string   `json:"strategy"`
	MoveID          string   `json:"move_id,omitempty"`
	Kind            string   `json:"kind,omitempty"`
	Sources         []string `json:"sources,omitempty"`
	Destinations    []string `json:"destinations,omitempty"`
	Pods            int      `json:"pods"`
	DeltaPotential  float64  `json:"delta_potential"`
	Disruption      float64  `json:"disruption"`
	SavingsPerHour  float64  `json:"savings_per_hour"`
	Accepted        bool     `json:"accepted"`
	Reason          string   `json:"reason,omitempty"`
	BudgetRemaining int      `json:"budget_remaining"`
	// Detail carries stage-specific numbers (counts, energies, launch types) as sorted key/value pairs.
	Detail map[string]float64 `json:"detail,omitempty"`
}

// Recorder receives decision events.
type Recorder interface{ Record(e DecisionEvent) }
