"""Finite natural-research bridge to the first machines; never ongoing science.

Only Automation and electric mining may use this starter handcraft allowance.
All subsequent science must come from the normal production capabilities.
"""

from __future__ import annotations

import json
from pathlib import Path

from factorio_agent.construction_supply import SMALL_TRANSFER
from factorio_agent.core.deterministic_state import _atomic_json


TARGETS = {"automation", "electric-mining-drill"}
# Lab centres tried around each powered pole, nearest first. The first eight
# are the historical ring; the ring at 3 still overlaps the small pole's
# supply area (2.5) with the 3x3 lab. Needed once the lab keeps off the
# reserved coal feed: on seed 2005199778 the feed belt ran past every pole of
# the plant and the eight-site ring found no site (no_powered_lab_site).
LAB_OFFSETS = [(2, 0), (-2, 0), (0, 2), (0, -2), (2, 2), (-2, -2), (2, -2), (-2, 2)] + sorted(
    ((dx, dy) for dx in range(-3, 4) for dy in range(-3, 4) if max(abs(dx), abs(dy)) == 3),
    key=lambda o: (abs(o[0]) + abs(o[1]), o),
)
LAB_OFFSETS_LUA = "{" + ",".join("{%d,%d}" % o for o in LAB_OFFSETS) + "}"


def validate_parameters(parameters: dict) -> None:
    if not isinstance(parameters, dict) or set(parameters) != {"technology"} or parameters["technology"] not in TARGETS:
        raise ValueError("starter_research permits only automation or electric-mining-drill")


