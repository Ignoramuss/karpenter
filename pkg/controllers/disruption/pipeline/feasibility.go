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
	"errors"
	"fmt"
	"sync/atomic"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"

	"sigs.k8s.io/karpenter/pkg/scheduling"
)

// Fast-feasibility rejection reasons, so callers and the decision log can count them.
var (
	ErrDoNotDisrupt   = errors.New("do-not-disrupt")
	ErrPDB            = errors.New("pod disruption budget")
	ErrNotSource      = errors.New("not a source")
	ErrNotDestination = errors.New("not a destination")
	ErrCapacity       = errors.New("insufficient capacity")
	ErrTaints         = errors.New("untolerated taint")
	ErrRequirements   = errors.New("incompatible node requirements")
	ErrVolumeZone     = errors.New("bound volume zone")
	ErrAntiAffinity   = errors.New("required pod anti-affinity")
	ErrIncomplete     = errors.New("removed node still holds reschedulable pods")
	ErrUnknown        = errors.New("unknown pod or node")
)

// FastChecker implements Feasibility.Fast from the snapshot alone. Topology spread constraints and inter-pod
// affinity across zones are not checked; TopologyUnchecked counts moves that carried such constraints, so the
// property test can report how often Fast and Full disagree on topology.
type FastChecker struct {
	TopologyUnchecked atomic.Int64
}

// Fast checks m against s.
//
//nolint:gocyclo
func (f *FastChecker) Fast(s Snapshot, m Move) error {
	launchCap := make([]Resources, len(m.Launch))
	for i, l := range m.Launch {
		launchCap[i] = l.Capacity
	}
	added := map[string]Resources{}
	// leaving counts requests the move takes off each node, so a node that both sends and receives pods (a swap) is
	// checked against its load after the move.
	leaving := map[string]Resources{}
	for _, b := range m.Bindings {
		for _, pv := range s.Pods(b.From) {
			if pv.Key == b.Pod {
				leaving[b.From] = leaving[b.From].Add(pv.Requests)
			}
		}
	}
	evictions := map[string]int{}
	moved := map[PodKey]bool{}
	podsOf := map[string]map[PodKey]PodView{}
	podsOn := func(node string) map[PodKey]PodView {
		if p, ok := podsOf[node]; ok {
			return p
		}
		p := map[PodKey]PodView{}
		for _, pv := range s.Pods(node) {
			p[pv.Key] = pv
		}
		podsOf[node] = p
		return p
	}
	// landed tracks the pods a move puts on each target, for anti-affinity among themselves.
	landed := map[string][]PodView{}
	topology := false
	for _, b := range m.Bindings {
		from, ok := s.Node(b.From)
		if !ok {
			return fmt.Errorf("%w: node %s", ErrUnknown, b.From)
		}
		if !from.Source {
			return fmt.Errorf("%w: %s", ErrNotSource, b.From)
		}
		if from.DoNotDisrupt {
			return fmt.Errorf("%w: node %s", ErrDoNotDisrupt, b.From)
		}
		pod, ok := podsOn(b.From)[b.Pod]
		if !ok {
			return fmt.Errorf("%w: pod %s on %s", ErrUnknown, b.Pod, b.From)
		}
		if !pod.Reschedulable {
			return fmt.Errorf("%w: pod %s is not reschedulable", ErrNotSource, b.Pod)
		}
		if pod.DoNotDisrupt {
			return fmt.Errorf("%w: pod %s", ErrDoNotDisrupt, b.Pod)
		}
		if moved[b.Pod] {
			return fmt.Errorf("%w: pod %s bound twice", ErrUnknown, b.Pod)
		}
		moved[b.Pod] = true
		for _, pdb := range pod.PDBs {
			evictions[pdb]++
			if allowed := s.PDBAllowed(pdb); allowed >= 0 && evictions[pdb] > allowed {
				return fmt.Errorf("%w: %s allows %d", ErrPDB, pdb, allowed)
			}
		}
		if pod.Pod != nil && hasTopologyConstraints(pod.Pod) {
			topology = true
		}
		if b.Target.Launch >= 0 {
			if b.Target.Launch >= len(m.Launch) {
				return fmt.Errorf("%w: launch %d", ErrUnknown, b.Target.Launch)
			}
			launchCap[b.Target.Launch] = launchCap[b.Target.Launch].Sub(pod.Requests)
			if !(Resources{}).Fits(launchCap[b.Target.Launch]) {
				return fmt.Errorf("%w: launch %d", ErrCapacity, b.Target.Launch)
			}
			continue
		}
		to, ok := s.Node(b.Target.Node)
		if !ok {
			return fmt.Errorf("%w: node %s", ErrUnknown, b.Target.Node)
		}
		if !to.Destination {
			return fmt.Errorf("%w: %s", ErrNotDestination, b.Target.Node)
		}
		added[to.Name] = added[to.Name].Add(pod.Requests)
		if !s.Load(to.Name).Add(added[to.Name]).Sub(leaving[to.Name]).Fits(to.Capacity) {
			return fmt.Errorf("%w: %s", ErrCapacity, to.Name)
		}
		if pod.Pod != nil {
			if err := scheduling.Taints(to.Taints).ToleratesPod(pod.Pod); err != nil {
				return fmt.Errorf("%w: %s on %s", ErrTaints, b.Pod, to.Name)
			}
			if err := scheduling.NewLabelRequirements(to.Labels).Compatible(scheduling.NewStrictPodRequirements(pod.Pod), scheduling.AllowUndefinedWellKnownLabels); err != nil {
				return fmt.Errorf("%w: %s on %s", ErrRequirements, b.Pod, to.Name)
			}
			if !volumeZonesAllow(pod.Pod, to.Zone) {
				return fmt.Errorf("%w: %s on %s", ErrVolumeZone, b.Pod, to.Name)
			}
			if violatesHostnameAntiAffinity(pod, append(s.Pods(to.Name), landed[to.Name]...)) {
				return fmt.Errorf("%w: %s on %s", ErrAntiAffinity, b.Pod, to.Name)
			}
		}
		landed[to.Name] = append(landed[to.Name], pod)
	}
	for _, r := range m.Remove {
		if _, ok := s.Node(r); !ok {
			return fmt.Errorf("%w: node %s", ErrUnknown, r)
		}
		for _, p := range s.Pods(r) {
			if p.Reschedulable && !moved[p.Key] {
				return fmt.Errorf("%w: %s on %s", ErrIncomplete, p.Key, r)
			}
		}
	}
	if topology {
		f.TopologyUnchecked.Add(1)
	}
	return nil
}

