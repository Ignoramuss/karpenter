"""Plain-assert tests for paper.analysis.timeseries_figures, on a synthetic runs tree with known effects. Run from the
repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_timeseries_figures

In the synthetic runs the P2 and P3 potentials drop by a step at minutes 28 + 20k and the matching node removals
(and cost drops) follow 5 minutes later, so with any of the 5, 10 and 20 minute windows a drop always lands in the
window before its removals; P1 is noise unrelated to removals. The tests check that Exp 2 recovers that (P2 and P3
support H2, P1 does not, the lead-lag peaks at +5 minutes), that the selection drops B0, invalid runs and duplicate
configurations, and that outputs are byte-reproducible and nothing is written without runs.
"""

from __future__ import annotations

import contextlib
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
from paper.analysis import timeseries_figures as tf

SEEDS = [101, 102, 103, 104, 105, 106]
DURATION = 180
SLOTS = [28 + 20 * k for k in range(8)]  # potential drops; removals follow at +LEAD
LEAD = 5
TS_HEADER = ("t_min,nodes,ready_nodes,cost_per_hour,pods_running,pods_pending,evictions_cum,eviction_cost_cum,"
             "nodes_removed_cum,cpu_util_mean,mem_util_mean,dom_util_mean,band_share_cpu,band_share_dom,"
             "p1_load_entropy,p1_norm,p2_util_entropy,p2_norm,p3_quadratic,p3_norm,hist_cpu,hist_mem")
OUTPUTS = ["tables/exp2_correlation.csv", "tables/exp2_correlation.md", "tables/exp2_potential_comparison.csv",
           "tables/exp2_potential_comparison.md", "tables/exp2_leadlag.csv", "tables/exp2_leadlag.md",
           "tables/timeseries_figures.json", "figures/out/fig1_utilization_histograms.png",
           "figures/out/fig1_utilization_histograms.pdf", "figures/out/fig2_potential_nodes_cost.png",
           "figures/out/fig2_potential_nodes_cost.pdf", "figures/out/fig2_potential_nodes_cost_w1.png",
           "figures/out/fig2_potential_nodes_cost_w1.pdf", "figures/out/fig6_leadlag.png",
           "figures/out/fig6_leadlag.pdf"]

# --- synthetic tree ---------------------------------------------------------------------------------------------------


def _arm(label: str) -> dict[str, Any]:
    """The arm config of a label; the lambda=0.5 sweep point is the same pipeline as random/entropy-greedy."""
    if label.startswith("B"):
        return {"name": f"{label}-baseline", "label": label}
    planner = "anneal-plan" if "anneal" in label else "entropy-greedy"
    lam = float(label.rsplit("=", 1)[1]) if "lambda=" in label else 0.5
    candidates = "upstream" if label.startswith("heuristic") else "uniform-random"
    pipeline: dict[str, Any] = {"candidates": {"name": candidates}, "planner": {"name": planner,
                                                                                 "params": {"lambda": lam}}}
    if label.startswith("merge-only"):
        pipeline["budgets"] = {"podEvictionsPerMinute": 10}
    return {"name": "pipeline", "label": label, "pipeline": pipeline}


def _series(rng: np.random.Generator, scenario: str, nodes0: int, events: bool) -> str:
    """A timeseries.csv: step drops of P2/P3 at chosen slots, removals LEAD minutes later; P1 is noise."""
    t = np.arange(DURATION + 1)
    chosen = sorted(rng.choice(SLOTS, size=int(rng.integers(4, 8)), replace=False).tolist()) if events else []
    removed = np.zeros(t.size, dtype=int)
    p2 = 0.3 + rng.normal(0, 0.0005, t.size)
    p3 = 0.07 + rng.normal(0, 0.0002, t.size)
    for d in chosen:
        k = int(rng.integers(1, 4))
        removed[d + LEAD:] += k
        p2[d:] -= 0.02 * k
        p3[d:] -= 0.005 * k
    p1 = 0.95 + np.cumsum(rng.normal(0, 0.002, t.size))
    nodes = np.maximum(nodes0 - removed, 1)
    empty_start = scenario != "W1"
    if empty_start:
        nodes[0] = 0
    frac = np.clip((t - 10) / 120, 0, 1) * 0.6 + 0.2  # share of nodes in the top bin grows over the run
    lines = [TS_HEADER]
    for i in t:
        n = int(nodes[i])
        top = int(round(frac[i] * n))
        hist = [0] * 10
        hist[9], hist[4] = top, n - top
        band = (n - top) / n if n else 0.0
        h = ";".join(str(v) for v in hist)
        pz = 0.0 if n == 0 else None
        lines.append(
            f"{i:.2f},{n},{n},{0.5 * n:.6f},{10 * n},0,{3 * removed[i]},{3.0 * removed[i]:.6f},{removed[i]},"
            f"0.6,0.5,0.6,{band:.6f},{band:.6f},4.0,{pz if pz is not None else p1[i]:.6f},10.0,"
            f"{pz if pz is not None else p2[i]:.6f},1.0,{pz if pz is not None else p3[i]:.6f},{h},{h}")
    return "\n".join(lines) + "\n"


