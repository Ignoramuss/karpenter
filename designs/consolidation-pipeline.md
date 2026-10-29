# Pluggable Consolidation Pipeline

Consolidation becomes four pluggable stages (candidate selection, move generation, planning within budgets, and
execution) with shared models for potential, feasibility, disruption and savings. The work sits behind a new alpha
feature gate, `ConsolidationPipeline`, which is off by default. With the gate on and no configuration, the pipeline's
upstream adapters make the same decisions as today's multi-node and single-node consolidation. A differential parity
test checks this.

**Status: draft.** The interfaces, adapters, parity test and simulator exist on the research branch. The
session-profile results (Tier A only, with four groups cut) are in
[`experiments/RESULTS.md`](../experiments/RESULTS.md). This document quotes no experimental numbers. The figures it
does quote are parity results and timing measurements recorded in
[`experiments/RESEARCH_LOG.md`](../experiments/RESEARCH_LOG.md).

## Motivation

Consolidation runs as two disruption methods, `MultiNodeConsolidation` and `SingleNodeConsolidation`. Each one
orders candidates, generates moves, decides whether to accept them and validates them inside a single
`ComputeCommands` call. The steps are interleaved, so none of them can be changed or measured on its own.

- **Multi-node.** `ComputeCommands` sorts candidates with `sortCandidates` (savings ratio, descending), drops those
  their NodePool's budget cannot cover, and runs `firstNConsolidationOption`. That function binary-searches over
  prefixes of the sorted list, using windows of up to 101 nodes. Each step calls `computeConsolidation` (which runs
  `SimulateScheduling`), then `filterOutSameInstanceType`, then `Evaluator.ApproveCommand`. Whether the window grows
  or shrinks depends on the acceptance test, so search and acceptance cannot be separated. The search stops at
  `MultiNodeConsolidationTimeoutDuration` (1 minute). That limit uses a wall-clock `context.WithTimeout`, so the
  decision can depend on machine speed. Validation, which includes a 15 s wait (`commandValidationDelay`), runs inside
  the same call.
- **Single-node.** `ComputeCommands` walks candidates interleaved by NodePool (`shuffleCandidates`). For each one it
  checks the budget, then `CanPassThreshold`, simulates, calls `ApproveCommand`, and stops at the first acceptable
  command, which it then validates. `SingleNodeConsolidationTimeoutDuration` (3 minutes, on the injected clock) bounds
  the scan. `PreviouslyUnseenNodePools` carries scan order from one pass to the next.

This structure has four consequences.

1. **Stages cannot vary independently.** Changing how candidates are ordered, which moves are tried, or how they are
   accepted means editing the search loop. Balanced's acceptance test (the `Evaluator` seam in `balanced.go`) is
   called from inside both loops, so it cannot be replaced without touching them.
2. **Work is bounded by time, not by explicit compute.** A full single-node scan costs one scheduling simulation per
   candidate, and each simulation deep-copies every `StateNode`. In the simulator's design spike on the 4 vCPU
   research VM, one `SimulateScheduling` at 1,000 nodes took 67 ms. A full scan at that size is estimated at about
   58 s (864 candidates × 67 ms), a third of the 3-minute limit. The multi-node limit is wall clock. There is no way
   to compare two strategies at equal compute.
3. **Every move deletes a node.** Delete and Replace both remove their sources, and Balanced scores only those moves.
   A move that improves packing without removing a node (moving some pods from a sparse node to a fuller one) cannot
   be expressed, so it has no value to score.
4. **Rejected alternatives are invisible.** Upstream logs and emits events for the command it starts, emits
   Unconsolidatable events for candidates, and records metrics for scored moves. It keeps no structured record of
   every move it considered and why each one lost.

### The Balanced RFC's open question

[Balanced consolidation](balanced-consolidation.md) lists asynchronous move generation as an open question:
"Scoring enables a shift from synchronous per-cycle consolidation to a continuous pipeline: generate, score, enqueue by
priority, execute as budget allows. This eliminates the per-cycle timeout problem."

This RFC builds the structure that question needs. Generation, scoring, planning and execution become separate stages
with explicit inputs and outputs. Work per cycle is capped by a search budget instead of a timeout. Every decision is
logged. This RFC does **not** make generation asynchronous: all four stages still run inside one `ComputeCommands`
call per disruption pass. A background generator that feeds a priority queue is listed as future work (Open Question
8).

### Why moves that delete no node need a value

The [pod-deletion-cost controller RFC](pod-deletion-cost-controller.md) describes a common way clusters fragment.
ReplicaSet scale-down spreads deletions across nodes, so every node ends up partly full and none becomes empty. Each
remaining merge must evict every pod on its source. Under Balanced, such merges can fail the threshold because
disruption is large relative to savings.

A **shift** moves a few pods from a sparse node to a fuller one and removes nothing. It saves nothing immediately, so
any savings-based score rates it at zero or below. Its value is deferred: once a sparse node has been emptied over
several shifts, Emptiness removes it, and the last step evicts no pods at all.

To rank shifts, the planner needs a **potential** Φ: a function of the cluster state that falls as nodes move toward
packed or empty. With such a potential, a shift has a measurable ΔΦ < 0 now, and its disruption can be weighed against
that drop. The potentials used here are concave in node load. A concave function over the feasible load polytope is
minimized at a vertex, where every node is empty or full except at most one, which is the fleet shape consolidation is
after. [`docs/research/PLAN.md`](../docs/research/PLAN.md) ("Formal model") gives the derivation.

### Pluggability for experimentation

Several questions about consolidation can only be answered empirically. Is random move generation as good as the
upstream heuristics when the planner is good? Does annealing at plan time beat greedy acceptance? How much of a
planned packing does kube-scheduler undo? Answering them needs stages that can be swapped one at a time while the
others stay fixed, equal compute budgets, seeded reproducibility, and one decision-log format shared by an in-process
simulator and a KWOK cluster. The research plan in [`docs/research/PLAN.md`](../docs/research/PLAN.md) defines these
experiments. The pipeline is the substrate they run on.

### Use Cases

1. **Evaluating an acceptance rule.** A maintainer wants to try Balanced with a different `k`, or a rule that also
   values the potential. Only the planner changes. Candidate selection, generation and execution stay on the upstream
   adapters, so the parity test still pins those stages to upstream behavior.
2. **Large clusters.** At 1,000 nodes or more, a full single-node scan costs tens of seconds of simulation per
   pass. An operator, or an experiment, caps full simulations per cycle (`search.maxFullSimulations`) and samples
   candidates instead of scanning all of them.
3. **Fragmentation after scale-down.** Every node is 30-60% utilized and no merge clears the Balanced threshold. A
   planner that accepts shifts with ΔΦ < 0, under a pod-eviction budget, empties nodes over several cycles, and
   Emptiness then removes them.
