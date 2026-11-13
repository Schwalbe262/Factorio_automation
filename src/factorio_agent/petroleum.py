"""Normally paid basic refining with fixed headers and guarded fluid handoffs.

Preparation reads the world. Only step delegates ordinary construction actions;
neither path inserts fluids, changes research, or changes production counters.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .planning import overlap, preflight, survey_slots
from .power_grid import plan_connection as power_connection
from .resource_policy import mining_reservations
from . import remedy
from .routing import LONG_ROUTE_TURNS, VECTORS, route_trunks
from .smelting import merge_entities
from .transport import xy


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def _identity(obs, value, catalog):
    return (
        obs.get("ok", True)
        and isinstance(obs.get("world_id"), str)
        and bool(obs["world_id"])
        and value.get("world_id") == obs["world_id"]
        and value.get("catalog_fingerprint") == catalog.fingerprint
    )


def basic_petroleum_rates(catalog):
    recipe = catalog.recipes["basic-oil-processing"]
    machine = catalog.entities["oil-refinery"]
    inputs = recipe["ingredients"]
    outputs = recipe["products"]
    if (
        len(inputs) != 1
        or inputs[0].get("type") != "fluid"
        or inputs[0]["name"] != "crude-oil"
        or len(outputs) != 1
        or outputs[0].get("type") != "fluid"
        or outputs[0]["name"] != "petroleum-gas"
        or recipe.get("category") not in machine.get("crafting_categories", [])
    ):
        raise ValueError("one crude input and one petroleum output in a compatible refinery required")
    seconds = recipe["energy"]
    speed = machine["crafting_speed"]
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (seconds, speed, inputs[0]["amount"])
    ):
        raise ValueError("positive finite installed recipe and machine rates required")
    crafts = 60 * speed / seconds
    return {
        "crafts_per_minute": crafts,
        "crude_per_minute": crafts * inputs[0]["amount"],
        "petroleum_per_minute": crafts * _yield(outputs[0]),
        "electric_kw": machine["energy_usage"] * 60 / 1000,
        "scope": "catalog nominal capacity; source availability and measured throughput are separate",
    }


def recipe_fluid_port(catalog, recipe_name, item, direction=0):
    """Recipe fluid indices count inputs and outputs separately, not physical boxes."""
    recipe = catalog.recipes[recipe_name]
    matches = []
    for side, production in (("ingredients", "input"), ("products", "output")):
        for row in recipe[side]:
            if row.get("type") == "fluid" and row["name"] == item:
                matches.append((row, production))
    if len(matches) != 1 or direction not in VECTORS:
        raise ValueError("one cardinal typed fluid port required")
    row, production = matches[0]
    ordinal = row.get("fluidbox_index")
    boxes = sorted(
        (b for b in catalog.entities["oil-refinery"]["fluidbox_prototypes"] if b.get("production_type") == production),
        key=lambda b: b["index"],
    )
    if type(ordinal) is not int or not 1 <= ordinal <= len(boxes):
        raise ValueError("explicit native recipe fluidbox ordinal required")
    box = boxes[ordinal - 1]
    connections = [c for c in box["pipe_connections"] if c.get("connection_type", "normal") == "normal"]
    if len(connections) != 1:
        raise ValueError("one native surface connection per refinery fluid box required")
    connection = connections[0]
    positions = connection.get("positions", [])
    if len(positions) != 4:
        raise ValueError("four rotated native refinery port positions required")
    inside = positions[direction // 4]
    facing = (connection["direction"] + direction) % 16
    dx, dy = VECTORS[facing]
    return {
        "fluidbox_index": box["index"],
        "recipe_ordinal": ordinal,
        "facing": facing,
        "offset": {"x": inside["x"] + dx, "y": inside["y"] + dy},
    }


def refinery_core(catalog, x, y, count=1):
    """One to three refineries; headers, ports and the full expansion area stay fixed."""
    rates = basic_petroleum_rates(catalog)
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("one to three refinery slots supported")
    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v % 1 - 0.5) > 1e-6 for v in (x, y)):
        raise ValueError("half-tile refinery centers required")
    prototype = catalog.entities["oil-refinery"]
    box = prototype["selection_box"]
    if box != {"left_top": {"x": -2.5, "y": -2.5}, "right_bottom": {"x": 2.5, "y": 2.5}}:
        raise ValueError("supported five by five refinery footprint required")
    if sorted((b["index"], b.get("production_type")) for b in prototype["fluidbox_prototypes"]) != [
        (1, "input"),
        (2, "input"),
        (3, "output"),
        (4, "output"),
        (5, "output"),
    ]:
        raise ValueError("five installed physical refinery fluid boxes required")
    crude = recipe_fluid_port(catalog, "basic-oil-processing", "crude-oil")
    gas = recipe_fluid_port(catalog, "basic-oil-processing", "petroleum-gas")
    if crude["offset"] != {"x": 1, "y": 3} or gas["offset"] != {"x": 2, "y": -3}:
        raise ValueError("installed refinery must support south crude and north east petroleum headers")
    machines = [entity("oil-refinery", x + 8 * i, y, recipe="basic-oil-processing") for i in range(count)]
    crude_pipes = [entity("pipe", x + i, y + 5) for i in range(-4, 18)]
    gas_pipes = [entity("pipe", x + i, y - 4) for i in range(2, 23)]
    for i in range(count):
        crude_pipes += [entity("pipe", x + 8 * i + 1, y + 3), entity("pipe", x + 8 * i + 1, y + 4)]
        gas_pipes += [entity("pipe", x + 8 * i + 2, y - 3)]
    poles = [entity("small-electric-pole", x + 8 * i + d, y) for i in range(count) for d in (-3, 3)]
    ports = [
        {
            "kind": "fluid",
            "item": "crude-oil",
            "direction": "input",
            "facing": 4,
            "name": "pipe",
            "position": deepcopy(crude_pipes[0]["position"]),
            "fluidbox_index": 1,
        },
        {
            "kind": "fluid",
            "item": "petroleum-gas",
            "direction": "output",
            "facing": 4,
            "name": "pipe",
            "position": deepcopy(gas_pipes[20]["position"]),
            "fluidbox_index": 1,
        },
    ]
    return _plan(
        crude_pipes + gas_pipes + poles + machines,
        ports,
        machines=machines,
        crude_pipes=crude_pipes,
        gas_pipes=gas_pipes,
        power_port=poles[0],
        reserved_area=[[x - 5.5, y - 5.5], [x + 24.5, y + 6.5]],
        refinery_fluidboxes={"crude-oil": crude["fluidbox_index"], "petroleum-gas": gas["fluidbox_index"]},
        capacity={
            **rates,
            "refinery_count": count,
            "total_nominal_petroleum_per_minute": rates["petroleum_per_minute"] * count,
        },
        layout_policy="fixed-basic-refinery-headers-v1",
    )


def compile_pipe_crossings(route, occupied, maximum_span):
    """Surface pipes turn; buried crossings have straight, clear, paired mouths."""
    if not route.get("ok"):
        return route
    positions = [xy(p) for p in route["segments"]]
    hits = [i for i, p in enumerate(positions) if p in occupied]
    spans = []
    for i in hits:
        if spans and i <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], i + 1)
        else:
            spans.append((i - 1, i + 1))
    mouths = {}
    buried = set()
    pairs = []
    for first, last in spans:
        if first <= 0 or last >= len(positions) - 1 or last - first > maximum_span:
            return {"ok": False, "reason": "pipe_crossing_requires_clear_bounded_mouths"}
        d = route["segments"][first]["direction"]
        dx, dy = VECTORS[d]
        if route["segments"][first - 1]["direction"] != d or route["segments"][last]["direction"] != d:
            return {"ok": False, "reason": "buried_pipe_turn_or_mouth_corner"}
        if any(positions[i + 1] != (positions[i][0] + dx, positions[i][1] + dy) for i in range(first, last)):
            return {"ok": False, "reason": "buried_pipe_turn_or_mouth_corner"}
        if positions[first] in occupied or positions[last] in occupied:
            return {"ok": False, "reason": "pipe_mouth_occupied"}
        mouths[first] = entity("pipe-to-ground", *positions[first], (d + 8) % 16)
        mouths[last] = entity("pipe-to-ground", *positions[last], d)
        buried.update(range(first + 1, last))
        pairs.append(
            {"first": list(positions[first]), "last": list(positions[last]), "direction": d, "span": last - first}
        )
    es = [mouths.get(i, entity("pipe", *p)) for i, p in enumerate(positions) if i not in buried]
    return _plan(
        es,
        [],
        underground_pairs=pairs,
        route_metrics=route["metrics"],
        full_path=[{"x": x, "y": y} for x, y in positions],
        layout_policy="bounded-fluid-trunk-v1",
    )


PIPE_SURVEY_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local b=args.bounds;local hard={};local soft={}
local function same(p,q)return p.x==q.x and p.y==q.y end
for y=b.min_y,b.max_y do for x=b.min_x,b.max_x do local p={x=x,y=y};local area={{x-.49,y-.49},{x+.49,y+.49}};local blocked=false;local crossing=false
 for _,zone in ipairs(args.zones)do local z=zone.area;if x+.49>z[1][1]and x-.49<z[2][1]and y+.49>z[1][2]and y-.49<z[2][2]then blocked=true;break end end
 for _,z in ipairs(args.reservations)do if x+.49>z[1][1]and x-.49<z[2][1]and y+.49>z[1][2]and y-.49<z[2][2]then blocked=true;break end end
 if s.count_entities_filtered{area=area,type="resource"}>0 then blocked=true end
 for _,tile in pairs(s.find_tiles_filtered{area=area})do for layer in pairs(prototypes.entity.pipe.collision_mask.layers)do if tile.collides_with(layer)then blocked=true;break end end end
 for _,e in pairs(s.find_entities_filtered{area=area})do
  local rock=e.type=="simple-entity"and(e.name=="big-rock"or e.name=="huge-rock"or e.name=="big-sand-rock")
  local natural=e.force.name=="neutral"and e.minable and(e.type=="tree"or rock)
  if e~=a and e.type~="resource"and e.type~="corpse"and not natural and not e.prototype.collision_mask.colliding_with_tiles_only then crossing=true end
 end
 for _,e in pairs(s.find_entities_filtered{area={{x-1.49,y-1.49},{x+1.49,y+1.49}}})do
  if not(e.unit_number==args.source.unit_number and same(e.position,args.source.position))then
   for _ in pairs(e.prototype.fluidbox_prototypes or{})do blocked=true;break end
  end
 end
 if same(p,args.source.position)then blocked=false;crossing=false end
 if blocked then hard[#hard+1]=p elseif crossing then soft[#soft+1]=p end
end end
return {ok=true,hard=hard,soft=soft}
"""


