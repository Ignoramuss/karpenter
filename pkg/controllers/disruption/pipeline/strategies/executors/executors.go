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

// Package executors holds the stage 4 executors that carry out shift moves, which the orchestration queue cannot
// run: a shift removes no node, so there is no command to hand back. Merges still go to the queue exactly as the
// upstream-queue executor (E0, with the other adapters in the pipeline package) sends them.
//
//	E1  shift-evict        validate (true), taint (true), taintTTL ("10m"): after the validation wait, re-check each
//	                       shift on a fresh snapshot, taint its source PreferNoSchedule under
//	                       pipeline.ShiftSourceTaintKey, and evict its pods through the Eviction API
//	E2  steered            E1's params, mode ("affinity" | "most-allocated"): E1, and in affinity mode ask env.Steer,
//	                       before each eviction, that the replacement prefer the planned node; most-allocated relies on
//	                       the cluster's scheduler running the MostAllocated profile instead
//	E3  make-before-break  E1's params: E2 in affinity mode that also requires each destination to be initialized and
//	                       not being deleted, and nominates it, before evicting into it; shifts wait a cycle behind a
//	                       merge that launches a node
//
// Whatever a plan says, these executors never evict a do-not-disrupt pod or a pod on a do-not-disrupt node: every
// shift is re-checked against a fresh snapshot, taken at the first shift (after the validation wait when validating),
// and such pods are skipped. They never exceed a PodDisruptionBudget allowance: evictions are counted per budget
// across the whole plan against that snapshot's allowances, and the Eviction API refuses (429) anything past the live
// allowance, which stops the move.
//
// Moves run in plan order. A merge whose source is also the source of a shift in the same plan waits (a later cycle,
// or emptiness, removes the node once it is empty). In a sequence plan, a move that fails aborts every move after it;
// a move this executor defers does not.
package executors

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/samber/lo"
	"k8s.io/apimachinery/pkg/util/sets"

	v1 "sigs.k8s.io/karpenter/pkg/apis/v1"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption"
	"sigs.k8s.io/karpenter/pkg/controllers/disruption/pipeline"
)

// Registered strategy names.
const (
	// ShiftEvictName is E1.
	ShiftEvictName = "shift-evict"
	// SteeredName is E2.
	SteeredName = "steered"
	// MakeBeforeBreakName is E3.
	MakeBeforeBreakName = "make-before-break"
)

// E2's modes.
const (
	// ModeAffinity asks env.Steer that each evicted pod's replacement prefer its planned node.
	ModeAffinity = "affinity"
	// ModeMostAllocated adds nothing at execution: it records that the cluster's scheduler runs the MostAllocated
	// profile, which the simulator models as the placement for such arms.
	ModeMostAllocated = "most-allocated"
)

// ShiftSourceSinceAnnotationKey records when a node was tainted as a shift source, as RFC3339 on the injected clock.
// The taint lapses taintTTL after it, so a source no move ever empties does not repel pods forever.
const ShiftSourceSinceAnnotationKey = v1.ShiftSourceSinceAnnotationKey

// Reasons recorded in Report.Reasons, so the decision log can count them.
const (
	// ReasonPDB stops a shift whose next eviction a PodDisruptionBudget refuses; pods already evicted stay evicted.
	ReasonPDB = "pdb"
	// ReasonDoNotDisrupt marks a shift that skipped do-not-disrupt pods.
	ReasonDoNotDisrupt = "do-not-disrupt"
	// ReasonSourceDraining defers a merge whose source a shift in the same plan is emptying.
	ReasonSourceDraining = "deferred: source draining"
	// ReasonWaitingForLaunch defers E3's shifts behind a merge that launches a node this cycle.
	ReasonWaitingForLaunch = "deferred: waiting for launched capacity"
	// ReasonDestinationNotReady aborts an E3 shift whose destination is not initialized or is being deleted.
	ReasonDestinationNotReady = "destination not ready"
	// ReasonChurnPrefix starts the reason of a shift the fresh snapshot no longer supports.
	ReasonChurnPrefix = "churn: "
	// ReasonPodsLeft marks a shift, run without validation, some of whose planned pods were no longer on their
	// source; the rest moved.
	ReasonPodsLeft = ReasonChurnPrefix + "planned pods left their source"
)

const defaultTaintTTL = 10 * time.Minute

var _ pipeline.Executor = (*executor)(nil)

func init() {
	pipeline.RegisterExecutor(ShiftEvictName, newShiftEvict)
	pipeline.RegisterExecutor(SteeredName, newSteered)
	pipeline.RegisterExecutor(MakeBeforeBreakName, newMakeBeforeBreak)
}

// shiftParams configure every executor here. Pointers tell an omitted value from an explicit false.
type shiftParams struct {
	// Validate waits out the validation delay before the first shift and re-checks each shift with Fast on a fresh
	// snapshot, aborting those that no longer pass. Default true. Without it the hard constraints are still checked.
	Validate *bool `json:"validate"`
	// Taint marks shift sources PreferNoSchedule so replacements do not land back on them. Default true.
	Taint *bool `json:"taint"`
	// TaintTTL is how long a shift-source taint lasts, as a Go duration. Default "10m".
	TaintTTL *string `json:"taintTTL"`
}

