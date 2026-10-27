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
	"fmt"
	"math"
)

// Potentials. Each is normalized by constants of the root snapshot (Normalizer), never of the fork being valued, so
// Delta(s, m) == Value(s.Apply(m)) - Value(s) holds exactly and removing an empty node is never penalized by a
// shrinking denominator.
//
//   - load-entropy (P1): H(p) = -Σ p_i ln p_i with p_i = L_i / Σ L_k, divided by ln N0. Blind to right-sizing.
//   - util-entropy (P2): Φ = Σ w_i h(u_i), h(u) = -u ln u - (1-u) ln(1-u), w_i = price, divided by Σ w at the root.
//     Zero for empty and full nodes, largest at 50%.
//   - util-quadratic (P3): Σ w_i u_i (1 - u_i), a cheaper smooth variant of P2.
//   - none: constant zero, for savings-only planners.
//
// Multi-resource forms apply the per-node term to each of CPU, memory (and accelerators where the node has them) and
// combine by "max" or "mean"; "dominant" applies it once to the dominant share. A stranding term adds
// stranding * |u_cpu - u_mem| per node, weighted the same way.

// PotentialParams configure the utilization potentials.
type PotentialParams struct {
	// Weight is "price" (default) or "uniform".
	Weight string `json:"weight"`
	// Resource is "dominant" (default), "cpu", "memory", "max" or "mean".
	Resource string `json:"resource"`
	// Stranding weights the |u_cpu - u_mem| term; 0 disables it.
	Stranding float64 `json:"stranding"`
}

func (p PotentialParams) validate() error {
	switch p.Weight {
	case "", "price", "uniform":
	default:
		return fmt.Errorf("potential weight %q, want price or uniform", p.Weight)
	}
	switch p.Resource {
	case "", "dominant", "cpu", "memory", "max", "mean":
	default:
		return fmt.Errorf("potential resource %q, want dominant, cpu, memory, max or mean", p.Resource)
	}
	return nil
}

func binaryEntropy(u float64) float64 {
	u = clamp01(u)
	if u <= 0 || u >= 1 {
		return 0
	}
	return -u*math.Log(u) - (1-u)*math.Log(1-u)
}

func quadratic(u float64) float64 {
	u = clamp01(u)
	return u * (1 - u)
}

func clamp01(u float64) float64 {
	if math.IsNaN(u) || u < 0 {
		return 0
	}
	if u > 1 {
		return 1
	}
	return u
}

// utilPotential is P2 or P3, depending on term.
type utilPotential struct {
	name   string
	term   func(float64) float64
	params PotentialParams
}

// NewUtilEntropy returns P2.
func NewUtilEntropy(p PotentialParams) (Potential, error) {
	if err := p.validate(); err != nil {
		return nil, err
	}
	return &utilPotential{name: "util-entropy", term: binaryEntropy, params: p}, nil
}

// NewUtilQuadratic returns P3.
func NewUtilQuadratic(p PotentialParams) (Potential, error) {
	if err := p.validate(); err != nil {
		return nil, err
	}
	return &utilPotential{name: "util-quadratic", term: quadratic, params: p}, nil
}

func (u *utilPotential) Name() string { return u.name }

func (u *utilPotential) weight(n NodeView) float64 {
	if u.params.Weight == "uniform" {
		return 1
	}
	return n.Price
}

func (u *utilPotential) norm(s Snapshot) float64 {
	nz := s.Normalizer()
	z := nz.TotalPrice
	if u.params.Weight == "uniform" {
		z = float64(nz.NodeCount)
	}
	if z <= 0 {
		return 1
	}
	return z
}

// nodeTerm is the unweighted per-node contribution for a node of this capacity and load.
func (u *utilPotential) nodeTerm(capacity, load Resources) float64 {
	share := load.Share(capacity)
	var v float64
	switch u.params.Resource {
	case "cpu":
		v = u.term(share[CPU])
	case "memory":
		v = u.term(share[Memory])
	case "max", "mean":
		terms := []float64{u.term(share[CPU]), u.term(share[Memory])}
		if capacity[Accelerators] > 0 {
			terms = append(terms, u.term(share[Accelerators]))
		}
		for _, t := range terms {
			if u.params.Resource == "max" {
				v = math.Max(v, t)
			} else {
				v += t / float64(len(terms))
			}
		}
	default:
		v = u.term(load.DominantShare(capacity))
	}
	if u.params.Stranding > 0 {
		v += u.params.Stranding * math.Abs(clamp01(share[CPU])-clamp01(share[Memory]))
	}
	return v
}

func (u *utilPotential) Value(s Snapshot) float64 {
	var sum float64
	for _, n := range s.Nodes() {
		sum += u.weight(n) * u.nodeTerm(n.Capacity, s.Load(n.Name))
	}
	return sum / u.norm(s)
}

func (u *utilPotential) Delta(s Snapshot, m Move) float64 {
	ch, ok := loadChanges(s, m)
	if !ok {
		return math.NaN()
	}
	var d float64
	for _, c := range ch {
		if !c.removed {
			d += u.weight(c.view) * u.nodeTerm(c.view.Capacity, c.after)
		}
		if !c.launched {
			d -= u.weight(c.view) * u.nodeTerm(c.view.Capacity, c.before)
		}
	}
	return d / u.norm(s)
}

