"""District allocation along the main bus.

Blocks are placed in the district strips on either side of the bus:

* the block's near edge is ``district_setback`` tiles off the bus edge (the
  setback band stays in the strip and is where taps connect);
* neighbouring blocks on the same side keep ``district_gap`` tiles of
  corridor between them (measured along the bus);
* the slot nearest to the bus head wins; on a tie the side opposite to the
  previous allocation wins (``first_side`` for the very first one), so
  equal blocks alternate sides naturally.

Orientation (see :class:`site.planzones.Allocation`): taps enter on the
bus-adjacent edge, belts flow downstream (the bus direction) on both sides,
and the product leaves at the downstream end on the bus edge
(``returns_to_bus``) or on the far edge (terminal products).

Each block is reserved on the map under ``block:<name>``; the strip
reservations (``districts:<side>``) are rewritten to the strip minus its
blocks, so zones never overlap. Everything is derived from the plan and the
map, so a reloaded plan + map continue exactly where they left off.
"""

from __future__ import annotations

from .corridors import subtract_rects
from .geometry import Rect
from .planmodel import SiteError, Strip
from .planzones import Allocation
from .worldmap import WorldMap


def _side_blocks(plan, side: str, skip: str | None = None) -> list[Allocation]:
    return [a for a in plan.allocations if a.side == side and a.name != skip]


def _side_strips(plan, side: str) -> list[Strip]:
    return sorted((s for s in plan.strips if s.side == side), key=lambda s: s.along0)


def block_rect(plan, side: str, along0: int, along1: int, depth: int) -> Rect:
    """World rect of a block on ``side`` covering bus-local ``[along0, along1)``."""
    setback = plan.config.district_setback
    return plan.bus.side_rect(side, along0, along1, setback, setback + depth)


def _blocked_along(plan, world: WorldMap, rect: Rect, owner: str) -> int | None:
    """Largest bus-local ``along`` of a tile that stops ``rect`` from being used.

    A tile stops it when ``world.free_rect`` (trees/rocks allowed, tiles of
    ``owner`` allowed) reports it blocked or when it carries a resource.
    Returns None when the rect is usable.
    """
    report = world.free_rect(rect.x0, rect.y0, rect.x1, rect.y1, allow_clearable=True, owner=owner)
    bad = [tile for tiles in report.blocked.values() for tile in tiles]
    bad.extend(report.resources)
    if not bad:
        return None
    return max(plan.bus.local_tile(x, y)[0] for x, y in bad)


def _gap_clash(blocks: list[Allocation], along0: int, along1: int, gap: int) -> Allocation | None:
    """First block (nearest the head) closer than ``gap`` to ``[along0, along1)``."""
    for block in sorted(blocks, key=lambda b: b.along0):
        if block.along0 - gap < along1 and along0 < block.along1 + gap:
            return block
    return None


def first_fit(plan, world: WorldMap, side: str, along: int, depth: int) -> int | None:
    """Smallest bus-local start of a free ``along`` x ``depth`` block on ``side``.

    Skips forward past strip ends, gap clashes and blocked tiles. Returns
    None when the side is exhausted.
    """
    strips = _side_strips(plan, side)
    blocks = _side_blocks(plan, side)
    gap = plan.config.district_gap
    position = 0
    while True:
        strip = next((s for s in strips if s.along1 - max(position, s.along0) >= along), None)
        if strip is None:
            return None
        position = max(position, strip.along0)
        clash = _gap_clash(blocks, position, position + along, gap)
        if clash is not None:
            position = clash.along1 + gap
            continue
        rect = block_rect(plan, side, position, position + along, depth)
        stop = _blocked_along(plan, world, rect, "districts:" + side)
        if stop is not None:
            position = stop + 1
            continue
        return position


def _other_side(side: str) -> str:
    return "left" if side == "right" else "right"


def _preferred_side(plan) -> str:
    if not plan.allocations:
        return plan.config.first_side
    return _other_side(plan.allocations[-1].side)


def _output_point(plan, side: str, along1: int, depth: int, returns_to_bus: bool) -> tuple[float, float]:
    """Centre of the last tile row (downstream end) on the near or far edge."""
    setback = plan.config.district_setback
    distance = setback if returns_to_bus else setback + depth
    lateral = plan.bus.half + distance
    if side == "left":
        lateral = -lateral
    return plan.bus.local_point(along1 - 0.5, lateral)


