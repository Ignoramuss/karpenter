"""Exp 7 (H7), decision latency vs cluster size, and Exp 8, the optimality gap to the offline packing bound (B6):
figures 4 and 7.

Exp 7 measures the wall time of a disruption pass for B2 (upstream WhenEmptyOrUnderutilized, prefix binary search),
heuristic/entropy-greedy and random/entropy-greedy on W1 at 50, 200 and 1,000 nodes (experiments.yaml ``exp7``: groups
exp7-n50, exp7-b2, exp7-b2-n1000, and Exp 1's exp1-n200 and exp1-n1000 cells). It tests H7 at 1,000 nodes: random vs
heuristic and random vs B2 on mean and p95 pass latency, paired by seed, Wilcoxon, Holm within Exp 7.

Exp 8 bounds each run's fleet cost from below with ``lower_bound`` (B6). At 50 nodes (groups exp8 and exp8-b5) every
podsets.jsonl snapshot gets the CP-SAT bound and the LP bound; at 200 and 1,000 nodes (Exp 1's groups, ``exp8.params``)
the final snapshot gets the LP bound. ``gap = cost_per_hour at the snapshot / bound - 1``.

Outputs under ``out_root`` (nothing is written for an experiment without runs):

- ``tables/exp7_latency.{csv,md}``: one row per (scenario, nodes, label): run counts, mean, p95 and max pass latency
  (ms), full simulations per pass and measured nodes (mean peak_nodes), each with the mean and median and their 95%
  bootstrap CIs over valid runs; passes over upstream's 1 and 3 minute timeouts and search timeouts (totals and runs
  affected).
- ``tables/exp7_tests.{csv,md}``: the preregistered H7 tests at 1,000 nodes (Holm within H7), with the paired effect,
  the speedup (reference mean / random mean), the direction and the outcome.
- ``tables/exp8_gap.{csv,md}``: one row per (nodes, scenario, label): which bound the row uses (cpsat at 50 nodes, lp
  at 200 and 1,000), the final-snapshot gap with CIs, the final gap against the LP bound, the mean gap over every
  snapshot (50 nodes), how many final bounds CP-SAT proved optimal and the bound's slack.
- ``tables/exp8_gap_snapshots.{csv,md}``: at 50 nodes, the mean gap with CIs per (scenario, label, snapshot time).
- ``tables/latency_optimality.json``: the headline numbers, validity counts and the H7 verdict.
- ``figures/out/fig4_latency.{png,pdf}`` and ``figures/out/fig7_optimality_gap.{png,pdf}``.
- ``tables/cache/lower_bounds.json``: the bound cache (``lower_bound.BoundCache``), derived and safe to delete.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.latency_optimality [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullLocator
from scipy import stats

from paper.analysis import common
from paper.analysis import lower_bound as lb

EXP7 = "exp7"
EXP8 = "exp8"
ALPHA = common.ALPHA

# --------------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------------


def _floats(values: Any) -> np.ndarray:
    """``values`` as a float array; missing and non-numeric entries become NaN (nothing is dropped)."""
    return pd.to_numeric(pd.Series(values, dtype=object), errors="coerce").to_numpy(dtype=float)


def _ordered(values: Iterable[Any], preferred: Sequence[Any]) -> list[Any]:
    """The distinct ``values``: those in ``preferred`` first, in its order, then the rest sorted as strings."""
    present = {v for v in values if not (isinstance(v, float) and math.isnan(v))}
    head = [v for v in preferred if v in present]
    return head + sorted(present - set(head), key=str)


def _fmt_nodes(nodes: Any) -> str:
    try:
        return f"{float(nodes):,.0f}"
    except (TypeError, ValueError):
        return str(nodes)


def ci_names(prefix: str) -> list[str]:
    return [f"{prefix}_{s}" for s in ("mean", "mean_lo", "mean_hi", "median", "median_lo", "median_hi")]


def ci_columns(prefix: str, values: Any) -> dict[str, float]:
    """``<prefix>_mean``, ``_mean_lo``, ``_mean_hi``, ``_median``, ``_median_lo``, ``_median_hi`` of the finite
    ``values``: the estimates and their 95% percentile bootstrap intervals (``common.bootstrap_ci``, seed 0)."""
    x = _floats(values)
    mean, lo, hi = common.bootstrap_ci(x, "mean")
    med, mlo, mhi = common.bootstrap_ci(x, "median")
    return dict(zip(ci_names(prefix), (mean, lo, hi, med, mlo, mhi)))


def _cells(df: pd.DataFrame, by: Sequence[str], label_order: Sequence[str],
           scenario_order: Sequence[str]) -> list[tuple[Any, ...]]:
    """The distinct ``by`` tuples of ``df`` (by = scenario, nodes, label in some order), sorted by scenario in
    registry order, nodes ascending, then label in registry order."""
    tuples = {tuple(t) for t in df[list(by)].itertuples(index=False, name=None)}
    scen = {s: i for i, s in enumerate(_ordered(df["scenario"], scenario_order))}
    labs = {s: i for i, s in enumerate(_ordered(df["label"], label_order))}

    def key(t: tuple[Any, ...]) -> tuple[Any, ...]:
        row = dict(zip(by, t))
        return tuple((scen.get(row[c], len(scen)), str(row[c])) if c == "scenario"
                     else (labs.get(row[c], len(labs)), str(row[c])) if c == "label"
                     else (float(row[c]), "") for c in by)

    return sorted(tuples, key=key)


def _in_cell(df: pd.DataFrame, by: Sequence[str], values: Sequence[Any]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, v in zip(by, values):
        mask &= df[col] == v
    return df.loc[mask]


def _err(mean: Any, lo: Any, hi: Any) -> np.ndarray:
    """matplotlib's asymmetric error-bar array [[below], [above]] from estimates and interval ends (NaN -> 0)."""
    mean, lo, hi = (np.asarray(v, dtype=float) for v in (mean, lo, hi))
    below = np.nan_to_num(np.clip(mean - lo, 0, None))
    above = np.nan_to_num(np.clip(hi - mean, 0, None))
    return np.vstack([below, above])


