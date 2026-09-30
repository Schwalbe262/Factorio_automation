"""World-scoped execution of reviewed array plans through the paid builder."""
from copy import deepcopy
from collections import deque
import json
import math

from .deterministic_arrays import optimize_array, plan_digest, translate_array, _footprint, PRODUCERS
from .deterministic_builder import plan_observed, _plan_lookup
from .deterministic_layout import _survey, bounds, reserved_aisles
from .deterministic_raw_ore import ensure_raw_ore
from .factory_templates import DIRECTIONS


def report(status, reason, **evidence):
    return {"status": status, "reason": reason, "evidence": evidence}


def construction_order(plan):
    """Finish receivers before activating paid producers and their input arms."""
    def rank(entity):
        if entity["name"] in PRODUCERS | {"lab"}:
            return 1, 0
        if entity["name"] in {"inserter", "fast-inserter"}:
            return 2, {"output": 0, "tap": 1, "input": 2}.get(entity.get("_role"), 2)
        return 0, 0
    return {**plan, "entities": sorted(plan["entities"], key=rank)}


def foundation_stages(plan, catalog, underground_distance, *, pole_wire=7.5, pole_supply=2.5,
                      include_intermediates=False):
    """Activate paid producers with complete supply paths before bulk exports."""
    from .deterministic_input_links import _geometry, _path
    view = {**plan, "entities": [e for e in plan["entities"] if e["name"] not in {"inserter", "fast-inserter"}],
            "underground_pairs": []}
    mouths = [e for e in plan["entities"] if e["name"] == "underground-belt"]
    for inlet in mouths:
        if inlet.get("belt_to_ground_type") != "input":
            continue
        dx, dy = DIRECTIONS[inlet["direction"]]
        x, y = inlet["position"]["x"], inlet["position"]["y"]
        candidates = []
        for outlet in mouths:
            vx, vy = outlet["position"]["x"] - x, outlet["position"]["y"] - y
            distance = vx * dx + vy * dy
            if (outlet.get("belt_to_ground_type") == "output" and outlet["direction"] == inlet["direction"]
                    and outlet.get("_item") == inlet.get("_item") and vx * dy == vy * dx
                    and 0 < distance <= underground_distance):
                candidates.append((distance, outlet))
        if not candidates:
            raise ValueError("foundation underground dependency has no bounded paired outlet")
        outlet = min(candidates, key=lambda row: row[0])[1]
        view["underground_pairs"].append({"input": inlet, "output": outlet, "max_distance": underground_distance})
    belts, edges, _ = _geometry(view)
    for arm in plan["entities"]:
        if arm.get("_role") != "tap":
            continue
        dx, dy = DIRECTIONS[arm["direction"]]
        pos = arm["position"]
        pickup, drop = (pos["x"] + dx, pos["y"] + dy), (pos["x"] - dx, pos["y"] - dy)
        if (pickup not in belts or drop not in belts
                or any(belts[p].get("_item") != arm["_item"] for p in (pickup, drop))):
            raise ValueError("foundation tap is not on its declared item path")
        edges[pickup].append((drop, arm))
    inputs = {p["item"]: p for p in plan["ports"] if p["direction"] == "input"}
    items = {n["recipe"]: n["item"] for n in plan["demand"]["nodes"]}
    machines = [e for e in plan["entities"] if e["name"] in PRODUCERS]
    process_order = {}
    if include_intermediates:
        pending = list(plan["demand"]["nodes"])
        available = set(inputs)
        while pending:
            ready = [n for n in pending if set(n["inputs"]).issubset(available)]
            if not ready:
                raise ValueError("producer construction order has no dependency-ready process")
            node = min(ready, key=lambda n: (n["item"] != "transport-belt", n["item"] != "iron-plate",
                                            n["item"] not in {"copper-plate", "iron-gear-wheel"}, n["item"]))
            process_order[node["item"]] = len(process_order)
            available.add(node["item"])
            pending.remove(node)
    machines.sort(key=lambda e: (process_order.get(items[e.get("recipe", e.get("_array_recipe"))],
                                                  items[e.get("recipe", e.get("_array_recipe"))] != "iron-plate"),
                                 e["position"]["y"], e["position"]["x"]))
    poles = [e for e in plan["entities"] if e["name"] == "small-electric-pole"]
    stages = []
    for machine in machines:
        left, top, right, bottom = _footprint(machine, catalog)
        rows, sources = [], set()
        for arm in plan["entities"]:
            if arm.get("_role") not in {"input", "output"}:
                continue
            dx, dy = DIRECTIONS[arm["direction"]]
            pos = arm["position"]
            pickup = pos["x"] + dx, pos["y"] + dy
            drop = pos["x"] - dx, pos["y"] - dy
            endpoint = drop if arm["_role"] == "input" else pickup
            if not (left <= endpoint[0] < right and top <= endpoint[1] < bottom):
                continue
            item = arm["_item"]
            if arm["_role"] == "input":
                if item in inputs:
                    port = inputs[item]["position"]
                    path = _path(belts, edges, (port["x"], port["y"]), pickup)
                    sources.add(item)
                elif include_intermediates:
                    paths = []
                    for output in plan["entities"]:
                        if output.get("_role") != "output" or output.get("_item") != item:
                            continue
                        ox, oy = DIRECTIONS[output["direction"]]
                        p = output["position"]
                        start = p["x"] - ox, p["y"] - oy
                        candidate = _path(belts, edges, start, pickup)
                        if candidate is not None:
                            paths.append((len(candidate), start, candidate))
                    path = min(paths, key=lambda p: (p[0], p[1]))[2] if paths else None
                else:
                    raise ValueError("foundation input is not an external material: " + item)
            else:
                # A real receiving belt safely backs up while the full export
                # route is still being paid for. Bootstrap can collect its output.
                path = [belts[drop]] if drop in belts and belts[drop].get("_item") == item else None
            if path is None:
                raise ValueError("foundation producer dependency path is disconnected: " + item)
            rows.extend(path)
            rows.append(arm)
        rows.append(machine)
        selected = {0}
        if not poles:
            raise ValueError("foundation producer has no connected power plan")
        for entity in rows:
            if entity["name"] not in {"inserter", "fast-inserter"} | PRODUCERS:
                continue
            l, t, r, b = _footprint(entity, catalog)
            covers = {i for i, p in enumerate(poles)
                      if min(r, p["position"]["x"] + pole_supply) > max(l, p["position"]["x"] - pole_supply)
                      and min(b, p["position"]["y"] + pole_supply) > max(t, p["position"]["y"] - pole_supply)}
            queue, previous = deque(sorted(selected)), dict.fromkeys(selected)
            found = None
            while queue:
                current = queue.popleft()
                if current in covers:
                    found = current
                    break
                origin = poles[current]["position"]
                for i, p in enumerate(poles):
                    if i not in previous and math.hypot(origin["x"] - p["position"]["x"], origin["y"] - p["position"]["y"]) <= pole_wire:
                        previous[i] = current
                        queue.append(i)
            if found is None:
                raise ValueError("foundation arm has no connected supply pole path")
            while found is not None:
                selected.add(found)
                found = previous[found]
        rows.extend(poles[i] for i in sorted(selected))
        unique = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in rows}
        stages.append({"sources": sorted(sources), "item": items[machine.get("recipe", machine.get("_array_recipe"))],
                       "plan": construction_order({"ok": True, "entities": list(unique.values())})})
    return stages


