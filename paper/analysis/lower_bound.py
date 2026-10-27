"""B6, the offline packing lower bound: how cheap a fleet could be that holds a given pod multiset.

The input is one podsets.jsonl snapshot, ``shapes = [(cpu_millicores, memory_mib, count), ...]``, and the instance
catalog (experiments/catalog.json: allocatable ``cpu_millicores``, ``memory_mib``, ``pods`` and ``price_per_hour``).
Every pod takes its CPU and memory request and one pod slot. All results are in $/hr:

- ``lp_bound``: the LP relaxation ``min sum_t price_t n_t`` s.t. ``sum_t n_t cap_{t,d} >= demand_d`` for d in
  {cpu, memory, pods}, ``n_t >= 0`` (scipy ``linprog``, HiGHS). A valid lower bound at any size; ``lp_mix`` is the
  fractional fleet that attains it.
- ``ffd_cost``: a feasible packing, so an upper bound. First-fit decreasing (pods by decreasing dominant share of the
  bin) into bins of a single instance type, run once for every type that fits every shape; each bin is then right-sized
  to the cheapest type that holds its contents, and the cheapest result wins (``ffd_type`` names its bin type).
- ``cpsat_bound``: for instances with at most ``CPSAT_MAX_PODS`` pods, an exact typed bin-packing model solved by
  OR-Tools CP-SAT. ``cpsat_bound`` is the solver's best objective bound (a valid lower bound even when optimality is
  not proven), ``cpsat_objective`` the cost of its best packing (an upper bound), ``cpsat_status`` the solver status
  (OPTIMAL means the two are equal). Larger instances get status ``skipped`` and NaN.
- ``bound``: the best valid lower bound, ``max(lp_bound, cpsat_bound)``. Both are valid, so their maximum is too; with
  the aggregate-capacity constraint in the model the CP-SAT bound is normally the larger.

The CP-SAT model, for bins ``b = 0..B-1``: ``y[b,t]`` in {0,1} with ``sum_t y[b,t] <= 1`` (bin b is of type t);
integer ``x[b,s] >= 0`` pods of shape s in bin b; ``sum_s x[b,s] r_{s,d} <= sum_t y[b,t] cap_{t,d}`` for each
dimension d; ``sum_b x[b,s] = count_s``; symmetry breaking (bin cost non-increasing in b, and a bin is used only if the
previous one is); the redundant aggregate constraint ``sum_{b,t} y[b,t] cap_{t,d} >= demand_d`` (it tightens the
solver's LP relaxation to at least ``lp_bound``); objective ``sum_{b,t} price_t y[b,t]`` with prices in integer units
of ``1 / PRICE_SCALE`` dollars (rounded down, so the scaled bound stays a valid bound; the catalog's prices are exact
at this scale). Instance types dominated by another (no more capacity in any dimension at no lower price) are left
out: any bin of a dominated type can switch to its dominator at no extra cost, so the optimum is unchanged.

``B`` comes from the first-fit-decreasing packing: an optimal fleet costs at most ``ffd_cost`` and every node costs at
least the cheapest price, so it has at most ``floor(ffd_cost / min_price)`` nodes (and never more nodes than pods).
The number of bins of the FFD packing itself would not do: an optimal fleet may use more, smaller nodes, and a model
capped at fewer bins would overstate the bound. The FFD packing is the solver's starting hint.

Results are deterministic for a given input: one search worker, a fixed seed, and a deterministic-time limit
(``CPSAT_PARAMS``; the wall-clock limit is a far looser safety net, and a result it cut short is flagged
``cpsat_wall_limited`` and logged). ``BoundCache`` keeps results in ``<out>/tables/cache/lower_bounds.json`` keyed by a
hash of the shape multiset, the catalog and the model settings; the file is derived and safe to delete.

Run ``/opt/research-venv/bin/python -m paper.analysis.lower_bound SHAPES_JSON`` to bound one multiset (a JSON list of
``[cpu_millicores, memory_mib, count]``) against experiments/catalog.json.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from paper.analysis import common

MODEL_VERSION = "b6-v1"  # part of the cache key: bump when the model or any parameter below changes meaning
CPSAT_MAX_PODS = 600
PRICE_SCALE = 100_000  # integer price units per dollar in the CP-SAT objective
CPSAT_PARAMS: dict[str, Any] = {
    "num_workers": 1,  # two workers interleave nondeterministically; one worker is reproducible
    "random_seed": 0,
    "max_deterministic_time": 10.0,
    "max_time_in_seconds": 120.0,  # safety net only; the deterministic limit is the one meant to bind
}
CACHE_RELPATH = Path("tables") / "cache" / "lower_bounds.json"
DIMENSIONS = ("cpu", "memory", "pods")
NUMERIC_FIELDS = ("lp_bound", "ffd_cost", "agg_bound", "cpsat_bound", "cpsat_objective", "bound")

Shape = tuple[int, int, int]


@dataclass(frozen=True)
class InstanceType:
    """One catalog entry as the packing sees it: allocatable capacity and hourly price."""

    name: str
    cpu: int
    memory: int
    pods: int
    price: float

    @property
    def caps(self) -> tuple[int, int, int]:
        return (self.cpu, self.memory, self.pods)


# --------------------------------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------------------------------


def catalog_types(catalog: pd.DataFrame | Sequence[Mapping[str, Any]] | None = None) -> list[InstanceType]:
    """The catalog as ``InstanceType``s sorted by name: a DataFrame or list of rows with name, cpu_millicores,
    memory_mib, pods and price_per_hour; ``None`` reads experiments/catalog.json (``common.load_catalog``)."""
    if catalog is None:
        catalog = common.load_catalog()
    rows = catalog.to_dict(orient="records") if isinstance(catalog, pd.DataFrame) else list(catalog)
    types = [InstanceType(str(r["name"]), int(r["cpu_millicores"]), int(r["memory_mib"]), int(r["pods"]),
                          float(r["price_per_hour"])) for r in rows]
    names = [t.name for t in types]
    if len(set(names)) != len(names):
        raise ValueError("catalog has duplicate instance type names")
    bad = [t.name for t in types if min(t.caps) <= 0 or not math.isfinite(t.price) or t.price <= 0]
    if bad:
        raise ValueError(f"catalog types without positive capacity and price: {', '.join(sorted(bad))}")
    return sorted(types, key=lambda t: t.name)


def normalize_shapes(shapes: Iterable[Sequence[Any]]) -> tuple[Shape, ...]:
    """The multiset as sorted ``(cpu_millicores, memory_mib, count)`` tuples, duplicates merged, zero counts
    dropped. Raises ValueError on negative requests or counts."""
    merged: dict[tuple[int, int], int] = {}
    for item in shapes:
        cpu, mem, count = (int(v) for v in item)
        if cpu < 0 or mem < 0 or count < 0:
            raise ValueError(f"negative shape {item!r}")
        if count:
            merged[(cpu, mem)] = merged.get((cpu, mem), 0) + count
    return tuple((c, m, n) for (c, m), n in sorted(merged.items()))


def demand(shapes: Sequence[Shape]) -> tuple[int, int, int]:
    """Total (cpu_millicores, memory_mib, pods) of a normalized multiset."""
    return (sum(c * n for c, _, n in shapes), sum(m * n for _, m, n in shapes), sum(n for *_, n in shapes))


def _catalog_material(types: Sequence[InstanceType]) -> list[list[Any]]:
    return [[t.name, t.cpu, t.memory, t.pods, t.price] for t in types]


def instance_key(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType]) -> str:
    """Cache key: SHA-256 (hex) of the normalized multiset, the catalog and the model settings."""
    material = {"model": MODEL_VERSION, "cpsat": CPSAT_PARAMS, "cpsat_max_pods": CPSAT_MAX_PODS,
                "price_scale": PRICE_SCALE, "catalog": _catalog_material(types),
                "shapes": [list(s) for s in normalize_shapes(shapes)]}
    text = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fits(shape: Shape, caps: Sequence[int]) -> bool:
    return shape[0] <= caps[0] and shape[1] <= caps[1] and caps[2] >= 1


def _fit_count(cpu: int, mem: int, rem: Sequence[int]) -> int:
    """How many pods of (cpu, mem) fit into remaining capacity (cpu, mem, pod slots)."""
    k = int(rem[2])
    if cpu > 0:
        k = min(k, int(rem[0]) // cpu)
    if mem > 0:
        k = min(k, int(rem[1]) // mem)
    return max(k, 0)


def undominated(types: Sequence[InstanceType]) -> list[InstanceType]:
    """``types`` without those another type dominates (at least as much of every dimension at no higher price; among
    identical types the first by name is kept). Sorted by name."""
    out = []
    for u in types:
        dominated = False
        for v in types:
            if v is u or not all(cv >= cu for cv, cu in zip(v.caps, u.caps)) or v.price > u.price:
                continue
            strictly = v.price < u.price or any(cv > cu for cv, cu in zip(v.caps, u.caps))
            if strictly or v.name < u.name:
                dominated = True
                break
        if not dominated:
            out.append(u)
    return sorted(out, key=lambda t: t.name)


# --------------------------------------------------------------------------------------------------------------------
# LP relaxation
# --------------------------------------------------------------------------------------------------------------------


def lp_bound(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType]) -> dict[str, Any]:
    """The LP relaxation bound: ``lp_bound`` ($/hr), ``lp_status`` ("optimal", "trivial" for no pods, or the HiGHS
    message) and ``lp_mix`` ({type name: fractional node count} for the types the LP uses)."""
    shapes = normalize_shapes(shapes)
    need = np.array(demand(shapes), dtype=float)
    if need[2] == 0:
        return {"lp_bound": 0.0, "lp_status": "trivial", "lp_mix": {}}
    prices = np.array([t.price for t in types], dtype=float)
    caps = np.array([t.caps for t in types], dtype=float).T  # (dimension, type)
    res = linprog(prices, A_ub=-caps, b_ub=-need, bounds=[(0, None)] * len(types), method="highs")
    if res.status != 0:
        return {"lp_bound": math.nan, "lp_status": str(res.message), "lp_mix": {}}
    mix = {t.name: float(n) for t, n in zip(types, res.x) if n > 1e-9}
    return {"lp_bound": float(res.fun), "lp_status": "optimal", "lp_mix": mix}


# --------------------------------------------------------------------------------------------------------------------
# Feasible packings (upper bounds)
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Packing:
    """A feasible packing: one (instance type, {shape index: pod count}) per node."""

    bins: list[tuple[InstanceType, dict[int, int]]]

    @property
    def cost(self) -> float:
        return float(sum(t.price for t, _ in self.bins))


def _cheapest_fit(used: Sequence[int], types: Sequence[InstanceType]) -> InstanceType | None:
    ok = [t for t in types if all(c >= u for c, u in zip(t.caps, used))]
    return min(ok, key=lambda t: (t.price, t.name)) if ok else None


def _used(shapes: Sequence[Shape], content: Mapping[int, int]) -> tuple[int, int, int]:
    return (sum(shapes[i][0] * k for i, k in content.items()), sum(shapes[i][1] * k for i, k in content.items()),
            sum(content.values()))


def _ffd_single(shapes: Sequence[Shape], base: InstanceType, types: Sequence[InstanceType],
                counts: Sequence[int] | None = None) -> Packing | None:
    """First-fit decreasing of the pods (``counts[i]`` of shape i, default the shapes' own counts) into bins of
    ``base``, each bin then right-sized to the cheapest of ``types`` that holds it. Pods are ordered by decreasing
    dominant share of ``base``; pods of one shape are placed in bulk (identical consecutive items fill the first bin
    with room, as item-by-item FFD would). None if a pod to place does not fit ``base``."""
    counts = [s[2] for s in shapes] if counts is None else list(counts)
    todo = [i for i in range(len(shapes)) if counts[i] > 0]
    if not all(_fits(shapes[i], base.caps) for i in todo):
        return None
    order = sorted(todo, key=lambda i: (-max(shapes[i][0] / base.cpu, shapes[i][1] / base.memory, 1 / base.pods),
                                        -shapes[i][0], -shapes[i][1], i))
    rem: list[list[int]] = []
    content: list[dict[int, int]] = []
    for i in order:
        cpu, mem, _ = shapes[i]
        left = counts[i]
        for r, c in zip(rem, content):
            if left == 0:
                break
            k = min(left, _fit_count(cpu, mem, r))
            if k:
                r[0] -= k * cpu
                r[1] -= k * mem
                r[2] -= k
                c[i] = c.get(i, 0) + k
                left -= k
        per_bin = _fit_count(cpu, mem, base.caps)
        while left > 0:
            k = min(left, per_bin)
            rem.append([base.cpu - k * cpu, base.memory - k * mem, base.pods - k])
            content.append({i: k})
            left -= k
    return Packing([(_cheapest_fit(_used(shapes, c), types) or base, c) for c in content])


def ffd_pack(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType]) -> tuple[Packing | None, str | None]:
    """The cheapest first-fit-decreasing packing over every single base type that fits every shape (ties: base type
    name), and that base type's name. (None, None) when some shape fits no type."""
    shapes = normalize_shapes(shapes)
    best: tuple[float, str, Packing] | None = None
    for base in types:
        p = _ffd_single(shapes, base, types)
        if p is not None and (best is None or (p.cost, base.name) < (best[0], best[1])):
            best = (p.cost, base.name, p)
    return (best[2], best[1]) if best else (None, None)


def ffd_bound(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType]) -> dict[str, Any]:
    """``ffd_cost`` ($/hr, NaN when infeasible), ``ffd_bins`` (nodes) and ``ffd_type`` (the base bin type)."""
    shapes = normalize_shapes(shapes)
    if not shapes:
        return {"ffd_cost": 0.0, "ffd_bins": 0, "ffd_type": ""}
    packing, base = ffd_pack(shapes, types)
    if packing is None:
        return {"ffd_cost": math.nan, "ffd_bins": 0, "ffd_type": ""}
    return {"ffd_cost": packing.cost, "ffd_bins": len(packing.bins), "ffd_type": base}


