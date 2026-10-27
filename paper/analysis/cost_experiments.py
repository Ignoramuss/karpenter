"""Exp 1 (H1), Exp 3 (H3), Exp 4 (H4) and the ablations (figure 8): what each consolidation configuration costs.

Run through ``paper/make_figures.py`` (``--only cost_experiments``) or on its own::

    /opt/research-venv/bin/python -m paper.analysis.cost_experiments [--runs experiments/runs] [--out paper]

Outputs under ``out_root`` (each written only when its experiment has runs; otherwise "skipped <exp>: no runs"):

- ``tables/exp1_h1``: the H1 equivalence tests. One row per paired comparison random/<planner> vs
  heuristic/<planner> on steady_cost_per_hour, per scenario and scale (family ``H1``: entropy-greedy and anneal-plan,
  the 12 preregistered TOSTs; family ``H1-P0-contrast``: savings-only), then ``reference`` rows for B2 and B3.
- ``tables/exp1_secondary``: per arm (scenario, nodes, label) and metric, the secondary metrics with bootstrap CIs, and
  for random arms the paired difference against the heuristic arm with the same planner (descriptive).
- ``tables/exp1_phases``: per arm, mean cost_per_hour in the early phase (first hour after warmup) and the late phase
  (the last 20% of samples, the simulator's steady-state window), with the same paired differences.
- ``tables/exp3_h3``: merge+shift vs merge-only per scenario and eviction budget on integrated_cost_post (family
  ``H3``), evictions (``H3-evictions``) and eviction_cost (``H3-eviction-cost``).
- ``tables/exp4_h4``: every anneal-plan and anneal-online configuration vs entropy-greedy on steady_cost_per_hour and
  evictions (family ``H4``: the preregistered contrasts; ``H4-sweep``: the other anneal-plan configurations).
- ``tables/exp4_sweep``: per configuration of the temperature/cooling sweep, cost, evictions and eviction cost.
- ``tables/fig8_ablation`` and ``figures/out/fig8_ablation``: full vs each ablated variant, per scenario.
- ``tables/cost_experiments.json``: the headline verdicts per experiment, with validity counts.

Statistical conventions (beyond ``experiments/PREREGISTRATION.md``):

- Comparison tables (exp1_h1, exp3_h3, exp4_h4) give each arm's mean and median over the kept pairs, so they agree
  with the paired difference and the TOST margin; per-arm tables (exp1_secondary, exp1_phases, exp4_sweep,
  fig8_ablation) use every valid run of the arm.
- The direction of a Wilcoxon result is the sign of W+ - W- (the signed-rank statistic the test is about), not the
  sign of the mean difference; the two only disagree when a few large differences outweigh many small ones.
- ``outcome`` is a's position relative to b after Holm (lower / higher / no significant difference; for TOST
  equivalent / higher or lower beyond margin / not shown equivalent); ``expect_met`` checks it against the
  registry's ``expect``.
- Rows exist for every preregistered stratum even before its runs do (``n_pairs`` 0, outcome "no data"), so
  verdicts can say how much of the design is in.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.table import Table
from scipy import stats

from paper.analysis import common

H1_METRIC = "steady_cost_per_hour"
# Exp 1 secondary metrics: the brief's list, then the registry's remaining secondary metrics.
EXP1_SECONDARY = ["integrated_cost_post", "evictions", "eviction_cost", "nodes_removed", "regret_evictions",
                  "re_disruptions", "full_simulations_per_pass", "wall_pass_mean_ms"]
EXP4_METRICS = ["steady_cost_per_hour", "evictions"]
EXP4_SWEEP_METRICS = ["steady_cost_per_hour", "evictions", "eviction_cost", "integrated_cost_post", "final_nodes"]
ABL_METRICS = ["steady_cost_per_hour", "integrated_cost_post", "evictions", "eviction_cost"]
ABL_HEADERS = {"steady_cost_per_hour": "Steady-state\ncost ($/hr)", "integrated_cost_post": "Cost after\nwarmup ($)",
               "evictions": "Pod\nevictions", "eviction_cost": "Eviction\ncost"}
ABL_DECIMALS = {"steady_cost_per_hour": 2, "integrated_cost_post": 1, "evictions": 0, "eviction_cost": 0}
FAMILY_ORDER = ["H1", "H1-P0-contrast", "reference", "H3", "H3-evictions", "H3-eviction-cost", "H4", "H4-sweep",
                "ablation", "ablation-eviction-cost"]
MINUS = "−"

ARM_STAT_COLUMNS = ["mean_a", "mean_a_lo", "mean_a_hi", "median_a", "median_a_lo", "median_a_hi",
                    "mean_b", "mean_b_lo", "mean_b_hi", "median_b", "median_b_lo", "median_b_hi"]
EFFECT_COLUMNS = ["mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo", "median_diff_hi",
                  "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon"]
PAIR_COUNT_COLUMNS = ["n_pairs", "n_dropped_invalid", "n_dropped_missing"]
H1_COLUMNS = [
    "family", "comparison", "scenario", "nodes", "planner", "a", "b", *PAIR_COUNT_COLUMNS, "n_runs", "n_invalid",
    *ARM_STAT_COLUMNS, *EFFECT_COLUMNS, "margin", "margin_pct", "ci90_lo", "ci90_hi", "ci90_lo_pct", "ci90_hi_pct",
    "p_lower", "p_upper", "p_tost", "p_tost_holm", "equivalent", "equivalent_holm", "p_upper_holm", "noninferior",
    "noninferior_holm", "outcome",
]
WILCOXON_COLUMNS = ["family", "comparison", "metric", "scenario", "nodes", "a", "b", "expect", *PAIR_COUNT_COLUMNS,
                    *ARM_STAT_COLUMNS, *EFFECT_COLUMNS, "p_holm", "significant", "direction", "outcome", "expect_met"]
ARM_TABLE_COLUMNS = ["scenario", "nodes", "label", "metric", "n_runs", "n_valid", "n_invalid", "mean", "mean_lo",
                     "mean_hi", "median", "median_lo", "median_hi", "ref", *PAIR_COUNT_COLUMNS, "diff_mean",
                     "diff_mean_lo", "diff_mean_hi", "diff_median", "diff_median_lo", "diff_median_hi", "diff_pct",
                     "diff_pct_lo", "diff_pct_hi", "p_wilcoxon"]


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Write the Exp 1, 3, 4 and ablation tables, figure 8 and cost_experiments.json (see the module docstring).

    Experiments without runs are skipped with a note and contribute nothing; the JSON holds one entry per experiment
    that has runs and is not written when none has.
    """
    runs = prepare_runs(common.load_runs(Path(runs_root), include_abandoned=True))
    headline: dict[str, Any] = {}
    for key, analyse in (("exp1", analyse_exp1), ("exp3", analyse_exp3), ("exp4", analyse_exp4),
                         ("abl", analyse_ablation)):
        sel = common.select_runs(runs, key)
        if sel.empty:
            common.skipped(key)
            continue
        common.logger.info("%s: %d runs (%d invalid or abandoned)", key, len(sel), int((~sel["valid"]).sum()))
        headline[key] = {"title": common.experiment(key)["title"],
                         "hypothesis": common.experiment(key)["hypothesis"],
                         "validity": validity_summary(sel), **analyse(sel, Path(out_root))}
    if headline:
        common.write_json(headline, Path(out_root), "cost_experiments")
        common.logger.info("wrote cost_experiments.json (%s)", ", ".join(headline))
    else:
        common.skipped("cost_experiments.json")


