"""Observation-driven, resource-backed block construction and starter power.

Plans persist per world, but construction and completion are always reobserved.
The builder returns one ordinary game action or an explicit status report. It
never grants items, researches technologies, or treats placement as production.
"""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from typing import Any

from .deterministic_game import BUILD_BATCH_LIMIT, BUILD_BATCH_NAMES
from .deterministic_state import _atomic_json, stop_requested
from .factory_templates import build_template, route_orthogonal, DIRECTIONS


def _report(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def power_flow_ready(evidence: dict[str, Any]) -> bool:
    return bool(evidence.get("ok") and evidence.get("water", 0) > 0 and evidence.get("steam", 0) > 0
                and evidence.get("boiler_fuel", 0) > 0 and evidence.get("drill_fuel", 0) > 0
                and evidence.get("coal_on_belts", 0) > 0 and evidence.get("connected_engines") == 2)


def _position(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 3), "y": round(y, 3)}


def _same_position(a: dict, b: dict) -> bool:
    return abs(a["x"] - b["x"]) < .2 and abs(a["y"] - b["y"]) < .2


def _find(observation: dict, entity: dict) -> dict | None:
    return next((e for e in observation.get("entities", [])
                 if e.get("name") == entity["name"] and _same_position(e["position"], entity["position"])), None)


def _direction_matches(name: str, actual: int, planned: int) -> bool:
    # This square turret's facing does not change its ammunition intake geometry.
    if name == "gun-turret":
        return True
    # These generators have two_direction_only in the real prototype: the game
    # normalizes south to north and west to east without changing fluid geometry.
    if name in {"steam-engine", "steam-turbine"}:
        return actual % 8 == planned % 8
    return actual == planned


def plan_observed(observation: dict, plan: dict) -> bool:
    """Check integrity without issuing construction, recipe, or fuel actions."""
    if not plan.get("ok") or not plan.get("entities"):
        return False
    for entity in plan["entities"]:
        found = _find(observation, entity)
        if found is None or (entity.get("recipe") and found.get("recipe") != entity["recipe"]):
            return False
        if (entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"}
                and not _direction_matches(entity["name"], found.get("direction", 0), entity.get("direction", 0))):
            return False
    return True


