"""Upcoming hand-craft demand for the supply cell: finished items and the intermediates inside them.

Why: the first supply cell priced only ``Step.builds`` constants, which
under-counted what was really hand crafted (a 50-minute run crafted 668 belts,
112 poles, 64 inserters) and never counted gears or circuits, because those are
consumed inside other crafts. A player stocks the intermediates: with gears and
circuits in hand, a drill or a lab is a few seconds of final assembly.

Rules (``docs/v2/PLANNERS.md``, "Supply cell"):

1. Builds (``upcoming_builds``): the next ``horizon`` unsatisfied roadmap steps
   (``progression._upcoming``). A step whose job already saved its plan
   (``MEASURED_PLANS``: the capability's state file, ``plan.required_items``)
   counts what that plan still lacks on the map; any other step counts its
   ``Step.builds`` estimate. Raw items and items without a recipe are dropped.
   Each row is marked ``current`` when its step has a job in flight.
2. Content (``contained``): the units of one item inside a bill, by recursive
   expansion of catalog recipes that stops at raw items and at the item
   itself. ``item_demand`` applies it to every non-raw item that a bill
   reaches, so gears count the gears inside belts, inserters, labs and drills.

Science packs are not counted: after automation they come from the startup
science cell, which makes its own gears.
"""

from __future__ import annotations

import json
from pathlib import Path

# capability -> state file whose ``plan`` (``core.deterministic_builder._plan``) lists the real hardware
MEASURED_PLANS = {
    "smelting_district": "smelting-district.json",
    "automation_science": "automation-science.json",
    "planned_main_bus": "planned-main-bus.json",
}
MAX_DEPTH = 24  # a recipe cycle stops here (as ``progression.raw_cost``)


def _raw_items():
    from .progression import RAW_ITEMS

    return RAW_ITEMS


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def measured_remainder(root, capability, observation):
    """``{item: count}`` the saved plan of ``capability`` still lacks on the map, or None without a plan.

    The plan must belong to the observed world. Entities of the plan that the
    observation shows at their planned position (same name) count as built.
    """
    name = MEASURED_PLANS.get(capability)
    if root is None or name is None:
        return None
    path = Path(root) / name
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = state.get("plan") if isinstance(state, dict) else None
    if state.get("world_id") != (observation or {}).get("world_id") or not isinstance(plan, dict):
        return None
    required = plan.get("required_items")
    if not isinstance(required, dict):
        return None
    seen = {
        (e.get("name"), (e.get("position") or {}).get("x"), (e.get("position") or {}).get("y"))
        for e in (observation or {}).get("entities", [])
    }
    built = {}
    for e in plan.get("entities") or ():
        key = (e.get("name"), (e.get("position") or {}).get("x"), (e.get("position") or {}).get("y"))
        if key in seen:
            built[e["name"]] = built.get(e["name"], 0) + 1
    return {item: max(0, n - built.get(item, 0)) for item, n in required.items() if _number(n)}


def upcoming_builds(catalog, jobs, observation, *, horizon, root=None):
    """``{item: {"count": n, "current": n of steps in flight, "measured": bool}}`` for the next steps."""
    from .progression import _names, _Progress, _upcoming

    from .early_start import is_early, succeeded_capabilities
    from .jobs import CAPABILITIES

    jobs = tuple(jobs)
    progress = _Progress(jobs, frozenset(_names((observation or {}).get("technologies"))))
    succeeded = succeeded_capabilities(jobs)
    rows = {}
    for step in _upcoming(jobs, progress.researched, horizon):
        measured = measured_remainder(root, step.capability, observation) if progress.in_flight(step) else None
        bill = measured if measured is not None else (step.builds or {})
        # An early-start job (early_start.py) is not current: contested plates never feed its demand.
        current = progress.in_flight(step) and not is_early(CAPABILITIES, step.capability, succeeded)
        for item, count in bill.items():
            if item in _raw_items() or not _number(count) or count <= 0 or not catalog.recipe_for_product(item):
                continue
            row = rows.setdefault(item, {"count": 0.0, "current": 0.0, "measured": False})
            row["count"] += float(count)
            row["current"] += float(count) if current else 0.0
            row["measured"] = row["measured"] or measured is not None
    return rows


def contained(catalog, bill, item):
    """Units of ``item`` inside ``bill`` (``{name: count}``), the bill's own ``item`` rows included."""
    from .progression import _product_amount

    total = 0.0

    def expand(name, count, depth):
        nonlocal total
        if name == item:
            total += count
            return
        if name in _raw_items() or depth > MAX_DEPTH or count <= 0:
            return
        recipe = catalog.recipe_for_product(name)
        if not recipe:
            return
        crafts = count / _product_amount(recipe, name)
        for row in recipe.get("ingredients") or ():
            if row.get("type", "item") == "item" and _number(row.get("amount")):
                expand(row["name"], row["amount"] * crafts, depth + 1)

    for name, count in bill.items():
        if _number(count):
            expand(name, float(count), 0)
    return total


def reachable(catalog, bill):
    """Every non-raw item the bill's recipes reach (the bill's items and all intermediates), sorted."""
    seen = set()

    def walk(name, depth):
        if name in seen or name in _raw_items() or depth > MAX_DEPTH:
            return
        recipe = catalog.recipe_for_product(name)
        if not recipe:
            return
        seen.add(name)
        for row in recipe.get("ingredients") or ():
            if row.get("type", "item") == "item":
                walk(row["name"], depth + 1)

    for name in bill:
        walk(name, 0)
    return sorted(seen)


def direct_ingredients(catalog, names):
    """Items that a recipe of ``names`` takes directly (what a hand craft of them could use from stock)."""
    found = set()
    for name in names:
        recipe = catalog.recipe_for_product(name)
        for row in (recipe or {}).get("ingredients") or ():
            if row.get("type", "item") == "item":
                found.add(row["name"])
    return found


def item_demand(catalog, jobs, observation, *, horizon, root=None):
    """``{item: {"count", "current", "direct", "direct_use"}}``: every reachable item's content in the upcoming builds.

    ``count`` sums all upcoming steps, ``current`` only the steps in flight,
    ``direct`` the units the steps place as items themselves, ``direct_use``
    whether the item is placed or is a direct ingredient of a placed item (a
    deeper intermediate, like cable when only circuits use it, is never
    collected for a hand craft and so is no product on its own).
    """
    builds = upcoming_builds(catalog, jobs, observation, horizon=horizon, root=root)
    total = {name: row["count"] for name, row in builds.items()}
    current = {name: row["current"] for name, row in builds.items()}
    used = set(total) | direct_ingredients(catalog, total)
    demand = {}
    for name in reachable(catalog, total):
        demand[name] = {
            "count": round(contained(catalog, total, name), 3),
            "current": round(contained(catalog, current, name), 3),
            "direct": round(total.get(name, 0.0), 3),
            "direct_use": name in used,
        }
    return demand
