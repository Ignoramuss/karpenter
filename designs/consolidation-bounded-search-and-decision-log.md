# Bounded Search Budgets and a Decision Log for Consolidation

Give consolidation an explicit, machine-independent search budget and a structured record of every move it considered,
through stage interfaces that reproduce today's decisions exactly, behind an off-by-default alpha feature gate.

This RFC is the upstream-facing subset of the [pluggable consolidation pipeline](consolidation-pipeline.md) that the
research branch built and measured. That document holds the full design (interfaces, adapters, shift moves,
potentials, planners). This one proposes only the three parts of it whose value the experiments established, and cites
the rest as evidence. Where the two documents overlap, this one wins on scope and the other on mechanism.

## Motivation

Karpenter's consolidation runs as two disruption methods, `MultiNodeConsolidation` and `SingleNodeConsolidation`. Each
one orders candidates, generates moves, decides whether to accept them and validates them inside a single
`ComputeCommands` call (consolidation-pipeline.md, Motivation). Three properties of that structure are the problem
this RFC addresses.

**The search is bounded by time, not by work.** Multi-node consolidation binary-searches over prefixes of the
savings-ratio-sorted candidates, with a window of up to 101 nodes, and stops after one minute of wall-clock time
(`MultiNodeConsolidationTimeoutDuration`, a `context.WithTimeout`). Single-node consolidation walks the candidates
until three minutes on the injected clock have passed. So what a pass finds depends on how fast the machine is and on
what else it is running: a busy controller finds fewer consolidations than an idle one from the same cluster state. It
also means two search strategies cannot be compared at equal compute, because neither reports how much work it did
(consolidation-pipeline.md, Motivation, consequence 2). A full single-node scan at 1,000 nodes was estimated at about
58 s on the 4 vCPU research VM, 864 candidates at 67 ms per scheduling simulation (consolidation-pipeline.md,
Motivation, consequence 2).

**Rejected alternatives leave no record.** The controller logs the command it starts, emits `Unconsolidatable` events
for candidates and records metrics for scored moves. It keeps no structured record of every move it considered and why
each one lost (consolidation-pipeline.md, Motivation, consequence 4). An operator who asks why a node was not
consolidated, or why the cluster has held a fragmented fleet for an hour, has no artifact to look at. The Balanced
RFC's issue list is largely this question in different forms: nodes at 93-99% utilization disrupted instead of lightly
utilized ones, multi-hour loops replacing the same instance types, churn where deleted nodes are re-provisioned at
once (balanced-consolidation.md, Motivation).

**The stages cannot vary independently.** Candidate order, move generation, acceptance and validation are interleaved
in one loop per method, so none can be changed or measured on its own, and the Balanced evaluator is called from
inside both loops (consolidation-pipeline.md, Motivation, consequence 1). Every improvement to consolidation,
including the Balanced policy itself, has had to be threaded through those loops.

The research branch addressed all three with a pluggable pipeline, then ran a preregistered set of experiments on it
(experiments/RESULTS.md) and a replay on a KWOK cluster (experiments/TIERB.md). The experiments did not find a new
strategy that beats upstream's search (see Non-Goals). What they did establish is that the instrument works: the
pipeline's upstream adapters reproduce upstream's decisions exactly, its search budgets make strategies comparable at
equal compute, and its decision log answers questions the controller cannot answer today. That instrument is what this
RFC proposes to carry upstream.

### Use Cases

1. **A per-pass compute ceiling that does not depend on the machine.** An operator running 1,000 or more nodes wants
   consolidation to spend at most, say, 16 scheduling simulations per pass, whatever the controller's CPU allocation,
   and wants a metric that says when that ceiling was hit. Today the only ceiling is the one-minute wall-clock
   deadline, which binds differently on every machine.
2. **Evaluating a change to the search or the acceptance rule at equal compute.** A maintainer wants to compare the
   current prefix search against a variant (a different candidate order, a different `k` for Balanced, a different
   window) and needs both to run under the same simulation budget, with a record of what each considered, so the
   comparison measures the strategy and not the machine. The research branch did exactly this for the session's
   configurations over 1,551 simulated runs (RESULTS.md, What ran).
3. **Explaining a non-decision.** An operator asks why node X, at 30% utilization, has not been consolidated for an
   hour. The decision log shows every move that touched X in each cycle, its predicted savings, and the reason it
   lost: `node budget for default: 2 > 1`, `conflicts with m4`, `validation: churn`, or that the search never reached
   it before the budget ran out.
4. **Measuring plan fidelity in the field.** A consolidation command is a bet that evicted pods will land where the
   simulation put them. The research branch measured that bet in the simulator: under default kube-scheduler scoring,
   upstream's own commands landed on plan for 45% of evicted pods (paper.md, section 6.5). Nothing in the controller
   measures it on a real cluster. A per-pod planned destination in the decision log is the half of that measurement
   the controller can supply.

### Non-Goals

- **No new default consolidation strategy.** With the gate on and no configuration, the pipeline makes the same
  decisions as today's two methods; with the gate off, nothing changes. No strategy other than the upstream adapters
  is proposed for the operator.
