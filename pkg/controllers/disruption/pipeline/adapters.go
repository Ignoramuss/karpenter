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
	"errors"
	"fmt"
	"math/rand"
	"sort"

	"github.com/samber/lo"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/log"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
)

// The upstream adapters map today's consolidation onto the four stages.
//
//	stage 1  upstream         GetCandidatesWithTotals + ShouldDisrupt (done by the controller), sortCandidates
//	stage 2  upstream-multi   MultiNodeConsolidation: budget pre-filter, prefix binary search over computeConsolidation
//	         upstream-single  SingleNodeConsolidation: nodepool-interleaved order, computeConsolidation per candidate
//	stage 3  upstream         Budgets, Balanced scoring for Balanced NodePools, first valid command
//	         balanced         The same, with Balanced scoring (k = 2) applied to every NodePool
//	stage 4  upstream-queue   Validation after the wait, then the orchestration queue
//
// The move adapters have two modes. "coarse" calls the upstream method's ComputeCommands and converts what it
// returns, so it is upstream by construction. "fine" rebuilds the same decision from upstream's building blocks with
// generation, acceptance and validation split across stages 2, 3 and 4; generation consults the planner (as a Guide)
// exactly where upstream consults its evaluator. Parity tests hold both to upstream's decisions.

const (
	modeCoarse = "coarse"
	modeFine   = "fine"
)

// SelfGating is implemented by generators that track, as upstream methods do, whether the cluster changed since they
// last found nothing, and skip work when it has not. The pipeline method only gates cycles itself when no generator
// does.
type SelfGating interface{ SelfGating() bool }

// FirstValid is implemented by planners that take the first acceptable move in generation order, as upstream does;
// generators may stop searching once they produce one.
type FirstValid interface{ FirstValid() bool }

func needUpstream(env *Env, name string) error {
	if env.Upstream == nil {
		return fmt.Errorf("%s needs a live cluster (pipeline.Env.Upstream)", name)
	}
	return nil
}

func names(views []NodeView) []string {
	return lo.Map(views, func(n NodeView, _ int) string { return n.Name })
}

// --- stage 1 ---------------------------------------------------------------------------------------------------

type upstreamSelector struct{ env *Env }

func (u *upstreamSelector) Name() string { return "upstream" }

// Select returns sources in upstream's candidate order and every destination. Without a live cluster it orders
// sources by the same savings ratio, ties by name.
func (u *upstreamSelector) Select(ctx context.Context, s Snapshot, _ *rand.Rand) ([]NodeView, []NodeView, error) {
	var sources, dests []NodeView
	for _, n := range s.Nodes() {
		if n.Destination {
			dests = append(dests, n)
		}
	}
	if up := u.env.Upstream; up != nil {
		ordered := up.Multi.SortBySavingsRatio(ctx, append([]*disruption.Candidate(nil), up.cycle.candidates...))
		for _, c := range ordered {
			if n, ok := s.Node(c.Name()); ok && n.Source {
				sources = append(sources, n)
			}
		}
		return sources, dests, nil
	}
	for _, n := range s.Nodes() {
		if n.Source {
			sources = append(sources, n)
		}
	}
	sort.SliceStable(sources, func(i, j int) bool {
		return disruptionutilsSavingsRatio(sources[i]) > disruptionutilsSavingsRatio(sources[j])
	})
	return sources, dests, nil
}

func disruptionutilsSavingsRatio(n NodeView) float64 {
	if n.DisruptionCost <= 0 {
		return n.Price
	}
	return n.Price / n.DisruptionCost
}

// --- stage 2 ---------------------------------------------------------------------------------------------------

type modeParams struct {
	Mode string `json:"mode"`
}

func decodeMode(p Params) (string, error) {
	var mp modeParams
	if err := p.Decode(&mp); err != nil {
		return "", err
	}
	switch mp.Mode {
	case "", modeCoarse:
		return modeCoarse, nil
	case modeFine:
		return modeFine, nil
	}
	return "", fmt.Errorf("mode %q, want %s or %s", mp.Mode, modeCoarse, modeFine)
}

