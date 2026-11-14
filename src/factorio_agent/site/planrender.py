"""SVG of a world map plus master-plan orientation marks.

On top of :func:`site.render.render_svg` it draws, per zone, a green bar on
the input edge and a red bar on the output edge, a small "in/out" label, and
an arrow along every corridor segment in its travel direction. Rail zones
and the rail trunk are outlined in purple with an arrow along the track
axis (the zone's ``growth``) and along each trunk segment. Same pixel
convention as ``render``: ``scale`` pixels per tile, origin at the north-west
corner of the surveyed bounds.

:func:`render_dashboard_svg` adds what the read-only run dashboard
(``tools/dashboard.py``) needs on top: the bus lane table, allocation
outlines and the blocks registered on the board (``pcb-board.json``), so the
plan and what was actually registered can be compared on one map, and the
board's built belts as tile cells, coloured by whether they lie inside their
block's ``reserved_area`` (``layout_tidiness.belt_cells``).
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

from .geometry import CHUNK_TILES, Rect
from .render import COLOURS, colour_for, render_svg
from .worldmap import WorldMap

INPUT_COLOUR = "#1b9e3e"
OUTPUT_COLOUR = "#d62828"
ARROW_COLOUR = "#111111"
RAIL_COLOUR = "#7b2cbf"
ALLOCATION_COLOUR = "#e07b00"
BOARD_COLOUR = "#00a3a3"
START_COLOUR = "#ff00ff"
# Built belt cells (board entities): inside the block's reservation, outside it, block without one.
BELT_COLOURS = {"inside": "#3a3a3a", "outside": "#e63946", "unreserved": "#5a7bd8"}
UNDERGROUND_COLOURS = {"inside": "#8a8a8a", "outside": "#ff9f1c", "unreserved": "#9fb3ea"}
# Bus lane colours: plates take their ore's colour, other materials a hashed one.
LANE_COLOURS = {"iron-plate": COLOURS["iron-ore"], "copper-plate": COLOURS["copper-ore"]}
MARKER = (
    '<defs><marker id="plan-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" '
    f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{ARROW_COLOUR}"/></marker></defs>'
)


def _edge_line(rect: Rect, side: str) -> tuple[float, float, float, float]:
    if side == "N":
        return (rect.x0, rect.y0, rect.x1, rect.y0)
    if side == "S":
        return (rect.x0, rect.y1, rect.x1, rect.y1)
    if side == "W":
        return (rect.x0, rect.y0, rect.x0, rect.y1)
    return (rect.x1, rect.y0, rect.x1, rect.y1)


def _segment_line(rect: Rect, direction: str) -> tuple[float, float, float, float]:
    cx, cy = rect.centre()
    if direction == "E":
        return (rect.x0, cy, rect.x1, cy)
    if direction == "W":
        return (rect.x1, cy, rect.x0, cy)
    if direction == "S":
        return (cx, rect.y0, cx, rect.y1)
    return (cx, rect.y1, cx, rect.y0)


class _Marks:
    def __init__(self, bounds: Rect, scale: int):
        self.bounds = bounds
        self.scale = scale
        self.parts: list[str] = []

    def _px(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.bounds.x0) * self.scale, (y - self.bounds.y0) * self.scale)

    def line(self, coords, colour: str, width: float, arrow: bool = False) -> None:
        x0, y0 = self._px(coords[0], coords[1])
        x1, y1 = self._px(coords[2], coords[3])
        tail = ' marker-end="url(#plan-arrow)"' if arrow else ""
        self.parts.append(
            f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{colour}" stroke-width="{width}"{tail}/>'
        )

    def outline(self, rect: Rect, colour: str) -> None:
        x0, y0 = self._px(rect.x0, rect.y0)
        x1, y1 = self._px(rect.x1, rect.y1)
        self.parts.append(
            f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" '
            f'stroke="{colour}" stroke-width="3" stroke-dasharray="6,3"/>'
        )

    def text(self, x: float, y: float, label: str) -> None:
        px, py = self._px(x, y)
        self.parts.append(f'<text x="{px + 2}" y="{py + 24}" font-size="10" font-family="sans-serif">{label}</text>')


def render_plan_svg(world: WorldMap, plan, scale: int = 4) -> str:
    """``render_svg(world)`` with input/output edges and corridor arrows."""
    svg = render_svg(world, scale=scale)
    marks = _Marks(world.bounds() or Rect(0, 0, CHUNK_TILES, CHUNK_TILES), scale)
    for zone in plan.zones:
        entries = [zone.input_side] + [p.side for p in zone.ports if p.role == "in"]
        for side in dict.fromkeys(s for s in entries if s and s != zone.output_side):
            marks.line(_edge_line(zone.rect, side), INPUT_COLOUR, 4)
        if zone.output_side:
            marks.line(_edge_line(zone.rect, zone.output_side), OUTPUT_COLOUR, 4)
        marks.text(zone.rect.x0, zone.rect.y0, f"in {zone.input_side or '-'} out {zone.output_side or '-'}")
    for zone in plan.zones:
        if zone.kind == "rail":
            marks.outline(zone.rect, RAIL_COLOUR)
            if zone.growth:
                marks.line(_segment_line(zone.rect, zone.growth), RAIL_COLOUR, 2.5, arrow=True)
    for corridor in plan.corridors:
        colour = RAIL_COLOUR if corridor.kind == "rail" else ARROW_COLOUR
        for segment in corridor.segments:
            if corridor.kind == "rail":
                marks.outline(segment.rect, RAIL_COLOUR)
            marks.line(_segment_line(segment.rect, segment.direction), colour, 1.5, arrow=True)
    for allocation in plan.allocations:
        marks.line(_segment_line(allocation.rect, allocation.flow), ARROW_COLOUR, 1.5, arrow=True)
    body = MARKER + "\n" + "\n".join(marks.parts)
    return svg.replace("</svg>", body + "\n</svg>")


def lane_colour(material: str) -> str:
    return LANE_COLOURS.get(material) or colour_for(material)


def board_rect(block) -> Rect | None:
    """Whole-tile rect of a board block's ``reserved_area`` ``[[x0, y0], [x1, y1]]`` (None if absent/invalid)."""
    area = block.get("reserved_area") if isinstance(block, dict) else None
    try:
        (x0, y0), (x1, y1) = area
        x0, x1 = sorted((math.floor(float(x0)), math.ceil(float(x1))))
        y0, y1 = sorted((math.floor(float(y0)), math.ceil(float(y1))))
    except (TypeError, ValueError, OverflowError):
        return None
    return Rect(x0, y0, x1, y1)


