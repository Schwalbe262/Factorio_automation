"""Belt lane flow model: how many items per minute can reach each belt lane of a plan (pure).

A planned belt network (belts, undergrounds, splitters, inserters, drills as plan entities)
is turned into a lane graph and the flow from declared sources is pushed through it. Every
belt has two lanes, ``left`` and ``right`` (looking along the belt). The rules (docs/v2/
LAYOUT_PRINCIPLES.md "Belt lanes and bandwidth"):

* head-on (a belt, underground or splitter output feeding the back of a belt): lanes kept;
* a turn (a belt fed from exactly one side and not from behind is a curve): lanes kept;
* a side-load (a belt fed from a side while it has a back input, or from both sides): both
  lanes of the feeder go onto the target's near lane; straight traffic keeps priority, the
  side-load fills what is left of that lane;
* underground pairs keep lanes; the exit is found within the tier's ``max_distance``;
* a splitter keeps lanes; per lane it splits evenly between its two live outputs (an
  output with nothing that could take items backs up, so the other gets everything),
  ``splitter_output_priority`` fills the priority side first, ``splitter_filter`` sends
  that item to the priority side and the rest to the other; input priority only decides
  which input drains first, not capacity;
* an inserter picks from both lanes and drops on the far lane of a straight belt (the
  ``lanes.inserter_output_lane`` rule; dropping along the belt is taken as the right lane);
* a drill output (``drill`` sources) is placed on the near lane (prototype
  ``vector_to_place_result`` lands less than half a tile into the belt: electric 1.85 for a
  3x3 body, burner 1.3 for 2x2). UNVERIFIED in game; drill rates are far below a lane, so
  the choice does not change any capacity verdict.

Conservative where unsure: a side-load onto an underground belt (the lane-filter trick)
is not modelled - it carries nothing and is reported ``unmodelled_join``; a belt loop is
reported ``belt_cycle`` and its tiles get no flow from inside the loop.

The capacity is the guaranteed steady state when every consumer draws at full speed:
the even split of a splitter is what each branch gets while both are hungry (two
side-loads and a splitter therefore give each branch a quarter belt of that item). Output:
per-tile lane flows, per-port capacity and lane fill, and ``findings``:

* ``capacity_shortfall`` / ``single_lane_feed`` - a consumer's line delivers less than its
  downstream demand x margin (``single_lane_feed`` when the item stands on one lane and
  both lanes would have sufficed);
* ``side_load_before_splitter`` - a feeder carrying more than one lane of an item is
  side-loaded within ``SIDE_LOAD_REACH`` tiles before a splitter that splits it: half of
  the feeder's capacity is lost at the junction and each branch gets half a lane;
* ``lane_starvation`` - a consumer whose item reaches its pickup tile on neither lane;
* ``unbalanced_lanes`` - parallel belts of one multi-belt flow carry uneven totals;
* ``unmodelled_join`` / ``belt_cycle`` - see above.
A plain side-load whose lane covers the demand is never a finding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
LANES = ("left", "right")
# Items per minute of one lane (belt speed x 4 items per lane-tile x 60 x 60 / 4).
LANE_PER_MINUTE = {
    "transport-belt": 450.0,
    "fast-transport-belt": 900.0,
    "express-transport-belt": 1350.0,
    "turbo-transport-belt": 1800.0,
}
TIER_PREFIX = {
    "": "transport-belt",
    "fast-": "fast-transport-belt",
    "express-": "express-transport-belt",
    "turbo-": "turbo-transport-belt",
}
# Underground max_distance per tier (entrance to exit offset, 2.0 prototypes).
UNDERGROUND_REACH = {
    "transport-belt": 5,
    "fast-transport-belt": 7,
    "express-transport-belt": 9,
    "turbo-transport-belt": 11,
}
INSERTER_REACH = {"long-handed-inserter": 2}
MARGIN = 1.1  # delivered capacity must cover demand x MARGIN
SIDE_LOAD_REACH = 4  # belts between a side-load and the splitter it feeds that still count as "immediately before"
LANE_IMBALANCE = 0.25  # parallel belts: the emptiest may carry at most this share less than the fullest
EPS = 1e-6
MAX_UPSTREAM = 512


def tier(name: str) -> str | None:
    """Belt tier of a belt-like entity name, or None."""
    for suffix in ("transport-belt", "underground-belt", "splitter"):
        if name.endswith(suffix):
            prefix = name[: -len(suffix)]
            return TIER_PREFIX.get(prefix)
    return None


def kind_of(name: str) -> str | None:
    if tier(name) is None:
        return None
    if name.endswith("underground-belt"):
        return "underground"
    if name.endswith("splitter"):
        return "splitter"
    return "belt"


def tile_of(position) -> tuple[int, int]:
    if isinstance(position, dict):
        return math.floor(position["x"]), math.floor(position["y"])
    return math.floor(position[0]), math.floor(position[1])


def left_vector(direction: int) -> tuple[int, int]:
    return VECTORS[(direction - 4) % 16]


def _add(a: tuple, b: tuple, k: int = 1) -> tuple[int, int]:
    return a[0] + k * b[0], a[1] + k * b[1]


def _total(lane: dict) -> float:
    return sum(lane.values())


def _scaled(lane: dict, room: float) -> dict:
    """``lane`` limited to ``room`` items/min, every item scaled alike."""
    total = _total(lane)
    if total <= room + EPS or total <= 0:
        return dict(lane)
    k = max(room, 0.0) / total
    return {item: rate * k for item, rate in lane.items() if rate * k > EPS}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for item, rate in b.items():
        out[item] = out.get(item, 0.0) + rate
    return out


@dataclass
class Unit:
    tile: tuple[int, int]
    kind: str  # belt | underground | half (one splitter tile)
    direction: int
    cap: float  # items/min per lane
    name: str
    ug_type: str | None = None  # input | output
    splitter: int | None = None  # index into Network.splitters
    side: str | None = None  # splitter half: left | right


@dataclass
class Splitter:
    index: int
    direction: int
    tiles: dict  # side -> tile
    cap: float
    output_priority: str  # left | right | none
    filter: str | None
    name: str


@dataclass
class Edge:
    source: tuple  # node key
    target: tuple
    kind: str  # back | curve | side | splitter_in | underground | side_ug
    lane: str | None = None  # side-load target lane
    source_side: str | None = None  # splitter output half
    target_tile: tuple | None = None  # tile entered (a splitter half)


@dataclass
class Network:
    units: dict = field(default_factory=dict)  # tile -> Unit
    splitters: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def node(unit: Unit) -> tuple:
    """Graph node: a belt/underground tile, or a whole splitter."""
    return ("splitter", unit.splitter) if unit.kind == "half" else ("tile", unit.tile)


def build_network(entities) -> Network:
    """Lane graph of the belt-like entities of a plan."""
    net = Network()
    for e in entities:
        name = e.get("name", "")
        k = kind_of(name)
        if k is None:
            continue
        direction = int(e.get("direction", 0)) % 16
        if direction not in VECTORS:
            net.problems.append(
                {"kind": "unmodelled_join", "reason": "non_cardinal_belt", "tile": tile_of(e["position"])}
            )
            continue
        cap = LANE_PER_MINUTE[tier(name)]
        if k == "splitter":
            lv = left_vector(direction)
            cx, cy = e["position"]["x"], e["position"]["y"]
            left = tile_of((cx + 0.5 * lv[0], cy + 0.5 * lv[1]))
            right = tile_of((cx - 0.5 * lv[0], cy - 0.5 * lv[1]))
            index = len(net.splitters)
            priority = str(e.get("splitter_output_priority") or "none")
            net.splitters.append(
                Splitter(
                    index, direction, {"left": left, "right": right}, cap, priority, e.get("splitter_filter"), name
                )
            )
            for side, tile in (("left", left), ("right", right)):
                net.units[tile] = Unit(tile, "half", direction, cap, name, splitter=index, side=side)
        else:
            tile = tile_of(e["position"])
            ug = e.get("belt_to_ground_type") if k == "underground" else None
            net.units[tile] = Unit(tile, k, direction, cap, name, ug_type=ug)
    for unit in list(net.units.values()):
        out = _out_tile(net, unit)
        if out is None:
            continue
        target_tile, via = out
        target = net.units.get(target_tile)
        if target is None:
            continue
        edge = _join(unit, target, via)
        if edge is not None:
            edge.source_side, edge.target_tile = unit.side, target_tile
            net.edges.append(edge)
    # A belt fed from exactly one side and not from behind is a curve: lanes kept.
    incoming = {}
    for edge in net.edges:
        incoming.setdefault(edge.target, []).append(edge)
    for target, edges in incoming.items():
        if target[0] != "tile" or net.units[target[1]].kind != "belt":
            continue
        backs = [x for x in edges if x.kind == "back"]
        sides = [x for x in edges if x.kind == "side"]
        if not backs and len(sides) == 1:
            sides[0].kind, sides[0].lane = "curve", None
    for edge in net.edges:
        if edge.kind == "side_ug":
            net.problems.append(
                {"kind": "unmodelled_join", "reason": "side_load_onto_underground_belt", "tile": edge.target[1]}
            )
    return net


def _out_tile(net: Network, unit: Unit):
    v = VECTORS[unit.direction]
    if unit.kind == "underground" and unit.ug_type == "input":
        reach = UNDERGROUND_REACH[tier(unit.name)]
        for step in range(1, reach + 1):
            t = net.units.get(_add(unit.tile, v, step))
            if t is not None and t.kind == "underground" and t.name == unit.name and t.direction == unit.direction:
                if t.ug_type == "output":
                    return t.tile, "underground"
                return None  # another entrance of the same tier takes the pair: no exit found
        return None
    return _add(unit.tile, v), "surface"


def _join(source: Unit, target: Unit, via: str) -> Edge | None:
    s, t = node(source), node(target)
    if via == "underground":
        return Edge(s, t, "underground")
    d, td = source.direction, target.direction
    if target.kind == "half":
        return Edge(s, t, "splitter_in") if td == d else None
    if td == (d + 8) % 16:
        return None
    if target.kind == "underground":
        if td == d:
            return Edge(s, t, "back") if target.ug_type == "input" else None  # an exit cannot be entered from behind
        return Edge(s, t, "side_ug")
    if td == d:
        return Edge(s, t, "back")
    offset = (source.tile[0] - target.tile[0], source.tile[1] - target.tile[1])
    lane = "left" if offset == left_vector(td) else "right"
    return Edge(s, t, "side", lane)


# ----- sources, consumers -----------------------------------------------------------------
def full_belt(item: str, position, *, lanes=LANES, cap: float | None = None) -> dict:
    """A source feeding the belt at ``position`` from behind with ``item`` on ``lanes``, at lane capacity."""
    return {"tile": tile_of(position), "item": item, "lanes": tuple(lanes), "rate": cap, "mode": "back"}


def inserter_lanes(entity: dict) -> tuple[tuple[int, int], tuple[int, int], int]:
    """(pickup tile, drop tile, reach) of an inserter (its ``direction`` is the pickup side)."""
    reach = INSERTER_REACH.get(entity["name"], 1)
    v = VECTORS[int(entity.get("direction", 0)) % 16]
    p = (entity["position"]["x"], entity["position"]["y"])
    pickup = tile_of((p[0] + reach * v[0], p[1] + reach * v[1]))
    drop = tile_of((p[0] - reach * v[0], p[1] - reach * v[1]))
    return pickup, drop, reach


def drop_lane(belt_direction: int, inserter_side: tuple[int, int]) -> str:
    """The far lane of a straight belt from an inserter standing on ``inserter_side`` (vector belt -> inserter)."""
    lv = left_vector(belt_direction)
    if inserter_side == lv:
        return "right"
    if inserter_side == (-lv[0], -lv[1]):
        return "left"
    return "right"  # along the belt (lanes.inserter_output_lane)


# ----- flow ---------------------------------------------------------------------------------
@dataclass
class Flow:
    network: Network
    lanes: dict  # node -> {"left": {item: rate}, "right": {...}} (tiles; splitter halves keyed by tile too)
    offered: dict  # (edge index) -> offered and accepted per item for side-loads
    demand: dict  # tile -> {item: downstream demand}
    findings: list
    ports: list

    def at(self, tile) -> dict:
        return self.lanes.get(tuple(tile), {"left": {}, "right": {}})

    def item_rate(self, tile, item) -> float:
        lanes = self.at(tile)
        return sum(lanes[lane].get(item, 0.0) for lane in LANES)


def solve(
    entities,
    sources=(),
    consumers=(),
    *,
    sinks=(),
    ports=(),
    groups=(),
    margin: float = MARGIN,
) -> Flow:
    """Push ``sources`` through the plan's belts and check ``consumers``.

    ``sources``: ``full_belt`` rows, or ``{"tile", "item", "lane", "rate", "mode": "drop"}`` for
    an inserter or drill dropping on one lane. ``consumers``: ``{"tile", "demand": {item: per
    min}}`` (an inserter pickup, a branch end). ``sinks``: tiles where items leave the plan (an
    output port, a branch continuing off-plan); a line that reaches neither a consumer nor a
    sink backs up. ``ports``: ``{"name", "tile"}`` rows reported with their lane flows.
    ``groups``: ``{"name", "tiles": [one tile per parallel belt]}`` for ``unbalanced_lanes``.
    """
    net = build_network(entities)
    findings = list(net.problems)
    out_edges, in_edges = {}, {}
    for i, edge in enumerate(net.edges):
        out_edges.setdefault(edge.source, []).append(i)
        in_edges.setdefault(edge.target, []).append(i)
    nodes = {node(u) for u in net.units.values()}
    consumer_at = {}
    for c in consumers:
        tile = tile_of(c["tile"])
        row = consumer_at.setdefault(tile, {})
        for item, rate in (c.get("demand") or {}).items():
            row[item] = row.get(item, 0.0) + float(rate)
    sink_tiles = {tile_of(t) for t in sinks}
    live = _live(net, nodes, out_edges, in_edges, set(consumer_at) | sink_tiles)
    unbounded = _live(net, nodes, out_edges, in_edges, sink_tiles - set(consumer_at))
    order, cyclic = _order(nodes, net, out_edges, in_edges)
    demand = _downstream_demand(net, order, cyclic, out_edges, consumer_at)

    def draw(n, item):
        return math.inf if n in unbounded else demand.get(n, {}).get(item, 0.0) * margin

    if cyclic:
        findings.append({"kind": "belt_cycle", "tiles": sorted(t[1] for t in cyclic if t[0] == "tile")[:8]})
    by_tile_sources = {}
    for s in sources:
        by_tile_sources.setdefault(tile_of(s["tile"]), []).append(s)
    lanes, outflow, offered = {}, {}, {}
    for n in order + [c for c in cyclic]:
        if n[0] == "splitter":
            sp = net.splitters[n[1]]
            per_half = {}
            for side, tile in sp.tiles.items():
                got = {"left": {}, "right": {}}
                for i in in_edges.get(n, []):
                    edge = net.edges[i]
                    if _edge_target_tile(net, edge) != tile:
                        continue
                    flow = outflow.get(i)
                    if flow:
                        for lane in LANES:
                            got[lane] = _merge(got[lane], flow[lane])
                for s in by_tile_sources.get(tile, []):
                    _inject(got, s, sp.cap)
                per_half[side] = {lane: _scaled(got[lane], sp.cap) for lane in LANES}
                lanes[tile] = per_half[side]
            outs = _split(net, sp, per_half, out_edges.get(n, []), live, draw)
            for i, flow in outs.items():
                outflow[i] = flow
            continue
        unit = net.units[n[1]]
        got = {"left": {}, "right": {}}
        for i in in_edges.get(n, []):
            edge = net.edges[i]
            if edge.kind in ("back", "curve", "underground", "splitter_in"):
                flow = outflow.get(i) or {"left": {}, "right": {}}
                for lane in LANES:
                    got[lane] = _merge(got[lane], flow[lane])
        for s in by_tile_sources.get(n[1], []):
            if s.get("mode", "back") == "back":
                _inject(got, s, unit.cap)
        got = {lane: _scaled(got[lane], unit.cap) for lane in LANES}
        for i in in_edges.get(n, []):
            edge = net.edges[i]
            if edge.kind != "side":
                continue
            flow = outflow.get(i) or {"left": {}, "right": {}}
            offer = _merge(flow["left"], flow["right"])
            room = unit.cap - _total(got[edge.lane])
            accepted = _scaled(offer, room)
            got[edge.lane] = _merge(got[edge.lane], accepted)
            offered[i] = {"offered": offer, "accepted": accepted}
        for s in by_tile_sources.get(n[1], []):
            if s.get("mode") == "drop":
                lane = s.get("lane", "right")
                room = unit.cap - _total(got[lane])
                got[lane] = _merge(got[lane], _scaled({s["item"]: float(s["rate"])}, room))
        lanes[n[1]] = got
        for i in out_edges.get(n, []):
            outflow[i] = got
    flow = Flow(net, lanes, offered, demand, findings, [])
    findings += _consumer_findings(flow, consumer_at, in_edges, margin)
    findings += _side_load_before_splitter(flow, in_edges, out_edges, consumer_at, live)
    findings += _group_findings(flow, groups)
    flow.ports = [port_capacity(flow, p) for p in ports]
    return flow


def port_capacity(flow: Flow, port: dict) -> dict:
    tile = tile_of(port["tile"])
    lanes = flow.at(tile)
    unit = flow.network.units.get(tile)
    cap = unit.cap if unit else LANE_PER_MINUTE["transport-belt"]
    per_lane = {lane: round(_total(lanes[lane]), 3) for lane in LANES}
    return {
        "name": port.get("name"),
        "item": port.get("item"),
        "tile": list(tile),
        "per_minute": round(sum(per_lane.values()), 3),
        "lanes": per_lane,
        "fill": {lane: round(per_lane[lane] / cap, 3) for lane in LANES},
        "items": {lane: sorted(lanes[lane]) for lane in LANES},
    }


def _edge_target_tile(net: Network, edge: Edge):
    """The tile an edge enters (for a splitter: which half)."""
    return edge.target_tile


def _inject(got: dict, source: dict, cap: float) -> None:
    rate = cap if source.get("rate") is None else float(source["rate"])
    lanes = tuple(source.get("lanes") or LANES)
    for lane in lanes:
        got[lane] = _merge(got[lane], {source["item"]: rate / len(lanes) if source.get("rate") is not None else rate})


def _split(net: Network, sp: Splitter, per_half: dict, edge_ids: list, live: set, draw=None) -> dict:
    """Output flow per outgoing edge of a splitter (lanes kept).

    ``draw(node, item)``: the most a branch takes of ``item`` (its declared downstream demand x
    margin; ``inf`` when an undeclared consumer or an exit is downstream). A branch that takes
    less than its share backs up, so the rest goes to the other output (that is what the
    game does); a priority output fills first; a filter sends its item to the priority side.
    """
    draw = draw or (lambda _n, _i: math.inf)
    out_side = {}
    for i in edge_ids:
        out_side.setdefault(net.edges[i].source_side, i)
    open_sides = [s for s in ("left", "right") if s in out_side and net.edges[out_side[s]].target in live]
    if not open_sides:
        open_sides = [s for s in ("left", "right") if s in out_side]
    result = {i: {"left": {}, "right": {}} for i in edge_ids}
    if not open_sides:
        return result
    cap = sp.cap
    lanes_in = {lane: _merge(per_half["left"][lane], per_half["right"][lane]) for lane in LANES}
    items = set(lanes_in["left"]) | set(lanes_in["right"])
    fraction = {side: {} for side in open_sides}
    for item in items:
        total = lanes_in["left"].get(item, 0.0) + lanes_in["right"].get(item, 0.0)
        if total <= EPS:
            continue
        if len(open_sides) == 1:
            fraction[open_sides[0]][item] = 1.0
            continue
        limit = {side: draw(net.edges[out_side[side]].target, item) for side in open_sides}
        if sp.filter:
            first = sp.output_priority if sp.output_priority in open_sides else "left"
            side = first if item == sp.filter else ("right" if first == "left" else "left")
            fraction[side][item] = 1.0  # filtered items wait for their side (unverified: no overflow modelled)
            continue
        if sp.output_priority in open_sides:
            first = sp.output_priority
            other = "right" if first == "left" else "left"
            got = min(total, limit[first], 2 * cap)
            alloc = {first: got, other: min(total - got, limit[other])}
        else:
            a, b = open_sides
            share = total / 2
            if limit[a] < share:
                alloc = {a: limit[a], b: min(limit[b], total - limit[a])}
            elif limit[b] < share:
                alloc = {b: limit[b], a: min(limit[a], total - limit[b])}
            else:
                alloc = {a: share, b: share}
        for side in open_sides:
            fraction[side][item] = alloc[side] / total
    for side in open_sides:
        for lane in LANES:
            shares = {item: rate * fraction[side].get(item, 0.0) for item, rate in lanes_in[lane].items()}
            result[out_side[side]][lane] = _scaled({k: v for k, v in shares.items() if v > EPS}, cap)
    return result


def _live(net, nodes, out_edges, in_edges, sink_tiles) -> set:
    """Nodes from which some consumer or sink can be reached (anything else backs up)."""
    seeds = set()
    for n in nodes:
        if n[0] == "tile" and n[1] in sink_tiles:
            seeds.add(n)
        if n[0] == "splitter" and any(t in sink_tiles for t in net.splitters[n[1]].tiles.values()):
            seeds.add(n)
    live, stack = set(seeds), list(seeds)
    while stack:
        n = stack.pop()
        for i in in_edges.get(n, []):
            s = net.edges[i].source
            if s not in live:
                live.add(s)
                stack.append(s)
    return live


def _order(nodes, net, out_edges, in_edges):
    indegree = {n: 0 for n in nodes}
    for edge in net.edges:
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
    ready = sorted((n for n, d in indegree.items() if d == 0), key=repr)
    order = []
    while ready:
        n = ready.pop()
        order.append(n)
        for i in out_edges.get(n, []):
            t = net.edges[i].target
            indegree[t] -= 1
            if indegree[t] == 0:
                ready.append(t)
    done = set(order)
    return order, [n for n in sorted(nodes, key=repr) if n not in done]


def _downstream_demand(net, order, cyclic, out_edges, consumer_at) -> dict:
    """Per node: demand of its own consumers plus everything downstream (items/min)."""
    demand = {}
    for n in reversed(order + cyclic):
        own = {}
        if n[0] == "tile":
            own = dict(consumer_at.get(n[1], {}))
        else:
            for tile in net.splitters[n[1]].tiles.values():
                own = _merge(own, consumer_at.get(tile, {}))
        for i in out_edges.get(n, []):
            own = _merge(own, demand.get(net.edges[i].target, {}))
        demand[n] = own
    return demand


def _node_of_tile(net, tile):
    unit = net.units.get(tile)
    return node(unit) if unit else None


def _consumer_findings(flow: Flow, consumer_at: dict, in_edges: dict, margin: float) -> list:
    net = flow.network
    rows = []
    for tile, own in sorted(consumer_at.items()):
        n = _node_of_tile(net, tile)
        if n is None:
            continue
        lanes = flow.at(tile)
        down = flow.demand.get(n, {})
        for item in sorted(own):
            need = down.get(item, 0.0)
            have = flow.item_rate(tile, item)
            if need <= EPS:
                continue
            if have <= EPS:
                rows.append(
                    {
                        "kind": "lane_starvation",
                        "tile": list(tile),
                        "item": item,
                        "demand_per_minute": round(need, 3),
                        "items_on_belt": sorted(set(lanes["left"]) | set(lanes["right"])),
                    }
                )
                continue
            if have + EPS >= need * margin:
                continue
            used = [lane for lane in LANES if lanes[lane].get(item, 0.0) > EPS]
            cap = net.units[tile].cap if tile in net.units else LANE_PER_MINUTE["transport-belt"]
            single = len(used) == 1 and need * margin <= 2 * cap + EPS
            rows.append(
                {
                    "kind": "single_lane_feed" if single else "capacity_shortfall",
                    "tile": list(tile),
                    "item": item,
                    "delivered_per_minute": round(have, 3),
                    "demand_per_minute": round(need, 3),
                    "margin": margin,
                    "lanes": used,
                    "trace": trace(flow, tile, item, in_edges),
                }
            )
    return _most_upstream(flow, rows, in_edges)


def _most_upstream(flow: Flow, rows: list, in_edges: dict) -> list:
    """One shortfall per line: drop a row when a row of the same item stands upstream on its line."""
    marked = {(tuple(r["tile"]), r["item"]) for r in rows if r["kind"] != "lane_starvation"}
    kept = []
    for r in rows:
        if r["kind"] == "lane_starvation":
            kept.append(r)
            continue
        tile, seen = tuple(r["tile"]), set()
        upstream = False
        for _ in range(MAX_UPSTREAM):
            prev = _straight_feeder(flow.network, tile, in_edges)
            if prev is None or prev in seen:
                break
            seen.add(prev)
            if (prev, r["item"]) in marked:
                upstream = True
                break
            tile = prev
        if not upstream:
            kept.append(r)
    return kept


def _straight_feeder(net: Network, tile, in_edges):
    """The tile feeding ``tile`` head-on or through a curve/underground, or None."""
    for i in in_edges.get(("tile", tile), []):
        edge = net.edges[i]
        if edge.kind in ("back", "curve", "underground") and edge.source[0] == "tile":
            return edge.source[1]
    return None


def trace(flow: Flow, tile, item, in_edges, limit: int = 64) -> list:
    """Upstream steps that cut ``item``'s capacity before ``tile``: side-loads and splitters, nearest first."""
    net = flow.network
    steps, n, seen = [], ("tile", tuple(tile)), set()
    for _ in range(limit):
        if n in seen:
            break
        seen.add(n)
        feeders = [net.edges[i] for i in in_edges.get(n, [])]
        carrying = [e for e in feeders if _carries(flow, e.source, item)]
        if not carrying:
            break
        edge = next((e for e in carrying if e.kind in ("back", "curve", "underground", "splitter_in")), carrying[0])
        if edge.kind == "side":
            steps.append({"step": "side_load", "tile": list(n[1]), "lane": edge.lane})
        if edge.source[0] == "splitter":
            sp = net.splitters[edge.source[1]]
            steps.append({"step": "splitter", "tile": list(sp.tiles["left"]), "priority": sp.output_priority})
            halves = [
                e for e in (net.edges[i] for i in in_edges.get(edge.source, [])) if _carries(flow, e.source, item)
            ]
            if not halves:
                break
            n = halves[0].source
            continue
        n = edge.source
    return steps


