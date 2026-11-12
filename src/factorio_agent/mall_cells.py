"""Compile small item recipe trees into powered, capped direct-insertion cells."""

from __future__ import annotations
from copy import deepcopy
import math

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .routing import VECTORS
from .smelting import port
from .transport import xy
from .compact_green import _transport, _spread

PRODUCTS = ("underground-belt", "splitter", "assembling-machine-1", "inserter")
RAW = {"iron-plate", "copper-plate"}
INTERMEDIATES = {"iron-gear-wheel", "transport-belt", "electronic-circuit", "copper-cable"}
FAST_BUDGET = 120


def recipe_tree(catalog, product):
    if product not in PRODUCTS:
        raise ValueError("supported local recipe-tree mall product required")
    recipes = {}
    needs = {}
    raw = {}

    def walk(item, amount, trail=()):
        if item in RAW:
            raw[item] = raw.get(item, 0) + amount
            return
        if item in trail or (item != product and item not in INTERMEDIATES):
            raise ValueError("unsupported or cyclic local construction recipe")
        r = catalog.recipes[item]
        y = _yield(r["products"][0]) if len(r["products"]) == 1 else 0
        if (
            type(y) not in (int, float)
            or not math.isfinite(y)
            or y <= 0
            or y % 1
            or r["products"][0]["name"] != item
            or r["products"][0].get("type", "item") != "item"
            or r.get("category") not in catalog.entities["assembling-machine-1"]["crafting_categories"]
            or type(r.get("energy")) not in (int, float)
            or not math.isfinite(r["energy"])
            or r["energy"] <= 0
            or any(
                q.get("type", "item") != "item"
                or type(q.get("amount")) not in (int, float)
                or not math.isfinite(q["amount"])
                or q["amount"] <= 0
                for q in r["ingredients"]
            )
        ):
            raise ValueError("ordinary deterministic native item recipes required")
        recipes[item] = {"recipe": r, "yield": y}
        needs[item] = needs.get(item, 0) + amount
        for q in r["ingredients"]:
            walk(q["name"], amount * q["amount"] / y, trail + (item,))

    walk(product, 1)
    children = lambda item: sorted(q["name"] for q in recipes[item]["recipe"]["ingredients"] if q["name"] not in RAW)
    roots = children(product)
    if not 1 <= len(roots) <= 2:
        raise ValueError("cell supports one or two direct local ingredient branches")
    roots.sort(key=lambda name: ("electronic-circuit" == name, name))
    positions = {product: 0}
    edges = []
    for side, root in zip((-1, 1), roots):
        parent = product
        node = root
        for depth in (1, 2):
            if node in positions:
                raise ValueError("shared local dependency requires an explicit different cell")
            positions[node] = side * 4 * depth
            edges.append((node, parent))
            next_ = children(node)
            if not next_:
                break
            if len(next_) != 1 or depth == 2:
                raise ValueError("direct branch depth exceeds bounded cell")
            parent, node = node, next_[0]
    if set(positions) != set(recipes):
        raise ValueError("recipe DAG did not fit the complete local cell")
    return recipes, needs, raw, positions, edges


def recipe_rates(catalog, product, target):
    recipes, needs, raw, positions, edges = recipe_tree(catalog, product)
    speed = catalog.entities["assembling-machine-1"]["crafting_speed"]
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
        raise ValueError("positive native machine speed required")
    limits = [
        dict(kind="machine", recipe=n, products_per_minute=60 * speed / r["recipe"]["energy"] * r["yield"] / needs[n])
        for n, r in recipes.items()
    ]
    machine_rate = min(q["products_per_minute"] for q in limits)
    for child, parent in edges:
        limits.append(
            dict(kind="direct_inserter", recipe=parent, item=child, products_per_minute=FAST_BUDGET / needs[child])
        )
    for name, row in recipes.items():
        for q in row["recipe"]["ingredients"]:
            if q["name"] in RAW:
                amount = needs[name] * q["amount"] / row["yield"]
                limits.append(
                    dict(kind="raw_inserter", recipe=name, item=q["name"], products_per_minute=FAST_BUDGET / amount)
                )
    limits.append(dict(kind="output_inserter", recipe=product, products_per_minute=FAST_BUDGET))
    rate = min(q["products_per_minute"] for q in limits)
    batch_raw = {}
    cycles = {}
    excess = {}

    def batch(item, amount):
        if item in RAW:
            batch_raw[item] = batch_raw.get(item, 0) + amount
            return
        row = recipes[item]
        runs = math.ceil(amount / row["yield"])
        cycles[item] = cycles.get(item, 0) + runs
        excess[item] = excess.get(item, 0) + runs * row["yield"] - amount
        for q in row["recipe"]["ingredients"]:
            batch(q["name"], runs * q["amount"])

    batch(product, target)
    return dict(
        nameplate_per_minute=rate,
        machine_nameplate_per_minute=machine_rate,
        raw_per_product=raw,
        raw_nameplate_per_minute={k: v * rate for k, v in raw.items()},
        target_items=target,
        raw_per_target_fill=batch_raw,
        continuous_fill_lower_bound={k: v * target for k, v in raw.items()},
        minimum_batch_cycles=cycles,
        minimum_batch_excess={k: v for k, v in excess.items() if v},
        recipes=recipes,
        capacity_limits=limits,
        scope="catalog machines and fast-inserter limits; empty-cell minimum recipe batches plus separately observed WIP, no sustained upstream rate",
    )


