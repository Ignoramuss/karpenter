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

package candidates_test

import (
	"context"
	"fmt"
	"math/rand"
	"reflect"
	"testing"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/conformance"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/strategies/candidates"
)

func newRNG(seed int64) *rand.Rand { return rand.New(rand.NewSource(seed)) } //nolint:gosec

func build(t testing.TB, env *pipeline.Env, name string, p pipeline.Params) pipeline.CandidateSelector {
	t.Helper()
	sel, err := pipeline.NewCandidateSelector(env, pipeline.StageConfig{Name: name, Params: p})
	if err != nil {
		t.Fatalf("%s %v: %v", name, p, err)
	}
	return sel
}

func TestConformance(t *testing.T) {
	for _, c := range []struct {
		name   string
		params pipeline.Params
	}{
		{candidates.UniformRandom, nil},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 0.6, "destinationFraction": 0.3}},
		{candidates.UtilBand, nil},
		{candidates.UtilBand, pipeline.Params{"resource": "cpu", "fullThreshold": 0.8}},
		{candidates.UtilBand, pipeline.Params{"resource": "memory"}},
		{candidates.PotentialWeighted, nil},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 0.7}},
	} {
		t.Run(fmt.Sprintf("%s %v", c.name, c.params), func(t *testing.T) {
			conformance.CheckSelector(t, func(env *pipeline.Env) pipeline.CandidateSelector { return build(t, env, c.name, c.params) })
		})
	}
}

func TestParams(t *testing.T) {
	for _, c := range []struct {
		name    string
		params  pipeline.Params
		wantErr bool
	}{
		{candidates.UniformRandom, nil, false},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 1, "destinationFraction": 0.1}, false},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 0}, true},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 1.5}, true},
		{candidates.UniformRandom, pipeline.Params{"destinationFraction": -0.1}, true},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": "half"}, true},
		{candidates.UniformRandom, pipeline.Params{"fraction": 0.5}, true},
		{candidates.UtilBand, nil, false},
		{candidates.UtilBand, pipeline.Params{"resource": "cpu", "fullThreshold": 1}, false},
		{candidates.UtilBand, pipeline.Params{"resource": "gpu"}, true},
		{candidates.UtilBand, pipeline.Params{"fullThreshold": 0}, true},
		{candidates.UtilBand, pipeline.Params{"fullThreshold": 1.2}, true},
		{candidates.UtilBand, pipeline.Params{"sourceFraction": 0.5}, true},
		{candidates.PotentialWeighted, nil, false},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 0.05}, false},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 0}, true},
		{candidates.PotentialWeighted, pipeline.Params{"destinationFraction": 0.5}, true},
	} {
		_, err := pipeline.NewCandidateSelector(pipeline.SyntheticEnv(nil), pipeline.StageConfig{Name: c.name, Params: c.params})
		if (err != nil) != c.wantErr {
			t.Errorf("%s %v: error %v, want error %t", c.name, c.params, err, c.wantErr)
		}
	}
	if _, err := pipeline.NewCandidateSelector(&pipeline.Env{}, pipeline.StageConfig{Name: candidates.PotentialWeighted}); err == nil {
		t.Errorf("%s built without a potential", candidates.PotentialWeighted)
	}
}

// node describes one hand-built node on a 1000-unit CPU and memory capacity, holding one pod at the given
// utilization (none when both are 0).
type node struct {
	name         string
	cpu, mem     float64
	price        float64
	source, dest bool
	dnd, podDND  bool
}

func at(name string, util float64, source, dest bool) node {
	return node{name: name, cpu: util, mem: util, price: 0.2, source: source, dest: dest}
}

