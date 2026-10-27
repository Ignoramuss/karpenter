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

// Package moves holds the snapshot-level stage 2 move generators, registered by name on import:
//
//	G2  random-merge  empty k random sources (1 <= k <= K) first-fit into a random order of destinations, falling back
//	                  to Full feasibility for Karpenter's own placement and replacement node
//	G3  random-shift  move 1 to m random pods from a source to a random fuller destination
//	G4  greedy-shift  per sampled (source, fuller destination) pair, the pod set that most lowers the potential
//	G5  swap          exchange a CPU-heavy pod of a CPU-stranded node for a memory-heavy pod of a memory-stranded one
//
// Params, with defaults: random-merge {maxSources: 3, launch: true, maxMoves: 50}, random-shift {maxPodsPerMove: 4,
// full: false}, greedy-shift {maxPodsPerMove: 4}, swap {strandingGap: 0.3}. Unknown keys and out-of-range values fail
// construction. Binaries that run these strategies import this package for its registrations.
//
// They are deliberately simple and random, so experiments can compare them with the upstream heuristics at equal
// search budget. Every emitted move passes the budget's Fast feasibility, which enforces do-not-disrupt and PDB
// allowances; speculative checks while searching use a private FastChecker so they do not count toward the shared
// checker's statistics. Randomness comes only from the rng passed to Generate, and no map iteration reaches an
// output, so a seed reproduces a run byte for byte.
package moves

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strings"
	"time"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// DefaultMaxAttempts bounds one Generate call when the search budget sets no MaxAttempts. Random generators sample
// until the budget says stop, and a deadline alone cannot stop them on a clock that only advances between cycles (the
// simulator's), so without this cap a config with no search limits would never return.
const DefaultMaxAttempts = 1000

func init() {
	pipeline.RegisterMoveGenerator(randomMergeName, newRandomMerge)
	pipeline.RegisterMoveGenerator(randomShiftName, newRandomShift)
	pipeline.RegisterMoveGenerator(greedyShiftName, newGreedyShift)
	pipeline.RegisterMoveGenerator(swapName, newSwap)
}

// attempts spends the search budget one generation attempt at a time.
type attempts struct {
	b     *pipeline.SearchBudget
	n     int
	limit int
}

func newAttempts(b *pipeline.SearchBudget) *attempts {
	a := &attempts{b: b}
	if b.MaxAttempts == 0 {
		a.limit = DefaultMaxAttempts
	}
	return a
}

// next reports whether another attempt may run. The cap is checked first so a capped call does not spend an attempt
// it will not use.
func (a *attempts) next() bool {
	if a.limit > 0 && a.n >= a.limit {
		return false
	}
	if !a.b.Next() {
		return false
	}
	a.n++
	return true
}

// budgetOf tolerates a nil budget, which callers outside the pipeline method may pass; it then bounds work by
// DefaultMaxAttempts alone.
func budgetOf(b *pipeline.SearchBudget) *pipeline.SearchBudget {
	if b == nil {
		return pipeline.NewSearchBudget(0, 0, time.Time{}, nil)
	}
	return b
}

// gate returns the Fast check every emitted move must pass: the budget's, which may be stricter than the snapshot
// checker, or the generator's own FastChecker when the budget carries no Feasibility.
func gate(b *pipeline.SearchBudget, own *pipeline.FastChecker) func(pipeline.Snapshot, pipeline.Move) error {
	if b.Feasibility != nil {
		return b.Feasibility.Fast
	}
	return own.Fast
}

// ids issues move IDs that are unique for the generator's lifetime, so moves from successive cycles never collide in
// the decision log.
type ids struct {
	prefix string
	n      int
}

func (i *ids) next() string {
	i.n++
	return fmt.Sprintf("%s-%d", i.prefix, i.n)
}

// canLose reports whether pods may be taken off a node at all.
func canLose(n pipeline.NodeView) bool { return n.Source && !n.DoNotDisrupt }

// movable returns the pods a move may take off a node, in key order: reschedulable and not do-not-disrupt.
func movable(s pipeline.Snapshot, node string) []pipeline.PodView {
	var out []pipeline.PodView
	for _, p := range s.Pods(node) {
		if p.Reschedulable && !p.DoNotDisrupt {
			out = append(out, p)
		}
	}
	return out
}

