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

package harness

import (
	"context"
	"fmt"
	"math/rand"
	"sort"
	"strconv"
	"time"

	"github.com/samber/lo"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// ParityState describes a generated cluster state.
type ParityState struct {
	Seed      int64  `json:"seed"`
	Policy    string `json:"policy"`
	NodePools int    `json:"nodePools"`
	Nodes     int    `json:"nodes"`
	Pods      int    `json:"pods"`
	Pending   int    `json:"pending"`
	PDBs      int    `json:"pdbs"`
}

// StateOptions bound generated states.
type StateOptions struct {
	Policy        v1.ConsolidationPolicy
	InstanceTypes []*cloudprovider.InstanceType
	MinNodes      int
	MaxNodes      int
}

var (
	cpuSizes = []string{"100m", "250m", "500m", "750m", "1", "1500m", "2", "3"}
	memSizes = []string{"128Mi", "256Mi", "512Mi", "1Gi", "2Gi", "3Gi", "4Gi", "6Gi", "8Gi"}
	budgets  = []string{"100%", "100%", "100%", "50%", "10%", "3", "2", "1", "0"}
)

type deployment struct {
	name       string
	requests   corev1.ResourceList
	labels     map[string]string
	annotation map[string]string
	priority   *int32
	antiAff    bool
}

// GenerateState writes a seeded, fragmented cluster state into the environment: a packed cluster built by first-fit
// placement of deployment replicas, then a random scale-down that leaves nodes partly empty, with occasional PDBs,
// do-not-disrupt pods and nodes, eviction-cost annotations, priorities, DaemonSets, uninitialized or not yet
// consolidatable nodes, hostname anti-affinity, restrictive budgets and pending pods. It is the parity test's input
// and reuses the spread of shapes the scheduling property tests draw from.
//
//nolint:gocyclo
func GenerateState(ctx context.Context, e *Env, seed int64, opts StateOptions) (ParityState, error) {
	rng := rand.New(rand.NewSource(seed)) //nolint:gosec
	st := ParityState{Seed: seed, Policy: string(opts.Policy)}
	nps := 1 + lo.Ternary(rng.Float64() < 0.35, 1, 0) + lo.Ternary(rng.Float64() < 0.1, 1, 0)
	st.NodePools = nps
	pools := make([]*v1.NodePool, nps)
	for i := range nps {
		pools[i] = NodePool(fmt.Sprintf("pool-%d", i), opts.Policy, "0s", budgets[rng.Intn(len(budgets))])
		pools[i].Spec.Weight = new(i32(10 * (nps - i)))
		if err := e.Apply(ctx, pools[i]); err != nil {
			return st, err
		}
	}
	n := opts.MinNodes + rng.Intn(opts.MaxNodes-opts.MinNodes+1)
	st.Nodes = n
	type nodeInfo struct {
		name  string
		alloc corev1.ResourceList
		used  corev1.ResourceList
	}
	nodes := make([]*nodeInfo, 0, n)
	for i := range n {
		it := opts.InstanceTypes[rng.Intn(len(opts.InstanceTypes))]
		off := it.Offerings.Available()[rng.Intn(len(it.Offerings.Available()))]
		name := fmt.Sprintf("node-%03d", i)
		spec := NodeSpec{Name: name, NodePool: pools[rng.Intn(nps)].Name, InstanceType: it,
			Zone: off.Requirements.Get(corev1.LabelTopologyZone).Any(), CapacityType: off.CapacityType(),
			Uninitialized: rng.Float64() < 0.03, NotConsolidatable: rng.Float64() < 0.07, DoNotDisrupt: rng.Float64() < 0.03}
		if _, _, err := e.AddNode(ctx, spec); err != nil {
			return st, err
		}
		nodes = append(nodes, &nodeInfo{name: name, alloc: it.Allocatable(), used: corev1.ResourceList{}})
	}
	if rng.Float64() < 0.3 {
		for _, nd := range nodes {
			req := Requests("100m", "128Mi")
			if _, err := e.AddPod(ctx, PodSpec{Name: "ds-" + nd.name, Owner: "ds", DaemonSet: true, Requests: req, NodeName: nd.name}); err != nil {
				return st, err
			}
			nd.used = add(nd.used, req)
			st.Pods++
		}
	}
	deps := make([]deployment, 3+rng.Intn(10))
	for i := range deps {
		d := deployment{name: fmt.Sprintf("app-%02d", i), requests: Requests(cpuSizes[rng.Intn(len(cpuSizes))], memSizes[rng.Intn(len(memSizes))]),
			labels: map[string]string{"app": fmt.Sprintf("app-%02d", i)}, annotation: map[string]string{}}
		switch r := rng.Float64(); {
		case r < 0.04:
			d.annotation[v1.DoNotDisruptAnnotationKey] = "true"
		case r < 0.14:
			d.annotation[v1.DisruptionCostAnnotationKey] = strconv.Itoa(rng.Intn(1<<28) - 1<<27)
		}
		if rng.Float64() < 0.05 {
			d.priority = new(i32(rng.Intn(1000)))
		}
		d.antiAff = rng.Float64() < 0.08
		deps[i] = d
	}
	// Pack replicas first-fit over a random node order, then delete a random share: the fragmented start.
	type placed struct {
		dep  int
		node *nodeInfo
		name string
	}
	var replicas []placed
	order := rng.Perm(len(nodes))
	for di, d := range deps {
		count := 2 + rng.Intn(4*n/len(deps)+2)
		for r := range count {
			for _, oi := range order {
				nd := nodes[oi]
				if d.antiAff && lo.ContainsBy(replicas, func(p placed) bool { return p.dep == di && p.node == nd }) {
					continue
				}
				if fits(add(nd.used, d.requests), nd.alloc) {
					nd.used = add(nd.used, d.requests)
					replicas = append(replicas, placed{dep: di, node: nd, name: fmt.Sprintf("%s-%03d", d.name, r)})
					break
				}
			}
		}
	}
	keep := 0.3 + 0.6*rng.Float64()
	podsPerDep := map[int]int{}
	for _, rp := range replicas {
		if rng.Float64() > keep {
			continue
		}
		d := deps[rp.dep]
		spec := PodSpec{Name: rp.name, Owner: d.name, Requests: d.requests, Labels: d.labels, Annotations: d.annotation, NodeName: rp.node.name, Priority: d.priority}
		if d.antiAff {
			spec.Affinity = &corev1.Affinity{PodAntiAffinity: &corev1.PodAntiAffinity{RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
				TopologyKey: corev1.LabelHostname, LabelSelector: metavSelector(d.labels)}}}}
		}
		if _, err := e.AddPod(ctx, spec); err != nil {
			return st, err
		}
		podsPerDep[rp.dep]++
		st.Pods++
	}
	for di, d := range deps {
		if podsPerDep[di] == 0 || rng.Float64() > 0.2 {
			continue
		}
		maxUnavailable := lo.Ternary(rng.Float64() < 0.3, 0, 1)
		if err := e.Apply(ctx, PDB("pdb-"+d.name, "", d.labels, maxUnavailable, podsPerDep[di], podsPerDep[di])); err != nil {
			return st, err
		}
		st.PDBs++
	}
	if rng.Float64() < 0.1 {
		for i := range 1 + rng.Intn(3) {
			d := deps[rng.Intn(len(deps))]
			if _, err := e.AddPod(ctx, PodSpec{Name: fmt.Sprintf("pending-%d", i), Owner: d.name, Requests: d.requests, Labels: d.labels}); err != nil {
				return st, err
			}
			st.Pending++
		}
	}
	e.Cluster.SetSynced(true)
	return st, nil
}

