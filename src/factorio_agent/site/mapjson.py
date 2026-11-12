"""Compact JSON cache format for :class:`WorldMap`.

Layout (version 1)::

    {"format": "factorio-agent-worldmap", "version": 1,
     "chunks": [[cx, cy], ...],
     "names": [str, ...],                  string table, indexed from 0
     "terrain": [[y, x, n, code], ...],    row runs, code 1 water 2 unbuildable
     "ore": [[y, x, n, name_index], ...],  row runs of 1 x 1 resources
     "ore_amounts": [int, ...],            one per ore tile, in run order
     "wells": [[x, y, name_index, amount, size], ...]   centre tile
     "features": [[kind_code, name_index, x0, y0, w, h, owner_index], ...]
     "zones": [[owner_index, x0, y0, x1, y1], ...]}

``kind_code`` is the 1-based position in ``worldmap.FEATURE_KINDS``;
``owner_index`` is -1 for no owner.
"""

from __future__ import annotations

import json

from .geometry import Rect, CHUNK_TILES
from .rle import decode_runs, encode_runs
from .worldmap import FEATURE_KINDS, TERRAIN_CODES, TERRAIN_NUMBERS, Feature, Well, WorldMap

FORMAT = "factorio-agent-worldmap"
VERSION = 1


class _Names:
    """String table that hands out stable indices.

    Pre-loaded with every name, sorted, so equal maps give identical JSON.
    """

    def __init__(self, names) -> None:
        self.names: list[str] = sorted(set(names))
        self._index: dict[str, int] = {name: i for i, name in enumerate(self.names)}

    def index(self, name: str | None) -> int:
        if name is None:
            return -1
        return self._index[name]


def _feature_row(feature: Feature, names: _Names) -> list:
    r = feature.rect
    kind = FEATURE_KINDS.index(feature.kind) + 1
    return [kind, names.index(feature.name), r.x0, r.y0, r.width, r.height, names.index(feature.owner)]


def _all_names(world: WorldMap) -> list[str]:
    found = [name for name, _ in world.ore_tiles().values()]
    found += [w.name for w in world.wells()]
    found += [f.name for f in world.features()]
    found += [f.owner for f in world.features() if f.owner is not None]
    found += [owner for owner, _ in world.zones()]
    return found


def dump_map(world: WorldMap) -> str:
    """Serialise the whole map to a compact JSON string (canonical order)."""
    names = _Names(_all_names(world))
    terrain = {t: TERRAIN_NUMBERS[v] for t, v in world.terrain_tiles().items()}
    ore = world.ore_tiles()
    ore_codes = {t: names.index(name) for t, (name, _) in ore.items()}
    ore_runs = encode_runs(ore_codes)
    amounts = [ore[(x, y)][1] for x, y, _ in decode_runs(ore_runs)]
    wells = [[w.centre[0], w.centre[1], names.index(w.name), w.amount, w.size] for w in world.wells()]
    features = [_feature_row(f, names) for f in world.features()]
    zones = [[names.index(owner), *rect.to_list()] for owner, rect in world.zones()]
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "chunks": sorted([list(c) for c in world.surveyed_chunks()]),
        "names": names.names,
        "terrain": encode_runs(terrain),
        "ore": ore_runs,
        "ore_amounts": amounts,
        "wells": sorted(wells),
        "features": sorted(features),
        "zones": zones,
    }
    return json.dumps(payload, separators=(",", ":"))


def load_map(text: str) -> WorldMap:
    """Rebuild a map written by :func:`dump_map`."""
    data = json.loads(text)
    if data.get("format") != FORMAT or data.get("version") != VERSION:
        raise ValueError("not a version 1 factorio-agent world map")
    names = data["names"]
    world = WorldMap()
    for cx, cy in data["chunks"]:
        x, y = cx * CHUNK_TILES, cy * CHUNK_TILES
        world.mark_surveyed(Rect(x, y, x + CHUNK_TILES, y + CHUNK_TILES))
    for x, y, code in decode_runs(data["terrain"]):
        world.set_terrain(x, y, TERRAIN_CODES[code])
    amounts = data["ore_amounts"]
    for position, (x, y, name_index) in enumerate(decode_runs(data["ore"])):
        world.set_ore(x, y, names[name_index], amounts[position])
    for x, y, name_index, amount, size in data["wells"]:
        world.add_well(Well(names[name_index], (x, y), amount, size))
    for kind, name_index, x0, y0, width, height, owner_index in data["features"]:
        owner = names[owner_index] if owner_index >= 0 else None
        rect = Rect(x0, y0, x0 + width, y0 + height)
        world.add_feature(Feature(FEATURE_KINDS[kind - 1], names[name_index], rect, owner))
    for owner_index, x0, y0, x1, y1 in data["zones"]:
        world.reserve(Rect(x0, y0, x1, y1), names[owner_index])
    return world
