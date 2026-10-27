# Pilot

Calibration run for the session profile (`docs/research/PLAN.md`, Phase 4 "Statistics"): W1 and W2 at 200 nodes,
calibration seeds 1-5, 120 cells (`experiments/matrix/pilot.yaml`, raw runs in `experiments/runs/pilot/`). Every
number here comes from those runs; no evaluation seed (101 and up) was run before `experiments/PREREGISTRATION.md`
was committed. All 120 runs are valid (no PDB or do-not-disrupt violation, no error, no timeout).

Binaries: the 20 baseline cells (B2, B3) ran on `c3e9e35`; the 100 pipeline cells ran on `2d8fce7`, after the fix
for capped heuristic scans (`experiments/RESEARCH_LOG.md`, "Pilot restart"). No change between the two commits
touches code the baseline arms execute. The pipeline cells used lambda = 0.5, t0 = 0.3 and cooling 0.98 except where
the label says otherwise.

## Results by arm (means over 5 seeds)

| Scenario | Arm | Steady $/hr (mean ± sd) | Relative to B2 | Evictions | Post-warmup cost ($) | Nodes removed | Wall s/cell |
| --- | --- | --- | --- | --- | --- | --- | --- |
| W1 | B2 | 43.83 ± 2.45 | 1.000 | 1210 | 128.9 | 178 | 289 |
| W1 | B3 | 45.12 ± 2.20 | 1.030 | 743 | 131.0 | 121 | 169 |
| W1 | heuristic/savings-only | 45.41 ± 1.68 | 1.037 | 1290 | 135.3 | 186 | 164 |
| W1 | heuristic/entropy-greedy | 45.24 ± 3.30 | 1.031 | 1056 | 133.2 | 162 | 171 |
| W1 | heuristic/anneal-plan | 44.68 ± 3.34 | 1.019 | 1089 | 133.0 | 162 | 189 |
| W1 | random/savings-only | 41.77 ± 2.01 | 0.954 | 2205 | 134.8 | 226 | 164 |
| W1 | random/entropy-greedy | 46.87 ± 3.26 | 1.069 | 1075 | 142.5 | 150 | 125 |
| W1 | random/anneal-plan | 46.96 ± 2.71 | 1.071 | 1516 | 149.6 | 159 | 168 |
| W1 | random/entropy-greedy/lambda=0.1 | 43.21 ± 1.99 | 0.986 | 1549 | 135.9 | 183 | 118 |
| W1 | random/entropy-greedy/lambda=2 | 47.80 ± 3.09 | 1.091 | 645 | 144.4 | 104 | 76 |
| W1 | random/anneal-plan/t0=0.1 | 46.05 ± 2.31 | 1.051 | 1812 | 150.3 | 174 | 112 |
| W1 | random/anneal-plan/t0=1 | 46.40 ± 3.55 | 1.058 | 1503 | 146.8 | 160 | 90 |
| W2 | B2 | 28.25 ± 1.56 | 1.000 | 1485 | 134.8 | 77 | 132 |
| W2 | B3 | 27.10 ± 1.22 | 0.960 | 806 | 132.2 | 61 | 117 |
| W2 | heuristic/savings-only | 28.09 ± 1.04 | 0.995 | 2030 | 135.7 | 80 | 125 |
| W2 | heuristic/entropy-greedy | 26.98 ± 1.15 | 0.956 | 994 | 132.8 | 61 | 116 |
| W2 | heuristic/anneal-plan | 27.39 ± 1.01 | 0.971 | 1605 | 133.3 | 69 | 466 |
| W2 | random/savings-only | 28.12 ± 1.41 | 0.997 | 2132 | 137.4 | 62 | 138 |
| W2 | random/entropy-greedy | 27.81 ± 1.57 | 0.985 | 1064 | 137.8 | 46 | 208 |
| W2 | random/anneal-plan | 28.17 ± 1.42 | 0.998 | 1730 | 138.5 | 48 | 177 |
| W2 | random/entropy-greedy/lambda=0.1 | 28.61 ± 1.69 | 1.013 | 1224 | 138.6 | 49 | 132 |
| W2 | random/entropy-greedy/lambda=2 | 27.22 ± 0.95 | 0.965 | 811 | 136.3 | 43 | 115 |
| W2 | random/anneal-plan/t0=0.1 | 28.03 ± 1.17 | 0.993 | 1632 | 138.3 | 48 | 163 |
| W2 | random/anneal-plan/t0=1 | 27.35 ± 1.31 | 0.969 | 1455 | 137.6 | 43 | 132 |


