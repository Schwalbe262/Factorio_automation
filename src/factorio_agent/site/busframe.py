"""The main bus and its local coordinate frame.

Bus-local coordinates:

* ``along`` is the distance in tiles from the bus head, growing in the bus
  ``direction``. The bus itself covers ``0 <= along < length``; the
  smelting zone sits at negative ``along`` (behind the head).
* ``lateral`` is the distance from the bus centreline, positive to the
  *right* of the bus direction (north bus: right is east).

``origin`` is the map point (integer corner, not a tile) on the centreline at
the head. A local box ``[a0, a1) x [l0, l1)`` maps to a whole-tile world
:class:`Rect`, because the origin is an integer point and the axes are the
compass axes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .geometry import Rect
from .planmodel import Lane

DIRECTIONS: dict[str, tuple[int, int]] = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}
COMPASS = ("N", "E", "S", "W")
SIDES = ("left", "right")


def right_of(direction: str) -> tuple[int, int]:
    """Unit vector pointing to the right of ``direction``."""
    dx, dy = DIRECTIONS[direction]
    return (-dy, dx)


def compass_of(vector: tuple[int, int]) -> str:
    """Compass letter of a unit axis vector."""
    for name, value in DIRECTIONS.items():
        if value == vector:
            return name
    raise ValueError(f"not a unit axis vector: {vector}")


def opposite(direction: str) -> str:
    dx, dy = DIRECTIONS[direction]
    return compass_of((-dx, -dy))


def dominant_compass(dx: float, dy: float) -> str:
    """Compass letter of the larger component of a vector ("" for zero)."""
    if dx == 0 and dy == 0:
        return ""
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


def frame_rect(origin: tuple[int, int], direction: str, a0: int, a1: int, l0: int, l1: int) -> Rect:
    """World rect of the local box ``[a0, a1) x [l0, l1)``."""
    dx, dy = DIRECTIONS[direction]
    rx, ry = right_of(direction)
    ox, oy = origin
    xs = []
    ys = []
    for along in (a0, a1):
        for lateral in (l0, l1):
            xs.append(ox + along * dx + lateral * rx)
            ys.append(oy + along * dy + lateral * ry)
    return Rect(min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class Bus:
    """An axis-aligned straight main bus strip.

    ``direction`` is the flow direction of items: from the head (next to the
    smelting zone, ``origin``) towards the open land at the tail. ``head``
    and ``tail`` are the centreline map points at both ends. ``width`` and
    ``length`` are tiles. ``lanes`` is the planned lane table.
    """

    origin: tuple[int, int]
    direction: str
    width: int
    length: int
    lanes: tuple[Lane, ...] = ()

    @property
    def half(self) -> int:
        return self.width // 2

    def local_rect(self, a0: int, a1: int, l0: int, l1: int) -> Rect:
        return frame_rect(self.origin, self.direction, a0, a1, l0, l1)

    def rect(self) -> Rect:
        return self.local_rect(0, self.length, -self.half, self.half)

    def side_band(self, side: str, near: int, far: int) -> tuple[int, int]:
        """Lateral interval ``[l0, l1)`` from ``near`` to ``far`` tiles off the bus edge."""
        if side == "right":
            return (self.half + near, self.half + far)
        if side == "left":
            return (-self.half - far, -self.half - near)
        raise ValueError(f"unknown side {side!r}")

    def side_rect(self, side: str, a0: int, a1: int, near: int, far: int) -> Rect:
        l0, l1 = self.side_band(side, near, far)
        return self.local_rect(a0, a1, l0, l1)

    def local_point(self, along: float, lateral: float) -> tuple[float, float]:
        """Map point of bus-local coordinates (floats allowed)."""
        dx, dy = DIRECTIONS[self.direction]
        rx, ry = right_of(self.direction)
        return (self.origin[0] + along * dx + lateral * rx, self.origin[1] + along * dy + lateral * ry)

    def local_tile(self, x: int, y: int) -> tuple[int, int]:
        """``(along, lateral)`` tile indices of world tile ``(x, y)``."""
        dx, dy = DIRECTIONS[self.direction]
        rx, ry = right_of(self.direction)
        cx = x + 0.5 - self.origin[0]
        cy = y + 0.5 - self.origin[1]
        return (math.floor(cx * dx + cy * dy), math.floor(cx * rx + cy * ry))

    def head_point(self) -> tuple[float, float]:
        return (float(self.origin[0]), float(self.origin[1]))

    @property
    def head(self) -> tuple[int, int]:
        return self.origin

    @property
    def tail(self) -> tuple[int, int]:
        dx, dy = DIRECTIONS[self.direction]
        return (self.origin[0] + self.length * dx, self.origin[1] + self.length * dy)

    @property
    def input_side(self) -> str:
        """Head edge of the bus rect (items enter here)."""
        return opposite(self.direction)

    @property
    def output_side(self) -> str:
        """Tail edge of the bus rect (items flow towards it)."""
        return self.direction

    def bus_edge_of(self, side: str) -> str:
        """Compass edge of a block on ``side`` that faces the bus."""
        rx, ry = right_of(self.direction)
        if side == "right":
            return compass_of((-rx, -ry))
        return compass_of((rx, ry))

    def to_dict(self) -> dict:
        return {
            "origin": list(self.origin),
            "direction": self.direction,
            "width": self.width,
            "length": self.length,
            "head": list(self.head),
            "tail": list(self.tail),
            "lanes": [lane.to_dict() for lane in self.lanes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Bus:
        lanes = tuple(Lane.from_dict(row) for row in data["lanes"])
        return cls(tuple(data["origin"]), data["direction"], data["width"], data["length"], lanes)


def lane_table(width: int, order: tuple[str, ...], group: int, gap: int) -> tuple[Lane, ...]:
    """Lanes in groups of ``group`` columns with ``gap`` empty tiles between groups.

    As many whole groups as fit in ``width`` are used, centred on the bus
    centreline. Lanes are numbered from the left edge (looking along the bus
    direction). Materials follow ``order``; remaining lanes are spare ("").
    """
    pitch = group + gap
    groups = (width + gap) // pitch
    if groups <= 0:
        return ()
    used = groups * pitch - gap
    left = -(used // 2)
    lanes = []
    for index in range(groups * group):
        number = index // group
        offset = left + number * pitch + index % group
        material = order[index] if index < len(order) else ""
        lanes.append(Lane(index, number, offset, material))
    return tuple(lanes)
