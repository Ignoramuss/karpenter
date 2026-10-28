"""One table of every preregistered verdict and its key effects, collected from the other modules' JSON headlines.

``experiments/RESULTS.md`` quotes its numbers from here, so the write-up and the raw runs cannot drift apart: run
last by ``paper/make_figures.py``, after run_checks, cost_experiments, frontier_fidelity, timeseries_figures and
latency_optimality have written ``<out>/tables/*.json``. It reads only those files (``runs_root`` is unused) and
writes, under ``out_root``:

- ``tables/summary.md``: per hypothesis or check (H1, H1-P0 contrast, H1-realism, H2, H3, H4, H5, H6, H7, H7-B2,
  H7-S, ablation, B4 parity, Exp 8, run checks): the verdict, the preregistered rule's key counts, each test's effect
  as a percent difference with its interval and outcome, and the tables and figures that show it.
- ``tables/summary.json``: the same, as data.

A hypothesis whose experiment has no runs is listed as "no runs". Verdict words are the owning module's.

Run on its own with ``/opt/research-venv/bin/python -m paper.analysis.summary [--out DIR]``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from paper.analysis import common

SOURCES = ("run_checks", "cost_experiments", "frontier_fidelity", "timeseries_figures", "latency_optimality")
COLUMNS = ["item", "verdict", "rule_counts", "effects", "where"]


def _load(out_root: Path) -> dict[str, Any]:
    out = {}
    for name in SOURCES:
        p = Path(out_root) / common.TABLES_DIR / f"{name}.json"
        out[name] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return out


def _num(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return math.nan
    return x


def pct(v: Any, lo: Any = None, hi: Any = None) -> str:
    """A percent difference with its interval, e.g. "-37.2% [-41.2, -33.2]"; "n/a" when undefined."""
    x = _num(v)
    if not math.isfinite(x):
        return "n/a"
    text = f"{x:+.1f}%"
    a, b = _num(lo), _num(hi)
    if math.isfinite(a) and math.isfinite(b):
        text += f" [{a:+.1f}, {b:+.1f}]"
    return text


def _records(v: Any) -> list[dict[str, Any]]:
    return [r for r in v if isinstance(r, Mapping)] if isinstance(v, list) else []


def _stratum(t: Mapping[str, Any], keys: Iterable[str]) -> str:
    parts = []
    for k in keys:
        v = t.get(k)
        if v is None or (isinstance(v, float) and math.isnan(v)) or v == "":
            continue
        parts.append(f"{int(v)}" if isinstance(v, float) and v.is_integer() else str(v))
    return " ".join(parts)


def _effects(tests: Iterable[Mapping[str, Any]], keys: Iterable[str], lo: str = "pct_diff_lo",
             hi: str = "pct_diff_hi", value: str = "pct_diff", outcome: str = "outcome") -> str:
    keys = list(keys)
    return "; ".join(f"{_stratum(t, keys)}: {pct(t.get(value), t.get(lo), t.get(hi))} ({t.get(outcome, '')})".strip()
                     for t in tests)


def _row(item: str, verdict: Any, counts: str = "", effects: str = "", where: str = "") -> dict[str, Any]:
    return {"item": item, "verdict": verdict if verdict not in (None, "") else "no runs", "rule_counts": counts,
            "effects": effects, "where": where}


def h1_rows(cost: Mapping[str, Any]) -> list[dict[str, Any]]:
    e = cost.get("exp1") or {}
    h1 = e.get("h1") or {}
    rows = [_row("H1 (random vs heuristic generation, entropy-guided planners; TOST at 3%)", h1.get("verdict"),
                 f"{h1.get('n_equivalent_holm', 'n/a')} of {h1.get('n_tests_expected', 12)} equivalent after Holm "
                 f"(need {h1.get('min_equivalent', 10)}); {h1.get('n_noninferior_holm', 'n/a')} non-inferior; "
                 f"{h1.get('n_better_beyond_margin', 'n/a')} lower beyond the margin; {h1.get('n_vetoed', 'n/a')} "
                 "vetoed" if h1 else "",
                 _effects(_records(h1.get("tests")), ("scenario", "nodes", "planner"), "ci90_lo_pct", "ci90_hi_pct"),
                 "tables/exp1_h1, exp1_secondary, exp1_phases")]
    p0 = e.get("p0_contrast") or {}
    if p0:
        rows.append(_row("H1-P0 contrast (savings-only; reported, not in H1)", "reported",
                         f"{p0.get('n_equivalent_holm', 'n/a')} of {p0.get('n_tests', 6)} equivalent after Holm",
                         _effects(_records(p0.get("tests")), ("scenario", "nodes"), "ci90_lo_pct", "ci90_hi_pct"),
                         "tables/exp1_h1"))
    r = (cost.get("realism") or {}).get("tost") or {}
    rows.append(_row("H1-realism (W5, W6 at 200 nodes; reported, not in H1)",
                     ("incomplete" if not r.get("complete", True) else "reported") if r else None,
                     f"{r.get('n_equivalent_holm', 'n/a')} of {r.get('n_tests', 4)} equivalent after Holm" if r else "",
                     _effects(_records(r.get("tests")), ("scenario", "nodes", "planner"), "ci90_lo_pct",
                              "ci90_hi_pct"), "tables/realism_h1, realism_secondary"))
    return rows


def h2_rows(ts: Mapping[str, Any]) -> list[dict[str, Any]]:
    h2 = ((ts.get("exp2") or {}).get("h2") or {}).get("verdicts") or {}
    rows = []
    for p in sorted(h2):
        v = h2[p]
        rows.append(_row(f"H2, potential {p} (sign test over seeds of rho(-dPhi, removals))", v.get("verdict"),
                         f"supported in {', '.join(v.get('supported_in') or []) or 'none'} of "
                         f"{', '.join(v.get('scenarios') or []) or 'none'}", "",
                         "tables/exp2_correlation, exp2_seeds, exp2_leadlag; figures/out/fig6_leadlag"))
    return rows or [_row("H2", None, where="tables/exp2_correlation")]


def wilcoxon_row(item: str, block: Mapping[str, Any], tests: Any, keys: Iterable[str], where: str,
                 counts: str = "") -> dict[str, Any]:
    return _row(item, block.get("verdict") if block else None, counts, _effects(_records(tests), keys), where)


def frontier_rows(ff: Mapping[str, Any]) -> list[dict[str, Any]]:
    h5 = ff.get("exp5") or {}
    levers = h5.get("levers") or {}
    counts = (f"off-plan share clause {'holds' if h5.get('off_plan_holds') else 'fails'}; levers: "
              + ", ".join(f"{k} {v.get('state', 'n/a')}" for k, v in levers.items())) if h5 else ""
    off = "; ".join(f"{o.get('scale_down')}: off-plan share {_num(o.get('off_plan_share_mean')):.3f} "
                    f"(lower bound {_num(o.get('off_plan_share_mean_lo')):.3f})" for o in _records(h5.get("off_plan")))
    h6 = ff.get("exp6") or {}
    cells = "; ".join(f"{c.get('baseline')} {c.get('scenario')}: {c.get('verdict')}" for c in _records(h6.get("cells")))
    return [_row("H5 (plan fidelity: off-plan share and levers)", h5.get("verdict") if h5 else None, counts, off,
                 "tables/exp5_fidelity, exp5_tests; figures/out/fig5_fidelity"),
            _row("H6 (savings vs disruption frontier, matched lambda vs B2, B3, B5)", h6.get("verdict") if h6 else None,
                 ", ".join(f"{k}: {v}" for k, v in (h6.get("verdict_counts") or {}).items()), cells,
                 "tables/exp6_frontier, exp6_dominance; figures/out/fig3_frontier")]


def latency_rows(lat: Mapping[str, Any]) -> list[dict[str, Any]]:
    e7 = lat.get("exp7") or {}
    h7 = e7.get("h7") or {}
    tests = _records(e7.get("tests"))
    epochs = "; ".join(f"{c.get('label')} on VM {c.get('machine_epochs')}" for c in _records(e7.get("cells"))
                       if _num(c.get("nodes")) == 1000 and c.get("machine_epochs"))
    rows = [_row("H7 (decision cost at 1,000 nodes: random vs heuristic, entropy-greedy)", h7.get("verdict"),
                 f"{h7.get('tests_lower', 'n/a')} of 2 tests lower; timeout clause {h7.get('timeout_clause', 'n/a')}"
                 + (f"; {epochs}" if epochs else "") if h7 else "",
                 _effects([t for t in tests if t.get("family") == "H7"], ("metric",)),
                 "tables/exp7_latency, exp7_tests; figures/out/fig4_latency")]
    b2 = e7.get("h7_b2") or {}
    rows.append(_row("H7-B2 (secondary: random vs B2 at 1,000 nodes, 1-hour runs)",
                     ("reported" if b2.get("status") == "present" else f"absent ({b2.get('reason', '')})") if b2
                     else None, f"{b2.get('tests_lower', 'n/a')} of 2 lower" if b2.get("status") == "present" else "",
                     _effects([t for t in tests if t.get("family") == "H7-B2"], ("metric",)), "tables/exp7_tests"))
    s = lat.get("h7s") or {}
    agree = s.get("tests_vs_h7") or {}
    rows.append(_row("H7-S (supplementary timing pass; reported beside H7, never replacing it)", s.get("verdict"),
                     (f"determinism {((s.get('determinism') or {}).get('status'))}; split seeds "
                      f"{(s.get('pairing') or {}).get('split_seeds')}; agrees with H7 test by test: "
                      + ", ".join(f"{m} {v.get('agree')}" for m, v in agree.items())) if s else "",
                     _effects(_records(s.get("tests")), ("metric",)), "tables/h7s_tests, h7s_runs"))
    e8 = lat.get("exp8") or {}
    gaps = "; ".join(f"{_stratum(c, ('nodes', 'scenario', 'label'))}: gap {_num(c.get('gap_final_mean')):.3f}"
                     for c in _records(e8.get("cells")))
    rows.append(_row("Exp 8 (optimality gap to the B6 bound; descriptive)", "reported" if e8 else None, "", gaps,
                     "tables/exp8_gap, exp8_gap_snapshots; figures/out/fig7_optimality_gap"))
    return rows


def cost_rows(cost: Mapping[str, Any]) -> list[dict[str, Any]]:
    e3 = cost.get("exp3") or {}
    e4 = cost.get("exp4") or {}
    h4 = e4.get("h4") or {}
    clauses = h4.get("clauses")
    abl = cost.get("abl") or {}
    b4 = (cost.get("b4") or {}).get("b4") or {}
    rows = [wilcoxon_row("H3 (merge+shift vs merge-only at equal eviction budgets)", e3.get("h3") or {},
                         (e3.get("h3") or {}).get("tests"), ("scenario", "nodes", "budget"),
                         "tables/exp3_h3, exp3_secondary"),
            _row("H4 (anneal at plan time vs greedy; anneal online)", h4.get("verdict") if h4 else None,
                 (f"plan clause {(h4.get('plan_clause') or {}).get('n_holds', 'n/a')} of "
                  f"{(h4.get('plan_clause') or {}).get('n', 'n/a')} hold; online clause "
                  f"{(h4.get('online_clause') or {}).get('n_holds', 'n/a')} of "
                  f"{(h4.get('online_clause') or {}).get('n', 'n/a')} hold") if h4 else "",
                 _effects(_records(clauses), ("clause", "scenario", "configuration", "bound_metric"),
                          outcome="state"), "tables/exp4_h4, exp4_clauses, exp4_sweep"),
            _row("Ablation (family of 8)",
                 ("incomplete" if not abl.get("complete", True) else "reported") if abl else None,
                 f"{sum(1 for t in _records(abl.get('tests')) if t.get('significant'))} of "
                 f"{len(_records(abl.get('tests')))} significant after Holm" if abl else "",
                 _effects(_records(abl.get("tests")), ("configuration", "metric", "scenario"), "diff_pct_lo",
                          "diff_pct_hi", "diff_pct"), "tables/fig8_ablation; figures/out/fig8_ablation"),
            _row("B4 parity in the wild (fine upstream adapters vs B2, B3)", b4.get("status") if b4 else None,
                 f"{b4.get('n_match', 'n/a')} of {b4.get('n_seeds', 'n/a')} seeds match exactly; "
                 f"{b4.get('n_equivalent_holm', 'n/a')} of 6 TOSTs equivalent" if b4 else "", "", "tables/b4_parity")]
    return rows


def checks_row(rc: Mapping[str, Any]) -> dict[str, Any]:
    if not rc:
        return _row("Run checks", None)
    v = rc.get("validity_totals") or {}
    d = rc.get("duplicates") or {}
    b = rc.get("binary_check") or {}
    return _row("Run checks (validity, cuts, duplicates, binary)", "reported",
                f"{rc.get('n_runs')} runs, {v.get('n_valid', 'n/a')} valid, {v.get('n_invalid', 'n/a')} invalid; "
                f"{rc.get('n_cut_configurations')} cut configurations; duplicate sets {d.get('n_differ')} of "
                f"{d.get('n_sets')} differ; wrong binary {b.get('n_wrong_binary')}", "",
                "tables/validity, cut_configurations, duplicate_consistency, run_checks.json")


def summary_rows(src: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    cost, ff = src.get("cost_experiments") or {}, src.get("frontier_fidelity") or {}
    ts, lat = src.get("timeseries_figures") or {}, src.get("latency_optimality") or {}
    rows = h1_rows(cost) + h2_rows(ts)
    rows += cost_rows(cost)[:2] + frontier_rows(ff) + latency_rows(lat) + cost_rows(cost)[2:]
    rows.append(checks_row(src.get("run_checks") or {}))
    return rows


def main(runs_root: Path, out_root: Path) -> None:  # noqa: ARG001 - the shared module signature
    """Write tables/summary.{md,json} from the other modules' headlines under ``out_root``."""
    src = _load(Path(out_root))
    if not any(src.values()):
        common.skipped("summary", "no module headlines")
        return
    import pandas as pd

    rows = summary_rows(src)
    common.write_table(pd.DataFrame(rows, columns=COLUMNS), Path(out_root), "summary")
    common.write_json({"rows": rows, "sources": [s for s in SOURCES if src.get(s)]}, Path(out_root), "summary")


if __name__ == "__main__":
    common.cli(main)
