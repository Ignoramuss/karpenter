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

package moves

import (
	"context"
	"fmt"
	"math"
	"math/rand"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

const (
	randomShiftName = "random-shift"
	greedyShiftName = "greedy-shift"
)

// improvement is how far below zero a potential change must be to count as a drop, so floating-point noise on a
// neutral move is not mistaken for progress.
const improvement = 1e-12

// randomShiftParams configure random-shift (G3).
type randomShiftParams struct {
	// MaxPodsPerMove is m: each move takes 1 to m pods. Default 4.
	MaxPodsPerMove *int `json:"maxPodsPerMove"`
	// Full confirms each Fast-feasible shift with a Full simulation while the budget allows one. Default false.
	Full bool `json:"full"`
}

func newRandomShift(_ *pipeline.Env, p pipeline.Params) (pipeline.MoveGenerator, error) {
	var raw randomShiftParams
	if err := decode(randomShiftName, p, &raw); err != nil {
		return nil, err
	}
	maxPods, err := positiveInt(randomShiftName, "maxPodsPerMove", raw.MaxPodsPerMove, 4)
	if err != nil {
		return nil, err
	}
	return &randomShift{maxPods: maxPods, full: raw.Full, own: &pipeline.FastChecker{}, ids: ids{prefix: randomShiftName}}, nil
}

// randomShift is G3: move a random handful of pods from a random source to a random fuller destination. Moving load
// uphill is what pushes nodes toward empty or full, so the destination must already be more utilized than the source;
// beyond that the move is unguided, leaving all judgment to the planner.
type randomShift struct {
	maxPods int
	full    bool
	own     *pipeline.FastChecker
	ids     ids
}

func (g *randomShift) Name() string { return randomShiftName }

// Generate emits Fast-feasible shifts, each at most once per call. With full set, a Full rejection drops the move;
// once the Full budget is spent (or no Feasibility is available) moves stand on Fast alone, since the budget, not the
// generator, decides how many simulations a cycle affords. A shift binds to one destination, so Full can only confirm
// or reject it, never re-place it.
func (g *randomShift) Generate(ctx context.Context, s pipeline.Snapshot, sources, destinations []pipeline.NodeView, rng *rand.Rand, b *pipeline.SearchBudget) ([]pipeline.Move, error) {
	b = budgetOf(b)
	pairs := newShiftPairs(s, sources, destinations)
	if pairs.pairs == 0 {
		return nil, nil
	}
	fast := gate(b, g.own)
	seen := map[string]bool{}
	var out []pipeline.Move
	for a := newAttempts(b); a.next(); {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		from, to := pairs.sample(rng)
		n := len(from.pods)
		count := 1 + rng.Intn(min(g.maxPods, n))
		pick := rng.Perm(n)[:count]
		pods := make([]pipeline.PodView, len(pick))
		for i, j := range pick {
			pods[i] = from.pods[j]
		}
		m := shiftMove(from.name, to, pods)
		// Fast and Full are deterministic on a fixed snapshot, so a repeated draw is judged once.
		key := bindingKey(m)
		if seen[key] {
			continue
		}
		seen[key] = true
		if fast(s, m) != nil {
			continue
		}
		if g.full && b.Feasibility != nil && b.UseFull() {
			if _, err := b.Feasibility.Full(ctx, s, m); err != nil {
				continue
			}
		}
		m.ID, m.Generator = g.ids.next(), g.Name()
		out = append(out, m)
	}
	return out, nil
}

// greedyShiftParams configure greedy-shift (G4).
type greedyShiftParams struct {
	// MaxPodsPerMove caps the greedy fill. Default 4.
	MaxPodsPerMove *int `json:"maxPodsPerMove"`
}

func newGreedyShift(_ *pipeline.Env, p pipeline.Params) (pipeline.MoveGenerator, error) {
	var raw greedyShiftParams
	if err := decode(greedyShiftName, p, &raw); err != nil {
		return nil, err
	}
	maxPods, err := positiveInt(greedyShiftName, "maxPodsPerMove", raw.MaxPodsPerMove, 4)
	if err != nil {
		return nil, err
	}
	pot, err := pipeline.NewUtilEntropy(pipeline.PotentialParams{})
	if err != nil {
		return nil, fmt.Errorf("%s fallback potential, %w", greedyShiftName, err)
	}
	return &greedyShift{maxPods: maxPods, fallback: pipeline.DefaultScorer{Potential: pot}, own: &pipeline.FastChecker{},
		ids: ids{prefix: greedyShiftName}}, nil
}

// greedyShift is G4: sample (source, fuller destination) pairs as random-shift does, but spend each attempt finding
// the pair's best pod set by the run's own potential, and emit it only if it lowers the potential. It is the guided
// contrast to random-shift at equal attempts: same pairs, judgment moved from the planner into generation.
type greedyShift struct {
	maxPods  int
	fallback pipeline.Scorer
	own      *pipeline.FastChecker
	ids      ids
}

func (g *greedyShift) Name() string { return greedyShiftName }

// Generate evaluates each sampled pair once and stops early once every pair has been. Scoring uses the budget's
// Scorer, so the potential is the run's; with none, util-entropy. A run whose potential is "none" never sees a drop,
// so this generator then emits nothing.
func (g *greedyShift) Generate(ctx context.Context, s pipeline.Snapshot, sources, destinations []pipeline.NodeView, rng *rand.Rand, b *pipeline.SearchBudget) ([]pipeline.Move, error) {
	b = budgetOf(b)
	pairs := newShiftPairs(s, sources, destinations)
	if pairs.pairs == 0 {
		return nil, nil
	}
	scorer := b.Scorer
	if scorer == nil {
		scorer = g.fallback
	}
	fast := gate(b, g.own)
	evaluated := map[[2]string]bool{}
	var out []pipeline.Move
	for a := newAttempts(b); len(evaluated) < pairs.pairs && a.next(); {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		from, to := pairs.sample(rng)
		if evaluated[[2]string{from.name, to}] {
			continue
		}
		evaluated[[2]string{from.name, to}] = true
		best := g.best(pairEval{s: s, from: from, to: to, scorer: scorer, fast: g.own})
		if !best.ok || best.dp >= -improvement || fast(s, best.move) != nil {
			continue
		}
		m := best.move
		m.ID, m.Generator = g.ids.next(), g.Name()
		out = append(out, m)
	}
	return out, nil
}

// candidate is one pod set for a pair, with its ΔΦ. ok is false when the set fails Fast or its ΔΦ is undefined.
type candidate struct {
	move pipeline.Move
	dp   float64
	ok   bool
}

// better orders candidates by ΔΦ, then by size, since a pod moved for no gain is pure disruption. Earlier candidates
// win exact ties, and pods are tried in key order, so the choice is deterministic.
func better(a, b candidate) bool {
	if !a.ok {
		return false
	}
	if !b.ok {
		return true
	}
	if a.dp != b.dp {
		return a.dp < b.dp
	}
	return len(a.move.Bindings) < len(b.move.Bindings)
}

// pairEval scores pod sets for one (source, destination) pair. Its Fast is the generator's own checker, so the many
// speculative sets tried here do not count toward the shared checker's statistics.
type pairEval struct {
	s      pipeline.Snapshot
	from   shiftSource
	to     string
	scorer pipeline.Scorer
	fast   *pipeline.FastChecker
}

func (e pairEval) eval(pods []pipeline.PodView) candidate {
	m := shiftMove(e.from.name, e.to, pods)
	if e.fast.Fast(e.s, m) != nil {
		return candidate{move: m}
	}
	dp := e.scorer.Score(e.s, m).DeltaPotential
	return candidate{move: m, dp: dp, ok: !math.IsNaN(dp)}
}

// best returns the pair's best candidate among every single pod and the greedy fill.
func (g *greedyShift) best(e pairEval) candidate {
	singles := make([]candidate, len(e.from.pods))
	var best candidate
	for i, p := range e.from.pods {
		singles[i] = e.eval([]pipeline.PodView{p})
		if better(singles[i], best) {
			best = singles[i]
		}
	}
	if fill := g.fill(e, singles); better(fill, best) {
		best = fill
	}
	return best
}

// fill grows a pod set one pod at a time, adding the pod whose addition most lowers ΔΦ while Fast still passes, until
// no addition lowers it or the set holds maxPodsPerMove pods. The singles are its first step; later steps rescore the
// grown set, since ΔΦ is not additive.
func (g *greedyShift) fill(e pairEval, singles []candidate) candidate {
	var chosen []pipeline.PodView
	used := make([]bool, len(e.from.pods))
	cur := candidate{ok: true} // the empty set changes nothing
	for len(chosen) < g.maxPods {
		pick, next := -1, cur
		for i, p := range e.from.pods {
			if used[i] {
				continue
			}
			c := singles[i]
			if len(chosen) > 0 {
				c = e.eval(append(append([]pipeline.PodView(nil), chosen...), p))
			}
			if c.ok && c.dp < next.dp-improvement {
				pick, next = i, c
			}
		}
		if pick < 0 {
			break
		}
		chosen, used[pick], cur = append(chosen, e.from.pods[pick]), true, next
	}
	if len(chosen) == 0 {
		return candidate{}
	}
	return cur
}
