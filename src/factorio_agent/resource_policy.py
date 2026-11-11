"""Resource keepouts with narrow mining-core and bounded source-exit scopes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


RESOURCES = frozenset({'iron-ore', 'copper-ore', 'coal', 'stone'})
MINING_NAMES = frozenset({'electric-mining-drill', 'burner-mining-drill', 'transport-belt', 'small-electric-pole'})


def _position(e):
    return e['position']['x'], e['position']['y']


def _area(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in value)
            and all(type(v) in (int, float) and math.isfinite(v) for p in value for v in p)
            and value[0][0] < value[1][0] and value[0][1] < value[1][1])


def mark_mining_core(entities, resource):
    """Annotate only the generator's local drills, collector and small poles."""
    if resource not in RESOURCES or any(e['name'] not in MINING_NAMES for e in entities):
        raise ValueError('mining core may contain only approved local mining entities')
    sizes = {'electric-mining-drill': 3, 'burner-mining-drill': 2, 'transport-belt': 1, 'small-electric-pole': 1}
    area = [[min(e['position']['x'] - sizes[e['name']] / 2 for e in entities),
             min(e['position']['y'] - sizes[e['name']] / 2 for e in entities)],
            [max(e['position']['x'] + sizes[e['name']] / 2 for e in entities),
             max(e['position']['y'] + sizes[e['name']] / 2 for e in entities)]]
    columns=sorted({e['position']['x'] for e in entities if e['name'] in ('electric-mining-drill','burner-mining-drill')})
    if not columns:raise ValueError('local mining core requires its actual drill columns')
    groups={x:[] for x in columns}
    for e in entities:
        column=min(columns,key=lambda x:(abs(x-e['position']['x']),x))
        groups[column].append(e)
    footprints={}
    for column,group in groups.items():
        footprints[column]=[[min(e['position']['x']-sizes[e['name']]/2 for e in group),
                             min(e['position']['y']-sizes[e['name']]/2 for e in group)],
                            [max(e['position']['x']+sizes[e['name']]/2 for e in group),
                             max(e['position']['y']+sizes[e['name']]/2 for e in group)]]
    for e in entities:
        column=min(columns,key=lambda x:(abs(x-e['position']['x']),x))
        e['mining_resource'] = resource; e['mining_footprint'] = deepcopy(footprints[column])
    return area


def mining_exit_candidates(source_position, *, extra_clear_steps=0):
    """Three bounded whole prefixes, in preference order; never maze search."""
    if type(extra_clear_steps) is not int or not 0 <= extra_clear_steps <= 4:
        raise ValueError('off-patch mining connector is limited to four additional cells')
    x, y = source_position['x'], source_position['y']
    return [[{'x': x + i, 'y': y} for i in range(13+extra_clear_steps)]] + [
        [dict(source_position)] + [{'x': x + 1, 'y': y + sign * i} for i in range(12+extra_clear_steps)]
        for sign in (-1, 1)]


def exit_directions(path):
    vectors = {(0, -1): 0, (1, 0): 4, (0, 1): 8, (-1, 0): 12}
    try:
        if (not 2 <= len(path) <= 13 or any(not isinstance(p, dict) or set(p) != {'x', 'y'}
                or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ('x', 'y')) for p in path)
                or len({_position({'position': p}) for p in path}) != len(path)):
            raise ValueError('invalid mining exit path')
        directions = [vectors[(b['x']-a['x'], b['y']-a['y'])] for a, b in zip(path, path[1:])]
        if directions[0] != 4 or (len(set(directions)) != 1
                and (directions[1] not in (0, 8) or any(d != directions[1] for d in directions[1:]))):
            raise ValueError('mining exit must go EAST straight or EAST once then NORTH/SOUTH')
        return directions
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError('invalid mining exit path') from exc


