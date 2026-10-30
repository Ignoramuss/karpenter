# Runbook: full profile, KWOK validation and an EKS sandbox pass

Everything here runs from the repository root on the branch that carries `docs/research/PLAN.md`. The session
profile (Tier A at 50, 200 and 1,000 nodes, 10 seeds) ran inside the cloud session; this runbook covers what it did
not: the full profile, Tier B on KWOK (run after the session, section 4), and an optional Tier C pass on EKS.
Session results and their caveats are in `experiments/RESULTS.md`; the preregistered analysis is in
`experiments/PREREGISTRATION.md`.

## 0. Prerequisites

- Go at the version in `go.mod` (`GOTOOLCHAIN=auto` fetches it).
- For `make verify`: mikefarah's Go `yq` v4 on PATH (`go install github.com/mikefarah/yq/v4@v4.44.3`); the
  Python `yq` some distributions ship fails in `hack/validation/*.sh`.
- Python 3.13 with the pinned analysis packages:

  ```bash
  python3 -m venv /opt/research-venv
  /opt/research-venv/bin/pip install -r paper/requirements.txt
  ```

- For Tier B: Docker, Go, `kubectl`, and network access to `registry.k8s.io` (KWOK), Docker Hub (the kind node
  image), `cgr.dev` (the `ko` base image), GitHub (KWOK's manifests) and `get.helm.sh`. Both registries were
  blocked in the session itself and opened afterwards. `experiments/tierb/env/setup.sh` installs kind, ko and helm.
- Build one simulator binary per commit and use it for a whole matrix, so every cell's manifest carries one SHA.
  To extend the session's runs (reused cells, the cut groups), build the Gate 3 commit in a separate clone. Do not
  use a `git worktree`: its `.git` is a file, so Go writes no VCS stamp and `binary_revision` reads "unknown".

  ```bash
  git clone . /tmp/csim-c78b5f9 && git -C /tmp/csim-c78b5f9 checkout --detach c78b5f95063c1995b10fa6d66571a16d0a32b243
  (cd /tmp/csim-c78b5f9 && go build -o bin/csim ./hack/consolidation-sim)
  mkdir -p bin && cp /tmp/csim-c78b5f9/bin/csim bin/csim-c78b5f9
  SHA=c78b5f9
  # For a fresh profile at the current commit instead, from a clean tree:
  #   SHA=$(git rev-parse --short HEAD) && go build -o bin/csim-$SHA ./hack/consolidation-sim
  ```

## 1. Tier A, full profile

`experiments/matrix/full.yaml` is the session matrix with 20 seeds per cell (101-120) plus 5,000-node cells for
Exp 1 and Exp 7 (3,900 cells: 3,480 at 20 seeds, plus 360 Exp 1 and 60 Exp 7 cells at 5,000 nodes). Its first ten
seeds have the session cells' run IDs, so session results can be copied in instead of rerun. Then run the rest with
the Gate 3 binary (`SHA=c78b5f9` above), so copied and new cells share one `binary_revision`:

```bash
mkdir -p experiments/runs/full
cp -r experiments/runs/session/*__* experiments/runs/full/    # optional: reuse the session's 1,551 cells
```

Run it in chunks; the runner skips finished cells, retries a failed cell once, and exits 0 when its wall budget is
spent, so re-invoke it until it reports `0 pending`:

```bash
until bin/csim-$SHA matrix --matrix experiments/matrix/full.yaml --workers $(nproc) --max-wall 2h \
    | tee -a full.log | grep -q ' 0 pending'; do :; done
```

Sizing (wall time measured in the session on 4 vCPUs, one cell per vCPU). A 200-node cell takes 0.07-45.7 minutes,
W5 aside (1,070 cells). Most W1, W2 and W4 cells take 1-5 minutes; B0 and B5 take under 1 minute on W2 and up to 3.1
on W3, and the slowest W2 pipeline arms take about 13. W3 is slower: its pipeline arms take 3-19.5 minutes and its
B2 and B3 baselines 6.6-45.7. A 1,000-node pipeline cell takes 7-26 minutes. No 1,000-node B2 or B3 cell ran in the
session. The pilot's one 1,000-node B2 run (W1, sharing the VM) hit its 100-minute wall limit at simulated minute
160, so budget over two hours of one core per 1,000-node B2 or B3 cell (`experiments/PILOT.md`; research log, "B2 at
1,000 nodes is the expensive cell"). On W3, B2 and B3 are the slowest cells, because upstream single-node
consolidation simulates every candidate each pass. At 1,000 nodes the pilot's one B2 run (W1) took over 100 minutes,
against 7-26 minutes for a session pipeline cell; no 1,000-node B3 cell has been timed. On W1 and W4 at 200 nodes
they fall within the range of the slower pipeline arms: B2's slowest cells (4.9 and 3.3 minutes) are just above the
slowest pipeline cells (4.6 and 3.2), and B3's (3.0 and 2.2) are below them. On W2 they are faster than the slowest
pipeline arms (1.2-2.7 minutes). Memory per cell was not recorded; all 1,551 session cells finished without running
out of memory, four at a time on the 15 GiB VM. W5 is the exception: its one finished session cell (B2, 200 nodes,
seed 101) took 63.5 minutes, and a 30-minute probe of the same cell took 16 minutes. The cause is a simulator cost
(`experiments/RESEARCH_LOG.md`, "W5 timing probe"): upstream topology counting lists pods by label selector for each
of W5's hostname spread and anti-affinity groups, and the controller-runtime fake client deep-copies every pod and
round-trips the list through JSON on each call. With the Gate 3 binary, budget more than an hour per W5 B2 cell: the
one that finished took 63.5 minutes, and the three B2 cells launched with it ran about 64 minutes without finishing
before they were stopped. No B3 or pipeline W5 cell has been timed. Serving label-selector pod lists from the
harness's pod index (`pkg/controllers/disruption/pipeline/harness/env.go`, `interceptors`) should remove most of
that cost. Before using it, verify that decisions are unchanged with a byte-identical rerun of the finished W5 cell
(`realism-w5__arm-b2_seed-101__421f66a6`): the decompressed decision log, `timeseries.csv`, `podsets.jsonl` and the
simulated `summary.json` fields should match the session's. Also check that the new binary still passes
`TestDeterminism`, which checks only run-to-run determinism within one binary. A fixed binary has a new
`binary_revision`, so its cells are invalid in the session tree under the preregistered binary rule. They need their
own run tree and a logged deviation. 5,000-node cells are untested; expect several hours each for B2 and give each
worker 8 GB. A 64-core machine should finish the full profile in about a day. Keep `--workers` at or below the core
count: CPU time per pass is a measured outcome (H7), and oversubscription and co-runners shift it.

### The session's cut groups

The session cut four groups on time alone: `realism-w5` under a time-only overrun rule declared in the session, and
the other three with it (`experiments/session-driver/cut-decisions.txt`; RESULTS deviation 7). With those groups,
H1 gets its realism TOSTs (W5, W6), H7-B2 gets data, H2 gets its W6 tests and its 1,000-node B2 and B3 runs, and
Exp 8 gets its 1,000-node B2 and B3 rows. To run only the three that are not timing comparisons, into the session
tree:

```bash
bin/csim-c78b5f9 matrix --matrix experiments/matrix/session.yaml --workers $(nproc) \
  --only realism-w5,realism-w6,exp1-baselines-n1000
```

Use the Gate 3 binary (`c78b5f9`, built as in section 0 from that commit), so the new cells match the session's
`binary_revision`; `make figures` checks this. Run W5 with the Gate 3 binary too, and budget more than an hour per
W5 B2 cell (above), or use a fixed binary in its own run tree under a logged deviation.

H7-B2 tests CPU time per pass (mean and p95) for B2 against the session's random/entropy-greedy W1 1,000-node cells.
Those cells ran on VM C at about 4-way load, beside other random-generation cells (for the first seeds partly
random/savings-only). A B2 run on another machine or at another load is not directly comparable: in H7-S, random's
CPU time per pass was 12% higher on VM D beside a mix of both arms than on VM C beside random cells, with the
machine and the co-runners changing together. So run `exp7-b2-n1000` in its own invocation beside a rerun of those
random cells, interleaved by seed at 4-way load, the way `experiments/matrix/h7-timing.yaml` pairs H7's arms. That
takes a new matrix: `--only` selects whole groups, so `session.yaml` cannot run just the W1 random cells
(`exp1-n1000` has 180 cells), and it runs groups in file order with each group's cells in a fixed order, so it
cannot interleave them by seed with B2. In the session tree the finished random cells would also be skipped, since
the runner skips any cell that has a `summary.json`. It also takes an analysis change, logged as a deviation the way
H7-S was. The analysis takes a run's group from its run-ID prefix, and H7-B2 compares B2 only with `exp1-n1000`'s
session random cells, so the new groups need an alias (`analysis.group_aliases` in
`paper/analysis/experiments.yaml`), H7-B2 must point at the reruns, and the new matrix must join
`SESSION_BINARY_MATRICES` in `paper/analysis/common.py` for the binary check. Or run `exp7-b2-n1000` alone from
`session.yaml` into the session tree and report H7-B2 as a cross-machine comparison with each run's `machine_epoch`,
as H7 is. First append the new machine to `experiments/session-driver/machine-log.txt`, in the format
`run_session_v4.sh` writes (boot ID, boot time, CPU, nproc). `bin/csim` does not log the machine, and without that
line its runs are labeled with the last logged VM (D). The preregistered analysis covers the three non-timing groups
and this cross-machine option unchanged.

## 2. Analysis and figures

```bash
make figures          # runs paper/make_figures.py over experiments/runs; tables in paper/tables, figures in paper/figures/out
/opt/research-venv/bin/python paper/make_figures.py --runs experiments/runs/full --out paper/full
```

`make figures` is deterministic: two runs over the same raw data produce byte-identical outputs. The B6 lower bounds
are cached in `paper/tables/cache/`; delete the cache to recompute them.

## 3. Parity at scale

Gate 1 ran 1,000 states per policy and catalog (all cells matched). The 10,000-state run is split into four chunks,
one per policy and catalog, in `experiments/parity/chunks/`. The WhenEmptyOrUnderutilized/kwok chunk ran before the
session and the other three after it (Oct 29 02:17-05:18 UTC). All four matched 10,000 of 10,000 states in every
configuration (`docs/research/parity.md`). To rerun one chunk, or all four in one command:

```bash
PARITY_STATES=10000 PARITY_SEED=2 PARITY_POLICIES=Balanced PARITY_CATALOGS=kwok \
  PARITY_OUT=$PWD/experiments/parity/chunks/parity-10000-seed2-balanced-kwok.jsonl \
  go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 30h -v
PARITY_STATES=10000 PARITY_SEED=2 PARITY_OUT=$PWD/experiments/parity/parity-10000-seed2.jsonl \
  go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 30h
```

`docs/research/parity.md` explains the comparison, including how multi-NodePool states, where upstream is itself
nondeterministic, are judged.

## 4. Tier B: KWOK on kind

Tier B replays the simulator's WB workload on a kind cluster whose nodes are KWOK's fake nodes, so the API server,
kube-scheduler, the ReplicaSet controller and Karpenter are real. The protocol is `experiments/TIERB.md`. The run's
history and every change to it are in `experiments/RESEARCH_LOG.md`, from "Tier B unblocked".

1. **Environment.** From the repository root, with Docker running and Go on PATH:

   ```bash
   TIERB_HOME=/tmp/tierb experiments/tierb/env/setup.sh
   ```

   The script deletes and recreates the kind cluster `test-cluster` (`KIND_CLUSTER_NAME` overrides it). It installs
   kind v0.30.0, ko v0.18.0 and helm v3.19.0 into `$TIERB_HOME/bin`. From a clean clone at the image commit
   (`IMAGE_SHA`, default `eff89a1`) it builds the simulator, the controller image (with `ko`) and the chart. The
   Tier B spec (`test/suites/performance/tierb_test.go`) is compiled from the checkout. The cluster runs
   `kindest/node:v1.34.0` with KWOK v0.8.0. The controller's chart values are `experiments/tierb/env/helm-values.yaml`:
   - info logging, because debug logging would inflate the controller CPU being measured;
   - no ServiceMonitor, since no Prometheus operator is installed;
   - a 1-CPU request and a 2-CPU limit;
   - the session catalog's 15 types, from `experiments/tierb/env/instance_types.json`, mounted as
     `INSTANCE_TYPES_FILE_PATH`. `instance_types.py` in the same directory regenerates that file and checks its
     prices against `experiments/catalog.json`.

   Two steps are workarounds for the cloud VM, and harmless elsewhere:
   - the kind config's containerd patch (`restrict_oom_score_adj`), for a host that withholds `CAP_SYS_RESOURCE`;
   - loading the KWOK image from the host, for a node that cannot reach the host's proxy.

   The VM also needed `dockerd` started by hand (`setsid nohup dockerd &`).
   The script ends by writing `$TIERB_HOME/env.sh`, the driver's environment.

2. **Tier A side.** `experiments/matrix/tierb.yaml` holds the same seven configurations on WB:
   - `tierb-cal`: pilot seeds 1-5 with a 45-minute window. It was used once, to fix the window T = 30 minutes.
   - `tierb-a`: seeds 101-110.

   Use the simulator `setup.sh` built, so both tiers share one commit:

   ```bash
   $TIERB_HOME/bin/csim matrix --matrix experiments/matrix/tierb.yaml --workers 4
   ```

3. **Run.** The driver runs the 70 cells (7 configurations x n = 50 and 200 x seeds 101-105), one at a time, in the
   order in `experiments/tierb/plan.json`. Each cell takes 36-37 minutes. TIERB.md gives the validity checks, the
   reruns and the stopping rule.

   ```bash
   # optional: one short check on a pilot seed, written outside the run tree
   experiments/tierb/env/run-driver.sh smoke --label B2 --nodes 50
   setsid nohup experiments/tierb/env/run-driver.sh run >> /tmp/tierb/driver.log 2>&1 < /dev/null &
   ```

   - Re-invoke `run` after any interruption: finished cells are skipped and an interrupted one starts over.
   - Progress is in `experiments/tierb/run.log`.
   - Each cell writes `cell.json`, `manifest.json`, `summary.json`, `timeseries.csv` and `test.log.gz` under
     `experiments/runs/tierb-kwok/<run_id>/`.
   - To commit cells as they finish, list `experiments/runs/tierb-kwok/` in `.git/info/exclude` and run
     `experiments/tierb/commit_tierb.sh`. It adds only cells whose manifest says `done`, so a cell still running
     is never committed.
   - Keep the machine otherwise idle: Tier B measures the controller's CPU.

4. **Analysis.** `make figures` includes `paper/analysis/tier_b.py`. It uses only complete (seed, n) blocks. Per
   size and metric, it reports Kendall's tau-b between the two tiers' configuration orders, the material
   reversals, and the two named pairs. Its outputs are `paper/tables/tierb_*` and `paper/tables/tier_b.json`.
   It skips itself when there are no Tier B runs.

5. **Larger runs.** The plan's full profile adds n = 500. Set `SIZES` and `SEEDS` in
   `experiments/tierb/run_tierb.py`, and add the matching sizes and seeds to `tierb-a`. 500 nodes was not tried
   here. Expect to need more CPU and memory for kind (at least 16 GB). Check that a smoke cell's pods are all
   running at the drop before a long run.

`test/suites/performance/pipeline_test.go` holds older stress specs from the session. They run a pipeline
configuration through a diurnal scale-out and a 65% scale-down, and are not part of the rank check. To run them on
a cluster set up as above: `FOCUS="Consolidation Pipeline" TEST_SUITE=performance make e2etests`. With debug
logging, the controller writes one `consolidation decision` line per decision event. This turns them into the
simulator's decision-log schema:

```bash
kubectl logs -n kube-system deploy/karpenter --since=24h \
  | jq -c 'select(.message == "consolidation decision")
      | {schema_version, run_id, cycle, t, stage, strategy, move_id, kind, sources, destinations, pods,
         delta_potential, disruption, savings_per_hour, accepted, reason, budget_remaining, detail}' > decisions.jsonl
```

## 5. Tier C: one EKS sandbox pass (optional)

Only in a sandbox account with a hard spend cap; never a shared or production cluster.

1. Set a budget alarm and a hard cap first (for example an AWS Budgets action that denies `ec2:RunInstances` at
   $50).
2. Create a throwaway EKS cluster with Karpenter installed from this branch's image (`ko build` with the AWS
   provider, which lives in `aws/karpenter-provider-aws`; pin the core module to this branch with a `replace`).
3. Enable the gate and one pipeline config; run the KWOK specs' workloads (W2 shape) at 50 nodes for 2 hours, once
   with the gate off (B2) and once on.
4. Collect cost (from `karpenter_cluster_cost` or the instance price list), evictions, PDB events and the decision
   log as above; tear the cluster down and confirm no instances remain.
