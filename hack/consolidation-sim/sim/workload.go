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
	"fmt"
	"math"
	"sort"
	"strconv"
	"time"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline/harness"
	"sigs.k8s.io/karpenter/pkg/controllers/pod/deletioncost"
)

const namespace = "default"

func shapeRequests(s Shape) corev1.ResourceList {
	return corev1.ResourceList{
		corev1.ResourceCPU:    *resource.NewMilliQuantity(s.CPU, resource.DecimalSI),
		corev1.ResourceMemory: *resource.NewQuantity(s.Memory<<20, resource.BinarySI),
	}
}

// --- setup -------------------------------------------------------------------------------------------------------

// seed creates the workload's initial objects: deployments' PDBs, the initial nodes and their bound pods (fragmented
// starts), and the events that drive replica schedules and job arrivals.
func (s *Sim) seed() error {
	for _, d := range s.work.Deployments {
		s.deploys[d.Name] = &deployState{spec: d, pods: map[podKey]bool{}}
		if d.PDBMaxUnavailable >= 0 {
			name := "pdb-" + d.Name
			s.pdbs[d.Name] = name
			if err := s.env.Apply(s.ctx, harness.PDB(name, namespace, map[string]string{"app": d.Name}, d.PDBMaxUnavailable, 0, 0)); err != nil {
				return err
			}
		}
	}
	for i, in := range s.work.Initial {
		e, ok := s.cat.Get(in.InstanceType)
		if !ok {
			return fmt.Errorf("initial node type %s not in catalog", in.InstanceType)
		}
		name := fmt.Sprintf("init-%05d", i)
		if err := s.addInitialNode(name, e, in.Zone); err != nil {
			return err
		}
		for _, dep := range sortedStrings(lo.Keys(in.Pods)) {
			for range in.Pods[dep] {
				p, err := s.createPod(s.deploys[dep], false, orphan{})
				if err != nil {
					return err
				}
				if err := s.bind(p, name); err != nil {
					return err
				}
				if err := s.markReady(p); err != nil {
					return err
				}
			}
		}
	}
	for _, name := range sortedStrings(lo.Keys(s.deploys)) {
		d := s.deploys[name]
		for _, pt := range d.spec.Schedule {
			at := pt.At.Duration
			s.at(s.start.Add(at), "replicas "+name, func() { s.reconcileDeployment(d) })
		}
	}
	for _, jc := range s.work.Jobs {
		s.scheduleJobArrival(jc)
	}
	for i, t := range s.work.Tasks {
		task := t
		jc := JobClass{Name: fmt.Sprintf("task-%d", i%16), Shape: task.Shape}
		if _, ok := lo.Find(s.work.Jobs, func(j JobClass) bool { return j.Name == jc.Name }); !ok {
			s.work.Jobs = append(s.work.Jobs, jc)
		}
		s.at(s.start.Add(task.Start.Duration), "task", func() {
			if err := s.createTaskPod(jc.Name, task); err != nil {
				s.fail(err)
			}
		})
	}
	for _, name := range sortedStrings(lo.Keys(s.deploys)) {
		s.syncPDB(name)
	}
	return nil
}

func sortedStrings(s []string) []string {
	sort.Strings(s)
	return s
}

// --- pods --------------------------------------------------------------------------------------------------------

func (s *Sim) podName(owner string) string {
	s.podSeq[owner]++
	return fmt.Sprintf("%s-%06d", owner, s.podSeq[owner])
}

