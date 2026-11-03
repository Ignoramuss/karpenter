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
	"sort"
	"time"

	"github.com/samber/lo"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/cloudprovider"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/provisioning"
	"sigs.k8s.io/karpenter/pkg/controllers/state"
	"sigs.k8s.io/karpenter/pkg/events"
	"sigs.k8s.io/karpenter/pkg/state/cost"
)

// ConsolidationType is the consolidation_type metric value for commands the pipeline produces.
var ConsolidationType = disruption.PipelineConsolidationType

// Method is the PipelineConsolidation disruption method. It replaces multi-node and single-node consolidation when
// the ConsolidationPipeline feature gate is on; Emptiness and Drift are unchanged.
type Method struct {
	cfg        Config
	env        *Env
	selector   CandidateSelector
	generators []MoveGenerator
	planner    Planner
	executor   Executor
	rng        *rand.Rand
	recorder   Recorder

	runID     string
	start     time.Time
	cycle     int
	moveSeq   int
	lastState time.Time
	// evictions is the trailing minute of evictions, for the pod-eviction budgets.
	evictions []eviction
	// fullSims and timeouts accumulate search effort across cycles.
	fullSims, timeouts int
	// searchFeasibility is the Feasibility search budgets hand to generators; execFull wraps it for everything else.
	searchFeasibility Feasibility
	execFull          *countingFeasibility
}

type eviction struct {
	at    time.Time
	pool  string
	owner string
}

var _ disruption.Method = (*Method)(nil)
var _ disruption.NodePoolTotalsSetter = (*Method)(nil)

// MethodOptions customize NewMethod.
type MethodOptions struct {
	Upstream UpstreamOptions
	// Recorder receives the decision log; nil discards it.
	Recorder Recorder
	// RunID labels decision log lines.
	RunID string
	// Feasibility overrides the live Feasibility, for tests.
	Feasibility Feasibility
	// Observer, when set, sees every move an executor carries out, before it runs.
	Observer func(Move)
	// Steer sets Env.Steer.
	Steer func(pod PodKey, node string)
}

// NewMethod builds the pipeline from cfg over live Karpenter components.
func NewMethod(clk clock.Clock, kubeClient client.Client, cluster *state.Cluster, provisioner *provisioning.Provisioner,
	cp cloudprovider.CloudProvider, recorder events.Recorder, queue *disruption.Queue, clusterCost *cost.ClusterCost, cfg Config, opts MethodOptions) (*Method, error) {
	up := NewUpstream(clk, kubeClient, cluster, provisioner, cp, recorder, queue, opts.Upstream)
	up.ClusterCost = clusterCost
	feas := opts.Feasibility
	if feas == nil {
		feas = &UpstreamFeasibility{FastChecker: &FastChecker{}, Up: up}
	}
	return newMethod(&Env{Clock: clk, Upstream: up, Feasibility: feas}, cfg, opts)
}

func newMethod(env *Env, cfg Config, opts MethodOptions) (*Method, error) {
	cfg.defaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	pot, err := NewPotential(cfg.Potential)
	if err != nil {
		return nil, err
	}
	env.Potential = pot
	env.Scorer = DefaultScorer{Potential: pot}
	// Generators reach Full through the cycle's search budget, which counts and caps it; everything else that holds
	// the env (executors confirming merges no generator confirmed) goes through a counter, so SearchTotals reports
	// every simulation a cycle spends.
	execFull := &countingFeasibility{Feasibility: env.Feasibility}
	searchFeasibility := env.Feasibility
	if env.Feasibility != nil {
		env.Feasibility = execFull
	}
	env.Recorder = lo.Ternary[Recorder](opts.Recorder != nil, opts.Recorder, NopRecorder{})
	env.Observer = opts.Observer
	env.Steer = opts.Steer
	m := &Method{cfg: cfg, env: env, rng: rand.New(rand.NewSource(cfg.Seed)), recorder: env.Recorder, runID: opts.RunID, start: env.Clock.Now(), //nolint:gosec
		searchFeasibility: searchFeasibility, execFull: execFull}
	if m.selector, err = NewCandidateSelector(env, cfg.Candidates); err != nil {
		return nil, err
	}
	for _, g := range cfg.Moves {
		gen, err := NewMoveGenerator(env, g)
		if err != nil {
			return nil, err
		}
		m.generators = append(m.generators, gen)
	}
	if m.planner, err = NewPlanner(env, cfg.Planner); err != nil {
		return nil, err
	}
	if m.executor, err = NewExecutor(env, cfg.Executor); err != nil {
		return nil, err
	}
	return m, nil
}

