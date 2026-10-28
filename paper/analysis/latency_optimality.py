"""Exp 7 (H7), decision cost vs cluster size, and Exp 8, the optimality gap to the offline packing bound (B6):
figures 4 and 7.

Exp 7 measures the decision cost of a disruption pass, as process CPU time (``cpu_pass_*``, the preregistered
measure: co-running cells perturb it less) and wall time (``wall_pass_*``, descriptive), both excluding the
validation waits during which a pass advances the simulated world, for B2 (upstream WhenEmptyOrUnderutilized, prefix
binary search), heuristic/entropy-greedy and random/entropy-greedy on W1 at 50, 200 and 1,000 nodes (experiments.yaml
``exp7``: Exp 1's exp1-n200 and exp1-n1000 cells, exp7-n50, exp7-b2, exp7-b2-n1000). It tests H7 at 1,000 nodes
(family H7, m = 2): random vs heuristic on mean and p95 CPU time per pass, paired by seed, Wilcoxon, Holm over the 2;
plus the timeout clause (no valid random/entropy-greedy run at 1,000 nodes with a pass over 1 minute of wall time).
The secondary family H7-B2 (random vs B2 from exp7-b2-n1000, 1-hour runs) is absent when that group is cut and never
enters the verdict.

H7-S (experiments.yaml ``h7s``) is the supplementary timing pass declared after the session VM was replaced mid-session
(experiments/RESEARCH_LOG.md, "H7 hardware confound" and "H7-S design revision"): both arms of H7's W1 1,000-node
cells rerun with the session binary on one VM, interleaved in one runner invocation at 4-way load beside a mix of
both arms (``experiments/runs/h7-timing``). Its configurations are
byte-identical to the session cells', so each rerun must reproduce its session twin's simulated outputs (decision log
up to the run ID, time series, pod-set snapshots and every summary field but wall and CPU time); one mismatch voids
the pass. A seed whose two arms started on different VMs (experiments/session-driver/machine-log.txt) leaves the
tests. It runs H7's two tests as family H7-S and is reported beside H7 test by test, never replacing its verdict.

Exp 8 bounds each run's fleet cost from below with ``lower_bound`` (B6). At 50 nodes (groups exp8 and exp8-b5) every
podsets.jsonl snapshot gets the CP-SAT bound and the LP bound; at 200 and 1,000 nodes (Exp 1's groups, ``exp8.params``)
the final snapshot gets the LP bound. ``gap = cost_per_hour at the snapshot / bound - 1``. Descriptive.

Outputs under ``out_root`` (nothing is written for an experiment without runs):

- ``tables/exp7_latency.{csv,md}``: one row per (scenario, nodes, label): run counts, mean, p95 and max CPU and wall
  time per pass (ms), full simulations per pass and measured nodes (mean peak_nodes), each with the mean and median
  and their 95% bootstrap CIs over valid runs; passes over upstream's 1 and 3 minute timeouts and search timeouts
  (totals and runs affected).
- ``tables/exp7_tests.{csv,md}``: the H7 tests (and H7-B2's when present) at 1,000 nodes with floors, Holm p, the
  paired effect, the speedup (reference mean / random mean), the direction and the outcome.
- ``tables/h7s_tests.{csv,md}`` and ``tables/h7s_runs.{csv,md}``: H7-S's tests, and per rerun its VM, pairing,
  concurrency, CPU time against its session twin and the determinism check.
- ``tables/exp8_gap.{csv,md}``: one row per (nodes, scenario, label): which bound the row uses (cpsat at 50 nodes, lp
  at 200 and 1,000), the final-snapshot gap with CIs, the final gap against the LP bound, the mean gap over every
  snapshot (50 nodes), how many final bounds CP-SAT proved optimal and the bound's slack.
- ``tables/exp8_gap_snapshots.{csv,md}``: at 50 nodes, the mean gap with CIs per (scenario, label, snapshot time).
- ``tables/latency_optimality.json``: the headline numbers, validity counts, the H7 verdict and H7-S.
- ``figures/out/fig4_latency.{png,pdf}`` and ``figures/out/fig7_optimality_gap.{png,pdf}``.
- ``tables/cache/lower_bounds.json``: the bound cache (``lower_bound.BoundCache``), derived and safe to delete.

Configurations with fewer than ``common.min_seeds()`` finished runs are cut from every table here.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.latency_optimality [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullLocator

from paper.analysis import common
from paper.analysis import lower_bound as lb

EXP7 = "exp7"
H7S = "h7s"
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
    """The direction the signed-rank test sees in paired differences (``common.rank_direction``): +1 when the positive
    differences carry more rank weight than the negative ones (W+ > W-), -1 for the reverse, 0 for a tie or no
    non-zero differences."""
    return common.rank_direction(_floats(diffs))


def _validity(sel: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-group run counts and invalid reasons (abandoned cells included) for the JSON headline."""
    return common.to_jsonable(common.validity_report(sel, by=("group",)))


