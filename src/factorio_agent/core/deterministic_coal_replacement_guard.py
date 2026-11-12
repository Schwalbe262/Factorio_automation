"""Atomic live preconditions for an ordinary paid coal-inserter replacement."""
from __future__ import annotations

import math

from .factory_templates import DIRECTIONS


def validate_coal_replacement_backend(action: dict, backend: str) -> None:
    if "coal_transit_replacement" in action and backend != "assisted":
        raise ValueError("coal transit replacement requires the assisted backend")


def _positive(value):
    return type(value) is int and value > 0


def _position(value):
    if (not isinstance(value, dict) or set(value) != {"x", "y"}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value.values())):
        raise ValueError("coal replacement requires finite exact positions")
    return value["x"], value["y"]


def validate_coal_transit_replacement(action: dict) -> None:
    if "coal_transit_replacement" not in action:
        return
    guard = action["coal_transit_replacement"]
    keys = {"item", "replacement", "expected_actor_unit_number", "expected_network_id", "old_direction", "pickup", "drop", "pole"}
    allowed = {"type", "name", "position", "count", "reason", "expected_entity_unit",
               "expected_entity_world_id", "coal_transit_replacement"}
    if (not isinstance(guard, dict) or set(guard) != keys or set(action) - allowed
            or action.get("type") != "mine" or action.get("name") != "burner-inserter"
            or type(action.get("count", 1)) is not int or action.get("count", 1) != 1
            or not _positive(action.get("expected_entity_unit"))
            or not isinstance(action.get("expected_entity_world_id"), str) or not action["expected_entity_world_id"]
            or guard.get("item") != "coal" or guard.get("replacement") != "fast-inserter"
            or not _positive(guard.get("expected_actor_unit_number"))
            or not _positive(guard.get("expected_network_id"))
            or type(guard.get("old_direction")) is not int or guard["old_direction"] not in DIRECTIONS):
        raise ValueError("invalid coal transit mining guard")
    position = _position(action.get("position"))
    positions, units = {position}, {action["expected_entity_unit"], guard["expected_actor_unit_number"]}
    if len(units) != 2:
        raise ValueError("coal replacement actor and target must differ")
    for label in ("pickup", "drop", "pole"):
        spec = guard[label]
        if (not isinstance(spec, dict) or set(spec) != {"name", "position", "direction", "unit_number"}
                or spec.get("name") != ("small-electric-pole" if label == "pole" else "transport-belt")
                or type(spec.get("direction")) is not int or spec["direction"] not in DIRECTIONS
                or not _positive(spec.get("unit_number"))):
            raise ValueError("invalid coal replacement endpoint identity")
        point = _position(spec["position"])
        if point in positions or spec["unit_number"] in units:
            raise ValueError("coal replacement identities must be distinct")
        positions.add(point)
        units.add(spec["unit_number"])
        if label != "pole":
            dx, dy = DIRECTIONS[guard["old_direction"]]
            sign = 1 if label == "pickup" else -1
            if point != (position[0] + sign * dx, position[1] + sign * dy):
                raise ValueError("coal replacement requires adjacent pickup and drop belts")


