"""Conservative rectangle coverage: each complete cell fits inside one disk."""
from __future__ import annotations

from copy import deepcopy
import math


CELL_SIZE = 4
COVERAGE_MARGIN = 2


def coverage_cells(areas, cell_size=CELL_SIZE):
    if type(cell_size) not in (int,float) or not math.isfinite(cell_size) or cell_size<=0:
        raise ValueError('defense coverage cell size must be positive and finite')
    if not isinstance(areas, (list, tuple)) or not areas or len(areas)>16:
        raise ValueError('defense coverage requires one to sixteen finite rectangles')
    cells=[]
    for area in areas:
        if (not isinstance(area,(list,tuple)) or len(area)!=2
                or any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in area)
                or any(type(v) not in (int,float) or not math.isfinite(v) for p in area for v in p)
                or area[0][0]>=area[1][0] or area[0][1]>=area[1][1]):
            raise ValueError('defense coverage rectangles must have increasing finite bounds')
        x=area[0][0]
        while x<area[1][0]:
            x2=min(x+cell_size,area[1][0]);y=area[0][1]
            while y<area[1][1]:
                y2=min(y+cell_size,area[1][1])
                cells.append({'area':[[x,y],[x2,y2]],'weight':(x2-x)*(y2-y),
                              'corners':[[x,y],[x,y2],[x2,y],[x2,y2]]})
                if len(cells)>256:raise ValueError('defense coverage cell budget exceeded')
                y=y2
            x=x2
    return cells


def _distance(cell,position):
    return max(math.hypot(x-position['x'],y-position['y']) for x,y in cell['corners'])


def coverage_report(cells,posts,effective_range):
    covered=[];distances=[]
    for i,cell in enumerate(cells):
        distance=min((_distance(cell,p['position']) for p in posts),default=float('inf'))
        if distance<=effective_range+1e-9:covered.append(i);distances.append(distance)
    total=sum(c['weight']for c in cells);weight=sum(cells[i]['weight']for i in covered)
    return {'complete':len(covered)==len(cells),'cells':len(cells),'covered_cells':len(covered),
            'covered_area':weight,'total_area':total,'covered_fraction':weight/total,
            'maximum_covered_corner_distance':max(distances,default=None),'effective_range':effective_range,
            'uncovered_cells':[i for i in range(len(cells))if i not in covered],
            'metric':'every cell has all four corners within one turret disk; convexity covers the entire cell'}


def select_cover(cells,existing,candidates,effective_range,*,maximum_new_posts=16):
    """Greedy additive set cover with a three-tile minimum post separation."""
    result=[];remaining=set(coverage_report(cells,existing,effective_range)['uncovered_cells'])
    available=[]
    for candidate in candidates:
        covered={i for i in remaining if _distance(cells[i],candidate['position'])<=effective_range+1e-9}
        if covered:available.append((deepcopy(candidate),covered))
    while remaining:
        choices=[]
        for candidate,covered in available:
            position=candidate['position']
            if any(math.hypot(position['x']-p['position']['x'],position['y']-p['position']['y'])<3 for p in existing+result):continue
            gain=covered&remaining
            if not gain:continue
            weight=sum(cells[i]['weight']for i in gain)
            distance=sum(_distance(cells[i],position)*cells[i]['weight']for i in gain)/weight
            choices.append((-weight,distance,position['x'],position['y'],candidate,gain))
        if not choices or len(result)>=maximum_new_posts:return None
        _,_,_,_,chosen,gain=min(choices,key=lambda row:row[:4])
        result.append(chosen);remaining-=gain
    return result


def native_candidate_survey(game,candidates,areas,reserved,bounds):
    """Read-only native placement and local walk-grid access to footprint edges."""
    import json
    payload=json.dumps(json.dumps({'candidates':candidates,'areas':areas,'reserved':reserved,'bounds':bounds},separators=(',',':')))
    return game.query('''
local args=helpers.json_to_table('''+payload+''');local proto=prototypes.entity["gun-turret"]
if not proto or not proto.attack_parameters then return {ok=false,reason="native_turret_range_unavailable"} end
local function overlaps(a,b)return a[1][1]<b[2][1] and a[2][1]>b[1][1] and a[1][2]<b[2][2] and a[2][2]>b[1][2] end
local function reserved(area)for _,r in ipairs(args.reserved)do if overlaps(area,r)then return true end end;return false end
local b=args.bounds;local cache={};local reachable={};local queue={};local head=1
local function key(x,y)return x..":"..y end
local function walk(x,y)
 if x<b[1][1] or x>b[2][1] or y<b[1][2] or y>b[2][2]then return false end
 local k=key(x,y);if cache[k]~=nil then return cache[k]end
 local area={{x,y},{x+1,y+1}}
 local legal=not reserved(area) and s.can_place_entity{name="character",position={x=x+.5,y=y+.5},force=f}
 cache[k]=legal;return legal
end
for x=b[1][1],b[2][1]do for y=b[1][2],b[2][2]do
 local near=false;for _,area in ipairs(args.areas)do
  local dx=math.max(area[1][1]-(x+.5),0,(x+.5)-area[2][1]);local dy=math.max(area[1][2]-(y+.5),0,(y+.5)-area[2][2])
  if dx*dx+dy*dy<=4 then near=true;break end
 end
 if near and walk(x,y)then local k=key(x,y);reachable[k]=true;queue[#queue+1]={x,y}end
end end
while head<=#queue do local p=queue[head];head=head+1
 for _,v in ipairs{{1,0},{-1,0},{0,1},{0,-1}}do local x=p[1]+v[1];local y=p[2]+v[2];local k=key(x,y)
  if not reachable[k] and walk(x,y)then reachable[k]=true;queue[#queue+1]={x,y}end
 end
end
local legal={}
for _,p in ipairs(args.candidates)do
 local area={{p.x-proto.tile_width/2,p.y-proto.tile_height/2},{p.x+proto.tile_width/2,p.y+proto.tile_height/2}}
 if not reserved(area) and s.count_entities_filtered{area=area,type="resource"}==0
  and s.can_place_entity{name="gun-turret",position=p,direction=0,force=f}then
  local access=false
  for x=math.floor(area[1][1])-1,math.ceil(area[2][1])do for y=math.floor(area[1][2])-1,math.ceil(area[2][2])do
   local tile={{x,y},{x+1,y+1}};if not overlaps(tile,area)and reachable[key(x,y)]then access=true end
  end end
  if access then legal[#legal+1]={position=p,local_walk_access=true}end
 end
end
return {ok=true,candidates=legal,range=proto.attack_parameters.range,
 magazine_size=prototypes.item["firearm-magazine"].magazine_size,
 access_scope="four-neighbour native-walkable local component connected to protected footprint perimeter; planning snapshot"}
''')
