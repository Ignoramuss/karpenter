# Progress

Mirrors `docs/research/PLAN.md`. Updated at every milestone. After a context compaction, re-read `PLAN.md` and this
file before the next action. Decisions and fallbacks are logged in `experiments/RESEARCH_LOG.md`.

## Definition of done

- [x] Branch rebased on upstream main (fast-forward to `32fd95b`); upstream disruption tests green.
- [ ] `make verify` clean (lint on every new and changed package is clean; the full target runs before the end).
- [x] `pkg/controllers/disruption/pipeline`: interfaces, registry, adapters, strategies, conformance and parity
      tests, behind the `ConsolidationPipeline` gate, off by default.
- [x] `hack/consolidation-sim/`: the simulator, a determinism test, and the resumable experiment runner.
- [ ] `experiments/PREREGISTRATION.md`, committed before the main runs, and session-profile results for all eight
      experiments in `experiments/runs/`.
- [ ] `paper/figures/` built by `make figures` (tooling done and verified), plus `experiments/RESULTS.md`.
- [x] `docs/research/`: code map, parity report (10,000-state run to be appended), runbook; RFC draft at
      `designs/consolidation-pipeline.md`.
- [x] Branch pushed (blocked by 403 until 18:40 UTC, then restored; pushed at every milestone since).

## Self-gates

- [x] Gate 0: rebased on upstream main; `go build ./...` and the disruption tests pass.
- [x] Gate 1: 100% parity on sampled states: 1,000/1,000 per policy and catalog, coarse and fine adapters, and
      re-checked on 150 states after each later pipeline change. The 10,000-state run is pending (background chunk).
- [x] Gate 2: determinism test passes (14 cases: W1-W6, B2, B3, B5, both scale-down models, five pipeline arms).
- [x] Gate 3: `experiments/PREREGISTRATION.md` committed (`c78b5f9`) before the first session run (20:52 UTC).
- [ ] Gate 4: `make figures` rebuilds every figure and table from raw runs.

## Phase 0: rebase, environment, code map

- [x] Rebase onto upstream main (no conflicts: the fork had no commits of its own).
- [x] Toolchain: Go 1.26.6, envtest 1.36.2, Python analysis venv (`paper/requirements.txt`).
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
- [ ] Session matrix (1,740 runs, 20 groups, preregistered order): running since 20:52 UTC. Done: `exp1-n200`,
      `exp1-baselines-n200`. Running: `exp1-n1000`.
- [ ] Exp 2 from logs; 10,000-state parity (first chunk done: 10,000/10,000 x 2), resumes after the session.

## Phases 5-6: validation and artifacts

- [ ] Tier B validation (skipped: blocked hosts); figures (tooling done, `make figures`), RESULTS (after the
      session), REPRODUCE (done), related work (drafted), runbook (done), full matrix (done).
