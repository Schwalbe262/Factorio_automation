"""A finite, normally filled construction-belt chest beside an owned output."""
from __future__ import annotations

from copy import deepcopy
import json

from .factory_templates import DIRECTIONS


BUFFER_KEY = "construction:transport-belt:buffer"


def _find(obs: dict, spec: dict) -> dict | None:
    return next((e for e in obs.get("entities", []) if e.get("name") == spec["name"]
                 and e.get("position") == spec["position"]), None)


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


def _reserve(factory, obs: dict, owner: dict, output: dict) -> dict | None:
    dx, dy = DIRECTIONS[output["facing"]]
    occupied = factory.builder._occupied_by_plan(factory._reserved()) | factory._port_clearances()
    # Prefer the penultimate output belt, keeping the canonical downstream
    # endpoint and its straight continuation available to science consumers.
    for back in (1, 0):
        pickup = {"x": output["position"]["x"] - back * dx, "y": output["position"]["y"] - back * dy}
        source = next((e for e in owner["entities"] if e["name"] == "transport-belt"
                       and e["position"] == pickup and e.get("direction", 0) == output["facing"]), None)
        if source is None or _find(obs, source) is None:
            continue
        for outward in ((output["facing"] - 4) % 16, (output["facing"] + 4) % 16):
            sx, sy = DIRECTIONS[outward]
            arm = {"name": "inserter", "position": {"x": pickup["x"] + sx, "y": pickup["y"] + sy},
                   "direction": (outward + 8) % 16}
            chest = {"name": "wooden-chest", "position": {"x": pickup["x"] + 2 * sx, "y": pickup["y"] + 2 * sy},
                     "direction": 0}
            equipment = [chest, arm]
            if _find(obs, chest) is not None or _find(obs, arm) is not None:
                continue  # Do not repurpose an unreserved player chest or inserter.
            if factory.builder._occupied_by_plan(equipment) & occupied:
                continue
            existing = [e for e in factory._reserved() if e["name"] == "small-electric-pole"
                        and all(abs(e["position"][axis] - arm["position"][axis]) <= 2 for axis in ("x", "y"))]
            for pole in existing + factory._intake_poles(arm["position"], equipment):
                if pole not in existing and factory.builder._occupied_by_plan([pole]) & occupied:
                    continue
                entities = [chest, pole, arm]
                if not factory.builder.can_place(entities).get("ok"):
                    continue
                plan = {"ok": True, "entities": entities, "ports": [], "source": deepcopy(source),
                        "source_port": deepcopy(output), "source_key": "recipe:transport-belt", "slots": 1}
                reserved = factory.register_plan(BUFFER_KEY, plan, obs)
                if reserved.get("ok"):
                    return reserved
    return None


def ensure_construction_buffer(factory, obs: dict) -> dict | None:
    """Return ordinary repair/build/bar actions; empty buffers never cause waits."""
    factory.bootstrap.construction_buffers.pop("transport-belt", None)
    owner = factory.state.get("blocks", {}).get("recipe:transport-belt")
    if not owner:
        return None
    machine = next((e for e in owner["entities"] if e.get("recipe") == "transport-belt"), None)
    actual_machine = _find(obs, machine) if machine else None
    if not actual_machine or actual_machine.get("recipe") != "transport-belt":
        return None
    output = next((p for p in owner.get("ports", []) if p.get("kind") == "item"
                   and p.get("item") == "transport-belt" and p.get("direction") == "output"
                   and p.get("facing") in DIRECTIONS), None)
    if output is None:
        return None
    plan = factory.state["blocks"].get(BUFFER_KEY)
    if plan is None:
        plan = _reserve(factory, obs, owner, output)
    if plan is None:
        return None  # A compact buffer is an optimization, not a new production prerequisite.
    source = plan["source"]
    actual_source = _find(obs, source)
    if plan.get("source_port") != output or source not in owner["entities"]:
        return {"status": "blocked", "reason": "construction buffer source ownership changed", "evidence": {}}
    if actual_source is None:
        return factory.builder.ensure_plan(obs, {"ok": True, "entities": [source]})
    if actual_source.get("direction", 0) != source.get("direction", 0):
        return {"type": "mine", "name": source["name"], "position": source["position"], "count": 1,
                "expected_entity_unit": actual_source["unit_number"], "expected_entity_world_id": obs["world_id"],
                "reason": "recover the owned construction output belt before restoring its reserved direction"}
    chest, pole, arm = plan["entities"]
    actual_chest, actual_arm = _find(obs, chest), _find(obs, arm)
    if actual_chest is None:
        if actual_arm is not None:
            # Stop the owned feeder before replacing a destroyed chest. Its
            # replacement must be barred before any automatic transfer resumes.
            return {"type": "mine", "name": arm["name"], "position": arm["position"], "count": 1,
                    "expected_entity_unit": actual_arm["unit_number"], "expected_entity_world_id": obs["world_id"],
                    "reason": "recover construction buffer feeder before replacing its bounded chest"}
        return factory.builder.ensure_plan(obs, {"ok": True, "entities": [chest]})
    payload = json.dumps(json.dumps({"world": obs["world_id"], "source": source,
        "source_unit": actual_source["unit_number"], "chest": chest, "chest_unit": actual_chest["unit_number"]}, separators=(",", ":")))
    survey = factory.game.query('''
local x=helpers.json_to_table(''' + payload + ''')
if not d or d.world_id~=x.world then return failure("buffer_world_changed") end
local source=target(x.source.position,x.source.name);local chest=target(x.chest.position,x.chest.name)
if not source or source.force~=f or source.unit_number~=x.source_unit or source.direction~=(x.source.direction or 0)
 or not chest or chest.force~=f or chest.unit_number~=x.chest_unit then return failure("buffer_identity_changed") end
local inv=chest.get_inventory(defines.inventory.chest)
if not inv or not inv.supports_bar() then return failure("buffer_bar_unsupported") end
for _,row in pairs(inv.get_contents()) do
 if row.name~="transport-belt" then return failure("buffer_chest_contaminated") end
end
for lane=1,2 do for _,row in pairs(source.get_transport_line(lane).get_contents()) do
 if row.name~="transport-belt" then return failure("buffer_source_contaminated") end
end end
return success{slots=inv.get_bar()-1}
''')
    if not survey.get("ok"):
        return {"status": "blocked", "reason": "cannot verify bounded construction chest", "evidence": survey}
    if survey.get("slots") != 1:
        move = factory.builder._move(obs, chest["position"])
        return move or {"type": "bar", "name": chest["name"], "position": chest["position"], "slots": 1,
                        "reason": "reserve one normal item stack for construction belts"}
    result = factory.builder.ensure_plan(obs, plan)
    if not _ready(result):
        return result
    result = factory.ensure_power_connection(obs, BUFFER_KEY, plan)
    if not _ready(result):
        return result
    factory.bootstrap.construction_buffers["transport-belt"] = {
        "world_id": obs["world_id"], "unit_number": actual_chest["unit_number"],
        "name": chest["name"], "position": chest["position"]}
    return None
