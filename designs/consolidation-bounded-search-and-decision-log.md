# Bounded Search Budgets and a Decision Log for Consolidation

Give consolidation an explicit, counted search budget and a structured record of every move it considered, through stage interfaces that reproduce today's decisions exactly, behind an off-by-default feature gate.

This RFC is the first increment of the [pluggable consolidation pipeline RFC](consolidation-pipeline.md). That RFC holds the full design: stage interfaces, adapters around today's two consolidation methods, shift moves, potentials and planners. It deliberately leaves the first increment to this RFC. This RFC proposes three parts of it — the search budget, the decision log and the stage seams with today's search behind them — and none of the experimental strategies.

Throughout, *today's search* and *the existing methods* mean the two consolidation methods as they run today, `MultiNodeConsolidation` and `SingleNodeConsolidation`. The adapters around them are registered under the strategy names `upstream`, `upstream-multi`, `upstream-single` and `upstream-queue`, "upstream" being the in-tree code they wrap.

## Motivation

Karpenter's consolidation runs as two disruption methods, `MultiNodeConsolidation` and `SingleNodeConsolidation`. Each one orders candidates, generates moves, decides whether to accept them and validates them inside a single `ComputeCommands` call. Three properties of that structure are the problem.

**The search is bounded by time, not by work.** Multi-node consolidation binary-searches over prefixes of the savings-ratio-sorted candidates that survive the NodePool budget filter, over a window of at most 101 of them, and stops after one minute of wall-clock time (`MultiNodeConsolidationTimeoutDuration`). Single-node consolidation walks the candidates until three minutes have passed on the injected clock, which is real time in production. What a pass finds therefore depends on how fast the controller's machine is and on what else it is running: a busy controller finds fewer consolidations than an idle one from the same cluster state. Neither search reports how much work it did, so two search strategies cannot be compared at equal compute. In a simulated 1,000-node fleet (an in-process simulator on a 4-vCPU machine), a full single-node scan is about 864 candidates at roughly 67 ms per scheduling simulation, close to a minute.

**Rejected alternatives leave no record.** The controller logs the command it starts and emits `Unconsolidatable` events for single-node candidates that lost, with a one-line reason. It keeps no record of the multi-node prefixes it tried, the predicted savings of each, or where each evicted pod was planned to go. An operator who asks why a node was not consolidated, or why the cluster has held a fragmented fleet for an hour, has no artifact to look at. The reports behind the Balanced consolidation RFC are this question in different forms:

