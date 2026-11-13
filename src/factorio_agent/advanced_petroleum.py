"""Parallel advanced refining with reserved heavy/light stock and surplus cracking.

No existing source, recipe or pipe is cut over.  Inputs must be actual published,
unclaimed distribution outlets.  ``prepare`` is read-only; only ``step`` persists
the normal builder's work and emits the narrowly guarded circuit action.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .chemical_production import plan_typed_fluid_connection, fluid_crossing_proof, chemical_plan_isolation, _handoff
from .fluid_handoffs import bound_handoffs
from .industrial import PlanningBudget
from .petroleum import pipeline_extent
from .planning import overlap, preflight, survey_slots
from .power_grid import plan_connection as power_connection
from .radar_network import RADAR_POWER_LUA, planned_power_watts
from .smelting import merge_entities
from .transport import VECTORS, xy, surface_pipe_proof, unbuilt_external_keepouts


INPUT_KEYS = {"crude-oil": "advanced-crude", "water": "advanced-water"}
OUTPUT_KEYS = {"heavy-oil": "heavy-oil", "light-oil": "light-oil", "petroleum-gas": "advanced-petroleum-gas"}
RECIPES = ("advanced-oil-processing", "heavy-oil-cracking", "light-oil-cracking")
OWNER = "advanced-petroleum"


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _same(obs, value, catalog):
    return (
        obs.get("ok", True)
        and bool(obs.get("world_id"))
        and value.get("world_id") == obs["world_id"]
        and value.get("catalog_fingerprint") == catalog.fingerprint
    )


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _amounts(rows):
    result = {}
    for row in rows:
        if row.get("type") != "fluid" or row["name"] in result:
            raise ValueError("unique fluid-only refinery/cracking recipe required")
        amount = _yield(row)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("positive deterministic fluid yield required")
        result[row["name"]] = amount
    return result


def advanced_petroleum_rates(catalog):
    rows = [catalog.recipes[n] for n in RECIPES]
    raw, products = _amounts(rows[0]["ingredients"]), _amounts(rows[0]["products"])
    if set(raw) != {"crude-oil", "water"} or set(products) != set(OUTPUT_KEYS):
        raise ValueError("advanced refinery requires crude/water and all three oil products")
    rates = []
    for recipe, machine in zip(rows, ("oil-refinery", "chemical-plant", "chemical-plant")):
        p = catalog.entities[machine]
        if recipe["category"] not in p["crafting_categories"]:
            raise ValueError("native machine does not support recipe category")
        seconds, speed = recipe["energy"], p["crafting_speed"]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (seconds, speed)):
            raise ValueError("positive native crafting time and speed required")
        rates.append(60 * speed / seconds)
    hi, ho = _amounts(rows[1]["ingredients"]), _amounts(rows[1]["products"])
    li, lo = _amounts(rows[2]["ingredients"]), _amounts(rows[2]["products"])
    if (
        set(hi) != {"water", "heavy-oil"}
        or set(ho) != {"light-oil"}
        or set(li) != {"water", "light-oil"}
        or set(lo) != {"petroleum-gas"}
    ):
        raise ValueError("supported water-fed heavy-to-light-to-gas cracking required")
    gross = {k: v * rates[0] for k, v in products.items()}
    hc = gross["heavy-oil"] / hi["heavy-oil"]
    light = gross["light-oil"] + hc * ho["light-oil"]
    lc = light / li["light-oil"]
    if hc > rates[1] + 1e-9 or lc > rates[2] + 1e-9:
        raise ValueError("one cracking plant per stage cannot clear all refinery byproducts")
    return {
        "refinery_count": 1,
        "cracking_plant_count": 2,
        "crude_per_minute": raw["crude-oil"] * rates[0],
        "water_per_minute_at_full_cracking": raw["water"] * rates[0] + hc * hi["water"] + lc * li["water"],
        "gross_per_minute": gross,
        "gas_per_minute_at_full_cracking": gross["petroleum-gas"] + lc * lo["petroleum-gas"],
        "heavy_cracking_crafts_required": hc,
        "light_cracking_crafts_required": lc,
        "electric_kw": sum(
            catalog.entities[n]["energy_usage"] * 60 / 1000
            for n in ("oil-refinery", "chemical-plant", "chemical-plant")
        ),
        "scope": "catalog nameplate; three outlets share one crude supply, not three independent capacities",
    }


def _machine_port(catalog, spec, fluid, side):
    recipe = catalog.recipes[spec["recipe"]]
    selected = [r for r in recipe["ingredients" if side == "input" else "products"] if r.get("type") == "fluid"]
    row = next(r for r in selected if r["name"] == fluid)
    ordinal = row.get("fluidbox_index", selected.index(row) + 1)
    boxes = sorted(
        (b for b in catalog.entities[spec["name"]]["fluidbox_prototypes"] if b["production_type"] == side),
        key=lambda b: b["index"],
    )
    if type(ordinal) is not int or not 1 <= ordinal <= len(boxes):
        raise ValueError("native recipe fluid ordinal unavailable")
    box = boxes[ordinal - 1]
    cs = [c for c in box["pipe_connections"] if c.get("connection_type", "normal") == "normal"]
    if len(cs) != 1 or len(cs[0]["positions"]) != 4:
        raise ValueError("one cardinal physical fluid connection required")
    c = cs[0]
    p = c["positions"][0]
    dx, dy = VECTORS[c["direction"]]
    return {"fluidbox_index": box["index"], "offset": (p["x"] + dx, p["y"] + dy)}


def advanced_refinery_core(catalog, x, y):
    """Fixed parallel block. Cracking enables above 500 units in its own tank."""
    rates = advanced_petroleum_rates(catalog)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 != 0.5 for v in (x, y)):
        raise ValueError("half-tile refinery anchor required")
    machines = [
        entity("oil-refinery", x, y, recipe=RECIPES[0]),
        entity("chemical-plant", x - 5, y - 10, recipe=RECIPES[1]),
        entity("chemical-plant", x + 7, y - 14, recipe=RECIPES[2]),
    ]
    expected = [
        (
            {"water": (-1, 3), "crude-oil": (1, 3)},
            {"heavy-oil": (-2, -3), "light-oil": (0, -3), "petroleum-gas": (2, -3)},
        ),
        ({"water": (-1, -2), "heavy-oil": (1, -2)}, {"light-oil": (-1, 2)}),
        ({"water": (-1, -2), "light-oil": (1, -2)}, {"petroleum-gas": (-1, 2)}),
    ]
    machine_boxes = {}
    for m, (inputs, outputs) in zip(machines, expected):
        mapping = {}
        for side, fluids in (("input", inputs), ("output", outputs)):
            for fluid, offset in fluids.items():
                binding = _machine_port(catalog, m, fluid, side)
                if binding["offset"] != offset:
                    raise ValueError("installed refinery/cracker port geometry changed")
                mapping[fluid] = binding["fluidbox_index"]
        machine_boxes[_key(m)] = mapping
    tank_proto = catalog.entities["storage-tank"]
    if tank_proto["selection_box"] != {"left_top": {"x": -1.5, "y": -1.5}, "right_bottom": {"x": 1.5, "y": 1.5}}:
        raise ValueError("three by three tank required")
    box = tank_proto["fluidbox_prototypes"][0]
    actual_ports = {
        (c["positions"][0]["x"] + VECTORS[c["direction"]][0], c["positions"][0]["y"] + VECTORS[c["direction"]][1])
        for c in box["pipe_connections"]
    }
    if actual_ports != {(-1, -2), (2, 1), (1, 2), (-2, -1)} or box["volume"] <= 1000:
        raise ValueError("native tank port geometry/capacity changed")
    groups = {k: {} for k in ("crude-oil", "water", *OUTPUT_KEYS)}
    pairs = []

    def line(fluid, vertices):
        for a, b in zip(vertices, vertices[1:]):
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError("cardinal internal pipe segments required")
            dx = (b[0] > a[0]) - (b[0] < a[0])
            dy = (b[1] > a[1]) - (b[1] < a[1])
            for i in range(abs(b[0] - a[0]) + abs(b[1] - a[1]) + 1):
                p = (x + a[0] + i * dx, y + a[1] + i * dy)
                groups[fluid][p] = entity("pipe", *p)

    line("crude-oil", [(-20, 6), (1, 6), (1, 3)])
    line("water", [(-20, 3), (-1, 3)])
    line("water", [(-18, 3), (-18, -16), (6, -16)])
    line("water", [(-6, -16), (-6, -12)])
    line("heavy-oil", [(-2, -3), (-2, -4), (-12, -4)])
    line("heavy-oil", [(-14, -8), (-14, -14), (-4, -14), (-4, -12)])
    line("heavy-oil", [(-15, -7), (-20, -7)])
    line("light-oil", [(0, -3), (0, -6)])
    line("light-oil", [(-6, -8), (-3, -8), (-3, -9)])
    line("light-oil", [(-2, -10), (-2, -21)])
    line("light-oil", [(-2, -18), (8, -18), (8, -16)])
    line("petroleum-gas", [(2, -3), (10, -3), (10, -6), (11, -6)])
    line("petroleum-gas", [(6, -12), (6, -10), (12, -10), (12, -7)])
    line("petroleum-gas", [(15, -4), (20, -4)])

    def buried(fluid, first, last, direction):
        a = (x + first[0], y + first[1])
        b = (x + last[0], y + last[1])
        dx, dy = VECTORS[direction]
        length = int(abs(a[0] - b[0]) + abs(a[1] - b[1]))
        for i in range(1, length):
            groups[fluid].pop((a[0] + i * dx, a[1] + i * dy))
        groups[fluid][a] = entity("pipe-to-ground", *a, (direction + 8) % 16)
        groups[fluid][b] = entity("pipe-to-ground", *b, direction)
        pairs.append({"first": list(a), "last": list(b), "direction": direction, "span": length, "fluid": fluid})

    buried("water", (-6, -15), (-6, -13), 8)
    buried("water", (-4, -16), (0, -16), 4)
    buried("water", (-18, -9), (-18, -5), 8)
    tanks = {
        "heavy-oil": entity("storage-tank", x - 13, y - 6),
        "light-oil": entity("storage-tank", x - 1, y - 8),
        "petroleum-gas": entity("storage-tank", x + 13, y - 5),
    }
    for fluid, tank in tanks.items():
        groups[fluid][xy(tank)] = tank
    poles = [
        entity("small-electric-pole", x + a, y + b)
        for a, b in ((3, 0), (3, 3), (-4, 1), (-7, -3), (-7, -10), (8, -4), (10, -9), (9, -14), (-9, -7), (3, -12))
    ]
    controls = [
        {
            "fluid": f,
            "tank": tanks[f],
            "machine": machines[i],
            "reserve": 500,
            "wires": [{"a": tanks[f], "b": poles[p]}, {"a": poles[p], "b": machines[i]}],
        }
        for f, i, p in (("heavy-oil", 1, 8), ("light-oil", 2, 9))
    ]

    def port(fluid, a, b, facing, side):
        return {
            "kind": "fluid",
            "item": fluid,
            "name": "pipe",
            "position": {"x": x + a, "y": y + b},
            "facing": facing,
            "direction": side,
            "fluidbox_index": 1,
        }

    inputs = {"water": port("water", -20, 3, 4, "input"), "crude-oil": port("crude-oil", -20, 6, 4, "input")}
    outputs = {
        "heavy-oil": port("heavy-oil", -20, -7, 12, "output"),
        "light-oil": port("light-oil", -2, -21, 0, "output"),
        "petroleum-gas": port("petroleum-gas", 20, -4, 4, "output"),
    }
    access = [
        entity(
            "pipe", p["position"]["x"] + VECTORS[p["facing"]][0] * i, p["position"]["y"] + VECTORS[p["facing"]][1] * i
        )
        for p in outputs.values()
        for i in (1, 2, 3)
    ]
    es = [e for g in groups.values() for e in g.values()] + machines + poles
    return _plan(
        es,
        list(inputs.values()) + list(outputs.values()),
        machines=machines,
        tanks=tanks,
        fluid_groups={f: list(g.values()) for f, g in groups.items()},
        inputs=inputs,
        outputs=outputs,
        machine_boxes=machine_boxes,
        controls=controls,
        underground_pairs=pairs,
        output_access=access,
        power_port=poles[0],
        capacity=rates,
        anchor={"x": x, "y": y},
        reserved_area=[[x - 21.5, y - 22.5], [x + 22.5, y + 18.5]],
        expansion_keepouts=[entity("oil-refinery", x + 8, y + 12)],
        flow="south inputs to north refinery outputs; surplus cracking above the refinery",
        growth="south; future refinery is reserved, not an installed capacity claim",
        layout_policy="parallel-advanced-refinery-v1",
    )


def fluid_graph(core, routes=None, sources=None):
    """Physical prototype-index graph, including tank ports and buried pairs."""
    routes, sources = routes or {}, sources or {}
    groups = {
        f: merge_entities(es, routes.get(f, {}).get("entities", []), [sources[f]] if f in sources else [])
        for f, es in core["fluid_groups"].items()
    }
    pairs = core["underground_pairs"] + [p for r in routes.values() for p in r["underground_pairs"]]
    edges, seen = [], set()

    def add(a, ai, b, bi, fluid):
        identity = tuple(sorted(((_key(a), ai), (_key(b), bi))))
        if identity not in seen:
            seen.add(identity)
            edges.append({"a": dict(a, fluidbox_index=ai), "b": dict(b, fluidbox_index=bi), "fluid": fluid})

    for fluid, nodes in groups.items():
        by = {xy(e): e for e in nodes}
        for e in nodes:
            if e["name"] == "storage-tank":
                for dx, dy in ((-1, -2), (2, 1), (1, 2), (-2, -1)):
                    other = by.get((xy(e)[0] + dx, xy(e)[1] + dy))
                    if other:
                        add(e, 1, other, 1, fluid)
                continue
            for d, (dx, dy) in VECTORS.items():
                other = by.get((xy(e)[0] + dx, xy(e)[1] + dy))
                if (
                    other
                    and other["name"] != "storage-tank"
                    and (e["name"] != "pipe-to-ground" or e["direction"] == d)
                    and (other["name"] != "pipe-to-ground" or other["direction"] == (d + 8) % 16)
                ):
                    add(e, 1, other, 1, fluid)
        for p in pairs:
            a, b = by.get(tuple(p["first"])), by.get(tuple(p["last"]))
            if a and b:
                add(a, 1, b, 1, fluid)
        for m in core["machines"]:
            index = core["machine_boxes"][_key(m)].get(fluid)
            if index is None:
                continue
            # These offsets were derived and checked against this catalog above.
            offsets = (
                {
                    "water": (-1, 3),
                    "crude-oil": (1, 3),
                    "heavy-oil": (-2, -3),
                    "light-oil": (0, -3),
                    "petroleum-gas": (2, -3),
                }
                if m["name"] == "oil-refinery"
                else {
                    "water": (-1, -2),
                    "heavy-oil" if m["recipe"] == RECIPES[1] else "light-oil": (1, -2),
                    "light-oil" if m["recipe"] == RECIPES[1] else "petroleum-gas": (-1, 2),
                }
            )
            dx, dy = offsets[fluid]
            other = by.get((xy(m)[0] + dx, xy(m)[1] + dy))
            if other:
                add(m, index, other, 1, fluid)
    return groups, edges


def audit_advanced_core(core, catalog, builder):
    errors = []
    footprints = [builder._occupied_by_plan([e]) for e in core["entities"]]
    if sum(map(len, footprints)) != len(set().union(*footprints)):
        errors.append("overlapping_entity_footprints")
    groups, edges = fluid_graph(core)
    ownership = {xy(e): f for f, es in groups.items() for e in es if e["name"] != "storage-tank"}
    for fluid, es in groups.items():
        for e in es:
            if e["name"] == "storage-tank":
                continue
            for d, (dx, dy) in VECTORS.items():
                other = ownership.get((xy(e)[0] + dx, xy(e)[1] + dy))
                if other and other != fluid and (e["name"] != "pipe-to-ground" or e["direction"] == d):
                    q = next(q for q in groups[other] if xy(q) == (xy(e)[0] + dx, xy(e)[1] + dy))
                    if q["name"] != "pipe-to-ground" or q["direction"] == (d + 8) % 16:
                        errors.append("different_fluid_surface_networks_touch")
        keys = {_key(e) for e in es}
        adjacency = {k: set() for k in keys}
        for edge in edges:
            if edge["fluid"] != fluid:
                continue
            a, b = _key(edge["a"]), _key(edge["b"])
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        reached, pending = set(), [next(iter(keys))]
        while pending:
            k = pending.pop()
            if k in reached:
                continue
            reached.add(k)
            pending.extend(adjacency[k] - reached)
        if not keys <= reached:
            errors.append("disconnected_" + fluid)
        for m in core["machines"]:
            if fluid in core["machine_boxes"][_key(m)] and _key(m) not in reached:
                errors.append("missing_machine_port_" + fluid)
    poles = [e for e in core["entities"] if e["name"] == "small-electric-pole"]
    reached, pending = set(), [0]
    while pending:
        i = pending.pop()
        if i in reached:
            continue
        reached.add(i)
        pending.extend(
            j for j in range(len(poles)) if j not in reached and math.dist(xy(poles[i]), xy(poles[j])) <= 7.5
        )
    if len(reached) != len(poles):
        errors.append("internal_power_islands")
    for m in core["machines"]:
        half = 2.5 if m["name"] == "oil-refinery" else 1.5
        if not any(abs(xy(p)[0] - xy(m)[0]) < half + 2.5 and abs(xy(p)[1] - xy(m)[1]) < half + 2.5 for p in poles):
            errors.append("unpowered_machine_geometry")
    for control in core["controls"]:
        if any(math.dist(xy(e["a"]), xy(e["b"])) > 7.5 for e in control["wires"]):
            errors.append("circuit_wire_too_long")
    return {"ok": not errors, "errors": sorted(set(errors)), "entity_count": len(core["entities"])}


SOURCE_LUA = r"""
local x=helpers.json_to_table(PAYLOAD);local out={}
if not d or d.world_id~=x.world_id then return {ok=false,reason="advanced_source_world_changed"}end
for fluid,q in pairs(x.sources)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="advanced_input_identity_changed",fluid=fluid}end
 for name in pairs(e.get_fluid_contents())do if name~=fluid then return {ok=false,reason="advanced_input_contaminated",fluid=fluid}end end
 if(e.get_fluid_contents()[fluid]or 0)<=0 or not e.has_fluid_segment(1)then return {ok=false,reason="advanced_input_not_natively_supplied",fluid=fluid}end
 out[fluid]={extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}
