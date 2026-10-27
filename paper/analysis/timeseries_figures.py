"""Exp 2 (H2), the potential as a lead indicator of consolidation, and the timeseries figures 1, 2 and 6.

Everything here reads the runs' ``timeseries.csv`` (one row per simulated minute).

**Exp 2 (H2).** Does a drop in the potential over a window predict node removals and a cost decrease in the next
window? The runs are every valid W2 and W3 run of the registry's ``exp2`` groups (experiments.yaml; remapped by
ANALYSIS_GROUP_MAP), as the preregistration says ("computed from the logs of the W2 and W3 runs above"), without B0,
which never consolidates. Runs whose configuration is identical apart from the label (e.g. Exp 6's lambda=0.5 point
is Exp 1's random/entropy-greedy, Exp 5's least-allocated/spread cells are Exp 1's) are the same simulation, so only
the first of them (in registry group order) is kept. Per run, after warmup (t >= 10 min) and with window w (10 min;
5 and 20 as sensitivity), at t = 10 + w, 10 + 2w, ... while t + w is in the run (non-overlapping windows):

- x(t) = Phi(t) - Phi(t - w) for the potentials as the plan's formal model defines them (the registry's
  ``exp2.params.potentials``): P1 = p1_load_entropy (H of the load shares), P2 = p2_util_entropy (sum of price x
  h(u)), P3 = p3_quadratic (sum of price x u(1 - u)). An empty node adds nothing to any of them, so removing one
  leaves them unchanged. The simulator's normalized forms (p1_norm = P1 / ln(ready nodes), p2_norm and p3_norm = P2
  and P3 / ready fleet price) are a sensitivity analysis (``form`` "norm", families ``H2-norm/...``): their
  denominators shrink at every removal of an empty node, so they rise mechanically when nodes are removed;
- y_removals(t) = nodes_removed_cum(t + w) - nodes_removed_cum(t);
- y_cost(t) = cost_per_hour(t) - cost_per_hour(t + w);

and the run's Spearman correlation of -x with each y (undefined, and the run skipped, when either series is constant
or has fewer than ``MIN_WINDOWS`` windows). x uses only minutes up to t and y only minutes after t, so there is no
look-ahead. Across runs, per (form, window, outcome, potential, scenario): the median and mean correlation with 95%
percentile bootstrap CIs over runs, the share of positive correlations, and a one-sided sign test that it exceeds one
half (zeros dropped). Holm runs within each (form, outcome, window) over potentials x scenarios (the preregistered
family is the raw potentials, removals, w = 10). H2 holds for a potential in a scenario when its sign test rejects
after Holm and its median is positive. Potentials are compared pairwise on the same runs with a two-sided Wilcoxon
signed-rank test of the per-run correlation differences, Holm within each (form, outcome, window) over pairs x
scenarios.

**Lead-lag (figure 6).** For the raw potentials, on per-minute differences after warmup, dPhi(t) = Phi(t) -
Phi(t - 1) and dRemovals(t) =
nodes_removed_cum(t) - nodes_removed_cum(t - 1): per run and lag k in -30..30 min, the Pearson correlation of
-dPhi(t) with dRemovals(t + k) over the overlapping minutes; then the median across runs with a bootstrap CI.
Positive mass at k > 0 means the potential's drop comes first.

**Figure 1.** CPU-utilization histograms over time, upstream B2 vs the pipeline (random/entropy-greedy, and
random/anneal-plan when present), W1 and W2 at 200 nodes: one heatmap per arm and scenario (share of ready nodes in
each 10% bin, averaged over seeds), plus the share of nodes in the 20%-80% band (band_share_cpu) per arm.

**Figure 2.** P2 potential (p2_norm: P2 per $/hr of ready fleet, so arms with different fleets compare), node count
and cost ($/hr) on shared time axes for B2, B3,
heuristic/entropy-greedy, random/entropy-greedy and random/anneal-plan at 200 nodes, mean across seeds with a 95%
bootstrap band: W2 (``fig2_potential_nodes_cost``) and W1 (``fig2_potential_nodes_cost_w1``).

Outputs under ``out_root`` (nothing is written for a part without runs; it logs "skipped <name>: no runs"):

- ``tables/exp2_correlation.{csv,md}``: one row per (form, window, outcome, potential, scenario).
- ``tables/exp2_potential_comparison.{csv,md}``: one row per (form, window, outcome, scenario, potential pair).
- ``tables/exp2_leadlag.{csv,md}``: the data behind figure 6, one row per (scenario, potential, lag).
- ``tables/timeseries_figures.json``: selection and validity counts, H2 verdicts, lead-lag summaries, and the
  late-run (last 20%) numbers behind figures 1 and 2.
- ``figures/out/fig1_utilization_histograms``, ``fig2_potential_nodes_cost``, ``fig2_potential_nodes_cost_w1`` and
  ``fig6_leadlag``, each ``.png`` and ``.pdf``.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.timeseries_figures [--runs DIR] [--out DIR]``.
"""

