"""Plain-assert tests for paper.analysis.latency_optimality, on a synthetic runs tree with known latencies and known
optimality gaps, plus a smoke run on the pilot. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_latency_optimality

``--write-synthetic DIR`` only writes the synthetic tree (``DIR/session/<run_id>/...``), e.g. to point
``paper/make_figures.py --runs DIR`` at it.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import traceback
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from paper.analysis import common
from paper.analysis import latency_optimality as lo

PILOT = common.REPO_ROOT / "experiments" / "runs" / "pilot"
SEEDS = list(range(101, 111))
EXP8_SEEDS = [101, 102, 103, 104]
C7I_2XL = 0.357  # four (1975m, 4093Mi) pods fill a c7i.2xlarge, the catalog's cheapest CPU: k nodes cost k * this
POD = (1975, 4093)
SNAP_TIMES = (10, 40, 70, 100, 130, 160, 180)
GAPS = {"B2": 0.20, "B3": 0.12, "B5": 0.15, "heuristic/entropy-greedy": 0.10, "random/entropy-greedy": 0.05,
        "random/savings-only": 0.08, "heuristic/anneal-plan": 0.07}
CPU_SHARE = 0.8  # synthetic CPU time per pass as a share of its wall time
EARLY_GAP = 0.5  # every run's gap at the t = 10 snapshot, before consolidation
ARMS = {"B2": "B2-when-empty-or-underutilized", "B3": "B3-balanced", "B5": "B5-descheduler"}
OUTPUTS = ["tables/exp7_latency.csv", "tables/exp7_latency.md", "tables/exp7_tests.csv", "tables/exp7_tests.md",
           "tables/exp8_gap.csv", "tables/exp8_gap.md", "tables/exp8_gap_snapshots.csv",
           "tables/exp8_gap_snapshots.md", "tables/latency_optimality.json", "figures/out/fig4_latency.png",
           "figures/out/fig4_latency.pdf", "figures/out/fig7_optimality_gap.png",
           "figures/out/fig7_optimality_gap.pdf"]


# --- synthetic runs tree -------------------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def latency_ms(label: str, nodes: int) -> float:
    """Synthetic mean pass wall time: random grows as sqrt(n), heuristic linearly, B2 as n^1.2 (CPU time is
    ``CPU_SHARE`` of it)."""
    base, exponent = {"random": (20.0, 0.5), "heuristic": (40.0, 1.0), "B2": (60.0, 1.2)}[label.split("/")[0]]
    return base * (nodes / 50) ** exponent


def _write_run(matrix: Path, group: str, label: str, scenario: str, nodes: int, seed: int, index: list[dict[str, Any]],
               rng: np.random.Generator, podsets: list[dict[str, Any]] | None = None, duration: str = "3h0m0s",
               placement: str = "least-allocated", drop: tuple[str, ...] = (), **metrics: Any) -> None:
    desc = f"arm-{_slug(label)}_scenario-{scenario.lower()}_seed-{seed}"
    run_id = f"{group}__{desc}__{zlib.crc32((group + desc).encode()):08x}"
    d = matrix / run_id
    d.mkdir(parents=True)
    pipeline = label not in ARMS
    arm_cfg: dict[str, Any] = {"name": "pipeline" if pipeline else ARMS[label], "label": label}
    if pipeline:
        arm_cfg["pipeline"] = {"planner": {"name": label.split("/")[1], "params": {"lambda": 0.5}},
                               "candidates": {"name": "uniform-random" if label.startswith("random") else "upstream"},
                               "moves": [{"name": "random-merge"}], "executor": {"name": "shift-evict"},
                               "potential": {"name": "util-entropy"}, "search": {"maxFullSimulations": 16}}
    cell = {"arm": arm_cfg, "runID": run_id, "scenario": {"name": scenario, "nodes": nodes}, "seed": seed,
            "placement": placement}
    (d / "cell.json").write_text(json.dumps(cell, indent=2) + "\n", encoding="utf-8")
    manifest = {"run_id": run_id, "git_sha": "0" * 40, "git_dirty": False, "seed": seed, "scenario": scenario,
                "config": {"runID": run_id, "seed": seed, "scenario": {"name": scenario, "nodes": nodes},
                           "arm": arm_cfg, "placement": placement, "scaleDown": "spread", "duration": duration,
                           "warmup": "10m0s"}}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    mean = latency_ms(label, nodes) * (1 + rng.normal(0, 0.05)) if label.split("/")[0] in ("random", "heuristic",
                                                                                           "B2") else 10.0
    passes = 900 + int(rng.integers(0, 50))
    summary: dict[str, Any] = {
        "run_id": run_id, "label": label, "arm": arm_cfg["name"], "scenario": scenario, "scenario_nodes": nodes,
        "seed": seed, "placement": placement, "scale_down": "spread", "sim_minutes": 180,
        "integrated_cost": 100.0, "integrated_cost_post": 90.0, "steady_cost_per_hour": 30.0, "final_nodes": nodes // 2,
        "peak_nodes": nodes, "evictions": 100, "eviction_cost": 100.0, "disruption_passes": passes,
        "full_simulations": 6 * passes if pipeline else 0, "search_timeouts": 0, "fidelity_hits": 0,
        "fidelity_misses": 0, "fidelity": 0.0, "invalid": False, "invalid_reasons": [], "timed_out": False,
        "wall_seconds": 100.0, "wall_pass_mean_ms": mean, "wall_pass_p95_ms": 3 * mean, "wall_pass_max_ms": 6 * mean,
        "wall_pass_total_s": mean * passes / 1000, "wall_passes_over_1m": 0, "wall_passes_over_3m": 0,
        "cpu_pass_mean_ms": CPU_SHARE * mean, "cpu_pass_p95_ms": CPU_SHARE * 3 * mean,
        "cpu_pass_max_ms": CPU_SHARE * 6 * mean, "cpu_pass_total_s": CPU_SHARE * mean * passes / 1000,
        "pdb_violations": 0, "do_not_disrupt_violations": 0,
    }
    summary.update(metrics)
    for key in drop:
        summary.pop(key, None)
    (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if podsets is not None:
        with open(d / "podsets.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(snap) + "\n" for snap in podsets)
    index.append({"run_id": run_id, "group": group, "status": "done", "exit_code": 0, "attempt": 1})


def _podsets(label: str, ks: list[int], final_only: bool = False) -> list[dict[str, Any]]:
    """Snapshots with ``ks[i]`` full c7i.2xlarge nodes of pods at SNAP_TIMES[i]: cost = bound * (1 + gap), gap
    EARLY_GAP at t = 10 and GAPS[label] after."""
    out = []
    for t, k in zip(SNAP_TIMES, ks):
        if final_only and t != SNAP_TIMES[-1]:
            continue
        gap = EARLY_GAP if t == SNAP_TIMES[0] else GAPS[label]
        out.append({"t_min": float(t), "cost_per_hour": k * C7I_2XL * (1 + gap), "nodes": k,
                    "shapes": [[POD[0], POD[1], 4 * k]]})
    return out[::-1]  # written newest first: the loader must sort


def exp8_ks(scenario: str, seed: int) -> list[int]:
    """Nodes' worth of pods per snapshot at 50 nodes: W1 constant per seed, W2 scaling down after t = 40."""
    if scenario == "W1":
        return [3 + seed % 2] * len(SNAP_TIMES)
    return [4, 4, 3, 3, 3, 3, 3]


