"""Shared library for the paper's analyses: loading raw runs, preregistered statistics, reproducible output.

Everything here reads ``experiments/runs`` (never writes it) and is deterministic: fixed bootstrap seeds, sorted
iteration, and outputs free of timestamps and absolute paths. Analysis modules use it as::

    from paper.analysis import common

    def main(runs_root: Path, out_root: Path) -> None:
        runs = common.load_runs(runs_root)
        sel = common.select_runs(runs, "exp3")
        if sel.empty:
            common.skipped("exp3")
            return
        results = common.evaluate(sel, common.comparisons("exp3"))
        common.write_table(results, out_root, "exp3_tests")

Layout of outputs under ``out_root`` (``paper/`` by default): tables in ``tables/<name>.csv`` and ``.md``, headline
numbers in ``tables/<name>.json``, figures in ``figures/out/<name>.png`` and ``.pdf``.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import logging
import math
import os
import re
import sys
import warnings
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from cycler import cycler  # noqa: E402
from scipy import stats  # noqa: E402

# --------------------------------------------------------------------------------------------------------------------
# Paths and logging
# --------------------------------------------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_ROOT = REPO_ROOT / "experiments" / "runs"
DEFAULT_OUT_ROOT = REPO_ROOT / "paper"
TABLES_DIR = PurePath("tables")
FIGURES_DIR = PurePath("figures") / "out"

logger = logging.getLogger("paper.analysis")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def skipped(name: str, reason: str = "no runs") -> None:
    """Log the standard note for an output that is not produced: ``skipped <name>: <reason>``."""
    logger.info("skipped %s: %s", name, reason)


# --------------------------------------------------------------------------------------------------------------------
# Loading raw runs
# --------------------------------------------------------------------------------------------------------------------

FAILED_INFIX = ".failed-"  # the matrix runner sets failed attempts aside as <run_id>.failed-<n>
PAIR_KEYS: tuple[str, ...] = ("scenario", "nodes", "seed")
HIST_BINS = 10
HIST_COLUMNS = ("hist_cpu", "hist_mem")

ID_COLUMNS = ["run_id", "matrix", "group", "experiment", "label", "scenario", "nodes", "seed", "placement", "scale_down"]
# Scalar summary.json fields, in the simulator's order (hack/consolidation-sim/sim/metrics.go, Summary). Missing fields
# load as NaN.
SUMMARY_FIELDS = [
    "arm", "scenario_nodes", "sim_minutes", "integrated_cost", "integrated_cost_post", "steady_cost_per_hour",
    "final_nodes", "peak_nodes", "nodes_removed", "evictions", "eviction_cost", "re_disruptions", "regret_evictions",
    "churn_nodes", "evictions_refused_by_pdb", "pdb_violations", "do_not_disrupt_violations", "pending_pod_seconds",
    "mean_time_to_schedule_s", "scale_downs", "jobs_completed", "disruption_passes", "commands_started",
    "commands_completed", "commands_failed", "fidelity_hits", "fidelity_misses", "fidelity", "predicted_delta_p2",
    "realized_delta_p2", "plans_scored", "full_simulations", "search_timeouts", "events_run", "invalid",
    "invalid_reasons", "violations", "timed_out", "error", "wall_seconds", "wall_pass_mean_ms", "wall_pass_p95_ms",
    "wall_pass_max_ms", "wall_pass_total_s", "wall_passes_over_1m", "wall_passes_over_3m",
]
# Map-valued summary fields; each key becomes a column <field>_<key> (commands_started also keeps its total).
SUMMARY_DICT_FIELDS = ("evictions_by", "pass_methods", "commands_started")
SUMMARY_LIST_FIELDS = ("invalid_reasons", "violations")
PLANNER_PARAMS = ("lambda", "t0", "cooling", "alpha", "mu")
CONFIG_COLUMNS = [
    "arm_name", "arm_policy", "planner", *(f"planner_{p}" for p in PLANNER_PARAMS), "planner_temperature_unit",
    "candidates", "moves", "moves_mode", "executor", "potential", "pod_evictions_per_minute",
    "workload_evictions_per_minute", "max_full_simulations", "max_attempts", "duration_min", "warmup_min",
    "git_sha", "git_dirty",
]
TAIL_COLUMNS = ["valid", "invalid_reason", "path"]
RUN_COLUMNS = ID_COLUMNS + SUMMARY_FIELDS + CONFIG_COLUMNS + TAIL_COLUMNS


def discover_runs(runs_root: Path) -> list[Path]:
    """Finished run directories under ``runs_root``: those holding ``summary.json``, sorted by relative path.

    ``runs_root`` may be the top of the tree (``experiments/runs``, runs at ``<matrix>/<run_id>``) or one matrix
    (``experiments/runs/pilot``). Failed attempts the runner set aside (``<run_id>.failed-<n>``) are skipped.
    """
    root = Path(runs_root)
    if not root.is_dir():
        return []
    found: set[Path] = set()
    for pattern in ("*/summary.json", "*/*/summary.json"):
        for summary in root.glob(pattern):
            if FAILED_INFIX not in summary.parent.name:
                found.add(summary.parent)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def load_runs(runs_root: Path, include_abandoned: bool = False) -> pd.DataFrame:
    """One row per finished run under ``runs_root``, sorted by (matrix, run_id).

    Columns (``RUN_COLUMNS`` always present, NaN where a run lacks the field):

    - identity: run_id, matrix (the run dir's parent name), group (run_id up to the first ``__``), experiment (group up
      to the first ``-``: exp1, abl, pilot, ...), label, scenario, nodes (= scenario_nodes), seed, placement,
      scale_down;
    - every scalar summary.json field (``SUMMARY_FIELDS``); map fields flattened to ``evictions_by_<k>``,
      ``pass_methods_<k>`` and ``commands_started_<k>`` (``commands_started`` itself is the total); list fields
      (invalid_reasons, violations) joined with ``"; "``. ``fidelity`` is NaN when no evicted pod was tracked
      (fidelity_hits + fidelity_misses == 0), since the share is undefined there;
    - config from manifest.json (NaN for baseline arms where not applicable): arm_name, arm_policy, planner,
      planner_lambda/t0/cooling/alpha/mu, planner_temperature_unit (any other planner param as ``planner_<snake>``),
      candidates, moves (names joined with ``+``), moves_mode, executor, potential, pod_evictions_per_minute and
      workload_evictions_per_minute (NaN or 0 means no pod-eviction budget), max_full_simulations, max_attempts,
      duration_min, warmup_min, git_sha, git_dirty;
    - valid (the preregistered exclusion rule: not invalid, not timed_out, empty error), invalid_reason ("" when
      valid), and path (the run directory; ``write_table`` never writes this column).

    With ``include_abandoned``, cells the runner abandoned (last index.jsonl status "abandoned" and no summary) are
    added as rows with valid False and invalid_reason "abandoned: ...", identity columns from their cell.json and
    metrics NaN, so per-experiment invalid counts include them. Their ``path`` may hold no run files.
    """
    root = Path(runs_root)
    records = []
    for d in discover_runs(root):
        try:
            records.append(_run_record(d))
        except (OSError, ValueError) as err:
            # A summary.json still being written by a running cell (or a corrupt one): not a finished run yet.
            logger.warning("skipping %s: unreadable run files (%s: %s)", d.name, type(err).__name__, err)
    if include_abandoned:
        records += _abandoned_records(root, {r["run_id"] for r in records})
    df = pd.DataFrame.from_records(records) if records else pd.DataFrame()
    for col in RUN_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    known = set(RUN_COLUMNS)
    extra = sorted(c for c in df.columns if c not in known)
    summary_extra = [c for c in extra if not c.startswith("planner_")]
    planner_extra = [c for c in extra if c.startswith("planner_")]
    order = ID_COLUMNS + SUMMARY_FIELDS + summary_extra + CONFIG_COLUMNS + planner_extra + TAIL_COLUMNS
    df = df[order]
    if len(df):
        df = df.sort_values(["matrix", "run_id"], kind="stable").reset_index(drop=True)
        df["valid"] = df["valid"].astype(bool)
        # A key absent from a finished run's map field is a zero count, not missing data.
        finished = ~df["invalid_reason"].astype(str).str.startswith("abandoned")
        counts = [c for c in summary_extra if c.startswith(tuple(f"{f}_" for f in SUMMARY_DICT_FIELDS))]
        for col in ["commands_started", *counts]:
            df.loc[finished, col] = pd.to_numeric(df.loc[finished, col], errors="coerce").fillna(0)
            if not df[col].isna().any():
                df[col] = df[col].astype("int64")
    return df


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _slug(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(key)).strip("_").lower()


def _snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).replace("-", "_").lower()


def _num(value: Any) -> float:
    """A float, or NaN for anything missing or non-numeric."""
    if isinstance(value, bool) or value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


_DURATION_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)")
_DURATION_MIN = {"ns": 1e-9 / 60, "us": 1e-6 / 60, "µs": 1e-6 / 60, "μs": 1e-6 / 60, "ms": 1e-3 / 60, "s": 1 / 60,
                 "m": 1.0, "h": 60.0}


def parse_duration_min(text: Any) -> float:
    """Minutes in a Go duration string ("3h0m0s", "90s", "1m30.5s"); NaN when missing or malformed."""
    if not isinstance(text, str) or not text:
        return math.nan
    s = text.strip()
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-")
    if s == "0":
        return 0.0
    pos, total = 0, 0.0
    for m in _DURATION_PART.finditer(s):
        if m.start() != pos:
            return math.nan
        total += float(m.group(1)) * _DURATION_MIN[m.group(2)]
        pos = m.end()
    return sign * total if pos == len(s) and pos > 0 else math.nan


def _validity(summary: Mapping[str, Any]) -> tuple[bool, str]:
    """The preregistered exclusion rule: invalid (PDB or do-not-disrupt violation), timed_out, or an error."""
    reasons = []
    if summary.get("invalid") is True:
        detail = ", ".join(str(r) for r in summary.get("invalid_reasons") or [])
        reasons.append(f"invalid: {detail}" if detail else "invalid")
    if summary.get("timed_out") is True:
        reasons.append("timed_out")
    error = summary.get("error")
    if error:
        reasons.append("error: " + str(error).replace(str(REPO_ROOT) + os.sep, ""))
    return not reasons, "; ".join(reasons)


def _identity(run_id: str, matrix: str) -> dict[str, Any]:
    group = run_id.split("__")[0]
    return {"run_id": run_id, "matrix": matrix, "group": group, "experiment": group.split("-")[0]}


def _run_record(run_dir: Path) -> dict[str, Any]:
    summary = _read_json(run_dir / "summary.json")
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    rec: dict[str, Any] = _identity(str(summary.get("run_id") or run_dir.name), run_dir.parent.name)
    for key, value in summary.items():
        if key == "run_id":
            continue
        if key in SUMMARY_DICT_FIELDS or isinstance(value, dict):
            items = value if isinstance(value, dict) else {}
            for k in sorted(items):
                rec[f"{key}_{_slug(k)}"] = items[k]
            if key == "commands_started":
                rec[key] = int(sum(_num(v) for v in items.values()))
        elif key in SUMMARY_LIST_FIELDS or isinstance(value, list):
            rec[key] = "; ".join(str(v) for v in value or [])
        else:
            rec[key] = value
    for key in SUMMARY_LIST_FIELDS:
        rec.setdefault(key, "")
    rec.setdefault("error", "")
    # Identity fields a summary lacks come from the run's own config (manifest.json, else cell.json), so a run with
    # a partial summary still pairs with its arms instead of colliding with them on a missing key.
    cfg = manifest.get("config") or {}
    if not cfg and (run_dir / "cell.json").exists():
        cfg = _read_json(run_dir / "cell.json")
    scenario = cfg.get("scenario") or {}
    for key, value in (("label", (cfg.get("arm") or {}).get("label")), ("scenario", scenario.get("name")),
                       ("scenario_nodes", scenario.get("nodes")), ("seed", cfg.get("seed")),
                       ("placement", cfg.get("placement") or "least-allocated"),
                       ("scale_down", cfg.get("scaleDown") or "spread")):
        if rec.get(key) is None and value is not None:
            rec[key] = value
    rec["nodes"] = rec.get("scenario_nodes")
    if _num(summary.get("fidelity_hits")) + _num(summary.get("fidelity_misses")) == 0:
        rec["fidelity"] = math.nan
    rec.update(_config_columns(manifest))
    rec["valid"], rec["invalid_reason"] = _validity(summary)
    rec["path"] = run_dir
    return rec


def _config_columns(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cfg = manifest.get("config") or {}
    arm = cfg.get("arm") or {}
    pipe = arm.get("pipeline") or {}
    out: dict[str, Any] = {c: math.nan for c in CONFIG_COLUMNS}
    out["arm_name"] = arm.get("name", math.nan)
    out["arm_policy"] = arm.get("policy", math.nan)
    if pipe:
        planner = pipe.get("planner") or {}
        params = planner.get("params") or {}
        out["planner"] = planner.get("name", math.nan)
        for p in PLANNER_PARAMS:
            out[f"planner_{p}"] = _num(params.get(p))
        out["planner_temperature_unit"] = params.get("temperatureUnit", math.nan)
        for k in sorted(params):
            if k not in PLANNER_PARAMS and k != "temperatureUnit":
                v = params[k]
                out[f"planner_{_snake(k)}"] = v if isinstance(v, str) else _num(v)
        out["candidates"] = (pipe.get("candidates") or {}).get("name", math.nan)
        moves = pipe.get("moves") or []
        if isinstance(moves, dict):
            moves = [moves]
        out["moves"] = "+".join(str(m.get("name", "")) for m in moves) if moves else math.nan
        out["moves_mode"] = pipe.get("movesMode", math.nan)
        out["executor"] = (pipe.get("executor") or {}).get("name", math.nan)
        out["potential"] = (pipe.get("potential") or {}).get("name", math.nan)
        budgets = pipe.get("budgets") or {}
        out["pod_evictions_per_minute"] = _num(budgets.get("podEvictionsPerMinute"))
        out["workload_evictions_per_minute"] = _num(budgets.get("workloadEvictionsPerMinute"))
        search = pipe.get("search") or {}
        out["max_full_simulations"] = _num(search.get("maxFullSimulations"))
        out["max_attempts"] = _num(search.get("maxAttempts"))
    out["duration_min"] = parse_duration_min(cfg.get("duration"))
    out["warmup_min"] = parse_duration_min(cfg.get("warmup"))
    out["git_sha"] = manifest.get("git_sha", math.nan)
    out["git_dirty"] = manifest.get("git_dirty", math.nan)
    return out


def _index_files(root: Path) -> list[Path]:
    return sorted([*root.glob("index.jsonl"), *root.glob("*/index.jsonl")], key=lambda p: p.as_posix())


def _abandoned_records(root: Path, finished: set[str]) -> list[dict[str, Any]]:
    """Rows for cells whose last index.jsonl status is "abandoned" and that never wrote a summary."""
    records = []
    for index in _index_files(root):
        last: dict[str, dict[str, Any]] = {}
        with open(index, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:  # a line the runner is still appending
                    logger.warning("%s: skipping a partial line", index.name)
                    continue
                if isinstance(entry, dict) and "run_id" in entry:
                    last[entry["run_id"]] = entry
        matrix_dir = index.parent
        for run_id in sorted(last):
            entry = last[run_id]
            if entry.get("status") != "abandoned" or run_id in finished:
                continue
            cell = _latest_cell(matrix_dir, run_id)
            arm = cell.get("arm") or {}
            scenario = cell.get("scenario") or {}
            rec = _identity(run_id, matrix_dir.name)
            rec.update({
                "label": arm.get("label") or arm.get("name", math.nan), "arm": arm.get("name", math.nan),
                "arm_name": arm.get("name", math.nan), "scenario": scenario.get("name", math.nan),
                "nodes": scenario.get("nodes", math.nan), "scenario_nodes": scenario.get("nodes", math.nan),
                "seed": cell.get("seed", math.nan), "placement": cell.get("placement", "least-allocated"),
                "scale_down": cell.get("scaleDown", "spread"), "valid": False,
                "invalid_reason": "abandoned" + (f": {entry['error']}" if entry.get("error") else ""),
                "path": matrix_dir / run_id,
            })
            records.append(rec)
    return records


def _latest_cell(matrix_dir: Path, run_id: str) -> dict[str, Any]:
    candidates = [matrix_dir / run_id / "cell.json"]
    candidates += sorted(matrix_dir.glob(f"{run_id}{FAILED_INFIX}*/cell.json"),
                         key=lambda p: int(re.sub(r"\D", "", p.parent.name.rsplit(FAILED_INFIX, 1)[1]) or 0),
                         reverse=True)
    for c in candidates:
        if c.exists():
            return _read_json(c)
    return {}


def load_timeseries(run_dir: Path) -> pd.DataFrame:
    """A run's timeseries.csv, one row per simulated minute, with each histogram column parsed.

    ``hist_cpu`` and ``hist_mem`` (10 ``;``-separated node counts for utilization bins [0, 0.1), ..., [0.9, 1.0])
    are replaced by integer columns ``hist_cpu_0`` .. ``hist_cpu_9`` and ``hist_mem_0`` .. ``hist_mem_9``.
    """
    df = pd.read_csv(Path(run_dir) / "timeseries.csv", dtype={c: str for c in HIST_COLUMNS})
    for col in HIST_COLUMNS:
        if col not in df.columns:
            continue
        rows = []
        for i, cell in enumerate(df[col].tolist()):
            parts = cell.split(";") if isinstance(cell, str) and cell else []
            if len(parts) != HIST_BINS:
                raise ValueError(f"{Path(run_dir).name}/timeseries.csv row {i}: {col} has {len(parts)} bins, "
                                 f"want {HIST_BINS}")
            rows.append([int(p) for p in parts])
        counts = np.asarray(rows, dtype=np.int64).reshape(-1, HIST_BINS)
        df = df.drop(columns=[col])
        for b in range(HIST_BINS):
            df[f"{col}_{b}"] = counts[:, b]
    return df


def load_podsets(run_dir: Path) -> list[dict[str, Any]]:
    """A run's podsets.jsonl, sorted by t_min: ``{"t_min", "cost_per_hour", "nodes", "shapes"}`` per snapshot.

    ``shapes`` is the reschedulable pods' request multiset at that instant as ``(cpu_millicores, memory_mib, count)``
    integer tuples; ``cost_per_hour`` and ``nodes`` are the run's own fleet then. Empty when the file is missing.
    """
    path = Path(run_dir) / "podsets.jsonl"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d["shapes"] = [(int(c), int(m), int(n)) for c, m, n in d.get("shapes") or []]
            out.append(d)
    out.sort(key=lambda d: float(d.get("t_min", math.nan)))
    return out


def load_decisions(run_dir: Path) -> Iterator[dict[str, Any]]:
    """Iterate a run's decision log, one dict per line, from decisions.jsonl.gz (newer runs) or decisions.jsonl.

    When both exist the gzipped log is read (the simulator writes one or the other). Yields nothing if neither
    exists. The file is opened lazily and closed when iteration ends.
    """
    d = Path(run_dir)
    gz, plain = d / "decisions.jsonl.gz", d / "decisions.jsonl"
    if gz.exists():
        f = gzip.open(gz, "rt", encoding="utf-8")
    elif plain.exists():
        f = open(plain, encoding="utf-8")  # noqa: SIM115 - closed by the with below
    else:
        return
    with f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_catalog(repo_root: Path = REPO_ROOT) -> pd.DataFrame:
    """experiments/catalog.json: the instance types the simulator can launch, sorted by name.

    Columns: name, family, cpu_millicores, memory_mib, pods (allocatable), price_per_hour.
    """
    df = pd.DataFrame(_read_json(Path(repo_root) / "experiments" / "catalog.json"))
    return df.sort_values("name", kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------------------------------------------------
# Experiment registry
# --------------------------------------------------------------------------------------------------------------------

EXPERIMENTS_FILE = Path(__file__).with_name("experiments.yaml")
GROUP_MAP_ENV = "ANALYSIS_GROUP_MAP"
TESTS = ("tost", "wilcoxon")
EXPECTATIONS = ("lower", "higher", "not_higher", "not_lower", "equal")


def _load_registry(path: Path) -> dict[str, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    exps = doc["experiments"]
    for key, e in exps.items():
        e.setdefault("title", key)
        e.setdefault("hypothesis", None)
        e.setdefault("groups", [])
        e.setdefault("select", {})
        e.setdefault("scenarios", [])
        e.setdefault("nodes", [])
        e.setdefault("labels", [])
        e.setdefault("baselines", [])
        e.setdefault("secondary_metrics", [])
        e.setdefault("pair_keys", list(PAIR_KEYS))
        e.setdefault("comparisons", [])
        e.setdefault("params", {})
    return exps


EXPERIMENTS: dict[str, dict[str, Any]] = _load_registry(EXPERIMENTS_FILE)


def group_map() -> dict[str, list[str]]:
    """The ANALYSIS_GROUP_MAP override: session group name -> replacement group names (read on every call).

    The variable is a JSON object whose values are a group name or a list of names, e.g.
    ``{"exp1-n200": "pilot-exp1", "exp1-baselines-n200": "pilot-baselines"}``. Unset or empty means no remapping.
    """
    raw = os.environ.get(GROUP_MAP_ENV, "").strip()
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{GROUP_MAP_ENV} is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"{GROUP_MAP_ENV} must be a JSON object, got {type(doc).__name__}")
    out = {}
    for k, v in doc.items():
        targets = [v] if isinstance(v, str) else list(v)
        if not all(isinstance(t, str) for t in targets):
            raise ValueError(f"{GROUP_MAP_ENV}[{k!r}] must be a group name or a list of names")
        out[str(k)] = targets
    return out


def map_groups(groups: Iterable[str]) -> list[str]:
    """Session group names after the ANALYSIS_GROUP_MAP override, de-duplicated in order."""
    mapping = group_map()
    out: list[str] = []
    for g in groups:
        for t in mapping.get(g, [g]):
            if t not in out:
                out.append(t)
    return out


def experiment(key: str) -> dict[str, Any]:
    """The registry entry for ``key`` (exp1..exp8, abl), as loaded from experiments.yaml (session group names)."""
    try:
        return EXPERIMENTS[key]
    except KeyError:
        raise KeyError(f"unknown experiment {key!r}; known: {', '.join(EXPERIMENTS)}") from None


def experiment_groups(key: str) -> list[str]:
    """The run groups of experiment ``key`` after the ANALYSIS_GROUP_MAP override."""
    return map_groups(experiment(key)["groups"])


def select_runs(runs: pd.DataFrame, key: str, valid_only: bool = False) -> pd.DataFrame:
    """Rows of ``runs`` belonging to experiment ``key``: its (remapped) groups, then its ``select`` filters.

    Invalid runs are kept unless ``valid_only`` (they must be counted and reported); ``paired`` and ``evaluate``
    drop them pair-wise. Select by this, not by the ``experiment`` column, which is the group's own prefix (pilot runs
    say "pilot", and Exp 7 reuses Exp 1's groups).
    """
    e = experiment(key)
    if runs.empty:
        return runs.copy()
    mask = runs["group"].isin(map_groups(e["groups"]))
    for col, allowed in (e.get("select") or {}).items():
        mask &= runs[col].isin(_as_list(allowed))
    if valid_only:
        mask &= runs["valid"].astype(bool)
    return runs.loc[mask].copy()


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


ArmSelector = str | Mapping[str, Any]


@dataclass
class Comparison:
    """One preregistered paired comparison (an expanded experiments.yaml entry). Differences are ``a - b``."""

    experiment: str
    name: str
    family: str
    test: str  # "tost" or "wilcoxon"
    metric: str
    a: ArmSelector
    b: ArmSelector
    by: tuple[str, ...] = ("scenario", "nodes")
    where: dict[str, list[Any]] = field(default_factory=dict)
    margin: float | None = None
    margin_type: str = "relative"  # "relative" (fraction of b's mean over the pairs) or "absolute"
    expect: str | None = None
    keys: tuple[str, ...] = PAIR_KEYS
    label_col: str = "label"


def _fill(obj: Any, ctx: Mapping[str, Any]) -> Any:
    if isinstance(obj, str):
        return obj.format_map(ctx) if ctx and "{" in obj else obj
    if isinstance(obj, dict):
        return {k: _fill(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, ctx) for v in obj]
    return obj


def comparisons(key: str) -> list[Comparison]:
    """Experiment ``key``'s comparisons with every ``foreach`` expanded (cartesian product, in listed order)."""
    e = experiment(key)
    out = []
    for raw in e.get("comparisons") or []:
        raw = dict(raw)
        foreach = raw.pop("foreach", None) or {}
        names = list(foreach)
        for combo in itertools.product(*(foreach[n] for n in names)) if names else [()]:
            c = _fill(raw, dict(zip(names, combo)))
            test = c.get("test", "wilcoxon")
            if test not in TESTS:
                raise ValueError(f"{key}: comparison {c.get('name')!r} has test {test!r}, want one of {TESTS}")
            if test == "tost" and c.get("margin") is None:
                raise ValueError(f"{key}: TOST comparison {c.get('name')!r} needs a margin")
            if c.get("expect") is not None and c["expect"] not in EXPECTATIONS:
                raise ValueError(f"{key}: comparison {c.get('name')!r} expects {c['expect']!r}")
            out.append(Comparison(
                experiment=key, name=c.get("name", f"{c['a']} vs {c['b']}"), family=c.get("family", key),
                test=test, metric=c["metric"], a=c["a"], b=c["b"], by=tuple(c.get("by", ("scenario", "nodes"))),
                where={k: _as_list(v) for k, v in (c.get("where") or {}).items()},
                margin=None if c.get("margin") is None else float(c["margin"]),
                margin_type=c.get("margin_type", "relative"), expect=c.get("expect"),
                keys=tuple(e.get("pair_keys") or PAIR_KEYS),
            ))
    return out