def merge_pass(packing: Packing, shapes: Sequence[Shape], types: Sequence[InstanceType]) -> Packing:
    """``packing`` improved by merging pairs of nodes: while the contents of two nodes fit one type that costs no
    more than the pair, replace the pair by the cheapest such type (first pair in order, most expensive pairs
    first). The result is feasible and costs no more; no two of its nodes of one type could merge into a type
    costing at most twice as much."""
    bins = [(t, dict(c)) for t, c in packing.bins]
    while True:
        bins.sort(key=lambda bc: (-bc[0].price, bc[0].name, sorted(bc[1].items())))
        merged = False
        for i in range(len(bins)):
            for j in range(i + 1, len(bins)):
                (ti, ci), (tj, cj) = bins[i], bins[j]
                both = dict(ci)
                for k, v in cj.items():
                    both[k] = both.get(k, 0) + v
                w = _cheapest_fit(_used(shapes, both), types)
                if w is not None and w.price <= ti.price + tj.price:
                    bins[i] = (w, both)
                    del bins[j]
                    merged = True
                    break
            if merged:
                break
        if not merged:
            return Packing(bins)


# --------------------------------------------------------------------------------------------------------------------
# CP-SAT exact model
# --------------------------------------------------------------------------------------------------------------------


def _scaled(price: float) -> int:
    """A price in integer objective units, rounded down (so a scaled bound is still a bound in dollars)."""
    return int(math.floor(price * PRICE_SCALE + 1e-6))


