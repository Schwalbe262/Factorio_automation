"""Conserved raw pickup and paid construction in a committed furnace gap."""

from __future__ import annotations

import json
import math
from pathlib import Path


def execute_gap_build(game, action, *, dry_run=False):
    allowed = {
        "type",
        "position",
        "direction",
        "resource",
        "expected_old_unit",
        "expected_actor_unit",
        "expected_world_id",
        "reason",
    }
    p = action.get("position")
    if (
        set(action) - allowed
        or action.get("type") != "build_furnace_gap"
        or action.get("direction") != 0
        or type(action.get("direction")) is not int
        or action.get("resource") not in ("iron-ore", "copper-ore")
        or any(type(action.get(k)) is not int or action[k] < 1 for k in ("expected_old_unit", "expected_actor_unit"))
        or not isinstance(action.get("expected_world_id"), str)
        or not action["expected_world_id"]
        or not isinstance(p, dict)
        or set(p) != {"x", "y"}
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in p.values())
    ):
        raise ValueError("gap build requires exact committed furnace, actor and world identity")
    if getattr(game, "expected_world", None) != action["expected_world_id"]:
        raise ValueError("gap build must target the bound world")
    if game.backend != "assisted":
        raise ValueError("atomic furnace gap construction requires the assisted backend")
    path = Path(game.cfg.runtime_dir) / "furnace-upgrade.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    pending = state.get("pending", {})
    if (
        state.get("phase") != "upgrade"
        or state.get("world_id") != action["expected_world_id"]
        or state.get("resource") != action["resource"]
        or pending.get("position") != p
        or pending.get("direction") != 0
        or pending.get("name") != "stone-furnace"
        or pending.get("unit_number") != action["expected_old_unit"]
    ):
        raise ValueError("gap build is restricted to the current prepaid replacement transaction")
    payload = dict(action, dry_run=dry_run)
    return game.query("local x=helpers.json_to_table(" + json.dumps(json.dumps(payload)) + ");" + GAP_BUILD_LUA)


GAP_BUILD_LUA = r"""
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if not d or d.world_id~=x.expected_world_id then return reject("furnace_gap_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.expected_actor_unit then return reject("furnace_gap_actor_changed")end
if target(x.position,"stone-furnace") or target(x.position,"steel-furnace")then return reject("furnace_gap_no_longer_empty")end
local inv=a.get_main_inventory()
if inv.get_item_count{name="steel-furnace",quality="normal"}<1 then return reject("prepaid_normal_steel_furnace_missing")end
if not s.can_place_entity{name="steel-furnace",position=x.position,direction=0,force=f,
 build_check_type=defines.build_check_type.manual_ghost,forced=true}then return reject("furnace_gap_other_collision")end
local b=prototypes.entity["steel-furnace"].collision_box
local rows={};local counts={};local total=0
for _,e in pairs(s.find_entities_filtered{area={{x.position.x+b.left_top.x,x.position.y+b.left_top.y},
 {x.position.x+b.right_bottom.x,x.position.y+b.right_bottom.y}}})do
 if e.type~="resource"then
  if e.type~="item-entity"then return reject("furnace_gap_foreign_entity")end
  local st=e.stack
  if not st.valid_for_read or st.prototype.type~="item"or st.quality.name~="normal"
   or(st.name~=x.resource and st.name~="coal")or st.health~=1 or st.spoil_tick~=0
   or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return reject("furnace_gap_foreign_stack")end
  total=total+st.count;if total>100 or #rows>=16 then return reject("furnace_gap_spill_budget_exceeded")end
  counts[st.name]=(counts[st.name]or 0)+st.count;rows[#rows+1]=e
 end
end
for name,count in pairs(counts)do if inv.get_insertable_count{name=name,quality="normal"}<count then return reject("furnace_gap_pickup_capacity_missing")end end
if x.dry_run then return {ok=true,status="ready",pickup=counts,paid_furnaces=1}end
local picked={}
for _,e in ipairs(rows)do local st=e.stack;local name=st.name;local count=st.count
 local moved=inv.insert{name=name,quality="normal",count=count}
 if moved>0 then
  picked[name]=(picked[name]or 0)+moved
  if moved==count then e.destroy()else st.count=count-moved end
 end
 if moved~=count then return reject("furnace_gap_native_pickup_incomplete")end
end
if not s.can_place_entity{name="steel-furnace",position=x.position,direction=0,force=f}then return reject("furnace_gap_collision_after_pickup")end
if inv.remove{name="steel-furnace",quality="normal",count=1}~=1 then return reject("furnace_gap_prepaid_item_changed")end
local built=s.create_entity{name="steel-furnace",position=x.position,direction=0,force=f,raise_built=true,player=a.player}
if not built then inv.insert{name="steel-furnace",quality="normal",count=1};return reject("furnace_gap_native_build_failed_refunded")end
return {ok=true,status="succeeded",unit_number=built.unit_number,picked=picked,paid_furnaces=1}
"""