# Crude corridor survey: one query covers at most PIPE_SURVEY_CELLS tiles; a long
# connection (a far crude field, oil_gathering survey rings) is surveyed in row
# strips up to the hard maximum PIPE_SURVEY_MAX_CELLS. A route still needs one
# native pipeline segment (pipeline_extent); pump segments are not planned yet.
PIPE_SURVEY_CELLS = 40000
PIPE_SURVEY_MAX_CELLS = 160000


def refinery_origin(board):
    """Where the refinery block is sought: west of the industrial district (the crude consumer)."""
    industrial = [
        b["reserved_area"]
        for k, b in (board or {}).get("blocks", {}).items()
        if k in ("steel_production", "brick_production") and b.get("reserved_area")
    ]
    base_x = math.floor(min((a[0][0] for a in industrial), default=77)) + 5.5
    base_y = math.floor(min((a[0][1] for a in industrial), default=-45)) - 11.5
    return base_x, base_y


def _survey_strips(bounds):
    width = bounds["max_x"] - bounds["min_x"] + 1
    rows = max(1, PIPE_SURVEY_CELLS // width)
    y = bounds["min_y"]
    while y <= bounds["max_y"]:
        yield dict(bounds, min_y=y, max_y=min(bounds["max_y"], y + rows - 1))
        y += rows


def plan_crude_connection(game, builder, obs, source, destination, core):
    a, b = source["position"], destination["position"]
    bounds = {
        "min_x": min(a["x"], b["x"]) - 12,
        "max_x": max(a["x"], b["x"]) + 12,
        "min_y": min(a["y"], b["y"]) - 12,
        "max_y": max(a["y"], b["y"]) + 12,
    }
    cells = (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1)
    if cells > PIPE_SURVEY_MAX_CELLS or bounds["max_x"] - bounds["min_x"] + 1 > PIPE_SURVEY_CELLS:
        return {
            "ok": False,
            "reason": "pipe_corridor_survey_budget_exceeded",
            "cells": cells,
            "limit": PIPE_SURVEY_MAX_CELLS,
        }
    boxes = game.query("""local out={};for _,b in pairs(prototypes.entity["pipe-to-ground"].fluidbox_prototypes)do
 for _,c in pairs(b.pipe_connections)do if c.connection_type=="underground"then out[#out+1]=c.max_underground_distance end end end
return {ok=true,spans=out}""")
    if (
        not boxes.get("ok")
        or len(boxes.get("spans", [])) != 1
        or type(boxes["spans"][0]) is not int
        or boxes["spans"][0] < 2
    ):
        return {"ok": False, "reason": "native_pipe_underground_distance_unavailable"}
    board_path = Path(game.cfg.runtime_dir) / "pcb-board.json"
    board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
    if board.get("world_id") != obs["world_id"]:
        return {"ok": False, "reason": "pipe_routing_board_world_changed"}
    reservations = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
    reservations += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
    zones = mining_reservations(game, obs["world_id"])
    survey = {"ok": True, "hard": [], "soft": []}
    for strip in _survey_strips(bounds):
        part = game.query(
            PIPE_SURVEY_LUA.replace(
                "PAYLOAD",
                _payload({"bounds": strip, "source": source, "reservations": reservations, "zones": zones}),
            )
        )
        if not part.get("ok"):
            return part
        survey["hard"] += part.get("hard", [])
        survey["soft"] += part.get("soft", [])
    hard = {xy({"position": p}) for p in survey.get("hard", [])}
    hard.update(builder._occupied_by_plan(core["entities"]))
    area = core.get("reserved_area")
    if area:
        # Preserve empty future refinery slots as well as today's hardware.
        cells = {
            (area[0][0] + 0.5 + i, area[0][1] + 0.5 + j)
            for i in range(int(area[1][0] - area[0][0]))
            for j in range(int(area[1][1] - area[0][1]))
        }
        entry = {(x, b["y"]) for x, y in cells if y == b["y"] and x <= b["x"]}
        hard.update(cells - entry)
    hard -= {(a["x"], a["y"]), (b["x"], b["y"])}
    soft = {(p["x"], p["y"]) for p in survey.get("soft", [])}

    def accepts(route):
        return compile_pipe_crossings(route, soft, boxes["spans"][0]).get("ok", False)

    # The shared long-route fallback (routing.LONG_ROUTE_*, remedy.ROUTE_RELAX) for a far crude field.
    relax = remedy.route_limits()
    reach = {"long_route_distance": relax["long_route_distance"]} if relax["long_route_distance"] else {}
    route = route_trunks(
        a,
        b,
        occupied=hard,
        bounds=bounds,
        start_direction=source["facing"],
        end_direction=destination["facing"],
        max_nodes=max(200000, min(400000, int(abs(a["x"] - b["x"]) + abs(a["y"] - b["y"]) + 1) * 2000)),
        max_turns=6,
        max_detour_ratio=max(1.35, relax["max_detour_ratio"] or 1.35),
        candidate_validator=accepts,
        long_route_turns=relax["long_route_turns"] or LONG_ROUTE_TURNS,
        **reach,
    )
    result = compile_pipe_crossings(route, soft, boxes["spans"][0])
    if not result.get("ok"):
        return result
    result["entities"] = [e for e in result["entities"] if xy(e) != (a["x"], a["y"])]
    result["maximum_underground_span"] = boxes["spans"][0]
    # The full path, including buried tiles, was hard-blocked against ore, water and mining growth.
    result["ports"] = [deepcopy(source), deepcopy(destination)]
    return result


def pipeline_extent(source_box, entities, limit):
    points = [xy(e) for e in entities]
    if not points or type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
        return {"ok": False, "reason": "invalid_native_pipeline_extent"}
    try:
        lo = source_box["left_top"]
        hi = source_box["right_bottom"]
        width = max(hi["x"], max(x for x, y in points) + 0.5) - min(lo["x"], min(x for x, y in points) - 0.5)
        height = max(hi["y"], max(y for x, y in points) + 0.5) - min(lo["y"], min(y for x, y in points) - 0.5)
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "native_source_pipeline_extent_missing"}
    return {
        "ok": width <= limit and height <= limit,
        "reason": "" if width <= limit and height <= limit else "pipeline_requires_explicit_pump_segment",
        "width": width,
        "height": height,
        "native_limit": limit,
    }