end
return {ok=true,segments=out}
"""


class AdvancedPetroleum:
    def __init__(self, game, builder, catalog, perimeter):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.path = Path(game.cfg.runtime_dir) / "advanced-petroleum.json"
        self.sources_path = self.path.parent / "fluid-sources.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.last_proof_failure = None

    def _save(self):
        _atomic_json(self.path, self.state)

    def _sources(self, obs, game=None):
        registry = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
        if not _same(obs, registry, self.catalog):
            return {"ok": False, "reason": "advanced_fluid_registry_identity_changed"}
        sources = {}
        for fluid, alias in INPUT_KEYS.items():
            source = registry.get("sources", {}).get(alias)
            if not source:
                return {
                    "ok": False,
                    "reason": "advanced_independent_fluid_source_required",
                    "source_key": alias,
                    "fluid": fluid,
                }
            if (
                source.get("kind") != "fluid"
                or source.get("item") != fluid
                or source.get("direction") != "output"
                or source.get("name") != "pipe"
                or source.get("facing") not in VECTORS
                or source.get("fluidbox_index") != 1
                or source.get("world_id") != obs["world_id"]
                or type(source.get("unit_number")) is not int
                or source["unit_number"] <= 0
                or not source.get("defense_scope_id")
            ):
                return {"ok": False, "reason": "advanced_exact_published_source_required", "source_key": alias}
            for owner, rows in registry.get("handoffs", {}).items():
                for row in rows.values():
                    if row.get("source") == source and (owner != OWNER or not self.state):
                        return {
                            "ok": False,
                            "reason": "advanced_input_already_claimed",
                            "source_key": alias,
                            "consumer": owner,
                        }
            sources[fluid] = deepcopy(source)
        if self.state and sources != self.state["sources"]:
            return {"ok": False, "reason": "advanced_prepared_input_changed"}
        native = (game or self.game).query(
            SOURCE_LUA.replace("PAYLOAD", _payload({"world_id": obs["world_id"], "sources": sources}))
        )
        return dict(native, sources=sources) if native.get("ok") else native

    def _prerequisites(self, obs):
        recipes = (
            *RECIPES,
            "oil-refinery",
            "chemical-plant",
            "storage-tank",
            "pipe",
            "pipe-to-ground",
            "small-electric-pole",
        )
        if not all(obs.get("technologies", {}).get(t) for t in ("advanced-oil-processing", "circuit-network")):
            return {"ok": False, "reason": "advanced_oil_and_circuit_research_required"}
        missing = [r for r in recipes if not obs.get("enabled_recipes", {}).get(r)]
        if missing:
            return {"ok": False, "reason": "advanced_native_recipes_locked", "recipes": missing}
        return None

    def prepare(self, obs):
        """Read-only whole layout, both input routes, isolation, power and defense."""
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return {"ok": False, "reason": "advanced_valid_world_observation_required"}
        gate = self._prerequisites(obs)
        if gate:
            return gate
        if self.state:
            return (
                deepcopy(self.state)
                if _same(obs, self.state, self.catalog)
                else {"ok": False, "reason": "advanced_world_or_catalog_changed"}
            )
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if not _same(obs, board, self.catalog):
            return {"ok": False, "reason": "advanced_board_identity_changed"}
        if "advanced_petroleum" in board.get("blocks", {}):
            return {"ok": False, "reason": "advanced_reservation_without_checkpoint"}
        budget = PlanningBudget(self.game)
        sources = self._sources(obs, budget.game)
        if not sources.get("ok"):
            return sources
        areas = [b["reserved_area"] for b in board["blocks"].values() if b.get("reserved_area")]
        areas += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        reference = board["blocks"].get("basic_petroleum", {}).get("reserved_area")
        if not reference:
            production = [
                b["reserved_area"] for k, b in board["blocks"].items() if b.get("reserved_area") and "mining" not in k
            ]
            if not production:
                return {"ok": False, "reason": "advanced_existing_production_district_required"}
            reference = production[0]
        bx = math.ceil(reference[1][0] / 16) * 16 + 24.5
        by = math.floor(reference[0][1] / 16) * 16 + 0.5
        future_hard = unbuilt_external_keepouts(self.builder, obs, board["blocks"])
        candidates = []
        try:
            for dx, dy in ((0, 0), (48, 0), (0, -48), (48, -48), (0, 48), (48, 48), (96, 0), (96, -48)):
                core = advanced_refinery_core(self.catalog, bx + dx, by + dy)
                audit = audit_advanced_core(core, self.catalog, self.builder)
                if not audit["ok"]:
                    return {"ok": False, "reason": "advanced_core_audit_failed", "audit": audit}
                if any(overlap(core["reserved_area"], a) for a in areas):
                    continue
                keepout = self.builder._occupied_by_plan(
                    core["entities"] + core["output_access"] + core["expansion_keepouts"]
                )
                if keepout & future_hard:
                    continue
                if any(
                    overlap(
                        [
                            [p["position"]["x"] - 0.49, p["position"]["y"] - 0.49],
                            [p["position"]["x"] + 0.49, p["position"]["y"] + 0.49],
                        ],
                        a,
                    )
                    for p in core["output_access"]
                    for a in areas
                ):
                    continue
                candidates.append(core)
        except (KeyError, ValueError, TypeError, StopIteration) as exc:
            return {"ok": False, "reason": "advanced_catalog_geometry_unsupported", "detail": str(exc)}
        if not candidates:
            return {"ok": False, "reason": "advanced_no_clear_production_reservation"}
        land = survey_slots(budget.game, [[c["reserved_area"]] for c in candidates])
        if not land.get("ok"):
            return land
        failures = []
        for i in land.get("legal", [])[:4]:
            if budget.expired():
                return budget.failure(failures=failures[-4:])
            core = candidates[i - 1]
            routes = {}
            prior = []
            for fluid in ("crude-oil", "water"):
                route = plan_typed_fluid_connection(
                    budget.game, self.builder, obs, sources["sources"][fluid], core["inputs"][fluid], core, prior
                )
                if not route.get("ok"):
                    failures.append({"stage": "input_route", "fluid": fluid, "reason": route.get("reason")})
                    break
                routes[fluid] = route
                prior += route["entities"]
            if len(routes) != 2:
                continue
            groups, edges = fluid_graph(core, routes, sources["sources"])
            maximum = min(r["maximum_underground_span"] for r in routes.values())
            if any(p["span"] > maximum for p in core["underground_pairs"]):
                failures.append({"stage": "internal_span", "reason": "advanced_native_pipe_span_too_short"})
                continue
            all_pairs = core["underground_pairs"] + [p for r in routes.values() for p in r["underground_pairs"]]
            fluid_plans = {}
            for f, nodes in groups.items():
                points = {xy(e) for e in nodes}
                fluid_plans[f] = {
                    "nodes": nodes,
                    "source": sources["sources"].get(f, {"unit_number": -1}),
                    "route": {
                        "maximum_underground_span": maximum,
                        "underground_pairs": [
                            p for p in all_pairs if tuple(p["first"]) in points and tuple(p["last"]) in points
                        ],
                    },
                }
            isolation = chemical_plan_isolation(budget.game, fluid_plans, dict(core, machine=core["machines"][0]))
            if isolation.get("ok"):
                isolation = budget.game.query(
                    """local q=helpers.json_to_table("""
                    + _payload(core["machines"])
                    + """)