- **No shift moves, potentials or entropy-guided planners in the operator.** The research branch's `random-merge`,
  `random-shift`, `greedy-shift`, `entropy-greedy`, `anneal-plan` and the `util-entropy` potential stay in the
  research branch's `strategies` packages and the simulator. They are evidence for the seams, not a proposal.
  Specifically, the experiments did not show that random or entropy-guided generation beats upstream: random
  generation was equivalent to upstream's search within 3% in only 3 of 12 cells and ended 1.4-4.3% dearer after a
  diurnal scale-down (RESULTS.md, H1); shifts cost 1.7-7.0% more than merge-only at an equal eviction budget
  (RESULTS.md, H3); annealing bought 3.0% of cost for 37.7% more evictions on one workload (RESULTS.md, H4); the
  pipeline's savings-versus-disruption frontier did not dominate upstream's Balanced policy (RESULTS.md, H6); on the
  KWOK cluster random generation ended 10.6% dearer than heuristic generation with the same planner at 200 nodes and
  used 52% more controller CPU (RESULTS.md, Tier B); and the cluster resolved no ordering among the five
  configurations within a few percent of upstream (RESULTS.md, Tier B).
- **No change to NodePool API fields**, to the semantics of disruption budgets, `consolidateAfter`,
  `consolidationPolicy` or any annotation users set.
- **No asynchronous or continuous generation.** All stages still run inside one `ComputeCommands` call per pass. The
  Balanced RFC's open question on asynchronous generation stays open; the seams are its prerequisite, not its answer.
- **No change to Emptiness, Drift, StaticDrift, Repair, the orchestration queue, termination or drain.**

## Proposal

With `--feature-gates ConsolidationPipeline=true`, the disruption controller runs one consolidation method,
`PipelineConsolidation`, where multi-node and single-node consolidation used to run. The method composes four stages
(candidates, moves, plan, execute) around shared services, and ships with exactly one implementation of each stage:
the coarse upstream adapters, which call today's `ComputeCommands` and convert the result. Three things are new to the
user:

1. **A search budget.** `search.maxFullSimulations` caps scheduling simulations per pass, `search.maxAttempts` caps
   generation attempts per generator, and `search.timeout` is a deadline on the injected clock. Each defaults to 0,
   which means unbounded, the same as today. When set, the search stops at the budget rather than at the wall clock,
   so the same cluster state gives the same decision on any machine, and a strategy's cost is counted, not timed.
2. **A decision log.** Every stage decision is one structured, schema-versioned event: the candidates considered, each
   move generated with its predicted savings and disruption, the planner's verdict and reason, and the executor's
   outcome, plus a per-pass summary of attempts, simulations and moves. A `Recorder` interface receives them. The
   default recorder is a no-op; a klog recorder writes them at verbosity 1 and 2; a file recorder writes JSON lines.
3. **Stage seams behind the gate.** The `CandidateSelector`, `MoveGenerator`, `Planner` and `Executor` interfaces, the
   `Snapshot`, `Feasibility`, `Scorer` and `SearchBudget` services, and a registry that selects each stage by name
   from configuration. A differential parity test holds the adapters to upstream's decisions.

### Proposed Spec

**Feature gate and flag** (`pkg/operator/options/options.go`; consolidation-pipeline.md, Proposed Spec):

| Item | Value |
| --- | --- |
| Feature gate | `ConsolidationPipeline`, an `options.FeatureGate` var in `KarpenterFeatureGates`, alpha, default `false` |
| Flag | `--consolidation-pipeline-config`, env `CONSOLIDATION_PIPELINE_CONFIG`, default empty |
| Flag value | A file path, or the configuration inline as YAML or JSON |
| Empty value | The upstream adapters, no budget, no-op recorder: today's decisions |
| Invalid value | The operator fails at startup with the parse or validation error |

**Configuration schema** (abridged; the full schema is in consolidation-pipeline.md, Proposed Spec):

```go
type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"`
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Search     SearchConfig `json:"search,omitzero"`
	Recorder   StageConfig  `json:"recorder,omitzero"` // new in this RFC: none (default), log, file
}

type SearchConfig struct {
	MaxAttempts        int    `json:"maxAttempts,omitempty"`        // generation attempts per generator; 0 = unlimited
	MaxFullSimulations int    `json:"maxFullSimulations,omitempty"` // scheduling simulations per pass; 0 = unlimited
	Timeout            string `json:"timeout,omitempty"`            // deadline on the injected clock, e.g. "1m"
}
```

Each stage's `params` are decoded strictly: an unknown key or an out-of-range value fails construction, and so does an
unknown strategy name. The operator registers only the adapters in the table below, so any other name is an error.

**What ships in the operator** (the registry with the gate on):

| Stage | Name | Role |
| --- | --- | --- |
| candidates | `upstream` | This pass's consolidation candidates, in the controller's order; upstream sorts them itself |
| moves | `upstream-multi` | Calls `MultiNodeConsolidation.ComputeCommands` and converts the returned command to a move |
| moves | `upstream-single` | Calls `SingleNodeConsolidation.ComputeCommands`, the same way |
| plan | `upstream` | Re-checks the command against the pass's budgets; a command upstream already approved passes |
| execute | `upstream-queue` | Returns the already-validated command for the orchestration queue |
| recorder | `none`, `log`, `file` | Decision log sink |

The research branch's `strategies/{candidates,moves,planners,executors}` packages, which hold the fine adapters and
every experimental strategy, are not imported by the operator. That is a change from the branch today, where
`pkg/controllers/controllers.go` imports `strategies/all` (consolidation-pipeline.md, Open Question 9). They stay
compiled into the simulator and the KWOK suite, where they are tested.

**A configuration that bounds the search and logs decisions:**

```yaml
pipeline:
  search:   {maxFullSimulations: 16, timeout: 1m}
  recorder: {name: log}