// SearchTotals returns the full scheduling simulations spent, in search and in execution, and the search timeouts
// hit, over every cycle so far.
func (m *Method) SearchTotals() (fullSimulations, timeouts int) {
	return m.fullSims + m.execFull.calls, m.timeouts
}

// countingFeasibility counts Full calls.
type countingFeasibility struct {
	Feasibility
	calls int
}

func (c *countingFeasibility) Full(ctx context.Context, s Snapshot, m Move) (Move, error) {
	c.calls++
	return c.Feasibility.Full(ctx, s, m)
}

// Env returns the strategies' environment, for harnesses.
func (m *Method) Env() *Env { return m.env }

// Config returns the pipeline config.
func (m *Method) Config() Config { return m.cfg }

func (m *Method) ShouldDisrupt(ctx context.Context, c *disruption.Candidate) bool {
	return m.env.Upstream.Single.ShouldDisrupt(ctx, c)
}

func (m *Method) Reason() v1.DisruptionReason { return v1.DisruptionReasonUnderutilized }
func (m *Method) Class() string               { return disruption.GracefulDisruptionClass }
func (m *Method) ConsolidationType() string   { return ConsolidationType.Name }

// SetNodePoolTotals receives the cycle's Balanced totals and passes them to upstream's methods, as the controller
// would have.
func (m *Method) SetNodePoolTotals(totals map[string]disruption.NodePoolTotals) {
	m.env.Upstream.cycle.totals = totals
	m.env.Upstream.Multi.SetNodePoolTotals(totals)
	m.env.Upstream.Single.SetNodePoolTotals(totals)
}

func (m *Method) selfGated() bool {
	for _, g := range m.generators {
		if sg, ok := g.(SelfGating); ok && sg.SelfGating() {
			return true
		}
	}
	return false
}

func (m *Method) record(e DecisionEvent) {
	e.SchemaVersion = DecisionLogSchemaVersion
	e.RunID = m.runID
	e.Cycle = m.cycle
	e.T = m.env.Clock.Since(m.start).Seconds()
	m.recorder.Record(e)
}

// ComputeCommands runs one pipeline cycle. Merges come back as commands for the orchestration queue; executors that
// run other moves (shifts) carry them out themselves.
//
//nolint:gocyclo
func (m *Method) ComputeCommands(ctx context.Context, budgets map[string]int, candidates ...*disruption.Candidate) ([]disruption.Command, error) {
	m.cycle++
	up := m.env.Upstream
	totals := up.cycle.totals
	up.SetCycle(candidates, totals, budgets)
	gated := !m.selfGated()
	if gated && !m.lastState.IsZero() && m.lastState.Equal(up.Cluster.ConsolidationState()) {
		return nil, nil
	}
	snap, err := BuildSnapshot(ctx, up)
	if err != nil {
		return nil, err
	}
	sources, dests, err := m.selector.Select(ctx, snap, m.rng)
	if err != nil {
		return nil, fmt.Errorf("selecting candidates, %w", err)
	}
	m.record(DecisionEvent{Stage: StageCandidates, Strategy: m.selector.Name(), Pods: len(candidates),
		Detail: map[string]float64{"sources": float64(len(sources)), "destinations": float64(len(dests)), "candidates": float64(len(candidates))}})

	rounds := [][]MoveGenerator{m.generators}
	if m.cfg.MovesMode == Sequential {
		rounds = lo.Map(m.generators, func(g MoveGenerator, _ int) []MoveGenerator { return []MoveGenerator{g} })
	}
	constrained := false
	snapAt := m.env.Clock.Now()
	for i, round := range rounds {
		// The controller recomputes candidates and budgets for each method it tries. A round after one that waited
		// out a validation period does the same, so it sees the cluster as it is now.
		if i > 0 && !m.env.Clock.Now().Equal(snapAt) {
			if err := m.refresh(ctx); err != nil {
				return nil, err
			}
			if snap, err = BuildSnapshot(ctx, up); err != nil {
				return nil, err
			}
			if sources, dests, err = m.selector.Select(ctx, snap, m.rng); err != nil {
				return nil, fmt.Errorf("selecting candidates, %w", err)
			}
			snapAt = m.env.Clock.Now()
		}
		cmds, executed, roundConstrained, err := m.round(ctx, snap, sources, dests, round)
		if err != nil {
			return nil, err
		}
		constrained = constrained || roundConstrained
		if executed {
			return cmds, nil
		}
	}
	if gated && !constrained {
		m.lastState = up.Cluster.ConsolidationState()
	}
	return nil, nil
}