type upstreamMulti struct {
	env  *Env
	mode string
	seq  int
}

func (g *upstreamMulti) Name() string     { return "upstream-multi" }
func (g *upstreamMulti) SelfGating() bool { return true }

func (g *upstreamMulti) nextID() string {
	g.seq++
	return fmt.Sprintf("multi-%d", g.seq)
}

func (g *upstreamMulti) Generate(ctx context.Context, s Snapshot, sources, _ []NodeView, _ *rand.Rand, b *SearchBudget) ([]Move, error) {
	up := g.env.Upstream
	cands := up.candidatesFor(names(sources))
	if g.mode == modeCoarse {
		cmds, err := up.Multi.ComputeCommands(ctx, up.Budgets(), cands...)
		if err != nil {
			return nil, err
		}
		return lo.Map(cmds, func(c disruption.Command, _ int) Move { return MoveFromCommand(s, c, g.nextID(), g.Name(), true) }), nil
	}
	if up.Multi.IsConsolidated() {
		return nil, nil
	}
	cands = up.Multi.SortBySavingsRatio(ctx, cands)
	budgets := up.Budgets()
	disruptable := make([]*disruption.Candidate, 0, len(cands))
	constrained := false
	for _, c := range cands {
		if budgets[c.NodePool.Name] == 0 {
			constrained = true
			continue
		}
		disruptable = append(disruptable, c)
		budgets[c.NodePool.Name]--
	}
	cmd, err := g.firstN(ctx, s, b, disruptable, lo.Clamp(len(disruptable), 0, 100))
	if err != nil {
		return nil, err
	}
	if cmd.Decision() == disruption.NoOpDecision {
		if !constrained {
			up.Multi.MarkConsolidated()
		}
		return nil, nil
	}
	return []Move{MoveFromCommand(s, cmd, g.nextID(), g.Name(), false)}, nil
}

// firstN is MultiNodeConsolidation.firstNConsolidationOption with the evaluator replaced by the planner's guide.
// Its one-minute limit runs on the injected clock rather than a wall-clock context deadline, per the pipeline's
// no-wall-clock rule; under a real clock the two agree.
func (g *upstreamMulti) firstN(ctx context.Context, s Snapshot, b *SearchBudget, cands []*disruption.Candidate, maxN int) (disruption.Command, error) {
	up := g.env.Upstream
	if len(cands) < 2 {
		return disruption.Command{}, nil
	}
	lo1, hi := 1, maxN
	if len(cands) <= hi {
		hi = len(cands) - 1
	}
	deadline := up.Clock.Now().Add(disruption.MultiNodeConsolidationTimeoutDuration)
	var last disruption.Command
	for lo1 <= hi {
		if up.Clock.Now().After(deadline) {
			disruption.ConsolidationTimeoutsTotal.Inc(map[string]string{disruption.ConsolidationTypeLabel: up.Multi.ConsolidationType()})
			return last, nil
		}
		if b != nil && !b.UseFull() {
			return last, nil
		}
		mid := (lo1 + hi) / 2
		batch := cands[0 : mid+1]
		cmd, err := up.Multi.ComputeConsolidation(ctx, batch...)
		if err != nil {
			return disruption.Command{}, err
		}
		valid := cmd.Decision() == disruption.DeleteDecision
		if cmd.Decision() == disruption.ReplaceDecision {
			r, ferr := disruption.FilterOutSameInstanceType(cmd.Replacements[0], batch)
			if ferr == nil && r != nil && len(r.InstanceTypeOptions) > 0 {
				cmd.Replacements[0] = r
				valid = true
			}
		}
		if valid && b != nil && b.Guide != nil {
			m := MoveFromCommand(s, cmd, "probe", g.Name(), false)
			valid = b.Guide.Accept(ctx, s, b.score(s, m))
		}
		if valid {
			last = cmd
			lo1 = mid + 1
		} else {
			hi = mid - 1
		}
	}
	return last, nil
}

