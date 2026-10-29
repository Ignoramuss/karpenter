# Tier B protocol: KWOK on kind (after the session)

Phase 5 of `docs/research/PLAN.md` checks whether the simulator's rankings survive a real API server, kube-scheduler
and controller, and measures what the simulator cannot: the controller's own CPU and memory. Tier B could not run
in the session because its image registries were blocked. They were opened afterwards (Oct 29). This protocol was
written before any Tier B run except one smoke spec, which checked the setup and is not data. Every later change is
logged in `experiments/RESEARCH_LOG.md`.

## Workload: WB, one workload replayed in both tiers

A rank-agreement check needs the same workload in both tiers. W2's 3-hour timeline would take 3 hours per KWOK
run, so both tiers run a compressed variant, **WB(n, seed)**:

- **Deployments:** exactly W2's for the same n and seed (same shapes, peak and low replica counts, no PDBs), from
  the simulator's own generator (`consolidation-sim workload`). Only the drop time differs: every deployment
  drops from its peak to its low count at once, at t = W.
- **Warmup:** W = 5 minutes. Consolidation is held off during it (Tier A: the simulator's warmup; Tier B: the
  NodePool's budget is `nodes: "0"`, then `10%` at t = W), so every arm starts from the same provisioned state.
- **Observation window:** T minutes after the drop. T is fixed before any evaluation-seed run, from Tier A WB runs
  on the pilot seeds 1-5 (all seven configurations, both sizes, a 45-minute window). Per run, the settle time is
  the last change in node count after the drop. T is the largest per-configuration 90th percentile of settle
  time, rounded up to 5 minutes, and clamped to 10-30. The calibration result is logged before any seed 101+ run.
- **Sizes:** n = 50 and n = 200, in the simulator's sense: W2's demand at n nodes.

## Configurations (seven)

| Label | What runs | NodePool policy |
| --- | --- | --- |
| B2 | gate off: upstream consolidation | WhenEmptyOrUnderutilized |
| B3 | gate off: upstream consolidation | Balanced |
| B4 | gate on: the session's `b4-pipeline` (fine upstream adapters, upstream planner and executor) | WhenEmptyOrUnderutilized |
| heuristic/entropy-greedy | gate on: the session's Exp 1 arm | WhenEmptyOrUnderutilized |
| heuristic/anneal-plan | gate on: the session's Exp 1 arm | WhenEmptyOrUnderutilized |
| random/anneal-plan | gate on: the session's Exp 1 arm | WhenEmptyOrUnderutilized |
| random/entropy-greedy | gate on: the session's Exp 1 arm | WhenEmptyOrUnderutilized |

The plan asks for "the top three configurations". They are fixed by a rule on session data: the three Exp 1
pipeline configurations with the lowest mean steady-state cost on W2 at 200 nodes, W2 being the session workload
WB derives from. In order: heuristic/entropy-greedy (26.61 $/hr), heuristic/anneal-plan (26.72) and
random/anneal-plan (27.40). random/entropy-greedy is added because it is the arm H1 and H7 center on. Pipeline
configurations are the session matrix's anchors verbatim (`experiments/matrix/session.yaml`): search budget 16 full
simulations per cycle and 500 attempts per generator, lambda 0.1, t0 1.

## Both tiers, alike

- **One NodePool.** It offers c7i, m7i and r7i, on-demand, Linux, up to 32 vCPUs: the session catalog's 15 types
  from `kwok/examples/aws_instance_types.json`. Tier B's KWOK provider loads that file
  (`INSTANCE_TYPES_FILE_PATH`), and the simulator builds its types the same way. The NodePool has
  `consolidateAfter: 30s` and a 10% disruption budget after warmup.
- **Scheduling.** Default kube-scheduler scoring (the simulator's `least-allocated` placement). Scale-down is by
  the ReplicaSet controller (the simulator's `spread` model approximates it).
- **Feature gates.** All arms run the same feature gates as the installed chart, plus `ConsolidationPipeline=true`
  for the pipeline arms and B4, and `=false` for B2 and B3.
- **Binary.** Tier A uses a simulator binary built at the commit that adds WB, from a clean clone. Tier B uses a
  controller image built by `ko` from that same commit.

## Metrics

Each is computed from one-minute samples over [W, W + T], the same way in both tiers.

| Metric | Definition | Tier B source |
| --- | --- | --- |
| `cost_end` | mean $/hr over the samples in the last 5 minutes | nodes' instance types x catalog prices |
| `cost_window` | integrated cost over [W, W + T], in $ | the same samples |
| `nodes_end` | node count at W + T | node list |
| `evictions` | pods evicted after the drop | pods carrying `DisruptionTarget` with reason `EvictionByEvictionAPI` |
| `settle_min` | minutes from the drop to the last node-count change | node list |
| `compute` | Tier A: CPU seconds in disruption passes; Tier B: controller process CPU seconds over [W, W + T] | controller `/metrics` |

Tier B also records controller memory (max and p95) and pending pods at the drop.

## Seeds, order and validity

- **Seeds.** 101-105 in both tiers, paired by seed, since the workload is identical per seed. Tier A also runs
  101-110 for reference.
- **Run order.** Tier B runs one spec at a time on one kind cluster. Rounds go by seed (101, then 102, ...). Within
  a round it runs n = 200 then n = 50, and the seven configurations in an order shuffled per round and size with a
  fixed seed (`experiments/tierb/plan.json`). An interrupted round is completed before the next one starts.
- **Stopping rule (time-only).** The first round to end after 36 hours of Tier B running is the last one.
- **Validity.** A Tier B run is invalid in any of these cases, and invalid runs are counted, reported and rerun
  once:
  - a pod is still pending at the drop (scale-out unfinished);
  - the controller restarted during the window;
  - the spec errored;
  - a PDB or `do-not-disrupt` violation (WB has neither, so none is expected).

## Analysis (descriptive)

For each size and metric, Kendall's tau-b between the two tiers' configuration orders, using means over seeds
101-105 (7 items). A configuration pair counts as a reversal when the two tiers order it differently and both
tiers' mean differences exceed a materiality floor. The floors are 1% for the cost metrics, 10% for evictions and
compute, and 2 minutes for settle time. Every reversal is reported as a finding. Two pairs are also reported
separately:
- random vs heuristic generation with the entropy-greedy planner (H1, H7);
- B4 vs B2 (parity on a real cluster).

Tier B's controller CPU and memory per configuration are reported beside Tier A's decision CPU. That speaks to
compute against decision quality, which Tier A measured but never combined. No hypothesis test is run: Tier B has
5 seeds and was not in the preregistration.
