# Experiment 9: biased candidate selection, potential-guided shifts and early stopping

Post-session, not preregistered. This protocol, the matrix (`experiments/matrix/exp9.yaml`) and the code it needs
were committed before any Exp 9 cell ran, and every later change is logged in `experiments/RESEARCH_LOG.md`. It
reads to anyone who knows Karpenter's consolidation; the mechanisms it tests are described in
`docs/consolidation/pipeline-refactor.md`.

## Why

The session's random generation arms (`random/*` in Exp 1) picked candidate nodes uniformly at random and spent a
fixed budget of 500 attempts per generator on every searching pass, whether or not the search had anything left to
find. The paper (`paper/paper.md`, section 7) traces random generation's higher decision CPU to exactly that: at
1,000 nodes every searching pass ran 1,000 attempts, and `random-merge` kept computing placements after its 16 full
simulations were spent and discarded them. The obvious alternatives, which the pipeline already supported but the
session never ran, are:

1. **Biased candidate selection.** Pick sources in proportion to how much emptying them would lower the potential
   (`potential-weighted`, C3), or deterministically take the nodes below the fleet's mean utilization as sources and
   those above it as destinations (`util-band`, C2), instead of a uniform quarter of the eligible nodes
   (`uniform-random`, C1).
2. **Potential-guided shifts.** For each sampled (source, fuller destination) pair, move the pod set that most
   lowers the potential (`greedy-shift`, G4) instead of a random handful of pods (`random-shift`, G3).
3. **Early stopping.** Stop a generator once a run of attempts has produced no move. This did not exist: it is the
   `idleAttempts` parameter added to `random-merge`, `random-shift` and `greedy-shift` for this experiment (a
   generator stops after that many consecutive attempts emitted nothing; 0, the default, never stops early).

Exp 9 runs each mechanism alone and then combined, against the session's two generation references under the same
planner, budget, executor and potential, and reports what changed in cost, evictions and decision CPU.

## Configurations

Every arm uses the session's settings for everything but generation: the `entropy-greedy` planner at lambda 0.1,
the search budget of 16 full simulations per cycle and 500 attempts per generator, pooled generators, the
`shift-evict` executor and the `util-entropy` potential (`experiments/matrix/session.yaml`, anchors `search` and
`std`; `experiments/PILOT.md` for how lambda was fixed).

| Label | Candidates | Moves | What it tests |
| --- | --- | --- | --- |
| `heuristic/entropy-greedy` | `upstream` | `upstream-multi` + `upstream-single` (fine mode) | Reference A: Karpenter's own candidate order and search |
| `random/entropy-greedy` | `uniform-random`, a quarter of sources | `random-merge` (confirm) + `random-shift` | Reference B: the session's random arm |
| `weighted/entropy-greedy` | `potential-weighted`, a quarter of sources | `random-merge` (confirm) + `random-shift` | Biased selection alone |
| `band/entropy-greedy` | `util-band` (dominant resource, full above 95%) | `random-merge` (confirm) + `random-shift` | Deterministic band selection alone |
| `weighted+greedy-shift/entropy-greedy` | `potential-weighted` | `random-merge` (confirm) + `greedy-shift` | Biased selection and guided shifts |
| `random/idle-50` | `uniform-random` | both generators with `idleAttempts: 50` | Early stop alone |
| `weighted/idle-50` | `potential-weighted` | both generators with `idleAttempts: 50` | Biased selection and early stop |
| `weighted+greedy-shift/idle-50` | `potential-weighted` | `random-merge` (confirm) + `greedy-shift`, both with `idleAttempts: 50` | Everything combined |

Both references are re-run under the Exp 9 binary rather than taken from the session's runs, so every comparison is
same-binary and same-machine. With `idleAttempts` at its default the generators behave exactly as in the session
binary (the change adds a counter the default never reaches), which the moves package's determinism and budget tests
cover; re-running the references removes any doubt.

**The early-stop level.** 50 is a tenth of the 500-attempt budget, fixed before any run by that rule, not tuned. At
the session's observed hit rates (about one shift found per eight `random-shift` attempts at 200 nodes, and no
merge found once `random-merge`'s simulations are spent) it ends a dry search well before the budget while rarely
cutting a live one short. A second level is not run; if 50 turns out to matter, a sweep is a follow-up.

## Workloads, sizes, seeds

