# Consolidation experiments on a KWOK cluster, and a plan for a real EKS cluster

This document describes the one set of consolidation experiments that ran on a real Kubernetes control plane, what
was held fixed, what was measured, what it found, and what to run next on an EKS cluster when compute is available.
It assumes you know Karpenter's features (NodePools, consolidation policies, `consolidateAfter`, disruption
budgets, drift) but nothing about this research branch. The companion documents are
[`pipeline-refactor.md`](pipeline-refactor.md), on the consolidation pipeline the experiments exercise, and the
paper at [`../../paper/paper.md`](../../paper/paper.md), which covers the simulator experiments.

Everything here is reproducible from the branch: the protocol is `experiments/TIERB.md`, the cluster runs are under
`experiments/runs/tierb-kwok/` and the matching simulator runs under `experiments/runs/tierb/`, the tables are
`paper/tables/tierb_*.md`, the scripts are in `experiments/tierb/`, and the full history of decisions is in
`experiments/RESEARCH_LOG.md` from "Tier B unblocked" onward. (The research files call this experiment "Tier B"; this
document does not.)

## 1. Why run on a cluster at all

Most of the project's experiments ran in an in-process simulator (`hack/consolidation-sim`). It runs Karpenter's
real disruption and scheduling code against an in-memory fake API, but it *models* three things that matter for
consolidation:

- **where kube-scheduler puts a pod** that Karpenter evicts (the simulator scores nodes `least-allocated`, the
  kube-scheduler default, and also offers `most-allocated` and an oracle that follows the plan);
