"""Normal Space Age starter-pack launch, with save-persistent retry evidence.

LAUNCH_LUA is an action fragment for DeterministicGame.act. It expects its
existing locals x, a, inv, s, f, d and target/success/failure helpers. The
observation fragment is a query body using d/f. No progress fields are written.
"""

from __future__ import annotations

from typing import Any, Mapping

from .world_catalog import WorldCatalog


LAUNCH_LUA = r'''
local state=d.first_rocket_launch
if state and state.ordered then
 if not state.platform or not state.platform.valid then return failure("launch_platform_lost") end
 local hub=state.platform.hub
 if hub and hub.valid and f.rockets_launched>state.baseline then
  return success{status="succeeded",rockets_launched=f.rockets_launched,platform_index=state.platform.index}
 end
 return success{status="running",reason="starter_pack_in_transit",rockets_launched=f.rockets_launched}
end
local silo=target(x.position,x.name or "rocket-silo")
if not silo or silo.type~="rocket-silo" or silo.force~=f then return failure("rocket_silo_missing") end
if state and state.silo_unit_number~=silo.unit_number then return failure("launch_silo_changed") end
if state and state.platform and not state.platform.valid then return failure("launch_platform_lost") end
local tech=f.technologies["rocket-silo"]
if not tech or not tech.researched then return failure("rocket_silo_research_required") end
if silo.rocket_silo_status~=defines.rocket_silo_status.rocket_ready then
 return success{status="waiting",reason="rocket_not_ready",rocket_parts=silo.rocket_parts}
end
local cargo=nil
--[[ In 2.1 the rocket inventory is virtual (its size can be zero). Inserting
through it prepares launch cargo; the attached unit alone does not do so. ]]
for _,name in ipairs({"rocket_silo_rocket","rocket_silo_attached_cargo_unit"}) do
 local index=defines.inventory[name]
 if index then local good,value=pcall(function() return silo.get_inventory(index) end);if good and value then cargo=value;break end end
end
if not cargo or not cargo.valid then return failure("rocket_cargo_unavailable") end
local pack="space-platform-starter-pack"
for _,item in pairs(cargo.get_contents()) do
 if item.name~=pack then return failure("conflicting_rocket_cargo:"..item.name) end
end
if cargo.get_item_count(pack)<1 then
 if inv.get_item_count(pack)<1 then return failure("starter_pack_required") end
 local inserted=cargo.insert{name=pack,count=1}
 if inserted~=1 then return failure("rocket_cargo_rejected") end
 if inv.remove{name=pack,count=1}~=1 then cargo.remove{name=pack,count=1};return failure("starter_pack_transfer_failed") end
end
if not state then
 state={baseline=f.rockets_launched,silo_unit_number=silo.unit_number,created_tick=game.tick}
 d.first_rocket_launch=state
end
if not state.platform then
 state.platform=f.create_space_platform{name=x.platform_name or "Autonomous first launch",planet=s.name,starter_pack=pack}
 if not state.platform then return failure("platform_request_rejected") end
end
if not silo.launch_rocket({type=defines.cargo_destination.space_platform,space_platform=state.platform}) then
 return success{status="waiting",reason="launch_not_ready",platform_index=state.platform.index}
end
state.ordered=true
state.ordered_tick=game.tick
return success{status="running",reason="launch_ordered",platform_index=state.platform.index,baseline=state.baseline}
'''


OBSERVE_LAUNCH_LUA = r'''
local state=d and d.first_rocket_launch
if not state then return {status="not_started",rockets_launched=f.rockets_launched} end
local platform=state.platform
local valid=platform and platform.valid or false
local hub=valid and platform.hub
local complete=state.ordered and hub and hub.valid and f.rockets_launched>state.baseline or false
return {status=complete and "succeeded" or state.ordered and "running" or "waiting",
 baseline=state.baseline,ordered=state.ordered or false,platform_valid=valid,
 platform_index=valid and platform.index or nil,platform_hub_valid=hub and hub.valid or false,
 rockets_launched=f.rockets_launched,silo_unit_number=state.silo_unit_number,
 ordered_tick=state.ordered_tick}
'''


class LaunchStage:
    def __init__(self, catalog: WorldCatalog):
        self.catalog = catalog
        self.parts_required = int(catalog.entities["rocket-silo"]["rocket_parts_required"])

    def next_action(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        """Return {status, reason, action, requirements}; action is adapter-ready.

        Merge the result of OBSERVE_LAUNCH_LUA into observation['launch'] when
        observing. Other launch counters alone never establish this run's success.
        """
        launch = observation.get("launch") or {}
        if (launch.get("ordered") and launch.get("platform_hub_valid")
                and launch.get("rockets_launched", observation.get("rockets_launched", 0)) > launch.get("baseline", float("inf"))):
            return {"status": "succeeded", "reason": "starter_pack_delivered", "action": None, "requirements": []}
        if launch.get("ordered"):
            if launch.get("platform_valid") is False:
                return {"status": "blocked", "reason": "launch_platform_lost", "action": None, "requirements": []}
            return {"status": "running", "reason": "starter_pack_in_transit", "action": None, "requirements": []}
        silos = [entity for entity in observation.get("entities", []) if entity.get("name") == "rocket-silo"]
        if launch.get("silo_unit_number") is not None:
            silos = [silo for silo in silos if silo.get("unit_number") == launch["silo_unit_number"]]
        if not silos:
            return {"status": "blocked", "reason": "rocket_silo_missing", "action": None,
                    "requirements": [{"kind": "build", "item": "rocket-silo", "count": 1}]}
        silo = min(silos, key=lambda entity: entity.get("unit_number", 0))
        remaining = (0 if silo.get("rocket_silo_status") == "rocket_ready" else
                     max(0, self.parts_required - int(silo.get("rocket_parts", 0))))
        if remaining:
            return {"status": "waiting", "reason": "rocket_parts_required", "action": None,
                    "requirements": [{"kind": "produce", "item": "rocket-part", "count": remaining,
                                      "machine_unit_number": silo.get("unit_number")}]}
        pack = "space-platform-starter-pack"
        if not silo.get("inventory", {}).get(pack) and not observation.get("inventory", {}).get(pack):
            source = next((entity for entity in observation.get("entities", []) if entity is not silo and entity.get("inventory", {}).get(pack, 0) > 0), None)
            if source:
                return {"status": "ready", "reason": "collect_starter_pack", "requirements": [],
                        "action": {"type": "take", "position": source["position"], "name": source["name"], "item": pack, "count": 1}}
            return {"status": "blocked", "reason": "starter_pack_required", "action": None,
                    "requirements": [{"kind": "produce", "item": pack, "count": 1}]}
        return {"status": "ready", "reason": "request_launch", "requirements": [],
                "action": {"type": "launch", "position": silo["position"], "name": "rocket-silo"}}
