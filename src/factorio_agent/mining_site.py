"""Where a new primary mining bank sends its ore, read from the site master plan.

Conventions (map tiles, x east, y south; sides and ports as in
:mod:`site.planzones`):

* The primary patch of ``resource`` is the plan zone ``mining:<resource>``
  (kind "mining"). Its ``output_side`` is the edge its feeder corridor leaves
  from, toward the consumer. A new bank's collector flows toward that side,
  so its exit runs off the ore straight into the reserved corridor.
* Outpost zones (kind "outpost") and zones without an output side give no
  exit: the caller keeps the historical east-flowing bank.
* :func:`plan_exit` returns a plain JSON-serialisable dict that is stored
  with the bank (``plan["site_exit"]``) so a restart keeps the decision:

  ``resource``, ``zone``, ``zone_rect`` [x0, y0, x1, y1], ``side``,
  ``flow`` (a :data:`mining_layout_policy.FLOWS` name), ``facing``
  (0/4/8/12), ``corridor`` (name or None), ``corridor_start`` [x, y] (the
  zone's out port, else the consumer's entry port), ``entry_port``
  ({"zone", "side", "point"} or None) and ``guide`` [x0, y0, x1, y1] (the
  corridor's bounding box, None when the zones touch).
"""

from __future__ import annotations

SIDE_FLOWS = {"N": "south_to_north", "E": "west_to_east", "S": "north_to_south", "W": "east_to_west"}
SIDE_FACINGS = {"N": 0, "E": 4, "S": 8, "W": 12}
INWARD = {"N": (0, 1), "S": (0, -1), "W": (1, 0), "E": (-1, 0)}


def corridor_box(corridor):
    """Bounding box ``[x0, y0, x1, y1]`` of a corridor's segments, or None."""
    if corridor is None or not corridor.segments:
        return None
    rects = corridor.rects
    return [min(r.x0 for r in rects), min(r.y0 for r in rects), max(r.x1 for r in rects), max(r.y1 for r in rects)]


def plan_exit(plan, resource):
    """The exit decision for a new ``resource`` bank on ``plan`` (a MasterPlan), or None."""
    zone = plan.zone("mining:" + resource)
    if zone is None or zone.kind != "mining" or zone.output_side not in SIDE_FLOWS:
        return None
    corridor = next((c for c in plan.corridors if c.kind == "feeder" and c.source == zone.name), None)
    consumer = plan.zone(corridor.target) if corridor is not None else None
    entry = consumer.port(resource, "in") if consumer is not None else None
    out = zone.port(resource, "out")
    start = out.point if out is not None else (entry.point if entry is not None else None)
    if start is None:
        return None
    return {
        "resource": resource,
        "zone": zone.name,
        "zone_rect": zone.rect.to_list(),
        "side": zone.output_side,
        "flow": SIDE_FLOWS[zone.output_side],
        "facing": SIDE_FACINGS[zone.output_side],
        "corridor": corridor.name if corridor is not None else None,
        "corridor_start": [float(start[0]), float(start[1])],
        "entry_port": (
            {"zone": consumer.name, "side": entry.side, "point": [float(entry.point[0]), float(entry.point[1])]}
            if entry is not None
            else None
        ),
        "guide": corridor_box(corridor),
    }


def alternative_exits(record):
    """Exit records for the other three sides of the same zone, best first.

    The planned side can be unusable in practice: the early burner cells sit on
    the patch fringe nearest the base, which is usually also the side facing
    the consumer (live, seed 1400895309: every candidate on the planned east
    fringe hit existing hardware). The bank may then leave by another side and
    the belt is routed to the same entry port. Sides are ordered by the
    distance from that side's edge midpoint to the entry port (or to the
    planned corridor start). The reserved corridor no longer applies, so the
    guide is the box spanning the new start and the entry port.
    """
    if not record:
        return []
    x0, y0, x1, y1 = record["zone_rect"]
    midpoints = {
        "N": ((x0 + x1) / 2, float(y0)),
        "S": ((x0 + x1) / 2, float(y1)),
        "W": (float(x0), (y0 + y1) / 2),
        "E": (float(x1), (y0 + y1) / 2),
    }
    entry = record.get("entry_port")
    target = entry["point"] if entry else record["corridor_start"]
    options = []
    for side, start in midpoints.items():
        if side == record["side"]:
            continue
        guide = [
            int(min(start[0], target[0])) - 2,
            int(min(start[1], target[1])) - 2,
            int(max(start[0], target[0])) + 2,
            int(max(start[1], target[1])) + 2,
        ]
        options.append(
            (
                abs(start[0] - target[0]) + abs(start[1] - target[1]),
                dict(
                    record,
                    side=side,
                    flow=SIDE_FLOWS[side],
                    facing=SIDE_FACINGS[side],
                    corridor=None,
                    corridor_start=[start[0], start[1]],
                    guide=guide,
                    planned_side=record["side"],
                ),
            )
        )
    return [option for _distance, option in sorted(options, key=lambda row: (row[0], row[1]["side"]))]


