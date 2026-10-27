"""Exp 6 (H6), the savings vs disruption frontier, and Exp 5 (H5), plan fidelity: figures 3 and 5.

Exp 6 sweeps the entropy-greedy planner's lambda and places each configuration by its savings against the
no-consolidation baseline B0 (``integrated_cost_post`` of the B0 run on the same scenario, nodes, seed and placement,
minus the run's own, in $: B5 runs under most-allocated placement and pairs with the most-allocated B0 of group
exp6-b5, everything else with the least-allocated B0) and its disruption (``eviction_cost``; ``evictions``
alongside). Each upstream operating point is compared with the lambda point fixed for it in advance (registry
``exp6.params.matched_lambda``: B2 with lambda 0.05, B3 with 0.5, B5 with 0.1; no selection on results), on savings
and on eviction cost, paired by seed, Wilcoxon, Holm over the 12 tests of family H6 (experiments/PREREGISTRATION.md,
H6).

Exp 5 measures plan fidelity (the share of evicted pods whose replacement lands on the planned node) by placement model
x scale-down model, for random/entropy-greedy, B2 and the steered executor, and tests the 10 preregistered contrasts
of family H5 (experiments.yaml ``exp5.comparisons``), plus the off-plan share clause (a) and the oracle check.

Outputs under ``out_root`` (nothing is written for an experiment without runs):

- ``tables/exp6_frontier.{csv,md}``: one row per (scenario, nodes, label, placement): savings, eviction cost, evictions
  and steady-state $/hr with bootstrap CIs, and whether the point is on the empirical Pareto set (descriptive).
- ``tables/exp6_dominance.{csv,md}``: the 12 H6 tests (matched lambda point - baseline, per scenario and metric) with
  their floors, Holm p and outcome, and the per-cell verdict (dominates, dominated, inconclusive).
- ``tables/exp5_fidelity.{csv,md}``: one row per (scenario, nodes, label, placement, scale_down) cell: fidelity,
  off-plan share, realized/predicted P2 change, steady-state $/hr and evictions.
- ``tables/exp5_tests.{csv,md}``: the 10 tests of family H5 with floors, Holm p, direction and outcome.
- ``tables/frontier_fidelity.json``: the headline numbers and verdicts of both experiments.
- ``figures/out/fig3_frontier.{png,pdf}`` and ``figures/out/fig5_fidelity.{png,pdf}``.

Configurations with fewer than ``common.min_seeds()`` finished runs are cut (``common.select_runs``); while a
configuration a verdict needs is missing or cut the verdict is "incomplete".

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.frontier_fidelity [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from paper.analysis import common

EXP6 = "exp6"
EXP5 = "exp5"
ALPHA = common.ALPHA

# --------------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------------


def _floats(values: Any) -> np.ndarray:
    """``values`` as a float array; missing and non-numeric entries become NaN (nothing is dropped)."""
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)


def _ordered(values: Iterable[Any], preferred: Sequence[Any]) -> list[Any]:
    """The distinct ``values``: those in ``preferred`` first, in its order, then the rest sorted as strings."""
    present = {v for v in values if not (isinstance(v, float) and math.isnan(v))}
    head = [v for v in preferred if v in present]
    return head + sorted(present - set(head), key=str)


def _fmt_nodes(nodes: Any) -> str:
    try:
        return f"{float(nodes):g}"
    except (TypeError, ValueError):
        return str(nodes)


def _strata(df: pd.DataFrame, scenarios: Sequence[str]) -> list[tuple[str, Any]]:
    """The (scenario, nodes) pairs in ``df``: scenarios in registry order, then nodes ascending."""
    pairs = {(s, n) for s, n in df[["scenario", "nodes"]].itertuples(index=False, name=None)
             if isinstance(s, str) and pd.notna(n)}
    order = {s: i for i, s in enumerate(_ordered((s for s, _ in pairs), scenarios))}
    return sorted(pairs, key=lambda p: (order[p[0]], float(p[1])))


def _in_stratum(df: pd.DataFrame, scenario: str, nodes: Any) -> pd.DataFrame:
    return df.loc[(df["scenario"] == scenario) & (df["nodes"] == nodes)]


def ci_columns(prefix: str, values: Any) -> dict[str, float]:
    """``<prefix>_mean``, ``_mean_lo``, ``_mean_hi``, ``_median``, ``_median_lo``, ``_median_hi`` of the finite
    ``values``: the estimates and their 95% percentile bootstrap intervals (``common.bootstrap_ci``, seed 0)."""
    mean, lo, hi = common.bootstrap_ci(_floats(values), "mean")
    med, mlo, mhi = common.bootstrap_ci(_floats(values), "median")
    return {f"{prefix}_mean": mean, f"{prefix}_mean_lo": lo, f"{prefix}_mean_hi": hi,
            f"{prefix}_median": med, f"{prefix}_median_lo": mlo, f"{prefix}_median_hi": mhi}


def ci_names(prefix: str) -> list[str]:
    return [f"{prefix}_{s}" for s in ("mean", "mean_lo", "mean_hi", "median", "median_lo", "median_hi")]


def rank_direction(diffs: Any) -> int:
    """The direction the signed-rank test sees in paired differences (``common.rank_direction``): +1 when the positive
    differences carry more rank weight than the negative ones (W+ > W-), -1 for the reverse, 0 for a tie or no
    non-zero differences."""
    return common.rank_direction(_floats(diffs))


def ratio_of_sums_ci(num: Any, den: Any, n: int = common.N_BOOT, alpha: float = ALPHA,
                     seed: int = common.BOOT_SEED) -> tuple[float, float, float]:
    """(estimate, lo, hi) of sum(num) / sum(den) over paired entries, with a percentile bootstrap interval that
    resamples entries (runs) jointly from ``default_rng(seed)``. NaN triple without pairs or when sum(den) is 0."""
    num, den = _floats(num), _floats(den)
    ok = np.isfinite(num) & np.isfinite(den)
    num, den = num[ok], den[ok]
    if num.size == 0 or den.sum() == 0:
        return math.nan, math.nan, math.nan
    est = float(num.sum() / den.sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, num.size, size=(n, num.size))
    with np.errstate(divide="ignore", invalid="ignore"):
        boot = num[idx].sum(axis=1) / den[idx].sum(axis=1)
    boot = boot[np.isfinite(boot)]
    if boot.size == 0:
        return est, math.nan, math.nan
    lo, hi = np.percentile(boot, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return est, float(lo), float(hi)


def pareto_flags(savings: Sequence[float], disruption: Sequence[float]) -> list[bool | None]:
    """For each point (savings, disruption): True if no other point has savings >= and disruption <= with at least
    one strict (the empirical Pareto set when more savings and less disruption are better), False if one does,
    None when the point itself is undefined (a NaN coordinate). Undefined points dominate nothing."""
    pts = [(float(s), float(d)) for s, d in zip(savings, disruption)]
    ok = [math.isfinite(s) and math.isfinite(d) for s, d in pts]
    out: list[bool | None] = []
    for i, (s, d) in enumerate(pts):
        if not ok[i]:
            out.append(None)
            continue
        dominated = any(ok[j] and j != i and sj >= s and dj <= d and (sj > s or dj < d)
                        for j, (sj, dj) in enumerate(pts))
        out.append(not dominated)
    return out


def _wrap_label(label: str, width: int = 22) -> str:
    """Break a configuration label after "/" so each line stays near ``width`` characters (panel titles)."""
    if len(label) <= width:
        return label
    lines, cur = [], ""
    for part in re.split(r"(?<=/)", label):
        if cur and len(cur) + len(part) > width:
            lines.append(cur)
            cur = part
        else:
            cur += part
    lines.append(cur)
    return "\n".join(lines)


def _err(mean: Any, lo: Any, hi: Any) -> np.ndarray:
    """matplotlib's asymmetric error-bar array [[below], [above]] from estimates and interval ends (NaN -> 0)."""
    mean, lo, hi = (np.asarray(v, dtype=float) for v in (mean, lo, hi))
    below = np.nan_to_num(np.clip(mean - lo, 0, None))
    above = np.nan_to_num(np.clip(hi - mean, 0, None))
    return np.vstack([below, above])