# --------------------------------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------------------------------

N_BOOT = 10_000
BOOT_SEED = 0
ALPHA = 0.05
_BOOT_CELLS = 4_000_000  # resample matrix elements per chunk


def _finite(values: Any) -> np.ndarray:
    x = np.asarray(values, dtype=float).ravel()
    return x[np.isfinite(x)]


def _resample(rng: np.random.Generator, n: int, size: int, stat: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    """``n`` bootstrap replicates of ``stat`` (applied row-wise to an (m, size) index matrix), in chunks."""
    out = np.empty(n, dtype=float)
    chunk = max(1, _BOOT_CELLS // max(size, 1))
    for start in range(0, n, chunk):
        m = min(chunk, n - start)
        out[start:start + m] = stat(rng.integers(0, size, size=(m, size)))
    return out


def bootstrap_ci(values: Any, stat: str = "mean", n: int = N_BOOT, alpha: float = ALPHA,
                 seed: int = BOOT_SEED) -> tuple[float, float, float]:
    """(estimate, lo, hi): the sample mean or median of the finite ``values`` and its percentile bootstrap interval.

    ``n`` resamples with replacement from ``np.random.default_rng(seed)``; the interval is the
    [alpha/2, 1 - alpha/2] percentiles of the resampled statistic. NaN triple when there are no finite values.
    """
    if stat not in ("mean", "median"):
        raise ValueError(f"stat {stat!r}, want 'mean' or 'median'")
    x = _finite(values)
    if x.size == 0:
        return math.nan, math.nan, math.nan
    est = float(x.mean() if stat == "mean" else np.median(x))
    rng = np.random.default_rng(seed)
    if stat == "mean":
        boot = _resample(rng, n, x.size, lambda idx: x[idx].mean(axis=1))
    else:
        boot = _resample(rng, n, x.size, lambda idx: np.median(x[idx], axis=1))
    lo, hi = np.percentile(boot, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return est, float(lo), float(hi)


def bootstrap_pct_ci(a: Any, b: Any, n: int = N_BOOT, alpha: float = ALPHA,
                     seed: int = BOOT_SEED) -> tuple[float, float, float]:
    """(estimate, lo, hi) of the mean paired difference as a percent of b's mean: 100 * mean(a - b) / mean(b).

    Pairs are resampled jointly (percentile bootstrap, ``default_rng(seed)``). Pairs with a non-finite side are
    dropped. NaN triple when there are no pairs or b's mean is zero.
    """
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size == 0 or b.mean() == 0:
        return math.nan, math.nan, math.nan
    d = a - b
    est = float(100 * d.mean() / b.mean())
    rng = np.random.default_rng(seed)
    with np.errstate(divide="ignore", invalid="ignore"):
        boot = _resample(rng, n, a.size, lambda idx: 100 * d[idx].mean(axis=1) / b[idx].mean(axis=1))
    boot = boot[np.isfinite(boot)]
    if boot.size == 0:
        return est, math.nan, math.nan
    lo, hi = np.percentile(boot, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return est, float(lo), float(hi)


def describe_arm(arm: ArmSelector) -> str:
    """A display string for an arm selector: the label itself, or ``col=value, ...`` for a filter."""
    if isinstance(arm, str):
        return arm
    return ", ".join(f"{k}={v}" for k, v in arm.items())


def arm_mask(df: pd.DataFrame, arm: ArmSelector, label_col: str = "label") -> pd.Series:
    """Boolean mask of the rows an arm selector picks: ``label_col == arm``, or every ``{column: value}`` matching
    (a list value matches any of its items)."""
    if isinstance(arm, str):
        return df[label_col] == arm
    mask = pd.Series(True, index=df.index)
    for col, value in arm.items():
        if col not in df.columns:
            raise KeyError(f"arm selector {describe_arm(arm)!r}: no column {col!r}")
        mask &= df[col].isin(_as_list(value))
    return mask


def _key_sort(t: tuple[Any, ...]) -> tuple[Any, ...]:
    """Sort key for key tuples mixing numbers and strings (numbers first, numerically)."""
    return tuple((0, float(x), "") if isinstance(x, (int, float, np.number)) and not isinstance(x, bool)
                 else (1, 0.0, str(x)) for x in t)


def _key_tuple(t: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(x.item() if isinstance(x, np.generic) else x for x in t)


@dataclass
class Aligned:
    """Runs of several arms aligned on shared keys (``align``)."""

    table: pd.DataFrame  # key columns plus one metric column per arm (named by describe_arm), sorted by key
    arms: list[str]  # the arm column names, in the order given
    dropped: int  # keys present in some arm but not kept: missing in an arm, or invalid / non-finite in one
    dropped_invalid: int  # of those, keys present in every arm but invalid or non-finite in at least one
    dropped_missing: int  # of those, keys absent from at least one arm

    @property
    def n(self) -> int:
        return len(self.table)


def align(df: pd.DataFrame, arms: Sequence[ArmSelector], metric: str, keys: Sequence[str] = PAIR_KEYS,
          label_col: str = "label") -> Aligned:
    """Align ``metric`` across ``arms`` on ``keys``, keeping a key only when every arm has a valid, finite run there.

    Implements the preregistered rule that a comparison drops a seed when any of its arms is invalid for it, and
    counts what it dropped. Raises ValueError when an arm has two runs with the same keys (the selector or the keys
    are missing a distinguishing column, e.g. placement in Exp 5). Rows lacking a ``valid`` column count as valid.
    """
    keys = list(keys)
    names = [describe_arm(a) for a in arms]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate arms: {names}")
    present: list[set[tuple[Any, ...]]] = []
    usable: list[dict[tuple[Any, ...], float]] = []
    for arm, name in zip(arms, names):
        sub = df.loc[arm_mask(df, arm, label_col)]
        dup = sub.duplicated(keys, keep=False)
        if dup.any():
            raise ValueError(f"arm {name!r} has {int(dup.sum())} runs sharing keys {keys}; add the distinguishing "
                             f"column to the arm selector or to the strata")
        tuples = [_key_tuple(t) for t in sub[keys].itertuples(index=False, name=None)]
        values = pd.to_numeric(sub[metric], errors="coerce").to_numpy(dtype=float)
        valid = sub["valid"].astype(bool).to_numpy() if "valid" in sub.columns else np.ones(len(sub), dtype=bool)
        present.append(set(tuples))
        usable.append({t: v for t, v, ok in zip(tuples, values, valid) if ok and np.isfinite(v)})
    universe = set().union(*present) if present else set()
    kept = sorted(set.intersection(*(set(u) for u in usable)) if usable else set(), key=_key_sort)
    missing = sum(1 for t in universe if any(t not in p for p in present))
    table = pd.DataFrame(kept, columns=keys) if kept else pd.DataFrame({k: [] for k in keys})
    for name, u in zip(names, usable):
        table[name] = np.array([u[t] for t in kept], dtype=float)
    dropped = len(universe) - len(kept)
    return Aligned(table=table, arms=names, dropped=dropped, dropped_invalid=dropped - missing,
                   dropped_missing=missing)


@dataclass
class Paired:
    """Two arms aligned for a paired test. Unpacks as ``a, b, keys, dropped = paired(...)``."""

    a: np.ndarray
    b: np.ndarray
    keys: pd.DataFrame  # the shared key rows, aligned with a and b
    dropped: int  # pairs dropped for invalid or missing runs
    dropped_invalid: int
    dropped_missing: int

    def __iter__(self) -> Iterator[Any]:
        return iter((self.a, self.b, self.keys, self.dropped))

    @property
    def n(self) -> int:
        return len(self.a)


def paired(df: pd.DataFrame, keys: Sequence[str] = PAIR_KEYS, label_col: str = "label",
           a: ArmSelector | None = None, b: ArmSelector | None = None, metric: str | None = None) -> Paired:
    """Arms ``a`` and ``b`` of ``df`` aligned on ``keys``: their ``metric`` values as arrays, the shared key rows, and
    how many pairs were dropped because a run was invalid, non-finite or missing (see ``align``).

    An arm is a label (matched against ``label_col``) or a ``{column: value}`` filter.
    """
    if a is None or b is None or metric is None:
        raise TypeError("paired() needs a, b and metric")
    al = align(df, [a, b], metric, keys, label_col)
    na, nb = al.arms
    return Paired(a=al.table[na].to_numpy(dtype=float), b=al.table[nb].to_numpy(dtype=float),
                  keys=al.table[list(keys)].reset_index(drop=True), dropped=al.dropped,
                  dropped_invalid=al.dropped_invalid, dropped_missing=al.dropped_missing)


def paired_lenient(df: pd.DataFrame, keys: Sequence[str] = PAIR_KEYS, label_col: str = "label",
                   a: ArmSelector | None = None, b: ArmSelector | None = None, metric: str | None = None,
                   where: str = "", log: bool = True) -> Paired:
    """``paired``, except that an arm with two runs on the same keys (no pairing is defined) logs a warning (unless
    ``log`` is False, for a stratum already reported) and gives no pairs, with every key counted as dropped
    (``dropped_invalid``), instead of raising, so one bad stratum does not abandon a whole analysis."""
    try:
        return paired(df, keys, label_col, a, b, metric)
    except ValueError as err:
        if "sharing keys" not in str(err):
            raise
        if log:
            logger.warning("%s%s", f"{where}: " if where else "", err)
        n_keys = len({_key_tuple(t) for t in df[list(keys)].itertuples(index=False, name=None)})
        return Paired(a=np.array([], dtype=float), b=np.array([], dtype=float),
                      keys=pd.DataFrame({k: [] for k in keys}), dropped=n_keys, dropped_invalid=n_keys,
                      dropped_missing=0)


def wilcoxon_paired(a: Any, b: Any) -> float:
    """Two-sided Wilcoxon signed-rank p-value for the paired differences a - b (scipy.stats.wilcoxon defaults).

    Pairs with a non-finite side are dropped. 1.0 when every difference is zero; NaN when there are no pairs.
    """
    d = np.asarray(a, dtype=float).ravel() - np.asarray(b, dtype=float).ravel()
    d = d[np.isfinite(d)]
    if d.size == 0:
        return math.nan
    if np.all(d == 0):
        return 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return float(stats.wilcoxon(d, alternative="two-sided").pvalue)


def wilcoxon_floor(n_nonzero: Any) -> float:
    """The smallest two-sided p-value an exact Wilcoxon signed-rank test can return with ``n_nonzero`` non-zero
    paired differences: 2 / 2^n (all differences of one sign), 1.0 without any, NaN when ``n_nonzero`` is missing.

    Below six differences it exceeds 0.05, so no difference can be significant; under Holm over m tests a test can
    be rejected on its own only when ``m * wilcoxon_floor(n) < alpha`` (10 pairs: m <= 25; 9 pairs: m <= 12).
    """
    n = _num(n_nonzero)
    if not math.isfinite(n):
        return math.nan
    return float(min(1.0, 2.0 ** (1 - int(n)))) if n > 0 else 1.0


def holm_reachable(p_floor: Any, pvalues: Any, alpha: float = ALPHA) -> np.ndarray:
    """Per test of one Holm family: whether its smallest attainable p-value (``p_floor``) times the family size m
    (the tests with a finite p-value in ``pvalues``) is below ``alpha``, i.e. whether the test could come out
    significant after Holm whatever the other tests show. False flags a test whose non-significance says nothing
    (too few non-zero pairs for the family size); NaN floors give False."""
    floor = np.asarray(p_floor, dtype=float).ravel()
    m = int(np.isfinite(np.asarray(pvalues, dtype=float)).sum())
    with np.errstate(invalid="ignore"):
        return np.isfinite(floor) & (floor * m < alpha)


def tost_paired(a: Any, b: Any, margin: float, alpha: float = ALPHA) -> dict[str, Any]:
    """Two one-sided paired t-tests of equivalence of a and b within +-margin (absolute units).

    Returns ``mean_diff`` (mean of a - b), ``ci90_lo``/``ci90_hi`` (the 1 - 2*alpha t interval of the mean
    difference, 90% at alpha 0.05), ``p_lower`` (H0: mean <= -margin), ``p_upper`` (H0: mean >= +margin),
    ``p_tost`` = max of the two, ``equivalent`` (p_tost < alpha, i.e. the interval lies inside (-margin, +margin)),
    ``noninferior`` (p_upper < alpha: a is not worse than b by the margin when lower is better), ``n`` and ``margin``.
    Needs at least two finite pairs; zero-variance differences give p 0 or 1 by where the constant lies.
    """
    d = np.asarray(a, dtype=float).ravel() - np.asarray(b, dtype=float).ravel()
    d = d[np.isfinite(d)]
    n = int(d.size)
    out: dict[str, Any] = {"n": n, "margin": float(margin), "mean_diff": math.nan, "ci90_lo": math.nan,
                           "ci90_hi": math.nan, "p_lower": math.nan, "p_upper": math.nan, "p_tost": math.nan,
                           "equivalent": False, "noninferior": False}
    if n == 0:
        return out
    mean = float(d.mean())
    out["mean_diff"] = mean
    if n < 2 or not np.isfinite(margin) or margin <= 0:
        return out
    sd = float(d.std(ddof=1))
    if sd == 0:
        p_lower = 0.0 if mean > -margin else 1.0
        p_upper = 0.0 if mean < margin else 1.0
        lo = hi = mean
    else:
        p_lower = float(stats.ttest_1samp(d, -margin, alternative="greater").pvalue)
        p_upper = float(stats.ttest_1samp(d, margin, alternative="less").pvalue)
        half = float(stats.t.ppf(1 - alpha, n - 1)) * sd / math.sqrt(n)
        lo, hi = mean - half, mean + half
    p_tost = max(p_lower, p_upper)
    out.update(ci90_lo=lo, ci90_hi=hi, p_lower=p_lower, p_upper=p_upper, p_tost=p_tost,
               equivalent=bool(p_tost < alpha), noninferior=bool(p_upper < alpha))
    return out


def holm(pvalues: Any) -> np.ndarray:
    """Holm step-down adjusted p-values, in input order. NaN entries stay NaN and do not count toward m."""
    p = np.asarray(pvalues, dtype=float).ravel()
    out = np.full(p.shape, math.nan)
    ok = np.flatnonzero(np.isfinite(p))
    m = ok.size
    if m == 0:
        return out
    order = ok[np.argsort(p[ok], kind="stable")]
    adj = np.maximum.accumulate(np.minimum(1.0, (m - np.arange(m)) * p[order]))
    out[order] = adj
    return out


def paired_effect(a: Any, b: Any, n: int = N_BOOT, alpha: float = ALPHA, seed: int = BOOT_SEED) -> dict[str, float]:
    """Effect sizes of paired a - b: means, mean and median difference with bootstrap CIs, percent of b's mean with
    CI, and the Wilcoxon p-value. Keys: n_pairs, mean_a, mean_b, mean_diff(_lo/_hi), median_diff(_lo/_hi),
    pct_diff(_lo/_hi), p_wilcoxon, and p_floor (``wilcoxon_floor`` of the non-zero differences; NaN without
    pairs)."""
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    d = a - b
    md, md_lo, md_hi = bootstrap_ci(d, "mean", n, alpha, seed)
    med, med_lo, med_hi = bootstrap_ci(d, "median", n, alpha, seed)
    pct, pct_lo, pct_hi = bootstrap_pct_ci(a, b, n, alpha, seed)
    finite = d[np.isfinite(d)]
    return {"n_pairs": int(finite.size), "mean_a": float(a.mean()) if a.size else math.nan,
            "mean_b": float(b.mean()) if b.size else math.nan, "mean_diff": md, "mean_diff_lo": md_lo,
            "mean_diff_hi": md_hi, "median_diff": med, "median_diff_lo": med_lo, "median_diff_hi": med_hi,
            "pct_diff": pct, "pct_diff_lo": pct_lo, "pct_diff_hi": pct_hi, "p_wilcoxon": wilcoxon_paired(a, b),
            "p_floor": wilcoxon_floor(int((finite != 0).sum())) if finite.size else math.nan}


COMPARISON_COLUMNS = [
    "experiment", "family", "comparison", "test", "metric", "a", "b", "expect", "stratum", "n_pairs", "n_dropped",
    "n_dropped_invalid", "n_dropped_missing", "mean_a", "mean_b", "mean_diff", "mean_diff_lo", "mean_diff_hi",
    "median_diff", "median_diff_lo", "median_diff_hi", "pct_diff", "pct_diff_lo", "pct_diff_hi", "p_wilcoxon",
    "p_floor", "margin", "ci90_lo", "ci90_hi", "p_lower", "p_upper", "p_tost", "equivalent", "noninferior", "p",
    "p_holm", "significant", "holm_reachable", "equivalent_holm",
]


def _strata(df: pd.DataFrame, by: Sequence[str]) -> list[tuple[Any, ...]]:
    if not by:
        return [()]
    tuples = {_key_tuple(t) for t in df[list(by)].itertuples(index=False, name=None)}
    return sorted(tuples, key=_key_sort)


def _stratum_rows(df: pd.DataFrame, by: Sequence[str], values: tuple[Any, ...]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, v in zip(by, values):
        mask &= df[col].isna() if isinstance(v, float) and math.isnan(v) else df[col] == v
    return df.loc[mask]


def compare(runs: pd.DataFrame, comparison: Comparison, n: int = N_BOOT, alpha: float = ALPHA,
            seed: int = BOOT_SEED) -> pd.DataFrame:
    """One row per stratum (``comparison.by``) of a paired comparison: pair counts (kept and dropped), means, effect
    sizes with bootstrap CIs (``paired_effect``), the Wilcoxon p-value and, for TOST, the equivalence test against
    the margin in absolute units (``margin``; relative margins are a fraction of b's mean over the kept pairs).
    ``p`` is the comparison's primary p-value (p_tost or p_wilcoxon); ``evaluate`` adds the Holm columns. The stratum
    columns themselves (e.g. scenario, nodes) are added after ``stratum``, its ``col=value`` display string.
    """
    c = comparison
    df = runs
    for col, allowed in c.where.items():
        df = df.loc[df[col].isin(allowed)]
    df = df.loc[arm_mask(df, c.a, c.label_col) | arm_mask(df, c.b, c.label_col)]
    rows = []
    for values in (_strata(df, c.by) if len(df) else []):
        part = _stratum_rows(df, c.by, values)
        p = paired_lenient(part, c.keys, c.label_col, c.a, c.b, c.metric,
                           where=f"{c.experiment}: {c.name}, " + ", ".join(f"{k}={v}" for k, v in zip(c.by, values)))
        row: dict[str, Any] = {
            "experiment": c.experiment, "family": c.family, "comparison": c.name, "test": c.test,
            "metric": c.metric, "a": describe_arm(c.a), "b": describe_arm(c.b), "expect": c.expect,
            "stratum": ", ".join(f"{k}={v}" for k, v in zip(c.by, values)),
            "n_dropped": p.dropped, "n_dropped_invalid": p.dropped_invalid, "n_dropped_missing": p.dropped_missing,
        }
        row.update(dict(zip(c.by, values)))
        row.update(paired_effect(p.a, p.b, n, alpha, seed))
        row["p"] = row["p_wilcoxon"]
        if c.test == "tost":
            row["p_floor"] = math.nan  # a t-test p-value has no floor
            ref = row["mean_b"]
            margin = c.margin * abs(ref) if c.margin_type == "relative" else c.margin
            t = tost_paired(p.a, p.b, margin if margin is not None and np.isfinite(margin) else math.nan, alpha)
            row.update({k: t[k] for k in ("margin", "ci90_lo", "ci90_hi", "p_lower", "p_upper", "p_tost",
                                          "equivalent", "noninferior")})
            row["p"] = t["p_tost"]
        rows.append(row)
    out = pd.DataFrame(rows)
    strata_cols = [b for b in c.by if b not in COMPARISON_COLUMNS]
    for col in COMPARISON_COLUMNS + strata_cols:
        if col not in out.columns:
            out[col] = np.nan
    return out[COMPARISON_COLUMNS[:9] + strata_cols + COMPARISON_COLUMNS[9:]]


def evaluate(runs: pd.DataFrame, comps: Sequence[Comparison], n: int = N_BOOT, alpha: float = ALPHA,
             seed: int = BOOT_SEED) -> pd.DataFrame:
    """Every comparison in ``comps`` (``compare``), concatenated in order, with Holm correction applied to ``p``
    within each (experiment, family): ``p_holm``, ``significant`` (p_holm < alpha), ``holm_reachable`` (Wilcoxon
    rows: whether the test has enough non-zero pairs to be significant after Holm on its own, ``holm_reachable``;
    None for TOST rows) and, for TOST rows, ``equivalent_holm`` (p_tost after Holm < alpha). Strata with no pairs
    have NaN p and do not count toward m.
    """
    frames = [compare(runs, c, n, alpha, seed) for c in comps]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=COMPARISON_COLUMNS)
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["p_holm"] = np.nan
    reachable = np.zeros(len(out), dtype=bool)
    for _, idx in out.groupby(["experiment", "family"], sort=True, dropna=False).indices.items():
        p = out["p"].to_numpy(dtype=float)[idx]
        out.loc[out.index[idx], "p_holm"] = holm(p)
        reachable[idx] = holm_reachable(out["p_floor"].to_numpy(dtype=float)[idx], p, alpha)
    p_holm = out["p_holm"].to_numpy(dtype=float)
    out["significant"] = np.isfinite(p_holm) & (p_holm < alpha)
    out["holm_reachable"] = pd.Series([bool(r) if t != "tost" else None for r, t in zip(reachable, out["test"])],
                                      dtype=object)
    out["equivalent_holm"] = pd.Series([bool(s) if t == "tost" else None
                                        for s, t in zip(out["significant"], out["test"])], dtype=object)
    strata_cols = [c for c in out.columns if c not in COMPARISON_COLUMNS]
    return out[COMPARISON_COLUMNS[:9] + strata_cols + COMPARISON_COLUMNS[9:]]


def evaluate_experiment(runs: pd.DataFrame, key: str, n: int = N_BOOT, alpha: float = ALPHA,
                        seed: int = BOOT_SEED) -> pd.DataFrame:
    """``evaluate`` over experiment ``key``'s runs (``select_runs``) and comparisons (``comparisons``)."""
    return evaluate(select_runs(runs, key), comparisons(key), n, alpha, seed)


def summarize(runs: pd.DataFrame, metrics: Sequence[str], by: Sequence[str] = ("scenario", "nodes", "label"),
              n: int = N_BOOT, alpha: float = ALPHA, seed: int = BOOT_SEED) -> pd.DataFrame:
    """Per-cell descriptive statistics: one row per ``by`` combination (sorted), with n_runs, n_valid, n_invalid,
    and for each metric over the valid runs ``<m>_mean``, ``<m>_mean_lo``, ``<m>_mean_hi``, ``<m>_median``,
    ``<m>_median_lo``, ``<m>_median_hi`` (percentile bootstrap)."""
    by = list(by)
    columns = by + ["n_runs", "n_valid", "n_invalid"]
    for m in metrics:
        columns += [f"{m}_{s}" for s in ("mean", "mean_lo", "mean_hi", "median", "median_lo", "median_hi")]
    if runs.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for values in _strata(runs, by):
        part = _stratum_rows(runs, by, values)
        valid = part.loc[part["valid"].astype(bool)]
        row: dict[str, Any] = dict(zip(by, values))
        row.update(n_runs=len(part), n_valid=len(valid), n_invalid=len(part) - len(valid))
        for m in metrics:
            vals = pd.to_numeric(valid[m], errors="coerce").to_numpy(dtype=float)
            mean, mlo, mhi = bootstrap_ci(vals, "mean", n, alpha, seed)
            med, dlo, dhi = bootstrap_ci(vals, "median", n, alpha, seed)
            row.update({f"{m}_mean": mean, f"{m}_mean_lo": mlo, f"{m}_mean_hi": mhi,
                        f"{m}_median": med, f"{m}_median_lo": dlo, f"{m}_median_hi": dhi})
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def validity_report(runs: pd.DataFrame, by: Sequence[str] = ("experiment", "group")) -> pd.DataFrame:
    """Invalid-run counts per ``by`` combination (sorted), for the per-experiment exclusion report: n_runs, n_valid,
    n_invalid, n_violation (invalid flag), n_timed_out, n_error, n_abandoned, and the distinct invalid_reason values
    joined with " | "."""
    by = list(by)
    columns = by + ["n_runs", "n_valid", "n_invalid", "n_violation", "n_timed_out", "n_error", "n_abandoned",
                    "reasons"]
    if runs.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for values in _strata(runs, by):
        part = _stratum_rows(runs, by, values)
        reasons = part["invalid_reason"].fillna("").astype(str)
        row: dict[str, Any] = dict(zip(by, values))
        row.update(
            n_runs=len(part), n_valid=int(part["valid"].astype(bool).sum()),
            n_invalid=int((~part["valid"].astype(bool)).sum()),
            n_violation=int((part["invalid"] == True).sum()),  # noqa: E712 - object column with NaN
            n_timed_out=int((part["timed_out"] == True).sum()),  # noqa: E712
            n_error=int(part["error"].fillna("").astype(str).ne("").sum()),
            n_abandoned=int(reasons.str.startswith("abandoned").sum()),
            reasons=" | ".join(sorted({r for r in reasons if r})),
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


# --------------------------------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------------------------------

FLOAT_FORMAT = "%.6g"
MD_FLOAT_FORMAT = "%.4g"
PNG_DPI = 150
PNG_METADATA = {"Software": None}
PDF_METADATA = {"CreationDate": None, "ModDate": None, "Producer": None, "Creator": None}


def _is_missing(v: Any) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    if isinstance(v, (float, np.floating)):
        return math.isnan(v)
    return False


def _md_cell(v: Any, float_format: str) -> str:
    if _is_missing(v):
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return float_format % float(v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(df: pd.DataFrame, float_format: str = MD_FLOAT_FORMAT) -> str:
    """A GitHub-markdown table of ``df`` (index not included); numeric columns right-aligned, missing values blank."""
    cols = [str(c) for c in df.columns]
    numeric = [pd.api.types.is_numeric_dtype(df.iloc[:, i]) and not pd.api.types.is_bool_dtype(df.iloc[:, i])
               for i in range(df.shape[1])]
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in cols) + " |",
             "| " + " | ".join("---:" if n else "---" for n in numeric) + " |"]
    for row in df.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(_md_cell(v, float_format) for v in row) + " |")
    return "\n".join(lines) + "\n"


def _table_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df
    if any(n is not None for n in frame.index.names):
        frame = frame.reset_index()
    return frame.drop(columns=["path"], errors="ignore")


def write_table(df: pd.DataFrame, out_root: Path, name: str, float_format: str = FLOAT_FORMAT,
                md_float_format: str = MD_FLOAT_FORMAT) -> tuple[Path, Path]:
    """Write ``df`` to ``<out_root>/tables/<name>.csv`` (floats as %.6g) and ``<name>.md`` (GitHub markdown, %.4g).

    Rows are written in the frame's order (sort first). A named index (e.g. from groupby) is written as columns, an
    unnamed one is not. The run-directory column ``path`` is never written. Returns the two paths.
    """
    d = Path(out_root) / TABLES_DIR
    d.mkdir(parents=True, exist_ok=True)
    frame = _table_frame(df)
    csv_path, md_path = d / f"{name}.csv", d / f"{name}.md"
    frame.to_csv(csv_path, index=False, float_format=float_format, lineterminator="\n")
    md_path.write_text(to_markdown(frame, md_float_format), encoding="utf-8")
    return csv_path, md_path


def to_jsonable(obj: Any) -> Any:
    """``obj`` converted for JSON: numpy scalars and arrays to Python, NaN and infinities to null, DataFrames to
    lists of row objects, Series to objects. Filesystem paths and timestamps are refused (TypeError)."""
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return [to_jsonable(r) for r in _table_frame(obj).to_dict(orient="records")]
    if isinstance(obj, pd.Series):
        return to_jsonable(obj.to_dict())
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(obj) else None
    if obj is None or obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, PurePath):
        raise TypeError(f"refusing to write a filesystem path to JSON: {obj.name}")
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")


def write_json(obj: Any, out_root: Path, name: str) -> Path:
    """Write ``obj`` (see ``to_jsonable``) to ``<out_root>/tables/<name>.json``: sorted keys, indent 2, trailing
    newline, no NaN. Returns the path."""
    text = json.dumps(to_jsonable(obj), sort_keys=True, indent=2, allow_nan=False, ensure_ascii=False)
    d = Path(out_root) / TABLES_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def save_figure(fig: matplotlib.figure.Figure, out_root: Path, name: str, close: bool = True) -> tuple[Path, Path]:
    """Save ``fig`` to ``<out_root>/figures/out/<name>.png`` (150 dpi) and ``.pdf``, byte-reproducibly (no software,
    producer or date metadata). Closes the figure unless ``close`` is False. Returns the two paths."""
    d = Path(out_root) / FIGURES_DIR
    d.mkdir(parents=True, exist_ok=True)
    png, pdf = d / f"{name}.png", d / f"{name}.pdf"
    fig.savefig(png, dpi=PNG_DPI, metadata=PNG_METADATA)
    fig.savefig(pdf, metadata=PDF_METADATA)
    if close:
        plt.close(fig)
    return png, pdf


# Okabe-Ito hues in a fixed order (validated colorblind-safe on white; the first three are distinguishable as all
# pairs, so prefer them for scatter plots). Baselines are drawn in neutral greys with distinct markers instead.
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9"]
NEUTRAL = "#333333"
GRID = "#DDDDDD"
SEQUENTIAL_CMAP = "cividis"
# Figure sizes in inches: single column, single column tall, full width, full width tall.
FIGSIZE = {"single": (3.4, 2.4), "single_tall": (3.4, 3.4), "double": (7.0, 2.6), "double_tall": (7.0, 4.6)}

PLANNER_COLORS = {"entropy-greedy": PALETTE[0], "anneal-plan": PALETTE[1], "savings-only": PALETTE[2],
                  "anneal-online": PALETTE[3]}
BASELINE_STYLES = {
    "B0": {"color": "#A0A0A0", "linestyle": ":", "marker": "x"},
    "B1": {"color": "#6E6E6E", "linestyle": (0, (1, 1)), "marker": "v"},
    "B2": {"color": "#000000", "linestyle": "--", "marker": "s"},
    "B3": {"color": "#4D4D4D", "linestyle": "-.", "marker": "D"},
    "B4": {"color": "#262626", "linestyle": (0, (5, 1)), "marker": "P"},
    "B5": {"color": "#7A7A7A", "linestyle": (0, (3, 1, 1, 1, 1, 1)), "marker": "^"},
}
GENERATION_STYLES = {"random": {"linestyle": "-", "marker": "o"}, "heuristic": {"linestyle": "--", "marker": "s"}}

# Axis labels for common metrics, so every figure names them the same way.
METRIC_LABELS = {
    "steady_cost_per_hour": "Steady-state cost ($/hr)",
    "integrated_cost_post": "Cost after warmup ($)",
    "integrated_cost": "Total cost ($)",
    "cost_per_hour": "Cost ($/hr)",
    "evictions": "Pod evictions",
    "eviction_cost": "Eviction cost",
    "final_nodes": "Final nodes",
    "peak_nodes": "Peak nodes",
    "nodes": "Nodes",
    "fidelity": "Plan fidelity",
    "wall_pass_mean_ms": "Mean pass latency (ms)",
    "wall_pass_p95_ms": "p95 pass latency (ms)",
    "p1_load_entropy": "P1 load-share entropy",
    "p2_util_entropy": "P2 utilization entropy",
    "p3_quadratic": "P3 quadratic potential",
    "t_min": "Time (min)",
}


def apply_style() -> None:
    """Set the paper's matplotlib style: 9 pt DejaVu Sans, no top/right spines, the Okabe-Ito color cycle, a
    recessive grid, constrained layout, the single-column default figure size, TrueType fonts in PDFs."""
    matplotlib.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.titlesize": 9,
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "legend.frameon": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.prop_cycle": cycler(color=PALETTE),
        "axes.edgecolor": NEUTRAL,
        "axes.labelcolor": NEUTRAL,
        "axes.titlecolor": NEUTRAL,
        "text.color": NEUTRAL,
        "xtick.color": NEUTRAL,
        "ytick.color": NEUTRAL,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.5,
        "lines.linewidth": 1.5,
        "lines.markersize": 4,
        "image.cmap": SEQUENTIAL_CMAP,
        "figure.figsize": FIGSIZE["single"],
        "figure.dpi": PNG_DPI,
        "figure.facecolor": "white",
        "figure.constrained_layout.use": True,
        "savefig.dpi": PNG_DPI,
        "savefig.facecolor": "white",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.hashsalt": "paper",
    })


def style_for(label: str, index: int | None = None) -> dict[str, Any]:
    """Line/marker style for a configuration label, consistent across figures.

    Baselines (B0..B5) get neutral greys with distinct markers. Labels that are exactly a planner, optionally after
    a generation ("random/entropy-greedy", "heuristic/anneal-plan", "entropy-greedy"), are colored by planner
    (entropy-greedy, anneal-plan, savings-only, anneal-online). A "random/" prefix adds solid lines and circles, a
    "heuristic/" prefix dashed lines and squares. Every other label (sweep points such as ".../lambda=0.1", ablation
    variants) takes ``PALETTE[index]`` when ``index`` is given (pass the label's position in the registry's ``labels``
    list), else a stable hash of the label.
    """
    if label in BASELINE_STYLES:
        return dict(BASELINE_STYLES[label])
    parts = label.split("/")
    generation = parts[0] if parts[0] in GENERATION_STYLES else None
    rest = parts[1:] if generation else parts
    style: dict[str, Any] = {"linestyle": "-", "marker": "o"}
    if generation:
        style.update(GENERATION_STYLES[generation])
    if len(rest) == 1 and rest[0] in PLANNER_COLORS:
        style["color"] = PLANNER_COLORS[rest[0]]
    elif index is not None:
        style["color"] = PALETTE[index % len(PALETTE)]
    else:
        style["color"] = PALETTE[zlib.crc32(label.encode("utf-8")) % len(PALETTE)]
    return style


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


def cli(main: Callable[[Path, Path], None], argv: Sequence[str] | None = None) -> None:
    """Run an analysis module's ``main`` from the command line: ``--runs`` (default experiments/runs) and ``--out``
    (default paper). Use as ``if __name__ == "__main__": common.cli(main)``."""
    doc = (main.__doc__ or "").strip()
    parser = argparse.ArgumentParser(description=doc.splitlines()[0] if doc else None)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS_ROOT, help="raw runs root (experiments/runs)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_ROOT, help="output root (paper)")
    args = parser.parse_args(argv)
    apply_style()
    main(args.runs, args.out)
