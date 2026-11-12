"""Normally smelted steel: opposing producers, typed feeds and fixed ports.

No iron/fuel insertion or handcrafting of steel is emitted. Installing a new
main-bus priority tap remains a separate whole-layout migration capability.
"""

from __future__ import annotations

from collections import defaultdict, deque
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield
from .board import entity
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities, port
from .transport import VECTORS, plan_connection, underground_proof, xy


def automatic_furnace_witness(live, raw_item, product_item):
    """A full automatic furnace can release its recipe while holding output."""
    if live.get("products_finished", 0) <= 0:
        return {"ok": False, "reason": "automatic_furnace_has_no_finished_products"}
    if live.get("recipe") not in (None, product_item):
        return {"ok": False, "reason": "automatic_furnace_wrong_recipe"}
    if set(live.get("inventory", {})) - {raw_item, product_item, "coal"}:
        return {"ok": False, "reason": "automatic_furnace_mixed_raw_or_fuel"}
    output = live.get("output_inventory", {})
    if set(output) - {product_item}:
        return {"ok": False, "reason": "automatic_furnace_wrong_output"}
    if live.get("remaining_burning_fuel", 0) <= 0 and live.get("inventory", {}).get("coal", 0) <= 0:
        return {"ok": False, "reason": "automatic_furnace_has_no_fuel"}
    if live.get("recipe") is None and output.get(product_item, 0) <= 0:
        return {"ok": False, "reason": "automatic_furnace_idle_without_expected_output"}
    return {"ok": True, "requires_native_idle_witness": live.get("recipe") is None}


IDLE_FURNACE_WITNESS_LUA = r"""
local function idle_furnace_witness(rows)
 for _,spec in ipairs(rows or {}) do local e=target(spec.position,spec.name)
  if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number then
   return {ok=false,reason="automatic_furnace_idle_identity_changed",position=spec.position} end
  local recipe=e.get_recipe()
  if recipe and recipe.name~=spec.product then return {ok=false,reason="automatic_furnace_wrong_recipe",position=spec.position} end
  if e.products_finished<=0 then return {ok=false,reason="automatic_furnace_has_no_finished_products",position=spec.position} end
  local output=e.get_output_inventory();local input=e.get_inventory(defines.inventory.crafter_input);local fuel=e.get_fuel_inventory()
  if not output or not input or not fuel then return {ok=false,reason="automatic_furnace_idle_inventory_missing"} end
  local produced=0;local coal=0
  for _,group in ipairs({{inventory=output,item=spec.product,kind="output"},{inventory=input,item=spec.raw,kind="input"},{inventory=fuel,item="coal",kind="fuel"}}) do
   for index=1,#group.inventory do local stack=group.inventory[index]
    if stack.valid_for_read then
     if stack.name~=group.item or stack.quality.name~="normal" then
      return {ok=false,reason="automatic_furnace_idle_contaminated_"..group.kind,position=spec.position} end
     if group.kind=="output" then produced=produced+stack.count elseif group.kind=="fuel" then coal=coal+stack.count end
    end
   end
  end
  if produced<=0 then return {ok=false,reason="automatic_furnace_idle_without_expected_normal_output",position=spec.position} end
  local burner=e.burner
  if coal<=0 and (not burner or burner.remaining_burning_fuel<=0) then return {ok=false,reason="automatic_furnace_has_no_fuel",position=spec.position} end
 end
 return {ok=true}
end
"""


