"""Plain-assert tests for paper.analysis.tier_b, on hand-written run trees. Run from the repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_tier_b
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from paper.analysis import tier_b


def _series(cost_after: float, nodes_after: int, evictions: int, settle_at: float) -> str:
    """A 0..35-minute series: 10 nodes at $20/hr through the warmup, then cost_after and nodes_after from
    settle_at on, with evictions spread evenly after the drop."""
    lines = ["t_min,nodes,ready_nodes,cost_per_hour,pods_running,pods_pending,evictions_cum"]
    for t in range(36):
        after = t >= settle_at
        cost, nodes = (cost_after, nodes_after) if after else (20.0, 10)
        ev = 0 if t <= 5 else round(evictions * min(1.0, (t - 5) / 30))
        lines.append(f"{t:.2f},{nodes},{nodes},{cost:.6f},100,0,{ev}")
    return "\n".join(lines) + "\n"


def _tier_a(root: Path, label: str, seed: int, n: int, cost: float, ev: int, cpu: float) -> None:
    d = root / "tierb" / f"tierb-a__arm-{label.replace('/', '-')}_nodes-{n}_seed-{seed}__x"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({"label": label, "seed": seed, "scenario_nodes": n,
                                                "cpu_pass_total_s": cpu, "pdb_violations": 0}))
    (d / "timeseries.csv").write_text(_series(cost, 6, ev, 12))


def _tier_b(root: Path, label: str, seed: int, n: int, cost: float, ev: int, cpu: float, valid: bool = True,
            suffix: str = "", boot: str = "vm-1") -> None:
    d = root / "tierb-kwok" / f"tierb-kwok__arm-{label.replace('/', '-')}_nodes-{n}_seed-{seed}{suffix}"
    d.mkdir(parents=True)
    (d / "cell.json").write_text(json.dumps({"label": label, "seed": seed, "nodes": n}))
    (d / "manifest.json").write_text(json.dumps({"test_exit_status": 0, "boot_id": boot}))
    (d / "summary.json").write_text(json.dumps({
        "valid": valid, "pending_at_drop": 0 if valid else 3, "evictions_before_drop": 0, "karpenter_restarted": False,
        "method_ok": True, "metrics_sample_count": 60, "nodes_at_drop": 10, "cost_at_drop": 20.0,
        "karpenter_cpu_core_seconds": cpu, "decision_evaluation_seconds": {"pipeline": 1.5, "empty": 9.0},
        "karpenter_max_memory_mb": 200.0}))
    (d / "timeseries.csv").write_text(_series(cost, 6, ev, 15))


def test_window_metrics() -> None:
    m = tier_b.window_metrics(pd.read_csv(pd.io.common.StringIO(_series(14.0, 6, 30, 12))))
    assert m["cost_end"] == 14.0 and m["nodes_end"] == 6 and m["evictions"] == 30 and m["settle_min"] == 7.0
    assert math.isclose(m["cost_end_rel"], 14.0 / 20.0)
    # integral over [5, 35]: $20/hr until minute 11, a ramp to $14 over minute 11-12, then $14/hr to 35
    want = (20 * 6 + (20 + 14) / 2 * 1 + 14 * 23) / 60
    assert math.isclose(m["cost_window"], want), (m["cost_window"], want)
    short = pd.read_csv(pd.io.common.StringIO(_series(14.0, 6, 30, 12))).query("t_min <= 20")
    assert math.isnan(tier_b.window_metrics(short)["cost_end"])


def test_agreement_reversal_and_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i, label in enumerate(tier_b.CONFIGS):
            for seed in (101, 102):
                _tier_a(root, label, seed, 50, cost=10.0 + i, ev=10 * (i + 1), cpu=1.0 + i)
                # Tier B: same cost order, except B3 and B4 swapped by far more than 1%; evictions reversed in full
                b_cost = {"B3": 13.0, "B4": 11.0}.get(label, 10.0 + i)
                _tier_b(root, label, seed, 50, cost=b_cost, ev=10 * (7 - i), cpu=2.0 + i)
        # seed 103: one invalid configuration drops the block whole, even with a valid rerun missing
        for label in tier_b.CONFIGS:
            _tier_b(root, label, 103, 50, cost=99.0, ev=1, cpu=1.0, valid=(label != "B2"))
        # a set-aside attempt is never used
        _tier_b(root, "B2", 101, 50, cost=99.0, ev=1, cpu=1.0, suffix=".failed-1")
        out = root / "out"
        tier_b.main(root, out)
        doc = json.loads((out / "tables" / "tier_b.json").read_text())
        assert doc["blocks"] == [{"seed": 101, "n": 50}, {"seed": 102, "n": 50}], doc["blocks"]
        assert doc["runs"]["set_aside"] == 1
        agree = pd.read_csv(out / "tables" / "tierb_agreement.csv").set_index("metric")
        assert math.isclose(agree.at["evictions", "tau_b"], -1.0)
        assert agree.at["cost_end", "material_reversals"] == 1
        assert 0 < agree.at["cost_end", "tau_b"] < 1
        revs = pd.read_csv(out / "tables" / "tierb_reversals.csv")
        assert {"B3", "B4"} == set(revs.query("metric == 'cost_end'")[["config_1", "config_2"]].iloc[0])
        means = pd.read_csv(out / "tables" / "tierb_means.csv").set_index("label")
        assert means.at["B2", "cost_end_b"] == 10.0  # the 99.0 runs (seed 103, the set-aside attempt) are not used
        assert means.at["B2", "evaluation_s"] == 1.5  # pipeline+multi+single only, not "empty"
        pairs = pd.read_csv(out / "tables" / "tierb_pairs.csv")
        row = pairs.query("pair == 'B4 vs B2' and metric == 'cost_end'").iloc[0]
        # Tier A: B4 $12/hr vs B2 $10/hr; Tier B: B4 $11/hr vs B2 $10/hr
        assert math.isclose(row.tier_a_pct, 20.0) and math.isclose(row.tier_b_pct, 10.0) and row.same_sign


def test_paired_differences() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        noise = {101: 1.0, 102: -1.0, 103: 0.5}
        for i, label in enumerate(tier_b.CONFIGS):
            for seed in noise:
                _tier_a(root, label, seed, 50, cost=10.0 + i, ev=10, cpu=1.0)
                # Tier B: B2 sits above B3 by a seed-dependent amount (+1, -1, +0.5): no resolved direction
                b_cost = 11.0 + noise[seed] if label == "B2" else 10.0 + i
                _tier_b(root, label, seed, 50, cost=b_cost, ev=10, cpu=1.0)
        out = root / "out"
        tier_b.main(root, out)
        paired = pd.read_csv(out / "tables" / "tierb_paired.csv")
        row = paired.query("metric == 'cost_end' and config_1 == 'B2' and config_2 == 'B3'").iloc[0]
        assert row.a_seeds == 3 and row.b_seeds == 3
        assert math.isclose(row.a_mean, -1.0) and row.a_resolved  # B2 below B3 in every seed in Tier A
        assert math.isclose(row.b_mean, 0.5 / 3, abs_tol=1e-4) and not row.b_resolved and row.b_seeds_with_a_sign == 1
        revs = pd.read_csv(out / "tables" / "tierb_reversals_paired.csv")
        rev = revs.query("metric == 'cost_end' and config_1 == 'B2' and config_2 == 'B3'")
        assert len(rev) == 1 and rev.a_resolved.iloc[0] and not rev.b_resolved.iloc[0]
        doc = json.loads((out / "tables" / "tier_b.json").read_text())
        assert doc["exploratory"]["reversals_resolved_in_both_tiers"] == 0
        summ = pd.read_csv(out / "tables" / "tierb_paired_summary.csv").set_index(["n", "metric"])
        # Tier A resolves all 21 cost_end pairs (no seed noise). Tier B shows every pair with Tier A's sign except
        # B2 vs B3, which it reverses without resolving
        row = summ.loc[(50, "cost_end")]
        assert row.pairs == 21 and row.a_resolved == 21 and row.b_same_sign == 20 and row.b_resolved_opposite == 0, row


def test_compute_on_one_vm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i, label in enumerate(tier_b.CONFIGS):
            for seed in (101, 102):
                _tier_a(root, label, seed, 50, cost=10.0 + i, ev=10, cpu=1.0 + i)
                # seed 102 moves to a second VM halfway, and its compute order there is reversed
                boot = "vm-2" if seed == 102 and i >= 3 else "vm-1"
                cpu = 2.0 + i if seed == 101 else 100.0 - 10 * i
                _tier_b(root, label, seed, 50, cost=10.0 + i, ev=10, cpu=cpu, boot=boot)
        # a set-aside attempt on yet another VM does not make a block mixed
        _tier_b(root, "B2", 101, 50, cost=99.0, ev=1, cpu=1.0, suffix=".interrupted-1", boot="vm-0")
        out = root / "out"
        tier_b.main(root, out)
        doc = json.loads((out / "tables" / "tier_b.json").read_text())
        assert doc["mixed_vm_blocks"] == [{"seed": 102, "n": 50}], doc["mixed_vm_blocks"]
        one = pd.read_csv(out / "tables" / "tierb_compute_one_vm.csv").set_index("n")
        assert one.at[50, "blocks"] == 1 and math.isclose(one.at[50, "tau_b"], 1.0), one
        runs = pd.read_csv(out / "tables" / "tierb_runs.csv")
        assert set(runs.boot_id) == {"vm-0", "vm-1", "vm-2"}


def test_nothing_without_tier_b() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tier_b.main(Path(tmp), Path(tmp) / "out")
        assert not (Path(tmp) / "out" / "tables" / "tier_b.json").exists()


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
