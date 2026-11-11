"""Whole orthogonal corridors with explicit underground crossing pairs."""
from __future__ import annotations

import json

from factorio_ai.deterministic_builder import _plan
from .board import entity
from .routing import VECTORS,route_trunks
from .resource_policy import (RESOURCES, mining_reservations, annotate_mining_exit,
                              mining_exit_candidates, exit_directions, MINING_EXIT_LUA)


def xy(e):
    return e['position']['x'],e['position']['y']


def compile_crossings(route,foreign,*,maximum_span=5):
    """Cross foreign surface belts without removing or joining their network.

    Every crossing is a straight input/output pair. Reject turns in buried
    spans, overlapping pairs and nearby same-axis mouths that could steal pairs.
    """
    if not route.get('ok'):return route
    segments=route['segments'];positions=[xy(e) for e in segments]
    surface={xy(e):e for e in foreign if e['name']=='transport-belt'}
    underground=[e for e in foreign if e['name']=='underground-belt']
    hits=[i for i,p in enumerate(positions) if p in surface]
    spans=[]
    for index in hits:
        if spans and index<=spans[-1][1]+1:
            spans[-1]=(spans[-1][0],index+1)
        else:spans.append((index-1,index+1))
    result=[];pairs=[];buried=set();mouths={}
    for first,last in spans:
        if first<=0 or last>=len(segments)-1 or last-first>maximum_span:
            return {'ok':False,'reason':'crossing_has_no_clear_straight_mouths_or_exceeds_span'}
        direction=segments[first]['direction'];v=VECTORS[direction]
        if segments[first-1]['direction']!=direction or segments[last]['direction']!=direction:
            return {'ok':False,'reason':'underground_mouth_requires_straight_approach_and_exit'}
        for i in range(first,last):
            if segments[i]['direction']!=direction or positions[i+1]!=(positions[i][0]+v[0],positions[i][1]+v[1]):
                return {'ok':False,'reason':'underground_crossing_would_include_a_turn'}
        if positions[first] in surface or positions[last] in surface:
            return {'ok':False,'reason':'underground_mouth_would_replace_foreign_belt'}
        if any(surface[positions[i]]['direction']%8==direction%8 for i in range(first+1,last) if positions[i] in surface):
            return {'ok':False,'reason':'foreign_parallel_belt_requires_explicit_junction'}
        for index,role in ((first,'input'),(last,'output')):
            p=positions[index]
            for other in underground:
                q=xy(other)
                aligned=(p[0]==q[0]) if direction%8==0 else (p[1]==q[1])
                if other['direction']==direction and aligned and abs(p[0]-q[0])+abs(p[1]-q[1])<=maximum_span:
                    return {'ok':False,'reason':'nearby_underground_endpoint_could_steal_pair'}
            mouths[index]=dict(entity('underground-belt',*p,direction),belt_to_ground_type=role)
        buried.update(range(first+1,last));pairs.append({'input':list(positions[first]),'output':list(positions[last]),'direction':direction,'span':last-first})
    for i,segment in enumerate(segments):
        if i not in buried:result.append(mouths.get(i,dict(segment,name='transport-belt')))
    # Reject accidental surface merges in either direction. Input mouths carry
    # forward underground, not onto the foreign crossing above their tunnel.
    own={xy(e):e for e in result}
    for p,e in own.items():
        if e.get('belt_to_ground_type')=='input' or p==positions[-1]:continue
        vx,vy=VECTORS[e['direction']]
        if (p[0]+vx,p[1]+vy) in surface:
            return {'ok':False,'reason':'route_would_feed_foreign_surface_belt'}
    for p,e in surface.items():
        vx,vy=VECTORS[e['direction']]
        recipient=(p[0]+vx,p[1]+vy)
        if recipient==positions[0] and e['direction']==segments[0]['direction']:continue
        if recipient in own:
            return {'ok':False,'reason':'foreign_surface_belt_would_feed_route'}
    return _plan(result,[],underground_pairs=pairs,route_metrics=route['metrics'],layout_policy='pcb-layered-trunks-v1')


