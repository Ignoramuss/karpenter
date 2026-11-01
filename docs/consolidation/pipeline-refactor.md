# The consolidation pipeline: what the refactor changed, and the trade-offs

This document explains the refactor of Karpenter's consolidation into a pluggable pipeline, for readers who know
Karpenter's features but not this research branch. It says what consolidation does today, what the pipeline
changes, what that makes possible, what it costs, and what the experiments found when the new pieces were put to
use. The RFC with the full design is [`designs/consolidation-pipeline.md`](../../designs/consolidation-pipeline.md);
the experiments are in the paper at [`../../paper/paper.md`](../../paper/paper.md).

In one paragraph: consolidation is split into four stages (which nodes to look at, which moves to try, which moves
to run, how to run them) with shared services between them (a cluster snapshot, feasibility checks, a "potential"
that scores fleet shape, a decision log). Each stage is chosen by name from a configuration file. Everything sits
behind a new alpha feature gate, `ConsolidationPipeline`, which is off by default. With the gate off, nothing
changes. With the gate on and no configuration, adapters reproduce today's consolidation decisions exactly, and a
differential test checks that on 10,000 generated cluster states.

## 1. How consolidation works today

Every 10 seconds the disruption controller runs its methods in order: emptiness, static drift, drift, multi-node
consolidation, single-node consolidation (plus node repair when that gate is on). The first method that returns
commands wins, and its commands go to the orchestration queue, which taints the nodes, launches any replacements,
waits for them to be initialized and deletes the NodeClaims; the node termination controller then drains each node
through the Eviction API and terminates it.

Both consolidation methods start from the same **candidates**: nodes that have been consolidatable for
`consolidateAfter`, carry no `karpenter.sh/do-not-disrupt` pod, whose pods' PodDisruptionBudgets allow eviction,
and whose NodePool's disruption budget has room. Candidates are sorted by savings ratio: the node's price over the
eviction cost of its pods, highest first.

- **Multi-node consolidation** binary-searches over how many of the highest-savings-ratio candidates can go at once:
  take the first k, simulate scheduling their pods onto the remaining nodes plus at most one new node, and accept
  if the result is valid and cheaper; grow or shrink k accordingly. The search window is up to 101 nodes and the
  whole search stops after 1 minute of wall-clock time.
- **Single-node consolidation** walks the candidates one at a time (interleaving NodePools), simulates each one's
  removal or replacement, and stops at the first acceptable command. It has a 3-minute limit and records which
  NodePools it did not reach so the next pass starts with them.
- **Acceptance** depends on the NodePool's `consolidationPolicy`. `WhenEmptyOrUnderutilized` accepts any command that
  reduces cost. Before either policy scores a command, the simulation result passes the price filters (a replacement
  must be cheaper than what it replaces, and is never more than one node) and, behind its own gate, the spot-to-spot
  rule: a spot node is replaced by spot only with `SpotToSpotConsolidation` on, and single-node consolidation
  additionally requires at least 15 cheaper instance types and launches only the 15 cheapest, so the new node is not
  itself the next consolidation target. `Balanced` scores a command by its savings fraction over its disruption
  fraction and accepts it at or above 1/k with k = 2.
- **Validation.** A chosen command waits 15 seconds and is re-simulated; if the cluster changed in a way that
  breaks it, it is rejected.

This design has four properties that the refactor set out to change.

1. **The steps are interleaved.** Ordering, generation, acceptance and validation all happen inside one
   `ComputeCommands` call per method. Balanced's acceptance test is called from inside both search loops, so trying
   a different acceptance rule means editing the loops.
2. **Work is bounded by time, not by compute.** A full single-node scan costs one scheduling simulation per
   candidate; the multi-node limit is wall-clock. Two strategies cannot be compared at equal compute, and a
   decision can depend on how fast the machine is.
3. **Every move deletes a node.** Delete and Replace both remove their sources. A move that improves packing
   without removing a node, such as moving a few pods off a sparse node so it can be emptied later, cannot be
   expressed, so it has no value to score. After a ReplicaSet scale-down spreads deletions across every node, this
   is exactly the move that is needed.
4. **Rejected alternatives leave no record.** Karpenter logs the command it starts and emits events for
   unconsolidatable candidates, but keeps no structured record of every move it considered and why each lost.

