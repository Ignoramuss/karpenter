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
	"fmt"
	"math"
	"math/rand"
	"sort"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// sequencePlanner is P4. A shift costs disruption now and saves only once its source empties, so judged alone it
// usually looks uphill. This planner chains the shifts off one source until the source empties, ends the chain in
// the source's delete, which it builds itself since no generator proposes deleting a node that is not yet empty, and
// judges the chain by its total energy. A single pool merge is a chain of its own.
type sequencePlanner struct {
	acceptAll
	env     *pipeline.Env
	w       pipeline.Weights
	horizon int
	// fast re-checks shifts on chain forks. It is private so speculative checks do not count toward the shared
	// checker's statistics.
	fast *pipeline.FastChecker
	// deletes numbers the deletes the planner builds, so their IDs stay unique across its lifetime.
	deletes int
}

var _ pipeline.Guide = (*sequencePlanner)(nil)

func (p *sequencePlanner) Name() string { return SequenceName }

// Weights returns the planner's energy weights.
func (p *sequencePlanner) Weights() pipeline.Weights { return p.w }

// chain is a run of moves judged as one.
type chain struct {
	// key is the first move's ID, which breaks ties between chains of equal energy.
	key   string
	moves []pipeline.Scored
	total float64
	// pool holds the indices of the input moves the chain uses; a synthesized delete has none.
	pool []int
}

func (c chain) touches() []string {
	seen := map[string]bool{}
	for _, m := range c.moves {
		for _, n := range m.Touches() {
			seen[n] = true
		}
	}
	return sortedKeys(seen)
}

func (p *sequencePlanner) Plan(ctx context.Context, s pipeline.Snapshot, moves []pipeline.Scored, budgets pipeline.Budgets, _ *rand.Rand) (pipeline.Plan, error) {
	plan := newPlan(p.Name(), true)
	setEnergies(p.w, moves)
	sc := scorerOf(p.env)
	chains, shifts := p.classify(s, moves, plan.Rejected)
	for _, src := range sortedKeys(shifts) {
		if err := ctx.Err(); err != nil {
			return pipeline.Plan{}, err
		}
		if c, ok := p.shiftChain(sc, s, src, moves, shifts[src], plan.Rejected); ok {
			chains = append(chains, c)
		}
	}
	sort.SliceStable(chains, func(a, b int) bool {
		if chains[a].total != chains[b].total {
			return chains[a].total < chains[b].total
		}
		return chains[a].key < chains[b].key
	})
	p.choose(sc, s, moves, chains, budgets, &plan)
	return plan, nil
}

// classify makes every downhill pool merge a chain of its own and groups single-source shifts by source.
func (p *sequencePlanner) classify(s pipeline.Snapshot, moves []pipeline.Scored, rejected map[string]string) ([]chain, map[string][]int) {
	var chains []chain
	shifts := map[string][]int{}
	for i, m := range moves {
		if reason := protected(s, m.Move); reason != "" {
			rejected[m.ID] = reason
			continue
		}
		switch m.Kind {
		case pipeline.Merge, pipeline.RightSize:
			if !(m.Energy < 0) {
				rejected[m.ID] = "not downhill"
				continue
			}
			chains = append(chains, chain{key: m.ID, moves: []pipeline.Scored{m}, total: m.Energy, pool: []int{i}})
		case pipeline.Shift:
			if src, ok := singleSource(m.Move); ok {
				shifts[src] = append(shifts[src], i)
			} else {
				rejected[m.ID] = "shift from more than one source"
			}
		default:
			rejected[m.ID] = fmt.Sprintf("unknown move kind %q", m.Kind)
		}
	}
	return chains, shifts
}

