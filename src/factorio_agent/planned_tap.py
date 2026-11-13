"""Taps on the planned main bus (``planned_bus.py``), in the bus frame, any direction, either side.

A consumer beside the bus takes plates from a *tap*: a splitter on the
trunk plus a short branch that ends in a port just outside the bus band on
the consumer's side. The consumer plans its taps together with its own
block (``connect_inputs``), so tap and block are one plan; the tap is built
first (``cutover``) so the trunk is interrupted only for a few actions.

Geometry, bus-local (``site.busframe``): ``a`` = splitter row (tiles from
the head), ``o`` = lateral offset of the tapped lane, ``s`` = +1 for the
right side, -1 for the left side, ``E`` = first lateral tile outside the bus
band on that side, ``P = E + s`` the port tile::

    row a     splitter on lanes o and o+s (the "partner" tile)
    row a+1   belt on o+s, facing the bus direction
    row a+2   branch row: a curve on o+s turning outward, then straight to P;
              reserved and spare lanes on the way are tunnelled under
              (underground pairs, mouths on gap tiles, span <= max span)
    rows a+1..a+3  every *built* trunk between o+s and the edge dives under
              the branch row (underground pair a+1 -> a+3 on that lane)

Rules (``layout``): the partner tile may not be a built trunk; rows ``a`` to
``a+3`` of every involved lane must be plain trunk belts (or not built yet);
no other trunk underground within the max span of a dive; tap windows
``[a, a+3]`` of different taps keep ``WINDOW_GAP`` rows apart; ``a >= 2``.
A lane involved in a tap that ends before ``a + 4 + EXTEND_MARGIN`` is
extended (paid trunk belts, foreign belts crossed underground like the
original trunk, capped at the bus length); beyond the bus length the tap is
a precise blocker (``tap_beyond_bus_length``).

Not yet: a later trunk on a reserved lane must pass under existing taps'
splitter / branch tiles on that lane; ``planned_bus.trunk_plan`` only crosses
plain belts, so building more lanes needs that rule first.

Splitter priorities (``planned_bus.tap_priorities``): output priority to the
branch, input priority none (one input). The side is derived from the
splitter's facing and the tap port, so it holds for every bus direction and
side. The consumer takes what its machines use, up to the whole lane; only the
surplus continues down the bus. ``cutover`` configures each built tap splitter
(``configure_splitter``, idempotent) before it returns None, also for taps
built before this rule.

Supply rule (``tap_budget``): a trunk carries the district's nominal plate
supply; every tap records its consumer's declared ``demand_per_minute``. A
new consumer may plan with ``max(nominal - sum(other taps' demand),
nominal / 2)`` and sizes its machine count down to that. While declared
demand stays within the trunk, branch priority hands every tap its full
demand wherever it sits on the bus. The half floor is only an
over-subscription allowance, not a guarantee: with branch priority the taps
upstream are served first and a downstream consumer gets the rest (an even
splitter, such as the district's green branch without a bus, does hand half).
Consumers then run below nameplate, which ``tools/report_run.py`` measures.

State: taps live in ``planned-main-bus.json`` ``taps`` (full records) and in
``board["shared_main_bus"]`` (``stations``, tap ``ports``, entities, pairs),
both written by ``publish``; the consumer keeps its own copy in its state.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path

from .board import blocks_without_bus, entity, foreign_belts, surface_conflict
from .core.deterministic_builder import _plan
from .core.deterministic_state import _atomic_json
from .planned_bus import branch_priorities, configure_action, splitter_side, tap_priorities
from .site.busframe import Bus, compass_of, right_of
from .smelting import merge_entities
from .transport import compile_crossings, plan_connection, xy

TEMPLATE = "planned_main_bus"
STATE_FILE = "planned-main-bus.json"
FACING = {"N": 0, "E": 4, "S": 8, "W": 12}
WINDOW_ROWS = 4  # rows a .. a+3
WINDOW_GAP = 1  # free rows between two tap windows
EXTEND_MARGIN = 4  # trunk tiles past a tap window when a lane is extended
FIRST_ROW = 2
ATTEMPTS = 3  # tap rows tried per consumer input (each costs one route)
SEARCH_MARGIN = 16  # tap rows searched beyond the consumer block's along range
MAX_SPAN_LUA = 'return {ok=true,max_span=prototypes.entity["underground-belt"].max_underground_distance}'


def max_span(game) -> int | None:
    reply = game.query(MAX_SPAN_LUA)
    return reply["max_span"] if isinstance(reply, dict) and reply.get("ok") else None


def published(board: dict, world_id: str) -> dict | None:
    """The planned bus of ``board`` for this world, or None (no bus, other template, other world)."""
    bus = board.get("shared_main_bus")
    if not isinstance(bus, dict) or bus.get("template") != TEMPLATE or bus.get("world_id") != world_id:
        return None
    return bus


def carries(bus: dict | None, item: str) -> bool:
    """True when the published bus has a built trunk of ``item``."""
    return bool(bus) and any(t.get("item") == item for t in bus.get("trunks", []))


def tap_name(owner: str, item: str) -> str:
    return f"{owner}:{item}"


def claimed(bus: dict, item: str, *, owner: str) -> float:
    """Declared demand of every other consumer's tap on ``item``."""
    return sum(
        float(s.get("demand_per_minute", 0))
        for s in bus.get("stations", [])
        if s.get("item") == item and s.get("owner") != owner
    )