## 2. What the refactor does

### One new method, behind a gate

With `--feature-gates ConsolidationPipeline=true`, the controller runs one method, `PipelineConsolidation`, where
multi-node and single-node consolidation used to run. The order becomes repair (when gated), emptiness, static
drift, drift, pipeline. Emptiness, drift, the orchestration queue, termination and drain are untouched. The method
reports the `Underutilized` disruption reason, so today's budgets apply to it unchanged.

The configuration comes from `--consolidation-pipeline-config` (a file path, or inline YAML or JSON). An empty
value selects the upstream adapters. An invalid value fails at startup.

### Four stages and shared services

| Stage | Interface | Job |
| --- | --- | --- |
| 1. Candidates | `CandidateSelector.Select(snapshot, rng) -> sources, destinations` | choose which nodes may lose pods and which may receive them |
| 2. Moves | `MoveGenerator.Generate(snapshot, sources, destinations, rng, budget) -> []Move` | propose moves within a search budget |
| 3. Plan | `Planner.Plan(snapshot, scoredMoves, budgets, rng) -> Plan` | decide which moves run, within every budget |
| 4. Execute | `Executor.Execute(plan) -> Report` | carry the plan out |

Stages 1 and 2 only propose. Stage 3 decides. The shared services are:

- **Snapshot:** an immutable, cheaply forkable view of the cluster (nodes, pods, loads, PDB allowances, Balanced's
  NodePool totals). Planners try moves on forks of it.
- **Feasibility:** a fast check from the snapshot alone (capacity, taints, node selectors and required node
  affinity, bound-volume zones, hostname anti-affinity, `do-not-disrupt`, PDB allowances), and a full check that
  runs Karpenter's real scheduling simulation. Topology spread constraints are only checked by the full check.
- **Potential:** a function of fleet shape that falls as nodes become packed or empty (section 3), used to give
  moves that delete no node a value.
- **Scorer:** every move gets its potential change, its disruption (the sum of its pods' eviction costs, plus one
  unit per removed node), its savings per hour, and both normalized by NodePool totals the way Balanced normalizes.
- **Search budget:** per cycle, a cap on generation attempts per generator, a cap on full simulations shared across
  generators, and a deadline on the injected clock.
- **Recorder:** one structured event per stage decision (section "The decision log" below).

A **move** is either a *merge* (empty some source nodes into existing nodes plus at most one new node, then
remove the sources; upstream's Delete and Replace both map to merges), a *shift* (move some pods to another node and
remove nothing), or a *right-size* (defined, no generator emits it yet).

### The configuration

```yaml
pipeline:
  candidates: {name: uniform-random, params: {sourceFraction: 0.25}}
  moves:
    - {name: random-merge, params: {confirm: true}}
    - {name: random-shift}
  movesMode: pooled                       # one plan over all generated moves ("sequential" = one generator per round)
  planner:   {name: entropy-greedy, params: {lambda: 0.1}}
  executor:  {name: shift-evict}
  potential: {name: util-entropy}
  search:    {maxFullSimulations: 16, maxAttempts: 500}
  budgets:   {podEvictionsPerMinute: 18, workloadEvictionsPerMinute: 2}  # optional; per NodePool, per owner
  seed:      101
```

Registered strategies (every one is selectable by name; the defaults when the flag is empty are marked):