def _label(marks: _Marks, rect: Rect, label: str, colour: str, kind: str, below: bool = False) -> None:
    s = marks.scale
    px, py = marks._px(rect.x0, rect.y1 if below else rect.y0)
    size = 5 * s
    y = py + size + s if below else py - s
    marks.parts.append(
        f'<text data-kind="{kind}" x="{px}" y="{y}" font-size="{size}" font-family="sans-serif" font-weight="bold" '
        f'fill="{colour}" stroke="#ffffff" stroke-width="{s * 0.6}" paint-order="stroke">{escape(label)}</text>'
    )


def _box(marks: _Marks, rect: Rect, colour: str, width: float, extra: str) -> None:
    x0, y0 = marks._px(rect.x0, rect.y0)
    x1, y1 = marks._px(rect.x1, rect.y1)
    marks.parts.append(
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" stroke="{colour}" stroke-width="{width}"{extra}/>'
    )


def belt_layer(marks: _Marks, blocks=None, shared_bus=None) -> None:
    """The built belts of the board as tile cells: one ``<path>`` per (belt/underground, state).

    State ``inside`` / ``outside`` the block's own reservation, ``unreserved`` for a block
    without one; ~3,000 belts stay a few short paths (``data-kind="belts"`` / ``"undergrounds"``,
    ``data-state``, ``data-count``)."""
    from ..layout_tidiness import belt_cells

    cells = belt_cells({"blocks": blocks or {}, "shared_main_bus": shared_bus})
    groups: dict[tuple[str, str], set] = {}
    counts: dict[tuple[str, str], int] = {}
    for cell in cells:
        state = "unreserved" if cell["inside"] is None else ("inside" if cell["inside"] else "outside")
        key = ("undergrounds" if cell["kind"] == "underground" else "belts", state)
        groups.setdefault(key, set()).update(cell["tiles"])
        counts[key] = counts.get(key, 0) + 1
    s = marks.scale
    for (kind, state), tiles in sorted(groups.items()):
        colour = (UNDERGROUND_COLOURS if kind == "undergrounds" else BELT_COLOURS)[state]
        d = "".join("M{0:g} {1:g}h{2:g}v{2:g}h{3:g}z".format(*marks._px(x, y), s, -s) for x, y in sorted(tiles))
        marks.parts.append(
            f'<path data-kind="{kind}" data-state="{state}" data-count="{counts[(kind, state)]}" d="{d}" '
            f'fill="{colour}" fill-opacity="0.85"/>'
        )


