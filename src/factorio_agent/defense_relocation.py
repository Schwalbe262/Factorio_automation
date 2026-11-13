"""Normally prepaid relocation of conflicting new engine coverage posts."""

from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .actor_client import DETACHED_ACTOR_LUA
from .defense_coverage import coverage_report, native_candidate_survey
from .defense_perimeter import AMMO_TARGET, protected_factory_areas
from .engine_production import SCOPE_ID, engine_core
from .planning import overlap, preflight
from .smelting import merge_entities
from .transport import VECTORS, surface_pipe_proof, xy


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _key(e):
    return e["name"] + ":" + str(e["position"]["x"]) + ":" + str(e["position"]["y"])


def _box(p):
    return [[p["x"] - 1, p["y"] - 1], [p["x"] + 1, p["y"] + 1]]


def _identity(obs, state, catalog):
    return (
        obs.get("ok", True)
        and state.get("world_id") == obs.get("world_id")
        and bool(obs.get("world_id"))
        and state.get("catalog_fingerprint") == catalog.fingerprint
    )


def _engine_guard(state):
    return _hash(
        {
            k: state.get(k)
            for k in (
                "plan",
                "core",
                "parameters",
                "iron_source",
                "steel_source",
                "steel_plan_revision",
                "steel_furnace_units",
            )
        }
    )


def _parent_checkpoint(document):
    return {k: v for k, v in document.items() if k not in ("continuations", "active_continuation_id")}


def _active_relocation(document):
    if not document.get("active_continuation_id"):
        if document.get("continuations"):
            raise ValueError("relocation continuation is not explicitly active")
        return document
    epochs = document.get("continuations")
    if not isinstance(epochs, list) or len(epochs) != 1:
        raise ValueError("one explicitly reviewed continuation required")
    epoch = epochs[0]
    if (
        epoch.get("id") != document["active_continuation_id"]
        or epoch.get("id") != document["id"] + ":continuation:1"
        or epoch.get("parent_revision") != _hash(_parent_checkpoint(document))
        or any(
            epoch.get(k) != document.get(k)
            for k in ("world_id", "catalog_fingerprint", "engine_guard", "shared_bindings")
        )
    ):
        raise ValueError("relocation continuation parent or identity changed")
    return epoch


def _referencing_scopes(perimeter, name):
    return {
        key: value
        for key, value in perimeter.get("coverage_scopes", {}).items()
        if name in set(value.get("covering_posts", [])) | set(value.get("new_posts", []))
    }


def _coverage_guards(perimeter, name):
    by = {p["name"]: p for p in perimeter["posts"]}
    result = []
    for key, scope in _referencing_scopes(perimeter, name).items():
        names = sorted(set(scope["covering_posts"]) | set(scope["new_posts"]))
        result.append(
            {
                "name": key,
                "cells": scope["cells"],
                "effective_range": scope["effective_range"],
                "posts": [{k: by[n][k] for k in ("name", "position", "unit_number")} for n in names],
            }
        )
    return result


def _perimeter_matches(state, perimeter):
    expected = {p["name"]: p for p in state["initial_posts"]}
    current = {p["name"]: p for p in perimeter.get("posts", [])}
    if (
        len(expected) != len(state["initial_posts"])
        or len(current) != len(perimeter.get("posts", []))
        or not set(expected) <= set(current)
    ):
        return False
    protected = set()
    protected_scopes = set()
    for entry in state["entries"]:
        refs = _referencing_scopes(perimeter, entry["name"])
        if set(refs) != set(entry["scope_revisions"]) or any(
            _hash(refs[k]) != v for k, v in entry["scope_revisions"].items()
        ):
            return False
        protected_scopes.update(refs)
        for scope in refs.values():
            protected.update(set(scope["covering_posts"]) | set(scope["new_posts"]))
    for entry in state["entries"]:
        if entry["phase"] in ("retire", "done"):
            expected[entry["name"]] = entry["new"]
        elif entry["phase"] == "publish" and current[entry["name"]] == entry["new"]:
            expected[entry["name"]] = entry["new"]
    if not protected <= set(expected) or any(current[name] != expected[name] for name in protected):
        return False
    mutable = {"unit_number", "lost_unit_number", "lost_unit_numbers", "lost_unit_origin", "commissioned"}
    for name, old in expected.items():
        post = current[name]
        if name in protected or post == old:
            continue
        history = post.get("lost_unit_numbers", [])
        if not isinstance(history, list) or len(history) > 16 or any(type(u) is not int or u < 1 for u in history):
            return False
        lost = post.get("lost_unit_number")
        if lost is not None and (type(lost) is not int or lost < 1):
            return False
        if lost is not None:
            history = history + [lost]
        origin = post.get("lost_unit_origin")
        old_origin = old.get("lost_unit_origin")
        if origin is not None and (type(origin) is not int or origin < 1):
            return False
        if old_origin is not None and origin != old_origin:
            return False
        lineage = (
            old.get("unit_number") in history
            or old.get("unit_number") == origin
            or (old_origin is not None and origin == old_origin)
        )
        if (
            type(old.get("unit_number")) is not int
            or not lineage
            or type(post.get("unit_number")) is not int
            or post["unit_number"] < 1
            or post["unit_number"] == old["unit_number"]
            or post.get("commissioned") is not True
            or {k: v for k, v in old.items() if k not in mutable} != {k: v for k, v in post.items() if k not in mutable}
        ):
            return False
    for name in set(current) - set(expected):
        post = current[name]
        scope_id = post.get("coverage_scope")
        scope = perimeter.get("coverage_scopes", {}).get(scope_id, {})
        if (
            scope_id in protected_scopes
            or not scope_id
            or not scope.get("commissioned")
            or name not in scope.get("new_posts", [])
            or post.get("commissioned") is not True
            or type(post.get("unit_number")) is not int
            or post["unit_number"] < 1
        ):
            return False
    return True


def _unrelated_native_posts(state, perimeter):
    protected = set()
    for entry in state["entries"]:
        for scope in _referencing_scopes(perimeter, entry["name"]).values():
            protected.update(set(scope["covering_posts"]) | set(scope["new_posts"]))
    return [
        {k: p[k] for k in ("name", "position", "unit_number")} for p in perimeter["posts"] if p["name"] not in protected
    ]


