"""Rotate and mirror whole entity plans (``board.entity`` dicts, ports, metadata).

Conventions (map tiles, x east, y south, Factorio 2.0 16-way directions:
0 = north, 4 = east, 8 = south, 12 = west):

* A :class:`Transform` first mirrors about the vertical (``mirror="x"``:
  x -> -x) or horizontal (``mirror="y"``: y -> -y) line through ``origin``,
  then rotates clockwise (on screen, y down) by ``rotation`` sixteenths of a
  turn about ``origin``. ``origin`` must be a tile corner or a tile centre
  (both coordinates integral, or both half-integral) so tiles stay on the
  tile grid; rotating a square about its centre maps it onto itself.
* Entity ``position`` is the footprint centre, so mapping the centre maps
  the footprint. ``_width`` / ``_height`` are the unrotated (north-facing)
  footprint and never change; the builder swaps them for directions 4/12.
* An entity whose orientation matters (belts, inserters, splitters,
  undergrounds, drills, ...) or that already carries a non-zero direction
  gets its direction mapped. Others (furnaces, poles, chests, labs, plain
  assemblers) keep direction 0; a non-square one of those cannot be turned
  by 90 degrees and raises ValueError.
* Mirroring keeps belt semantics: a side-load stays a side-load and a
  straight belt stays straight, but the two lanes swap sides (the left lane
  of a mirrored belt carries what the right lane carried). Inserters take
  from both lanes, so furnace and assembler rows behave the same. Splitter
  ``input_priority`` / ``output_priority`` "left"/"right", ``lane`` and the
  keys of a port's ``lane_items`` {"left", "right"} are swapped. Fluid
  machines with asymmetric connections (chemical plant, refinery, and an
  assembler with a non-zero direction) cannot be mirrored: ValueError.

Recognised plan keys, recursively (so nested ``banks`` / candidate dicts
work): any point dict {x, y, ...} (``position``, ``join``, ...);
``facing`` and integer ``direction``;
``reserved_area`` / ``area`` / ``mining_footprint`` / ``mining_reserved_area`` /
``growth_area``
[[x0, y0], [x1, y1]]; underground pair
``input`` / ``output`` [x, y]; splitter priorities. ``clear_obstacles``
names real map objects and must be empty (transform before preflight).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

ROTATIONS = (0, 4, 8, 12)
MIRRORS = (None, "x", "y")
ORIENTED = frozenset(
    {
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
        "splitter",
        "fast-splitter",
        "express-splitter",
        "burner-inserter",
        "inserter",
        "long-handed-inserter",
        "fast-inserter",
        "bulk-inserter",
        "stack-inserter",
        "pipe-to-ground",
        "pump",
        "offshore-pump",
        "boiler",
        "steam-engine",
        "burner-mining-drill",
        "electric-mining-drill",
        "pumpjack",
        "chemical-plant",
        "oil-refinery",
        "loader",
    }
)
SIDED_KEYS = frozenset({"input_priority", "output_priority", "lane"})
RECT_KEYS = frozenset({"reserved_area", "area", "mining_footprint", "mining_reserved_area", "growth_area"})
UNMIRRORABLE = frozenset({"chemical-plant", "oil-refinery"})
FOOTPRINTS = {"boiler": (3, 2), "steam-engine": (3, 5), "rocket-silo": (9, 9)}


@dataclass(frozen=True)
class Transform:
    """Mirror (optional) then clockwise rotation about ``origin``."""

    rotation: int = 0
    mirror: str | None = None
    origin: tuple[float, float] = (0, 0)

    def __post_init__(self):
        if self.rotation not in ROTATIONS:
            raise ValueError(f"rotation must be one of {ROTATIONS}")
        if self.mirror not in MIRRORS:
            raise ValueError(f"mirror must be one of {MIRRORS}")
        ox, oy = self.origin
        if not (float(2 * ox).is_integer() and float(2 * oy).is_integer() and float(ox - oy).is_integer()):
            raise ValueError("origin must be a tile corner or a tile centre")

    def to_dict(self) -> dict:
        return {"rotation": self.rotation, "mirror": self.mirror, "origin": [_number(v) for v in self.origin]}

    @classmethod
    def from_dict(cls, data: dict | None) -> Transform:
        if not data:
            return cls()
        return cls(data["rotation"], data["mirror"], tuple(data["origin"]))

    @property
    def identity(self) -> bool:
        return self.rotation == 0 and self.mirror is None

    def point(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.origin
        dx, dy = x - ox, y - oy
        if self.mirror == "x":
            dx = -dx
        elif self.mirror == "y":
            dy = -dy
        for _ in range(self.rotation // 4):
            dx, dy = -dy, dx
        return (ox + dx, oy + dy)

    def direction(self, d: int) -> int:
        if self.mirror == "x":
            d = (16 - d) % 16
        elif self.mirror == "y":
            d = (8 - d) % 16
        return (d + self.rotation) % 16

    def side(self, side: str) -> str:
        """Compass letter of an edge after the transform."""
        compass = {"N": 0, "E": 4, "S": 8, "W": 12}
        mapped = self.direction(compass[side])
        return {v: k for k, v in compass.items()}[mapped]

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        ax, ay = self.point(x0, y0)
        bx, by = self.point(x1, y1)
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    def inverse(self) -> Transform:
        """A mirror composed with a rotation is its own inverse."""
        if self.mirror is not None:
            return self
        return Transform((16 - self.rotation) % 16, None, self.origin)


def variants(origin: tuple[int, int] = (0, 0)) -> list[Transform]:
    """The 8 distinct orientations (4 rotations x {plain, mirrored})."""
    return [Transform(r, m, origin) for m in (None, "x") for r in ROTATIONS]


def _number(v: float) -> float:
    """Keep integers integral so JSON plans look like hand-written ones."""
    return int(v) if float(v).is_integer() else v


def _entity(t: Transform, e: dict) -> dict:
    name = e["name"]
    direction = e.get("direction", 0)
    if t.mirror and (name in UNMIRRORABLE or (name.startswith("assembling-machine") and direction)):
        raise ValueError(f"{name} has asymmetric fluid connections and cannot be mirrored")
    oriented = name in ORIENTED or direction != 0
    if not oriented and t.rotation % 8:
        width, height = FOOTPRINTS.get(name, (1, 1))
        width, height = e.get("_width", width), e.get("_height", height)
        if width != height:
            raise ValueError(f"non-square {name} without a direction cannot be turned 90 degrees")
    out = {}
    for key, value in e.items():
        if key == "direction" and isinstance(value, int):
            out[key] = t.direction(value) if oriented else value
        else:
            out[key] = _value(t, key, value)
    return out


def _is_point(value: dict) -> bool:
    """Any ``{"x": number, "y": number, ...}`` dict is a map point (``position``, ``join``, ...)."""
    return all(isinstance(value.get(axis), (int, float)) and not isinstance(value.get(axis), bool) for axis in "xy")


def _value(t: Transform, key, value):
    if key == "clear_obstacles" and value:
        raise ValueError("transform a plan before preflight: clear_obstacles are real map objects")
    if isinstance(value, dict):
        if "name" in value and isinstance(value.get("position"), dict):
            return _entity(t, value)
        if _is_point(value):
            x, y = t.point(value["x"], value["y"])
            return dict(value, x=_number(x), y=_number(y))
        if key == "lane_items" and t.mirror and set(value) <= {"left", "right"}:
            return {("right" if k == "left" else "left"): v for k, v in value.items()}
        return {k: _value(t, k, v) for k, v in value.items()}
    if isinstance(value, list):
        if key in RECT_KEYS and len(value) == 2 and all(len(c) == 2 for c in value):
            x0, y0, x1, y1 = t.rect(value[0][0], value[0][1], value[1][0], value[1][1])
            return [[_number(x0), _number(y0)], [_number(x1), _number(y1)]]
        if key in ("input", "output") and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            return [_number(v) for v in t.point(*value)]
        return [_value(t, key, v) for v in value]
    if isinstance(value, tuple):
        return tuple(_value(t, key, v) for v in value)
    if key in ("facing", "direction") and isinstance(value, int) and not isinstance(value, bool):
        return t.direction(value)
    if key in SIDED_KEYS and t.mirror and value in ("left", "right"):
        return "right" if value == "left" else "left"
    return value


def transform_plan(plan: dict, *, rotation: int = 0, mirror: str | None = None, origin=(0, 0)) -> dict:
    """A transformed deep copy of ``plan`` (see module conventions)."""
    return apply(Transform(rotation, mirror, tuple(origin)), plan)


def apply(t: Transform, plan):
    """``t`` applied to a deep copy of ``plan`` (a plan, candidate, port or entity list)."""
    return _value(t, None, copy.deepcopy(plan))


def plan_transform(plan: dict) -> Transform:
    """The ``site_transform`` a plan was placed with (identity when absent)."""
    return Transform.from_dict(plan.get("site_transform"))


def in_canonical(plan: dict, function):
    """``function`` evaluated on the canonical (untransformed) ``plan``, its
    positional result mapped back onto the map. For helpers written for one
    fixed orientation (for example a westbound lab row)."""
    t = plan_transform(plan)
    if t.identity:
        return function(plan)
    return apply(t, function(apply(t.inverse(), plan)))


def point(t: Transform, position: dict) -> dict:
    """A transformed ``{"x", "y"}`` position."""
    x, y = t.point(position["x"], position["y"])
    return {"x": _number(x), "y": _number(y)}
