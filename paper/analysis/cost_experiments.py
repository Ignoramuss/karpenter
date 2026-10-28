"""Exp 1 (H1), the realism check, Exp 3 (H3), Exp 4 (H4), the ablations (figure 8) and the B4 parity check: what each
consolidation configuration costs.

Run through ``paper/make_figures.py`` (``--only cost_experiments``) or on its own::

    /opt/research-venv/bin/python -m paper.analysis.cost_experiments [--runs experiments/runs] [--out paper]

Outputs under ``out_root`` (each written only when its experiment has runs; otherwise "skipped <exp>: no runs"):

- ``tables/exp1_h1``: the H1 equivalence tests. One row per paired comparison random/<planner> vs
  heuristic/<planner> on steady_cost_per_hour, per scenario and scale (family ``H1``: entropy-greedy and anneal-plan,
  the 12 preregistered TOSTs; family ``H1-P0-contrast``: savings-only, 6), then ``reference`` rows for B2 and B3.
- ``tables/exp1_secondary``: per arm (scenario, nodes, label) and metric, the secondary metrics with bootstrap CIs, and
  for random arms the paired difference against the heuristic arm with the same planner (descriptive, no tests).
- ``tables/exp1_phases``: per arm, mean cost_per_hour in the early phase (first hour after warmup) and the late phase
  (samples with t >= 0.8 x horizon, the simulator's steady-state window), with the same paired differences.
- ``tables/realism_h1`` and ``realism_secondary``: the same on W5 and W6 (family ``H1-realism``, 4 TOSTs).
- ``tables/exp3_h3``: merge+shift vs merge-only per scenario and eviction budget on integrated_cost_post (family
  ``H3``, 4 tests); ``tables/exp3_secondary``: evictions, eviction cost and the rest, descriptive.
- ``tables/exp4_h4``: the 8 tests of family ``H4``; ``tables/exp4_clauses``: the plan and online clauses (test plus
  bootstrap bound) per scenario and configuration; ``tables/exp4_sweep``: the temperature/cooling sweep (descriptive).
- ``tables/fig8_ablation`` and ``figures/out/fig8_ablation``: full vs each ablated variant, per scenario (family
  ``ablation``: steady-state cost, 8 tests; the other metrics descriptive).
- ``tables/b4_parity``: B4 vs B2 and B4-balanced vs B3 per scenario: seeds whose summaries match exactly, and the
  6 TOSTs of family ``B4``.
- ``tables/cost_experiments.json``: the headline verdicts per experiment, with validity counts.

Statistical conventions (experiments/PREREGISTRATION.md governs; the shared rules are in ``common``):

- Every Holm family has its preregistered size (a test that cannot run enters with p 1) and records each test's
  attainable floor ``p_floor`` and ``holm_reachable`` (``p_floor * m < 0.05``). A non-significant test that could not
  have been significant is "inconclusive", and a clause resting on it is never support.
- Configurations with fewer than ``common.min_seeds()`` finished runs are cut (``common.select_runs``); while any
  configuration a verdict needs is missing or cut, the verdict is "incomplete" (``verdict_on_available`` says what
  the rule gives on the runs so far) and ``missing_configurations`` lists them.
- Comparison tables (exp1_h1, exp3_h3, exp4_h4) give each arm's mean and median over the kept pairs, so they agree
  with the paired difference and the TOST margin; per-arm tables (exp1_secondary, exp1_phases, exp4_sweep,
  fig8_ablation) use every valid run of the arm.
- The direction of a Wilcoxon result is the sign of W+ - W- (the signed-rank statistic the test is about), not the
  sign of the mean difference; the two only disagree when a few large differences outweigh many small ones.
- ``outcome`` is a's position relative to b after Holm (``common.comparison_outcome``); ``expect_met`` checks it
  against the registry's ``expect``.
- Rows exist for every preregistered stratum even before its runs do (``n_pairs`` 0, outcome "no data").
"""

from __future__ import annotations

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

from paper.analysis import common

H1_METRIC = "steady_cost_per_hour"
# Exp 1 secondary metrics: the brief's list, then the registry's remaining secondary metrics.
EXP1_SECONDARY = ["integrated_cost_post", "evictions", "eviction_cost", "nodes_removed", "regret_evictions",
                  "re_disruptions", "full_simulations_per_pass", "wall_pass_mean_ms", "cpu_pass_mean_ms",
                  common.WALL_CPU_RATIO, "pending_pod_seconds", "mean_time_to_schedule_s"]
EXP4_SWEEP_METRICS = ["steady_cost_per_hour", "evictions", "eviction_cost", "integrated_cost_post", "final_nodes"]
ABL_METRICS = ["steady_cost_per_hour", "integrated_cost_post", "evictions", "eviction_cost"]
ABL_HEADERS = {"steady_cost_per_hour": "Steady-state\ncost ($/hr)", "integrated_cost_post": "Cost after\nwarmup ($)",
               "evictions": "Pod\nevictions", "eviction_cost": "Eviction\ncost"}
ABL_DECIMALS = {"steady_cost_per_hour": 2, "integrated_cost_post": 1, "evictions": 0, "eviction_cost": 0}
FAMILY_ORDER = ["H1", "H1-P0-contrast", "H1-realism", "reference", "H3", "H4", "ablation", "descriptive", "B4"]
MINUS = "−"

ARM_STAT_COLUMNS = ["mean_a", "mean_a_lo", "mean_a_hi", "median_a", "median_a_lo", "median_a_hi",
                    "mean_b", "mean_b_lo", "mean_b_hi", "median_b", "median_b_lo", "median_b_hi"]
