# Progress

Mirrors `docs/research/PLAN.md`. Updated at every milestone. After a context compaction, re-read `PLAN.md` and this
file before the next action. Decisions and fallbacks are logged in `experiments/RESEARCH_LOG.md`.

## Definition of done

- [x] Branch rebased on upstream main (fast-forward to `32fd95b`); upstream disruption tests green.
- [x] `make verify` clean (13:37 UTC Oct 28, at the session source; needs mikefarah `yq` v4 on PATH).
- [x] `pkg/controllers/disruption/pipeline`: interfaces, registry, adapters, strategies, conformance and parity
      tests, behind the `ConsolidationPipeline` gate, off by default.
- [x] `hack/consolidation-sim/`: the simulator, a determinism test, and the resumable experiment runner.
- [x] `experiments/PREREGISTRATION.md`, committed before the main runs, and session-profile results for all eight
      experiments in `experiments/runs/`: 1,551 of 1,740 session runs (four cut-candidate groups cut on time alone, by
      an overrun rule declared in the session; every experiment has data, though the cuts removed H1-realism (W5, W6),
      H2's W6 tests and its 1,000-node B2/B3 W2 runs, H7-B2 (Exp 7's 1,000-node B2 point) and the 1,000-node B2/B3
      reference rows of Exp 1 and Exp 8; RESULTS deviation 7) and the supplementary H7 timing pass (24 runs).
- [x] `paper/figures/` built by `make figures`, plus `experiments/RESULTS.md` (final for the session).
- [x] `docs/research/`: code map, parity report (with the 10,000-state chunks), runbook; RFC draft at
      `designs/consolidation-pipeline.md`.
- [x] Branch pushed (blocked by 403 until 18:40 UTC, then restored; pushed at every milestone since).

## Self-gates

- [x] Gate 0: rebased on upstream main; `go build ./...` and the disruption tests pass.
- [x] Gate 1: 100% parity on sampled states: 1,000/1,000 per policy and catalog, coarse and fine adapters, and
      re-checked on 100-150 states per cell after later pipeline changes (recorded only in `docs/research/parity.md`'s
      table; their output was not saved). The 10,000-state run uses a test binary built after the last pipeline commit
      (`2d8fce7`; `pkg/` is unchanged since). All four chunks are 10,000/10,000 in every configuration (the last
      finished Oct 29 05:18 UTC).
- [x] Gate 2: determinism test passes (14 cases: W1-W6, B2, B3, B5, both scale-down models, five pipeline arms).
- [x] Gate 3: `experiments/PREREGISTRATION.md` committed (`c78b5f9`) before the first session run (20:52 UTC).
- [x] Gate 4: `make figures` rebuilds every figure and table from raw runs (byte-identical across two runs; outputs
      committed in `paper/tables/` and `paper/figures/out/`).

## Phase 0: rebase, environment, code map

- [x] Rebase onto upstream main (no conflicts: the fork had no commits of its own).
- [x] Toolchain: Go 1.26.10 (from `go.mod` at the base; the Phase 0 log's 1.26.6 was the pre-rebase fork's),
      envtest 1.36.2, Python analysis venv (`paper/requirements.txt`).
- [x] Baseline: `go build ./...` 93 s; `go test ./pkg/controllers/disruption/... -race` green, 400 s.
- [x] Code map with seams, nondeterminism sources, time limits and measurement assets (`docs/research/code-map.md`).
- [x] Tier B blocked hosts recorded (`registry.k8s.io`, `cgr.dev`).

## Phase 1: decomposition and parity

- [x] Interfaces, registry, Snapshot, Move, Recorder, config loader.
- [x] `ConsolidationPipeline` alpha gate (off) and `--consolidation-pipeline-config`; registration replaces
      multi-node and single-node consolidation only when the gate is on.
- [x] Upstream adapters, coarse and fine, plus `Command` to `Move` conversion and its inverse (Full feasibility).
- [x] Differential parity harness.
- [x] Parity at 1,000 states per policy; mutation check (two of three injected bugs caught; the third is an
      equivalent mutant).