def write_synthetic_tree(root: Path) -> None:
    """A session-like runs tree under ``root/session`` with known Exp 7 and Exp 8 effects.

    Exp 7 (W1; 50, 200, 1,000 nodes): latencies per ``latency_ms`` (random fastest), p95 = 3x and max = 6x the mean.
    At 1,000 nodes heuristic seed 103 is invalid; B2 seeds 101-103 have 2 passes over 1 minute each and seed 104 does
    not report the counts but its slowest pass took 70 s (count unknown); B2 at 200 nodes does not report them either
    but never passed 60 s (count 0). Exp 1 cells outside Exp 7 (W2, random/savings-only) exist and must be ignored.

    Exp 8 (W1, W2 at 50 nodes; B2, heuristic entropy-greedy and anneal-plan, random entropy-greedy, and B5 under
    most-allocated placement, seeds 101-104, so run with ANALYSIS_MIN_SEEDS=4): every snapshot holds 4k (1975m,
    4093Mi) pods, whose optimum is k c7i.2xlarge; the run's cost is
    bound * (1 + gap) with gap 0.5 at t = 10 and ``GAPS[label]`` later. W1 random seed 104 is invalid; W2 B2 seed 102
    has no podsets.jsonl. The LP scales reuse Exp 1's 200- and 1,000-node cells (final snapshots, k = 20 and 100).
    """
    rng = np.random.default_rng(0)
    matrix = Path(root) / "session"
    matrix.mkdir(parents=True)
    index: list[dict[str, Any]] = []
    for seed in SEEDS:
        for label in ("B2", "heuristic/entropy-greedy", "random/entropy-greedy"):
            _write_run(matrix, "exp7-n50", label, "W1", 50, seed, index, rng)
        _write_run(matrix, "exp7-b2", "B2", "W1", 200, seed, index, rng,
                   drop=("wall_passes_over_1m", "wall_passes_over_3m"))
        extra: dict[str, Any] = {}
        drop: tuple[str, ...] = ()
        if seed <= 103:
            extra = {"wall_passes_over_1m": 2, "wall_pass_max_ms": 75_000.0}
        elif seed == 104:
            extra, drop = {"wall_pass_max_ms": 70_000.0}, ("wall_passes_over_1m", "wall_passes_over_3m")
        _write_run(matrix, "exp7-b2-n1000", "B2", "W1", 1000, seed, index, rng, duration="1h0m0s", drop=drop,
                   **extra)
        for group, nodes, k in (("exp1-n200", 200, 20), ("exp1-n1000", 1000, 100)):
            for label in ("heuristic/entropy-greedy", "random/entropy-greedy", "random/savings-only"):
                bad = group == "exp1-n1000" and label == "heuristic/entropy-greedy" and seed == 103
                _write_run(matrix, group, label, "W1", nodes, seed, index, rng,
                           podsets=_podsets(label, [k] * len(SNAP_TIMES)), invalid=bad,
                           invalid_reasons=["pdb violation"] if bad else [], pdb_violations=1 if bad else 0)
            _write_run(matrix, group, "heuristic/entropy-greedy", "W2", nodes, seed, index, rng,
                       podsets=_podsets("heuristic/entropy-greedy", [k] * len(SNAP_TIMES), final_only=True))
    for scenario in ("W1", "W2"):
        for seed in EXP8_SEEDS:
            ks = exp8_ks(scenario, seed)
            for label in ("B2", "heuristic/entropy-greedy", "heuristic/anneal-plan", "random/entropy-greedy"):
                bad = scenario == "W1" and label == "random/entropy-greedy" and seed == 104
                missing = scenario == "W2" and label == "B2" and seed == 102
                _write_run(matrix, "exp8", label, scenario, 50, seed, index, rng,
                           podsets=None if missing else _podsets(label, ks), invalid=bad,
                           pdb_violations=1 if bad else 0)
            if scenario == "W1":
                _write_run(matrix, "exp8-b5", "B5", scenario, 50, seed, index, rng, placement="most-allocated",
                           podsets=_podsets("B5", ks))
    with open(matrix / "index.jsonl", "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(entry) + "\n" for entry in index)


@contextlib.contextmanager
def _env(**values: str | None) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _synthetic(min_seeds: int | None = len(EXP8_SEEDS)) -> Iterator[tuple[Path, Path]]:
    """The synthetic tree, with ANALYSIS_MIN_SEEDS set so Exp 8's 4-seed cells are not cut (None: the default 10)."""
    with tempfile.TemporaryDirectory() as tmp, _env(**{common.MIN_SEEDS_ENV: None if min_seeds is None else
                                                       str(min_seeds), common.GROUP_MAP_ENV: None}):
        runs, out = Path(tmp) / "runs", Path(tmp) / "out"
        write_synthetic_tree(runs)
        yield runs, out


def _quiet(fn: Callable[[], Any]) -> tuple[Any, str]:
    buf = io.StringIO()
    handler = common.logger.handlers[0]
    old = handler.setStream(buf)
    try:
        return fn(), buf.getvalue()
    finally:
        handler.setStream(old)


def _run_main(runs: Path, out: Path) -> str:
    common.apply_style()
    return _quiet(lambda: lo.main(runs, out))[1]


def _files(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def _row(df: pd.DataFrame, **match: Any) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for k, v in match.items():
        mask &= df[k] == v
    rows = df.loc[mask]
    assert len(rows) == 1, (match, len(rows))
    return rows.iloc[0]


# --- tests ---------------------------------------------------------------------------------------------------------


def test_empty_runs_write_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        log = _run_main(Path(tmp) / "none", out)
        assert "skipped exp7: no runs" in log and "skipped exp8: no runs" in log
        assert not out.exists() or not any(out.rglob("*"))


def test_synthetic_outputs() -> None:
    with _synthetic() as (runs, out):
        log = _run_main(runs, out)
        files = _files(out)
        for name in OUTPUTS:
            assert name in files, (name, log)
        tmp = str(runs.parent)
        for name, data in files.items():
            assert tmp.encode() not in data, f"{name} embeds an absolute path"

        lat = pd.read_csv(out / "tables" / "exp7_latency.csv")
        assert len(lat) == 9 and set(lat["scenario"]) == {"W1"}, lat[["nodes", "label"]]
        assert set(lat["label"]) == {"B2", "heuristic/entropy-greedy", "random/entropy-greedy"}
        assert lat["nodes"].tolist() == [50] * 3 + [200] * 3 + [1000] * 3
        assert lat["label"].tolist()[:3] == ["heuristic/entropy-greedy", "random/entropy-greedy", "B2"]
        b2k = _row(lat, nodes=1000, label="B2")
        assert b2k["duration_min"] == 60 and math.isnan(b2k["full_simulations_per_pass_mean"])
        assert b2k["passes_over_1m_total"] == 6 and b2k["runs_over_1m"] == 4 and b2k["runs_over_1m_unknown"] == 1
        assert b2k["runs_over_3m"] == 0 or b2k["runs_over_3m_unknown"] == 0
        b2 = _row(lat, nodes=200, label="B2")
        assert b2["passes_over_1m_total"] == 0 and b2["runs_over_1m"] == 0 and b2["runs_over_1m_unknown"] == 0
        h = _row(lat, nodes=1000, label="heuristic/entropy-greedy")
        assert h["n_runs"] == 10 and h["n_valid"] == 9 and h["n_invalid"] == 1
        assert abs(h["full_simulations_per_pass_mean"] - 6.0) < 1e-9 and h["peak_nodes_mean"] == 1000
        r = _row(lat, nodes=1000, label="random/entropy-greedy")
        assert abs(r["wall_pass_mean_ms_mean"] / latency_ms("random", 1000) - 1) < 0.05
        assert r["wall_pass_mean_ms_mean_lo"] <= r["wall_pass_mean_ms_mean"] <= r["wall_pass_mean_ms_mean_hi"]
        assert abs(r["wall_pass_p95_ms_mean"] / r["wall_pass_mean_ms_mean"] - 3) < 1e-4  # CSV keeps 6 digits
        assert abs(r["cpu_pass_mean_ms_mean"] / r["wall_pass_mean_ms_mean"] - CPU_SHARE) < 1e-4

        tests = pd.read_csv(out / "tables" / "exp7_tests.csv")
        assert len(tests) == 4 and set(tests["nodes"]) == {1000}, tests
        assert set(tests["metric"]) == {"cpu_pass_mean_ms", "cpu_pass_p95_ms"}, "H7 tests CPU time per pass"
        assert (tests["outcome"] == "lower").all() and (tests["direction"] == "lower").all()
        assert (tests["expect_met"] == True).all()  # noqa: E712
        assert list(tests["family"]) == ["H7", "H7", "H7-B2", "H7-B2"] and (tests["m"] == 2).all()
        assert (tests["p_holm"] >= tests["p_wilcoxon"]).all()
        vs_h = tests.loc[tests["b"] == "heuristic/entropy-greedy"]
        assert (vs_h["n_pairs"] == 9).all() and (vs_h["n_dropped_invalid"] == 1).all()
        assert np.allclose(vs_h["p_wilcoxon"], 2 / 512) and np.allclose(vs_h["p_holm"], 2 * 2 / 512)
        assert (tests.loc[tests["b"] == "B2", "n_pairs"] == 10).all()
        assert (tests["speedup"] > 1).all() and "p_tost" not in tests.columns

        head = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))
        h7 = head["exp7"]["h7"]
        assert h7["verdict"] == "supported" and h7["timeout_clause"] == "holds" and h7["tests_lower"] == 2, h7
        assert head["exp7"]["h7_b2"]["status"] == "present" and head["exp7"]["h7_b2"]["tests_lower"] == 2
        assert head["exp7"]["timeouts_at_tested_nodes"]["1m"] == {"passes_total": 0.0, "runs_over": 0,
                                                                   "runs_unknown": 0}

        gap = pd.read_csv(out / "tables" / "exp8_gap.csv")  # CSV floats keep 6 significant digits
        assert gap["nodes"].tolist() == sorted(gap["nodes"].tolist())
        small = gap.loc[gap["nodes"] == 50]
        assert set(small["bound"]) == {"cpsat"} and set(gap.loc[gap["nodes"] > 50, "bound"]) == {"lp"}
        assert len(small) == 9, small[["scenario", "label"]]
        assert set(small["label"]) >= {"heuristic/anneal-plan"}, "Exp 8 includes heuristic/anneal-plan"
        for _, row in gap.iterrows():
            want = GAPS[row["label"]]
            assert abs(row["gap_final_mean"] - want) < 1e-6, (row["nodes"], row["scenario"], row["label"])
            assert row["gap_final_mean_lo"] <= row["gap_final_mean"] + 1e-12 <= row["gap_final_mean_hi"] + 2e-12
            assert abs(row["gap_lp_final_mean"] - want) < 1e-6  # the LP bound is exact for these pods
            if row["bound"] == "cpsat":
                assert abs(row["gap_all_mean"] - (EARLY_GAP + 6 * want) / 7) < 1e-6
                assert row["n_final_proven_optimal"] == row["n_with_podsets"]
                assert abs(row["bound_slack_final_mean"]) < 1e-6
            else:
                assert math.isnan(row["gap_all_mean"])
        rw1 = _row(gap, nodes=50, scenario="W1", label="random/entropy-greedy")
        assert rw1["n_runs"] == 4 and rw1["n_valid"] == 3 and rw1["n_with_podsets"] == 3
        bw2 = _row(gap, nodes=50, scenario="W2", label="B2")
        assert bw2["n_valid"] == 4 and bw2["n_with_podsets"] == 3
        b5 = _row(gap, nodes=50, scenario="W1", label="B5")
        assert b5["n_with_podsets"] == 4
        lp200 = _row(gap, nodes=200, scenario="W1", label="heuristic/entropy-greedy")
        assert lp200["n_with_podsets"] == 10 and abs(lp200["bound_final_mean"] - 20 * C7I_2XL) < 1e-6
        lp1k = _row(gap, nodes=1000, scenario="W1", label="heuristic/entropy-greedy")
        assert lp1k["n_valid"] == 9 and abs(lp1k["bound_final_mean"] - 100 * C7I_2XL) < 1e-6

        snaps = pd.read_csv(out / "tables" / "exp8_gap_snapshots.csv")
        assert set(snaps["nodes"]) == {50} and sorted(set(snaps["t_min"])) == list(map(float, SNAP_TIMES))
        early = snaps.loc[snaps["t_min"] == 10]
        assert (abs(early["gap_mean"] - EARLY_GAP) < 1e-6).all()

        b = head["exp8"]["bounds"]
        assert b["cpsat_bound_below_lp"] == 0 and b["cpsat_wall_limited"] == 0
        assert abs(b["mean_cpsat_over_lp_final"] - 1) < 1e-9
        # 50 nodes: two multisets (k = 3 and 4, shared by W1 and W2); LP scales: k = 20 and 100, no CP-SAT.
        assert b["cpsat_status_counts"] == {"OPTIMAL": 2, "not requested": 2}, b["cpsat_status_counts"]
        assert len(gap) == 17
        assert (out / "tables" / "cache" / "lower_bounds.json").exists()


