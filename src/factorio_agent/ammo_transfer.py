"""Exact-slot transfers of full normal magazines, preserving partial magazines."""

from __future__ import annotations

import json
import math


CHESTS = frozenset(("wooden-chest", "iron-chest", "steel-chest"))


def validate_ammo_action(action, *, kind, expected_world=None):
    common = {
        "type",
        "name",
        "position",
        "expected_entity_unit",
        "expected_entity_world_id",
        "expected_actor_unit",
        "reason",
    }
    extra = {"item", "count", "source_slots"} if kind == "take_full_magazines" else {"slots"}
    if (
        not isinstance(action, dict)
        or set(action) - common - extra
        or action.get("type") != kind
        or action.get("name") not in CHESTS
        or any(type(action.get(k)) is not int or action[k] < 1 for k in ("expected_entity_unit", "expected_actor_unit"))
        or not isinstance(action.get("expected_entity_world_id"), str)
        or not action["expected_entity_world_id"]
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("ammo transfer requires exact chest, actor and world identities")
    p = action.get("position")
    if (
        not isinstance(p, dict)
        or set(p) != {"x", "y"}
        or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ("x", "y"))
    ):
        raise ValueError("ammo transfer requires a finite exact chest position")
    if expected_world is not None and action["expected_entity_world_id"] != expected_world:
        raise ValueError("ammo transfer must target the bound world")
    if kind == "take_full_magazines":
        slots = action.get("source_slots")
        if (
            action.get("item") != "firearm-magazine"
            or type(action.get("count")) is not int
            or not 1 <= action["count"] <= 50
            or not isinstance(slots, list)
            or not 1 <= len(slots) <= 16
            or any(type(slot) is not int or not 1 <= slot <= 48 for slot in slots)
            or len(set(slots)) != len(slots)
        ):
            raise ValueError("ammo take requires normal firearm magazines, count1..50 and unique bounded source slots")
    elif kind != "configure_ammo_buffer" or action.get("slots") != 1 or type(action.get("slots")) is not int:
        raise ValueError("ammo buffer supports exactly one available chest slot")


AMMO_GUARD_LUA = r"""
local function reject(reason)return {ok=false,status="blocked",reason=reason,moved=0}end
if not d or d.world_id~=x.expected_entity_world_id then return reject("ammo_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.expected_actor_unit then return reject("ammo_actor_changed")end
local e=target(x.position,x.name)
if not e or not e.valid or e.type~="container"or e.force~=f or e.surface~=s or e.unit_number~=x.expected_entity_unit
 or e.position.x~=x.position.x or e.position.y~=x.position.y then return reject("ammo_chest_changed")end
if character_backend and not a.can_reach_entity(e)then return reject("ammo_chest_out_of_reach")end
local source=e.get_inventory(defines.inventory.chest);local dest=a.get_main_inventory()
if not source or not source.valid or not dest or not dest.valid or source==dest then return reject("ammo_inventory_missing")end
local proto=prototypes.item["firearm-magazine"];local size=proto and proto.magazine_size
if not proto or proto.type~="ammo"or not size or size<=0 then return reject("native_magazine_prototype_missing")end
local function normal(stack)
 return stack.valid_for_read and stack.name=="firearm-magazine"and stack.quality.name=="normal"
end
local function plain(stack)
 return normal(stack)and stack.prototype.type=="ammo"and stack.health==1 and stack.spoil_tick==0
  and not stack.is_item_with_tags and not stack.is_item_with_inventory and not stack.is_item_with_entity_data
  and stack.ammo>=1 and stack.ammo<=size and stack.ammo%1==0
end
"""

TAKE_FULL_MAGAZINES_LUA = (
    AMMO_GUARD_LUA
    + r"""
for i=1,#dest do local stack=dest[i]
 if normal(stack)and not plain(stack)then return reject("ammo_actor_magazine_metadata_unsupported")end
end
local eligible={}
for _,index in ipairs(x.source_slots)do
 if index>#source then return reject("ammo_source_slot_outside_inventory")end
 local stack=source[index]
 if stack.valid_for_read then
  if not plain(stack)then return reject("ammo_source_slot_item_quality_or_metadata_changed")end
  eligible[#eligible+1]=index
 end
end
local function totals(inv)
 local count,rounds=0,0
 for i=1,#inv do local stack=inv[i];if normal(stack)then
  count=count+stack.count;rounds=rounds+(stack.count-1)*size+stack.ammo
 end end
 return count,rounds
end
local sc,sr=totals(source);local dc,dr=totals(dest);local moved=0;local receipts={}
local capacity=dest.get_insertable_count{name="firearm-magazine",quality="normal"}
for _,index in ipairs(eligible)do
 if moved>=x.count or capacity<=0 then break end
 local stack=source[index];local before=stack.count;local ammo=stack.ammo
 local available=before-(ammo<size and 1 or 0);local count=math.min(x.count-moved,available,capacity)
 if count>0 then
  local inserted=dest.insert{name="firearm-magazine",quality="normal",count=count}
  if type(inserted)~="number"or inserted<0 or inserted>count or inserted%1~=0 then return reject("ammo_invalid_native_insert_count")end
  if inserted>0 then
   stack.count=before-inserted;moved=moved+inserted;capacity=capacity-inserted
   receipts[#receipts+1]={slot=index,before=before,after=before-inserted,moved=inserted,partial_preserved=ammo<size}
  end
 end
end
local sc2,sr2=totals(source);local dc2,dr2=totals(dest)
if sc+dc~=sc2+dc2 or sr+dr~=sr2+dr2 then return reject("ammo_count_or_round_conservation_failed")end
return {ok=true,status=moved>0 and "succeeded"or "waiting",moved=moved,requested=x.count,source_unit=e.unit_number,
 actor_unit=a.unit_number,quality="normal",source_slots=receipts,rounds_before=sr+dr,rounds_after=sr2+dr2,
 reason=moved==0 and(capacity<=0 and "actor_ammo_capacity_full"or "waiting_for_full_factory_magazines")or nil}
"""
)

CONFIGURE_AMMO_BUFFER_LUA = (
    AMMO_GUARD_LUA
    + r"""
for i=1,#source do local stack=source[i]
 if stack.valid_for_read and not plain(stack)then return reject("ammo_buffer_contains_other_item_quality_or_metadata")end
 if i>1 and stack.valid_for_read then return reject("ammo_buffer_already_exceeds_single_slot")end
end
if not source.supports_bar()then return reject("ammo_buffer_bar_unsupported")end
source.set_bar(2)
if source.get_bar()~=2 then return reject("ammo_buffer_bar_rejected")end
return {ok=true,status="succeeded",slots=1,unit_number=e.unit_number}
"""
)


def _execute(game, action, kind, body):
    validate_ammo_action(action, kind=kind, expected_world=getattr(game, "expected_world", None))
    backend = getattr(game, "backend", "assisted")
    if backend not in ("assisted", "character"):
        raise ValueError("ammo transfer requires an ordinary supported backend")
    payload = json.dumps(json.dumps(action, separators=(",", ":")))
    return game.query(
        "local x=helpers.json_to_table("
        + payload
        + ");local character_backend="
        + ("true" if backend == "character" else "false")
        + ";"
        + body
    )


def execute_take_full_magazines(game, action):
    return _execute(game, action, "take_full_magazines", TAKE_FULL_MAGAZINES_LUA)


def execute_configure_ammo_buffer(game, action):
    return _execute(game, action, "configure_ammo_buffer", CONFIGURE_AMMO_BUFFER_LUA)
