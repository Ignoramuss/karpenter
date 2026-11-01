"""Tests for paper.analysis.exp9 on a synthetic runs tree."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

from paper.analysis import exp9

REF_H, REF_R = exp9.REFERENCES
TREAT = "random/idle-50"


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _run(root: Path, label: str, seed: int, cost: float, cpu_total_s: float, passes: int,
         searches: list[dict[str, object]], valid: bool = True) -> Path:
    slug = label.replace("/", "-")
    d = root / "exp9" / f"exp9-n200__arm-{slug}_scenario-w1_seed-{seed}__{abs(hash((label, seed))) % 10**8:08x}"
    _write(d / "summary.json", {
        "run_id": d.name, "arm": "pipeline", "scenario": "W1", "scenario_nodes": 200, "seed": seed,
        "steady_cost_per_hour": cost, "integrated_cost_post": cost * 2, "final_nodes": 10, "evictions": 100,
        "eviction_cost": 50, "cpu_pass_mean_ms": 1000 * cpu_total_s / passes, "cpu_pass_p95_ms": 30,
        "cpu_pass_total_s": cpu_total_s, "full_simulations": 20, "disruption_passes": passes,
        "fidelity_hits": 1, "fidelity_misses": 0, "fidelity": 1, "invalid": not valid, "timed_out": False,
        "invalid_reasons": [] if valid else ["pdb violations"], "pdb_violations": 0 if valid else 1,
    })
    _write(d / "manifest.json", {"run_id": d.name, "git_sha": "abc", "git_dirty": False, "config": {
        "duration": "3h0m0s", "warmup": "10m0s", "seed": seed, "scenario": {"name": "W1", "nodes": 200},
        "arm": {"name": "pipeline", "label": label, "pipeline": {
            "candidates": {"name": "uniform-random"}, "moves": [{"name": "random-merge"}], "movesMode": "pooled",
            "planner": {"name": "entropy-greedy", "params": {"lambda": 0.1}}, "executor": {"name": "shift-evict"},
            "potential": {"name": "util-entropy"}, "search": {"maxFullSimulations": 16, "maxAttempts": 500}}}}})
    with gzip.open(d / "decisions.jsonl.gz", "wt", encoding="utf-8") as f:
        for i, s in enumerate(searches, start=1):
            f.write(json.dumps({"cycle": i, "stage": "candidates", "strategy": "x", "detail": {"sources": 3}}) + "\n")
            f.write(json.dumps({"cycle": i, "stage": "moves", "strategy": "random-merge", "detail": s}) + "\n")
            f.write(json.dumps({"cycle": i, "stage": "moves", "strategy": "random-merge", "kind": "merge",
                                "move_id": f"m-{i}", "detail": {}}) + "\n")
        f.write(json.dumps({"cycle": len(searches) + 1, "stage": "plan", "strategy": "p", "detail": {}}) + "\n")
    return d


def _tree(root: Path) -> None:
    full = [{"attempts": 1000, "full_simulations": 16, "moves": 5, "exhausted": 1, "timed_out": 0},
            {"attempts": 1000, "full_simulations": 16, "moves": 3, "exhausted": 1, "timed_out": 0}]
    idle = [{"attempts": 200, "full_simulations": 16, "moves": 5, "exhausted": 0, "timed_out": 0},
            {"attempts": 300, "full_simulations": 10, "moves": 3, "exhausted": 1, "timed_out": 0}]
    none = [{"attempts": 0, "full_simulations": 6, "moves": 1, "exhausted": 0, "timed_out": 0}]
    for seed in (101, 102, 103):
        _run(root, REF_H, seed, cost=40 + seed % 3, cpu_total_s=1.0, passes=10, searches=none)
        _run(root, REF_R, seed, cost=42 + seed % 3, cpu_total_s=4.0, passes=10, searches=full)
        _run(root, TREAT, seed, cost=42 + seed % 3 + 0.1 * (seed - 101), cpu_total_s=2.0, passes=10, searches=idle,
             valid=seed != 103)


def test_search_stats(tmp_path: Path) -> None:
    _tree(tmp_path)
    runs = exp9.load(tmp_path)
    assert len(runs) == 9
    idle = runs.loc[(runs["label"] == TREAT) & (runs["seed"] == 101)].iloc[0]
    assert idle["searching_passes"] == 2
    assert idle["searching_share"] == 0.2
    assert idle["attempts_per_search"] == 250
    assert idle["full_per_search"] == 13
    assert idle["moves_per_search"] == 4
    assert idle["exhausted_share"] == 0.5
    assert idle["cpu_per_search_ms"] == 1000.0
    heuristic = runs.loc[(runs["label"] == REF_H) & (runs["seed"] == 101)].iloc[0]
    assert heuristic["attempts_per_search"] == 0 and heuristic["searching_share"] == 0.1
    invalid = runs.loc[(runs["label"] == TREAT) & (runs["seed"] == 103)].iloc[0]
    assert not invalid["valid"] and math.isnan(invalid["attempts_per_search"])


def test_arm_and_paired_tables(tmp_path: Path) -> None:
    _tree(tmp_path)
    runs = exp9.load(tmp_path)
    arms = exp9.arm_table(runs)
    row = arms.loc[(arms["label"] == TREAT) & (arms["metric"] == "attempts_per_search")].iloc[0]
    assert row["n_runs"] == 3 and row["n_valid"] == 2 and bool(row["flagged"]) and row["mean"] == 250
    ref = arms.loc[(arms["label"] == REF_R) & (arms["metric"] == "steady_cost_per_hour")].iloc[0]
    assert ref["n_valid"] == 3 and abs(ref["mean"] - 43) < 1e-9
    paired = exp9.paired_table(runs)
    # Two references x every metric, for the one treatment present.
    assert set(paired["reference"]) == set(exp9.REFERENCES)
    assert set(paired["treatment"]) == {TREAT}
    assert len(paired) == 2 * len(exp9.METRICS)
    att = paired.loc[(paired["reference"] == REF_R) & (paired["metric"] == "attempts_per_search")].iloc[0]
    assert att["n_pairs"] == 2 and att["n_dropped_invalid"] == 1
    assert att["mean_diff"] == -750 and bool(att["resolved"]) and att["direction"] == "lower"
    assert abs(att["pct_diff"] + 75) < 1e-9
    cost = paired.loc[(paired["reference"] == REF_R) & (paired["metric"] == "steady_cost_per_hour")].iloc[0]
    # Differences 0 and +0.1: the bootstrap interval of the mean reaches zero, so the difference is unresolved.
    assert cost["n_pairs"] == 2 and not bool(cost["resolved"]) and cost["direction"] == "none"
    head = exp9.headline(arms, paired)
    entry = head["strata"]["W1-200"][TREAT][REF_R]
    assert any(e["metric"] == "attempts_per_search" and e["direction"] == "lower" for e in entry["resolved"])
    assert "steady_cost_per_hour" in entry["unresolved"]
    # Three seeds per arm is under MIN_RUNS, so every configuration is flagged.
    assert {f["label"] for f in head["flagged"]} == {REF_H, REF_R, TREAT}


def test_main_writes_tables(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = tmp_path / "out"
    exp9.main(tmp_path, out)
    for name in ("exp9_runs", "exp9_arms", "exp9_search", "exp9_paired"):
        assert (out / "tables" / f"{name}.md").is_file() and (out / "tables" / f"{name}.csv").is_file()
    head = json.loads((out / "tables" / "exp9.json").read_text(encoding="utf-8"))
    assert head["references"] == list(exp9.REFERENCES)
    search = (out / "tables" / "exp9_search.md").read_text(encoding="utf-8")
    assert "attempts_per_search" in search and "steady_cost_per_hour" not in search


def test_empty_tree(tmp_path: Path) -> None:
    (tmp_path / "exp9").mkdir()
    exp9.main(tmp_path, tmp_path / "out")
    assert not (tmp_path / "out" / "tables").exists()