def self_mergeable(types: Sequence[InstanceType]) -> list[InstanceType]:
    """Types u for which some type w holds twice u's capacity at no more than twice u's price. Some optimal fleet
    (one with the fewest nodes among the optimal ones) has at most one node of each such type, since two could merge
    into a w at no extra cost."""
    return [u for u in types
            if any(all(cw >= 2 * cu for cw, cu in zip(w.caps, u.caps)) and w.price <= 2 * u.price for w in types)]


def _empty_cpsat(status: str, max_bins: int = 0) -> dict[str, Any]:
    return {"cpsat_bound": math.nan, "cpsat_objective": math.nan, "cpsat_status": status, "cpsat_max_bins": max_bins,
            "cpsat_bins_used": 0, "cpsat_mix": {}, "cpsat_wall_limited": False, "agg_bound": math.nan,
            "agg_status": status}


def _solver(params: Mapping[str, Any]) -> Any:
    from ortools.sat.python import cp_model

    solver = cp_model.CpSolver()
    for k, v in params.items():
        setattr(solver.parameters, k, v)
    solver.parameters.log_search_progress = False
    return solver


def _wall_limited(solver: Any, status: int, params: Mapping[str, Any]) -> bool:
    from ortools.sat.python import cp_model

    limit = float(params.get("max_time_in_seconds", math.inf))
    return bool(status not in (cp_model.OPTIMAL, cp_model.INFEASIBLE) and solver.wall_time >= 0.99 * limit)


