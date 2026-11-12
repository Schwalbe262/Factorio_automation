"""Bulk, read-only survey of the game map into a :class:`WorldMap`.

The requested area is widened to whole chunks and cut into square slices of
``chunk_tiles`` tiles; each slice costs exactly one ``game.query``. The Lua
only reads (find_tiles_filtered / find_entities_filtered / prototypes) and
never creates or destroys anything.

Reply format of one slice (all name fields are 1-based indices into
``names``)::

    terrain:  [[y, x, n, code], ...]     row runs, code 1 water 2 unbuildable
    ore:      [[y, x, n, name], ...]     row runs of 1 x 1 resource entities
    amounts:  [int, ...]                 one per ore tile, in run order
    wells:    [[tx, ty, name, amount, size], ...]   multi-tile resources
    features: [[kind, name, tx0, ty0, w, h], ...]   kind = FEATURE_KINDS code

A feature is reported by the slice that contains its anchor tile (see
``Rect.anchor``), so features crossing slice edges are never duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time

from .geometry import CHUNK_TILES, Rect, chunk_aligned, split_rect
from .rle import as_rows, decode_runs
from .worldmap import FEATURE_KINDS, TERRAIN_CODES, Feature, Well, WorldMap

# Never use "--" comments here: the game client joins the body onto one line.
SURVEY_LUA = r"""
local x0,y0,x1,y1=__AREA__
local A={left_top={x=x0,y=y0},right_bottom={x=x1,y=y1}}
local names={}
local index={}
local function id(n)
 local i=index[n]
 if not i then
  i=#names+1
  names[i]=n
  index[n]=i
 end
 return i
