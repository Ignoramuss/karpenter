# Results: consolidation as entropy reduction (session profile)

> **Status: final for the session matrix and the H7 timing pass** (Oct 29, 2026). The two Balanced 10,000-state
> parity chunks run after the session; `docs/research/parity.md` reports each chunk as it finishes. Every verdict and
> effect below comes from `paper/tables/summary.md` and the tables it points to, which `make figures` regenerates from
> the raw runs.

Every verdict, effect and run count here comes from `make figures` (`paper/tables/`, `paper/figures/out/`) run over
`experiments/runs/` (two runs over the same data are byte-identical). A few descriptive numbers that no table holds
come straight from the runs' own files: the W1/W4 mean CPU utilization at t = 0 (`timeseries.csv`), the W2, W3 and W5
peak node counts (`summary.json`, `peak_nodes`) and the slowest single passes (`summary.json`, `wall_pass_max_ms`).
The run history (session and H7-S times, VM epochs, cut projections) comes from `experiments/session-driver/`
(`session.log`, `machine-log.txt`, `cut-decisions.txt`), and the decision-level parity counts from
`experiments/parity/` (`docs/research/parity.md`). The preregistration (`experiments/PREREGISTRATION.md`, Gate 3,
`c78b5f9`) defines each test, family and verdict rule; `experiments/RESEARCH_LOG.md` records every decision taken
after it, with its reason and when it was made relative to the data. How to rebuild everything: `paper/REPRODUCE.md`.

## What ran

- **Scale.** "n nodes" is `scenario.nodes = n`. W1 and W4 start with exactly n nodes of random mid-size types, each
  filled to a random 15-75% (mean CPU utilization 0.39-0.44 at t = 0 for n = 200). W2, W3, W5 and W6 are sized as
  the demand of n mid-size nodes at 70% fill and provisioned by Karpenter, so their node counts differ from n. W2
  runs far fewer, larger nodes (in Exp 6 at n = 200 it peaked at 42-50). W3's job churn takes it above n at peak
  (124-339 in Exp 6, above 200 in 73 of 120 cells). The single W5 cell peaked at 255, and W6 did not run. The
  preregistration's wording ("each workload ... at 70% fill", "fewer, larger nodes") describes the code only for
  W2 (deviation 9). Analyses that plot against size use the measured node count.
- **Tier A only**, in the in-memory simulator (`hack/consolidation-sim`), on one 4-vCPU cloud VM at a time (four in
  turn, below), up to four cells at a time, every cell with the Gate 3 binary (`c78b5f9`; each manifest's
  `binary_revision`) and 10 workload seeds (101-110). Tier B (KWOK on kind) could not run: its images come from
  `registry.k8s.io` and `cgr.dev`, which the session's network blocked. The KWOK suite compiles;
  `docs/research/runbook.md` has the commands.
- **Session matrix:** `experiments/matrix/session.yaml`, 20 groups in the preregistered order, from Oct 27 20:52 to
  Oct 29 00:29 UTC. 1,551 of the 1,740 preregistered runs finished, every one valid: the first 16 groups complete
  (1,550 runs) and one W5 cell. The four cut-candidate groups (`realism-w5`, `realism-w6`, `exp7-b2-n1000`,
  `exp1-baselines-n1000`: 190 cells, 189 of them unfinished, including four in-flight W5 B2 cells that were stopped
  mid-run and discarded) were cut on time alone (`experiments/session-driver/cut-decisions.txt`). `realism-w5` passed
  the preregistered start check on a projection that turned out to be wrong by more than 30x. It was then stopped
  under a time-only overrun rule declared in the session, and that rule, not the preregistered one, cut the other
  three with it (deviation 7 below). The session ran on four VMs in turn (epochs A-D,
  `experiments/session-driver/machine-log.txt`); the three replacements killed in-flight cells, which were relaunched
  from scratch.
