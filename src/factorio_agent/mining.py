"""Bounded extra starter cells; final mining still needs automated belt supply.

This is capital bootstrapping, not a substitute for scalable electric mining.
The model chooses the absolute cell target rather than individual placements.
"""

from __future__ import annotations

import json
from pathlib import Path

from factorio_agent.core.deterministic_bootstrap import SURVEY_LAYOUT_TICKS
from factorio_agent.core.deterministic_state import _atomic_json
from .construction_supply import FUELS, FUEL_SKIP_STOCK


UNLOCK_TECHNOLOGY = "electric-mining-drill"
BULK_RESOURCES = frozenset({"iron-ore", "copper-ore"})
CELLS_AFTER_UNLOCK = 2  # a plate resource keeps (or recovers) at most this many burner cells after the unlock
WORKING_DRILL = frozenset({"working", "waiting_for_space_in_destination"})


def _researched(technologies, name: str) -> bool:
    if isinstance(technologies, dict):
        return technologies.get(name) is True
    return name in (technologies or ())


def closed_by_unlock(resource: str, cells: int, technologies) -> bool:
    """A plate cell target above ``CELLS_AFTER_UNLOCK`` once electric mining is researched.

    Plate supply then grows by outposts and the district (``throughput_growth``);
    the planner does not propose such a target and an open job closes on it.
    Only under the ``cap`` plate policy (``capacity_planning.PLATE_CELL_POLICY``): with
    ``demand`` the outposts' ore waits on belts until the district's first bank smelts it
    (aud-1114-0436: outposts 16.8, producer 21.1), so the cells stay the plate source until
    they retire and the payback rule, not the unlock, ends their growth.
    """
    from .capacity_planning import demand_driven

    if demand_driven():
        return False
    return resource in BULK_RESOURCES and cells > CELLS_AFTER_UNLOCK and _researched(technologies, UNLOCK_TECHNOLOGY)


def validate_parameters(parameters: dict) -> None:
    from .capacity_planning import MAX_STARTER_CELLS

    if not isinstance(parameters, dict) or set(parameters) != {"resource", "cells"}:
        raise ValueError("starter_capacity requires resource and cells")
    if parameters["resource"] not in {"iron-ore", "copper-ore", "coal", "stone"}:
        raise ValueError("unsupported starter resource")
    if type(parameters["cells"]) is not int or not 1 <= parameters["cells"] <= MAX_STARTER_CELLS:
        raise ValueError(f"starter cell target must be an integer between 1 and {MAX_STARTER_CELLS}")


