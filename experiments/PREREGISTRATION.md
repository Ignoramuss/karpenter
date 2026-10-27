# Preregistration: consolidation as entropy reduction

Gate 3 of `docs/research/PLAN.md`. The **Gate 3 commit** is the commit that finalizes this file (the first in which
it carries no draft marker); its SHA is recorded in `experiments/RESEARCH_LOG.md` and in
`experiments/session-binary.txt` immediately after it, and it precedes every commit that adds results under
`experiments/runs/session/`. The git history is the evidence.

Calibration used only the pilot's seeds (1-5, `experiments/runs/pilot/`), by rules committed before the calibration
cells were inspected (`experiments/RESEARCH_LOG.md`, "Calibration rules"); values the pilot set are marked *(pilot)*,
and the pilot analysis is in `experiments/PILOT.md`. Evaluation seeds are 101-110 and were never run before the Gate
3 commit. No parameter is tuned on evaluation seeds.

**What governs.** This text governs. The analysis that implements it is `paper/analysis/` (registry
`paper/analysis/experiments.yaml`, run by `make figures`). When this file was finalized, that code was still being
aligned with the rules below after an adversarial review; the alignment and every later analysis change are
recorded in the research log, move the code toward this text, and never change a test, a family, a margin or a
decision rule stated here. Where code and text disagree, the code is wrong.

## What runs

- **Simulator:** Tier A only. Tier B (KWOK) cannot run in this session: its image registries are blocked
  (`experiments/RESEARCH_LOG.md`). One binary, built with `go build -o` from a clean tree at the Gate 3 commit, runs
  every session cell; each manifest records the binary's embedded VCS revision (`binary_revision`,
  `binary_modified`), and the analysis invalidates any session run whose revision differs from
  `experiments/session-binary.txt` or is marked modified. `TestDeterminism` is run at that commit before the first
  session cell, and its result is logged.
- **Matrix:** `experiments/matrix/session.yaml`: 1,740 runs (174 configurations x 10 seeds) in 20 groups, listed in
  the file in run order. Every comparison shares workload seeds across its arms, so differences are paired.
  Identical configurations that appear in more than one group run separately (a cross-group determinism check: their
  summaries must match).
- **World:** catalog `kwok/examples/aws_instance_types.json` as shipped, on-demand c7i, m7i and r7i up to 32 vCPU
  (`experiments/catalog.json`); one NodePool, `consolidateAfter: 30s`, disruption budget 10%; launch 2-5 s,
  registration 30-60 s, pod startup 2-10 s, shutdown 1-5 s; placement `least-allocated` and scale-down `spread`
  unless a group sets them. Horizon 3 h (1 h for `exp7-b2-n1000`), warmup 10 min before consolidation starts,
  metrics every simulated minute. No per-cell wall-time limit (`maxWall: 0s`): a 1,000-node B2 or B3 cell takes
  hours, and a limit would exclude those arms selectively.
- **Scale:** `scenario.nodes = n` sizes each workload as the demand of n mid-size nodes at 70% fill. W1 and W4 start
  with exactly n nodes; W2, W3, W5 and W6 are provisioned by Karpenter and run fewer, larger nodes. Analyses that
  plot against size use the measured node count.
- **Metrics:** steady-state cost = mean $/hr over the samples with t >= 0.8 x horizon; regret window 15 min;
  re-disruption window 30 min; middle band 20%-80% utilization; decision cost per disruption pass as process CPU
  time and wall time, both excluding the validation waits during which a pass advances the simulated world.
- **Concurrency:** up to 4 cells at a time on the VM's 4 vCPUs (fewer at a group's tail).

### Fixed pipeline settings (every pipeline arm unless its experiment says otherwise)

