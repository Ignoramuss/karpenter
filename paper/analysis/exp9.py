"""Exp 9 (``experiments/EXP9.md``): biased candidate selection, potential-guided shifts and early stopping, against
the session's two generation references under the entropy-greedy planner.

Descriptive, as the protocol fixes it: no hypothesis test and no Holm adjustment. Per workload and size, every
treatment arm is paired by seed with both references, and the report gives the mean per-seed difference (treatment
minus reference) and the difference as a percent of the reference mean, each with a 95% percentile bootstrap
interval over the seeds (``common.N_BOOT`` resamples, ``common.BOOT_SEED``). A difference is resolved when its
interval excludes zero.

Reads ``<runs>/exp9/*`` (the ``exp9`` matrix; a run counts if its summary says valid, and invalid runs are counted
and dropped pair-wise). Besides ``summary.json``, each run's decision log gives the search statistics: the passes
that searched (cycles with a ``moves`` summary record), attempts, full simulations and moves per searching pass, and
the share of searching passes that hit a search cap (the attempt budget or the simulation pool). Writes, under ``out_root``:

- ``tables/exp9_runs.md``: every run with its validity and the per-run metrics.
- ``tables/exp9_arms.md``: per size, workload, arm and metric, the mean with its 95% bootstrap interval, over valid
  runs, with the number of runs; a configuration with fewer than ``MIN_RUNS`` is flagged.
- ``tables/exp9_paired.md``: per size, workload, treatment, reference and metric, the paired differences.
- ``tables/exp9_search.md``: the search statistics per arm (the arms table restricted to them, with the share of
  passes that search).
- ``tables/exp9.json``: the headline: per stratum and treatment, which metrics are resolved against each reference
  and in which direction.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.exp9 [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from paper.analysis import common

MATRIX = "exp9"
REFERENCES = ("heuristic/entropy-greedy", "random/entropy-greedy")
TREATMENTS = ("weighted/entropy-greedy", "band/entropy-greedy", "weighted+greedy-shift/entropy-greedy",
              "random/idle-50", "weighted/idle-50", "weighted+greedy-shift/idle-50")
ARMS = (*REFERENCES, *TREATMENTS)
MIN_RUNS = 10  # EXP9.md: a configuration with fewer finished runs is reported but flagged
# summary.json metrics (EXP9.md, "Metrics"), then the search statistics derived from the decision log.
SUMMARY_METRICS = ("steady_cost_per_hour", "integrated_cost_post", "final_nodes", "evictions", "eviction_cost",
                   "cpu_pass_mean_ms", "cpu_pass_p95_ms", "cpu_pass_total_s", "full_simulations",
                   "disruption_passes")
SEARCH_METRICS = ("searching_share", "attempts_per_search", "full_per_search", "moves_per_search",
                  "exhausted_share", "cpu_per_search_ms")
METRICS = (*SUMMARY_METRICS, *SEARCH_METRICS)
STRATA = ["nodes", "scenario"]
RESOLVED_FLOOR_PCT = 1.0  # EXP9.md: effects under 1% are reported but not discussed


# --------------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------------


def search_stats(run_dir: Path, passes: float, cpu_total_s: float) -> dict[str, float]:
    """The search statistics of one run from its decision log: a searching pass is a cycle with a ``moves`` summary
    record (no move kind), whose ``detail`` holds the cycle's attempts, full simulations, moves and whether a search
    cap (the attempt budget or the simulation pool) was hit. ``cpu_per_search_ms`` is the run's pass CPU over its searching passes, an upper
    bound that carries the gated passes' floor (the paper, section 7)."""
    attempts: list[float] = []
    full: list[float] = []
    moves: list[float] = []
    exhausted: list[float] = []
    for rec in common.load_decisions(run_dir):
        if rec.get("stage") != "moves" or "kind" in rec:
            continue
        d = rec.get("detail") or {}
        attempts.append(float(d.get("attempts", 0)))
        full.append(float(d.get("full_simulations", 0)))
        moves.append(float(d.get("moves", 0)))
        exhausted.append(1.0 if d.get("exhausted") else 0.0)
    n = len(attempts)
    nan = math.nan
    return {
        "searching_passes": float(n),
        "searching_share": n / passes if passes and math.isfinite(passes) and passes > 0 else nan,
        "attempts_per_search": float(np.mean(attempts)) if n else nan,
        "full_per_search": float(np.mean(full)) if n else nan,
        "moves_per_search": float(np.mean(moves)) if n else nan,
        "exhausted_share": float(np.mean(exhausted)) if n else nan,
        "cpu_per_search_ms": 1000.0 * cpu_total_s / n if n and math.isfinite(cpu_total_s) else nan,
    }


