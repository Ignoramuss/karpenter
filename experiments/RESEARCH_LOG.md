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
- **W6 trace.** The Google 2011 cluster trace bucket is reachable; W6 replays a 3-hour window (6 h to 9 h after the
  trace start), sampled to 20% of tasks by a seeded hash, with CPU and memory scaled to 32 vCPU / 128 GiB machines
  (`hack/consolidation-sim/traces/README.md` documents the mapping, the input shards' SHA-256 and the CC BY 4.0
  attribution). W6 defaults to the bundled CSV and joined the determinism test (14 cases).
- **RFC review of code against plan.** Drafting `designs/consolidation-pipeline.md` surfaced inconsistencies. Fixed:
  the two experimental annotations (`consolidation.experimental.karpenter.sh/shift-source-since`,
  `simulation.karpenter.sh/volume-zone`) are now `wellknown.Annotation`s in `KarpenterAnnotations`, as CLAUDE.md
  requires; the `pipeline:`-wrapped config form is decoded strictly; executors lift expired shift-source taints on
  every cycle that executes nothing, including cycles with no moves; a bad pipeline config panics with a `serrors`
  error. Documented as open questions in the RFC instead: energy planners accept strictly downhill moves (dE < 0)
  where Balanced approves at the threshold; fine mode does not emit `consolidation_score` metrics or
  `ConsolidationApproved` events; shift sources are not marked as planned removals, so a drained shift source is
  removed by Emptiness under the Empty reason and budgets; `budget_remaining` is unset in decision events; the
  operator has no steering webhook, so `steered` (affinity mode) and `make-before-break` do not steer outside the
  simulator.
- **Pilot restart after a stall in capped heuristic generation.** The first 35 pilot cells showed
  heuristic/savings-only stuck at 125 nodes on W1 (B2: 105). Under the 16-simulation cap, `upstream-single` (fine)
  scanned candidates in upstream's deterministic order and stopped at the cap, so every re-evaluation re-tried the
  same leading candidates. Fixed (`2d8fce7`): a capped scan resumes after the last candidate it evaluated. A check at
  W1 100 nodes, seed 1: $24.04/hr before, $22.61/hr after, B2 $23.30/hr. The pilot's pipeline cells were deleted and
  rerun on `2d8fce7`; its 20 baseline cells (B2, B3) ran on `c3e9e35`, and no change between the two commits touches
  code those arms execute (gzip output, summary counters, pipeline-only fixes).