def test_reproducible_and_cache_neutral() -> None:
    with _synthetic() as (runs, out):
        _run_main(runs, out)
        first = _files(out)
        _run_main(runs, out)  # with the cache in place
        assert _files(out) == first
        cache = out / "tables" / "cache" / "lower_bounds.json"
        cache.unlink()
        _run_main(runs, out)  # cache deleted: recomputed, same outputs and the same cache file
        assert _files(out) == first


def test_latency_columns() -> None:
    df = pd.DataFrame({
        "arm_name": ["pipeline", "pipeline", "B2-when-empty-or-underutilized"],
        "full_simulations": [600, 0, 0], "disruption_passes": [100, 0, 100],
        "wall_pass_max_ms": [61_000.0, 1_000.0, 200_000.0],
        "wall_passes_over_1m": [1, np.nan, np.nan], "wall_passes_over_3m": [0, np.nan, np.nan],
    })
    out = lo.add_latency_columns(df)
    assert out["full_simulations_per_pass"].tolist()[0] == 6.0
    assert np.isnan(out["full_simulations_per_pass"].tolist()[1:]).all()
    assert out["passes_over_1m"].tolist()[:2] == [1.0, 0.0] and math.isnan(out["passes_over_1m"].tolist()[2])
    assert out["passes_over_3m"].tolist()[:2] == [0.0, 0.0] and math.isnan(out["passes_over_3m"].tolist()[2])
    assert lo.rank_direction([-1, -2, 0.5]) == -1 and lo.rank_direction([0, 0]) == 0


