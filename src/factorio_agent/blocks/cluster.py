"""Recipe tree -> compact machine cluster (``docs/v2/LAYOUT_PRINCIPLES.md``), pure Python.

The generator decides, from catalog numbers only:

1. the tree: every solid ingredient of the product is either *sourced* (a bus
   tap or a board port delivers it) or made by an assembler in the block
   (``tree``); one machine per recipe serves every consumer of its item;
2. per producer -> consumer edge, direct insertion or a belt
   (``decide_edge``, rule 2): direct insertion only when ONE producer machine
   feeds at most ``DIRECT_MAX_CONSUMERS`` consumer machines and the flow fits
   the inserters of one shared machine edge; otherwise the item must travel on
   a belt (a row lane or a bus lane) and the cluster is rejected with
   ``needs_belt``, so the caller uses a row;
3. the embedding (``embed``): machines on a square grid with a one-tile gap
   (pitch = machine size + 1); every direct edge joins two edge-adjacent cells
   (the inserter sits in the gap); every machine taking sourced items touches
   the input belt, which runs along the top edge and may continue down the
   right edge (one turn, never around a machine); the smallest bounding box
   wins;
4. the ports: the sourced items enter on one belt (two items: one per lane,
   side-loaded from two feeders at the belt head); the product leaves through
   one inserter on a free side the caller chooses (``output_side``:
   ``left`` = the input edge, ``bottom``, ``right``), into a stock chest or
   onto a one-tile belt port. Every variant reserves the same area, so the
   placement can pick the side from where the consumer is.

Canonical frame (like ``blocks.row``): u east, v south, ``origin`` = the
top-left gap corner (a tile centre). Machine (c, r) centre = origin +
(P*c + half + 1, P*r + half + 1); gap rows v = P*r, gap columns u = P*c;
the input belt runs at v = -1 and, turned, at u = P*C + 1. Poles stand on
gap corners (P*i, P*j), which no inserter uses.

Inserter convention (repo-wide): direction d picks up at
``position + reach * VECTORS[d]`` and drops at ``position - reach * VECTORS[d]``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..board import entity
from ..core.deterministic_builder import _plan
from ..lanes import inserter_output_lane
from ..routing import VECTORS
from .geometry import bounding_box, footprint, require_tile_centre, tile_size, xy
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, lane_capacity, pole_radius, pole_reach

TEMPLATE = "direct_insertion_cluster_v1"
MAX_NODES = 8  # machines in one cluster; a bigger tree is a row/bus design
MAX_ROWS = 2
MAX_DEPTH = 4
DIRECT_MAX_CONSUMERS = 2  # consumer machines one producer can reach by adjacency in a row-shaped cluster
MAX_ARMS_PER_EDGE = 3  # inserters on one shared 3-tile machine edge
OUTPUT_SIDES = ("bottom", "left", "right")
SIDE_STEP = {"top": (0, -1), "right": (1, 0), "bottom": (0, 1), "left": (-1, 0)}


@dataclass
class Node:
    recipe: str
    product: str
    product_amount: float
    nameplate: float  # crafts/minute of one machine
    crafts: float = 0.0  # crafts/minute the cluster needs
    sourced: dict = field(default_factory=dict)  # item -> amount per craft
    made: dict = field(default_factory=dict)  # item -> producer recipe
    amounts: dict = field(default_factory=dict)  # every solid ingredient -> amount per craft


def _solid(rows) -> list:
    return [r for r in rows if r.get("type", "item") == "item"]


def _machine_ok(catalog: Any, name: str, recipe: dict) -> bool:
    row = catalog.entities.get(name) or {}
    return bool(
        row.get("type") == "assembling-machine"
        and row.get("electric")
        and set(recipe.get("categories") or ()) & set(row.get("crafting_categories") or ())
    )


def tree(catalog: Any, product: str, sourced: Callable[[str], bool], machine: str) -> tuple[str, dict[str, Node]]:
    """``(root recipe, recipe -> Node)``; ValueError names the item that cannot be made or sourced."""
    nodes: dict[str, Node] = {}
    speed = float((catalog.entities.get(machine) or {}).get("crafting_speed") or 0)
    if speed <= 0:
        raise ValueError(f"{machine}: no crafting speed")

    def visit(item: str, depth: int) -> str:
        recipe = catalog.recipe_for_product(item)
        if not isinstance(recipe, dict):
            raise ValueError(f"no source and no recipe for {item}")
        name = recipe["name"]
        if name in nodes:
            return name
        if depth > MAX_DEPTH:
            raise ValueError(f"{item}: recipe tree deeper than {MAX_DEPTH}")
        rows = list(recipe["ingredients"]) + list(recipe["products"])
        if any(r.get("type") == "fluid" for r in rows):
            raise ValueError(f"{name}: fluid recipes are not laid out by a cluster")
        if len(recipe["products"]) != 1 or not _machine_ok(catalog, machine, recipe):
            raise ValueError(f"{name}: not a single-product {machine} recipe")
        node = Node(name, item, float(recipe["products"][0]["amount"]), 60 * speed / float(recipe["energy"]))
        nodes[name] = node
        for row in _solid(recipe["ingredients"]):
            ingredient, amount = row["name"], float(row["amount"])
            node.amounts[ingredient] = node.amounts.get(ingredient, 0) + amount
            if sourced(ingredient):
                node.sourced[ingredient] = node.sourced.get(ingredient, 0) + amount
            else:
                node.made[ingredient] = visit(ingredient, depth + 1)
        return name

    root = visit(product, 0)
    if len(nodes) > MAX_NODES:
        raise ValueError(f"{product}: {len(nodes)} recipes exceed one cluster ({MAX_NODES})")
    return root, nodes


def _topological(root: str, nodes: dict[str, Node]) -> list[str]:
    order, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for child in nodes[name].made.values():
            visit(child)
        order.append(name)

    visit(root)
    return order[::-1]  # consumers before producers


def propagate(root: str, nodes: dict[str, Node], root_crafts: float) -> None:
    """Crafts/minute every node must make so the root makes ``root_crafts``."""
    for node in nodes.values():
        node.crafts = 0.0
    nodes[root].crafts = root_crafts
    for name in _topological(root, nodes):
        node = nodes[name]
        for item, child in node.made.items():
            nodes[child].crafts += node.crafts * node.amounts[item] / nodes[child].product_amount


def decide_edge(flow: float, producers: int, consumers: int, arm_rate: float) -> tuple[str, str]:
    """``("direct" | "belt", reason)`` for one producer -> consumer item edge (rule 2).

    ``flow`` items/minute, machine counts on both ends, ``arm_rate`` items/minute
    of one inserter. Direct insertion needs one side of the pair to be a single
    machine next to at most ``DIRECT_MAX_CONSUMERS`` machines of the other, and
    the flow to fit the arms of one shared edge.
    """
    if min(producers, consumers) != 1 or max(producers, consumers) > DIRECT_MAX_CONSUMERS:
        return "belt", f"ratio {producers}:{consumers} fans out beyond adjacency"
    per_pair = flow / max(producers, consumers)
    if per_pair > MAX_ARMS_PER_EDGE * arm_rate + 1e-9:
        return "belt", f"{per_pair:g}/min exceeds {MAX_ARMS_PER_EDGE} inserters of {arm_rate:g}/min"
    return "direct", f"ratio {producers}:{consumers}, {per_pair:g}/min within one machine edge"


def _arms(flow: float, rate: float, what: str) -> int:
    count = max(1, math.ceil(flow / rate - 1e-9))
    if count > MAX_ARMS_PER_EDGE:
        raise ValueError(f"{what}: {flow:g}/min needs {count} inserters on one 3-tile edge")
    return count


# ----- embedding -------------------------------------------------------------------
@dataclass(frozen=True)
class Embedding:
    columns: int
    rows: int
    turned: bool  # the input belt continues down the right edge
    cells: tuple  # ((recipe, (c, r)), ...)

    def cell(self, recipe: str) -> tuple[int, int]:
        return dict(self.cells)[recipe]


def _belt_sides(c: int, r: int, columns: int, turned: bool) -> list[str]:
    sides = ["top"] if r == 0 else []
    if turned and c == columns - 1:
        sides.append("right")
    return sides


def _output_sides(c: int, r: int, columns: int, rows: int, turned: bool, last_belt_row: int) -> list[str]:
    sides = []
    if r == rows - 1:
        sides.append("bottom")
    if c == 0:
        sides.append("left")
    if c == columns - 1 and not (turned and r <= last_belt_row):
        sides.append("right")
    return sides


def embed(root: str, nodes: dict[str, Node], output_side: str | None = None) -> Embedding:
    """Smallest grid placement satisfying adjacency, belt access and the output side (module doc)."""
    edges = {(child, name) for name, node in nodes.items() for child in node.made.values()}
    neighbours = {name: set() for name in nodes}
    for a, b in edges:
        neighbours[a].add(b)
        neighbours[b].add(a)
    order, queue = [], deque([root])
    while queue:
        name = queue.popleft()
        if name in order:
            continue
        order.append(name)
        queue.extend(sorted(neighbours[name] - set(order)))
    fed = {name for name, node in nodes.items() if node.sourced}
    n = len(nodes)
    options = []
    for rows in range(1, MAX_ROWS + 1):
        for columns in range(math.ceil(n / rows), n + 1):
            for turned in (False, True):
                if turned and rows == 1:
                    continue
                for found in _search(order, neighbours, fed, root, columns, rows, turned, output_side):
                    options.append(found)
                    break
    if not options:
        raise ValueError(f"{root}: no adjacency layout within {MAX_ROWS} rows (needs belts between machines)")

    def score(e: Embedding):
        width = 4 * e.columns + 4 + (2 if e.turned else 0)
        height = 4 * e.rows + 4
        return (width * height, e.turned, e.rows, e.columns)

    return min(options, key=score)


def _search(order, neighbours, fed, root, columns, rows, turned, output_side):
    cells = [(c, r) for r in range(rows) for c in range(columns)]
    placed: dict[str, tuple[int, int]] = {}
    used: set = set()

    def ok(name, cell):
        c, r = cell
        for other in neighbours[name]:
            if other in placed:
                oc, orow = placed[other]
                if abs(oc - c) + abs(orow - r) != 1:
                    return False
        if name in fed and not _belt_sides(c, r, columns, turned):
            return False
        return True

    def finish():
        if turned:
            last = max((placed[n][1] for n in fed if placed[n][0] == columns - 1), default=-1)
            if last < 1:
                return False  # a turned belt must serve a lower row
        else:
            last = -1
        if not all(r == 0 or (turned and c == columns - 1) for n in fed for c, r in [placed[n]]):
            return False
        if max(c for c, _ in placed.values()) != columns - 1 or max(r for _, r in placed.values()) != rows - 1:
            return False
        sides = _output_sides(*placed[root], columns, rows, turned, last)
        return bool(sides) and (output_side is None or output_side in sides)

    def go(index):
        if index == len(order):
            if finish():
                yield Embedding(columns, rows, turned, tuple(sorted(placed.items())))
            return
        name = order[index]
        for cell in cells:
            if cell in used or not ok(name, cell):
                continue
            placed[name] = cell
            used.add(cell)
            yield from go(index + 1)
            del placed[name]
            used.discard(cell)

    yield from go(0)


# ----- geometry --------------------------------------------------------------------
def _side_tiles(c: int, r: int, side: str, pitch: int, size: int) -> list[tuple[int, int]]:
    """Gap tiles along one machine side, middle first."""
    ks = sorted(range(size), key=lambda k: (abs(k - size // 2), k))
    if side == "top":
        return [(pitch * c + 1 + k, pitch * r) for k in ks]
    if side == "bottom":
        return [(pitch * c + 1 + k, pitch * (r + 1)) for k in ks]
    if side == "left":
        return [(pitch * c, pitch * r + 1 + k) for k in ks]
    return [(pitch * (c + 1), pitch * r + 1 + k) for k in ks]


def _towards(side: str) -> int:
    """Direction from a gap tile on ``side`` of a machine back towards the machine."""
    dx, dy = SIDE_STEP[side]
    return next(d for d, v in VECTORS.items() if v == (-dx, -dy))


def _shared_side(a: tuple[int, int], b: tuple[int, int]) -> str:
    """Side of cell ``a`` that faces cell ``b``."""
    return next(s for s, v in SIDE_STEP.items() if (a[0] + v[0], a[1] + v[1]) == b)


def cluster_block(
    catalog: Any,
    product: str,
    rate: float,
    *,
    sourced: Callable[[str], bool],
    machine: str = "assembling-machine-1",
    inserter: str = "inserter",
    belt: str = "transport-belt",
    pole: str = "small-electric-pole",
    chest: str | None = None,
    output_side: str | None = None,
    plate_order: Iterable[str] | None = None,
    origin: tuple[float, float] = (0.5, 0.5),
    limits: RowLimits = DEFAULT_LIMITS,
) -> dict:
    """One cluster plan (module doc); ValueError names why the tree needs a row or a belt.

    ``rate`` items/minute of ``product`` is capped at one product machine
    (start small, rule 3); ``chest`` None ends in a one-tile belt port.
    ``plate_order`` orders the two sourced items: the first gets the north
    feeder (left lane), so the caller can follow its bus lane order (rule 8).
    """
    ox, oy = require_tile_centre(origin)
    size, height = tile_size(catalog, machine)
    if size != height or size % 2 == 0 or size < 3:
        raise ValueError(f"{machine}: odd square machines only")
    pitch = size + 1
    root, nodes = tree(catalog, product, sourced, machine)
    head = nodes[root]
    crafts = min(rate / head.product_amount, head.nameplate)
    propagate(root, nodes, crafts)
    arm = inserter_rate(catalog, inserter, limits)
    decisions = []
    for name, node in nodes.items():
        if node.crafts > node.nameplate + 1e-9:
            raise ValueError(
                f"needs_more_machines: {name} needs {node.crafts:g} crafts/min, one machine makes {node.nameplate:g}"
            )
        for item, child in node.made.items():
            flow = node.crafts * node.amounts[item]
            consumers = sum(1 for other in nodes.values() if item in other.made)
            kind, why = decide_edge(flow, 1, consumers, arm)
            decisions.append({"item": item, "from": child, "to": name, "per_minute": flow, "kind": kind, "why": why})
            if kind != "direct":
                raise ValueError(f"needs_belt: {item} {why}")
    plates = sorted({item for node in nodes.values() for item in node.sourced})
    if len(plates) > 2:
        raise ValueError(f"{product}: {len(plates)} sourced items need two input belts; use a row")
    if plate_order is not None:
        wanted = [item for item in plate_order if item in plates]
        plates = wanted + [item for item in plates if item not in wanted]
    layout = embed(root, nodes, output_side)
    cells = dict(layout.cells)
    columns, rows, turned = layout.columns, layout.rows, layout.turned
    placed: list[dict] = []
    occupied: set = set()

    def put(e: dict) -> dict:
        tiles = footprint(catalog, e)
        if tiles & occupied:
            raise ValueError(f"cluster geometry overlap at {xy(e)}")
        occupied.update(tiles)
        placed.append(e)
        return e

    def at(u: float, v: float) -> tuple[float, float]:
        return ox + u, oy + v

    machines = {}
    for name, (c, r) in sorted(cells.items(), key=lambda row: row[1][::-1]):
        x, y = at(pitch * c + size // 2 + 1, pitch * r + size // 2 + 1)
        machines[name] = put(dict(entity(machine, x, y, 0, recipe=name), _width=size, _height=size))
    edges = []
    for decision in decisions:
        a, b = cells[decision["from"]], cells[decision["to"]]
        side = _shared_side(a, b)
        count = _arms(decision["per_minute"], arm, f"{decision['from']} -> {decision['to']}")
        for u, v in _side_tiles(*a, side, pitch, size)[:count]:
            put(entity(inserter, *at(u, v), _towards(side)))
        edges.append(dict(decision, inserters=count))
    last_belt_row = max((cells[n][1] for n in nodes if nodes[n].sourced and cells[n][0] == columns - 1), default=-1)
    plate_flow: dict[str, float] = {}
    for name, node in nodes.items():
        if not node.sourced:
            continue
        c, r = cells[name]
        flow = sum(node.crafts * amount for amount in node.sourced.values())
        for item, amount in node.sourced.items():
            plate_flow[item] = plate_flow.get(item, 0.0) + node.crafts * amount
        side = "top" if r == 0 else "right"
        for u, v in _side_tiles(c, r, side, pitch, size)[: _arms(flow, arm, f"{name} sourced inputs")]:
            put(entity(inserter, *at(u, v), (_towards(side) + 8) % 16))  # picks up from the belt
    # Input belt: head, top run, optional turn down the right edge.
    belts = []
    start = -2 if len(plates) == 2 else -1
    top_end = (
        pitch * columns + 1
        if turned
        else max(pitch * cells[n][0] + size for n in nodes if nodes[n].sourced and cells[n][1] == 0)
    )
    for u in range(start, top_end + (0 if turned else 1)):
        belts.append(put(entity(belt, *at(u, -1), 4)))
    if turned:
        bottom = pitch * last_belt_row + size
        for v in range(-1, bottom + 1):
            belts.append(put(entity(belt, *at(pitch * columns + 1, v), 8)))
    ports = []
    if len(plates) == 2:
        for item, v, facing, lane in ((plates[0], -2, 8, "left"), (plates[1], 0, 0, "right")):
            put(entity(belt, *at(-2, v), facing))
            feeder = put(entity(belt, *at(-3, v), 4))
            ports.append(
                {
                    "kind": "item",
                    "item": item,
                    "position": dict(feeder["position"]),
                    "facing": 4,
                    "direction": "input",
                    "lane": lane,
                }
            )
    else:
        ports.append(
            {
                "kind": "item",
                "item": plates[0],
                "position": dict(belts[0]["position"]),
                "facing": 4,
                "direction": "input",
            }
        )
    # Output: one free side of the product machine; every side's tiles are reserved.
    c, r = cells[root]
    sides = _output_sides(c, r, columns, rows, turned, last_belt_row)
    side = output_side if output_side in sides else sides[0]
    reserve = set()
    for s in sides:
        u, v = _side_tiles(c, r, s, pitch, size)[0]
        du, dv = SIDE_STEP[s]
        reserve |= {at(u, v), at(u + du, v + dv)}
    u, v = _side_tiles(c, r, side, pitch, size)[0]
    du, dv = SIDE_STEP[side]
    out_count = _arms(crafts * head.product_amount, arm, f"{root} output")
    if out_count != 1:
        raise ValueError(f"{root}: output needs {out_count} inserters; use a row")
    arm_entity = put(entity(inserter, *at(u, v), _towards(side)))
    drop = at(u + du, v + dv)
    facing = {"bottom": 4, "left": 8, "right": 8}[side]
    if chest is not None:
        sink = put(entity(chest, *drop))
        output = {
            "kind": "item",
            "item": product,
            "direction": "output",
            "name": chest,
            "position": dict(sink["position"]),
            "facing": (_towards(side) + 8) % 16,
        }
    else:
        sink = put(entity(belt, *drop, facing))
        lane = inserter_output_lane(arm_entity["direction"], facing)
        output = {
            "kind": "item",
            "item": product,
            "direction": "output",
            "position": dict(sink["position"]),
            "facing": facing,
            "lane": lane,
        }
    ports.append(output)
    poles = _poles(catalog, placed, occupied, columns, rows, pitch, pole, limits, at)
    for p in poles:
        put(p)
    tiles = set(occupied) | reserve
    plan = _plan(
        placed,
        ports,
        template=TEMPLATE,
        layout_policy="direct-insertion-cluster-v1",
        kind="cluster",
        recipe=root,
        product=product,
        machine=machine,
        count=1,
        target_per_minute=float(rate),
        rated_per_minute=crafts * head.product_amount,
        crafts_per_machine=crafts,
        nodes=[
            {
                "recipe": n,
                "cell": list(cells[n]),
                "crafts_needed": round(nodes[n].crafts, 4),
                "nameplate_crafts": round(nodes[n].nameplate, 4),
                "utilisation": round(nodes[n].crafts / nodes[n].nameplate, 4),
            }
            for n in sorted(nodes)
        ],
        edges=edges,
        plates={item: round(flow, 4) for item, flow in sorted(plate_flow.items())},
        output_side=side,
        output_sides=sides,
        grid={"columns": columns, "rows": rows, "turned_belt": turned, "pitch": pitch},
        reserved_area=bounding_box(tiles, margin=1),
        growth={"remaining_slots": 0, "rule": "a bigger rate re-evaluates the edges (decide_edge); see grow_decision"},
        expansion_slots=[],
        inserter=inserter,
        power_port={"name": pole, "position": dict(poles[0]["position"])} if poles else None,
        scope="nameplate machine rates and conservative inserter/belt budgets; not a measured rate",
    )
    return plan


def _poles(catalog, placed, occupied, columns, rows, pitch, pole, limits, at) -> list[dict]:
    """Greedy pole set on free gap corners covering every electric entity, wire-connected."""
    radius = pole_radius(catalog, pole, limits) + 0.5
    reach = pole_reach(catalog, pole, limits)
    corners = [
        at(pitch * i, pitch * j)
        for j in range(rows + 1)
        for i in range(columns + 1)
        if at(pitch * i, pitch * j) not in occupied
    ]
    needs = [e for e in placed if (catalog.entities.get(e["name"]) or {}).get("electric")]

    def covers(corner, e):
        return any(abs(t[0] - corner[0]) < radius and abs(t[1] - corner[1]) < radius for t in footprint(catalog, e))

    chosen: list = []
    left = list(range(len(needs)))
    while left:
        best = max(corners, key=lambda p: (sum(covers(p, needs[i]) for i in left), -p[1], -p[0]))
        gained = [i for i in left if covers(best, needs[i])]
        if not gained:
            raise ValueError("cluster entity out of every gap-corner pole's supply area")
        chosen.append(best)
        left = [i for i in left if i not in gained]

    # Connect: walk the corner lattice between components (wire reach >= pitch).
    def linked(a, b):
        return math.dist(a, b) <= reach + 1e-9

    network = [chosen[0]]
    pending = chosen[1:]
    while pending:
        joined = [p for p in pending if any(linked(p, q) for q in network)]
        if joined:
            network += joined
            pending = [p for p in pending if p not in joined]
            continue
        target = pending[0]
        frontier = deque([(q, []) for q in network])
        seen = set(network)
        path = None
        while frontier:
            q, trail = frontier.popleft()
            if linked(q, target):
                path = trail
                break
            for p in corners:
                if p not in seen and linked(p, q):
                    seen.add(p)
                    frontier.append((p, trail + [p]))
        if path is None:
            raise ValueError("cluster poles cannot be wire-connected on gap corners")
        network += path
    return [entity(pole, *p) for p in dict.fromkeys(network)]


def grow_decision(
    catalog: Any, plan: dict, product_machines: int, *, inserter: str | None = None, limits: RowLimits = DEFAULT_LIMITS
) -> list[dict]:
    """Edge decisions if the product were made by ``product_machines`` machines (growth check).

    Built hardware never moves: an edge that turns into ``belt`` means growth is a
    new row block fed by a belt lane, next to the built cluster.
    """
    arm = inserter_rate(catalog, inserter or plan.get("inserter") or "inserter", limits)
    nodes = {n["recipe"]: n for n in plan["nodes"]}
    out = []
    for edge in plan["edges"]:
        node = nodes[edge["from"]]
        producers = max(1, math.ceil(node["crafts_needed"] * product_machines / node["nameplate_crafts"] - 1e-9))
        consumers = product_machines if edge["to"] == plan["recipe"] else 1
        kind, why = decide_edge(edge["per_minute"] * product_machines, producers, consumers, arm)
        out.append({"item": edge["item"], "from": edge["from"], "to": edge["to"], "kind": kind, "why": why})
    return out


def lane_loads(catalog: Any, plan: dict, limits: RowLimits = DEFAULT_LIMITS) -> dict[str, tuple[float, float]]:
    """``item -> (declared flow, capacity)`` of each input port's lane(s)."""
    out = {}
    belts = {
        xy(e): e for e in plan["entities"] if (catalog.entities.get(e["name"]) or {}).get("type") == "transport-belt"
    }
    for port in plan["ports"]:
        if port.get("direction") != "input":
            continue
        belt = belts.get(xy(port))
        cap = lane_capacity(catalog, belt["name"], limits) * (1 if port.get("lane") else 2) if belt else 0.0
        out[port["item"]] = (float(plan["plates"].get(port["item"], 0)), cap)
    return out
