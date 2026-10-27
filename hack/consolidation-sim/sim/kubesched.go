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

package sim

import (
	"math"
	"sort"
	"time"

	corev1 "k8s.io/api/core/v1"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// kickScheduler runs the kube-scheduler model at the current time, once per instant however many changes asked.
func (s *Sim) kickScheduler() {
	if s.schedQueued {
		return
	}
	s.schedQueued = true
	s.at(s.now(), "kube-scheduler", func() {
		s.schedQueued = false
		s.schedulePending()
	})
}

// schedulePending binds every pending pod it can, oldest first, and marks the rest unschedulable so Karpenter's
// provisioner sees them.
func (s *Sim) schedulePending() {
	var pending []*simPod
	for _, p := range s.pods {
		if p.phase == podPending {
			pending = append(pending, p)
		}
	}
	sort.Slice(pending, func(i, j int) bool {
		if !pending[i].createdAt.Equal(pending[j].createdAt) {
			return pending[i].createdAt.Before(pending[j].createdAt)
		}
		return pending[i].key.Name < pending[j].key.Name
	})
	nodes := s.schedulableNodes()
	newlyUnschedulable := false
	for _, p := range pending {
		node := s.place(p, nodes)
		if node == "" {
			if !p.unschedulableMarked {
				if err := s.markUnschedulable(p); err != nil {
					s.fail(err)
					return
				}
				newlyUnschedulable = true
			}
			continue
		}
		if err := s.bind(p, node); err != nil {
			s.fail(err)
			return
		}
		key := p.key
		s.after(s.draw(s.cfg.Latency.PodStartup), "ready "+key.Name, func() {
			if q, ok := s.pods[key]; ok && q.phase == podBound {
				if err := s.markReady(q); err != nil {
					s.fail(err)
				}
			}
		})
	}
	if newlyUnschedulable {
		s.lastUnsched = s.now()
		s.triggerProvisioning()
	}
}

func (s *Sim) markUnschedulable(p *simPod) error {
	pod := &corev1.Pod{}
	if err := s.env.Client.Get(s.ctx, p.key, pod); err != nil {
		return err
	}
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable}}
	if err := s.env.Client.Status().Update(s.ctx, pod); err != nil {
		return err
	}
	p.unschedulableMarked = true
	return nil
}