def annotate_mining_exit(entities, resource, source_position, length, *, path=None):
    """Tag only the necessary on-patch prefix; the first clear cell is untagged."""
    if resource not in RESOURCES or type(length) is not int or not 0 <= length <= 11:
        raise ValueError('mining exit needs a known resource and first-clear tile within twelve steps')
    if path is not None:
        directions = exit_directions(path)
        if path[0] != source_position or len(path) != length + 2:
            raise ValueError('mining exit path must include the source through the first clear tile')
        by_position = {_position(e): e for e in entities}
        for i, p in enumerate(path):
            e = by_position.get((p['x'], p['y']))
            if not e or e['name'] != 'transport-belt' or (i < len(directions) and e.get('direction') != directions[i]):
                raise ValueError('mining exit requires its complete ordered surface-belt path')
        for i, p in enumerate(path[:-1]):
            e = by_position[p['x'], p['y']]
            if e.get('mining_footprint'):
                continue
            e['mining_resource'] = resource
            e['mining_exit'] = {'source_position': dict(source_position), 'facing': 4, 'step': i,
                                'length': length, 'path': deepcopy(path)}
        return entities
    for e in entities:
        step = e['position']['x'] - source_position['x']
        if e['position']['y'] == source_position['y'] and step % 1 == 0 and 0 <= step <= length:
            if e['name'] != 'transport-belt' or e.get('direction') != 4:
                raise ValueError('mining exit cannot turn or use underground mouths on the patch')
            e['mining_resource'] = resource
            e['mining_exit'] = {'source_position': dict(source_position), 'facing': 4, 'step': int(step), 'length': length}
    return entities


def validate_annotations(entities, builder):
    for e in entities:
        if {'resource_virtual_tunnel','resource_tunnel_anchors','resource_preserved_unit','survey_area'} & set(e):
            raise ValueError('resource survey internals cannot be supplied as construction metadata')
        if 'mining_resource' not in e:
            if 'mining_exit' in e or 'mining_footprint' in e:
                raise ValueError('mining scope requires its explicit resource')
            continue
        if e['mining_resource'] not in RESOURCES or e['name'] not in MINING_NAMES:
            raise ValueError('resource exemption is limited to local mining drills, belts and small poles')
        exit_spec = e.get('mining_exit')
        if exit_spec is not None:
            p = exit_spec.get('source_position') if isinstance(exit_spec, dict) else None
            path = exit_spec.get('path') if isinstance(exit_spec, dict) else None
            if (not isinstance(p, dict) or set(p) != {'x', 'y'} or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ('x', 'y'))
                    or e['name'] != 'transport-belt' or exit_spec.get('facing') != 4
                    or type(exit_spec.get('step')) is not int or type(exit_spec.get('length')) is not int
                    or not 0 <= exit_spec['step'] <= exit_spec['length'] <= 11):
                raise ValueError('mining exit must be a bounded EAST source prefix')
            if path is None:
                if e.get('direction') != 4 or _position(e) != (p['x'] + exit_spec['step'], p['y']):
                    raise ValueError('mining exit must be a bounded straight EAST prefix')
            else:
                directions = exit_directions(path)
                if (path[0] != p or len(path) != exit_spec['length'] + 2
                        or e['position'] != path[exit_spec['step']] or e.get('direction') != directions[exit_spec['step']]):
                    raise ValueError('mining exit path metadata does not match its surface belt')
                by_position = {_position(v): v for v in entities}
                for index, cell in enumerate(path):
                    v = by_position.get((cell['x'], cell['y']))
                    if (not v or v['name'] != 'transport-belt'
                            or (index < len(directions) and v.get('direction') != directions[index])
                            or (index < len(directions) and v.get('mining_resource') != e['mining_resource'])):
                        raise ValueError('mining exit path requires all its matching ordered surface belts')
        else:
            area = e.get('mining_footprint')
            if (not _area(area) or area[1][0] - area[0][0] > 40 or area[1][1] - area[0][1] > 12
                    or any(not (area[0][0] <= x - .5 and x + .5 <= area[1][0]
                                and area[0][1] <= y - .5 and y + .5 <= area[1][1])
                           for x, y in builder._occupied_by_plan([e]))):
                raise ValueError('local mining exemption requires its bounded exact core footprint')


def mining_reservations(game, world_id):
    cfg = getattr(game, 'cfg', None); root = getattr(cfg, 'runtime_dir', None)
    if root is None: return []
    path = Path(root) / 'pcb-board.json'
    if not path.exists(): return []
    board = json.loads(path.read_text(encoding='utf-8'))
    if board.get('world_id') != world_id: raise ValueError('resource reservation board world changed')
    result = []
    for block in board.get('blocks', {}).values():
        resource = block.get('resource')
        if resource in RESOURCES and any(e.get('name') in ('electric-mining-drill', 'burner-mining-drill') for e in block.get('entities', [])):
            area = block.get('mining_reserved_area', block.get('reserved_area', block.get('mining_footprint')))
            if 'mining_reserved_area' in block:
                outer = block.get('reserved_area')
                if (not _area(area) or not _area(outer)
                        or any(not outer[0][axis] <= area[0][axis] < area[1][axis] <= outer[1][axis] for axis in (0, 1))):
                    raise ValueError('explicit mining reservation must be inside its complete bank reservation')
            if _area(area): result.append({'area': area, 'resource': resource})
    return result


