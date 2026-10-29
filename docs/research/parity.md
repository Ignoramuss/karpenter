# Parity report: pipeline upstream adapters vs upstream consolidation

Gate 1 of `docs/research/PLAN.md` requires the consolidation pipeline, configured with its upstream adapters, to
make exactly the decisions today's multi-node and single-node consolidation make, on sampled cluster states. This
report describes the differential test, its results, and what it does and does not cover.

## The test

`TestParity` (`pkg/controllers/disruption/pipeline/parity_test.go`) generates seeded cluster states with
`harness.GenerateState`: NodePools with a consolidation policy, nodes drawn from an instance catalog, and pods placed
with a mix of shapes, owners, PDBs, do-not-disrupt annotations and fragmentation from random scale-down. For each
state it runs, on identical copies of the environment and the same fake clock:

- **upstream:** the disruption controller's `MultiNodeConsolidation` then `SingleNodeConsolidation`, as the
  controller orders them;
- **pipeline:** `pipeline.NewMethod` with one of three configurations:
  - `coarse`: the upstream adapters wrapping each method's `ComputeCommands`;
  - `fine`: the stage-split adapters (upstream candidates, the multi-node prefix search and the single-node scan as
    move generators, the upstream planner, the upstream-queue executor);
  - `fine-balanced-planner` (Balanced states only): the fine adapters with the `balanced` planner, which applies
    Balanced scoring to every NodePool.

A decision (`harness.Decision`) is the decision type (delete, replace, none), the candidates, the replacement
instance-type options and the disruption budget consumed (with the method that produced it, for diagnosis). Two decisions match only if all of these are equal.

**States with more than one NodePool.** Upstream is itself nondeterministic there:
`SingleNodeConsolidation.shuffleCandidates` interleaves NodePools in `lo.Keys(map)` order, and Go starts iterating a
small map at a random one of its eight slots. So with two pools the second comes first only one time in eight. For
these states the reference is the set of decisions upstream makes over repeated runs: upstream runs 8 times, and a
pipeline decision outside the observed set triggers up to 256 more upstream runs before it counts as a mismatch.
Single-NodePool states must match exactly, with upstream run twice as a control.

**Catalogs.** `kwok` (the shipped `kwok/examples/aws_instance_types.json`) and `assorted` (the disruption suite's
`fake.InstanceTypesAssorted()`: on-demand and spot offerings in several zones, thinned to keep states small). **Policies.** `WhenEmptyOrUnderutilized` and `Balanced`.

## Results

| Run | States per cell | Cells | Result |
| --- | --- | --- | --- |
| Development, coarse and fine adapters | 200 | 10 | 200/200 in every cell |
| Gate 1 (`PARITY_SEED=1`, `experiments/parity/parity-1000-seed1.*`) | 1,000 | 10 | 1,000/1,000 in every cell |
| Re-checks after each later pipeline change | 100-150 | 10 | all matched |
| Final (`PARITY_SEED=2`, four chunks in `experiments/parity/chunks/`) | 10,000 | 10 | 10,000/10,000 in every cell (per chunk below) |

The 1,000-state run, per cell (upstream decided on 846-925 states; it disagreed with itself on 10-22 multi-NodePool
states per cell, all resolved within the observed decision set):

| Policy | Catalog | coarse | fine | fine-balanced-planner |
| --- | --- | --- | --- | --- |
| WhenEmptyOrUnderutilized | kwok | 1,000/1,000 | 1,000/1,000 | |
| WhenEmptyOrUnderutilized | assorted | 1,000/1,000 | 1,000/1,000 | |
| Balanced | kwok | 1,000/1,000 | 1,000/1,000 | 1,000/1,000 |
| Balanced | assorted | 1,000/1,000 | 1,000/1,000 | 1,000/1,000 |

The 10,000-state run (`PARITY_SEED=2`) matched on every state in all 10 cells: 40,000 states, each in two or three
configurations. It ran as one chunk per policy and catalog (`parity-10000-seed2-<chunk>.summary.txt` and
`.jsonl.gz`); "nondeterministic" counts the multi-NodePool states on which upstream disagreed with itself, all
resolved within the observed decision set:

| Chunk | Ran (UTC) | coarse | fine | fine-balanced-planner | Upstream decided | Nondeterministic |
| --- | --- | --- | --- | --- | --- | --- |
| WhenEmptyOrUnderutilized, kwok | Oct 27, before 20:52 | 10,000/10,000 | 10,000/10,000 | | 9,141 | 228 |
| WhenEmptyOrUnderutilized, assorted | Oct 29 02:17-03:14 | 10,000/10,000 | 10,000/10,000 | | 8,961 | 84 |
| Balanced, kwok | Oct 29 03:14-04:07 | 10,000/10,000 | 10,000/10,000 | 10,000/10,000 | 8,675 | 247 |
| Balanced, assorted | Oct 29 04:07-05:18 | 10,000/10,000 | 10,000/10,000 | 10,000/10,000 | 8,347 | 79 |

The first chunk ran with both catalogs selected and was stopped when the session started. Its output file also holds
the first 4,474 assorted states, all matched. The assorted chunk regenerated them from the same seed, which checks
the generator and the test across runs. All 4,474 states and every match verdict are identical between the two runs.
39 records differ, all multi-NodePool states, and only in the decisions observed (upstream's and the adapters',
which keep upstream's map-order nondeterminism, and the nondeterminism flag). The 2,625 single-NodePool records are
byte-identical.

**Mutation check** (does the test catch adapter bugs?). Three bugs were injected one at a time into the fine
adapters. Dropping the multi-node budget decrement was caught (44/60 match). Making the upstream planner accept every
move was caught on Balanced states (85/100) and, correctly, invisible on WhenEmptyOrUnderutilized states, where
upstream accepts every move with savings. Skipping the `CanPass` pre-filter was not caught, and cannot be: `CanPass`
only skips candidates whose best case already fails the threshold, so it changes compute, never decisions (an
equivalent mutant).

## Parity in the wild

The session matrix's `b4` group runs the fine upstream adapters, uncapped, through whole 3-hour simulations on W1,
W2 and W4 at 200 nodes, against B2 and (under the Balanced policy) B3 on the same seeds. The preregistered check
(`experiments/PREREGISTRATION.md`, "B4 parity in the wild") counts seeds whose outcomes match exactly and, failing
that, tests equivalence at the 3% margin. Exact matches are not guaranteed even with identical decision logic: the
simulator's candidate-order shuffle draws from one seeded stream per run, and the pipeline's method boundaries can
consume that stream differently from upstream's three separate methods. Results are in `experiments/RESULTS.md`.

## What parity does not cover

- Events and metrics: the test compares decisions, not the Kubernetes events or Prometheus metrics each path emits.
  In fine mode the pipeline calls Balanced's scoring directly, so it emits no `karpenter_consolidation_score` metric
  or `ConsolidationApproved` event (RFC open question).
- Wall-clock timeouts: upstream's multi-node search stops at a 1-minute wall-clock timeout; the fine adapter uses the
  injected clock. Parity states are small enough never to reach either limit.
- Strategies beyond the adapters: parity says nothing about the new strategies, which are meant to decide
  differently; they are checked by the conformance suite and the determinism test instead.
