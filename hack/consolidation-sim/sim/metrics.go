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
	"io"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/samber/lo"
	"k8s.io/apimachinery/pkg/types"

	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// Fixed windows from the plan's decisions table.
const (
	regretWindow       = 15 * time.Minute
	reDisruptionWindow = 30 * time.Minute
	bandLow, bandHigh  = 0.2, 0.8
)

type evictionRecord struct {
	at     time.Time
	node   string
	reason string
}

type planRecord struct {
	at        time.Time
	sources   []string
	predicted float64 // ΔP2 the plan predicted, $/hr-weighted (unnormalized)
	before    float64 // world P2 when the plan started, $/hr-weighted
	realized  float64
	done      bool
}

// collector computes every metric from the world's own events, identically for every arm.
type collector struct {
	s *Sim

	totalPrice   float64
	lastChange   time.Time
	costIntegral float64 // $ (price x hours), whole run
	costPost     float64 // $ after warmup

	launched     map[string]time.Time
	removedAt    map[string]time.Time
	nodesRemoved int
	churnNodes   int

	evictions      []evictionRecord
	evictionsBy    map[string]int
	evictionCost   float64
	lastEvicted    map[int]time.Time // lineage -> last eviction
	reDisruptions  int
	refusedByPDB   int
	scaleDowns     int
	jobsCompleted  int
	violations     []string
	planned        map[types.NamespacedName]string
	fidelityHits   int
	fidelityMisses int

	waitTotal  time.Duration
	binds      int
	createdAt  map[types.NamespacedName]time.Time
	pendingSec float64

	passes      []time.Duration
	passMethods map[string]int
	started     map[string]int
	completed   int
	failed      []string
	plans       []*planRecord
	sourcePlan  map[string]*planRecord

	rows    []string
	podsets []string
}

func newCollector(s *Sim) *collector {
	return &collector{s: s, lastChange: s.start, launched: map[string]time.Time{}, removedAt: map[string]time.Time{},
		evictionsBy: map[string]int{}, lastEvicted: map[int]time.Time{}, planned: map[types.NamespacedName]string{},
		createdAt: map[types.NamespacedName]time.Time{}, passMethods: map[string]int{}, started: map[string]int{},
		sourcePlan: map[string]*planRecord{}}
}

func (c *collector) advanceCost() {
	now := c.s.now()
	dt := now.Sub(c.lastChange).Hours()
	c.costIntegral += c.totalPrice * dt
	warm := c.s.start.Add(c.s.cfg.Warmup.Duration)
	if now.After(warm) {
		from := c.lastChange
		if from.Before(warm) {
			from = warm
		}
		c.costPost += c.totalPrice * now.Sub(from).Hours()
	}
	c.lastChange = now
}

func (c *collector) nodeLaunched(n *simNode) {
	c.advanceCost()
	c.totalPrice += n.price
	c.launched[n.name] = c.s.now()
}

func (c *collector) nodeDrainStarted(*simNode) {}

func (c *collector) nodeRemoved(n *simNode) {
	c.advanceCost()
	c.totalPrice -= n.price
	now := c.s.now()
	c.removedAt[n.name] = now
	c.nodesRemoved++
	if l, ok := c.launched[n.name]; ok && !n.initial && l.After(c.s.start.Add(c.s.cfg.Warmup.Duration)) && now.Sub(l) < regretWindow {
		c.churnNodes++
	}
	if pr, ok := c.sourcePlan[n.name]; ok && !pr.done {
		if lo.EveryBy(pr.sources, func(src string) bool { _, gone := c.removedAt[src]; return gone }) {
			pr.done = true
			pr.realized = c.worldP2() - pr.before
		}
	}
}

func (c *collector) podCreated(p *simPod) { c.createdAt[p.key] = c.s.now() }

func (c *collector) podBound(p *simPod, n *simNode) {
	if t, ok := c.createdAt[p.key]; ok {
		c.waitTotal += c.s.now().Sub(t)
		c.pendingSec += c.s.now().Sub(t).Seconds()
		c.binds++
		delete(c.createdAt, p.key)
	}
	if p.replaces && p.planned != "" {
		if n.name == p.planned {
			c.fidelityHits++
		} else {
			c.fidelityMisses++
		}
	}
}

func (c *collector) podEvicted(p *simPod, reason string) {
	now := c.s.now()
	c.evictions = append(c.evictions, evictionRecord{at: now, node: p.node, reason: reason})
	c.evictionsBy[reason]++
	c.evictionCost += p.evictionCost
	if last, ok := c.lastEvicted[p.lineage]; ok && now.Sub(last) < reDisruptionWindow {
		c.reDisruptions++
	}
	c.lastEvicted[p.lineage] = now
}