def _write_run(matrix: Path, group: str, label: str, scenario: str, seed: int, nodes: int = 200,
               invalid: bool = False, events: bool = True, max_wall: str = "0s") -> str:
    run_id = f"{group}__{label.replace('/', '-')}_{scenario.lower()}_seed-{seed}__{zlib.crc32(label.encode()):08x}"
    d = matrix / run_id
    d.mkdir(parents=True)
    arm = _arm(label)
    cfg = {"runID": run_id, "seed": seed, "scenario": {"name": scenario, "nodes": nodes}, "arm": arm,
           "duration": "3h0m0s", "warmup": "10m0s", "maxWall": max_wall}
    (d / "cell.json").write_text(json.dumps({"runID": run_id, "seed": seed, "arm": arm,
                                             "scenario": {"name": scenario, "nodes": nodes}}))
    (d / "manifest.json").write_text(json.dumps({"run_id": run_id, "config": cfg, "seed": seed}))
    summary = {"run_id": run_id, "label": label, "arm": arm["name"], "scenario": scenario, "scenario_nodes": nodes,
               "seed": seed, "placement": "least-allocated", "scale_down": "spread", "sim_minutes": DURATION,
               "invalid": invalid, "invalid_reasons": ["pdb violation"] if invalid else [], "timed_out": False}
    (d / "summary.json").write_text(json.dumps(summary))
    rng = np.random.default_rng(zlib.crc32(run_id.encode()))
    (d / "timeseries.csv").write_text(_series(rng, scenario, nodes, events))
    return run_id


def write_synthetic_tree(root: Path) -> None:
    """``root/session/<run_id>/``: Exp 1 W1/W2 arms and baselines at 200 nodes, a 1,000-node W2 arm, Exp 6 on W2
    and W3 (its W2 lambda=0.5 runs duplicate Exp 1's random/entropy-greedy), B0 and B2 in exp6-baselines (B2 on W3
    with no removals at all, so its correlations are undefined), and one invalid Exp 3 run."""
    m = root / "session"
    for seed in SEEDS:
        for scenario in ("W1", "W2"):
            for label in ("random/entropy-greedy", "random/anneal-plan", "heuristic/entropy-greedy"):
                _write_run(m, "exp1-n200", label, scenario, seed)
            for label in ("B2", "B3"):
                _write_run(m, "exp1-baselines-n200", label, scenario, seed)
        _write_run(m, "exp1-n1000", "random/entropy-greedy", "W2", seed, nodes=1000)
        for scenario in ("W2", "W3"):
            _write_run(m, "exp6", "random/entropy-greedy/lambda=0.5", scenario, seed, max_wall="100m0s")
            _write_run(m, "exp6", "random/entropy-greedy/lambda=2", scenario, seed)
            _write_run(m, "exp6-baselines", "B0", scenario, seed, events=False)
        _write_run(m, "exp6-baselines", "B2", "W3", seed, events=False)
    _write_run(m, "exp3", "merge-only/budget-lo", "W2", 101, invalid=True)


@contextlib.contextmanager
def _synthetic() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_synthetic_tree(root / "runs")
        yield root / "runs", root / "out"


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


def _quiet(fn: Callable[[], Any]) -> tuple[Any, str]:
    buf = io.StringIO()
    handler = common.logger.handlers[0]
    old = handler.stream
    handler.setStream(buf)
    try:
        return fn(), buf.getvalue()
    finally:
        handler.setStream(old)