class StarterResearch:
    def __init__(self, game, bootstrap, catalog):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.path = Path(game.cfg.runtime_dir) / "starter-research.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def lab(self, obs: dict) -> dict:
        # A connected lab wins: a prebuilt science-cell lab slot may still
        # wait for its pole while the starter lab already researches.
        labs = [entity for entity in obs.get("entities", []) if entity["name"] == "lab"]
        connected = [entity for entity in labs if entity.get("electric_network_connected")]
        if connected:
            # The lab already holding or consuming the packs stays the starter lab.
            busy = [
                e
                for e in connected
                if (e.get("inventory") or {}).get("automation-science-pack") or e.get("status_name") == "working"
            ]
            return {"status": "succeeded", "entity": (busy or connected)[0]}
        if labs:
            return {"status": "blocked", "reason": "existing_lab_not_connected"}
        site = self.state.get("lab_site")
        if site is None:
            # The lab stands on free land near power, so it keeps off every
            # reserved board block and planned-but-unbuilt entity (seed
            # 2005199778: the lab took a tile of the reserved coal feed belt).
            from .board import KEEPOUT_LUA, starter_keepouts

            keepouts = starter_keepouts(self.path.parent, obs)
            payload = json.dumps(json.dumps(keepouts, separators=(",", ":")))
            site = self.game.query(
                "local keepouts=helpers.json_to_table("
                + payload
                + ");local LAB_OFFSETS="
                + LAB_OFFSETS_LUA
                + KEEPOUT_LUA
                + """
local supplied={}
for _,generator in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if generator.electric_network_id and generator.energy>0 then supplied[generator.electric_network_id]=true end
end
for _,pole in pairs(s.find_entities_filtered{force=f,type="electric-pole"}) do
 if pole.electric_network_id and supplied[pole.electric_network_id] then
  for _,offset in ipairs(LAB_OFFSETS) do
   local p={x=math.floor(pole.position.x)+0.5+offset[1],y=math.floor(pole.position.y)+0.5+offset[2]}
   if not keepout_hit(p.x-1.5,p.y-1.5,p.x+1.5,p.y+1.5) and s.can_place_entity{name="lab",position=p,force=f} then return {ok=true,position=p} end
  end
 end
end
return {ok=false,reason="no_powered_lab_site"}
"""
            )
            if not site.get("ok"):
                return {"status": "blocked", "reason": site.get("reason", "lab_site_query_failed")}
            self.state["lab_site"] = site
            _atomic_json(self.path, self.state)
        if obs.get("inventory", {}).get("lab", 0) < 1:
            return self.bootstrap.ensure_item(obs, "lab", 1)
        return {
            "type": "build",
            "name": "lab",
            "position": site["position"],
            "direction": 0,
            "reason": "build startup research lab next to operating power",
        }

    def step(self, obs: dict, parameters: dict) -> dict:
        validate_parameters(parameters)
        name = parameters["technology"]
        if self.state.get("world_id") != obs["world_id"]:
            self.state = {"world_id": obs["world_id"], "allowances": {}}
        if obs.get("technologies", {}).get(name):
            return {
                "status": "succeeded",
                "reason": "target researched naturally",
                "evidence": {
                    "technology": name,
                    "tick": obs["tick"],
                    "science": obs.get("production", {}).get("automation-science-pack", {}),
                },
            }
        tech = self.catalog.technologies[name]
        if any(not obs.get("technologies", {}).get(parent) for parent in tech["prerequisites"]):
            return {"status": "blocked", "reason": "research_prerequisites_missing"}
        if tech.get("research_trigger") or tech["ingredients"] != [
            {"amount": 1, "name": "automation-science-pack", "type": "item"}
        ]:
            return {"status": "blocked", "reason": "startup_research_rules_changed"}
        result = self.lab(obs)
        if result.get("status") != "succeeded":
            return result
        lab = result["entity"]
        if obs.get("research") not in (None, name):
            return {"status": "blocked", "reason": "another_research_is_running"}
        if obs.get("research") != name:
            return {"type": "research", "technology": name, "reason": "start model-selected natural research"}
        item = "automation-science-pack"
        allowance = self.state["allowances"].get(name)
        if allowance is None:
            # Persist before crafting. A retry/restart/rollback cannot replenish
            # this finite handcraft budget. Existing packs need no extra funding.
            allowance = {"budget": int(tech["unit_count"]), "reserved_output": 0, "created_tick": obs["tick"]}
            self.state["allowances"][name] = allowance
            _atomic_json(self.path, self.state)
        held = obs.get("inventory", {}).get(item, 0)
        lab_packs = lab.get("inventory", {}).get(item, 0)
        if held and held <= SMALL_TRANSFER and lab_packs and obs.get("crafting_queue"):
            # Fold the packs the queue is still crafting into one insert: the lab holds packs to
            # research on (aud-1114-0930: 97 of 102 startup science inserts were 1-2 packs).
            return {
                "status": "waiting",
                "reason": "lab consumes normal startup science",
                "evidence": {"technology": name, "packs_in_lab": lab_packs, "carried": held},
            }
        if held:
            return {
                "type": "insert",
                "name": "lab",
                "position": lab["position"],
                "inventory": "lab_input",
                "item": item,
                "count": min(held, int(tech["unit_count"])),
                "reason": "supply finite startup science",
            }
        if lab_packs or lab.get("status_name") == "working":
            return {
                "status": "waiting",
                "reason": "lab consumes normal startup science",
                "evidence": {
                    "technology": name,
                    "progress": obs.get("research_progress", 0),
                    "packs_in_lab": lab_packs,
                },
            }
        if obs.get("crafting_queue"):
            return {
                "status": "waiting",
                "reason": "engine is crafting the reserved startup science batch",
                "evidence": {"technology": name, "reserved_output": allowance["reserved_output"]},
            }
        remaining = allowance["budget"] - allowance["reserved_output"]
        if remaining <= 0:
            return {"status": "blocked", "reason": "startup_science_allowance_exhausted_requires_automatic_production"}
        action = self.bootstrap.ensure_item(obs, item, remaining)
        if action.get("type") == "craft" and action.get("recipe") == item:
            recipe = self.catalog.recipe_for_product(item)
            output = sum(p.get("amount", 0) for p in recipe["products"] if p["name"] == item)
            reserved = output * action["count"]
            if reserved > remaining:
                return {"status": "blocked", "reason": "startup_science_batch_exceeds_allowance"}
            allowance["reserved_output"] += reserved
            _atomic_json(self.path, self.state)
        return action

    @staticmethod
    def proof(obs: dict, parameters: dict) -> dict | None:
        if not obs.get("technologies", {}).get(parameters["technology"]):
            return None
        return {
            "tick": obs["tick"],
            "technology": parameters["technology"],
            "science_statistics": obs.get("production", {}).get("automation-science-pack", {}),
            "research_mode": "finite normal starter science",
        }
