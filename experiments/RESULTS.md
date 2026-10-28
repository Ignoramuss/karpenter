# Results: consolidation as entropy reduction (session profile)

> **Status: draft, session in progress.** The session matrix is still running (`experiments/session-driver/
> session.log`). The sections marked *to be filled* get their numbers from `paper/tables/summary.md`, which
> `make figures` regenerates from the raw runs; nothing below is final until this banner is gone.

Every number here comes from `make figures` (`paper/tables/`, `paper/figures/out/`) run over `experiments/runs/`
at the commit that removes this banner. The preregistration (`experiments/PREREGISTRATION.md`, Gate 3,
`c78b5f9`) defines each test, family and verdict rule; `experiments/RESEARCH_LOG.md` records every decision taken
after it, with its reason and when it was made relative to the data. How to rebuild everything:
`paper/REPRODUCE.md`.

## What ran

- **Tier A only**, in the in-memory simulator (`hack/consolidation-sim`), on one 4-vCPU cloud VM, up to four cells
  at a time, every cell with the Gate 3 binary (`c78b5f9`; each manifest's `binary_revision`) and 10 workload seeds
  (101-110). Tier B (KWOK on kind) could not run: its images come from `registry.k8s.io` and `cgr.dev`, which the
  session's network blocked. The KWOK suite compiles; `docs/research/runbook.md` has the commands.
- **Session matrix:** `experiments/matrix/session.yaml`, 20 groups in the preregistered order. *To be filled:* runs
  finished, cut groups (time-based rule, `experiments/session-driver/cut-decisions.txt`), invalid runs by reason
  (`paper/tables/validity.md`).
- **Supplementary H7 timing pass (H7-S):** `experiments/matrix/h7-timing.yaml`, after the matrix. *To be filled.*
- **Parity:** 1,000 states per policy and catalog for Gate 1, and a 10,000-state run (`docs/research/parity.md`).
  *To be filled:* the 10,000-state chunks.

## Verdicts

*To be filled from `paper/tables/summary.md`:* one row per hypothesis (H1-H7), the secondary families (H1-P0
contrast, H1-realism, H7-B2, H7-S), the ablation family, B4 parity and Exp 8, each with its verdict, the rule's
counts, each test's effect with its interval, and the table and figure that show it.

## Per hypothesis

*To be filled*, one subsection each, in this form: the hypothesis as preregistered; the verdict; the effects that
decide it (percent differences with 95% bootstrap or 90% TOST intervals, Holm-adjusted p); the figure; and what the
result does and does not show.

## Deviations from the preregistration

All are logged in `experiments/RESEARCH_LOG.md` with the time they were decided relative to the data.

1. **Session driver order.** The first driver omitted `b4` and ran the last two groups in the wrong order; found
   during the third group, before any affected group ran. Driver v3 runs the preregistered order
   (`experiments/session-driver/`).
2. **Two VM replacements during `exp1-n1000`** (Oct 27 23:52 and Oct 28 03:30 UTC). In-flight cells were relaunched
   from scratch; simulated outcomes do not depend on the machine. Decision-cost results carry each run's VM
   (`machine_epoch`).
3. **H7 confound and H7-S.** Exp 1 runs arm-major, so H7's heuristic arm ran on one VM beside heuristic cells only and
   its random arm on another beside random cells only. Before any random-arm 1,000-node result existed, a
   supplementary timing pass (H7-S) was declared: both arms of every seed rerun on one VM, interleaved at 4-way load.
   H7 is reported as preregistered; H7-S beside it, test by test; no difference is attributed to hardware alone.
   An early claim about the confound's direction was withdrawn: the data did not support it.
4. **Cut rule mechanized, with a fallback for W5 and W6** (no same-scenario session cells to take a median from:
   the median over same-scale cells), declared before any W5 or W6 cell ran. The rule takes medians over all arms,
   so it under-projects the B2 and B3 groups at 1,000 nodes; it is applied as written.
5. **Analysis code after Gate 3** changed only toward the preregistered text (ten alignment fixes, each with a test)
   or added descriptive output (the summary table, H7-S, wall-to-CPU ratios, machine epochs).

## Threats to validity

The preregistered threats (simulated scheduler and ReplicaSets, synthetic workloads, calibration on random arms
only, H1's power at 10 seeds) stand. Added in the session:

- **Decision cost depends on the machine and its load.** Two VM replacements put H7's arms on different VMs. Load
  also varies inside every group: each hour the runner stops launching and drains before it is re-invoked, and the
  session's own analysis and reviews ran on the VM at logged times (none during the cells H7, H7-S or Exp 7 test).
  Each run's `wall_cpu_ratio` shows the contention it saw.
- **Tier B did not run**, so no rank-agreement check bounds the simulator's scheduler and controller models.

## Next steps

*To be filled:* the full profile (`experiments/matrix/full.yaml`, `docs/research/runbook.md`), Tier B on a machine
that can reach the image registries, and whatever the cuts left out.