for _,m in ipairs(q)do local r=m.name=="oil-refinery"and 3.49 or 2.49;local p=m.position
 for _,e in pairs(s.find_entities_filtered{area={{p.x-r,p.y-r},{p.x+r,p.y+r}}})do
  for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="advanced_machine_near_existing_fluid_network"}end
 end
end;return {ok=true}"""
                )
            if not isolation.get("ok"):
                failures.append({"stage": "isolation", "reason": isolation.get("reason")})
                continue
            extents = {
                f: pipeline_extent(
                    sources["segments"][f]["extent"],
                    routes[f]["entities"] + core["fluid_groups"][f],
                    sources["segments"][f]["limit"],
                )
                for f in INPUT_KEYS
            }
            if not all(v.get("ok") for v in extents.values()):
                failures.append({"stage": "pipeline_extent", "reason": "advanced_input_needs_explicit_pump"})
                continue
            route_es = merge_entities(*(r["entities"] for r in routes.values()))
            power = power_connection(
                budget.game,
                self.builder,
                obs,
                core["power_port"]["position"],
                merge_entities(
                    route_es,
                    [e for e in core["entities"] if e["name"] != "small-electric-pole"],
                    core["expansion_keepouts"],
                ),
            )
            if not power.get("ok"):
                failures.append({"stage": "power", "reason": power.get("reason")})
                continue
            es = merge_entities(core["entities"], route_es, power["entities"])
            check = preflight(budget.game, self.builder, obs, es + core["output_access"] + core["expansion_keepouts"])
            if not check.get("ok"):
                failures.append({"stage": "whole_preflight", "reason": check.get("reason")})
                continue
            source_pole = next(
                (e for e in obs["entities"] if e.get("unit_number") == power["source_unit_number"]), None
            )
            if not source_pole:
                return {"ok": False, "reason": "advanced_exact_power_source_required"}
            pin = {
                k: deepcopy(source_pole[k])
                for k in ("name", "position", "unit_number", "direction", "electric_network_id")
            }
            pin["world_id"] = obs["world_id"]
            watts = core["capacity"]["electric_kw"] * 1000
            power_gate = budget.game.query(
                RADAR_POWER_LUA.replace(
                    "PAYLOAD",
                    _payload(
                        dict(
                            world_id=obs["world_id"],
                            source=pin,
                            preserved_poles=[pin],
                            scheduled_watts=planned_power_watts(self.catalog, obs, board),
                            additional_watts=watts,
                        )
                    ),
                )
            )
            if not power_gate.get("ok"):
                failures.append({"stage": "power_capacity", "reason": power_gate.get("reason")})
                continue
            scope = OWNER + "-" + _digest([obs["world_id"], core["ports"]])[:16]
            defense = self.perimeter.prepare_coverage(obs, scope, [core["reserved_area"]])
            if not defense.get("ok"):
                failures.append({"stage": "defense", "reason": defense.get("reason")})
                continue
            pairs = core["underground_pairs"] + [p for r in routes.values() for p in r["underground_pairs"]]
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                underground_pairs=pairs,
                preserved_fluid_crossings=[q for r in routes.values() for q in r.get("preserved_fluid_crossings", [])],
                owned_fluid_crossings=[q for r in routes.values() for q in r.get("owned_fluid_crossings", [])],
                clear_obstacles=check["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                layout_policy=core["layout_policy"],
            )
            gaps = {_key(r["first_neighbor"]) for r in routes.values()}
            cold = _plan(
                [e for e in es if _key(e) not in gaps],
                core["ports"],
                clear_obstacles=check["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                underground_pairs=pairs,
                layout_policy="advanced-refinery-isolated-cold-build-v1",
            )
            return dict(
                ok=True,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                phase="cold",
                core=core,
                plan=plan,
                cold_plan=cold,
                routes=routes,
                sources=sources["sources"],
                fluid_nodes=[dict(e, fluid=f) for f, nodes in groups.items() for e in nodes],
                fluid_edges=edges,
                bindings={},
                power_source=pin,
                power_preview=power_gate,
                defense_scope_id=scope,
                defense_preview=defense,
                pipeline_extents=extents,
                prepared_tick=obs.get("tick"),
            )
        return budget.failure("advanced_no_whole_native_refinery_site", failures=failures[-4:])

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        changed = False
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            old = self.state["bindings"].get(_key(spec))
            if old is not None and (not live or live.get("unit_number") != old):
                return {"status": "blocked", "reason": "advanced_paid_asset_missing_or_replaced", "entity": spec}
            if live:
                if type(live.get("unit_number")) is not int or (
                    spec["name"] not in ("pipe", "small-electric-pole")
                    and live.get("direction") != spec.get("direction", 0)
                ):
                    return {"status": "blocked", "reason": "advanced_asset_unit_or_direction_changed", "entity": spec}
                if old is None:
                    self.state["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def _native(self, obs, *, cold=False, controls=False):
        args = dict(
            world_id=obs["world_id"],
            core=self.state["core"],
            bindings=self.state["bindings"],
            nodes=self.state["fluid_nodes"],
            edges=self.state["fluid_edges"],
            sources=self.state["sources"],
            cold=cold,
            configure=False,
            controls=controls,
            handoffs=[],
            gaps=[_key(r["first_neighbor"]) for r in self.state["routes"].values()],
        )
        if not cold:
            registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
            for fluid, p in self.state["core"]["outputs"].items():
                source = dict(
                    p,
                    world_id=obs["world_id"],
                    unit_number=self.state["bindings"].get(_key(p)),
                    defense_scope_id=self.state["defense_scope_id"],
                )
                args["handoffs"] += bound_handoffs(registry, obs, self.catalog, source)
        return self.game.query(ADVANCED_INSPECT_LUA.replace("PAYLOAD", _payload(args)))

    def step(self, obs):
        if self.state and not _same(obs, self.state, self.catalog):
            return {"status": "blocked", "reason": "advanced_world_or_catalog_changed"}
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        gate = self._prerequisites(obs)
        if gate:
            return {"status": "blocked", **gate}
        src = self._sources(obs)
        if not src.get("ok"):
            return {"status": "blocked", **src}
        blocked = self._bind(obs)
        if blocked:
            return blocked
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if not _same(obs, board, self.catalog):
            return {"status": "blocked", "reason": "advanced_board_identity_changed"}
        registered = board["blocks"].get("advanced_petroleum")
        if registered and registered != self.state["plan"]:
            return {"status": "blocked", "reason": "advanced_registered_plan_changed"}
        if any(
            k != "advanced_petroleum"
            and b.get("reserved_area")
            and overlap(self.state["core"]["reserved_area"], b["reserved_area"])
            for k, b in board["blocks"].items()
        ):
            return {"status": "blocked", "reason": "advanced_reservation_conflict"}
        if not registered:
            board["blocks"]["advanced_petroleum"] = self.state["plan"]
            _atomic_json(board_path, board)
        power = self.game.query(
            RADAR_POWER_LUA.replace(
                "PAYLOAD",
                _payload(
                    dict(
                        world_id=obs["world_id"],
                        source=self.state["power_source"],
                        preserved_poles=[self.state["power_source"]],
                        scheduled_watts=planned_power_watts(self.catalog, obs, board),
                        additional_watts=0,
                    )
                ),
            )
        )
        if not power.get("ok"):
            return {"status": "waiting", "reason": "advanced_waiting_for_reserved_native_power", "evidence": power}
        cover = self.perimeter.ensure_coverage(
            obs, self.state["defense_scope_id"], [self.state["core"]["reserved_area"]]
        )
        if cover is not None:
            return cover
        for guard in (
            surface_pipe_proof(self.game, obs, self.state["plan"]),
            fluid_crossing_proof(self.game, obs, self.state["plan"], owned_bindings=self.state["bindings"]),
        ):
            if not guard.get("ok"):
                return {"status": "blocked", "reason": "advanced_preserved_crossing_changed", "evidence": guard}
        for fluid, route in self.state["routes"].items():
            neighbor = {k: v for k, v in route["first_neighbor"].items() if k != "unit_number"}
            claim = _handoff(self.game, self.catalog, obs, OWNER, fluid, self.state["sources"][fluid], neighbor)
            if not claim.get("ok"):
                return {"status": "blocked", "reason": "advanced_input_handoff_not_authorized", "evidence": claim}
        if not self.state.get("construction_authorized"):
            whole = preflight(
                self.game,
                self.builder,
                obs,
                self.state["plan"]["entities"]
                + self.state["core"]["output_access"]
                + self.state["core"]["expansion_keepouts"],
            )
            if not whole.get("ok"):
                return {"status": "blocked", "reason": "advanced_post_defense_preflight_failed", "evidence": whole}
            self.state["construction_authorized"] = True
            self._save()
        if self.state["phase"] == "cold":
            result = self.builder.ensure_plan(obs, self.state["cold_plan"])
            if result.get("status") != "succeeded":
                return result
            baseline = self._native(obs, cold=True)
            if not baseline.get("ok"):
                return {"status": "blocked", "reason": "advanced_cold_native_baseline_failed", "evidence": baseline}
            if not self.state.get("baseline"):
                self.state["baseline"] = {str(q["unit_number"]): q["products_finished"] for q in baseline["machines"]}
                self._save()
            controls = self._native(obs, cold=True, controls=True)
            if not controls.get("ok"):
                if controls.get("reason") != "advanced_circuit_controls_not_configured":
                    return {"status": "blocked", "reason": "advanced_cold_control_guard_failed", "evidence": controls}
                return {
                    "type": "configure_advanced_petroleum",
                    "expected_world_id": obs["world_id"],
                    "expected_actor_unit": obs.get("actor_unit_number"),
                    "expected_catalog_fingerprint": self.catalog.fingerprint,
                    "plan_sha256": _digest(self.state["plan"]),
                    "reason": "configure paid surplus cracking before either input is connected",
                }
            self.state["phase"] = "feed"
            self._save()
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        for fluid, route in self.state["routes"].items():
            neighbor = dict(
                route["first_neighbor"], unit_number=self.state["bindings"].get(_key(route["first_neighbor"]))
            )
            if type(neighbor["unit_number"]) is not int:
                return {"status": "waiting", "reason": "advanced_input_neighbor_not_bound"}
            bound = _handoff(
                self.game, self.catalog, obs, OWNER, fluid, self.state["sources"][fluid], neighbor, bind=True
            )
            if not bound.get("ok"):
                return {"status": "blocked", "reason": "advanced_input_handoff_not_bound", "evidence": bound}
        evidence = self.proof(obs)
        if not evidence:
            return {
                "status": "waiting",
                "reason": "advanced_waiting_for_real_refining_and_both_surplus_cracking_products",
                "evidence": self.last_proof_failure,
            }
        registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
        if not _same(obs, registry, self.catalog):
            return {"status": "blocked", "reason": "advanced_publish_registry_changed"}
        for fluid, alias in OUTPUT_KEYS.items():
            old = registry["sources"].get(alias)
            new = evidence["sources"][fluid]
            if old and old != new:
                return {"status": "blocked", "reason": "advanced_output_alias_already_owned", "source_key": alias}
        for fluid, alias in OUTPUT_KEYS.items():
            registry["sources"][alias] = evidence["sources"][fluid]
        _atomic_json(self.sources_path, registry)
        self.state.update(phase="verified", proof=evidence)
        self._save()
        return {
            "status": "succeeded",
            "reason": "advanced_parallel_refinery_and_surplus_cracking_commissioned",
            "evidence": evidence,
        }

    def proof(self, obs):
        self.last_proof_failure = None
        if not self.state or not _same(obs, self.state, self.catalog) or not self.state.get("baseline"):
            return None
        blocked = self._bind_readonly(obs)
        if blocked:
            self.last_proof_failure = blocked
            return None
        board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
        if not _same(obs, board, self.catalog) or board["blocks"].get("advanced_petroleum") != self.state["plan"]:
            self.last_proof_failure = {"reason": "advanced_registered_plan_changed"}
            return None
        src = self._sources(obs)
        if not src.get("ok"):
            self.last_proof_failure = src
            return None
        for guard in (
            surface_pipe_proof(self.game, obs, self.state["plan"]),
            fluid_crossing_proof(self.game, obs, self.state["plan"], owned_bindings=self.state["bindings"]),
        ):
            if not guard.get("ok"):
                self.last_proof_failure = guard
                return None
        cover = self.perimeter.coverage_proof(obs, self.state["defense_scope_id"])
        if not cover:
            self.last_proof_failure = {"reason": "advanced_defense_coverage_missing"}
            return None
        native = self._native(obs, controls=True)
        if not native.get("ok"):
            self.last_proof_failure = native
            return None
        if any(
            q["products_finished"] <= self.state["baseline"].get(str(q["unit_number"]), math.inf)
            for q in native["machines"]
        ):
            self.last_proof_failure = {"reason": "advanced_all_three_native_product_counters_must_advance"}
            return None
        outputs = {
            f: dict(
                p,
                world_id=obs["world_id"],
                unit_number=self.state["bindings"][_key(p)],
                defense_scope_id=self.state["defense_scope_id"],
            )
            for f, p in self.state["core"]["outputs"].items()
        }
        return {
            "tick": native["tick"],
            "sources": outputs,
            "native": native,
            "capacity": self.state["core"]["capacity"],
            "defense_coverage": cover,
            "scope": "native production, isolated typed networks and surplus controls; no measured sustained throughput claim",
        }

    def _bind_readonly(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        for q in self.state["plan"]["entities"]:
            e = actual.get((q["name"], *xy(q)))
            unit = self.state["bindings"].get(_key(q))
            if type(unit) is not int or not e or e.get("unit_number") != unit:
                return {"reason": "advanced_exact_paid_asset_missing_or_changed", "entity": q}
        return None


ADVANCED_ENTITIES_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
local changed=false
local function reject(reason)return {ok=false,reason=changed and"advanced_configuration_outcome_unknown"or reason}end
if not d or d.world_id~=x.world_id then return reject("advanced_native_world_changed")end
if x.configure and(not a or not a.valid or a.unit_number~=x.actor_unit)then return reject("advanced_native_actor_changed")end
local live={};local maps={};local indices={};local fluids={};local gaps={};local sources={};local counts={}
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local function id(e,index)return e.unit_number..":"..index end
for _,k in ipairs(x.gaps or{})do gaps[k]=true end
for fluid,q in pairs(x.sources)do sources[q.unit_number]=fluid end
local specs={};for _,q in ipairs(x.nodes)do specs[#specs+1]=q end
for _,q in ipairs(x.core.machines)do specs[#specs+1]=q end
for _,q in ipairs(specs)do local k=key(q);local e=target(q.position,q.name)
 local unit=q.unit_number or x.bindings[k]
 if not e and x.cold and gaps[k]then
 else
  if not unit or not e or not e.valid or e.unit_number~=unit or e.force~=f or e.surface~=s
   or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"then return reject("advanced_native_asset_identity_changed")end
  if(q.name=="pipe-to-ground"or q.name=="storage-tank"or q.name=="oil-refinery"or q.name=="chemical-plant")and e.direction~=(q.direction or 0)then return reject("advanced_native_direction_changed")end
  local allowed={};local physical={}
  if q.recipe then
   local recipe=e.get_recipe()
   if not recipe or recipe.name~=q.recipe or not e.is_connected_to_electric_network()or e.energy<=0 then return reject("advanced_recipe_or_power_changed")end
   if x.cold and e.products_finished~=0 then return reject("advanced_new_machine_not_cold")end
   counts[#counts+1]={unit_number=e.unit_number,products_finished=e.products_finished,recipe=recipe.name}
   for fluid,index in pairs(x.core.machine_boxes[k])do physical[index]=fluid;allowed[fluid]=true end
  else physical[1]=q.fluid;allowed[q.fluid]=true end
  for fluid,amount in pairs(e.get_fluid_contents())do
   if not allowed[fluid]then return reject("advanced_native_fluid_contamination")end
   if x.cold and not sources[unit]and amount>0 then return reject("advanced_new_network_not_cold")end
  end
  local mapping={};local active={};local maximum=q.name=="oil-refinery"and 5 or(q.name=="chemical-plant"and 4 or 1)
  for index=1,maximum do local ok,proto=pcall(function()return e.get_fluid_box_prototype(index)end)
   if ok and proto then
    local ps=proto.index and{proto}or proto;local expected=nil
    for _,p in pairs(ps)do
     local fluid=physical[p.index]
     if not fluid or(expected and fluid~=expected)or mapping[p.index]then return reject("advanced_native_storage_mapping_changed")end
     expected=fluid;mapping[p.index]=index
    end
    if not expected then return reject("advanced_native_storage_mapping_changed")end
    local content=e.get_fluid(index)
    if content and content.amount>0 and content.name~=expected then return reject("advanced_wrong_runtime_fluid_box")end
    active[#active+1]=index
   end
  end
  for index in pairs(physical)do if not mapping[index]then return reject("advanced_runtime_fluid_box_missing")end end
  if not q.recipe then
   if not e.has_fluid_segment(1)then return reject("advanced_native_segment_missing")end
   local box=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
   if box.right_bottom.x-box.left_top.x>limit or box.right_bottom.y-box.left_top.y>limit then return reject("advanced_native_pipeline_extent_exceeded")end
  end
  live[k]=e;maps[unit]=mapping;indices[unit]=active;fluids[k]=allowed
 end
end
"""