| Stage | Name | What it does |
| --- | --- | --- |
| 1 | `upstream` (default) | upstream's candidates, in its savings-ratio order |
| 1 | `uniform-random` | a uniform sample of eligible nodes as sources; every node as a destination |
| 1 | `util-band` | sources below the fleet's mean utilization, destinations above it |
| 1 | `potential-weighted` | sources sampled in proportion to the potential drop from emptying them |
| 2 | `upstream-multi`, `upstream-single` (default, `mode: coarse`) | the upstream searches. Coarse mode wraps upstream's own `ComputeCommands`; fine mode re-implements the search in stages so the planner can be swapped |
| 2 | `random-merge` | pick 1 to 3 random sources, first-fit their pods into a random order of destinations, optionally confirm with a full simulation; `idleAttempts` stops the search after that many attempts in a row found nothing (added for Exp 9; off by default) |
| 2 | `random-shift` | move 1 to 4 random pods from a source to a fuller destination; `idleAttempts` as above |
| 2 | `greedy-shift` | per sampled node pair, the pod set that most lowers the potential; `idleAttempts` as above |
| 2 | `swap` | exchange pods between a CPU-stranded and a memory-stranded node |
| 3 | `upstream` (default), `savings-only` | the first acceptable move within budgets; Balanced scoring where a NodePool asks for it |
| 3 | `balanced` | the same, scoring every NodePool as Balanced |
| 3 | `entropy-greedy` | every move whose energy falls (section 3), taken in order of energy saved per unit of disruption, no two touching a node, within budgets |
| 3 | `anneal-plan` | simulated annealing over move sequences on the snapshot (`t0` the start temperature, `cooling` the geometric factor per step, `steps` 400, `maxLength` 8, `temperatureUnit: per-node` divides the temperature by the node count so one value means the same acceptance odds at every fleet size); runs only a net-downhill sequence |
| 3 | `anneal-online` | one annealing step per cycle, executed on the cluster (experiment-only) |
| 3 | `sequence` | chains shifts that empty a node and scores the chain with its final delete |
| 4 | `upstream-queue` (default) | validate merges and hand them to the orchestration queue |
| 4 | `shift-evict` | merges to the queue; shifts by the Eviction API, with a `PreferNoSchedule` taint on the source |
| 4 | `steered`, `make-before-break` | `shift-evict` plus a preference for each pod's planned node; the latter evicts only into initialized destinations |
| - | potentials `none` (default), `load-entropy`, `util-entropy`, `util-quadratic` | section 3 |

### Reproducing upstream exactly: the adapters and the parity test

The default configuration runs the **coarse** adapters: stage 2 calls upstream's own multi-node and single-node
`ComputeCommands`, so upstream's search, acceptance, metrics and events all run as today, and the pipeline only
converts the result into a move and back. This mode is upstream by construction.

The **fine** adapters re-implement the two searches in stages: upstream's candidate order, the prefix binary search
with its budget pre-filter (candidates whose NodePool's node budget is already used up are dropped from the sorted
list, in order, so each NodePool contributes at most its budget) and instance-type filter (when a replacement type is
also one of the candidates' own types, every replacement type priced at or above that candidate's price is dropped,
since keeping that candidate would be the same consolidation with less disruption), the single-node scan with its
3-minute limit and its resume state. Where upstream consults its acceptance test, the fine adapters consult the
configured planner. This is what makes the stages swappable: with `mode: fine`, a different planner changes acceptance
without touching the search.

`TestParity` checks both. For each of thousands of seeded cluster states (4 to 40 nodes, one to three NodePools,
two instance catalogs, both consolidation policies), it runs upstream's methods and then one pipeline cycle with
each adapter configuration, and requires identical decisions: the kind, the candidates in order, the replacement
instance-type options in order, the budget consumed per NodePool. Results: 1,000 of 1,000 states matched per
policy and catalog, and then 10,000 of 10,000 in every configuration, on states where upstream itself decided to
act in 83 to 91% of cases (`upstream_decided` 8,347 to 9,141 of 10,000 per chunk in
`experiments/parity/chunks/*.summary.txt`). The test handles one wrinkle:
upstream's single-node scan interleaves NodePools in Go map order, so with more than one NodePool upstream itself is
nondeterministic; the test compares against the set of decisions upstream makes over repeated runs. A 40-state
version runs in `make test`.