- **Consolidated-state gating, decided.** A pipeline cycle that executes nothing marks the cluster consolidated, for
  every generator (upstream's fine-mode scan through upstream's own flag, random generators through the pipeline's),
  so both arms of a comparison re-evaluate on the same triggers: a cluster change or upstream's forced
  re-evaluation every 5 minutes. Re-running every 10-second pass after a fruitless cycle, as upstream does after a
  timeout, would cost a full budget of simulations per pass forever in static workloads (an estimated hour of wall
  time per 1,000-node run) and would not change what Exp 1 compares.
- **Push works again.** At 18:40 UTC `git push -u origin claude/new-session-03ukbe` succeeded (GitHub access was
  evidently restored); the branch on the fork carries every commit so far. Pushes resume at every milestone.
- **B2 at 1,000 nodes is the expensive cell.** W1, seed 1, sharing the VM with three pilot workers: 11.4 s of wall
  time per disruption pass on average (p95 31 s, max 41 s); the run hit its 100-minute wall limit at simulated minute
  160 (`timed_out`), still removing about one node per pass by single-node consolidation (1,000 to 523 nodes). A
  1,000-node B2 or B3 cell therefore costs over two hours of one core. The plan's cut order drops 1,000-node cells
  outside Exp 1 first (`exp1-baselines-n1000`, `exp7-b2-n1000`); whether they run is decided from the session's
  remaining time once Exp 1 is done.
- **Calibration rules, fixed before the `pilot-calibrate` cells were inspected** (the Exp 1 pilot cells were already
  in view; the calibration cells were not yet looked at):
  - lambda: among {0.1, 0.5, 2} for random/entropy-greedy, the value minimizing the mean, over W1 and W2 and seeds
    1-5, of steady-state cost divided by the same seed's B2 steady-state cost. If the best value improves on
    lambda = 0.5 (the default) by less than 1%, keep 0.5. The chosen lambda applies to every energy planner in every
    arm, heuristic and random alike.
  - t0: the same rule among {0.1, 0.3, 1} for random/anneal-plan, default 0.3.
  - Pod-eviction budgets (Exp 3, ablations): budget-lo = the median, over W2 random/entropy-greedy pilot runs, of the
    mean evictions per minute across minutes in which any pod was evicted, rounded to the nearest integer (at least
    1); budget-hi = 2 x budget-lo. So budget-lo binds during active consolidation and budget-hi binds less.
  - Not calibrated: maxFullSimulations (16), cooling (0.98), maxAttempts (500), sourceFraction (0.25), tau and the
    re-disruption window (plan defaults).
- **Preregistration review before Gate 3.** Three independent reviewers (matrix, analysis code, plan and
  statistics) checked the draft preregistration against `session.yaml`, the analysis registry and code, and the
  plan. Changes made in response, before any evaluation run:
  - *Provenance:* manifests now record the binary's embedded VCS revision (`binary_revision`, `binary_modified`);
    one binary built at the Gate 3 commit runs every session cell, and runs from any other binary are invalid. The
    pilot manifests' `git_sha` is HEAD at cell start, which moved during the pilot.
  - *Decision cost:* summaries gained per-pass process CPU time (`cpu_pass_*`), and both CPU and wall time now
    exclude validation waits, during which a pass advances the simulated world. H7's primary metric is CPU time per
    pass, which co-running cells perturb less than wall time.
  - *Steady state* is defined by time (samples with t >= 0.8 x horizon); the duplicated final timeseries row is gone.
  - *Holm families* sized so exact Wilcoxon tests at 10 pairs can reject (smallest p 2/1024): H4 cut to 8 tests,
    the ablation family to 8 (steady-state cost only), the Exp 4 sweep made descriptive, H7's B2 contrast moved to
    its own secondary family.
  - *No result-dependent choices:* H6's matched lambda per baseline is fixed in advance (B2: 0.05; B3: 0.5, from
    Balanced being dE <= 0 at lambda/mu = 1/2; B5: 0.1) instead of picked from evaluation data. H4's "no extra
    disruption" and "little gain" clauses use bootstrap bounds (+10% evictions, -1% cost) instead of
    non-significance. H2 uses the seed as its unit, so correlated runs on one workload seed are not counted as
    independent. H3, H5 and H6 gained explicit verdict rules; H1's rules were made unambiguous (Holm-adjusted
    equivalence, unadjusted veto, fixed m = 12).
  - *Design:* B5 now pairs with a B0 run under its own (most-allocated) placement; Exp 8 gained heuristic/anneal-plan;
    a B4 group (fine upstream adapters, uncapped) re-checks parity in the wild against B2 and B3; the steering mode
    is explicit; `maxWall: 0s` is explicit (no per-cell limit, so no arm is excluded by its run time); groups appear
    in `session.yaml` in run order, and the cut rule is time-based and written down. 1,740 runs in 20 groups.
  - *Exclusion and completeness:* violations are counted from the PDB and do-not-disrupt fields; retried cells are
    counted; a configuration with fewer than 10 finished runs is cut whole; undefined metric values are counted
    apart from invalid runs; identical configurations across groups serve as a cross-group determinism check.
  The analysis code is being aligned with these rules (one writer, then an independent verifier) before the Gate 3
  commit.
- **Gate 3.** The preregistration was finalized in `c78b5f95063c1995b10fa6d66571a16d0a32b243`, before any
  evaluation-seed run. The session binary was built from a clean clone checked out at that commit
  (`vcs.revision=c78b5f9...`, `vcs.modified=false`; a `git worktree` checkout does not get Go's VCS stamp, so a
  clone was used) and its revision written to `experiments/session-binary.txt`. `TestDeterminism` passed at that
  commit, all 14 cases, before the first session cell. The analysis code was still being aligned with the final
  rules (`paper/analysis`, one writer and an independent verifier); those commits follow and change code only toward
  the preregistered text.