end
local function inside(x,y) return x>=x0 and x<x1 and y>=y0 and y<y1 end
local function emit(rows,out,amounts)
 for y=y0,y1-1 do
  local row=rows[y]
  if row then
   local x=x0
   while x<x1 do
    local cell=row[x]
    if cell then
     local start=x
     while x<x1 and row[x] and row[x].k==cell.k do
      if amounts then amounts[#amounts+1]=row[x].a end
      x=x+1
     end
     out[#out+1]={y,start,x-start,cell.k}
    else
     x=x+1
    end
   end
  end
 end
end
local probe=prototypes.entity["assembling-machine-1"].collision_mask.layers
local tclass={}
local blocked={}
for tname,tp in pairs(prototypes.tile) do
 local layers=tp.collision_mask.layers
 local hit=false
 for layer in pairs(probe) do
  if layers[layer] then hit=true end
 end
 if hit then
  blocked[#blocked+1]=tname
  if layers["water_tile"] and not layers["ground_tile"] then tclass[tname]=1 else tclass[tname]=2 end
 end
end
local trows={}
if #blocked>0 then
 for _,t in pairs(s.find_tiles_filtered{area=A,name=blocked}) do
  local p=t.position
  trows[p.y]=trows[p.y] or {}
  trows[p.y][p.x]={k=tclass[t.name]}
 end
end
local terrain={}
emit(trows,terrain,nil)
local orows={}
local wells={}
for _,e in pairs(s.find_entities_filtered{area=A,type="resource"}) do
 local p=e.position
 if inside(p.x,p.y) then
  local box=e.prototype.collision_box
  local size=math.floor(box.right_bottom.x-box.left_top.x+0.5)
  local tx=math.floor(p.x)
  local ty=math.floor(p.y)
  if size<=1 then
   orows[ty]=orows[ty] or {}
   orows[ty][tx]={k=id(e.name),a=e.amount}
  else
   wells[#wells+1]={tx,ty,id(e.name),e.amount,size}
  end
 end
end
local ore={}
local amounts={}
emit(orows,ore,amounts)
local features={}
local function add(code,e)
 local box=e.bounding_box
 local tx0=math.floor(box.left_top.x+0.001)
 local ty0=math.floor(box.left_top.y+0.001)
 local tx1=math.max(tx0+1,math.ceil(box.right_bottom.x-0.001))
 local ty1=math.max(ty0+1,math.ceil(box.right_bottom.y-0.001))
 local w=tx1-tx0
 local h=ty1-ty0
 if inside(tx0+math.floor(w/2),ty0+math.floor(h/2)) then
  features[#features+1]={code,id(e.name),tx0,ty0,w,h}
 end
end
for _,e in pairs(s.find_entities_filtered{area=A,type="tree"}) do add(1,e) end
for _,e in pairs(s.find_entities_filtered{area=A,type="simple-entity"}) do
 if e.prototype.mineable_properties.minable then add(2,e) else add(3,e) end
end
for _,e in pairs(s.find_entities_filtered{area=A,type="cliff"}) do add(4,e) end
local skip={["character"]=true,["entity-ghost"]=true,["tile-ghost"]=true,["item-request-proxy"]=true}
for _,e in pairs(s.find_entities_filtered{area=A,force=f}) do
 if e~=a and not skip[e.type] and next(e.prototype.collision_mask.layers)~=nil then add(5,e) end
end
return {ok=true,names=names,terrain=terrain,ore=ore,amounts=amounts,wells=wells,features=features}
"""


class SurveyError(RuntimeError):
    """The game refused or failed a survey slice."""


@dataclass
class SurveyResult:
    """A surveyed map plus cost metrics.

    ``seconds`` is wall time spent inside ``game.query``; ``reply_bytes`` is
    the compact-JSON size of all replies (an estimate of the wire size).
    """

    world: WorldMap
    queries: int = 0
    seconds: float = 0.0
    reply_bytes: int = 0
    max_reply_bytes: int = 0
    slices: list[Rect] = field(default_factory=list)


def survey_body(rect: Rect) -> str:
    """The Lua query body that surveys one slice."""
    area = f"{rect.x0},{rect.y0},{rect.x1},{rect.y1}"
    return SURVEY_LUA.replace("__AREA__", area)


def _as_rect(area) -> Rect:
    if isinstance(area, Rect):
        return area
    x0, y0, x1, y1 = area
    return Rect(int(x0), int(y0), int(x1), int(y1))


def _name(names: list, index: int) -> str:
    if not 1 <= index <= len(names):
        raise SurveyError(f"name index {index} out of range")
    return names[index - 1]


def _check_inside(rect: Rect, x: int, y: int) -> None:
    if not rect.contains(x, y):
        raise SurveyError(f"survey reply tile ({x}, {y}) lies outside slice {rect}")


def _merge_ore(world: WorldMap, rect: Rect, reply: dict, names: list) -> None:
    amounts = as_rows(reply.get("amounts"))
    tiles = list(decode_runs(as_rows(reply.get("ore"))))
    if len(tiles) != len(amounts):
        raise SurveyError(f"{len(tiles)} ore tiles but {len(amounts)} amounts")
    for (x, y, name_index), amount in zip(tiles, amounts):
        _check_inside(rect, x, y)
        world.set_ore(x, y, _name(names, name_index), amount)


def merge_reply(world: WorldMap, rect: Rect, reply: dict) -> None:
    """Replace ``rect``'s data in ``world`` with one survey reply."""
    if not isinstance(reply, dict) or not reply.get("ok"):
        reason = reply.get("reason") if isinstance(reply, dict) else reply
        raise SurveyError(f"survey of {rect} failed: {reason}")
    names = as_rows(reply.get("names"))
    world.clear_region(rect)
    for x, y, code in decode_runs(as_rows(reply.get("terrain"))):
        _check_inside(rect, x, y)
        world.set_terrain(x, y, TERRAIN_CODES[code])
    _merge_ore(world, rect, reply, names)
    for x, y, name_index, amount, size in as_rows(reply.get("wells")):
        _check_inside(rect, x, y)
        world.add_well(Well(_name(names, name_index), (x, y), int(amount), int(size)))
    for kind, name_index, x0, y0, width, height in as_rows(reply.get("features")):
        feature_rect = Rect(x0, y0, x0 + width, y0 + height)
        _check_inside(rect, *feature_rect.anchor())
        world.add_feature(Feature(FEATURE_KINDS[kind - 1], _name(names, name_index), feature_rect))
    world.mark_surveyed(rect)


def update(world: WorldMap, game, area, *, chunk_tiles: int = 96) -> SurveyResult:
    """Re-survey ``area`` (widened to whole chunks) into an existing map.

    ``game`` needs only ``query(body) -> dict``. One query per slice.
    """
    if chunk_tiles <= 0 or chunk_tiles % CHUNK_TILES:
        raise ValueError(f"chunk_tiles must be a positive multiple of {CHUNK_TILES}")
    result = SurveyResult(world)
    for rect in split_rect(chunk_aligned(_as_rect(area)), chunk_tiles):
        started = time.perf_counter()
        reply = game.query(survey_body(rect))
        result.seconds += time.perf_counter() - started
        result.queries += 1
        size = len(json.dumps(reply, separators=(",", ":")))
        result.reply_bytes += size
        result.max_reply_bytes = max(result.max_reply_bytes, size)
        result.slices.append(rect)
        merge_reply(world, rect, reply)
    return result


def survey_area(game, area, *, chunk_tiles: int = 96) -> SurveyResult:
    """Survey ``area`` (a Rect or (x0, y0, x1, y1) in tiles) into a new map."""
    return update(WorldMap(), game, area, chunk_tiles=chunk_tiles)