// unique drops repeated nodes, keeping the first occurrence, so a selector that lists a node twice cannot bias draws.
func unique(views []pipeline.NodeView) []pipeline.NodeView {
	seen := make(map[string]bool, len(views))
	out := make([]pipeline.NodeView, 0, len(views))
	for _, v := range views {
		if !seen[v.Name] {
			seen[v.Name] = true
			out = append(out, v)
		}
	}
	return out
}

func podKeyLess(a, b pipeline.PodKey) bool {
	if a.Namespace != b.Namespace {
		return a.Namespace < b.Namespace
	}
	return a.Name < b.Name
}

// rankedNode is a destination with its dominant utilization.
type rankedNode struct {
	name string
	util float64
}

// rankDestinations returns the destinations ordered by dominant utilization, ties by name, so the nodes fuller than
// any given utilization form a suffix.
func rankDestinations(s pipeline.Snapshot, dests []pipeline.NodeView) []rankedNode {
	var out []rankedNode
	for _, d := range unique(dests) {
		if d.Destination {
			out = append(out, rankedNode{name: d.Name, util: s.Load(d.Name).DominantShare(d.Capacity)})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].util != out[j].util {
			return out[i].util < out[j].util
		}
		return out[i].name < out[j].name
	})
	return out
}

// fuller returns the ranked destinations whose utilization is strictly above util. A node is never fuller than
// itself, so a source is excluded from its own targets.
func fuller(ranked []rankedNode, util float64) []rankedNode {
	return ranked[sort.Search(len(ranked), func(i int) bool { return ranked[i].util > util }):]
}

// shiftSource is a node a shift may take pods from, with the fuller destinations it may send them to.
type shiftSource struct {
	name   string
	pods   []pipeline.PodView
	fuller []rankedNode
}

// shiftPairs samples (source, fuller destination) pairs. Sources with nothing movable or no fuller destination are
// dropped up front: drawing them would only waste attempts, and conditioned on a usable draw the distribution is the
// same as drawing every source and skipping.
type shiftPairs struct {
	froms []shiftSource
	pairs int
}

func newShiftPairs(s pipeline.Snapshot, sources, dests []pipeline.NodeView) shiftPairs {
	ranked := rankDestinations(s, dests)
	var p shiftPairs
	for _, n := range unique(sources) {
		if !canLose(n) {
			continue
		}
		pods := movable(s, n.Name)
		up := fuller(ranked, s.Load(n.Name).DominantShare(n.Capacity))
		if len(pods) == 0 || len(up) == 0 {
			continue
		}
		p.froms = append(p.froms, shiftSource{name: n.Name, pods: pods, fuller: up})
		p.pairs += len(up)
	}
	return p
}

// sample draws a source uniformly, then one of its fuller destinations uniformly.
func (p shiftPairs) sample(rng *rand.Rand) (shiftSource, string) {
	from := p.froms[rng.Intn(len(p.froms))]
	return from, from.fuller[rng.Intn(len(from.fuller))].name
}

// shiftMove moves pods off one node onto another.
func shiftMove(from, to string, pods []pipeline.PodView) pipeline.Move {
	m := pipeline.Move{Kind: pipeline.Shift, Sources: []string{from}}
	for _, p := range pods {
		m.Bindings = append(m.Bindings, pipeline.Binding{Pod: p.Key, From: from, Target: pipeline.ExistingTarget(to)})
	}
	m.SortBindings()
	return m
}

// bindingKey identifies a move by what it does, for duplicate detection: every pod and where it goes.
func bindingKey(m pipeline.Move) string {
	var sb strings.Builder
	for _, b := range m.Bindings {
		fmt.Fprintf(&sb, "%s>%s#%d;", b.Pod, b.Target.Node, b.Target.Launch)
	}
	return sb.String()
}

func finite(xs ...float64) bool {
	for _, x := range xs {
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return false
		}
	}
	return true
}

// positiveInt applies a default to an optional count parameter. Params are pointers so an explicit 0 is rejected
// rather than silently read as "use the default".
func positiveInt(strategy, name string, v *int, def int) (int, error) {
	if v == nil {
		return def, nil
	}
	if *v < 1 {
		return 0, fmt.Errorf("%s %s %d, want at least 1", strategy, name, *v)
	}
	return *v, nil
}

func decode(strategy string, p pipeline.Params, out any) error {
	if err := p.Decode(out); err != nil {
		return fmt.Errorf("%s params, %w", strategy, err)
	}
	return nil
}