- [x] Property tests for shared services.
- [x] RFC draft (`designs/consolidation-pipeline.md`).

## Phase 2: simulator and harness

- [x] Design spike: in-memory (fake client) chosen over envtest.
- [x] Simulator engine, placement and scale-down models, catalog, decision log (gzipped), pod-set snapshots.
- [x] Determinism test (Gate 2).
- [x] Resumable experiment runner (`consolidation-sim matrix`).
- [x] KWOK suite compiles (`test/suites/performance/pipeline_test.go`; runs skipped: blocked hosts).
- [x] W6 trace replay (Google 2011, CC BY 4.0).

## Phase 3: strategies

- [x] C0-C3, G0-G5, P0-P4, E0-E3, pod-eviction budgets, conformance suite (registry-wide).

## Phase 4: experiments

- [x] Pilot (W1, W2 at 200 nodes, seeds 1-5): 120 cells, all valid; calibration in `experiments/PILOT.md`.
- [x] Preregistration, reviewed adversarially and finalized (Gate 3); analysis aligned and independently verified.
- [x] Session matrix (1,740 runs, 20 groups, preregistered order), Oct 27 20:52 to Oct 29 00:29 UTC: the first 16
      groups complete (1,550 runs) plus one W5 cell, all valid. `realism-w5` started on a projection that proved
      wrong by more than 30x and was cut under a time-only overrun rule. `realism-w6`, `exp7-b2-n1000` and
      `exp1-baselines-n1000` were cut with it (`experiments/session-driver/cut-decisions.txt`). Three VM
      replacements (epochs A-D), with in-flight cells relaunched. Drivers v1 to v4 (`experiments/session-driver/`).
- [x] Supplementary H7 timing pass (`experiments/matrix/h7-timing.yaml`), Oct 29 00:29-02:18 UTC on VM D: 20 cells
      plus 4 fillers, no seed split, no retry, determinism 20/20; agrees with H7 on both tests.
- [x] Exp 2 from logs (H2 on W2 and W3; W6 cut).
- [x] 10,000-state parity, all four chunks 10,000/10,000 in every configuration: `wour-kwok` (before the session, by
      hand), then under driver v4 `wour-assorted` (Oct 29 02:17-03:14 UTC), `balanced-kwok` (03:14-04:07) and
      `balanced-assorted` (04:07-05:18) (`docs/research/parity.md`).

## Phases 5-6: validation and artifacts

- [x] Tier B validation skipped (blocked hosts `registry.k8s.io`, `cgr.dev`; said so in RESULTS); figures and
      tables (`make figures`), RESULTS (final), REPRODUCE (hardware epochs, H7-S, cut groups), related work
      (drafted, every reference marked "verify"), runbook (full profile, cut groups, KWOK, EKS), full matrix.
- [x] Final push and session summary (Oct 29, after the 10,000-state parity run and four rounds of claim checks).

## After the session: Tier B on KWOK (Oct 29-31)

- [x] Network access to the image registries added by the user. Four environment workarounds are logged:
      dockerd, containerd's `restrict_oom_score_adj`, images loaded from the host, and the controller installed
      without its verify step.
- [x] Protocol (`experiments/TIERB.md`), first written before any evaluation-seed run (`eff89a1`). It defines WB,
      calibrates the window T = 30 minutes on pilot seeds, and adds `consolidation-sim workload`. Tier A ran
      `tierb-cal` and `tierb-a` at `eff89a1`. The protocol was revised after the harness review and the smokes
      (`1806e5a`, `8ef3481`), changing the validity and rerun rules, the stopping rule, the complete-block analysis
      and `cost_end_rel`. The revisions came after the 140 `tierb-a` runs (08:47-08:59 UTC) and before any
      evaluation-seed Tier B run.