func (b *SearchBudget) score(s Snapshot, m Move) Scored {
	if b.Scorer == nil {
		return Scored{Move: m}
	}
	return b.Scorer.Score(s, m)
}

type upstreamSingle struct {
	env  *Env
	mode string
	seq  int
}

func (g *upstreamSingle) Name() string     { return "upstream-single" }
func (g *upstreamSingle) SelfGating() bool { return true }

func (g *upstreamSingle) nextID() string {
	g.seq++
	return fmt.Sprintf("single-%d", g.seq)
}

//nolint:gocyclo
func (g *upstreamSingle) Generate(ctx context.Context, s Snapshot, sources, _ []NodeView, _ *rand.Rand, b *SearchBudget) ([]Move, error) {
	up := g.env.Upstream
	cands := up.candidatesFor(names(sources))
	if g.mode == modeCoarse {
		cmds, err := up.Single.ComputeCommands(ctx, up.Budgets(), cands...)
		if err != nil {
			return nil, err
		}
		return lo.Map(cmds, func(c disruption.Command, _ int) Move { return MoveFromCommand(s, c, g.nextID(), g.Name(), true) }), nil
	}
	if up.Single.IsConsolidated() {
		return nil, nil
	}
	cands = up.Single.SortCandidates(ctx, cands)
	timeout := up.Clock.Now().Add(disruption.SingleNodeConsolidationTimeoutDuration)
	budgets := up.Budgets()
	constrained := false
	unseen := sets.New(lo.Map(cands, func(c *disruption.Candidate, _ int) string { return c.NodePool.Name })...)
	var out []Move
	for i, c := range cands {
		if up.Clock.Now().After(timeout) {
			disruption.ConsolidationTimeoutsTotal.Inc(map[string]string{disruption.ConsolidationTypeLabel: up.Single.ConsolidationType()})
			log.FromContext(ctx).V(1).Info("abandoning single-node consolidation due to timeout", "candidates_evaluated", i)
			up.Single.PreviouslyUnseenNodePools = unseen
			return out, nil
		}
		unseen.Delete(c.NodePool.Name)
		if budgets[c.NodePool.Name] == 0 {
			constrained = true
			continue
		}
		if b != nil && b.Guide != nil && !b.Guide.CanPass(ctx, s, []string{c.Name()}) {
			continue
		}
		if b != nil && !b.UseFull() {
			break
		}
		cmd, err := up.Single.ComputeConsolidation(ctx, c)
		if err != nil {
			log.FromContext(ctx).Error(err, "failed computing consolidation")
			continue
		}
		if cmd.Decision() == disruption.NoOpDecision {
			continue
		}
		m := MoveFromCommand(s, cmd, g.nextID(), g.Name(), false)
		if b != nil && b.Guide != nil && !b.Guide.Accept(ctx, s, b.score(s, m)) {
			continue
		}
		out = append(out, m)
		if b != nil && b.StopAtFirst {
			return out, nil
		}
	}
	if !constrained && len(out) == 0 {
		up.Single.MarkConsolidated()
	}
	up.Single.PreviouslyUnseenNodePools = unseen
	return out, nil
}

// --- stage 3 ---------------------------------------------------------------------------------------------------

// upstreamPlanner takes the first acceptable move within budgets. "upstream" applies Balanced scoring to Balanced
// NodePools only, as the evaluator does; "balanced" applies it to every NodePool.
type upstreamPlanner struct {
	env           *Env
	name          string
	forceBalanced bool
}

func (p *upstreamPlanner) Name() string     { return p.name }
func (p *upstreamPlanner) FirstValid() bool { return true }