ADVANCED_CONTROLS_LUA = r"""
if x.controls then
 if not f.technologies["circuit-network"]or not f.technologies["circuit-network"].researched then return reject("advanced_circuit_research_missing")end
 local expected={};local connectors={};local edges={};local behaviors={}
 local cid=defines.wire_connector_id.circuit_red
 local function connector(q)
  local e=target(q.position,q.name);local k=key(q);local unit=x.bindings[k]
  if not unit or not e or not e.valid or e.unit_number~=unit or e.force~=f or e.surface~=s or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end
  local c=e.get_wire_connector(cid,x.configure);if c then connectors[e.unit_number]=c end;return c
 end
 for _,q in ipairs(x.core.controls)do
  for _,edge in ipairs(q.wires)do
   local ca=connector(edge.a);local cb=connector(edge.b)
   if not ca or not cb then return reject("advanced_circuit_controls_not_configured")end
   if not ca.can_wire_reach(cb)then return reject("advanced_native_wire_reach_changed")end
   expected[ca.owner.unit_number]=expected[ca.owner.unit_number]or{};expected[cb.owner.unit_number]=expected[cb.owner.unit_number]or{}
   expected[ca.owner.unit_number][cb.owner.unit_number]=true;expected[cb.owner.unit_number][ca.owner.unit_number]=true
   edges[#edges+1]={a=ca,b=cb}
  end
  local tank=live[key(q.tank)];local machine=live[key(q.machine)]
  if x.configure then changed=true end
  local tc=x.configure and tank.get_or_create_control_behavior()or tank.get_control_behavior()
  local mc=x.configure and machine.get_or_create_control_behavior()or machine.get_control_behavior()
  if not tc or not mc then return reject("advanced_circuit_controls_not_configured")end
  behaviors[#behaviors+1]={tank=tc,machine=mc,fluid=q.fluid,reserve=q.reserve}
 end
 for unit,c in pairs(connectors)do
  for _,link in pairs(c.connections)do
   if link.target.wire_connector_id~=cid or not expected[unit][link.target.owner.unit_number]then return reject("advanced_foreign_circuit_wire")end
  end
  local green=c.owner.get_wire_connector(defines.wire_connector_id.circuit_green,false)
  if green and #green.connections>0 then return reject("advanced_foreign_circuit_wire")end
 end
 if x.configure then
  for _,edge in ipairs(edges)do if not edge.a.is_connected_to(edge.b)and not edge.a.connect_to(edge.b,true,defines.wire_origin.player)then error("advanced_wire_connect_failed")end end
  for _,q in ipairs(behaviors)do
   q.tank.circuit_exclusive_mode_of_operation=defines.control_behavior.single_fluid_box.exclusive_mode.send_contents
   q.tank.read_temperature=false
   q.machine.circuit_condition={first_signal={type="fluid",name=q.fluid},comparator=">",constant=q.reserve}
   q.machine.circuit_enable_disable=true;q.machine.circuit_set_recipe=false;q.machine.circuit_read_contents=false
   q.machine.circuit_read_ingredients=false;q.machine.circuit_read_recipe_finished=false;q.machine.circuit_read_working=false
   q.machine.connect_to_logistic_network=false
  end
 end
 for _,edge in ipairs(edges)do if not edge.a.is_connected_to(edge.b)then return reject("advanced_circuit_controls_not_configured")end end
 for _,q in ipairs(behaviors)do
  local c=q.machine.circuit_condition;c=c and(c.condition or c)
  if q.tank.circuit_exclusive_mode_of_operation~=defines.control_behavior.single_fluid_box.exclusive_mode.send_contents or q.tank.read_temperature
   or not q.machine.circuit_enable_disable or q.machine.circuit_set_recipe or q.machine.circuit_read_contents
   or q.machine.circuit_read_ingredients or q.machine.circuit_read_recipe_finished or q.machine.circuit_read_working
   or q.machine.connect_to_logistic_network or not c or not c.first_signal or c.first_signal.type~="fluid"
   or c.first_signal.name~=q.fluid or c.comparator~=">"or c.constant~=q.reserve or c.second_signal then return reject("advanced_circuit_controls_not_configured")end
 end
end
"""