| Setting | Value |
| --- | --- |
| Moves mode | pooled: one round, one plan over all generated moves. With the savings-only planner (P0), which is upstream's first-valid planner, generation stops after the first generator that yields an acceptable move, as upstream does |
| Search budget | `maxFullSimulations: 16` per cycle, one pool shared by all generators; `maxAttempts: 500` per generator. The cap covers search; upstream's own validation is outside it, as in upstream. Every arm here confirms its merges during search, so no full simulation happens at execution |
| Heuristic generation (C0, G0, G1) | `upstream` candidates; `upstream-multi` and `upstream-single` in fine mode (a capped single-node scan resumes after its last evaluated candidate) |
| Random generation (C1, G2, G3) | `uniform-random` candidates (`sourceFraction: 0.25`); `random-merge` (`confirm: true`) and `random-shift` |
| Potential | `util-entropy` (P2), price-weighted, dominant resource |
| Executor | `shift-evict` (E1): merges through the orchestration queue, shifts through the Eviction API |
| Planner weights | alpha = 1, mu = 1, lambda = 0.1 *(pilot)* |
| anneal-plan | t0 = 1 *(pilot)*, cooling 0.98, 400 steps, max length 8, `temperatureUnit: per-node` |
| Pod-eviction budgets (Exp 3, ablations) | budget-lo = 18, budget-hi = 36 pods per minute per NodePool *(pilot)* |

### Experiment setups

| Experiment | Groups | Scenarios and scale | Arms |
| --- | --- | --- | --- |
| Exp 1 (H1) | `exp1-n200`, `exp1-n1000`; references `exp1-baselines-n200`, `exp1-baselines-n1000` | W1, W2, W4 at 200 and 1,000 | generation {heuristic, random} x planner {savings-only, entropy-greedy, anneal-plan}; B2 and B3 as untested references |
| Realism | `realism-w5`, `realism-w6` | W5, W6 at 200 | B2, B3, {heuristic, random} x {entropy-greedy, anneal-plan} |
| Exp 2 (H2) | the W2, W3 and W6 runs of the groups the registry lists | | from logs |
| Exp 3 (H3) | `exp3` | W2, W3 at 200 | random generation, entropy-greedy; moves {merge-only (`random-merge`), merge+shift}; budget {lo, hi} |
| Exp 4 (H4) | `exp4` | W1, W4 at 200 | random generation; entropy-greedy; anneal-plan t0 in {0.3, 1, 3} x cooling in {0.95, 0.98}; anneal-online t0 in {0.3, 1, 3}, cooling 0.99 per cycle |
| Exp 5 (H5) | `exp5`, `exp5-steered` | W2 at 200 | placement {least-allocated, most-allocated, plan-following} x scale-down {spread, deletion-cost} x arm {random/entropy-greedy, B2}; plus random/entropy-greedy with the `steered` executor (affinity mode) under least-allocated |
| Exp 6 (H6) | `exp6`, `exp6-baselines`, `exp6-b5` | W2, W3 at 200 | random/entropy-greedy at lambda in {0.05, 0.1, 0.2, 0.5, 1, 2, 5}; B0, B2, B3 under least-allocated; B0 and B5 (descheduler, threshold 0.5, every 5 min) under most-allocated |
| Exp 7 (H7) | `exp7-n50`, `exp7-b2`, `exp7-b2-n1000`, and Exp 1's W1 cells | W1 at 50, 200, 1,000 | B2, heuristic/entropy-greedy, random/entropy-greedy |
| Exp 8 | `exp8`, `exp8-b5` | W1, W2, W4 at 50 | B2, B3, B5, {heuristic, random} x {savings-only, entropy-greedy}, heuristic/anneal-plan, random/anneal-plan |
| Ablations | `abl` | W1, W2 at 200 | full (random, anneal-plan, util-entropy, budget-lo) and one component removed: no potential (alpha = 0, potential `none`), no eviction budget, no annealing (entropy-greedy), no randomness (heuristic generation) |
| B4 parity | `b4` | W1, W2, W4 at 200 | B4: the pipeline with fine upstream adapters, sequential, upstream planner, upstream-queue executor, no search caps; B4-balanced: the same under the Balanced policy |

