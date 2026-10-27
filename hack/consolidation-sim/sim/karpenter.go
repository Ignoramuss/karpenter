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
	"context"
	"fmt"
	"reflect"
	"sort"
	"time"
	"unsafe"

	"github.com/awslabs/operatorpkg/option"
	"github.com/awslabs/operatorpkg/status"
	"github.com/google/uuid"
	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/metrics"
	disruptionutils "sigs.k8s.io/karpenter/pkg/utils/disruption"
	"sigs.k8s.io/karpenter/pkg/utils/nodepool"
)

type ctxT = context.Context

func provisioningReason() option.Function[provisioning.LaunchOptions] {
	return provisioning.WithReason(metrics.ProvisionedReason)
}
func recordNomination() option.Function[provisioning.LaunchOptions] {
	return provisioning.RecordPodNomination
}

// pollingPeriod is the disruption controller's.
const pollingPeriod = 10 * time.Second

// disruptionTick is disruption.Controller.Reconcile with the controller's own loop replaced by the event engine:
// wait for cluster state to sync, clear stale disrupted taints, then try each method in order with fresh candidates,
// totals and budgets, and start the first method's commands on the real orchestration queue. Candidates come in a
// seeded random order (see orderCandidates). Drift methods are omitted: nothing drifts in the simulator.
func (s *Sim) disruptionTick() {
	if len(s.methods) == 0 {
		return
	}
	s.disrupting = true
	defer func() { s.disrupting = false }()
	if s.now().Sub(s.start) < s.cfg.Warmup.Duration {
		s.atDeferred(s.start.Add(s.cfg.Warmup.Duration), "disruption", s.disruptionTick)
		return
	}
	s.syncConsolidatable()
	if !s.env.Cluster.Synced(s.ctx) {
		s.atDeferred(s.now().Add(time.Second), "disruption", s.disruptionTick)
		return
	}
	if err := s.clearStaleTaints(); err != nil {
		s.fail(err)
		return
	}
	wall := time.Now() //nolint:forbidigo // decision latency is the one wall-clock measurement, and it is never fed back
	cpu := processCPU()
	s.passWaitWall, s.passWaitCPU = 0, 0
	success := false
	method := ""
	for _, m := range s.methods {
		cands, totals, err := disruption.GetCandidatesWithTotals(s.ctx, s.env.Cluster, s.env.Client, s.env.Recorder, s.env.Clock, s.env.CloudProvider,
			m.ShouldDisrupt, m.Class(), s.env.Queue, s.env.ClusterCost)
		if err != nil {
			s.fail(fmt.Errorf("determining candidates, %w", err))
			return
		}
		if len(cands) == 0 {
			continue
		}
		cands = s.orderCandidates(cands)
		if setter, ok := m.(disruption.NodePoolTotalsSetter); ok {
			setter.SetNodePoolTotals(totals)
		}
		budgets, err := disruption.BuildDisruptionBudgetMapping(s.ctx, s.env.Cluster, s.env.Clock, s.env.Client, s.env.CloudProvider, s.env.Recorder, m.Reason())
		if err != nil {
			s.fail(err)
			return
		}
		cmds, err := m.ComputeCommands(s.ctx, budgets, cands...)
		if err != nil {
			s.fail(fmt.Errorf("computing commands with %T, %w", m, err))
			return
		}
		cmds = lo.Filter(cmds, func(c disruption.Command, _ int) bool { return c.Decision() != disruption.NoOpDecision })
		if len(cmds) == 0 {
			continue
		}
		for i := range cmds {
			cmd := cmds[i]
			cmd.CreationTimestamp = s.now()
			cmd.ID = lo.Must(uuid.NewRandomFromReader(s.rngOrder))
			cmd.Method = m
			if err := s.env.Queue.StartCommand(s.ctx, &cmd); err != nil {
				s.m.commandFailed(err)
				continue
			}
			s.drainQueueSource()
			s.m.commandStarted(m, &cmd)
		}
		success = true
		method = m.ConsolidationType()
		break
	}
	s.m.disruptionPass(time.Since(wall)-s.passWaitWall, processCPU()-cpu-s.passWaitCPU, method)
	s.ensureQueueTicking()
	if success {
		// RequeueImmediately: the next pass runs as soon as the commands just started have been taken in.
		s.atDeferred(s.now(), "disruption", s.disruptionTick)
		return
	}
	s.atDeferred(s.now().Add(pollingPeriod), "disruption", s.disruptionTick)
}

// clearStaleTaints mirrors the controller's cleanup of karpenter.sh/disrupted taints on nodes no command owns.
func (s *Sim) clearStaleTaints() error {
	outdated := lo.Reject(s.env.Cluster.DeepCopyNodes(), func(n *state.StateNode, _ int) bool {
		return s.env.Queue.HasAny(n.ProviderID()) || n.MarkedForDeletion() || n.Node == nil ||
			!lo.ContainsBy(n.Node.Spec.Taints, func(t corev1.Taint) bool { return t.MatchTaint(&v1.DisruptedNoScheduleTaint) })
	})
	if len(outdated) == 0 {
		return nil
	}
	sort.Slice(outdated, func(i, j int) bool { return outdated[i].Name() < outdated[j].Name() })
	if err := state.RequireNoScheduleTaint(s.ctx, s.env.Client, false, outdated...); err != nil {
		return err
	}
	return state.ClearNodeClaimsCondition(s.ctx, s.env.Client, s.env.Clock, v1.ConditionTypeDisruptionReason, outdated...)
}