// shiftChain builds src's chain and reports whether it counts: it ends in src's delete and is downhill as a whole.
// Shifts it leaves out, or uses in a chain that does not count, are recorded as rejected.
func (p *sequencePlanner) shiftChain(sc pipeline.Scorer, s pipeline.Snapshot, src string, moves []pipeline.Scored, cands []int, rejected map[string]string) (chain, bool) {
	c, reason := p.chainFrom(sc, s, src, moves, cands)
	used := map[int]bool{}
	for _, i := range c.pool {
		used[i] = true
	}
	for _, i := range cands {
		if !used[i] {
			rejected[moves[i].ID] = "not chosen for the chain from " + src
		}
	}
	if reason == "" && !(c.total < 0) {
		reason = "chain from " + src + " is not downhill"
	}
	if reason != "" {
		rejectAll(rejected, moves, c.pool, reason)
		return chain{}, false
	}
	return c, true
}

// choose takes chains in order, skipping any that touches a node an earlier choice touches. Each chain was built on
// the snapshot; chains on disjoint nodes commute, so one runs unchanged after the earlier ones, but it is still
// re-applied and re-scored where it runs and checked against what the earlier chains spent.
func (p *sequencePlanner) choose(sc pipeline.Scorer, s pipeline.Snapshot, moves []pipeline.Scored, chains []chain, budgets pipeline.Budgets, plan *pipeline.Plan) {
	remaining := budgets.Clone()
	pdbs := pdbLedger{}
	taken := claims{}
	cur := s
	for _, c := range chains {
		touches := c.touches()
		if by := taken.conflict(touches); by != "" {
			rejectAll(plan.Rejected, moves, c.pool, "conflicts with the chain starting at "+by)
			continue
		}
		left, spent := remaining.Clone(), pdbs.clone()
		out, next, reason, budgetOnly := p.run(sc, cur, c, left, spent)
		if reason != "" {
			rejectAll(plan.Rejected, moves, c.pool, reason)
			plan.ConstrainedByBudgets = plan.ConstrainedByBudgets || budgetOnly
			continue
		}
		remaining, pdbs, cur = left, spent, next
		taken.claim(touches, c.key)
		plan.Moves = append(plan.Moves, out...)
	}
}

// run applies c's moves in order from s, spending from left and spent. It returns the moves as scored where they run
// and the state they leave, or why the chain cannot run there and whether a budget alone was why.
func (p *sequencePlanner) run(sc pipeline.Scorer, s pipeline.Snapshot, c chain, left pipeline.Budgets, spent pdbLedger) ([]pipeline.Scored, pipeline.Snapshot, string, bool) {
	var out []pipeline.Scored
	total := 0.0
	for _, m := range c.moves {
		if reason := pipeline.ExceedsBudgets(s, m, left); reason != "" {
			return nil, nil, reason, true
		}
		if reason := spent.exceeds(s, m.Move); reason != "" {
			return nil, nil, reason, false
		}
		next, err := s.Apply(m.Move)
		if err != nil {
			return nil, nil, "does not apply after earlier chains: " + err.Error(), false
		}
		scored := rescore(sc, p.w, s, m.Move)
		pipeline.SpendBudgets(s, m.Move, left)
		spent.spend(s, m.Move)
		out = append(out, scored)
		total += scored.Energy
		s = next
	}
	if !(total < 0) {
		return nil, nil, "not downhill after earlier chains", false
	}
	return out, s, "", false
}

func rejectAll(rejected map[string]string, moves []pipeline.Scored, pool []int, reason string) {
	for _, i := range pool {
		rejected[moves[i].ID] = reason
	}
}