def _make_allocation(
    plan, name: str, kind: str, side: str, along0: int, along1: int, depth: int, returns_to_bus: bool
) -> Allocation:
    """Block record; see :class:`Allocation` for the orientation rule."""
    rect = block_rect(plan, side, along0, along1, depth)
    tap = plan.bus.side_rect(side, along0, along1, 0, plan.config.district_setback)
    bus_edge = plan.bus.bus_edge_of(side)
    far_edge = plan.bus.bus_edge_of(_other_side(side))
    return Allocation(
        name=name,
        kind=kind,
        side=side,
        along0=along0,
        along1=along1,
        depth=depth,
        rect=rect,
        flow=plan.bus.direction,
        bus_edge=bus_edge,
        tap_rect=tap,
        input_side=bus_edge,
        output_side=bus_edge if returns_to_bus else far_edge,
        output_point=_output_point(plan, side, along1, depth, returns_to_bus),
        returns_to_bus=returns_to_bus,
    )


def rezone_side(plan, world: WorldMap, side: str) -> None:
    """Rewrite the ``districts:<side>`` reservation as strips minus blocks."""
    owner = "districts:" + side
    world.release(owner)
    holes = [a.rect for a in _side_blocks(plan, side)]
    for strip in _side_strips(plan, side):
        for piece in subtract_rects(strip.rect, holes):
            world.reserve(piece, owner)


def _check_request(plan, name: str, along: int, depth: int) -> None:
    if along <= 0 or depth <= 0:
        raise SiteError(f"allocation {name!r}: along and depth must be positive (got {along} x {depth})")
    if depth > plan.district_depth:
        raise SiteError(f"allocation {name!r}: depth {depth} exceeds district depth {plan.district_depth}")
    if any(a.name == name for a in plan.allocations):
        raise SiteError(f"allocation {name!r} already exists")


def allocate(
    plan, world: WorldMap, kind: str, along: int, depth: int, name: str, returns_to_bus: bool = True
) -> Allocation:
    """Reserve and record the next free district block (see module docstring).

    ``returns_to_bus`` selects the output edge (see :class:`Allocation`).
    """
    _check_request(plan, name, along, depth)
    preferred = _preferred_side(plan)
    options = []
    for rank, side in enumerate((preferred, _other_side(preferred))):
        position = first_fit(plan, world, side, along, depth)
        if position is not None:
            options.append((position, rank, side))
    if not options:
        raise SiteError(
            f"allocation {name!r}: bus exhausted, no free {along} x {depth} slot on either side "
            f"of the {plan.bus.length}-tile bus"
        )
    position, _rank, side = min(options)
    allocation = _make_allocation(plan, name, kind, side, position, position + along, depth, returns_to_bus)
    plan.allocations.append(allocation)
    world.release("districts:" + side)
    world.reserve(allocation.rect, allocation.owner)
    rezone_side(plan, world, side)
    return allocation


def _find(plan, name: str) -> Allocation:
    for allocation in plan.allocations:
        if allocation.name == name:
            return allocation
    raise SiteError(f"no allocation named {name!r}")


def release(plan, world: WorldMap, name: str) -> Allocation:
    """Forget one allocation and give its land back to the strip."""
    allocation = _find(plan, name)
    plan.allocations.remove(allocation)
    world.release(allocation.owner)
    rezone_side(plan, world, allocation.side)
    return allocation


def extend(plan, world: WorldMap, name: str, extra_along: int) -> Allocation:
    """Grow an allocation by ``extra_along`` tiles away from the bus head.

    The grown block must stay inside its strip, keep the gap to the next
    block, and the added tiles must be free (trees/rocks allowed, no ore).
    """
    if extra_along <= 0:
        raise SiteError(f"extend {name!r}: extra_along must be positive")
    old = _find(plan, name)
    new_end = old.along1 + extra_along
    strip = next((s for s in _side_strips(plan, old.side) if s.along0 <= old.along0 and old.along1 <= s.along1), None)
    if strip is None or new_end > strip.along1:
        raise SiteError(f"extend {name!r}: district strip ends before along {new_end}")
    others = _side_blocks(plan, old.side, skip=name)
    clash = _gap_clash(others, old.along0, new_end, plan.config.district_gap)
    if clash is not None:
        raise SiteError(f"extend {name!r}: would come within the corridor gap of {clash.name!r}")
    added = block_rect(plan, old.side, old.along1, new_end, old.depth)
    if _blocked_along(plan, world, added, "districts:" + old.side) is not None:
        raise SiteError(f"extend {name!r}: the {extra_along} tiles beyond along {old.along1} are not free")
    grown = _make_allocation(plan, old.name, old.kind, old.side, old.along0, new_end, old.depth, old.returns_to_bus)
    plan.allocations[plan.allocations.index(old)] = grown
    world.release(old.owner)
    world.release("districts:" + old.side)
    world.reserve(grown.rect, grown.owner)
    rezone_side(plan, world, old.side)
    return grown
