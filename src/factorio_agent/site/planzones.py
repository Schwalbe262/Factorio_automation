"""Zones, ports, corridors and allocations of the master plan.

Orientation vocabulary (used everywhere in the site package):

* A *side* is the compass letter of a rect edge: "N" is the edge at
  ``y0`` (north), "S" at ``y1``, "W" at ``x0``, "E" at ``x1``.
* ``input_side`` is the edge where the zone's main input arrives,
  ``output_side`` the edge where its main product leaves. "" means none.
* A :class:`Port` is one material crossing one edge at one map point.
* A segment ``direction`` is the compass direction material travels in.

Every class has ``to_dict`` / ``from_dict`` for the plan JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .geometry import Rect

SIDE_NAMES = ("N", "E", "S", "W")


def _rect(data) -> Rect:
    return Rect(*data)


def edge_touches(zone: Rect, other: Rect, side: str) -> bool:
    """True when ``other`` lies just outside ``zone``'s ``side`` edge and
    overlaps it along that edge (so material can cross the edge)."""
    if side in ("N", "S"):
        touching = other.y1 == zone.y0 if side == "N" else other.y0 == zone.y1
        return touching and other.x0 < zone.x1 and zone.x0 < other.x1
    if side in ("E", "W"):
        touching = other.x0 == zone.x1 if side == "E" else other.x1 == zone.x0
        return touching and other.y0 < zone.y1 and zone.y0 < other.y1
    raise ValueError(f"unknown side {side!r}")


@dataclass(frozen=True)
class Port:
    """``material`` enters (``role`` "in") or leaves ("out") through edge
    ``side`` at map point ``point`` (on the edge line)."""

    material: str
    role: str
    side: str
    point: tuple[float, float]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["point"] = list(self.point)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Port:
        return cls(data["material"], data["role"], data["side"], tuple(data["point"]))


@dataclass(frozen=True)
class Zone:
    """A reserved rectangle. ``name`` is also the owner used on the WorldMap.

    ``growth`` is the compass direction the zone may grow towards ("" none).
    """

    kind: str
    name: str
    rect: Rect
    growth: str = ""
    input_side: str = ""
    output_side: str = ""
    ports: tuple[Port, ...] = ()

    def port(self, material: str, role: str) -> Port | None:
        return next((p for p in self.ports if p.material == material and p.role == role), None)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "rect": self.rect.to_list(),
            "growth": self.growth,
            "input_side": self.input_side,
            "output_side": self.output_side,
            "ports": [p.to_dict() for p in self.ports],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Zone:
        ports = tuple(Port.from_dict(p) for p in data["ports"])
        return cls(
            data["kind"],
            data["name"],
            _rect(data["rect"]),
            data["growth"],
            data["input_side"],
            data["output_side"],
            ports,
        )


@dataclass(frozen=True)
class Segment:
    """One straight leg of a corridor; material travels in ``direction``."""

    rect: Rect
    direction: str

    def to_dict(self) -> dict:
        return {"rect": self.rect.to_list(), "direction": self.direction}

    @classmethod
    def from_dict(cls, data: dict) -> Segment:
        return cls(_rect(data["rect"]), data["direction"])


@dataclass(frozen=True)
class Corridor:
    """A reserved strip of straight segments (at most two elbows) between zones.

    ``kind`` is "feeder" (ore/coal to a consumer), "header" (smelting to bus
    head) or "power". It leaves ``source`` through edge ``from_side`` and
    enters ``target`` through edge ``to_side``. ``segments`` are empty when
    the zones touch. Corridors of different names may cross each other.
    """

    name: str
    kind: str
    material: str
    source: str
    target: str
    width: int
    from_side: str
    to_side: str
    segments: tuple[Segment, ...]

    @property
    def rects(self) -> tuple[Rect, ...]:
        return tuple(s.rect for s in self.segments)

    @property
    def length(self) -> int:
        return sum(r.area for r in self.rects) // max(1, self.width)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["segments"] = [s.to_dict() for s in self.segments]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Corridor:
        values = dict(data)
        values["segments"] = tuple(Segment.from_dict(s) for s in data["segments"])
        return cls(**values)


@dataclass(frozen=True)
class Allocation:
    """A district block handed out next to the bus.

    ``along0``/``along1`` are bus-local (tiles from the head); ``depth`` is
    the extent away from the bus.

    Orientation rule:

    * ``input_side`` is always the bus-adjacent edge (``bus_edge``): taps
      arrive there through ``tap_rect``, the setback band.
    * ``flow`` is the direction belts run inside the block: parallel to the
      bus and pointing downstream (= the bus direction), so every block on
      both sides flows the same way.
    * ``output_side``: a product that returns to the bus
      (``returns_to_bus``) leaves on the bus-adjacent edge at the downstream
      end; a terminal product (science to labs, mall chests) leaves on the
      far edge, also at the downstream end. ``output_point`` is that spot.
    """

    name: str
    kind: str
    side: str
    along0: int
    along1: int
    depth: int
    rect: Rect
    flow: str
    bus_edge: str
    tap_rect: Rect
    input_side: str
    output_side: str
    output_point: tuple[float, float]
    returns_to_bus: bool

    @property
    def owner(self) -> str:
        return "block:" + self.name

    def to_dict(self) -> dict:
        data = asdict(self)
        data["rect"] = self.rect.to_list()
        data["tap_rect"] = self.tap_rect.to_list()
        data["output_point"] = list(self.output_point)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Allocation:
        values = dict(data)
        values["rect"] = _rect(data["rect"])
        values["tap_rect"] = _rect(data["tap_rect"])
        values["output_point"] = tuple(data["output_point"])
        return cls(**values)