def phase_lab_count(catalog, packs, geometry, rate):
    """Size once per pack phase, independent of the currently selected research."""
    durations = [float(row["unit_energy"]) for row in catalog.technologies.values()
                 if row.get("unit_energy") and row.get("ingredients")
                 and {r["name"] for r in row["ingredients"]}.issubset(packs)]
    duration = max(durations, default=600)
    lab_rate = (3600 * geometry["lab_speed"] * (1 + geometry["lab_bonus"])
                * geometry["lab_drain"] / 100 / duration)
    return max(1, math.ceil(rate / lab_rate - 1e-9))


def owned_production(plan, observation):
    """Counters only from declared machines, with identity for rebuild detection."""
    find = _plan_lookup(observation)
    result = {}
    for item in plan["lab_inputs"]:
        node = next(n for n in plan["demand"]["nodes"] if n["item"] == item)
        machines = [e for e in plan["entities"] if e["name"] in PRODUCERS
                    and e.get("recipe", e.get("_array_recipe")) == node["recipe"]]
        rows = [find(e) for e in machines]
        if any(r is None or type(r.get("products_finished")) not in (int, float)
               or not r.get("unit_number") for r in rows):
            return None
        result[item] = {"produced": sum(r["products_finished"] for r in rows) * node["output_amount"],
                        "units": sorted(r["unit_number"] for r in rows),
                        "consumed": observation.get("production", {}).get(item, {}).get("consumed", 0)}
    return result