## Statistics common to every test

Means and medians with 95% bootstrap intervals (10,000 resamples, percentile method, seed 0). Wilcoxon means the
two-sided exact signed-rank test on per-seed paired differences. Holm adjustment is applied within the families
named below, and only within them, at alpha = 0.05. With 10 pairs the smallest attainable Wilcoxon p is 2/1024, so
every Wilcoxon family below (m <= 12) can reject; each test records its attainable floor, and a clause whose test
cannot reach significance after Holm (floor x m >= 0.05, e.g. after dropped pairs) is reported as inconclusive,
never as support. Effect sizes are mean paired differences, absolute and as a percent of the reference arm's mean
(ratio of means), with bootstrap intervals.

## Hypotheses and decision rules

**H1.** With planning fixed to entropy-greedy or anneal-plan, random generation reaches the same steady-state cost
as heuristic generation at equal full-simulation budget. Family H1, m = 12 fixed: random/P vs heuristic/P for P in
{entropy-greedy, anneal-plan} x scenario {W1, W2, W4} x scale {200, 1,000}; two one-sided paired t-tests with
margin 3% of the heuristic arm's mean over the pairs. A test with fewer than 2 valid pairs is not equivalent and
enters Holm with p = 1. A test is equivalent iff its Holm-adjusted p_TOST < 0.05. Veto: any test whose unadjusted
90% interval lower bound of the mean paired difference exceeds +3% (random worse beyond the margin). **H1 is
supported iff at least 10 of the 12 tests are equivalent and no test is vetoed.** Non-inferiority (Holm-adjusted
upper one-sided test) is reported. Savings-only pairs (family H1-P0-contrast, m = 6) and the W5/W6 realism pairs
(family H1-realism, m = 4) are reported the same way and do not enter H1's verdict.

**H2.** A drop in the potential over a 10-minute window predicts node removals in the next window. Unit: the
workload seed. Runs: every valid W2, W3 and W6 run of the registry's Exp 2 groups except B0, each distinct
(configuration without label, seed) once. Per run, after warmup, over non-overlapping 10-minute windows: x =
Phi(t) - Phi(t - 10), with Phi the unnormalized potential the simulator logs (P1 `p1_load_entropy`, P2
`p2_util_entropy`, P3 `p3_quadratic`, which removing an empty node leaves unchanged); y = node removals in (t,
t + 10]; rho = Spearman(-x, y), undefined if a series is constant or there are fewer than 4 windows. Per potential,
scenario and seed: the median rho over that seed's runs with a defined rho (a seed with none counts as 0). Exact
one-sided sign test over the seeds that the per-seed median is positive. Family H2: Holm over potential x scenario
with data (m <= 9). **A potential satisfies H2 iff its test rejects after Holm and the median of per-seed medians
is positive in every scenario with data;** partially if in some. Secondary, each its own family and outside the
verdict: next-window cost decrease as the outcome (H2-cost); the normalized columns `p*_norm` (H2-norm); windows of
5 and 20 minutes (H2-w5, H2-w20). Undefined correlations are counted and reported.

**H3.** Merge plus shift beats merge-only on post-warmup integrated cost at an equal pod-eviction budget, under
churn. Family H3, m = 4: Wilcoxon per scenario {W2, W3} x budget {lo, hi}. **Supported iff all four are significant
with merge+shift lower;** partially if some. Evictions and eviction cost are reported descriptively.

**H4.** Annealing at plan time reaches cheaper states than greedy acceptance with no extra executed disruption;
annealing online pays disruption for little gain. Family H4, m = 8 Wilcoxon tests. Plan clause, per scenario {W1,
W4}: `anneal-plan/t0=1/cooling=0.98` vs entropy-greedy has significantly lower steady-state cost (2 tests) and
non-inferior evictions (bootstrap 95% upper bound of the percent difference in evictions below +10%). Online clause,
per anneal-online t0 {0.3, 1, 3} x scenario: evictions significantly higher (6 tests) and little cost gain
(bootstrap 95% lower bound of the percent difference in steady-state cost above -1%). **Supported iff the plan
clause holds in both scenarios and the online clause in all six cells;** partially otherwise. The temperature and
cooling sweep is descriptive.