def _validity(sel: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-group run counts and invalid reasons (abandoned cells included) for the JSON headline."""
    return common.to_jsonable(common.validity_report(sel, by=("group",)))


# --------------------------------------------------------------------------------------------------------------------
# Exp 6: savings vs disruption frontier
# --------------------------------------------------------------------------------------------------------------------

SAVINGS = "savings"
FRONTIER_METRICS = (SAVINGS, "eviction_cost", "evictions", "steady_cost_per_hour")
FRONTIER_COLUMNS = (["scenario", "nodes", "label", "placement", "kind", "lambda", "n_runs", "n_valid", "n_invalid",
                     "n_excluded", "n_used"] + [c for m in FRONTIER_METRICS for c in ci_names(m)] + ["pareto"])
DOMINANCE_COLUMNS = [
    "experiment", "family", "scenario", "nodes", "baseline", "matched_label", "matched_lambda", "metric", "better_if",
    "n_pairs", "n_dropped", "n_dropped_invalid", "n_undefined", "n_dropped_missing", "n_b0_invalid", "mean_matched",
    "mean_baseline", "mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo", "median_diff_hi",
    "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon", "p_floor", "m", "p_holm", "significant",
    "holm_reachable", "direction", "outcome", "result", "verdict",
]
_LAMBDA_IN_LABEL = re.compile(r"(?:^|/)lambda=([^/]+)$")


def add_savings(df: pd.DataFrame, keys: Sequence[str], zero_label: str, cost_metric: str) -> pd.DataFrame:
    """A copy of ``df`` with ``b0_cost`` (``cost_metric`` of the valid ``zero_label`` run sharing the row's ``keys``,
    which include the placement, so B5's most-allocated runs pair with the most-allocated B0), ``b0_invalid`` (that B0
    run exists but is invalid) and ``savings`` = b0_cost - the row's own ``cost_metric`` ($ saved by consolidation).
    Savings are NaN (undefined) without a valid B0 on the row's keys.

    Keys on which two ``zero_label`` runs collide have no defined reference: they are logged and get NaN savings.
    """
    keys = list(keys)
    zero = df.loc[df["label"] == zero_label]
    dup = zero.duplicated(keys, keep=False)
    if dup.any():
        common.logger.warning("exp6: %d %s runs share keys %s; their seeds get no savings", int(dup.sum()),
                              zero_label, keys)
        zero = zero.loc[~dup]
    cost = pd.to_numeric(zero[cost_metric], errors="coerce")
    ok = zero["valid"].astype(bool) & np.isfinite(cost.to_numpy(dtype=float))
    ref = {tuple(k): float(c) for k, c in zip(zero.loc[ok, keys].itertuples(index=False, name=None), cost[ok])}
    bad = {tuple(k) for k in zero.loc[~zero["valid"].astype(bool), keys].itertuples(index=False, name=None)}
    out = df.copy()
    tuples = [tuple(k) for k in out[keys].itertuples(index=False, name=None)]
    out["b0_cost"] = [ref.get(k, math.nan) for k in tuples]
    out["b0_invalid"] = [k in bad for k in tuples]
    out[SAVINGS] = out["b0_cost"] - pd.to_numeric(out[cost_metric], errors="coerce")
    return out


def _lambda_of(cell: pd.DataFrame, label: str, column: str) -> float:
    """The swept parameter of a configuration: the single finite value of ``column`` among its runs, else the value
    after "lambda=" in its label, else NaN."""
    values = sorted(set(v for v in _floats(cell[column]) if math.isfinite(v))) if column in cell else []
    if len(values) == 1:
        return float(values[0])
    m = _LAMBDA_IN_LABEL.search(label)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return math.nan


def frontier_table(df: pd.DataFrame, exp: dict[str, Any]) -> pd.DataFrame:
    """Exp 6 frontier points (descriptive): one row per (scenario, nodes, label, placement) for B0 (each placement it
    runs under), the lambda sweep and the operating points.

    ``df`` carries ``savings`` (``add_savings``). Statistics are over the point's *used* runs: valid, with a valid B0
    run on the same seed and placement, and finite savings and eviction cost (``n_excluded`` counts valid runs left
    out for lack of a B0 pair). ``kind`` is reference (B0), sweep or baseline; ``lambda`` the swept value; ``pareto``
    whether the point is on the empirical Pareto set of its stratum (``pareto_flags`` over mean savings and mean
    eviction cost; all points, B0 included). A stratum without any valid B0 run is skipped, since savings are
    undefined there.
    """
    params = exp["params"]
    zero, sweep, ops = params["zero_label"], list(exp["labels"]), list(params["operating_points"])
    labels = [zero, *sweep, *ops]
    rows: list[dict[str, Any]] = []
    for scenario, nodes in _strata(df, exp["scenarios"]):
        part = _in_stratum(df, scenario, nodes)
        if not np.isfinite(part["b0_cost"].to_numpy(dtype=float)).any():
            common.skipped(f"exp6 frontier {scenario}/{_fmt_nodes(nodes)}", f"no valid {zero} run, so savings are "
                           "undefined")
            continue
        stratum: list[dict[str, Any]] = []
        for label in labels:
            lab = part.loc[part["label"] == label]
            for placement in _ordered(lab["placement"], ["least-allocated", "most-allocated"]):
                cell = lab.loc[lab["placement"] == placement]
                valid = cell.loc[cell["valid"].astype(bool)]
                used = valid.loc[np.isfinite(valid[SAVINGS].to_numpy(dtype=float))
                                 & np.isfinite(_floats(valid["eviction_cost"]))]
                row: dict[str, Any] = {
                    "scenario": scenario, "nodes": nodes, "label": label, "placement": placement,
                    "kind": "reference" if label == zero else ("sweep" if label in sweep else "baseline"),
                    "lambda": _lambda_of(cell, label, params["sweep_param"]) if label in sweep else math.nan,
                    "n_runs": len(cell), "n_valid": len(valid), "n_invalid": len(cell) - len(valid),
                    "n_excluded": len(valid) - len(used), "n_used": len(used),
                }
                for m in FRONTIER_METRICS:
                    row.update(ci_columns(m, used[m]))
                stratum.append(row)
        flags = pareto_flags([r[f"{SAVINGS}_mean"] for r in stratum], [r["eviction_cost_mean"] for r in stratum])
        for r, flag in zip(stratum, flags):
            r["pareto"] = flag
        rows += stratum
    return pd.DataFrame(rows, columns=FRONTIER_COLUMNS)


def _check_matched(exp: dict[str, Any], comps: Sequence[common.Comparison]) -> dict[str, str]:
    """baseline -> matched lambda label, from the registry comparisons, checked against ``params.matched_lambda``
    (the preregistered pairing, fixed in advance)."""
    params = exp["params"]
    matched: dict[str, str] = {}
    for c in comps:
        matched.setdefault(str(c.b), str(c.a))
    for baseline, lam in params["matched_lambda"].items():
        label = matched.get(baseline)
        if label is None or not math.isclose(_lambda_of(pd.DataFrame(), label, params["sweep_param"]), float(lam)):
            raise ValueError(f"exp6: the comparisons pair {baseline} with {label!r}, the registry fixes lambda {lam}")
    return matched


def _b0_invalid_pairs(df: pd.DataFrame, a: str, b: str, scenario: str, nodes: Any, keys: Sequence[str]) -> int:
    """Seeds of a (scenario, nodes) stratum where both arms ran but a B0 reference of either arm is invalid (its
    savings are then undefined)."""
    part = _in_stratum(df, scenario, nodes)
    seeds: dict[str, set[Any]] = {}
    bad: set[Any] = set()
    for arm in (a, b):
        rows = part.loc[part["label"] == arm]
        seeds[arm] = set(rows["seed"].tolist())
        bad |= set(rows.loc[rows["b0_invalid"].astype(bool), "seed"].tolist())
    return len(bad & seeds[a] & seeds[b])


def cell_result(metric_row: Mapping[str, Any], better_if: str) -> str:
    """One H6 test as seen from the pipeline: "better" / "worse" (significant after Holm, in the signed-rank
    direction), "no difference", or "inconclusive" when the test could not have been significant (unreachable)."""
    outcome = metric_row.get("outcome")
    if outcome in ("lower", "higher"):
        return "better" if outcome == better_if else "worse"
    return "inconclusive" if outcome in common.UNINFORMATIVE_OUTCOMES else "no difference"


def cell_verdict(results: Mapping[str, str]) -> str:
    """H6 for one (baseline, scenario) cell from its two tests' results (``cell_result``): "dominates" iff one metric
    is significantly better and the other not significantly worse, "dominated" iff a metric is significantly worse
    and none significantly better, otherwise "inconclusive". A "not significantly worse" resting on an unreachable
    test is inconclusive, never support (G6); an unreachable test cannot be significantly better, so it never rescues
    a cell that is significantly worse on the other metric."""
    better = [m for m, r in results.items() if r == "better"]
    worse = [m for m, r in results.items() if r == "worse"]
    if better and not worse:
        others = [results[m] for m in results if m not in better]
        return "inconclusive" if "inconclusive" in others else "dominates"
    if worse and not better:
        return "dominated"
    return "inconclusive"


def dominance_table(df: pd.DataFrame, exp: dict[str, Any], comps: Sequence[common.Comparison]) -> pd.DataFrame:
    """Exp 6 dominance tests: the 12 tests of family H6 (``common.evaluate`` over the registry comparisons: each
    operating point against its preregistered lambda point on savings and on eviction cost, per scenario; Holm over
    the 12), one row per test (``DOMINANCE_COLUMNS``), differences matched - baseline. ``result`` is the test from the
    pipeline's side (``cell_result``) and ``verdict`` the cell's (``cell_verdict``). ``n_b0_invalid`` counts seeds
    whose savings are undefined because a B0 reference is invalid (in ``n_undefined``)."""
    params = exp["params"]
    keys = list(exp["pair_keys"])
    tests = common.evaluate(df, comps)
    if tests.empty:
        return pd.DataFrame(columns=DOMINANCE_COLUMNS)
    better_if = params["better_if"]
    out = tests.rename(columns={"b": "baseline", "a": "matched_label", "mean_a": "mean_matched",
                                "mean_b": "mean_baseline"})
    out["matched_lambda"] = [_lambda_of(pd.DataFrame(), lab, params["sweep_param"]) for lab in out["matched_label"]]
    out["better_if"] = out["metric"].map(better_if)
    out["n_b0_invalid"] = [
        _b0_invalid_pairs(df, a, b, s, n, keys) if m == SAVINGS else 0
        for a, b, s, n, m in zip(out["matched_label"], out["baseline"], out["scenario"], out["nodes"], out["metric"])]
    out["result"] = [cell_result(r, better_if[r["metric"]]) for r in out.to_dict("records")]
    out["verdict"] = ""
    for _, idx in out.groupby(["scenario", "nodes", "baseline"], sort=False).groups.items():
        g = out.loc[idx]
        out.loc[idx, "verdict"] = cell_verdict(dict(zip(g["metric"], g["result"])))
    return out.reindex(columns=DOMINANCE_COLUMNS).reset_index(drop=True)


def figure3(frontier: pd.DataFrame, exp: dict[str, Any], out_root: Path) -> bool:
    """Figure 3: one panel per (scenario, nodes); x = mean cumulative eviction cost, y = mean cumulative savings vs
    B0 ($), 95% bootstrap CIs as error bars on both. Lambda points are joined in lambda order and labelled with their
    lambda; B0 and the operating points are grey markers with direct labels (B5 and its B0 run under most-allocated
    placement). Returns False (and writes nothing) when no point has defined savings."""
    fr = frontier.loc[np.isfinite(frontier[f"{SAVINGS}_mean"].to_numpy(dtype=float))
                      & np.isfinite(frontier["eviction_cost_mean"].to_numpy(dtype=float))]
    if fr.empty:
        common.skipped("fig3_frontier", "no frontier point with defined savings")
        return False
    sweep_placement = exp["params"].get("sweep_placement", "least-allocated")
    strata = list(dict.fromkeys(fr[["scenario", "nodes"]].itertuples(index=False, name=None)))
    figsize = common.FIGSIZE["single_tall"] if len(strata) == 1 else (common.FIGSIZE["double"][0], 3.0)
    fig, axes = plt.subplots(1, len(strata), figsize=figsize, squeeze=False)
    handles: dict[str, Any] = {}
    sweep_color = common.PALETTE[0]
    for ax, (scenario, nodes) in zip(axes[0], strata):
        part = _in_stratum(fr, scenario, nodes)
        sweep = part.loc[part["kind"] == "sweep"].sort_values("lambda", kind="stable")
        if len(sweep):
            x, y = sweep["eviction_cost_mean"].to_numpy(float), sweep[f"{SAVINGS}_mean"].to_numpy(float)
            h = ax.errorbar(x, y, xerr=_err(x, sweep["eviction_cost_mean_lo"], sweep["eviction_cost_mean_hi"]),
                            yerr=_err(y, sweep[f"{SAVINGS}_mean_lo"], sweep[f"{SAVINGS}_mean_hi"]),
                            color=sweep_color, marker="o", markersize=4, linestyle="-", linewidth=1.5,
                            elinewidth=0.8, capsize=2, zorder=3)
            handles.setdefault("λ sweep (random/entropy-greedy)", h)
            for xi, yi, lam in zip(x, y, sweep["lambda"]):
                if math.isfinite(lam):
                    # Above-left of the (typically concave, increasing) sweep, away from dominated baselines.
                    ax.annotate(f"λ={lam:g}", (xi, yi), xytext=(-4, 4), textcoords="offset points", fontsize=7,
                                ha="right", va="bottom", color=common.NEUTRAL)
        others = part.loc[part["kind"] != "sweep"]
        for label in _ordered(others["label"], exp["baselines"]):
            for r in others.loc[others["label"] == label].to_dict("records"):
                style = common.style_for(label)
                name = label if r["placement"] == sweep_placement else f"{label} ({r['placement']})"
                x, y = float(r["eviction_cost_mean"]), float(r[f"{SAVINGS}_mean"])
                h = ax.errorbar([x], [y], xerr=_err([x], [r["eviction_cost_mean_lo"]], [r["eviction_cost_mean_hi"]]),
                                yerr=_err([y], [r[f"{SAVINGS}_mean_lo"]], [r[f"{SAVINGS}_mean_hi"]]),
                                color=style["color"], marker=style["marker"], markersize=6, linestyle="none",
                                elinewidth=0.8, capsize=2, zorder=4)
                handles.setdefault(label, h)
                if label == exp["params"]["zero_label"] and r["placement"] != sweep_placement:
                    continue  # both B0 points sit at (0, 0): label one
                ax.annotate(name, (x, y), xytext=(6, 0), textcoords="offset points", fontsize=8, ha="left",
                            va="center", color=common.NEUTRAL)
        ax.set_title(f"{scenario}, {_fmt_nodes(nodes)} nodes")
        ax.set_xlabel("Cumulative eviction cost")
        ax.margins(x=0.12, y=0.12)
    axes[0][0].set_ylabel("Cumulative savings vs B0 ($)")
    names = list(handles)
    fig.legend([handles[n] for n in names], names, loc="outside lower center", ncol=min(len(names), 5))
    common.save_figure(fig, out_root, "fig3_frontier")
    return True


def exp6_headline(everything: pd.DataFrame, frontier: pd.DataFrame, dominance: pd.DataFrame, exp: dict[str, Any],
                  missing: list[dict[str, Any]]) -> dict[str, Any]:
    """Exp 6 headline: validity counts, the Pareto set per stratum, one entry per (baseline, scenario) cell with the
    preregistered lambda point, the savings and eviction-cost differences (CIs, floors, Holm p) and the cell verdict,
    and H6's verdict: supported iff no cell is dominated and at least ``min_dominating_cells`` of the 6 dominate; not
    supported if any is dominated; otherwise inconclusive ("incomplete" while configurations are missing)."""
    pareto = {}
    for (scenario, nodes), part in frontier.groupby(["scenario", "nodes"], sort=False):
        pareto[f"{scenario}/{_fmt_nodes(nodes)}"] = [
            f"{lab} ({pl})" if lab == exp["params"]["zero_label"] else lab
            for lab, pl in part.loc[part["pareto"] == True, ["label", "placement"]].itertuples(  # noqa: E712
                index=False, name=None)]
    entries = []
    for (scenario, nodes, baseline), g in dominance.groupby(["scenario", "nodes", "baseline"], sort=False):
        e: dict[str, Any] = {"scenario": scenario, "nodes": nodes, "baseline": baseline,
                             "matched_label": g["matched_label"].iloc[0],
                             "matched_lambda": g["matched_lambda"].iloc[0], "verdict": g["verdict"].iloc[0]}
        for r in g.to_dict("records"):
            e[r["metric"]] = {k: r[k] for k in ("n_pairs", "n_dropped", "n_dropped_invalid", "n_undefined",
                                                "n_b0_invalid", "mean_matched", "mean_baseline", "mean_diff",
                                                "mean_diff_lo", "mean_diff_hi", "pct_diff", "pct_diff_lo",
                                                "pct_diff_hi", "p_wilcoxon", "p_floor", "m", "p_holm",
                                                "holm_reachable", "outcome", "result", "family")}
        entries.append(e)
    verdicts = [e["verdict"] for e in entries]
    need = int(exp["params"].get("min_dominating_cells", 3))
    if "dominated" in verdicts:
        verdict = "not supported"
    elif verdicts.count("dominates") >= need:
        verdict = "supported"
    else:
        verdict = "inconclusive"
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(everything), "pareto": pareto, "cells": entries,
        "verdict_counts": {v: verdicts.count(v) for v in sorted(set(verdicts))},
        "n_cells": len(entries), "matched_lambda": dict(exp["params"]["matched_lambda"]),
        "rule": "per baseline and scenario: the preregistered lambda point (B2: 0.05, B3: 0.5, B5: 0.1) vs the "
                "baseline on savings vs B0 (same seed and placement) and on eviction cost, paired two-sided exact "
                "Wilcoxon, Holm over the 12; a cell dominates iff one metric is significantly better and the other "
                "not significantly worse, is dominated iff a metric is significantly worse and none significantly "
                "better, else inconclusive; supported iff no cell is dominated and at least 3 of 6 dominate, not "
                "supported if any is dominated, otherwise inconclusive",
        "verdict": "incomplete" if missing else verdict, "verdict_on_available": verdict, "complete": not missing,
        "missing_configurations": missing,
    }


def run_exp6(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 6 tables (exp6_frontier, exp6_dominance) and figure 3; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP6)
    params = exp["params"]
    everything = common.select_runs(runs, EXP6, include_cut=True)
    sel = common.select_runs(runs, EXP6)
    known = [params["zero_label"], *exp["labels"], *params["operating_points"]]
    if everything.empty or not everything["label"].isin(known).any():
        common.skipped(EXP6)
        return None
    extra = sorted(set(sel["label"].dropna().astype(str)) - set(known))
    if extra:
        common.logger.info("exp6: ignoring labels outside the registry: %s", ", ".join(extra))
    sel = sel.loc[sel["label"].isin(known)]
    keys = list(exp["pair_keys"]) + ["placement"]
    df = add_savings(sel, keys, params["zero_label"], params["savings_metric"])
    comps = common.comparisons(EXP6)
    _check_matched(exp, comps)
    frontier = frontier_table(df, exp)
    if len(frontier):
        common.write_table(frontier, out_root, "exp6_frontier")
    else:
        common.skipped("exp6_frontier", f"no valid {params['zero_label']} run, so savings are undefined")
    dominance = dominance_table(df, exp, comps)
    common.write_table(dominance, out_root, "exp6_dominance")
    if len(frontier):
        figure3(frontier, exp, out_root)
    zero_refs = [({"label": params["zero_label"], "placement": pl}, {"scenario": s, "nodes": n})
                 for pl in sorted(set(params["placement_of"].values())) for s in exp["scenarios"] for n in exp["nodes"]]
    missing = common.missing_configurations(everything, comps, zero_refs)
    return exp6_headline(everything, frontier, dominance, exp, missing)


# --------------------------------------------------------------------------------------------------------------------
# Exp 5: plan fidelity
# --------------------------------------------------------------------------------------------------------------------

RATIO = "delta_p2_ratio"
CELL_KEYS = ["scenario", "nodes", "label", "placement", "scale_down"]
FIDELITY_COLUMNS = (
    CELL_KEYS + ["n_runs", "n_valid", "n_invalid", "n_fidelity"] + ci_names("fidelity")
    + ["fidelity_pooled", "off_plan_share_mean", "off_plan_share_mean_lo", "off_plan_share_mean_hi", "n_ratio"]
    + ci_names(RATIO) + ["delta_p2_ratio_of_sums", "delta_p2_ratio_of_sums_lo", "delta_p2_ratio_of_sums_hi",
                         "predicted_delta_p2_mean", "predicted_delta_p2_mean_lo", "predicted_delta_p2_mean_hi",
                         "realized_delta_p2_mean", "realized_delta_p2_mean_lo", "realized_delta_p2_mean_hi"]
    + ci_names("steady_cost_per_hour") + ci_names("evictions"))
TOST_COLUMNS = ["margin", "ci90_lo", "ci90_hi", "p_lower", "p_upper", "p_tost", "equivalent", "noninferior",
                "equivalent_holm"]
SCALE_DOWN_COLORS = (common.PALETTE[0], common.PALETTE[1], common.PALETTE[2])
ORACLE_SHADE = "#F2F2F2"


def add_ratio(df: pd.DataFrame) -> pd.DataFrame:
    """A copy of ``df`` with ``delta_p2_ratio`` = realized_delta_p2 / predicted_delta_p2 per run, NaN where the
    predicted change is zero or either side is missing."""
    out = df.copy()
    pred, real = _floats(out["predicted_delta_p2"]), _floats(out["realized_delta_p2"])
    ok = np.isfinite(pred) & np.isfinite(real) & (pred != 0)
    ratio = np.full(len(out), math.nan)
    ratio[ok] = real[ok] / pred[ok]
    out[RATIO] = ratio
    return out


def fidelity_table(df: pd.DataFrame, exp: dict[str, Any]) -> pd.DataFrame:
    """Exp 5 cells: one row per (scenario, nodes, label, placement, scale_down), over the cell's valid runs.

    ``fidelity_*``: mean and median of per-run fidelity with 95% bootstrap CIs, over the ``n_fidelity`` valid runs
    that tracked at least one evicted pod; ``fidelity_pooled``: all tracked pods' hit share (sum of hits over sum of
    hits and misses); ``off_plan_share_*`` = 1 - fidelity (mean and its CI). ``delta_p2_ratio_*``: mean and median of
    per-run realized / predicted P2 change over the ``n_ratio`` valid runs with a non-zero prediction;
    ``delta_p2_ratio_of_sums``: sum realized / sum predicted over every valid run with both values (the ratio of
    the cell's mean changes, so runs with a zero prediction still count), CI by resampling runs. Then the
    predicted and realized P2 change, steady-state $/hr and evictions. Sorted by registry order of label, placement
    and scale-down model.
    """
    params = exp["params"]
    label_order = list(exp["labels"]) + list(exp["baselines"])
    rows: list[dict[str, Any]] = []
    for scenario, nodes in _strata(df, exp["scenarios"]):
        part = _in_stratum(df, scenario, nodes)
        for label in _ordered(part["label"], label_order):
            lp = part.loc[part["label"] == label]
            for placement in _ordered(lp["placement"], params["placements"]):
                pp = lp.loc[lp["placement"] == placement]
                for scale_down in _ordered(pp["scale_down"], params["scale_downs"]):
                    cell = pp.loc[pp["scale_down"] == scale_down]
                    valid = cell.loc[cell["valid"].astype(bool)]
                    fid = _floats(valid["fidelity"])
                    hits, misses = _floats(valid["fidelity_hits"]), _floats(valid["fidelity_misses"])
                    tracked = np.nansum(hits) + np.nansum(misses)
                    row: dict[str, Any] = {"scenario": scenario, "nodes": nodes, "label": label,
                                           "placement": placement, "scale_down": scale_down, "n_runs": len(cell),
                                           "n_valid": len(valid), "n_invalid": len(cell) - len(valid),
                                           "n_fidelity": int(np.isfinite(fid).sum())}
                    row.update(ci_columns("fidelity", fid))
                    row["fidelity_pooled"] = float(np.nansum(hits) / tracked) if tracked > 0 else math.nan
                    row["off_plan_share_mean"] = 1 - row["fidelity_mean"]
                    row["off_plan_share_mean_lo"] = 1 - row["fidelity_mean_hi"]
                    row["off_plan_share_mean_hi"] = 1 - row["fidelity_mean_lo"]
                    ratio = _floats(valid[RATIO])
                    ok = np.isfinite(ratio)
                    row["n_ratio"] = int(ok.sum())
                    row.update(ci_columns(RATIO, ratio))
                    # Ratio of sums over every valid run with both values (zero predictions included: they add
                    # nothing to the denominator but their realized change still counts); the per-run ratios above
                    # need a non-zero prediction.
                    est, lo, hi = ratio_of_sums_ci(_floats(valid["realized_delta_p2"]),
                                                   _floats(valid["predicted_delta_p2"]))
                    row.update(delta_p2_ratio_of_sums=est, delta_p2_ratio_of_sums_lo=lo, delta_p2_ratio_of_sums_hi=hi)
                    for m in ("predicted_delta_p2", "realized_delta_p2"):
                        est, lo, hi = common.bootstrap_ci(_floats(valid[m]), "mean")
                        row.update({f"{m}_mean": est, f"{m}_mean_lo": lo, f"{m}_mean_hi": hi})
                    for m in ("steady_cost_per_hour", "evictions"):
                        row.update(ci_columns(m, valid[m]))
                    rows.append(row)
    return pd.DataFrame(rows, columns=FIDELITY_COLUMNS)


def fidelity_tests(df: pd.DataFrame, comps: Sequence[common.Comparison]) -> pd.DataFrame:
    """The preregistered Exp 5 tests: ``common.evaluate`` (paired two-sided exact Wilcoxon on fidelity, Holm over the
    10 tests of family H5; a run with no tracked eviction has undefined fidelity and is counted in n_undefined),
    TOST columns dropped. ``direction`` is the side the signed-rank statistic favours and ``outcome`` the common
    vocabulary (higher / lower when significant, no significant difference, inconclusive, no data)."""
    res = common.evaluate(df, comps)
    if res.empty:
        return res
    return res.drop(columns=[c for c in TOST_COLUMNS if c in res.columns]).reset_index(drop=True)


def _lever(tests: pd.DataFrame, comparison: str) -> dict[str, Any]:
    """A lever of H5 (b): whether ``comparison`` significantly raises fidelity in at least one stratum ("holds"),
    else "inconclusive" when one of its tests could not have been significant, else "fails"."""
    rows = tests.loc[tests["comparison"] == comparison] if len(tests) else tests
    outcomes = rows["outcome"].tolist() if len(rows) else []
    if "higher" in outcomes:
        state = "holds"
    elif not outcomes or any(o in common.UNINFORMATIVE_OUTCOMES for o in outcomes):
        state = "inconclusive"
    else:
        state = "fails"
    return {"comparison": comparison, "outcomes": outcomes, "state": state}


def h5_verdict(cells: pd.DataFrame, tests: pd.DataFrame, exp: dict[str, Any],
               missing: list[dict[str, Any]]) -> dict[str, Any]:
    """H5. (a) The off-plan share (1 - fidelity) of ``params.off_plan_label`` under ``off_plan_placement`` has a
    bootstrap 95% CI lower bound above ``off_plan_min_share`` under every scale-down model. (b) For
    ``params.lever_label``, most-allocated significantly raises fidelity in at least one scale-down stratum, and
    deletion-cost in at least one placement stratum (family H5). Supported iff (a) holds and both levers do; partial
    if (a) holds and one lever does; otherwise inconclusive when (a) holds and a lever is inconclusive (an
    unreachable test), else not supported. Also the oracle check: every plan-following cell's mean fidelity at least
    ``oracle_min_fidelity`` (else the run set is flagged as a simulator problem)."""
    params = exp["params"]
    label, placement = params["off_plan_label"], params["off_plan_placement"]
    off: list[dict[str, Any]] = []
    for scale_down in params["scale_downs"]:
        c = cells.loc[(cells["label"] == label) & (cells["placement"] == placement)
                      & (cells["scale_down"] == scale_down)]
        lo = float(c["off_plan_share_mean_lo"].iloc[0]) if len(c) else math.nan
        off.append({"scale_down": scale_down, "n_fidelity": int(c["n_fidelity"].iloc[0]) if len(c) else 0,
                    "off_plan_share_mean": float(c["off_plan_share_mean"].iloc[0]) if len(c) else math.nan,
                    "off_plan_share_mean_lo": lo,
                    "holds": bool(math.isfinite(lo) and lo > params["off_plan_min_share"])})
    a_holds = bool(off) and all(o["holds"] for o in off)
    lever = params["lever_label"]
    levers = {"most-allocated": _lever(tests, f"most- vs least-allocated, {lever}"),
              "deletion-cost": _lever(tests, f"deletion-cost vs spread, {lever}")}
    states = [v["state"] for v in levers.values()]
    if not a_holds:
        verdict = "not supported"
    elif states.count("holds") == 2:
        verdict = "supported"
    elif states.count("holds") == 1:
        verdict = "partial"
    elif "inconclusive" in states:
        verdict = "inconclusive"
    else:
        verdict = "not supported"
    oracle = cells.loc[cells["placement"] == params["oracle_placement"]]
    low = oracle.loc[~(oracle["fidelity_mean"].astype(float) >= float(params["oracle_min_fidelity"]))]
    return {
        "rule": "(a) bootstrap 95% CI lower bound of the off-plan share of random/entropy-greedy under least-allocated "
                "above 0.05 under both scale-down models; (b) for random/entropy-greedy, most-allocated significantly "
                "raises fidelity in at least one scale-down stratum and deletion-cost in at least one placement "
                "stratum (Wilcoxon, Holm over the 10 of H5); supported iff (a) and both levers, partial if (a) and one",
        "off_plan": off, "off_plan_holds": a_holds, "levers": levers,
        "verdict": "incomplete" if missing else verdict, "verdict_on_available": verdict, "complete": not missing,
        "missing_configurations": missing,
        "oracle": {"placement": params["oracle_placement"], "min_fidelity": params["oracle_min_fidelity"],
                   "n_cells": int(len(oracle)), "ok": bool(len(oracle)) and low.empty,
                   "flagged_cells": low[["label", "placement", "scale_down", "fidelity_mean"]]},
    }


def figure5(cells: pd.DataFrame, exp: dict[str, Any], out_root: Path) -> bool:
    """Figure 5: mean plan fidelity with 95% bootstrap CIs, grouped by placement model, one bar per scale-down model
    (colour), one panel per arm (and one row per (scenario, nodes) if there are several). The plan-following group
    sits on a shaded band as the oracle reference. Returns False (and writes nothing) without defined fidelity."""
    data = cells.loc[np.isfinite(cells["fidelity_mean"].to_numpy(dtype=float))]
    if data.empty:
        common.skipped("fig5_fidelity", "no runs with tracked evictions")
        return False
    params = exp["params"]
    oracle = params.get("oracle_placement")
    strata = list(dict.fromkeys(data[["scenario", "nodes"]].itertuples(index=False, name=None)))
    arms = _ordered(data["label"], list(exp["labels"]) + list(exp["baselines"]))
    scale_downs = _ordered(data["scale_down"], params["scale_downs"])
    colors = {sd: SCALE_DOWN_COLORS[i % len(SCALE_DOWN_COLORS)] for i, sd in enumerate(scale_downs)}
    placements_of = {arm: _ordered(data.loc[data["label"] == arm, "placement"], params["placements"]) for arm in arms}
    width = 0.8 / len(scale_downs)
    fig, axes = plt.subplots(len(strata), len(arms), sharey=True, squeeze=False,
                             figsize=(common.FIGSIZE["double"][0], 0.4 + 2.5 * len(strata)),
                             gridspec_kw={"width_ratios": [max(1, len(placements_of[a])) for a in arms]})
    for i, (scenario, nodes) in enumerate(strata):
        for j, arm in enumerate(arms):
            ax = axes[i][j]
            placements = placements_of[arm]
            part = _in_stratum(data, scenario, nodes)
            part = part.loc[part["label"] == arm]
            for x, placement in enumerate(placements):
                if placement == oracle:
                    ax.axvspan(x - 0.5, x + 0.5, color=ORACLE_SHADE, zorder=0, linewidth=0)
                for k, sd in enumerate(scale_downs):
                    r = part.loc[(part["placement"] == placement) & (part["scale_down"] == sd)]
                    if r.empty:
                        continue
                    r = r.iloc[0]
                    off = (k - (len(scale_downs) - 1) / 2) * width
                    ax.bar(x + off, r["fidelity_mean"], width * 0.9, color=colors[sd], zorder=2,
                           yerr=_err([r["fidelity_mean"]], [r["fidelity_mean_lo"]], [r["fidelity_mean_hi"]]),
                           error_kw={"elinewidth": 0.8, "capsize": 2, "ecolor": common.NEUTRAL, "zorder": 3})
            ax.set_xticks(range(len(placements)))
            ax.set_xticklabels([p.replace("-", "-\n", 1) + ("\n(oracle)" if p == oracle else "") for p in placements])
            ax.set_xlim(-0.5, len(placements) - 0.5)
            ax.set_ylim(0, 1.05)
            ax.grid(axis="x", visible=False)
            title = _wrap_label(arm, 22 if len(placements) > 1 else 14)
            ax.set_title(title if len(strata) == 1 else f"{title}\n{scenario}, {_fmt_nodes(nodes)} nodes")
            if j == 0:
                ax.set_ylabel(common.METRIC_LABELS["fidelity"])
    handles = [Patch(facecolor=colors[sd], label=sd) for sd in scale_downs]
    fig.legend(handles=handles, loc="outside lower center", ncol=len(handles), title="Scale-down model",
               title_fontsize=8)
    common.save_figure(fig, out_root, "fig5_fidelity")
    return True


def exp5_headline(everything: pd.DataFrame, cells: pd.DataFrame, tests: pd.DataFrame, exp: dict[str, Any],
                  missing: list[dict[str, Any]]) -> dict[str, Any]:
    """Exp 5 headline: validity counts, fidelity and off-plan share per cell (with CIs and the realized/predicted
    ratio of sums), every test with its floor, Holm p and outcome, and H5's verdict with the oracle check."""
    cell_cols = ["scenario", "nodes", "label", "placement", "scale_down", "n_fidelity", "fidelity_mean",
                 "fidelity_mean_lo", "fidelity_mean_hi", "off_plan_share_mean", "off_plan_share_mean_lo",
                 "off_plan_share_mean_hi", "delta_p2_ratio_of_sums", "delta_p2_ratio_of_sums_lo",
                 "delta_p2_ratio_of_sums_hi"]
    test_cols = ["comparison", "stratum", "n_pairs", "n_dropped", "n_dropped_invalid", "n_undefined", "mean_a",
                 "mean_b", "mean_diff", "mean_diff_lo", "mean_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi",
                 "p_wilcoxon", "p_floor", "m", "p_holm", "holm_reachable", "direction", "outcome", "expect_met"]
    outcomes = tests["outcome"].tolist() if len(tests) else []
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(everything), "cells": cells[cell_cols],
        "tests": tests[test_cols] if len(tests) else [],
        "outcome_counts": {o: outcomes.count(o) for o in sorted(set(outcomes))}, "n_tests": len(outcomes),
        "h5": h5_verdict(cells, tests, exp, missing),
    }