func (s *Sim) createPod(d *deployState, replaces bool, o orphan) (*simPod, error) {
	spec := d.spec
	name := s.podName(spec.Name)
	lineage := o.lineage
	if lineage == 0 {
		s.lineageSeq++
		lineage = s.lineageSeq
	}
	p := &simPod{key: podKey{Namespace: namespace, Name: name}, owner: spec.Name, shape: spec.Shape, req: pipeline.ResourcesFromList(shapeRequests(spec.Shape)),
		createdAt: s.now(), lineage: lineage, replaces: replaces, dnd: spec.DoNotDisrupt, planned: o.planned, steer: o.steer}
	ps := harness.PodSpec{Name: name, Namespace: namespace, Owner: spec.Name, Requests: shapeRequests(spec.Shape), Labels: map[string]string{"app": spec.Name},
		Annotations: map[string]string{}}
	if spec.DoNotDisrupt {
		ps.Annotations[v1.DoNotDisruptAnnotationKey] = "true"
	}
	if spec.EvictionCost != 0 {
		ps.Annotations[v1.DisruptionCostAnnotationKey] = strconv.Itoa(spec.EvictionCost)
		p.evictionCost = float64(spec.EvictionCost) / math.Pow(2, 27)
	}
	p.evictionCost = math.Max(0, math.Min(10, 1+p.evictionCost))
	if spec.ZoneSpread {
		ps.Spread = []corev1.TopologySpreadConstraint{{MaxSkew: 1, TopologyKey: corev1.LabelTopologyZone, WhenUnsatisfiable: corev1.DoNotSchedule,
			LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": spec.Name}}}}
	}
	if spec.HostAntiAffinity {
		ps.Affinity = &corev1.Affinity{PodAntiAffinity: &corev1.PodAntiAffinity{RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
			TopologyKey: corev1.LabelHostname, LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": spec.Name}}}}}}
	}
	pod := s.env.Pod(ps)
	// New pods have not been seen by the scheduler yet; the scheduler model marks them unschedulable if they fail.
	pod.Status.Conditions = nil
	if err := s.env.Apply(s.ctx, pod); err != nil {
		return nil, err
	}
	s.pods[p.key] = p
	d.pods[p.key] = true
	s.m.podCreated(p)
	s.kickScheduler()
	return p, nil
}

func (s *Sim) createJobPod(jc JobClass, duration time.Duration, lineage int) error {
	name := s.podName(jc.Name)
	if lineage == 0 {
		s.lineageSeq++
		lineage = s.lineageSeq
	}
	p := &simPod{key: podKey{Namespace: namespace, Name: name}, owner: jc.Name, job: true, shape: jc.Shape, req: pipeline.ResourcesFromList(shapeRequests(jc.Shape)),
		createdAt: s.now(), lineage: lineage, jobDuration: duration, evictionCost: 1}
	pod := s.env.Pod(harness.PodSpec{Name: name, Namespace: namespace, Requests: shapeRequests(jc.Shape), Labels: map[string]string{"job": jc.Name}})
	pod.OwnerReferences = []metav1.OwnerReference{{APIVersion: "batch/v1", Kind: "Job", Name: name, UID: types.UID("job/" + name), Controller: lo.ToPtr(true)}}
	pod.Status.Conditions = nil
	if err := s.env.Apply(s.ctx, pod); err != nil {
		return err
	}
	s.pods[p.key] = p
	s.m.podCreated(p)
	s.kickScheduler()
	return nil
}

// createTaskPod creates a trace task's pod. Tasks share sixteen owner names so jobs-by-owner lookups stay small;
// each task keeps its own shape and duration.
func (s *Sim) createTaskPod(owner string, t Task) error {
	name := s.podName(owner)
	s.lineageSeq++
	p := &simPod{key: podKey{Namespace: namespace, Name: name}, owner: owner, job: true, shape: t.Shape, req: pipeline.ResourcesFromList(shapeRequests(t.Shape)),
		createdAt: s.now(), lineage: s.lineageSeq, jobDuration: t.Duration.Duration, evictionCost: 1}
	pod := s.env.Pod(harness.PodSpec{Name: name, Namespace: namespace, Requests: shapeRequests(t.Shape), Labels: map[string]string{"job": owner}})
	pod.OwnerReferences = []metav1.OwnerReference{{APIVersion: "batch/v1", Kind: "Job", Name: name, UID: types.UID("job/" + name), Controller: lo.ToPtr(true)}}
	pod.Status.Conditions = nil
	if err := s.env.Apply(s.ctx, pod); err != nil {
		return err
	}
	s.pods[p.key] = p
	s.m.podCreated(p)
	s.kickScheduler()
	return nil
}

// bind places a pod on a node: what kube-scheduler's binding and kubelet's start do, plus the podevents
// controller's LastPodEventTime update that resets consolidateAfter.
func (s *Sim) bind(p *simPod, node string) error {
	n := s.nodes[node]
	pod := &corev1.Pod{}
	if err := s.env.Client.Get(s.ctx, p.key, pod); err != nil {
		return err
	}
	pod.Spec.NodeName = node
	if err := s.env.Client.Update(s.ctx, pod); err != nil {
		return err
	}
	pod.Status.Phase = corev1.PodRunning
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}, {Type: corev1.PodReady, Status: corev1.ConditionFalse}}
	if err := s.env.Client.Status().Update(s.ctx, pod); err != nil {
		return err
	}
	p.node, p.phase, p.boundAt = node, podBound, s.now()
	n.used = n.used.Add(p.req)
	n.pods[p.key] = true
	if _, err := s.podEvents.Reconcile(s.ctx, pod); err != nil {
		return err
	}
	s.m.podBound(p, n)
	return nil
}

func (s *Sim) markReady(p *simPod) error {
	pod := &corev1.Pod{}
	if err := s.env.Client.Get(s.ctx, p.key, pod); err != nil {
		return nil
	}
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}, {Type: corev1.PodReady, Status: corev1.ConditionTrue}}
	if err := s.env.Client.Status().Update(s.ctx, pod); err != nil {
		return err
	}
	p.ready = true
	if p.job {
		dur := p.jobDuration
		key := p.key
		s.after(dur, "complete "+key.Name, func() { s.completeJob(key) })
	}
	s.syncPDB(p.owner)
	return nil
}

