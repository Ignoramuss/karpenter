# Preregistration: consolidation as entropy reduction

**Status: draft.** Values marked *(pilot)* are placeholders until the pilot finishes; the final version replaces
this line, and that commit, not this one, is the Gate 3 commit that must precede every session run.

Committed before any session-profile run (Gate 3, `docs/research/PLAN.md`). The commit that adds this file precedes
every commit that adds results under `experiments/runs/session/`; the git history is the evidence. Calibration used
only the pilot's seeds (1-5, `experiments/runs/pilot/`); every number below that the pilot set is marked *(pilot)*,
and the pilot analysis is in `experiments/PILOT.md`. Evaluation seeds are 101-110 and were never run before this
commit.

## What runs

- **Simulator:** Tier A, `hack/consolidation-sim` at the commit that adds this file (each run's manifest records its
  SHA). Tier B (KWOK) is not run in this session: its image registries are blocked (`experiments/RESEARCH_LOG.md`).
- **Matrix:** `experiments/matrix/session.yaml`, 1,510 cells in 17 groups, 10 seeds per cell. Every comparison
  shares workload seeds across its arms, so differences are paired.
- **World:** catalog `kwok/examples/aws_instance_types.json` as shipped, on-demand c7i, m7i and r7i up to 32 vCPU
  (`experiments/catalog.json`); one NodePool, `consolidateAfter: 30s`, disruption budget 10%; launch 2-5 s,
  registration 30-60 s, pod startup 2-10 s, shutdown 1-5 s; placement `least-allocated` and scale-down `spread`
  unless a group varies them. Horizon 3 h (1 h for `exp7-b2-n1000`, which measures latency only), warmup 10 min
  before consolidation starts, metrics every simulated minute.
- **Scale:** `scenario.nodes = n` sizes each workload as the demand of n mid-size nodes at 70% fill. W1 and W4 start
  with exactly n nodes; W2, W3 and W5 are provisioned by Karpenter and run fewer, larger nodes. Analyses that plot
  against size use the measured node count.

### Fixed pipeline settings (all pipeline arms unless an experiment varies them)

| Setting | Value |
| --- | --- |
| Moves mode | pooled: every generator in one round, one plan over all moves |
| Search budget | `maxFullSimulations: 16` per cycle (shared by all generators and execution), `maxAttempts: 500` per generator |
| Heuristic generation (C0, G0, G1) | `upstream` candidates; `upstream-multi` and `upstream-single`, fine mode |
| Random generation (C1, G2, G3) | `uniform-random` candidates (`sourceFraction: 0.25`); `random-merge` (`confirm: true`, so every merge is confirmed by Karpenter's scheduling simulation within the budget) and `random-shift` |
| Potential | `util-entropy` (P2), price-weighted, dominant resource |
| Executor | `shift-evict` (E1): merges through the orchestration queue, shifts through the Eviction API |
| Planner weights | alpha = 1, mu = 1, lambda = LAMBDA *(pilot)* |
| anneal-plan | t0 = T0 *(pilot)*, cooling 0.98, 400 steps, max length 8, `temperatureUnit: per-node` |
| Pod-eviction budgets (Exp 3, ablations) | budget-lo = BUDGET_LO and budget-hi = BUDGET_HI pods per minute per NodePool *(pilot)* |

## Hypotheses, primary metrics and tests

Statistics for every experiment: means and medians with 95% bootstrap intervals (10,000 resamples, percentile,
seed 0); paired Wilcoxon signed-rank tests on per-seed differences; Holm correction within each experiment's family
of tests, at alpha = 0.05. Effect sizes are reported as mean paired differences, absolute and as a percent of the
reference arm's mean, with intervals. Steady-state cost is the mean of the per-minute cost ($/hr) over the last 20%
of the horizon.

| ID | Prediction | Primary metric | Test and decision rule |
| --- | --- | --- | --- |
| H1 | With planning fixed to entropy-greedy (P1) or anneal-plan (P2), random generation (C1+G2+G3) reaches the same steady-state cost as heuristic generation (C0+G0+G1) at equal full-simulation budget | Steady-state $/hr | Two one-sided paired t-tests (TOST) per scenario (W1, W2, W4), scale (200, 1,000) and planner (P1, P2): equivalent iff the 90% CI of the mean paired difference lies inside +-3% of the heuristic arm's mean. Holm over these 12 tests. H1 holds if at least 10 of 12 are equivalent after Holm and none shows random worse by more than the margin (lower CI bound above +3%). Secondary: non-inferiority (upper 90% bound below +3%). The savings-only (P0) pairs are a reported contrast, in their own Holm family. |
| H2 | A drop in the potential over a 10-minute window predicts node removals and cost decrease in the next window | Spearman correlation of -dPhi with next-window removals, per run | For each potential (P1, P2, P3) on W2 and W3 runs: median per-run correlation with bootstrap CI, and a sign test that the share of positive correlations exceeds one half. H2 holds for a potential if the sign test rejects (Holm over potentials x scenarios) and the median is positive. Windows are non-overlapping; 5 and 20 minutes are sensitivity analyses. |
| H3 | Merge plus shift beats merge-only on integrated cost at an equal pod-eviction budget, under churn | Post-warmup integrated cost ($) | Paired Wilcoxon per scenario (W2, W3) and budget (lo, hi), Holm over the four; H3 holds where merge+shift is lower and significant. |
| H4 | Annealing at plan time reaches cheaper states than greedy acceptance with no extra executed disruption; annealing online pays disruption for little gain | Steady-state $/hr and total evictions | anneal-plan at the preregistered default (t0 = T0, cooling 0.98, the same config Exp 1 uses; label `anneal-plan/t0=T0/cooling=0.98`) vs entropy-greedy: cost lower or equal (Wilcoxon) and evictions not higher. Each anneal-online config vs entropy-greedy: evictions higher and cost not significantly lower. Holm within Exp 4. The full sweep is reported descriptively. |
| H5 | Under least-allocated placement a measurable share of evicted pods land off-plan; most-allocated placement and deletion-cost scale-down shrink it | Plan fidelity (share of evicted pods whose replacement lands on the planned node) | Paired Wilcoxon: most-allocated vs least-allocated, per scale-down model and arm; deletion-cost vs spread, per placement and arm; steered (E2) vs unsteered under least-allocated. Holm within Exp 5. Plan-following is the oracle check (fidelity near 1). Realized vs predicted P2 change is reported per cell. |
| H6 | Sweeping lambda traces a savings vs disruption frontier that matches or dominates B2, B3 and B5 | Savings vs B0 on the same seed ($) against eviction cost | For each baseline and scenario (W2, W3): the lambda point with the highest mean savings among those with mean eviction cost at or below the baseline's; paired differences in savings and eviction cost, Wilcoxon, Holm within Exp 6. "Dominates" if savings are significantly higher or disruption significantly lower with neither significantly worse; "matches" if neither differs; otherwise "dominated". |
| H7 | Random sampling with fast feasibility has lower decision latency than prefix binary search at 1,000 nodes, and no timeouts | Wall time per disruption pass (mean and p95) | At 1,000 nodes (W1): random/entropy-greedy vs heuristic/entropy-greedy and vs B2, paired Wilcoxon on mean and p95 pass latency, Holm within Exp 7. Timeouts: passes whose wall time exceeds upstream's 1-minute and 3-minute limits (the simulator's injected clock never fires them itself). |
| Exp 8 | (descriptive) | Final-snapshot cost over the offline packing lower bound (B6), minus one | CP-SAT bound at 50 nodes, LP relaxation bound at 200 and 1,000; means with CIs per configuration and scenario. |