func newState(ns ...node) *pipeline.State {
	var nodes []pipeline.NodeView
	var pods []pipeline.PodView
	var total pipeline.Totals
	for _, n := range ns {
		nodes = append(nodes, pipeline.NodeView{Name: n.name, NodePool: "default", Capacity: pipeline.Resources{pipeline.CPU: 1000, pipeline.Memory: 1000, pipeline.Pods: 110},
			Price: n.price, Source: n.source, Destination: n.dest, DoNotDisrupt: n.dnd, DisruptionCost: 1})
		total.TotalCost += n.price
		total.TotalDisruptionCost++
		if n.cpu > 0 || n.mem > 0 {
			pods = append(pods, pipeline.PodView{Key: pipeline.PodKey{Namespace: "default", Name: n.name + "-pod"}, Node: n.name,
				Requests: pipeline.Resources{pipeline.CPU: n.cpu * 1000, pipeline.Memory: n.mem * 1000, pipeline.Pods: 1}, EvictionCost: 1,
				Reschedulable: true, DoNotDisrupt: n.podDND})
		}
	}
	return pipeline.NewState(nodes, pods, map[string]pipeline.Totals{"default": total}, nil)
}

func names(ns []pipeline.NodeView) []string {
	var out []string
	for _, n := range ns {
		out = append(out, n.Name)
	}
	return out
}

// fleet has 10 nodes that are both sources and destinations, 2 destinations only and 1 node that is neither.
func fleet() *pipeline.State {
	var ns []node
	for i := range 10 {
		ns = append(ns, at(fmt.Sprintf("both-%d", i), 0.05+0.09*float64(i), true, true))
	}
	ns = append(ns, at("dest-0", 0.4, false, true), at("dest-1", 0.6, false, true), at("neither", 0.5, false, false))
	return newState(ns...)
}

func TestUniformRandomDrawsWithPerm(t *testing.T) {
	ctx := context.Background()
	s := fleet()
	var src, dst []pipeline.NodeView
	for _, n := range s.Nodes() {
		if n.Source {
			src = append(src, n)
		}
		if n.Destination {
			dst = append(dst, n)
		}
	}
	orders := map[string]bool{}
	for seed := int64(1); seed <= 20; seed++ {
		sel := build(t, nil, candidates.UniformRandom, pipeline.Params{"destinationFraction": 0.5})
		gotSrc, gotDst, err := sel.Select(ctx, s, newRNG(seed))
		if err != nil {
			t.Fatal(err)
		}
		// Sources are drawn first, then destinations, each as a prefix of one permutation of the name order.
		r := newRNG(seed)
		var wantSrc, wantDst []string
		for _, i := range r.Perm(len(src))[:3] {
			wantSrc = append(wantSrc, src[i].Name)
		}
		for _, i := range r.Perm(len(dst))[:6] {
			wantDst = append(wantDst, dst[i].Name)
		}
		if !reflect.DeepEqual(names(gotSrc), wantSrc) || !reflect.DeepEqual(names(gotDst), wantDst) {
			t.Errorf("seed %d: got %v / %v, want %v / %v", seed, names(gotSrc), names(gotDst), wantSrc, wantDst)
		}
		orders[fmt.Sprint(names(gotSrc))] = true
	}
	if len(orders) < 10 {
		t.Errorf("20 seeds drew only %d distinct source samples", len(orders))
	}
}

func TestFractions(t *testing.T) {
	ctx := context.Background()
	s := fleet() // 10 sources, 12 destinations
	none := newState(at("a", 0.5, false, true), at("b", 0.2, false, true))
	for _, c := range []struct {
		name             string
		params           pipeline.Params
		state            *pipeline.State
		wantSrc, wantDst int
	}{
		{candidates.UniformRandom, nil, s, 3, 12},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 0.7, "destinationFraction": 0.5}, s, 7, 6},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 0.01, "destinationFraction": 0.01}, s, 1, 1},
		{candidates.UniformRandom, pipeline.Params{"sourceFraction": 1}, s, 10, 12},
		{candidates.UniformRandom, nil, none, 0, 2},
		{candidates.PotentialWeighted, nil, s, 3, 12},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 0.7}, s, 7, 12},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 0.01}, s, 1, 12},
		{candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 1}, s, 10, 12},
		{candidates.PotentialWeighted, nil, none, 0, 2},
	} {
		for seed := int64(1); seed <= 5; seed++ {
			src, dst, err := build(t, pipeline.SyntheticEnv(nil), c.name, c.params).Select(ctx, c.state, newRNG(seed))
			if err != nil {
				t.Fatal(err)
			}
			if len(src) != c.wantSrc || len(dst) != c.wantDst {
				t.Errorf("%s %v seed %d: %d sources and %d destinations, want %d and %d", c.name, c.params, seed, len(src), len(dst), c.wantSrc, c.wantDst)
			}
		}
	}
}