def rank_direction(diffs: Any) -> int:
    """The direction the signed-rank test sees in paired differences: +1 when the positive differences carry more
    rank weight than the negative ones (W+ > W-), -1 for the reverse, 0 for a tie or no non-zero differences."""
    d = _floats(diffs)
    d = d[np.isfinite(d) & (d != 0)]
    if d.size == 0:
        return 0
    ranks = stats.rankdata(np.abs(d))
    return int(np.sign(ranks[d > 0].sum() - ranks[d < 0].sum()))


def _validity(sel: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-group run counts and invalid reasons (abandoned cells included) for the JSON headline."""
    return common.to_jsonable(common.validity_report(sel, by=("group",)))


def _counts(part: pd.DataFrame) -> dict[str, int]:
    valid = part["valid"].astype(bool)
    return {"n_runs": len(part), "n_valid": int(valid.sum()), "n_invalid": int((~valid).sum())}


# --------------------------------------------------------------------------------------------------------------------
# Exp 7: decision latency vs cluster size
# --------------------------------------------------------------------------------------------------------------------

LATENCY_METRICS = ("wall_pass_mean_ms", "wall_pass_p95_ms", "wall_pass_max_ms")
SIMS_PER_PASS = "full_simulations_per_pass"
SIZE = "peak_nodes"
# Upstream's consolidation timeouts: multi-node 1 minute, single-node 3 minutes (in ms), with the summary column that
# counts passes over each.
TIMEOUTS = (("1m", "wall_passes_over_1m", 60_000.0), ("3m", "wall_passes_over_3m", 180_000.0))
EXPECT_SIGN = {"lower": -1, "higher": 1}
LATENCY_COLUMNS = (["scenario", "nodes", "label", "n_runs", "n_valid", "n_invalid", "duration_min"]
                   + [c for m in (*LATENCY_METRICS, SIMS_PER_PASS, SIZE) for c in ci_names(m)]
                   + ["disruption_passes_mean"]
                   + [c for name, _, _ in TIMEOUTS for c in (f"passes_over_{name}_total", f"runs_over_{name}",
                                                             f"runs_over_{name}_unknown")]
                   + ["search_timeouts_total", "runs_with_search_timeouts"])
TOST_COLUMNS = ("margin", "ci90_lo", "ci90_hi", "p_lower", "p_upper", "p_tost", "equivalent", "noninferior",
                "equivalent_holm")


def add_latency_columns(df: pd.DataFrame) -> pd.DataFrame:
    """``df`` with the derived Exp 7 columns.

    - ``full_simulations_per_pass``: full_simulations / disruption_passes; NaN for baseline arms (``arm_name`` not
      "pipeline"), which do not count full simulations, and when there were no passes.
    - ``passes_over_1m`` / ``passes_over_3m``: wall_passes_over_1m / _3m where the run reports them; for older runs
      that do not, 0 when wall_pass_max_ms shows no pass reached the limit, else NaN (some pass exceeded it, how many
      is unknown).
    """
    out = df.copy()
    sims, passes = _floats(out["full_simulations"]), _floats(out["disruption_passes"])
    pipeline = (out["arm_name"].astype(str) == "pipeline").to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(pipeline & (passes > 0), sims / passes, np.nan)
    out[SIMS_PER_PASS] = ratio
    worst = _floats(out["wall_pass_max_ms"])
    for name, col, limit_ms in TIMEOUTS:
        reported = _floats(out[col]) if col in out.columns else np.full(len(out), np.nan)
        derived = np.where(worst <= limit_ms, 0.0, np.nan)
        out[f"passes_over_{name}"] = np.where(np.isfinite(reported), reported, derived)
    return out


def latency_table(df: pd.DataFrame, exp: dict[str, Any]) -> pd.DataFrame:
    """Exp 7 per-cell table (``LATENCY_COLUMNS``): one row per (scenario, nodes, label), statistics over valid runs.
    ``passes_over_<t>_total`` sums the known counts of passes over the timeout (``add_latency_columns``);
    ``runs_over_<t>`` counts valid runs with at least one such pass (a known count above 0, or an unknown count with
    the slowest pass over the limit); ``runs_over_<t>_unknown`` counts runs whose number of such passes is unknown."""
    by = ("scenario", "nodes", "label")
    rows = []
    for values in _cells(df, by, list(exp["labels"]) + list(exp["baselines"]), exp["scenarios"]):
        part = _in_cell(df, by, values)
        valid = part.loc[part["valid"].astype(bool)]
        row: dict[str, Any] = dict(zip(by, values))
        row.update(_counts(part))
        duration, passes = _floats(valid["duration_min"]), _floats(valid["disruption_passes"])
        row["duration_min"] = float(np.median(duration[np.isfinite(duration)])) if np.isfinite(duration).any() \
            else math.nan
        for m in (*LATENCY_METRICS, SIMS_PER_PASS, SIZE):
            row.update(ci_columns(m, valid[m]))
        row["disruption_passes_mean"] = float(passes[np.isfinite(passes)].mean()) if np.isfinite(passes).any() \
            else math.nan
        for name, _, limit_ms in TIMEOUTS:
            over = _floats(valid[f"passes_over_{name}"])
            row[f"passes_over_{name}_total"] = float(np.nansum(over)) if len(over) else math.nan
            worst = _floats(valid["wall_pass_max_ms"])
            row[f"runs_over_{name}"] = int(np.sum(np.isfinite(over) & (over > 0))
                                           + np.sum(~np.isfinite(over) & (worst > limit_ms)))
            row[f"runs_over_{name}_unknown"] = int(np.sum(~np.isfinite(over)))
        st = _floats(valid["search_timeouts"])
        row["search_timeouts_total"] = float(np.nansum(st)) if np.isfinite(st).any() else math.nan
        row["runs_with_search_timeouts"] = int(np.sum(np.isfinite(st) & (st > 0)))
        rows.append(row)
    return pd.DataFrame(rows, columns=LATENCY_COLUMNS)


def latency_tests(df: pd.DataFrame, comps: Sequence[common.Comparison]) -> pd.DataFrame:
    """The preregistered Exp 7 comparisons (``common.evaluate``: paired by scenario, nodes and seed, invalid seeds
    dropped pair-wise, Holm within H7), without the TOST columns, plus ``speedup`` (mean_b / mean_a: how many times
    faster a is), ``direction`` (of a - b as the signed-rank test sees it: lower, higher, none) and ``outcome``:
    "as expected" (p_holm < 0.05 in the expected direction), "opposite", "not significant" or "no pairs"."""
    res = common.evaluate(df, comps)
    if res.empty:
        return res
    by_name = {c.name: c for c in comps}
    directions, outcomes = [], []
    for _, row in res.iterrows():
        c = by_name[row["comparison"]]
        part = df
        for col, allowed in c.where.items():
            part = part.loc[part[col].isin(allowed)]
        for col in c.by:
            part = part.loc[part[col] == row[col]]
        p = common.paired_lenient(part, c.keys, c.label_col, c.a, c.b, c.metric, log=False)
        sign = rank_direction(p.a - p.b)
        directions.append({1: "higher", -1: "lower", 0: "none"}[sign])
        want = EXPECT_SIGN.get(c.expect or "")
        if not row["n_pairs"]:
            outcomes.append("no pairs")
        elif not row["significant"]:
            outcomes.append("not significant")
        elif want is None:
            outcomes.append("significant")
        else:
            outcomes.append("as expected" if sign == want else "opposite")
    res = res.drop(columns=[c for c in TOST_COLUMNS if c in res.columns])
    with np.errstate(divide="ignore", invalid="ignore"):
        res["speedup"] = _floats(res["mean_b"]) / _floats(res["mean_a"])
    res["direction"] = directions
    res["outcome"] = outcomes
    return res.reset_index(drop=True)


def figure4(table: pd.DataFrame, exp: dict[str, Any], out_root: Path) -> bool:
    """Figure 4: decision latency vs measured cluster size, log-log. Left panel the mean pass latency, right the p95,
    each point the mean over valid runs with 95% bootstrap CIs (vertical: latency; horizontal: mean peak nodes), one
    series per configuration, with upstream's 60 s and 180 s consolidation timeouts as reference lines. Series are
    dodged sideways by a few percent (multiplicatively) so configurations at the same size stay apart. One row of
    panels per scenario. Returns False (writes nothing) when no cell has a latency."""
    metrics = ("wall_pass_mean_ms", "wall_pass_p95_ms")
    data = table.loc[np.isfinite(_floats(table["wall_pass_mean_ms_mean"]))
                     & np.isfinite(_floats(table[f"{SIZE}_mean"]))]
    if data.empty:
        common.skipped("fig4_latency", "no runs with pass latencies")
        return False
    scenarios = _ordered(data["scenario"], exp["scenarios"])
    labels = _ordered(data["label"], list(exp["labels"]) + list(exp["baselines"]))
    width, height = common.FIGSIZE["double"]
    fig, axes = plt.subplots(len(scenarios), len(metrics), sharex=True, sharey=True, squeeze=False,
                             figsize=(width, 0.5 + (height - 0.5) * len(scenarios)))
    lows = [v for m in metrics for v in _floats(data[f"{m}_mean_lo"]) if np.isfinite(v) and v > 0]
    y_lo = 10 ** math.floor(math.log10(min(lows) / 2)) if lows else 1.0
    # Room above the 180 s line, and above every interval should a configuration be slower than that.
    highs = [v for m in metrics for v in _floats(data[f"{m}_mean_hi"]) if np.isfinite(v) and v > 0]
    y_hi = max(6e5, 10 ** math.ceil(math.log10(2 * max(highs)))) if highs else 6e5
    sizes = sorted({float(n) for n in data["nodes"]})
    handles: dict[str, Any] = {}
    for i, scenario in enumerate(scenarios):
        for j, metric in enumerate(metrics):
            ax = axes[i][j]
            part = data.loc[data["scenario"] == scenario]
            for k, label in enumerate(labels):
                r = part.loc[part["label"] == label].sort_values(f"{SIZE}_mean")
                if r.empty:
                    continue
                st = common.style_for(label, k)
                x, y = _floats(r[f"{SIZE}_mean"]), _floats(r[f"{metric}_mean"])
                dodge = math.exp((k - (len(labels) - 1) / 2) * 0.04)
                h = ax.errorbar(x * dodge, y, yerr=_err(y, r[f"{metric}_mean_lo"], r[f"{metric}_mean_hi"]),
                                xerr=_err(x, r[f"{SIZE}_mean_lo"], r[f"{SIZE}_mean_hi"]) * dodge, color=st["color"],
                                linestyle=st["linestyle"], marker=st["marker"], markersize=4, capsize=2,
                                elinewidth=0.8, linewidth=1.2, label=label)
                handles.setdefault(label, h)
            for (name, _, limit_ms), text in zip(TIMEOUTS, ("60 s: upstream multi-node timeout",
                                                            "180 s: upstream single-node timeout")):
                ax.axhline(limit_ms, color=common.NEUTRAL, linewidth=0.8, linestyle=":", zorder=1)
                ax.annotate(text, xy=(0.02, limit_ms), xycoords=("axes fraction", "data"), xytext=(0, 2),
                            textcoords="offset points", fontsize=7, color=common.NEUTRAL, va="bottom")
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_ylim(y_lo, y_hi)
            ax.xaxis.set_major_locator(FixedLocator(sizes))
            ax.xaxis.set_minor_locator(NullLocator())
            ax.set_xticklabels([_fmt_nodes(n) for n in sizes])
            ax.set_xlim(min(sizes) / 1.5, max(sizes) * 1.5)
            title = "Mean pass latency" if metric == "wall_pass_mean_ms" else "p95 pass latency"
            ax.set_title(title if len(scenarios) == 1 else f"{title}, {scenario}")
            if i == len(scenarios) - 1:
                ax.set_xlabel("Measured nodes (mean peak)")
            if j == 0:
                ax.set_ylabel("Pass latency (ms)")
    legend = [handles[label] for label in labels if label in handles]
    fig.legend(handles=legend, loc="outside lower center", ncol=min(len(legend), 3))
    common.save_figure(fig, out_root, "fig4_latency")
    return True


def exp7_headline(sel: pd.DataFrame, table: pd.DataFrame, tests: pd.DataFrame, exp: dict[str, Any]) -> dict[str, Any]:
    """Exp 7 headline: validity counts, the per-cell latency table, every test with its Holm p and outcome, the
    timeout counts at the tested size, and the H7 verdict.

    The verdict ("supported", "not supported", "incomplete") follows PREREGISTRATION.md: H7 is supported when all four
    tests at 1,000 nodes are significant after Holm with random/entropy-greedy lower, and no valid random/entropy-greedy
    run at 1,000 nodes has a pass over upstream's 1-minute timeout (hence none over 3 minutes either). "incomplete"
    when a test is missing or has no pairs, or when no run is known to exceed the limit but some run's count cannot be
    told."""
    cols = ["scenario", "nodes", "label", "n_runs", "n_valid", "n_invalid", "wall_pass_mean_ms_mean",
            "wall_pass_mean_ms_mean_lo", "wall_pass_mean_ms_mean_hi", "wall_pass_p95_ms_mean",
            "wall_pass_p95_ms_mean_lo", "wall_pass_p95_ms_mean_hi", "wall_pass_max_ms_mean",
            "full_simulations_per_pass_mean", "peak_nodes_mean", "passes_over_1m_total", "passes_over_3m_total",
            "runs_over_1m", "runs_over_1m_unknown", "runs_over_3m", "runs_over_3m_unknown", "search_timeouts_total"]
    test_cols = ["comparison", "metric", "a", "b", "stratum", "n_pairs", "n_dropped", "n_dropped_invalid",
                 "n_dropped_missing", "mean_a", "mean_b", "mean_diff", "mean_diff_lo", "mean_diff_hi", "pct_diff",
                 "pct_diff_lo", "pct_diff_hi", "speedup", "p_wilcoxon", "p_holm", "direction", "outcome"]
    comps = common.comparisons(EXP7)
    tested_nodes = sorted({float(n) for c in comps for n in c.where.get("nodes", [])})
    subject = sorted({c.a for c in comps if isinstance(c.a, str)})
    at_size = table.loc[table["nodes"].astype(float).isin(tested_nodes) & table["label"].isin(subject)]
    timeouts = {name: {"passes_total": float(at_size[f"passes_over_{name}_total"].sum()) if len(at_size) else None,
                       "runs_over": int(at_size[f"runs_over_{name}"].sum()),
                       "runs_unknown": int(at_size[f"runs_over_{name}_unknown"].sum())}
                for name, _, _ in TIMEOUTS}
    outcomes = tests["outcome"].tolist() if len(tests) else []
    n_expected = len(comps)
    if len(outcomes) < n_expected or "no pairs" in outcomes or at_size.empty:
        verdict = "incomplete"
    elif all(o == "as expected" for o in outcomes) and timeouts["1m"]["runs_over"] == 0:
        verdict = "incomplete" if timeouts["1m"]["runs_unknown"] else "supported"
    else:
        verdict = "not supported"
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(sel), "cells": table[cols],
        "tests": tests[test_cols] if len(tests) else [], "tested_nodes": tested_nodes, "subject": subject,
        "timeouts_at_tested_nodes": timeouts,
        "outcome_counts": {o: outcomes.count(o) for o in sorted(set(outcomes))}, "n_tests": len(outcomes),
        "verdict": verdict,
    }


def run_exp7(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 7 tables (exp7_latency, exp7_tests) and figure 4; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP7)
    sel = common.select_runs(runs, EXP7)
    if sel.empty:
        common.skipped(EXP7)
        return None
    df = add_latency_columns(sel)
    table = latency_table(df, exp)
    common.write_table(table, out_root, "exp7_latency")
    tests = latency_tests(df, common.comparisons(EXP7))
    if len(tests):
        common.write_table(tests, out_root, "exp7_tests")
    else:
        common.skipped("exp7_tests", "no runs at the tested size")
    figure4(table, exp, out_root)
    return exp7_headline(sel, table, tests, exp)


# --------------------------------------------------------------------------------------------------------------------
# Exp 8: optimality gap to the offline packing bound (B6)
# --------------------------------------------------------------------------------------------------------------------

CPSAT, LP = "cpsat", "lp"
BOUND_NAMES = {CPSAT: "CP-SAT", LP: "LP"}
SNAPSHOT_KEYS = ["run_id", "scenario", "nodes", "label", "seed", "group"]
GAP_COLUMNS = (["nodes", "scenario", "label", "bound", "n_runs", "n_valid", "n_invalid", "n_with_podsets",
                "final_t_min", "n_snapshots_mean"]
               + ci_names("cost_final") + ci_names("bound_final") + ci_names("gap_final") + ci_names("gap_lp_final")
               + ci_names("gap_all") + ["n_final_proven_optimal", "n_final_unbounded", "bound_slack_final_mean",
                                        "bound_slack_final_max"])
SNAPSHOT_COLUMNS = ["scenario", "nodes", "label", "t_min", "n_runs"] + ci_names("gap") + ci_names("gap_lp")


def snapshots(df: pd.DataFrame, final_only: bool) -> pd.DataFrame:
    """One row per podsets.jsonl snapshot of the valid runs in ``df`` (only the last one per run with ``final_only``):
    ``SNAPSHOT_KEYS``, t_min, cost_per_hour, snapshot_nodes, shapes (normalized tuples), final (largest t_min of
    the run). Invalid and abandoned runs are not read. Sorted by run_id and t_min."""
    rows = []
    valid = df.loc[df["valid"].astype(bool)].sort_values("run_id", kind="stable")
    for rec in valid.to_dict(orient="records"):
        snaps = common.load_podsets(rec["path"])
        if not snaps:
            continue
        last = max(float(s["t_min"]) for s in snaps)
        for s in snaps:
            t = float(s["t_min"])
            if final_only and t != last:
                continue
            row = {k: rec[k] for k in SNAPSHOT_KEYS}
            row.update(t_min=t, cost_per_hour=float(s.get("cost_per_hour", math.nan)),
                       snapshot_nodes=s.get("nodes"), shapes=lb.normalize_shapes(s["shapes"]), final=t == last)
            rows.append(row)
    cols = SNAPSHOT_KEYS + ["t_min", "cost_per_hour", "snapshot_nodes", "shapes", "final"]
    return pd.DataFrame(rows, columns=cols)


def attach_bounds(snaps: pd.DataFrame, entries: dict[str, dict[str, Any]], types: Sequence[lb.InstanceType],
                  bound: str) -> pd.DataFrame:
    """``snaps`` with each snapshot's bounds and gaps: lp_bound, cpsat_bound, cpsat_objective, cpsat_status,
    bound_used (cpsat_bound for ``bound`` "cpsat", lp_bound for "lp"), gap (cost / bound_used - 1), gap_lp (cost /
    lp_bound - 1) and bound_slack (cpsat_objective / cpsat_bound - 1: how far the CP-SAT bound may sit below the
    optimum; 0 when proven optimal). A gap is NaN when its bound is missing or not positive."""
    out = snaps.copy()
    keys = [lb.instance_key(s, types) for s in out["shapes"]]
    for field in ("lp_bound", "cpsat_bound", "cpsat_objective"):
        out[field] = _floats([entries[k].get(field, math.nan) for k in keys])
    out["cpsat_status"] = [str(entries[k].get("cpsat_status", "")) for k in keys]
    used = out["cpsat_bound"] if bound == CPSAT else out["lp_bound"]
    cost = _floats(out["cost_per_hour"])
    with np.errstate(divide="ignore", invalid="ignore"):
        b = _floats(used)
        out["bound_used"] = b
        out["gap"] = np.where(b > 0, cost / b - 1, np.nan)
        lpb = _floats(out["lp_bound"])
        out["gap_lp"] = np.where(lpb > 0, cost / lpb - 1, np.nan)
        cb, co = _floats(out["cpsat_bound"]), _floats(out["cpsat_objective"])
        out["bound_slack"] = np.where(cb > 0, co / cb - 1, np.nan)
    out["bound"] = bound
    return out


def gap_table(df: pd.DataFrame, snaps: pd.DataFrame, exp: dict[str, Any], label_order: Sequence[str]) -> pd.DataFrame:
    """Exp 8 per-cell table (``GAP_COLUMNS``), one row per (nodes, scenario, label) of ``df`` (the runs, for counts),
    with statistics over valid runs that have snapshots: final-snapshot cost, bound and gap (against the row's bound)
    and gap against the LP bound, each the mean and median with 95% bootstrap CIs over runs; ``gap_all``: per run the
    mean gap over all its snapshots, then the same statistics over runs (CP-SAT rows only); the number of final bounds
    CP-SAT proved optimal, final snapshots without a usable bound, and the mean and largest bound slack."""
    by = ("scenario", "nodes", "label")
    rows = []
    for values in _cells(df, by, label_order, exp["scenarios"]):
        part = _in_cell(df, by, values)
        s = _in_cell(snaps, by, values) if len(snaps) else snaps
        final = s.loc[s["final"].astype(bool)] if len(s) else s
        bound = str(final["bound"].iloc[0]) if len(final) else (CPSAT if float(values[1]) in
                                                                 _cpsat_nodes(exp) else LP)
        row: dict[str, Any] = dict(zip(by, values))
        row["bound"] = bound
        row.update(_counts(part))
        row["n_with_podsets"] = int(final["run_id"].nunique()) if len(final) else 0
        row["final_t_min"] = float(np.median(_floats(final["t_min"]))) if len(final) else math.nan
        row["n_snapshots_mean"] = float(s.groupby("run_id").size().mean()) if len(s) else math.nan
        row.update(ci_columns("cost_final", final["cost_per_hour"] if len(final) else []))
        row.update(ci_columns("bound_final", final["bound_used"] if len(final) else []))
        row.update(ci_columns("gap_final", final["gap"] if len(final) else []))
        row.update(ci_columns("gap_lp_final", final["gap_lp"] if len(final) else []))
        per_run = s.groupby("run_id", sort=True)["gap"].mean() if (len(s) and bound == CPSAT) else []
        row.update(ci_columns("gap_all", per_run))
        row["n_final_proven_optimal"] = int((final["cpsat_status"] == "OPTIMAL").sum()) if len(final) else 0
        row["n_final_unbounded"] = int((~np.isfinite(_floats(final["gap"]))).sum()) if len(final) else 0
        slack = _floats(final["bound_slack"]) if (len(final) and bound == CPSAT) else np.array([])
        slack = slack[np.isfinite(slack)]
        row["bound_slack_final_mean"] = float(slack.mean()) if slack.size else math.nan
        row["bound_slack_final_max"] = float(slack.max()) if slack.size else math.nan
        rows.append(row)
    out = pd.DataFrame(rows, columns=GAP_COLUMNS)
    if len(out):
        out = out.sort_values(["nodes"], kind="stable").reset_index(drop=True)
    return out


def snapshot_table(snaps: pd.DataFrame, exp: dict[str, Any], label_order: Sequence[str]) -> pd.DataFrame:
    """Per (scenario, nodes, label, t_min) at the CP-SAT scale: runs and the mean and median gap (CP-SAT and LP
    bounds) with 95% bootstrap CIs over runs (``SNAPSHOT_COLUMNS``)."""
    rows = []
    if snaps.empty:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    by = ("scenario", "nodes", "label", "t_min")
    for values in _cells(snaps, by, label_order, exp["scenarios"]):
        part = _in_cell(snaps, by, values)
        row: dict[str, Any] = dict(zip(by, values))
        row["n_runs"] = int(part["run_id"].nunique())
        row.update(ci_columns("gap", part["gap"]))
        row.update(ci_columns("gap_lp", part["gap_lp"]))
        rows.append(row)
    return pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)


def _cpsat_nodes(exp: dict[str, Any]) -> set[float]:
    return {float(n) for n in exp["params"].get("cp_sat_nodes", exp["nodes"])}


def figure7(table: pd.DataFrame, exp: dict[str, Any], label_order: Sequence[str], out_root: Path) -> bool:
    """Figure 7: mean final-snapshot optimality gap (%) by scale, one panel per scenario, one series per
    configuration, 95% bootstrap CIs. Filled markers use the CP-SAT bound (50 nodes), hollow markers the LP bound
    (200 and 1,000 nodes); a series is dotted where it crosses from one bound to the other. Points are dodged
    sideways per configuration so the intervals stay apart. Returns False (writes nothing) without any gap."""
    data = table.loc[np.isfinite(_floats(table["gap_final_mean"]))]
    if data.empty:
        common.skipped("fig7_optimality_gap", "no runs with podsets and a bound")
        return False
    scenarios = _ordered(data["scenario"], exp["scenarios"])
    labels = _ordered(data["label"], label_order)
    scales = sorted({float(n) for n in data["nodes"]})
    bound_of = {float(n): b for n, b in data[["nodes", "bound"]].itertuples(index=False, name=None)}
    width, height = common.FIGSIZE["double"]
    fig, axes = plt.subplots(1, len(scenarios), sharey=True, squeeze=False, figsize=(width, height + 0.4))
    handles: dict[str, Any] = {}
    for j, scenario in enumerate(scenarios):
        ax = axes[0][j]
        part = data.loc[data["scenario"] == scenario]
        for k, label in enumerate(labels):
            r = part.loc[part["label"] == label].sort_values("nodes")
            if r.empty:
                continue
            st = common.style_for(label, k)
            dodge = math.exp((k - (len(labels) - 1) / 2) * 0.045)
            x = _floats(r["nodes"]) * dodge
            y = 100 * _floats(r["gap_final_mean"])
            err = 100 * _err(_floats(r["gap_final_mean"]), r["gap_final_mean_lo"], r["gap_final_mean_hi"])
            kinds = r["bound"].tolist()
            for a in range(len(r) - 1):
                ax.plot(x[a:a + 2], y[a:a + 2], color=st["color"], linewidth=1.0,
                        linestyle=st["linestyle"] if kinds[a] == kinds[a + 1] else ":")
            for a in range(len(r)):
                filled = kinds[a] == CPSAT
                ax.errorbar(x[a:a + 1], y[a:a + 1], yerr=err[:, a:a + 1], color=st["color"], linestyle="none",
                            marker=st["marker"], markersize=4.5, capsize=2, elinewidth=0.8,
                            markerfacecolor=st["color"] if filled else "white", markeredgecolor=st["color"])
            handles.setdefault(label, Line2D([], [], color=st["color"], linestyle=st["linestyle"],
                                             marker=st["marker"], markersize=4.5, label=label))
        ax.axhline(0, color=common.NEUTRAL, linewidth=0.6, zorder=1)
        ax.set_xscale("log")
        ax.xaxis.set_major_locator(FixedLocator(scales))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticklabels([f"{_fmt_nodes(n)}\n({BOUND_NAMES.get(bound_of[n], bound_of[n])})" for n in scales])
        ax.set_xlim(min(scales) / 1.6, max(scales) * 1.6)
        ax.set_title(scenario)
        ax.set_xlabel("Scale (scenario nodes)")
        if j == 0:
            ax.set_ylabel("Gap to lower bound (%)")
    legend = [handles[label] for label in labels if label in handles]
    fig.legend(handles=legend, loc="outside lower center", ncol=min(len(legend), 4))
    key = [Line2D([], [], color=common.NEUTRAL, linestyle="none", marker="o", markersize=4.5,
                  markerfacecolor=common.NEUTRAL, label="CP-SAT bound"),
           Line2D([], [], color=common.NEUTRAL, linestyle="none", marker="o", markersize=4.5,
                  markerfacecolor="white", label="LP bound"),
           Line2D([], [], color=common.NEUTRAL, linestyle=":", linewidth=1.0, label="bound changes")]
    fig.legend(handles=key, loc="outside right upper")
    common.save_figure(fig, out_root, "fig7_optimality_gap")
    return True


def exp8_headline(sel: pd.DataFrame, lp_sel: pd.DataFrame, table: pd.DataFrame, snaps: pd.DataFrame,
                  entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Exp 8 headline: validity counts (50-node groups and the LP-scale groups), the per-cell gap table, and how the
    bounds came out: distinct multisets, CP-SAT statuses, CP-SAT bounds below the LP bound (should be none), searches
    the wall-clock safety limit cut short (should be none: their results may not reproduce), and at the CP-SAT scale
    the mean ratio of the CP-SAT to the LP bound over final snapshots (how much the LP bound, used at the larger
    scales, understates the bound)."""
    cols = ["nodes", "scenario", "label", "bound", "n_runs", "n_valid", "n_invalid", "n_with_podsets",
            "gap_final_mean", "gap_final_mean_lo", "gap_final_mean_hi", "gap_final_median", "gap_lp_final_mean",
            "gap_all_mean", "n_final_proven_optimal", "n_final_unbounded", "bound_slack_final_mean"]
    statuses: dict[str, int] = {}
    below = wall_limited = 0
    for e in entries.values():
        st = str(e.get("cpsat_status", ""))
        statuses[st] = statuses.get(st, 0) + 1
        wall_limited += bool(e.get("cpsat_wall_limited"))
        cb, lpb = float(e.get("cpsat_bound", math.nan)), float(e.get("lp_bound", math.nan))
        if math.isfinite(cb) and math.isfinite(lpb) and cb < lpb - 1e-9:
            below += 1
    ratio = math.nan
    if len(snaps):
        f = snaps.loc[snaps["final"].astype(bool) & (snaps["bound"] == CPSAT)]
        r = _floats(f["cpsat_bound"]) / _floats(f["lp_bound"])
        r = r[np.isfinite(r)]
        ratio = float(r.mean()) if r.size else math.nan
    return {
        "hypothesis": None, "validity": _validity(sel), "validity_lp_scales": _validity(lp_sel),
        "cells": table[cols] if len(table) else [],
        "bounds": {"n_multisets": len(entries), "cpsat_status_counts": dict(sorted(statuses.items())),
                   "cpsat_bound_below_lp": below, "cpsat_wall_limited": wall_limited,
                   "mean_cpsat_over_lp_final": ratio,
                   "cpsat_params": dict(lb.CPSAT_PARAMS), "cpsat_max_pods": lb.CPSAT_MAX_PODS,
                   "model_version": lb.MODEL_VERSION},
    }


def run_exp8(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 8 tables (exp8_gap, exp8_gap_snapshots) and figure 7; returns the JSON headline, or None when skipped.

    The CP-SAT scale (``params.cp_sat_nodes``) comes from the experiment's own groups; the LP scales
    (``params.lp_nodes``) from ``params.lp_groups`` (Exp 1's groups, remapped like any group) on the experiment's
    scenarios. Either part may be missing; with neither, the experiment is skipped."""
    exp = common.experiment(EXP8)
    params = exp["params"]
    sel = common.select_runs(runs, EXP8)
    sel = sel.loc[sel["nodes"].astype(float).isin(_cpsat_nodes(exp))] if len(sel) else sel
    lp_sel = runs.iloc[0:0]
    if len(runs):
        mask = (runs["group"].isin(common.map_groups(params.get("lp_groups", [])))
                & runs["nodes"].astype(float).isin([float(n) for n in params.get("lp_nodes", [])])
                & runs["scenario"].isin(exp["scenarios"]))
        lp_sel = runs.loc[mask].copy()
    if sel.empty and lp_sel.empty:
        common.skipped(EXP8)
        return None
    if sel.empty:
        common.skipped(f"{EXP8} at {', '.join(_fmt_nodes(n) for n in sorted(_cpsat_nodes(exp)))} nodes (CP-SAT)")
    if lp_sel.empty:
        common.skipped(f"{EXP8} at {', '.join(_fmt_nodes(n) for n in params.get('lp_nodes', []))} nodes (LP)")
    types = lb.catalog_types(common.load_catalog())
    cache = lb.BoundCache(lb.cache_path(out_root))
    snaps_cp = snapshots(sel, final_only=False)
    snaps_lp = snapshots(lp_sel, final_only=True)
    entries = lb.compute_bounds(snaps_cp["shapes"], types, cache, cpsat=True)
    entries.update(lb.compute_bounds(snaps_lp["shapes"], types, cache, cpsat=False))
    snaps_cp = attach_bounds(snaps_cp, entries, types, CPSAT)
    snaps_lp = attach_bounds(snaps_lp, entries, types, LP)
    exp1 = common.experiment("exp1")
    label_order = list(dict.fromkeys([*exp1["labels"], *exp["labels"], *exp["baselines"], *exp1["baselines"]]))
    both = pd.concat([sel, lp_sel], ignore_index=True)
    all_snaps = pd.concat([snaps_cp, snaps_lp], ignore_index=True)
    table = gap_table(both, all_snaps, exp, label_order)
    common.write_table(table, out_root, "exp8_gap")
    if len(snaps_cp):
        common.write_table(snapshot_table(snaps_cp, exp, label_order), out_root, "exp8_gap_snapshots")
    else:
        common.skipped("exp8_gap_snapshots", "no runs at the CP-SAT scale")
    figure7(table, exp, label_order, out_root)
    return exp8_headline(sel, lp_sel, table, snaps_cp, entries)


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Exp 7 decision latency (figure 4) and Exp 8 optimality gap to the B6 bound (figure 7) from the raw runs."""
    runs = common.load_runs(runs_root, include_abandoned=True)
    headline: dict[str, Any] = {}
    for key, run in ((EXP7, run_exp7), (EXP8, run_exp8)):
        result = run(runs, out_root)
        if result is not None:
            headline[key] = result
    if headline:
        common.write_json(headline, out_root, "latency_optimality")
    else:
        common.skipped("latency_optimality", "no exp7 or exp8 runs")


if __name__ == "__main__":
    common.cli(main)