ADVANCED_GRAPH_LUA = r"""
local expected={}
local function edge(a,ai,b,bi)
 local yes=false;local reverse=false
 for _,p in pairs(a.get_fluid_box_neighbours(ai)or{})do if p.entity==b and p.index==bi then yes=true end end
 for _,p in pairs(b.get_fluid_box_neighbours(bi)or{})do if p.entity==a and p.index==ai then reverse=true end end
 if not yes or not reverse then return false end
 expected[id(a,ai)]=expected[id(a,ai)]or{};expected[id(a,ai)][id(b,bi)]=true
 expected[id(b,bi)]=expected[id(b,bi)]or{};expected[id(b,bi)][id(a,ai)]=true
 return true
end
for _,q in ipairs(x.edges)do local a=live[key(q.a)];local b=live[key(q.b)]
 if a and b then
  local ai=maps[a.unit_number][q.a.fluidbox_index];local bi=maps[b.unit_number][q.b.fluidbox_index]
  if not ai or not bi or not edge(a,ai,b,bi)then return reject("advanced_reciprocal_typed_edge_missing")end
 elseif not x.cold then return reject("advanced_native_edge_endpoint_missing")end
end
for _,h in ipairs(x.handoffs or{})do local q=h.first_neighbor;local source=live[key(h.source)];local e=target(q.position,q.name)
 if not source or not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y
  or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return reject("advanced_bound_consumer_identity_changed")end
 for fluid in pairs(e.get_fluid_contents())do if fluid~=h.source.item then return reject("advanced_bound_consumer_contaminated")end end
 if not edge(source,1,e,1)then return reject("advanced_bound_consumer_peer_missing")end
end
for k,e in pairs(live)do if not sources[e.unit_number]then
 for _,i in ipairs(indices[e.unit_number])do
  for _,p in pairs(e.get_fluid_box_neighbours(i)or{})do if not expected[id(e,i)]or not expected[id(e,i)][id(p.entity,p.index)]then return reject("advanced_unplanned_native_fluid_peer")end end
 end
end end
local stock={}
if not x.cold then for fluid,q in pairs(x.core.outputs)do local e=live[key(q)];local amount=e and(e.get_fluid_contents()[fluid]or 0)or 0
 if amount<=0 then return reject("advanced_all_three_typed_outputs_required")end;stock[fluid]=amount
end end
"""