- **which replicas a ReplicaSet deletes** on scale-down (the simulator spreads deletions across nodes, as the
  ReplicaSet controller's heuristic does);
- **timing**: node launch, registration, pod startup and shutdown are drawn from fixed ranges, and the controller
  runs on a virtual clock.

A consolidation plan is a bet on all three. Karpenter simulates where the pods of a drained node will fit, but
kube-scheduler makes the real placement, and the ReplicaSet controller decides which pods survive a scale-down. If
the simulator got these wrong, its ranking of consolidation strategies could be wrong too. The cluster experiment
exists to check that ranking where the control plane is real.

**What is real in the KWOK cluster.** `kind` runs a genuine control plane: API server, etcd, kube-scheduler and
kube-controller-manager at Kubernetes 1.34. Karpenter is the real controller built from the branch, with every
controller running (provisioning, disruption, termination, drain through the Eviction API). KWOK replaces the
kubelet: its controller marks nodes Ready and pods Running without starting containers. So scheduling decisions,
ReplicaSet scale-down choices, Karpenter's own decisions and timing, API server latency, and the controller's CPU and
memory are all real.

**What is still fake.** There is no kubelet, no container runtime, no image pull, no readiness probe, no network
and no cloud API. A node "boots" after a configurable delay (`nodeRegistrationDelay` on the `KWOKNodeClass`), and
instance types and prices come from a JSON catalog. Pods become Running the moment KWOK sees them bound. Section 9
lists what a real cluster adds.

## 2. The cluster

| Item | Value |
| --- | --- |
| Machine | the same class of cloud VM across three boots (the VM changed at each of two container restarts; `experiments/tierb/machine-log.txt`), 4 vCPUs, 15 GiB RAM per the session's VM description, kept otherwise idle during the run |
| Control plane | `kind` v0.30.0, node image `kindest/node:v1.34.0`, one control-plane node |
| Fake nodes | KWOK v0.8.0, one controller instance (KWOK can shard nodes across several) |
| Zones | four fake zones, `us-west-2a` to `us-west-2d`, from the instance catalog |
| Karpenter | built with `ko` from branch commit `eff89a1`; the KWOK cloud provider (`kwok/`) |
| Controller resources | requests 1 CPU, 1 GiB; limits 2 CPU, 2 GiB; log level `info` |
| Feature gates | `NodeRepair`, `CapacityBuffer`, `StaticCapacity` on; `PredictionEnabled` off; `ConsolidationPipeline` set per configuration (section 4) |
| Instance types | 15: `c7i`, `m7i`, `r7i` at `large`, `xlarge`, `2xlarge`, `4xlarge`, `8xlarge`; on-demand only; $0.098 to $2.117 per hour, identical to the simulator's catalog (`experiments/catalog.json`) |
| Scheduler | kube-scheduler defaults (`LeastAllocated` scoring) |

Two workarounds were needed on this VM and are harmless elsewhere: containerd's `restrict_oom_score_adj` in the kind
config (the VM withholds `CAP_SYS_RESOURCE`), and loading the KWOK image from the host because the kind node could not
reach the VM's proxy. `experiments/tierb/env/setup.sh` recreates the cluster, KWOK, the controller image and the
binaries on a machine that already has Go, Docker and kubectl. In two end-to-end checks on the VM that had just run
the experiment (Go's caches and the kind and KWOK images already present) it took 316 s and then 174 s; a fresh
machine will take longer.

## 3. The workload

Each run replays one workload generated by the simulator's "diurnal" generator for a given **seed** and **size**.
The size is the simulator's notion of scale: a workload "at n nodes" has about the demand of n mid-size nodes at
70% fill. Because Karpenter picks larger instances, the fleet is much smaller than n.

| Size | Deployments | Pods at peak (seed 101; range over seeds) | Pods after the drop | Fleet at the drop (observed) |
| --- | --- | --- | --- | --- |
| 50 | 12 | 306 (306 to 491) | 118 (118 to 214) | 9 to 14 nodes |
| 200 | 50 | 1,790 (1,502 to 1,997) | 718 (633 to 825) | 40 to 50 nodes |
 Pods request 0.25 to 4 vCPU and 0.5 to 8 GiB in eight
fixed shapes. There are **no** PodDisruptionBudgets, topology spread constraints, anti-affinity rules or
`do-not-disrupt` annotations: this workload isolates consolidation from scheduling constraints (section 10 adds
them).

The timeline of one run:

1. **t = 0.** Every Deployment is created at its peak replica count. Only once every pod exists and is
   unschedulable is the NodePool created, so Karpenter's first provisioning pass sees the whole workload at once,
   as the simulator does.
2. **Warm-up, 5 minutes.** Karpenter provisions the fleet. Consolidation is held off (section 5), so every
   configuration starts the measured window from a comparable fleet.
3. **The drop, t = 5 min.** Every Deployment is scaled to its low count at once (each drops by 50 to 70%).
   Consolidation is switched on.
4. **Observation, 30 minutes.** Metrics are sampled every minute. The window length was fixed before any
   evaluation run, from simulator runs on pilot seeds: the 90th percentile of the time each configuration took to
   stop changing its node count was 30 to 43 minutes, and the protocol's rule caps the window at 30.

This compresses the simulator's 3-hour diurnal workload (whose deployments scale down one by one over an hour) into
35 minutes, so one run costs about 36 minutes of wall time and 70 runs fit in two days.

## 4. The seven configurations

| Label | What runs | NodePool `consolidationPolicy` | `ConsolidationPipeline` gate |
| --- | --- | --- | --- |
| **B2** | upstream consolidation, unchanged | `WhenEmptyOrUnderutilized` | off |
| **B3** | upstream consolidation, unchanged | `Balanced` | off |
| **B4** | the pipeline configured to reproduce upstream exactly (its "fine" upstream adapters, which re-implement upstream's search in stages, with upstream's planner and the orchestration queue; see [`pipeline-refactor.md`](pipeline-refactor.md)) | `WhenEmptyOrUnderutilized` | on |
| **heuristic/entropy-greedy** | upstream's candidate order and search, with the entropy-greedy planner | `WhenEmptyOrUnderutilized` | on |
| **heuristic/anneal-plan** | the same search, with simulated annealing at plan time | `WhenEmptyOrUnderutilized` | on |
| **random/anneal-plan** | random candidates and random merge/shift moves, annealing at plan time | `WhenEmptyOrUnderutilized` | on |
| **random/entropy-greedy** | random candidates and moves, the entropy-greedy planner | `WhenEmptyOrUnderutilized` | on |

B2, B3 and B4 are references. The three heuristic/random configurations after them are the three of Experiment
1's six pipeline configurations (generation {heuristic, random} x planner {savings-only, entropy-greedy,
anneal-plan}) with the lowest mean steady-state cost (the mean hourly fleet cost over the last 20% of the
simulator's 3-hour runs) on the diurnal workload at 200 nodes: 26.61, 26.72 and 27.40 $/hr, chosen by that rule
before the cluster runs. random/entropy-greedy is added because it is the configuration most of the
simulator's hypotheses center on. The pipeline configurations are copied verbatim from the simulator matrix; this is
random/entropy-greedy:

```yaml
pipeline:
  candidates: {name: uniform-random, params: {sourceFraction: 0.25}}
  moves:
    - {name: random-merge, params: {confirm: true}}
    - {name: random-shift}
  movesMode: pooled
  planner:   {name: entropy-greedy, params: {lambda: 0.1}}
  executor:  {name: shift-evict}
  potential: {name: util-entropy}
  search:    {maxFullSimulations: 16, maxAttempts: 500}
  seed:      <the run's seed>
```

heuristic/entropy-greedy differs only in its first two lines: `candidates: {name: upstream}` and
`moves: [{name: upstream-multi, params: {mode: fine}}, {name: upstream-single, params: {mode: fine}}]`. The
anneal-plan configurations replace the planner with
`{name: anneal-plan, params: {lambda: 0.1, t0: 1, cooling: 0.98, temperatureUnit: per-node}}`. See
[`pipeline-refactor.md`](pipeline-refactor.md) for what these names mean.

## 5. What was held fixed, and what varied

The experiment is a paired comparison: for each seed and size, all seven configurations replay the same workload,
so differences between configurations within a seed are not workload noise. The table lists each controlled
variable and how it was controlled.

| Controlled variable | How |
| --- | --- |
| Workload | generated from the seed by the simulator's generator; the same Deployments, shapes and replica counts in the simulator and on the cluster |
| Instance types and prices | the 15-type catalog mounted into the controller (`INSTANCE_TYPES_FILE_PATH`); prices checked equal to the simulator's |
| NodePool | one NodePool: Linux, on-demand, families `c7i`/`m7i`/`r7i`, up to 32 vCPU; `consolidateAfter: 30s`; disruption budget `nodes: 10%` |
| Consolidation during warm-up | held off with `consolidateAfter: Never` and a `nodes: "0"` budget, both patched at the drop. A zero budget alone is not enough: the pipeline's shift moves remove no node, so a node budget does not stop them |
| Provisioning | the NodePool is created only after every pod is pending, so provisioning is one batch rather than a sequence of batches that depends on how fast the ReplicaSets create pods |
| Node registration delay | `nodeRegistrationDelay` on the `KWOKNodeClass`: `0s` during warm-up, patched to `47s` at the drop. 47 s approximates the simulator's mean launch-plus-registration time of 48.5 s (launch 2 to 5 s plus registration 30 to 60 s), less the second or so KWOK itself takes to register a node. During warm-up it is zero so that pods bind before Karpenter's next 10-second provisioning pass; with 47 s there, the number of extra NodeClaims Karpenter added depended on timing and the arms of one seed started from different fleets |
| Randomness | the pipeline's seed is the run's seed, as in the simulator |
| Scheduler and scale-down | kube-scheduler defaults; the ReplicaSet controller's own scale-down |
| Feature gates | the same for every configuration except `ConsolidationPipeline` |
| Controller | restarted before every run (a nonce in its environment) so no state carries over; `info` logging for every configuration, because debug logging would inflate the CPU being measured |
| Load | one run at a time; nothing else running on the VM |
| Binary | one controller image for the whole run (digest recorded in every run's manifest); the simulator side uses a binary from the same commit |

Two differences from the simulator were left in place and judged harmless: the kind network plugin puts one small
DaemonSet pod (100 millicores, 50 MiB) on every node, which the simulator does not have, and the `NodeRepair`,
`CapacityBuffer` and `StaticCapacity` gates were on, which the simulator does not run; the workload never sets the
conditions they act on.

What varied: the **configuration** (seven), the **seed** (101 to 105) and the **size** (50 and 200 nodes). Seeds
101 to 105 had never been used for calibration.

## 6. Procedure

- **Order.** Runs went seed by seed (101, then 102, ...). Within a seed, all seven configurations ran at 200
  nodes, then all seven at 50, in an order shuffled per seed and size with a fixed random seed
  (`experiments/tierb/plan.json`). A started seed was always finished.
- **Validity.** A run is invalid if any of these holds: the test spec exited non-zero; a pod was still pending at
  the drop; a pod was evicted before the drop; the controller restarted during the run; the decisions came from
  the wrong code path (a gate-on configuration must show pipeline evaluations and no multi-node or single-node ones
  in the controller's metrics, and a gate-off configuration the reverse); or the metrics poller collected no
  samples. An invalid run is rerun once; a second failure abandons the cell. A run interrupted by the machine does
  not count as an attempt.
- **Outcome.** All 70 runs were valid on their first counted attempt. Two attempts were interrupted by restarts of
  the VM's container and rerun. The whole run took 43 h 44 min (Oct 29 to 31, 2026), 36 to 37 minutes per cell.
  Three VM boots were involved; the analysis records each run's boot and reports controller CPU with and without
  the two seed-size blocks that straddle a boot.

## 7. What was measured

All metrics are computed from one-minute samples over the 30-minute window, the same way for the cluster runs and
the simulator runs; "cost at the end" is this window's analogue of the paper's steady-state cost. A difference is
*resolved* when its paired 95% interval over the five seeds excludes zero.

| Metric | Definition | Measured on the cluster from |
| --- | --- | --- |
| Cost at the end | mean $/hr over the last 5 minutes | the fleet's instance types times catalog prices |
| Relative cost at the end | cost at the end over cost at the drop | the same |
| Integrated cost | $/hr integrated over the window, in $ | the same |
| Nodes at the end | node count at t = 35 min | node list |
| Evictions | pods evicted after the drop | pods carrying the `DisruptionTarget` condition with reason `EvictionByEvictionAPI` |
| Settle time | minutes from the drop to the last change in node count | node list |
| Controller CPU | CPU seconds of the whole controller process over the window | the controller's `/metrics` endpoint, polled through the API server |
| Decision evaluation time | wall-clock seconds in `karpenter_voluntary_disruption_decision_evaluation_duration_seconds`, for consolidation only | `/metrics` |
| Controller memory | peak and 95th-percentile resident memory | `/metrics` |

Decision evaluation time needs a caveat. The histogram times each whole disruption attempt, which includes the
fixed 15-second wait before Karpenter validates a command. For upstream consolidation, that wait alone is about 65%
of the column. It measures how long the controller is busy with consolidation, not CPU.

## 8. Results

**How well does the simulator's ranking hold?** Kendall's tau-b compares the order of the seven configurations'
means on the cluster with their order in the simulator (1 is the same order, 0 no relation, -1 the reverse). A
*material reversal* is a pair of configurations the two orders disagree on by more than a floor: 1% for cost, 10%
for evictions and controller CPU, 2 minutes for settle time.

| Metric | tau-b, 50 nodes | tau-b, 200 nodes | Material reversals (50 / 200) |
| --- | --- | --- | --- |
| Cost at the end | 0.20 | 0.33 | 5 / 3 |
| Relative cost at the end | 0.20 | 0.33 | 4 / 3 |
| Integrated cost | 0.29 | 0.62 | 5 / 0 |
| Nodes at the end | 0.69 | 0.16 | no floor |
| Evictions | 0.49 | 0.81 | 0 / 0 |
| Settle time | 0.49 | 0.49 | 1 / 0 |
| Compute (simulator: decision CPU; cluster: controller CPU) | 0.33 | 0.52 | 0 / 0 |

**The coarse order holds.** In both the simulator and the cluster, the two random-generation configurations have
the two highest integrated costs at both sizes, and at 200 nodes the two highest end costs and the most controller
CPU. Balanced (B3) evicts the fewest pods at both sizes.

**The fine order is not established, by either side.** Seventeen of the 21 reversals are among the five
configurations whose simulator costs lie within 5% of one another (B2, B3, B4 and the two heuristic pipeline
arms). At five seeds, the simulator itself does not separate any cost difference among those five: every paired
95% interval includes zero. The cluster separates seven of them, all at 200 nodes, for example Balanced ending
1.5% dearer than B4, and heuristic/anneal-plan 4.2% dearer than B2. No reversal is resolved on both sides, and in
none of the 94 differences the simulator does resolve does the cluster resolve the opposite direction.

**The two named comparisons.**

- *Random vs heuristic generation with the same planner* (the central question of the simulator experiments). On
  the cluster, random generation ends 10.6% dearer at 200 nodes (19.3% in the simulator), resolved at five seeds;
  integrates 23% more cost (28%); and uses 52% more controller CPU (the simulator's decision CPU over the same
  30-minute window: 101% more). At 50
  nodes the signs agree but the end-cost difference (+0.9%) is not resolved. Every metric has the same sign in
  both places.
- *The pipeline reproducing upstream (B4) vs upstream (B2)*. No difference is resolved on the cluster. That is
  consistent with the parity the pipeline claims, but the cluster's own run-to-run variation is too large to
  establish it: the 95% interval on end cost is about ±7% at 50 nodes and ±2.9% at 200.

**Compute against decision quality at 200 nodes** (means over five seeds):

| Configuration | $/hr at the end | Evictions | Controller CPU, core-seconds | Decision evaluation, s | Peak memory, MB |
| --- | --- | --- | --- | --- | --- |
| B2 (upstream) | 26.66 | 545 | 149 | 415 | 216 |
| B3 (Balanced) | 27.14 | 473 | 136 | 389 | 200 |
| B4 (pipeline as upstream) | 26.74 | 575 | 154 | 462 | 227 |
| heuristic/entropy-greedy | 26.72 | 527 | 145 | 527 | 254 |
| heuristic/anneal-plan | 27.79 | 763 | 149 | 620 | 287 |
| random/anneal-plan | 31.27 | 943 | 228 | 904 | 346 |
| random/entropy-greedy | 29.57 | 651 | 219 | 886 | 341 |

Random generation is worse on cost and compute together: 219 to 228 core-seconds against 136 to 154 for the other
five, 1.4 to 2.3 times their decision-evaluation time, and the most memory. Among the other five, controller CPU
differs by at most 13%, and heuristic/entropy-greedy matches upstream's end cost and evictions within the cluster's
resolution while spending more time in decision evaluation and more memory. At 50 nodes the controller's baseline
work dominates: every configuration used 45 to 51 core-seconds over the window, under 3% of one core.

**Noise the cluster adds.** Two sources were found and are worth planning for:

- *The starting fleet varies.* Although consolidation is held off during warm-up, the configurations of one seed
  started the window from fleets differing by up to 10% in cost at 50 nodes and 4.5% at 200. kube-scheduler binds
  pods in its own order rather than Karpenter's planned order, which changes how many follow-up NodeClaims
  provisioning adds. The relative-cost metric removes the level of the starting fleet and gives the same tau-b.
- *Identical decisions do not give identical runs.* B4 and B2 decide identically in the simulator in every seed at
  50 nodes, yet differed by 10% in end cost in one seed on the cluster.

With five seeds, the median 95% interval on end cost across configuration pairs was about ±5% on the cluster and
±7% in the simulator. Differences of a few percent need many more seeds (section 10).

## 9. What a real cluster adds beyond KWOK

Each row is something the KWOK cluster fakes or lacks and a real cluster supplies, with the consolidation behavior
it affects.

| Real on EKS, fake or absent on KWOK | What it changes |
| --- | --- |
| Node boot: EC2 launch, AMI boot, kubelet registration, CNI readiness | replacement latency during consolidation; how long a replace command keeps two nodes running; whether the 47-second stand-in was realistic |
| Pod startup: image pulls, init containers, readiness probes | how long a workload is below its replica count during a move; pending-pod time; whether `terminationGracePeriodSeconds` matters |
| Drain: real termination, PDB-blocked evictions, graceful termination | drain duration; how often validation rejects a command because the cluster changed during the 15-second wait |
| kubelet resource accounting, DaemonSets, system pods | allocatable capacity and the room consolidation actually has |
| Capacity: insufficient-capacity errors, instance type availability per zone | failed launches and retries; the replacement types actually chosen |
| Prices: live on-demand (and optionally spot) prices from the provider | real dollar savings; spot-to-spot consolidation rules |
| Zones: real AZs with real subnets | zonal topology spread, volume affinity, cross-zone replacements |
| API server under real kubelet load | controller latency and decision-evaluation time |
| Cost: a bill | the only end-to-end check of the dollar figures |

What EKS does not change: the controller code, the NodePool API, and the two things the cluster experiment already
made real (kube-scheduler placement and ReplicaSet scale-down).

## 10. A plan for EKS

### Goals

1. **Rank agreement with real node lifecycle.** Does the coarse order from section 8 hold when nodes really boot,
   pods really start and drains really take time? This is the same question the KWOK experiment asked, one layer
   down.
2. **Real cost and real disruption.** Dollars from instance-hours, and pod unavailability (replica-seconds below
   the desired count) per configuration. Neither exists on KWOK.
3. **Validation under real timing.** How often Karpenter rejects a planned command after its 15-second wait, and
   how often a pipeline shift aborts because the cluster changed, when node boots and drains take real time.
4. **Scheduling constraints.** How much topology spread, pod anti-affinity, PDBs and `do-not-disrupt` raise the
   node count a workload needs, and whether the configurations differ under them. The simulator ran one such
   workload once (the paper, section 3.4); nothing has measured it on a cluster.
5. **Controller cost at scale.** CPU and memory of the controller at 200 and 500 nodes, with real API server load.

### Safety rules

- A sandbox AWS account used for nothing else, with a hard spend cap (an AWS Budgets action that denies
  `ec2:RunInstances` at the cap) and an alarm at half of it. Never a shared or production cluster.
- NodePool `limits` on CPU, so a runaway provisioning loop cannot exceed the largest planned fleet.
- Every run time-boxed by the test harness, and the cluster torn down by a scheduled job if the harness dies. Check
  that no instances remain after teardown.
- On-demand only in the first stages, so prices and interruptions do not add variance; spot is a later stage.

### Cost estimate

The KWOK runs give the fleet cost per run directly, because the catalog prices are on-demand EC2 prices. At 200
nodes a run integrated $16 to $22 of fleet cost over its 30-minute window, plus about $6 for the 5-minute warm-up
at the peak fleet ($69/hr): about $25 per run. At 50 nodes, about $6. Replicating the KWOK matrix (7
configurations x 2 sizes x 5 seeds) is therefore about 35 x $25 + 35 x $6, roughly $1,100 of EC2 at on-demand
prices, plus the EKS control plane and the idle time between runs. Doubling the seeds at 200 nodes adds about $875.
A 3-hour diurnal run at 200 nodes costs about $140 (the simulator's integrated cost for that workload, $130 after
its 10-minute warm-up plus the warm-up at the peak fleet). These are
instance costs only; EBS, data transfer and the control plane are small beside them.

### Stages

Each stage reuses the KWOK experiment's protocol (sections 3 to 7) unless a row below says otherwise. Run the
stages in order; each one's smoke results decide whether the next is worth its money.

| Stage | What | Configurations | Seeds | Size | Purpose and rough cost |
| --- | --- | --- | --- | --- | --- |
| 0. Smoke | one run of the compressed diurnal workload | B2 | 1 | 50 | prove the harness, the image, the NodeClass, the cost accounting and teardown; under $10 |
| 1. Replication | the KWOK matrix as run | all seven | 101 to 105 | 50 and 200 | rank agreement across simulator, KWOK and EKS; the real-cost and unavailability columns; about $1,100 |
| 2. Resolution | more seeds at 200 nodes | B2, B3, B4, heuristic/entropy-greedy | 106 to 125 | 200 | narrow the interval on the near-tied configurations to about ±2.1% (25 seeds in all, from the ±4.7% five-seed interval scaled by one over the square root of the seed count; resolving 2% needs about 30 seeds and 1% about 110); about $2,000 |
| 3. Constraints | the simulator's constrained diurnal workload: half the Deployments carry a PDB (`maxUnavailable: 1`), 30% a hard hostname topology spread (`maxSkew: 2`), 10% of the rest required hostname anti-affinity, 5% `do-not-disrupt` | B2, B3, heuristic/entropy-greedy, random/entropy-greedy | 101 to 105 | 200 | the fleet floor constraints impose and the strategies' behavior against it; the random arm is included because shifts and PDBs interact; about $500 at the unconstrained fleet's cost, $540 to $560 if the constrained fleet costs what it did in the simulator (7% more per hour at peak demand, 9% more integrated after warm-up and 11% over the whole of the one constrained run), and budget more, since that fleet peaked at 5.7 times the nodes and settled at 3.4 times |
| 4. Full diurnal | the simulator's 3-hour diurnal workload (Deployments scale down one by one over an hour) | B2, B3, heuristic/entropy-greedy | 101 to 105 | 200 | integrated cost over a realistic timeline, in dollars; about $2,100 (15 runs at $140) |
| 5. Scale | the compressed workload at 500 nodes | B2, B3, heuristic/entropy-greedy, random/entropy-greedy | 101 to 103 | 500 | controller CPU, memory and decision-evaluation time with real API server load; whether any pass approaches upstream's 1-minute multi-node limit; about $750 (12 runs at roughly 2.5 times the 200-node fleet cost, the simulator's cost scaling about linearly with size), more if the window is lengthened for 500 nodes |

### What to port

- **The test spec.** `test/suites/performance/tierb_test.go` drives one run against the KWOK provider's test
  environment. It needs a variant for the AWS provider's test environment (`aws/karpenter-provider-aws`,
  `test/pkg/environment/aws`), which supplies a default `EC2NodeClass`. The spec has two KWOK-specific pieces: the
  `nodeRegistrationDelay` it sets on the NodeClass (drop it, since EC2 provides the real delay) and the KWOK
  provider's label keys in the NodePool requirements (`karpenter.kwok.sh/instance-family` and
  `karpenter.kwok.sh/instance-cpu`, imported from `kwok/apis/v1alpha1`; replace them with
  `karpenter.k8s.aws/instance-family` and `karpenter.k8s.aws/instance-cpu`). Everything else (the Deployments, the
  drop, the sampler, the eviction counter, the metrics poller, the validity checks) is provider-independent.