func (s *Sim) completeJob(key podKey) {
	p, ok := s.pods[key]
	if !ok || p.phase != podBound {
		return
	}
	s.m.jobCompleted(p)
	s.removePod(p)
}

// removePod deletes a pod at once (scale-down after shutdown, job completion, evicted pod gone).
func (s *Sim) removePod(p *simPod) {
	pod := &corev1.Pod{}
	if err := s.env.Client.Get(s.ctx, p.key, pod); err == nil {
		if err := s.env.Client.Delete(s.ctx, pod); err != nil && !apierrors.IsNotFound(err) {
			s.fail(err)
			return
		}
		if p.node != "" {
			_, _ = s.podEvents.Reconcile(s.ctx, pod)
		}
	}
	if n, ok := s.nodes[p.node]; ok {
		n.used = n.used.Sub(p.req)
		delete(n.pods, p.key)
	}
	delete(s.pods, p.key)
	if d, ok := s.deploys[p.owner]; ok {
		delete(d.pods, p.key)
	}
	s.syncPDB(p.owner)
	s.kickScheduler()
}

// terminatePod marks a pod terminating and removes it after the shutdown latency. Terminating pods still hold their
// node's resources, as they do until kubelet confirms they are gone.
func (s *Sim) terminatePod(p *simPod) {
	if p.phase == podTerminating {
		return
	}
	p.phase = podTerminating
	if d, ok := s.deploys[p.owner]; ok {
		delete(d.pods, p.key)
	}
	key := p.key
	s.after(s.draw(s.cfg.Latency.PodShutdown), "gone "+key.Name, func() {
		if q, ok := s.pods[key]; ok {
			s.removePod(q)
		}
	})
	s.syncPDB(p.owner)
}

// --- evictions -----------------------------------------------------------------------------------------------------

var errPDB = fmt.Errorf("would violate a pod disruption budget")

