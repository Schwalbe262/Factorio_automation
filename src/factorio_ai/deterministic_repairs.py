"""Acquire ordinary repair packs and schedule native repairs for owned assets."""
from __future__ import annotations

import json
import math

from .deterministic_defense import _ASSETS


_NON_REPAIR_TARGETS = {"character", "resource", "corpse", "character-corpse", "rail-remnants",
                       "entity-ghost", "tile-ghost", "item-entity"}


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _position(value) -> bool:
    return isinstance(value, dict) and all(_finite(value.get(axis)) for axis in ("x", "y"))


def _owned(entity) -> bool:
    # The adapter already force-filters observation entities. Explicit contrary
    # force evidence must never turn a foreign entity into a repair candidate.
    return isinstance(entity, dict) and entity.get("force", "player") == "player"


class NativeRepairs:
    def __init__(self, game, bootstrap, factory, catalog):
        self.game, self.bootstrap, self.factory, self.catalog = game, bootstrap, factory, catalog

    def _quiet(self, obs: dict, target: dict, defense) -> bool:
        assets = [entity for entity in obs.get("entities", [])
                  if _owned(entity) and entity.get("name") in _ASSETS | {"gun-turret"}]
        anchors = [*assets, target, {"position": obs["position"]}]
        if any(not _position(entity.get("position")) for entity in anchors):
            return False
        if defense is not None:
            result = defense._threats(anchors)
            enemies = result.get("enemies") if isinstance(result, dict) else None
            # Factorio serializes an empty Lua table as either {} or [].
            if (not isinstance(result, dict) or result.get("ok") is not True
                    or type(enemies) not in (dict, list) or enemies):
                return False
            # Defense surveys observation positions without an identity guard.
            # Recheck the live actor and world, including its current position,
            # before allowing repair acquisition or research to take priority.
        positions = list({(entity["position"]["x"], entity["position"]["y"]): entity["position"]
                          for entity in anchors}.values())
        payload = json.dumps(json.dumps({"positions": positions, "world": obs["world_id"],
                                        "actor": obs["actor_unit_number"]}, separators=(",", ":")))
        result = self.game.query('''
local x=helpers.json_to_table(''' + payload + ''')
if not d or d.world_id~=x.world or not a or not a.valid or a.unit_number~=x.actor
 or a.force~=f or a.surface~=s then return {ok=false,reason="repair_actor_changed"} end
x.positions[#x.positions+1]=a.position
for _,p in ipairs(x.positions) do
 if s.count_entities_filtered{position=p,radius=48,force="enemy",type={"unit","unit-spawner","turret"}}>0 then
  return {ok=true,quiet=false,tick=game.tick}
 end
end
return {ok=true,quiet=true,tick=game.tick}
''')
        return (isinstance(result, dict) and result.get("ok") is True and result.get("quiet") is True
                and type(result.get("tick")) is int and result["tick"] >= obs["tick"])

    def next_action(self, obs: dict, defense=None) -> dict | None:
        if (obs.get("ok") is not True or not isinstance(obs.get("world_id"), str) or not obs["world_id"]
                or type(obs.get("actor_unit_number")) is not int or obs["actor_unit_number"] <= 0
                or type(obs.get("tick")) is not int or obs["tick"] < 0
                or not _position(obs.get("position"))):
            return None
        candidates = [entity for entity in obs.get("entities", [])
                      if _owned(entity) and isinstance(entity.get("name"), str) and entity["name"]
                      and isinstance(entity.get("type"), str) and entity["type"]
                      and entity["type"] not in _NON_REPAIR_TARGETS
                      and type(entity.get("unit_number")) is int and entity["unit_number"] > 0
                      and _position(entity.get("position"))
                      and _finite(entity.get("health")) and _finite(entity.get("max_health"))
                      and 0 < entity["health"] < entity["max_health"]]
        if not candidates:
            return None
        def distance(entity):
            return sum((entity["position"][axis] - obs["position"][axis]) ** 2 for axis in ("x", "y"))
        target = min(candidates, key=lambda entity: (entity["health"] / entity["max_health"],
                                                    distance(entity), entity["unit_number"]))
        if not self._quiet(obs, target, defense):
            return None
        carried = (obs.get("inventory") or {}).get("repair-pack", 0)
        if carried > 1:
            return {"status": "blocked", "reason": "exactly one carried pack required",
                    "evidence": {"item": "repair-pack", "count": carried, "required_count": 1}}
        if not (obs.get("enabled_recipes") or {}).get("repair-pack"):
            unlock = self.factory.request_recipe_unlock(obs, "repair-pack")
            if unlock.get("type") or unlock.get("status") in {"blocked", "failed"}:
                return unlock
            technologies = sorted(name for name, technology in self.catalog.technologies.items()
                                  if "repair-pack" in (technology.get("unlocks") or []))
            original = self.factory.priority_research
            current = obs.get("research")
            priorities = ([current] if isinstance(current, str) and current else []) + technologies + original
            try:
                self.factory.priority_research = list(dict.fromkeys(priorities))
                action = self.factory.next_action(obs)
                # Startup research precedes Factory's ordinary priority list.
                # Its proposed switch has no material debit; retain the finite
                # active project while passing every other dependency action on.
                if (isinstance(current, str) and current and action.get("type") == "research"
                        and action.get("technology") != current):
                    return {"status": "waiting", "reason": "preserve active research before repair capability research",
                            "evidence": {"technology": current, "deferred_technology": action.get("technology")}}
                return action
            finally:
                self.factory.priority_research = original
        pack = self.bootstrap.ensure_item(obs, "repair-pack", 1)
        if pack.get("type") or pack.get("status") != "succeeded":
            return pack
        if self.game.backend == "assisted" and distance(target) > 16:
            return {"type": "move", "position": dict(target["position"]),
                    "reason": "approach owned damaged entity for native repair"}
        return {"type": "repair", "name": target["name"], "position": dict(target["position"]),
                "expected_world_id": obs["world_id"], "expected_actor_unit": obs["actor_unit_number"],
                "expected_entity_unit": target["unit_number"],
                "reason": "repair owned damaged entity with an ordinarily acquired repair pack"}
