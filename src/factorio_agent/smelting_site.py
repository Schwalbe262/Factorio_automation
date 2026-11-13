"""Smelting district geometry and its site on the master plan.

Canonical district (anchor ``(x, y)``, map tiles, x east, y south): the
reserved area is ``[x - 8, x + 40) x [y - 8, y + 24)`` (48 x 32, corner
``(X0, Y0) = (x - 8, y - 8)``). Two paired banks
(:func:`smelting.paired_furnace_bank`, 12 rows each) fill it::

    rows  0-11  iron bank: ore port (X0+0.5, Y0+0.5) facing east, upper and
                lower furnace rows around the output belt at Y0+6.5
    rows 12-19  coal: input port (X0+0.5, Y0+15.5) facing east, splitter at
                X0+1.5, branches up/down column X0+5.5 into both banks' coal
                ports (split head; the older shared head: column X0+3.5)
    rows 20-31  copper bank, the iron bank mirrored about y = Y0+16: ore port
                (X0+0.5, Y0+31.5), output belt at Y0+25.5

Feed head: the flow model's choice at full reserved growth (``district_head_choice``,
``feed_head.rank_heads``), today the split head (``feed_head.split_feed_head``): ore split
head-on first, each half side-loaded onto its row's belt, coal likewise on the other lane;
each row gets a full lane of ore (450/min, 24 stone furnaces) instead of a quarter belt
(225/min, 12 furnaces). Saved districts keep their recorded ``feed_head`` (``shared`` when
absent); ``head_rework`` replaces a deficient one.

Every input enters on the west edge and both plate ports sit on the east
edge (facing east). Each bank has ``capacity_slots`` (12) furnace slots per
row; the built ones come from demand, the rest are the bank's reserved
growth (``growth_area``, ``growth_slots``): nothing else, not even the
district's own feed routes or power poles, may use those tiles, so growth
is an in-place addition (``SmeltingDistrict.capacity_step``).

On the plan (:func:`plan_candidates`) the district takes one end of the
plan's ``smelting`` zone (64 x 48: the district's 48-tile length runs along
the zone depth, so plates leave through the zone's output edge and the
other 32 x 48 half stays free for a second district) and is turned with
:func:`site.fit.orientations` so ore/coal ports face the feeders' entry
points and plate outputs face the zone's output edge. Every candidate
records its ``site_transform`` (canonical -> map); banks carry it too.

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

DISTRICT_WIDTH = 48  # along the plate flow (canonical x)
DISTRICT_HEIGHT = 32
DISTRICT_SIZE = (DISTRICT_WIDTH, DISTRICT_HEIGHT)
FEEDS = ("iron-ore", "copper-ore", "coal")
PLAN_CANDIDATES = 8


def per_row(count: int) -> int:
    """Furnaces per row of a paired bank for ``count`` furnaces (both rows equal, at least one)."""
    return max(1, -(-count // 2))


def _bank(x0, y0, count, resource, head):
    from .feed_head import with_head
    from .smelting import paired_furnace_bank

    bank = paired_furnace_bank(x0, y0, per_row(count), resource, length=DISTRICT_WIDTH)
    return with_head(bank, x0, y0, head)


def district_bank_audit(head: str, resource: str = "iron-ore") -> dict:
    """belt_flow audit of one canonical district bank with ``head`` at full reserved growth.

    Stone furnace rates are the ones the district audit uses (``bandwidth_audit.district_rows``):
    18.75 ore/min and 1.35 coal/min per furnace.
    """
    from .bandwidth_audit import COAL_JOULES, FURNACE_WATTS, PLATE_SECONDS, _with_growth, furnace_bank
    from .smelting import BANK_SLOTS

    bank = _bank(0, 0, 2 * BANK_SLOTS, resource, head)
    ore = 60 / PLATE_SECONDS
    return furnace_bank(
        _with_growth(bank),
        bank["ports"],
        raw=resource,
        raw_per_furnace=ore,
        coal_per_furnace=FURNACE_WATTS["stone-furnace"] * 60 / COAL_JOULES,
        product=bank["product"],
        product_per_furnace=ore,
    )


_HEAD_CHOICE: dict = {}


def district_head_choice() -> dict:
    """The district banks' feed head, ranked by the flow model at full reserved growth (pure, cached)."""
    if not _HEAD_CHOICE:
        from .feed_head import rank_heads

        _HEAD_CHOICE.update(
            rank_heads(district_bank_audit, lambda head: len(_bank(0, 0, 2, "iron-ore", head)["entities"]))
        )
    return dict(_HEAD_CHOICE)


