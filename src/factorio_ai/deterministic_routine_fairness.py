"""Give quiet factories a construction turn after three completed defense actions."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .deterministic_defense import _ASSETS
from .deterministic_state import _atomic_json


SAFETY_LUA = '''
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id or not a or not a.valid or a.unit_number~=x.actor
 or a.force~=f or a.surface~=s then return {ok=false,reason="fairness_actor_changed"} end
local function healthy(e)
 return e.health and e.max_health and e.max_health>0 and e.health>=e.max_health
end
if not healthy(a) then return {ok=true,quiet=false,reason="damaged_actor"} end
local anchors={a.position};local seen={};local live_assets=s.find_entities_filtered{force=f,name=x.names}
if #live_assets~=#x.assets then return {ok=false,reason="fairness_assets_changed"} end
for _,p in ipairs(x.assets) do
 local e=target(p.position,p.name)
 if not e or e.force~=f or e.unit_number~=p.unit_number or e.position.x~=p.position.x or e.position.y~=p.position.y then
  return {ok=false,reason="fairness_asset_changed"} end
 if not healthy(e) then return {ok=true,quiet=false,reason="damaged_asset",unit_number=e.unit_number} end
 if e.name=="gun-turret" then
  local inv=e.get_inventory(defines.inventory.turret_ammo)
  if not inv or inv.get_item_count("firearm-magazine")<10 then
   return {ok=true,quiet=false,reason="low_turret_ammunition",unit_number=e.unit_number}
  end
 end
 seen[e.unit_number]=true;anchors[#anchors+1]=e.position
end
for _,e in ipairs(live_assets) do if not seen[e.unit_number] then return {ok=false,reason="fairness_assets_changed"} end end
for _,p in ipairs(anchors) do
 if s.count_entities_filtered{position=p,radius=48,force="enemy",type={"unit","unit-spawner","turret"}}>0 then
  return {ok=true,quiet=false,reason="nearby_enemy"}
 end
end
for _,p in ipairs(x.routes) do
 local e=target(p.position,p.name)
 if not e or e.force~=f or e.position.x~=p.position.x or e.position.y~=p.position.y
  or (p.unit_number and e.unit_number~=p.unit_number)
  or (p.direction~=nil and e.direction~=p.direction) then
  return {ok=true,quiet=true,routes_ready=false,reason="ammunition_route_missing"}
 end
 if not healthy(e) then return {ok=true,quiet=false,reason="damaged_ammunition_route"} end
 if e.type=="inserter" or e.type=="assembling-machine" then
  if e.energy<=0 or not e.is_connected_to_electric_network() then
   return {ok=true,quiet=true,routes_ready=false,reason="ammunition_route_unpowered"}
  end
 end
 if p.recipe then local recipe=e.get_recipe();if not recipe or recipe.name~=p.recipe then
  return {ok=true,quiet=true,routes_ready=false,reason="ammunition_recipe_changed"} end end
 if p.ammunition and e.type=="transport-belt" then
  for i=1,2 do for _,stack in pairs(e.get_transport_line(i).get_contents()) do
   if stack.count>0 and stack.name~="firearm-magazine" then
    return {ok=true,quiet=true,routes_ready=false,reason="ammunition_route_contaminated"}
   end
  end end
 end
end
return {ok=true,quiet=true,routes_ready=true,tick=game.tick}
'''


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def completed_action(action: dict, outcome: dict) -> bool:
    """Positive adapter receipts exclude movement polls and no-op acknowledgements."""
    if outcome.get("ok") is not True or outcome.get("status") != "succeeded" or outcome.get("continued_action"):
        return False
    kind = action.get("type")
    if kind in {"take", "insert", "recover_equipped_ammo"}:
        return _positive(outcome.get("moved"))
    if kind == "build":
        return type(outcome.get("unit_number")) is int and outcome["unit_number"] > 0 and not outcome.get("reused")
    if kind == "build_many":
        return (isinstance(action.get("actions"), list) and bool(action["actions"])
                and type(outcome.get("completed")) is int and outcome["completed"] == len(action["actions"])
                and _positive(outcome.get("built"))
                and "failed_index" not in outcome and "uncertain_index" not in outcome)
    return kind == "mine" and _positive(outcome.get("mined"))


class RoutineFairness:
    def __init__(self, game):
        self.game = game
        self.path = Path(game.cfg.runtime_dir) / "routine-fairness.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and (self.state.get("schema_version") != 1
                or type(self.state.get("completed")) is not int or not 0 <= self.state["completed"] <= 3):
            raise ValueError("invalid routine fairness checkpoint")
        self.selection = None
        self.safety = {"ok": False, "reason": "not_observed"}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _sync(self, obs, fingerprint):
        world, tick = obs.get("world_id"), obs.get("tick")
        if (not isinstance(world, str) or not world or not isinstance(fingerprint, str) or not fingerprint
                or type(tick) is not int or tick < 0):
            return False
        if (self.state.get("world_id") != world or self.state.get("catalog_fingerprint") != fingerprint
                or tick < self.state.get("last_tick", 0)):
            self.state = {"schema_version": 1, "world_id": world, "catalog_fingerprint": fingerprint,
                          "last_tick": tick, "completed": 0}
        self.state["last_tick"] = tick
        self._save()
        return True

    def _payload(self, obs, factory, armaments):
        fingerprint = factory.catalog.fingerprint
        for state in (factory.state, armaments.state):
            if (not isinstance(state, dict) or state.get("world_id") != obs["world_id"]
                    or state.get("catalog_fingerprint") != fingerprint or state.get("last_tick", 0) > obs["tick"]):
                raise ValueError("fairness controller identity differs")
        if obs.get("ok") is not True or type(obs.get("actor_unit_number")) is not int or obs["actor_unit_number"] <= 0:
            raise ValueError("fairness requires the observed actor")
        assets = [e for e in obs["entities"] if e["name"] in _ASSETS | {"gun-turret"}]
        turrets = [e for e in assets if e["name"] == "gun-turret"]
        if not turrets or any(type(e.get("unit_number")) is not int or e["unit_number"] <= 0 for e in assets):
            raise ValueError("fairness requires identified armed turrets")
        routes = []
        live = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs["entities"]}
        producer = factory.state["blocks"].get("recipe:firearm-magazine")
        for turret in turrets:
            key = armaments._key(turret)
            row = armaments.state["turrets"].get(key)
            link = factory.state["links"].get("armaments:" + key)
            if row and row.get("unit_number") != turret["unit_number"]:
                raise ValueError("fairness turret identity differs")
            if not link:  # A newly seeded turret need not already have a reserved intake.
                if row and row.get("plan"):
                    raise ValueError("fairness saved intake has no link")
                continue
            if (not row or not row.get("plan") or not producer
                    or factory.state["blocks"].get("armaments:" + key) != row["plan"]
                    or link["source_port"] not in producer["ports"]
                    or link["consumer_port"] not in row["plan"]["ports"]
                    or any(p.get("kind") != "item" or p.get("item") != "firearm-magazine"
                           for p in (link["source_port"], link["consumer_port"]))):
                raise ValueError("fairness ammunition ownership differs")
            for plan in (link, row["plan"]):
                routes.extend({**e, "ammunition": True} for e in plan["entities"])
            for name in ("armaments:" + key, "tap:armaments:" + key):
                routes.extend(deepcopy(factory.state.get("power_links", {}).get(name, {}).get("entities", [])))
        if routes:
            routes.extend(deepcopy(producer["entities"]))
        for e in routes:
            found = live.get((e["name"], e["position"]["x"], e["position"]["y"]))
            if found:
                e["unit_number"] = found["unit_number"]
        assets = [{key: e[key] for key in ("name", "unit_number", "position")} for e in assets]
        return {"world_id": obs["world_id"], "actor": obs["actor_unit_number"], "assets": assets,
                "names": sorted(_ASSETS | {"gun-turret"}), "routes": routes}

    def prefer_production(self, factory, armaments, defense, obs):
        self.selection = None
        self.safety = {"ok": False, "reason": "fairness_observation_unavailable"}
        if not self._sync(obs, getattr(factory.catalog, "fingerprint", None)):
            return False
        try:
            payload = self._payload(obs, factory, armaments)
            damage = defense._observe_damage(obs, defense._assets(obs))
            if damage:
                self.safety = {"ok": True, "quiet": False, "reason": "recent_asset_damage"}
            else:
                encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
                self.safety = self.game.query(SAFETY_LUA.replace("PAYLOAD", encoded))
        except Exception:
            return False  # The normal defense planner retains authority on unknown evidence.
        if not isinstance(self.safety, dict):
            self.safety = {"ok": False, "reason": "malformed_fairness_survey"}
            return False
        return (self.state["completed"] >= 3 and self.safety.get("ok") is True
                and self.safety.get("quiet") is True and self.safety.get("routes_ready") is True
                and type(self.safety.get("tick")) is int and self.safety["tick"] >= obs["tick"])

    def bind(self, lane, action):
        if action.get("type") and self.state:
            self.selection = (lane, deepcopy(action), self.safety.get("ok") is True and self.safety.get("quiet") is True)
        return action

    def record(self, action, outcome):
        selected, self.selection = self.selection, None
        if not selected or selected[1] != action or not completed_action(action, outcome):
            return
        lane, _, quiet = selected
        if lane == "production":
            self.state["completed"] = 0
        elif lane == "routine" and quiet:
            self.state["completed"] = min(3, self.state["completed"] + 1)
        else:
            return
        self._save()
