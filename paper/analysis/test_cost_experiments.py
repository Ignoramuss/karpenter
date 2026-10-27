"""Plain-assert tests for paper.analysis.cost_experiments on synthetic runs trees. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_cost_experiments

The synthetic session tree plants known effects (random generation equivalent to heuristic under P1/P2 and 10% worse
under P0; merge+shift 5% cheaper on W2 only; the default anneal-plan 3% cheaper with equal evictions and anneal-online
with 50% more evictions; no-potential 5% dearer, no-eviction-budget twice the evictions; B4 identical to B2 and
B4-balanced to B3), so every verdict path runs. Exp 1 has 5 seeds per cell, so its tests run with ANALYSIS_MIN_SEEDS
= 5; the other experiments have 10.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
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
from paper.analysis import cost_experiments as ce

DURATION_MIN = 180
WARMUP_MIN = 10
EARLY_EXTRA = 20.0  # cost_per_hour above the steady state before t = 100 min


# --- synthetic runs ------------------------------------------------------------------------------------------------


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _planner(label: str) -> dict[str, Any] | None:
    """The pipeline planner config behind a synthetic label (None for baselines)."""
    if label.startswith("B"):
        return None
    if "savings-only" in label:
        return {"name": "savings-only"}
    if "anneal-online" in label:
        t0 = float(label.split("t0=")[1])
        return {"name": "anneal-online", "params": {"lambda": 0.1, "t0": t0, "cooling": 0.99}}
    if "t0=" in label:
        t0 = float(label.split("t0=")[1].split("/")[0])
        cooling = float(label.split("cooling=")[1])
        return {"name": "anneal-plan", "params": {"lambda": 0.1, "t0": t0, "cooling": cooling}}
    if "anneal-plan" in label or label in ("full", "no-potential", "no-eviction-budget", "no-randomness"):
        return {"name": "anneal-plan", "params": {"lambda": 0.1, "t0": 1, "cooling": 0.98}}
    return {"name": "entropy-greedy", "params": {"lambda": 0.1}}


def _make_run(matrix: Path, group: str, label: str, scenario: str, nodes: int, seed: int, metrics: dict[str, float],
              timeseries: bool, invalid: bool = False) -> None:
    """One finished run directory in the simulator's layout (summary, manifest, optionally a timeseries). ``invalid``
    plants a PDB violation."""
    slug = label.replace("/", "-").replace("=", "").replace("+", "-").lower()
    run_id = f"{group}__arm-{slug}_scenario-{scenario.lower()}_nodes-{nodes}_seed-{seed}"
    run_id += f"__{zlib.crc32(run_id.encode()):08x}"
    d = matrix / run_id
    planner = _planner(label)
    steady = metrics["steady_cost_per_hour"]
    summary = {"run_id": run_id, "label": label, "arm": "pipeline" if planner else label, "scenario": scenario,
               "scenario_nodes": nodes, "seed": seed, "placement": "least-allocated", "scale_down": "spread",
               "sim_minutes": DURATION_MIN, "disruption_passes": 100, "full_simulations": 400, "fidelity_hits": 3,
               "fidelity_misses": 1, "fidelity": 0.75, "invalid": invalid, "timed_out": False,
               "invalid_reasons": ["pdb or do-not-disrupt violation"] if invalid else [],
               "pdb_violations": 1 if invalid else 0, "do_not_disrupt_violations": 0, "wall_pass_mean_ms": 50.0,
               "cpu_pass_mean_ms": 40.0, "nodes_removed": 10, "regret_evictions": 0, "re_disruptions": 1,
               "final_nodes": 100, "integrated_cost_post": steady * 3, "evictions": 100, "eviction_cost": 100,
               **metrics}
    arm: dict[str, Any] = {"name": "pipeline" if planner else f"{label}-baseline", "label": label}
    if planner:
        arm["pipeline"] = {"planner": planner, "candidates": {"name": "uniform-random"}}
    _write(d / "summary.json", summary)
    _write(d / "manifest.json", {"run_id": run_id, "git_sha": "abc", "git_dirty": False, "config": {
        "duration": "3h0m0s", "warmup": "10m0s", "arm": arm, "seed": seed,
        "scenario": {"name": scenario, "nodes": nodes}}})
    _write(d / "cell.json", {"seed": seed, "arm": arm, "scenario": {"name": scenario, "nodes": nodes}})
    if timeseries:
        lines = ["t_min,nodes,cost_per_hour,hist_cpu,hist_mem"]
        for t in range(DURATION_MIN + 1):  # one sample per minute, the last at the horizon
            cost = steady + (EARLY_EXTRA if t < 100 else 0.0)
            lines.append(f"{t:.2f},10,{cost:.6f},0;0;0;0;0;0;0;0;0;10,10;0;0;0;0;0;0;0;0;0")
        (d / "timeseries.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session_tree(root: Path, exp1_seeds: int = 5, seeds: int = 10, only: tuple[str, ...] = (),
                  b4: str = "exact") -> None:
    """The synthetic session matrix (see the module docstring); ``only`` restricts it to some experiment keys and
    ``b4`` sets how B4 relates to B2 ("exact", "equivalent": within 0.1%, "provisional": 10% dearer)."""
    rng = np.random.default_rng(0)
    m = root / "session"
    want = (lambda k: not only or k in only)
    if want("exp1"):
        for nodes, scenario in [(n, s) for n in (200, 1000) for s in ("W1", "W2", "W4")]:
            for seed in range(101, 101 + exp1_seeds):
                base = 50.0 + rng.normal(0, 2)
                group = f"exp1-n{nodes}"
                for planner in ("savings-only", "entropy-greedy", "anneal-plan"):
                    h = base * (1.05 if planner == "savings-only" else 1.0)
                    r = h * (1.10 if planner == "savings-only" else 1.0 + rng.normal(0, 0.002))
                    _make_run(m, group, f"heuristic/{planner}", scenario, nodes, seed,
                              {"steady_cost_per_hour": h}, timeseries=True)
                    bad = planner == "anneal-plan" and scenario == "W2" and nodes == 200 and seed == 103
                    _make_run(m, group, f"random/{planner}", scenario, nodes, seed, {"steady_cost_per_hour": r},
                              timeseries=True, invalid=bad)
                for b, f in (("B2", 1.02), ("B3", 1.04)):
                    _make_run(m, f"exp1-baselines-n{nodes}", b, scenario, nodes, seed,
                              {"steady_cost_per_hour": base * f, "evictions": 120 + seed, "nodes_removed": seed % 7,
                               "final_nodes": 90 + seed % 3}, timeseries=True)
        # An abandoned cell: counted as invalid, never paired.
        gone = "exp1-n200__arm-random-entropy-greedy_scenario-w4_nodes-200_seed-199__dead"
        _write(m / f"{gone}.failed-2" / "cell.json", {"seed": 199, "arm": {"label": "random/entropy-greedy"},
                                                       "scenario": {"name": "W4", "nodes": 200}})
        (m / "index.jsonl").write_text(json.dumps({"run_id": gone, "group": "exp1-n200", "status": "abandoned",
                                                   "attempt": 2, "error": "exit status 2"}) + "\n",
                                       encoding="utf-8")
    alternating = np.array([(-1) ** i * (1 + i) for i in range(seeds)], dtype=float)  # signed ranks cancel out
    if want("exp3"):
        for scenario in ("W2", "W3"):
            for i, seed in enumerate(range(101, 101 + seeds)):
                base = 100.0 + rng.normal(0, 5)
                for budget in ("lo", "hi"):
                    f = 0.95 if scenario == "W2" else 1 + 0.001 * alternating[i]
                    _make_run(m, "exp3", f"merge-only/budget-{budget}", scenario, 200, seed,
                              {"steady_cost_per_hour": 30.0, "integrated_cost_post": base, "evictions": 200,
                               "eviction_cost": 200}, timeseries=False)
                    _make_run(m, "exp3", f"merge+shift/budget-{budget}", scenario, 200, seed,
                              {"steady_cost_per_hour": 30.0, "integrated_cost_post": base * f,
                               "evictions": 200 + int(alternating[i]), "eviction_cost": 200 + int(alternating[i])},
                              timeseries=False)
    if want("exp4"):
        labels = common.experiment("exp4")["labels"]
        for scenario in ("W1", "W4"):
            for i, seed in enumerate(range(101, 101 + seeds)):
                cost, ev = 40.0 + rng.normal(0, 1), 300.0 + rng.normal(0, 10)
                for label in labels:
                    c, n = cost, ev
                    if label == "anneal-plan/t0=1/cooling=0.98":
                        c = cost * 0.97
                    elif label.startswith("anneal-online/"):
                        c, n = cost * (1 + 0.0005 * alternating[i]), ev * 1.5
                    elif label.startswith("anneal-plan/"):
                        c = cost * 0.99
                    _make_run(m, "exp4", label, scenario, 200, seed,
                              {"steady_cost_per_hour": c, "evictions": round(n), "eviction_cost": round(n)},
                              timeseries=False)
    if want("abl"):
        for scenario in ("W1", "W2"):
            for seed in range(101, 101 + seeds):
                cost, ev = 40.0 + rng.normal(0, 1), 300 + int(rng.integers(0, 20))
                for label, cf, ef in (("full", 1.0, 1), ("no-potential", 1.05, 1), ("no-eviction-budget", 1.0, 2),
                                      ("no-annealing", 1.01, 1), ("no-randomness", 1.0, 1)):
                    _make_run(m, "abl", label, scenario, 200, seed,
                              {"steady_cost_per_hour": cost * cf, "integrated_cost_post": cost * cf * 3,
                               "evictions": ev * ef, "eviction_cost": ev * ef}, timeseries=False)
    if want("b4"):
        # B4 and B4-balanced re-run B2 and B3 (of exp1-baselines-n200) on W1, W2 and W4 at 200 nodes.
        for scenario in ("W1", "W2", "W4"):
            for seed in range(101, 111):
                base = 50.0 + rng.normal(0, 2)
                pairs = {"B2": ("B4", 1.02), "B3": ("B4-balanced", 1.04)}
                for ref, (label, f) in pairs.items():
                    metrics = {"steady_cost_per_hour": base * f, "evictions": 120 + seed, "nodes_removed": seed % 7,
                               "final_nodes": 90 + seed % 3}
                    _make_run(m, "exp1-baselines-n200", ref, scenario, 200, seed, metrics, timeseries=False)
                    mine = dict(metrics)
                    if b4 == "equivalent":
                        mine["steady_cost_per_hour"] *= 1 + rng.normal(0, 0.0005)
                    elif b4 == "provisional":
                        mine["steady_cost_per_hour"] *= 1.10
                    _make_run(m, "b4", label, scenario, 200, seed, mine, timeseries=False)


@contextlib.contextmanager
def _captured_log() -> Iterator[list[str]]:
    """Collect the analysis logger's messages for the duration of the block."""
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Collect()
    common.logger.addHandler(handler)
    try:
        yield messages
    finally:
        common.logger.removeHandler(handler)


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


