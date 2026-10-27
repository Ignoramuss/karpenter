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

package planners

import (
	"context"
	"math"
	"math/rand"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// annealPlan is P2: simulated annealing over move sequences on the snapshot. Each move in a sequence is scored on the
// state the earlier moves leave, so a move that pays off only after another (the shift that lets a node empty) can
// be found, while the uphill steps on the way are taken only in simulation: nothing runs unless the best sequence
// seen is downhill as a whole.
type annealPlan struct {
	acceptAll
	env              *pipeline.Env
	w                pipeline.Weights
	t0, cooling      float64
	unit             string
	steps, maxLength int
	// fast re-checks moves on forks. It is private so speculative checks do not count toward the shared checker's
	// statistics.
	fast *pipeline.FastChecker
}

var _ pipeline.Guide = (*annealPlan)(nil)

func (p *annealPlan) Name() string { return AnnealPlanName }

// Weights returns the planner's energy weights.
func (p *annealPlan) Weights() pipeline.Weights { return p.w }

// frame is one state of the search: the fork the sequence so far leaves, and what that sequence has spent.
type frame struct {
	snap    pipeline.Snapshot
	budgets pipeline.Budgets
	pdbs    pdbLedger
	total   float64
	// move led here from the frame below, scored on that frame's fork; index is its position in the input, -1 for
	// the root.
	move  pipeline.Scored
	index int
}

// extend appends m to the sequence ending at f. It reports false when m cannot follow, and, separately, whether a
// budget alone stopped a move that would have lowered the energy.
func (p *annealPlan) extend(sc pipeline.Scorer, f frame, m pipeline.Scored, index int) (next frame, ok, constrained bool) {
	snap, err := f.snap.Apply(m.Move)
	if err != nil {
		return frame{}, false, false
	}
	if p.fast.Fast(f.snap, m.Move) != nil || f.pdbs.exceeds(f.snap, m.Move) != "" {
		return frame{}, false, false
	}
	scored := rescore(sc, p.w, f.snap, m.Move)
	if math.IsNaN(scored.Energy) {
		return frame{}, false, false
	}
	if pipeline.ExceedsBudgets(f.snap, scored, f.budgets) != "" {
		return frame{}, false, scored.Energy < 0
	}
	budgets := f.budgets.Clone()
	pipeline.SpendBudgets(f.snap, m.Move, budgets)
	pdbs := f.pdbs.clone()
	pdbs.spend(f.snap, m.Move)
	return frame{snap: snap, budgets: budgets, pdbs: pdbs, total: f.total + scored.Energy, move: scored, index: index}, true, false
}

func (p *annealPlan) Plan(ctx context.Context, s pipeline.Snapshot, moves []pipeline.Scored, budgets pipeline.Budgets, rng *rand.Rand) (pipeline.Plan, error) {
	plan := newPlan(p.Name(), true)
	setEnergies(p.w, moves)
	pool := p.pool(s, moves, budgets, &plan)
	if len(pool) == 0 {
		return plan, nil
	}
	best, constrained, err := p.search(ctx, s, moves, pool, budgets, rng)
	if err != nil {
		return pipeline.Plan{}, err
	}
	plan.ConstrainedByBudgets = plan.ConstrainedByBudgets || constrained
	plan.Moves = best
	chosen := map[string]bool{}
	for _, m := range best {
		chosen[m.ID] = true
	}
	reason := "not in the best sequence"
	if len(best) == 0 {
		reason = "no net-downhill sequence found"
	}
	for _, i := range pool {
		if id := moves[i].ID; !chosen[id] {
			plan.Rejected[id] = reason
		}
	}
	return plan, nil
}

// pool returns the indices of the moves a sequence may use. Budgets only shrink along a sequence, so a move that does
// not fit them on the snapshot never will; forks keep every node and pod view, so do-not-disrupt is settled here too.
func (p *annealPlan) pool(s pipeline.Snapshot, moves []pipeline.Scored, budgets pipeline.Budgets, plan *pipeline.Plan) []int {
	var pool []int
	for i, m := range moves {
		if reason := protected(s, m.Move); reason != "" {
			plan.Rejected[m.ID] = reason
			continue
		}
		if reason := pipeline.ExceedsBudgets(s, m, budgets); reason != "" {
			plan.Rejected[m.ID] = reason
			plan.ConstrainedByBudgets = plan.ConstrainedByBudgets || m.Energy < 0
			continue
		}
		pool = append(pool, i)
	}
	return pool
}

// search runs the Metropolis chain over sequences of pool moves. It returns the lowest-energy sequence it visits,
// empty unless one is downhill, and whether a budget alone stopped a proposal that would have lowered the energy.
func (p *annealPlan) search(ctx context.Context, s pipeline.Snapshot, moves []pipeline.Scored, pool []int, budgets pipeline.Budgets, rng *rand.Rand) ([]pipeline.Scored, bool, error) {
	sc := scorerOf(p.env)
	stack := []frame{{snap: s, budgets: budgets.Clone(), pdbs: pdbLedger{}, index: -1}}
	inSequence := make([]bool, len(moves))
	var best []pipeline.Scored
	bestTotal := 0.0
	constrained := false
	t := temperature(p.t0, p.unit, s)
	for range p.steps {
		if err := ctx.Err(); err != nil {
			return nil, false, err
		}
		top, length := stack[len(stack)-1], len(stack)-1
		if p.grows(length, len(pool), rng) {
			i := pick(pool, inSequence, len(pool)-length, rng)
			next, ok, blocked := p.extend(sc, top, moves[i], i)
			constrained = constrained || blocked
			if ok && metropolis(next.move.Energy, t, rng) {
				stack = append(stack, next)
				inSequence[i] = true
			}
		} else if metropolis(-top.move.Energy, t, rng) {
			inSequence[top.index] = false
			stack = stack[:len(stack)-1]
		}
		if cur := stack[len(stack)-1]; cur.total < bestTotal {
			bestTotal, best = cur.total, sequenceOf(stack)
		}
		t *= p.cooling
	}
	return best, constrained, nil
}

// grows decides whether the next proposal appends to a sequence of this length or drops its last move: always append
// to the empty sequence, never past maxLength or once every pool move is in, otherwise append with appendProbability.
func (p *annealPlan) grows(length, pool int, rng *rand.Rand) bool {
	switch {
	case length >= p.maxLength || length >= pool:
		return false
	case length == 0:
		return true
	}
	return rng.Float64() < appendProbability
}

// sequenceOf returns the moves of a search stack, in order, past the root.
func sequenceOf(stack []frame) []pipeline.Scored {
	out := make([]pipeline.Scored, 0, len(stack)-1)
	for _, f := range stack[1:] {
		out = append(out, f.move)
	}
	return out
}

// pick draws uniformly among the pool moves not yet in the sequence, of which there are free.
func pick(pool []int, inSequence []bool, free int, rng *rand.Rand) int {
	k := rng.Intn(free)
	for _, i := range pool {
		if inSequence[i] {
			continue
		}
		if k == 0 {
			return i
		}
		k--
	}
	panic("planners: pick ran past the free moves")
}

// annealOnline is P3: the Metropolis rule applied move by move on the cluster rather than on a snapshot. Each cycle
// takes the first move, in random order, that the rule accepts at the current temperature, uphill or not, and the
// temperature cools once per cycle across the planner's lifetime. An uphill move it takes really runs, so its
// disruption is paid even when a later cycle undoes it; that cost is what H4 measures against anneal-plan.
type annealOnline struct {
	acceptAll
	w       pipeline.Weights
	cooling float64
	unit    string
	// t is the temperature the next Plan call accepts at, in the planner's temperature unit.
	t float64
}

var _ pipeline.Guide = (*annealOnline)(nil)

func (p *annealOnline) Name() string { return AnnealOnlineName }

// Weights returns the planner's energy weights.
func (p *annealOnline) Weights() pipeline.Weights { return p.w }

// Temperature returns the temperature the next Plan call accepts at.
func (p *annealOnline) Temperature() float64 { return p.t }

func (p *annealOnline) Plan(_ context.Context, s pipeline.Snapshot, moves []pipeline.Scored, budgets pipeline.Budgets, rng *rand.Rand) (pipeline.Plan, error) {
	plan := newPlan(p.Name(), false)
	setEnergies(p.w, moves)
	t := temperature(p.t, p.unit, s)
	// The schedule advances once per cycle whether or not a move is taken: it tracks time on the cluster, not search
	// effort.
	p.t *= p.cooling
	remaining := budgets.Clone()
	taken := ""
	for _, i := range rng.Perm(len(moves)) {
		m := moves[i]
		if reason, budget := p.decline(s, m, t, remaining, taken, rng); reason != "" {
			plan.Rejected[m.ID] = reason
			plan.ConstrainedByBudgets = plan.ConstrainedByBudgets || budget
			continue
		}
		pipeline.SpendBudgets(s, m.Move, remaining)
		plan.Moves = []pipeline.Scored{m}
		taken = m.ID
	}
	return plan, nil
}

// decline returns why anneal-online does not take m at temperature t, or "", and whether a budget alone was why. The
// Metropolis draw comes before the budget check, so a move a budget turns away is known to have been acceptable.
func (p *annealOnline) decline(s pipeline.Snapshot, m pipeline.Scored, t float64, remaining pipeline.Budgets, taken string, rng *rand.Rand) (string, bool) {
	if taken != "" {
		return "took " + taken + " first", false
	}
	if math.IsNaN(m.Energy) {
		return "energy undefined", false
	}
	if reason := protected(s, m.Move); reason != "" {
		return reason, false
	}
	if !metropolis(m.Energy, t, rng) {
		return "rejected by the Metropolis rule", false
	}
	if reason := pipeline.ExceedsBudgets(s, m, remaining); reason != "" {
		return reason, true
	}
	// A single move has nothing to conflict with, but a multi-node merge can still exceed a PDB allowance alone.
	return (pdbLedger{}).exceeds(s, m.Move), false
}
