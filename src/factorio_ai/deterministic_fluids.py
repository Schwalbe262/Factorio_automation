"""Live-prototype fluid geometry and deterministic oil production support.

Recipe fluidbox indices are relative to input/output boxes, whereas prototype
box indices address all boxes. Keeping these two indices distinct is essential:
basic oil's output index 3 is refinery box 5, the petroleum outlet.
"""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .deterministic_state import _atomic_json
from .factory_templates import build_template, DIRECTIONS


def _report(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


def _point(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return float(value["x"]), float(value["y"])
    return float(value[0]), float(value[1])


def _position(x: float, y: float) -> dict:
    return {"x": round(x, 3), "y": round(y, 3)}


def _rotate(x: float, y: float, direction: int) -> tuple[float, float]:
    for _ in range(direction // 4):
        x, y = -y, x
    return x, y


def _sequence(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _bounds_size(entity: dict) -> tuple[int, int]:
    box = entity.get("selection_box") or entity.get("collision_box")
    if not box:
        raise ValueError("live entity footprint is missing")
    if isinstance(box, dict):
        left, top = _point(box["left_top"])
        right, bottom = _point(box["right_bottom"])
    else:
        left, top = _point(box[0])
        right, bottom = _point(box[1])
    return math.ceil(right - left), math.ceil(bottom - top)


def exterior_connections(box: dict) -> list[dict]:
    """Convert north prototype INTERNAL connection coordinates to pipe tiles.
    Factorio 2.1 exports four rotated ``positions``; the first is north.
    A connection's ``direction`` points from that internal tile to its neighbour.
    """
    result = []
    for connection in _sequence(box.get("pipe_connections")):
        if connection.get("connection_type", "normal") != "normal":
            continue
        positions = _sequence(connection.get("positions"))
        point = positions[0] if positions else connection.get("position")
        direction = connection.get("direction")
        if point is None or direction not in DIRECTIONS:
            raise ValueError("live fluid connection requires north position and cardinal direction")
        x, y = _point(point)
        dx, dy = DIRECTIONS[direction]
        result.append({"position": _position(x + dx, y + dy), "facing": direction})
    if not result:
        raise ValueError("fluid box has no supported normal connection")
    return result


def recipe_fluid_geometry(recipe: dict, entity: dict) -> dict:
    """Bind typed recipe fluids to real role-relative fluidbox slots; fail closed."""
    width, height = _bounds_size(entity)
    boxes = sorted(_sequence(entity.get("fluidbox_prototypes")), key=lambda b: int(b.get("index", 0)))
    ports, fluid_inputs, fluid_outputs = [], [], []
    used_boxes = set()
    for role, field, names in (("input", "ingredients", fluid_inputs), ("output", "products", fluid_outputs)):
        materials = [row for row in recipe.get(field, []) if row.get("type", "item") == "fluid"]
        candidates = [box for box in boxes if box.get("production_type") in {role, "input-output"}]
        assigned = {}
        for ordinal, material in enumerate(materials, 1):
            slot = material.get("fluidbox_index")
            if slot is not None:
                if isinstance(slot, bool) or not isinstance(slot, (int, float)) or int(slot) != slot or not 1 <= slot <= len(candidates):
                    raise ValueError(f"invalid {role} fluidbox index for {material['name']}")
                assigned[ordinal] = int(slot)
        for ordinal, material in enumerate(materials, 1):
            slot = assigned.get(ordinal)
            if slot is None:
                slot = next((i for i, box in enumerate(candidates, 1)
                             if i not in assigned.values() and box.get("index") not in used_boxes
                             and (not box.get("filter") or box["filter"] == material["name"])), None)
                if slot is None:
                    raise ValueError(f"no {role} fluidbox available for {material['name']}")
                assigned[ordinal] = slot
            box = candidates[slot - 1]
            if box.get("index") in used_boxes:
                raise ValueError("two recipe fluids require the same prototype fluidbox")
            if box.get("filter") and box["filter"] != material["name"]:
                raise ValueError("recipe fluid contradicts prototype fluid filter")
            used_boxes.add(box.get("index"))
            connection = exterior_connections(box)[0]
            ports.append({"item": material["name"], "direction": role,
                          "position": connection["position"], "fluidbox_index": box["index"],
                          "recipe_fluidbox_index": slot})
            names.append(material["name"])
    if not ports:
        raise ValueError("recipe has no fluid inputs or outputs")
    return {"width": width, "height": height, "fluid_ports": ports,
            "fluid_inputs": fluid_inputs, "fluid_outputs": fluid_outputs}


class FluidProduction:
    """Attach ``factory`` after construction for shared site/power/item routing.

    The factory must expose reserve_site, ensure_power_connection, ensure_product
    and connect_input. Reports always expose output/input ports once a plan exists.
    """
    def __init__(self, game: Any, builder: Any, catalog: Any):
        self.game, self.builder, self.catalog = game, builder, catalog
        self._fingerprint = getattr(catalog, "fingerprint", None)
        self.factory: Any = None
        self.path = Path(game.cfg.runtime_dir) / "fluid-production.json"
        self.state: dict[str, Any] = {}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("schema_version") != 1:
                raise ValueError("unsupported fluid production checkpoint")

    def _sync(self, observation: dict) -> None:
        world = observation.get("world_id")
        if not world:
            raise ValueError("fluid production observation requires world_id")
        if self.state.get("world_id") != world or self.state.get("catalog_fingerprint") != self._fingerprint:
            self.state = {"schema_version": 1, "world_id": world, "catalog_fingerprint": self._fingerprint,
                          "sources": {}, "links": {}, "buffers": {}}
            self._save()

    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def plan(self, recipe: str, machine: str, anchor: dict, rotation: int = 0, *, count: int = 1) -> dict:
        try:
            row, entity = self.catalog.recipes[recipe], self.catalog.entities[machine]
            categories = set(row.get("categories") or [row.get("category")])
            if not categories.intersection(entity.get("crafting_categories", [])):
                raise ValueError(f"{machine} cannot craft {recipe}'s live category")
            geometry = recipe_fluid_geometry(row, entity)
            products = row.get("products", [])
            solid_outputs = [p["name"] for p in products if p.get("type", "item") == "item"]
            if len(solid_outputs) > 1:
                raise ValueError("multiple solid recipe outputs require a separate output template")
            output = solid_outputs[0] if solid_outputs else products[0]["name"]
            result = build_template("refinery_row" if machine == "oil-refinery" else "fluid_machine_row",
                                    recipe=recipe, machine=machine, count=count,
                                    inputs=[i["name"] for i in row.get("ingredients", [])], output=output,
                                    prototype_geometry=geometry, anchor=anchor, rotation=rotation)
            if not result["ok"]:
                return result
            for port in result["ports"]:
                if port["kind"] == "fluid":
                    binding = next(p for p in geometry["fluid_ports"]
                                   if p["item"] == port["item"] and p["direction"] == port["direction"])
                    port["fluidbox_index"] = binding["fluidbox_index"]
                    port["recipe_fluidbox_index"] = binding["recipe_fluidbox_index"]
            result.update(recipe=recipe, machine=machine, machine_count=count,
                          coproducts=[p["name"] for p in products if p.get("type") == "fluid"],
                          input_rates={i["name"]: float(i["amount"]) * 60 * float(entity.get("crafting_speed", 1)) * count / float(row["energy"])
                                       for i in row.get("ingredients", [])})
            return result
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, StopIteration) as exc:
            return {"ok": False, "reason": str(exc), "entities": [], "ports": [], "required_items": {}}

    @staticmethod
    def _decorate(result: dict, plan: dict) -> dict:
        result = dict(result)
        result["evidence"] = {**result.get("evidence", {}),
                              "ports": [p for p in plan.get("ports", []) if p["direction"] == "output"],
                              "input_ports": [p for p in plan.get("ports", []) if p["direction"] == "input" and p["kind"] != "power"],
                              "flow_verified": result.get("evidence", {}).get("flow_verified", False)}
        return result

    def _pick_recipe(self, observation: dict, product: str) -> str | None:
        preferred = {"heavy-oil": "advanced-oil-processing", "light-oil": "advanced-oil-processing",
                     "petroleum-gas": "basic-oil-processing", "solid-fuel": "solid-fuel-from-petroleum-gas"}
        if product == "petroleum-gas" and "advanced-oil-processing" in self.state.get("sources", {}):
            preferred[product] = "advanced-oil-processing"
        name = preferred.get(product, product)
        if name in self.catalog.recipes:
            return name
        return None

    def _pick_machine(self, recipe: str, observation: dict) -> str | None:
        categories = set(self.catalog.recipes[recipe].get("categories") or [self.catalog.recipes[recipe].get("category")])
        existing = {e["name"] for e in observation.get("entities", [])}
        enabled = observation.get("enabled_recipes", {})
        for name in ("chemical-plant", "oil-refinery", "assembling-machine-2", "assembling-machine-3"):
            entity = self.catalog.entities.get(name)
            if entity and categories.intersection(entity.get("crafting_categories", [])) and (enabled.get(name) or name in existing):
                return name
        return None

    def ensure_source(self, observation: dict, product: str, amount: float = 100,
                      *, rate_per_minute: float | None = None, _stack: tuple[str, ...] = ()) -> dict:
        """Ensure a connected continuous source; success proves nonzero output.

        ``amount`` is downstream demand context, not a stock reservation.
        ``rate_per_minute`` sizes nominal machine capacity. Observed throughput
        remains the supervisor's responsibility; a template never proves that rate.
        """
        self._sync(observation)
        if not math.isfinite(amount) or amount <= 0 or (rate_per_minute is not None and (not math.isfinite(rate_per_minute) or rate_per_minute <= 0)):
            return _report("blocked", "source amount/rate must be positive and finite")
        if self.factory is None:
            return _report("blocked", "fluid production requires the shared factory site and power router")
        if product in {"water", "crude-oil"}:
            result = self._ensure_raw_source(observation, product, amount)
            if rate_per_minute is None or result.get("status") != "succeeded" or result.get("type"):
                return result
            return self._raw_capacity(observation, product, rate_per_minute, result)
        recipe = self._pick_recipe(observation, product)
        if not recipe:
            return _report("blocked", "required fluid production recipe is unavailable", product=product)
        if not observation.get("enabled_recipes", {}).get(recipe):
            return self.factory.request_recipe_unlock(observation, recipe)
        return self._ensure_recipe(observation, recipe, product, amount, rate_per_minute, _stack)

    def _ensure_recipe(self, observation: dict, recipe: str, product: str, amount: float,
                       rate: float | None, stack: tuple[str, ...]) -> dict:
        if recipe in stack:
            return _report("blocked", "fluid dependency cycle", recipe=recipe, stack=stack)
        machine = self._pick_machine(recipe, observation)
        if not machine:
            categories = set(self.catalog.recipes[recipe].get("categories") or [self.catalog.recipes[recipe].get("category")])
            for name in ("chemical-plant", "oil-refinery", "assembling-machine-2", "assembling-machine-3"):
                if categories.intersection(self.catalog.entities.get(name, {}).get("crafting_categories", [])):
                    return self.factory.request_recipe_unlock(observation, name)
            return _report("blocked", "no unlocked live machine for fluid recipe", recipe=recipe)
        row = self.catalog.recipes[recipe]
        output = sum(float(p.get("amount", 0)) * float(p.get("probability", 1))
                     for p in row["products"] if p["name"] == product)
        if not math.isfinite(output) or output <= 0:
            return _report("blocked", "recipe has no deterministic requested product output")
        from .deterministic_machine_ports import ARM_BUDGETS
        def capacity(plan: dict) -> float:
            name = plan.get("machine", machine)
            count = plan.get("machine_count", 1)
            if type(count) is not int or not 1 <= count <= 64:
                raise ValueError("fluid cell has invalid saved machine count")
            per_machine = 60 * float(self.catalog.entities[name].get("crafting_speed", 1)) * output / float(row["energy"])
            seen = set()
            for port in plan.get("ports", []):
                if port["kind"] != "item":
                    continue
                index = port.get("machine_index", 0 if count == 1 else None)
                if type(index) is not int or not 0 <= index < count:
                    raise ValueError("fluid material port has no valid owning machine")
                values = row["ingredients"] if port["direction"] == "input" else row["products"]
                amount = sum(float(value["amount"]) for value in values if value["name"] == port["item"])
                dx, dy = DIRECTIONS[port["facing"]]
                sign = 1 if port["direction"] == "input" else -1
                point = {"x": port["position"]["x"] + 2 * dx * sign, "y": port["position"]["y"] + 2 * dy * sign}
                arms = [e for e in plan["entities"] if e["position"] == point and e["name"] in ARM_BUDGETS
                        and e.get("direction", 0) == (port["facing"] + 8) % 16]
                if len(arms) != 1 or amount <= 0:
                    raise ValueError("fluid cell has unsupported solid material port geometry")
                per_machine = min(per_machine, ARM_BUDGETS[arms[0]["name"]] * output / amount)
                seen.add((index, port["direction"], port["item"]))
            required = {(index, direction, value["name"]) for index in range(count)
                        for direction, values in (("input", row["ingredients"]), ("output", row["products"]))
                        for value in values if value.get("type", "item") == "item"}
            if not required.issubset(seen):
                raise ValueError("fluid cell is missing a required solid material port")
            # A legacy mixed-arm row uses its slowest arm as a conservative
            # per-machine allowance; expansion cells each contain one machine.
            return per_machine * count
        unit_plan = self.plan(recipe, machine, {"x": .5, "y": .5})
        if not unit_plan.get("ok"):
            return _report("blocked", unit_plan.get("reason", "fluid capacity geometry unavailable"), recipe=recipe)
        try:
            unit_capacity = capacity(unit_plan)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            return _report("blocked", str(error), recipe=recipe)
        if not math.isfinite(unit_capacity) or unit_capacity <= 0:
            return _report("blocked", "fluid machine has no positive nominal capacity", recipe=recipe)
        plan = self.state["sources"].get(recipe)
        if plan is None:
            origin = self.plan(recipe, machine, {"x": .5, "y": .5})
            if not origin["ok"]:
                return _report("blocked", origin["reason"], recipe=recipe)
            plan = self.factory.reserve_site(origin, "fluid:" + recipe, observation)
            if not plan.get("ok"):
                return _report("blocked", plan.get("reason", "no fluid site"))
            self.state["sources"][recipe] = plan
            self._save()
        prefix = recipe + ":capacity:"
        cells = [(recipe, plan)] + sorted((key, cell) for key, cell in self.state["sources"].items()
                                          if key.startswith(prefix))
        try:
            nominal = sum(capacity(cell) for _, cell in cells)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            return self._decorate(_report("blocked", str(error), recipe=recipe), plan)
        machine_count = sum(int(cell.get("machine_count", 1)) for _, cell in cells)
        additional = max(0, math.ceil(((rate or 0) - nominal) / unit_capacity - 1e-9))
        if machine_count + additional > 64:
            return self._decorate(_report("blocked", "fluid recipe capacity exceeds bounded 64-machine expansion", recipe=recipe), plan)
        for _ in range(additional):
            index = 1
            while prefix + str(index) in self.state["sources"]:
                index += 1
            key = prefix + str(index)
            origin = self.plan(recipe, machine, {"x": .5, "y": .5})
            if not origin.get("ok"):
                return self._decorate(_report("blocked", origin.get("reason", "fluid capacity geometry unavailable")), plan)
            origin["capacity_key"] = key
            extra = self.factory.reserve_site(origin, "fluid:" + key, observation,
                                              reference=plan["entities"][0]["position"])
            if not extra.get("ok"):
                return self._decorate(_report("blocked", extra.get("reason", "fluid capacity site unavailable")), plan)
            self.state["sources"][key] = extra
            self._save()
            cells.append((key, extra))
            nominal += capacity(extra)
            machine_count += int(extra.get("machine_count", 1))
        # Keep the original ports stable, including legacy multi-machine rows.
        # Every additional outlet must join its same-material primary network.
        outputs = {}
        for port in plan["ports"]:
            if port["direction"] == "output":
                outputs.setdefault((port["kind"], port["item"]), port)
        input_rates = ({(i.get("type", "item"), i["name"]): float(i["amount"]) * rate / output
                        for i in row["ingredients"]} if rate is not None else {})
        sources = {}
        for cell_key, cell in cells:
            built = self.builder.ensure_plan(observation, cell)
            if built.get("status") != "succeeded" or built.get("type"):
                return self._decorate(built, plan)
            powered = self.factory.ensure_power_connection(observation, "fluid:" + cell_key, cell)
            if powered.get("status") != "succeeded" or powered.get("type"):
                return self._decorate(powered, plan)
            # Shared tanks must receive every coproduct before any crude feed.
            if len(cell.get("coproducts", [])) > 1:
                for fluid in cell["coproducts"]:
                    buffered = self._ensure_buffer(observation, fluid, cell)
                    if buffered.get("status") != "succeeded" or buffered.get("type"):
                        return self._decorate(buffered, plan)
            for port in cell["ports"]:
                if port["direction"] != "input" or port["kind"] == "power":
                    continue
                material = port["kind"], port["item"]
                if material not in sources:
                    kwargs = {"rate_per_minute": input_rates[material]} if material in input_rates else {}
                    source = (self.ensure_source(observation, port["item"], _stack=(*stack, recipe), **kwargs)
                              if port["kind"] == "fluid" else self.factory.ensure_product(observation, port["item"], **kwargs))
                    if source.get("status") != "succeeded" or source.get("type"):
                        return self._decorate(source, plan)
                    available = [p for p in source.get("evidence", {}).get("ports", [])
                                 if p["kind"] == port["kind"] and p["item"] == port["item"] and p["direction"] == "output"]
                    if not available:
                        return self._decorate(_report("blocked", "upstream source exposes no compatible output port", item=port["item"]), plan)
                    sources[material] = available[0]
                key = f"fluid:{cell_key}:{port['machine_index']}:{port['item']}"
                connected = (self._connect_pipe(observation, sources[material], port, key, cell)
                             if port["kind"] == "fluid" else self.factory.connect_input(observation, sources[material], port, key))
                if connected.get("status") != "succeeded" or connected.get("type"):
                    return self._decorate(connected, plan)
            for port in cell["ports"]:
                if port["direction"] != "output":
                    continue
                destination = outputs.get((port["kind"], port["item"]))
                if destination is None:
                    return self._decorate(_report("blocked", "fluid capacity outlet differs from its primary recipe", item=port["item"]), plan)
                if port == destination:
                    continue
                key = f"fluid:{cell_key}:output:{port.get('machine_index', 0)}:{port['item']}"
                merged = (self._connect_pipe(observation, port, destination, key,
                                            {"entities": cell["entities"] + plan["entities"]})
                          if port["kind"] == "fluid" else self.factory._merge_output(observation, port, destination, key))
                if merged.get("status") != "succeeded" or merged.get("type"):
                    return self._decorate(merged, plan)
        observed_plan = {**plan, "entities": [entity for _, cell in cells for entity in cell["entities"]]}
        evidence = self._source_evidence(observation, observed_plan, product)
        # Once all routes exist, an empty product buffer is a real production wait.
        status = "succeeded" if evidence["available"] > 0 else "waiting"
        return self._decorate(_report(status, "fluid recipe output observed" if status == "succeeded" else "waiting for connected fluid recipe production",
                                      **evidence, flow_verified=status == "succeeded", throughput_verified=False,
                                      machines_constructed=machine_count, requested_rate_per_minute=rate,
                                      nominal_capacity_per_minute=nominal, input_rates_per_minute={name: value for (_, name), value in input_rates.items()},
                                      capacity_basis="base prototype speed and conservative installed item-arm allowances",
                                      raw_source_capacity_verified=rate is not None), plan)

    def _raw_capacity(self, observation: dict, fluid: str, rate: float, result: dict) -> dict:
        """Reject insufficient extraction instead of crediting idle refinery rows.

        This checks the existing owned source only. Selecting and joining extra
        oil wells is a separate capability; insufficient yield remains explicit.
        """
        plan = self.state["sources"].get("raw:" + fluid, {})
        name = "pumpjack" if fluid == "crude-oil" else "offshore-pump"
        machines = [e for e in plan.get("entities", []) if e["name"] == name]
        if len(machines) != 1:
            return _report("blocked", "raw fluid capacity requires an exact owned source", fluid=fluid, requested_rate_per_minute=rate)
        payload = json.dumps(json.dumps(machines[0], separators=(",", ":")))
        survey = self.game.query('''
--[[ raw_fluid_capacity: current nominal extraction, never measured throughput. ]]
local x=helpers.json_to_table(''' + payload + ''');local e=target(x.position,x.name)
if not e or e.force~=f then return {ok=false,reason="raw fluid source is missing or foreign"} end
local rate;local amount
if e.type=="offshore-pump" then
 rate=e.prototype.get_pumping_speed(e.quality)*3600
else
 local wells=s.find_entities_filtered{position=e.position,radius=0.1,name="crude-oil"}
 if #wells~=1 then return {ok=false,reason="owned pumpjack has no unique crude oil well"} end
 local well=wells[1];local proto=well.prototype;local mine=proto.mineable_properties
 local normal=proto.normal_resource_amount;local product=0
 if not proto.infinite_resource or not normal or normal<=0 or not mine.mining_time or mine.mining_time<=0
  then return {ok=false,reason="unsupported live oil yield geometry"} end
 for _,row in pairs(mine.products or {}) do
  if row.type=="fluid" and row.name=="crude-oil" then product=product+(row.amount or 0) end
 end
 amount=well.amount
 --[[ Ignore positive bonuses; negative speed/productivity effects cannot inflate the bound. ]]
 rate=e.prototype.mining_speed*math.max(0,1+math.min(0,e.speed_bonus))
  *math.max(0,1+math.min(0,e.productivity_bonus))*60/mine.mining_time*product*amount/normal
end
return {ok=true,world_id=d and d.world_id,unit_number=e.unit_number,nominal_capacity_per_minute=rate,resource_amount=amount}
''')
        capacity = survey.get("nominal_capacity_per_minute")
        if (not survey.get("ok") or survey.get("world_id") != observation["world_id"]
                or isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or not math.isfinite(capacity) or capacity <= 0):
            return self._decorate(_report("blocked", "raw fluid nominal capacity is unavailable", fluid=fluid,
                                         requested_rate_per_minute=rate, query_error=survey.get("reason")), plan)
        if capacity + 1e-9 < rate:
            return self._decorate(_report("blocked", "owned raw fluid source requires extraction capacity expansion", fluid=fluid,
                                         requested_rate_per_minute=rate, nominal_capacity_per_minute=capacity,
                                         throughput_verified=False, raw_source_capacity_verified=False), plan)
        return {**result, "evidence": {**result.get("evidence", {}), "requested_rate_per_minute": rate,
                                      "nominal_capacity_per_minute": capacity, "throughput_verified": False,
                                      "raw_source_capacity_verified": True}}

    def _source_evidence(self, observation: dict, plan: dict, product: str) -> dict:
        positions = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in plan["entities"]}
        available = 0.0
        for e in observation.get("entities", []):
            p = e["position"]
            if (e["name"], p["x"], p["y"]) in positions:
                available += float(e.get("fluids", {}).get(product, 0))
                available += float(e.get("output_inventory", {}).get(product, 0))
                available += float(e.get("belt_inventory", {}).get(product, 0))
        # Observe output inventories and transport lines directly: an assembler's
        # aggregate inventory can contain the requested item only as an ingredient.
        payload = json.dumps(json.dumps(plan["entities"], separators=(",", ":")))
        probe = self.game.query('''
local rows=helpers.json_to_table(''' + payload + ''');local item=''' + json.dumps(product) + '''
local available=0;local products_finished=0
for _,row in ipairs(rows) do
 local e=target(row.position,row.name)
 if e then
  local ok,fluids=pcall(function() return e.get_fluid_contents() end)
  if ok then available=available+(fluids[item] or 0) end
  local good,inv=pcall(function() return e.get_output_inventory() end)
  if good and inv then available=available+(contents(inv)[item] or 0) end
  if e.type=="transport-belt" then
   for line=1,2 do available=available+e.get_transport_line(line).get_item_count(item) end
  end
  local finished,n=pcall(function() return e.products_finished end)
  if finished and n then products_finished=products_finished+n end
 end
end
return {ok=true,available=available,products_finished=products_finished}
''')
        if isinstance(probe, dict) and probe.get("ok"):
            available = float(probe["available"])
        return {"product": product, "available": available}

    def _connect_pipe(self, observation: dict, source: dict, destination: dict, key: str, reserved: dict) -> dict:
        if source.get("kind") != "fluid" or destination.get("kind") != "fluid" or source.get("item") != destination.get("item"):
            return _report("blocked", "fluid route requires matching typed fluid ports")
        link = self.state["links"].get(key)
        if link is None:
            obstacles = reserved.get("entities", []) + self.factory._reserved() + [
                {"name": "reserved-port-approach", "position": {"x": x, "y": y}}
                for x, y in self.factory._port_clearances()]
            try:
                obstacles += self._machine_connection_obstacles(observation.get("entities", []) + obstacles)
            except ValueError as exc:
                return _report("blocked", str(exc))
            network = {}
            pairs = []
            route = self.builder.route(source["position"], destination["position"], "pipe", obstacles)
            if not route.get("ok"):
                network = self._network_taps(source, destination)
                if network.get("connected"):
                    return _report("succeeded", "source and destination already share the actual fluid segment")
                # A busy source outlet may already have pipes in every useful
                # direction. Branch from another VERIFIED member of that exact
                # segment instead of treating a same-named fluid elsewhere as it.
                pairs = [(a, b) for a in network.get("taps", [])[:24]
                         for b in (network.get("destination_taps") or [destination["position"]])[:24]]
                pairs.sort(key=lambda pair: math.dist(_point(pair[0]), _point(pair[1])))
                for tap, receiver in pairs[:32]:
                    trial = self.builder.route(tap, receiver, "pipe", obstacles)
                    if trial.get("ok"):
                        route = trial
                        break
            if route.get("ok"):
                entities = [{"name": "pipe", "position": p, "direction": 0} for p in route["path"]]
            else:
                escape = self._underground_escape(observation, source, destination, obstacles)
                if not escape.get("ok") and not escape.get("needs_recipe"):
                    for tap, receiver in pairs[:12]:
                        for facing in DIRECTIONS:
                            escape = self._underground_escape(observation, {**source, "position": tap, "facing": facing},
                                                              {**destination, "position": receiver}, obstacles)
                            if escape.get("ok"):
                                break
                        if escape.get("ok"):
                            break
                if not escape.get("ok"):
                    if escape.get("needs_recipe"):
                        return self.factory.request_recipe_unlock(observation, escape["needs_recipe"])
                    return _report("blocked", escape.get("reason", route.get("reason", "fluid pipe route unavailable")))
                entities = escape["entities"]
            link = {"ok": True, "entities": [{**e, "_fluid": source["item"]} for e in entities], "ports": []}
            registered = self.factory.register_plan("fluid-link:" + key, link, observation)
            if not registered.get("ok"):
                return _report("blocked", registered.get("reason", "fluid route reservation conflict"))
            self.state["links"][key] = link
            self._save()
        built = self.builder.ensure_plan(observation, link)
        if built.get("status") != "succeeded":
            return built
        network = self._network_taps(source, destination)
        if not network.get("connected"):
            return _report("blocked", "constructed pipe route does not join the actual fluid segments",
                           link=key, fluid=source["item"], flow_verified=False)
        return _report("succeeded", "fluid endpoints share the observed segment", link=key, flow_verified=False)

    def _machine_connection_obstacles(self, entities: list[dict]) -> list[dict]:
        """An unused machine port is an actual fluid junction, even without a pipe.

        Exclude every such tile except a route's explicitly selected endpoints
        (which the builder permits). This prevents a water path touching an
        engine's spare steam port or an unfilled chemical plant inlet. Matching
        existing pipe segments may still be selected as verified network taps.
        """
        positions = set()
        for entity in entities:
            if entity["name"] in {"pipe", "pipe-to-ground"}:
                continue
            boxes = _sequence(self.catalog.entities.get(entity["name"], {}).get("fluidbox_prototypes"))
            for box in boxes:
                if not any(c.get("connection_type", "normal") == "normal"
                           for c in _sequence(box.get("pipe_connections"))):
                    continue
                for connection in exterior_connections(box):
                    direction = entity.get("direction", 0)
                    if direction not in DIRECTIONS:
                        raise ValueError("fluid routing requires cardinal machine connection geometry")
                    dx, dy = _rotate(*_point(connection["position"]), direction)
                    x, y = _point(entity["position"])
                    positions.add((round(x + dx, 3), round(y + dy, 3)))
        return [{"name": "reserved-fluid-connection", "position": _position(x, y)}
                for x, y in sorted(positions)]

    def _network_taps(self, source: dict, destination: dict) -> dict:
        payload = json.dumps(json.dumps({"source": source, "destination": destination}, separators=(",", ":")))
        result = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local origin=target(args.source.position,"pipe")
if not origin then return {ok=false,reason="source pipe missing"} end
local id=origin.get_fluid_segment_id(1);local receiver=target(args.destination.position,"pipe")
if not id then return {ok=false,reason="source fluid segment missing"} end
local receiver_id=receiver and receiver.get_fluid_segment_id(1);local taps={};local destinations={}
for _,e in pairs(s.find_entities_filtered{force=f,type="pipe"}) do
 local segment=e.get_fluid_segment_id(1)
 if segment==id and (e.get_fluid_contents()[args.source.item] or 0)>0 then taps[#taps+1]=pos(e.position) end
 if receiver_id and segment==receiver_id then destinations[#destinations+1]=pos(e.position) end
end
local d=args.destination.position
table.sort(taps,function(a,b) return (a.x-d.x)^2+(a.y-d.y)^2<(b.x-d.x)^2+(b.y-d.y)^2 end)
local s=args.source.position
table.sort(destinations,function(a,b) return (a.x-s.x)^2+(a.y-s.y)^2<(b.x-s.x)^2+(b.y-s.y)^2 end)
return {ok=true,taps=taps,destination_taps=destinations,connected=receiver_id==id}
''')
        return result if isinstance(result, dict) and result.get("ok") else {"taps": []}

    def _underground_escape(self, observation: dict, source: dict, destination: dict, reserved: list[dict]) -> dict:
        """Cross an enclosing pipe with one real, prototype-sized underground pair.

        The ordinary route remains above ground. Only a port's outward corridor
        may tunnel; endpoints face the source/continuation and cannot connect to
        a crossed pipe at their closed backs. Existing tunnel mouths in that same
        corridor are rejected so a new pair cannot steal an older connection.
        """
        entity = self.catalog.entities.get("pipe-to-ground", {})
        if not observation.get("enabled_recipes", {}).get("pipe-to-ground"):
            return {"ok": False, "reason": "fluid outlet is enclosed and underground pipes are locked", "needs_recipe": "pipe-to-ground"}
        boxes = _sequence(entity.get("fluidbox_prototypes"))
        connections = _sequence(boxes[0].get("pipe_connections")) if boxes else []
        normal = next((p for p in connections if p.get("connection_type") == "normal"), {})
        underground = next((p for p in connections if p.get("connection_type") == "underground"), {})
        normal_direction, underground_direction = normal.get("direction"), underground.get("direction")
        maximum = underground.get("max_underground_distance", 0)
        if (normal_direction not in DIRECTIONS or underground_direction not in DIRECTIONS
                or (normal_direction + 8) % 16 != underground_direction or maximum < 2):
            return {"ok": False, "reason": "unsupported live underground pipe connection geometry"}
        for connection in (normal, underground):
            positions = _sequence(connection.get("positions"))
            point = positions[0] if positions else connection.get("position")
            if point is None or _point(point) != (0, 0):
                return {"ok": False, "reason": "underground pipe mouths must use centered prototype geometry"}
        for at_destination, port in ((False, source), (True, destination)):
            if port.get("facing") not in DIRECTIONS:
                continue
            direction = (port["facing"] + (8 if at_destination else 0)) % 16
            dx, dy = DIRECTIONS[direction]
            x, y = _point(port["position"])
            entry = _position(x + dx, y + dy)
            for distance in range(2, int(maximum) + 1):
                exit = _position(entry["x"] + dx * distance, entry["y"] + dy * distance)
                continuation = _position(exit["x"] + dx, exit["y"] + dy)
                pair = [{"name": "pipe-to-ground", "position": entry, "direction": (direction + 8 - normal_direction) % 16},
                        {"name": "pipe-to-ground", "position": exit, "direction": (direction - normal_direction) % 16}]
                extension = {"name": "pipe", "position": continuation, "direction": 0}
                if self.builder._occupied_by_plan(pair + [extension]) & self.builder._occupied_by_plan(reserved):
                    continue
                if not self.builder.can_place(pair + [extension]).get("ok"):
                    continue
                existing = [e for e in observation.get("entities", []) + reserved if e["name"] == "pipe-to-ground"]
                if any(abs((e["position"]["x"] - entry["x"]) * dy - (e["position"]["y"] - entry["y"]) * dx) < .1
                       and -.1 <= (e["position"]["x"] - entry["x"]) * dx + (e["position"]["y"] - entry["y"]) * dy <= distance + .1
                       for e in existing):
                    continue
                start, end = (source["position"], continuation) if at_destination else (continuation, destination["position"])
                route = self.builder.route(start, end, "pipe", reserved + pair)
                if route.get("ok"):
                    pipes = [{"name": "pipe", "position": p, "direction": 0} for p in route["path"]]
                    return {"ok": True, "entities": [{"name": "pipe", "position": port["position"], "direction": 0}] + pair + pipes}
        return {"ok": False, "reason": "no safe above-ground or underground outlet route"}

    def _utility_plan(self, machine: str, position: dict, direction: int, fluid: str) -> dict:
        entity = self.catalog.entities[machine]
        # Offshore pumps have a two-tile selection envelope reaching into water;
        # the outlet remains one land-side pipe tile from the entity position.
        expected = (3, 3) if machine == "pumpjack" else (2, 2)
        if _bounds_size(entity) != expected:
            raise ValueError("unsupported live raw source footprint")
        boxes = _sequence(entity.get("fluidbox_prototypes"))
        connection = exterior_connections(boxes[0])[0]
        dx, dy = _rotate(*_point(connection["position"]), direction)
        pipe_position = _position(position["x"] + dx, position["y"] + dy)
        entities = [{"name": machine, "position": position, "direction": direction},
                    {"name": "pipe", "position": pipe_position, "direction": 0}]
        ports = [{"kind": "fluid", "item": fluid, "direction": "output", "position": pipe_position,
                  "facing": (connection["facing"] + direction) % 16}]
        if machine == "pumpjack":
            for x, y in ((-2, -2), (2, -2), (-2, 2), (2, 2)):
                entities.append({"name": "small-electric-pole", "position": _position(position["x"] + x, position["y"] + y), "direction": 0})
            ports.append({"kind": "power", "item": "electricity", "direction": "input",
                          "position": _position(position["x"] - 2, position["y"] - 2)})
        return {"ok": True, "entities": entities, "ports": ports,
                "required_items": dict(Counter(e["name"] for e in entities))}

    def _ensure_raw_source(self, observation: dict, fluid: str, amount: float) -> dict:
        key = "raw:" + fluid
        required_machine = "pumpjack" if fluid == "crude-oil" else "offshore-pump"
        if (not observation.get("enabled_recipes", {}).get(required_machine)
                and not any(e["name"] == required_machine for e in observation.get("entities", []))):
            return self.factory.request_recipe_unlock(observation, required_machine)
        plan = self.state["sources"].get(key)
        if plan is None:
            if fluid == "crude-oil":
                oil = observation.get("resources", {}).get("crude-oil")
                if not oil:
                    return _report("blocked", "no discovered crude oil well")
                candidates = [(oil["position"], d) for d in DIRECTIONS]
                machine = "pumpjack"
            else:
                pumps = [e for e in observation.get("entities", []) if e["name"] == "offshore-pump"]
                candidates = [(e["position"], e.get("direction", 0)) for e in pumps]
                if not candidates:
                    candidates = [(s["position"], s["direction"]) for s in self.builder.water_sites()]
                machine = "offshore-pump"
            for position, direction in candidates:
                try:
                    candidate = self._utility_plan(machine, position, direction, fluid)
                except (KeyError, ValueError) as exc:
                    return _report("blocked", str(exc))
                if self.builder.can_place(candidate["entities"]).get("ok"):
                    plan = candidate
                    break
            if plan is None:
                return _report("blocked", "source site cannot accommodate fluid and power connections", fluid=fluid)
            registered = self.factory.register_plan("fluid:" + key, plan, observation)
            if not registered.get("ok"):
                return _report("blocked", registered.get("reason", "raw fluid site reservation conflict"))
            self.state["sources"][key] = plan
            self._save()
        built = self.builder.ensure_plan(observation, plan)
        if built.get("status") != "succeeded":
            return self._decorate(built, plan)
        if fluid == "crude-oil":
            powered = self.factory.ensure_power_connection(observation, "fluid:" + key, plan)
            if powered.get("status") != "succeeded":
                return self._decorate(powered, plan)
        evidence = self._source_evidence(observation, plan, fluid)
        status = "succeeded" if evidence["available"] > 0 else "waiting"
        return self._decorate(_report(status, "raw fluid source observed" if status == "succeeded" else "waiting for source fluid",
                                      **evidence, flow_verified=status == "succeeded"), plan)

    def _ensure_buffer(self, observation: dict, fluid: str, producer: dict) -> dict:
        # Buffer geometry and coproduct maintenance are deliberately separate from
        # the recipe template, allowing different producers to share the same tank.
        buffer = self.state["buffers"].get(fluid)
        if buffer is None:
            if not observation.get("enabled_recipes", {}).get("storage-tank"):
                return self.factory.request_recipe_unlock(observation, "storage-tank")
            source = next(p for p in producer["ports"] if p["item"] == fluid and p["direction"] == "output")
            try:
                prototype = self.catalog.entities["storage-tank"]
                if _bounds_size(prototype) != (3, 3):
                    raise ValueError("unsupported live storage tank footprint")
                connections = exterior_connections(_sequence(prototype["fluidbox_prototypes"])[0])
                north = next(c for c in connections if c["facing"] == 0)
                south = next(c for c in connections if c["facing"] == 8)
            except (KeyError, TypeError, ValueError, IndexError, StopIteration):
                return _report("blocked", "live storage tank requires supported north/south exterior connections")
            entities = [{"name": "storage-tank", "position": {"x": .5, "y": .5}, "direction": 0}]
            ports = []
            for connection, role in ((south, "input"), (north, "output")):
                p = _position(.5 + connection["position"]["x"], .5 + connection["position"]["y"])
                entities.append({"name": "pipe", "position": p, "direction": 0})
                ports.append({"kind": "fluid", "item": fluid, "direction": role, "position": p,
                              "facing": connection["facing"] if role == "output" else (connection["facing"] + 8) % 16})
            origin = {"ok": True, "entities": entities, "ports": ports,
                      "required_items": {"storage-tank": 1, "pipe": 2},
                      "bounds": {"min_x": -1, "max_x": 2, "min_y": -2, "max_y": 3, "width": 3, "height": 5}}
            buffer = self.factory.reserve_site(origin, "fluid:buffer:" + fluid, observation, reference=source["position"])
            if not buffer.get("ok"):
                return _report("blocked", buffer.get("reason", "no coproduct tank site"))
            self.state["buffers"][fluid] = buffer
            self._save()
        built = self.builder.ensure_plan(observation, buffer)
        if built.get("status") != "succeeded":
            return built
        destination = next(p for p in buffer["ports"] if p["direction"] == "input")
        for source in (p for p in producer["ports"] if p["item"] == fluid and p["direction"] == "output"):
            key = f"fluid:buffer:{producer.get('capacity_key', producer.get('recipe'))}:{fluid}"
            if source.get("machine_index", 0):
                key += ":" + str(source["machine_index"])
            result = self._connect_pipe(observation, source, destination, key,
                                        {"entities": producer["entities"] + buffer["entities"]})
            if result.get("status") != "succeeded":
                return result
        return _report("succeeded", "all producer outlets connect to observed coproduct storage", fluid=fluid)

    def maintain_coproducts(self, observation: dict) -> dict | None:
        """Drain surplus light/heavy oil through actual cracking when tanks fill.
        Invoke before ordinary production; no dumping or destructive fluid reset.
        """
        self._sync(observation)
        amounts = {name: self._source_evidence(observation, plan, name)["available"]
                   for name, plan in self.state["buffers"].items()}
        boxes = _sequence(self.catalog.entities.get("storage-tank", {}).get("fluidbox_prototypes"))
        capacity = float(boxes[0].get("volume", 0)) if boxes else 0
        if self.state["buffers"] and capacity <= 0:
            return _report("blocked", "live coproduct storage capacity is unavailable")
        pending = self.state.setdefault("coproduct_jobs", {})
        for fluid, recipe, output in (("light-oil", "light-oil-cracking", "petroleum-gas"),
                                      ("heavy-oil", "heavy-oil-cracking", "light-oil")):
            if recipe not in pending and (amounts.get(fluid, 0) < .8 * capacity or amounts.get(output, 0) >= .8 * capacity):
                continue
            if not observation.get("enabled_recipes", {}).get(recipe):
                return self.factory.request_recipe_unlock(observation, recipe)
            pending[recipe] = True
            self._save()
            result = self._ensure_recipe(observation, recipe, output, 100, None, ())
            if result.get("status") != "succeeded":
                return result
            plan = self.state["sources"][recipe]
            result = self._ensure_buffer(observation, output, plan)
            if result.get("status") != "succeeded":
                return result
            pending.pop(recipe, None)
            self._save()
        return None