def _run_main(runs: Path, out: Path) -> str:
    common.apply_style()
    with _env(**{common.GROUP_MAP_ENV: None}):
        return _quiet(lambda: tf.main(runs, out))[1]


# --- unit tests -------------------------------------------------------------------------------------------------------


def test_window_samples() -> None:
    minutes = np.arange(0, 41)
    pm = pd.DataFrame({"t_min": minutes.astype(float), "nodes_removed_cum": (minutes >= 25).astype(int) * 2,
                       "cost_per_hour": np.where(minutes >= 25, 8.0, 10.0), "p1_norm": 0.5,
                       "p2_norm": np.where(minutes >= 18, 0.2, 0.4), "p3_norm": minutes * 0.01})
    pm = tf.per_minute(pm)
    s = tf.window_samples(pm, 10)
    assert s["t"].tolist() == [20, 30], s
    assert np.allclose(s["drop_P2"], [0.2, 0.0]) and np.allclose(s["drop_P1"], [0.0, 0.0])
    assert np.allclose(s["drop_P3"], [-0.1, -0.1])
    assert s["removals"].tolist() == [2, 0] and np.allclose(s["cost_decrease"], [2.0, 0.0])
    assert tf.window_samples(pm, 5)["t"].tolist() == [15, 20, 25, 30, 35]
    # A missing minute makes its samples NaN rather than shifting the windows.
    gap = tf.per_minute(pm.reset_index(drop=True).drop(index=30))
    assert len(gap) == 41 and math.isnan(gap.loc[30, "p2_norm"])
    s = tf.window_samples(gap, 10)
    assert math.isnan(s.loc[0, "removals"]) and math.isnan(s.loc[1, "drop_P2"]) and s.loc[0, "drop_P2"] == 0.2


def test_spearman_and_sign() -> None:
    assert math.isnan(tf.spearman([1, 2, 3, 4], [0, 0, 0, 0])), "a constant series has no correlation"
    assert math.isnan(tf.spearman([1, 2, 3], [1, 2, 3])), f"needs {tf.MIN_WINDOWS} windows"
    assert abs(tf.spearman([1, 2, 3, 4], [2, 4, 6, 9]) - 1.0) < 1e-12
    s = tf.sign_test([0.5, 0.2, -0.1, 0.0, float("nan"), 0.3])
    assert (s["n_positive"], s["n_negative"], s["n_zero"]) == (3, 1, 1)
    assert abs(s["p_sign"] - 5 / 16) < 1e-12, s  # P(X >= 3), X ~ Bin(4, 1/2)
    assert math.isnan(tf.sign_test([0.0, float("nan")])["p_sign"])
    assert tf.signed_rank_direction([1, 2, -0.5]) == 1 and tf.signed_rank_direction([-3, 1]) == -1
    assert tf.signed_rank_direction([0, 0]) == 0


def test_cross_correlation() -> None:
    rng = np.random.default_rng(0)
    u = rng.normal(size=200)
    v = np.concatenate([np.zeros(7), u[:-7]])  # v(t) = u(t - 7): u leads by 7
    cc = tf.cross_correlation(u, v)
    assert tf.LAGS[int(np.nanargmax(cc))] == 7 and cc[tf.LAGS.index(7)] > 0.99
    assert math.isnan(tf.cross_correlation(u, np.ones(200))[0])
    assert np.isnan(tf.cross_correlation(u[:5], v[:5])).all(), "too few overlapping minutes"


def test_fingerprint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp)
        a = _write_run(m, "exp1-n200", "random/entropy-greedy", "W2", 101)
        b = _write_run(m, "exp6", "random/entropy-greedy/lambda=0.5", "W2", 101, max_wall="100m0s")
        c = _write_run(m, "exp6", "random/entropy-greedy/lambda=2", "W2", 101)
        d = _write_run(m, "exp6", "random/entropy-greedy/lambda=0.5", "W2", 102)
        fa, fb, fc, fd = (tf.config_fingerprint(m / r) for r in (a, b, c, d))
        assert fa == fb, "label, runID and maxWall do not change the simulation"
        assert fa != fc and fa != fd
        assert tf.config_fingerprint(m / "missing") is None