4. **Explaining a decision.** An operator asks why node X was not consolidated. The decision log records every move
   considered in the cycle, its scores, and the reason it was rejected (budget, threshold, conflict, validation churn).

### Goals

- Four stage interfaces and shared services, with every stage chosen by name from configuration.
- Upstream adapters that reproduce today's multi-node and single-node decisions exactly, in two forms: a coarse form
  that wraps `ComputeCommands`, and a fine form that splits generation, acceptance and validation across stages.
- No behavior change with the gate off. With the gate on and no configuration, the same decisions as today.
- Explicit per-cycle search budgets (generation attempts, full simulations, a deadline on the injected clock), so that
  strategies are compared at equal compute.
- Shift moves with an execution path that respects PodDisruptionBudgets and `karpenter.sh/do-not-disrupt`, plus
  pod-eviction budgets that node-count budgets cannot provide.
- Reproducibility: one seeded `*rand.Rand` passed through every stage, time read only from the injected clock, and a
  versioned decision log.
- A small diff to upstream files. New code lives in new packages.

### Non-Goals

- No CRD, NodePool or NodeClaim API changes, and no new user-facing annotation.
- No change to any default. Neither Balanced nor any new strategy becomes a default policy.
- No asynchronous or continuous generation in this RFC. That is future work built on these interfaces.
- No change to Emptiness, Drift, StaticDrift, Repair, the orchestration queue or termination.
- No changes to kube-scheduler or the ReplicaSet controller. Plan fidelity is measured, not enforced.
- Not a descheduler. The pipeline only acts on consolidation candidates, within Karpenter's budgets.
- No production recommendation for any experimental strategy until the experiments report.

## Proposal

With `--feature-gates ConsolidationPipeline=true`, the disruption controller runs one method,
`PipelineConsolidation`, where multi-node and single-node consolidation used to run. The method composes four stages
and shared services named in a configuration file passed by `--consolidation-pipeline-config`. An empty value selects
the upstream adapters.

### Proposed Spec

**Feature gate and flag** (`pkg/operator/options/options.go`)

| Item | Value |
| --- | --- |
| Feature gate | `ConsolidationPipeline` (`ConsolidationPipelineFeatureGate`), alpha, default `false`, listed in `KarpenterFeatureGates` |
| Flag | `--consolidation-pipeline-config`, env `CONSOLIDATION_PIPELINE_CONFIG`, default empty |
| Flag value | A file path, or the config itself as YAML or JSON. A value that starts with `{` or `pipeline:`, or spans lines, is parsed inline (`LoadConfig`). |
| Empty value | `DefaultConfig()`: the upstream adapters |
| Invalid value | The operator panics at startup with the parse or validation error (`disruptionMethods` in `pkg/controllers/controllers.go`) |

**Config schema** (abridged from `pkg/controllers/disruption/pipeline/config.go`, comments condensed)

```go
type StageConfig struct {
	Name   string `json:"name"`
	Params Params `json:"params,omitempty"`
}

type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"` // one object or a list
	MovesMode  MovesMode    `json:"movesMode,omitempty"` // "sequential" (default) or "pooled"
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Potential  StageConfig  `json:"potential"`
	Seed       int64        `json:"seed"`
	Search     SearchConfig `json:"search,omitzero"`
	Budgets    BudgetConfig `json:"budgets,omitzero"`
}

type SearchConfig struct {
	MaxAttempts        int    `json:"maxAttempts,omitempty"`        // generation attempts per generator; 0 = unlimited
	MaxFullSimulations int    `json:"maxFullSimulations,omitempty"` // full simulations per cycle; 0 = unlimited
	Timeout            string `json:"timeout,omitempty"`            // deadline on the injected clock, e.g. "1m"
}

type BudgetConfig struct {
	PodEvictionsPerMinute      int `json:"podEvictionsPerMinute,omitempty"`      // per NodePool; 0 = unlimited
	WorkloadEvictionsPerMinute int `json:"workloadEvictionsPerMinute,omitempty"` // per controlling owner; 0 = unlimited
}
```

Each strategy's factory decodes its `params` strictly with `Params.Decode`, so an unknown key or an out-of-range
value fails construction. The top-level keys are strict only in the unwrapped form today (Open Question 10).

**The default configuration.** This is equivalent to an empty flag:

```yaml
pipeline:
  candidates: {name: upstream}
  moves:
    - {name: upstream-multi, params: {mode: coarse}}
    - {name: upstream-single, params: {mode: coarse}}
  movesMode: sequential
  planner:   {name: upstream}
  executor:  {name: upstream-queue}
  potential: {name: none}
```

**An experimental configuration.** The values are illustrative and uncalibrated; the Phase 4 pilot calibrates them:

```yaml
pipeline:
  seed: 42
  candidates: {name: uniform-random, params: {sourceFraction: 0.25}}
  moves:
    - {name: random-merge, params: {maxSources: 3, confirm: true}}
    - {name: random-shift, params: {maxPodsPerMove: 4}}
  movesMode: pooled
  planner:   {name: anneal-plan, params: {lambda: 0.5, t0: 0.3, cooling: 0.98, temperatureUnit: per-node}}
  executor:  {name: shift-evict, params: {taintTTL: 10m}}
  potential: {name: util-entropy, params: {weight: price, resource: dominant}}
  search:    {maxAttempts: 500, maxFullSimulations: 16, timeout: 1m}
  budgets:   {podEvictionsPerMinute: 20, workloadEvictionsPerMinute: 2}