def _counts(part: pd.DataFrame) -> dict[str, int]:
    valid = part["valid"].astype(bool)
    return {"n_runs": len(part), "n_valid": int(valid.sum()), "n_invalid": int((~valid).sum())}


# --------------------------------------------------------------------------------------------------------------------
# Exp 7: decision latency vs cluster size
# --------------------------------------------------------------------------------------------------------------------

CPU_METRICS = ("cpu_pass_mean_ms", "cpu_pass_p95_ms", "cpu_pass_max_ms")
WALL_METRICS = ("wall_pass_mean_ms", "wall_pass_p95_ms", "wall_pass_max_ms")
LATENCY_METRICS = (*CPU_METRICS, *WALL_METRICS)
SIMS_PER_PASS = "full_simulations_per_pass"
SIZE = "peak_nodes"
# Upstream's consolidation timeouts: multi-node 1 minute, single-node 3 minutes (in ms), with the summary column that
# counts passes over each.
TIMEOUTS = (("1m", "wall_passes_over_1m", 60_000.0), ("3m", "wall_passes_over_3m", 180_000.0))
LATENCY_COLUMNS = (["scenario", "nodes", "label", "n_runs", "n_valid", "n_invalid", "duration_min"]
                   + [c for m in (*LATENCY_METRICS, SIMS_PER_PASS, SIZE) for c in ci_names(m)]
                   + ["disruption_passes_mean", "cpu_pass_total_s_mean"]
                   + [c for name, _, _ in TIMEOUTS for c in (f"passes_over_{name}_total", f"runs_over_{name}",
                                                             f"runs_over_{name}_unknown")]
                   + ["search_timeouts_total", "runs_with_search_timeouts"])
TOST_COLUMNS = ("margin", "ci90_lo", "ci90_hi", "p_lower", "p_upper", "p_tost", "equivalent", "noninferior",
                "equivalent_holm", "p_upper_holm", "noninferior_holm")


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
    """Exp 7 per-cell table (``LATENCY_COLUMNS``): one row per (scenario, nodes, label), statistics over valid runs
    (runs from simulators without CPU-time fields have NaN there and leave those statistics).
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
        cpu_total = _floats(valid["cpu_pass_total_s"])
        row["cpu_pass_total_s_mean"] = float(cpu_total[np.isfinite(cpu_total)].mean()) \
            if np.isfinite(cpu_total).any() else math.nan
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
    dropped pair-wise, a run without the metric counted as undefined, Holm within H7 and within H7-B2 at their
    fixed sizes), without the TOST columns, plus ``speedup`` (mean_b / mean_a: how many times cheaper a is)."""
    res = common.evaluate(df, comps)
    if res.empty:
        return res
    res = res.drop(columns=[c for c in TOST_COLUMNS if c in res.columns])
    with np.errstate(divide="ignore", invalid="ignore"):
        res["speedup"] = _floats(res["mean_b"]) / _floats(res["mean_a"])
    return res.reset_index(drop=True)


def b2_family_present(everything: pd.DataFrame, exp: dict[str, Any]) -> tuple[bool, str]:
    """Whether the secondary family H7-B2 is reported: its B2 configuration (``params.b2_group`` at the tested size)
    has runs and is not cut. Returns (present, reason when absent)."""
    params = exp["params"]
    groups = common.map_groups([params["b2_group"]])
    rows = everything.loc[everything["group"].isin(groups) & (everything["label"] == "B2")
                          & (_floats(everything["nodes"]) == float(params["tested_nodes"]))]
    if rows.empty:
        return False, f"no {params['b2_group']} runs"
    if bool(common.cut_mask(everything).loc[rows.index].any()):
        return False, f"{params['b2_group']} is cut (fewer than {common.min_seeds()} finished runs)"
    return True, ""


def _panel_data(table: pd.DataFrame, metric: str) -> pd.DataFrame:
    return table.loc[np.isfinite(_floats(table[f"{metric}_mean"])) & np.isfinite(_floats(table[f"{SIZE}_mean"]))]


