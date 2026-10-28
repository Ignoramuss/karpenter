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

Each subsection gives the hypothesis as preregistered, the verdict, the effects that decide it and what the result
does and does not show. Percent differences are random minus heuristic (or arm minus reference) over the reference's
mean; "90% CI" is the TOST interval, "95% CI" a paired bootstrap interval (10,000 resamples). H2-H6 and the other
checks are *to be filled* when their groups finish.

### H1. Random generation vs heuristic generation, entropy-guided planners (Exp 1; complete)

**Hypothesis.** With planning (stage 3) fixed to entropy-greedy or anneal-plan, and the executor (stage 4) fixed to
shift-evict in every arm, random candidates and moves (uniform-random candidates, random merge and shift) reach
steady-state cost within 3% of heuristic generation (upstream's candidate order with the multi-node prefix search
and single-node scan) at an equal full-simulation budget per pass. Twelve paired TOSTs (2 planners x W1, W2, W4 x
200 and 1,000 nodes, 10 seeds), Holm over the 12; supported iff at least 10 are equivalent and none is vetoed.

**Verdict: not supported.** 3 of 12 equivalent after Holm, none vetoed, 9 of 12 non-inferior
(`paper/tables/exp1_h1.md`). Equivalence fails in both directions:

| Steady-state $/hr, random vs heuristic | W1 (fragmented start) | W2 (diurnal) | W4 (mixed shapes) |
| --- | --- | --- | --- |
| entropy-greedy, 200 nodes | -1.7% (90% CI -3.2, -0.2) | +4.3% (+2.1, +6.5) | -4.1% (-5.3, -2.9) |
| anneal-plan, 200 nodes | -4.3% (-5.8, -2.7) | +2.5% (+1.1, +4.0) | -5.1% (-6.3, -3.8), lower beyond the margin |
| entropy-greedy, 1,000 nodes | -2.4% (-3.1, -1.7) | +1.4% (+0.6, +2.3), equivalent | -3.9% (-4.8, -2.9) |
| anneal-plan, 1,000 nodes | +0.1% (-0.8, +1.0), equivalent | +2.6% (+2.1, +3.1) | -1.5% (-2.1, -0.9), equivalent |

On the static workloads (W1, W4) random generation ends 1.5-5.1% cheaper than heuristic generation in 7 of the 8
cells (W1 1,000-node anneal-plan: +0.1%, equivalent); 4 of the 8 point estimates are beyond the 3% margin, one (W4
200-node anneal-plan) with its whole interval. On the diurnal workload (W2) it ends 1.4-4.3% more expensive. Under
savings-only planning (the P0 contrast, not part of H1) the split is starker: random is 6.6-10.7% cheaper on W1 and
W4 and 2.6% more expensive on W2 at 1,000 nodes; none of the six is equivalent.

**What it costs** (descriptive, `paper/tables/exp1_secondary.md`, `exp1_phases.md`). The cheaper end states are not
free. With entropy-guided planners, random generation evicted 48-129% more pods on W1 and W4 and 26-41% more on W2
at 1,000 nodes (W2 at 200 nodes: -5% to +13%, intervals spanning zero), and removed 16-54% more nodes on W1 and W4
(9-28% fewer on W2). It converges more slowly: in the first hour after warmup its fleet cost is higher (W1 and W4:
+7% to +54% in 7 of 8 cells, W4 1,000-node entropy-greedy +1.8% with an interval spanning zero; W2: +0.4% to
+2.0%), so its cost integrated over the run is higher in 10 of the 12 cells (+1.7% to
+24.5%; W1 1,000-node entropy-greedy +0.3% with an interval spanning zero, W4 1,000-node entropy-greedy -1.3%). It
ran 21-58% fewer full simulations per pass and left fewer pod-seconds pending.

**What it shows.** Under this simulator and these budgets, random generation is not a drop-in equivalent of
upstream's heuristic search: it trades disruption and slower convergence for a lower final cost on fragmented and
mixed-shape fleets in most cells, and does worse after a diurnal scale-down. The test answers the preregistered
question (within 3% or not); it does not say which arm is preferable, which depends on how evictions are priced (Exp
6 measures that trade-off). Tier B did not run, so whether real kube-scheduler placement preserves the ranking is
untested.

### H6. Savings vs disruption against upstream and the descheduler (Exp 6; complete)