- **Supplementary H7 timing pass (H7-S):** `experiments/matrix/h7-timing.yaml`, Oct 29 00:29-02:18 UTC on VM D: 20
  measured cells (both arms of H7's ten seeds, interleaved at 4-way load) and 4 discarded fillers; no seed's arms were
  split across VMs and no attempt was retried.
- **Parity:** 1,000 states per policy and catalog for Gate 1 (all matched), and a 10,000-state run in four chunks
  (`experiments/parity/chunks/`). Both WhenEmptyOrUnderutilized chunks (kwok before the session, assorted after it)
  matched 10,000 of 10,000 states in both adapter modes; the two Balanced chunks run after the session
  (`docs/research/parity.md`).

## Verdicts

From `paper/tables/summary.md` (each item's verdict and rule counts, intervals for H1, H1-P0, H3, H4, H7, H7-S and the
ablations, and the tables and figures behind each) and the per-experiment tables it points to, which give the other
intervals and all p values (H1 `exp1_h1`, H2 `exp2_correlation`, H3 `exp3_h3`, H4 `exp4_h4`, H5 `exp5_tests`, H6
`exp6_dominance`, H7 `exp7_tests`, H7-S `h7s_tests` and `h7s_runs`, ablations `fig8_ablation`, B4 `b4_parity`):

| Item | Verdict | Deciding effects |
| --- | --- | --- |
| H1: random vs heuristic generation within 3% | not supported | 3 of 12 equivalent; random 1.5-5.1% cheaper in 7 of 8 W1/W4 cells, 1.4-4.3% dearer on W2 |
| H1-P0 contrast (savings-only) | reported | 0 of 6 equivalent |
| H1-realism (W5, W6) | no runs (groups cut) | |
| H2: potential drop predicts removals | P1 partial; P2, P3 not supported | P1 on W2 10 of 10 seeds (Holm p 0.006), W3 9 of 10 (0.054); W2 and W3 only |
| H3: merge+shift beats merge-only | not supported | merge+shift 1.7-7.0% dearer, all four significant |
| H4: anneal at plan time; online pays disruption | partial (1 of 8 clause cells) | plan: -3.0% cost on W1 but +21-38% evictions; online on W1: +68% cost, -31% evictions |
| H5: off-plan placement and its levers | partial | off-plan share 0.75; most-allocated +29-44% fidelity; deletion-cost n.s.; run set flagged |
| H6: frontier matches or dominates B2, B3, B5 | not supported | 3 cells dominated, 1 dominates, 2 inconclusive |
| H7: random sampling cheaper per pass at 1,000 nodes | not supported | mean -37.2%, p95 +19.3%; no pass over 1 minute |
| H7-B2 (random vs B2 at 1,000 nodes) | absent (group cut) | no `exp7-b2-n1000` runs |
| H7-S (timing pass, one VM) | not supported; agrees with H7 on both tests | mean -29.4%, p95 +37.9%; all 20 reruns reproduce their twins |
| Ablations (family of 8) | reported | every removal lowers cost (-7.5% to -30.7%), 6 of 8 significant |
| B4 parity in the wild | equivalent | 6 of 6 TOSTs at 3%; 7 of 60 seeds exact |
| Exp 8 optimality gap | reported | 8-21% at 50 nodes (B5 78-145%), 9-22% at 200, 11-43% at 1,000 |
| Run checks | reported: all valid | 1,551 runs; 130 cross-group duplicate sets identical; 0 wrong binary |

## Per hypothesis

Each subsection gives the hypothesis as preregistered, the verdict, the effects that decide it and what the result
does and does not show. Percent differences are random minus heuristic (or arm minus reference) over the reference's
mean; "90% CI" is the TOST interval, "95% CI" a paired bootstrap interval (10,000 resamples).

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

### H2. Does a potential drop predict node removals? (Exp 2; W2 and W3, W6 cut)

**Hypothesis.** A drop in the potential over a 10-minute window predicts node removals in the next window. Per run
(every valid W2, W3 and W6 run of the Exp 2 groups except B0, each distinct configuration and seed once):
rho = Spearman(-dPhi over the window, node removals in the next), per potential (P1 load entropy, P2 utilization
entropy, P3 quadratic); per seed the median rho; an exact one-sided sign test over the 10 seeds, Holm over potential
x scenario with data. A potential satisfies H2 iff its test rejects after Holm and the median of per-seed medians is
positive in every scenario with data; partially if in some.

**Verdict:** P1 partially supported, P2 and P3 not supported, on W2 and W3 only: `realism-w6`, whose W6 runs Exp 2
would have used, was cut, so Holm runs over the 6 potential x scenario tests with data, and `exp1-baselines-n1000`,
which would have added B2 and B3 W2 runs at 1,000 nodes, was cut too (`paper/tables/exp2_correlation.md`, figure
`fig6_leadlag`):

| Potential | W2: median rho (95% CI), seeds positive, Holm p | W3 | Verdict |
| --- | --- | --- | --- |
| P1 load entropy | +0.61 (+0.58, +0.63), 10 of 10, 0.006 | +0.48 (+0.34, +0.62), 9 of 10, 0.054 | partial |
| P2 utilization entropy | -0.27 (-0.32, -0.22), 0 of 10, 1 | +0.18 (+0.04, +0.26), 9 of 10, 0.054 | not supported |
| P3 quadratic | -0.30 (-0.36, -0.23), 0 of 10, 1 | +0.12 (-0.01, +0.21), 7 of 10, 0.52 | not supported |

With 10 seeds the smallest one-sided sign-test p is 1/1,024; after Holm over 6, 9 of 10 seeds positive (p = 0.011,
adjusted 0.054) is not enough here. P1's 10-minute drop correlates positively with next-window removals on W2 (10 of
10 seeds), and on W3 nine of ten seeds point the same way. The per-minute lead-lag (figure `fig6_leadlag`,
descriptive) peaks at lag 0 and is roughly symmetric, so it does not show that drops come before removals; whether
they do was not tested. On W2, P2 and P3 drops are followed by fewer removals in the next window (a non-positive
per-seed median in every seed; overall medians -0.27 and -0.30), although minute by minute they coincide with
removals (lag-0 median correlation about +0.36 for both).

