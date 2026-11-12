"""Deterministic, native-geometry green science block for shared plate buses.

This is a pure blueprint compiler and verifier; it never changes a live world.
All inserters use the ordinary prototype pickup/drop positions.  The only
undergrounds are three explicit, straight pairs under unrelated local belts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from types import SimpleNamespace

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from .board import entity
from .manufacturing import raw_demand
from .smelting import port
from .transport import xy


VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
PINNED_RECIPES = {
    "logistic-science-pack": (6, {"transport-belt": 1, "inserter": 1}, 1),
    "transport-belt": (.5, {"iron-plate": 1, "iron-gear-wheel": 1}, 2),
    "inserter": (.5, {"iron-plate": 1, "iron-gear-wheel": 1, "electronic-circuit": 1}, 1),
    "iron-gear-wheel": (.5, {"iron-plate": 2}, 1),
    "electronic-circuit": (.5, {"iron-plate": 1, "copper-cable": 3}, 1),
    "copper-cable": (.5, {"copper-plate": 1}, 2),
}


def compact_green_core(x, y):
    """Compile seven AM1 science machines and five shared intermediates.

    ``x,y`` is a tile-center anchor, not the north-west reservation corner.
    Inputs: copper(x-3,y-2) E; iron(x-3,y+1) E.  Output: (x+26,y+19) E.
    Reservation: 32 x 28; occupied envelope: 30 x 22.  No external routing or
    electrical-grid connection is included in this pure production block.
    """
    es = []

    def add(name, dx, dy, direction=0, **extra):
        spec = entity(name, x+dx, y+dy, direction)
        spec.update(extra)
        es.append(spec)

    def belt(dx, dy, direction):
        add("transport-belt", dx, dy, direction)

    def arm(dx, dy, direction, fast=True):
        add("fast-inserter" if fast else "inserter", dx, dy, direction)

    # Each intermediate has a shared plate input or a direct machine transfer.
    for dx, recipe in ((2, "transport-belt"), (8, "iron-gear-wheel"),
                       (14, "inserter"), (18, "electronic-circuit"),
                       (22, "copper-cable")):
        add("assembling-machine-1", dx, 4, recipe=recipe)
    for dx in (2, 8, 14, 18):
        arm(dx, 2, 0)                 # Shared eastbound iron, north of machines.
    arm(24, 4, 4)                     # Copper -> cable from the east.
    arm(20, 4, 4)                     # Cable -> circuit, direct insertion.
    arm(16, 4, 4)                     # Circuit -> inserter, direct insertion.
    arm(6, 4, 4)                      # Gear west output -> belt assembler branch.
    arm(8, 6, 0)                      # Gear south output -> inserter branch.
    arm(2, 6, 8)
    arm(14, 6, 8)
    arm(0, 4, 4)                      # Belt product output.
    arm(12, 4, 4)                     # Inserter product output.

    for dx in range(-3, 23):
        belt(dx, 1, 4)
    for dx in range(-3, 26):
        belt(dx, -2, 4 if dx < 25 else 8)
    for dy in (-1, 3, 4):
        belt(25, dy, 8)

    # Gear extraction is genuine demand fanout: one branch per consumer.
    for dy in range(4, 8):
        belt(5, dy, 8 if dy < 7 else 12)
    for dx in (4, 3, 2):
        belt(dx, 7, 12)
    for dx in range(8, 15):
        belt(dx, 7, 4)

    # Belt product approaches the mixer from its north side.
    for dy in (4, 5, 6, 7, 8, 12):
        belt(-1, dy, 8)
    # Inserter product approaches from the south, before ALL seven pickups.
    for dy in (4, 5, 9, 10):
        belt(11, dy, 8 if dy < 10 else 12)
    for dx in range(10, -4, -1):
        belt(dx, 10, 12 if dx > -3 else 8)
    for dy in range(11, 15):
        belt(-3, dy, 8 if dy < 14 else 4)
    belt(-2, 14, 0)
    for dx in range(-2, 26):
        belt(dx, 13, 4)

    pairs = []
    for dx, first, last in ((25, 0, 2), (11, 6, 8), (-1, 9, 11)):
        for dy, role in ((first, "input"), (last, "output")):
            add("underground-belt", dx, dy, 8, belt_to_ground_type=role)
        pairs.append({"input": [x+dx, y+first], "output": [x+dx, y+last],
                      "direction": 8, "span": last-first})

    for dx in range(1, 26, 4):
        add("assembling-machine-1", dx, 16, recipe="logistic-science-pack")
        arm(dx, 14, 0, False)
        arm(dx, 18, 0, False)
        add("small-electric-pole", dx+2, 16)
    for dx in range(1, 27):
        belt(dx, 19, 4)
    for dx, dy in ((0, 2), (0, 6), (6, 2), (6, 6), (12, 2),
                   (16, 6), (20, 2), (24, 6), (12, 10)):
        add("small-electric-pole", dx, dy)

    return _plan(es, [port("iron-plate", x-3, y+1, 4),
                      port("copper-plate", x-3, y-2, 4),
                      port("logistic-science-pack", x+26, y+19, 4, "output")],
                 template="compact_green_shared_bus_v1", target_rate_per_minute=30,
                 rated_rate_per_minute=35, underground_pairs=pairs,
                 reserved_area=[[x-4.5, y-3.5], [x+27.5, y+24.5]],
                 extension_slots=[{"purpose": "future_3x3_machine", "position": {"x": x+dx, "y": y+23},
                                   "reserved_only": True} for dx in (3, 9, 15, 21)],
                 mixed_feed_lanes={"left": "transport-belt", "right": "inserter"},
                 power_port={"position": {"x": x, "y": y+2}, "name": "small-electric-pole"})


def _recipe_catalog(catalog=None):
    if catalog is not None:
        return catalog
    recipes = {name: {"energy": energy,
                      "ingredients": [{"name": k, "amount": v} for k, v in ingredients.items()],
                      "products": [{"name": name, "amount": amount}]}
               for name, (energy, ingredients, amount) in PINNED_RECIPES.items()}
    return SimpleNamespace(recipes=recipes, entities={"assembling-machine-1": {"crafting_speed": .5}},
                           recipe_for_product=recipes.get)


def _transport(es, pairs):
    """Return a lane-preserving physical graph; mouths pair exactly as declared."""
    belts = {xy(e): e for e in es if e["name"] in ("transport-belt", "underground-belt")}
    jumps = {tuple(p["input"]): tuple(p["output"]) for p in pairs}
    graph = {}; outputs = {}; incoming = defaultdict(list)
    for p, e in belts.items():
        if e.get("belt_to_ground_type") == "input":
            q = jumps.get(p)
        else:
            vx, vy = VECTORS[e["direction"]]; q = p[0]+vx, p[1]+vy
        outputs[p] = q
        if q in belts and (e["direction"]-belts[q]["direction"]) % 16 != 8:
            incoming[q].append(e["direction"])
    for p, e in belts.items():
        q = outputs[p]
        other = belts.get(q)
        if other is None:
            continue
        if e["direction"] == other["direction"]:
            graph[p] = (q, {"left": "left", "right": "right"})
        elif (e["direction"]-other["direction"]) % 16 != 8:
            # A sole perpendicular feed bends an ordinary belt. Native curves
            # carry BOTH lanes through the bend; they do not merge both onto a
            # single lane. An inline tail or two opposite perpendicular feeds
            # keeps the recipient straight and produces a real side-load.
            if other["name"] == "transport-belt" and len(set(incoming[q])) == 1:
                graph[p] = (q, {"left": "left", "right": "right"})
                continue
            vx, vy = VECTORS[other["direction"]]
            # A perpendicular feeder occupies the recipient's approached side.
            lane = "left" if (p[0]-q[0])*vy-(p[1]-q[1])*vx > 0 else "right"
            graph[p] = (q, {"left": lane, "right": lane})
    return belts, graph


def _spread(lanes, graph):
    changed = True
    while changed:
        changed = False
        for p, (q, mapping) in graph.items():
            for before, after in mapping.items():
                source, dest = lanes[p, before], lanes[q, after]
                if not source.issubset(dest):
                    dest.update(source)
                    changed = True


def _machine_at(machines, p):
    return next((e for e in machines if abs(xy(e)[0]-p[0]) <= 1 and abs(xy(e)[1]-p[1]) <= 1), None)


def audit_compact_green(plan, catalog=None):
    """Prove prospective geometry, native mouths, raw-rooted item/lane paths.

    Static capacity is a necessary commissioning gate, never a measured rate.
    Conservative inserter budgets are 120 items/min fast and 30 items/min yellow;
    the 105 cable/min direct transfer must therefore use a fast inserter.
    """
    errors = []
    es = plan["entities"]
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    boxes = [geometry._occupied_by_plan([e]) for e in es]
    if sum(map(len, boxes)) != len(set().union(*boxes)):
        errors.append("prospective_footprint_overlap")
    area = plan["reserved_area"]
    if any(not (area[0][0] <= px-.5 and px+.5 <= area[1][0] and
                area[0][1] <= py-.5 and py+.5 <= area[1][1]) for box in boxes for px, py in box):
        errors.append("entity_outside_reservation")
    for slot in plan.get("extension_slots", []):
        box = geometry._occupied_by_plan([dict(slot, name="assembling-machine-1")])
        if any(box & other for other in boxes):
            errors.append("extension_slot_occupied")
    belts, graph = _transport(es, plan.get("underground_pairs", []))
    declared = set()
    for pair in plan.get("underground_pairs", []):
        a, b = tuple(pair["input"]), tuple(pair["output"])
        direction = pair["direction"]
        vx, vy = VECTORS[direction]
        span = pair["span"]
        if not 2 <= span <= 5 or b != (a[0]+span*vx, a[1]+span*vy):
            errors.append("invalid_underground_span")
        for p, role in ((a, "input"), (b, "output")):
            declared.add(p)
            e = belts.get(p, {})
            if e.get("name") != "underground-belt" or e.get("direction") != direction or e.get("belt_to_ground_type") != role:
                errors.append("missing_or_wrong_underground_mouth")
        for p in ((a[0]-vx, a[1]-vy), (b[0]+vx, b[1]+vy)):
            if belts.get(p, {}).get("direction") != direction:
                errors.append("underground_requires_straight_approach_and_exit")
        # Native matching must encounter this output before another same-axis mouth.
        for p, e in belts.items():
            if p in (a, b) or e["name"] != "underground-belt" or e["direction"] != direction:
                continue
            ahead = (p[0]-a[0])*vx+(p[1]-a[1])*vy
            if (p[0]-a[0])*vy == (p[1]-a[1])*vx and 0 < ahead <= span:
                errors.append("underground_pair_can_be_stolen")
    if {p for p, e in belts.items() if e["name"] == "underground-belt"} != declared:
        errors.append("undeclared_underground_mouth")

    catalog = _recipe_catalog(catalog)
    machines = [e for e in es if e["name"] == "assembling-machine-1"]
    arms = [e for e in es if e["name"] in ("inserter", "fast-inserter")]
    poles = [xy(e) for e in es if e["name"] == "small-electric-pole"]
    for e in machines+arms:
        if not any(abs(xy(e)[0]-px) <= 2.5 and abs(xy(e)[1]-py) <= 2.5 for px, py in poles):
            errors.append("unpowered_consumer_position:"+str(xy(e)))
    connected = {poles[0]} if poles else set()
    while True:
        more = {p for p in poles if any((p[0]-q[0])**2+(p[1]-q[1])**2 <= 7.5**2 for q in connected)}
        if more <= connected:
            break
        connected.update(more)
    if len(connected) != len(poles):
        errors.append("disconnected_internal_poles")

    lanes = defaultdict(set)
    for p in plan["ports"]:
        if p["direction"] == "input":
            for lane in ("left", "right"):
                lanes[xy(p), lane].add(p["item"])
    supplied = defaultdict(set)
    ready = set()
    arm_items = defaultdict(set)
    # Outputs become reachable only after ALL of that machine's ingredients.
    for _ in range(len(machines)+2):
        _spread(lanes, graph)
        for arm in arms:
            p = xy(arm)
            vx, vy = VECTORS[arm["direction"]]
            pickup, drop = (p[0]+vx, p[1]+vy), (p[0]-vx, p[1]-vy)
            source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
            items = ({source["recipe"]} if source is not None and xy(source) in ready else set())
            if pickup in belts:
                items |= lanes[pickup, "left"] | lanes[pickup, "right"]
            if dest is not None:
                ingredients = {i["name"] for i in catalog.recipes[dest["recipe"]]["ingredients"]}
                items &= ingredients
                supplied[xy(dest)].update(items)
            elif drop in belts:
                dx, dy = VECTORS[belts[drop]["direction"]]
                side = (p[0]-drop[0])*dy-(p[1]-drop[1])*dx
                lane = "right" if side > 0 else "left"
                lanes[drop, lane].update(items)
            arm_items[p].update(items)
        ready.update(xy(m) for m in machines if
                     {i["name"] for i in catalog.recipes[m["recipe"]]["ingredients"]} <= supplied[xy(m)])
    _spread(lanes, graph)
    for m in machines:
        missing = {i["name"] for i in catalog.recipes[m["recipe"]]["ingredients"]} - supplied[xy(m)]
        if missing:
            errors.append("unreachable_ingredients:"+str(xy(m))+":"+",".join(sorted(missing)))
        if m["recipe"] == "logistic-science-pack":
            pickup = xy(m)[0], xy(m)[1]-3
            for lane, item in plan["mixed_feed_lanes"].items():
                if lanes[pickup, lane] != {item}:
                    errors.append("science_input_lane_mismatch:"+str(pickup)+":"+lane)
    output = next(p for p in plan["ports"] if p["direction"] == "output")
    if "logistic-science-pack" not in lanes[xy(output), "left"] | lanes[xy(output), "right"]:
        errors.append("science_output_unreachable")
    for m in machines:
        sinks = {xy(output)} if m["recipe"] == "logistic-science-pack" else set()
        starts = set()
        direct = False
        for arm in arms:
            p = xy(arm)
            vx, vy = VECTORS[arm["direction"]]
            pickup, drop = (p[0]+vx, p[1]+vy), (p[0]-vx, p[1]-vy)
            source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
            if source is m:
                starts.add(drop)
                if dest is not None and m["recipe"] in {i["name"] for i in catalog.recipes[dest["recipe"]]["ingredients"]}:
                    direct = True
            if dest is not None and m["recipe"] in {i["name"] for i in catalog.recipes[dest["recipe"]]["ingredients"]}:
                sinks.add(pickup)
        reached = set(starts)
        while True:
            more = {graph[p][0] for p in reached if p in graph}
            if more <= reached:
                break
            reached.update(more)
        if not direct and not reached & sinks:
            errors.append("machine_output_unconnected:"+str(xy(m)))

    rate = plan["rated_rate_per_minute"]
    demand = raw_demand(catalog, "logistic-science-pack", rate)
    counts = Counter(m["recipe"] for m in machines)
    product_rates = Counter({"logistic-science-pack": rate})

    def visit(item, quantity):
        recipe = catalog.recipe_for_product(item)
        if item in ("iron-plate", "copper-plate"):
            return
        amount = sum(p["amount"] for p in recipe["products"] if p["name"] == item)
        for ingredient in recipe["ingredients"]:
            needed = quantity*ingredient["amount"]/amount
            product_rates[ingredient["name"]] += needed
            visit(ingredient["name"], needed)

    visit("logistic-science-pack", rate)
    for recipe, equivalent in demand["machine_equivalents"].items():
        if equivalent > counts[recipe]+1e-9:
            errors.append("machine_capacity_insufficient:"+recipe)
    # Every individual supply inserter carries only its destination's recipe demand.
    for arm in arms:
        p = xy(arm)
        vx, vy = VECTORS[arm["direction"]]
        pickup, drop = (p[0]+vx, p[1]+vy), (p[0]-vx, p[1]-vy)
        source, dest = _machine_at(machines, pickup), _machine_at(machines, drop)
        if dest is not None:
            recipe = catalog.recipes[dest["recipe"]]
            amount = sum(o["amount"] for o in recipe["products"] if o["name"] == dest["recipe"])
            load = sum(i["amount"] for i in recipe["ingredients"] if i["name"] in arm_items[p])
            load *= product_rates[dest["recipe"]]/counts[dest["recipe"]]/amount
        elif source is not None:
            load = product_rates[source["recipe"]]/counts[source["recipe"]]
        else:
            errors.append("inserter_has_no_machine_endpoint:"+str(p))
            continue
        limit = 120 if arm["name"] == "fast-inserter" else 30
        if load > limit+1e-9:
            errors.append("inserter_capacity_insufficient:"+str(p))
    for items in lanes.values():
        if sum(product_rates[item] for item in items) > 450:
            errors.append("yellow_belt_lane_capacity_insufficient")
    return {"ok": not errors, "errors": sorted(set(errors)), "raw_items_per_minute": demand["raw_items_per_minute"],
            "machine_equivalents": demand["machine_equivalents"], "machines_ready": len(ready),
            "machine_count": len(machines), "normal_belts": sum(e["name"] == "transport-belt" for e in es),
            "underground_pairs": len(plan.get("underground_pairs", [])), "entities": len(es),
            "reservation_width": area[1][0]-area[0][0], "reservation_height": area[1][1]-area[0][1]}


def audit_lab_injection(lab_plan, entry):
    """Check every actual lab pickup is downstream of the injection port."""
    es = lab_plan["entities"]+entry["entities"]
    _, graph = _transport(es, [entry["pair"]])
    lanes = defaultdict(set)
    for lane in ("left", "right"):
        lanes[xy(entry["port"]), lane].add("logistic-science-pack")
    _spread(lanes, graph)
    labs = [e for e in es if e["name"] == "lab"]
    reached = set()
    for arm in es:
        if arm["name"] not in ("inserter", "fast-inserter"):
            continue
        p = xy(arm)
        vx, vy = VECTORS[arm["direction"]]
        pickup, drop = (p[0]+vx, p[1]+vy), (p[0]-vx, p[1]-vy)
        lab = _machine_at(labs, drop)
        if lab is not None and "logistic-science-pack" in lanes[pickup, "left"] | lanes[pickup, "right"]:
            reached.add(xy(lab))
    return {"ok": len(reached) == len(labs) and bool(labs), "labs": len(labs), "labs_reached": len(reached)}


def preflight_compact_green(game, builder, obs, plan, catalog=None):
    """Read-only audit and native survey, including the reserved expansion area."""
    from .planning import preflight, survey_slots

    audit = audit_compact_green(plan, catalog)
    if not audit["ok"]:
        return {"ok": False, "reason": "compact_green_audit_failed", "audit": audit}
    slots = survey_slots(game, [[plan["reserved_area"]]])
    if not slots.get("ok") or 1 not in slots.get("legal", []):
        return {"ok": False, "reason": "compact_green_reservation_unavailable", "audit": audit}
    return dict(preflight(game, builder, obs, plan["entities"]), audit=audit)