func add(a, b corev1.ResourceList) corev1.ResourceList {
	out := a.DeepCopy()
	for k, v := range b {
		q := out[k]
		q.Add(v)
		out[k] = q
	}
	return out
}

func fits(used, alloc corev1.ResourceList) bool {
	for k, v := range used {
		if a, ok := alloc[k]; ok && v.Cmp(a) > 0 {
			return false
		}
	}
	return true
}

func metavSelector(l map[string]string) *metav1.LabelSelector {
	return &metav1.LabelSelector{MatchLabels: l}
}

// Decision is what one consolidation pass decided, in the form the parity test compares.
type Decision struct {
	Method             string         `json:"method"` // which upstream method (or pipeline round) produced it
	Kind               string         `json:"kind"`
	Candidates         []string       `json:"candidates"`
	ReplacementOptions [][]string     `json:"replacementOptions,omitempty"`
	BudgetConsumed     map[string]int `json:"budgetConsumed,omitempty"`
	Error              string         `json:"error,omitempty"`
}

func decisionOf(method string, cmds []disruption.Command) Decision {
	cmds = lo.Filter(cmds, func(c disruption.Command, _ int) bool { return c.Decision() != disruption.NoOpDecision })
	if len(cmds) == 0 {
		return Decision{Kind: string(disruption.NoOpDecision)}
	}
	d := Decision{Method: method, BudgetConsumed: map[string]int{}}
	kinds := lo.Map(cmds, func(c disruption.Command, _ int) string { return string(c.Decision()) })
	d.Kind = lo.Reduce(kinds, func(acc, k string, i int) string { return lo.Ternary(i == 0, k, acc+","+k) }, "")
	for _, c := range cmds {
		for _, cand := range c.Candidates {
			d.Candidates = append(d.Candidates, cand.Name())
			d.BudgetConsumed[cand.NodePool.Name]++
		}
		for _, r := range c.Replacements {
			d.ReplacementOptions = append(d.ReplacementOptions, lo.Map(r.InstanceTypeOptions, func(it *cloudprovider.InstanceType, _ int) string { return it.Name }))
		}
	}
	return d
}

// Equal compares decisions field by field.
func (d Decision) Equal(o Decision) bool {
	return d.Kind == o.Kind && lo.ElementsMatch(d.Candidates, o.Candidates) && sameOrder(d.Candidates, o.Candidates) &&
		fmt.Sprint(d.ReplacementOptions) == fmt.Sprint(o.ReplacementOptions) && fmt.Sprint(d.BudgetConsumed) == fmt.Sprint(o.BudgetConsumed) &&
		d.Error == o.Error
}

