"""Finite paid bastion advances; only native gunfire may kill enemy targets."""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .defense_coverage import coverage_cells
from .defense_hardening import wall_bastion,mutual_coverage,_cluster_area,_box,SERVICE_ACCESS_LUA
from .defense_perimeter import protected_factory_areas
from .nearby_threats import NearbyThreats
from .planning import overlap,preflight
from .resource_policy import mining_reservations
from factorio_agent.core.world_catalog import _yield

MAX_STAGES=3
MAX_LOSSES=2
LAUNCH_TICKS=10800
OPERATION_GUNS=MAX_STAGES*3+MAX_LOSSES
OPERATION_MAGAZINES=MAX_STAGES*3*20+20
OPERATION_WALLS=MAX_STAGES*3*8

def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])

def deployment_stage(state):
    """Trusted known-file adapter used by the owner-whitelisted atomic builder."""
    index=state.get('active_stage')
    if(type(index)is not int or not 0<=index<len(state.get('stages',[]))or index>=MAX_STAGES
            or state.get('phase')not in('register','deploy','fight','stock','verified')):
        raise ValueError('one active bounded nest assault stage required')
    stage=deepcopy(state['stages'][index])
    stage.update(world_id=state['world_id'],catalog_fingerprint=state['catalog_fingerprint'],operation_id=state['id'])
    return stage

def stage_candidates(report,*,maximum=96,remaining_stages=MAX_STAGES):
    """Bounded forward cells covered by at least two native armed rear guns."""
    targets=[q for q in report['targets']if q.get('dangerously_close')and q.get('role')in('worm','spawner')]
    if not targets:return []
    target=min(targets,key=lambda q:(q['factory_distance'],q['unit_number']));p=target['position'];radius=report['gun']['range']-2
    rear=[]
    for q in report['friendly_guns']:
        tiers=q.get('ammo',{}).get('by_item',{})
        tier='piercing-rounds-magazine'if tiers.get('piercing-rounds-magazine',{}).get('full_normal_magazines',0)>=5 else'firearm-magazine'
        stock=tiers.get(tier,{}).get('full_normal_magazines',q.get('ammo',{}).get('full_normal_magazines',0)if tier=='firearm-magazine'else 0)
        if q.get('health',0)>=q.get('max_health',1)*.5 and stock>=5:rear.append(dict(q,ammo_item=tier))
    rear=sorted(rear,key=lambda q:(math.dist((p['x'],p['y']),(q['position']['x'],q['position']['y'])),q['unit_number']))[:6]
    if len(rear)<2:return []
    cx=sum(q['position']['x']for q in rear[:3])/min(3,len(rear));cy=sum(q['position']['y']for q in rear[:3])/min(3,len(rear))
    length=math.hypot(p['x']-cx,p['y']-cy)
    if length<=0:return []
    ux,uy=(p['x']-cx)/length,(p['y']-cy)/length;result=[]
    for x in range(math.floor(cx-radius),math.ceil(cx+radius)+1):
        for y in range(math.floor(cy-radius),math.ceil(cy+radius)+1):
            support=[q for q in rear if math.dist((x,y),(q['position']['x'],q['position']['y']))<=radius]
            advance=(x-cx)*ux+(y-cy)*uy
            if(len(support)>=2 and advance>=4 and math.dist((x,y),(p['x'],p['y']))<=report['gun']['range']+radius*(remaining_stages-1)):
                result.append({'position':{'x':x,'y':y},'advance':advance,'support':support})
    result.sort(key=lambda q:(-q['advance'],math.dist((q['position']['x'],q['position']['y']),(p['x'],p['y'])),q['position']['x'],q['position']['y']))
    # A dense prefix can contain no room for three complete wall cages. Cover
    # the bounded frontier at two-tile spacing before filling its closest gaps.
    selected=[];keys=set()
    for row in result:
        px,py=row['position'].values()
        if all(max(abs(px-q['position']['x']),abs(py-q['position']['y']))>=2 for q in selected):
            selected.append(row);keys.add((px,py))
            if len(selected)==maximum:return selected
    for row in result:
        if tuple(row['position'].values())not in keys:selected.append(row)
        if len(selected)==maximum:break
    return selected

ASSAULT_DEPLOYMENT_GUARD_LUA=r'''
local policy=x.assault_policy
local op=policy and d.nest_assaults and d.nest_assaults[policy.operation_id]
if not op or policy.mode~="controlled_projectile_assault_v1"or op.world_id~=x.expected_world_id
 or op.catalog_fingerprint~=x.catalog_fingerprint or op.actor_unit~=a.unit_number
 or op.target_set_hash~=policy.target_set_hash or policy.max_post_losses~=2
 or policy.deadline_tick~=op.deadline_tick or policy.started_tick~=op.started_tick
 or game.tick>op.deadline_tick or op.loss_count>=2 then return reject("assault_launch_budget_or_native_authority_changed")end
if type(_G.__factorio_nest_death_handler)~="function"or script.get_event_handler(defines.events.on_entity_died)~=_G.__factorio_nest_death_handler then
 return reject("assault_current_native_death_listener_required")end
if f.get_ammo_damage_modifier("bullet")<policy.force_profile.ammo_damage
 or f.get_turret_attack_modifier("gun-turret")<policy.force_profile.turret_damage
 or f.get_gun_speed_modifier("bullet")<policy.force_profile.gun_speed then return reject("assault_force_profile_weakened")end
local covered=0;local seen_back={}
for _,q in ipairs(policy.backstop_specs)do local back=target(q.position,"gun-turret")
 if seen_back[q.unit_number]then return reject("assault_duplicate_rear_witness")end;seen_back[q.unit_number]=true
 if back and back.force==f and back.surface==s and back.unit_number==q.unit_number
 and back.position.x==q.position.x and back.position.y==q.position.y
 and back.health>=back.max_health*.5 and(back.position.x-p.x)^2+(back.position.y-p.y)^2<=(gun.attack_parameters.range-2)^2 then
  local count=0;local inv=back.get_inventory(defines.inventory.turret_ammo)
  local back_item=q.ammo_item or "firearm-magazine";local back_size=prototypes.item[back_item].magazine_size
  if inv then for i=1,#inv do local st=inv[i]
   if st.valid_for_read and st.name==back_item and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
    and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data then
    count=count+st.count-(st.ammo<back_size and 1 or 0)
   end
  end end
  if count>=5 then covered=covered+1 end
 end
end
if covered<2 then return reject("assault_requires_two_current_armed_rear_posts")end
local reviewed={}
for _,q in ipairs(policy.targets)do local foe=target(q.position,q.name)
 reviewed[q.unit_number]=q
 if foe then
  if foe.unit_number~=q.unit_number or foe.force.index~=q.force_index or foe.surface.index~=q.surface_index
   or foe.position.x~=q.position.x or foe.position.y~=q.position.y then return reject("assault_enemy_witness_changed")end
  local ap=foe.prototype.attack_parameters
  if foe.type=="turret"and(not ap or not ap.range or ap.range~=q.reviewed_range or ap.range>policy.max_enemy_range)then return reject("assault_enemy_range_exceeds_reviewed_profile")end
 elseif not op.deaths[tostring(q.unit_number)]then return reject("assault_target_absent_without_native_death")end
end
local scan_range=policy.max_enemy_range
for _,proto in pairs(prototypes.entity)do if proto.type=="turret"then local ap=proto.attack_parameters
 if not ap or not ap.range or ap.range>94 then return reject("assault_native_enemy_range_scan_unsupported")end
 scan_range=math.max(scan_range,ap.range)
end end
for _,foe in pairs(s.find_entities_filtered{position=p,radius=scan_range+2,force="enemy",type="turret"})do
 local ap=foe.prototype.attack_parameters
 if not ap or not ap.range then return reject("assault_unreviewed_enemy_turret_profile")end
 if(foe.position.x-p.x)^2+(foe.position.y-p.y)^2<=(ap.range+2)^2 then
  local q=reviewed[foe.unit_number]
  if not q or foe.name~=q.name or foe.position.x~=q.position.x or foe.position.y~=q.position.y
   or foe.force.index~=q.force_index or foe.surface.index~=q.surface_index or ap.range~=q.reviewed_range then
   return reject("assault_new_or_changed_covering_worm")end
 end
end
'''

