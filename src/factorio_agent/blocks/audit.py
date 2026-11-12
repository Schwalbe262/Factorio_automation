"""Independent static audit of a recipe row plan; ``[]`` means no finding.

It reads only ``entities``, ``ports``, ``recipe`` and ``reserved_area`` of the
plan plus catalog data; it never trusts the generator's counters. Entity kinds
come from the catalog ``type`` field, footprints from collision boxes.

Conventions: inserter direction d picks up at ``pos + reach * VECTORS[d]``
and drops at ``pos - reach * VECTORS[d]`` (reach from ``params``); inserters
pick from either belt lane and drop on the far lane (``lanes.inserter_output_lane``).
Rate model: nameplate crafts/min = 60 * crafting_speed / energy; belt lane and
inserter items/min come from ``params`` (conservative belt->machine budgets).
A derated plan declares ``crafts_per_machine`` below nameplate: the audit
then checks every inserter, lane and output budget against that declared
rate (a claim it verifies, never a counter it trusts), and a declared rate
above nameplate is the finding ``declared_rate_above_nameplate``.
Power model: an electric entity is powered when any of its tiles overlaps a
pole's square supply area; poles must form one wire-connected network.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from ..lanes import inserter_output_lane
from .belts import LANES, belt_graph, belt_tiles, reachable, spread, straight_walk
from .geometry import footprint, step, xy
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, inserter_reach, lane_capacity, pole_radius, pole_reach


@dataclass(frozen=True)
class Arm:
    entity: dict
    pickup: tuple[float, float]
    drop: tuple[float, float]


@dataclass
class Scene:
    """Derived physical facts shared by the individual checks."""

    catalog: Any
    plan: dict
    limits: RowLimits
    machines: list[dict]
    tiles: dict
    arms: list[Arm]
    belts: dict
    graph: dict

    def machine_at(self, tile):
        return self.tiles.get(tile)

    def recipe(self, machine: dict) -> dict:
        return self.catalog.recipes.get(machine.get("recipe")) or {}

    def nameplate(self, machine: dict) -> float:
        energy = float(self.recipe(machine).get("energy") or 0)
        speed = float(self.catalog.entities[machine["name"]].get("crafting_speed") or 0)
        return 60 * speed / energy if energy > 0 else 0.0

    def crafts(self, machine: dict) -> float:
        """Declared (derated) crafts/minute when the plan has one, else nameplate."""
        nameplate = self.nameplate(machine)
        declared = self.plan.get("crafts_per_machine")
        if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0:
            return min(nameplate, float(declared))
        return nameplate

    def ports(self, direction: str) -> list[dict]:
        return [p for p in self.plan["ports"] if p.get("direction") == direction]


def _kind(catalog: Any, e: dict) -> str:
    return (catalog.entities.get(e["name"]) or {}).get("type", "")


def _scene(catalog: Any, plan: dict, limits: RowLimits) -> tuple[Scene, list[str]]:
    errors = []
    entities = plan["entities"]
    machines = [e for e in entities if _kind(catalog, e) == "assembling-machine"]
    tiles = {t: m for m in machines for t in footprint(catalog, m)}
    arms = []
    for e in entities:
        if _kind(catalog, e) != "inserter":
            continue
        try:
            reach = inserter_reach(catalog, e["name"], limits)
        except ValueError:
            errors.append("no_inserter_reach:" + e["name"])
            continue
        p = xy(e)
        arms.append(Arm(e, step(p, e["direction"], reach), step(p, (e["direction"] + 8) % 16, reach)))
    belts = belt_tiles(catalog, entities)
    return Scene(catalog, plan, limits, machines, tiles, arms, belts, belt_graph(belts)), errors


def _check_footprints(s: Scene) -> list[str]:
    errors = [f"unknown_entity:{e['name']}" for e in s.plan["entities"] if e["name"] not in s.catalog.entities]
    counts = Counter(t for e in s.plan["entities"] for t in footprint(s.catalog, e))
    errors += [f"footprint_overlap:{t}" for t, n in counts.items() if n > 1]
    (x0, y0), (x1, y1) = s.plan["reserved_area"]
    if any(not (x0 < t[0] < x1 and y0 < t[1] < y1) for t in counts):
        errors.append("entity_outside_reserved_area")
    for slot in s.plan.get("expansion_slots", []):
        if footprint(s.catalog, dict(slot, direction=0)) & set(counts):
            errors.append(f"expansion_slot_occupied:{xy(slot)}")
    return errors


def _check_recipes(s: Scene) -> list[str]:
    errors = [] if s.machines else ["no_machines"]
    for m in s.machines:
        recipe = s.recipe(m)
        if m.get("recipe") != s.plan["recipe"]:
            errors.append(f"machine_recipe_mismatch:{xy(m)}")
        categories = set(s.catalog.entities[m["name"]].get("crafting_categories") or ())
        if not recipe or not set(recipe.get("categories") or ()) & categories:
            errors.append(f"machine_cannot_craft:{xy(m)}")
    return errors


def _seed_inputs(s: Scene) -> dict:
    lanes = defaultdict(set)
    for port in s.ports("input"):
        for lane in [port["lane"]] if port.get("lane") in LANES else LANES:
            lanes[xy(port), lane].add(port["item"])
    spread(lanes, s.graph)
    return lanes


def _belt_items(lanes: dict, tile) -> set:
    return lanes[tile, "left"] | lanes[tile, "right"]


def _check_ingredients(s: Scene, lanes: dict) -> list[str]:
    errors = []
    for m in s.machines:
        needed = {i["name"] for i in s.recipe(m).get("ingredients", ())}
        got = set()
        for arm in s.arms:
            if s.machine_at(arm.drop) is not m:
                continue
            if arm.pickup in s.belts:
                got |= _belt_items(lanes, arm.pickup) & needed
            elif s.machine_at(arm.pickup) is None:
                errors.append(f"inserter_pickup_not_belt:{xy(arm.entity)}")
        errors += [f"ingredient_unreachable:{xy(m)}:{item}" for item in sorted(needed - got)]
    return errors


def _check_products(s: Scene, inputs: dict) -> list[str]:
    errors = []
    products = defaultdict(set)
    outputs = s.ports("output")
    sinks = {xy(p) for p in outputs}
    for m in s.machines:
        drops = [a for a in s.arms if s.machine_at(a.pickup) is m and a.drop in s.belts]
        if not drops:
            errors.append(f"machine_output_missing:{xy(m)}")
        for arm in drops:
            lane = inserter_output_lane(arm.entity["direction"], s.belts[arm.drop]["direction"])
            products[arm.drop, lane] |= {p["name"] for p in s.recipe(m).get("products", ())}
        if drops and not any(reachable(a.drop, s.graph) & sinks for a in drops):
            errors.append(f"machine_output_unconnected:{xy(m)}")
    spread(products, s.graph)
    for port in outputs:
        sides = [port["lane"]] if port.get("lane") in LANES else LANES
        if not any(port["item"] in products[xy(port), side] for side in sides):
            errors.append(f"output_port_missing_product:{port['item']}")
        if _belt_items(inputs, xy(port)):
            errors.append(f"input_items_reach_output:{xy(port)}")
    fed = {t for (t, _), items in inputs.items() if items}
    errors += [f"product_on_input_belt:{t}" for t in sorted(fed) if _belt_items(products, t)]
    return errors


def _check_belt_lines(s: Scene) -> list[str]:
    errors = []
    covered = set()
    for port in s.plan["ports"]:
        p, facing = xy(port), port.get("facing")
        belt = s.belts.get(p)
        if belt is None or belt["direction"] != facing:
            errors.append(f"port_not_on_facing_belt:{port['item']}:{p}")
            continue
        backwards = port.get("direction") == "output"
        covered.update(straight_walk(p, s.belts, s.graph, facing, backwards=backwards))
        outside = step(p, facing) if backwards else step(p, (facing + 8) % 16)
        if outside in s.belts or any(outside in footprint(s.catalog, e) for e in s.plan["entities"]):
            errors.append(f"port_not_at_boundary:{port['item']}:{p}")
    errors += [f"belt_not_on_port_line:{t}" for t in sorted(set(s.belts) - covered)]
    return errors


def _check_power(s: Scene) -> list[str]:
    errors = []
    poles = [e for e in s.plan["entities"] if _kind(s.catalog, e) == "electric-pole"]
    radius = {e["name"]: pole_radius(s.catalog, e["name"], s.limits) for e in poles}
    for e in s.plan["entities"]:
        row = s.catalog.entities.get(e["name"]) or {}
        if not row.get("electric") or row.get("type") == "electric-pole":
            continue
        tiles = footprint(s.catalog, e)
        covered = any(
            abs(t[0] - xy(q)[0]) < radius[q["name"]] + 0.5 and abs(t[1] - xy(q)[1]) < radius[q["name"]] + 0.5
            for q in poles
            for t in tiles
        )
        if not covered:
            errors.append(f"unpowered:{e['name']}:{xy(e)}")
    if not poles:
        return errors + ["no_poles"]
    network = {0}
    frontier = [0]
    while frontier:
        a = poles[frontier.pop()]
        for index, b in enumerate(poles):
            reach = min(pole_reach(s.catalog, a["name"], s.limits), pole_reach(s.catalog, b["name"], s.limits))
            if index not in network and math.dist(xy(a), xy(b)) <= reach + 1e-9:
                network.add(index)
                frontier.append(index)
    if len(network) != len(poles):
        errors.append("poles_not_wire_connected")
    return errors


def _rate(s: Scene, name: str) -> float:
    try:
        return inserter_rate(s.catalog, name, s.limits)
    except ValueError:
        return 0.0


def _check_throughput(s: Scene, lanes: dict) -> list[str]:
    errors = []
    demand = defaultdict(float)
    product_total = defaultdict(float)
    for m in s.machines:
        crafts = s.crafts(m)
        for row in s.recipe(m).get("ingredients", ()):
            demand[row["name"]] += crafts * float(row.get("amount", 0))
        for row in s.recipe(m).get("products", ()):
            product_total[row["name"]] += crafts * float(row.get("amount", 0))
        errors += _check_machine_arms(s, m, lanes, crafts)
    capacity = defaultdict(float)
    for port in s.ports("input"):
        belt = s.belts.get(xy(port))
        if belt is not None:
            capacity[port["item"]] += lane_capacity(s.catalog, belt["name"], s.limits) * (1 if port.get("lane") else 2)
    errors += [f"lane_capacity_exceeded:{i}" for i, d in demand.items() if d > capacity[i] + 1e-9]
    for port in s.ports("output"):
        belt = s.belts.get(xy(port))
        cap = lane_capacity(s.catalog, belt["name"], s.limits) if belt else 0.0
        if product_total[port["item"]] > cap + 1e-9:
            errors.append(f"output_lane_capacity_exceeded:{port['item']}")
    return errors


def _check_machine_arms(s: Scene, m: dict, lanes: dict, crafts: float) -> list[str]:
    errors = []
    amounts = {row["name"]: float(row.get("amount", 0)) for row in s.recipe(m).get("ingredients", ())}
    groups = defaultdict(float)
    for arm in s.arms:
        if s.machine_at(arm.drop) is m and arm.pickup in s.belts:
            groups[frozenset(_belt_items(lanes, arm.pickup) & set(amounts))] += _rate(s, arm.entity["name"])
    for items, rate in groups.items():
        if sum(amounts[i] for i in items) * crafts > rate + 1e-9:
            errors.append(f"inserter_throughput_insufficient:{xy(m)}:{','.join(sorted(items))}")
    out = sum(_rate(s, a.entity["name"]) for a in s.arms if s.machine_at(a.pickup) is m and a.drop in s.belts)
    made = sum(float(r.get("amount", 0)) for r in s.recipe(m).get("products", ())) * crafts
    if out and made > out + 1e-9:
        errors.append(f"output_inserter_throughput_insufficient:{xy(m)}")
    return errors


def _check_declared_rate(s: Scene) -> list[str]:
    declared = s.plan.get("crafts_per_machine")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return []
    return [f"declared_rate_above_nameplate:{xy(m)}" for m in s.machines if declared > s.nameplate(m) + 1e-9]


def audit_row(catalog: Any, plan: dict, *, limits: RowLimits = DEFAULT_LIMITS) -> list[str]:
    """Return sorted, de-duplicated finding codes (``code:detail``); empty = OK."""
    scene, errors = _scene(catalog, plan, limits)
    errors += _check_footprints(scene)
    errors += _check_recipes(scene)
    lanes = _seed_inputs(scene)
    errors += _check_ingredients(scene, lanes)
    errors += _check_products(scene, lanes)
    errors += _check_belt_lines(scene)
    errors += _check_power(scene)
    errors += _check_throughput(scene, lanes)
    errors += _check_declared_rate(scene)
    return sorted(set(errors))
