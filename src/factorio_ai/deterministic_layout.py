"""Bounded production siting with room between extraction and the factory."""
from __future__ import annotations

from copy import deepcopy
import json
import math


RESOURCE_MARGIN = 4
BLOCK_AISLE = 2
ANCHOR_RADIUS = 12
SEARCH_RADIUS = 64
PRODUCTION_TYPES = {"assembling-machine", "furnace", "lab", "rocket-silo", "storage-tank", "container"}
PRODUCTION_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
                    "stone-furnace", "steel-furnace", "electric-furnace", "lab", "rocket-silo",
                    "chemical-plant", "oil-refinery", "storage-tank", "wooden-chest", "iron-chest", "steel-chest"}


def production_plan(factory, plan: dict) -> bool:
    """Direct-feed mining cells retain their resource-dependent placement."""
    if plan.get("resource_cell"):
        return False
    return any(e["name"] in PRODUCTION_NAMES
               or factory.catalog.entities.get(e["name"], {}).get("type") in PRODUCTION_TYPES
               for e in plan.get("entities", []))


def bounds(tiles: set[tuple[float, float]], margin: int = 0) -> list[list[float]]:
    return [[min(x for x, _ in tiles) - .5 - margin, min(y for _, y in tiles) - .5 - margin],
            [max(x for x, _ in tiles) + .5 + margin, max(y for _, y in tiles) + .5 + margin]]


def reserved_aisles(factory) -> set[tuple[float, float]]:
    """Leave two tiles around new blocks; belts and pipes may route through them."""
    result = set()
    for plan in factory.state.get("blocks", {}).values():
        area = plan.get("production_area")
        if area:
            result.update((x + .5, y + .5)
                          for x in range(math.floor(area[0][0]), math.ceil(area[1][0]))
                          for y in range(math.floor(area[0][1]), math.ceil(area[1][1])))
    return result


def _translated(origin: dict, offset: dict) -> dict:
    plan = deepcopy(origin)
    for obj in plan["entities"] + plan.get("ports", []):
        obj["position"] = {axis: obj["position"][axis] + offset[axis] for axis in ("x", "y")}
    return plan


def _survey(factory, candidates: list[dict]) -> dict:
    payload = json.dumps(json.dumps([candidate["area"] for candidate in candidates], separators=(",", ":")))
    result = factory.game.query('''
local areas=helpers.json_to_table(''' + payload + ''');local clear={}
for i,area in ipairs(areas) do
 local resources=s.count_entities_filtered{area=area,type="resource"}
 local drills=s.count_entities_filtered{area=area,type="mining-drill"}
 local water=s.count_tiles_filtered{area=area,name={"water","deepwater","water-green","deepwater-green","out-of-map"}}
 if resources==0 and drills==0 and water==0 then clear[#clear+1]=i end
end
return {ok=true,clear=clear}
''')
    # Factorio serializes an empty Lua array as an object.
    if result.get("clear") == {}:
        result["clear"] = []
    return result


def _existing_anchor(factory, reference: dict) -> dict:
    # An existing clean assembly/research district is a better starting point
    # than another ore-adjacent input source. Never adopt unverified positions.
    positions = []
    for plan in factory.state.get("blocks", {}).values():
        if plan.get("resource_cell") or plan.get("retired_for_upgrade"):
            continue
        for entity in plan.get("entities", []):
            name = entity["name"]
            if (name in {"lab", "chemical-plant", "oil-refinery", "rocket-silo"}
                    or name.startswith("assembling-machine-")):
                positions.append(entity["position"])
    positions.sort(key=lambda p: (abs(p["x"] - reference["x"]) + abs(p["y"] - reference["y"]), p["x"], p["y"]))
    candidates = []
    for position in positions[:24]:
        offset = {axis: math.floor(position[axis]) for axis in ("x", "y")}
        candidates.append({"offset": offset, "area": [
            [offset["x"] - ANCHOR_RADIUS, offset["y"] - ANCHOR_RADIUS],
            [offset["x"] + ANCHOR_RADIUS, offset["y"] + ANCHOR_RADIUS]]})
    if not candidates:
        return {"ok": True}
    survey = _survey(factory, candidates)
    if not survey.get("ok") or not isinstance(survey.get("clear"), list):
        return {"ok": False, "reason": "existing production area survey failed"}
    clear = set(survey["clear"])
    return next(({"ok": True, "anchor": candidate["offset"]}
                 for index, candidate in enumerate(candidates, 1) if index in clear), {"ok": True})


def reserve_production_site(factory, origin: dict, key: str, reference: dict,
                            occupied: set[tuple[float, float]]) -> dict:
    """Persist the first clear anchor, then grow near it with source-aware ties.

    The search is a local 128-tile square, never a bounding rectangle around all
    ore patches. Each block keeps its own resource buffer and transport aisle.
    Existing owned plans are returned by the caller before this policy runs.
    """
    layout = factory.state.get("production_layout")
    if not layout:
        existing = _existing_anchor(factory, reference)
        if not existing.get("ok"):
            return existing
        if existing.get("anchor") is not None:
            layout = {"anchor": existing["anchor"]}
    anchor = layout["anchor"] if layout else {axis: math.floor(reference[axis]) for axis in ("x", "y")}
    offsets = [(x, y) for x in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1, 8)
               for y in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1, 8)]
    # Manhattan transport cost is secondary to proximity to the stable factory
    # anchor. Even distant oil inputs cannot drag every downstream site away.
    offsets.sort(key=lambda p: (abs(p[0]) + abs(p[1]) + .25 * (
        abs(anchor["x"] + p[0] - reference["x"]) + abs(anchor["y"] + p[1] - reference["y"])),
        p[0] * p[0] + p[1] * p[1], p))
    candidates = []
    for dx, dy in offsets:
        offset = {"x": anchor["x"] + dx, "y": anchor["y"] + dy}
        plan = _translated(origin, offset)
        footprint = factory.builder._occupied_by_plan(plan["entities"])
        if not footprint or footprint & occupied:
            continue
        area = bounds(footprint, RESOURCE_MARGIN)
        if not layout:
            # The first block must leave enough dry, resource-free room for a
            # neighbor; a thin strip at a patch edge is not a factory anchor.
            for index, sign in ((0, -1), (1, 1)):
                combine = min if index == 0 else max
                area[index] = [combine(area[index][i], offset[axis] + sign * ANCHOR_RADIUS)
                               for i, axis in enumerate(("x", "y"))]
        candidates.append({"plan": plan, "offset": offset, "area": area,
                           "production_area": bounds(footprint, BLOCK_AISLE)})
    for start in range(0, len(candidates), 24):
        batch = candidates[start:start + 24]
        survey = _survey(factory, batch)
        if not survey.get("ok") or not isinstance(survey.get("clear"), list):
            return {"ok": False, "reason": "production area resource/terrain survey failed", "key": key,
                    "error": survey.get("reason")}
        clear = set(survey["clear"])
        for index, candidate in enumerate(batch, 1):
            if index not in clear or not factory.builder.can_place(candidate["plan"]["entities"]).get("ok"):
                continue
            plan = candidate["plan"]
            plan.update(key=key, production_area=candidate["production_area"])
            if not factory.state.get("production_layout"):
                factory.state["production_layout"] = {"anchor": layout["anchor"] if layout else candidate["offset"],
                                                      "resource_margin": RESOURCE_MARGIN, "block_aisle": BLOCK_AISLE}
            factory.state["blocks"][key] = plan
            factory._save()
            return plan
    return {"ok": False, "reason": "no clear production area within bounded factory search", "key": key}
