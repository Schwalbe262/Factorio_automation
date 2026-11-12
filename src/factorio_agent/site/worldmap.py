"""In-memory tile map of the world, filled by bulk surveys and kept in Python.

See :mod:`factorio_agent.site.geometry` for coordinate conventions. All data is
stored sparsely; a tile with no entry in any layer is plain buildable land.
Only tiles inside surveyed chunks may be asked about: any query on an
unsurveyed tile raises :class:`UnsurveyedError` instead of answering "free".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Rect, Tile, chunk_of, chunks_in, plan_rect, CHUNK_TILES

LAND = "land"
WATER = "water"
UNBUILDABLE = "unbuildable"
# Integer codes used in survey replies and in the JSON cache.
TERRAIN_CODES = {1: WATER, 2: UNBUILDABLE}
TERRAIN_NUMBERS = {WATER: 1, UNBUILDABLE: 2}

# Feature kinds. Codes are 1-based positions in this tuple (survey + JSON).
# tree/rock: natural and removable by mining. fixed: natural, not minable.
# cliff: not clearable. entity: built player-force entity. plan: reserved by a
# planner for a not-yet-built entity or block.
FEATURE_KINDS = ("tree", "rock", "fixed", "cliff", "entity", "plan")
CLEARABLE_KINDS = frozenset({"tree", "rock"})
SURVEYED_KINDS = frozenset({"tree", "rock", "fixed", "cliff", "entity"})


class UnsurveyedError(LookupError):
    """Raised when a query touches a tile whose chunk has not been surveyed."""

    def __init__(self, x: int, y: int):
        super().__init__(f"tile ({x}, {y}) is in unsurveyed chunk {chunk_of(x, y)}")
        self.tile = (x, y)


@dataclass(frozen=True)
class Feature:
    """Something standing on the map: an obstacle, a built entity or a plan.

    ``rect`` is the set of tiles it blocks. ``owner`` is only used for plans
    (who reserved the space).
    """

    kind: str
    name: str
    rect: Rect
    owner: str | None = None

    @property
    def clearable(self) -> bool:
        return self.kind in CLEARABLE_KINDS

    def key(self) -> tuple:
        return (self.kind, self.name, self.rect, self.owner)


@dataclass(frozen=True)
class Well:
    """A fluid resource entity such as crude oil.

    ``centre`` is the tile holding the entity position; the footprint is the
    ``size`` x ``size`` square of tiles centred on it (3 x 3 for crude oil).
    ``amount`` is the raw game amount (crude oil: 300000 = 100 % yield).
    """

    name: str
    centre: Tile
    amount: int
    size: int = 3

    @property
    def rect(self) -> Rect:
        half = self.size // 2
        x, y = self.centre
        return Rect(x - half, y - half, x - half + self.size, y - half + self.size)


@dataclass
class RectReport:
    """Result of :meth:`WorldMap.free_rect`.

    ``blocked`` maps a reason ("water", "unbuildable", "cliff", "fixed",
    "entity", "plan", "reserved", and "tree"/"rock" when clearing is not
    allowed) to the blocked tiles. ``clearable`` lists trees and rocks that
    must be mined first. ``resources`` maps resource tiles under the rect to
    the resource name; resources never block by themselves.
    """

    rect: Rect
    blocked: dict[str, list[Tile]] = field(default_factory=dict)
    clearable: list[Feature] = field(default_factory=list)
    resources: dict[Tile, str] = field(default_factory=dict)

    @property
    def free(self) -> bool:
        return not self.blocked

    def add_blocked(self, reason: str, tile: Tile) -> None:
        self.blocked.setdefault(reason, []).append(tile)


class WorldMap:
    """Sparse tile map. Units are tiles; see the geometry module for axes."""

    def __init__(self) -> None:
        self._chunks: set[tuple[int, int]] = set()
        self._terrain: dict[Tile, str] = {}
        self._ore: dict[Tile, tuple[str, int]] = {}
        self._wells: dict[Tile, Well] = {}
        self._well_cover: dict[Tile, Tile] = {}
        self._features: dict[tuple, Feature] = {}
        self._feature_tiles: dict[Tile, list[Feature]] = {}
        self._features_by_chunk: dict[tuple[int, int], set[tuple]] = {}
        self._zones: list[tuple[str, Rect]] = []
        self._reserved: dict[Tile, str] = {}

    # ----- survey bookkeeping -------------------------------------------
    def mark_surveyed(self, rect: Rect) -> None:
        """Record that every chunk of a chunk-aligned rect is now known."""
        if rect.x0 % CHUNK_TILES or rect.y0 % CHUNK_TILES or rect.x1 % CHUNK_TILES or rect.y1 % CHUNK_TILES:
            raise ValueError(f"surveyed rect must be chunk aligned: {rect}")
        self._chunks.update(chunks_in(rect))

    def is_surveyed(self, x: int, y: int) -> bool:
        return chunk_of(x, y) in self._chunks

    def surveyed_chunks(self) -> frozenset[tuple[int, int]]:
        return frozenset(self._chunks)

    def bounds(self) -> Rect | None:
        """Smallest rect covering every surveyed chunk, or None when empty."""
        if not self._chunks:
            return None
        xs = [cx for cx, _ in self._chunks]
        ys = [cy for _, cy in self._chunks]
        c = CHUNK_TILES
        return Rect(min(xs) * c, min(ys) * c, (max(xs) + 1) * c, (max(ys) + 1) * c)

    def _require(self, x: int, y: int) -> None:
        if chunk_of(x, y) not in self._chunks:
            raise UnsurveyedError(x, y)

    # ----- low-level mutators (used by survey and JSON loading) ----------
    def clear_region(self, rect: Rect) -> None:
        """Forget surveyed data owned by ``rect`` before it is re-surveyed.

        Removes terrain and ore tiles inside it, wells whose centre is inside
        it, and surveyed features whose anchor tile is inside it. Plans and
        reservations are kept.
        """
        for tile in rect.tiles():
            self._terrain.pop(tile, None)
            self._ore.pop(tile, None)
            well = self._wells.get(tile)
            if well is not None:
                self._remove_well(well)
        for chunk in chunks_in(rect):
            for key in list(self._features_by_chunk.get(chunk, ())):
                feature = self._features[key]
                if feature.kind in SURVEYED_KINDS and rect.contains(*feature.rect.anchor()):
                    self.remove_feature(feature)

    def set_terrain(self, x: int, y: int, terrain: str) -> None:
        """Set terrain of one tile: LAND, WATER or UNBUILDABLE."""
        if terrain == LAND:
            self._terrain.pop((x, y), None)
        elif terrain in TERRAIN_NUMBERS:
            self._terrain[(x, y)] = terrain
        else:
            raise ValueError(f"unknown terrain {terrain!r}")

    def set_ore(self, x: int, y: int, name: str, amount: int) -> None:
        """Record a 1 x 1 resource entity (ore) on a tile."""
        self._ore[(x, y)] = (name, int(amount))

    def add_well(self, well: Well) -> None:
        """Record a fluid resource entity (e.g. crude oil) by its centre tile."""
        old = self._wells.get(well.centre)
        if old is not None:
            self._remove_well(old)
        self._wells[well.centre] = well
        for tile in well.rect.tiles():
            self._well_cover[tile] = well.centre

    def _remove_well(self, well: Well) -> None:
        del self._wells[well.centre]
        for tile in well.rect.tiles():
            if self._well_cover.get(tile) == well.centre:
                del self._well_cover[tile]

    def add_feature(self, feature: Feature) -> Feature:
        """Add an obstacle, entity or plan. Adding the same feature twice is a no-op."""
        key = feature.key()
        if key in self._features:
            return self._features[key]
        self._features[key] = feature
        self._features_by_chunk.setdefault(chunk_of(*feature.rect.anchor()), set()).add(key)
        for tile in feature.rect.tiles():
            self._feature_tiles.setdefault(tile, []).append(feature)
        return feature

    def remove_feature(self, feature: Feature) -> None:
        key = feature.key()
        if self._features.pop(key, None) is None:
            return
        self._features_by_chunk[chunk_of(*feature.rect.anchor())].discard(key)
        for tile in feature.rect.tiles():
            rows = self._feature_tiles[tile]
            rows.remove(feature)
            if not rows:
                del self._feature_tiles[tile]

    # ----- read access to whole layers (render, JSON, patches) ------------
    def terrain_tiles(self) -> dict[Tile, str]:
        """Non-land tiles: ``{(x, y): WATER or UNBUILDABLE}`` (a copy)."""
        return dict(self._terrain)

    def ore_tiles(self) -> dict[Tile, tuple[str, int]]:
        """``{(x, y): (resource name, amount)}`` for 1 x 1 resources (a copy)."""
        return dict(self._ore)

    def wells(self) -> list[Well]:
        return list(self._wells.values())

    def features(self, kind: str | None = None) -> list[Feature]:
        return [f for f in self._features.values() if kind is None or f.kind == kind]

    def zones(self) -> list[tuple[str, Rect]]:
        """Reserved zones as ``(owner, rect)`` in reservation order."""
        return list(self._zones)

    # ----- per-tile queries -----------------------------------------------
    def peek_terrain(self, x: int, y: int) -> str | None:
        """Terrain of a tile, or None if unsurveyed (never raises)."""
        if not self.is_surveyed(x, y):
            return None
        return self._terrain.get((x, y), LAND)

    def terrain(self, x: int, y: int) -> str:
        self._require(x, y)
        return self._terrain.get((x, y), LAND)

    def resource_at(self, x: int, y: int) -> tuple[str, int] | None:
        """``(name, amount)`` of the ore or well covering the tile, else None."""
        self._require(x, y)
        ore = self._ore.get((x, y))
        if ore is not None:
            return ore
        centre = self._well_cover.get((x, y))
        if centre is None:
            return None
        well = self._wells[centre]
        return (well.name, well.amount)

    def features_at(self, x: int, y: int) -> tuple[Feature, ...]:
        self._require(x, y)
        return tuple(self._feature_tiles.get((x, y), ()))

    def occupied_by(self, x: int, y: int) -> str | None:
        """Name of the built or planned entity on the tile, else None."""
        for feature in self.features_at(x, y):
            if feature.kind in ("entity", "plan"):
                return feature.name
        return None

    def is_buildable(self, x: int, y: int, *, allow_clearable: bool = True) -> bool:
        """True when the tile is land with nothing standing on it.

        Trees and rocks count as free when ``allow_clearable``. Resources and
        reservations do not affect buildability.
        """
        if self.terrain(x, y) != LAND:
            return False
        for feature in self.features_at(x, y):
            if not (allow_clearable and feature.clearable):
                return False
        return True

    def free_rect(
        self, x0: int, y0: int, x1: int, y1: int, *, allow_clearable: bool = True, owner: str | None = None
    ) -> RectReport:
        """Check the half-open tile rect ``[x0, x1) x [y0, y1)``.

        Tiles reserved by someone other than ``owner`` are blocked as
        "reserved". Raises :class:`UnsurveyedError` if any tile is unsurveyed.
        """
        rect = Rect(x0, y0, x1, y1)
        report = RectReport(rect)
        seen: set[tuple] = set()
        for x, y in rect.tiles():
            terrain = self.terrain(x, y)
            if terrain != LAND:
                report.add_blocked(terrain, (x, y))
            reserved = self._reserved.get((x, y))
            if reserved is not None and reserved != owner:
                report.add_blocked("reserved", (x, y))
            resource = self.resource_at(x, y)
            if resource is not None:
                report.resources[(x, y)] = resource[0]
            for feature in self._feature_tiles.get((x, y), ()):
                if allow_clearable and feature.clearable:
                    if feature.key() not in seen:
                        seen.add(feature.key())
                        report.clearable.append(feature)
                else:
                    report.add_blocked(feature.kind, (x, y))
        return report

    # ----- planning: plans and zoning -------------------------------------
    def mark_occupied(self, rect: Rect, name: str = "planned-block", owner: str | None = None) -> Feature:
        """Reserve the tiles of a planned but unbuilt block or entity."""
        return self.add_feature(Feature("plan", name, rect, owner))

    def apply_plan(self, entities: list[dict], owner: str | None = None) -> list[Feature]:
        """Mark every row of an entity plan (``board.entity`` dicts) as planned."""
        return [self.mark_occupied(plan_rect(e), e["name"], owner) for e in entities]

    def clear_plans(self, owner: str | None = None) -> int:
        """Drop planned features (all, or only those of ``owner``). Returns the count."""
        plans = [f for f in self._features.values() if f.kind == "plan" and (owner is None or f.owner == owner)]
        for feature in plans:
            self.remove_feature(feature)
        return len(plans)

    def reserve(self, rect: Rect, owner: str) -> None:
        """Zone ``rect`` for ``owner``. Raises ValueError on another owner's tile."""
        clashes = [t for t in rect.tiles() if self._reserved.get(t, owner) != owner]
        if clashes:
            raise ValueError(f"{owner!r} overlaps zone of {self._reserved[clashes[0]]!r} at {clashes[0]}")
        self._zones.append((owner, rect))
        for tile in rect.tiles():
            self._reserved[tile] = owner

    def release(self, owner: str) -> None:
        """Remove every zone held by ``owner``."""
        self._zones = [(o, r) for o, r in self._zones if o != owner]
        self._reserved = {t: o for t, o in self._reserved.items() if o != owner}

    def reserved_by(self, x: int, y: int) -> str | None:
        return self._reserved.get((x, y))

    # ----- delegated helpers ----------------------------------------------
    def resource_patches(self, name: str | None = None):
        """Connected resource patches; see :func:`site.patches.find_patches`."""
        from .patches import find_patches

        return find_patches(self, name)

    def water_tiles_near(self, x: int, y: int, radius: int) -> list[Tile]:
        """Water tiles within ``radius`` tiles, nearest first; see site.patches."""
        from .patches import water_tiles_near

        return water_tiles_near(self, x, y, radius)

    def nearest_shore(self, x: int, y: int, radius: int = 64):
        """Nearest water tile next to buildable land; see site.patches."""
        from .patches import nearest_shore

        return nearest_shore(self, x, y, radius)

    def to_json(self) -> str:
        from .mapjson import dump_map

        return dump_map(self)

    @classmethod
    def from_json(cls, text: str) -> WorldMap:
        from .mapjson import load_map

        return load_map(text)