def tap_budget(nominal: float, others: float) -> float:
    """Plates/minute a new tap may plan with (module doc, supply rule).

    ``nominal - others`` is what branch-priority taps guarantee; the half floor is
    an over-subscription allowance (a guarantee only behind an even splitter).
    """
    return max(nominal - others, nominal / 2)


# ----- bus view ------------------------------------------------------------------
def _tile(position) -> tuple:
    return (position["x"], position["y"])


class BusView:
    """The published bus plus the taps planned so far in this prepare (pure, in memory)."""

    def __init__(self, bus: dict):
        self.bus = Bus.from_dict(bus["frame"])
        self.facing = FACING[self.bus.direction]
        self.lanes = {lane.index: lane for lane in self.bus.lanes}
        self.lengths = {t["lane"]: t["length"] for t in bus["trunks"]}
        self.items = {t["item"]: t["lane"] for t in bus["trunks"]}
        self.tiles = {xy(e): e for e in bus["entities"]}
        self.stations = [dict(s) for s in bus.get("stations", [])]

    def world(self, along: float, lateral: float) -> dict:
        """Map position of the centre of local tile ``(along, lateral)`` (floats: any local point)."""
        x, y = self.bus.local_point(along + 0.5, lateral + 0.5)
        return {"x": x, "y": y}

    def along_of(self, position) -> int:
        return self.bus.local_tile(math.floor(position["x"]), math.floor(position["y"]))[0]

    def built_offsets(self) -> dict[int, int]:
        """Lateral offset -> lane index of every built trunk."""
        return {self.lanes[index].offset: index for index in self.lengths}

    def window_free(self, along: int, own: str | None = None) -> bool:
        return all(abs(along - s["along"]) >= WINDOW_ROWS + WINDOW_GAP for s in self.stations if s.get("name") != own)

    def add(self, tap: dict) -> None:
        for position in tap["remove"]:
            self.tiles.pop(_tile(position), None)
        for e in tap["entities"]:
            self.tiles[xy(e)] = e
        for lane, length in tap["extension"].items():
            self.lengths[int(lane)] = max(self.lengths[int(lane)], length)
        self.stations.append(station(tap))


def station(tap: dict) -> dict:
    """Board summary of one tap (``shared_main_bus.stations``)."""
    keys = ("name", "owner", "item", "lane", "side", "along", "demand_per_minute", "port", "built")
    if tap.get("kind") == "feed":  # plates enter the bus here (``feed_layout``): a supply, no demand
        row = {k: tap.get(k) for k in keys}
        row.update(demand_per_minute=0, supply_per_minute=tap.get("supply_per_minute", 0))
        return {"kind": "planned_feed", **row, "splitter": tap["splitter"]["position"]}
    return {"kind": "planned_tap", **{k: tap.get(k) for k in keys}, "splitter": tap["splitter"]["position"]}


# ----- geometry (pure) -------------------------------------------------------------
def _outward(bus: Bus, s: int) -> int:
    rx, ry = right_of(bus.direction)
    return FACING[compass_of((rx * s, ry * s))]


def _extension(view: BusView, lane, target: int, foreign, span: int) -> dict:
    """Trunk belts of ``lane`` from its built length up to ``target`` (foreign belts crossed underground)."""
    start = view.lengths[lane.index]
    if target <= start:
        return {"ok": True, "entities": [], "underground_pairs": []}
    segments = [{"position": view.world(a, lane.offset), "direction": view.facing} for a in range(start, target)]
    route = {"ok": True, "segments": segments, "metrics": {"length": len(segments), "turns": 0}}
    return compile_crossings(route, foreign, maximum_span=span)


