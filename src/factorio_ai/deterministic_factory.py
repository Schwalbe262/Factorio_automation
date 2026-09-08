"""Persistent, material-backed production ports and automatic science feeding."""
from __future__ import annotations

from copy import deepcopy
import heapq
import json
import math
from pathlib import Path
from typing import Any

from .deterministic_production import ProductionGraph
from .deterministic_mining_upgrade import ensure_source_upgrade
from .deterministic_state import _atomic_json
from .factory_templates import build_template, DIRECTIONS


def _report(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _ready(value: dict[str, Any]) -> bool:
    return value.get("status") == "succeeded" and "type" not in value


def _distance(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _plan(entities: list[dict], ports: list[dict] | None = None, **extra: Any) -> dict:
    return {"ok": True, "entities": entities, "ports": ports or [], **extra}


class DeterministicFactory:
    def __init__(self, game: Any, bootstrap: Any, builder: Any, catalog: Any):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.graph = ProductionGraph(catalog)
        self.fluids: Any = None
        self.priority_research: list[str] = []
        self.path = Path(game.cfg.runtime_dir) / "factory-production.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and self.state.get("schema_version") != 1:
            raise ValueError("unsupported factory production checkpoint")
        self._fingerprint = catalog.fingerprint
        self._power_observation: dict | None = None
        self._power_context: tuple | None = None
        self._power_grids: dict[tuple, dict] = {}
        self._product_observation: dict | None = None
        self._product_context: tuple | None = None
        self._product_results: dict[tuple, dict] = {}
        self._product_revision = 0

    def _sync(self, observation: dict) -> None:
        world = observation.get("world_id")
        if not world:
            raise ValueError("production observation requires world_id")
        if self.state.get("world_id") != world or self.state.get("catalog_fingerprint") != self._fingerprint:
            # Construction plans depend on prototypes; an issued startup
            # science allowance belongs to the world and cannot be renewed by
            # re-exporting the catalog or loading an older save.
            startup = deepcopy(self.state.get("startup_research", {})) if self.state.get("world_id") == world else {}
            self.state = {"schema_version": 1, "world_id": world, "catalog_fingerprint": self._fingerprint,
                          "blocks": {}, "links": {}, "power_links": {}, "flow_samples": {}, "startup_research": startup}
            self._save()
        tick = int(observation.get("tick") or 0)
        previous_tick = self.state.get("last_tick", 0)
        if tick < self.state.get("last_tick", 0):
            self.state["flow_samples"] = {}
            self.state.pop("bootstrap_science", None)
            self.state["automated_burners"] = []
            for upgrade in self.state.get("source_upgrades", {}).values():
                if upgrade.get("state") == "observed":
                    upgrade["state"] = "reserved"
                upgrade.pop("observed_tick", None)
                upgrade.pop("observed_unit_number", None)
            for budget in self.state.get("startup_research", {}).values():
                budget.pop("completion_tick", None)
        self.state["last_tick"] = tick
        if tick != previous_tick:
            self._save()

    def _save(self) -> None:
        self._invalidate_product_results()
        _atomic_json(self.path, self.state)

    def _invalidate_product_results(self) -> None:
        self._product_revision += 1
        self._product_results.clear()

    def _reserved(self, *, exclude: str | None = None) -> list[dict]:
        entities = []
        for category in ("blocks", "links", "power_links", "source_upgrades"):
            for key, plan in self.state.get(category, {}).items():
                if category == "links" and self.state.get("blocks", {}).get(key, {}).get("retired_for_upgrade"):
                    continue  # Preserve its history; the obsolete fuel link is never built again.
                if key != exclude:
                    entities.extend(plan.get("entities", []))
        return entities

    def _port_clearances(self) -> set[tuple[float, float]]:
        """Keep future belt approaches free when placing machines and poles."""
        clearances = set()
        for plan in self.state.get("blocks", {}).values():
            for port in plan.get("ports", []):
                if port.get("kind") != "item" or port.get("facing") not in DIRECTIONS:
                    continue
                dx, dy = DIRECTIONS[port["facing"]]
                outward = -1 if port["direction"] == "input" else 1
                clearances.add((port["position"]["x"] + dx * outward, port["position"]["y"] + dy * outward))
        return clearances

    def register_plan(self, key: str, plan: dict, obs: dict) -> dict:
        """Reserve an already positioned external block or fluid route.

        Shared end entities are allowed only when name, facing and fluid agree;
        other planned footprints remain unavailable to every producer/router.
        """
        self._sync(obs)
        if not plan.get("ok"):
            return plan
        def identity(entity: dict) -> tuple:
            position = entity["position"]
            direction = 0 if entity["name"] in {"pipe", "small-electric-pole", "wooden-chest", "iron-chest", "steel-chest"} else entity.get("direction", 0)
            return (entity["name"], position["x"], position["y"], direction)
        proposed = {identity(entity): entity for entity in plan.get("entities", [])}
        remaining = []
        for entity in self._reserved(exclude=key):
            matching = proposed.get(identity(entity))
            if matching is None or (matching.get("_fluid") and entity.get("_fluid") and matching["_fluid"] != entity["_fluid"]):
                remaining.append(entity)
        if self.builder._occupied_by_plan(plan.get("entities", [])) & self.builder._occupied_by_plan(remaining):
            return {"ok": False, "reason": "fixed plan overlaps another reserved footprint", "key": key}
        saved = deepcopy(plan)
        saved["key"] = key
        self.state["blocks"][key] = saved
        self._save()
        return saved

    def reserve_site(self, plan_at_origin: dict, key: str, obs: dict,
                     reference: dict | None = None) -> dict:
        self._sync(obs)
        if key in self.state["blocks"]:
            return self.state["blocks"][key]
        if not plan_at_origin.get("ok"):
            return plan_at_origin
        if reference is None:
            producers = [e for e in obs.get("entities", []) if e.get("name") == "stone-furnace"]
            reference = producers[0]["position"] if producers else obs.get("position", {"x": 0, "y": 0})
        occupied = self.builder._occupied_by_plan(self._reserved()) | self._port_clearances()
        offsets = [(x, y) for radius in (8, 16, 24, 32, 48, 64)
                   for x in range(-radius, radius + 1, 8) for y in range(-radius, radius + 1, 8)
                   if max(abs(x), abs(y)) == radius]
        offsets.sort(key=lambda p: p[0] * p[0] + p[1] * p[1])
        for dx, dy in offsets:
            plan = deepcopy(plan_at_origin)
            offset = {"x": math.floor(reference["x"]) + dx, "y": math.floor(reference["y"]) + dy}
            for obj in plan["entities"] + plan.get("ports", []):
                obj["position"] = {"x": obj["position"]["x"] + offset["x"], "y": obj["position"]["y"] + offset["y"]}
            if self.builder._occupied_by_plan(plan["entities"]) & occupied:
                continue
            machines = plan["entities"]
            protected = False
            if machines:
                payload = json.dumps(json.dumps(machines, separators=(",", ":")))
                survey = self.game.query('''
local machines=helpers.json_to_table(''' + payload + ''');local covered=0
for _,e in ipairs(machines) do
 local p=prototypes.entity[e.name]
 if p and (p.type=="assembling-machine" or p.type=="furnace" or p.type=="lab" or p.type=="rocket-silo" or p.type=="storage-tank") then
  local w=p.tile_width;local h=p.tile_height
  if e.direction==4 or e.direction==12 then w,h=h,w end
  covered=covered+s.count_entities_filtered{area={{e.position.x-w/2,e.position.y-h/2},{e.position.x+w/2,e.position.y+h/2}},type="resource"}
 end
end
return {ok=true,covered=covered}
''')
                if not survey.get("ok"):
                    return {"ok": False, "reason": "resource protection survey failed", "error": survey.get("reason")}
                protected = int(survey.get("covered", 0)) > 0
            if not protected and self.builder.can_place(plan["entities"]).get("ok"):
                plan["key"] = key
                self.state["blocks"][key] = plan
                self._save()
                return plan
        return {"ok": False, "reason": "no clear adjacent production block site", "key": key}

    def ensure_power_connection(self, obs: dict, key: str, plan: dict) -> dict:
        self._sync(obs)
        poles = [e for e in plan.get("entities", []) if e["name"] == "small-electric-pole"]
        if not poles:
            return _report("succeeded", "block requires no electric connection")
        grid = self._power_grid(obs, poles)
        if not grid.get("ok"):
            return _report("blocked", "cannot inspect factory power network", query_error=grid.get("reason"))
        if int(grid.get("connected", 0)) == len(poles):
            return _report("succeeded", "block poles connected to generator network")
        live = grid.get("live") or []
        if not live:
            return _report("blocked", "no generator-connected power pole is available")
        if len(poles) > 1:
            # A multi-crossing route can contain disconnected pole groups. A
            # single persisted wire path cannot repair all of them. Finish any
            # legacy path before assigning a stable connection to each pole.
            legacy = self.state["power_links"].get(key)
            if legacy:
                result = self.builder.ensure_plan(obs, legacy)
                if not _ready(result):
                    return result
            for pole in poles:
                x, y = pole["position"]["x"], pole["position"]["y"]
                result = self.ensure_power_connection(
                    obs, f"{key}:pole:{x:g},{y:g}", {**plan, "entities": [deepcopy(pole)]})
                if not _ready(result):
                    return result
            return _report("succeeded", "all block pole groups connected to generator network")
        if key not in self.state["power_links"]:
            candidates = sorted(((source, target) for source in live for target in poles),
                                key=lambda pair: _distance(pair[0], pair[1]["position"]))
            for source, target in candidates[:24]:
                if _distance(source, target["position"]) < .2:
                    continue
                route = self._power_route(source, target["position"])
                if not route.get("ok"):
                    continue
                path = route["path"]
                link = _plan([{"name": "small-electric-pole", "position": position, "direction": 0} for position in path])
                if self.builder.can_place(link["entities"]).get("ok"):
                    self.state["power_links"][key] = link
                    self._save()
                    break
            if key not in self.state["power_links"]:
                return _report("blocked", "no clear power connection to reserved factory block", block=key)
        result = self.builder.ensure_plan(obs, self.state["power_links"][key])
        return result if not _ready(result) else _report("waiting", "waiting for observed generator connection", block=key)

    def _power_grid(self, obs: dict, poles: list[dict]) -> dict:
        # Recursive production dependencies revisit the same poles during one
        # planning observation. Only their read evidence is shared; construction,
        # placement and action guards still run every time. The catalog and its
        # fingerprint are fixed snapshots for this planner's lifetime; prototype
        # changes reload the catalog/planner, rather than mutate it in place.
        context = (obs.get("world_id"), obs.get("tick"), self._fingerprint, id(self.catalog))
        if self._power_observation is not obs or self._power_context != context:
            self._power_observation, self._power_context = obs, context
            self._power_grids = {}
        positions = tuple((e["position"]["x"], e["position"]["y"]) for e in poles)
        if positions in self._power_grids:
            return self._power_grids[positions]
        payload = json.dumps(json.dumps([e["position"] for e in poles], separators=(",", ":")))
        grid = self.game.query('''
local wanted=helpers.json_to_table(''' + payload + ''');local networks={};local live={}
for _,e in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if e.electric_network_id then networks[e.electric_network_id]=true end
end
local connected=0
for _,p in ipairs(wanted) do local e=target(p,"small-electric-pole")
 if e and networks[e.electric_network_id] then connected=connected+1 end
end
for _,e in pairs(s.find_entities_filtered{force=f,type="electric-pole"}) do
 if networks[e.electric_network_id] then live[#live+1]=pos(e.position) end
end
return {ok=true,connected=connected,live=live}
''')
        if grid.get("ok") and int(grid.get("connected", 0)) == len(poles):
            self._power_grids[positions] = grid
        return grid

    def _power_route(self, source: dict, destination: dict) -> dict:
        # Electric wires pass over belts, machines and water. Walking a pole
        # footprint through every intervening tile falsely disconnects grids
        # enclosed by a conveyor loop, so route legal wire-length hops instead.
        bounds = {"min_x": min(source["x"], destination["x"]) - 48,
                  "max_x": max(source["x"], destination["x"]) + 48,
                  "min_y": min(source["y"], destination["y"]) - 48,
                  "max_y": max(source["y"], destination["y"]) + 48}
        points = [(source["x"] + x * 3, source["y"] + y * 3)
                  for x in range(math.ceil((bounds["min_x"] - source["x"]) / 3), math.floor((bounds["max_x"] - source["x"]) / 3) + 1)
                  for y in range(math.ceil((bounds["min_y"] - source["y"]) / 3), math.floor((bounds["max_y"] - source["y"]) / 3) + 1)]
        if len(points) > 25000:
            return {"ok": False, "reason": "power survey exceeds 25000 pole sites"}
        payload = json.dumps(json.dumps([{"x": x, "y": y} for x, y in points], separators=(",", ":")))
        survey = self.game.query('''
local positions=helpers.json_to_table(''' + payload + ''');local blocked={}
for _,p in ipairs(positions) do
 if not target(p,"small-electric-pole") and not s.can_place_entity{name="small-electric-pole",position=p,force=f} then
  blocked[#blocked+1]=p
 end
end
return {ok=true,blocked=blocked}
''')
        if not survey.get("ok"):
            return {"ok": False, "reason": survey.get("reason", "power placement survey failed")}
        blocked = self.builder._occupied_by_plan(self._reserved()) | self._port_clearances()
        blocked.update((p["x"], p["y"]) for p in survey.get("blocked", []))
        start, end = (source["x"], source["y"]), (destination["x"], destination["y"])
        allowed = set(points) - blocked
        allowed.add(start)
        vectors = [(x, y) for x in (-6, -3, 0, 3, 6) for y in (-6, -3, 0, 3, 6) if 0 < math.hypot(x, y) <= 7]
        frontier, cost, previous = [(0, start)], {start: 0.0}, {}
        visited = 0
        while frontier and visited < 25000:
            _, point = heapq.heappop(frontier)
            visited += 1
            if math.dist(point, end) <= 7:
                path = [end] if point != end else []
                while True:
                    path.append(point)
                    if point == start:
                        break
                    point = previous[point]
                return {"ok": True, "path": [{"x": x, "y": y} for x, y in reversed(path)]}
            for dx, dy in vectors:
                neighbor = point[0] + dx, point[1] + dy
                if neighbor not in allowed:
                    continue
                trial = cost[point] + math.hypot(dx, dy)
                if trial >= cost.get(neighbor, math.inf):
                    continue
                cost[neighbor], previous[neighbor] = trial, point
                heapq.heappush(frontier, (trial + math.dist(neighbor, end), neighbor))
        return {"ok": False, "reason": "no buildable pole chain within legal wire reach"}

    def _intake_poles(self, position: dict, equipment: list[dict]) -> list[dict]:
        occupied = self.builder._occupied_by_plan(equipment)
        offsets = [(dx, dy) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2) if dx or dy]
        offsets.sort(key=lambda offset: (max(abs(offset[0]), abs(offset[1])) != 2, abs(offset[0]) + abs(offset[1])))
        return [{"name": "small-electric-pole", "position": {"x": position["x"] + dx, "y": position["y"] + dy}, "direction": 0}
                for dx, dy in offsets if (position["x"] + dx, position["y"] + dy) not in occupied]

    def _source_endpoint(self, observation: dict, item: str) -> dict:
        resources = {"iron-plate": ("iron-ore", "stone-furnace"), "copper-plate": ("copper-ore", "stone-furnace"),
                     "coal": ("coal", "wooden-chest"), "stone": ("stone", "wooden-chest")}
        resource, receiver = resources[item]
        upgrade = ensure_source_upgrade(self, observation, item, resource)
        if upgrade is not None and not _ready(upgrade):
            return upgrade
        if upgrade is not None:
            cell = self.bootstrap.discover_cell(resource, receiver, preferred_receiver=upgrade["evidence"]["receiver"])
        else:
            existing = self.state["blocks"].get("source:" + item, {})
            preferred = (existing.get("active_source") or {}).get("receiver") or existing.get("source_receiver")
            cell = (self.bootstrap.discover_cell(resource, receiver, preferred_receiver=preferred)
                    if preferred else self.bootstrap.discover_cell(resource, receiver))
        if not cell.get("ok"):
            return _report("blocked", "raw-material source discovery failed", item=item, resource=resource,
                           query_error=cell.get("reason", "cell_site_query_failed"))
        if not cell.get("complete"):
            # A drill can exhaust after the supervisor's bootstrap observation.
            # Resume ordinary cell construction without changing the established
            # bus or granting ownership before its replacement is connected.
            result = self.bootstrap._ensure_cell(observation, resource, receiver)
            if result is not None and not _ready(result):
                return result
            return _report("waiting", "waiting for replacement raw-material cell observation", item=item, resource=resource)
        primary_key = "source:" + item
        key = primary_key
        primary = self.state["blocks"].get(primary_key)
        current_receiver = {"name": cell["receiver"]["name"], "position": deepcopy(cell["receiver"]["position"])}
        if primary:
            if "source_receiver" not in primary:
                association = self._identify_source_receiver(primary, receiver)
                if not association.get("ok"):
                    return _report("blocked", "legacy source receiver could not be identified", item=item,
                                   query_error=association.get("reason"))
                primary["source_receiver"] = association["receiver"]
                self._save()
            if primary["source_receiver"] != current_receiver:
                key = primary_key + ":relocation:" + self._entity_key(current_receiver)
                old_receiver = primary["source_receiver"].get("position")
                for entity in observation.get("entities", []):
                    if (old_receiver and entity.get("name") == "burner-mining-drill"
                            and entity.get("status_name") in {"no_minable_resources", "no_mineable_resources"}
                            and _distance(entity["position"], old_receiver) <= 3):
                        payload = json.dumps(json.dumps({"drill": entity["position"], "receiver": primary["source_receiver"]}))
                        ownership = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''')
local drill=target(args.drill,"burner-mining-drill");local receiver=target(args.receiver.position,args.receiver.name)
if not drill or not receiver then return {ok=true,feeds_receiver=false} end
local p=drill.drop_position
return {ok=true,feeds_receiver=math.abs(p.x-receiver.position.x)<=receiver.prototype.tile_width/2
 and math.abs(p.y-receiver.position.y)<=receiver.prototype.tile_height/2}
''')
                        if ownership.get("ok") and ownership.get("feeds_receiver"):
                            return {"type": "mine", "name": entity["name"], "position": entity["position"], "count": 1,
                                    "reason": "recover exhausted source drill to reconnect its established material bus"}
                # The established bus and every downstream consumer keep their
                # original endpoints. Restore that infrastructure if damaged.
                result = self.builder.ensure_plan(observation, primary)
                if not _ready(result):
                    return result
                result = self.ensure_power_connection(observation, primary_key, primary)
                if not _ready(result):
                    return result
        if key != primary_key and key not in self.state["blocks"]:
            planned = self._reserve_relocated_source(observation, item, current_receiver, key, primary)
            if not planned.get("ok"):
                if planned.get("action"):
                    return planned["action"]
                return _report("blocked", planned.get("reason", "relocated source has no clear continuity route"), item=item)
        if key not in self.state["blocks"]:
            p = cell["receiver"]["position"]
            width = 2 if receiver == "stone-furnace" else 1
            half = width / 2
            candidates = []
            for direction in (4, 12, 0, 8):
                dx, dy = DIRECTIONS[direction]
                for tangent in ((-.5, .5) if width == 2 else (0,)):
                    base = {"x": p["x"] - dy * tangent, "y": p["y"] + dx * tangent}
                    inserter = {"name": "inserter", "position": {"x": base["x"] + dx * (half + .5), "y": base["y"] + dy * (half + .5)},
                                "direction": (direction + 8) % 16}
                    belts = [{"name": "transport-belt", "position": {"x": base["x"] + dx * (half + .5 + n),
                                "y": base["y"] + dy * (half + .5 + n)}, "direction": direction} for n in (1, 2)]
                    port = {"kind": "item", "item": item, "direction": "output", "position": belts[-1]["position"], "facing": direction}
                    for pole in self._intake_poles(inserter["position"], [inserter, *belts]):
                        candidate = _plan([inserter, *belts, pole], [port], key=key,
                                          source_receiver=deepcopy(current_receiver))
                        if not self.builder._occupied_by_plan(candidate["entities"]) & (self.builder._occupied_by_plan(self._reserved()) | self._port_clearances()):
                            candidates.append(candidate)
            candidate = next((p for p in candidates if self.builder.can_place(p["entities"]).get("ok")), None)
            if candidate is None:
                return _report("blocked", "no clear material extraction port at source", item=item)
            self.state["blocks"][key] = candidate
            self._save()
        plan = self.state["blocks"][key]
        result = self.builder.ensure_plan(observation, plan)
        if not _ready(result):
            return result
        result = self.ensure_power_connection(observation, key, plan)
        if not _ready(result):
            return result
        primary = self.state["blocks"][primary_key]
        if key != primary_key:
            dependencies = list(plan.get("continuity_dependencies", []))
            if key + ":refill" in self.state["blocks"]:
                dependencies.append({"category": "blocks", "key": key + ":refill"})
            for dependency in dependencies:
                category, dependency_key = dependency.get("category"), dependency.get("key")
                maintained = self.state.get(category, {}).get(dependency_key) if category in {"blocks", "links"} else None
                if maintained is None:
                    return _report("blocked", "source continuity dependency is missing", item=item, dependency=dependency)
                if dependency_key.endswith(":refill"):
                    buffer = maintained.get("receiver") or primary["source_receiver"]
                    if not buffer.get("position") or buffer.get("name") not in {"wooden-chest", "iron-chest", "steel-chest"}:
                        return _report("blocked", "source continuity refill has no restorable item buffer", item=item)
                    if not any(self._entity_key(e) == self._entity_key(buffer) for e in maintained["entities"]):
                        maintained["entities"].insert(0, {**deepcopy(buffer), "direction": 0})
                        self._save()
                result = self.builder.ensure_plan(observation, maintained)
                if not _ready(result):
                    return result
                result = self.ensure_power_connection(observation, dependency_key, maintained)
                if not _ready(result):
                    return result
            result = self._merge_output(observation, plan["ports"][0], plan.get("continuity_target", primary["ports"][0]), key + ":output")
            if not _ready(result):
                return result
        coal = _report("succeeded", "coal source port", ports=primary["ports"]) if item == "coal" else self.ensure_product(observation, "coal")
        if not _ready(coal):
            return coal
        drill = cell.get("drill", {})
        burner_drill = not cell.get("electric") and (drill.get("name") == "burner-mining-drill"
            or self.catalog.entities.get(drill.get("name"), {}).get("burner"))
        burners = [drill] if burner_drill else []
        if receiver == "stone-furnace":
            burners.append(cell["receiver"])
        for burner in burners:
            if not burner.get("position"):
                continue
            result = self._fuel_burner(observation, burner, coal["evidence"]["ports"][0])
            if not _ready(result):
                return result
        association = {"extraction_block": key, "receiver": current_receiver, "drill": deepcopy(cell.get("drill"))}
        if primary.get("active_source") != association:
            primary["active_source"] = association
            self._save()
        return _report("succeeded", "active raw source connected to its established material bus",
                       ports=primary["ports"], active_source=association, relocated=key != primary_key, flow_verified=False)

    def _identify_source_receiver(self, plan: dict, receiver_name: str) -> dict:
        """Read the actual pickup receiver of a checkpoint predating identities."""
        inserters = [e for e in plan["entities"] if e["name"] == "inserter"]
        payload = json.dumps(json.dumps({"inserters": inserters, "receiver": receiver_name}, separators=(",", ":")))
        survey = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local receivers={};local seen={}
