"""Anchors for fixed-shape blocks from the shared ``SiteService`` (never map waypoints).

The oil and chemistry controllers build a fixed core at an anchor ``(x, y)``
(``advanced_refinery_core``, ``fluid_product_core``, ``chemical_core``, ...). On a planned
world the anchor must come from the master plan: ``anchors(site, obs, owner, build)`` probes
the core's footprint once (``build(0.5, 0.5)["reserved_area"]``), asks
``site.allocate_candidates("production", w, h, name=owner, ...)`` for district blocks of that
size, and returns one anchor per block that puts the core's reserved area on the block's
corner, with the allocation name. The controller keeps its own audits, routes and land
checks per anchor; ``keep(site, obs, owner, name)`` settles the chosen block (or releases all
candidates with ``None``), as ``site.placement.settle`` does for ``produce``.

Without a site service (development map, tests) ``anchors`` returns None and the caller keeps
its legacy relative anchors.

Item inputs on a planned world (``planned_bus`` published): ``bus_taps`` plans the core's
taps with the block (``planned_tap.connect_inputs`` on the core's allocation, as
``production_site._taps`` does); the controller stores the taps, keeps them out of its own
plan (``own_entities``), and in ``step`` calls ``planned_tap.publish`` and
``planned_tap.cutover`` before its ``ensure_plan``.
"""

from __future__ import annotations

import math

CANDIDATES = 6
CANDIDATES_MAX = 12  # hard maximum of the site:more_blocks remedy rung
KIND = "production"


def footprint(build) -> tuple[int, int, float, float]:
    """``(width, height, dx, dy)``: size of ``build``'s reserved area and the anchor's offset from its corner."""
    (x0, y0), (x1, y1) = build(0.5, 0.5)["reserved_area"]
    return int(math.ceil(x1 - x0)), int(math.ceil(y1 - y0)), 0.5 - x0, 0.5 - y0


def anchors(site, obs, owner: str, build, *, count: int = CANDIDATES, returns_to_bus: bool = False):
    """``[((x, y), allocation_name), ...]`` from the site service, ``[]`` when none fits, None without a site."""
    found = blocks(site, obs, owner, build, count=count, returns_to_bus=returns_to_bus)
    return None if found is None else [(anchor, block.name) for anchor, block in found]


def blocks(site, obs, owner: str, build, *, count: int = CANDIDATES, returns_to_bus: bool = False):
    """``anchors`` with the allocation objects: ``[((x, y), allocation), ...]`` (side and along range for taps)."""
    if site is None:
        return None
    from . import remedy

    # Remedy rung "site:more_blocks" (remedy.SITE_MORE_BLOCKS): more district blocks probed, hard maximum.
    wider = remedy.option("block_site_candidates", None)
    if isinstance(wider, int) and wider > count:
        count = min(wider, CANDIDATES_MAX)
    width, height, dx, dy = footprint(build)
    try:
        found = site.allocate_candidates(
            KIND, width, height, name=owner, count=count, returns_to_bus=returns_to_bus, world_id=obs["world_id"]
        )
    except Exception:  # noqa: BLE001 - an allocator failure is "no site", never a crash
        return []
    return [((block.rect.x0 + dx, block.rect.y0 + dy), block) for block in found]


def planned_bus(root, obs) -> dict | None:
    """The published planned main bus of this world (``planned_tap.published``), or None (no bus, dev map)."""
    import json
    from pathlib import Path

    from . import planned_tap

    path = Path(root) / "pcb-board.json"
    try:
        board = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return None
    return planned_tap.published(board, obs.get("world_id")) if isinstance(board, dict) else None


def bus_taps(game, builder, obs, bus, allocation, *, owner, ports, entities, demand, route=None) -> dict:
    """Planned-bus taps and belt routes for a fixed core's item ``ports`` (``planned_tap.connect_inputs``).

    ``allocation`` is the core's district block (side, along range); without one the tap has no
    side to sit on. The result is ``connect_inputs``'s: ``entities`` = ``entities`` + taps +
    routes, ``taps`` (published with the bus, built first by ``planned_tap.cutover``), ``routes``,
    ``ignored_units`` (trunk belts the taps replace). A failure names the item (``plan_needs`` tap).
    """
    from . import planned_tap
    from .transport import plan_connection

    if allocation is None:
        return {"ok": False, "reason": "planned_bus_tap_needs_site_allocation", "owner": owner}
    span = planned_tap.max_span(game)
    if span is None:
        return {"ok": False, "reason": "planned_bus_underground_span_unknown"}
    return planned_tap.connect_inputs(
        game,
        builder,
        obs,
        bus,
        owner=owner,
        allocation=allocation,
        inputs=list(ports),
        entities=list(entities),
        demand=dict(demand),
        span=span,
        route=route or plan_connection,
    )


