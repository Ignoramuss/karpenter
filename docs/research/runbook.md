# Runbook: full profile, KWOK validation and an EKS sandbox pass

Everything here runs from the repository root on the branch that carries `docs/research/PLAN.md`. The session
profile (Tier A at 50, 200 and 1,000 nodes, 10 seeds) ran inside the cloud session; this runbook covers what it did
not: the full profile, Tier B on KWOK, and an optional Tier C pass on EKS. Session results and their caveats are in
`experiments/RESULTS.md`; the preregistered analysis is in `experiments/PREREGISTRATION.md`.

## 0. Prerequisites

- Go at the version in `go.mod` (`GOTOOLCHAIN=auto` fetches it).
- Python 3.13 with the pinned analysis packages:

  ```bash
  python3 -m venv /opt/research-venv
  /opt/research-venv/bin/pip install -r paper/requirements.txt
  ```

- For Tier B: Docker, `kind` (`go install sigs.k8s.io/kind@v0.30.0`), `kubectl`, `helm`, and network access to
  `registry.k8s.io` (KWOK images) and `cgr.dev` (the `ko` base image). Both were blocked in the session.
- Build one simulator binary per commit and use it for a whole matrix, so every cell's manifest carries one SHA:

  ```bash
  SHA=$(git rev-parse --short HEAD)
  go build -o bin/csim-$SHA ./hack/consolidation-sim
  ```

## 1. Tier A, full profile

`experiments/matrix/full.yaml` is the session matrix with 20 seeds per cell (101-120) plus 5,000-node cells for
Exp 1 and Exp 7 (3,440 cells). Its first ten seeds have the session cells' run IDs, so session results can be copied
in instead of rerun:

```bash
mkdir -p experiments/runs/full
cp -r experiments/runs/session/*__* experiments/runs/full/    # optional: reuse the session's 1,510 cells
```

Run it in chunks; the runner skips finished cells, retries a failed cell once, and exits 0 when its wall budget is
spent, so re-invoke it until it reports `0 pending`:

```bash
until bin/csim-$SHA matrix --matrix experiments/matrix/full.yaml --workers $(nproc) --max-wall 2h \
    | tee -a full.log | grep -q ' 0 pending'; do :; done
```

Sizing (measured in the session on 4 vCPUs, one cell per vCPU; see `experiments/PILOT.md` and the research log):
a 200-node cell takes 2-15 minutes of one core, a 1,000-node cell up to about an hour (B2 and B3 are the slowest,
since upstream single-node consolidation simulates every candidate each pass), and memory stays under 2 GB per cell.
5,000-node cells are untested; expect several hours each for B2 and give each worker 8 GB. A 64-core machine should
finish the full profile in about a day. Keep `--workers` at or below the core count: per-pass wall time is a measured
outcome (H7), so oversubscription biases it.

## 2. Analysis and figures

```bash
make figures          # runs paper/make_figures.py over experiments/runs; tables in paper/tables, figures in paper/figures/out
/opt/research-venv/bin/python paper/make_figures.py --runs experiments/runs/full --out paper/full
```

`make figures` is deterministic: two runs over the same raw data produce byte-identical outputs. The B6 lower bounds
are cached in `paper/tables/cache/`; delete the cache to recompute them.

## 3. Parity at scale

The session ran 1,000 states per policy and catalog (all cells matched) and 10,000 states as a background chunk.
To rerun or extend:

```bash
PARITY_STATES=10000 PARITY_SEED=2 PARITY_OUT=$PWD/experiments/parity/parity-10000-seed2.jsonl \
  go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 24h
```

`docs/research/parity.md` explains the comparison, including how multi-NodePool states, where upstream is itself
nondeterministic, are judged.

## 4. Tier B: KWOK on kind (50, 200 and 500 nodes)

1. Cluster and controller (the kind config mounts a kube-scheduler configuration with a `most-allocated` profile
   next to the default one):

   ```bash
   make setup-kind
   make install-kwok
   make apply-with-kind
   ```

2. The pipeline specs are in `test/suites/performance/pipeline_test.go`. Each enables the gate and sets an inline
   pipeline config through the controller's environment (`FEATURE_GATES=ConsolidationPipeline=true`,
   `CONSOLIDATION_PIPELINE_CONFIG=<yaml or json>`), runs a diurnal scale-out and 65% scale-down, and writes a
   performance report:

   ```bash
   FOCUS="Consolidation Pipeline" TEST_SUITE=performance make e2etests
   ```

   For the plan's Phase 5 validation, set the configs in `pipelineConfigs` to the top three session configurations
   plus B4 (the upstream adapters), and run B2 and B3 with the gate off (the existing Balanced specs), 5 seeds each.
   For 500 nodes, raise the replica counts in the specs proportionally and give kind at least 16 GB.

3. Decision logs: the controller logs one `consolidation decision` line per decision event at debug level. Extract
   them into the simulator's schema with:

   ```bash
   kubectl logs -n kube-system deploy/karpenter --since=24h \
     | jq -c 'select(.message == "consolidation decision")
         | {schema_version, run_id, cycle, t, stage, strategy, move_id, kind, sources, destinations, pods,
            delta_potential, disruption, savings_per_hour, accepted, reason, budget_remaining, detail}' > decisions.jsonl
   ```

4. Rank agreement: compare each metric's configuration order between Tier A and Tier B with Kendall's tau, and
   report any reversal as a finding. Also record controller CPU and memory, API server request rate, eviction rate
   and validation aborts, which the simulator cannot measure.

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
