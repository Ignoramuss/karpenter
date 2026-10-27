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

// Package planners holds the energy-based stage 3 planners, registered by name on import:
//
//	P1  entropy-greedy  every downhill move, most energy saved per unit of disruption first, no two touching a node
//	P2  anneal-plan     Metropolis search over move sequences on the snapshot; the best sequence runs only if it is
//	                    net downhill, so disruption is paid only for what runs
//	P3  anneal-online   Metropolis acceptance of one move per cycle, the temperature carried across cycles, so uphill
//	                    moves run on the cluster; the experiment-only contrast for H4
//	P4  sequence        chains the shifts that empty a node within a horizon, ends the chain in that node's delete and
//	                    judges the chain as a whole, so a shift is never judged alone
//
// Every planner scores a move by the planning energy ΔE = α ΔΦ + λ D̂ - μ Ŝ (pipeline.Weights) and takes the params
// alpha, lambda and mu, default 1, 0.5 and 1. anneal-plan adds t0 (0.05), cooling (0.97), temperatureUnit (absolute),
// steps (400) and maxLength (8); anneal-online adds t0, cooling and temperatureUnit; sequence adds horizon (4).
//
// Plan writes each input move's energy into its Energy field, so the decision log reports it. Plans never exceed the
// node and pod-eviction budgets, and never take a PodDisruptionBudget past its allowance in total: Fast feasibility
// checks each move alone, and moves that each fit can still exceed an allowance together. Every planner declines a
// move that would disrupt a do-not-disrupt pod or node whatever its generator checked, and the sequence planners
// re-check Fast feasibility on the fork each move applies to, a state no generator saw. Randomness comes only from the
// rng passed to Plan and no map iteration reaches a plan, so a seed reproduces one exactly.
package planners

