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
  `summary.json` and are relaunched. The new VM reports "Intel(R) Xeon(R) Processor @ 2.80GHz" (boot `e21797db`,
  logged by driver v3 in `machine-log.txt`). Driver v3 was restarted alone at 23:24 UTC and followed the recorded
  decision: `realism-w5` continues and runs to completion. The cut rule projected it at 2.0 minutes per cell (the
  same-scale fallback median); W5 cells are evidently slower. The next candidates are decided when `realism-w5` ends,
  so its real duration enters their decisions through the clock (and W5's finished cells enter W6's same-scale
  fallback median). The claim check of the RESULTS ablation, H2, Exp 8 and B4 sections that was running was lost and
  is rerun.
- **Overrun rule for a started cut candidate; `realism-w5` and every later candidate cut (Oct 29, about 00:28
  UTC, recorded at 00:29:08; time-only).** The cut rule started `realism-w5` at 23:08 UTC projecting 2.0
  minutes per cell (the same-scale fallback median, as no W5 cell had ever run at 200 nodes; the pilot ran W1 and W2
  only). Its B2 cells have since run 16 minutes (before restart #3) and then 62 more without finishing, so a B2 W5
  cell takes over an hour. Rule, declared now: a started cut-candidate group is re-projected from its own timing
  once any exists (its finished cells' median, or, before any finishes, the elapsed time of its in-flight cells as a
  lower bound per cell of that arm); if the re-projection ends after the deadline (2026-10-29T02:52Z), the group is
  stopped and cut whole (no analysis uses its cells), and every later candidate is cut, exactly as if the original
  projection had been right. Re-projection for `realism-w5`: its 20 B2 and B3 cells alone need at least
  20 x 62 min / 4 = 5.2 h, ending after 05:40 UTC whatever the pipeline arms cost, so it is cut, with `realism-w6`,
  `exp7-b2-n1000` and `exp1-baselines-n1000`. This replaces the earlier "a candidate group that starts runs to
  completion", which was meant to keep decisions restart-safe, not to commit to an unbounded run. Consequences:
  H1-realism (the W5 and W6 TOSTs) and H7-B2 have no data; H2 is decided on W2 and W3 only; Exp 8 has no 1,000-node
  B2 or B3 rows. The four in-flight W5 cells are stopped (none had a summary) and their directories removed. Driver
  v4 (`run_session_v4.sh`: v3 with the last recorded decision for a group taking effect, so a decision can be
  superseded only by a later logged one) records the CUT and moves on to the H7 timing pass. Why W5 is so slow at
  200 nodes was not diagnosed; a timing probe runs after the H7 pass, for the runbook.
  *Correction:* one W5 cell finished while the rule was being written (B2, seed 101, at 00:28:24 UTC, 3,809 s of
  wall time), unnoticed because the check looked at the time-series files, which the
  simulator writes only at the end. Only its wall time has been looked at. It confirms the decision: re-projected
  from it, the 59 pending cells need 59 x 63.5 min / 4 = 15.6 h. It stays in the run tree as a valid session run;
  its configuration has 1 of 10 seeds, so the completeness rule keeps it out of every test, and the cut table lists
  it.
  Driver v4 cut the four candidates at 00:29:10 UTC, reported `SESSION COMPLETE` and started the H7 timing pass at
  once (its start-up banner still says "v3"; the code is v4). The session matrix ends with 1,551 finished runs.
- **H7 timing pass complete; final analysis (Oct 29, 02:18-02:28 UTC).** The H7 timing pass ran 24 of 24 cells on
  VM D in one invocation (00:29-02:18 UTC). `h7_pairs.py` round 0 found no seed with arms on two VMs and no retried
  attempt, so nothing was rerun. `make figures` then ran over the complete run tree (1,551 session runs and 24 H7
  timing runs), and its outputs were committed (`9c93190`). `experiments/RESULTS.md` was finalized from
  `paper/tables/summary.md` (`d2d546f`). After the session the driver runs the three remaining 10,000-state parity
  chunks (`wour-assorted` from 02:17:50 UTC, then `balanced-kwok` and `balanced-assorted`). They are not
  timing-sensitive, so the W5 timing probe promised above runs beside them.
- **Correction: the Go version in the Phase 0 toolchain entry.** That entry reads `go.mod` as `go 1.26.6`, but this
  was the pre-rebase fork head (`d1aacae`). The upstream base `32fd95b` has `go 1.26.10` (upstream `700a369`). The
  image's own Go is 1.24.7, and `GOTOOLCHAIN=auto` fetches the version `go.mod` names. So the Phase 0 check ran
  1.26.6, and every binary built after the rebase used go1.26.10: `go version` reports go1.26.10 for the pilot
  binaries (`c3e9e35`, `2d8fce7`), the Gate 3 session binary and the parity test binary. `paper/REPRODUCE.md` and
  `PROGRESS.md` give 1.26.10.
- **W5 timing probe: why W5 is slow (Oct 29, 02:31-02:47 UTC, VM D, beside one parity chunk).** One B2 cell (W5, 200
  nodes, seed 101) run for 30 simulated minutes with the Gate 3 binary and `--cpuprofile`, outside the run tree
  (scratch directory; not a session run). It took 16 min 10 s of wall time (17 min 9 s user) for 94 disruption passes,
  with a peak of 255 nodes. Percentages are of all CPU samples. 84% is upstream `SimulateScheduling`, called from
  single-node consolidation (66% in all) and multi-node consolidation (18%). 80.5% is `scheduling.NewTopology` →
  `Topology.countDomains`, which lists pods by label selector once per topology group. The harness's pod index serves
  only `spec.nodeName` lists, so these lists fall through to the controller-runtime fake client. Its `List` (76% of
  CPU) deep-copies every pod in the tracker and converts the list through JSON (unmarshal 42%, marshal 20%). W5 is the
  only built-in scenario with hostname spread and anti-affinity (`scenario.go`; `ZoneSpread` is unused), so only W5
  pays this, and every pass pays it per topology group. W1, the H7 scenario, has none. **Cause:** a simulator cost,
  not a property of the strategies; a real controller reads an informer cache, with no JSON round trip. **Not fixed in
  the session:** a fix changes the binary, and every session cell must carry the Gate 3 `binary_revision`. Next step
  (runbook, RESULTS "Next steps"): serve label-selector pod lists from the harness's pod index. Then rerun the
  finished W5 cell (`realism-w5__arm-b2_seed-101__421f66a6`) and check that its decompressed decision log,
  `timeseries.csv`, `podsets.jsonl` and simulated `summary.json` fields match the session's (its timings and manifest
  will differ), which shows only that this cell's decisions are unchanged. Also check that the new binary still passes
  `TestDeterminism` (run-to-run determinism within one binary), before using it for new runs under a logged deviation.