from __future__ import annotations

import copy
import itertools
import json
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
EXP2_GROUPS = tuple(_EXP2["groups"])  # session names (registry order), remapped by ANALYSIS_GROUP_MAP
EXP2_SCENARIOS = tuple(_EXP2["scenarios"])  # W2, W3 (the registry's select)
EXP2_EXCLUDED_LABELS = ("B0",)  # never consolidates
# Primary: the potentials as defined (registry exp2.params.potentials, the raw sums). Sensitivity: the simulator's
# normalized forms, whose denominators shrink whenever an empty node is removed (see the module docstring).
POTENTIALS = {str(k): str(v) for k, v in _EXP2["params"]["potentials"].items()}
POTENTIALS_NORM = {"P1": "p1_norm", "P2": "p2_norm", "P3": "p3_norm"}
FORMS = {"raw": POTENTIALS, "norm": POTENTIALS_NORM}
PRIMARY_FORM = "raw"
OUTCOMES = ("removals", "cost_decrease")
PRIMARY_WINDOW = 10
WINDOWS = (PRIMARY_WINDOW, 5, 20)  # primary first, then the sensitivity windows
PRIMARY_OUTCOME = "removals"
WARMUP_MIN = 10
MIN_WINDOWS = 4  # fewer non-overlapping windows than this and a run's correlation is undefined
LAGS = tuple(range(-30, 31))
MIN_LAG_PAIRS = 10  # overlapping minutes a lag needs for its correlation
# Keys of manifest.json's config that the matrix runner sets but that do not change the simulation.
RUNNER_ONLY_CONFIG = ("runID", "maxWall")

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
LATE_FRACTION = 0.2  # "late" = the last 20% of the run, as for steady_cost_per_hour
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


def signed_rank_direction(d: Any) -> int:
    """+1 when the positive differences carry more signed-rank weight than the negative ones, -1 when fewer, else 0."""
    d = _floats(d)
    d = d[np.isfinite(d) & (d != 0)]
    if d.size == 0:
        return 0
    ranks = stats.rankdata(np.abs(d))
    return int(np.sign(ranks[d > 0].sum() - ranks[d < 0].sum()))


def sign_test(values: Any) -> dict[str, Any]:
    """One-sided sign test that positive values are more common than negative ones (exact binomial, p = 1/2).

    Zeros are dropped, as in the classical sign test. Returns n_positive, n_negative, n_zero and p_sign (NaN when no
    value is nonzero).
    """
    x = _floats(values)
    x = x[np.isfinite(x)]
    pos, neg = int((x > 0).sum()), int((x < 0).sum())
    p = float(stats.binomtest(pos, pos + neg, 0.5, alternative="greater").pvalue) if pos + neg else math.nan
    return {"n_positive": pos, "n_negative": neg, "n_zero": int(x.size - pos - neg), "p_sign": p}


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


def config_fingerprint(run_dir: Path) -> str | None:
    """The run's simulation config (manifest.json ``config``) without the label and the runner-only keys, as
    canonical JSON: equal for runs that simulate the same thing under different labels or groups. None without a
    manifest."""
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        cfg = copy.deepcopy(json.load(f).get("config") or {})
    for key in RUNNER_ONLY_CONFIG:
        cfg.pop(key, None)
    if isinstance(cfg.get("arm"), dict):
        cfg["arm"].pop("label", None)
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def _validity(rows: pd.DataFrame, by: Sequence[str] = ("group",)) -> list[dict[str, Any]]:
    """Per-``by`` run counts and invalid reasons (abandoned cells included), for the JSON."""
    return common.to_jsonable(common.validity_report(rows, by=by))