def validate_relocation_action(action, kind, world=None):
    required = {"type", "transaction_id", "post_name", "expected_old_unit", "expected_actor_unit", "expected_world_id"}
    extra = {"position"} if kind == "build_defense_replacement" else {"expected_new_unit"}
    if kind == "reconcile_defense_post_retirement":
        extra |= {"evidence_id"}
    if (
        not isinstance(action, dict)
        or set(action) - required - extra - {"reason"}
        or not required <= set(action)
        or action.get("type") != kind
        or kind not in ("build_defense_replacement", "retire_defense_post", "reconcile_defense_post_retirement")
        or any(type(action.get(k)) is not int or action[k] < 1 for k in ("expected_old_unit", "expected_actor_unit"))
        or any(
            not isinstance(action.get(k), str) or not action[k] or len(action[k]) > 180
            for k in ("transaction_id", "post_name", "expected_world_id")
        )
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("defense relocation requires exact transaction, post, actor and world identities")
    if world is not None and action["expected_world_id"] != world:
        raise ValueError("defense relocation must target the bound world")
    if kind == "build_defense_replacement":
        p = action.get("position")
        if (
            not isinstance(p, dict)
            or set(p) != {"x", "y"}
            or any(type(v) not in (int, float) or not math.isfinite(v) or v % 1 for v in p.values())
        ):
            raise ValueError("replacement turret requires an exact finite integer-center position")
    elif type(action.get("expected_new_unit")) is not int or action["expected_new_unit"] < 1:
        raise ValueError("retirement requires the exact paid replacement unit")
    if kind == "reconcile_defense_post_retirement" and (
        not isinstance(action.get("evidence_id"), str)
        or len(action["evidence_id"]) != 64
        or any(c not in "0123456789abcdef" for c in action["evidence_id"])
    ):
        raise ValueError("legacy retirement reconciliation requires exact persisted evidence")


def _action_context(game, action, kind):
    validate_relocation_action(action, kind, getattr(game, "expected_world", None))
    if getattr(game, "expected_world", None) != action["expected_world_id"]:
        raise ValueError("defense relocation requires a bound game world")
    if getattr(game, "backend", None) != "assisted":
        raise ValueError("guarded post relocation requires the ordinary assisted backend")
    root = Path(game.cfg.runtime_dir)
    state = _active_relocation(json.loads((root / "defense-relocation.json").read_text(encoding="utf-8")))
    engine = json.loads((root / "engine-production.json").read_text(encoding="utf-8"))
    perimeter = json.loads((root / "defense-perimeter.json").read_text(encoding="utf-8"))
    matches = [e for e in state.get("entries", []) if e.get("name") == action["post_name"]]
    if (
        state.get("id") != action["transaction_id"]
        or state.get("world_id") != action["expected_world_id"]
        or len(matches) != 1
        or _engine_guard(engine) != state.get("engine_guard")
        or engine.get("phase") != "build"
        or engine.get("bindings") != state.get("shared_bindings")
        or any(
            v.get("world_id") != state["world_id"] or v.get("catalog_fingerprint") != state["catalog_fingerprint"]
            for v in (engine, perimeter)
        )
    ):
        raise ValueError("defense action is restricted to the current unchanged engine prebuild transaction")
    entry = matches[0]
    expected_phase = "build" if kind == "build_defense_replacement" else "retire"
    if entry.get("phase") != expected_phase or entry["old"]["unit_number"] != action["expected_old_unit"]:
        raise ValueError("defense action must match the exact persisted phase and old unit")
    if not _perimeter_matches(state, perimeter):
        raise ValueError("known defense post or referenced scope metadata changed")
    current = [p for p in perimeter["posts"] if p["name"] == entry["name"]]
    wanted = entry["old"] if kind == "build_defense_replacement" else entry["new"]
    if len(current) != 1 or current[0] != wanted:
        raise ValueError("logical defense post publication identity changed")
    for name, revision in entry["scope_revisions"].items():
        if _hash(perimeter.get("coverage_scopes", {}).get(name)) != revision:
            raise ValueError("referencing defense scope changed")
    marker = state["id"] + ":" + entry["name"]
    payload = dict(
        action,
        marker=marker,
        old=entry["old"],
        new=entry["new"],
        other_posts=_unrelated_native_posts(state, perimeter),
        sources=[dict(engine[k], name="transport-belt") for k in ("iron_source", "steel_source")],
        consumer_specs=[
            {k: e[k] for k in ("name", "position")}
            for e in engine["plan"]["entities"]
            if _key(e) not in state["shared_bindings"]
        ],
    )
    if state.get("parent_revision"):
        if action["expected_actor_unit"] != state.get("actor_unit"):
            raise ValueError("continuation actor changed")
        payload["history"] = state["historical_resolution"]
    if kind == "build_defense_replacement":
        if action["position"] != entry["new"]["position"]:
            raise ValueError("paid replacement site changed")
        protected = protected_factory_areas(game, state["world_id"], state["catalog_fingerprint"])
        if any(overlap(_box(action["position"]), area) for area in protected):
            raise ValueError("replacement site overlaps protected production or port access")
        payload["protected"] = protected
    else:
        if entry["new"].get("unit_number") != action["expected_new_unit"]:
            raise ValueError("paid replacement identity changed")
        if entry.get("publication") != {"old_revision": _hash(entry["old"]), "new_revision": _hash(entry["new"])}:
            raise ValueError("persisted replacement publication intent required before retirement")
        payload["coverage"] = _coverage_guards(perimeter, entry["name"])
        if not payload["coverage"]:
            raise ValueError("referencing scope proof is required before retirement")
        if kind == "reconcile_defense_post_retirement":
            intent = entry.get("legacy_retirement_reconciliation", {})
            if (
                intent.get("evidence_id") != action["evidence_id"]
                or intent.get("evidence_id") != _hash({k: v for k, v in intent.items() if k != "evidence_id"})
                or intent.get("baseline_available") is not False
                or intent.get("recovered_counts_available") is not False
                or intent.get("version") != 1
                or intent.get("transaction_id") != state["id"]
                or intent.get("post_name") != entry["name"]
            ):
                raise ValueError("exact explicitly reviewed legacy reconciliation intent required")
            original = {
                k: action[k]
                for k in (
                    "transaction_id",
                    "post_name",
                    "expected_old_unit",
                    "expected_actor_unit",
                    "expected_world_id",
                    "expected_new_unit",
                )
            }
            original.update(
                type="retire_defense_post",
                reason="normally recover exact old post after paid armed replacement coverage",
            )
            evidence = _legacy_action_evidence(root, original, intent.get("action_evidence", {}).get("job_key"))
            if evidence != intent.get("action_evidence"):
                raise ValueError("legacy paired action evidence changed")
            payload["reconciliation"] = intent
    return payload


POST_GUARD_LUA = (
    DETACHED_ACTOR_LUA
    + r"""
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if not d or d.world_id~=x.expected_world_id then return reject("defense_relocation_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.expected_actor_unit then return reject("defense_relocation_actor_changed")end
if prototypes.entity["gun-turret"].tile_width~=2 or prototypes.entity["gun-turret"].tile_height~=2 then return reject("defense_turret_footprint_changed")end
for _,q in ipairs(x.sources or{})do local e=target(q.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x
  or e.position.y~=q.position.y or e.direction~=q.facing then return reject("defense_relocation_engine_source_changed")end
end
for _,q in ipairs(x.consumer_specs or{})do if target(q.position,q.name)then return reject("engine_consumer_construction_already_started")end end
local function exact(q)
 local e=target(q.position,"gun-turret")
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"then return nil end
 return e end
for _,q in ipairs(x.other_posts or{})do if not exact(q)then return reject("unrelated_repaired_defense_post_native_identity_changed")end end
local ammo_proto=prototypes.item["firearm-magazine"];local size=ammo_proto and ammo_proto.magazine_size
if not ammo_proto or ammo_proto.type~="ammo"or not size or size<=0 then return reject("defense_magazine_prototype_missing")end
local function plain(stack,name)
 return stack.valid_for_read and stack.name==name and stack.quality.name=="normal"and stack.health==1
  and stack.spoil_tick==0 and not stack.is_item_with_tags and not stack.is_item_with_inventory and not stack.is_item_with_entity_data end
local function ammo(e,full_only)
 local inv=e.get_inventory(defines.inventory.turret_ammo);if not inv or not inv.valid then return nil end
 local count,full,rounds=0,0,0
 for i=1,#inv do local stack=inv[i];if stack.valid_for_read then
  if not plain(stack,"firearm-magazine")or stack.prototype.type~="ammo"or stack.ammo<1 or stack.ammo>size
   or stack.ammo%1~=0 or(full_only and stack.ammo~=size)then return nil end
  count=count+stack.count;full=full+stack.count-(stack.ammo<size and 1 or 0)
  rounds=rounds+(stack.count-1)*size+stack.ammo
 end end
 return {count=count,full=full,rounds=rounds}end
local main=a.get_main_inventory();if not main or not main.valid then return reject("defense_actor_inventory_missing")end
local marker=d.defense_relocations and d.defense_relocations[x.marker]or nil
if marker and(marker.old_unit~=x.old.unit_number or marker.old_position.x~=x.old.position.x
 or marker.old_position.y~=x.old.position.y or marker.position.x~=x.new.position.x or marker.position.y~=x.new.position.y)then
 return reject("defense_relocation_receipt_identity_changed")end
if x.history then
 local h=x.history;local original=d.defense_relocations and d.defense_relocations[h.marker_id]
 if not original or original.old_unit~=h.old.unit_number or original.unit_number~=h.original_replacement.unit_number
  or original.actor_unit~=h.original_actor_unit or original.paid_turrets~=1 or original.retired or original.retirement_intent
  or original.old_position.x~=h.old.position.x or original.old_position.y~=h.old.position.y
  or original.position.x~=h.current_post.position.x or original.position.y~=h.current_post.position.y then return reject("continuation_original_paid_marker_changed")end
 if target(h.old.position,"gun-turret")or not exact(h.current_post)then return reject("continuation_historical_post_identity_changed")end
 if s.count_entities_filtered{position=h.old.position,radius=2,type="item-entity"}>0 then return reject("continuation_historical_ground_items_changed")end
 local owner=d.crafting_player_index and game.get_player(d.crafting_player_index)or nil
 if not owner or not owner.valid or owner.name~="FactoryAutomaton"or game.get_player("FactoryAutomaton")~=owner
  or (not detached_actor() and (owner.character~=a or a.player~=owner or not owner.connected)) or owner.force~=f
  or d.actor~=a or d.crafting_actor_unit_number~=a.unit_number then return reject("continuation_pinned_owner_changed")end
 local k=d.world_id..":"..owner.index..":"..h.original_actor_unit..":"..a.unit_number
 local record=d.actor_successions and d.actor_successions[k]
 if not record or record.kind~="owner-character-succession-v1"or record.world_id~=d.world_id
  or record.owner_index~=owner.index or record.owner_name~=owner.name or record.old_unit~=h.original_actor_unit
  or record.new_unit~=a.unit_number or record.force_index~=f.index or record.surface_index~=s.index
  or record.old_pointer_invalid~=true or record.old_character_absent~=true or type(record.tick)~="number"
  or record.tick>game.tick or record.tick<h.paid_repair_evidence.event_tick then
  return reject("continuation_owner_succession_changed")end
 local function same(q,r)
  if type(q)~=type(r)then return false end;if type(q)~="table"then return q==r end
  for key,v in pairs(q)do if not same(v,r[key])then return false end end
  for key in pairs(r)do if q[key]==nil then return false end end;return true end
 if(h.original_marker and not same(h.original_marker,original))or(h.owner_succession and not same(h.owner_succession,record))then
  return reject("continuation_reviewed_native_history_changed")end
end
"""
)


BUILD_REPLACEMENT_LUA = (
    POST_GUARD_LUA
    + r"""
if marker then
 local e=exact({position=marker.position,unit_number=marker.unit_number})
 if not e then return reject("paid_replacement_missing_or_replaced")end
 return {ok=true,status="succeeded",unit_number=e.unit_number,reused_receipt=true}end
if not exact(x.old)then return reject("old_defense_post_identity_changed")end
if target(x.new.position,"gun-turret")then return reject("unexpected_replacement_post_before_paid_build")end
local p=x.new.position;local area={{p.x-1,p.y-1},{p.x+1,p.y+1}}
for _,r in ipairs(x.protected)do if area[1][1]<r[2][1]and area[2][1]>r[1][1]and area[1][2]<r[2][2]and area[2][2]>r[1][2]then return reject("replacement_site_protected")end end
if s.count_entities_filtered{area=area,type="resource"}>0 or not s.can_place_entity{name="gun-turret",position=p,direction=0,force=f}then return reject("replacement_site_changed")end
local slot=nil
for i=1,#main do local stack=main[i];if plain(stack,"gun-turret")and stack.prototype.type=="item"then slot=i;break end end
if not slot then return reject("paid_normal_turret_missing")end
local before=main.get_item_count{name="gun-turret",quality="normal"};local stack=main[slot];stack.count=stack.count-1
local e=s.create_entity{name="gun-turret",position=p,direction=0,force=f,quality="normal",raise_built=true,player=a.player}
if not e then main.insert{name="gun-turret",quality="normal",count=1};return reject("normal_turret_build_failed_refunded")end
if main.get_item_count{name="gun-turret",quality="normal"}~=before-1 then return reject("paid_turret_count_conservation_failed")end
d.defense_relocations=d.defense_relocations or{}
d.defense_relocations[x.marker]={old_unit=x.old.unit_number,old_position=x.old.position,unit_number=e.unit_number,position=p,paid_turrets=1,actor_unit=a.unit_number}
return {ok=true,status="succeeded",unit_number=e.unit_number,paid_turrets=1}
"""
)


SCOPE_COVERAGE_LUA = r"""
for _,scope in ipairs(x.coverage)do
 if scope.effective_range>prototypes.entity["gun-turret"].attack_parameters.range-2 then return reject("defense_native_range_changed")end
 local posts={}
 for _,q in ipairs(scope.posts)do local e=exact(q);local stock=e and ammo(e,false)
  if not e or e.health~=e.max_health or not stock or stock.full<5 then return reject("referencing_scope_post_unhealthy")end
  posts[#posts+1]=e end
 for _,cell in ipairs(scope.cells)do local covered=false
  for _,e in ipairs(posts)do local fits=true
   for _,p in ipairs(cell.corners)do if(e.position.x-p[1])^2+(e.position.y-p[2])^2>scope.effective_range^2+1e-9 then fits=false;break end end
   if fits then covered=true;break end
  end
  if not covered then return reject("referencing_scope_coverage_missing")end
 end
end
"""


RETIRE_COVERAGE_LUA = (
    POST_GUARD_LUA
    + r"""
if not marker or marker.unit_number~=x.new.unit_number or marker.actor_unit~=a.unit_number or marker.paid_turrets~=1 then return reject("paid_replacement_receipt_required")end
local replacement=exact(x.new);local loaded=replacement and ammo(replacement,false)
if not replacement or replacement.health~=replacement.max_health or not loaded or loaded.full<10 then return reject("replacement_requires_ten_full_normal_magazines")end
"""
    + SCOPE_COVERAGE_LUA
)


ACTOR_TOTALS_LUA = r"""
local function same(a,b)
 if type(a)~=type(b)then return false end
 if type(a)~="table"then return a==b end
 for k,v in pairs(a)do if not same(v,b[k])then return false end end
 for k in pairs(b)do if a[k]==nil then return false end end
 return true end
local function totals()
 local equipped=a.get_inventory(defines.inventory.character_ammo)
 if not equipped or not equipped.valid or equipped==main or #main>2000 or #equipped>100 then return nil end
 local value={gun_turrets=0,magazines=0,rounds=0,snapshot={},other_items={}}
 for index,inv in ipairs({main,equipped})do local slots={};value.snapshot[index]=slots
  for i=1,#inv do local st=inv[i];local row={slot=i}
   if st.valid_for_read then
    row.name=st.name;row.count=st.count;row.quality=st.quality.name
    if st.name=="gun-turret"or st.name=="firearm-magazine"then
     if not plain(st,st.name)or type(st.count)~="number"or st.count<1 or st.count%1~=0 then return nil end
     if st.name=="gun-turret"then
      if st.prototype.type~="item"then return nil end;value.gun_turrets=value.gun_turrets+st.count
     else
      if st.prototype.type~="ammo"or type(st.ammo)~="number"or st.ammo<1 or st.ammo>size or st.ammo%1~=0 then return nil end
      row.magazine_rounds=st.ammo;value.magazines=value.magazines+st.count;value.rounds=value.rounds+(st.count-1)*size+st.ammo
     end
    else value.other_items[#value.other_items+1]={inventory=index,slot=i,name=row.name,count=row.count,quality=row.quality}end
   else row.empty=true end
   slots[#slots+1]=row
  end
 end
 return value end
local function valid_totals(q)
 if type(q)~="table"or type(q.other_items)~="table"or type(q.snapshot)~="table"then return false end
 for _,name in ipairs({"gun_turrets","magazines","rounds"})do if type(q[name])~="number"or q[name]<0 or q[name]%1~=0 then return false end end
 return true end
local function conserved(before,after,stock,rounds)
 local function other_totals(rows)
  local result={}
  for _,q in ipairs(rows)do local inv=result[q.inventory]or{};result[q.inventory]=inv
   local names=inv[q.name]or{};inv[q.name]=names;names[q.quality]=(names[q.quality]or 0)+q.count end
  return result end
 return valid_totals(before)and valid_totals(after)and after.gun_turrets==before.gun_turrets+1 and after.magazines==before.magazines+stock
  and after.rounds==before.rounds+(rounds or stock*size) and same(other_totals(before.other_items),other_totals(after.other_items))end
"""


RETIRE_POST_LUA = (
    RETIRE_COVERAGE_LUA
    + ACTOR_TOTALS_LUA
    + r"""
local old=target(x.old.position,"gun-turret")
if marker.retired then if old then return reject("retired_post_location_reoccupied")end;return {ok=true,status="succeeded",reused_receipt=true}end
local before=totals();if not before then return reject("actor_recovery_inventory_metadata_unsupported")end
local intent=marker.retirement_intent
if intent and((intent.version~=2 and intent.version~=3)or intent.world_id~=x.expected_world_id or intent.actor_unit~=a.unit_number
 or intent.old_unit~=x.old.unit_number or intent.new_unit~=x.new.unit_number or not same(intent.old_position,x.old.position)
 or not same(intent.new_position,x.new.position)or type(intent.old_magazines)~="number"or intent.old_magazines<0
 or intent.old_magazines%1~=0 or type(intent.old_rounds)~="number"or intent.old_rounds%1~=0
 or intent.old_rounds<intent.old_magazines or intent.old_rounds>intent.old_magazines*size
 or(intent.version==2 and intent.old_rounds~=intent.old_magazines*size)or not valid_totals(intent.before)
 or intent.inventory_scope~="character_main+character_ammo")then
 return reject("retirement_intent_identity_changed")end
local recovered_from_intent=not old
if not old then
 if not intent then return reject("old_post_missing_without_retirement_intent")end
 if not conserved(intent.before,before,intent.old_magazines,intent.old_rounds)then return reject("retirement_intent_conservation_changed")end
else
 old=exact(x.old);if not old then return reject("old_defense_post_identity_changed")end
 local stock=ammo(old,false)
 if old.health~=old.max_health or not stock then return reject("old_post_partial_or_nonplain_ammo_requires_explicit_handling")end
 if intent and(not same(intent.before,before)or intent.old_magazines~=stock.count or intent.old_rounds~=stock.rounds)then return reject("retirement_intent_before_inventory_or_stock_changed")end
local empty=0
for i=1,#main do if not main[i].valid_for_read and(not main.supports_filters()or not main.get_filter(i))then empty=empty+1 end end
local required=1+math.ceil(stock.count/ammo_proto.stack_size)
if empty<required or main.get_insertable_count{name="gun-turret",quality="normal"}<1
 or main.get_insertable_count{name="firearm-magazine",quality="normal"}<stock.count then return reject("aggregate_post_recovery_inventory_capacity_missing")end
 if not intent then
  intent={version=3,world_id=x.expected_world_id,actor_unit=a.unit_number,old_unit=x.old.unit_number,new_unit=x.new.unit_number,
   old_position=x.old.position,new_position=x.new.position,before=before,old_magazines=stock.count,old_rounds=stock.rounds,
   inventory_scope="character_main+character_ammo",tick=game.tick};marker.retirement_intent=intent
 end
 if not a.mine_entity(old)then return reject("ordinary_defense_post_mining_failed")end
 intent.native_mine_returned_true=true;before=totals()
 if target(x.old.position,"gun-turret")or not conserved(intent.before,before,intent.old_magazines,intent.old_rounds)then return reject("post_recovery_count_or_round_conservation_failed")end
end
marker.retired=true;marker.recovered_magazines=intent.old_magazines;marker.recovered_rounds=intent.old_rounds
marker.retirement_receipt={version=intent.version,kind="ordinary_mine_conserved",before=intent.before,after=before,
 inventory_scope=intent.inventory_scope,recovered_turrets=1,recovered_magazines=marker.recovered_magazines,
 recovered_rounds=marker.recovered_rounds,recovered_from_intent=recovered_from_intent,tick=game.tick}
return {ok=true,status="succeeded",recovered_turrets=1,recovered_magazines=marker.recovered_magazines,
 recovered_rounds=marker.recovered_rounds,recovered_from_intent=recovered_from_intent}
"""
)


def execute_build_replacement(game, action):
    payload = _action_context(game, action, "build_defense_replacement")
    return game.query("local x=helpers.json_to_table(" + _payload(payload) + ");" + BUILD_REPLACEMENT_LUA)


def execute_retire_post(game, action):
    payload = _action_context(game, action, "retire_defense_post")
    return game.query("local x=helpers.json_to_table(" + _payload(payload) + ");" + RETIRE_POST_LUA)


def _legacy_action_evidence(root, action, job_key):
    if not isinstance(job_key, str) or not job_key or len(job_key) > 180:
        raise ValueError("exact legacy job key required")
    path = Path(root) / "job-events.jsonl"
    pending = {}
    matches = []
    matching_actions = 0
    with path.open("rb") as stream:
        start = max(0, path.stat().st_size - 3 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        for raw in stream:
            row = json.loads(raw)
            if row.get("key") != job_key:
                continue
            if row.get("event") == "action_intent":
                pending[job_key] = (row, hashlib.sha256(raw).hexdigest())
                if row.get("action") == action:
                    matching_actions += 1
            elif row.get("event") == "action_result" and job_key in pending:
                before, digest = pending.pop(job_key)
                if before.get("action") != action:
                    continue
                if row.get("tick") != before.get("tick") or row.get("result") != {
                    "ok": False,
                    "status": "blocked",
                    "reason": "post_recovery_count_or_round_conservation_failed",
                }:
                    continue
                matches.append(
                    {
                        "job_key": job_key,
                        "event_tick": row["tick"],
                        "original_action": action,
                        "intent_line_sha256": digest,
                        "result_line_sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
    if matching_actions != 1 or len(matches) != 1:
        raise ValueError("one exact paired legacy ordinary-mine conservation failure required in bounded ledger tail")
    return matches[0]


LEGACY_RECONCILIATION_PROBE_LUA = (
    RETIRE_COVERAGE_LUA
    + ACTOR_TOTALS_LUA
    + r"""
if target(x.old.position,"gun-turret")then return reject("legacy_retired_post_not_absent")end
if marker.retired or marker.retirement_intent then return reject("legacy_reconciliation_requires_missing_old_intent")end
local current=totals()
if not current or current.gun_turrets<1 or current.magazines<10 or current.rounds~=current.magazines*size then
 return reject("legacy_actor_plain_full_recovery_stock_required")end
local p=x.old.position
if s.count_entities_filtered{area={{p.x-2,p.y-2},{p.x+2,p.y+2}},type="item-entity"}>0 then return reject("legacy_old_post_ground_items_require_explicit_handling")end
return {ok=true,current_actor_totals=current,baseline_available=false,recovered_counts_available=false}
"""
)


RECONCILE_RETIREMENT_LUA = (
    RETIRE_COVERAGE_LUA
    + ACTOR_TOTALS_LUA
    + r"""
if target(x.old.position,"gun-turret")then return reject("legacy_retired_post_not_absent")end
if marker.retired then
 local receipt=marker.retirement_receipt
 if not receipt or receipt.kind~="legacy_action_evidence_reconciliation"or receipt.evidence_id~=x.evidence_id then return reject("legacy_reconciliation_receipt_changed")end
 return {ok=true,status="succeeded",reused_receipt=true,baseline_available=false,recovered_counts_available=false}end
if marker.retirement_intent then return reject("legacy_reconciliation_cannot_replace_native_intent")end
local current=totals()
if not current or not same(current,x.reconciliation.current_actor_totals)then return reject("legacy_actor_inventory_changed_after_review")end
if current.gun_turrets<1 or current.magazines<10 or current.rounds~=current.magazines*size then return reject("legacy_actor_plain_full_recovery_stock_required")end
local p=x.old.position
if s.count_entities_filtered{area={{p.x-2,p.y-2},{p.x+2,p.y+2}},type="item-entity"}>0 then return reject("legacy_old_post_ground_items_require_explicit_handling")end
marker.retirement_receipt={version=1,kind="legacy_action_evidence_reconciliation",evidence_id=x.evidence_id,
 evidence=x.reconciliation.action_evidence,current_actor_totals=current,baseline_available=false,recovered_counts_available=false,tick=game.tick}
marker.retired=true
return {ok=true,status="succeeded",legacy_reconciliation=true,baseline_available=false,recovered_counts_available=false}
"""
)


def execute_reconcile_retirement(game, action):
    payload = _action_context(game, action, "reconcile_defense_post_retirement")
    return game.query("local x=helpers.json_to_table(" + _payload(payload) + ");" + RECONCILE_RETIREMENT_LUA)


def _paid_repair_evidence(root, post):
    action = dict(
        type="build",
        name="gun-turret",
        position=post["position"],
        direction=0,
        reason="normally build proactive strategic perimeter post " + post["name"],
    )
    path = Path(root) / "job-events.jsonl"
    pending = {}
    matches = []
    with path.open("rb") as stream:
        start = max(0, path.stat().st_size - 3 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        for raw in stream:
            row = json.loads(raw)
            key = row.get("key")
            if row.get("event") == "action_intent":
                pending[key] = (row, hashlib.sha256(raw).hexdigest())
            elif row.get("event") == "action_result" and key in pending:
                before, digest = pending.pop(key)
                result = row.get("result", {})
                if (
                    before.get("action") == action
                    and row.get("tick") == before.get("tick")
                    and result.get("ok") is True
                    and result.get("status") == "succeeded"
                    and result.get("unit_number") == post["unit_number"]
                    and not result.get("reused")
                ):
                    matches.append(
                        dict(
                            job_key=key,
                            event_tick=row["tick"],
                            original_action=action,
                            intent_line_sha256=digest,
                            result_line_sha256=hashlib.sha256(raw).hexdigest(),
                        )
                    )
    if len(matches) != 1:
        raise ValueError("one exact paid non-reused canonical post repair receipt required")
    return matches[0]


CONTINUATION_PROBE_LUA = (
    POST_GUARD_LUA
    + SCOPE_COVERAGE_LUA
    + r"""
for _,q in ipairs(x.all_posts)do if not exact(q)then return reject("continuation_current_registered_post_changed")end end
for _,q in ipairs(x.remaining)do local e=exact(q.old)
 if not e or e.health~=e.max_health or not ammo(e,false)then return reject("continuation_remaining_old_post_changed")end
 if target(q.new.position,"gun-turret")then return reject("continuation_replacement_site_already_occupied")end
end
local h=x.history;local owner=game.get_player(d.crafting_player_index)
local k=d.world_id..":"..owner.index..":"..h.original_actor_unit..":"..a.unit_number
return {ok=true,tick=game.tick,original_marker=d.defense_relocations[h.marker_id],owner_succession=d.actor_successions[k]}
"""
)


def activate_relocation_continuation(controller, obs, proposal):
    """Metadata-only CAS activation; caller must hold the existing writer lock."""
    if not isinstance(controller, DefenseRelocation):
        raise ValueError("the owning relocation controller is required")
    return controller.activate_continuation(obs, proposal)


class DefenseRelocation:
    def __init__(self, game, bootstrap, builder, catalog, perimeter, engines):
        self.game, self.bootstrap, self.builder, self.catalog, self.perimeter, self.engines = (
            game,
            bootstrap,
            builder,
            catalog,
            perimeter,
            engines,
        )
        self.path = Path(game.cfg.runtime_dir) / "defense-relocation.json"
        document = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.state = deepcopy(_active_relocation(document))
        self._document_revision = _hash(document)

    def _save(self):
        if self.state.get("parent_revision"):
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if _hash(document) != self._document_revision or _active_relocation(document)["id"] != self.state["id"]:
                raise ValueError("relocation continuation checkpoint changed")
            document["continuations"] = [deepcopy(self.state)]
        else:
            document = self.state
        _atomic_json(self.path, document)
        self._document_revision = _hash(document)

    def _checkpoint_matches(self):
        try:
            return _active_relocation(json.loads(self.path.read_text(encoding="utf-8"))) == self.state
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @property
    def active(self):
        return bool(self.state and self.state.get("phase") != "verified")

    def _engine_ready(self, obs, *, allow_constructed=False):
        s = self.engines.state
        if not _identity(obs, s, self.catalog) or (s.get("phase") != "build" and not allow_constructed):
            return {"ok": False, "reason": "defense_relocation_requires_same_engine_prebuild"}
        if self.state and _engine_guard(s) != self.state["engine_guard"]:
            return {"ok": False, "reason": "defense_relocation_engine_plan_or_source_changed"}
        shared = {_key(dict(s[k], name="transport-belt")): s[k]["unit_number"] for k in ("iron_source", "steel_source")}
        if not allow_constructed and s.get("bindings") != shared:
            return {"ok": False, "reason": "defense_relocation_consumer_bindings_already_present"}
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        for spec in s["plan"]["entities"]:
            if not allow_constructed and _key(spec) not in shared and (spec["name"], *xy(spec)) in actual:
                return {"ok": False, "reason": "defense_relocation_consumer_asset_already_built", "entity": spec}
        for name in ("iron_source", "steel_source"):
            q = s[name]
            live = actual.get(("transport-belt", *xy(q)))
            if not live or live.get("unit_number") != q["unit_number"] or live.get("direction") != q["facing"]:
                return {"ok": False, "reason": "defense_relocation_fixed_engine_source_changed", "source": name}
        return {"ok": True, "bindings": shared}

    def _continuation_preflight(self, obs, entries):
        planned = [
            dict(name="gun-turret", position=e["new"]["position"], direction=0, _width=2, _height=2) for e in entries
        ]
        ignored = [e["old"]["unit_number"] for e in entries]
        engine = self.engines.state
        protected = protected_factory_areas(self.game, obs["world_id"], self.catalog.fingerprint)
        if any(overlap(_box(p["position"]), a) for p in planned for a in protected):
            return {"ok": False, "reason": "continuation_replacement_site_protected"}
        future = engine_core(self.catalog, *xy({"position": engine["core"]["anchor"]}), 4)
        for stage, es in (
            ("whole", merge_entities(engine["plan"]["entities"], planned)),
            ("future", merge_entities(engine["plan"]["entities"], future["entities"], planned)),
        ):
            check = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
            if not check.get("ok"):
                return {"ok": False, "reason": "continuation_" + stage + "_preflight_failed", "evidence": check}
        pipes = surface_pipe_proof(self.game, obs, engine["plan"])
        return (
            {"ok": True}
            if pipes.get("ok")
            else {"ok": False, "reason": "continuation_pipe_proof_failed", "evidence": pipes}
        )

    def prepare_continuation(self, obs, job_key):
        """Read-only reviewed epoch proposal; never completes the old receipt."""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document != self.state or document.get("continuations") or document.get("active_continuation_id"):
                return {"ok": False, "reason": "continuation_requires_unchanged_original_checkpoint"}
            if not _identity(obs, document, self.catalog):
                raise ValueError("continuation world or catalog changed")
            ready = self._engine_ready(obs)
            if not ready.get("ok"):
                return ready
            engine = json.loads(self.engines.path.read_text(encoding="utf-8"))
            if engine != self.engines.state:
                raise ValueError("engine checkpoint changed")
            disk = json.loads(self.perimeter.path.read_text(encoding="utf-8"))
            if not _identity(obs, disk, self.catalog):
                raise ValueError("current perimeter world or catalog changed")
            unfinished = [e for e in document["entries"] if e["phase"] != "done"]
            if (
                len(unfinished) < 2
                or len(unfinished) > 4
                or unfinished[0]["phase"] != "retire"
                or any(e["phase"] != "build" for e in unfinished[1:])
            ):
                raise ValueError("one historical retirement followed by unbuilt replacements required")
            historical = unfinished[0]
            remaining = deepcopy(unfinished[1:])
            old_intent = historical.get("legacy_retirement_reconciliation", {})
            evidence = old_intent.get("action_evidence", {})
            original = evidence.get("original_action", {})
            expected = dict(
                type="retire_defense_post",
                transaction_id=document["id"],
                post_name=historical["name"],
                expected_old_unit=historical["old"]["unit_number"],
                expected_new_unit=historical["new"]["unit_number"],
                expected_actor_unit=original.get("expected_actor_unit"),
                expected_world_id=obs["world_id"],
                reason="normally recover exact old post after paid armed replacement coverage",
            )
            if (
                original != expected
                or type(original.get("expected_actor_unit")) is not int
                or original["expected_actor_unit"] == obs["actor_unit_number"]
                or old_intent.get("baseline_available") is not False
                or old_intent.get("recovered_counts_available") is not False
                or old_intent.get("evidence_id") != _hash({k: v for k, v in old_intent.items() if k != "evidence_id"})
                or _legacy_action_evidence(self.path.parent, original, job_key) != evidence
            ):
                raise ValueError("exact original legacy action and stale reviewed intent required")
            by = {p["name"]: p for p in disk["posts"]}
            if len(by) != len(disk["posts"]):
                raise ValueError("duplicate current registered post")
            current = by[historical["name"]]
            prior = historical["new"]
            mutable = {"unit_number", "lost_unit_number", "lost_unit_numbers", "lost_unit_origin", "commissioned"}
            history = current.get("lost_unit_numbers", []) + [
                current.get("lost_unit_number"),
                current.get("lost_unit_origin"),
            ]
            if (
                current.get("unit_number") == prior["unit_number"]
                or prior["unit_number"] not in history
                or current.get("commissioned") is not True
                or type(current.get("unit_number")) is not int
                or {k: v for k, v in current.items() if k not in mutable}
                != {k: v for k, v in prior.items() if k not in mutable}
            ):
                raise ValueError("same-position canonical paid replacement lineage required")
            repair = _paid_repair_evidence(self.path.parent, current)
            if not evidence["event_tick"] < repair["event_tick"] <= obs["tick"]:
                raise ValueError("paid repair must follow original recovery before this observation")
            baseline = {p["name"]: p for p in document["initial_posts"]}
            baseline[historical["name"]] = prior
            coverage = {}
            for entry in unfinished:
                refs = _referencing_scopes(disk, entry["name"])
                if set(refs) != set(entry["scope_revisions"]) or any(
                    _hash(refs[k]) != v for k, v in entry["scope_revisions"].items()
                ):
                    raise ValueError("referencing engine scope revision changed")
                for key, scope in refs.items():
                    for name in set(scope["covering_posts"]) | set(scope["new_posts"]):
                        if name != historical["name"] and by.get(name) != baseline.get(name):
                            raise ValueError("another protected post changed")
                coverage.update({v["name"]: v for v in _coverage_guards(disk, entry["name"])})
            check = self._continuation_preflight(obs, remaining)
            if not check.get("ok"):
                return check
            resolution = dict(
                kind="known_ordinary_mine_without_original_baseline",
                name=historical["name"],
                old=deepcopy(historical["old"]),
                original_replacement=deepcopy(prior),
                current_post=deepcopy(current),
                original_actor_unit=original["expected_actor_unit"],
                marker_id=document["id"] + ":" + historical["name"],
                action_evidence=evidence,
                paid_repair_evidence=repair,
                baseline_available=False,
                recovered_counts_available=False,
                superseded_review=dict(
                    evidence_id=old_intent["evidence_id"],
                    reason="review_superseded_by_paid_post_repair_and_owner_succession",
                ),
            )
            args = dict(
                expected_world_id=obs["world_id"],
                expected_actor_unit=obs["actor_unit_number"],
                marker=document["id"] + ":continuation:1:review",
                old=historical["old"],
                new=current,
                history=resolution,
                sources=[dict(engine[k], name="transport-belt") for k in ("iron_source", "steel_source")],
                consumer_specs=[
                    {k: e[k] for k in ("name", "position")}
                    for e in engine["plan"]["entities"]
                    if _key(e) not in ready["bindings"]
                ],
                coverage=list(coverage.values()),
                all_posts=disk["posts"],
                remaining=remaining,
            )
            native = self.game.query("local x=helpers.json_to_table(" + _payload(args) + ");" + CONTINUATION_PROBE_LUA)
            if not native.get("ok"):
                return native
            resolution.update(original_marker=native["original_marker"], owner_succession=native["owner_succession"])
            epoch = dict(
                ok=True,
                id=document["id"] + ":continuation:1",
                parent_revision=_hash(document),
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                actor_unit=obs["actor_unit_number"],
                phase="relocate",
                engine_guard=document["engine_guard"],
                shared_bindings=deepcopy(ready["bindings"]),
                initial_posts=deepcopy(disk["posts"]),
                entries=remaining,
                historical_resolution=resolution,
                prepared_tick=obs.get("tick"),
                prospective_whole_preflight=True,
                prospective_future_preflight=True,
                requirements={"gun-turret": len(remaining), "firearm-magazine": AMMO_TARGET * len(remaining)},
            )
            board = json.loads((self.path.parent / "pcb-board.json").read_text(encoding="utf-8"))
            proposal = dict(
                version=1,
                job_key=job_key,
                epoch=epoch,
                guards={
                    "relocation": _hash(document),
                    "engine": _hash(engine),
                    "perimeter": _hash(disk),
                    "board": _hash(board),
                },
            )
            proposal["proposal_id"] = _hash(proposal)
            return {"ok": True, **proposal, "baseline_available": False, "recovered_counts_available": False}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "invalid_relocation_continuation", "detail": str(exc)}

    def activate_continuation(self, obs, proposal):
        """Explicit metadata-only activation with fresh proofs and per-file CAS."""
        if (
            not isinstance(proposal, dict)
            or proposal.get("ok") is not True
            or proposal.get("version") != 1
            or set(proposal)
            != {
                "ok",
                "version",
                "job_key",
                "epoch",
                "guards",
                "proposal_id",
                "baseline_available",
                "recovered_counts_available",
            }
            or proposal.get("proposal_id")
            != _hash(
                {
                    k: v
                    for k, v in proposal.items()
                    if k not in ("ok", "proposal_id", "baseline_available", "recovered_counts_available")
                }
            )
            or proposal.get("baseline_available") is not False
            or proposal.get("recovered_counts_available") is not False
        ):
            raise ValueError("exact reviewed continuation proposal required")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        active = _active_relocation(document)
        if active.get("activation_proposal_id") == proposal["proposal_id"]:
            self.state = deepcopy(active)
            self._document_revision = _hash(document)
            return {"ok": True, "reused_activation": True, "epoch_id": active["id"]}
        if document != self.state:
            return {"ok": False, "reason": "continuation_activation_checkpoint_changed"}
        fresh = self.prepare_continuation(obs, proposal["job_key"])
        if not fresh.get("ok"):
            return fresh

        def authority(value):
            return {k: v for k, v in value.items() if k != "prepared_tick"}

        if fresh["guards"] != proposal["guards"] or authority(fresh["epoch"]) != authority(proposal["epoch"]):
            return {"ok": False, "reason": "continuation_reviewed_authority_changed"}
        paths = {
            "relocation": self.path,
            "engine": self.engines.path,
            "perimeter": self.perimeter.path,
            "board": self.path.parent / "pcb-board.json",
        }
        if any(
            _hash(json.loads(path.read_text(encoding="utf-8"))) != proposal["guards"][key]
            for key, path in paths.items()
        ):
            return {"ok": False, "reason": "continuation_activation_publication_changed"}
        epoch = deepcopy(proposal["epoch"])
        epoch["activation_proposal_id"] = proposal["proposal_id"]
        document["continuations"] = [epoch]
        document["active_continuation_id"] = epoch["id"]
        _atomic_json(self.path, document)
        self.state = deepcopy(epoch)
        self._document_revision = _hash(document)
        return {"ok": True, "epoch_id": epoch["id"], "baseline_available": False, "recovered_counts_available": False}

    def prepare(self, obs):
        """Read-only finite replacement selection; never alters posts or plans."""
        if self.state:
            if not _identity(obs, self.state, self.catalog):
                return {"ok": False, "reason": "defense_relocation_world_or_catalog_changed"}
            return (
                deepcopy(self.state)
                if _engine_guard(self.engines.state) == self.state["engine_guard"]
                else {"ok": False, "reason": "defense_relocation_engine_plan_or_source_changed"}
            )
        ready = self._engine_ready(obs)
        if not ready.get("ok"):
            return ready
        if self.perimeter._identity_error(obs):
            return {"ok": False, "reason": "defense_relocation_perimeter_identity_changed"}
        report = self.perimeter.inspect(obs)
        if not self.perimeter._proof(report):
            return {"ok": False, "reason": "healthy_existing_defense_required_before_relocation"}
        scope = self.perimeter.state.get("coverage_scopes", {}).get(SCOPE_ID)
        if (
            not scope
            or not scope.get("commissioned")
            or scope["areas"] != [self.engines.state["core"]["reserved_area"]]
        ):
            return {"ok": False, "reason": "commissioned_exact_engine_coverage_scope_required"}
        try:
            protected = protected_factory_areas(self.game, obs["world_id"], self.catalog.fingerprint)
            engine = self.engines.state
            geometry = self.builder._occupied_by_plan(engine["plan"]["entities"])
            conflict_areas = [engine["core"]["reserved_area"]] + [
                [[x - 0.5, y - 0.5], [x + 0.5, y + 0.5]] for x, y in geometry
            ]
            for p in engine["core"]["ports"]:
                dx, dy = VECTORS[p["facing"]]
                if p["direction"] == "input":
                    dx, dy = -dx, -dy
                conflict_areas += [
                    [
                        [p["position"]["x"] + i * dx - 0.5, p["position"]["y"] + i * dy - 0.5],
                        [p["position"]["x"] + i * dx + 0.5, p["position"]["y"] + i * dy + 0.5],
                    ]
                    for i in range(3)
                ]
            actual = {e["unit_number"]: e for e in obs.get("entities", []) if e["name"] == "gun-turret"}
            owned = {p.get("unit_number"): p for p in self.perimeter.state["posts"]}
            conflicts = []
            for unit, e in actual.items():
                if not any(overlap(_box(e["position"]), a) for a in conflict_areas):
                    continue
                post = owned.get(unit)
                if (
                    not post
                    or post.get("coverage_scope") != SCOPE_ID
                    or post["name"] not in scope["new_posts"]
                    or post["position"] != e["position"]
                    or not post.get("commissioned")
                ):
                    return {
                        "ok": False,
                        "reason": "conflicting_post_is_not_owned_new_engine_scope",
                        "unit_number": unit,
                    }
                conflicts.append(post)
            if len(conflicts) > 4:
                return {"ok": False, "reason": "engine_relocation_conflict_budget_exceeded"}
            if not conflicts:
                return {"ok": False, "reason": "no_owned_new_engine_post_conflict"}
            conflicts = sorted(conflicts, key=lambda p: p["name"])
            areas = [engine["core"]["reserved_area"]]
            radius = self.perimeter.state["range"]
            bounds = [
                [math.floor(areas[0][0][0] - radius - 3), math.floor(areas[0][0][1] - radius - 3)],
                [math.ceil(areas[0][1][0] + radius + 3), math.ceil(areas[0][1][1] + radius + 3)],
            ]
            local = [a for a in protected if overlap(a, bounds)]
            positions = {
                (int(p["position"]["x"]) + dx, int(p["position"]["y"]) + dy)
                for p in conflicts
                for dx in range(-8, 9)
                for dy in range(-8, 9)
                if 2 <= math.hypot(dx, dy) <= 8
            }
            candidates = [
                {"x": x, "y": y}
                for x, y in sorted(positions)
                if not any(overlap(_box({"x": x, "y": y}), a) for a in local)
            ]
            native = native_candidate_survey(self.game, candidates, areas, local, bounds)
            if not native.get("ok") or native.get("range") != radius or native.get("magazine_size") != 10:
                return {"ok": False, "reason": "native_defense_replacement_site_survey_failed", "evidence": native}
            legal = native.get("candidates", [])
            if legal == {}:
                legal = []
            posts = deepcopy(self.perimeter.state["posts"])
            entries = []
            for old in conflicts:
                refs = _referencing_scopes(self.perimeter.state, old["name"])
                chosen = None
                if not refs:
                    return {"ok": False, "reason": "engine_post_has_no_registered_coverage_reference"}
                for row in sorted(
                    legal, key=lambda p: (math.dist(xy(p), xy(old)), p["position"]["x"], p["position"]["y"])
                ):
                    p = row["position"]
                    if math.dist(xy(row), xy(old)) > 8 or any(
                        other["name"] != old["name"] and math.dist(xy(row), xy(other)) < 3 for other in posts
                    ):
                        continue
                    trial = [dict(post, position=p) if post["name"] == old["name"] else post for post in posts]
                    if all(
                        coverage_report(
                            s["cells"],
                            [q for q in trial if q["name"] in set(s["covering_posts"]) | set(s["new_posts"])],
                            s["effective_range"],
                        )["complete"]
                        for s in refs.values()
                    ):
                        chosen = p
                        posts = trial
                        break
                if chosen is None:
                    return {
                        "ok": False,
                        "reason": "no_protected_free_replacement_preserves_all_referenced_scopes",
                        "post": old["name"],
                    }
                new = deepcopy(old)
                new.update(position=deepcopy(chosen))
                new.pop("unit_number", None)
                entries.append(
                    {
                        "name": old["name"],
                        "old": deepcopy(old),
                        "new": new,
                        "phase": "build",
                        "scope_revisions": {name: _hash(value) for name, value in refs.items()},
                    }
                )
            planned_posts = [
                {"name": "gun-turret", "position": e["new"]["position"], "direction": 0, "_width": 2, "_height": 2}
                for e in entries
            ]
            ignored = [e["old"]["unit_number"] for e in entries]
            x, y = xy({"position": engine["core"]["anchor"]})
            future = engine_core(self.catalog, x, y, 4)
            whole = preflight(
                self.game,
                self.builder,
                obs,
                merge_entities(engine["plan"]["entities"], planned_posts),
                ignored_units=ignored,
            )
            if not whole.get("ok"):
                return {"ok": False, "reason": "replacement_post_and_engine_whole_preflight_failed", "evidence": whole}
            future_check = preflight(
                self.game,
                self.builder,
                obs,
                merge_entities(engine["plan"]["entities"], future["entities"], planned_posts),
                ignored_units=ignored,
            )
            if not future_check.get("ok"):
                return {
                    "ok": False,
                    "reason": "replacement_post_and_engine_future_preflight_failed",
                    "evidence": future_check,
                }
            pipes = surface_pipe_proof(self.game, obs, engine["plan"])
            if not pipes.get("ok"):
                return {"ok": False, "reason": "engine_pipe_preservation_proof_missing", "evidence": pipes}
            return {
                "ok": True,
                "id": "engine-posts:" + obs["world_id"] + ":" + _hash(engine["plan"])[:12],
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "relocate",
                "engine_guard": _engine_guard(engine),
                "shared_bindings": ready["bindings"],
                "entries": entries,
                "initial_posts": deepcopy(self.perimeter.state["posts"]),
                "prepared_tick": obs.get("tick"),
                "prospective_whole_preflight": True,
                "prospective_future_preflight": True,
                "requirements": {"gun-turret": len(entries), "firearm-magazine": AMMO_TARGET * len(entries)},
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "invalid_defense_relocation_authority", "detail": str(exc)}

    def _probe(self, obs, entry):
        x = {
            "expected_world_id": obs["world_id"],
            "expected_actor_unit": obs["actor_unit_number"],
            "marker": self.state["id"] + ":" + entry["name"],
            "old": entry["old"],
            "new": entry["new"],
            "other_posts": _unrelated_native_posts(self.state, self.perimeter.state),
            "legacy_approved": bool(entry.get("legacy_retirement_reconciliation")),
            "legacy_evidence_id": entry.get("legacy_retirement_reconciliation", {}).get("evidence_id"),
        }
        if self.state.get("parent_revision"):
            x["history"] = self.state["historical_resolution"]
        return self.game.query(
            "local x=helpers.json_to_table("
            + _payload(x)
            + ");"
            + POST_GUARD_LUA
            + r"""
local old=target(x.old.position,"gun-turret")
if old and not exact(x.old)then return reject("old_defense_post_identity_changed")end
local stock=old and ammo(old,false)
if old and(old.health~=old.max_health or not stock)then return reject("old_post_partial_or_nonplain_ammo_requires_explicit_handling")end
if not old and not(marker and(marker.retired or marker.retirement_intent)or x.legacy_approved)then return reject("old_post_missing_without_retirement_receipt")end
local new=nil
if marker then
 new=exact({position=marker.position,unit_number=marker.unit_number})
 if not new or marker.paid_turrets~=1 or marker.actor_unit~=a.unit_number then return reject("paid_replacement_missing_or_replaced")end
 if marker.retired then local receipt=marker.retirement_receipt
  if not receipt then return reject("retirement_receipt_missing")end
  if x.legacy_evidence_id then
   if receipt.kind~="legacy_action_evidence_reconciliation"or receipt.evidence_id~=x.legacy_evidence_id then return reject("legacy_reconciliation_receipt_changed")end
  elseif receipt.kind~="ordinary_mine_conserved"or(receipt.version~=2 and receipt.version~=3)then return reject("retirement_receipt_kind_changed")end
 end
else if target(x.new.position,"gun-turret")then return reject("unexpected_replacement_post_before_paid_build")end end
local turrets,empty=0,0
for i=1,#main do local st=main[i]
 if plain(st,"gun-turret")and st.prototype.type=="item"then turrets=turrets+st.count end
 if not st.valid_for_read and(not main.supports_filters()or not main.get_filter(i))then empty=empty+1 end
end
return {ok=true,new_unit=new and new.unit_number or nil,retired=marker and marker.retired or false,
 old_present=old~=nil,old_magazines=stock and stock.count or 0,old_rounds=stock and stock.rounds or 0,normal_turrets=turrets,empty_slots=empty,
 pending_retirement_intent=marker and marker.retirement_intent~=nil or false,
 retirement_kind=marker and marker.retirement_receipt and marker.retirement_receipt.kind or nil}
"""
        )

    def prepare_legacy_retirement_reconciliation(self, obs, job_key):
        """Read-only proposal; an operator must explicitly persist the returned intent."""
        try:
            if not _identity(obs, self.state, self.catalog) or not self._checkpoint_matches():
                return {"ok": False, "reason": "legacy_reconciliation_checkpoint_changed"}
            ready = self._engine_ready(obs)
            if not ready.get("ok"):
                return ready
            entries = [e for e in self.state["entries"] if e["phase"] != "done"]
            if not entries or entries[0]["phase"] != "retire":
                return {"ok": False, "reason": "legacy_reconciliation_requires_published_retirement_phase"}
            entry = entries[0]
            if entry.get("legacy_retirement_reconciliation"):
                return {"ok": False, "reason": "legacy_reconciliation_already_has_reviewed_intent"}
            action = dict(
                type="retire_defense_post",
                transaction_id=self.state["id"],
                post_name=entry["name"],
                expected_old_unit=entry["old"]["unit_number"],
                expected_new_unit=entry["new"]["unit_number"],
                expected_actor_unit=obs["actor_unit_number"],
                expected_world_id=obs["world_id"],
                reason="normally recover exact old post after paid armed replacement coverage",
            )
            payload = _action_context(self.game, action, "retire_defense_post")
            evidence = _legacy_action_evidence(self.path.parent, action, job_key)
            native = self.game.query(
                "local x=helpers.json_to_table(" + _payload(payload) + ");" + LEGACY_RECONCILIATION_PROBE_LUA
            )
            if not native.get("ok"):
                return native
            intent = dict(
                version=1,
                transaction_id=self.state["id"],
                post_name=entry["name"],
                action_evidence=evidence,
                current_actor_totals=native["current_actor_totals"],
                baseline_available=False,
                recovered_counts_available=False,
                prepared_tick=obs.get("tick"),
            )
            intent["evidence_id"] = _hash(intent)
            return {
                "ok": True,
                "intent": intent,
                "post_name": entry["name"],
                "action_type": "reconcile_defense_post_retirement",
                "baseline_available": False,
                "recovered_counts_available": False,
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "legacy_reconciliation_evidence_invalid", "detail": str(exc)}

    def _virtual(self, obs, entry):
        virtual = copy(self.perimeter)
        virtual.state = deepcopy(self.perimeter.state)
        virtual.state["posts"] = [
            deepcopy(entry["new"]) if p["name"] == entry["name"] else p for p in virtual.state["posts"]
        ]
        report = virtual.inspect(obs)
        proofs = {}
        if not report.get("ok"):
            return None, report
        for name in entry["scope_revisions"]:
            proof = virtual._coverage_evidence(report, virtual.state["coverage_scopes"][name])
            if not proof:
                return None, report
            proofs[name] = proof
        return proofs, report

    def _publish(self, obs, entry):
        intent = entry.get("publication")
        if entry.get("phase") != "publish" or intent != {
            "old_revision": _hash(entry["old"]),
            "new_revision": _hash(entry["new"]),
        }:
            return {"status": "blocked", "reason": "persisted_defense_publication_intent_required"}
        if not self._checkpoint_matches():
            return {"status": "blocked", "reason": "defense_relocation_checkpoint_changed"}
        path = self.perimeter.path
        disk = json.loads(path.read_text(encoding="utf-8"))
        if not _identity(obs, disk, self.catalog):
            return {"status": "blocked", "reason": "defense_publication_world_or_catalog_changed"}
        if not _perimeter_matches(self.state, disk):
            return {"status": "blocked", "reason": "known_defense_publication_identity_changed"}
        found = [p for p in disk["posts"] if p["name"] == entry["name"]]
        if len(found) != 1 or found[0] not in (entry["old"], entry["new"]):
            return {"status": "blocked", "reason": "logical_defense_post_publication_changed"}
        if set(_referencing_scopes(disk, entry["name"])) != set(entry["scope_revisions"]) or any(
            _hash(disk["coverage_scopes"][k]) != v for k, v in entry["scope_revisions"].items()
        ):
            return {"status": "blocked", "reason": "referencing_defense_scope_publication_changed"}
        proofs, report = self._virtual(obs, entry)
        if not proofs:
            return {"status": "blocked", "reason": "replacement_all_scope_coverage_proof_missing", "evidence": report}
        target = next(p for p in report["posts"] if p["name"] == entry["name"])
        if target.get("full_magazines", 0) < AMMO_TARGET:
            return {"status": "blocked", "reason": "replacement_requires_ten_full_normal_magazines"}
        if found[0] != entry["new"]:
            disk["posts"] = [deepcopy(entry["new"]) if p["name"] == entry["name"] else p for p in disk["posts"]]
            _atomic_json(path, disk)
        self.perimeter.state = disk
        entry.update(phase="retire", coverage_proof=proofs)
        self._save()
        return {"status": "waiting", "reason": "paid_armed_replacement_published_before_old_post_recovery"}

    def step(self, obs):
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if not _identity(obs, self.state, self.catalog):
            return {"status": "blocked", "reason": "defense_relocation_world_or_catalog_changed"}
        if self.state.get("parent_revision") and obs.get("actor_unit_number") != self.state.get("actor_unit"):
            return {"status": "blocked", "reason": "defense_relocation_continuation_actor_changed"}
        if not self._checkpoint_matches():
            return {"status": "blocked", "reason": "defense_relocation_checkpoint_changed"}
        if self.state.get("phase") == "verified":
            proof = self.proof(obs)
            return (
                {"status": "succeeded", "evidence": proof}
                if proof
                else {"status": "blocked", "reason": "verified_defense_relocation_proof_missing"}
            )
        ready = self._engine_ready(obs)
        if not ready.get("ok"):
            return {"status": "blocked", **ready}
        disk = json.loads(self.perimeter.path.read_text(encoding="utf-8"))
        if not _identity(obs, disk, self.catalog) or not _perimeter_matches(self.state, disk):
            return {"status": "blocked", "reason": "known_defense_post_or_scope_metadata_changed"}
        self.perimeter.state = disk
        entry = next((e for e in self.state["entries"] if e["phase"] != "done"), None)
        if entry is None:
            proof = self.proof(obs)
            if not proof:
                return {"status": "blocked", "reason": "post_relocation_engine_future_or_coverage_preflight_failed"}
            self.state.update(phase="verified", proof=proof)
            self._save()
            return {
                "status": "succeeded",
                "reason": "normally_paid_engine_defense_relocation_verified",
                "evidence": proof,
            }
        probe = self._probe(obs, entry)
        if not probe.get("ok"):
            return {"status": "blocked", **probe}
        action = {
            "transaction_id": self.state["id"],
            "post_name": entry["name"],
            "expected_old_unit": entry["old"]["unit_number"],
            "expected_actor_unit": obs["actor_unit_number"],
            "expected_world_id": obs["world_id"],
        }
        if entry["phase"] == "build":
            if probe.get("new_unit"):
                entry["new"]["unit_number"] = probe["new_unit"]
                entry["phase"] = "arm"
                self._save()
                return {"status": "waiting", "reason": "paid_replacement_unit_bound_from_atomic_receipt"}
            if probe["normal_turrets"] < 1:
                return self.bootstrap.ensure_item(
                    {**obs, "inventory": {**obs.get("inventory", {}), "gun-turret": probe["normal_turrets"]}},
                    "gun-turret",
                    1,
                )
            report = self.perimeter.inspect(obs)
            if report.get("actor_full_magazines", 0) < AMMO_TARGET:
                return self.perimeter._ensure_magazines(obs, report, AMMO_TARGET)
            return dict(
                action,
                type="build_defense_replacement",
                position=entry["new"]["position"],
                reason="normally prepay replacement before relocating " + entry["name"],
            )
        if probe.get("new_unit") != entry["new"].get("unit_number"):
            return {"status": "blocked", "reason": "paid_defense_replacement_binding_changed"}
        if entry["phase"] == "arm":
            proofs, report = self._virtual(obs, entry)
            target = next((p for p in report.get("posts", []) if p["name"] == entry["name"]), {})
            needed = AMMO_TARGET - target.get("full_magazines", 0)
            if needed > 0:
                held = report.get("actor_full_magazines", 0)
                if held < 1:
                    return self.perimeter._ensure_magazines(obs, report, needed)
                return {
                    "type": "insert",
                    "name": "gun-turret",
                    "position": entry["new"]["position"],
                    "item": "firearm-magazine",
                    "inventory": "turret_ammo",
                    "count": min(held, needed),
                    "expected_entity_unit": entry["new"]["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "load ten full normal magazines in paid defense replacement",
                }
            if not proofs:
                return {
                    "status": "blocked",
                    "reason": "replacement_all_scope_coverage_proof_missing",
                    "evidence": report,
                }
            entry["phase"] = "publish"
            entry["publication"] = {"old_revision": _hash(entry["old"]), "new_revision": _hash(entry["new"])}
            self._save()
        if entry["phase"] == "publish":
            return self._publish(obs, entry)
        if entry["phase"] == "retire":
            if probe.get("retired") and not probe.get("old_present"):
                entry["phase"] = "done"
                self._save()
                return {"status": "waiting", "reason": "old_post_recovered_from_exact_atomic_receipt"}
            proofs, _ = self._virtual(obs, entry)
            if not proofs:
                return {"status": "blocked", "reason": "replacement_all_scope_coverage_proof_missing"}
            if entry.get("legacy_retirement_reconciliation"):
                result = dict(
                    action,
                    type="reconcile_defense_post_retirement",
                    expected_new_unit=entry["new"]["unit_number"],
                    evidence_id=entry["legacy_retirement_reconciliation"]["evidence_id"],
                    reason="reconcile exact logged legacy ordinary post recovery without inventing before totals",
                )
                try:
                    _action_context(self.game, result, "reconcile_defense_post_retirement")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    return {"status": "blocked", "reason": "legacy_reconciliation_evidence_invalid", "detail": str(exc)}
                return result
            if not probe.get("old_present") and probe.get("pending_retirement_intent"):
                return dict(
                    action,
                    type="retire_defense_post",
                    expected_new_unit=entry["new"]["unit_number"],
                    reason="finish exact persisted native post retirement intent",
                )
            required = 1 + math.ceil(probe["old_magazines"] / self.catalog.items["firearm-magazine"]["stack_size"])
            if probe["empty_slots"] < required:
                return {
                    "status": "blocked",
                    "reason": "aggregate_post_recovery_inventory_capacity_missing",
                    "required_empty_slots": required,
                }
            return dict(
                action,
                type="retire_defense_post",
                expected_new_unit=entry["new"]["unit_number"],
                reason="normally recover exact old post after paid armed replacement coverage",
            )
        return {"status": "blocked", "reason": "unknown_defense_relocation_phase"}

    def proof(self, obs):
        if (
            not self.state
            or not _identity(obs, self.state, self.catalog)
            or any(e["phase"] != "done" for e in self.state["entries"])
        ):
            return None
        ready = self._engine_ready(obs, allow_constructed=self.state.get("phase") == "verified")
        if not ready.get("ok"):
            return None
        recovery = []
        if self.state.get("historical_resolution"):
            h = self.state["historical_resolution"]
            recovery.append(
                {
                    "name": h["name"],
                    "kind": h["kind"],
                    "baseline_available": False,
                    "recovered_counts_available": False,
                    "action_evidence": h["action_evidence"],
                    "original_paid_replacement_unit": h["original_replacement"]["unit_number"],
                    "current_paid_repair_unit": h["current_post"]["unit_number"],
                }
            )
        for entry in self.state["entries"]:
            probe = self._probe(obs, entry)
            if (
                not probe.get("ok")
                or probe.get("old_present")
                or not probe.get("retired")
                or probe.get("new_unit") != entry["new"]["unit_number"]
            ):
                return None
            current = next((p for p in self.perimeter.state["posts"] if p["name"] == entry["name"]), None)
            if current != entry["new"]:
                return None
            proofs, _ = self._virtual(obs, entry)
            if not proofs:
                return None
            recovery.append(
                {
                    "name": entry["name"],
                    "kind": probe.get("retirement_kind"),
                    "baseline_available": probe.get("retirement_kind") == "ordinary_mine_conserved",
                }
            )
        plan = self.engines.state["plan"]
        x, y = xy({"position": self.engines.state["core"]["anchor"]})
        future = engine_core(self.catalog, x, y, 4)
        if not preflight(self.game, self.builder, obs, plan["entities"]).get("ok"):
            return None
        if not preflight(self.game, self.builder, obs, merge_entities(plan["entities"], future["entities"])).get("ok"):
            return None
        if not surface_pipe_proof(self.game, obs, plan).get("ok"):
            return None
        return {
            "tick": obs.get("tick"),
            "relocated": [
                {
                    "name": e["name"],
                    "old_unit": e["old"]["unit_number"],
                    "replacement_unit": e["new"]["unit_number"],
                    "position": e["new"]["position"],
                }
                for e in self.state["entries"]
            ],
            "engine_plan_unchanged": True,
            "future_core_preflight": True,
            "recovery_evidence": recovery,
            "scope": "geometric armed coverage and normal recovery receipts; legacy reconciliations have no original conservation baseline; no combat victory claim",
        }