// refresh recomputes the cycle's candidates, totals and budgets the way the disruption controller does before each
// method.
func (m *Method) refresh(ctx context.Context) error {
	up := m.env.Upstream
	cands, totals, err := disruption.GetCandidatesWithTotals(ctx, up.Cluster, up.KubeClient, up.Events, up.Clock, up.CloudProvider,
		m.ShouldDisrupt, m.Class(), up.Queue, up.ClusterCost)
	if err != nil {
		return fmt.Errorf("determining candidates, %w", err)
	}
	if up.OrderCandidates != nil {
		cands = up.OrderCandidates(cands)
	}
	budgets, err := disruption.BuildDisruptionBudgetMapping(ctx, up.Cluster, up.Clock, up.KubeClient, up.CloudProvider, up.Events, m.Reason())
	if err != nil {
		return fmt.Errorf("building disruption budgets, %w", err)
	}
	m.SetNodePoolTotals(totals)
	up.SetCycle(cands, totals, budgets)
	return nil
}

func (m *Method) searchBudget() *SearchBudget {
	var deadline time.Time
	if d, _ := m.cfg.Search.timeout(); d > 0 {
		deadline = m.env.Clock.Now().Add(d)
	}
	b := NewSearchBudget(m.cfg.Search.MaxAttempts, m.cfg.Search.MaxFullSimulations, deadline, m.env.Clock)
	if g, ok := m.planner.(Guide); ok {
		b.Guide = g
	}
	if fv, ok := m.planner.(FirstValid); ok {
		b.StopAtFirst = fv.FirstValid()
	}
	b.Feasibility = m.searchFeasibility
	b.Scorer = m.env.Scorer
	return b
}

func (m *Method) cycleBudgets(s Snapshot) Budgets {
	b := Budgets{Nodes: m.env.Upstream.Budgets()}
	now := m.env.Clock.Now()
	m.evictions = lo.Filter(m.evictions, func(e eviction, _ int) bool { return now.Sub(e.at) < time.Minute })
	if n := m.cfg.Budgets.PodEvictionsPerMinute; n > 0 {
		b.PodEvictions = map[string]int{}
		for _, node := range s.Nodes() {
			b.PodEvictions[node.NodePool] = n
		}
		for _, e := range m.evictions {
			b.PodEvictions[e.pool]--
		}
	}
	if n := m.cfg.Budgets.WorkloadEvictionsPerMinute; n > 0 {
		b.WorkloadEvictions = map[string]int{}
		for _, node := range s.Nodes() {
			for _, p := range s.Pods(node.Name) {
				if p.Owner != "" {
					b.WorkloadEvictions[p.Owner] = n
				}
			}
		}
		for _, e := range m.evictions {
			if _, ok := b.WorkloadEvictions[e.owner]; ok {
				b.WorkloadEvictions[e.owner]--
			}
		}
	}
	return b
}