Two further checks ran the pipeline-as-upstream configuration (called B4 in the experiments: the fine adapters
with the upstream planner and the queue, not the coarse default) against upstream over whole 3-hour simulated
runs and on a KWOK cluster. In the simulator, its steady-state cost was within -0.75% to +0.34% of upstream's
across six workload-policy pairs, every 90% interval within -1.8% to +1.0% (seven of 60 seeds matched upstream
exactly on steady-state cost, evictions, nodes removed and final node count; the other 53 were not diagnosed. The
stated but untested hypothesis is that the simulator's candidate shuffle consumes its seeded random stream
differently across the pipeline's method boundaries).
On the KWOK cluster, no difference between the two was resolved at five seeds.

### Shift moves and eviction budgets

Shifts cannot go through the orchestration queue, because the queue taints and deletes every node it is handed. The
`shift-evict` executor runs them itself: it re-checks each shift on a fresh snapshot after the validation delay,
taints the source `PreferNoSchedule` under an experimental key, and evicts the pods through the Eviction API in
order, stopping the move if a PDB refuses. Shifts remove no node, so NodePool node budgets cannot see them; the
pipeline adds pod-eviction budgets per NodePool and per workload (controlling owner) per minute, charged with the
pods actually evicted.

### The decision log

Every stage decision is one structured event: cycle, time on the injected clock, stage, strategy, the move (kind,
sources, destinations, pod count), its scores, whether it was accepted, and if not, why (`not downhill`, `node budget
for default: 2 > 1`, `conflicts with m4`, `validation: churn`). The controller writes them at verbosity 1 (Karpenter's
`--log-level debug`); the simulator writes them to a file. Given the same seed, configuration and commit, the log is
byte-identical.

### Size

The pipeline package is 41 new files and 12,356 lines, of which 3,356 lines in 8 files are tests (62 top-level
test functions, the parity test among them). Six upstream-owned files are touched: five are modified by 70 added
and 2 removed lines (the gate and flag in `options.go`, the wiring in `controllers.go`, the test options, one
documented metric value, and two registered experimental annotations in `labels.go`: the shift-source timestamp
and a simulated volume zone), and one is new, 61 lines of
exported hooks that change no behavior. The disruption controller itself is not changed. The simulator
(`hack/consolidation-sim`) is another 5,400 lines of Go, 1,600 lines of tests and a trace file.

## 3. The potential, in brief

The planners that are new need a number for a move that removes no node. The pipeline's answer is a **potential**:
a function of the fleet that is lowest when every node is either packed or empty. The default, `util-entropy`, is

    Phi = (1/P) * sum over nodes i of  price_i * h(u_i),    h(u) = -u ln u - (1-u) ln(1-u)

where u_i is the node's dominant-resource utilization and P is the fleet's total price when the cycle's snapshot was
taken (forks keep the same P, so removing a node never shrinks the denominator). h is zero at u = 0 and u = 1 and
largest at 50%, and it is concave, so moving load from a less-utilized node onto a more-utilized one of the same price
and size always lowers Phi. A planner then scores a move by an **energy change**

    dE = alpha * dPhi + lambda * D - mu * S

with D the normalized disruption and S the normalized savings. Balanced's acceptance rule is the special case alpha =
0, lambda = 1, mu = k; `WhenEmptyOrUnderutilized` is the limit lambda -> 0: any move with S > 0. With alpha > 0 a
shift, which saves nothing now, can still be worth its disruption if it lowers Phi enough. The paper discusses what
the data say about this.

## 4. What the pipeline makes possible

- **Change one thing at a time.** The planner can change while candidate selection, generation and execution stay
  on the upstream adapters, so the parity test still pins the stages not under study. The experiments came close
  to this: upstream's candidate order and searches (the fine adapters) with three different planners at one search
  budget, and the same planner with two different generators; those arms ran pooled with capped search and
  executed through `shift-evict`, which sends merges, the only moves the upstream searches produce, to the
  orchestration queue.
- **Compare at equal compute.** `maxFullSimulations` and `maxAttempts` cap search per cycle, so two strategies can
  be given the same budget. Upstream's time limits cannot do this.
- **Express moves that delete no node**, and bound their disruption with pod-eviction budgets.
- **Reproduce a decision.** One seeded random source flows through every stage and time comes only from the
  injected clock, so a cycle can be replayed exactly. The simulator's determinism test checks byte-identical logs.
- **See why a node was not consolidated.** The decision log has every move considered in the cycle and the reason
  it lost.
- **Run the same code in a simulator and on a cluster.** The in-process simulator and the KWOK suite drive the
  same method with the same configuration format and write the same log.
- **A home for the Balanced RFC's open question.** The Balanced design lists asynchronous generation (generate,
  score, enqueue, execute as budget allows) as future work. The stage boundaries are its prerequisite, though this
  refactor keeps all four stages inside one synchronous pass.

## 5. What it costs

