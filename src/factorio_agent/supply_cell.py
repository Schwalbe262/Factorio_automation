"""``supply_cell``: a temporary, hand-fed assembler cell for bulk construction items and their intermediates.

Why: before the smelting district and the bus exist, every belt and inserter
was hand crafted (668 belts, 112 poles, 64 inserters in the first 50 minutes on
seed 283895142) and jobs queued behind "crafting queue is busy". A player puts
down a small chest-fed factory as soon as power and automation exist, and keeps
gears and circuits in stock so a drill or a lab is only a final assembly. This
module is that cell, modelled on the startup science cell
(``board.startup_science_cell``): not connected to any belt or the bus, plates
go in by hand in batches, machines make the products, products are collected
from chests.

Lifecycle (rules in code, see ``docs/v2/PLANNERS.md``, "Supply cell"):

1. Propose (``supply_cell_nodes``): after ``power`` succeeded and
   assembling-machine-1 is enabled, once per map, for the products that
   ``choose_products`` picks from the upcoming demand
   (``supply_demand.item_demand``: the hardware of the next
   ``DEMAND_HORIZON`` roadmap steps, measured from saved plans where a job
   already planned, and every intermediate inside it). Ingredients are priced
   first; a product whose ingredients the cell already stocks is priced with
   only its own machine (shared machines) and its residual hand time.
2. Build (``SupplyCell.step``): design the cell (``layout``), survey a site
   near the powered grid (never on ore, never in a site-plan zone or another
   block), register board block ``supply_cell``, build it, cap each product
   chest with a bar, feed a first batch, prove machine output in the chests.
3. Use: ``ensure_item`` collects products from their chests and stops hand
   crafting above ``HANDCRAFT_LIMIT`` (``stock`` hook, time rule); before a
   hand craft it collects the stocked direct ingredients (``intermediate_take``)
   so the craft queue only does the final assembly. Shared upkeep and the idle
   lane top up the input chests, sized from the whole upcoming demand
   (pre-production), but only from the steps in flight for a plate that a
   job is waiting for (``contested_plates``).
4. Retire (``supply_cell_nodes`` with ``{"retire": true}``): when every
   product's remaining demand is at most ``HANDCRAFT_LIMIT`` or a ``produce``
   row for it succeeded, or when the build job stayed blocked for
   ``ABANDON_TICKS``. Feeding stops at once; the job waits while any job still
   waits for its products and (at most ``DRAIN_TICKS``) while input chests
   still hold plates, then mines the machines, inserters and chests (paid
   ``mine``, contents return to the inventory), removes the board block and
   records ``phase: retired`` (never proposed again). Poles stay: other blocks'
   pole chains may hang on them.

Cell geometry for ``[gear, circuit, belt, inserter]`` (``render(layout(...))``
on the real catalog). Every product has one product chest (``H``, bar 2
stacks); a machine whose ingredient is another product takes it from that
chest through its own inserter, so the one gear machine feeds the gear chest,
the belt machine and the inserter machine (arrows: where an inserter drops;
``c`` hand-fed input chest; letters: machines; ``P`` poles)::

    .............P..P...
    .........DDD...EEE..     D circuit machine, E cable machine (chained off D's input chest)
    .......H<DDD<c<EEE<c     H circuit chest
    .......vPDDD...EEE..
    ......AAA...........     A inserter machine: circuits from the north chest,
    ....c>AAA>H.........       gears from the south chest, iron from c, product H east
    P.....AAA...........
    ...CCCP^.BBB........     C gear machine, B belt machine
    .c>CCC>H>BBB>H......     H in the middle: the gear chest (C -> H -> A and B)
    ...CCC...BBB........
    ..........^P........
    ..........c.........

(printed again by ``tests/test_v2_supply_cell.py`` ``SharedLayoutTests``).

Placement (``_Grid``): products with the most stocked ingredients first; a
machine stands at inserter distance (3 tiles) from every product chest it
touches, its own chest, its stocked ingredients' chests and one input chest
take one side each (4 sides at most); a machine that touches no placed chest
goes below the cell. Non-stocked intermediates (cable inside a circuit when
cable is not a product) are chained off the input chest as before
(``_place``). Raw ingredients (``progression.RAW_ITEMS``) are hand-fed ports on
input chests. Inserters only take what a recipe needs, so a chest may hold
plates and an intermediate at once.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import permutations
import json
import math
from pathlib import Path
import re

from .blocks.params import pole_radius, pole_reach
from .blocks.stock_tail import STOCK_STACKS
from .board import Board, entity, hardware_conflicts, planned_unbuilt_hardware, pole_chain
from .construction_supply import COLLECT_BATCH, HANDCRAFT_LIMIT
from .core.deterministic_builder import FactoryBuilder, _plan
from .core.deterministic_state import _atomic_json
from .machine_stock import STALL_TICKS
from .routing import VECTORS

# ----- constants (tuning = change here and the pinning test) -----------------
CAPABILITY = "supply_cell"
STATE_FILE = "supply-cell.json"
BLOCK = "supply_cell"
TEMPLATE = "supply_cell_v1"
MACHINE = "assembling-machine-1"
INSERTER = "inserter"
POLE = "small-electric-pole"
CHESTS = ("wooden-chest", "iron-chest")  # cheapest enabled first
DEMAND_HORIZON = 8  # roadmap steps whose builds count as upcoming demand
MAX_PRODUCTS = 6  # product chests one cell keeps (validate_parameters)
# Chest <-> machine basic inserter, hand size 1: about 0.83 swings/s.
CHEST_INSERTER_PER_MINUTE = 50.0
HAND_CRAFT_SPEED = 1.0  # the character crafts at speed 1
# A waiting job leaves the character to the other active jobs (jobs.MAX_ACTIVE_JOBS = 4).
WAIT_FACTOR = 3.0
# Machine-made stock counts only as far as the cell makes it within this window: the steps of
# DEMAND_HORIZON took about 50 minutes on seed 283895142, later stock would come too late.
PRODUCTION_WINDOW_SECONDS = 1800.0
MAX_CHILDREN = 3  # intermediate ingredients one machine's input chest can take (west, north, south)
MAX_DEPTH = 4
MACHINE_SIDES = 4  # chests one machine touches: its product, stocked ingredients, one input chest
FEED_CAP = 100  # items of one kind per input chest (one plate stack)
FEED_BATCH = 3 * COLLECT_BATCH  # one feed trip fills a third of an input chest
FEED_RESERVE = 50  # plates a job's own upkeep feed leaves in the character's hands
PROOF_UNITS = 2  # first batch: enough for two products of each kind
OUTPUT_STACKS = STOCK_STACKS
SITE_PITCH = 8  # candidate origins on an 8-tile grid around the powered pole
SITE_REACH = 6  # ... up to 48 tiles each way, nearest first
MAX_SITE_CANDIDATES = 40
PROGRESS_WRITE_TICKS = 600  # at most one progress write per 10 game seconds
DRAIN_TICKS = 3600  # retiring waits at most one game minute for machines to use the plates inside
ABANDON_TICKS = 36000  # a build blocked this long (10 game minutes, auto-retry had its chances) is retired
BELOW_TRIES = 16  # rows tried below the cell for a machine that touches no placed chest
BUILD_PRIORITY = -52  # just below produce rows (-51)
RETIRE_PRIORITY = -1000  # demolition is never on the critical path
NODE_ID = "auto_supply_cell"
RETIRE_NODE_ID = "auto_supply_cell_retire"
ACTIVE = ("running", "waiting", "pending")
# Side preference of a machine's chests: product east, input west, stocked ingredients north, then south.
SIDES = ((1, 0), (-1, 0), (0, -1), (0, 1))
_GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def _raw_items():
    from .progression import RAW_ITEMS

    return RAW_ITEMS


def _out_amount(recipe, item):
    from .progression import _product_amount

    return _product_amount(recipe, item)


# ----- recipe tree ---------------------------------------------------------
def recipe_tree(catalog, item, units=1.0, *, enabled=None, depth=0, stocked=frozenset()):
    """``{item, recipe, energy, crafts, raw, hubs, children}`` per product unit, from catalog recipes only.

    ``crafts`` is crafts of the recipe per product unit, ``raw`` the hand-fed
    items this machine's input chest needs per product unit, ``hubs`` (root
    only) the units of ``stocked`` ingredients taken from their own product
    chest instead of a sub-tree. Raises ValueError when the tree does not fit
    the cell (fluid, no recipe, a category assembling-machine-1 cannot craft,
    too deep or too wide, locked recipe when ``enabled`` is given).
    """
    if depth > MAX_DEPTH:
        raise ValueError(f"supply cell recipe tree too deep at {item}")
    recipe = catalog.recipe_for_product(item)
    if not recipe:
        raise ValueError(f"no recipe for {item}")
    categories = set((catalog.entities.get(MACHINE) or {}).get("crafting_categories") or ())
    if not set(recipe.get("categories") or [recipe.get("category")]) & categories:
        raise ValueError(f"{MACHINE} cannot craft {recipe['name']}")
    if enabled is not None and recipe["name"] not in enabled:
        raise ValueError(f"recipe {recipe['name']} is locked")
    crafts = units / _out_amount(recipe, item)
    raw, hubs, children = {}, {}, []
    for row in recipe.get("ingredients") or ():
        if row.get("type", "item") != "item":
            raise ValueError(f"{recipe['name']} needs a fluid")
        need = row["amount"] * crafts
        if row["name"] in _raw_items():
            raw[row["name"]] = raw.get(row["name"], 0.0) + need
        elif depth == 0 and row["name"] in stocked and row["name"] != item:
            hubs[row["name"]] = hubs.get(row["name"], 0.0) + need
        else:
            children.append(recipe_tree(catalog, row["name"], need, enabled=enabled, depth=depth + 1))
    if len(children) > MAX_CHILDREN:
        raise ValueError(f"{recipe['name']} has more than {MAX_CHILDREN} intermediate ingredients")
    if 1 + len(hubs) + bool(raw or children) > MACHINE_SIDES:
        raise ValueError(f"{recipe['name']} touches more than {MACHINE_SIDES} chests")
    return {
        "item": item,
        "recipe": recipe["name"],
        "energy": float(recipe.get("energy") or 0.5),
        "units": units,
        "crafts": crafts,
        "raw": raw,
        "hubs": hubs,
        "children": children,
    }


def _nodes(tree):
    yield tree
    for child in tree["children"]:
        yield from _nodes(child)


def _fed(node):
    """A node has a hand-fed input chest when it needs raw items or builds sub-trees."""
    return bool(node["raw"] or node["children"])


def hand_seconds(catalog, item, count=1.0, stocked=frozenset()):
    """Character crafting time for ``count`` items; intermediates are included unless ``stocked`` (taken from the cell)."""
    if item in _raw_items():
        return 0.0
    recipe = catalog.recipe_for_product(item)
    if not recipe:
        return 0.0
    crafts = count / _out_amount(recipe, item)
    total = crafts * float(recipe.get("energy") or 0.5) / HAND_CRAFT_SPEED
    for row in recipe.get("ingredients") or ():
        if row.get("type", "item") == "item" and row["name"] not in stocked:
            total += hand_seconds(catalog, row["name"], row["amount"] * crafts, stocked)
    return total


def recipe_depth(catalog, item, depth=0):
    """0 for a recipe of raw items only, else 1 + the deepest ingredient (ingredients are priced first)."""
    recipe = catalog.recipe_for_product(item) if item not in _raw_items() else None
    if not recipe or depth > MAX_DEPTH * 4:
        return 0
    deeper = [
        1 + recipe_depth(catalog, row["name"], depth + 1)
        for row in recipe.get("ingredients") or ()
        if row.get("type", "item") == "item" and row["name"] not in _raw_items()
    ]
    return max(deeper, default=0)


def units_per_minute(catalog, tree, hub_rates=None):
    """Product units/min: the slowest machine, chest inserter or stocked-ingredient supply of the tree."""
    speed = float((catalog.entities.get(MACHINE) or {}).get("crafting_speed") or 0.5)
    rates = []
    for node in _nodes(tree):
        rates.append(60.0 * speed / node["energy"] / node["crafts"])
        moved_in = sum(node["raw"].values()) + sum(child["units"] for child in node["children"])
        if moved_in:
            rates.append(CHEST_INSERTER_PER_MINUTE / moved_in)
        rates.append(CHEST_INSERTER_PER_MINUTE / node["units"])
        for hub, units in node.get("hubs", {}).items():
            rates.append(CHEST_INSERTER_PER_MINUTE / units)
            if hub_rates and hub in hub_rates:
                rates.append(hub_rates[hub] / units)
    return min(rates)


def hardware(tree):
    """Items the tree places: one machine per node, an inserter per chest it touches, its input chests, one product chest."""
    nodes = list(_nodes(tree))
    return {
        MACHINE: len(nodes),
        INSERTER: sum(1 + _fed(n) + len(n.get("hubs", {})) for n in nodes),
        "chest": sum(_fed(n) for n in nodes) + 1,
    }


def evaluate(catalog, item, count, *, enabled=None, chest="wooden-chest", stocked=frozenset(), hub_rates=None):
    """Whether making ``count`` items in the cell pays, with ``stocked`` products already in the cell."""
    row = {"item": item, "count": count, "qualifies": False}
    stocked = frozenset(stocked) - {item}
    try:
        tree = recipe_tree(catalog, item, enabled=enabled, stocked=stocked)
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        return dict(row, reason=str(exc)[:120])
    hand = hand_seconds(catalog, item, count, stocked)
    rate = units_per_minute(catalog, tree, hub_rates)
    made = min(count, rate * PRODUCTION_WINDOW_SECONDS / 60.0)
    saved = hand_seconds(catalog, item, made, stocked)
    # The cell is hand crafted before it holds any stock: its hardware is priced at full hand time.
    build = sum(hand_seconds(catalog, name if name != "chest" else chest, n) for name, n in hardware(tree).items())
    row.update(
        hand_seconds=round(hand, 1),
        machine_seconds=round(60.0 * count / rate, 1),
        saved_hand_seconds=round(saved, 1),
        build_hand_seconds=round(build, 1),
        units_per_minute=round(rate, 2),
        stocked_inputs=sorted(tree["hubs"]),
    )
    if count <= HANDCRAFT_LIMIT:
        return dict(row, reason="demand within the hand-craft limit")
    if hand < build:
        return dict(row, reason="hand crafting the demand is cheaper than hand crafting the cell")
    if saved < build:
        return dict(row, reason="the cell makes too little within the production window to pay for itself")
    return dict(row, qualifies=True, reason="cell pays for itself")


def choose_products(catalog, demand, *, enabled=None, chest=CHESTS[0]):
    """``(products, rows)``: greedy over ``demand`` (``{item: count}``), largest hand time first.

    Each item is priced with the products chosen before it stocked
    (``evaluate``: a machine that takes a stocked ingredient saves only the
    residual hand time) and kept only when the cell layout still fits.
    """
    order = sorted(demand, key=lambda i: (-hand_seconds(catalog, i, demand[i]), i))
    chosen, rates, rows = [], {}, {}
    for item in order:
        row = evaluate(catalog, item, demand[item], enabled=enabled, chest=chest, stocked=chosen, hub_rates=rates)
        if row["qualifies"] and len(chosen) >= MAX_PRODUCTS:
            row = dict(row, qualifies=False, reason=f"the cell already holds {MAX_PRODUCTS} products")
        if row["qualifies"]:
            try:
                layout(catalog, chosen + [item], chest=chest, enabled=enabled)
            except ValueError as exc:
                row = dict(row, qualifies=False, reason=f"no room in the cell layout: {exc}"[:120])
        if row["qualifies"]:
            chosen.append(item)
            rates[item] = row["units_per_minute"]
        rows[item] = row
    return chosen, rows


def candidates(catalog, jobs, observation, *, root=None):
    """Receipt field ``supply_cell_candidates``: per-item demand rows and the chosen products (in choice order)."""
    from .supply_demand import item_demand

    rows = item_demand(catalog, jobs, observation, horizon=DEMAND_HORIZON, root=root)
    demand = {item: row["count"] for item, row in rows.items() if row["direct_use"]}
    enabled = _enabled(observation)
    chest = choose_chest(enabled) or CHESTS[0]
    products, evaluated = choose_products(catalog, demand, enabled=enabled, chest=chest)
    return {
        "demand": demand,
        "current": {item: row["current"] for item, row in rows.items() if row["direct_use"]},
        "products": products,
        "rows": evaluated,
    }


def _enabled(observation):
    value = (observation or {}).get("enabled_recipes") or {}
    if isinstance(value, dict):
        return {name for name, on in value.items() if on}
    return set(value)


def choose_chest(enabled):
    return next((name for name in CHESTS if name in enabled), None)


# ----- layout --------------------------------------------------------------
def _xy(e):
    return (e["position"]["x"], e["position"]["y"])


def _direction(vector):
    return next(d for d, v in VECTORS.items() if v == tuple(vector))


def _add(p, v, k=1):
    return (p[0] + k * v[0], p[1] + k * v[1])


def _point(p):
    return {"x": p[0], "y": p[1]}


def _place(tree, chest_at, d, chest, rows):
    """Machine of ``tree`` at ``chest_at`` + 3 ``d`` feeding that chest, its input chest beyond, then its children."""
    facing = _direction(d)
    machine_at, input_at = _add(chest_at, d, 3), _add(chest_at, d, 6)
    rows["entities"] += [
        entity(INSERTER, *_add(chest_at, d, 1), facing),
        entity(MACHINE, *machine_at, recipe=tree["recipe"]),
        entity(INSERTER, *_add(chest_at, d, 5), facing),
        entity(chest, *input_at),
    ]
    rows["nodes"].append(
        {"item": tree["item"], "machine": _point(machine_at), "chest": _point(input_at), "raw": dict(tree["raw"])}
    )
    side = (d[1], -d[0])
    for child, way in zip(tree["children"], (d, side, (-side[0], -side[1]))):
        _place(child, input_at, way, chest, rows)


def _tiles(entities):
    return set().union(*[_GEOMETRY._occupied_by_plan([e]) for e in entities]) if entities else set()


class _Grid:
    """The cell under construction: entities, taken tiles, each product's chest and its machines."""

    def __init__(self, chest, origin):
        self.chest, self.origin = chest, origin
        self.entities, self.taken = [], set()
        self.chests = {}  # product -> its product chest position (a hub for machines that use it)
        self.nodes = {}  # product -> node rows of its machines (root first)

    def place(self, tree):
        for center in self._centers(tree):
            for sides in self._side_choices(tree, center):
                placed = self._try(tree, center, sides)
                if placed is not None:
                    return
        raise ValueError(f"supply cell has no free place for {tree['item']}")

    def _links(self, tree):
        """The chests the root machine touches besides its input chest: its product, then stocked ingredients."""
        return [tree["item"]] + sorted(tree["hubs"])

    def _centers(self, tree):
        fixed = [self.chests[i] for i in self._links(tree) if i in self.chests]
        if fixed:
            around = [_add(fixed[0], s, 3) for s in SIDES]
            return [c for c in around if all(any(_add(c, s, 3) == f for s in SIDES) for f in fixed)]
        if not self.entities:
            return [_add(self.origin, (-3, 0))]  # product chest at the origin, machine west of it
        bottom = max(t[1] for t in self.taken)
        return [(self.origin[0] - 3, bottom + 3 + k) for k in range(BELOW_TRIES)]

    def _side_choices(self, tree, center):
        """``{role: side}`` per arrangement; a role whose chest exists already must face it."""
        roles = [("product", tree["item"])] + ([("input", None)] if _fed(tree) else [])
        roles += [("stocked", item) for item in sorted(tree["hubs"])]
        forced = {}
        for role in roles:
            if role[1] in self.chests and role[0] != "input":
                side = next((s for s in SIDES if _add(center, s, 3) == self.chests[role[1]]), None)
                if side is None:
                    return
                forced[role] = side
        free_roles = [r for r in roles if r not in forced]
        free_sides = [s for s in SIDES if s not in forced.values()]
        for chosen in permutations(free_sides, len(free_roles)):
            yield {**forced, **dict(zip(free_roles, chosen))}

    def _try(self, tree, center, sides):
        new = [entity(MACHINE, *center, recipe=tree["recipe"])]
        chests, input_at = {}, None
        for (kind, item), side in sides.items():
            pick = (-side[0], -side[1]) if kind == "product" else side  # the product inserter picks from the machine
            new.append(entity(INSERTER, *_add(center, side, 2), _direction(pick)))
            at = _add(center, side, 3)
            if kind == "input":
                input_at = at
                new.append(entity(self.chest, *at))
            elif item not in self.chests:
                chests[item] = at
                new.append(entity(self.chest, *at))
        nodes = [
            {
                "item": tree["item"],
                "machine": _point(center),
                "chest": _point(input_at) if input_at else None,
                "raw": dict(tree["raw"]),
            }
        ]
        if input_at is not None:
            rows = {"entities": [], "nodes": []}
            way = sides[("input", None)]
            side = (way[1], -way[0])
            for child, d in zip(tree["children"], (way, side, (-side[0], -side[1]))):
                _place(child, input_at, d, self.chest, rows)
            new += rows["entities"]
            nodes += rows["nodes"]
        tiles = [_GEOMETRY._occupied_by_plan([e]) for e in new]
        union = set().union(*tiles)
        if sum(map(len, tiles)) != len(union) or union & self.taken:
            return None
        self.entities += new
        self.taken |= union
        self.chests.update(chests)
        self.nodes[tree["item"]] = nodes
        return nodes