def _min_seeds(n: int | None) -> contextlib.AbstractContextManager[None]:
    return _env(**{common.MIN_SEEDS_ENV: None if n is None else str(n), common.GROUP_MAP_ENV: None})


def _outputs(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


EXPECTED_FILES = sorted(
    [f"tables/{t}.{x}" for t in ("exp1_h1", "exp1_secondary", "exp1_phases", "exp3_h3", "exp3_secondary", "exp4_h4",
                                 "exp4_clauses", "exp4_sweep", "fig8_ablation") for x in ("csv", "md")]
    + ["tables/cost_experiments.json", "figures/out/fig8_ablation.png", "figures/out/fig8_ablation.pdf"])


# --- tests -----------------------------------------------------------------------------------------------------------


def test_signed_rank_direction() -> None:
    assert common.rank_direction([2, 3, 4], [1, 1, 1]) == 1
    assert common.rank_direction([1, 1, 1], [2, 3, 4]) == -1
    assert common.rank_direction([1, 2], [1, 2]) == 0 and common.rank_direction([], []) == 0
    # Mean difference positive (one large gain) but most pairs negative: the signed ranks say "lower".
    a, b = np.array([100.0, 1, 1, 1, 1]), np.array([0.0, 2, 2, 2, 2])
    assert (a - b).mean() > 0 and common.rank_direction(a, b) == -1


def test_run_phases_matches_simulator_rule() -> None:
    # The simulator's steady state: the mean over samples with t >= 0.8 x horizon (one sample per minute, no
    # duplicate final row).
    t = list(range(181))
    ts = pd.DataFrame({"t_min": t, "cost_per_hour": [float(x) for x in t]})
    ph = ce.run_phases(ts, [10, 70], 0.8 * 180)
    assert ph["late"] == float(np.mean(range(144, 181))) and ph["late_from"] == 144.0 and ph["t_end"] == 180
    assert ph["early"] == float(np.mean(range(10, 70)))  # [10, 70): 60 samples
    one = ce.run_phases(pd.DataFrame({"t_min": [0.0], "cost_per_hour": [5.0]}), [10, 70], 0.0)
    assert one["late"] == 5.0 and math.isnan(one["early"])


def test_no_runs_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp, _captured_log() as log:
        root, out = Path(tmp) / "runs", Path(tmp) / "out"
        root.mkdir()
        ce.main(root, out)
        assert not out.exists(), "nothing may be written without runs"
        for key in ("exp1", "exp3", "exp4", "abl", "b4"):
            assert f"skipped {key}: no runs" in log, log


def test_synthetic_session() -> None:
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(5):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp1", "exp3", "exp4", "abl"))  # B4 has its own test (it shares B2/B3 cells)
        outs = []
        for i in range(2):
            out = Path(tmp) / f"out{i}"
            ce.main(root, out)
            outs.append(_outputs(out))
        assert sorted(outs[0]) == EXPECTED_FILES, sorted(outs[0])
        assert outs[0] == outs[1], "outputs must be byte-identical across runs"
        for name, data in outs[0].items():
            assert tmp.encode() not in data, f"{name} embeds an absolute path"
        out = Path(tmp) / "out0"
        doc = json.loads(outs[0]["tables/cost_experiments.json"])
        _check_exp1(out, doc["exp1"])
        _check_exp3(out, doc["exp3"])
        _check_exp4(out, doc["exp4"])
        _check_ablation(out, doc["abl"])
        assert "b4" not in doc, "no B4 runs: skipped"


def _check_exp1(out: Path, doc: dict[str, Any]) -> None:
    v = doc["validity"]
    assert v["n_invalid"] == 2 and v["n_runs"] == 241 and v["n_violation"] == 1 and v["n_abandoned"] == 1, v
    assert v["n_retried"] == 1 and v["n_succeeded_on_retry"] == 0  # the abandoned cell's set-aside attempt
    h1 = doc["h1"]
    assert h1["verdict"] == "supported" and h1["complete"] and h1["n_tests"] == 12, h1
    assert h1["n_tests_with_data"] == 12 and h1["n_equivalent_holm"] == 12 and h1["n_vetoed"] == 0
    assert h1["n_pairs_dropped_invalid"] == 1
    p0 = doc["p0_contrast"]
    assert p0["n_tests"] == 6 and p0["n_vetoed"] == 6 and p0["n_equivalent_holm"] == 0
    table = pd.read_csv(out / "tables" / "exp1_h1.csv")
    assert list(table["family"].unique()) == ["H1", "H1-P0-contrast", "reference"]
    assert len(table) == 12 + 6 + 12
    assert (table.loc[table["family"] == "H1", "m"] == 12).all() and (
        table.loc[table["family"] == "H1-P0-contrast", "m"] == 6).all()
    w2 = table.loc[(table["planner"] == "anneal-plan") & (table["scenario"] == "W2") & (table["nodes"] == 200)
                   & (table["family"] == "H1")].iloc[0]
    assert w2["n_pairs"] == 4 and w2["n_dropped_invalid"] == 1 and w2["outcome"] == "equivalent"
    assert abs(w2["margin_pct"] - 3.0) < 1e-6 and w2["ci90_lo_pct"] > -3 and w2["ci90_hi_pct"] < 3
    assert w2["p_floor"] == 0 and bool(w2["holm_reachable"])
    p0row = table.loc[table["family"] == "H1-P0-contrast"].iloc[0]
    assert p0row["outcome"] == "higher beyond margin" and abs(p0row["pct_diff"] - 10.0) < 1e-6
    assert bool(p0row["vetoed"])
    ref = table.loc[table["family"] == "reference"].iloc[0]
    assert ref["a"] == "B2" and ref["n_runs"] == 5 and math.isnan(ref["p_tost"])
    sec = pd.read_csv(out / "tables" / "exp1_secondary.csv")
    assert "p_wilcoxon" not in sec.columns, "secondary metrics are descriptive: no tests"
    fs = sec.loc[sec["metric"] == "full_simulations_per_pass"]
    assert fs.loc[fs["label"].isin(["B2", "B3"]), "mean"].isna().all(), "baselines do not count full simulations"
    assert (fs.loc[~fs["label"].isin(["B2", "B3"]), "mean"] == 4.0).all()
    rnd = sec.loc[(sec["label"] == "random/savings-only") & (sec["metric"] == "integrated_cost_post")]
    assert (rnd["ref"] == "heuristic/savings-only").all() and np.allclose(rnd["diff_pct"], 10.0)
    phases = pd.read_csv(out / "tables" / "exp1_phases.csv")
    assert doc["phases"]["late_vs_steady_cost_max_abs_diff"] < 1e-5  # the timeseries is written at 6 decimals
    early = phases.loc[phases["phase"] == "early"]
    late = phases.loc[phases["phase"] == "late"].reset_index(drop=True)
    assert np.allclose(early["mean"].to_numpy() - late["mean"].to_numpy(), EARLY_EXTRA, atol=1e-4)
    assert (early["t_from_min"] == WARMUP_MIN).all() and (early["t_to_min"] == WARMUP_MIN + 60).all()
    assert (late["t_from_min"] == 144).all() and (late["t_to_min"] == DURATION_MIN).all()


def _check_exp3(out: Path, doc: dict[str, Any]) -> None:
    h3 = doc["h3"]
    assert h3["verdict"] == "partial" and h3["n_tests"] == 4 and h3["n_lower"] == 2, h3
    by = {(t["scenario"], t["budget"]): t for t in h3["tests"]}
    assert by[("W2", "lo")]["outcome"] == "lower" and by[("W2", "lo")]["expect_met"] is True
    assert by[("W3", "hi")]["outcome"] == "no significant difference" and by[("W3", "hi")]["expect_met"] is False
    assert abs(by[("W2", "hi")]["pct_diff"] + 5.0) < 1e-9
    table = pd.read_csv(out / "tables" / "exp3_h3.csv")
    assert set(table["family"]) == {"H3"} and len(table) == 4 and (table["m"] == 4).all()
    sec = pd.read_csv(out / "tables" / "exp3_secondary.csv")
    assert "p_wilcoxon" not in sec.columns and {"evictions", "eviction_cost"} <= set(sec["metric"])


def _check_exp4(out: Path, doc: dict[str, Any]) -> None:
    h4 = doc["h4"]
    assert doc["default_anneal_label"] == "anneal-plan/t0=1/cooling=0.98"
    assert h4["verdict"] == "supported", h4
    assert h4["plan_clause"]["n_holds"] == 2 and h4["online_clause"]["n_holds"] == 6
    table = pd.read_csv(out / "tables" / "exp4_h4.csv")
    assert set(table["family"]) == {"H4"} and len(table) == 8 and (table["m"] == 8).all()
    assert "p_holm_all" not in table.columns
    assert list(table["metric"]) == ["steady_cost_per_hour"] * 2 + ["evictions"] * 6
    clauses = pd.read_csv(out / "tables" / "exp4_clauses.csv")
    assert len(clauses) == 8 and set(clauses["state"]) == {"holds"}
    plan = clauses.loc[clauses["clause"] == "plan"]
    assert (plan["bound_metric"] == "evictions").all() and (plan["pct_diff_hi"] < 10).all()
    online = clauses.loc[clauses["clause"] == "online"]
    assert (online["bound_metric"] == "steady_cost_per_hour").all() and (online["pct_diff_lo"] > -1).all()
    sweep = pd.read_csv(out / "tables" / "exp4_sweep.csv")
    assert len(sweep) == 20 and sweep.loc[sweep["default"], "label"].unique().tolist() == [
        "anneal-plan/t0=1/cooling=0.98"]
    online = sweep.loc[sweep["planner"] == "anneal-online"]
    assert (online["cooling"] == 0.99).all() and sorted(online["t0"].unique()) == [0.3, 1.0, 3.0]


def _check_ablation(out: Path, doc: dict[str, Any]) -> None:
    table = pd.read_csv(out / "tables" / "fig8_ablation.csv")
    assert len(table) == 2 * 5 * 4
    full = table.loc[table["configuration"] == "full"]
    assert (full["family"] == "reference").all() and full["p_wilcoxon"].isna().all()
    tested = table.loc[table["family"] == "ablation"]
    assert len(tested) == 8 and (tested["metric"] == "steady_cost_per_hour").all() and (tested["m"] == 8).all()
    descriptive = table.loc[table["family"] == "descriptive"]
    assert len(descriptive) == 2 * 4 * 3 and descriptive["p_wilcoxon"].isna().all()
    row = table.loc[(table["configuration"] == "no-potential") & (table["metric"] == "steady_cost_per_hour")]
    assert (row["outcome"] == "higher").all() and np.allclose(row["diff_pct"], 5.0)
    nb = table.loc[(table["configuration"] == "no-eviction-budget") & (table["metric"] == "eviction_cost")]
    assert (nb["family"] == "descriptive").all() and np.allclose(nb["diff_pct"], 100.0)
    same = tested.loc[tested["configuration"] == "no-randomness"]
    assert (same["outcome"] == "no significant difference").all() and (same["p_wilcoxon"] == 1.0).all()
    assert len(doc["tests"]) == 8 and len(doc["descriptive"]) == 24


def test_ablation_family_m8() -> None:
    # The ablation family is the 8 steady-state cost tests (4 variants x {W1, W2}), Holm over the 8 and nothing
    # else: with every difference positive at 10 pairs, p = 2/1024 and p_holm = 8 * 2/1024.
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(None):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("abl",))
        sel = common.select_runs(common.load_runs(root, include_abandoned=True), "abl")
        tests = common.evaluate(sel, common.comparisons("abl"))
        assert len(tests) == 8 and set(tests["family"]) == {"ablation"} and (tests["m"] == 8).all()
        nop = tests.loc[tests["a"] == "no-potential"]
        assert np.allclose(nop["p_wilcoxon"], 2 / 1024) and np.allclose(nop["p_holm"], 8 * 2 / 1024), nop
        assert (tests["holm_reachable"]).all()