def layout(view: BusView, item: str, side: str, along: int, *, span: int, foreign=()) -> dict:
    """One tap of ``item`` at splitter row ``along`` towards ``side`` (module doc), or a failure."""
    bus = view.bus
    if item not in view.items:
        return {"ok": False, "reason": "tap_item_has_no_built_trunk", "item": item}
    lane = view.lanes[view.items[item]]
    s = 1 if side == "right" else -1
    if side not in ("left", "right"):
        return {"ok": False, "reason": "tap_side_unknown", "side": side}
    a = along
    if a < FIRST_ROW:
        return {"ok": False, "reason": "tap_row_too_close_to_head"}
    edge = bus.half if s > 0 else -bus.half - 1
    port_lateral = edge + s
    partner = lane.offset + s
    built = view.built_offsets()
    if partner in built:
        return {"ok": False, "reason": "tap_partner_tile_is_a_built_trunk"}
    lo, hi = sorted((partner, edge))
    dived = [built[off] for off in sorted(built) if lo < off < hi and off != lane.offset]
    target = a + WINDOW_ROWS + EXTEND_MARGIN
    if target > bus.length:
        return {"ok": False, "reason": "tap_beyond_bus_length", "along": a, "bus_length": bus.length}
    # Tiles the tap replaces on built lanes: splitter row of the tapped lane, dive rows of the others.
    replaced = {(lane.index, a)}
    for index in dived:
        replaced.update({(index, a + 1), (index, a + 2), (index, a + 3)})
    entities, pairs, extension, remove = [], [], {}, []
    for index in [lane.index, *dived]:
        current = view.lanes[index]
        if view.lengths[index] < target:
            ext = _extension(view, current, target, foreign, span)
            if not ext.get("ok"):
                return {"ok": False, "reason": "tap_trunk_extension_blocked", "lane": index, "detail": ext}
            for e in ext["entities"]:
                row = bus.local_tile(math.floor(e["position"]["x"]), math.floor(e["position"]["y"]))[0]
                if (index, row) in replaced:
                    if e["name"] != "transport-belt":
                        return {"ok": False, "reason": "tap_rows_meet_trunk_crossing", "lane": index}
                    continue
                entities.append(e)
            pairs += ext.get("underground_pairs", [])
            extension[index] = target
        for row in range(a, a + WINDOW_ROWS):
            if row >= view.lengths[index]:
                continue
            spot = view.world(row, current.offset)
            here = view.tiles.get(_tile(spot))
            plain = here is not None and here["name"] == "transport-belt" and here.get("direction") == view.facing
            if (index, row) in replaced and not plain:
                return {"ok": False, "reason": "tap_rows_meet_trunk_crossing", "lane": index, "along": row}
            if (index, row) in replaced:
                remove.append(spot)
        if index in dived:
            for row in range(a + 1 - span, a + 4 + span):
                here = view.tiles.get(_tile(view.world(row, current.offset)))
                if here is not None and here["name"] == "underground-belt":
                    return {"ok": False, "reason": "tap_dive_near_trunk_underground", "lane": index, "along": row}
    # Splitter, its partner belt and the branch row.
    centre = view.world(a, min(lane.offset, partner) + 0.5)
    splitter = dict(entity("splitter", centre["x"], centre["y"], view.facing), _width=2, _height=1)
    entities += [splitter, entity("transport-belt", **view.world(a + 1, partner), direction=view.facing)]
    out = _outward(bus, s)
    laterals = list(range(partner, port_lateral + s, s))
    segments = [{"position": view.world(a + 2, lat), "direction": out} for lat in laterals]
    dived_offsets = {view.lanes[i].offset for i in dived}
    lane_offsets = {ln.offset for ln in bus.lanes}
    virtual = [
        entity("transport-belt", **view.world(a + 2, lat), direction=view.facing)
        for lat in laterals[1:]
        if lat in lane_offsets and lat not in dived_offsets
    ]
    route = {"ok": True, "segments": segments, "metrics": {"length": len(segments), "turns": 0}}
    branch = compile_crossings(route, virtual, maximum_span=span)
    if not branch.get("ok"):
        return {"ok": False, "reason": "tap_branch_cannot_tunnel_under_lanes", "detail": branch.get("reason")}
    entities += branch["entities"]
    pairs += branch["underground_pairs"]
    for index in dived:
        offset = view.lanes[index].offset
        first, last = view.world(a + 1, offset), view.world(a + 3, offset)
        entities += [
            dict(entity("underground-belt", first["x"], first["y"], view.facing), belt_to_ground_type="input"),
            dict(entity("underground-belt", last["x"], last["y"], view.facing), belt_to_ground_type="output"),
        ]
        pairs.append({"input": list(_tile(first)), "output": list(_tile(last)), "direction": view.facing, "span": 2})
    port = {
        "kind": "item",
        "item": item,
        "position": view.world(a + 2, port_lateral),
        "facing": out,
        "direction": "output",
    }
    # Same dict as in ``entities``: the spec carries the wanted native priorities.
    splitter.update(branch_priorities(splitter, port["position"]))
    return {
        "ok": True,
        "item": item,
        "lane": lane.index,
        "side": side,
        "along": a,
        "splitter": splitter,
        "entities": entities,
        "underground_pairs": pairs,
        "remove": remove,
        "dived_lanes": dived,
        "extension": extension,
        "port": port,
    }