def _covers(catalog, pole, e):
    reach = pole_radius(catalog, POLE) - 0.5
    px, py = pole
    return any(abs(tx - px) <= reach + 1e-6 and abs(ty - py) <= reach + 1e-6 for tx, ty in _tiles([e]))


def place_poles(catalog, entities):
    """Greedy poles: most uncovered machines/inserters first, every new pole within wire reach of one before."""
    needy = [e for e in entities if e["name"] in (MACHINE, INSERTER)]
    taken = _tiles(entities)
    xs = [t[0] for t in taken]
    ys = [t[1] for t in taken]
    free = [
        (x + 0.5, y + 0.5)
        for x in range(math.floor(min(xs)) - 1, math.floor(max(xs)) + 2)
        for y in range(math.floor(min(ys)) - 1, math.floor(max(ys)) + 2)
        if (x + 0.5, y + 0.5) not in taken
    ]
    wire = pole_reach(catalog, POLE)
    poles, uncovered = [], list(needy)
    while uncovered:
        near = [t for t in free if not poles or any(math.dist(t, p) <= wire for p in poles)]
        scored = sorted(near, key=lambda t: (-sum(_covers(catalog, t, e) for e in uncovered), t))
        if not scored:
            raise ValueError("supply cell poles cannot reach every machine")
        best = scored[0]
        if not any(_covers(catalog, best, e) for e in uncovered):
            # A relay towards the nearest uncovered entity keeps the chain connected.
            goal = min((_xy(e) for e in uncovered), key=lambda g: min(math.dist(g, p) for p in poles))
            best = min(near, key=lambda t: (math.dist(t, goal), t))
        poles.append(best)
        free.remove(best)
        uncovered = [e for e in uncovered if not _covers(catalog, best, e)]
        if len(poles) > len(needy) + 8:
            raise ValueError("supply cell pole placement does not converge")
    return [entity(POLE, x, y) for x, y in poles]


