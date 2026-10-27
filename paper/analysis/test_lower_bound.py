"""Plain-assert tests for paper.analysis.lower_bound (B6) on hand-made instances with known optima. Run from the
repository root:

    /opt/research-venv/bin/python -m paper.analysis.test_lower_bound
"""

from __future__ import annotations

import itertools
import json
import math
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import numpy as np

from paper.analysis import lower_bound as lb

CATALOG = lb.catalog_types()
PRICE = {t.name: t.price for t in CATALOG}


def _close(x: float, y: float, tol: float = 1e-9) -> bool:
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def _ordered_bounds(r: dict, tol: float = 1e-9) -> None:
    """lp_bound <= agg_bound <= cpsat_bound <= cpsat_objective <= ffd_cost (where defined), and bound is the best."""
    chain = [r["lp_bound"], r["agg_bound"], r["cpsat_bound"], r["cpsat_objective"], r["ffd_cost"]]
    chain = [v for v in chain if math.isfinite(v)]
    for lo, hi in itertools.pairwise(chain):
        assert lo <= hi + tol * max(1.0, abs(hi)), r
    assert _close(r["bound"], max(v for v in (r["lp_bound"], r["agg_bound"], r["cpsat_bound"]) if math.isfinite(v)))


def _types(rows: Sequence[tuple[str, int, int, int, float]]) -> list[lb.InstanceType]:
    return lb.catalog_types([{"name": n, "cpu_millicores": c, "memory_mib": m, "pods": p, "price_per_hour": pr}
                             for n, c, m, p, pr in rows])


# --- brute force reference -------------------------------------------------------------------------------------------


def _partitions(items: list[int]) -> Iterator[list[list[int]]]:
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in _partitions(rest):
        for i in range(len(part)):
            yield part[:i] + [[first] + part[i]] + part[i + 1:]
        yield [[first]] + part


def brute_force(shapes: Sequence[tuple[int, int, int]], types: Sequence[lb.InstanceType]) -> float:
    """The exact optimum by enumerating every partition of the pods into nodes (tiny instances only)."""
    pods = [(c, m) for c, m, n in shapes for _ in range(n)]
    best = math.inf
    for part in _partitions(list(range(len(pods)))):
        total = 0.0
        for block in part:
            used = (sum(pods[i][0] for i in block), sum(pods[i][1] for i in block), len(block))
            fit = [t.price for t in types if all(c >= u for c, u in zip(t.caps, used))]
            if not fit:
                total = math.inf
                break
            total += min(fit)
        best = min(best, total)
    return best


# --- tests ---------------------------------------------------------------------------------------------------------


def test_exact_fill_one_type() -> None:
    # One pod that is exactly a c7i.large, then two half-size pods that fill one: the packing optimum is its price.
    # The LP may buy a fraction of a type with cheaper CPU (c7i.2xlarge), so it is lower, never higher.
    for shapes in ([(1900, 4086, 1)], [(950, 2043, 2)]):
        r = lb.bounds(shapes, CATALOG)
        for k in ("cpsat_bound", "cpsat_objective", "ffd_cost", "bound"):
            assert _close(r[k], PRICE["c7i.large"]), (shapes, k, r[k])
        assert 1900 * PRICE["c7i.2xlarge"] / 7900 - 1e-12 <= r["lp_bound"] < r["cpsat_bound"], r["lp_bound"]
        assert r["cpsat_status"] == "OPTIMAL" and r["cpsat_mix"] == {"c7i.large": 1}
        _ordered_bounds(r)
    # With that one type alone in the catalog, the LP is exact too.
    only = [t for t in CATALOG if t.name == "c7i.large"]
    r = lb.bounds([(950, 2043, 2)], only)
    for k in ("lp_bound", "agg_bound", "cpsat_bound", "cpsat_objective", "ffd_cost", "bound"):
        assert _close(r[k], PRICE["c7i.large"]), (k, r[k])