```

Both examples pass `ParseConfig`, and every strategy and potential they name builds with these params.

**Registered strategies.** `Registered()` lists them. The upstream adapters and potentials register in package
`pipeline`. The others register in `pipeline/strategies/{candidates,moves,planners,executors}`, which
`pipeline/strategies/all` imports. `pkg/controllers/controllers.go` imports `strategies/all`, so the operator binary
contains every strategy (Open Question 9).

| Stage | Name | Params (defaults) | Role |
| --- | --- | --- | --- |
| 1 | `upstream` | none | Upstream candidates in savings-ratio order |
| 1 | `uniform-random` | `sourceFraction` (0.25), `destinationFraction` (1.0) | Uniform sample of eligible nodes |
| 1 | `util-band` | `resource` (dominant), `fullThreshold` (0.95) | Sources below mean utilization; destinations above it |
| 1 | `potential-weighted` | `sourceFraction` (0.25) | Sources sampled in proportion to the potential drop from emptying them |
| 2 | `upstream-multi`, `upstream-single` | `mode`: `coarse` (default) or `fine` | Parity adapters |
| 2 | `random-merge` | `maxSources` (3), `launch` (true), `maxMoves` (50), `confirm` (false) | Empty k random sources first-fit, optionally confirmed by Full |
| 2 | `random-shift` | `maxPodsPerMove` (4), `full` (false) | Move 1 to m pods to a fuller destination |
| 2 | `greedy-shift` | `maxPodsPerMove` (4) | Per sampled pair, the pod set that most lowers Φ |
| 2 | `swap` | `strandingGap` (0.3) | Exchange pods between CPU-stranded and memory-stranded nodes |
| 3 | `upstream`, `savings-only` | none | First acceptable move within budgets; Balanced scoring where a NodePool asks for it |
| 3 | `balanced` | none | The same, with Balanced (k = 2) applied to every NodePool |
| 3 | `entropy-greedy` | `alpha` (1), `lambda` (0.5), `mu` (1) | Every downhill move, best ratio first, no two touching a node |
| 3 | `anneal-plan` | energy weights, `t0` (0.05), `cooling` (0.97), `temperatureUnit` (absolute), `steps` (400), `maxLength` (8) | Metropolis search over move sequences on the snapshot; runs only a net-downhill sequence |
| 3 | `anneal-online` | energy weights, `t0`, `cooling`, `temperatureUnit` | One Metropolis move per cycle on the cluster. Experiment-only. |
| 3 | `sequence` | energy weights, `horizon` (4) | Chains shifts that empty a node and scores the chain with its final delete |
| 4 | `upstream-queue` | none | Validate merges, return them as `Command`s for the orchestration queue |
| 4 | `shift-evict` | `validate` (true), `taint` (true), `taintTTL` ("10m") | Merges to the queue; shifts by the Eviction API with a `PreferNoSchedule` source taint |
| 4 | `steered` | `shift-evict` params, `mode` (affinity or most-allocated) | `shift-evict`, plus a request that each replacement pod prefer its planned node |
| 4 | `make-before-break` | `shift-evict` params | `steered` in affinity mode, evicting only into initialized destinations |
| - | potentials `none`, `load-entropy`, `util-entropy`, `util-quadratic` | `weight` (price), `resource` (dominant), `stranding` (0) | P1 to P3 from the plan; `none` for savings-only planners |

### How It Works

#### Stages and shared services

Stages 1 and 2 only propose. Stage 3 decides what runs, using the shared scoring models. Stage 4 carries the plan out.
The declarations below are quoted from `pkg/controllers/disruption/pipeline/interfaces.go`, with comments shortened.

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

// Guide is implemented by planners whose acceptance test search-guided generators consult while they search.
type Guide interface {
	Accept(ctx context.Context, s Snapshot, m Scored) bool
	CanPass(ctx context.Context, s Snapshot, sources []string) bool
}

type Executor interface { // stage 4
	Name() string
	Execute(ctx context.Context, p Plan) (Report, error)
}

// IdleExecutor is implemented by executors with housekeeping to do on cycles that execute nothing.
type IdleExecutor interface {
	Idle(ctx context.Context)
}

type Potential interface {
	Name() string
	Value(s Snapshot) float64
	Delta(s Snapshot, m Move) float64 // must equal Value(s.Apply(m)) - Value(s)
}

type Feasibility interface {
	Fast(s Snapshot, m Move) error                              // snapshot only
	Full(ctx context.Context, s Snapshot, m Move) (Move, error) // Karpenter's scheduling simulation
}

type Scorer interface {
	Score(s Snapshot, m Move) Scored
}

type Recorder interface{ Record(e DecisionEvent) }
```

`Guide` mirrors the `Evaluator` seam: `Accept` corresponds to `ApproveCommand` and `CanPass` to `CanPassThreshold`. It
exists because upstream's multi-node search consults acceptance at every step. A fine-mode generator asks the
configured planner through `Guide`, so swapping the planner also changes what the search finds, exactly as swapping the
evaluator does upstream.

**Moves** (abridged from `types.go`). A `Move` is a composition of actions that ends in a feasible state:

```go
type Move struct {
	ID        string
	Kind      MoveKind     // "merge", "shift", "rightsize"
	Sources   []string     // nodes losing pods
	Bindings  []Binding    // per pod: unbind from From, planned bind to Target (existing node or Launch index); sorted by pod key
	Launch    []LaunchSpec // add-node
	Remove    []string     // remove-node
	Generator string
	Upstream  any          // the upstream Command a merge corresponds to, for the queue
}
```

- **Merge.** Empty the sources into existing destinations plus at most one launched node, then remove the sources.
  Upstream Delete and Replace both convert to merges (`MoveFromCommand`).
- **Shift.** Move some pods to another node and remove nothing.
- **Right-size.** Replace one node with a cheaper one holding the same pods. The kind is defined, but no generator
  emits it yet; upstream single-node replaces arrive as merges.

**Scoring** (`score.go`, `DefaultScorer`). Every move gets:

- `DeltaPotential`: the configured `Potential.Delta`.
- `Disruption`: the sum of `max(0, EvictionCost)` over the pods the move binds, plus `PerNodeBaseDisruptionCost` (1.0)
  for each removed node. For a merge that binds every reschedulable pod of its sources, this equals the sum of the
  sources' `RescheduleDisruptionCost`, the numerator Balanced scores.