def _rates(catalog, trees):
    """Units/min per product; a product's stocked ingredients limit it by their own rate."""
    rates = {}

    def rate(item):
        if item not in rates:
            hubs = {h: rate(h) for h in trees[item]["hubs"]}
            rates[item] = units_per_minute(catalog, trees[item], hubs)
        return rates[item]

    for item in trees:
        rate(item)
    return rates


def layout(catalog, products, *, chest, enabled=None, origin=(0.5, 0.5)):
    """The cell plan for ``products``: entities, ports, per-product rows (product chest, machines, rates)."""
    stocked = frozenset(products)
    trees = {p: recipe_tree(catalog, p, enabled=enabled, stocked=stocked - {p}) for p in products}
    grid = _Grid(chest, origin)
    for item in sorted(products, key=lambda p: (-len(trees[p]["hubs"]), products.index(p))):
        grid.place(trees[item])
    rates = _rates(catalog, trees)
    ports, rows = [], []
    for item in products:
        output = _point(grid.chests[item])
        ports.append({"item": item, "kind": "item", "direction": "output", "position": dict(output)})
        for node in grid.nodes[item]:
            for raw in node["raw"]:
                ports.append({"item": raw, "kind": "item", "direction": "input", "position": dict(node["chest"])})
        rows.append(
            {
                "item": item,
                "output": output,
                "nodes": grid.nodes[item],
                "stocked_inputs": sorted(trees[item]["hubs"]),
                "units_per_minute": round(rates[item], 3),
                "hand_seconds_per_unit": round(hand_seconds(catalog, item, 1.0, stocked - {item}), 4),
            }
        )
    entities = grid.entities + place_poles(catalog, grid.entities)
    return {"entities": entities, "ports": ports, "products": rows, "chest": chest}


