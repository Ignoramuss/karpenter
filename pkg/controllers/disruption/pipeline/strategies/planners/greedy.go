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
	"math/rand"
	"sort"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// entropyGreedy is P1. It plans every downhill move it can, most energy saved per unit of disruption first, so the
// budgets go to the cheapest improvements. Its moves were all scored on the same snapshot, and they stay valid
// together only when they are independent, so it skips any move touching a node an earlier pick touches.
type entropyGreedy struct {
	w pipeline.Weights
}

var _ pipeline.Guide = (*entropyGreedy)(nil)

func (p *entropyGreedy) Name() string { return EntropyGreedyName }

// Weights returns the planner's energy weights.
func (p *entropyGreedy) Weights() pipeline.Weights { return p.w }

// Accept reports whether the move lowers the energy, the planner's own admission test.
func (p *entropyGreedy) Accept(_ context.Context, _ pipeline.Snapshot, m pipeline.Scored) bool {
	return p.w.Energy(m) < 0
}

// CanPass reports true: a move's energy depends on where its pods go, so its sources alone bound nothing.
func (p *entropyGreedy) CanPass(context.Context, pipeline.Snapshot, []string) bool { return true }

func (p *entropyGreedy) Plan(_ context.Context, s pipeline.Snapshot, moves []pipeline.Scored, budgets pipeline.Budgets, _ *rand.Rand) (pipeline.Plan, error) {
	plan := newPlan(p.Name(), false)
	setEnergies(p.w, moves)
	type candidate struct {
		i     int
		ratio float64
	}
	var cands []candidate
	for i, m := range moves {
		// Written as a negation so a NaN energy, from a move that does not match the snapshot, is declined too.
		if !(m.Energy < 0) {
			plan.Rejected[m.ID] = "not downhill"
			continue
		}
		if reason := protected(s, m.Move); reason != "" {
			plan.Rejected[m.ID] = reason
			continue
		}
		d := m.NormalizedDisruption
		if !(d > minDisruption) {
			d = minDisruption
		}
		cands = append(cands, candidate{i: i, ratio: -m.Energy / d})
	}
	sort.SliceStable(cands, func(a, b int) bool {
		if cands[a].ratio != cands[b].ratio {
			return cands[a].ratio > cands[b].ratio
		}
		return moves[cands[a].i].ID < moves[cands[b].i].ID
	})
	remaining := budgets.Clone()
	pdbs := pdbLedger{}
	taken := claims{}
	for _, c := range cands {
		m := moves[c.i]
		touches := m.Touches()
		if by := taken.conflict(touches); by != "" {
			plan.Rejected[m.ID] = "conflicts with " + by
			continue
		}
		if reason := pipeline.ExceedsBudgets(s, m, remaining); reason != "" {
			plan.Rejected[m.ID] = reason
			plan.ConstrainedByBudgets = true
			continue
		}
		if reason := pdbs.exceeds(s, m.Move); reason != "" {
			plan.Rejected[m.ID] = reason
			continue
		}
		pipeline.SpendBudgets(s, m.Move, remaining)
		pdbs.spend(s, m.Move)
		taken.claim(touches, m.ID)
		plan.Moves = append(plan.Moves, m)
	}
	return plan, nil
}