- `SavingsPerHour`: removed node prices minus launched node prices.
- `NormalizedDisruption` and `NormalizedSavings` (D̂, Ŝ): these divide by the NodePool totals Balanced uses (summed
  over the move's pools), so the planner weights are scale-free.

**Potentials** (`potential.go`). Each potential is normalized by constants of the root snapshot (`Normalizer`), never
by those of the fork being valued. This makes `Delta` exact, and removing an empty node is never penalized by a
shrinking denominator.

| Name | Definition | Note |
| --- | --- | --- |
| `load-entropy` (P1) | H(p) = −Σ pᵢ ln pᵢ, pᵢ = Lᵢ / Σ L, divided by ln N₀ | Blind to right-sizing |
| `util-entropy` (P2) | Φ = Σ wᵢ h(uᵢ), h(u) = −u ln u − (1−u) ln(1−u), wᵢ = price, divided by Σ w at the root | 0 for empty and full nodes, largest at 50% |
| `util-quadratic` (P3) | Σ wᵢ uᵢ (1 − uᵢ) | Cheaper smooth variant of P2 |
| `none` | 0 | Savings-only planners |

For P2 and P3, utilization is the dominant share by default (CPU, memory and accelerators; pod count is excluded).
They can instead use one resource, or combine resources by max or mean, and can add a stranding term
`stranding · |u_cpu − u_mem|`. P1 uses CPU requests by default. For nodes of equal price and capacity, moving load from the less utilized node to the more utilized one
always lowers P2 and P3, because h is concave. With price weights, the outcome also depends on prices and capacities,
which is why the weights exist.

**Feasibility** (`feasibility.go`, `upstream.go`).

- `FastChecker.Fast` uses only the snapshot. It checks that each source is a source, that each target is a
  destination, capacity, taints, node requirements (selectors and required node affinity), bound-volume zones and
  hostname anti-affinity. It also enforces `karpenter.sh/do-not-disrupt` on pods and nodes, PDB allowances, and that
  removed nodes are empty of reschedulable pods. It does not check topology spread, and it counts moves that carry
  such constraints (`TopologyUnchecked`).
- `UpstreamFeasibility.Full` runs Karpenter's scheduler against the live cluster. A merge is re-simulated through
  `ComputeConsolidation`, with `FilterOutSameInstanceType` applied to multi-source replaces, and comes back carrying
  the upstream `Command`. A shift is solved against its destinations only, with no new NodeClaims allowed.

**Search budget** (`SearchBudget`). This holds `MaxAttempts`, `MaxFull`, a `Deadline` on the injected `Clock`, the
planner's `Guide`, `StopAtFirst` (set for first-valid planners), and the `Feasibility` and `Scorer` that generators may
consult. Generators call `Next()` once per attempt and `UseFull()` before each `Full`. The method gives every
generator its own `Fork()`. Attempts are counted per fork, while full simulations, the deadline and the stop flags are
shared for the cycle. A generator that uses up its attempts therefore cannot starve the generators after it, and the
cycle stays under one full-simulation cap. `Stats()` reports usage for the decision log.

#### One cycle

```
disrupt() ─► Method.ComputeCommands(budgets, candidates)
               │  SetCycle(candidates, totals, budgets); skip if ConsolidationState unchanged (when no generator self-gates)
               ├─ BuildSnapshot ─► State (nodes sorted by name, pods, PDB allowances, totals)
               ├─ stage 1  selector.Select(snapshot, rng)                ─► sources, destinations
               ├─ for each round (sequential: one generator per round; pooled: all in one round)
               │    ├─ stage 2  g.Generate(..., rng, budget.Fork())     ─► moves
               │    ├─ Scorer.Score each move                           ─► Scored
               │    ├─ stage 3  planner.Plan(snapshot, scored, budgets) ─► Plan   (or IdleExecutor.Idle if empty)
               │    ├─ stage 4  executor.Execute(plan)                  ─► Report (merges come back as Commands)
               │    └─ a round that executes anything ends the cycle
               └─ record a DecisionEvent at every stage
disrupt() ─► Queue.StartCommand for each returned merge (unchanged)
```

In sequential mode the method reproduces how the controller runs multi-node and then single-node consolidation. Each
generator is its own round. The next round runs only if the previous one executed nothing. If the clock moved during
a round, for example during a validation wait, the method recomputes candidates, totals and budgets the way
`disrupt()` does before each method (`GetCandidatesWithTotals`, `BuildDisruptionBudgetMapping`).

When no generator tracks consolidated state itself (`SelfGating`), the method skips cycles while
`cluster.ConsolidationState()` is unchanged since a cycle that found nothing and was not constrained by budgets. This
is the same short-circuit upstream methods apply.

#### Where it plugs in

The disruption package does not import the pipeline. The pipeline imports the disruption package, so the switch lives
at the wiring layer:

- `disruptionMethods` in `pkg/controllers/controllers.go` returns no options when the gate is off. With the gate on,
  it returns `disruption.WithMethods(pipeline.Methods(...)...)`.
- `pipeline.Methods` loads the config, builds the method with `NewMethod`, and calls `ReplaceConsolidation` on
  `disruption.NewMethods(...)`.
- `ReplaceConsolidation` identifies `*MultiNodeConsolidation` and `*SingleNodeConsolidation` by type, puts the
  pipeline where multi-node was, and drops single-node. The order becomes Repair (when gated), Emptiness, StaticDrift,
  Drift, Pipeline.

| Method identity | Value |
| --- | --- |
| `Reason()` | `DisruptionReasonUnderutilized`, so today's budgets apply. A new reason would need a CRD enum change. |
| `Class()` | `GracefulDisruptionClass` |
| `ShouldDisrupt` | Consolidation's own (empty nodes, WhenEmpty pools and static pools are excluded, as today) |
| `ConsolidationType()` | `"pipeline"` (`PipelineConsolidationType`) |
| `SetNodePoolTotals` | Receives the cycle's Balanced totals and forwards them to the wrapped upstream methods |

The only changes to upstream-owned files are:

- `pkg/controllers/controllers.go`: `disruptionMethods` and its call site.
- `pkg/operator/options/options.go`: the gate and the flag.
- `pkg/test/options.go`: the test options.
- `pkg/controllers/disruption/metrics.go`: the documented `pipeline` value.
- `pkg/controllers/disruption/pipeline_hooks.go`: a new additive file of exports that change no behavior
  (`ReschedulablePods`, `InstanceType`, `Zone`, `CapacityType`, `ComputeConsolidation`, `SortBySavingsRatio`,
  `MarkConsolidated`, `FilterOutSameInstanceType`).

#### Upstream adapters: the coarse and fine split

| Stage | Adapter | Coarse (`mode: coarse`, default) | Fine (`mode: fine`) |
| --- | --- | --- | --- |
| 1 | `upstream` | This cycle's candidates as sources; the coarse generators hand them to upstream in the controller's order, and upstream sorts them itself | Sources in `SortBySavingsRatio` order; destinations are every node the snapshot marks as one |
| 2 | `upstream-multi` | Calls `MultiNodeConsolidation.ComputeCommands`, which includes budgets, the evaluator, validation and the wall-clock 1-minute limit, and converts the returned `Command` | Re-implements `firstNConsolidationOption`: budget pre-filter, prefix binary search over `ComputeConsolidation` and `FilterOutSameInstanceType`, `Guide.Accept` where upstream calls the evaluator, one `UseFull` per step, and the 1-minute limit on the injected clock |
| 2 | `upstream-single` | Calls `SingleNodeConsolidation.ComputeCommands` | Re-implements the scan: `SortCandidates`, the 3-minute limit, budgets, `Guide.CanPass`, `ComputeConsolidation`, `Guide.Accept`, stop at first, `PreviouslyUnseenNodePools`, `MarkConsolidated` |
| 3 | `upstream` | Re-checks the command with `EvaluateBalancedMove` and the budgets; a command upstream already approved passes both | `Accept` is `EvaluateBalancedMove`'s decision (without its metrics and events) for Balanced pools; first valid move within budgets |
| 3 | `balanced` | - | The same, scoring every NodePool as Balanced (`ScoreMove`, `BalancedK`) |
| 4 | `upstream-queue` | Returns the already-validated command | Validates with the method's validator after `ValidationDelay`, emits rejection events on failure, returns the `Command` |

The coarse form is upstream by construction. It is the default because it carries no reimplementation risk. The fine
form is what makes stages swappable: with `mode: fine`, a different planner changes acceptance without touching
search. Both forms pass the parity test.

Every adapter keeps upstream state where upstream keeps it. `Upstream` builds its own `MultiNodeConsolidation` and
`SingleNodeConsolidation` with `MakeConsolidation`, as `NewMethods` does, so `lastConsolidationState`,
`PreviouslyUnseenNodePools` and the validators behave the same. `upstream-queue` returns merges to `disrupt()` rather
than starting them, so `Queue.StartCommand` runs exactly once.

#### Execution paths

- **Merges** carry their upstream `Command` (`UpstreamCommand`, `CommandOf`). An executor validates them, or
  confirms them with `Full` when no generator did, and returns them in `Report.Commands`. `disrupt()` hands them to
  the orchestration queue: taint, launch replacements, wait, delete, drain. That path is unchanged.
- **Shifts** cannot go to the queue, because `StartCommand` taints and deletes every candidate. The `shift-evict`
  family runs them itself:
  1. Optionally wait out the validation delay, then re-check each shift with `Fast` on a fresh snapshot. A shift that
     no longer passes is aborted with a `churn:` reason.
  2. Taint each source `PreferNoSchedule` under `ShiftSourceTaintKey`
     (`consolidation.experimental.karpenter.sh/shift-source`), recording the time in
     `consolidation.experimental.karpenter.sh/shift-source-since`. The taint is deliberately not
     `karpenter.sh/disrupted`, because the node is not being removed yet.
  3. Evict the pods through the Eviction API in binding order. A 429 from a PDB stops the move.

  Taints older than `taintTTL` are lifted on later `Execute` and `Idle` calls. A merge whose source a shift in the
  same plan is emptying is deferred.

Termination already evicts pods and the queue already taints nodes, so Karpenter's ClusterRole has `create` on
`pods/eviction` and `patch` on nodes (see `kwok/charts/templates/clusterrole.yaml`). Shifts need no RBAC change.

#### Budgets

A plan must fit every one of these, and each is enforced at the stage named:

| Budget | Source | Enforced by |
| --- | --- | --- |
| Node disruption budget per NodePool | `BuildDisruptionBudgetMapping` for `Underutilized`, passed by `disrupt()` | `ExceedsBudgets` in every planner; `SpendBudgets` in the planners that pick several moves |
| Pod evictions per NodePool per minute | `budgets.podEvictionsPerMinute`, over a trailing minute on the injected clock | the same |
| Pod evictions per workload (controlling owner) per minute | `budgets.workloadEvictionsPerMinute` | the same |
| PodDisruptionBudget allowance | `Snapshot.PDBAllowed` (from `status.disruptionsAllowed`) | `Fast` per move; energy planners across the whole plan; executors per plan and through the Eviction API |
| `karpenter.sh/do-not-disrupt` | `NodeView.DoNotDisrupt`, `PodView.DoNotDisrupt` | `Fast`; energy planners again (including removed nodes); executors on the fresh snapshot |
| Search compute | `search.maxAttempts`, `search.maxFullSimulations`, `search.timeout` | `SearchBudget` |

Node-count budgets cannot see shifts, because shifts remove no node. The pod-eviction budgets close that gap. They are
charged with the bindings an executor actually evicted (`Report.Evicted`) when that is fewer than planned, for example
when a PDB stopped a shift. Otherwise they are charged with every planned binding, since a merge handed to the queue
eventually evicts all of its pods.

#### Balanced as a planning energy

Planners that score with an energy use:

```
ΔE(m) = α ΔΦ(m) + λ D̂(m) − μ Ŝ(m)          (pipeline.Weights{Alpha, Lambda, Mu}.Energy)
```

Balanced's acceptance rule is the case α = 0, λ = 1, μ = k of ΔE ≤ 0. With Ŝ = savings_fraction and
D̂ = disruption_fraction, as `ScoreMove` computes them:

```
Balanced approves      Ŝ / D̂ ≥ 1/k
⇔                      k Ŝ ≥ D̂             (D̂ > 0 for every node-deleting move: 1.0 base cost per removed node)
⇔                      1·D̂ − k·Ŝ ≤ 0
⇔                      ΔE ≤ 0   with α = 0, λ = 1, μ = k   (k = BalancedK = 2)
```

When Ŝ ≤ 0, Balanced rejects (`Score()` returns 0), and ΔE ≥ D̂ > 0 rejects too, so the two agree there as well.
`WhenEmptyOrUnderutilized` is the limit k → ∞. In energy terms that is α = 0, λ = 0, μ = 1 with a strict ΔE < 0,
meaning any positive savings. These equivalences cover acceptance only. Generation, the price filters and the
spot-to-spot rules stay in `computeConsolidation`.

The equivalence has four limits. Reviewers should keep them in mind:

1. **One NodePool.** Balanced scores each Balanced source pool independently, attributes savings to each pool by its
   share of source cost, and requires every pool to approve. `DefaultScorer` normalizes a cross-pool move by the summed
   totals of its pools, so cross-pool moves are not equivalent.
2. **The boundary.** `entropy-greedy`, `sequence` and `anneal-plan`'s final net-downhill test use a strict ΔE < 0, but
   Balanced approves a score exactly equal to 1/k. For example, take the Balanced RFC's "Uniform Pool Replace" (savings
   2.42 of 48.40, disruption 8 of 80, score 0.50). Balanced approves it. `entropy-greedy` with α = 0, λ = 1, μ = 2
   computes ΔE = 0 and declines it.
