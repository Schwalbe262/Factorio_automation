"""Paid replacement of an observed shared coal transit arm, with durable ownership."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math


def _report(reason, status="blocked", **evidence):
    return {"status": status, "reason": reason, "evidence": evidence}


def _spec(row):
    return {key: deepcopy(row[key]) for key in ("name", "position", "direction", "unit_number")}


def coal_upgrade_epoch_error(state, obs, fingerprint):
    """Reject a foreign durable intent before a generic planner can reset its owners."""
    if not isinstance(state, dict) or "coal_transit_upgrade" not in state:
        return None
    record = state["coal_transit_upgrade"]
    if (not isinstance(record, dict) or record.get("world_id") != obs.get("world_id")
            or record.get("catalog_fingerprint") != fingerprint
            or record.get("actor_unit") != obs.get("actor_unit_number")
            or type(obs.get("tick")) is not int or obs["tick"] < 0):
        return _report("coal upgrade world, catalog or actor changed; receipt preserved")
    return None


def _plans(energy):
    for i, feed in enumerate(energy.state["feeds"]):
        yield ("feeds", i), feed["plan"], f"energy:feed:{i}"
    for i, bank in enumerate(energy.state["banks"]):
        yield ("banks", i), bank, f"energy:bank:{i}"
    for key, plan in energy.state.get("coal_links", {}).items():
        yield ("coal_links", key), plan, "energy:coal-bank:" + key


def _set_plan(energy, path, plan):
    if path[0] == "feeds":
        energy.state["feeds"][path[1]]["plan"] = deepcopy(plan)
    else:
        energy.state[path[0]][path[1]] = deepcopy(plan)


def _primary_pole(energy, obs):
    spec = next(e for e in energy.state["banks"][0]["entities"] if e["name"] == "small-electric-pole")
    # Pole orientation is not structural; bind its current observed orientation
    # for the immediate query, just as the normal builder treats pole reuse.
    rows = [e for e in obs.get("entities", []) if e.get("name") == spec["name"] and e.get("position") == spec["position"]
            and type(e.get("unit_number")) is int]
    if len(rows) != 1:
        raise ValueError("coal upgrade primary power pole is not observed exactly")
    return _spec(rows[0])


def _owners(energy, arm):
    """Capture every exact reference; unsupported or unpaired owners fail closed."""
    owners = []
    for path, plan, key in _plans(energy):
        rows = [e for e in plan["entities"] if e["position"] == arm["position"]]
        if not rows:
            continue
        if (len(rows) != 1 or any(rows[0].get(k, 0) != arm.get(k, 0) for k in ("name", "direction"))
                or energy.factory.state.get("blocks", {}).get(key) != {**plan, "key": key}):
            raise ValueError("shared coal arm ownership conflicts with its canonical plan")
        old = deepcopy(plan)
        new = deepcopy(plan)
        entity = next(e for e in new["entities"] if e["position"] == arm["position"])
        entity["name"] = "fast-inserter"
        entity.pop("item", None)
        new["required_items"] = dict(Counter(e.get("item") or e["name"] for e in new["entities"]))
        owners.append({"path": list(path), "key": key, "old": old, "new": new,
                       "factory_old": {**deepcopy(old), "key": key}, "factory_new": {**deepcopy(new), "key": key}})
    keys = {owner["key"] for owner in owners}
    for category in ("blocks", "links", "power_links"):
        for key, plan in energy.factory.state.get(category, {}).items():
            if any(e["position"] == arm["position"] for e in plan.get("entities", [])):
                if category != "blocks" or key not in keys:
                    raise ValueError("shared coal arm has an unsupported additional owner")
    for key in ("coal_plan", "power_plan"):
        if any(e["position"] == arm["position"] for e in energy.builder.state.get(key, {}).get("entities", [])):
            raise ValueError("shared coal arm is also owned by a primary builder plan")
    if len(owners) < 2:
        raise ValueError("coal transit upgrade requires multiple exact saved owners")
    return owners


SURVEY = r'''
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id or not a or not a.valid or a.unit_number~=x.actor_unit
 or a.force~=f or a.surface~=s then return {ok=false,reason="coal upgrade actor/world changed"} end
local function normal(e) return e.quality and e.quality.name=="normal" end
local function healthy(e) return e.health and e.max_health and e.health==e.max_health end
local function exact(spec)
 local rows=s.find_entities_filtered{position=spec.position,name=spec.name}
 if #rows~=1 then return nil end
 local e=rows[1]
 if e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or not normal(e) or not healthy(e)
  or e.direction~=spec.direction or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return nil end
 return e
end
local function pure(e)
 for i=1,e.get_max_transport_line_index() do
  for _,v in pairs(e.get_transport_line(i).get_contents()) do
   if v.count>0 and (v.name~="coal" or v.quality~="normal") then return false end
  end
 end
 return true
end
local source=exact(x.pickup);local sink=exact(x.drop)
if not source or not sink or source.type~="transport-belt" or sink.type~="transport-belt"
 or not pure(source) or not pure(sink) then return {ok=false,reason="coal upgrade belt identity or purity changed"} end
local old=prototypes.entity["burner-inserter"];local fast=prototypes.entity["fast-inserter"]
local function xy(p) return p.x or p[1],p.y or p[2] end
local function same(p,q) local px,py=xy(p);local qx,qy=xy(q);return px==qx and py==qy end
if not old or not fast or not same(old.inserter_pickup_position,fast.inserter_pickup_position)
 or not same(old.inserter_drop_position,fast.inserter_drop_position)
 or not same(old.collision_box.left_top,fast.collision_box.left_top)
 or not same(old.collision_box.right_bottom,fast.collision_box.right_bottom) then
 return {ok=false,reason="coal upgrade prototype geometry changed"} end
local function turn(v) local vx,vy=xy(v);for i=1,x.arm.direction/4 do vx,vy=-vy,vx end;return {x=x.arm.position.x+vx,y=x.arm.position.y+vy} end
local pickup=turn(fast.inserter_pickup_position);local drop=turn(fast.inserter_drop_position)
local function tile(p) return {x=math.floor(p.x)+.5,y=math.floor(p.y)+.5} end
if not same(tile(pickup),source.position) or not same(tile(drop),sink.position) then
 return {ok=false,reason="coal upgrade arm no longer connects its two owned belts"} end
local powered=nil;local rebound=nil;local primary=exact(x.primary_pole)
for _,spec in ipairs(x.poles) do
 local p=exact(spec)
 if p then
  local distance=p.prototype.get_supply_area_distance("normal")
  if math.abs(p.position.x-x.arm.position.x)<=distance and math.abs(p.position.y-x.arm.position.y)<=distance then
   for _,g in pairs(s.find_entities_filtered{type="generator",force=f}) do
    if g.electric_network_id==p.electric_network_id and g.energy>0 then
     if p.electric_network_id==x.expected_network_id then powered=spec end
     if primary and primary.electric_network_id==p.electric_network_id then rebound=p.electric_network_id end
     break
    end
   end
  end
 end
 if powered then break end
end
local rows=s.find_entities_filtered{area={{x.arm.position.x-.15,x.arm.position.y-.15},{x.arm.position.x+.15,x.arm.position.y+.15}}}
local live=nil
for _,e in pairs(rows) do if e.type~="resource" and e.type~="character" then
 if live then return {ok=false,reason="coal upgrade footprint has multiple entities"} end;live=e
end end
local phase="empty";local unit=nil;local energized=false
if live then
 if live.force~=f or live.surface~=s or not normal(live) or not healthy(live)
  or live.position.x~=x.arm.position.x or live.position.y~=x.arm.position.y or live.direction~=x.arm.direction
  or (live.name~="burner-inserter" and live.name~="fast-inserter")
  or not same(tile(live.pickup_position),source.position) or not same(tile(live.drop_position),sink.position)
  or (live.held_stack.valid_for_read and (live.held_stack.name~="coal" or live.held_stack.quality.name~="normal")) then
  return {ok=false,reason="coal upgrade live arm geometry, material or ownership changed"} end
 phase=live.name=="burner-inserter" and "old" or "new";unit=live.unit_number
 energized=phase=="new" and powered~=nil and live.energy>0 and live.electric_network_id==target(powered.position,powered.name).electric_network_id
end
local inventory=a.get_main_inventory()
local rotation=fast.get_inserter_rotation_speed("normal");local extension=fast.get_inserter_extension_speed("normal")
local function radius(p) return math.sqrt((p.x-x.arm.position.x)^2+(p.y-x.arm.position.y)^2) end
return {ok=true,world_id=d.world_id,actor_unit=a.unit_number,tick=game.tick,phase=phase,unit_number=unit,
 pole=powered,powered=energized,stock=inventory.get_item_count{name="fast-inserter",quality="normal"},
 rebind_network_id=rebound,
 enabled=f.recipes["fast-inserter"] and f.recipes["fast-inserter"].enabled,
 can_place=phase=="empty" and s.can_place_entity{name="fast-inserter",position=x.arm.position,direction=x.arm.direction,force=f},
 coal_per_minute=3600/(1/rotation+2*math.abs(radius(drop)-radius(pickup))/extension)}
'''


def _survey(energy, record):
    payload = {key: record[key] for key in ("world_id", "actor_unit", "arm", "pickup", "drop", "poles", "primary_pole", "expected_network_id")}
    return energy.game.query(SURVEY.replace("PAYLOAD", json.dumps(json.dumps(payload, separators=(",", ":")))))


def _proof_error(record, obs, proof):
    if (not proof.get("ok") or proof.get("world_id") != record["world_id"]
            or proof.get("actor_unit") != record["actor_unit"] or type(proof.get("tick")) is not int
            or proof["tick"] < obs["tick"] or proof.get("phase") not in {"old", "new", "empty"}):
        return proof.get("reason", "coal upgrade live proof is unavailable or stale")
    return None


def _reconcile(energy, record, destination):
    """The intent is durable first; either half of a two-file publish may be retried."""
    owners = record.get("repair_owners", record["owners"])
    current = {tuple(path): plan for path, plan, _ in _plans(energy)}
    position = record["arm"]["position"]
    paths = {tuple(owner["path"]) for owner in owners}
    keys = {owner["key"] for owner in owners}
    if {path for path, plan, _ in _plans(energy) if any(e["position"] == position for e in plan["entities"])} != paths:
        raise ValueError("coal upgrade acquired or lost a shared energy owner")
    for category in ("blocks", "links", "power_links"):
        for key, plan in energy.factory.state.get(category, {}).items():
            if any(e["position"] == position for e in plan.get("entities", [])) and (category != "blocks" or key not in keys):
                raise ValueError("coal upgrade acquired an additional canonical owner")
    for owner in owners:
        if (current.get(tuple(owner["path"])) not in (owner["old"], owner["new"])
                or energy.factory.state.get("blocks", {}).get(owner["key"]) not in (owner["factory_old"], owner["factory_new"])):
            raise ValueError("coal upgrade saved owner changed during cutover; receipt preserved")
    for owner in owners:
        energy.factory.state["blocks"][owner["key"]] = deepcopy(owner["factory_" + destination])
    energy.factory._save()
    for owner in owners:
        _set_plan(energy, owner["path"], owner[destination])
    energy._save()


def _power_repair(energy, record, obs):
    """Rebuild/reconnect only the already owned covering pole, at normal cost."""
    pole = record["power_pole"]
    rows = [e for e in obs.get("entities", []) if e.get("position") == pole["position"]]
    plan = {"ok": True, "entities": [{k: deepcopy(pole[k]) for k in ("name", "position", "direction")}], "ports": []}
    if not rows:
        record["pole_build_pending"] = True
        energy._save()
        return energy.builder.ensure_plan(obs, plan)
    if len(rows) != 1 or rows[0].get("name") != pole["name"] or rows[0].get("direction", 0) != pole["direction"]:
        return _report("coal upgrade covering pole footprint changed")
    if rows[0].get("unit_number") != pole["unit_number"]:
        if not record.get("pole_build_pending") or type(rows[0].get("unit_number")) is not int:
            return _report("coal upgrade covering pole identity changed")
        pole["unit_number"] = rows[0]["unit_number"]
        record["poles"] = [deepcopy(pole)]
        record.pop("pole_build_pending", None)
        energy._save()
        return _report("coal upgrade covering pole rebuilt; reobserve power", "waiting")
    result = energy.factory.ensure_power_connection(obs, "energy:coal-transit-power:" + str(record["old_unit"]), plan)
    if result.get("status") == "succeeded" and "type" not in result:
        return _report("coal upgrade awaits observed power from its repaired connection", "waiting")
    return result


def resume_coal_upgrade(energy, obs):
    record = energy.state.get("coal_transit_upgrade")
    if record is None:
        return None
    try:
        error = coal_upgrade_epoch_error(energy.state, obs, energy._fingerprint)
        if error:
            return error
        if record["phase"] not in {"preparing", "mining", "building", "published"}:
            return _report("coal upgrade phase is unknown; receipt preserved")
        rollback = obs["tick"] < record["last_tick"]
        # Completed normal geometry is still freshly revalidated by _coal_routes.
        if record["phase"] == "published" and not rollback:
            found = next((e for e in obs.get("entities", []) if e.get("position") == record["arm"]["position"]), None)
            if (found and found.get("name") == "fast-inserter" and found.get("unit_number") == record.get("new_unit")
                    and found.get("direction", 0) == record["arm"]["direction"] and found.get("energy", 0) > 0):
                return None
        if record["phase"] == "published" and not rollback:
            record["primary_pole"] = _primary_pole(energy, obs)
        proof = _survey(energy, record)
        error = _proof_error(record, obs, proof)
        if error:
            return _report(error)
        if proof["phase"] == "old" and proof["unit_number"] != record["old_unit"]:
            return _report("coal upgrade original arm identity changed")
        if (record["phase"] == "published" and not rollback and proof["phase"] in {"new", "empty"}
                and type(proof.get("rebind_network_id")) is int and proof["rebind_network_id"] > 0
                and proof["rebind_network_id"] != record["expected_network_id"]):
            record.update(expected_network_id=proof["rebind_network_id"], last_tick=proof["tick"])
            energy._save()
            return _report("coal upgrade reobserved its current primary generator network; reobserve", "waiting")
        if rollback and proof["phase"] == "old":
            _reconcile(energy, record, "old")
            record.update(phase="preparing", last_tick=proof["tick"], rollback_from_tick=record["last_tick"])
            record.pop("new_unit", None)
            energy._save()
            return _report("coal upgrade rollback restored original ownership; reobserve", "waiting")
        if proof["phase"] == "new":
            if record["phase"] not in {"building", "published"} or proof["unit_number"] != record.get("new_unit", proof["unit_number"]):
                return _report("coal upgrade replacement differs from saved build intent")
            if not proof.get("powered"):
                return _power_repair(energy, record, obs)
            record.update(new_unit=proof["unit_number"], last_tick=proof["tick"])
            energy._save()
            _reconcile(energy, record, "new")
            record.update(phase="published", published_tick=proof["tick"])
            energy._save()
            return _report("paid shared coal arm replacement observed and published; reobserve", "waiting")
        if proof["phase"] == "empty" and record["phase"] not in {"mining", "building", "published"}:
            return _report("coal upgrade arm disappeared before the guarded mine intent")
        if not proof.get("pole"):
            return _power_repair(energy, record, obs)
        if not proof.get("enabled"):
            return energy.factory.request_recipe_unlock(obs, "fast-inserter")
        if proof.get("stock", 0) < 1:
            fresh = {**obs, "tick": proof["tick"], "inventory": {**obs.get("inventory", {}), "fast-inserter": proof.get("stock", 0)}}
            result = energy.bootstrap.ensure_item(fresh, "fast-inserter", 1)
            materials = getattr(energy.builder, "construction_materials", None)
            return materials.ensure(fresh, result) if materials is not None else result
        record["last_tick"] = proof["tick"]
        if proof["phase"] == "old":
            # Validate both copies BEFORE mining; no old builder runs during this intent.
            _reconcile(energy, record, "old")
            record["phase"] = "mining"
            energy._save()
            return {"type": "mine", "name": "burner-inserter", "position": deepcopy(record["arm"]["position"]), "count": 1,
                    "expected_entity_unit": record["old_unit"], "expected_entity_world_id": record["world_id"],
                    "coal_transit_replacement": {"item": "coal", "replacement": "fast-inserter",
                        "expected_actor_unit_number": record["actor_unit"], "old_direction": record["arm"]["direction"],
                        "expected_network_id": record["expected_network_id"],
                        "pickup": record["pickup"], "drop": record["drop"], "pole": proof["pole"]}}
        if not proof.get("can_place"):
            return _report("coal upgrade mined footprint is no longer available")
        if record["phase"] == "published":
            # Preserve the original cutover receipt, including subsequently added
            # legitimate shared feeds, while repairing its missing paid fast arm.
            record["repair_owners"] = _owners(energy, {**record["arm"], "name": "fast-inserter"})
        record.update(phase="building")
        if record.get("new_unit"):
            record.setdefault("replaced_units", []).append(record["new_unit"])
        record.pop("new_unit", None)
        energy._save()
        return {"type": "build", "name": "fast-inserter", "item": "fast-inserter",
                "position": deepcopy(record["arm"]["position"]), "direction": record["arm"]["direction"]}
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as error:
        return _report("coal upgrade checkpoint cannot be reconciled: " + str(error))


def start_coal_upgrade(energy, obs, evidence, capacity):
    """Choose only a proven shared transport bottleneck before paying for another drill."""
    if energy.state.get("coal_transit_upgrade") is not None or "coal_routes" not in evidence:
        return None
    if (energy.state.get("world_id") != obs.get("world_id") or energy.factory.state.get("world_id") != obs.get("world_id")
            or energy.state.get("catalog_fingerprint") != energy._fingerprint
            or energy.factory.state.get("catalog_fingerprint") != energy._fingerprint
            or type(obs.get("tick")) is not int or obs["tick"] < 0
            or type(obs.get("actor_unit_number")) is not int or obs["actor_unit_number"] <= 0
            or type(evidence.get("tick")) is not int or evidence["tick"] < obs["tick"]
            or type(evidence.get("network_id")) is not int or evidence["network_id"] <= 0):
        return _report("coal upgrade requires fresh matching world, catalog, actor and network evidence")
    rows = {row["unit_number"]: row for paths in evidence["coal_routes"].values() for path in paths.values()
            for row in path if row["name"] == "burner-inserter"}
    actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}
    owned_belts = {(e["position"]["x"], e["position"]["y"], e.get("direction", 0)) for _, plan, _ in _plans(energy)
                   for e in plan["entities"] if e["name"] == "transport-belt"}
    candidates = []
    for unit, row in rows.items():
        if not row.get("pickup_position") or not row.get("drop_position"):
            continue
        ends = [actual.get(("transport-belt", *(math.floor(row[k][axis]) + .5 for axis in ("x", "y"))))
                for k in ("pickup_position", "drop_position")]
        if any(not end or type(end.get("unit_number")) is not int
               or (end["position"]["x"], end["position"]["y"], end.get("direction", 0)) not in owned_belts for end in ends):
            continue  # This controller only replaces belt-to-belt branches, never boiler intakes.
        trial = deepcopy(evidence)
        for paths in trial["coal_routes"].values():
            for path in paths.values():
                for edge in path:
                    if edge["unit_number"] == unit:
                        edge.update(name="fast-inserter", coal_per_minute=1e9)
        gain = energy.capacity(trial)["total_kw"] - capacity["total_kw"]
        if gain > .01:
            candidates.append((-gain, unit, row, ends))
    if not candidates:
        return None
    _, unit, arm, ends = min(candidates, key=lambda row: row[:2])
    try:
        owners = _owners(energy, arm)
        owned_poles = {(e["position"]["x"], e["position"]["y"])
                       for category in ("blocks", "links", "power_links")
                       for plan in energy.factory.state.get(category, {}).values()
                       for e in plan.get("entities", []) if e["name"] == "small-electric-pole"}
        poles = [_spec(e) for e in obs.get("entities", []) if e.get("name") == "small-electric-pole"
                 and (e["position"]["x"], e["position"]["y"]) in owned_poles and e.get("unit_number")
                 and max(abs(e["position"][axis] - arm["position"][axis]) for axis in ("x", "y")) <= 8]
        record = {"world_id": obs["world_id"], "catalog_fingerprint": energy._fingerprint,
                  "actor_unit": obs["actor_unit_number"], "arm": _spec(arm), "old_unit": unit,
                  "expected_network_id": evidence["network_id"],
                  "primary_pole": _primary_pole(energy, obs),
                  "pickup": _spec(ends[0]), "drop": _spec(ends[1]), "poles": poles,
                  "owners": owners, "phase": "preparing", "last_tick": obs["tick"]}
        proof = _survey(energy, record)
        error = _proof_error(record, obs, proof)
        if error or proof.get("phase") != "old" or proof.get("unit_number") != unit or not proof.get("pole"):
            return _report(error or "binding shared coal arm cannot be safely replaced")
        rate = proof.get("coal_per_minute")
        if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= arm["coal_per_minute"]:
            return _report("binding shared coal arm has no verified faster replacement")
        record.update(last_tick=proof["tick"], started_tick=proof["tick"], nominal_replacement_rate=rate,
                      power_pole=deepcopy(proof["pole"]), poles=[deepcopy(proof["pole"])])
        energy.state["coal_transit_upgrade"] = record
        energy._save()
        return _report("shared coal transport bottleneck reserved for a paid fast inserter; reobserve", "waiting", unit_number=unit)
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as error:
        return _report("binding shared coal arm ownership cannot be upgraded: " + str(error))
