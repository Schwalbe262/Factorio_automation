"""Mine frames that follow their patch, and the border with a neighbouring patch of another ore.

Pure helpers for :mod:`mining_layout_policy` (docs/v2/LAYOUT_PRINCIPLES.md
"Mine output trunk", "Patch frame and neighbouring patches").

* :func:`tile_rects` turns a tile set into a row-wise polygon (a list of
  rects): a bank's board reservation is its planned drill cells, lane/pole
  tiles and exit, not their bounding box, so a neighbouring patch keeps its
  tiles for its own mine.
* :func:`mixed_zone` finds tiles where two resources are interleaved
  (``mixed_ore_zone``): no normal bank goes there.
* Border drills (a drill whose 5x5 mining area holds this patch's ore and a
  neighbour's ore) are excluded unless :func:`decide_border_lane` finds that
  the ore they gain pays for a filter splitter device (:func:`filter_device`):
  the border lane's foreign item leaves on the splitter's filter side to a
  sink, the main item continues, so the trunk stays single-item.
  :func:`device_proof` checks that on a live observation and says when the
  border drills must stop (their sink is nearly full), so a full sink can never
  jam the lane.
"""

from __future__ import annotations

import math

# A tile is part of a mixed zone when another resource lies on both opposite
# sides of it within this many tiles (a straight border has it on one side only).
MIXED_REACH = 2
MIXED_ZONE_MIN_TILES = 8
# A port facing another patch: foreign ore within this many tiles ahead of it.
FACING_REACH = 16
# Splitters need the logistics research (filter splitters are splitters).
FILTER_RESEARCH = "logistics"
# Raw ore per device part (recipes: splitter 5 circuits + 5 plates + 4 belts;
# belt 1.5 plates; inserter 1 circuit + 1 gear + 1 plate; wooden chest = 2 wood).
DEVICE_RAW_ORE = {"splitter": 23.5, "transport-belt": 1.5, "inserter": 6.5, "wooden-chest": 0.0}
# A border lane must win at least this many times its device's raw ore cost.
MIXED_PAYBACK = 50
# Stop the border drills while the sink still holds what the lane can carry
# up to the splitter (belt tiles of the lane x 8 items) plus this margin.
SINK_MARGIN_ITEMS = 50
WOODEN_CHEST_SLOTS = 16


