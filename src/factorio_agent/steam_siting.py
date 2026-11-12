"""Live checks for the next steam pair: adjacent bank slots first, then a free site.

Rules:

* Known hardware comes from the builder's saved first-plant plans and every
  registered board block; a slot overlapping any of it, or any board
  reservation, is never offered.
* Every offered stub variant passed ``planning.preflight`` on the live map:
  trees and rocks are cleared, ore, water and other entities block.
* The two adjacent slots (either end of the existing row) are tried before
  any free site. A free site is a degraded layout: it gets its own short
  surface water run from an old boiler's free water connection, audited
  for fluid mixing and network connection like the adjacent bridge.
"""

from __future__ import annotations

import math

from .board import entity
from .core.factory_templates import COAL_ENTRIES
from .planning import overlap, preflight
from .site.candidates import free_sites
from .site.planmodel import SiteError
from .steam_layout import (
    adjacent_layouts,
    bank_at,
    plant_frame,
    reserved_area,
    tiles,
    variant,
    water_tiles,
)

MAX_WATER_RUN = 32  # tiles of surface pipe a degraded free-site layout may add
FALLBACK_SITES = 4


def known_rows(builder, board: dict) -> list[dict]:
    """Planned rows of the first plant, its coal feed and every registered block."""
    state = getattr(builder, "state", {}) or {}
    rows = []
    for key in ("power_plan", "coal_plan"):
        rows += state.get(key, {}).get("entities", [])
    for block in board.get("blocks", {}).values():
        rows += block.get("entities", [])
    unique = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in rows}
    return list(unique.values())


def board_areas(board: dict) -> list:
    return [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]


def _clear_of_board(rows: list[dict], areas: list) -> bool:
    area = reserved_area(rows)
    return not any(overlap(area, other) for other in areas)


def _live(game, builder, obs, layout: dict, areas: list) -> dict | None:
    """Keep the stub variants that are clear of the board and pass live preflight."""
    kept = []
    for found in layout["variants"]:
        if not _clear_of_board(found["entities"], areas):
            continue
        check = preflight(game, builder, obs, found["entities"])
        if check.get("ok"):
            kept.append(dict(found, clear_obstacles=check["clear_obstacles"]))
    return dict(layout, variants=kept) if kept else None


def _coal_origin(obs: dict) -> dict | None:
    known = obs.get("resources", {}).get("coal", {}).get("position")
    if isinstance(known, dict) and "x" in known and "y" in known:
        return known
    return None


def _side_order(layouts: list[dict], origin: dict | None) -> list[dict]:
    """Prefer the end of the row nearer the coal patch (shorter paired feed)."""
    if origin is None:
        return layouts

    def distance(layout):
        boiler = layout["variants"][0]["boiler"]["position"]
        return math.dist((boiler["x"], boiler["y"]), (origin["x"], origin["y"]))

    return sorted(layouts, key=distance)


def _water_run(builder, old_boilers, existing, banks, water_port) -> tuple[dict | None, list[dict]]:
    """Shortest surface pipe from a free old-boiler water tile to ``water_port``."""
    taken = tiles(existing)
    reserved = existing + [e for bank in banks for e in bank["entities"]]
    best = None
    for old in old_boilers:
        for tx, ty in sorted(water_tiles(old) - taken):
            source = {"x": tx + 0.5, "y": ty + 0.5}
            route = builder.route(source, water_port["position"], "pipe", reserved, margin=8)
            if not route.get("ok") or len(route["path"]) > MAX_WATER_RUN:
                continue
            if best is None or len(route["path"]) < len(best[1]):
                best = (old, route["path"])
    if best is None:
        return None, []
    return best[0], [entity("pipe", p["x"], p["y"]) for p in best[1]]


def fallback_layouts(builder, obs, site, rotation: int, old_boilers, existing) -> list[dict]:
    """Degraded layouts: one bank of the same template on a free site near the plant."""
    if site is None:
        return []
    probe = {"rotation": rotation, "anchor": {"x": 0.5, "y": 0.5}}
    union = [e for entry in COAL_ENTRIES for e in bank_at(probe, 0, entry)["entities"]]
    (x0, y0), (x1, y1) = reserved_area(union)
    around = [(b["position"]["x"], b["position"]["y"]) for b in old_boilers]
    try:
        world = site.world(obs["world_id"])
        rects = free_sites(world, around, x1 - x0, y1 - y0, step=4, limit=FALLBACK_SITES, margin=1)
    except (LookupError, SiteError, ValueError):
        return []
    layouts = []
    for rect in rects:
        frame = {"rotation": rotation, "anchor": {"x": 0.5 + rect.x0 - x0, "y": 0.5 + rect.y0 - y0}}
        banks = [dict(bank_at(frame, 0, entry), coal_entry=entry) for entry in COAL_ENTRIES]
        banks = [b for b in banks if b.get("ok")]
        if not banks:
            continue
        water = next(p for p in banks[0]["ports"] if p["item"] == "water")
        old, pipes = _water_run(builder, old_boilers, existing, banks, water)
        if old is None:
            continue
        variants = [found for bank in banks if (found := variant(bank, pipes, old, existing))]
        if variants:
            layouts.append(
                {
                    "kind": "degraded_free_site",
                    "site": rect.to_list(),
                    "water_run_tiles": len(pipes),
                    "old_boiler": old,
                    "rotation": rotation,
                    "variants": variants,
                }
            )
    return layouts


def candidate_layouts(game, builder, obs, site, board: dict):
    """Yield live-checked layouts: adjacent banks (nearer coal first), then free sites."""
    power = (getattr(builder, "state", {}) or {}).get("power_plan") or {}
    frame = plant_frame(power)
    old_boilers = [e for e in power.get("entities", []) if e["name"] == "boiler"]
    if not old_boilers:
        return
    existing = known_rows(builder, board)
    areas = board_areas(board)
    origin = _coal_origin(obs)
    adjacent = adjacent_layouts(frame, power, existing) if frame else []
    live = [kept for layout in adjacent if (kept := _live(game, builder, obs, layout, areas))]
    yield from _side_order(live, origin)
    rotation = frame["rotation"] if frame else old_boilers[0].get("direction", 0) % 16
    for layout in fallback_layouts(builder, obs, site, rotation, old_boilers, existing):
        kept = _live(game, builder, obs, layout, areas)
        if kept:
            yield kept
