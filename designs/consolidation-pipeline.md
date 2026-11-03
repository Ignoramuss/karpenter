# Pluggable Consolidation Pipeline

Split consolidation into four pluggable stages — candidate selection, move generation, planning within budgets, and execution — behind an off-by-default feature gate, with adapters that reproduce today's decisions exactly and a differential parity test that proves it.

[Bounded search budgets and a decision log](consolidation-bounded-search-and-decision-log.md) proposes the subset of this design that ships first: counted search budgets, the decision log, and the stage seams with today's search behind the gate. This document is the full design those seams come from.

## Motivation

Karpenter's consolidation runs as two disruption methods, `MultiNodeConsolidation` and `SingleNodeConsolidation`. Each one orders candidates, generates moves, decides whether to accept them, and validates them inside a single `ComputeCommands` call:

- **Multi-node** sorts candidates by savings ratio, drops those their NodePool budget cannot cover, and binary-searches over prefixes of the sorted list (windows of up to 101 nodes). Every step runs a scheduling simulation, filters out same-instance-type replacements, and asks the Balanced evaluator for approval. The search stops after one minute of wall-clock time (`MultiNodeConsolidationTimeoutDuration`, a `context.WithTimeout`).
- **Single-node** walks candidates interleaved by NodePool, checks the budget and the Balanced threshold, simulates, and stops at the first acceptable command. The scan stops after three minutes on the injected clock.

Four things follow from that structure.

1. **Stages cannot vary independently.** Changing how candidates are ordered, which moves are tried, or how they are accepted means editing the search loop. The Balanced evaluator is called from inside both loops.
2. **Work is bounded by time, not by compute.** A full single-node scan costs one scheduling simulation per candidate. In the in-process simulator on a 4-vCPU machine, one scheduling simulation of a seeded 1,000-node fleet took 67 ms, so a full single-node scan of its 864 candidates is about 58 s. The multi-node limit is wall clock, so what a pass finds depends on the machine it runs on, and two search strategies cannot be compared at equal compute.
3. **Every move deletes a node.** Delete and Replace both remove their sources, and Balanced scores only those moves. A move that improves packing without removing a node — moving a few pods from a sparse node to a fuller one — cannot be expressed, so it has no value to score.
4. **Rejected alternatives are invisible.** The controller logs the command it starts, emits `Unconsolidatable` events for single-node candidates that lost, and records metrics for scored moves. It keeps no structured record of every move it considered and why each one lost.

