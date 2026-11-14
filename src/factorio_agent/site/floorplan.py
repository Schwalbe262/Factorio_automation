"""The planned floor: one slot per known block along the bus, chosen by production chain.

The allocator alone is first come, first served (``allocator.first_fit``). The floor plans the
whole district once, when the master plan is made, from an ordered list of :class:`FloorBlock`
specs (``factorio_agent.floor_blocks.floor_blocks``: roadmap order + rocket bill, real
footprints, a growth allowance). Each block gets a :class:`FloorSlot` (side, along range) whose
frame includes its growth length. The allocator then hands a block its own slot; a block the
floor does not know is placed by the same score, never on another block's unclaimed slot while
other land is free (:func:`choose`).

Slot score (lower wins; the same function at plan time and at run time, :func:`score`):

* ``W_HEAD * earliness * along0`` - early blocks sit near the bus head (earliness 1 for the first
  roadmap block down to ``MIN_EARLINESS`` for the last);
* ``W_LINK * feed`` + ``W_CROSS`` per placed partner that exchanges a privately routed item (an
  intermediate that is not taken from a bus lane); ``feed`` is the belt length between them, round
  the nearer bus end when they stand on different sides (no private belt may cross the bus band,
  ``transport.bus_band``), so suppliers and consumers become neighbours on one side;
* ``W_UPSTREAM`` per bus lane input whose producing block sits downstream of the consumer (a lane
  fed mid-bus reaches only blocks downstream of its feed);
* ``W_NO_GROWTH`` when the frame with the growth allowance does not fit and only the built length
  does.

Assignment order is the roadmap rank (a consumer's suppliers come before it). An over-full bus
gives up optional growth room (latest block first) before it gives up blocks
(:func:`assign_floor`); room for a block's sibling row is demand and is kept.

Run time (:func:`choose`): a block takes its own slot, compacted behind whatever really stands
before it (an upstream block that came out shorter leaves no hole) and moved along the bus in order
when the spot does not fit; a sibling row (``names[1:]``) goes into its family's frame. A block
the floor does not know, or one without a slot, takes the best scored free land off the unclaimed
slots; on a full bus the slots of blocks not reached yet give way (those blocks move along when
they come), then slots latest block first, then other blocks' unused growth room.

Bad terrain degrades gracefully: candidate positions come from a forward scan of each side
(``allocator.fit_from``), so a slot that does not fit moves along the bus in order; a block that
fits nowhere at plan time keeps an unplaced slot (side "", a warning) whose relations still score
its position when it appears.

Old plans: a plan without a ``floor`` (written by older code) gets one when it is loaded with
floor blocks (``SiteService``): existing allocations stay where they are and become fixed slots
of their blocks (``fixed=True``); only blocks without an allocation get new slots.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re

from . import allocator
from .planmodel import SiteError

FLOOR_VERSION = 1

W_HEAD = 1.0
W_LINK = 2.0
W_CROSS = 400.0
W_UPSTREAM = 600.0
W_NO_GROWTH = 150.0
MIN_EARLINESS = 0.25

_INDEX = re.compile(r"#\d+$")


def base_name(name: str) -> str:
    """Allocation name without its candidate index (``steel_production#2`` -> ``steel_production``)."""
    return _INDEX.sub("", name)


@dataclass(frozen=True)
class FloorBlock:
    """One block the base will need.

    ``names`` are the allocation base names that use this block's slot (the first is the main one;
    e.g. a produce row and its belt-output row, or two candidate geometries of one block).
    ``inputs`` are every item the block consumes, ``private`` the inputs it routes from its
    supplier's outlet instead of a bus tap; ``outputs`` its products. ``along`` x ``depth`` is the
    built footprint (along the bus x away from it), ``growth`` the extra along tiles its frame
    holds for in-place growth. ``rank`` is the roadmap order (0 first).
    """

    names: tuple[str, ...]
    kind: str
    along: int
    depth: int
    growth: int = 0
    inputs: tuple[str, ...] = ()
    private: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    rank: int = 0

    @property
    def name(self) -> str:
        return self.names[0]


@dataclass(frozen=True)
class FloorSlot:
    """A block's planned frame: ``[along0, along1)`` on ``side`` (``along1`` includes growth).

    ``built`` is the planned built length (``along1 - along0 - built`` tiles are growth room).
    ``fixed`` slots mirror an allocation that existed before the floor was planned (old plans).
    """

    names: tuple[str, ...]
    kind: str
    side: str
    along0: int
    along1: int
    depth: int
    built: int
    rank: int
    inputs: tuple[str, ...] = ()
    private: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    fixed: bool = False

    @property
    def name(self) -> str:
        return self.names[0]

    def to_dict(self) -> dict:
        return {
            "names": list(self.names),
            "kind": self.kind,
            "side": self.side,
            "along0": self.along0,
            "along1": self.along1,
            "depth": self.depth,
            "built": self.built,
            "rank": self.rank,
            "inputs": list(self.inputs),
            "private": list(self.private),
            "outputs": list(self.outputs),
            "fixed": self.fixed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FloorSlot:
        values = dict(data)
        for key in ("names", "inputs", "private", "outputs"):
            values[key] = tuple(values.get(key) or ())
        return cls(**values)


def floor_to_dict(slots) -> dict | None:
    if slots is None:
        return None
    return {"version": FLOOR_VERSION, "slots": [s.to_dict() for s in slots]}


def floor_from_dict(data) -> tuple[FloorSlot, ...] | None:
    """Slots of a stored floor; None for a missing floor or another floor version (re-planned)."""
    if not isinstance(data, dict) or data.get("version") != FLOOR_VERSION:
        return None
    return tuple(FloorSlot.from_dict(s) for s in data.get("slots") or ())


# ----- relations -------------------------------------------------------------------------------
@dataclass
class _Placed:
    """Geometry + relations of a placed partner (an allocation or a planned slot)."""

    side: str
    along0: int
    along1: int
    depth: int
    inputs: tuple[str, ...]
    private: tuple[str, ...]
    outputs: tuple[str, ...]
    names: tuple[str, ...] = field(default_factory=tuple)


def _bus_items(plan) -> set[str]:
    return {lane.material for lane in plan.bus.lanes if lane.material}


def linked(a, b) -> bool:
    """True when ``a`` and ``b`` exchange a privately routed item (either direction)."""
    return bool(set(a.outputs) & set(b.private)) or bool(set(b.outputs) & set(a.private))


def feed(plan, block, partner) -> float:
    """Belt length between the centres of ``block`` = ``(side, along0, along1, depth)`` and
    ``partner``: Manhattan on one side; across the bus round the nearer bus end (no private belt
    crosses the bus band, ``transport.bus_band``)."""
    side, a0, a1, depth = block
    setback = plan.config.district_setback
    along = (a0 + a1) / 2
    other = (partner.along0 + partner.along1) / 2
    lateral = setback + depth / 2
    other_lateral = setback + partner.depth / 2
    if partner.side == side:
        return abs(along - other) + abs(lateral - other_lateral)
    length = plan.bus.length
    around = min(along + other, 2 * length - along - other)
    return around + lateral + other_lateral + plan.bus.width


def score(plan, spec, side: str, along0: int, along1: int, depth: int, partners, earliness: float = 1.0) -> float:
    """Slot score of ``spec`` at ``[along0, along1)`` on ``side`` (module doc); lower is better."""
    bus = _bus_items(plan)
    total = W_HEAD * earliness * along0
    taps = set(spec.inputs) - set(spec.private)
    for partner in partners:
        if linked(spec, partner):
            total += W_LINK * feed(plan, (side, along0, along1, depth), partner)
            if partner.side != side:
                total += W_CROSS
        if taps & set(partner.outputs) & bus and along0 < partner.along1:
            total += W_UPSTREAM
        produced = set(spec.outputs) & bus & (set(partner.inputs) - set(partner.private))
        if produced and partner.along0 < along1:
            total += W_UPSTREAM
    return total


# ----- positions ---------------------------------------------------------------------------------
def _held(plan, slots, skip=None) -> list[tuple[str, int, int, str]]:
    """``(side, along0, along1, name)`` spans of slots no allocation claims yet (except ``skip``),
    latest roadmap rank last."""
    claimed = {base_name(a.name) for a in plan.allocations}
    rows = [s for s in slots if s is not skip and s.side and not s.fixed and not (set(s.names) & claimed)]
    return [(s.side, s.along0, s.along1, s.name) for s in sorted(rows, key=lambda s: s.rank)]


def _block_spans(plan, side: str, with_growth: bool) -> list[tuple[int, int, str]]:
    if with_growth:
        return allocator.spans(plan, side)
    return [(a.along0, a.along1, a.name) for a in plan.allocations if a.side == side]


def positions(plan, world, length: int, depth: int, held, *, with_growth: bool = True) -> list[tuple[str, int]]:
    """Every packed free start ``(side, along0)`` of a ``length`` x ``depth`` block.

    Starts are the strip starts and each block or held span end plus the gap, each scanned forward
    (``allocator.fit_from``), so terrain moves a position along the bus in order. ``with_growth``
    False lets the block take other blocks' unused growth room (the last resort of :func:`choose`).
    """
    gap = plan.config.district_gap
    found = set()
    for side in ("left", "right"):
        spans = _block_spans(plan, side, with_growth) + [(a0, a1, name) for s, a0, a1, name in held if s == side]
        starts = {strip.along0 for strip in plan.strips if strip.side == side}
        starts |= {a1 + gap for _a0, a1, _n in spans}
        for start in sorted(starts):
            position = allocator.fit_from(plan, world, side, start, length, depth, spans)
            if position is not None:
                found.add((side, position))
    return sorted(found, key=lambda p: (p[1], p[0]))


def _earliness(rank: int, count: int) -> float:
    if count <= 1:
        return 1.0
    return max(MIN_EARLINESS, 1.0 - (1.0 - MIN_EARLINESS) * rank / (count - 1))


def best(
    plan,
    world,
    spec,
    depth: int,
    partners,
    held,
    *,
    earliness: float = 1.0,
    along=None,
    growth=None,
    with_growth: bool = True,
    tail: bool = False,
):
    """``(score, side, along0, growth)`` of the best position, or None.

    The frame ``along + growth`` is tried first; only the built ``along`` (no growth) costs
    ``W_NO_GROWTH`` more. ``tail`` picks the position furthest down the bus instead of the best
    score (a block that pushes other slots along pushes the fewest).
    """
    along = spec.along if along is None else along
    growth = spec.growth if growth is None else growth
    preferred = allocator.preferred_side(plan)
    options = []
    for room, penalty in ((growth, 0.0), (0, W_NO_GROWTH if growth else 0.0)):
        for side, position in positions(plan, world, along + room, depth, held, with_growth=with_growth):
            value = score(plan, spec, side, position, position + along, depth, partners, earliness) + penalty
            options.append((round(value, 6), position, side != preferred, side, room))
        if options and room == growth:
            break
    if not options:
        return None
    if tail:
        value, position, _rank, side, room = max(options, key=lambda o: (o[1], -o[0]))
    else:
        value, position, _rank, side, room = min(options)
    return value, side, position, room


# ----- plan time ---------------------------------------------------------------------------------
def _order(blocks) -> list:
    """Roadmap rank: every block is placed after its suppliers (module doc)."""
    return sorted(blocks, key=lambda b: b.rank)


def _fixed_slot(block, allocation) -> FloorSlot:
    return FloorSlot(
        names=block.names,
        kind=block.kind,
        side=allocation.side,
        along0=allocation.along0,
        along1=allocation.along1 + allocation.growth,
        depth=allocation.depth,
        built=allocation.along1 - allocation.along0,
        rank=block.rank,
        inputs=block.inputs,
        private=block.private,
        outputs=block.outputs,
        fixed=True,
    )


def assign_floor(plan, world, blocks) -> tuple[tuple[FloorSlot, ...], list[str]]:
    """Slots for ``blocks`` on ``plan``'s district strips; ``(slots, warnings)``.

    Blocks that already have an allocation keep it (a fixed slot); the others are placed in
    :func:`_order` by :func:`best`. Nothing is reserved on the map: the allocator honours the slots.
    """
    fixed: list[FloorSlot] = []
    by_base = {}
    for allocation in plan.allocations:
        by_base.setdefault(base_name(allocation.name), allocation)
    todo = []
    for block in blocks:
        allocation = next((by_base[n] for n in block.names if n in by_base), None)
        if allocation is not None:
            fixed.append(_fixed_slot(block, allocation))
        else:
            todo.append(block)
    count = max((b.rank for b in blocks), default=0) + 1
    # An over-full bus gives up growth room before blocks: the most blocks that fit without any
    # growth room is the target; growth allowances are dropped latest roadmap block first until the
    # floor places that many (an over-committed floor packs like first-fit, never fewer blocks).
    # Room for a sibling row the block's family will ask for (``names`` beyond the first) is demand,
    # not an option, and is never dropped.
    optional = [b for b in todo if b.growth and len(b.names) == 1]
    lean = {b.name: 0 for b in optional}

    def placed(run) -> int:
        return sum(1 for s in run[0] if s.side and not s.fixed)

    target = placed(_assign(plan, world, todo, list(fixed), count, lean))
    growth = {b.name: b.growth for b in todo}
    latest = sorted(optional, key=lambda b: -b.rank)
    run = _assign(plan, world, todo, list(fixed), count, growth)
    dropped = 0
    while placed(run) < target and dropped < len(latest):
        growth[latest[dropped].name] = 0
        dropped += 1
        run = _assign(plan, world, todo, list(fixed), count, growth)
    slots, warnings = run
    if dropped:
        names = ", ".join(b.name for b in latest[:dropped])
        warnings.append(f"floor: growth room dropped for {names} so more blocks fit the bus")
    return tuple(slots), warnings


def _assign(plan, world, todo, slots, count: int, growth: dict):
    warnings: list[str] = []
    for block in _order(todo):
        if growth.get(block.name, block.growth) != block.growth:
            block = replace(block, growth=growth[block.name])
        if block.depth > plan.district_depth or block.along <= 0:
            warnings.append(f"floor: {block.name} {block.along}x{block.depth} does not fit the district depth")
            continue
        partners = [_as_placed(s) for s in slots if s.side]
        found = best(
            plan,
            world,
            block,
            block.depth,
            partners,
            _held(plan, slots),
            earliness=_earliness(block.rank, count),
        )
        if found is None:
            warnings.append(f"floor: no slot for {block.name} ({block.along}+{block.growth} x {block.depth})")
            # Kept unplaced (side ""): its relations and frame length (along1) still score and size
            # its position when it appears.
            slots.append(
                FloorSlot(
                    names=block.names,
                    kind=block.kind,
                    side="",
                    along0=0,
                    along1=block.along + block.growth,
                    depth=block.depth,
                    built=block.along,
                    rank=block.rank,
                    inputs=block.inputs,
                    private=block.private,
                    outputs=block.outputs,
                )
            )
            continue
        _value, side, position, room = found
        if room < block.growth:
            warnings.append(f"floor: {block.name} gets no growth room")
        slots.append(
            FloorSlot(
                names=block.names,
                kind=block.kind,
                side=side,
                along0=position,
                along1=position + block.along + room,
                depth=block.depth,
                built=block.along,
                rank=block.rank,
                inputs=block.inputs,
                private=block.private,
                outputs=block.outputs,
            )
        )
    return slots, warnings


def _as_placed(slot: FloorSlot) -> _Placed:
    return _Placed(
        slot.side,
        slot.along0,
        slot.along0 + slot.built,
        slot.depth,
        slot.inputs,
        slot.private,
        slot.outputs,
        slot.names,
    )


# ----- run time ------------------------------------------------------------------------------------
def slot_of(plan, name: str) -> FloorSlot | None:
    base = base_name(name)
    return next((s for s in plan.floor or () if base in s.names), None)


def _partners(plan, slot) -> list[_Placed]:
    """Partners of ``slot``'s block: allocated blocks at their allocation, the others at their slot."""
    out = []
    for other in plan.floor or ():
        if other is slot:
            continue
        allocation = next((a for a in plan.allocations if base_name(a.name) in other.names), None)
        if allocation is None:
            if other.side:
                out.append(_as_placed(other))
        else:
            out.append(
                _Placed(
                    allocation.side,
                    allocation.along0,
                    allocation.along1,
                    allocation.depth,
                    other.inputs,
                    other.private,
                    other.outputs,
                    other.names,
                )
            )
    return out


def _shrinks(plan, side: str, along0: int, along1: int) -> list[tuple[str, int]]:
    """``(name, growth)`` cuts that keep every allocation's growth room off ``[along0, along1)`` + gap."""
    gap = plan.config.district_gap
    cuts = []
    for a in plan.allocations:
        if a.side == side and a.growth and a.along1 <= along0 and a.along1 + a.growth + gap > along0:
            cuts.append((a.name, max(0, along0 - gap - a.along1)))
    return cuts


def choose(plan, world, name: str, along: int, depth: int):
    """``(side, along0, growth, shrinks)`` for a new allocation ``name``, or None (bus exhausted).

    ``shrinks`` are ``(allocation, growth)`` cuts of other blocks' growth room the new block takes.
    Order (module doc): a sibling row in its family's frame; the block's own unclaimed slot; the
    best scored position off other blocks' unclaimed slots; on a full bus the slots of blocks not
    reached yet give way, then slots latest block first (an arriving block outranks a planned
    future one); last, other blocks' unused growth room.
    """
    gap = plan.config.district_gap
    slot = slot_of(plan, name)
    base = base_name(name)
    if slot is not None:
        family = [a for a in plan.allocations if base_name(a.name) in slot.names and base_name(a.name) != base]
        for sibling in sorted(family, key=lambda a: -a.along1):
            start = sibling.along1 + gap
            spans = [s for s in allocator.spans(plan, sibling.side) if s[2] != sibling.name]
            spans += [(a0, a1, n) for side, a0, a1, n in _held(plan, plan.floor, skip=slot) if side == sibling.side]
            if allocator.fit_from(plan, world, sibling.side, start, along, depth, spans, exact=True) is not None:
                room = max(0, sibling.along1 + sibling.growth - (start + along))
                return sibling.side, start, room, [(sibling.name, 0)]
        claimed = any(base_name(a.name) in slot.names for a in plan.allocations)
        if slot.side and not claimed and depth <= plan.district_depth:
            # Slots of later blocks give way (they move along the bus in order when they come).
            earlier = [s for s in plan.floor if s.rank < slot.rank]
            held = _held(plan, earlier, skip=slot)
            spans = allocator.spans(plan, slot.side) + [(a0, a1, n) for s, a0, a1, n in held if s == slot.side]
            # Compact: a block upstream that came out shorter than planned leaves no hole; the
            # slot starts right behind whatever really stands before it.
            standing = spans + [(a0, a1, n) for s, a0, a1, n in _held(plan, plan.floor, skip=slot) if s == slot.side]
            before = [a1 for _a0, a1, _n in standing if a1 <= slot.along0]
            start = min(slot.along0, max(before) + gap) if before else 0
            planned = max(0, slot.along1 - slot.along0 - slot.built)
            # Its own spot with its growth room (moved along by at most that room), then without
            # the room, before anything further down.
            for exact, room, reach in (
                (False, planned, planned),
                (True, 0, 0),
                (False, planned, None),
                (False, 0, None),
            ):
                position = allocator.fit_from(plan, world, slot.side, start, along + room, depth, spans, exact=exact)
                if position is not None and (reach is None or position <= start + reach):
                    return slot.side, position, room, []
    spec = slot or FloorBlock(names=(base,), kind="", along=along, depth=depth)
    partners = _partners(plan, slot) if slot is not None else []
    growth = max(0, (slot.along1 - slot.along0) - slot.built) if slot is not None else 0
    held = _held(plan, plan.floor, skip=slot)
    found = best(plan, world, spec, depth, partners, held, along=along, growth=growth)
    if found is None:
        # No land off the planned slots: the slots of blocks not reached yet (a roadmap rank above
        # every block already standing) give way; those blocks move along the bus in order when
        # they come (their own-slot scan), so the bus packs like first-fit instead of fragmenting ...
        rank = {s.name: s.rank for s in plan.floor or ()}
        claimed = {base_name(a.name) for a in plan.allocations}
        reached = max((s.rank for s in plan.floor or () if set(s.names) & claimed), default=-1)
        earlier = [h for h in held if rank.get(h[3], 0) <= reached]
        if len(earlier) < len(held):
            found = best(plan, world, spec, depth, partners, earlier, along=along, growth=growth)
    # ... else give up slots latest roadmap block first.
    keep = len(held)
    while found is None and keep > 0:
        keep -= 1
        found = best(plan, world, spec, depth, partners, held[:keep], along=along, growth=growth)
    if found is not None:
        _value, side, position, room = found
        return side, position, room, []
    found = best(plan, world, spec, depth, partners, [], along=along, growth=0, with_growth=False)
    if found is None:
        return None
    _value, side, position, _room = found
    return side, position, 0, _shrinks(plan, side, position, position + along)


def require(plan, world, name, along, depth):
    found = choose(plan, world, name, along, depth)
    if found is None:
        raise SiteError(
            f"allocation {name!r}: bus exhausted, no free {along} x {depth} slot on either side "
            f"of the {plan.bus.length}-tile bus"
        )
    return found