import (
	"context"
	"fmt"
	"maps"
	"math"
	"math/rand"
	"slices"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// Registered planner names.
const (
	EntropyGreedyName = "entropy-greedy"
	AnnealPlanName    = "anneal-plan"
	AnnealOnlineName  = "anneal-online"
	SequenceName      = "sequence"
)

// Parameter defaults. The energy weights and the cooling schedule are the plan's placeholders; the Phase 4 pilot
// calibrates them.
const (
	DefaultAlpha     = 1.0
	DefaultLambda    = 0.5
	DefaultMu        = 1.0
	DefaultT0        = 0.05
	DefaultCooling   = 0.97
	DefaultSteps     = 400
	DefaultMaxLength = 8
	DefaultHorizon   = 4
)

// appendProbability is how often anneal-plan proposes growing its sequence when it could also shrink it. Above one
// half, so the search reaches multi-move sequences and the drop proposal mostly undoes bad tails.
const appendProbability = 0.7

// minDisruption floors the disruption entropy-greedy divides by, so a move that disrupts nothing ranks first instead
// of dividing by zero.
const minDisruption = 1e-6

func init() {
	pipeline.RegisterPlanner(EntropyGreedyName, newEntropyGreedy)
	pipeline.RegisterPlanner(AnnealPlanName, newAnnealPlan)
	pipeline.RegisterPlanner(AnnealOnlineName, newAnnealOnline)
	pipeline.RegisterPlanner(SequenceName, newSequence)
}

// --- params ----------------------------------------------------------------------------------------------------

// energyParams are the energy weights every planner here takes. Pointers tell an explicit zero (lambda: 0 ignores
// disruption) from an absent key.
type energyParams struct {
	Alpha  *float64 `json:"alpha"`
	Lambda *float64 `json:"lambda"`
	Mu     *float64 `json:"mu"`
}

func (p energyParams) weights() (pipeline.Weights, error) {
	w := pipeline.Weights{Alpha: DefaultAlpha, Lambda: DefaultLambda, Mu: DefaultMu}
	for _, f := range []struct {
		name string
		in   *float64
		out  *float64
	}{{"alpha", p.Alpha, &w.Alpha}, {"lambda", p.Lambda, &w.Lambda}, {"mu", p.Mu, &w.Mu}} {
		if f.in == nil {
			continue
		}
		// A negative weight would reward what the energy exists to penalize: a rising potential, disruption, or
		// lost savings.
		if !finite(*f.in) || *f.in < 0 {
			return w, fmt.Errorf("%s %v, want a finite non-negative weight", f.name, *f.in)
		}
		*f.out = *f.in
	}
	return w, nil
}

// Temperature units. Energies are normalized by NodePool totals, so a typical move's |ΔE| shrinks as 1/n with the
// fleet size n; "per-node" divides the temperature by the root snapshot's node count, so one t0 means the same
// acceptance odds at every scale (an uphill move worth one node's share of the fleet is taken with probability
// exp(-1/t0) at t0). "absolute" uses t0 as is.
const (
	TemperatureAbsolute = "absolute"
	TemperaturePerNode  = "per-node"
)

// scheduleParams are the annealing planners' temperature schedule.
type scheduleParams struct {
	T0              *float64 `json:"t0"`
	Cooling         *float64 `json:"cooling"`
	TemperatureUnit string   `json:"temperatureUnit"`
}

// unit validates the temperature unit and returns it, "absolute" by default.
func (p scheduleParams) unit() (string, error) {
	switch p.TemperatureUnit {
	case "", TemperatureAbsolute:
		return TemperatureAbsolute, nil
	case TemperaturePerNode:
		return TemperaturePerNode, nil
	}
	return "", fmt.Errorf("temperatureUnit %q, want %s or %s", p.TemperatureUnit, TemperatureAbsolute, TemperaturePerNode)
}

// temperature converts a schedule temperature to energy units on s.
func temperature(t float64, unit string, s pipeline.Snapshot) float64 {
	if unit == TemperaturePerNode {
		if n := s.Normalizer().NodeCount; n > 0 {
			return t / float64(n)
		}
	}
	return t
}

func (p scheduleParams) schedule() (t0, cooling float64, err error) {
	t0, cooling = DefaultT0, DefaultCooling
	if p.T0 != nil {
		if !finite(*p.T0) || *p.T0 <= 0 {
			return 0, 0, fmt.Errorf("t0 %v, want a finite positive temperature", *p.T0)
		}
		t0 = *p.T0
	}
	if p.Cooling != nil {
		// 1 holds the temperature constant; above 1 would heat the search instead of settling it.
		if !finite(*p.Cooling) || *p.Cooling <= 0 || *p.Cooling > 1 {
			return 0, 0, fmt.Errorf("cooling %v, want 0 < cooling <= 1", *p.Cooling)
		}
		cooling = *p.Cooling
	}
	return t0, cooling, nil
}

func atLeastOne(name string, in *int, def int) (int, error) {
	if in == nil {
		return def, nil
	}
	if *in < 1 {
		return 0, fmt.Errorf("%s %d, want at least 1", name, *in)
	}
	return *in, nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

func newEntropyGreedy(_ *pipeline.Env, p pipeline.Params) (pipeline.Planner, error) {
	var params energyParams
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", EntropyGreedyName, err)
	}
	w, err := params.weights()
	if err != nil {
		return nil, fmt.Errorf("%s: %w", EntropyGreedyName, err)
	}
	return &entropyGreedy{w: w}, nil
}

func newAnnealPlan(env *pipeline.Env, p pipeline.Params) (pipeline.Planner, error) {
	var params struct {
		energyParams
		scheduleParams
		Steps     *int `json:"steps"`
		MaxLength *int `json:"maxLength"`
	}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	pl := &annealPlan{env: env, fast: &pipeline.FastChecker{}}
	var err error
	if pl.w, err = params.weights(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	if pl.t0, pl.cooling, err = params.schedule(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	if pl.unit, err = params.unit(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	if pl.steps, err = atLeastOne("steps", params.Steps, DefaultSteps); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	if pl.maxLength, err = atLeastOne("maxLength", params.MaxLength, DefaultMaxLength); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealPlanName, err)
	}
	return pl, nil
}

func newAnnealOnline(_ *pipeline.Env, p pipeline.Params) (pipeline.Planner, error) {
	var params struct {
		energyParams
		scheduleParams
	}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealOnlineName, err)
	}
	pl := &annealOnline{}
	var err error
	if pl.w, err = params.weights(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealOnlineName, err)
	}
	if pl.t, pl.cooling, err = params.schedule(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealOnlineName, err)
	}
	if pl.unit, err = params.unit(); err != nil {
		return nil, fmt.Errorf("%s: %w", AnnealOnlineName, err)
	}
	return pl, nil
}

func newSequence(env *pipeline.Env, p pipeline.Params) (pipeline.Planner, error) {
	var params struct {
		energyParams
		Horizon *int `json:"horizon"`
	}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", SequenceName, err)
	}
	pl := &sequencePlanner{env: env, fast: &pipeline.FastChecker{}}
	var err error
	if pl.w, err = params.weights(); err != nil {
		return nil, fmt.Errorf("%s: %w", SequenceName, err)
	}
	if pl.horizon, err = atLeastOne("horizon", params.Horizon, DefaultHorizon); err != nil {
		return nil, fmt.Errorf("%s: %w", SequenceName, err)
	}
	return pl, nil
}

// --- shared ----------------------------------------------------------------------------------------------------

// acceptAll is the Guide of planners that filter moves themselves: a search-guided generator should hand them every
// move it finds, since only the planner sees how moves combine.
type acceptAll struct{}

// Accept reports true; the planner decides.
func (acceptAll) Accept(context.Context, pipeline.Snapshot, pipeline.Scored) bool { return true }

// CanPass reports true; a planner that judges moves in combination has no bound for a source alone.
func (acceptAll) CanPass(context.Context, pipeline.Snapshot, []string) bool { return true }

func newPlan(name string, sequence bool) pipeline.Plan {
	return pipeline.Plan{Planner: name, Sequence: sequence, Rejected: map[string]string{}}
}

