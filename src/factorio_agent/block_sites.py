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
"""

from __future__ import annotations

import math

CANDIDATES = 6
KIND = "production"


def footprint(build) -> tuple[int, int, float, float]:
    """``(width, height, dx, dy)``: size of ``build``'s reserved area and the anchor's offset from its corner."""
    (x0, y0), (x1, y1) = build(0.5, 0.5)["reserved_area"]
    return int(math.ceil(x1 - x0)), int(math.ceil(y1 - y0)), 0.5 - x0, 0.5 - y0


def anchors(site, obs, owner: str, build, *, count: int = CANDIDATES, returns_to_bus: bool = False):
    """``[((x, y), allocation_name), ...]`` from the site service, ``[]`` when none fits, None without a site."""
    if site is None:
        return None
    width, height, dx, dy = footprint(build)
    try:
        blocks = site.allocate_candidates(
            KIND, width, height, name=owner, count=count, returns_to_bus=returns_to_bus, world_id=obs["world_id"]
        )
    except Exception:  # noqa: BLE001 - an allocator failure is "no site", never a crash
        return []
    out = []
    for block in blocks:
        x0, y0 = block.rect.x0, block.rect.y0
        out.append(((x0 + dx, y0 + dy), block.name))
    return out


def keep(site, obs, owner: str, name: str | None) -> None:
    """Settle ``name`` for ``owner`` (or release every candidate with None)."""
    if site is None:
        return
    from .site.placement import settle

    settle(site, obs["world_id"], owner, name)
