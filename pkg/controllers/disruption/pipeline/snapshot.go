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
	"sort"
)

type nodeState struct {
	view NodeView
	pods []PodView // sorted by key
	load Resources
}

func (n *nodeState) clone() *nodeState {
	return &nodeState{view: n.view, pods: append([]PodView(nil), n.pods...), load: n.load}
}

// State is the in-memory Snapshot. Forks share untouched nodes with their parent, so Apply costs a slice copy of node
// pointers plus clones of the nodes a move touches.
type State struct {
	nodes      []*nodeState // sorted by name
	index      map[string]int
	totals     map[string]Totals
	pdbAllowed map[string]int
	norm       Normalizer
}

var _ Snapshot = (*State)(nil)

// NewState builds a root snapshot. Pods are assigned to nodes by PodView.Node; pods naming an unknown node are
// dropped. Totals and PDB allowances are fixed for the snapshot and its forks.
func NewState(nodes []NodeView, pods []PodView, totals map[string]Totals, pdbAllowed map[string]int) *State {
	s := &State{totals: totals, pdbAllowed: pdbAllowed}
	sorted := append([]NodeView(nil), nodes...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })
	s.nodes = make([]*nodeState, len(sorted))
	for i := range sorted {
		s.nodes[i] = &nodeState{view: sorted[i]}
	}
	s.reindex()
	for _, p := range pods {
		if i, ok := s.index[p.Node]; ok {
			s.nodes[i].pods = append(s.nodes[i].pods, p)
			s.nodes[i].load = s.nodes[i].load.Add(p.Requests)
		}
	}
	for _, n := range s.nodes {
		sortPods(n.pods)
		s.norm.TotalPrice += n.view.Price
	}
	s.norm.NodeCount = len(s.nodes)
	return s
}

func sortPods(pods []PodView) {
	sort.Slice(pods, func(i, j int) bool {
		if pods[i].Key.Namespace != pods[j].Key.Namespace {
			return pods[i].Key.Namespace < pods[j].Key.Namespace
		}
		return pods[i].Key.Name < pods[j].Key.Name
	})
}

func (s *State) reindex() {
	s.index = make(map[string]int, len(s.nodes))
	for i, n := range s.nodes {
		s.index[n.view.Name] = i
	}
}

func (s *State) Nodes() []NodeView {
	out := make([]NodeView, len(s.nodes))
	for i, n := range s.nodes {
		out[i] = n.view
	}
	return out
}

func (s *State) Node(name string) (NodeView, bool) {
	if i, ok := s.index[name]; ok {
		return s.nodes[i].view, true
	}
	return NodeView{}, false
}

func (s *State) Pods(node string) []PodView {
	if i, ok := s.index[node]; ok {
		return append([]PodView(nil), s.nodes[i].pods...)
	}
	return nil
}

func (s *State) Load(node string) Resources {
	if i, ok := s.index[node]; ok {
		return s.nodes[i].load
	}
	return Resources{}
}

func (s *State) Totals(nodePool string) Totals { return s.totals[nodePool] }

func (s *State) Normalizer() Normalizer { return s.norm }

func (s *State) PDBAllowed(pdb string) int {
	if v, ok := s.pdbAllowed[pdb]; ok {
		return v
	}
	return -1
}

// LaunchName is the name a launched node takes in forked snapshots.
func LaunchName(moveID string, i int) string { return fmt.Sprintf("launch/%s/%d", moveID, i) }

