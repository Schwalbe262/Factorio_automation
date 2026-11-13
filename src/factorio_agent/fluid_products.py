"""Paid acid/lubricant cells; exact independent sources and native fluid proofs."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .bus_tap import BusTap
from .chemical_production import (
    plan_typed_fluid_connection,
    typed_fluid_edges,
    chemical_plan_isolation,
    fluid_crossing_proof,
    candidate_failure,
    _handoff,
)
from .industrial import PlanningBudget, free_tap_parameters
from .manufacturing import connect_power_groups
from .petroleum import pipeline_extent
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities, port
from .transport import VECTORS, xy, plan_connection, surface_pipe_proof, underground_proof, unbuilt_external_keepouts


PRODUCTS = {"sulfuric-acid": "sulfur-processing", "lubricant": "lubricant"}
SOURCE_KEYS = {
    "sulfuric-acid": {"water": "acid-water", "petroleum-gas": "acid-gas"},
    "lubricant": {"heavy-oil": "lubricant-heavy-oil"},
}
IRON_TAP = "fluid-acid-iron"


def _key(q):
    return q["name"] + ":" + str(q["position"]["x"]) + ":" + str(q["position"]["y"])


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _identity(obs, value, catalog):
    return value.get("world_id") == obs.get("world_id") and value.get("catalog_fingerprint") == catalog.fingerprint


def validate_fluid_product(parameters):
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"product"}
        or not isinstance(parameters.get("product"), str)
        or parameters["product"] not in PRODUCTS
    ):
        raise ValueError("fluid_products requires product sulfuric-acid or lubricant")


def fluid_product_rates(catalog, product):
    if product not in PRODUCTS:
        raise ValueError("supported fluid product required")
    machine = catalog.entities["chemical-plant"]
    recipes = [catalog.recipes[product]]
    if product == "sulfuric-acid":
        recipes.append(catalog.recipes["sulfur"])
    for r in recipes:
        if (
            r.get("category") not in machine["crafting_categories"]
            or len(r["products"]) != 1
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in (r["energy"], machine["crafting_speed"])
            )
            or any(
                type(q.get("amount")) not in (int, float) or not math.isfinite(q["amount"]) or q["amount"] <= 0
                for q in r["ingredients"]
            )
            or any(q.get("probability", 1) != 1 or q.get("independent_probability", 1) != 1 for q in r["products"])
        ):
            raise ValueError("deterministic ordinary compatible chemical recipe required")
    r = recipes[0]
    wanted = {
        "sulfuric-acid": {"iron-plate": ("item", 1), "sulfur": ("item", 5), "water": ("fluid", 100)},
        "lubricant": {"heavy-oil": ("fluid", 10)},
    }[product]
    if (
        {q["name"]: (q.get("type", "item"), q["amount"]) for q in r["ingredients"]} != wanted
        or r["products"][0]["name"] != product
        or r["products"][0].get("type") != "fluid"
    ):
        raise ValueError("installed acid/lubricant ingredient roles changed")
    machine_crafts = 60 * machine["crafting_speed"] / r["energy"]
    crafts = machine_crafts
    inputs = {}
    if product == "sulfuric-acid":
        precursor = recipes[1]
        if (
            {q["name"]: (q.get("type"), q["amount"]) for q in precursor["ingredients"]}
            != {"water": ("fluid", 30), "petroleum-gas": ("fluid", 30)}
            or precursor["products"][0]["name"] != "sulfur"
            or precursor["products"][0].get("type", "item") != "item"
        ):
            raise ValueError("ordinary sulfur precursor recipe changed")
        precursor_crafts = 60 * machine["crafting_speed"] / precursor["energy"]
        crafts = min(crafts, precursor_crafts * _yield(precursor["products"][0]) / wanted["sulfur"][1])
        sulfur_crafts = crafts * wanted["sulfur"][1] / _yield(precursor["products"][0])
        for q in precursor["ingredients"]:
            inputs[q["name"]] = sulfur_crafts * q["amount"]
    for q in r["ingredients"]:
        if q["name"] != "sulfur":
            inputs[q["name"]] = inputs.get(q["name"], 0) + crafts * q["amount"]
    return dict(
        product=product,
        fluid_per_minute=crafts * _yield(r["products"][0]),
        inputs_per_minute=inputs,
        machine_alone_per_minute=machine_crafts * _yield(r["products"][0]),
        chemical_plants=len(recipes),
        scope="catalog coupled nameplate; actual shared supply and measured output are separate, native backpressure bounds storage",
    )


def _plant_binding(catalog, recipe_name, item, side):
    rows = [
        q
        for q in catalog.recipes[recipe_name]["ingredients" if side == "input" else "products"]
        if q.get("type") == "fluid"
    ]
    row = next(q for q in rows if q["name"] == item)
    ordinal = row.get("fluidbox_index", next(i for i, q in enumerate(rows, 1) if q is row))
    boxes = sorted(
        (q for q in catalog.entities["chemical-plant"]["fluidbox_prototypes"] if q["production_type"] == side),
        key=lambda q: q["index"],
    )
    if type(ordinal) is not int or not 1 <= ordinal <= len(boxes):
        raise ValueError("native recipe-relative fluid ordinal changed")
    box = boxes[ordinal - 1]
    connections = [c for c in box["pipe_connections"] if c.get("connection_type", "normal") == "normal"]
    if len(connections) != 1 or len(connections[0].get("positions", [])) != 4:
        raise ValueError("four native port rotations required")
    c = connections[0]
    p = c["positions"][0]
    dx, dy = VECTORS[c["direction"]]
    return dict(fluidbox_index=box["index"], offset={"x": p["x"] + dx, "y": p["y"] + dy})


def fluid_product_core(catalog, product, x, y):
    rated = fluid_product_rates(catalog, product)
    if any(type(n) not in (int, float) or not math.isfinite(n) or n % 1 != 0.5 for n in (x, y)):
        raise ValueError("half-tile plant centers required")
    input_fluid = "water" if product == "sulfuric-acid" else "heavy-oil"
    incoming = _plant_binding(catalog, product, input_fluid, "input")
    outgoing = _plant_binding(catalog, product, product, "output")
    if incoming["offset"] != {"x": -1, "y": -2} or outgoing["offset"] != {"x": -1, "y": 2}:
        raise ValueError("installed chemical input/output geometry changed")
    machine = entity("chemical-plant", x, y, recipe=product)
    machines = [machine]
    groups = {}
    inputs = {}
    required = {_key(machine): [incoming["fluidbox_index"], outgoing["fluidbox_index"]]}
    physical = {
        _key(machine): {
            str(q["index"]): input_fluid if q["production_type"] == "input" else product
            for q in catalog.entities["chemical-plant"]["fluidbox_prototypes"]
        }
    }
    groups[input_fluid] = [entity("pipe", x + i, y - 4) for i in range(-6, 0)] + [
        entity("pipe", x - 1, y - 3),
        entity("pipe", x - 1, y - 2),
    ]
    inputs[input_fluid] = dict(
        kind="fluid",
        item=input_fluid,
        name="pipe",
        position={"x": x - 6, "y": y - 4},
        direction="input",
        facing=4,
        fluidbox_index=1,
    )
    arms = []
    iron_port = None
    es = []
    if product == "sulfuric-acid":
        precursor = entity("chemical-plant", x + 4, y, recipe="sulfur")
        machines.append(precursor)
        water = _plant_binding(catalog, "sulfur", "water", "input")
        gas = _plant_binding(catalog, "sulfur", "petroleum-gas", "input")
        if water["offset"] != {"x": -1, "y": -2} or gas["offset"] != {"x": 1, "y": -2}:
            raise ValueError("native precursor input geometry changed")
        groups["water"] += [entity("pipe", x + i, y - 4) for i in range(0, 4)] + [
            entity("pipe", x + 3, y - 3),
            entity("pipe", x + 3, y - 2),
        ]
        groups["petroleum-gas"] = [entity("pipe", x + i, y - 7) for i in range(-6, 6)] + [
            entity("pipe", x + 5, y + i) for i in range(-6, -1)
        ]
        inputs["petroleum-gas"] = dict(
            kind="fluid",
            item="petroleum-gas",
            name="pipe",
            position={"x": x - 6, "y": y - 7},
            direction="input",
            facing=4,
            fluidbox_index=1,
        )
        required[_key(precursor)] = [water["fluidbox_index"], gas["fluidbox_index"]]
        physical[_key(precursor)] = {str(water["fluidbox_index"]): "water", str(gas["fluidbox_index"]): "petroleum-gas"}
        arms = [
            dict(
                entity("fast-inserter", x - 2, y, 12),
                feed="iron-plate",
                from_position={"x": x - 3, "y": y},
                to_machine=_key(machine),
            ),
            dict(
                entity("fast-inserter", x + 2, y, 4),
                feed="sulfur",
                from_machine=_key(precursor),
                to_machine=_key(machine),
            ),
        ]
        es += [entity("transport-belt", x + i, y, 4) for i in range(-6, -2)]
        iron_port = port("iron-plate", x - 6, y, 4)
    groups[product] = [entity("pipe", x - 1, y + 2)] + [entity("pipe", x + i, y + 3) for i in range(-1, 15)]
    output = dict(
        kind="fluid",
        item=product,
        name="pipe",
        position={"x": x + 14, "y": y + 3},
        direction="output",
        facing=4,
        fluidbox_index=1,
    )
    poles = [entity("small-electric-pole", x - 2, y + 1)]
    if len(machines) == 2:
        poles += [entity("small-electric-pole", x + 2, y + 2), entity("small-electric-pole", x + 6, y + 1)]
    es = merge_entities(
        es,
        *groups.values(),
        machines,
        [
            {k: v for k, v in arm.items() if k not in ("feed", "from_position", "from_machine", "to_machine")}
            for arm in arms
        ],
        poles,
    )
    access = [entity("pipe", x + i, y + 3) for i in (15, 16)]
    future = [entity("chemical-plant", x + 10, y)]
    return _plan(
        es,
        list(inputs.values()) + ([iron_port] if iron_port else []) + [output],
        product=product,
        machine=machine,
        machines=machines,
        fluid_groups=groups,
        input_fluid_ports=inputs,
        output_port=output,
        output_access=access,
        iron_port=iron_port,
        arms=arms,
        physical_boxes=physical,
        required_boxes=required,
        rated=rated,
        reserved_area=[[x - 6.5, y - 7.5], [x + 16.5, y + 4.5]],
        anchor={"x": x, "y": y},
        expansion_keepouts=future,
        expansion_slots=[dict(position=future[0]["position"], built=False)],
        material_flow="fluid inputs EAST; iron EAST; sulfur direct WEST into acid; fluid output EAST",
        growth_direction="EAST reserved second processing slot",
        layout_policy="fixed-paid-fluid-product-cell-v1",
    )


def fluid_product_edges(catalog, core, fluid, nodes, pairs):
    edges = []
    for machine in core["machines"]:
        recipe = catalog.recipes[machine["recipe"]]
        for side, rows in (("input", recipe["ingredients"]), ("output", recipe["products"])):
            if not any(q.get("type") == "fluid" and q["name"] == fluid for q in rows):
                continue
            binding = _plant_binding(catalog, machine["recipe"], fluid, side)
            p = machine["position"]
            d = binding["offset"]
            stub = next(e for e in nodes if xy(e) == (p["x"] + d["x"], p["y"] + d["y"]))
            if not edges:
                edges = typed_fluid_edges(nodes, pairs, machine, binding["fluidbox_index"], stub)
            else:
                edges.append(
                    {"a": dict(machine, fluidbox_index=binding["fluidbox_index"]), "b": dict(stub, fluidbox_index=1)}
                )
    if not edges:
        raise ValueError("native fluid network requires its matching physical machine port")
    return edges


class FluidProducts:
    def __init__(
        self,
        game,
        builder,
        catalog,
        perimeter,
        *,
        bus_factory=None,
        science=None,
        green=None,
        fluid_source_keys=None,
        site=None,
    ):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.site = site  # planned world: cell anchors from the master plan (block_sites)
        self.path = Path(game.cfg.runtime_dir) / "fluid-products.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.source_keys = deepcopy(SOURCE_KEYS)
        if fluid_source_keys is not None:
            if not isinstance(fluid_source_keys, dict) or set(fluid_source_keys) - set(PRODUCTS):
                raise ValueError("known product source-key mapping required")
            for product, keys in fluid_source_keys.items():
                if (
                    not isinstance(keys, dict)
                    or set(keys) != set(SOURCE_KEYS[product])
                    or any(not isinstance(k, str) or not k for k in keys.values())
                ):
                    raise ValueError("exact product fluid source keys required")
                self.source_keys[product] = deepcopy(keys)
        self.tap = BusTap(game, builder, catalog, bus_factory=bus_factory, science=science, green=green)
        self.last_proof_failure = None

    def _save(self):
        _atomic_json(self.path, self.state)

    def _read(self, name):
        path = self.path.parent / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _sources(self, obs, product):
        registry = self._read("fluid-sources.json")
        if not _identity(obs, registry, self.catalog):
            return dict(ok=False, reason="fluid_product_registry_missing_or_changed")
        sources = {}
        consumer = "fluid-product:" + product
        saved = self.state.get("products", {}).get(product, {}).get("sources", {})
        for fluid, key in self.source_keys[product].items():
            source = saved.get(fluid) or registry.get("sources", {}).get(key)
            if source is None and product == "lubricant" and key == "lubricant-heavy-oil":
                source = registry.get("sources", {}).get("heavy-oil")
            if source is None:
                return dict(
                    ok=False,
                    reason="fluid_product_paid_source_required",
                    item=fluid,
                    registry_key=key,
                    prerequisite="independent water intake acid-water"
                    if fluid == "water"
                    else "paid FluidDistribution outlet " + key,
                )
            if (
                source not in registry.get("sources", {}).values()
                or source.get("kind") != "fluid"
                or source.get("item") != fluid
                or source.get("direction") != "output"
                or source.get("name") != "pipe"
                or source.get("world_id") != obs["world_id"]
                or type(source.get("unit_number")) is not int
                or source["unit_number"] < 1
                or source.get("fluidbox_index") != 1
                or source.get("facing") not in VECTORS
            ):
                return dict(ok=False, reason="fluid_product_exact_published_source_required", item=fluid)
            for owner, rows in registry.get("handoffs", {}).items():
                if (
                    any(h.get("source", {}).get("unit_number") == source["unit_number"] for h in rows.values())
                    and owner != consumer
                ):
                    return dict(
                        ok=False,
                        reason="fluid_product_source_needs_paid_independent_branch",
                        item=fluid,
                        existing_consumer=owner,
                    )
            sources[fluid] = deepcopy(source)
        native = self.game.query(SOURCE_LUA.replace("PAYLOAD", _payload({"sources": sources})))
        if (
            native.get("ok")
            and not saved
            and any(q.get("amount", 0) <= 0 for q in native.get("snapshots", {}).values())
        ):
            return dict(ok=False, reason="fluid_product_filled_real_source_required")
        return dict(native, sources=sources)

    def _iron_source(self, obs):
        factory = self._read("bus-factory.json")
        board = self._read("pcb-board.json")
        bus = factory.get("bus", {})
        if (
            not _identity(obs, factory, self.catalog)
            or not _identity(obs, board, self.catalog)
            or factory.get("phase") != "verified"
            or board.get("shared_main_bus") != bus
        ):
            return dict(ok=False, reason="acid_requires_verified_shared_main_bus")
        wanted = next((p for p in bus.get("ports", []) if p.get("name") == IRON_TAP), None)
        if not wanted:
            return dict(ok=False, reason="acid_paid_iron_tap_required")
        rate = fluid_product_rates(self.catalog, "sulfuric-acid")["inputs_per_minute"]["iron-plate"]
        if (
            wanted.get("item") != "iron-plate"
            or wanted.get("priority") != "surplus"
            or wanted.get("demand_per_minute") != rate
        ):
            return dict(ok=False, reason="acid_iron_tap_contract_changed")
        live = next(
            (e for e in obs["entities"] if e["name"] == "transport-belt" and e["position"] == wanted["position"]), None
        )
        station = next((s for s in bus["stations"] if s["name"] == IRON_TAP), None)
        if not live or not station or type(live.get("unit_number")) is not int:
            return dict(ok=False, reason="acid_native_paid_tap_missing")
        splitter = next(
            (
                e
                for e in obs["entities"]
                if e["name"] == "splitter" and e["position"] == station["splitter"]["position"]
            ),
            None,
        )
        if not splitter or type(splitter.get("unit_number")) is not int:
            return dict(ok=False, reason="acid_native_paid_splitter_missing")
        pinned = dict(station["splitter"], unit_number=splitter["unit_number"])
        source = dict(
            wanted,
            name="transport-belt",
            port_name=IRON_TAP,
            unit_number=live["unit_number"],
            world_id=obs["world_id"],
            priority_splitter=pinned,
        )
        native = self.game.query(IRON_SOURCE_LUA.replace("PAYLOAD", _payload({"source": source, "splitter": pinned})))
        return dict(native, source=source)

    def _iron_prerequisite(self, obs):
        saved = self.state.get("acid_tap_parameters")
        if saved:
            preview = self.tap.prepare(obs, saved)
            return dict(ok=False, reason="acid_paid_iron_tap_required", tap_parameters=saved, preview=preview)
        factory = self._read("bus-factory.json")
        try:
            choices = free_tap_parameters(
                factory["bus"],
                name=IRON_TAP,
                item="iron-plate",
                demand_per_minute=fluid_product_rates(self.catalog, "sulfuric-acid")["inputs_per_minute"]["iron-plate"],
            )
            failures = []
            for parameters in choices[:12]:
                preview = self.tap.prepare(obs, parameters)
                if preview.get("ok"):
                    return dict(
                        ok=False, reason="acid_paid_iron_tap_required", tap_parameters=parameters, preview=preview
                    )
                failures.append({"reason": preview.get("reason"), "parameters": parameters})
            return dict(
                ok=False,
                reason="acid_bus_capacity_or_paid_frontier_extension_required",
                candidate_failures=failures[-4:],
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return dict(ok=False, reason="acid_shared_bus_prerequisite_unavailable", detail=str(exc))

    def prepare(self, obs, parameters):
        validate_fluid_product(parameters)
        product = parameters["product"]
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return dict(ok=False, reason="fluid_product_valid_world_observation_required")
        recipes = [product, "chemical-plant", "pipe", "pipe-to-ground", "small-electric-pole"] + (
            ["sulfur", "fast-inserter", "transport-belt"] if product == "sulfuric-acid" else []
        )
        if not obs.get("technologies", {}).get(PRODUCTS[product]) or not all(
            obs.get("enabled_recipes", {}).get(r) for r in recipes
        ):
            return dict(
                ok=False,
                reason="native_fluid_product_technology_and_recipes_required",
                technology=PRODUCTS[product],
                recipes=recipes,
            )
        if self.state and not _identity(obs, self.state, self.catalog):
            return dict(ok=False, reason="fluid_products_world_or_catalog_changed")
        try:
            sources = self._sources(obs, product)
            if not sources.get("ok"):
                return sources
            iron = None
            if product == "sulfuric-acid":
                iron = self._iron_source(obs)
                if not iron.get("ok"):
                    return self._iron_prerequisite(obs) if iron["reason"] == "acid_paid_iron_tap_required" else iron
            wanted = dict(sources["sources"], **({"iron-plate": iron["source"]} if iron else {}))
            existing = self.state.get("products", {}).get(product)
            if existing:
                return (
                    deepcopy(existing)
                    if existing["sources"] == wanted
                    else dict(ok=False, reason="fluid_product_prepared_source_changed")
                )
            board = self._read("pcb-board.json")
            if not _identity(obs, board, self.catalog):
                return dict(ok=False, reason="fluid_product_board_identity_changed")
            areas = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
            areas += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
            future = unbuilt_external_keepouts(self.builder, obs, board["blocks"])
            source = next(iter(sources["sources"].values()))
            sx, sy = xy(source)
            facing = source["facing"]
            base_x = sx + (-20 if facing == 12 else 10)
            base_y = sy + ({0: -12, 4: 8, 8: 16, 12: 8}[facing])
            sign = -1 if facing == 12 else 1
            anchors = [
                (base_x + sign * dx, base_y + dy)
                for dx, dy in ((0, 0), (24, 0), (0, 18), (24, 18), (0, -18), (24, -18), (0, -36), (24, -36))
            ]
            from . import block_sites

            owner = "fluid_product_" + product
            sited = block_sites.anchors(
                self.site, obs, owner, lambda ax, ay: fluid_product_core(self.catalog, product, ax, ay)
            )
            self._allocations = {anchor: name for anchor, name in sited or ()}
            if sited is not None:
                anchors = list(self._allocations)
                if not anchors:
                    return dict(ok=False, reason="fluid_product_no_site_allocation", candidate_failures=[])
            budget = PlanningBudget(self.game)
            failures = []
            for x, y in anchors:
                if budget.expired():
                    return budget.failure(candidate_failures=failures[-4:])
                core = fluid_product_core(self.catalog, product, x, y)
                area = core["reserved_area"]
                if any(overlap(area, a) for a in areas) or any(
                    area[0][0] < px + 0.49
                    and px - 0.49 < area[1][0]
                    and area[0][1] < py + 0.49
                    and py - 0.49 < area[1][1]
                    for px, py in future
                ):
                    continue
                land = survey_slots(budget.game, [[area]])
                if not land.get("ok") or 1 not in land.get("legal", []):
                    continue
                es = core["entities"][:]
                networks = {}
                prior = []
                handoffs = {}
                for fluid, destination in core["input_fluid_ports"].items():
                    options = {}
                    if prior:
                        from .joint_fluid_routing import trusted_prior

                        options["owned_prior"] = trusted_prior(obs["world_id"], core, networks)
                    route = plan_typed_fluid_connection(
                        budget.game, self.builder, obs, wanted[fluid], destination, core, prior, **options
                    )
                    if not route.get("ok"):
                        failures.append(candidate_failure({"x": x, "y": y}, "fluid_route", route, item=fluid))
                        break
                    nodes = merge_entities([wanted[fluid]], route["entities"], core["fluid_groups"][fluid])
                    extent = pipeline_extent(
                        sources["snapshots"][fluid]["extent"],
                        nodes + core["machines"],
                        sources["snapshots"][fluid]["limit"],
                    )
                    if not extent.get("ok"):
                        failures.append(extent)
                        break
                    networks[fluid] = dict(nodes=nodes, route=route, source=wanted[fluid], extent=extent)
                    handoffs[fluid] = dict(
                        consumer_id="fluid-product:" + product,
                        source=wanted[fluid],
                        first_neighbor=route["first_neighbor"],
                    )
                    es = merge_entities(es, route["entities"])
                    prior += route["entities"]
                if len(networks) != len(core["input_fluid_ports"]):
                    continue
                maximum = max(n["route"]["maximum_underground_span"] for n in networks.values())
                networks[product] = dict(
                    nodes=core["fluid_groups"][product],
                    source={"unit_number": 0},
                    route=dict(underground_pairs=[], maximum_underground_span=maximum),
                )
                belt = None
                if iron:
                    belt = plan_connection(budget.game, self.builder, obs, iron["source"], core["iron_port"], es)
                    if not belt.get("ok"):
                        failures.append(belt)
                        continue
                    es = merge_entities(es, belt["entities"])
                power = connect_power_groups(
                    budget.game, self.builder, obs, [core], es + core["output_access"] + core["expansion_keepouts"]
                )
                if not power.get("ok"):
                    failures.append(power)
                    continue
                es = merge_entities(es, power["entities"])
                whole = preflight(
                    budget.game, self.builder, obs, es + core["output_access"] + core["expansion_keepouts"]
                )
                if not whole.get("ok"):
                    failures.append(whole)
                    continue
                isolation = chemical_plan_isolation(budget.game, networks, core)
                if not isolation.get("ok"):
                    failures.append(isolation)
                    continue
                scope = "fluid-product:" + product
                defense = self.perimeter.prepare_coverage(obs, scope, [area])
                if not defense.get("ok"):
                    return dict(ok=False, reason="fluid_product_defense_preflight_failed", evidence=defense)
                for fluid, network in networks.items():
                    network["edges"] = fluid_product_edges(
                        self.catalog, core, fluid, network["nodes"], network["route"]["underground_pairs"]
                    )
                plan = _plan(
                    es,
                    core["ports"],
                    reserved_area=area,
                    underground_pairs=belt["underground_pairs"] if belt else [],
                    fluid_underground_pairs=[
                        deepcopy(q) for n in networks.values() for q in n["route"]["underground_pairs"]
                    ],
                    preserved_fluid_crossings=list(
                        {
                            q["unit_number"]: q
                            for n in networks.values()
                            for q in n["route"].get("preserved_fluid_crossings", [])
                        }.values()
                    ),
                    owned_fluid_crossings=[
                        q for n in networks.values() for q in n["route"].get("owned_fluid_crossings", [])
                    ],
                    clear_obstacles=whole["clear_obstacles"],
                    clearance_world_id=obs["world_id"],
                    defense_scope_id=scope,
                    layout_policy=core["layout_policy"],
                )
                return dict(
                    ok=True,
                    world_id=obs["world_id"],
                    catalog_fingerprint=self.catalog.fingerprint,
                    product=product,
                    phase="build",
                    core=core,
                    plan=plan,
                    sources=wanted,
                    fluid_plans=networks,
                    fluid_handoffs=handoffs,
                    bindings={},
                    defense_scope_id=scope,
                    defense_preview=defense,
                    rated=core["rated"],
                    site_allocation=getattr(self, "_allocations", {}).get((x, y)),
                )
            return dict(
                ok=False, reason="no_complete_resource_clear_fluid_product_site", candidate_failures=failures[-6:]
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return dict(ok=False, reason="fluid_product_context_or_catalog_invalid", detail=str(exc))

    def _bind(self, obs, entry):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        changed = False
        for spec in entry["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            old = entry["bindings"].get(_key(spec))
            if old and (not live or live.get("unit_number") != old):
                return dict(status="blocked", reason="fluid_product_owned_asset_lost_or_replaced", entity=spec)
            if live:
                if (
                    type(live.get("unit_number")) is not int
                    or (
                        spec["name"] not in ("pipe", "small-electric-pole")
                        and live.get("direction", 0) != spec.get("direction", 0)
                    )
                    or spec.get("recipe")
                    and live.get("recipe")
                    not in ((spec["recipe"],) if entry.get("phase") == "verified" else (None, spec["recipe"]))
                ):
                    return dict(status="blocked", reason="fluid_product_owned_configuration_changed", entity=spec)
                if old is None:
                    entry["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def step(self, obs, parameters):
        validate_fluid_product(parameters)
        product = parameters["product"]
        if self.state and not _identity(obs, self.state, self.catalog):
            return dict(status="blocked", reason="fluid_products_world_or_catalog_changed")
        entry = self.state.get("products", {}).get(product)
        if not entry:
            ready = self.prepare(obs, parameters)
            if not ready.get("ok"):
                if ready.get("reason") == "acid_paid_iron_tap_required" and ready.get("tap_parameters"):
                    if not self.state:
                        self.state = dict(
                            world_id=obs["world_id"], catalog_fingerprint=self.catalog.fingerprint, products={}
                        )
                    prior = self.state.get("acid_tap_parameters")
                    if prior and prior != ready["tap_parameters"]:
                        return dict(status="blocked", reason="acid_paid_tap_request_changed")
                    self.state["acid_tap_parameters"] = ready["tap_parameters"]
                    self._save()
                    result = self.tap.step(obs, ready["tap_parameters"])
                    return (
                        dict(status="running", reason="acid_paid_iron_tap_ready_continue_product_cell", evidence=result)
                        if result.get("status") == "succeeded"
                        else result
                    )
                return dict(
                    ready, status="blocked", evidence={k: v for k, v in ready.items() if k not in ("ok", "status")}
                )
            if not self.state:
                self.state = dict(world_id=obs["world_id"], catalog_fingerprint=self.catalog.fingerprint, products={})
            self.state["products"][product] = entry = ready
            self._save()
            from . import block_sites

            block_sites.keep(self.site, obs, "fluid_product_" + product, ready.get("site_allocation"))
        sources = self._sources(obs, product)
        if not sources.get("ok"):
            return dict(status="blocked", reason="fluid_product_source_unavailable", evidence=sources)
        wanted = dict(sources["sources"])
        if product == "sulfuric-acid":
            iron = self._iron_source(obs)
            if not iron.get("ok"):
                return dict(status="blocked", reason="fluid_product_iron_source_unavailable", evidence=iron)
            wanted["iron-plate"] = iron["source"]
        if wanted != entry["sources"]:
            return dict(status="blocked", reason="fluid_product_prepared_source_changed")
        blocked = self._bind(obs, entry)
        if blocked:
            return blocked
        path = self.path.parent / "pcb-board.json"
        board = self._read("pcb-board.json")
        name = "fluid_product_" + product
        if not _identity(obs, board, self.catalog):
            return dict(status="blocked", reason="fluid_product_board_identity_changed")
        old = board["blocks"].get(name)
        if old is not None and old != entry["plan"]:
            return dict(status="blocked", reason="fluid_product_registered_plan_changed")
        if any(
            k != name and overlap(entry["core"]["reserved_area"], p["reserved_area"])
            for k, p in board["blocks"].items()
            if p.get("reserved_area")
        ):
            return dict(status="blocked", reason="fluid_product_reserved_area_conflict")
        if old is None:
            board["blocks"][name] = entry["plan"]
            _atomic_json(path, board)
        coverage = self.perimeter.ensure_coverage(obs, entry["defense_scope_id"], [entry["core"]["reserved_area"]])
        if coverage is not None:
            return coverage
        guard = surface_pipe_proof(self.game, obs, entry["plan"])
        if not guard.get("ok"):
            return dict(status="blocked", reason="fluid_product_preserved_pipe_changed", evidence=guard)
        guard = fluid_crossing_proof(self.game, obs, entry["plan"], owned_bindings=entry["bindings"])
        if not guard.get("ok"):
            return dict(status="blocked", reason="fluid_product_preserved_crossing_changed", evidence=guard)
        for fluid, h in entry["fluid_handoffs"].items():
            neighbor = {k: v for k, v in h["first_neighbor"].items() if k != "unit_number"}
            declared = _handoff(self.game, self.catalog, obs, h["consumer_id"], fluid, h["source"], neighbor)
            if not declared.get("ok"):
                return dict(status="blocked", reason="fluid_product_handoff_not_authorized", evidence=declared)
            unit = entry["bindings"].get(_key(neighbor))
            if unit:
                bound = _handoff(
                    self.game,
                    self.catalog,
                    obs,
                    h["consumer_id"],
                    fluid,
                    h["source"],
                    dict(neighbor, unit_number=unit),
                    bind=True,
                )
                if not bound.get("ok"):
                    return dict(status="blocked", reason="fluid_product_handoff_not_bound", evidence=bound)
        if not entry.get("construction_authorized"):
            check = preflight(
                self.game,
                self.builder,
                obs,
                entry["plan"]["entities"] + entry["core"]["output_access"] + entry["core"]["expansion_keepouts"],
            )
            if not check.get("ok"):
                return dict(status="blocked", reason="fluid_product_post_defense_preflight_failed", evidence=check)
            entry["construction_authorized"] = True
            self._save()
        result = self.builder.ensure_plan(obs, entry["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs, parameters)
        if not proof:
            return dict(
                status="waiting",
                reason="waiting_for_native_fluid_product_and_reciprocal_flow",
                evidence=self.last_proof_failure,
            )
        registry = self._read("fluid-sources.json")
        if not _identity(obs, registry, self.catalog):
            return dict(status="blocked", reason="fluid_product_output_registry_changed")
        old = registry.setdefault("sources", {}).get(product)
        if old is not None and old != proof["source"]:
            return dict(status="blocked", reason="fluid_product_fixed_output_already_owned")
        registry["sources"][product] = deepcopy(proof["source"])
        _atomic_json(self.path.parent / "fluid-sources.json", registry)
        entry.update(phase="verified", proof=proof)
        self._save()
        return dict(status="succeeded", reason="normally supplied permanent fluid product commissioned", evidence=proof)

    def _output(self, obs, entry):
        return dict(
            entry["core"]["output_port"],
            world_id=obs["world_id"],
            unit_number=entry["bindings"].get(_key(entry["core"]["output_port"])),
            defense_scope_id=entry["defense_scope_id"],
        )

    def proof(self, obs, parameters):
        validate_fluid_product(parameters)
        self.last_proof_failure = None
        product = parameters["product"]
        if not self.state or not _identity(obs, self.state, self.catalog):
            return None
        entry = self.state.get("products", {}).get(product)
        if not entry:
            return None
        try:
            actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            for spec in entry["plan"]["entities"]:
                live = actual.get((spec["name"], *xy(spec)))
                unit = entry["bindings"].get(_key(spec))
                if (
                    not unit
                    or not live
                    or live.get("unit_number") != unit
                    or spec["name"] not in ("pipe", "small-electric-pole")
                    and live.get("direction", 0) != spec.get("direction", 0)
                ):
                    self.last_proof_failure = dict(reason="fluid_product_exact_native_binding_required", entity=spec)
                    return None
            if not underground_proof(obs, entry["plan"]):
                return None
            for guard in (
                surface_pipe_proof(self.game, obs, entry["plan"]),
                fluid_crossing_proof(self.game, obs, entry["plan"], owned_bindings=entry["bindings"]),
            ):
                if not guard.get("ok"):
                    self.last_proof_failure = guard
                    return None
            defense = self.perimeter.coverage_proof(obs, entry["defense_scope_id"])
            if not defense:
                return None
            ready = self._sources(obs, product)
            if not ready.get("ok") or ready["sources"] != {
                k: v for k, v in entry["sources"].items() if k != "iron-plate"
            }:
                return None
            if product == "sulfuric-acid":
                iron = self._iron_source(obs)
                if not iron.get("ok") or iron["source"] != entry["sources"]["iron-plate"]:
                    return None
            registry = self._read("fluid-sources.json")
            for fluid, h in entry["fluid_handoffs"].items():
                row = registry.get("handoffs", {}).get(h["consumer_id"], {}).get(fluid, {})
                neighbor = dict(h["first_neighbor"], unit_number=entry["bindings"].get(_key(h["first_neighbor"])))
                if (
                    not _identity(obs, row, self.catalog)
                    or row.get("phase") != "bound"
                    or row.get("source") != h["source"]
                    or row.get("first_neighbor") != neighbor
                ):
                    return None
            bindings = dict(entry["bindings"])
            bindings.update({_key(q): q["unit_number"] for q in entry["sources"].values()})
            groups = {
                fluid: [dict(q, unit_number=bindings[_key(q)]) for q in n["nodes"]]
                for fluid, n in entry["fluid_plans"].items()
            }
            machines = [
                dict(
                    q,
                    unit_number=bindings[_key(q)],
                    physical_boxes=entry["core"]["physical_boxes"][_key(q)],
                    required_boxes=entry["core"]["required_boxes"][_key(q)],
                )
                for q in entry["core"]["machines"]
            ]
            output = self._output(obs, entry)
            downstream = []
            from .fluid_handoffs import _descriptor

            for rows in registry.get("handoffs", {}).values():
                for row in rows.values():
                    if row.get("source") == output:
                        if not _identity(obs, row, self.catalog) or row.get("phase") != "bound":
                            continue
                        downstream.append(_descriptor(row["first_neighbor"], output, bound=True))
            args = dict(
                product=product,
                groups=groups,
                machines=machines,
                edges=[e for n in entry["fluid_plans"].values() for e in n["edges"]],
                source_units=[q["unit_number"] for q in entry["sources"].values() if q["kind"] == "fluid"],
                prototype_box_count=len(self.catalog.entities["chemical-plant"]["fluidbox_prototypes"]),
                arms=[dict(q, unit_number=bindings[_key(q)]) for q in entry["core"]["arms"]],
                belts=[
                    dict(q, unit_number=bindings[_key(q)])
                    for q in entry["plan"]["entities"]
                    if q["name"] in ("transport-belt", "underground-belt")
                ],
                output=output,
                downstream=downstream,
            )
            native = self.game.query(FLUID_PRODUCT_PROOF_LUA.replace("PAYLOAD", _payload(args)))
            if not native.get("ok"):
                self.last_proof_failure = native
                return None
            return dict(
                tick=native["tick"],
                product=product,
                source=output,
                native=native,
                rated=entry["rated"],
                defense_coverage=defense,
                scope="actual naturally unlocked powered recipes and positive typed fluid output; native buffers/backpressure, no sustained nameplate promise",
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self.last_proof_failure = dict(reason="fluid_product_proof_context_invalid", detail=str(exc))
            return None


SOURCE_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local snapshots={}
for fluid,q in pairs(args.sources)do local e=target(q.position,q.name)
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="fluid_product_source_unit_changed",fluid=fluid}end
 local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~=fluid then return {ok=false,reason="fluid_product_source_contaminated"}end end
 if not e.has_fluid_segment(1)then return {ok=false,reason="fluid_product_source_segment_missing"}end
 snapshots[fluid]={amount=fluids[fluid]or 0,extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}
end;return {ok=true,snapshots=snapshots}
"""

