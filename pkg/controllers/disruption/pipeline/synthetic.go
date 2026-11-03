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
	"context"
	"fmt"
	"math/rand"
)

// SyntheticOptions shape a random snapshot for unit, property and conformance tests, where no cluster exists.
type SyntheticOptions struct {
	Nodes int
	// MaxFill bounds each node's dominant utilization; nodes fill to a uniform draw below it.
	MaxFill float64
	// DoNotDisruptProb and PDBProb add hard constraints to pods.
	DoNotDisruptProb float64
	PDBProb          float64
	// NodePools spreads nodes over this many pools (default 1).
	NodePools int
	// NonSourceProb marks nodes as not eligible to lose pods.
	NonSourceProb float64
}

var syntheticTypes = []struct {
	name  string
	cpu   float64 // millicores
	mem   float64 // bytes
	price float64
}{
	{"c.xlarge", 3900, 7.5 * (1 << 30), 0.17}, {"m.xlarge", 3900, 15 * (1 << 30), 0.19}, {"r.xlarge", 3900, 31 * (1 << 30), 0.25},
	{"c.2xlarge", 7900, 15 * (1 << 30), 0.34}, {"m.2xlarge", 7900, 31 * (1 << 30), 0.38}, {"r.2xlarge", 7900, 63 * (1 << 30), 0.50},
	{"c.4xlarge", 15900, 31 * (1 << 30), 0.68}, {"m.4xlarge", 15900, 63 * (1 << 30), 0.77}, {"r.4xlarge", 15900, 127 * (1 << 30), 1.01},
}

var syntheticShapes = [][2]float64{{250, 0.5 * (1 << 30)}, {500, 1 << 30}, {1000, 2 * (1 << 30)}, {1000, 4 * (1 << 30)}, {2000, 4 * (1 << 30)}, {500, 4 * (1 << 30)}, {2000, 2 * (1 << 30)}}

// RandomState builds a seeded synthetic snapshot: random node types and zones, pods of a handful of owners placed
// first-fit up to each node's random fill, occasional do-not-disrupt pods and PDBs, and Balanced-style totals.
func RandomState(rng *rand.Rand, o SyntheticOptions) *State {
	if o.MaxFill == 0 {
		o.MaxFill = 0.9
	}
	if o.NodePools == 0 {
		o.NodePools = 1
	}
	zones := []string{"zone-a", "zone-b", "zone-c"}
	var nodes []NodeView
	var pods []PodView
	pdbAllowed := map[string]int{}
	owners := 3 + rng.Intn(8)
	for i := range o.Nodes {
		t := syntheticTypes[rng.Intn(len(syntheticTypes))]
		pool := fmt.Sprintf("pool-%d", rng.Intn(o.NodePools))
		n := NodeView{Name: fmt.Sprintf("node-%03d", i), NodePool: pool, InstanceType: t.name, Zone: zones[rng.Intn(len(zones))], CapacityType: "on-demand",
			Capacity: Resources{CPU: t.cpu, Memory: t.mem, Pods: 110}, Price: t.price, Source: rng.Float64() >= o.NonSourceProb, Destination: true,
			Labels: map[string]string{"node.kubernetes.io/instance-type": t.name, "topology.kubernetes.io/zone": zones[0]}, DisruptionCost: 1}
		n.Labels["topology.kubernetes.io/zone"] = n.Zone
		fill := rng.Float64() * o.MaxFill
		var load Resources
		for j := range 40 {
			sh := syntheticShapes[rng.Intn(len(syntheticShapes))]
			req := Resources{CPU: sh[0], Memory: sh[1], Pods: 1}
			next := load.Add(req)
			if next.DominantShare(n.Capacity) > fill {
				continue
			}
			load = next
			owner := rng.Intn(owners)
			pv := PodView{Key: PodKey{Namespace: "default", Name: fmt.Sprintf("p-%03d-%02d", i, j)}, Node: n.Name, Requests: req, EvictionCost: 1,
				Owner: fmt.Sprintf("ReplicaSet/default/app-%d", owner), Labels: map[string]string{"app": fmt.Sprintf("app-%d", owner)}, Reschedulable: true,
				DoNotDisrupt: rng.Float64() < o.DoNotDisruptProb}
			if rng.Float64() < o.PDBProb {
				key := fmt.Sprintf("default/pdb-app-%d", owner)
				pv.PDBs = []string{key}
				if _, ok := pdbAllowed[key]; !ok {
					pdbAllowed[key] = rng.Intn(3)
				}
			}
			n.DisruptionCost += pv.EvictionCost
			pods = append(pods, pv)
		}
		nodes = append(nodes, n)
	}
	totals := map[string]Totals{}
	for _, n := range nodes {
		t := totals[n.NodePool]
		t.TotalCost += n.Price
		t.TotalDisruptionCost += n.DisruptionCost
		totals[n.NodePool] = t
	}
	return NewState(nodes, pods, totals, pdbAllowed)
}

// SnapshotFeasibility uses Fast for Full, for synthetic snapshots with no cluster behind them. Full rejects merges
// that would need a launch, since only the live scheduler can choose one.
type SnapshotFeasibility struct{ *FastChecker }

// NewSnapshotFeasibility returns a SnapshotFeasibility.
func NewSnapshotFeasibility() *SnapshotFeasibility {
	return &SnapshotFeasibility{FastChecker: &FastChecker{}}
}

func (f *SnapshotFeasibility) Full(_ context.Context, s Snapshot, m Move) (Move, error) {
	if len(m.Launch) > 0 {
		return m, fmt.Errorf("%w: launches need the live scheduler", ErrInfeasible)
	}
	if err := f.Fast(s, m); err != nil {
		return m, fmt.Errorf("%w: %w", ErrInfeasible, err)
	}
	return m, nil
}

// SyntheticEnv returns an Env for synthetic snapshots: a potential, snapshot feasibility and a scorer, no cluster.
func SyntheticEnv(pot Potential) *Env {
	if pot == nil {
		pot, _ = NewUtilEntropy(PotentialParams{})
	}
	return &Env{Potential: pot, Feasibility: NewSnapshotFeasibility(), Scorer: DefaultScorer{Potential: pot}, Recorder: NopRecorder{}}
}