def policy_specs(builder, obs, entities, *, ignored_units=()):
    validate_annotations(entities, builder)
    actual = {(e['name'], *_position(e)): e for e in obs.get('entities', [])}; ignored = set(ignored_units)
    specs = []
    for e in entities:
        box = builder._occupied_by_plan([e]); xs = [p[0] for p in box]; ys = [p[1] for p in box]
        spec = dict(e, survey_area=[[min(xs) - .49, min(ys) - .49], [max(xs) + .49, max(ys) + .49]])
        old = actual.get((e['name'], *_position(e)))
        if (old and old.get('unit_number') not in ignored and type(old.get('unit_number')) is int
                and old.get('direction', 0) == e.get('direction', 0)
                and old.get('belt_to_ground_type') == e.get('belt_to_ground_type')
                and (not e.get('recipe') or old.get('recipe') == e['recipe'])
                and all(old.get(k)==e[k] for k in ('splitter_input_priority','splitter_output_priority') if k in e)):
            spec['resource_preserved_unit'] = old['unit_number']
        specs.append(spec)
    desired={(e['name'],*_position(e)) for e in entities if e['name'].endswith('underground-belt')}
    mouths={(e['name'],*_position(e)):e for e in obs.get('entities',[])
            if e['name'].endswith('underground-belt') and e.get('unit_number') not in ignored}
    mouths.update({(e['name'],*_position(e)):e for e in entities if e['name'].endswith('underground-belt')})
    vectors={0:(0,-1),4:(1,0),8:(0,1),12:(-1,0)}
    for first in (e for e in mouths.values() if e.get('belt_to_ground_type')=='input'):
        if first.get('direction') not in vectors:continue
        dx,dy=vectors[first['direction']];fx,fy=_position(first);options=[]
        for last in mouths.values():
            if last['name']!=first['name'] or last.get('belt_to_ground_type')!='output' or last.get('direction')!=first['direction']:continue
            lx,ly=_position(last);distance=(lx-fx)*dx+(ly-fy)*dy
            if distance>0 and (lx-fx)*dy==(ly-fy)*dx:options.append((distance,last))
        if not options:continue
        distance,last=min(options,key=lambda row:row[0])
        if not ({(first['name'],fx,fy),(last['name'],*_position(last))}&desired):continue
        if distance%1:raise ValueError('invalid bounded underground resource survey span')
        if distance>32:continue
        first_old=actual.get((first['name'],fx,fy));last_old=actual.get((last['name'],*_position(last)))
        anchors=None
        if (first_old and last_old and first_old.get('unit_number') not in ignored and last_old.get('unit_number') not in ignored
                and first_old.get('underground_pair_verified') and last_old.get('underground_pair_verified')
                and first_old.get('direction')==first['direction'] and last_old.get('direction')==last['direction']
                and first_old.get('belt_to_ground_type')=='input' and last_old.get('belt_to_ground_type')=='output'
                and first_old.get('underground_neighbour',{}).get('unit_number')==last_old.get('unit_number')
                and last_old.get('underground_neighbour',{}).get('unit_number')==first_old.get('unit_number')):
            anchors=[{k:e[k] for k in ('name','position','unit_number','direction','belt_to_ground_type')} for e in (first_old,last_old)]
        for step in range(1,int(distance)):
            x,y=fx+step*dx,fy+step*dy
            specs.append({'name':first['name'],'position':{'x':x,'y':y},'direction':first['direction'],
                          'survey_area':[[x-.49,y-.49],[x+.49,y+.49]],'resource_virtual_tunnel':True,
                          'resource_tunnel_anchors':anchors,'resource_tunnel_span':distance})
    return specs