func (p *upstreamPlanner) balancedFor(pool string, s Snapshot) bool {
	if p.forceBalanced {
		return true
	}
	if up := p.env.Upstream; up != nil {
		for _, c := range up.cycle.candidates {
			if c.NodePool.Name == pool {
				return c.NodePool.Spec.Disruption.ConsolidationPolicy.IsBalanced()
			}
		}
	}
	return false
}

// Accept is the evaluator's ApproveCommand decision, without its metrics and events.
func (p *upstreamPlanner) Accept(ctx context.Context, s Snapshot, sc Scored) bool {
	if sc.Kind == Shift {
		return false
	}
	if uc, ok := CommandOf(sc.Move); ok && p.env.Upstream != nil {
		if !p.forceBalanced {
			approved, _ := disruption.EvaluateBalancedMove(ctx, uc.Command, p.env.Upstream.NodePoolTotals())
			return approved
		}
		return evaluateAllPoolsBalanced(uc.Command, p.env.Upstream.NodePoolTotals())
	}
	if len(sc.Launch) > 0 && sc.SavingsPerHour <= 0 {
		return false
	}
	return p.scoreFromSnapshot(s, sc)
}

// scoreFromSnapshot applies EvaluateBalancedMove's per-pool attribution to a move without an upstream command.
func (p *upstreamPlanner) scoreFromSnapshot(s Snapshot, sc Scored) bool {
	byPool := map[string][]NodeView{}
	var total float64
	for _, src := range sc.Remove {
		if n, ok := s.Node(src); ok {
			byPool[n.NodePool] = append(byPool[n.NodePool], n)
			total += n.Price
		}
	}
	if len(byPool) == 0 {
		return false
	}
	for pool, nodes := range byPool {
		if !p.balancedFor(pool, s) {
			continue
		}
		var poolCost, dis float64
		for _, n := range nodes {
			poolCost += n.Price
			dis += n.DisruptionCost
		}
		savings := sc.SavingsPerHour
		if total > 0 && len(byPool) > 1 {
			savings *= poolCost / total
		}
		t := s.Totals(pool)
		if !disruption.ScoreMove(savings, dis, disruption.NodePoolTotals{TotalCost: t.TotalCost, TotalDisruptionCost: t.TotalDisruptionCost}, v1.BalancedK).Approved() {
			return false
		}
	}
	return true
}

func evaluateAllPoolsBalanced(cmd disruption.Command, totals map[string]disruption.NodePoolTotals) bool {
	if len(cmd.Candidates) == 0 {
		return false
	}
	byPool := lo.GroupBy(cmd.Candidates, func(c *disruption.Candidate) string { return c.NodePool.Name })
	savings, total := cmd.EstimatedSavings(), cmd.SourceCost()
	for pool, cs := range byPool {
		poolSavings := savings
		if poolCost := lo.SumBy(cs, func(c *disruption.Candidate) float64 { return c.Price }); total > 0 && len(byPool) > 1 {
			poolSavings = savings * (poolCost / total)
		}
		if !disruption.ScoreMove(poolSavings, cmd.PoolDisruptionCost(pool), totals[pool], v1.BalancedK).Approved() {
			return false
		}
	}
	return true
}

// CanPass is the evaluator's CanPassThreshold: a delete is the best case, so a candidate whose delete fails cannot
// pass.
func (p *upstreamPlanner) CanPass(_ context.Context, s Snapshot, sources []string) bool {
	if len(sources) != 1 {
		return true
	}
	n, ok := s.Node(sources[0])
	if !ok || !p.balancedFor(n.NodePool, s) {
		return true
	}
	t := s.Totals(n.NodePool)
	if t.TotalCost <= 0 {
		return true
	}
	if up := p.env.Upstream; up != nil {
		if c, ok := up.Candidate(n.Name); ok {
			return disruption.ScoreMove(c.Price, c.RescheduleDisruptionCost, disruption.NodePoolTotals{TotalCost: t.TotalCost, TotalDisruptionCost: t.TotalDisruptionCost}, v1.BalancedK).Approved()
		}
	}
	return disruption.ScoreMove(n.Price, n.DisruptionCost, disruption.NodePoolTotals{TotalCost: t.TotalCost, TotalDisruptionCost: t.TotalDisruptionCost}, v1.BalancedK).Approved()
}