class ArrayProduction:
    def __init__(self, factory):
        self.factory = factory
        self.geometry = None
        self._building = False
        self._local_key = None
        self._local_plan = None

    def _local_blueprint(self, obs, targets, geometry, packs, count, phase):
        """Cache only pure compilation; source and placement evidence stays fresh."""
        key = plan_digest({"world": obs.get("world_id"), "surface": obs.get("surface"),
                           "catalog": self.factory.catalog.fingerprint,
                           "enabled": obs.get("enabled_recipes", {}), "targets": targets,
                           "geometry": geometry, "packs": packs, "labs": count, "phase": phase})
        if key != self._local_key:
            local = optimize_array(self.factory.catalog, obs, targets, geometry,
                    boundary={"iron-ore", "copper-ore", "coal", "stone", "wood"}, labs=packs, lab_count=count)
            local["phase"] = phase
            self._local_plan, self._local_key = local, key
        return deepcopy(self._local_plan)

    def _geometry(self, obs):
        if self.geometry is None:
            value = self.factory.game.query('''
local pole=prototypes.entity["small-electric-pole"]
local basic=prototypes.entity.inserter;local fast=prototypes.entity["fast-inserter"]
local function ordinary(p)
 local pickup=p.inserter_pickup_position;local drop=p.inserter_drop_position
 return math.floor((pickup.x or pickup[1])+.5)==0 and math.floor((pickup.y or pickup[2])+.5)==-1
  and math.floor((drop.x or drop[1])+.5)==0 and math.floor((drop.y or drop[2])+.5)==1
end
return {ok=ordinary(basic) and ordinary(fast),belt_speed=prototypes.entity["transport-belt"].belt_speed,
 fast_belt_speed=prototypes.entity["fast-transport-belt"].belt_speed,
 underground_distance=prototypes.entity["underground-belt"].max_underground_distance,
 pole_supply=pole.get_supply_area_distance("normal"),pole_wire=pole.get_max_wire_distance("normal"),
 lab_speed=prototypes.entity.lab.get_researching_speed(),lab_bonus=f.laboratory_speed_modifier,
 lab_drain=prototypes.entity.lab.science_pack_drain_rate_percent}
''')
            required = ("belt_speed", "underground_distance", "pole_supply", "pole_wire", "lab_speed", "lab_drain")
            if not value.get("ok") or any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k])
                                          or value[k] <= 0 for k in required):
                raise ValueError("array transport/lab prototype geometry is unavailable or unsupported")
            self.geometry = value
        return {**self.geometry, "fast_belt_enabled": bool(obs.get("enabled_recipes", {}).get("fast-transport-belt"))}

    def _targets(self, obs):
        foundation_ready = any(p.get("phase") == "construction-foundation" and p.get("completed")
                               for p in self.factory.state.get("array_plans", {}).values())
        if not foundation_ready:
            demand = self.factory.state.get("array_demands", {})
            # The existing startup contract requests 60 iron/min for construction.
            # Build this small automatic smelting stage before financing science.
            return {"iron-plate": max(60, demand.get("iron-plate", 0)),
                    "copper-plate": max(30, demand.get("copper-plate", 0))}, [], "construction-foundation"
        packs = [name for name in ("automation-science-pack", "logistic-science-pack", "chemical-science-pack")
                 if obs.get("enabled_recipes", {}).get(name)]
        targets = {name: self.factory.graph.science_rate_per_minute for name in packs}
        if obs.get("enabled_recipes", {}).get("transport-belt"):
            targets["transport-belt"] = 30
        if obs.get("enabled_recipes", {}).get("firearm-magazine"):
            targets["firearm-magazine"] = 10
        for item, rate in self.factory.state.get("array_demands", {}).items():
            targets[item] = max(targets.get(item, 0), rate)
        return targets, packs, "science"

    def ensure_product(self, obs, item, rate=None):
        wanted = 10.0 if rate is None else float(rate)
        if not math.isfinite(wanted) or wanted <= 0:
            return report("blocked", "array product rate must be finite and positive", item=item)
        if not obs.get("enabled_recipes", {}).get(self.factory.catalog.recipe_for_product(item)["name"]):
            return self.factory.request_recipe_unlock(obs, self.factory.catalog.recipe_for_product(item)["name"])
        for plan in reversed(list(self.factory.state.get("array_plans", {}).values())):
            port = next((p for p in plan["ports"] if p["direction"] == "output" and p["item"] == item), None)
            if port and plan_observed(obs, plan) and port["rate_per_minute"] + 1e-9 >= wanted:
                return report("succeeded", "array product has observed infrastructure; sustained flow is separate",
                              ports=[deepcopy(port)], flow_verified=bool(plan.get("flow_proof")), input_handcarry=False,
                              nominal_capacity_per_minute=port["rate_per_minute"])
        demand = self.factory.state.setdefault("array_demands", {})
        if demand.get(item, 0) < wanted:
            demand[item] = wanted
            self.factory._save()
        if self._building:
            return report("blocked", "array construction ingredient producer is not yet ready", item=item)
        return self.next_action(obs)

    def _sources(self, obs, demand, owner):
        sources = {}
        for item, rate in demand.items():
            if item in {"iron-ore", "copper-ore", "coal", "stone"}:
                result = ensure_raw_ore(self.factory, obs, item, rate,
                                       source_key=f"source:{item}:arrays:{owner}")
            else:
                result = self.factory.fluids.ensure_source(obs, item, rate_per_minute=rate)
            if result.get("status") != "succeeded" or result.get("type"):
                return result, None
            port = next((p for p in result["evidence"]["ports"] if p.get("item") == item), None)
            if port is None:
                return report("blocked", "array external producer has no typed output", item=item), None
            sources[item] = port
        return None, sources

    def _clearance(self, entities):
        """Admit ordinary natural mining only after checking the whole district."""
        from .deterministic_navigation import BUILD_FOOTPRINT_LUA
        payload = json.dumps(json.dumps(entities, separators=(",", ":")))
        result = self.factory.game.query(BUILD_FOOTPRINT_LUA + '''
local rows=helpers.json_to_table(''' + payload + ''');local first=nil;local count=0;local seen={}
for _,x in ipairs(rows) do
 if not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
  type=x.belt_to_ground_type} then
  if not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
   type=x.belt_to_ground_type,build_check_type=defines.build_check_type.manual_ghost,forced=true}
   then return {ok=false,reason="district terrain is not buildable"} end
  local left,top,right,bottom=build_box(x);local found=false
  for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
   if e.type~="resource" and not e.prototype.collision_mask.colliding_with_tiles_only then
    local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
    if e.force.name~="neutral" or not e.minable or not (e.type=="tree" or rock)
     then return {ok=false,reason="district contains a protected obstacle"} end
    found=true
    local identity=e.unit_number or (e.name..":"..e.position.x..":"..e.position.y)
    if not seen[identity] then
     seen[identity]=true;count=count+1
     first=first or {name=e.name,position=pos(e.position)}
    end
   end
  end
  if not found then return {ok=false,reason="district collision is unexplained"} end
 end
end
return {ok=count<=256,first=first,count=count}
''')
        if result.get("ok") and result.get("first"):
            return {"type": "mine", **result["first"], "count": 1,
                    "reason": "normally mine a verified natural obstacle in the candidate array district"}
        return None

    def _reserve(self, obs, local, owner, sources):
        factory = self.factory
        reference = factory.state.get("production_layout", {}).get("anchor")
        if reference is None:
            reference = {axis: sum(p["position"][axis] for p in sources.values()) / max(1, len(sources))
                         for axis in ("x", "y")}
        occupied = factory.builder._occupied_by_plan(factory._reserved()) | reserved_aisles(factory)
        corners = [_footprint(e, factory.catalog) for e in local["entities"]]
        left, top = min(r[0] for r in corners), min(r[1] for r in corners)
        right, bottom = max(r[2] for r in corners), max(r[3] for r in corners)
        offsets = sorted(((x, y) for x in range(-192, 193, 16) for y in range(-192, 193, 16)),
                         key=lambda p: (abs(p[0]) + abs(p[1]), p))
        candidates = []
        for dx, dy in offsets:
            anchor = {"x": math.floor(reference["x"]) + dx + .5,
                      "y": math.floor(reference["y"]) + dy + .5}
            for rotation in DIRECTIONS:
                points = []
                for x, y in ((left - .5, top - .5), (right - .5, bottom - .5)):
                    for _ in range(rotation // 4):
                        x, y = -y, x
                    points.append((anchor["x"] + x, anchor["y"] + y))
                area = [[min(p[i] for p in points) - 4 for i in (0, 1)],
                        [max(p[i] for p in points) + 4 for i in (0, 1)]]
                candidates.append({"anchor": anchor, "rotation": rotation, "area": area})
        attempts = 0
        for start in range(0, len(candidates), 24):
            batch = candidates[start:start + 24]
            survey = _survey(factory, batch)
            if not survey.get("ok") or not isinstance(survey.get("clear"), list):
                return report("blocked", "array district terrain/resource survey failed", query=survey.get("reason"))
            for index in survey["clear"]:
                candidate = batch[index - 1]
                plan = translate_array(local, candidate["anchor"], candidate["rotation"])
                if factory.builder._occupied_by_plan(plan["entities"]) & occupied:
                    continue
                if not factory.builder.can_place(plan["entities"]).get("ok"):
                    clearing = self._clearance(plan["entities"])
                    if clearing is not None:
                        return clearing
                    continue
                links, reserved = {}, factory._reserved() + plan["entities"]
                for port in plan["ports"]:
                    if port["direction"] != "input":
                        continue
                    source = sources[port["item"]]
                    route = factory._material_route(source["position"], port["position"], reserved,
                                start_direction=source.get("facing"), end_direction=port["facing"], allow_underground=True)
                    if not route.get("ok"):
                        clearing = factory.builder.clear_route_obstacle(
                            source["position"], port["position"], reserved,
                            start_direction=source.get("facing"), end_direction=port["facing"])
                        if clearing.get("ok") and clearing.get("action"):
                            return clearing["action"]
                        break
                    entities = [{"name": "transport-belt", **segment} for segment in route["segments"]]
                    links[port["item"]] = {"ok": True, "entities": entities,
                                            "source_port": deepcopy(source), "consumer_port": deepcopy(port)}
                    reserved += entities
                else:
                    poles = [e for e in plan["entities"] if e["name"] == "small-electric-pole"]
                    grid = factory._power_grid(obs, poles[:1])
                    if not grid.get("ok") or not grid.get("live"):
                        return report("blocked", "array district has no observed generator network")
                    power = None
                    for source in sorted(grid["live"], key=lambda p: math.hypot(
                            p["x"] - poles[0]["position"]["x"], p["y"] - poles[0]["position"]["y"]))[:4]:
                        route = factory._power_route(source, poles[0]["position"])
                        if route.get("ok"):
                            trial = [{"name": "small-electric-pole", "position": p, "direction": 0} for p in route["path"]]
                            own = factory.builder._occupied_by_plan(plan["entities"] + [e for l in links.values() for e in l["entities"]])
                            own.discard((poles[0]["position"]["x"], poles[0]["position"]["y"]))
                            if not factory.builder._occupied_by_plan(trial) & own and factory.builder.can_place(trial).get("ok"):
                                power = {"ok": True, "entities": trial}
                                break
                    if power is None:
                        continue
                    plan.update(world_id=obs["world_id"], surface=obs["surface"], catalog_fingerprint=factory.catalog.fingerprint,
                                source_links=links, power_plan=power, owner=owner, completed=False,
                                production_area=bounds(factory.builder._occupied_by_plan(plan["entities"]), 2))
                    key = "arrays:" + owner
                    # Publish only after all external material routes and power can be built.
                    factory.state.setdefault("array_plans", {})[owner] = plan
                    factory.state["blocks"][key] = plan
                    for item, link in links.items():
                        factory.state["links"][key + ":" + item] = link
                    factory.state["power_links"][key] = power
                    factory.state["array_active"] = owner
                    if "production_layout" not in factory.state:
                        factory.state["production_layout"] = {"anchor": candidate["anchor"], "resource_margin": 4, "block_aisle": 2}
                    burns = factory.state.setdefault("automated_burners", [])
                    for entity in plan["entities"]:
                        if entity["name"] in {"stone-furnace", "steel-furnace"}:
                            identity = factory._entity_key(entity)
                            if identity not in burns:
                                burns.append(identity)
                    factory._save()
                    return report("waiting", "complete array district and external routes reserved; reobserve", owner=owner)
                attempts += 1
                if attempts >= 3:
                    return report("blocked", "three clear array district candidates could not connect supply/power", owner=owner)
        return report("blocked", "no buildable array district inside the bounded 192-tile search", owner=owner)

    def next_action(self, obs):
        factory = self.factory
        if not obs.get("enabled_recipes", {}).get("underground-belt"):
            bridge = factory.bootstrap_electric_mining(obs, technology_name="logistics")
            if bridge is not None:
                return bridge
        geometry = self._geometry(obs)
        targets, packs, phase = self._targets(obs)
        if not packs and phase == "science":
            return report("blocked", "array production requires unlocked automatic science")
        research = factory.graph.next_research(obs)
        for capability in ("logistics", "logistics-2"):
            if not obs.get("technologies", {}).get(capability) and capability in factory.catalog.technologies:
                rows = factory.catalog.technology_order(capability, include_researched=True)
                name = next((n for n in rows if not obs.get("technologies", {}).get(n)
                             and all(obs.get("technologies", {}).get(p) for p in factory.catalog.technologies[n]["prerequisites"])), None)
                if name and set(r["name"] for r in factory.catalog.technologies[name]["ingredients"]).issubset(packs):
                    research = {"technology": name}
                    break
        count = phase_lab_count(factory.catalog, packs, geometry, factory.graph.science_rate_per_minute) if packs else 0
        owner = plan_digest({"targets": targets, "packs": packs, "labs": count, "phase": phase})[:12]
        active = factory.state.get("array_plans", {}).get(factory.state.get("array_active"))
        if active and not active.get("completed"):
            owner = active["owner"]
        plan = factory.state.get("array_plans", {}).get(owner)
        if plan is None:
            try:
                local = self._local_blueprint(obs, targets, geometry, packs, count, phase)
            except ValueError as error:
                return report("blocked", str(error), phase_targets=targets)
            action, sources = self._sources(obs, local["demand"]["external_rates"], owner)
            if action:
                return action
            return self._reserve(obs, local, owner, sources)
        if (plan["world_id"] != obs["world_id"] or plan["surface"] != obs["surface"]
                or plan["catalog_fingerprint"] != factory.catalog.fingerprint):
            return report("blocked", "array checkpoint belongs to another world, surface or catalog")
        action, sources = self._sources(obs, plan["demand"]["external_rates"], owner)
        if action:
            return action
        self._building = True
        try:
            stages = foundation_stages(plan, factory.catalog, geometry["underground_distance"],
                                      pole_wire=geometry["pole_wire"], pole_supply=geometry["pole_supply"],
                                      include_intermediates=plan.get("phase") != "construction-foundation")
            for link in ([] if stages else plan["source_links"].values()):
                result = factory.builder.ensure_plan(obs, link)
                if result.get("status") != "succeeded" or result.get("type"):
                    return result
            result = factory.builder.ensure_plan(obs, plan["power_plan"])
            if result.get("status") != "succeeded" or result.get("type"):
                return result
            for stage in stages:
                for item in stage["sources"]:
                    result = factory.builder.ensure_plan(obs, plan["source_links"][item])
                    if result.get("status") != "succeeded" or result.get("type"):
                        return result
                result = factory.builder.ensure_plan(obs, stage["plan"])
                if result.get("status") != "succeeded" or result.get("type"):
                    return result
            result = factory.builder.ensure_plan(obs, construction_order(plan))
            if result.get("status") != "succeeded" or result.get("type"):
                return result
        finally:
            self._building = False
        if not plan.get("completed"):
            plan["completed"] = True
            factory._save()
        if plan.get("phase") == "construction-foundation":
            return report("waiting", "construction smelting capacity observed; advance to science arrays",
                          owner=owner, nominal_rates=plan["demand"]["targets"], flow_verified=False)
        production = owned_production(plan, obs)
        tick = obs["tick"]
        sample = plan.get("flow_sample")
        reset = (not sample or tick < sample["tick"] or production is None
                 or any(production[item]["units"] != sample["production"].get(item, {}).get("units")
                        or production[item]["produced"] < sample["production"][item]["produced"]
                        for item in production))
        if reset:
            plan.pop("flow_proof", None)
            plan.pop("flow_sample", None)
            if production is not None:
                plan["flow_sample"] = {"tick": tick, "production": production}
            factory._save()
        elif tick - sample["tick"] >= 60 * 60 * 5:
            minutes = (tick - sample["tick"]) / 3600
            rates = {item: (production[item].get("produced", 0) - sample["production"][item].get("produced", 0)) / minutes
                     for item in production}
            consumed = {item: production[item].get("consumed", 0) - sample["production"][item].get("consumed", 0)
                        for item in production}
            if all(rate >= factory.graph.science_rate_per_minute - 1e-9 for rate in rates.values()) and all(n > 0 for n in consumed.values()):
                plan["flow_proof"] = {"from_tick": sample["tick"], "to_tick": tick,
                                      "rates_per_minute": rates, "force_consumed": consumed,
                                      "production_scope": "owned-array-machines"}
                factory._save()
            else:
                plan.pop("flow_proof", None)
                plan["flow_sample"] = {"tick": tick, "production": production}
                factory._save()
        if research and obs.get("research") != research["technology"]:
            return {"type": "research", "technology": research["technology"], "reason": "advance catalog research using array-fed labs"}
        if research is None:
            return report("succeeded", "array science infrastructure and required research are complete")
        return report("waiting", "array-fed automatic science and research running", waiting_for_progress=True,
                      owner=owner, science_flow=plan.get("flow_proof"), nominal_capacity_verified=True, flow_verified=bool(plan.get("flow_proof")))