3. **The `balanced` planner is separate.** It does not go through the energy. It calls `ScoreMove` directly, and it is
   what the parity test covers. The energy form is for planners that add α ΔΦ.
4. **No metrics or events.** Balanced's `karpenter_consolidation_score` and `karpenter_consolidation_moves_total`
   metrics and its `ConsolidationApproved` events come from the evaluator. Only the coarse adapters still go through
   it (Observability).

With α > 0 the energy also values moves that delete no node. A shift has Ŝ = 0 and is accepted when
α |ΔΦ| > λ D̂, which is exactly the trade-off Balanced cannot express.

#### Decision log

Every stage decision produces one `DecisionEvent` (schema version `DecisionLogSchemaVersion = 1`; bump it on any field
change). The controller writes events through `LogRecorder`: the message `"consolidation decision"` at V(1) on the
`consolidation-pipeline` logger. The simulator writes them through `JSONLRecorder` to
`experiments/runs/<run-id>/decisions.jsonl`, next to `manifest.json`, `timeseries.csv` and `summary.json`.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `run_id` | string | `"controller"` in the operator; the run ID in the simulator |
| `cycle` | int | Pipeline cycle counter |
| `t` | float | Seconds since the method started, from the injected clock |
| `stage` | string | `candidates`, `moves`, `plan`, `execute` |
| `strategy` | string | Strategy name (generators joined with `+` on a round summary) |
| `move_id` | string | Unique per strategy instance, for example `multi-3`; empty on summaries |
| `kind` | string | `merge`, `shift` or `rightsize`. The simulator's baseline arms log upstream's decision (`delete`, `replace`) here. |
| `sources`, `destinations` | []string | Sources in the move's order (upstream's candidate order for adapter moves); destinations sorted |
| `pods` | int | Bindings (on the candidates summary: candidate count) |
| `delta_potential`, `disruption`, `savings_per_hour` | float | `Scored` fields |
| `accepted` | bool | The stage's verdict |
| `reason` | string | Why not, for example `not downhill`, `node budget for default: 2 > 1`, `conflicts with m4`, `validation: churn` |
| `budget_remaining` | int | Reserved. No code sets it yet, so it is always 0 (Open Question 10). |
| `detail` | map[string]float | Stage-specific numbers. Candidates: `sources`, `destinations`, `candidates`. Round summary: `moves`, `attempts`, `full_simulations`, `timed_out`, `exhausted`. Per move: `launch`, `remove`, `energy`, `norm_disruption`, `norm_savings`. |