- Nodes at 93-99% CPU utilization disrupted instead of lightly utilized ones ([aws#8868](https://github.com/aws/karpenter-provider-aws/issues/8868), [kubernetes-sigs#2319](https://github.com/kubernetes-sigs/karpenter/issues/2319)). A decision log would show which lightly utilized candidates were considered in that pass and why each lost.
- Multi-hour consolidation loops replacing the same instance types with no net savings ([aws#8536](https://github.com/aws/karpenter-provider-aws/issues/8536), [aws#6642](https://github.com/aws/karpenter-provider-aws/issues/6642), [aws#7146](https://github.com/aws/karpenter-provider-aws/issues/7146)). The log would show each pass's predicted savings for the replacement.
- Rapid node churn where consolidation deletes nodes that are immediately re-provisioned ([kubernetes-sigs#1019](https://github.com/kubernetes-sigs/karpenter/issues/1019), [kubernetes-sigs#735](https://github.com/kubernetes-sigs/karpenter/issues/735), [kubernetes-sigs#1851](https://github.com/kubernetes-sigs/karpenter/issues/1851)). The per-pod planned destination would show whether the plan was ever feasible.

**The stages cannot vary independently.** Candidate order, move generation, acceptance and validation are interleaved in one loop per method, so none can be changed or measured on its own, and the Balanced evaluator is called from inside both loops. Every improvement to consolidation, including the Balanced policy itself, has had to be threaded through those loops.

We propose the instrument that addresses all three: stage seams whose adapters reproduce today's decisions exactly, a counted search budget that makes two searches comparable at equal compute, and a decision log that records what each pass considered. No new search strategy ships with it.

### Use Cases

1. **A per-pass compute ceiling that does not depend on the machine.** An operator running 1,000 or more nodes wants consolidation to spend at most, say, 16 scheduling simulations per pass whatever the controller's CPU allocation, and a metric that says when that ceiling was hit. Today's ceilings are two time limits, which bind differently on every machine, and the 101-candidate window.
2. **Explaining a non-decision.** An operator asks why node X, at 30% utilization, has not been consolidated for an hour. The decision log shows every probe that touched X in each pass, its predicted savings, and why it lost: no cheaper placement, no cheaper instance type, rejected by the Balanced evaluator, skipped by its NodePool's budget, skipped by the Balanced threshold pre-filter, rejected at validation, or never reached before the budget ran out.
3. **Evaluating a change to the search at equal compute.** A maintainer wants to compare the current prefix search against a variant (a different candidate order, a different `k` for Balanced, a different window) under the same simulation budget, with a record of what each considered. The seams put the variant behind an interface; the budget and the log make the comparison about the strategy, not the machine. The comparison itself runs in the pipeline RFC's simulator; what this RFC gives the controller is the same counters and the same log format, so field data and simulator data are comparable.
4. **Measuring plan fidelity in the field.** A consolidation command is a bet that evicted pods land where the simulation put them. In the simulator, under default kube-scheduler scoring, today's own commands landed on plan for about 45% of evicted pods. Nothing in the controller measures this. A per-pod planned destination in the decision log is the half of that measurement the controller can supply.

### Non-Goals

- **No new default consolidation strategy.** With the gate on and no configuration, the pipeline makes the same decisions as today's two methods. With the gate off, nothing changes.
- **No shift moves, potentials or entropy-guided planners.** The pipeline RFC's experimental generators and planners stay in the simulator. None has shown a cost, disruption or compute advantage over today's search in the pipeline RFC's measurements, so none ships here; the pipeline RFC's Open Question 9 holds that decision.
- **No change to NodePool API fields**, to disruption budget semantics, `consolidateAfter`, `consolidationPolicy`, or any annotation users set.
- **No asynchronous or continuous generation.** All stages run inside one `ComputeCommands` call per pass. The Balanced RFC's open question on asynchronous generation stays open; the seams are its prerequisite, not its answer.
- **No change to Emptiness, Drift, StaticDrift, Repair, the orchestration queue, termination or drain.**

## Proposal

With `--feature-gates ConsolidationPipeline=true`, the disruption controller runs one consolidation method, `PipelineConsolidation`, where multi-node and single-node consolidation used to run. The method composes four stages (candidates, moves, plan, execute) around shared services. It ships with one implementation per stage, two for moves: the *coarse adapters*, which call today's `MultiNodeConsolidation.ComputeCommands` and `SingleNodeConsolidation.ComputeCommands` whole and convert the result, so today's search, acceptance, validation, metrics and events run unchanged. (The pipeline RFC also has *fine* adapters that re-implement the two searches stage by stage so the acceptance rule can be swapped. They are not in this RFC; see [Alternative 5](#alternative-5-ship-the-fine-adapters-instead-of-the-coarse-ones).)

Three things are new to the user:

1. **A search budget.** `search.maxFullSimulations` caps scheduling simulations per pass and `search.maxAttempts` caps generation attempts per generator; `search.timeout` is a deadline checked between simulations. Each defaults to no bound, leaving today's one- and three-minute limits as the only ones. When a budget is set, the search stops at the budget or at today's limits, whichever comes first. Whenever the budget binds first, what a pass finds no longer depends on the machine's speed: the same cluster state gives the same decision on any machine, up to the existing single-node scan's NodePool interleaving, and a strategy's cost is counted, not only timed.
2. **A decision log.** Every stage decision is one structured, schema-versioned event: the candidates considered, each probe the search made with its predicted savings and disruption, the planner's verdict and reason, the executor's outcome, and a per-round summary of attempts, simulations and moves. A `Recorder` receives them. The default recorder discards; a `log` recorder writes round summaries at V(1) and per-move events at V(2); a `file` recorder writes JSON lines.
3. **Stage seams behind the gate.** The `CandidateSelector`, `MoveGenerator`, `Planner` and `Executor` interfaces, the `Snapshot`, `Feasibility`, `Scorer` and `SearchBudget` services, and a registry that selects each stage by name, restricted by an allowlist to the adapters. A differential parity test holds the adapters to today's decisions.

### Proposed Spec

```yaml
# --consolidation-pipeline-config, or CONSOLIDATION_PIPELINE_CONFIG
pipeline:
  search:   {maxFullSimulations: 16, timeout: 1m}
  recorder: {name: log}
```

Everything not named takes its default, so this configuration runs today's own search, stopped after 16 scheduling simulations in the pass, one minute on the injected clock, or the existing one- and three-minute limits, whichever comes first, and logs what it considered. Validation simulations are outside the 16.

| Item | Value |
|---|---|
| Feature gate | `ConsolidationPipeline`, alpha, default `false` |
| Flag | `--consolidation-pipeline-config` (env `CONSOLIDATION_PIPELINE_CONFIG`), default empty |
| Flag value | A file path, or the configuration inline. A value that starts with `{` or `pipeline:`, or spans more than one line, is parsed inline as YAML or JSON; any other value is read as a file path. A one-line bare form such as `search: {maxFullSimulations: 16}` is read as a path and fails at startup; wrap it in `pipeline:` or write it on more than one line |
| Empty value | The adapters around today's methods, no budget, no recorder: today's decisions |
| Invalid value | Karpenter fails at startup with the parse or validation error, or the file-read error for an unreadable path |

The configuration schema, complete for this RFC:

```go
type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"`             // one object or a list
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Search     SearchConfig `json:"search,omitzero"`
	Recorder   StageConfig  `json:"recorder,omitzero"` // none (default), log, file
}

type StageConfig struct {
	Name   string         `json:"name"`
	Params map[string]any `json:"params,omitempty"` // decoded strictly by the strategy
}

type SearchConfig struct {
	MaxAttempts        int    `json:"maxAttempts,omitempty"`        // generation attempts per generator; 0 = no bound
	MaxFullSimulations int    `json:"maxFullSimulations,omitempty"` // scheduling simulations per pass; 0 = no bound
	Timeout            string `json:"timeout,omitempty"`            // deadline on the injected clock, e.g. "1m"; empty = none
}
```

Unknown keys fail at startup, strategy parameters are decoded strictly, and an unknown strategy name is an error. The pipeline RFC's `movesMode`, `potential`, `seed` and `budgets` keys are not accepted: the generators always run in sequence and the method always runs with the `none` potential.

**The allowlist.** The registry accepts every name a compiled-in package registered, and the core package registers more than the adapters (the `savings-only` and `balanced` planners, the `mode: fine` parameter of both move adapters, and three experimental potentials). This RFC drops the `strategies/all` import from `pkg/controllers/controllers.go`, so the experimental strategies are not compiled into Karpenter, and validates the configuration at startup against an allowlist, since dropping those packages from the build is not what keeps the rest out:

| Stage | Allowed | Role |
|---|---|---|
| candidates | `upstream` | This pass's consolidation candidates, in today's savings-ratio order |
| moves | `upstream-multi`, `upstream-single` (`mode: coarse` only; `fine` is rejected) | Call the existing method's `ComputeCommands` and convert the returned command to a move |
| plan | `upstream` | Re-runs the Balanced evaluation on the returned command and re-checks it against the pass's budgets; a command the existing method already approved passes both |
| execute | `upstream-queue` | Returns the validated command for the orchestration queue |
| recorder | `none` (default), `log`, `file` | Decision log sink. `file` takes `path` (required, on a writable volume in the controller pod) and `maxSizeMB` (default 64; one rotated file is kept) |

**Stage interfaces**, abridged (the full set is in the pipeline RFC):

```go
type Snapshot interface { // immutable, cheaply forkable view of cluster state
	Nodes() []NodeView
	Pods(node string) []PodView
	Load(node string) Resources
	Apply(m Move) (Snapshot, error)
	Totals(nodePool string) Totals  // Balanced's totals, fixed for the pass
	PDBAllowed(pdb string) int
}
type CandidateSelector interface { Name() string; Select(ctx context.Context, s Snapshot, rng *rand.Rand) (sources, destinations []NodeView, err error) }
type MoveGenerator     interface { Name() string; Generate(ctx context.Context, s Snapshot, sources, destinations []NodeView, rng *rand.Rand, b *SearchBudget) ([]Move, error) }
type Planner           interface { Name() string; Plan(ctx context.Context, s Snapshot, moves []Scored, budgets Budgets, rng *rand.Rand) (Plan, error) }
type Executor          interface { Name() string; Execute(ctx context.Context, p Plan) (Report, error) }
type Feasibility       interface { Fast(s Snapshot, m Move) error; Full(ctx context.Context, s Snapshot, m Move) (Move, error) }
type Recorder          interface { Record(e DecisionEvent) }

// SearchBudget caps one pass's search. Attempts are counted per fork (one per generator);
// full simulations and the deadline are shared by every fork of one budget.
type SearchBudget struct {
	MaxAttempts int        // 0 = no bound
	MaxFull     int        // 0 = no bound
	Deadline    time.Time  // zero = no deadline
	Clock       clock.Clock
}
```

**Decision log schema.** This RFC ships schema version 2. Version 1 has `budget_remaining` always 0 and no `bindings`; it stays readable, and consumers read `schema_version` first. The version is bumped on any field change.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | 2 |
| `run_id` | string | `"controller"` in Karpenter; a run ID in the simulator |
| `cycle` | int | Pass counter: incremented once per `ComputeCommands` call. A *round* is one generator's turn within a pass |
| `t` | float | Seconds since the method started, on the injected clock |
| `stage` | string | `candidates`, `moves`, `plan`, `execute` |
| `strategy` | string | Strategy name; generators joined with `+` on a round summary |
| `move_id` | string | Unique per strategy instance, e.g. `multi-3`; empty on summaries |
| `kind` | string | `merge` (today's delete and replace both map to it; `detail.launch` is 0 for a delete, 1 for a replace). Other values are reserved for the simulator |
| `sources`, `destinations` | []string | Sources in today's candidate order; destinations sorted |
| `pods` | int | Bindings (on the candidates summary: candidate count) |
| `disruption`, `savings_per_hour` | float | The move's scores |
| `delta_potential` | float | 0 with the `none` potential; the pipeline RFC's potentials give it meaning |
| `accepted` | bool | The stage's verdict |
| `reason` | string | Why not: `no-op`, `replacement not cheaper`, `evaluator rejected`, `nodepool budget`, `below threshold`, `validation: budget\|scheduling\|churn`; from the pipeline's own stages, `not accepted`, `not selected`, `node budget for default: 2 > 1` |
| `budget_remaining` | int | Simulations left in the pass after this event; -1 when `maxFullSimulations` is 0 (always 0 in version 1) |
| `bindings` | list | Optional: per pod, its source node and planned destination. Written by the `file` sink; whether the `log` recorder writes it at V(2) is Open Question 5 |
| `detail` | map | Candidates summary: `sources`, `destinations`, `candidates`. Round summary: `moves`, `attempts`, `full_simulations`, `timed_out`, `exhausted`, `unreached`, `budget_skipped`, `threshold_skipped`. Per move: `launch`, `remove`, `energy` (0 with the `none` potential), `norm_disruption`, `norm_savings` |

The plan-fidelity *metric* that `bindings` enables is [Open Question 7](#open-questions), not part of this RFC.

### How It Works

**One pass.** The method runs where multi-node consolidation ran, with the same candidates and budgets `disrupt()` computes today:

```
disrupt() ─► Method.ComputeCommands(budgets, candidates)
               ├─ BuildSnapshot                                   ─► nodes, pods, PDB allowances, totals
               ├─ one SearchBudget for the pass
               ├─ stage 1  selector.Select(snapshot)              ─► sources, destinations
               ├─ for each generator, in order (one round each)
               │    ├─ rounds after the first: if the clock moved, refresh candidates, totals and budgets and rebuild the snapshot
               │    ├─ stage 2  g.Generate(..., budget.Fork())    ─► moves
               │    ├─ stage 3  planner.Plan(snapshot, moves, budgets)
               │    ├─ stage 4  executor.Execute(plan)            ─► commands
               │    └─ a round that executes anything ends the pass
               └─ a DecisionEvent at every stage
disrupt() ─► Queue.StartCommand for each returned command (unchanged)
```

Each generator is its own round, and the next round runs only if the previous one executed nothing, which is how the controller runs multi-node and then single-node consolidation today. Before the second round, if the injected clock has moved (as it has whenever the first round waited out a validation delay), the method re-fetches candidates, totals and budgets as `disrupt()` does per method and rebuilds the snapshot, so the single-node round sees the cluster as it is after a failed validation wait. With the adapters the method uses each existing method's own `IsConsolidated` skip rule, not the pipeline's cluster-state gate, so the number of searches per hour is unchanged.

**The coarse adapters.** `upstream-multi` and `upstream-single` call the existing methods' own `ComputeCommands` and convert the returned `Command` into a `Move` and back. One `MultiNodeConsolidation` and one `SingleNodeConsolidation` are built with the constructors `NewMethods` uses and shared by both adapters, so `lastConsolidationState`, `PreviouslyUnseenNodePools` and the validators behave as today. This mode is today's behavior by construction.

**The budget.** A `SearchBudget` holds `MaxAttempts`, `MaxFull` and the deadline. Generators call `Next()` once per attempt and `UseFull()` before each scheduling simulation; attempts are counted per generator fork, full simulations and the deadline are shared across the pass, and `Stats()` feeds the round summary. One budget is built per pass and forked per round, so a pass with two generators cannot spend twice the cap; each round's summary reports its own fork's attempts and simulations.

**The observer.** Under the coarse adapters, the existing methods' own loops run the search, so a budget has nothing to bind unless those loops consult it. We propose one small interface in the disruption package, implemented by the pipeline package (the dependency runs that way):

```go
// SearchObserver is consulted by the two consolidation searches when the pipeline drives them.
type SearchObserver interface {
	Next() bool            // one probe is about to run; false stops the search
	UseFull() bool         // one scheduling simulation is about to run; false stops the search
	Record(p SearchProbe)  // the probe's result
}

// SearchProbe is one simulated prefix (multi-node) or candidate (single-node): the command it
// produced, if any, and why it was not accepted.
type SearchProbe struct {
	Candidates []*Candidate
	Command    Command
	Reason     string // "", "no-op", "replacement not cheaper", "evaluator rejected", "nodepool budget", "below threshold", "validation: ..."
}
```

A `SetSearchObserver` method on the `consolidation` struct both methods embed is called by the adapters before each `ComputeCommands`, with the pass's budget fork and the method's recording function rather than the raw `Recorder` sink, so the `ComputeCommands` signature is unchanged. The observer is nil with the gate off, and the loops are unchanged there. With it set:

- `firstNConsolidationOption` calls `Next()` and `UseFull()` before each simulated prefix and stops the search when either is refused, keeping the best prefix found so far, exactly as it does when the one-minute context expires. The single-node scan does the same per candidate it simulates. One `Next()` is one probe in the binary search or one simulated candidate in the scan.
- Candidates the search skips are not attempts, but each is one `Record()` call with no command: `nodepool budget` for a candidate dropped by its NodePool's budget, `below threshold` for one the Balanced threshold pre-filter skips. The round summary counts them as `budget_skipped` and `threshold_skipped`.
- The validators' own simulation is outside the budget, so whatever the search accepted is always validated; `ComputeCommands` records the validation verdict as one more probe.
- A budget stop does not increment `consolidation_timeouts_total`; `consolidation_search_stopped_total` counts it.
- The pipeline's observer converts each `SearchProbe` into a `DecisionEvent` and passes it to the method's recording function, which stamps `schema_version`, `run_id`, `cycle` and `t` as it does for the round's own events.

Two further changes in the loops go with the observer:

- **A budget stop does not mark the cluster consolidated.** A search stopped by the budget before any accepted prefix sets `constrainedByBudgets` in both methods, so the next pass searches again with a fresh budget. Today, a multi-node deadline expiry with no accepted prefix goes through the no-op path, which marks the cluster consolidated whenever no NodePool budget held a candidate back — rare under a 10% budget, usual under a permissive one — and `IsConsolidated` then skips multi-node until the cluster state changes.
- **The single-node scan resumes where it stopped.** A cursor on `SingleNodeConsolidation` is set when `Next()` or `UseFull()` is refused, to the candidate before the refused one whether that one was simulated or skipped, and kept at its old value when the refusal comes before any candidate; the next scan starts after it and wraps around. It is cleared when the next scan starts, not set on a deadline stop, and ignored when its candidate is no longer a candidate; a budget stop sets `PreviouslyUnseenNodePools` as a completed scan does. Consecutive capped scans that find nothing therefore walk on through the candidate order instead of restarting at its head. This is a progress rule, not a coverage guarantee: a scan that executes a command or stops on the deadline leaves no cursor, and with several NodePools the interleaving can change between passes. Without it a binding budget would re-simulate the same leading candidates every pass and leave the tail of the savings-ratio order unreachable.

This is the one change on the hot path of both searches. The mitigations are the nil observer with the gate off, the parity test extended with budget-capped states (a capped multi-node search must return a prefix the uncapped search also accepted; a capped single-node scan must simulate a contiguous run of the uncapped scan's candidate order from its cursor and give each candidate the verdict the uncapped scan gives it), and the fine adapters as the fallback design.

**What "stop at the budget" means.** The multi-node search probes prefixes until a probe is refused, then keeps the best prefix found so far; if that is nothing, the pass moves to the single-node round with whatever budget is left. The single-node scan stops at the candidate it has reached and records its cursor. In both cases the pass emits whatever was accepted before the budget ran out, validated, and the round summary records `exhausted: 1` with the attempts and simulations this round spent and `unreached` with the candidates the budget cut off.

**The recorder.** `Recorder.Record` is called synchronously at each stage. `none`, the default, discards. `log` writes round summaries at V(1) and per-move events at V(2) through the `consolidation-pipeline` logger with the message `consolidation decision` and the event's fields as key-value pairs. `file` appends one JSON object per line to its path, synchronously and buffered, with size rotation; under load it slows the pass rather than dropping events, since a file with gaps would be incomplete for the pass it describes. A write error disables the recorder for the rest of the process, with one log line, and is never returned to the method. A recorder can never fail a pass. No map iteration order reaches a logged list and `detail`'s keys are sorted, so in the simulator (fake clock, one NodePool, fixed candidate order) the same seed, configuration and commit produce byte-identical logs; in the controller, `t` is real time and the single-node scan order is Go map order across NodePools, so two passes on the same state log the same decisions, not the same bytes.

**Code changes this RFC asks for**, beyond the new `pipeline` package: (1) one `SearchBudget` per pass, forked per round; (2) the `SearchObserver` hook in the two searches and `SetSearchObserver` on `consolidation`; (3) `constrainedByBudgets` set on a budget stop in both methods; (4) the single-node resume cursor; (5) the allowlist, the dropped `strategies/all` import, the dropped configuration keys and the rejection of `mode: fine`; (6) recorder selection by name, the V(1)/V(2) split and the `file` recorder; (7) `budget_remaining` set on every event; (8) the metrics below. The new `pipeline` package is about 5,800 lines with its tests and the parity harness. The `Potential` interface stays (the scorer holds one) and the method always runs with the `none` potential. Of the pipeline RFC's two well-known annotations, this RFC registers only the simulated volume-zone annotation that `Fast` reads (internal-only; only the simulator sets it), not the shift-source-since annotation, which only the shift executors set.

### Interaction with Existing Features

| Feature | With the gate on, default configuration |
|---|---|
| NodePool disruption budgets | Unchanged. The method reports the `Underutilized` reason, so today's budgets apply; the adapters consult them where the existing methods do. |
| `karpenter.sh/do-not-disrupt` | Unchanged: candidacy is today's. |
| PodDisruptionBudgets | Unchanged: today's candidate filter and the Eviction API. |
| `consolidateAfter` | Unchanged: sources are this pass's consolidation candidates, which the existing methods filter by the `Consolidatable` condition. |
| `consolidationPolicy: Balanced` | Unchanged. The adapters call the existing evaluator, so Balanced metrics and `ConsolidationApproved` events are emitted exactly as today; the `upstream` planner's re-check logs the `consolidation score` debug line a second time for the returned command. With the observer, each probe's evaluator verdict also becomes a `moves` event. |
| `SpotToSpotConsolidation` | Unchanged: the rule lives in `computeConsolidation`, which the adapters call. |
| Emptiness, Drift, StaticDrift, Repair, expiration | Unchanged, in the same order, with the same reasons and budgets. The pipeline replaces only the two consolidation methods. |
| `IsConsolidated` | Kept. Each method skips its search while the cluster's consolidation state is unchanged since a pass in which it found nothing and was not held back by a NodePool budget. Multi-node skips `markConsolidated` whenever a NodePool has more candidates than its budget allows (nearly always under a 10% budget, because it decrements the budget per kept candidate); single-node never decrements the mapping, so a completed scan normally marks the cluster consolidated. A pass stopped by the search budget never marks it, so a budget cannot turn a truncated search into a skipped one. |
| Validation | Unchanged: the same validators and the same 15-second delay, inside the adapters. Validation simulations are outside the search budget. |
| Orchestration queue, termination, drain | Unchanged: commands reach `Queue.StartCommand` through `disrupt()` exactly once. |

The search budget is the one new interaction. With `maxFullSimulations` set, the multi-node search can stop before its window closes and the single-node scan before its candidates run out, so a pass under a budget may consolidate less than an unbounded pass would. That is the point of the budget, and it is the same behavior as today's time limits, except that it is counted, and machine-independent up to the NodePool interleaving whenever it binds before a time limit.

### Observability

Each dimension below is a `metrics.Label` with its stable values enumerated as `metrics.Value`s; `consolidation_type` reuses the existing label with the new value `pipeline`. All are in the `voluntary_disruption` subsystem.

| Metric | Type | Dimensions | Meaning |
|---|---|---|---|
| `consolidation_search_attempts_total` | counter | `consolidation_type`, `strategy` | Generation attempts: one per multi-node probe and one per single-node candidate simulated |
| `consolidation_full_simulations_total` | counter | `consolidation_type`, `strategy` | Scheduling simulations the search ran; validation simulations are not counted |
| `consolidation_search_stopped_total` | counter | `consolidation_type`, `limit` (`attempts`, `full_simulations`, `timeout`) | Passes whose search stopped at a budget. `timeout` is the pipeline's own `search.timeout`; the existing one- and three-minute limits keep incrementing `consolidation_timeouts_total` |
| `consolidation_moves_considered_total` | counter | `consolidation_type`, `stage`, `outcome` (`accepted`, `no_op`, `not_cheaper`, `evaluator`, `budget`, `threshold`, `validation`, `other`) | Probes and moves by the verdict each stage gave them; `outcome` buckets the decision log's `reason` |
| `consolidation_search_budget_remaining` | gauge | `consolidation_type` | Simulations left when the last pass ended; 0 means the pass spent its whole cap, whether or not a probe was then refused. Not set when `maxFullSimulations` is 0 |

How to use these: if `consolidation_search_stopped_total{limit="full_simulations"}` grows on most passes, the budget is binding and the single-node scan is making progress only through its cursor; raise `maxFullSimulations` or accept slower convergence. If it never grows and `consolidation_full_simulations_total` stays far below the cap, the budget costs nothing and the time limits are what bound the search. If `consolidation_moves_considered_total{outcome="budget"}` dominates, NodePool budgets, not the search, are what hold consolidation back. The `log` recorder at V(1) gives one summary line per generator per pass (attempts, simulations, moves, what stopped the search); V(2) adds every probe.

**Log volume.** Under the observer each scheduling simulation is one `moves` event: at most 7 for the multi-node binary search plus one per single-node candidate simulated, bounded by `maxFullSimulations` when it is set. Each skipped candidate is one more event, and the cap does not bound those: under a 10% budget the multi-node filter drops most candidates, so these alone run to hundreds per pass at 1,000 nodes. None of it belongs at V(1); the `file` recorder is the sink for anyone who wants every event without raising the log level.

**Events.** Today's `Unconsolidatable`, `ConsolidationApproved` and `ConsolidationRejected` events are emitted by the code the adapters call. `BuildSnapshot` calls `podutils.IsDisruptable` with the event recorder for every pod, so the existing `karpenter.sh/do-not-disrupt` annotation events (invalid value, duration-based protection still active, grace period elapsed) are published once more per round than today; the recorder dedupes each per pod for two minutes.

### Edge Cases

- **The budget runs out mid-pass.** The multi-node search keeps its best accepted prefix and stops; if nothing was accepted the pass moves to the single-node round with the budget that is left. Whatever was accepted is validated and emitted; nothing is emitted unvalidated. Neither method marks the cluster consolidated, so the next pass searches again.
- **The budget is smaller than one probe.** `maxFullSimulations: 1` lets the multi-node search run exactly one simulation and the single-node scan none. Validation is outside the budget, so a pass runs at most `maxFullSimulations` plus one validation simulation per method. The configuration is accepted; a value below 0 is a startup error.
- **The recorder blocks or errors.** `Record` is synchronous, so a slow sink slows the pass. A write error disables the `file` recorder for the rest of the process with one log line. A recorder can never fail a pass.
- **The gate is flipped at runtime.** Feature gates are read at startup; changing the gate or the configuration requires a restart. No shift executor ships, so nothing is left on nodes when the gate is turned off.
- **An unknown strategy name, an experimental strategy, or `mode: fine`.** Construction fails at startup with the allowed names. There is no fallback to the default, so a typo cannot silently run today's search while the operator believes a budget is set.
- **A multi-NodePool cluster.** Today's single-node scan interleaves NodePools in Go map order, so the existing method itself is nondeterministic there; the adapters inherit that, and the parity test compares against the set of decisions it makes over repeated runs. A budget does not change this.
- **A pass with no candidates.** `disrupt()` returns before calling the method, so no decision event is recorded for that pass, as today.

## Example

A 1,000-node NodePool under `consolidationPolicy: WhenEmptyOrUnderutilized` with a 10% disruption budget runs the configuration in the Proposed Spec. Suppose a pass has 864 candidates, of which the budget filter keeps 100 for the multi-node search.

The multi-node binary search probes prefixes of 51, 76, 63, 69, 66 and 67 candidates (six simulations), accepts the 66-candidate prefix, and the planner and validator confirm it. The round summary reads `moves: 1, attempts: 6, full_simulations: 6, exhausted: 0, budget_skipped: 764` and the pass ends; `consolidation_search_budget_remaining` is 10. Each of the six probes is a `moves` event at V(2); the three rejected prefixes (76, 69, 67) carry their reason (`no-op`; under `Balanced` it could also be `evaluator rejected`), and each of the 764 budget-skipped candidates is a `moves` event with reason `nodepool budget`.

On a later pass nothing multi-node is found in 6 simulations (every prefix rejected: 51, 26, 13, 7, 4 and 2 candidates), and the single-node scan simulates ten candidates, the 10 simulations left of the 16, before the next `UseFull()` is refused (a refused probe counts as neither an attempt nor a simulation): `exhausted: 1, attempts: 10, full_simulations: 10, unreached: 854`, `consolidation_search_stopped_total{limit="full_simulations"}` increments, the cluster is not marked consolidated, and the next pass's scan resumes after the last candidate reached. Today the same pass would have scanned all 864 candidates, about a minute at 67 ms per simulation and longer on a slower machine, and, finding nothing, marked the cluster consolidated if no NodePool budget had held a candidate back; only a machine too slow to finish inside the three-minute limit would have stopped early, and then without marking it.

## Evidence

The pipeline was measured in an in-process simulator (3-hour simulated runs of synthetic fleets on four workloads at 50 to 1,000 nodes, ten runs per configuration) and on a KWOK cluster at 50 and 200 nodes. What bears on this RFC:

- **The adapters reproduce today's decisions.** The parity test runs the two existing methods and then one pipeline pass on generated cluster states (4-40 nodes, one to three NodePools, two instance catalogs, both consolidation policies) and requires identical decisions: kind, candidates in order, replacement instance types in order, budget consumed per NodePool. 10,000 of 10,000 states matched for the coarse adapters, the fine adapters, and the fine adapters with the Balanced planner, on states where the existing methods themselves acted in 83-91% of cases. Injected bugs in the fine adapters (dropping the multi-node budget decrement; accepting every move) were caught at 44 of 60 and 85 of 100 states; skipping the `CanPass` pre-filter was not and cannot be, since it changes compute but not decisions, which is why the budget needs its own metric. A 1,000-state run takes about 21 minutes; a 40-state run is in `make test`.
- **Configured as today's methods, the pipeline matches them over whole runs.** Over 3-hour simulated runs at 200 nodes its steady-state cost was within 1% of today's on the three workloads measured, and 7 of 60 runs matched exactly on four metrics. On the KWOK cluster end cost differed by at most 1.7% (50 nodes) and 0.3% (200 nodes), with no measurable difference. These runs used the fine adapters, which pass the same parity test; the coarse adapters proposed here are covered by the parity test only.
- **Its overhead is small and not consistently in one direction.** In the simulator at 200 nodes it spent 5-16% less CPU per disruption pass than today's methods (93 vs 111 ms on the diurnal workload); on the compressed cluster workload it spent 3.7% more per pass at 50 nodes and 4.6% more at 200 on the five runs paired with the KWOK runs, and +2.6% and -0.03% over all ten, neither distinguishable from noise. On the KWOK cluster at 200 nodes over 30 minutes it used 153.8 controller core-seconds against 148.8 and 227 MB of peak memory against 216, again within noise.
- **A budgeted search reproduces its decisions on another machine.** Twenty 1,000-node simulator runs, ten of a random generator and ten of the pipeline's copy of today's search, both under a 16-simulation budget, rerun on a different VM, reproduced their decision logs up to run IDs and their summaries exactly, while mean CPU per pass changed by 12% for the random generator and under 1% for the copy of today's search. Today's time-bounded search was not rerun; its machine dependence follows from the `context.WithTimeout`. Whether the one-minute limit binds on real clusters was not measured; no simulated pass reached it.
- **The decision log explains costs the metrics cannot.** Why random move generation costs more CPU per pass than a savings-ordered search was answered entirely from per-pass counts of attempts, simulations and moves, none of which exist as metrics today. From three runs each of a 1,000-node fleet over 3 simulated hours, where "heuristic generation" is today's candidate order and searches run as move generators under an entropy-guided planner (which keeps searching past the first acceptable move, so its per-search figures are not today's own):

  | 1,000-node fleet, three runs each | Share of passes that search | CPU per searching pass | Simulations per search | Moves per search |
  |---|---|---|---|---|
  | Fragmented workload: heuristic generation / random generation | 0.99 / 0.48 | 1.5 s / 2.0 s | 11.1 / 16.0 | 2.5 / 25 |
  | Diurnal workload | 1.00 / 0.31 | 0.66 s / 3.6 s | 7.0 / 16.0 | 2.0 / 30 |

  The pipeline RFC's early-stopping comparison was read from the same counts; a budget and a log make that kind of question answerable in the field as well as in the simulator.
- **Plan fidelity is low and measurable.** In the simulator under default kube-scheduler scoring, about one in four pods evicted by the pipeline's experimental configuration landed on its planned node (fidelity 0.25), and today's own commands landed on plan for 45% of evicted pods. Steering replacement pods toward their planned node raised the experimental configuration's fidelity to 0.43; `MostAllocated` scoring raised it by 29-44%. Nothing in the controller measures this today; `bindings` in the decision log is the controller's half of the measurement.

## Alternatives Considered

### Alternative 1: Keep the time limits only

Leave `MultiNodeConsolidationTimeoutDuration`, `SingleNodeConsolidationTimeoutDuration` and the 101-candidate window as the only bounds. This is today. Decisions keep depending on the machine, and there is still no way to say how much work a pass did. Rejected because the budget is a strict addition: with the budget unset, the time limits and the window are all there is, exactly as today.

### Alternative 2: Extend today's logging

Add fields to the orchestration queue's `disrupting node(s)` log line and the `Unconsolidatable` events instead of a structured per-move record. This cannot answer "why not" for a node the search never probed, cannot show the multi-node prefixes tried before a command was accepted or any probe's predicted savings, and cannot carry per-pod planned destinations. Rejected; the decision log subsumes it and its default recorder is a no-op.

### Alternative 3: A full strategy plugin API now

Expose the stage interfaces as a supported extension point so operators can ship their own generators and planners. The measurements argue against doing this yet: no strategy beat today's search on cost, disruption and compute together, and the interfaces changed as the experimental strategies needed them (a `Guide` seam, a pooled generation mode, pod-eviction budgets). A plugin API would freeze them. Rejected for now: the seams ship, the allowlist admits only the adapters, and the registry is not a public API. Whether the recorder should be public is [Open Question 2](#open-questions).

### Alternative 4: Budgets without the seams

Thread a `SearchBudget` into the two existing search loops (the observer above) and stop there: no pipeline method, no stage interfaces, no snapshot. This is the smallest change that makes the search counted and, when the budget binds first, machine-independent. What it loses is the decision log's structure (a record per stage rather than per simulation), the parity test that pins the adapters, and the ability to compare a different search or acceptance rule at equal compute. Rejected on its own, but accepted as the first step of the proposal: the observer in the two search loops is the same code either way.

### Alternative 5: Ship the fine adapters instead of the coarse ones

The pipeline RFC's fine adapters re-implement the multi-node binary search and the single-node scan in stages and consult the budget and the planner at every step. They pass the same 10,000-state parity test, were the configuration checked over whole runs and on KWOK, bind the budget without touching the existing loops, carry the resume cursor already, and make the planner swappable. Their cost is a second copy of the two searches that every change to the existing methods must be mirrored into, with the parity test as the only thing that catches a drift, and they do not emit Balanced's metrics and events, because the fine planner mirrors the evaluator rather than calling it. Not chosen for alpha, where the lower-risk path is today's code by construction; kept as [Open Question 3](#open-questions).

## Backward Compatibility

- **Gate off (the default).** `NewController` builds exactly today's methods; the pipeline packages are compiled in but the method is never constructed, and the observer in the two search loops is nil. The KWOK runs with the gate off confirmed that only multi-node and single-node evaluations appear in the controller's metrics. This is by construction: no test compares gate-off output byte for byte.
- **Gate on, flag empty.** The decisions are today's, as the parity test checks on 10,000 states. The visible differences are `consolidation_type="pipeline"` on `decision_evaluation_duration_seconds`, `decisions_total`, `decisions_by_nodepool_total` and `queue_failures_total` (`consolidation_timeouts_total` and `failed_validations_total` keep `multi` and `single`), and the existing `karpenter.sh/do-not-disrupt` annotation events that building the snapshot republishes once more per round. Dashboards that filter on `consolidation_type` need the new value.
- **Gate on with a budget or a recorder.** New behavior by definition: the search may stop earlier than today's limits would stop it, a budget-stopped pass does not mark the cluster consolidated, and log volume rises at V(2). No node, NodeClaim or pod is touched differently.
- **Rollback.** Turning the gate off restores today's methods at the next start. Nothing is left on cluster objects.
- **Configuration.** The flag is new; existing deployments are unaffected. There is no CRD or NodePool change, and no change to any annotation users set; the one annotation this RFC registers is the simulator's internal-only volume-zone annotation. Configuration errors fail at startup, never silently.
- **Schema.** The decision log schema this RFC ships is version 2; version 1 stays readable (its `budget_remaining` is always 0 and it has no `bindings`). Consumers read `schema_version` first.

## Graduation Criteria

**Alpha.** The `ConsolidationPipeline` gate, default off. An empty flag selects the coarse adapters with no budget and no recorder; the supported configurations are the default, a `search` block and a `recorder` block. Too risky to enable by default because the observer is new code on the hot path of both searches, the metrics are new, and the log volume at V(2) is unmeasured on large clusters.

**Beta.** All of the following:

1. The parity test runs in CI: the 40-state run in `make test` on every change (exists), and the 10,000-state run (about 4 hours, one job per policy and instance catalog) as a weekly scheduled workflow owned by the RFC author until beta, with no unexplained mismatch, including the budget-capped states added for the observer.
2. An equivalence check on KWOK: the gate on with the default configuration against the gate off, for at least 30 runs per configuration; end cost within 3%; no systematic difference in evictions; and every executed `execute` event's command equal to the command the orchestration queue logged for that pass. Thirty runs is the count needed to resolve a 2% end-cost difference at 200 nodes. Evictions vary too much between runs for a 3% margin at that count, so the eviction criterion is only that no systematic difference is found.
3. The decision log schema is documented in `designs/` with its version, and a change to it bumps the version in the same PR.
4. The metrics above reviewed by the SIG, with `consolidation_search_stopped_total` and `consolidation_moves_considered_total` at minimum.
5. At least one real-cluster run of the gate on against the gate off, with the team that runs and pays for it named before beta.
6. Open Questions 1 and 3 resolved.

The gate still defaults off at beta; nothing in this RFC changes a default.

**GA.** Out of scope. It needs production feedback on the budget and the recorder, a decision on whether the two existing methods are retired behind the adapters, and a decision on asynchronous generation.

## Open Questions

1. **Should search budgets be per NodePool?** The budget proposed here is per pass, which matches where the search runs: one multi-node search over all candidates, one single-node scan interleaving NodePools. A per-NodePool budget would let a large pool be searched harder than a small one, but the multi-node prefix search does not partition by pool. Proposed: per pass at alpha; revisit with field data from `consolidation_search_stopped_total`.
2. **Should the `Recorder` be a public extension point?** It is the stable part of the design (one method, one versioned event type) and the part operators would most plausibly want to plug in a metrics pipeline or an audit sink. Making it public makes the event schema an API. Proposed: internal at alpha with the three built-in sinks; decide at beta with the schema documented.
3. **Coarse adapters with an observer, or the fine adapters?** The observer is a small change inside the two searches; the fine adapters are a second copy of them pinned by a parity test, with the whole-run and cluster measurements behind them. Which do maintainers prefer to own?
4. **Should the pipeline-level gate replace `IsConsolidated`?** The multi-node method counts itself as budget-limited almost always under a 10% budget, so it re-searches on nearly every pass and most of those searches execute nothing; that is where a large share of today's decision CPU goes. Replacing the rule changes how often the search runs, which is behavior, not instrumentation. Proposed: keep `IsConsolidated` at alpha and decide at beta from `consolidation_search_stopped_total` and the round summaries.
5. **How much of a probe goes in the log?** With the observer, each multi-node prefix and each single-node candidate simulated is one `moves` event with the command it produced or the reason it was rejected. Whether the probe's full placement (every pod's planned node, the `bindings` of schema version 2) belongs in the `log` recorder at V(2) or only in the `file` sink is a volume question for beta.
6. **Snapshot cost under the coarse adapters.** `BuildSnapshot` runs once per round (a NodePool list with `GetInstanceTypes` per NodePool, a PDB list, a `DeepCopyNodes` and a pod list per node), so twice in a pass whose multi-node round executes nothing, and has not been profiled at any scale. The coarse adapters use it only to filter sources and convert the returned command. Proposed: profile at 1,000 nodes before beta, and build a reduced snapshot (candidates and their pods only) for the default configuration if it is not small next to one scheduling simulation.
7. **A plan-fidelity metric.** A `consolidation_plan_fidelity` gauge would need a component that pairs the log's per-pod planned destination with pod binding events over a trailing window, matching by controlling owner (the replacement is a different pod) and mapping a replace command's launch index to the NodeClaim the queue creates. Proposed: ship `bindings` at alpha and decide the gauge at beta, once field logs with `bindings` exist.
8. **Should the observer also replace today's time limits?** Under the gate, the one-minute context and the three-minute clock check still run beside the budget. Replacing them with the budget's deadline would make `search.timeout` the only clock, at the cost of a second change inside the loops. Proposed: keep both at alpha, so the gate changes nothing a time limit does today.

## Frequently Asked Questions

### Does a budget make consolidation find less?

Yes, when it binds: a pass under a budget may consolidate less than an unbounded pass would, exactly as a pass that hits today's one-minute limit does. The difference is that the budget is counted, the same on every machine, and visible in `consolidation_search_stopped_total`; and the single-node cursor keeps a capped scan moving through the candidate list instead of restarting at its head. With no budget set, nothing changes.

### Why not make the budget the only limit and drop the time limits?

Because the time limits are what today's users rely on, and the gate should change nothing a time limit does. Making `search.timeout` the only clock is [Open Question 8](#open-questions).

### Is the gate-on, flag-empty configuration byte-for-byte today's behavior?

Its decisions are, as the parity test checks on 10,000 states. Its metrics carry a new `consolidation_type` value, and the snapshot it builds publishes the existing `karpenter.sh/do-not-disrupt` annotation events (invalid value, duration-based protection active, grace period elapsed) once more per round. Whole-run and cluster equivalence were measured with the fine adapters, which share the parity test but are not what ships; the beta criteria measure the shipped configuration directly.

### Will any of the experimental strategies ship later?

Only on evidence the measurements have not produced. Random and entropy-guided generation did not beat today's search on cost, disruption and compute together, and nothing here proposes them. The seams make such a proposal possible to evaluate at equal compute if one comes.

## References

- [Pluggable consolidation pipeline](consolidation-pipeline.md): the full design this RFC is the first increment of.
- [Balanced consolidation](balanced-consolidation.md): the issue reports behind Use Case 2 and the open question on asynchronous generation.