### H3. Merge plus shift vs merge only (Exp 3; complete)

**Hypothesis.** Merge plus shift beats merge-only on post-warmup integrated cost at an equal pod-eviction budget,
under churn. Random generation with entropy-greedy planning, moves {random merge; random merge plus random shift},
pod-eviction budget {lo: 18, hi: 36 pods per minute per NodePool}, on W2 (diurnal) and W3 (batch churn) at 200
nodes. Four paired Wilcoxon tests, Holm over the 4; supported iff all four are significant with merge+shift lower.

**Verdict: not supported, and the effect runs the other way** (`paper/tables/exp3_h3.md`): merge+shift has higher
integrated cost in all four cells, significantly after Holm.

| Merge+shift vs merge-only | Integrated cost after warmup | Evictions | Steady-state $/hr |
| --- | --- | --- | --- |
| W2, budget lo | +7.0% (95% CI +5.1, +9.0) | +241% (+188, +321) | +5.1% (-0.6, +11.6) |
| W2, budget hi | +3.9% (+2.8, +5.0) | +23% (+14, +31) | +2.9% (+0.4, +5.6) |
| W3, budget lo | +1.9% (+1.2, +2.7) | +34% (+23, +47) | +2.9% (+1.9, +3.9) |
| W3, budget hi | +1.7% (+0.4, +3.0) | +38% (+21, +61) | +3.8% (+2.1, +5.7) |

(Holm p = 0.008 for the first three, 0.027 for W3 budget hi; descriptive columns from
`paper/tables/exp3_secondary.md`.)

**What it shows.** "Equal budget" is an equal cap, not equal use: with shifts available the pipeline spends more of
the eviction budget (on W2 at the low budget, 1,144 evictions against 335) and ends with a higher steady-state cost
in every cell (+2.9% to +5.1%; on W2 at the low budget the 95% interval spans zero, -0.6 to +11.6). On W3 the fleet
ends with 15-17% more nodes; on W2 the final node counts do not differ (+0.5% and +0.6%). Under these settings the
shift moves, which relocate pods without deleting a node, cost more than they save; whether a stricter shift
acceptance rule would change that was not tested.

### H4. Annealing at plan time and online (Exp 4; complete)

**Hypothesis.** Annealing at plan time reaches cheaper states than greedy acceptance with no extra executed
disruption; annealing online pays disruption for little gain. Random generation, W1 and W4 at 200 nodes. Plan
clause, per scenario: the default anneal-plan (t0 = 1, cooling 0.98) vs entropy-greedy has significantly lower
steady-state cost, and the bootstrap 95% upper bound of the percent difference in evictions is below +10%. Online
clause, per anneal-online t0 {0.3, 1, 3} and scenario: significantly more evictions, and the 95% lower bound of the
percent difference in steady-state cost above -1%. Eight Wilcoxon tests, Holm over the 8. Supported iff every clause
holds; partially otherwise (the analysis reads "partially" as at least one clause cell holding).

**Verdict: partially supported, on one of eight clause cells** (`paper/tables/exp4_clauses.md`, `exp4_h4.md`).

- **Plan clause fails in both scenarios: on disruption in both, and on W4 also on cost.** Anneal-plan lowers
  steady-state cost on W1 by 3.0% (95% CI -4.7 to -1.7, Holm p = 0.020) and on W4 by 1.0% (-2.7 to +0.8, not
  significant), but evicts 37.7% (+29.3 to +47.6) and 20.9% (+12.7 to +30.3) more pods than entropy-greedy, far past
  the +10% allowance.
- **Online clause holds in one cell** (W4, t0 = 3: 13.3% more evictions, Holm p = 0.020, at 11.0% higher cost). On
  W1 annealing online does not pay disruption: it evicts about 31% fewer pods than entropy-greedy (Holm p = 0.016 at
  every t0) and ends 68-69% more expensive, with 114-116 nodes left against 65. On W4 the other two t0 values evict
  7-8% more (not significant) at 12-13% higher cost.

**The sweep** (`paper/tables/exp4_sweep.md`, descriptive). All six anneal-plan settings end 1-3% cheaper than
entropy-greedy on W1 and W4 (W1 $42.4-43.1 against $43.7 per hour; W4 $43.0-43.4 against $43.9) with 21-48% more
evictions, and none is cheaper in integrated cost after warmup ($140.0-147.1 against $136.9 on W1). The
three anneal-online settings are nearly identical to one another.

**What it shows.** In this simulator, annealing at plan time buys a small steady-state saving (significant on W1,
-3.0%; -1.0% and not significant on W4) with a large increase in executed disruption, the opposite of "no extra
disruption". Annealing online, executed move by move with its temperature cooled per cycle, mostly fails to
consolidate rather than over-disrupting: it ends with a far larger fleet on W1. It takes one move per cycle, the
first in random order that the Metropolis rule accepts, and every downhill move qualifies, so it mostly executes
small shifts: on W1, 94% of its executed moves are shifts and it merges about 22 times per run, against 78 for
entropy-greedy (decision logs).