- **A second copy of upstream's search.** The fine adapters re-implement the multi-node binary search and the
  single-node scan. Every upstream change to those loops must be mirrored, and the parity test is what catches a
  drift. The parity test compares decisions only, not events or metric deltas, and it cannot catch a bug that
  changes compute without changing decisions (a mutation that skipped a pre-filter went undetected).
- **Metric and event changes with the gate on.** Some series move from `consolidation_type="multi"` and `"single"`
  to `"pipeline"`, while others keep the old labels. In fine mode, Balanced's score metrics and `ConsolidationApproved`
  events are not emitted, because the fine planner mirrors the evaluator rather than reusing it.
- **Experimental strategies ship in the binary.** The operator imports every registered strategy, so any of them
  can be selected by configuration behind the alpha gate. Whether alpha should ship only the adapters is an open
  question in the RFC.
- **Shift-source taints have lifecycle gaps.** They are lifted only on later execute or idle calls, and nothing
  removes them if the gate is turned off. They are `PreferNoSchedule`, so the harm is bounded, but they are a new
  thing to see on nodes.
- **Shifts interact with `consolidateAfter`.** Evicting pods from a source restarts its `consolidateAfter`
  window, and an emptied source is removed by emptiness only after the window passes, so a chain of shifts delays
  its own payoff. Whether pipeline-initiated evictions should be exempt is unresolved.
- **Fine mode's multi-node limit is on the injected clock.** Upstream's is a wall-clock context deadline that can
  stop a scheduling simulation part-way. The two agree with a real clock except at the granularity of one
  simulation, which is what makes fine mode deterministic, but it is a difference.
- **A snapshot per pass.** Building it deep-copies the nodes and lists pods and PDBs. It is small next to a
  scheduling simulation, but it has not been profiled at any scale (the only CPU profiles taken were of upstream
  arms); the RFC's own caveat is that it is unprofiled at 5,000 nodes.
- **Measured overhead of the pipeline configured as upstream.** Small, and not consistently in one direction. In the
  simulator over whole 3-hour runs at 200 nodes it spent 5 to 16% *less* CPU per disruption pass than upstream (for
  example 93 vs 111 ms on the diurnal workload; from the raw `b4` and `exp1-baselines-n200` runs), and on the
  compressed diurnal workload (35-minute runs at 50 and 200 nodes) 3 to 4% more decision CPU on the five seeds that
  pair with the KWOK runs (+3.7% per pass at 50 nodes, +4.6% at 200), but +2.6% and -0.03% per pass over all ten
  simulator seeds; neither five-seed paired interval excludes zero. On the KWOK cluster at 200 nodes: 3.4% more
  controller CPU (154 vs 149 core-seconds over 30 minutes, the paired interval including zero), 5% more peak memory
  (227 vs 216 MB) and 11% more wall-clock time in decision evaluation (462 vs 415 s), with no resolved difference in
  cost or evictions.
- **Loose ends.** The decision log's `budget_remaining` field is never set; the conformance suite (the shared test
  suite that every registered selector, generator, planner and potential that builds without a live cluster must pass)
  checks generated moves against the fast feasibility check (the snapshot-only check of section 2), not the full one,
  and does not exercise executors; the RFC's open questions list two items the code already handles (the
  `pipeline:`-wrapped configuration form is decoded strictly, and the shift-source annotation is registered; the code
  and the RFC text that calls them open date from the same commit), so the RFC is behind the code there.
- **12,000 lines to own.**

## 6. What the experiments found when the new pieces were used

The point of the pipeline was to answer questions, and the answers so far favor upstream. In the simulator
(preregistered, 10 seeds per cell; details and intervals in the paper):

- **Random generation is not a drop-in for upstream's search.** With the same planner and the same simulation budget,
  it ended 1.5 to 5.1% cheaper in seven of eight cells on workloads that start fragmented (the eighth was equivalent
  at +0.1%) but 1.4 to 4.3% dearer after a diurnal scale-down; evicted 48 to 129% more pods on the fragmented and
  mixed-shape workloads and 26 to 41% more on the diurnal one at 1,000 nodes (at 200 nodes on the diurnal workload,
  -5% to +13% with intervals spanning zero); and converged more slowly. On the KWOK cluster it ended 10.6% dearer at
  200 nodes and used 52% more controller CPU. Section 7 of the paper explains why random costs more.