def prepare_runs(runs: pd.DataFrame) -> pd.DataFrame:
    """``load_runs`` output plus ``full_simulations_per_pass`` (full_simulations / disruption_passes).

    Only the pipeline counts full scheduling simulations; the simulator writes 0 for baseline arms, so both columns
    are NaN there rather than a misleading zero.
    """
    out = runs.copy()
    if out.empty:
        out["full_simulations_per_pass"] = pd.Series(dtype=float)
        return out
    pipeline = out["arm_name"].astype(object) == "pipeline"
    sims = pd.to_numeric(out["full_simulations"], errors="coerce").astype(float)
    passes = pd.to_numeric(out["disruption_passes"], errors="coerce").astype(float)
    out["full_simulations"] = sims.where(pipeline)
    out["full_simulations_per_pass"] = (sims / passes.where(passes > 0)).where(pipeline)
    return out


def validity_summary(sel: pd.DataFrame) -> dict[str, Any]:
    """Invalid-run counts for an experiment (the preregistered exclusion report): totals and per group."""
    valid = sel["valid"].astype(bool)
    return {"n_runs": int(len(sel)), "n_valid": int(valid.sum()), "n_invalid": int((~valid).sum()),
            "by_group": common.validity_report(sel, by=("group",))}


# --------------------------------------------------------------------------------------------------------------------
# Paired comparisons
# --------------------------------------------------------------------------------------------------------------------


def signed_rank_direction(a: Any, b: Any) -> int:
    """Sign of W+ - W- for the paired differences a - b (zeros dropped, average ranks): -1, 0 or +1.

    This is the side on which the two-sided Wilcoxon signed-rank test rejects, so it gives the direction of a
    significant result.
    """
    d = np.asarray(a, dtype=float).ravel() - np.asarray(b, dtype=float).ravel()
    d = d[np.isfinite(d) & (d != 0)]
    if d.size == 0:
        return 0
    ranks = stats.rankdata(np.abs(d))
    return int(np.sign(ranks[d > 0].sum() - ranks[d < 0].sum()))


def _direction_name(sign: int) -> str:
    return {-1: "lower", 0: "none", 1: "higher"}[sign]


def _norm_key(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(float(v) if isinstance(v, (int, float, np.number)) and not isinstance(v, bool) else str(v)
                 for v in values)


def design_strata(key: str, comparison: common.Comparison) -> list[tuple[Any, ...]]:
    """The preregistered strata of a comparison: the registry's scenarios x nodes (after ``where``), in registry
    order. Empty when the comparison stratifies on anything else (then only strata with data are reported)."""
    e = common.experiment(key)
    axes = {"scenario": list(e["scenarios"]), "nodes": list(e["nodes"])}
    if not comparison.by or any(b not in axes for b in comparison.by):
        return []
    lists = [[v for v in axes[b] if b not in comparison.where or v in comparison.where[b]] for b in comparison.by]
    return list(itertools.product(*lists))


def _stratum_rows(df: pd.DataFrame, values: Mapping[str, Any]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, v in values.items():
        mask &= df[col] == v
    return df.loc[mask]


def _pair_extras(sel: pd.DataFrame, c: common.Comparison, stratum: Mapping[str, Any]) -> dict[str, Any]:
    """Per-arm mean and median with 95% bootstrap CIs over the kept pairs, and the signed-rank direction."""
    part = sel
    for col, allowed in c.where.items():
        part = part.loc[part[col].isin(allowed)]
    p = common.paired(_stratum_rows(part, stratum), c.keys, c.label_col, c.a, c.b, c.metric)
    out: dict[str, Any] = {}
    for side, x in (("a", p.a), ("b", p.b)):
        for stat in ("mean", "median"):
            est, lo, hi = common.bootstrap_ci(x, stat)
            out.update({f"{stat}_{side}": est, f"{stat}_{side}_lo": lo, f"{stat}_{side}_hi": hi})
    out["direction"] = _direction_name(signed_rank_direction(p.a, p.b))
    return out


def _blank_row(c: common.Comparison, values: tuple[Any, ...]) -> dict[str, Any]:
    """A comparison row for a preregistered stratum with no runs in either arm yet."""
    row: dict[str, Any] = {col: math.nan for col in common.COMPARISON_COLUMNS}
    row.update({"experiment": c.experiment, "family": c.family, "comparison": c.name, "test": c.test,
                "metric": c.metric, "a": common.describe_arm(c.a), "b": common.describe_arm(c.b),
                "expect": c.expect, "stratum": ", ".join(f"{k}={v}" for k, v in zip(c.by, values)),
                "n_pairs": 0, "n_dropped": 0, "n_dropped_invalid": 0, "n_dropped_missing": 0})
    row.update(dict(zip(c.by, values)))
    return row


def comparison_table(sel: pd.DataFrame, key: str, comps: Sequence[common.Comparison]) -> pd.DataFrame:
    """Every comparison of ``comps`` per stratum (``common.compare``), with each preregistered stratum present, the
    per-arm CIs and direction (``_pair_extras``), Holm within (experiment, family) and the outcome columns.

    Columns: ``common.COMPARISON_COLUMNS`` plus the strata, the per-arm CIs, ``direction``, ``p_upper_holm`` and
    ``noninferior_holm`` (TOST: Holm over the non-inferiority p-values of the family), ``margin_pct``,
    ``ci90_lo_pct``/``ci90_hi_pct`` (percent of b's mean), ``outcome``, ``expect_met`` and ``order`` (the
    comparison's position in ``comps``).
    """
    records: list[dict[str, Any]] = []
    for order, c in enumerate(comps):
        found = common.compare(sel, c).to_dict("records")
        have = {_norm_key(r[b] for b in c.by) for r in found}
        found += [_blank_row(c, s) for s in design_strata(key, c) if _norm_key(s) not in have]
        for r in found:
            r.update(_pair_extras(sel, c, {b: r[b] for b in c.by}))
            r["order"] = order
            records.append(r)
    out = pd.DataFrame.from_records(records)
    if out.empty:
        return out
    _add_holm(out)
    out["outcome"] = [_outcome(r) for r in out.to_dict("records")]
    out["expect_met"] = pd.Series([_expect_met(r) for r in out.to_dict("records")], dtype=object)
    return out


def _holm_by_family(df: pd.DataFrame, column: str, mask: pd.Series | None = None) -> np.ndarray:
    """Holm-adjusted ``column`` within each (experiment, family), NaN outside ``mask``."""
    out = np.full(len(df), math.nan)
    p = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
    keep = np.ones(len(df), dtype=bool) if mask is None else mask.to_numpy(dtype=bool)
    for _, idx in df.groupby(["experiment", "family"], sort=True, dropna=False).indices.items():
        idx = np.asarray([i for i in idx if keep[i]], dtype=int)
        if idx.size:
            out[idx] = common.holm(p[idx])
    return out


def _add_holm(out: pd.DataFrame) -> None:
    """Add p_holm and significant (as ``common.evaluate``), the TOST equivalence and non-inferiority after Holm, and
    the margin and 90% CI as percent of the reference mean."""
    alpha = common.ALPHA
    out["p_holm"] = _holm_by_family(out, "p")
    p_holm = out["p_holm"].to_numpy(dtype=float)
    out["significant"] = np.isfinite(p_holm) & (p_holm < alpha)
    tost = out["test"] == "tost"
    out["p_upper_holm"] = _holm_by_family(out, "p_upper", tost)
    p_up = out["p_upper_holm"].to_numpy(dtype=float)
    # TOST verdict columns are None (not False) where no test could run, and absent for Wilcoxon rows.
    tested = tost.to_numpy() & np.isfinite(pd.to_numeric(out["p_tost"], errors="coerce").to_numpy(dtype=float))
    for col in ("equivalent", "noninferior"):
        out[col] = pd.Series([bool(v) if t else None for v, t in zip(out[col], tested)], dtype=object)
    out["equivalent_holm"] = pd.Series([bool(s) if t else None for s, t in zip(out["significant"], tested)],
                                       dtype=object)
    out["noninferior_holm"] = pd.Series([bool(q < alpha) if t else None for q, t in zip(p_up, tested)],
                                        dtype=object)
    ref = pd.to_numeric(out["mean_b"], errors="coerce").to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        for col in ("margin", "ci90_lo", "ci90_hi"):
            pct = 100 * pd.to_numeric(out[col], errors="coerce").to_numpy(dtype=float) / np.abs(ref)
            out[f"{col}_pct"] = np.where(np.isfinite(pct), pct, math.nan)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float, np.number)) and not isinstance(v, bool) and math.isfinite(float(v))