def test_pilot_smoke() -> None:
    if not PILOT.is_dir() or not any(PILOT.glob("*/summary.json")):
        print("    (no pilot runs; skipped)")
        return
    mapping = {"exp1-n200": "pilot-exp1", "exp1-baselines-n200": "pilot-baselines", "exp7-b2": "pilot-baselines"}
    with tempfile.TemporaryDirectory() as tmp, _env(**{common.GROUP_MAP_ENV: json.dumps(mapping),
                                                       common.MIN_SEEDS_ENV: "5"}):
        out = Path(tmp) / "out"
        log = _run_main(PILOT, out)  # the pilot matrix only: session runs share experiments/runs
        assert "skipped exp8 at 50 nodes (CP-SAT): no runs" in log, log
        lat = pd.read_csv(out / "tables" / "exp7_latency.csv")
        assert set(lat["nodes"]) == {200} and set(lat["scenario"]) == {"W1"}
        assert "B2" in set(lat["label"])
        assert lat["cpu_pass_mean_ms_mean"].isna().all(), "older pilot binaries wrote no CPU time"
        gap = pd.read_csv(out / "tables" / "exp8_gap.csv")
        assert set(gap["bound"]) == {"lp"} and set(gap["nodes"]) == {200}
        assert (gap["gap_final_mean"] > 0).all() and (gap["n_with_podsets"] == gap["n_valid"]).all()
        assert (out / "figures" / "out" / "fig7_optimality_gap.png").exists()
        assert (out / "figures" / "out" / "fig4_latency.png").exists()
        tests = pd.read_csv(out / "tables" / "exp7_tests.csv")  # no 1,000-node pilot cells: H7's 2 rows, no data
        assert len(tests) == 2 and set(tests["outcome"]) == {"no data"} and set(tests["family"]) == {"H7"}
        head = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["exp7"]
        assert head["h7"]["verdict"] == "incomplete" and head["h7_b2"]["status"] == "absent", head["h7"]