- **Correction: the overrun cascade was the new rule's, not the preregistered rule's (found by the final claim check,
  Oct 29 02:33-03:32 UTC; no data involved).** The overrun-rule entry above cut `realism-w6`, `exp7-b2-n1000` and
  `exp1-baselines-n1000` "exactly as if the original projection had been right", and RESULTS credited that cascade to
  the preregistered rule. The preregistered rule is a start check only: a candidate that fails it is cut with every
  later candidate. `realism-w5` passed it (START at 23:08:40 UTC), so the cascade comes from the overrun rule. It also
  departs from this log's own plan at restart #3 ("the next candidates are decided when `realism-w5` ends"). Judged
  each on its own start projection at 00:29 UTC, with the mechanized rule over the 1,551 runs then finished:
  `realism-w6` 60 x 2.0 min / 4 = 30 min (the same-scale fallback median over 1,071 cells, the one W5 had just shown
  to be unreliable), `exp7-b2-n1000` 10 x 16.0 min / 4 = 40 min (W1 1,000-node median over 60 cells), and
  `exp1-baselines-n1000` 20 x (16.0 + 15.9 + 13.1) min / 4 = 3.75 h. `realism-w6` would have passed its start check,
  with a projected end about 00:59 UTC. The driver runs the candidates in turn, so `exp7-b2-n1000` would have been
  checked only when `realism-w6` ended. It would have passed if that was by about 02:12 UTC (02:52 minus its 40
  minutes), with a projected end about 01:39 UTC if `realism-w6` took its projected 30 minutes. These are projections
  only. The W6 one uses the fallback median that W5 had just shown to be wrong. The W1 1,000-node median comes from
  pipeline cells, while a 1,000-node B2 cell runs far longer (the pilot's hit its 100-minute wall limit at simulated
  minute 160). Whether either would have finished by the deadline is unknown. The decision stands as recorded, since
  the deadline has passed. RESULTS deviation 7, "What ran", REPRODUCE, the runbook and PROGRESS now name the overrun
  rule as the cause of all four cuts, and RESULTS deviation 7 gives this counterfactual. The record's 5.2 h estimate
  (20 B2/B3 cells x 62 min / 4) is also not a strict lower bound, since no B3 W5 cell had run. RESULTS now cites both
  of the record's figures: the 5.2 h estimate, with that caveat, and the finished-cell re-projection (15.6 h).