def test_exact_fill_many_nodes() -> None:
    # 4 pods of (1975m, 4093Mi) fill a c7i.2xlarge exactly in CPU, the cheapest CPU in the catalog: k nodes.
    for k in (3, 8, 13):
        r = lb.bounds([(1975, 4093, 4 * k)], CATALOG)
        want = k * PRICE["c7i.2xlarge"]
        assert _close(r["lp_bound"], want) and _close(r["cpsat_bound"], want), r
        assert _close(r["cpsat_objective"], want) and r["cpsat_status"] == "OPTIMAL"
        assert r["cpsat_mix"] == {"c7i.2xlarge": k}
        _ordered_bounds(r)
    # Memory-heavy pods: 16 of (1975m, 16381Mi) fill an r7i.8xlarge; the LP is a hair below the integer optimum.
    r = lb.bounds([(1975, 16381, 32)], CATALOG)
    assert _close(r["cpsat_bound"], 2 * PRICE["r7i.8xlarge"]) and r["cpsat_status"] == "OPTIMAL"
    assert r["lp_bound"] < r["cpsat_bound"] and _close(r["lp_bound"], 32 * 16381 / 262134 * PRICE["r7i.8xlarge"])
    _ordered_bounds(r)


def test_integrality_gap() -> None:
    # Two pods of 1000m: 2000m > a c7i.large. The LP buys 2000m of the cheapest CPU, the optimum is one c7i.xlarge
    # (0.19635, a hair under two c7i.large at 0.19636).
    r = lb.bounds([(1000, 1000, 2)], CATALOG)
    assert _close(r["lp_bound"], 2000 * PRICE["c7i.2xlarge"] / 7900)
    assert _close(r["cpsat_bound"], PRICE["c7i.xlarge"]) and r["cpsat_status"] == "OPTIMAL"
    assert r["lp_bound"] < r["cpsat_bound"] <= r["ffd_cost"] + 1e-12
    _ordered_bounds(r)


def test_more_nodes_than_ffd() -> None:
    # A catalog where one big node holds everything but three small ones are cheaper: the optimum needs more nodes
    # than the cheapest single-type FFD packing in big nodes, so B must not be the FFD node count.
    # FFD packs (5)+(4) and (4)+(4) into two big nodes ($20); the optimum is one big node for (5)+(4) and two small
    # ones ($16, three nodes).
    types = _types([("big", 12, 12, 12, 10.0), ("small", 4, 4, 4, 3.0)])
    shapes = [(4, 4, 3), (5, 5, 1)]
    r = lb.bounds(shapes, types)
    assert _close(r["ffd_cost"], 20.0) and r["ffd_bins"] == 2, r
    assert _close(brute_force(shapes, types), 16.0)
    assert _close(r["cpsat_bound"], 16.0) and _close(r["cpsat_objective"], 16.0), r
    assert r["cpsat_mix"] == {"big": 1, "small": 2} and r["cpsat_bins_used"] == 3 > r["ffd_bins"]
    assert r["cpsat_max_bins"] >= 3
    _ordered_bounds(r)


def test_random_tiny_instances_match_brute_force() -> None:
    rng = np.random.default_rng(0)
    checked = 0
    for _ in range(60):
        types = _types([(f"t{i}", int(rng.integers(2, 13)), int(rng.integers(2, 13)), int(rng.integers(1, 5)),
                         float(rng.integers(10, 100)) / 10) for i in range(int(rng.integers(1, 4)))])
        shapes = [(int(rng.integers(1, 7)), int(rng.integers(1, 7)), int(rng.integers(1, 3)))
                  for _ in range(int(rng.integers(1, 4)))]
        shapes = lb.normalize_shapes(shapes)
        if sum(n for *_, n in shapes) > 7:
            continue
        opt = brute_force(shapes, types)
        r = lb.bounds(shapes, types)
        if not math.isfinite(opt):
            assert r["cpsat_status"] == "infeasible" and math.isnan(r["ffd_cost"]), (shapes, types, r)
            continue
        checked += 1
        assert r["cpsat_status"] == "OPTIMAL", (shapes, types, r)
        assert _close(r["cpsat_bound"], opt) and _close(r["cpsat_objective"], opt), (shapes, types, opt, r)
        assert r["lp_bound"] <= opt + 1e-9 and opt <= r["ffd_cost"] + 1e-9
        _ordered_bounds(r)
    assert checked >= 30, checked


