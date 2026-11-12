"""Paid, same-tick deployment of registered defense posts."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder
from .ammo_transfer import AMMO_GUARD_LUA
from .planning import overlap
from .resource_policy import mining_reservations
from .transport import VECTORS


ACTION='build_and_arm_bastion_post'
GROUP_ACTION='build_and_arm_bastion_group'
AMMO_ITEMS={'firearm-magazine','piercing-rounds-magazine'}
OWNER_FILES={'hardening':'defense-hardening.json','nest_assault':'nest-assault.json','nest_sweep':'nest-sweep.json'}
SCOPE_FIELDS=('areas','cells','effective_range','covering_posts','new_posts')
def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _identity(v,world,catalog):return v.get('world_id')==world and v.get('catalog_fingerprint')==catalog
def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _generation(v):
    result={k:v[k]for k in('owner_kind','authority_revision','ammo_item','post_name','scope_id','position','plan_revision','expected_world_id','expected_lost_unit')}
    result['position']={k:int(n)for k,n in result['position'].items()};return result


def validate_bastion_deployment(action,world=None):
    required={'type','owner_kind','authority_revision','ammo_item','deployment_id','post_name','scope_id','position','plan_revision','expected_world_id',
              'expected_actor_unit','expected_lost_unit'}
    if(not isinstance(action,dict)or set(action)-required-{'reason'}or not required<=set(action)or action.get('type')!=ACTION
            or action.get('owner_kind')not in OWNER_FILES
            or action.get('ammo_item')not in AMMO_ITEMS
            or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or any(not isinstance(action.get(k),str)or not action[k]or len(action[k])>180
                   for k in('post_name','scope_id','expected_world_id'))
            or any(not isinstance(action.get(k),str)or len(action[k])!=64 or any(c not in'0123456789abcdef'for c in action[k])
                   for k in('deployment_id','plan_revision','authority_revision'))
            or(action['expected_lost_unit']is not None and(type(action['expected_lost_unit'])is not int or action['expected_lost_unit']<1))
            or('reason'in action and not isinstance(action['reason'],str))):raise ValueError('exact typed bastion deployment envelope required')
    p=action.get('position')
    if(not isinstance(p,dict)or set(p)!={'x','y'}or any(type(v)not in(int,float)or not math.isfinite(v)or v%1 for v in p.values())):
        raise ValueError('exact finite integer bastion position required')
    if world is not None and action['expected_world_id']!=world:raise ValueError('bastion deployment world changed')
    if action['deployment_id']!=_hash(_generation(action)):raise ValueError('bastion deployment identity does not match its immutable generation')


def _foreign_areas(root,board,own,world,catalog):
    """Exclude only the exact authorized Hardening block; retain all other reserves."""
    blocks=[b for name,b in board['blocks'].items()if name!=own];bus=board.get('shared_main_bus',{})
    areas=[b['reserved_area']for b in blocks if b.get('reserved_area')]
    areas += [r['area']for r in bus.get('reservations',[])if r.get('area')]
    geometry=FactoryBuilder.__new__(FactoryBuilder)
    cells=geometry._occupied_by_plan([e for b in blocks+[bus]for e in b.get('entities',[])])
    ports=[p for b in blocks+[bus]for p in b.get('ports',[])]
    for b in blocks+[bus]:
        for pair in b.get('underground_pairs',[]):
            if pair.get('direction')not in VECTORS:raise ValueError('foreign prospective tunnel heading invalid')
            dx,dy=VECTORS[pair['direction']];a,c=pair['input'],pair['output']
            span=abs(c[0]-a[0])+abs(c[1]-a[1])
            if span%1 or not 1<=span<=32 or(a[0]+span*dx,a[1]+span*dy)!=tuple(c):raise ValueError('foreign prospective tunnel span invalid')
            cells.update((a[0]+i*dx,a[1]+i*dy)for i in range(int(span)+1))
    for name in('material-sources.json','fluid-sources.json'):
        path=root/name
        if path.exists():
            registry=json.loads(path.read_text(encoding='utf-8'))
            if not _identity(registry,world,catalog):raise ValueError('foreign typed source registry identity changed')
            ports.extend(registry.get('sources',{}).values())
    for p in ports:
        if p.get('facing')not in VECTORS or p.get('direction')not in('input','output'):continue
        x,y=p['position']['x'],p['position']['y'];dx,dy=VECTORS[p['facing']]
        if p['direction']=='input':dx,dy=-dx,-dy
        containing=[a for a in areas if a[0][0]<=x<=a[1][0]and a[0][1]<=y<=a[1][1]];length=2
        for step in range(1,5):
            xx,yy=x+step*dx,y+step*dy
            if containing and not any(xx+.49>a[0][0]and xx-.49<a[1][0]and yy+.49>a[0][1]and yy-.49<a[1][1]for a in containing):
                length=max(length,step);break
        cells.update((x+i*dx,y+i*dy)for i in range(length+1))
    areas.extend([[[x-.5,y-.5],[x+.5,y+.5]]for x,y in cells])
    return areas


def _load_owner(root,owner_kind):
    if owner_kind not in OWNER_FILES:raise ValueError('unknown deployment owner authority')
    document=json.loads((root/OWNER_FILES[owner_kind]).read_text(encoding='utf-8'))
    if owner_kind=='hardening':return document,document,None,_hash({'owner_kind':'hardening','worm_policy':'exclude'})
    if owner_kind=='nest_sweep':return _load_sweep(document)
    try:from .nest_assault import deployment_stage
    except ImportError as exc:raise ValueError('trusted NestAssault authority not installed')from exc
    stage=deployment_stage(document);policy=stage.get('underfire_policy',{})
    start,deadline=document.get('started_tick'),policy.get('deadline_tick')
    if(policy.get('mode')!='controlled_projectile_assault_v1'or policy.get('operation_id')!=document.get('id')
            or type(document.get('active_stage'))is not int or not 0<=document['active_stage']<3
            or type(policy.get('stage_index'))is not int or policy['stage_index']!=document['active_stage']or not isinstance(document.get('stages'),list)
            or not 1<=len(document['stages'])<=3 or policy.get('max_post_losses')!=2 or policy.get('max_enemy_range')!=25
            or policy.get('min_rear_count')!=2 or policy.get('coverage_margin')!=2
            or policy.get('min_backstop_health_fraction')!=.5 or policy.get('min_backstop_full_mags')!=5
            or type(start)is not int or type(deadline)is not int or not start<deadline<=start+180*60
            or deadline!=document.get('deadline_tick')or policy.get('started_tick')!=start
            or policy.get('target_set_hash')!=document.get('target_set_hash')
            or policy.get('targets')!=document.get('targets')or _hash(policy.get('targets'))!=policy.get('target_set_hash')
            or not isinstance(policy.get('target_set_hash'),str)or len(policy['target_set_hash'])!=64
            or not isinstance(policy.get('targets'),list)or not 1<=len(policy['targets'])<=12
            or not isinstance(policy.get('backstop_specs'),list)or not 2<=len(policy['backstop_specs'])<=12
            or policy.get('ammo_item')not in AMMO_ITEMS or stage.get('ammo_item')!=policy['ammo_item']
            or len(stage.get('posts',[]))!=3 or sum(e['name']=='gun-turret'for e in stage['plan']['entities'])!=3
            or sum(e['name']=='stone-wall'for e in stage['plan']['entities'])!=24):
        raise ValueError('registered NestAssault controlled-underfire policy is invalid or unbounded')
    for witnesses in(policy['targets'],policy['backstop_specs']):
        units=[]
        for q in witnesses:
            p=q.get('position')
            if(type(q.get('unit_number'))is not int or q['unit_number']<1 or not isinstance(q.get('name'),str)or not q['name']
                    or not isinstance(p,dict)or set(p)!={'x','y'}or any(type(v)not in(int,float)or not math.isfinite(v)for v in p.values())):
                raise ValueError('typed finite NestAssault native witness required')
            units.append(q['unit_number'])
        if len(set(units))!=len(units):raise ValueError('distinct native NestAssault witnesses required')
    return document,stage,policy,_hash({'owner_kind':'nest_assault','operation_id':document['id'],
        'stage_index':document['active_stage'],'policy':policy})


def _load_sweep(document):
    """Only the installed known-file adapter may authorize a committed wave."""
    try:from .nest_sweep import deployment_wave
    except ImportError as exc:raise ValueError('trusted NestSweep authority not installed')from exc
    wave=deployment_wave(document);policy=wave.get('underfire_policy',{});posts=wave.get('posts',[])
    count=policy.get('post_count');start=document.get('started_tick');deadline=policy.get('deadline_tick')
    entities=wave.get('plan',{}).get('entities',[]);profile=policy.get('force_profile')
    if(policy.get('mode')!='committed_projectile_wave_v2'or policy.get('operation_id')!=document.get('id')
            or type(count)is not int or not 6<=count<=12 or len(posts)!=count
            or policy.get('ammo_item')!='piercing-rounds-magazine'or wave.get('ammo_item')!=policy['ammo_item']
            or policy.get('max_enemy_range')!=25 or policy.get('coverage_margin')!=2
            or policy.get('min_planned_neighbors')!=2 or policy.get('min_target_coverage')!=2
            or policy.get('min_backstop_count')!=2 or policy.get('min_backstop_health_fraction')!=.5
            or policy.get('min_backstop_full_mags')!=5 or policy.get('planned_posts')!=posts
            or type(start)is not int or type(deadline)is not int or not start<deadline<=start+10800
            or policy.get('started_tick')!=start or document.get('deadline_tick')!=deadline
            or not isinstance(policy.get('targets'),list)or not 1<=len(policy['targets'])<=12
            or policy['targets']!=document.get('targets')or _hash(policy['targets'])!=document.get('target_set_hash')
            or policy.get('target_set_hash')!=document.get('target_set_hash')
            or not isinstance(policy.get('backstop_specs'),list)or not 2<=len(policy['backstop_specs'])<=12
            or not isinstance(profile,dict)or any(type(profile.get(k))not in(int,float)or not math.isfinite(profile[k])
                for k in('ammo_damage','turret_damage','gun_speed'))
            or sum(e.get('name')=='gun-turret'for e in entities)!=count
            or not 0<=sum(e.get('name')=='stone-wall'for e in entities)<=8*count
            or type(document.get('actor_unit'))is not int or document['actor_unit']<1):
        raise ValueError('registered committed wave policy is invalid, changed or unbounded')
    for witnesses in(policy['targets'],policy['backstop_specs']):
        units=[]
        for q in witnesses:
            p=q.get('position')
            if(type(q.get('unit_number'))is not int or q['unit_number']<1 or not isinstance(q.get('name'),str)or not q['name']
                    or not isinstance(p,dict)or set(p)!={'x','y'}or any(type(v)not in(int,float)or not math.isfinite(v)for v in p.values())):
                raise ValueError('exact finite committed wave witness required')
            units.append(q['unit_number'])
        if len(set(units))!=len(units):raise ValueError('distinct committed wave witnesses required')
    return document,wave,policy,_hash({'owner_kind':'nest_sweep','operation_id':document['id'],'policy':policy})


def _context(game,action):
    validate_bastion_deployment(action,getattr(game,'expected_world',None))
    if getattr(game,'expected_world',None)!=action['expected_world_id']or getattr(game,'backend',None)!='assisted':
        raise ValueError('bastion deployment requires the bound ordinary assisted game')
    root=Path(game.cfg.runtime_dir);document,state,policy,authority=_load_owner(root,action['owner_kind'])
    if action['authority_revision']!=authority:raise ValueError('deployment owner policy revision changed')
    if action['owner_kind']=='nest_sweep'and action['expected_actor_unit']!=document['actor_unit']:
        raise ValueError('committed wave actor changed')
    board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
    perimeter=(json.loads((root/'defense-perimeter.json').read_text(encoding='utf-8'))
               if action['owner_kind']=='hardening'else state.get('perimeter',{}))
    world=action['expected_world_id'];catalog=state.get('catalog_fingerprint')
    phases=('register','walls','verified')if action['owner_kind']=='hardening'else('register','deploy','fight','verified')
    if(not catalog or not all(_identity(v,world,catalog)for v in(document,state,board,perimeter))
            or state.get('phase')not in phases or state.get('scope_id')!=action['scope_id']
            or action['ammo_item']!=(state.get('ammo_item','firearm-magazine')if policy is None else policy['ammo_item'])
            or _hash(state.get('plan'))!=action['plan_revision']or board.get('blocks',{}).get(state.get('block'))!=state['plan']):
        raise ValueError('registered deployment owner world, catalog or whole plan changed')
    posts=[p for p in state['posts']if p.get('name')==action['post_name']]
    published=[p for p in perimeter['posts']if p.get('name')==action['post_name']]
    if len(posts)!=1 or len(published)!=1:raise ValueError('one exact registered bastion post required')
    post,registered=posts[0],published[0]
    if(post.get('position')!=action['position']or any(registered.get(k)!=post.get(k)
            for k in('name','position','coverage_scope','local_walk_access'))or post.get('coverage_scope')!=action['scope_id']
            or(policy is not None and(post.get('ammo_item')!=action['ammo_item']or registered.get('ammo_item')!=action['ammo_item']))
            or registered.get('lost_unit_number')!=action['expected_lost_unit']):raise ValueError('registered bastion post or loss generation changed')
    scope=perimeter.get('coverage_scopes',{}).get(action['scope_id'],{})
    if(any(scope.get(k)!=state['scope'].get(k)for k in SCOPE_FIELDS)
            or action['post_name']not in scope.get('new_posts',[])):raise ValueError('registered bastion scope changed')
    spec={'name':'gun-turret','position':post['position'],'direction':0}
    if spec not in state['plan']['entities']:raise ValueError('bastion is not an exact planned turret')
    geometry=FactoryBuilder.__new__(FactoryBuilder);boxes=[geometry._occupied_by_plan([e])for e in state['plan']['entities']]
    if(any(e['name']not in('gun-turret','stone-wall')for e in state['plan']['entities'])
            or sum(map(len,boxes))!=len(set().union(*boxes))):raise ValueError('whole prospective bastion footprints overlap or changed type')
    p=post['position'];box=[[p['x']-1,p['y']-1],[p['x']+1,p['y']+1]]
    area=state['plan']['reserved_area']
    if not(area[0][0]<=box[0][0]and area[0][1]<=box[0][1]and box[1][0]<=area[1][0]and box[1][1]<=area[1][1]):
        raise ValueError('bastion footprint lies outside its exact reserved block')
    foreign=_foreign_areas(root,board,state['block'],world,catalog)
    zones=mining_reservations(game,world)
    if any(overlap(box,a)for a in foreign+[z['area']for z in zones]):raise ValueError('bastion overlaps foreign production, prospective transport, port apron or mining reserve')
    bound=state.get('bindings',{}).get(_key(spec));registered_unit=registered.get('unit_number')
    if any(v is not None and(type(v)is not int or v<1)for v in(bound,registered_unit)):raise ValueError('bastion saved unit invalid')
    lineage=set(registered.get('lost_unit_numbers',[]))|{registered.get('lost_unit_origin'),registered.get('lost_unit_number')}
    prior_authorized=bound is None or(action['expected_lost_unit']is not None and bound in lineage)
    return dict(action,catalog_fingerprint=catalog,registered_unit=registered_unit,bound_unit=bound,
                prior_binding_authorized=prior_authorized,scope_revision=_hash({k:scope[k]for k in SCOPE_FIELDS}),
                effective_range=scope['effective_range'],hotspot=state['hotspot'],assault_policy=policy)


def bastion_deployment_action(game,obs,post_name,*,owner_kind='hardening'):
    """Read-only emitter; the actual operation independently revalidates all inputs."""
    root=Path(game.cfg.runtime_dir);_,state,policy,authority=_load_owner(root,owner_kind)
    perimeter=(json.loads((root/'defense-perimeter.json').read_text(encoding='utf-8'))
               if owner_kind=='hardening'else state.get('perimeter',{}))
    matches=[p for p in perimeter.get('posts',[])if p.get('name')==post_name]
    if len(matches)!=1:raise ValueError('one registered bastion name required')
    p=matches[0];identity=dict(owner_kind=owner_kind,authority_revision=authority,
        ammo_item=state.get('ammo_item','firearm-magazine')if policy is None else policy['ammo_item'],
        post_name=post_name,scope_id=state['scope_id'],position=p['position'],plan_revision=_hash(state['plan']),
        expected_world_id=obs.get('world_id'),expected_lost_unit=p.get('lost_unit_number'))
    action=dict(type=ACTION,**identity,deployment_id=_hash(_generation(identity)),expected_actor_unit=obs.get('actor_unit_number'),
        reason='normally pay one turret and arm twenty full magazines in one native operation')
    if not obs.get('ok',True):raise ValueError('fresh successful world observation required')
    _context(game,action);return action


# Reuse the exact normal/metadata predicates used by the existing full-ammo collector.
AMMO_PLAIN_LUA=('local function normal(stack)'+AMMO_GUARD_LUA.split('local function normal(stack)',1)[1]).replace('stack.name=="firearm-magazine"','stack.name==x.ammo_item ')

DEPLOY_BASTION_LUA=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if x.owner_kind~="hardening"and x.owner_kind~="nest_assault"and x.owner_kind~="nest_sweep"then return reject("bastion_deployment_owner_invalid")end
if not d or d.world_id~=x.expected_world_id then return reject("bastion_deployment_world_changed")end
if not a or not a.valid or a.unit_number~=x.expected_actor_unit or a.force~=f or a.surface~=s then return reject("bastion_deployment_actor_changed")end
local main=a.get_main_inventory();if not main or not main.valid then return reject("bastion_main_inventory_missing")end
if x.ammo_item~="firearm-magazine"and x.ammo_item~="piercing-rounds-magazine"then return reject("bastion_ammo_item_invalid")end
local gun=prototypes.entity["gun-turret"];local proto=prototypes.item[x.ammo_item];local size=proto and proto.magazine_size
if not gun or gun.tile_width~=2 or gun.tile_height~=2 or not proto or proto.type~="ammo"or size~=10
 or not proto.ammo_category or proto.ammo_category.name~="bullet"
 or not proto.stack_size or proto.stack_size<20 or not gun.attack_parameters
 or x.effective_range>gun.attack_parameters.range-2 then return reject("bastion_native_prototypes_changed")end
if x.ammo_item=="piercing-rounds-magazine"and(not f.recipes[x.ammo_item]or not f.recipes[x.ammo_item].enabled)then return reject("bastion_piercing_recipe_not_unlocked")end
local ok,slots=pcall(function()return gun.get_inventory_size(defines.inventory.turret_ammo)end)
if not ok or not slots or slots<1 then return reject("bastion_native_ammo_inventory_prototype_missing")end
'''+AMMO_PLAIN_LUA+r'''
local function plain_gun(st)return st.valid_for_read and st.name=="gun-turret"and st.quality.name=="normal"
 and st.prototype.type=="item"and st.health==1 and st.spoil_tick==0
 and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data end
local function totals(inv)
 local guns,count,rounds,full=0,0,0,0
 for i=1,#inv do local st=inv[i];if st.valid_for_read then
  if st.name=="gun-turret"and st.quality.name=="normal"then if not plain_gun(st)then return nil end;guns=guns+st.count end
  if normal(st)then if not plain(st)then return nil end;count=count+st.count;rounds=rounds+(st.count-1)*size+st.ammo;full=full+st.count-(st.ammo<size and 1 or 0)end
 end end
 return {guns=guns,magazines=count,rounds=rounds,full_magazines=full}end
local p=x.position;local existing=target(p,"gun-turret");local marker=d.defense_deployments and d.defense_deployments[x.deployment_id]
if marker then
 if marker.world_id~=x.expected_world_id or marker.catalog_fingerprint~=x.catalog_fingerprint
  or marker.owner_kind~=x.owner_kind or marker.authority_revision~=x.authority_revision or marker.ammo_item~=x.ammo_item
  or marker.actor_unit~=a.unit_number or marker.post_name~=x.post_name
  or marker.scope_id~=x.scope_id or marker.plan_revision~=x.plan_revision or marker.scope_revision~=x.scope_revision
  or marker.position.x~=p.x or marker.position.y~=p.y or marker.expected_lost_unit~=x.expected_lost_unit then return reject("bastion_deployment_marker_identity_changed")end
 if marker.phase~="complete"then return reject("bastion_deployment_incomplete_intent_requires_recovery")end
 if not existing or existing.force~=f or existing.surface~=s or existing.unit_number~=marker.unit_number
  or existing.position.x~=p.x or existing.position.y~=p.y or existing.quality.name~="normal"
  or(x.registered_unit and x.registered_unit~=marker.unit_number)
  or(x.bound_unit and x.bound_unit~=marker.unit_number and not x.prior_binding_authorized)then return reject("paid_bastion_missing_or_replaced")end
 local result={};for k,v in pairs(marker.receipt)do result[k]=v end
 result.reused_receipt=true;result.paid_this_call=0;result.loaded_this_call=0;return result
end
if existing then return reject("unmarked_existing_bastion_not_adopted")end
if x.registered_unit or not x.prior_binding_authorized then return reject("bastion_known_loss_must_be_recovered_before_build")end
local area={{p.x-1,p.y-1},{p.x+1,p.y+1}}
if s.count_entities_filtered{area=area,type="resource"}>0 or not s.can_place_entity{name="gun-turret",position=p,direction=0,force=f}then return reject("bastion_site_changed_or_resource_overlap")end
for layer in pairs(gun.collision_mask.layers)do if s.get_tile(p).collides_with(layer)then return reject("bastion_site_tile_changed")end end
local assault_operation=nil
if x.owner_kind~="hardening"then
 return reject("nest_assault_guard_not_linked")
else
 for _,enemy in pairs(s.find_entities_filtered{position=x.hotspot,radius=48,force="enemy",type="turret"})do local attack=enemy.prototype.attack_parameters
  if attack and attack.range and(enemy.position.x-p.x)^2+(enemy.position.y-p.y)^2<=(attack.range+2)^2 then return reject("bastion_site_inside_enemy_turret_range")end
 end
end
local before=totals(main);if not before then return reject("bastion_actor_ammo_metadata_unsupported")end
local gun_slot=nil;local sources={};local remaining=20
for i=1,#main do local st=main[i]
 if not gun_slot and plain_gun(st)then gun_slot=i end
 if remaining>0 and normal(st)then
  if not plain(st)then return reject("bastion_actor_ammo_metadata_unsupported")end
  local available=st.count-(st.ammo<size and 1 or 0);local amount=math.min(remaining,available)
  if amount>0 then sources[#sources+1]={slot=i,count=amount,before=st.count,ammo=st.ammo};remaining=remaining-amount end
 end
end
if not gun_slot or remaining>0 then return reject("bastion_paid_plain_turret_and_twenty_main_magazines_required")end
if x.preflight_only then return {ok=true,preflight=true,before=before}end
d.defense_deployments=d.defense_deployments or{}
marker={phase="intent",world_id=x.expected_world_id,catalog_fingerprint=x.catalog_fingerprint,
 owner_kind=x.owner_kind,authority_revision=x.authority_revision,ammo_item=x.ammo_item,
 actor_unit=a.unit_number,post_name=x.post_name,scope_id=x.scope_id,
 plan_revision=x.plan_revision,scope_revision=x.scope_revision,position=p,expected_lost_unit=x.expected_lost_unit,
 before=before,gun_slot=gun_slot,gun_stack_before=main[gun_slot].count,sources=sources,start_tick=game.tick}
d.defense_deployments[x.deployment_id]=marker
main[gun_slot].count=main[gun_slot].count-1;marker.paid_turrets=1
local built=s.create_entity{name="gun-turret",position=p,direction=0,force=f,quality="normal",raise_built=true,player=a.player}
if not built then
 if target(p,"gun-turret")then marker.phase="uncertain_build";return reject("bastion_build_outcome_unknown")end
 local current=totals(main);local refunded=current and main.insert{name="gun-turret",quality="normal",count=1}==1
 local restored=refunded and totals(main);refunded=restored and restored.guns==before.guns
  and restored.magazines==before.magazines and restored.rounds==before.rounds
 marker.phase=refunded and"failed_refunded"or"failed_refund_incomplete";marker.refunded_turrets=refunded and 1 or 0
 return reject(refunded and"bastion_build_failed_paid_turret_refunded"or"bastion_build_failed_refund_incomplete")
end
marker.unit_number=built.unit_number;marker.phase="built"
local dest=built.get_inventory(defines.inventory.turret_ammo)
if not dest or not dest.valid or dest.get_item_count()>0 or dest.get_insertable_count{name=x.ammo_item,quality="normal"}<20 then return reject("bastion_created_ammo_inventory_unexpected")end
local moved=0;marker.source_transfers={}
while moved<20 do
 local index=nil;local amount=0
 for i=1,#main do local st=main[i];if normal(st)then
  if not plain(st)then return reject("bastion_paid_source_changed_inside_operation")end
  local available=st.count-(st.ammo<size and 1 or 0)
  if available>0 then index=i;amount=math.min(20-moved,available);break end
 end end
 if not index then return reject("bastion_paid_source_changed_inside_operation")end
 local st=main[index];local source_count=st.count;local source_ammo=st.ammo
 local inserted=dest.insert{name=x.ammo_item,quality="normal",count=amount}
 if type(inserted)~="number"or inserted<0 or inserted>amount or inserted%1~=0 then return reject("bastion_native_insert_count_invalid")end
 if inserted>0 then
  st.count=source_count-inserted;moved=moved+inserted;marker.loaded_magazines=moved
  marker.source_transfers[#marker.source_transfers+1]={slot=index,before=source_count,moved=inserted,partial_preserved=source_ammo<size}
 end
 if inserted~=amount then return reject("bastion_native_ammo_transfer_incomplete")end
end
local after=totals(main);local loaded=totals(dest)
if not after or not loaded or moved~=20 or loaded.magazines~=20 or loaded.rounds~=20*size or loaded.guns~=0
 or after.guns~=before.guns-1 or before.magazines~=after.magazines+loaded.magazines
 or before.rounds~=after.rounds+loaded.rounds or game.tick~=marker.start_tick then return reject("bastion_payment_or_round_conservation_failed")end
if x.owner_kind~="hardening"then local e=built;local op=assault_operation
 return reject("nest_assault_configure_not_linked")
end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",deployment_id=x.deployment_id,world_id=x.expected_world_id,
 owner_kind=x.owner_kind,authority_revision=x.authority_revision,ammo_item=x.ammo_item,magazine_size=size,ammo_category=proto.ammo_category.name,
 catalog_fingerprint=x.catalog_fingerprint,plan_revision=x.plan_revision,scope_revision=x.scope_revision,
 actor_unit=a.unit_number,post_name=x.post_name,scope_id=x.scope_id,unit_number=built.unit_number,position=p,
 paid_turrets=1,loaded_magazines=20,loaded_rounds=loaded.rounds,paid_this_call=1,loaded_this_call=20,
 rounds_before=before.rounds,rounds_after=after.rounds+loaded.rounds,before=before,after=after,
 start_tick=marker.start_tick,armed_tick=game.tick,quality="normal"}
return marker.receipt
'''


def _native_body(owner_kind):
    body=DEPLOY_BASTION_LUA
    if owner_kind!='hardening':
        if owner_kind=='nest_assault':
            from .nest_assault import ASSAULT_DEPLOYMENT_GUARD_LUA as guard,ASSAULT_CONFIGURE_POST_LUA as configure
        elif owner_kind=='nest_sweep':
            from .nest_sweep import SWEEP_DEPLOYMENT_GUARD_LUA as guard,SWEEP_CONFIGURE_POST_LUA as configure
        else:raise ValueError('unknown trusted deployment owner')
        if not all(isinstance(v,str)and v.strip()and '--'not in v for v in(guard,configure)):
            raise ValueError('trusted native assault guards must be nonempty flattenable module constants')
        body=body.replace('return reject("nest_assault_guard_not_linked")',guard+';assault_operation=op')
        body=body.replace('return reject("nest_assault_configure_not_linked")',configure)
    return body


def execute_bastion_deployment(game,action):
    payload=_context(game,action)
    if action['owner_kind']=='nest_sweep':raise ValueError('committed nest sweep requires its complete registered wave in one group')
    body=_native_body(action['owner_kind'])
    return game.query('local x=helpers.json_to_table('+_payload(payload)+');'+body)


def _group_id(deployments):
    return _hash({'deployment_ids':[q['deployment_id']for q in deployments]})


def validate_bastion_group(action,world=None):
    if(not isinstance(action,dict)or set(action)-{'type','group_id','deployments','reason'}
            or not {'type','group_id','deployments'}<=set(action)or action['type']!=GROUP_ACTION
            or not isinstance(action['deployments'],list)or len(action['deployments'])not in(3,6,7,8,9,10,11,12)
            or('reason'in action and not isinstance(action['reason'],str))):raise ValueError('exact bounded registered deployment group required')
    rows=action['deployments']
    for q in rows:validate_bastion_deployment(q,world)
    count=len(rows)
    common=('owner_kind','authority_revision','ammo_item','scope_id','plan_revision','expected_world_id','expected_actor_unit')
    if(any(any(q[k]!=rows[0][k]for k in common)for q in rows)
            or(count!=3 if rows[0]['owner_kind']!='nest_sweep'else not 6<=count<=12)
            or len({q['post_name']for q in rows})!=count
            or len({tuple(q['position'][k]for k in('x','y'))for q in rows})!=count
            or action['group_id']!=_group_id(rows)):raise ValueError('one immutable complete stage, actor, ammunition and distinct posts required')


def bastion_group_deployment_action(game,obs,*,owner_kind='nest_assault'):
    """Read-only emitter for the complete current trusted stage or committed wave."""
    _,state,_,_=_load_owner(Path(game.cfg.runtime_dir),owner_kind)
    count=len(state.get('posts',[]))
    if(count!=3 if owner_kind!='nest_sweep'else not 6<=count<=12):raise ValueError('complete bounded registered stage or wave required')
    rows=[bastion_deployment_action(game,obs,p['name'],owner_kind=owner_kind)for p in state['posts']]
    action=dict(type=GROUP_ACTION,group_id=_group_id(rows),deployments=rows,
        reason='normally pay and arm the complete registered group in one native tick')
    validate_bastion_group(action,getattr(game,'expected_world',None));return action


GROUP_DEPLOY_LUA=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason,group_id=g.group_id}end
local first=g.deployments[1]
local count=#g.deployments;local magazines=20*count
if(first.owner_kind=="nest_sweep"and(count<6 or count>12))or(first.owner_kind~="nest_sweep"and count~=3)then return reject("bastion_group_native_count_invalid")end
local record=d and d.defense_deployment_groups and d.defense_deployment_groups[g.group_id]
if record then
 if record.revision~=g.revision or record.actor_unit~=first.expected_actor_unit or record.world_id~=first.expected_world_id
  or record.ammo_item~=first.ammo_item then return reject("bastion_group_marker_identity_changed")end
 if record.phase~="complete"then return reject("bastion_group_incomplete_intent_requires_recovery")end
 for _,q in ipairs(g.deployments)do q.group_id=g.group_id;q.preflight_only=true;local r=deploy_one(q)
  if not r.ok or not r.reused_receipt then return reject("paid_bastion_group_missing_or_replaced")end
 end
 local r={};for k,v in pairs(record.receipt)do r[k]=v end
 r.reused_receipt=true;r.paid_this_call=0;r.loaded_this_call=0;return r
end
local before=nil
for i,q in ipairs(g.deployments)do
 q.group_id=g.group_id;q.preflight_only=true;local r=deploy_one(q)
 if not r.ok then r.group_id=g.group_id;r.failed_index=i;r.preflight_failed=true;return r end
 if not r.preflight then return reject("bastion_group_requires_all_registered_posts_unbuilt")end
 if not before then before=r.before end
end
if before.guns<count or before.full_magazines<magazines then return reject(count==3 and"bastion_group_three_guns_and_sixty_full_main_magazines_required"or"bastion_wave_all_guns_and_full_main_magazines_required")end
d.defense_deployment_groups=d.defense_deployment_groups or{}
record={phase="intent",revision=g.revision,world_id=first.expected_world_id,catalog_fingerprint=first.catalog_fingerprint,
 owner_kind=first.owner_kind,authority_revision=first.authority_revision,actor_unit=first.expected_actor_unit,
 ammo_item=first.ammo_item,plan_revision=first.plan_revision,scope_id=first.scope_id,
 before=before,start_tick=game.tick,completed_prefix={}}
d.defense_deployment_groups[g.group_id]=record
if first.owner_kind=="nest_sweep"then
 return reject("nest_sweep_group_commit_not_linked")
end
local after=nil;local total_rounds=0
for i,q in ipairs(g.deployments)do
 q.preflight_only=false;local ok,r=pcall(function()return deploy_one(q)end)
 if not ok or not r or not r.ok then
  record.phase="incomplete";record.failed_index=i;record.failure=ok and r and r.reason or"unexpected_native_exception"
  return {ok=false,status="blocked",reason="bastion_group_incomplete_intent_requires_recovery",group_id=g.group_id,
   failed_index=i,completed_prefix=record.completed_prefix,failure=record.failure}
 end
 record.completed_prefix[#record.completed_prefix+1]=r;after=r.after;total_rounds=total_rounds+r.loaded_rounds
 if r.paid_this_call~=1 or r.loaded_this_call~=20 or r.start_tick~=record.start_tick or r.armed_tick~=record.start_tick then
  record.phase="incomplete";record.failure="group_payment_or_tick_changed";return reject("bastion_group_payment_or_tick_changed")end
end
if after.guns~=before.guns-count or before.magazines~=after.magazines+magazines or before.rounds~=after.rounds+total_rounds
 or total_rounds~=magazines*record.completed_prefix[1].magazine_size or game.tick~=record.start_tick then
 record.phase="incomplete";record.failure="group_conservation_failed";return reject("bastion_group_conservation_failed")end
record.phase="complete";record.receipt={ok=true,status="succeeded",group_id=g.group_id,
 owner_kind=first.owner_kind,authority_revision=first.authority_revision,world_id=first.expected_world_id,
 catalog_fingerprint=first.catalog_fingerprint,actor_unit=first.expected_actor_unit,plan_revision=first.plan_revision,
 scope_id=first.scope_id,ammo_item=first.ammo_item,magazine_size=record.completed_prefix[1].magazine_size,
 ammo_category=record.completed_prefix[1].ammo_category,paid_turrets=count,loaded_magazines=magazines,loaded_rounds=total_rounds,
 paid_this_call=count,loaded_this_call=magazines,start_tick=record.start_tick,armed_tick=game.tick,
 same_tick=true,before=before,after=after,deployments=record.completed_prefix,quality="normal"}
return record.receipt
'''


def execute_bastion_group(game,action):
    """Validate every trusted context, then execute ONE native query."""
    validate_bastion_group(action,getattr(game,'expected_world',None))
    rows=[_context(game,q)for q in action['deployments']]
    _,stage,_,_=_load_owner(Path(game.cfg.runtime_dir),rows[0]['owner_kind'])
    if [q['post_name']for q in rows]!=[p['name']for p in stage['posts']]:raise ValueError('group must contain the complete registered stage in plan order')
    payload=dict(group_id=action['group_id'],deployments=rows,revision=_hash([dict(_generation(q),
        expected_actor_unit=q['expected_actor_unit'],catalog_fingerprint=q['catalog_fingerprint'],scope_revision=q['scope_revision'])for q in rows]))
    group_body=GROUP_DEPLOY_LUA
    if rows[0]['owner_kind']=='nest_sweep':
        from .nest_sweep import SWEEP_BEGIN_WAVE_LUA
        if not isinstance(SWEEP_BEGIN_WAVE_LUA,str)or not SWEEP_BEGIN_WAVE_LUA.strip()or'--'in SWEEP_BEGIN_WAVE_LUA:
            raise ValueError('trusted native sweep commit must be a nonempty flattenable module constant')
        group_body=group_body.replace('return reject("nest_sweep_group_commit_not_linked")',SWEEP_BEGIN_WAVE_LUA)
    return game.query('local g=helpers.json_to_table('+_payload(payload)+');local function deploy_one(x) '
        +_native_body(rows[0]['owner_kind'])+' end;'+group_body)
