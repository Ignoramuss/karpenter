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