def test_partial_data() -> None:
    """Only some arms finished: tables still come out, verdicts say incomplete."""
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(3):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp1", "abl"), seeds=3, exp1_seeds=3)
        for d in sorted((root / "session").glob("exp1-n*__arm-random-*")):
            (d / "summary.json").unlink(missing_ok=True)  # unfinished: no summary
        for d in sorted((root / "session").glob("abl__arm-no-*_scenario-w2*")):
            (d / "summary.json").unlink(missing_ok=True)
        out = Path(tmp) / "out"
        with _captured_log() as log:
            ce.main(root, out)
        assert "skipped exp3: no runs" in log and "skipped exp4: no runs" in log
        doc = json.loads((out / "tables" / "cost_experiments.json").read_text())
        assert set(doc) == {"exp1", "abl"}
        h1 = doc["exp1"]["h1"]
        assert h1["verdict"] == "incomplete" and h1["n_tests_with_data"] == 0 and not h1["complete"], h1
        assert h1["verdict_on_available"] == "not supported" and len(h1["missing_configurations"]) == 12
        assert not (out / "tables" / "exp3_h3.csv").exists()
        table = pd.read_csv(out / "tables" / "exp1_h1.csv")
        assert (table.loc[table["family"] != "reference", "outcome"] == "no data").all()
        abl = pd.read_csv(out / "tables" / "fig8_ablation.csv")
        w2 = abl.loc[(abl["scenario"] == "W2") & (abl["configuration"] != "full")]
        assert (w2["n_valid"] == 0).all() and (w2.loc[w2["family"] == "ablation", "outcome"] == "no data").all()
        assert not doc["abl"]["complete"]
        assert (out / "figures" / "out" / "fig8_ablation.png").exists()