// chainFrom builds src's chain greedily: at each step, of the shifts off src that still apply on the current fork,
// the one with the lowest energy there, until src holds no reschedulable pods or the horizon is used up. It returns
// the chain ended in src's delete, or the chain so far and why it does not end in one.
func (p *sequencePlanner) chainFrom(sc pipeline.Scorer, s pipeline.Snapshot, src string, moves []pipeline.Scored, cands []int) (chain, string) {
	var c chain
	fork := s
	pdbs := pdbLedger{}
	used := map[int]bool{}
	for len(c.pool) < p.horizon && holdsReschedulable(fork, src) {
		i, scored, next := p.nextShift(sc, fork, moves, cands, used, pdbs)
		if i < 0 {
			break
		}
		used[i] = true
		pdbs.spend(fork, moves[i].Move)
		c.pool = append(c.pool, i)
		c.moves = append(c.moves, scored)
		c.total += scored.Energy
		fork = next
	}
	if len(c.moves) == 0 {
		return c, "no shift off " + src + " applies"
	}
	c.key = c.moves[0].ID
	if holdsReschedulable(fork, src) {
		return c, fmt.Sprintf("chain from %s does not empty it within %d shifts", src, p.horizon)
	}
	del, err := p.deleteOf(fork, src)
	if err != nil {
		return c, fmt.Sprintf("cannot delete %s: %v", src, err)
	}
	// Removing the emptied node gains its price and costs the per-node disruption base; any DaemonSet load it still
	// carries leaves the potential with it.
	scored := rescore(sc, p.w, fork, del)
	c.moves = append(c.moves, scored)
	c.total += scored.Energy
	return c, ""
}

// nextShift returns, of the unused candidate shifts that apply on fork within the PDB allowances left, the one with
// the lowest energy there (ties by ID), its score and the state it leaves; -1 when none applies.
func (p *sequencePlanner) nextShift(sc pipeline.Scorer, fork pipeline.Snapshot, moves []pipeline.Scored, cands []int, used map[int]bool, pdbs pdbLedger) (int, pipeline.Scored, pipeline.Snapshot) {
	best := -1
	var bestScored pipeline.Scored
	var bestNext pipeline.Snapshot
	for _, i := range cands {
		if used[i] {
			continue
		}
		m := moves[i].Move
		next, err := fork.Apply(m)
		if err != nil || p.fast.Fast(fork, m) != nil || pdbs.exceeds(fork, m) != "" {
			continue
		}
		scored := rescore(sc, p.w, fork, m)
		if math.IsNaN(scored.Energy) {
			continue
		}
		if best < 0 || lowerEnergy(scored, bestScored) {
			best, bestScored, bestNext = i, scored, next
		}
	}
	return best, bestScored, bestNext
}

// lowerEnergy orders moves by energy, then ID.
func lowerEnergy(a, b pipeline.Scored) bool {
	if a.Energy != b.Energy {
		return a.Energy < b.Energy
	}
	return a.ID < b.ID
}

// deleteOf builds the merge that removes src once shifts have emptied it. Only a node that may be disrupted at all
// qualifies: a candidate source, with no do-not-disrupt annotation on it or on any pod it still holds.
func (p *sequencePlanner) deleteOf(s pipeline.Snapshot, src string) (pipeline.Move, error) {
	n, ok := s.Node(src)
	switch {
	case !ok:
		return pipeline.Move{}, pipeline.ErrUnknown
	case !n.Source || n.Launched:
		return pipeline.Move{}, pipeline.ErrNotSource
	case n.DoNotDisrupt:
		return pipeline.Move{}, pipeline.ErrDoNotDisrupt
	}
	for _, pod := range s.Pods(src) {
		if pod.DoNotDisrupt {
			return pipeline.Move{}, fmt.Errorf("%w: pod %s", pipeline.ErrDoNotDisrupt, pod.Key)
		}
	}
	m := pipeline.Move{Kind: pipeline.Merge, Sources: []string{src}, Remove: []string{src}, Generator: SequenceName}
	if err := p.fast.Fast(s, m); err != nil {
		return pipeline.Move{}, err
	}
	if _, err := s.Apply(m); err != nil {
		return pipeline.Move{}, err
	}
	p.deletes++
	m.ID = fmt.Sprintf("%s-delete-%s-%d", SequenceName, src, p.deletes)
	return m, nil
}

// singleSource returns the one node a shift takes pods from, or false when it takes them from several (a swap).
func singleSource(m pipeline.Move) (string, bool) {
	src := ""
	same := func(n string) bool {
		if src == "" {
			src = n
		}
		return n == src
	}
	for _, n := range m.Sources {
		if !same(n) {
			return "", false
		}
	}
	for _, b := range m.Bindings {
		if !same(b.From) {
			return "", false
		}
	}
	return src, src != ""
}
