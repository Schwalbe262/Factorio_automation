"""Fixed-port assembly cells, real splitter fanout and catalog demand math."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from factorio_agent.core.deterministic_builder import _plan
from .board import entity
from .smelting import port, merge_entities
from .power_grid import plan_connection as power_connection


def recipe_cell(x, y, recipe, ingredients, *, split_output=False):
    if not 1 <= len(ingredients) <= 3:
        raise ValueError("assembly cell accepts one to three item ingredients")
    es = [
        entity("assembling-machine-1", x, y, recipe=recipe),
        entity("fast-inserter", x + 2, y, 12),
        entity("small-electric-pole", x - 2, y - 2),
        entity("small-electric-pole", x + 2, y + 2),
    ]
    inputs = []
    for item, (dx, dy, direction) in zip(ingredients, ((-1, 0, 12), (0, -1, 0), (0, 1, 8))):
        es.append(entity("fast-inserter", x + 2 * dx, y + 2 * dy, direction))
        facing = (direction + 8) % 16
        es += [entity("transport-belt", x + n * dx, y + n * dy, facing) for n in (3, 4)]
        inputs.append(port(item, x + 4 * dx, y + 4 * dy, facing))
    es += [entity("transport-belt", x + 3, y, 4), entity("transport-belt", x + 4, y, 4)]
    outputs = [port(recipe, x + 4, y, 4, "output")]
    if split_output:
        es += [
            dict(entity("splitter", x + 5, y + 0.5, 4), _width=2, _height=1),
            entity("transport-belt", x + 6, y, 4),
            entity("transport-belt", x + 6, y + 1, 4),
        ]
        outputs = [port(recipe, x + 6, y + dy, 4, "output") for dy in (0, 1)]
    return _plan(
        es,
        inputs + outputs,
        recipe=recipe,
        template="three_side_assembly_cell",
        reserved_area=[[x - 5.5, y - 5.5], [x + 7.5, y + 6.5]],
    )


def fanout(x, y, item, count):
    if not 2 <= count <= 6:
        raise ValueError("fanout supports two to six branches")
    es = [entity("transport-belt", x, y, 4)]
    outputs = []
    for i in range(count - 1):
        dx = 3 * i
        es += [
            dict(entity("splitter", x + dx + 1, y + 0.5, 4), _width=2, _height=1),
            entity("transport-belt", x + dx + 2, y, 4),
            entity("transport-belt", x + dx + 2, y + 1, 4),
        ]
        outputs.append(port(item, x + dx + 2, y + 1, 4, "output"))
        if i < count - 2:
            es.append(entity("transport-belt", x + dx + 3, y, 4))
    outputs.append(port(item, x + 3 * (count - 2) + 2, y, 4, "output"))
    return _plan(es, [port(item, x, y, 4)] + outputs, template="native_splitter_fanout")


def science_consumer_row(x, y, count):
    es = []
    for i in range(count):
        cx = x + 4 * i
        es += [
            entity("assembling-machine-1", cx, y, recipe="logistic-science-pack"),
            entity("inserter", cx, y - 2, 0),
            entity("inserter", cx, y + 2, 0),
            entity("small-electric-pole", cx + 2, y),
        ]
    length = 4 * (count - 1) + 3
    es += [entity("transport-belt", x + dx, y - 3, 4) for dx in range(-4, length)]
    es += [entity("transport-belt", x + dx, y + 3, 4) for dx in range(length)]
    es += [entity("transport-belt", x - 2, y - dy, 8) for dy in (5, 4)]
    es += [entity("transport-belt", x - 3, y - dy, 0) for dy in (1, 2)]
    return _plan(
        es,
        [
            port("inserter", x - 2, y - 5, 8),
            port("transport-belt", x - 3, y - 1, 0),
            port("logistic-science-pack", x + length - 1, y + 3, 4, "output"),
        ],
        count=count,
        template="mixed_lane_green_science_row",
        reserved_area=[[x - 4.5, y - 6.5], [x + length - 0.5, y + 4.5]],
    )


GREEN_CORE_DEPTH = 48  # narrow core: plate edge (west) to far edge (east)
GREEN_CORE_LENGTH = 54  # along the plate edge (53 used, even so a site fit can turn it)


def green_core(x, y, count, *, narrow=False):
    """Green science core; plates enter on the west edge, packs leave east.

    The reserved area is ``[x - 10.5, x + 43.5) x [y - 10.5, y + 42.5)``
    (54 x 53). ``narrow`` moves the inserter cell 4 tiles west, directly
    behind the circuit cell, which trims the reservation to
    ``[x - 10.5, x + 37.5) x [y - 10.5, y + 43.5)`` (``GREEN_CORE_DEPTH`` x
    ``GREEN_CORE_LENGTH``): 48 tiles from the plate edge, the depth of a
    master plan district strip. The internal routes stay inside either
    reservation.
    """
    inserters_dx = 28 if narrow else 32
    cells = {
        "gears": recipe_cell(x, y, "iron-gear-wheel", ["iron-plate"], split_output=True),
        "belts": recipe_cell(x + 24, y, "transport-belt", ["iron-gear-wheel", "iron-plate"]),
        "wire": recipe_cell(x, y + 16, "copper-cable", ["copper-plate"]),
        "circuits": recipe_cell(x + 16, y + 16, "electronic-circuit", ["copper-cable", "iron-plate"]),
        "inserters": recipe_cell(
            x + inserters_dx, y + 16, "inserter", ["electronic-circuit", "iron-gear-wheel", "iron-plate"]
        ),
        "green": science_consumer_row(x + 8, y + 36, count),
        "iron": fanout(x - 8, y - 8, "iron-plate", 4),
    }

    def ip(cell, item):
        return next(p for p in cells[cell]["ports"] if p["item"] == item and p["direction"] == "input")

    def op(cell, index=0):
        return [p for p in cells[cell]["ports"] if p["direction"] == "output"][index]

    pairs = [
        (op("wire"), ip("circuits", "copper-cable")),
        (op("circuits"), ip("inserters", "electronic-circuit")),
        (op("gears"), ip("belts", "iron-gear-wheel")),
        (op("gears", 1), ip("inserters", "iron-gear-wheel")),
        (op("belts"), ip("green", "transport-belt")),
        (op("inserters"), ip("green", "inserter")),
    ]
    for i, cell in enumerate(("gears", "belts", "circuits", "inserters")):
        pairs.append((op("iron", i), ip(cell, "iron-plate")))
    return {
        "cells": cells,
        "entities": merge_entities(*(c["entities"] for c in cells.values())),
        "connections": pairs[6:] + pairs[:6],
        "ports": [ip("iron", "iron-plate"), ip("wire", "copper-plate"), op("green")],
        "reserved_area": [[x - 10.5, y - 10.5], [x + 37.5, y + 43.5]]
        if narrow
        else [[x - 10.5, y - 10.5], [x + 43.5, y + 42.5]],
    }


def lab_green_entry(lab_plan):
    labs = [e for e in lab_plan["entities"] if e["name"] == "lab"]
    if not labs or lab_plan["ports"][0]["facing"] != 12:
        raise ValueError("westbound lab row required")
    y = labs[0]["position"]["y"]
    x = max(e["position"]["x"] for e in labs) + 2
    es = [
        entity("transport-belt", x, y + 3, 0),
        dict(entity("underground-belt", x, y + 2, 0), belt_to_ground_type="input"),
        dict(entity("underground-belt", x, y - 1, 0), belt_to_ground_type="output"),
        entity("transport-belt", x, y - 2, 0),
    ]
    return {
        "join": {"x": x, "y": y - 3},
        "port": port("logistic-science-pack", x, y + 3, 0),
        "entities": es,
        "pair": {"input": [x, y + 2], "output": [x, y - 1], "direction": 0, "span": 3},
    }


def lab_green_entry_outside(lab_plan):
    """``lab_green_entry`` with its receiving port moved outside the lab block.

    A plan-site science block is a registered board reservation: a belt
    route may only end at a port outside it (``transport._production_keepouts``),
    and red science's own route runs up the first column east of the block
    into the lab belt head. Below the labs the chain doglegs one column west
    and back, then tunnels east under that column (one underground, gap 3)
    to the port 5 tiles east of the head, outside the block; the last tiles
    are the old south side-load, so green still rides the free lane.
    """
    entry = lab_green_entry(lab_plan)
    x, y = entry["join"]["x"], entry["join"]["y"] + 3
    es = entry["entities"] + [
        entity("transport-belt", x, y + 4, 0),
        entity("transport-belt", x - 1, y + 4, 4),
        entity("transport-belt", x - 1, y + 5, 0),
        dict(entity("underground-belt", x, y + 5, 12), belt_to_ground_type="output"),
        dict(entity("underground-belt", x + 4, y + 5, 12), belt_to_ground_type="input"),
        entity("transport-belt", x + 5, y + 5, 12),
    ]
    return {
        "join": entry["join"],
        "port": port("logistic-science-pack", x + 5, y + 5, 12),
        "entities": es,
        "pairs": [entry["pair"], {"input": [x + 4, y + 5], "output": [x, y + 5], "direction": 12, "span": 4}],
    }


def raw_demand(catalog, item, rate, *, leaves=("iron-plate", "copper-plate")):
    result = Counter()
    machines = Counter()

    def visit(name, quantity, stack):
        if name in leaves:
            result[name] += quantity
            return
        if name in stack:
            raise ValueError("cyclic recipe demand")
        recipe = catalog.recipe_for_product(name)
        if not recipe or any(p.get("type", "item") != "item" for p in recipe["ingredients"]):
            raise ValueError("unsupported intermediate " + name)
        amount = sum(p["amount"] for p in recipe["products"] if p["name"] == name and p.get("probability", 1) == 1)
        if not amount:
            raise ValueError("deterministic product yield required")
        cycles = quantity / amount
        machines[name] += cycles * recipe["energy"] / 60 / catalog.entities["assembling-machine-1"]["crafting_speed"]
        for p in recipe["ingredients"]:
            visit(p["name"], cycles * p["amount"], (*stack, name))

    visit(item, rate, ())
    return {"raw_items_per_minute": dict(result), "machine_equivalents": dict(machines)}


def connect_power_groups(game, builder, obs, groups, entities):
    """Plan wire connectivity incrementally; native power is proved after build."""
    virtual = deepcopy(obs)
    result = []
    networks = {
        e.get("electric_network_id") for e in obs["entities"] if e["name"] == "steam-engine" and e.get("energy", 0) > 0
    }
    if not networks:
        return {"ok": False, "reason": "no_supplied_pole_network"}
    network = next(iter(networks))
    for group in groups:
        poles = [e for e in group["entities"] if e["name"] == "small-electric-pole"]
        if not poles:
            continue
        bridge = power_connection(
            game, builder, virtual, poles[0]["position"], [e for e in entities if e["name"] != "small-electric-pole"]
        )
        if not bridge.get("ok"):
            return bridge
        result = merge_entities(result, bridge["entities"])
        virtual["entities"] += [dict(e, electric_network_id=network) for e in bridge["entities"] + poles]
    return {"ok": True, "entities": result}