**H5.** Under least-allocated placement a measurable share of evicted pods land off-plan; most-allocated placement
and deletion-cost scale-down shrink it. (a) For random/entropy-greedy under least-allocated, the bootstrap 95%
lower bound of the off-plan share (1 - fidelity) exceeds 0.05 under both scale-down models. (b) Family H5, m = 10
Wilcoxon tests on fidelity: most-allocated vs least-allocated per scale-down x arm {random/entropy-greedy, B2} (4);
deletion-cost vs spread per placement {least-, most-allocated} x arm (4); steered vs unsteered random/entropy-greedy
under least-allocated per scale-down (2). **Supported iff (a) holds and, for random/entropy-greedy, most-allocated
significantly raises fidelity in at least one scale-down stratum and deletion-cost significantly raises it in at
least one placement stratum;** partially if only one lever does. Oracle check: plan-following mean fidelity >= 0.95,
or the run set is flagged. Realized vs predicted P2 change is descriptive.

**H6.** Sweeping lambda traces a savings vs disruption frontier that matches or dominates B2, B3 and B5. The matched
lambda per baseline is fixed in advance, not chosen from results: B2 with lambda = 0.05 (it accepts any saving),
B3 with lambda = 0.5 (Balanced at k = 2 is dE <= 0 with lambda/mu = 1/2), B5 with lambda = 0.1 (the calibrated
default). Savings = post-warmup integrated cost of B0 on the same seed, scenario and placement (most-allocated B0 for
B5) minus the run's. Family H6, m = 12: Wilcoxon on savings and on eviction cost, per baseline x scenario {W2, W3}.
A (baseline, scenario) cell **dominates** iff one metric is significantly better and the other not significantly
worse, is **dominated** iff a metric is significantly worse and none significantly better, and is inconclusive
otherwise. **Supported iff no cell is dominated and at least 3 of the 6 dominate;** not supported if any is
dominated; inconclusive otherwise. The full frontier and the empirical Pareto set are descriptive.