def load(runs_root: Path) -> pd.DataFrame:
    """Every run of the ``exp9`` matrix with the summary metrics and the search statistics, one row per run."""
    runs = common.load_runs(Path(runs_root), include_abandoned=True)
    if runs.empty:
        return runs
    sel = runs.loc[runs["matrix"] == MATRIX].copy()
    if sel.empty:
        return sel
    stats = []
    for r in sel.to_dict("records"):
        path = Path(r["path"])
        if r.get("valid") and path.is_dir():
            stats.append(search_stats(path, common._num(r.get("disruption_passes")),
                                      common._num(r.get("cpu_pass_total_s"))))
        else:
            stats.append({k: math.nan for k in ("searching_passes", *SEARCH_METRICS)})
    for k in ("searching_passes", *SEARCH_METRICS):
        sel[k] = [s[k] for s in stats]
    sel["nodes"] = pd.to_numeric(sel["nodes"], errors="coerce")
    sel["seed"] = pd.to_numeric(sel["seed"], errors="coerce")
    return sel.sort_values(["nodes", "scenario", "label", "seed"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------------------------------


def runs_table(sel: pd.DataFrame) -> pd.DataFrame:
    cols = ["run_id", "group", "label", "scenario", "nodes", "seed", "valid", "invalid_reason", "searching_passes",
            *METRICS]
    return sel.reindex(columns=cols)


def arm_table(sel: pd.DataFrame, metrics: Sequence[str] = METRICS) -> pd.DataFrame:
    """Per stratum, arm and metric: the mean and median of the valid runs with 95% bootstrap intervals, the run
    counts, and ``flagged`` when fewer than ``MIN_RUNS`` finished."""
    rows = []
    for (nodes, scenario), stratum in sel.groupby(STRATA, sort=True):
        for label in ARMS:
            arm = stratum.loc[stratum["label"] == label]
            if arm.empty:
                continue
            valid = arm.loc[arm["valid"].astype(bool)]
            for m in metrics:
                vals = pd.to_numeric(valid[m], errors="coerce").to_numpy(dtype=float)
                vals = vals[np.isfinite(vals)]
                mean, lo, hi = common.bootstrap_ci(vals, "mean") if vals.size else (math.nan,) * 3
                med, mlo, mhi = common.bootstrap_ci(vals, "median") if vals.size else (math.nan,) * 3
                rows.append({"nodes": int(nodes), "scenario": scenario, "label": label, "metric": m,
                             "n_runs": int(len(arm)), "n_valid": int(vals.size), "flagged": len(arm) < MIN_RUNS,
                             "mean": mean, "mean_lo": lo, "mean_hi": hi, "median": med, "median_lo": mlo,
                             "median_hi": mhi})
    return pd.DataFrame(rows)


def paired_table(sel: pd.DataFrame, metrics: Sequence[str] = METRICS) -> pd.DataFrame:
    """Per stratum, treatment, reference and metric: the paired differences treatment minus reference over the
    seeds both arms have valid runs for (``common.paired_lenient``), with ``common.paired_effect``'s mean and
    percent differences and their intervals, and ``resolved`` (the mean difference's interval excludes zero) with
    its ``direction`` (lower, higher, or none)."""
    rows = []
    for (nodes, scenario), stratum in sel.groupby(STRATA, sort=True):
        labels = set(stratum["label"])
        for treatment in TREATMENTS:
            if treatment not in labels:
                continue
            for ref in REFERENCES:
                if ref not in labels:
                    continue
                for m in metrics:
                    p = common.paired_lenient(stratum, a=treatment, b=ref, metric=m,
                                              where=f"exp9 {scenario}-{nodes} {treatment} vs {ref} {m}")
                    e = common.paired_effect(p.a, p.b) if p.a.size else {}
                    lo, hi = e.get("mean_diff_lo", math.nan), e.get("mean_diff_hi", math.nan)
                    resolved = bool(math.isfinite(lo) and math.isfinite(hi) and (lo > 0 or hi < 0))
                    rows.append({"nodes": int(nodes), "scenario": scenario, "treatment": treatment,
                                 "reference": ref, "metric": m, "n_pairs": int(p.a.size),
                                 "n_dropped_invalid": int(p.dropped_invalid),
                                 "n_undefined": int(getattr(p, "dropped_undefined", 0) or 0),
                                 "n_dropped_missing": int(p.dropped_missing),
                                 "mean_treatment": e.get("mean_a", math.nan), "mean_reference": e.get("mean_b", math.nan),
                                 "mean_diff": e.get("mean_diff", math.nan), "mean_diff_lo": lo, "mean_diff_hi": hi,
                                 "pct_diff": e.get("pct_diff", math.nan), "pct_diff_lo": e.get("pct_diff_lo", math.nan),
                                 "pct_diff_hi": e.get("pct_diff_hi", math.nan), "resolved": resolved,
                                 "direction": ("lower" if hi < 0 else "higher") if resolved else "none"})
    return pd.DataFrame(rows)


def headline(arms: pd.DataFrame, paired: pd.DataFrame) -> dict[str, Any]:
    """Per stratum and treatment: the resolved metrics against each reference with their direction and percent
    effect (effects under ``RESOLVED_FLOOR_PCT`` listed separately as ``resolved_under_floor``), and the flagged
    configurations."""
    out: dict[str, Any] = {"references": list(REFERENCES), "treatments": list(TREATMENTS),
                           "metrics": list(METRICS), "min_runs": MIN_RUNS, "resolved_floor_pct": RESOLVED_FLOOR_PCT,
                           "strata": {}, "flagged": []}
    if not arms.empty:
        flagged = arms.loc[arms["flagged"], ["nodes", "scenario", "label", "n_runs"]].drop_duplicates()
        out["flagged"] = flagged.to_dict("records")
    if paired.empty:
        return out
    for (nodes, scenario), stratum in paired.groupby(STRATA, sort=True):
        key = f"{scenario}-{int(nodes)}"
        entry: dict[str, Any] = {}
        for (treatment, ref), block in stratum.groupby(["treatment", "reference"], sort=False):
            res = block.loc[block["resolved"]]
            effects = [{"metric": r["metric"], "direction": r["direction"], "pct_diff": r["pct_diff"],
                        "pct_diff_lo": r["pct_diff_lo"], "pct_diff_hi": r["pct_diff_hi"], "n_pairs": r["n_pairs"]}
                       for r in res.to_dict("records")]
            above = [e for e in effects if not (math.isfinite(e["pct_diff"]) and abs(e["pct_diff"]) < RESOLVED_FLOOR_PCT)]
            under = [e for e in effects if e not in above]
            entry.setdefault(treatment, {})[ref] = {"n_pairs": int(block["n_pairs"].max()), "resolved": above,
                                                    "resolved_under_floor": under,
                                                    "unresolved": [m for m in block.loc[~block["resolved"], "metric"]]}
        out["strata"][key] = entry
    return out


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    sel = load(Path(runs_root))
    if sel.empty:
        common.skipped("exp9")
        return
    common.logger.info("exp9: %d runs (%d invalid or abandoned)", len(sel), int((~sel["valid"].astype(bool)).sum()))
    common.write_table(runs_table(sel), Path(out_root), "exp9_runs")
    arms = arm_table(sel)
    common.write_table(arms, Path(out_root), "exp9_arms")
    search = arms.loc[arms["metric"].isin(SEARCH_METRICS)].reset_index(drop=True)
    common.write_table(search, Path(out_root), "exp9_search")
    paired = paired_table(sel)
    common.write_table(paired, Path(out_root), "exp9_paired")
    common.write_json(headline(arms, paired), Path(out_root), "exp9")
    common.logger.info("wrote exp9_runs, exp9_arms, exp9_search, exp9_paired, exp9.json")


if __name__ == "__main__":
    common.cli(main)