"Relative to B2" is the mean over seeds of the run's steady-state cost divided by the same seed's B2 steady-state
cost. Steady state is the mean $/hr over the last 20% of the 3-hour horizon.

## Calibration (rules fixed before these cells were inspected; research log, "Calibration rules")

| Parameter | Candidates (arm) | Mean cost relative to B2 over W1 and W2 | Chosen |
| --- | --- | --- | --- |
| lambda | 0.1 / 0.5 / 2 (random/entropy-greedy) | 0.9997 / 1.0268 / 1.0279 | **0.1** (2.6% better than the default 0.5, above the 1% threshold) |
| t0 (per-node) | 0.1 / 0.3 / 1 (random/anneal-plan, lambda 0.5) | 1.0222 / 1.0347 / 1.0138 | **1** (2.0% better than the default 0.3) |
| Pod-eviction budget | median over W2 random/entropy-greedy runs of the mean evictions per active minute | 18.3 (run means 14.0-20.8) | **budget-lo = 18, budget-hi = 36** pods per minute per NodePool |

t0 was calibrated at lambda = 0.5; the session runs anneal-plan at lambda = 0.1 and t0 = 1, a combination the pilot
did not run. t0 = 1 is the largest value tried, so Exp 4's sweep moved to t0 in {0.3, 1, 3} (cooling 0.95 and 0.98)
to bracket it. lambda = 0.1 applies to every energy planner in every arm (heuristic and random alike, Exp 1, 3, 4,
5, 8 and the ablations); Exp 6 sweeps lambda separately.

## Variance and power for H1

| --- | --- | --- | --- | --- |
| W1 | savings-only | -8.02 | 1.69 | 0.98 |
| W1 | entropy-greedy | +3.61 | 1.05 | 0.61 |
| W1 | anneal-plan | +5.10 | 1.57 | 0.91 |
| W2 | savings-only | +0.10 | 5.34 | 3.10 |
| W2 | entropy-greedy | +3.09 | 2.04 | 1.18 |
| W2 | anneal-plan | +2.85 | 3.56 | 2.06 |

At 10 seeds the projected 90% interval half-width is under 1% of cost on W1 and 1-3% on W2, inside the 3% margin, so
TOST at 10 seeds can establish equivalence when the true difference is near zero. These pilot differences were
measured at lambda = 0.5 and t0 = 0.3, not the calibrated values, and on calibration seeds; they size the test and
are not evidence for or against H1.

## Timing (sizing the session profile)

Cells shared the VM's 4 vCPUs with 3-5 other processes, so wall times are upper bounds. Per cell: 200-node W1/W2
pipeline arms 1.5-8 minutes, B2 and B3 2-5 minutes. Single timing runs at 1,000 nodes (W1, seed 1, 3 hours):
random/entropy-greedy 14 minutes, heuristic/entropy-greedy 22 minutes, B2 over 100 minutes (it hit its wall limit
at simulated minute 160). A W3 200-node B2 cell took 14 minutes. Projected session wall time with 4 workers, without
the two 1,000-node baseline groups: roughly 20-30 hours, about half of it Exp 1's 1,000-node cells. Under the plan's
cut order the 1,000-node groups outside Exp 1 (`exp1-baselines-n1000`, about 120 hours of one core, and
`exp7-b2-n1000`) run last and only if time remains.
