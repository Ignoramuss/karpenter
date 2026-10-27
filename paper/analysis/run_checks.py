"""Run-level checks shared by every experiment: validity by reason, set-aside attempts, cut configurations, the
cross-group determinism check and the binary check (experiments/PREREGISTRATION.md: "What runs", "Matrix",
"Exclusion, completeness and undefined values").

Run through ``paper/make_figures.py`` (``--only run_checks``) or on its own::

    /opt/research-venv/bin/python -m paper.analysis.run_checks [--runs experiments/runs] [--out paper]

The runs are those of the registry's groups (every experiment in experiments.yaml, remapped by ANALYSIS_GROUP_MAP),
abandoned cells included. Outputs under ``out_root`` (nothing is written without such runs):

- ``tables/validity.{csv,md}``: per (experiment, group), the runs and each invalidity reason counted on its own
  (``common.validity_report``: violation, error, timed out, abandoned, wrong binary), the set-aside attempts
  (n_retried, n_set_aside, n_succeeded_on_retry), cells still waiting for a retry, and runs in cut configurations.
- ``tables/cut_configurations.{csv,md}``: every configuration (group, label, scenario, nodes, placement, scale_down)
  with fewer than MIN_SEEDS finished runs (``common.min_seeds()``; ANALYSIS_MIN_SEEDS overrides), with its count and
  the experiments that would have used it. Cut configurations are excluded from every test and verdict.
- ``tables/duplicate_consistency.{csv,md}``: identical configurations (label and group ignored) on the same seed must
  produce identical summaries (steady_cost_per_hour, evictions, nodes_removed, final_nodes): per duplicated
  configuration, the number of such sets and how many differ, then the totals (``common.duplicate_consistency``).
- ``tables/run_checks.json``: the totals, the differing sets, and the binary check (session runs, whether
  experiments/session-binary.txt was found, runs from another binary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from paper.analysis import common

CUT_COLUMNS = [*common.CONFIG_KEYS, "n_finished", "n_valid", "n_invalid", "min_seeds", "experiments"]


def registry_runs(runs: pd.DataFrame) -> pd.DataFrame:
    """One row per (registry experiment, run) for every experiment's selection (``common.select_runs`` with cut
    configurations kept), with the experiment key in ``experiment``, sorted by experiment (registry order), group
    and run_id."""
    frames = []
    for i, key in enumerate(common.EXPERIMENTS):
        sel = common.select_runs(runs, key, include_cut=True)
        if len(sel):
            frames.append(sel.assign(experiment=key, _order=i))
    if not frames:
        return runs.iloc[0:0].copy()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["_order", "group", "run_id"], kind="stable").drop(columns="_order").reset_index(drop=True)


def validity_table(per_exp: pd.DataFrame, pending: pd.DataFrame) -> pd.DataFrame:
    """``common.validity_report`` per (experiment, group) in registry order, plus ``n_pending_retry``: cells of the
    group with a set-aside attempt that neither finished nor were abandoned yet."""
    order = {k: i for i, k in enumerate(common.EXPERIMENTS)}
    report = common.validity_report(per_exp, by=("experiment", "group"))
    report = report.assign(_o=report["experiment"].map(order)).sort_values(["_o", "group"], kind="stable")
    waiting = pending.groupby("group").size() if len(pending) else pd.Series(dtype=int)
    report.insert(report.columns.get_loc("reasons"), "n_pending_retry",
                  [int(waiting.get(g, 0)) for g in report["group"]])
    return report.drop(columns="_o").reset_index(drop=True)


def cut_table(rows: pd.DataFrame, per_exp: pd.DataFrame) -> pd.DataFrame:
    """The cut configurations (``common.completeness``) of the registry's runs, with the experiments using each."""
    comp = common.completeness(rows)
    cut = comp.loc[comp["cut"].astype(bool)].drop(columns="cut")
    users = per_exp.groupby("group")["experiment"].agg(lambda s: ", ".join(dict.fromkeys(s))) if len(per_exp) \
        else pd.Series(dtype=str)
    cut = cut.assign(experiments=[users.get(g, "") for g in cut["group"]])
    return cut.reindex(columns=CUT_COLUMNS).reset_index(drop=True)


def binary_check(rows: pd.DataFrame) -> dict[str, Any]:
    """The binary check's coverage: session runs (``common.is_session_run``), whether each tree's
    experiments/session-binary.txt exists and the SHA it records, and the runs flagged as from another binary."""
    session = rows.loc[[common.is_session_run(Path(p)) for p in rows["path"]]] if len(rows) else rows
    files: dict[str, Any] = {}
    for p in sorted({common.session_binary_path(Path(p)) for p in session["path"]}, key=lambda x: x.as_posix()):
        sha = common.read_session_binary(p)
        files[f"{p.parent.name}/{p.name}"] = {"found": sha is not None, "sha": sha}
    finished = session.loc[~session["invalid_abandoned"].astype(bool)] if len(session) else session
    return {"n_session_runs": int(len(finished)), "files": files,
            "checked": bool(files) and all(f["found"] for f in files.values()),
            "n_wrong_binary": int(finished["invalid_wrong_binary"].astype(bool).sum()) if len(finished) else 0}


def main(runs_root: Path, out_root: Path) -> None:
    """Write the validity, cut-configuration and duplicate-consistency tables and run_checks.json (see the module
    docstring); nothing without runs in the registry's groups."""
    runs = common.load_runs(Path(runs_root), include_abandoned=True)
    per_exp = registry_runs(runs)
    if per_exp.empty:
        common.skipped("run_checks")
        return
    rows = per_exp.drop_duplicates(["matrix", "run_id"]).drop(columns="experiment").reset_index(drop=True)
    pending = common.pending_retries(Path(runs_root), runs)
    pending = pending.loc[pending["group"].isin(set(rows["group"]))]
    validity = validity_table(per_exp, pending)
    common.write_table(validity, out_root, "validity")
    cut = cut_table(rows, per_exp)
    common.write_table(cut, out_root, "cut_configurations")
    dup = common.duplicate_consistency(rows)
    sets = common.duplicate_sets(rows)
    common.write_table(dup, out_root, "duplicate_consistency")
    total = dup.iloc[-1]
    differing = sets.loc[~sets["consistent"].astype(bool)] if len(sets) else sets
    headline = {
        "min_seeds": common.min_seeds(), "n_runs": int(len(rows)),
        "n_configurations": int(len(common.completeness(rows))), "n_cut_configurations": int(len(cut)),
        "n_pending_retry": int(len(pending)),
        "validity_totals": {c: int(validity[c].sum()) for c in [*common.VALIDITY_COLUMNS[:-1], "n_pending_retry"]},
        "duplicates": {"n_sets": int(total["n_sets"]), "n_differ": int(total["n_differ"]),
                       "all_match": int(total["n_differ"]) == 0, "differing_sets": differing},
        "binary_check": binary_check(rows),
    }
    common.write_json(headline, out_root, "run_checks")
    common.logger.info("run_checks: %d runs, %d configurations cut, %d of %d duplicate sets differ", len(rows),
                       len(cut), headline["duplicates"]["n_differ"], headline["duplicates"]["n_sets"])


if __name__ == "__main__":
    common.cli(main)