def test_h7_b2_family_absent_when_cut() -> None:
    """H7-B2 is absent (not "incomplete") when exp7-b2-n1000 is cut; H7's verdict does not depend on it."""
    with _synthetic(min_seeds=None) as (runs, out):
        for d in sorted((runs / "session").glob("exp7-b2-n1000__*"))[:5]:  # 5 of 10 finished: cut
            shutil.rmtree(d)
        for d in sorted((runs / "session").glob("exp8*__*")):  # Exp 8's 4-seed cells would be cut too
            shutil.rmtree(d)
        log = _run_main(runs, out)
        assert "skipped H7-B2: exp7-b2-n1000 is cut (fewer than 10 finished runs)" in log, log
        tests = pd.read_csv(out / "tables" / "exp7_tests.csv")
        assert list(tests["family"]) == ["H7", "H7"] and (tests["m"] == 2).all()
        head = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["exp7"]
        assert head["h7_b2"]["status"] == "absent" and head["h7"]["verdict"] == "supported", head
        lat = pd.read_csv(out / "tables" / "exp7_latency.csv")
        assert 1000 not in set(lat.loc[lat["label"] == "B2", "nodes"]), "a cut configuration leaves every table"


def test_h7_timeout_clause() -> None:
    """One valid random/entropy-greedy run at 1,000 nodes with a pass over 1 minute of wall time fails H7."""
    with _synthetic() as (runs, out):
        d = sorted((runs / "session").glob("exp1-n1000__arm-random-entropy-greedy_scenario-w1_*"))[0]
        s = json.loads((d / "summary.json").read_text())
        s.update(wall_passes_over_1m=1, wall_pass_max_ms=65_000.0)
        (d / "summary.json").write_text(json.dumps(s))
        _run_main(runs, out)
        h7 = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["exp7"]["h7"]
        assert h7["timeout_clause"] == "fails" and h7["tests_lower"] == 2 and h7["verdict"] == "not supported", h7