def other_patches(site, world_id, resource):
    """The plan's other patches of ``resource`` (best first) as ``{"centroid", "bbox", "distance"}``; [] without a plan."""
    if site is None:
        return []
    try:
        row = site.plan(world_id).resource(resource)
    except Exception:  # noqa: BLE001 - the plan is preferred, never a new failure mode
        return []
    if row is None:
        return []
    return [
        {
            "centroid": [float(p.centroid[0]), float(p.centroid[1])],
            "bbox": p.bbox.to_list(),
            "distance": p.distance,
            "amount": p.amount,
            "tile_count": p.tile_count,
        }
        for p in row.others
    ]


def planned_patch(site, world_id, resource):
    """The plan's primary patch of ``resource`` (its mining zone's patch) as ``{"centroid", "bbox", "distance"}``, or None."""
    if site is None:
        return None
    try:
        row = site.plan(world_id).resource(resource)
    except Exception:  # noqa: BLE001 - the plan is preferred, never a new failure mode
        return None
    p = getattr(row, "primary", None)
    if p is None:
        return None
    return {"centroid": [float(p.centroid[0]), float(p.centroid[1])], "bbox": p.bbox.to_list(), "distance": p.distance}


ZONE_MARGIN = 6  # SiteConfig.mining_margin: a patch's zone is its bounding box plus this margin


def seed_in_zone(record, seed, margin=ZONE_MARGIN) -> bool:
    """True when the patch seed ``{"x", "y"}`` lies in the exit record's zone (``margin`` tiles of slack)."""
    x0, y0, x1, y1 = record["zone_rect"]
    return x0 - margin <= seed["x"] <= x1 + margin and y0 - margin <= seed["y"] <= y1 + margin


def patch_exits(record, bbox, margin=ZONE_MARGIN):
    """Exit records for every side of a patch the plan has no zone for, nearest the consumer first.

    ``record`` is the resource's :func:`plan_exit` (it names the consumer's entry port); the
    patch's zone is its ``bbox`` [x0, y0, x1, y1] plus ``margin``. Fleet all-1113-2155 seed
    1541632234: the plan's copper zone was a patch 298 tiles out (the near patch overlapped the
    iron zone), the bank surveyed the near one, the far zone dropped every site on all four
    sides and the near patch was then tried only with the historical east flow, which faced
    the iron patch. [] without a record.
    """
    if not record:
        return []
    x0, y0, x1, y1 = bbox
    zone = [x0 - margin, y0 - margin, x1 + margin, y1 + margin]
    base = dict(record, zone="patch", zone_rect=zone, side=None, corridor=None, guide=None)
    return [dict(option, planned_side=record["side"]) for option in alternative_exits(base)]


def plan_zone_areas(site, world_id, *, exclude_prefix=""):
    """``[(owner, [[x0, y0], [x1, y1]])]`` of every reserved plan zone, owners with ``exclude_prefix`` left out."""
    if site is None:
        return []
    try:
        zones = site.world(world_id).zones()
    except Exception:  # noqa: BLE001 - the plan is preferred, never a new failure mode
        return []
    return [
        ("plan:" + owner, [[r.x0, r.y0], [r.x1, r.y1]])
        for owner, r in zones
        if not (exclude_prefix and owner.startswith(exclude_prefix))
    ]


def site_exit(site, world_id, resource):
    """:func:`plan_exit` on the site service's plan; None without a service or plan."""
    if site is None:
        return None
    try:
        plan = site.plan(world_id)
    except Exception:  # noqa: BLE001 - the plan is preferred, never a new failure mode
        return None
    if plan is None:
        return None
    return plan_exit(plan, resource)


def entry_tile(entry):
    """Router destination for an entry port: the first tile inside the consumer.

    ``entry`` is the ``entry_port`` dict of :func:`plan_exit`. The port point
    lies on the edge line between two tiles; the tile on its lower-coordinate
    side along the edge is used. Returns ``({"x", "y"}, facing)`` where
    ``facing`` is the direction material travels as it enters.
    """
    side = entry["side"]
    x, y = entry["point"]
    ix, iy = INWARD[side]
    if side in ("N", "S"):
        tile = {"x": x - 0.5, "y": y + 0.5 * iy}
    else:
        tile = {"x": x + 0.5 * ix, "y": y - 0.5}
    facing = {(0, 1): 8, (0, -1): 0, (1, 0): 4, (-1, 0): 12}[(ix, iy)]
    return tile, facing


def guide_tuple(record):
    """The ``guide`` of a stored exit record as ``plan_connection``'s ``(x0, y0, x1, y1)``, or None."""
    guide = (record or {}).get("guide")
    return tuple(guide) if guide else None