def recipe_cell(catalog, product, x, y, target):
    if any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 != 0.5 for v in (x, y)):
        raise ValueError("half-tile mall anchor required")
    recipes, needs, raw, columns, links = recipe_tree(catalog, product)
    rated = recipe_rates(catalog, product, target)
    es = []
    arms = []
    connections = []
    raw_feeds = []
    direct = []
    ports = []
    machines = {}

    def add(name, dx, dy, d=0, **kw):
        q = dict(entity(name, x + dx, y + dy, d), **kw)
        es.append(q)
        return q

    def arm(dx, dy, d, item, source, dest):
        q = add("fast-inserter", dx, dy, d)
        arms.append(q)
        vx, vy = VECTORS[d]
        connections.append(
            dict(
                arm=q,
                pickup_position=dict(x=q["position"]["x"] + vx, y=q["position"]["y"] + vy),
                drop_position=dict(x=q["position"]["x"] - vx, y=q["position"]["y"] - vy),
                item=item,
                source=source,
                destination=dest,
            )
        )
        return q

    for name, dx in sorted(columns.items(), key=lambda q: q[1]):
        machines[name] = add("assembling-machine-1", dx, 0, recipe=name)
    for child, parent in links:
        cx, px = columns[child], columns[parent]
        d = 12 if cx < px else 4
        q = arm((cx + px) / 2, 0, d, child, machines[child], machines[parent])
        direct.append(dict(source=machines[child], destination=machines[parent], arm=q, item=child))
    west = min(columns.values()) - 2
    east = max(columns.values()) + 2
    iron_nodes = [
        (name, dx)
        for name, dx in columns.items()
        if any(q["name"] == "iron-plate" for q in recipes[name]["recipe"]["ingredients"])
    ]
    iron = [add("transport-belt", dx, -3, 4) for dx in range(west, max(dx for _, dx in iron_nodes) + 1)]
    ports.append(port("iron-plate", x + west, y - 3, 4))
    for name, dx in iron_nodes:
        q = arm(dx, -2, 0, "iron-plate", None, machines[name])
        raw_feeds.append(dict(item="iron-plate", arm=q, machine=machines[name], belts=iron))
    copper_nodes = [
        (name, dx)
        for name, dx in columns.items()
        if any(q["name"] == "copper-plate" for q in recipes[name]["recipe"]["ingredients"])
    ]
    if copper_nodes:
        if len(copper_nodes) != 1:
            raise ValueError("cell requires one bounded local copper receiver")
        name, dx = copper_nodes[0]
        copper = [add("transport-belt", xx, 7, 4 if xx < dx else 0) for xx in range(west, dx + 1)]
        copper += [add("transport-belt", dx, dy, 0) for dy in range(6, 2, -1)]
        ports.append(port("copper-plate", x + west, y + 7, 4))
        q = arm(dx, 2, 8, "copper-plate", None, machines[name])
        raw_feeds.append(dict(item="copper-plate", arm=q, machine=machines[name], belts=copper))
    chest = add("iron-chest", 0, 3)
    output = arm(0, 2, 0, product, machines[product], chest)
    poles = [add("small-electric-pole", dx, dy) for dy in (-2, 2) for dx in range(west, east + 1, 4)]
    spine = sorted(
        poles,
        key=lambda q: (q["position"]["y"], q["position"]["x"] if q["position"]["y"] == y - 2 else -q["position"]["x"]),
    )
    wires = [dict(color="red", a=a, b=b) for a, b in zip(spine, spine[1:])]
    for q in list(machines.values()) + arms + [chest]:
        wires.append(dict(color="red", a=q, b=min(poles, key=lambda p: math.dist(xy(p), xy(q)))))
    future_x = max(columns.values()) + 8
    future = [
        entity("assembling-machine-1", x + future_x, y, recipe=product),
        entity("iron-chest", x + future_x, y + 3),
    ]
    access = [entity("transport-belt", x, y + dy) for dy in (4, 5)]
    outlet = dict(
        kind="item", item=product, direction="output", name="iron-chest", position=deepcopy(chest["position"])
    )
    return _plan(
        es,
        ports + [outlet],
        machines=list(machines.values()),
        final_machine=machines[product],
        arms=arms,
        input_arms=[q for q in arms if q != output],
        output_arm=output,
        chest=chest,
        raw_feeds=raw_feeds,
        direct_edges=direct,
        arm_connections=connections,
        product=product,
        rated=rated,
        target=target,
        bar_slots=math.ceil(target / catalog.items[product]["stack_size"]),
        output_access=access,
        expansion_keepouts=future,
        reserved_area=[[x + west - 0.5, y - 4.5], [x + future_x + 2.5, y + 8.5]],
        poles=poles,
        wire_edges=wires,
        output_arms=[output],
        global_arms=arms,
        input_buffers=[],
        feeders=[],
        recipe_inputs={n: [q["name"] for q in r["recipe"]["ingredients"]] for n, r in recipes.items()},
        component_layout="two-direct-recipe-branches-v1",
        flow="iron EAST on north header; copper EAST then NORTH locally; direct intermediates; output SOUTH",
        growth="EAST into reserved second-final slot; keep both input ports and output chest fixed",
        layout_policy="circuit-limited-recipe-tree-mall-v1",
    )