No Go map iteration order reaches a logged list, and `encoding/json` sorts the keys of `detail`, so the same seed,
config and commit produce byte-identical logs. `TestDeterminism` in `hack/consolidation-sim/sim` checks this.

### Interaction with Existing Features

**What stays unchanged**

| Component | With the gate on |
| --- | --- |
| Emptiness, StaticDrift, Drift, Repair | Unchanged, in the same order, with the same reasons and budgets |
| Orchestration queue (`queue.go`) | Unchanged. Merges arrive as `Command`s through `disrupt()`, as today. |
| Termination and drain | Unchanged |
| Validation | Merges use the same validators and the same 15 s delay. Shifts are re-checked on a fresh snapshot after the same delay (with `validate: true`, the default). |
| NodePool disruption budgets | Unchanged; the pipeline uses the `Underutilized` reason |
| Metrics | Unchanged except for the new `consolidation_type` value `"pipeline"`, and Balanced's metrics in fine mode (Observability) |
| APIs, CRDs, annotations users set | Unchanged |

- **`consolidateAfter`.** Candidacy is unchanged: sources are this cycle's consolidation candidates, which upstream
  filters by the `Consolidatable` condition. Shifts interact with it in two ways (Open Question 1):
  - Pods leaving a shift source are pod events, which restart the source's `consolidateAfter` window.
  - A source that a shift empties is excluded from consolidation (`ShouldDisrupt` drops empty nodes). Emptiness
    removes it after `consolidateAfter`, under the `Empty` reason and its budgets. A shift's savings are therefore
    realized through Emptiness, at least one `consolidateAfter` after the last shift.
- **Balanced.** It is unchanged and stays one policy among several. In coarse mode, Balanced pools are scored
  exactly as today. In fine mode, the `upstream` planner applies the same decision.
- **PDBs and `karpenter.sh/do-not-disrupt`.** These are hard constraints for every strategy (Budgets). In the
  simulator, a run that violates either is invalid, and its summary counts violations.
- **PodDeletionCostManagement.** The pipeline is independent of it. The simulator models deletion-cost scale-down as a
  placement input. Feeding the planner's next merge targets into the ranking is future work (Open Question 7).
- **kube-scheduler.** Karpenter only simulates bind-pod. Shifts depend on where kube-scheduler puts each evicted pod's
  replacement (Open Question 3). The regression suite's scheduler config adds an opt-in `most-allocated` profile for
  experiments (`test/suites/regression/testdata/kube-scheduler-config.yaml`).

### Observability

- **`consolidation_type="pipeline"`** appears on `decision_evaluation_duration_seconds`, with one sample per pass
  where upstream recorded one sample for each consolidation method it ran. It also appears on `decisions_total`,
  `decisions_by_nodepool_total` and `queue_failures_total`, because `disrupt()` sets `cmd.Method` to the pipeline.
- **Labels that stay `multi` and `single`.** `consolidation_timeouts_total` and `failed_validations_total` keep these
  labels, because the wrapped upstream methods and validators emit them.
- **Balanced metrics and events.** `karpenter_consolidation_score`, `karpenter_consolidation_moves_total` and the
  `ConsolidationApproved` events are emitted only in coarse mode, which calls upstream's evaluator. Fine mode and the
  energy planners decide without them. Validation rejections still emit `ConsolidationRejected` events through
  `EmitRejectedEvents`.
- **Pipeline search timeouts and budget exhaustion** are recorded only in the decision log (`timed_out`,
  `exhausted`). They are not exported as metrics yet.
- **The decision log** is the primary tool. At V(1), each cycle logs the candidate summary, every generated move, the
  planner's verdict and reason for each move, and the executor's outcome.

### Edge Cases

- **Gate on, flag empty.** The coarse adapters run the upstream methods' own `ComputeCommands`. Decisions, Balanced
  metrics and consolidation events match today's. The visible differences are the `pipeline` label on the metrics
  listed above, and that building the snapshot can republish upstream's events about invalid `do-not-disrupt`
  annotations (`podutils.IsDisruptable` takes the event recorder).
- **A PDB refuses mid-shift.** The Eviction API returns 429. The executor stops that move with reason `pdb`, pods
  already evicted stay evicted, and `Report.Evicted` lists only those pods, so the eviction budgets charge only those.
- **A shift source is never emptied.** Its `PreferNoSchedule` taint lapses after `taintTTL` (10 minutes by default),
  so the node does not repel pods indefinitely. A `PreferNoSchedule` taint never makes a pod unschedulable.
- **A cycle that executes only shifts.** Shifts run inside `ComputeCommands` and return no `Command`, so `disrupt()`
  reports no action. The controller then goes through its remaining methods (none follow the pipeline) and requeues
  after its 10 s polling period, not immediately as it does after starting a command.
- **A merge and a shift share a source.** The executor defers the merge (`deferred: source draining`). A later
  cycle, or Emptiness, removes the node once it is empty.
- **Validation finds churn.** Merges are rejected as today. Shifts that no longer pass `Fast` on the fresh snapshot
  are aborted with a `churn:` reason, and the decision log counts the aborts.
- **A multi-NodePool cluster under the default config.** The upstream adapters inherit upstream's candidate order,
  including its nondeterminism (Graduation Criteria). Seeded runs fix candidate order through
  `UpstreamOptions.OrderCandidates`. Production leaves it unset.
- **Generators on a frozen clock.** The simulator's clock does not advance during generation, so a deadline alone
  cannot stop a random generator. The generators in `strategies/moves` cap a `Generate` call at `DefaultMaxAttempts`
  (1,000) when `search.maxAttempts` is 0.

## Alternatives Considered

### Alternative 1: Add knobs to the two existing methods

Expose ordering, acceptance and limits as options on `MultiNodeConsolidation` and `SingleNodeConsolidation`. This
keeps one code path, but the interleaving remains. Each new knob is another branch inside the binary search or the
scan, and new move kinds (shifts) do not fit a loop whose output is a node-deleting `Command`. Rejected because it
makes the methods harder to reason about without making any stage swappable.