func hasTopologyConstraints(p *corev1.Pod) bool {
	if len(p.Spec.TopologySpreadConstraints) > 0 {
		return true
	}
	a := p.Spec.Affinity
	return a != nil && (a.PodAffinity != nil || (a.PodAntiAffinity != nil && len(a.PodAntiAffinity.RequiredDuringSchedulingIgnoredDuringExecution) > 0))
}

// VolumeZoneAnnotation lets synthetic workloads declare the zone their bound volume lives in; the simulator sets it
// on pods with zonal persistent volumes. Full feasibility reads real PersistentVolumes instead.
const VolumeZoneAnnotation = "simulation.karpenter.sh/volume-zone"

func volumeZonesAllow(p *corev1.Pod, zone string) bool {
	want, ok := p.Annotations[VolumeZoneAnnotation]
	return !ok || zone == "" || want == zone
}

// violatesHostnameAntiAffinity checks required anti-affinity terms keyed on hostname, in both directions.
func violatesHostnameAntiAffinity(pod PodView, others []PodView) bool {
	for _, o := range others {
		if o.Key == pod.Key {
			continue
		}
		if antiAffinityMatches(pod, o) || antiAffinityMatches(o, pod) {
			return true
		}
	}
	return false
}

func antiAffinityMatches(owner, other PodView) bool {
	if owner.Pod == nil || owner.Pod.Spec.Affinity == nil || owner.Pod.Spec.Affinity.PodAntiAffinity == nil {
		return false
	}
	for _, term := range owner.Pod.Spec.Affinity.PodAntiAffinity.RequiredDuringSchedulingIgnoredDuringExecution {
		if term.TopologyKey != corev1.LabelHostname || term.LabelSelector == nil {
			continue
		}
		if len(term.Namespaces) > 0 && !contains(term.Namespaces, other.Key.Namespace) {
			continue
		}
		if len(term.Namespaces) == 0 && term.NamespaceSelector == nil && other.Key.Namespace != owner.Key.Namespace {
			continue
		}
		sel, err := metav1.LabelSelectorAsSelector(term.LabelSelector)
		if err == nil && sel.Matches(labels.Set(other.Labels)) {
			return true
		}
	}
	return false
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