FEED = "feed"  # tap record kind of a feed: plates enter the bus instead of leaving it


def feed_layout(view: BusView, item: str, side: str, along: int, *, span: int) -> dict:
    """One *feed* of ``item`` from ``side``: a tap mirrored about its splitter row ``s = along + 3``.

    A block that makes ``item`` away from the district (a remote smelting block) puts its
    plates onto the built trunk of ``item`` (``remote_smelting``). Rows, bus-local::

        row along      built trunks between the partner tile and the edge dive (input)
        row along+1    branch row: the port tile P outside the band, belts inward to the
                       partner tile, turning there into the bus direction; reserved and spare
                       lanes tunnelled under (as a tap's branch row)
        row along+2    belt on the partner tile, bus direction; the dived trunks surface here
        row along+3    splitter on the trunk lane and the partner tile

    The splitter's partner output (row ``along + 4``, partner tile) stays free; its output
    priority is the trunk side, input priority none (trunk and feed share the lane fairly).
    ``along`` is the window start, as for a tap, so tap and feed windows keep apart alike.
    The trunk must run past the splitter (no extension: a feed only joins a standing trunk).
    """
    bus = view.bus
    if item not in view.items:
        return {"ok": False, "reason": "feed_item_has_no_built_trunk", "item": item}
    if side not in ("left", "right"):
        return {"ok": False, "reason": "feed_side_unknown", "side": side}
    lane = view.lanes[view.items[item]]
    s = 1 if side == "right" else -1
    if along < FIRST_ROW:
        return {"ok": False, "reason": "feed_row_too_close_to_head"}
    a = along + WINDOW_ROWS - 1  # splitter row
    if a + 1 >= view.lengths[lane.index]:
        return {"ok": False, "reason": "feed_beyond_built_trunk", "along": along}
    edge = bus.half if s > 0 else -bus.half - 1
    port_lateral = edge + s
    partner = lane.offset + s
    built = view.built_offsets()
    if partner in built:
        return {"ok": False, "reason": "feed_partner_tile_is_a_built_trunk"}
    lo, hi = sorted((partner, edge))
    dived = []
    for off in sorted(built):
        index = built[off]
        if not lo < off < hi or off == lane.offset:
            continue
        if view.lengths[index] <= along:
            continue  # the trunk ends before the window: its tiles are tunnelled like a reserved lane
        if view.lengths[index] < a:
            return {"ok": False, "reason": "feed_rows_meet_trunk_end", "lane": index}
        dived.append(index)
    replaced = [(lane.index, a)] + [(index, row) for index in dived for row in range(along, a)]
    remove = []
    for index, row in replaced:
        spot = view.world(row, view.lanes[index].offset)
        here = view.tiles.get(_tile(spot))
        if here is None or here["name"] != "transport-belt" or here.get("direction") != view.facing:
            return {"ok": False, "reason": "feed_rows_meet_trunk_crossing", "lane": index, "along": row}
        remove.append(spot)
    for index in dived:
        offset = view.lanes[index].offset
        for row in range(along - span, a + span):
            here = view.tiles.get(_tile(view.world(row, offset)))
            if here is not None and here["name"] == "underground-belt":
                return {"ok": False, "reason": "feed_dive_near_trunk_underground", "lane": index, "along": row}
    for row in (along + 1, along + 2, a, a + 1):  # branch turn, partner belt, splitter, its free output
        if _tile(view.world(row, partner)) in view.tiles:
            return {"ok": False, "reason": "feed_partner_tile_taken", "along": row}
    centre = view.world(a, min(lane.offset, partner) + 0.5)
    splitter = dict(entity("splitter", centre["x"], centre["y"], view.facing), _width=2, _height=1)
    entities = [splitter, entity("transport-belt", **view.world(a - 1, partner), direction=view.facing)]
    inward = _outward(bus, -s)
    laterals = list(range(port_lateral, partner, -s))
    segments = [{"position": view.world(along + 1, lat), "direction": inward} for lat in laterals]
    segments.append({"position": view.world(along + 1, partner), "direction": view.facing})
    dived_offsets = {view.lanes[i].offset for i in dived}
    lane_offsets = {ln.offset for ln in bus.lanes}
    virtual = [
        entity("transport-belt", **view.world(along + 1, lat), direction=view.facing)
        for lat in laterals
        if lat in lane_offsets and lat not in dived_offsets
    ]
    route = {"ok": True, "segments": segments, "metrics": {"length": len(segments), "turns": 1}}
    branch = compile_crossings(route, virtual, maximum_span=span)
    if not branch.get("ok"):
        return {"ok": False, "reason": "feed_branch_cannot_tunnel_under_lanes", "detail": branch.get("reason")}
    entities += branch["entities"]
    pairs = list(branch["underground_pairs"])
    for index in dived:
        offset = view.lanes[index].offset
        first, last = view.world(along, offset), view.world(a - 1, offset)
        entities += [
            dict(entity("underground-belt", first["x"], first["y"], view.facing), belt_to_ground_type="input"),
            dict(entity("underground-belt", last["x"], last["y"], view.facing), belt_to_ground_type="output"),
        ]
        pairs.append({"input": list(_tile(first)), "output": list(_tile(last)), "direction": view.facing, "span": 2})
    port = {
        "kind": "item",
        "item": item,
        "position": view.world(along + 1, port_lateral),
        "facing": inward,
        "direction": "input",
    }
    trunk_side = splitter_side(splitter, view.world(a, lane.offset))
    splitter.update(splitter_input_priority="none", splitter_output_priority=trunk_side)
    return {
        "ok": True,
        "kind": FEED,
        "item": item,
        "lane": lane.index,
        "side": side,
        "along": along,
        "splitter_row": a,
        "splitter": splitter,
        "entities": entities,
        "underground_pairs": pairs,
        "remove": remove,
        "dived_lanes": dived,
        "extension": {},
        "port": port,
    }