### H5. Plan fidelity: where evicted pods land (Exp 5; complete)

**Hypothesis.** Under least-allocated placement a measurable share of evicted pods land off-plan; most-allocated
placement and deletion-cost scale-down shrink it. (a) For random/entropy-greedy under least-allocated, the bootstrap
95% lower bound of the off-plan share (1 - fidelity) exceeds 0.05 under both scale-down models. (b) Family H5, 10
Wilcoxon tests on fidelity (W2, 200 nodes). Supported iff (a) holds and, for random/entropy-greedy, most-allocated
significantly raises fidelity in at least one scale-down stratum and deletion-cost in at least one placement
stratum; partially if only one lever does. Oracle check: plan-following placement must reach mean fidelity 0.95,
or the run set is flagged.

**Verdict: partially supported** (`paper/tables/exp5_fidelity.md`, `exp5_tests.md`, figure `fig5_fidelity`).

- (a) holds: about three in four evicted pods land off-plan. Random/entropy-greedy under least-allocated has
  off-plan share 0.749 (95% lower bound 0.725) with spread scale-down and 0.755 (0.737) with deletion-cost.
- Most-allocated placement raises fidelity: random/entropy-greedy from 0.251 to 0.325 (+29.3%, 95% CI +18.9 to
  +42.4) under spread and from 0.245 to 0.353 (+43.7%, +35.2 to +56.4) under deletion-cost; B2 by +17.4% and +17.6%
  (0.451 to 0.530, 0.455 to 0.535); all four significant after Holm (p = 0.020).
- Deletion-cost scale-down does not significantly raise it: random/entropy-greedy -2.3% (-10.3 to +7.5) under
  least-allocated and +8.5% (+2.2 to +15.3; unadjusted p = 0.027, Holm p = 0.11) under most-allocated; B2 +0.8% and
  +1.0%; none significant after Holm.
- The steered executor raises random/entropy-greedy's fidelity under least-allocated by +72.5% (0.251 to 0.434;
  +52.7 to +94.5) and +72.8% (0.245 to 0.424; +60.7 to +82.8), Holm p = 0.020. In its affinity mode it gives the
  pods that shift moves evict a preferred node affinity for their planned node; merges go through the upstream queue
  unsteered. Steered pods alone cannot account for the whole gain: shift evictions fell from 21% to 14% of
  evictions under the steered executor, and if every one of them had landed on plan while merges kept their
  unsteered fidelity (at most 0.32), fidelity would be at most about 0.41, short of 0.43; so the unsteered merge
  evictions landed on plan more often too.

**Oracle flag.** Plan-following placement is an oracle: it binds each evicted pod's replacement to its planned node
if that node still passes the scheduler's filter at bind time (schedulable, untainted, with room, and satisfying
anti-affinity and hard spread), and otherwise falls back to least-allocated scoring. Random/entropy-greedy reached
0.954 with spread scale-down but 0.937 (95% CI 0.902 to 0.962) with deletion-cost, below the 0.95 floor, so the
Exp 5 run set is flagged, as the preregistration requires; B2 reached 0.98 under both. The shortfall comes mostly
from one seed: seed 107 reached 0.807 (727 hits, 174 misses), while the other nine averaged 0.951 (seed 107 with
spread scale-down: 0.976). A miss under the oracle means the planned node failed the filter at bind time; the cause
was not diagnosed in the session. It is specific to the pipeline (B2 misses about 2% under the same scale-down
schedule), which runs several moves per cycle where B2 runs one command at a time, so its own concurrent moves
draining, tainting or filling planned destinations is a plausible channel. The flagged cell is not in any of the 10
H5 tests; the lever comparisons assume the same cause does not bias the least- and most-allocated cells, which was
not checked.

**What it shows.** Under upstream's default scheduler profile, the pipeline's plans mostly do not survive contact
with placement. B2's fidelity is higher (0.451 and 0.455 under least-allocated); why was not tested. Most of B2's
evictions come from replace commands that launch a new node (501 replace vs 190 delete commands), while the
pipeline's merges remove whole nodes with destinations from the same upstream simulation yet land on plan less
often, so neither whole-node moves nor the simulation explains the gap. The steered executor is the most effective
lever measured, though only part of its effect comes from the pods it steers; deletion-cost scale-down produced no
significant change after Holm (at most a small effect under most-allocated that this family cannot confirm).

### H6. Savings vs disruption against upstream and the descheduler (Exp 6; complete)