def figure4(table: pd.DataFrame, exp: dict[str, Any], out_root: Path) -> bool:
    """Figure 4: decision cost vs measured cluster size, log-log. Columns: mean and p95 per pass; rows: process CPU
    time (the preregistered measure) and wall time (descriptive), per scenario. Each point is the mean over valid
    runs with 95% bootstrap CIs (vertical: cost; horizontal: mean peak nodes), one series per configuration, with
    upstream's 60 s and 180 s consolidation timeouts as reference lines on the wall-time panels. Series are dodged
    sideways by a few percent (multiplicatively) so configurations at the same size stay apart. A panel without data
    (runs from simulators without CPU-time fields) says so. Returns False (writes nothing) when no cell has any."""
    kinds = (("CPU", ("cpu_pass_mean_ms", "cpu_pass_p95_ms")), ("Wall", ("wall_pass_mean_ms", "wall_pass_p95_ms")))
    metrics = [m for _, ms in kinds for m in ms]
    if all(_panel_data(table, m).empty for m in metrics):
        common.skipped("fig4_latency", "no runs with pass latencies")
        return False
    scenarios = _ordered(table["scenario"], exp["scenarios"])
    labels = _ordered(table["label"], list(exp["labels"]) + list(exp["baselines"]))
    rows = [(kind, scenario, ms) for scenario in scenarios for kind, ms in kinds]
    width, height = common.FIGSIZE["double"]
    fig, axes = plt.subplots(len(rows), 2, sharex=True, squeeze=False,
                             figsize=(width, 0.5 + (height - 0.5) * len(rows)))
    lows = [v for m in metrics for v in _floats(table[f"{m}_mean_lo"]) if np.isfinite(v) and v > 0]
    y_lo = 10 ** math.floor(math.log10(min(lows) / 2)) if lows else 1.0
    highs = [v for m in metrics for v in _floats(table[f"{m}_mean_hi"]) if np.isfinite(v) and v > 0]
    # Room above the 180 s line, and above every interval should a configuration be slower than that.
    y_hi = max(6e5, 10 ** math.ceil(math.log10(2 * max(highs)))) if highs else 6e5
    sizes = sorted({float(n) for n in table["nodes"]})
    handles: dict[str, Any] = {}
    for i, (kind, scenario, ms) in enumerate(rows):
        for j, metric in enumerate(ms):
            ax = axes[i][j]
            data = _panel_data(table.loc[table["scenario"] == scenario], metric)
            for k, label in enumerate(labels):
                r = data.loc[data["label"] == label].sort_values(f"{SIZE}_mean")
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
            if kind == "Wall":
                for (_, _, limit_ms), text in zip(TIMEOUTS, ("60 s: upstream multi-node timeout",
                                                             "180 s: upstream single-node timeout")):
                    ax.axhline(limit_ms, color=common.NEUTRAL, linewidth=0.8, linestyle=":", zorder=1)
                    ax.annotate(text, xy=(0.02, limit_ms), xycoords=("axes fraction", "data"), xytext=(0, 2),
                                textcoords="offset points", fontsize=7, color=common.NEUTRAL, va="bottom")
            if data.empty:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center",
                        color=common.NEUTRAL, fontsize=8)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_ylim(y_lo, y_hi)
            ax.xaxis.set_major_locator(FixedLocator(sizes))
            ax.xaxis.set_minor_locator(NullLocator())
            ax.set_xticklabels([_fmt_nodes(n) for n in sizes])
            ax.set_xlim(min(sizes) / 1.5, max(sizes) * 1.5)
            stat = "Mean" if metric.endswith("mean_ms") else "p95"
            title = f"{stat} {'CPU' if kind == 'CPU' else 'wall'} time per pass"
            ax.set_title(title if len(scenarios) == 1 else f"{title}, {scenario}")
            if i == len(rows) - 1:
                ax.set_xlabel("Measured nodes (mean peak)")
            if j == 0:
                ax.set_ylabel(f"{kind} time (ms)")
    legend = [handles[label] for label in labels if label in handles]
    fig.legend(handles=legend, loc="outside lower center", ncol=min(len(legend), 3))
    common.save_figure(fig, out_root, "fig4_latency")
    return True