def test_self_mergeable_catalogs_match_brute_force() -> None:
    # Catalogs with a type and its double at 1.6x to 2.2x the price, so the "at most one node of a self-mergeable
    # type" caps are exercised (at factor <= 2) and not applied (above 2).
    rng = np.random.default_rng(1)
    capped = 0
    for factor in (1.6, 1.8, 2.0, 2.2):
        for _ in range(8):
            c, m, p = int(rng.integers(3, 7)), int(rng.integers(3, 7)), int(rng.integers(2, 4))
            price = float(rng.integers(10, 40)) / 10
            other = ("o", int(rng.integers(2, 9)), int(rng.integers(2, 9)), 3, float(rng.integers(10, 60)) / 10)
            types = _types([("s", c, m, p, price), ("d", 2 * c, 2 * m, 2 * p, round(factor * price, 4)), other])
            shapes = lb.normalize_shapes([(int(rng.integers(1, c + 1)), int(rng.integers(1, m + 1)),
                                           int(rng.integers(1, 4))) for _ in range(2)])
            if sum(n for *_, n in shapes) > 7:
                continue
            capped += bool(lb.self_mergeable(lb.undominated(types)))
            opt = brute_force(shapes, types)
            r = lb.bounds(shapes, types)
            assert r["cpsat_status"] == "OPTIMAL" and _close(r["cpsat_bound"], opt), (factor, shapes, types, opt, r)
            _ordered_bounds(r)
    assert capped >= 10, capped


def reference_optimum(shapes: Sequence[tuple[int, int, int]], types: Sequence[lb.InstanceType],
                      limit_s: float = 60.0) -> float | None:
    """The packing optimum from a plain CP-SAT model that shares none of lower_bound's restrictions (every type,
    dominated ones included; one bin per pod; no node-count caps, no self-merge caps, no cuts). None unless proven
    optimal within ``limit_s``."""
    from ortools.sat.python import cp_model

    n_bins = sum(n for *_, n in shapes)
    scale = 100_000
    prices = [round(t.price * scale) for t in types]
    model = cp_model.CpModel()
    y = [[model.new_bool_var("") for _ in types] for _ in range(n_bins)]
    x = [[model.new_int_var(0, n, "") for *_, n in shapes] for _ in range(n_bins)]
    cost = [sum(p * v for p, v in zip(prices, y[b])) for b in range(n_bins)]
    for b in range(n_bins):
        model.add(sum(y[b]) <= 1)
        for d in range(3):
            model.add(sum((s[0], s[1], 1)[d] * x[b][i] for i, s in enumerate(shapes))
                      <= sum(t.caps[d] * y[b][k] for k, t in enumerate(types)))
        if b + 1 < n_bins:
            model.add(cost[b] >= cost[b + 1])
    for i, s in enumerate(shapes):
        model.add(sum(x[b][i] for b in range(n_bins)) == s[2])
    model.minimize(sum(cost))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = limit_s
    if solver.solve(model) != cp_model.OPTIMAL:
        return None
    return float(sum(t.price for b in range(n_bins) for k, t in enumerate(types) if solver.boolean_value(y[b][k])))