def test_h7_timeout_clause_covers_every_scenario() -> None:
    """The clause covers every valid random/entropy-greedy run at 1,000 nodes, W2 and W4 included, not only W1."""
    with _synthetic() as (runs, out):
        ds = sorted((runs / "session").glob("exp1-n1000__arm-random-entropy-greedy_scenario-w2_*"))
        if not ds:  # the synthetic tree has no W2 cells at 1,000 nodes: add a complete W2 configuration (all seeds,
            # so the completeness rule keeps it) copied from the W1 cells
            for src in sorted((runs / "session").glob("exp1-n1000__arm-random-entropy-greedy_scenario-w1_*")):
                dst = src.with_name(src.name.replace("scenario-w1", "scenario-w2"))
                shutil.copytree(src, dst)
                for name in ("summary.json", "cell.json", "manifest.json"):
                    f = dst / name
                    if f.exists():
                        f.write_text(f.read_text().replace('"W1"', '"W2"'))
                ds.append(dst)
        s = json.loads((ds[0] / "summary.json").read_text())
        s.update(wall_passes_over_1m=2, wall_pass_max_ms=70_000.0)
        (ds[0] / "summary.json").write_text(json.dumps(s))
        _run_main(runs, out)
        h7 = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["exp7"]["h7"]
        assert h7["timeout_clause"] == "fails" and h7["verdict"] == "not supported", h7


def test_h7_unreachable_tests_are_inconclusive() -> None:
    """Five invalid seeds leave 5 pairs (floor 2/32 > 0.05): both H7 tests are unreachable, so H7 is inconclusive,
    not "not supported" (G6)."""
    with _synthetic() as (runs, out):
        cells = sorted((runs / "session").glob("exp1-n1000__arm-random-entropy-greedy_scenario-w1_*"))
        for d in cells[5:]:
            s = json.loads((d / "summary.json").read_text())
            s.update(pdb_violations=1, invalid=True)
            (d / "summary.json").write_text(json.dumps(s))
        _run_main(runs, out)
        h7 = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["exp7"]["h7"]
        assert h7["verdict"] == "inconclusive", h7


