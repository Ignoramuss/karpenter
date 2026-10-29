"""Rebuild every paper figure and table from the raw runs: the single entry point behind ``make figures``.

Run from the repository root::

    /opt/research-venv/bin/python paper/make_figures.py [--runs experiments/runs] [--out paper] [--only MODULE]

Each module in MODULES (``paper.analysis.<module>``) exposes ``main(runs_root, out_root)`` and writes tables to
``<out>/tables`` and figures to ``<out>/figures/out``. Modules run in order, each with the paper's matplotlib style
freshly applied, so no module's output depends on which modules ran before it. A module that does not exist yet is
skipped with a note; one that raises is reported and makes the run exit 1 after the others have run.

A full run (no ``--only``) first deletes the previous outputs (``<out>/tables/*.csv|md|json`` and
``<out>/figures/out/*.png|pdf``; the bound cache in ``tables/cache`` stays), so every file left is one this run wrote:
an experiment that is skipped now (no runs, or a different ANALYSIS_GROUP_MAP) cannot leave stale numbers behind.
``--only`` runs leave other modules' files alone.

Set ANALYSIS_GROUP_MAP to run the session analyses on other groups, and ANALYSIS_MIN_SEEDS to lower the completeness
threshold (MIN_SEEDS = 10 finished runs per configuration), e.g. on the pilot's 5 seeds::

    ANALYSIS_GROUP_MAP='{"exp1-n200": "pilot-exp1", "exp1-baselines-n200": "pilot-baselines"}' ANALYSIS_MIN_SEEDS=5 \\
        /opt/research-venv/bin/python paper/make_figures.py --out /tmp/pilot-paper
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "0")

# Analysis modules under paper/analysis, run in this order.
MODULES = (
    "run_checks",  # validity by reason, cut configurations, cross-group determinism, binary check
    "cost_experiments",  # Exp 1, realism, Exp 3, 4, ablations (figure 8), B4 parity
    "frontier_fidelity",  # Exp 5, 6 (figures 3 and 5)
    "timeseries_figures",  # Exp 2 (figures 1, 2 and 6)
    "latency_optimality",  # Exp 7, 8, the B6 lower bound (figures 4 and 7)
    "tier_b",  # Tier B: rank agreement between KWOK and the simulator on WB (experiments/TIERB.md)
    "summary",  # every verdict and its key effects, from the headlines above (experiments/RESULTS.md quotes it)
)
PACKAGE = "paper.analysis"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the paper's figures and tables from the raw runs.")
    parser.add_argument("--runs", type=Path, default=REPO_ROOT / "experiments" / "runs",
                        help="raw runs root (default: experiments/runs)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "paper",
                        help="output root; tables go to <out>/tables, figures to <out>/figures/out (default: paper)")
    parser.add_argument("--only", action="append", default=[], metavar="MODULE",
                        help="run only this module (repeatable or comma-separated); one of: " + ", ".join(MODULES))
    args = parser.parse_args(argv)
    only = [m.strip() for item in args.only for m in item.split(",") if m.strip()]
    unknown = [m for m in only if m not in MODULES]
    if unknown:
        parser.error(f"unknown module(s) {', '.join(unknown)}; choose from {', '.join(MODULES)}")
    args.only = only
    return args


OUTPUT_GLOBS = ((("tables",), ("*.csv", "*.md", "*.json")), (("figures", "out"), ("*.png", "*.pdf")))


def clear_outputs(out_root: Path) -> int:
    """Delete the generated tables and figures directly under ``<out_root>/tables`` and ``<out_root>/figures/out``
    (not the bound cache in ``tables/cache``, nor anything else). Returns how many files were removed."""
    removed = 0
    for parts, patterns in OUTPUT_GLOBS:
        d = Path(out_root).joinpath(*parts)
        if not d.is_dir():
            continue
        for pattern in patterns:
            for f in sorted(d.glob(pattern)):
                if f.is_file():
                    f.unlink()
                    removed += 1
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected analysis modules; return the process exit code (1 if any module failed)."""
    args = parse_args(argv)
    import matplotlib
    import matplotlib.pyplot as plt

    from paper.analysis import common

    selected = [m for m in MODULES if not args.only or m in args.only]
    if not args.only:
        removed = clear_outputs(args.out)
        if removed:
            print(f"removed {removed} previous output file(s) under {common.TABLES_DIR} and {common.FIGURES_DIR}")
    ran, missing, failed = [], [], []
    for name in selected:
        qualified = f"{PACKAGE}.{name}"
        if importlib.util.find_spec(qualified) is None:
            print(f"skipped module {name}: paper/analysis/{name}.py does not exist yet")
            missing.append(name)
            continue
        print(f"== {name}")
        plt.close("all")
        matplotlib.rcdefaults()
        common.apply_style()
        try:
            module = importlib.import_module(qualified)
            entry = getattr(module, "main", None)
            if not callable(entry):
                raise TypeError(f"{qualified} has no main(runs_root, out_root)")
            entry(args.runs, args.out)
        except Exception:  # noqa: BLE001 - report every failing module, then fail the run
            traceback.print_exc()
            failed.append(name)
        else:
            ran.append(name)
        finally:
            plt.close("all")
    print(f"make_figures: {len(ran)} ran, {len(missing)} not present, {len(failed)} failed"
          + (f" ({', '.join(failed)})" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