# --- end to end -------------------------------------------------------------------------------------------------------


def test_synthetic_end_to_end() -> None:
    with _synthetic() as (runs_root, out):
        log = _run_main(runs_root, out)
        for rel in OUTPUTS:
            assert (out / rel).exists(), f"missing {rel}\n{log}"
        doc = json.loads((out / "tables" / "timeseries_figures.json").read_text())
        sel = doc["exp2"]["selection"]
        n_seeds = len(SEEDS)
        assert sel["n_excluded_label"] == 2 * n_seeds, sel  # B0 on W2 and W3
        assert sel["n_invalid"] == 1 and sel["n_duplicate_config"] == n_seeds, sel
        assert all("lambda=0.5" in d["run_id"] and "_w2_" in d["run_id"] for d in sel["duplicates"])
        # Exp 1 W2 (3 pipeline labels; its baselines group is not in Exp 2) + 1,000 nodes + Exp 6 (W2 lambda=2, W3
        # both points) + B2 on W3.
        assert sel["n_used"] == n_seeds * (3 + 1 + 3 + 1), sel

        corr = pd.read_csv(out / "tables" / "exp2_correlation.csv")
        prim = corr.loc[corr["role"] == "primary"].set_index(["potential", "scenario"])
        assert len(prim) == 6 and set(prim["family"]) == {"H2/removals/w10"}
        for p in ("P2", "P3"):
            for scenario in ("W2", "W3"):
                r = prim.loc[(p, scenario)]
                assert r["median_rho"] > 0.5 and bool(r["supports_h2"]), r
        assert not prim.loc[("P1", "W2"), "supports_h2"]
        # B2 on W3 removes nothing: its correlations are undefined and counted, not dropped silently.
        assert prim.loc[("P2", "W3"), "n_undefined"] == n_seeds and prim.loc[("P2", "W3"), "n_runs"] == 2 * n_seeds
        assert prim.loc[("P2", "W2"), "nodes"] == "200, 1000"
        assert set(corr["window_min"]) == {5, 10, 20} and set(corr["outcome"]) == {"removals", "cost_decrease"}
        cost = corr.loc[(corr["role"] == "secondary") & (corr["potential"] == "P2")]
        assert (cost["median_rho"] > 0.5).all()
        verdict = doc["exp2"]["verdicts"]["H2/removals/w10"]
        assert verdict["P2"]["verdict"] == "holds" and verdict["P1"]["verdict"] == "not supported", verdict

        comp = pd.read_csv(out / "tables" / "exp2_potential_comparison.csv")
        row = comp.loc[(comp["role"] == "primary") & (comp["scenario"] == "W2") & (comp["a"] == "P1")
                       & (comp["b"] == "P2")].iloc[0]
        assert row["favours"] == "P2" and bool(row["significant"]) and row["median_diff"] < 0, row
        assert set(comp["family"]) == {f"H2-compare/{o}/w{w}" for o in tf.OUTCOMES for w in tf.WINDOWS}

        ll = doc["exp2"]["leadlag"]
        for scenario in ("W2", "W3"):
            # Drops recur every 20 minutes, so echoes of the +5 peak sit at -15 and +25: the lead mass is 2/3.
            assert ll[scenario]["P2"]["peak_lag_min"] == LEAD and ll[scenario]["P2"]["lead_share"] > 0.6, ll
            assert ll[scenario]["P2"]["peak_median"] > 0.9 > 0.2 > ll[scenario]["P1"]["peak_median"], ll
        leadlag = pd.read_csv(out / "tables" / "exp2_leadlag.csv")
        assert len(leadlag) == 2 * 3 * len(tf.LAGS)

        fig1 = doc["fig1_utilization_histograms"]
        assert fig1["arms"] == list(tf.FIG1_ARMS) and fig1["scenarios"] == ["W1", "W2"]
        b2 = fig1["cells"]["W1"]["B2"]
        assert b2["n_used"] == n_seeds and b2["band_share_cpu_late"]["mean"] < b2["band_share_cpu_at_warmup_end"]["mean"]
        assert abs(b2["packed_share_at_warmup_end"]["mean"] - 0.2) < 0.02 and b2["packed_share_late"]["mean"] > 0.75
        assert b2["low_share_late"]["mean"] == 0.0
        fig2 = doc["fig2_potential_nodes_cost"]
        assert list(fig2["arms"]) == sorted(tf.FIG2_ARMS) and fig2["scenario"] == "W2"
        assert all(v["n_used"] == n_seeds for v in fig2["arms"].values())
        text = "".join((out / rel).read_text() for rel in OUTPUTS if rel.startswith("tables/"))
        assert str(runs_root) not in text and "/tmp" not in text, "absolute paths must not reach outputs"