func (s *Sim) schedulableNodes() []*simNode {
	var out []*simNode
	for _, n := range s.nodes {
		if n.ready && !n.removed {
			out = append(out, n)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out
}

func hardTaint(t corev1.Taint) bool {
	return t.Effect == corev1.TaintEffectNoSchedule || t.Effect == corev1.TaintEffectNoExecute
}

// feasible is kube-scheduler's filter phase for the constraints the workloads use: resources, NoSchedule and
// NoExecute taints, required hostname anti-affinity, and hard zone spread.
func (s *Sim) feasible(p *simPod, n *simNode, zoneCounts map[string]int, zones []string) bool {
	for _, t := range n.taints {
		if hardTaint(t) {
			return false
		}
	}
	if !n.used.Add(p.req).Fits(n.alloc) {
		return false
	}
	d, isDeploy := s.deploys[p.owner]
	if !isDeploy {
		return true
	}
	if d.spec.HostAntiAffinity {
		for k := range n.pods {
			if q := s.pods[k]; q != nil && q.owner == p.owner && q.phase != podTerminating {
				return false
			}
		}
	}
	if d.spec.ZoneSpread {
		minCount := math.MaxInt
		for _, z := range zones {
			minCount = min(minCount, zoneCounts[z])
		}
		if zoneCounts[n.zone]+1-minCount > 1 {
			return false
		}
	}
	return true
}

// place returns the node the placement model binds p to, or "".
//
//   - least-allocated: kube-scheduler's default scoring: NodeResourcesFit LeastAllocated (weight 1),
//     BalancedAllocation (1), the default soft PodTopologySpread constraints (hostname maxSkew 3, zone maxSkew 5;
//     weight 2) and TaintToleration against PreferNoSchedule taints (3). Ties break at random, as kube-scheduler's
//     do.
//   - most-allocated: the same with NodeResourcesFit MostAllocated, the profile the Balanced RFC recommends.
//   - Steered replacements (E2) carry preferred node affinity to the planned node, scored like NodeAffinity (2).
//   - plan-following: an oracle that binds a replacement where the disruption plan said it would land, when that
//     node is feasible, and otherwise falls back to least-allocated.
//
//nolint:gocyclo
func (s *Sim) place(p *simPod, nodes []*simNode) string {
	zoneCounts := map[string]int{}
	hostCounts := map[string]int{}
	zoneSet := map[string]bool{}
	for _, n := range nodes {
		zoneSet[n.zone] = true
		for k := range n.pods {
			if q := s.pods[k]; q != nil && q.owner == p.owner && q.phase != podTerminating {
				zoneCounts[n.zone]++
				hostCounts[n.name]++
			}
		}
	}
	zones := make([]string, 0, len(zoneSet))
	for z := range zoneSet {
		zones = append(zones, z)
	}
	sort.Strings(zones)
	var feasible []*simNode
	for _, n := range nodes {
		if s.feasible(p, n, zoneCounts, zones) {
			feasible = append(feasible, n)
		}
	}
	if len(feasible) == 0 {
		return ""
	}
	if s.cfg.Placement == PlanFollowing && p.planned != "" {
		for _, n := range feasible {
			if n.name == p.planned {
				return n.name
			}
		}
	}
	maxHost, maxZone := 0, 0
	for _, n := range feasible {
		maxHost = max(maxHost, hostCounts[n.name])
		maxZone = max(maxZone, zoneCounts[n.zone])
	}
	var best []*simNode
	bestScore := math.Inf(-1)
	for _, n := range feasible {
		after := n.used.Add(p.req)
		cpu, mem := after[pipeline.CPU]/n.alloc[pipeline.CPU], after[pipeline.Memory]/n.alloc[pipeline.Memory]
		var fit float64
		if s.cfg.Placement == MostAllocated {
			fit = 100 * (cpu + mem) / 2
		} else {
			fit = 100 * ((1 - cpu) + (1 - mem)) / 2
		}
		balanced := 100 * (1 - math.Abs(cpu-mem))
		spread := 100.0
		if maxHost > 0 || maxZone > 0 {
			spread = 100 * (1 - (float64(hostCounts[n.name])/float64(maxHost+1)+float64(zoneCounts[n.zone])/float64(maxZone+1))/2)
		}
		taint := 100.0
		for _, t := range n.taints {
			if t.Effect == corev1.TaintEffectPreferNoSchedule {
				taint = 0
			}
		}
		affinity := 0.0
		if p.steer != "" && p.steer == n.name {
			// A preferred node affinity term of weight 100, under the NodeAffinity plugin's weight 2.
			affinity = 100
		}
		score := fit + balanced + 2*spread + 3*taint + 2*affinity
		switch {
		case score > bestScore+1e-9:
			bestScore, best = score, []*simNode{n}
		case math.Abs(score-bestScore) <= 1e-9:
			best = append(best, n)
		}
	}
	return best[s.rngSched.Intn(len(best))].name
}

// --- provisioning --------------------------------------------------------------------------------------------------

// triggerProvisioning opens a batch window the way Karpenter's batcher does: it closes after BatchIdleDuration (1s)
// without new unschedulable pods, or BatchMaxDuration (10s) after it opened.
func (s *Sim) triggerProvisioning() {
	if s.batchOpen {
		return
	}
	s.batchOpen = true
	s.batchStart = s.now()
	s.after(time.Second, "batch", s.checkBatch)
}

func (s *Sim) checkBatch() {
	if s.now().Sub(s.lastUnsched) < time.Second && s.now().Sub(s.batchStart) < 10*time.Second {
		s.after(time.Second, "batch", s.checkBatch)
		return
	}
	s.batchOpen = false
	s.provision()
}

// provision is Provisioner.Reconcile after its batch: schedule pending pods and pods on deleting nodes, then create
// NodeClaims for what does not fit, recording nominations. It retries every 10s while unschedulable pods remain,
// as the provisioner's pod trigger keeps it running.
func (s *Sim) provision() {
	if !s.env.Cluster.Synced(s.ctx) {
		s.after(time.Second, "provision", s.provision)
		return
	}
	results, err := s.env.Provisioner.Schedule(s.ctx)
	if err != nil {
		s.fail(err)
		return
	}
	if len(results.NewNodeClaims) > 0 {
		if _, err := s.env.Provisioner.CreateNodeClaims(s.ctx, results.NewNodeClaims, provisioningReason(), recordNomination()); err != nil {
			s.fail(err)
			return
		}
	}
	unscheduled := 0
	for _, p := range s.pods {
		if p.phase == podPending && p.unschedulableMarked {
			unscheduled++
		}
	}
	if unscheduled > 0 && !s.provisionQueued {
		s.provisionQueued = true
		s.after(10*time.Second, "provision-retry", func() {
			s.provisionQueued = false
			s.provision()
		})
	}
}