**Hypothesis.** Sweeping lambda traces a savings vs disruption frontier that matches or dominates B2, B3 and B5. Per
baseline and workload (W2 diurnal, W3 batch churn, 200 nodes), random/entropy-greedy at the lambda fixed in advance
for that baseline (B2: 0.05, since it accepts any saving; B3: 0.5, since Balanced at k = 2 is dE <= 0 with lambda/mu
= 1/2; B5: 0.1, the calibrated default) vs the baseline on savings (B0's integrated cost after warmup on the same
seed and placement minus the run's, most-allocated B0 for B5) and on eviction cost, paired exact Wilcoxon, Holm over
the 12. A cell dominates iff one metric is significantly better and the other not significantly worse, and is
dominated iff a metric is significantly worse and none significantly better. Supported iff no cell is dominated and
at least 3 of 6 dominate.

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

**The frontier** (`paper/tables/exp6_frontier.md`, means over 10 seeds). Raising lambda cuts eviction cost at no
loss of savings up to lambda 2 on W2 and lambda 1 on W3, so within the sweep the lower lambdas, the matched 0.05,
0.1 and 0.5 among them, are dominated by higher ones; only beyond that does it give up savings: on W2 the pipeline
saves $60.6-61.9 at lambda 0.05-2 while eviction cost falls from 1,182 to 748, then $49.5 at lambda 5 with 375; on
W3 it saves $18.1-19.2 at lambda 0.05-1 (eviction cost 3,033 to 2,012) and $13.0 and $4.4 at lambda 2 and 5 (918 and
90). Only lambda 2 and 5 are Pareto-optimal; on W2 so is B3 ($66.9 saved at 822), which saves more than any pipeline
point. On W3 the upstream methods save more than any pipeline point (B2 $26.2 at 2,138; B3 $24.1 at 1,441), and B5
($6.0 at 478) is also on the frontier.

**What it shows.** The matched points were fixed in advance, not chosen from results: B2's and B3's come from those
baselines' acceptance rules (B3's up to the pipeline's alpha*dPhi term), and B5 runs at the calibrated default
lambda = 0.1. Upstream
Balanced consolidation (B3) is the stronger operating point on both workloads; the pipeline only
reaches the frontier at high lambda, where it evicts least. The descheduler baseline (B5, HighNodeUtilization under
most-allocated scheduling) saves much less than either on W2. W3's batch churn is where the pipeline does worst
relative to upstream.

### Ablations (complete)

**Design.** The "full" configuration (random generation, anneal-plan, the util-entropy potential, the low
pod-eviction budget of 18 pods per minute) against four variants with one component removed: no potential (alpha =
0, potential `none`), no eviction budget, no annealing (entropy-greedy), no randomness (heuristic generation). W1
and W2 at 200 nodes; family of 8 Wilcoxon tests on steady-state cost, Holm over the 8; no direction was
preregistered.

**Result** (`paper/tables/fig8_ablation.md`, figure `fig8_ablation`): removing any one component lowers steady-state
cost in every cell, significantly after Holm in 6 of 8.

| Removed (vs full) | W1 steady-state $/hr | W2 steady-state $/hr | Evictions, W1 / W2 |
| --- | --- | --- | --- |
| (full) | $50.18 | $39.52 | 1,973 / 1,448 |
| potential | -9.7% (95% CI -12.4, -7.2) | -12.7% (-19.8, -4.6), Holm p = 0.07 | -41% / -76% |
| eviction budget | -15.5% (-18.7, -12.5) | -30.7% (-36.7, -23.5) | +3% (-3, +10) / +33% |
| annealing | -8.9% (-11.9, -5.8) | -9.5% (-18.9, +0.5), Holm p = 0.11 | -37% / -21% |
| randomness | -7.5% (-11.2, -4.1) | -15.5% (-21.8, -8.7) | -48% / -73% |

(Holm p = 0.016 or 0.018 where not shown.) Integrated cost after warmup is lower for every variant too (-3% to -27%;
W2 without annealing -2.9%, 95% CI -6.4 to +0.4).

**What it shows.** At the low eviction budget (18 pods per minute: the median over the pilot's W2
random/entropy-greedy runs of the mean evictions per minute in minutes with any eviction, 18.3 rounded, chosen so
that it binds), the full configuration is the most expensive of
the five. Each of the other three removals also cuts evictions (by 21-76%) and lowers cost, which suggests the
potential, annealing and random generation each spend the capped budget on moves that do not pay off within it; that
reading was not tested further. Removing the budget alone recovers most of the cost; the W1 variant
without it is the same configuration as Exp 1's random/anneal-plan at 200 nodes and reproduces its steady-state cost
exactly ($42.40). The ablation measures components under this one budget; it does not show that the components
hurt without one (Exp 1 and Exp 4 run without a pod-eviction budget).

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
confound's direction"). A supplementary timing pass, H7-S, declared before any random-arm 1,000-node result, reran both
arms of every seed on one VM.

**H7-S** (`paper/tables/h7s_tests.md`, `h7s_runs.md`). All 20 reruns reproduced their session twins exactly (the
decision log up to run IDs, the time series, the pod-set snapshots and every simulated summary field), and every
seed's two arms ran on VM D at a mean concurrency of 4.0. Random generation's mean CPU time per pass is 29.4% lower
(95% CI -33.6 to -25.2; 1,004 vs 1,422 ms; Holm p = 0.004) and its p95 37.9% higher (+34.0 to +41.7; 3,127 vs 2,267
ms; Holm p = 0.004): the same outcome as H7 on both tests, so H7-S is "not supported" and agrees with H7 test by
test. CPU time per pass, rerun over session twin: heuristic 0.998 (0.96 to 1.03 across seeds), random 1.12 (1.07 to
1.16). The heuristic cells cost the same on VM D beside a mix of both arms as on VM B beside heuristic cells; the
random cells cost 12% more than on VM C beside random cells. The change of design (machine and co-runners) moved
random's mean advantage from 37% to 29% and did not change either test's direction.

**Scaling** (descriptive, `paper/tables/exp7_latency.md`, figure `fig4_latency`; W1, mean CPU time per pass):

| Nodes | heuristic/entropy-greedy | random/entropy-greedy | B2 (upstream) |
| --- | --- | --- | --- |
| 50 | 19.7 ms (p95 67.8), VM C | 16.8 ms (p95 104.0), VM C | 21.3 ms (p95 71.5), VM C |
| 200 | 143.1 ms (p95 324.2), VM A | 97.9 ms (p95 392.3), VM A | 198.6 ms (p95 908.2), VM C |
| 1,000 | 1,425.1 ms (p95 2,285.5), VM B | 894.8 ms (p95 2,725.6), VM C | cut (`exp7-b2-n1000`) |

From 50 to 1,000 nodes the mean grows about 72x for heuristic and 53x for random generation, while the node count
grows 20x (heuristic's 50- and 1,000-node cells ran on different VMs, and co-runners differ by size for both arms,
so the two ratios are not directly comparable); random's p95 is above heuristic's at every size. B2's 200-node p95
is 2.8x heuristic's. No pass in these cells came near a minute: the slowest took 6.1 s (random/entropy-greedy,
1,000 nodes). Across all W1 session cells the slowest pass took 7.0 s (heuristic/anneal-plan, 1,000 nodes) and in
the H7-S reruns 20.8 s (random/entropy-greedy, seed 101). The single finished W5 B2 cell (200 nodes, cut group
`realism-w5`) had a 52.3 s pass, the session's slowest and still under the 1-minute limit. B2 at 1,000 nodes was
cut.