def test_h1_missing_test_enters_holm_with_p1() -> None:
    """H1 keeps m = 12: a test with fewer than two valid pairs is not equivalent and enters Holm with p_TOST = 1. With
    one such test (11 equivalent) H1 is still supported; with the configuration absent altogether, session runs are
    missing and the verdict is "incomplete" (the rule on the runs so far still says supported)."""
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(5):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp1",))
        target = sorted((root / "session").glob("exp1-n1000__arm-random-anneal-plan_scenario-w4_*"))
        assert len(target) == 5
        for d in target[1:]:  # 4 of 5 runs invalid: 1 valid pair
            s = json.loads((d / "summary.json").read_text())
            s.update(pdb_violations=1, invalid=True)
            (d / "summary.json").write_text(json.dumps(s))
        out = Path(tmp) / "out"
        ce.main(root, out)
        table = pd.read_csv(out / "tables" / "exp1_h1.csv")
        h1 = table.loc[table["family"] == "H1"]
        one = h1.loc[(h1["scenario"] == "W4") & (h1["nodes"] == 1000) & (h1["planner"] == "anneal-plan")].iloc[0]
        assert one["n_pairs"] == 1 and one["p_tost"] == 1 and one["p_tost_holm"] == 1 and one["m"] == 12
        assert one["outcome"] == "insufficient data" and not bool(one["equivalent_holm"])
        assert np.allclose(h1["p_tost_holm"], common.holm(h1["p_tost"])), "Holm over all 12, the p = 1 included"
        doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp1"]["h1"]
        assert doc["n_equivalent_holm"] == 11 and doc["verdict"] == "supported" and doc["complete"], doc
        for d in target:
            shutil.rmtree(d)
        ce.main(root, out)
        doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp1"]["h1"]
        assert doc["verdict"] == "incomplete" and doc["verdict_on_available"] == "supported", doc
        assert doc["missing_configurations"] == [{"arm": "random/anneal-plan", "stratum": "scenario=W4, nodes=1000",
                                                  "n_finished": 0, "min_seeds": 5}]
        table = pd.read_csv(out / "tables" / "exp1_h1.csv")
        assert len(table.loc[table["family"] == "H1"]) == 12 and (table.loc[table["family"] == "H1", "m"] == 12).all()