- **The NodePool.** Keep the requirements (Linux, on-demand, `c7i`/`m7i`/`r7i`, `karpenter.k8s.aws/instance-cpu`
  below 33) and the `consolidateAfter`/budget patches. Pin one AMI (an `amiSelectorTerms` by ID) for the whole
  experiment and record it.
- **Prices.** The AWS provider fetches on-demand prices itself. Record the prices it used for every instance type
  that appeared, so the cost columns are reproducible, and compare them with the catalog's.
- **The driver.** `experiments/tierb/run_tierb.py` runs cells in order, enforces validity and reruns; point its
  `PERF_TEST` at the ported spec and lift its 36-hour stopping rule (`STOP_AFTER_H`) or use a fresh run directory.
- **The workload image.** Use a tiny image that starts in under a second (for example `pause`) in stages 1, 2
  and 5, so pod startup does not add variance; use a realistic image with a readiness probe in stages 3 and 4,
  where startup time is part of what is measured.

### Additional controlled variables on EKS

In addition to the table in section 5: one AMI ID; one Kubernetes minor version; one AZ set (either the four zones
KWOK offered, or a single AZ, recorded either way); DaemonSets limited to the CNI and kube-proxy; no cluster
autoscaler, no other NodePools, no other workloads; the EKS control plane left to its defaults; the Karpenter image
digest; and the same controller resource requests and log level as on KWOK. Run one cell at a time, and leave at
least `consolidateAfter` plus the drain grace period between cells so no NodeClaim from the previous cell is still
terminating when the next one provisions.