def fluid_plan_isolation(game, source, core, route):
    """Never accidentally join gas to crude or steal an existing underground peer."""
    crude = {xy(e) for e in route["entities"] + core["crude_pipes"]} | {xy(source)}
    gas = {xy(e) for e in core["gas_pipes"]}
    if crude & gas or any((x + dx, y + dy) in gas for x, y in crude for dx, dy in VECTORS.values()):
        return {"ok": False, "reason": "prospective_crude_petroleum_networks_touch"}
    return game.query(
        """local args=helpers.json_to_table("""
        + _payload(
            {
                "source": source,
                "span": route.get("maximum_underground_span", 0),
                "specs": merge_entities(route["entities"], core["crude_pipes"], core["gas_pipes"], core["machines"]),
            }
        )
        + """)
for _,q in ipairs(args.specs)do local p=q.position;local radius=q.name=="oil-refinery"and 3.49 or 1.49
 for _,e in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}}})do
  if e.unit_number~=args.source.unit_number then for _ in pairs(e.prototype.fluidbox_prototypes or{})do
   return {ok=false,reason="prospective_pipe_near_foreign_fluid_network",position=p,foreign_unit=e.unit_number}end end
 end
 if q.name=="pipe-to-ground"then for _,e in pairs(s.find_entities_filtered{area={{p.x-args.span,p.y-args.span},{p.x+args.span,p.y+args.span}},name="pipe-to-ground"})do
  if(p.x==e.position.x or p.y==e.position.y)and math.abs(p.x-e.position.x)+math.abs(p.y-e.position.y)<=args.span then
   return {ok=false,reason="existing_underground_pipe_could_steal_peer",position=p,foreign_unit=e.unit_number}end
 end end
end
return {ok=true}"""
    )