def _rotate(x: float, y: float, direction: int) -> tuple[float, float]:
    for _ in range(direction // 4):
        x, y = -y, x
    return x, y


def _plan(entities: list[dict], ports: list[dict], **extra: Any) -> dict:
    unique = {}
    for entity in entities:
        p = entity["position"]
        unique[(entity["name"], p["x"], p["y"])] = entity
    entities = list(unique.values())
    return {"ok": True, "reason": "", "entities": entities, "ports": ports,
            "required_items": dict(Counter(e.get("item") or e["name"] for e in entities)), **extra}


class FactoryBuilder:
    def __init__(self, game: Any, bootstrap: Any, catalog: Any):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.construction_materials: Any = None
        self.path = Path(game.cfg.runtime_dir) / "factory-builder.json"
        self.state: dict[str, Any] = {}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("schema_version") != 1:
                raise ValueError("unsupported factory builder checkpoint")

    def _sync(self, observation: dict) -> None:
        world_id = observation.get("world_id")
        if not world_id:
            raise ValueError("builder observation requires world_id")
        if self.state.get("world_id") != world_id:
            self.state = {"schema_version": 1, "world_id": world_id, "seeds": {}}
            self._save()
        tick = int(observation.get("tick", 0))
        fingerprint = getattr(self.catalog, "fingerprint", None)
        catalog_changed = ("catalog_fingerprint" in self.state
                           and self.state["catalog_fingerprint"] != fingerprint)
        if tick < self.state.get("last_tick", 0) or catalog_changed:
            self.state.pop("power_sample_tick", None)
            self.state.pop("power_verified_once", None)
            self.state["seeds"] = {}
        self.state["catalog_fingerprint"] = fingerprint
        self.state["last_tick"] = tick

    def _save(self) -> None:
        _atomic_json(self.path, self.state)

    def _placement_item(self, name: str) -> str:
        rows = getattr(self.catalog, "entities", {}).get(name, {}).get("items_to_place_this") or []
        return str(rows[0]["name"]) if rows else name

    def owns_automated_burner(self, entity: dict) -> bool:
        """Exclude these burners from repeated bootstrap inventory hand-feeding."""
        coal = self.state.get("coal_plan", {}).get("drill")
        if coal and entity.get("name") == coal["name"] and _same_position(entity["position"], coal["position"]):
            return True
        return any(e["name"] == "boiler" and entity.get("name") == "boiler"
                   and _same_position(entity["position"], e["position"])
                   for e in self.state.get("power_plan", {}).get("entities", []))

    def ensure_plan(self, observation: dict, plan: dict) -> dict:
        """Return one material/move/build/recipe action, or a construction status.
        A succeeded result means the listed entities and recipes are observed; ports
        must still be connected and operating flow verified by the caller.
        """
        self._sync(observation)
        if not plan.get("ok") or not plan.get("entities"):
            return _report("blocked", plan.get("reason") or "empty or invalid block plan")
        # A powered drill can spill ore into an unfinished receiver footprint.
        # This also applies to persisted cells reserved by an older version.
        entities = (sorted(plan["entities"], key=lambda row: row["name"] == "electric-mining-drill")
                    if plan.get("resource_cell") else plan["entities"])
        for index, entity in enumerate(entities):
            existing = _find(observation, entity)
            if existing is not None:
                direction_matches = _direction_matches(entity["name"], existing.get("direction", 0), entity.get("direction", 0))
                if entity.get("recipe") and (existing.get("recipe") != entity["recipe"] or not direction_matches):
                    if entity["recipe"] not in observation.get("enabled_recipes", {}):
                        return _report("blocked", "block recipe is locked", recipe=entity["recipe"])
                    move = self._move(observation, entity["position"])
                    return move or {"type": "recipe", "name": entity["name"], "position": entity["position"],
                                    "recipe": entity["recipe"], "direction": entity.get("direction", 0)}
                if entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"} and not direction_matches:
                    return _report("blocked", "existing entity direction differs from reserved plan", entity=entity)
                continue
            item = entity.get("item") or self._placement_item(entity["name"])
            if int(observation.get("inventory", {}).get(item, 0)) < 1:
                entity_type = self.catalog.entities.get(entity["name"], {}).get("type", entity["name"])
                infrastructure = {"transport-belt", "underground-belt", "splitter", "pipe", "pipe-to-ground",
                                  "electric-pole", "wall", "gate"}
                cap = (32 if entity_type in infrastructure else 8 if entity_type == "inserter"
                       else 4 if entity_type in {"container", "logistic-container"} else 2)
                missing = set()
                for wanted in plan["entities"]:
                    placement_item = wanted.get("item") or self._placement_item(wanted["name"])
                    if placement_item == item and _find(observation, wanted) is None:
                        p = wanted["position"]
                        missing.add((wanted["name"], p["x"], p["y"]))
                result = self.bootstrap.ensure_item(observation, item, min(cap, len(missing)))
                if self.construction_materials is not None:
                    return self.construction_materials.ensure(observation, result)
                return result
            placement = self.can_place([entity])
            if not placement.get("ok"):
                if self.game.backend == "character":
                    from .deterministic_navigation import BUILD_FOOTPRINT_LUA
                    encoded = json.dumps(json.dumps(entity, separators=(",", ":")))
                    obstruction = self.game.query(BUILD_FOOTPRINT_LUA + '''
local x=helpers.json_to_table(''' + encoded + ''')
local left,top,right,bottom=build_box(x)
local own_actor=false;local other=false
for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
 if e==a then own_actor=true elseif e.type~="resource" then other=true end
end
local terrain=s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
 build_check_type=defines.build_check_type.script,forced=false}
return {ok=true,only_actor=own_actor and not other and terrain}
''')
                    if obstruction.get("ok") and obstruction.get("only_actor"):
                        return {"type": "build", "name": entity["name"], "item": item,
                                "position": entity["position"], "direction": entity.get("direction", 0),
                                "reason": "step outside the reserved build footprint before placement"}
                recovery = self._recover_resource_cell_obstruction(observation, plan, entity)
                if recovery is not None:
                    return recovery
                return _report("blocked", "reserved block placement is obstructed", blockers=placement.get("blocked"), entity=entity)
            # New character builds use the navigator's live build reach. A
            # fixed approach radius can send an actor back into the footprint
            # after belts carry it away from the chosen standing position.
            if self.game.backend == "assisted" and entity["name"] in BUILD_BATCH_NAMES:
                batch = self._affordable_builds(observation, entities[index:])
                if len(batch) > 1:
                    return {"type": "build_many", "actions": batch}
            return {"type": "build", "name": entity["name"], "item": item,
                    "position": entity["position"], "direction": entity.get("direction", 0)}
        return _report("succeeded", "block entities and recipes observed", constructed=len(plan["entities"]),
                       ports=plan.get("ports", []), flow_verified=False)

    def _recover_resource_cell_obstruction(self, observation: dict, plan: dict, entity: dict) -> dict | None:
        """Recover a partial cell through ordinary mining and conserved pickup."""
        if not plan.get("resource_cell") or entity["name"] not in {"stone-furnace", "wooden-chest"}:
            return None
        drills = [row for row in plan["entities"] if row["name"] == "electric-mining-drill"]
        if len(drills) != 1:
            return None
        observed = _find(observation, drills[0])
        payload = json.dumps(json.dumps({"receiver": entity, "drill": drills[0]}, separators=(",", ":")))
        survey = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local x=args.receiver
if target(x.position,x.name) then return {ok=false} end
local box=prototypes.entity[x.name].collision_box
local function rotate(x,y)
 local direction=args.receiver.direction or 0
 if direction==4 then return -y,x elseif direction==8 then return -x,-y elseif direction==12 then return y,-x end
 return x,y
end
local x1,y1=rotate(box.left_top.x,box.left_top.y);local x2,y2=rotate(box.right_bottom.x,box.right_bottom.y)
local left,right=x.position.x+math.min(x1,x2),x.position.x+math.max(x1,x2)
local top,bottom=x.position.y+math.min(y1,y2),x.position.y+math.max(y1,y2)
local ground={}
for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
 if e~=a and e.type~="resource" then
  if e.type~="item-entity" or not e.stack.valid_for_read or e.stack.prototype.type~="item" then return {ok=false} end
  ground[#ground+1]={name=e.name,position=pos(e.position),item=e.stack.name,quality=e.stack.quality.name,count=e.stack.count}
 end
end
if #ground==0 or not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,
 build_check_type=defines.build_check_type.manual_ghost,forced=true} then return {ok=false} end
local drill=target(args.drill.position,args.drill.name);local emitter=nil
if drill then
 local p=drill.drop_position
 if drill.force~=f or not drill.minable or drill.direction~=(args.drill.direction or 0)
  or p.x<left or p.x>right or p.y<top or p.y>bottom then return {ok=false} end
 emitter={unit_number=drill.unit_number}
end
return {ok=true,world_id=d and d.world_id,emitter=emitter,ground=ground}
''')
        if not survey.get("ok") or survey.get("world_id") != observation["world_id"]:
            return None
        emitter = survey.get("emitter")
        if emitter:
            unit = emitter.get("unit_number")
            if not observed or not unit or observed.get("unit_number") != unit:
                return None
            return {"type": "mine", "name": drills[0]["name"], "position": drills[0]["position"], "count": 1,
                    "expected_entity_unit": unit, "expected_entity_world_id": observation["world_id"],
                    "reason": "recover the owned electric drill until its reserved receiver is built"}
        if observed:
            return None
        ground = sorted(survey.get("ground") or [], key=lambda row: (row["position"]["x"], row["position"]["y"], row["item"], row["quality"]))
        if not ground:
            return None
        row = ground[0]
        return {"type": "take", "name": row["name"], "position": row["position"], "item": row["item"],
                "quality": row["quality"], "count": min(50, row["count"]),
                "reason": "collect spilled ore before constructing the reserved resource receiver"}

    def _affordable_builds(self, observation: dict, entities: list[dict]) -> list[dict]:
        """Take a bounded infrastructure prefix without skipping required work."""
        stock = Counter(observation.get("inventory", {}))
        rows, seen = [], {}
        for entity in entities:
            existing = _find(observation, entity)
            if existing is not None:
                direction_matches = _direction_matches(entity["name"], existing.get("direction", 0), entity.get("direction", 0))
                if (entity.get("recipe") and (existing.get("recipe") != entity["recipe"] or not direction_matches)
                        or entity["name"] not in {"pipe", "small-electric-pole", "wooden-chest"} and not direction_matches):
                    break
                continue
            if entity["name"] not in BUILD_BATCH_NAMES or entity.get("recipe"):
                break
            p = entity["position"]
            key = entity["name"], p["x"], p["y"]
            if key in seen:
                if entity["name"] != "small-electric-pole" and seen[key] != entity.get("direction", 0):
                    break
                continue
            item = entity.get("item") or self._placement_item(entity["name"])
            if stock[item] < 1:
                break
            stock[item] -= 1
            seen[key] = entity.get("direction", 0)
            rows.append({"type": "build", "name": entity["name"], "item": item,
                         "position": p, "direction": entity.get("direction", 0)})
            if len(rows) == BUILD_BATCH_LIMIT:
                break
        return rows

    def _move(self, observation: dict, position: dict) -> dict | None:
        if self.game.backend != "character":
            return None
        actor = observation.get("position") or {"x": 0, "y": 0}
        if math.hypot(actor["x"] - position["x"], actor["y"] - position["y"]) > 4:
            return {"type": "move", "position": position}
        return None

    def can_place(self, entities: list[dict]) -> dict:
        if stop_requested(self.path.parent / "stop.json"):
            raise InterruptedError("operator_stop_requested")
        payload = json.dumps(json.dumps(entities, separators=(",", ":")))
        return self.game.query('''
local specs=helpers.json_to_table(''' + payload + ''');local blocked={}
for _,x in ipairs(specs) do
 local old=target(x.position,x.name)
 if old then
  local mismatch=x.direction and old.direction~=x.direction
  if x.name=="gun-turret" and old.force==f then mismatch=false end
  if x.name=="steam-engine" or x.name=="steam-turbine" then mismatch=x.direction and old.direction%8~=x.direction%8 end
  if mismatch and not x.recipe and x.name~="pipe" and x.name~="small-electric-pole" then
   blocked[#blocked+1]={name=x.name,position=x.position,reason="existing_direction_mismatch"}
  end
 elseif not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f} then
  blocked[#blocked+1]={name=x.name,position=x.position,reason="terrain_or_entity_collision"}
 end
end
return {ok=#blocked==0,blocked=blocked}
''')

    def water_sites(self, radius: int = 192, limit: int = 24) -> list[dict]:
        result = self.game.query('''
local candidates={};local seen={}
local vectors={{0,-1,0},{1,0,4},{0,1,8},{-1,0,12}}
local tiles=s.find_tiles_filtered{position={0,0},radius=''' + str(radius) + ''',name={"water","deepwater","water-green","deepwater-green"},limit=60000}
for _,tile in pairs(tiles) do
 for _,v in ipairs(vectors) do
  local p={x=tile.position.x+0.5-v[1],y=tile.position.y+0.5-v[2]}
  local key=p.x..":"..p.y..":"..v[3]
  if not seen[key] and s.can_place_entity{name="offshore-pump",position=p,direction=v[3],force=f} then
   seen[key]=true;candidates[#candidates+1]={position=p,direction=v[3],water_out={x=p.x-v[1],y=p.y-v[2]},distance=p.x*p.x+p.y*p.y}
  end
 end
end
table.sort(candidates,function(a,b) return a.distance<b.distance end)
local out={};for i=1,math.min(#candidates,''' + str(limit) + ''') do out[#out+1]=candidates[i] end
return success{sites=out}
''')
        return result.get("sites", [])

    def _occupied_by_plan(self, entities: list[dict]) -> set[tuple[float, float]]:
        known = {"boiler": (3, 2), "steam-engine": (3, 5), "burner-mining-drill": (2, 2),
                 "stone-furnace": (2, 2), "steel-furnace": (2, 2), "electric-mining-drill": (3, 3),
                 "lab": (3, 3), "assembling-machine-1": (3, 3),
                 "assembling-machine-2": (3, 3), "assembling-machine-3": (3, 3),
                 "oil-refinery": (5, 5), "chemical-plant": (3, 3), "pumpjack": (3, 3),
                 "storage-tank": (3, 3), "rocket-silo": (9, 9)}
        occupied = set()
        for e in entities:
            w, h = known.get(e["name"], (1, 1))
            w, h = e.get("_width", w), e.get("_height", h)
            if e.get("direction") in (4, 12):
                w, h = h, w
            x, y = e["position"]["x"], e["position"]["y"]
            for tx in range(math.floor(x - w / 2 + 1e-6), math.ceil(x + w / 2 - 1e-6)):
                for ty in range(math.floor(y - h / 2 + 1e-6), math.ceil(y + h / 2 - 1e-6)):
                    occupied.add((tx + .5, ty + .5))
        return occupied

    def route(self, source: dict, destination: dict, name: str, reserved: list[dict],
              *, start_direction: int | None = None, end_direction: int | None = None,
              margin: float = 12) -> dict:
        return self._route(source, destination, name, reserved, start_direction=start_direction,
                           end_direction=end_direction, margin=margin)

    def clear_route_obstacle(self, source: dict, destination: dict, reserved: list[dict],
                             *, start_direction: int | None = None, end_direction: int | None = None,
                             margin: float = 24) -> dict:
        """Return one ordinary mining action only after proving a clearable route.

        The alternate path is never exposed as a construction plan. The caller
        must reobserve and use normal collision checks after every mined entity.
        """
        result = self._route(source, destination, "transport-belt", reserved, margin=margin,
                             start_direction=start_direction, end_direction=end_direction, clear_natural=True)
        if not result.get("ok"):
            return {"ok": False, "reason": result.get("reason", "no clearable route")}
        clearable = {(row["position"]["x"], row["position"]["y"]): row["entities"]
                     for row in result.get("clearable", [])}
        for point in result["path"]:
            for entity in clearable.get((point["x"], point["y"]), []):
                if (entity.get("force") == "neutral" and entity.get("minable")
                        and (entity.get("type") == "tree" or (entity.get("type") == "simple-entity"
                             and entity.get("name") in {"big-rock", "huge-rock", "big-sand-rock"}))):
                    return {"ok": True, "action": {"type": "mine", "name": entity["name"],
                        "position": entity["position"], "count": 1,
                        "reason": "clear an observed natural obstacle on a proven material route"},
                        "planned_length": len(result["path"])}
        return {"ok": False, "reason": "route has no verified natural obstacle to clear"}

    def _route(self, source: dict, destination: dict, name: str, reserved: list[dict],
               *, start_direction: int | None = None, end_direction: int | None = None,
               margin: float = 12, clear_natural: bool = False) -> dict:
        if stop_requested(self.path.parent / "stop.json"):
            raise InterruptedError("operator_stop_requested")
        if (isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin)
                or margin < 1 or int(margin) != margin):
            return {"ok": False, "reason": "route margin must be a positive whole number of tiles"}
        bounds = {"min_x": min(source["x"], destination["x"]) - margin,
                  "max_x": max(source["x"], destination["x"]) + margin,
                  "min_y": min(source["y"], destination["y"]) - margin,
                  "max_y": max(source["y"], destination["y"]) + margin}
        cells = (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1)
        if cells > 50000:
            return {"ok": False, "reason": "route survey exceeds 50000 tiles"}
        payload = json.dumps(json.dumps({"bounds": bounds, "name": name, "source": source,
                                        "destination": destination, "clear_natural": clear_natural}, separators=(",", ":")))
        survey = self.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local b=args.bounds;local blocked={};local clearable={}
local function natural(p)
 if not args.clear_natural then return nil end
 --[[ Ghost checks retain terrain collision but may ignore buildings;
 independently reject every other entity before admitting this tile. ]]
 if not s.can_place_entity{name=args.name,position=p,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true} then return nil end
 local box=prototypes.entity[args.name].collision_box
 local area={{p.x+box.left_top.x,p.y+box.left_top.y},{p.x+box.right_bottom.x,p.y+box.right_bottom.y}}
 local out={}
 for _,e in pairs(s.find_entities_filtered{area=area}) do
  if e.type~="resource" then
   local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
   if e.force.name~="neutral" or not e.minable or not (e.type=="tree" or rock) then return nil end
   out[#out+1]={name=e.name,type=e.type,position=pos(e.position),force=e.force.name,minable=true}
  end
 end
 if #out>0 then return out end
end
for x=b.min_x,b.max_x,1 do for y=b.min_y,b.max_y,1 do
 local p={x=x,y=y}
 if not s.can_place_entity{name=args.name,position=p,force=f} then
  local removable=natural(p)
  if removable then clearable[#clearable+1]={position=p,entities=removable} else blocked[#blocked+1]=p end
 end
end end
local function endpoint(p) return (p.x==args.source.x and p.y==args.source.y) or (p.x==args.destination.x and p.y==args.destination.y) end
for _,e in pairs(s.find_entities_filtered{area={{b.min_x-1,b.min_y-1},{b.max_x+1,b.max_y+1}},force=f}) do
 if not endpoint(e.position) then
  if e.type=="transport-belt" then
   local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};local v=vectors[e.direction]
   if v then blocked[#blocked+1]={x=e.position.x+v[1],y=e.position.y+v[2]} end
  elseif args.name=="pipe" and (e.type=="pipe" or e.type=="pipe-to-ground") then
   for _,v in ipairs({{0,-1},{1,0},{0,1},{-1,0}}) do blocked[#blocked+1]={x=e.position.x+v[1],y=e.position.y+v[2]} end
  end
 end
end
return success{blocked=blocked,clearable=clearable}
''')
        if not survey.get("ok"):
            return {"ok": False, "reason": survey.get("reason") or "route survey failed"}
        occupied = self._occupied_by_plan(reserved)
        endpoints = {(source["x"], source["y"]), (destination["x"], destination["y"])}
        for entity in reserved:
            p = entity["position"]
            if (p["x"], p["y"]) in endpoints:
                continue
            if entity["name"] == "transport-belt":
                dx, dy = DIRECTIONS[entity.get("direction", 0)]
                occupied.add((p["x"] + dx, p["y"] + dy))
            elif name == "pipe" and entity["name"] in {"pipe", "pipe-to-ground"}:
                occupied.update((p["x"] + dx, p["y"] + dy) for dx, dy in DIRECTIONS.values())
        occupied.update((p["x"], p["y"]) for p in survey.get("blocked", []))
        occupied.discard((source["x"], source["y"]))
        occupied.discard((destination["x"], destination["y"]))
        result = route_orthogonal(source, destination, occupied=occupied, bounds=bounds,
                                  max_nodes=25000, start_direction=start_direction, end_direction=end_direction)
        if clear_natural and result.get("ok"):
            result["clearable"] = survey.get("clearable", [])
        return result

    def _choose_power_plan(self) -> dict:
        for site in self.water_sites():
            inward = (site["direction"] + 8) % 16
            dx, dy = DIRECTIONS[inward]
            rotation = (inward - 4) % 16
            px, py = _rotate(-4, 1, rotation)
            for distance in (3, 8, 14, 24):
                for sideways in (0, 10, -10):
                    desired = _position(site["water_out"]["x"] + dx * distance - dy * sideways,
                                        site["water_out"]["y"] + dy * distance + dx * sideways)
                    anchor = _position(desired["x"] - px, desired["y"] - py)
                    bank = build_template("steam_bank", anchor=anchor, rotation=rotation)
                    if not bank["ok"] or not self.can_place(bank["entities"])["ok"]:
                        continue
                    pump = {"name": "offshore-pump", "position": site["position"], "direction": site["direction"]}
                    water = next(p for p in bank["ports"] if p["item"] == "water")
                    path = self.route(site["water_out"], water["position"], "pipe", bank["entities"] + [pump])
                    if not path.get("ok"):
                        continue
                    pipes = [{"name": "pipe", "position": p, "direction": 0} for p in path["path"]]
                    plan = _plan([pump] + bank["entities"] + pipes, bank["ports"], template="starter_power")
                    if self.can_place(plan["entities"])["ok"]:
                        return plan
        return {"ok": False, "reason": "no clear starter coast supports connected steam power"}

    def coal_sites(self) -> list[dict]:
        result = self.game.query('''
local out={};local seen={}
for _,e in pairs(s.find_entities_filtered{position={0,0},radius=192,name="coal",limit=3000}) do
 local p={x=math.floor(e.position.x+0.5),y=math.floor(e.position.y+0.5)};local key=p.x..":"..p.y
 if not seen[key] then
  seen[key]=true
  local mixed=false
  for _,r in pairs(s.find_entities_filtered{area={{p.x-1,p.y-1},{p.x+1,p.y+1}},type="resource"}) do
   if r.name~="coal" then mixed=true;break end
  end
  if not mixed then out[#out+1]=p end
 end
end
table.sort(out,function(a,b) return a.x*a.x+a.y*a.y<b.x*b.x+b.y*b.y end)
return success{sites=out}
''')
        return result.get("sites", [])

    @staticmethod
    def _coal_plan(position: dict) -> dict:
        x, y = position["x"], position["y"]
        drill = {"name": "burner-mining-drill", "position": position, "direction": 4}
        path = [(1.5, -.5, 4), (2.5, -.5, 0), (2.5, -1.5, 0), (2.5, -2.5, 12),
                (1.5, -2.5, 12), (.5, -2.5, 12), (-.5, -2.5, 12), (-1.5, -2.5, 12),
                (-2.5, -2.5, 8), (-2.5, -1.5, 8), (-2.5, -.5, 8), (-2.5, .5, 8), (-2.5, 1.5, 8)]
        entities = [drill]
        entities.extend({"name": "transport-belt", "position": _position(x + dx, y + dy), "direction": direction}
                        for dx, dy, direction in path)
        entities.append({"name": "burner-inserter", "position": _position(x - 1.5, y - .5), "direction": 12})
        return _plan(entities, [{"kind": "item", "item": "coal", "direction": "output",
                                "position": _position(x - 2.5, y + 1.5), "facing": 8}], drill=drill)

    def _choose_coal_plan(self, power: dict) -> dict:
        destination = next(p for p in power["ports"] if p["item"] == "coal")
        for position in self.coal_sites()[:256]:
            coal = self._coal_plan(position)
            if not self.can_place(coal["entities"])["ok"]:
                continue
            source = coal["ports"][0]
            path = self.route(source["position"], destination["position"], "transport-belt",
                              power["entities"] + coal["entities"],
                              start_direction=source["facing"], end_direction=destination["facing"])
            if not path.get("ok"):
                continue
            belts = [{"name": "transport-belt", **segment} for segment in path["segments"]]
            plan = _plan(coal["entities"] + belts, coal["ports"], drill=coal["drill"], template="dedicated_coal_feed")
            if self.can_place(plan["entities"])["ok"]:
                return plan
        return {"ok": False, "reason": "no pure coal drill site has a clear dedicated boiler route"}

    def _seed(self, observation: dict, key: str, entity: dict, count: int) -> dict | None:
        actual = _find(observation, entity)
        if actual is None:
            return _report("blocked", "cannot seed a missing burner", entity=entity)
        state = self.state["seeds"].setdefault(key, {"attempts": 0, "observed": False})
        fuel = int(actual.get("inventory", {}).get("coal", 0))
        if fuel > 0 or actual.get("remaining_burning_fuel", 0) > 0:
            state["observed"] = True
            self._save()
            return None
        if state["observed"]:
            return None  # dedicated belt repair must restore fuel after startup
        if state["attempts"] >= 3:
            return _report("blocked", "startup fuel insertion was not observed after three attempts", target=key)
        available = int(observation.get("inventory", {}).get("coal", 0))
        if available < count:
            return self.bootstrap.ensure_item(observation, "coal", count)
        move = self._move(observation, entity["position"])
        if move:
            return move
        state["attempts"] += 1
        self._save()
        return {"type": "insert", "name": entity["name"], "position": entity["position"],
                "item": "coal", "inventory": "fuel", "count": count, "reason": "one-time power bootstrap fuel"}

    def power_evidence(self, power: dict, coal: dict) -> dict:
        targets = {"boiler": next(e for e in power["entities"] if e["name"] == "boiler"),
                   "engines": [e for e in power["entities"] if e["name"] == "steam-engine"],
                   "pole": next(e for e in power["entities"] if e["name"] == "small-electric-pole"),
                   "drill": coal["drill"],
                   "belts": [e for e in coal["entities"] if e["name"] == "transport-belt"]}
        payload = json.dumps(json.dumps(targets, separators=(",", ":")))
        return self.game.query('''
local t=helpers.json_to_table(''' + payload + ''')
local function fuel(e)
 if not e or not e.burner then return 0 end
 return (e.burner.remaining_burning_fuel or 0)+(e.burner.inventory.get_item_count("coal") or 0)*4000000
end
local function fluid(e,name)
 if not e then return 0 end
 local contents=e.get_fluid_contents();return contents[name] or 0
end
local boiler=target(t.boiler.position,t.boiler.name);local drill=target(t.drill.position,t.drill.name)
local steam=0;local connected=0;local energized=0
for _,row in ipairs(t.engines) do local e=target(row.position,row.name)
 if e then steam=steam+fluid(e,"steam");if e.is_connected_to_electric_network() then connected=connected+1 end;if e.energy>0 then energized=energized+1 end end
end
local coal_on_belts=0
for _,row in ipairs(t.belts) do local e=target(row.position,row.name)
 if e then for lane=1,2 do coal_on_belts=coal_on_belts+e.get_transport_line(lane).get_item_count("coal") end end
end
local generation_kw=0;local total_energy=0;local pole=target(t.pole.position,t.pole.name)
if pole then
 local stats=pole.electric_network_statistics
 generation_kw=stats.get_flow_count{name="steam-engine",category="output",precision_index=defines.flow_precision_index.five_seconds}*60/1000
 total_energy=stats.get_output_count("steam-engine")
end
local coal_rate=0
if drill then
 local mined=prototypes.entity.coal.mineable_properties
 coal_rate=drill.prototype.mining_speed*60/mined.mining_time
end
return success{boiler_fuel=fuel(boiler),drill_fuel=fuel(drill),water=fluid(boiler,"water"),steam=steam,
 connected_engines=connected,energized_engines=energized,generation_kw=generation_kw,total_energy_generated=total_energy,
 coal_on_belts=coal_on_belts,coal_mining_per_minute=coal_rate,coal_feed_needs_expansion_for_full_engine_capacity=true,tick=game.tick}
''')

    def ensure_power(self, observation: dict) -> dict:
        self._sync(observation)
        if not observation.get("technologies", {}).get("steam-power") and "boiler" not in observation.get("enabled_recipes", {}):
            return _report("blocked", "steam power technology must unlock naturally before construction")
        if "power_plan" not in self.state:
            plan = self._choose_power_plan()
            if not plan.get("ok"):
                return _report("blocked", plan["reason"])
            self.state["power_plan"] = plan
            self._save()
        power = self.state["power_plan"]
        result = self.ensure_plan(observation, power)
        if result.get("status") != "succeeded":
            return result
        if "coal_plan" not in self.state:
            plan = self._choose_coal_plan(power)
            if not plan.get("ok"):
                return _report("blocked", plan["reason"])
            self.state["coal_plan"] = plan
            self._save()
        coal = self.state["coal_plan"]
        result = self.ensure_plan(observation, coal)
        if result.get("status") != "succeeded":
            return result
        # Seed only after the complete coal path exists, so construction cannot
        # consume the seed while the eventual transport route remains unfinished.
        for key, entity, count in [("coal_drill", coal["drill"], 8),
                                   ("boiler", next(e for e in power["entities"] if e["name"] == "boiler"), 8)]:
            seed = self._seed(observation, key, entity, count)
            if seed:
                return seed
        evidence = self.power_evidence(power, coal)
        if not evidence.get("ok"):
            return _report("blocked", "power evidence query failed", diagnostics=evidence)
        ready = power_flow_ready(evidence)
        if not ready:
            self.state.pop("power_sample_tick", None)
            self._save()
            return _report("waiting", "waiting for dedicated coal, water, and steam flow", **evidence)
        tick = int(evidence.get("tick", observation.get("tick", 0)))
        first = self.state.setdefault("power_sample_tick", tick)
        self._save()
        if tick - first < 1800:
            return _report("waiting", "verifying power supply over 30 game seconds", **evidence)
        # Historical capability only: retain it through starvation so a restarted
        # supervisor can initialize the coal-repair controller. Current flow must
        # still pass every measurement above before this method succeeds.
        self.state["power_verified_once"] = True
        self._save()
        return _report("succeeded", "steam power has a dedicated self-fueled coal feed", **evidence,
                       flow_verified=True, verification_ticks=tick - first,
                       power_ports=[p for p in power["ports"] if p["kind"] == "power"])