func (p *upstreamPlanner) Plan(ctx context.Context, s Snapshot, moves []Scored, budgets Budgets, _ *rand.Rand) (Plan, error) {
	plan := Plan{Planner: p.Name(), Rejected: map[string]string{}}
	for _, m := range moves {
		if !p.Accept(ctx, s, m) {
			plan.Rejected[m.ID] = "not accepted"
			continue
		}
		if reason := ExceedsBudgets(s, m, budgets); reason != "" {
			plan.Rejected[m.ID] = reason
			plan.ConstrainedByBudgets = true
			continue
		}
		plan.Moves = []Scored{m}
		return plan, nil
	}
	return plan, nil
}

// ExceedsBudgets checks a move against the cycle's remaining budgets and returns why it does not fit, or "".
func ExceedsBudgets(s Snapshot, m Scored, b Budgets) string {
	removals := map[string]int{}
	for _, r := range m.Remove {
		if n, ok := s.Node(r); ok {
			removals[n.NodePool]++
		}
	}
	for pool, k := range removals {
		if k > b.Nodes[pool] {
			return fmt.Sprintf("node budget for %s: %d > %d", pool, k, b.Nodes[pool])
		}
	}
	if b.PodEvictions != nil || b.WorkloadEvictions != nil {
		pools, owners := evictionCounts(s, m.Move)
		for pool, k := range pools {
			if left, ok := b.PodEvictions[pool]; ok && k > left {
				return fmt.Sprintf("pod-eviction budget for %s: %d > %d", pool, k, left)
			}
		}
		for owner, k := range owners {
			if left, ok := b.WorkloadEvictions[owner]; ok && k > left {
				return fmt.Sprintf("workload-eviction budget for %s: %d > %d", owner, k, left)
			}
		}
	}
	return ""
}

// SpendBudgets subtracts a move from budgets in place.
func SpendBudgets(s Snapshot, m Move, b Budgets) {
	for _, r := range m.Remove {
		if n, ok := s.Node(r); ok && b.Nodes != nil {
			b.Nodes[n.NodePool]--
		}
	}
	pools, owners := evictionCounts(s, m)
	for pool, k := range pools {
		if _, ok := b.PodEvictions[pool]; ok {
			b.PodEvictions[pool] -= k
		}
	}
	for owner, k := range owners {
		if _, ok := b.WorkloadEvictions[owner]; ok {
			b.WorkloadEvictions[owner] -= k
		}
	}
}

// evictionCounts counts a move's evictions per source NodePool and per workload.
func evictionCounts(s Snapshot, m Move) (map[string]int, map[string]int) {
	pools, owners := map[string]int{}, map[string]int{}
	podsOf := map[string]map[PodKey]PodView{}
	for _, b := range m.Bindings {
		if _, ok := podsOf[b.From]; !ok {
			podsOf[b.From] = lo.SliceToMap(s.Pods(b.From), func(p PodView) (PodKey, PodView) { return p.Key, p })
		}
		n, _ := s.Node(b.From)
		pools[n.NodePool]++
		if p, ok := podsOf[b.From][b.Pod]; ok && p.Owner != "" {
			owners[p.Owner]++
		}
	}
	return pools, owners
}

// --- stage 4 ---------------------------------------------------------------------------------------------------

type upstreamQueue struct{ env *Env }

func (e *upstreamQueue) Name() string { return "upstream-queue" }

