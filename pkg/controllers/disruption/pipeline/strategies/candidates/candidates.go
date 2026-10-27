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

// Package candidates holds the stage 1 candidate selectors that span random to heuristic, so an experiment can vary
// stage 1 while the other stages stay fixed. The upstream selector (C0) lives with the other adapters in the pipeline
// package.
//
//	C1  uniform-random      sourceFraction (0.25), destinationFraction (1.0): sources and destinations sampled
//	                        uniformly without replacement from the eligible nodes
//	C2  util-band           resource (dominant), fullThreshold (0.95): sources below the fleet mean utilization,
//	                        emptiest first; destinations above it and below fullThreshold, fullest first
//	C3  potential-weighted  sourceFraction (0.25): sources sampled without replacement in proportion to the potential
//	                        drop from emptying them; every destination
//
// A fraction f of n eligible nodes selects ceil(f * n) of them, at least one when any are eligible. Selectors return
// only nodes the snapshot marks eligible (NodeView.Source, NodeView.Destination), draw only from the rng they are
// given, and never let map iteration order reach their output, so a seed and a snapshot fix the result.
package candidates

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sort"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// Registered strategy names.
const (
	// UniformRandom is C1.
	UniformRandom = "uniform-random"
	// UtilBand is C2.
	UtilBand = "util-band"
	// PotentialWeighted is C3.
	PotentialWeighted = "potential-weighted"
)

var (
	_ pipeline.CandidateSelector = (*uniformRandom)(nil)
	_ pipeline.CandidateSelector = (*utilBand)(nil)
	_ pipeline.CandidateSelector = (*potentialWeighted)(nil)
)

// eligible splits the snapshot's nodes into sources and destinations, both in the snapshot's name order. A node may
// be both, so generators must not plan a node's pods onto itself.
func eligible(s pipeline.Snapshot) (sources, destinations []pipeline.NodeView) {
	for _, n := range s.Nodes() {
		if n.Source {
			sources = append(sources, n)
		}
		if n.Destination {
			destinations = append(destinations, n)
		}
	}
	return sources, destinations
}

// sampleSize is ceil(fraction * n), at least 1 when n > 0. The epsilon keeps a product that floating point lands just
// above an integer (0.7 * 10 = 7.000000000000001) from rounding up a whole extra node.
func sampleSize(fraction float64, n int) int {
	if n == 0 {
		return 0
	}
	k := int(math.Ceil(fraction*float64(n) - 1e-9))
	return min(max(k, 1), n)
}

func validFraction(strategy, param string, f float64) error {
	if !(f > 0 && f <= 1) {
		return fmt.Errorf("%s: %s %v, want a fraction in (0, 1]", strategy, param, f)
	}
	return nil
}

func needRNG(strategy string, rng *rand.Rand) error {
	if rng == nil {
		return fmt.Errorf("%s needs an rng", strategy)
	}
	return nil
}

// --- C1 uniform-random -----------------------------------------------------------------------------------------

type uniformParams struct {
	SourceFraction      float64 `json:"sourceFraction"`
	DestinationFraction float64 `json:"destinationFraction"`
}

type uniformRandom struct {
	sourceFraction, destinationFraction float64
}

func newUniformRandom(_ *pipeline.Env, p pipeline.Params) (pipeline.CandidateSelector, error) {
	params := uniformParams{SourceFraction: 0.25, DestinationFraction: 1}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", UniformRandom, err)
	}
	if err := validFraction(UniformRandom, "sourceFraction", params.SourceFraction); err != nil {
		return nil, err
	}
	if err := validFraction(UniformRandom, "destinationFraction", params.DestinationFraction); err != nil {
		return nil, err
	}
	return &uniformRandom{sourceFraction: params.SourceFraction, destinationFraction: params.DestinationFraction}, nil
}

func (u *uniformRandom) Name() string { return UniformRandom }

// Select draws sources, then destinations, each from one permutation of the eligible nodes in name order. Even at
// destinationFraction 1 the destinations come back shuffled: first-fit generators read them in order, so the order is
// part of what is random.
func (u *uniformRandom) Select(_ context.Context, s pipeline.Snapshot, rng *rand.Rand) ([]pipeline.NodeView, []pipeline.NodeView, error) {
	if err := needRNG(UniformRandom, rng); err != nil {
		return nil, nil, err
	}
	src, dst := eligible(s)
	sources := sampleUniform(rng, src, sampleSize(u.sourceFraction, len(src)))
	return sources, sampleUniform(rng, dst, sampleSize(u.destinationFraction, len(dst))), nil
}