def smelting_rates(catalog, raw_item, product_item, furnace_count=2, furnace_name="stone-furnace"):
    """Catalog-derived rated balances; energy_usage is exported J/game tick."""
    if type(furnace_count) is not int or furnace_count < 2:
        raise ValueError("at least two independent furnaces required")
    recipe, furnace, coal = catalog.recipes[product_item], catalog.entities[furnace_name], catalog.items["coal"]
    ingredients, products = recipe["ingredients"], recipe["products"]
    if (
        len(ingredients) != 1
        or ingredients[0]["name"] != raw_item
        or ingredients[0].get("type", "item") != "item"
        or len(products) != 1
        or products[0]["name"] != product_item
        or products[0].get("type", "item") != "item"
    ):
        raise ValueError("paired smelting requires the declared single raw item and deterministic product")
    categories = recipe.get("categories") or [recipe.get("category")]
    if not set(categories) & set(furnace.get("crafting_categories", [])) or not furnace.get("burner"):
        raise ValueError("catalog furnace cannot burner-smelt the declared product")
    yield_amount = _yield(products[0])
    values = [
        recipe["energy"],
        furnace["crafting_speed"],
        ingredients[0]["amount"],
        yield_amount,
        furnace["energy_usage"],
        coal["fuel_value"],
    ]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("positive finite recipe, furnace and coal catalog data required")
    cycles = furnace["crafting_speed"] * 60 / recipe["energy"]
    return {
        "furnace_count": furnace_count,
        "recipe_seconds": recipe["energy"],
        "recipe_yield": yield_amount,
        "crafting_speed": furnace["crafting_speed"],
        "product_per_minute": furnace_count * cycles * yield_amount,
        "raw_per_minute": furnace_count * cycles * ingredients[0]["amount"],
        "coal_per_minute_at_max_catalog_usage": furnace_count * furnace["energy_usage"] * 3600 / coal["fuel_value"],
        "burner_watts": furnace_count * furnace["energy_usage"] * 60,
        "fuel_basis": "catalog maximum burner J/tick divided by coal fuel_value; not measured consumption",
        "scope": "rated normal-quality recipe balance; supply, inserters and power require native commissioning",
    }


def steel_rates(catalog, furnace_count=2, furnace_name="stone-furnace"):
    rates = smelting_rates(catalog, "iron-plate", "steel-plate", furnace_count, furnace_name)
    return {**rates, "steel_per_minute": rates["product_per_minute"], "iron_per_minute": rates["raw_per_minute"]}


def steel_core(catalog, x=82, y=-40, *, per_side_count=1, reserved_pairs=3):
    return paired_smelting_core(
        catalog,
        x,
        y,
        raw_item="iron-plate",
        product_item="steel-plate",
        per_side_count=per_side_count,
        reserved_pairs=reserved_pairs,
    )


def paired_smelting_core(
    catalog,
    x,
    y,
    *,
    raw_item,
    product_item,
    input_inserter="inserter",
    output_inserter="inserter",
    per_side_count=1,
    reserved_pairs=3,
):
    """Two opposing rows feed opposite steel lanes; spare slots stay empty."""
    if (
        type(per_side_count) is not int
        or type(reserved_pairs) is not int
        or not 1 <= per_side_count <= reserved_pairs <= 3
        or any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 for v in (x, y))
    ):
        raise ValueError("integer furnace anchor and one to three reserved pairs required")
    if input_inserter not in ("inserter", "fast-inserter") or output_inserter not in ("inserter", "fast-inserter"):
        raise ValueError("supported native short inserters required")
    rates = (
        steel_rates(catalog, 2 * per_side_count)
        if product_item == "steel-plate" and raw_item == "iron-plate"
        else smelting_rates(catalog, raw_item, product_item, 2 * per_side_count)
    )
    es, producers, outputs = [], {"right": [], "left": []}, []
    for side, dy, pickup in (("right", 0, 0), ("left", 5, 8)):
        for i in range(per_side_count):
            cx = x + 4 * i
            furnace = entity("stone-furnace", cx, y + dy)
            producers[side].append(furnace)
            es.append(furnace)
            iy, oy = (y - 1.5, y + 1.5) if side == "right" else (y + 6.5, y + 3.5)
            arm = entity(output_inserter, cx + 0.5, oy, pickup)
            es += [
                entity(input_inserter, cx + 0.5, iy, pickup),
                arm,
                entity("small-electric-pole", cx + 1.5, y + 0.5 if side == "right" else y + 4.5),
            ]
            outputs.append({"inserter": arm, "lane": side})
    last_input = 4 * (per_side_count - 1)
    for row in (y - 2.5, y + 7.5):
        es += [entity("transport-belt", x + dx + 0.5, row, 4) for dx in range(-2, last_input + 1)]
    es += [entity("transport-belt", x - 1.5, y + dy + 0.5, 8) for dy in range(-2, 7)]
    es += [
        dict(
            entity("splitter", x - 2.5, y - 2, 4),
            _width=2,
            _height=1,
            splitter_input_priority="left",
            splitter_output_priority="none",
        )
    ]
    es += [entity("transport-belt", x + dx + 0.5, y - 2.5, 4) for dx in (-6, -5, -4)]
    es += [
        entity("transport-belt", x - 3.5, y - 4.5, 8),
        entity("transport-belt", x - 3.5, y - 3.5, 8),
        entity("transport-belt", x - 4.5, y - 0.5, 0),
        entity("transport-belt", x - 4.5, y - 1.5, 0),
    ]
    out_x = x + 4 * (reserved_pairs - 1) + 2.5
    es += [entity("transport-belt", x + dx + 0.5, y + 2.5, 4) for dx in range(int(out_x - x + 0.5))]
    ports = [
        port(raw_item, x - 3.5, y - 4.5, 8),
        port("coal", x - 4.5, y - 0.5, 0),
        dict(port(product_item, out_x, y + 2.5, 4, "output"), lane_items={"left": product_item, "right": product_item}),
    ]
    return _plan(
        es,
        ports,
        template="paired_steel_fixed_ports_v1" if product_item == "steel-plate" else "paired_smelting_fixed_ports_v1",
        recipe_name=product_item,
        rated=rates,
        producer_rows=producers,
        output_inserters=outputs,
        input_lane_items={"left": raw_item, "right": "coal"},
        per_side_count=per_side_count,
        reserved_pairs=reserved_pairs,
        reserved_area=[[x - 6.5, y - 5.5], [out_x + 1.5, y + 8.5]],
        power_port={"position": {"x": x + 1.5, "y": y + 0.5}, "name": "small-electric-pole"},
        underground_pairs=[],
        layout_policy="pcb-paired-steel-v1",
    )


