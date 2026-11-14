"""Offline measure of district slot choice: a base's allocation requests replayed on a fake world.

``simulate(world, config, blocks, use_floor)`` plans the site (with the planned floor when
``use_floor``, else first-fit), then replays every block's request the way the controllers make it
(``SiteService.allocate_candidates``: ``CANDIDATES`` blocks ``<name>#i``, the first kept and
settled, the others released), in roadmap order with a belt-output row after each produce row
that has one, and a growth step of every block (``extend`` by its growth allowance). ``jitter`` changes each requested length by up to that fraction (a layout that
came out larger or smaller than the estimate). One block the floor does not know (``UNKNOWN``)
arrives in the middle.

Metrics (``Result``):

* ``link_sum`` / ``link_max``: feed length (:func:`feed_length`: Manhattan on one side, round the
  nearer bus end across it) from a supplier's output point to its consumer's tap band for every
  privately routed item (an intermediate not taken from a bus lane);
* ``core_sum`` / ``core_max`` / ``core_links``: the same over links between roadmap blocks (the
  rocket-bill blocks at the bus tail do not fit a 320-tile bus in either mode, so their links
  are not comparable);
* ``crossings``: such links whose ends are on different sides of the bus (no private belt may cross);
* ``upstream``: bus-lane inputs whose producing block sits downstream of the consumer;
* ``grown`` / ``growth_tried``: blocks whose growth step stayed inside their own frame;
* ``failures``: requests the allocator could not place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random

from .busframe import DIRECTIONS, right_of
from .floorplan import base_name
from .masterplan import plan_site
from .planmodel import SiteError

CANDIDATES = 4
# A block the floor does not know, arriving after the block of this rank (a mall row, a repair cell).
UNKNOWN = ("unplanned_block", 16, 16)
UNKNOWN_AFTER_RANK = 20
BELT_ROW_ALONG = 14
ROCKET_KIND = "rocket"  # floor_blocks.BILL_KIND: a block only the rocket bill asks for


@dataclass
class Result:
    link_sum: float = 0.0
    link_max: float = 0.0
    links: int = 0
    core_sum: float = 0.0  # the same over links between roadmap blocks only (both modes place them)
    core_max: float = 0.0
    core_links: int = 0
    crossings: int = 0
    upstream: int = 0
    grown: int = 0
    growth_tried: int = 0
    failures: int = 0
    allocated: int = 0
    notes: list = field(default_factory=list)


def _request(plan, world, name, along, depth, returns_to_bus=True):
    """``allocate_candidates`` + ``keep_only(#0)`` as the SiteService does it; the kept block or None."""
    found = []
    for index in range(CANDIDATES):
        try:
            found.append(plan.allocate(world, "production", along, depth, name=f"{name}#{index}", settled=False))
        except SiteError:
            break
    if not found:
        return None
    for extra in found[1:]:
        plan.release(world, extra.name)
    return plan.settle(found[0].name)


def arrivals(blocks, jitter: float = 0.0, seed: int = 0):
    """``(name, along, depth, growth)`` requests in the order a run makes them (module doc)."""
    rng = random.Random(seed)
    out = []
    unknown = False
    for block in sorted(blocks, key=lambda b: b.rank):
        if not unknown and block.rank > UNKNOWN_AFTER_RANK:
            out.append((UNKNOWN[0], UNKNOWN[1], UNKNOWN[2], 0))
            unknown = True
        along = block.along
        if jitter:
            along = max(4, int(round(along * (1 + rng.uniform(-jitter, jitter)))))
        out.append((block.names[0], along, min(block.depth, 48), block.growth))
        if len(block.names) > 1 and block.names[1].endswith("__belt"):
            out.append((block.names[1], BELT_ROW_ALONG, min(block.depth, 48), 0))
    return out


def _local(bus, point) -> tuple[float, float]:
    """``(along, lateral)`` bus-local coordinates of a map point."""
    dx, dy = DIRECTIONS[bus.direction]
    rx, ry = right_of(bus.direction)
    x, y = point[0] - bus.origin[0], point[1] - bus.origin[1]
    return x * dx + y * dy, x * rx + y * ry


def feed_length(plan, supplier, consumer) -> float:
    """Belt length of a private feed from ``supplier``'s output point to ``consumer``'s tap band.

    Same side: Manhattan in the bus frame. Opposite sides: no private belt may cross the bus band
    (``transport.bus_band``), so the feed goes round the nearer bus end (head or tail).
    """
    sa, sl = _local(plan.bus, supplier.output_point)
    ca, cl = _local(plan.bus, consumer.tap_rect.centre())
    lateral = abs(sl - cl)
    if supplier.side == consumer.side:
        return abs(sa - ca) + lateral
    length = plan.bus.length
    return min(sa + ca, (length - sa) + (length - ca)) + lateral


def measure(plan, blocks) -> Result:
    """Link, crossing and upstream metrics of the allocations on ``plan`` (module doc)."""
    result = Result()
    by_block = {}
    for allocation in plan.allocations:
        base = base_name(allocation.name)
        for block in blocks:
            if base in block.names:
                by_block.setdefault(block.name, allocation)
    bus = {lane.material for lane in plan.bus.lanes if lane.material}
    specs = {b.name: b for b in blocks}
    for consumer_name, consumer in by_block.items():
        spec = specs[consumer_name]
        for supplier_name, supplier in by_block.items():
            if supplier_name == consumer_name:
                continue
            other = specs[supplier_name]
            items = set(spec.private) & set(other.outputs)
            if items:
                length = feed_length(plan, supplier, consumer)
                result.links += 1
                result.link_sum += length
                result.link_max = max(result.link_max, length)
                if supplier.side != consumer.side:
                    result.crossings += 1
                if spec.kind != ROCKET_KIND and other.kind != ROCKET_KIND:
                    result.core_links += 1
                    result.core_sum += length
                    result.core_max = max(result.core_max, length)
            taps = (set(spec.inputs) - set(spec.private)) & bus & set(other.outputs)
            if taps and consumer.along0 < supplier.along1:
                result.upstream += 1
    return result


def simulate(world, config, blocks, *, use_floor: bool, jitter: float = 0.0, seed: int = 0) -> Result:
    """Plan + replay + measure on ``world`` (consumed: reservations are written to it)."""
    plan = plan_site(world, config=config, floor_blocks=blocks if use_floor else None)
    failures, kept = 0, []
    for name, along, depth, growth in arrivals(blocks, jitter, seed):
        if depth > plan.district_depth:
            failures += 1
            continue
        allocation = _request(plan, world, name, along, depth)
        if allocation is None:
            failures += 1
            continue
        kept.append((allocation.name, growth))
    result = measure(plan, blocks)
    by_base = {base_name(name): name for name, _growth in kept}
    gap = plan.config.district_gap
    for block in blocks:
        main = by_base.get(block.names[0])
        belt = next((by_base[n] for n in block.names[1:] if n.endswith("__belt") and n in by_base), None)
        if main is None:
            continue
        if belt is not None:
            # Growth by a belt-output row: in frame when it stands right behind its main row.
            result.growth_tried += 1
            a = next(x for x in plan.allocations if x.name == main)
            b = next(x for x in plan.allocations if x.name == belt)
            result.grown += int(a.side == b.side and b.along0 - a.along1 == gap)
        elif block.growth > 0:
            result.growth_tried += 1
            try:
                plan.extend(world, main, block.growth)
                result.grown += 1
            except SiteError:
                pass
    result.failures = failures
    result.allocated = len(kept)
    problems = plan.validate(world)
    if problems:
        result.notes.append(f"invalid: {problems[0]}")
    return result