// setEnergies records each move's energy on the snapshot it was scored on.
func setEnergies(w pipeline.Weights, moves []pipeline.Scored) {
	for i := range moves {
		moves[i].Energy = w.Energy(moves[i])
	}
}

// scorerOf returns the run's scorer, falling back to the default model over the run's potential. It is resolved per
// Plan call, not at construction, so a harness that fills Env in after building strategies still gets its scorer.
func scorerOf(env *pipeline.Env) pipeline.Scorer {
	if env != nil && env.Scorer != nil {
		return env.Scorer
	}
	var pot pipeline.Potential
	if env != nil {
		pot = env.Potential
	}
	return pipeline.DefaultScorer{Potential: pot}
}

// rescore scores m on s, a fork its generator did not score it on, and sets its energy.
func rescore(sc pipeline.Scorer, w pipeline.Weights, s pipeline.Snapshot, m pipeline.Move) pipeline.Scored {
	out := sc.Score(s, m)
	out.Energy = w.Energy(out)
	return out
}

// metropolis reports whether a change of dE is accepted at temperature t: always when it is downhill, otherwise with
// probability exp(-dE/t). Only uphill changes draw from rng, so a run's random stream depends on the energies alone.
func metropolis(dE, t float64, rng *rand.Rand) bool {
	switch {
	case math.IsNaN(dE):
		return false
	case dE <= 0:
		return true
	case t <= 0:
		return false
	}
	return rng.Float64() < math.Exp(-dE/t)
}

// movedPods returns, in binding order, the pods m takes off their nodes on s. A binding to a pod s lacks yields a zero
// view, which Apply rejects anyway.
func movedPods(s pipeline.Snapshot, m pipeline.Move) []pipeline.PodView {
	out := make([]pipeline.PodView, 0, len(m.Bindings))
	byNode := map[string]map[pipeline.PodKey]pipeline.PodView{}
	for _, b := range m.Bindings {
		if _, ok := byNode[b.From]; !ok {
			byNode[b.From] = map[pipeline.PodKey]pipeline.PodView{}
			for _, p := range s.Pods(b.From) {
				byNode[b.From][p.Key] = p
			}
		}
		out = append(out, byNode[b.From][b.Pod])
	}
	return out
}

// protected returns why m would disrupt something karpenter.sh/do-not-disrupt protects, or "". Generators' Fast
// checks cover the pods a move binds; planners check again, and also the nodes a move removes, which Fast does not,
// because stage 3 is the last to decide what runs.
func protected(s pipeline.Snapshot, m pipeline.Move) string {
	for _, b := range m.Bindings {
		if n, ok := s.Node(b.From); ok && n.DoNotDisrupt {
			return "do-not-disrupt node " + b.From
		}
	}
	for _, p := range movedPods(s, m) {
		if p.DoNotDisrupt {
			return "do-not-disrupt pod " + p.Key.String()
		}
	}
	for _, r := range m.Remove {
		if n, ok := s.Node(r); ok && n.DoNotDisrupt {
			return "do-not-disrupt node " + r
		}
	}
	return ""
}

// pdbLedger counts the evictions a plan has committed against each PodDisruptionBudget.
type pdbLedger map[string]int

// pdbsOf lists, in binding order, the PodDisruptionBudget of every eviction m makes on s.
func pdbsOf(s pipeline.Snapshot, m pipeline.Move) []string {
	var out []string
	for _, p := range movedPods(s, m) {
		out = append(out, p.PDBs...)
	}
	return out
}

// exceeds returns why m's evictions on s, added to the ledger's, would take a budget past its allowance, or "".
func (l pdbLedger) exceeds(s pipeline.Snapshot, m pipeline.Move) string {
	extra := map[string]int{}
	for _, pdb := range pdbsOf(s, m) {
		extra[pdb]++
		if allowed := s.PDBAllowed(pdb); allowed >= 0 && l[pdb]+extra[pdb] > allowed {
			return fmt.Sprintf("pod disruption budget %s: %d > %d", pdb, l[pdb]+extra[pdb], allowed)
		}
	}
	return ""
}

func (l pdbLedger) spend(s pipeline.Snapshot, m pipeline.Move) {
	for _, pdb := range pdbsOf(s, m) {
		l[pdb]++
	}
}

func (l pdbLedger) clone() pdbLedger {
	out := make(pdbLedger, len(l))
	maps.Copy(out, l)
	return out
}

// claims records which planned move touches each node, so later moves on the same nodes can be declined.
type claims map[string]string

// conflict returns the ID of a planned move that touches one of nodes, or "".
func (c claims) conflict(nodes []string) string {
	for _, n := range nodes {
		if id, ok := c[n]; ok {
			return id
		}
	}
	return ""
}

func (c claims) claim(nodes []string, id string) {
	for _, n := range nodes {
		c[n] = id
	}
}

func holdsReschedulable(s pipeline.Snapshot, node string) bool {
	for _, p := range s.Pods(node) {
		if p.Reschedulable {
			return true
		}
	}
	return false
}

func sortedKeys[V any](m map[string]V) []string { return slices.Sorted(maps.Keys(m)) }
