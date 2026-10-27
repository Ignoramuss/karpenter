"""Exp 2 (H2), the potential as a lead indicator of consolidation, and the timeseries figures 1, 2 and 6.

Everything here reads the runs' ``timeseries.csv`` (one row per simulated minute).

**Exp 2 (H2).** Does a drop in the potential over a window predict node removals in the next window? Unit: the
workload seed (experiments/PREREGISTRATION.md, H2). The runs are every valid W2, W3 and W6 run of the registry's
``exp2`` groups (experiments.yaml; remapped by ANALYSIS_GROUP_MAP; configurations with fewer than MIN_SEEDS finished
runs cut), without B0, which never consolidates. Runs whose configuration is identical apart from the label and the
group (``common.config_fingerprint``, seed included: e.g. Exp 6's lambda=0.1 point is Exp 1's random/entropy-greedy,
Exp 5's least-allocated/spread cells are Exp 1's) are the same simulation, so each distinct (configuration, seed)
counts once: the first in registry group order. Per run, after warmup (t >= 10 min) and with window w, at t = 10 + w,
10 + 2w, ... while t + w is in the run (non-overlapping windows):

- x(t) = Phi(t) - Phi(t - w), Phi the unnormalized potential the simulator logs (``exp2.params.potentials``: P1 =
  p1_load_entropy, P2 = p2_util_entropy, P3 = p3_quadratic), which removing an empty node leaves unchanged;
- y(t) = nodes_removed_cum(t + w) - nodes_removed_cum(t) (or, for H2-cost, cost_per_hour(t) - cost_per_hour(t + w));

and the run's rho = Spearman(-x, y), undefined when either series is constant or there are fewer than
``MIN_WINDOWS`` windows. x uses only minutes up to t and y only minutes after t, so there is no look-ahead. Per
(potential, scenario, seed): the median rho over that seed's runs with a defined rho; a seed with none counts as 0
(non-positive). The test is an exact one-sided sign (binomial) test over the seeds that P(per-seed median > 0) > 1/2,
with floor 0.5^n for n seeds. Family H2 (primary: raw potentials, removals, w = 10): Holm over potential x scenario
with data (<= 9). A potential holds in a scenario when its test rejects after Holm and the median of the per-seed
medians is positive; it **holds** if it does in every scenario with data, **partial** if in some. Secondary families,
each with the same procedure and outside the verdict: H2-cost (next-window cost decrease), H2-norm (the normalized
columns p*_norm, whose denominators shrink whenever an empty node is removed), H2-w5 and H2-w20 (5- and 20-minute
windows). No other combination is tested.

**Lead-lag (figure 6, descriptive).** For the raw potentials, on per-minute differences after warmup, dPhi(t) =
Phi(t) - Phi(t - 1) and dRemovals(t) = nodes_removed_cum(t) - nodes_removed_cum(t - 1): per run and lag k in
-30..30 min, the Pearson correlation of -dPhi(t) with dRemovals(t + k) over the overlapping minutes; then the median
across runs with a bootstrap CI. Positive mass at k > 0 means the potential's drop comes first.

**Figure 1.** CPU-utilization histograms over time, upstream B2 vs the pipeline (random/entropy-greedy, and
random/anneal-plan when present), W1 and W2 at 200 nodes: one heatmap per arm and scenario (share of ready nodes in
each 10% bin, averaged over seeds), plus the share of nodes in the 20%-80% band (band_share_cpu) per arm.

**Figure 2.** P2 potential (p2_norm: P2 per $/hr of ready fleet, so arms with different fleets compare), node count
and cost ($/hr) on shared time axes for B2, B3,
heuristic/entropy-greedy, random/entropy-greedy and random/anneal-plan at 200 nodes, mean across seeds with a 95%
bootstrap band: W2 (``fig2_potential_nodes_cost``) and W1 (``fig2_potential_nodes_cost_w1``).

Outputs under ``out_root`` (nothing is written for a part without runs; it logs "skipped <name>: no runs"):

- ``tables/exp2_correlation.{csv,md}``: one row per (family, potential, scenario): runs, undefined correlations,
  seeds, the per-seed medians' median with a bootstrap CI over seeds, the sign test, its floor, Holm and the verdict.
- ``tables/exp2_seeds.{csv,md}``: the per-seed medians behind the sign tests, one row per (family, potential,
  scenario, seed).
- ``tables/exp2_leadlag.{csv,md}``: the data behind figure 6, one row per (scenario, potential, lag).
- ``tables/timeseries_figures.json``: selection and validity counts, H2 verdicts, lead-lag summaries, and the
  late-run (t >= 0.8 x horizon) numbers behind figures 1 and 2.
- ``figures/out/fig1_utilization_histograms``, ``fig2_potential_nodes_cost``, ``fig2_potential_nodes_cost_w1`` and
  ``fig6_leadlag``, each ``.png`` and ``.pdf``.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.timeseries_figures [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import PowerNorm
from scipy import stats

from paper.analysis import common

ALPHA = common.ALPHA

# --- Exp 2 -----------------------------------------------------------------------------------------------------------
EXP2 = "exp2"
_EXP2 = common.experiment(EXP2)
_P2 = _EXP2["params"]
EXP2_GROUPS = tuple(_EXP2["groups"])  # session names (registry order), remapped by ANALYSIS_GROUP_MAP
EXP2_SCENARIOS = tuple(_EXP2["scenarios"])  # W2, W3, W6 (the registry's select)
EXP2_EXCLUDED_LABELS = tuple(_P2.get("excluded_labels", ["B0"]))  # B0 never consolidates
# The potentials as defined (registry exp2.params.potentials, the raw sums), and the simulator's normalized forms,
# whose denominators shrink whenever an empty node is removed (family H2-norm).
POTENTIALS = {str(k): str(v) for k, v in _P2["potentials"].items()}
POTENTIALS_NORM = {str(k): str(v) for k, v in _P2["potentials_norm"].items()}
FORMS = {"raw": POTENTIALS, "norm": POTENTIALS_NORM}
OUTCOMES = ("removals", "cost_decrease")
# The preregistered analyses, primary first: (family, form, outcome, window). Holm runs within each family.
ANALYSES = tuple((str(a["family"]), str(a["form"]), str(a["outcome"]), int(a["window_min"])) for a in _P2["analyses"])
PRIMARY_FAMILY = ANALYSES[0][0]
WARMUP_MIN = float(_P2.get("warmup_min", 10))
MIN_WINDOWS = int(_P2.get("min_windows", 4))  # fewer non-overlapping windows than this and a run's rho is undefined
LAGS = tuple(range(-30, 31))
MIN_LAG_PAIRS = 10  # overlapping minutes a lag needs for its correlation

# --- Figures 1 and 2 -------------------------------------------------------------------------------------------------
FIG_GROUPS = ("exp1-n200", "exp1-baselines-n200")
FIG_NODES = 200
FIG1 = "fig1_utilization_histograms"
FIG1_SCENARIOS = ("W1", "W2")
FIG1_ARMS = ("B2", "random/entropy-greedy", "random/anneal-plan")
FIG2_ARMS = ("B2", "B3", "heuristic/entropy-greedy", "random/entropy-greedy", "random/anneal-plan")
FIG2 = {"W2": "fig2_potential_nodes_cost", "W1": "fig2_potential_nodes_cost_w1"}
FIG2_PANELS = (("p2_norm", "P2 / fleet price\n(mean h(u))"), ("nodes", "Nodes"), ("cost_per_hour", "Cost ($/hr)"))
FIG6 = "fig6_leadlag"
HIST_BINS = 10
# Histogram bins reported in the JSON: nearly empty ([0, 0.2)) and packed ([0.8, 1.0]) nodes, the two modes the
# potential pushes the fleet towards (band_share_cpu covers the middle).
LOW_BINS = (0, 1)
PACKED_BINS = (8, 9)
LATE_FROM_FRACTION = 0.8  # "late" = minutes t >= 0.8 x the run's last minute, as for steady_cost_per_hour
# Columns that are undefined (the simulator writes 0) at minutes with no ready node; NaN there in the figures.
EMPTY_UNDEFINED = ("p1_norm", "p2_norm", "p3_norm", "band_share_cpu", "band_share_dom", "cpu_util_mean")
HEATMAP_CMAP = "Blues"  # single-hue sequential, white at zero so empty bins recede
HEATMAP_GAMMA = 0.5  # square-root color scale, so the small shares in the middle bins stay visible
HEATMAP_TICKS = (0.0, 0.05, 0.2, 0.5, 1.0)
MUTED = "#666666"
WARMUP_SHADE = "#F0F0F0"
POTENTIAL_STYLES = {"P1": {"color": common.PALETTE[0], "marker": "o"},
                    "P2": {"color": common.PALETTE[1], "marker": "s"},
                    "P3": {"color": common.PALETTE[2], "marker": "^"}}

# --------------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------------


def _floats(values: Any) -> np.ndarray:
    """``values`` as a float array; missing and non-numeric entries become NaN."""
    return pd.to_numeric(pd.Series(values, dtype=object), errors="coerce").to_numpy(dtype=float)


def _ordered(values: Iterable[Any], preferred: Sequence[Any]) -> list[Any]:
    """The distinct non-missing ``values``: those in ``preferred`` first, in its order, then the rest sorted."""
    present = {v for v in values if isinstance(v, str) or (v is not None and not pd.isna(v))}
    head = [v for v in preferred if v in present]
    return head + sorted(present - set(head), key=str)


def _nanmean(x: np.ndarray, axis: int = 0) -> np.ndarray:
    """Mean over ``axis`` ignoring NaN; NaN where a slice has no finite value (and no warning)."""
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    count = ok.sum(axis=axis)
    total = np.where(ok, x, 0.0).sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def _nodes_text(values: Iterable[Any]) -> str:
    """Distinct node counts as "200, 1000"."""
    nodes = sorted({int(v) for v in _floats(list(values)) if np.isfinite(v)})
    return ", ".join(str(n) for n in nodes)


def _column(pm: pd.DataFrame, col: str) -> pd.Series:
    """``pm[col]`` as floats, or all NaN when the run's timeseries lacks the column."""
    if col not in pm.columns:
        return pd.Series(np.nan, index=pm.index, dtype=float)
    return pd.to_numeric(pm[col], errors="coerce").astype(float)


