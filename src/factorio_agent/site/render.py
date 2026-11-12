"""Standalone SVG rendering of a :class:`WorldMap` (no dependencies).

One map tile becomes ``scale`` x ``scale`` SVG pixels; the SVG origin is the
north-west corner of the surveyed bounds. Usage from a shell::

    python -m factorio_agent.site.render <map.json> <out.svg>
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .geometry import CHUNK_TILES, Rect, plan_rect
from .rle import encode_runs
from .worldmap import UNBUILDABLE, WATER, WorldMap

COLOURS = {
    "land": "#e8e4d0",
    "unsurveyed": "#9a9a9a",
    WATER: "#3a78c2",
    UNBUILDABLE: "#2b2b2b",
    "iron-ore": "#7b93b5",
    "copper-ore": "#d07a3a",
    "coal": "#3a3a3a",
    "stone": "#c4ac7c",
    "uranium-ore": "#6bd13c",
    "crude-oil": "#8a3bb0",
    "tree": "#2f7d32",
    "rock": "#8d8478",
    "fixed": "#5d554b",
    "cliff": "#6b3e1f",
    "entity": "#f2a20c",
    "plan": "#d62828",
}


def colour_for(name: str) -> str:
    """Fixed colour for known names; a stable hashed colour otherwise."""
    if name in COLOURS:
        return COLOURS[name]
    digest = hashlib.sha1(name.encode()).hexdigest()
    return "#" + digest[:6]


class _Canvas:
    """Collects SVG elements in tile coordinates relative to ``origin``."""

    def __init__(self, bounds: Rect, scale: int):
        self.bounds = bounds
        self.scale = scale
        self.parts: list[str] = []

    def rect(self, rect: Rect, fill: str, extra: str = "") -> None:
        s = self.scale
        x = (rect.x0 - self.bounds.x0) * s
        y = (rect.y0 - self.bounds.y0) * s
        self.parts.append(f'<rect x="{x}" y="{y}" width="{rect.width * s}" height="{rect.height * s}" {fill}{extra}/>')

    def text(self, x: int, y: int, label: str) -> None:
        s = self.scale
        px = (x - self.bounds.x0) * s + 2
        py = (y - self.bounds.y0) * s + 12
        self.parts.append(f'<text x="{px}" y="{py}" font-size="11" font-family="sans-serif">{escape(label)}</text>')

    def svg(self) -> str:
        width = self.bounds.width * self.scale
        height = self.bounds.height * self.scale
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        )
        return "\n".join([head, *self.parts, "</svg>"]) + "\n"


def _draw_ground(canvas: _Canvas, world: WorldMap) -> None:
    b = canvas.bounds
    canvas.rect(b, f'fill="{COLOURS["unsurveyed"]}"')
    for cx, cy in sorted(world.surveyed_chunks()):
        x, y = cx * CHUNK_TILES, cy * CHUNK_TILES
        canvas.rect(Rect(x, y, x + CHUNK_TILES, y + CHUNK_TILES), f'fill="{COLOURS["land"]}"')
    cells = {tile: colour_for(terrain) for tile, terrain in world.terrain_tiles().items()}
    cells.update({tile: colour_for(name) for tile, (name, _) in world.ore_tiles().items()})
    palette = sorted(set(cells.values()))
    codes = {tile: palette.index(colour) for tile, colour in cells.items()}
    for y, x, n, code in encode_runs(codes):
        canvas.rect(Rect(x, y, x + n, y + 1), f'fill="{palette[code]}"')
    for well in world.wells():
        canvas.rect(well.rect, f'fill="{colour_for(well.name)}" stroke="#000" stroke-width="1"')


def _draw_features(canvas: _Canvas, world: WorldMap) -> None:
    for feature in sorted(world.features(), key=lambda f: (f.kind, f.rect.y0, f.rect.x0)):
        colour = colour_for(feature.kind)
        if feature.kind == "plan":
            canvas.rect(feature.rect, f'fill="none" stroke="{colour}" stroke-width="1"')
        else:
            canvas.rect(feature.rect, f'fill="{colour}"')


def _draw_zones(canvas: _Canvas, world: WorldMap) -> None:
    for owner, rect in world.zones():
        colour = colour_for("zone:" + owner)
        canvas.rect(rect, f'fill="{colour}" fill-opacity="0.25" stroke="{colour}" stroke-width="2"')
        canvas.text(rect.x0, rect.y0, owner)


def _draw_plans(canvas: _Canvas, plans) -> None:
    for entity in plans:
        rect = plan_rect(entity)
        canvas.rect(rect, 'fill="none" stroke="#c2185b" stroke-width="1.5"', f' data-name="{escape(entity["name"])}"')


def render_svg(world: WorldMap, *, plans=(), reservations: bool = True, scale: int = 4) -> str:
    """Draw the surveyed part of ``world`` as an SVG document string.

    ``plans`` is an iterable of entity-plan rows (``board.entity`` dicts),
    drawn as outlined boxes. Unsurveyed chunks inside the bounds are grey.
    """
    bounds = world.bounds() or Rect(0, 0, CHUNK_TILES, CHUNK_TILES)
    canvas = _Canvas(bounds, scale)
    _draw_ground(canvas, world)
    _draw_features(canvas, world)
    if reservations:
        _draw_zones(canvas, world)
    _draw_plans(canvas, plans)
    if bounds.contains(0, 0):
        canvas.rect(Rect(0, 0, 1, 1), 'fill="#ff00ff"')
    return canvas.svg()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m factorio_agent.site.render <map.json> <out.svg>", file=sys.stderr)
        return 2
    world = WorldMap.from_json(Path(args[0]).read_text(encoding="utf-8"))
    Path(args[1]).write_text(render_svg(world), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