ASSAULT_CONFIGURE_POST_LUA=r'''
op.posts[tostring(e.unit_number)]={name=x.post_name,position=e.position,stage=x.assault_policy.stage_index,unit_number=e.unit_number}
local priorities={"small-worm-turret","medium-worm-turret","big-worm-turret","behemoth-worm-turret",
 "biter-spawner","spitter-spawner","medium-biter","medium-spitter","small-biter","small-spitter"}
for index,name in ipairs(priorities)do local ok=pcall(function()e.set_priority_target(index,name)end)
 if not ok then return reject("assault_paid_post_priority_configuration_failed")end
end
e.ignore_unprioritised_targets=false
'''

def _start_context(game,action):
    required={'type','operation_id','target_set_hash','expected_world_id','expected_actor_unit'}
    if(not isinstance(action,dict)or not required<=set(action)or set(action)-required-{'reason'}or action['type']!='start_nest_assault'
            or type(action['expected_actor_unit'])is not int or action['expected_actor_unit']<1
            or any(not isinstance(action[k],str)or not action[k]for k in('operation_id','target_set_hash','expected_world_id'))):
        raise ValueError('exact typed nest assault initialization required')
    if getattr(game,'expected_world',None)!=action['expected_world_id']or getattr(game,'backend',None)!='assisted':
        raise ValueError('nest assault requires bound ordinary assisted execution')
    state=json.loads((Path(game.cfg.runtime_dir)/'nest-assault.json').read_text(encoding='utf-8'))
    if(state.get('id')!=action['operation_id']or state.get('target_set_hash')!=action['target_set_hash']
            or state.get('world_id')!=action['expected_world_id']or state.get('actor_unit')!=action['expected_actor_unit']
            or not 1<=len(state.get('targets',[]))<=12 or _hash(state['targets'])!=state['target_set_hash']):raise ValueError('saved nest assault authority changed')
    stage=deployment_stage(state)
    return {**action,'targets':state['targets'],'backstop_specs':stage['backstop_specs'],'catalog_fingerprint':state['catalog_fingerprint']}

def execute_start_assault(game,action):
    args=_start_context(game,action)
    try:result=game.query('local x=helpers.json_to_table('+_payload(args)+');'+START_ASSAULT_LUA)
    except Exception as exc:return {'ok':False,'status':'blocked','reason':'nest_assault_initialization_outcome_unknown','detail':str(exc)[:300]}
    known_rejections={'assault_start_world_or_actor_changed','assault_actor_must_remain_at_safe_rear',
        'assault_start_existing_journal_changed','assault_initial_enemy_identity_changed',
        'assault_existing_death_handler_requires_safe_integration'}
    if(not isinstance(result,dict)or result.get('ok')not in(True,False)
            or result.get('ok')is False and(result.get('status')!='blocked'or result.get('reason')not in known_rejections)
            or result.get('ok')and(type(result.get('started_tick'))is not int
                or type(result.get('deadline_tick'))is not int or result['deadline_tick']!=result['started_tick']+LAUNCH_TICKS)):
        return {'ok':False,'status':'blocked','reason':'nest_assault_initialization_outcome_unknown'}
    return result

START_ASSAULT_LUA=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if not d or d.world_id~=x.expected_world_id or not a or not a.valid or a.unit_number~=x.expected_actor_unit
 or a.force~=f or a.surface~=s or s.name~="nauvis"then return reject("assault_start_world_or_actor_changed")end
for _,q in ipairs(x.targets)do
 if(a.position.x-q.position.x)^2+(a.position.y-q.position.y)^2<48^2 then return reject("assault_actor_must_remain_at_safe_rear")end
end
local op=d.nest_assaults and d.nest_assaults[x.operation_id];local targets={}
if op then
 if op.world_id~=x.expected_world_id or op.catalog_fingerprint~=x.catalog_fingerprint or op.actor_unit~=a.unit_number
  or op.target_set_hash~=x.target_set_hash then return reject("assault_start_existing_journal_changed")end
else
 for _,q in ipairs(x.targets)do local foe=target(q.position,q.name)
  if targets[tostring(q.unit_number)]or not foe or foe.unit_number~=q.unit_number or foe.force.index~=q.force_index
   or foe.surface.index~=q.surface_index or foe.position.x~=q.position.x or foe.position.y~=q.position.y then
   return reject("assault_initial_enemy_identity_changed")end
  targets[tostring(q.unit_number)]=q
 end