- **Correction: the preregistration's scale wording (RESULTS deviation 9).** PREREGISTRATION.md says `scenario.nodes =
  n` sizes each workload as n mid-size nodes at 70% fill, and that W2, W3, W5 and W6 run fewer, larger nodes. The Gate
  3 code fills W1 and W4's n starting nodes to a random 15-75% each (`fragmented` in
  `hack/consolidation-sim/sim/scenario.go`; mean CPU utilization 0.39-0.44 at t = 0 for n = 200). The 70% fill applies
  to W2, W3, W5 (`capCPU`) and W6 (`traceFill`). W3 peaks above n in most cells (above 200 in 73 of 120 Exp 6 cells).
  The code was not changed. The text was a description error, and this log's own "Scenario scale" entry (Oct 27) makes
  the same one. W1 and W4 are not at 70% fill, and W3 does not consistently run fewer nodes than n: it peaks above 200
  in 73 of 120 Exp 6 cells (peaks 124-339) and at or below 200 in the other 47. RESULTS "What ran" now states what
  ran.
- **Analysis changes after Gate 3, with times.** The alignment commit `5b6b2e3` (Oct 27 21:14 UTC) landed when 33
  session cells had finished, and the verifier's ten fixes `df7831e` (21:50 UTC) when 88 had. Both changed the code
  toward the preregistered text, not the text. Before them the code ran, for example, H7 on wall time per pass and H4
  as 16 tests; `df7831e` fixed ten places where the code still departed from the text ("Analysis alignment verified").
  Neither changed a preregistered test, family, margin or decision rule. One rule is a reading of the text: H4's
  "partially otherwise" is read as "not supported" when no clause cell holds (item 10 there), and H4 is partial on the
  data either way. Two presentation-only commits touched `paper/analysis` without their own entry here: `bd2277c` (the
  summary reads H5's nested verdict, Oct 28 17:26) and `a83907b` (H3 counts in the summary, 19:08). The others have
  entries: `032fe98` H7-S, `7dccb8d` and `e9117f7` from the reviews, `e776227` the summary module ("Provisional
  analysis and a summary module"), and `dcd657b` machine epochs.
- **Final claim check (Oct 29 02:33-03:32 UTC).** Six checkers, one per passage, read RESULTS' status banner, "What
  ran", verdicts, H2 and H7-S, Exp 8 and run checks, deviations, threats and next steps, plus REPRODUCE, the runbook
  and PROGRESS. A second, independent checker re-derived each of their 49 flags. 38 were confirmed and fixed in
  `b8aabd6`, with every replacement number recomputed from the run tree, the tables or git. parity.md's chunk results
  had already been filled in by `272cfc3`, and the runbook's "four chunks" sentence was missed and fixed in the next
  round. The fixes:
  - the cascade and scale corrections above;
  - the W1/W4 fill and W3 node counts;
  - "one VM" (four in turn);
  - where the parity counts and run history come from;
  - the verdict table's sources;
  - the slowest passes (W1 7.0 s, H7-S 20.8 s, the W5 B2 cell 52.3 s);
  - the complete list of what the cuts lost;
  - which tables carry `machine_epoch`;
  - the analysis commit times;
  - the first driver's error;
  - the threats list, including the full preregistered set and what `wall_cpu_ratio` shows;
  - the Exp 5 seed-107 clause;
  - running `exp7-b2-n1000` as a paired pass;
  - in RESULTS "Next steps": fixing W5's simulator cost first, from the timing probe;
  - in PROGRESS: parity re-checks on 100-150 states per cell (was "150 states");
  - in REPRODUCE: the driver order (v1, then v3, then v4; v2 was armed but never acted);
  - in REPRODUCE: building at Gate 3 in a separate build tree (first written as a `git worktree`, which gets no Go VCS
    stamp, as the Gate 3 entry says; corrected to a clean clone in the next round); `TestDeterminism`'s actual scope,
    with the H7-S twin check as the cross-VM evidence; the upstream 1-minute multi-node timeout, never reached;
    rebuilding the pilot with its own binaries;
  - in the runbook: the full matrix size (3,900), cell timings from the session data, and memory (not recorded).

  Of the 11 unconfirmed flags, eight were wording points. Two were wrong on the facts: they said no two-run
  byte-identity check was recorded for the final outputs (`9c93190` records it). The third said parity.md was stale,
  which was true when it was raised and is the same point as a confirmed flag, but `272cfc3` (03:16) updated parity.md
  before its verifier read the file. Five changes from the wording points were applied anyway: "190 cells, 189 of them
  unrun" (corrected to "unfinished" in the next round, since three of them, W5 B2 seeds 102-104, had run before they
  were stopped); the verdict table's words matched to summary.md ("absent (group cut)" for H7-B2, "no runs (groups
  cut)" for H1-realism, "reported: all valid" for the run checks); H7-S "not supported" without "too"; the retry
  definition in the run checks; and "heuristic/savings-only" in Exp 8.
- **Second check: the fixes themselves (Oct 29 03:41-04:33 UTC).** Five checkers re-read the lines `b8aabd6` added to
  RESULTS, REPRODUCE, the runbook, this log and PROGRESS. A sixth swept every document for text still stating the
  corrected facts in their old form. An independent checker re-derived each flag: 37 flags, 33 confirmed. Fixed in
  `ddc71b6`, the commit that adds this entry:
  - **The worktree build.** REPRODUCE and the runbook had said to build the Gate 3 binary in a `git worktree`. Go
    1.26.10 detects a Git root only when `.git` is a directory (`cmd/go/internal/vcs`), so a worktree build has no
    VCS stamp and its runs would read as the wrong binary. Both now use a separate clone, as the session did.
  - **The cut record.** RESULTS said the record used only the in-flight 5.2 h estimate and that the finished W5 cell
    was "noticed minutes later". The record (00:29:08) cites both figures, and the cell finished at 00:28:24.
  - **The counterfactual.** It now says that both start projections are unreliable, so whether `realism-w6` or
    `exp7-b2-n1000` would have finished is unknown.
  - **W5 instructions.** RESULTS and the runbook gave conflicting orders for W5: run it with the Gate 3 binary, and
    fix the simulator first. A fixed binary is a new `binary_revision`, invalid in the session tree, so it needs its
    own run tree and a logged deviation; the docs now say so.
  - **The paired H7-B2 design.** It needs a new matrix, a group alias and an H7-B2 change in the analysis registry
    (`experiments.yaml`), and the new matrix added to `SESSION_BINARY_MATRICES` in `paper/analysis/common.py`. The
    cross-machine option instead needs the new machine's line in `machine-log.txt`.
  - **Smaller points:**
    - where the p values live (`h7s_tests`, `exp1_h1`, `exp3_h3`, `exp7_tests`, `fig8_ablation`);
    - which numbers come straight from run files;
    - the inputs `make figures` reads;
    - the first parity chunk's two-catalog run;
    - W5 B2 timing (more than an hour; B3 never timed);
    - B2/B3 being faster than the slowest pipeline arms on W2;
    - the 12% H7-S rise (machine and co-runners together);
    - `TestDeterminism` cannot compare two binaries;
    - stale parity status in the runbook, the RFC and PROGRESS;
    - the driver README's "the cloud VM";
    - this log's own entries: the alignment wording, the scale entry, the unconfirmed-flag tally, "unrun" →
      "unfinished".
- **10,000-state parity complete (Oct 29 05:18 UTC).** Driver v4 ran the last three chunks one after another:
  `wour-assorted` 02:17-03:14, `balanced-kwok` 03:14-04:07, `balanced-assorted` 04:07-05:18. It then logged `ALL
  BACKGROUND WORK COMPLETE` and exited. All four chunks matched 10,000 of 10,000 states in every configuration (coarse
  and fine adapters, plus the Balanced planner on Balanced states): 40,000 states, each in two or three
  configurations. Upstream decided on 8,347-9,141 states per chunk and disagreed with itself on 79-247 multi-NodePool
  states, all resolved within the observed decision set. Every chunk ran on a parity test binary built after the last
  pipeline commit (`pkg/` is unchanged since `2d8fce7`). Gate 1's 10,000-state run is therefore complete, with no
  mismatch. `docs/research/parity.md`, RESULTS, PROGRESS, the runbook and the RFC give the final result; REPRODUCE
  gives the chunks' run times and points to `docs/research/parity.md`. `experiments/session-driver/session.log` is the
  driver's complete log.
- **Correction: three, not four, W5 cells were stopped at the overrun cut.** The overrun entry says "the four
  in-flight W5 cells are stopped". The runner had launched B2 seeds 101-104 at 23:24:56 UTC under a 60-minute
  wall budget per invocation. When seed 101 finished at 00:28:24, 63.5 minutes in, the budget was spent, so no
  fifth cell was launched. The cut at 00:29:08 therefore stopped three cells (seeds 102-104), and their
  directories were removed. RESULTS now says three.
- **Third check: the second round's fixes (Oct 29 04:39-05:19 UTC).** Five checkers read the lines `ddc71b6`
  changed, and a sixth swept the documents for passages that state the same fact differently. An independent
  checker re-derived each of their 25 flags, and all 25 were confirmed. They are fixed in the commit that adds this
  entry:
  - three, not four, W5 cells stopped;
  - W3 peaks above n in most cells, not all (here and in RESULTS deviation 9);
  - the counterfactual: `exp7-b2-n1000` would have been checked only when `realism-w6` ended;
  - which numbers come straight from run files (the list is examples, not exhaustive);
  - a W5 rerun shows only that cell's decisions are unchanged;
  - the run-check count: 1,571 = 1,551 session runs + 20 measured H7-S runs (the 4 fillers also carry
    `c78b5f9`);
  - REPRODUCE's cut-group command no longer runs the timing group `exp7-b2-n1000` with the others;
  - the runbook's B2/B3 timing comparisons (B3 never timed at 1,000 nodes; W1/W4 within the pipeline range);
  - why the paired H7-B2 design needs a new matrix (`--only` selects whole groups);
  - stale parity status in parity.md, the runbook, PROGRESS and the RFC (now final);
  - the RFC's two remaining "results pending" lines;
  - this log's own entries ("commit that follows", the flag tallies, the H7-B2 design summary, "faster than the
    pipeline", the 15.6 h citation, `TestDeterminism`'s scope in the probe entry).

  The confirmed-flag counts fell from 38 to 33 to 25 over the three rounds (from 49, 37 and 25 flags raised). Most of
  this round's flags were in text the earlier rounds had added or in status that went stale while the parity chunks
  ran. Two were in text written just before the first check began at 02:33: RESULTS' run-check count (`d2d546f`,
  02:27) and REPRODUCE's cut-group command (`91b2fba`, 02:32). None touched a verdict, effect or test.
- **Fourth check, and the session's end (Oct 29 05:22-05:37 UTC).** Four checkers read only the lines `d9633aa`
  changed. RESULTS, parity.md, REPRODUCE and the runbook drew no flags. The research log and PROGRESS drew 7, and
  an independent checker confirmed all 7. They are fixed in the commit that adds this entry:
  - RESULTS deviation 7 now has the candidates' order (`exp7-b2-n1000` would have been checked only when
    `realism-w6` ended);
  - one leftover "four" became "three";
  - the third round's tallies and their provenance;
  - the probe entry's rerun check;
  - which documents state the final parity result;
  - PROGRESS's "under driver v4", which excluded the first chunk.

  The checks stop here. The last round found nothing in RESULTS or REPRODUCE, and its flags were in this log's own
  bookkeeping. **Final reproducibility check:** `make figures` at `d9633aa`, run into a scratch directory (05:22-05:24
  UTC, 1 min 32 s), gave all 60 committed tables and 18 committed figures byte for byte. No table, figure, verdict or
  analysis code changed after `9c93190`. The next commit, `5ceb8d9`, added the runs it was built from that were not
  yet committed (the 24 H7 timing runs and the two run indexes). Every later change is to the documents, the driver's
  log and the parity records.

## 2026-10-29 (after the session): Tier B

- **Tier B unblocked (Oct 29 about 07:20 UTC).** The user added network access for the Kubernetes image
  registries. From this VM, `registry.k8s.io`, `cgr.dev`, Docker Hub, `dl.k8s.io`, `get.helm.sh`,
  raw.githubusercontent.com and the KWOK release downloads all answer. Tier B work from here on is after the
  session and outside the preregistration's session profile. The validation design is logged before any Tier B
  result, below.
- **Tier B environment: four workarounds (07:24-08:00 UTC, no data).**
  1. **Docker.** `dockerd` was installed but not running, so it is started by hand.
  2. **The control plane.** `kind` v0.30.0 (node image `kindest/node:v1.34.0`, containerd 2.1.3) could not start
     the control plane. Every pod sandbox failed with runc's "can't get final child's PID from pipe". The VM
     withholds `CAP_SYS_RESOURCE` (CapEff `0x1fffeffffff`, bit 24 clear), so no process may lower its
     `oom_score_adj`, and runc fails on the kubelet's -997/-998 for control-plane pods. The fix is containerd's
     rootless setting, `restrict_oom_score_adj = true`, which clamps the score instead. It is applied as a
     `containerdConfigPatches` entry on a copy of `hack/kind/cluster.yaml`, with the repository file unchanged.
     The kubelet still logs that it cannot set its own OOM score or the file-handle rlimit; both are non-fatal.
  3. **Images.** The kind node inherits the host's `HTTPS_PROXY=http://127.0.0.1:40999`, which inside the node is
     the node's own loopback, so the node cannot pull. Images are pulled on the host and loaded with
     `kind load image-archive`. The archive is made with `docker save --platform linux/amd64`, because a plain
     `kind load docker-image` fails on Docker's containerd image store ("content digest ... not found"). Only the
     KWOK controller image needs this. `ko` loads the Karpenter image itself (`KO_DOCKER_REPO=kind.local`), and
     workload pods run on KWOK's fake nodes, which pull nothing.
  4. **Installation.** The controller is installed as `make apply-with-kind` does, minus its `verify` step and with
     `serviceMonitor.enabled=false`, since no Prometheus operator is installed. The performance suite reads the
     controller's own `/metrics` through the API server, so it needs neither Prometheus nor metrics-server.
  The tools are `kind` v0.30.0 and `ko` v0.18.0 (`go install`), helm v3.19.0, and envtest's kubectl v1.36.2. The
  controller image was built by `ko` from `a54fb19`.