def tile_rects(tiles) -> list:
    """Row-wise rects ``[[x0, y0], [x1, y1]]`` covering tile centres ``(x, y)`` (half-tile values).

    Runs along x per row, then rows with identical runs merged downward.
    """
    rows = {}
    for x, y in {(float(a), float(b)) for a, b in tiles}:
        rows.setdefault(y, []).append(x)
    runs = {}
    for y, xs in rows.items():
        xs.sort()
        start = prev = xs[0]
        for x in xs[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            runs.setdefault((start, prev), []).append(y)
            if x is not None:
                start = prev = x
    rects = []
    for (x0, x1), ys in runs.items():
        ys.sort()
        top = last = ys[0]
        for y in ys[1:] + [None]:
            if y is not None and y == last + 1:
                last = y
                continue
            rects.append([[x0 - 0.5, top - 0.5], [x1 + 0.5, last + 0.5]])
            if y is not None:
                top = last = y
    return sorted(rects)


def rect_tiles(rect) -> set:
    """Tile centres inside ``[[x0, y0], [x1, y1]]``."""
    (x0, y0), (x1, y1) = rect
    return {
        (x + 0.5, y + 0.5) for x in range(math.floor(x0), math.ceil(x1)) for y in range(math.floor(y0), math.ceil(y1))
    }


def block_rects(block) -> list:
    """A board block's or bank's reservation as rects: ``reserved_rects`` when recorded, else ``[reserved_area]``.

    Banks planned before the row-wise frame keep their one rectangle as a legal reservation.
    """
    rects = block.get("reserved_rects") if isinstance(block, dict) else None
    if isinstance(rects, list) and rects:
        return rects
    area = block.get("reserved_area") if isinstance(block, dict) else None
    return [area] if area else []


def neighbour_reservations(blocks, banks, resource) -> list:
    """Keep-out rects for a new ``resource`` bank: every block's and bank's reservation rects,
    plus the whole planned patch frame (``patch_frame``) of mines of another resource.

    The neighbouring patch's frame is held whichever patch was planned first;
    this patch's own frame is not a keep-out (its later row pairs grow into it).
    """
    rects = []
    for item in list(blocks) + list(banks):
        if not isinstance(item, dict):
            continue
        rects += block_rects(item)
        owner = item.get("resource") or (item.get("plan") or {}).get("resource")
        if owner and owner != resource:
            rects += [r for r in item.get("patch_frame") or [] if r]
    return rects


def mixed_zone(own, other, *, reach=MIXED_REACH, minimum=MIXED_ZONE_MIN_TILES):
    """``mixed_ore_zone`` record of tiles where ``own`` and ``other`` (tile -> resource) interleave, or None.

    A tile is mixed when the other kind lies on both opposite sides of it
    (west and east, or north and south) within ``reach``. A clean border
    between two touching patches has it on one side only and is no mixed zone.
    """

    def flanked(p, kind):
        x, y = p
        for ux, uy in ((1, 0), (0, 1)):
            ahead = any(((x + ux * d, y + uy * d) in kind) for d in range(1, reach + 1))
            behind = any(((x - ux * d, y - uy * d) in kind) for d in range(1, reach + 1))
            if ahead and behind:
                return True
        return False

    mine = {p for p in own if flanked(p, other)}
    theirs = {p for p in other if flanked(p, own)}
    if len(mine) + len(theirs) < minimum or not mine:
        return None
    tiles = mine | theirs
    xs = [p[0] for p in tiles]
    ys = [p[1] for p in tiles]
    return {
        "tiles": len(tiles),
        "own_tiles": len(mine),
        "foreign_tiles": len(theirs),
        "resources": sorted({other[p] for p in theirs}),
        "bbox": [[min(xs) - 0.5, min(ys) - 0.5], [max(xs) + 0.5, max(ys) + 0.5]],
        "handled": False,
        "reason": "interleaved resources: needs sorted lanes (filter splitters), not a normal bank",
    }


def device_raw_cost(entities) -> float:
    return sum(DEVICE_RAW_ORE.get(e["name"], 0.0) for e in entities)


def decide_border_lane(gained_amount, gained_tiles, foreign, device, *, filter_ready):
    """``{"decision": "mixed" | "exclude", ...}`` for one grid row's border drills.

    Mixed only when filter splitters are researched, the border drills win own
    ore no clean drill reaches, exactly one foreign resource meets the lane
    (one filter), and that ore is at least ``MIXED_PAYBACK`` times the
    device's raw ore cost; otherwise the border cells stay excluded.
    """
    cost = device_raw_cost(device)
    record = {
        "gained_amount": gained_amount,
        "gained_tiles": gained_tiles,
        "foreign": sorted(foreign),
        "device_raw_ore": cost,
        "payback": MIXED_PAYBACK,
    }
    if not gained_tiles:
        return dict(record, decision="exclude", reason="border drills reach no ore a clean drill misses")
    if len(foreign) != 1:
        return dict(record, decision="exclude", reason="more than one foreign resource needs more than one filter")
    if gained_amount < MIXED_PAYBACK * cost:
        return dict(record, decision="exclude", reason="ore gained does not pay for the filter device")
    if not filter_ready:
        return dict(record, decision="exclude", reason="filter splitter needs " + FILTER_RESEARCH + " research")
    return dict(record, decision="mixed", reason="ore gained pays for the filter device")


def filter_device(port, main, foreign, *, sink="chest"):
    """Canonical EAST filter device after a collector whose last belt is ``port`` ``(x, y)``.

    The splitter stands one tile past the port, its two lanes on the collector
    row (left, north: the main item continues east) and the row below (right,
    south: filter = ``foreign``, output priority right). ``sink`` "chest": the
    foreign belt ends in an inserter and a wooden chest; "trunk": the foreign
    belt continues east to the neighbour's trunk (its route is that trunk's
    tap, outside this device). Returns ``entities``, ``main_output``,
    ``foreign_output`` and ``chest`` (or None).
    """
    from .board import entity

    px, py = port
    sx = px + 1
    splitter = dict(
        entity("splitter", sx, py + 0.5, 4),
        splitter_input_priority="none",
        splitter_output_priority="right",
        splitter_filter=foreign,
        filter_item=foreign,
        main_item=main,
    )
    main_out = entity("transport-belt", sx + 1, py, 4)
    foreign_out = entity("transport-belt", sx + 1, py + 1, 4)
    entities = [splitter, main_out, foreign_out]
    chest = None
    if sink == "chest":
        entities.append(entity("inserter", sx + 2, py + 1, 12))  # picks the belt (west), drops east
        chest = entity("wooden-chest", sx + 3, py + 1)
        entities.append(chest)
    return {
        "entities": entities,
        "splitter": splitter,
        "main_output": main_out,
        "foreign_output": foreign_out,
        "chest": chest,
        "sink": sink,
    }


def configure_action(builder, obs, splitter) -> dict | None:
    """The paid ``configure_splitter`` (priorities + filter) for a built border splitter that differs, else None."""
    live = next(
        (e for e in obs.get("entities", []) if e["name"] == "splitter" and e["position"] == splitter["position"]),
        None,
    )
    wanted = {k: splitter[k] for k in ("splitter_input_priority", "splitter_output_priority", "splitter_filter")}
    if (
        live is None
        or live.get("direction") != splitter["direction"]
        or all(live.get(k) == v for k, v in wanted.items())
    ):
        return None
    move = getattr(builder, "_move", None)  # character backend: walk into reach first
    step = move(obs, live["position"]) if callable(move) else None
    if isinstance(step, dict):
        return step
    return {
        "type": "configure_splitter",
        "name": "splitter",
        "position": live["position"],
        "input_priority": wanted["splitter_input_priority"],
        "output_priority": wanted["splitter_output_priority"],
        "filter": wanted["splitter_filter"],
        "expected_splitter_unit": live["unit_number"],
        "expected_splitter_world_id": obs["world_id"],
        "reason": "border lane filter: the foreign item leaves on the filter side, the main item stays on the trunk",
    }


def device_proof(obs, device, lane_belts, border_drills, *, stack_size=50):
    """Live check of a border lane device.

    ``ok`` when the main output belt carries only the main item and the
    splitter carries the wanted filter; ``retire_border`` when the chest sink
    has no more room than the lane could still bring (lane belts x 8 + margin):
    the border drills must stop now so the foreign item can never back up
    into the splitter and stop the main item.
    """
    at = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}

    def live(spec):
        return at.get((spec["name"], spec["position"]["x"], spec["position"]["y"]))

    splitter = live(device["splitter"])
    if splitter is None:
        return {"ok": False, "reason": "border_filter_splitter_missing"}
    if splitter.get("splitter_filter") != device["splitter"]["splitter_filter"]:
        return {"ok": False, "reason": "border_filter_not_configured"}
    main = device["splitter"]["main_item"]
    out = live(device["main_output"]) or {}
    carried = {k for k, v in (out.get("belt_inventory") or {}).items() if v}
    if carried - {main}:
        return {"ok": False, "reason": "border_trunk_contaminated", "items": sorted(carried)}
    result = {"ok": True, "main_items": sorted(carried), "retire_border": False}
    if device.get("chest") is not None:
        chest = live(device["chest"]) or {}
        held = sum((chest.get("inventory") or {}).values())
        room = WOODEN_CHEST_SLOTS * stack_size - held
        need = 8 * max(1, len(lane_belts)) + SINK_MARGIN_ITEMS
        result.update(sink_items=held, sink_room=room)
        if room <= need:
            result.update(retire_border=True, retire=[dict(d) for d in border_drills])
    return result