def _is_constant(x: np.ndarray) -> bool:
    return bool(x.size == 0 or np.max(x) == np.min(x))


def seed_sign_test(per_seed: Any) -> dict[str, Any]:
    """The H2 sign test over seeds: exact one-sided binomial test that P(per-seed median > 0) > 1/2. Every seed
    counts; one whose median is 0 or below (including a seed without a defined rho, which counts as 0) is a
    non-positive outcome. Returns n_seeds, n_positive, n_nonpositive, p_sign (1.0 without seeds) and p_floor
    (0.5^n_seeds: every seed positive)."""
    x = _floats(per_seed)
    x = np.where(np.isfinite(x), x, 0.0)
    n, pos = int(x.size), int((x > 0).sum())
    p = float(stats.binomtest(pos, n, 0.5, alternative="greater").pvalue) if n else 1.0
    return {"n_seeds": n, "n_positive": pos, "n_nonpositive": n - pos, "p_sign": p, "p_floor": common.sign_floor(n)}


# --------------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------------


def per_minute(ts: pd.DataFrame) -> pd.DataFrame:
    """A timeseries indexed by whole simulated minute (``round(t_min)``), every minute from first to last present
    (missing minutes are NaN rows; a duplicated minute keeps its last sample)."""
    t = _floats(ts["t_min"])
    ts = ts.loc[np.isfinite(t)]
    minutes = np.rint(t[np.isfinite(t)]).astype(int)
    out = ts.assign(minute=minutes).drop_duplicates("minute", keep="last").set_index("minute").sort_index()
    if out.empty:
        return out
    return out.reindex(pd.RangeIndex(int(out.index.min()), int(out.index.max()) + 1, name="minute"))