H7S_T0 = pd.Timestamp("2026-10-29T04:00:00Z")


def _write_h7s_pass(runs: Path, random_share: float = 0.5) -> list[Path]:
    """The H7 timing pass beside the synthetic session: each exp1-n1000 W1 entropy-greedy run copied to
    ``runs/h7-timing/h7t-<seed>__...`` with its own run ID (in summary, manifest, cell and a decision log both copies
    get) and fresh machine timings: heuristic at 2x its session CPU time, random at ``random_share`` of that. The
    i-th copy (heuristic and random alternating by seed) runs from H7S_T0 + 5i minutes for 20 minutes."""
    session, matrix = runs / "session", runs / "h7-timing"
    out = []
    srcs = sorted(session.glob("exp1-n1000__arm-*-entropy-greedy_scenario-w1_*"),
                  key=lambda p: (int(re.search(r"_seed-(\d+)__", p.name).group(1)), p.name))
    for i, src in enumerate(srcs):
        seed = int(re.search(r"_seed-(\d+)__", src.name).group(1))
        dst = matrix / src.name.replace("exp1-n1000__", f"h7t-{seed}__")
        shutil.copytree(src, dst)
        for d, rid in ((src, src.name), (dst, dst.name)):
            line = json.dumps({"run_id": rid, "cycle": 1, "t": 12.0, "kind": "merge", "accepted": True})
            (d / "decisions.jsonl.gz").write_bytes(gzip.compress((line + "\n").encode(), mtime=0))
        for name in ("summary.json", "manifest.json", "cell.json"):
            f = dst / name
            f.write_text(f.read_text().replace(src.name, dst.name))
        man = json.loads((dst / "manifest.json").read_text())
        start = H7S_T0 + pd.Timedelta(minutes=5 * i)
        man["started_at"] = start.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        man["finished_at"] = (start + pd.Timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (dst / "manifest.json").write_text(json.dumps(man))
        s = json.loads((dst / "summary.json").read_text())
        factor = 2.0 * (random_share if "random" in src.name else 1.0)
        for k in [k for k in s if k.startswith(("cpu_pass_", "wall_pass_")) and "over" not in k]:
            s[k] = s[k] * factor
        (dst / "summary.json").write_text(json.dumps(s))
        out.append(dst)
    return out


def test_h7s_pass() -> None:
    """H7-S on a paired timing pass whose reruns reproduce their session twins: both tests lower, determinism pass,
    supported and in agreement with H7; groups h7t-<seed> count as one group (h7-timing), so nothing is cut. The
    copied heuristic seed 103 is invalid, as in the session tree, so 9 pairs remain."""
    with _synthetic(min_seeds=None) as (runs, out):
        reruns = _write_h7s_pass(runs)
        assert len(reruns) == 20
        log = _run_main(runs, out)
        head = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))
        h7s = head["h7s"]
        assert h7s["determinism"] == {"status": "pass", "runs": 20, "matched": 20, "differs": [], "not_checked": [],
                                      "no_twin": []}, (h7s, log)
        assert h7s["pairing"] == {"status": "not checked", "split_seeds": []}, "no machine log in this tree"
        assert [t["outcome"] for t in h7s["tests"]] == ["lower", "lower"], h7s["tests"]
        assert h7s["verdict"] == "supported" and h7s["all_tests_agree"] is True, h7s
        assert {m: v["agree"] for m, v in h7s["tests_vs_h7"].items()} == {"cpu_pass_mean_ms": True,
                                                                         "cpu_pass_p95_ms": True}
        assert h7s["h7_timeout_clause"] == "holds" and h7s["h7_verdict"] == "supported"
        ratio = h7s["cpu_ratio_to_twin"]
        assert ratio["heuristic/entropy-greedy"]["n"] == 9 and abs(ratio["heuristic/entropy-greedy"]["geomean"] - 2) < 1e-9
        assert abs(ratio["random/entropy-greedy"]["geomean"] - 1) < 1e-9 and ratio["random/entropy-greedy"]["n"] == 10
        # 20 cells of 20 minutes starting 5 minutes apart: an inner cell overlaps 20 + 2 x (15 + 10 + 5) cell-minutes,
        # a mean of 4 at once; the first and last cells see 2.5 (to within the start times' fractional seconds).
        runs_t = pd.read_csv(out / "tables" / "h7s_runs.csv")
        conc = runs_t["concurrency_mean"]
        assert abs(conc.max() - 4) < 1e-3 and abs(conc.min() - 2.5) < 1e-3, conc.tolist()
        assert abs(_row(runs_t, seed=105, label="random/entropy-greedy")["concurrency_mean"] - 4) < 1e-3
        assert {v["group"] for v in h7s["validity"]} == {"h7-timing"}, h7s["validity"]
        tests = pd.read_csv(out / "tables" / "h7s_tests.csv")
        assert list(tests["family"]) == ["H7-S", "H7-S"] and (tests["n_pairs"] == 9).all() and (tests["m"] == 2).all()
        assert abs(float(tests["speedup"].iloc[0]) - 2 * float(
            pd.read_csv(out / "tables" / "exp7_tests.csv")["speedup"].iloc[0])) < 0.5, "random at half the heuristic"