func (c *collector) evictionRefused(*simPod, string) { c.refusedByPDB++ }
func (c *collector) podScaledDown(*simPod)           { c.scaleDowns++ }
func (c *collector) jobCompleted(*simPod)            { c.jobsCompleted++ }

func (c *collector) violation(kind string, p *simPod, reason string) {
	c.violations = append(c.violations, fmt.Sprintf("%s %s %s at %s", kind, p.key, reason, c.s.now().Sub(c.s.start)))
}

func (c *collector) plannedFor(p *simPod) string { return c.planned[p.key] }

func (c *collector) commandFailed(err error) { c.failed = append(c.failed, err.Error()) }

// commandStarted records the plan a command made: where each displaced pod should land (for plan fidelity) and the
// potential change it predicts (for realized vs predicted ΔΦ).
func (c *collector) commandStarted(m disruption.Method, cmd *disruption.Command) {
	c.started[string(cmd.Decision())]++
	for _, en := range cmd.Results.ExistingNodes {
		for _, p := range en.Pods {
			c.planned[types.NamespacedName{Namespace: p.Namespace, Name: p.Name}] = en.Name()
		}
	}
	for i, r := range cmd.Replacements {
		if i < len(cmd.Results.NewNodeClaims) {
			for _, p := range cmd.Results.NewNodeClaims[i].Pods {
				c.planned[types.NamespacedName{Namespace: p.Namespace, Name: p.Name}] = r.Name
			}
		}
	}
	snap := c.worldState()
	mv := pipeline.MoveFromCommand(snap, *cmd, fmt.Sprintf("cmd-%d", c.s.cmdSeq), m.ConsolidationType(), true)
	c.s.cmdSeq++
	pr := &planRecord{at: c.s.now(), sources: cmd.SourceNodeNames(), before: c.worldP2(), predicted: p2.Delta(snap, mv) * snap.Normalizer().TotalPrice}
	c.plans = append(c.plans, pr)
	for _, src := range pr.sources {
		c.sourcePlan[src] = pr
	}
	if c.s.pipe == nil {
		// Validation can return a command's candidates in map order (EmptinessValidator rebuilds them from
		// GetCandidates), an order with no effect but the log's; sort so the log is reproducible.
		sources := append([]string(nil), mv.Sources...)
		sort.Strings(sources)
		c.s.decisions.Record(pipeline.DecisionEvent{SchemaVersion: pipeline.DecisionLogSchemaVersion, RunID: c.s.cfg.RunID,
			T: c.s.now().Sub(c.s.start).Seconds(), Stage: pipeline.StageExecute, Strategy: m.ConsolidationType(), MoveID: mv.ID, Kind: string(cmd.Decision()),
			Sources: sources, Destinations: mv.Destinations(), Pods: len(mv.Bindings), DeltaPotential: pr.predicted,
			Disruption:     lo.SumBy(cmd.Candidates, func(cd *disruption.Candidate) float64 { return cd.RescheduleDisruptionCost }),
			SavingsPerHour: cmd.EstimatedSavings(), Accepted: true})
	}
}

// ObservePlan records a pipeline move's planned placements; executors that evict pods themselves call it.
func (c *collector) ObservePlan(m pipeline.Move) {
	for _, b := range m.Bindings {
		if b.Target.Launch < 0 {
			c.planned[types.NamespacedName{Namespace: b.Pod.Namespace, Name: b.Pod.Name}] = b.Target.Node
		}
	}
	snap := c.worldState()
	pr := &planRecord{at: c.s.now(), sources: m.Remove, before: c.worldP2(), predicted: p2.Delta(snap, m) * snap.Normalizer().TotalPrice}
	if len(m.Remove) == 0 {
		// A shift removes nothing; its realized change is read when its pods have rebound, approximated here by
		// the next sample after the move (see shiftRealized).
		pr.sources = nil
	}
	c.plans = append(c.plans, pr)
	for _, src := range pr.sources {
		c.sourcePlan[src] = pr
	}
}

func (c *collector) commandCompleted(*disruption.Command) { c.completed++ }

func (c *collector) disruptionPass(wall time.Duration, method string) {
	c.passes = append(c.passes, wall)
	c.passMethods[lo.CoalesceOrEmpty(method, "none")]++
}

// p2 is the plan's P2, the potential world metrics report; normalized by the root snapshot's total price.
var p2 = lo.Must(pipeline.NewUtilEntropy(pipeline.PotentialParams{}))