MINING_EXIT_LUA = r'''
local function mining_patch_at(p,resource,zones)
 local occupied=false;local mismatch=false
 for _,ore in pairs(s.find_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}) do
  occupied=true;if ore.name~=resource then mismatch=true end
 end
 for _,zone in ipairs(zones) do local q=zone.area
  if p.x+.49>q[1][1] and p.x-.49<q[2][1] and p.y+.49>q[1][2] and p.y-.49<q[2][2] then
   occupied=true;if zone.resource~=resource then mismatch=true end
  end
 end
 return occupied,mismatch
end
local function select_mining_exit(source,zones,ignored,planned,candidates,accept_clear,allow_natural,clear_extension)
 clear_extension=clear_extension or 0
 local origin=source.position;local old=target(origin,"transport-belt");local declared=false;local drill=false
 if source.facing~=4 or source.item~=source.mining_resource then return {ok=false,reason="invalid_registered_mining_exit_source"} end
 for _,spec in ipairs(planned) do
  if spec.name=="transport-belt" and spec.position.x==origin.x and spec.position.y==origin.y
   and spec.direction==4 and spec.mining_resource==source.mining_resource then declared=true end
  if (spec.name=="electric-mining-drill" or spec.name=="burner-mining-drill") and spec.mining_resource==source.mining_resource
   and math.abs(spec.position.x-origin.x)<=3 and math.abs(spec.position.y-origin.y)<=3 then drill=true end
 end
 if old and (old.force~=f or ignored[old.unit_number] or old.direction~=4) then
  return {ok=false,reason="mining_exit_source_identity_or_direction_changed"}
 end
 if not old and not declared then return {ok=false,reason="mining_exit_requires_owned_source"} end
 if not drill then for _,e in pairs(s.find_entities_filtered{area={{origin.x-3,origin.y-3},{origin.x+3,origin.y+3}},force=f,type="mining-drill"}) do
  if not ignored[e.unit_number] and math.abs(e.position.x-origin.x)<=3 and math.abs(e.position.y-origin.y)<=3 then drill=true;break end
 end end
 if not drill then return {ok=false,reason="mining_exit_without_nearby_source_drill"} end
 if old then for lane=1,2 do for _,item in pairs(old.get_transport_line(lane).get_contents()) do
  if item.name~=source.item then return {ok=false,reason="mining_exit_existing_belt_contaminated"} end
 end end end
 local function placeable(p,direction,step)
  for _,spec in ipairs(planned) do
   local same=step==0 and spec.name=="transport-belt" and spec.position.x==p.x and spec.position.y==p.y
   local size=spec.name=="electric-mining-drill" and 3 or spec.name=="burner-mining-drill" and 2 or 1
   if not same and math.abs(spec.position.x-p.x)<size/2+.49 and math.abs(spec.position.y-p.y)<size/2+.49 then return false end
  end
  local removable=false
  for _,e in pairs(s.find_entities_filtered{area={{p.x-.4,p.y-.4},{p.x+.4,p.y+.4}}}) do
   if e~=a and e.type~="resource" and not (step==0 and e==old) and not e.prototype.collision_mask.colliding_with_tiles_only then
    local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
    if (ignored[e.unit_number] and e.force==f) or (allow_natural and e.force.name=="neutral" and e.minable and (e.type=="tree" or rock)) then
     removable=true
    else return false end
   end
  end
  if step==0 and (old or declared) then return true end
  if s.can_place_entity{name="transport-belt",position=p,direction=direction,force=f,
   build_check_type=defines.build_check_type.manual_ghost,forced=true} then return true end
  if not removable then return false end
  for layer in pairs(prototypes.entity["transport-belt"].collision_mask.layers) do
   if s.get_tile(p).collides_with(layer) then return false end
  end
  return true
 end
 for _,candidate in ipairs(candidates) do local path={};local prefix=nil;local first_clear=nil;local clear_direction=nil;local connector={}
  for index,p in ipairs(candidate) do
   local occupied,mismatch=mining_patch_at(p,source.mining_resource,zones)
   if mismatch then break end
   if first_clear and (occupied or index-first_clear>clear_extension) then break end
   if not first_clear and index>13 then break end
   local direction=4;local next_p=candidate[index+1]
   if next_p then direction=next_p.y<p.y and 0 or next_p.y>p.y and 8 or 4 end
   if not placeable(p,direction,index-1) then break end
   path[#path+1]=p
   if not occupied then
    if not first_clear then
     first_clear=index;prefix={};for _,cell in ipairs(path) do prefix[#prefix+1]=cell end
     local previous=candidate[index-1]
     clear_direction=previous and (p.y<previous.y and 0 or p.y>previous.y and 8 or 4) or 4
    else connector[#connector+1]=p end
    if not accept_clear or accept_clear(p) then return {ok=true,exit_prefix=prefix,exit_length=first_clear-2,
     clear_position=prefix[#prefix],hub_connector=connector,hub_position=p} end
    if index-first_clear>=clear_extension or direction~=clear_direction then break end
   end
   if not first_clear and index==13 then break end
  end
 end
 return {ok=false,reason="no_safe_bounded_mining_exit"}
end
'''