### Additional measurements on EKS

| Metric | Source |
| --- | --- |
| Node boot time | NodeClaim `Launched` to Node `Ready`, per NodeClaim, from their conditions |
| Pod unavailability | replica-seconds below the desired count per Deployment, from pod Ready transitions |
| Drain duration | per terminated node, from the `karpenter.sh/disrupted` taint to node deletion |
| Validation rejections and timeouts | `karpenter_voluntary_disruption_failed_validations_total`, `..._consolidation_timeouts_total` |
| Launch failures | NodeClaim events (`InsufficientCapacity` and similar) |
| API server load | `apiserver_request_total` by verb and resource |
| Dollars | instance-hours times the recorded on-demand prices, cross-checked against the account's bill for the day |
| The decision log | the controller's structured `consolidation decision` lines, written at verbosity 1 (`--log-level debug`), on one seed per configuration; the KWOK runs collected none, because debug logging inflates the CPU being measured, so keep these runs out of the CPU comparison |

### Validity rules on EKS

The six KWOK rules, plus: no NodeClaim launch failure during the run; no spot interruption (on-demand makes this
moot); every pod image already present on the AMI or pulled during warm-up; and a cell whose node boot times fall
outside the recorded range of the other cells (for example because of an EC2 incident) is set aside and rerun.