The ablation table (figure 8) compares `full` (random generation, anneal-plan, util-entropy, budget-lo) with one
component removed at a time: no potential (alpha = 0, potential `none`), no eviction budget, no annealing
(entropy-greedy), no randomness (heuristic generation). Paired Wilcoxon against `full` on steady-state cost,
integrated cost and evictions, Holm within the ablation family.

## Exclusion rules

A run is **invalid**, not a data point, if any of these hold; invalid runs are counted and reported per experiment,
never dropped silently:

- a PodDisruptionBudget or `karpenter.sh/do-not-disrupt` violation (`invalid: true` in the summary);
- the run errored or crashed (`error` set, or no `summary.json` after two attempts: the runner marks it abandoned);
- it hit its wall-time limit (`timed_out: true`);
- nondeterminism: the determinism test (`TestDeterminism`) fails at the run's commit.

A paired comparison drops a seed when any arm in it is invalid for that seed, and reports how many seeds it dropped.
No run is excluded for its results.

## Run order and stopping

Groups run in this order: Exp 1 (`exp1-n200`, `exp1-baselines-n200`, `exp1-n1000`), Exp 6, Exp 5, Exp 3, Exp 4,
Exp 7 (`exp7-n50`, `exp7-b2`), ablations, Exp 8, then the 1,000-node reference groups (`exp1-baselines-n1000`,
`exp7-b2-n1000`). Exp 2 is computed from the logs of the W2 and W3 runs above; W6 (trace replay) runs only if time
remains. If the session cannot finish, groups are cut in the plan's order: 1,000-node cells outside Exp 1 first, then
W6, then W5. Exp 1 cells are never cut, and no cell runs with fewer than 10 seeds. Whatever is cut is listed in
`experiments/RESULTS.md`.

## Threats acknowledged in advance

Tier A models kube-scheduler and ReplicaSet behavior; Tier B, which would bound that, cannot run here. Synthetic
workloads may flatter random generation; W5 and W6 are the realism checks, and Exp 1 reports early (first hour after
warmup) and late (last 20%) phases separately. Prices are on-demand only. EvictionCost stands in for restart cost.
Wall-time latencies (H7) depend on this VM (4 vCPUs) and on the other cells running alongside; every cell runs with
the same number of concurrent workers.