def feed_candidates(view: BusView, item: str, side: str, *, span: int, limit: int = ATTEMPTS, accept=None):
    """Up to ``limit`` feasible feeds of ``item`` from ``side``, nearest the bus head first.

    Nearest the head first: a feed there reaches every tap of the lane (the remote block's
    plates serve the whole bus, not only the consumers downstream of it). Returns
    ``(layouts, rejected)`` like :func:`candidates`.
    """
    found, rejected = [], Counter()
    if item not in view.items:
        return found, {"feed_item_has_no_built_trunk": 1}
    for along in range(FIRST_ROW, view.lengths[view.items[item]]):
        if len(found) >= limit:
            break
        if not view.window_free(along):
            rejected["feed_window_taken"] += 1
            continue
        row = feed_layout(view, item, side, along, span=span)
        if not row["ok"]:
            rejected[row["reason"]] += 1
        elif accept is not None and not accept(row):
            rejected["feed_not_accepted"] += 1
        else:
            found.append(row)
    return found, dict(rejected)


def candidates(
    view: BusView,
    item: str,
    side: str,
    prefer: int,
    lo: int,
    hi: int,
    *,
    span: int,
    foreign=(),
    accept=None,
    limit=None,
):
    """Up to ``limit`` feasible tap layouts, port row nearest ``prefer`` first (rows ``lo..hi``).

    Returns ``(layouts, rejected)``; ``rejected`` counts the reasons of the
    rows that did not fit. ``accept(layout)`` may reject a fitting layout.
    """
    rows = sorted(range(max(FIRST_ROW, lo), hi + 1), key=lambda a: (abs(a + 2 - prefer), a))
    found, rejected = [], Counter()
    for a in rows:
        if limit is not None and len(found) >= limit:
            break
        if not view.window_free(a):
            rejected["tap_window_taken"] += 1
            continue
        row = layout(view, item, side, a, span=span, foreign=foreign)
        if not row["ok"]:
            rejected[row["reason"]] += 1
        elif accept is not None and not accept(row):
            rejected["tap_trunk_belt_not_observed"] += 1
        else:
            found.append(row)
    return found, dict(rejected)