### Alternative 2: Make the Balanced `Evaluator` the only seam

Swap scorers through `Evaluator` (`ApproveCommand`, `CanPassThreshold`) and leave generation alone. This is a smaller
change and covers Use Case 1. It cannot vary candidate selection or generation, cannot express shifts, and does not
cap compute. The pipeline keeps this seam's semantics in `Guide`, so this alternative is a strict subset of the
proposal.

### Alternative 3: An external compaction controller (descheduler-style)

Evict pods from nodes below a utilization threshold and let kube-scheduler with `MostAllocated` repack them, the
descheduler's `HighNodeUtilization` pattern. It needs no Karpenter change. It does not see Karpenter's budgets,
prices, `consolidateAfter` or in-flight commands, and it competes with consolidation over the same nodes. The
experiments include it as a baseline (B5 in the plan) rather than as the design.

### Alternative 4: Asynchronous generation now

Run generators continuously in the background, score moves into a priority queue, and execute as budgets allow, as
the Balanced RFC's open question describes. This needs move invalidation when cluster state changes, a staleness
policy for queued moves, and a new interaction with validation and `consolidateAfter`. Doing it first would make
parity impossible to establish. The stage boundaries proposed here are the prerequisite, so it is deferred (Open
Question 8).

### Alternative 5: An offline optimizer per cycle

Solve the packing exactly (ILP or CP-SAT) on the snapshot each cycle. The result is optimal for the snapshot but
costly at scale, and it ignores disruption ordering and kube-scheduler placement. The experiments use it as a lower
bound on small scenarios (B6), not as a strategy.

### Alternative 6: A research fork without a gate

Change the consolidation methods directly on a fork. That is the fastest way to experiment, but it gives no
compatibility story, no path upstream, and no parity guarantee for the parts not under study. Rejected.

## Backward Compatibility

- **Gate off (the default).** `disruptionMethods` returns no options, so `NewController` builds exactly today's
  methods. The pipeline packages are compiled in but never constructed. No behavior, metric or event changes.
- **Gate on, flag empty.** The decisions are today's, as the parity test checks. Some metric series move from `multi`
  and `single` to `pipeline` (Observability). Dashboards that filter on `consolidation_type` need the new value.
- **Gate on with a custom config.** This is experimental by definition. Shift executors add
  `consolidation.experimental.karpenter.sh/shift-source` taints and `.../shift-source-since` annotations to nodes.
- **Rollback.** Turning the gate off restores today's methods immediately. Shift-source taints and annotations
  already on nodes are not removed. They are `PreferNoSchedule`, and they disappear with their nodes, but nothing
  lifts them once the pipeline stops running (Open Question 2).
- **Config errors** fail fast at startup with the validation error. They are never silently ignored, except for
  unknown top-level keys under `pipeline:` (Open Question 10).

## Graduation Criteria

### Rollout

- **Alpha (this RFC).** The `ConsolidationPipeline` gate is off by default. No default changes. An empty flag selects
  the coarse upstream adapters. The only supported production configuration is the default one. Every other strategy
  is experimental, and the experiments report on it.
- **Beta.** Requires all of:
  1. A 10,000-state parity run with no unexplained mismatch.
  2. Experimental results published in `experiments/RESULTS.md`, with a decision on which strategies, if any, are
     recommended.
  3. Open Questions 1, 3, 9 and 10 resolved.
  4. The shift-source annotation registered as a `wellknown.Annotation`, and a metric for pipeline search timeouts.
  5. Fine-mode parity for events and Balanced metrics, not only decisions.

  The gate would still default off at beta unless the experiments show a strategy worth enabling by default.
- **GA.** Requires production feedback on at least one non-default configuration, a decision on asynchronous
  generation, and a decision on whether the two upstream methods are retired in favor of the coarse or fine adapters.

### Parity as the compatibility guarantee

`TestParity` (`pkg/controllers/disruption/pipeline/parity_test.go`) is the guarantee that the gate on with the
default config changes nothing.

**What one case does.** For each seeded cluster state, the test runs upstream multi-node and then single-node
consolidation the way `disrupt()` does. It then runs one pipeline cycle with each adapter configuration and requires
identical `harness.Decision`s: the decision kind, the candidates in order, the replacement instance-type options in
order, the budget consumed per NodePool, and any error.

**What it holds fixed.** States come from `harness.GenerateState`: 4 to 40 nodes, on the KWOK catalog (up to 16
vCPU) and on a thinned copy of the disruption suite's assorted catalog. Both paths get candidates ordered by name,
validators that skip the wait (`harness.ZeroDelay`), and one fake clock, and every case stays far below the 1-minute
multi-node limit.

**The configurations compared.**

- `coarse`: the default config.
- `fine`: both move adapters in `mode: fine`.
- `fine-balanced-planner`: the fine adapters with the `balanced` planner, on Balanced states only.

**Upstream's own nondeterminism.** With more than one NodePool, upstream is not deterministic:
`SingleNodeConsolidation.shuffleCandidates` interleaves pools in Go map order. With two pools, the second-inserted
pool comes first only about 1 time in 8. The test handles this as follows:

- On single-NodePool states it runs upstream twice as a control, and the decisions must match exactly.
- On multi-NodePool states the reference is the set of decisions upstream makes over 8 runs. A pipeline decision
  outside that set sends upstream back for up to 256 more runs before it counts as a mismatch.

**Results so far.** These come from `experiments/parity/parity-1000-seed1.summary.txt`, with
`PARITY_STATES=1000 PARITY_SEED=1`.

| Policy | Catalog | Configurations | Match | Upstream non-no-op decisions | Upstream nondeterministic states |
| --- | --- | --- | --- | --- | --- |
| Balanced | assorted | coarse, fine, fine-balanced-planner | 1000/1000 each | 846 | 10 |
| Balanced | kwok | coarse, fine, fine-balanced-planner | 1000/1000 each | 882 | 22 |
| WhenEmptyOrUnderutilized | assorted | coarse, fine | 1000/1000 each | 903 | 10 |
| WhenEmptyOrUnderutilized | kwok | coarse, fine | 1000/1000 each | 925 | 19 |

The nondeterministic multi-NodePool states all resolved within the observed upstream decision set. The 1,000-state
run took 21 minutes.

**Mutation check.** Three bugs were injected into the fine adapters one at a time:

- Dropping the multi-node budget decrement was caught (44 of 60 states matched).
- Making the planner accept every move was caught on Balanced states (85 of 100 matched). It is correctly invisible on
  `WhenEmptyOrUnderutilized` states.
- Skipping the `CanPass` pre-filter was not caught, and cannot be: it changes compute, never decisions.

