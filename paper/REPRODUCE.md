# Reproducing the results

Every number, table and figure in `experiments/RESULTS.md` and under `paper/` is regenerated from the raw runs in
`experiments/runs/` by one command, and every raw run is regenerated from a commit, a configuration and a seed.

## Commits

| What | Commit |
| --- | --- |
| Upstream base (kubernetes-sigs/karpenter main) | `32fd95b` |
| Preregistration, Gate 3 (`experiments/PREREGISTRATION.md`) | `c78b5f95063c1995b10fa6d66571a16d0a32b243` |
| Simulator binary for every session and H7 timing run (`experiments/session-binary.txt`; each manifest's `binary_revision`; built with Go 1.26.10) | `c78b5f95063c1995b10fa6d66571a16d0a32b243` |
| Pilot binaries (`experiments/PILOT.md`) | baselines `c3e9e35`, pipeline arms `2d8fce7` |
| Analysis code | `paper/analysis/` at the head of the branch; every change after Gate 3 is listed in `experiments/RESEARCH_LOG.md` |

## Hardware and software of the session

- Claude Code cloud VMs: 4 vCPUs (one thread per core), 15 GiB RAM, Linux 6.18. The VM was replaced three times,
  so the runs come from four VMs in turn, epochs A-D in `experiments/session-driver/machine-log.txt`:

  | Epoch | Booted (UTC) | CPU as `lscpu` names it | Ran |
  | --- | --- | --- | --- |
  | A | before Oct 27 20:52 | Intel Xeon @ 2.10 GHz | session runs from 20:52 |
  | B | Oct 27, about 23:52 | Intel Xeon @ 2.80 GHz | session runs |
  | C | Oct 28 03:29:38 | Intel Xeon @ 2.10 GHz | session runs |
  | D | Oct 28 23:23:55 | Intel Xeon @ 2.80 GHz | the rest of the session, the H7 timing pass, the late parity chunks |

  A run's VM is the last boot at or before its manifest's `started_at`; the analysis derives it as the
  `machine_epoch` column. The clock labels are `lscpu`'s generic model names and do not identify the CPU.
- Go 1.26.10 (`GOTOOLCHAIN=auto` from `go.mod`), Python 3.13.17 with `paper/requirements.txt`.
- Up to 4 cells ran concurrently (one per vCPU). Decision-cost metrics (wall and CPU time per pass) depend on the
  machine and its load; every other metric is a deterministic function of the binary, the configuration and the
  seed. Cells killed by a VM replacement were relaunched from scratch, so each run ran on one VM.

## Rebuilding a run

```bash
git checkout c78b5f95063c1995b10fa6d66571a16d0a32b243
go build -o bin/csim ./hack/consolidation-sim          # from a clean clone, so the VCS stamp is exact
bin/csim run --config experiments/runs/session/<run_id>/cell.json --out /tmp/<run_id>
```

`decisions.jsonl.gz`, `timeseries.csv`, `podsets.jsonl` and every simulated field of `summary.json` come out
byte-identical (the determinism test, `TestDeterminism`, checks this for 14 configurations; the result at the session
commit is in `experiments/session-determinism.txt`). Wall- and CPU-time fields and the manifest's timestamps differ.

## Rebuilding the matrices

```bash
bin/csim matrix --matrix experiments/matrix/pilot.yaml     --workers 4    # 120 cells, seeds 1-5
bin/csim matrix --matrix experiments/matrix/session.yaml   --workers 4    # 1,740 cells, seeds 101-110
bin/csim matrix --matrix experiments/matrix/h7-timing.yaml --workers 4 --max-wall 0s   # H7-S: 20 cells, 4 fillers
```

Run IDs are a hash of each cell's configuration, so a rerun writes the same directory names; the runner skips cells
that already have a `summary.json` and can be re-invoked until it reports `0 pending`. The H7 timing pass runs in one
invocation (no wall budget). Its cells have the configurations of the session's W1 1,000-node
heuristic/entropy-greedy and random/entropy-greedy cells in `exp1-n1000`, so the hash at the end of each run ID
matches, and only their timing fields differ.

The session ran its groups one at a time in the preregistered order, under the driver in
`experiments/session-driver/` (v1 to v4 in turn; its README says what each changed, and `session.log` is the full
log). 1,551 of the 1,740 session cells ran. The four cut-candidate groups were cut by the preregistered time-based
rule (`experiments/session-driver/cut-decisions.txt`), and one `realism-w5` cell had finished by then. To run them
on a larger machine:

```bash
bin/csim matrix --matrix experiments/matrix/session.yaml --workers $(nproc) \
  --only realism-w5,realism-w6,exp7-b2-n1000,exp1-baselines-n1000
```

`make figures` then includes them, since the completeness rule (10 seeds per configuration) admits them once
they are complete. Keep `exp7-b2-n1000` on a quiet machine, because it is a timing comparison.

## Rebuilding the analysis

```bash
make figures                     # all tables (paper/tables) and figures (paper/figures/out) from experiments/runs
/opt/research-venv/bin/python paper/make_figures.py --only <module>
```

The outputs are byte-reproducible: two runs over the same raw data give identical files. The offline packing bounds
(B6) are cached in `paper/tables/cache/lower_bounds.json`. The cache is not committed, so a fresh clone recomputes
the bounds with CP-SAT on its first run. The solver runs on one worker with a fixed seed and a deterministic work
limit, so the bounds do not depend on the machine's speed. The exception is an instance that hits the 120-second
wall-clock safety limit, which the analysis logs as a warning.

## Parity

```bash
PARITY_STATES=1000  PARITY_SEED=1 go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 6h
PARITY_STATES=10000 PARITY_SEED=2 go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 30h
```

The session ran the 10,000-state run in four chunks, one per policy and catalog, each selected with
`PARITY_POLICIES` and `PARITY_CATALOGS` and written to `experiments/parity/chunks/`, for example:

```bash
PARITY_STATES=10000 PARITY_SEED=2 PARITY_POLICIES=Balanced PARITY_CATALOGS=assorted \
  PARITY_OUT=$PWD/experiments/parity/chunks/parity-10000-seed2-balanced-assorted.jsonl \
  go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 30h -v
```

Results: `experiments/parity/` and `docs/research/parity.md`.