# ----- consumer planning -----------------------------------------------------------
def connect_inputs(
    game, builder, obs, bus: dict, *, owner, allocation, inputs, entities, demand, span, route=plan_connection
) -> dict:
    """Taps and routes for every input port of a block beside the bus (no ``act``).

    ``allocation`` is the consumer's ``site.planzones.Allocation`` (side and
    along range); ``demand`` maps item -> declared plates/minute. Returns
    ``{"ok": True, "entities", "routes", "taps", "ignored_units", "sources"}``
    with ``entities`` = ``entities`` + taps + routes, or a failure naming the item.

    Routes are planned one after the other, so an earlier short route can wall
    off a later port (eval-1575138435: the 5-tile copper branch ran across the
    iron input's approach; all three iron tap rows then had no corridor). When
    an input fails, the whole set is planned again with that input first, at
    most once per input (``route_order`` on the result; a failure carries
    ``orders_tried``).
    """
    order = list(range(len(inputs)))
    tried = []
    while True:
        tried.append([inputs[i]["item"] for i in order])
        found = _connect_in_order(
            game,
            builder,
            obs,
            bus,
            owner=owner,
            allocation=allocation,
            inputs=[inputs[i] for i in order],
            entities=entities,
            demand=demand,
            span=span,
            route=route,
        )
        if found.get("ok"):
            return dict(found, route_order=tried[-1]) if len(tried) > 1 else found
        failed = next((i for i in order if inputs[i]["item"] == found.get("item")), None)
        if failed is None or failed == order[0] or len(tried) > len(inputs):
            return dict(found, orders_tried=tried) if len(tried) > 1 else found
        order = [failed] + [i for i in order if i != failed]
        if [inputs[i]["item"] for i in order] in tried:
            return dict(found, orders_tried=tried)


def _connect_in_order(game, builder, obs, bus: dict, *, owner, allocation, inputs, entities, demand, span, route):
    """``connect_inputs`` for one fixed input order."""
    view = BusView(bus)
    # Trunk extensions cross other blocks' planned belts too and may not meet their hardware.
    blocks = _board_blocks(game, obs)
    observed = foreign_belts(obs, blocks, exclude=(owner,))
    live = {(e["name"], *xy(e)): e for e in obs["entities"]}
    routes, taps, sources, ignored = [], [], {}, []

    def units_of(row):
        return [live.get(("transport-belt", *_tile(p)), {}).get("unit_number") for p in row["remove"]]

    seen_items = Counter()
    for port in inputs:
        item = port["item"]
        # Two input ports of one item (a belt row's plate and gear inputs) get two taps: each
        # needs its own name, the bus state keys taps by name (fixpace1 seed 3794581402: the
        # second tap replaced the first in the bus state, the bus rebuilt a trunk belt under
        # its splitter and blocked "reserved block placement is obstructed").
        seen_items[item] += 1
        name = tap_name(owner, item) + ("" if seen_items[item] == 1 else f"#{seen_items[item]}")
        done = next((s for s in view.stations if s.get("name") == name and s.get("built")), None)
        rejected = {}
        if done is not None:  # this consumer's tap is already built (restart after a lost state)
            attempts = [(done["port"], [], [], None)]
        else:
            prefer = view.along_of(port["position"])
            lo, hi = allocation.along0 - SEARCH_MARGIN, allocation.along1 + SEARCH_MARGIN
            found, rejected = candidates(
                view,
                item,
                allocation.side,
                prefer,
                lo,
                hi,
                span=span,
                foreign=observed,
                accept=lambda row: all(type(u) is int for u in units_of(row)),
                limit=ATTEMPTS,
            )
            attempts = []
            for row in found:
                units = units_of(row)
                # A port that states its own draw (two inputs of one item) declares that, not the item's total.
                rate = port.get("demand_per_minute", demand.get(item, 0))
                tap = dict(row, name=name, owner=owner, demand_per_minute=round(rate, 3), built=False)
                tap["remove_units"] = units
                attempts.append((row["port"], units, row["entities"], tap))
        failures = []
        for source, units, tap_entities, tap in attempts:
            clash = surface_conflict(builder, obs, blocks, tap_entities, exclude=(owner,)) if tap else None
            if clash is not None:
                failures.append({"reason": "tap_meets_planned_hardware", "detail": clash})
                continue
            try:
                trial = merge_entities(entities, tap_entities)
            except ValueError:
                failures.append({"reason": "tap_conflicts_with_planned_entities"})
                continue
            source = dict(source, name=name)
            found_route = route(game, builder, obs, source, port, trial, ignored_units=tuple(ignored + units))
            if not found_route.get("ok"):
                failures.append({"reason": found_route.get("reason"), "along": tap and tap["along"]})
                continue
            entities = merge_entities(trial, found_route["entities"])
            routes.append(found_route)
            sources[item] = source
            ignored += units
            if tap is not None:
                tap["port"] = source
                taps.append(tap)
                view.add(tap)
            break
        else:
            return {
                "ok": False,
                "reason": "planned_bus_tap_unavailable" if not attempts else "planned_bus_tap_route_failed",
                "item": item,
                "side": allocation.side,
                "rows_rejected": rejected,
                "failures": failures[-ATTEMPTS:],
            }
    return {
        "ok": True,
        "entities": entities,
        "routes": routes,
        "taps": taps,
        "ignored_units": ignored,
        "sources": sources,
    }