The reports behind [Balanced consolidation](balanced-consolidation.md) are the user-visible side of this: nodes at 93-99% utilization disrupted instead of lightly utilized ones ([aws#8868](https://github.com/aws/karpenter-provider-aws/issues/8868), [kubernetes-sigs#2319](https://github.com/kubernetes-sigs/karpenter/issues/2319)), multi-hour consolidation loops ([aws#8536](https://github.com/aws/karpenter-provider-aws/issues/8536), [aws#6642](https://github.com/aws/karpenter-provider-aws/issues/6642), [aws#7146](https://github.com/aws/karpenter-provider-aws/issues/7146)), and churn where deleted nodes are immediately re-provisioned ([kubernetes-sigs#1019](https://github.com/kubernetes-sigs/karpenter/issues/1019), [kubernetes-sigs#735](https://github.com/kubernetes-sigs/karpenter/issues/735), [kubernetes-sigs#1851](https://github.com/kubernetes-sigs/karpenter/issues/1851)). Each is a question about what consolidation considered and why it chose what it did, and none can be answered from today's logs.

The Balanced RFC leaves asynchronous move generation as an open question: "generate, score, enqueue by priority, execute as budget allows." That needs generation, scoring, planning and execution to be separate stages with explicit inputs and outputs. The [pod deletion cost controller RFC](pod-deletion-cost-controller.md) describes the fragmentation case that needs a new kind of move: ReplicaSet scale-down spreads deletions across nodes, every node ends up partly full, and each remaining merge must evict every pod on its source, which Balanced rightly rejects.

We propose to build consolidation as a pipeline of four stages over shared models of cluster state, feasibility, savings and disruption, behind a feature gate. With the gate on and no configuration, adapters around the existing methods make exactly today's decisions. With configuration, any stage can be replaced, including moves that delete no node.

### Use Cases

1. **Evaluating an acceptance rule.** A maintainer wants to try Balanced with a different `k`, or a rule that also values packing. Only the planner changes; candidate selection, generation and execution stay on the adapters around the existing methods, and the parity test still pins those stages to today's behavior.
2. **Large clusters.** At 1,000 nodes or more, a full single-node scan costs tens of seconds of simulation per pass. An operator caps full simulations per cycle (`search.maxFullSimulations`) and samples candidates instead of scanning all of them.
3. **Fragmentation after scale-down.** Every node is 30-60% utilized and no merge clears the Balanced threshold. A planner that accepts shifts (moves that empty a node over several cycles) under a pod-eviction budget lets Emptiness remove the drained nodes.
4. **Explaining a decision.** An operator asks why node X was not consolidated. The decision log records every move considered in the cycle, its scores, and the reason it was rejected: budget, threshold, conflict, or validation churn.

### Non-Goals

- No CRD, NodePool or NodeClaim API changes, and no new user-facing annotation.
- No change to any default. Neither Balanced nor any new strategy becomes a default policy.
- No asynchronous or continuous generation. All four stages still run inside one `ComputeCommands` call per pass; a background generator is future work built on these interfaces (Open Question 8).
- No change to Emptiness, Drift, StaticDrift, Repair, the orchestration queue or termination.
- No changes to kube-scheduler or the ReplicaSet controller. Plan fidelity is measured, not enforced.
- Not a descheduler. The pipeline acts only on consolidation candidates, within Karpenter's budgets.
- No production recommendation for any experimental strategy (see [Evidence](#evidence)).

## Proposal

With `--feature-gates ConsolidationPipeline=true`, the disruption controller runs one method, `PipelineConsolidation`, where multi-node and single-node consolidation used to run. The method composes four stages and shared services named in a configuration passed by `--consolidation-pipeline-config`. An empty value selects the adapters around the existing methods.

### Proposed Spec

| Item | Value |
|---|---|
| Feature gate | `ConsolidationPipeline`, alpha, default `false`, listed in `KarpenterFeatureGates` |
| Flag | `--consolidation-pipeline-config`, env `CONSOLIDATION_PIPELINE_CONFIG`, default empty |
| Flag value | A file path, or the config itself as YAML or JSON. A value that starts with `{` or `pipeline:`, or spans more than one line, is parsed inline; anything else is read as a path. |
| Empty value | The default configuration below: the adapters around the existing methods |
| Invalid value | Karpenter fails at startup with the parse or validation error. Unknown keys are rejected at every level (Open Question 11 names the one factory that does not yet decode its params). |

**Default configuration.** Equivalent to an empty flag:

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

The adapters are registered as `upstream` (candidates and planner), `upstream-multi`, `upstream-single` and `upstream-queue`; *upstream* in those names means the existing in-tree methods they wrap, `MultiNodeConsolidation` and `SingleNodeConsolidation`.

**An experimental configuration**, as an illustration of what the seams allow; none of these strategies is supported in production (see [Experimental strategies](#experimental-strategies)). Random generation, an annealing planner, shifts executed through the Eviction API, a packing potential, and explicit compute and eviction budgets:

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

**Config schema** (abridged):

```go
type Config struct {
	Candidates StageConfig  `json:"candidates"`
	Moves      StageList    `json:"moves"`               // one object or a list
	MovesMode  MovesMode    `json:"movesMode,omitempty"` // "sequential" (default) or "pooled"
	Planner    StageConfig  `json:"planner"`
	Executor   StageConfig  `json:"executor"`
	Potential  StageConfig  `json:"potential"`
	Seed       int64        `json:"seed"`
	Search     SearchConfig `json:"search,omitzero"`
	Budgets    BudgetConfig `json:"budgets,omitzero"`
	Recorder   StageConfig  `json:"recorder,omitzero"` // none (default), log, file
}

type StageConfig struct {
	Name   string `json:"name"`
	Params Params `json:"params,omitempty"` // decoded strictly by the strategy; unknown keys fail construction (Open Question 11)
}

type SearchConfig struct {
	MaxAttempts        int    `json:"maxAttempts,omitempty"`        // generation attempts per generator; 0 = unlimited
	MaxFullSimulations int    `json:"maxFullSimulations,omitempty"` // scheduling simulations per pass; 0 = unlimited
	Timeout            string `json:"timeout,omitempty"`            // deadline on the injected clock, e.g. "1m"
}

type BudgetConfig struct {
	PodEvictionsPerMinute      int `json:"podEvictionsPerMinute,omitempty"`      // per NodePool; 0 = unlimited
	WorkloadEvictionsPerMinute int `json:"workloadEvictionsPerMinute,omitempty"` // per controlling owner; 0 = unlimited
}
```

**Registered strategies.** Every stage is chosen by name. The adapters and the potentials register in the `pipeline` package; the experimental strategies register in `pipeline/strategies/{candidates,moves,planners,executors}`, which `pkg/controllers/controllers.go` imports through `strategies/all`, so the controller binary contains every strategy and any of them is selectable by name behind the gate (Open Question 9). The strategies the default configuration names, plus two variants of the `upstream` planner (`savings-only`, the same planner under the name the experiments use, and `balanced`):

| Stage | Name | Params | Role |
|---|---|---|---|
| candidates | `upstream` | — | Today's candidates in savings-ratio order |
| moves | `upstream-multi`, `upstream-single` | `mode`: `coarse` (default) or `fine` | The parity adapters around the two existing methods (see [below](#the-adapters-coarse-and-fine)) |
| planner | `upstream`, `savings-only` | — | First acceptable move within budgets; Balanced scoring where a NodePool asks for it |
| planner | `balanced` | — | The same, with Balanced (k=2) applied to every NodePool |
| executor | `upstream-queue` | — | Validate merges and hand them to the orchestration queue |
| potential | `none` | — | No potential; savings-only planning |

The experimental selectors, generators, planners, executors and potentials are listed under [Experimental strategies](#experimental-strategies).

### How It Works

#### Stages and shared services

Stages 1 and 2 only propose. Stage 3 decides what runs, using the shared scoring models. Stage 4 carries the plan out.

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

// Guide is the planner's acceptance test, consulted by search-guided generators while they search.
type Guide interface {
	Accept(ctx context.Context, s Snapshot, m Scored) bool
	CanPass(ctx context.Context, s Snapshot, sources []string) bool
}

type Executor interface { // stage 4
	Name() string
	Execute(ctx context.Context, p Plan) (Report, error)
}

type Potential interface {
	Name() string
	Value(s Snapshot) float64
	Delta(s Snapshot, m Move) float64 // equals Value(s.Apply(m)) - Value(s)
}

type Feasibility interface {
	Fast(s Snapshot, m Move) error                              // snapshot only
	Full(ctx context.Context, s Snapshot, m Move) (Move, error) // Karpenter's scheduling simulation
}

type Scorer interface{ Score(s Snapshot, m Move) Scored }
type Recorder interface{ Record(e DecisionEvent) }
```

`Guide` mirrors the Balanced `Evaluator` seam: `Accept` corresponds to `ApproveCommand` and `CanPass` to `CanPassThreshold`. It exists because today's multi-node search consults acceptance at every step. A fine-mode generator asks the configured planner through `Guide`, so swapping the planner also changes what the search finds, exactly as swapping the evaluator does today.

**Feasibility** has two levels. `Fast` uses only the snapshot: capacity, taints, node requirements, bound-volume zones, hostname anti-affinity, `karpenter.sh/do-not-disrupt` on pods and nodes, and PDB allowances; it does not check topology spread. `Full` runs Karpenter's scheduling simulation against the live cluster: a merge is re-simulated through `ComputeConsolidation` (with `FilterOutSameInstanceType` applied to multi-source replaces) and comes back carrying the `Command` the existing method produced; a shift is solved against its destinations only, with no new NodeClaims allowed.

**Search budget.** A `SearchBudget` holds `MaxAttempts`, `MaxFull`, a `Deadline` on the injected clock, the planner's `Guide`, and `StopAtFirst` (set for first-valid planners, so a search stops at the first acceptable move as the existing scan does). Generators call `Next()` once per attempt and `UseFull()` before each full simulation. Each generator gets its own `Fork()`: attempts are counted per fork, while full simulations and the deadline are shared. One `SearchBudget` is built per pass and forked per round, so the two adapters in sequence share one `maxFullSimulations`. The coarse adapters hand the whole search to the existing methods' `ComputeCommands`; the budget reaches that search only through the `SearchObserver` hook the bounded-search RFC adds inside the two loops, which the adapters set with the pass's budget fork before each call. The fine adapters and the experimental generators consult the budget directly.

#### One cycle

```
disrupt() ─► Method.ComputeCommands(budgets, candidates)
               │  skip if the cluster's consolidation state is unchanged since a cycle that found nothing and was not held back by a budget
               ├─ BuildSnapshot                                        ─► nodes, pods, PDB allowances, totals
               ├─ one SearchBudget for the pass
               ├─ stage 1  selector.Select(snapshot, rng)              ─► sources, destinations
               ├─ for each round (sequential: one generator per round; pooled: all in one)
               │    ├─ (rounds after the first) refresh candidates, totals, budgets and the snapshot if the clock moved
               │    ├─ stage 2  g.Generate(..., rng, budget.Fork())    ─► moves
               │    ├─ Scorer.Score each move                          ─► Scored
               │    ├─ stage 3  planner.Plan(snapshot, scored, budgets) ─► Plan
               │    ├─ stage 4  executor.Execute(plan)                 ─► Report (merges come back as Commands)
               │    └─ a round that executes anything ends the cycle
               └─ record a DecisionEvent at every stage
disrupt() ─► Queue.StartCommand for each returned merge (unchanged)
```

In sequential mode the method reproduces how the controller runs multi-node and then single-node consolidation: each generator is its own round, and the next round runs only if the previous one executed nothing. The pipeline's own skip rule is the same short-circuit the existing methods apply — skip while the cluster's consolidation state is unchanged since a cycle that found nothing and was not held back by a disruption budget. With the adapters around the existing methods, which track consolidated state themselves, the method defers to their per-method `IsConsolidated` instead.

#### Where it plugs in

The disruption package does not import the pipeline; the pipeline imports the disruption package. `disruptionMethods` in `pkg/controllers/controllers.go` returns no options when the gate is off. With the gate on, `pipeline.Methods` loads the config, builds the method, and puts it where multi-node consolidation was, dropping single-node consolidation. The method order becomes Repair (when gated), Emptiness, StaticDrift, Drift, Pipeline. The method reports `DisruptionReasonUnderutilized`, so today's budgets apply, and the graceful disruption class (`GracefulDisruptionClass`); its `ShouldDisrupt` is consolidation's own, so empty nodes, `WhenEmpty` pools and static pools are excluded as today; it receives the cycle's Balanced totals through `SetNodePoolTotals` and forwards them to the wrapped methods; its `ConsolidationType()` is `pipeline`.

The existing Go files that change are `controllers.go` (the wiring), `options.go` (the gate and flag), `pkg/test/options.go`, `disruption/metrics.go` (the documented `pipeline` label value), `pkg/apis/v1/labels.go` (two well-known annotations: the internal-only simulated volume-zone annotation, which only the simulator sets on synthetic pods and `Fast` reads as the pod's bound-volume zone, and `shift-source-since`) and a new additive `disruption/pipeline_hooks.go` that exports existing unexported building blocks (`ComputeConsolidation`, `SortBySavingsRatio`, `MarkConsolidated`, `FilterOutSameInstanceType`, candidate accessors) without changing behavior. Outside Go, `test/suites/regression/testdata/kube-scheduler-config.yaml` gains the opt-in `most-allocated` kube-scheduler profile the performance suite can select.

**Proposed layout.** The pipeline lives in `pkg/controllers/disruption/pipeline` (interfaces, adapters, method, registry, conformance checks, parity harness and test), with the experimental strategies under `pipeline/strategies/`. An in-process simulator under `hack/consolidation-sim` drives the same method with a fake clock and a JSON-lines recorder, and is what `run_id` identifies outside the controller; a KWOK suite under `test/suites/performance/` runs the gate against a KWOK cluster. Both are test harnesses this RFC adds, not production code.

#### The adapters: coarse and fine

| Stage | Adapter | Coarse (`mode: coarse`, default) | Fine (`mode: fine`) |
|---|---|---|---|
| candidates | `upstream` | This cycle's candidates; the existing method sorts them itself | Sources in `SortBySavingsRatio` order; destinations are every node the snapshot marks as one |
| moves | `upstream-multi` | Calls `MultiNodeConsolidation.ComputeCommands` — budgets, evaluator, validation and the one-minute wall-clock limit included — and converts the returned `Command` | Re-implements the prefix binary search: budget pre-filter, `ComputeConsolidation` and `FilterOutSameInstanceType`, `Guide.Accept` where the existing search calls the evaluator, one `UseFull` per step, the one-minute limit on the injected clock |
| moves | `upstream-single` | Calls `SingleNodeConsolidation.ComputeCommands` | Re-implements the scan: `SortCandidates`, the three-minute limit, budgets, `Guide.CanPass`, `ComputeConsolidation`, `Guide.Accept`, stop at first, `PreviouslyUnseenNodePools`, `MarkConsolidated`, plus a resume cursor when the budget stops the scan |
| planner | `upstream` | Re-checks the command with `EvaluateBalancedMove` and the budgets; a command the existing method approved passes both | `Accept` is `EvaluateBalancedMove`'s decision for Balanced pools (without its metrics and events); first valid move within budgets |
| planner | `balanced` | — | The same, scoring every NodePool as Balanced |
| executor | `upstream-queue` | Returns the already-validated command | Validates after the validation delay, emits rejection events on failure, returns the `Command` |

The coarse form is today's behavior by construction and is the default because it carries no reimplementation risk. The fine form is what makes stages swappable: with `mode: fine`, a different planner changes acceptance without touching the search. Both forms pass the parity test. The fine planner mirrors the Balanced evaluator rather than calling it, so Balanced's score metrics and `ConsolidationApproved` events are not emitted in fine mode. The adapters build their own `MultiNodeConsolidation` and `SingleNodeConsolidation` with the constructor `NewMethods` uses, so `lastConsolidationState`, `PreviouslyUnseenNodePools` and the validators behave the same.

#### Moves, scoring and potentials

A `Move` is a composition of actions that ends in a feasible state: `Sources` (nodes losing pods), `Bindings` (per pod: unbind from a source, planned bind to an existing node or to a launched node), `Launch` (new nodes) and `Remove` (deleted nodes). There are three kinds:

- **Merge.** Empty the sources into existing destinations plus at most one launched node, then remove the sources. Today's Delete and Replace both convert to merges.
- **Shift.** Move some pods to another node and remove nothing.
- **Right-size.** Replace one node with a cheaper one holding the same pods. Defined; no generator emits it yet.

Every move is scored: `DeltaPotential` (the configured potential's change), `Disruption` (the sum of `max(0, EvictionCost)` over the pods it binds, plus 1.0 per removed node — for a merge, exactly the numerator Balanced scores), `SavingsPerHour` (removed node prices minus launched node prices), and their normalized forms `D̂` and `Ŝ`, divided by the NodePool totals Balanced uses so that planner weights are scale-free.

A **potential** Φ is a function of the cluster state that falls as nodes move toward packed or empty. A concave function of node load is minimized at a vertex of the feasible region, where every node is empty or full except at most one, which is the fleet shape consolidation is after. Each potential is normalized by constants of the root snapshot, never by those of the fork being valued, so `Delta` is exact and removing an empty node is never penalized by a shrinking denominator.

| Name | Definition | Note |
|---|---|---|
| `load-entropy` | H(p) = −Σ pᵢ ln pᵢ, pᵢ = Lᵢ / Σ L, divided by ln N₀ | Blind to right-sizing |
| `util-entropy` | Φ = Σ wᵢ h(uᵢ), h(u) = −u ln u − (1−u) ln(1−u), wᵢ = price, divided by Σ w at the root | 0 for empty and full nodes, largest at 50% |
| `util-quadratic` | Σ wᵢ uᵢ (1 − uᵢ) | Cheaper smooth variant |
| `none` | 0 | Savings-only planners |

Utilization is the dominant share by default (CPU, memory and accelerators); a single resource, or a max or mean over resources, can be chosen instead, and a stranding term `stranding · |u_cpu − u_mem|` can be added.

**Balanced as a planning energy.** Planners that score with an energy use

```
ΔE(m) = α ΔΦ(m) + λ D̂(m) − μ Ŝ(m)
```

Balanced's acceptance rule is the case α = 0, λ = 1, μ = k of ΔE ≤ 0: Balanced approves when Ŝ / D̂ ≥ 1/k, which is kŜ ≥ D̂, which is 1·D̂ − k·Ŝ ≤ 0. `WhenEmptyOrUnderutilized` is the limit k → ∞ (α = 0, λ = 0, μ = 1, strict ΔE < 0: any positive savings). With α > 0 the energy also values moves that delete no node: a shift has Ŝ = 0 and is accepted when α |ΔΦ| > λ D̂, the trade-off Balanced cannot express.

The equivalence has limits. Balanced scores each source pool independently and requires every pool to approve, while the scorer normalizes a cross-pool move by the summed totals of its pools, so cross-pool moves are not equivalent. The energy planners use a strict ΔE < 0 where Balanced approves a score exactly equal to 1/k (the Balanced RFC's "Uniform Pool Replace", score 0.50, is approved by Balanced and declined by `entropy-greedy`). And the `balanced` planner itself calls `ScoreMove` directly rather than going through the energy; it is what the parity test covers.

#### Experimental strategies

These are the strategies registered for simulator and KWOK experiments, not for production. None is proposed as a default, and the first increment's allowlist rejects them at startup; Open Question 9 decides whether any becomes supported.

| Stage | Name | Params (defaults) | Role |
|---|---|---|---|
| candidates | `uniform-random` | `sourceFraction` (0.25), `destinationFraction` (1.0) | A uniform sample of eligible nodes |
| candidates | `util-band` | `resource` (dominant), `fullThreshold` (0.95) | Sources below the fleet's mean utilization; destinations above it |
| candidates | `potential-weighted` | `sourceFraction` (0.25) | Sources sampled in proportion to the potential drop from emptying them |
| moves | `random-merge` | `maxSources` (3), `launch` (true), `maxMoves` (50), `confirm` (false), `idleAttempts` (0) | Empty 1 to k random sources first-fit into random destinations, optionally confirmed by a full simulation |
| moves | `random-shift` | `maxPodsPerMove` (4), `full` (false), `idleAttempts` (0) | Move 1 to m random pods from a source to a fuller destination |
| moves | `greedy-shift` | `maxPodsPerMove` (4), `idleAttempts` (0) | Per sampled node pair, the pod set that most lowers the potential |
| moves | `swap` | `strandingGap` (0.3) | Exchange pods between a CPU-stranded and a memory-stranded node |
| planner | `entropy-greedy` | `alpha` (1), `lambda` (0.5), `mu` (1) | Every move whose energy falls, best ratio first, no two touching a node |
| planner | `anneal-plan` | energy weights, `t0` (0.05), `cooling` (0.97), `temperatureUnit` (absolute), `steps` (400), `maxLength` (8) | Simulated annealing over move sequences on the snapshot; runs only a net-downhill sequence |
| planner | `anneal-online` | energy weights, `t0`, `cooling`, `temperatureUnit` | One annealing step per cycle, executed on the cluster (experiment only) |
| planner | `sequence` | energy weights, `horizon` (4) | Chains shifts that empty a node and scores the chain with its final delete |
| executor | `shift-evict` | `validate` (true), `taint` (true), `taintTTL` (10m) | Merges to the queue; shifts through the Eviction API with a `PreferNoSchedule` taint on the source |
| executor | `steered` | `shift-evict` params, `mode` (`affinity` or `most-allocated`) | `shift-evict`, plus a preference for each replacement pod to land on its planned node |
| executor | `make-before-break` | `shift-evict` params | `steered` in affinity mode, evicting only into initialized destinations |
| potential | `load-entropy`, `util-entropy`, `util-quadratic` | `weight` (price), `resource` (dominant), `stranding` (0) | The packing potentials (see [Scoring](#moves-scoring-and-potentials)) |

`idleAttempts` stops a random generator after that many consecutive attempts that produced nothing; 0 never stops early.

#### Shifts and execution paths


**Merges** carry the `Command` the existing method produced. An executor validates them, or confirms them with `Full` when no generator did, and returns them in `Report.Commands`; `disrupt()` hands them to the orchestration queue (taint, launch replacements, wait, delete, drain), unchanged.

**Shifts** cannot go to the queue, because `StartCommand` taints and deletes every candidate. The `shift-evict` family runs them itself:

1. Wait out the validation delay, then re-check each shift with `Fast` on a fresh snapshot. A shift that no longer passes is aborted with a `churn:` reason.
2. Taint each source `PreferNoSchedule` with `consolidation.experimental.karpenter.sh/shift-source` and record the time in the `consolidation.experimental.karpenter.sh/shift-source-since` annotation. The taint is deliberately not `karpenter.sh/disrupted`, because the node is not being removed yet.
3. Evict the pods through the Eviction API in binding order. A 429 from a PDB stops the move.

Taints older than `taintTTL` are lifted on later `Execute` and `Idle` calls. A merge whose source a shift in the same plan is emptying is deferred. Termination already evicts pods and the queue already taints nodes, so shifts need no RBAC change.

A shift saves nothing immediately: its value is deferred until the source has been emptied over several shifts and Emptiness removes it, at which point the last step evicts no pods at all. That is why shifts need a potential to be valued.

#### Budgets

A plan must fit every one of these:

| Budget | Source | Enforced by |
|---|---|---|
| Node disruption budget per NodePool | `BuildDisruptionBudgetMapping` for `Underutilized`, passed by `disrupt()` | Every planner; the planners that pick several moves spend it move by move |
| Pod evictions per NodePool per minute | `budgets.podEvictionsPerMinute`, over a trailing minute on the injected clock | Every planner |
| Pod evictions per workload (controlling owner) per minute | `budgets.workloadEvictionsPerMinute` | Every planner |
| PodDisruptionBudget allowance | `Snapshot.PDBAllowed` (from `status.disruptionsAllowed`) | `Fast` per move; the energy planners across the whole plan; executors per plan and through the Eviction API |
| `karpenter.sh/do-not-disrupt` | `NodeView.DoNotDisrupt`, `PodView.DoNotDisrupt` | `Fast`; the energy planners; executors on the fresh snapshot |
| Search compute | `search.maxAttempts`, `search.maxFullSimulations`, `search.timeout` | `SearchBudget` |

Node-count budgets cannot see shifts, because shifts remove no node. The pod-eviction budgets close that gap. They are charged with the bindings an executor actually evicted when that is fewer than planned (a PDB stopped a shift), and otherwise with every planned binding, since a merge handed to the queue eventually evicts all of its pods.

#### Decision log

Every stage decision produces one `DecisionEvent` (schema version 2, as the [bounded-search RFC](consolidation-bounded-search-and-decision-log.md) defines it; bump it on any field change). The controller writes events through the recorder the configuration names, as the bounded-search RFC defines them: `none`, the default, discards; `log` writes through the `consolidation-pipeline` logger with the message `consolidation decision`, round summaries at V(1) and per-move events at V(2); `file` appends JSON lines. The simulator writes JSON lines.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | 2 |
| `run_id` | string | `controller` in Karpenter; the run ID in the simulator |
| `cycle` | int | Pass counter, incremented once per `ComputeCommands` call |
| `t` | float | Seconds since the method started, from the injected clock |
| `stage` | string | `candidates`, `moves`, `plan`, `execute` |
| `strategy` | string | Strategy name (generators joined with `+` on a round summary) |
| `move_id` | string | Unique per strategy instance, e.g. `multi-3`; empty on summaries |
| `kind` | string | `merge`, `shift` or `rightsize` |
| `sources`, `destinations` | []string | Sources in the move's order; destinations sorted |
| `pods` | int | Bindings (on the candidates summary: candidate count) |
| `delta_potential`, `disruption`, `savings_per_hour` | float | The move's scores |
| `accepted` | bool | The stage's verdict |
| `reason` | string | Why not: `not downhill`, `node budget for default: 2 > 1`, `conflicts with m4`, `validation: churn` |
| `budget_remaining` | int | Simulations left in the pass after this event; -1 when `maxFullSimulations` is 0 |
| `bindings` | list | Optional: per pod, its source node and planned destination |
| `detail` | map[string]float | Candidates summary: `sources`, `destinations`, `candidates`. Round summary: `moves`, `attempts`, `full_simulations`, `timed_out`, `exhausted`, `unreached`, `budget_skipped`, `threshold_skipped`. Per move: `launch`, `remove`, `energy`, `norm_disruption`, `norm_savings` |

No Go map iteration order reaches a logged list, and the JSON encoder sorts the keys of `detail`, so in the simulator — a fake clock, one NodePool, a fixed candidate order — the same seed, config and commit produce byte-identical logs. In the controller `t` is real time and the single-node scan order is Go map order with several NodePools, so two passes on the same state log the same decisions, not the same bytes.

### Interaction with Existing Features

| Component | With the gate on |
|---|---|
| Emptiness, StaticDrift, Drift, Repair | Unchanged, in the same order, with the same reasons and budgets |
| Orchestration queue, termination, drain | Unchanged. Merges arrive as `Command`s through `disrupt()`, as today |
| Validation | Merges use the same validators and the same 15 s delay. Shifts are re-checked on a fresh snapshot after the same delay |
| NodePool disruption budgets | Unchanged; the pipeline uses the `Underutilized` reason |
| Balanced | Unchanged, one policy among several. In coarse mode Balanced pools are scored exactly as today; in fine mode the `upstream` planner applies the same decision without the evaluator's metrics and events |
| PDBs and `karpenter.sh/do-not-disrupt` | Hard constraints for every strategy, at `Fast`, in the planners and in the executors |
| `SpotToSpotConsolidation` | Unchanged: the rule lives in `computeConsolidation`, which every adapter and `Full` call |
| APIs, CRDs, annotations users set | Unchanged |

**`consolidateAfter`.** Candidacy is unchanged: sources are this cycle's consolidation candidates, which the controller filters by the `Consolidatable` condition. Shifts interact with it in two ways (Open Question 1). Pods leaving a shift source are pod events, which restart the source's `consolidateAfter` window; and a source that a shift empties is excluded from consolidation (empty nodes are not candidates), so Emptiness removes it after `consolidateAfter`, under the `Empty` reason and its budgets. A shift's savings are therefore realized at least one `consolidateAfter` after the last shift.

**kube-scheduler.** Karpenter only simulates where pods would go; kube-scheduler places the replacement pods. Shifts depend on that placement more than merges do (Open Question 3). The `steered` executor expresses a preference for the planned node, and the regression suite carries an opt-in `most-allocated` scheduler profile for experiments.

**PodDeletionCostManagement.** Independent. Feeding the planner's next merge targets into that controller's ranking is future work (Open Question 7).

### Observability

- **`consolidation_type="pipeline"`** appears on `decision_evaluation_duration_seconds` (one sample per pass, where today's methods record one each), `decisions_total`, `decisions_by_nodepool_total` and `queue_failures_total`.
- **`consolidation_timeouts_total` and `failed_validations_total`** keep their `multi` and `single` labels, because the wrapped existing methods and validators emit them.
- **Balanced's `karpenter_consolidation_score` and `karpenter_consolidation_moves_total`** and its `ConsolidationApproved` events are emitted only in coarse mode. Validation rejections still emit `ConsolidationRejected`.
- **Search timeouts and budget exhaustion** are recorded in the decision log's round summary (`timed_out`, `exhausted`); the bounded-search RFC adds metrics for them.
- **The decision log** is the primary tool: every cycle logs the candidate summary, every generated move, the planner's verdict and reason, and the executor's outcome.

### Observability of a non-decision

How to use the log: an operator asking why node `ip-10-0-1-7` was not consolidated filters the `consolidation decision` lines for that node in `sources`. The candidates summary carries counts, not names, so if the node never appears, either the candidate selector never offered it (check `consolidateAfter` and the `Consolidatable` condition) or no generator produced a move for it; under the coarse adapters that includes a command the existing method found and its own validation then rejected, since the adapters return validated commands only. This log cannot tell these apart; the bounded-search RFC's probe events can. Every generated move appears at `moves` and again at `plan`; `accepted: false` there and the `reason` says why: a NodePool budget (`node budget for default: 2 > 1`), the planner's acceptance test (`not accepted` from the `upstream` planner is the Balanced evaluator's verdict; `not downhill` from the energy planners), a conflict with another move (`conflicts with m4`), or `not selected` when the planner took an earlier move. A planned move appears at `execute` whether or not it ran; `accepted: false` there and the `reason` says what stopped it, for a merge `validation: budget`, `validation: scheduling` or `validation: churn`.

### Edge Cases

- **Gate on, flag empty.** The coarse adapters run the existing methods' own `ComputeCommands`. Decisions, Balanced metrics and events match today's. The visible differences are the `pipeline` label on the metrics above and that building the snapshot republishes the existing `karpenter.sh/do-not-disrupt` annotation events (invalid value, duration-based protection active, grace period elapsed) once more per round; the default recorder discards, so `consolidation decision` log lines appear only once a `recorder` is configured (see the bounded-search RFC).
- **A PDB refuses mid-shift.** The Eviction API returns 429. The executor stops that move with reason `pdb`; pods already evicted stay evicted, and the eviction budgets are charged only for those.
- **A shift source is never emptied.** Its `PreferNoSchedule` taint lapses after `taintTTL` (10 minutes by default). A `PreferNoSchedule` taint never makes a pod unschedulable.
- **A cycle that executes only shifts.** Shifts return no `Command`, so `disrupt()` reports no action and requeues after its 10 s polling period rather than immediately.
- **A merge and a shift share a source.** The executor defers the merge (`deferred: source draining`). A later cycle, or Emptiness, removes the node once it is empty.
- **Validation finds churn.** Merges are rejected as today. Shifts that no longer pass `Fast` on the fresh snapshot are aborted with a `churn:` reason.
- **Several NodePools under the default config.** The adapters inherit the existing scan's candidate order, including its nondeterminism (see [Parity](#parity-as-the-compatibility-guarantee)).
- **A generator on a frozen clock.** The simulator's clock does not advance during generation, so a deadline alone cannot stop a random generator; the random generators cap one `Generate` call at 1,000 attempts when `search.maxAttempts` is 0.

## Examples

**The default configuration, one pass.** A 100-node fleet with two candidates whose combined removal fails the Balanced threshold, though the first alone passes it. `disrupt()` calls the pipeline with the candidates and budgets. Round 1 runs `upstream-multi`, which calls `MultiNodeConsolidation.ComputeCommands`: the binary search probes its one prefix, both candidates, the evaluator rejects it, and the round summary logs `moves: 0`. Round 2 runs `upstream-single`, which calls `SingleNodeConsolidation.ComputeCommands`: the scan finds a replace for the first candidate, validates it after 15 s, and the adapter converts it to a merge. The `upstream` planner re-checks it (`accepted: true`), `upstream-queue` returns it, and `disrupt()` starts it on the queue, exactly as today. With a `log` recorder at V(2) or the `file` recorder, the log holds six lines: the candidates summary, the first round's summary (`moves: 0`), the merge at `moves`, the second round's summary, and the merge at `plan` and `execute`.

**An experimental configuration, one pass.** The same fleet after a scale-down: every node 40% utilized, no merge clears the threshold. `uniform-random` draws a quarter of the sources. `random-shift` proposes moving two pods from node A (35%) to node B (70%); the potential falls by 0.003 and the move evicts two default-cost pods, so with λ = 0.5 its energy is negative. `anneal-plan` keeps it with three other shifts, no two touching a node, within 20 pod evictions per minute. `shift-evict` waits 15 s, re-checks each shift, taints the four sources `PreferNoSchedule`, and evicts eight pods through the Eviction API. Nothing is deleted; `disrupt()` reports no command. Over the next cycles the four sources drain, and Emptiness removes them.

## Evidence

The pipeline was measured in an in-process simulator (3-hour runs of synthetic fleets at 50 to 1,000 nodes, ten runs per configuration) and on a KWOK cluster at 50 and 200 nodes. What the measurements showed:

- **Configured as today's methods, the pipeline is equivalent to them.** Over 3-hour simulated runs at 200 nodes its steady-state cost was within -0.75% to +0.34% of today's on three workloads, and 7 of 60 runs were identical on the four metrics compared. On the KWOK cluster the pair's end cost differed by +1.7% at 50 nodes and +0.3% at 200, no measurable difference. Both measurements used the fine adapters; the coarse adapters are covered by the parity test ([Parity as the compatibility guarantee](#parity-as-the-compatibility-guarantee)).
- **Random generation did not beat today's search.** With the same planner and simulation budget it was within 3% of the existing search in 3 of 12 comparisons: 1.5 to 5.1% cheaper in seven of eight cases on fleets that start fragmented (the eighth was equivalent, +0.1%) and 1.4 to 4.3% dearer after a diurnal scale-down. On the KWOK cluster at 200 nodes it ended 10.6% dearer and used 52% more controller CPU.
- **Shifts cost more than they saved at equal eviction budgets.** Merge plus shift had 1.7 to 7.0% higher cost than merge only over 3-hour runs with the same pod-eviction budget.
- **Annealing at plan time** lowered steady-state cost by 3.0% on one workload for 38% more evictions; on another workload the 1.0% it saved was not a measurable difference, for 21% more evictions.
- **Plans mostly did not survive placement.** Under default kube-scheduler scoring about one in four pods evicted by the experimental configuration landed on its planned node, against about one in two for today's commands. The `steered` executor raised the experimental configuration's share to 0.43; `MostAllocated` scheduler scoring raised it by 29% under spread scale-down and 44% under deletion-cost scale-down.
- **Early stopping is cheap but not free.** Stopping a random generator after 50 idle attempts removed 75 to 85% of its attempts and 13 to 31% of mean decision CPU per pass at 200 nodes (37% at 1,000), with steady-state cost unchanged within 3%, but with 7 to 9% more evictions on two of the three workloads at 200 nodes (8.5% fewer at 1,000) and 7 to 15% fewer moves found per search.

The design's value is as an instrument: parity-checked stage boundaries, equal-compute comparison, a decision log, and one code path from simulator to cluster. No new strategy is a candidate default.

## Alternatives Considered

### Alternative 1: Add knobs to the two existing methods

Expose ordering, acceptance and limits as options on `MultiNodeConsolidation` and `SingleNodeConsolidation`. One code path, but the interleaving remains: each knob is another branch inside the binary search or the scan, and shifts do not fit a loop whose output is a node-deleting `Command`. Rejected because it makes the methods harder to reason about without making any stage swappable.

### Alternative 2: Make the Balanced `Evaluator` the only seam

Swap scorers through `Evaluator` and leave generation alone. Smaller, and it covers Use Case 1. It cannot vary candidate selection or generation, cannot express shifts, and does not cap compute. The pipeline keeps this seam's semantics in `Guide`, so this is a strict subset of the proposal.

### Alternative 3: An external compaction controller (descheduler-style)

Evict pods from nodes below a utilization threshold and let kube-scheduler with `MostAllocated` repack them. No Karpenter change, but it does not see Karpenter's budgets, prices, `consolidateAfter` or in-flight commands, and it competes with consolidation over the same nodes. The simulator study ran it as a baseline, not as the design.

### Alternative 4: Asynchronous generation now

Run generators continuously, score moves into a priority queue, and execute as budgets allow. This needs move invalidation when cluster state changes, a staleness policy, and a new interaction with validation and `consolidateAfter`. Doing it first would make parity impossible to establish. The stage boundaries are its prerequisite, so it is deferred (Open Question 8).

### Alternative 5: An offline optimizer per cycle

Solve the packing exactly (ILP or CP-SAT) on the snapshot each cycle. Optimal for the snapshot but costly at scale, and blind to disruption ordering and kube-scheduler placement. The simulator study used it as a lower bound on small fleets, not as a strategy.

### Alternative 6: Ship without a feature gate, or out of tree

Replace the two methods outright, or ship the pipeline as a separate controller. The first gives no compatibility story and no rollback; the second cannot reuse the disruption controller's candidates, budgets, validators and queue without duplicating them, and competes with in-tree consolidation over the same nodes. Rejected: the gate plus the parity test is what makes the change reviewable.

## Backward Compatibility

- **Gate off (the default).** `disruptionMethods` returns no options, so `NewController` builds exactly today's methods; the pipeline packages are compiled in but never constructed. This holds by construction, not by a byte-for-byte test.
- **Gate on, flag empty.** The decisions are today's, as the parity test checks. Some metric series move from `multi` and `single` to `pipeline`; dashboards that filter on `consolidation_type` need the new value.
- **Gate on with a custom config.** Experimental by definition. Shift executors add the `shift-source` taint and `shift-source-since` annotation to nodes.
- **Rollback.** Turning the gate off restores today's methods at the next start. Shift-source taints already on nodes are `PreferNoSchedule` and lapse with their nodes, but nothing lifts them once the pipeline stops running (Open Question 2).
- **Config errors** fail at startup with the validation error, in both the bare and the `pipeline:`-wrapped form.

## Graduation Criteria

- **Alpha.** Shipped by the [bounded-search RFC](consolidation-bounded-search-and-decision-log.md): the `ConsolidationPipeline` gate, default off, with the coarse adapters, a search budget and a decision log as the only supported configurations. Every strategy in this RFC beyond those is experimental and does not ship at alpha.
- **Beta.** A 10,000-state parity run with no unexplained mismatch, repeated on a schedule; fine-mode parity for events and Balanced metrics, not only decisions; a decision on which strategies, if any, are recommended from the evidence; Open Questions 1, 3 and 9 resolved. The gate still defaults off at beta unless a strategy is shown to be worth enabling.
- **GA.** Production feedback on at least one non-default configuration, a decision on asynchronous generation, and a decision on whether the two existing methods are retired in favor of the adapters.

### Parity as the compatibility guarantee

`TestParity` (`pkg/controllers/disruption/pipeline/parity_test.go`) is the proof that the gate on with the default config changes nothing.

**What one case does.** For each seeded cluster state, the test runs today's multi-node and then single-node consolidation the way `disrupt()` does, then runs one pipeline cycle with each adapter configuration, and requires identical decisions: the kind, the candidates in order, the replacement instance-type options in order, the budget consumed per NodePool, and any error.

**What it holds fixed.** States have 4 to 40 nodes and one to three NodePools, on the KWOK instance catalog and on a thinned copy of the disruption suite's catalog, under both consolidation policies. Both paths get candidates ordered by name, validators that skip the wait, and one fake clock.

**The existing scan's own nondeterminism.** With more than one NodePool, the single-node scan interleaves pools in Go map order; with two pools the second-inserted pool comes first about one time in eight. On single-pool states the test runs the existing methods twice as a control and requires an exact match. On multi-pool states the reference is the set of decisions the existing methods make over 8 runs; a pipeline decision outside that set sends them back for up to 256 more runs before it counts as a mismatch.

**Result.** A 10,000-state run matches in every configuration (coarse, fine, and fine with the `balanced` planner on Balanced states) for each policy and catalog, on states where the existing methods themselves decide to act in 83 to 91% of cases. A 1,000-state run over both policies and both catalogs takes about 21 minutes, each of the four 10,000-state runs (one per policy and catalog) about an hour, and a 40-state run is in `make test`.

**What it catches.** Bugs injected into the fine adapters that change decisions are caught: dropping the multi-node budget decrement (44 of 60 states matched) and making the planner accept every move (85 of 100 on Balanced states; correctly invisible on `WhenEmptyOrUnderutilized` states). A bug that changes only compute, such as skipping the `CanPass` pre-filter, is not caught and cannot be. That gap is what the search budget's metrics and the decision log cover.

## Open Questions

The recorder's default and verbosity, the meaning of `budget_remaining`, the per-pass budget and what the registry admits are settled in the [bounded-search RFC](consolidation-bounded-search-and-decision-log.md). Both configuration forms are parsed strictly (see [Proposed Spec](#proposed-spec)) and `shift-source-since` is proposed as a well-known annotation.

1. **Shift moves and `consolidateAfter`.** Evicting pods from a shift source restarts its `consolidateAfter` window, and an emptied source is removed by Emptiness only after the window passes, so a chain of shifts delays its own payoff. Should pipeline-initiated evictions be exempt from the pod-event timestamp (a change to the existing candidate code), or should planners price the delay into ΔE?
2. **The `PreferNoSchedule` shift-source taint.** It is lifted only on `Execute` and `Idle` (which runs only when a plan is empty), and nothing removes it after rollback. Should a small reconciler own it instead of the executor?
3. **Plan fidelity under kube-scheduler.** A shift plans a destination, but kube-scheduler places the replacement pod, and under `LeastAllocated` scoring it spreads them. `steered` is implemented in the simulator only; in the controller it behaves like `shift-evict`. How much fidelity loss is acceptable, and is a mutating webhook for preferred node affinity in scope at all?
4. **The multi-node limit in fine mode.** Today's method bounds the multi-node search with a one-minute `context.WithTimeout` that can stop a simulation part-way; the fine adapter checks the same duration on the injected clock between steps, so it can finish one simulation past the limit. Is that difference acceptable for a production default, or should fine mode keep a context deadline as well?
5. **Metric label drift.** Some series move to `consolidation_type="pipeline"`, others keep `multi` and `single`, and Balanced's score metrics disappear in fine mode. Should the pipeline report the label of the existing method whose move won instead?
6. **Snapshot cost.** `BuildSnapshot` runs once per round (a NodePool list with instance types, a PDB list, a deep copy of the nodes and a pod list per node) and has not been profiled at any scale.
7. **Deletion-cost steering.** The planner's next merge targets could feed the `PodDeletionCostManagement` ranking so that ReplicaSet scale-down empties them at no disruption cost. Should the planner export that ranking?
8. **Asynchronous generation.** Given these stages, what is the staleness and invalidation policy for a move generated in one cycle and executed in a later one?
9. **Which strategies ship.** `controllers.go` imports `strategies/all`, so every experimental strategy is compiled into the controller and selectable by configuration behind the gate, while the evidence recommends none of them as a default. The bounded-search RFC drops that import at alpha, so the controller compiles only the adapters, and its allowlist rejects the rest at startup. What result would justify compiling an experimental strategy back into the controller and supporting it?
10. **Should the `Recorder` be a public extension point?** It is the stable part of the design and the part operators would most plausibly want to plug. Making it public makes the event schema an API.
11. **Loose ends in the implementation.** The `none` potential's factory ignores its `params` instead of decoding them strictly, so a misspelled key under `potential.params` is accepted at startup; it should decode them like every other strategy. The conformance suite checks that generated moves pass `Fast`, not `Full`, and executors are exercised only in the simulator; a beta criterion could ask for both.

## Frequently Asked Questions

### Does the gate on change what my cluster does?

Not with an empty flag: the coarse adapters call today's `ComputeCommands`, and the parity test holds them to today's decisions on 10,000 synthetic states. What changes is observability: a `pipeline` label on some metrics, and a decision log once a `recorder` is configured.

### Why keep both a coarse and a fine adapter?

The coarse adapter is today's behavior by construction and is the safe default. The fine adapter is the one that makes the planner swappable, and it binds the search budget without the `SearchObserver` hook; it re-implements the two searches and is pinned to today's decisions by the same parity test. Which one should carry the budget into Karpenter is the bounded-search RFC's Open Question 3.

### Why a potential at all? Balanced already scores moves.

Balanced scores savings against disruption, and a shift saves nothing now. A potential gives a shift a value today for a saving that arrives later, when the drained node is removed. Without one, no planner can rank shifts.

### Is random generation a serious proposal?

No. It is the control that showed what today's ordering is worth: at equal compute, random generation was within 3% of the existing search in 3 of 12 comparisons and dearer after a diurnal scale-down. It is not proposed for production; the bounded-search RFC drops it from the controller build at alpha and its allowlist rejects it at startup, and what would justify compiling it back in is Open Question 9.

## References

- [Balanced consolidation](balanced-consolidation.md): the evaluator seam, its open question on asynchronous generation, and its FAQ on kube-scheduler placement.
- [Pod deletion cost controller](pod-deletion-cost-controller.md): the fragmentation case and the `PodDeletionCostManagement` gate.
- [Bounded search budgets and a decision log](consolidation-bounded-search-and-decision-log.md): the first increment of this design.
- Code this RFC adds: the `pkg/controllers/disruption/pipeline` package (interfaces, adapters, method, registry, conformance checks, parity harness and test) with `pipeline/strategies/` for the experimental strategies; `pkg/controllers/disruption/pipeline_hooks.go`; the gate and flag in `pkg/operator/options/options.go` and the wiring in `pkg/controllers/controllers.go`; an in-process simulator under `hack/consolidation-sim` and a KWOK suite under `test/suites/performance/` as the test harnesses the whole-run and cluster checks run on.
