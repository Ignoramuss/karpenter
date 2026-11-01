# Bounded Search Budgets and a Decision Log for Consolidation

Give consolidation an explicit, counted search budget and a structured record of every move it considered, through
stage interfaces that reproduce today's decisions exactly, behind an off-by-default alpha feature gate.

This RFC is the upstream-facing subset of the [pluggable consolidation pipeline](consolidation-pipeline.md) that the
research branch built and measured. The research branch is the branch this document lives on (today
`claude/new-session-03ukbe` on the fork). It carries the full pipeline (`pkg/controllers/disruption/pipeline`), an
in-process simulator (`hack/consolidation-sim`), a KWOK suite (`test/suites/performance/`), and the experiment
write-ups and raw runs under `experiments/` and `paper/`; "the branch" below always means it. "The session" is
RESULTS.md's name for the branch's main experiment: 1,551 simulator runs whose hypotheses and tests were fixed before
the runs, on one 4-vCPU virtual machine at a time (experiments/RESULTS.md, What ran). The pipeline design document
holds the full design (interfaces, adapters, shift moves, potentials, planners). This one proposes only the three
parts of it whose value the experiments established, and cites the rest as evidence. Where the two documents overlap,
this one wins on scope and the other on mechanism.

## Motivation

Karpenter's consolidation runs as two disruption methods, `MultiNodeConsolidation` and `SingleNodeConsolidation`. Each
one orders candidates, generates moves, decides whether to accept them and validates them inside a single
`ComputeCommands` call (consolidation-pipeline.md, Motivation). Three properties of that structure are the problem
this RFC addresses.

**The search is bounded by time, not by work.** Multi-node consolidation binary-searches over prefixes of the
savings-ratio-sorted candidates that survive the NodePool-budget pre-filter, with a window capped at 100
(`lo.Clamp(len(disruptableCandidates), 0, 100)`, so a prefix holds at most 101 candidates), and stops after one minute
of wall-clock time (`MultiNodeConsolidationTimeoutDuration`, a `context.WithTimeout`). Single-node consolidation walks
the candidates until three minutes have passed on the injected clock (the controller's `clock.Clock`: real time in
production, a fake clock in tests and the simulator, which is what makes those runs reproducible). So what a pass
finds depends on how fast the machine is and on what else it is running: a busy controller finds fewer consolidations
than an idle one from the same cluster state. It also means two search strategies cannot be compared at equal compute,
because neither reports how much work it did (consolidation-pipeline.md, Motivation, consequence 2). A full
single-node scan at 1,000 nodes was estimated at about 58 s on the 4 vCPU research VM, 864 candidates at 67 ms per
scheduling simulation (consolidation-pipeline.md, Motivation, consequence 2).

**Rejected alternatives leave no record.** The controller logs the command it starts, emits `Unconsolidatable` events
for candidates and records metrics for scored moves. It keeps no structured record of every move it considered and why
each one lost (consolidation-pipeline.md, Motivation, consequence 4). An operator who asks why a node was not
consolidated, or why the cluster has held a fragmented fleet for an hour, has no artifact to look at. The reports
behind the Balanced RFC show what such an artifact would have to answer (balanced-consolidation.md, Motivation):

