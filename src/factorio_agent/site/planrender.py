"""SVG of a world map plus master-plan orientation marks.

On top of :func:`site.render.render_svg` it draws, per zone, a green bar on
the input edge and a red bar on the output edge, a small "in/out" label, and
an arrow along every corridor segment in its travel direction. Same pixel
convention as ``render``: ``scale`` pixels per tile, origin at the north-west
corner of the surveyed bounds.
"""

from __future__ import annotations

from .geometry import CHUNK_TILES, Rect
from .render import render_svg
from .worldmap import WorldMap

INPUT_COLOUR = "#1b9e3e"
OUTPUT_COLOUR = "#d62828"
ARROW_COLOUR = "#111111"
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

    def text(self, x: float, y: float, label: str) -> None:
        px, py = self._px(x, y)
        self.parts.append(f'<text x="{px + 2}" y="{py + 24}" font-size="10" font-family="sans-serif">{label}</text>')


def render_plan_svg(world: WorldMap, plan, scale: int = 4) -> str:
    """``render_svg(world)`` with input/output edges and corridor arrows."""
    svg = render_svg(world, scale=scale)
    marks = _Marks(world.bounds() or Rect(0, 0, CHUNK_TILES, CHUNK_TILES), scale)
    for zone in plan.zones:
        if zone.input_side:
            marks.line(_edge_line(zone.rect, zone.input_side), INPUT_COLOUR, 4)
        if zone.output_side:
            marks.line(_edge_line(zone.rect, zone.output_side), OUTPUT_COLOUR, 4)
        marks.text(zone.rect.x0, zone.rect.y0, f"in {zone.input_side or '-'} out {zone.output_side or '-'}")
    for corridor in plan.corridors:
        for segment in corridor.segments:
            marks.line(_segment_line(segment.rect, segment.direction), ARROW_COLOUR, 1.5, arrow=True)
    for allocation in plan.allocations:
        marks.line(_segment_line(allocation.rect, allocation.flow), ARROW_COLOUR, 1.5, arrow=True)
    body = MARKER + "\n" + "\n".join(marks.parts)
    return svg.replace("</svg>", body + "\n</svg>")