func TestUtilBand(t *testing.T) {
	ctx := context.Background()
	// Mean utilization over the ten nodes with capacity is 0.517; "nocap" has none and is never rated.
	s := newState(
		at("n0", 0, true, true),
		at("n1", 0.30, true, true),
		at("n2", 0.10, true, true),
		at("n3", 0.20, false, true), // below the mean, not a source
		at("n4", 0.70, true, true),
		at("n5", 0.90, false, true),
		at("n6", 0.97, true, true),  // above fullThreshold
		at("n7", 0.80, true, false), // above the mean, not a destination
		at("n8", 0.30, true, true),  // ties n1
		at("n9", 0.90, false, true), // ties n5
	)
	withNoCapacity := pipeline.NewState(append(s.Nodes(), pipeline.NodeView{Name: "nocap", Source: true, Destination: true}), podsOf(s), nil, nil)
	for _, c := range []struct {
		params           pipeline.Params
		state            *pipeline.State
		wantSrc, wantDst []string
	}{
		{nil, s, []string{"n0", "n2", "n1", "n8"}, []string{"n5", "n9", "n4"}},
		{nil, withNoCapacity, []string{"n0", "n2", "n1", "n8"}, []string{"n5", "n9", "n4"}},
		{pipeline.Params{"fullThreshold": 0.99}, s, []string{"n0", "n2", "n1", "n8"}, []string{"n6", "n5", "n9", "n4"}},
		{pipeline.Params{"fullThreshold": 0.9}, s, []string{"n0", "n2", "n1", "n8"}, []string{"n4"}},
	} {
		sel := build(t, nil, candidates.UtilBand, c.params)
		for _, r := range []*rand.Rand{nil, newRNG(1), newRNG(2)} {
			src, dst, err := sel.Select(ctx, c.state, r)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(names(src), c.wantSrc) || !reflect.DeepEqual(names(dst), c.wantDst) {
				t.Errorf("%v: got %v / %v, want %v / %v", c.params, names(src), names(dst), c.wantSrc, c.wantDst)
			}
		}
	}
}

func podsOf(s pipeline.Snapshot) []pipeline.PodView {
	var out []pipeline.PodView
	for _, n := range s.Nodes() {
		out = append(out, s.Pods(n.Name)...)
	}
	return out
}

func TestUtilBandResource(t *testing.T) {
	ctx := context.Background()
	s := newState(
		node{name: "cpu-heavy", cpu: 0.9, mem: 0.1, source: true, dest: true},
		node{name: "mem-heavy", cpu: 0.1, mem: 0.9, source: true, dest: true},
		node{name: "mid", cpu: 0.5, mem: 0.5, source: true, dest: true},
	)
	for _, c := range []struct {
		resource         string
		wantSrc, wantDst []string
	}{
		{"cpu", []string{"mem-heavy"}, []string{"cpu-heavy"}},
		{"memory", []string{"cpu-heavy"}, []string{"mem-heavy"}},
		{"dominant", []string{"mid"}, []string{"cpu-heavy", "mem-heavy"}},
	} {
		src, dst, err := build(t, nil, candidates.UtilBand, pipeline.Params{"resource": c.resource}).Select(ctx, s, nil)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(names(src), c.wantSrc) || !reflect.DeepEqual(names(dst), c.wantDst) {
			t.Errorf("%s: got %v / %v, want %v / %v", c.resource, names(src), names(dst), c.wantSrc, c.wantDst)
		}
	}
}

// emptyingTargets has eleven sources. Under the price-weighted entropy potential, emptying "best" (half full, the
// priciest node whose pods may leave) drops the potential far more than emptying any other: the near-full nodes sit
// close to h = 0 already, the empty ones have nothing to move, and the do-not-disrupt ones may not be emptied at all.
func emptyingTargets() *pipeline.State {
	ns := []node{
		{name: "best", cpu: 0.5, mem: 0.5, price: 1, source: true, dest: true},
		{name: "dnd-node", cpu: 0.5, mem: 0.5, price: 2, source: true, dest: true, dnd: true},
		{name: "dnd-pod", cpu: 0.5, mem: 0.5, price: 2, source: true, dest: true, podDND: true},
	}
	for i := range 5 {
		ns = append(ns, node{name: fmt.Sprintf("full-%d", i), cpu: 0.98, mem: 0.98, price: 0.2, source: true, dest: true})
	}
	for i := range 3 {
		ns = append(ns, node{name: fmt.Sprintf("empty-%d", i), price: 0.2, source: true, dest: true})
	}
	return newState(ns...)
}