// worldState is a planning snapshot of the world's nodes and bound pods, for potentials.
func (c *collector) worldState() *pipeline.State {
	var nodes []pipeline.NodeView
	var pods []pipeline.PodView
	for _, n := range c.s.nodes {
		if !n.ready {
			continue
		}
		nodes = append(nodes, pipeline.NodeView{Name: n.name, NodePool: NodePoolName, Capacity: n.alloc, Price: n.price, Destination: !n.draining, Source: true})
		for k := range n.pods {
			p := c.s.pods[k]
			pods = append(pods, pipeline.PodView{Key: pipeline.PodKey{Namespace: k.Namespace, Name: k.Name}, Node: n.name, Requests: p.req, Reschedulable: true})
		}
	}
	return pipeline.NewState(nodes, pods, nil, nil)
}

// worldP2 is Σ price · h(dominant share) in $/hr, unnormalized, so values compare across time.
func (c *collector) worldP2() float64 {
	st := c.worldState()
	return p2.Value(st) * st.Normalizer().TotalPrice
}

func binaryEntropy(u float64) float64 {
	if u <= 0 || u >= 1 {
		return 0
	}
	return -u*math.Log(u) - (1-u)*math.Log(1-u)
}

const timeseriesHeader = "t_min,nodes,ready_nodes,cost_per_hour,pods_running,pods_pending,evictions_cum,eviction_cost_cum,nodes_removed_cum," +
	"cpu_util_mean,mem_util_mean,dom_util_mean,band_share_cpu,band_share_dom,p1_load_entropy,p1_norm,p2_util_entropy,p2_norm,p3_quadratic,p3_norm," +
	"hist_cpu,hist_mem"

// sample appends one timeseries row.
func (c *collector) sample() {
	c.advanceCost()
	var ready, running, pending int
	var cpuSum, memSum, domSum, bandCPU, bandDom, p2raw, p3raw, priceReady float64
	var loads []float64
	histCPU, histMem := make([]int, 10), make([]int, 10)
	for _, n := range c.s.nodes {
		if !n.ready {
			continue
		}
		ready++
		cpu, mem := n.used[pipeline.CPU]/n.alloc[pipeline.CPU], n.used[pipeline.Memory]/n.alloc[pipeline.Memory]
		dom := math.Max(cpu, mem)
		cpuSum, memSum, domSum = cpuSum+cpu, memSum+mem, domSum+dom
		if cpu >= bandLow && cpu <= bandHigh {
			bandCPU++
		}
		if dom >= bandLow && dom <= bandHigh {
			bandDom++
		}
		histCPU[min(9, int(cpu*10))]++
		histMem[min(9, int(mem*10))]++
		p2raw += n.price * binaryEntropy(math.Min(1, dom))
		u := math.Min(1, dom)
		p3raw += n.price * u * (1 - u)
		priceReady += n.price
		loads = append(loads, n.used[pipeline.CPU])
	}
	for _, p := range c.s.pods {
		switch p.phase {
		case podBound:
			running++
		case podPending:
			pending++
		}
	}
	var total, sxl float64
	for _, l := range loads {
		total += l
		if l > 0 {
			sxl += l * math.Log(l)
		}
	}
	p1 := 0.0
	if total > 0 {
		p1 = math.Log(total) - sxl/total
	}
	div := func(a, b float64) float64 {
		if b == 0 {
			return 0
		}
		return a / b
	}
	r := float64(ready)
	hist := func(h []int) string {
		return strings.Join(lo.Map(h, func(v int, _ int) string { return fmt.Sprint(v) }), ";")
	}
	c.rows = append(c.rows, fmt.Sprintf("%.2f,%d,%d,%.6f,%d,%d,%d,%.6f,%d,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%s,%s",
		c.s.now().Sub(c.s.start).Minutes(), len(c.s.nodes), ready, c.totalPrice, running, pending, len(c.evictions), c.evictionCost, c.nodesRemoved,
		div(cpuSum, r), div(memSum, r), div(domSum, r), div(bandCPU, r), div(bandDom, r), p1, div(p1, math.Log(math.Max(2, r))),
		p2raw, div(p2raw, priceReady), p3raw, div(p3raw, priceReady), hist(histCPU), hist(histMem)))
}

// podSetInterval is how often the run records its pod multiset for the offline packing lower bound (B6, Exp 8).
const podSetInterval = 30 * time.Minute

