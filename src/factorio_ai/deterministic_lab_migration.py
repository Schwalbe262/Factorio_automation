"""Move one empty, unconnected owned laboratory off ore using ordinary actions."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

from .factory_templates import build_template


PRIMARY = "research:labs"
REPLACEMENT = "research:labs:replacement"


def _report(reason: str, **evidence) -> dict:
    return {"status": "blocked", "reason": reason, "evidence": evidence}


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


def lab_site_clear(factory, plan: dict) -> bool:
    """Adopting an existing lab follows the same extraction buffer as new labs."""
    from .deterministic_layout import RESOURCE_MARGIN, _survey, bounds
    footprint = factory.builder._occupied_by_plan(plan.get("entities", []))
    if not footprint:
        return False
    result = _survey(factory, [{"area": bounds(footprint, RESOURCE_MARGIN)}])
    return bool(result.get("ok") and result.get("clear") == [1])


def _survey(factory, old_plan: dict, new_plan: dict | None = None) -> dict:
    payload = json.dumps(json.dumps({"old": old_plan, "new": new_plan}, separators=(",", ":")))
    return factory.game.query('''
--[[ lab_migration_survey: read-only identities, inventory, ore and connections. ]]
local args=helpers.json_to_table(''' + payload + ''')
local old_spec
for _,row in ipairs(args.old.entities) do if row.name=="lab" then old_spec=row end end
if not old_spec then return {ok=false,reason="owned laboratory specification missing"} end
local old=target(old_spec.position,"lab");local p=old_spec.position;local proto=prototypes.entity.lab
local covered=s.count_entities_filtered{area={{p.x-proto.tile_width/2,p.y-proto.tile_height/2},
 {p.x+proto.tile_width/2,p.y+proto.tile_height/2}},type="resource"}
local info={present=old~=nil,covered=covered,empty=true,unconnected=true}
if old then
 local input=old.get_inventory(defines.inventory.lab_input);local modules=old.get_module_inventory()
 info.unit_number=old.unit_number;info.owned=old.force==f and old.minable
 info.empty=input~=nil and input.is_empty() and (not modules or modules.is_empty())
end
local owned={};local material={};local live={}
for _,row in ipairs(args.old.entities) do
 local e=target(row.position,row.name)
 if e then
  owned[e.unit_number]=true;live[#live+1]=e
  if e.type~="electric-pole" then material[e.unit_number]=true end
  if e.force~=f or (e.type~="electric-pole" and e.direction~=(row.direction or 0)) then info.unconnected=false end
 end
end
for _,e in ipairs(live) do
 if e.type=="transport-belt" then
  if e.get_transport_line(1).get_item_count()>0 or e.get_transport_line(2).get_item_count()>0 then info.empty=false end
  for _,group in pairs(e.belt_neighbours) do
   for _,other in pairs(group) do if not owned[other.unit_number] then info.unconnected=false end end
  end
 elseif e.type=="inserter" then
  if e.held_stack.valid_for_read then info.empty=false end
  if e.pickup_target and not owned[e.pickup_target.unit_number] then info.unconnected=false end
 end
end
for _,e in pairs(s.find_entities_filtered{position=p,radius=12,type="inserter"}) do
 if not owned[e.unit_number] and e.drop_target and material[e.drop_target.unit_number] then info.unconnected=false end
end
local replacement={present=false,ready=false,powered=false}
if args.new then
 replacement.ready=true
 for _,row in ipairs(args.new.entities) do
  local e=target(row.position,row.name)
  if not e or e.force~=f or (e.type~="electric-pole" and e.direction~=(row.direction or 0)) then replacement.ready=false end
  if row.name=="lab" and e then
   replacement.present=true;replacement.unit_number=e.unit_number;replacement.powered=e.force==f and e.energy>0
  end
 end
end
return {ok=true,world_id=d and d.world_id,tick=game.tick,research=f.current_research and f.current_research.name,
 old=info,replacement=replacement,recoverable=a.get_main_inventory().get_insertable_count("lab")>=1}
''')


def _linked(factory, plan: dict) -> bool:
    ports = [p for p in plan.get("ports", []) if p.get("kind") == "item"]
    return any(link.get("consumer_port") in ports or link.get("source_port") in ports
               for link in factory.state.get("links", {}).values())


def _remap_power(factory, source: str, destination: str) -> None:
    links = factory.state["power_links"]
    for key in list(links):
        if key == source or key.startswith(source + ":pole:"):
            links[destination + key[len(source):]] = links.pop(key)


def ensure_lab_migration(factory, obs: dict) -> dict | None:
    """Return a normal action/block, or None when canonical lab construction may run.

    A replacement is fully built and powered before the canonical checkpoint is
    switched. The original poles/belts remain reserved, even when shared with
    coal routes. Only the exact original laboratory receives a mining action.
    """
    record = factory.state.get("lab_migration")
    # The bounded startup science bridge always retains its original laboratory.
    if not all((obs.get("technologies") or {}).get(name) for name in ("automation", "electric-mining-drill")):
        return None
    old_plan = record["old_plan"] if record else factory.state.get("blocks", {}).get(PRIMARY)
    if not old_plan:
        return None
    labs = [e for e in old_plan.get("entities", []) if e.get("name") == "lab"]
    if len(labs) != 1:
        return _report("laboratory migration requires one exact owned laboratory") if record else None
    old_lab = labs[0]
    new_key = PRIMARY if record and record["state"] in {"retiring", "retired"} else REPLACEMENT
    replacement = factory.state["blocks"].get(new_key) if record else None
    survey = _survey(factory, old_plan, replacement)
    if not survey.get("ok") or survey.get("world_id") != obs["world_id"]:
        return _report("laboratory migration observation unavailable or world changed")
    old = survey.get("old", {})
    if not record and (not old.get("present") or int(old.get("covered", 0)) == 0):
        return None
    if record and record.get("world_id") != obs["world_id"]:
        return _report("laboratory migration checkpoint belongs to another world")
    if old.get("present") and (not old.get("owned") or not old.get("unit_number")
            or (record and old["unit_number"] != record["old_unit_number"])):
        return _report("owned laboratory identity changed during migration")
    if record and record["state"] == "retired":
        if not old.get("present"):
            return None  # Ordinary canonical construction reobserves the new lab.
        # A save rollback can resurrect the same owned unit. Reserve it even
        # when resumed startup research or input activity prevents retirement.
        retired = factory.state["blocks"][record["retired_key"]]
        retired["entities"] = deepcopy(old_plan["entities"])
        retired["required_items"] = deepcopy(old_plan.get("required_items", {}))
        record["state"] = "retiring"
        record.pop("old_absence_observed_tick", None)
        factory._save()
    if not record or old.get("present"):
        if survey.get("research") or not old.get("empty") or not old.get("unconnected") or _linked(factory, old_plan):
            return _report("ore-covered laboratory migration requires idle research and empty unconnected inputs")
    if not record:
        record = {"world_id": obs["world_id"], "state": "building", "created_tick": survey["tick"],
                  "old_unit_number": old["unit_number"], "old_plan": deepcopy(old_plan),
                  "retired_key": PRIMARY + ":retired:" + str(old["unit_number"])}
        factory.state["lab_migration"] = record
        factory._save()
    if record["state"] == "building":
        if factory.state["blocks"].get(PRIMARY) != old_plan:
            return _report("canonical laboratory plan changed during migration")
        packs = [p["item"] for p in old_plan.get("ports", []) if p.get("kind") == "item"]
        replacement = factory.reserve_site(build_template("labs_row", inputs=packs), REPLACEMENT, obs,
                                           reference=old_lab["position"])
        if not replacement.get("ok"):
            return _report(replacement.get("reason", "no separated replacement laboratory site"))
    elif not replacement:
        return _report("replacement laboratory reservation is missing")
    result = factory.builder.ensure_plan(obs, replacement)
    if not _ready(result):
        return result
    result = factory.ensure_power_connection(obs, new_key, replacement)
    if not _ready(result):
        return result
    survey = _survey(factory, old_plan, replacement)
    live_new = survey.get("replacement", {})
    old = survey.get("old", {})
    if (not survey.get("ok") or survey.get("world_id") != obs["world_id"]
            or not live_new.get("ready") or not live_new.get("powered") or not live_new.get("unit_number")):
        return _report("replacement laboratory is not fully observed and powered")
    if old.get("present") and (old.get("unit_number") != record["old_unit_number"] or not old.get("owned")
            or not old.get("empty") or not old.get("unconnected") or survey.get("research") or _linked(factory, old_plan)):
        return _report("original laboratory identity or idle inputs changed before retirement")
    if record["state"] == "building":
        retired = deepcopy(old_plan)
        retired.update(key=record["retired_key"], ports=[], retired_for_lab_migration=True)
        factory.state["blocks"][record["retired_key"]] = retired
        _remap_power(factory, PRIMARY, record["retired_key"])
        promoted = factory.state["blocks"].pop(REPLACEMENT)
        promoted["key"] = PRIMARY
        factory.state["blocks"][PRIMARY] = promoted
        _remap_power(factory, REPLACEMENT, PRIMARY)
        record.update(state="retiring", replacement_unit_number=live_new["unit_number"], switched_tick=survey["tick"])
        factory._save()  # One atomic checkpoint owns both plans and both power paths.
    if old.get("present"):
        if not survey.get("recoverable"):
            return _report("character inventory has no room to recover the original laboratory")
        new_lab = next(e for e in replacement["entities"] if e["name"] == "lab")
        return {"type": "mine", "name": "lab", "position": deepcopy(old_lab["position"]), "count": 1,
                "expected_entity_unit": record["old_unit_number"], "expected_entity_world_id": obs["world_id"],
                "lab_replacement": {"position": deepcopy(new_lab["position"]), "unit_number": live_new["unit_number"]},
                "reason": "recover the empty owned laboratory after its separated replacement is built and powered"}
    if record["state"] != "retired":
        retired = factory.state["blocks"][record["retired_key"]]
        retired["entities"] = [e for e in retired["entities"] if e["name"] != "lab"]
        retired["required_items"] = dict(Counter(e.get("item") or e["name"] for e in retired["entities"]))
        record.update(state="retired", old_absence_observed_tick=survey["tick"])
        factory._save()
    return None