def aggregate_relaxation(shapes: Sequence[Shape], types: Sequence[InstanceType], n_max: Sequence[int],
                         params: Mapping[str, Any]) -> tuple[int | None, str, list[int], list[list[int]]]:
    """The integer aggregate relaxation, in the CP-SAT objective's integer price units: integer node counts n_t (at
    most ``n_max[t]``) and integer pod counts z[t][s] (zero where shape s does not fit type t) with
    ``sum_t z[t][s] = count_s`` and, per type and dimension, ``sum_s z[t][s] r_{s,d} <= n_t cap_{t,d}``. It drops
    only the split of a type's pods across its nodes, so every packing satisfies it and its optimum bounds the
    packing optimum from below. Returns (proven bound or None, status name, n, z of its best solution)."""
    from ortools.sat.python import cp_model

    prices = [_scaled(t.price) for t in types]
    model = cp_model.CpModel()
    n = [model.new_int_var(0, int(n_max[t]), f"n_{t}") for t in range(len(types))]
    z = [[model.new_int_var(0, s[2] if _fits(s, ty.caps) else 0, f"z_{t}_{i}") for i, s in enumerate(shapes)]
         for t, ty in enumerate(types)]
    for i, s in enumerate(shapes):
        model.add(sum(z[t][i] for t in range(len(types))) == s[2])
    for t, ty in enumerate(types):
        for d in range(len(DIMENSIONS)):
            size = [(s[0], s[1], 1)[d] for s in shapes]
            model.add(sum(size[i] * z[t][i] for i in range(len(shapes))) <= ty.caps[d] * n[t])
    model.minimize(sum(p * v for p, v in zip(prices, n)))
    solver = _solver(params)
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        bound = None if status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID) else math.ceil(
            solver.best_objective_bound - 1e-6)
        return bound, solver.status_name(status), [], []
    bound = int(math.ceil(solver.best_objective_bound - 1e-6))
    return (bound, solver.status_name(status), [int(solver.value(v)) for v in n],
            [[int(solver.value(v)) for v in row] for row in z])