// recordPodSet appends the multiset of running and pending pod shapes, {cpu millicores, memory MiB, count}, sorted,
// and the run's cost at that instant: the input an offline bin-packing solver needs to bound the cheapest fleet.
func (c *collector) recordPodSet() {
	counts := map[Shape]int{}
	for _, p := range c.s.pods {
		if p.phase != podTerminating {
			counts[p.shape]++
		}
	}
	shapes := lo.Keys(counts)
	sort.Slice(shapes, func(i, j int) bool {
		if shapes[i].CPU != shapes[j].CPU {
			return shapes[i].CPU < shapes[j].CPU
		}
		return shapes[i].Memory < shapes[j].Memory
	})
	parts := lo.Map(shapes, func(sh Shape, _ int) string { return fmt.Sprintf("[%d,%d,%d]", sh.CPU, sh.Memory, counts[sh]) })
	c.podsets = append(c.podsets, fmt.Sprintf(`{"t_min":%.2f,"cost_per_hour":%.6f,"nodes":%d,"shapes":[%s]}`,
		c.s.now().Sub(c.s.start).Minutes(), c.totalPrice, len(c.s.nodes), strings.Join(parts, ",")))
}

func (c *collector) writePodSets(w io.Writer) error {
	for _, r := range c.podsets {
		if _, err := fmt.Fprintln(w, r); err != nil {
			return err
		}
	}
	return nil
}

func (c *collector) writeTimeseries(w io.Writer) error {
	if _, err := fmt.Fprintln(w, timeseriesHeader); err != nil {
		return err
	}
	for _, r := range c.rows {
		if _, err := fmt.Fprintln(w, r); err != nil {
			return err
		}
	}
	return nil
}

// Summary is a run's outcome. Fields prefixed Wall are wall-clock measurements and the only nondeterministic ones.
type Summary struct {
	RunID               string         `json:"run_id"`
	Arm                 string         `json:"arm"`
	Scenario            string         `json:"scenario"`
	Nodes               int            `json:"scenario_nodes"`
	Seed                int64          `json:"seed"`
	Placement           string         `json:"placement"`
	ScaleDown           string         `json:"scale_down"`
	SimMinutes          float64        `json:"sim_minutes"`
	IntegratedCost      float64        `json:"integrated_cost"`      // $ over the whole run
	IntegratedCostPost  float64        `json:"integrated_cost_post"` // $ after warmup
	SteadyCostPerHour   float64        `json:"steady_cost_per_hour"` // mean $/hr over the last 20% of the run
	FinalNodes          int            `json:"final_nodes"`
	PeakNodes           int            `json:"peak_nodes"`
	NodesRemoved        int            `json:"nodes_removed"`
	Evictions           int            `json:"evictions"`
	EvictionsBy         map[string]int `json:"evictions_by"`
	EvictionCost        float64        `json:"eviction_cost"`
	ReDisruptions       int            `json:"re_disruptions"`
	RegretEvictions     int            `json:"regret_evictions"`
	ChurnNodes          int            `json:"churn_nodes"`
	RefusedByPDB        int            `json:"evictions_refused_by_pdb"`
	PDBViolations       int            `json:"pdb_violations"`
	DNDViolations       int            `json:"do_not_disrupt_violations"`
	Violations          []string       `json:"violations,omitempty"`
	PendingPodSeconds   float64        `json:"pending_pod_seconds"`
	MeanTimeToSchedule  float64        `json:"mean_time_to_schedule_s"`
	ScaleDowns          int            `json:"scale_downs"`
	JobsCompleted       int            `json:"jobs_completed"`
	Passes              int            `json:"disruption_passes"`
	PassMethods         map[string]int `json:"pass_methods"`
	CommandsStarted     map[string]int `json:"commands_started"`
	CommandsCompleted   int            `json:"commands_completed"`
	CommandsFailed      int            `json:"commands_failed"`
	FidelityHits        int            `json:"fidelity_hits"`
	FidelityMisses      int            `json:"fidelity_misses"`
	Fidelity            float64        `json:"fidelity"`
	PredictedDeltaP2    float64        `json:"predicted_delta_p2"`
	RealizedDeltaP2     float64        `json:"realized_delta_p2"`
	PlansScored         int            `json:"plans_scored"`
	FullSimulations     int            `json:"full_simulations"`
	SearchTimeouts      int            `json:"search_timeouts"`
	EventsRun           int            `json:"events_run"`
	Invalid             bool           `json:"invalid"`
	InvalidReasons      []string       `json:"invalid_reasons,omitempty"`
	TimedOut            bool           `json:"timed_out"`
	Error               string         `json:"error,omitempty"`
	WallSeconds         float64        `json:"wall_seconds"`
	WallPassMeanMs      float64        `json:"wall_pass_mean_ms"`
	WallPassP95Ms       float64        `json:"wall_pass_p95_ms"`
	WallPassMaxMs       float64        `json:"wall_pass_max_ms"`
	WallPassTotalSecond float64        `json:"wall_pass_total_s"`
}