**Hypothesis.** Sweeping lambda traces a savings vs disruption frontier that matches or dominates B2, B3 and B5. Per
baseline and
workload (W2 diurnal, W3 batch churn, 200 nodes), random/entropy-greedy at the lambda fixed in advance for that
baseline (B2: 0.05, since it accepts any saving; B3: 0.5, since Balanced at k = 2 is dE <= 0 with lambda/mu = 1/2;
B5: 0.1, the calibrated default) vs the baseline on savings (B0's integrated cost after warmup on the same seed and
placement minus the run's, most-allocated B0 for B5) and on eviction cost, paired exact Wilcoxon, Holm over the 12.
A cell dominates iff one metric is significantly better and the other not significantly worse, and is dominated iff
a metric is significantly worse and none significantly better. Supported iff no cell is dominated and at least 3 of
6 dominate.

**Verdict: not supported:** 3 cells dominated, 1 dominates, 2 inconclusive (`paper/tables/exp6_dominance.md`,
figure `fig3_frontier`).

| Matched point vs baseline | Savings | Eviction cost | Cell |
| --- | --- | --- | --- |
| lambda 0.05 vs B2, W2 | -5.2% (95% CI -6.0, -4.4) | -26.2% (-31.6, -20.7) | inconclusive (less saved, fewer evictions) |
| lambda 0.05 vs B2, W3 | -30.8% (-38.4, -23.8) | +41.8% (+16.6, +72.7) | dominated |
| lambda 0.5 vs B3, W2 | -8.0% (-10.0, -6.2) | +13.4% (+5.1, +22.8) | dominated |
| lambda 0.5 vs B3, W3 | -22.2% (-34.0, -10.8) | +86.4% (+45.8, +146.7) | dominated |
| lambda 0.1 vs B5, W2 | +74.6% (+58.4, +94.5) | -10.1% (-25.6, +12.5), n.s. | dominates |
| lambda 0.1 vs B5, W3 | +205.6% (+120.5, +328.6) | +529.6% (+299.3, +1128.5) | inconclusive (more saved, far more evictions) |

**The frontier** (`paper/tables/exp6_frontier.md`, means over 10 seeds). Raising lambda trades evictions for
savings: on W2 the pipeline saves $60.6-61.9 at lambda 0.05-2 while eviction cost falls from 1,182 to 748, then
$49.5 at lambda 5 with 375; on W3 it saves $18.1-19.2 at lambda 0.05-1 (eviction cost 3,033 to 2,012) and $13.0 and
$4.4 at lambda 2 and 5 (918 and 90). Only lambda 2 and 5 are Pareto-optimal; on W2 so is B3 ($66.9 saved at 822),
which saves more than any pipeline point. On W3 the upstream methods save more than any pipeline point (B2 $26.2 at
2,138; B3 $24.1 at 1,441), and B5 ($6.0 at 478) is also on the frontier.

**What it shows.** The matched points come from the baselines' own acceptance rules, not from results. Upstream
Balanced consolidation (B3) is the stronger operating point on both workloads; the pipeline only
reaches the frontier at high lambda, where it evicts least. The descheduler baseline (B5, HighNodeUtilization under
most-allocated scheduling) saves much less than either on W2. W3's batch churn is where the pipeline does worst
relative to upstream.

### H7. Decision cost at 1,000 nodes (Exp 7; primary family complete)

**Hypothesis.** Random sampling with fast feasibility has lower decision cost than prefix binary search at 1,000
nodes, and no timeouts: random/entropy-greedy vs heuristic/entropy-greedy on W1 at 1,000 nodes (Exp 1's 3-hour
runs), process CPU time per disruption pass, mean and p95, paired by seed, exact Wilcoxon, Holm over 2; and no valid
random/entropy-greedy run at 1,000 nodes (W1, W2, W4) with a pass over upstream's 1-minute limit.

**Verdict: not supported** (`paper/tables/exp7_tests.md`). Mean CPU time per pass is 37.2% lower for random
generation (95% CI -41.2 to -33.2; 895 vs 1,425 ms; lower in all 10 seeds, Holm p = 0.004), but the p95 is 19.3%
higher (95% CI +14.3 to +23.4; 2,726 vs 2,285 ms; higher in all 10 seeds, Holm p = 0.004). The timeout clause holds:
none of the 30 random/entropy-greedy 1,000-node runs had a pass over 1 minute (the slowest took 9.5 s). Random
generation ran 32% fewer full simulations per pass (7.5 vs 10.9). At 200 nodes (descriptive) the pattern is the
same: mean 98 vs 143 ms, p95 392 vs 324 ms.

**Caveat: the two arms ran on different VMs.** The session VM was replaced twice during Exp 1, which runs arm-major:
all ten heuristic cells ran on VM B and all ten random cells on VM C (`machine_epoch` in
`paper/tables/exp7_latency.md`), each beside up to three other cells of its own generation (heuristic or random; for
the first seeds partly savings-only cells), never beside a cell of the other generation. Both arms saw little
contention (wall over CPU time 1.004 each). The VMs' CPU labels differ (2.80 vs 2.10 GHz), but per unit of work the
session's own data show no consistent speed difference (`experiments/RESEARCH_LOG.md`, "Correction: the H7
confound's direction"). A supplementary timing pass, H7-S (`experiments/matrix/h7-timing.yaml`, declared before any
random-arm 1,000-node result), reruns both arms of every seed on one VM, interleaved at 4-way load, after the
matrix; *to be filled* with its result, its twin CPU ratios and its test-by-test comparison with H7.

**What it shows.** Random sampling makes the typical pass cheaper and the slow passes slower: its cost per pass is
more variable, so a fixed per-pass time limit binds on the tail, not the mean. Neither arm comes close to upstream's
1-minute limit at 1,000 nodes in this simulator.

## Deviations from the preregistration

All are logged in `experiments/RESEARCH_LOG.md` with the time they were decided relative to the data.

1. **Session driver order.** The first driver omitted `b4` and ran the last two groups in the wrong order; found
   during the third group, before any affected group ran. Driver v3 runs the preregistered order
   (`experiments/session-driver/`).
2. **Two VM replacements during `exp1-n1000`** (Oct 27 23:52 and Oct 28 03:30 UTC). In-flight cells were relaunched
   from scratch; simulated outcomes do not depend on the machine. Decision-cost results carry each run's VM
   (`machine_epoch`).
3. **H7 confound and H7-S.** Exp 1 runs arm-major, so H7's heuristic arm ran on one VM beside heuristic cells only
   and its random arm on another beside random cells only. Before any random-arm 1,000-node result existed, a
   supplementary timing pass (H7-S) was declared: both arms of every seed rerun on one VM, interleaved at 4-way
   load. H7 is reported as preregistered; H7-S beside it, test by test; no difference is attributed to hardware
   alone. An early claim about the confound's direction was withdrawn: the data did not support it.
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