EFFECT_COLUMNS = ["mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo", "median_diff_hi",
                  "pct_diff", "pct_diff_lo", "pct_diff_hi"]
PAIR_COUNT_COLUMNS = ["n_pairs", "n_dropped_invalid", "n_undefined", "n_dropped_missing"]
HOLM_COLUMNS = ["p", "p_floor", "m", "p_holm", "significant", "holm_reachable"]
H1_COLUMNS = [
    "family", "comparison", "scenario", "nodes", "planner", "a", "b", *PAIR_COUNT_COLUMNS, "n_runs", "n_invalid",
    *ARM_STAT_COLUMNS, *EFFECT_COLUMNS, "margin", "margin_pct", "ci90_lo", "ci90_hi", "ci90_lo_pct", "ci90_hi_pct",
    "p_lower", "p_upper", "p_tost", "p_floor", "m", "p_tost_holm", "holm_reachable", "equivalent", "equivalent_holm",
    "vetoed", "p_upper_holm", "noninferior", "noninferior_holm", "outcome",
]
WILCOXON_COLUMNS = ["family", "comparison", "metric", "scenario", "nodes", "a", "b", "expect", *PAIR_COUNT_COLUMNS,
                    *ARM_STAT_COLUMNS, *EFFECT_COLUMNS, "p_wilcoxon", *HOLM_COLUMNS[1:], "direction", "outcome",
                    "expect_met"]
ARM_TABLE_COLUMNS = ["scenario", "nodes", "label", "metric", "n_runs", "n_valid", "n_invalid", "mean", "mean_lo",
                     "mean_hi", "median", "median_lo", "median_hi", "ref", *PAIR_COUNT_COLUMNS, "diff_mean",
                     "diff_mean_lo", "diff_mean_hi", "diff_median", "diff_median_lo", "diff_median_hi", "diff_pct",
                     "diff_pct_lo", "diff_pct_hi"]


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Write the Exp 1, realism, Exp 3, Exp 4, ablation and B4 tables, figure 8 and cost_experiments.json (see the
    module docstring).

    Experiments without runs are skipped with a note and contribute nothing; the JSON holds one entry per experiment
    that has runs and is not written when none has.
    """
    runs = prepare_runs(common.load_runs(Path(runs_root), include_abandoned=True))
    headline: dict[str, Any] = {}
    for key, analyse in (("exp1", analyse_exp1), ("realism", analyse_realism), ("exp3", analyse_exp3),
                         ("exp4", analyse_exp4), ("abl", analyse_ablation), ("b4", analyse_b4)):
        everything = common.select_runs(runs, key, include_cut=True)
        own = everything.loc[everything["label"].isin(common.experiment(key)["labels"])]
        if own.empty:
            common.skipped(key)
            continue
        sel = common.select_runs(runs, key)
        common.logger.info("%s: %d runs (%d invalid or abandoned, %d in cut configurations)", key, len(everything),
                           int((~everything["valid"]).sum()), len(everything) - len(sel))
        headline[key] = {"title": common.experiment(key)["title"],
                         "hypothesis": common.experiment(key)["hypothesis"],
                         "validity": validity_summary(everything), **analyse(sel, everything, Path(out_root))}
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


def validity_summary(everything: pd.DataFrame) -> dict[str, Any]:
    """Run counts for an experiment (the preregistered exclusion report), totals and per group: every reason counted
    on its own, set-aside attempts, and runs in cut configurations (``common.validity_report``)."""
    valid = everything["valid"].astype(bool)
    report = common.validity_report(everything, by=("group",))
    totals = {c: int(report[c].sum()) for c in common.VALIDITY_COLUMNS if c != "reasons"}
    return {**totals, "n_valid": int(valid.sum()), "n_invalid": int((~valid).sum()), "by_group": report}


def _verdict_block(rule: str, verdict: str, missing: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """A verdict entry: "incomplete" while configurations are missing (with the rule's verdict on the runs so far)."""
    return {"rule": rule, "verdict": "incomplete" if missing else verdict, "verdict_on_available": verdict,
            "complete": not missing, "missing_configurations": missing, **extra}


# --------------------------------------------------------------------------------------------------------------------
# Paired comparisons
# --------------------------------------------------------------------------------------------------------------------


def _pair_extras(sel: pd.DataFrame, c: common.Comparison, stratum: Mapping[str, Any]) -> dict[str, Any]:
    """Per-arm mean and median with 95% bootstrap CIs over the kept pairs."""
    part = sel
    for col, allowed in c.where.items():
        part = part.loc[part[col].isin(allowed)]
    for col, v in stratum.items():
        part = part.loc[part[col] == v]
    p = common.paired_lenient(part, c.keys, c.label_col, c.a, c.b, c.metric, log=False)
    out: dict[str, Any] = {}
    for side, x in (("a", p.a), ("b", p.b)):
        for stat in ("mean", "median"):
            est, lo, hi = common.bootstrap_ci(x, stat)
            out.update({f"{stat}_{side}": est, f"{stat}_{side}_lo": lo, f"{stat}_{side}_hi": hi})
    return out


def comparison_table(sel: pd.DataFrame, comps: Sequence[common.Comparison]) -> pd.DataFrame:
    """Every comparison of ``comps`` per preregistered stratum (``common.evaluate``: fixed-size Holm families, floors,
    reachability, outcome), with the per-arm CIs (``_pair_extras``), the TOST veto (``vetoed``: n >= 2 and the
    unadjusted 90% CI lower bound above +margin), the margin and 90% CI as percent of b's mean, and ``order`` (the
    comparison's position in ``comps``)."""
    out = common.evaluate(sel, comps)
    if out.empty:
        return out
    by_name = {c.name + "|" + c.family: c for c in comps}
    order = {c.name + "|" + c.family: i for i, c in enumerate(comps)}
    extras = []
    for r in out.to_dict("records"):
        c = by_name[r["comparison"] + "|" + r["family"]]
        extras.append(_pair_extras(sel, c, {b: r[b] for b in c.by}))
    for col in ARM_STAT_COLUMNS:
        out[col] = [e[col] for e in extras]
    out["order"] = [order[n + "|" + f] for n, f in zip(out["comparison"], out["family"])]
    n_pairs = pd.to_numeric(out["n_pairs"], errors="coerce").fillna(0).to_numpy()
    lo, margin = (pd.to_numeric(out[c], errors="coerce").to_numpy(dtype=float) for c in ("ci90_lo", "margin"))
    tost = (out["test"] == "tost").to_numpy()
    with np.errstate(invalid="ignore"):
        out["vetoed"] = pd.Series([bool(n >= 2 and v > mg) if t else None for n, v, mg, t in
                                   zip(n_pairs, lo, margin, tost)], dtype=object)
    ref = pd.to_numeric(out["mean_b"], errors="coerce").to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        for col in ("margin", "ci90_lo", "ci90_hi"):
            pct = 100 * pd.to_numeric(out[col], errors="coerce").to_numpy(dtype=float) / np.abs(ref)
            out[f"{col}_pct"] = np.where(np.isfinite(pct), pct, math.nan)
    return out


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float, np.number)) and not isinstance(v, bool) and math.isfinite(float(v))