**What it shows.** Random sampling makes the typical pass cheaper and the slow passes slower: its cost per pass is
more variable, so a fixed per-pass time limit binds on the tail, not the mean. Neither arm comes close to upstream's
1-minute limit at 1,000 nodes in this simulator.

### Exp 8. Optimality gap to the offline packing bound (descriptive; 1,000-node B2/B3 rows cut)

Each run's fleet cost at its final pod-set snapshot against a lower bound on the cheapest fleet that could hold
those pods (B6: CP-SAT at 50 nodes: of the 59 distinct pod sets, 48 bounds proven optimal, 8 feasible, 3 skipped
above its 600-pod limit; the LP relaxation at 200 and 1,000 nodes, on average 3% below CP-SAT on the 50-node final
snapshots), as gap = cost / bound - 1 (`paper/tables/exp8_gap.md`, figure `fig7_optimality_gap`). The bound is exact
only where CP-SAT proved optimality (25 of the 30 distinct final 50-node pod sets), so the gaps are upper estimates
of the distance to the cheapest fleet, more so at 200 and 1,000 nodes. At 50 nodes the consolidating arms end 8-21%
above the bound and B5 (the descheduler) 78-145% above; at 200 nodes 9-22%; at 1,000 nodes 11-43% for the six
pipeline arms (the B2 and B3 rows would have come from `exp1-baselines-n1000`, which was cut), widest for
heuristic/savings-only on W1 and W4 (42-43%). The lowest final gaps are
random/savings-only's on W1 and W4 (8-12% at 50 and 200 nodes). Gaps are computed on the final snapshot only at 200
and 1,000 nodes. At 50 nodes the table also has each run's mean over its snapshots that have a CP-SAT bound; the 3
skipped pod sets (early W2 snapshots of two seeds, 698-739 pods) are left out of those means, not given the LP
bound, and no final snapshot was skipped.

### B4. Parity in the wild (complete)

The pipeline configured with its fine upstream adapters (B4, and B4-balanced under the Balanced policy) against B2
and B3 on the same seeds, W1, W2 and W4 at 200 nodes, uncapped, over whole 3-hour simulations
(`paper/tables/b4_parity.md`). **Status: equivalent** (the preregistered middle tier): all six TOSTs at the 3%
margin pass after Holm (steady-state cost differences -0.75% to +0.34%, every 90% interval within -1.8% to +1.0%),
but only 7 of 60 seeds match exactly (B4 vs B2: 2 of 10 on W2, none on W1 or W4; B4-balanced vs B3: 5 of 10 on W2,
none on W1 or W4). Exact matches were not guaranteed even with identical decision logic (`docs/research/parity.md`,
"Parity in the wild", written before these runs): the simulator's candidate-order shuffle draws from one seeded
stream per run, and the pipeline's method boundaries can consume that stream differently from upstream's separate
methods. Whether this accounts for the 53 non-matching seeds was not tested. The decision-level parity test
(`docs/research/parity.md`) is the exact check.

### Run checks

