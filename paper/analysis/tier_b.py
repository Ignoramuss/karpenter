"""Tier B (``experiments/TIERB.md``): the seven WB configurations on KWOK against the simulator (Tier A).

Descriptive only, as the protocol fixes it: per size, Kendall's tau-b between the two tiers' configuration orders
for each metric, the configuration pairs the tiers order differently by more than a materiality floor, two pairs
reported on their own (random vs heuristic generation with the entropy-greedy planner; B4 vs B2), and the controller's
own compute beside Tier A's decision CPU. Both tiers' metrics come from their per-minute ``timeseries.csv`` over the
window [W, W + T], computed the same way.

Reads ``<runs>/tierb/tierb-a__*`` (Tier A) and ``<runs>/tierb-kwok/*`` (Tier B; a run counts only if its manifest
records exit status 0 and its summary says valid). Uses only the (seed, n) blocks in which all seven configurations
have a valid Tier B run, and the same blocks in Tier A. Writes, under ``out_root``:

- ``tables/tierb_runs.md``: every Tier B run, valid or not, with its reasons and starting fleet.
- ``tables/tierb_means.md``: per size and configuration, each metric's mean in both tiers over the retained blocks.
- ``tables/tierb_agreement.md``: per size and metric, tau-b, the number of material reversals and the blocks used.
- ``tables/tierb_reversals.md``: every material reversal.
- ``tables/tierb_pairs.md``: the two named pairs, per size and metric, as percent differences in both tiers.
- ``tables/tier_b.json``: the headline.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.tier_b [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from paper.analysis import common

TIER_A_DIR, TIER_A_GROUP, TIER_B_DIR = "tierb", "tierb-a", "tierb-kwok"
W, T = 5.0, 30.0  # warmup and window, minutes (TIERB.md; the window from the calibration on pilot seeds)
END_SPAN = 5.0  # cost_end averages the samples in the window's last 5 minutes
CONFIGS = ("B2", "B3", "B4", "heuristic/entropy-greedy", "heuristic/anneal-plan", "random/anneal-plan",
           "random/entropy-greedy")
# cost_end_rel (cost_end over the cost at the drop) was added before any evaluation-seed Tier B run, when the smoke
# cells showed Tier B arms of one seed starting from slightly different fleets (RESEARCH_LOG, "Tier B smokes").
METRICS = ("cost_end", "cost_end_rel", "cost_window", "nodes_end", "evictions", "settle_min", "compute")
# Materiality floors for a reversal (TIERB.md, "Analysis"): relative for cost, evictions and compute, absolute
# minutes for settle time. nodes_end has none in the protocol, so it gets tau-b only.
FLOORS: Mapping[str, tuple[str, float]] = {"cost_end": ("rel", 0.01), "cost_end_rel": ("rel", 0.01),
                                           "cost_window": ("rel", 0.01),
                                           "evictions": ("rel", 0.10), "compute": ("rel", 0.10),
                                           "settle_min": ("abs", 2.0)}
PAIRS = (("random/entropy-greedy", "heuristic/entropy-greedy"), ("B4", "B2"))
EVALUATED_TYPES = ("pipeline", "multi", "single")  # the consolidation types the arms differ in


def window_metrics(ts: pd.DataFrame) -> dict[str, float]:
    """The protocol's per-run metrics from a per-minute time series (columns t_min, nodes, cost_per_hour,
    evictions_cum), over [W, W + T]. NaN where the series does not cover the window."""
    ts = ts.sort_values("t_min")
    end = W + T
    win = ts[(ts.t_min >= W - 1e-6) & (ts.t_min <= end + 1e-6)]
    nan = dict.fromkeys(METRICS[:-1], math.nan)
    if len(win) < 2 or win.t_min.iloc[0] > W + 0.5 or win.t_min.iloc[-1] < end - 0.5:
        return nan
    tail = win[win.t_min >= end - END_SPAN - 1e-6]
    changes = win.t_min[win.nodes.diff().fillna(0) != 0]
    cost_end, at_drop = float(tail.cost_per_hour.mean()), float(win.cost_per_hour.iloc[0])
    return {
        "cost_end": cost_end,
        "cost_end_rel": cost_end / at_drop if at_drop > 0 else math.nan,
        "cost_window": float(np.trapezoid(win.cost_per_hour, win.t_min) / 60.0),
        "nodes_end": float(win.nodes.iloc[-1]),
        "evictions": float(win.evictions_cum.iloc[-1] - win.evictions_cum.iloc[0]),
        "settle_min": float(changes.max() - W) if len(changes) else 0.0,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tier_a_runs(runs_root: Path) -> pd.DataFrame:
    rows = []
    for d in sorted((Path(runs_root) / TIER_A_DIR).glob(f"{TIER_A_GROUP}__*")):
        s, ts = _read_json(d / "summary.json"), d / "timeseries.csv"
        if not s or not ts.is_file():
            continue
        valid = not s.get("error") and not s.get("timed_out") and s.get("pdb_violations", 0) == 0 and \
            s.get("do_not_disrupt_violations", 0) == 0
        rows.append({"label": s["label"], "seed": int(s["seed"]), "n": int(s["scenario_nodes"]), "valid": valid,
                     **window_metrics(pd.read_csv(ts)), "compute": float(s.get("cpu_pass_total_s", math.nan))})
    return pd.DataFrame(rows, columns=["label", "seed", "n", "valid", *METRICS])


def _tier_b_reasons(m: Mapping[str, Any] | None, s: Mapping[str, Any] | None) -> list[str]:
    if not m:
        return ["no manifest"]
    reasons = []
    if m.get("test_exit_status") != 0:
        reasons.append(f"exit status {m.get('test_exit_status')}")
    if not s:
        return reasons + ["no summary"]
    if s.get("pending_at_drop"):
        reasons.append(f"{s['pending_at_drop']} pods pending at the drop")
    if s.get("evictions_before_drop"):
        reasons.append(f"{s['evictions_before_drop']} evictions before the drop")
    if s.get("karpenter_restarted"):
        reasons.append("controller restarted")
    if s.get("method_ok") is False:
        reasons.append("wrong methods decided")
    if not s.get("metrics_sample_count"):
        reasons.append("no metrics samples")
    if s.get("valid") is not True and not reasons:
        reasons.append("summary not valid")
    return reasons


def tier_b_runs(runs_root: Path) -> pd.DataFrame:
    """Every Tier B run directory, including set-aside attempts (``<run_id>.failed-N`` and the like)."""
    rows = []
    for d in sorted((Path(runs_root) / TIER_B_DIR).iterdir()) if (Path(runs_root) / TIER_B_DIR).is_dir() else []:
        if not d.is_dir():
            continue
        m, s = _read_json(d / "manifest.json"), _read_json(d / "summary.json")
        cell = _read_json(d / "cell.json") or {}
        reasons = _tier_b_reasons(m, s)
        ts = d / "timeseries.csv"
        metrics = window_metrics(pd.read_csv(ts)) if ts.is_file() else dict.fromkeys(METRICS[:-1], math.nan)
        evals = (s or {}).get("decision_evaluation_seconds") or {}
        rows.append({
            "run_id": d.name, "attempt": d.name.partition(".")[2] or "final", "label": cell.get("label"),
            "seed": cell.get("seed"), "n": cell.get("nodes"), "valid": not reasons and "." not in d.name,
            "reasons": "; ".join(reasons), "nodes_at_drop": (s or {}).get("nodes_at_drop"),
            "cost_at_drop": (s or {}).get("cost_at_drop"), **metrics,
            "compute": (s or {}).get("karpenter_cpu_core_seconds", math.nan),
            "evaluation_s": float(sum(evals.get(t, 0.0) for t in EVALUATED_TYPES)) if s else math.nan,
            "max_memory_mb": (s or {}).get("karpenter_max_memory_mb", math.nan),
        })
    cols = ["run_id", "attempt", "label", "seed", "n", "valid", "reasons", "nodes_at_drop", "cost_at_drop", *METRICS,
            "evaluation_s", "max_memory_mb"]
    return pd.DataFrame(rows, columns=cols)


def complete_blocks(b: pd.DataFrame) -> list[tuple[int, int]]:
    """(seed, n) blocks in which every configuration has a valid Tier B run."""
    ok = b[b.valid]
    have = ok.groupby(["seed", "n"]).label.apply(lambda s: set(s))
    return sorted((int(seed), int(n)) for (seed, n), labels in have.items() if set(CONFIGS) <= labels)


def _material(metric: str, x: float, y: float) -> bool:
    kind, floor = FLOORS[metric]
    if kind == "abs":
        return abs(x - y) > floor
    mid = (abs(x) + abs(y)) / 2
    return mid > 0 and abs(x - y) / mid > floor


def agreement(means: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per size and metric: tau-b over the configurations, and the material reversals."""
    rows, revs = [], []
    for n, g in means.groupby("n"):
        g = g.set_index("label").reindex(list(CONFIGS))
        for metric in METRICS:
            a, b = g[f"{metric}_a"], g[f"{metric}_b"]
            ok = a.notna() & b.notna()
            tau, p = (stats.kendalltau(a[ok], b[ok]) if ok.sum() >= 3 else (math.nan, math.nan))
            n_rev = 0
            if metric in FLOORS:
                labels = list(a[ok].index)
                for i, li in enumerate(labels):
                    for lj in labels[i + 1:]:
                        da, db = a[li] - a[lj], b[li] - b[lj]
                        if np.sign(da) * np.sign(db) < 0 and _material(metric, a[li], a[lj]) and \
                                _material(metric, b[li], b[lj]):
                            n_rev += 1
                            revs.append({"n": n, "metric": metric, "config_1": li, "config_2": lj,
                                         "tier_a_1": a[li], "tier_a_2": a[lj], "tier_b_1": b[li], "tier_b_2": b[lj]})
            rows.append({"n": n, "metric": metric, "configs": int(ok.sum()), "tau_b": tau, "p_value": p,
                         "material_reversals": n_rev if metric in FLOORS else None})
    return pd.DataFrame(rows), pd.DataFrame(revs, columns=["n", "metric", "config_1", "config_2", "tier_a_1",
                                                           "tier_a_2", "tier_b_1", "tier_b_2"])