def _fluid_edges(nodes, pairs, core, source):
    """Expected reciprocal entity/index edges, including physical refinery ports."""
    by = {xy(e): e for e in nodes}
    edges = []
    seen = set()

    def add(a, ai, b, bi):
        key = tuple(sorted(((_key(a), ai), (_key(b), bi))))
        if key not in seen:
            seen.add(key)
            edges.append({"a": dict(a, fluidbox_index=ai), "b": dict(b, fluidbox_index=bi)})

    def surface(e, d):
        return e["name"] != "pipe-to-ground" or e["direction"] == d

    for p, e in by.items():
        for d, (dx, dy) in VECTORS.items():
            other = by.get((p[0] + dx, p[1] + dy))
            if other and surface(e, d) and surface(other, (d + 8) % 16):
                add(e, 1, other, 1)
    for pair in pairs:
        add(by[tuple(pair["first"])], 1, by[tuple(pair["last"])], 1)
    for machine in core["machines"]:
        for item, offset in (("crude-oil", (1, 3)), ("petroleum-gas", (2, -3))):
            pipe = by[(machine["position"]["x"] + offset[0], machine["position"]["y"] + offset[1])]
            add(machine, core["refinery_fluidboxes"][item], pipe, 1)
    return edges


class BasicPetroleum:
    def __init__(self, game, builder, catalog, perimeter):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.path = Path(game.cfg.runtime_dir) / "basic-petroleum.json"
        self.sources_path = self.path.parent / "fluid-sources.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.last_proof_failure = None

    def _source(self, obs):
        if not self.sources_path.exists():
            return {"ok": False, "reason": "proven_crude_source_not_published"}
        registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
        if not _identity(obs, registry, self.catalog):
            return {"ok": False, "reason": "fluid_source_registry_identity_changed"}
        source = registry.get("sources", {}).get("crude-oil", {})
        if (
            source.get("kind") != "fluid"
            or source.get("item") != "crude-oil"
            or source.get("direction") != "output"
            or source.get("name") != "pipe"
            or source.get("facing") not in VECTORS
            or source.get("fluidbox_index") != 1
            or source.get("world_id") != obs["world_id"]
            or type(source.get("unit_number")) is not int
            or not source.get("defense_scope_id")
        ):
            return {"ok": False, "reason": "proven_exact_crude_pipe_source_required"}
        result = self.game.query(
            """local q=helpers.json_to_table("""
            + _payload(source)
            + """);local e=target(q.position,q.name)
if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="native_crude_source_identity_changed"}end
local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~="crude-oil"then return {ok=false,reason="native_crude_source_contaminated"}end end
if(fluids["crude-oil"]or 0)<=0 or not e.has_fluid_segment(1)then return {ok=false,reason="native_crude_source_empty"}end
return {ok=true,extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}"""
        )
        return dict(result, source=deepcopy(source))

    def prepare(self, obs):
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return {"ok": False, "reason": "petroleum_requires_valid_world_observation"}
        if not obs.get("technologies", {}).get("oil-processing") or not all(
            obs.get("enabled_recipes", {}).get(r) for r in ("basic-oil-processing", "oil-refinery")
        ):
            return {"ok": False, "reason": "naturally_researched_basic_oil_processing_required"}
        if self.state:
            return (
                deepcopy(self.state)
                if _identity(obs, self.state, self.catalog)
                else {"ok": False, "reason": "petroleum_world_or_catalog_changed"}
            )
        source = self._source(obs)
        if not source.get("ok"):
            return source
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if not _identity(obs, board, self.catalog):
            return {"ok": False, "reason": "petroleum_board_identity_changed"}
        reserved = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
        reserved += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        # Relative to existing industrial district's west edge, not a well-specific waypoint.
        base_x, base_y = refinery_origin(board)
        anchors = [(base_x + dx, base_y + dy) for dx, dy in ((0, 0), (32, 0), (0, -16), (32, -16), (0, -32), (32, -32))]
        failures = []
        deadline = time.monotonic() + 20
        attempts = 0
        for x, y in anchors:
            if time.monotonic() > deadline:
                break
            attempts += 1
            try:
                core = refinery_core(self.catalog, x, y)
            except (KeyError, ValueError, TypeError) as exc:
                return {"ok": False, "reason": "unsupported_native_basic_refinery", "detail": str(exc)}
            if any(overlap(core["reserved_area"], r) for r in reserved):
                failures.append({"stage": "reservation", "position": [x, y]})
                continue
            survey = survey_slots(self.game, [[core["reserved_area"]]])
            if not survey.get("ok") or 1 not in survey.get("legal", []):
                failures.append({"stage": "core_land", "position": [x, y], "detail": survey.get("reason")})
                continue
            route = plan_crude_connection(self.game, self.builder, obs, source["source"], core["ports"][0], core)
            if not route.get("ok"):
                failures.append({"stage": "crude_route", "position": [x, y], "detail": route})
                continue
            isolation = fluid_plan_isolation(self.game, source["source"], core, route)
            if not isolation.get("ok"):
                failures.append({"stage": "fluid_isolation", "detail": isolation})
                continue
            extent = pipeline_extent(source.get("extent"), route["entities"] + core["crude_pipes"], source.get("limit"))
            if not extent.get("ok"):
                failures.append({"stage": "pipeline_extent", "detail": extent})
                continue
            power = power_connection(
                self.game,
                self.builder,
                obs,
                core["power_port"]["position"],
                route["entities"] + [e for e in core["entities"] if e["name"] != "small-electric-pole"],
            )
            if not power.get("ok"):
                failures.append({"stage": "power", "detail": power})
                continue
            es = merge_entities(route["entities"], core["entities"], power["entities"])
            check = preflight(self.game, self.builder, obs, es)
            if not check.get("ok"):
                failures.append({"stage": "whole_preflight", "detail": check})
                continue
            scope = (
                "petroleum-"
                + hashlib.sha256(json.dumps([obs["world_id"], core["ports"]], sort_keys=True).encode()).hexdigest()[:20]
            )
            defense = self.perimeter.prepare_coverage(obs, scope, [core["reserved_area"]])
            if not defense.get("ok"):
                return {"ok": False, "reason": "petroleum_defense_preflight_failed", "evidence": defense}
            nodes = merge_entities(
                [dict(entity("pipe", *xy(source["source"])), unit_number=source["source"]["unit_number"])],
                route["entities"],
                core["crude_pipes"],
                core["gas_pipes"],
            )
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                clear_obstacles=check["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                layout_policy="guarded-basic-petroleum-v1",
            )
            actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            bindings = {
                _key(e): actual[(e["name"], *xy(e))]["unit_number"] for e in es if (e["name"], *xy(e)) in actual
            }
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "build",
                "prepared_tick": obs.get("tick"),
                "plan": plan,
                "core": core,
                "route": route,
                "source": deepcopy(source["source"]),
                "bindings": bindings,
                "defense_scope_id": scope,
                "defense_preview": defense,
                "pipeline_extent": extent,
                "fluid_nodes": nodes,
                "fluid_edges": _fluid_edges(nodes, route["underground_pairs"], core, source["source"]),
                "wire_hops": power["wire_hops"],
                "planning_attempts": attempts,
            }
        return {
            "ok": False,
            "reason": "no_bounded_basic_petroleum_site_with_complete_native_connections",
            "evidence": {
                "attempts": attempts,
                "planning_budget_seconds": 20,
                "budget_exhausted": time.monotonic() > deadline,
                "failures": failures[-6:],
            },
        }

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        changed = False
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            expected = self.state["bindings"].get(_key(spec))
            if expected and (not live or live.get("unit_number") != expected):
                return {"status": "blocked", "reason": "petroleum_asset_missing_or_replaced", "entity": spec}
            if live:
                if type(live.get("unit_number")) is not int or (
                    spec["name"] in ("oil-refinery", "pipe-to-ground") and live.get("direction") != spec["direction"]
                ):
                    return {
                        "status": "blocked",
                        "reason": "petroleum_asset_identity_or_direction_changed",
                        "entity": spec,
                    }
                if expected is None:
                    self.state["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def _reserve(self, obs):
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if not _identity(obs, board, self.catalog):
            return {"status": "blocked", "reason": "petroleum_board_identity_changed"}
        old = board.get("blocks", {}).get("basic_petroleum")
        area = self.state["core"]["reserved_area"]
        if old and any(old.get(k) != self.state["plan"].get(k) for k in ("entities", "ports", "reserved_area")):
            return {"status": "blocked", "reason": "petroleum_reservation_changed"}
        if any(
            k != "basic_petroleum" and overlap(area, b["reserved_area"])
            for k, b in board["blocks"].items()
            if b.get("reserved_area")
        ):
            return {"status": "blocked", "reason": "petroleum_reservation_conflict"}
        board["blocks"]["basic_petroleum"] = dict(self.state["plan"], defense_scope_id=self.state["defense_scope_id"])
        _atomic_json(path, board)
        return None

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if not _identity(obs, self.state, self.catalog):
            return {"status": "blocked", "reason": "petroleum_world_or_catalog_changed"}
        source = self._source(obs)
        if not source.get("ok") or source.get("source") != self.state["source"]:
            return {"status": "blocked", "reason": "prepared_crude_source_changed_or_empty", "evidence": source}
        for result in (self._bind(obs),):
            if result:
                return result
        result = self._reserve(obs)
        if result:
            return result
        result = self.perimeter.ensure_coverage(
            obs, self.state["defense_scope_id"], [self.state["core"]["reserved_area"]]
        )
        if result is not None:
            return result
        # New defensive hardware may occupy a route. Recheck once after coverage and before construction.
        if not self.state.get("construction_authorized"):
            check = preflight(self.game, self.builder, obs, self.state["plan"]["entities"])
            if not check.get("ok"):
                return {
                    "status": "blocked",
                    "reason": "petroleum_post_defense_whole_preflight_failed",
                    "evidence": check,
                }
            self.state["construction_authorized"] = True
            self._save()
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if not evidence:
            return {
                "status": "waiting",
                "reason": "waiting for native basic refinery products and reciprocal typed fluid connections",
                "evidence": self.last_proof_failure,
            }
        registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
        if not _identity(obs, registry, self.catalog):
            return {"status": "blocked", "reason": "fluid_source_registry_identity_changed"}
        old = registry["sources"].get("petroleum-gas")
        if old and old != evidence["source"]:
            return {"status": "blocked", "reason": "published_petroleum_output_identity_changed"}
        registry["sources"]["petroleum-gas"] = evidence["source"]
        _atomic_json(self.sources_path, registry)
        self.state.update(phase="verified", proof=evidence)
        self._save()
        return {
            "status": "succeeded",
            "reason": "normally extracted crude produced native petroleum gas",
            "evidence": evidence,
        }

    def proof(self, obs):
        self.last_proof_failure = None
        if not self.state or not _identity(obs, self.state, self.catalog):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            unit = self.state["bindings"].get(_key(spec))
            if not live or type(unit) is not int or live.get("unit_number") != unit:
                self.last_proof_failure = {"reason": "petroleum_asset_missing_or_replaced", "entity": spec}
                return None
        defense = self.perimeter.coverage_proof(obs, self.state["defense_scope_id"])
        if not defense:
            self.last_proof_failure = {"reason": "petroleum_defense_coverage_missing"}
            return None
        source = self._source(obs)
        if not source.get("ok") or source.get("source") != self.state["source"]:
            self.last_proof_failure = source
            return None
        bindings = dict(self.state["bindings"])
        bindings[_key(dict(self.state["source"], name="pipe"))] = self.state["source"]["unit_number"]
        relevant = self.state["fluid_nodes"] + self.state["core"]["machines"]
        if any(_key(e) not in bindings for e in relevant):
            self.last_proof_failure = {"reason": "petroleum_native_bindings_incomplete"}
            return None
        from .fluid_handoffs import bound_handoffs

        gas_source = dict(
            self.state["core"]["ports"][1],
            world_id=obs["world_id"],
            unit_number=bindings[_key(self.state["core"]["ports"][1])],
            defense_scope_id=self.state["defense_scope_id"],
        )
        try:
            registry = json.loads(self.sources_path.read_text(encoding="utf-8"))
            handoffs = bound_handoffs(registry, obs, self.catalog, gas_source)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.last_proof_failure = {"reason": "petroleum_handoff_registry_invalid", "detail": str(exc)}
            return None
        args = {
            "nodes": [dict(e, unit_number=bindings[_key(e)]) for e in relevant],
            "edges": self.state["fluid_edges"],
            "bindings": bindings,
            "source": self.state["source"],
            "gas_positions": [e["position"] for e in self.state["core"]["gas_pipes"]],
            "outlet": self.state["core"]["ports"][1],
            "handoffs": handoffs,
        }
        result = self.game.query(PETROLEUM_PROOF_LUA.replace("PAYLOAD", _payload(args)))
        if not result.get("ok"):
            self.last_proof_failure = result
            return None
        outlet = dict(
            self.state["core"]["ports"][1],
            world_id=obs["world_id"],
            unit_number=result["outlet_unit"],
            defense_scope_id=self.state["defense_scope_id"],
        )
        return {
            "tick": result["tick"],
            "source": outlet,
            "native": result,
            "capacity": self.state["core"]["capacity"],
            "defense_coverage": defense,
            "scope": "normally powered basic refinery and native petroleum buffer; no sustained rate certification",
        }


PETROLEUM_PROOF_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local live={};local gas={};local expected={};local storage_maps={};local storage_indices={};local limit=prototypes.utility_constants.default_pipeline_extent
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local function id(q,index)return key(q).."@"..index end
for _,p in ipairs(args.gas_positions)do gas[p.x..":"..p.y]=true end
for _,q in ipairs(args.nodes)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="petroleum_native_asset_identity_changed",position=q.position}end
 if(q.name=="pipe-to-ground"or q.name=="oil-refinery")and e.direction~=q.direction then return {ok=false,reason="petroleum_native_direction_changed"}end
 local fluids=e.get_fluid_contents();local isgas=gas[q.position.x..":"..q.position.y]
 for name in pairs(fluids)do if(q.name=="oil-refinery"and name~="crude-oil"and name~="petroleum-gas")or(q.name~="oil-refinery"and name~=(isgas and"petroleum-gas"or"crude-oil"))then return {ok=false,reason="petroleum_native_fluid_contamination",position=q.position}end end
 if q.name=="oil-refinery"then local recipe=e.get_recipe()
  if not recipe or recipe.name~="basic-oil-processing"or e.products_finished<=0 or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="petroleum_refinery_not_normally_producing",position=q.position}end
  local mapping={};local indices={}
  for index=1,5 do local ok,proto=pcall(function()return e.get_fluid_box_prototype(index)end)
   if ok and proto then
    local prototypes=proto.index and{proto}or proto;local allowed=nil
    for _,p in pairs(prototypes)do
     local item=p.index==2 and"crude-oil"or(p.index==5 and"petroleum-gas"or nil)
     if not item or(allowed and allowed~=item)or mapping[p.index]then return {ok=false,reason="petroleum_runtime_storage_mapping_invalid"}end
     allowed=item;mapping[p.index]=index
    end
    if not allowed then return {ok=false,reason="petroleum_runtime_storage_mapping_invalid"}end
    local fluid=e.get_fluid(index)
    if fluid and fluid.amount>0 and fluid.name~=allowed then return {ok=false,reason="petroleum_wrong_physical_fluid_box",index=index,position=q.position}end
    indices[#indices+1]=index
   end
  end
  if not mapping[2]or not mapping[5]or mapping[2]==mapping[5]then return {ok=false,reason="petroleum_required_runtime_storages_missing"}end
  storage_maps[e.unit_number]=mapping;storage_indices[e.unit_number]=indices
 else
  if not e.has_fluid_segment(1)then return {ok=false,reason="petroleum_native_segment_missing"}end
  local b=e.get_fluid_segment_extent_bounding_box(1)
  if b.right_bottom.x-b.left_top.x>limit or b.right_bottom.y-b.left_top.y>limit then return {ok=false,reason="petroleum_native_pipeline_extent_exceeded"}end
 end
 live[key(q)]=e
end
for _,edge in ipairs(args.edges)do local a=live[key(edge.a)];local b=live[key(edge.b)]
 local ai=storage_maps[a.unit_number]and storage_maps[a.unit_number][edge.a.fluidbox_index]or edge.a.fluidbox_index
 local bi=storage_maps[b.unit_number]and storage_maps[b.unit_number][edge.b.fluidbox_index]or edge.b.fluidbox_index
 local forward=false;local reverse=false
 for _,peer in pairs(a.get_fluid_box_neighbours(ai)or{})do if peer.entity==b and peer.index==bi then forward=true end end
 for _,peer in pairs(b.get_fluid_box_neighbours(bi)or{})do if peer.entity==a and peer.index==ai then reverse=true end end
 if not forward or not reverse then return {ok=false,reason="petroleum_reciprocal_typed_peer_missing",position=edge.a.position}end
 expected[id(edge.a,ai)]=expected[id(edge.a,ai)]or{};expected[id(edge.a,ai)][b.unit_number..":"..bi]=true
 expected[id(edge.b,bi)]=expected[id(edge.b,bi)]or{};expected[id(edge.b,bi)][a.unit_number..":"..ai]=true
end
for _,handoff in ipairs(args.handoffs or{})do local q=handoff.first_neighbor;local e=target(q.position,q.name);local src=live[key(args.outlet)]
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y
  or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="petroleum_bound_consumer_identity_changed"}end
 for name in pairs(e.get_fluid_contents())do if name~="petroleum-gas"then return {ok=false,reason="petroleum_bound_consumer_contaminated"}end end
 local forward=false;local reverse=false
 for _,p in pairs(src.get_fluid_box_neighbours(1)or{})do if p.entity==e and p.index==1 then forward=true end end
 for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do if p.entity==src and p.index==1 then reverse=true end end
 if not forward or not reverse then return {ok=false,reason="petroleum_bound_consumer_edge_missing"}end
 expected[id(args.outlet,1)]=expected[id(args.outlet,1)]or{};expected[id(args.outlet,1)][e.unit_number..":1"]=true
end
for _,q in ipairs(args.nodes)do if q.unit_number~=args.source.unit_number then
 local e=live[key(q)];local indices=storage_indices[e.unit_number]or{1}
 for _,index in ipairs(indices)do for _,peer in pairs(e.get_fluid_box_neighbours(index)or{})do
  if not(expected[id(q,index)]or{})[peer.entity.unit_number..":"..peer.index]then return {ok=false,reason="petroleum_unexpected_foreign_fluid_peer",position=q.position}end
 end end
end end
local outlet=live[key(args.outlet)];local amount=outlet.get_fluid_contents()["petroleum-gas"]or 0
if amount<=0 then return {ok=false,reason="petroleum_typed_output_empty"}end
local products={};for _,q in ipairs(args.nodes)do if q.name=="oil-refinery"then products[#products+1]={unit_number=q.unit_number,products_finished=live[key(q)].products_finished}end end
return {ok=true,tick=game.tick,outlet_unit=outlet.unit_number,petroleum=amount,refineries=products,reciprocal_edges=#args.edges,pipeline_limit=limit}
"""
