"""Select paid input bypasses from requested science demand and saved ownership.

Demand is a construction trigger, never evidence of actual transport capacity.
Only explicit tap ancestry and complete collinear paths are considered here.
"""
from __future__ import annotations

from collections import Counter
import math

from .deterministic_input_links import _geometry, _identity, _path, _point
from .deterministic_underground_bypass import propose_collinear_bypasses
from .factory_templates import DIRECTIONS


LONG_ARM_TRIGGER_PER_MINUTE = 60.0


def _segments(plan):
    belts, edges, _ = _geometry(plan)
    source, consumer = plan.get("source_port"), plan.get("consumer_port")
    if not source or not consumer:
        return []
    path = _path(belts, edges, _point(source), _point(consumer))
    if not path:
        return []
    runs, run = [], []
    direction = None
    for row in path:
        facing = row.get("direction")
        travel = (facing + 8) % 16 if row["name"] == "long-handed-inserter" else facing
        supported = row["name"] in {"transport-belt", "long-handed-inserter"} and travel in DIRECTIONS
        same_axis = False
        if supported and run and travel == direction:
            dx, dy = DIRECTIONS[direction]
            x, y = _point(row); px, py = _point(run[-1])
            same_axis = (x-px)*dy == (y-py)*dx and (x-px)*dx+(y-py)*dy > 0
        if run and not same_axis:
            runs.append(run)
            run = []
        if supported:
            run.append(row)
            direction = travel
    if run:
        runs.append(run)
    candidates = []
    for run in runs:
        arms = [i for i, row in enumerate(run) if row["name"] == "long-handed-inserter"]
        if not arms:
            continue
        # Maximal chains first; individual crossings remain alternatives when a
        # tap protects the middle of a longer chain.
        spans = [(arms[0], arms[-1]), *((i, i) for i in arms)]
        for first, last in spans:
            if first < 2 or last + 2 >= len(run):
                continue
            segment = run[first-2:last+3]
            if len(segment) <= 128 and segment not in candidates:
                candidates.append(segment)
    return candidates[:16]


def _input_demands(state, catalog, cycles):
    cells = []
    for key, block in state.get("blocks", {}).items():
        if block.get("retired_for_upgrade"):
            continue
        recipes = {e.get("recipe") for e in block.get("entities", []) if e.get("recipe")}
        if len(recipes) == 1:
            cells.append((key, block, recipes.pop()))
    counts = Counter(recipe for _, _, recipe in cells)
    result = {}
    for key, link in state.get("links", {}).items():
        port = link.get("consumer_port")
        owners = [(block, recipe) for _, block, recipe in cells if port in block.get("ports", [])]
        if len(owners) != 1 or not port or port.get("kind") != "item":
            continue
        _, recipe = owners[0]
        amounts = [row["amount"] for row in catalog.recipes.get(recipe, {}).get("ingredients", [])
                   if row.get("type", "item") == "item" and row["name"] == port.get("item")]
        rate = cycles.get(recipe, 0)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (rate, *amounts)):
            continue
        demand = rate * sum(amounts) / counts[recipe]
        if math.isfinite(demand) and demand > 0:
            result[key] = demand
    return result


def _segment_demand(links, owner_key, segment, demands):
    owner = links[owner_key]
    belts, edges, _ = _geometry(owner)
    source = owner["source_port"]
    needed = {_identity(row) for row in segment}
    total = 0.0
    for key, demand in demands.items():
        if demand <= 0:
            continue
        cursor, target, seen = key, None, set()
        while cursor in links and cursor not in seen and len(seen) < 32:
            seen.add(cursor)
            plan = links[cursor]
            if plan.get("source_port") != source:
                break
            if cursor == owner_key:
                target = target or plan.get("consumer_port")
                path = _path(belts, edges, _point(source), _point(target)) if target else None
                if path and needed <= {_identity(row) for row in path}:
                    total += demand
                break
            tap = plan.get("upstream_tap") or {}
            target, cursor = tap.get("belt"), tap.get("link_key")
            parent = links.get(cursor, {})
            if (not target or target.get("name") != "transport-belt"
                    or _identity(target) not in {_identity(row) for row in parent.get("entities", [])
                                                  if row.get("name") == "transport-belt"}):
                break
    return total


def _needed_segments(state, catalog, cycles):
    links = state.get("links", {})
    demands = _input_demands(state, catalog, cycles)
    for key, plan in sorted(links.items()):
        if key in state.get("input_bypasses", {}):
            continue  # Preserve this route's published receipt for save rollback.
        try:
            segments = _segments(plan)
            others = [other for category in ("blocks", "links", "power_links", "source_upgrades")
                      for other_key, other in state.get(category, {}).items()
                      if category != "links" or other_key != key]
            for segment in segments:
                rate = _segment_demand(links, key, segment, demands)
                if rate <= LONG_ARM_TRIGGER_PER_MINUTE:
                    continue
                yield key, plan, segment, others, rate
        except (KeyError, TypeError, ValueError, OverflowError):
            continue  # Unsupported legacy geometry retains all of its assets.


def input_bypass_candidates(state, catalog, cycles, *, max_distance):
    """Pure bounded selection; returned proposals still require live preflight."""
    result = []
    for key, plan, segment, others, rate in _needed_segments(state, catalog, cycles):
        for proposal in propose_collinear_bypasses(plan, segment, max_distance=max_distance, other_plans=others):
            result.append((key, proposal, rate))
            if len(result) == 32:
                return result
    return result


def maybe_upgrade_input_routes(factory, observation):
    from .deterministic_input_bypass import resume_input_bypass, start_input_bypass
    pending = resume_input_bypass(factory, observation, critical_only=False)
    if pending is not None:
        return pending
    if getattr(factory.game, "backend", None) != "assisted":
        return None
    enabled = observation.get("enabled_recipes") or {}
    recipe = factory.catalog.recipe_for_product("underground-belt")
    if recipe is None or not enabled.get(recipe["name"]):
        return None
    sciences = [name for name in factory.graph.for_first_rocket()["bom"]["science_packs"]
                if (row := factory.catalog.recipe_for_product(name)) and enabled.get(row["name"])]
    if not sciences:
        return None
    cycles, _ = factory.graph._continuous_rates(sciences)
    # Establish demand before querying the engine on production decisions.
    if next(_needed_segments(factory.state, factory.catalog, cycles), None) is None:
        return None
    limit = factory.game.query('''
local proto=prototypes.entity["underground-belt"]
return {ok=proto~=nil,max_distance=proto and proto.max_underground_distance}
''')
    distance = limit.get("max_distance")
    if not limit.get("ok") or type(distance) is not int or distance < 1:
        return {"status": "blocked", "reason": "underground bypass requires the current prototype range", "evidence": {}}
    clearances = factory._port_clearances()
    for key, proposal, rate in input_bypass_candidates(factory.state, factory.catalog, cycles, max_distance=distance):
        pieces = proposal["new_entities"]
        occupied = factory.builder._occupied_by_plan(factory._reserved()) | clearances
        if factory.builder._occupied_by_plan(pieces) & occupied:
            continue
        if not factory.builder.can_place(pieces).get("ok"):
            continue
        return start_input_bypass(factory, key, proposal, observation)
    return None
