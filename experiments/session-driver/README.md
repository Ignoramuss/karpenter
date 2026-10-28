# Session driver

The scripts that ran the session matrix in the cloud VM, kept for audit. They hard-code the session's scratch paths.

- `run_session_v1.sh` ran from the first session cell (2026-10-27T20:52:41Z) through `exp1-n1000`. Its group list
  omitted `b4` and swapped `exp7-b2-n1000` and `exp1-baselines-n1000` relative to the preregistered order; the
  mistake was found during `exp1-n1000`, before any affected group started (`experiments/RESEARCH_LOG.md`).
- `run_session_v2.sh` took over at the `exp1-n1000` boundary: the preregistered order, the time-based cut rule
  (`cut_projection.py`) before each cut-candidate group, then the supplementary H7 timing pass
  (`experiments/matrix/h7-timing.yaml`) and the remaining 10,000-state parity chunks.
- `commit_runs.sh` commits finished cells (directories with a `summary.json`) as the session goes.

The driver's log lines (`START`, `DONE`, `CUT-RULE`, `SKIP`) are quoted in `experiments/RESULTS.md`.
