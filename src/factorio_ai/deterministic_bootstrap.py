"""Observation-driven starter cells and natural early research unlocks.

Only coal, stone and trees may be hand-mined during bootstrap. Iron and copper
always come from drills feeding real furnaces. All crafting uses live enabled
recipes and the character's engine crafting queue.
"""
from __future__ import annotations

import json
import math
from typing import Any


def _report(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _at(entity: dict[str, Any], name: str, position: dict[str, float]) -> bool:
    p = entity.get("position") or {}
    return entity.get("name") == name and abs(p.get("x", 1e9) - position["x"]) < .2 and abs(p.get("y", 1e9) - position["y"]) < .2


class DeterministicBootstrap:
    def __init__(self, game: Any, catalog: Any = None):
        self.game = game
        self.catalog = catalog
        self._recipes: dict[str, dict[str, Any]] = {}
        self._world_id: str | None = None

    def next_action(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not observation.get("ok", True):
            return _report("blocked", observation.get("reason", "observation_failed"))
        world_id = observation.get("world_id")
        if world_id != self._world_id:
            self._recipes.clear()
            self._world_id = world_id
        for entity in observation.get("entities", []):
            if entity.get("name") == "burner-mining-drill" and entity.get("status_name") in {"no_minable_resources", "no_mineable_resources"}:
                return {"type": "mine", "name": entity["name"], "position": entity["position"], "count": 1,
                        "reason": "recover depleted drill for a live ore patch"}
        fuel = self.maintain_fuel(observation)
        if fuel is not None:
            return fuel
        for resource, receiver in [("iron-ore", "stone-furnace"), ("coal", "wooden-chest"),
                                   ("copper-ore", "stone-furnace"), ("stone", "wooden-chest")]:
            action = self._ensure_cell(observation, resource, receiver)
            if action is not None:
                return action
        techs = observation.get("technologies") or {}
        if not techs.get("steam-power"):
            return self._collect_or_wait(observation, "iron-plate", 60, "waiting for natural 50-iron-plate steam-power unlock")
        if not techs.get("electronics"):
            return self._collect_or_wait(observation, "copper-plate", 20, "waiting for natural 10-copper-plate electronics unlock")
        if not self._count(observation, "lab") and not any(e.get("name") == "lab" for e in observation.get("entities", [])):
            return self.ensure_item(observation, "lab", 1)
        if not techs.get("automation-science-pack"):
            actor = self.game.query('return {ok=true,associated=a.player~=nil}')
            if not actor.get("associated"):
                return _report("blocked", "engine crafting requires an associated player for research credit",
                               dependency="crafting_player", query_error=actor.get("reason"))
            # A resumed prototype may contain a lab crafted by an unassociated
            # character. Craft another through the associated player's engine;
            # owning the old item cannot substitute for the natural craft event.
            return self.ensure_item(observation, "lab", self._count(observation, "lab") + 1)
        return _report("succeeded", "starter mining cells and natural early unlocks verified",
                       technologies=["steam-power", "electronics", "automation-science-pack"],
                       lab_count=self._count(observation, "lab"))

    @staticmethod
    def _count(observation: dict[str, Any], item: str) -> int:
        return int((observation.get("inventory") or {}).get(item, 0))

    def _recipe(self, item: str) -> dict[str, Any]:
        if item in self._recipes:
            return self._recipes[item]
        # Querying force recipes keeps the engine's exact categories and amounts;
        # enabled flags are rechecked against each fresh observation below.
        body = '''
local item=''' + json.dumps(item) + '''
local r=f.recipes[item]
if not r then
 for _,candidate in pairs(f.recipes) do
  if not candidate.hidden then for _,p in pairs(candidate.products) do
   if p.type=="item" and p.name==item then r=candidate;break end
  end end
  if r then break end
 end
end
if not r then return {ok=false,reason="no_recipe"} end
local categories={}
local good,values=pcall(function() return r.categories end)
if good and values then categories=values else
 local old_good,category=pcall(function() return r.category end)
 if old_good and category then categories={category} end
end
local hand=false
for k,v in pairs(categories) do
 local category=type(k)=="string" and k or v
 if prototypes.entity.character.crafting_categories[category] then hand=true end
end
local ingredients={};local products={}
for _,p in pairs(r.ingredients) do ingredients[#ingredients+1]={name=p.name,type=p.type,amount=p.amount} end
for _,p in pairs(r.products) do products[#products+1]={name=p.name,type=p.type,amount=p.amount,probability=p.probability} end
return {ok=true,name=r.name,enabled=r.enabled,handcraftable=hand,ingredients=ingredients,products=products}
'''
        recipe = self.game.query(body)
        if recipe.get("ok"):
            self._recipes[item] = recipe
        return recipe

    def ensure_item(self, observation: dict[str, Any], item: str, count: int,
                    _stack: tuple[str, ...] = ()) -> dict[str, Any]:
        """Return one material-acquisition/crafting action, or an explicit report."""
        have = self._count(observation, item)
        if have >= count:
            return _report("succeeded", "required inventory available", item=item, count=have)
        collect = self._take_output(observation, item, count - have)
        if collect is not None:
            return collect
        if item in {"iron-plate", "copper-plate"}:
            return _report("waiting", f"waiting for drill-fed {item} production", item=item, have=have, need=count)
        if item in {"iron-ore", "copper-ore"}:
            return _report("blocked", "iron and copper ore require a mining drill", item=item)
        if item in {"coal", "stone", "wood"}:
            return self._raw_material(observation, item, count - have)
        if observation.get("crafting_queue"):
            return _report("waiting", "engine crafting queue is busy", item=item)
        if item in _stack:
            return _report("blocked", "handcraft dependency cycle", item=item)
        recipe = self._recipe(item)
        if not recipe.get("ok"):
            return _report("blocked", "no live recipe for required item", item=item,
                           query_error=recipe.get("reason", "recipe_query_failed"))
        enabled = observation.get("enabled_recipes") or {}
        if not enabled.get(recipe["name"]):
            return _report("blocked", "required recipe is locked", item=item, recipe=recipe["name"])
        if not recipe.get("handcraftable"):
            return _report("blocked", "required item needs a production machine", item=item, recipe=recipe["name"])
        output = sum(float(p.get("amount") or 0) for p in recipe["products"]
                     if p.get("name") == item and p.get("type", "item") == "item" and p.get("probability", 1) in {None, 1})
        if output <= 0:
            return _report("blocked", "recipe has no deterministic item output", item=item)
        runs = math.ceil((count - have) / output)
        for ingredient in recipe["ingredients"]:
            if ingredient.get("type", "item") != "item":
                return _report("blocked", "handcraft recipe requires fluid", item=item)
            amount = math.ceil(float(ingredient["amount"]) * runs)
            if self._count(observation, ingredient["name"]) < amount:
                return self.ensure_item(observation, ingredient["name"], amount, (*_stack, item))
        return {"type": "craft", "recipe": recipe["name"], "count": runs,
                "reason": f"engine craft {item} from live recipe"}

    def _take_output(self, observation: dict[str, Any], item: str, count: int) -> dict[str, Any] | None:
        for entity in observation.get("entities", []):
            name = entity.get("name", "")
            if name not in {"stone-furnace", "steel-furnace", "electric-furnace", "wooden-chest", "iron-chest", "steel-chest",
                            "assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}:
                continue
            if item in {"coal", "wood"} and "chest" not in name:
                continue  # Never count an operating machine's fuel as output.
            if "furnace" in name and item not in {"iron-plate", "copper-plate", "steel-plate", "stone-brick"}:
                continue
            stock = int((entity.get("inventory") or {}).get(item, 0))
            if stock > 0 and name.startswith("assembling-machine"):
                result = self.game.query('local e=target({x=' + str(float(entity["position"]["x"])) +
                    ',y=' + str(float(entity["position"]["y"])) + '},' + json.dumps(name) + '); '
                    'local output=e and e.get_output_inventory();return {count=output and output.get_item_count(' + json.dumps(item) + ') or 0}')
                stock = int(result.get("count", 0))
            if stock > 0:
                return {"type": "take", "name": name, "position": entity["position"], "item": item,
                        "count": min(count, stock, 50), "reason": f"collect produced {item}"}
        return None

    def _collect_or_wait(self, observation: dict[str, Any], item: str, count: int, reason: str) -> dict[str, Any]:
        collect = self._take_output(observation, item, max(1, count - self._count(observation, item)))
        return collect or _report("waiting", reason)

    def maintain_fuel(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        for entity in observation.get("entities", []):
            if entity.get("name") not in {"burner-mining-drill", "stone-furnace"}:
                continue
            fuel = (entity.get("inventory") or {}).get("coal", 0) + (entity.get("inventory") or {}).get("wood", 0)
            if fuel >= 2:
                continue
            coal = self._count(observation, "coal")
            if coal <= 0:
                return self.ensure_item(observation, "coal", 24)
            return {"type": "insert", "name": entity["name"], "position": entity["position"],
                    "item": "coal", "count": min(8, coal), "inventory": "fuel", "reason": "fuel starter production"}
        return None

    def _raw_material(self, observation: dict[str, Any], item: str, count: int) -> dict[str, Any]:
        if item == "wood":
            tree = self.game.query('''
local best=nil;local distance=math.huge
for _,e in pairs(s.find_entities_filtered{position=a.position,radius=384,type="tree"}) do
 local ds=(e.position.x-a.position.x)^2+(e.position.y-a.position.y)^2
 if ds<distance then best=e;distance=ds end
end
return best and {ok=true,name=best.name,position=pos(best.position)} or {ok=false,reason="no_tree"}
''')
            if not tree.get("ok"):
                return _report("blocked", "no reachable bootstrap wood source")
            return {"type": "mine", "name": tree["name"], "position": tree["position"], "count": 1,
                    "reason": "collect starter wood from a real tree"}
        cells = self._existing_cells(item)
        if not cells.get("ok"):
            return _report("blocked", "cannot verify automated raw-material supply", item=item,
                           query_error=cells.get("reason"))
        if cells.get("cells"):
            # A coal drill with no fuel cannot bootstrap itself. A small emergency
            # seed may restart it; once operating, wait for its actual chest output.
            if item != "coal" or any(c.get("fuel", 0) > 0 or c.get("burning") for c in cells["cells"]):
                return _report("waiting", f"waiting for automated {item} output")
            count = min(count, 8)
        resource = (observation.get("resources") or {}).get(item)
        if not resource:
            return _report("blocked", "starter resource not observed", item=item)
        return {"type": "mine", "name": item, "position": resource["position"], "count": min(50, max(1, count)),
                "reason": f"bootstrap {item} until drill supply is operating"}

    def _existing_cells(self, resource: str) -> dict[str, Any]:
        return self.game.query('''
local resource=''' + json.dumps(resource) + '''
local cells={}
for _,e in pairs(s.find_entities_filtered{force=f,type="mining-drill"}) do
 local target_name=nil
 local good,t=pcall(function() return e.mining_target end)
 if good and t and t.valid then target_name=t.name end
 local counts={};local best=0
 if not target_name then
  for _,ore in pairs(s.find_entities_filtered{position=e.position,radius=1.5,type="resource"}) do
   counts[ore.name]=(counts[ore.name] or 0)+1
   if counts[ore.name]>best then best=counts[ore.name];target_name=ore.name end
  end
 end
 if target_name==resource then
  local receiver=nil
  for _,candidate in pairs(s.find_entities_filtered{position=e.drop_position,radius=1.5,force=f}) do
   if candidate.type=="furnace" or candidate.type=="container" then
    local box=candidate.bounding_box;local p=e.drop_position
    if math.abs(p.x-candidate.position.x)<=candidate.prototype.tile_width/2
     and math.abs(p.y-candidate.position.y)<=candidate.prototype.tile_height/2 then receiver=candidate;break end
   end
  end
  local burner=e.burner
  cells[#cells+1]={drill={name=e.name,position=pos(e.position),direction=e.direction},drop_position=pos(e.drop_position),
   receiver=receiver and {name=receiver.name,position=pos(receiver.position)} or nil,
   fuel=e.get_fuel_inventory() and e.get_fuel_inventory().get_item_count("coal") or 0,
   burning=burner and burner.remaining_burning_fuel>0 or false}
 end
end
return {ok=true,cells=cells}
''')

    def discover_cell(self, resource: str, receiver_name: str) -> dict[str, Any]:
        existing = self._existing_cells(resource)
        if not existing.get("ok"):
            return existing
        for cell in existing.get("cells", []):
            if cell.get("receiver") and cell["receiver"]["name"] == receiver_name:
                return {"ok": True, "complete": True, **cell}
            if cell["drill"].get("direction") == 0:
                p = cell["drill"]["position"]
                drop = cell["drop_position"]
                receiver = {"x": p["x"], "y": p["y"] - 2} if receiver_name == "stone-furnace" else {
                    "x": math.floor(drop["x"]) + .5, "y": math.floor(drop["y"]) + .5}
                return {"ok": True, "complete": False, "drill": cell["drill"],
                        "receiver": {"name": receiver_name, "position": receiver}}
        return self.game.query('''
local resource=''' + json.dumps(resource) + ''';local receiver_name=''' + json.dumps(receiver_name) + '''
local seen={};local best=nil;local best_score=-math.huge
for _,ore in pairs(s.find_entities_filtered{position={0,0},radius=384,name=resource}) do
 local x=math.floor(ore.position.x+0.5);local y=math.floor(ore.position.y+0.5)
 local key=x..","..y
 if not seen[key] then
  seen[key]=true
  local p={x=x,y=y};local receiver={x=x,y=y-2}
  if receiver_name=="wooden-chest" then receiver={x=x-0.5,y=y-1.5} end
  local old_receiver=target(receiver,receiver_name)
  if s.can_place_entity{name="burner-mining-drill",position=p,direction=0,force=f}
   and (old_receiver or s.can_place_entity{name=receiver_name,position=receiver,direction=0,force=f}) then
   local richness=0;local tiles=0;local mixed=false
   for _,r in pairs(s.find_entities_filtered{area={{x-1,y-1},{x+1,y+1}},type="resource"}) do
    if r.name==resource then richness=richness+r.amount;tiles=tiles+1 else mixed=true end
   end
   local score=(old_receiver and 1000000000 or 0)+math.min(tiles,4)*1000000+math.min(richness,1000)-(x*x+y*y)
   if not mixed and richness>0 and score>best_score then
    best_score=score;best={ok=true,complete=false,drill={name="burner-mining-drill",position=p,direction=0},
      receiver={name=receiver_name,position=receiver}}
   end
  end
 end
end
return best or {ok=false,reason="no_clear_direct_mining_cell_site"}
''')

    def _ensure_cell(self, observation: dict[str, Any], resource: str, receiver_name: str) -> dict[str, Any] | None:
        cell = self.discover_cell(resource, receiver_name)
        if not cell.get("ok"):
            return _report("blocked", cell.get("reason", "cell_site_query_failed"), resource=resource)
        if cell.get("complete"):
            return None
        for part in [cell["receiver"], cell["drill"]]:
            if any(_at(entity, part["name"], part["position"]) for entity in observation.get("entities", [])):
                continue
            if self._count(observation, part["name"]) < 1:
                return self.ensure_item(observation, part["name"], 1)
            return {"type": "build", **part, "direction": part.get("direction", 0),
                    "reason": f"build direct {resource} supply cell"}
        return _report("waiting", "waiting for direct cell drop-point verification", resource=resource)