- [x] KWOK spec and driver (`test/suites/performance/tierb_test.go`, `experiments/tierb/run_tierb.py`).
      - A mechanics smoke cell ran first, then an adversarial harness review that used it.
      - Three smoke cells then ran with a 47 s registration delay throughout.
      - The warmup registration delay was then set to 0 s, and four more smoke cells checked it.
- [x] 70 evaluation cells, all valid on their first counted attempt. Two attempts were interrupted by container
      restarts and set aside. Machine log: `experiments/tierb/machine-log.txt`.
- [x] Analysis (`paper/analysis/tier_b.py`): the protocol's agreement tables, plus exploratory paired
      differences. Write-up in `experiments/RESULTS.md`, "Tier B".
- [x] Environment as scripts (`experiments/tierb/env/`), runbook section 4. `setup.sh` was checked end to end
      on a fresh cluster after the run:
      - The first check found a race between the control-plane taint and kind's local-path provisioner, fixed in
        `c5a055f`.
      - The second check passed, with a valid B4 smoke cell and the same controller image digest.
- [x] Three documents for readers who know Karpenter but not this branch's jargon (`docs/consolidation/README.md`):
      the paper (`paper/paper.md`), the pipeline refactor and its trade-offs
      (`docs/consolidation/pipeline-refactor.md`), and the KWOK experiments with an EKS plan
      (`docs/consolidation/kwok-experiments-and-eks-plan.md`). Each was claim-checked against the code, tables and
      raw runs by a checker-and-verifier workflow before its final commit.

## After the paper: Exp 9 and the upstream RFC (Nov 1)

Two follow-ups to the questions the paper raised: why the random generators spend a fixed budget on blind
passes rather than selecting candidates by their utilization, and what of this work is worth proposing upstream.

- [x] `idleAttempts` on `random-merge`, `random-shift` and `greedy-shift` (`5aff11e`): a generator stops after
      that many consecutive attempts that emitted nothing; the default never stops. Tests cover the parameter, the
      prefix property against the uncapped call, and the budget and conformance checks.
- [x] Exp 9 protocol (`experiments/EXP9.md`) and matrix (`experiments/matrix/exp9.yaml`), committed before any
      cell ran: eight arms under the entropy-greedy planner (both generation references re-run, biased selection
      by potential drop and by utilization band, guided shifts, early stopping, and their combinations) on W1, W2
      and W4 at 200 nodes, seeds 101-110, and five arms on W1 at 1,000 nodes. Binary built from a clean clone
      (`experiments/exp9-binary.txt`), driver `experiments/run_exp9.sh`, machine log
      `experiments/exp9-machine-log.txt`.
- [x] Analysis module `paper/analysis/exp9.py` (registered in `make figures`, with tests): per-arm and paired
      tables with bootstrap intervals, and the search statistics from each decision log.
- [x] 200-node group: 240 of 240 cells valid. Write-up in `experiments/RESULTS.md`, "Exp 9". Early stopping cut
      attempts per searching pass by 75 to 85% and mean CPU per pass by 13 to 31% against the random reference with
      cost unresolved; biased selection added nothing on top of it; the band selector is a different operating
      point because it often has no destination on a consolidated fleet; guided shifts helped on W1 only.
- [x] 1,000-node group (50 cells, W1): all valid. Early stopping cut mean CPU per pass by 37% there (the saving
      grows with size because the early-stop arm also searches on fewer passes), and biased selection ended 2.8%
      cheaper than uniform selection with 13% fewer evictions, which it had not at 200 nodes. Write-up in
      RESULTS.md, "Exp 9"; the paper's sections 7 and 9 carry the result.
- [x] Upstream RFC `designs/consolidation-bounded-search-and-decision-log.md`: counted search budgets, a decision
      log through a recorder, and the stage seams with the coarse upstream adapters only, behind the gate, with the
      evidence scoped to what covers that configuration. Four review rounds (a disruption maintainer, an operator
      reader, a numbers checker and a code checker, each verified) found 88, 31, 18 and 6 problems, all applied;
      the entries in `experiments/RESEARCH_LOG.md` list the substantive ones.