def timeout_clause_runs(runs: pd.DataFrame, exp: dict[str, Any]) -> pd.DataFrame:
    """The runs H7's timeout clause covers: every valid, non-cut run of the subject label (random/entropy-greedy) at
    the tested size in the Exp 1 1,000-node group, across all of its scenarios (W1, W2, W4), not only Exp 7's W1."""
    params = exp["params"]
    groups = common.map_groups(params.get("timeout_clause_groups", ["exp1-n1000"]))
    sel = runs.loc[runs["group"].isin(groups) & (runs["label"] == params["subject_label"])
                   & (pd.to_numeric(runs["nodes"], errors="coerce") == float(params["tested_nodes"]))]
    if sel.empty:
        return sel
    sel = sel.loc[~common.cut_mask(sel)]
    return sel.loc[sel["valid"].astype(bool)]


def exp7_headline(everything: pd.DataFrame, table: pd.DataFrame, tests: pd.DataFrame, exp: dict[str, Any],
                  missing: list[dict[str, Any]], b2: tuple[bool, str],
                  clause_runs: pd.DataFrame | None = None) -> dict[str, Any]:
    """Exp 7 headline: validity counts, the per-cell latency table, every test with its floor, Holm p and outcome,
    the timeout counts at the tested size, and the H7 verdict.

    H7 (PREREGISTRATION.md) is supported iff both H7 tests at 1,000 nodes (CPU time per pass, mean and p95) are
    significant after Holm with random/entropy-greedy lower and the timeout clause holds: no valid
    random/entropy-greedy run at 1,000 nodes has a pass over 1 minute of wall time (``wall_passes_over_1m`` > 0);
    otherwise not supported; "incomplete" while an H7 configuration is missing or cut. The clause is "unknown" (and
    the verdict "inconclusive") when no run exceeds the limit but some run's count cannot be told. H7-B2 is reported
    when present and never enters the verdict."""
    params = exp["params"]
    cols = ["scenario", "nodes", "label", "n_runs", "n_valid", "n_invalid", "cpu_pass_mean_ms_mean",
            "cpu_pass_mean_ms_mean_lo", "cpu_pass_mean_ms_mean_hi", "cpu_pass_p95_ms_mean", "cpu_pass_p95_ms_mean_lo",
            "cpu_pass_p95_ms_mean_hi", "wall_pass_mean_ms_mean", "wall_pass_mean_ms_mean_lo",
            "wall_pass_mean_ms_mean_hi", "wall_pass_p95_ms_mean", "wall_pass_max_ms_mean",
            "full_simulations_per_pass_mean", "peak_nodes_mean", "passes_over_1m_total", "passes_over_3m_total",
            "runs_over_1m", "runs_over_1m_unknown", "runs_over_3m", "runs_over_3m_unknown", "search_timeouts_total"]
    test_cols = ["family", "comparison", "metric", "a", "b", "stratum", "n_pairs", "n_dropped", "n_dropped_invalid",
                 "n_undefined", "n_dropped_missing", "mean_a", "mean_b", "mean_diff", "mean_diff_lo", "mean_diff_hi",
                 "pct_diff", "pct_diff_lo", "pct_diff_hi", "speedup", "p_wilcoxon", "p_floor", "m", "p_holm",
                 "holm_reachable", "direction", "outcome", "expect_met"]
    tested, subject = float(params["tested_nodes"]), params["subject_label"]
    at_size = table.loc[(table["nodes"].astype(float) == tested) & (table["label"] == subject)]
    timeouts = {name: {"passes_total": float(at_size[f"passes_over_{name}_total"].sum()) if len(at_size) else None,
                       "runs_over": int(at_size[f"runs_over_{name}"].sum()),
                       "runs_unknown": int(at_size[f"runs_over_{name}_unknown"].sum())}
                for name, _, _ in TIMEOUTS}
    h7 = tests.loc[tests["family"] == "H7"] if len(tests) else tests
    # The clause covers every valid random/entropy-greedy run at 1,000 nodes (all Exp 1 scenarios), not only W1.
    over = pd.to_numeric(clause_runs["wall_passes_over_1m"], errors="coerce") if clause_runs is not None and len(
        clause_runs) else pd.Series(dtype=float)
    timeouts["1m_all_scenarios"] = {"runs": int(len(over)), "runs_over": int((over > 0).sum()),
                                    "runs_unknown": int(over.isna().sum())}
    if over.empty:
        clause = "no data"
    elif int((over > 0).sum()):
        clause = "fails"
    elif int(over.isna().sum()):
        clause = "unknown"
    else:
        clause = "holds"
    outcomes = h7["outcome"].tolist() if len(h7) else []
    both = len(outcomes) == 2 and all(o == "lower" for o in outcomes)
    uninformative = len(outcomes) < 2 or any(o in common.UNINFORMATIVE_OUTCOMES for o in outcomes)
    if clause == "fails":
        verdict = "not supported"
    elif both and clause == "holds":
        verdict = "supported"
    elif (both and clause in ("unknown", "no data")) or (uninformative and not any(
            o in ("higher", "no significant difference") for o in outcomes)):
        # An unreachable or missing test cannot count against H7 either (G6): inconclusive, as in H2-H6.
        verdict = "inconclusive"
    else:
        verdict = "not supported"
    present, reason = b2
    b2_tests = tests.loc[tests["family"] == params["b2_family"]] if len(tests) else tests
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(everything), "cells": table[cols],
        "tests": tests[test_cols] if len(tests) else [], "tested_nodes": tested, "subject": subject,
        "timeouts_at_tested_nodes": timeouts,
        "h7": {"rule": "both H7 tests (random vs heuristic entropy-greedy at 1,000 nodes, W1, on CPU time per pass, "
                       "mean and p95; Wilcoxon, Holm over 2) significant with random lower, and no valid "
                       "random/entropy-greedy run at 1,000 nodes with wall_passes_over_1m > 0",
               "tests_lower": int((h7["outcome"] == "lower").sum()) if len(h7) else 0,
               "timeout_clause": clause, "verdict": "incomplete" if missing else verdict,
               "verdict_on_available": verdict, "complete": not missing, "missing_configurations": missing},
        "h7_b2": ({"status": "present", "tests": b2_tests[test_cols],
                   "tests_lower": int((b2_tests["outcome"] == "lower").sum())} if present
                  else {"status": "absent", "reason": reason}),
    }


