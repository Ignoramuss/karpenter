"""Plain-assert tests for paper.analysis.cost_experiments on synthetic runs trees. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_cost_experiments

The synthetic session tree plants known effects (random generation equivalent to heuristic under P1/P2 and 10% worse
under P0; merge+shift 5% cheaper on W2 only; the default anneal-plan 3% cheaper with equal evictions and anneal-online
with 50% more evictions; no-potential 5% dearer, no-eviction-budget twice the evictions), so every verdict path runs.
"""

from __future__ import annotations

import contextlib
import os
import json
import logging
import math
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
        return {"name": "anneal-online", "params": {"lambda": 0.5, "t0": t0, "cooling": 0.99}}
    if "t0=" in label:
        t0 = float(label.split("t0=")[1].split("/")[0])
        cooling = float(label.split("cooling=")[1])
        return {"name": "anneal-plan", "params": {"lambda": 0.5, "t0": t0, "cooling": cooling}}
    if "anneal-plan" in label or label in ("full", "no-potential", "no-eviction-budget", "no-randomness"):
        return {"name": "anneal-plan", "params": {"lambda": 0.5, "t0": 0.3, "cooling": 0.98}}
    return {"name": "entropy-greedy", "params": {"lambda": 0.5}}


def _make_run(matrix: Path, group: str, label: str, scenario: str, nodes: int, seed: int, metrics: dict[str, float],
              timeseries: bool, invalid: bool = False) -> None:
    """One finished run directory in the simulator's layout (summary, manifest, optionally a timeseries)."""
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
               "nodes_removed": 10, "regret_evictions": 0, "re_disruptions": 1, "final_nodes": 100,
               "integrated_cost_post": steady * 3, "evictions": 100, "eviction_cost": 100, **metrics}
    arm: dict[str, Any] = {"name": "pipeline" if planner else f"{label}-baseline", "label": label}
    if planner:
        arm["pipeline"] = {"planner": planner, "candidates": {"name": "uniform-random"}}
    _write(d / "summary.json", summary)
    _write(d / "manifest.json", {"run_id": run_id, "git_sha": "abc", "git_dirty": False, "config": {
        "duration": "3h0m0s", "warmup": "10m0s", "arm": arm, "seed": seed}})
    _write(d / "cell.json", {"seed": seed, "arm": arm, "scenario": {"name": scenario, "nodes": nodes}})
    if timeseries:
        lines = ["t_min,nodes,cost_per_hour,hist_cpu,hist_mem"]
        times = list(range(DURATION_MIN + 1)) + [DURATION_MIN]  # the simulator repeats the final sample
        for t in times:
            cost = steady + (EARLY_EXTRA if t < 100 else 0.0)
            lines.append(f"{t:.2f},10,{cost:.6f},0;0;0;0;0;0;0;0;0;10,10;0;0;0;0;0;0;0;0;0")
        (d / "timeseries.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session_tree(root: Path, exp1_seeds: int = 5, seeds: int = 10, only: tuple[str, ...] = ()) -> None:
    """The synthetic session matrix (see the module docstring); ``only`` restricts it to some experiment keys."""
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
                              {"steady_cost_per_hour": base * f}, timeseries=True)
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
                    if label == "anneal-plan/t0=0.3/cooling=0.98":
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


