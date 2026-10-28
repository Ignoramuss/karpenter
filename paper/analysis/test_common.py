"""Plain-assert tests for paper.analysis.common. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_common
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import math
import os
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from paper.analysis import common

PILOT = common.REPO_ROOT / "experiments" / "runs" / "pilot"


def _close(x: float, y: float, tol: float = 1e-9) -> bool:
    return abs(x - y) <= tol


# --- statistics ------------------------------------------------------------------------------------------------------


def test_bootstrap_constant() -> None:
    for stat in ("mean", "median"):
        assert common.bootstrap_ci([5.0] * 10, stat=stat) == (5.0, 5.0, 5.0)
    assert common.bootstrap_ci([7.0]) == (7.0, 7.0, 7.0)
    est, lo, hi = common.bootstrap_ci([])
    assert math.isnan(est) and math.isnan(lo) and math.isnan(hi)
    # NaNs are ignored, not propagated.
    assert common.bootstrap_ci([2.0, float("nan"), 2.0]) == (2.0, 2.0, 2.0)


def test_bootstrap_properties() -> None:
    x = np.random.default_rng(0).normal(10, 2, size=30)
    first = common.bootstrap_ci(x)
    assert first == common.bootstrap_ci(x), "same seed must give the same interval"
    est, lo, hi = first
    assert lo < est < hi
    assert _close(est, float(x.mean()))
    med, mlo, mhi = common.bootstrap_ci(x, stat="median")
    assert mlo <= med <= mhi and _close(med, float(np.median(x)))
    # A 95% interval of the mean of 30 N(10, 2) draws is about +-0.7 wide on each side.
    assert 0.3 < est - lo < 1.5 and 0.3 < hi - est < 1.5
    assert common.bootstrap_ci(x, seed=1) != first
    pct, plo, phi = common.bootstrap_pct_ci(x * 1.1, x)
    assert _close(pct, 10.0, 1e-9) and _close(plo, 10.0, 1e-9) and _close(phi, 10.0, 1e-9)


def test_tost() -> None:
    rng = np.random.default_rng(0)
    b = 100 + rng.normal(0, 5, size=10)
    a_eq = b + rng.normal(0, 0.5, size=10)  # within a 3-unit margin
    t = common.tost_paired(a_eq, b, margin=3.0)
    assert t["equivalent"] and t["p_tost"] < 0.05 and t["noninferior"]
    assert -3.0 < t["ci90_lo"] <= t["mean_diff"] <= t["ci90_hi"] < 3.0
    assert t["p_tost"] == max(t["p_lower"], t["p_upper"]) and t["n"] == 10
    a_worse = b + 5 + rng.normal(0, 0.5, size=10)  # 5 units worse: outside the margin
    t = common.tost_paired(a_worse, b, margin=3.0)
    assert not t["equivalent"] and t["p_upper"] > 0.5 and t["p_lower"] < 0.05 and not t["noninferior"]
    a_noisy = b + rng.normal(0, 10, size=10)  # centered but too noisy to show equivalence
    assert not common.tost_paired(a_noisy, b, margin=3.0)["equivalent"]
    # The 90% interval matches the t interval computed by hand.
    d = a_eq - b
    from scipy import stats

    half = stats.t.ppf(0.95, 9) * d.std(ddof=1) / math.sqrt(10)
    t = common.tost_paired(a_eq, b, margin=3.0)
    assert _close(t["ci90_lo"], d.mean() - half) and _close(t["ci90_hi"], d.mean() + half)
    # Zero-variance differences: equivalent iff the constant lies inside the margin.
    assert common.tost_paired([2.0, 3.0], [1.0, 2.0], margin=3.0)["equivalent"]
    assert not common.tost_paired([5.0, 6.0], [1.0, 2.0], margin=3.0)["equivalent"]
    assert not common.tost_paired([1.0], [1.0], margin=3.0)["equivalent"]  # one pair: no test


def test_wilcoxon() -> None:
    assert common.wilcoxon_paired([1, 2, 3], [1, 2, 3]) == 1.0
    assert math.isnan(common.wilcoxon_paired([], []))
    a = np.arange(10, dtype=float) + 1
    p = common.wilcoxon_paired(a + 1, a)  # every difference +1: smallest possible exact p for n = 10 is 2/1024
    assert _close(p, 2 / 1024)
    assert common.wilcoxon_paired(a, a + 1) == p


def test_wilcoxon_floor_and_reachability() -> None:
    assert common.wilcoxon_floor(5) == 2 / 32 and common.wilcoxon_floor(10) == 2 / 1024
    assert common.wilcoxon_floor(0) == 1.0 and math.isnan(common.wilcoxon_floor(float("nan")))
    # The floor is what the exact test returns when every difference has the same sign.
    assert _close(common.wilcoxon_paired(np.arange(1.0, 11.0), np.zeros(10)), 2 / 1024)
    assert _close(common.wilcoxon_paired(np.arange(1.0, 6.0), np.zeros(5)), common.wilcoxon_floor(5))
    # Holm over m tests: reachable iff floor * m < 0.05. H6 (m = 12): 9 pairs can (12 * 2/512 = 0.047), 8 cannot.
    assert common.holm_reachable([2 / 512], 12).all() and not common.holm_reachable([2 / 256], 12).any()
    assert common.holm_reachable([2 / 1024] * 16, 16).all() and not common.holm_reachable([2 / 512] * 16, 16).any()
    assert not common.holm_reachable([math.nan], 1).any()
    # The exact test discards zero differences, so the floor is that of the non-zero ones (5 here, not 6 pairs).
    e = common.paired_effect([1.0, 2.0, 3.0, 4.0, 5.0, 5.0], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    assert e["p_floor"] == common.wilcoxon_floor(5) and e["n_pairs"] == 6 and e["n_nonzero"] == 5, e
    assert e["p_wilcoxon"] == e["p_floor"]  # all five non-zero differences of one sign: the floor is attained
    assert common.paired_effect([], [])["p_floor"] == 1.0
    assert common.tost_floor(2) == 0.0 and common.tost_floor(1) == 1.0 and common.tost_floor(0) == 1.0
    assert common.sign_floor(10) == 1 / 1024 and common.sign_floor(0) == 1.0


def test_holm() -> None:
    # Sorted 0.005, 0.01, 0.03, 0.04 with m = 4: 0.02, 0.03, 0.06, max(0.06, 0.04) = 0.06; back in input order.
    adj = common.holm([0.01, 0.04, 0.03, 0.005])
    assert np.allclose(adj, [0.03, 0.06, 0.06, 0.02])
    adj = common.holm([0.5, float("nan"), 0.01])  # NaN passes through and does not count toward m
    assert _close(adj[0], 0.5) and math.isnan(adj[1]) and _close(adj[2], 0.02)
    assert np.allclose(common.holm([0.9, 0.8]), [1.0, 1.0])  # 2 * 0.8 caps at 1, and adjusted p never decreases
    assert np.allclose(common.holm([0.04, 0.2]), [0.08, 0.2])
    assert common.holm([]).size == 0


def _synthetic_runs() -> pd.DataFrame:
    rows = []
    for seed in range(1, 6):
        for label, offset in (("A", 1.0), ("B", 0.0)):
            rows.append({"scenario": "W1", "nodes": 200, "seed": seed, "label": label,
                         "metric": 10.0 * seed + offset, "valid": True, "placement": "least-allocated"})
    df = pd.DataFrame(rows)
    df.loc[(df.seed == 3) & (df.label == "B"), "valid"] = False  # invalid in one arm
    df = df.loc[~((df.seed == 5) & (df.label == "A"))]  # missing in the other arm
    df.loc[(df.seed == 4) & (df.label == "A"), "metric"] = np.nan  # valid but no value: dropped too
    return df.reset_index(drop=True)


def test_paired_drops_invalid() -> None:
    df = _synthetic_runs()
    p = common.paired(df, a="A", b="B", metric="metric")
    a, b, keys, dropped = p  # unpacks as the four documented values
    assert list(keys["seed"]) == [1, 2]
    assert np.allclose(a, [11.0, 21.0]) and np.allclose(b, [10.0, 20.0])
    # G4: the valid run with no value (seed 4) is undefined, counted apart from the invalid one (seed 3).
    assert dropped == 3 and p.dropped_missing == 1 and p.dropped_invalid == 1 and p.dropped_undefined == 1
    assert list(keys.columns) == ["scenario", "nodes", "seed"]
    # Dict selectors and duplicate detection.
    q = common.paired(df, a={"label": "A", "placement": "least-allocated"}, b="B", metric="metric")
    assert q.n == 2
    dup = pd.concat([df, df.loc[df.label == "B"]], ignore_index=True)
    try:
        common.paired(dup, a="A", b="B", metric="metric")
    except ValueError as e:
        assert "sharing keys" in str(e)
    else:
        raise AssertionError("duplicate keys within an arm must raise")
    al = common.align(df, ["A", "B"], "metric")
    assert al.n == 2 and al.arms == ["A", "B"] and al.dropped == 3


def test_evaluate() -> None:
    rng = np.random.default_rng(0)
    rows = []
    for scenario in ("W1", "W2"):
        for seed in range(101, 111):
            base = 100 + rng.normal(0, 5)
            rows.append({"scenario": scenario, "nodes": 200, "seed": seed, "label": "heuristic/x", "valid": True,
                         "cost": base})
            rows.append({"scenario": scenario, "nodes": 200, "seed": seed, "label": "random/x", "valid": True,
                         "cost": base + rng.normal(0, 0.5) + (0 if scenario == "W1" else 8)})
    df = pd.DataFrame(rows)
    comp = common.Comparison(experiment="t", name="r vs h", family="F", test="tost", metric="cost", a="random/x",
                             b="heuristic/x", margin=0.03)
    out = common.evaluate(df, [comp])
    assert list(out["scenario"]) == ["W1", "W2"] and list(out["n_pairs"]) == [10, 10]
    assert list(out["equivalent"]) == [True, False]
    assert np.allclose(out["p_holm"], common.holm(out["p_tost"]))
    assert out.loc[0, "equivalent_holm"] is True and out.loc[1, "equivalent_holm"] is False
    assert _close(out.loc[0, "margin"], 0.03 * out.loc[0, "mean_b"])
    assert list(out.columns[:9]) == common.COMPARISON_COLUMNS[:9]
    w = common.evaluate(df, [common.Comparison(experiment="t", name="w", family="G", test="wilcoxon", metric="cost",
                                               a="random/x", b="heuristic/x")])
    assert w.loc[1, "p_wilcoxon"] < 0.01 and w.loc[1, "significant"] and w["equivalent_holm"].isna().all()
    assert list(w["p_floor"]) == [2 / 1024] * 2 and list(w["holm_reachable"]) == [True, True], w
    # A t-based TOST with at least two pairs has no floor: 0, so it can always reach significance.
    assert (out["p_floor"] == 0).all() and out["holm_reachable"].all() and (out["m"] == 2).all()
    assert list(out["outcome"]) == ["equivalent", "higher beyond margin"], out["outcome"]
    assert list(out["expect_met"]) == [None, None], "no expectation set"
    assert common.evaluate(df.iloc[0:0], [comp]).empty


# --- loading -----------------------------------------------------------------------------------------------------------


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _fake_tree(root: Path) -> None:
    """A small runs tree: one pipeline run, one baseline run, a failed attempt, an unfinished cell, an abandoned one."""
    m = root / "session"
    pipe = m / "exp1-n200__arm-x_scenario-w1_seed-101__aaaa"
    _write(pipe / "summary.json", {
        "run_id": pipe.name, "label": "random/anneal-plan", "arm": "pipeline", "scenario": "W1", "scenario_nodes": 200,
        "seed": 101, "placement": "least-allocated", "scale_down": "spread", "steady_cost_per_hour": 40.5,
        "evictions": 12, "evictions_by": {"merge": 10, "shift": 2}, "commands_started": {"delete": 3},
        "fidelity_hits": 0, "fidelity_misses": 0, "fidelity": 0, "invalid": False, "timed_out": True,
        "invalid_reasons": ["exceeded max wall time"],
    })
    _write(pipe / "manifest.json", {"run_id": pipe.name, "git_sha": "abc", "git_dirty": True, "config": {
        "duration": "3h0m0s", "warmup": "10m0s",
        "arm": {"name": "pipeline", "label": "random/anneal-plan", "pipeline": {
            "candidates": {"name": "uniform-random", "params": {"sourceFraction": 0.25}},
            "moves": [{"name": "random-merge"}, {"name": "random-shift"}], "movesMode": "pooled",
            "planner": {"name": "anneal-plan", "params": {"lambda": 0.5, "t0": 0.3, "cooling": 0.98,
                                                          "temperatureUnit": "per-node", "maxSteps": 400}},
            "executor": {"name": "shift-evict"}, "potential": {"name": "util-entropy"},
            "search": {"maxFullSimulations": 16, "maxAttempts": 500}, "budgets": {"podEvictionsPerMinute": 10}}}}})
    with gzip.open(pipe / "decisions.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write('{"cycle": 0, "stage": "plan"}\n{"cycle": 1, "stage": "execute"}\n')
    (pipe / "timeseries.csv").write_text(
        "t_min,nodes,cost_per_hour,hist_cpu,hist_mem\n"
        "0.00,3,1.5,0;1;0;0;0;0;0;0;0;2,3;0;0;0;0;0;0;0;0;0\n"
        "1.00,2,1.0,0;0;0;0;0;0;0;0;0;2,2;0;0;0;0;0;0;0;0;0\n", encoding="utf-8")
    (pipe / "podsets.jsonl").write_text(
        '{"t_min":40.00,"cost_per_hour":2.0,"nodes":2,"shapes":[[250,512,3]]}\n'
        '{"t_min":10.00,"cost_per_hour":3.0,"nodes":3,"shapes":[[250,512,3],[1000,2048,1]]}\n', encoding="utf-8")
    base = m / "exp1-baselines-n200__arm-b2_scenario-w1_seed-101__bbbb"
    _write(base / "summary.json", {"run_id": base.name, "label": "B2", "arm": "B2-when-empty-or-underutilized",
                                   "scenario": "W1", "scenario_nodes": 200, "seed": 101, "steady_cost_per_hour": 41.0,
                                   "evictions_by": {"drain": 5}, "fidelity_hits": 1, "fidelity_misses": 1,
                                   "fidelity": 0.5, "invalid": False, "timed_out": False,
                                   "wall_passes_over_1m": 2})
    (base / "decisions.jsonl").write_text('{"cycle": 0}\n', encoding="utf-8")
    _write(m / f"{base.name}.failed-1" / "summary.json", {"run_id": base.name})  # a set-aside attempt
    _write(m / "exp1-n200__unfinished__cccc" / "cell.json", {"seed": 102})  # still running: no summary
    gone = "exp1-n200__arm-y_scenario-w2_seed-103__dddd"
    _write(m / f"{gone}.failed-2" / "cell.json", {"runID": gone, "seed": 103, "arm": {"label": "random/x"},
                                                   "scenario": {"name": "W2", "nodes": 200}})
    (m / "index.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"run_id": gone, "group": "exp1-n200", "status": "failed", "attempt": 1},
        {"run_id": gone, "group": "exp1-n200", "status": "abandoned", "attempt": 2, "error": "exit status 2"},
        {"run_id": base.name, "group": "exp1-baselines-n200", "status": "done", "attempt": 2},
    ]) + "\n", encoding="utf-8")


def test_partial_summary_and_duplicate_runs() -> None:
    # A summary without its identity fields takes them from the run's config instead of loading as NaN (which would
    # make it collide with its arm's other runs), and an arm with two runs on one seed leaves that stratum unpaired
    # (counted, logged) rather than crashing the analysis.
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "session"
        for seed, nodes in ((101, 200), (101, 1000)):
            d = m / f"exp1-n{nodes}__arm-x_seed-{seed}__{nodes}"
            _write(d / "summary.json", {"run_id": d.name, "label": "random/x", "seed": seed, "scenario": "W1"})
            _write(d / "manifest.json", {"run_id": d.name, "config": {
                "seed": seed, "scenario": {"name": "W1", "nodes": nodes}, "arm": {"name": "pipeline", "label": "random/x"},
                "placement": "most-allocated", "scaleDown": "spread"}})
        runs = common.load_runs(Path(tmp))
        assert sorted(runs["nodes"].tolist()) == [200, 1000] and set(runs["placement"]) == {"most-allocated"}, runs
    df = pd.DataFrame({"scenario": ["W1"] * 4, "nodes": [200] * 4, "seed": [1, 1, 1, 2], "valid": True,
                       "label": ["a", "a", "b", "b"], "m": [1.0, 2.0, 3.0, 4.0]})
    c = common.Comparison(experiment="t", name="a vs b", family="F", test="wilcoxon", metric="m", a="a", b="b")
    out = common.evaluate(df, [c])
    assert len(out) == 1 and out.loc[0, "n_pairs"] == 0 and out.loc[0, "n_dropped_invalid"] == 2, out
    assert common.paired_lenient(df, a="a", b="b", metric="m", log=False).n == 0


def test_load_runs_synthetic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fake_tree(root)
        dirs = common.discover_runs(root)
        assert [d.name for d in dirs] == ["exp1-baselines-n200__arm-b2_scenario-w1_seed-101__bbbb",
                                          "exp1-n200__arm-x_scenario-w1_seed-101__aaaa"]
        assert common.discover_runs(root / "session") == dirs  # a single matrix works as the root too
        df = common.load_runs(root)
        assert len(df) == 2 and list(df.columns[:len(common.ID_COLUMNS)]) == common.ID_COLUMNS
        assert all(c in df.columns for c in common.RUN_COLUMNS)
        p = df.set_index("label").loc["random/anneal-plan"]
        assert p["group"] == "exp1-n200" and p["experiment"] == "exp1" and p["matrix"] == "session"
        assert p["planner"] == "anneal-plan" and p["planner_lambda"] == 0.5 and p["planner_t0"] == 0.3
        assert p["planner_cooling"] == 0.98 and math.isnan(p["planner_alpha"]) and math.isnan(p["planner_mu"])
        assert p["planner_temperature_unit"] == "per-node" and p["planner_max_steps"] == 400
        assert p["moves"] == "random-merge+random-shift" and p["candidates"] == "uniform-random"
        assert p["executor"] == "shift-evict" and p["potential"] == "util-entropy" and p["moves_mode"] == "pooled"
        assert p["pod_evictions_per_minute"] == 10 and p["max_full_simulations"] == 16
        assert p["duration_min"] == 180 and p["warmup_min"] == 10 and p["git_sha"] == "abc"
        assert bool(p["git_dirty"]) and not bool(p["valid"]) and p["invalid_reason"] == "timed out"
        assert bool(p["invalid_timed_out"]) and not bool(p["invalid_violation"]) and p["n_set_aside"] == 0
        assert isinstance(p["config_id"], str) and isinstance(p["config_id_noseed"], str)
        assert p["evictions_by_merge"] == 10 and p["evictions_by_drain"] == 0 and p["commands_started"] == 3
        assert math.isnan(p["fidelity"])  # no tracked evictions: undefined, not zero
        assert math.isnan(p["wall_passes_over_1m"])  # field absent from this summary
        b = df.set_index("label").loc["B2"]
        assert bool(b["valid"]) and b["invalid_reason"] == "" and b["fidelity"] == 0.5
        assert b["n_set_aside"] == 1, "its first attempt was set aside; the retry succeeded and is valid"
        assert pd.isna(b["config_id"]), "no manifest: no configuration identity"
        assert b["commands_started"] == 0 and math.isnan(b["planner_lambda"]) and math.isnan(b["duration_min"])
        assert b["wall_passes_over_1m"] == 2
        with_abandoned = common.load_runs(root, include_abandoned=True)
        assert len(with_abandoned) == 3
        ab = with_abandoned.set_index("seed").loc[103]
        assert not bool(ab["valid"]) and ab["invalid_reason"] == "abandoned: exit status 2"
        assert ab["label"] == "random/x" and ab["scenario"] == "W2" and ab["group"] == "exp1-n200"
        assert bool(ab["invalid_abandoned"])
        report = common.validity_report(with_abandoned)
        assert list(report["n_invalid"]) == [0, 2] and list(report["n_abandoned"]) == [0, 1]
        assert list(report["n_timed_out"]) == [0, 1] and list(report["n_violation"]) == [0, 0]
        assert list(report["n_retried"]) == [1, 1] and list(report["n_succeeded_on_retry"]) == [1, 0]
        # Per-run files.
        pdir = root / "session" / "exp1-n200__arm-x_scenario-w1_seed-101__aaaa"
        assert [d["cycle"] for d in common.load_decisions(pdir)] == [0, 1]
        assert [d["cycle"] for d in common.load_decisions(dirs[0])] == [0]
        assert list(common.load_decisions(root)) == []
        ts = common.load_timeseries(pdir)
        assert "hist_cpu" not in ts.columns and ts["hist_cpu_9"].tolist() == [2, 2]
        assert ts["hist_mem_0"].dtype == np.int64 and ts[[f"hist_cpu_{i}" for i in range(10)]].sum(axis=1).tolist() \
            == [3, 2]
        ps = common.load_podsets(pdir)
        assert [s["t_min"] for s in ps] == [10.0, 40.0] and ps[0]["shapes"] == [(250, 512, 3), (1000, 2048, 1)]
        assert common.load_podsets(root) == []
    empty = common.load_runs(Path(tempfile.gettempdir()) / "no-such-runs-root")
    assert empty.empty and all(c in empty.columns for c in common.RUN_COLUMNS)
    assert common.select_runs(empty, "exp1").empty
    # Without runs, every preregistered test still has its row (no pairs, entering Holm with p 1).
    none = common.evaluate_experiment(empty, "exp1")
    assert len(none) == 18 and (none["n_pairs"] == 0).all() and (none["p"] == 1).all(), none
    assert set(none["outcome"]) == {"no data"}
    assert (none["m"] == none["family"].map({"H1": 12, "H1-P0-contrast": 6})).all()


def test_load_runs_pilot() -> None:
    finished = sorted(PILOT.glob("*/summary.json"))
    df = common.load_runs(common.DEFAULT_RUNS_ROOT)
    pilot = df.loc[df["matrix"] == "pilot"]
    assert len(pilot) >= len(finished), (len(pilot), len(finished))  # more may finish while the test runs
    if not finished:
        print("  (no finished pilot runs yet)")
        return
    for col in common.RUN_COLUMNS + ["steady_cost_per_hour", "valid", "invalid_reason", "planner_lambda"]:
        assert col in df.columns, col
    assert set(pilot["experiment"]) == {"pilot"}
    assert set(pilot["group"]) <= {"pilot-baselines", "pilot-exp1", "pilot-calibrate"}
    assert (pilot["nodes"] == 200).all() and pilot["seed"].between(1, 5).all()
    assert pilot["valid"].dtype == bool and pilot["steady_cost_per_hour"].notna().all()
    assert pilot["run_id"].is_unique and list(pilot["run_id"]) == sorted(pilot["run_id"])
    assert (pilot["duration_min"] == 180).all() and pilot["git_sha"].notna().all()
    first = pilot.iloc[0]
    ts = common.load_timeseries(first["path"])
    hist = ts[[f"hist_cpu_{i}" for i in range(10)]]
    assert len(ts) > 100 and (hist.sum(axis=1) <= ts["nodes"]).all()
    assert len(common.load_podsets(first["path"])) >= 1
    assert next(iter(common.load_decisions(first["path"])), None) is not None
    cat = common.load_catalog()
    assert {"name", "family", "cpu_millicores", "memory_mib", "pods", "price_per_hour"} <= set(cat.columns)
    assert len(cat) > 0 and list(cat["name"]) == sorted(cat["name"])


def test_registry_and_group_map() -> None:
    assert set(common.EXPERIMENTS) == {"exp1", "realism", "exp2", "exp3", "exp4", "exp5", "exp6", "exp7", "h7s",
                                       "exp8", "abl", "b4"}
    exp1 = common.comparisons("exp1")
    assert [(c.a, c.b, c.family) for c in exp1] == [
        ("random/entropy-greedy", "heuristic/entropy-greedy", "H1"),
        ("random/anneal-plan", "heuristic/anneal-plan", "H1"),
        ("random/savings-only", "heuristic/savings-only", "H1-P0-contrast"),
    ]
    assert all(c.test == "tost" and c.margin == 0.03 and c.margin_type == "relative" for c in exp1)
    assert all(c.by == ("scenario", "nodes") for c in exp1)
    abl = common.comparisons("abl")
    assert len(abl) == 4 and {c.metric for c in abl} == {"steady_cost_per_hour"} and {c.b for c in abl} == {"full"}
    h4 = common.comparisons("exp4")
    assert [(c.a, c.metric) for c in h4] == [("anneal-plan/t0=1/cooling=0.98", "steady_cost_per_hour")] + [
        (f"anneal-online/t0={t}", "evictions") for t in ("0.3", "1", "3")]
    h7 = common.comparisons("exp7")
    assert [(c.family, c.b, c.metric) for c in h7] == [
        ("H7", "heuristic/entropy-greedy", "cpu_pass_mean_ms"), ("H7", "heuristic/entropy-greedy", "cpu_pass_p95_ms"),
        ("H7-B2", "B2", "cpu_pass_mean_ms"), ("H7-B2", "B2", "cpu_pass_p95_ms")]
    assert all(c.where == {"nodes": [1000]} for c in h7)
    assert common.comparisons("exp5")[0].a == {"label": "random/entropy-greedy", "placement": "most-allocated"}
    assert common.experiment("exp3")["params"]["budgets_pods_per_minute"] == {"lo": 18, "hi": 36}
    assert "heuristic/anneal-plan" in common.experiment("exp8")["labels"]
    # H6's lambda points are fixed per baseline, in the comparisons and in params.
    h6 = {(c.b, c.a) for c in common.comparisons("exp6")}
    assert h6 == {("B2", "random/entropy-greedy/lambda=0.05"), ("B3", "random/entropy-greedy/lambda=0.5"),
                  ("B5", "random/entropy-greedy/lambda=0.1")}
    # Every declared family has exactly its preregistered number of design tests (G6).
    sizes: dict[str, int] = {}
    for key in common.EXPERIMENTS:
        for c in common.comparisons(key):
            sizes[c.family] = sizes.get(c.family, 0) + len(common.design_strata(c))
    assert sizes == {"H1": 12, "H1-P0-contrast": 6, "H1-realism": 4, "H3": 4, "H4": 8, "H5": 10, "H6": 12, "H7": 2,
                     "H7-B2": 2, "H7-S": 2, "ablation": 8, "B4": 6}, sizes
    assert {f for f, v in common.FAMILIES.items() if v["m"] is None} == {"H2", "H2-cost", "H2-norm", "H2-w5",
                                                                         "H2-w20"}
    assert all(common.FAMILIES[f]["m"] == n for f, n in sizes.items())
    for key in common.EXPERIMENTS:  # every entry expands and names its metric
        common.comparisons(key)
        assert common.experiment(key)["primary_metric"]
    # Session groups and labels in the registry exist in the session matrix (guards against typos), and every
    # session group is used by some experiment.
    matrix = (common.REPO_ROOT / "experiments" / "matrix" / "session.yaml").read_text(encoding="utf-8")
    used = set()
    h7_timing = (common.REPO_ROOT / "experiments" / "matrix" / "h7-timing.yaml").read_text(encoding="utf-8")
    import re
    names = re.findall(r"^- name: (\S+)$", h7_timing, flags=re.M)
    h7t = [g for g in names if g.startswith("h7t-")]
    assert len(h7t) == 10 and {common._identity(f"{g}__x", "h7-timing")["group"] for g in h7t} == {"h7-timing"}
    fillers = [g for g in names if g not in h7t]  # load-keeping fillers: their own groups, in no experiment
    assert fillers == ["h7fill-111", "h7fill-112"] and all(
        common._identity(f"{g}__x", "h7-timing")["group"] == g for g in fillers)
    assert not {g for e in common.EXPERIMENTS.values() for g in e["groups"]} & set(fillers)
    assert common.experiment("h7s")["groups"] == ["h7-timing"]
    for key, e in common.EXPERIMENTS.items():
        if key == "h7s":  # its groups are the H7 timing pass's (experiments/matrix/h7-timing.yaml), checked above
            continue
        for g in e["groups"] + list((e["params"] or {}).get("lp_groups", [])):
            assert f"- name: {g}\n" in matrix, (key, g)
            used.add(g)
        for label in e["labels"] + e["baselines"]:
            assert f"label: {label}" in matrix or f"label: {label}," in matrix, (key, label)
    assert set(re.findall(r"^  - name: (\S+)$", matrix, flags=re.M)) == used
    with _group_map(None):
        assert common.experiment_groups("exp1") == common.experiment("exp1")["groups"]
        # Exp 7's select keeps only W1 entropy-greedy and B2 rows of the reused Exp 1 groups.
        df = pd.DataFrame({"group": ["exp1-n200", "exp1-n200", "exp1-n200", "exp7-n50"],
                           "label": ["random/entropy-greedy", "random/anneal-plan", "random/entropy-greedy", "B2"],
                           "scenario": ["W1", "W1", "W2", "W1"], "valid": [True] * 4})
        assert common.select_runs(df, "exp7", include_cut=True).index.tolist() == [0, 3]
        assert common.select_runs(df, "exp7").empty, "one run per configuration: every configuration is cut"
    with _group_map(json.dumps({"exp1-n200": "pilot-exp1", "exp1-baselines-n200": ["pilot-baselines"]})):
        assert common.experiment_groups("exp1") == ["pilot-exp1", "pilot-baselines", "exp1-n1000",
                                                    "exp1-baselines-n1000"]
        df = pd.DataFrame({"group": ["pilot-exp1", "pilot-baselines", "exp1-n200", "pilot-calibrate"],
                           "label": ["x", "B2", "y", "z"], "valid": [True, False, True, True],
                           "scenario": ["W1", "W1", "W1", "W1"]})
        assert list(common.select_runs(df, "exp1", include_cut=True)["group"]) == ["pilot-exp1", "pilot-baselines"]
        assert list(common.select_runs(df, "exp1", valid_only=True, include_cut=True)["group"]) == ["pilot-exp1"]
    with _group_map("[1, 2]"):
        try:
            common.group_map()
        except ValueError:
            pass
        else:
            raise AssertionError("a non-object map must be rejected")


@contextlib.contextmanager
def _group_map(value: str | None) -> Iterator[None]:
    """Set (or clear, for None) ANALYSIS_GROUP_MAP for the duration of the block."""
    saved = os.environ.get(common.GROUP_MAP_ENV)
    if value is None:
        os.environ.pop(common.GROUP_MAP_ENV, None)
    else:
        os.environ[common.GROUP_MAP_ENV] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(common.GROUP_MAP_ENV, None)
        else:
            os.environ[common.GROUP_MAP_ENV] = saved


# --- preregistered run rules (G2-G6) --------------------------------------------------------------------------------


def _mini_run(matrix: Path, run_id: str, seed: int, label: str = "random/x", scenario: str = "W1", nodes: int = 200,
              summary: dict | None = None, manifest: dict | None = None, config: dict | None = None) -> Path:
    """A finished run with a minimal summary and a manifest whose config is ``config`` (default: label, scenario,
    seed)."""
    d = matrix / run_id
    s = {"run_id": run_id, "label": label, "scenario": scenario, "scenario_nodes": nodes, "seed": seed,
         "placement": "least-allocated", "scale_down": "spread", "steady_cost_per_hour": 10.0, "evictions": 5,
         "nodes_removed": 3, "final_nodes": 7, "pdb_violations": 0, "do_not_disrupt_violations": 0,
         "invalid": False, "timed_out": False}
    s.update(summary or {})
    cfg = config if config is not None else {"seed": seed, "scenario": {"name": scenario, "nodes": nodes},
                                             "arm": {"name": "pipeline", "label": label}}
    m = {"run_id": run_id, "config": cfg}
    m.update(manifest or {})
    _write(d / "summary.json", s)
    _write(d / "manifest.json", m)
    return d


def test_validity_reasons_counted_separately() -> None:
    # G2: violation from the violation fields (not from invalid == true, which the simulator also sets for timeouts
    # and errors), error, timed out; each reason counted on its own.
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "pilot"
        _mini_run(m, "g__a", 1)
        _mini_run(m, "g__b", 2, summary={"pdb_violations": 2, "invalid": True,
                                         "invalid_reasons": ["pdb or do-not-disrupt violation"]})
        _mini_run(m, "g__c", 3, summary={"do_not_disrupt_violations": 1, "invalid": True})
        _mini_run(m, "g__d", 4, summary={"error": "boom", "invalid": True, "invalid_reasons": ["simulation error"]})
        _mini_run(m, "g__e", 5, summary={"timed_out": True, "invalid": True,
                                         "invalid_reasons": ["exceeded max wall time"]})
        _mini_run(m, "g__f", 6, summary={"invalid": True, "invalid_reasons": ["something else"]})  # no rule applies
        _mini_run(m, "g__h", 7, summary={"pdb_violations": 1, "error": "also", "invalid": True})
        runs = common.load_runs(Path(tmp)).set_index("run_id")
        assert list(runs["valid"]) == [True, False, False, False, False, True, False], runs["valid"]
        assert runs.loc["g__b", "invalid_reason"] == "violation (pdb 2, do-not-disrupt 0)"
        assert runs.loc["g__h", "invalid_reason"] == "violation (pdb 1, do-not-disrupt 0); error: also"
        report = common.validity_report(runs.reset_index(), by=("group",)).iloc[0]
        assert (report["n_runs"], report["n_valid"], report["n_invalid"]) == (7, 2, 5)
        assert (report["n_violation"], report["n_error"], report["n_timed_out"]) == (3, 2, 1), report
        assert report["n_abandoned"] == 0 and report["n_wrong_binary"] == 0


def test_set_aside_attempts_counted() -> None:
    # G2: set-aside attempts (<run_id>.failed-<n>) are counted (n_retried); a cell that succeeds on retry is valid; a
    # cell with two set-aside attempts and no summary is abandoned (the tree decides, as for the runner); one with a
    # single set-aside attempt and no summary is waiting for its retry, not a finished run.
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "session"
        ok = _mini_run(m, "exp3__retried", 101)
        _write(m / "exp3__retried.failed-1" / "cell.json", {"seed": 101})
        for n in (1, 2):
            _write(m / f"exp3__gone.failed-{n}" / "cell.json", {"seed": 102, "arm": {"label": "random/x"},
                                                                 "scenario": {"name": "W1", "nodes": 200}})
        _write(m / "exp3__waiting.failed-1" / "cell.json", {"seed": 103})
        assert common.set_aside_attempts(Path(tmp)) == {("session", "exp3__gone"): 2, ("session", "exp3__retried"): 1,
                                                        ("session", "exp3__waiting"): 1}
        runs = common.load_runs(Path(tmp), include_abandoned=True).set_index("run_id")
        assert sorted(runs.index) == ["exp3__gone", "exp3__retried"], runs.index
        assert bool(runs.loc["exp3__retried", "valid"]) and runs.loc["exp3__retried", "n_set_aside"] == 1
        assert bool(runs.loc["exp3__gone", "invalid_abandoned"]) and runs.loc["exp3__gone", "n_set_aside"] == 2
        assert runs.loc["exp3__gone", "invalid_reason"] == "abandoned" and runs.loc["exp3__gone", "seed"] == 102
        report = common.validity_report(runs.reset_index(), by=("group",)).iloc[0]
        assert (report["n_retried"], report["n_set_aside"], report["n_succeeded_on_retry"]) == (2, 3, 1), report
        assert (report["n_abandoned"], report["n_invalid"]) == (1, 1)
        pending = common.pending_retries(Path(tmp), runs.reset_index())
        assert pending.to_dict("records") == [{"matrix": "session", "run_id": "exp3__waiting", "group": "exp3",
                                               "n_set_aside": 1}]
        assert ok.exists()


def test_wrong_binary_invalidates_session_runs() -> None:
    # G2: under experiments/runs/session only, a run whose manifest binary_revision differs from
    # experiments/session-binary.txt, or whose binary_modified is true, is invalid ("wrong binary"); without that file
    # the check is skipped with a warning.
    sha = "0123456789abcdef0123456789abcdef01234567"
    with tempfile.TemporaryDirectory() as tmp:
        exp = Path(tmp) / "experiments"
        s, pil = exp / "runs" / "session", exp / "runs" / "pilot"
        _mini_run(s, "exp1-n200__good", 101, manifest={"binary_revision": sha, "binary_modified": False})
        _mini_run(s, "exp1-n200__other", 102, manifest={"binary_revision": "f" * 40, "binary_modified": False})
        _mini_run(s, "exp1-n200__dirty", 103, manifest={"binary_revision": sha, "binary_modified": True})
        _mini_run(s, "exp1-n200__old", 104)  # an older binary: no stamp at all
        _mini_run(pil, "pilot-exp1__other", 1, manifest={"binary_revision": "f" * 40})  # not a session run
        with _captured() as log:
            runs = common.load_runs(exp / "runs").set_index("run_id")
        assert runs["valid"].all(), "no session-binary.txt: nothing is flagged"
        assert any("session-binary.txt not found: binary check skipped for 4 session run(s)" in m for m in log), log
        (exp / "session-binary.txt").write_text(f"Gate 3 build\n{sha}\n", encoding="utf-8")
        for root in (exp / "runs", s):  # the tree or the session matrix alone
            runs = common.load_runs(root).set_index("run_id")
            flagged = sorted(runs.index[runs["invalid_wrong_binary"]])
            assert flagged == ["exp1-n200__dirty", "exp1-n200__old", "exp1-n200__other"], flagged
            assert not runs.loc[flagged, "valid"].any() and bool(runs.loc["exp1-n200__good", "valid"])
        assert runs.loc["exp1-n200__dirty", "invalid_reason"] == "wrong binary (revision 0123456789ab, modified)"
        assert runs.loc["exp1-n200__old", "invalid_reason"] == "wrong binary (revision missing)"
        assert bool(common.load_runs(exp / "runs").set_index("run_id").loc["pilot-exp1__other", "valid"])
        report = common.validity_report(common.load_runs(exp / "runs"), by=("matrix",)).set_index("matrix")
        assert report.loc["session", "n_wrong_binary"] == 3 and report.loc["pilot", "n_wrong_binary"] == 0
        # The location can also be given explicitly (then every run of a matrix named "session" is checked).
        alt = Path(tmp) / "elsewhere.txt"
        alt.write_text("f" * 40 + "\n", encoding="utf-8")
        runs = common.load_runs(exp / "runs", session_binary=alt).set_index("run_id")
        assert sorted(runs.index[runs["invalid_wrong_binary"]]) == ["exp1-n200__dirty", "exp1-n200__good",
                                                                    "exp1-n200__old"]


def test_determinism_exclusion() -> None:
    # A session-determinism.txt that reports FAIL, or a commit other than the session binary's, invalidates every
    # session run ("nondeterminism"); PASS at the binary's commit leaves them valid; an absent file skips the check.
    sha = "0123456789abcdef0123456789abcdef01234567"
    with tempfile.TemporaryDirectory() as tmp:
        exp = Path(tmp) / "experiments"
        s = exp / "runs" / "session"
        _mini_run(s, "exp1-n200__a", 101, manifest={"binary_revision": sha, "binary_modified": False})
        _mini_run(exp / "runs" / "pilot", "pilot-exp1__a", 1)
        (exp / "session-binary.txt").write_text(sha + "\n", encoding="utf-8")
        with _captured() as log:
            assert common.load_runs(exp / "runs")["valid"].all()
        assert any("session-determinism.txt not found" in m for m in log), log
        for text, valid in ((f"PASS {sha} 14/14", True), (f"FAIL {sha}", False), ("PASS " + "f" * 40, False)):
            (exp / "session-determinism.txt").write_text(text + "\n", encoding="utf-8")
            runs = common.load_runs(exp / "runs").set_index("run_id")
            assert bool(runs.loc["exp1-n200__a", "valid"]) is valid, text
            assert bool(runs.loc["exp1-n200__a", "invalid_nondeterminism"]) is (not valid), text
            assert bool(runs.loc["pilot-exp1__a", "valid"])  # never a session run
        assert "nondeterminism" in runs.loc["exp1-n200__a", "invalid_reason"]


def test_session_configurations_cover_the_matrix() -> None:
    # run_checks expands experiments/matrix/session.yaml into every configuration (seeds collapsed), so cut
    # configurations with no finished run at all are still listed.
    from paper.analysis import run_checks
    configs = run_checks.session_configurations()
    assert len(configs) == 174 and len(set(configs)) == 174
    assert ("exp6-b5", "B0", "W2", 200.0, "most-allocated", "spread") in configs
    assert ("exp7-b2-n1000", "B2", "W1", 1000.0, "least-allocated", "spread") in configs


@contextlib.contextmanager
def _captured() -> Iterator[list[str]]:
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
def _min_seeds(value: str | None) -> Iterator[None]:
    saved = os.environ.get(common.MIN_SEEDS_ENV)
    if value is None:
        os.environ.pop(common.MIN_SEEDS_ENV, None)
    else:
        os.environ[common.MIN_SEEDS_ENV] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(common.MIN_SEEDS_ENV, None)
        else:
            os.environ[common.MIN_SEEDS_ENV] = saved


def test_completeness_cuts_at_min_seeds() -> None:
    # G3: a configuration (group, label, scenario, nodes, placement, scale_down) with fewer than MIN_SEEDS finished
    # runs (valid or invalid; abandoned cells count) is cut from every selection and listed with its count.
    with tempfile.TemporaryDirectory() as tmp, _min_seeds(None):
        m = Path(tmp) / "session"
        for seed in range(101, 111):
            _mini_run(m, f"exp3__full_seed-{seed}", seed, label="merge-only/budget-lo", scenario="W2")
        for seed in range(101, 110):  # 9 finished (one invalid) + 1 abandoned = 10
            extra = {"pdb_violations": 1} if seed == 101 else {}
            _mini_run(m, f"exp3__short_seed-{seed}", seed, label="merge+shift/budget-lo", scenario="W2", summary=extra)
        for n in (1, 2):
            _write(m / f"exp3__short_seed-110.failed-{n}" / "cell.json", {
                "seed": 110, "arm": {"label": "merge+shift/budget-lo"}, "scenario": {"name": "W2", "nodes": 200}})
        for seed in range(101, 110):  # 9 finished: cut
            _mini_run(m, f"exp3__cut_seed-{seed}", seed, label="merge-only/budget-hi", scenario="W2")
        runs = common.load_runs(Path(tmp), include_abandoned=True)
        assert common.MIN_SEEDS == 10 and common.min_seeds() == 10
        comp = common.completeness(runs).set_index("label")
        assert comp.loc["merge-only/budget-lo", "n_finished"] == 10 and not comp.loc["merge-only/budget-lo", "cut"]
        assert comp.loc["merge+shift/budget-lo", "n_finished"] == 10 and comp.loc["merge+shift/budget-lo",
                                                                                  "n_invalid"] == 2
        assert not comp.loc["merge+shift/budget-lo", "cut"], "an abandoned cell is a finished (invalid) run"
        assert comp.loc["merge-only/budget-hi", "n_finished"] == 9 and comp.loc["merge-only/budget-hi", "cut"]
        sel = common.select_runs(runs, "exp3")
        assert set(sel["label"]) == {"merge-only/budget-lo", "merge+shift/budget-lo"} and len(sel) == 20
        assert len(common.select_runs(runs, "exp3", include_cut=True)) == 29
        # Cut arms leave their tests without pairs (p 1), and the report counts their runs.
        tests = common.evaluate(sel, common.comparisons("exp3"))
        hi = tests.loc[tests["comparison"].str.endswith("budget-hi") & (tests["scenario"] == "W2")].iloc[0]
        assert hi["n_pairs"] == 0 and hi["p"] == 1 and hi["outcome"] == "no data"
        everything = common.select_runs(runs, "exp3", include_cut=True)
        report = common.validity_report(everything, by=("group",)).iloc[0]
        assert report["n_in_cut_configurations"] == 9
        # A run listed once per experiment that uses it still counts once toward its configuration's completeness.
        twice = pd.concat([everything.assign(experiment="a"), everything.assign(experiment="b")], ignore_index=True)
        report = common.validity_report(twice, by=("experiment",))
        assert list(report["n_in_cut_configurations"]) == [9, 9], report
        missing = common.missing_configurations(common.select_runs(runs, "exp3", include_cut=True),
                                                common.comparisons("exp3"))
        assert {"arm": "merge-only/budget-hi", "stratum": "scenario=W2, nodes=200", "n_finished": 9,
                "min_seeds": 10} in missing
        with _min_seeds("5"):
            assert common.min_seeds() == 5 and not common.cut_mask(runs).any()
            assert len(common.select_runs(runs, "exp3")) == 29
        with _min_seeds("zero"):
            try:
                common.min_seeds()
            except ValueError:
                pass
            else:
                raise AssertionError("a malformed ANALYSIS_MIN_SEEDS must be rejected")


def test_duplicate_consistency() -> None:
    # G5: identical configurations (label and group ignored) on the same seed must match on steady_cost_per_hour,
    # evictions, nodes_removed and final_nodes; the table counts the sets and those that differ.
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "session"
        for seed in (101, 102, 103):
            cfg = {"seed": seed, "scenario": {"name": "W2", "nodes": 200}, "arm": {"name": "pipeline", "label": "x",
                                                                                     "pipeline": {"k": 1}}}
            _mini_run(m, f"exp1-n200__a_seed-{seed}", seed, label="random/entropy-greedy", scenario="W2",
                      config=cfg)
            other = json.loads(json.dumps(cfg))
            other["arm"]["label"], other["runID"], other["maxWall"] = "random/entropy-greedy/lambda=0.1", "y", "9h"
            _mini_run(m, f"exp6__b_seed-{seed}", seed, label="random/entropy-greedy/lambda=0.1", scenario="W2",
                      config=other, summary={"evictions": 6} if seed == 103 else {})
            different = json.loads(json.dumps(cfg))
            different["arm"]["pipeline"]["k"] = 2
            _mini_run(m, f"exp6__c_seed-{seed}", seed, label="random/entropy-greedy/lambda=2", scenario="W2",
                      config=different)
        runs = common.load_runs(Path(tmp))
        sets = common.duplicate_sets(runs)
        assert len(sets) == 3 and list(sets["seed"]) == [101, 102, 103] and list(sets["n_runs"]) == [2, 2, 2]
        assert list(sets["consistent"]) == [True, True, False] and not sets.iloc[2]["match_evictions"]
        assert sets.iloc[0]["groups"] == "exp1-n200 + exp6", sets.iloc[0]
        table = common.duplicate_consistency(runs)
        assert list(table["configuration"])[-1] == "all" and len(table) == 2
        row, total = table.iloc[0], table.iloc[-1]
        assert (row["n_sets"], row["n_differ"], row["differing_metrics"], row["differing_seeds"]) == (
            3, 1, "evictions", "103"), row
        assert (total["n_sets"], total["n_differ"]) == (3, 1)
        none = common.duplicate_consistency(runs.loc[runs["group"] == "exp6"])
        assert none["configuration"].tolist() == ["all"] and none.iloc[0]["n_sets"] == 0


def test_run_checks_tables() -> None:
    # The run-level tables: validity per experiment and group (every reason, set-aside attempts, pending retries),
    # cut configurations with their counts, and the cross-group determinism check.
    from paper.analysis import run_checks

    with tempfile.TemporaryDirectory() as tmp, _min_seeds(None):
        m = Path(tmp) / "session"
        for seed in range(101, 111):
            cfg = {"seed": seed, "scenario": {"name": "W2", "nodes": 200}, "arm": {"name": "pipeline", "label": "x"}}
            _mini_run(m, f"exp1-n200__a_seed-{seed}", seed, label="random/entropy-greedy", scenario="W2", config=cfg)
            _mini_run(m, f"exp6__b_seed-{seed}", seed, label="random/entropy-greedy/lambda=0.1", scenario="W2",
                      config=cfg, summary={"final_nodes": 8} if seed == 110 else {})
        for seed in range(101, 104):  # 3 of 10: cut
            _mini_run(m, f"exp3__c_seed-{seed}", seed, label="merge-only/budget-lo", scenario="W2")
        _write(m / "exp6__b_seed-101.failed-1" / "cell.json", {"seed": 101})  # retried, then succeeded
        _write(m / "exp3__c_seed-104.failed-1" / "cell.json", {"seed": 104})  # waiting for its retry
        # The supplementary H7 timing pass: per-seed groups h7t-<seed> form one group (h7-timing), listed in the
        # validity table (with its pending retry) but kept out of the totals and the duplicate check, although each
        # rerun duplicates a session cell.
        h7 = Path(tmp) / "h7-timing"
        for seed in range(101, 111):
            for arm in ("heuristic", "random"):
                cfg = {"seed": seed, "scenario": {"name": "W1", "nodes": 1000},
                       "arm": {"name": "pipeline", "label": f"{arm}/entropy-greedy", "pipeline": {"candidates": {"name": arm}}}}
                _mini_run(m, f"exp1-n1000__arm-{arm}_seed-{seed}__{arm[0]}{seed}", seed, label=f"{arm}/entropy-greedy",
                          nodes=1000, config=cfg)
                _mini_run(h7, f"h7t-{seed}__arm-{arm}__{arm[0]}{seed}", seed, label=f"{arm}/entropy-greedy", nodes=1000,
                          config=cfg)
        _write(h7 / "h7t-105__arm-other__o105.failed-1" / "cell.json", {"seed": 105})  # waiting for its retry
        out = Path(tmp) / "out"
        with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
            run_checks.main(Path(tmp), out)
        validity = pd.read_csv(out / "tables" / "validity.csv")
        exp6 = validity.loc[(validity["experiment"] == "exp6") & (validity["group"] == "exp6")].iloc[0]
        assert exp6["n_runs"] == 10 and exp6["n_retried"] == 1 and exp6["n_succeeded_on_retry"] == 1
        exp3 = validity.loc[validity["experiment"] == "exp3"].iloc[0]
        assert exp3["n_in_cut_configurations"] == 3 and exp3["n_pending_retry"] == 1, exp3
        assert set(validity["experiment"]) >= {"exp1", "exp2", "exp3", "exp6", "h7s"}
        h7row = validity.loc[validity["experiment"] == "h7s"].iloc[0]
        assert h7row["group"] == "h7-timing" and h7row["n_runs"] == 20 and h7row["n_pending_retry"] == 1, h7row
        cut = pd.read_csv(out / "tables" / "cut_configurations.csv")
        assert cut[["group", "label", "n_finished", "min_seeds"]].values.tolist() == [
            ["exp3", "merge-only/budget-lo", 3, 10]] and cut.iloc[0]["experiments"] == "exp2, exp3", cut
        dup = pd.read_csv(out / "tables" / "duplicate_consistency.csv")
        assert dup.iloc[-1]["configuration"] == "all" and dup.iloc[-1]["n_sets"] == 10 and dup.iloc[-1]["n_differ"] == 1
        doc = json.loads((out / "tables" / "run_checks.json").read_text())
        assert doc["duplicates"]["n_differ"] == 1 and not doc["duplicates"]["all_match"]
        assert [s["seed"] for s in doc["duplicates"]["differing_sets"]] == [110]
        assert doc["n_cut_configurations"] == 1 and doc["min_seeds"] == 10 and doc["n_pending_retry"] == 1
        assert doc["n_runs"] == 10 + 10 + 3 + 20 and doc["supplementary_runs"] == {"h7s": 20}, doc["n_runs"]
        assert doc["binary_check"] == {"n_session_runs": 0, "files": {}, "checked": False, "n_wrong_binary": 0}
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
            run_checks.main(Path(tmp) / "none", Path(tmp) / "out")
        assert not (Path(tmp) / "out").exists(), "nothing is written without runs"


def test_holm_families_fixed_size() -> None:
    # G6: a registry family is Holm-adjusted at its declared size m whatever has data (H1: a test without pairs enters
    # with p 1 and m stays 12), only declared families exist, and a family of the wrong size is refused.
    rng = np.random.default_rng(1)
    rows = []
    for seed in range(101, 111):
        base = 50 + rng.normal(0, 2)
        for label, f in (("heuristic/entropy-greedy", 1.0), ("random/entropy-greedy", 1.0 + rng.normal(0, 0.001))):
            rows.append({"group": "exp1-n200", "scenario": "W1", "nodes": 200, "seed": seed, "label": label,
                         "placement": "least-allocated", "scale_down": "spread", "valid": True,
                         "steady_cost_per_hour": base * f})
    df = pd.DataFrame(rows)
    h1 = [c for c in common.comparisons("exp1") if c.family == "H1"]
    out = common.evaluate(df, h1)
    assert len(out) == 12 and (out["m"] == 12).all()
    have = out.loc[out["n_pairs"] > 0]
    assert len(have) == 1 and (out.loc[out["n_pairs"] == 0, "p_tost"] == 1).all()
    assert _close(have.iloc[0]["p_holm"], min(1.0, 12 * have.iloc[0]["p_tost"])), have
    assert bool(have.iloc[0]["equivalent_holm"]) and out["equivalent_holm"].sum() == 1
    assert not out.loc[out["n_pairs"] == 0, "holm_reachable"].any(), "p_floor 1: a missing test cannot reject"
    try:
        common.evaluate(df, h1[:1])  # one comparison of H1 gives 6 rows, not the declared 12
    except ValueError as err:
        assert "declares m = 12" in str(err), err
    else:
        raise AssertionError("a family of the wrong size must be refused")
    try:
        common.family_size("H4-sweep")
    except KeyError:
        pass
    else:
        raise AssertionError("undeclared families do not exist")


# --- output ------------------------------------------------------------------------------------------------------------


def test_duration_and_style() -> None:
    assert common.parse_duration_min("3h0m0s") == 180 and common.parse_duration_min("90s") == 1.5
    assert common.parse_duration_min("1h30m0s") == 90 and common.parse_duration_min("0s") == 0
    assert _close(common.parse_duration_min("1m30.5s"), 1 + 30.5 / 60)
    assert math.isnan(common.parse_duration_min("soon")) and math.isnan(common.parse_duration_min(None))
    assert common.style_for("random/entropy-greedy")["color"] == common.style_for("heuristic/entropy-greedy")["color"]
    assert common.style_for("random/entropy-greedy")["linestyle"] != common.style_for(
        "heuristic/entropy-greedy")["linestyle"]
    assert common.style_for("B2")["color"] == "#000000"
    assert common.style_for("random/entropy-greedy/lambda=2", index=1)["color"] == common.PALETTE[1]
    assert common.style_for("no-potential") == common.style_for("no-potential")


def test_outputs_reproducible() -> None:
    df = pd.DataFrame({"label": ["a|b", "c"], "x": [1.0 / 3, float("nan")], "n": [1, 2], "ok": [True, False],
                       "path": [Path("/abs/run"), Path("/abs/run2")]})
    digests = []
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        for tmp in (t1, t2):
            out = Path(tmp)
            csv_path, md_path = common.write_table(df, out, "demo")
            json_path = common.write_json({"b": np.float64(1.5), "a": [np.int64(2), float("nan")],
                                           "table": df.drop(columns="path")}, out, "demo")
            common.apply_style()
            fig, ax = plt.subplots(figsize=common.FIGSIZE["single"])
            for i, label in enumerate(["random/entropy-greedy", "B2"]):
                ax.plot([0, 1, 2], [i, i + 1, i * 2], label=label, **common.style_for(label))
            ax.set_xlabel(common.METRIC_LABELS["t_min"])
            ax.legend()
            png, pdf = common.save_figure(fig, out, "demo")
            assert not plt.get_fignums(), "save_figure closes the figure"
            digests.append([p.read_bytes() for p in (csv_path, md_path, json_path, png, pdf)])
            assert csv_path.parent == out / "tables" and png.parent == out / "figures" / "out"
        assert digests[0] == digests[1], "outputs must be byte-identical across runs"
    csv_text, md_text, json_text, png_bytes, pdf_bytes = (d for d in digests[0])
    assert csv_text.decode() == "label,x,n,ok\na|b,0.333333,1,True\nc,,2,False\n"
    assert md_text.decode().splitlines() == ["| label | x | n | ok |", "| --- | ---: | ---: | --- |",
                                             "| a\\|b | 0.3333 | 1 | true |", "| c |  | 2 | false |"]
    doc = json.loads(json_text)
    assert doc["a"] == [2, None] and doc["b"] == 1.5 and doc["table"][1]["x"] is None
    assert json_text.decode().index('"a"') < json_text.decode().index('"b"')  # sorted keys
    assert b"/abs" not in csv_text + md_text + json_text and b"Matplotlib" not in png_bytes + pdf_bytes
    assert b"CreationDate" not in pdf_bytes
    with tempfile.TemporaryDirectory() as tmp:
        try:
            common.write_json({"p": Path("/abs")}, Path(tmp), "never")
        except TypeError:
            assert not (Path(tmp) / "tables").exists(), "a refused write leaves nothing behind"
        else:
            raise AssertionError("paths must not be written to JSON")


def test_make_figures_clears_stale_outputs() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_figures", common.REPO_ROOT / "paper" / "make_figures.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    with tempfile.TemporaryDirectory() as tmp, _group_map(None):
        out = Path(tmp) / "out"
        stale = [out / "tables" / "exp1_h1.csv", out / "tables" / "exp1_h1.md", out / "tables" / "x.json",
                 out / "figures" / "out" / "fig3_frontier.png", out / "figures" / "out" / "fig3_frontier.pdf"]
        kept = [out / "tables" / "cache" / "lower_bounds.json", out / "figures" / "fig_script.py",
                out / "tables" / "notes.txt"]
        for f in stale + kept:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("old", encoding="utf-8")
        with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
            # --only leaves other modules' files alone; a full run removes every earlier output first.
            assert mf.main(["--runs", str(Path(tmp) / "none"), "--out", str(out), "--only", "frontier_fidelity"]) == 0
            assert all(f.exists() for f in stale)
            assert mf.main(["--runs", str(Path(tmp) / "none"), "--out", str(out)]) == 0
        assert not any(f.exists() for f in stale), "a skipped experiment must not leave stale outputs behind"
        assert all(f.exists() for f in kept), "only generated tables and figures are removed"


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