- **Tier B protocol and window (Oct 29, 08:10-08:40 UTC; before any evaluation-seed run in either tier).** The
  protocol is `experiments/TIERB.md` (committed in `eff89a1`, with the simulator's new WB scenario and
  `consolidation-sim workload`). The calibration group `tierb-cal` ran on pilot seeds 1-5: 7 configurations, n = 50
  and 200, a 45-minute window, on the binary built from a clean clone at `eff89a1`. All 70 runs were valid. Settle
  times are long because late single moves keep trickling in. The per-configuration 90th percentile of minutes from
  the drop to the last node-count change was:

  | B2 | B3 | B4 | heuristic/entropy-greedy | heuristic/anneal-plan | random/anneal-plan | random/entropy-greedy |
  | --- | --- | --- | --- | --- | --- | --- |
  | 41.3 | 29.7 | 41.1 | 42.1 | 34.9 | 42.3 | 43.2 |

  The rule (largest value, rounded up to 5 minutes, clamped to 10-30) gives **T = 30 minutes**; the clamp binds.
  WB at n = 50 averaged 12.4 nodes at the drop and 6.2 at the window's end; at n = 200, 45.2 and 18.2. The
  `tierb-a` group (seeds 101-110, duration 35 minutes) is added to `experiments/matrix/tierb.yaml`.