local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}
for _,row in ipairs(args.inserters) do
 local arm=target(row.position,row.name);local v=vectors[row.direction or 0]
 local pickup=arm and arm.pickup_position or {x=row.position.x+v[1],y=row.position.y+v[2]}
 for _,e in pairs(s.find_entities_filtered{position=pickup,radius=1.5,name=args.receiver,force=f}) do
  if math.abs(pickup.x-e.position.x)<e.prototype.tile_width/2
   and math.abs(pickup.y-e.position.y)<e.prototype.tile_height/2 and not seen[e.unit_number] then
   seen[e.unit_number]=true;receivers[#receivers+1]={name=e.name,position=pos(e.position)}
  end
 end
end
return {ok=true,receivers=receivers}
''')
        if not survey.get("ok") or "receivers" not in survey:
            return {"ok": False, "reason": survey.get("reason", "incomplete receiver survey")}
        receivers = list(survey["receivers"] or [])
        if len(receivers) > 1:
            return {"ok": False, "reason": "source extraction has multiple receiver identities"}
        # A destroyed old receiver does not invalidate the established bus.
        # Recording that it is missing allows a new active cell to refill it.
        return {"ok": True, "receiver": receivers[0] if receivers else {"name": receiver_name, "missing": True}}

    def _receiver_transfer_candidates(self, receiver: dict, item: str, *, output: bool,
                                      occupied: set[tuple[float, float]], blocked_equipment: list[dict] | None = None) -> list[dict]:
        """A single belt permits turns immediately after a crowded receiver."""
        p = receiver["position"]
        half = 1 if receiver["name"] in {"stone-furnace", "steel-furnace"} else .5
        candidates = []
        for direction, (dx, dy) in DIRECTIONS.items():
            for tangent in ((-.5, .5) if half == 1 else (0,)):
                base = {"x": p["x"] - dy * tangent, "y": p["y"] + dx * tangent}
                arm = {"name": "inserter", "position": {"x": base["x"] + dx * (half + .5), "y": base["y"] + dy * (half + .5)},
                       "direction": (direction + 8) % 16 if output else direction}
                belt_position = {"x": arm["position"]["x"] + dx, "y": arm["position"]["y"] + dy}
                facings = (direction, (direction + 4) % 16, (direction + 12) % 16) if output else ((direction + 8) % 16,)
                for facing in facings:
                    belt = {"name": "transport-belt", "position": belt_position, "direction": facing}
                    equipment = [arm, belt]
                    if self.builder._occupied_by_plan(equipment) & occupied:
                        continue
                    placement = self.builder.can_place(equipment)
                    if not placement.get("ok"):
                        if blocked_equipment is not None:
                            blocked_equipment.extend(e for e in placement.get("blocked", [])
                                                     if e.get("reason") == "terrain_or_entity_collision")
                        continue
                    accepted = 0
                    for pole in self._intake_poles(arm["position"], equipment):
                        entities = [*equipment, pole]
                        fx, fy = DIRECTIONS[facing]
                        if output and pole["position"] == {"x": belt_position["x"] + fx, "y": belt_position["y"] + fy}:
                            continue
                        if self.builder._occupied_by_plan(entities) & occupied:
                            continue
                        if not self.builder.can_place(entities).get("ok"):
                            continue
                        candidates.append(_plan(entities, [{"kind": "item", "item": item,
                            "direction": "output" if output else "input", "position": belt_position, "facing": facing}]))
                        accepted += 1
                        if accepted == 2:
                            break
        return candidates

    def _previous_source_targets(self, item: str, primary: dict) -> tuple[list[dict], list[dict], dict | None]:
        """Reuse only the directed upstream tail of the established source.

        Its complete construction dependencies stay owned by the replacement,
        including the original chest. Consumer branches are never candidates.
        """
        primary_key = "source:" + item
        previous_key = (primary.get("active_source") or {}).get("extraction_block")
        previous = self.state["blocks"].get(previous_key)
        link = self.state["links"].get(str(previous_key) + ":output")
        if not previous or previous_key == primary_key or not link or not previous.get("ports"):
            return [], [], None
        source, target = previous["ports"][0], previous.get("continuity_target", primary["ports"][0])
        if (source.get("item") != item or target.get("item") != item
                or link.get("source_port") != source or link.get("consumer_port") != target):
            return [], [], None
        dependencies = deepcopy(previous.get("continuity_dependencies", []))
        if not dependencies:
            dependencies = [{"category": "blocks", "key": primary_key}]
        if any(d.get("category") not in {"blocks", "links"}
               or d.get("key") not in self.state[d["category"]] for d in dependencies):
            return [], [], None
        if {"category": "blocks", "key": primary_key} not in dependencies:
            return [], [], None
        refill_key = previous_key + ":refill"
        refill = self.state["blocks"].get(refill_key)
        if refill:
            receiver = refill.get("receiver") or primary["source_receiver"]
            receivers = [self.state["blocks"][d["key"]].get("source_receiver")
                         for d in dependencies if d["category"] == "blocks"]
            if receiver not in receivers or target not in refill.get("ports", []):
                return [], [], None
            # Legacy refill checkpoints omit receiver identity; establish
            # the exact ordinary inserter drop geometry before reusing it.
            p = receiver["position"]
            matching_arm = any(e["name"] == "inserter" and e.get("direction") in DIRECTIONS
                and e["position"] == {"x": p["x"] + DIRECTIONS[e["direction"]][0],
                                      "y": p["y"] + DIRECTIONS[e["direction"]][1]}
                and target["position"] == {"x": p["x"] + 2 * DIRECTIONS[e["direction"]][0],
                                           "y": p["y"] + 2 * DIRECTIONS[e["direction"]][1]}
                and target["facing"] == (e["direction"] + 8) % 16
                for e in refill["entities"])
            if not matching_arm:
                return [], [], None
            dependencies.append({"category": "blocks", "key": refill_key})
        elif not any(e["name"] == "transport-belt" and e["position"] == target["position"]
                     and e["direction"] == target["facing"]
                     for d in dependencies for e in self.state[d["category"]][d["key"]]["entities"]):
            return [], [], None
        dependencies += [{"category": "blocks", "key": previous_key},
                         {"category": "links", "key": previous_key + ":output"}]
        dependencies = [dict(category=category, key=key) for category, key in
                        dict.fromkeys((d["category"], d["key"]) for d in dependencies)]
        belts = {}
        for entity in link["entities"]:
            if entity["name"] != "transport-belt":
                continue
            point = entity["position"]["x"], entity["position"]["y"]
            if point in belts and belts[point]["direction"] != entity["direction"]:
                return [], [], None
            belts[point] = entity
        end = target["position"]["x"], target["position"]["y"]
        destinations = []
        for start, entity in belts.items():
            point, seen = start, set()
            while point in belts and point not in seen:
                seen.add(point)
                belt = belts[point]
                if point == end:
                    if belt["direction"] == target["facing"]:
                        destinations.append({**target, "position": entity["position"], "facing": entity["direction"]})
                    break
                delta = DIRECTIONS.get(belt.get("direction"))
                if delta is None:
                    break
                point = point[0] + delta[0], point[1] + delta[1]
        # Refilling a previous receiver is safe only when its extraction belt
        # itself belongs to the proven tail, not merely a downstream fragment.
        receiver = previous.get("source_receiver") if any(p["position"] == source["position"]
            and p["facing"] == source["facing"] for p in destinations) else None
        return destinations, dependencies, receiver

    def _reserve_relocated_source(self, obs: dict, item: str, receiver: dict, key: str, primary: dict) -> dict:
        reserved = self._reserved()
        occupied = self.builder._occupied_by_plan(reserved + obs.get("entities", [])) | self._port_clearances()
        blocked_equipment = []
        outputs = self._receiver_transfer_candidates(receiver, item, output=True, occupied=occupied,
                                                    blocked_equipment=blocked_equipment)
        # Every belt in the original short extraction segment precedes every
        # downstream branch. Merging into an arbitrary consumer branch would
        # leave the other consumers starved.
        destinations = [(None, {**primary["ports"][0], "position": e["position"], "facing": e["direction"]}, [])
                        for e in primary["entities"] if e["name"] == "transport-belt"]
        old = primary["source_receiver"]
        upstream, dependencies, previous_receiver = self._previous_source_targets(item, primary)
        destinations += [(None, target, dependencies) for target in upstream]
        for buffer, maintained in [(old, [])] + ([(previous_receiver, dependencies)] if previous_receiver else []):
            if buffer.get("position") and buffer["name"] in {"wooden-chest", "iron-chest", "steel-chest"}:
                for plan in self._receiver_transfer_candidates(buffer, item, output=False, occupied=occupied,
                                                               blocked_equipment=blocked_equipment):
                    plan["receiver"] = deepcopy(buffer)
                    destinations.append((plan, plan["ports"][0], maintained))
        pairs = [(output, refill, destination, maintained) for output in outputs for refill, destination, maintained in destinations]
        pairs.sort(key=lambda row: _distance(row[0]["ports"][0]["position"], row[2]["position"]))
        def reserve_route(output: dict, refill: dict | None, destination: dict, maintained: list, route: dict) -> dict:
            route["segments"][-1]["direction"] = destination["facing"]
            output.update(key=key, source_receiver=deepcopy(receiver), continuity_target=deepcopy(destination),
                          continuity_dependencies=deepcopy(maintained))
            self.state["blocks"][key] = output
            if refill:
                refill["entities"].insert(0, {**deepcopy(refill["receiver"]), "direction": 0})
                self.state["blocks"][key + ":refill"] = refill
            self.state["links"][key + ":output"] = _plan(
                [{"name": "transport-belt", **segment} for segment in route["segments"]],
                source_port=output["ports"][0], consumer_port=destination)
            self._save()
            return output

        blocked_routes = {}
        for output, refill, destination, maintained in pairs[:96]:
            equipment = output["entities"] + (refill["entities"] if refill else [])
            if refill and self.builder._occupied_by_plan(output["entities"]) & self.builder._occupied_by_plan(refill["entities"]):
                continue
            source = output["ports"][0]
            dx, dy = DIRECTIONS[destination["facing"]]
            front = {"name": "port-clearance", "position": {"x": destination["position"]["x"] + dx,
                                                             "y": destination["position"]["y"] + dy}}
            route_reserved = reserved + equipment + [front]
            route = self._material_route(source["position"], destination["position"], route_reserved,
                                         start_direction=source["facing"], allow_bridge=False)
            if not route.get("ok"):
                if route.get("reason") == "no route within bounds":
                    point = destination["position"]["x"], destination["position"]["y"]
                    blocked_routes.setdefault(point, (output, refill, destination, maintained, route_reserved))
                continue
            return reserve_route(output, refill, destination, maintained, route)
        obstacle = self._source_corridor_obstacle(blocked_equipment)
        if obstacle is not None:
            return {"ok": False, "action": obstacle}
        alternatives = []
        for output, refill, destination, maintained, route_reserved in list(blocked_routes.values())[:4]:
            source = output["ports"][0]
            clearances = self._port_clearances()
            dx, dy = DIRECTIONS[source["facing"]]
            clearances.discard((source["position"]["x"] + dx, source["position"]["y"] + dy))
            route_reserved = route_reserved + [{"name": "port-clearance", "position": {"x": x, "y": y}}
                                               for x, y in clearances]
            alternatives.append((source, destination, route_reserved))
            for margin in (48, 96):
                route = self.builder.route(source["position"], destination["position"], "transport-belt", route_reserved,
                    start_direction=source["facing"], margin=margin)
                if route.get("ok"):
                    return reserve_route(output, refill, destination, maintained, route)
                if route.get("reason") != "no route within bounds":
                    break
        # Prove a complete alternative corridor before mining a route obstacle.
        # The helper returns one normal action, never a buildable path through
        # an uncleared tree/rock. Bound surveys across distinct intake targets.
        for source, destination, route_reserved in alternatives:
            for margin in (24, 48, 96):
                clearing = self.builder.clear_route_obstacle(source["position"], destination["position"], route_reserved,
                    start_direction=source["facing"], margin=margin)
                if clearing.get("ok") and clearing.get("action"):
                    return {"ok": False, "action": clearing["action"]}
                if clearing.get("reason") != "no route within bounds":
                    break
        return {"ok": False, "reason": "relocated receiver cannot reach its original source segment or buffer"}

    def _source_corridor_obstacle(self, blocked: list[dict]) -> dict | None:
        positions = sorted({(e["position"]["x"], e["position"]["y"]) for e in blocked})[:64]
        if not positions:
            return None
        payload = json.dumps(json.dumps([{"x": x, "y": y} for x, y in positions]))
        survey = self.game.query('''
local positions=helpers.json_to_table(''' + payload + ''');local obstacles={};local seen={}
for _,p in ipairs(positions) do
 for _,e in pairs(s.find_entities_filtered{area={{p.x-.45,p.y-.45},{p.x+.45,p.y+.45}},force="neutral"}) do
  if (e.type=="tree" or e.type=="simple-entity") and e.minable
   and not string.find(e.name,"crash") and not string.find(e.name,"wreck") then
   local key=e.name..":"..e.position.x..","..e.position.y
   if not seen[key] then
    seen[key]=true;obstacles[#obstacles+1]={name=e.name,type=e.type,position=pos(e.position),force=e.force.name,minable=true}
   end
  end
 end
end
return {ok=true,obstacles=obstacles}
''')
        if survey.get("ok"):
            for entity in sorted(survey.get("obstacles") or [], key=lambda e: (e["position"]["x"], e["position"]["y"], e["name"])):
                if (entity.get("type") in {"tree", "simple-entity"} and entity.get("force") == "neutral"
                        and entity.get("minable") and not any(term in entity["name"] for term in ("crash", "wreck"))):
                    return {"type": "mine", "name": entity["name"], "position": entity["position"], "count": 1,
                            "reason": "clear observed natural obstacle from source continuity equipment footprint"}
        return None

    @staticmethod
    def _entity_key(entity: dict) -> str:
        return f'{entity["name"]}:{entity["position"]["x"]:g},{entity["position"]["y"]:g}'

    def owns_automated_burner(self, entity: dict) -> bool:
        return self._entity_key(entity) in self.state.get("automated_burners", [])

    def request_recipe_unlock(self, obs: dict, recipe_name: str) -> dict:
        self._sync(obs)
        if (obs.get("enabled_recipes") or {}).get(recipe_name):
            return _report("succeeded", "required recipe is observed enabled", recipe=recipe_name)
        unlocks = sorted(name for name, technology in self.catalog.technologies.items()
                         if recipe_name in technology.get("unlocks", []))
        if not unlocks:
            return _report("blocked", "required recipe has no catalog research unlock", recipe=recipe_name)
        pending = self.state.setdefault("capability_research", [])
        if unlocks[0] not in pending:
            pending.append(unlocks[0])
            self._save()
        return _report("waiting", "required production capability queued for research", recipe=recipe_name, technology=unlocks[0])

    def _fuel_burner(self, obs: dict, burner: dict, coal_port: dict) -> dict:
        key = "fuel:" + self._entity_key(burner)
        if self.state["blocks"].get(key, {}).get("retired_for_upgrade"):
            return _report("blocked", "raw drill fuel intake was retired for electric replacement", burner=burner)
        if key not in self.state["blocks"]:
            center = burner["position"]
            width = 2 if burner["name"] in {"burner-mining-drill", "stone-furnace", "steel-furnace"} else 1
            occupied = self.builder._occupied_by_plan(self._reserved()) | self._port_clearances()
            for direction in (12, 4, 8, 0):
                dx, dy = DIRECTIONS[direction]
                for tangent in ((-.5, .5) if width == 2 else (0,)):
                    base = {"x": center["x"] - dy * tangent, "y": center["y"] + dx * tangent}
                    position = {"x": base["x"] + dx * (width / 2 + .5), "y": base["y"] + dy * (width / 2 + .5)}
                    inserter = {"name": "inserter", "position": position, "direction": direction}
                    belts = [{"name": "transport-belt", "position": {"x": position["x"] + dx * n, "y": position["y"] + dy * n},
                              "direction": (direction + 8) % 16} for n in (1, 2)]
                    approach = (belts[-1]["position"]["x"] + dx, belts[-1]["position"]["y"] + dy)
                    if approach in occupied:
                        continue
                    for pole in self._intake_poles(position, [inserter, *belts]):
                        candidate = _plan([inserter, *belts, pole], [{"kind": "item", "item": "coal", "direction": "input",
                                                                  "position": belts[-1]["position"], "facing": (direction + 8) % 16}])
                        if self.builder._occupied_by_plan(candidate["entities"]) & occupied:
                            continue
                        if self.builder.can_place(candidate["entities"]).get("ok"):
                            self.state["blocks"][key] = candidate
                            self._save()
                            break
                    if key in self.state["blocks"]:
                        break
                if key in self.state["blocks"]:
                    break
            if key not in self.state["blocks"]:
                from .deterministic_fuel_intake import reserve_adjacent_fuel_intake
                if not reserve_adjacent_fuel_intake(self, obs, burner, coal_port, key):
                    return _report("blocked", "no clear automatic burner fuel intake", burner=burner)
        plan = self.state["blocks"][key]
        if plan.get("adjacent_fuel_intake"):
            from .deterministic_fuel_intake import validate_adjacent_fuel_intake
            invalid = validate_adjacent_fuel_intake(self, obs, burner, coal_port, key)
            if invalid is not None:
                return invalid
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        result = self.ensure_power_connection(obs, key, plan)
        if not _ready(result):
            return result
        result = self.connect_input(obs, coal_port, plan["ports"][0], key)
        if not _ready(result):
            return result
        owned = self.state.setdefault("automated_burners", [])
        if self._entity_key(burner) not in owned:
            owned.append(self._entity_key(burner))
            self._save()
        return _report("succeeded", "burner coal belt and powered intake constructed", input_handcarry=False)

    def ensure_product(self, obs: dict, item: str, _stack: tuple[str, ...] = (), *,
                       rate_per_minute: float | None = None) -> dict:
        self._sync(obs)
        if item in _stack:
            return _report("blocked", "production dependency cycle", item=item)
        context = (obs.get("world_id"), obs.get("tick"), self._fingerprint,
                   id(self.catalog), self.catalog.fingerprint, id(self.graph),
                   id(self.builder), id(self.bootstrap), id(self.fluids))
        if self._product_observation is not obs or self._product_context != context:
            self._product_observation, self._product_context = obs, context
            self._invalidate_product_results()
        # Repeated recursive dependencies can share completed checks within one
        # observation. A save can establish ownership or alter a reserved route,
        # so neither that evaluation nor any earlier result remains reusable.
        # Exact ancestor stacks also preserve dependency-cycle detection.
        key = (item, rate_per_minute, _stack)
        if key in self._product_results:
            return deepcopy(self._product_results[key])
        revision = self._product_revision
        result = self._ensure_product(obs, item, _stack, rate_per_minute=rate_per_minute)
        if _ready(result) and self._product_revision == revision:
            self._product_results[key] = deepcopy(result)
            return deepcopy(result)
        return result

    def _ensure_product(self, obs: dict, item: str, _stack: tuple[str, ...], *,
                        rate_per_minute: float | None) -> dict:
        if item in {"iron-plate", "copper-plate", "coal", "stone"}:
            result = self._source_endpoint(obs, item)
            if _ready(result) and rate_per_minute is not None:
                return self._expand_raw_source(obs, item, rate_per_minute, result["evidence"]["ports"][0])
            return result
        recipe = self.catalog.recipe_for_product(item)
        if recipe is None:
            return _report("blocked", "no catalog production recipe", item=item)
        if recipe["name"] not in (obs.get("enabled_recipes") or {}):
            result = self.request_recipe_unlock(obs, recipe["name"])
            if result["status"] == "blocked":
                result["reason"] = "production recipe is locked and has no catalog research unlock"
            return result
        if any(row.get("type", "item") == "fluid" for row in recipe["ingredients"] + recipe["products"]):
            # The fluid driver owns separate mutable state. Its success and all
            # enclosing product evaluations must retain their original checks.
            self._invalidate_product_results()
            if self.fluids is None:
                return _report("blocked", "fluid production driver is required", item=item)
            return self.fluids.ensure_source(obs, item, rate_per_minute=rate_per_minute) if rate_per_minute is not None else self.fluids.ensure_source(obs, item)
        machines = self.graph.machines_for_recipe(recipe["name"], obs)
        if not machines:
            future = self.graph.machines_for_recipe(recipe["name"], obs, unlocked_only=False)
            for machine in future:
                for placement in machine.get("placement_items", []):
                    build_recipe = self.catalog.recipe_for_product(placement)
                    if build_recipe:
                        result = self.request_recipe_unlock(obs, build_recipe["name"])
                        if result["status"] == "waiting":
                            return result
            return _report("blocked", "no unlocked production machine", item=item, recipe=recipe["name"])
        machine = machines[0]["name"]
        if machine not in {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "stone-furnace", "steel-furnace"}:
            return _report("blocked", "solid machine needs a specialized supply block", item=item, machine=machine)
        inputs = [row["name"] for row in recipe["ingredients"]]
        if machine in {"stone-furnace", "steel-furnace"} and "coal" not in inputs:
            inputs.append("coal")
        if len(inputs) > 3:
            return _report("blocked", "solid recipe exceeds available input-port geometry", recipe=recipe["name"])
        sources = {}
        for ingredient in inputs:
            source = self.ensure_product(obs, ingredient, (*_stack, item))
            if not _ready(source):
                return source
            sources[ingredient] = source["evidence"]["ports"][0]
        key = "recipe:" + recipe["name"]
        origin = build_template("furnace_row" if machine in {"stone-furnace", "steel-furnace"} else "assembler_row",
                                recipe=recipe["name"], machine=machine, inputs=inputs, output=item)
        reference = {axis: sum(p["position"][axis] for p in sources.values()) / len(sources) for axis in ("x", "y")} if sources else None
        plan = self.reserve_site(origin, key, obs, reference)
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "factory site unavailable"), item=item)
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        result = self.ensure_power_connection(obs, key, plan)
        if not _ready(result):
            return result
        for port in plan["ports"]:
            if port["kind"] != "item" or port["direction"] != "input":
                continue
            result = self.connect_input(obs, sources[port["item"]], port, key + ":" + port["item"])
            if not _ready(result):
                return result
        outputs = [p for p in plan["ports"] if p["kind"] == "item" and p["direction"] == "output"]
        desired_count = 1
        if rate_per_minute is not None:
            output_per_cycle = sum(float(row.get("amount", 1)) * float(row.get("probability", 1)) for row in recipe["products"] if row["name"] == item)
            machine_rate = float(machines[0]["crafting_speed"]) * 60 / float(recipe["energy"]) * output_per_cycle
            desired_count = max(1, math.ceil(rate_per_minute / machine_rate - 1e-9))
            for index in range(1, desired_count):
                extra_key = key + ":capacity:" + str(index)
                extra = self.reserve_site(origin, extra_key, obs, plan["entities"][0]["position"])
                if not extra.get("ok"):
                    return _report("blocked", extra.get("reason", "capacity expansion site unavailable"), item=item, required_machines=desired_count)
                result = self.builder.ensure_plan(obs, extra)
                if not _ready(result):
                    return result
                result = self.ensure_power_connection(obs, extra_key, extra)
                if not _ready(result):
                    return result
                for port in extra["ports"]:
                    if port["kind"] != "item" or port["direction"] != "input":
                        continue
                    result = self.connect_input(obs, sources[port["item"]], port, extra_key + ":" + port["item"])
                    if not _ready(result):
                        return result
                output = next(p for p in extra["ports"] if p["kind"] == "item" and p["direction"] == "output")
                result = self._merge_output(obs, output, outputs[0], extra_key + ":output")
                if not _ready(result):
                    return result
        return _report("succeeded", "automatic production block connected", ports=outputs, flow_verified=False,
                       recipe=recipe["name"], input_handcarry=False, machines_constructed=desired_count,
                       requested_rate_per_minute=rate_per_minute)

    def _upstream_output_tails(self, obs: dict, bus_port: dict) -> list[dict]:
        """Keep only owned same-item belt tails that reach the original bus.

        Missing reserved belts remain paid construction dependencies. Observed
        belts must retain their facing; consumer branches are never endpoints.
        """
        end = bus_port["position"]["x"], bus_port["position"]["y"]
        candidates, seen_tails = [], set()
        observed = {(e["position"]["x"], e["position"]["y"]): e
                    for e in obs.get("entities", []) if e["name"] == "transport-belt"}
        reserved = self._reserved()
        foreign = [e for plan in self.state["links"].values()
                   if any((plan.get(field) or {}).get("item") not in (None, bus_port["item"])
                          for field in ("source_port", "consumer_port"))
                   for e in plan.get("entities", [])]
        foreign_footprint = self.builder._occupied_by_plan(foreign)
        for category in ("blocks", "links"):
            for key, plan in self.state[category].items():
                if category == "blocks":
                    if bus_port not in plan.get("ports", []):
                        continue
                elif (plan.get("consumer_port") != bus_port
                      or (plan.get("source_port") or {}).get("item") != bus_port["item"]):
                    continue
                belts, contradictory = {}, False
                for entity in plan.get("entities", []):
                    if entity["name"] != "transport-belt":
                        continue
                    point = entity["position"]["x"], entity["position"]["y"]
                    if point in belts and belts[point].get("direction") != entity.get("direction"):
                        contradictory = True
                        break
                    belts[point] = entity
                if contradictory:
                    continue
                for start, entity in belts.items():
                    if start == end:
                        continue
                    point, visited, tail = start, set(), []
                    while point in belts and point not in visited and len(tail) < 256:
                        visited.add(point)
                        belt = belts[point]
                        delta = DIRECTIONS.get(belt.get("direction"))
                        actual = observed.get(point)
                        if (delta is None or (actual and (actual.get("direction") != belt["direction"]
                                or any(item != bus_port["item"] and count > 0
                                       for item, count in actual.get("belt_inventory", {}).items())))):
                            break
                        tail.append(belt)
                        if point == end:
                            if belt["direction"] != bus_port["facing"]:
                                break
                            signature = tuple((b["position"]["x"], b["position"]["y"], b["direction"]) for b in tail)
                            if signature in seen_tails:
                                break
                            matching = set(signature)
                            other = [e for e in reserved if (e["name"] != "transport-belt"
                                or (e["position"]["x"], e["position"]["y"], e.get("direction")) not in matching)]
                            if self.builder._occupied_by_plan(tail) & (self.builder._occupied_by_plan(other) | foreign_footprint):
                                break
                            seen_tails.add(signature)
                            candidates.append({"entities": deepcopy(tail), "category": category, "key": key,
                                "port": {**bus_port, "position": deepcopy(entity["position"]), "facing": entity["direction"]}})
                            break
                        following = point[0] + delta[0], point[1] + delta[1]
                        if following in belts and belts[following].get("direction") == (belt["direction"] + 8) % 16:
                            break
                        point = following
        return candidates

    def _source_pickup_bridge_route(self, obs: dict, source_port: dict, destination: dict, reserved: list[dict]) -> dict:
        """An owned output belt may feed a crossing without changing its facing."""
        position = source_port["position"]
        actual = next((e for e in obs.get("entities", []) if e["name"] == "transport-belt"
                       and e["position"] == position and e.get("direction") == source_port.get("facing")), None)
        owned = any(source_port in plan.get("ports", []) and any(e["name"] == "transport-belt"
                    and e["position"] == position and e.get("direction") == source_port.get("facing")
                    for e in plan.get("entities", [])) for plan in self.state["blocks"].values())
        if (not owned or not actual or not actual.get("unit_number")
                or source_port.get("kind") != "item" or source_port.get("direction") != "output"
                or source_port.get("item") != destination.get("item")
                or self.state.get("world_id") != obs.get("world_id")):
            return {"ok": False, "reason": "crossing pickup is not an observed owned output belt"}
        args = json.dumps(json.dumps({"position": position, "direction": actual["direction"],
            "unit": actual["unit_number"], "world": obs["world_id"], "item": source_port["item"]}, separators=(",", ":")))
        survey = self.game.query('''
local x=helpers.json_to_table(''' + args + ''')
local source=target(x.position,"transport-belt")
if not d or d.world_id~=x.world or not source or source.force~=f or source.unit_number~=x.unit
 or source.direction~=x.direction then return {ok=false,reason="owned crossing pickup changed"} end
for lane=1,2 do for _,item in pairs(source.get_transport_line(lane).get_contents()) do
 if item.name~=x.item and item.count>0 then return {ok=false,reason="crossing pickup contains another item"} end
end end
local recipe=f.recipes["long-handed-inserter"]
if not recipe or not recipe.enabled then return {ok=false,reason="long inserter recipe is locked"} end
local candidates={}
for direction,v in pairs({[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}) do
 local p={x=x.position.x+3*v[1],y=x.position.y+3*v[2]}
 local belt=target(p,"transport-belt")
 if belt and belt.force==f and belt.direction%8~=direction%8 then candidates[#candidates+1]={direction=direction,over=p} end
end
return {ok=true,candidates=candidates}
''')
        if not survey.get("ok"):
            return survey
        clearances = [{"name": "port-clearance", "position": {"x": x, "y": y}} for x, y in self._port_clearances()]
        reserved = reserved + clearances
        limited = [e for e in reserved if e["name"] != "transport-belt" or e["position"] != position
                   or e.get("direction") != actual["direction"]]
        occupied = self.builder._occupied_by_plan(limited)
        # An identical geometric reservation for another item is still foreign.
        for plan in self.state["links"].values():
            if any((plan.get(field) or {}).get("item") not in (None, source_port["item"])
                   for field in ("source_port", "consumer_port")):
                occupied.update(self.builder._occupied_by_plan(plan.get("entities", [])))
        best = None
        for candidate in sorted(survey.get("candidates", []), key=lambda row: row["direction"])[:4]:
            direction = candidate["direction"]
            dx, dy = DIRECTIONS[direction]
            drop = {"x": position["x"] + 4 * dx, "y": position["y"] + 4 * dy}
            if drop == destination["position"]:
                continue  # Destination sharing requires its own identity proof.
            source = {"name": "transport-belt", "position": position, "direction": actual["direction"]}
            arm = {"name": "long-handed-inserter", "position": {"x": position["x"] + 2 * dx,
                   "y": position["y"] + 2 * dy}, "direction": (direction + 8) % 16}
            equipment = [source, arm, {"name": "transport-belt", "position": drop, "direction": direction}]
            if self.builder._occupied_by_plan(equipment) & occupied:
                continue
            for pole in self._intake_poles(arm["position"], equipment)[:8]:
                trial = equipment + [pole]
                if self.builder._occupied_by_plan(trial) & occupied or not self.builder.can_place(trial).get("ok"):
                    continue
                route = self.builder.route(drop, destination["position"], "transport-belt", reserved + trial,
                                           margin=48, start_direction=direction)
                if not route.get("ok"):
                    break
                segments = [source, arm, pole] + route["segments"]
                if best is None or len(segments) < len(best["segments"]):
                    best = {"ok": True, "segments": segments, "flow_verified": False,
                            "crossing": {"kind": "long-handed-inserter", "over": candidate["over"],
                                         "pickup_unit_number": actual["unit_number"]}}
                break
        return best or {"ok": False, "reason": "no clear owned-output pickup crossing"}

    def _route_upstream_output(self, obs: dict, source_port: dict, bus_port: dict) -> dict:
        tails = self._upstream_output_tails(obs, bus_port)
        def identity(entity: dict) -> tuple:
            return entity["name"], entity["position"]["x"], entity["position"]["y"], entity.get("direction")
        owned = {identity(e) for tail in tails for e in tail["entities"]}
        source = ("transport-belt", source_port["position"]["x"], source_port["position"]["y"], source_port.get("facing"))
        if any(source_port in plan.get("ports", []) and any(identity(e) == source for e in plan.get("entities", []))
               for plan in self.state["blocks"].values()):
            owned.add(source)
        observed = {identity(e) for e in obs.get("entities", [])
                    if e["name"] == "transport-belt" and type(e.get("unit_number")) is int and e["unit_number"] > 0
                    and obs.get("world_id") == self.state.get("world_id") and identity(e) in owned}
        def new_count(entities: list[dict]) -> int:
            return sum(identity(e) not in observed for e in entities)
        tails.sort(key=lambda tail: (_distance(source_port["position"], tail["port"]["position"])
                                    + new_count(tail["entities"]),
                                    _distance(source_port["position"], tail["port"]["position"]) + len(tail["entities"]), tail["key"]))
        # Bound recovery work even when a large factory has many old outputs.
        tails = tails[:16]
        reserved = self._reserved()
        for mode in ("belts", "source-pickup", "bridge"):
            candidates = []
            for tail in tails:
                destination = tail["port"]
                dx, dy = DIRECTIONS[destination["facing"]]
                front = {"name": "port-clearance", "position": {"x": destination["position"]["x"] + dx,
                                                                "y": destination["position"]["y"] + dy}}
                route = (self._source_pickup_bridge_route(obs, source_port, destination, reserved + [front])
                    if mode == "source-pickup" else self._material_route(source_port["position"], destination["position"],
                        reserved + [front], allow_bridge=mode == "bridge", start_direction=source_port.get("facing")))
                if not route.get("ok"):
                    continue
                # The copied suffix provides every missing construction step;
                # a current placement check rejects changed entities/terrain.
                segments = route["segments"][:-1] + tail["entities"]
                entities = [{"name": "transport-belt", **segment} for segment in segments]
                candidates.append({**route, "segments": entities, "upstream_tail": {
                    "category": tail["category"], "key": tail["key"], "entry_port": destination}})
            # Nearby entry points can require long detours around an existing
            # bus. Compare complete construction costs within the bounded set.
            candidates.sort(key=lambda route: (new_count(route["segments"]), len(route["segments"])))
            for candidate in candidates:
                if self.builder.can_place(candidate["segments"]).get("ok"):
                    return candidate
        return {"ok": False, "reason": "no reachable owned upstream output tail"}

    def _merge_output(self, obs: dict, source_port: dict, bus_port: dict, key: str) -> dict:
        """Join same-item capacity outputs while retaining the existing bus facing."""
        if source_port.get("item") != bus_port.get("item"):
            return _report("blocked", "cannot merge different material outputs")
        if key not in self.state["links"]:
            dx, dy = DIRECTIONS[bus_port["facing"]]
            forbidden_front = {"name": "port-clearance", "position": {"x": bus_port["position"]["x"] + dx,
                                                                         "y": bus_port["position"]["y"] + dy}}
            route = self._material_route(source_port["position"], bus_port["position"], self._reserved() + [forbidden_front],
                                         allow_bridge=False, start_direction=source_port.get("facing"))
            if not route.get("ok") and route.get("reason") in {"no route within bounds", "route search budget exhausted"}:
                route = self._route_upstream_output(obs, source_port, bus_port)
                if not route.get("ok"):
                    route = self._material_route(source_port["position"], bus_port["position"], self._reserved() + [forbidden_front],
                                                 start_direction=source_port.get("facing"))
            if not route.get("ok"):
                return _report("blocked", "capacity output cannot reach its material bus", link=key, query_error=route.get("reason"))
            segments = route["segments"]
            segments[-1]["direction"] = bus_port["facing"]
            self.state["links"][key] = _plan([{"name": "transport-belt", **segment} for segment in segments],
                                              source_port=source_port, consumer_port=bus_port)
            if route.get("upstream_tail"):
                self.state["links"][key]["upstream_tail"] = route["upstream_tail"]
            self._save()
        plan = self.state["links"][key]
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        if any(entity["name"] == "small-electric-pole" for entity in plan["entities"]):
            result = self.ensure_power_connection(obs, "merge:" + key, plan)
            if not _ready(result):
                return result
        return _report("succeeded", "additional producer joins its same-item output bus", flow_verified=False)

    def _material_route(self, source: dict, destination: dict, reserved: list[dict], *,
                        allow_bridge: bool = True, **directions: Any) -> dict:
        clearances = self._port_clearances()
        for position, direction, sign in ((source, directions.get("start_direction"), 1), (destination, directions.get("end_direction"), -1)):
            if direction in DIRECTIONS:
                dx, dy = DIRECTIONS[direction]
                clearances.discard((position["x"] + dx * sign, position["y"] + dy * sign))
        # These are planning-only footprint obstacles, never constructed entities.
        reserved = reserved + [{"name": "port-clearance", "position": {"x": x, "y": y}} for x, y in clearances]
        result = {"ok": False, "reason": "no material routing attempt"}
        for margin in ((12, 24, 48) if allow_bridge else (12, 24)):
            result = self.builder.route(source, destination, "transport-belt", reserved, margin=margin, **directions)
            if result.get("ok") or result.get("reason") != "no route within bounds":
                break
        if allow_bridge and not result.get("ok") and result.get("reason") in {"no route within bounds", "route search budget exhausted"}:
            bridge = self._belt_bridge_route(source, destination, reserved, **directions)
            if bridge.get("ok"):
                return bridge
        return result

    def _belt_bridge_route(self, source: dict, destination: dict, reserved: list[dict], **directions: Any) -> dict:
        from .deterministic_belt_crossings import plan_belt_crossings
        return plan_belt_crossings(self, source, destination, reserved, **directions)

    @staticmethod
    def _electric_source_plan(item: str, x: float, y: float) -> dict:
        drill = {"name": "electric-mining-drill", "position": {"x": x + .5, "y": y + .5}, "direction": 0, "_width": 3, "_height": 3}
        if item in {"iron-plate", "copper-plate"}:
            receiver = {"name": "stone-furnace", "position": {"x": x, "y": y - 2}, "direction": 0, "_width": 2, "_height": 2}
            entities = [drill, receiver,
                        {"name": "inserter", "position": {"x": x - 1.5, "y": y - 1.5}, "direction": 4},
                        {"name": "inserter", "position": {"x": x + 1.5, "y": y - 2.5}, "direction": 4}]
            entities += [{"name": "transport-belt", "position": {"x": x - n - .5, "y": y - 1.5}, "direction": 12} for n in (2, 3)]
            entities += [{"name": "transport-belt", "position": {"x": x + n + .5, "y": y - 2.5}, "direction": 12} for n in (2, 3)]
            entities += [{"name": "small-electric-pole", "position": {"x": x - 2.5, "y": y + .5}, "direction": 0},
                         {"name": "small-electric-pole", "position": {"x": x + 2.5, "y": y - .5}, "direction": 0}]
            ports = [{"kind": "item", "item": item, "direction": "output", "position": {"x": x - 3.5, "y": y - 1.5}, "facing": 12},
                     {"kind": "item", "item": "coal", "direction": "input", "position": {"x": x + 3.5, "y": y - 2.5}, "facing": 12}]
        else:
            receiver = {"name": "wooden-chest", "position": {"x": x + .5, "y": y - 1.5}, "direction": 0}
            entities = [drill, receiver, {"name": "inserter", "position": {"x": x + 1.5, "y": y - 1.5}, "direction": 12},
                        {"name": "small-electric-pole", "position": {"x": x + 2.5, "y": y + .5}, "direction": 0}]
            entities += [{"name": "transport-belt", "position": {"x": x + n + .5, "y": y - 1.5}, "direction": 4} for n in (2, 3)]
            ports = [{"kind": "item", "item": item, "direction": "output", "position": {"x": x + 3.5, "y": y - 1.5}, "facing": 4}]
        return _plan(entities, ports, resource_cell=True)

    def _raw_capacity_site(self, obs: dict, item: str, key: str) -> dict:
        if key in self.state["blocks"]:
            return self.state["blocks"][key]
        resource = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}.get(item, item)
        payload = json.dumps(resource)
        survey = self.game.query('''
local name=''' + payload + ''';local seen={};local sites={}
for _,ore in pairs(s.find_entities_filtered{position={0,0},radius=512,name=name,type="resource"}) do
 local x=math.floor(ore.position.x);local y=math.floor(ore.position.y);local key=x..","..y
 if not seen[key] then
  seen[key]=true
  if s.can_place_entity{name="electric-mining-drill",position={x=x+.5,y=y+.5},direction=0,force=f} then
   local amount=0;local count=0;local mixed=false
   for _,r in pairs(s.find_entities_filtered{area={{x-2,y-2},{x+3,y+3}},type="resource"}) do
    if r.name==name then amount=amount+r.amount;count=count+1 else mixed=true end
   end
   if not mixed and count>=4 then sites[#sites+1]={x=x,y=y,score=count*100000+math.min(amount,10000)-(x*x+y*y)} end
  end
 end
end
table.sort(sites,function(a,b) return a.score>b.score end)
local best={};for i=1,math.min(#sites,256) do best[i]=sites[i] end
return {ok=true,sites=best}
''')
        if not survey.get("ok"):
            return {"ok": False, "reason": "electric mining site survey failed", "error": survey.get("reason")}
        occupied = self.builder._occupied_by_plan(self._reserved())
        clearances = self._port_clearances()
        for site in survey.get("sites", []):
            plan = self._electric_source_plan(item, site["x"], site["y"])
            if self.builder._occupied_by_plan(plan["entities"]) & (occupied | clearances):
                continue
            approaches = set()
            for port in plan["ports"]:
                dx, dy = DIRECTIONS[port["facing"]]
                sign = -1 if port["direction"] == "input" else 1
                approaches.add((port["position"]["x"] + dx * sign, port["position"]["y"] + dy * sign))
            if approaches & (occupied | clearances):
                continue
            if not self.builder.can_place(plan["entities"]).get("ok"):
                continue
            return self.register_plan(key, plan, obs)
        return {"ok": False, "reason": "no clear dense electric mining cell site", "resource": resource}

    def _expand_raw_source(self, obs: dict, item: str, rate: float, primary_port: dict) -> dict:
        if rate <= 0:
            return _report("succeeded", "no additional raw supply requested", ports=[primary_port])
        if not (obs.get("enabled_recipes") or {}).get("electric-mining-drill"):
            return self.request_recipe_unlock(obs, "electric-mining-drill")
        resource = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}.get(item, item)
        mining_time = float(self.catalog.entities[resource]["mining_time"])
        electric_rate = float(self.catalog.entities["electric-mining-drill"]["mining_speed"]) * 60 / mining_time
        initial_rate = float(self.catalog.entities["burner-mining-drill"]["mining_speed"]) * 60 / mining_time
        if item in {"iron-plate", "copper-plate"}:
            smelting = self.catalog.recipe_for_product(item)
            furnace_rate = float(self.catalog.entities["stone-furnace"]["crafting_speed"]) * 60 / float(smelting["energy"])
            electric_rate, initial_rate = min(electric_rate, furnace_rate), min(initial_rate, furnace_rate)
        additional = max(0, math.ceil((rate - initial_rate) / electric_rate - 1e-9))
        coal_port = primary_port
        if item != "coal" and additional:
            coal = self.ensure_product(obs, "coal")
            if not _ready(coal):
                return coal
            coal_port = coal["evidence"]["ports"][0]
        for index in range(additional):
            key = f"source:{item}:capacity:{index}"
            plan = self._raw_capacity_site(obs, item, key)
            if not plan.get("ok"):
                return _report("blocked", plan["reason"], item=item, requested_rate_per_minute=rate)
            result = self.builder.ensure_plan(obs, plan)
            if not _ready(result):
                return result
            result = self.ensure_power_connection(obs, key, plan)
            if not _ready(result):
                return result
            for consumer in plan["ports"]:
                if consumer["direction"] != "input":
                    continue
                result = self.connect_input(obs, coal_port, consumer, key + ":fuel")
                if not _ready(result):
                    return result
                furnace = next(e for e in plan["entities"] if e["name"] == "stone-furnace")
                owned = self.state.setdefault("automated_burners", [])
                if self._entity_key(furnace) not in owned:
                    owned.append(self._entity_key(furnace))
                    self._save()
            result = self._merge_output(obs, plan["ports"][0], primary_port, key + ":output")
            if not _ready(result):
                return result
        return _report("succeeded", "raw mining and smelting capacity constructed with automatic fuel", ports=[primary_port],
                       additional_cells=additional, nominal_capacity_per_minute=initial_rate + additional * electric_rate,
                       requested_rate_per_minute=rate, flow_verified=False)

    def ensure_capacity(self, obs: dict, science_packs: list[str]) -> dict:
        """Expand physical producers for the active science rates from the graph."""
        self._sync(obs)
        targets = self.state.setdefault("capacity_science", [])
        if any(item not in targets for item in science_packs):
            targets.extend(item for item in science_packs if item not in targets)
            self._save()
        cycles, raw_rates = self.graph._continuous_rates(targets)
        requirements = {}
        for name, rate in cycles.items():
            recipe = self.catalog.recipes[name]
            for product in recipe["products"]:
                if product.get("type", "item") == "item":
                    requirements[product["name"]] = requirements.get(product["name"], 0) + float(product.get("amount", 1)) * float(product.get("probability", 1)) * rate
        requirements.update({name: rate for (kind, name), rate in raw_rates.items() if kind == "item" and name in {"stone", "coal"}})
        # Fuel is separate from recipe ingredients. Reserve coal for every
        # requested smelter plus the bootstrap drills using live prototype watts.
        coal_joules = float(getattr(self.catalog, "items", {}).get("coal", {}).get("fuel_value", 4000000) or 4000000)
        fuel = 0.0
        for name, rate in cycles.items():
            machines = self.graph.machines_for_recipe(name, obs)
            if machines and machines[0].get("burner"):
                active = rate * float(self.catalog.recipes[name]["energy"]) / float(machines[0]["crafting_speed"]) / 60
                fuel += active * float(machines[0].get("energy_usage_per_tick", 0)) * 3600 / coal_joules
        requirements["coal"] = max(requirements.get("coal", 0) + fuel, 20)
        ordered = sorted(requirements, key=lambda item: (item not in {"coal", "iron-plate", "copper-plate", "stone"}, item != "coal", item))
        for item in ordered:
            if item in {"iron-ore", "copper-ore"}:
                continue
            result = self.ensure_product(obs, item, rate_per_minute=requirements[item])
            if not _ready(result):
                return result
        return _report("succeeded", "active science producers have physical nominal capacity", science_rate_per_minute=self.graph.science_rate_per_minute,
                       requirements_per_minute=requirements, flow_verified=False)

    def _recover_unbuilt_link(self, obs: dict, source_port: dict, consumer_port: dict, link_key: str) -> dict | None:
        """Discard a contradictory old route only after a complete live survey.

        Earlier routing could revisit its source and overwrite the saved belt
        direction. Already constructed routes require explicit repair; only an
        entirely unbuilt link between independently reserved endpoints is safe
        to plan again without moving or rotating any entity.
        """
        plan = self.state["links"].get(link_key)
        if not plan:
            return None
        endpoints = {(p["position"]["x"], p["position"]["y"]): p.get("facing")
                     for p in (source_port, consumer_port)}
        contradictory = any(e["name"] == "transport-belt"
                            and (point := (e["position"]["x"], e["position"]["y"])) in endpoints
                            and endpoints[point] is not None and e.get("direction", 0) != endpoints[point]
                            for e in plan["entities"])
        if not contradictory:
            return None
        failure = _report("blocked", "contradictory material route cannot be safely replanned", link=link_key)
        if plan.get("source_port") != source_port or plan.get("consumer_port") != consumer_port:
            return failure
        if "tap:" + link_key in self.state["power_links"]:
            return failure
        other = self._reserved(exclude=link_key)
        stable = {(e["position"]["x"], e["position"]["y"]): e.get("direction", 0)
                  for e in other if e["name"] == "transport-belt"}
        if any(stable.get(point) != direction for point, direction in endpoints.items()):
            return failure
        interior = [e for e in plan["entities"] if (e["position"]["x"], e["position"]["y"]) not in endpoints]
        if self.builder._occupied_by_plan(interior) & self.builder._occupied_by_plan(other):
            return failure
        payload = json.dumps(json.dumps(plan["entities"], separators=(",", ":")))
        survey = self.game.query('''
local rows=helpers.json_to_table(''' + payload + ''');local found={}
for _,row in ipairs(rows) do
 local entities=s.find_entities_filtered{position=row.position,radius=.1,force=f}
 for _,e in pairs(entities) do
  found[#found+1]={name=e.name,position=pos(e.position),direction=e.direction}
 end
end
return {ok=true,checked=#rows,existing=found}
''')
        if not survey.get("ok") or survey.get("checked") != len(plan["entities"]) or "existing" not in survey:
            failure["evidence"]["query_error"] = survey.get("reason", "incomplete route survey")
            return failure
        observed = {}
        for entity in survey["existing"]:
            point = (entity["position"]["x"], entity["position"]["y"])
            if point not in endpoints or entity["name"] != "transport-belt" or entity.get("direction", 0) != endpoints[point]:
                failure["evidence"]["existing_entity"] = entity
                return failure
            observed[point] = entity
        if set(observed) != set(endpoints):
            return failure
        del self.state["links"][link_key]
        self.state["route_recoveries"] = (self.state.get("route_recoveries", []) + [{"link": link_key,
            "tick": obs.get("tick"), "reason": "unbuilt route contradicted stable endpoint directions",
            "checked_entities": len(plan["entities"]), "preserved_endpoints": len(observed)}])[-20:]
        self._save()
        return None

    def _consumer_drop_bridge_route(self, obs: dict, source: dict, consumer: dict,
                                    reserved: list[dict], *, start_direction: int | None,
                                    owned_plan_key: str | None = None) -> dict:
        """Feed an enclosed owned input using the live long-arm drop geometry."""
        destination, facing = consumer["position"], consumer.get("facing")
        actual = next((e for e in obs.get("entities", []) if e.get("name") == "transport-belt"
                       and e.get("position") == destination and e.get("direction") == facing), None)
        owned = any(consumer in plan.get("ports", []) and any(e.get("name") == "transport-belt"
                    and e.get("position") == destination and e.get("direction") == facing
                    for e in plan.get("entities", [])) for plan in self.state["blocks"].values())
        if owned_plan_key is not None:
            # Energy can join an intermediate belt only after proving its tail
            # reaches the intended bank. The named saved block must itself own
            # that exact facing belt and exclusively carry the requested item.
            owner = self.state["blocks"].get(owned_plan_key, {})
            ports = [p for p in owner.get("ports", []) if p.get("kind") == "item"]
            belts = [e for e in owner.get("entities", []) if e.get("name") == "transport-belt"
                     and e.get("position") == destination]
            owned = (bool(consumer.get("item")) and bool(ports) and bool(belts)
                     and all(p.get("item") == consumer["item"] for p in ports)
                     and all(e.get("direction", 0) == facing for e in belts))
        if (not owned or actual is None or type(actual.get("unit_number")) is not int or actual["unit_number"] < 1
                or facing not in DIRECTIONS or consumer.get("kind") != "item" or consumer.get("direction") != "input"
                or not obs.get("world_id") or obs["world_id"] != self.state.get("world_id")):
            return {"ok": False, "reason": "long-arm destination is not an observed owned input belt"}
        payload = json.dumps(json.dumps({"position": destination, "facing": facing, "item": consumer["item"],
            "unit": actual["unit_number"], "world": obs["world_id"]}, separators=(",", ":")))
        survey = self.game.query('''
--[[ owned_consumer_long_arm_drop: observation and live prototype geometry only. ]]
local x=helpers.json_to_table(''' + payload + ''');local belt=target(x.position,"transport-belt")
if not d or d.world_id~=x.world or not belt or belt.force~=f or belt.unit_number~=x.unit or belt.direction~=x.facing
 then return {ok=false,reason="long-arm input belt identity changed"} end
for lane=1,2 do for _,row in pairs(belt.get_transport_line(lane).get_contents()) do
 if row.name~=x.item and row.count>0 then return {ok=false,reason="long-arm input belt carries another material"} end
end end
local recipe=f.recipes["long-handed-inserter"];local proto=prototypes.entity["long-handed-inserter"]
if not recipe or not recipe.enabled then return {ok=false,reason="long inserter recipe is locked"} end
local function rotate(v,direction)
 local px,py=v.x or v[1],v.y or v[2]
 if direction==4 then return -py,px elseif direction==8 then return -px,-py elseif direction==12 then return py,-px end
 return px,py
end
local networks={};for _,e in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if e.energy>0 and e.electric_network_id then networks[e.electric_network_id]=true end
end
local poles={};for _,e in pairs(s.find_entities_filtered{force=f,name="small-electric-pole"}) do
 if networks[e.electric_network_id] then poles[#poles+1]=e end
end
table.sort(poles,function(a,b) return a.unit_number<b.unit_number end)
local rows={};local box=belt.bounding_box;local pickup_box=prototypes.entity["transport-belt"].collision_box
for _,direction in ipairs{0,4,8,12} do
 local px,py=rotate(proto.inserter_pickup_position,direction)
 local dx,dy=rotate(proto.inserter_drop_position,direction)
 local arm={name="long-handed-inserter",position={x=x.position.x-math.floor(dx+.5),y=x.position.y-math.floor(dy+.5)},direction=direction}
 local drop={x=arm.position.x+dx,y=arm.position.y+dy}
 local point={x=arm.position.x+px,y=arm.position.y+py}
 local pickup={x=math.floor(point.x)+.5,y=math.floor(point.y)+.5}
 local vx,vy=rotate({0,-1},direction)
 if px*vx+py*vy>0 and math.abs(px*vy-py*vx)<0.000001
  and drop.x>box.left_top.x and drop.x<box.right_bottom.x and drop.y>box.left_top.y and drop.y<box.right_bottom.y
  and point.x>pickup.x+pickup_box.left_top.x and point.x<pickup.x+pickup_box.right_bottom.x
  and point.y>pickup.y+pickup_box.left_top.y and point.y<pickup.y+pickup_box.right_bottom.y
  and s.can_place_entity{name=arm.name,position=arm.position,direction=direction,force=f} then
  local covered={};for _,pole in ipairs(poles) do
   local reach=pole.prototype.get_supply_area_distance(pole.quality)
   if math.abs(arm.position.x-pole.position.x)<=reach and math.abs(arm.position.y-pole.position.y)<=reach then
    covered[#covered+1]={name=pole.name,position=pos(pole.position),direction=pole.direction,unit_number=pole.unit_number}
   end
  end
  rows[#rows+1]={arm=arm,pickup=pickup,pickup_position=point,drop_position=drop,poles=covered}
 end
end
return {ok=true,candidates=rows,new_pole_reach=prototypes.entity["small-electric-pole"].get_supply_area_distance("normal")}
''')
        if not survey.get("ok"):
            return survey
        clearances = self._port_clearances()
        if start_direction in DIRECTIONS:
            dx, dy = DIRECTIONS[start_direction]
            clearances.discard((source["x"] + dx, source["y"] + dy))
        reserved = reserved + [{"name": "port-clearance", "position": {"x": x, "y": y}}
                               for x, y in clearances]
        foreign = [e for plan in self.state["links"].values()
                   if any((plan.get(field) or {}).get("item") not in (None, consumer["item"])
                          for field in ("source_port", "consumer_port")) for e in plan.get("entities", [])
                   if e["name"] == "transport-belt"]
        foreign_footprint = self.builder._occupied_by_plan(foreign)
        best = None
        for option in sorted(survey.get("candidates", []), key=lambda row: (_distance(source, row["pickup"]), row["arm"]["direction"]))[:4]:
            arm, pickup = option["arm"], option["pickup"]
            flow = (arm["direction"] + 8) % 16
            pickup_facing = start_direction if pickup == source and start_direction in DIRECTIONS else flow
            pickup_belt = {"name": "transport-belt", "position": pickup, "direction": pickup_facing}
            final_belt = {"name": "transport-belt", "position": destination, "direction": facing}
            equipment = [pickup_belt, arm, final_belt]
            existing_poles = option.get("poles") or []
            poles = existing_poles[:3] or self._intake_poles(arm["position"], equipment)[:8]
            for power in poles:
                pole = {key: power[key] for key in ("name", "position", "direction")}
                if not existing_poles and max(abs(pole["position"][axis] - arm["position"][axis]) for axis in ("x", "y")) > float(survey.get("new_pole_reach", 0)):
                    continue
                allowed = [final_belt] + ([pole] if existing_poles else []) + ([pickup_belt] if pickup == source else [])
                limited = [e for e in reserved if not any(e["name"] == expected["name"] and e["position"] == expected["position"]
                           and e.get("direction", 0) == expected["direction"] for expected in allowed)]
                trial = equipment + [pole]
                if self.builder._occupied_by_plan(trial) & (self.builder._occupied_by_plan(limited) | foreign_footprint):
                    continue
                if not self.builder.can_place(trial).get("ok"):
                    continue
                route = self._material_route(source, pickup, reserved + trial, allow_bridge=False,
                    start_direction=start_direction, end_direction=pickup_facing)
                if not route.get("ok"):
                    continue
                entities = [{"name": "transport-belt", **segment} for segment in route["segments"]] + [arm, pole, final_belt]
                if not self.builder.can_place(entities).get("ok"):
                    continue
                score = (len(entities) - bool(existing_poles), len(entities))
                if best is None or score < best[0]:
                    best = (score, {**route, "segments": entities, "flow_verified": False,
                        "consumer_drop": {"unit_number": actual["unit_number"], "pickup_position": option["pickup_position"],
                                          "drop_position": option["drop_position"], "pole_unit_number": power.get("unit_number")}})
                break
        return best[1] if best else {"ok": False, "reason": "no clear powered long-arm drop into owned input belt"}

    def _consumer_material_route(self, obs: dict, source: dict, consumer: dict,
                                 reserved: list[dict], *, start_direction: int | None) -> dict:
        """A verified input belt may accept a side feed without being rotated."""
        destination, facing = consumer["position"], consumer.get("facing")
        if facing not in DIRECTIONS:
            return self._material_route(source, destination, reserved, start_direction=start_direction)
        dx, dy = DIRECTIONS[facing]
        approach = (destination["x"] - dx, destination["y"] - dy)
        result = {"ok": False, "reason": "consumer belt approach is occupied"}
        if approach == (source["x"], source["y"]) or approach not in self.builder._occupied_by_plan(reserved):
            result = self._material_route(source, destination, reserved,
                                          start_direction=start_direction, end_direction=facing)
            if result.get("ok") or result.get("reason") not in {"no route within bounds", "route search budget exhausted"}:
                return result
        if not any(consumer in plan.get("ports", []) and any(
                e.get("name") == "transport-belt" and e.get("position") == destination
                and e.get("direction") == facing for e in plan.get("entities", []))
                for plan in self.state["blocks"].values()):
            return result
        actual = next((e for e in obs.get("entities", []) if e.get("name") == "transport-belt"
                       and e.get("position") == destination and e.get("direction") == facing), None)
        if actual is None or not actual.get("unit_number") or not obs.get("world_id"):
            return result
        payload = json.dumps(json.dumps({"position": destination, "facing": facing, "item": consumer["item"],
            "unit": actual["unit_number"], "world_id": obs["world_id"]}, separators=(",", ":")))
        proof = self.game.query('''
local x=helpers.json_to_table(''' + payload + ''');local e=target(x.position,"transport-belt")
if not d or d.world_id~=x.world_id or not e or e.force~=f or e.unit_number~=x.unit or e.direction~=x.facing
 then return {ok=false,reason="input belt identity changed"} end
for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents()) do
 if row.name~=x.item then return {ok=false,reason="input belt carries another material"} end
end end
return {ok=true,input_belt_verified=true}
''')
        if not proof.get("ok") or not proof.get("input_belt_verified"):
            return {"ok": False, "reason": proof.get("reason", "input belt identity was not verified")}
        front = {"name": "port-clearance", "position": {"x": destination["x"] + dx, "y": destination["y"] + dy}}
        route = self._material_route(source, destination, reserved + [front], allow_bridge=False,
                                     start_direction=start_direction)
        if not route.get("ok"):
            if route.get("reason") in {"no route within bounds", "route search budget exhausted"}:
                return self._consumer_drop_bridge_route(obs, source, consumer, reserved,
                                                        start_direction=start_direction)
            return route
        segments = deepcopy(route["segments"])
        if len(segments) < 2 or segments[-1]["position"] != destination:
            return {"ok": False, "reason": "side feed has no adjacent terminal belt"}
        previous = segments[-2]["position"]
        offset = (previous["x"] - destination["x"], previous["y"] - destination["y"])
        if offset not in {(-dx, -dy), (-dy, dx), (dy, -dx)}:
            return {"ok": False, "reason": "side feed would oppose the input belt"}
        segments[-1]["direction"] = facing
        entities = [{"name": "transport-belt", **segment} for segment in segments]
        if not self.builder.can_place(entities).get("ok"):
            return {"ok": False, "reason": "side feed placement changed"}
        return {**route, "segments": entities, "side_feed": True}

    def connect_input(self, obs: dict, source_port: dict, consumer_port: dict, link_key: str) -> dict:
        self._sync(obs)
        if source_port.get("item") != consumer_port.get("item") or source_port.get("kind") != consumer_port.get("kind"):
            return _report("blocked", "material ports are incompatible", source=source_port, consumer=consumer_port)
        if source_port.get("kind") != "item":
            return _report("blocked", "fluid ports require the fluid network router")
        recovery = self._recover_unbuilt_link(obs, source_port, consumer_port, link_key)
        if recovery is not None:
            return recovery
        if link_key not in self.state["links"]:
            source = source_port["position"]
            reused = [(key, p) for key, p in self.state["links"].items() if p.get("source_port") == source_port]
            tap_entities = []
            upstream_tap = None
            start_direction = source_port.get("facing")
            route = None
            if reused:
                # Independent inserter side-taps distribute one belt to multiple
                # consumers before splitter research, preserving the original route.
                candidate_belts = [(key, e) for key, p in reused for e in p["entities"] if e["name"] == "transport-belt"]
                # Extend the existing network nearest the new consumer. Always
                # branching next to the original source needlessly crosses its
                # earlier supply corridors and can trap distant consumers.
                candidate_belts.sort(key=lambda row: (_distance(row[1]["position"], consumer_port["position"]),
                                                      _distance(row[1]["position"], source)))
                reserved = self.builder._occupied_by_plan(self._reserved())
                for parent_key, belt in candidate_belts[:64]:
                    for side in ((belt.get("direction", 0) + 4) % 16, (belt.get("direction", 0) + 12) % 16):
                        dx, dy = DIRECTIONS[side]
                        bp = belt["position"]
                        inserter = {"name": "inserter", "position": {"x": bp["x"] + dx, "y": bp["y"] + dy}, "direction": (side + 8) % 16}
                        new_belt = {"name": "transport-belt", "position": {"x": bp["x"] + dx * 2, "y": bp["y"] + dy * 2}, "direction": side}
                        pole = {"name": "small-electric-pole", "position": {"x": bp["x"] + dx - dy, "y": bp["y"] + dy + dx}, "direction": 0}
                        trial = [inserter, new_belt, pole]
                        if self.builder._occupied_by_plan(trial) & reserved:
                            continue
                        if self.builder.can_place(trial).get("ok"):
                            attempt = self._consumer_material_route(obs, new_belt["position"], consumer_port, self._reserved() + trial,
                                                                    start_direction=side)
                            if attempt.get("ok"):
                                tap_entities, source, start_direction, route = trial, new_belt["position"], side, attempt
                                upstream_tap = {"link_key": parent_key, "belt": deepcopy(belt)}
                                break
                    if tap_entities:
                        break
                if not tap_entities:
                    return _report("blocked", "no clear inserter distribution tap from producer belt", link=link_key)
            if route is None:
                route = self._consumer_material_route(obs, source, consumer_port, self._reserved() + tap_entities,
                                                      start_direction=start_direction)
            if not route.get("ok"):
                return _report("blocked", "material route is obstructed", link=link_key, query_error=route.get("reason"))
            entities = tap_entities + [{"name": "transport-belt", **segment} for segment in route["segments"]]
            unique = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in entities}
            plan = _plan(list(unique.values()), source_port=source_port, consumer_port=consumer_port)
            if upstream_tap is not None:
                plan["upstream_tap"] = upstream_tap
            self.state["links"][link_key] = plan
            self._save()
        plan = self.state["links"][link_key]
        from .deterministic_input_links import ensure_input_dependencies
        dependency = ensure_input_dependencies(self, obs, source_port, link_key)
        if not _ready(dependency):
            return dependency
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        poles = [e for e in plan["entities"] if e["name"] == "small-electric-pole"]
        for pole in poles:
            power_key, power_plan = "tap:" + link_key, plan
            if len(poles) > 1:
                x, y = pole["position"]["x"], pole["position"]["y"]
                power_key += f":pole:{x:g},{y:g}"
                power_plan = {**plan, "entities": [deepcopy(pole)]}
            result = self.ensure_power_connection(obs, power_key, power_plan)
            if not _ready(result):
                return result
        return _report("succeeded", "producer-to-consumer belt connection observed", flow_verified=False, link=link_key)

    def _lab_plan(self, obs: dict) -> dict:
        packs = list(self.graph.for_first_rocket()["bom"]["science_packs"])
        packs.sort(key=lambda item: (item != "automation-science-pack", item != "logistic-science-pack", item))
        key = "research:labs"
        if key not in self.state["blocks"]:
            existing = next((e for e in obs.get("entities", []) if e.get("name") == "lab"), None)
            if existing:
                plan = build_template("labs_row", inputs=packs, anchor=existing["position"])
                if self.builder.can_place(plan.get("entities", [])).get("ok"):
                    self.state["blocks"][key] = plan
                    self._save()
        return self.reserve_site(build_template("labs_row", inputs=packs), key, obs)

    def _ensure_lab(self, obs: dict) -> dict:
        plan = self._lab_plan(obs)
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "no lab site"))
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        return self.ensure_power_connection(obs, "research:labs", plan)

    def ensure_lab_capacity(self, obs: dict, technology: dict) -> dict:
        """Match laboratory consumption to the actual research duration in ticks."""
        primary = self.state.get("blocks", {}).get("research:labs")
        if not primary:
            return _report("blocked", "primary research laboratory is not reserved")
        timing = self.game.query('''
return {ok=true,speed=prototypes.entity.lab.get_researching_speed(),
 bonus=f.laboratory_speed_modifier,drain=prototypes.entity.lab.science_pack_drain_rate_percent}
''')
        duration = float(technology.get("unit_energy") or 0)
        speed = float(timing.get("speed") or 0) * (1 + float(timing.get("bonus") or 0))
        drain = float(timing.get("drain") or 0) / 100
        amounts = [float(row["amount"]) for row in technology.get("ingredients", [])]
        if (not timing.get("ok") or not amounts or min(amounts) <= 0
                or not all(math.isfinite(value) and value > 0 for value in (duration, speed, drain))):
            return _report("blocked", "live laboratory research timing is unavailable")
        per_lab = 3600 * speed * drain * min(amounts) / duration
        required = max(1, math.ceil(self.graph.science_rate_per_minute / per_lab - 1e-9))
        existing = [key for key in self.state["blocks"] if key.startswith("research:lab-capacity:")]
        desired = max(required, 1 + len(existing))
        packs = [port["item"] for port in primary["ports"] if port["kind"] == "item"]
        reference = next(entity["position"] for entity in primary["entities"] if entity["name"] == "lab")
        sources = {}
        for ingredient in technology["ingredients"]:
            item = ingredient["name"]
            source = self.ensure_product(obs, item)
            if not _ready(source):
                return source
            sources[item] = next(port for port in source["evidence"]["ports"] if port["item"] == item)
        for index in range(1, desired):
            key = f"research:lab-capacity:{index}"
            plan = self.reserve_site(build_template("labs_row", inputs=packs), key, obs, reference)
            if not plan.get("ok"):
                return _report("blocked", plan.get("reason", "laboratory expansion site unavailable"), required_labs=desired)
            result = self.builder.ensure_plan(obs, plan)
            if not _ready(result):
                return result
            result = self.ensure_power_connection(obs, key, plan)
            if not _ready(result):
                return result
            for consumer in plan["ports"]:
                if consumer["kind"] == "item" and consumer["item"] in sources:
                    result = self.connect_input(obs, sources[consumer["item"]], consumer, key + ":" + consumer["item"])
                    if not _ready(result):
                        return result
        self.state["laboratory_capacity"] = {"constructed_labs": desired, "required_labs": required,
            "nominal_consumption_per_minute": desired * per_lab, "unit_energy_ticks": duration,
            "flow_verified": False}
        self._save()
        return _report("succeeded", "laboratories connected for the active research duration", **self.state["laboratory_capacity"])

    def _bootstrap_automation(self, obs: dict) -> dict:
        result = self._ensure_lab(obs)
        if not _ready(result):
            return result
        technology = self.catalog.technologies["automation"]
        if obs.get("research") != "automation":
            return {"type": "research", "technology": "automation", "reason": "research initial assembler capability"}
        labs = [e for e in obs.get("entities", []) if e.get("name") == "lab"]
        for ingredient in technology["ingredients"]:
            item = ingredient["name"]
            required = math.ceil(float(technology["unit_count"]) * float(ingredient["amount"]))
            current_produced = int((obs.get("production") or {}).get(item, {}).get("produced", 0))
            baseline = self.state.setdefault("bootstrap_science", {}).get(item)
            if baseline is None:
                baseline = {"produced": current_produced,
                            "allowance": max(0, required - int(obs.get("inventory", {}).get(item, 0))
                                             - sum(int(e.get("inventory", {}).get(item, 0)) for e in labs))}
                self.state["bootstrap_science"][item] = baseline
                self._save()
            held = int(obs.get("inventory", {}).get(item, 0))
            if held:
                return {"type": "insert", "name": "lab", "position": labs[0]["position"], "item": item,
                        "count": min(held, required), "inventory": "lab_input", "reason": "one-off science seed for Automation research"}
            produced = max(0, current_produced - baseline["produced"])
            if produced < baseline["allowance"]:
                return self.bootstrap.ensure_item(obs, item, baseline["allowance"] - produced)
        return _report("waiting", "initial Automation research consumes its bounded science batch",
                       research_progress=obs.get("research_progress", 0))

    def bootstrap_electric_mining(self, obs: dict) -> dict | None:
        """A finite, streamed research bridge before financing the full mall.

        The ledger debits science crafts before they leave this method. It is
        retained across restart, save rollback and catalog changes in this world.
        Missing packets after that finite budget must come from automatic
        production, never another handcraft allowance.
        """
        self._sync(obs)
        name, item = "electric-mining-drill", "automation-science-pack"
        if (obs.get("technologies") or {}).get(name):
            budget = self.state.get("startup_research", {}).get(name)
            if budget is not None and "completion_tick" not in budget:
                production = (obs.get("production") or {}).get(item, {})
                self.state["flow_samples"][item] = {"produced": int(production.get("produced", 0)),
                                                     "consumed": int(production.get("consumed", 0))}
                budget["completion_tick"] = int(obs.get("tick") or 0)
                self._save()
            return None
        if not (obs.get("technologies") or {}).get("automation"):
            return _report("waiting", "initial Automation research precedes the electric mining bridge")
        technology = self.catalog.technologies.get(name) or {}
        recipe = self.catalog.recipe_for_product(item)
        try:
            units = float(technology["unit_count"])
            duration = float(technology["unit_energy"])
            ingredients = technology["ingredients"]
            prerequisites = technology["prerequisites"]
            products = recipe["products"] if recipe else []
            valid = (not isinstance(technology["unit_count"], bool) and math.isfinite(units) and units.is_integer() and 0 < units <= 25
                     and math.isfinite(duration) and duration > 0 and not technology.get("research_trigger")
                     and isinstance(prerequisites, list) and all(isinstance(p, str) for p in prerequisites)
                     and len(ingredients) == 1 and ingredients[0]["name"] == item
                     and ingredients[0].get("type", "item") == "item" and float(ingredients[0]["amount"]) == 1
                     and len(products) == 1 and products[0]["name"] == item
                     and products[0].get("type", "item") == "item" and float(products[0]["amount"]) == 1
                     and float(products[0].get("probability", 1)) == 1)
        except (KeyError, TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            return _report("blocked", "unsupported electric mining startup research data", technology=name, maximum_handcraft_packs=25)
        if any(not (obs.get("technologies") or {}).get(parent) for parent in prerequisites):
            return _report("blocked", "electric mining research prerequisite is not observed", prerequisites=prerequisites)
        progress = float(obs.get("research_progress") or 0) if obs.get("research") == name else 0.0
        if not math.isfinite(progress) or not 0 <= progress <= 1:
            return _report("blocked", "invalid live startup research progress", technology=name)
        labs = [e for e in obs.get("entities", []) if e.get("name") == "lab"]
        held = int((obs.get("inventory") or {}).get(item, 0))
        lab_stock = sum(int((e.get("inventory") or {}).get(item, 0)) for e in labs)
        queued = sum(int(row.get("count", 0)) for row in obs.get("crafting_queue") or []
                     if row.get("recipe") == recipe["name"])
        remaining_units = math.ceil(units * (1 - progress) - 1e-9)
        current_produced = int(((obs.get("production") or {}).get(item) or {}).get("produced", 0))
        budgets = self.state.setdefault("startup_research", {})
        if name not in budgets:
            budgets[name] = {"required_packs": int(units), "recipe": recipe["name"],
                             "allowance": max(0, remaining_units - held - lab_stock - queued), "issued": 0,
                             "baseline_produced": current_produced, "produced_high_water": current_produced,
                             "pending_issued": 0, "initial_queue_pending": queued, "external_produced": 0,
                             "credited_held": held, "credited_lab": lab_stock, "credited_queue": queued,
                             "initial_progress": progress}
            self._save()
        budget = budgets[name]
        if budget["required_packs"] != int(units) or budget["recipe"] != recipe["name"]:
            return _report("blocked", "startup research requirements changed after allowance was issued", technology=name)
        if current_produced > budget["produced_high_water"]:
            delta = current_produced - budget["produced_high_water"]
            initial = min(delta, budget["initial_queue_pending"])
            budget["initial_queue_pending"] -= initial
            delta -= initial
            issued = min(delta, budget["pending_issued"])
            budget["pending_issued"] -= issued
            budget["external_produced"] += delta - issued
            budget["produced_high_water"] = current_produced
            self._save()
        produced = max(0, budget["produced_high_water"] - budget["baseline_produced"] - budget["credited_queue"])
        unused = max(0, budget["allowance"] - budget["issued"] - budget["external_produced"])
        evidence = {"technology": name, "allowance": budget["allowance"], "issued": budget["issued"],
                    "produced_credit": produced, "remaining_allowance": unused, "research_progress": progress}
        result = self._ensure_lab(obs)
        if not _ready(result):
            return result
        if not labs:
            return _report("waiting", "waiting for an observed laboratory for startup science", **evidence)
        if obs.get("research") != name:
            return {"type": "research", "technology": name, "reason": "unlock electric raw production before constructing the full mall"}
        if held and remaining_units > lab_stock and labs:
            return {"type": "insert", "name": "lab", "position": labs[0]["position"], "item": item,
                    "count": min(5, held, remaining_units - lab_stock), "inventory": "lab_input",
                    "reason": "stream finite startup science into natural electric mining research"}
        needed = max(0, remaining_units - held - lab_stock - queued)
        if needed and unused:
            chunk = min(5, unused, needed)
            action = self.bootstrap.ensure_item(obs, item, chunk)
            if action.get("type") == "craft" and action.get("recipe") == recipe["name"]:
                count = action.get("count")
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= chunk:
                    return _report("blocked", "startup science craft exceeds its finite chunk allowance", **evidence)
                budget["issued"] += count
                budget["pending_issued"] += count
                self._save()
            if not _ready(action):
                return action
        automatic_required = unused == 0 and needed > 0 and held + lab_stock + queued == 0
        return _report("waiting", "finite startup science awaits observed electric mining research",
                       **evidence, automatic_science_required=automatic_required)

    def _ensure_startup_iron(self, obs: dict) -> dict:
        if not (obs.get("enabled_recipes") or {}).get("electric-mining-drill"):
            return _report("blocked", "researched electric mining recipe is not enabled")
        result = self.ensure_product(obs, "iron-plate", rate_per_minute=60)
        if _ready(result):
            self.state["startup_iron_capacity"] = {"requested_rate_per_minute": 60,
                "nominal_capacity_per_minute": result.get("evidence", {}).get("nominal_capacity_per_minute"),
                "observed_tick": obs.get("tick"), "flow_verified": False}
            self._save()
        return result

    def next_action(self, obs: dict) -> dict:
        self._sync(obs)
        from .deterministic_construction_buffer import ensure_construction_buffer
        buffer_action = ensure_construction_buffer(self, obs)
        if buffer_action is not None:
            return buffer_action
        if not (obs.get("technologies") or {}).get("automation"):
            return self._bootstrap_automation(obs)
        startup = self.bootstrap_electric_mining(obs)
        if startup is not None and not startup.get("evidence", {}).get("automatic_science_required"):
            return startup
        if startup is None:
            capacity = self._ensure_startup_iron(obs)
            if not _ready(capacity):
                return capacity
        for item in self.graph.for_first_rocket()["bom"]["science_packs"]:
            production = (obs.get("production") or {}).get(item, {})
            if item not in self.state["flow_samples"]:
                self.state["flow_samples"][item] = {"produced": int(production.get("produced", 0)),
                                                   "consumed": int(production.get("consumed", 0))}
                self._save()
        # Establish construction-belt production before extending the science network.
        for item in ("transport-belt", "automation-science-pack"):
            result = self.ensure_product(obs, item)
            if not _ready(result):
                return result
        result = self._ensure_lab(obs)
        if not _ready(result):
            return result
        next_research = self.graph.next_research(obs)
        if not (obs.get("technologies") or {}).get("logistics"):
            next_research = {"technology": "logistics", "kind": "research"}
        done = {name for name, researched in (obs.get("technologies") or {}).items() if researched}
        priorities = self.priority_research + self.state.get("capability_research", [])
        if startup is not None:
            priorities = ["electric-mining-drill", *priorities]
        if "electric-mining-drill" in self.catalog.technologies:
            priorities = priorities + ["electric-mining-drill"]
        for requested in priorities:
            if requested not in self.catalog.technologies or requested in done:
                continue
            for name in self.catalog.technology_order([requested], include_researched=True):
                technology = self.catalog.technologies[name]
                if name not in done and all(parent in done for parent in technology["prerequisites"]):
                    next_research = {"technology": name, "kind": "research"}
                    break
            else:
                continue
            break
        if next_research is None:
            return _report("succeeded", "all first-rocket research technologies are observed complete")
        technology_name = next_research["technology"]
        technology = self.catalog.technologies[technology_name]
        if technology.get("research_trigger"):
            trigger = technology["research_trigger"]
            if trigger.get("type") == "craft-item":
                item = trigger["item"]
                result = self.ensure_product(obs, item["name"] if isinstance(item, dict) else item)
                return result if not _ready(result) else _report("waiting", "waiting for natural production research trigger",
                                                               technology=technology_name, trigger=trigger)
            if trigger.get("type") in {"craft-fluid", "mine-entity"}:
                if self.fluids is None:
                    return _report("blocked", "natural fluid research trigger needs fluid production", technology=technology_name)
                if trigger["type"] == "craft-fluid":
                    fluid = trigger.get("fluid")
                    product = fluid["name"] if isinstance(fluid, dict) else fluid
                else:
                    entities = trigger.get("entities") or [trigger.get("entity")]
                    product = next((name for name in entities if name == "crude-oil"), None)
                if product:
                    result = self.fluids.ensure_source(obs, product)
                    return result if not _ready(result) else _report("waiting", "waiting for natural fluid production research trigger",
                                                                   technology=technology_name, trigger=trigger)
            return _report("blocked", "natural research trigger needs a dedicated producer", technology=technology_name, trigger=trigger)
        lab = self.state["blocks"]["research:labs"]
        for ingredient in technology["ingredients"]:
            item = ingredient["name"]
            source = self.ensure_product(obs, item)
            if not _ready(source):
                return source
            consumer = next((p for p in lab["ports"] if p["kind"] == "item" and p["item"] == item), None)
            if consumer is None:
                return _report("blocked", "research science has no lab input port", item=item)
            result = self.connect_input(obs, source["evidence"]["ports"][0], consumer, "lab:" + item)
            if not _ready(result):
                return result
        if obs.get("research") != technology_name:
            return {"type": "research", "technology": technology_name, "reason": "advance catalog research with automatic science feed"}
        if (obs.get("enabled_recipes") or {}).get("electric-mining-drill"):
            capacity = self.ensure_capacity(obs, [row["name"] for row in technology["ingredients"]])
            if not _ready(capacity):
                return capacity
            capacity = self.ensure_lab_capacity(obs, technology)
            if not _ready(capacity):
                return capacity
        return _report("waiting", "automatic science production and lab consumption running",
                       technology=technology_name, research_progress=obs.get("research_progress", 0), input_handcarry=False,
                       science_flow=self.flow_evidence(obs))

    def flow_evidence(self, obs: dict) -> dict:
        result = {}
        for item, baseline in self.state.get("flow_samples", {}).items():
            current = (obs.get("production") or {}).get(item, {})
            produced = int(current.get("produced", 0)) - baseline["produced"]
            consumed = int(current.get("consumed", 0)) - baseline["consumed"]
            result[item] = {"produced_since_automation": max(0, produced), "consumed_since_automation": max(0, consumed),
                            "production_and_consumption_verified": produced > 0 and consumed > 0}
        return result