// Apply returns the snapshot after m.
//
//nolint:gocyclo
func (s *State) Apply(m Move) (Snapshot, error) {
	next := &State{nodes: append([]*nodeState(nil), s.nodes...), index: s.index, totals: s.totals, pdbAllowed: s.pdbAllowed, norm: s.norm}
	cloned := map[int]bool{}
	mutable := func(i int) *nodeState {
		if !cloned[i] {
			next.nodes[i] = next.nodes[i].clone()
			cloned[i] = true
		}
		return next.nodes[i]
	}
	// Launches append new nodes; keep them at the end until the final sort so indices stay valid during the move.
	launchIdx := make([]int, len(m.Launch))
	for li, l := range m.Launch {
		name := LaunchName(m.ID, li)
		if _, exists := next.index[name]; exists {
			return nil, fmt.Errorf("launch %s already exists", name)
		}
		next.nodes = append(next.nodes, &nodeState{view: NodeView{
			Name: name, NodePool: l.NodePool, InstanceType: firstOr(l.InstanceTypes, ""), Zone: l.Zone, CapacityType: l.CapacityType,
			Capacity: l.Capacity, Price: l.Price, Destination: true, Launched: true, DisruptionCost: 1,
		}})
		launchIdx[li] = len(next.nodes) - 1
		cloned[len(next.nodes)-1] = true
	}
	lookup := func(name string) (int, bool) {
		if i, ok := next.index[name]; ok {
			return i, true
		}
		for li := range m.Launch {
			if LaunchName(m.ID, li) == name {
				return launchIdx[li], true
			}
		}
		return 0, false
	}
	for _, b := range m.Bindings {
		fi, ok := lookup(b.From)
		if !ok {
			return nil, fmt.Errorf("binding %s: source node %s not found", b.Pod, b.From)
		}
		var ti int
		if b.Target.Launch >= 0 {
			if b.Target.Launch >= len(m.Launch) {
				return nil, fmt.Errorf("binding %s: launch index %d out of range", b.Pod, b.Target.Launch)
			}
			ti = launchIdx[b.Target.Launch]
		} else if ti, ok = lookup(b.Target.Node); !ok {
			return nil, fmt.Errorf("binding %s: target node %s not found", b.Pod, b.Target.Node)
		}
		if fi == ti {
			return nil, fmt.Errorf("binding %s: source and target are both %s", b.Pod, b.From)
		}
		from := mutable(fi)
		pi := sort.Search(len(from.pods), func(i int) bool {
			k := from.pods[i].Key
			return k.Namespace > b.Pod.Namespace || (k.Namespace == b.Pod.Namespace && k.Name >= b.Pod.Name)
		})
		if pi >= len(from.pods) || from.pods[pi].Key != b.Pod {
			return nil, fmt.Errorf("binding %s: pod not on %s", b.Pod, b.From)
		}
		pod := from.pods[pi]
		from.pods = append(from.pods[:pi], from.pods[pi+1:]...)
		from.load = from.load.Sub(pod.Requests)
		to := mutable(ti)
		pod.Node = to.view.Name
		to.pods = append(to.pods, pod)
		to.load = to.load.Add(pod.Requests)
	}
	for i := range cloned {
		n := next.nodes[i]
		sortPods(n.pods)
		if !n.load.Fits(n.view.Capacity) {
			return nil, fmt.Errorf("node %s over capacity after move", n.view.Name)
		}
	}
	if len(m.Remove) > 0 {
		remove := map[string]bool{}
		for _, r := range m.Remove {
			i, ok := lookup(r)
			if !ok {
				return nil, fmt.Errorf("remove: node %s not found", r)
			}
			for _, p := range next.nodes[i].pods {
				if p.Reschedulable {
					return nil, fmt.Errorf("remove: node %s still holds reschedulable pod %s", r, p.Key)
				}
			}
			remove[r] = true
		}
		kept := next.nodes[:0:0]
		for _, n := range next.nodes {
			if !remove[n.view.Name] {
				kept = append(kept, n)
			}
		}
		next.nodes = kept
	}
	if len(m.Launch) > 0 || len(m.Remove) > 0 {
		sort.Slice(next.nodes, func(i, j int) bool { return next.nodes[i].view.Name < next.nodes[j].view.Name })
		next.reindex()
	}
	return next, nil
}

func firstOr(s []string, d string) string {
	if len(s) > 0 {
		return s[0]
	}
	return d
}
