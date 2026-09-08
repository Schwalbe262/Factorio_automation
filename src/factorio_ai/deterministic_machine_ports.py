"""Conservative solid-cell arm budgets and ordinary, owned inserter upgrades.

40/100 items/min are engineering allowances for single-item basic/fast arms,
not measured belt transfer guarantees. Installed base prototypes share the
pickup/drop geometry (rotation .014/.04 and extension .035/.1); belt pickup,
power and upstream route arms still require sustained flow verification.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

from .factory_templates import DIRECTIONS


ARM_BUDGETS = {"inserter": 40.0, "fast-inserter": 100.0}


def _blocked(reason: str, **evidence: Any) -> dict:
    return {"status": "blocked", "reason": reason, "evidence": evidence}


def _ports(plan: dict, recipe: dict, item: str) -> list[dict]:
    output = sum(float(row.get("amount", 1)) * float(row.get("probability", 1))
                 for row in recipe["products"] if row["name"] == item)
    if not output > 0:
        raise ValueError("solid cell output yield is missing")
    rows = []
    for port in plan["ports"]:
        if port.get("kind") != "item":
            continue
        ingredients = recipe["ingredients"] if port["direction"] == "input" else recipe["products"]
        amount = sum(float(row.get("amount", 1)) * float(row.get("probability", 1))
                     for row in ingredients if row["name"] == port["item"])
        if amount == 0:  # Furnace fuel is accounted for separately.
            continue
        dx, dy = DIRECTIONS[port["facing"]]
        sign = 1 if port["direction"] == "input" else -1
        position = {"x": port["position"]["x"] + 2 * dx * sign,
                    "y": port["position"]["y"] + 2 * dy * sign}
        arms = [e for e in plan["entities"] if e["position"] == position and e["name"] in ARM_BUDGETS
                and e.get("direction", 0) == (port["facing"] + 8) % 16]
        if len(arms) != 1:
            raise ValueError("solid cell has unsupported or missing machine port arm")
        rows.append({"port": port, "arm": arms[0], "ratio": amount / output,
                     "belt": {"name": "transport-belt", "position": {
                         "x": port["position"]["x"] + dx * sign,
                         "y": port["position"]["y"] + dy * sign}, "direction": port["facing"]}})
    required = {("input", row["name"]) for row in recipe["ingredients"]} | {("output", item)}
    if not required.issubset({(row["port"]["direction"], row["port"]["item"]) for row in rows}):
        raise ValueError("solid cell is missing a required recipe port")
    return rows


def _select_arm(plan: dict, arm: dict, name: str) -> None:
    counts = plan.get("required_items", {})
    if arm["name"] != name and arm["name"] in counts:
        counts[arm["name"]] -= 1
        if counts[arm["name"]] == 0:
            del counts[arm["name"]]
        counts[name] = counts.get(name, 0) + 1
    arm["name"] = name
    arm.pop("item", None)


def fast_available(factory: Any, obs: dict) -> bool:
    return bool(obs.get("enabled_recipes", {}).get("fast-inserter") or any(
        "fast-inserter" in tech.get("unlocks", []) for tech in factory.catalog.technologies.values()))


def cell_capacity(factory: Any, obs: dict, plan: dict, recipe: dict, item: str,
                  *, prefer_fast: bool = False) -> float:
    """Use the saved machine, since a newly unlocked tier need not be installed."""
    machines = [e for e in plan["entities"] if e.get("recipe") == recipe["name"]
                or e["name"] in {"stone-furnace", "steel-furnace"}]
    if len(machines) != 1:
        raise ValueError("solid port capacity requires exactly one saved machine")
    machine = machines[0]
    speed = factory.catalog.entities.get(machine["name"], {}).get("crafting_speed")
    if speed is None:
        speed = next((m["crafting_speed"] for m in factory.graph.machines_for_recipe(recipe["name"], obs)
                      if m["name"] == machine["name"]), 0)
    output = sum(float(row.get("amount", 1)) * float(row.get("probability", 1))
                 for row in recipe["products"] if row["name"] == item)
    rate = float(speed) * 60 / float(recipe["energy"]) * output
    for row in _ports(plan, recipe, item):
        budget = ARM_BUDGETS["fast-inserter" if prefer_fast else row["arm"]["name"]]
        rate = min(rate, budget / row["ratio"])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("solid cell has no positive machine and port capacity")
    return rate


def _survey(factory: Any, plan: dict, row: dict) -> dict:
    machine = next(e for e in plan["entities"] if e.get("recipe") or e["name"] in {"stone-furnace", "steel-furnace"})
    payload = json.dumps(json.dumps({"machine": machine, "arm": row["arm"], "belt": row["belt"],
        "input": row["port"]["direction"] == "input"}, separators=(",", ":")))
    return factory.game.query('''
local args=helpers.json_to_table(''' + payload + ''')
local machine=target(args.machine.position,args.machine.name)
local belt=target(args.belt.position,args.belt.name)
local basic=prototypes.entity["inserter"];local fast=prototypes.entity["fast-inserter"]
local function same(a,b) return a and b and math.abs((a.x or a[1])-(b.x or b[1]))<.01
 and math.abs((a.y or a[2])-(b.y or b[2]))<.01 end
if not machine or machine.force~=f or not belt or belt.force~=f or belt.direction~=args.belt.direction
 or not basic or not fast or not same(basic.inserter_pickup_position,fast.inserter_pickup_position)
 or not same(basic.inserter_drop_position,fast.inserter_drop_position) then return {ok=false} end
local function inside(p,e) local b=e.bounding_box
 return p.x>=b.left_top.x and p.x<=b.right_bottom.x and p.y>=b.left_top.y and p.y<=b.right_bottom.y end
local rows={}
for _,e in pairs(s.find_entities_filtered{position=args.arm.position,radius=.1}) do
 if e.type~="resource" and e~=a then
  local valid=e.type=="inserter" and e.force==f and e.minable and e.direction==args.arm.direction
  if valid then
   valid=inside(e.pickup_position,args.input and belt or machine)
     and inside(e.drop_position,args.input and machine or belt)
  end
  rows[#rows+1]={name=e.name,unit_number=e.unit_number,valid=valid}
 end
end
return {ok=true,world_id=d and d.world_id,rows=rows}
''')


def ensure_machine_ports(factory: Any, obs: dict, plan: dict, recipe: dict, item: str,
                         *, rate_per_minute: float | None = None) -> dict | None:
    """Reconcile saved upgrades before normal plan building; return one action.

    A locked rate request queues normal research without changing the plan.
    No-rate bootstrap calls therefore remain able to establish lab feeding and
    select that research. A rollback to before research restores basic plans.
    """
    upgrades = factory.state.get("machine_port_upgrades", {})
    try:
        rows = _ports(plan, recipe, item)
    except (KeyError, ValueError) as error:
        return _blocked(str(error), plan=plan.get("key"))
    for row in rows:
        arm = row["arm"]
        plan_key = plan.get("key")
        key = (plan_key + ":" + factory._entity_key({**arm, "name": "port"})
               if isinstance(plan_key, str) and plan_key else None)
        record = upgrades.get(key)
        needed = rate_per_minute is not None and rate_per_minute * row["ratio"] > ARM_BUDGETS[arm["name"]] + 1e-9
        if not record and not needed:
            continue
        if key is None:
            return _blocked("machine port upgrade requires a saved plan key")
        if needed and rate_per_minute * row["ratio"] > ARM_BUDGETS["fast-inserter"] + 1e-9:
            return _blocked("requested solid machine port exceeds fast inserter allowance", item=row["port"]["item"])
        enabled = obs.get("enabled_recipes", {}).get("fast-inserter")
        if not record and not enabled:
            return factory.request_recipe_unlock(obs, "fast-inserter")
        # Only this saved cell may own the arm footprint being replaced.
        if any(e["position"] == arm["position"] for e in factory._reserved(exclude=plan["key"])):
            return _blocked("machine port arm is shared with another reserved plan", port=key)
        proof = _survey(factory, plan, row)
        live = proof.get("rows")
        if live == {}:  # Factorio encodes an empty Lua array as an object.
            live = []
        if (not proof.get("ok") or proof.get("world_id") != obs["world_id"]
                or not isinstance(live, list) or len(live) > 1
                or any(not e.get("valid") or e.get("name") not in ARM_BUDGETS or not e.get("unit_number") for e in live)):
            return _blocked("cannot prove owned machine port geometry", port=key)
        actual = live[0] if live else None
        if record and record.get("world_id") != obs["world_id"]:
            return _blocked("machine port upgrade belongs to another world", port=key)
        if actual and actual["name"] == "inserter":
            if record and record.get("old_unit_number") != actual["unit_number"]:
                return _blocked("owned machine port identity changed", port=key)
            if not record:
                observed = next((e for e in obs.get("entities", []) if e.get("name") == "inserter"
                                 and e.get("position") == arm["position"]), None)
                if not observed or observed.get("unit_number") != actual["unit_number"]:
                    return _blocked("machine port upgrade requires the observed owned arm", port=key)
        if actual and actual["name"] == "fast-inserter":
            if (arm["name"] != "fast-inserter" or not record or record.get("state") != "observed"
                    or record.get("observed_unit_number") != actual["unit_number"]):
                _select_arm(plan, arm, "fast-inserter")
                record = record or {"world_id": obs["world_id"], "plan_key": plan["key"], "position": deepcopy(arm["position"])}
                record.update(state="observed", observed_unit_number=actual["unit_number"], observed_tick=obs.get("tick"))
                factory.state.setdefault("machine_port_upgrades", {})[key] = record
                factory._save()
            continue
        if not enabled:
            if arm["name"] != "inserter":
                _select_arm(plan, arm, "inserter")
                factory._save()
            if rate_per_minute is not None:
                return factory.request_recipe_unlock(obs, "fast-inserter")
            continue
        if int(obs.get("inventory", {}).get("fast-inserter", 0)) < 1:
            return factory.bootstrap.ensure_item(obs, "fast-inserter", 1)
        if record is None:
            record = {"world_id": obs["world_id"], "plan_key": plan["key"], "position": deepcopy(arm["position"]),
                      "old_unit_number": actual["unit_number"] if actual else None, "state": "reserved"}
            factory.state.setdefault("machine_port_upgrades", {})[key] = record
            factory._save()
        if actual:
            return {"type": "mine", "name": "inserter", "position": arm["position"], "count": 1,
                    "expected_entity_unit": actual["unit_number"], "expected_entity_world_id": obs["world_id"],
                    "reason": "upgrade the owned machine port using a normally crafted fast inserter"}
        replacement = {**arm, "name": "fast-inserter"}
        replacement.pop("item", None)
        return factory.builder.ensure_plan(obs, {"ok": True, "entities": [replacement], "ports": []})
    return None