ARROWS = {(1, 0): ">", (-1, 0): "<", (0, -1): "^", (0, 1): "v"}


def render(design):
    """ASCII picture of a design: machine letters (legend), ``H`` product chest, ``c`` input chest, drop arrows, ``P``."""
    outputs = {(r["output"]["x"], r["output"]["y"]) for r in design["products"]}
    letters, cells = {}, {}
    for e in design["entities"]:
        if e["name"] == MACHINE:
            mark = letters.setdefault(e["recipe"], chr(ord("A") + len(letters)))
        elif e["name"] == INSERTER:
            vx, vy = VECTORS[e["direction"]]
            mark = ARROWS[(-vx, -vy)]
        elif e["name"] == POLE:
            mark = "P"
        else:
            mark = "H" if _xy(e) in outputs else "c"
        for tile in _GEOMETRY._occupied_by_plan([e]):
            cells[tile] = mark
    xs = [t[0] for t in cells]
    ys = [t[1] for t in cells]
    lines = []
    for y in range(math.floor(min(ys)), math.floor(max(ys)) + 1):
        lines.append(
            "".join(cells.get((x + 0.5, y + 0.5), ".") for x in range(math.floor(min(xs)), math.floor(max(xs)) + 1))
        )
    legend = ", ".join(f"{mark} {recipe}" for recipe, mark in letters.items())
    return "\n".join(lines + [legend])


def _area(entities, margin=1):
    tiles = _tiles(entities)
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return [[min(xs) - 0.5 - margin, min(ys) - 0.5 - margin], [max(xs) + 0.5 + margin, max(ys) + 0.5 + margin]]


def _shift(design, dx, dy):
    moved = deepcopy(design)

    def go(p):
        if p is not None:
            p["x"] += dx
            p["y"] += dy

    for e in moved["entities"]:
        go(e["position"])
    for port in moved["ports"]:
        go(port["position"])
    for row in moved["products"]:
        go(row["output"])
        for node in row["nodes"]:
            go(node["machine"])
            go(node["chest"])
    return moved


def _overlaps(a, b):
    return a[0][0] < b[1][0] and b[0][0] < a[1][0] and a[0][1] < b[1][1] and b[0][1] < a[1][1]