RESOURCE_CHECK_LUA = MINING_EXIT_LUA + r'''
local function resource_violation(spec,reservations,ignored,specs)
 local ores=s.find_entities_filtered{area=spec.survey_area,type="resource"};local zones={}
 for _,zone in ipairs(reservations) do local box=zone.area;local q=spec.survey_area
  if q[1][1]<box[2][1] and q[2][1]>box[1][1] and q[1][2]<box[2][2] and q[2][2]>box[1][2] then zones[#zones+1]=zone end
 end
 if #ores==0 and #zones==0 then return nil,nil end
 if spec.resource_virtual_tunnel then
  if spec.resource_tunnel_span>prototypes.entity[spec.name].max_underground_distance then return nil,nil end
  local pair=spec.resource_tunnel_anchors
  if pair then
   local a_spec,b_spec=pair[1],pair[2];local first=target(a_spec.position,a_spec.name);local last=target(b_spec.position,b_spec.name)
   if first and last and first.force==f and last.force==f and first.surface==s and last.surface==s
    and first.unit_number==a_spec.unit_number and last.unit_number==b_spec.unit_number
    and first.position.x==a_spec.position.x and first.position.y==a_spec.position.y
    and last.position.x==b_spec.position.x and last.position.y==b_spec.position.y
    and not ignored[first.unit_number] and not ignored[last.unit_number]
    and first.direction==a_spec.direction and last.direction==b_spec.direction
    and first.belt_to_ground_type=="input" and last.belt_to_ground_type=="output"
    and first.underground_belt_neighbour==last and last.underground_belt_neighbour==first then return nil,{anchors=pair} end
  end
  return "new_underground_tunnel_over_resource_or_mining_reservation",nil
 end
 local old=target(spec.position,spec.name)
 if old and old.force==f and old.surface==s and old.unit_number==spec.resource_preserved_unit
  and not ignored[old.unit_number] and old.position.x==spec.position.x and old.position.y==spec.position.y
  and old.direction==(spec.direction or 0) and (not spec.belt_to_ground_type or old.belt_to_ground_type==spec.belt_to_ground_type) then
  local good=true
  if spec.recipe then local ok,r=pcall(function()return old.get_recipe()end);good=ok and r and r.name==spec.recipe end
  if spec.splitter_input_priority and old.splitter_input_priority~=spec.splitter_input_priority then good=false end
  if spec.splitter_output_priority and old.splitter_output_priority~=spec.splitter_output_priority then good=false end
  if good then return nil,{name=spec.name,position=spec.position,unit_number=old.unit_number,direction=old.direction,belt_to_ground_type=spec.belt_to_ground_type,
   recipe=spec.recipe,splitter_input_priority=spec.splitter_input_priority,splitter_output_priority=spec.splitter_output_priority} end
 end
 if not spec.mining_resource then return "new_entity_over_resource_or_mining_reservation",nil end
 if spec.mining_exit then
  local p=spec.mining_exit.source_position;local source_drill=false
  if spec.mining_exit.path then
   for index,cell in ipairs(spec.mining_exit.path) do
    local occupied,mismatch=mining_patch_at(cell,spec.mining_resource,reservations)
    if mismatch then return "mining_exit_foreign_resource_or_reservation",nil end
    if occupied~=(index<#spec.mining_exit.path) then return "mining_exit_must_end_at_first_clear_tile",nil end
   end
  end
  for _,drill in ipairs(specs) do
   if (drill.name=="electric-mining-drill" or drill.name=="burner-mining-drill") and drill.mining_resource==spec.mining_resource
    and math.abs(drill.position.x-p.x)<=3 and math.abs(drill.position.y-p.y)<=3 then source_drill=true;break end
  end
  if not source_drill then
   for _,drill in pairs(s.find_entities_filtered{area={{p.x-3,p.y-3},{p.x+3,p.y+3}},force=f,type="mining-drill"}) do
    if math.abs(drill.position.x-p.x)<=3 and math.abs(drill.position.y-p.y)<=3 then source_drill=true;break end
   end
  end
  if not source_drill then return "mining_exit_without_nearby_source_drill",nil end
 elseif spec.name~="electric-mining-drill" and spec.name~="burner-mining-drill" then
  local local_drill=false
  for _,drill in ipairs(specs) do
   if (drill.name=="electric-mining-drill" or drill.name=="burner-mining-drill") and drill.mining_resource==spec.mining_resource
    and math.abs(drill.position.x-spec.position.x)<=3 and math.abs(drill.position.y-spec.position.y)<=3 then local_drill=true;break end
  end
  if not local_drill then
   for _,drill in pairs(s.find_entities_filtered{area=spec.mining_footprint,force=f,type="mining-drill"}) do
    if math.abs(drill.position.x-spec.position.x)<=3 and math.abs(drill.position.y-spec.position.y)<=3 then local_drill=true;break end
   end
  end
  if not local_drill then return "mining_local_asset_without_nearby_drill",nil end
 end
 for _,ore in pairs(ores) do if ore.name~=spec.mining_resource then return "mining_core_resource_mismatch",nil end end
 for _,zone in ipairs(zones) do if zone.resource~=spec.mining_resource then return "mining_reservation_resource_mismatch",nil end end
 return nil,nil
end
'''