func sampleUniform(rng *rand.Rand, nodes []pipeline.NodeView, k int) []pipeline.NodeView {
	if k == 0 {
		return nil
	}
	perm := rng.Perm(len(nodes))
	out := make([]pipeline.NodeView, k)
	for i := range out {
		out[i] = nodes[perm[i]]
	}
	return out
}

// --- C2 util-band ----------------------------------------------------------------------------------------------

type bandParams struct {
	Resource      string  `json:"resource"`
	FullThreshold float64 `json:"fullThreshold"`
}

type utilBand struct {
	resource      string
	fullThreshold float64
}

func newUtilBand(_ *pipeline.Env, p pipeline.Params) (pipeline.CandidateSelector, error) {
	params := bandParams{Resource: "dominant", FullThreshold: 0.95}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", UtilBand, err)
	}
	switch params.Resource {
	case "":
		params.Resource = "dominant"
	case "dominant", "cpu", "memory":
	default:
		return nil, fmt.Errorf("%s: resource %q, want dominant, cpu or memory", UtilBand, params.Resource)
	}
	if err := validFraction(UtilBand, "fullThreshold", params.FullThreshold); err != nil {
		return nil, err
	}
	return &utilBand{resource: params.Resource, fullThreshold: params.FullThreshold}, nil
}

func (u *utilBand) Name() string { return UtilBand }

// utilization is a node's load over capacity on the band's resource, and false for a node with no capacity there,
// which has no utilization to compare. Shares above 1 (an overcommitted node, or a request on a dimension the node
// lacks, which DominantShare reports as +Inf) count as full, so one such node cannot drag the fleet mean past every
// other node.
func (u *utilBand) utilization(n pipeline.NodeView, load pipeline.Resources) (float64, bool) {
	var share float64
	switch u.resource {
	case "cpu", "memory":
		d := pipeline.CPU
		if u.resource == "memory" {
			d = pipeline.Memory
		}
		if n.Capacity[d] <= 0 {
			return 0, false
		}
		share = load[d] / n.Capacity[d]
	default:
		if n.Capacity[pipeline.CPU] <= 0 && n.Capacity[pipeline.Memory] <= 0 && n.Capacity[pipeline.Accelerators] <= 0 {
			return 0, false
		}
		share = load.DominantShare(n.Capacity)
	}
	return math.Min(share, 1), true
}

type rated struct {
	node pipeline.NodeView
	util float64
}

// Select splits the fleet at its mean utilization: the unweighted mean over every node with capacity on the resource,
// eligible or not, so the band reflects the whole fleet rather than only the part this cycle may touch. It is
// deterministic and draws nothing from rng, so it leaves the rng stream of later stages unchanged.
func (u *utilBand) Select(_ context.Context, s pipeline.Snapshot, _ *rand.Rand) ([]pipeline.NodeView, []pipeline.NodeView, error) {
	var all []rated
	var sum float64
	for _, n := range s.Nodes() {
		if util, ok := u.utilization(n, s.Load(n.Name)); ok {
			all = append(all, rated{node: n, util: util})
			sum += util
		}
	}
	if len(all) == 0 {
		return nil, nil, nil
	}
	mean := sum / float64(len(all))
	var src, dst []rated
	for _, r := range all {
		if r.node.Source && r.util < mean {
			src = append(src, r)
		}
		if r.node.Destination && r.util > mean && r.util < u.fullThreshold {
			dst = append(dst, r)
		}
	}
	// Emptiest sources first, so generators that stop early drain the cheapest-to-empty nodes; fullest destinations
	// first, so first-fit packs toward full.
	sortRated(src, func(a, b float64) bool { return a < b })
	sortRated(dst, func(a, b float64) bool { return a > b })
	return nodesOf(src), nodesOf(dst), nil
}

func sortRated(rs []rated, before func(a, b float64) bool) {
	sort.Slice(rs, func(i, j int) bool {
		if rs[i].util != rs[j].util {
			return before(rs[i].util, rs[j].util)
		}
		return rs[i].node.Name < rs[j].node.Name
	})
}

func nodesOf(rs []rated) []pipeline.NodeView {
	if len(rs) == 0 {
		return nil
	}
	out := make([]pipeline.NodeView, len(rs))
	for i, r := range rs {
		out[i] = r.node
	}
	return out
}

// --- C3 potential-weighted -------------------------------------------------------------------------------------

const (
	// minWeight floors source weights so a source whose emptying does not lower the potential (an empty node, a node
	// whose pods cannot leave, or any node under a potential blind to the move) keeps a chance of being sampled.
	minWeight = 1e-9
	// probeID names the scoring probe's launched node in the potential's view; the probe is never returned, so it need
	// not be unique.
	probeID = "potential-weighted-probe"
)

