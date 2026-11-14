"""Layout tidiness of a board: built belts against the areas reserved for them. Data in, data out.

``board_tidiness(board, site_plan=None)`` takes the ``pcb-board.json`` dict (``blocks``,
``shared_main_bus``, ``connections``) and optionally the ``site-plan.json`` dict (or its
``plan``) and returns a metrics dict; it reads no files, so a live run (``tools/report_run.py``,
``tools/fleet.py report``) and an offline planner sweep over a planned board use the same numbers.

Per block (``blocks[name]``, plus the shared main bus as ``shared_main_bus``):

- ``belts``: belt-like entities of the block's ``entities`` (transport belts, undergrounds and
  splitters of any tier, ``belt_flow.kind_of``); ``growth_entities`` are a plan, not counted;
- ``outside``: those whose centre lies outside the block's ``reserved_area`` (the shared bus: outside
  every lane ``reservations`` area); None for a block without a reservation;
- ``undergrounds`` (underground belt entities), ``splitters``;
- ``turns``: belt curves, a transport belt fed by exactly one side neighbour and nothing from
  behind (the game's own curve rule), feeders looked up over the whole board.

Whole base (``totals``): the sums, ``outside_share`` = outside / belts of the blocks that have a
reservation, ``outside_plan`` = belts outside every block and bus reservation and, with a site plan,
outside the plan's bus zone, corridors and allocations (incl. their tap strips): belts no plan
accounts for. ``connections``: the board's planned point-to-point links (count; the longest by
Manhattan distance; the board holds endpoints only, so turns and crossings of a link are not known).
"""

from __future__ import annotations

import math

from .belt_flow import kind_of
from .routing import VECTORS

SHARED_BUS = "shared_main_bus"


def _rect(area):
    """``[[x0, y0], [x1, y1]]`` or ``[x0, y0, x1, y1]`` -> (x0, y0, x1, y1) floats, or None."""
    try:
        if len(area) == 2:
            (x0, y0), (x1, y1) = area
        else:
            x0, y0, x1, y1 = area
        x0, x1 = sorted((float(x0), float(x1)))
        y0, y1 = sorted((float(y0), float(y1)))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return (x0, y0, x1, y1)


def _inside(x, y, rects):
    return any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in rects)