def _outcome(r: Mapping[str, Any]) -> str:
    """a relative to b after Holm: see the module docstring."""
    if not _finite(r["n_pairs"]) or r["n_pairs"] == 0:
        return "no data"
    if r["test"] == "tost":
        if r["n_pairs"] < 2 or not _finite(r["p_tost"]):
            return "insufficient data"
        if r["equivalent_holm"]:
            return "equivalent"
        if r["ci90_lo"] > r["margin"]:
            return "higher beyond margin"
        if r["ci90_hi"] < -r["margin"]:
            return "lower beyond margin"
        return "not shown equivalent"
    if not _finite(r["p_holm"]):
        return "insufficient data"
    if r["significant"] and r["direction"] in ("lower", "higher"):
        return str(r["direction"])
    return "no significant difference"


def _expect_met(r: Mapping[str, Any]) -> bool | None:
    """Whether ``outcome`` matches the registry's ``expect`` (None without an expectation or without data)."""
    outcome, expect = r["outcome"], r.get("expect")
    if outcome in ("no data", "insufficient data") or not isinstance(expect, str):
        return None
    return {"lower": outcome == "lower", "higher": outcome == "higher", "not_higher": outcome != "higher",
            "not_lower": outcome != "lower", "equal": outcome == "equivalent"}.get(expect)


COUNT_COLUMNS = ("nodes", "n_pairs", "n_dropped_invalid", "n_dropped_missing", "n_runs", "n_valid", "n_invalid")


def tidy(df: pd.DataFrame) -> pd.DataFrame:
    """``df`` with its count columns as nullable integers (blank, not NaN, where a row has no count)."""
    out = df.copy()
    for col in COUNT_COLUMNS:
        if col in out.columns:
            v = pd.to_numeric(out[col], errors="coerce")
            if bool(((v.dropna() % 1) == 0).all()):
                out[col] = v.round().astype("Int64")
    return out


def _ranked(values: Iterable[Any], order: Sequence[Any]) -> list[tuple[int, Any]]:
    """Sort keys placing ``order``'s items first, in that order, then anything else sorted."""
    pos = {v: i for i, v in enumerate(order)}
    return [(pos[v], "") if v in pos else (len(pos), str(v)) for v in values]


def _sort(df: pd.DataFrame, keys: Sequence[tuple[str, Sequence[Any] | None]]) -> pd.DataFrame:
    """Sort rows by columns, each by a given value order (or numerically / lexically when the order is None)."""
    if df.empty:
        return df.reset_index(drop=True)
    tmp = df.copy()
    names = []
    for i, (col, order) in enumerate(keys):
        name = f"__sort{i}"
        if order is None:
            tmp[name] = pd.to_numeric(tmp[col], errors="coerce")
        else:
            tmp[name] = [f"{k[0]:04d}|{k[1]}" for k in _ranked(tmp[col], order)]
        names.append(name)
    return tmp.sort_values(names, kind="stable").drop(columns=names).reset_index(drop=True)


# --------------------------------------------------------------------------------------------------------------------
# Per-arm tables
# --------------------------------------------------------------------------------------------------------------------


def arm_table(df: pd.DataFrame, key: str, metrics: Sequence[str], refs: Mapping[str, str]) -> pd.DataFrame:
    """Long per-arm table: one row per (scenario, nodes, label, metric) present in ``df``, for the experiment's labels
    and baselines in registry order.

    Columns: n_runs, n_valid, n_invalid; mean and median with 95% bootstrap CIs over the arm's valid runs; and, for a
    label in ``refs``, the paired difference against ``refs[label]`` on the same seeds (mean and median with CIs,
    percent of the reference mean with CI, two-sided Wilcoxon p; descriptive, not Holm-adjusted).
    """
    e = common.experiment(key)
    labels = [*e["labels"], *e["baselines"]]
    df = df.loc[df["label"].isin(labels)]
    if df.empty:
        return pd.DataFrame(columns=ARM_TABLE_COLUMNS)
    summary = common.summarize(df, metrics, by=("scenario", "nodes", "label"))
    keys = e.get("pair_keys") or common.PAIR_KEYS
    rows = []
    for s in summary.to_dict("records"):
        part = _stratum_rows(df, {"scenario": s["scenario"], "nodes": s["nodes"]})
        for m in metrics:
            rec: dict[str, Any] = {c: math.nan for c in ARM_TABLE_COLUMNS}
            rec.update(scenario=s["scenario"], nodes=s["nodes"], label=s["label"], metric=m, n_runs=s["n_runs"],
                       n_valid=s["n_valid"], n_invalid=s["n_invalid"], ref="")
            for stat in ("mean", "median"):
                for suffix in ("", "_lo", "_hi"):
                    rec[f"{stat}{suffix}"] = s[f"{m}_{stat}{suffix}"]
            ref = refs.get(s["label"])
            if ref is not None:
                p = common.paired(part, keys, "label", s["label"], ref, m)
                eff = common.paired_effect(p.a, p.b)
                rec.update(ref=ref, n_pairs=p.n, n_dropped_invalid=p.dropped_invalid,
                           n_dropped_missing=p.dropped_missing, p_wilcoxon=eff["p_wilcoxon"])
                for name in ("mean", "median", "pct"):
                    for suffix in ("", "_lo", "_hi"):
                        rec[f"diff_{name}{suffix}"] = eff[f"{name}_diff{suffix}"]
            rows.append(rec)
    out = pd.DataFrame(rows, columns=ARM_TABLE_COLUMNS)
    return _sort(out, [("nodes", None), ("scenario", e["scenarios"]), ("label", labels), ("metric", metrics)])