// evict is an Eviction API call: refused when the pod's PDB has no disruptions left, as the API server refuses it.
// The owner replaces an evicted replica at once; a job restarts from the beginning.
func (s *Sim) evict(p *simPod, reason string) error {
	if p.phase == podTerminating {
		return nil
	}
	if pdb, ok := s.pdbs[p.owner]; ok && p.phase == podBound {
		if s.pdbAllowed(p.owner) <= 0 {
			s.m.evictionRefused(p, pdb)
			return errPDB
		}
	}
	if p.dnd {
		s.m.violation("do-not-disrupt", p, reason)
	}
	o := orphan{lineage: p.lineage, planned: s.m.plannedFor(p), steer: s.steers[p.key]}
	delete(s.steers, p.key)
	s.m.podEvicted(p, reason)
	s.terminatePod(p)
	if d, ok := s.deploys[p.owner]; ok {
		d.orphans = append(d.orphans, o)
		s.reconcileDeployment(d)
	} else if p.job {
		jc, _ := lo.Find(s.work.Jobs, func(j JobClass) bool { return j.Name == p.owner })
		jc.Shape = p.shape
		if err := s.createJobPod(jc, p.jobDuration, p.lineage); err != nil {
			s.fail(err)
		}
	}
	return nil
}

// onEvictAPI handles Eviction API calls from Karpenter or pipeline executors.
func (s *Sim) onEvictAPI(_ ctxT, key types.NamespacedName) error {
	p, ok := s.pods[key]
	if !ok {
		return apierrors.NewNotFound(schema.GroupResource{Resource: "pods"}, key.Name)
	}
	if err := s.evict(p, "eviction-api"); err != nil {
		return apierrors.NewTooManyRequests("Cannot evict pod as it would violate the pod's disruption budget.", 10)
	}
	return nil
}

// pdbAllowed recomputes a deployment's PDB allowance from live pod state: healthy replicas minus the minimum the
// budget keeps.
func (s *Sim) pdbAllowed(owner string) int {
	d, ok := s.deploys[owner]
	if !ok {
		return math.MaxInt32
	}
	healthy := 0
	for k := range d.pods {
		if p := s.pods[k]; p != nil && p.phase == podBound && p.ready {
			healthy++
		}
	}
	return healthy - (d.desired - d.spec.PDBMaxUnavailable)
}

// syncPDB writes a deployment's PDB status, which Karpenter reads when it builds candidates and simulates.
func (s *Sim) syncPDB(owner string) {
	name, ok := s.pdbs[owner]
	if !ok {
		return
	}
	d := s.deploys[owner]
	healthy := 0
	for k := range d.pods {
		if p := s.pods[k]; p != nil && p.phase == podBound && p.ready {
			healthy++
		}
	}
	want := harness.PDB(name, namespace, map[string]string{"app": owner}, d.spec.PDBMaxUnavailable, healthy, d.desired)
	cur := want.DeepCopy()
	if err := s.env.Client.Get(s.ctx, client.ObjectKeyFromObject(want), cur); err != nil {
		return
	}
	if cur.Status.DisruptionsAllowed == want.Status.DisruptionsAllowed && cur.Status.CurrentHealthy == want.Status.CurrentHealthy &&
		cur.Status.ExpectedPods == want.Status.ExpectedPods {
		return
	}
	cur.Status = want.Status
	if err := s.env.Client.Status().Update(s.ctx, cur); err != nil {
		s.fail(fmt.Errorf("updating pdb %s, %w", name, err))
	}
}

// --- deployments and jobs ------------------------------------------------------------------------------------------