COAL_TRANSIT_REPLACEMENT_LUA = r'''
if x.coal_transit_replacement then
 local q=x.coal_transit_replacement
 if not d or d.world_id~=x.expected_entity_world_id or not a or not a.valid
  or a.unit_number~=q.expected_actor_unit_number or a.force~=f or a.surface~=s then
  return failure("coal_replacement_actor_changed")
 end
 local function normal(v) return v and v.quality and v.quality.name=="normal" end
 local function healthy(v)
  return v and v.valid and v.force==f and v.surface==s and normal(v)
   and v.health>0 and v.health==v.max_health
 end
 local function exact(spec)
  local v=target(spec.position,spec.name)
  if not healthy(v) or v.name~=spec.name or v.unit_number~=spec.unit_number or v.direction~=spec.direction
   or v.position.x~=spec.position.x or v.position.y~=spec.position.y then return nil end
  return v
 end
 if not healthy(e) or e.name~="burner-inserter" or e.type~="inserter" or not e.minable
  or e.unit_number~=x.expected_entity_unit or e.direction~=q.old_direction
  or e.position.x~=x.position.x or e.position.y~=x.position.y then return failure("coal_replacement_target_changed") end
 local pickup=exact(q.pickup);local drop=exact(q.drop);local pole=exact(q.pole)
 if not pickup or not drop or pickup.type~="transport-belt" or drop.type~="transport-belt" or not pole
  or pole.type~="electric-pole" then return failure("coal_replacement_endpoints_changed") end
 local function name(v) return type(v)=="string" and v or v and v.name end
 local function coal_count(rows)
  local count=0
  for _,item in pairs(rows) do
   if item.name~="coal" or name(item.quality)~="normal" or type(item.count)~="number"
    or item.count<1 or item.count~=math.floor(item.count) then return nil end
   count=count+item.count
  end
  return count
 end
 for _,belt in ipairs({pickup,drop}) do
  for lane=1,2 do
   if coal_count(belt.get_transport_line(lane).get_contents())==nil then return failure("coal_replacement_belt_contaminated") end
  end
 end
 local function same(a,b)
  return a and b and (a.x or a[1])==(b.x or b[1]) and (a.y or a[2])==(b.y or b[2])
 end
 local fast=prototypes.entity[q.replacement];local old=e.prototype;local recipe=f.recipes[q.replacement]
 if not fast or fast.type~="inserter" or not fast.electric_energy_source_prototype or fast.burner_prototype
  or not recipe or not recipe.enabled or not same(old.inserter_pickup_position,fast.inserter_pickup_position)
  or not same(old.inserter_drop_position,fast.inserter_drop_position)
  or not same(old.collision_box.left_top,fast.collision_box.left_top)
  or not same(old.collision_box.right_bottom,fast.collision_box.right_bottom) then
  return failure("coal_replacement_prototype_changed")
 end
 local mining=old.mineable_properties;local products=mining and mining.products
 if not products or #products~=1 or products[1].type~="item" or products[1].name~="burner-inserter"
  or products[1].amount~=1 or (products[1].probability and products[1].probability~=1) then
  return failure("coal_replacement_mining_product_changed")
 end
 local function rotate(p,direction)
  local px,py=p.x or p[1],p.y or p[2]
  if direction==4 then return -py,px elseif direction==8 then return -px,-py elseif direction==12 then return py,-px end
  return px,py
 end
 local function agrees(actual,offset,receiver)
  local dx,dy=rotate(offset,q.old_direction);local b=receiver.bounding_box
  return math.abs(actual.x-e.position.x-dx)<=1/256 and math.abs(actual.y-e.position.y-dy)<=1/256
   and actual.x>b.left_top.x and actual.x<b.right_bottom.x and actual.y>b.left_top.y and actual.y<b.right_bottom.y
 end
 if not agrees(e.pickup_position,fast.inserter_pickup_position,pickup)
  or not agrees(e.drop_position,fast.inserter_drop_position,drop) then return failure("coal_replacement_geometry_changed") end
 local network=pole.electric_network_id;local reach=pole.prototype.get_supply_area_distance(pole.quality)
 if not network or network~=q.expected_network_id or not reach or reach<=0
  or math.max(math.abs(pole.position.x-e.position.x),math.abs(pole.position.y-e.position.y))>reach then
  return failure("coal_replacement_power_unavailable")
 end
 local powered=false
 for _,generator in pairs(s.find_entities_filtered{force=f,type="generator"}) do
  if healthy(generator) and generator.electric_network_id==network and generator.energy>0 then powered=true;break end
 end
 if not powered then return failure("coal_replacement_power_unavailable") end
 local burner=e.burner;local fuel=burner and burner.inventory
 if not burner or not burner.valid or not fuel or not fuel.valid then return failure("coal_replacement_fuel_unavailable") end
 local cargo=coal_count(fuel.get_contents())
 if cargo==nil then return failure("coal_replacement_fuel_contaminated") end
 local burnt=burner.burnt_result_inventory
 if burnt and burnt.valid and not burnt.is_empty() then return failure("coal_replacement_burnt_result_present") end
 local burning=burner.currently_burning
 if burning and (name(burning.name)~="coal" or (burning.quality and name(burning.quality)~="normal")) then
  return failure("coal_replacement_fuel_contaminated")
 end
 if e.held_stack.valid_for_read then
  local held=coal_count{{name=e.held_stack.name,quality=e.held_stack.quality,count=e.held_stack.count}}
  if held==nil then return failure("coal_replacement_held_contaminated") end
  cargo=cargo+held
 end
 if not inv or not inv.valid then return failure("coal_replacement_inventory_unavailable") end
 if inv.get_item_count{name=q.replacement,quality="normal"}<1 then return failure("coal_replacement_item_missing") end
 --[[ Each physical recovered item gets its own free unfiltered/unbarred slot.
 Burning energy is deliberately excluded: only the engine's normal mine may
 recover inventory/held items. No fuel, energy or replacement is synthesized. ]]
 if inv.count_empty_stacks(false,false)<1+cargo then return failure("coal_replacement_inventory_full") end
end
'''