def audit_recipe_cell(core):
    errors = []
    received = {q["recipe"]: set() for q in core["machines"]}

    def inside(p, q):
        return abs(p["x"] - q["position"]["x"]) <= 1 and abs(p["y"] - q["position"]["y"]) <= 1

    if len(core["arm_connections"]) != len(core["arms"]) or any(
        q["arm"] not in core["arms"] or q["arm"] not in core["entities"] for q in core["arm_connections"]
    ):
        errors.append("unbound_or_missing_ordinary_arm")
    belts, graph = _transport(core["entities"], [])
    lanes = {(p, side): set() for p in belts for side in ("left", "right")}
    for p in core["ports"]:
        if p.get("direction") == "input" and xy(p) in belts:
            for side in ("left", "right"):
                lanes[xy(p), side].add(p["item"])
    _spread(lanes, graph)
    # Validate ordinary physical endpoints against the declared complete recipe tree.
    for edge in core["arm_connections"]:
        q = edge["arm"]
        vx, vy = VECTORS[q["direction"]]
        p = q["position"]
        pickup = dict(x=p["x"] + vx, y=p["y"] + vy)
        drop = dict(x=p["x"] - vx, y=p["y"] - vy)
        if pickup != edge["pickup_position"] or drop != edge["drop_position"]:
            errors.append("ordinary_inserter_vector_mismatch")
        source, dest = edge["source"], edge["destination"]
        if source is None:
            feed = next((r for r in core["raw_feeds"] if r["arm"] == q), None)
            if not feed or not any(xy(b) == (pickup["x"], pickup["y"]) for b in feed["belts"]):
                errors.append("raw_pickup_missing")
            if any(lanes.get(((pickup["x"], pickup["y"]), side)) != {edge["item"]} for side in ("left", "right")):
                errors.append("raw_header_lane_not_connected_to_typed_input")
        elif not inside(pickup, source) or source.get("recipe") != edge["item"]:
            errors.append("direct_pickup_not_product_machine")
        if dest["name"] == "assembling-machine-1":
            if not inside(drop, dest):
                errors.append("ingredient_drop_misses_machine")
            received[dest["recipe"]].add(edge["item"])
        elif drop != dest["position"] or edge["item"] != core["product"]:
            errors.append("final_output_misses_fixed_chest")
    for name, items in core["recipe_inputs"].items():
        if received[name] != set(items):
            errors.append("missing_or_wrong_recipe_ingredient:" + name)
    return errors


def _key(q):
    return q["name"] + ":" + str(q["position"]["x"]) + ":" + str(q["position"]["y"])