- **200 nodes:** W1, W2 and W4 (the session's Exp 1 workloads: diurnal, steady with a drop, and bursty), all eight
  arms, seeds 101-110, 3 simulated hours. 240 cells.
- **1,000 nodes:** W1 only, where the session measured random generation's highest per-pass cost, five arms (both
  references, `random/idle-50`, `weighted/idle-50`, `weighted+greedy-shift/idle-50`), seeds 101-110, 3 simulated
  hours. 50 cells.

Seeds are the session's evaluation seeds; the pilot's seeds 1-5 were used only for the smoke run that checked the
new arms construct and produce moves (logged, not data).

## Binary and run order

The binary is built from a clean clone at the commit recorded in `experiments/exp9-binary.txt`, so its VCS stamp is
clean, and every run's manifest records that revision. Groups run in the order `exp9-n200`, `exp9-n1000`, four cells
at a time on the same 4-vCPU virtual machine the session used, with the runner's resume loop (`exp9.yaml`, header).
A cell that fails is set aside and retried once, as in the session; a cell with two set-aside attempts is abandoned.

## Metrics

From each run's `summary.json`:

- cost: `steady_cost_per_hour` (the fleet's hourly price at the end), `integrated_cost_post` (dollars after the
  warmup), `final_nodes`;
- disruption: `evictions`, `eviction_cost`;
- decision compute: `cpu_pass_mean_ms`, `cpu_pass_p95_ms`, `cpu_pass_total_s`, `full_simulations`,
  `disruption_passes`.

From each run's `decisions.jsonl.gz`, over the searching passes (cycles with a `moves` summary record):

- the share of passes that search;
- attempts per searching pass, full simulations per searching pass and moves emitted per searching pass;
- the share of searching passes that hit a search cap (`exhausted`: the attempt budget or the simulation pool),
  which early stopping should lower;
- CPU per searching pass: `cpu_pass_total_s` over the number of searching passes (an upper bound, as in the paper,
  since it carries the gated passes' floor).

## Reporting rules (fixed before any run)

Descriptive. No hypothesis is tested and nothing is Holm-adjusted; Exp 9 answers "what changes, and by how much",
not "is the hypothesis supported".

- Per workload and size, every treatment arm is paired by seed with **both** references. The report gives the mean
  per-seed difference (treatment minus reference) and that difference as a percent of the reference mean, each with
  a 95% percentile bootstrap interval over the seeds (10,000 resamples, the analysis package's fixed seed).
- A difference is called **resolved** when its interval excludes zero, and its direction is then reported; otherwise
  it is reported as unresolved with the interval. Percent effects smaller than 1% are reported but not discussed
  as effects.
- The same metrics are reported per arm (mean, 95% bootstrap interval) so arms can be read on their own.
- A configuration with fewer than 10 finished runs in a stratum is still reported, flagged with its count, and
  excluded from any sentence that generalizes over seeds.
- Validity follows the session's rules (`experiments/PREREGISTRATION.md`, "Exclusion"): a run with a PDB or
  do-not-disrupt violation, an error, a timeout or abandonment is invalid and dropped pair-wise; invalid counts are
  reported.
- The write-up (`experiments/RESULTS.md`, "Exp 9"; `paper/paper.md`, section 9) reports every arm and metric in the
  tables, whichever way the results fall.

**Expectations, written down so they can be wrong:**

- E1. Early stopping (`random/idle-50` vs `random/entropy-greedy`) lowers attempts per searching pass and CPU per
  pass, and leaves cost unresolved or within 1%: the attempts it cuts are the ones that found nothing.
- E2. Biased selection (`weighted/entropy-greedy` vs `random/entropy-greedy`) finds its moves in fewer attempts, so
  the gain from early stopping is larger when the two are combined (`weighted/idle-50`); cost is no worse.
- E3. Guided shifts (`weighted+greedy-shift/entropy-greedy` vs `weighted/entropy-greedy`) lower evictions per unit of
  cost removed, because each shift is the pair's best pod set rather than a random one; CPU per searching pass may
  rise, since each attempt scores every pod of the source.
- E4. None of the treatments closes the gap to the heuristic reference on W2 CPU per searching pass, where the
  cost of a random search comes from the size of each probe (the paper, section 7), not from blind attempts.

## Analysis

`paper/analysis/exp9.py` (`make figures` runs it; `/opt/research-venv/bin/python -m paper.analysis.exp9` runs it
alone) reads `experiments/runs/exp9/*` and writes `paper/tables/exp9_arms.md`, `exp9_paired.md`,
`exp9_search.md` and `exp9.json`.