def _carries(flow: Flow, n, item) -> bool:
    if n[0] == "tile":
        return flow.item_rate(n[1], item) > EPS
    sp = flow.network.splitters[n[1]]
    return any(flow.item_rate(t, item) > EPS for t in sp.tiles.values())


def _side_load_before_splitter(flow: Flow, in_edges, out_edges, consumer_at, live) -> list:
    net = flow.network
    rows = []
    for sp in net.splitters:
        n = ("splitter", sp.index)
        open_outputs = [i for i in out_edges.get(n, []) if net.edges[i].target in live]
        if len(open_outputs) < 2:
            continue
        for i in in_edges.get(n, []):
            start = net.edges[i].source
            if start[0] != "tile":
                continue
            tile, steps = start[1], 0
            while tile is not None and steps < SIDE_LOAD_REACH:
                if tile in consumer_at:
                    break
                for j in in_edges.get(("tile", tile), []):
                    side = net.edges[j]
                    record = flow.offered.get(j)
                    if side.kind != "side" or record is None:
                        continue
                    cap = net.units[tile].cap
                    for item, offer in record["offered"].items():
                        if offer <= cap + EPS:
                            continue  # the feeder carries no more than one lane: nothing lost
                        accepted = record["accepted"].get(item, 0.0)
                        rows.append(
                            {
                                "kind": "side_load_before_splitter",
                                "item": item,
                                "side_load_tile": list(tile),
                                "splitter_tile": list(sp.tiles["left"]),
                                "feeder_per_minute": round(offer, 3),
                                "after_side_load_per_minute": round(accepted, 3),
                                "per_branch_per_minute": round(accepted / 2, 3),
                                "severity": "waste",
                            }
                        )
                tile = _straight_feeder(net, tile, in_edges)
                steps += 1
    return rows


def _group_findings(flow: Flow, groups) -> list:
    rows = []
    for g in groups:
        totals = [round(sum(_total(flow.at(tile_of(t))[lane]) for lane in LANES), 3) for t in g["tiles"]]
        if len(totals) < 2 or max(totals) <= EPS:
            continue
        if min(totals) < (1 - LANE_IMBALANCE) * max(totals):
            rows.append({"kind": "unbalanced_lanes", "group": g.get("name"), "belt_per_minute": totals})
    return rows


def summary(flow: Flow) -> dict:
    """Compact record: findings and port capacities (items/min, lane fill)."""
    counts = {}
    for f in flow.findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {"findings": flow.findings, "counts": counts, "ports": flow.ports}