COUNT_COLUMNS = ("nodes", "n_pairs", "n_dropped_invalid", "n_undefined", "n_dropped_missing", "n_runs", "n_valid",
                 "n_invalid", "m", "n_seeds", "n_paired", "n_match")


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


def _stratum_rows(df: pd.DataFrame, values: Mapping[str, Any]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, v in values.items():
        mask &= df[col] == v
    return df.loc[mask]


def _row(table: pd.DataFrame, **match: Any) -> dict[str, Any] | None:
    """The single row of ``table`` matching every column = value, as a dict (None when absent)."""
    mask = pd.Series(True, index=table.index)
    for col, v in match.items():
        mask &= table[col] == v
    hit = table.loc[mask]
    return hit.iloc[0].to_dict() if len(hit) else None


# --------------------------------------------------------------------------------------------------------------------
# Per-arm tables
# --------------------------------------------------------------------------------------------------------------------


def arm_table(df: pd.DataFrame, key: str, metrics: Sequence[str], refs: Mapping[str, str]) -> pd.DataFrame:
    """Long per-arm table: one row per (scenario, nodes, label, metric) present in ``df``, for the experiment's labels
    and baselines in registry order.

    Columns: n_runs, n_valid, n_invalid; mean and median with 95% bootstrap CIs over the arm's valid runs; and, for a
    label in ``refs``, the paired difference against ``refs[label]`` on the same seeds (mean and median with CIs,
    percent of the reference mean with CI). Descriptive: no tests.
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
                p = common.paired_lenient(part, keys, "label", s["label"], ref, m,
                                          where=f"{key} {s['scenario']}/{s['nodes']} {m}")
                eff = common.paired_effect(p.a, p.b)
                rec.update(ref=ref, n_pairs=p.n, n_dropped_invalid=p.dropped_invalid, n_undefined=p.dropped_undefined,
                           n_dropped_missing=p.dropped_missing)
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
# Exp 1 (H1) and the realism check
# --------------------------------------------------------------------------------------------------------------------


def _tost_table(sel: pd.DataFrame, key: str) -> pd.DataFrame:
    """An experiment's TOST comparisons (``comparison_table``) in the H1 table's columns, plus reference rows."""
    e = common.experiment(key)
    tests = comparison_table(sel, common.comparisons(key))
    tests["planner"] = tests["a"].astype(str).str.split("/").str[-1]
    tests = tests.rename(columns={"p_holm": "p_tost_holm"})
    table = pd.concat([tests.reindex(columns=H1_COLUMNS), reference_rows(sel, e)], ignore_index=True)
    planners = [lab.split("/", 1)[1] for lab in e["labels"] if lab.startswith("heuristic/")]
    return tidy(_sort(table, [("family", FAMILY_ORDER), ("nodes", None), ("scenario", e["scenarios"]),
                              ("planner", planners), ("a", e["baselines"])]))


def analyse_exp1(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp1_h1, exp1_secondary and exp1_phases; return the H1 verdict, the P0 contrast and the phase check."""
    e = common.experiment("exp1")
    params = e["params"]
    h1 = _tost_table(sel, "exp1")
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
    comps = common.comparisons("exp1")
    h1_comps = [c for c in comps if c.family == "H1"]
    missing = common.missing_configurations(everything, h1_comps)
    return {"tables": ["exp1_h1", "exp1_secondary"] + ([] if phases.empty else ["exp1_phases"]),
            "h1": h1_verdict(h1.loc[h1["family"] == "H1"], missing, int(params["h1_tests"]),
                             int(params["h1_min_equivalent"])),
            "p0_contrast": tost_counts(h1.loc[h1["family"] == "H1-P0-contrast"])
            | {"tests": _tost_brief(h1.loc[h1["family"] == "H1-P0-contrast"])},
            "phases": check}


def analyse_realism(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write realism_h1 and realism_secondary: Exp 1's entropy-guided random vs heuristic TOSTs on W5 and W6, a
    realism check reported the same way as H1 in its own Holm family (it does not enter H1's verdict)."""
    e = common.experiment("realism")
    table = _tost_table(sel, "realism")
    common.write_table(table, out_root, "realism_h1")
    refs = generation_refs(e["labels"])
    metrics = EXP1_SECONDARY + [m for m in e["secondary_metrics"] if m not in EXP1_SECONDARY]
    common.write_table(tidy(arm_table(sel, "realism", metrics, refs)), out_root, "realism_secondary")
    rows = table.loc[table["family"] == "H1-realism"]
    missing = common.missing_configurations(everything, common.comparisons("realism"))
    return {"tables": ["realism_h1", "realism_secondary"],
            "tost": tost_counts(rows) | {"complete": not missing, "missing_configurations": missing,
                                         "tests": _tost_brief(rows)}}


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
    """Counts over a family of TOST rows: tests, tests with a TOST (at least two pairs; the rest enter Holm with
    p 1), equivalent (raw and after Holm), vetoed (unadjusted 90% CI lower bound above +margin), non-inferior (raw
    and after Holm), random better beyond the margin (90% CI entirely below -margin), and dropped pairs."""
    n_pairs = pd.to_numeric(rows["n_pairs"], errors="coerce").fillna(0)
    tested = rows.loc[n_pairs >= 2]
    return {
        "n_tests": int(len(rows)),
        "n_tests_with_data": int(len(tested)),
        "n_equivalent": int((tested["equivalent"] == True).sum()),  # noqa: E712 - object column
        "n_equivalent_holm": int((rows["equivalent_holm"] == True).sum()),  # noqa: E712
        "n_vetoed": int((rows["vetoed"] == True).sum()),  # noqa: E712
        "n_noninferior": int((tested["noninferior"] == True).sum()),  # noqa: E712
        "n_noninferior_holm": int((rows["noninferior_holm"] == True).sum()),  # noqa: E712
        "n_better_beyond_margin": int((pd.to_numeric(tested["ci90_hi"], errors="coerce")
                                       < -pd.to_numeric(tested["margin"], errors="coerce")).sum()),
        "n_pairs_dropped_invalid": int(pd.to_numeric(rows["n_dropped_invalid"], errors="coerce").fillna(0).sum()),
        "n_undefined": int(pd.to_numeric(rows["n_undefined"], errors="coerce").fillna(0).sum()),
    }


def _tost_brief(rows: pd.DataFrame) -> pd.DataFrame:
    cols = ["scenario", "nodes", "planner", "n_pairs", "n_dropped_invalid", "n_undefined", "mean_a", "mean_b",
            "pct_diff", "pct_diff_lo", "pct_diff_hi", "margin_pct", "ci90_lo_pct", "ci90_hi_pct", "p_tost",
            "p_tost_holm", "equivalent_holm", "vetoed", "noninferior", "noninferior_holm", "outcome"]
    return rows.reindex(columns=cols).reset_index(drop=True)


def h1_verdict(rows: pd.DataFrame, missing: list[dict[str, Any]], n_expected: int,
               min_equivalent: int) -> dict[str, Any]:
    """The preregistered H1 decision over the 12 tests of family H1 (Holm over 12 fixed; a test with fewer than two
    valid pairs is not equivalent and enters Holm with p_TOST = 1): supported iff at least ``min_equivalent`` are
    equivalent (Holm-adjusted p_TOST < 0.05) and none is vetoed (unadjusted 90% CI lower bound of the mean paired
    difference above +margin); otherwise not supported. "incomplete" only while session runs are missing (an H1
    configuration with fewer than MIN_SEEDS finished runs, or none)."""
    counts = tost_counts(rows)
    if len(rows) != n_expected:
        raise ValueError(f"H1 has {len(rows)} tests, want {n_expected}")
    ok = counts["n_equivalent_holm"] >= min_equivalent and counts["n_vetoed"] == 0
    return _verdict_block(
        f"supported iff >= {min_equivalent} of {n_expected} TOSTs are equivalent after Holm (90% CI of the mean "
        "paired difference inside +-3% of the heuristic mean; Holm over the 12, p 1 for a test with < 2 pairs) and "
        "none is vetoed (unadjusted 90% CI lower bound above +margin)",
        "supported" if ok else "not supported", missing,
        n_tests_expected=n_expected, min_equivalent=min_equivalent, **counts, tests=_tost_brief(rows))


def run_phases(ts: pd.DataFrame, early: Sequence[float], late_from_min: float) -> dict[str, float]:
    """Mean cost_per_hour of one run's timeseries in the early window [early[0], early[1]) and in the late window,
    the samples with t >= ``late_from_min`` (0.8 x the horizon: the simulator's steady-state rule, so late equals the
    summary's steady_cost_per_hour). Also returns where the late window starts and the last sample time."""
    t = pd.to_numeric(ts["t_min"], errors="coerce").to_numpy(dtype=float)
    cost = pd.to_numeric(ts["cost_per_hour"], errors="coerce").to_numpy(dtype=float)
    if len(cost) == 0:
        return {"early": math.nan, "late": math.nan, "late_from": math.nan, "t_end": math.nan}
    in_early = (t >= early[0]) & (t < early[1])
    in_late = t + 1e-9 >= late_from_min
    return {"early": float(cost[in_early].mean()) if in_early.any() else math.nan,
            "late": float(cost[in_late].mean()) if in_late.any() else math.nan,
            "late_from": float(t[in_late].min()) if in_late.any() else math.nan, "t_end": float(np.nanmax(t))}


def phase_table(sel: pd.DataFrame, key: str, refs: Mapping[str, str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Exp 1's early vs late phase table and a consistency check (late phase vs the summary's steady state).

    One row per (scenario, nodes, label, phase) in ``arm_table``'s columns (``metric`` renamed ``phase``: early or
    late), plus ``t_from_min``/``t_to_min``: the registry's early window ([10, 70) min) and, for late, the earliest
    start and the latest end of the runs' late windows (t >= 0.8 x the run's horizon, ``duration_min``, else its last
    sample). Only valid runs are read; invalid ones count in n_invalid.
    """
    e = common.experiment(key)
    params = e.get("params") or {}
    early = [float(x) for x in params.get("early_window_min", [10, 70])]
    late_fraction = float(params.get("late_from_fraction", 0.8))
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
                horizon = float(r["duration_min"]) if _finite(r.get("duration_min")) else float(
                    pd.to_numeric(ts["t_min"], errors="coerce").max())
                rec.update(run_phases(ts, early, late_fraction * horizon))
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
    if _finite(max_gap) and max_gap > 1e-5:
        common.logger.warning("exp1_phases: late-phase mean differs from steady_cost_per_hour by up to %.3g (runs "
                              "from simulators before the time-based steady-state rule)", max_gap)
    check = {"early_window_min": early, "late_from_fraction": late_fraction,
             "n_runs_read": int(per_run["late"].notna().sum()),
             "late_vs_steady_cost_max_abs_diff": max_gap}
    return table, check


# --------------------------------------------------------------------------------------------------------------------
# Exp 3 (H3)
# --------------------------------------------------------------------------------------------------------------------


def analyse_exp3(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp3_h3 (family H3) and exp3_secondary (descriptive) and return H3's verdict: supported iff all four
    tests are significant after Holm with merge+shift lower; partial if some are; otherwise inconclusive when a test
    could not reach significance, else not supported."""
    e = common.experiment("exp3")
    comps = common.comparisons("exp3")
    table = comparison_table(sel, comps)
    table["budget"] = table["a"].astype(str).str.rsplit("budget-", n=1).str[-1]
    budgets = list(e["params"].get("budgets_pods_per_minute", {}))
    table = tidy(_sort(table, [("nodes", None), ("scenario", e["scenarios"]), ("budget", budgets)]))
    cols = WILCOXON_COLUMNS[:5] + ["budget"] + WILCOXON_COLUMNS[5:]
    common.write_table(table.reindex(columns=cols), out_root, "exp3_h3")
    refs = {lab: lab.replace("merge+shift", "merge-only") for lab in e["labels"] if lab.startswith("merge+shift")}
    metrics = [e["primary_metric"], *e["params"].get("descriptive_metrics", [])]
    common.write_table(tidy(arm_table(sel, "exp3", metrics, refs)), out_root, "exp3_secondary")
    common.logger.info("wrote exp3_h3, exp3_secondary")
    n_lower = int((table["outcome"] == "lower").sum())
    if n_lower == len(table):
        verdict = "supported"
    elif n_lower:
        verdict = "partial"
    elif (table["outcome"].isin(common.UNINFORMATIVE_OUTCOMES)).any():
        verdict = "inconclusive"
    else:
        verdict = "not supported"
    brief = ["scenario", "nodes", "budget", "n_pairs", "n_dropped_invalid", "n_undefined", "mean_a", "mean_b",
             "mean_diff", "mean_diff_lo", "mean_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon",
             "p_floor", "p_holm", "holm_reachable", "outcome", "expect_met"]
    missing = common.missing_configurations(everything, comps)
    return {"tables": ["exp3_h3", "exp3_secondary"],
            "h3": _verdict_block("per scenario and budget: merge+shift lower on integrated_cost_post, significant "
                                 "after Holm over the four (paired Wilcoxon); supported iff all four, partial if "
                                 "some", verdict, missing, n_tests=int(len(table)), n_lower=n_lower,
                                 tests=table.reindex(columns=brief))}


# --------------------------------------------------------------------------------------------------------------------
# Exp 4 (H4)
# --------------------------------------------------------------------------------------------------------------------


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


CLAUSE_COLUMNS = ["clause", "scenario", "nodes", "configuration", "test_metric", "test_outcome", "test_p_holm",
                  "test_holm_reachable", "bound_metric", "n_pairs", "pct_diff", "pct_diff_lo", "pct_diff_hi", "bound",
                  "limit", "bound_ok", "state"]


def _clause(kind: str, test: Mapping[str, Any] | None, want: str, bound_metric: str, pct: tuple[float, float, float],
            n_pairs: int, bound_side: str, limit: float) -> dict[str, Any]:
    """One H4 clause: its Wilcoxon test (in family H4) must come out ``want`` and the paired bootstrap 95% bound of the
    percent difference in ``bound_metric`` must lie on the right side of ``limit`` (``bound_side`` "hi": the upper
    bound below the limit; "lo": the lower bound above it). ``state``: "holds", "fails", or "inconclusive" when the
    test is unreachable (could not have been significant after Holm), missing, or the bound undefined."""
    est, lo, hi = pct
    bound = hi if bound_side == "hi" else lo
    bound_ok = None if not math.isfinite(bound) else bool(bound < limit if bound_side == "hi" else bound > limit)
    outcome = test["outcome"] if test else "no data"
    reachable = bool(test["holm_reachable"]) if test else False
    if not reachable or outcome in common.UNINFORMATIVE_OUTCOMES:
        state = "inconclusive"
    elif outcome == want and bound_ok:
        state = "holds"
    elif outcome == want and bound_ok is None:
        state = "inconclusive"
    else:
        state = "fails"
    return {"clause": kind, "test_metric": test["metric"] if test else math.nan, "test_outcome": outcome,
            "test_p_holm": test["p_holm"] if test else math.nan, "test_holm_reachable": reachable,
            "bound_metric": bound_metric, "n_pairs": n_pairs, "pct_diff": est, "pct_diff_lo": lo, "pct_diff_hi": hi,
            "bound": f"{'upper' if bound_side == 'hi' else 'lower'} 95% bound {'<' if bound_side == 'hi' else '>'}",
            "limit": limit, "bound_ok": bound_ok, "state": state}


def h4_clauses(sel: pd.DataFrame, tests: pd.DataFrame, e: Mapping[str, Any]) -> pd.DataFrame:
    """The H4 clauses (``CLAUSE_COLUMNS``), per scenario: the plan clause (default anneal-plan vs entropy-greedy:
    steady-state cost significantly lower, and the paired bootstrap 95% upper bound of the percent difference in
    evictions below +``plan_evictions_max_pct``) and per anneal-online configuration the online clause (evictions
    significantly higher, and the bootstrap 95% lower bound of the percent difference in steady-state cost above
    ``online_cost_min_pct``)."""
    params = e["params"]
    keys = e.get("pair_keys") or common.PAIR_KEYS
    greedy, default = params["greedy_label"], params["default_anneal_label"]
    rows = []
    for nodes, scenario in itertools.product(e["nodes"], e["scenarios"]):
        part = _stratum_rows(sel, {"scenario": scenario, "nodes": nodes})
        specs = [("plan", default, "steady_cost_per_hour", "lower", "evictions", "hi",
                  float(params["plan_evictions_max_pct"]))]
        specs += [("online", label, "evictions", "higher", "steady_cost_per_hour", "lo",
                   float(params["online_cost_min_pct"])) for label in params["online_labels"]]
        for kind, label, metric, want, bound_metric, side, limit in specs:
            test = _row(tests, a=label, b=greedy, metric=metric, scenario=scenario, nodes=nodes)
            p = common.paired_lenient(part, keys, "label", label, greedy, bound_metric, log=False)
            rec = _clause(kind, test, want, bound_metric, common.bootstrap_pct_ci(p.a, p.b), p.n, side, limit)
            rec.update(scenario=scenario, nodes=nodes, configuration=label)
            rows.append(rec)
    return pd.DataFrame(rows, columns=CLAUSE_COLUMNS)


def analyse_exp4(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write exp4_h4, exp4_clauses and exp4_sweep; return H4's verdict."""
    e = common.experiment("exp4")
    params = e["params"]
    configs = config_params(sel, e["labels"])
    comps = common.comparisons("exp4")
    table = comparison_table(sel, comps)
    table = table.merge(configs.rename(columns={"label": "a"}), on="a", how="left")
    table = tidy(_sort(table, [("order", None), ("nodes", None), ("scenario", e["scenarios"])]))
    cols = WILCOXON_COLUMNS[:7] + ["planner", "t0", "cooling"] + WILCOXON_COLUMNS[7:]
    common.write_table(table.reindex(columns=cols), out_root, "exp4_h4")
    clauses = h4_clauses(sel, table, e)
    common.write_table(tidy(clauses), out_root, "exp4_clauses")

    sweep = common.summarize(sel.loc[sel["label"].isin(e["labels"])], EXP4_SWEEP_METRICS,
                             by=("scenario", "nodes", "label"))
    sweep = sweep.merge(configs, on="label", how="left")
    sweep.insert(3, "default", sweep["label"] == params["default_anneal_label"])
    for i, col in enumerate(("planner", "t0", "cooling")):
        sweep.insert(4 + i, col, sweep.pop(col))
    sweep = tidy(_sort(sweep, [("nodes", None), ("scenario", e["scenarios"]), ("label", e["labels"])]))
    common.write_table(sweep, out_root, "exp4_sweep")
    common.logger.info("wrote exp4_h4, exp4_clauses, exp4_sweep")
    missing = common.missing_configurations(everything, comps)
    return {"tables": ["exp4_h4", "exp4_clauses", "exp4_sweep"], "default_anneal_label": params["default_anneal_label"],
            "h4": h4_verdict(clauses, missing)}


def h4_verdict(clauses: pd.DataFrame, missing: list[dict[str, Any]]) -> dict[str, Any]:
    """H4: supported iff the plan clause holds in both scenarios and the online clause in all six cells; partial if
    some clauses hold; otherwise inconclusive when a clause is (an unreachable test), else not supported."""
    states = clauses["state"].tolist()
    n_hold = states.count("holds")
    if n_hold == len(states) and states:
        verdict = "supported"
    elif n_hold:
        verdict = "partial"
    elif "inconclusive" in states:
        verdict = "inconclusive"
    else:
        verdict = "not supported"

    def summary(kind: str) -> dict[str, Any]:
        part = clauses.loc[clauses["clause"] == kind]
        s = part["state"].tolist()
        return {"n": len(s), "n_holds": s.count("holds"), "n_fails": s.count("fails"),
                "n_inconclusive": s.count("inconclusive")}

    return _verdict_block(
        "plan clause per scenario: default anneal-plan vs entropy-greedy has significantly lower steady-state cost "
        "(Wilcoxon, Holm within H4) and the paired bootstrap 95% upper bound of the percent difference in evictions "
        "below +10%; online clause per anneal-online t0 and scenario: significantly more evictions (Wilcoxon, H4) and "
        "the bootstrap 95% lower bound of the percent difference in steady-state cost above -1%; an unreachable test "
        "makes its clause inconclusive. Supported iff every clause holds, partial if some do",
        verdict, missing, plan_clause=summary("plan"), online_clause=summary("online"), clauses=clauses)


# --------------------------------------------------------------------------------------------------------------------
# Ablations (figure 8)
# --------------------------------------------------------------------------------------------------------------------

ABL_COLUMNS = ["scenario", "nodes", "configuration", "metric", "family", "n_runs", "n_valid", "n_invalid",
               "mean", "mean_lo", "mean_hi", "median", "median_lo", "median_hi", *PAIR_COUNT_COLUMNS,
               "diff_mean", "diff_mean_lo", "diff_mean_hi", "diff_median", "diff_median_lo", "diff_median_hi",
               "diff_pct", "diff_pct_lo", "diff_pct_hi", "p_wilcoxon", "p_floor", "m", "p_holm", "significant",
               "holm_reachable", "direction", "outcome"]


def ablation_table(sel: pd.DataFrame) -> pd.DataFrame:
    """Figure 8 as a long table: one row per (scenario, nodes, configuration, metric) of the design, full first.

    mean/median (95% bootstrap CIs) are over the configuration's valid runs; diff_* are the paired differences
    variant - full on shared valid seeds (percent of full's mean over those seeds). Steady-state cost rows of the
    variants carry the test of family ``ablation`` (Wilcoxon, Holm over the 8); the other metrics are descriptive
    (family "descriptive", no test); full's rows are the reference.
    """
    e = common.experiment("abl")
    ref = e["params"]["reference_label"]
    keys = e.get("pair_keys") or common.PAIR_KEYS
    tests = common.evaluate(sel, common.comparisons("abl"))
    summary = common.summarize(sel.loc[sel["label"].isin(e["labels"])], ABL_METRICS,
                               by=("scenario", "nodes", "label"))
    rows = []
    for nodes, scenario, label, metric in itertools.product(e["nodes"], e["scenarios"], e["labels"], ABL_METRICS):
        rec: dict[str, Any] = {c: math.nan for c in ABL_COLUMNS}
        rec.update(scenario=scenario, nodes=nodes, configuration=label, metric=metric, n_runs=0, n_valid=0,
                   n_invalid=0, family="reference" if label == ref else "descriptive")
        s = summary.loc[(summary["scenario"] == scenario) & (summary["nodes"] == nodes) & (summary["label"] == label)]
        if len(s):
            s0 = s.iloc[0]
            rec.update(n_runs=s0["n_runs"], n_valid=s0["n_valid"], n_invalid=s0["n_invalid"])
            for stat in ("mean", "median"):
                for suffix in ("", "_lo", "_hi"):
                    rec[f"{stat}{suffix}"] = s0[f"{metric}_{stat}{suffix}"]
        if label != ref:
            part = _stratum_rows(sel, {"scenario": scenario, "nodes": nodes})
            p = common.paired_lenient(part, keys, "label", label, ref, metric, log=False)
            eff = common.paired_effect(p.a, p.b)
            rec.update(n_pairs=p.n, n_dropped_invalid=p.dropped_invalid, n_undefined=p.dropped_undefined,
                       n_dropped_missing=p.dropped_missing)
            for name in ("mean", "median", "pct"):
                for suffix in ("", "_lo", "_hi"):
                    rec[f"diff_{name}{suffix}"] = eff[f"{name}_diff{suffix}"]
            t = _row(tests, a=label, b=ref, metric=metric, scenario=scenario, nodes=nodes) if len(tests) else None
            if t is not None:
                rec.update(family=t["family"], p_wilcoxon=t["p_wilcoxon"], p_floor=t["p_floor"], m=t["m"],
                           p_holm=t["p_holm"], significant=bool(t["significant"]),
                           holm_reachable=bool(t["holm_reachable"]), direction=t["direction"], outcome=t["outcome"])
        rows.append(rec)
    return pd.DataFrame(rows, columns=ABL_COLUMNS)


def analyse_ablation(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write fig8_ablation (table and figure); return the ablation family's tests and the descriptive effects."""
    table = tidy(ablation_table(sel))
    common.write_table(table, out_root, "fig8_ablation")
    ablation_figure(table, out_root)
    common.logger.info("wrote fig8_ablation (table and figure)")
    effects = table.loc[table["family"] != "reference"]
    cols = ["scenario", "nodes", "configuration", "metric", "family", "n_pairs", "n_dropped_invalid", "n_undefined",
            "diff_mean", "diff_pct", "diff_pct_lo", "diff_pct_hi", "p_wilcoxon", "p_floor", "p_holm", "significant",
            "holm_reachable", "outcome"]
    missing = common.missing_configurations(everything, common.comparisons("abl"))
    return {"tables": ["fig8_ablation"], "figures": ["fig8_ablation"],
            "reference": common.experiment("abl")["params"]["reference_label"],
            "complete": not missing, "missing_configurations": missing,
            "tests": effects.loc[effects["family"] == "ablation"].reindex(columns=cols),
            "descriptive": effects.loc[effects["family"] == "descriptive"].reindex(columns=cols[:12])}


# --------------------------------------------------------------------------------------------------------------------
# B4 parity in the wild
# --------------------------------------------------------------------------------------------------------------------

B4_COLUMNS = ["pair", "scenario", "nodes", "a", "b", "n_seeds", "n_paired", "n_match",
              *(f"n_match_{m}" for m in ("steady_cost_per_hour", "evictions", "nodes_removed", "final_nodes")),
              "unmatched_seeds", "exact", *PAIR_COUNT_COLUMNS, "mean_a", "mean_b", "pct_diff", "pct_diff_lo",
              "pct_diff_hi", "margin", "margin_pct", "ci90_lo", "ci90_hi", "p_tost", "p_floor", "m", "p_tost_holm",
              "holm_reachable", "equivalent_holm", "outcome"]


def b4_parity_table(sel: pd.DataFrame, tests: pd.DataFrame, e: Mapping[str, Any]) -> pd.DataFrame:
    """Per (pair, scenario, nodes): the seeds of either arm (n_seeds), those with both runs valid (n_paired), those
    whose summaries match exactly on every ``params.match_metrics`` (n_match; per metric too), the seeds that do
    not (unmatched_seeds), ``exact`` (every seed matches), and the pair's TOST (family B4)."""
    params = e["params"]
    metrics = list(params["match_metrics"])
    rows = []
    for (a, b), nodes, scenario in itertools.product(params["pairs"].items(), e["nodes"], e["scenarios"]):
        part = _stratum_rows(sel, {"scenario": scenario, "nodes": nodes})
        ra, rb = (part.loc[part["label"] == lab] for lab in (a, b))
        seeds = sorted({*ra["seed"].tolist(), *rb["seed"].tolist()}, key=float)
        va = {s: r for s, r in zip(ra["seed"], ra.to_dict("records")) if r["valid"]}
        vb = {s: r for s, r in zip(rb["seed"], rb.to_dict("records")) if r["valid"]}
        paired_seeds = [s for s in seeds if s in va and s in vb]
        match = {m: [s for s in paired_seeds if _exact(va[s][m], vb[s][m])] for m in metrics}
        all_match = [s for s in paired_seeds if all(s in match[m] for m in metrics)]
        rec: dict[str, Any] = {c: math.nan for c in B4_COLUMNS}
        rec.update(pair=f"{a} vs {b}", scenario=scenario, nodes=nodes, a=a, b=b, n_seeds=len(seeds),
                   n_paired=len(paired_seeds), n_match=len(all_match),
                   unmatched_seeds=", ".join(str(int(s)) for s in seeds if s not in all_match),
                   exact=bool(seeds) and len(all_match) == len(seeds))
        for m in metrics:
            rec[f"n_match_{m}"] = len(match[m])
        t = _row(tests, a=a, b=b, scenario=scenario, nodes=nodes) if len(tests) else None
        if t is not None:
            for col in (*PAIR_COUNT_COLUMNS, "mean_a", "mean_b", "pct_diff", "pct_diff_lo", "pct_diff_hi", "margin",
                        "margin_pct", "ci90_lo", "ci90_hi", "p_tost", "p_floor", "m", "holm_reachable",
                        "equivalent_holm", "outcome"):
                rec[col] = t[col]
            rec["p_tost_holm"] = t["p_holm"]
        rows.append(rec)
    return pd.DataFrame(rows, columns=B4_COLUMNS)


def _exact(x: Any, y: Any) -> bool:
    """Exact equality of two summary values (both missing counts as equal)."""
    a, b = common._num(x), common._num(y)
    return (math.isnan(a) and math.isnan(b)) or a == b


def analyse_b4(sel: pd.DataFrame, everything: pd.DataFrame, out_root: Path) -> dict[str, Any]:
    """Write b4_parity and return B4's status: "exact" if every seed of every (pair, scenario) cell matches exactly
    (and no configuration is missing), "equivalent" if all six TOSTs (family B4, Holm over 6) are equivalent after
    Holm, otherwise "provisional"."""
    e = common.experiment("b4")
    comps = common.comparisons("b4")
    tests = comparison_table(sel, comps)
    table = tidy(b4_parity_table(sel, tests, e))
    common.write_table(table, out_root, "b4_parity")
    common.logger.info("wrote b4_parity")
    missing = common.missing_configurations(everything, comps)
    exact = bool(len(table)) and bool(table["exact"].astype(bool).all()) and not missing
    equivalent = bool(len(tests)) and bool((tests["equivalent_holm"] == True).all())  # noqa: E712
    status = "exact" if exact else ("equivalent" if equivalent else "provisional")
    return {"tables": ["b4_parity"],
            "b4": {"rule": "exact if every seed's summary matches exactly (steady_cost_per_hour, evictions, "
                           "nodes_removed, final_nodes) in every pair and scenario; equivalent if all six TOSTs at "
                           "the 3% margin pass after Holm (family B4); otherwise provisional",
                   "status": status, "complete": not missing, "missing_configurations": missing,
                   "n_seeds": int(pd.to_numeric(table["n_seeds"]).sum()) if len(table) else 0,
                   "n_match": int(pd.to_numeric(table["n_match"]).sum()) if len(table) else 0,
                   "n_equivalent_holm": int((tests["equivalent_holm"] == True).sum())  # noqa: E712
                   if len(tests) else 0,
                   "cells": table}}


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
            "(steady-state cost only: the preregistered ablation family of 8 tests; the other metrics are "
            "descriptive).")


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
    # Footnote: wrapped to the figure width (about 0.58 em per character at 6.5 pt), height from its line count.
    note_pt = 6.5
    note = textwrap.wrap(_ablation_note(table), int(common.FIGSIZE["double"][0] * 72 / (0.58 * note_pt)) - 4)
    foot_h = 0.14 + len(note) * note_pt * 1.25 / 72
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
        fig.text(0.005, 0.06 / height, "\n".join(note), ha="left", va="bottom", fontsize=note_pt,
                 color=common.NEUTRAL)
        common.save_figure(fig, out_root, name)




if __name__ == "__main__":
    common.cli(main)