end
local existing=script.get_event_handler(defines.events.on_entity_died)
if existing and existing~=_G.__factorio_nest_death_handler then return reject("assault_existing_death_handler_requires_safe_integration")end
if not existing then
 local function handler(event)
  local root=storage.deterministic_player
  if not root or not root.nest_assaults then return end
  local dead=event.entity;if not dead or not dead.valid or not dead.unit_number then return end
  for _,op in pairs(root.nest_assaults)do
   if op.world_id==root.world_id and dead.surface.index==op.surface_index then
    local key=tostring(dead.unit_number);local expected=op.targets[key]
    if expected and dead.name==expected.name and dead.force.index==expected.force_index
     and dead.position.x==expected.position.x and dead.position.y==expected.position.y then
     local cause=event.cause;local own=cause and cause.valid and(op.posts[tostring(cause.unit_number)]or op.support[tostring(cause.unit_number)])
     op.deaths[key]={unit_number=dead.unit_number,name=dead.name,position=dead.position,tick=event.tick,
      cause_unit=cause and cause.valid and cause.unit_number,cause_name=cause and cause.valid and cause.name,
      own_gun_projectile=not not(own and cause.force.index==op.player_force_index and cause.type=="ammo-turret"and event.damage_type and event.damage_type.name=="physical")}
    end
    if op.posts[key]and not op.lost_posts[key]then op.lost_posts[key]=event.tick;op.loss_count=op.loss_count+1 end
   end
  end
 end
 _G.__factorio_nest_death_handler=handler;script.on_event(defines.events.on_entity_died,handler)
end
d.nest_assaults=d.nest_assaults or{}
if op then
 return {ok=true,status="succeeded",started_tick=op.started_tick,deadline_tick=op.deadline_tick,reused=true}
end
local support={};for _,q in ipairs(x.backstop_specs or{})do support[tostring(q.unit_number)]=q end
op={world_id=x.expected_world_id,catalog_fingerprint=x.catalog_fingerprint,actor_unit=a.unit_number,surface_index=s.index,player_force_index=f.index,support=support,
 target_set_hash=x.target_set_hash,targets=targets,posts={},deaths={},lost_posts={},loss_count=0,started_tick=game.tick,deadline_tick=game.tick+10800}