def resource_preflight(game, builder, obs, entities, *, ignored_units=()):
    try:
        specs = policy_specs(builder, obs, entities, ignored_units=ignored_units)
        zones = mining_reservations(game, obs['world_id'])
    except (ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'reason': 'invalid_mining_resource_scope', 'detail': str(exc)}
    if not specs: return {'ok': True, 'resource_grandfathered': []}
    payload = json.dumps(json.dumps({'specs': specs, 'zones': zones, 'ignored': list(ignored_units)}, separators=(',', ':')))
    result = game.query('local args=helpers.json_to_table(' + payload + ');' + RESOURCE_CHECK_LUA + '''
local ignored={};for _,unit in ipairs(args.ignored) do ignored[unit]=true end
local preserved={}
for _,spec in ipairs(args.specs) do
 local reason,old=resource_violation(spec,args.zones,ignored,args.specs)
 if reason then return {ok=false,reason=reason,entity=spec.name,position=spec.position} end
 if old then
  if old.anchors then for _,anchor in ipairs(old.anchors) do preserved[#preserved+1]=anchor end
  else preserved[#preserved+1]=old end
 end
end
return {ok=true,resource_grandfathered=preserved}
''')
    if result.get('resource_grandfathered') == {}: result = dict(result, resource_grandfathered=[])
    if isinstance(result.get('resource_grandfathered'),list):
        result=dict(result,resource_grandfathered=list({p['unit_number']:p for p in result['resource_grandfathered']}.values()))
    return result


def ensure_resource_policy(game, builder, obs, plan):
    """Cache by world/plan/reservations; a lost grandfathered unit invalidates it."""
    try: zones = mining_reservations(game, obs['world_id'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'ok':False,'reason':'resource_reservation_state_unavailable','detail':str(exc)}
    key = hashlib.sha256(json.dumps([obs['world_id'], plan.get('entities', []), zones], sort_keys=True).encode()).hexdigest()
    cache = getattr(builder, '_resource_policy_cache', {})
    actual = {(e['name'], *_position(e)): e for e in obs.get('entities', [])}
    entry = cache.get(key)
    if entry and obs.get('tick', 0) >= entry['tick']:
        if all(actual.get((p['name'], *_position(p)), {}).get('unit_number') == p['unit_number']
               and actual[(p['name'], *_position(p))].get('direction', 0) == p['direction']
               and all(actual[(p['name'], *_position(p))].get(field)==p.get(field) for field in
                       ('belt_to_ground_type','recipe','splitter_input_priority','splitter_output_priority') if field in p)
               for p in entry['result'].get('resource_grandfathered', [])):
            return entry['result']
    result = resource_preflight(game, builder, obs, plan.get('entities', []))
    if result.get('ok'):
        if len(cache) >= 32: cache.clear()
        cache[key] = {'tick': obs.get('tick', 0), 'result': result}
        builder._resource_policy_cache = cache
    return result
