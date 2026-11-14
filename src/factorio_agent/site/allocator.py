"""District allocation along the main bus.

Blocks are placed in the district strips on either side of the bus:

* the block's near edge is ``district_setback`` tiles off the bus edge (the
  setback band stays in the strip and is where taps connect);
* neighbouring blocks on the same side keep ``district_gap`` tiles of
  corridor between them (measured along the bus);
* with a planned floor (``plan.floor``, :mod:`site.floorplan`) a block takes
  its own slot and unknown blocks the best scored free land; without one
  (a plan planned without floor blocks) the slot nearest to the bus head
  wins; on a tie the side opposite to the previous allocation wins
  (``first_side`` for the very first one), so equal blocks alternate sides;
* a block's span includes its held growth room (``Allocation.growth``):
  no other block is placed there and :func:`extend` grows into it.

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

from dataclasses import replace

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
    """First block (nearest the head) closer than ``gap`` to ``[along0, along1)``.

    A block's span includes its held growth room (``Allocation.growth``).
    """
    for block in sorted(blocks, key=lambda b: b.along0):
        if block.along0 - gap < along1 and along0 < block.along1 + block.growth + gap:
            return block
    return None


def spans(plan, side: str, skip: str | None = None) -> list[tuple[int, int, str]]:
    """``(along0, along1 + growth, name)`` of every allocation on ``side`` (growth room included)."""
    return [(a.along0, a.along1 + a.growth, a.name) for a in _side_blocks(plan, side, skip)]


def fit_from(
    plan, world: WorldMap, side: str, start: int, along: int, depth: int, taken, *, exact: bool = False
) -> int | None:
    """Smallest bus-local start ``>= start`` of a free ``along`` x ``depth`` block on ``side``.

    ``taken`` are ``(along0, along1, name)`` spans the block keeps the corridor gap to (allocations
    with growth, held floor slots). Skips forward past strip ends, gap clashes and blocked tiles;
    ``exact`` only tests ``start``. Returns None when the side is exhausted (or ``start`` is not free).
    """
    strips = _side_strips(plan, side)
    gap = plan.config.district_gap
    position = max(0, start)
    while True:
        strip = next((s for s in strips if s.along1 - max(position, s.along0) >= along), None)
        if strip is None:
            return None
        if exact and (position < strip.along0 or position + along > strip.along1):
            return None
        position = max(position, strip.along0)
        clash = next(
            (s for s in sorted(taken) if s[0] - gap < position + along and position < s[1] + gap),
            None,
        )
        if clash is not None:
            if exact:
                return None
            position = clash[1] + gap
            continue
        rect = block_rect(plan, side, position, position + along, depth)
        stop = _blocked_along(plan, world, rect, "districts:" + side)
        if stop is not None:
            if exact:
                return None
            position = stop + 1
            continue
        return position


def first_fit(plan, world: WorldMap, side: str, along: int, depth: int) -> int | None:
    """Smallest bus-local start of a free ``along`` x ``depth`` block on ``side``.

    Skips forward past strip ends, gap clashes and blocked tiles. Returns
    None when the side is exhausted.
    """
    return fit_from(plan, world, side, 0, along, depth, spans(plan, side))


def _other_side(side: str) -> str:
    return "left" if side == "right" else "right"


def preferred_side(plan) -> str:
    return _preferred_side(plan)


def _preferred_side(plan) -> str:
    """Side tried first: alternate after the previous block.

    The very first block goes to the side with more district strip (tie: the
    side whose strips lie nearer the start, then ``first_side``), so a
    mirrored map makes the mirrored choice.
    """
    if plan.allocations:
        return _other_side(plan.allocations[-1].side)

    def rank(side: str) -> tuple:
        strips = _side_strips(plan, side)
        total = sum(s.along1 - s.along0 for s in strips)
        near = min((_distance(s.rect.centre(), plan.start) for s in strips), default=float("inf"))
        return (-total, round(near, 6), side != plan.config.first_side)

    return min(("left", "right"), key=rank)


def _distance(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _output_point(plan, side: str, along1: int, depth: int, returns_to_bus: bool) -> tuple[float, float]:
    """Centre of the last tile row (downstream end) on the near or far edge."""
    setback = plan.config.district_setback
    distance = setback if returns_to_bus else setback + depth
    lateral = plan.bus.half + distance
    if side == "left":
        lateral = -lateral
    return plan.bus.local_point(along1 - 0.5, lateral)


def _make_allocation(
    plan,
    name: str,
    kind: str,
    side: str,
    along0: int,
    along1: int,
    depth: int,
    returns_to_bus: bool,
    growth: int = 0,
    settled: bool | None = None,
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
        growth=growth,
        settled=settled,
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
    plan,
    world: WorldMap,
    kind: str,
    along: int,
    depth: int,
    name: str,
    returns_to_bus: bool = True,
    settled: bool | None = None,
) -> Allocation:
    """Reserve and record a district block.

    With a planned floor (``plan.floor``, :mod:`site.floorplan`) the block gets its own slot (or
    its family's frame, or the best scored position); without one (old plans planned no floor
    blocks) the next free block in the module docstring's order. ``returns_to_bus`` selects the
    output edge (see :class:`Allocation`); ``settled`` False marks an unchosen candidate.
    """
    _check_request(plan, name, along, depth)
    growth, shrinks = 0, []
    if getattr(plan, "floor", None):
        from . import floorplan

        side, position, growth, shrinks = floorplan.require(plan, world, name, along, depth)
    else:
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
    took = []
    for other_name, room in shrinks:
        other = _find(plan, other_name)
        took.append((other.name, other.along1 + other.growth))
        plan.allocations[plan.allocations.index(other)] = replace(other, growth=room)
    allocation = _make_allocation(
        plan, name, kind, side, position, position + along, depth, returns_to_bus, growth, settled
    )
    allocation = replace(allocation, took=tuple(took))
    plan.allocations.append(allocation)
    world.release("districts:" + side)
    world.reserve(allocation.rect, allocation.owner)
    rezone_side(plan, world, side)
    return allocation


def settle(plan, name: str) -> Allocation:
    """Mark ``name`` as the chosen block of its candidates (never released as an unused candidate)."""
    allocation = _find(plan, name)
    if allocation.settled is True:
        return allocation
    settled = replace(allocation, settled=True)
    plan.allocations[plan.allocations.index(allocation)] = settled
    return settled


def _find(plan, name: str) -> Allocation:
    for allocation in plan.allocations:
        if allocation.name == name:
            return allocation
    raise SiteError(f"no allocation named {name!r}")


def release(plan, world: WorldMap, name: str) -> Allocation:
    """Forget one allocation and give its land back to the strip."""
    allocation = _find(plan, name)
    plan.allocations.remove(allocation)
    # Neighbours whose growth room this block took get it back (a sibling row, a last-resort fit).
    for other_name, frame_end in allocation.took:
        other = next((a for a in plan.allocations if a.name == other_name), None)
        if other is None or other.along1 + other.growth >= frame_end:
            continue
        nxt = min(
            (a.along0 for a in _side_blocks(plan, other.side, other.name) if a.along0 >= other.along1), default=None
        )
        if nxt is not None:
            frame_end = min(frame_end, nxt - plan.config.district_gap)
        if frame_end > other.along1 + other.growth:
            plan.allocations[plan.allocations.index(other)] = replace(other, growth=frame_end - other.along1)
    world.release(allocation.owner)
    rezone_side(plan, world, allocation.side)
    return allocation


def extend(plan, world: WorldMap, name: str, extra_along: int) -> Allocation:
    """Grow an allocation by ``extra_along`` tiles away from the bus head.

    Inside its held growth room (``Allocation.growth``) only the added tiles must be free; beyond
    it the grown block must stay inside its strip, keep the gap to the next block, and the added
    tiles must be free (trees/rocks allowed, no ore).
    """
    if extra_along <= 0:
        raise SiteError(f"extend {name!r}: extra_along must be positive")
    old = _find(plan, name)
    new_end = old.along1 + extra_along
    strip = next((s for s in _side_strips(plan, old.side) if s.along0 <= old.along0 and old.along1 <= s.along1), None)
    if strip is None or new_end > strip.along1:
        raise SiteError(f"extend {name!r}: district strip ends before along {new_end}")
    if new_end > old.along1 + old.growth:
        others = _side_blocks(plan, old.side, skip=name)
        clash = _gap_clash(others, old.along0, new_end, plan.config.district_gap)
        if clash is not None:
            raise SiteError(f"extend {name!r}: would come within the corridor gap of {clash.name!r}")
    added = block_rect(plan, old.side, old.along1, new_end, old.depth)
    if _blocked_along(plan, world, added, "districts:" + old.side) is not None:
        raise SiteError(f"extend {name!r}: the {extra_along} tiles beyond along {old.along1} are not free")
    room = max(0, old.along1 + old.growth - new_end)
    grown = _make_allocation(
        plan, old.name, old.kind, old.side, old.along0, new_end, old.depth, old.returns_to_bus, room, old.settled
    )
    plan.allocations[plan.allocations.index(old)] = grown
    world.release(old.owner)
    world.release("districts:" + old.side)
    world.reserve(grown.rect, grown.owner)
    rezone_side(plan, world, old.side)
    return grown