func (c *collector) summary(s *Sim) Summary {
	c.advanceCost()
	sum := Summary{RunID: s.cfg.RunID, Arm: armLabel(s.cfg.Arm), Scenario: s.cfg.Scenario.Name, Nodes: s.cfg.Scenario.Nodes, Seed: s.cfg.Seed,
		Placement: s.cfg.Placement, ScaleDown: s.cfg.ScaleDown, SimMinutes: s.now().Sub(s.start).Minutes(),
		IntegratedCost: c.costIntegral, IntegratedCostPost: c.costPost, FinalNodes: len(s.nodes), NodesRemoved: c.nodesRemoved,
		Evictions: len(c.evictions), EvictionsBy: c.evictionsBy, EvictionCost: c.evictionCost, ReDisruptions: c.reDisruptions,
		ChurnNodes: c.churnNodes, RefusedByPDB: c.refusedByPDB, Violations: c.violations, PendingPodSeconds: c.pendingSec,
		ScaleDowns: c.scaleDowns, JobsCompleted: c.jobsCompleted, Passes: len(c.passes), PassMethods: c.passMethods,
		CommandsStarted: c.started, CommandsCompleted: c.completed, CommandsFailed: len(c.failed),
		FidelityHits: c.fidelityHits, FidelityMisses: c.fidelityMisses, EventsRun: s.eventsRun}
	for _, v := range c.violations {
		if strings.HasPrefix(v, "do-not-disrupt") {
			sum.DNDViolations++
		} else {
			sum.PDBViolations++
		}
	}
	if sum.PDBViolations > 0 || sum.DNDViolations > 0 {
		sum.Invalid = true
		sum.InvalidReasons = append(sum.InvalidReasons, "pdb or do-not-disrupt violation")
	}
	// Pods still pending at the end count their wait so far.
	for _, t := range c.createdAt {
		sum.PendingPodSeconds += s.now().Sub(t).Seconds()
	}
	if c.binds > 0 {
		sum.MeanTimeToSchedule = c.waitTotal.Seconds() / float64(c.binds)
	}
	if n := c.fidelityHits + c.fidelityMisses; n > 0 {
		sum.Fidelity = float64(c.fidelityHits) / float64(n)
	}
	for _, e := range c.evictions {
		if r, ok := c.removedAt[e.node]; !ok || r.Sub(e.at) > regretWindow {
			sum.RegretEvictions++
		}
	}
	for _, pr := range c.plans {
		if pr.done {
			sum.PlansScored++
			sum.PredictedDeltaP2 += pr.predicted
			sum.RealizedDeltaP2 += pr.realized
		}
	}
	// Steady state: mean of the last 20% of samples.
	if n := len(c.rows); n > 0 {
		from := n - max(1, n/5)
		var tot float64
		for _, r := range c.rows[from:] {
			var cost float64
			fmt.Sscanf(strings.Split(r, ",")[3], "%f", &cost) //nolint:errcheck
			tot += cost
		}
		sum.SteadyCostPerHour = tot / float64(n-from)
		for _, r := range c.rows {
			var nodes int
			fmt.Sscanf(strings.Split(r, ",")[2], "%d", &nodes) //nolint:errcheck
			sum.PeakNodes = max(sum.PeakNodes, nodes)
		}
	}
	if len(c.passes) > 0 {
		ds := append([]time.Duration(nil), c.passes...)
		sort.Slice(ds, func(i, j int) bool { return ds[i] < ds[j] })
		var tot time.Duration
		for _, d := range ds {
			tot += d
		}
		sum.WallPassMeanMs = float64(tot.Milliseconds()) / float64(len(ds))
		sum.WallPassP95Ms = float64(ds[int(0.95*float64(len(ds)-1))].Microseconds()) / 1000
		sum.WallPassMaxMs = float64(ds[len(ds)-1].Microseconds()) / 1000
		sum.WallPassTotalSecond = tot.Seconds()
	}
	return sum
}

func armLabel(a ArmConfig) string {
	if a.Name == ArmPipeline && a.Pipeline != nil {
		p := a.Pipeline
		return fmt.Sprintf("pipeline[%s|%s|%s|%s|%s]", p.Candidates.Name, strings.Join(lo.Map(p.Moves, func(m pipeline.StageConfig, _ int) string { return m.Name }), "+"),
			p.Planner.Name, p.Executor.Name, p.Potential.Name)
	}
	return a.Name
}