def _repair(shapes: Sequence[Shape], types: Sequence[InstanceType], z: Sequence[Sequence[int]]) -> Packing | None:
    """A feasible packing from a relaxation solution: each type's assigned pods packed first-fit decreasing into
    nodes of that type, then right-sized. None without a solution."""
    if not z:
        return None
    bins: list[tuple[InstanceType, dict[int, int]]] = []
    for t, ty in enumerate(types):
        if sum(z[t]):
            p = _ffd_single(shapes, ty, types, counts=z[t])
            if p is None:
                return None
            bins += p.bins
    return Packing(bins)


def cpsat_bound(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType], max_pods: int = CPSAT_MAX_PODS,
                params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The exact bin-packing model (module docstring) solved by CP-SAT with ``CPSAT_PARAMS`` (or ``params``).

    Returns ``cpsat_bound`` (the solver's best objective bound, $/hr), ``cpsat_objective`` (cost of the best packing
    found, $/hr), ``cpsat_status`` (OPTIMAL, FEASIBLE, UNKNOWN, INFEASIBLE; "trivial" for no pods; "skipped" above
    ``max_pods`` pods; "infeasible" when some shape fits no type), ``cpsat_max_bins`` (B), ``cpsat_bins_used`` and
    ``cpsat_mix`` ({type name: nodes} of the best packing), ``cpsat_wall_limited`` (the wall-clock safety limit
    stopped a search, so the result may not reproduce), and ``agg_bound`` / ``agg_status`` (the integer aggregate
    relaxation, $/hr, which the model uses as a redundant objective cut).
    """
    from ortools.sat.python import cp_model

    shapes = normalize_shapes(shapes)
    n_pods = demand(shapes)[2]
    if n_pods == 0:
        out = _empty_cpsat("trivial")
        out.update(cpsat_bound=0.0, cpsat_objective=0.0, agg_bound=0.0)
        return out
    if n_pods > max_pods:
        return _empty_cpsat("skipped")
    p = dict(CPSAT_PARAMS if params is None else params)
    kept = undominated(types)
    packing, _ = ffd_pack(shapes, kept)
    if packing is None:
        return _empty_cpsat("infeasible")
    single = {t.name for t in self_mergeable(kept)}
    # Upper bound and node-count caps valid for some optimal fleet with the fewest nodes (module docstring).
    upper = merge_pass(packing, shapes, kept)
    n_cap = [1 if t.name in single else int(math.floor(upper.cost / t.price + 1e-9)) for t in kept]
    agg, agg_status, _, z = aggregate_relaxation(shapes, kept, n_cap, p)
    repaired = _repair(shapes, kept, z)
    if repaired is not None:
        repaired = merge_pass(repaired, shapes, kept)
        if repaired.cost < upper.cost:
            upper = repaired
            n_cap = [1 if t.name in single else int(math.floor(upper.cost / t.price + 1e-9)) for t in kept]
    others = [t.price for t in kept if t.name not in single]
    n_bins = len(single) + (int(math.floor(upper.cost / min(others) + 1e-9)) if others else 0)
    n_bins = max(min(n_bins, n_pods), len(upper.bins))
    prices = [_scaled(t.price) for t in kept]
    caps = [t.caps for t in kept]
    sizes = [(c, m, 1) for c, m, _ in shapes]
    need = demand(shapes)
    nt, ns = len(kept), len(shapes)

    model = cp_model.CpModel()
    y = [[model.new_bool_var(f"y_{b}_{t}") for t in range(nt)] for b in range(n_bins)]
    x = [[model.new_int_var(0, min(cnt, max(_fit_count(cpu, mem, cap) for cap in caps)), f"x_{b}_{s}")
          for s, (cpu, mem, cnt) in enumerate(shapes)] for b in range(n_bins)]
    used = [sum(y[b]) for b in range(n_bins)]
    cost = [sum(pr * v for pr, v in zip(prices, y[b])) for b in range(n_bins)]
    for b in range(n_bins):
        model.add(used[b] <= 1)
        for d in range(len(DIMENSIONS)):
            model.add(sum(sizes[s][d] * x[b][s] for s in range(ns)) <= sum(caps[t][d] * y[b][t] for t in range(nt)))
        if b + 1 < n_bins:
            model.add(cost[b] >= cost[b + 1])
            model.add(used[b] >= used[b + 1])
    for s in range(ns):
        model.add(sum(x[b][s] for b in range(n_bins)) == shapes[s][2])
    for t in range(nt):
        model.add(sum(y[b][t] for b in range(n_bins)) <= n_cap[t])
    for d in range(len(DIMENSIONS)):
        model.add(sum(caps[t][d] * y[b][t] for b in range(n_bins) for t in range(nt)) >= need[d])
    if agg is not None:
        model.add(sum(cost) >= agg)
    model.minimize(sum(cost))
    index = {t.name: i for i, t in enumerate(kept)}
    hint = sorted(upper.bins, key=lambda bc: (-bc[0].price, bc[0].name, sorted(bc[1].items())))
    for b in range(n_bins):
        ty, content = hint[b] if b < len(hint) else (None, {})
        for t in range(nt):
            model.add_hint(y[b][t], ty is not None and index[ty.name] == t)
        for s in range(ns):
            model.add_hint(x[b][s], content.get(s, 0))

    solver = _solver(p)
    status = solver.solve(model)
    out = _empty_cpsat(solver.status_name(status), n_bins)
    out["agg_status"] = agg_status
    out["agg_bound"] = math.nan if agg is None else agg / PRICE_SCALE
    out["cpsat_wall_limited"] = _wall_limited(solver, status, p)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        mix: dict[str, int] = {}
        for b in range(n_bins):
            for t in range(nt):
                if solver.boolean_value(y[b][t]):
                    mix[kept[t].name] = mix.get(kept[t].name, 0) + 1
        total = float(sum(kept[index[name]].price * k for name, k in sorted(mix.items())))
        out.update(cpsat_objective=total, cpsat_bins_used=int(sum(mix.values())), cpsat_mix=dict(sorted(mix.items())))
    if status not in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
        out["cpsat_bound"] = float(solver.best_objective_bound) / PRICE_SCALE
    if out["cpsat_wall_limited"]:
        common.logger.warning("lower_bound: CP-SAT hit the %.0f s wall-clock limit on a %d-pod instance; its result "
                              "may not reproduce", float(p.get("max_time_in_seconds", math.nan)), n_pods)
    return out


# --------------------------------------------------------------------------------------------------------------------
# All bounds, cached
# --------------------------------------------------------------------------------------------------------------------


def _best(entry: Mapping[str, Any]) -> float:
    vals = [float(entry.get(k, math.nan)) for k in ("lp_bound", "agg_bound", "cpsat_bound")]
    vals = [v for v in vals if math.isfinite(v)]
    return max(vals) if vals else math.nan


def bounds(shapes: Iterable[Sequence[Any]], types: Sequence[InstanceType] | None = None,
           cpsat: bool = True) -> dict[str, Any]:
    """Every bound for one multiset (no cache): n_pods, n_shapes, demand_cpu, demand_memory, the ``lp_bound``,
    ``ffd_bound`` and (with ``cpsat``) ``cpsat_bound`` fields, and ``bound`` (the best valid lower bound). Without
    ``cpsat`` the CP-SAT fields are NaN with status "not requested"."""
    types = catalog_types() if types is None else list(types)
    shapes = normalize_shapes(shapes)
    cpu, mem, pods = demand(shapes)
    entry: dict[str, Any] = {"n_pods": pods, "n_shapes": len(shapes), "demand_cpu": cpu, "demand_memory": mem}
    entry.update(lp_bound(shapes, types))
    entry.update(ffd_bound(shapes, types))
    entry.update(cpsat_bound(shapes, types) if cpsat else _empty_cpsat("not requested"))
    entry["bound"] = _best(entry)
    return entry


def _decode(entry: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    for k in NUMERIC_FIELDS:
        if out.get(k) is None:
            out[k] = math.nan
    return out


class BoundCache:
    """Bounds by ``instance_key``, persisted as JSON at ``path`` (None: in memory only). A missing, unreadable or
    other-version file starts an empty cache; ``save`` rewrites the file atomically with sorted keys."""

    def __init__(self, path: Path | None) -> None:
        self.path = None if path is None else Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if self.path is not None and self.path.exists():
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                if doc.get("model") == MODEL_VERSION and isinstance(doc.get("entries"), dict):
                    self.entries = {k: _decode(v) for k, v in doc["entries"].items()}
                else:
                    common.logger.info("lower_bound: ignoring %s (other model version)", self.path.name)
            except (OSError, ValueError, AttributeError) as e:
                common.logger.info("lower_bound: ignoring unreadable %s (%s)", self.path.name, type(e).__name__)

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def put(self, key: str, entry: Mapping[str, Any]) -> None:
        self.entries[key] = dict(entry)
        self.dirty = True

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"model": MODEL_VERSION, "entries": self.entries}
        text = json.dumps(common.to_jsonable(doc), sort_keys=True, indent=1, allow_nan=False)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def cache_path(out_root: Path) -> Path:
    """Where the analyses keep the bound cache: ``<out_root>/tables/cache/lower_bounds.json``."""
    return Path(out_root) / CACHE_RELPATH


def compute_bounds(shape_sets: Iterable[Iterable[Sequence[Any]]], types: Sequence[InstanceType] | None = None,
                   cache: BoundCache | None = None, cpsat: bool = True, save_every: int = 10) -> dict[str, dict]:
    """``bounds`` for every distinct multiset in ``shape_sets``, keyed by ``instance_key``, reusing and filling
    ``cache``. Distinct multisets are solved in key order; the cache is saved every ``save_every`` new CP-SAT solves
    and at the end, so an interrupted run resumes. A cached entry computed without CP-SAT is completed when ``cpsat``
    is asked for and the instance is small enough."""
    types = catalog_types() if types is None else list(types)
    cache = cache if cache is not None else BoundCache(None)
    distinct: dict[str, tuple[Shape, ...]] = {}
    for shapes in shape_sets:
        norm = normalize_shapes(shapes)
        distinct.setdefault(instance_key(norm, types), norm)
    todo = []
    for key in sorted(distinct):
        hit = cache.get(key)
        if hit is None or (cpsat and hit.get("cpsat_status") == "not requested"
                           and hit.get("n_pods", math.inf) <= CPSAT_MAX_PODS):
            todo.append(key)
    n_solves = sum(1 for k in todo if cpsat and 0 < demand(distinct[k])[2] <= CPSAT_MAX_PODS)
    if n_solves:
        common.logger.info("lower_bound: %d multisets (%d cached), %d CP-SAT solves of up to %.0f deterministic s",
                           len(distinct), len(distinct) - len(todo), n_solves,
                           CPSAT_PARAMS["max_deterministic_time"])
    solved = 0
    try:
        for key in todo:
            hit = cache.get(key)
            if hit is not None and hit.get("cpsat_status") == "not requested":
                entry = dict(hit)
                entry.update(cpsat_bound(distinct[key], types))
                entry["bound"] = _best(entry)
            else:
                entry = bounds(distinct[key], types, cpsat=cpsat)
            cache.put(key, entry)
            if entry.get("cpsat_status") not in ("skipped", "not requested", "trivial", "infeasible"):
                solved += 1
                if save_every and solved % save_every == 0:
                    cache.save()
    finally:
        cache.save()
    return {key: cache.get(key) for key in sorted(distinct)}


def main(argv: Sequence[str] | None = None) -> int:
    """Print every bound for one multiset (a JSON list of [cpu_millicores, memory_mib, count]) as JSON."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m paper.analysis.lower_bound '[[cpu_millicores, memory_mib, count], ...]'")
        return 2
    result = bounds(json.loads(args[0]))
    print(json.dumps(common.to_jsonable(result), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