// syncConsolidatable plays the NodeClaim disruption controller's consolidation sub-reconciler: a claim is
// Consolidatable once it is initialized and consolidateAfter has passed since its last pod event, using upstream's
// own predicate.
func (s *Sim) syncConsolidatable() {
	np := &v1.NodePool{}
	if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: NodePoolName}, np); err != nil {
		s.fail(err)
		return
	}
	for _, sn := range s.env.Cluster.DeepCopyNodes() {
		nc := sn.NodeClaim
		if nc == nil || !nc.DeletionTimestamp.IsZero() || !nodepool.IsManaged(np, s.env.CloudProvider) {
			continue
		}
		want := nc.StatusConditions().Get(v1.ConditionTypeInitialized).IsTrue() && np.Spec.Disruption.ConsolidateAfter.Duration != nil &&
			!disruptionutils.IsUnderConsolidateAfter(np, nc, s.env.Clock)
		has := nc.StatusConditions().Get(v1.ConditionTypeConsolidatable).IsTrue()
		if want == has {
			continue
		}
		stored := nc.DeepCopy()
		if err := s.env.Client.Get(s.ctx, types.NamespacedName{Name: nc.Name}, stored); err != nil {
			continue
		}
		if want {
			stored.StatusConditions(status.WithClock(s.env.Clock)).SetTrue(v1.ConditionTypeConsolidatable)
		} else {
			_ = stored.StatusConditions(status.WithClock(s.env.Clock)).Clear(v1.ConditionTypeConsolidatable)
		}
		if err := s.env.Client.Status().Update(s.ctx, stored); err != nil {
			s.fail(err)
			return
		}
	}
}

// ensureQueueTicking reconciles the orchestration queue every second while it holds commands, as its workqueue
// requeues them (queueBaseDelay).
func (s *Sim) ensureQueueTicking() {
	if s.queueTicking || s.env.Queue.IsEmpty() {
		return
	}
	s.queueTicking = true
	s.after(time.Second, "queue", s.reconcileQueue)
}

func (s *Sim) reconcileQueue() {
	s.queueTicking = false
	cmds := s.env.Queue.GetCommands()
	sort.Slice(cmds, func(i, j int) bool {
		if !cmds[i].CreationTimestamp.Equal(cmds[j].CreationTimestamp) {
			return cmds[i].CreationTimestamp.Before(cmds[j].CreationTimestamp)
		}
		return cmds[i].Candidates[0].Name() < cmds[j].Candidates[0].Name()
	})
	for _, cmd := range cmds {
		if len(cmd.Candidates) == 0 {
			continue
		}
		if _, err := s.env.Queue.Reconcile(s.ctx, cmd.Candidates[0].NodeClaim); err != nil {
			s.fail(fmt.Errorf("reconciling disruption queue, %w", err))
			return
		}
		if cmd.Succeeded {
			s.m.commandCompleted(cmd)
		}
	}
	s.ensureQueueTicking()
}

// drainQueueSource empties the orchestration queue's event channel. In a manager the controller reads it; here the
// simulator reconciles commands directly, and an unread channel would block StartCommand once full.
func (s *Sim) drainQueueSource() {
	f := reflect.ValueOf(s.env.Queue).Elem().FieldByName("source")
	ch := reflect.NewAt(f.Type(), unsafe.Pointer(f.UnsafeAddr())).Elem() //nolint:gosec
	for {
		if _, ok := ch.TryRecv(); !ok {
			return
		}
	}
}

// --- B5: descheduler HighNodeUtilization ---------------------------------------------------------------------------

// descheduleTick evicts every pod from nodes whose CPU and memory requests are both below the threshold, so a
// MostAllocated scheduler repacks them and emptiness removes the nodes: the Kubernetes descheduler's
// HighNodeUtilization strategy. Evictions go through the PDB-respecting eviction path.
func (s *Sim) descheduleTick() {
	cfg := s.cfg.Arm.Descheduler
	if s.now().Sub(s.start) >= s.cfg.Warmup.Duration {
		var low []*simNode
		for _, n := range s.schedulableNodes() {
			if n.draining || len(n.pods) == 0 {
				continue
			}
			cpu, mem := n.used[0]/n.alloc[0], n.used[1]/n.alloc[1]
			if cpu < cfg.Threshold && mem < cfg.Threshold {
				low = append(low, n)
			}
		}
		for _, n := range low {
			for _, key := range sortedKeys(n.pods) {
				if p := s.pods[key]; p != nil && p.phase == podBound && !p.dnd {
					_ = s.evict(p, "descheduler")
				}
			}
		}
	}
	s.after(cfg.Interval.Duration, "descheduler", s.descheduleTick)
}