def generation_refs(labels: Sequence[str]) -> dict[str, str]:
    """random/<planner> -> heuristic/<planner> for every pair present in ``labels``."""
    return {lab: "heuristic/" + lab.split("/", 1)[1] for lab in labels
            if lab.startswith("random/") and "heuristic/" + lab.split("/", 1)[1] in labels}


# --------------------------------------------------------------------------------------------------------------------
# Exp 1 (H1)
# --------------------------------------------------------------------------------------------------------------------


def analyse_exp1(sel: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp1_h1, exp1_secondary and exp1_phases; return the H1 verdict, the P0 contrast and the phase check."""
    e = common.experiment("exp1")
    params = e["params"]
    tests = comparison_table(sel, "exp1", common.comparisons("exp1"))
    tests["planner"] = tests["a"].astype(str).str.split("/").str[-1]
    tests = tests.rename(columns={"p_holm": "p_tost_holm"})
    h1 = pd.concat([tests.reindex(columns=H1_COLUMNS), reference_rows(sel, e)], ignore_index=True)
    planners = [lab.split("/", 1)[1] for lab in e["labels"] if lab.startswith("heuristic/")]
    h1 = tidy(_sort(h1, [("family", FAMILY_ORDER), ("nodes", None), ("scenario", e["scenarios"]),
                         ("planner", planners), ("a", e["baselines"])]))
    common.write_table(h1, out_root, "exp1_h1")

    refs = generation_refs(e["labels"])
    metrics = EXP1_SECONDARY + [m for m in e["secondary_metrics"] if m not in EXP1_SECONDARY]
    common.write_table(tidy(arm_table(sel, "exp1", metrics, refs)), out_root, "exp1_secondary")

    phases, check = phase_table(sel, "exp1", refs)
    if phases.empty:
        common.skipped("exp1_phases", "no valid runs with a timeseries")
    else:
        common.write_table(tidy(phases), out_root, "exp1_phases")
    common.logger.info("wrote exp1_h1, exp1_secondary%s", "" if phases.empty else ", exp1_phases")
    h1_rows = h1.loc[h1["family"] == "H1"]
    p0_rows = h1.loc[h1["family"] == "H1-P0-contrast"]
    return {"tables": ["exp1_h1", "exp1_secondary"] + ([] if phases.empty else ["exp1_phases"]),
            "h1": h1_verdict(h1_rows, int(params["h1_tests"]), int(params["h1_min_equivalent"])),
            "p0_contrast": tost_counts(p0_rows) | {"tests": _tost_brief(p0_rows)},
            "phases": check}


def reference_rows(sel: pd.DataFrame, e: Mapping[str, Any]) -> pd.DataFrame:
    """Untested reference rows for the baselines (B2, B3): each arm's steady-state cost over its valid runs, per
    scenario and scale, in the H1 table's columns (``a`` is the baseline; test columns blank)."""
    base = sel.loc[sel["label"].isin(e["baselines"])]
    if base.empty:
        return pd.DataFrame(columns=H1_COLUMNS)
    summary = common.summarize(base, [H1_METRIC], by=("scenario", "nodes", "label"))
    rows = []
    for s in summary.to_dict("records"):
        rec: dict[str, Any] = {c: math.nan for c in H1_COLUMNS}
        rec.update(family="reference", comparison=f"{s['label']} (reference)", scenario=s["scenario"],
                   nodes=s["nodes"], planner="", a=s["label"], b="", n_runs=s["n_runs"], n_invalid=s["n_invalid"])
        for stat in ("mean", "median"):
            for suffix in ("", "_lo", "_hi"):
                rec[f"{stat}_a{suffix}"] = s[f"{H1_METRIC}_{stat}{suffix}"]
        rows.append(rec)
    return pd.DataFrame(rows, columns=H1_COLUMNS)


def tost_counts(rows: pd.DataFrame) -> dict[str, int]:
    """Counts over a family of TOST rows: tests with data, equivalent (raw and after Holm), non-inferior (raw and
    after Holm), and random beyond the margin either way (90% CI entirely above +margin or below -margin)."""
    tested = rows.loc[pd.to_numeric(rows["p_tost"], errors="coerce").notna()]
    return {
        "n_tests_with_data": int(len(tested)),
        "n_equivalent": int((tested["equivalent"] == True).sum()),  # noqa: E712 - object column
        "n_equivalent_holm": int((tested["equivalent_holm"] == True).sum()),  # noqa: E712
        "n_noninferior": int((tested["noninferior"] == True).sum()),  # noqa: E712
        "n_noninferior_holm": int((tested["noninferior_holm"] == True).sum()),  # noqa: E712
        "n_worse_beyond_margin": int((tested["ci90_lo"] > tested["margin"]).sum()),
        "n_better_beyond_margin": int((tested["ci90_hi"] < -tested["margin"]).sum()),
        "n_pairs_dropped_invalid": int(pd.to_numeric(rows["n_dropped_invalid"], errors="coerce").fillna(0).sum()),
    }


def _tost_brief(rows: pd.DataFrame) -> pd.DataFrame:
    cols = ["scenario", "nodes", "planner", "n_pairs", "n_dropped_invalid", "mean_a", "mean_b", "pct_diff",
            "pct_diff_lo", "pct_diff_hi", "margin_pct", "ci90_lo_pct", "ci90_hi_pct", "p_tost", "p_tost_holm",
            "equivalent_holm", "noninferior", "noninferior_holm", "outcome"]
    return rows.reindex(columns=cols).reset_index(drop=True)


def h1_verdict(rows: pd.DataFrame, n_expected: int, min_equivalent: int) -> dict[str, Any]:
    """The preregistered H1 decision: supported iff all ``n_expected`` TOSTs have data, at least ``min_equivalent``
    are equivalent after Holm, and none has random worse by more than the margin (90% CI lower bound above +margin).
    "incomplete" while tests lack data (Holm over fewer tests is anti-conservative, so counts are provisional)."""
    counts = tost_counts(rows)
    if counts["n_tests_with_data"] < n_expected:
        verdict = "incomplete"
    elif counts["n_equivalent_holm"] >= min_equivalent and counts["n_worse_beyond_margin"] == 0:
        verdict = "supported"
    else:
        verdict = "not supported"
    return {"rule": f"supported iff >= {min_equivalent} of {n_expected} TOSTs (90% CI of the mean paired difference "
                    "inside +-3% of the heuristic mean) are equivalent after Holm and none has its 90% CI lower "
                    "bound above +margin",
            "n_tests_expected": n_expected, "min_equivalent": min_equivalent, **counts, "verdict": verdict,
            "tests": _tost_brief(rows)}


def run_phases(ts: pd.DataFrame, early: Sequence[float], late_fraction: float) -> dict[str, float]:
    """Mean cost_per_hour of one run's timeseries in the early window [early[0], early[1]) and over the late window,
    the last max(1, floor(n * late_fraction)) samples (the simulator's steady-state rule, so late equals the
    summary's steady_cost_per_hour). Also returns where the late window starts and the last sample time."""
    t = pd.to_numeric(ts["t_min"], errors="coerce").to_numpy(dtype=float)
    cost = pd.to_numeric(ts["cost_per_hour"], errors="coerce").to_numpy(dtype=float)
    n = len(cost)
    if n == 0:
        return {"early": math.nan, "late": math.nan, "late_from": math.nan, "t_end": math.nan}
    in_early = (t >= early[0]) & (t < early[1])
    k = max(1, int(math.floor(n * late_fraction + 1e-9)))
    return {"early": float(cost[in_early].mean()) if in_early.any() else math.nan,
            "late": float(cost[n - k:].mean()), "late_from": float(t[n - k]), "t_end": float(t[-1])}


def phase_table(sel: pd.DataFrame, key: str, refs: Mapping[str, str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Exp 1's early vs late phase table and a consistency check (late phase vs the summary's steady state).

    One row per (scenario, nodes, label, phase) in ``arm_table``'s columns (``metric`` renamed ``phase``: early or
    late), plus ``t_from_min``/``t_to_min``: the registry's early window ([10, 70) min) and, for late, the earliest
    start and the latest end of the runs' late windows. Only valid runs are read; invalid ones count in n_invalid.
    """
    e = common.experiment(key)
    params = e["params"]
    early = [float(x) for x in params["early_window_min"]]
    late_fraction = float(params["late_fraction"])
    labels = set(e["labels"]) | set(e["baselines"])
    recs = []
    for r in sel.sort_values(["matrix", "run_id"], kind="stable").to_dict("records"):
        rec = {k: r[k] for k in ("scenario", "nodes", "seed", "label", "valid")}
        rec.update(early=math.nan, late=math.nan, late_from=math.nan, t_end=math.nan,
                   steady=r[H1_METRIC])
        if bool(r["valid"]) and r["label"] in labels:
            warmup = r.get("warmup_min")
            if _finite(warmup) and float(warmup) != early[0]:
                common.logger.warning("%s: warmup %.0f min differs from the early window start %.0f min",
                                      r["run_id"], float(warmup), early[0])
            try:
                ts = common.load_timeseries(Path(r["path"]))
            except (FileNotFoundError, ValueError, KeyError) as err:
                common.logger.warning("%s: no usable timeseries (%s)", r["run_id"], type(err).__name__)
            else:
                rec.update(run_phases(ts, early, late_fraction))
        recs.append(rec)
    per_run = pd.DataFrame(recs)
    if per_run.empty or per_run["early"].notna().sum() + per_run["late"].notna().sum() == 0:
        return pd.DataFrame(), {}
    table = arm_table(per_run, key, ["early", "late"], refs).rename(columns={"metric": "phase"})
    late_from = per_run.groupby(["scenario", "nodes", "label"])["late_from"].min()
    t_end = per_run.groupby(["scenario", "nodes", "label"])["t_end"].max()
    t_from, t_to = [], []
    for r in table.to_dict("records"):
        cell = (r["scenario"], r["nodes"], r["label"])
        if r["phase"] == "early":
            t_from.append(early[0])
            t_to.append(early[1])
        else:
            t_from.append(float(late_from.get(cell, math.nan)))
            t_to.append(float(t_end.get(cell, math.nan)))
    table.insert(4, "t_from_min", t_from)
    table.insert(5, "t_to_min", t_to)
    gap = (per_run["late"] - pd.to_numeric(per_run["steady"], errors="coerce")).abs()
    max_gap = float(gap.max()) if gap.notna().any() else math.nan
    if _finite(max_gap) and max_gap > 1e-6:
        common.logger.warning("exp1_phases: late-phase mean differs from steady_cost_per_hour by up to %.3g",
                              max_gap)
    check = {"early_window_min": early, "late_fraction": late_fraction,
             "n_runs_read": int(per_run["late"].notna().sum()),
             "late_vs_steady_cost_max_abs_diff": max_gap}
    return table, check


# --------------------------------------------------------------------------------------------------------------------
# Exp 3 (H3)
# --------------------------------------------------------------------------------------------------------------------


def exp3_comparisons() -> list[common.Comparison]:
    """The registry's Exp 3 comparisons plus eviction_cost in its own Holm family (H3-eviction-cost)."""
    comps = common.comparisons("exp3")
    extra = [dataclasses.replace(c, name=c.name.replace(", evictions", ", eviction_cost"),
                                 family="H3-eviction-cost", metric="eviction_cost")
             for c in comps if c.family == "H3-evictions"]
    return comps + extra


def analyse_exp3(sel: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp3_h3 and return H3's verdict: per scenario and budget, supported iff merge+shift has lower
    integrated_cost_post, significant after Holm over the four H3 tests."""
    e = common.experiment("exp3")
    table = comparison_table(sel, "exp3", exp3_comparisons())
    table["budget"] = table["a"].astype(str).str.rsplit("budget-", n=1).str[-1]
    budgets = list(e["params"].get("budgets_pods_per_minute", {}))
    table = tidy(_sort(table, [("family", FAMILY_ORDER), ("nodes", None), ("scenario", e["scenarios"]),
                               ("budget", budgets)]))
    cols = WILCOXON_COLUMNS[:5] + ["budget"] + WILCOXON_COLUMNS[5:]
    common.write_table(table.reindex(columns=cols), out_root, "exp3_h3")
    common.logger.info("wrote exp3_h3")
    h3 = table.loc[table["family"] == "H3"]
    tested = h3.loc[~h3["outcome"].isin(["no data", "insufficient data"])]
    n_supported = int((tested["expect_met"] == True).sum())  # noqa: E712
    n_expected = len(h3)
    verdict = (f"supported in {n_supported} of {n_expected} strata" if len(tested) == n_expected
               else f"incomplete ({len(tested)} of {n_expected} tests have data; supported in {n_supported})")
    brief = ["scenario", "nodes", "budget", "n_pairs", "n_dropped_invalid", "mean_a", "mean_b", "mean_diff",
             "mean_diff_lo", "mean_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon", "p_holm",
             "outcome", "expect_met"]
    evictions = table.loc[table["family"] == "H3-evictions"]
    return {"tables": ["exp3_h3"],
            "h3": {"rule": "per scenario and budget: merge+shift lower on integrated_cost_post and significant "
                           "(paired Wilcoxon, Holm over the four)",
                   "n_tests_expected": n_expected, "n_tests_with_data": int(len(tested)),
                   "n_supported": n_supported, "verdict": verdict, "tests": h3.reindex(columns=brief)},
            "evictions": evictions.reindex(columns=brief[:3] + ["mean_a", "mean_b", "pct_diff", "p_holm",
                                                                "outcome"])}


# --------------------------------------------------------------------------------------------------------------------
# Exp 4 (H4)
# --------------------------------------------------------------------------------------------------------------------


def exp4_comparisons() -> list[common.Comparison]:
    """The registry's H4 family, plus every other anneal-plan configuration vs entropy-greedy on steady-state cost
    and evictions in an exploratory family (H4-sweep), so the table covers every configuration."""
    e = common.experiment("exp4")
    params = e["params"]
    comps = common.comparisons("exp4")
    covered = {(common.describe_arm(c.a), c.metric) for c in comps}
    template = comps[0] if comps else common.Comparison("exp4", "", "H4", "wilcoxon", H1_METRIC, "", "")
    extra = []
    for label in e["labels"]:
        if label == params["greedy_label"]:
            continue
        for metric in EXP4_METRICS:
            if (label, metric) not in covered:
                extra.append(dataclasses.replace(
                    template, name=f"{label} vs {params['greedy_label']}, {metric}", family="H4-sweep",
                    metric=metric, a=label, b=params["greedy_label"], expect=None, where={}))
    return comps + extra


def config_params(sel: pd.DataFrame, labels: Sequence[str]) -> pd.DataFrame:
    """planner, t0 and cooling per configuration label: from the label's first finished run (manifest config),
    else parsed from the label itself (``<planner>/t0=<x>/cooling=<y>``)."""
    rows = []
    finished = sel.loc[sel["planner"].notna()].sort_values(["label", "run_id"], kind="stable")
    first = finished.drop_duplicates("label").set_index("label")
    for label in labels:
        if label in first.index:
            r = first.loc[label]
            rows.append({"label": label, "planner": r["planner"], "t0": float(r["planner_t0"]),
                         "cooling": float(r["planner_cooling"])})
            continue
        parts = label.split("/")
        kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        rows.append({"label": label, "planner": parts[0], "t0": float(kv.get("t0", math.nan)),
                     "cooling": float(kv.get("cooling", math.nan))})
    return pd.DataFrame(rows, columns=["label", "planner", "t0", "cooling"])


def analyse_exp4(sel: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp4_h4 and exp4_sweep; return H4's verdict per scenario and overall."""
    e = common.experiment("exp4")
    params = e["params"]
    configs = config_params(sel, e["labels"])
    table = comparison_table(sel, "exp4", exp4_comparisons())
    table["p_holm_all"] = common.holm(pd.to_numeric(table["p"], errors="coerce").to_numpy(dtype=float))
    table = table.merge(configs.rename(columns={"label": "a"}), on="a", how="left")
    table = tidy(_sort(table, [("family", FAMILY_ORDER), ("order", None), ("nodes", None),
                               ("scenario", e["scenarios"])]))
    cols = WILCOXON_COLUMNS[:7] + ["planner", "t0", "cooling"] + WILCOXON_COLUMNS[7:]
    cols.insert(cols.index("p_holm") + 1, "p_holm_all")
    common.write_table(table.reindex(columns=cols), out_root, "exp4_h4")

    sweep = common.summarize(sel.loc[sel["label"].isin(e["labels"])], EXP4_SWEEP_METRICS,
                             by=("scenario", "nodes", "label"))
    sweep = sweep.merge(configs, on="label", how="left")
    sweep.insert(3, "default", sweep["label"] == params["default_anneal_label"])
    for i, col in enumerate(("planner", "t0", "cooling")):
        sweep.insert(4 + i, col, sweep.pop(col))
    sweep = tidy(_sort(sweep, [("nodes", None), ("scenario", e["scenarios"]), ("label", e["labels"])]))
    common.write_table(sweep, out_root, "exp4_sweep")
    common.logger.info("wrote exp4_h4, exp4_sweep")
    return {"tables": ["exp4_h4", "exp4_sweep"], "default_anneal_label": params["default_anneal_label"],
            "h4": h4_verdict(table, e)}


def h4_verdict(table: pd.DataFrame, e: Mapping[str, Any]) -> dict[str, Any]:
    """H4 per scenario (preregistered, Holm within the H4 family): the default anneal-plan vs entropy-greedy has
    cost lower or equal (not significantly higher) and evictions not significantly higher; each anneal-online
    configuration has significantly more evictions and cost not significantly lower. "supported" iff every clause
    holds in every scenario; "incomplete" while any H4 test lacks data."""
    params = e["params"]
    h4 = table.loc[table["family"] == "H4"]
    default = params["default_anneal_label"]
    online = [lab for lab in e["labels"] if lab.startswith("anneal-online/")]

    def cell(a: str, metric: str, scenario: Any, nodes: Any) -> Mapping[str, Any] | None:
        hit = h4.loc[(h4["a"] == a) & (h4["metric"] == metric) & (h4["scenario"] == scenario) & (h4["nodes"] == nodes)]
        if hit.empty or hit.iloc[0]["outcome"] in ("no data", "insufficient data"):
            return None
        return hit.iloc[0].to_dict()

    plan, onl, complete = [], [], True
    for nodes in e["nodes"]:
        for scenario in e["scenarios"]:
            cost, ev = cell(default, "steady_cost_per_hour", scenario, nodes), cell(default, "evictions", scenario,
                                                                                     nodes)
            if cost is None or ev is None:
                complete = False
                plan.append({"scenario": scenario, "nodes": nodes, "holds": None})
            else:
                plan.append({"scenario": scenario, "nodes": nodes, "cost_pct_diff": cost["pct_diff"],
                             "cost_outcome": cost["outcome"], "cost_not_higher": cost["outcome"] != "higher",
                             "cost_significantly_lower": cost["outcome"] == "lower",
                             "evictions_pct_diff": ev["pct_diff"], "evictions_outcome": ev["outcome"],
                             "evictions_not_higher": ev["outcome"] != "higher",
                             "holds": cost["outcome"] != "higher" and ev["outcome"] != "higher"})
            for label in online:
                cost, ev = cell(label, "steady_cost_per_hour", scenario, nodes), cell(label, "evictions", scenario,
                                                                                       nodes)
                if cost is None or ev is None:
                    complete = False
                    onl.append({"scenario": scenario, "nodes": nodes, "config": label, "holds": None})
                    continue
                onl.append({"scenario": scenario, "nodes": nodes, "config": label,
                            "evictions_pct_diff": ev["pct_diff"], "evictions_outcome": ev["outcome"],
                            "evictions_higher": ev["outcome"] == "higher", "cost_pct_diff": cost["pct_diff"],
                            "cost_outcome": cost["outcome"], "cost_not_lower": cost["outcome"] != "lower",
                            "holds": ev["outcome"] == "higher" and cost["outcome"] != "lower"})

    def clause(items: list[dict[str, Any]]) -> str:
        if not items or any(i["holds"] is None for i in items):
            return "incomplete"
        return "supported" if all(i["holds"] for i in items) else "not supported"

    plan_v, online_v = clause(plan), clause(onl)
    overall = ("incomplete" if not complete else
               "supported" if plan_v == online_v == "supported" else "not supported")
    return {"rule": "anneal-plan default vs entropy-greedy: cost not significantly higher and evictions not "
                    "significantly higher; each anneal-online vs entropy-greedy: evictions significantly higher and "
                    "cost not significantly lower (paired Wilcoxon, Holm within H4)",
            "plan_clause": plan_v, "online_clause": online_v, "verdict": overall, "plan": plan, "online": onl}


# --------------------------------------------------------------------------------------------------------------------
# Ablations (figure 8)
# --------------------------------------------------------------------------------------------------------------------


def ablation_comparisons() -> list[common.Comparison]:
    """The registry's ablation family plus each variant vs full on eviction_cost in its own family."""
    comps = common.comparisons("abl")
    e = common.experiment("abl")
    ref = e["params"]["reference_label"]
    template = comps[0] if comps else common.Comparison("abl", "", "ablation", "wilcoxon", H1_METRIC, "", ref)
    covered = {(common.describe_arm(c.a), c.metric) for c in comps}
    extra = [dataclasses.replace(template, name=f"{v} vs {ref}, {m}", family="ablation-eviction-cost", metric=m,
                                 a=v, b=ref, expect=None, where={})
             for v in e["labels"] if v != ref for m in ABL_METRICS if (v, m) not in covered]
    return comps + extra


ABL_COLUMNS = ["scenario", "nodes", "configuration", "metric", "family", "n_runs", "n_valid", "n_invalid",
               "mean", "mean_lo", "mean_hi", "median", "median_lo", "median_hi", *PAIR_COUNT_COLUMNS,
               "diff_mean", "diff_mean_lo", "diff_mean_hi", "diff_median", "diff_median_lo", "diff_median_hi",
               "diff_pct", "diff_pct_lo", "diff_pct_hi", "p_wilcoxon", "p_holm", "p_holm_all", "significant",
               "direction", "outcome"]


def ablation_table(sel: pd.DataFrame) -> pd.DataFrame:
    """Figure 8 as a long table: one row per (scenario, nodes, configuration, metric) of the design, full first.

    mean/median (95% bootstrap CIs) are over the configuration's valid runs; diff_* are the paired differences
    variant - full on shared valid seeds (percent of full's mean over those seeds), with the Wilcoxon p, Holm within
    the family (``ablation``: the preregistered steady-state cost, integrated cost and evictions; eviction cost in
    ``ablation-eviction-cost``) and ``p_holm_all`` (Holm over all of the table's tests, a sensitivity check).
    """
    e = common.experiment("abl")
    ref = e["params"]["reference_label"]
    tests = comparison_table(sel, "abl", ablation_comparisons())
    tests["p_holm_all"] = common.holm(pd.to_numeric(tests["p"], errors="coerce").to_numpy(dtype=float))
    summary = common.summarize(sel.loc[sel["label"].isin(e["labels"])], ABL_METRICS,
                               by=("scenario", "nodes", "label"))
    rows = []
    for nodes, scenario, label, metric in itertools.product(e["nodes"], e["scenarios"], e["labels"], ABL_METRICS):
        rec: dict[str, Any] = {c: math.nan for c in ABL_COLUMNS}
        rec.update(scenario=scenario, nodes=nodes, configuration=label, metric=metric, n_runs=0, n_valid=0,
                   n_invalid=0, family="reference" if label == ref else "")
        s = summary.loc[(summary["scenario"] == scenario) & (summary["nodes"] == nodes) & (summary["label"] == label)]
        if len(s):
            s0 = s.iloc[0]
            rec.update(n_runs=s0["n_runs"], n_valid=s0["n_valid"], n_invalid=s0["n_invalid"])
            for stat in ("mean", "median"):
                for suffix in ("", "_lo", "_hi"):
                    rec[f"{stat}{suffix}"] = s0[f"{metric}_{stat}{suffix}"]
        t = tests.loc[(tests["a"] == label) & (tests["b"] == ref) & (tests["metric"] == metric)
                      & (tests["scenario"] == scenario) & (tests["nodes"] == nodes)]
        if len(t):
            t0 = t.iloc[0]
            rec.update(family=t0["family"], **{c: t0[c] for c in PAIR_COUNT_COLUMNS},
                       p_wilcoxon=t0["p_wilcoxon"], p_holm=t0["p_holm"], p_holm_all=t0["p_holm_all"],
                       significant=bool(t0["significant"]), direction=t0["direction"], outcome=t0["outcome"])
            for name in ("mean", "median", "pct"):
                for suffix in ("", "_lo", "_hi"):
                    rec[f"diff_{name}{suffix}"] = t0[f"{name}_diff{suffix}"]
        rows.append(rec)
    return pd.DataFrame(rows, columns=ABL_COLUMNS)


def analyse_ablation(sel: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write fig8_ablation (table and figure); return each variant's effect per scenario and metric."""
    table = tidy(ablation_table(sel))
    common.write_table(table, out_root, "fig8_ablation")
    ablation_figure(table, out_root)
    common.logger.info("wrote fig8_ablation (table and figure)")
    effects = table.loc[table["family"] != "reference"]
    cols = ["scenario", "nodes", "configuration", "metric", "family", "n_pairs", "n_dropped_invalid", "diff_mean",
            "diff_pct", "diff_pct_lo", "diff_pct_hi", "p_wilcoxon", "p_holm", "significant", "outcome"]
    return {"tables": ["fig8_ablation"], "figures": ["fig8_ablation"],
            "reference": common.experiment("abl")["params"]["reference_label"],
            "effects": effects.reindex(columns=cols)}


def _fmt(v: Any, decimals: int, signed: bool = False) -> str:
    """A number for the figure: fixed decimals, thousands separators, a true minus sign."""
    if not _finite(v):
        return "–"
    text = f"{float(v):+,.{decimals}f}" if signed else f"{float(v):,.{decimals}f}"
    return text.replace("-", MINUS)


def _ablation_cell(r: Mapping[str, Any], reference: bool) -> str:
    """Two lines: the configuration's mean, then the reference's 95% CI or the variant's paired difference.

    The difference is a percent of full's mean (whole percents from 10% up, else one decimal), or absolute when
    full's mean is 0; CI bounds carry a sign only when negative, and a star marks Holm-adjusted p < 0.05.
    """
    d = ABL_DECIMALS[r["metric"]]
    if not _finite(r["mean"]):
        return "\u2013"
    first = _fmt(r["mean"], d)
    if reference:
        return f"{first}\n[{_fmt(r['mean_lo'], d)}, {_fmt(r['mean_hi'], d)}]"
    if _finite(r["diff_pct"]):
        size = max(abs(float(r[k])) for k in ("diff_pct", "diff_pct_lo", "diff_pct_hi") if _finite(r[k]))
        pd_ = 0 if size >= 10 else 1
        delta = (f"\u0394 {_fmt(r['diff_pct'], pd_, True)}% "
                 f"[{_fmt(r['diff_pct_lo'], pd_)}, {_fmt(r['diff_pct_hi'], pd_)}]")
    elif _finite(r["diff_mean"]):
        delta = (f"\u0394 {_fmt(r['diff_mean'], d, True)} "
                 f"[{_fmt(r['diff_mean_lo'], d)}, {_fmt(r['diff_mean_hi'], d)}]")
    else:
        delta = "\u0394 \u2013"
    significant = isinstance(r["significant"], (bool, np.bool_)) and bool(r["significant"])
    return f"{first}\n{delta}{'*' if significant else ''}"


def _ablation_rows(table: pd.DataFrame) -> list[tuple[str, list[str]]]:
    """The figure's rows in order: ("section", [scenario heading]) then ("body", [configuration, metric cells])."""
    e = common.experiment("abl")
    ref = e["params"]["reference_label"]
    rows: list[tuple[str, list[str]]] = []
    for nodes, scenario in itertools.product(e["nodes"], e["scenarios"]):
        rows.append(("section", [f"{scenario} ({nodes} nodes)"]))
        for label in e["labels"]:
            cells = [f"{label} (reference)" if label == ref else label]
            for m in ABL_METRICS:
                r = table.loc[(table["scenario"] == scenario) & (table["nodes"] == nodes)
                              & (table["configuration"] == label) & (table["metric"] == m)]
                cells.append(_ablation_cell(r.iloc[0].to_dict(), label == ref) if len(r) else "\u2013")
            rows.append(("body", cells))
    return rows


def _ablation_note(table: pd.DataFrame) -> str:
    valid = pd.to_numeric(table["n_valid"], errors="coerce").dropna().astype(int)
    valid = valid[valid > 0]
    n_text = ("no valid runs yet" if valid.empty else f"n = {valid.min()}" if valid.min() == valid.max()
              else f"n = {valid.min()}\u2013{valid.max()}")
    return (f"Mean over valid runs per configuration ({n_text} seeds). Reference rows: 95% bootstrap CI of the mean. "
            "Variant rows: \u0394 is the mean paired difference vs full on shared seeds as % of full's mean "
            "(absolute when full's mean is 0), with its 95% bootstrap CI; * Holm-adjusted paired Wilcoxon p < 0.05 "
            "(steady-state cost, cost after warmup and evictions: the preregistered ablation family; eviction cost: "
            "its own family).")


def ablation_figure(table: pd.DataFrame, out_root: Path, name: str = "fig8_ablation") -> None:
    """Figure 8: the ablation table rendered as a booktabs-style figure (figures/out/<name>.png and .pdf).

    One section per scenario, full then each variant; columns: the four metrics. A cell shows the mean over valid
    runs, then (full) its 95% bootstrap CI or (variant) the mean paired difference vs full as percent of full's mean
    with its CI, starred when the Holm-adjusted Wilcoxon p is below 0.05. Text is in the neutral ink only; the star,
    not color, carries significance.
    """
    rows = _ablation_rows(table)
    header = ["Configuration", *(ABL_HEADERS[m] for m in ABL_METRICS)]
    widths = [0.16, 0.21, 0.21, 0.21, 0.21]
    align = ["left", "right", "right", "right", "right"]
    heights = {"header": 0.36, "section": 0.22, "body": 0.36}  # inches
    foot_h = 0.48
    table_h = heights["header"] + sum(heights[kind] for kind, _ in rows)
    width, height = common.FIGSIZE["double"][0], table_h + foot_h
    last_body = max(i for i, (kind, _) in enumerate(rows) if kind == "body")
    # The paper style turns on constrained layout, which would re-engage at save time and warn about these
    # hand-placed axes; this figure positions everything itself.
    with plt.rc_context({"figure.constrained_layout.use": False}):
        fig = plt.figure(figsize=(width, height), layout="none")
        ax = fig.add_axes((0.0, foot_h / height, 1.0, table_h / height - 0.005))
        ax.set_axis_off()
        tbl = Table(ax, bbox=(0, 0, 1, 1))
        for c, text in enumerate(header):
            cell = tbl.add_cell(0, c, widths[c], heights["header"], text=text, loc=align[c], facecolor="white",
                                edgecolor=common.NEUTRAL)
            cell.visible_edges = "TB"
            cell.set_linewidth(0.8)
            cell.get_text().set_fontweight("bold")
        for i, (kind, cells) in enumerate(rows):
            block_end = i == len(rows) - 1 or rows[i + 1][0] == "section"
            for c in range(len(header)):
                text = cells[c] if c < len(cells) else ""
                cell = tbl.add_cell(i + 1, c, widths[c], heights[kind], text=text, loc=align[c] if kind == "body"
                                    else "left", facecolor="white",
                                    edgecolor=common.NEUTRAL if i == last_body else common.GRID)
                cell.visible_edges = "B" if kind == "body" and block_end else "open"
                cell.set_linewidth(0.8 if i == last_body else 0.6)
                if kind == "section":
                    cell.get_text().set_fontweight("bold")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(7.5)
        for cell in tbl.get_celld().values():
            cell.get_text().set_color(common.NEUTRAL)
        ax.add_table(tbl)
        fig.text(0.005, 0.02, "\n".join(textwrap.wrap(_ablation_note(table), 150)), ha="left", va="bottom",
                 fontsize=6.5, color=common.NEUTRAL)
        common.save_figure(fig, out_root, name)


if __name__ == "__main__":
    common.cli(main)