def _position(entity):
    position = entity.get("position") if isinstance(entity, dict) else None
    try:
        x, y = (float(position["x"]), float(position["y"])) if isinstance(position, dict) else map(float, position)
    except (TypeError, ValueError, KeyError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _belts(entities):
    """(kind, x, y, direction, entity) of the belt-like entities of an entity list."""
    rows = []
    for entity in entities if isinstance(entities, list) else []:
        kind = kind_of(str(entity.get("name") or "")) if isinstance(entity, dict) else None
        xy = _position(entity) if kind else None
        if xy is not None:
            direction = entity.get("direction")
            rows.append((kind, xy[0], xy[1], direction if direction in VECTORS else 0, entity))
    return rows


def _tiles(kind, x, y, direction):
    """Tiles (integer x, y) a belt-like entity covers; a splitter is two tiles across its direction."""
    if kind != "splitter":
        return [(math.floor(x), math.floor(y))]
    if direction in (0, 8):
        return [(math.floor(x - 0.5), math.floor(y)), (math.floor(x + 0.5), math.floor(y))]
    return [(math.floor(x), math.floor(y - 0.5)), (math.floor(x), math.floor(y + 0.5))]


def board_blocks(board):
    """(name, entities, reservation rects or None) per board block, then the shared main bus."""
    board = board if isinstance(board, dict) else {}
    blocks = board.get("blocks") if isinstance(board.get("blocks"), dict) else {}
    rows = []
    for name, block in sorted(blocks.items(), key=lambda item: str(item[0])):
        if not isinstance(block, dict):
            continue
        rect = _rect(block.get("reserved_area"))
        rows.append((str(name), block.get("entities"), [rect] if rect else None))
    bus = board.get("shared_main_bus")
    if isinstance(bus, dict) and isinstance(bus.get("entities"), list):
        rects = [
            r
            for r in (_rect(row.get("area")) for row in bus.get("reservations") or [] if isinstance(row, dict))
            if r is not None
        ]
        rows.append((SHARED_BUS, bus["entities"], rects or None))
    return rows


def belt_cells(board):
    """Every built belt-like entity of the board: dict(block, kind, tiles, inside).

    ``inside`` is False outside the block's own reservation, None for a block without one."""
    cells = []
    for name, entities, rects in board_blocks(board):
        for kind, x, y, direction, _ in _belts(entities):
            inside = None if rects is None else _inside(x, y, rects)
            cells.append(dict(block=name, kind=kind, tiles=_tiles(kind, x, y, direction), inside=inside))
    return cells


def _emitters(rows):
    """tile -> direction of every entity that pushes items forward (belts, splitters, underground exits)."""
    out = {}
    for kind, x, y, direction, entity in rows:
        if kind == "underground" and str(entity.get("belt_to_ground_type") or entity.get("type")) == "input":
            continue
        for tile in _tiles(kind, x, y, direction):
            out[tile] = direction
    return out


def _is_curve(x, y, direction, emitters):
    tx, ty = math.floor(x), math.floor(y)

    def feeds(d):
        dx, dy = VECTORS[d]
        return emitters.get((tx - dx, ty - dy)) == d

    if feeds(direction):
        return False
    return sum(feeds(d) for d in ((direction + 4) % 16, (direction + 12) % 16)) == 1


def _plan_rects(site_plan):
    """Bus zone, corridor segments, allocations and their tap strips of a site plan (stored file or plan)."""
    plan = (
        site_plan.get("plan") if isinstance(site_plan, dict) and isinstance(site_plan.get("plan"), dict) else site_plan
    )
    if not isinstance(plan, dict):
        return []
    areas = [z.get("rect") for z in plan.get("zones") or [] if isinstance(z, dict) and z.get("kind") == "bus"]
    for corridor in plan.get("corridors") or []:
        areas += [s.get("rect") for s in (corridor or {}).get("segments") or [] if isinstance(s, dict)]
    for allocation in plan.get("allocations") or []:
        if isinstance(allocation, dict):
            areas += [allocation.get("rect"), allocation.get("tap_rect")]
    return [r for r in (_rect(a) for a in areas if a) if r is not None]


def _point(value):
    try:
        if isinstance(value, dict):
            return float(value["x"]), float(value["y"])
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def connection_metrics(connections):
    """Count and the longest (Manhattan) of the board's planned connections."""
    rows = []
    for row in connections if isinstance(connections, list) else []:
        a, b = (_point(row.get("source")), _point(row.get("destination"))) if isinstance(row, dict) else (None, None)
        if a and b:
            rows.append((abs(a[0] - b[0]) + abs(a[1] - b[1]), row))
    longest = max(rows, key=lambda r: r[0], default=None)
    return dict(
        count=len(rows),
        longest=round(longest[0], 1) if longest else None,
        longest_item=longest[1].get("item") if longest else None,
        longest_owner=longest[1].get("owner") if longest else None,
    )


def board_tidiness(board, site_plan=None):
    """Tidiness metrics of a board (see the module docstring): dict(blocks=[...], totals={...}, connections={...})."""
    groups = [(name, _belts(entities), rects) for name, entities, rects in board_blocks(board)]
    emitters = _emitters([row for _, rows, _ in groups for row in rows])
    every = [r for _, _, rects in groups for r in rects or []] + _plan_rects(site_plan)
    blocks = []
    for name, rows, rects in groups:
        if not rows:
            continue
        belts = len(rows)
        outside = None if rects is None else sum(not _inside(x, y, rects) for _, x, y, _, _ in rows)
        blocks.append(
            dict(
                block=name,
                belts=belts,
                outside=outside,
                outside_share=round(outside / belts, 3) if outside is not None else None,
                undergrounds=sum(kind == "underground" for kind, *_ in rows),
                splitters=sum(kind == "splitter" for kind, *_ in rows),
                turns=sum(kind == "belt" and _is_curve(x, y, d, emitters) for kind, x, y, d, _ in rows),
                outside_plan=sum(not _inside(x, y, every) for _, x, y, _, _ in rows),
            )
        )
    reserved = [b for b in blocks if b["outside"] is not None]
    belts = sum(b["belts"] for b in blocks)
    reserved_belts = sum(b["belts"] for b in reserved)
    outside = sum(b["outside"] for b in reserved)
    totals = dict(
        blocks=len(blocks),
        belts=belts,
        outside=outside,
        outside_share=round(outside / reserved_belts, 3) if reserved_belts else None,
        unreserved_belts=belts - reserved_belts,
        outside_plan=sum(b["outside_plan"] for b in blocks),
        undergrounds=sum(b["undergrounds"] for b in blocks),
        splitters=sum(b["splitters"] for b in blocks),
        turns=sum(b["turns"] for b in blocks),
    )
    board = board if isinstance(board, dict) else {}
    blocks.sort(key=lambda b: (-(b["outside"] or 0), -b["belts"], b["block"]))
    return dict(blocks=blocks, totals=totals, connections=connection_metrics(board.get("connections")))


def run_tidiness(root):
    """``board_tidiness`` of a run folder's ``pcb-board.json`` and ``site-plan.json`` (read-only), or None."""
    import json
    from pathlib import Path

    def read(name):
        try:
            return json.loads((Path(root) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    board = read("pcb-board.json")
    if not isinstance(board, dict):
        return None
    plan = read("site-plan.json")
    if isinstance(plan, dict) and plan.get("world_id") not in (None, board.get("world_id")):
        plan = None  # an older map's plan
    return board_tidiness(board, plan)