def run_exp5(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 5 tables (exp5_fidelity, exp5_tests) and figure 5; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP5)
    everything = common.select_runs(runs, EXP5, include_cut=True)
    if everything.empty:
        common.skipped(EXP5)
        return None
    sel = common.select_runs(runs, EXP5)
    df = add_ratio(sel)
    cells = fidelity_table(df, exp)
    common.write_table(cells, out_root, "exp5_fidelity")
    comps = common.comparisons(EXP5)
    tests = fidelity_tests(df, comps)
    common.write_table(tests, out_root, "exp5_tests")
    figure5(cells, exp, out_root)
    return exp5_headline(everything, cells, tests, exp, common.missing_configurations(everything, comps))


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Exp 6 frontier and dominance (figure 3) and Exp 5 plan fidelity (figure 5) from the raw runs."""
    runs = common.load_runs(runs_root, include_abandoned=True)
    headline: dict[str, Any] = {}
    for key, run in ((EXP6, run_exp6), (EXP5, run_exp5)):
        result = run(runs, out_root)
        if result is not None:
            headline[key] = result
    if headline:
        common.write_json(headline, out_root, "frontier_fidelity")
    else:
        common.skipped("frontier_fidelity", "no exp5 or exp6 runs")


if __name__ == "__main__":
    common.cli(main)
