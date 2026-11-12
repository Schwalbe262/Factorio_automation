"""Fresh, bounded preconditions for the ordinary mining step of a belt bypass."""
from __future__ import annotations

import math

from .deterministic_underground_geometry import underground_edges


def validate_belt_switch_backend(action: dict, backend: str) -> None:
    if "belt_route_replacement" in action and backend != "assisted":
        raise ValueError("belt route switch requires the assisted backend")


def _positive(value):
    return type(value) is int and value > 0


def _position(value):
    if (not isinstance(value, dict) or set(value) != {"x", "y"}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value.values())):
        raise ValueError("belt switch requires finite exact positions")
    return value["x"], value["y"]


def _endpoint(spec):
    if not isinstance(spec, dict):
        raise ValueError("belt switch requires exact endpoint descriptors")
    keys = {"name", "position", "direction", "unit_number"}
    if spec.get("name") == "underground-belt":
        keys.add("belt_to_ground_type")
        if spec.get("belt_to_ground_type") not in ("input", "output"):
            raise ValueError("belt switch requires explicit underground roles")
    elif spec.get("name") != "transport-belt":
        raise ValueError("belt switch supports ordinary transport and underground belts")
    if (set(spec) != keys or not _positive(spec.get("unit_number"))
            or type(spec.get("direction")) is not int or spec["direction"] not in (0, 4, 8, 12)):
        raise ValueError("belt switch endpoint identity is invalid")
    return _position(spec["position"])


def validate_belt_route_replacement(action: dict) -> None:
    if "belt_route_replacement" not in action:
        return
    guard = action["belt_route_replacement"]
    keys = {"item", "entry_direction", "expected_actor_unit_number", "exit", "entities", "pairs"}
    if (not isinstance(guard, dict) or set(guard) != keys or action.get("type") != "mine"
            or action.get("name") != "transport-belt" or type(action.get("count", 1)) is not int
            or action.get("count", 1) != 1 or not _positive(action.get("expected_entity_unit"))
            or not isinstance(action.get("expected_entity_world_id"), str) or not action["expected_entity_world_id"]
            or not isinstance(guard.get("item"), str) or not guard["item"]
            or not _positive(guard.get("expected_actor_unit_number"))
            or type(guard.get("entry_direction")) is not int or guard["entry_direction"] not in (0, 4, 8, 12)):
        raise ValueError("invalid belt route mining guard")
    positions = {_position(action.get("position"))}
    units = {action["expected_entity_unit"]}
    entities, pairs = guard["entities"], guard["pairs"]
    if (not isinstance(entities, list) or not 1 <= len(entities) <= 32
            or not isinstance(pairs, list) or not 1 <= len(pairs) <= 16
            or not isinstance(guard["exit"], dict) or guard["exit"].get("name") != "transport-belt"):
        raise ValueError("belt switch requires a bounded replacement and surface exit")
    for spec in [guard["exit"], *entities]:
        point = _endpoint(spec)
        if point in positions or spec["unit_number"] in units:
            raise ValueError("belt switch endpoint identities must be unique")
        positions.add(point)
        units.add(spec["unit_number"])
    if guard["expected_actor_unit_number"] in units:
        raise ValueError("belt switch actor cannot be an endpoint")
    for pair in pairs:
        if (not isinstance(pair, dict) or set(pair) != {"input", "output", "max_distance"}
                or pair.get("input") not in entities or pair.get("output") not in entities):
            raise ValueError("belt switch pairs must reference exact replacement identities")
    underground_edges({"entities": entities, "underground_pairs": pairs})


BELT_ROUTE_REPLACEMENT_LUA = r'''
if x.belt_route_replacement then
 local q=x.belt_route_replacement
 if not d or d.world_id~=x.expected_entity_world_id or not a or not a.valid
  or a.unit_number~=q.expected_actor_unit_number or a.force~=f or a.surface~=s then
  return failure("belt_switch_actor_changed")
 end
 local function healthy(v)
  return v and v.valid and v.force==f and v.surface==s and v.quality.name=="normal"
   and v.health>0 and v.health==v.max_health
 end
 local function exact(spec)
  local v=target(spec.position,spec.name)
  if not healthy(v) or v.unit_number~=spec.unit_number or v.name~=spec.name
   or v.position.x~=spec.position.x or v.position.y~=spec.position.y or v.direction~=spec.direction
   or (spec.belt_to_ground_type and v.belt_to_ground_type~=spec.belt_to_ground_type) then return nil end
  return v
 end
 local function pure(v)
  local count=0
  local limit=v.type=="underground-belt" and v.get_max_transport_line_index() or 2
  for index=1,limit do
   for _,item in pairs(v.get_transport_line(index).get_contents()) do
    local quality=type(item.quality)=="string" and item.quality or (item.quality and item.quality.name)
    if item.name~=q.item or quality~="normal" then return nil end
    count=count+item.count
   end
  end
  return count
 end
 if not healthy(e) or e.name~="transport-belt" or e.type~="transport-belt" or not e.minable
  or e.unit_number~=x.expected_entity_unit or e.position.x~=x.position.x or e.position.y~=x.position.y
  or e.direction~=q.entry_direction then return failure("belt_switch_entry_changed") end
 local cargo=pure(e)
 if cargo==nil then return failure("belt_switch_entry_contaminated") end
 local exit=exact(q.exit)
 if not exit or pure(exit)==nil then return failure("belt_switch_exit_changed") end
 local live={}
 for _,spec in ipairs(q.entities) do
  local v=exact(spec)
  if not v or pure(v)==nil then return failure("belt_switch_replacement_changed") end
  live[spec.unit_number]=v
 end
 for _,pair in ipairs(q.pairs) do
  local first=live[pair.input.unit_number];local last=live[pair.output.unit_number]
  if not first or not last or first.underground_belt_neighbour~=last or last.underground_belt_neighbour~=first
   or first.belt_to_ground_type~="input" or last.belt_to_ground_type~="output" or first.direction~=last.direction then
   return failure("belt_switch_pair_changed")
  end
  local dx,dy=last.position.x-first.position.x,last.position.y-first.position.y
  local span=math.abs(dx)+math.abs(dy)
  local forward=(first.direction==0 and dx==0 and dy<0) or (first.direction==4 and dy==0 and dx>0)
   or (first.direction==8 and dx==0 and dy>0) or (first.direction==12 and dy==0 and dx<0)
  if not forward or span>pair.max_distance or span>first.prototype.max_underground_distance
   or span>last.prototype.max_underground_distance then return failure("belt_switch_pair_changed") end
 end
 if not inv or not inv.valid or not prototypes.item[q.item] then return failure("belt_switch_inventory_unavailable") end
 if inv.get_item_count{name="transport-belt",quality="normal"}<1 then return failure("belt_switch_replacement_item_missing") end
 --[[ Do not double-count empty slots through separate insertability queries or
 assume metadata-bearing cargo will merge. Each recovered item gets a slot. ]]
 if inv.count_empty_stacks(false,false)<cargo+1 then return failure("belt_switch_inventory_full") end
end
'''