def test_h1_veto() -> None:
    """A test whose unadjusted 90% CI lower bound exceeds +margin vetoes H1 even with 11 equivalent."""
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(5):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp1",))
        for d in sorted((root / "session").glob("exp1-n200__arm-random-entropy-greedy_scenario-w1_*")):
            s = json.loads((d / "summary.json").read_text())
            s["steady_cost_per_hour"] *= 1.06
            (d / "summary.json").write_text(json.dumps(s))
        out = Path(tmp) / "out"
        ce.main(root, out)
        doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp1"]["h1"]
        assert doc["n_equivalent_holm"] == 11 and doc["n_vetoed"] == 1 and doc["verdict"] == "not supported", doc


def test_h4_inconclusive_on_unreachable_tests() -> None:
    """With 7 pairs the exact Wilcoxon floor is 2/128 and Holm over H4's 8 tests cannot reject (8 * 2/128 > 0.05):
    every clause rests on an unreachable test, so it is inconclusive, never support, and so is H4."""
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(None):
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp4",))
        for d in sorted((root / "session").glob("exp4__arm-entropy-greedy_*")):
            seed = int(d.name.split("_seed-")[1].split("__")[0])
            if seed <= 103:  # 3 of 10 greedy runs invalid in each scenario: 7 pairs
                s = json.loads((d / "summary.json").read_text())
                s.update(do_not_disrupt_violations=1, invalid=True)
                (d / "summary.json").write_text(json.dumps(s))
        out = Path(tmp) / "out"
        ce.main(root, out)
        table = pd.read_csv(out / "tables" / "exp4_h4.csv")
        assert (table["n_pairs"] == 7).all() and (table["n_dropped_invalid"] == 3).all()
        assert np.allclose(table["p_floor"], 2 / 128) and not table["holm_reachable"].astype(bool).any(), table
        assert not table["significant"].astype(bool).any() and set(table["outcome"]) == {"inconclusive"}
        clauses = pd.read_csv(out / "tables" / "exp4_clauses.csv")
        assert set(clauses["state"]) == {"inconclusive"}, clauses[["clause", "state"]]
        doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp4"]["h4"]
        assert doc["verdict"] == "inconclusive" and doc["plan_clause"]["n_inconclusive"] == 2, doc
        assert doc["online_clause"]["n_inconclusive"] == 6 and doc["online_clause"]["n_holds"] == 0


