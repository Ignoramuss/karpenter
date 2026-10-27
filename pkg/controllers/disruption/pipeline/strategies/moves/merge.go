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
	"math/rand"
	"slices"
	"sort"
	"strings"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

const randomMergeName = "random-merge"

// randomMergeParams configure random-merge (G2).
type randomMergeParams struct {
	// MaxSources is K: each attempt empties k sources, k drawn uniformly from [1, K]. Default 3.
	MaxSources *int `json:"maxSources"`
	// Launch lets a merge whose pods do not all fit existing destinations ask Full feasibility for Karpenter's own
	// placement, which may add one replacement node. Default true.
	Launch *bool `json:"launch"`
	// MaxMoves caps the moves one Generate call emits. Default 50.
	MaxMoves *int `json:"maxMoves"`
	// Confirm sends every merge through Full feasibility before emitting it, first-fit placed or not, so only merges
	// Karpenter's scheduler confirms reach the planner, each at the cost of one Full simulation. Default false: a
	// first-fit merge is emitted on Fast alone and the executor confirms it if it runs.
	Confirm *bool `json:"confirm"`
}

func newRandomMerge(_ *pipeline.Env, p pipeline.Params) (pipeline.MoveGenerator, error) {
	var raw randomMergeParams
	if err := decode(randomMergeName, p, &raw); err != nil {
		return nil, err
	}
	g := &randomMerge{launch: true, own: &pipeline.FastChecker{}, ids: ids{prefix: randomMergeName}}
	if raw.Launch != nil {
		g.launch = *raw.Launch
	}
	if raw.Confirm != nil {
		g.confirm = *raw.Confirm
	}
	var err error
	if g.maxSources, err = positiveInt(randomMergeName, "maxSources", raw.MaxSources, 3); err != nil {
		return nil, err
	}
	if g.maxMoves, err = positiveInt(randomMergeName, "maxMoves", raw.MaxMoves, 50); err != nil {
		return nil, err
	}
	return g, nil
}

// randomMerge is G2: the random counterpart of upstream multi-node consolidation. Each attempt draws a random set of
// sources and first-fits their pods, largest first, into the destinations taken in a random order. A set whose pods
// all fit becomes a delete-only merge; one that does not may still merge onto a replacement node, which only the live
// scheduler can choose, so that path costs one Full simulation.
type randomMerge struct {
	maxSources int
	launch     bool
	confirm    bool
	maxMoves   int
	own        *pipeline.FastChecker
	ids        ids
}

func (g *randomMerge) Name() string { return randomMergeName }

// mergeState is one Generate call's working set.
type mergeState struct {
	s     pipeline.Snapshot
	dests []pipeline.NodeView
	// pods holds each mergeable source's reschedulable pods.
	pods map[string][]pipeline.PodView
	// fits caches the per-placement constraint probe, which depends only on the pod and destination.
	fits map[placement]bool
	own  *pipeline.FastChecker
}

type placement struct {
	pod  pipeline.PodKey
	dest string
}

//nolint:gocyclo
func (g *randomMerge) Generate(ctx context.Context, s pipeline.Snapshot, sources, destinations []pipeline.NodeView, rng *rand.Rand, b *pipeline.SearchBudget) ([]pipeline.Move, error) {
	b = budgetOf(b)
	st := &mergeState{s: s, pods: map[string][]pipeline.PodView{}, fits: map[placement]bool{}, own: g.own}
	// Sources that can never be emptied are filtered once rather than drawn and skipped: a do-not-disrupt node or
	// pod pins the node in place.
	var cands []string
	for _, n := range unique(sources) {
		if pods, ok := mergeable(s, n); ok {
			cands = append(cands, n.Name)
			st.pods[n.Name] = pods
		}
	}
	for _, d := range unique(destinations) {
		if d.Destination {
			st.dests = append(st.dests, d)
		}
	}
	if len(cands) == 0 {
		return nil, nil
	}
	fast := gate(b, g.own)
	emitted := map[string]bool{}
	simulated := map[string]bool{}
	var out []pipeline.Move
	emit := func(m pipeline.Move, key string) {
		m.ID, m.Generator = g.ids.next(), g.Name()
		out = append(out, m)
		emitted[key] = true
	}
	for a := newAttempts(b); len(out) < g.maxMoves && a.next(); {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		k := 1 + rng.Intn(min(g.maxSources, len(cands)))
		set := make([]string, k)
		for i, j := range rng.Perm(len(cands))[:k] {
			set[i] = cands[j]
		}
		sort.Strings(set)
		key := strings.Join(set, ",")
		if emitted[key] || !st.pdbAllows(set) {
			continue
		}
		placed, ok := st.firstFit(set, rng)
		fits := ok && fast(s, placed) == nil
		if fits && !g.confirm {
			emit(placed, key)
			continue
		}
		// A set first-fit could not place, or placed in a way Fast rejects (anti-affinity among pods landing together),
		// may still merge where Karpenter's scheduler puts it, possibly onto one replacement; with confirm, a set it did
		// place is checked the same way. Full is deterministic on a fixed snapshot, so a set it already judged is not
		// simulated again.
		if (!fits && !g.launch) || b.Feasibility == nil || simulated[key] || !b.UseFull() {
			continue
		}
		simulated[key] = true
		probe := pipeline.Move{Kind: pipeline.Merge, Sources: set, Remove: append([]string(nil), set...), Generator: g.Name()}
		m, err := b.Feasibility.Full(ctx, s, probe)
		if err != nil || m.Kind != pipeline.Merge || !sameNodes(m.Sources, set) || !sameNodes(m.Remove, set) || (!g.launch && len(m.Launch) > 0) {
			continue
		}
		m.Sources, m.Remove = set, append([]string(nil), set...)
		m.SortBindings()
		// The simulation's placements must still pass the hard constraints Fast enforces (do-not-disrupt, PDB
		// allowances), and Fast confirms it placed every pod.
		if fast(s, m) == nil {
			emit(m, key)
		}
	}
	return out, nil
}