def _board_blocks(game, obs) -> dict:
    """This world's board blocks without bus-owned tap hardware ({} without a readable board)."""
    root = getattr(getattr(game, "cfg", None), "runtime_dir", None)
    if root is None:
        return {}
    try:
        board = _read(Path(root) / "pcb-board.json")
    except (OSError, ValueError):
        return {}
    if not isinstance(board, dict) or board.get("world_id") != obs.get("world_id"):
        return {}
    return blocks_without_bus(board)


def tap_pairs(taps) -> list[dict]:
    return [p for tap in taps for p in tap["underground_pairs"]]


# ----- publication and cut-over ---------------------------------------------------
def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def publish(root, world_id: str, taps) -> dict | None:
    """Record ``taps`` in the bus state and on the board (idempotent); a blocked status or None."""
    if not taps:
        return None
    from .planned_bus import publication

    root = Path(root)
    state = _read(root / STATE_FILE)
    board = _read(root / "pcb-board.json")
    if state.get("world_id") != world_id or board.get("world_id") != world_id:
        return {"status": "blocked", "reason": "planned_bus_tap_bus_world_changed"}
    known = {t["name"]: t for t in state.get("taps", [])}
    changed = False
    for tap in taps:
        old = known.get(tap["name"])
        if old is not None and (old.get("built") or old == tap):
            continue
        known[tap["name"]] = tap
        changed = True
        for trunk in state["trunks"]:
            target = tap["extension"].get(trunk["lane"], tap["extension"].get(str(trunk["lane"])))
            if target and target > trunk["length"]:
                bus = Bus.from_dict(state["bus"])
                lane = next(lane for lane in bus.lanes if lane.index == trunk["lane"])
                x, y = bus.local_point(target - 1 + 0.5, lane.offset + 0.5)
                trunk.update(length=target, tail={"x": x, "y": y})
    if changed:
        state["taps"] = list(known.values())
        _atomic_json(root / STATE_FILE, state)
    bus = publication(state)
    if board.get("shared_main_bus") != bus:
        board["shared_main_bus"] = bus
        _atomic_json(root / "pcb-board.json", board)
    return None


def released_tiles(root, world_id: str) -> dict:
    """Tile -> splitter of every built bus tap that replaced a trunk belt there (``remove``).

    A consumer's plan may hold trunk belts (the lane extension its own tap laid); a later tap
    of another consumer mines one of them by unit and builds its splitter there, and only the bus
    publication forgets the belt. A proof reading that consumer's plan treats the belt as the
    tap's (eval-1575138435: the steel tap at 82.0,-119.5 replaced a trunk belt of the green
    block's extension; the green proof failed for good and plastics research never started).
    """
    state = _read(Path(root) / STATE_FILE)
    if state.get("world_id") != world_id:
        return {}
    tiles = {}
    for tap in state.get("taps", []):
        splitter = tap.get("splitter")
        if not tap.get("built") or not isinstance(splitter, dict):
            continue
        for p in tap.get("remove") or ():
            tiles[_tile(p)] = {k: splitter[k] for k in ("name", "position", "direction")}
    return tiles