SURVEY_LUA = """local plans=helpers.json_to_table(PAYLOAD);local legal={};local clearable={}
for i,p in ipairs(plans) do
 local ok=s.count_entities_filtered{area=p.reserved_area,type="resource"}==0
 if ok then for _,e in pairs(s.find_entities_filtered{area=p.reserved_area,force=f}) do
  if e.type~="character" and e.type~="electric-pole" then ok=false;break end end end
 local natural=false
 if ok then for _,e in ipairs(p.entities) do
  local q=e.position
  if s.count_entities_filtered{area={{q.x-.5,q.y-.5},{q.x+.5,q.y+.5}},type="resource"}>0 then ok=false;break end
  local old=target(q,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=q,direction=e.direction,force=f} then
   if s.can_place_entity{name=e.name,position=q,direction=e.direction,force=f,
    build_check_type=defines.build_check_type.manual_ghost,forced=true} then natural=true else ok=false;break end
  end
 end end
 if ok and natural then clearable[#clearable+1]=i elseif ok then legal[#legal+1]=i end
end
return {ok=true,legal=legal,clearable=clearable}"""


def _indices(value):
    return list(value.values()) if isinstance(value, dict) else list(value or [])


# ----- controller ------------------------------------------------------------
class SupplyCell:
    """One cell per map; state ``supply-cell.json``, board block ``supply_cell``."""

    def __init__(self, game, bootstrap, builder, catalog, *, site=None, rows_stock=None):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.site = site
        self.rows_stock = rows_stock  # produce rows hook: a producing row is the permanent source
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE

    # state
    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self, state):
        _atomic_json(self.path, state)

    def _live(self, obs):
        """The saved state when it belongs to this world and catalog, else {}."""
        state = self.state()
        if state.get("world_id") != obs.get("world_id") or state.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {}
        return state

    def _jobs(self, obs):
        path = self.root / "jobs.json"
        if not path.exists():
            return []
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("world_id") != obs.get("world_id"):
            return []
        return [job for job in ledger.get("jobs", {}).values() if job.get("world_id") == obs.get("world_id")]

    def _demand_rows(self, obs):
        from .supply_demand import item_demand

        return item_demand(self.catalog, self._jobs(obs), obs, horizon=DEMAND_HORIZON, root=self.root)

    def demand(self, obs):
        """Units of each item in the hardware of the next DEMAND_HORIZON steps (pre-production target)."""
        return {item: row["count"] for item, row in self._demand_rows(obs).items()}

    def current_demand(self, obs):
        """Units of each item in the hardware of the steps whose jobs are in flight."""
        return {item: row["current"] for item, row in self._demand_rows(obs).items()}

    def contested_plates(self, obs):
        """Raw items another active job is waiting for (its reason or evidence names the item)."""
        items = set()
        for job in self._jobs(obs):
            if job.get("capability") == CAPABILITY or job.get("status") not in ACTIVE:
                continue
            reason = str(job.get("reason") or "")
            evidence = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
            for raw in _raw_items():
                named = re.search(rf"(?<![\w-]){re.escape(raw)}(?![\w-])", reason)
                if evidence.get("item") == raw or (reason.startswith("waiting") and named):
                    items.add(raw)
        return items

    # site
    def prepare(self, obs, products):
        enabled = _enabled(obs)
        chest = choose_chest(enabled)
        if chest is None:
            return {"ok": False, "reason": "supply_cell_chest_recipe_locked"}
        try:
            canonical = layout(self.catalog, products, chest=chest, enabled=enabled)
        except ValueError as exc:
            return {"ok": False, "reason": "supply_cell_design_unsupported", "detail": str(exc)[:200]}
        networks = {
            e.get("electric_network_id")
            for e in obs.get("entities", [])
            if e.get("name") == "steam-engine" and e.get("energy", 0) > 0
        }
        poles = [
            e for e in obs.get("entities", []) if e.get("name") == POLE and e.get("electric_network_id") in networks
        ]
        if not poles:
            return {"ok": False, "reason": "no_energized_source_network"}
        board = Board(self.game, self.builder, self.catalog)
        if board.state and board.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "board_world_changed"}
        blocks = board.state.get("blocks", {})
        planned = planned_unbuilt_hardware(self.builder, obs, blocks)
        zones = self._zones(obs)
        if isinstance(zones, dict):
            return zones
        reference = poles[0]["position"]
        bx, by = math.floor(reference["x"]) + 0.5, math.floor(reference["y"]) + 0.5
        reach = range(-SITE_REACH, SITE_REACH + 1)
        offsets = sorted(
            ((SITE_PITCH * i, SITE_PITCH * j) for i in reach for j in reach), key=lambda o: (math.hypot(*o), o)
        )
        found = []
        for dx, dy in offsets:
            design = _shift(canonical, bx + dx - 0.5, by + dy - 0.5)
            area = _area(design["entities"])
            if any(_overlaps(area, b["reserved_area"]) for b in blocks.values() if b.get("reserved_area")):
                continue
            if any(_overlaps(area, z) for z in zones):
                continue
            cell_poles = [e for e in design["entities"] if e["name"] == POLE]
            target = min(cell_poles, key=lambda e: min(math.dist(_xy(e), _xy(p)) for p in poles))
            source = min(poles, key=lambda p: math.dist(_xy(p), _xy(target)))
            for horizontal_first in (True, False):
                chain = pole_chain(source["position"], target["position"], horizontal_first=horizontal_first)
                entities = design["entities"] + [e for e in chain if e["position"] != target["position"]]
                tiles = [self.builder._occupied_by_plan([e]) for e in entities]
                if sum(map(len, tiles)) != len(set().union(*tiles)):
                    continue
                if any(_overlaps(_area([e], 0), z) for e in chain for z in zones):
                    continue
                if hardware_conflicts(self.builder, entities, planned):
                    continue
                found.append(dict(design, entities=entities, reserved_area=area))
                break
            if len(found) >= MAX_SITE_CANDIDATES:
                break
        if not found:
            return {"ok": False, "reason": "no_supply_cell_site_off_ore_and_plan_zones"}
        payload = json.dumps(json.dumps(found, separators=(",", ":")))
        surveyed = self.game.query(SURVEY_LUA.replace("PAYLOAD", payload))
        if not isinstance(surveyed, dict) or not surveyed.get("ok"):
            return {"ok": False, "reason": (surveyed or {}).get("reason", "supply_cell_site_survey_failed")}
        chosen, clear = None, None
        legal = _indices(surveyed.get("legal"))
        if legal:
            chosen = found[legal[0] - 1]
        else:
            from .planning import preflight

            for index in _indices(surveyed.get("clearable")):
                check = preflight(self.game, self.builder, obs, found[index - 1]["entities"])
                if check.get("ok"):
                    chosen, clear = found[index - 1], check["clear_obstacles"]
                    break
        if chosen is None:
            return {"ok": False, "reason": "no_clear_supply_cell_site", "candidates": len(found)}
        extra = {"clear_obstacles": clear, "clearance_world_id": obs["world_id"]} if clear else {}
        plan = _plan(
            chosen["entities"],
            chosen["ports"],
            template=TEMPLATE,
            layout_policy="pcb-block-v1",
            reserved_area=chosen["reserved_area"],
            **extra,
        )
        return {
            "ok": True,
            "schema_version": 1,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "products": chosen["products"],
            "plan": plan,
            "phase": "build",
        }

    def _zones(self, obs):
        """Site-plan zone rects (bus band, mining, smelting, corridors, allocations) as areas."""
        if self.site is None:
            return []
        from .site.planmodel import SiteError

        try:
            world = self.site.world(obs["world_id"])
        except SiteError as exc:
            return {"ok": False, "reason": "supply_cell_site_plan_unavailable", "detail": str(exc)[:200]}
        return [[[r.x0, r.y0], [r.x1, r.y1]] for _, r in world.zones()]

    def _publish(self, obs, state):
        board = Board(self.game, self.builder, self.catalog)
        board.refresh(force=True)
        if not board.state:
            board.state = {
                "schema_version": 1,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "blocks": {},
            }
        if (
            board.state.get("world_id") != obs["world_id"]
            or board.state.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            return {"status": "blocked", "reason": "supply_cell_board_identity_changed"}
        if board.state["blocks"].get(BLOCK) != state["plan"]:
            board.state["blocks"][BLOCK] = state["plan"]
            _atomic_json(board.path, board.state)
        return None

    # execution
    def step(self, obs, parameters):
        validate_parameters(parameters)
        if parameters.get("retire"):
            return self._retire(obs)
        state = self.state()
        if not state:
            prepared = self.prepare(obs, parameters["products"])
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            prepared.pop("ok")
            state = prepared
            self._save(state)
        if not self._live(obs):
            return {"status": "blocked", "reason": "supply_cell_world_or_catalog_changed"}
        if state.get("phase") in ("retiring", "retired"):
            return {"status": "blocked", "reason": "supply_cell_retired"}
        locked = sorted(self._recipes(state) - _enabled(obs))
        if locked:
            return {"status": "blocked", "reason": "supply_cell_recipes_locked", "missing": locked}
        published = self._publish(obs, state)
        if published:
            return published
        result = self.builder.ensure_plan(obs, state["plan"])
        if result.get("status") != "succeeded":
            return result
        bar = self._bar(obs, state)
        if bar is not None:
            return bar
        evidence = self.proof(obs)
        if evidence is not None:
            state["phase"] = "verified"
            self._save(state)
            return {
                "status": "succeeded",
                "reason": "hand-fed supply cell stocks machine-made items",
                "evidence": evidence,
            }
        feed = self.feed_action(obs, reserve=0, acquire=True, minimum=PROOF_UNITS)
        if feed is not None:
            return feed
        return {"status": "waiting", "reason": "waiting for machine-made products in the supply cell"}

    def _recipes(self, state):
        return {e["recipe"] for e in state["plan"]["entities"] if e.get("recipe")}

    def _bar(self, obs, state):
        from .production_rows import BAR_LUA

        done = set(state.get("bars", []))
        for row in state["products"]:
            key = f"{row['output']['x']},{row['output']['y']}"
            if key in done:
                continue
            name = self._chest_name(state)
            payload = json.dumps(json.dumps({"name": name, "position": row["output"]}))
            native = self.game.query(BAR_LUA.replace("PAYLOAD", payload))
            if not isinstance(native, dict) or not native.get("ok"):
                return {"status": "blocked", **(native if isinstance(native, dict) else {"reason": "bar_query_failed"})}
            if native.get("slots") != OUTPUT_STACKS:
                return {"type": "bar", "name": name, "position": dict(row["output"]), "slots": OUTPUT_STACKS}
            done.add(key)
            state["bars"] = sorted(done)
            self._save(state)
        return None

    def _chest_name(self, state):
        output = state["products"][0]["output"]
        return next(e["name"] for e in state["plan"]["entities"] if e["position"] == output)

    def _find(self, obs, name, position):
        return next(
            (e for e in obs.get("entities", []) if e.get("name") == name and e.get("position") == position), None
        )

    def proof(self, obs):
        """Every product: its machines exist with the recipe and have finished products, its chest holds stock."""
        state = self._live(obs)
        if not state or state.get("phase") == "retired":
            return None
        chest = self._chest_name(state)
        rows = []
        for row in state["products"]:
            machines = [self._find(obs, MACHINE, node["machine"]) for node in row["nodes"]]
            if any(m is None or m.get("products_finished", 0) < 1 for m in machines):
                return None
            if any(m.get("recipe") != node_recipe for m, node_recipe in zip(machines, self._node_recipes(state, row))):
                return None
            box = self._find(obs, chest, row["output"])
            stock = int(((box or {}).get("inventory") or {}).get(row["item"], 0))
            if stock < 1:
                return None
            rows.append(
                {
                    "item": row["item"],
                    "chest_unit": box.get("unit_number"),
                    "chest_stock": stock,
                    "machine_products_finished": [m.get("products_finished") for m in machines],
                }
            )
        return {
            "tick": obs.get("tick"),
            "products": rows,
            "scope": "machine output in the capped chest of each product",
        }

    def _node_recipes(self, state, row):
        by_position = {(e["position"]["x"], e["position"]["y"]): e.get("recipe") for e in state["plan"]["entities"]}
        return [by_position.get((n["machine"]["x"], n["machine"]["y"])) for n in row["nodes"]]

    # feeding
    def _active_products(self, obs, state):
        """Products still fed: no producing permanent row for them."""
        items = []
        for row in state["products"]:
            permanent = self.rows_stock(obs, row["item"]) if self.rows_stock is not None else None
            if permanent is None or permanent.get("status") != "producing":
                items.append(row)
        return items

    def feed_targets(self, obs, state, *, minimum=0, demand=None, contested=frozenset()):
        """``[(deficit, item, chest position)]`` from the demand of each fed product, largest first.

        need = max(minimum, demand - product stock - carried) product units,
        demand = the whole upcoming demand (pre-production), or only the steps
        in flight for a ``contested`` plate; target = min(FEED_CAP, ceil(need x
        raw per unit)) per input chest and item.
        """
        only = None if demand is None else set(demand)  # an explicit demand feeds only its products
        upcoming = self.demand(obs) if demand is None else demand
        current = self.current_demand(obs) if demand is None and contested else upcoming
        chest = self._chest_name(state)
        carried = obs.get("inventory") or {}
        rows = []
        for row in self._active_products(obs, state):
            if only is not None and row["item"] not in only:
                continue
            box = self._find(obs, chest, row["output"])
            stock = int(((box or {}).get("inventory") or {}).get(row["item"], 0)) + int(carried.get(row["item"], 0))
            for node in row["nodes"]:
                if not node.get("chest"):
                    continue
                inside = (self._find(obs, chest, node["chest"]) or {}).get("inventory") or {}
                for item, per_unit in node["raw"].items():
                    wanted = (current if item in contested else upcoming).get(row["item"], 0)
                    need = max(minimum, wanted - stock)
                    target = min(FEED_CAP, math.ceil(need * per_unit - 1e-9))
                    deficit = target - int(inside.get(item, 0))
                    if deficit > 0 and deficit >= min(FEED_BATCH, target):
                        rows.append((deficit, item, node["chest"]))
        return sorted(rows, key=lambda r: (-r[0], r[1], r[2]["x"], r[2]["y"]))

    def feed_action(self, obs, *, reserve, acquire=False, collect=False, minimum=0, demand=None):
        """One insert into the hungriest input chest, or the collection that precedes it, or None."""
        state = self._live(obs)
        if not state or state.get("phase") in ("retiring", "retired"):
            return None
        chest = self._chest_name(state)
        contested = self.contested_plates(obs) if demand is None else set()
        for deficit, item, position in self.feed_targets(
            obs, state, minimum=minimum, demand=demand, contested=contested
        ):
            held = int((obs.get("inventory") or {}).get(item, 0)) - reserve
            if held >= min(deficit, FEED_BATCH):
                return {
                    "type": "insert",
                    "name": chest,
                    "position": dict(position),
                    "inventory": "chest",
                    "item": item,
                    "count": min(deficit, held),
                    "reason": f"feed the supply cell {item}",
                }
            if acquire:
                return self.bootstrap.ensure_item(obs, item, reserve + deficit)
            if collect and item not in contested:  # never collect plates a waiting job needs
                taken = self.bootstrap._take_output(obs, item, deficit)
                if taken is not None:
                    return taken
        return None

    def upkeep(self, obs):
        """Shared-upkeep feed: only plates beyond FEED_RESERVE in the character's hands."""
        state = self._live(obs)
        if state.get("phase") != "verified":
            return None
        return self.feed_action(obs, reserve=FEED_RESERVE)

    def idle_feed(self, obs):
        """Idle lane: may also collect furnace plates (batched) to feed the cell."""
        state = self._live(obs)
        if state.get("phase") != "verified":
            return None
        return self.feed_action(obs, reserve=0, collect=True)

    # construction stock hook
    def stock(self, obs, item):
        """``machine_stock`` hook result for ``item`` (see ``machine_stock``), or None when the cell does not make it.

        producing: collect, wait above HANDCRAFT_LIMIT. starved (+ ``feed``): no
        plates inside and no recent progress. slow: the time rule prefers
        hand crafting. stalled/broken: hand crafting resumes.
        """
        state = self._live(obs)
        row = next((r for r in state.get("products", []) if r["item"] == item), None)
        if row is None or state.get("phase") == "retired":
            return None
        if state.get("phase") not in ("verified", "retiring"):
            return {"status": "building", "item": item}
        chest = self._chest_name(state)
        box = self._find(obs, chest, row["output"])
        machines = [self._find(obs, MACHINE, node["machine"]) for node in row["nodes"]]
        if box is None or any(m is None for m in machines):
            return {"status": "broken", "item": item, "reason": "supply cell chest or machine missing"}
        stock = int((box.get("inventory") or {}).get(item, 0))
        finished = sum(int(m.get("products_finished") or 0) for m in machines)
        idle = self._idle_ticks(obs, state, finished)
        result = {
            "item": item,
            "stock": stock,
            "chest": {"name": chest, "position": dict(row["output"])},
            # time rule inputs for ensure_item (construction_supply._waiting_pays)
            "wait_seconds_per_unit": round(60.0 / row["units_per_minute"], 4),
            "hand_seconds_per_unit": row["hand_seconds_per_unit"],
            "wait_factor": WAIT_FACTOR,
        }
        if stock > 0:
            return dict(result, status="producing")
        if state.get("phase") == "retiring":
            return dict(result, status="stalled")
        fed = all(
            int(((self._find(obs, chest, node["chest"]) or {}).get("inventory") or {}).get(raw, 0)) >= math.ceil(per)
            for node in row["nodes"]
            if node.get("chest")
            for raw, per in node["raw"].items()
        )
        if not fed and idle > PROGRESS_WRITE_TICKS:
            missing = max(PROOF_UNITS, HANDCRAFT_LIMIT + 1)
            feed = self.feed_action(obs, reserve=0, demand={item: missing}, minimum=missing)
            return dict(result, status="starved", **({"feed": feed} if feed else {}))
        if idle > STALL_TICKS:
            return dict(result, status="stalled", ticks_without_progress=idle)
        return dict(result, status="producing", ticks_without_progress=idle)

    def intermediate_take(self, obs, item, missing):
        """Before hand crafting ``missing`` ``item``: a batched collection of a direct ingredient the cell stocks, or None.

        Only the ingredients the craft still lacks are taken (recipe amount x
        runs - carried), through ``_take_output`` (batch rule, owned input
        chests excluded), so the craft queue only does the final assembly.
        """
        state = self._live(obs)
        if state.get("phase") not in ("verified", "retiring") or missing < 1:
            return None
        recipe = self.catalog.recipe_for_product(item)
        if not recipe:
            return None
        runs = math.ceil(missing / _out_amount(recipe, item))
        stocked = {row["item"]: row for row in state["products"]}
        carried = obs.get("inventory") or {}
        chest = self._chest_name(state)
        for row in recipe.get("ingredients") or ():
            name = row.get("name")
            if row.get("type", "item") != "item" or name == item or name not in stocked:
                continue
            lacking = math.ceil(row["amount"] * runs) - int(carried.get(name, 0))
            box = self._find(obs, chest, stocked[name]["output"])
            if lacking < 1 or int(((box or {}).get("inventory") or {}).get(name, 0)) < 1:
                continue
            taken = self.bootstrap._take_output(obs, name, lacking)
            if taken is not None:
                return taken
        return None

    def _idle_ticks(self, obs, state, finished):
        tick = int(obs.get("tick") or 0)
        seen = state.get("progress") or {}
        key = ",".join(r["item"] for r in state["products"])
        last = seen.get(key)
        if last is None or finished > last["finished"]:
            if last is None or tick - last.get("written", -PROGRESS_WRITE_TICKS) >= PROGRESS_WRITE_TICKS:
                state.setdefault("progress", {})[key] = {"finished": finished, "tick": tick, "written": tick}
                self._save(state)
            return 0
        return max(0, tick - last["tick"])

    # retirement
    def _retire(self, obs):
        state = self.state()
        if not state:
            return {"status": "succeeded", "reason": "no supply cell to retire", "evidence": {"tick": obs.get("tick")}}
        if not self._live(obs):
            return {"status": "blocked", "reason": "supply_cell_world_or_catalog_changed"}
        if state.get("phase") != "retired":
            if state.get("phase") != "retiring":
                state["phase"] = "retiring"  # feeding stops now
                state["retiring_tick"] = obs.get("tick")
                self._save(state)
            products = {r["item"] for r in state["products"]}
            dependent = [
                job.get("key")
                for job in self._jobs(obs)
                if job.get("capability") != CAPABILITY
                and job.get("status") in ("running", "waiting", "pending")
                and any(f"machine-made {p}" in str(job.get("reason", "")) for p in products)
            ]
            if dependent:
                return {
                    "status": "waiting",
                    "reason": "supply_cell_retire_waits_for_dependent_jobs",
                    "evidence": {"jobs": dependent},
                }
            draining = self._plates_inside(obs, state)
            waited = int(obs.get("tick") or 0) - int(state.get("retiring_tick") or obs.get("tick") or 0)
            if draining and waited < DRAIN_TICKS:
                return {
                    "status": "waiting",
                    "reason": "supply_cell_retire_drains_input_chests",
                    "evidence": {"plates": draining, "ticks_left": DRAIN_TICKS - waited},
                }
            for e in self._demolition_order(state):
                live = self._find(obs, e["name"], e["position"])
                if live is None or type(live.get("unit_number")) is not int:
                    continue
                return {
                    "type": "mine",
                    "name": e["name"],
                    "position": dict(e["position"]),
                    "count": 1,
                    "expected_entity_unit": live["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "demolish the retired temporary supply cell",
                }
            board = Board(self.game, self.builder, self.catalog)
            board.refresh(force=True)
            if board.state.get("world_id") == obs["world_id"] and BLOCK in board.state.get("blocks", {}):
                del board.state["blocks"][BLOCK]
                _atomic_json(board.path, board.state)
            state["phase"] = "retired"
            state["retired_tick"] = obs.get("tick")
            self._save(state)
        evidence = self.retire_proof(obs)
        if evidence is None:
            return {"status": "waiting", "reason": "supply_cell_demolition_not_observed_yet"}
        return {
            "status": "succeeded",
            "reason": "temporary supply cell demolished and its block released",
            "evidence": evidence,
        }

    def _plates_inside(self, obs, state):
        """Raw items still in the input chests of machines that may use them (verified cells only)."""
        if not state.get("bars"):
            return {}
        chest = self._chest_name(state)
        left = {}
        for row in state["products"]:
            for node in row["nodes"]:
                if not node.get("chest"):
                    continue
                inside = (self._find(obs, chest, node["chest"]) or {}).get("inventory") or {}
                for raw in node["raw"]:
                    if int(inside.get(raw, 0)) > 0:
                        left[raw] = left.get(raw, 0) + int(inside[raw])
        return left

    @staticmethod
    def _demolition_order(state):
        rank = {INSERTER: 0, MACHINE: 1}
        inside = [e for e in state["plan"]["entities"] if e["name"] != POLE]
        return sorted(inside, key=lambda e: rank.get(e["name"], 2))

    def retire_proof(self, obs):
        state = self.state()
        if not state:
            return {"tick": obs.get("tick"), "retired": "never built"}
        if state.get("phase") != "retired" or state.get("world_id") != obs.get("world_id"):
            return None
        left = [e["name"] for e in self._demolition_order(state) if self._find(obs, e["name"], e["position"])]
        board = Board(self.game, self.builder, self.catalog)
        if left or BLOCK in board.state.get("blocks", {}):
            return None
        return {
            "tick": obs.get("tick"),
            "retired_tick": state.get("retired_tick"),
            "products": [r["item"] for r in state["products"]],
        }


def chain_stock(rows_hook, cell_hook):
    """The ``machine_stock`` hook: a producing ``produce`` row wins, else the supply cell, else the row answer."""

    def hook(observation, item):
        row = rows_hook(observation, item) if rows_hook is not None else None
        if row is not None and row.get("status") == "producing":
            return row
        cell = cell_hook(observation, item)
        return cell if cell is not None else row

    return hook


def validate_parameters(parameters):
    """``{"products": [1..6 distinct item names]}`` to build, ``{"retire": true}`` to demolish."""
    if parameters == {"retire": True}:
        return
    if not isinstance(parameters, dict) or set(parameters) != {"products"}:
        raise ValueError("supply_cell takes products (list of items) or retire: true")
    products = parameters["products"]
    if (
        not isinstance(products, list)
        or not 1 <= len(products) <= MAX_PRODUCTS
        or len(set(products)) != len(products)
        or not all(isinstance(p, str) and 0 < len(p) <= 64 for p in products)
    ):
        raise ValueError("supply_cell products must be 1..6 distinct item names")


# ----- planner -------------------------------------------------------------
def _rows(jobs, queue_nodes):
    return [r for r in list(jobs) + list(queue_nodes) if r.get("capability") == CAPABILITY]


def _abandoned(job, tick):
    """A build job that ended blocked or failed and was not retried for ABANDON_TICKS."""
    if job.get("status") not in ("blocked", "failed"):
        return False
    since = job.get("blocked_at_tick", job.get("last_tick"))
    return isinstance(since, (int, float)) and isinstance(tick, (int, float)) and tick - since >= ABANDON_TICKS


def _retire_node(reason):
    return {
        "id": RETIRE_NODE_ID,
        "capability": CAPABILITY,
        "parameters": {"retire": True},
        "priority": RETIRE_PRIORITY,
        "depends_on": [],
        "reason": reason,
    }


def supply_cell_nodes(jobs, queue_nodes, observation, capabilities):
    """Build once when products qualify; retire once when demand ended, a produce row took over or the build was abandoned."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    found = observation.get("supply_cell_candidates")
    if not isinstance(found, dict):
        return []
    mine = _rows(jobs, queue_nodes)
    if not mine:
        if MACHINE not in _enabled(observation) or not found.get("products"):
            return []
        products = list(found["products"])[:MAX_PRODUCTS]
        rows = found.get("rows") or {}
        note = ", ".join(f"{p} {rows.get(p, {}).get('count', '?')}" for p in products)
        return [
            {
                "id": NODE_ID,
                "capability": CAPABILITY,
                "parameters": {"products": products},
                "priority": BUILD_PRIORITY,
                "depends_on": [],
                "reason": f"temporary hand-fed supply cell for upcoming hand crafts: {note}"[:300],
            }
        ]
    if any((r.get("parameters") or {}).get("retire") for r in mine):
        return []
    builds = [j for j in jobs if j.get("capability") == CAPABILITY]
    built = [j for j in builds if j.get("status") == "succeeded"]
    if not built:
        if builds and all(_abandoned(j, observation.get("tick")) for j in builds):
            return [_retire_node("supply cell build stayed blocked: demolish what stands and release the block")]
        return []
    products = (built[0].get("parameters") or {}).get("products") or []
    demand = found.get("demand") or {}
    permanent = {
        (j.get("parameters") or {}).get("item")
        for j in jobs
        if j.get("capability") == "produce" and j.get("status") == "succeeded"
    }
    if not all(demand.get(p, 0) <= HANDCRAFT_LIMIT or p in permanent for p in products):
        return []
    return [_retire_node("supply cell products have a permanent source or no remaining demand: drain and demolish")]
