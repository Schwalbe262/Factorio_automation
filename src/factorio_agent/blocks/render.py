"""One character per tile, north up, for eyeballing a plan.

Legend: belts ``> < ^ v`` (flow), machines ``M``, plain inserter ``i``, fast
inserter ``f``, long-handed ``L``, other inserters ``I``, poles ``+``, input
port ``@``, output port ``O``, chest ``C``, reserved future machine ``:``, empty ``.``.
"""

from __future__ import annotations

from typing import Any

from .geometry import footprint, xy

BELT_GLYPHS = {0: "^", 4: ">", 8: "v", 12: "<"}
NAMED_GLYPHS = {"inserter": "i", "fast-inserter": "f", "long-handed-inserter": "L"}
TYPE_GLYPHS = {"assembling-machine": "M", "inserter": "I", "electric-pole": "+", "container": "C"}


def _glyph(catalog: Any, e: dict, letters: dict | None = None) -> str:
    kind = (catalog.entities.get(e["name"]) or {}).get("type", "")
    if letters and e.get("recipe") in letters:
        return letters[e["recipe"]]
    if kind == "transport-belt":
        return BELT_GLYPHS[e["direction"]]
    return NAMED_GLYPHS.get(e["name"]) or TYPE_GLYPHS.get(kind, "?")


def render_ascii(catalog: Any, plan: dict, letters: dict | None = None) -> str:
    """``letters`` optionally maps a machine recipe to its glyph (cluster pictures)."""
    cells: dict[tuple[float, float], str] = {}
    for slot in plan.get("expansion_slots", []):
        for tile in footprint(catalog, dict(slot, direction=0)):
            cells[tile] = ":"
    for e in plan["entities"]:
        for tile in footprint(catalog, e):
            cells[tile] = _glyph(catalog, e, letters)
    for port in plan["ports"]:
        cells[xy(port)] = "@" if port["direction"] == "input" else "O"
    xs = sorted({t[0] for t in cells})
    ys = sorted({t[1] for t in cells})
    lines = []
    y = ys[0]
    while y <= ys[-1]:
        x = xs[0]
        row = []
        while x <= xs[-1]:
            row.append(cells.get((x, y), "."))
            x += 1
        lines.append("".join(row))
        y += 1
    return "\n".join(lines)
