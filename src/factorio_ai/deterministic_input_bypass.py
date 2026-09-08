"""Paid, restartable cutover of one owned surface chain to an underground bypass.

Preparation leaves the canonical route intact. Once switching is persisted,
ordinary planning is suspended until a fresh survey publishes the new route.
Published receipts remain available to reconcile a subsequently loaded save.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

from .deterministic_underground import OBSERVE_UNDERGROUND_LUA, underground_fields
from .deterministic_underground_bypass import propose_collinear_bypasses


def _report(reason, *, status="blocked", **evidence):
    return {"status": status, "reason": reason, "evidence": evidence}


def describe_bypass_entity(entity: dict) -> dict:
    """Return the exact descriptor accepted by the guarded ordinary mine."""
    return {"name": entity["name"], "position": deepcopy(entity["position"]),
            "direction": entity["direction"], "unit_number": entity["unit_number"],
            **underground_fields(entity)}


def _point(entity):
    return entity["position"]["x"], entity["position"]["y"]


def _key(entity):
    return json.dumps([entity["name"], *_point(entity), entity.get("direction", 0),
                       entity.get("belt_to_ground_type")], separators=(",", ":"))


def _pairs(proposal):
    points = {_point(row) for row in proposal["new_entities"]}
    return [pair for pair in proposal["plan"].get("underground_pairs", [])
            if _point(pair["input"]) in points and _point(pair["output"]) in points]


def observe_input_bypass(factory, proposal: dict, observation: dict) -> dict:
    """One fresh, inert census; no cached evidence and no action or planner save."""
    payload = {"entry": proposal["entry"]["old"], "exit": proposal["exit"],
               "new_entities": proposal["new_entities"], "retained_segment": proposal["retained_segment"],
               "allowed": [*proposal["plan"]["entities"], *proposal["retained_segment"]],
               "item": proposal["plan"]["source_port"]["item"]}
    encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
    return factory.game.query(OBSERVE_UNDERGROUND_LUA + '''
--[[ input_bypass_survey: exact live identities and topology, no mutations. ]]
local args=helpers.json_to_table(''' + encoded + ''')
local function normal(q) return not q or (type(q)=="string" and q=="normal") or q.name=="normal" end
local function pure(e)
 if not normal(e.quality) then return false end
 if e.type=="transport-belt" or e.type=="underground-belt" then
  for index=1,e.get_max_transport_line_index() do
   for _,item in pairs(e.get_transport_line(index).get_contents()) do
    if item.count>0 and (item.name~=args.item or not normal(item.quality)) then return false end
   end
  end
 elseif e.type=="inserter" and e.held_stack.valid_for_read then
  if e.held_stack.name~=args.item or not normal(e.held_stack.quality) then return false end
 end
 return true
end
local function row(spec)
 local found=nil
 for _,e in pairs(s.find_entities_filtered{position=spec.position,radius=0.05}) do
  if e.type~="resource" and e.type~="item-entity" and e.type~="character"
   and e.position.x==spec.position.x and e.position.y==spec.position.y then
   if found then return {present=true,ambiguous=true} end
   found=e
  end
 end
 if not found then
  return {present=false,can_place=s.can_place_entity{name=spec.name,position=spec.position,
   direction=spec.direction or 0,force=f,type=spec.belt_to_ground_type}}
 end
 local e=found
 local out={present=true,name=e.name,position=pos(e.position),direction=e.direction,unit_number=e.unit_number,
  owned=e.force==f and e.surface==s,healthy=e.health~=nil and e.health>=e.max_health,pure=pure(e)}
 if e.type=="underground-belt" then
  for k,v in pairs(observe_underground(e)) do out[k]=v end
 end
 return out
end
local function rows(specs) local out={} for _,spec in ipairs(specs) do out[#out+1]=row(spec) end return out end
local function expected(e)
 for _,spec in ipairs(args.allowed) do
  if e.name==spec.name and e.position.x==spec.position.x and e.position.y==spec.position.y then return true end
 end
 return false
end
local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}
local topology=true
local protected={args.entry};for _,spec in ipairs(args.new_entities) do protected[#protected+1]=spec end
for _,spec in ipairs(protected) do
 for _,e in pairs(s.find_entities_filtered{position=spec.position,radius=3}) do
  if not expected(e) then
   if e.type=="inserter" then
    for _,p in ipairs({e.pickup_position,e.drop_position}) do
     if math.abs(p.x-spec.position.x)<0.51 and math.abs(p.y-spec.position.y)<0.51 then topology=false end
    end
   elseif e.type=="transport-belt" or e.type=="underground-belt" or e.type=="splitter" then
    local v=vectors[e.direction];local w=vectors[spec.direction]
    if (v and e.position.x+v[1]==spec.position.x and e.position.y+v[2]==spec.position.y)
     or (w and spec.position.x+w[1]==e.position.x and spec.position.y+w[2]==e.position.y) then topology=false end
   end
  end
 end
end
local d=storage.deterministic_player
return {ok=true,world_id=d and d.world_id,tick=game.tick,actor_unit_number=a.unit_number,
 max_distance=prototypes.entity["underground-belt"].max_underground_distance,topology_clear=topology,
 normal_inventory={['transport-belt']=a.get_main_inventory().get_item_count{name="transport-belt",quality="normal"},
 ['underground-belt']=a.get_main_inventory().get_item_count{name="underground-belt",quality="normal"}},
 entry=row(args.entry),exit=row(args.exit),new_entities=rows(args.new_entities),retained_segment=rows(args.retained_segment)}
''')


def _identity_error(factory, observation, record):
    if (record.get("world_id") != observation.get("world_id")
            or record.get("world_id") != factory.state.get("world_id")
            or record.get("catalog_fingerprint") != factory._fingerprint
            or record.get("catalog_fingerprint") != factory.state.get("catalog_fingerprint")
            or record.get("actor_unit_number") != observation.get("actor_unit_number")):
        return "input bypass world, catalog or actor identity changed; receipt preserved"
    if getattr(factory.game, "backend", None) != "assisted":
        return "input bypass switching requires the assisted action backend"
    return None


def input_bypass_sync_error(factory, observation):
    """Prevent destructive reset or recursive canonical planning during cutover."""
    records = factory.state.get("input_bypasses", {})
    if not isinstance(records, dict):
        return "malformed input bypass checkpoint; receipt preserved"
    for record in records.values():
        if not isinstance(record, dict):
            return "malformed input bypass checkpoint; receipt preserved"
        error = _identity_error(factory, observation, record)
        if error:
            return error
        if record.get("phase") not in {"preparing", "published"} or observation.get("tick", 0) < record.get("last_tick", 0):
            return "input bypass requires critical reconciliation before canonical planning"
    return None


def _owners(factory, link_key, record=None):
    excluded = {record["pending_key"], record["retained_key"]} if record else set()
    return [plan for category in ("blocks", "links", "power_links", "source_upgrades")
            for key, plan in factory.state.get(category, {}).items()
            if not (category == "links" and key == link_key) and not (category == "blocks" and key in excluded)]


def _valid_proposal(factory, link_key, proposal, old_plan, limit, record=None):
    path = [proposal["entry"]["old"], *proposal["retained_segment"], proposal["exit"]]
    return proposal in propose_collinear_bypasses(old_plan, path, max_distance=limit,
                                                  other_plans=_owners(factory, link_key, record))


def _footprints_clear(factory, proposal, record=None):
    reserved = factory._reserved(exclude=record["pending_key"] if record else None)
    occupied = factory.builder._occupied_by_plan(reserved) | factory._port_clearances()
    return not (factory.builder._occupied_by_plan(proposal["new_entities"]) & occupied)


def _survey_error(survey, observation, record=None):
    if (not isinstance(survey, dict) or survey.get("ok") is not True
            or survey.get("world_id") != observation.get("world_id")
            or type(survey.get("tick")) is not int or survey["tick"] < observation.get("tick", 0)
            or survey.get("actor_unit_number") != observation.get("actor_unit_number")
            or type(survey.get("max_distance")) is not int or survey["max_distance"] < 1
            or survey.get("topology_clear") is not True):
        return "input bypass survey is stale, malformed or has foreign topology"
    if record and survey["tick"] < record.get("last_tick", 0) and observation.get("tick", 0) >= record["last_tick"]:
        return "input bypass survey precedes its persisted receipt"
    groups = (survey.get("new_entities"), survey.get("retained_segment"))
    if any(not isinstance(group, list) for group in groups):
        return "input bypass survey entity lists are malformed"
    rows = [survey.get("entry"), survey.get("exit"), *groups[0], *groups[1]]
    units = [row.get("unit_number") for row in rows if isinstance(row, dict) and row.get("present") is True]
    if any(type(unit) is not int or unit < 1 for unit in units) or len(set(units)) != len(units):
        return "input bypass survey contains missing or duplicate engine identities"
    return None


def _matches(spec, row, unit=None):
    return (isinstance(row, dict) and row.get("present") is True and row.get("owned") is True
            and row.get("healthy") is True and row.get("pure") is True and not row.get("ambiguous")
            and row.get("name") == spec["name"] and row.get("position") == spec["position"]
            and row.get("direction") == spec.get("direction", 0)
            and row.get("belt_to_ground_type") == spec.get("belt_to_ground_type")
            and type(row.get("unit_number")) is int and row["unit_number"] > 0
            and (unit is None or row["unit_number"] == unit))


def _fixed_error(record, survey):
    proposal = record["proposal"]
    if not _matches(proposal["exit"], survey.get("exit"), record["exit_unit"]):
        return "input bypass unchanged exit identity, health or material changed"
    rows = survey.get("retained_segment")
    if not isinstance(rows, list) or len(rows) != len(proposal["retained_segment"]):
        return "input bypass retained survey is incomplete"
    for spec, row in zip(proposal["retained_segment"], rows):
        if not _matches(spec, row, record["retained_units"][_key(spec)]):
            return "input bypass retained route identity, health or material changed"
    return None


def _new_error(record, survey, *, rollback=False):
    specs, rows = record["proposal"]["new_entities"], survey.get("new_entities")
    if not isinstance(rows, list) or len(rows) != len(specs):
        return "input bypass new-piece survey is incomplete"
    for spec, row in zip(specs, rows):
        key = _key(spec)
        known = record["new_units"].get(key)
        if not isinstance(row, dict) or type(row.get("present")) is not bool:
            return "input bypass new-piece survey is malformed"
        if row["present"]:
            if not _matches(spec, row, known) or (known is None and record.get("building") != key):
                return "input bypass pending piece identity, health or material changed"
        elif known is not None and not rollback:
            return "input bypass previously built piece disappeared"
    return None


def _pair_error(proposal, survey):
    rows = {_key(spec): row for spec, row in zip(proposal["new_entities"], survey["new_entities"])}
    for pair in _pairs(proposal):
        for side, opposite in (("input", "output"), ("output", "input")):
            row, neighbour = rows[_key(pair[side])], rows[_key(pair[opposite])]
            live = row.get("underground_neighbour", {})
            if (row.get("underground_pair_verified") is not True or live.get("reciprocal") is not True
                    or row.get("max_underground_distance") != survey["max_distance"]
                    or pair["max_distance"] != survey["max_distance"]
                    or any(live.get(key) != value for key, value in describe_bypass_entity(neighbour).items())):
                return "input bypass underground pair lacks exact reciprocal live proof"
    return None


def _pending_plan(record):
    return {"ok": True, "entities": deepcopy(record["proposal"]["new_entities"]), "ports": [],
            "underground_pairs": deepcopy(_pairs(record["proposal"])), "input_bypass_pending": record["link_key"]}


def start_input_bypass(factory, link_key: str, proposal: dict, observation: dict) -> dict:
    """Reserve one validated bypass without changing its canonical input link."""
    if getattr(factory.game, "backend", None) != "assisted":
        return _report("input bypass switching requires the assisted action backend")
    records = factory.state.get("input_bypasses", {})
    if (not isinstance(records, dict) or link_key in records
            or any(not isinstance(r, dict) or r.get("phase") != "published" for r in records.values())):
        return _report("an input bypass receipt already owns this route or another cutover is pending")
    if (not observation.get("world_id") or observation.get("world_id") != factory.state.get("world_id")
            or factory._fingerprint != factory.state.get("catalog_fingerprint")
            or type(observation.get("actor_unit_number")) is not int):
        return _report("input bypass start requires the current world, catalog and actor")
    old_plan = factory.state.get("links", {}).get(link_key)
    try:
        survey = observe_input_bypass(factory, proposal, observation)
        error = _survey_error(survey, observation)
        if error:
            return _report(error)
        if not _valid_proposal(factory, link_key, proposal, old_plan, survey["max_distance"]):
            return _report("input bypass proposal does not match the exact owned canonical route")
        if not _footprints_clear(factory, proposal):
            return _report("input bypass overlaps a reserved footprint or port approach")
        old = proposal["entry"]["old"]
        if not _matches(old, survey.get("entry")) or not _matches(proposal["exit"], survey.get("exit")):
            return _report("input bypass original endpoints are not healthy, owned and pure")
        retained = survey.get("retained_segment")
        new = survey.get("new_entities")
        if (not isinstance(retained, list) or len(retained) != len(proposal["retained_segment"])
                or any(not _matches(spec, row) for spec, row in zip(proposal["retained_segment"], retained))
                or not isinstance(new, list) or len(new) != len(proposal["new_entities"])
                or any(row.get("present") is not False or row.get("can_place") is not True for row in new)):
            return _report("input bypass old route or new construction footprint is not available")
        record = {"phase": "preparing", "link_key": link_key, "world_id": observation["world_id"],
                  "catalog_fingerprint": factory._fingerprint, "actor_unit_number": observation["actor_unit_number"],
                  "started_tick": survey["tick"], "last_tick": survey["tick"], "old_plan": deepcopy(old_plan),
                  "proposal": deepcopy(proposal), "old_entry_unit": survey["entry"]["unit_number"],
                  "exit_unit": survey["exit"]["unit_number"], "new_units": {},
                  "retained_units": {_key(spec): row["unit_number"] for spec, row in zip(proposal["retained_segment"], retained)},
                  "pending_key": "input-bypass:pending:" + link_key, "retained_key": "input-bypass:retained:" + link_key}
        if any(record[key] in factory.state.get("blocks", {}) for key in ("pending_key", "retained_key")):
            return _report("input bypass reservation key already exists")
        factory.state.setdefault("input_bypasses", {})[link_key] = record
        factory.state.setdefault("blocks", {})[record["pending_key"]] = _pending_plan(record)
        factory._save()
        return _report("input bypass reserved; reobserve before paid construction", status="waiting", link=link_key)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _report("malformed input bypass proposal or live survey")


def _build(spec):
    return {"type": "build", "name": spec["name"], "item": spec["name"],
            "position": deepcopy(spec["position"]), "direction": spec["direction"], **underground_fields(spec)}


def _mine(record, survey):
    proposal = record["proposal"]
    live = {_key(spec): row for spec, row in zip(proposal["new_entities"], survey["new_entities"])}
    return {"type": "mine", "name": "transport-belt", "position": deepcopy(proposal["entry"]["old"]["position"]),
            "count": 1, "expected_entity_world_id": record["world_id"], "expected_entity_unit": record["old_entry_unit"],
            "belt_route_replacement": {"item": proposal["plan"]["source_port"]["item"],
                "entry_direction": proposal["entry"]["old"]["direction"],
                "expected_actor_unit_number": record["actor_unit_number"],
                "exit": describe_bypass_entity(survey["exit"]),
                "entities": [describe_bypass_entity(row) for row in survey["new_entities"]],
                "pairs": [{"input": describe_bypass_entity(live[_key(pair["input"])]),
                           "output": describe_bypass_entity(live[_key(pair["output"])]),
                           "max_distance": pair["max_distance"]} for pair in _pairs(proposal)]}}


def _publish(factory, record, survey):
    proposal = record["proposal"]
    factory.state["links"][record["link_key"]] = deepcopy(proposal["plan"])
    factory.state["blocks"].pop(record["pending_key"], None)
    factory.state["blocks"][record["retained_key"]] = {
        "ok": True, "entities": deepcopy(proposal["retained_segment"]), "ports": [],
        "input_bypass_retained": record["link_key"], "retired_for_upgrade": True}
    record.update(phase="published", published_tick=survey["tick"], last_tick=survey["tick"],
                  new_entry_unit=survey["entry"]["unit_number"], canonical_published=True)
    record.pop("building", None)
    factory._save()
    return _report("input bypass published from fresh proof; reobserve before ordinary planning",
                   status="waiting", link=record["link_key"], flow_verified=False)


def _resume(factory, record, observation):
    proposal = record["proposal"]
    rollback = observation.get("tick", 0) < record["last_tick"]
    current = factory.state.get("links", {}).get(record["link_key"])
    expected = proposal["plan"] if record.get("canonical_published") else record["old_plan"]
    if current != expected:
        return _report("input bypass canonical route changed; receipt preserved")
    survey = observe_input_bypass(factory, proposal, observation)
    error = _survey_error(survey, observation, record) or _fixed_error(record, survey) or _new_error(record, survey, rollback=rollback)
    if error:
        return _report(error)
    if not _valid_proposal(factory, record["link_key"], proposal, record["old_plan"], survey["max_distance"], record):
        return _report("input bypass ownership or live underground distance changed; receipt preserved")
    # Published geometry itself now owns these pieces; the old-plan proposal
    # still checks other owners. Preparation additionally protects full machine
    # footprints and reserved approaches, beyond proposal center coordinates.
    if not record.get("canonical_published") and not _footprints_clear(factory, proposal, record):
        return _report("input bypass overlaps a newly reserved footprint or port approach")
    entry = survey.get("entry")
    old_present = _matches(proposal["entry"]["old"], entry, record["old_entry_unit"])
    if rollback and old_present:
        previous_tick = record["last_tick"]
        record["new_units"] = {_key(spec): row["unit_number"] for spec, row in zip(proposal["new_entities"], survey["new_entities"])
                               if row["present"]}
        record.update(phase="preparing", last_tick=survey["tick"], rollback_from_tick=previous_tick, canonical_published=False)
        record.pop("building", None)
        record.pop("new_entry_unit", None)
        factory.state["links"][record["link_key"]] = deepcopy(record["old_plan"])
        factory.state["blocks"][record["pending_key"]] = _pending_plan(record)
        factory._save()
        return _report("input bypass rollback restored its intact original route; reobserve", status="waiting")
    if rollback and record["phase"] == "preparing":
        return _report("input bypass rollback lacks its original entry; receipt preserved")
    for spec, row in zip(proposal["new_entities"], survey["new_entities"]):
        if row["present"]:
            record["new_units"][_key(spec)] = row["unit_number"]
    record["last_tick"] = survey["tick"]
    if record.get("building") in record["new_units"]:
        record.pop("building", None)
    missing = [spec for spec, row in zip(proposal["new_entities"], survey["new_entities"]) if not row["present"]]
    stock = survey.get("normal_inventory", {})
    if record["phase"] == "preparing":
        if not old_present:
            return _report("input bypass original entry changed before switching; receipt preserved")
        bill = Counter(spec["name"] for spec in missing)
        bill["transport-belt"] += 1  # Includes the final entry before any guarded mine.
        for item, count in sorted(bill.items()):
            if type(stock.get(item)) is not int or stock[item] < count:
                factory._save()
                fresh = {**observation, "tick": survey["tick"],
                         "inventory": {**observation.get("inventory", {}), **stock}}
                result = factory.bootstrap.ensure_item(fresh, item, count)
                materials = getattr(factory.builder, "construction_materials", None)
                return materials.ensure(fresh, result) if materials is not None else result
        if missing:
            index = proposal["new_entities"].index(missing[0])
            if survey["new_entities"][index].get("can_place") is not True:
                return _report("input bypass pending footprint became obstructed")
            record["building"] = _key(missing[0])
            factory._save()
            return _build(missing[0])
        error = _pair_error(proposal, survey)
        if error:
            return _report(error)
        record.update(phase="switching", entry_state="mine", switching_tick=survey["tick"])
        factory._save()  # Durable intent precedes the ordinary guarded mine.
        return _mine(record, survey)
    if missing:
        return _report("input bypass critical path lost a prepared piece; receipt preserved")
    error = _pair_error(proposal, survey)
    if error:
        return _report(error)
    if old_present and record.get("entry_state") == "mine":
        if stock.get("transport-belt", 0) < 1:
            return _report("input bypass replacement belt missing before guarded mine")
        factory._save()
        return _mine(record, survey)
    if isinstance(entry, dict) and entry.get("present") is False:
        if stock.get("transport-belt", 0) < 1 or entry.get("can_place") is not True:
            return _report("input bypass critical replacement lacks held belt or free footprint")
        record.update(phase="switching", entry_state="build")
        record.pop("new_entry_unit", None)  # A rolled-back build receives a new engine unit.
        factory._save()
        return _build(proposal["entry"]["new"])
    if (record.get("entry_state") == "build"
            and _matches(proposal["entry"]["new"], entry, record.get("new_entry_unit"))
            and entry["unit_number"] != record["old_entry_unit"]):
        return _publish(factory, record, survey)
    return _report("input bypass switched entry identity differs from persisted intent; receipt preserved")


def resume_input_bypass(factory, observation: dict, *, critical_only=False) -> dict | None:
    """Resume one transaction; every publication/reconciliation ends this decision."""
    records = factory.state.get("input_bypasses", {})
    if not isinstance(records, dict):
        return _report("malformed input bypass checkpoint; receipt preserved")
    try:
        for key, record in records.items():
            if not isinstance(record, dict) or record.get("link_key") != key:
                return _report("malformed input bypass checkpoint; receipt preserved")
            error = _identity_error(factory, observation, record)
            if error:
                return _report(error)
            if record.get("phase") not in {"preparing", "switching", "published"}:
                return _report("unknown input bypass phase; receipt preserved")
        ordered = sorted(records.values(), key=lambda record: observation.get("tick", 0) >= record["last_tick"])
        for record in ordered:
            rollback = observation.get("tick", 0) < record["last_tick"]
            critical = record["phase"] == "switching" or rollback
            if (critical_only and not critical) or (record["phase"] == "published" and not rollback):
                continue
            return _resume(factory, record, observation)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _report("malformed input bypass receipt or live survey; receipt preserved")
    return None