- **10,000-state parity, first chunk.** WhenEmptyOrUnderutilized on the KWOK catalog, coarse and fine adapters:
  10,000/10,000 each (upstream decided on 9,141 states and disagreed with itself on 228 multi-NodePool states, all
  resolved within the observed decision set; `experiments/parity/chunks/`). The run was stopped when the session
  started, so it would not perturb the decision-cost measurements; the remaining policy and catalog cells run as
  separate chunks after the session.
- **Analysis alignment verified.** An independent verifier built synthetic run trees with hand-planted answers for
  every experiment (and an independent H2 implementation) and found ten discrepancies between the aligned analysis
  code and the preregistered rules; all are fixed, each with a regression test (code toward the text only):
  1. H6: a cell significantly worse on one metric was "inconclusive" when the other test was unreachable; an
     unreachable test cannot be significantly better, so the cell is "dominated".
  2. H7: the timeout clause covered only W1; it now covers every valid random/entropy-greedy run at 1,000 nodes
     (W1, W2, W4), as the text says.
  3. H7: unreachable tests gave "not supported"; they now give "inconclusive", as in H2-H6.
  4. Configurations with no finished run were missing from `cut_configurations`; `run_checks` now expands
     `session.yaml` (174 configurations) and lists them with n_finished = 0.
  5. The Wilcoxon floor counted zero differences, which the exact test discards; it now uses the non-zero ones, so
     a test that cannot reject is marked unreachable (inconclusive).
  6. Validity totals double-counted runs used by several experiments; totals are now over distinct runs.
  7. Relative `--runs` paths skipped the binary check; run paths are resolved.
  8. The cut table attributed configurations to every experiment using the group; now per configuration.
  9. The nondeterminism exclusion is implemented: `experiments/session-determinism.txt` records TestDeterminism at
     the session commit (PASS, `c78b5f9`, 14/14); a FAIL or another commit would invalidate every session run.
  10. H4 wording: "supported iff ...; partially otherwise" is read as partially when at least one clause cell holds
      and not supported when none does (inconclusive where a test is unreachable), matching H3 and H5's "partially
      if some". The condition for "supported" is unchanged.
  `make figures` stays byte-reproducible (two runs on the pilot: 40 identical files); all analysis tests pass.
- **Container restart, Oct 27 about 23:52 UTC.** The session VM restarted during `exp1-n1000`, killing the driver
  and four in-flight cells (no `summary.json`; the runner wiped and relaunched them). 248 finished cells were intact.
  The restarted VM has a faster CPU: Intel Xeon @ 2.80 GHz instead of @ 2.10 GHz (4 vCPUs, 15 GiB, both), and both
  report hostname `vm`. Simulated outcomes are unaffected (they depend only on binary, configuration and seed); wall
  and CPU time per pass are not. Cells that finished by 23:43 UTC (all of `exp1-n200` and `exp1-baselines-n200`,
  and eight `exp1-n1000` heuristic/savings-only W1 cells) ran on the 2.10 GHz machine; every later cell, including
  every cell H7's primary test uses (heuristic/entropy-greedy and random/entropy-greedy at 1,000 nodes), on the
  2.80 GHz one. Decision-cost results that mix the two (latency against cluster size at 200 nodes vs 50 and 1,000)
  are reported with the machine noted, by each run's `started_at` relative to the restart. The cut rule's clock
  keeps its start, the first session cell at 20:52 UTC. Driver restarted at 23:53 UTC.
- **Container restart #2, Oct 28 about 03:30 UTC.** The VM was replaced again during `exp1-n1000`, this time with a
  2.10 GHz CPU (Intel Xeon @ 2.10 GHz, 4 vCPUs, 15 GiB), the same model as before restart #1. The driver and four
  in-flight cells (heuristic/entropy-greedy W4, seeds 108-110, and the first heuristic/anneal-plan W1 cell) died
  without a `summary.json` and were wiped and relaunched by the runner; 302 finished cells were intact. Driver
  restarted at 03:30 UTC. Machine epochs, by each run's `started_at`: **A** before 2026-10-27T23:52Z (2.10 GHz),
  **B** from then until 2026-10-28T03:30Z (2.80 GHz), **C** after (2.10 GHz). The cut rule's clock is unchanged.