func (s *Sim) reconcileDeployment(d *deployState) {
	d.desired = d.spec.replicasAt(s.now().Sub(s.start))
	active := len(d.pods)
	for active < d.desired {
		var o orphan
		replaces := false
		if len(d.orphans) > 0 {
			o, d.orphans = d.orphans[0], d.orphans[1:]
			replaces = true
		}
		if _, err := s.createPod(d, replaces, o); err != nil {
			s.fail(err)
			return
		}
		active++
	}
	if active > d.desired {
		for _, p := range s.scaleDownVictims(d, active-d.desired) {
			s.m.podScaledDown(p)
			if p.phase == podPending {
				s.removePod(p)
			} else {
				s.terminatePod(p)
			}
		}
	}
	d.orphans = nil
	s.syncPDB(d.spec.Name)
}

func (s *Sim) scheduleJobArrival(jc JobClass) {
	if jc.RatePerHour <= 0 {
		return
	}
	gap := time.Duration(s.rngWork.ExpFloat64() / jc.RatePerHour * float64(time.Hour))
	at := s.now().Add(gap)
	if at.Sub(s.start) > jc.Until.Duration {
		return
	}
	s.at(at, "arrive "+jc.Name, func() {
		dur := time.Duration(float64(jc.MedianDuration.Duration) * math.Exp(jc.Sigma*s.rngWork.NormFloat64()))
		dur = max(30*time.Second, min(dur, 12*time.Hour))
		if err := s.createJobPod(jc, dur, 0); err != nil {
			s.fail(err)
			return
		}
		s.scheduleJobArrival(jc)
	})
}

// scaleDownVictims picks which replicas a ReplicaSet deletes.
//
//   - spread: the ReplicaSet controller's default ranking: unscheduled first, then not ready, then pods on nodes
//     with more replicas of the same set (spreading), then the newest.
//   - deletion-cost: the same ranking with pod-deletion-cost first, from Karpenter's PodDeletionCostManagement
//     ranking (deletioncost.RankNodes): pods on nodes it would consolidate first get the lowest cost.
//   - random: uniform.
func (s *Sim) scaleDownVictims(d *deployState, k int) []*simPod {
	pods := lo.Map(sortedKeys(d.pods), func(key podKey, _ int) *simPod { return s.pods[key] })
	if s.cfg.ScaleDown == ScaleDownRandom {
		s.rngWork.Shuffle(len(pods), func(i, j int) { pods[i], pods[j] = pods[j], pods[i] })
		return pods[:k]
	}
	colocated := map[string]int{}
	for _, p := range pods {
		if p.node != "" {
			colocated[p.node]++
		}
	}
	cost := map[string]int{}
	if s.cfg.ScaleDown == ScaleDownDeletionCost {
		cost = s.deletionCostRanks()
	}
	sort.SliceStable(pods, func(i, j int) bool {
		a, b := pods[i], pods[j]
		if (a.node == "") != (b.node == "") {
			return a.node == ""
		}
		if a.ready != b.ready {
			return !a.ready
		}
		if ca, cb := cost[a.node], cost[b.node]; ca != cb {
			return ca < cb
		}
		if colocated[a.node] != colocated[b.node] {
			return colocated[a.node] > colocated[b.node]
		}
		if !a.createdAt.Equal(b.createdAt) {
			return a.createdAt.After(b.createdAt)
		}
		return a.key.Name > b.key.Name
	})
	return pods[:k]
}

// deletionCostRanks returns the pod-deletion-cost Karpenter's controller would write on each node's pods.
func (s *Sim) deletionCostRanks() map[string]int {
	nodes := s.env.Cluster.DeepCopyNodes()
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].Name() < nodes[j].Name() })
	npMap, itMap, err := disruption.BuildNodePoolMap(s.ctx, s.env.Client, s.env.CloudProvider)
	if err != nil {
		return nil
	}
	a, bc, _, err := deletioncost.RankNodes(s.ctx, s.env.Client, s.env.Clock, nodes, npMap, itMap)
	if err != nil {
		return nil
	}
	out := map[string]int{}
	for _, n := range a {
		out[n.Name()] = math.MinInt32
	}
	for i, n := range bc {
		out[n.Name()] = deletioncost.RankForBC(i, len(bc))
	}
	return out
}