def own_entities(entities, taps) -> list[dict]:
    """``entities`` without the taps' hardware (the taps are published and built with the bus)."""
    from .transport import xy

    tapped = {(e["name"], *xy(e)) for tap in taps or () for e in tap.get("entities", [])}
    return [e for e in entities if (e["name"], *xy(e)) not in tapped]


def extended(state: dict, new_core: dict) -> dict:
    """In-place growth of a fixed core into its reserved slots (same anchor, larger machine count).

    ``new_core`` is the core rebuilt at ``state["core"]["anchor"]`` with more machines. Every
    entity of the built core must stand unchanged in the new one (``growth_changes_built_core``
    otherwise); the result is the saved plan plus the new core's added entities and underground
    pairs (routes, power and ports kept), ready to republish and build.
    """
    from copy import deepcopy

    from .core.deterministic_builder import _plan
    from .transport import xy

    def key(e):
        return (e["name"], *xy(e), e.get("direction", 0), e.get("recipe"))

    old_core = state["core"]
    if new_core.get("anchor") != old_core.get("anchor"):
        return {"ok": False, "reason": "growth_anchor_changed"}
    old = {key(e) for e in old_core["entities"]}
    new = {key(e) for e in new_core["entities"]}
    if not old <= new:
        return {"ok": False, "reason": "growth_changes_built_core", "missing": len(old - new)}
    added = [deepcopy(e) for e in new_core["entities"] if key(e) not in old]
    plan = state["plan"]
    pairs = list(plan.get("underground_pairs") or [])
    pairs += [p for p in new_core.get("underground_pairs") or () if p not in pairs]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    extra["underground_pairs"] = pairs
    return {"ok": True, "added": added, "plan": _plan(list(plan["entities"]) + added, plan["ports"], **extra)}


def planned_tiles(builder, obs, root, board, *, exclude=()) -> dict:
    """``{tile: {"block", "name", "position"}}`` of every unbuilt planned entity a source attachment must keep off.

    Board blocks, the planned bus (published beside the blocks) and the plans no board block
    holds (``board.unboarded_plans``: power feeds, pole chains, pending starter cells); the
    caller's own blocks in ``exclude`` are skipped (``board.planned_unbuilt_hardware``).
    """
    from .board import planned_unbuilt_hardware, unboarded_plans

    blocks = dict(board.get("blocks") or {})
    if isinstance(board.get("shared_main_bus"), dict):
        blocks.setdefault("shared_main_bus", board["shared_main_bus"])
    try:
        blocks.update(unboarded_plans(root, obs))
    except (OSError, ValueError, TypeError):
        pass
    return planned_unbuilt_hardware(builder, obs, blocks, exclude=tuple(exclude))


def first_clash(builder, entities, tiles: dict) -> dict | None:
    """The first planned entity of another block on a tile ``entities`` would occupy, or None."""
    if not tiles:
        return None
    for tile in sorted(builder._occupied_by_plan(list(entities))):
        if tile in tiles:
            return dict(tiles[tile])
    return None


def header_needs(owner: str, reason: str, source: dict, failures) -> dict:
    """``plan_needs`` (kind ``site``) of a source-attached header that fits at no candidate offset."""
    from . import plan_needs

    rows = [f for f in failures if isinstance(f, dict)]
    blockers = [f["blocker"] for f in rows if isinstance(f.get("blocker"), dict)]
    return plan_needs.make(
        "site",
        reason,
        block=owner,
        item=source.get("item"),
        at=source.get("position"),
        tried=len(failures) or None,
        rejected_by=plan_needs._counts(failures),
        blockers=blockers,
        change=f"a free header spot beside the {source.get('item')} outlet, clear of planned blocks",
    )


def tap_needs(owner: str, failure: dict) -> dict:
    """The ``plan_needs`` record of a failed ``bus_taps`` (kind ``tap``)."""
    from . import plan_needs

    inner = failure.get("failures") if isinstance(failure.get("failures"), list) else []
    return plan_needs.make(
        "tap",
        str(failure.get("reason") or "planned_bus_tap_unavailable"),
        block=owner,
        item=failure.get("item"),
        side=failure.get("side"),
        tried=len(inner) or None,
        rejected_by=failure.get("rows_rejected") or None,
    )


def keep(site, obs, owner: str, name: str | None) -> None:
    """Settle ``name`` for ``owner`` (or release every candidate with None)."""
    if site is None:
        return
    from .site.placement import settle

    settle(site, obs["world_id"], owner, name)
