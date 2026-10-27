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
	"sort"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

const swapName = "swap"

// swapParams configure swap (G5).
type swapParams struct {
	// StrandingGap is how far one dimension's share must exceed the other's for a node to count as stranded on it,
	// in [0, 1). Default 0.3.
	StrandingGap *float64 `json:"strandingGap"`
}

func newSwap(_ *pipeline.Env, p pipeline.Params) (pipeline.MoveGenerator, error) {
	var raw swapParams
	if err := decode(swapName, p, &raw); err != nil {
		return nil, err
	}
	g := &swap{gap: 0.3, own: &pipeline.FastChecker{}, ids: ids{prefix: swapName}}
	if raw.StrandingGap != nil {
		g.gap = *raw.StrandingGap
		if math.IsNaN(g.gap) || g.gap < 0 || g.gap >= 1 {
			return nil, fmt.Errorf("%s strandingGap %v, want a value in [0, 1)", swapName, g.gap)
		}
	}
	return g, nil
}

// swap is G5: fix CPU-versus-memory stranding, which no one-way move can, by exchanging pods. A node full on CPU but
// mostly empty of memory cannot take memory-heavy pods without first shedding CPU, and the reverse holds for a
// memory-stranded node, so each gives up the pod that most worsens its imbalance in return for the other's. Neither
// node is emptied, so the move is a shift whose two sources are also its destinations.
type swap struct {
	gap float64
	own *pipeline.FastChecker
	ids ids
}

func (g *swap) Name() string { return swapName }

// stranded is a node on one side of a swap, with the pod it would give up.
type stranded struct {
	name string
	pod  pipeline.PodView
}

// Generate evaluates each sampled (CPU-stranded, memory-stranded) pair once and stops early once every pair has been.
// A pair's swap is fixed by its nodes' extreme pods, so there is nothing more to sample for it.
func (g *swap) Generate(ctx context.Context, s pipeline.Snapshot, sources, destinations []pipeline.NodeView, rng *rand.Rand, b *pipeline.SearchBudget) ([]pipeline.Move, error) {
	b = budgetOf(b)
	cpuSide, memSide := g.classify(s, sources, destinations)
	if len(cpuSide) == 0 || len(memSide) == 0 {
		return nil, nil
	}
	fast := gate(b, g.own)
	pairs := len(cpuSide) * len(memSide)
	evaluated := map[[2]int]bool{}
	var out []pipeline.Move
	for a := newAttempts(b); len(evaluated) < pairs && a.next(); {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		i, j := rng.Intn(len(cpuSide)), rng.Intn(len(memSide))
		if evaluated[[2]int{i, j}] {
			continue
		}
		evaluated[[2]int{i, j}] = true
		cpuNode, memNode := cpuSide[i], memSide[j]
		// The exchange only helps if it sends the CPU-heavier pod to the memory-stranded node.
		if cpuPerMemory(cpuNode.pod) <= cpuPerMemory(memNode.pod) {
			continue
		}
		m := pipeline.Move{Kind: pipeline.Shift, Sources: []string{cpuNode.name, memNode.name}, Bindings: []pipeline.Binding{
			{Pod: cpuNode.pod.Key, From: cpuNode.name, Target: pipeline.ExistingTarget(memNode.name)},
			{Pod: memNode.pod.Key, From: memNode.name, Target: pipeline.ExistingTarget(cpuNode.name)},
		}}
		sort.Strings(m.Sources)
		m.SortBindings()
		// Fast checks each node against its load after the pod it sheds leaves; Apply confirms the exchange as a
		// whole, since it applies the two bindings in key order and checks capacity only at the end.
		if fast(s, m) != nil {
			continue
		}
		if _, err := s.Apply(m); err != nil {
			continue
		}
		m.ID, m.Generator = g.ids.next(), g.Name()
		out = append(out, m)
	}
	return out, nil
}

// classify splits the nodes that are both sources and destinations into CPU-stranded and memory-stranded ones, each
// with the pod it would give up: the highest CPU/memory request ratio on a CPU-stranded node, the lowest on a
// memory-stranded one. Nodes keep the sources' order, so draws index a deterministic list.
func (g *swap) classify(s pipeline.Snapshot, sources, destinations []pipeline.NodeView) (cpuSide, memSide []stranded) {
	isDest := map[string]bool{}
	for _, d := range destinations {
		if d.Destination {
			isDest[d.Name] = true
		}
	}
	for _, n := range unique(sources) {
		if !canLose(n) || !n.Destination || !isDest[n.Name] {
			continue
		}
		share := s.Load(n.Name).Share(n.Capacity)
		if !finite(share[pipeline.CPU], share[pipeline.Memory]) {
			continue
		}
		skew := share[pipeline.CPU] - share[pipeline.Memory]
		if math.Abs(skew) <= g.gap {
			continue
		}
		pick, ok := mostSkewed(movable(s, n.Name), skew > 0)
		if !ok {
			continue
		}
		if skew > 0 {
			cpuSide = append(cpuSide, stranded{name: n.Name, pod: pick})
		} else {
			memSide = append(memSide, stranded{name: n.Name, pod: pick})
		}
	}
	return cpuSide, memSide
}

// mostSkewed returns the pod with the highest CPU/memory ratio, or with cpuHeavy false the lowest, ignoring pods that
// request neither. Pods come in key order and only a strictly more extreme ratio replaces the pick, so ties go to the
// first key.
func mostSkewed(pods []pipeline.PodView, cpuHeavy bool) (pipeline.PodView, bool) {
	var pick pipeline.PodView
	found := false
	for _, p := range pods {
		if p.Requests[pipeline.CPU] <= 0 && p.Requests[pipeline.Memory] <= 0 {
			continue
		}
		r := cpuPerMemory(p)
		if !found || (cpuHeavy && r > cpuPerMemory(pick)) || (!cpuHeavy && r < cpuPerMemory(pick)) {
			pick, found = p, true
		}
	}
	return pick, found
}

// cpuPerMemory is a pod's CPU/memory request ratio; a pod requesting no memory is the most CPU-heavy. Units do not
// matter, since every pod's ratio scales by the same constant.
func cpuPerMemory(p pipeline.PodView) float64 {
	if p.Requests[pipeline.Memory] <= 0 {
		return math.Inf(1)
	}
	return p.Requests[pipeline.CPU] / p.Requests[pipeline.Memory]
}
