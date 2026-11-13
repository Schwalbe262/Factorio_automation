"""Live checks for the next steam pair: adjacent bank slots first, then a free site.

Rules:

* Known hardware comes from the builder's saved first-plant plans and every
  registered board block; a slot overlapping any of it, or any board
  reservation, is never offered.
* Every offered stub variant passed ``planning.preflight`` on the live map:
  trees and rocks are cleared, ore, water and other entities block.
* The two adjacent slots (either end of the existing row) are tried before
  any free site. A free site is a degraded layout: it gets its own short
  surface water run from the old water network (an old boiler's free water
  connection or a free tile beside one of its surface pipes, the nearest
  ``WATER_SOURCES`` to the new inlet), audited for fluid mixing and network
  connection like the adjacent bridge. ``MAX_WATER_RUN`` / ``FALLBACK_SITES``
  widen under the ``site:wider_steam_search`` remedy up to their hard maxima.
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
# Hard maxima of the ``site:wider_steam_search`` remedy (remedy.RELAXATIONS["steam_expansion"]).
MAX_WATER_RUN_LIMIT = 64
FALLBACK_SITES_LIMIT = 12


def _limits() -> tuple[int, int]:
    """Water run and free-site count under the active remedy, capped at the hard maxima."""
    from . import remedy

    run = remedy.option("steam_water_run_max", MAX_WATER_RUN)
    sites = remedy.option("steam_fallback_sites", FALLBACK_SITES)
    run = min(int(run), MAX_WATER_RUN_LIMIT) if isinstance(run, int) and run > 0 else MAX_WATER_RUN
    sites = min(int(sites), FALLBACK_SITES_LIMIT) if isinstance(sites, int) and sites > 0 else FALLBACK_SITES
    return run, sites


def _reject(rejected, reason: str) -> None:
    if rejected is not None:
        rejected[reason] = rejected.get(reason, 0) + 1


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


def _live(game, builder, obs, layout: dict, areas: list, rejected=None) -> dict | None:
    """Keep the stub variants that are clear of the board and pass live preflight."""
    kept = []
    for found in layout["variants"]:
        if not _clear_of_board(found["entities"], areas):
            _reject(rejected, f"{layout['kind']}:board_reservation_overlap")
            continue
        check = preflight(game, builder, obs, found["entities"])
        if check.get("ok"):
            kept.append(dict(found, clear_obstacles=check["clear_obstacles"]))
        else:
            _reject(rejected, f"{layout['kind']}:preflight:{check.get('reason', 'failed')}"[:80])
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


WATER_SOURCES = 6  # nearest water taps tried per free site (each is one route query)


def water_sources(old_boilers, existing) -> list[tuple[dict, tuple[int, int]]]:
    """Free tiles where a new pipe joins an old boiler's water network.

    An old boiler's free water connection, and (seed 1277653921: the plant's
    only free water connection faced its own coal feed) every free tile beside
    a surface pipe of that network, e.g. the pump-to-boiler pipe: a pipe there
    joins the network. Fluid mixing and foreign contacts are audited by
    ``steam_layout.variant`` afterwards.
    """
    from .steam_layout import water_network

    taken = tiles(existing)
    surface_pipes = {next(iter(tiles([e]))) for e in existing if e["name"] == "pipe"}
    found, seen = [], set()
    for old in old_boilers:
        rows = [(old, t) for t in sorted(water_tiles(old) - taken)]
        network, _ = water_network(existing, old)
        for tx, ty in sorted(network & surface_pipes):
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                t = (tx + dx, ty + dy)
                if t not in taken:
                    rows.append((old, t))
        for old_boiler, t in rows:
            if t not in seen:
                seen.add(t)
                found.append((old_boiler, t))
    return found


def _water_run(builder, old_boilers, existing, banks, water_port, max_run=MAX_WATER_RUN):
    """Shortest surface pipe (``(old boiler, pipes)``) from the old water network to ``water_port``."""
    reserved = existing + [e for bank in banks for e in bank["entities"]]
    port = water_port["position"]
    sources = sorted(
        water_sources(old_boilers, existing),
        key=lambda row: math.dist((row[1][0] + 0.5, row[1][1] + 0.5), (port["x"], port["y"])),
    )[:WATER_SOURCES]
    best = None
    for old, (tx, ty) in sources:
        source = {"x": tx + 0.5, "y": ty + 0.5}
        route = builder.route(source, port, "pipe", reserved, margin=8)
        if not route.get("ok") or len(route["path"]) > max_run:
            continue
        if best is None or len(route["path"]) < len(best[1]):
            best = (old, route["path"])
    if best is None:
        return None, []
    return best[0], [entity("pipe", p["x"], p["y"]) for p in best[1]]


def fallback_layouts(builder, obs, site, rotation: int, old_boilers, existing, rejected=None) -> list[dict]:
    """Degraded layouts: one bank of the same template on a free site near the plant."""
    if site is None:
        _reject(rejected, "degraded_free_site:no_site_service")
        return []
    max_run, limit = _limits()
    probe = {"rotation": rotation, "anchor": {"x": 0.5, "y": 0.5}}
    union = [e for entry in COAL_ENTRIES for e in bank_at(probe, 0, entry)["entities"]]
    (x0, y0), (x1, y1) = reserved_area(union)
    around = [(b["position"]["x"], b["position"]["y"]) for b in old_boilers]
    try:
        world = site.world(obs["world_id"])
        rects = free_sites(world, around, x1 - x0, y1 - y0, step=4, limit=limit, margin=1)
    except (LookupError, SiteError, ValueError) as exc:
        _reject(rejected, f"degraded_free_site:site_error:{type(exc).__name__}")
        return []
    if not rects:
        _reject(rejected, "degraded_free_site:no_free_site")
    layouts = []
    for rect in rects:
        frame = {"rotation": rotation, "anchor": {"x": 0.5 + rect.x0 - x0, "y": 0.5 + rect.y0 - y0}}
        banks = [dict(bank_at(frame, 0, entry), coal_entry=entry) for entry in COAL_ENTRIES]
        banks = [b for b in banks if b.get("ok")]
        if not banks:
            _reject(rejected, "degraded_free_site:template_failed")
            continue
        water = next(p for p in banks[0]["ports"] if p["item"] == "water")
        old, pipes = _water_run(builder, old_boilers, existing, banks, water, max_run)
        if old is None:
            _reject(rejected, f"degraded_free_site:no_water_run_within_{max_run}")
            continue
        variants = [found for bank in banks if (found := variant(bank, pipes, old, existing))]
        if not variants:
            _reject(rejected, "degraded_free_site:water_link_audit_failed")
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


def candidate_layouts(game, builder, obs, site, board: dict, rejected=None):
    """Yield live-checked layouts: adjacent banks (nearer coal first), then free sites.

    ``rejected`` (a dict) collects why slots and sites were dropped (``kind:reason`` -> count).
    """
    power = (getattr(builder, "state", {}) or {}).get("power_plan") or {}
    frame = plant_frame(power)
    old_boilers = [e for e in power.get("entities", []) if e["name"] == "boiler"]
    if not old_boilers:
        _reject(rejected, "first_plant_plan_has_no_boiler")
        return
    existing = known_rows(builder, board)
    areas = board_areas(board)
    origin = _coal_origin(obs)
    adjacent = adjacent_layouts(frame, power, existing) if frame else []
    if len(adjacent) < 2:
        # Both row ends: the plant's own coal feed or the shore usually takes one or both.
        _reject(rejected, "adjacent_bank:slot_meets_known_hardware_or_water")
    live = [kept for layout in adjacent if (kept := _live(game, builder, obs, layout, areas, rejected))]
    yield from _side_order(live, origin)
    rotation = frame["rotation"] if frame else old_boilers[0].get("direction", 0) % 16
    for layout in fallback_layouts(builder, obs, site, rotation, old_boilers, existing, rejected):
        kept = _live(game, builder, obs, layout, areas, rejected)
        if kept:
            yield kept