def test_b4_exact_equivalent_provisional() -> None:
    """B4 is exact when every seed's summary matches B2's (B4-balanced: B3's) exactly, equivalent when all six TOSTs
    pass after Holm, otherwise provisional."""
    for mode, want in (("exact", "exact"), ("equivalent", "equivalent"), ("provisional", "provisional")):
        with tempfile.TemporaryDirectory() as tmp, _min_seeds(None):
            root = Path(tmp) / "runs"
            _session_tree(root, only=("b4",), b4=mode)
            out = Path(tmp) / "out"
            ce.main(root, out)
            doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["b4"]["b4"]
            assert doc["status"] == want, (mode, doc)
            table = pd.read_csv(out / "tables" / "b4_parity.csv")
            assert len(table) == 6 and (table["n_seeds"] == 10).all() and (table["m"] == 6).all()
            if mode == "exact":
                assert (table["n_match"] == 10).all() and table["exact"].all() and doc["n_match"] == 60
                assert doc["n_equivalent_holm"] == 6
            elif mode == "equivalent":
                assert (table["n_match"] == 0).all() and (table["n_match_evictions"] == 10).all()
                assert table["equivalent_holm"].all() and not table["exact"].any()
            else:
                assert not table["equivalent_holm"].any() and set(table["outcome"]) == {"higher beyond margin"}
            # One seed missing from B4: not exact any more (and the configuration is cut at 9 finished runs).
            if mode == "exact":
                victim = sorted((root / "session").glob("b4__arm-b4_scenario-w1_*"))[0]
                shutil.rmtree(victim)
                ce.main(root, out)
                doc = json.loads((out / "tables" / "cost_experiments.json").read_text())["b4"]["b4"]
                assert doc["status"] == "provisional" and not doc["complete"], doc