### Analysis

Extend the analysis module (`paper/analysis/tier_b.py`, written for exactly two tiers, the simulator's `tierb-a` runs
and the cluster's `tierb-kwok` runs) to read the EKS runs as a third tier and produce the same tables: rank agreement
with Kendall's tau-b per metric and size, material reversals, the two named comparisons, and per-seed paired
differences with 95% intervals. Report EKS against both the simulator and KWOK, so that any reversal can be attributed
to node lifecycle (EKS vs KWOK) or to placement and scale-down (KWOK vs simulator). Treat the result as descriptive
unless a hypothesis is registered before the runs; with five seeds the intervals will be several percent wide, as they
were on KWOK.

### Runbook sketch

```bash
# 1. Account: spend cap, alarm, sandbox credentials
# 2. Cluster: eksctl or the AWS provider's getting-started steps; one node group for Karpenter itself
# 3. Image: build from this branch with ko into ECR; install the AWS provider's chart with that image,
#    the same controller resources and log level as experiments/tierb/env/helm-values.yaml
# 4. NodeClass: an EC2NodeClass with a pinned AMI, subnets in the chosen AZs, the cluster's security group
# 5. Spec: the ported tierb_test.go compiled with go test -c
# 6. Driver: TIERB_HOME=/tmp/eks experiments/tierb/run_tierb.py smoke --label B2 --nodes 50   # stage 0
#            ... run                                                                          # stages 1+
# 7. After each stage: commit the run directories, make figures, read tierb_*.md
# 8. Teardown: delete NodePools (wait for all NodeClaims to go), uninstall Karpenter, delete the cluster,
#    confirm no instances remain, check the bill
```

The KWOK runbook (`docs/research/runbook.md`, section 4) has the exact driver commands and the commit script; the
EKS variant changes only steps 2 to 4.
