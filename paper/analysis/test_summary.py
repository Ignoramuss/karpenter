"""Plain-assert tests for paper.analysis.summary, on hand-written module headlines. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_summary
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from paper.analysis import summary

HEADLINES = {
    "cost_experiments": {
        "exp1": {"h1": {"verdict": "not supported", "n_equivalent_holm": 3, "n_tests_expected": 12,
                        "min_equivalent": 10, "n_noninferior_holm": 9, "n_better_beyond_margin": 1, "n_vetoed": 0,
                        "tests": [{"scenario": "W1", "nodes": 200, "planner": "entropy-greedy", "pct_diff": -1.72,
                                   "ci90_lo_pct": -3.23, "ci90_hi_pct": -0.2, "outcome": "not shown equivalent"}]},
                 "p0_contrast": {"n_tests": 6, "n_equivalent_holm": 0, "tests": []}},
        "exp4": {"h4": {"verdict": "partial", "plan_clause": {"n": 2, "n_holds": 1},
                        "online_clause": {"n": 6, "n_holds": 6},
                        "clauses": [{"clause": "plan", "scenario": "W2", "configuration": "anneal-plan/t0=1",
                                     "bound_metric": "evictions", "pct_diff": 4.0, "pct_diff_lo": 1.0,
                                     "pct_diff_hi": 8.5, "state": "holds"}]}},
    },
    "latency_optimality": {
        "exp7": {"h7": {"verdict": "not supported", "tests_lower": 1, "timeout_clause": "holds"},
                 "tests": [{"family": "H7", "metric": "cpu_pass_mean_ms", "pct_diff": -37.21, "pct_diff_lo": -41.25,
                            "pct_diff_hi": -33.16, "outcome": "lower"},
                           {"family": "H7", "metric": "cpu_pass_p95_ms", "pct_diff": 19.26, "pct_diff_lo": 14.29,
                            "pct_diff_hi": 23.45, "outcome": "higher"}],
                 "h7_b2": {"status": "absent", "reason": "no exp7-b2-n1000 runs"}},
    },
    "run_checks": {"n_runs": 424, "validity_totals": {"n_valid": 424, "n_invalid": 0}, "n_cut_configurations": 2,
                   "duplicates": {"n_sets": 5, "n_differ": 0}, "binary_check": {"n_wrong_binary": 0}},
}


def _write(out: Path) -> None:
    (out / "tables").mkdir(parents=True)
    for name, doc in HEADLINES.items():
        (out / "tables" / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_rows_and_formatting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        _write(out)
        summary.main(out / "runs", out)
        table = pd.read_csv(out / "tables" / "summary.csv")
        by = dict(zip(table["item"].str.split(" ").str[0], table.to_dict("records")))
        assert by["H1"]["verdict"] == "not supported"
        assert by["H1"]["rule_counts"].startswith("3 of 12 equivalent after Holm (need 10); 9 non-inferior")
        assert by["H1"]["effects"] == "W1 200 entropy-greedy: -1.7% [-3.2, -0.2] (not shown equivalent)"
        assert by["H7"]["effects"] == ("cpu_pass_mean_ms: -37.2% [-41.2, -33.2] (lower); "
                                       "cpu_pass_p95_ms: +19.3% [+14.3, +23.4] (higher)")
        assert by["H7"]["rule_counts"] == "1 of 2 tests lower; timeout clause holds"
        assert by["H7-B2"]["verdict"] == "absent (no exp7-b2-n1000 runs)"
        assert by["H4"]["verdict"] == "partial" and "plan clause 1 of 2 hold; online clause 6 of 6 hold" == \
            by["H4"]["rule_counts"]
        assert by["H4"]["effects"] == "plan W2 anneal-plan/t0=1 evictions: +4.0% [+1.0, +8.5] (holds)"
        assert by["H3"]["verdict"] == "no runs" and by["H2"]["verdict"] == "no runs"
        assert by["Run"]["rule_counts"].startswith("424 runs, 424 valid, 0 invalid; 2 cut configurations")
        doc = json.loads((out / "tables" / "summary.json").read_text(encoding="utf-8"))
        assert doc["sources"] == ["run_checks", "cost_experiments", "latency_optimality"]
        assert [r["item"] for r in doc["rows"]] == table["item"].tolist()


def test_nothing_without_headlines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        summary.main(Path(tmp) / "runs", Path(tmp))
        assert not (Path(tmp) / "tables" / "summary.md").exists()


def test_pct() -> None:
    assert summary.pct(None) == "n/a" and summary.pct(float("nan")) == "n/a"
    assert summary.pct(2.04) == "+2.0%" and summary.pct(-0.05, -1, 1) == "-0.1% [-1.0, +1.0]"


def main(argv: list[str] | None = None) -> int:
    tests: list[tuple[str, Callable[[], None]]] = [
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 - report every failing test
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