def test_group_map_pilot_layout() -> None:
    """Session analyses run on pilot-named groups through ANALYSIS_GROUP_MAP."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        with _min_seeds(5):
            _session_tree(root, only=("exp1",))
        pilot = root / "pilot"
        for d in sorted((root / "session").glob("exp1-n200__*")) + sorted(
                (root / "session").glob("exp1-baselines-n200__*")):
            new = d.name.replace("exp1-baselines-n200", "pilot-baselines").replace("exp1-n200", "pilot-exp1")
            target = pilot / new
            target.mkdir(parents=True)
            for f in sorted(d.iterdir()):
                text = f.read_text(encoding="utf-8").replace(d.name, new)
                (target / f.name).write_text(text, encoding="utf-8")
        out = Path(tmp) / "out"
        mapping = '{"exp1-n200": "pilot-exp1", "exp1-baselines-n200": "pilot-baselines"}'
        with _env(**{common.GROUP_MAP_ENV: mapping, common.MIN_SEEDS_ENV: "5"}):
            ce.main(pilot, out)
        h1 = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp1"]["h1"]
        assert h1["n_tests_with_data"] == 6 and h1["verdict"] == "incomplete", h1
        # Unmapped, with the default MIN_SEEDS, the same pilot-named tree is no experiment's.
        with _min_seeds(None), _captured_log() as log:
            ce.main(pilot, Path(tmp) / "none")
        assert "skipped exp1: no runs" in log and not (Path(tmp) / "none").exists()


def main() -> int:
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
    print(f"{len(tests) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
