"""Analysis of the consolidation experiments: every paper figure and table, regenerated from experiments/runs.

`paper.analysis.common` is the shared library (run loading, preregistered statistics, reproducible output, the
experiment registry in experiments.yaml). Each other module covers some experiments and exposes
``main(runs_root: Path, out_root: Path) -> None``; ``paper/make_figures.py`` runs them all.
"""