def test_property_real_catalog() -> None:
    # The validity property on the real catalog, against references independent of lower_bound: 20 tiny multisets
    # (brute force) and 10 medium ones (the unrestricted reference model), with pod shapes like the workloads'.
    # lp_bound <= cpsat_bound <= optimum <= ffd_cost, and cpsat_bound <= cpsat_objective.
    rng = np.random.default_rng(20261027)
    cpus = [100, 250, 500, 750, 1000, 1500, 2000, 4000, 7000]
    mems = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 30000]
    checked = 0
    for i in range(30):
        tiny = i < 20
        shapes = lb.normalize_shapes([(int(rng.choice(cpus)), int(rng.choice(mems)),
                                       int(rng.integers(1, 4) if tiny else rng.integers(2, 9)))
                                      for _ in range(int(rng.integers(1, 4) if tiny else rng.integers(2, 5)))])
        while tiny and sum(n for *_, n in shapes) > 7:
            shapes = shapes[:-1]
        opt = brute_force(shapes, CATALOG) if tiny else reference_optimum(shapes, CATALOG)
        if opt is None:
            continue
        r = lb.bounds(shapes, CATALOG)
        tol = 1e-9 * max(1.0, opt)
        assert r["lp_bound"] <= r["cpsat_bound"] + tol, (shapes, r)
        assert r["cpsat_bound"] <= opt + tol, ("not a lower bound", shapes, opt, r)
        assert opt <= r["ffd_cost"] + tol and r["cpsat_bound"] <= r["cpsat_objective"] + tol, (shapes, opt, r)
        assert r["agg_bound"] <= opt + tol and r["bound"] <= opt + tol, (shapes, opt, r)
        assert r["cpsat_status"] == "OPTIMAL" and _close(r["cpsat_objective"], opt, 1e-6), (shapes, opt, r)
        checked += 1
    assert checked >= 28, checked


def test_edge_cases() -> None:
    r = lb.bounds([], CATALOG)
    assert r["n_pods"] == 0 and r["lp_bound"] == 0 and r["cpsat_bound"] == 0 and r["ffd_cost"] == 0
    assert r["cpsat_status"] == "trivial" and r["bound"] == 0
    # A pod larger than every type: no packing, but the LP still bounds the aggregate demand.
    r = lb.bounds([(64000, 1024, 1)], CATALOG)
    assert r["cpsat_status"] == "infeasible" and math.isnan(r["ffd_cost"]) and math.isnan(r["cpsat_bound"])
    assert math.isfinite(r["lp_bound"]) and _close(r["bound"], r["lp_bound"])
    # Above the CP-SAT size limit only the LP and FFD are computed.
    r = lb.bounds([(250, 512, lb.CPSAT_MAX_PODS + 1)], CATALOG)
    assert r["cpsat_status"] == "skipped" and math.isnan(r["cpsat_bound"]) and math.isfinite(r["lp_bound"])
    assert r["lp_bound"] <= r["ffd_cost"]
    r = lb.bounds([(250, 512, 10)], CATALOG, cpsat=False)
    assert r["cpsat_status"] == "not requested" and math.isnan(r["cpsat_bound"]) and math.isfinite(r["ffd_cost"])
    try:
        lb.normalize_shapes([(100, -1, 1)])
    except ValueError:
        pass
    else:
        raise AssertionError("negative shape accepted")


def test_normalize_and_key() -> None:
    a = lb.normalize_shapes([(500, 1024, 2), (250, 512, 3), (500, 1024, 1), (100, 100, 0)])
    assert a == ((250, 512, 3), (500, 1024, 3))
    k = lb.instance_key(a, CATALOG)
    assert k == lb.instance_key([[500, 1024, 3], [250, 512, 1], [250, 512, 2]], CATALOG)
    assert k != lb.instance_key([(250, 512, 3), (500, 1024, 4)], CATALOG)
    cheaper = lb.catalog_types([{"name": t.name, "cpu_millicores": t.cpu, "memory_mib": t.memory, "pods": t.pods,
                                 "price_per_hour": t.price * (0.5 if t.name == "c7i.large" else 1)} for t in CATALOG])
    assert k != lb.instance_key(a, cheaper)
    assert len(k) == 64 and all(c in "0123456789abcdef" for c in k)


def test_dominance_and_merging() -> None:
    names = [t.name for t in lb.undominated(CATALOG)]
    assert names == sorted(names) and len(names) == len(CATALOG)  # nothing in this catalog is dominated
    types = _types([("a", 4, 4, 4, 1.0), ("b", 4, 4, 4, 1.0), ("c", 2, 4, 4, 1.5), ("d", 8, 8, 8, 2.0)])
    assert [t.name for t in lb.undominated(types)] == ["a", "d"]  # b duplicates a, c is dominated by a
    single = {t.name for t in lb.self_mergeable(CATALOG)}
    assert {"c7i.large", "c7i.xlarge", "c7i.4xlarge", "m7i.large"} <= single
    assert "c7i.2xlarge" not in single and "m7i.xlarge" not in single
    shapes = lb.normalize_shapes([(500, 1024, 40), (1000, 2048, 10)])
    packing, _ = lb.ffd_pack(shapes, CATALOG)
    merged = lb.merge_pass(packing, shapes, CATALOG)
    assert merged.cost <= packing.cost + 1e-12
    assert sorted(sum(c.get(i, 0) for _, c in merged.bins) for i in range(len(shapes))) == sorted(
        s[2] for s in shapes)


