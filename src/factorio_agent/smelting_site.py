"""Smelting district geometry and its site on the master plan.

Canonical district (anchor ``(x, y)``, map tiles, x east, y south): the
reserved area is ``[x - 8, x + 24) x [y - 8, y + 24)`` (32 x 32). The iron
furnace bank sits at ``(x, y)`` and the copper bank at ``(x, y + 12)``; ore
enters each bank from the north-west, coal enters the district on the west
edge at ``(x - 7.5, y + 3.5)`` and is split to both banks; plates leave each
bank on the east edge (facing east). See :func:`smelting.furnace_bank`.

On the plan (:func:`plan_candidates`) the district is placed in a 32 x 32
corner of the plan's ``smelting`` zone (the rest stays growth land) and
turned with :func:`site.fit.orientations` so ore/coal ports face the
feeders' entry points and plate outputs face the zone's output edge.
Every candidate records its ``site_transform`` (canonical -> map); banks
carry it too so later expansion rebuilds them in the same orientation.

Lanes: a mirrored bank keeps a straight shared input belt with ore
side-loaded on one lane and coal on the other, only the lane sides swap;
the inserters take from both lanes and the proof checks "one item per lane,
two different items", so a mirrored bank behaves identically.
"""

from __future__ import annotations

from .board import entity
from .site.fit import orientations
from .site.geometry import Rect
from .site.orientation import edge_point
from .site.transform import Transform, apply

DISTRICT_TILES = 32
FEEDS = ("iron-ore", "copper-ore", "coal")
PLAN_CANDIDATES = 8


def district_candidate(x, y, counts):
    """The canonical district candidate dict used by ``SmeltingDistrict.prepare``."""
    from .smelting import furnace_bank, merge_entities, port

    area = [[x - 8, y - 8], [x + 24, y + 24]]
    banks = [
        furnace_bank(x, y, counts["iron-ore"], "iron-ore"),
        furnace_bank(x, y + 12, counts["copper-ore"], "copper-ore"),
    ]
    splitter = dict(entity("splitter", x - 5.5, y + 4, 4), _width=2, _height=1)
    split = [
        splitter,
        entity("transport-belt", x - 7.5, y + 3.5, 4),
        entity("transport-belt", x - 6.5, y + 3.5, 4),
        entity("transport-belt", x - 4.5, y + 3.5, 4),
        entity("transport-belt", x - 4.5, y + 4.5, 4),
    ]
    return {
        "area": area,
        "banks": banks,
        "entities": merge_entities(*(b["entities"] for b in banks), split),
        "coal_input": port("coal", x - 7.5, y + 3.5, 4),
        "coal_branches": [
            port("coal", x - 4.5, y + 3.5, 4, "output"),
            port("coal", x - 4.5, y + 4.5, 4, "output"),
        ],
    }


def oriented(candidate: dict, transform: Transform) -> dict:
    """Record ``transform`` on the candidate and on each of its banks."""
    record = transform.to_dict()
    candidate["site_transform"] = record
    for bank in candidate["banks"]:
        bank["site_transform"] = record
    return candidate


def canonical_bank(bank: dict) -> tuple[dict, Transform]:
    """The bank in canonical orientation and the transform back to the map."""
    t = Transform.from_dict(bank.get("site_transform"))
    if t.identity:
        return bank, t
    return apply(t.inverse(), bank), t


def _feed_points(plan, smelting) -> dict[str, tuple[tuple[float, float], str]]:
    """Where each feed arrives and through which edge.

    Its entry port on the zone when it has one, else the centre of its mining
    zone (no edge: judged by direction to the point).
    """
    points = {}
    for item in FEEDS:
        entry = smelting.port(item, "in")
        if entry is not None:
            points[item] = (entry.point, entry.side)
            continue
        mining = plan.zone("mining:" + item)
        points[item] = (mining.rect.centre() if mining is not None else smelting.rect.centre(), "")
    return points


def _wants(points, outlet, output_side):
    def wants(candidate):
        rows = []
        for bank in candidate["banks"]:
            point, side = points[bank["resource"]]
            rows.append((bank["ports"][0], point, "in", side))
        point, side = points["coal"]
        rows.append((candidate["coal_input"], point, "in", side))
        rows += [(bank["ports"][-1], outlet, "out", output_side) for bank in candidate["banks"]]
        return rows

    return wants


def zone_slots(zone: Rect, size: int = DISTRICT_TILES) -> list[Rect]:
    """The four corner squares of ``zone`` (one when the zone is exactly ``size``)."""
    if zone.width < size or zone.height < size:
        return []
    xs = dict.fromkeys((zone.x0, zone.x1 - size))
    ys = dict.fromkeys((zone.y0, zone.y1 - size))
    return [Rect(x, y, x + size, y + size) for x in xs for y in ys]


def plan_candidates(plan, counts, *, limit: int = PLAN_CANDIDATES) -> list[dict]:
    """Oriented district candidates inside the plan's smelting zone, best first.

    Each corner square of the zone is tried with every fitting orientation;
    the score is :func:`site.fit.port_score` summed over the ore/coal inputs
    (to the feeders' entry points) and the plate outputs (to the zone's
    output edge point). The best ``limit`` are returned.
    """
    smelting = plan.zone("smelting")
    if smelting is None:
        return []
    points = _feed_points(plan, smelting)
    outlet = edge_point(smelting.rect, smelting.output_side) if smelting.output_side else smelting.rect.centre()
    wants = _wants(points, outlet, smelting.output_side)
    placements = []
    for slot in zone_slots(smelting.rect):

        def build(x0, y0):
            return district_candidate(x0 + 8, y0 + 8, counts)

        placements += orientations(build, (DISTRICT_TILES, DISTRICT_TILES), slot, wants)
    placements.sort(key=lambda p: p.score)
    return [oriented(p.plan, p.transform) for p in placements[:limit]]


def corridor_guide(plan, item: str):
    """Bounding box ``(x0, y0, x1, y1)`` of the reserved feeder corridor for ``item``, or None."""
    corridor = plan.corridor(f"corridor:{item}->smelting")
    if corridor is None or not corridor.segments:
        return None
    rects = corridor.rects
    return (
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )
