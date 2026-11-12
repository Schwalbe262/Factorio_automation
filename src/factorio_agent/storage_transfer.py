"""Bounded exact-slot deposits of ordinary normal items into owned chests."""

from __future__ import annotations

import json
import math


CHESTS = frozenset({"wooden-chest", "iron-chest", "steel-chest"})
KNOWN_FAILURES = frozenset(
    {
        "storage_world_changed",
        "storage_actor_changed",
        "storage_chest_changed",
        "storage_out_of_reach",
        "storage_inventory_missing",
        "storage_item_metadata_unsupported",
        "storage_destination_metadata_unsupported",
    }
)


def validate_deposit(action, *, backend="assisted", expected_world=None):
    allowed = {
        "type",
        "name",
        "position",
        "item",
        "count",
        "expected_entity_unit",
        "expected_entity_world_id",
        "reason",
    }
    if (
        not isinstance(action, dict)
        or set(action) - allowed
        or action.get("type") != "deposit"
        or action.get("name") not in CHESTS
        or not isinstance(action.get("item"), str)
        or not action["item"]
        or type(action.get("count")) is not int
        or not 1 <= action["count"] <= 1000
        or type(action.get("expected_entity_unit")) is not int
        or action["expected_entity_unit"] < 1
        or not isinstance(action.get("expected_entity_world_id"), str)
        or not action["expected_entity_world_id"]
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("deposit requires a plain item, owned chest, exact identity and count 1..1000")
    p = action.get("position")
    if (
        not isinstance(p, dict)
        or set(p) != {"x", "y"}
        or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ("x", "y"))
    ):
        raise ValueError("deposit requires finite exact chest position")
    if backend not in ("assisted", "character"):
        raise ValueError("deposit requires a supported ordinary backend")
    if expected_world is not None and expected_world != action["expected_entity_world_id"]:
        raise ValueError("deposit must target the bound world")


DEPOSIT_LUA = r"""
local function reject(reason)
 return {ok=false,status="blocked",reason=reason,moved=0,requested=x.count,
  unit_number=x.expected_entity_unit,world_id=x.expected_entity_world_id,item=x.item,quality="normal"}
end
if not d or d.world_id~=x.expected_entity_world_id then return reject("storage_world_changed") end
if not a or not a.valid or a.force~=f or a.surface~=s then return reject("storage_actor_changed") end
local e=target(x.position,x.name)
if not e or not e.valid or e.unit_number~=x.expected_entity_unit or e.force~=f or e.surface~=s
 or e.position.x~=x.position.x or e.position.y~=x.position.y or e.type~="container" then return reject("storage_chest_changed") end
if character_backend and not a.can_reach_entity(e) then return reject("storage_out_of_reach") end
local proto=prototypes.item[x.item]
if not proto or proto.type~="item" then return reject("storage_item_metadata_unsupported") end
local source=a.get_main_inventory();local dest=e.get_inventory(defines.inventory.chest)
if not source or not source.valid or not dest or not dest.valid then return reject("storage_inventory_missing") end
local moved=0
local good,result=pcall(function()
 local function plain(stack)
  return stack.prototype.type=="item" and stack.quality.name=="normal" and stack.health==1
   and stack.spoil_tick==0 and stack.item==nil
 end
 for i=1,#dest do local stack=dest[i]
  if stack.valid_for_read and stack.name==x.item and stack.quality.name=="normal" and not plain(stack) then
   return reject("storage_destination_metadata_unsupported")
  end
 end
 local eligible={}
 for i=1,#source do local stack=source[i]
  if stack.valid_for_read and stack.name==x.item and plain(stack) then eligible[#eligible+1]=i end
 end
 for _,index in ipairs(eligible) do
  if moved>=x.count then break end
  local stack=source[index];local before=stack.count;local n=math.min(x.count-moved,before)
  local inserted=dest.insert{name=x.item,quality="normal",count=n}
  if type(inserted)~="number" or inserted<0 or inserted>n or inserted%1~=0 then error("invalid_storage_insert_count") end
  if inserted>0 then
   if not stack.valid_for_read or stack.name~=x.item or not plain(stack) or stack.count~=before then
    local rolled=dest.remove{name=x.item,quality="normal",count=inserted}
    if rolled~=inserted then error("storage_conservation_rollback_failed") end
    error("storage_source_slot_changed")
   end
   stack.count=before-inserted;moved=moved+inserted
  end
 end
 return {ok=true,status=moved>0 and "succeeded" or "waiting",moved=moved,requested=x.count,
  unit_number=e.unit_number,world_id=d.world_id,item=x.item,quality="normal"}
end)
if not good then return {ok=false,status="blocked",reason="storage_transfer_outcome_unknown",confirmed_moved=moved,exception=tostring(result)} end
return result
"""


def _unknown(reason, confirmed=0):
    return {
        "ok": False,
        "status": "blocked",
        "reason": "storage_transfer_outcome_unknown",
        "confirmed_moved": confirmed,
        "exception": str(reason),
    }


def execute_deposit(game, action):
    """One native command, never automatically retried after possible effects."""
    backend = getattr(game, "backend", "assisted")
    validate_deposit(action, backend=backend, expected_world=getattr(game, "expected_world", None))
    encoded = json.dumps(json.dumps(action, separators=(",", ":")))
    try:
        receipt = game.query(
            "local x=helpers.json_to_table("
            + encoded
            + ");local character_backend="
            + ("true" if backend == "character" else "false")
            + ";"
            + DEPOSIT_LUA
        )
    except Exception as exc:
        return _unknown(type(exc).__name__ + ": " + str(exc))
    if not isinstance(receipt, dict):
        return _unknown("invalid_storage_transfer_receipt")
    if receipt.get("reason") == "storage_transfer_outcome_unknown":
        confirmed = receipt.get("confirmed_moved")
        if type(confirmed) is int and 0 <= confirmed <= action["count"] and receipt.get("ok") is False:
            return receipt
        return _unknown("invalid_storage_uncertainty_receipt")
    moved = receipt.get("moved")
    valid = (
        type(moved) is int
        and 0 <= moved <= action["count"]
        and type(receipt.get("unit_number")) is int
        and receipt["unit_number"] == action["expected_entity_unit"]
        and receipt.get("world_id") == action["expected_entity_world_id"]
        and receipt.get("requested") == action["count"]
        and type(receipt.get("requested")) is int
        and receipt.get("item") == action["item"]
        and receipt.get("quality") == "normal"
    )
    if not valid:
        return _unknown("invalid_storage_transfer_receipt")
    if receipt.get("ok") is True and receipt.get("status") == ("succeeded" if moved else "waiting"):
        return receipt
    if (
        receipt.get("ok") is False
        and receipt.get("status") == "blocked"
        and moved == 0
        and receipt.get("reason") in KNOWN_FAILURES
    ):
        return receipt
    return _unknown("unexpected_storage_transfer_receipt")