def _outputs(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


EXPECTED_FILES = sorted(
    [f"tables/{t}.{x}" for t in ("exp1_h1", "exp1_secondary", "exp1_phases", "exp3_h3", "exp4_h4", "exp4_sweep",
                                 "fig8_ablation") for x in ("csv", "md")]
    + ["tables/cost_experiments.json", "figures/out/fig8_ablation.png", "figures/out/fig8_ablation.pdf"])


# --- tests -----------------------------------------------------------------------------------------------------------


def test_signed_rank_direction() -> None:
    assert ce.signed_rank_direction([2, 3, 4], [1, 1, 1]) == 1
    assert ce.signed_rank_direction([1, 1, 1], [2, 3, 4]) == -1
    assert ce.signed_rank_direction([1, 2], [1, 2]) == 0 and ce.signed_rank_direction([], []) == 0
    # Mean difference positive (one large gain) but most pairs negative: the signed ranks say "lower".
    a, b = np.array([100.0, 1, 1, 1, 1]), np.array([0.0, 2, 2, 2, 2])
    assert (a - b).mean() > 0 and ce.signed_rank_direction(a, b) == -1


def test_run_phases_matches_simulator_rule() -> None:
    t = list(range(181)) + [180]
    ts = pd.DataFrame({"t_min": t, "cost_per_hour": [float(x) for x in t]})
    ph = ce.run_phases(ts, [10, 70], 0.2)
    k = len(t) // 5  # the simulator: last max(1, n/5) samples
    assert ph["late"] == float(np.mean(t[-k:])) and ph["late_from"] == float(t[-k]) and ph["t_end"] == 180
    assert ph["early"] == float(np.mean(range(10, 70)))  # [10, 70): 60 samples
    one = ce.run_phases(pd.DataFrame({"t_min": [0.0], "cost_per_hour": [5.0]}), [10, 70], 0.2)
    assert one["late"] == 5.0 and math.isnan(one["early"])


def test_no_runs_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp, _captured_log() as log:
        root, out = Path(tmp) / "runs", Path(tmp) / "out"
        root.mkdir()
        ce.main(root, out)
        assert not out.exists(), "nothing may be written without runs"
        for key in ("exp1", "exp3", "exp4", "abl"):
            assert f"skipped {key}: no runs" in log, log


def test_synthetic_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        _session_tree(root)
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
        _check_exp3(doc["exp3"])
        _check_exp4(out, doc["exp4"])
        _check_ablation(out, doc["abl"])


def _check_exp1(out: Path, doc: dict[str, Any]) -> None:
    assert doc["validity"]["n_invalid"] == 2 and doc["validity"]["n_runs"] == 241  # one violation, one abandoned
    h1 = doc["h1"]
    assert h1["verdict"] == "supported" and h1["n_tests_with_data"] == 12 and h1["n_equivalent_holm"] == 12, h1
    assert h1["n_worse_beyond_margin"] == 0 and h1["n_pairs_dropped_invalid"] == 1
    p0 = doc["p0_contrast"]
    assert p0["n_tests_with_data"] == 6 and p0["n_worse_beyond_margin"] == 6 and p0["n_equivalent_holm"] == 0
    table = pd.read_csv(out / "tables" / "exp1_h1.csv")
    assert list(table["family"].unique()) == ["H1", "H1-P0-contrast", "reference"]
    assert len(table) == 12 + 6 + 12
    w2 = table.loc[(table["planner"] == "anneal-plan") & (table["scenario"] == "W2") & (table["nodes"] == 200)
                   & (table["family"] == "H1")].iloc[0]
    assert w2["n_pairs"] == 4 and w2["n_dropped_invalid"] == 1 and w2["outcome"] == "equivalent"
    assert abs(w2["margin_pct"] - 3.0) < 1e-6 and w2["ci90_lo_pct"] > -3 and w2["ci90_hi_pct"] < 3
    p0row = table.loc[table["family"] == "H1-P0-contrast"].iloc[0]
    assert p0row["outcome"] == "higher beyond margin" and abs(p0row["pct_diff"] - 10.0) < 1e-6
    ref = table.loc[table["family"] == "reference"].iloc[0]
    assert ref["a"] == "B2" and ref["n_runs"] == 5 and math.isnan(ref["p_tost"])
    sec = pd.read_csv(out / "tables" / "exp1_secondary.csv")
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
    assert (late["t_from_min"] == 146).all() and (late["t_to_min"] == DURATION_MIN).all()


def _check_exp3(doc: dict[str, Any]) -> None:
    h3 = doc["h3"]
    assert h3["verdict"] == "supported in 2 of 4 strata", h3["verdict"]
    by = {(t["scenario"], t["budget"]): t for t in h3["tests"]}
    assert by[("W2", "lo")]["outcome"] == "lower" and by[("W2", "lo")]["expect_met"] is True
    assert by[("W3", "hi")]["outcome"] == "no significant difference" and by[("W3", "hi")]["expect_met"] is False
    assert abs(by[("W2", "hi")]["pct_diff"] + 5.0) < 1e-9


def _check_exp4(out: Path, doc: dict[str, Any]) -> None:
    h4 = doc["h4"]
    assert doc["default_anneal_label"] == "anneal-plan/t0=0.3/cooling=0.98"
    assert h4["verdict"] == "supported" and h4["plan_clause"] == "supported" and h4["online_clause"] == "supported"
    assert all(p["cost_significantly_lower"] for p in h4["plan"]) and len(h4["online"]) == 6
    table = pd.read_csv(out / "tables" / "exp4_h4.csv")
    assert set(table["family"]) == {"H4", "H4-sweep"} and len(table) == 36
    assert (table["family"] == "H4").sum() == 16
    # 36 tests at the smallest exact p for 10 pairs (2/1024): Holm over all of them cannot reject.
    assert (table["p_holm_all"] >= 36 * 2 / 1024 - 1e-12).all()
    sweep = pd.read_csv(out / "tables" / "exp4_sweep.csv")
    assert len(sweep) == 20 and sweep.loc[sweep["default"], "label"].unique().tolist() == [
        "anneal-plan/t0=0.3/cooling=0.98"]
    online = sweep.loc[sweep["planner"] == "anneal-online"]
    assert (online["cooling"] == 0.99).all() and sorted(online["t0"].unique()) == [0.1, 0.3, 1.0]


def _check_ablation(out: Path, doc: dict[str, Any]) -> None:
    table = pd.read_csv(out / "tables" / "fig8_ablation.csv")
    assert len(table) == 2 * 5 * 4
    full = table.loc[table["configuration"] == "full"]
    assert (full["family"] == "reference").all() and full["p_wilcoxon"].isna().all()
    row = table.loc[(table["configuration"] == "no-potential") & (table["metric"] == "steady_cost_per_hour")]
    assert (row["outcome"] == "higher").all() and np.allclose(row["diff_pct"], 5.0)
    nb = table.loc[(table["configuration"] == "no-eviction-budget") & (table["metric"] == "eviction_cost")]
    assert (nb["family"] == "ablation-eviction-cost").all() and (nb["outcome"] == "higher").all()
    same = table.loc[table["configuration"] == "no-randomness"]
    assert (same["outcome"] == "no significant difference").all() and (same["p_wilcoxon"] == 1.0).all()
    assert not table["p_holm_all"].lt(0.05).any(), "32 tests at n = 10: Holm over all cannot reject"
    assert len(doc["effects"]) == 2 * 4 * 4


def test_partial_data() -> None:
    """Only some arms finished (as in the pilot so far): tables still come out, verdicts say incomplete."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        _session_tree(root, only=("exp1", "abl"), seeds=3)
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
        assert doc["exp1"]["h1"]["verdict"] == "incomplete" and doc["exp1"]["h1"]["n_tests_with_data"] == 0
        assert not (out / "tables" / "exp3_h3.csv").exists()
        table = pd.read_csv(out / "tables" / "exp1_h1.csv")
        assert (table.loc[table["family"] != "reference", "outcome"] == "no data").all()
        abl = pd.read_csv(out / "tables" / "fig8_ablation.csv")
        w2 = abl.loc[(abl["scenario"] == "W2") & (abl["configuration"] != "full")]
        assert (w2["outcome"] == "no data").all() and (w2["n_valid"] == 0).all()
        assert (out / "figures" / "out" / "fig8_ablation.png").exists()


def test_group_map_pilot_layout() -> None:
    """Session analyses run on pilot-named groups through ANALYSIS_GROUP_MAP."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
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
        saved = os.environ.get(common.GROUP_MAP_ENV)
        os.environ[common.GROUP_MAP_ENV] = mapping
        try:
            ce.main(pilot, out)
        finally:
            if saved is None:
                os.environ.pop(common.GROUP_MAP_ENV, None)
            else:
                os.environ[common.GROUP_MAP_ENV] = saved
        h1 = json.loads((out / "tables" / "cost_experiments.json").read_text())["exp1"]["h1"]
        assert h1["n_tests_with_data"] == 6 and h1["verdict"] == "incomplete", h1


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