- **H7 hardware confound and a supplementary timing pass (declared before any random/entropy-greedy W1 1,000-node
  result existed).** Exp 1 runs arm-major, so all ten heuristic/entropy-greedy W1 1,000-node cells, H7's reference
  arm, ran in epoch B on the 2.80 GHz CPU, and every random/entropy-greedy cell will run in epoch C on the 2.10 GHz
  one. A per-pass CPU-time comparison across the two machines is biased in random sampling's disfavor by roughly the
  clock ratio (about 25%), which can mask a real advantage or invent a disadvantage. Decision:
  1. The preregistered H7 analysis is run and reported exactly as specified, with the confound stated next to it.
  2. A supplementary paired timing pass (`experiments/matrix/h7-timing.yaml`) reruns both arms of H7's W1 1,000-node
     cells after the session matrix, with the Gate 3 binary, one group per seed and two seeds (four cells) at a
     time, so each heuristic/random pair shares one machine and one load. Its configurations are byte-identical to
     the session cells (same run-ID hashes, e.g. `0d7a1044` for heuristic/entropy-greedy W1 seed 101), so their
     simulated outputs must equal the session cells' exactly; any difference is a determinism failure and voids
     the pass. It is analysed with H7's own test (paired Wilcoxon on CPU time per pass, mean and p95, Holm m = 2)
     and reported as H7-S, supplementary; it does not replace H7's verdict, but where the two disagree, RESULTS
     says that H7's primary verdict is explained by hardware.
  3. Other decision-cost results that compare cells across epochs (Exp 7's 50/200/1,000-node scaling, Exp 8's
     decision cost) are reported with each run's epoch.
  The pass runs only if session time allows after the 30-hour cut; otherwise it is listed under next steps.
- **Driver order bug, found and fixed before it mattered (Oct 28, 03:40 UTC).** The session driver's group list
  (`experiments/session-driver/run_session_v1.sh`) omitted `b4` and ran `exp1-baselines-n1000` before
  `exp7-b2-n1000`, against the preregistered order. It was found during `exp1-n1000`, the third group, so no group
  had run out of order. Fix: v1 stops after `exp1-n1000`, and `run_session_v2.sh` continues in the preregistered
  order (it re-invokes the finished groups first, which the runner reports as `0 pending`).
- **Cut rule, mechanized; fallback for W5 and W6 (declared before any W5 or W6 cell ran).** `cut_projection.py`
  applies the preregistered rule before each cut-candidate group: pending cells x the median wall time of finished
  session cells of the same scenario and scale, divided by 4 workers, must end by 2026-10-29T02:52Z (30 h after the
  first cell), or that group and every later candidate are cut whole. No session group other than `realism-w5` and
  `realism-w6` runs W5 or W6, so for them the rule has no same-scenario median; the fallback is the median over
  finished session cells at the same scale (200 nodes). This is a time-only rule and uses no results. The rule as
  written takes the median over all arms, so for the B2/B3 groups at 1,000 nodes it uses mostly pipeline cells,
  which are much faster than B2 and B3 there; it is applied as written, and a candidate group that starts runs to
  completion. Dry run at 03:36 UTC (all four would start if considered now): W5 and W6 60 cells x 2.1 min;
  `exp7-b2-n1000` 10 x 19.5 min; `exp1-baselines-n1000` 60 x 14.5-19.5 min.
- **H7-S analysis (code for the declared supplementary pass; written before any H7-S or random-arm 1,000-node
  result).** `paper/analysis/latency_optimality.py` gains `run_h7s`: family H7-S (m = 2, experiments.yaml `h7s`),
  H7's two Wilcoxon tests on the timing-pass runs, plus a determinism check of every rerun against its session twin
  (same `config_id`): every summary field except wall and CPU time, the decision log with run IDs blanked, the time
  series and the pod-set snapshots must be identical, or H7-S is "void". Verdict words as H7's ("supported",
  "not supported", "inconclusive", "incomplete"), plus `agrees_with_h7`. Plumbing: the timing pass's per-seed groups
  (`h7t-101` ... `h7t-110`) are one analysis group, `h7-timing` (experiments.yaml `analysis.group_aliases`), so its
  configurations have 10 seeds and are not cut; its runs get the session binary and determinism checks
  (`common.SESSION_BINARY_MATRICES`). Also fixed a test fixture: the latency module's pilot smoke test read every
  matrix under `experiments/runs`, which now holds session runs too; it reads the pilot matrix only. No preregistered
  rule changed. All analysis test suites pass.
