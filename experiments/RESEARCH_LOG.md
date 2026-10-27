# Research log

Every decision taken without asking, every fallback, and every blocked host, in order. Times are UTC.

## 2026-10-27

- **Rebase (Phase 0, step 1).** `upstream` (kubernetes-sigs/karpenter) was reachable over git through the session proxy.
  Upstream main head: `32fd95b` (chore: remove dead code and deprecate long-unused exports (#3482)), one commit after the
  `cac0d0b` named in the plan. The fork branch `claude/new-session-03ukbe` had no commits of its own (HEAD `d1aacae` was
  an ancestor of upstream main, 110 commits behind), so the rebase was a fast-forward with no conflicts to resolve.
- **Base check.** `ConsolidationPolicyBalanced` exists at `pkg/apis/v1/nodepool.go:165`; `type Evaluator interface` exists
  at `pkg/controllers/disruption/balanced.go:191`. The base is new enough.
- **Toolchain.** `go version` 1.26.6 matches `go.mod` (`go 1.26.6`). envtest installed exactly as
  `.github/actions/install-deps/action.yaml` does (`go tool -modfile=go.tools.mod setup-envtest use -p path 1.36.x`),
  resolving to Kubernetes **1.36.2**, moved into `/usr/local/kubebuilder/bin`. The primary index worked; no GCS fallback.
- **Python.** The system Python is externally managed, so analysis dependencies live in a venv at `/opt/research-venv`
  (pinned versions in `paper/requirements.txt`).
- **Environment variables.** `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` were not set in the session. Long work
  runs as background commands and resumable chunks instead (operating rule 12).
- **Docker.** The daemon was not running; `dockerd` started fine (overlayfs, cgroup v1).
- **Blocked hosts (Tier B).** The network policy denies `registry.k8s.io` and `cgr.dev` (proxy answers 403 to CONNECT).
  Docker Hub (`registry-1.docker.io`) is reachable. KWOK's controller image comes from `registry.k8s.io` and `ko`'s
  default base image from `cgr.dev`, so per the plan's fallback **Tier B runs are skipped**; the KWOK suite is still
  written and must compile. To enable Tier B, add both hosts to the environment's allowed domains (or use Full network
  access) and follow `docs/research/runbook.md`.
- **Baseline (Phase 0, step 3).** On the rebased tree, before any change: `go build ./...` exit 0 in 93 s (cold module
  and build cache); `go test ./pkg/controllers/disruption/... -race` exit 0, package time 399.8 s, wall 524 s including
  compilation. No pre-existing failures.
- **Simulator design spike (Phase 2, Tier A).** `hack/consolidation-sim/spike` builds the same seeded fragmented
  cluster (random c7i/m7i/r7i sizes up to 16 vCPU, each node filled to a random 20-80% of CPU) on two backends and
  times one upstream consolidation cycle. Wall times on the session VM (4 vCPU):

  | backend | nodes | pods | populate | GetCandidatesWithTotals | multi-node ComputeCommands | single-node (stops at first) | one SimulateScheduling |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | in-memory (fake client) | 100 | 415 | 0.23 s | 4 ms | 92 ms | 6 ms | 5.9 ms |
  | in-memory (fake client) | 1,000 | 3,616 | 2.1 s | 103 ms | 703 ms | 122 ms | 67 ms |
  | envtest | 100 | 415 | 12.4 s | 225 ms | 167 ms | 20 ms | 14 ms |
  | envtest | 1,000 | 3,616 | 105.5 s | 2.2 s | 753 ms | 135 ms | 80 ms |

  **Decision: the in-memory design.** It hosts the same real Karpenter code (state.Cluster, provisioner, disruption
  methods, orchestration queue) on a controller-runtime fake client and a fake clock, and is 20-50x faster on writes,
  which a churn simulation issues constantly. Two fixes made it fast: a plain object tracker (the default field-managed
  tracker rebuilt a REST mapper per write) and a typed pods-by-node index for `spec.nodeName` lists (the fake client
  deep-copies every pod per indexed list). Seconds per simulated cycle are about 0.1 s at 100 nodes and about 1 s at
  1,000 when consolidation finds a move early; a full single-node scan at 1,000 nodes costs about 864 x 67 ms = 58 s,
  which is exactly the upstream cost H7 measures.
- **envtest fidelity trap.** On envtest the same state made different decisions (multi-node: none; single-node:
  replace) because kube-apiserver's TaintNodesByCondition admission adds `node.kubernetes.io/not-ready:NoSchedule` to
  every created Node and no node lifecycle controller removes it, so existing nodes refuse rescheduled pods. Anything
  envtest-based here (the parity harness) must strip that taint, as the disruption suite's `ExpectMakeNodesReady` does.
- **Upstream nondeterminism, observed.** Repeated spike runs with one seed chose multi-node deletes of 34, 35, 23, 21
  and 20 nodes. `cluster.DeepCopyNodes` iterates a map, so candidate order is random, and `sortCandidates` (savings
  ratio, `sort.Slice`) leaves ties, common because nodes of one type with equal pod counts have equal ratios, in that
  random order. The simulator and the parity harness therefore fix candidate order themselves (see the code map).
- **Code map.** A read-only workflow (7 readers, 1 synthesizer, 2 adversarial verifiers) wrote
  `docs/research/code-map.md`; the verifiers checked 1,030 `path:line` citations and corrected about 20. New upstream
  findings it surfaced: only one disruption spec hits a consolidation timeout (single-node, `-5s`), no test checks
  `ConsolidationTimeoutsTotal`, and `newStateFromNodeClaim` drops `podDisruptionCosts`, so Balanced's disruption
  totals for non-candidate nodes depend on the order informer events arrive.
- **Parity design (Phase 1).** Upstream itself is nondeterministic when a state has more than one NodePool:
  `SingleNodeConsolidation.shuffleCandidates` interleaves pools in `lo.Keys(map)` order. Go starts iterating a small
  map at a random one of its 8 slots, so with two pools the second pool comes first only 1 time in 8, and 8 repeated
  upstream runs missed it about a third of the time. Decision: for multi-NodePool states the reference is the set of
  decisions upstream makes over repeated runs, grown adaptively (up to 256 extra upstream runs before a pipeline
  decision counts as a mismatch); single-NodePool states must match exactly, with upstream run twice as a control.
  The simulator's workloads use one NodePool, which removes this source there.
- **Parity, first samples.** 200 states per (policy, catalog) cell: coarse adapters, fine adapters and the
  `balanced` planner all matched upstream on 200/200 in every cell, over a decision mix of multi-node deletes and
  replaces, single-node deletes and replaces, and no-ops (0 to 16 candidates per decision).
- **Mutation check of the parity test.** Three bugs injected one at a time into the fine adapters: dropping the
  multi-node budget decrement was caught (44/60 match); making the upstream planner accept every move was caught on
  Balanced states (85/100) and correctly invisible on WhenEmptyOrUnderutilized states (100/100); skipping the
  `CanPass` pre-filter was not caught, and cannot be, because `CanPass` only skips candidates whose best case (a
  delete) already fails the threshold, so it changes compute, never decisions (an equivalent mutant).
- **Push blocked.** `git push` to `origin` (Ignoramuss/karpenter) returns 403: the Claude GitHub App has no access to
  that repository for the user's account. Commits are kept on the local branch `claude/new-session-03ukbe` and the
  push is retried at every milestone. To fix: reconnect GitHub at https://claude.ai/connect-github and install the
  Claude GitHub App on the repository.
- **Gate 1, parity at 1,000 states.** `PARITY_STATES=1000 PARITY_SEED=1` over both policies and both catalogs: every
  cell matched 1,000/1,000 for the coarse adapters, the fine (stage-split) adapters and, on Balanced states, the
  `balanced` planner (`experiments/parity/parity-1000-seed1.summary.txt`, raw records gzipped alongside). Upstream
  disagreed with itself on 10-22 multi-NodePool states per cell, all resolved within the observed decision set.
  Wall time 21 minutes. The 10,000-state run is scheduled as a background chunk before the write-up.
- **Shared-service properties.** `Delta == Value(Apply) - Value` holds for every potential and parameter variant on
  40 synthetic states; `Apply` conserves pods and never exceeds capacity; on 40 live states Fast and Full agreed on
  all 800 reference moves checked (0 resource disagreements, 0 topology disagreements: the generator's states carry
  few topology constraints, so W5 runs report this count again); 39 command-to-move-to-command round trips kept the
  decision, candidates and replacement options.
- **Simulator determinism, first check.** Three sources of nondeterminism found and fixed on the way to byte-identical
  outputs: Karpenter writes NodeClaims and taints from parallel goroutines (`workqueue.ParallelizeUntil`), so the
  simulator buffers write hooks and schedules them in name order after each event; the fake client's `GenerateName`
  counter handed out names in goroutine order, so NodeClaims are named from a hash of their canonicalized spec
  (identical claims are interchangeable); and requirement lists come from map iteration, so the hash sorts them first.
  After the fixes, four runs of W1/B2 (30 nodes, seed 7) produced identical decision logs, timeseries and summaries.
- **Gate 2 passes.** `TestDeterminism` (hack/consolidation-sim/sim) runs nine cases twice in one process (W1-W5,
  B2, B3, B5 with most-allocated placement, deletion-cost scale-down, and a pipeline arm with the fine upstream
  adapters) and requires byte-identical decision logs and timeseries: all nine pass. Two more upstream sources had
  to be handled on the way:
  - Hard zone topology spread is not reproducible in Karpenter's scheduler: when zones tie for the minimum count,
    `TopologyGroup` pins a new NodeClaim to the first tied zone it meets while ranging over a map
    (`pkg/controllers/provisioning/scheduling/topologygroup.go`, the `for domain := range t.domains` loop).
    Upstream code stays untouched, and a nondeterministic run would be invalid, so **W5 uses a hard hostname spread
    (maxSkew 2) in place of a zone spread**; it still exercises topology constraints Fast feasibility does not
    check. Zone spread remains available as a scenario parameter, documented as nondeterministic.
  - `EmptinessValidator` returns a validated command's candidates in `GetCandidates` (map) order. The order has no
    effect beyond logs; the simulator sorts sources when it logs upstream commands.
- **Scenario scale.** `scenario.nodes = n` sizes every workload as the demand of n mid-size nodes (the catalog's
  3.5-16 vCPU types) at 70% fill. W1 and W4 start with exactly n nodes; W2, W3 and W5 are provisioned by Karpenter,
  which packs whole batches into large (up to 32 vCPU) nodes, so their node counts are lower: at n = 200, W2 peaked at
  44 nodes and W3 at 124. Every summary reports `peak_nodes` and `final_nodes`, and figures that plot against cluster
  size (Exp 7) use the measured node count, not n.
- **Cell timing, B2, seed 1, 3 h horizon, one core.** W1 at 200: 236 s wall (consolidation converges by minute 80,
  156 nodes removed); W2 at 200: 146 s; W3 at 200: 862 s (batch churn keeps consolidation busy all run). A CPU
  profile of W1 at 200 puts half the time in single-node consolidation's per-candidate `SimulateScheduling`, which
  deep-copies every StateNode (`Cluster.DeepCopyNodes`, 28%) and rebuilds the scheduler (22%) for each candidate, so a
  pass costs O(n^2) at convergence. Pipeline arms cap full simulations per cycle (`search.maxFullSimulations`), so
  they cost far less than B2 and B3 at 1,000 nodes.
- **Annealing temperature unit.** Energies are normalized by NodePool totals, so a move's |dE| shrinks as 1/n. The
  annealing planners gained `temperatureUnit: per-node` (temperature divided by the root node count); experiments use
  it so one t0 means the same acceptance odds at every scale.
- **Strategy details fixed by the Phase 3 implementations.** Generators cap a `Generate` call at 1,000 attempts when
  `search.maxAttempts` is 0, since the simulator's clock does not advance during generation and a deadline alone
  cannot stop it. `random-shift` with `full: true` emits Fast-only moves once the Full budget is spent. Energy
  planners track PDB allowances across all moves of a plan and decline any move that touches a do-not-disrupt pod or
  node; the upstream planners (P0, P0b) keep upstream's behavior. No session workload except W5 has PDBs, so Exp 1 is
  unaffected by the difference.
- **Push via the GitHub API is refused too.** The GitHub MCP tools can read the fork, but creating the branch
  returns `403 Resource not accessible by integration`, so commits stay on the local branch.
- **Executors and three fixes found by the first end-to-end runs.** E1-E3 (`shift-evict`, `steered`,
  `make-before-break`) landed; smoke runs of complete arms (W2, 60 nodes) then showed:
  - In pooled mode every generator drew on one attempt counter, so `random-merge` spent all attempts and
    `random-shift` never ran. Each generator now gets its own attempts (`SearchBudget.Fork`); full simulations stay
    one shared pool per cycle, which is what Exp 1 holds equal.
  - Merges `random-merge` placed by first-fit passed Fast but 16 of 35 failed Karpenter's simulation at execution
    ("simulation found no consolidation"): the simulation also reschedules pods of in-flight commands, which the
    snapshot does not reserve room for. `random-merge` gained `confirm: true` (every emitted merge confirmed by Full,
    one simulation each, within the cycle's cap), and experiments use it, so random and heuristic arms both hand the
    planner only confirmed merges and spend the same full-simulation budget. With it, execution failures fell to
    validation churn (3 of 18).
  - Full simulations executors run (confirming merges no generator confirmed) bypassed the search budget; they are
    now counted, so `full_simulations` in every summary covers search and execution.
  - Eviction budgets charged every planned binding of an executed shift, even pods a PDB refusal stopped; reports
    now carry the bindings actually evicted. Executors also lift expired shift-source taints on idle cycles.
- **Determinism, strategy arms.** `TestDeterminism` gained four strategy arms (random generation with entropy-greedy
  and E1; anneal-plan with swap; util-band with greedy-shift, the sequence planner and E2; potential-weighted with
  anneal-online and E3 on W5); all thirteen cases are byte-identical across runs.
