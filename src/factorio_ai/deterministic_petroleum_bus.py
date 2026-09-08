"""Preserve existing petroleum consumers when advanced refining is introduced."""
from __future__ import annotations

import json


def ensure_petroleum_bus(fluids, observation: dict, advanced: dict) -> dict:
    """Join both owned refinery outlets before admitting advanced refinery feeds.

    Existing consumer links keep their original basic-refinery endpoint. The
    regular pipe builder pays for the join and rechecks its actual segment on
    every pass, including reloads and destroyed/rebuilt pipes.
    """
    basic = fluids.state.get("sources", {}).get("basic-oil-processing")
    if basic is None:
        return {"status": "succeeded", "reason": "no earlier basic petroleum network"}

    ports = []
    for plan in (advanced, basic):
        candidates = [p for p in plan.get("ports", [])
                      if p.get("kind") == "fluid" and p.get("item") == "petroleum-gas"
                      and p.get("direction") == "output" and p.get("machine_index", 0) == 0]
        if len(candidates) != 1:
            return {"status": "blocked", "reason": "owned refinery has no unique primary petroleum outlet"}
        ports.append(candidates[0])

    # The basic plan may need ordinary repairs after a rollback or destruction.
    # Do not trust a saved route whose original endpoint has disappeared.
    result = fluids.builder.ensure_plan(observation, basic)
    if result.get("status") != "succeeded" or result.get("type"):
        return result
    result = fluids.factory.ensure_power_connection(observation, "fluid:basic-oil-processing", basic)
    if result.get("status") != "succeeded" or result.get("type"):
        return result
    payload = json.dumps(json.dumps([p["position"] for p in ports], separators=(",", ":")))
    proof = fluids.game.query('''
--[[ petroleum_bus_endpoints: never connect an unknown or foreign fluid. ]]
local points=helpers.json_to_table(''' + payload + ''')
for _,p in ipairs(points) do
 local e=target(p,"pipe")
 if not e or e.force~=f or not e.get_fluid_segment_id(1) then
  return {ok=false,reason="owned petroleum endpoint is missing"}
 end
 for name,amount in pairs(e.get_fluid_contents()) do
  if amount>0 and name~="petroleum-gas" then return {ok=false,reason="petroleum endpoint contains another fluid"} end
 end
end
return {ok=true,world_id=d and d.world_id}
''')
    if not isinstance(proof, dict) or not proof.get("ok") or proof.get("world_id") != observation.get("world_id"):
        return {"status": "blocked", "reason": "owned petroleum bus endpoints are not verified"}
    # Advanced crude is still gated. Its verified, empty segment must therefore
    # offer alternate taps when the original outlet already feeds its tank.
    source = {**ports[0], "allow_empty_segment": True}
    return fluids._connect_pipe(observation, source, ports[1], "petroleum-bus:advanced-to-basic",
                                {"entities": advanced["entities"] + basic["entities"]})
