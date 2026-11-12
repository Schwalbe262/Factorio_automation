"""Underground endpoint metadata and current engine pairing evidence.

Transport lines can be shared by paired mouths; their contents are not additive
across endpoints. A successful endpoint build alone is not a network proof.
"""

UNDERGROUND_NAMES = frozenset({"underground-belt", "fast-underground-belt",
                               "express-underground-belt", "turbo-underground-belt"})


def underground_fields(spec: dict) -> dict:
    if spec.get("name") not in UNDERGROUND_NAMES:
        if "belt_to_ground_type" in spec:
            raise ValueError("underground role supplied for another entity")
        return {}
    role = spec.get("belt_to_ground_type")
    if role not in ("input", "output"):
        raise ValueError("underground belt requires input or output role")
    direction = spec.get("direction", 0)
    if type(direction) is not int or direction not in (0, 4, 8, 12):
        raise ValueError("underground belt requires a cardinal direction")
    return {"belt_to_ground_type": role}


OBSERVE_UNDERGROUND_LUA = r'''
local function observe_underground(e)
 local row={belt_to_ground_type=e.belt_to_ground_type,force=e.force.name,surface=e.surface.name,
  transport_lines={},belt_inventory={},underground_pair_verified=false,
  max_underground_distance=e.prototype.max_underground_distance}
 for index=1,e.get_max_transport_line_index() do
  local items={};local contents=e.get_transport_line(index).get_contents()
  for _,item in pairs(contents) do
   items[item.name]=(items[item.name] or 0)+item.count
   row.belt_inventory[item.name]=(row.belt_inventory[item.name] or 0)+item.count
  end
  row.transport_lines[#row.transport_lines+1]={index=index,items=items,contents=contents}
 end
 local n=e.underground_belt_neighbour
 if n and n.valid then
  row.underground_neighbour={name=n.name,unit_number=n.unit_number,position=pos(n.position),direction=n.direction,
   belt_to_ground_type=n.belt_to_ground_type,force=n.force.name,surface=n.surface.name,
   reciprocal=n.underground_belt_neighbour==e}
  local first=e.belt_to_ground_type=="input" and e or n
  local last=e.belt_to_ground_type=="input" and n or e
  local dx,dy=last.position.x-first.position.x,last.position.y-first.position.y
  local span=math.abs(dx)+math.abs(dy)
  local forward=(e.direction==0 and dx==0 and dy<0) or (e.direction==4 and dy==0 and dx>0)
   or (e.direction==8 and dx==0 and dy>0) or (e.direction==12 and dy==0 and dx<0)
  row.underground_pair_verified=row.underground_neighbour.reciprocal and n.name==e.name and n.force==e.force
   and n.surface==e.surface and n.direction==e.direction and first.belt_to_ground_type=="input"
   and last.belt_to_ground_type=="output" and forward and span<=e.prototype.max_underground_distance or false
  row.underground_span=span
 end
 return row
end
'''
