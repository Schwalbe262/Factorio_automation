"""Resource-backed gun-turret coverage and ammunition upkeep for live assets.

This module only chooses ordinary craft/build/insert actions. Enemy observations
never authorize entity deletion, free ammunition, damage, or research mutation.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable


_ASSETS = {"burner-mining-drill", "electric-mining-drill", "stone-furnace", "steel-furnace", "electric-furnace",
           "boiler", "steam-engine", "offshore-pump", "assembling-machine-1", "assembling-machine-2",
           "assembling-machine-3", "lab", "oil-refinery", "pumpjack", "chemical-plant", "rocket-silo"}


def _distance(first: dict[str, float], second: dict[str, float]) -> float:
    return math.hypot(float(first["x"]) - float(second["x"]), float(first["y"]) - float(second["y"]))


def _result(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


class DeterministicDefense:
    def __init__(self, game: Any, bootstrap: Any, catalog: Any = None, *, ammo_target: int = 20):
        if ammo_target < 1:
            raise ValueError("ammo_target must be positive")
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.ammo_target = ammo_target
        self._world_id: str | None = None
        self._last_tick = -1
        self._health: dict[str, float] = {}
        self._damage: dict[str, int] = {}
        self._range: float | None = None
        self.automatic_ammo: Callable[[dict], bool] | None = None

    def _turret_range(self) -> float:
        if self._range is None:
            data = self.game.query('local p=prototypes.entity["gun-turret"];return {range=p and p.attack_parameters and p.attack_parameters.range or 0}')
            self._range = float(data.get("range") or 0)
        return self._range

    @staticmethod
    def _key(entity: dict[str, Any]) -> str:
        p = entity["position"]
        return str(entity.get("unit_number") or f"{entity['name']}:{p['x']}:{p['y']}")

    def _observe_damage(self, observation: dict[str, Any], assets: list[dict[str, Any]]) -> list[str]:
        tick = int(observation.get("tick") or 0)
        world = str(observation.get("world_id") or "")
        if world != self._world_id or tick < self._last_tick:
            self._health.clear()
            self._damage.clear()
            self._range = None
        self._world_id, self._last_tick = world, tick
        now: dict[str, float] = {}
        for entity in assets:
            if not isinstance(entity.get("health"), (float, int)):
                continue
            key = self._key(entity)
            health = float(entity["health"])
            if key in self._health and health < self._health[key]:
                self._damage[key] = tick
            now[key] = health
        self._health = now
        self._damage = {key: damage_tick for key, damage_tick in self._damage.items() if tick - damage_tick <= 600}
        return sorted(self._damage)

    def _assets(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        assets = [e for e in observation.get("entities", []) if e.get("name") in _ASSETS and isinstance(e.get("position"), dict)]
        priority = {"lab": 0, "boiler": 1, "steam-engine": 2, "rocket-silo": 3}
        return sorted(assets, key=lambda e: (priority.get(e["name"], 4), e["position"]["x"], e["position"]["y"]))

    def _threats(self, assets: list[dict[str, Any]]) -> dict[str, Any]:
        if not assets:
            return {"ok": True, "enemies": []}
        anchors = [{"x": float(e["position"]["x"]), "y": float(e["position"]["y"])} for e in assets]
        encoded = json.dumps(json.dumps(anchors, separators=(",", ":")))
        return self.game.query('''
local anchors=helpers.json_to_table(''' + encoded + ''');local out={};local seen={}
for _,anchor in ipairs(anchors) do
 for _,e in pairs(s.find_entities_filtered{position=anchor,radius=48,force="enemy",type={"unit","unit-spawner","turret"}}) do
  local key=e.unit_number or (e.name..":"..e.position.x..":"..e.position.y)
  if not seen[key] then seen[key]=true;out[#out+1]={name=e.name,type=e.type,position=pos(e.position)} end
 end
end
return {ok=true,enemies=out}
''')

    def requirements(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Construction/research targets for the supervisor's production planner."""
        assets = self._assets(observation)
        turrets = [e for e in observation.get("entities", []) if e.get("name") == "gun-turret"]
        coverage = max(0, self._turret_range() - 2) if assets else 0
        uncovered = [e for e in assets if not any(_distance(e["position"], t["position"]) <= coverage for t in turrets)]
        # Reserve one planned coverage center per separated group of assets. Exact
        # collision-aware placement is handled by next_action's live query.
        centers: list[dict[str, float]] = []
        for entity in uncovered:
            if not any(_distance(entity["position"], p) <= max(1, coverage - 4) for p in centers):
                centers.append(entity["position"])
        desired = len(turrets) + len(centers)
        stock = sum(int((e.get("inventory") or {}).get("firearm-magazine", 0)) for e in turrets)
        held = int((observation.get("inventory") or {}).get("firearm-magazine", 0))
        research: list[str] = []
        if desired and not (observation.get("enabled_recipes") or {}).get("gun-turret"):
            technologies = getattr(self.catalog, "technologies", {}) if self.catalog is not None else {}
            for name, tech in technologies.items():
                if "gun-turret" in (tech.get("unlocks") or []) and not (observation.get("technologies") or {}).get(name):
                    research.append(name)
        return {"items": {"gun-turret": len(centers),
                          "firearm-magazine": max(0, desired * self.ammo_target - stock - held)},
                "production_per_minute": {"firearm-magazine": max(4, 2 * desired)} if desired else {},
                "research": sorted(research), "uncovered_assets": len(uncovered), "turret_count": len(turrets)}

    def next_action(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not observation.get("ok", True):
            return _result("blocked", "defense requires a valid observation", error=observation.get("reason"))
        assets = self._assets(observation)
        damage = self._observe_damage(observation, assets)
        if not assets:
            return _result("succeeded", "no production assets require turret coverage")
        threat = self._threats(assets)
        if not threat.get("ok"):
            return _result("blocked", "enemy observation failed", error=threat.get("reason"))
        enemies = threat.get("enemies") or []
        urgent = bool(damage) or any(e.get("type") == "unit" and any(
            _distance(e["position"], a["position"]) < 32 for a in assets) for e in enemies)
        turrets = [e for e in observation.get("entities", []) if e.get("name") == "gun-turret"]
        refill = max(1, self.ammo_target // 2)
        for turret in sorted(turrets, key=lambda e: int((e.get("inventory") or {}).get("firearm-magazine", 0))):
            if self.automatic_ammo is not None and self.automatic_ammo(turret):
                continue
            stock = int((turret.get("inventory") or {}).get("firearm-magazine", 0))
            if stock >= refill:
                continue
            held = int((observation.get("inventory") or {}).get("firearm-magazine", 0))
            if held:
                return {"type": "insert", "name": "gun-turret", "position": turret["position"],
                        "item": "firearm-magazine", "inventory": "turret_ammo", "count": min(held, self.ammo_target - stock),
                        "reason": "arm production perimeter turret", "urgent": urgent}
            return self.bootstrap.ensure_item(observation, "firearm-magazine", self.ammo_target - stock)
        coverage = self._turret_range() - 2
        if coverage <= 0:
            return _result("blocked", "live gun-turret range is unavailable")
        uncovered = [e for e in assets if not any(_distance(e["position"], t["position"]) <= coverage for t in turrets)]
        if not uncovered:
            empty = sum(not int((turret.get("inventory") or {}).get("firearm-magazine", 0)) for turret in turrets)
            if empty:
                return _result("waiting", "automatic ammunition supply has empty perimeter turrets",
                               empty_turrets=empty, urgent=urgent, damage_observed=damage)
            return _result("succeeded", "production sites covered by armed turrets", turret_count=len(turrets),
                           nearby_enemies=len(enemies), damage_observed=damage, urgent=urgent)
        if not (observation.get("enabled_recipes") or {}).get("gun-turret"):
            return _result("blocked" if urgent else "waiting", "gun-turret research required for production defense",
                           requirements=self.requirements(observation), urgent=urgent, damage_observed=damage)
        anchor = uncovered[0]["position"]
        nearest = min(enemies, key=lambda e: _distance(anchor, e["position"]), default=None)
        site = self._find_turret_site(anchor, nearest["position"] if nearest else None, assets, coverage)
        if not site.get("ok"):
            return _result("blocked", "no clear turret site covers production", anchor=anchor,
                           query_error=site.get("reason"), urgent=urgent)
        # Stock both gun and ammunition before exposing a new, empty turret.
        if int((observation.get("inventory") or {}).get("firearm-magazine", 0)) < self.ammo_target:
            return self.bootstrap.ensure_item(observation, "firearm-magazine", self.ammo_target)
        if int((observation.get("inventory") or {}).get("gun-turret", 0)) < 1:
            return self.bootstrap.ensure_item(observation, "gun-turret", 1)
        return {"type": "build", "name": "gun-turret", "position": site["position"], "direction": 0,
                "reason": "build armed perimeter coverage for production", "urgent": urgent}

    def _find_turret_site(self, anchor: dict[str, float], threat: dict[str, float] | None,
                         assets: list[dict[str, Any]], coverage: float) -> dict[str, Any]:
        payload = {"anchor": anchor, "threat": threat,
                   "assets": [e["position"] for e in assets], "coverage": coverage}
        encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
        return self.game.query('''
local args=helpers.json_to_table(''' + encoded + ''');local best=nil;local best_score=-math.huge
local ax=math.floor(args.anchor.x+0.5);local ay=math.floor(args.anchor.y+0.5)
for dx=-12,12,2 do for dy=-12,12,2 do
 local p={x=ax+dx,y=ay+dy};local d=dx*dx+dy*dy
 if d>=16 and d<=args.coverage^2 and s.can_place_entity{name="gun-turret",position=p,force=f,direction=0}
  and s.count_entities_filtered{area={{p.x-1,p.y-1},{p.x+1,p.y+1}},type="resource"}==0 then
  local covered=0
  for _,asset in ipairs(args.assets) do if (p.x-asset.x)^2+(p.y-asset.y)^2<=args.coverage^2 then covered=covered+1 end end
  local facing=0
  if args.threat then facing=dx*(args.threat.x-args.anchor.x)+dy*(args.threat.y-args.anchor.y) end
  local score=covered*10000-math.abs(math.sqrt(d)-6)*10+facing*0.01
  if score>best_score then best_score=score;best=p end
 end
end end
return best and {ok=true,position=best} or {ok=false,reason="placement_blocked"}
''')