`paper/tables/validity.md`, `cut_configurations.md`, `duplicate_consistency.md`, `run_checks.json`. All 1,551
finished session runs are valid: no PDB or do-not-disrupt violation, error, timeout, abandoned or retried cell (no
attempt failed or was set aside; cells killed by a VM replacement were wiped and relaunched, which is not a retry
under the preregistration), run
from another binary (all 1,571 runs checked, the timing pass's included, carry `c78b5f9`, unmodified) or
nondeterminism (TestDeterminism passed at that commit). Every one of the 130 sets of identical configurations run in
different groups (the cross-group determinism check) produced identical summaries, as did the 20 H7-S reruns. The
19 cut configurations are exactly the four cut groups' (one with the single finished W5 run).

## Deviations from the preregistration

All are logged in `experiments/RESEARCH_LOG.md`, most with the time they were decided relative to the data; where
the log gives no time, the item below gives the commit time.

1. **Session driver order.** The first driver's group list omitted `b4` and swapped the last two groups
   (`exp7-b2-n1000`, `exp1-baselines-n1000`). This was found during the third group (`exp1-n1000`), before any
   affected group ran, and that driver stopped after `exp1-n1000`. Driver v3 runs the preregistered order
   (`experiments/session-driver/`).
2. **Two VM replacements during `exp1-n1000`** (Oct 27 23:52 and Oct 28 03:30 UTC). In-flight cells were relaunched
   from scratch; simulated outcomes do not depend on the machine. Each run's VM (`machine_epoch`, the last boot at or
   before its `started_at`) is shown per cell for Exp 7 (`machine_epochs` in `paper/tables/exp7_latency.md` and the
   summary's H7 row) and per run for H7-S (`boot` in `h7s_runs.md`). Exp 1's timing columns (`exp1_secondary.md`)
   carry no VM. Two of its 1,000-node heuristic cells span two VMs: savings-only W1 (A: 8, B: 2) and entropy-greedy W4
   (B: 7, C: 3).
3. **H7 confound and H7-S.** Exp 1 runs arm-major, so H7's heuristic arm ran on one VM beside heuristic cells only and
   its random arm on another beside random cells only. Before any random-arm 1,000-node result existed, a
   supplementary timing pass (H7-S) was declared: both arms of every seed rerun on one VM, interleaved at 4-way load.
   H7 is reported as preregistered; H7-S beside it, test by test; no difference is attributed to hardware alone. An
   early claim about the confound's direction was withdrawn: the data did not support it.
4. **Cut rule mechanized, with a fallback for W5 and W6** (no same-scenario session cells to take a median from: the
   median over same-scale cells), declared before any W5 or W6 cell ran. The rule takes medians over all arms, so it
   under-projects the B2 and B3 groups at 1,000 nodes; it is applied as written.
5. **Analysis code after Gate 3.** The analysis was aligned with the preregistered text after Gate 3: one alignment
   commit (`5b6b2e3`, Oct 27 21:14 UTC, when 33 session cells had finished), then ten fixes from an independent
   verifier, each with a regression test (`df7831e`, 21:50 UTC, 88 finished). Both changed the code toward the
   preregistered text, not the text. Before them the code ran, for example, H7 on wall time per pass and H4 as 16
   tests. Neither changed a preregistered test, family, margin or decision rule. One rule is a reading of the text:
   H4's "partially otherwise" is read as "not supported" when no clause cell holds, and H4 is partial on the data
   either way. Later changes added the supplementary H7-S family and descriptive output (the summary table,
   wall-to-CPU ratios, machine epochs).
6. **A third VM replacement** (Oct 28 23:24 UTC), during `realism-w5`; nothing finished was lost.
7. **Overrun rule; four groups cut.** The cut rule started `realism-w5` projecting 2.0 minutes per cell (the
   same-scale fallback median, as W5 had never run at 200 nodes), but its B2 cells take over an hour (one finished in
   63.5 minutes). A time-only rule, declared before any W5 result was looked at, re-projects a started candidate from
   its own timing and cuts it whole if it then ends after the deadline. `realism-w5` was cut. The cut record (00:29:08
   UTC) gives two figures. One is an in-flight estimate (20 B2/B3 cells x 62 min / 4 = 5.2 h), which is not a strict
   lower bound, since no B3 W5 cell had run. The other is the re-projection from the one B2 cell that finished at
   00:28:24, while the rule was being written: 59 pending cells x 63.5 min / 4 = 15.6 h, ending long after the
   deadline. The same new rule then cut `realism-w6`, `exp7-b2-n1000` and `exp1-baselines-n1000`, treating the stop as
   if the original projection had been right. The preregistered rule cascades only after a failed start check, which
   `realism-w5` had passed. This cascade also departed from the log's own plan at restart #3 to decide each later
   candidate fresh. Judged each on its own start projection at 00:29, `realism-w6` (60 x 2.0 min / 4 = 30 min) and
   `exp7-b2-n1000` (10 x 16.0 min / 4 = 40 min) would have passed their start checks, and only `exp1-baselines-n1000`
   (about 3.7 h) would have been cut. Both projections are unreliable. The W6 one uses the same-scale fallback median
   that W5 had just shown to be wrong. The `exp7-b2-n1000` one uses the W1 1,000-node median of pipeline cells, and a
   1,000-node B2 cell takes far longer (the pilot's hit its 100-minute wall limit at simulated minute 160 of 180, and
   deviation 4 notes that the rule under-projects B2 and B3 at 1,000 nodes). So whether either would have finished by
   the deadline is unknown. Lost: H1-realism, H2's W6 tests and its B2 and B3 W2 runs at 1,000 nodes, H7-B2 and Exp
   7's 1,000-node B2 scaling point, and the 1,000-node B2 and B3 reference rows of Exp 1 and Exp 8.
8. **Driver v4** lets the last recorded decision for a group win, so the overrun cut supersedes the earlier START.
9. **Scale wording.** The preregistration says `scenario.nodes = n` sizes each workload as n mid-size nodes at 70%
   fill, and that W2, W3, W5 and W6 run fewer, larger nodes. The Gate 3 code (`scenario.go` in
   `hack/consolidation-sim/sim`) fills W1 and W4's n starting nodes to a random 15-75% each, and W3 (124-339 in Exp 6)
   and the one W5 cell (255) peak above n. The code was not changed. The text was a description error, found after the
   session, and "What ran" states what ran.

## Threats to validity

The preregistered threats stand: simulated scheduler and ReplicaSets, which no Tier B rank-agreement check bounds
because Tier B could not run; synthetic workloads; calibration on random arms only; H1's power at 10 seeds; little
headroom in a bimodal fleet; on-demand prices, with EvictionCost standing in for restart cost; and decision cost
measured on a 4-vCPU VM with up to 4 concurrent cells. Added in the session:

- **Decision cost also depends on which machine ran it.** Two VM replacements put H7's arms on different VMs. Load
  also varies inside every group: each hour the runner stops launching and drains before it is re-invoked, and the
  session's own analysis and reviews ran on the VM. The research log times the two adversarial reviews, the
  recomputation, `make verify`, the disruption tests and the final analysis. The analysis alignment (committed Oct 27
  at 21:14 and 21:50 UTC, during `exp1-n200`) and the RESULTS claim checks are dated by their commits. None ran during
  the H7 or H7-S cells or during `exp7-n50` and `exp7-b2`. Each run's `wall_cpu_ratio` shows only CPU-scheduling
  contention, meaning time the process was runnable but not running, plus steal, offset by parallel GC threads (so
  values can fall below 1). It does not show interference that raises CPU time itself. In H7-S, random's CPU time per
  pass was 12% above its session twins while its ratio stayed at 0.989-1.005.
- **No realism check ran.** W5 (constraints) and W6 (trace replay) were cut, so the preregistered threat that
  synthetic workloads may flatter random generation is only partly addressed. Exp 1's early and late phase split, the
  other preregistered response, did run (`paper/tables/exp1_phases.md`), but on the same synthetic workloads.

## Next steps

- Run what the cuts left out on a larger machine, with the Gate 3 binary (`bin/csim matrix --matrix
  experiments/matrix/session.yaml --only <group>`; the run IDs match the session's): `realism-w5` and `realism-w6`,
  and `exp1-baselines-n1000` (budget over two hours of one core per 1,000-node B2 or B3 cell). One W5 B2 cell at 200
  nodes took 63.5 minutes on this VM's class of CPU. No B3 or pipeline W5 cell has been timed. `exp7-b2-n1000` is a
  timing comparison against the session's random/entropy-greedy W1 1,000-node cells (VM C). Run it in one invocation
  beside a rerun of those cells at the same load, interleaved by seed as in H7-S
  (`experiments/matrix/h7-timing.yaml`), or report it as a cross-machine comparison with each run's `machine_epoch`,
  as H7 is.
- Fix W5's simulator cost before running `realism-w5`, or budget for it with the Gate 3 binary. A post-session CPU
  profile of one W5 B2 cell (30 simulated minutes, `experiments/RESEARCH_LOG.md`, "W5 timing probe") traced it to
  upstream topology counting. That code lists pods by label selector, which the harness's pod index does not serve, so
  each list goes through the fake client's deep copy and JSON round trip. The fix (serving those lists from the index)
  is a new binary, so its cells will not carry the Gate 3 `binary_revision` and `make figures` will mark them invalid
  in the session tree. Before using it, check that every decision is unchanged with a byte-identical rerun of the
  finished W5 cell, and log the new binary as a deviation with its own run tree.
- The full profile (`experiments/matrix/full.yaml`: 20 seeds, 5,000-node cells) and Tier B on KWOK, on a machine that
  can reach `registry.k8s.io` and `cgr.dev` (`docs/research/runbook.md`).
- Diagnose two things the session only measured: why the pipeline misses about 6% of plan-following placements in one
  Exp 5 cell (random/entropy-greedy with deletion-cost scale-down: 19% in seed 107, about 5% in the other nine seeds,
  against about 2% for B2), and why B2's plans survive placement better than the pipeline's merges.