IRON_SOURCE_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local q=args.source;local e=target(q.position,"transport-belt");local p=args.splitter;local splitter=target(p.position,"splitter")
if not e or e.unit_number~=q.unit_number or e.force~=f or e.surface~=s or e.direction~=q.facing or not splitter or splitter.unit_number~=p.unit_number or splitter.force~=f or splitter.surface~=s or splitter.direction~=p.direction or splitter.splitter_input_priority~=p.splitter_input_priority or splitter.splitter_output_priority~=p.splitter_output_priority then return {ok=false,reason="acid_native_priority_tap_changed"}end
for i=1,2 do for _,row in pairs(e.get_transport_line(i).get_contents())do if row.name~="iron-plate"then return {ok=false,reason="acid_iron_tap_contaminated"}end end end;return {ok=true}
"""

FLUID_PRODUCT_PROOF_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local live={};local expected={};local maps={};local active={};local fluids={};local units={}
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local function id(q,index)return key(q).."@"..index end
local function exact(q)local e=target(q.position,q.name);if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e end
for fluid,nodes in pairs(args.groups)do local total=0;for _,q in ipairs(nodes)do local e=exact(q)
 if not e or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="fluid_product_pipe_unit_or_direction_changed"}end
 for name,amount in pairs(e.get_fluid_contents())do if name~=fluid then return {ok=false,reason="fluid_product_network_contaminated"}end;total=total+amount end
 if not e.has_fluid_segment(1)then return {ok=false,reason="fluid_product_native_segment_missing"}end
 local box=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
 if box.right_bottom.x-box.left_top.x>limit or box.right_bottom.y-box.left_top.y>limit then return {ok=false,reason="fluid_product_extent_exceeded"}end
 live[key(q)]=e
end;if total<=0 then return {ok=false,reason="fluid_product_positive_typed_flow_required",fluid=fluid}end;fluids[fluid]=total end
for _,q in ipairs(args.machines)do local e=exact(q)
 if not e or e.direction~=0 or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="fluid_product_machine_missing_or_unpowered"}end
 local recipe=e.get_recipe();if not recipe or recipe.name~=q.recipe or e.products_finished<=0 then return {ok=false,reason="fluid_product_actual_recipe_products_required"}end
 local map={};local indices={};local roles={}
 for index=1,args.prototype_box_count do local ok,prototype=pcall(function()return e.get_fluid_box_prototype(index)end)
  if ok and prototype then local list=prototype.index and{prototype}or prototype;local role=nil;local count=0
   for _,p in pairs(list)do
    if type(p.index)~="number"or p.index%1~=0 or p.index<1 or p.index>args.prototype_box_count or map[tostring(p.index)]then return {ok=false,reason="fluid_product_active_box_mapping_invalid"}end
    local allowed=q.physical_boxes[tostring(p.index)];if allowed then if role and role~=allowed then return {ok=false,reason="fluid_product_different_fluids_merged"}end;role=allowed end
    map[tostring(p.index)]=index;count=count+1
   end
   if count==0 then return {ok=false,reason="fluid_product_active_box_mapping_empty"}end
   local liquid=e.get_fluid(index);if liquid and liquid.amount>0 and liquid.name~=role then return {ok=false,reason="fluid_product_wrong_machine_storage"}end
   indices[#indices+1]=index;roles[#roles+1]={runtime_index=index,fluid=role}
  end
 end
 for _,index in ipairs(q.required_boxes)do if not map[tostring(index)]then return {ok=false,reason="fluid_product_required_physical_box_inactive"}end end
 maps[key(q)]=map;active[key(q)]=indices;live[key(q)]=e;units[tostring(e.unit_number)]={recipe=recipe.name,products_finished=e.products_finished,active_boxes=roles}
end
local function runtime(q,index)local map=maps[key(q)];return map and map[tostring(index)]or index end
local function edge(a,ai,b,bi)
 local first=live[key(a)];local second=live[key(b)];if not first or not second or not ai or not bi then return false end
 local forward=false;local reverse=false
 for _,peer in pairs(first.get_fluid_box_neighbours(ai)or{})do if peer.entity==second and peer.index==bi then forward=true end end
 for _,peer in pairs(second.get_fluid_box_neighbours(bi)or{})do if peer.entity==first and peer.index==ai then reverse=true end end
 if not forward or not reverse then return false end
 expected[id(a,ai)]=expected[id(a,ai)]or{};expected[id(a,ai)][second.unit_number..":"..bi]=true
 expected[id(b,bi)]=expected[id(b,bi)]or{};expected[id(b,bi)][first.unit_number..":"..ai]=true;return true
end
for _,row in ipairs(args.edges)do if not edge(row.a,runtime(row.a,row.a.fluidbox_index),row.b,runtime(row.b,row.b.fluidbox_index))then return {ok=false,reason="fluid_product_reciprocal_peer_missing"}end end
if #args.downstream>1 then return {ok=false,reason="fluid_product_output_has_multiple_consumers"}end
for _,q in ipairs(args.downstream)do local e=exact(q)
 if not e or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="fluid_product_bound_consumer_changed"}end
 for name in pairs(e.get_fluid_contents())do if name~=args.product then return {ok=false,reason="fluid_product_consumer_contaminated"}end end
 live[key(q)]=e;if not edge(args.output,1,q,1)then return {ok=false,reason="fluid_product_bound_consumer_peer_missing"}end
end
local upstream={};for _,unit in ipairs(args.source_units)do upstream[unit]=true end
for _,nodes in pairs(args.groups)do for _,q in ipairs(nodes)do if not upstream[q.unit_number]then for _,peer in pairs(live[key(q)].get_fluid_box_neighbours(1)or{})do
 if not(expected[id(q,1)]or{})[peer.entity.unit_number..":"..peer.index]then return {ok=false,reason="fluid_product_foreign_pipe_peer"}end
end end end end
for _,q in ipairs(args.machines)do for _,index in ipairs(active[key(q)])do for _,peer in pairs(live[key(q)].get_fluid_box_neighbours(index)or{})do
 if not(expected[id(q,index)]or{})[peer.entity.unit_number..":"..peer.index]then return {ok=false,reason="fluid_product_foreign_machine_peer"}end
end end end
for _,q in ipairs(args.belts)do local e=exact(q)
 if not e or e.direction~=q.direction then return {ok=false,reason="fluid_product_iron_belt_changed"}end
 for i=1,2 do for _,row in pairs(e.get_transport_line(i).get_contents())do if row.name~="iron-plate"then return {ok=false,reason="fluid_product_iron_belt_contaminated"}end end end
 live[key(q)]=e
end
local function inside(p,m)return math.abs(p.x-m.position.x)<=1.5 and math.abs(p.y-m.position.y)<=1.5 end
for _,q in ipairs(args.arms)do local e=exact(q);local destination=live[q.to_machine]
 if not e or e.direction~=q.direction or not e.is_connected_to_electric_network()or e.energy<=0 or not destination or not inside(e.drop_position,destination)then return {ok=false,reason="fluid_product_native_input_arm_changed"}end
 if q.from_machine then local origin=live[q.from_machine];if not origin or not inside(e.pickup_position,origin)then return {ok=false,reason="fluid_product_precursor_direct_feed_missing"}end
 else local origin=target(q.from_position,"transport-belt");if not origin or not live[key(origin)]or origin.direction~=4 or math.abs(e.pickup_position.x-origin.position.x)>.01 or math.abs(e.pickup_position.y-origin.position.y)>.01 then return {ok=false,reason="fluid_product_iron_pickup_missing"}end end
end
local outlet=exact(args.output);local amount=outlet and(outlet.get_fluid_contents()[args.product]or 0)or 0
if amount<=0 then return {ok=false,reason="fluid_product_actual_fixed_output_required"}end
return {ok=true,tick=game.tick,output_unit=outlet.unit_number,output_amount=amount,machines=units,fluid_amounts=fluids,reciprocal_edges=#args.edges}
"""