**10,000-state run.** It is split into four chunks, one per policy and catalog (`experiments/parity/chunks/`). All
four matched 10,000 of 10,000 states in every configuration (`docs/research/parity.md`). The test compares decisions,
not events or metric deltas.

**In CI.** A 40-state run (the `PARITY_STATES` default) runs with `make test`. Larger runs are invoked by hand.

**Other checks.**

- `TestRegisteredStrategiesConform` runs the shared conformance suite (`conformance.CheckSelector`,
  `CheckGenerator`, `CheckPlanner`, `CheckPotential`) over every registered snapshot-level strategy.
- `TestPotentialDeltaMatchesValue`, `TestSnapshotApply`, `TestFastNeverAcceptsWhatFullRejectsOnResources` and
  `TestCommandMoveRoundTrip` cover the shared services.

## Open Questions

Each item is a known risk, how the current code handles it, and the decision that is still open.

1. **Shift moves and `consolidateAfter`.** Evicting pods from a shift source restarts its `consolidateAfter` window
   (the pod events controller in `pkg/controllers/nodeclaim/podevents` sets `LastPodEventTime` when a pod binds,
   goes terminating or goes terminal), and an emptied source is removed by Emptiness only after the window passes. A chain of shifts therefore delays its
   own payoff, and shifts onto a destination delay that node's candidacy too. The plan proposed marking shift sources
   as planned removals. That is not implemented, and the delay is not yet recorded per source. Should
   pipeline-initiated evictions be exempt from the pod-event timestamp? That would be a change to upstream code. Or
   should planners price the delay into ΔE?
2. **The PreferNoSchedule shift-source taint.** The taint keeps replacements from landing back on the source without
   making the source unschedulable. Open points:
   - Its key is experimental, and `consolidation.experimental.karpenter.sh/shift-source-since` is not registered in
     `KarpenterAnnotations`.
   - It is lifted only on `Execute` and `Idle`, and `Idle` runs only when a plan is empty. A cycle that generates no
     moves, or that is skipped because the cluster state is unchanged, lifts nothing.
   - Nothing removes it after rollback.

   Should the taint be owned by a small reconciler instead of the executor?
3. **Plan fidelity under kube-scheduler.** A shift plans a destination, but kube-scheduler places the replacement
   pod. Under the default `LeastAllocated` scoring it may spread replacements, which undoes part of the potential
   drop. Balanced's FAQ notes the same divergence for merges. The mitigations so far are experiment-only:
   - an opt-in `most-allocated` scheduler profile;
   - `steered` in affinity mode, which calls `Env.Steer`. Only the simulator implements it, so in the operator it
     does nothing and the executor behaves like `shift-evict`;
   - `make-before-break`.

   How much fidelity loss is acceptable, and is a mutating webhook for preferred node affinity in scope at all? The
   simulator measures this (plan fidelity, Exp 5 / H5); the session results are in `experiments/RESULTS.md`, which
   rates H5 partial and flags the Exp 5 run set.
4. **The wall-clock multi-node timeout becomes the injected clock in fine mode.** Upstream bounds the multi-node
   search with a 1-minute `context.WithTimeout`. The scheduler checks that context between pod scheduling attempts,
   so the search can stop partway through a simulation. The fine adapter checks the same duration on the injected
   clock between binary-search steps. With a real clock the two agree except for granularity: fine mode can finish
   one simulation past the limit. With a fake clock, fine mode times out only if the clock is stepped, which is what
   makes it deterministic. Coarse mode keeps upstream's wall-clock limit. Is that difference acceptable
   for a production default, or should fine mode keep a context deadline as well?
5. **Metric label drift.** With the gate on, some series move to `consolidation_type="pipeline"`, others keep
   `multi`/`single`, and Balanced's score metrics disappear in fine mode. Should the pipeline report the label of the
   upstream method whose move won, as the parity harness does, rather than `pipeline`?
6. **Snapshot cost.** `BuildSnapshot` makes one `DeepCopyNodes`, lists pods per node from the cached client, and lists
   PDBs each cycle. Upstream also deep-copies per simulation, so this is small next to simulation cost, but it is new
   per-pass work that has not been profiled at 5,000 nodes.
7. **Deletion-cost steering.** The planner's next merge targets could feed the PodDeletionCostManagement ranking, so
   that ReplicaSet scale-down empties them at no disruption cost. Should the planner export that ranking, given the
   pod-deletion-cost RFC's requirement that both share one ranking function?
8. **Asynchronous generation.** The Balanced RFC's question remains open. Given these stages, what is the staleness
   and invalidation policy for a move generated in one cycle and executed in a later one?
9. **What ships in the operator.** `pkg/controllers/controllers.go` imports `strategies/all`, so every experimental
   strategy is compiled into the operator and selectable by config behind the alpha gate. Should alpha ship only the
   upstream adapters and keep experimental strategies to the simulator and the KWOK suite?
10. **Loose ends found while drafting.**
    - `ParseConfig` decodes the `pipeline:`-wrapped form non-strictly, so a misspelled top-level key such as `planer:`
      is silently replaced by its default. The unwrapped form rejects it.
    - The decision log's `budget_remaining` is never set.
    - The `none` potential's factory ignores its params instead of decoding them strictly.
    - The conformance suite checks that generated moves pass `Fast`, not `Full`. Executors are checked only in the
      simulator, not in the conformance suite.
    - The `Guide` mirrors the `Evaluator` but does not reuse it, which is why fine mode emits no Balanced metrics.

## References

- [Balanced consolidation RFC](balanced-consolidation.md), in particular its Open Questions (async move generation)
  and its FAQ on kube-scheduler placement.
- [Pod deletion cost controller RFC](pod-deletion-cost-controller.md), gate `PodDeletionCostManagement`.
- [`docs/research/PLAN.md`](../docs/research/PLAN.md): the formal model, hypotheses, strategies and experiment matrix.
- [`docs/research/code-map.md`](../docs/research/code-map.md), sections 8.1 to 8.7: the seams, the semantics the
  method must preserve, the adapters, the snapshot contents, command and move conversion, and what parity holds fixed.
- [`experiments/RESEARCH_LOG.md`](../experiments/RESEARCH_LOG.md): decisions, timing measurements, parity and
  nondeterminism findings.
- [`experiments/RESULTS.md`](../experiments/RESULTS.md): session-profile experimental results (Tier A only, four groups
  cut).
- Code: `pkg/controllers/disruption/pipeline` (interfaces, adapters, method, registry, conformance, harness),
  `pkg/controllers/disruption/pipeline_hooks.go`, `pkg/controllers/controllers.go` (`disruptionMethods`),
  `pkg/operator/options/options.go` (`ConsolidationPipelineFeatureGate`, `--consolidation-pipeline-config`),
  `hack/consolidation-sim` (the simulator), and `test/suites/performance/pipeline_test.go` (the KWOK suite).