def audit_steel_core(
    plan,
    catalog,
    *,
    raw_item="iron-plate",
    product_item="steel-plate",
    input_inserter="inserter",
    output_inserter="inserter",
):
    """Check footprint, automatic feed reach and genuine opposite-side outputs."""
    errors = []
    es = plan["entities"]
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    boxes = [geometry._occupied_by_plan([e]) for e in es]
    if sum(map(len, boxes)) != len(set().union(*boxes)):
        errors.append("prospective_footprint_overlap")
    belts = {xy(e): e for e in es if e["name"] == "transport-belt"}
    graph = defaultdict(set)
    for p, belt in belts.items():
        vx, vy = VECTORS[belt["direction"]]
        graph[p].add((p[0] + vx, p[1] + vy))
    for splitter in (e for e in es if e["name"] == "splitter"):
        if splitter["direction"] != 4:
            errors.append("unsupported_input_splitter_direction")
            continue
        sx, sy = xy(splitter)
        for row in (sy - 0.5, sy + 0.5):
            graph[(sx, row)].update(((sx + 1, sy - 0.5), (sx + 1, sy + 0.5)))
    reached = defaultdict(set)
    for input_port in (p for p in plan["ports"] if p["direction"] == "input"):
        queue = deque([xy(input_port)])
        seen = set()
        while queue:
            p = queue.popleft()
            if p in seen:
                continue
            seen.add(p)
            reached[p].add(input_port["item"])
            queue.extend(graph[p])
    inputs, outputs, output_producers = 0, 0, set()
    furnaces = [e for e in es if e["name"] == "stone-furnace"]
    output_port = next(p for p in plan["ports"] if p["direction"] == "output")
    for row in plan["output_inserters"]:
        arm = row["inserter"]
        ax, ay = xy(arm)
        vx, vy = VECTORS[arm["direction"]]
        if arm["name"] != output_inserter:
            errors.append("wrong_output_inserter_type")
        pickup, drop = (ax + vx, ay + vy), (ax - vx, ay - vy)
        furnace = next((e for e in furnaces if abs(xy(e)[0] - pickup[0]) < 1 and abs(xy(e)[1] - pickup[1]) < 1), None)
        belt = belts.get(drop)
        expected_lane = "right" if arm["direction"] == 0 else "left"
        if not furnace or not belt or belt["direction"] != 4 or row["lane"] != expected_lane:
            errors.append("output_not_native_opposite_lane")
            continue
        if xy(furnace) in output_producers:
            errors.append("duplicate_output_producer")
        output_producers.add(xy(furnace))
        p, seen = drop, set()
        while p in belts and p not in seen and p != xy(output_port):
            seen.add(p)
            b = belts[p]
            dx, dy = VECTORS[b["direction"]]
            p = p[0] + dx, p[1] + dy
        if p != xy(output_port):
            errors.append("steel_output_not_connected")
        else:
            outputs += 1
    for furnace in furnaces:
        fx, fy = xy(furnace)
        for arm in (
            e
            for e in es
            if e["name"] in ("inserter", "fast-inserter") and e not in [r["inserter"] for r in plan["output_inserters"]]
        ):
            ax, ay = xy(arm)
            vx, vy = VECTORS[arm["direction"]]
            if abs(ax - vx - fx) < 1 and abs(ay - vy - fy) < 1:
                if arm["name"] != input_inserter:
                    errors.append("wrong_input_inserter_type")
                if reached[(ax + vx, ay + vy)] != {raw_item, "coal"}:
                    errors.append("furnace_missing_automatic_iron_or_coal")
                else:
                    inputs += 1
    if inputs != len(furnaces) or outputs != len(furnaces):
        errors.append("every_furnace_needs_input_and_output")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "furnaces": len(furnaces),
        "automatic_inputs": inputs,
        "opposite_lane_outputs": outputs,
        "rated": (
            steel_rates(catalog, len(furnaces))
            if product_item == "steel-plate" and raw_item == "iron-plate"
            else smelting_rates(catalog, raw_item, product_item, len(furnaces))
        ),
    }


