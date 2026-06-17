"""Territory model: every factory site owns a bounded rectangle that other sites must not
encroach on (constraint C3), with reserved I/O corridors + train-station pad (C4) and GROWTH
HEADROOM so a site can be expanded in place instead of relocated (C7).

A non-overlap allocator places a new cell's reserved box in free space near a desired anchor,
avoiding existing factory zones, other reserved sites, and (protected) resource patches.

Pure module operating on rectangles + the world-memory dict; no RCON / file I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Rect:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    def inflate(self, d: float) -> "Rect":
        return Rect(self.min_x - d, self.min_y - d, self.max_x + d, self.max_y + d)

    def intersects(self, other: "Rect", *, gap: float = 0.0) -> bool:
        return not (
            self.max_x + gap <= other.min_x
            or other.max_x + gap <= self.min_x
            or self.max_y + gap <= other.min_y
            or other.max_y + gap <= self.min_y
        )

    def contains_point(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def rect_from_center(cx: float, cy: float, w: float, h: float) -> Rect:
    return Rect(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def rect_from_bounds(bounds: Any) -> Rect | None:
    if not isinstance(bounds, dict):
        return None
    try:
        return Rect(
            float(bounds["min_x"]), float(bounds["min_y"]),
            float(bounds["max_x"]), float(bounds["max_y"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def reserved_box_dims(
    required_w: float,
    required_h: float,
    *,
    growth_factor: float = 2.0,
    io_margin: float = 4.0,
    station_pad: float = 0.0,
) -> tuple[float, float]:
    """Size a site's reserved box: grow along the row axis for future machines (C7) + reserve I/O
    belt margins on every side (C4) + an optional train-station pad on one side."""
    w = required_w * max(1.0, growth_factor) + 2 * io_margin + station_pad
    h = required_h + 2 * io_margin
    return (round(w, 1), round(h, 1))


def occupied_rects(
    world_memory: dict[str, Any] | None,
    sites: list[dict[str, Any]] | None,
    *,
    protect_resources: bool = True,
) -> list[Rect]:
    """Collect the rectangles a new site must avoid: existing factory zones, other sites' reserved
    boxes, and (optionally) resource patches to preserve for miners."""
    rects: list[Rect] = []
    wm = world_memory or {}
    factory = wm.get("factory") if isinstance(wm.get("factory"), dict) else {}
    for zone in factory.get("zones") or []:
        rect = rect_from_bounds(zone.get("bounds") if isinstance(zone, dict) else None)
        if rect is not None:
            rects.append(rect)
    for reserved in wm.get("reserved_sites") or []:
        rect = rect_from_bounds(reserved.get("reserved_box") if isinstance(reserved, dict) else None)
        if rect is not None:
            rects.append(rect)
    if protect_resources:
        resources = wm.get("resources") if isinstance(wm.get("resources"), dict) else {}
        for patch in resources.get("patches") or []:
            rect = rect_from_bounds(patch.get("bounds") if isinstance(patch, dict) else None)
            if rect is not None:
                rects.append(rect)
    for site in sites or []:
        if not isinstance(site, dict):
            continue
        rect = rect_from_bounds(site.get("reserved_box") or site.get("bounds"))
        if rect is not None:
            rects.append(rect)
    return rects


def allocate_box(
    anchor_x: float,
    anchor_y: float,
    box_w: float,
    box_h: float,
    occupied: list[Rect],
    *,
    step: float = 8.0,
    max_rings: int = 48,
    gap: float = 2.0,
) -> dict[str, Any]:
    """Find a ``box_w x box_h`` rectangle near ``(anchor_x, anchor_y)`` that does not intersect any
    occupied rect (keeping ``gap`` tiles clearance). Spiral-ring search outward from the anchor.

    Returns ``{ok, rect, anchor, status, reason}``."""

    def free_at(cx: float, cy: float) -> Rect | None:
        rect = rect_from_center(cx, cy, box_w, box_h)
        for other in occupied:
            if rect.intersects(other, gap=gap):
                return None
        return rect

    rect = free_at(anchor_x, anchor_y)
    if rect is not None:
        return {"ok": True, "rect": rect.to_dict(), "anchor": {"x": anchor_x, "y": anchor_y},
                "status": "placed_at_anchor", "reason": ""}

    for ring in range(1, max_rings + 1):
        r = ring * step
        # walk the perimeter of the square ring at radius r
        offsets: list[tuple[float, float]] = []
        x = -ring
        while x <= ring:
            offsets.append((x * step, -r))
            offsets.append((x * step, r))
            x += 1
        y = -ring + 1
        while y <= ring - 1:
            offsets.append((-r, y * step))
            offsets.append((r, y * step))
            y += 1
        # nearest-first within the ring
        offsets.sort(key=lambda o: o[0] * o[0] + o[1] * o[1])
        for dx, dy in offsets:
            rect = free_at(anchor_x + dx, anchor_y + dy)
            if rect is not None:
                cx, cy = rect.center
                return {"ok": True, "rect": rect.to_dict(), "anchor": {"x": cx, "y": cy},
                        "status": "placed_in_ring", "reason": f"shifted {ring * step:.0f} tiles from anchor"}

    return {"ok": False, "rect": None, "anchor": {"x": anchor_x, "y": anchor_y},
            "status": "no_free_space", "reason": f"no {box_w:.0f}x{box_h:.0f} gap within {max_rings * step:.0f} tiles"}


def register_site(
    world_memory: dict[str, Any],
    site_id: str,
    rect: Rect,
    *,
    target_item: str | None = None,
    io_corridors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record a reserved site in the world-memory dict (in place) so later allocations avoid it.
    Returns the reserved-site record. (Persistence to disk is handled by the caller via
    world_memory.update_world_map_memory.)"""
    record = {
        "site_id": site_id,
        "target_item": target_item,
        "reserved_box": rect.to_dict(),
        "io_corridors": io_corridors or [],
    }
    reserved = world_memory.setdefault("reserved_sites", [])
    reserved[:] = [r for r in reserved if isinstance(r, dict) and r.get("site_id") != site_id]
    reserved.append(record)
    return record
