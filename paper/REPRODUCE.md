# Reproducing the results

Every number, table and figure in `experiments/RESULTS.md` and under `paper/` is regenerated from the raw runs in
`experiments/runs/` by one command, and every raw run is regenerated from a commit, a configuration and a seed.

## Commits

| What | Commit |
| --- | --- |
| Upstream base (kubernetes-sigs/karpenter main) | `32fd95b` |
| Preregistration, Gate 3 (`experiments/PREREGISTRATION.md`) | `c78b5f95063c1995b10fa6d66571a16d0a32b243` |
| Simulator binary for every session run (`experiments/session-binary.txt`; each manifest's `binary_revision`) | `c78b5f95063c1995b10fa6d66571a16d0a32b243` |
| Pilot binaries (`experiments/PILOT.md`) | baselines `c3e9e35`, pipeline arms `2d8fce7` |
| Analysis code | `paper/analysis/` at the head of the branch; every change after Gate 3 is listed in `experiments/RESEARCH_LOG.md` |

## Hardware and software of the session

- Claude Code cloud VM: 4 vCPUs (Intel Xeon @ 2.10 GHz, one thread per core), 15 GiB RAM, Linux 6.18.
- Go 1.26.10 (`GOTOOLCHAIN=auto` from `go.mod`), Python 3.13.17 with `paper/requirements.txt`.
- Up to 4 cells ran concurrently (one per vCPU). Decision-cost metrics (wall and CPU time per pass) depend on this
  machine; every other metric is a deterministic function of the binary, the configuration and the seed.

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
bin/csim matrix --matrix experiments/matrix/pilot.yaml   --workers 4    # 120 cells, seeds 1-5
bin/csim matrix --matrix experiments/matrix/session.yaml --workers 4    # 1,740 cells, seeds 101-110
```

Run IDs are a hash of each cell's configuration, so a rerun writes the same directory names; the runner skips cells
that already have a `summary.json` and can be re-invoked until it reports `0 pending`. The session ran its groups in
the file's order, one group at a time (`docs/research/runbook.md`).

## Rebuilding the analysis

```bash
make figures                     # all tables (paper/tables) and figures (paper/figures/out) from experiments/runs
/opt/research-venv/bin/python paper/make_figures.py --only <module>
```

The outputs are byte-reproducible: two runs over the same raw data give identical files. The offline packing bounds
(B6) are cached in `paper/tables/cache/lower_bounds.json`; delete it to recompute them with CP-SAT.

## Parity

```bash
PARITY_STATES=1000  PARITY_SEED=1 go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 6h
PARITY_STATES=10000 PARITY_SEED=2 go test ./pkg/controllers/disruption/pipeline/ -run TestParity -count=1 -timeout 30h
```

Results: `experiments/parity/` and `docs/research/parity.md`.