// mergeable reports whether a source could ever be emptied, and returns its reschedulable pods.
func mergeable(s pipeline.Snapshot, n pipeline.NodeView) ([]pipeline.PodView, bool) {
	if !canLose(n) {
		return nil, false
	}
	var pods []pipeline.PodView
	for _, p := range s.Pods(n.Name) {
		if !p.Reschedulable {
			continue
		}
		if p.DoNotDisrupt {
			return nil, false
		}
		pods = append(pods, p)
	}
	return pods, true
}

// pdbAllows reports whether evicting every reschedulable pod of the set stays within each PDB's allowance. No
// placement can fix a set that fails, so it is skipped before any simulation is spent on it.
func (st *mergeState) pdbAllows(set []string) bool {
	evictions := map[string]int{}
	for _, n := range set {
		for _, p := range st.pods[n] {
			for _, pdb := range p.PDBs {
				evictions[pdb]++
				if allowed := st.s.PDBAllowed(pdb); allowed >= 0 && evictions[pdb] > allowed {
					return false
				}
			}
		}
	}
	return true
}

// firstFit places the set's pods, largest first, on the first destination in a random order with room and whose
// constraints admit the pod. It reports false as soon as one pod fits nowhere.
func (st *mergeState) firstFit(set []string, rng *rand.Rand) (pipeline.Move, bool) {
	pods := st.largestFirst(set)
	order := st.shuffledDestinations(set, rng)
	m := pipeline.Move{Kind: pipeline.Merge, Sources: set, Remove: append([]string(nil), set...)}
	added := map[string]pipeline.Resources{}
	for _, p := range pods {
		placed := false
		for _, d := range order {
			load := st.s.Load(d.Name).Add(added[d.Name]).Add(p.pod.Requests)
			if !load.Fits(d.Capacity) || !st.admits(p.pod, p.from, d.Name) {
				continue
			}
			added[d.Name] = added[d.Name].Add(p.pod.Requests)
			m.Bindings = append(m.Bindings, pipeline.Binding{Pod: p.pod.Key, From: p.from, Target: pipeline.ExistingTarget(d.Name)})
			placed = true
			break
		}
		if !placed {
			return pipeline.Move{}, false
		}
	}
	m.SortBindings()
	return m, true
}

// pending is a pod waiting for a place, with the source it leaves.
type pending struct {
	pod  pipeline.PodView
	from string
}

// largestFirst returns the set's reschedulable pods by requests descending, CPU then memory, ties by key: the usual
// first-fit-decreasing order, which places the hard pods while there is still room.
func (st *mergeState) largestFirst(set []string) []pending {
	var pods []pending
	for _, n := range set {
		for _, p := range st.pods[n] {
			pods = append(pods, pending{pod: p, from: n})
		}
	}
	sort.Slice(pods, func(i, j int) bool {
		a, b := pods[i].pod.Requests, pods[j].pod.Requests
		if a[pipeline.CPU] != b[pipeline.CPU] {
			return a[pipeline.CPU] > b[pipeline.CPU]
		}
		if a[pipeline.Memory] != b[pipeline.Memory] {
			return a[pipeline.Memory] > b[pipeline.Memory]
		}
		return podKeyLess(pods[i].pod.Key, pods[j].pod.Key)
	})
	return pods
}

// shuffledDestinations returns the destinations outside the set in a random order.
func (st *mergeState) shuffledDestinations(set []string, rng *rand.Rand) []pipeline.NodeView {
	order := make([]pipeline.NodeView, 0, len(st.dests))
	for _, d := range st.dests {
		if !slices.Contains(set, d.Name) {
			order = append(order, d)
		}
	}
	rng.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	return order
}

// admits reports whether a destination's taints, selectors, volume zones and anti-affinity admit a pod, by Fast on a
// one-pod move. Capacity is tracked by the caller; anti-affinity among the pods a merge lands together is left to the
// final check of the whole move.
func (st *mergeState) admits(p pipeline.PodView, from, dest string) bool {
	k := placement{pod: p.Key, dest: dest}
	if ok, seen := st.fits[k]; seen {
		return ok
	}
	probe := shiftMove(from, dest, []pipeline.PodView{p})
	ok := st.own.Fast(st.s, probe) == nil
	st.fits[k] = ok
	return ok
}

// sameNodes reports whether a holds exactly the names in sorted.
func sameNodes(a, sorted []string) bool {
	x := slices.Clone(a)
	slices.Sort(x)
	return slices.Equal(x, sorted)
}