# --------------------------------------------------------------------------------------------------------------------
# Exp 2: run selection and per-run correlations
# --------------------------------------------------------------------------------------------------------------------


def exp2_select(runs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Exp 2's runs: (candidates, used, counts).

    ``candidates`` are every row (abandoned cells included) the registry selects for ``exp2``
    (``common.select_runs``: the remapped ``EXP2_GROUPS`` on ``EXP2_SCENARIOS``). ``used`` are the valid ones,
    without ``EXP2_EXCLUDED_LABELS`` and with duplicate configurations dropped (the first in group order, then
    run_id, is kept), sorted by run_id. ``counts`` reports every exclusion.
    """
    groups = common.experiment_groups(EXP2)
    if runs.empty:
        return runs.copy(), runs.copy(), {}
    cand = common.select_runs(runs, EXP2)
    excluded = cand["label"].isin(EXP2_EXCLUDED_LABELS)
    rest = cand.loc[~excluded]
    valid = rest.loc[rest["valid"].astype(bool)].copy()
    rank = {g: i for i, g in enumerate(groups)}
    valid["_rank"] = valid["group"].map(rank)
    valid = valid.sort_values(["_rank", "run_id"], kind="stable")
    valid["_fingerprint"] = [config_fingerprint(p) or f"run:{r}" for p, r in zip(valid["path"], valid["run_id"])]
    dup = valid.duplicated("_fingerprint", keep="first")
    first = valid.loc[~dup].set_index("_fingerprint")["run_id"]
    duplicates = [{"run_id": r, "same_config_as": first[f]}
                  for r, f in zip(valid.loc[dup, "run_id"], valid.loc[dup, "_fingerprint"])]
    used = valid.loc[~dup].drop(columns=["_rank", "_fingerprint"]).sort_values("run_id", kind="stable")
    counts = {
        "groups": list(EXP2_GROUPS), "groups_after_map": groups, "scenarios_considered": list(EXP2_SCENARIOS),
        "excluded_labels": list(EXP2_EXCLUDED_LABELS), "n_candidates": int(len(cand)),
        "n_excluded_label": int(excluded.sum()), "n_runs": int(len(rest)), "n_valid": int(len(valid)),
        "n_invalid": int(len(rest) - len(valid)), "n_duplicate_config": int(dup.sum()),
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


def exp2_per_run(used: pd.DataFrame, series: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-run correlations: one row per (run, form, window, outcome, potential) with n_windows and ``rho``
    (Spearman of the potential's drop with the outcome in the next window; NaN when undefined), for each potential
    form in ``FORMS`` (raw: primary; norm: sensitivity). Runs without a series are absent."""
    rows = []
    for r in used.itertuples(index=False):
        pm = series.get(r.run_id)
        if pm is None:
            continue
        for form, potentials in FORMS.items():
            for w in WINDOWS:
                s = window_samples(pm, w, _warmup(r), potentials)
                for outcome in OUTCOMES:
                    for p in potentials:
                        rows.append({"run_id": r.run_id, "group": r.group, "label": r.label, "scenario": r.scenario,
                                     "nodes": r.nodes, "seed": r.seed, "form": form, "window_min": w,
                                     "outcome": outcome, "potential": p, "n_windows": int(len(s)),
                                     "rho": spearman(s[f"drop_{p}"], s[outcome])})
    return pd.DataFrame(rows, columns=["run_id", "group", "label", "scenario", "nodes", "seed", "form", "window_min",
                                       "outcome", "potential", "n_windows", "rho"])


# --------------------------------------------------------------------------------------------------------------------
# Exp 2: aggregation and tests
# --------------------------------------------------------------------------------------------------------------------

CORRELATION_COLUMNS = [
    "role", "family", "form", "window_min", "outcome", "potential", "column", "scenario", "nodes", "n_runs_total",
    "n_runs",
    "n_undefined", "median_rho", "median_rho_lo", "median_rho_hi", "mean_rho", "mean_rho_lo", "mean_rho_hi",
    "n_positive", "n_negative", "n_zero", "share_positive", "p_sign", "p_holm", "significant", "supports_h2",
]
COMPARISON_COLUMNS = [
    "role", "family", "form", "window_min", "outcome", "scenario", "a", "b", "n_runs_total", "n_pairs", "n_dropped",
    "median_a", "median_b", "mean_diff", "mean_diff_lo", "mean_diff_hi", "median_diff", "median_diff_lo",
    "median_diff_hi", "p_wilcoxon", "p_holm", "significant", "favours",
]


def _role(window: int, outcome: str, form: str = PRIMARY_FORM) -> str:
    if form != PRIMARY_FORM or window != PRIMARY_WINDOW:
        return "sensitivity"
    return "primary" if outcome == PRIMARY_OUTCOME else "secondary"


def _family(kind: str, form: str, outcome: str, window: int) -> str:
    """Holm family name: ``H2/<outcome>/w<window>`` (sign tests) or ``H2-compare/...`` (potential comparisons) for
    the raw potentials, with ``-norm`` after ``H2`` for the normalized sensitivity forms."""
    return f"H2{'' if form == PRIMARY_FORM else '-' + form}{kind}/{outcome}/w{window}"


def _cells(per_run: pd.DataFrame) -> list[tuple[str, int, str, str]]:
    """(form, window, outcome, scenario) combinations present, in display order (primary form, window and outcome
    first)."""
    scenarios = _ordered(per_run["scenario"], EXP2_SCENARIOS)
    return [(f, w, o, s) for f in FORMS for w in WINDOWS for o in OUTCOMES for s in scenarios
            if ((per_run["form"] == f) & (per_run["window_min"] == w) & (per_run["outcome"] == o)
                & (per_run["scenario"] == s)).any()]


def _add_holm(df: pd.DataFrame, p_col: str) -> pd.DataFrame:
    df = df.copy()
    df["p_holm"] = np.nan
    for _, idx in df.groupby("family", sort=True).groups.items():
        df.loc[idx, "p_holm"] = common.holm(df.loc[idx, p_col].to_numpy(dtype=float))
    p = df["p_holm"].to_numpy(dtype=float)
    df["significant"] = np.isfinite(p) & (p < ALPHA)
    return df


def correlation_table(per_run: pd.DataFrame) -> pd.DataFrame:
    """Across-run aggregates per (form, window, outcome, potential, scenario): median and mean correlation with
    bootstrap CIs over runs, the sign test, Holm within (form, outcome, window) over potentials x scenarios, and
    ``supports_h2`` (significant after Holm with a positive median)."""
    rows = []
    for form, w, outcome, scenario in _cells(per_run):
        for p, col in FORMS[form].items():
            part = per_run.loc[(per_run["form"] == form) & (per_run["window_min"] == w)
                               & (per_run["outcome"] == outcome) & (per_run["scenario"] == scenario)
                               & (per_run["potential"] == p)]
            rho = part["rho"].to_numpy(dtype=float)
            med = common.bootstrap_ci(rho, "median")
            mean = common.bootstrap_ci(rho, "mean")
            n = int(np.isfinite(rho).sum())
            sign = sign_test(rho)
            rows.append({
                "role": _role(w, outcome, form), "family": _family("", form, outcome, w), "form": form,
                "window_min": w, "outcome": outcome, "potential": p, "column": col, "scenario": scenario,
                "nodes": _nodes_text(part["nodes"]),
                "n_runs_total": int(len(part)), "n_runs": n, "n_undefined": int(len(part) - n),
                "median_rho": med[0], "median_rho_lo": med[1], "median_rho_hi": med[2],
                "mean_rho": mean[0], "mean_rho_lo": mean[1], "mean_rho_hi": mean[2], **sign,
                "share_positive": sign["n_positive"] / n if n else math.nan,
            })
    if not rows:
        return pd.DataFrame(columns=CORRELATION_COLUMNS)
    out = _add_holm(pd.DataFrame(rows), "p_sign")
    out["supports_h2"] = out["significant"] & (out["median_rho"] > 0)
    return out[CORRELATION_COLUMNS]


def potential_comparison_table(per_run: pd.DataFrame) -> pd.DataFrame:
    """Pairwise potential comparisons per (form, window, outcome, scenario), paired by run: per-run correlation
    differences a - b with mean and median and bootstrap CIs, two-sided Wilcoxon signed-rank p, Holm within
    (form, outcome, window) over pairs x scenarios, and ``favours`` (the potential the signed ranks favour)."""
    rows = []
    for form, w, outcome, scenario in _cells(per_run):
        part = per_run.loc[(per_run["form"] == form) & (per_run["window_min"] == w)
                           & (per_run["outcome"] == outcome) & (per_run["scenario"] == scenario)]
        wide = part.pivot(index="run_id", columns="potential", values="rho").sort_index()
        for a, b in itertools.combinations(FORMS[form], 2):
            ra = wide[a].to_numpy(dtype=float) if a in wide else np.full(len(wide), np.nan)
            rb = wide[b].to_numpy(dtype=float) if b in wide else np.full(len(wide), np.nan)
            ok = np.isfinite(ra) & np.isfinite(rb)
            d = ra[ok] - rb[ok]
            mean = common.bootstrap_ci(d, "mean")
            med = common.bootstrap_ci(d, "median")
            direction = signed_rank_direction(d)
            rows.append({
                "role": _role(w, outcome, form), "family": _family("-compare", form, outcome, w), "form": form,
                "window_min": w, "outcome": outcome, "scenario": scenario, "a": a, "b": b,
                "n_runs_total": int(len(wide)),
                "n_pairs": int(ok.sum()), "n_dropped": int(len(wide) - ok.sum()),
                "median_a": float(np.median(ra[ok])) if ok.any() else math.nan,
                "median_b": float(np.median(rb[ok])) if ok.any() else math.nan,
                "mean_diff": mean[0], "mean_diff_lo": mean[1], "mean_diff_hi": mean[2],
                "median_diff": med[0], "median_diff_lo": med[1], "median_diff_hi": med[2],
                "p_wilcoxon": common.wilcoxon_paired(ra[ok], rb[ok]),
                "favours": {1: a, -1: b}.get(direction, ""),
            })
    if not rows:
        return pd.DataFrame(columns=COMPARISON_COLUMNS)
    return _add_holm(pd.DataFrame(rows), "p_wilcoxon")[COMPARISON_COLUMNS]


def verdicts(corr: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per (outcome, window) family and potential: the scenarios with data, those where H2 is supported (sign test
    significant after Holm, positive median), and a verdict: "holds" (every scenario), "partial" (some), or "not
    supported" (none)."""
    out: dict[str, dict[str, Any]] = {}
    for family in _ordered(corr["family"], [_family("", f, o, w) for f in FORMS for w in WINDOWS for o in OUTCOMES]):
        fam = corr.loc[corr["family"] == family]
        per_potential = {}
        for p in POTENTIALS:
            rows = fam.loc[(fam["potential"] == p) & (fam["n_runs"] > 0)]
            scenarios = list(rows["scenario"])
            supported = list(rows.loc[rows["supports_h2"].astype(bool), "scenario"])
            verdict = ("holds" if scenarios and len(supported) == len(scenarios)
                       else "partial" if supported else "not supported")
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
    """Exp 2 tables (exp2_correlation, exp2_potential_comparison, exp2_leadlag) and figure 6; the JSON headline, or
    None when there are no runs."""
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
    corr = correlation_table(per_run)
    comp = potential_comparison_table(per_run)
    common.write_table(corr, out_root, "exp2_correlation")
    common.write_table(comp, out_root, "exp2_potential_comparison")
    ll = leadlag_table(used, series)
    if len(ll):
        common.write_table(ll, out_root, "exp2_leadlag")
    figure6(ll, out_root)
    primary = corr.loc[corr["role"] == "primary"]
    norm_family = _family("", "norm", PRIMARY_OUTCOME, PRIMARY_WINDOW)
    head.update({
        "primary_family": _family("", PRIMARY_FORM, PRIMARY_OUTCOME, PRIMARY_WINDOW),
        "primary": primary.drop(columns=["role"]),
        "sensitivity_normalized_primary": corr.loc[corr["family"] == norm_family].drop(columns=["role"]),
        "verdicts": verdicts(corr),
        "potential_comparisons_primary": comp.loc[comp["role"] == "primary"].drop(columns=["role"]),
        "potential_comparisons_significant": comp.loc[comp["significant"].astype(bool),
                                                      ["family", "scenario", "a", "b", "median_diff", "p_holm",
                                                       "favours"]],
        "leadlag": leadlag_summary(ll),
    })
    common.logger.info("exp2: %d runs used (%d invalid, %d duplicate configs, %d B0 excluded)", len(used),
                       counts["n_invalid"], counts["n_duplicate_config"], counts["n_excluded_label"])
    return head


def _counts(df: pd.DataFrame, by: Sequence[str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    g = df.groupby(list(by), sort=True, dropna=False).size().reset_index(name="n")
    return common.to_jsonable(g)


def _method() -> dict[str, Any]:
    """The statistical settings, recorded next to the numbers."""
    return {
        "potentials": POTENTIALS, "potentials_sensitivity_normalized": POTENTIALS_NORM,
        "why_raw": "the potentials as defined (registry exp2.params.potentials); the normalized forms divide by "
                   "ln(ready nodes) or the ready fleet price, which shrink whenever an empty node is removed, so they "
                   "rise mechanically at removals",
        "outcomes": {"removals": "nodes_removed_cum(t+w) - nodes_removed_cum(t)",
                                               "cost_decrease": "cost_per_hour(t) - cost_per_hour(t+w)"},
        "windows_min": list(WINDOWS), "primary_window_min": PRIMARY_WINDOW, "warmup_min": WARMUP_MIN,
        "min_windows_per_run": MIN_WINDOWS, "per_run_statistic": "Spearman of -(Phi(t) - Phi(t-w)) with y(t)",
        "aggregate": "median and mean over runs, 95% percentile bootstrap (10,000 resamples, seed 0)",
        "sign_test": "exact binomial, one-sided (share positive > 1/2), zeros dropped",
        "holm_families": "per (form, outcome, window): potentials x scenarios for the sign tests, potential pairs "
                         "x scenarios for the Wilcoxon comparisons",
        "comparison_test": "two-sided Wilcoxon signed-rank on per-run correlation differences, paired by run",
        "deduplication": "runs with identical manifest config apart from label/runID/maxWall count once",
        "leadlag": {"lags_min": [min(LAGS), max(LAGS)], "statistic": "Pearson of -dPhi(t) with dRemovals(t+k) over "
                    "overlapping post-warmup minutes", "min_pairs": MIN_LAG_PAIRS,
                    "aggregate": "median over runs with a 95% percentile bootstrap CI"},
    }


# --------------------------------------------------------------------------------------------------------------------
# Figures 1 and 2
# --------------------------------------------------------------------------------------------------------------------


def figure_runs(runs: pd.DataFrame, scenarios: Sequence[str], labels: Sequence[str]) -> pd.DataFrame:
    """Rows (abandoned cells included) of the remapped ``FIG_GROUPS`` at ``FIG_NODES`` nodes on ``scenarios`` and
    ``labels``."""
    if runs.empty:
        return runs.copy()
    mask = (runs["group"].isin(common.map_groups(FIG_GROUPS)) & runs["scenario"].isin(scenarios)
            & runs["label"].isin(labels) & (_floats(runs["nodes"]) == FIG_NODES))
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
    return minutes >= (1 - LATE_FRACTION) * float(minutes.max())


def _per_run_late(mat: np.ndarray, minutes: np.ndarray) -> np.ndarray:
    """Each run's mean over the last 20% of minutes."""
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
    return {"scenarios": scenarios, "arms": arms, "late_fraction": LATE_FRACTION, "warmup_min": WARMUP_MIN,
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
    return {"late_fraction": LATE_FRACTION, "arms": numbers}


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
