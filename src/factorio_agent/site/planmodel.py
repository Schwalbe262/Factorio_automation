"""Data classes of the site master plan (see :mod:`site.masterplan`).

Zones, corridors and allocations live in :mod:`site.planzones`. Every class
here is plain data with ``to_dict`` / ``from_dict`` so a plan can
be stored as JSON and reloaded. Units are tiles; rects are half-open
:class:`Rect` boxes (see :mod:`site.geometry`). Directions are compass letters
"N", "E", "S", "W" (y grows south). ``growth`` is a compass letter or "" when
a zone has no preferred growth direction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from .geometry import Rect
from .patches import Shore


class SiteError(RuntimeError):
    """Raised when the map cannot hold even the minimum master plan, or when
    an allocation request cannot be satisfied."""


@dataclass(frozen=True)
class SiteConfig:
    """All sizes (tiles) and weights used by the planner.

    Nothing in the planner uses fixed map coordinates: every position derives
    from the map and from these numbers.
    """

    # Resources ------------------------------------------------------------
    required_solids: tuple[str, ...] = ("iron-ore", "copper-ore", "coal", "stone")
    feed_resources: tuple[str, ...] = ("iron-ore", "copper-ore")
    smelting_feeds: tuple[str, ...] = ("iron-ore", "copper-ore", "stone", "coal")
    fuel_resource: str = "coal"
    early_patch_tiles: int = 150
    early_patch_amount: int = 150_000
    small_patch_penalty: float = 200.0
    mining_margin: int = 6
    # Main bus -------------------------------------------------------------
    bus_width: int = 28
    bus_target_length: int = 320
    bus_min_length: int = 160
    bus_length_step: int = 8
    bus_search_radius: int = 128
    bus_search_step: int = 8
    lane_order: tuple[str, ...] = (
        "iron-plate",
        "iron-plate",
        "copper-plate",
        "copper-plate",
        "coal",
        "stone",
        "steel-plate",
        "electronic-circuit",
    )
    lane_group: int = 4
    lane_group_gap: int = 2
    # Districts ------------------------------------------------------------
    district_depth: int = 48
    district_depth_fallbacks: tuple[int, ...] = (32, 24)
    district_min_clean: float = 0.5
    district_setback: int = 3
    district_gap: int = 4
    district_min_run: int = 8
    first_side: str = "right"
    # Other zones ----------------------------------------------------------
    smelting_width: int = 64
    smelting_depth: int = 48
    head_gap: int = 4
    power_width: int = 40
    power_height: int = 32
    shore_search_radius: int = 224
    corridor_width: int = 4
    # Rail reservations (data only) ------------------------------------------
    rail_unload_tracks: int = 2
    rail_train_tiles: int = 35  # 1 locomotive + 4 wagons, 7 tiles each
    rail_approach_tiles: int = 16
    rail_track_tiles: int = 8  # per track: rail plus inserters/chests/belts
    rail_trunk_width: int = 8
    rail_outpost_min_tiles: int = 300
    rail_outposts_max: int = 6
    rail_load_length: int = 56
    rail_load_width: int = 16
    # Orientation weights (corridor tiles) ---------------------------------
    crossing_weight: float = 60.0
    turn_penalty: float = 20.0
    unroutable_penalty: float = 1000.0
    # Bus scoring weights --------------------------------------------------
    side_weight: float = 150.0
    feed_weight: float = 1.0
    start_weight: float = 0.25
    smelting_penalty: float = 300.0
    band_penalty: float = 120.0

    def __post_init__(self) -> None:
        if self.bus_width % 2:
            raise ValueError("bus_width must be even")
        if self.bus_min_length > self.bus_target_length:
            raise ValueError("bus_min_length must not exceed bus_target_length")
        if self.first_side not in ("left", "right"):
            raise ValueError("first_side must be 'left' or 'right'")

    @property
    def rail_unload_length(self) -> int:
        """Station length along the tracks: train + approach/exit, rounded up to 8."""
        return -(-(self.rail_train_tiles + self.rail_approach_tiles) // 8) * 8

    @property
    def rail_unload_width(self) -> int:
        """Station width across the tracks (2 tracks: 2 x 8 + 4 = 20)."""
        return self.rail_unload_tracks * self.rail_track_tiles + 4

    def to_dict(self) -> dict:
        data = {}
        for item in fields(self):
            value = getattr(self, item.name)
            data[item.name] = list(value) if isinstance(value, tuple) else value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> SiteConfig:
        known = {f.name for f in fields(cls)}
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in data.items() if k in known}
        return cls(**values)


def _rect(data) -> Rect:
    return Rect(*data)


@dataclass(frozen=True)
class PatchRef:
    """Summary of one resource patch. ``distance`` is from the plan start to
    the patch centroid (tiles, Euclidean); ``score`` is the selection score
    (lower is better)."""

    name: str
    tile_count: int
    amount: int
    bbox: Rect
    centroid: tuple[float, float]
    distance: float
    score: float
    wells: int = 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["bbox"] = self.bbox.to_list()
        data["centroid"] = list(self.centroid)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PatchRef:
        values = dict(data)
        values["bbox"] = _rect(data["bbox"])
        values["centroid"] = tuple(data["centroid"])
        return cls(**values)


@dataclass(frozen=True)
class ResourcePlan:
    """Chosen starting patch (or None) and the other patches ranked best first."""

    name: str
    primary: PatchRef | None
    others: tuple[PatchRef, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "primary": None if self.primary is None else self.primary.to_dict(),
            "others": [p.to_dict() for p in self.others],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ResourcePlan:
        primary = None if data["primary"] is None else PatchRef.from_dict(data["primary"])
        return cls(data["name"], primary, tuple(PatchRef.from_dict(p) for p in data["others"]))


@dataclass(frozen=True)
class Lane:
    """One planned belt column of the bus (data only, nothing is built).

    ``offset`` is the lateral tile index from the bus centreline: the lane
    occupies local band ``[offset, offset + 1)``; positive is to the right of
    the bus direction. ``material`` is "" for a spare slot.
    """

    index: int
    group: int
    offset: int
    material: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Lane:
        return cls(**data)


@dataclass(frozen=True)
class Strip:
    """A run of district land along one side of the bus.

    ``along0``/``along1`` are bus-local distances from the head (tiles,
    half-open). The strip spans the setback band plus the district depth.
    """

    side: str
    along0: int
    along1: int
    rect: Rect

    @property
    def owner(self) -> str:
        return "districts:" + self.side

    def to_dict(self) -> dict:
        return {"side": self.side, "along0": self.along0, "along1": self.along1, "rect": self.rect.to_list()}

    @classmethod
    def from_dict(cls, data: dict) -> Strip:
        return cls(data["side"], data["along0"], data["along1"], _rect(data["rect"]))


def shore_to_dict(shore: Shore | None) -> dict | None:
    if shore is None:
        return None
    return {"water": list(shore.water), "land": list(shore.land), "direction": shore.direction}


def shore_from_dict(data: dict | None) -> Shore | None:
    if data is None:
        return None
    return Shore(tuple(data["water"]), tuple(data["land"]), data["direction"])
