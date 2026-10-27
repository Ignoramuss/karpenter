# Karpenter Consolidation as Entropy Reduction: Coding Agent Plan

Oct 27, 2026 · @Ignoramuss

## Mission: one autonomous session

Execute this plan end to end in one autonomous Claude Code cloud session on the fork of `kubernetes-sigs/karpenter`: rebase onto upstream main, build a pluggable consolidation pipeline, prove parity, then run, analyze and write up the experiments. Never pause for input. When a choice comes up, take the default under **Decisions fixed for this run** at the end, log it, and keep going.

**Before launching** (for the human; the agent skips this)

- Commit this doc to the fork as `docs/research/PLAN.md`, push, and start a cloud session on the fork with: `Execute docs/research/PLAN.md end to end without stopping for questions.`
- Make upstream reachable: sync the fork on GitHub first, or attach `kubernetes-sigs/karpenter` to the session as an extra repository. The session's GitHub proxy only serves attached repositories.
- Pick an environment with **Full** network access, or **Custom** with the Trusted defaults plus the image registries KWOK and `ko` pull from (for example `registry.k8s.io` and `cgr.dev`). Trusted alone covers Go modules, PyPI, GitHub and Docker Hub. See [cloud environments](https://code.claude.com/docs/en/cloud-environments).
- Add environment variables `BASH_DEFAULT_TIMEOUT_MS=1200000` and `BASH_MAX_TIMEOUT_MS=3600000`; otherwise commands time out after 2 minutes by default and 10 at most.

**What you are building**

- A four-stage pipeline (candidates, moves, budgets and planning, execution) with a registry, so any stage can be swapped by config.
- Upstream adapters that reproduce today's consolidation decisions exactly, proven by a differential parity test.
- A fast deterministic simulator and a KWOK end-to-end harness that share one decision-log schema.
- A family of strategies per stage, from random to heuristic, and an experiment matrix that compares them.

**Definition of done:** all of this exists on the pushed branch.

- [ ] Branch rebased on upstream main; upstream tests green; `make verify` clean.
- [ ] `pkg/controllers/disruption/pipeline`: interfaces, registry, adapters, strategies, conformance and parity tests, behind the `ConsolidationPipeline` gate, off by default.
- [ ] `hack/consolidation-sim/`: the simulator, a determinism test, and the resumable experiment runner.
- [ ] `experiments/PREREGISTRATION.md`, committed before the main runs, and session-profile results for all eight experiments in `experiments/runs/`.
- [ ] `paper/figures/` built by `make figures`, plus `experiments/RESULTS.md`.
- [ ] `docs/research/`: code map, parity report, and a runbook for the full profile and KWOK; RFC draft at `designs/consolidation-pipeline.md`.

**Operating rules**

1. Rebase first. Nothing else starts until the branch sits on upstream main and builds (Phase 0).
2. Never stop to ask. Take the documented default or fallback, record it in `experiments/RESEARCH_LOG.md`, and continue. The only allowed stop is a failed Phase 0 prerequisite, explained in `BLOCKED.md`.
3. Behavior-preserving first. The refactor passes all upstream tests and the parity test before any new strategy lands.
4. Everything new is off by default, behind the gate. Upstream defaults never change.
5. Keep the diff to upstream files small. New code lives in additive packages so the branch rebases cleanly.
6. All randomness flows from one seeded RNG passed explicitly through the stages. No global `rand`, and no wall-clock reads in planning code; use the injected clock.
7. Every run writes a manifest: git SHA, config, seed, scenario, environment, start and end time.
8. No shared or production clusters. Use the in-process simulator, or kind with KWOK inside the session VM.
9. PDBs and `karpenter.sh/do-not-disrupt` are hard constraints for every strategy. A run that violates either is invalid, not a data point.
10. Keep `docs/research/PROGRESS.md` as a checklist mirroring this plan, updated at every milestone. After any context compaction, re-read `PLAN.md` and `PROGRESS.md` before the next action.
11. Commit at every milestone with a descriptive message and push the session's working branch, never the default branch. Per the repo's `AGENTS.md`, never open upstream issues or PRs, or post comments.
12. Long work runs in chunks. Foreground commands time out, so tests and experiments run through resumable commands that write results incrementally and are re-invoked until done.
13. Use subagents for read-only exploration and self-contained files, such as the code map and analysis scripts. Keep one writer per package.

**Self-gates** (check, record, then continue or take the fallback)

- [ ] Gate 0, end of Phase 0: rebased on upstream main; `go build ./...` and the disruption tests pass. No fallback: write `BLOCKED.md` with the exact error and the access needed, then stop.
- [ ] Gate 1, end of Phase 1: 100% parity on sampled states. Fallback: after a time-boxed debugging pass, document each residual mismatch and its root cause in `docs/research/parity.md`, mark B4 provisional, and continue. B2 and B3 run upstream code directly, so the other comparisons stay valid.
- [ ] Gate 2, end of Phase 2: the determinism test passes. No fallback; fix it before any experiment runs.
- [ ] Gate 3, before the main runs: `experiments/PREREGISTRATION.md` is committed. The commit order is the evidence.
- [ ] Gate 4, at the end: `make figures` rebuilds every figure and table from raw runs.

## Related upstream work

The fork is of `kubernetes-sigs/karpenter` core, where the consolidation logic lives. Upstream main, checked at commit cac0d0b (Oct 26, 2026), already contains two pieces this work builds on and must be positioned against.

- [Balanced consolidation](https://github.com/kubernetes-sigs/karpenter/blob/main/designs/balanced-consolidation.md) (`consolidationPolicy: Balanced`, `BalancedK = 2`) scores node-deleting moves as savings fraction over disruption fraction: the savings vs disruption trade-off, implemented. Its open questions list async move generation (generate, score, enqueue, execute as budget allows), which is this pipeline.
- The [pod-deletion-cost controller RFC](https://github.com/kubernetes-sigs/karpenter/blob/main/designs/pod-deletion-cost-controller.md) (gate `PodDeletionCostManagement`) says ReplicaSet scale-down spreading raises cluster entropy, and steers scale-down toward consolidation targets. That is a zero-disruption entropy lever.
- Balanced's FAQ notes kube-scheduler can scatter evicted pods and recommends `MostAllocated` scoring.
- New in this work: a global potential that values moves which delete no node (shifting), plan-level scoring of move sequences, pluggable stages, and the claim that generation can be random.

## Formal model

Consolidation is minimizing a concave potential over feasible placements, and concave minima sit at extreme points: every node empty or full except at most one. That one fact ties the entropy objective to the target fleet shape, where every node is packed or empty.

**State.** Node i has a capacity vector cᵢ (CPU, memory, pods, accelerators), an hourly price πᵢ, labels, taints and a zone. Pod j has a request vector rⱼ, scheduling constraints, PDB membership and an eviction cost. Load Lᵢ is the sum of requests bound to node i; utilization uᵢ = Lᵢ / cᵢ per dimension, summarized by the dominant share.

**Actions**: add-node, remove-node (only once no reschedulable pods remain), unbind-pod (eviction), bind-pod. Karpenter only simulates bind-pod; kube-scheduler performs it. That gap is measured as plan fidelity.

**Moves** are compositions of actions that end in a feasible state.

- **Merge:** empty a set of sources into existing destinations plus at most one new node, then remove the sources. Upstream Delete and Replace are merges. Savings are immediate.
- **Shift:** move some pods from a low-utilization source to a higher-utilization destination; nothing is removed. Savings are deferred, but the potential drops now.
- **Right-size:** replace one node with a cheaper one holding the same pods.

**Potentials** are pluggable; which one best predicts savings is itself a research question.

P1, load-share entropy:

```latex
H(p) = -\sum_{i=1}^{n} p_i \ln p_i, \qquad p_i = \frac{L_i}{\sum_k L_k}
```

P2, price-weighted utilization entropy, zero when a node is empty or full and largest at 50% (the push away from mid-range utilization, made exact):

```latex
\Phi = \sum_{i=1}^{n} w_i \, h(u_i), \qquad h(u) = -u \ln u - (1-u) \ln (1-u), \qquad w_i = \pi_i
```

P3, a cheaper smooth variant: the sum over nodes of wᵢ · uᵢ · (1 − uᵢ). Multi-resource versions apply each form per dimension and combine by max or weighted sum, plus a stranding term for nodes full on one dimension and empty on another.

**Why minimizing them yields packed-or-empty nodes.** For one resource dimension, feasible loads form the polytope below. P1 and P2 are concave in L, so their minimum over F lies at a vertex. A vertex has at least n − 1 tight bounds, so every node is empty or full except at most one.

```latex
F = \{\, L \in \mathbb{R}^n : \sum_i L_i = L_{\mathrm{tot}},\; 0 \le L_i \le c_i \,\}
```

Discrete pods, several dimensions and placement constraints turn this into a landscape with local minima. That motivates annealing, and the size of the gap is itself a result.

**Traps the code must avoid.**

- Never optimize the entropy of the utilization histogram. A fleet with every node at 50% fills one bin and scores zero.
- P1 is blind to right-sizing. Replacing a half-full 4-unit node with a full 2-unit node leaves every pᵢ unchanged, so H does not move, while P2 drops to zero for that node.
- Both potentials ignore empty nodes (0 ln 0 = 0, h(0) = 0). Removing an empty node is a separate step, valued by the savings term.

**Planning energy and acceptance.** Each move gets an energy change; D̂ and Ŝ are disruption and immediate savings normalized by NodePool totals, as Balanced does, so λ and μ are scale-free. D is the sum of max(0, EvictionCost) over evicted pods, matching upstream. Acceptance uses the Metropolis rule (Boltzmann weighting) at temperature T.

```latex
\Delta E(m) = \alpha\, \Delta\Phi(m) + \lambda\, \hat{D}(m) - \mu\, \hat{S}(m), \qquad P(\mathrm{accept}) = \min\left(1,\; e^{-\Delta E(m)/T}\right)
```

## Hypotheses

Seven hypotheses, each with one primary metric and one experiment. H1 is the paper's central claim, and "random is as good" is an equivalence claim: test it against a preregistered margin, never by failing to find a difference.

| ID | Hypothesis | Primary metric | Experiment |
| --- | --- | --- | --- |
| H1 | With stages 3 and 4 fixed to entropy-guided planning, random candidates and moves reach steady-state cost within a preregistered margin of heuristic generation, at equal simulation budget | Steady-state $/hr, equivalence test | Exp 1 |
| H2 | A drop in the potential over a window predicts node removals and cost decrease in the next window | Lagged correlation, per potential | Exp 2 |
| H3 | Merge plus shift beats merge-only on integrated cost at an equal pod-eviction budget under churn | Cost-hours at equal evictions | Exp 3 |
| H4 | Annealing at plan time, on a snapshot, reaches cheaper states than greedy acceptance with no extra executed disruption; annealing online pays disruption for little gain | Final cost and total evictions | Exp 4 |
| H5 | Under default kube-scheduler scoring, a measurable share of evicted pods land off-plan and undo part of the potential drop; MostAllocated and deletion-cost steering shrink it | Plan fidelity, realized vs predicted ΔΦ | Exp 5 |
| H6 | Sweeping λ traces a savings vs disruption frontier that matches or dominates the WhenEmptyOrUnderutilized and Balanced operating points | Frontier dominance | Exp 6 |
| H7 | Random sampling with a fast feasibility filter scales better than prefix binary search: lower decision latency at 1,000+ nodes and no timeouts | Latency per cycle, timeouts hit | Exp 7 |

## Target architecture: four pluggable stages

Add one new disruption `Method`, `PipelineConsolidation`, that composes four stage interfaces and shared services. Upstream methods stay untouched and remain the default.

&#91;embedded content: consolidation pipeline · 4 stages, 5 shared services\]

Stages 1 and 2 only propose; stage 3 decides what runs using the shared potential, disruption and savings models, and stage 4 carries it out safely.

**Where it plugs in**

- The controller already accepts injected methods through `WithMethods(...)` and runs them in order: Repair (when gated), Emptiness, StaticDrift, Drift, MultiNodeConsolidation, SingleNodeConsolidation. The first method that returns commands wins, and its commands start in parallel through the orchestration queue.
- Under a new alpha gate (proposed name `ConsolidationPipeline`), register `PipelineConsolidation` in place of the two consolidation methods. Emptiness and Drift stay as they are.
- Reuse the `Evaluator` seam in `balanced.go` (`ApproveCommand`, `CanPassThreshold`) so Balanced becomes one scorer among several.
- Merges still emit upstream `Command`s, so the queue, validation and termination paths are reused unchanged. Shift moves need a new execution path (Phase 3).

**Interface sketch** (`pkg/controllers/disruption/pipeline`)

```go
// Snapshot is an immutable, cheaply forkable view of cluster state used for planning.
type Snapshot interface {
	Nodes() []NodeView
	Pods(node string) []PodView
	Apply(m Move) (Snapshot, error) // copy-on-write, used by search
	Totals(nodePool string) Totals  // cost and disruption totals, computed as Balanced does
}

type MoveKind string // "merge", "shift", "rightsize"

type Move struct {
	Kind     MoveKind
	Sources  []string          // nodes losing pods
	Bindings map[PodKey]Target // unbind-pod plus planned bind-pod, per pod
	Launch   []LaunchSpec      // add-node
	Remove   []string          // remove-node
}

type Scored struct {
	Move
	DeltaPotential float64
	Disruption     float64 // sum of max(0, EvictionCost) over evicted pods
	SavingsPerHour float64
}

type CandidateSelector interface { // stage 1
	Name() string
	Select(ctx context.Context, s Snapshot, rng *rand.Rand) (sources, destinations []NodeView, err error)
}

type MoveGenerator interface { // stage 2
	Name() string
	Generate(ctx context.Context, s Snapshot, sources, destinations []NodeView, rng *rand.Rand, b SearchBudget) ([]Move, error)
}

type Planner interface { // stage 3
	Name() string
	Plan(ctx context.Context, s Snapshot, moves []Scored, budgets Budgets, rng *rand.Rand) (Plan, error)
}

type Executor interface { // stage 4
	Name() string
	Execute(ctx context.Context, p Plan) (Report, error)
}

// Shared services
type Potential interface {
	Name() string
	Value(s Snapshot) float64
	Delta(s Snapshot, m Move) float64 // must equal Value(s.Apply(m)) - Value(s); property-tested
}

type Feasibility interface {
	Fast(s Snapshot, m Move) error                      // resources, taints, selectors, zones of bound volumes
	Full(ctx context.Context, s Snapshot, m Move) error // Karpenter scheduling simulation
}

type Recorder interface{ Record(e DecisionEvent) } // one JSON line per stage decision
```

**Interface rules**

- The RNG and clock are injected; no stage reads globals.
- `SearchBudget` caps work per cycle (full simulations, wall time), so strategies are compared at equal compute.
- Every implementation passes a shared conformance suite: moves pass `Full`, plans never exceed budgets, executors never violate PDBs or do-not-disrupt, and `Delta` matches `Value` differences.

**Config**, one file per run, selected by flag or ConfigMap:

```yaml
# placeholder values; the Phase 4 pilot calibrates them
pipeline:
  candidates: {name: random, params: {sourceFraction: 0.25}}
  moves:      {name: random-shift, params: {maxPodsPerMove: 4}}
  planner:    {name: anneal-plan, params: {t0: 0.05, cooling: 0.97, lambda: 0.5}}
  executor:   {name: queue-plus-evict}
  potential:  {name: util-entropy, params: {weight: price, resource: dominant}}
  seed: 42
```

**How upstream maps onto the stages** (these become the parity adapters)

| Stage | Upstream code today | Adapter |
| --- | --- | --- |
| 1 Candidates | `GetCandidatesWithTotals` with each method's `ShouldDisrupt`, then `sortCandidates` (savings ratio, descending, on current main) | `upstream` |
| 2 Moves | `SingleNodeConsolidation` per candidate; `MultiNodeConsolidation.firstNConsolidationOption` prefix binary search; both via `computeConsolidation` and `SimulateScheduling` | `upstream-single`, `upstream-multi` |
| 3 Plan | `BuildDisruptionBudgetMapping`, price filters, spot-to-spot rules, `Evaluator.ApproveCommand`, validation after the wait period | `upstream`, `balanced` |
| 4 Execute | Orchestration queue in `queue.go`: taint, launch replacements, wait, then delete and drain through termination | `upstream-queue` |

**Adding a strategy** touches one folder: implement the interface, register it, add a config, run the conformance suite.

```go
type randomShift struct{ maxPods int }

func (g *randomShift) Name() string { return "random-shift" }

func (g *randomShift) Generate(ctx context.Context, s Snapshot, src, dst []NodeView, rng *rand.Rand, b SearchBudget) ([]Move, error) {
	if len(src) == 0 || len(dst) == 0 {
		return nil, nil
	}
	var out []Move
	for b.Next() { // stops at the per-cycle budget
		from, to := src[rng.Intn(len(src))], dst[rng.Intn(len(dst))]
		pods := samplePods(rng, s.Pods(from.Name), 1+rng.Intn(g.maxPods))
		out = append(out, ShiftMove(from.Name, to.Name, pods))
	}
	return out, nil
}

func init() { RegisterMoveGenerator("random-shift", newRandomShift) }
```

## Phase 0: Rebase, environment, and code map

Put the branch on upstream main, get the toolchain working in the cloud VM, and write a code map before changing any behavior.

**Step 1: rebase onto upstream main**

```bash
git remote add upstream https://github.com/kubernetes-sigs/karpenter.git
git fetch upstream main
git rebase upstream/main
```

- Resolve conflicts by taking upstream's version of upstream-owned files and re-applying the fork's own changes on top. Log every resolution.
- If the fetch is refused (the session's GitHub proxy serves only attached repositories), read upstream's head commit from the Go module proxy, which is reachable: run `go list -m -json sigs.k8s.io/karpenter@main` from a temporary directory outside the repo. If `git merge-base --is-ancestor <commit> origin/<default branch>` succeeds, the fork is already synced; rebase onto it instead.
- Confirm the base is new enough: `ConsolidationPolicyBalanced` exists in `pkg/apis/v1/nodepool.go`, and `type Evaluator interface` exists in `pkg/controllers/disruption/balanced.go`. If not, take Gate 0's stop path.

**Step 2: toolchain**

- The VM runs Ubuntu 24.04 with Go, Python and Docker preinstalled, and roughly 4 vCPUs, 16 GB of RAM and 30 GB of disk ([cloud environments](https://code.claude.com/docs/en/cloud-environments)).
- If `go version` is older than the `go` line in `go.mod`, keep `GOTOOLCHAIN=auto` so Go fetches the right toolchain from proxy.golang.org.
- Install envtest binaries exactly as `.github/actions/install-deps/action.yaml` does: `setup-envtest` from `go.tools.mod`, into `/usr/local/kubebuilder/bin`, at the Kubernetes version CI uses. If that download is refused, retry through setup-envtest's legacy GCS index (see `setup-envtest --help`; storage.googleapis.com is on the Trusted list) and record the version used.
- Install analysis dependencies, pinned in `paper/requirements.txt`: pandas, numpy, scipy, matplotlib, pyyaml, ortools.

**Step 3: baseline and code map**

- [ ] On the rebased tree, before any change, run `go build ./...` and `go test ./pkg/controllers/disruption/... -race`. Record runtimes. Record pre-existing failures; do not fix them.
- [ ] Read the repo's `AGENTS.md` (same text as `CLAUDE.md`) and follow it: targeted tests; `make verify` after API changes; gates added as `options.FeatureGate` vars in `KarpenterFeatureGates`; `serrors` for errors.
- [ ] Trace one disruption cycle end to end into `docs/research/code-map.md` (a subagent can do this read-only exploration): the controller loop, `GetCandidatesWithTotals`, `BuildDisruptionBudgetMapping`, single and multi `ComputeCommands`, `computeConsolidation`, `SimulateScheduling`, the `Evaluator`, validation (including churn rejections), the queue, termination and drain, and `state.Cluster` sync.
- [ ] Record the time limits: multi-node consolidation stops at 1 minute and single-node at 3 minutes. Note how often existing tests hit them.
- [ ] List every source of nondeterminism (map iteration, wall clock, UUIDs, `shuffleCandidates`), since parity and seeded runs need each one controlled.
- [ ] Inventory measurement assets to reuse: metrics in `pkg/controllers/disruption/metrics.go` (including `consolidation_score` and `consolidation_moves_total`); `test/suites/performance` (Balanced basic, churn and heterogeneous specs; `ReportConsolidation` in `report.go`); seeded scenario generators in `pkg/test/schedulingproperties`; instance catalogs in `kwok/examples`; and the simulation in `designs/scripts/balanced-consolidation-ranking.py`.
- [ ] Propose the seams in the code map: where `PipelineConsolidation` registers, which functions each adapter wraps, and what `Snapshot` copies from `state.Cluster`.

**Exit (Gate 0):** rebased, toolchain working, baseline tests green or pre-existing failures recorded, code map and seams committed and pushed. Otherwise write `BLOCKED.md` and stop.

## Phase 1: Behavior-preserving decomposition and parity

Land the interfaces, the registry and the upstream adapters, then prove with a differential test that the pipeline makes exactly upstream's decisions on thousands of seeded cluster states. No new strategy starts before this passes.

1. Add `pkg/controllers/disruption/pipeline` with the interfaces, registry, `Snapshot`, `Move`, `Recorder` and config loader. Touch upstream files only to register the method behind the new alpha gate.
2. Build coarse adapters first. Wrap single-node and multi-node `ComputeCommands` as one move generator each, and convert each returned `Command` into a `Move`. Upstream's multi-node path interleaves ordering, generation and validation inside its binary search, so split it finer only after parity holds.
3. Write a `Command` to `Move` converter and its inverse, so merges keep flowing through the existing queue.
4. Build the differential parity test:
   1. Generate seeded cluster states: reuse `pkg/test/schedulingproperties` generators where they fit, and add fragmented states made by random scale-down.
   2. Run upstream methods and the pipeline with upstream adapters on the same state and the same fake clock.
   3. Assert identical outputs: candidates, decision, replacement instance-type options, and budget consumed.
   4. Run 1,000 states per policy (`WhenEmptyOrUnderutilized`, `Balanced`) before each pipeline commit, and 10,000 once, as a background chunk, before the write-up.
5. Add property tests for shared services: `Delta` equals the difference of `Value`; `Snapshot.Apply` conserves pods and never exceeds capacity; `Fast` never accepts a move that `Full` rejects on resources. Record, without failing, how often they disagree on topology.
6. Draft the RFC in `designs/consolidation-pipeline.md` from `designs/rfc-template.md`: motivation (async move generation is an open question in the Balanced RFC), interfaces, gate, rollout, and what stays unchanged.

**Exit (Gate 1):** all upstream tests green, `make verify` clean, 100% parity on sampled states (or Gate 1's documented fallback), RFC draft written, gate off by default. Commit and push.

## Phase 2: Simulation and measurement harness

Build two tiers that share one scenario format and one decision-log schema: a deterministic in-process simulator for large sweeps, and KWOK on kind for end-to-end checks.

**Tier A: in-process simulator** (`hack/consolidation-sim/`)

- Virtual clock and discrete events: pod arrivals and departures, a controller cycle per polling period, node launch latency and drain latency drawn from configurable distributions.
- Reuse real Karpenter code where affordable: `state.Cluster`, the provisioning scheduler, and the pipeline stages. Spike two designs first, an envtest-backed harness (the disruption suite's pattern, highest fidelity) and an in-memory state with a scheduler adapter (fastest). Measure seconds per simulated cycle at 100 and 1,000 nodes, choose, and log the decision.
- **Placement model**, where kube-scheduler puts a pending pod: `least-allocated` with soft default topology spread (the kube-scheduler default), `most-allocated`, and `plan-following` (an oracle that lands pods where the plan said). Fidelity is measured against this.
- **Scale-down model**, which replicas a ReplicaSet deletes: `spread` (the default heuristic), `deletion-cost` (honors the PodDeletionCostManagement ranking), and `random`.
- **Catalog and prices:** KWOK's `aws_instance_types.json`, as shipped, using on-demand offerings for c7i, m7i and r7i to match the Balanced RFC's analysis. No network fetch is needed.
- **Determinism test:** the same seed, config and SHA must produce byte-identical decision logs; run a scenario twice and diff.

**Experiment runner** (shared by both tiers): `go run ./hack/consolidation-sim run --matrix <file> --workers 4 --max-wall 20m` runs pending cells, appends results, and exits. Re-invoke it until it reports the matrix complete. Four workers match the VM's 4 vCPUs.

**Tier B: KWOK on kind** (best effort inside the session)

- Docker is preinstalled; install kind with `go install sigs.k8s.io/kind@<pinned version>`. Then run `make setup-kind`, `make install-kwok` and `make apply-with-kind` from the branch.
- The repo's kind config already mounts a `KubeSchedulerConfiguration`; add a profile with `NodeResourcesFit` scoring set to `MostAllocated` beside the default, and let workloads opt in by `schedulerName`.
- Add a performance suite for pipeline configs next to `test/suites/performance`, reusing `ReportConsolidation` and `monitorConsolidationRounds`. A workload driver replays the same scenario files as Tier A.
- Scrape controller metrics with Prometheus, and collect the decision log from the controller's structured logs.
- Stay at 50 and 200 nodes, within the VM's 16 GB of memory.
- If image pulls fail (KWOK pulls from `registry.k8s.io`, and `ko` needs a base image, both outside the Trusted network level), log the blocked hosts, skip Tier B runs, and still make the suite compile.

**Tier C** (out of scope for the session): write the steps for one small EKS sandbox run, with a hard spend cap, into `docs/research/runbook.md`.

**Metrics**, computed identically in every tier:

| Family | Metrics | Why |
| --- | --- | --- |
| Cost | Node $ integrated over the run; steady-state $/hr | Primary outcome |
| Shape | P1 to P3 over time; utilization histogram per tick (CPU, memory); share of nodes between 20% and 80% | The target bimodal shape, measured |
| Disruption | Pods evicted, raw and EvictionCost-weighted; nodes disrupted; re-disruption within 30 min; PDB violations (must be 0) | The cost side of the savings vs disruption trade-off |
| Regret | Evictions not followed by a node removal within τ; nodes launched then removed within τ | Wasted disruption and churn |
| Scheduling impact | Pending pods; time to schedule | Headroom cost of a bimodal fleet |
| Decision cost | Wall time per cycle; full simulations per cycle; moves generated, feasible, accepted; timeouts | Scalability (H7) |
| Fidelity | Share of evicted pods landing on the planned node; realized vs predicted ΔΦ | Scheduler divergence (H5) |
| Optimality | Gap to an offline packing lower bound, small scenarios only | How far from best |

**Decision log**, one JSON line per stage decision, versioned because it is the paper's raw data: `run_id`, `cycle`, `t`, `stage`, `strategy`, `move_id`, `kind`, `sources`, `destinations`, `pods`, `delta_potential`, `disruption`, `savings_per_hour`, `accepted`, `reason`, `budget_remaining`.

## Phase 3: Strategy implementations

Implement a small, deliberately spread set of strategies per stage, from random to heuristic, so each experiment varies one stage while the others stay fixed.

| Stage | ID | Strategy | What it does |
| --- | --- | --- | --- |
| 1 Candidates | C0 | `upstream` | Upstream filters and sort |
| 1 Candidates | C1 | `uniform-random` | Sources and destinations sampled uniformly from eligible nodes |
| 1 Candidates | C2 | `util-band` | Sources below fleet mean utilization; destinations above mean but not full |
| 1 Candidates | C3 | `potential-weighted` | Sources sampled in proportion to the potential drop from emptying them |
| 2 Moves | G0, G1 | `upstream-single`, `upstream-multi` | Parity adapters |
| 2 Moves | G2 | `random-merge` | Random k sources (k from 1 to K), first-fit their pods into a random order of destinations, optional single replacement node |
| 2 Moves | G3 | `random-shift` | Move 1 to m random pods from a source to a random feasible destination with higher utilization |
| 2 Moves | G4 | `greedy-shift` | Sample pairs, keep the best ΔΦ per pair |
| 2 Moves | G5 | `swap` (stretch) | Exchange pods between two nodes to fix CPU-heavy vs memory-heavy stranding |
| 3 Plan | P0, P0b | `upstream`, `balanced` | Budgets and first valid command; Balanced score at k = 2 |
| 3 Plan | P1 | `entropy-greedy` | Accept moves with ΔE < 0; choose a non-conflicting set by ratio within budgets |
| 3 Plan | P2 | `anneal-plan` | Metropolis search over move sequences on the snapshot with geometric cooling; execute only a net-downhill sequence. Disruption is paid only for what runs |
| 3 Plan | P3 | `anneal-online` | Same acceptance, executed move by move on the cluster. Experiment-only contrast for H4 |
| 3 Plan | P4 | `sequence` | Chains shifts that end in a merge within horizon h and scores the chain as a whole, so a shift, which costs disruption now and saves later, is never judged alone |
| 4 Execute | E0 | `upstream-queue` | Merges through the existing queue: taint, launch, wait, drain, delete |
| 4 Execute | E1 | `shift-evict` | Evict chosen pods through the Eviction API (PDB-respecting) and add a `PreferNoSchedule` taint on the source under a new experimental key, not `karpenter.sh/disrupted` |
| 4 Execute | E2 | `steered` | Raises fidelity via a MostAllocated profile, temporary taints on non-destinations, or a webhook adding preferred node affinity to replacement pods. The last two are experiment-only |
| 4 Execute | E3 | `make-before-break` | Launches and registers destination capacity before evicting |

**Budgets for every planner.** Keep upstream NodePool node budgets, and add pod-eviction budgets per minute, per NodePool and per workload. Shift moves delete no node, so node-count budgets cannot see them.

**Interactions to handle explicitly**

- `consolidateAfter`: pods leaving a shift source reset its timer and can delay its final removal. Mark shift sources as planned removals and record the delay either way.
- Validation: upstream re-validates after a wait period and rejects on churn. Shift plans need the same incremental re-check and an abort path; count aborts.
- Deletion-cost steering: optionally feed the planner's next merge targets into the PodDeletionCostManagement ranking, so ReplicaSet scale-downs empty them at no disruption cost.

## Phase 4: Experiments

Run a pilot to calibrate, freeze a preregistration, then run eight experiments. Exp 1 tests the central hypothesis directly with a 2 by 3 crossed design.

**Workloads**

| ID | Scenario | What it stresses |
| --- | --- | --- |
| W1 | Fragmented start: random placement, no churn | Pure consolidation efficiency and convergence |
| W2 | Diurnal: deployments scale up, then down by 50% to 70% | Classic consolidation after scale-down |
| W3 | Batch churn: short jobs with heavy-tailed durations | Stability, thrash, passive entropy |
| W4 | Mixed shapes: CPU-heavy and memory-heavy pods on c, m and r families | Multi-dimensional stranding |
| W5 | Constrained: PDBs, topology spread, anti-affinity, do-not-disrupt | Realism; gap between fast and full feasibility |
| W6 | Trace replay: a public cluster trace (for example Google 2019 or Alibaba) mapped to pod requests and lifetimes; verify license and document the mapping | External validity |

**Scales and profiles:** the `session` profile runs inside the cloud VM: Tier A at 50, 200 and 1,000 nodes, plus Tier B at 50 and 200 nodes when it works. The `full` profile adds Tier A at 5,000 nodes, 20 or more seeds per cell, and Tier B at 500 nodes; ship it as `experiments/matrix/full.yaml` with a runbook instead of running it. If the pilot shows the session profile will not fit, cut in this order: 1,000-node cells, then W6, then W5. Never cut Exp 1 cells, and never go below 10 seeds.

**Baselines**

- B0: no consolidation.
- B1: upstream `WhenEmpty`.
- B2: upstream `WhenEmptyOrUnderutilized`.
- B3: upstream `Balanced` (k = 2).
- B4: the pipeline with upstream adapters; it must match B2 and B3, which re-checks parity in the wild.
- B5: descheduler-style compaction: evict from nodes below a utilization threshold under MostAllocated scheduling (the Kubernetes descheduler's HighNodeUtilization pattern).
- B6: an offline packing lower bound (CP-SAT or ILP) of the pod set at fixed instants, for scenarios up to 50 nodes.

**Experiments**

| Exp | Tests | Design |
| --- | --- | --- |
| 1 | H1 | Generation {random: C1 with G2 and G3; heuristic: C0 with G0 and G1} × planner {P0 savings-only, P1 entropy-greedy, P2 anneal-plan}; W1, W2, W4; 200 and 1,000 nodes; equal full-simulation budget per cycle |
| 2 | H2 | Lagged correlation of ΔΦ over a window with node removals and cost in the next window, for P1, P2 and P3; W2, W3, W6 |
| 3 | H3 | Merge-only vs merge plus shift at an equal pod-eviction budget; W2, W3 |
| 4 | H4 | P1 greedy vs P2 anneal-plan vs P3 anneal-online; sweep starting temperature and cooling; W1, W4 |
| 5 | H5 | Placement model {least-allocated, most-allocated, plan-following} × scale-down model {spread, deletion-cost}; Tier A, then Tier B |
| 6 | H6 | Sweep λ on a log grid; plot cumulative savings vs cumulative disruption; mark B2, B3 and B5 |
| 7 | H7 | Decision latency and simulations per cycle vs node count, G1 vs G2 and G3 with fast feasibility |
| 8 | Optimality | Gap to B6 on small scenarios for every main configuration |

**Statistics**

- Pilot first: W1 and W2 at 200 nodes, 5 seeds, to calibrate temperature, cooling, λ, budgets and τ, to estimate variance, and to time one cell so the session profile can be sized.
- Then commit `experiments/PREREGISTRATION.md`: hypotheses, the primary metric for each, the H1 equivalence margin, seeds, exclusion rules, and the run order. Calibrate on pilot seeds, evaluate on fresh ones.
- Seeds: 10 per cell in the session profile, 20 or more in the full profile, 5 per cell in Tier B. Every configuration in a comparison shares workload seeds, so differences are paired.
- Run order: Exp 1, 6, 5, 3, 4, 7, 8, so the central claim lands first. Compute Exp 2 from logs the other runs already produced, adding W6 runs only if time remains.
- Report means and medians with 95% bootstrap intervals; paired Wilcoxon signed-rank tests for differences; two one-sided tests (TOST) for H1; Holm correction within each experiment.

**Figures the paper needs** (one script each under `paper/figures/`)

1. Utilization histograms over time, upstream vs pipeline: the target bimodal shape, measured.
2. Potential, node count and cost over time on shared time axes.
3. Savings vs disruption frontier with upstream operating points.
4. Decision latency vs cluster size.
5. Plan fidelity by scheduler profile and scale-down model.
6. Lead-lag correlation of ΔΦ and savings.
7. Optimality gap by scale.
8. Ablation table: without the potential, without eviction budgets, without annealing, without randomness.

## Phases 5 and 6: Validation and paper artifacts

Confirm that the simulator's rankings survive on KWOK, then package the work so every number in the paper regenerates from raw logs with one command.

**Phase 5: validation**

- If Tier B works in the session, rerun the top three configurations plus B2, B3 and B4 on KWOK at 50 and 200 nodes, 5 seeds each. Otherwise skip, and say so in `RESULTS.md`.
- Check rank agreement with Tier A per metric (Kendall's tau over configuration order). Report any reversal as a finding, not a footnote.
- Measure what simulation cannot: controller CPU and memory, API server request rate, eviction rate, and real validation aborts.
- Leave 500-node KWOK runs and the optional EKS pass to the runbook.

**Phase 6: artifacts**

- `experiments/runs/<run-id>/` holds `manifest.json`, `decisions.jsonl` and `timeseries.csv`. Raw data is never edited by hand.
- `make figures` regenerates every figure and table from raw runs. `paper/REPRODUCE.md` lists commit SHAs, configs, seeds and hardware.
- `paper/related-work.md` collects candidate references, each marked "verify": the Balanced and pod-deletion-cost RFCs in this repo, the Kubernetes descheduler's HighNodeUtilization, kube-scheduler MostAllocated scoring, Tetris multi-resource packing (SIGCOMM 2014), dominant resource fairness (NSDI 2011), Borg (EuroSys 2015), Protean (OSDI 2020), and simulated annealing (Kirkpatrick et al., 1983).
- `experiments/RESULTS.md` for the human: each hypothesis with its verdict, effect size and interval, and the figure that shows it.
- `experiments/matrix/full.yaml` and `docs/research/runbook.md`: everything needed to run the full profile and 500-node KWOK runs on a larger machine.
- Last step: update `PROGRESS.md`, push the branch, and end the session with a short summary: what ran, what is provisional, any blocked hosts, and the next commands to run.

## Guardrails, threats to validity, fixed decisions

Keep experiments safe and claims honest. Every choice this plan leaves open has a fixed default below, so the run never waits on a person.

**Guardrails**

- The gate is off by default and no upstream default changes. Per the repo's `AGENTS.md`, the agent never opens upstream issues or PRs, or posts comments.
- Sandboxes only, with spend caps on any cloud account.
- Exclusion rules are written before runs: PDB or do-not-disrupt violations, controller crashes, and nondeterministic logs make a run invalid. Invalid runs are counted and reported, never silently dropped.
- No tuning on evaluation seeds.

**Threats to validity**

- Tier A models kube-scheduler and ReplicaSet behavior instead of running them. The Tier B rank-agreement check bounds this.
- Synthetic workloads may flatter random generation. W5 and W6 test realism, and results are reported separately for early (fragmented) and late (near-converged) phases of each run.
- On-demand prices only at first. Spot, reserved and ODCR pricing change savings, as the Balanced RFC's ODCR section shows.
- Restart cost is proxied by EvictionCost; real warm-up costs are not modeled.
- A bimodal fleet leaves little headroom. Pending-pod latency is measured, and the CapacityBuffer gate is the comparison point if headroom matters.

**Decisions fixed for this run**

| Decision | Default |
| --- | --- |
| Repository and base | Fork of `kubernetes-sigs/karpenter` core, rebased onto upstream main |
| Feature gate | `ConsolidationPipeline`, alpha, off by default |
| Branch | The session's working branch; never the default branch |
| Price catalog | `kwok/examples/aws_instance_types.json` as shipped; on-demand c7i, m7i and r7i |
| H1 equivalence margin | 3% of steady-state $/hr |
| Regret window τ | 15 minutes |
| Re-disruption window | 30 minutes |
| Middle band for shape metrics | 20% to 80% utilization |
| Session profile | Tier A at 50, 200 and 1,000 nodes, 10 seeds per cell; Tier B at 50 and 200 nodes when it works |
| Cut order when time is short | 1,000-node cells, then W6, then W5; never Exp 1 cells, never below 10 seeds |