- **Tier B harness.**
  - `test/suites/performance/tierb_test.go` replays one cell. It skips unless `TIERB_CELL` is set, so CI is
    unaffected. `experiments/tierb/run_tierb.py` drives the cells in plan order.
  - The controller image was rebuilt by `ko` from the clean clone at `eff89a1` (tag `f5ee205e…`) and runs the
    session catalog's 15 types: a ConfigMap of the 15 Linux entries of `aws_instance_types.json` is mounted as
    `INSTANCE_TYPES_FILE_PATH`, and its on-demand prices were checked equal to `experiments/catalog.json`.
  - The controller runs at info log level for every arm, because debug logging would inflate the controller CPU
    being measured.
  - One mechanics check (B2, n = 50, pilot seed 1, a 5-minute window, written outside the run tree) runs before
    the evaluation cells. It checks the files and the eviction counter, not the numbers.
- **Tier B harness review (Oct 29, 08:50-09:44 UTC; before any evaluation-seed Tier B run).** Three reviewers read
  the protocol, the KWOK spec and the driver, and an independent verifier re-derived each of their 22 findings: 13
  were confirmed. All are fixed, and `experiments/TIERB.md` is revised to match.
  - **Blocker: a zero node budget does not hold consolidation off.** The pipeline's shift moves remove no node, so
    `ExceedsBudgets` passes them. During warmup the two random arms would have consolidated in Tier B only, since
    the simulator runs no pass then. The NodePool now has `consolidateAfter: Never` through the warmup, switched to
    `30s` at the drop. A cell with any eviction before the drop is invalid.
  - **Provisioning in batches.** The mechanics cell provisioned in six batches as the ReplicaSets created pods, and
    ended with 14 nodes against Tier A's 12. The spec now creates the NodePool only when every pod exists and is
    unschedulable, and t = 0 is that moment.
  - **Node latency.** KWOK registered nodes in about 1 s, against the simulator's 32-65 s, which favors arms that
    replace often. Tier B now sets `nodeRegistrationDelay: 47s`, near the simulator's mean of 48.5 s.
  - **Smaller fixes.**
    - The pipeline is seeded with the run seed, as in the simulator, instead of 0.
    - Every attempt restarts the controller: `TIERB_RUN_ID` carries a nonce.
    - Each cell records which methods evaluated decisions, and a cell fails validity on a mismatch. It also records
      the controller's decision-evaluation time, nodes and cost at the drop, and evictions before it.
    - The driver counts a cell only if its spec exited 0 and its summary is valid.
    - It writes the manifest atomically when a cell starts, and checks env vars and cluster health first.
    - It reruns an invalid cell once, at once. It does not count interrupted or infrastructure failures as
      attempts, and stops after two failures in a row.
    - The stopping rule no longer starts a new round after 36 h, but always finishes a round it has started.
    - The analysis uses only complete (seed, n) blocks.
  - **Not confirmed (left as is, with the verifiers' reasons).** kindnet puts a 100m/50Mi DaemonSet pod on every KWOK
    node, and the simulator has no DaemonSets. But WB's pod shapes are multiples of 250m and 512Mi, and both tiers
    compute allocatable the same way, so the overhead never changes what fits. The controller runs NodeRepair,
    CapacityBuffer and StaticCapacity in every Tier B arm, and the simulator runs none. Repair only acts on node
    conditions WB never sets, so decisions do not change. The CPU-seconds estimate from mean rates is exact when no
    scrape fails, and none failed. The compiled test binary matched the committed spec.

  Before the evaluation cells, three smoke cells on pilot seed 1 check the fixes:
  - B2 at n = 50 and heuristic/entropy-greedy at n = 50, covering both gates and the method check;
  - random/anneal-plan at n = 200, checking provisioning within the warmup with the 47 s delay.
- **Tier B smokes (Oct 29, 09:47-11:10 UTC; pilot seed 1; before any evaluation-seed Tier B run).** The first
  three smoke cells, with a 47 s registration delay throughout, were valid on every check, and both gates showed
  the right methods. But one seed's arms started from different fleets: B2 had 14 nodes ($19.20/hr) and
  heuristic/entropy-greedy 12 ($18.32/hr). Pods stayed pending while nodes registered, and Karpenter's 10 s
  re-plans added 1-4 NodeClaims depending on timing. The simulator re-plans the same way, but deterministically,
  so all its arms of a seed start alike.

  The fix is a registration delay of 0 s during the warmup and 47 s from the drop, both set per cell. Four more
  smoke cells:

  | Cell | Nodes at the drop | $/hr at the drop | Tier A |
  | --- | --- | --- | --- |
  | B2, n = 50 | 11 | 17.53 | 12 nodes, $17.10/hr |
  | heuristic/entropy-greedy, n = 50 | 12 | 17.10 | 12 nodes, $17.10/hr |
  | random/entropy-greedy, n = 50 | 11 | 17.53 | 12 nodes, $17.10/hr |
  | B3, n = 200 | 45 | 71.08 | 44 nodes, $68.03/hr |

  All four were valid, with nothing pending and no eviction before the drop. What variation remains comes from
  kube-scheduler binding pods in its own order rather than Karpenter's plan, so one follow-up NodeClaim varies.
  That is real-cluster behavior and does not depend on the arm, since consolidation is off during warmup: it adds
  noise, not bias. A secondary metric, `cost_end_rel` (`cost_end` over $/hr at the drop), is added before any
  evaluation-seed run so that the analysis can separate the starting fleet from consolidation. Every run records
  its starting fleet. The smoke outputs are in `experiments/tierb/smoke-*`, with the first design's in
  `smoke-warmup47s-*`.
- **Tier B evaluation run (from Oct 29, 11:11 UTC).** The driver started the 70 evaluation cells (seeds 101-105,
  both sizes, seven configurations) at 11:11 UTC, in the order in `experiments/tierb/plan.json`. Cells take 36-37
  minutes. Finished cells are committed in batches by `experiments/tierb/commit_tierb.sh`, run after every third
  cell (about every 110 minutes).
- **Interim structural check (Oct 30, 05:30 UTC; 30 of 70 cells done).** The analysis module was run once on the
  finished cells, into scratch space, to confirm that it reads real Tier B output. It found four complete blocks
  (seeds 101 and 102, both sizes) and no missing metric in any finished cell. The running cell, which has no
  summary yet, was counted as invalid, as expected. Only block counts and missing values were printed: no ranking,
  mean or agreement statistic was looked at, and nothing in the protocol or the analysis changed.
- **Container restart during Tier B (Oct 30, about 07:01 UTC; 32 of 70 cells done).** The container restarted
  during cell 33 (B3, n = 200, seed 103; started 06:43). Docker and the driver stopped. Cells 1-32 had all finished
  and were valid. The 32nd finished at 06:43. Cells 31 and 32 were on disk but not yet committed at the restart, and
  were committed after it (`c0180c5`, 07:07). After the restart:
  - `dockerd` was started by hand, and the kind node came back on its own, with the interrupted cell's NodePool,
    NodeClaims and 825 pods still in place. They were deleted by hand, and the cluster was left with only its
    control-plane node before the driver resumed. The suite's own pre-suite cleanup does the same deletion but
    does not wait for leftover nodes to go, and a leftover node would take the next cell's pods.
  - The driver resumes per protocol: it sets cell 33's first attempt aside as interrupted and reruns it, without
    counting an attempt. The downtime counts toward the 36-hour stopping rule.
  - **The VM changed.** The new boot's CPU is "Intel(R) Xeon(R) Processor @ 2.10GHz". The first Tier B boot's
    (`51dc11d6`) was not recorded. From now on `experiments/tierb/env/run-driver.sh` writes one line per boot to
    `experiments/tierb/machine-log.txt`, and every cell's manifest already carries its `boot_id`.
  - **A sensitivity check, decided now, before any ranking or mean is looked at.** Controller CPU depends on the
    machine, and the block (seed 103, n = 200) now mixes two VMs: four cells on the first, three on the second.
    The analysis will add each run's `boot_id` and report the compute agreement twice: on all complete blocks, and
    on the blocks whose seven cells ran on one VM. The other metrics do not depend on machine speed in the
    simulator. In Tier B, they could depend on it only through timing, and they are reported on all blocks.
- **Second container restart (Oct 31, about 00:42 UTC; 60 of 70 cells done).** The container restarted during
  cell 61 (B3, n = 200, seed 105; started 00:09). This time the session's scratch directory, which held the driver's
  tools and binaries, was lost as well. Docker's state and the committed repository survived, so the kind cluster
  came back with the controller image already loaded. Recovery:
  - Docker was started by hand. kind v0.30.0 and ko v0.18.0 (`go install`) and helm v3.19.0 were reinstalled into
    `/tmp/tierb/bin`, as `experiments/tierb/env/setup.sh` does. kubectl, which setup.sh only requires on PATH, was
    linked there from envtest's `/usr/local/kubebuilder/bin/kubectl` (v1.36.2, the binary used before).
  - The simulator was rebuilt from a clean clone at `eff89a1`; its VCS stamp is `eff89a1`, unmodified. It regenerated
    seed 105's n = 200 workload identical to the one committed in that block's B2 cell.
  - The spec binary was rebuilt from the branch. No file under `test/`, `pkg/`, `cmd/`, `kwok/` or the Go modules
    changed after `bd2619e`, the Go toolchain is the same (1.26.10), and the new binary has the same size, 96,071,646
    bytes. From cell 61's rerun on, manifests record the new binaries' paths (`/tmp/tierb/bin/...`).
  - The interrupted cell's NodePool, NodeClaims and 807 pods were deleted by hand, as after the first restart. The
    driver resumed at 00:52 through `experiments/tierb/env/run-driver.sh`: it set cell 61's first attempt aside and
    reran it. Seed 105's round had started at 21:40 on Oct 30, before the 36-hour cutoff (23:11), so it is finished
    as the stopping rule says.
  - The new boot's CPU is again "Intel(R) Xeon(R) Processor @ 2.10GHz" (`experiments/tierb/machine-log.txt`). The
    block (seed 105, n = 200) now mixes two boots, so the single-VM compute check leaves it out too.
- **Tier B complete (Oct 31, 06:56 UTC).** All 70 cells finished and were valid on their first counted attempt. No
  cell was rerun for invalidity and none was abandoned. Two attempts were interrupted by container restarts and set
  aside: cell 33 lost about 25 minutes, cell 61 about 43. The run took 43 h 44 min from the first cell's start
  (Oct 29, 11:11:33 UTC). All ten (seed, n) blocks are complete, so the analysis drops nothing. Two blocks,
  (103, 200) and (105, 200), span two boots each.
- **Tier B results, and post-hoc exploratory tables (Oct 31, about 06:57-07:12 UTC).** These are the protocol's
  tables (`tierb_runs`, `tierb_means`, `tierb_agreement`, `tierb_reversals`, `tierb_pairs`, `tierb_compute_one_vm`
  and `tier_b.json` in `paper/tables/`):
  - Kendall's tau-b for `cost_end` is 0.20 at n = 50 and 0.33 at n = 200, with 21 material reversals across the
    floored metrics.
  - For evictions, tau-b is 0.49 and 0.81, with no material reversal.
  - For compute, tau-b is 0.33 and 0.52 on all blocks, and 0.33 and 0.71 on the single-VM blocks (5 and 3 blocks),
    with no material reversal.

  Two things in those tables mattered for reading them:
  - **B4 and B2 decide alike in Tier A.** Their per-seed `cost_end` is identical in all 5 seeds at n = 50 and in 3 of
    5 at n = 200. In Tier B the same pair differs by 10.4% in one seed (n = 50, seed 102).
  - **The starting fleets vary.** Each configuration's cost at the drop varies by up to 10% within a seed (n = 50,
    seed 101: $15.17-16.75/hr), although consolidation is held off until the drop.

  So Tier B's run-to-run variation is larger than many of the 1% differences that count as material. To show which
  differences Tier B resolves at 5 seeds, `tierb_paired` and `tierb_reversals_paired` were added after the
  protocol's tables had been seen. `tierb_paired_summary`, which backs the counts below, was added shortly after
  (`671c35e`). They give the per-seed paired differences per pair and metric in each tier,
  with a two-sided 95% t interval. They are exploratory, and the protocol's tables are unchanged. The result:
  - No material reversal is resolved in both tiers.
  - Tier B resolves one reversal: B3 vs B4 at n = 200 on `cost_end`, a direction Tier A does not resolve.
  - Of the 54 directions Tier A resolves at 5 seeds (cost_end, evictions and compute at both sizes), Tier B shows
    the same sign in 47 and resolves the opposite direction in none.
