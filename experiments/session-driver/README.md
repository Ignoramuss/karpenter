# Session driver

The scripts that ran the session matrix in the cloud VM, kept for audit, and the records they write. They hard-code
the session's scratch paths. Decisions and their reasons are in `experiments/RESEARCH_LOG.md`.

- `run_session_v1.sh` ran from the first session cell (2026-10-27T20:52:41Z) through `exp1-n1000`. Its group list
  omitted `b4` and swapped `exp7-b2-n1000` and `exp1-baselines-n1000` relative to the preregistered order; the
  mistake was found during `exp1-n1000`, before any affected group started.
- `run_session_v2.sh` was armed to take over at the `exp1-n1000` boundary but never acted: a review found its cut
  decisions were not restart-safe, and it was stopped while still waiting.
- `run_session_v3.sh` takes over at the `exp1-n1000` boundary: the preregistered order; the time-based cut rule
  (`cut_projection.py`) applied once per cut-candidate group, its decision recorded in `cut-decisions.txt` before
  acting and followed after any restart; the VM logged in `machine-log.txt`; then the supplementary H7 timing pass
  (`experiments/matrix/h7-timing.yaml`, with `h7_pairs.py` setting aside any seed whose arms started on different
  VMs) and the remaining 10,000-state parity chunks. After a VM replacement it is restarted alone, without the pid
  argument. The running copy lives in the scratchpad, since bash reads a running script as it goes.
- `commit_runs.sh` commits finished cells, set-aside attempts, a snapshot of each matrix's `index.jsonl` (`index-<matrix>.jsonl`; the file itself at the end, with `FINAL=1`), the driver's log
  (`session.log`, copied from the scratchpad), the machine log and the cut decisions as the session goes.

`session.log` holds the driver's `START`, `DONE`, `CUT-RULE`, `SKIP` and restart lines that RESULTS quotes.
