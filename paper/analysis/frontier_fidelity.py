"""Exp 6 (H6), the savings vs disruption frontier, and Exp 5 (H5), plan fidelity: figures 3 and 5.

Exp 6 sweeps the entropy-greedy planner's lambda and places each configuration by its savings against the
no-consolidation baseline B0 (``integrated_cost_post`` of B0 on the same scenario, nodes and seed, minus the run's own,
in $) and its disruption (``eviction_cost``; ``evictions`` alongside). The upstream operating points B2, B3 and B5 are
compared with the best lambda point at no more disruption than theirs (experiments/PREREGISTRATION.md, H6).

Exp 5 measures plan fidelity (the share of evicted pods whose replacement lands on the planned node) by placement model
x scale-down model, for random/entropy-greedy, B2 and the steered executor, and tests the preregistered contrasts
(experiments.yaml ``exp5.comparisons``).

Outputs under ``out_root`` (nothing is written for an experiment without runs):

- ``tables/exp6_frontier.{csv,md}``: one row per (scenario, nodes, label): savings, eviction cost, evictions and
  steady-state $/hr with bootstrap CIs, and whether the point is on the empirical Pareto set.
- ``tables/exp6_dominance.{csv,md}``: per (scenario, nodes, operating point, metric) the matched lambda point, the
  paired difference (matched - baseline) with CIs, Wilcoxon p, Holm within H6, and the dominance verdict.
- ``tables/exp5_fidelity.{csv,md}``: one row per (scenario, nodes, label, placement, scale_down) cell: fidelity,
  off-plan share, realized/predicted P2 change, steady-state $/hr and evictions.
- ``tables/exp5_tests.{csv,md}``: the preregistered paired Wilcoxon tests, Holm within H5, with their direction.
- ``tables/frontier_fidelity.json``: the headline numbers and verdicts of both experiments.
- ``figures/out/fig3_frontier.{png,pdf}`` and ``figures/out/fig5_fidelity.{png,pdf}``.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.frontier_fidelity [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch
from scipy import stats

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
    """The direction the signed-rank test sees in paired differences: +1 when the positive differences carry more
    rank weight than the negative ones (W+ > W-), -1 for the reverse, 0 for a tie or no non-zero differences."""
    d = _floats(diffs)
    d = d[np.isfinite(d) & (d != 0)]
    if d.size == 0:
        return 0
    ranks = stats.rankdata(np.abs(d))
    return int(np.sign(ranks[d > 0].sum() - ranks[d < 0].sum()))


def wilcoxon_one_sided(diffs: Any, alternative: str) -> float:
    """One-sided Wilcoxon signed-rank p-value of paired differences ("greater": median above 0, "less": below), with
    the conventions of ``common.wilcoxon_paired``: NaN without pairs, 1.0 when every difference is zero."""
    d = _floats(diffs)
    d = d[np.isfinite(d)]
    if d.size == 0:
        return math.nan
    if np.all(d == 0):
        return 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return float(stats.wilcoxon(d, alternative=alternative).pvalue)


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
FRONTIER_COLUMNS = (["scenario", "nodes", "label", "kind", "lambda", "n_runs", "n_valid", "n_invalid", "n_excluded",
                     "n_used"] + [c for m in FRONTIER_METRICS for c in ci_names(m)] + ["pareto"])
# (metric, which direction is better for the pipeline, Holm family). Savings and eviction cost decide the verdict;
# evictions are reported in a family of their own.
DOMINANCE_METRICS = ((SAVINGS, "higher", "H6"), ("eviction_cost", "lower", "H6"), ("evictions", "lower", "H6-evictions"))
VERDICT_FAMILY = "H6"
DOMINANCE_COLUMNS = [
    "experiment", "family", "scenario", "nodes", "baseline", "matched_label", "matched_lambda", "selection", "metric",
    "better_if", "n_pairs", "n_dropped", "n_dropped_invalid", "n_dropped_missing", "mean_matched", "mean_baseline",
    "mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo", "median_diff_hi", "pct_diff",
    "pct_diff_lo", "pct_diff_hi", "p_wilcoxon", "p_floor", "p_holm", "direction", "significant", "holm_reachable",
    "outcome", "p_better", "p_better_holm", "p_worse", "p_worse_holm", "verdict", "verdict_one_sided", "underpowered",
]
_LAMBDA_IN_LABEL = re.compile(r"(?:^|/)lambda=([^/]+)$")


def add_savings(df: pd.DataFrame, keys: Sequence[str], zero_label: str, cost_metric: str) -> pd.DataFrame:
    """A copy of ``df`` with ``b0_cost`` (``cost_metric`` of the valid ``zero_label`` run sharing the row's ``keys``,
    NaN when there is none) and ``savings`` = b0_cost - the row's own ``cost_metric`` ($ saved by consolidation).

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
    out = df.copy()
    out["b0_cost"] = [ref.get(tuple(k), math.nan) for k in out[keys].itertuples(index=False, name=None)]
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
    """Exp 6 frontier points: one row per (scenario, nodes, label) for B0, the lambda sweep and the operating points.

    ``df`` carries ``savings`` (``add_savings``). Statistics are over the label's *used* runs: valid, with a valid B0
    run on the same seed, and finite savings and eviction cost (``n_excluded`` counts valid runs left out for lack of a
    B0 pair). ``kind`` is reference (B0), sweep or baseline; ``lambda`` the swept value; ``pareto`` whether the point
    is on the empirical Pareto set of its stratum (``pareto_flags`` over mean savings and mean eviction cost; all
    points, B0 included). A stratum without any valid B0 run is skipped, since savings are undefined there.
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
            cell = part.loc[part["label"] == label]
            if cell.empty:
                continue
            valid = cell.loc[cell["valid"].astype(bool)]
            used = valid.loc[np.isfinite(valid[SAVINGS].to_numpy(dtype=float))
                             & np.isfinite(_floats(valid["eviction_cost"]))]
            row: dict[str, Any] = {
                "scenario": scenario, "nodes": nodes, "label": label,
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


def select_matched(sweep: pd.DataFrame, baseline_disruption: float) -> tuple[pd.Series | None, str]:
    """The lambda point matched to an operating point, and the rule that picked it.

    Among sweep rows (``frontier_table``) with defined means: the highest mean savings among those whose mean eviction
    cost is at or below ``baseline_disruption`` (ties: lower eviction cost, then lower lambda); if there is none, the
    lowest mean eviction cost (ties: higher savings, then lower lambda). (None, "") without sweep points.
    """
    ok = sweep.loc[np.isfinite(sweep[f"{SAVINGS}_mean"].to_numpy(dtype=float))
                   & np.isfinite(sweep["eviction_cost_mean"].to_numpy(dtype=float))]
    if ok.empty:
        return None, ""
    eligible = ok.loc[ok["eviction_cost_mean"] <= baseline_disruption]
    if len(eligible):
        pick = eligible.sort_values([f"{SAVINGS}_mean", "eviction_cost_mean", "lambda"], ascending=[False, True, True],
                                    kind="stable", na_position="last").iloc[0]
        return pick, "highest savings at disruption <= baseline"
    pick = ok.sort_values(["eviction_cost_mean", f"{SAVINGS}_mean", "lambda"], ascending=[True, False, True],
                          kind="stable", na_position="last").iloc[0]
    return pick, "lowest disruption (none at or below baseline)"


def _lookup(df: pd.DataFrame, label: str, keys: Sequence[str], cols: Sequence[str]) -> dict[tuple[Any, ...], dict]:
    sub = df.loc[(df["label"] == label) & df["valid"].astype(bool), list(keys) + list(cols)]
    return {tuple(rec[k] for k in keys): rec for rec in sub.to_dict("records")}


def _dominance_rows(part: pd.DataFrame, keys: Sequence[str], matched: pd.Series, baseline: str, zero: str,
                    selection: str, cost_metric: str) -> list[dict[str, Any]]:
    """Paired rows (one per DOMINANCE_METRICS entry) of the matched lambda point against one operating point.

    Seeds are kept only when the matched point, the operating point and B0 all have a valid run there with finite
    cost, eviction cost and evictions, so every metric is compared on the same seeds (``common.align`` counts the
    dropped ones). Savings differences are (B0 - matched) - (B0 - baseline), i.e. baseline cost - matched cost.
    """
    keys = list(keys)
    cols = [cost_metric, "eviction_cost", "evictions"]
    work = part.copy()
    complete = np.all([np.isfinite(_floats(work[c])) for c in cols], axis=0)
    work["_complete"] = np.where(complete, 1.0, math.nan)
    arms = [matched["label"], baseline, zero]
    al = common.align(work, arms, "_complete", keys)
    kept = [tuple(k) for k in al.table[keys].itertuples(index=False, name=None)]
    look = {arm: _lookup(work, arm, keys, cols) for arm in arms}

    def values(arm: str, col: str) -> np.ndarray:
        return np.array([float(look[arm][k][col]) for k in kept], dtype=float)

    b0 = values(zero, cost_metric)
    pairs = {
        SAVINGS: (b0 - values(matched["label"], cost_metric), b0 - values(baseline, cost_metric)),
        "eviction_cost": (values(matched["label"], "eviction_cost"), values(baseline, "eviction_cost")),
        "evictions": (values(matched["label"], "evictions"), values(baseline, "evictions")),
    }
    rows = []
    for metric, better_if, family in DOMINANCE_METRICS:
        a, b = pairs[metric]
        eff = common.paired_effect(a, b)
        d = a - b
        greater, less = wilcoxon_one_sided(d, "greater"), wilcoxon_one_sided(d, "less")
        rows.append({
            "experiment": EXP6, "family": family, "scenario": matched["scenario"], "nodes": matched["nodes"],
            "baseline": baseline, "matched_label": matched["label"], "matched_lambda": matched["lambda"],
            "selection": selection, "metric": metric, "better_if": better_if, "n_pairs": eff["n_pairs"],
            "n_dropped": al.dropped, "n_dropped_invalid": al.dropped_invalid, "n_dropped_missing": al.dropped_missing,
            "mean_matched": eff["mean_a"], "mean_baseline": eff["mean_b"],
            **{k: eff[k] for k in ("mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo",
                                   "median_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon",
                                   "p_floor")},
            "_dir": rank_direction(d),
            "p_better": greater if better_if == "higher" else less,
            "p_worse": less if better_if == "higher" else greater,
        })
    return rows


def _verdict(outcomes: Sequence[str], n_pairs: Sequence[int]) -> str:
    if not outcomes or any(n == 0 for n in n_pairs):
        return "no pairs"
    if "worse" in outcomes:
        return "dominated"
    if "better" in outcomes:
        return "dominates"
    return "matches"


def dominance_table(df: pd.DataFrame, frontier: pd.DataFrame, exp: dict[str, Any]) -> pd.DataFrame:
    """Exp 6 dominance tests: for each (scenario, nodes) and operating point (B2, B3, B5), the matched lambda point
    (``select_matched`` on the frontier means) against the operating point, paired on seeds (``_dominance_rows``).

    One row per metric: savings and eviction cost (Holm family H6, which decides the verdict) and evictions (family
    H6-evictions, descriptive). Differences are matched - baseline. ``p_wilcoxon`` is two-sided and ``p_holm`` its Holm
    adjustment within the family; ``direction`` is the side the signed-rank statistic favours (higher, lower, none);
    ``outcome`` is better / worse for the pipeline when p_holm < 0.05, else "no difference". ``verdict`` (the
    preregistered rule): dominated if savings or eviction cost is significantly worse, else dominates if either is
    significantly better, else matches; "no pairs" without paired seeds. ``verdict_one_sided`` is the same rule with
    one-sided Wilcoxon tests at 0.05 (``p_better``/``p_worse``, each Holm-adjusted within its family), a sensitivity
    check.
    """
    params = exp["params"]
    zero, ops, cost_metric = params["zero_label"], list(params["operating_points"]), params["savings_metric"]
    keys = list(exp["pair_keys"])
    rows: list[dict[str, Any]] = []
    strata = list(dict.fromkeys(frontier[["scenario", "nodes"]].itertuples(index=False, name=None)))
    for scenario, nodes in strata:
        fr = _in_stratum(frontier, scenario, nodes)
        part = _in_stratum(df, scenario, nodes)
        where = f"{scenario}/{_fmt_nodes(nodes)}"
        for baseline in ops:
            brow = fr.loc[fr["label"] == baseline]
            if brow.empty or not math.isfinite(float(brow["eviction_cost_mean"].iloc[0])):
                common.skipped(f"exp6 dominance {baseline} {where}", f"no valid {baseline} runs paired with {zero}")
                continue
            matched, selection = select_matched(fr.loc[fr["kind"] == "sweep"],
                                                float(brow["eviction_cost_mean"].iloc[0]))
            if matched is None:
                common.skipped(f"exp6 dominance {baseline} {where}", "no lambda sweep runs paired with B0")
                continue
            try:
                rows += _dominance_rows(part, keys, matched, baseline, zero, selection, cost_metric)
            except ValueError as err:  # an arm with two runs on the same seed: no pairing is defined
                common.skipped(f"exp6 dominance {baseline} {where}", str(err))
    if not rows:
        return pd.DataFrame(columns=DOMINANCE_COLUMNS)
    out = pd.DataFrame(rows)
    for col in ("p_holm", "p_better_holm", "p_worse_holm"):
        out[col] = math.nan
    out["holm_reachable"] = False
    for _, idx in out.groupby("family", sort=True).groups.items():
        for src, dst in (("p_wilcoxon", "p_holm"), ("p_better", "p_better_holm"), ("p_worse", "p_worse_holm")):
            out.loc[idx, dst] = common.holm(out.loc[idx, src].to_numpy(dtype=float))
        out.loc[idx, "holm_reachable"] = common.holm_reachable(out.loc[idx, "p_floor"].to_numpy(dtype=float),
                                                               out.loc[idx, "p_wilcoxon"].to_numpy(dtype=float))
    out["holm_reachable"] = out["holm_reachable"].astype(bool)
    p_holm = out["p_holm"].to_numpy(dtype=float)
    out["significant"] = np.isfinite(p_holm) & (p_holm < ALPHA)
    out["direction"] = out["_dir"].map({1: "higher", -1: "lower", 0: "none"})
    good = np.where(out["better_if"] == "higher", out["_dir"] > 0, out["_dir"] < 0)
    out["outcome"] = np.where(~out["significant"], "no difference", np.where(good, "better", "worse"))
    out["verdict"], out["verdict_one_sided"], out["underpowered"] = "", "", False
    for _, idx in out.groupby(["scenario", "nodes", "baseline"], sort=False).groups.items():
        g = out.loc[idx]
        dec = g.loc[g["family"] == VERDICT_FAMILY]
        verdict = _verdict(dec["outcome"].tolist(), dec["n_pairs"].tolist())
        out.loc[idx, "verdict"] = verdict
        # "matches" from tests that could not have been significant (too few non-zero pairs for Holm over the
        # family) is no evidence of matching.
        out.loc[idx, "underpowered"] = bool(verdict == "matches" and not dec["holm_reachable"].all())
        one = ["worse" if pw < ALPHA else ("better" if pb < ALPHA else "no difference")
               for pw, pb in zip(dec["p_worse_holm"].fillna(1.0), dec["p_better_holm"].fillna(1.0))]
        out.loc[idx, "verdict_one_sided"] = _verdict(one, dec["n_pairs"].tolist())
    return out[DOMINANCE_COLUMNS].reset_index(drop=True)


def figure3(frontier: pd.DataFrame, exp: dict[str, Any], out_root: Path) -> bool:
    """Figure 3: one panel per (scenario, nodes); x = mean cumulative eviction cost, y = mean cumulative savings vs
    B0 ($), 95% bootstrap CIs as error bars on both. Lambda points are joined in lambda order and labelled with their
    lambda; B0 and the operating points are grey markers with direct labels. Returns False (and writes nothing) when
    no point has defined savings."""
    fr = frontier.loc[np.isfinite(frontier[f"{SAVINGS}_mean"].to_numpy(dtype=float))
                      & np.isfinite(frontier["eviction_cost_mean"].to_numpy(dtype=float))]
    if fr.empty:
        common.skipped("fig3_frontier", "no frontier point with defined savings")
        return False
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
        for label in _ordered(part.loc[part["kind"] != "sweep", "label"], exp["baselines"]):
            r = part.loc[part["label"] == label].iloc[0]
            style = common.style_for(label)
            x, y = float(r["eviction_cost_mean"]), float(r[f"{SAVINGS}_mean"])
            h = ax.errorbar([x], [y], xerr=_err([x], [r["eviction_cost_mean_lo"]], [r["eviction_cost_mean_hi"]]),
                            yerr=_err([y], [r[f"{SAVINGS}_mean_lo"]], [r[f"{SAVINGS}_mean_hi"]]),
                            color=style["color"], marker=style["marker"], markersize=6, linestyle="none",
                            elinewidth=0.8, capsize=2, zorder=4)
            handles.setdefault(label, h)
            ax.annotate(label, (x, y), xytext=(6, 0), textcoords="offset points", fontsize=8, ha="left",
                        va="center", color=common.NEUTRAL)
        ax.set_title(f"{scenario}, {_fmt_nodes(nodes)} nodes")
        ax.set_xlabel("Cumulative eviction cost")
        ax.margins(x=0.12, y=0.12)
    axes[0][0].set_ylabel("Cumulative savings vs B0 ($)")
    names = list(handles)
    fig.legend([handles[n] for n in names], names, loc="outside lower center", ncol=min(len(names), 5))
    common.save_figure(fig, out_root, "fig3_frontier")
    return True


def exp6_headline(sel: pd.DataFrame, frontier: pd.DataFrame, dominance: pd.DataFrame,
                  exp: dict[str, Any]) -> dict[str, Any]:
    """Exp 6 headline: validity counts, the Pareto set per stratum, one entry per (stratum, operating point) with the
    matched point, the savings and eviction-cost differences (CIs, Holm p) and the verdicts, and ``h6_supported``:
    True when every operating point in every stratum is matched or dominated by the sweep (verdict matches or
    dominates), False when any verdict is dominated, None when a comparison is missing or has no pairs."""
    pareto = {}
    for (scenario, nodes), part in frontier.groupby(["scenario", "nodes"], sort=False):
        pareto[f"{scenario}/{_fmt_nodes(nodes)}"] = part.loc[part["pareto"] == True, "label"].tolist()  # noqa: E712
    entries = []
    if len(dominance):
        for (scenario, nodes, baseline), g in dominance.groupby(["scenario", "nodes", "baseline"], sort=False):
            e: dict[str, Any] = {"scenario": scenario, "nodes": nodes, "baseline": baseline,
                                 "matched_label": g["matched_label"].iloc[0],
                                 "matched_lambda": g["matched_lambda"].iloc[0], "selection": g["selection"].iloc[0],
                                 "verdict": g["verdict"].iloc[0], "verdict_one_sided": g["verdict_one_sided"].iloc[0],
                                 "underpowered": bool(g["underpowered"].iloc[0])}
            for r in g.itertuples(index=False):
                e[r.metric] = {"n_pairs": r.n_pairs, "n_dropped": r.n_dropped, "mean_matched": r.mean_matched,
                               "mean_baseline": r.mean_baseline, "mean_diff": r.mean_diff, "ci": [r.mean_diff_lo,
                               r.mean_diff_hi], "pct_diff": r.pct_diff, "pct_ci": [r.pct_diff_lo, r.pct_diff_hi],
                               "p_wilcoxon": r.p_wilcoxon, "p_floor": r.p_floor, "p_holm": r.p_holm,
                               "holm_reachable": r.holm_reachable, "outcome": r.outcome, "family": r.family}
            entries.append(e)
    verdicts = [e["verdict"] for e in entries]
    # The preregistered design, not just the strata that have runs: a missing scenario leaves H6 open.
    design = {(s, float(n), b) for s in exp["scenarios"] for n in exp["nodes"]
              for b in exp["params"]["operating_points"]}
    evaluated = {(e["scenario"], float(e["nodes"]), e["baseline"]) for e in entries
                 if e["verdict"] in ("matches", "dominates", "dominated")}
    expected = len(design)
    if "dominated" in verdicts:
        supported: bool | None = False
    elif design <= evaluated and not any(e["underpowered"] for e in entries):
        supported = True
    else:
        supported = None
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(sel), "pareto": pareto, "dominance": entries,
        "verdict_counts": {v: verdicts.count(v) for v in sorted(set(verdicts))},
        "comparisons_expected": expected, "comparisons_evaluated": len(entries),
        "comparisons_missing": [f"{s}/{_fmt_nodes(n)}/{b}" for s, n, b in sorted(design - evaluated, key=str)],
        "n_underpowered": sum(bool(e["underpowered"]) for e in entries), "h6_supported": supported,
        "rule": "per operating point and stratum: the lambda point with the highest mean savings among those with "
                "mean eviction cost <= the operating point's (else the lowest-disruption point); paired two-sided "
                "Wilcoxon on savings and eviction cost, Holm within H6; dominated if either is significantly worse, "
                "dominates if either is significantly better and neither worse, else matches. h6_supported is None "
                "(open) while a design comparison is missing or a 'matches' rests on tests too small to reach "
                "significance after Holm (underpowered)",
    }


def run_exp6(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 6 tables (exp6_frontier, exp6_dominance) and figure 3; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP6)
    params = exp["params"]
    sel = common.select_runs(runs, EXP6)
    if sel.empty:
        common.skipped(EXP6)
        return None
    known = [params["zero_label"], *exp["labels"], *params["operating_points"]]
    extra = sorted(set(sel["label"].dropna().astype(str)) - set(known))
    if extra:
        common.logger.info("exp6: ignoring labels outside the registry: %s", ", ".join(extra))
    sel = sel.loc[sel["label"].isin(known)]
    if sel.empty:
        common.skipped(EXP6)
        return None
    df = add_savings(sel, exp["pair_keys"], params["zero_label"], params["savings_metric"])
    frontier = frontier_table(df, exp)
    if frontier.empty:
        common.skipped(EXP6, f"no {params['zero_label']} runs, so savings are undefined")
        return None
    common.write_table(frontier, out_root, "exp6_frontier")
    dominance = dominance_table(df, frontier, exp)
    if len(dominance):
        common.write_table(dominance, out_root, "exp6_dominance")
    else:
        common.skipped("exp6_dominance", "no operating point and lambda point paired with B0")
    figure3(frontier, exp, out_root)
    return exp6_headline(sel, frontier, dominance, exp)


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
EXPECT_SIGN = {"higher": 1, "lower": -1}
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
    """The preregistered Exp 5 tests (``common.evaluate``: paired two-sided Wilcoxon on fidelity, Holm within H5),
    TOST columns dropped, plus ``direction`` (the side the signed-rank statistic favours: higher, lower or none) and
    ``outcome``: "as expected" (p_holm < 0.05 in the expected direction), "opposite" (p_holm < 0.05 against it),
    "not significant", or "no pairs"."""
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
    res["direction"] = directions
    res["outcome"] = outcomes
    return res.reset_index(drop=True)


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


def exp5_headline(sel: pd.DataFrame, cells: pd.DataFrame, tests: pd.DataFrame, exp: dict[str, Any]) -> dict[str, Any]:
    """Exp 5 headline: validity counts, fidelity and off-plan share per cell (with CIs and the realized/predicted
    ratio of sums), the oracle check (plan-following cells), and every test with its Holm p and outcome."""
    oracle = exp["params"].get("oracle_placement")
    cell_cols = ["scenario", "nodes", "label", "placement", "scale_down", "n_fidelity", "fidelity_mean",
                 "fidelity_mean_lo", "fidelity_mean_hi", "off_plan_share_mean", "off_plan_share_mean_lo",
                 "off_plan_share_mean_hi", "delta_p2_ratio_of_sums", "delta_p2_ratio_of_sums_lo",
                 "delta_p2_ratio_of_sums_hi"]
    test_cols = ["comparison", "stratum", "n_pairs", "n_dropped", "mean_a", "mean_b", "mean_diff", "mean_diff_lo",
                 "mean_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon", "p_holm", "direction",
                 "outcome"]
    outcomes = tests["outcome"].tolist() if len(tests) else []
    return {
        "hypothesis": exp["hypothesis"], "validity": _validity(sel), "cells": cells[cell_cols],
        "oracle_check": cells.loc[cells["placement"] == oracle, cell_cols],
        "tests": tests[test_cols] if len(tests) else [],
        "outcome_counts": {o: outcomes.count(o) for o in sorted(set(outcomes))}, "n_tests": len(outcomes),
    }


def run_exp5(runs: pd.DataFrame, out_root: Path) -> dict[str, Any] | None:
    """Exp 5 tables (exp5_fidelity, exp5_tests) and figure 5; returns the JSON headline, or None when skipped."""
    exp = common.experiment(EXP5)
    sel = common.select_runs(runs, EXP5)
    if sel.empty:
        common.skipped(EXP5)
        return None
    df = add_ratio(sel)
    cells = fidelity_table(df, exp)
    common.write_table(cells, out_root, "exp5_fidelity")
    tests = fidelity_tests(df, common.comparisons(EXP5))
    if len(tests):
        common.write_table(tests, out_root, "exp5_tests")
    else:
        common.skipped("exp5_tests", "no paired arms")
    figure5(cells, exp, out_root)
    return exp5_headline(sel, cells, tests, exp)


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