- **Claim check of the Tier B write-up (Oct 31, 07:24-07:58 UTC; fixes until about 08:15).** A workflow checked the
  new RESULTS section, the edits for Tier B elsewhere in RESULTS, PROGRESS, REPRODUCE, the runbook and these log
  entries against the tables, the raw runs, the scripts and git history. It had five checkers, each followed by an
  independent verifier, and the verifiers confirmed 32 problems. Fixed:
  - **The fine-order explanation.** The first write-up blamed Tier B's run-to-run variation for the reversals
    among the five near-tied configurations. That was wrong. At 5 seeds Tier A itself resolves none of their
    cost-metric differences (0 of 60 pairs). Tier B resolves 7 of them at n = 200, none of which Tier A resolves.
    The median `cost_end` interval across pairs is also wider in Tier A than in Tier B (±7.3% against ±5.4% at
    n = 50; ±7.1% against ±4.7% at n = 200). RESULTS now says the fine order is not established by either tier.
    The claims that differences of a few percent "do not resolve" in Tier B, and that effects of that size need
    "far more than 5 seeds", are withdrawn. So is the seed arithmetic built on the B2 vs B4 interval. To back the
    new text, `tierb_paired` gained each interval's half-width as a percent of the pair's level, and
    `tierb_paired_summary` gained the medians. `tierb_means` gained the commands decided per cell.
  - **The protocol's timing.** RESULTS and PROGRESS said the protocol was written before any evaluation-seed run
    in either tier. That is true only of its first version (`eff89a1`, 08:26). Both revisions (`1806e5a` 09:49,
    `8ef3481` 11:11, which added `cost_end_rel`) came after Tier A's 140 evaluation-seed runs (08:47-08:59), which
    were not analyzed, and before any evaluation-seed Tier B run. The same applies to the "Tier B smokes" entry
    above: `cost_end_rel` was added before any evaluation-seed Tier B run, not before any evaluation-seed run.
    REPRODUCE now cites `8ef3481` for the protocol as run.
  - **The other corrections:**
    - the named-pair table's n = 200 eviction cell (neither tier resolves it);
    - "random generation is worse on every axis" (not on evictions);
    - the scope of "random generation ends dearer" (resolved at n = 200, not at n = 50);
    - what the decision-evaluation histogram times (each whole disruption attempt, including the 15-second
      validation wait);
    - what the single-VM check covers (compute's tau-b only);
    - in REPRODUCE and the runbook: how the driver skips cells (valid cells only), the manual steps after a
      container restart, the stopping rule for new seeds, the dependence on `tierb-cal`, and what
      `commit_tierb.sh` adds and where it pushes;
    - the smoke-cell order in PROGRESS;
    - in this log: kubectl's source after the second restart, the commit cadence, the results entry's time and
      tables, and `tier_b.py`'s "the VM changed once".

  The checkers' rejected findings and the verifiers' reasons are in the workflow transcript.
- **`setup.sh` checked end to end (Oct 31, 07:02-07:38 UTC).** The check ran `setup.sh` on a fresh cluster (the run's
  cluster deleted), then one smoke cell (B4, n = 50, pilot seed 1).
  - **First attempt.** The setup finished (316 s), but the smoke failed in the suite's clean-cluster check: kind's
    local-path provisioner was pending. `setup.sh` had tainted the control plane before the provisioner was
    scheduled, and the provisioner does not tolerate `CriticalAddonsOnly`. The session's cluster never showed this
    because it was tainted by hand minutes after creation. `make setup-kind` has the same race. `setup.sh` now waits
    for kind's add-ons before tainting (`c5a055f`).
  - **Second attempt.** Setup took 174 s, and `ko` built the run's controller image with the same digest
    (`f5ee205e…`). The smoke was valid, with 12 nodes at the drop, nothing pending and the right methods.