- Nodes at 93-99% CPU utilization disrupted instead of lightly utilized ones
  ([aws#8868](https://github.com/aws/karpenter-provider-aws/issues/8868),
  [kubernetes-sigs#2319](https://github.com/kubernetes-sigs/karpenter/issues/2319)). The log would show which lightly
  utilized candidates were considered in that pass and why each lost.
- Multi-hour consolidation loops replacing the same instance types with no net savings
  ([aws#8536](https://github.com/aws/karpenter-provider-aws/issues/8536),
  [aws#6642](https://github.com/aws/karpenter-provider-aws/issues/6642),
  [aws#7146](https://github.com/aws/karpenter-provider-aws/issues/7146)). The log would show each pass's predicted
  savings for the replacement.
- Rapid node churn where consolidation deletes nodes that are immediately re-provisioned
  ([kubernetes-sigs#1019](https://github.com/kubernetes-sigs/karpenter/issues/1019),
  [kubernetes-sigs#735](https://github.com/kubernetes-sigs/karpenter/issues/735),
  [kubernetes-sigs#1851](https://github.com/kubernetes-sigs/karpenter/issues/1851)). The per-pod planned destination
  would show whether the plan was ever feasible.

Today's log line names only the command that ran; the `Unconsolidatable` events name single-node candidates that lost
and a one-line reason, but not the multi-node prefixes tried, the predicted savings, or where each pod was planned to
go.

**The stages cannot vary independently.** Candidate order, move generation, acceptance and validation are interleaved
in one loop per method, so none can be changed or measured on its own, and the Balanced evaluator is called from
inside both loops (consolidation-pipeline.md, Motivation, consequence 1). Every improvement to consolidation,
including the Balanced policy itself, has had to be threaded through those loops.

The research branch addressed all three with a pluggable pipeline, then ran a set of experiments whose hypotheses and
tests were fixed before the runs (experiments/RESULTS.md) and replayed part of them on a KWOK cluster
(experiments/TIERB.md). The experiments did not find a new strategy that beats upstream's search (see Non-Goals). What
they did establish is that the instrument works: the pipeline's upstream adapters reproduce upstream's decisions
exactly, its search budgets make strategies comparable at equal compute, and its decision log answers questions the
controller cannot answer today. That instrument is what this RFC proposes to carry upstream.

### Use Cases

1. **A per-pass compute ceiling that does not depend on the machine.** An operator running 1,000 or more nodes wants
   consolidation to spend at most, say, 16 scheduling simulations per pass, whatever the controller's CPU allocation,
   and wants a metric that says when that ceiling was hit. Today the ceilings are the one-minute wall-clock deadline
   on the multi-node search, the three-minute limit on the single-node scan (on the injected clock, real time in
   production) and the 100-candidate window; the two time limits bind differently on every machine, and only the
   window is a count.
2. **Evaluating a change to the search or the acceptance rule at equal compute.** A maintainer wants to compare the
   current prefix search against a variant (a different candidate order, a different `k` for Balanced, a different
   window) and needs both to run under the same simulation budget, with a record of what each considered, so the
   comparison measures the strategy and not the machine. The comparison runs in the simulator and the KWOK suite on
   the research branch, where the fine adapters and the experimental strategies live; the research branch did exactly
   this across its preregistered experiments, 1,551 simulator runs on one 4-vCPU VM at a time (RESULTS.md, What ran).
   What this RFC gives the controller is the same budget counters and the same log format, so field data and simulator
   data are comparable.
3. **Explaining a non-decision.** An operator asks why node X, at 30% utilization, has not been consolidated for an
   hour. The decision log shows every probe that touched X in each pass, its predicted savings, and the reason it
   lost: `no-op` (the simulation found no cheaper placement), `replacement not cheaper` (`filterOutSameInstanceType`
   left no instance type), `evaluator rejected` (Balanced), `nodepool budget` (skipped by its NodePool's budget),
   `below threshold` (skipped by Balanced's `CanPassThreshold` pre-filter), `validation: churn`, or an `unreached`
   count in the round summary, the candidates the budget cut off before the scan got to them.
4. **Measuring plan fidelity in the field.** A consolidation command is a bet that evicted pods will land where the
   simulation put them. The research branch measured that bet in the simulator: under default kube-scheduler scoring,
   upstream's own commands landed on plan for 45% of evicted pods (paper.md, section 6.5). Nothing in the controller
   measures it on a real cluster. A per-pod planned destination in the decision log is the half of that measurement
   the controller can supply; pairing it with where the replacement pod bound is Open Question 8.

### Non-Goals

- **No new default consolidation strategy.** With the gate on and no configuration, the pipeline makes the same
  decisions as today's two methods; with the gate off, nothing changes. No strategy other than the upstream adapters
  is proposed for production.
- **No shift moves, potentials or entropy-guided planners in Karpenter.** A potential, in the research branch's sense,
  is a function of fleet shape the research planners used to value moves that delete no node. The branch's
  `random-merge`, `random-shift`, `greedy-shift`, `entropy-greedy` and `anneal-plan` stay in its `strategies` packages
  and the simulator. The `util-entropy`, `load-entropy` and `util-quadratic` potentials are registered by package
  `pipeline` itself (`registry.go`), not by a `strategies` package; this RFC drops the potential registry and
  hard-wires `none`. They are evidence for the seams, not a proposal. Specifically, the experiments did not show that
  random or entropy-guided generation beats upstream: random generation was equivalent to upstream's search within 3%
  in only 3 of 12 cells and ended 1.4-4.3% dearer after a diurnal scale-down (RESULTS.md, H1); shifts cost 1.7-7.0%
  more than merge-only at an equal eviction budget (RESULTS.md, H3); annealing bought 3.0% of cost for 37.7% more
  evictions on one workload (RESULTS.md, H4); the pipeline's savings-versus-disruption frontier did not dominate
  upstream's Balanced policy (RESULTS.md, H6); on the KWOK cluster (RESULTS.md, Tier B, the KWOK replay) random
  generation ended 10.6% dearer than heuristic generation with the same planner at 200 nodes and used 52% more
  controller CPU; and neither tier established an order among the five configurations within a few percent of upstream
  (a difference is resolved when its paired 95% interval excludes zero): the simulator resolved none of their cost
  differences at five seeds, and the seven the cluster resolved at 200 nodes, among them heuristic/anneal-plan dearer
  than upstream by 4.2% on end cost, the simulator did not (RESULTS.md, Tier B).
- **No change to NodePool API fields**, to the semantics of disruption budgets, `consolidateAfter`,
  `consolidationPolicy` or any annotation users set.
- **No asynchronous or continuous generation.** All stages still run inside one `ComputeCommands` call per pass. The
  Balanced RFC's open question on asynchronous generation stays open; the seams are its prerequisite, not its answer.
- **No change to Emptiness, Drift, StaticDrift, Repair, the orchestration queue, termination or drain.**

## Proposal

With `--feature-gates ConsolidationPipeline=true`, the disruption controller runs one consolidation method,
`PipelineConsolidation`, where multi-node and single-node consolidation used to run. The method composes four stages
(candidates, moves, plan, execute) around shared services, and ships with only the coarse upstream adapters, one per
stage and two for moves (`upstream-multi`, `upstream-single`), which call today's `ComputeCommands` and convert the
result. The research branch has two forms of adapter. *Coarse* adapters wrap each upstream method's `ComputeCommands`
whole, so upstream's search, acceptance, validation, metrics and events run unchanged and the adapter only converts
the result. *Fine* adapters re-implement the two searches stage by stage so the acceptance rule can be swapped. This
RFC ships the coarse form only; the fine form is Alternative 5 and Open Question 3. Three things are new to the user:

1. **A search budget.** `search.maxFullSimulations` caps scheduling simulations per pass, `search.maxAttempts` caps
   generation attempts per generator, and `search.timeout` is a deadline on the injected clock; in production it is a
   wall-clock limit like today's, checked between simulations rather than cancelling one part-way. `maxAttempts` and
   `maxFullSimulations` default to 0 and `timeout` to empty; each means no pipeline-level bound, leaving the adapters'
   own one- and three-minute limits as today. When set, the search stops at the budget or at today's one- and
   three-minute deadlines, whichever comes first; the observer described in How It Works adds a `UseFull()` check to
   upstream's loops and removes neither limit. Whenever the budget binds first, what a pass finds no longer depends on
   the machine's speed: the same cluster state gives the same decision on any machine, up to upstream's own NodePool
   interleaving (Edge Cases, A multi-NodePool cluster), and a strategy's cost is counted, not only timed;
   `consolidation_timeouts_total{consolidation_type="multi"|"single"}` shows the passes where a deadline bound
   instead. `maxFullSimulations` counts the search's simulations only. The validation simulation that follows an
   accepted command (at most one per method per pass) is not counted and is never refused, so a pass runs at most
   `maxFullSimulations` plus one validation simulation per method.
2. **A decision log.** Every stage decision is one structured, schema-versioned event: the candidates considered, each
   probe the search made with its predicted savings and disruption, the planner's verdict and reason, and the
   executor's outcome, plus a per-pass summary of attempts, simulations and moves. A `Recorder` interface receives
   them. This RFC makes the default a no-op (today the branch always installs the `log` recorder, which writes every
   event at V(1)), splits the `log` recorder into round summaries at V(1) and per-move events at V(2), and adds a
   `file` recorder that writes JSON lines.
3. **Stage seams behind the gate.** The `CandidateSelector`, `MoveGenerator`, `Planner` and `Executor` interfaces, the
   `Snapshot`, `Feasibility`, `Scorer` and `SearchBudget` services, and a registry that selects each stage by name
   from configuration, with an allowlist that admits only the adapters below. A differential parity test holds the
   adapters to upstream's decisions.

### Proposed Spec

**Feature gate and flag** (`pkg/operator/options/options.go`; consolidation-pipeline.md, Proposed Spec):

| Item | Value |
| --- | --- |
| Feature gate | `ConsolidationPipeline`, an `options.FeatureGate` var in `KarpenterFeatureGates`, alpha, default `false` |
| Flag | `--consolidation-pipeline-config`, env `CONSOLIDATION_PIPELINE_CONFIG`, default empty |
| Flag value | A file path, or the configuration inline. A value that starts with `{` or `pipeline:`, or spans more than one line, is parsed inline as YAML or JSON; any other value is read as a file path (`LoadConfig`). A one-line bare form such as `search: {maxFullSimulations: 16}` is therefore read as a path and fails at startup; wrap it in `pipeline:` or write it on more than one line |
| Empty value | The upstream adapters, no budget, no-op recorder: today's decisions |
| Invalid value | Karpenter fails at startup: with the parse or validation error for inline or file content, or with the file-read error when the value is read as a path that cannot be read (the one-line bare form above included) |

**Configuration schema** (complete for this RFC):

```go
type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"`
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Search     SearchConfig `json:"search,omitzero"`
	Recorder   StageConfig  `json:"recorder,omitzero"` // new in this RFC: none (default), log, file
}

type StageConfig struct {
	Name   string         `json:"name"`
	Params map[string]any `json:"params,omitempty"` // decoded strictly by the strategy
}

type StageList []StageConfig // one object or a list

type SearchConfig struct {
	MaxAttempts        int    `json:"maxAttempts,omitempty"`        // generation attempts per generator; 0 = no bound
	MaxFullSimulations int    `json:"maxFullSimulations,omitempty"` // scheduling simulations per pass; 0 = no bound
	Timeout            string `json:"timeout,omitempty"`            // deadline on the injected clock, e.g. "1m"; empty = none
}
```

Each stage's `params` are decoded strictly: an unknown key or an out-of-range value fails construction, and so does an
unknown strategy name. These are the only top-level keys. An unknown top-level key, including the research branch's
`movesMode`, `potential`, `seed` and `budgets` (its `Config` has all four; this RFC drops them), fails at startup in
both the bare and the `pipeline:`-wrapped form, as the branch's `ParseConfig` already does for keys it does not know.

**The allowlist.** The branch's registry accepts every name any imported package registered, and package `pipeline`
itself registers more than the adapters: the `savings-only` and `balanced` planners, the `mode: fine` parameter of
both move generators, and the `load-entropy`, `util-entropy` and `util-quadratic` potentials. Dropping the
`strategies/all` import from `pkg/controllers/controllers.go` therefore does not narrow the registry enough. This RFC
validates the configuration at startup against an allowlist: candidates `upstream`; moves `upstream-multi` and
`upstream-single` with `mode: coarse` only (`fine` is rejected); planner `upstream`; executor `upstream-queue`;
recorder `none`, `log` or `file`. There is no `potential` key: the method always runs with the `none` potential. There
is no `movesMode` key: the generators always run in sequence.

**What ships in Karpenter** (the names the allowlist admits):

| Stage | Name | Role |
| --- | --- | --- |
| candidates | `upstream` | This pass's consolidation candidates, in upstream's savings-ratio order (`SortBySavingsRatio`); the coarse adapters hand upstream's `ComputeCommands` the matching candidates, which sorts them again |
| moves | `upstream-multi` | Calls `MultiNodeConsolidation.ComputeCommands` and converts the returned command to a move |
| moves | `upstream-single` | Calls `SingleNodeConsolidation.ComputeCommands`, the same way |
| plan | `upstream` | Re-runs the Balanced evaluation on the returned command (`EvaluateBalancedMove`, without the evaluator's metrics and events; reason `not accepted`) and re-checks it against the pass's budgets (`ExceedsBudgets`); a command upstream already approved passes both |
| execute | `upstream-queue` | Returns the already-validated command for the orchestration queue |
| recorder | `none`, `log`, `file` | Decision log sink (new: the branch's registry has no recorder kind and `Methods` hard-wires `LogRecorder`; this RFC selects the recorder by name from the `recorder` block). `file` takes `params` `path` (required; on a writable volume mounted into the controller pod) and `maxSizeMB` (default 64; one rotated file is kept) |

The research branch's `strategies/{candidates,moves,planners,executors}` packages, which hold every experimental
strategy, are not compiled into Karpenter. That is a change from the branch today, where
`pkg/controllers/controllers.go` imports `strategies/all` (consolidation-pipeline.md, Open Question 9). The fine
adapters are not there: they are the `mode: fine` parameter of `upstream-multi` and `upstream-single` in
`pkg/controllers/disruption/pipeline/adapters.go`, registered by package `pipeline` itself, which is why the allowlist
rejects the mode rather than relying on the import. The strategies stay compiled into the simulator and the KWOK
suite, where they are tested.

**Stage interfaces and services** (abridged from `pkg/controllers/disruption/pipeline/interfaces.go`; the full block,
with the `Guide` and `IdleExecutor` interfaces this RFC does not ship, is in consolidation-pipeline.md, Stages and
shared services):

```go
// Snapshot is an immutable, cheaply forkable view of cluster state used for planning.
type Snapshot interface {
	Nodes() []NodeView                // every node, sorted by name
	Node(name string) (NodeView, bool)
	Pods(node string) []PodView       // sorted by key
	Load(node string) Resources
	Apply(m Move) (Snapshot, error)   // copy-on-write
	Totals(nodePool string) Totals    // Balanced's totals, fixed for the cycle
	Normalizer() Normalizer           // fleet constants of the root snapshot, inherited by forks
	PDBAllowed(pdb string) int        // -1 if unknown
}

type CandidateSelector interface { // stage 1
	Name() string
	Select(ctx context.Context, s Snapshot, rng *rand.Rand) (sources, destinations []NodeView, err error)
}

type MoveGenerator interface { // stage 2
	Name() string
	Generate(ctx context.Context, s Snapshot, sources, destinations []NodeView, rng *rand.Rand, b *SearchBudget) ([]Move, error)
}

type Planner interface { // stage 3
	Name() string
	Plan(ctx context.Context, s Snapshot, moves []Scored, budgets Budgets, rng *rand.Rand) (Plan, error)
}

type Executor interface { // stage 4
	Name() string
	Execute(ctx context.Context, p Plan) (Report, error)
}

type Feasibility interface {
	Fast(s Snapshot, m Move) error                              // snapshot only
	Full(ctx context.Context, s Snapshot, m Move) (Move, error) // Karpenter's scheduling simulation
}

type Scorer interface {
	Score(s Snapshot, m Move) Scored
}

type Recorder interface{ Record(e DecisionEvent) }

// SearchBudget caps one pass's search. Attempts are counted per fork (one per generator); full simulations and
// the deadline are shared by every fork of one budget.
type SearchBudget struct {
	MaxAttempts int        // 0 means no bound
	MaxFull     int        // 0 means no bound
	Deadline    time.Time  // zero means no deadline
	Clock       clock.Clock
	// ...
}
```

**A configuration that bounds the search and logs decisions:**

```yaml
pipeline:
  search:   {maxFullSimulations: 16, timeout: 1m}
  recorder: {name: log}
```

Everything not named takes its default, so this runs upstream's own search, stopped after 16 scheduling simulations in
the pass (the multi-node search and the single-node scan together, under the per-pass budget of How It Works), at one
minute on the injected clock, or at upstream's own deadlines, whichever comes first, and logs what it considered.
Validation simulations are outside the 16.

**Decision log schema** (version 1, as the branch writes it; bump on any field change; consolidation-pipeline.md,
Decision log):

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `run_id` | string | `"controller"` in Karpenter; the run ID in the simulator |
| `cycle` | int | Pass counter: incremented once per `Method.ComputeCommands` call, so it numbers this method's passes since the controller started (the pipeline RFC's cycle; a round is one generator's turn within a pass) |
| `t` | float | Seconds since the method started, from the injected clock |
| `stage` | string | `candidates`, `moves`, `plan`, `execute` |
| `strategy` | string | Strategy name; generators joined with `+` on a round summary |
| `move_id` | string | Unique per strategy instance, for example `multi-3`; empty on summaries |
| `kind` | string | Always `merge` in this RFC (upstream's delete and replace both map to it; `detail.launch` is 0 for a delete and 1 for a replace). `shift` and `rightsize` are reserved for the simulator, whose upstream arms also write `delete` and `replace` (upstream's `Decision()`) on their `execute` events, with `strategy` `multi`, `single` or `empty` |
| `sources`, `destinations` | []string | Sources in upstream's candidate order; destinations sorted |
| `pods` | int | Bindings (on the candidates summary: candidate count) |
| `delta_potential` | float | Always 0 in this RFC; kept for schema compatibility with the simulator, where it is the change in a research potential |
| `disruption`, `savings_per_hour` | float | The move's scores |
| `accepted` | bool | The stage's verdict |
| `reason` | string | Why not. With the observer of How It Works: `no-op`, `replacement not cheaper`, `evaluator rejected`, `nodepool budget`, `below threshold`, `validation: budget|scheduling|churn`. From the pipeline's own stages under the upstream adapters: `not accepted`, `not selected`, `node budget for default: 2 > 1`. The branch's `not downhill` and `conflicts with m4` come from planners that do not ship |
| `budget_remaining` | int | Simulations left in the pass after this event. Reserved on the branch today and never set (consolidation-pipeline.md, Open Question 10), so version 1 always carries 0; from schema version 2 (the version that adds `bindings`) this RFC sets it on every event: the count, or -1 when `maxFullSimulations` is 0 |
| `detail` | map[string]float | Stage-specific numbers. Candidates summary: `sources`, `destinations`, `candidates`. Round summary: `moves`, `attempts`, `full_simulations`, `timed_out`, `exhausted`, and with the observer `unreached`, `budget_skipped` and `threshold_skipped`. Per move: `launch`, `remove`, `energy` (always 0 here: only the research planners, which do not ship, compute it; the `none` potential zeroes `delta_potential` only), `norm_disruption`, `norm_savings` |

A move's `bindings` (pod, from, planned destination) are not in the version 1 schema; a per-pod planned destination is
what Use Case 4 needs, so this RFC adds an optional `bindings` field in the `file` sink as schema version 2; whether
it also goes to the `log` recorder at V(2) is Open Question 5. Plan fidelity itself is not a metric in this RFC:
pairing a planned destination with where the replacement pod bound needs a watcher on pod bindings, owner-based
matching (the replacement is a different pod), and a mapping from a replace command's launch index to the NodeClaim
the queue creates. `bindings` in the `file` sink are the controller's half of that measurement, for offline analysis;
the metric is Open Question 8.

### How It Works

A *source* is a node the pass may empty (a consolidation candidate); a *destination* is a node that may receive its
pods. A *full simulation* is one `SimulateScheduling` call; `Feasibility` wraps it behind an interface (its
snapshot-only `Fast` check also ships as part of the seam, but nothing in this RFC calls it); `Scorer` computes a
move's savings per hour and disruption. A *planner* is the stage that decides which generated moves run; the only
planner here is upstream's own acceptance, re-checked against the pass's budgets.

**One pass.** The method runs where multi-node consolidation ran, with the same candidates and budgets `disrupt()`
computes today (consolidation-pipeline.md, How It Works, One cycle):

```
disrupt() ─► Method.ComputeCommands(budgets, candidates)
               ├─ BuildSnapshot                                      ─► nodes, pods, PDB allowances, totals
               ├─ one SearchBudget for the pass (this RFC; see The budget)
               ├─ stage 1  selector.Select(snapshot)                 ─► sources, destinations
                              ├─ for each generator, in order (one round each)
               │    ├─ (rounds after the first) refresh candidates, totals and budgets; BuildSnapshot and Select again if the clock moved
               │    ├─ stage 2  g.Generate(..., budget.Fork())       ─► moves
               │    ├─ stage 3  planner.Plan(snapshot, moves, budgets) ─► plan
               │    ├─ stage 4  executor.Execute(plan)               ─► commands
               │    └─ a round that executes anything ends the pass
               └─ record a DecisionEvent at every stage
disrupt() ─► Queue.StartCommand for each returned command (unchanged)
```

The method runs the two generators in sequence (the research branch also has a `pooled` mode that plans over all
generators' moves at once; it is not in this RFC and `movesMode` is not a configuration key): each generator is its
own round, and the next round runs only if the previous one executed nothing, which is how the controller runs
multi-node and then single-node consolidation today. Before that next round, whenever the injected clock has moved
since the snapshot was built (after any work at all under the real clock), the method re-fetches candidates, totals
and budgets as `disrupt()` does per method (`refresh` in `method.go`) and rebuilds the snapshot and the selection, so
the single-node round sees the cluster as it is after a failed validation wait; this is the second `BuildSnapshot` of
Open Question 7. The method keeps upstream's own skip rule: with the upstream adapters it uses `IsConsolidated` per
method rather than the pipeline's cluster-state gate.

**The coarse adapters.** `upstream-multi` and `upstream-single` call the upstream methods' own `ComputeCommands`, so
upstream's search, acceptance, Balanced metrics, events and validation all run as today, and the adapter only converts
the returned `Command` into a `Move` and back (consolidation-pipeline.md, Upstream adapters). `NewUpstream` builds one
`MultiNodeConsolidation` and one `SingleNodeConsolidation` with the constructors `NewMethods` uses, and both adapters
share them through `Env.Upstream`, so `lastConsolidationState`, `PreviouslyUnseenNodePools` and the validators behave
the same. This mode is upstream by construction, and it is the only mode this RFC ships.

**The budget.** A `SearchBudget` holds `MaxAttempts`, `MaxFull` and the deadline. Generators call `Next()` once per
attempt and `UseFull()` before each scheduling simulation; attempts are counted per generator (`Fork()`), and
`Stats()` reports usage to the decision log (consolidation-pipeline.md, Search budget). On the branch today
`Method.round()` builds a fresh `SearchBudget` for every round (`m.searchBudget()` calls `NewSearchBudget`), so with
the two generators in sequence the multi-node round and the single-node round each get their own `maxFullSimulations`
and their own deadline, and a pass may spend up to twice the cap; full simulations are shared among generators only
within one round. This RFC moves that construction into `Method.ComputeCommands`: one budget per pass, forked per
round, so the cap and the deadline cover the whole pass as the example configuration and the Edge Cases assume, with a
test for it; each round's summary keeps reporting that round's own attempts and simulations. That is a code change on
the branch, listed with the others below.

**Why it cannot bind today.** On the research branch the budget is consulted by the fine adapters, which re-implement
the two searches, not by upstream's own loops. Under the coarse adapters a budget therefore has nothing to bind:
`upstream-multi` and `upstream-single` hand the whole search to `ComputeCommands`, and the only `moves` event the log
gets is the command that comes back.

**The observer (new; not on the branch).** This RFC adds one small interface in the disruption package
(`pipeline_hooks.go`), which the pipeline package implements, since `pipeline` imports `disruption` and not the
reverse, so the hook cannot name `pipeline.DecisionEvent`:

```go
// SearchObserver is consulted by the two consolidation searches when the consolidation pipeline drives them.
type SearchObserver interface {
	Next() bool              // one probe is about to run; false stops the search
	UseFull() bool           // one scheduling simulation is about to run; false stops the search
	Record(p SearchProbe)    // the probe's result
}

// SearchProbe is one simulated prefix (multi-node) or candidate (single-node): the command it produced, if any,
// and why it was not accepted.
type SearchProbe struct {
	Candidates []*Candidate
	Command    Command
	Reason     string // "", "no-op", "replacement not cheaper", "evaluator rejected", "nodepool budget", "below threshold", "validation: ..."
}
```

A `SetSearchObserver(SearchObserver)` method, exported from the same file and stored on the `consolidation` struct
both methods embed, is called by the coarse adapters before each `ComputeCommands` with the pass's budget fork and
`Env.Record`, so `ComputeCommands`' signature is unchanged; the observer is nil with the gate off, so the loops are
unchanged there. `firstNConsolidationOption` calls `Next()` and `UseFull()` before each `computeConsolidation` and
stops the search when either is refused, keeping the best prefix found so far exactly as it does when the one-minute
context expires; it calls `Record()` after the evaluator's verdict. The single-node scan does the same per candidate
it simulates. One `Next()` is one probe in the binary search and one simulated candidate in the scan. Candidates the
search skips are not attempts, but each is one `Record()` call with no command: the single-node scan records reason
`nodepool budget` or `below threshold` (`CanPassThreshold`) for each candidate it skips, and
`MultiNodeConsolidation.ComputeCommands` records `nodepool budget` for each candidate its budget filter drops before
`firstNConsolidationOption` runs; the round summary counts them as `budget_skipped` and `threshold_skipped`. The
validators' own `SimulateScheduling` is outside the budget, so whatever the search accepted is always validated;
`ComputeCommands` calls `Record()` once more with the validation verdict (`validation: budget|scheduling|churn`). A
budget stop does not increment `consolidation_timeouts_total`; `consolidation_search_stopped_total` counts it. The
pipeline's `SearchObserver` implementation converts each `SearchProbe` into a `DecisionEvent` and passes it to
`Method.record`, which stamps `schema_version`, `run_id`, `cycle` and `t` on it as it does for the round's own events;
the adapters reach it through a new `Env.Record func(DecisionEvent)` that `newMethod` sets to `m.record`, since `Env`
today carries only the raw `Recorder` sink and no handle on the `Method`. The header of `pipeline_hooks.go`, which
today says nothing in the file changes behavior, changes to say that with a nil observer nothing does.

Two further changes in the loops go with the observer. A search stopped by the budget before any accepted prefix sets
`constrainedByBudgets`, in both methods, so neither marks the cluster consolidated and the next pass searches again
with a fresh budget; today a multi-node deadline expiry with no accepted prefix goes through the no-op path, which
marks it whenever no NodePool budget held a candidate back (`if !constrainedByBudgets { m.markConsolidated() }`; rare
under a 10% budget, usual under a permissive one), and `IsConsolidated` then skips multi-node until the cluster state
changes. And the single-node scan under a budget resumes after the last candidate it reached, wrapping around (a
cursor on `SingleNodeConsolidation`, as the fine adapter's `resumeAfter` does: set when `Next()` or `UseFull()` is
refused (either refusal stops the scan, as above; the fine adapter checks only `UseFull()` because it never calls
`Next()`), to the candidate before the refused one whether that one was simulated or skipped, kept at its old value
when the refusal comes before any candidate, cleared when the next scan starts, not set on a deadline stop, and
ignored when the named candidate is no longer a candidate; a budget stop sets `PreviouslyUnseenNodePools` as a
completed scan does, again as the fine adapter does), so consecutive capped scans that find nothing walk on through
the candidate order instead of restarting at its head. This is a progress rule, not a coverage guarantee: a scan that
executes a command or stops on the deadline leaves no cursor, and with several NodePools the interleaving can change
between passes. Without it a binding budget would re-simulate the same leading candidates every pass and leave the
tail of the savings-ratio order unreachable, because the scan restarts from the top each pass and
`PreviouslyUnseenNodePools` reorders NodePools only, not candidates within a pool.

This is the one change on the hot path of both searches, and none of it exists on the branch yet: the risk is new code
inside the two loops that every consolidation pass runs. The mitigations are the nil observer with the gate off, which
leaves the loops as they are; the parity test, extended with budget-capped states, which must show that a capped
multi-node search returns a prefix the uncapped search also accepted, and that a capped single-node scan simulates a
contiguous run of the uncapped scan's candidate order starting after its cursor and gives each candidate it simulates
the verdict the uncapped scan gives it; and the fine adapters as the fallback design (Alternative 5), which bind the
budget without touching the loops.

**What "stop at the budget" means.** Under the per-pass budget, the multi-node search probes prefixes of the sorted
candidates until a probe is refused, then keeps the best prefix found so far; if that is nothing, the pass moves to
the single-node round with whatever budget is left. The single-node scan stops at the candidate it has reached and
records its cursor. In both cases the pass emits whatever was accepted before the budget ran out, validated, and the
round summary records `exhausted: 1` with the attempts and simulations this round itself spent (its own fork's counts,
not `Stats()` on the pass budget, which sums every fork's attempts and the shared simulation count; without that rule
the single-node round's line would carry the multi-node round's attempts and simulations, and `m.fullSims` would count
them twice) and `unreached` with the candidates the budget cut off before the search reached them (candidates the
search skipped on the way are counted under `budget_skipped` and `threshold_skipped` instead).

**The recorder.** `Recorder.Record(DecisionEvent)` is called synchronously at each stage. On the branch today the
controller always installs `LogRecorder`, a logr logger named `consolidation-pipeline` that writes every event at
V(1), and the simulator uses `JSONLRecorder` over an `io.Writer`, which is synchronous under a mutex, never drops an
event, does not rotate, and after its first encode error writes nothing more (the error is readable through `Err()`,
which nothing in the method calls). This RFC proposes: `none`, the default, discards. `log` writes the round summaries
at V(1) and per-move events at V(2) through the `consolidation-pipeline` logger with the message `consolidation
decision` and the event's fields as key-value pairs. `file` (new) is `JSONLRecorder` over a path, synchronous and
buffered, with size rotation; under load it slows the pass rather than dropping events, since a file that dropped
events would be incomplete for the pass it describes; a write error disables the recorder for the rest of the process
with one log line, and is never returned to the method. A recorder can never fail a pass: the method does not look at
anything the recorder returns. No Go map iteration order reaches a logged list and the JSON encoder sorts `detail`'s
keys, so in the simulator, where the clock is fake, there is one NodePool and `UpstreamOptions.OrderCandidates` fixes
candidate order, the same seed, configuration and commit produce byte-identical logs; the simulator's
`TestDeterminism` checks this on `JSONLRecorder` (consolidation-pipeline.md, Decision log). In the controller `t` is
real time and `cycle` counts passes, and with more than one NodePool the single-node scan order is Go map order (Edge
Cases), so two passes on the same state log the same decisions, not the same bytes.

**Code changes this RFC asks for**, beyond carrying the core `pipeline` package: (1) `SearchBudget` built once per
pass in `Method.ComputeCommands` and forked per round; (2) the `SearchObserver` hook in `firstNConsolidationOption`
and the single-node scan, with `SetSearchObserver` on `consolidation` and `Env.Record`; (3) `constrainedByBudgets` set
on a budget stop in both methods; (4) the single-node resume cursor; (5) the allowlist, the dropped configuration keys
and the rejection of `mode: fine`; (6) recorder selection by name, the V(1)/V(2) split and the `file` recorder; (7)
`budget_remaining` set on every event; (8) the metrics below.

### Evidence from the research branch

Labels below are RESULTS.md's. B2 is upstream with `WhenEmptyOrUnderutilized`, B3 upstream with `Balanced`, B4 the
pipeline's fine adapters configured as upstream (the upstream planner and queue, uncapped). W1 is a fleet that starts
fragmented, W2 a diurnal scale-down, W4 mixed pod shapes. Tier A is the simulator; Tier B is the KWOK-on-kind replay
of one compressed diurnal workload at 50 and 200 nodes, five seeds. H1 to H7 are RESULTS.md's hypothesis sections.
Full tables are under `paper/tables/` and are regenerated from the raw runs by `make figures`.

Everything below was measured on the research branch with the fine adapters (B4) or the experimental strategies. The
coarse configuration this RFC ships is covered by the 10,000-state parity test only; its whole-run and cluster
overhead are unmeasured.

**The adapters reproduce upstream's decisions.** `TestParity` runs upstream's two methods and then one pipeline cycle
with each adapter configuration on seeded cluster states (4 to 40 nodes, one to three NodePools, two instance
catalogs, both consolidation policies) and requires identical decisions: kind, candidates in order, replacement
instance-type options in order, budget consumed per NodePool. Results: 1,000 of 1,000 states matched per policy and
catalog, then 10,000 of 10,000 in every configuration (coarse, fine, and fine with the Balanced planner on Balanced
states) (RESULTS.md, What ran, Parity; docs/research/parity.md, Results), on states where upstream itself decided to
act in 83 to 91% of cases in the 10,000-state run (8,347 to 9,141 of 10,000 per chunk;
`experiments/parity/chunks/*.summary.txt`, `upstream_decided`). A 1,000-state run took 21 minutes
(consolidation-pipeline.md, Parity as the compatibility guarantee); three of the four 10,000-state chunks took 53 to
71 minutes each (docs/research/parity.md, Results); a 40-state run is in `make test`. Three injected bugs in the fine
adapters: dropping the multi-node budget decrement was caught (44 of 60 states matched), accepting every move was
caught on Balanced states (85 of 100), and skipping the `CanPass` pre-filter was not and cannot be, since it changes
compute but not decisions (consolidation-pipeline.md, Mutation check). That last point is why the search budget needs
its own metric and the decision log, not only the parity test.

**Whole-run and cluster parity were checked with the fine adapters, which this RFC does not ship.** The coarse
adapters proposed here have the 10,000-state single-cycle parity test only. The whole-run and KWOK comparisons below
ran the fine adapters (B4), which re-implement the same searches and pass the same parity test, so they are consistent
with the shipped path without exercising it. Over 3-hour simulated runs on three workloads at 200 nodes, B4 was within
-0.75% to +0.34% of upstream's steady-state cost, every 90% interval within -1.8% to +1.0%, all six equivalence tests
at 3% (tests that the difference lies within ±3%) passing, 7 of 60 seeds matching upstream exactly on four metrics
(RESULTS.md, B4). RESULTS.md offers the simulator's seeded candidate shuffle, which the pipeline's method boundaries
can consume differently from upstream's separate methods, as a possible cause of the other 53; whether it accounts for
them was not tested, and the decision-level parity test is the exact check. On the KWOK cluster at five seeds, every
Tier B interval for B4 against upstream included zero: end cost +1.7% at 50 nodes and +0.3% at 200, intervals of ±6.9%
and ±2.9% (RESULTS.md, Tier B, named pairs; B4 vs B2).

**The overhead of the pipeline configured as upstream.** In the simulator the direction was not consistent: over
3-hour runs at 200 nodes it spent 5 to 16% less CPU per disruption pass than upstream (93 vs 111 ms on the diurnal
workload), while on the compressed diurnal workload (35-minute runs) it spent 3.7% (50 nodes) and 4.6% (200 nodes)
more per pass on the five seeds that pair with the KWOK runs, +2.6% and -0.03% over all ten seeds, with neither
five-seed paired interval excluding zero (pipeline-refactor.md, section 5). On the KWOK cluster at 200 nodes over a
30-minute window: 153.8 controller core-seconds against upstream's 148.8, peak memory 227.0 against 216.1 MB, 461.9
against 414.7 seconds in decision evaluation (which includes the fixed 15-second validation wait per command), 18.8
against 18.0 commands decided (RESULTS.md, Tier B, compute table). The paired CPU interval includes zero
(pipeline-refactor.md, section 5).

**A budgeted search reproduced its decisions on a different machine.** In the simulator, the research branch's random
generator and its copy of upstream's search, both under a 16-simulation budget on the injected clock, were rerun on a
different VM, on W1 at 1,000 nodes: all 20 reruns reproduced their session twins' decision logs up to run IDs, and
their time series and summaries exactly, while CPU per pass changed by 12% on the mean for the random arm (RESULTS.md,
H7-S). That is the property a count-based budget gives. Upstream's wall-clock-bounded search was not rerun (its
1,000-node group was cut; RESULTS.md, H7-B2), so its machine dependence follows from the `context.WithTimeout` in
`firstNConsolidationOption`, not from a measurement. Whether today's one-minute limit binds on real clusters was not
measured either: no simulated pass exceeded 21 s, about a third of the limit, except one (the next slowest, 20.8 s,
was an H7-S rerun; `wall_pass_max_ms` in each run's `summary.json` under `experiments/runs/`; RESULTS.md, H7,
Scaling). The slowest simulated pass in any run was upstream's own, 52.3 s, at 200 nodes, on the one finished run of
the workload with PodDisruptionBudgets, hostname spread and anti-affinity (W5; RESULTS.md, H7, Scaling), and it was
inflated by a simulator cost: Karpenter's topology code lists pods by label selector, which the simulator's fake
client serves through a JSON round trip that a real controller's informer cache does not pay (paper.md, section 3.4;
RESULTS.md, Next steps). So whether the deadline binds on constrained real clusters is unmeasured. The scaling table
is still what a budget protects against: mean CPU per pass grew about 72x for the pipeline arm that runs upstream's
search from 50 to 1,000 nodes against a 20x growth in nodes (19.7 ms to 1,425.1 ms; the two sizes ran on different VMs
with different co-runners, so the ratio is approximate, and part of the growth is the larger fleet the heuristic
leaves rather than the search; RESULTS.md, H7, Scaling; paper.md, section 7, item 4), while no pass in those cells
came near the one-minute limit, the slowest being 6.1 s.

**The decision log explains costs the metrics cannot.** The paper's analysis of why random generation costs more CPU
per pass than upstream's search (paper.md, section 7) was done from decision logs and the run summaries: per-cycle
counts of attempts, simulations and moves showed that one strategy searched on 99 to 100% of passes and the other on
17 to 48%, that under the entropy-guided planners the random strategy spent exactly 1,000 attempts and all 16
simulations on every searching pass at 1,000 nodes (under upstream's first-valid planner the round stops after the
first generator that yields a move, so those runs logged 500 attempts on the 68 to 89% of searching passes where
`random-merge` found one), and that the cost per searching pass differed by 5.5 to 6.8x on dense fleets against 1.3 to
3.1x on fragmented ones. In both tables, heuristic generation means the pipeline's heuristic arm: upstream's candidate
order and its two searches as move generators under the entropy-greedy planner, not upstream itself, which the first
table lists separately. The two tables that carry that argument, as the paper prints them:

| Mean CPU per pass, random / heuristic generation, entropy-greedy planner | 200 nodes | 1,000 nodes |
| --- | --- | --- |
| W1 fragmented | 98 / 143 ms | 895 / 1,425 ms |
| W4 mixed shapes | 59 / 103 ms | 468 / 944 ms |
| W2 diurnal | 121 / 97 ms | 1,246 / 666 ms |
| Compressed diurnal (the cluster workload) | 194 / 85 ms (upstream itself: 56 ms) | not run |

| | Share of passes that search | CPU per searching pass | Simulations per search | Moves emitted per search |
| --- | --- | --- | --- | --- |
| W1, 1,000 nodes: heuristic / random | 0.99 / 0.48 | 1.5 s / 2.0 s | 11.1 / 16.0 | 2.5 / 25 |
| W2, 1,000 nodes | 1.00 / 0.31 | 0.66 s / 3.6 s | 7.0 / 16.0 | 2.0 / 30 |
| W2, 200 nodes | 1.00 / 0.20 | 0.10 s / 0.67 s | 3.9 / 15.6 | 0.6 / 32 |
| Compressed diurnal, 200 nodes | 0.99 / 0.33 | 0.09 s / 0.58 s | 3.5 / 14.6 | 2 / 149 |

(paper.md, section 7, both tables.) The second table's columns do not exist as metrics today; the first is
approximated by `decision_evaluation_duration_seconds`, which measures wall time including the validation wait. The 85
ms in the first table's last row is a research planner (entropy-greedy) that keeps searching past the first acceptable
move; it does not apply to the upstream planner proposed here, which stops where upstream stops (paper.md, section 7,
item 2). On the cluster itself, random generation used 219 to 228 controller core-seconds over 30 minutes against 136
to 154 for every other configuration, upstream included (RESULTS.md, Tier B, compute table).

**Plan fidelity is low and measurable.** In the simulator under default kube-scheduler scoring, about three in four
pods evicted by the pipeline's random/entropy-greedy configuration on W2 at 200 nodes landed somewhere other than
planned (off-plan share 0.749, 95% lower bound 0.725, with spread scale-down; 0.755 with deletion-cost scale-down;
RESULTS.md flags the Exp 5 run set because its oracle placement, which binds each evicted pod to its planned node
whenever that node still passes the scheduler's filter, reached 0.937 fidelity under deletion-cost scale-down against
the preregistered 0.95 floor, mostly from one seed, and the flagged cell is in none of the ten H5 tests), and
upstream's own commands in the same cells landed on plan for 0.451 and 0.455 of evicted pods (RESULTS.md, H5). The
pipeline realized 57% of the drop in its research potential (a measure of fleet packing) that its plans predicted,
upstream 47%, against 87% under `MostAllocated` scoring (paper.md, section 6.5). Steering replacement pods toward
their planned node raised fidelity by 72.5% (0.251 to 0.434); `MostAllocated` scoring by 29.3% and 43.7% (RESULTS.md,
H5). Every consolidation command is such a bet, and today nothing measures whether it is won. The per-pod planned
destination in the decision log is the controller's half of that measurement.

**Size of the change.** The pipeline package on the branch is 41 files and 12,462 lines, of which 3,412 lines in 8
files are tests (63 top-level test functions), at the branch head this RFC describes (pipeline-refactor.md, section 2,
Size, gives the counts before one later change to the package, the `idleAttempts` early stop added to
`strategies/moves` for Exp 9). Six upstream-owned files are touched on the branch: five modified by 70 added and 2
removed lines, and the new 61-line `pipeline_hooks.go`. This RFC's subset keeps `interfaces.go` (265 lines),
`types.go` (370), `config.go` (222, minus the dropped keys), `registry.go` (204), `register.go` (86), `snapshot.go`
(242), `feasibility.go` (233), `score.go` (108), `upstream.go` (493), the coarse half of `adapters.go` (657 today,
shared with the fine adapters), `recorder.go` (74), `method.go` (448) and `synthetic.go` (134, whose `SyntheticEnv`
defaults to `util-entropy` today and would default to `none`), with the `harness` package (1,011 lines in four files;
`parity.go` is 355 of them, and the parity and feasibility tests need the rest for `harness.New`, `GenerateState`,
`UpstreamDecision` and `PipelineDecision`), its `catalog` (312), the `conformance` package (359) and the parity,
pipeline and feasibility tests (573 lines; `pipeline_test.go` drops its `strategies/all` import, and its registry-wide
loops then cover the adapters only): about 5,800 lines before the dropped keys and the fine half are removed. It drops
the research potentials and the `potential` registry kind (all of `potential.go` but `nonePotential`, which moves to
`score.go`) and the `strategies` packages; the `Potential` interface stays, since `DefaultScorer` and `Env` hold one,
and `newMethod` builds `DefaultScorer{Potential: nonePotential{}}` directly, so `delta_potential` is always 0
(`energy` is too, since only the dropped research planners assign it). It touches the branch's six upstream-owned
files plus `multinodeconsolidation.go`, `singlenodeconsolidation.go` and `consolidation.go` for the observer, which is
not yet written. Of the two annotations the branch adds to `pkg/apis/v1/labels.go`, `ShiftSourceSinceAnnotation` is
not carried (only the shift executors in `strategies/executors` set it) and `SimulatedVolumeZoneAnnotation` is,
because `feasibility.go`'s `Fast` check reads it (`VolumeZoneAnnotation`); if `Fast` is dropped from the seam, the
annotation goes with it. Alternative 4 alone is the observer plus a budget type, a few hundred lines with no new
package. The 40-state parity run's time in `make test` has not been recorded and should be measured before beta.

### Interaction with Existing Features

| Feature | With the gate on, default configuration |
| --- | --- |
| NodePool disruption budgets | Unchanged. The method reports the `Underutilized` reason, so today's budgets apply to it; the adapters consult them where upstream does. |
| `karpenter.sh/do-not-disrupt` on pods and nodes | Unchanged: candidacy is upstream's. The snapshot carries the flag so any future stage sees it as a hard constraint. |
| PodDisruptionBudgets | Unchanged: upstream's candidate filter and the Eviction API. The snapshot carries each PDB's allowance for the same reason. |
| `consolidateAfter` | Unchanged: sources are this pass's consolidation candidates, which upstream filters by the `Consolidatable` condition. The interaction with shift moves (consolidation-pipeline.md, Open Question 1) does not arise, since no shift executor ships. |
| `consolidationPolicy: Balanced` | Unchanged. The coarse adapters call upstream's evaluator, so Balanced scores, its metrics and its `ConsolidationApproved` events are emitted exactly as today. Today the decision log records only the command upstream returned and the planner's re-check; with the observer proposed in How It Works, each probe's evaluator verdict becomes a `moves` event (Open Question 5). |
| `SpotToSpotConsolidation` | Unchanged: the rule lives in `computeConsolidation`, which the adapters call. |
| Emptiness, Drift, StaticDrift, Repair, expiration | Unchanged, in the same order, with the same reasons and budgets. The pipeline replaces only the two consolidation methods. |
| `IsConsolidated` | Kept. Upstream's rule: each method skips its search while the cluster's consolidation state is unchanged since a pass in which it found nothing and was not held back by a NodePool budget (`IsConsolidated` and `markConsolidated` in `consolidation.go`). Multi-node skips `markConsolidated` whenever a NodePool has more candidates than its budget allows, which with a 10% budget is nearly always, because it decrements the budget per candidate it keeps; single-node skips it only when a candidate's NodePool had no disruptions allowed at the start of the pass (its budget was zero then: an active `nodes: 0` schedule, or in-flight disruptions and NotReady nodes had used it up), since it never decrements the mapping, so a completed single-node scan normally marks the cluster consolidated. The pipeline's gate is the same test applied to the whole pass (`method.go`); with the upstream adapters the method defers to upstream's rule, so the number of searches per hour is the same as today. A pass stopped by the search budget never marks the cluster consolidated, so a budget cannot turn a truncated search into a skipped one. paper.md, section 7, item 1, measures the effect; Open Question 4 asks whether to switch. |
| Validation | Unchanged: the same validators and the same 15-second delay, inside the coarse adapters. Validation simulations are outside the search budget. |
| Orchestration queue, termination, drain | Unchanged: commands reach `Queue.StartCommand` through `disrupt()` exactly once, as today. |
| PodDeletionCostManagement | Independent. |

The search budget is the one new interaction. With `maxFullSimulations` set, the multi-node binary search can stop
before its window closes and the single-node scan before its candidates run out, so a pass under a budget may
consolidate less than an unbounded pass would. Without the resume cursor a binding budget would re-simulate the same
leading candidates every pass and leave the tail of the candidate list unreachable. That is the point of the budget,
and it is the same behavior as today's deadlines, except that it is counted, and machine-independent up to the
NodePool interleaving whenever the budget binds before a deadline; when a deadline binds first it is today's behavior.

### Observability

**Metrics.** Each dimension below is a `metrics.Label` with its stable values enumerated as `metrics.Value`s, in the
repo's style; `consolidation_type` reuses the existing `ConsolidationType` label, whose `pipeline` value already
exists on the branch (`pkg/controllers/disruption/metrics.go`). All are in the `voluntary_disruption` subsystem.

| Metric | Type | Dimensions | Meaning |
| --- | --- | --- | --- |
| `consolidation_search_attempts_total` | counter | `consolidation_type`, `strategy` | Generation attempts (`SearchBudget.Next` calls: with the observer, one per multi-node probe and one per single-node candidate simulated) |
| `consolidation_full_simulations_total` | counter | `consolidation_type`, `strategy` | Scheduling simulations the search ran; validation simulations are not counted |
| `consolidation_search_stopped_total` | counter | `consolidation_type`, `limit` (`attempts`, `full_simulations`, `timeout`) | Passes whose search stopped at a budget. `timeout` is the pipeline's own `search.timeout`; upstream's one- and three-minute deadlines keep incrementing `consolidation_timeouts_total`. This is the metric for pipeline search timeouts the pipeline RFC's beta criteria ask for (consolidation-pipeline.md, Graduation Criteria, item 4); today timeouts and exhaustion are recorded only in the decision log (consolidation-pipeline.md, Observability) |
| `consolidation_moves_considered_total` | counter | `consolidation_type`, `stage`, `outcome` (`accepted`, `no_op`, `not_cheaper`, `evaluator`, `budget`, `threshold`, `validation`, `other`) | Probes and moves by the verdict each stage gave them. `outcome` buckets the decision log's `reason` (`threshold` is `below threshold`, kept apart from `evaluator` as `threshold_skipped` is in the round summary); `not_downhill` and `conflict` are reserved for planners that do not ship in this RFC |
| `consolidation_search_budget_remaining` | gauge | `consolidation_type` | Simulations left when the last pass ended; 0 means the pass spent its whole cap, whether or not a probe was then refused (`consolidation_search_stopped_total{limit="full_simulations"}` counts the refusals). Not set when `maxFullSimulations` is 0 |

`stage` and `strategy` are new labels with values `candidates`, `moves`, `plan`, `execute` and the registered adapter
names. `limit` and `outcome` are new labels with the values listed. The existing `consolidation_timeouts_total` keeps
its `multi` and `single` labels, because the wrapped upstream methods emit it.

**Log volume.** On the research branch the controller writes every decision event at V(1). Under the observer each
scheduling simulation is one `moves` event: at most 7 for the multi-node binary search plus one per single-node
candidate simulated, 3.5 to 11.1 per searching pass on the research branch's heuristic arm (paper.md, section 7,
second table), bounded by `maxFullSimulations` when it is set and by the candidate count (hundreds at 1,000 nodes)
when it is not. Each candidate the search skips is one more `moves` event, with reason `nodepool budget` or `below
threshold`, and `maxFullSimulations` does not bound those: the multi-node filter records one for every candidate past
its NodePool's budget, so under a 10% budget they alone run to hundreds per pass at 1,000 nodes (864 candidates there;
Motivation), whatever the cap. A random generator emitted up to 149 moves per searching pass at 200 nodes. None of it
belongs at V(1). The `log` recorder therefore writes the round summary (one line per generator per pass: attempts,
simulations, moves, stopped-at) at V(1) and per-move events at V(2), and the `file` recorder is the sink for anyone
who wants every event without raising the log level. The research branch's decision logs are in
`experiments/runs/*/*/decisions.jsonl.gz` (group, then run); the simulator and Karpenter write the same schema.

**Events.** Upstream's `Unconsolidatable`, `ConsolidationApproved` and `ConsolidationRejected` events are emitted by
the code the adapters call. One addition: `BuildSnapshot` calls `podutils.IsDisruptable` with the event recorder for
every pod on every node, so a malformed `karpenter.sh/do-not-disrupt` annotation value is published once more per
round than today: once more in a pass whose multi-node round executes, twice more in a pass that goes on to the
single-node round (Open Question 7; consolidation-pipeline.md, Edge Cases, Gate on flag empty). The candidate re-fetch
before that round publishes it too, but so does today's per-method `GetCandidatesWithTotals` in `disrupt()`, so it
adds nothing over today; and the recorder dedupes the event per pod and message for two minutes, so the extra
publishes seldom become extra Kubernetes events. The snapshot's cost under the coarse adapters is Open Question 7.

### Edge Cases

- **The budget runs out mid-pass.** The multi-node search keeps its best accepted prefix and stops; if no prefix was
  accepted the pass moves to the single-node round with the budget that is left. A search stopped by the budget sets
  `constrainedByBudgets` in both methods, so neither marks the cluster consolidated and the next pass searches again
  with a fresh budget (today a multi-node deadline expiry with no accepted prefix goes through the no-op path, which
  marks it unless a NodePool budget held a candidate back). The single-node scan stops at the current candidate and
  records its cursor. Whatever was accepted is validated and emitted; nothing is emitted unvalidated. The round
  summary records `exhausted: 1` and the metric `consolidation_search_stopped_total{limit="full_simulations"}`
  increments.
- **The budget is smaller than one probe.** Under the per-pass budget this RFC introduces, `maxFullSimulations: 1`
  lets the multi-node search run exactly one simulation (its first prefix) and the single-node scan none; on the
  branch today, where each sequential round builds its own budget, the fine adapters would let the single-node round
  run one more, and the coarse adapters ignore the cap altogether. `maxFullSimulations` counts the search's
  simulations only: the validation simulation that follows an accepted command (at most one per method per pass) is
  not counted and is never refused, so a pass runs at most `maxFullSimulations` plus one validation simulation per
  method. The configuration is accepted; the decision log shows what it cost. A value below 0 is a startup error.
- **The recorder blocks or errors.** `Record` is synchronous, so a slow sink slows the pass. The `file` recorder is a
  buffered synchronous append, so under load it slows the pass rather than dropping events, and buffering changes when
  bytes reach the file, not which. A write error disables the recorder for the rest of the process, with one log line,
  and is never returned to the method. A recorder can never fail a pass: the method does not look at anything the
  recorder returns.
- **The gate is flipped at runtime.** Feature gates are read at startup. Changing the gate or the configuration
  requires a restart, as every Karpenter feature gate does. Because this RFC ships no shift executor, nothing is left
  on nodes when the gate is turned off (the shift-source taint problem of consolidation-pipeline.md, Open Question 2,
  does not arise).
- **An unknown strategy name, a research strategy, or `mode: fine`.** Construction fails at startup with `unknown
  moves strategy "random-merge"` and the allowed names, and `mode: fine` fails the same way. There is no fallback to
  the default, so a typo cannot silently run upstream's search while the operator believes a budget is set.
- **A multi-NodePool cluster.** Upstream's single-node scan interleaves NodePools in Go map order, so upstream itself
  is nondeterministic there; the adapters inherit that, and the parity test compares against the set of decisions
  upstream makes over repeated runs (consolidation-pipeline.md, Parity, Upstream's own nondeterminism). A budget does
  not change this.
- **A pass with no candidates.** The controller's `disrupt()` returns before calling the method when it finds no
  candidates, so no decision event is recorded for that pass, as today.

## Alternatives Considered

### Alternative 1: Keep the time deadline only

Leave `MultiNodeConsolidationTimeoutDuration`, `SingleNodeConsolidationTimeoutDuration` and the 100-candidate window
as the only bounds. This is today. It has no cost and no benefit: decisions keep depending on the machine, the two
time limits and the 100-candidate window stay the only bounds on constrained clusters (whether either time limit binds
there stays unmeasured; no session pass reached one, and the slowest, 52.3 s, was a simulator-inflated pass on the
constrained workload W5; RESULTS.md, H7, Scaling; paper.md, section 3.4), and there is still no way to say how much
work a pass did. Rejected because the budget is a strict addition: with the budget unset the two deadlines and the
window are all there is, exactly as today.

### Alternative 2: Keep one log line per command (today's logging)

Add fields to `Queue.StartCommand`'s existing "disrupting node(s)" log line and the `Unconsolidatable` events instead
of a structured per-move record. This cannot answer "why not" for a node the search never probed, cannot show the
multi-node prefixes a search tried before it accepted a command or any probe's predicted savings (the
`Unconsolidatable` events already name single-node candidates that lost, as the Motivation says), and cannot carry
per-pod planned destinations. The paper's section 7 analysis, which needed per-pass attempts, simulations and moves,
is not possible from it. Rejected; the decision log subsumes it and its default recorder is a no-op.

### Alternative 3: A full strategy plugin API now

Expose the stage interfaces as a supported extension point, with the research branch's registry open to out-of-tree
strategies, so operators can ship their own generators and planners. The experiments argue against doing this yet: no
strategy other than upstream's beat upstream on cost, disruption and compute together (Non-Goals), the interfaces
changed during the research (the `Guide` seam, the moves mode, the pod-eviction budgets were all added as the
strategies needed them), and a plugin API would freeze them. Rejected for now. The seams ship, but the allowlist
admits only the adapters, and the registry is not a public API. Open Question 2 asks whether the recorder, which is
the stable part, should be.

### Alternative 4: Budgets without the seams

Thread a `SearchBudget` into the two upstream loops (the observer of How It Works) and stop there: no pipeline method,
no stage interfaces, no snapshot. This is the smallest change that makes the search counted and, when the budget binds
first, machine-independent, and it is a strict subset of the proposal. What it loses is the decision log's structure
(a record per stage rather than a record per simulation), the parity test that pins the adapters, and the ability to
compare a different search or acceptance rule at equal compute, which needs a seam to put the alternative behind.
Rejected on its own, but accepted as the first commit of the proposal: the observer in the upstream loops is the same
code either way.

### Alternative 5: Ship the fine adapters instead of the coarse ones

The research branch's fine adapters re-implement the multi-node binary search and the single-node scan in stages and
consult the budget and the planner at every step; they pass the same 10,000-state parity test and were the
configuration checked over whole runs and on KWOK (B4). They make the budget bind without touching upstream's loops,
carry the resume cursor already, and they make the planner swappable. Their cost is a second copy of the two searches
that every upstream change must be mirrored into, with the parity test as the only thing that catches a drift, and
they drop Balanced's metrics and events because the fine planner mirrors the evaluator rather than calling it
(pipeline-refactor.md, section 5). Not chosen for alpha, where the lower-risk path is upstream by construction, but
kept as Open Question 3: if maintainers would rather not add an observer to the upstream loops, the fine adapters are
the alternative with the evidence behind it.

## Backward Compatibility

- **Gate off (the default).** `disruptionMethods` in `pkg/controllers/controllers.go` returns no options when the gate
  is off, so `NewController` builds exactly today's methods; the pipeline packages are compiled in but the method is
  never constructed, and the observer this RFC adds to the two upstream loops would be nil (consolidation-pipeline.md,
  Backward Compatibility; pipeline-refactor.md, section 7: with the gate off, no code path changes). Package `init()`
  registration of strategy names still runs with the gate off; only the method is never constructed. This is by
  construction, not by measurement: the KWOK runs with the gate off (B2, B3) checked that only multi-node and
  single-node evaluations appeared in the controller's metrics (TIERB.md, Validity), which confirms the wiring, not
  byte-identical behavior. No test compares gate-off output byte for byte; the claim rests on `disruptionMethods`
  returning nil.
- **Gate on, flag empty.** The decisions are today's, as the parity test checks on 10,000 states; the whole-run and
  KWOK comparisons of the fine adapters (B4) are consistent with it but do not exercise the coarse path. The visible
  differences: `consolidation_type="pipeline"` appears on `decision_evaluation_duration_seconds`, `decisions_total`,
  `decisions_by_nodepool_total` and `queue_failures_total`, while `consolidation_timeouts_total` and
  `failed_validations_total` keep `multi` and `single` (consolidation-pipeline.md, Observability). Dashboards that
  filter on `consolidation_type` need the new value. Open Question 5 of the pipeline RFC (report the winning upstream
  method's label instead) is inherited.
- **Gate on with a budget or a recorder.** New behavior by definition: the search may stop earlier than today's
  deadlines would stop it, a budget-stopped pass does not mark the cluster consolidated, and log volume rises at V(2).
  No node, NodeClaim or pod is touched differently.
- **Rollback.** Turning the gate off restores today's methods at the next start. Nothing is left on cluster objects.
- **Configuration.** The flag is new, so existing deployments are unaffected. There is no CRD, NodePool or annotation
  change. Configuration errors fail at startup, never silently.
- **The decision log schema** is versioned from 1; version 2 adds `bindings` and gives `budget_remaining` its meaning.
  Consumers read `schema_version` first.

## Graduation Criteria

**Alpha (this RFC).** The `ConsolidationPipeline` gate, default off. An empty flag selects the coarse adapters with no
budget and no recorder. The supported configurations are: the default; a `search` block; a `recorder` block. No
strategy name outside the allowlist is accepted in Karpenter: the `savings-only` and `balanced` planners that
`adapters.go`'s `init()` registers beside the adapters are rejected at startup, as `mode: fine` is. Too risky to
enable by default because the observer in the upstream loops is new code on the hot path, the metrics are new, and the
log volume at V(2) is unmeasured on large clusters.

**Beta.** All of the following, each checkable:

1. The parity test runs in CI: the 40-state run in `make test` on every change (exists), and the 10,000-state run
   (about 4 hours: three of its four chunks took 53 to 71 minutes each; docs/research/parity.md, Results) as a weekly
   scheduled workflow in this repository, one job per policy and catalog (`PARITY_STATES=10000` with `PARITY_POLICIES`
   and `PARITY_CATALOGS`, the split the branch's chunks used), where a mismatch opens an issue against the disruption
   controller that the RFC author triages until beta, with no unexplained mismatch, including the budget-capped states
   added for the observer.
2. An equivalence check on KWOK: the gate on with the default configuration against the gate off, on the Tier B
   harness (`experiments/tierb/`), for at least 30 seeds, with two one-sided paired tests at a 3% margin on end cost
   (the margin B4's simulator check used; RESULTS.md, B4) passing, and on evictions a paired 95% interval that
   includes zero (Tier B's five-seed B4-versus-upstream evictions half-width was ±13% at 200 nodes and ±59% at 50,
   paper/tables/tierb_paired.md, so no 3% equivalence margin on evictions is reachable at 30 seeds), and, for every
   gate-on pass that executed, the `execute` event's command equal to the command `Queue.StartCommand` logged for that
   pass (the log and the controller agree on what ran, through the adapter's conversion and back); decision-level
   parity against upstream remains `TestParity`'s job, since a live run cannot replay one state under both methods.
   Thirty is the seed count paper.md, section 9, estimates for resolving a 2% difference in end cost at 200 nodes,
   from the ±4.7% median interval at five seeds; at five seeds the B4-versus-upstream interval on end cost was ±2.9%
   at 200 nodes (RESULTS.md, Tier B). Thirty is derived from that estimate, not a measured requirement.
3. The decision log schema is documented in `designs/` with its version, and a change to it bumps the version in the
   same PR.
4. The metrics above reviewed by the SIG, with `consolidation_search_stopped_total` and
   `consolidation_moves_considered_total` at minimum, and their `metrics.Label` definitions in `pkg/metrics` or the
   disruption package.
5. At least one real-cluster run of the gate on against the gate off, following the EKS plan in
   `docs/consolidation/kwok-experiments-and-eks-plan.md` (the replication there is estimated at about $1,100 of
   on-demand instances; paper.md, section 9), with the team that runs and pays for it named before beta.
6. Open Questions 1 and 3 below resolved.

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
   is behavior, not instrumentation. Proposed: keep `IsConsolidated` at alpha, and decide at beta, from
   `consolidation_search_stopped_total` and the round summaries, whether the pipeline-level gate replaces it.
5. **What does the per-move record contain under the coarse adapters?** With the observer, each multi-node probe (one
   prefix, one simulation) and each single-node probe is one `moves` event with the upstream `Command` it produced or
   the reason it was rejected. The planner's `plan` event then records the evaluator's verdict. Whether the probe's
   full placement (every pod's planned node, the `bindings` of schema version 2) belongs in the `log` recorder at
   V(2), or only in the `file` sink, is a volume question for beta.
6. **The research branch's fine adapters, strategies and simulator.** They stay where they are, on the research branch
   and in the simulator, and are not part of this proposal. Whether any of them comes upstream later depends on
   results the experiments did not produce (Non-Goals). A follow-up experiment on early stopping and biased candidate
   selection for the research branch's random generators (`experiments/EXP9.md`) is running; it may inform how a
   budget is best spent, and does not bear on this proposal's scope.
7. **Snapshot cost under the coarse adapters.** `BuildSnapshot` runs once per round with the gate on, so twice in a
   pass whose multi-node round executes nothing: before every round after the first, `Method.ComputeCommands`
   re-fetches candidates, totals and budgets (`refresh`, as `disrupt()` does per method today) and rebuilds the
   snapshot and the selection whenever the injected clock has moved since the last build, which under the real clock
   it always has. Each build is a NodePool list with `GetInstanceTypes` per NodePool (`BuildNodePoolMap`, to price
   non-candidate nodes), a PDB list, a `DeepCopyNodes` and a pod list per node, and it has not been profiled at any
   scale; the only CPU profiles taken were of upstream arms (pipeline-refactor.md, section 5;
   consolidation-pipeline.md, Open Question 6, states the caveat as unprofiled at 5,000 nodes). The coarse adapters
   use it only to filter sources and convert the returned command, not to search. Proposed: profile it at 1,000 nodes
   before beta, and if it is not small next to one scheduling simulation, build a reduced snapshot (candidates and
   their pods only) in the default configuration.
8. **A plan-fidelity metric.** `consolidation_plan_fidelity` would need a component that pairs the decision log's
   per-pod planned destination (schema version 2) with pod binding events from the existing pod informer over a
   trailing window, so it holds state across passes; the replacement is a different pod, so matching is by controlling
   owner, and a replace command's planned destination is a launch index that must be mapped to the NodeClaim the queue
   creates. This RFC does not fix its owner, window, memory bound or cost. Proposed: ship the `bindings` field (the
   controller's half of the measurement) at alpha and decide the gauge at beta, once field decision logs with
   `bindings` exist.
9. **Should the observer also replace upstream's deadlines?** Under the gate, upstream's `context.WithTimeout` on the
   multi-node search and the single-node clock check still run beside the budget. Replacing them with the budget's
   deadline would make `search.timeout` the only clock and stop `consolidation_timeouts_total{multi,single}`
   incrementing under the gate, at the cost of a second change inside the two loops. Proposed: keep both at alpha, so
   the gate changes nothing a deadline does today.

## References

- [Pluggable consolidation pipeline](consolidation-pipeline.md): the full design this RFC is a subset of; its Open
  Questions 1 to 10 are inherited where this RFC does not close them.
- [Balanced consolidation](balanced-consolidation.md): the issue list behind Use Case 3 and the open question on
  asynchronous generation.
- `docs/consolidation/pipeline-refactor.md`: the refactor for readers who know Karpenter's features, with the size,
  overhead and pros and cons tables cited above.
- `experiments/RESULTS.md`: the preregistered experiments (H1 to H7, ablations, B4, Exp 8) and the Tier B KWOK replay,
  with every number cited here.
- `docs/research/parity.md` and `experiments/parity/chunks/*.summary.txt`: the parity runs, their timings and the
  per-chunk counts of states upstream decided on.
- `paper/paper.md`: sections 3.4 (the constrained workload and the simulator's cost on it), 6 (results), 7 (why random
  generation costs more CPU, from the decision logs), 8 (discussion) and 9 (next steps).
- `experiments/TIERB.md`: the KWOK protocol, whose validity rule checks which methods decided with the gate on and
  off.
- `experiments/EXP9.md`: the follow-up early-stop and biased-selection experiment on the research generators.
- Code on the research branch: `pkg/controllers/disruption/pipeline` (interfaces, adapters, method, registry, parity
  test), `pkg/controllers/disruption/pipeline_hooks.go`, `pkg/controllers/disruption/multinodeconsolidation.go` and
  `singlenodeconsolidation.go` (the loops the observer would enter), `pkg/controllers/controllers.go`
  (`disruptionMethods`), `pkg/operator/options/options.go`, `hack/consolidation-sim`, `test/suites/performance/`.