// loadEntropy is P1. Loads are the chosen dimension's requests (CPU by default), or the mean of the CPU and memory
// entropies for "mean".
type loadEntropy struct {
	params PotentialParams
}

// NewLoadEntropy returns P1.
func NewLoadEntropy(p PotentialParams) (Potential, error) {
	if err := p.validate(); err != nil {
		return nil, err
	}
	return &loadEntropy{params: p}, nil
}

func (l *loadEntropy) Name() string { return "load-entropy" }

func (l *loadEntropy) dims() []Dimension {
	switch l.params.Resource {
	case "memory":
		return []Dimension{Memory}
	case "mean", "max":
		return []Dimension{CPU, Memory}
	}
	return []Dimension{CPU}
}

func xlogx(x float64) float64 {
	if x <= 0 {
		return 0
	}
	return x * math.Log(x)
}

// entropy computes H from T = Σ L and S = Σ L ln L: H = ln T - S / T.
func entropy(t, sxl float64) float64 {
	if t <= 0 {
		return 0
	}
	return math.Log(t) - sxl/t
}

func (l *loadEntropy) norm(s Snapshot) float64 {
	n := s.Normalizer().NodeCount
	if n < 2 {
		return 1
	}
	return math.Log(float64(n))
}

func (l *loadEntropy) combine(hs []float64) float64 {
	var v float64
	for _, h := range hs {
		if l.params.Resource == "max" {
			v = math.Max(v, h)
		} else {
			v += h / float64(len(hs))
		}
	}
	return v
}

func (l *loadEntropy) Value(s Snapshot) float64 {
	var hs []float64
	for _, d := range l.dims() {
		var t, sxl float64
		for _, n := range s.Nodes() {
			x := s.Load(n.Name)[d]
			t += x
			sxl += xlogx(x)
		}
		hs = append(hs, entropy(t, sxl))
	}
	return l.combine(hs) / l.norm(s)
}

func (l *loadEntropy) Delta(s Snapshot, m Move) float64 {
	ch, ok := loadChanges(s, m)
	if !ok {
		return math.NaN()
	}
	var before, after []float64
	for _, d := range l.dims() {
		var t, sxl float64
		for _, n := range s.Nodes() {
			x := s.Load(n.Name)[d]
			t += x
			sxl += xlogx(x)
		}
		t2, sxl2 := t, sxl
		for _, c := range ch {
			if !c.launched {
				t2 -= c.before[d]
				sxl2 -= xlogx(c.before[d])
			}
			if !c.removed {
				t2 += c.after[d]
				sxl2 += xlogx(c.after[d])
			}
		}
		before = append(before, entropy(t, sxl))
		after = append(after, entropy(t2, sxl2))
	}
	return (l.combine(after) - l.combine(before)) / l.norm(s)
}

type nonePotential struct{}

func (nonePotential) Name() string                 { return "none" }
func (nonePotential) Value(Snapshot) float64       { return 0 }
func (nonePotential) Delta(Snapshot, Move) float64 { return 0 }

// loadChange is one node's load before and after a move.
type loadChange struct {
	view          NodeView
	before, after Resources
	launched      bool
	removed       bool
}

// loadChanges computes the loads a move leaves on each node it touches, without forking the snapshot. It reports
// false when the move references a node or pod the snapshot lacks.
//
//nolint:gocyclo
func loadChanges(s Snapshot, m Move) ([]loadChange, bool) {
	idx := map[string]int{}
	var out []loadChange
	get := func(name string) (int, bool) {
		if i, ok := idx[name]; ok {
			return i, true
		}
		for li, l := range m.Launch {
			if LaunchName(m.ID, li) == name {
				out = append(out, loadChange{view: NodeView{Name: name, Capacity: l.Capacity, Price: l.Price, Launched: true}, launched: true})
				idx[name] = len(out) - 1
				return len(out) - 1, true
			}
		}
		n, ok := s.Node(name)
		if !ok {
			return 0, false
		}
		load := s.Load(name)
		out = append(out, loadChange{view: n, before: load, after: load})
		idx[name] = len(out) - 1
		return len(out) - 1, true
	}
	// Ensure launched nodes are present even when no pod binds to them.
	for li := range m.Launch {
		if _, ok := get(LaunchName(m.ID, li)); !ok {
			return nil, false
		}
	}
	podCache := map[string]map[PodKey]Resources{}
	for _, b := range m.Bindings {
		fi, ok := get(b.From)
		if !ok {
			return nil, false
		}
		target := b.Target.Node
		if b.Target.Launch >= 0 {
			target = LaunchName(m.ID, b.Target.Launch)
		}
		ti, ok := get(target)
		if !ok {
			return nil, false
		}
		reqs, ok := podCache[b.From]
		if !ok {
			reqs = map[PodKey]Resources{}
			for _, p := range s.Pods(b.From) {
				reqs[p.Key] = p.Requests
			}
			podCache[b.From] = reqs
		}
		r, ok := reqs[b.Pod]
		if !ok {
			return nil, false
		}
		out[fi].after = out[fi].after.Sub(r)
		out[ti].after = out[ti].after.Add(r)
	}
	for _, name := range m.Remove {
		i, ok := get(name)
		if !ok {
			return nil, false
		}
		out[i].removed = true
	}
	return out, true
}