def pair_table(means: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for n, g in means.groupby("n"):
        g = g.set_index("label")
        for x, y in PAIRS:
            if x not in g.index or y not in g.index:
                continue
            for metric in METRICS:
                pa = 100 * (g.at[x, f"{metric}_a"] / g.at[y, f"{metric}_a"] - 1) if g.at[y, f"{metric}_a"] else math.nan
                pb = 100 * (g.at[x, f"{metric}_b"] / g.at[y, f"{metric}_b"] - 1) if g.at[y, f"{metric}_b"] else math.nan
                rows.append({"n": n, "pair": f"{x} vs {y}", "metric": metric, "tier_a_pct": pa, "tier_b_pct": pb,
                             "same_sign": bool(np.sign(pa) == np.sign(pb)) if np.isfinite(pa) and np.isfinite(pb)
                             else None})
    return pd.DataFrame(rows, columns=["n", "pair", "metric", "tier_a_pct", "tier_b_pct", "same_sign"])


def main(runs_root: Path, out_root: Path) -> None:
    """Write the Tier B tables (rank agreement with Tier A) under out_root."""
    b = tier_b_runs(Path(runs_root))
    if b.empty:
        common.skipped("tier_b", "no Tier B runs")
        return
    a = tier_a_runs(Path(runs_root))
    blocks = complete_blocks(b)
    keep = pd.MultiIndex.from_tuples(blocks, names=["seed", "n"]) if blocks else None
    common.write_table(b.sort_values(["n", "seed", "label", "run_id"]), Path(out_root), "tierb_runs")

    def block_means(df: pd.DataFrame, cols: Sequence[str]) -> pd.DataFrame:
        if keep is None:
            return pd.DataFrame(columns=["n", "label", *cols])
        sel = df[df.set_index(["seed", "n"]).index.isin(keep)]
        return sel.groupby(["n", "label"], as_index=False)[list(cols)].mean()

    ma = block_means(a[a.valid], METRICS).rename(columns={m: f"{m}_a" for m in METRICS})
    mb = block_means(b[b.valid], [*METRICS, "evaluation_s", "max_memory_mb", "nodes_at_drop", "cost_at_drop"]).rename(
        columns={m: f"{m}_b" for m in METRICS})
    means = ma.merge(mb, on=["n", "label"], how="outer")
    means["label"] = pd.Categorical(means["label"], categories=list(CONFIGS), ordered=True)
    means = means.sort_values(["n", "label"]).reset_index(drop=True)
    means["label"] = means["label"].astype(str)
    agree, revs = agreement(means)
    pairs = pair_table(means)
    common.write_table(means, Path(out_root), "tierb_means")
    common.write_table(agree, Path(out_root), "tierb_agreement")
    common.write_table(revs, Path(out_root), "tierb_reversals")
    common.write_table(pairs, Path(out_root), "tierb_pairs")
    common.write_json({
        "blocks": [{"seed": s, "n": n} for s, n in blocks],
        "runs": {"total": int(len(b)), "valid": int(b.valid.sum()), "set_aside": int((b.attempt != "final").sum())},
        "agreement": agree, "material_reversals": int(len(revs)), "pairs": pairs,
    }, Path(out_root), "tier_b")


if __name__ == "__main__":
    common.cli(main)