// firstPicks counts, over seeds, how often each node is the one source a selector returns.
func firstPicks(t *testing.T, env *pipeline.Env, name string, p pipeline.Params, s pipeline.Snapshot, seeds int) map[string]int {
	t.Helper()
	counts := map[string]int{}
	for seed := int64(1); seed <= int64(seeds); seed++ {
		src, _, err := build(t, env, name, p).Select(context.Background(), s, newRNG(seed))
		if err != nil {
			t.Fatal(err)
		}
		if len(src) != 1 {
			t.Fatalf("%s: %d sources, want 1", name, len(src))
		}
		counts[src[0].Name]++
	}
	return counts
}

func TestPotentialWeightedFavorsTheBestEmptyingTarget(t *testing.T) {
	const seeds = 400
	s := emptyingTargets()
	one := pipeline.Params{"sourceFraction": 0.05} // ceil(0.05 * 11) = 1
	weighted := firstPicks(t, pipeline.SyntheticEnv(nil), candidates.PotentialWeighted, one, s, seeds)
	uniform := firstPicks(t, nil, candidates.UniformRandom, one, s, seeds)
	// The weights put about 0.88 of the mass on "best"; a uniform sampler gives it 1/11.
	if weighted["best"] < seeds*3/4 || weighted["best"] < 3*uniform["best"] {
		t.Errorf("potential-weighted picked best %d of %d times, uniform-random %d", weighted["best"], seeds, uniform["best"])
	}
	for _, n := range []string{"dnd-node", "dnd-pod"} {
		if weighted[n] > 0 {
			t.Errorf("potential-weighted picked %s, which may not be emptied, %d times", n, weighted[n])
		}
	}

	// Under a potential that never moves every weight is the floor, so the draw is uniform and every source keeps
	// its chance.
	none, err := pipeline.NewPotential(pipeline.StageConfig{Name: "none"})
	if err != nil {
		t.Fatal(err)
	}
	flat := firstPicks(t, pipeline.SyntheticEnv(none), candidates.PotentialWeighted, one, s, seeds)
	for _, n := range s.Nodes() {
		if flat[n.Name] < seeds/40 || flat[n.Name] > seeds/5 {
			t.Errorf("under the none potential, %s was picked %d of %d times, want about %d", n.Name, flat[n.Name], seeds, seeds/11)
		}
	}
}

func TestPotentialWeightedReturnsSnapshotNodesAndEveryDestination(t *testing.T) {
	ctx := context.Background()
	s := emptyingTargets()
	want := names(s.Nodes()) // every node is a destination, in name order
	for seed := int64(1); seed <= 20; seed++ {
		src, dst, err := build(t, pipeline.SyntheticEnv(nil), candidates.PotentialWeighted, pipeline.Params{"sourceFraction": 1}).Select(ctx, s, newRNG(seed))
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(names(dst), want) {
			t.Errorf("seed %d: destinations %v, want %v", seed, names(dst), want)
		}
		// The probe's launched node exists only in the potential's view; every source must be a real node.
		if len(src) != len(want) {
			t.Errorf("seed %d: %d sources, want %d", seed, len(src), len(want))
		}
		for _, n := range src {
			if _, ok := s.Node(n.Name); !ok {
				t.Errorf("seed %d: returned %s, which is not in the snapshot", seed, n.Name)
			}
		}
	}
}

func TestRandomSelectorsNeedAnRNG(t *testing.T) {
	for _, name := range []string{candidates.UniformRandom, candidates.PotentialWeighted} {
		if _, _, err := build(t, pipeline.SyntheticEnv(nil), name, nil).Select(context.Background(), fleet(), nil); err == nil {
			t.Errorf("%s selected without an rng", name)
		}
	}
}
