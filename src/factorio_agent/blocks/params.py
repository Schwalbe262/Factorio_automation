"""Explicit layout parameters that the exported world catalog does not carry.

The 2.1 catalog export (``core/world_catalog.py``) has recipes, crafting speeds,
crafting categories and collision boxes, but NOT belt speed, inserter
throughput or reach, or electric-pole supply area and wire reach. Every such
number lives here as a documented, overridable default. Each lookup first
checks the catalog for the native field (so a richer export wins) and only
then falls back to the table below.

Inserter rate model: conservative belt-to-machine items/minute at hand size 1
(no inserter capacity research), the same budget style used by
``compact_green.audit_compact_green`` (30 yellow, 120 fast). Long-handed uses
40: roughly 0.6 x its chest-to-chest 1.2 items/s, like the yellow ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RowLimits:
    """Fallback figures; override per call to model research or other tiers."""

    inserter_items_per_minute: Mapping[str, float] = field(
        default_factory=lambda: {"inserter": 30, "long-handed-inserter": 40, "fast-inserter": 120}
    )
    inserter_reach: Mapping[str, int] = field(
        default_factory=lambda: {"inserter": 1, "fast-inserter": 1, "burner-inserter": 1, "long-handed-inserter": 2}
    )
    lane_items_per_minute: Mapping[str, float] = field(
        default_factory=lambda: {
            "transport-belt": 450,
            "fast-transport-belt": 900,
            "express-transport-belt": 1350,
            "turbo-transport-belt": 1800,
        }
    )
    pole_supply_radius: Mapping[str, float] = field(
        default_factory=lambda: {"small-electric-pole": 2.5, "medium-electric-pole": 3.5}
    )
    pole_wire_reach: Mapping[str, float] = field(
        default_factory=lambda: {"small-electric-pole": 7.5, "medium-electric-pole": 9}
    )
    long_inserter: str = "long-handed-inserter"


DEFAULT_LIMITS = RowLimits()


def _entity(catalog: Any, name: str) -> Mapping[str, Any]:
    return catalog.entities.get(name) or {}


def _lookup(table: Mapping[str, float], name: str, what: str) -> float:
    if name not in table:
        raise ValueError(f"no {what} figure for {name!r}: not exported by the catalog; pass RowLimits")
    return table[name]


def lane_capacity(catalog: Any, belt: str, limits: RowLimits = DEFAULT_LIMITS) -> float:
    """Items/minute on ONE lane. Native ``belt_speed`` (tiles/tick) x 60 x 4 items x 60."""
    speed = _entity(catalog, belt).get("belt_speed")
    if isinstance(speed, (int, float)) and speed > 0:
        return float(speed) * 60 * 4 * 60
    return float(_lookup(limits.lane_items_per_minute, belt, "belt lane throughput"))


def inserter_rate(catalog: Any, name: str, limits: RowLimits = DEFAULT_LIMITS) -> float:
    """Conservative belt->machine items/minute (catalog exports no swing speed)."""
    return float(_lookup(limits.inserter_items_per_minute, name, "inserter throughput"))


def inserter_reach(catalog: Any, name: str, limits: RowLimits = DEFAULT_LIMITS) -> int:
    """Tiles from the inserter to its pickup (and drop) tile; native pickup_position wins."""
    pickup = _entity(catalog, name).get("pickup_position")
    if isinstance(pickup, Mapping) and "y" in pickup:
        return int(round(abs(float(pickup["y"]))))
    return int(_lookup(limits.inserter_reach, name, "inserter reach"))


def pole_radius(catalog: Any, name: str, limits: RowLimits = DEFAULT_LIMITS) -> float:
    """Half-width of the square supply area, in tiles from the pole centre."""
    native = _entity(catalog, name).get("supply_area_distance")
    if isinstance(native, (int, float)) and native > 0:
        return float(native)
    return float(_lookup(limits.pole_supply_radius, name, "pole supply area"))


def pole_reach(catalog: Any, name: str, limits: RowLimits = DEFAULT_LIMITS) -> float:
    """Maximum copper-wire distance between two pole centres."""
    native = _entity(catalog, name).get("max_wire_distance")
    if isinstance(native, (int, float)) and native > 0:
        return float(native)
    return float(_lookup(limits.pole_wire_reach, name, "pole wire reach"))