// Execute validates each merge after the validation wait (the first one waits; the rest were built from the same
// state) and hands the survivors back as commands for the orchestration queue.
func (e *upstreamQueue) Execute(ctx context.Context, p Plan) (Report, error) {
	up := e.env.Upstream
	r := Report{Reasons: map[string]string{}}
	waited := false
	for _, m := range p.Moves {
		if m.Kind == Shift {
			r.Aborted = append(r.Aborted, m.ID)
			r.Reasons[m.ID] = "upstream-queue runs merges only"
			continue
		}
		uc, ok := CommandOf(m.Move)
		if !ok {
			confirmed, err := e.env.Feasibility.Full(ctx, p.Snapshot, m.Move)
			if err != nil {
				r.Aborted = append(r.Aborted, m.ID)
				r.Reasons[m.ID] = "full feasibility: " + err.Error()
				continue
			}
			uc, _ = CommandOf(confirmed)
		}
		cmd := uc.Command
		if !uc.Validated {
			v := up.SingleValidator
			if m.Generator == "upstream-multi" || len(cmd.Candidates) > 1 {
				v = up.MultiValidator
			}
			validated, err := v.Validate(ctx, cmd, lo.Ternary(waited, 0, up.ValidationDelay))
			waited = true
			if err != nil {
				if disruption.IsValidationError(err) {
					reason := validationReason(err)
					cmd.EmitRejectedEvents(up.Events, reason)
					r.Aborted = append(r.Aborted, m.ID)
					r.Reasons[m.ID] = "validation: " + reason
					continue
				}
				return r, fmt.Errorf("validating consolidation, %w", err)
			}
			cmd = validated
		}
		r.Commands = append(r.Commands, cmd)
		r.Executed = append(r.Executed, m.ID)
		r.PodsEvicted += len(m.Bindings)
	}
	return r, nil
}

func validationReason(err error) string {
	var budgetErr *disruption.BudgetValidationError
	var schedErr *disruption.SchedulingValidationError
	var churnErr *disruption.ChurnValidationError
	switch {
	case errors.As(err, &budgetErr):
		return "budget"
	case errors.As(err, &schedErr):
		return "scheduling"
	case errors.As(err, &churnErr):
		return "churn"
	}
	return "unknown"
}

func init() {
	RegisterCandidateSelector("upstream", func(env *Env, p Params) (CandidateSelector, error) {
		if err := p.Decode(&struct{}{}); err != nil {
			return nil, err
		}
		return &upstreamSelector{env: env}, nil
	})
	RegisterMoveGenerator("upstream-multi", func(env *Env, p Params) (MoveGenerator, error) {
		mode, err := decodeMode(p)
		if err != nil {
			return nil, err
		}
		return &upstreamMulti{env: env, mode: mode}, needUpstream(env, "upstream-multi")
	})
	RegisterMoveGenerator("upstream-single", func(env *Env, p Params) (MoveGenerator, error) {
		mode, err := decodeMode(p)
		if err != nil {
			return nil, err
		}
		return &upstreamSingle{env: env, mode: mode}, needUpstream(env, "upstream-single")
	})
	RegisterPlanner("upstream", func(env *Env, p Params) (Planner, error) {
		return &upstreamPlanner{env: env, name: "upstream"}, p.Decode(&struct{}{})
	})
	// savings-only is the upstream planner under the name the experiments use for P0: first valid move by savings,
	// with Balanced scoring only where a NodePool asks for it.
	RegisterPlanner("savings-only", func(env *Env, p Params) (Planner, error) {
		return &upstreamPlanner{env: env, name: "savings-only"}, p.Decode(&struct{}{})
	})
	RegisterPlanner("balanced", func(env *Env, p Params) (Planner, error) {
		return &upstreamPlanner{env: env, name: "balanced", forceBalanced: true}, p.Decode(&struct{}{})
	})
	RegisterExecutor("upstream-queue", func(env *Env, p Params) (Executor, error) {
		return &upstreamQueue{env: env}, errors.Join(p.Decode(&struct{}{}), needUpstream(env, "upstream-queue"))
	})
}