def test_shares_and_bands() -> None:
    with _synthetic() as (runs_root, _):
        runs = common.load_runs(runs_root)
        rows = tf.figure_runs(runs, ["W2"], ["B2"])
        series, missing = tf.load_series(rows)
        assert not missing and len(series) == len(SEEDS)
        ids = sorted(series)
        minutes, shares = tf.hist_shares(series, ids)
        assert shares.shape == (len(ids), DURATION + 1, 10)
        assert np.isnan(shares[:, 0, :]).all(), "minute 0 of W2 has no node"
        assert np.allclose(np.nansum(shares[:, 1:, :], axis=-1), 1.0)
        minutes, mat = tf.stack(series, ids, "nodes")
        assert np.isnan(mat[:, 0]).all() and np.isfinite(mat[:, 1:]).all(), "nothing is plotted before the first node"
        est, lo, hi = tf.mean_band(mat)
        j = 50
        assert (est[j], lo[j], hi[j]) == common.bootstrap_ci(mat[:, j]), "bands are the shared bootstrap"
        assert np.all(lo[1:] <= est[1:]) and np.all(est[1:] <= hi[1:])


def test_reproducible() -> None:
    digests = []
    with _synthetic() as (runs_root, out):
        for i in range(2):
            target = out / str(i)
            _run_main(runs_root, target)
            digests.append({rel: (target / rel).read_bytes() for rel in OUTPUTS})
    assert digests[0] == digests[1], [k for k in OUTPUTS if digests[0][k] != digests[1][k]]


def test_skips_without_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        log = _run_main(Path(tmp) / "none", out)
        assert not out.exists(), "nothing is written without runs"
        for name in ("exp2", "fig1_utilization_histograms", "fig2_potential_nodes_cost", "fig6_leadlag",
                     "timeseries_figures"):
            assert f"skipped {name}: no runs" in log, log


def test_only_b0_or_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        m = Path(tmp) / "runs" / "session"
        _write_run(m, "exp6-baselines", "B0", "W2", 101, events=False)
        _write_run(m, "exp3", "merge-only/budget-lo", "W2", 101, invalid=True)
        out = Path(tmp) / "out"
        log = _run_main(Path(tmp) / "runs", out)
        doc = json.loads((out / "tables" / "timeseries_figures.json").read_text())
        assert doc["exp2"]["selection"]["n_invalid"] == 1 and doc["exp2"]["selection"]["n_used"] == 0
        assert "skipped exp2: no valid runs" in log, log
        assert not (out / "tables" / "exp2_correlation.csv").exists()


def test_pilot_runs() -> None:
    pilot = common.REPO_ROOT / "experiments" / "runs" / "pilot"
    if not pilot.is_dir():
        print("  (no pilot runs; skipped)")
        return
    mapping = json.dumps({"exp1-n200": "pilot-exp1", "exp1-baselines-n200": "pilot-baselines"})
    with tempfile.TemporaryDirectory() as tmp, _env(**{common.GROUP_MAP_ENV: mapping}):
        common.apply_style()
        _, log = _quiet(lambda: tf.main(pilot, Path(tmp)))
        assert "Traceback" not in log
        if (Path(tmp) / "tables" / "timeseries_figures.json").exists():
            doc = json.loads((Path(tmp) / "tables" / "timeseries_figures.json").read_text())
            assert set(doc) <= {"exp2", "fig1_utilization_histograms", "fig2_potential_nodes_cost",
                                "fig2_potential_nodes_cost_w1", "figure_runs_validity"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--write-synthetic":
        write_synthetic_tree(Path(argv[1]))
        print(f"wrote synthetic runs under {argv[1]}/session")
        return 0
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
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