def run_exp7(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 7 tables (exp7_latency, exp7_tests) and figure 4; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP7)
    params = exp["params"]
    everything = common.select_runs(runs, EXP7, include_cut=True)
    if everything.empty:
        common.skipped(EXP7)
        return None
    sel = common.select_runs(runs, EXP7)
    df = add_latency_columns(sel)
    table = latency_table(df, exp)
    common.write_table(table, out_root, "exp7_latency")
    b2 = b2_family_present(everything, exp)
    comps = [c for c in common.comparisons(EXP7) if b2[0] or c.family != params["b2_family"]]
    if not b2[0]:
        common.skipped(params["b2_family"], b2[1])
    tests = latency_tests(df, comps)
    common.write_table(tests, out_root, "exp7_tests")
    figure4(table, exp, out_root)
    missing = common.missing_configurations(everything, [c for c in comps if c.family == "H7"])
    return exp7_headline(everything, table, tests, exp, missing, b2, timeout_clause_runs(runs, exp))


# --------------------------------------------------------------------------------------------------------------------
# H7-S: the supplementary paired timing pass
# --------------------------------------------------------------------------------------------------------------------

H7S_FAMILY = "H7-S"
# Summary fields that measure the machine, not the simulation: they may differ between a rerun and its twin.
UNSIMULATED_PREFIXES = ("wall_", "cpu_")
UNSIMULATED_FIELDS = ("run_id", "label")
SIMULATED_FILES = ("decisions.jsonl.gz", "timeseries.csv", "podsets.jsonl")
_RUN_ID_FIELD = re.compile(rb'"run_id"\s*:\s*"[^"]*"')
_BTIME = re.compile(r"\bbtime=(\S+)")
_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z")
MACHINE_LOG = Path("session-driver") / "machine-log.txt"  # under experiments/, beside the runs tree
H7S_RUN_COLUMNS = ["run_id", "label", "seed", "valid", "started_at", "finished_at", "boot", "paired",
                   "concurrency_mean", "twin_run_id", "twin_status", "differences", "cpu_pass_mean_ms",
                   "twin_cpu_pass_mean_ms", "cpu_ratio_to_twin"]


def _file_digest(path: Path) -> str | None:
    """sha256 of a run file's simulated content: decision logs decompressed and with their run IDs blanked (a rerun
    has its own run ID), other files as written; None when the file is absent."""
    if not path.is_file():
        return None
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    if path.name.startswith("decisions"):
        data = _RUN_ID_FIELD.sub(b'"run_id":""', data)
    return hashlib.sha256(data).hexdigest()


def _simulated_summary(run_dir: Path) -> dict[str, Any]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return {k: v for k, v in summary.items() if k not in UNSIMULATED_FIELDS and not k.startswith(UNSIMULATED_PREFIXES)}