ADVANCED_INSPECT_LUA = (
    ADVANCED_ENTITIES_LUA
    + ADVANCED_GRAPH_LUA
    + ADVANCED_CONTROLS_LUA
    + "\nreturn {ok=true,tick=game.tick,machines=counts,stock=stock,controls_checked=x.controls==true,cold=x.cold==true}"
)


def execute_configure_advanced_petroleum(game, action):
    """Known-file guarded native UI configuration; no fluid/resource edits."""
    from factorio_agent.core.world_catalog import WorldCatalog

    allowed = {
        "type",
        "expected_world_id",
        "expected_actor_unit",
        "expected_catalog_fingerprint",
        "plan_sha256",
        "reason",
    }
    if (
        not isinstance(action, dict)
        or set(action) - allowed
        or action.get("type") != "configure_advanced_petroleum"
        or type(action.get("expected_actor_unit")) is not int
        or action["expected_actor_unit"] < 1
    ):
        raise ValueError("invalid advanced petroleum configuration envelope")
    root = Path(game.cfg.runtime_dir)
    state = json.loads((root / "advanced-petroleum.json").read_text(encoding="utf-8"))
    board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
    catalog = WorldCatalog.from_dict(json.loads((root / "catalog.json").read_text(encoding="utf-8")))
    world = action.get("expected_world_id")
    if (
        not world
        or world != getattr(game, "expected_world", None)
        or state.get("phase") != "cold"
        or state.get("world_id") != world
        or state.get("catalog_fingerprint") != catalog.fingerprint
        or action.get("expected_catalog_fingerprint") != catalog.fingerprint
        or board.get("world_id") != world
        or board.get("catalog_fingerprint") != catalog.fingerprint
        or board.get("blocks", {}).get("advanced_petroleum") != state["plan"]
        or action.get("plan_sha256") != _digest(state["plan"])
        or not state.get("baseline")
        or any(type(state["bindings"].get(_key(q))) is not int for q in state["cold_plan"]["entities"])
    ):
        raise ValueError("advanced petroleum known-file authority changed")
    args = dict(
        world_id=world,
        actor_unit=action["expected_actor_unit"],
        core=state["core"],
        bindings=state["bindings"],
        sources=state["sources"],
        nodes=state["fluid_nodes"],
        edges=state["fluid_edges"],
        gaps=[_key(r["first_neighbor"]) for r in state["routes"].values()],
        handoffs=[],
        cold=True,
        controls=True,
        configure=True,
    )
    try:
        result = game.query(ADVANCED_INSPECT_LUA.replace("PAYLOAD", _payload(args)))
    except Exception:
        return {"ok": False, "reason": "advanced_configuration_outcome_unknown"}
    if (
        not isinstance(result, dict)
        or type(result.get("ok")) is not bool
        or (
            result.get("ok")
            and (
                result.get("controls_checked") is not True
                or result.get("cold") is not True
                or type(result.get("tick")) is not int
            )
        )
    ):
        return {"ok": False, "reason": "advanced_configuration_outcome_unknown"}
    return result