// steeredParams configure E2.
type steeredParams struct {
	shiftParams
	// Mode is ModeAffinity (default) or ModeMostAllocated.
	Mode *string `json:"mode"`
}

func (p shiftParams) options(name string) (options, error) {
	o := options{validate: lo.FromPtrOr(p.Validate, true), taint: lo.FromPtrOr(p.Taint, true), taintTTL: defaultTaintTTL}
	if p.TaintTTL != nil {
		d, err := time.ParseDuration(*p.TaintTTL)
		if err != nil {
			return o, fmt.Errorf("%s taintTTL %q, %w", name, *p.TaintTTL, err)
		}
		if d <= 0 {
			return o, fmt.Errorf("%s taintTTL %q, want a positive duration", name, *p.TaintTTL)
		}
		o.taintTTL = d
	}
	return o, nil
}

func decode(name string, p pipeline.Params, out any) error {
	if err := p.Decode(out); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

func newShiftEvict(env *pipeline.Env, p pipeline.Params) (pipeline.Executor, error) {
	var raw shiftParams
	if err := decode(ShiftEvictName, p, &raw); err != nil {
		return nil, err
	}
	o, err := raw.options(ShiftEvictName)
	if err != nil {
		return nil, err
	}
	return newExecutor(env, ShiftEvictName, o)
}

func newSteered(env *pipeline.Env, p pipeline.Params) (pipeline.Executor, error) {
	var raw steeredParams
	if err := decode(SteeredName, p, &raw); err != nil {
		return nil, err
	}
	o, err := raw.options(SteeredName)
	if err != nil {
		return nil, err
	}
	switch mode := lo.FromPtrOr(raw.Mode, ModeAffinity); mode {
	case ModeAffinity:
		o.steer = true
	case ModeMostAllocated:
	default:
		return nil, fmt.Errorf("%s mode %q, want %s or %s", SteeredName, mode, ModeAffinity, ModeMostAllocated)
	}
	return newExecutor(env, SteeredName, o)
}

func newMakeBeforeBreak(env *pipeline.Env, p pipeline.Params) (pipeline.Executor, error) {
	var raw shiftParams
	if err := decode(MakeBeforeBreakName, p, &raw); err != nil {
		return nil, err
	}
	o, err := raw.options(MakeBeforeBreakName)
	if err != nil {
		return nil, err
	}
	o.steer, o.makeBeforeBreak = true, true
	return newExecutor(env, MakeBeforeBreakName, o)
}

func newExecutor(env *pipeline.Env, name string, o options) (pipeline.Executor, error) {
	if env == nil || env.Upstream == nil {
		return nil, fmt.Errorf("%s needs a live cluster (pipeline.Env.Upstream)", name)
	}
	return &executor{env: env, name: name, options: o, fast: &pipeline.FastChecker{}}, nil
}

type options struct {
	validate bool
	taint    bool
	taintTTL time.Duration
	// steer asks env.Steer for each eviction (E2 affinity, E3).
	steer bool
	// makeBeforeBreak checks and nominates destinations, and holds shifts behind launches (E3).
	makeBeforeBreak bool
}

// executor is E1, E2 and E3; they differ only in options.
type executor struct {
	env  *pipeline.Env
	name string
	options
	// fast re-checks shifts when the env has no Feasibility service.
	fast *pipeline.FastChecker
}

func (e *executor) Name() string { return e.name }

// Idle lifts expired shift-source taints on cycles with nothing to execute.
func (e *executor) Idle(ctx context.Context) { e.expireTaints(ctx) }

var _ pipeline.IdleExecutor = (*executor)(nil)

// outcome is what happened to one move.
type outcome struct {
	executed bool
	// complete reports the move ran in full, for sequence plans.
	complete bool
	// deferred reports the executor chose to wait, which does not break a sequence.
	deferred bool
	reason   string
	pods     int
	// evicted are the bindings whose pods a shift evicted, when fewer than it planned.
	evicted []pipeline.Binding
	command any
}

// failed is an abort whose reason carries the error that caused it.
func failed(prefix string, err error) outcome { return outcome{reason: prefix + ": " + err.Error()} }

// breaks reports whether the outcome ends a sequence: anything short of running in full, except a deferral.
func (o outcome) breaks() bool { return !o.complete && !o.deferred }

func (o outcome) addTo(r *pipeline.Report, id string) {
	if o.executed {
		r.Executed = append(r.Executed, id)
	} else {
		r.Aborted = append(r.Aborted, id)
	}
	if o.reason != "" {
		r.Reasons[id] = o.reason
	}
	r.PodsEvicted += o.pods
	if o.evicted != nil {
		if r.Evicted == nil {
			r.Evicted = map[string][]pipeline.Binding{}
		}
		r.Evicted[id] = o.evicted
	}
	if o.command != nil {
		r.Commands = append(r.Commands, o.command)
	}
}

// Execute runs the plan's moves in order. It first lifts expired shift-source taints; then, if any shift will run
// and validation is on, waits out the validation delay once, so every shift and merge after it validates against the
// cluster as it is after the wait.
func (e *executor) Execute(ctx context.Context, p pipeline.Plan) (pipeline.Report, error) {
	r := pipeline.Report{Reasons: map[string]string{}}
	e.expireTaints(ctx)
	deferred := e.deferrals(p)
	run := newShiftRun(p, deferred)
	waited := false
	if e.validate && run.shifts > 0 {
		if err := e.wait(ctx, e.env.Upstream.ValidationDelay); err != nil {
			return r, fmt.Errorf("waiting to validate shifts, %w", err)
		}
		waited = true
	}
	broken := ""
	for i, m := range p.Moves {
		o, err := e.dispatch(ctx, p, m, deferred[i], broken, run, &waited)
		if err != nil {
			return r, err
		}
		o.addTo(&r, m.ID)
		if p.Sequence && broken == "" && o.breaks() {
			broken = m.ID
		}
	}
	return r, nil
}

// dispatch runs one move, unless a failed predecessor in a sequence (broken) or a deferral holds it back.
func (e *executor) dispatch(ctx context.Context, p pipeline.Plan, m pipeline.Scored, deferral, broken string, run *shiftRun, waited *bool) (outcome, error) {
	switch {
	case broken != "":
		return outcome{reason: fmt.Sprintf("sequence: %s did not complete", broken)}, nil
	case deferral != "":
		return outcome{reason: deferral, deferred: true}, nil
	case m.Kind == pipeline.Shift:
		return e.shift(ctx, run, m.Move)
	}
	return e.merge(ctx, p, m, waited)
}

func (e *executor) wait(ctx context.Context, d time.Duration) error {
	if w := e.env.Upstream.Wait; w != nil {
		return w(ctx, d)
	}
	return nil
}

// deferrals decides, by plan index, which moves wait for a later cycle. Deciding before anything runs keeps the
// validation wait and every later check consistent with one view of the plan.
func (e *executor) deferrals(p pipeline.Plan) []string {
	out := make([]string, len(p.Moves))
	launches := lo.ContainsBy(p.Moves, func(m pipeline.Scored) bool { return m.Kind != pipeline.Shift && len(m.Launch) > 0 })
	draining := sets.New[string]()
	for i, m := range p.Moves {
		if m.Kind != pipeline.Shift {
			continue
		}
		// The queue launches a merge's replacement before it deletes anything, so E3 lets that capacity arrive
		// rather than racing the launch for the same destinations.
		if e.makeBeforeBreak && launches {
			out[i] = ReasonWaitingForLaunch
			continue
		}
		draining.Insert(m.Sources...)
		for _, b := range m.Bindings {
			draining.Insert(b.From)
		}
	}
	for i, m := range p.Moves {
		if m.Kind != pipeline.Shift && lo.SomeBy(m.Sources, draining.Has) {
			out[i] = ReasonSourceDraining
		}
	}
	return out
}

// merge is the upstream-queue path: confirm a move that carries no upstream command with Full, validate the command
// with the validator its method would use (waiting only if nothing has waited yet this cycle), and hand it back for
// the orchestration queue. Its pods count as evicted, as upstream-queue counts them.
func (e *executor) merge(ctx context.Context, p pipeline.Plan, m pipeline.Scored, waited *bool) (outcome, error) {
	up := e.env.Upstream
	uc, ok := pipeline.CommandOf(m.Move)
	if !ok {
		if e.env.Feasibility == nil || p.Snapshot == nil {
			return outcome{reason: "full feasibility: no feasibility service or plan snapshot"}, nil
		}
		confirmed, err := e.env.Feasibility.Full(ctx, p.Snapshot, m.Move)
		if err != nil {
			return failed("full feasibility", err), nil
		}
		if uc, ok = pipeline.CommandOf(confirmed); !ok {
			return outcome{reason: "full feasibility: no upstream command"}, nil
		}
	}
	cmd := uc.Command
	if !uc.Validated {
		v := up.SingleValidator
		if m.Generator == "upstream-multi" || len(cmd.Candidates) > 1 {
			v = up.MultiValidator
		}
		validated, err := v.Validate(ctx, cmd, lo.Ternary(*waited, 0, up.ValidationDelay))
		*waited = true
		if err != nil {
			if disruption.IsValidationError(err) {
				reason := validationReason(err)
				cmd.EmitRejectedEvents(up.Events, reason)
				return outcome{reason: "validation: " + reason}, nil
			}
			return outcome{}, fmt.Errorf("validating consolidation, %w", err)
		}
		cmd = validated
	}
	return outcome{executed: true, complete: true, pods: len(m.Bindings), command: cmd}, nil
}

// validationReason mirrors upstream-queue's classification of validation failures.
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
