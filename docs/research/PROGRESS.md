# Progress

Mirrors `docs/research/PLAN.md`. Updated at every milestone. After a context compaction, re-read `PLAN.md` and this
file before the next action. Decisions and fallbacks are logged in `experiments/RESEARCH_LOG.md`.

## Definition of done

- [x] Branch rebased on upstream main (fast-forward to `32fd95b`); upstream disruption tests green.
- [ ] `make verify` clean.
- [ ] `pkg/controllers/disruption/pipeline`: interfaces, registry, adapters, strategies, conformance and parity
      tests, behind the `ConsolidationPipeline` gate, off by default.
- [ ] `hack/consolidation-sim/`: the simulator, a determinism test, and the resumable experiment runner.
- [ ] `experiments/PREREGISTRATION.md`, committed before the main runs, and session-profile results for all eight
      experiments in `experiments/runs/`.
- [ ] `paper/figures/` built by `make figures`, plus `experiments/RESULTS.md`.
- [ ] `docs/research/`: code map, parity report, and a runbook for the full profile and KWOK; RFC draft at
      `designs/consolidation-pipeline.md`.

## Self-gates

- [x] Gate 0: rebased on upstream main; `go build ./...` and the disruption tests pass.
- [ ] Gate 1: 100% parity on sampled states (1,000 per policy before each pipeline commit; 10,000 once).
- [ ] Gate 2: determinism test passes.
- [ ] Gate 3: `experiments/PREREGISTRATION.md` committed before the main runs.
- [ ] Gate 4: `make figures` rebuilds every figure and table from raw runs.

## Phase 0: rebase, environment, code map

- [x] Rebase onto upstream main (no conflicts: the fork had no commits of its own).
- [x] Toolchain: Go 1.26.6, envtest 1.36.2, Python analysis venv.
- [x] Baseline: `go build ./...` 93 s; `go test ./pkg/controllers/disruption/... -race` green, 400 s.
- [x] Code map with seams, nondeterminism sources, time limits and measurement assets (`docs/research/code-map.md`).
- [x] Tier B blocked hosts recorded (`registry.k8s.io`, `cgr.dev`).

## Phase 1: decomposition and parity

- [x] Interfaces, registry, Snapshot, Move, Recorder, config loader.
- [x] `ConsolidationPipeline` alpha gate (off) and `--consolidation-pipeline-config`; registration replaces
      multi-node and single-node consolidation only when the gate is on.
- [x] Upstream adapters, coarse (wrap ComputeCommands) and fine (stage-split), plus `Command` to `Move`
      conversion; the inverse is Full feasibility, which rebuilds the upstream command by simulation.
- [x] Differential parity harness (`harness.GenerateState`, `UpstreamDecision`, `PipelineDecision`).
- [ ] Parity at 1,000 states per policy; mutation check that the test catches injected adapter bugs.
- [ ] Property tests for shared services.
- [ ] RFC draft.

## Phase 2: simulator and harness

- [x] Design spike: in-memory (fake client) chosen over envtest; numbers in the research log.
- [ ] Simulator engine, placement and scale-down models, catalog, decision log.
- [ ] Determinism test (Gate 2).
- [ ] Resumable experiment runner.
- [ ] KWOK suite compiles (runs skipped: blocked hosts).

## Phase 3: strategies

- [ ] C0-C3, G0-G5, P0-P4, E0-E3, eviction budgets, conformance suite.

## Phase 4: experiments

- [ ] Pilot, preregistration, Exp 1, 6, 5, 3, 4, 7, 8, then Exp 2 from logs.

## Phases 5-6: validation and artifacts

- [ ] Tier B validation (skipped unless hosts are allowed), figures, RESULTS, REPRODUCE, related work, runbook,
      full matrix.