func sameOrder(a, b []string) bool { return fmt.Sprint(a) == fmt.Sprint(b) }

// zeroDelay validates immediately: in a static state the validation wait changes nothing, and the parity test
// should exercise validation without waiting on the fake clock.
type zeroDelay struct{ disruption.Validator }

func (z zeroDelay) Validate(ctx context.Context, cmd disruption.Command, _ time.Duration) (disruption.Command, error) {
	return z.Validator.Validate(ctx, cmd, 0)
}

// ZeroDelay wraps a validator to skip the validation wait.
func ZeroDelay(v disruption.Validator) disruption.Validator { return zeroDelay{v} }

// SortedCandidates fetches this cycle's consolidation candidates and totals and orders candidates by name, the
// deterministic order both parity arms receive in place of the controller's map order.
func (e *Env) SortedCandidates(ctx context.Context, shouldDisrupt disruption.CandidateFilter) ([]*disruption.Candidate, map[string]disruption.NodePoolTotals, error) {
	cands, totals, err := disruption.GetCandidatesWithTotals(ctx, e.Cluster, e.Client, e.Recorder, e.Clock, e.CloudProvider, shouldDisrupt,
		disruption.GracefulDisruptionClass, e.Queue, e.ClusterCost)
	if err != nil {
		return nil, nil, err
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].Name() < cands[j].Name() })
	return cands, totals, nil
}

// UpstreamDecision runs multi-node then single-node consolidation the way the disruption controller does (fresh
// candidates, totals and budgets per method; the first method that returns commands wins), with candidates in name
// order and validation that does not wait.
func (e *Env) UpstreamDecision(ctx context.Context) Decision {
	c := disruption.MakeConsolidation(e.Clock, e.Cluster, e.Client, e.Provisioner, e.CloudProvider, e.Recorder, e.Queue)
	methods := []disruption.Method{
		disruption.NewMultiNodeConsolidation(c, disruption.WithValidator(ZeroDelay(disruption.NewMultiConsolidationValidator(c)))),
		disruption.NewSingleNodeConsolidation(c, disruption.WithValidator(ZeroDelay(disruption.NewSingleConsolidationValidator(c)))),
	}
	for _, m := range methods {
		cands, totals, err := e.SortedCandidates(ctx, m.ShouldDisrupt)
		if err != nil {
			return Decision{Error: err.Error()}
		}
		if len(cands) == 0 {
			continue
		}
		m.(disruption.NodePoolTotalsSetter).SetNodePoolTotals(totals)
		budgets, err := disruption.BuildDisruptionBudgetMapping(ctx, e.Cluster, e.Clock, e.Client, e.CloudProvider, e.Recorder, m.Reason())
		if err != nil {
			return Decision{Error: err.Error()}
		}
		cmds, err := m.ComputeCommands(ctx, budgets, cands...)
		if err != nil {
			return Decision{Error: err.Error()}
		}
		if d := decisionOf(m.ConsolidationType(), cmds); d.Kind != string(disruption.NoOpDecision) {
			return d
		}
	}
	return Decision{Kind: string(disruption.NoOpDecision)}
}

// PipelineDecision runs one pipeline cycle on the same inputs.
func (e *Env) PipelineDecision(ctx context.Context, cfg pipeline.Config, rec pipeline.Recorder) Decision {
	m, err := pipeline.NewMethod(e.Clock, e.Client, e.Cluster, e.Provisioner, e.CloudProvider, e.Recorder, e.Queue, e.ClusterCost, cfg,
		pipeline.MethodOptions{Upstream: pipeline.UpstreamOptions{WrapValidator: ZeroDelay, OrderCandidates: byName}, Recorder: rec, RunID: "parity"})
	if err != nil {
		return Decision{Error: err.Error()}
	}
	cands, totals, err := e.SortedCandidates(ctx, m.ShouldDisrupt)
	if err != nil {
		return Decision{Error: err.Error()}
	}
	if len(cands) == 0 {
		return Decision{Kind: string(disruption.NoOpDecision)}
	}
	m.SetNodePoolTotals(totals)
	budgets, err := disruption.BuildDisruptionBudgetMapping(ctx, e.Cluster, e.Clock, e.Client, e.CloudProvider, e.Recorder, m.Reason())
	if err != nil {
		return Decision{Error: err.Error()}
	}
	cmds, err := m.ComputeCommands(ctx, budgets, cands...)
	if err != nil {
		return Decision{Error: err.Error()}
	}
	d := decisionOf("", cmds)
	if len(cmds) > 0 {
		// The pipeline reports itself as one method; name the upstream method its winning move came from.
		// Multi-node commands always hold at least two candidates and single-node commands exactly one.
		d.Method = lo.Ternary(len(cmds[0].Candidates) > 1, disruption.MultiNodeConsolidationType.Name, disruption.SingleNodeConsolidationType.Name)
	}
	return d
}

func byName(cands []*disruption.Candidate) []*disruption.Candidate {
	sort.Slice(cands, func(i, j int) bool { return cands[i].Name() < cands[j].Name() })
	return cands
}
