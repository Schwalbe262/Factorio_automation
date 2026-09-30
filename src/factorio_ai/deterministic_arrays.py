"""Graph-first, belt-fed arrays. Pure planning; no world mutation or model calls.

Each item has a shared southbound spine. Producers precede consumers, and
horizontal connectors cross foreign spines through explicit underground pairs.
Capacity is a gate. Geometry search never purchases a throughput shortfall.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math

from .deterministic_layout_metrics import layout_metrics
from .deterministic_machine_ports import ARM_BUDGETS
from .deterministic_production import ProductionGraph
from .factory_templates import DIRECTIONS
from .world_catalog import CatalogError, _amount, _yield


RAW_ITEMS = {"iron-plate", "copper-plate", "coal", "stone", "wood", "stone-brick"}
MACHINES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
FURNACES = {"stone-furnace", "steel-furnace"}
PRODUCERS = MACHINES | FURNACES


def plan_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def production_demand(catalog, observation: dict, targets: dict[str, float], boundary=RAW_ITEMS) -> dict:
    """Aggregate shared ingredients before rounding machine counts.

Fluid recipes are explicit external boundary producers, owned by the existing
fluid executor. Their rates are retained; they are never hand-waved away.
"""
    graph = ProductionGraph(catalog)
    order, recipes, seen, visiting = [], {}, set(), set()
    external = set(boundary)

    def visit(item):
        if item in visiting:
            raise CatalogError("array production dependency cycle: " + item)
        if item in seen:
            return
        seen.add(item)
        recipe = catalog.recipe_for_product(item)
        if item in external or recipe is None or any(row.get("type", "item") == "fluid"
                for row in recipe["ingredients"] + recipe["products"]):
            external.add(item)
            return
        if not observation.get("enabled_recipes", {}).get(recipe["name"]):
            raise CatalogError("array recipe is locked: " + recipe["name"])
        if len(recipe["products"]) != 1 or recipe["products"][0].get("probability", 1) != 1:
            raise CatalogError("array recipe requires a coproduct/stochastic executor: " + recipe["name"])
        visiting.add(item)
        for row in recipe["ingredients"]:
            visit(row["name"])
        visiting.remove(item)
        recipes[item] = recipe
        order.append(item)

    for item, rate in sorted(targets.items()):
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("array target rates must be finite and positive")
        visit(item)
    rates = defaultdict(float, targets)
    selected = {}
    for item in reversed(order):
        recipe = recipes[item]
        candidates = [m for m in graph.machines_for_recipe(recipe["name"], observation) if m["name"] in PRODUCERS]
        if not candidates:
            raise CatalogError("array has no supported unlocked machine: " + item)
        selected[item] = candidates[0]
        crafts = rates[item] / _yield(recipe["products"][0])
        for row in recipe["ingredients"]:
            rates[row["name"]] += crafts * _amount(row["amount"])
        if selected[item]["burner"]:
            watts = float(selected[item]["energy_usage_per_tick"]) * 60
            fuel = float(catalog.items.get("coal", {}).get("fuel_value", 0))
            if not fuel > 0 or not watts > 0:
                raise CatalogError("array furnace requires live fuel and energy data")
            rates["coal"] += crafts * float(recipe["energy"]) / selected[item]["crafting_speed"] * watts / fuel
            external.add("coal")
    nodes = []
    for item in order:
        recipe = recipes[item]
        inputs = {r["name"]: _amount(r["amount"]) for r in recipe["ingredients"]}
        machine = selected[item]
        if machine["burner"]:
            inputs["coal"] = (float(recipe["energy"]) / machine["crafting_speed"] *
                              machine["energy_usage_per_tick"] * 60 / catalog.items["coal"]["fuel_value"])
        if not inputs or len(inputs) > 3:
            raise CatalogError("array requires one to three solid inputs: " + item)
        output = _yield(recipe["products"][0])
        arm = "fast-inserter" if observation.get("enabled_recipes", {}).get("fast-inserter") else "inserter"
        limit = min(machine["crafting_speed"] * 60 / float(recipe["energy"]),
                    ARM_BUDGETS[arm] / output, *(ARM_BUDGETS[arm] / n for n in inputs.values()))
        if not math.isfinite(limit) or limit <= 0:
            raise CatalogError("array has invalid nominal machine capacity: " + item)
        count = math.ceil(rates[item] / (limit * output) - 1e-9)
        if count > 64:
            raise CatalogError("array exceeds 64 machines per process: " + item)
        nodes.append({"item": item, "recipe": recipe["name"], "machine": machine["name"],
                      "count": count, "rate": rates[item], "per_machine_capacity": limit * output,
                      "inputs": inputs, "output_amount": output, "arm": arm})
    return {"nodes": nodes, "rates": dict(sorted(rates.items())),
            "external_rates": {item: rates[item] for item in sorted(external) if rates[item] > 0},
            "targets": dict(sorted(targets.items()))}


def _entity(name, x, y, direction=0, **fields):
    return {"name": name, "position": {"x": x + .5, "y": y + .5}, "direction": direction, **fields}


def _footprint(entity, catalog):
    prototype = catalog.entities.get(entity["name"], {})
    box = prototype.get("selection_box") or prototype.get("collision_box")
    if box:
        width = math.ceil(box["right_bottom"]["x"] - box["left_top"]["x"])
        height = math.ceil(box["right_bottom"]["y"] - box["left_top"]["y"])
    else:
        width = height = 3 if entity["name"] in MACHINES or entity["name"] == "lab" else 2 if entity["name"] in FURNACES else 1
    if entity.get("direction", 0) in (4, 12):
        width, height = height, width
    p = entity["position"]
    return (p["x"] - width / 2, p["y"] - height / 2, p["x"] + width / 2, p["y"] + height / 2)


def validate_array(plan: dict, catalog, geometry: dict) -> dict:
    """Check physical collisions, directed material paths, arm endpoints and power."""
    errors, occupied, at, rectangles = [], {}, {}, {}
    entities = plan["entities"]
    for index, entity in enumerate(entities):
        p = entity["position"]
        key = (p["x"], p["y"])
        if key in at:
            errors.append({"rule": "entity_collision", "position": p})
        at[key] = entity
        rect = _footprint(entity, catalog)
        rectangles[key] = rect
        for x in range(math.floor(rect[0]), math.ceil(rect[2])):
            for y in range(math.floor(rect[1]), math.ceil(rect[3])):
                if (x, y) in occupied:
                    errors.append({"rule": "footprint_collision", "position": p})
                occupied[x, y] = index
    belts = {p: e for p, e in at.items() if e["name"].endswith(("transport-belt", "underground-belt"))}
    edges = defaultdict(set)
    incoming = defaultdict(set)
    for p, belt in belts.items():
        dx, dy = DIRECTIONS[belt["direction"]]
        target = (p[0] + dx, p[1] + dy)
        if belt.get("belt_to_ground_type") == "input":
            candidates = [(i, (p[0] + dx * i, p[1] + dy * i))
                          for i in range(1, int(geometry["underground_distance"]) + 1)]
            pair = next((point for _, point in candidates if point in belts
                         and belts[point]["name"] == belt["name"]
                         and belts[point]["direction"] == belt["direction"]
                         and belts[point].get("belt_to_ground_type") == "output"), None)
            if pair is None:
                errors.append({"rule": "underground_pair_missing", "position": belt["position"]})
                continue
            target = pair
        if target in belts:
            if belts[target].get("_item") != belt.get("_item"):
                errors.append({"rule": "mixed_item_merge", "position": belt["position"]})
            else:
                edges[p].add(target)
                incoming[target].add(p)
    poles = [p for p, e in at.items() if e["name"] == "small-electric-pole"]
    supply = float(geometry["pole_supply"])
    wire = float(geometry["pole_wire"])
    powered = set()
    if poles:
        powered.add(poles[0])
        frontier = [poles[0]]
        while frontier:
            p = frontier.pop()
            for q in poles:
                if q not in powered and math.dist(p, q) <= wire:
                    powered.add(q)
                    frontier.append(q)
    if len(powered) != len(poles):
        errors.append({"rule": "disconnected_power_plan"})
    machine_inputs = defaultdict(set)
    for p, entity in at.items():
        if entity["name"] in MACHINES | {"lab", "inserter", "fast-inserter"}:
            rect = rectangles[p]
            if not any(min(rect[2], q[0] + supply) > max(rect[0], q[0] - supply)
                       and min(rect[3], q[1] + supply) > max(rect[1], q[1] - supply) for q in powered):
                errors.append({"rule": "no_power_plan", "position": entity["position"]})
        if entity["name"] not in ARM_BUDGETS:
            continue
        dx, dy = DIRECTIONS[entity["direction"]]
        pickup, drop = (p[0] + dx, p[1] + dy), (p[0] - dx, p[1] - dy)
        def endpoint(point):
            if point in belts:
                return point
            return next((q for q, r in rectangles.items()
                         if at[q]["name"] in PRODUCERS | {"lab"}
                         and r[0] <= point[0] < r[2] and r[1] <= point[1] < r[3]), None)
        source, target = endpoint(pickup), endpoint(drop)
        if source is None or target is None:
            errors.append({"rule": "inserter_endpoint", "position": entity["position"]})
            continue
        for point in (source, target):
            if point in belts and belts[point].get("_item") != entity.get("_item"):
                errors.append({"rule": "inserter_item_mismatch", "position": entity["position"]})
        edges[source].add(target)
        incoming[target].add(source)
        if entity.get("_role") == "input":
            machine_inputs[target].add(entity.get("_item"))
    raw_ports = {(p["position"]["x"], p["position"]["y"]) for p in plan["ports"] if p["direction"] == "input"}
    for p, entity in at.items():
        if entity["name"] not in ARM_BUDGETS or entity.get("_role") != "input":
            continue
        dx, dy = DIRECTIONS[entity["direction"]]
        start = (p[0] + dx, p[1] + dy)
        seen, frontier, found = set(), [start], False
        while frontier:
            q = frontier.pop()
            if q in seen:
                continue
            seen.add(q)
            if q in raw_ports or (q in at and at[q]["name"] in PRODUCERS):
                found = True
                break
            frontier.extend(incoming[q])
        if not found:
            errors.append({"rule": "no_source", "position": entity["position"], "item": entity.get("_item")})
    for node in plan["demand"]["nodes"]:
        actual = [e for e in entities if e["name"] == node["machine"]
                  and e.get("recipe", e.get("_array_recipe")) == node["recipe"]]
        if len(actual) != node["count"]:
            errors.append({"rule": "machine_count_mismatch", "item": node["item"]})
        for machine in actual:
            p = machine["position"]
            provided = machine_inputs[p["x"], p["y"]]
            if provided != set(node["inputs"]):
                errors.append({"rule": "recipe_input_missing", "position": p})
        if node["count"] * node["per_machine_capacity"] + 1e-9 < node["rate"]:
            errors.append({"rule": "rate_shortfall", "item": node["item"]})
    return {"ok": not errors, "errors": errors[:20], "error_count": len(errors),
            "flow_verified": False, "capacity_basis": "conservative single-item arm budgets plus live machine speed"}


def compile_array(catalog, demand: dict, geometry: dict, *, pitch=6, gap=3, raw_order=None,
                  labs: list[str] | None = None, lab_count=1) -> dict:
    """Place all production and shared connections together before any reservation."""
    if (pitch < 6 or gap < 3 or type(lab_count) is not int
            or not (1 if labs else 0) <= lab_count <= 64):
        raise ValueError("invalid array geometry or lab count")
    nodes = demand["nodes"]
    raw = list(raw_order or sorted(demand["external_rates"], key=lambda k: (-demand["rates"][k], k)))
    if set(raw) != set(demand["external_rates"]) or len(raw) != len(set(raw)):
        raise ValueError("raw spine order must contain each external item once")
    taps = Counter(item for node in nodes for item in node["inputs"])
    items = raw + sorted((node["item"] for node in nodes), key=lambda item: (-taps[item], -demand["rates"][item], item))
    columns = {item: -5 - gap * i for i, item in enumerate(items)}
    entities, ports, spans, rows = {}, [], defaultdict(list), []

    def add(entity):
        p = entity["position"]
        key = p["x"], p["y"]
        if key in entities and entities[key] != entity:
            raise ValueError(f"array connector collision at {key}: {entities[key]['name']}/{entity['name']}")
        entities[key] = entity

    def belt(x, y, direction, item, **fields):
        add(_entity("transport-belt", x, y, direction, _item=item, **fields))

    def horizontal(start, end, y, item):
        direction = 4 if end >= start else 12
        step = 1 if direction == 4 else -1
        tunnels = {}
        for other, col in columns.items():
            first, last = lane_spans[other]
            if first - 1 <= y <= last + 1 and min(start, end) < col < max(start, end):
                tunnels[col - step] = "input"
                tunnels[col + step] = "output"
        x = start
        while True:
            if x in tunnels:
                add(_entity("underground-belt", x, y, direction, _item=item, belt_to_ground_type=tunnels[x]))
            elif not any(col == x and lane_spans[other][0] - 1 <= y <= lane_spans[other][1] + 1
                         for other, col in columns.items()) or x == end:
                belt(x, y, 8 if x == end and x in columns.values() else direction, item)
            if x == end:
                break
            x += step

    current = 0
    for node in nodes:
        for i in range(node["count"]):
            y = current + pitch * i
            rows.append((y, node))
        current += pitch * (node["count"] - 1) + 10
    if labs:
        if len(labs) > 3 or not set(labs).issubset(items):
            raise ValueError("array labs require one to three planned science inputs")
        for i in range(lab_count):
            rows.append((current + pitch * i, {"item": None, "machine": "lab", "arm": "inserter",
                                              "inputs": dict.fromkeys(labs, 1)}))
    for y, node in rows:
        slots = (-1, 0) if node["machine"] in FURNACES else (-1, 0, 1)
        for slot, item in zip(slots, sorted(node["inputs"])):
            spans[item].append(y + slot)
        if node["item"]:
            spans[node["item"]].append(y + (2 if node["machine"] in FURNACES else 3))
    lane_spans = {item: (min(spans[item]) - (2 if item in raw else 0),
                        max(spans[item]) + (0 if item in raw else 2)) for item in items}
    for y, node in rows:
        furnace = node["machine"] in FURNACES
        machine = _entity(node["machine"], -.5 if furnace else 0, y - .5 if furnace else y)
        if node.get("recipe"):
            machine["_array_recipe" if furnace else "recipe"] = node["recipe"]
        add(machine)
        slots = (-1, 0) if furnace else (-1, 0, 1)
        for slot, item in zip(slots, sorted(node["inputs"])):
            col = columns[item]
            add(_entity(node["arm"], -2, y + slot, 12, _item=item, _role="input"))
            add(_entity(node["arm"], col + 1, y + slot, 12, _item=item, _role="tap"))
            add(_entity("small-electric-pole", col + 1, y - 2 if slot <= 0 else y + 2))
            horizontal(col + 2, -3, y + slot, item)
        if node["item"]:
            item = node["item"]
            add(_entity(node["arm"], 0, y + (1 if furnace else 2), 0, _item=item, _role="output"))
            horizontal(0, columns[item], y + (2 if furnace else 3), item)
        # Cover machine arms, then bridge the distant tap poles without occupying
        # any vertical material spine. Both are checked by the static gate.
        add(_entity("small-electric-pole", -2, y - 2))
        for pole_x in range(-2, min(columns.values()), -6):
            if pole_x in columns.values():
                pole_x += 1
            add(_entity("small-electric-pole", pole_x, y + (3 if furnace else 2)))
    for item, col in columns.items():
        if not spans[item]:
            continue
        start, end = min(spans[item]), max(spans[item])
        if item in raw:
            start -= 2
        else:
            end += 2
        name = "transport-belt"
        capacity = float(geometry["belt_speed"]) * 4 * 3600  # one conservatively usable lane
        if demand["rates"][item] > capacity + 1e-9:
            if not geometry.get("fast_belt_enabled") or demand["rates"][item] > geometry.get("fast_belt_speed", 0) * 4 * 3600 + 1e-9:
                raise ValueError("array shared spine needs an unlocked faster belt: " + item)
            name = "fast-transport-belt"
        for y in range(start, end + 1):
            entity = _entity(name, col, y, 8, _item=item)
            key = entity["position"]["x"], entity["position"]["y"]
            if key in entities and entities[key]["name"] == "transport-belt" and entities[key].get("_item") == item:
                entities.pop(key)
            add(entity)
        if item in raw:
            ports.append({"kind": "item", "item": item, "direction": "input",
                          "position": {"x": col + .5, "y": start + .5}, "facing": 8,
                          "rate_per_minute": demand["external_rates"][item]})
        else:
            ports.append({"kind": "item", "item": item, "direction": "output",
                          "position": {"x": col + .5, "y": end + .5}, "facing": 8,
                          "rate_per_minute": demand["targets"].get(item, 0),
                          "gross_rate_per_minute": demand["rates"][item]})
    result = {"ok": True, "schema_version": 2, "template": "shared-spine-array",
              "entities": list(entities.values()), "ports": ports, "demand": deepcopy(demand),
              "lab_inputs": list(labs or []), "lab_count": lab_count if labs else 0,
              "geometry": {"pitch": pitch, "gap": gap, "raw_order": raw},
              "required_items": dict(sorted(Counter(e["name"] for e in entities.values()).items()))}
    result["validation"] = validate_array(result, catalog, geometry)
    result["ok"] = result["validation"]["ok"]
    return result


def optimize_array(catalog, observation, targets, geometry, *, boundary=RAW_ITEMS,
                   labs=None, lab_count=1, rounds=6) -> dict:
    if type(rounds) is not int or not 0 <= rounds <= 6:
        raise ValueError("array optimization allows zero through six rounds")
    demand = production_demand(catalog, observation, targets, boundary)
    order = sorted(demand["external_rates"], key=lambda k: (-demand["rates"][k], k))
    cache, history = {}, []
    def evaluate(cfg):
        key = (cfg["pitch"], cfg["gap"], tuple(cfg["raw_order"]))
        if key not in cache:
            try:
                plan = compile_array(catalog, demand, geometry, labs=labs, lab_count=lab_count, **cfg)
                if plan["ok"]:
                    metrics = layout_metrics(plan["entities"], catalog)
                    score = (metrics["construction"]["raw_item_units"], metrics["area"], key)
                    cache[key] = score, plan, metrics
                else:
                    cache[key] = None
            except ValueError:
                cache[key] = None
        return cache[key]
    # Start with feasible, expanded spacing, then squeeze only through the gate.
    cfg = {"pitch": 8, "gap": 4, "raw_order": order}
    best = evaluate(cfg)
    if best is None:
        cfg = {**cfg, "pitch": 6, "gap": 3}
        best = evaluate(cfg)
    if best is None:
        raise ValueError("no array baseline passes static geometry/capacity gates")
    history.append({"round": 0, "score": best[0][:2]})
    for round_index in range(1, rounds + 1):
        moves = [{**cfg, "pitch": cfg["pitch"] - 1}, {**cfg, "gap": cfg["gap"] - 1}]
        for i in range(len(order) - 1):
            alternative = list(cfg["raw_order"])
            alternative[i], alternative[i + 1] = alternative[i + 1], alternative[i]
            moves.append({**cfg, "raw_order": alternative})
        improved = []
        for alternative in moves[:32]:
            result = evaluate(alternative)
            if result and result[0] < best[0]:
                improved.append((result[0], alternative, result))
        if not improved:
            break
        _, cfg, best = min(improved, key=lambda row: row[0])
        history.append({"round": round_index, "score": best[0][:2]})
    result = deepcopy(best[1])
    result.update(metrics=best[2], optimization={"history": history, "candidates": len(cache),
                  "max_rounds": rounds, "max_candidates_per_round": 32})
    result["digest"] = plan_digest(result)
    return result


def translate_array(plan, anchor: dict, rotation=0):
    if rotation not in DIRECTIONS:
        raise ValueError("array rotation must be cardinal")
    result = deepcopy(plan)
    for obj in result["entities"] + result["ports"]:
        x, y = obj["position"]["x"] - .5, obj["position"]["y"] - .5
        for _ in range(rotation // 4):
            x, y = -y, x
        obj["position"] = {"x": anchor["x"] + x, "y": anchor["y"] + y}
        field = "facing" if "facing" in obj else "direction"
        obj[field] = (obj.get(field, 0) + rotation) % 16
    result["digest"] = plan_digest({k: v for k, v in result.items() if k != "digest"})
    return result