def render_dashboard_svg(world: WorldMap, plan, blocks=None, scale: int = 2, *, shared_bus=None) -> str:
    """``render_plan_svg`` plus bus lanes, allocation outlines, board blocks and built belts.

    ``blocks`` is the ``pcb-board.json`` ``blocks`` mapping (name -> block);
    each block with a valid ``reserved_area`` is drawn as a filled outline
    with its name, and the belts of every block's ``entities`` (plus those of
    ``shared_bus``, the board's ``shared_main_bus``) as cells (``belt_layer``).
    Overlay elements carry ``data-kind`` / ``data-name``.
    """
    svg = render_plan_svg(world, plan, scale=scale)
    marks = _Marks(world.bounds() or Rect(0, 0, CHUNK_TILES, CHUNK_TILES), scale)
    bus = plan.bus
    for lane in bus.lanes:
        if not lane.material:
            continue
        x0, y0 = marks._px(*bus.local_point(0, lane.offset + 0.5))
        x1, y1 = marks._px(*bus.local_point(bus.length, lane.offset + 0.5))
        marks.parts.append(
            f'<line data-kind="lane" data-name="{escape(lane.material)}" x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" '
            f'stroke="{lane_colour(lane.material)}" stroke-width="{scale * 0.7}"/>'
        )
    for allocation in plan.allocations:
        extra = (
            f' fill="none" stroke-dasharray="{scale * 3},{scale * 1.5}" data-kind="allocation"'
            f' data-name="{escape(allocation.name)}"'
        )
        _box(marks, allocation.rect, ALLOCATION_COLOUR, scale * 0.8, extra)
        _label(marks, allocation.rect, allocation.name, ALLOCATION_COLOUR, "allocation", below=True)
    for name, block in sorted((blocks or {}).items(), key=lambda item: str(item[0])):
        rect = board_rect(block)
        if rect is None:
            continue
        extra = f' fill="{BOARD_COLOUR}" fill-opacity="0.18" data-kind="board" data-name="{escape(str(name))}"'
        _box(marks, rect, BOARD_COLOUR, scale * 1.0, extra)
        _label(marks, rect, str(name), BOARD_COLOUR, "board")
    belt_layer(marks, blocks, shared_bus if isinstance(shared_bus, dict) else None)
    sx, sy = marks._px(*plan.start)
    marks.parts.append(
        f'<circle data-kind="start" cx="{sx}" cy="{sy}" r="{scale * 2}" fill="{START_COLOUR}" stroke="#000" '
        f'stroke-width="{scale * 0.4}"/>'
    )
    return svg.replace("</svg>", '<g id="dashboard-overlay">\n' + "\n".join(marks.parts) + "\n</g>\n</svg>")


def dashboard_focus(world: WorldMap, plan, blocks=None, scale: int = 2, margin: int = 24) -> list[float]:
    """SVG ``viewBox`` ``[x, y, w, h]`` around the built-up core.

    The core is the bus, smelting and power zones, the allocations and the
    board blocks (far outposts and rail loaders stay outside); the whole
    surveyed map when none of them exists.
    """
    bounds = world.bounds() or Rect(0, 0, CHUNK_TILES, CHUNK_TILES)
    whole = [0, 0, bounds.width * scale, bounds.height * scale]
    rects = [z.rect for z in plan.zones if z.name in ("bus", "smelting", "power")]
    rects += [a.rect for a in plan.allocations]
    rects += [r for r in (board_rect(b) for b in (blocks or {}).values()) if r is not None]
    if not rects:
        return whole
    x0 = max(bounds.x0, min(r.x0 for r in rects) - margin)
    y0 = max(bounds.y0, min(r.y0 for r in rects) - margin)
    x1 = min(bounds.x1, max(r.x1 for r in rects) + margin)
    y1 = min(bounds.y1, max(r.y1 for r in rects) + margin)
    if x1 <= x0 or y1 <= y0:
        return whole
    return [(x0 - bounds.x0) * scale, (y0 - bounds.y0) * scale, (x1 - x0) * scale, (y1 - y0) * scale]