class StarterCapacity:
    def __init__(self, game, bootstrap):
        self.game, self.bootstrap = game, bootstrap
        self.path = Path(game.cfg.runtime_dir) / "starter-capacity.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def cells(self, resource: str) -> list:
        result = self.bootstrap._existing_cells(resource, max_age_ticks=SURVEY_LAYOUT_TICKS)
        if not result.get("ok"):
            raise RuntimeError(result.get("reason", "cell_survey_failed"))
        return [c for c in result.get("cells", []) if c.get("receiver") and c.get("remaining", 0) > 0]

    def choose_site(self, resource: str, obs: dict | None = None) -> dict:
        """The best new cell site; with ``obs`` it avoids every board block and planned-unbuilt entity."""
        from .board import KEEPOUT_LUA, starter_keepouts

        receiver = "stone-furnace" if resource in {"iron-ore", "copper-ore"} else "wooden-chest"
        keepouts = starter_keepouts(self.path.parent, obs) if obs is not None else {"areas": [], "tiles": []}
        return self.game.query(
            """
local resource="""
            + json.dumps(resource)
            + """;local receiver_name="""
            + json.dumps(receiver)
            + """;local keepouts=helpers.json_to_table("""
            + json.dumps(json.dumps(keepouts, separators=(",", ":")))
            + ")"
            + KEEPOUT_LUA
            + """
local function kept_out(p,r)
 if keepout_hit(p.x-1,p.y-1,p.x+1,p.y+1) then return true end
 if receiver_name=="wooden-chest" then return keepout_hit(r.x-.5,r.y-.5,r.x+.5,r.y+.5) end
 return keepout_hit(r.x-1,r.y-1,r.x+1,r.y+1)
end
local seen={};local best=nil;local best_score=-math.huge
for _,ore in pairs(s.find_entities_filtered{position={0,0},radius=384,name=resource,limit=2000}) do
 local x=math.floor(ore.position.x);local y=math.floor(ore.position.y);local key=x..":"..y
 if not seen[key] then
  seen[key]=true
  local p={x=x,y=y};local r={x=x,y=y-2}
  if receiver_name=="wooden-chest" then r={x=x-0.5,y=y-1.5} end
  if not kept_out(p,r) and s.can_place_entity{name="burner-mining-drill",position=p,direction=0,force=f}
    and s.can_place_entity{name=receiver_name,position=r,direction=0,force=f} then
   local richness=0;local tiles=0;local mixed=false
   for _,entity in pairs(s.find_entities_filtered{area={{x-1,y-1},{x+1,y+1}},type="resource"}) do
    if entity.name==resource then richness=richness+entity.amount;tiles=tiles+1 else mixed=true end
   end
   local score=math.min(tiles,4)*1000000+math.min(richness,1000)-x*x-y*y
   if richness>0 and not mixed and score>best_score then
    best_score=score;best={ok=true,drill={name="burner-mining-drill",position=p,direction=0},
     receiver={name=receiver_name,position=r,direction=0}}
   end
  end
 end
end
return best or {ok=false,reason="no_clear_starter_capacity_site"}
"""
        )

    @staticmethod
    def matching(obs: dict, part: dict):
        return next(
            (
                e
                for e in obs.get("entities", [])
                if e["name"] == part["name"]
                and abs(e["position"]["x"] - part["position"]["x"]) < 0.1
                and abs(e["position"]["y"] - part["position"]["y"]) < 0.1
            ),
            None,
        )

    def proof(self, obs: dict, parameters: dict) -> dict | None:
        resource, count = parameters["resource"], parameters["cells"]
        cells = self.cells(resource)
        productive = []
        for cell in cells:
            receiver = self.matching(obs, cell["receiver"])
            if not receiver:
                continue
            # Fuel upkeep deliberately stops feeding a cell whose receiver
            # already holds FUEL_SKIP_STOCK finished items. Such a cell is idle
            # by design and still counts; an empty unfuelled cell does not.
            finished = receiver.get("output_inventory")
            if finished is None:
                finished = {k: v for k, v in (receiver.get("inventory") or {}).items() if k not in FUELS}
            stocked = sum(finished.values()) >= FUEL_SKIP_STOCK
            if not (stocked or cell.get("fuel", 0) > 0 or cell.get("burning") or cell.get("operating")):
                continue
            produced = (
                receiver.get("products_finished", 0)
                if receiver["name"] == "stone-furnace"
                else sum(receiver.get("inventory", {}).values())
            )
            if produced <= 0 and receiver["name"] != "stone-furnace":
                # A chest the collector just emptied holds nothing; its drill mining into it
                # (native status) is the same production evidence (seeds 2212603376, 1590659659:
                # a two-cell coal job collected every pass and ran 30 minutes into its deadline).
                drill = self.matching(obs, cell["drill"])
                produced = int(bool(drill and drill.get("status_name") in WORKING_DRILL))
            if produced > 0:
                productive.append({"drill": cell["drill"], "receiver": cell["receiver"], "observed_output": produced})
        if len(productive) < count and closed_by_unlock(resource, count, obs.get("technologies")):
            # Closed, not reached: electric mining replaces bulk plate cells (closed_by_unlock).
            return {
                "tick": obs["tick"],
                "resource": resource,
                "target_cells": count,
                "productive_cells": len(productive),
                "closed_by_technology": UNLOCK_TECHNOLOGY,
                "next": "mining_outpost and smelting_capacity (throughput_growth) grow this plate",
            }
        if len(productive) < count:
            return None
        return {
            "tick": obs["tick"],
            "resource": resource,
            "productive_cells": len(productive),
            "cells": productive,
            "fuel_mode": "bounded starter upkeep; not automated belt mining",
        }

    def step(self, obs: dict, parameters: dict) -> dict:
        validate_parameters(parameters)
        if self.state.get("world_id") != obs["world_id"]:
            self.state = {"world_id": obs["world_id"], "pending": {}}
        resource, wanted = parameters["resource"], parameters["cells"]
        # Completion has priority over unrelated starter fuel chores. Otherwise
        # a paid, productive target can loop forever replenishing other cells.
        if len(self.cells(resource)) >= wanted:
            proof = self.proof(obs, parameters)
            if proof:
                return {"status": "succeeded", "reason": "starter capacity has live machine output", "evidence": proof}
        if closed_by_unlock(resource, wanted, obs.get("technologies")):
            # No new burner investment; paid cells and the pending site stay. The job closes
            # (proof: closed_by_technology) instead of blocking and holding its lane.
            return {
                "status": "succeeded",
                "reason": "starter_capacity_closed_by_electric_mining_unlock",
                "evidence": {"resource": resource, "target_cells": wanted, "closed_by_technology": UNLOCK_TECHNOLOGY},
            }
        fuel = self.bootstrap.maintain_fuel(obs)
        if fuel:
            return fuel
        if len(self.cells(resource)) >= wanted:
            proof = self.proof(obs, parameters)
            return (
                {"status": "succeeded", "reason": "starter capacity has live machine output", "evidence": proof}
                if proof
                else {"status": "waiting", "reason": "waiting for every target cell to produce"}
            )
        pending = self.state["pending"].get(resource)
        if pending and all(self.matching(obs, p) for p in (pending["receiver"], pending["drill"])):
            self.state["pending"].pop(resource)
            pending = None
        if pending is None:
            pending = self.choose_site(resource, obs)
            if not pending.get("ok"):
                return {"status": "blocked", "reason": pending.get("reason", "capacity_survey_failed")}
            self.state["pending"][resource] = pending
            _atomic_json(self.path, self.state)
        for part in (pending["receiver"], pending["drill"]):
            if self.matching(obs, part):
                continue
            if obs.get("inventory", {}).get(part["name"], 0) < 1:
                return self.bootstrap.ensure_item(obs, part["name"], 1)
            return {"type": "build", **part, "reason": "expand " + resource + " starter capacity"}
        return {"status": "waiting", "reason": "waiting for new cell verification"}