**H7.** Random sampling with fast feasibility has lower decision cost than prefix binary search at 1,000 nodes, and
no timeouts. Family H7, m = 2: at 1,000 nodes (W1, Exp 1's 3-hour runs), random/entropy-greedy vs
heuristic/entropy-greedy on CPU time per pass, mean and p95. Timeout clause: no valid random/entropy-greedy run at
1,000 nodes has a pass longer than 1 minute of wall time (upstream's multi-node limit; the simulator's injected
clock never fires upstream's timeouts itself). **Supported iff both tests are significant with random lower and the
timeout clause holds.** Secondary family H7-B2, m = 2: random vs B2 (`exp7-b2-n1000`, 1-hour runs, weighted to the
busy early phase) on the same metrics; absent if that group is cut, and never part of the verdict. Wall-time
latency, full simulations per pass and latency against the measured node count are descriptive.

**Exp 8** (descriptive): final-snapshot cost over the offline packing lower bound (B6), minus one, per configuration
and scenario, with the CP-SAT bound at 50 nodes and the LP relaxation bound at 200 and 1,000.

**Ablations** (figure 8): family ablation, m = 8: Wilcoxon of each removed component vs `full` on steady-state cost,
per scenario {W1, W2}. Other metrics descriptive.

**B4 parity in the wild:** B4 vs B2 and B4-balanced vs B3, same seeds. Primary: the number of seeds whose summaries
match exactly (steady-state cost, evictions, nodes removed, final nodes). Secondary family B4, m = 6: TOST at the 3%
margin on steady-state cost per scenario and pair. B4 is **exact** if every seed matches, **equivalent** if all six
TOSTs pass after Holm, otherwise **provisional**.

## Exclusion, completeness and undefined values

A run is **invalid**, not a data point, if any of these hold; invalid runs are counted per experiment by reason and
never dropped silently:

- a PodDisruptionBudget or `karpenter.sh/do-not-disrupt` violation (`pdb_violations` or
  `do_not_disrupt_violations` above 0);
- the run errored or crashed (`error` set), or failed twice (the runner marks it abandoned). A cell that fails once
  and succeeds on retry is valid; set-aside attempts are counted and reported;
- it timed out (`timed_out`; cannot occur, since no per-cell limit is set);
- it was produced by a binary other than the Gate 3 build (`binary_revision`, `binary_modified`);
- nondeterminism: `TestDeterminism` fails at the Gate 3 commit (then every session run is invalid).

A paired comparison drops a seed when any arm in it is invalid for that seed and reports how many it dropped. A
configuration with fewer than 10 finished runs (for example in a group cut part-way) is cut: excluded from every
test and listed. A valid run whose metric is undefined (for example fidelity when no evicted pod was tracked) is
left out of that metric's tests and counted separately as undefined. No run is excluded for its results.

## Run order, stopping and cuts

Groups run to completion one at a time, in the order of `experiments/matrix/session.yaml`: `exp1-n200`,
`exp1-baselines-n200`, `exp1-n1000`, `exp6`, `exp6-baselines`, `exp6-b5`, `exp5`, `exp5-steered`, `exp3`, `exp4`,
`exp7-n50`, `exp7-b2`, `abl`, `exp8`, `exp8-b5`, `b4`, `realism-w5`, `realism-w6`, `exp7-b2-n1000`,
`exp1-baselines-n1000`. Exp 2 is computed from the logs. Cuts are decided by time only, never by results: a group
among the cut candidates (`realism-w5`, `realism-w6`, `exp7-b2-n1000`, `exp1-baselines-n1000`) starts only if its
projected run time (cells x the median wall time of finished session cells of the same scenario and scale, divided
by 4) ends within 30 hours of the first session cell's start; otherwise it and every later candidate are cut whole.
The plan's cut order (1,000-node cells outside Exp 1, then W6, then W5) is the reverse of their run order. Exp 1's
own groups (`exp1-n200`, `exp1-n1000`) are never cut; `exp1-baselines-n1000` holds untested reference rows and is
not an Exp 1 cell for this rule. Whatever is cut is listed in `experiments/RESULTS.md`.

## Threats acknowledged in advance

- Tier A models kube-scheduler and ReplicaSet behavior; Tier B, which would bound that, cannot run here.
- Synthetic workloads may flatter random generation; W5 and W6 are the realism checks, and Exp 1 reports early
  (first hour after warmup) and late (last 20%) phases separately.
- lambda and t0 were calibrated on random-generation arms only, which can favor random generation in H1; the same
  values apply to both arms. t0 = 1 was calibrated at lambda = 0.5 and runs at lambda = 0.1, a pair the pilot did
  not run. Cooling, the regret window and the search budget were not calibrated.
- Pilot power for H1 (Holm over 12, pilot variances, W4 and 1,000-node variance assumed equal to W1 and W2 at 200):
  P(at least 10 of 12 equivalent) is about 0.93 if the true difference is 0, 0.49 at +1% and near 0 at +2% or more.
  The pilot measured random generation 3-5% dearer than heuristic generation on W1 under entropy-greedy and
  anneal-plan at the uncalibrated lambda = 0.5 (`experiments/PILOT.md`).
- A bimodal fleet leaves little headroom: pending-pod time and mean time to schedule are reported per arm.
- Prices are on-demand only; EvictionCost stands in for restart cost.
- Decision cost (H7) is measured on this 4-vCPU VM with up to 4 concurrent cells; CPU time per pass is the primary
  measure because it is less sensitive than wall time to co-running cells.