d.nest_assaults[x.operation_id]=op
return {ok=true,status="succeeded",started_tick=op.started_tick,deadline_tick=op.deadline_tick}
'''


CANDIDATE_SURVEY_LUA=r'''
if not d or d.world_id~=x.world_id or s.name~="nauvis"then return {ok=false,reason="assault_candidate_world_changed"}end
local listener=script.get_event_handler(defines.events.on_entity_died)
if listener and listener~=_G.__factorio_nest_death_handler then return {ok=false,reason="assault_existing_death_handler_requires_safe_integration"}end
local rows={}
for i,q in ipairs(x.candidates)do local legal=true
 for _,spec in ipairs(q.entities)do local h=spec.name=="gun-turret"and 1 or .5;local p=spec.position
  local area={{p.x-h,p.y-h},{p.x+h,p.y+h}}
  for cx=math.floor(area[1][1]/32),math.floor((area[2][1]-.0001)/32)do
   for cy=math.floor(area[1][2]/32),math.floor((area[2][2]-.0001)/32)do if not f.is_chunk_charted(s,{x=cx,y=cy})then legal=false end end
  end
  if legal and(s.count_entities_filtered{area=area,type="resource"}>0 or not s.can_place_entity{
   name=spec.name,position=p,direction=0,force=f,build_check_type=defines.build_check_type.manual_ghost,forced=true})then legal=false end
  if legal then for _,e in pairs(s.find_entities_filtered{area=area})do
   local rock=e.type=="simple-entity"and(e.name=="big-rock"or e.name=="huge-rock"or e.name=="big-sand-rock")
   local nature=e.force.name=="neutral"and e.minable and(e.type=="tree"or rock)
   if e~=a and e.type~="corpse"and not nature and not e.prototype.collision_mask.colliding_with_tiles_only then legal=false;break end
  end end
  if not legal then break end
 end
 if legal then rows[#rows+1]=i end
end
return {ok=true,tick=game.tick,legal=rows}
'''


INSPECT_ASSAULT_LUA=r'''
if not d or d.world_id~=x.world_id or not a or not a.valid or a.unit_number~=x.actor_unit
 or s.name~="nauvis"then return {ok=false,reason="assault_inspection_frame_changed"}end
local op=d.nest_assaults and d.nest_assaults[x.operation_id]
local size=prototypes.item[x.ammo_item].magazine_size
local function ammo(inv)local full,rounds=0,0
 if inv then for i=1,#inv do local t=inv[i]
  if t.valid_for_read and t.name==x.ammo_item and t.quality.name=="normal"and t.health==1 and t.spoil_tick==0
   and not t.is_item_with_tags and not t.is_item_with_inventory and not t.is_item_with_entity_data then
   full=full+t.count-(t.ammo<size and 1 or 0);rounds=rounds+(t.count-1)*size+t.ammo
  end
 end end;return {full_normal_magazines=full,normal_rounds=rounds}end
local listener=script.get_event_handler(defines.events.on_entity_died)
local owned=type(_G.__factorio_nest_death_handler)=="function"and listener==_G.__factorio_nest_death_handler
local out={ok=true,tick=game.tick,world_id=d.world_id,actor_unit=a.unit_number,actor_position=a.position,
 main_ammo=ammo(a.get_main_inventory()),native_started=not not op,posts={},targets={},loss_count=op and op.loss_count or 0,
 listener_owned=owned,listener_state=owned and"owned"or(listener and"foreign"or"missing")}
if op then
 if op.world_id~=x.world_id or op.catalog_fingerprint~=x.catalog_fingerprint or op.actor_unit~=x.actor_unit
  or op.target_set_hash~=x.target_set_hash then return {ok=false,reason="assault_native_journal_identity_changed"}end
 out.started_tick=op.started_tick;out.deadline_tick=op.deadline_tick;out.deaths=op.deaths;out.lost_posts=op.lost_posts
end
for _,q in ipairs(x.targets)do
 if(a.position.x-q.position.x)^2+(a.position.y-q.position.y)^2<48^2 then return {ok=false,reason="assault_actor_must_remain_at_safe_rear"}end
 local e=target(q.position,q.name);local row={unit_number=q.unit_number,name=q.name,position=q.position,present=false}
 if e then
  if e.unit_number~=q.unit_number or e.force.index~=q.force_index or e.surface.index~=q.surface_index
   or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="assault_target_identity_changed"}end
  row.present=true;row.health=e.health;row.max_health=e.max_health
 end
 out.targets[#out.targets+1]=row
end
for _,q in ipairs(x.posts)do
 local e=target(q.position,"gun-turret");local row={name=q.name,position=q.position,present=false}
 local marker=q.deployment_id and d.defense_deployments and d.defense_deployments[q.deployment_id]
 if marker then row.marker_phase=marker.phase;row.marker_unit=marker.unit_number
  row.paid=marker.owner_kind=="nest_assault"and marker.post_name==q.name and marker.world_id==x.world_id
   and marker.catalog_fingerprint==x.catalog_fingerprint and marker.ammo_item==x.ammo_item
   and marker.actor_unit==x.actor_unit and marker.phase=="complete"and marker.receipt and marker.receipt.paid_turrets==1
   and marker.receipt.loaded_magazines==20 and marker.receipt.loaded_rounds==200
 end
 if e then
  if e.force~=f or e.surface~=s or e.position.x~=q.position.x or e.position.y~=q.position.y
   or not op or not op.posts[tostring(e.unit_number)]or not row.paid or marker.unit_number~=e.unit_number then
   return {ok=false,reason="assault_post_requires_exact_paid_native_marker"}end
  row.present=true;row.unit_number=e.unit_number;row.health=e.health;row.max_health=e.max_health;row.ammo=ammo(e.get_inventory(defines.inventory.turret_ammo))
  row.ignore_unprioritised_targets=e.ignore_unprioritised_targets;row.priorities={}
  for index=1,6 do local value=e.get_priority_target(index);row.priorities[index]=value and value.name end
 end
 out.posts[#out.posts+1]=row
end
return out
'''


def choose_bastions(candidates,protected,radius,*,maximum_groups=4096):
    """Finite line/triangle groups, followed by bounded combinations; no map coordinates."""
    bypos={(q['position']['x'],q['position']['y']):q for q in candidates}
    groups=[];seen=set()
    for q in candidates:
        x,y=q['position'].values()
        for offsets in(((0,-4),(0,0),(0,4)),((-4,0),(0,0),(4,0)),((-2,-4),(0,0),(-2,4)),((2,-4),(0,0),(2,4))):
            keys=tuple(sorted((x+dx,y+dy)for dx,dy in offsets))
            if keys not in seen and all(k in bypos for k in keys):seen.add(keys);groups.append(tuple(bypos[k]for k in keys))
    for group in combinations(candidates,3):
        if len(groups)>=maximum_groups:break
        keys=tuple(sorted((q['position']['x'],q['position']['y'])for q in group))
        if keys not in seen:seen.add(keys);groups.append(group)
    for group in groups:
        bs=[wall_bastion(q['position'],q.get('facing',4))for q in group]
        hotspot={k:sum(q['position'][k]for q in group)/3 for k in('x','y')}
        if mutual_coverage(bs,hotspot,radius)and not any(overlap(_cluster_area(bs),r)for r in protected):
            yield bs,hotspot


class NestAssault:
    """Resumable finite normal-projectile trial, with no actor frontline movement."""
    def __init__(self,game,bootstrap,builder,catalog,perimeter):
        self.game,self.bootstrap,self.builder,self.catalog,self.perimeter=game,bootstrap,builder,catalog,perimeter
        self.path=Path(game.cfg.runtime_dir)/'nest-assault.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.threats=NearbyThreats(game,catalog)

    def _identity(self,obs):return(obs.get('ok',True)and self.state.get('world_id')==obs.get('world_id')
        and self.state.get('catalog_fingerprint')==self.catalog.fingerprint
        and self.state.get('actor_unit')==obs.get('actor_unit_number'))
    def _save(self):_atomic_json(self.path,self.state)

    def _stage(self,obs,report,targets,operation,index):
        protected=protected_factory_areas(self.game,obs['world_id'],self.catalog.fingerprint)
        protected += [q['area']for q in mining_reservations(self.game,obs['world_id'])]
        scoped=dict(report,targets=[dict(q,dangerously_close=True)for q in report['targets']if q['unit_number']in {t['unit_number']for t in targets}])
        choices=stage_candidates(scoped,remaining_stages=MAX_STAGES-index);target=min(scoped['targets'],
            key=lambda q:(q['factory_distance'],q['unit_number']),default=None)
        if not target:return {'ok':False,'reason':'assault_no_remaining_original_stationary_target'}
        raw=[]
        for q in choices:
            p=q['position'];dx=target['position']['x']-p['x'];dy=target['position']['y']-p['y']
            facing=(4 if dx>=0 else 12)if abs(dx)>=abs(dy)else(8 if dy>=0 else 0)
            b=wall_bastion(p,facing)
            if not any(overlap(_box(e),r)for e in b['entities']for r in protected):raw.append(dict(q,facing=facing,entities=b['entities']))
        if not raw:return {'ok':False,'reason':'assault_no_two_rear_covered_unreserved_candidate','candidate_count':len(choices),
            'primary_target':{k:target[k]for k in('name','unit_number','position','factory_distance')},
            'rear_status':[{k:deepcopy(q[k])for k in('name','unit_number','position','health','max_health','ammo')}for q in sorted(
                report['friendly_guns'],key=lambda q:math.dist(tuple(q['position'].values()),tuple(target['position'].values())))[:6]]}
        surveyed=self.game.query('local x=helpers.json_to_table('+_payload({'world_id':obs['world_id'],'candidates':raw})+');'+CANDIDATE_SURVEY_LUA)
        if not surveyed.get('ok'):return surveyed
        legal=[raw[i-1]for i in surveyed.get('legal',[])if type(i)is int and 1<=i<=len(raw)]
        recent=[];whole_attempts=0
        for bs,hotspot in choose_bastions(legal,protected,report['gun']['range']-2):
            if whole_attempts>=4:break
            whole_attempts+=1
            es=[e for b in bs for e in b['entities']];whole=preflight(self.game,self.builder,obs,es)
            if not whole.get('ok'):recent.append(whole.get('reason'));continue
            scope_id='assault:'+operation[:16]+':'+str(index);posts=[dict(name=scope_id+':'+str(i),position=b['position'],
                coverage_scope=scope_id,local_walk_access=True,commissioned=False)for i,b in enumerate(bs)]
            area=[[hotspot['x']-1,hotspot['y']-1],[hotspot['x']+1,hotspot['y']+1]];names=[p['name']for p in posts]
            support={q['unit_number']:q for row in raw if row['position']in[b['position']for b in bs]for q in row['support']}
            bounds=[[math.floor(min([b['position'][k]for b in bs]+[q['position'][k]for q in support.values()]))-4 for k in('x','y')],
                    [math.ceil(max([b['position'][k]for b in bs]+[q['position'][k]for q in support.values()]))+4 for k in('x','y')]]
            if math.prod(bounds[1][i]-bounds[0][i]+1 for i in(0,1))>4096:return {'ok':False,'reason':'assault_local_service_budget_exceeded'}
            stage=dict(phase='deploy',scope_id=scope_id,block='defense_'+scope_id,hotspot=hotspot,bastions=bs,posts=posts,
                scope=dict(areas=[area],cells=coverage_cells([area]),effective_range=report['gun']['range']-2,native_range=report['gun']['range'],
                    covering_posts=names,new_posts=names,commissioned=False,access_scope='wall-aware ordinary rear service'),
                plan=_plan(es,[],reserved_area=_cluster_area(bs),clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],
                    layout_policy='finite-paid-projectile-bastion-v1'),bindings={},deployment_ids={},reserved=protected,bounds=bounds,
                backstop_specs=[{k:deepcopy(q[k])for k in('name','position','unit_number','ammo_item')}for q in support.values()])
            # The service component starts at an actual rear gun, rather than an inaccessible enemy-facing point.
            access={'ok':False,'reason':'prospective_bastion_rear_service_disconnected'}
            for back in sorted(support.values(),key=lambda q:math.dist(tuple(q['position'].values()),tuple(hotspot.values())))[:2]:
                stage['service_origin']=deepcopy(back['position']);access=self._access(stage)
                if access.get('ok'):break
            if not access.get('ok'):recent.append(access.get('reason'));continue
            stage['access_preview']=access;return {'ok':True,'stage':stage,'candidates':len(choices),'legal_candidates':len(legal)}
        return {'ok':False,'reason':'assault_no_three_gun_resource_free_serviced_stage','candidate_count':len(choices),
            'legal_candidates':len(legal),'legal_positions':[q['position']for q in legal[:24]],
            'whole_group_attempts':whole_attempts,'recent_failures':recent[-4:]}

    def _access(self,stage):
        args={k:stage[k]for k in('bounds','reserved','bastions')};args.update(hotspot=stage['service_origin'],entities=stage['plan']['entities'])
        return self.game.query('local x=helpers.json_to_table('+_payload(args)+');'+SERVICE_ACCESS_LUA)

    def prepare(self,obs):
        if self.state:return deepcopy(self.state)if self._identity(obs)else{'ok':False,'reason':'assault_world_catalog_or_actor_changed'}
        if getattr(self.game,'backend',None)!='assisted':return {'ok':False,'reason':'assault_requires_ordinary_assisted_construction'}
        if not all(obs.get('enabled_recipes',{}).get(n)for n in('gun-turret','firearm-magazine','stone-wall')):
            return {'ok':False,'reason':'assault_native_military_recipes_required'}
        report=self.threats.survey(obs)
        if not report.get('ok'):return report
        if report.get('truncated'):return {'ok':False,'reason':'assault_complete_local_charted_survey_required'}
        if not(report.get('actor',{}).get('character_controller')or report.get('actor',{}).get('remote_character_controller')):
            return {'ok':False,'reason':'assault_actor_character_controller_required'}
        close=[q for q in report['targets']if q.get('dangerously_close')and q['role']in('worm','spawner')]
        if not close:return {'ok':False,'reason':'no_dangerously_close_charted_stationary_nest'}
        primary=min(close,key=lambda q:(q['factory_distance'],q['unit_number']))
        cluster=[q for q in report['targets']if q['role']in('worm','spawner')and math.dist(tuple(q['position'].values()),tuple(primary['position'].values()))<=48]
        if not 1<=len(cluster)<=12:return {'ok':False,'reason':'assault_twelve_exact_target_budget_exceeded'}
        if any(q['role']=='worm'and(type(q.get('combat',{}).get('range'))not in(int,float)or q['combat']['range']>25)for q in cluster):
            return {'ok':False,'reason':'assault_enemy_range_exceeds_reviewed_twenty_five'}
        if any(q.get('armor_warning')for q in cluster):return {'ok':False,'reason':'assault_normal_ammo_native_armor_unresolved'}
        targets=[{**{k:deepcopy(q[k])for k in('name','unit_number','position','world_id','surface_index','force_index')},
                  'reviewed_range':q.get('combat',{}).get('range')}for q in sorted(cluster,key=lambda t:t['unit_number'])]
        operation=_hash({'world':obs['world_id'],'catalog':self.catalog.fingerprint,'targets':targets,'prepared':report['tick']})
        ready=self._stage(obs,report,targets,operation,0)
        if not ready.get('ok'):return ready
        ammo_item,ammo_evidence=self._select_ammo(obs,report)
        ready['stage']['ammo_item']=ammo_item
        for post in ready['stage']['posts']:post['ammo_item']=ammo_item
        return dict(ok=True,id=operation,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,actor_unit=obs['actor_unit_number'],
            phase='stock',targets=targets,target_set_hash=_hash(targets),stages=[ready['stage']],active_stage=0,
            prepared_tick=report['tick'],initial_force_profile=report['force_modifiers'],initial_survey_id=report['survey_id'],
            ammo_item=ammo_item,ammo_selection=ammo_evidence,requirements={'gun-turret':OPERATION_GUNS,ammo_item:OPERATION_MAGAZINES,'stone-wall':OPERATION_WALLS,'repair-pack':1},
            maximum_stages=MAX_STAGES,maximum_post_losses=MAX_LOSSES,maximum_launch_ticks=LAUNCH_TICKS,
            handcrafted_magazines_issued=0,scope='bounded under-fire normal-projectile trial; no predicted victory')

    def inspect(self,obs):
        if not self._identity(obs):return {'ok':False,'reason':'assault_world_catalog_or_actor_changed'}
        posts=[dict(q,deployment_id=s['deployment_ids'].get(q['name']))for s in self.state['stages']for q in s['posts']]
        args={k:self.state[k]for k in('actor_unit','world_id','catalog_fingerprint','target_set_hash','targets')}
        args.update(operation_id=self.state['id'],posts=posts,ammo_item=self.state['ammo_item'])
        result=self.game.query('local x=helpers.json_to_table('+_payload(args)+');'+INSPECT_ASSAULT_LUA)
        if not isinstance(result,dict)or not result.get('ok'):return result if isinstance(result,dict)else{'ok':False,'reason':'assault_native_inspection_receipt_invalid'}
        if(result.get('world_id')!=self.state['world_id']or result.get('actor_unit')!=self.state['actor_unit']
                or type(result.get('tick'))is not int or not isinstance(result.get('targets'),list)
                or len(result['targets'])!=len(self.state['targets'])
                or {q.get('unit_number')for q in result['targets']}!={q['unit_number']for q in self.state['targets']}
                or not isinstance(result.get('posts'),list)or len(result['posts'])!=len(posts)
                or {q.get('name')for q in result['posts']}!={q['name']for q in posts}
                or type(result.get('loss_count'))is not int or result['loss_count']<0):
            return {'ok':False,'reason':'assault_native_inspection_receipt_invalid'}
        return result

    def _register(self,obs,stage):
        boardpath=self.path.parent/'pcb-board.json';board=json.loads(boardpath.read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id']or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'assault_board_identity_changed'}
        old=board['blocks'].get(stage['block'])
        if old is not None and old!=stage['plan']:return {'status':'blocked','reason':'assault_registered_plan_changed'}
        if old is None:board['blocks'][stage['block']]=deepcopy(stage['plan']);_atomic_json(boardpath,board)
        expected=dict(world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,
            posts=deepcopy(stage['posts']),coverage_scopes={stage['scope_id']:deepcopy(stage['scope'])})
        if 'perimeter'not in stage:stage['perimeter']=expected;self._save()
        elif any(stage['perimeter']['coverage_scopes'][stage['scope_id']].get(k)!=stage['scope'][k]
                for k in('areas','cells','effective_range','covering_posts','new_posts')):
            return {'status':'blocked','reason':'assault_stage_owned_scope_changed'}
        return None

    def _select_ammo(self,obs,report):
        """Prefer researched piercing when ordinary, already produced ingredients fund the raid."""
        stocks=dict(obs.get('inventory',{}))
        for e in obs.get('entities',[]):
            if e.get('name','').startswith('assembling-machine')or e.get('name')in('stone-furnace','steel-furnace','electric-furnace'):
                for k,v in e.get('output_inventory',{}).items():stocks[k]=stocks.get(k,0)+v
        recipe=self.catalog.recipes.get('piercing-rounds-magazine',{});product=next((p for p in recipe.get('products',[])if p['name']=='piercing-rounds-magazine'),None)
        runs=math.ceil(OPERATION_MAGAZINES/_yield(product))if product and _yield(product)>0 else None
        needed={p['name']:p['amount']*runs for p in recipe.get('ingredients',[])if p.get('type','item')=='item'}if runs else{}
        if 'firearm-magazine'in needed:
            needed['iron-plate']=max(0,needed['firearm-magazine']-stocks.get('firearm-magazine',0))*4
        missing={k:max(0,v-stocks.get(k,0))for k,v in needed.items()if k!='firearm-magazine'}
        enabled=report.get('recipes_enabled',{}).get('piercing-rounds-magazine')is True
        if enabled and runs:
            return 'piercing-rounds-magazine',{'reason':'native_military_2_prefer_piercing_collect_missing_ordinary_materials','runs':runs,'requirements':needed,
                'missing_ordinary_materials':missing,'normal_character_crafting_seconds':runs*recipe['energy']}
        return 'firearm-magazine',{'reason':'explicit_yellow_fallback_native_piercing_recipe_unavailable','native_piercing_enabled':enabled,'missing_ordinary_materials':missing}

    def _craft(self,item,runs,reason):
        product=next(p for p in self.catalog.recipes[item]['products']if p['name']==item)
        serial=self.state.get('craft_serial',0)+1;identity=_hash([self.state['id'],item,runs,serial])
        self.state.update(craft_serial=serial,pending_craft=dict(id=identity,item=item,runs=runs,product_yield=_yield(product)))
        self._save()
        return {'type':'craft','recipe':item,'count':runs,'assault_operation_id':self.state['id'],'assault_craft_id':identity,'reason':reason}

    def _reconcile_craft(self):
        """Charge only a paired positive native begin_crafting receipt, never an intent."""
        pending=self.state.get('pending_craft')
        if not pending:return None
        path=self.path.parent/'job-events.jsonl';pairs=[];intents={}
        if path.exists():
            with path.open('rb')as stream:
                start=max(0,path.stat().st_size-1024*1024);stream.seek(start)
                if start:stream.readline()
                for raw in stream:
                    try:row=json.loads(raw)
                    except(ValueError,UnicodeDecodeError):continue
                    key=row.get('key')
                    if row.get('event')=='action_intent':
                        intents.pop(key,None)
                        a=row.get('action',{})
                        if(a.get('type')=='craft'and a.get('assault_operation_id')==self.state['id']and a.get('assault_craft_id')==pending['id']
                                and a.get('recipe')==pending['item']and a.get('count')==pending['runs']):intents[key]=row
                    elif row.get('event')=='action_result'and key in intents and row.get('tick')==intents[key].get('tick'):
                        pairs.append(row.get('result',{}));intents.pop(key)
        if len(pairs)!=1:return {'status':'blocked','reason':'assault_craft_outcome_unknown_requires_paired_native_receipt','craft_id':pending['id']}
        paired=pairs[0]
        started=paired.get('started')
        if paired.get('ok')and type(started)is int and 0<started<=pending['runs']:
            amount=started*pending['product_yield'];issued=dict(self.state.get('crafted_by_item',{}))
            if issued.get(pending['item'],0)+amount>OPERATION_MAGAZINES:
                return {'status':'blocked','reason':'assault_native_started_crafting_exceeds_finite_product_bound'}
            issued[pending['item']]=issued.get(pending['item'],0)+amount
            self.state['crafted_by_item']=issued
            self.state['handcrafted_magazines_issued']=sum(issued.values())
        elif paired.get('ok')and paired.get('reason')=='crafting_queue_busy'and started is None:
            amount=0
        elif paired.get('ok')is False and paired.get('reason')in('not_craftable','recipe_locked'):
            amount=0
        else:return {'status':'blocked','reason':'assault_craft_outcome_unknown_requires_paired_native_receipt','craft_id':pending['id']}
        self.state['last_craft_receipt']=dict(craft_id=pending['id'],native_started=started or 0,produced_or_queued=amount,reason=paired.get('reason'))
        self.state.pop('pending_craft');self._save();return None

    def _prestock(self,obs,native):
        stage=self.state['stages'][self.state['active_stage']];deployed=sum(q.get('paid',False)for q in native['posts'])
        initial=not native.get('native_started');remaining=sum(q['name']not in stage['bindings']for q in stage['posts'])
        wanted={'gun-turret':OPERATION_GUNS if initial else remaining,'stone-wall':OPERATION_WALLS if initial else 24}
        for name,count in wanted.items():
            if obs.get('inventory',{}).get(name,0)<count:return self.bootstrap.ensure_item(obs,name,count)
        count=OPERATION_MAGAZINES if initial else remaining*20;held=native['main_ammo']['full_normal_magazines'];ammo_item=self.state['ammo_item']
        if held<count:
            if obs.get('crafting_queue'):return {'status':'waiting','reason':'assault_waiting_normally_prepaid_engine_crafting_queue'}
            source=getattr(self.perimeter,'ammo_source',None)
            if ammo_item=='firearm-magazine'and source is not None and source.commissioned:
                action=source.ensure_full_magazines(obs,count)
                if action and action.get('type'):return action
            issued=self.state.get('crafted_by_item',{});budget=OPERATION_MAGAZINES-issued.get(ammo_item,0);needed=min(count-held,max(0,budget),50)
            if needed<=0:return {'status':'waiting','reason':'assault_finite_handcraft_budget_used_waiting_ordinary_factory_magazines'}
            recipe=self.catalog.recipes[ammo_item];products=recipe.get('products',[])
            if len(products)!=1 or products[0].get('name')!=ammo_item or _yield(products[0])<=0:
                return {'status':'blocked','reason':'assault_installed_plain_magazine_recipe_required'}
            runs=math.ceil(needed/_yield(products[0]));produced=runs*_yield(products[0])
            if produced>budget:return {'status':'blocked','reason':'assault_handcraft_product_bound_exceeded'}
            for ingredient in recipe['ingredients']:
                if ingredient.get('type','item')!='item':return {'status':'blocked','reason':'assault_handcraft_requires_plain_items'}
                name=ingredient['name'];amount=math.ceil(ingredient['amount']*runs)
                if name=='firearm-magazine':
                    report=self.perimeter.inspect(obs)
                    if not report.get('ok'):return {'status':'blocked','reason':'assault_existing_perimeter_inspection_failed'}
                    if report['actor_full_magazines']<amount:
                        if source is not None and source.commissioned:
                            transfer=source.ensure_full_magazines(obs,amount)
                            if transfer and transfer.get('type'):return transfer
                        yellow_needed=int(min(amount-report['actor_full_magazines'],OPERATION_MAGAZINES-issued.get(name,0)))
                        if yellow_needed<=0:return {'status':'waiting','reason':'assault_yellow_supplement_bound_waiting_factory'}
                        if obs.get('inventory',{}).get('iron-plate',0)<4*yellow_needed:return self.bootstrap.ensure_item(obs,'iron-plate',4*yellow_needed)
                        return self._craft(name,yellow_needed,'finite normal precursor ammunition for assault piercing rounds')
                elif obs.get('inventory',{}).get(name,0)<amount:return self.bootstrap.ensure_item(obs,name,amount)
            return self._craft(ammo_item,runs,'finite assault-only normally paid magazine supplement')
        self.state['prestock_evidence']={'tick':native['tick'],'main_full_magazines':held,'paid_posts':deployed,'remaining_stage_guns':remaining}
        self._save();return None

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs):return {'status':'blocked','reason':'assault_world_catalog_or_actor_changed'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:return {'status':'blocked','reason':'assault_checkpoint_changed'}
        craft=self._reconcile_craft()
        if craft:return craft
        native=self.inspect(obs)
        if not native.get('ok'):return {'status':'blocked',**native}
        proof=self._proof(native)
        if proof:
            self.state.update(phase='verified',verification=proof);self._save();return {'status':'succeeded','evidence':proof}
        if native.get('native_started'):
            clock={k:native[k]for k in('started_tick','deadline_tick')}
            if any(self.state.get(k)!=v for k,v in clock.items()):
                self.state.update(clock);self._save()
            if native['tick']>native['deadline_tick']or native['loss_count']>=MAX_LOSSES:
                self.state.update(phase='stopped',last_native=native);self._save()
                return {'status':'blocked','reason':'assault_finite_time_or_post_loss_limit','evidence':native}
            if not native['listener_owned']:
                if native.get('listener_state')=='foreign':return {'status':'blocked','reason':'assault_existing_death_handler_requires_safe_integration'}
                return {'type':'start_nest_assault','operation_id':self.state['id'],'target_set_hash':self.state['target_set_hash'],
                    'expected_world_id':obs['world_id'],'expected_actor_unit':obs['actor_unit_number'],
                    'reason':'restore missing native death listener without resetting the launch deadline'}
        stage=self.state['stages'][self.state['active_stage']]
        if stage['phase']=='deploy':
            stock=self._prestock(obs,native)
            if stock:return stock
            if not native.get('native_started')and not self.state.get('launch_geometry_refreshed_tick'):
                report=self.threats.survey(obs)
                if not report.get('ok'):return {'status':'blocked',**report}
                ready=self._stage(obs,report,self.state['targets'],self.state['id'],0)
                if not ready.get('ok'):return {'status':'blocked',**ready}
                stage=ready['stage'];stage['ammo_item']=self.state['ammo_item']
                for post in stage['posts']:post['ammo_item']=self.state['ammo_item']
                self.state['stages']=[stage];self.state['initial_force_profile']=report['force_modifiers']
                self.state['launch_geometry_refreshed_tick']=report['tick'];self._save()
                return {'status':'waiting','reason':'full_operation_stocked_first_stage_rebound_to_current_native_rears'}
            clear=self.builder.ensure_plan(obs,_plan([],[],clear_obstacles=stage['plan']['clear_obstacles'],clearance_world_id=obs['world_id']))
            if clear.get('status')!='succeeded':return clear
            access=self._access(stage)
            if not access.get('ok'):return {'status':'blocked','reason':'assault_rear_service_missing_before_turret_payment','evidence':access}
            registered=self._register(obs,stage)
            if registered:return registered
            if not native.get('native_started'):
                return {'type':'start_nest_assault','operation_id':self.state['id'],'target_set_hash':self.state['target_set_hash'],
                    'expected_world_id':obs['world_id'],'expected_actor_unit':obs['actor_unit_number'],
                    'reason':'initialize bounded native projectile death and paid post-loss journal'}
            if not stage.get('underfire_policy'):
                stage['underfire_policy']=dict(mode='controlled_projectile_assault_v1',operation_id=self.state['id'],stage_index=self.state['active_stage'],
                    ammo_item=self.state['ammo_item'],
                    max_post_losses=2,max_enemy_range=25,min_rear_count=2,coverage_margin=2,min_backstop_health_fraction=.5,min_backstop_full_mags=5,
                    started_tick=native['started_tick'],deadline_tick=native['deadline_tick'],targets=deepcopy(self.state['targets']),
                    target_set_hash=self.state['target_set_hash'],backstop_specs=stage['backstop_specs'],force_profile=self.state['initial_force_profile'])
            self.state['phase']='deploy';self._save()
            byname={q['name']:q for q in native['posts']}
            missing=[]
            for post in stage['posts']:
                row=byname[post['name']]
                if row.get('paid'):
                    unit=row.get('unit_number')or row.get('marker_unit')
                    if not row.get('present')and not native.get('lost_posts',{}).get(str(unit)):
                        return {'status':'blocked','reason':'assault_paid_post_absent_without_native_loss_no_repurchase','evidence':row}
                    stage['bindings'][post['name']]=unit;stage['bindings'][_key(dict(post,name='gun-turret'))]=unit
                    registered=next(q for q in stage['perimeter']['posts']if q['name']==post['name'])
                    registered['unit_number']=unit;registered['commissioned']=bool(row.get('present'))
                    self._save();continue
                if row.get('marker_phase'):return {'status':'blocked','reason':'assault_atomic_deployment_incomplete_requires_recovery','evidence':row}
                missing.append(post)
            if missing:
                if len(missing)!=3:return {'status':'blocked','reason':'assault_partial_group_requires_exact_native_reconciliation'}
                from .defense_deployment import bastion_group_deployment_action
                action=bastion_group_deployment_action(self.game,obs,owner_kind='nest_assault')
                stage['group_id']=action['group_id']
                for child in action['deployments']:stage['deployment_ids'][child['post_name']]=child['deployment_id']
                self._save();return action
            alive_positions={tuple(q['position'].values())for q in native['posts']if q.get('present')}
            walls=[e for b in stage['bastions']if tuple(b['position'].values())in alive_positions for e in b['entities']if e['name']=='stone-wall']
            if not walls:return {'status':'blocked','reason':'assault_no_surviving_stage_guns_no_wall_rebuild'}
            built=self.builder.ensure_plan(obs,_plan(walls,[],clear_obstacles=[],clearance_world_id=obs['world_id']))
            if built.get('status')!='succeeded':return built
            stage['phase']='fight';stage['scope']['commissioned']=True
            stage['perimeter']['coverage_scopes'][stage['scope_id']]['commissioned']=True
            self.state.update(phase='fight',last_progress_tick=native['tick'],last_target_health=sum(q.get('health',0)for q in native['targets']))
            self._save();return {'status':'waiting','reason':'normally_armed_stage_now_firing_ordinary_projectiles','evidence':native}
        health=sum(q.get('health',0)for q in native['targets']);previous=self.state.get('last_target_health',health)
        if health<previous:self.state['last_progress_tick']=native['tick']
        self.state['last_target_health']=health;self._save()
        posts=[q for q in native['posts']if q.get('present')and q.get('health',0)>0 and q.get('ammo',{}).get('normal_rounds',0)>0]
        opportunity=any(math.dist(tuple(p['position'].values()),tuple(q['position'].values()))<=stage['scope']['native_range']
            for p in posts for q in native['targets']if q['present'])
        if opportunity:
            if native['tick']-self.state['last_progress_tick']>600:return {'status':'blocked','reason':'assault_no_target_damage_progress_for_ten_seconds','evidence':native}
            return {'status':'waiting','reason':'native_projectile_fire_waiting_exact_target_death','evidence':native}
        if len(self.state['stages'])>=MAX_STAGES:return {'status':'blocked','reason':'assault_three_stage_limit_before_complete_clear','evidence':native}
        report=self.threats.survey(obs)
        if not report.get('ok'):return {'status':'blocked',**report}
        remaining=[q for q in self.state['targets']if any(r['unit_number']==q['unit_number']and r['present']for r in native['targets'])]
        ready=self._stage(obs,report,remaining,self.state['id'],len(self.state['stages']))
        if not ready.get('ok'):return {'status':'blocked',**ready,'combat_evidence':native}
        ready['stage']['ammo_item']=self.state['ammo_item']
        for post in ready['stage']['posts']:post['ammo_item']=self.state['ammo_item']
        self.state['stages'].append(ready['stage']);self.state['active_stage']+=1;self.state['phase']='deploy';self._save()
        return {'status':'waiting','reason':'next_bounded_two_rear_covered_stage_prepared','stage':self.state['active_stage']}

    def _proof(self,native):
        if not native.get('native_started')or not native.get('listener_owned'):return None
        if(len(native.get('targets',[]))!=len(self.state['targets'])or
                {q.get('unit_number')for q in native['targets']}!={q['unit_number']for q in self.state['targets']}):return None
        deaths=native.get('deaths',{});deaths={}if deaths==[]else deaths
        if any(q.get('present')or not deaths.get(str(q['unit_number']),{}).get('own_gun_projectile')for q in native['targets']):return None
        return {'tick':native['tick'],'world_id':self.state['world_id'],'target_set_hash':self.state['target_set_hash'],'ammo_item':self.state['ammo_item'],
            'targets_cleared':len(self.state['targets']),'native_projectile_deaths':[deaths[str(q['unit_number'])]for q in native['targets']],
            'post_losses':native['loss_count'],'stages':len(self.state['stages']),'surviving_posts':[q for q in native['posts']if q.get('present')],
            'scope':'exact initial stationary targets died to native physical gun projectiles; no broad map safety claim'}

    def proof(self,obs):
        native=self.inspect(obs)if self.state else{}
        return self._proof(native)if native.get('ok')else None
