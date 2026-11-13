"""Independent audit of a cluster plan and the layout-principle checks shared with rows.

``audit_cluster`` reads only ``entities``, ``ports``, ``recipe``,
``reserved_area``, ``nodes`` (declared crafts, a claim it verifies) and catalog
data. ``layout_findings`` is the shape check of ``docs/v2/LAYOUT_PRINCIPLES.md``
for any block plan (cluster or row): no underground/splitter inside the block
(no internal crossing), no belt cycle, no machine with belts on three or more
sides (a loop around a machine), belts per machine slot and machine-tile
density bounded. Thresholds are the module constants below.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..lanes import inserter_output_lane
from .audit import _check_footprints, _check_power, _scene
from .belts import LANES, spread
from .geometry import bounding_box, footprint, step, xy
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, lane_capacity

MAX_BELTS_PER_SLOT = 16  # transport belts per built or reserved machine slot
MIN_DENSITY = 0.12  # machine tiles / bounding-box tiles of the block entities
CROSSING_KINDS = ("underground-belt", "splitter")
SIDES = ((0, -1), (1, 0), (0, 1), (-1, 0))


def _kind(catalog: Any, e: dict) -> str:
    return (catalog.entities.get(e["name"]) or {}).get("type", "")


def layout_findings(catalog: Any, plan: dict, *, slots: int | None = None) -> list[str]:
    """Principle findings for ``plan`` (``[]`` = OK); ``slots`` = built + reserved machine slots."""
    entities = plan["entities"]
    errors = [f"internal_crossing:{e['name']}:{xy(e)}" for e in entities if _kind(catalog, e) in CROSSING_KINDS]
    machines = [e for e in entities if _kind(catalog, e) == "assembling-machine"]
    belts = {xy(e): e for e in entities if _kind(catalog, e) == "transport-belt"}
    slots = max(slots or 0, len(machines)) or 1
    if len(belts) > MAX_BELTS_PER_SLOT * slots:
        errors.append(f"too_many_belts:{len(belts)}>{MAX_BELTS_PER_SLOT}x{slots}")
    tiles = set().union(*(footprint(catalog, e) for e in entities)) if entities else set()
    if machines and tiles:
        (x0, y0), (x1, y1) = bounding_box(tiles)
        # Reserved slots count as machine area: their belts are laid already.
        per = sum(len(footprint(catalog, m)) for m in machines) / len(machines)
        machine_tiles = per * slots
        density = machine_tiles / ((x1 - x0) * (y1 - y0))
        if density < MIN_DENSITY:
            errors.append(f"block_too_sparse:{density:.3f}<{MIN_DENSITY}")
    # Belt cycle: follow each belt's facing.
    for start in belts:
        seen, p = set(), start
        while p in belts and p not in seen:
            seen.add(p)
            p = step(p, belts[p]["direction"])
        if p == start:
            errors.append(f"belt_cycle:{start}")
            break
    # A belt within two tiles of a machine on three or four sides wraps it.
    for m in machines:
        mt = footprint(catalog, m)
        xs, ys = [t[0] for t in mt], [t[1] for t in mt]
        sides = 0
        for dx, dy in SIDES:
            band = set()
            for t in mt:
                for d in (1, 2, 3):
                    q = (t[0] + dx * d, t[1] + dy * d)
                    if min(xs) <= q[0] <= max(xs) or min(ys) <= q[1] <= max(ys):
                        band.add(q)
            if band & set(belts):
                sides += 1
        if sides >= 3:
            errors.append(f"belt_wraps_machine:{xy(m)}")
    return sorted(set(errors))


def _seed(s, ports) -> dict:
    lanes = defaultdict(set)
    for port in ports:
        if port.get("direction") != "input":
            continue
        for lane in LANES:  # a feeder carries its item on both lanes
            lanes[xy(port), lane].add(port["item"])
    spread(lanes, s.graph)
    return lanes


def audit_cluster(catalog: Any, plan: dict, *, limits: RowLimits = DEFAULT_LIMITS) -> list[str]:
    """Sorted finding codes; ``[]`` = OK. See the module docstring."""
    s, errors = _scene(catalog, plan, limits)
    errors += _check_footprints(s)
    errors += _check_power(s)
    lanes = _seed(s, plan["ports"])
    declared = {n["recipe"]: float(n["crafts_needed"]) for n in plan.get("nodes", [])}
    made_by = {}
    for m in s.machines:
        for p in s.recipe(m).get("products", ()):
            made_by[id(m)] = p["name"]
    product = plan.get("product")
    sinks = [p for p in plan["ports"] if p.get("direction") == "output"]
    for m in s.machines:
        recipe = s.recipe(m)
        if not recipe:
            errors.append(f"machine_cannot_craft:{xy(m)}")
            continue
        crafts = declared.get(m.get("recipe"), 0.0)
        if crafts > s.nameplate(m) + 1e-9:
            errors.append(f"declared_rate_above_nameplate:{m.get('recipe')}")
        needed = {r["name"]: float(r["amount"]) for r in recipe.get("ingredients", ())}
        supply = defaultdict(float)
        for arm in s.arms:
            if s.machine_at(arm.drop) is not m:
                continue
            rate = inserter_rate(catalog, arm.entity["name"], limits)
            source = s.machine_at(arm.pickup)
            if source is not None:
                item = made_by.get(id(source))
                if item not in needed:
                    errors.append(f"direct_insertion_item_unused:{item}->{m.get('recipe')}")
                    continue
                supply[frozenset([item])] += rate
            elif arm.pickup in s.belts:
                got = (lanes[arm.pickup, "left"] | lanes[arm.pickup, "right"]) & set(needed)
                if got:
                    supply[frozenset(got)] += rate
            else:
                errors.append(f"inserter_pickup_not_belt_or_machine:{xy(arm.entity)}")
        reached = set().union(*supply) if supply else set()
        errors += [f"ingredient_unreachable:{m.get('recipe')}:{i}" for i in sorted(set(needed) - reached)]
        for items, rate in supply.items():
            if sum(needed[i] for i in items) * crafts > rate + 1e-9:
                errors.append(f"inserter_throughput_insufficient:{m.get('recipe')}:{','.join(sorted(items))}")
        outs = [a for a in s.arms if s.machine_at(a.pickup) is m]
        if not outs:
            errors.append(f"machine_output_unused:{m.get('recipe')}")
        if m.get("recipe") == plan.get("recipe"):
            reach = False
            for a in outs:
                if any(xy(port) == a.drop for port in sinks):
                    reach = True
                    port = next(p for p in sinks if xy(p) == a.drop)
                    belt = s.belts.get(a.drop)
                    if (
                        belt is not None
                        and port.get("lane")
                        and port["lane"] != inserter_output_lane(a.entity["direction"], belt["direction"])
                    ):
                        errors.append("output_lane_mismatch")
            if not reach:
                errors.append("product_not_at_output_port")
    # Lane purity: each belt lane carries at most one item; inputs never reach the output.
    for (tile, lane), items in lanes.items():
        if len(items) > 1:
            errors.append(f"lane_mixed:{tile}:{lane}:{','.join(sorted(items))}")
    for port in sinks:
        if lanes[xy(port), "left"] | lanes[xy(port), "right"]:
            errors.append("input_items_reach_output")
        if port.get("item") != product:
            errors.append("output_port_wrong_item")
    # Lane capacity against the declared sourced demand.
    demand = defaultdict(float)
    for m in s.machines:
        crafts = declared.get(m.get("recipe"), 0.0)
        for r in s.recipe(m).get("ingredients", ()):
            demand[r["name"]] += crafts * float(r["amount"])
    for port in plan["ports"]:
        if port.get("direction") == "input":
            belt = s.belts.get(xy(port))
            cap = lane_capacity(catalog, belt["name"], limits) * (1 if port.get("lane") else 2) if belt else 0
            if demand[port["item"]] > cap + 1e-9:
                errors.append(f"lane_capacity_exceeded:{port['item']}")
    errors += layout_findings(catalog, plan)
    return sorted(set(errors))
