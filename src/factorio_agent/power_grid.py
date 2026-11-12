"""Resource-backed pole-hop plans, independent from belt corridor routing."""
from __future__ import annotations

import heapq
import json
import math

from factorio_agent.core.deterministic_builder import _plan
from .board import entity
from .resource_policy import mining_reservations,validate_annotations


def plan_connection(game,builder,obs,destination,reserved=()):
    networks={e.get("electric_network_id") for e in obs["entities"] if e["name"]=="steam-engine" and e.get("energy",0)>0}
    sources=[e for e in obs["entities"] if e["name"]=="small-electric-pole" and e.get("electric_network_id") in networks]
    if not sources:return {"ok":False,"reason":"no_supplied_pole_network"}
    goal=(destination["x"],destination["y"])
    mining_goal=None
    for spec in reserved:
        area=spec.get('mining_footprint');resource=spec.get('mining_resource')
        if resource and area and area[0][0]<=goal[0]-.5 and goal[0]+.5<=area[1][0] and area[0][1]<=goal[1]-.5 and goal[1]+.5<=area[1][1]:
            mining_goal=dict(entity('small-electric-pole',*goal),mining_resource=resource,mining_footprint=area)
            try:validate_annotations([mining_goal],builder)
            except ValueError:return {'ok':False,'reason':'invalid_mining_power_goal'}
            break
    zones=mining_reservations(game,obs.get('world_id',''))
    closest=min(sources,key=lambda e:math.dist(goal,(e["position"]["x"],e["position"]["y"])))
    start=closest["position"]["x"],closest["position"]["y"]
    bounds=(min(start[0],goal[0])-12,max(start[0],goal[0])+12,min(start[1],goal[1])-12,max(start[1],goal[1])+12)
    points={(goal[0]+x*4,goal[1]+y*4)
            for x in range(math.ceil((bounds[0]-goal[0])/4),math.floor((bounds[1]-goal[0])/4)+1)
            for y in range(math.ceil((bounds[2]-goal[1])/4),math.floor((bounds[3]-goal[1])/4)+1)}
    starts={(e["position"]["x"],e["position"]["y"]):e for e in sources
            if bounds[0]<=e["position"]["x"]<=bounds[1] and bounds[2]<=e["position"]["y"]<=bounds[3]}
    points.update(starts);points.add(goal)
    if len(points)>2500:return {"ok":False,"reason":"power_connection_survey_budget_exceeded"}
    ordered=sorted(points)
    payload=json.dumps(json.dumps({'points':[{"x":x,"y":y} for x,y in ordered],
                                  'zones':zones,'goal':destination,'mining_goal':mining_goal},separators=(",",":")))
    result=game.query('''
local args=helpers.json_to_table('''+payload+''');local clear={}
for i,p in ipairs(args.points) do
 local old=target(p,"small-electric-pole")
 local legal=(old and old.force==f and old.position.x==p.x and old.position.y==p.y) or false
 if not legal and s.can_place_entity{name="small-electric-pole",position=p,force=f} then
  legal=true;local mining=args.mining_goal and p.x==args.goal.x and p.y==args.goal.y and args.mining_goal.mining_resource or nil
  for _,ore in pairs(s.find_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}) do
   if not mining or ore.name~=mining then legal=false;break end
  end
  if legal then for _,zone in ipairs(args.zones) do local b=zone.area
   if p.x+.49>b[1][1] and p.x-.49<b[2][1] and p.y+.49>b[1][2] and p.y-.49<b[2][2]
    and (not mining or zone.resource~=mining) then legal=false;break end
  end end
 end
 if legal then clear[#clear+1]=i end
end
return {ok=true,clear=clear}
''')
    if not result.get("ok"):return {"ok":False,"reason":result.get("reason","power_survey_failed")}
    legal={ordered[i-1] for i in result.get("clear",[])}
    legal-=builder._occupied_by_plan(list(reserved))
    if goal not in legal:return {"ok":False,"reason":"power_target_footprint_obstructed"}
    frontier=[];costs={};parents={}
    for position in starts:
        if position in legal:
            costs[position]=0;heapq.heappush(frontier,(math.dist(position,goal)/7,position))
    visited=0
    while frontier and visited<2500:
        _,node=heapq.heappop(frontier);visited+=1
        if node==goal:break
        for nxt in legal:
            distance=math.dist(node,nxt)
            if distance==0 or distance>7:continue
            cost=costs[node]+1+distance*.001
            if cost<costs.get(nxt,math.inf):
                costs[nxt]=cost;parents[nxt]=node
                heapq.heappush(frontier,(cost+math.dist(nxt,goal)/7,nxt))
    else:return {"ok":False,"reason":"no_bounded_pole_hop_connection"}
    path=[goal]
    while path[-1] not in starts:path.append(parents[path[-1]])
    path.reverse()
    entities=[dict(mining_goal) if mining_goal and (x,y)==goal else entity("small-electric-pole",x,y) for x,y in path[1:]]
    return _plan(entities,[],
                 template="power_grid_extension",source_unit_number=starts[path[0]].get("unit_number"),
                 source_position={"x":path[0][0],"y":path[0][1]},
                 destination=dict(destination),wire_hops=len(path)-1,maximum_hop_tiles=7)