def _coal_network(x0, y0, head):
    """The district's coal input, splitter and fixed branches into both banks' coal ports.

    Shared head: straight up/down the coal column x0 + 3.5 into each bank's coal stub.
    Split head: each branch turns along its splitter row to column x0 + 5.5 and enters the
    bank's coal port on the bank's inner edge (row 11 of the iron bank, row 20 of the
    mirrored copper bank).
    """
    middle = y0 + DISTRICT_HEIGHT // 2  # coal splitter rows middle - 1 and middle
    coal = [
        entity("transport-belt", x0 + 0.5, middle - 0.5, 4),
        dict(entity("splitter", x0 + 1.5, middle, 4), _width=2, _height=1),
        entity("transport-belt", x0 + 2.5, middle - 0.5, 4),
        entity("transport-belt", x0 + 2.5, middle + 0.5, 4),
    ]
    if head == "split":
        coal += [entity("transport-belt", x0 + dx + 0.5, middle - 0.5, 4) for dx in (3, 4)]
        coal += [entity("transport-belt", x0 + dx + 0.5, middle + 0.5, 4) for dx in (3, 4)]
        coal += [entity("transport-belt", x0 + 5.5, yy + 0.5, 0) for yy in range(y0 + 12, middle)]
        coal += [entity("transport-belt", x0 + 5.5, yy + 0.5, 8) for yy in range(middle, y0 + DISTRICT_HEIGHT - 12)]
        return coal
    coal += [entity("transport-belt", x0 + 3.5, yy + 0.5, 0) for yy in range(y0 + 4, middle)]
    coal += [entity("transport-belt", x0 + 3.5, yy + 0.5, 8) for yy in range(middle, y0 + DISTRICT_HEIGHT - 4)]
    return coal


def district_candidate(x, y, counts, head=None):
    """The canonical district candidate dict used by ``SmeltingDistrict.prepare`` (module doc).

    ``head`` is the banks' feed head; None takes the flow model's choice
    (``district_head_choice``)."""
    from .smelting import BANK_DEPTH, merge_entities, port

    head = head or district_head_choice()["head"]
    x0, y0 = x - 8, y - 8
    iron = _bank(x0, y0, counts["iron-ore"], "iron-ore", head)
    copper = _bank(x0, y0, counts["copper-ore"], "copper-ore", head)
    copper = apply(Transform(0, "y", (x0, y0 + DISTRICT_HEIGHT // 2)), copper)
    middle = y0 + DISTRICT_HEIGHT // 2
    coal = _coal_network(x0, y0, head)
    if BANK_DEPTH * 2 > DISTRICT_HEIGHT - 4:
        raise ValueError("two banks and the coal band do not fit the district")
    banks = [iron, copper]
    return {
        "area": [[x0, y0], [x0 + DISTRICT_WIDTH, y0 + DISTRICT_HEIGHT]],
        "banks": banks,
        "entities": merge_entities(*(b["entities"] for b in banks), coal),
        "coal_input": port("coal", x0 + 0.5, middle - 0.5, 4),
        "coal_branches": [],  # fixed belts above; nothing to route inside the district
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


def zone_slots(zone: Rect, size: tuple[int, int] = DISTRICT_SIZE) -> list[Rect]:
    """Corner rects of ``zone`` the district fits in, in both turns (``size`` and its transpose)."""
    slots = []
    for w, h in dict.fromkeys((size, size[::-1])):
        if zone.width < w or zone.height < h:
            continue
        xs = dict.fromkeys((zone.x0, zone.x1 - w))
        ys = dict.fromkeys((zone.y0, zone.y1 - h))
        slots += [Rect(x, y, x + w, y + h) for x in xs for y in ys]
    return slots


def plan_candidates(plan, counts, *, limit: int = PLAN_CANDIDATES) -> list[dict]:
    """Oriented district candidates inside the plan's smelting zone, best first.

    Each corner rect of the zone is tried with every fitting orientation;
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

        placements += orientations(build, DISTRICT_SIZE, slot, wants)
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