- **Adversarial review of the driver, the cut rule and H7-S (Oct 28, 03:51-04:48 UTC).** Three independent reviewers
  (driver; H7-S analysis; preregistration fidelity), each finding checked by two skeptics told to refute it: 17
  findings, 13 confirmed by both, 2 split, 2 refuted. The reviewers ran read-only commands on the VM while
  `exp1-n1000` ran (load average about 7 instead of 4). The cells running then were heuristic/anneal-plan W1 and W2
  and the relaunched heuristic/entropy-greedy W4 seeds 108-110; none of them enters H7, H7-S or Exp 7's tables
  (W1 entropy-greedy and B2 only), and their simulated outcomes do not depend on load. The fixes follow.
- **Correction: the H7 confound's direction (before any random/entropy-greedy 1,000-node result).** The "H7
  hardware confound" entry said a cross-machine comparison is biased against random sampling "by roughly the clock
  ratio (about 25%)". That rested on lscpu's model strings ("@ 2.80GHz" in epoch B, "@ 2.10GHz" in A and C), which
  are generic labels on these VMs and do not identify the hardware. The session's own data do not show it: per unit
  of work (CPU time per full simulation), heuristic/savings-only W1 at 1,000 nodes took 126-144 ms in epoch A and
  127 and 134 ms in epoch B, and heuristic/entropy-greedy W4 took 96-119 ms in B and 92-97 ms in C (different seeds
  and co-runners, so this is not a clean estimate either). The claim is withdrawn: the size and sign of the machine
  effect are unknown. H7 also differs between its arms in co-runner composition: Exp 1 runs arm-major, so each arm's
  cells ran beside three cells of the same arm. RESULTS reports H7 as preregistered with both differences stated,
  and attributes nothing to hardware unless H7-S's twin ratios (below) show it.
- **H7-S design revision (before any H7-S cell ran).** The reviewers found that the declared design (two seeds, four
  cells, per invocation) gives the arms unequal loads: random cells finish first, their slots stay empty, and the
  heuristic cells run their last passes at 2-way load. Revised design (`experiments/matrix/h7-timing.yaml`): all 20
  cells in one runner invocation with no wall budget, interleaved (seed 101 heuristic, seed 101 random, ...), so a
  freed slot is refilled at once and every cell runs at 4-way load beside a mix of both arms; four filler cells
  (groups `h7fill-111`, `h7fill-112`, seeds 111-112, discarded) keep the load at 4 while the last measured cells
  finish. A seed whose two arms started on different VMs is set aside with the fillers and rerun
  (`experiments/session-driver/h7_pairs.py`, up to three rounds), and the analysis drops any seed still split. The
  pass runs after the session's last group, unconditionally (this replaces "only if session time allows"). Reported
  with it: each run's mean concurrency, and each arm's CPU time per pass relative to its session twin (heuristic:
  session in epoch B; random: session in epoch C), which measures the combined machine and co-runner effect per arm.
  H7-S is compared with H7 test by test (CPU mean and p95), with H7's timeout clause reported beside it, never
  verdict against verdict; a difference is described as a difference between the two designs (machine and
  co-runner composition).
- **Driver v3 (replaces v2 before v2 acted).** The reviewers found v2's cut decisions were not restart-safe: they
  lived in memory and were recomputed after every restart, so after the deadline a restarted v2 would have cut a
  finished candidate and, with it, a candidate still running; and a crash of the cut-rule script exited 1, the same
  status as CUT. `run_session_v3.sh` records each candidate's decision once, before acting, in
  `experiments/session-driver/cut-decisions.txt` and follows it after any restart; a candidate with any run
  directory is already started and runs to completion; the script exits 0 (START), 3 (CUT) or anything else
  (error, after three tries the driver stops, recording nothing). It logs the VM (boot ID, boot time, CPU) in
  `experiments/session-driver/machine-log.txt` before every runner invocation on a new boot (epochs A and B
  reconstructed from the driver log), runs the revised H7-S pass and then the remaining parity chunks. v2 was
  stopped at 04:52 UTC while still waiting for v1 to finish `exp1-n1000`; v3 waits the same way. The scratchpad copy
  runs (a running bash script must not be edited); the repository copy is identical. `commit_runs.sh` now also
  commits each matrix's `index.jsonl`, set-aside attempts, the driver log, the machine log and the cut decisions.