def test_ffd_is_feasible() -> None:
    shapes = lb.normalize_shapes([(250, 512, 70), (500, 1024, 41), (1000, 4096, 21), (4000, 8192, 9)])
    packing, base = lb.ffd_pack(shapes, CATALOG)
    assert base and packing is not None
    for t, content in packing.bins:
        used = (sum(shapes[i][0] * k for i, k in content.items()), sum(shapes[i][1] * k for i, k in content.items()),
                sum(content.values()))
        assert all(c >= u for c, u in zip(t.caps, used)), (t, used)
    for i, s in enumerate(shapes):
        assert sum(c.get(i, 0) for _, c in packing.bins) == s[2]
    assert _close(lb.ffd_bound(shapes, CATALOG)["ffd_cost"], packing.cost)


def test_deterministic() -> None:
    shapes = [(250, 512, 30), (500, 2048, 20), (1000, 2048, 12), (2000, 8192, 6), (4000, 8192, 4)]
    first = lb.bounds(shapes, CATALOG)
    assert first["cpsat_status"] in ("OPTIMAL", "FEASIBLE") and not first["cpsat_wall_limited"]
    assert json.dumps(first, sort_keys=True) == json.dumps(lb.bounds(shapes, CATALOG), sort_keys=True)
    _ordered_bounds(first)


def test_cache_roundtrip() -> None:
    sets = [[(1975, 4093, 8)], [(1975, 4093, 4), (1975, 4093, 4)], [(1000, 1000, 2)], [(250, 512, 700)]]
    with tempfile.TemporaryDirectory() as tmp:
        path = lb.cache_path(Path(tmp))
        first = lb.compute_bounds(sets, CATALOG, lb.BoundCache(path))
        assert len(first) == 3  # the first two are one multiset
        assert path.exists() and not path.with_name(path.name + ".tmp").exists()
        raw = path.read_text(encoding="utf-8")
        assert tmp not in raw and "NaN" not in raw
        # A fresh cache answers from the file without solving anything.
        original = lb.cpsat_bound
        lb.cpsat_bound = lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache miss"))
        try:
            again = lb.compute_bounds(sets, CATALOG, lb.BoundCache(path))
        finally:
            lb.cpsat_bound = original
        assert json.dumps(again, sort_keys=True) == json.dumps(first, sort_keys=True)
        assert path.read_text(encoding="utf-8") == raw
        # An entry computed without CP-SAT is completed when CP-SAT is asked for.
        lp_only = lb.compute_bounds([[(950, 2043, 2)]], CATALOG, lb.BoundCache(path), cpsat=False)
        assert next(iter(lp_only.values()))["cpsat_status"] == "not requested"
        full = lb.compute_bounds([[(950, 2043, 2)]], CATALOG, lb.BoundCache(path), cpsat=True)
        assert next(iter(full.values()))["cpsat_status"] == "OPTIMAL"
        # A corrupt or foreign file is ignored and rewritten; deleting it is safe.
        path.write_text("{not json", encoding="utf-8")
        assert lb.BoundCache(path).entries == {}
        path.write_text(json.dumps({"model": "other", "entries": {"x": {}}}), encoding="utf-8")
        assert lb.BoundCache(path).entries == {}
        path.unlink()
        fresh = lb.compute_bounds(sets, CATALOG, lb.BoundCache(path))
        assert json.dumps(fresh, sort_keys=True) == json.dumps(first, sort_keys=True)


def main() -> int:
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
    sys.exit(main())