class SteelProduction:
    def __init__(self, game, builder, catalog, *, iron_source=None, coal_source=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.iron_source, self.coal_source = iron_source, coal_source
        self.path = Path(game.cfg.runtime_dir) / "steel-production.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def prepare(self, obs, *, iron_source=None, coal_source=None, anchors=((82, -40), (94, -40), (82, -24))):
        """Read-only; requires already installed exact typed surplus/coal ports."""
        if not obs.get("technologies", {}).get("steel-processing") or not obs.get("enabled_recipes", {}).get(
            "steel-plate"
        ):
            return {"ok": False, "reason": "native_steel_processing_required"}
        iron_source, coal_source = iron_source or self.iron_source, coal_source or self.coal_source
        if (
            not iron_source
            or iron_source.get("item") != "iron-plate"
            or iron_source.get("direction") != "output"
            or iron_source.get("priority") != "surplus"
            or not isinstance(iron_source.get("priority_splitter"), dict)
        ):
            return {"ok": False, "reason": "steel_requires_installed_surplus_main_bus_tap"}
        if not coal_source or coal_source.get("item") != "coal" or coal_source.get("direction") != "output":
            return {"ok": False, "reason": "steel_requires_installed_typed_coal_output"}
        sources = [iron_source, coal_source]
        splitter = iron_source["priority_splitter"]
        if (
            any(
                type(p.get("unit_number")) is not int
                or p["unit_number"] < 1
                or type(p.get("facing")) is not int
                or p["facing"] not in VECTORS
                for p in sources
            )
            or type(splitter.get("unit_number")) is not int
            or splitter["unit_number"] < 1
            or any(
                not isinstance(p.get("position"), dict)
                or set(p["position"]) != {"x", "y"}
                or any(
                    type(p["position"][axis]) not in (int, float) or not math.isfinite(p["position"][axis])
                    for axis in ("x", "y")
                )
                for p in sources + [splitter]
            )
        ):
            return {"ok": False, "reason": "steel_source_exact_native_identities_required"}
        payload = json.dumps(json.dumps({"sources": sources, "splitter": splitter}, separators=(",", ":")))
        native = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """)
for _,spec in ipairs(args.sources) do
 local e=target(spec.position,"transport-belt")
 if not e or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or e.direction~=spec.facing
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return {ok=false,reason="steel_typed_source_changed"} end
 for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents()) do
  if item.name~=spec.item then return {ok=false,reason="steel_typed_source_contaminated"} end
 end end
end
local spec=args.splitter;local e=target(spec.position,"splitter")
if not e or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or e.direction~=0
 or e.position.x~=spec.position.x or e.position.y~=spec.position.y
 or e.splitter_input_priority~="left" or e.splitter_output_priority~="left" then return {ok=false,reason="steel_surplus_native_priority_missing"} end
return {ok=true}
"""
        )
        if not native.get("ok"):
            return native
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "steel_board_identity_changed"}
        candidates = []
        failures = []

        def failed(core, stage, result, **extra):
            first = core["producer_rows"]["right"][0]["position"]
            return {
                "candidate": dict(first),
                "reserved_area": core["reserved_area"],
                "stage": stage,
                "reason": result.get("reason", "unspecified_native_failure"),
                **{key: result[key] for key in ("visited", "native_reason", "position") if key in result},
                **extra,
            }

        for x, y in anchors:
            core = steel_core(self.catalog, x, y)
            conflicts = [
                name
                for name, b in board["blocks"].items()
                if "reserved_area" in b and overlap(core["reserved_area"], b["reserved_area"])
            ]
            if conflicts:
                failures.append(
                    failed(core, "reservation", {"reason": "steel_site_overlaps_reserved_block"}, blocks=conflicts)
                )
            else:
                candidates.append(core)
        legal = survey_slots(self.game, [[c["reserved_area"]] for c in candidates])
        if not legal.get("ok"):
            return legal
        legal_indices = legal.get("legal", [])
        for index, core in enumerate(candidates, 1):
            if index not in legal_indices:
                failures.append(failed(core, "site_survey", {"reason": "native_land_resource_or_structure_rejected"}))
        for index in legal_indices:
            core = candidates[index - 1]
            audit = audit_steel_core(core, self.catalog)
            if not audit["ok"]:
                return {"ok": False, "reason": "steel_core_audit_failed", "audit": audit}
            es, routes = core["entities"][:], []
            for source, destination in zip(sources, core["ports"][:2]):
                route = plan_connection(self.game, self.builder, obs, source, destination, es)
                if not route.get("ok"):
                    failures.append(
                        failed(
                            core,
                            "raw_feed",
                            route,
                            item=source["item"],
                            source=source["position"],
                            destination=destination["position"],
                        )
                    )
                    break
                routes.append(route)
                es = merge_entities(es, route["entities"])
            if len(routes) != 2:
                continue
            power = connect_power_groups(self.game, self.builder, obs, [core], es)
            if not power.get("ok"):
                failures.append(failed(core, "power", power))
                continue
            es = merge_entities(es, power["entities"])
            check = preflight(self.game, self.builder, obs, es)
            if not check.get("ok"):
                failures.append(failed(core, "whole_preflight", check))
                continue
            obstacles = {(e["name"], *xy(e)): e for r in routes + [check] for e in r["clear_obstacles"]}
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                underground_pairs=[p for r in routes for p in r["underground_pairs"]],
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-steel-v1",
            )
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "build",
                "plan": plan,
                "core": core,
                "audit": audit,
                "sources": sources,
                "surplus_splitter": splitter,
                "route_metrics": [r["route_metrics"] for r in routes],
            }
        evidence = {
            "candidate_count": len(anchors),
            "legal_site_count": len(legal_indices),
            "candidate_failures": failures,
        }
        return {
            "ok": False,
            "reason": "no_steel_site_with_complete_native_connections",
            "failures": failures,
            "evidence": evidence,
        }

    def step(self, obs):
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get("ok"):
                return {"status": "blocked", **ready}
            self.state = ready
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "steel_world_or_catalog_changed"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "steel_board_identity_changed"}
        if board["blocks"].get("steel_production") != self.state["plan"]:
            if "steel_production" in board["blocks"]:
                return {"status": "blocked", "reason": "steel_reserved_block_changed"}
            board["blocks"]["steel_production"] = self.state["plan"]
            _atomic_json(board_path, board)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        evidence = self.proof(obs)
        if not evidence:
            return {"status": "waiting", "reason": "waiting_for_automatic_steel_on_both_native_output_lanes"}
        self.state["phase"] = "verified"
        _atomic_json(self.path, self.state)
        return {
            "status": "succeeded",
            "reason": "normally belt-supplied opposing steel furnaces commissioned",
            "evidence": evidence,
        }

    def proof(self, obs, *, consumer_furnace_units=None):
        self.last_proof_failure = None
        if consumer_furnace_units is not None and (
            self.state.get("phase") != "verified"
            or not isinstance(consumer_furnace_units, (list, tuple))
            or not consumer_furnace_units
            or any(type(unit) is not int or unit < 1 for unit in consumer_furnace_units)
            or len(set(consumer_furnace_units)) != len(consumer_furnace_units)
        ):
            self.last_proof_failure = {"reason": "steel_consumer_requires_commissioned_exact_furnaces"}
            return None
        if (
            not self.state
            or self.state["world_id"] != obs["world_id"]
            or self.state["catalog_fingerprint"] != self.catalog.fingerprint
            or not underground_proof(obs, self.state["plan"])
        ):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        furnaces = []
        idle_furnaces = []
        for source in self.state["sources"]:
            live = actual.get(("transport-belt", *xy(source)))
            if (
                not live
                or live["unit_number"] != source["unit_number"]
                or live["direction"] != source["facing"]
                or set(live.get("belt_inventory", {})) - {source["item"]}
            ):
                return None
        wanted = self.state["surplus_splitter"]
        splitter = actual.get(("splitter", *xy(wanted)))
        if (
            not splitter
            or splitter["unit_number"] != wanted["unit_number"]
            or splitter["direction"] != 0
            or splitter.get("splitter_input_priority") != "left"
            or splitter.get("splitter_output_priority") != "left"
        ):
            return None
        for wanted in self.state["plan"]["entities"]:
            live = actual.get((wanted["name"], *xy(wanted)))
            if not live or (wanted["name"] != "stone-furnace" and live["direction"] != wanted["direction"]):
                return None
            if wanted["name"] == "stone-furnace":
                witness = automatic_furnace_witness(live, "iron-plate", "steel-plate")
                if not witness["ok"]:
                    self.last_proof_failure = dict(witness, unit_number=live["unit_number"])
                    return None
                if witness["requires_native_idle_witness"]:
                    idle_furnaces.append(
                        dict(
                            name=live["name"],
                            position=live["position"],
                            unit_number=live["unit_number"],
                            raw="iron-plate",
                            product="steel-plate",
                        )
                    )
                furnaces.append(live)
            if wanted["name"] == "inserter" and (
                not live.get("electric_network_connected") or live.get("energy", 0) <= 0
            ):
                return None
            if wanted["name"] == "splitter" and any(
                live.get(k) != wanted[k] for k in ("splitter_input_priority", "splitter_output_priority")
            ):
                return None
        if consumer_furnace_units is not None and sorted(consumer_furnace_units) != sorted(
            e["unit_number"] for e in furnaces
        ):
            self.last_proof_failure = {"reason": "steel_consumer_furnace_identity_changed"}
            return None
        belt_specs = [
            e
            for e in self.state["core"]["entities"]
            if e["name"] == "transport-belt"
            and e["direction"] == 4
            and e["position"]["y"] == self.state["core"]["ports"][-1]["position"]["y"]
        ]
        first_furnace = self.state["core"]["producer_rows"]["right"][0]["position"]
        input_belts = [
            e
            for e in self.state["core"]["entities"]
            if e["name"] == "transport-belt"
            and e["direction"] == 4
            and e["position"]["x"] >= first_furnace["x"] - 1.5
            and e["position"]["y"] in (first_furnace["y"] - 2.5, first_furnace["y"] + 7.5)
        ]
        payload = json.dumps(
            json.dumps(
                {"output": belt_specs, "input": input_belts, "idle_furnaces": idle_furnaces}, separators=(",", ":")
            )
        )
        lanes = self.game.query(
            IDLE_FURNACE_WITNESS_LUA
            + """local args=helpers.json_to_table("""
            + payload
            + """);local checked=idle_furnace_witness(args.idle_furnaces)
if not checked.ok then return checked end
local result={ok=true,tick=game.tick}
for _,group in ipairs({"input","output"}) do local lanes={{},{}}
 for _,spec in ipairs(args[group]) do local e=target(spec.position,"transport-belt")
  if not e or e.force~=f or e.direction~=4 then return {ok=false} end
  for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do lanes[lane][v.name]=(lanes[lane][v.name] or 0)+v.count end end
 end
 if group=="input" then result.input_left=lanes[1];result.input_right=lanes[2] else result.left=lanes[1];result.right=lanes[2] end
end
return result"""
        )
        if not lanes.get("ok"):
            self.last_proof_failure = lanes
        if not lanes.get("ok"):
            return None
        if any(set(lanes.get(side, {})) - {"steel-plate"} for side in ("left", "right")):
            self.last_proof_failure = {"reason": "steel_output_lane_contaminated"}
            return None
        buffered = all(lanes.get(side, {}).get("steel-plate", 0) > 0 for side in ("left", "right"))
        if not buffered and consumer_furnace_units is None:
            self.last_proof_failure = {
                "reason": "steel_commissioning_requires_both_buffered_output_lanes",
                "native_output_lanes": lanes,
            }
            return None
        for side, item in self.state["core"]["input_lane_items"].items():
            content = lanes.get("input_" + side, {})
            if set(content) - {item} or content.get(item, 0) <= 0:
                return None
        return {
            "tick": obs["tick"],
            "furnace_units": [e["unit_number"] for e in furnaces],
            "products_finished": [e["products_finished"] for e in furnaces],
            "native_output_lanes": lanes,
            "output_buffers_present": buffered,
            "rated": self.state["core"]["rated"],
            "scope": (
                "commissioned exact furnaces with normal automatic inputs for an active consumer; output buffers may be consumed; no sustained-rate acceptance"
                if consumer_furnace_units is not None
                else "ordinary automatic steel commissioning; no sustained-rate acceptance"
            ),
        }
