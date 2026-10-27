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
	"math"
	"sort"

	disruptionutils "sigs.k8s.io/karpenter/pkg/utils/disruption"
)

// DefaultScorer is the shared disruption, savings and potential model.
//
// Disruption is upstream's: each evicted pod costs max(0, EvictionCost), and each removed node adds the per-node
// base cost, so a merge's disruption equals the sum of its sources' RescheduleDisruptionCost, the numerator Balanced
// scores. Savings are immediate: removed node prices minus launched node prices. Both are normalized by the totals of
// the NodePools the move's sources belong to.
type DefaultScorer struct {
	Potential Potential
}

//nolint:gocyclo
func (d DefaultScorer) Score(s Snapshot, m Move) Scored {
	sc := Scored{Move: m}
	if d.Potential != nil {
		sc.DeltaPotential = d.Potential.Delta(s, m)
	}
	costs := map[PodKey]float64{}
	for _, b := range m.Bindings {
		if _, ok := costs[b.Pod]; ok {
			continue
		}
		for _, p := range s.Pods(b.From) {
			if p.Key == b.Pod {
				costs[b.Pod] = math.Max(0, p.EvictionCost)
				break
			}
		}
	}
	keys := make([]PodKey, 0, len(costs))
	for k := range costs {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
	for _, k := range keys {
		sc.Disruption += costs[k]
	}
	pools := map[string]bool{}
	for _, r := range m.Remove {
		if n, ok := s.Node(r); ok {
			sc.Disruption += disruptionutils.PerNodeBaseDisruptionCost
			sc.SavingsPerHour += n.Price
			pools[n.NodePool] = true
		}
	}
	for _, l := range m.Launch {
		sc.SavingsPerHour -= l.Price
	}
	for _, src := range m.Sources {
		if n, ok := s.Node(src); ok {
			pools[n.NodePool] = true
		}
	}
	names := make([]string, 0, len(pools))
	for p := range pools {
		names = append(names, p)
	}
	sort.Strings(names)
	var t Totals
	for _, p := range names {
		pt := s.Totals(p)
		t.TotalCost += pt.TotalCost
		t.TotalDisruptionCost += pt.TotalDisruptionCost
	}
	if t.TotalCost > 0 {
		sc.NormalizedSavings = sc.SavingsPerHour / t.TotalCost
	}
	if t.TotalDisruptionCost > 0 {
		sc.NormalizedDisruption = sc.Disruption / t.TotalDisruptionCost
	}
	return sc
}

// Weights are the planning energy's coefficients: ΔE = Alpha ΔΦ + Lambda D̂ - Mu Ŝ.
type Weights struct {
	Alpha  float64 `json:"alpha"`
	Lambda float64 `json:"lambda"`
	Mu     float64 `json:"mu"`
}

// Energy returns ΔE for a scored move.
func (w Weights) Energy(sc Scored) float64 {
	return w.Alpha*sc.DeltaPotential + w.Lambda*sc.NormalizedDisruption - w.Mu*sc.NormalizedSavings
}