```

Everything not named takes its default, so this runs upstream's own search, stopped after 16 scheduling simulations
per pass or one minute on the injected clock, whichever comes first, and logs what it considered.

**Decision log schema** (version 1; bump on any field change; consolidation-pipeline.md, Decision log):

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `run_id` | string | `"controller"` in the operator; the run ID in the simulator |
| `cycle` | int | Pipeline cycle counter |
| `t` | float | Seconds since the method started, from the injected clock |
| `stage` | string | `candidates`, `moves`, `plan`, `execute` |
| `strategy` | string | Strategy name; generators joined with `+` on a round summary |
| `move_id` | string | Unique per strategy instance, for example `multi-3`; empty on summaries |
| `kind` | string | `merge` (upstream's delete and replace both map to it), `shift`, `rightsize` |
| `sources`, `destinations` | []string | Sources in upstream's candidate order; destinations sorted |
| `pods` | int | Bindings (on the candidates summary: candidate count) |
| `delta_potential`, `disruption`, `savings_per_hour` | float | The move's scores; `delta_potential` is 0 under the `none` potential |
| `accepted` | bool | The stage's verdict |
| `reason` | string | Why not: `not downhill`, `node budget for default: 2 > 1`, `conflicts with m4`, `validation: churn` |
| `budget_remaining` | int | Simulations left in the pass after this event. Reserved on the branch today and never set (consolidation-pipeline.md, Open Question 10); this RFC makes it required. |
| `detail` | map[string]float | Stage-specific numbers. Round summary: `moves`, `attempts`, `full_simulations`, `timed_out`, `exhausted`. Per move: `launch`, `remove`, `norm_disruption`, `norm_savings`. |

A move's `bindings` (pod, from, planned destination) are not in the version 1 schema; a per-pod planned destination is
what the plan-fidelity metric needs (Use Case 4), so this RFC adds an optional `bindings` field at verbosity 2 and in
the file sink, as schema version 2.

### How It Works

**One pass.** The method runs where multi-node consolidation ran, with the same candidates and budgets `disrupt()`
computes today (consolidation-pipeline.md, How It Works, One cycle):

```
disrupt() ─► Method.ComputeCommands(budgets, candidates)
               ├─ BuildSnapshot                                      ─► nodes, pods, PDB allowances, totals
               ├─ stage 1  selector.Select(snapshot)                 ─► sources, destinations
               ├─ for each generator, in order (one round each)
               │    ├─ stage 2  g.Generate(..., budget.Fork())       ─► moves
               │    ├─ stage 3  planner.Plan(snapshot, moves, budgets) ─► plan
               │    ├─ stage 4  executor.Execute(plan)               ─► commands
               │    └─ a round that executes anything ends the pass
               └─ record a DecisionEvent at every stage