def test_h7s_void_and_disagreement() -> None:
    """A rerun whose simulated output differs from its twin voids H7-S; with matching reruns where random is slower
    (20x its share: the synthetic random arm is about 9x faster at 1,000 nodes), H7-S is not supported and disagrees
    with H7."""
    with _synthetic(min_seeds=None) as (runs, out):
        reruns = _write_h7s_pass(runs)
        bad = reruns[3]
        s = json.loads((bad / "summary.json").read_text())
        s["evictions"] += 1
        (bad / "summary.json").write_text(json.dumps(s))
        _run_main(runs, out)
        h7s = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["h7s"]
        assert h7s["verdict"] == "void" and h7s["determinism"]["differs"] == [bad.name], h7s
        det = pd.read_csv(out / "tables" / "h7s_runs.csv")
        assert _row(det, run_id=bad.name)["differences"] == "summary.evictions"
    with _synthetic(min_seeds=None) as (runs, out):
        _write_h7s_pass(runs, random_share=20.0)
        _run_main(runs, out)
        h7s = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["h7s"]
        assert [t["outcome"] for t in h7s["tests"]] == ["higher", "higher"], h7s["tests"]
        assert h7s["verdict"] == "not supported" and h7s["h7_verdict"] == "supported", h7s
        assert h7s["all_tests_agree"] is False


def test_h7s_split_seed_and_abandoned_twin() -> None:
    """A seed whose arms started on different boots of the machine log leaves the tests (8 pairs: seed 103's
    heuristic is invalid); a session twin the runner abandoned leaves its rerun "not checked" without blocking the
    determinism check."""
    with _synthetic(min_seeds=None) as (runs, out):
        reruns = _write_h7s_pass(runs)
        r105 = next(d for d in reruns if d.name.startswith("h7t-105__arm-random"))
        start = json.loads((r105 / "manifest.json").read_text())["started_at"]
        boot2 = (pd.Timestamp(start.replace(".123456789", "")) - pd.Timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = runs.parent / "session-driver" / "machine-log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("# comment btime=2000-01-01T00:00:00Z\n"
                       "2026-10-29T03:00:00Z boot_id=a btime=2026-10-29T02:59:00Z cpu=\"x\" nproc=4\n"
                       f"{boot2} boot_id=b btime={boot2} cpu=\"x\" nproc=4\n")
        # every cell after r105 also starts on the second boot, so only seed 105 straddles the boots
        r110 = next(d for d in reruns if d.name.startswith("h7t-110__arm-heuristic"))
        twin110 = runs / "session" / r110.name.replace("h7t-110__", "exp1-n1000__")
        (twin110 / "summary.json").unlink()
        with open(runs / "session" / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"run_id": twin110.name, "status": "abandoned", "attempt": 2}) + "\n")
        _run_main(runs, out)
        h7s = json.loads((out / "tables" / "latency_optimality.json").read_text(encoding="utf-8"))["h7s"]
        assert h7s["pairing"] == {"status": "checked", "split_seeds": [105]}, h7s["pairing"]
        assert h7s["determinism"]["status"] == "pass" and h7s["determinism"]["not_checked"] == [r110.name], h7s
        tests = pd.read_csv(out / "tables" / "h7s_tests.csv")
        assert (tests["n_pairs"] == 8).all(), tests[["n_pairs"]]
        runs_t = pd.read_csv(out / "tables" / "h7s_runs.csv")
        assert set(runs_t.loc[~runs_t["paired"].astype(bool), "seed"]) == {105}
        assert _row(runs_t, run_id=r110.name)["twin_status"] == "not checked (twin abandoned)"
        assert h7s["verdict"] == "supported", h7s["verdict"]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "--write-synthetic":
        write_synthetic_tree(Path(args[1]))
        return 0
    tests: list[tuple[str, Callable[[], None]]] = [
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 - report every failing test
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