def plan_connection(game,builder,obs,source,destination,reserved=(),*,ignored_units=()):
    if source.get('item')!=destination.get('item'):
        return {'ok':False,'reason':'port_item_mismatch'}
    mining_resource=source.get('mining_resource')
    if mining_resource is not None and (mining_resource not in RESOURCES or mining_resource!=source.get('item') or source.get('facing')!=4):
        return {'ok':False,'reason':'invalid_registered_mining_exit_source'}
    a,b=source['position'],destination['position'];ends={(a['x'],a['y']),(b['x'],b['y'])}
    bounds={'min_x':min(a['x'],b['x'])-16,'max_x':max(a['x'],b['x'])+16,
            'min_y':min(a['y'],b['y'])-16,'max_y':max(a['y'],b['y'])+16}
    if (bounds['max_x']-bounds['min_x']+1)*(bounds['max_y']-bounds['min_y']+1)>40000:
        return {'ok':False,'reason':'belt_corridor_survey_budget_exceeded'}
    zones=mining_reservations(game,obs['world_id'])
    payload=json.dumps(json.dumps({'bounds':bounds,'ignored':list(ignored_units),'source':source,
                                  'destination':destination,'zones':zones,
                                  'exit_candidates':mining_exit_candidates(a)},separators=(',',':')))
    survey=game.query('''
local args=helpers.json_to_table('''+payload+''');local b=args.bounds;local blocked={};local clearable={}
local ignored={};for _,unit in ipairs(args.ignored) do ignored[unit]=true end
local function key(p)return p.x..":"..p.y end
'''+MINING_EXIT_LUA+'''
local function patch_at(p)
 return mining_patch_at(p,args.source.mining_resource,args.zones)
end
local exit_allowed={};local exit_shared={};local exit_prefix={};local exit_length=-1;local exit_directions={}
local source_patch=patch_at(args.source.position)
if source_patch then
 if not args.source.mining_resource or args.source.facing~=4 then return {ok=false,reason="corridor_resource_source_requires_registered_exit"} end
 local chosen=select_mining_exit(args.source,args.zones,ignored,{},args.exit_candidates,nil,true)
 if not chosen.ok then return chosen end
 exit_prefix=chosen.exit_prefix;exit_length=chosen.exit_length
 for index,p in ipairs(exit_prefix) do
  exit_shared[key(p)]=true
  if index<#exit_prefix then
   exit_allowed[key(p)]=index-1;local q=exit_prefix[index+1]
   exit_directions[key(p)]=q.y<p.y and 0 or q.y>p.y and 8 or 4
  end
 end
end
local destination_patch=patch_at(args.destination.position)
if destination_patch then return {ok=false,reason="corridor_destination_on_resource_or_mining_reservation"} end
for x=b.min_x,b.max_x do for y=b.min_y,b.max_y do
 local p={x=x,y=y};local old=target(p,"transport-belt")
 if old and ignored[old.unit_number] then old=nil end
 local entities=s.find_entities_filtered{area={{x-.4,y-.4},{x+.4,y+.4}}}
 local on_patch=false
 for _,e in pairs(entities) do if e.type=="resource" then on_patch=true;break end end
 if not on_patch then for _,zone in ipairs(args.zones) do local q=zone.area
  if x+.4>q[1][1] and x-.4<q[2][1] and y+.4>q[1][2] and y-.4<q[2][2] then on_patch=true;break end
 end end
 local legal=(old and old.force==f) or s.can_place_entity{name="transport-belt",position=p,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true}
 if not legal then
  local replacing=false;for _,e in pairs(entities) do if ignored[e.unit_number] then replacing=true end end
  if replacing then
   legal=true;for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers) do
    if s.get_tile(p).collides_with(layer) then legal=false;break end
   end
  end
 end
 local natural={}
 if on_patch and exit_allowed[key(p)]==nil then legal=false end
 if old and exit_shared[key(p)] then
  if exit_directions[key(p)] and old.direction~=exit_directions[key(p)] then return {ok=false,reason="mining_exit_existing_belt_direction_changed",position=p} end
  for lane=1,2 do for _,item in pairs(old.get_transport_line(lane).get_contents()) do
   if item.name~=args.source.item then return {ok=false,reason="mining_exit_existing_belt_contaminated",position=p} end
  end end
 end
 if legal then for _,e in pairs(entities) do
  if e~=a and e.type~="resource" and e~=old and not ignored[e.unit_number] and not e.prototype.collision_mask.colliding_with_tiles_only then
   local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
   if e.force.name=="neutral" and e.minable and (e.type=="tree" or rock) then
    natural[#natural+1]={name=e.name,position=pos(e.position),unit_number=e.unit_number}
   else legal=false;break end
  end
 end end
 if not legal then blocked[#blocked+1]=p elseif #natural>0 then
  clearable[#clearable+1]={position=p,entities=natural}
 end
end end
return {ok=true,blocked=blocked,clearable=clearable,max_span=prototypes.entity["underground-belt"].max_underground_distance,
 exit_prefix=exit_prefix,exit_length=exit_length}
''')
    if not survey.get('ok'):return survey
    # Surface belts are crossable, all other prospective footprints are hard.
    all_entities={ (e['name'],*xy(e)):e for e in obs['entities']+list(reserved) if e.get('unit_number') not in ignored_units }
    prefix=survey.get('exit_prefix',[])
    if prefix=={}:prefix=[]
    try: prefix_directions=exit_directions(prefix) if prefix else []
    except ValueError as exc:return {'ok':False,'reason':'invalid_native_mining_exit','detail':str(exc)}
    prefix_headings={(p['x'],p['y']):d for p,d in zip(prefix,prefix_directions)}
    foreign=[e for e in all_entities.values() if e['name'] in {'transport-belt','underground-belt'} and xy(e) not in ends
             and not (e['name']=='transport-belt' and e['direction']==prefix_headings.get(xy(e)))]
    hard={(p['x'],p['y']) for p in survey['blocked']}
    hard.update(builder._occupied_by_plan([e for e in reserved if e['name']!='transport-belt']))
    hard-=ends
    surface={xy(e) for e in foreign if e['name']=='transport-belt'}
    rejected=0
    def accepts_layers(candidate):
        nonlocal rejected
        if prefix:
            segments=candidate.get('segments',[])
            if len(segments)<len(prefix) or any(xy(segments[i])!=(p['x'],p['y'])
                    or (i<len(prefix)-1 and segments[i]['direction']!=prefix_directions[i]) for i,p in enumerate(prefix)):
                return False
        layered=compile_crossings(candidate,foreign,maximum_span=survey['max_span'])
        accepted=layered.get('ok',False)
        if accepted and prefix:
            by_position={xy(e):e for e in layered['entities']}
            accepted=all(by_position.get((p['x'],p['y']),{}).get('name')=='transport-belt' for p in prefix[:-1])
        if not accepted:rejected+=1
        return accepted
    distance=abs(a['x']-b['x'])+abs(a['y']-b['y'])
    node_budget=max(150000,min(400000,int(distance+1)*2000))
    for attempt in range(8):
        route=route_trunks(a,b,occupied=hard,bounds=bounds,start_direction=source['facing'],
                           end_direction=destination['facing'],max_nodes=node_budget,candidate_validator=accepts_layers)
        compiled=compile_crossings(route,foreign,maximum_span=survey['max_span'])
        if compiled.get('ok'):break
        if not route.get('ok'):return compiled
        conflicts={xy(e) for e in route['segments']} & surface - ends
        fresh=conflicts-hard
        if not fresh:return compiled
        # Reject these unsafe crossing cells, then choose another COMPLETE
        # corridor under the same original turn/detour gates. Never maze-route.
        hard.update(fresh)
    else:return {'ok':False,'reason':'bounded_layered_corridor_replanning_exhausted'}
    compiled['routing_attempts']=attempt+1
    if survey.get('exit_length',-1)>=0:
        annotate_mining_exit(compiled['entities'],mining_resource,a,survey['exit_length'],path=prefix)
        compiled['mining_exit']={'resource':mining_resource,'source_position':dict(a),'last_step':survey['exit_length']}
    compiled['layer_candidates_rejected']=rejected
    compiled['ports']=[source,destination]
    positions={xy(e) for e in compiled['entities']}
    allowed_tiles=set();obstacles={}
    for row in survey.get('clearable',[]):
        p=row['position']['x'],row['position']['y']
        if p not in positions:continue
        allowed_tiles.add(p)
        for e in row['entities']:obstacles[(e['name'],*xy(e))]=e
    if len(obstacles)>64:return {'ok':False,'reason':'corridor_natural_clearance_budget_exceeded'}
    compiled['clear_obstacles']=list(obstacles.values())
    compiled['clearance_world_id']=obs['world_id']
    preflight=builder.can_place(compiled['entities'])
    # Prospective destination/source mouths are deliberately shared with blocks.
    failures=[e for e in preflight.get('blocked',[]) if xy(e) not in allowed_tiles or e.get('reason')!='terrain_or_entity_collision']
    if not preflight.get('ok') and (not preflight.get('blocked') or failures):
        return {'ok':False,'reason':'native_corridor_preflight_failed','blocked':failures,'native_reason':preflight.get('reason')}
    return compiled


def underground_proof(obs,plan):
    by_position={xy(e):e for e in obs['entities'] if e['name']=='underground-belt'}
    for pair in plan.get('underground_pairs',[]):
        first=by_position.get(tuple(pair['input']));last=by_position.get(tuple(pair['output']))
        if not first or not last:return False
        if not first.get('underground_pair_verified') or not last.get('underground_pair_verified'):return False
        if first.get('underground_neighbour',{}).get('unit_number')!=last['unit_number']:return False
        if first.get('direction')!=pair['direction'] or last.get('direction')!=pair['direction']:return False
    return True