- **Shifts, as configured, cost more than they saved.** Merge-plus-shift had 1.7 to 7.0% higher integrated cost
  than merge-only at an equal pod-eviction budget, because the budget was spent on moves that did not pay off.
- **Annealing at plan time bought 1 to 3% of steady-state cost for 21 to 48% more evictions.**
- **The savings-versus-disruption frontier did not dominate upstream.** Balanced was the stronger operating point on
  both workloads tested; the pipeline reached the frontier only where it evicted least.
- **Plans mostly did not survive placement.** Under default kube-scheduler scoring, about three in four pods evicted
  by the pipeline landed somewhere other than planned (upstream: about one in two). Steering replacement pods toward
  their planned node was the most effective lever.
- **Under a tight eviction budget, every new component hurt.** Removing the potential term, the annealing, the
  random generation or the budget itself each lowered cost.
- **The pipeline reproducing upstream was equivalent to upstream** (within 3% of steady-state cost; no resolved
  difference on KWOK).
- **Early stopping is free; biased selection pays only at scale (Exp 9, run after the paper; not preregistered).**
  Letting each random generator stop after 50 consecutive attempts that found nothing cut attempts per searching pass
  by three quarters or more and mean decision CPU per pass by 13 to 31% at 200 nodes and 37% at 1,000, with cost
  unresolved within about 2%; the searching pass itself got only 13 to 27% cheaper, because the simulations, not the
  attempts, are most of its cost. Choosing sources in proportion to the potential drop from emptying them added no
  CPU saving; at 1,000 nodes it ended 2.8% cheaper than uniform selection with 13% fewer evictions, and at 200 nodes
  it was unresolved on cost. The utilization-band selector often had no destination on a consolidated fleet (no node
  between the mean utilization and 95% full) and ended 6 to 8% dearer on two workloads with a third fewer evictions.

So the pipeline's demonstrated value is as an instrument: parity-checked stage boundaries, equal-compute
comparison, a decision log, and one code path from simulator to cluster. No new strategy is a candidate default.

## 7. Pros and cons

| For | Against |
| --- | --- |
| Off by default; with the gate off, no code path changes | 12,000 new lines and a second copy of the two upstream searches to keep in sync |
| Decisions proven identical to upstream on 10,000 states, and equivalent over whole 3-hour simulated runs | Parity covers decisions, not events or metrics; fine mode drops Balanced's metrics |
| Any stage swappable by name; strategies compared at equal compute | Experimental strategies are compiled into the operator |
| Moves that delete no node, with pod-eviction budgets | Shift taints and `consolidateAfter` interactions are unresolved |
| Seeded, deterministic, fully logged decisions | A few percent more controller CPU and memory in the as-upstream configuration |
| One code path for the simulator, KWOK and a real cluster | The new strategies have not beaten upstream on any tested workload |
| The structure the Balanced RFC's asynchronous-generation question needs | Generation is still synchronous; async would need staleness and invalidation rules |

## 8. If this goes further

The RFC's own bar for beta is: a clean 10,000-state parity run (done); published experimental results with a
decision on which strategies, if any, to recommend (done: none yet); resolution of the shift/`consolidateAfter`
interaction, plan fidelity, what ships in the operator, and the configuration loose ends; the shift-source
annotation registered as a well-known annotation (done) and a metric for pipeline search timeouts; and parity for
events and Balanced metrics in fine mode, not only decisions.

Three design choices would decide whether the pipeline is worth carrying upstream:

1. **Ship the adapters only.** Keep the stage interfaces and the coarse and fine adapters in the operator, and keep
   the experimental strategies in the simulator and the test suite. This gives upstream the stage boundaries and
   the decision log at minimal risk.
2. **Reuse the evaluator.** Make the fine planner call Balanced's `Evaluator` instead of mirroring it, so fine mode
   emits the same metrics and events.
3. **Decide on asynchronous generation.** The stages exist; the open design work is move staleness and
   invalidation. If that is not wanted, the pipeline's value is the instrument, and the simulator and KWOK suite
   can carry it without the operator change.