def released_belt_stands(actual: dict, released: dict, wanted: dict) -> bool:
    """``wanted`` is a plan transport belt a built tap replaced and that tap's splitter stands."""
    if wanted.get("name") != "transport-belt":
        return False
    splitter = released.get(_tile(wanted["position"]))
    if splitter is None:
        return False
    live = actual.get(("splitter", *_tile(splitter["position"])))
    return live is not None and live.get("direction", 0) == splitter.get("direction", 0)


def tap_plan(taps, *, clear_obstacles=(), world_id=None) -> dict:
    entities = merge_entities(*(tap["entities"] for tap in taps))
    return _plan(
        entities,
        [tap["port"] for tap in taps],
        underground_pairs=tap_pairs(taps),
        clear_obstacles=list(clear_obstacles),
        clearance_world_id=world_id,
        layout_policy="pcb-planned-bus-tap-v1",
    )


def _present(obs, taps) -> bool:
    live = {(e["name"], *xy(e)): e for e in obs["entities"]}
    for tap in taps:
        for e in tap["entities"]:
            found = live.get((e["name"], *xy(e)))
            if found is None or found.get("direction") != e.get("direction"):
                return False
    return True


def _mark_built(root, world_id, taps) -> None:
    root = Path(root)
    state = _read(root / STATE_FILE)
    if state.get("world_id") != world_id:
        return
    names = {t["name"] for t in taps}
    changed = False
    for tap in state.get("taps", []):
        if tap["name"] in names and not tap.get("built"):
            tap["built"] = True
            changed = True
    if changed:
        _atomic_json(root / STATE_FILE, state)
        publish(root, world_id, [t for t in state["taps"] if t["name"] in names])


def cutover(root, builder, obs, taps, *, clear_obstacles=()) -> dict | None:
    """Build the consumer's taps first: stock, mine the exact trunk belts, build; None once all stand.

    Every new tap item is in the inventory before the first trunk belt is
    mined, so the trunk is interrupted only for the mine and build actions.
    A built tap splitter then gets its native priorities (``configure``);
    None only once every tap stands and is configured.
    """
    pending = [t for t in taps if not t.get("built")]
    if not pending:
        return configure(builder, obs, taps)
    if _present(obs, pending):
        for tap in pending:
            tap["built"] = True
        _mark_built(root, obs["world_id"], pending)
        return configure(builder, obs, taps)
    live = {(e["name"], *xy(e)): e for e in obs["entities"]}
    missing = Counter(e["name"] for tap in pending for e in tap["entities"] if (e["name"], *xy(e)) not in live)
    inventory = obs.get("inventory", {})
    for name, count in sorted(missing.items()):
        if inventory.get(name, 0) < count:
            result = builder.bootstrap.ensure_item(obs, name, count)
            if result.get("type") or result.get("status") != "succeeded":
                return result
    # A tap may put its own belt on a removed trunk tile (the branch crossing the next
    # lane between two undergrounds): once placed it is tap hardware, not a changed trunk
    # belt (pace1 seed 2692229611: automation_science blocked planned_bus_tap_trunk_identity_changed).
    own = {(*xy(e), e.get("direction")) for tap in pending for e in tap["entities"] if e["name"] == "transport-belt"}
    for tap in pending:
        for position, unit in zip(tap["remove"], tap["remove_units"]):
            old = live.get(("transport-belt", *_tile(position)))
            if old is None or (old.get("unit_number") != unit and (*_tile(position), old.get("direction")) in own):
                continue
            if old.get("unit_number") != unit:
                return {"status": "blocked", "reason": "planned_bus_tap_trunk_identity_changed", "tap": tap["name"]}
            return {
                "type": "mine",
                "name": old["name"],
                "position": old["position"],
                "count": 1,
                "expected_entity_unit": unit,
                "expected_entity_world_id": obs["world_id"],
                "reason": "normally recover one owned trunk belt for a fully stocked planned bus tap",
            }
    result = builder.ensure_plan(obs, tap_plan(pending, clear_obstacles=clear_obstacles, world_id=obs["world_id"]))
    if result.get("status") != "succeeded":
        return result
    for tap in pending:
        tap["built"] = True
    _mark_built(root, obs["world_id"], pending)
    return configure(builder, obs, taps)


def configure(builder, obs, taps) -> dict | None:
    """The next ``configure_splitter`` for a built tap splitter whose priorities differ (idempotent), else None."""
    return configure_action(builder, obs, [(tap["splitter"], tap_priorities(tap)) for tap in taps])