//nolint:gocyclo
func (m *Method) round(ctx context.Context, snap Snapshot, sources, dests []NodeView, gens []MoveGenerator) ([]disruption.Command, bool, bool, error) {
	budget := m.searchBudget()
	var moves []Move
	for _, g := range gens {
		gm, err := g.Generate(ctx, snap, sources, dests, m.rng, budget.Fork())
		if err != nil {
			return nil, false, false, fmt.Errorf("generating moves with %s, %w", g.Name(), err)
		}
		moves = append(moves, gm...)
		if budget.StopAtFirst && len(gm) > 0 {
			break
		}
	}
	scored := make([]Scored, len(moves))
	for i, mv := range moves {
		m.moveSeq++
		if mv.ID == "" {
			mv.ID = fmt.Sprintf("m%d", m.moveSeq)
		}
		scored[i] = m.env.Scorer.Score(snap, mv)
		m.record(moveEvent(StageMoves, mv.Generator, scored[i], true, ""))
	}
	attempts, full, timedOut, exhausted := budget.Stats()
	m.fullSims += full
	if timedOut {
		m.timeouts++
	}
	m.record(DecisionEvent{Stage: StageMoves, Strategy: generatorNames(gens), Detail: map[string]float64{
		"moves": float64(len(moves)), "attempts": float64(attempts), "full_simulations": float64(full),
		"timed_out": b2f(timedOut), "exhausted": b2f(exhausted)}})
	if len(scored) == 0 {
		m.idle(ctx)
		return nil, false, false, nil
	}
	budgets := m.cycleBudgets(snap)
	plan, err := m.planner.Plan(ctx, snap, scored, budgets.Clone(), m.rng)
	if err != nil {
		return nil, false, false, fmt.Errorf("planning with %s, %w", m.planner.Name(), err)
	}
	plan.Snapshot = snap
	accepted := lo.SliceToMap(plan.Moves, func(s Scored) (string, bool) { return s.ID, true })
	for _, sc := range scored {
		if accepted[sc.ID] {
			m.record(moveEvent(StagePlan, m.planner.Name(), sc, true, ""))
		} else {
			m.record(moveEvent(StagePlan, m.planner.Name(), sc, false, lo.CoalesceOrEmpty(plan.Rejected[sc.ID], "not selected")))
		}
	}
	if len(plan.Moves) == 0 {
		m.idle(ctx)
		return nil, false, plan.ConstrainedByBudgets, nil
	}
	report, err := m.executor.Execute(ctx, plan)
	if err != nil {
		return nil, false, plan.ConstrainedByBudgets, fmt.Errorf("executing with %s, %w", m.executor.Name(), err)
	}
	executed := lo.SliceToMap(report.Executed, func(id string) (string, bool) { return id, true })
	for _, sc := range plan.Moves {
		m.record(moveEvent(StageExecute, m.executor.Name(), sc, executed[sc.ID], report.Reasons[sc.ID]))
		if executed[sc.ID] {
			mv := sc.Move
			if done, ok := report.Evicted[sc.ID]; ok {
				mv.Bindings = done
			}
			m.noteEvictions(snap, mv)
		}
	}
	cmds := make([]disruption.Command, 0, len(report.Commands))
	for _, c := range report.Commands {
		if cmd, ok := c.(disruption.Command); ok {
			cmds = append(cmds, cmd)
		}
	}
	if len(report.Executed) > 0 {
		log.FromContext(ctx).V(1).Info("consolidation pipeline executed moves", "moves", report.Executed, "planner", m.planner.Name())
	}
	return cmds, len(report.Executed) > 0, plan.ConstrainedByBudgets, nil
}

func (m *Method) noteEvictions(s Snapshot, mv Move) {
	now := m.env.Clock.Now()
	podsOf := map[string]map[PodKey]PodView{}
	for _, b := range mv.Bindings {
		if _, ok := podsOf[b.From]; !ok {
			podsOf[b.From] = lo.SliceToMap(s.Pods(b.From), func(p PodView) (PodKey, PodView) { return p.Key, p })
		}
		n, _ := s.Node(b.From)
		m.evictions = append(m.evictions, eviction{at: now, pool: n.NodePool, owner: podsOf[b.From][b.Pod].Owner})
	}
}

// idle gives the executor its housekeeping on a cycle that executes nothing.
func (m *Method) idle(ctx context.Context) {
	if idle, ok := m.executor.(IdleExecutor); ok {
		idle.Idle(ctx)
	}
}

func generatorNames(gens []MoveGenerator) string {
	ns := lo.Map(gens, func(g MoveGenerator, _ int) string { return g.Name() })
	sort.Strings(ns)
	return lo.Reduce(ns, func(acc, n string, i int) string { return lo.Ternary(i == 0, n, acc+"+"+n) }, "")
}

func moveEvent(stage, strategy string, sc Scored, accepted bool, reason string) DecisionEvent {
	return DecisionEvent{
		Stage: stage, Strategy: strategy, MoveID: sc.ID, Kind: string(sc.Kind), Sources: sc.Sources,
		Destinations: sc.Destinations(), Pods: len(sc.Bindings), DeltaPotential: sc.DeltaPotential, Disruption: sc.Disruption,
		SavingsPerHour: sc.SavingsPerHour, Accepted: accepted, Reason: reason,
		Detail: map[string]float64{"launch": float64(len(sc.Launch)), "remove": float64(len(sc.Remove)), "energy": sc.Energy,
			"norm_disruption": sc.NormalizedDisruption, "norm_savings": sc.NormalizedSavings},
	}
}

func b2f(b bool) float64 { return lo.Ternary(b, 1.0, 0.0) }
