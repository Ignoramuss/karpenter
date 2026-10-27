"""Plain-assert tests for paper.analysis.frontier_fidelity, on a synthetic runs tree with known effects. Run from the
repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_frontier_fidelity

``--write-synthetic DIR`` only writes the synthetic tree (``DIR/session/<run_id>/...``), e.g. to point
``paper/make_figures.py --runs DIR`` at it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import math
import os
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
from paper.analysis import frontier_fidelity as ff

SEEDS = list(range(101, 111))
LAMBDAS = [0.05, 0.1, 0.2, 0.5, 1, 2, 5]
TS_HEADER = ("t_min,nodes,ready_nodes,cost_per_hour,pods_running,pods_pending,evictions_cum,eviction_cost_cum,"
             "nodes_removed_cum,cpu_util_mean,mem_util_mean,dom_util_mean,band_share_cpu,band_share_dom,"
             "p1_load_entropy,p1_norm,p2_util_entropy,p2_norm,p3_quadratic,p3_norm,hist_cpu,hist_mem")
OUTPUTS = ["tables/exp6_frontier.csv", "tables/exp6_frontier.md", "tables/exp6_dominance.csv",
           "tables/exp6_dominance.md", "tables/exp5_fidelity.csv", "tables/exp5_fidelity.md", "tables/exp5_tests.csv",
           "tables/exp5_tests.md", "tables/frontier_fidelity.json", "figures/out/fig3_frontier.png",
           "figures/out/fig3_frontier.pdf", "figures/out/fig5_fidelity.png", "figures/out/fig5_fidelity.pdf"]


# --- synthetic runs tree -------------------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def _summary(run_id: str, label: str, arm: str, scenario: str, seed: int, placement: str, scale_down: str,
             **metrics: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "run_id": run_id, "label": label, "arm": arm, "scenario": scenario, "scenario_nodes": 200, "seed": seed,
        "placement": placement, "scale_down": scale_down, "sim_minutes": 180, "integrated_cost": 0.0,
        "integrated_cost_post": 0.0, "steady_cost_per_hour": 27.0, "final_nodes": 20, "peak_nodes": 44,
        "nodes_removed": 30, "evictions": 0, "evictions_by": {"drain": 0}, "eviction_cost": 0.0, "re_disruptions": 0,
        "regret_evictions": 0, "churn_nodes": 0, "evictions_refused_by_pdb": 0, "pdb_violations": 0,
        "do_not_disrupt_violations": 0, "pending_pod_seconds": 0.0, "mean_time_to_schedule_s": 1.0,
        "scale_downs": 0, "jobs_completed": 0, "disruption_passes": 100, "pass_methods": {"none": 100},
        "commands_started": {"delete": 1}, "commands_completed": 1, "commands_failed": 0, "fidelity_hits": 0,
        "fidelity_misses": 0, "fidelity": 0.0, "predicted_delta_p2": 0.0, "realized_delta_p2": 0.0,
        "plans_scored": 0, "full_simulations": 0, "search_timeouts": 0, "events_run": 1000, "invalid": False,
        "timed_out": False, "wall_seconds": 10.0, "wall_pass_mean_ms": 5.0, "wall_pass_p95_ms": 9.0,
        "wall_pass_max_ms": 12.0, "wall_pass_total_s": 0.5, "wall_passes_over_1m": 0, "wall_passes_over_3m": 0,
    }
    s.update(metrics)
    s["integrated_cost"] = s["integrated_cost_post"] + 10.0
    return s


def _write_run(matrix: Path, group: str, label: str, arm: str, scenario: str, seed: int, placement: str,
               scale_down: str, lam: float | None, index: list[dict[str, Any]], abandoned: bool = False,
               **metrics: Any) -> None:
    desc = f"arm-{_slug(label)}_scenario-{scenario.lower()}_seed-{seed}"
    if group.startswith("exp5"):
        desc += f"_placement-{placement}_scale-down-{scale_down}"
    run_id = f"{group}__{desc}__{zlib.crc32(desc.encode()):08x}"
    d = matrix / run_id
    d.mkdir(parents=True)
    arm_cfg: dict[str, Any] = {"name": arm, "label": label}
    if lam is not None or arm == "pipeline":
        arm_cfg["pipeline"] = {"planner": {"name": "entropy-greedy", "params": {"lambda": 0.5 if lam is None else lam}},
                               "candidates": {"name": "uniform-random"}, "moves": [{"name": "random-merge"}],
                               "executor": {"name": "shift-evict"}, "potential": {"name": "util-entropy"}}
    cell = {"arm": arm_cfg, "duration": "3h", "runID": run_id, "scenario": {"name": scenario, "nodes": 200},
            "seed": seed, "placement": placement, "scaleDown": scale_down}
    (d / "cell.json").write_text(json.dumps(cell, indent=2) + "\n", encoding="utf-8")
    if abandoned:
        index.append({"run_id": run_id, "group": group, "status": "abandoned", "error": "wall limit", "attempt": 3})
        return
    manifest = {"run_id": run_id, "git_sha": "0" * 40, "git_dirty": False, "seed": seed, "scenario": scenario,
                "config": {"runID": run_id, "seed": seed, "scenario": {"name": scenario, "nodes": 200}, "arm": arm_cfg,
                           "placement": placement, "scaleDown": scale_down, "duration": "3h0m0s", "warmup": "10m0s"},
                "environment": {"go_version": "go1.26", "os": "linux", "arch": "amd64", "cpus": 4},
                "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:01:00Z"}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    summary = _summary(run_id, label, arm, scenario, seed, placement, scale_down, **metrics)
    (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    hist = ";".join(["2"] * 10)
    rows = [f"{t},40,40,{summary['steady_cost_per_hour']:.3f},400,0,0,0,0,0.5,0.4,0.5,0.3,0.3,1.2,0.8,1.1,0.7,0.2,0.3,"
            f"{hist},{hist}" for t in (0, 1, 2)]
    (d / "timeseries.csv").write_text("\n".join([TS_HEADER, *rows]) + "\n", encoding="utf-8")
    index.append({"run_id": run_id, "group": group, "status": "done", "exit_code": 0, "attempt": 1})


def lam_disruption(lam: float) -> float:
    """Synthetic mean eviction cost of the lambda point: 200 at 0.05, +150 per doubling."""
    return 200 + 150 * math.log2(lam / 0.05)


def lam_savings(lam: float) -> float:
    """Synthetic mean savings vs B0 ($) of the lambda point: concave in disruption."""
    return 20 + 15 * math.sqrt(math.log2(lam / 0.05))


B0_MA_EXTRA = 50.0  # the most-allocated B0 fleet costs this much more ($) than the least-allocated one
B5_SAVINGS = 60.0  # B5's savings against its own (most-allocated) B0


def write_synthetic_tree(root: Path) -> None:
    """A small session-like runs tree under ``root/session`` with known Exp 6 and Exp 5 effects.

    Exp 6 (W2, W3): B0 at about $300 under least-allocated placement (exp6-baselines) and $50 more under
    most-allocated (exp6-b5); the sweep per ``lam_savings``/``lam_disruption``. Against the preregistered lambda
    points: B2 saves $10 at eviction cost 760, so lambda 0.05 (savings 20, eviction cost 200) dominates it; B3
    shadows lambda 0.5 within noise (inconclusive); B5 (most-allocated) saves $60 against its own B0 at eviction cost
    150, so lambda 0.1 (35, 350) is worse on both and B5 dominates it (dominated). W2 lambda 0.5 seed 103 is invalid;
    W3 lambda 1 seed 110 was abandoned; W3 most-allocated B0 seed 104 is invalid, so B5's savings there are
    undefined. Exp 5 (W2): fidelity 0.45 / 0.75 / 0.99 by placement, +0.1 under deletion-cost (capped at 1), -0.03
    for B2, +0.25 for the steered executor; B2 most-allocated deletion-cost seed 110 tracked no evicted pods.
    """
    rng = np.random.default_rng(0)
    matrix = Path(root) / "session"
    matrix.mkdir(parents=True)
    index: list[dict[str, Any]] = []
    for scenario in ("W2", "W3"):
        for seed in SEEDS:
            b0 = 300 + rng.normal(0, 10)
            b0_ma = b0 + B0_MA_EXTRA + rng.normal(0, 1)
            _write_run(matrix, "exp6-baselines", "B0", "B0-none", scenario, seed, "least-allocated", "spread", None,
                       index, integrated_cost_post=b0)
            bad_b0 = scenario == "W3" and seed == 104
            _write_run(matrix, "exp6-b5", "B0", "B0-none", scenario, seed, "most-allocated", "spread", None, index,
                       integrated_cost_post=b0_ma, pdb_violations=1 if bad_b0 else 0, invalid=bad_b0)
            costs: dict[float, tuple[float, float]] = {}
            for lam in LAMBDAS:
                cost = b0 - lam_savings(lam) + rng.normal(0, 0.5)
                ev = lam_disruption(lam) + rng.normal(0, 5)
                costs[lam] = (cost, ev)
                bad = scenario == "W2" and lam == 0.5 and seed == 103
                _write_run(matrix, "exp6", f"random/entropy-greedy/lambda={lam:g}", "pipeline", scenario, seed,
                           "least-allocated", "spread", lam, index,
                           abandoned=scenario == "W3" and lam == 1 and seed == 110,
                           integrated_cost_post=cost, eviction_cost=ev, evictions=int(round(ev)),
                           invalid=bad, invalid_reasons=["pdb or do-not-disrupt violation"] if bad else [],
                           pdb_violations=1 if bad else 0)
            sign = 1 if seed % 2 else -1
            cost3, ev3 = costs[0.5]
            for label, arm, cost, ev, group, placement in (
                    ("B2", "B2-when-empty-or-underutilized", b0 - 10 + rng.normal(0, 0.5), 760 + rng.normal(0, 5),
                     "exp6-baselines", "least-allocated"),
                    ("B3", "B3-balanced", cost3 + 0.3 * sign, ev3 + 3 * sign + 0.5, "exp6-baselines",
                     "least-allocated"),
                    ("B5", "B5-descheduler", b0_ma - B5_SAVINGS + rng.normal(0, 0.5), 150 + rng.normal(0, 5),
                     "exp6-b5", "most-allocated")):
                _write_run(matrix, group, label, arm, scenario, seed, placement, "spread", None, index,
                           integrated_cost_post=cost, eviction_cost=ev, evictions=int(round(ev)))
    base = {"least-allocated": 0.45, "most-allocated": 0.75, "plan-following": 0.99}
    arms = [("exp5", "random/entropy-greedy", "pipeline", 0.0, list(base)),
            ("exp5", "B2", "B2-when-empty-or-underutilized", -0.03, list(base)),
            ("exp5-steered", "random/entropy-greedy/steered", "pipeline", 0.25, ["least-allocated"])]
    for group, label, arm, shift, placements in arms:
        for placement in placements:
            for scale_down in ("spread", "deletion-cost"):
                for seed in SEEDS:
                    f = base[placement] + shift + (0.1 if scale_down == "deletion-cost" else 0.0)
                    f = float(np.clip(f + rng.normal(0, 0.02), 0, 1))
                    tracked = 0 if (label == "B2" and placement == "most-allocated" and scale_down == "deletion-cost"
                                    and seed == 110) else 400 + seed
                    hits = int(round(f * tracked))
                    pred = -80 + rng.normal(0, 5)
                    _write_run(matrix, group, label, arm, "W2", seed, placement, scale_down,
                               0.5 if arm == "pipeline" else None, index,
                               fidelity_hits=hits, fidelity_misses=tracked - hits,
                               fidelity=hits / tracked if tracked else 0.0, predicted_delta_p2=pred,
                               realized_delta_p2=pred * (0.3 + 0.7 * f), plans_scored=20,
                               steady_cost_per_hour=27 + rng.normal(0, 0.3), evictions=1000 + seed,
                               eviction_cost=1000.0 + seed)
    with open(matrix / "index.jsonl", "w", encoding="utf-8") as fh:
        for entry in index:
            fh.write(json.dumps(entry) + "\n")


@contextlib.contextmanager
def _synthetic() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory() as tmp:
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


# --- tests ---------------------------------------------------------------------------------------------------------


def test_pareto_flags() -> None:
    flags = ff.pareto_flags([0, 10, 20, 15, float("nan")], [0, 100, 200, 250, 50])
    assert flags == [True, True, True, False, None], flags
    assert ff.pareto_flags([5, 5], [1, 1]) == [True, True], "identical points do not dominate each other"


def test_h6_fixed_lambda_mapping() -> None:
    # H6's matched lambda per baseline is fixed in advance (B2: 0.05, B3: 0.5, B5: 0.1), never chosen from results.
    exp = common.experiment("exp6")
    comps = common.comparisons("exp6")
    assert ff._check_matched(exp, comps) == {"B2": "random/entropy-greedy/lambda=0.05",
                                             "B3": "random/entropy-greedy/lambda=0.5",
                                             "B5": "random/entropy-greedy/lambda=0.1"}
    bad = [dataclasses.replace(c, a="random/entropy-greedy/lambda=2") if c.b == "B3" else c for c in comps]
    try:
        ff._check_matched(exp, bad)
    except ValueError as err:
        assert "B3" in str(err)
    else:
        raise AssertionError("a pairing other than the registry's must be refused")
    assert not hasattr(ff, "select_matched"), "no data-dependent selection of the matched point"


def test_helpers() -> None:
    assert ff.rank_direction([1, 2, -0.5]) == 1 and ff.rank_direction([-3, 1]) == -1 and ff.rank_direction([0]) == 0
    est, lo, hi = ff.ratio_of_sums_ci([1, 2, 3], [2, 4, 6])
    assert est == lo == hi == 0.5
    assert all(math.isnan(v) for v in ff.ratio_of_sums_ci([], []))
    assert ff._wrap_label("random/entropy-greedy/steered", 14) == "random/\nentropy-greedy/\nsteered"


def test_savings_pair_by_placement() -> None:
    # B5 runs under most-allocated placement and pairs with the most-allocated B0; everything else with the
    # least-allocated B0 on the same scenario, nodes and seed.
    rows = []
    for seed in (1, 2):
        for label, placement, cost in (("B0", "least-allocated", 100.0), ("B0", "most-allocated", 150.0),
                                       ("B5", "most-allocated", 90.0), ("B2", "least-allocated", 80.0)):
            rows.append({"label": label, "placement": placement, "scenario": "W2", "nodes": 200, "seed": seed,
                         "valid": not (label == "B0" and placement == "most-allocated" and seed == 2),
                         "integrated_cost_post": cost})
    df = ff.add_savings(pd.DataFrame(rows), ["scenario", "nodes", "seed", "placement"], "B0", "integrated_cost_post")
    s = df.set_index(["label", "seed"])["savings"]
    assert s[("B5", 1)] == 60.0 and s[("B2", 1)] == 20.0, s
    assert math.isnan(s[("B5", 2)]) and bool(df.set_index(["label", "seed"]).loc[("B5", 2), "b0_invalid"])


def test_cell_verdicts() -> None:
    v = ff.cell_verdict
    assert v({"savings": "better", "eviction_cost": "no difference"}) == "dominates"
    assert v({"savings": "better", "eviction_cost": "better"}) == "dominates"
    assert v({"savings": "no difference", "eviction_cost": "worse"}) == "dominated"
    assert v({"savings": "worse", "eviction_cost": "worse"}) == "dominated"
    assert v({"savings": "better", "eviction_cost": "worse"}) == "inconclusive"
    assert v({"savings": "no difference", "eviction_cost": "no difference"}) == "inconclusive"
    # "not significantly worse" resting on an unreachable test is no support.
    assert v({"savings": "better", "eviction_cost": "inconclusive"}) == "inconclusive"
    # An unreachable test is never significantly better, so a significantly worse metric is dominated regardless.
    assert v({"savings": "worse", "eviction_cost": "inconclusive"}) == "dominated"
    row = {"outcome": "higher"}
    assert ff.cell_result(row, "higher") == "better" and ff.cell_result(row, "lower") == "worse"
    assert ff.cell_result({"outcome": "inconclusive"}, "higher") == "inconclusive"
    assert ff.cell_result({"outcome": "no significant difference"}, "lower") == "no difference"


def test_h6_verdict_rule() -> None:
    # Supported iff no cell is dominated and at least 3 of 6 dominate; not supported if any is dominated; otherwise
    # inconclusive; "incomplete" while configurations are missing.
    exp = common.experiment("exp6")
    frontier = pd.DataFrame(columns=ff.FRONTIER_COLUMNS)

    def head(verdicts: list[str], missing: list | None = None) -> dict:
        rows = []
        for (s, b), verdict in zip([(s, b) for s in ("W2", "W3") for b in ("B2", "B3", "B5")], verdicts):
            for metric in ("savings", "eviction_cost"):
                rows.append({c: math.nan for c in ff.DOMINANCE_COLUMNS} | {
                    "scenario": s, "nodes": 200, "baseline": b, "matched_label": "x", "matched_lambda": 0.1,
                    "metric": metric, "verdict": verdict, "family": "H6"})
        return ff.exp6_headline(pd.DataFrame(columns=common.RUN_COLUMNS), frontier, pd.DataFrame(rows), exp,
                                missing or [])

    assert head(["dominates"] * 3 + ["inconclusive"] * 3)["verdict"] == "supported"
    assert head(["dominates"] * 2 + ["inconclusive"] * 4)["verdict"] == "inconclusive"
    assert head(["dominates"] * 5 + ["dominated"])["verdict"] == "not supported"
    h = head(["dominates"] * 6, missing=[{"arm": "B5"}])
    assert h["verdict"] == "incomplete" and h["verdict_on_available"] == "supported" and not h["complete"]


def test_synthetic_end_to_end() -> None:
    with _synthetic() as (runs, out):
        _, log = _quiet(lambda: ff.main(runs, out))
        for rel in OUTPUTS:
            assert (out / rel).is_file(), f"missing {rel}"
        frontier = pd.read_csv(out / "tables/exp6_frontier.csv")
        w2 = frontier.loc[frontier["scenario"] == "W2"]
        assert list(zip(w2["label"], w2["placement"])) == [
            ("B0", "least-allocated"), ("B0", "most-allocated"),
            *[(f"random/entropy-greedy/lambda={x:g}", "least-allocated") for x in LAMBDAS],
            ("B2", "least-allocated"), ("B3", "least-allocated"), ("B5", "most-allocated")]
        w2 = w2.set_index("label")
        assert (w2.loc["B0", "savings_mean"] == 0).all() and (w2.loc["B0", "n_used"] == 10).all()
        lam05 = w2.loc["random/entropy-greedy/lambda=0.5"]
        assert lam05["n_invalid"] == 1 and lam05["n_used"] == 9 and lam05["lambda"] == 0.5
        assert abs(lam05["savings_mean"] - lam_savings(0.5)) < 1.0
        assert lam05["savings_mean_lo"] < lam05["savings_mean"] < lam05["savings_mean_hi"]
        # B5's savings are against the most-allocated B0 (they would be about $50 more against least-allocated).
        assert abs(w2.loc["B5", "savings_mean"] - B5_SAVINGS) < 1.0, w2.loc["B5"]
        w3 = frontier.loc[frontier["scenario"] == "W3"].set_index(["label", "placement"])
        assert w3.loc[("random/entropy-greedy/lambda=1", "least-allocated"), "n_runs"] == 10, "abandoned counted"
        assert w3.loc[("random/entropy-greedy/lambda=1", "least-allocated"), "n_invalid"] == 1
        assert w3.loc[("B5", "most-allocated"), "n_used"] == 9 and w3.loc[("B5", "most-allocated"), "n_excluded"] == 1
        assert w3.loc[("B0", "most-allocated"), "n_invalid"] == 1
        assert bool(frontier.loc[(frontier["label"] == "B0") & (frontier["scenario"] == "W2")]["pareto"].iloc[0])

        dom = pd.read_csv(out / "tables/exp6_dominance.csv")
        assert len(dom) == 12 and set(dom["family"]) == {"H6"} and (dom["m"] == 12).all(), dom
        assert set(dom["metric"]) == {"savings", "eviction_cost"}
        verdicts = dom.groupby(["scenario", "baseline"])["verdict"].first().to_dict()
        assert verdicts == {(s, b): v for s in ("W2", "W3") for b, v in
                            (("B2", "dominates"), ("B3", "inconclusive"), ("B5", "dominated"))}, verdicts
        matched = dom.groupby("baseline")["matched_lambda"].unique().to_dict()
        assert {b: list(v) for b, v in matched.items()} == {"B2": [0.05], "B3": [0.5], "B5": [0.1]}, matched
        r = dom.loc[(dom["scenario"] == "W2") & (dom["baseline"] == "B2") & (dom["metric"] == "savings")].iloc[0]
        assert r["n_pairs"] == 10 and abs(r["mean_diff"] - (lam_savings(0.05) - 10)) < 1.0 and r["result"] == "better"
        assert r["p_holm"] >= r["p_wilcoxon"] and abs(r["p_floor"] - 2 / 1024) < 1e-8 and bool(r["holm_reachable"])
        r = dom.loc[(dom["scenario"] == "W2") & (dom["baseline"] == "B3") & (dom["metric"] == "savings")].iloc[0]
        assert r["n_pairs"] == 9 and r["n_dropped_invalid"] == 1 and r["result"] == "no difference", r
        r = dom.loc[(dom["scenario"] == "W3") & (dom["baseline"] == "B5") & (dom["metric"] == "savings")].iloc[0]
        assert r["n_pairs"] == 9 and r["n_undefined"] == 1 and r["n_b0_invalid"] == 1 and r["result"] == "worse", r
        b5 = dom.loc[dom["baseline"] == "B5"]
        assert set(b5["result"]) == {"worse"}

        cells = pd.read_csv(out / "tables/exp5_fidelity.csv")
        assert len(cells) == 2 * 3 * 2 + 2, len(cells)
        c = cells.loc[(cells["label"] == "B2") & (cells["placement"] == "most-allocated")
                      & (cells["scale_down"] == "deletion-cost")].iloc[0]
        assert c["n_valid"] == 10 and c["n_fidelity"] == 9, "a run with no tracked evictions has no fidelity"
        least = cells.loc[(cells["label"] == "random/entropy-greedy") & (cells["placement"] == "least-allocated")
                          & (cells["scale_down"] == "spread")].iloc[0]
        assert abs(least["fidelity_mean"] - 0.45) < 0.03
        assert abs(least["off_plan_share_mean"] + least["fidelity_mean"] - 1) < 1e-12
        assert abs(least["delta_p2_ratio_mean"] - (0.3 + 0.7 * least["fidelity_mean"])) < 0.02
        assert least["n_ratio"] == 10 and least["delta_p2_ratio_of_sums_lo"] <= least["delta_p2_ratio_of_sums_hi"]

        tests = pd.read_csv(out / "tables/exp5_tests.csv")
        assert len(tests) == 10 and set(tests["family"]) == {"H5"} and (tests["m"] == 10).all(), len(tests)
        assert "p_tost" not in tests.columns and "plan-following" not in set(tests["placement"].dropna())
        assert set(tests["outcome"]) == {"higher"}, tests[["comparison", "stratum", "outcome"]]
        undefined = tests.loc[tests["n_undefined"] > 0, ["comparison", "stratum"]].values.tolist()
        assert undefined == [["most- vs least-allocated, B2", "scenario=W2, nodes=200, scale_down=deletion-cost"],
                             ["deletion-cost vs spread, B2", "scenario=W2, nodes=200, placement=most-allocated"]], \
            undefined

        doc = json.loads((out / "tables/frontier_fidelity.json").read_text(encoding="utf-8"))
        e6 = doc["exp6"]
        assert e6["verdict"] == "not supported" and e6["complete"], e6["verdict"]
        assert e6["verdict_counts"] == {"dominated": 2, "dominates": 2, "inconclusive": 2}
        assert e6["n_cells"] == 6 and "B0 (least-allocated)" in e6["pareto"]["W2/200"]
        h5 = doc["exp5"]["h5"]
        assert h5["verdict"] == "supported" and h5["off_plan_holds"] and h5["complete"], h5
        assert {k: v["state"] for k, v in h5["levers"].items()} == {"most-allocated": "holds",
                                                                    "deletion-cost": "holds"}
        assert h5["oracle"]["ok"] and h5["oracle"]["n_cells"] == 4 and h5["oracle"]["flagged_cells"] == []
        validity = {v["group"]: v for v in e6["validity"]}
        assert validity["exp6"]["n_invalid"] == 2 and validity["exp6"]["n_abandoned"] == 1
        assert validity["exp6"]["n_violation"] == 1 and validity["exp6-b5"]["n_violation"] == 1

        root = str(runs.parent)
        for rel in OUTPUTS:
            data = (out / rel).read_bytes()
            assert root.encode() not in data, f"{rel} embeds a temporary path"
        assert "skipped" not in log, log


def test_h5_partial_and_oracle_flag() -> None:
    # One lever only (deletion-cost has no effect) is "partial"; a plan-following cell below 0.95 is flagged.
    exp = common.experiment("exp5")
    cells = pd.DataFrame([
        {"label": "random/entropy-greedy", "placement": "least-allocated", "scale_down": sd, "n_fidelity": 10,
         "off_plan_share_mean": 0.5, "off_plan_share_mean_lo": 0.4, "fidelity_mean": 0.5} for sd in ("spread",
                                                                                                    "deletion-cost")]
        + [{"label": "B2", "placement": "plan-following", "scale_down": "spread", "n_fidelity": 10,
            "off_plan_share_mean": 0.1, "off_plan_share_mean_lo": 0.08, "fidelity_mean": 0.9}])
    tests = pd.DataFrame([
        {"comparison": "most- vs least-allocated, random/entropy-greedy", "outcome": "higher"},
        {"comparison": "most- vs least-allocated, random/entropy-greedy", "outcome": "no significant difference"},
        {"comparison": "deletion-cost vs spread, random/entropy-greedy", "outcome": "no significant difference"},
        {"comparison": "deletion-cost vs spread, random/entropy-greedy", "outcome": "no significant difference"}])
    h5 = ff.h5_verdict(cells, tests, exp, [])
    assert h5["verdict"] == "partial" and h5["levers"]["deletion-cost"]["state"] == "fails", h5
    assert not h5["oracle"]["ok"] and len(h5["oracle"]["flagged_cells"]) == 1
    tests.loc[2, "outcome"] = "inconclusive"
    tests.loc[0, "outcome"] = "no significant difference"
    assert ff.h5_verdict(cells, tests, exp, [])["verdict"] == "inconclusive"
    cells.loc[1, "off_plan_share_mean_lo"] = 0.01  # (a) fails under deletion-cost
    assert ff.h5_verdict(cells, tests, exp, [])["verdict"] == "not supported"


def test_reproducible() -> None:
    with _synthetic() as (runs, out):
        out2 = out.parent / "out2"
        _quiet(lambda: ff.main(runs, out))
        _quiet(lambda: ff.main(runs, out2))
        for rel in OUTPUTS:
            assert (out / rel).read_bytes() == (out2 / rel).read_bytes(), f"{rel} differs between runs"


def test_skips_without_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _, log = _quiet(lambda: ff.main(Path(tmp) / "missing", Path(tmp) / "out"))
        assert not (Path(tmp) / "out").exists(), "nothing is written without runs"
        assert "skipped exp6: no runs" in log and "skipped exp5: no runs" in log, log


def test_pilot_skips() -> None:
    pilot = common.REPO_ROOT / "experiments" / "runs"
    if not pilot.is_dir():
        return
    old = os.environ.get(common.GROUP_MAP_ENV)
    try:
        for mapping in ("", json.dumps({"exp6": "pilot-calibrate", "exp6-baselines": "pilot-baselines"})):
            os.environ[common.GROUP_MAP_ENV] = mapping
            with tempfile.TemporaryDirectory() as tmp:
                _, log = _quiet(lambda: ff.main(pilot, Path(tmp)))
                written = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file())
                assert "skipped exp5: no runs" in log, log
                assert not any("exp5" in w or "fig5" in w for w in written), written
                assert "skipped exp6" in log, log  # no runs, or no B0 to compute savings against
    finally:
        if old is None:
            os.environ.pop(common.GROUP_MAP_ENV, None)
        else:
            os.environ[common.GROUP_MAP_ENV] = old


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--write-synthetic"] and len(argv) == 2:
        write_synthetic_tree(Path(argv[1]))
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
    print(f"{len(tests) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