- **Corrections to the restart entries.** At restart #2, 297 cells had finished (180 `exp1-n200`, 60
  `exp1-baselines-n200`, 57 `exp1-n1000`), not 302. Restart #1 killed heuristic/savings-only W1 seeds 109-110 and W2
  seeds 101-102, relaunched at 23:53:34 UTC.
- **Analysis fixes from the review (H7-S and run checks; no preregistered rule changed).** H7-S: per-run statuses
  (match, differs, not checked when a side was abandoned, no twin), so an abandoned run is dropped pair-wise
  instead of holding the check open; the pairing check by boot; twin CPU ratios and concurrency
  (`tables/h7s_runs`); test-by-test comparison with H7 (`tests_vs_h7`, `all_tests_agree`). `run_checks`: the
  timing pass stays out of the session totals, the cut table and the cross-group duplicate check (it has its own
  validity row and twin check), and its pending retries are counted under its group.
- **Second review, of the fixes (Oct 28, 05:00-05:57 UTC).** Same design, aimed at commit `7dccb8d`: 16 findings,
  13 confirmed by both skeptics, all minor but one (concurrency accounting). Fixed before driver v3 acts (it was
  restarted at 05:58 UTC with the fixes, still waiting for v1): `decide()` no longer reads an unset variable when a
  dry run fails (that would have killed the driver instead of retrying); `h7_pairs.py` reruns seeds with a retried
  attempt (a retry runs in a later invocation at lower load) as well as split seeds, reports abandoned arms instead
  of stopping the check, and moves nothing in the last round (`--report-only`); a parity chunk whose test fails on a
  mismatch still records its counts (with the exit status); `commit_runs.sh` stages the removal of cells
  `h7_pairs.py` moves. Analysis: concurrency counts set-aside cells and failed attempts (they ran beside the
  others); a run with no start time (abandoned) no longer marks its seed as split; `h7s_runs` writes times as
  RFC 3339 strings (they were rounded floats).
- **Amendment to the H7 attribution rule.** The twin ratios mix machine and co-runner effects for the heuristic arm
  (session in epoch B beside heuristic cells only; pass on the pass VM beside a mix) and measure co-runner effects
  alone for the random arm (both on 2.10 GHz-labelled VMs, if the pass runs on one). They are reported as the
  combined machine and co-runner effect per arm. No difference between H7 and H7-S is attributed to hardware alone.
- **Correction to the review-window entry, and a quiet rule.** The cells running during the first review (03:51-04:48
  UTC) were heuristic/anneal-plan W1 seeds 102-110 and W2 seeds 101-107 (the W4 entropy-greedy relaunches had
  finished by 03:46). Other elevated-load windows from my own work: about 03:30-04:05 (driver setup, analysis tests)
  and 04:50-05:57 (analysis tests, the second review). The cells overlapping them are heuristic/anneal-plan and
  heuristic/entropy-greedy W4 at 1,000 nodes, whose decision-cost numbers are descriptive only (Exp 1's secondary
  timing columns); the analysis reports each run's wall-to-CPU ratio per pass, a contention indicator, next to them.
  From now on, while cells whose decision cost enters a test or Exp 7 run (`exp1-n1000` random/entropy-greedy,
  `exp7-n50`, `exp7-b2`, `exp7-b2-n1000`, the H7 timing pass), I run no review workflows, test suites or builds on
  the VM: only the driver, monitoring and file edits.
- **Correction: load average is not a contention measure here.** With nothing but the four cells running (08:43
  UTC, no other process above 1% CPU), the load average is about 7: each Go process keeps several runnable threads
  (GC workers). So the "load average about 7 instead of 4" in the first review entry does not show that the
  reviewers perturbed the cells. Each run's `wall_cpu_ratio` (wall over CPU time across its passes) is the
  contention indicator the analysis reports. top also shows 2-3% steal time from the hypervisor, which no session
  process controls.