def twin_differences(rerun: Path, twin: Path) -> list[str]:
    """What differs between a rerun and its session twin: summary fields (``UNSIMULATED_*`` aside) and the simulated
    files (``SIMULATED_FILES``; a file both lack is no difference). Empty when they match."""
    out = []
    a, b = _simulated_summary(rerun), _simulated_summary(twin)
    out += [f"summary.{k}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    out += [name for name in SIMULATED_FILES if _file_digest(rerun / name) != _file_digest(twin / name)]
    return out


def parse_time(text: Any) -> float:
    """An RFC 3339 UTC timestamp (up to nanosecond precision) as POSIX seconds; NaN when unparsable."""
    m = _TIME.match(text) if isinstance(text, str) else None
    if not m:
        return math.nan
    whole = pd.Timestamp(m.group(1), tz="UTC").timestamp()
    return whole + (float("0" + m.group(2)) if m.group(2) else 0.0)


def machine_boots(path: Path) -> list[float] | None:
    """Sorted boot times (POSIX seconds) from the session driver's machine log (experiments/session-driver/
    machine-log.txt; "btime=unknown", the first VM's, sorts first as -inf); None when the file is absent."""
    if not path.is_file():
        return None
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _BTIME.search(line)
        if line.startswith("#") or not m:
            continue
        out.add(-math.inf if m.group(1) == "unknown" else parse_time(m.group(1)))
    return sorted(b for b in out if not math.isnan(b))


def boot_index(t: float, boots: Sequence[float]) -> int:
    """The VM a run started on: the index of the last boot at or before ``t`` (-1 when none or ``t`` is NaN)."""
    if math.isnan(t):
        return -1
    return max((i for i, b in enumerate(boots) if b <= t), default=-1)


def _run_times(run_dir: Path) -> tuple[float, float]:
    try:
        m = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return math.nan, math.nan
    return parse_time(m.get("started_at")), parse_time(m.get("finished_at"))


def mean_concurrency(interval: tuple[float, float], intervals: Sequence[tuple[float, float]]) -> float:
    """Time-weighted mean number of the ``intervals`` (all cells of the matrix, this one included) running during
    ``interval``; NaN for an empty or unknown interval."""
    s, f = interval
    if not (math.isfinite(s) and math.isfinite(f)) or f <= s:
        return math.nan
    overlap = sum(max(0.0, min(f, b) - max(s, a)) for a, b in intervals if math.isfinite(a) and math.isfinite(b))
    return overlap / (f - s)


def _hash_key(run_id: Any) -> str:
    """The runner's configuration hash, the last ``__`` part of a run ID: equal for a rerun and its session twin."""
    return str(run_id).rsplit("__", 1)[-1]


def h7s_runs(runs: pd.DataFrame, sel: pd.DataFrame, exp: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One row per H7-S run (``H7S_RUN_COLUMNS``): its VM, whether its seed's two arms started on one VM
    (``paired``), its mean concurrency, and the determinism check against its session twin (the run of
    ``params.reference_groups`` with the same runner hash).

    ``twin_status``: "match" or "differs" (``twin_differences``, plus "config_id" when the analysis fingerprints
    differ); "not checked (rerun abandoned)" or "not checked (twin abandoned)" when one side has no summary (such a
    run is dropped pair-wise like any invalid run, so it does not block the check); "no twin" when the twin is not
    in the runs at all. The check's status: "fail" when any run differs (H7-S is void), "incomplete" when a twin is
    missing, "pass" when at least one run matched and none differs, else "not checked".

    ``paired`` is False for both arms of a seed whose arms started on different boots of the machine log
    (``MACHINE_LOG`` beside the runs tree); those seeds leave the tests. Without the log, every seed counts as paired
    and the epoch check is "not checked"."""
    groups = common.map_groups(exp["params"]["reference_groups"])
    ref = runs.loc[runs["group"].isin(groups)]
    twins = {_hash_key(r): row for r, row in zip(ref["run_id"], ref.to_dict("records"))}
    paths = [Path(p) for p in sel["path"]]
    log_path = paths[0].resolve().parent.parent.parent / MACHINE_LOG if paths else None
    boots = machine_boots(log_path) if log_path is not None else None
    if boots is None and paths:
        common.logger.warning("%s not found: H7-S pairs not checked for a shared VM", MACHINE_LOG.as_posix())
    matrix_dirs = {p.parent for p in paths}
    intervals = [_run_times(d.parent) for m in matrix_dirs for d in m.glob("*/manifest.json")]
    rows = []
    for _, r in sel.sort_values(["seed", "label"], kind="stable").iterrows():
        start, finish = _run_times(Path(r["path"]))
        row: dict[str, Any] = {"run_id": r["run_id"], "label": r["label"], "seed": r["seed"], "valid": bool(r["valid"]),
                               "started_at": start, "finished_at": finish,
                               "boot": boot_index(start, boots) if boots is not None else math.nan,
                               "concurrency_mean": mean_concurrency((start, finish), intervals),
                               "cpu_pass_mean_ms": _floats([r.get("cpu_pass_mean_ms")])[0]}
        twin = twins.get(_hash_key(r["run_id"]))
        rerun_ok = (Path(r["path"]) / "summary.json").is_file()
        twin_ok = twin is not None and (Path(twin["path"]) / "summary.json").is_file()
        row["twin_run_id"] = twin["run_id"] if twin is not None else ""
        row["twin_cpu_pass_mean_ms"] = _floats([twin.get("cpu_pass_mean_ms")])[0] if twin is not None else math.nan
        if twin is None:
            row["twin_status"], row["differences"] = "no twin", ""
        elif not rerun_ok:
            row["twin_status"], row["differences"] = "not checked (rerun abandoned)", ""
        elif not twin_ok:
            row["twin_status"], row["differences"] = "not checked (twin abandoned)", ""
        else:
            diff = twin_differences(Path(r["path"]), Path(twin["path"]))
            if r.get("config_id") != twin.get("config_id"):
                diff.append("config_id")
            row["twin_status"], row["differences"] = ("differs" if diff else "match"), ", ".join(diff)
        both_valid = row["valid"] and twin is not None and bool(twin.get("valid"))
        row["cpu_ratio_to_twin"] = (row["cpu_pass_mean_ms"] / row["twin_cpu_pass_mean_ms"]
                                    if both_valid and row["twin_cpu_pass_mean_ms"] > 0 else math.nan)
        rows.append(row)
    table = pd.DataFrame(rows, columns=H7S_RUN_COLUMNS)
    split: list[int] = []
    if boots is not None and len(table):
        for seed, part in table.groupby("seed", sort=True):
            if part["boot"].nunique(dropna=False) > 1:
                split.append(int(seed))
    table["paired"] = ~table["seed"].isin(split) if len(table) else pd.Series(dtype=bool)
    statuses = set(table["twin_status"])
    status = ("fail" if "differs" in statuses else "incomplete" if "no twin" in statuses
              else "pass" if "match" in statuses else "not checked")
    ratios = {}
    for label, part in table.groupby("label", sort=True):
        v = _floats(part["cpu_ratio_to_twin"])
        v = v[np.isfinite(v) & (v > 0)]
        ratios[label] = {"n": int(len(v)), "geomean": float(np.exp(np.log(v).mean())) if len(v) else None,
                         "min": float(v.min()) if len(v) else None, "max": float(v.max()) if len(v) else None}
    conc = {label: float(np.nanmean(_floats(part["concurrency_mean"]))) if np.isfinite(
        _floats(part["concurrency_mean"])).any() else None for label, part in table.groupby("label", sort=True)}
    info = {"determinism": {"status": status, "runs": int(len(table)),
                            "matched": int((table["twin_status"] == "match").sum()),
                            "differs": table.loc[table["twin_status"] == "differs", "run_id"].tolist(),
                            "not_checked": table.loc[table["twin_status"].str.startswith("not checked"),
                                                     "run_id"].tolist(),
                            "no_twin": table.loc[table["twin_status"] == "no twin", "run_id"].tolist()},
            "pairing": {"status": "not checked" if boots is None else "checked", "split_seeds": split},
            "cpu_ratio_to_twin": ratios, "concurrency_mean": conc}
    return table, info


def _test_outcomes(tests: Any, family: str) -> dict[str, str]:
    if not isinstance(tests, pd.DataFrame) or tests.empty:
        return {}
    part = tests.loc[tests["family"] == family]
    return dict(zip(part["metric"], part["outcome"]))


def run_h7s(runs: pd.DataFrame, out_root: Path, h7: dict[str, Any] | None) -> dict[str, Any] | None:
    """H7-S tables (h7s_tests, h7s_runs) and headline, or None when the pass has no runs.

    The tests use the valid runs of paired seeds (``h7s_runs``). Verdict: "void" when the determinism check fails;
    "incomplete" while a configuration is missing or cut or a twin is missing; else "supported" when both tests are
    significant with random lower, "not supported" when either is higher or not significantly different, and
    "inconclusive" otherwise (an unreachable or missing test), as for H7's tests. H7-S has no timeout clause: it
    re-measures H7's CPU-time tests only, so it is compared with H7 test by test (``tests_vs_h7``), never with H7's
    verdict, which also carries the clause (reported beside it as ``h7_timeout_clause``)."""
    exp = common.experiment(H7S)
    everything = common.select_runs(runs, H7S, include_cut=True)
    if everything.empty:
        common.skipped(H7S_FAMILY)
        return None
    table, info = h7s_runs(runs, everything, exp)
    common.write_table(table, out_root, "h7s_runs")
    sel = common.select_runs(runs, H7S)
    split = set(info["pairing"]["split_seeds"])
    sel = sel.loc[~pd.to_numeric(sel["seed"], errors="coerce").isin(split)]
    tests = latency_tests(add_latency_columns(sel), common.comparisons(H7S))
    common.write_table(tests, out_root, "h7s_tests")
    missing = common.missing_configurations(everything, common.comparisons(H7S))
    outcomes = tests["outcome"].tolist() if len(tests) else []
    det = info["determinism"]["status"]
    if det == "fail":
        verdict = "void"
    elif missing or det == "incomplete":
        verdict = "incomplete"
    elif len(outcomes) == 2 and all(o == "lower" for o in outcomes):
        verdict = "supported"
    elif any(o in ("higher", "no significant difference") for o in outcomes):
        verdict = "not supported"
    else:
        verdict = "inconclusive"
    h7_block = (h7 or {}).get("h7", {})
    h7_outcomes = _test_outcomes((h7 or {}).get("tests"), "H7")
    s_outcomes = _test_outcomes(tests, H7S_FAMILY)
    comparable = verdict not in ("void", "incomplete") and bool(h7_block) and h7_block.get("complete", False)
    per_test = {m: {"h7": h7_outcomes.get(m), "h7s": s_outcomes.get(m),
                    "agree": (h7_outcomes.get(m) == s_outcomes.get(m)) if comparable else None}
                for m in sorted(set(h7_outcomes) | set(s_outcomes))}
    cols = ["comparison", "metric", "n_pairs", "mean_a", "mean_b", "pct_diff", "pct_diff_lo", "pct_diff_hi", "speedup",
            "p_wilcoxon", "p_floor", "p_holm", "holm_reachable", "outcome"]
    return {"family": H7S_FAMILY, "validity": _validity(everything), "tests": tests[cols] if len(tests) else [],
            **info, "verdict": verdict, "missing_configurations": missing,
            "tests_vs_h7": per_test,
            "all_tests_agree": all(v["agree"] for v in per_test.values()) if comparable and per_test else None,
            "h7_verdict": h7_block.get("verdict"), "h7_timeout_clause": h7_block.get("timeout_clause")}


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
    everything = common.select_runs(runs, EXP8, include_cut=True)
    sel = common.select_runs(runs, EXP8)
    sel = sel.loc[sel["nodes"].astype(float).isin(_cpsat_nodes(exp))] if len(sel) else sel
    everything = everything.loc[everything["nodes"].astype(float).isin(_cpsat_nodes(exp))] if len(everything) \
        else everything
    lp_sel = lp_everything = runs.iloc[0:0]
    if len(runs):
        mask = (runs["group"].isin(common.map_groups(params.get("lp_groups", [])))
                & runs["nodes"].astype(float).isin([float(n) for n in params.get("lp_nodes", [])])
                & runs["scenario"].isin(exp["scenarios"]))
        lp_everything = runs.loc[mask].copy()
        lp_sel = runs.loc[mask & ~common.cut_mask(runs)].copy()
    if everything.empty and lp_everything.empty:
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
    return exp8_headline(everything, lp_everything, table, snaps_cp, entries)


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Exp 7 decision latency (figure 4), H7-S, and Exp 8 optimality gap to the B6 bound (figure 7) from the raw
    runs."""
    runs = common.load_runs(runs_root, include_abandoned=True)
    headline: dict[str, Any] = {}
    for key, run in ((EXP7, run_exp7), (EXP8, run_exp8)):
        result = run(runs, out_root)
        if result is not None:
            headline[key] = result
    h7s = run_h7s(runs, out_root, headline.get(EXP7))
    if h7s is not None:
        headline[H7S] = h7s
    if headline:
        common.write_json(headline, out_root, "latency_optimality")
    else:
        common.skipped("latency_optimality", "no exp7 or exp8 runs")


if __name__ == "__main__":
    common.cli(main)