disrupt() ─► Queue.StartCommand for each returned command (unchanged)
```

In sequential mode, the default and the only mode shipped here, each generator is its own round and the next round
runs only if the previous one executed nothing, which is how the controller runs multi-node and then single-node
consolidation today. The method keeps upstream's own skip rule: with the upstream adapters it uses `IsConsolidated`
per method rather than the pipeline's cluster-state gate.

**The coarse adapters.** `upstream-multi` and `upstream-single` call the upstream methods' own `ComputeCommands`, so
upstream's search, acceptance, Balanced metrics, events and validation all run as today, and the adapter only converts
the returned `Command` into a `Move` and back (consolidation-pipeline.md, Upstream adapters). Each adapter builds its
own `MultiNodeConsolidation` or `SingleNodeConsolidation` through the same constructor `NewMethods` uses, so
`lastConsolidationState`, `PreviouslyUnseenNodePools` and the validators behave the same. This mode is upstream by
construction, and it is the only mode this RFC ships.

**Where the budget binds.** A `SearchBudget` holds `MaxAttempts`, `MaxFull` and the deadline. Generators call `Next()`
once per attempt and `UseFull()` before each scheduling simulation; attempts are counted per generator (`Fork()`),
full simulations and the deadline are shared across the pass, and `Stats()` reports usage to the decision log
(consolidation-pipeline.md, Search budget). On the research branch the budget is consulted by the fine adapters, which
re-implement the two searches, not by upstream's own loops, so under the coarse adapters a budget has nothing to bind.
This RFC closes that gap with one small change to the two upstream loops, which is proposed here and not yet
written on the research branch: `firstNConsolidationOption` and the single-node scan accept an optional observer with
`Next()`, `UseFull()` and `Record()` (the existing `pipeline_hooks.go` is the home for the type), check `UseFull()`
before each `SimulateScheduling` and stop the search when it is refused, and call `Record()` with the probe's
result. The observer is nil with the gate off, so the loops
are unchanged there. With the gate on it is the pass's `SearchBudget` and `Recorder`. This makes the coarse adapters
both bounded and logged without a second copy of the search. The fine adapters, which are the branch's existing answer
to the same need and pass the same parity test, are Alternative 5 and Open Question 3.

**What "stop at the budget" means.** The multi-node binary search probes prefixes of the sorted candidates; when a
probe is refused it keeps the best prefix found so far, exactly as it does when the one-minute context expires today.
The single-node scan stops at the candidate it has reached and records the NodePools it did not visit in
`PreviouslyUnseenNodePools`, as it does at its three-minute limit. In both cases the pass emits whatever was accepted
before the budget ran out, and the decision log's round summary records `exhausted: 1` with the attempts and
simulations spent.

**The recorder.** `Recorder.Record(DecisionEvent)` is called synchronously at each stage. The `none` recorder
discards. The `log` recorder writes the round summaries at V(1) and per-move events at V(2) through the
`consolidation-pipeline` logger with the message `consolidation decision` and the event's fields as key-value pairs.
The `file` recorder appends one JSON object per line to a path, and rotates by size. No Go map iteration order reaches
a logged list and the JSON encoder sorts `detail`'s keys, so the same cluster state, configuration and commit produce
byte-identical events; the simulator's `TestDeterminism` checks this (consolidation-pipeline.md, Decision log).

### Evidence from the research branch

Everything below was measured on the research branch with the full pipeline, including the fine adapters and the
experimental strategies, so the numbers bound what this RFC's subset can do rather than measure it directly. Full
tables are under `paper/tables/` and are regenerated from the raw runs by `make figures`.

**The adapters reproduce upstream's decisions.** `TestParity` runs upstream's two methods and then one pipeline cycle
with each adapter configuration on seeded cluster states (4 to 40 nodes, one to three NodePools, two instance
catalogs, both consolidation policies) and requires identical decisions: kind, candidates in order, replacement
instance-type options in order, budget consumed per NodePool. Results: 1,000 of 1,000 states matched per policy and
catalog, then 10,000 of 10,000 in every configuration (coarse, fine, and fine with the Balanced planner on Balanced
states), on states where upstream itself decided to act in 83 to 91% of cases (RESULTS.md, What ran, Parity;
pipeline-refactor.md, section 2). A 1,000-state run took 21 minutes; a 40-state run is in `make test`
(consolidation-pipeline.md, Parity as the compatibility guarantee). Three injected bugs in the fine adapters: dropping
the multi-node budget decrement was caught (44 of 60 states matched), accepting every move was caught on Balanced
states (85 of 100), and skipping the `CanPass` pre-filter was not and cannot be, since it changes compute but not
decisions (consolidation-pipeline.md, Mutation check). That last point is why the search budget needs its own metric
and the decision log, not only the parity test.

**Parity holds over whole runs and on a real control plane.** Over 3-hour simulated runs on three workloads at 200
nodes, the pipeline configured as upstream (B4: the fine adapters with the upstream planner and queue, uncapped) was
within -0.75% to +0.34% of upstream's steady-state cost, every 90% interval within -1.8% to +1.0%, all six equivalence
tests at 3% passing, 7 of 60 seeds matching upstream exactly on four metrics (RESULTS.md, B4). On the KWOK cluster at
five seeds, every Tier B interval for B4 against upstream included zero: end cost +1.7% at 50 nodes and +0.3% at 200,
intervals of ±6.9% and ±2.9% (RESULTS.md, Tier B, named pairs; B4 vs B2).

**The overhead of the pipeline configured as upstream.** In the simulator at 200 nodes it spent 5 to 16% less CPU per
disruption pass than upstream (93 vs 111 ms on the diurnal workload) (pipeline-refactor.md, section 5). On the KWOK
cluster at 200 nodes over a 30-minute window: 153.8 controller core-seconds against upstream's 148.8, peak memory
227.0 against 216.1 MB, 461.9 against 414.7 seconds in decision evaluation (which includes the fixed 15-second
validation wait per command), 18.8 against 18.0 commands decided (RESULTS.md, Tier B, compute table). The paired CPU
interval includes zero (pipeline-refactor.md, section 5).

**Time-bounded search is machine-dependent; budgeted search is not.** Exp 7 compared two generation strategies at
1,000 nodes and found that the mean CPU per pass favored one (895 vs 1,425 ms, -37.2%) and the 95th percentile the
other (2,726 vs 2,285 ms, +19.3%), both in all ten seeds (RESULTS.md, H7). Because the two arms had run on different
VMs, both were rerun interleaved on one machine: -29.4% and +37.9%, the same outcome, and every rerun reproduced its
session twin's decision log exactly (RESULTS.md, H7-S). The budgeted arms' decisions did not depend on the machine;
only their timing did. The scaling table is what a budget protects against: mean CPU per pass grew about 72x for the
pipeline arm that runs upstream's search from 50 to 1,000 nodes against a 20x growth in nodes (19.7 ms to 1,425.1 ms),
while no pass in those cells came near the one-minute limit, the slowest being 6.1 s, about a tenth of it (RESULTS.md,
H7, Scaling). The session's slowest pass overall was upstream's own, 52.3 s, on the one run of the workload with
PodDisruptionBudgets, hostname spread and anti-affinity that finished (RESULTS.md, H7, Scaling). On a cluster with
such constraints, upstream's one-minute deadline is close.

**The decision log explains costs the metrics cannot.** The paper's analysis of why random generation costs more CPU
per pass than upstream's search (paper.md, section 7) was done entirely from decision logs: per-cycle counts of
attempts, simulations and moves showed that one strategy searched on 99 to 100% of passes and the other on 17 to 48%,
that the random strategy spent exactly 1,000 attempts and all 16 simulations on every searching pass at 1,000 nodes,
and that the cost per searching pass differed by 5.5 to 6.8x on dense fleets against 1.3 to 3.1x on fragmented ones.
The two tables that carry that argument:

| Mean CPU per pass, random / upstream's search, same planner | 200 nodes | 1,000 nodes |
| --- | --- | --- |
| W1 fragmented | 98 / 143 ms | 895 / 1,425 ms |
| W4 mixed shapes | 59 / 103 ms | 468 / 944 ms |
| W2 diurnal | 121 / 97 ms | 1,246 / 666 ms |
| Compressed diurnal (the cluster workload) | 194 / 85 ms (upstream itself: 56 ms) | not run |

| Per searching pass (decision logs, three to five seeds) | Share of passes that search | CPU per searching pass | Simulations per search | Moves emitted per search |
| --- | --- | --- | --- | --- |
| W1, 1,000 nodes: upstream's search / random | 0.99 / 0.48 | 1.5 s / 2.0 s | 11.1 / 16.0 | 2.5 / 25 |
| W2, 1,000 nodes | 1.00 / 0.31 | 0.66 s / 3.6 s | 7.0 / 16.0 | 2.0 / 30 |
| W2, 200 nodes | 1.00 / 0.20 | 0.10 s / 0.67 s | 3.9 / 15.6 | 0.6 / 32 |
| Compressed diurnal, 200 nodes | 0.99 / 0.33 | 0.09 s / 0.58 s | 3.5 / 14.6 | 2 / 149 |

(paper.md, section 7, both tables.) None of those columns exists as a metric today. The first table's row for the
cluster workload also shows the cost of the pipeline's own gating rule against upstream's: the heuristic pipeline arm,
which does not stop at the first acceptable move under its planner, cost 85 ms per pass against upstream's 56
(paper.md, section 7, item 2). On the cluster itself, random generation used 219 to 228 controller core-seconds over
30 minutes against 136 to 154 for every other configuration, upstream included (RESULTS.md, Tier B, compute table).

**Plan fidelity is low and measurable.** In the simulator under default kube-scheduler scoring, about three in four
pods evicted by the pipeline landed somewhere other than planned (off-plan share 0.749, 95% lower bound 0.725, with
spread scale-down; 0.755 with deletion-cost scale-down), and upstream's own commands landed on plan for 0.451 and
0.455 of evicted pods (RESULTS.md, H5). The pipeline realized 57% of the potential drop its plans predicted, upstream
47%, against 87% under `MostAllocated` scoring (paper.md, section 6.5). Steering replacement pods toward their planned
node raised fidelity by 72.5% (0.251 to 0.434); `MostAllocated` scoring by 29.3% and 43.7% (RESULTS.md, H5). Every
consolidation command is such a bet, and today nothing measures whether it is won. The per-pod planned destination in
the decision log is the controller's half of that measurement.

**Size of the change on the branch.** The pipeline package is 41 files and 12,356 lines, of which 3,356 lines in 8
files are tests (62 top-level test functions). Six upstream-owned files are touched: five modified by 70 added and 2
removed lines, and one new 61-line file of exported hooks that change no behavior (pipeline-refactor.md, section 2,
Size). This RFC's subset is smaller: it drops the shift executors, the planners, the potentials and the random
generators, and adds the observer in the two upstream loops.

**Exp 9 (pending).** An experiment on early stopping and biased candidate selection for the random generators
(`experiments/EXP9.md`) is running; its numbers on attempts and CPU per searching pass under an early-stop rule will
be filled in here from its analysis when it completes. It bears on how a budget should be spent, not on whether one
should exist.

### Interaction with Existing Features

| Feature | With the gate on, default configuration |
| --- | --- |
| NodePool disruption budgets | Unchanged. The method reports the `Underutilized` reason, so today's budgets apply to it; the adapters consult them where upstream does. |
| `karpenter.sh/do-not-disrupt` on pods and nodes | Unchanged: candidacy is upstream's. The snapshot carries the flag so any future stage sees it as a hard constraint. |
| PodDisruptionBudgets | Unchanged: upstream's candidate filter and the Eviction API. The snapshot carries each PDB's allowance for the same reason. |
| `consolidateAfter` | Unchanged: sources are this pass's consolidation candidates, which upstream filters by the `Consolidatable` condition. The interaction with shift moves (consolidation-pipeline.md, Open Question 1) does not arise, since no shift executor ships. |
| `consolidationPolicy: Balanced` | Unchanged. The coarse adapters call upstream's evaluator, so Balanced scores, its metrics and its `ConsolidationApproved` events are emitted exactly as today. The decision log records the evaluator's verdict per probe. |
| `SpotToSpotConsolidation` | Unchanged: the rule lives in `computeConsolidation`, which the adapters call. |
| Emptiness, Drift, StaticDrift, Repair, expiration | Unchanged, in the same order, with the same reasons and budgets. The pipeline replaces only the two consolidation methods. |
| `IsConsolidated` | Kept. With the upstream adapters the method uses upstream's own per-method skip rule, not the pipeline's cluster-state gate, so the number of searches per hour is the same as today (paper.md, section 7, item 1, describes both rules). Open Question 4 asks whether the pipeline-level gate should replace it. |
| Validation | Unchanged: the same validators and the same 15-second delay, inside the coarse adapters. |
| Orchestration queue, termination, drain | Unchanged: commands reach `Queue.StartCommand` through `disrupt()` exactly once, as today. |
| PodDeletionCostManagement | Independent. |

The search budget is the one new interaction. With `maxFullSimulations` set, the multi-node binary search can stop
before its window closes and the single-node scan before its candidates run out, so a pass under a budget may
consolidate less than an unbounded pass would. That is the point of the budget, and it is the same behavior as today's
deadlines, except that it is deterministic and counted.

### Observability

**Metrics.** Each dimension below is a `metrics.Label` with its stable values enumerated as `metrics.Value`s, in the
repo's style; `consolidation_type` reuses the existing `ConsolidationType` label, whose `pipeline` value already
exists on the branch (`pkg/controllers/disruption/metrics.go`). All are in the `voluntary_disruption` subsystem.

| Metric | Type | Dimensions | Meaning |
| --- | --- | --- | --- |
| `consolidation_search_attempts_total` | counter | `consolidation_type`, `strategy` | Generation attempts (`SearchBudget.Next` calls) |
| `consolidation_full_simulations_total` | counter | `consolidation_type`, `strategy` | Scheduling simulations the search ran |
| `consolidation_search_stopped_total` | counter | `consolidation_type`, `limit` (`attempts`, `full_simulations`, `timeout`) | Passes whose search stopped at a budget. The `timeout` value is the metric for pipeline search timeouts the pipeline RFC's beta criteria ask for (consolidation-pipeline.md, Graduation Criteria, item 4); today timeouts and exhaustion are recorded only in the decision log (consolidation-pipeline.md, Observability). |
| `consolidation_moves_considered_total` | counter | `consolidation_type`, `stage`, `outcome` (`accepted`, `not_downhill`, `budget`, `conflict`, `validation`, `other`) | Moves by the verdict each stage gave them. `outcome` buckets the decision log's free-text `reason`. |
| `consolidation_search_budget_remaining` | gauge | `consolidation_type` | Simulations left when the last pass ended; 0 means the budget bound. |
| `consolidation_plan_fidelity` | gauge | `consolidation_type` | Share of evicted pods, over a trailing window, whose replacement bound to the planned node. Computed by a small watcher that pairs the decision log's per-pod planned destination (schema version 2) with pod binding events. Optional; it is the one metric here that needs state beyond the pass. |

`stage` and `strategy` are new labels with values `candidates`, `moves`, `plan`, `execute` and the registered adapter
names. `limit` and `outcome` are new labels with the values listed. The existing `consolidation_timeouts_total` keeps
its `multi` and `single` labels, because the wrapped upstream methods emit it.

**Log volume.** On the research branch the controller writes every decision event at V(1). That is acceptable for
upstream's search, which emits 0.6 to 2.5 moves per searching pass, and not for a random generator, which emitted up
to 149 per searching pass at 200 nodes (paper.md, section 7, second table). The `log` recorder therefore writes the
round summary (one line per generator per pass: attempts, simulations, moves, stopped-at) at V(1) and per-move events
at V(2), and the `file` recorder is the sink for anyone who wants every event without raising the log level. The
research branch's decision logs are in `experiments/runs/*/decisions.jsonl.gz`; the simulator and the operator write
the same schema.

**Events.** Unchanged: upstream's `Unconsolidatable`, `ConsolidationApproved` and `ConsolidationRejected` events are
emitted by the code the adapters call.

### Edge Cases

- **The budget runs out mid-pass.** The multi-node search keeps its best accepted prefix and stops; if no prefix was
  accepted the pass moves to the single-node round, as it does today when the deadline expires. The single-node scan
  stops at the current candidate and records unseen NodePools. Whatever was accepted is validated and emitted; nothing
  is emitted unvalidated. The round summary records `exhausted: 1` and the metric
  `consolidation_search_stopped_total{limit="full_simulations"}` increments.
- **The budget is smaller than one probe.** `maxFullSimulations: 1` lets the multi-node search run exactly one
  simulation (its first prefix) and the single-node scan none. The configuration is accepted; the decision log shows
  what it cost. A value below 0 is a startup error.
- **The recorder blocks or errors.** `Record` is synchronous, so a slow sink slows the pass. The `file` recorder
  writes through a bounded buffer and drops events, counting the drops in its next round summary (`detail.dropped`),
  rather than block; a write error is logged once per minute and never returned to the method. A recorder can never
  fail a pass: the method does not look at anything the recorder returns.
- **The gate is flipped at runtime.** Feature gates are read at startup. Changing the gate or the configuration
  requires a restart, as every Karpenter feature gate does. Because this RFC ships no shift executor, nothing is left
  on nodes when the gate is turned off (the shift-source taint problem of consolidation-pipeline.md, Open Question 2,
  does not arise).
- **An unknown strategy name, or a strategy name from the research branch.** Construction fails at startup with
  `unknown moves strategy "random-merge"` and the registered names. There is no fallback to the default, so a typo
  cannot silently run upstream's search while the operator believes a budget is set.
- **A multi-NodePool cluster.** Upstream's single-node scan interleaves NodePools in Go map order, so upstream itself
  is nondeterministic there; the adapters inherit that, and the parity test compares against the set of decisions
  upstream makes over repeated runs (consolidation-pipeline.md, Parity, Upstream's own nondeterminism). A budget does
  not change this.
- **A pass with no candidates.** The candidates stage records a summary with `candidates: 0` and the pass ends, as
  today.

## Alternatives Considered

### Alternative 1: Keep the time deadline only

Leave `MultiNodeConsolidationTimeoutDuration` and `SingleNodeConsolidationTimeoutDuration` as the only bounds. This is
today. It has no cost and no benefit: decisions keep depending on the machine, the one-minute limit keeps binding on
constrained clusters (the one such run that finished had a 52.3 s pass; RESULTS.md, H7, Scaling), and there is still
no way to say how much work a pass did. Rejected because the budget is a strict addition: with the budget unset the
deadline is all there is, exactly as today.

### Alternative 2: Keep one log line per command (today's logging)

Add fields to the existing "disrupting nodes" log line and the `Unconsolidatable` events instead of a structured
per-move record. This cannot answer "why not" for a node the search never probed, cannot show the probes a search made
before it accepted a command, and cannot carry per-pod planned destinations. The paper's section 7 analysis, which
needed per-pass attempts, simulations and moves, is not possible from it. Rejected; the decision log subsumes it and
its default recorder is a no-op.

### Alternative 3: A full strategy plugin API now

Expose the stage interfaces as a supported extension point, with the research branch's registry open to out-of-tree
strategies, so operators can ship their own generators and planners. The experiments argue against doing this yet: no
strategy other than upstream's beat upstream on cost, disruption and compute together (Non-Goals), the interfaces
changed during the research (the `Guide` seam, the moves mode, the pod-eviction budgets were all added as the
strategies needed them), and a plugin API would freeze them. Rejected for now. The seams ship, but the only registered
strategies are the adapters, and the registry is not a public API. Open Question 2 asks whether the recorder, which is
the stable part, should be.

### Alternative 4: Budgets without the seams

Thread a `SearchBudget` into the two upstream loops (the observer of How It Works) and stop there: no pipeline method,
no stage interfaces, no snapshot. This is the smallest change that makes the search machine-independent and counted,
and it is a strict subset of the proposal. What it loses is the decision log's structure (a record per stage rather
than a record per simulation), the parity test that pins the adapters, and the ability to compare a different search
or acceptance rule at equal compute, which needs a seam to put the alternative behind. Rejected on its own, but
accepted as the first commit of the proposal: the observer in the upstream loops is the same code either way.

### Alternative 5: Ship the fine adapters instead of the coarse ones

The research branch's fine adapters re-implement the multi-node binary search and the single-node scan in stages and
consult the budget and the planner at every step; they pass the same 10,000-state parity test and were the
configuration checked over whole runs and on KWOK (B4). They make the budget bind without touching upstream's loops,
and they make the planner swappable. Their cost is a second copy of the two searches that every upstream change must
be mirrored into, with the parity test as the only thing that catches a drift, and they drop Balanced's metrics and
events because the fine planner mirrors the evaluator rather than calling it (pipeline-refactor.md, section 5). Not
chosen for alpha, where the lower-risk path is upstream by construction, but kept as Open Question 3: if maintainers
would rather not add an observer to the upstream loops, the fine adapters are the alternative with the evidence behind
it.

## Backward Compatibility

- **Gate off (the default).** `disruptionMethods` in `pkg/controllers/controllers.go` returns no options when the gate
  is off, so `NewController` builds exactly today's methods; the pipeline packages are compiled in but never
  constructed, and the observer in the two upstream loops is nil (consolidation-pipeline.md, Backward Compatibility;
  pipeline-refactor.md, section 7: with the gate off, no code path changes). This is by construction, not by
  measurement: the KWOK runs with the gate off (B2, B3) checked that only multi-node and single-node evaluations
  appeared in the controller's metrics (TIERB.md, Validity), which confirms the wiring, not byte-identical behavior.
  <!-- unsourced as a measurement: no test compares gate-off output byte for byte -->
- **Gate on, flag empty.** The decisions are today's, as the parity test checks on 10,000 states and the whole-run and
  KWOK comparisons bound. The visible differences: `consolidation_type="pipeline"` appears on
  `decision_evaluation_duration_seconds`, `decisions_total`, `decisions_by_nodepool_total` and `queue_failures_total`,
  while `consolidation_timeouts_total` and `failed_validations_total` keep `multi` and `single`
  (consolidation-pipeline.md, Observability). Dashboards that filter on `consolidation_type` need the new value. Open
  Question 5 of the pipeline RFC (report the winning upstream method's label instead) is inherited.
- **Gate on with a budget or a recorder.** New behavior by definition: the search may stop earlier than today's
  deadlines would stop it, and log volume rises at V(2). No node, NodeClaim or pod is touched differently.
- **Rollback.** Turning the gate off restores today's methods at the next start. Nothing is left on cluster objects.
- **Configuration.** The flag is new, so existing deployments are unaffected. There is no CRD, NodePool or annotation
  change. Configuration errors fail at startup, never silently.
- **The decision log schema** is versioned from 1; version 2 adds `bindings`. Consumers read `schema_version` first.

## Graduation Criteria

**Alpha (this RFC).** The `ConsolidationPipeline` gate, default off. An empty flag selects the coarse adapters with no
budget and no recorder. The supported configurations are: the default; a `search` block; a `recorder` block. No other
strategy name is registered in the operator. Too risky to enable by default because the observer in the upstream loops
is new code on the hot path, the metrics are new, and the log volume at V(2) is unmeasured on large clusters.

**Beta.** All of the following, each checkable:

1. The parity test runs in CI: the 40-state run in `make test` on every change (exists), and the 10,000-state run
   (about 3.5 hours at the 1,000-state run's rate of 21 minutes; consolidation-pipeline.md, Parity) on a schedule,
   with no unexplained mismatch.
2. A soak on KWOK: the gate on with the default configuration against the gate off, on the Tier B harness
   (`experiments/tierb/`), for at least 30 seeds, with no resolved difference in end cost, evictions or commands
   decided, where resolved means a paired 95% interval that excludes zero. Thirty is the seed count paper.md, section
   9, estimates for resolving a 2% difference in end cost at 200 nodes, from the ±4.7% median interval at five seeds;
   at five seeds the B4-versus-upstream interval on end cost was ±2.9% at 200 nodes (RESULTS.md, Tier B). <!-- the
   30-seed threshold is a proposal derived from that estimate, not a measured requirement -->
3. The decision log schema is documented in `designs/` with its version, and a change to it bumps the version in the
   same PR.
4. The metrics above reviewed by the SIG, with `consolidation_search_stopped_total` and
   `consolidation_moves_considered_total` at minimum, and their `metrics.Label` definitions in `pkg/metrics` or the
   disruption package.
5. At least one real-cluster run of the gate on against the gate off, following the EKS plan in
   `docs/consolidation/kwok-experiments-and-eks-plan.md` (the replication there is estimated at about $1,100 of
   on-demand instances; paper.md, section 9).
6. Open Questions 1, 3 and 4 below resolved.

The gate still defaults off at beta; nothing in this RFC changes a default.

**GA.** Out of scope. It would need production feedback on the budget and the recorder, a decision on whether the two
upstream methods are retired behind the adapters, and a decision on asynchronous generation, none of which this RFC
takes up.

## Open Questions

1. **Should search budgets be per NodePool?** The budget proposed here is per pass, which matches where the search
   runs (one multi-node search over all candidates, one single-node scan interleaving NodePools). A per-NodePool
   budget would let a large pool be searched harder than a small one, but the multi-node prefix search does not
   partition by pool, so it would need the fine adapters or a change to the search. Proposed: per pass at alpha;
   revisit with field data from `consolidation_search_stopped_total`.
2. **Should the `Recorder` be a public extension point?** It is the stable part of the design (one method, one
   versioned event type) and the part operators would most plausibly want to plug (a metrics pipeline, an audit sink).
   Making it public means the event schema is an API. Proposed: keep it internal at alpha with the three built-in
   sinks, and decide at beta with the schema documented.
3. **Coarse adapters with an observer, or the fine adapters?** How It Works chooses an observer in the two upstream
   loops so that the budget binds under the coarse adapters. The fine adapters are the alternative with the
   measurements behind them (Alternative 5). Which do maintainers prefer to own: a small change inside the two
   searches, or a second copy of them pinned by a parity test?
4. **Should the pipeline-level gate replace `IsConsolidated`?** The pipeline has a cluster-state gate that skips a
   pass when nothing changed since a pass that found nothing and was not budget-limited; with the upstream adapters it
   defers to `IsConsolidated` instead, so searches per hour are unchanged. The decision logs show that the multi-node
   adapter counts itself as budget-limited almost always under a 10% budget, so it re-searches on 99 to 100% of passes
   and most of those searches execute nothing (paper.md, section 7, item 1). That is upstream's behavior today, and it
   is where a large share of upstream's decision CPU goes. Replacing it would change how often the search runs, which
   is behavior, not instrumentation, so it is out of this RFC; the metrics proposed here would measure it first.
5. **What does the per-move record contain under the coarse adapters?** With the observer, each multi-node probe (one
   prefix, one simulation) and each single-node probe is one `moves` event with the upstream `Command` it produced or
   the reason it was rejected. The planner's `plan` event then records the evaluator's verdict. Whether the probe's
   full placement (every pod's planned node) belongs in the log at V(2), or only in the file sink, is a volume
   question for beta.
6. **The research branch's fine adapters, strategies and simulator.** They stay where they are, on the research branch
   and in the simulator, and are not part of this proposal. Whether any of them comes upstream later depends on
   results the experiments did not produce (Non-Goals).

## References

- [Pluggable consolidation pipeline](consolidation-pipeline.md): the full design this RFC is a subset of; its Open
  Questions 1 to 10 are inherited where this RFC does not close them.
- [Balanced consolidation](balanced-consolidation.md): the issue list behind Use Case 3 and the open question on
  asynchronous generation.
- `docs/consolidation/pipeline-refactor.md`: the refactor for readers who know Karpenter's features, with the size,
  overhead and pros and cons tables cited above.
- `experiments/RESULTS.md`: the preregistered experiments (H1 to H7, ablations, B4, Exp 8) and the Tier B KWOK replay,
  with every number cited here.
- `paper/paper.md`: sections 6 (results), 7 (why random generation costs more CPU, from the decision logs), 8
  (discussion) and 9 (next steps).
- `experiments/TIERB.md`: the KWOK protocol, whose validity rule checks which methods decided with the gate on and
  off.
- `experiments/EXP9.md`: the pending early-stop and biased-selection experiment.
- Code on the research branch: `pkg/controllers/disruption/pipeline` (interfaces, adapters, method, registry, parity
  test), `pkg/controllers/disruption/pipeline_hooks.go`, `pkg/controllers/controllers.go` (`disruptionMethods`),
  `pkg/operator/options/options.go`, `hack/consolidation-sim`, `test/suites/performance/`.