def load_series(rows: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Per-minute timeseries (``per_minute``) of each row's run, by run_id, and the run_ids without a usable one
    (no timeseries.csv, an unreadable one, or one without samples; counted as missing, not dropped silently).

    Only call with valid rows: abandoned cells have no run files.
    """
    series: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for run_id, path in sorted(zip(rows["run_id"], rows["path"]), key=lambda t: str(t[0])):
        if run_id in series or run_id in missing:
            continue
        if not (Path(path) / "timeseries.csv").exists():
            common.logger.info("no timeseries.csv for %s", run_id)
            missing.append(run_id)
            continue
        try:
            pm = per_minute(common.load_timeseries(Path(path)))
        except (ValueError, KeyError, pd.errors.ParserError) as err:
            common.logger.warning("unreadable timeseries.csv for %s (%s)", run_id, err)
            missing.append(run_id)
            continue
        if pm.empty:  # a header without samples: nothing to plot or correlate
            common.logger.info("empty timeseries.csv for %s", run_id)
            missing.append(run_id)
            continue
        series[run_id] = pm
    return series, missing


def _validity(rows: pd.DataFrame, by: Sequence[str] = ("group",)) -> list[dict[str, Any]]:
    """Per-``by`` run counts and invalid reasons (abandoned cells included), for the JSON."""
    return common.to_jsonable(common.validity_report(rows, by=by))


# --------------------------------------------------------------------------------------------------------------------
# Exp 2: run selection and per-run correlations
# --------------------------------------------------------------------------------------------------------------------


def exp2_select(runs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Exp 2's runs: (candidates, used, counts).

    ``candidates`` are every row (abandoned cells and cut configurations included) the registry selects for ``exp2``
    (``common.select_runs``: the remapped ``EXP2_GROUPS`` on ``EXP2_SCENARIOS``). ``used`` are the valid ones outside
    cut configurations, without ``EXP2_EXCLUDED_LABELS`` and with each distinct (configuration, seed) once
    (``config_id``; the first in group order, then run_id, is kept), sorted by run_id. ``counts`` reports every
    exclusion.
    """
    groups = common.experiment_groups(EXP2)
    if runs.empty:
        return runs.copy(), runs.copy(), {}
    cand = common.select_runs(runs, EXP2, include_cut=True)
    cut = common.cut_mask(runs).reindex(cand.index).fillna(False).astype(bool)
    excluded = cand["label"].isin(EXP2_EXCLUDED_LABELS)
    rest = cand.loc[~excluded]
    kept = rest.loc[~cut.loc[rest.index]]
    valid = kept.loc[kept["valid"].astype(bool)].copy()
    rank = {g: i for i, g in enumerate(groups)}
    valid["_rank"] = valid["group"].map(rank)
    valid = valid.sort_values(["_rank", "run_id"], kind="stable")
    valid["_fingerprint"] = [c if isinstance(c, str) else f"run:{r}" for c, r in zip(valid["config_id"],
                                                                                     valid["run_id"])]
    dup = valid.duplicated("_fingerprint", keep="first")
    first = valid.loc[~dup].set_index("_fingerprint")["run_id"]
    duplicates = [{"run_id": r, "same_config_as": first[f]}
                  for r, f in zip(valid.loc[dup, "run_id"], valid.loc[dup, "_fingerprint"])]
    used = valid.loc[~dup].drop(columns=["_rank", "_fingerprint"]).sort_values("run_id", kind="stable")
    counts = {
        "groups": list(EXP2_GROUPS), "groups_after_map": groups, "scenarios_considered": list(EXP2_SCENARIOS),
        "excluded_labels": list(EXP2_EXCLUDED_LABELS), "n_candidates": int(len(cand)),
        "n_excluded_label": int(excluded.sum()), "n_runs": int(len(rest)),
        "n_in_cut_configurations": int(len(rest) - len(kept)), "n_valid": int(len(valid)),
        "n_invalid": int(len(kept) - len(valid)), "n_duplicate_config": int(dup.sum()),
        "duplicates": sorted(duplicates, key=lambda d: d["run_id"]),
        "validity": _validity(rest, ("group", "scenario")),
    }
    return cand, used, counts


def window_samples(pm: pd.DataFrame, w: int, warmup: float = WARMUP_MIN,
                   potentials: Mapping[str, str] | None = None) -> pd.DataFrame:
    """Non-overlapping window samples of one run (``per_minute`` frame) at t = warmup + w, warmup + 2w, ... while
    t + w is in the run. Columns: t, ``drop_<P>`` = -(Phi(t) - Phi(t - w)) per potential (``potentials``: name ->
    timeseries column, default ``POTENTIALS``), ``removals`` = removals in (t, t + w], ``cost_decrease`` =
    cost_per_hour(t) - cost_per_hour(t + w). The predictor only reads minutes <= t and the outcomes only minutes >= t,
    and consecutive samples' predictor windows do not overlap. A sample with a missing minute is NaN."""
    potentials = POTENTIALS if potentials is None else potentials
    columns = ["t", *(f"drop_{p}" for p in potentials), *OUTCOMES]
    if pm.empty:
        return pd.DataFrame(columns=columns)
    start, last = int(math.ceil(warmup)) + w, int(pm.index.max())
    t = np.arange(start, last - w + 1, w, dtype=int)

    def at(col: str, minutes: np.ndarray) -> np.ndarray:
        return _column(pm, col).reindex(minutes).to_numpy(dtype=float)

    out: dict[str, Any] = {"t": t}
    for p, col in potentials.items():
        out[f"drop_{p}"] = -(at(col, t) - at(col, t - w))
    out["removals"] = at("nodes_removed_cum", t + w) - at("nodes_removed_cum", t)
    out["cost_decrease"] = at("cost_per_hour", t) - at("cost_per_hour", t + w)
    return pd.DataFrame(out, columns=columns)


def spearman(a: Any, b: Any, min_n: int = MIN_WINDOWS) -> float:
    """Spearman correlation over the finite pairs; NaN with fewer than ``min_n`` pairs or a constant side."""
    a, b = _floats(a), _floats(b)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < min_n or _is_constant(a) or _is_constant(b):
        return math.nan
    return float(stats.spearmanr(a, b).statistic)


def _warmup(row: Any) -> float:
    w = getattr(row, "warmup_min", math.nan)
    return max(float(WARMUP_MIN), float(w)) if w is not None and np.isfinite(_floats([w])[0]) else float(WARMUP_MIN)


PER_RUN_COLUMNS = ["run_id", "group", "label", "scenario", "nodes", "seed", "family", "form", "window_min", "outcome",
                   "potential", "n_windows", "rho"]


def exp2_per_run(used: pd.DataFrame, series: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-run correlations: one row per (run, analysis in ``ANALYSES``, potential) with n_windows and ``rho``
    (Spearman of the potential's drop with the outcome in the next window; NaN when undefined). Runs without a
    series are absent."""
    rows = []
    for r in used.itertuples(index=False):
        pm = series.get(r.run_id)
        if pm is None:
            continue
        samples: dict[tuple[str, int], pd.DataFrame] = {}
        for family, form, outcome, w in ANALYSES:
            potentials = FORMS[form]
            if (form, w) not in samples:
                samples[(form, w)] = window_samples(pm, w, _warmup(r), potentials)
            s = samples[(form, w)]
            for p in potentials:
                rows.append({"run_id": r.run_id, "group": r.group, "label": r.label, "scenario": r.scenario,
                             "nodes": r.nodes, "seed": r.seed, "family": family, "form": form, "window_min": w,
                             "outcome": outcome, "potential": p, "n_windows": int(len(s)),
                             "rho": spearman(s[f"drop_{p}"], s[outcome])})
    return pd.DataFrame(rows, columns=PER_RUN_COLUMNS)


# --------------------------------------------------------------------------------------------------------------------
# Exp 2: per-seed aggregation and the sign tests
# --------------------------------------------------------------------------------------------------------------------

SEED_COLUMNS = ["family", "potential", "scenario", "seed", "n_runs", "n_defined", "median_rho", "positive"]
CORRELATION_COLUMNS = [
    "role", "family", "form", "window_min", "outcome", "potential", "column", "scenario", "nodes", "n_runs",
    "n_undefined", "n_seeds", "n_seeds_without_defined", "median_rho", "median_rho_lo", "median_rho_hi",
    "run_median_rho", "n_positive", "n_nonpositive", "p_sign", "p_floor", "m", "p_holm", "significant",
    "holm_reachable", "supports_h2", "outcome_h2",
]


def seed_table(per_run: pd.DataFrame) -> pd.DataFrame:
    """Per (family, potential, scenario, seed): the seed's runs, those with a defined rho, the median rho over the
    defined ones (0 when none is defined: a non-positive outcome) and whether it is positive."""
    rows = []
    if per_run.empty:
        return pd.DataFrame(columns=SEED_COLUMNS)
    for (family, potential, scenario, seed), part in per_run.groupby(["family", "potential", "scenario", "seed"],
                                                                      sort=True):
        rho = part["rho"].to_numpy(dtype=float)
        defined = rho[np.isfinite(rho)]
        med = float(np.median(defined)) if defined.size else 0.0
        rows.append({"family": family, "potential": potential, "scenario": scenario, "seed": seed,
                     "n_runs": int(len(part)), "n_defined": int(defined.size), "median_rho": med,
                     "positive": bool(med > 0)})
    return pd.DataFrame(rows, columns=SEED_COLUMNS)


def correlation_table(per_run: pd.DataFrame, seeds: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per (family, potential, scenario) with runs: n_runs and n_undefined (runs whose rho is undefined),
    n_seeds and those without a defined rho, the median of the per-seed medians with a 95% bootstrap CI over seeds
    (and the median over runs, descriptive), the sign test over seeds (``seed_sign_test``) with its floor, Holm within
    the family over potential x scenario with data (m = the family's rows), ``holm_reachable``, ``supports_h2``
    (significant after Holm and the median of per-seed medians positive) and ``outcome_h2``: "supports",
    "inconclusive" (could not have been significant), or "does not support"."""
    seeds = seed_table(per_run) if seeds is None else seeds
    rows = []
    for family, form, outcome, w in ANALYSES:
        fam = per_run.loc[per_run["family"] == family]
        for scenario in _ordered(fam["scenario"], EXP2_SCENARIOS):
            for p, col in FORMS[form].items():
                part = fam.loc[(fam["scenario"] == scenario) & (fam["potential"] == p)]
                if part.empty:
                    continue
                s = seeds.loc[(seeds["family"] == family) & (seeds["scenario"] == scenario)
                              & (seeds["potential"] == p)]
                rho = part["rho"].to_numpy(dtype=float)
                med = common.bootstrap_ci(s["median_rho"].to_numpy(dtype=float), "median")
                test = seed_sign_test(s["median_rho"])
                rows.append({
                    "role": "primary" if family == PRIMARY_FAMILY else "secondary", "family": family, "form": form,
                    "window_min": w, "outcome": outcome, "potential": p, "column": col, "scenario": scenario,
                    "nodes": _nodes_text(part["nodes"]), "n_runs": int(len(part)),
                    "n_undefined": int((~np.isfinite(rho)).sum()), "n_seeds": test["n_seeds"],
                    "n_seeds_without_defined": int((s["n_defined"] == 0).sum()),
                    "median_rho": med[0], "median_rho_lo": med[1], "median_rho_hi": med[2],
                    "run_median_rho": float(np.median(rho[np.isfinite(rho)])) if np.isfinite(rho).any() else math.nan,
                    "n_positive": test["n_positive"], "n_nonpositive": test["n_nonpositive"],
                    "p_sign": test["p_sign"], "p_floor": test["p_floor"],
                })
    if not rows:
        return pd.DataFrame(columns=CORRELATION_COLUMNS)
    out = pd.DataFrame(rows)
    out["m"], out["p_holm"] = 0, np.nan
    reachable = np.zeros(len(out), dtype=bool)
    for family, idx in out.groupby("family", sort=False).indices.items():
        common.family_size(family)  # declared (m: null = the tests with data)
        out.loc[out.index[idx], "m"] = len(idx)
        out.loc[out.index[idx], "p_holm"] = common.holm(out["p_sign"].to_numpy(dtype=float)[idx])
        reachable[idx] = common.holm_reachable(out["p_floor"].to_numpy(dtype=float)[idx], len(idx))
    p = out["p_holm"].to_numpy(dtype=float)
    out["significant"] = np.isfinite(p) & (p < ALPHA)
    out["holm_reachable"] = reachable
    out["supports_h2"] = out["significant"] & (out["median_rho"] > 0)
    out["outcome_h2"] = np.where(out["supports_h2"], "supports",
                                 np.where(~out["holm_reachable"], "inconclusive", "does not support"))
    return out[CORRELATION_COLUMNS]


def verdicts(corr: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per family and potential: the scenarios with data, those where H2 is supported (sign test significant after
    Holm, median of per-seed medians positive), and a verdict: "holds" (every scenario with data), "partial" (some),
    "inconclusive" (none, but a test could not have been significant), or "not supported"."""
    out: dict[str, dict[str, Any]] = {}
    for family, form, _, _ in ANALYSES:
        fam = corr.loc[corr["family"] == family]
        if fam.empty:
            continue
        per_potential = {}
        for p in FORMS[form]:
            rows = fam.loc[(fam["potential"] == p) & (fam["n_runs"] > 0)]
            scenarios = list(rows["scenario"])
            supported = list(rows.loc[rows["supports_h2"].astype(bool), "scenario"])
            if scenarios and len(supported) == len(scenarios):
                verdict = "holds"
            elif supported:
                verdict = "partial"
            elif (~rows["holm_reachable"].astype(bool)).any():
                verdict = "inconclusive"
            else:
                verdict = "not supported"
            per_potential[p] = {"scenarios": scenarios, "supported_in": supported, "verdict": verdict}
        out[family] = per_potential
    return out


# --------------------------------------------------------------------------------------------------------------------
# Exp 2: lead-lag (figure 6)
# --------------------------------------------------------------------------------------------------------------------


def cross_correlation(u: Any, v: Any, lags: Sequence[int] = LAGS, min_pairs: int = MIN_LAG_PAIRS) -> np.ndarray:
    """corr(u[t], v[t + k]) for each lag k: Pearson over the overlapping finite pairs; NaN when fewer than
    ``min_pairs`` pairs or a constant side."""
    u, v = _floats(u), _floats(v)
    n = len(u)
    out = np.full(len(lags), np.nan)
    for i, k in enumerate(lags):
        a, b = (u[:max(n - k, 0)], v[k:]) if k >= 0 else (u[-k:], v[:max(n + k, 0)])
        ok = np.isfinite(a) & np.isfinite(b)
        a, b = a[ok], b[ok]
        if a.size < min_pairs or _is_constant(a) or _is_constant(b):
            continue
        out[i] = float(np.corrcoef(a, b)[0, 1])
    return out


def run_leadlag(pm: pd.DataFrame, warmup: float = WARMUP_MIN, lags: Sequence[int] = LAGS) -> dict[str, np.ndarray]:
    """Per potential (the primary, raw ``POTENTIALS``): the cross-correlation of -dPhi(t) with dRemovals(t + k)
    over lags, on per-minute differences of the run's post-warmup minutes (t - 1 >= warmup)."""
    seg = pm.loc[pm.index >= int(math.ceil(warmup))]
    rem = _column(seg, "nodes_removed_cum").diff().to_numpy()[1:]
    return {p: cross_correlation(-_column(seg, col).diff().to_numpy()[1:], rem, lags)
            for p, col in POTENTIALS.items()}


LEADLAG_COLUMNS = ["scenario", "potential", "lag_min", "n_runs", "median", "median_lo", "median_hi", "mean"]


def leadlag_table(used: pd.DataFrame, series: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Per (scenario, potential, lag): the number of runs with a defined correlation, their median with a bootstrap
    CI, and their mean."""
    rows = []
    for scenario in _ordered(used["scenario"], EXP2_SCENARIOS):
        part = used.loc[used["scenario"] == scenario]
        curves = [run_leadlag(series[r.run_id], _warmup(r)) for r in part.itertuples(index=False)
                  if r.run_id in series]
        if not curves:
            continue
        for p in POTENTIALS:
            mat = np.vstack([c[p] for c in curves])
            for j, k in enumerate(LAGS):
                col = mat[:, j]
                med = common.bootstrap_ci(col, "median")
                rows.append({"scenario": scenario, "potential": p, "lag_min": k,
                             "n_runs": int(np.isfinite(col).sum()), "median": med[0], "median_lo": med[1],
                             "median_hi": med[2], "mean": float(_nanmean(col[None, :], axis=1)[0])})
    return pd.DataFrame(rows, columns=LEADLAG_COLUMNS)


def leadlag_summary(ll: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per scenario and potential: runs, the lag with the highest median correlation, the median at lag 0, and
    ``lead_share``: the positive median mass at k > 0 over that at k != 0 (above 0.5: the potential leads)."""
    out: dict[str, dict[str, Any]] = {}
    for scenario in _ordered(ll["scenario"], EXP2_SCENARIOS):
        out[scenario] = {}
        for p in POTENTIALS:
            sub = ll.loc[(ll["scenario"] == scenario) & (ll["potential"] == p)].sort_values("lag_min")
            med = sub["median"].to_numpy(dtype=float)
            lag = sub["lag_min"].to_numpy(dtype=int)
            pos = np.where(np.isfinite(med), np.maximum(med, 0.0), 0.0)
            lead, lagging = float(pos[lag > 0].sum()), float(pos[lag < 0].sum())
            finite = np.isfinite(med)
            best = int(np.argmax(np.where(finite, med, -np.inf))) if finite.any() else None
            zero = sub.loc[sub["lag_min"] == 0, "median"]
            out[scenario][p] = {
                "n_runs_max": int(sub["n_runs"].max()) if len(sub) else 0,
                "peak_lag_min": int(lag[best]) if best is not None else None,
                "peak_median": float(med[best]) if best is not None else math.nan,
                "median_lag0": float(zero.iloc[0]) if len(zero) else math.nan,
                "positive_mass_lead": lead, "positive_mass_lag": lagging,
                "lead_share": lead / (lead + lagging) if lead + lagging > 0 else math.nan,
            }
    return out


def figure6(ll: pd.DataFrame, out_root: Path) -> bool:
    """Figure 6: median lead-lag correlation per potential with its bootstrap band, one panel per scenario."""
    scenarios = _ordered(ll["scenario"], EXP2_SCENARIOS)
    if not scenarios:
        common.skipped(FIG6)
        return False
    size = common.FIGSIZE["single"] if len(scenarios) == 1 else common.FIGSIZE["double"]
    fig, axes = plt.subplots(1, len(scenarios), figsize=size, sharey=True, squeeze=False)
    for ax, scenario in zip(axes[0], scenarios):
        n_max = 0
        for p in POTENTIALS:
            sub = ll.loc[(ll["scenario"] == scenario) & (ll["potential"] == p)].sort_values("lag_min")
            st = POTENTIAL_STYLES[p]
            lag = sub["lag_min"].to_numpy(dtype=float)
            ax.fill_between(lag, sub["median_lo"].to_numpy(dtype=float), sub["median_hi"].to_numpy(dtype=float),
                            color=st["color"], alpha=0.18, linewidth=0)
            ax.plot(lag, sub["median"].to_numpy(dtype=float), color=st["color"], marker=st["marker"],
                    markevery=list(range(0, len(lag), 10)), label=p)
            n_max = max(n_max, int(sub["n_runs"].max()) if len(sub) else 0)
        ax.axhline(0, color=common.NEUTRAL, linewidth=0.6)
        ax.axvline(0, color=common.NEUTRAL, linewidth=0.6)
        ax.set_xlim(min(LAGS), max(LAGS))
        ax.set_xticks([-30, -15, 0, 15, 30])
        ax.set_xlabel("Lag k (min)")
        ax.set_title(f"{scenario} (n = {n_max} runs)", loc="left")
        ax.text(0.98, 0.97, "potential leads →", transform=ax.transAxes, ha="right", va="top", fontsize=7,
                color=MUTED)
        ax.text(0.02, 0.97, "← removals lead", transform=ax.transAxes, ha="left", va="top", fontsize=7,
                color=MUTED)
    axes[0, 0].set_ylabel("Median correlation of\n−ΔΦ(t) and removals(t+k)")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3)
    common.save_figure(fig, out_root, FIG6)
    return True


def run_exp2(series: Mapping[str, pd.DataFrame], missing: Sequence[str], used: pd.DataFrame,
             counts: Mapping[str, Any], out_root: Path) -> dict[str, Any] | None:
    """Exp 2 tables (exp2_correlation, exp2_seeds, exp2_leadlag) and figure 6; the JSON headline, or None when there
    are no runs."""
    if not counts or counts["n_runs"] == 0:
        common.skipped(EXP2)
        common.skipped(FIG6)
        return None
    used = used.loc[used["run_id"].isin(list(series))]
    head: dict[str, Any] = {
        "hypothesis": "H2",
        "selection": {**counts, "n_missing_timeseries": int(len(missing)), "missing_timeseries": list(missing),
                      "n_used": int(len(used)),
                      "used_by_scenario_nodes": _counts(used, ("scenario", "nodes")),
                      "used_by_label": _counts(used, ("scenario", "label"))},
        "method": _method(),
    }
    if used.empty:
        common.skipped(EXP2, "no valid runs")
        common.skipped(FIG6, "no valid runs")
        return head
    per_run = exp2_per_run(used, series)
    seeds = seed_table(per_run)
    corr = correlation_table(per_run, seeds)
    common.write_table(corr, out_root, "exp2_correlation")
    common.write_table(seeds, out_root, "exp2_seeds")
    ll = leadlag_table(used, series)
    if len(ll):
        common.write_table(ll, out_root, "exp2_leadlag")
    figure6(ll, out_root)
    found = verdicts(corr)
    head.update({
        "primary_family": PRIMARY_FAMILY,
        "primary": corr.loc[corr["family"] == PRIMARY_FAMILY].drop(columns=["role"]),
        "h2": {"rule": "per potential: the sign test over seeds (per-seed median rho > 0; a seed without a defined "
                       "rho counts as 0) rejects after Holm over potential x scenario with data, and the median of "
                       "the per-seed medians is positive, in every scenario with data (holds) or in some (partial)",
               "family": PRIMARY_FAMILY, "verdicts": found.get(PRIMARY_FAMILY, {})},
        "secondary": {f: v for f, v in found.items() if f != PRIMARY_FAMILY},
        "leadlag": leadlag_summary(ll),
    })
    common.logger.info("exp2: %d runs used (%d invalid, %d duplicate configs, %d B0 excluded, %d cut)", len(used),
                       counts["n_invalid"], counts["n_duplicate_config"], counts["n_excluded_label"],
                       counts["n_in_cut_configurations"])
    return head


def _counts(df: pd.DataFrame, by: Sequence[str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    g = df.groupby(list(by), sort=True, dropna=False).size().reset_index(name="n")
    return common.to_jsonable(g)


def _method() -> dict[str, Any]:
    """The statistical settings, recorded next to the numbers."""
    return {
        "potentials": POTENTIALS, "potentials_normalized": POTENTIALS_NORM,
        "why_raw": "the potentials as defined (registry exp2.params.potentials); the normalized forms divide by "
                   "ln(ready nodes) or the ready fleet price, which shrink whenever an empty node is removed, so they "
                   "rise mechanically at removals",
        "outcomes": {"removals": "nodes_removed_cum(t+w) - nodes_removed_cum(t)",
                     "cost_decrease": "cost_per_hour(t) - cost_per_hour(t+w)"},
        "analyses": [{"family": f, "form": form, "outcome": o, "window_min": w} for f, form, o, w in ANALYSES],
        "warmup_min": WARMUP_MIN, "min_windows_per_run": MIN_WINDOWS,
        "per_run_statistic": "Spearman of -(Phi(t) - Phi(t-w)) with y(t), undefined if a series is constant or there "
                             "are fewer windows than min_windows_per_run",
        "unit": "the workload seed: per (potential, scenario, seed) the median rho over the seed's runs with a "
                "defined rho, 0 when none",
        "sign_test": "exact one-sided binomial over seeds, H1: P(per-seed median > 0) > 1/2; non-positive medians "
                     "count against; floor 0.5^n_seeds",
        "holm_families": "within each family, over potential x scenario with data",
        "aggregate": "median of the per-seed medians with a 95% percentile bootstrap over seeds (10,000 resamples, "
                     "seed 0)",
        "deduplication": "each distinct (configuration without label, seed) counts once (manifest config without "
                         "label, runID and maxWall), the first in registry group order",
        "leadlag": {"lags_min": [min(LAGS), max(LAGS)], "statistic": "Pearson of -dPhi(t) with dRemovals(t+k) over "
                    "overlapping post-warmup minutes", "min_pairs": MIN_LAG_PAIRS,
                    "aggregate": "median over runs with a 95% percentile bootstrap CI (descriptive)"},
    }


# --------------------------------------------------------------------------------------------------------------------
# Figures 1 and 2
# --------------------------------------------------------------------------------------------------------------------


def figure_runs(runs: pd.DataFrame, scenarios: Sequence[str], labels: Sequence[str]) -> pd.DataFrame:
    """Rows (abandoned cells included) of the remapped ``FIG_GROUPS`` at ``FIG_NODES`` nodes on ``scenarios`` and
    ``labels``, outside cut configurations."""
    if runs.empty:
        return runs.copy()
    mask = (runs["group"].isin(common.map_groups(FIG_GROUPS)) & runs["scenario"].isin(scenarios)
            & runs["label"].isin(labels) & (_floats(runs["nodes"]) == FIG_NODES) & ~common.cut_mask(runs))
    return runs.loc[mask].copy()


def _run_ids(rows: pd.DataFrame, series: Mapping[str, pd.DataFrame], scenario: str, label: str) -> list[str]:
    """Valid runs of one (scenario, label) cell that have a timeseries, sorted."""
    sub = rows.loc[(rows["scenario"] == scenario) & (rows["label"] == label) & rows["valid"].astype(bool)]
    return sorted(r for r in sub["run_id"] if r in series)


def _masked(pm: pd.DataFrame, column: str) -> pd.Series:
    """``column`` for plotting: NaN before the run's first node exists (W2 and W3 start from an empty cluster), and
    for ratios (``EMPTY_UNDEFINED``) also at minutes without a ready node."""
    s = _column(pm, column)
    has_nodes = _column(pm, "nodes") > 0
    if has_nodes.any():
        s = s.where(pm.index >= has_nodes.idxmax())
    if column in EMPTY_UNDEFINED and "ready_nodes" in pm.columns:
        s = s.where(_column(pm, "ready_nodes") > 0)
    return s


def stack(series: Mapping[str, pd.DataFrame], run_ids: Sequence[str], column: str) -> tuple[np.ndarray, np.ndarray]:
    """(minutes, matrix): one row per run of ``column`` on the union of their minutes (NaN where a run has none)."""
    frames = [series[r] for r in run_ids]
    lo = min(int(f.index.min()) for f in frames)
    hi = max(int(f.index.max()) for f in frames)
    minutes = np.arange(lo, hi + 1)
    mat = np.vstack([_masked(f, column).reindex(minutes).to_numpy(dtype=float) for f in frames])
    return minutes, mat


def hist_shares(series: Mapping[str, pd.DataFrame], run_ids: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    """(minutes, shares): shares[run, minute, bin] = the share of the run's ready nodes in CPU-utilization bin
    ``bin`` (NaN at minutes without a ready node)."""
    minutes = None
    per_bin = []
    for b in range(HIST_BINS):
        minutes, mat = stack(series, run_ids, f"hist_cpu_{b}")
        per_bin.append(mat)
    counts = np.stack(per_bin, axis=-1)
    total = counts.sum(axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        shares = np.where(total > 0, counts / np.where(total > 0, total, 1), np.nan)
    return minutes, shares


def mean_band(mat: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per column (minute): the mean over rows (runs) and its 95% percentile bootstrap interval (common.bootstrap_ci,
    seed 0), NaNs dropped."""
    est, lo, hi = (np.full(mat.shape[1], np.nan) for _ in range(3))
    for j in range(mat.shape[1]):
        est[j], lo[j], hi[j] = common.bootstrap_ci(mat[:, j], "mean")
    return est, lo, hi


def _late_mask(minutes: np.ndarray) -> np.ndarray:
    return minutes + 1e-9 >= LATE_FROM_FRACTION * float(minutes.max())


def _per_run_late(mat: np.ndarray, minutes: np.ndarray) -> np.ndarray:
    """Each run's mean over the minutes t >= 0.8 x the last minute (the steady-state window)."""
    return _nanmean(mat[:, _late_mask(minutes)], axis=1)


def _at_minute(mat: np.ndarray, minutes: np.ndarray, minute: int) -> np.ndarray:
    idx = np.flatnonzero(minutes == minute)
    return mat[:, idx[0]] if idx.size else np.full(mat.shape[0], np.nan)


def _ci_dict(values: Any) -> dict[str, float]:
    est, lo, hi = common.bootstrap_ci(values, "mean")
    return {"mean": est, "lo": lo, "hi": hi}


def _shade_warmup(ax: plt.Axes) -> None:
    ax.axvspan(0, WARMUP_MIN, color=WARMUP_SHADE, linewidth=0, zorder=0)


def figure1(rows: pd.DataFrame, series: Mapping[str, pd.DataFrame], out_root: Path) -> dict[str, Any] | None:
    """Figure 1: per arm and scenario, the seed-averaged CPU-utilization histogram over time (heatmap), and per
    scenario the share of ready nodes in the 20%-80% band per arm. Returns the JSON numbers, or None when skipped."""
    scenarios = [s for s in FIG1_SCENARIOS if any(_run_ids(rows, series, s, a) for a in FIG1_ARMS)]
    arms = [a for a in FIG1_ARMS if any(_run_ids(rows, series, s, a) for s in scenarios)]
    if not arms:
        common.skipped(FIG1)
        return None
    heights = [1.0] * len(arms) + [0.9]
    fig, axes = plt.subplots(len(arms) + 1, len(scenarios), sharex="col", squeeze=False,
                             figsize=(common.FIGSIZE["double"][0], 0.95 * len(arms) + 1.7),
                             gridspec_kw={"height_ratios": heights})
    cmap = plt.get_cmap(HEATMAP_CMAP).copy()
    cmap.set_bad((1, 1, 1, 0))
    norm = PowerNorm(HEATMAP_GAMMA, vmin=0.0, vmax=1.0)
    image = None
    numbers: dict[str, Any] = {}
    for j, scenario in enumerate(scenarios):
        band_ax = axes[-1, j]
        _shade_warmup(band_ax)
        numbers[scenario] = {}
        for i, arm in enumerate(arms):
            ax = axes[i, j]
            ax.grid(False)
            ids = _run_ids(rows, series, scenario, arm)
            cell = rows.loc[(rows["scenario"] == scenario) & (rows["label"] == arm)]
            ax.set_title(f"{arm}, {scenario} (n = {len(ids)})", loc="left")
            ax.set_yticks([0, 0.5, 1], ["0%", "50%", "100%"])
            if j == 0:
                ax.set_ylabel("CPU util.")
            if not ids:
                ax.text(0.5, 0.5, "no runs", transform=ax.transAxes, ha="center", va="center", color=MUTED)
                continue
            minutes, shares = hist_shares(series, ids)
            mean = _nanmean(shares, axis=0)  # (minute, bin)
            image = ax.imshow(mean.T, origin="lower", aspect="auto", cmap=cmap, norm=norm, interpolation="nearest",
                              extent=(minutes[0] - 0.5, minutes[-1] + 0.5, 0.0, 1.0))
            bminutes, band = stack(series, ids, "band_share_cpu")
            est, lo, hi = mean_band(band)
            st = common.style_for(arm)
            band_ax.fill_between(bminutes, lo, hi, color=st["color"], alpha=0.15, linewidth=0)
            band_ax.plot(bminutes, est, color=st["color"], linestyle=st["linestyle"], label=arm)
            numbers[scenario][arm] = {
                "n_runs": int(len(cell)), "n_valid": int(cell["valid"].astype(bool).sum()), "n_used": len(ids),
                "band_share_cpu_at_warmup_end": _ci_dict(_at_minute(band, bminutes, WARMUP_MIN)),
                "band_share_cpu_late": _ci_dict(_per_run_late(band, bminutes)),
            }
            for key, bins in (("low", LOW_BINS), ("packed", PACKED_BINS)):
                part = shares[:, :, list(bins)].sum(axis=-1)  # NaN at minutes without a ready node
                numbers[scenario][arm][f"{key}_share_at_warmup_end"] = _ci_dict(_at_minute(part, minutes, WARMUP_MIN))
                numbers[scenario][arm][f"{key}_share_late"] = _ci_dict(_per_run_late(part, minutes))
        band_ax.set_ylim(bottom=0)
        band_ax.set_xlabel(common.METRIC_LABELS["t_min"])
        band_ax.set_title(f"Nodes at 20%–80% CPU, {scenario}", loc="left")
        if j == 0:
            band_ax.set_ylabel("Share of nodes")
    handles, labels = axes[-1, 0].get_legend_handles_labels()
    for ax in axes[-1, 1:]:  # an arm may have runs in only one scenario
        for h, lab in zip(*ax.get_legend_handles_labels()):
            if lab not in labels:
                handles.append(h)
                labels.append(lab)
    fig.legend(handles, labels, loc="outside lower center", ncols=len(labels))
    if image is not None:
        bar = fig.colorbar(image, ax=axes[:-1, :].ravel().tolist(), label="Share of\nready nodes",
                           ticks=list(HEATMAP_TICKS))
        bar.ax.set_yticklabels([f"{t:g}" for t in HEATMAP_TICKS])
    common.save_figure(fig, out_root, FIG1)
    return {"scenarios": scenarios, "arms": arms, "late_from_fraction": LATE_FROM_FRACTION, "warmup_min": WARMUP_MIN,
            "low_bins": "[0, 0.2)", "packed_bins": "[0.8, 1.0]", "band": "[0.2, 0.8] (band_share_cpu)",
            "cells": numbers}


def figure2(rows: pd.DataFrame, series: Mapping[str, pd.DataFrame], scenario: str, name: str,
            out_root: Path) -> dict[str, Any] | None:
    """Figure 2 for one scenario: P2 potential, nodes and cost over time, mean across seeds with a 95% bootstrap
    band, per arm. Returns the JSON numbers (late-run means per arm), or None when skipped."""
    arms = [a for a in FIG2_ARMS if _run_ids(rows, series, scenario, a)]
    if not arms:
        common.skipped(name)
        return None
    fig, axes = plt.subplots(len(FIG2_PANELS), 1, sharex=True, figsize=common.FIGSIZE["double_tall"])
    numbers: dict[str, Any] = {}
    n_used = []
    for arm in arms:
        ids = _run_ids(rows, series, scenario, arm)
        cell = rows.loc[(rows["scenario"] == scenario) & (rows["label"] == arm)]
        n_used.append(len(ids))
        st = common.style_for(arm)
        numbers[arm] = {"n_runs": int(len(cell)), "n_valid": int(cell["valid"].astype(bool).sum()),
                        "n_used": len(ids)}
        for ax, (column, _) in zip(axes, FIG2_PANELS):
            minutes, mat = stack(series, ids, column)
            est, lo, hi = mean_band(mat)
            ax.fill_between(minutes, lo, hi, color=st["color"], alpha=0.15, linewidth=0)
            ax.plot(minutes, est, color=st["color"], linestyle=st["linestyle"], marker=st["marker"],
                    markevery=list(range(int(30 - minutes[0]) % 30, len(minutes), 30)), label=arm)
            numbers[arm][f"{column}_late"] = _ci_dict(_per_run_late(mat, minutes))
    for ax, (_, ylabel) in zip(axes, FIG2_PANELS):
        _shade_warmup(ax)
        ax.set_ylabel(ylabel)
    axes[1].set_ylim(bottom=0)
    axes[2].set_ylim(bottom=0)
    axes[-1].set_xlabel(common.METRIC_LABELS["t_min"])
    seeds = f"{n_used[0]} seeds" if len(set(n_used)) == 1 else f"{min(n_used)}–{max(n_used)} seeds"
    axes[0].set_title(f"{scenario}, {FIG_NODES} nodes ({seeds} per arm)", loc="left")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside right upper", ncols=1)
    common.save_figure(fig, out_root, name)
    return {"late_from_fraction": LATE_FROM_FRACTION, "arms": numbers}


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(runs_root: Path, out_root: Path) -> None:
    """Exp 2 (H2) tables and figure 6, and figures 1 and 2, from the raw runs (see the module docstring)."""
    out_root = Path(out_root)
    runs = common.load_runs(Path(runs_root), include_abandoned=True)
    _, used, counts = exp2_select(runs)
    fig_rows = figure_runs(runs, sorted(set(FIG1_SCENARIOS) | set(FIG2)), sorted(set(FIG1_ARMS) | set(FIG2_ARMS)))
    frames = [f for f in (used, fig_rows.loc[fig_rows["valid"].astype(bool)] if len(fig_rows) else fig_rows)
              if len(f)]
    series, missing = load_series(pd.concat(frames)) if frames else ({}, [])
    headline: dict[str, Any] = {}
    exp2 = run_exp2(series, [m for m in missing if m in set(used["run_id"])], used, counts, out_root)
    if exp2 is not None:
        headline[EXP2] = exp2
    if fig_rows.empty:
        for name in (FIG1, *FIG2.values()):
            common.skipped(name)
    else:
        fig1 = figure1(fig_rows, series, out_root)
        if fig1 is not None:
            headline[FIG1] = fig1
        for scenario, name in FIG2.items():
            fig2 = figure2(fig_rows, series, scenario, name, out_root)
            if fig2 is not None:
                headline[name] = {**fig2, "scenario": scenario, "nodes": FIG_NODES}
        if any(k in headline for k in (FIG1, *FIG2.values())):
            headline["figure_runs_validity"] = _validity(fig_rows, ("group", "scenario", "label"))
    if headline:
        common.write_json(headline, out_root, "timeseries_figures")
        common.logger.info("wrote timeseries_figures.json (%s)", ", ".join(sorted(headline)))
    else:
        common.skipped("timeseries_figures")


if __name__ == "__main__":
    common.cli(main)