- **Handover to driver v3 (Oct 28, 11:28 UTC).** v1 finished `exp1-n1000` (180/180, none failed), skipped the rest
  (`session.skip`) and exited; v3 took over at 11:28:13 UTC on the same VM (epoch C), re-invoked the finished groups
  (each `0 pending`) and started `exp6`. No cut decision is recorded yet (no candidate group reached).
- **Provisional analysis and a summary module (11:40 UTC, no rule changed).** With Exp 1 complete, `make figures`
  ran on the finished groups into the scratchpad (not committed: most experiments are still running). Nothing in
  the remaining runs or the analysis depends on what it showed. New `paper/analysis/summary.py` (run last by
  `make figures`, with tests) collects every verdict and its key effects from the modules' JSON headlines into
  `tables/summary.{md,json}`, which `experiments/RESULTS.md` will quote, so the write-up cannot drift from the
  tables.
- **index.jsonl snapshots.** The runner appends to each matrix's `index.jsonl` with every finished cell (every 30 s
  or so at 200 nodes), so a tracked copy is always modified. The live file is untracked during the session;
  `commit_runs.sh` commits a snapshot as `experiments/session-driver/index-<matrix>.jsonl` with every data commit,
  and the final file goes back in place at the end (`FINAL=1 commit_runs.sh`), where the analysis reads it.
- **Independent recomputation of the provisional H1, H7 and run checks (12:00 UTC).** Three agents reimplemented
  H1 and the P0 contrast (TOST, 90% CIs, Holm, veto), H7 (exact Wilcoxon, floors, Holm, paired bootstrap, timeout
  clause, per-cell means) and the validity, completeness, duplicate and epoch checks from the raw run files, without
  the analysis code: every number and verdict in the provisional tables matched (bootstrap intervals to 0.01
  points); the one finding (restart-killed attempts are not counted as retries) was refuted by both verifiers, as
  the preregistered definition of a retry is a set-aside failed attempt. They noted that no table carried the
  machine epochs the H7 entries promised: each run now gets `machine_epoch` (A, B, C, ... from
  `experiments/session-driver/machine-log.txt` and its manifest's `started_at`), shown per cell in `exp7_latency`
  and in the summary's H7 row (heuristic/entropy-greedy at 1,000 nodes on VM B, random/entropy-greedy on VM C).
- **`make verify` clean (Oct 28, 13:37 UTC).** No Go code changed since Gate 3, so this checks the session binary's
  source. The first attempt failed on tooling only: the VM's `yq` is the Python jq wrapper, while
  `hack/validation/*.sh` need mikefarah's Go `yq`; with v4.44.3 installed (`go install`, scratchpad, first on PATH)
  `make verify` passes: `go generate` and the validation scripts leave no diff, `go vet` is clean, and the
  kube-api linter reports 0 issues. Run at `nice 10` while `exp6` (descriptive decision cost only) ran.
- **Disruption tests green at the session source (13:47 UTC).** `go test ./pkg/controllers/disruption/... -race`:
  the controller (479 s), the pipeline package with its parity and conformance suites (421 s) and the four strategy
  packages all pass (`nice 10`, beside `exp6`).
- **Container restart #3, Oct 28 about 23:24 UTC (epoch D).** The VM was replaced during `realism-w5`, its first
  cut-candidate group, which had started at 23:08:40 UTC under a recorded START decision. Every group before it had
  finished (1,550 cells, all valid). The four in-flight W5 cells (none had finished in 16 minutes) died without a
  `summary.json` and are relaunched. The new VM reports "Intel(R) Xeon(R) Processor @ 2.80GHz" (boot
  `e21797db`, logged by driver v3 in `machine-log.txt`). Driver v3 was restarted alone at 23:24 UTC and followed the
  recorded decision: `realism-w5` continues and runs to completion. The cut rule projected it at 2.0 minutes per cell
  (the same-scale fallback median); W5 cells are evidently slower, so the later candidates' projections will use
  W5's actual times through the elapsed clock. The claim check of the RESULTS ablation, H2, Exp 8 and B4 sections
  that was running was lost and is rerun.