// probeCapacity fits any node's pods on every dimension, so the probe's launch never limits what it can empty.
var probeCapacity = func() (r pipeline.Resources) {
	for i := range r {
		r[i] = 1e18
	}
	return r
}()

type weightedParams struct {
	SourceFraction float64 `json:"sourceFraction"`
}

type potentialWeighted struct {
	env            *pipeline.Env
	sourceFraction float64
}

func newPotentialWeighted(env *pipeline.Env, p pipeline.Params) (pipeline.CandidateSelector, error) {
	params := weightedParams{SourceFraction: 0.25}
	if err := p.Decode(&params); err != nil {
		return nil, fmt.Errorf("%s: %w", PotentialWeighted, err)
	}
	if err := validFraction(PotentialWeighted, "sourceFraction", params.SourceFraction); err != nil {
		return nil, err
	}
	if env == nil || env.Potential == nil {
		return nil, fmt.Errorf("%s needs a potential (pipeline.Env.Potential)", PotentialWeighted)
	}
	return &potentialWeighted{env: env, sourceFraction: params.SourceFraction}, nil
}

func (p *potentialWeighted) Name() string { return PotentialWeighted }

type keyed struct {
	node pipeline.NodeView
	key  float64
}

// Select samples sources by Efraimidis-Spirakis: each source draws u from rng and keeps the largest keys u^(1/w),
// which is weighted sampling without replacement in one pass. Keys are compared as ln(u)/w, the same order, because
// u^(1/w) underflows to 0 for small weights and would turn those sources into name-ordered ties. u is drawn from
// (0, 1] so the logarithm is finite. Under a potential that never moves (none), every weight is the floor and the
// draw is uniform.
func (p *potentialWeighted) Select(_ context.Context, s pipeline.Snapshot, rng *rand.Rand) ([]pipeline.NodeView, []pipeline.NodeView, error) {
	if err := needRNG(PotentialWeighted, rng); err != nil {
		return nil, nil, err
	}
	pot := p.env.Potential
	if pot == nil {
		return nil, nil, fmt.Errorf("%s needs a potential (pipeline.Env.Potential)", PotentialWeighted)
	}
	src, dst := eligible(s)
	keys := make([]keyed, len(src))
	for i, n := range src {
		w := minWeight
		if probe, ok := emptyingProbe(s, n); ok {
			// A NaN delta fails the comparison and keeps the floor.
			if drop := -pot.Delta(s, probe); drop > minWeight {
				w = drop
			}
		}
		keys[i] = keyed{node: n, key: math.Log(1-rng.Float64()) / w}
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].key != keys[j].key {
			return keys[i].key > keys[j].key
		}
		return keys[i].node.Name < keys[j].node.Name
	})
	k := sampleSize(p.sourceFraction, len(keys))
	var sources []pipeline.NodeView
	for _, kn := range keys[:k] {
		sources = append(sources, kn.node)
	}
	return sources, dst, nil
}

// emptyingProbe is a shift of every pod that may leave n onto one launched node that fits anything and costs nothing,
// so the potential's delta isolates what emptying n removes. Pods that may not leave (do-not-disrupt pods, any pod on
// a do-not-disrupt node) stay in place: a weight for a drop no move may make would spend samples on nodes generators
// cannot use. It reports false when nothing may leave.
func emptyingProbe(s pipeline.Snapshot, n pipeline.NodeView) (pipeline.Move, bool) {
	if n.DoNotDisrupt {
		return pipeline.Move{}, false
	}
	m := pipeline.Move{ID: probeID, Kind: pipeline.Shift, Sources: []string{n.Name}, Generator: PotentialWeighted,
		Launch: []pipeline.LaunchSpec{{NodePool: n.NodePool, Zone: n.Zone, CapacityType: n.CapacityType, Capacity: probeCapacity}}}
	for _, pod := range s.Pods(n.Name) {
		if pod.Reschedulable && !pod.DoNotDisrupt {
			m.Bindings = append(m.Bindings, pipeline.Binding{Pod: pod.Key, From: n.Name, Target: pipeline.Target{Launch: 0}})
		}
	}
	m.SortBindings()
	return m, len(m.Bindings) > 0
}

func init() {
	pipeline.RegisterCandidateSelector(UniformRandom, newUniformRandom)
	pipeline.RegisterCandidateSelector(UtilBand, newUtilBand)
	pipeline.RegisterCandidateSelector(PotentialWeighted, newPotentialWeighted)
}
