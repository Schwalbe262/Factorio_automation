"""One prepaid, concentrated, normally armed projectile wave; no staged creep."""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .defense_hardening import _box
from .defense_perimeter import protected_factory_areas
from .nearby_threats import NearbyThreats
from .nest_assault import (NestAssault,ASSAULT_DEPLOYMENT_GUARD_LUA,ASSAULT_CONFIGURE_POST_LUA,
    START_ASSAULT_LUA,CANDIDATE_SURVEY_LUA,INSPECT_ASSAULT_LUA,_hash,_payload)
from .planning import overlap,preflight
from .resource_policy import mining_reservations

POST_COUNT=10
MAGAZINES_PER_POST=20
STOCK_MAGAZINES=POST_COUNT*MAGAZINES_PER_POST
AMMO_ITEM='piercing-rounds-magazine'

SWEEP_START_STOCK_LUA=r'''
local prior=d and d.nest_assaults and d.nest_assaults[x.operation_id]
if not prior then
 if not a or not a.valid or a.unit_number~=x.expected_actor_unit then return {ok=false,status="blocked",reason="assault_start_world_or_actor_changed"}end
 local inv=a.get_main_inventory();local guns,magazines=0,0
 for i=1,#inv do local st=inv[i]
  if st.valid_for_read and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
   and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data then
   if st.name=="gun-turret"then guns=guns+st.count end
   if st.name=="piercing-rounds-magazine"then magazines=magazines+st.count-(st.ammo<st.prototype.magazine_size and 1 or 0)end
  end
 end
 if guns<10 or magazines<200 or a.crafting_queue_size>0 then return {ok=false,status="blocked",reason="sweep_launch_requires_idle_full_stock"}end
end
'''


def deployment_wave(document):
    if(document.get('phase')not in('stock','deploy','fight','verified')or not isinstance(document.get('wave'),dict)):
        raise ValueError('one trusted concentrated nest sweep wave required')
    wave=deepcopy(document['wave'])
    wave.update(world_id=document['world_id'],catalog_fingerprint=document['catalog_fingerprint'],operation_id=document['id'])
    return wave


def _sweep_base_guard():
    body=ASSAULT_DEPLOYMENT_GUARD_LUA
    replacements={
        'policy.mode~="controlled_projectile_assault_v1"':'policy.mode~="committed_projectile_wave_v2"',
        ' or op.target_set_hash~=policy.target_set_hash or policy.max_post_losses~=2':' or op.target_set_hash~=policy.target_set_hash',
        ' or game.tick>op.deadline_tick or op.loss_count>=2':' or game.tick>op.deadline_tick',
        ' and back.health>=back.max_health*.5 and(back.position.x-p.x)^2+(back.position.y-p.y)^2<=(gun.attack_parameters.range-2)^2 then':
            ' and back.health>=back.max_health*.5 then',
    }
    for before,after in replacements.items():
        if body.count(before)!=1:raise RuntimeError('known native assault guard changed; concentrated-wave adaptation requires review')
        body=body.replace(before,after)
    return body


SWEEP_DEPLOYMENT_GUARD_LUA=_sweep_base_guard()+r'''
if op.wave_group_id and op.wave_group_id~=x.group_id then return reject("sweep_operation_already_committed_to_another_wave")end
local nodes=policy.planned_posts;local n=policy.post_count
if not nodes or #nodes~=n or n<6 or n>12 or policy.min_planned_neighbors~=2 or policy.min_target_coverage~=2 then
 return reject("sweep_finite_whole_wave_policy_changed")end
local radius=gun.attack_parameters.range-2;local edges={}
for i,q in ipairs(nodes)do edges[i]={};local neighbors=0
 for j,r in ipairs(nodes)do if i~=j and(q.position.x-r.position.x)^2+(q.position.y-r.position.y)^2<=radius^2 then
  edges[i][#edges[i]+1]=j;neighbors=neighbors+1 end end
 if neighbors<2 then return reject("sweep_planned_post_lacks_two_mutual_neighbors")end
end
for _,foe in ipairs(policy.targets)do local count=0
 for _,q in ipairs(nodes)do if(q.position.x-foe.position.x)^2+(q.position.y-foe.position.y)^2<=gun.attack_parameters.range^2 then count=count+1 end end
 if count<2 then return reject("sweep_target_not_covered_by_two_planned_guns")end
end
local rear={}
for _,q in ipairs(policy.backstop_specs)do local back=target(q.position,"gun-turret")
 if back and back.unit_number==q.unit_number and back.force==f and back.surface==s and back.health>=back.max_health*.5 then
  local full=0;local inv=back.get_inventory(defines.inventory.turret_ammo);local name=q.ammo_item or"firearm-magazine"
  local back_size=prototypes.item[name].magazine_size
  if inv then for i=1,#inv do local st=inv[i]
   if st.valid_for_read and st.name==name and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
    and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data then
    full=full+st.count-(st.ammo<back_size and 1 or 0)end
  end end
  if full>=5 then rear[#rear+1]=q end
 end
end
if #rear<2 then return reject("sweep_two_current_rear_anchors_required")end
for removed=0,n do for rear_index=1,2 do local reached={};local queue={};local head=1
 for i,q in ipairs(nodes)do if i~=removed and(q.position.x-rear[rear_index].position.x)^2+(q.position.y-rear[rear_index].position.y)^2<=radius^2 then
  reached[i]=true;queue[#queue+1]=i end end
 while head<=#queue do local i=queue[head];head=head+1
  for _,j in ipairs(edges[i])do if j~=removed and not reached[j]then reached[j]=true;queue[#queue+1]=j end end
 end
 for i=1,n do if i~=removed and not reached[i]then return reject("sweep_whole_wave_lacks_two_robust_rear_paths")end end
end end
'''

SWEEP_CONFIGURE_POST_LUA=ASSAULT_CONFIGURE_POST_LUA.replace('stage=x.assault_policy.stage_index,','')

SWEEP_BEGIN_WAVE_LUA=r'''
local policy=first.assault_policy
local op=policy and d.nest_assaults and d.nest_assaults[policy.operation_id]
if not op or op.wave_group_id or op.wave_launch_count then return reject("sweep_operation_may_commit_exactly_one_wave")end
op.wave_group_id=g.group_id;op.wave_launch_count=1;op.wave_committed_tick=game.tick
'''

SWEEP_INSPECT_LUA=INSPECT_ASSAULT_LUA.replace('marker.owner_kind=="nest_assault"','marker.owner_kind=="nest_sweep"').replace('return out',r'''
out.wave_group_id=op and op.wave_group_id
out.wave_launch_count=op and op.wave_launch_count
local group=op and op.wave_group_id and d.defense_deployment_groups and d.defense_deployment_groups[op.wave_group_id]
if group then out.group_phase=group.phase
 if group.receipt then out.group_paid_turrets=group.receipt.paid_turrets;out.group_loaded_magazines=group.receipt.loaded_magazines
  out.group_loaded_rounds=group.receipt.loaded_rounds;out.group_same_tick=group.receipt.same_tick end
end
out.main_gun_turrets=0
local main=a.get_main_inventory()
for i=1,#main do local st=main[i]
 if st.valid_for_read and st.name=="gun-turret"and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
  and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data then
  out.main_gun_turrets=out.main_gun_turrets+st.count end
end
return out
''')


def execute_start_sweep(game,action):
    required={'type','operation_id','target_set_hash','expected_world_id','expected_actor_unit'}
    if(not isinstance(action,dict)or not required<=set(action)or set(action)-required-{'reason'}or action['type']!='start_nest_sweep'
            or type(action['expected_actor_unit'])is not int or action['expected_actor_unit']<1
            or any(not isinstance(action[k],str)or not action[k]for k in('operation_id','target_set_hash','expected_world_id'))
            or getattr(game,'expected_world',None)!=action['expected_world_id']or getattr(game,'backend',None)!='assisted'):
        raise ValueError('exact bound typed sweep initialization required')
    doc=json.loads((Path(game.cfg.runtime_dir)/'nest-sweep.json').read_text(encoding='utf-8'))
    if(doc.get('id')!=action['operation_id']or doc.get('world_id')!=action['expected_world_id']or doc.get('actor_unit')!=action['expected_actor_unit']
            or doc.get('target_set_hash')!=action['target_set_hash']or _hash(doc['targets'])!=doc['target_set_hash']
            or not 1<=len(doc['targets'])<=12):raise ValueError('saved single-wave initialization authority changed')
    wave=deployment_wave(doc);payload=dict(action,targets=doc['targets'],catalog_fingerprint=doc['catalog_fingerprint'],backstop_specs=wave['backstop_specs'])
    try:r=game.query('local x=helpers.json_to_table('+_payload(payload)+');'+SWEEP_START_STOCK_LUA+START_ASSAULT_LUA)
    except Exception:return {'ok':False,'status':'blocked','reason':'nest_sweep_initialization_outcome_unknown'}
    known={'assault_start_world_or_actor_changed','assault_actor_must_remain_at_safe_rear','assault_start_existing_journal_changed',
        'assault_initial_enemy_identity_changed','assault_existing_death_handler_requires_safe_integration','sweep_launch_requires_idle_full_stock'}
    if(not isinstance(r,dict)or r.get('ok')not in(True,False)
            or r.get('ok')and(type(r.get('started_tick'))is not int or r.get('deadline_tick')!=r['started_tick']+10800)
            or r.get('ok')is False and(r.get('status')!='blocked'or r.get('reason')not in known)):
        return {'ok':False,'status':'blocked','reason':'nest_sweep_initialization_outcome_unknown'}
    return r


def _point(p):return p['x'],p['y']
def _distance(a,b):return math.dist(_point(a),_point(b))
def _separate(a,b):return max(abs(a[k]-b[k])for k in('x','y'))>=2


def wave_candidates(targets,rears,*,maximum=256):
    """Sample the real target envelope and two parallel approach chains."""
    center={k:sum(t['position'][k]for t in targets)/len(targets)for k in('x','y')}
    rear={k:sum(t['position'][k]for t in rears[:2])/2 for k in('x','y')}
    distance=_distance(center,rear);ux,uy=((center[k]-rear[k])/distance for k in('x','y'))
    result={}
    def add(x,y):
        x,y=round(x),round(y);depth=(x-rear['x'])*ux+(y-rear['y'])*uy
        key=(x,y);result[key]={'position':{'x':x,'y':y},'depth':depth,'center_distance':math.hypot(x-center['x'],y-center['y']),
            'coverage':sum(math.hypot(x-t['position']['x'],y-t['position']['y'])<=18 for t in targets)}
    # The approach is two concurrent support chains, not successive construction stages.
    for depth in(12,24,34):
        for lateral in(-3,3):
            for dx in(-2,0,2):
                for dy in(-2,0,2):add(rear['x']+depth*ux-lateral*uy+dx,rear['y']+depth*uy+lateral*ux+dy)
    for x in range(math.floor(center['x'])-12,math.ceil(center['x'])+13):
        for y in range(math.floor(center['y'])-14,math.ceil(center['y'])+15):
            if (x-int(center['x']))%2==0 and(y-int(center['y']))%2==0:add(x,y)
    # Include narrow legal gaps at both integer phases of the central target band.
    for x in range(math.floor(center['x'])-4,math.ceil(center['x'])+5):
        for y in range(math.floor(center['y'])-8,math.ceil(center['y'])+9):add(x,y)
    rows=list(result.values());corridor=sorted((q for q in rows if q['depth']<30),key=lambda q:(q['depth'],q['center_distance'],_point(q['position'])))
    front=sorted((q for q in rows if q['depth']>=30),key=lambda q:(-q['coverage'],q['center_distance'],_point(q['position'])))
    # Preserve approach diversity instead of spending the entire cap on enemy-centre cells.
    chosen=corridor[:96]+front[:maximum-96]
    return chosen,center


def wave_graph(positions,rears,targets,*,radius=16,gun_range=18):
    n=len(positions);edges={i:[j for j in range(n)if i!=j and _distance(positions[i],positions[j])<=radius]for i in range(n)}
    coverage=[sum(_distance(p,t['position'])<=gun_range for p in positions)for t in targets]
    if any(len(v)<2 for v in edges.values())or any(v<2 for v in coverage):return False
    for removed in range(-1,n):
        for rear in rears[:2]:
            seen={i for i,p in enumerate(positions)if i!=removed and _distance(p,rear['position'])<=radius};queue=list(seen)
            for i in queue:
                for j in edges[i]:
                    if j!=removed and j not in seen:seen.add(j);queue.append(j)
            if len(seen)!=n-(removed>=0):return False
    return True


def choose_wave(candidates,rears,targets,*,count=POST_COUNT):
    """Two short approach pairs plus one concentrated target-covering firing group."""
    bridges=[q for q in candidates if 8<=q['depth']<=18 and all(_distance(q['position'],r['position'])<=16 for r in rears[:2])]
    bridges.sort(key=lambda q:(-q['depth'],q['center_distance'],_point(q['position'])))
    attempted=0
    for pair in combinations(bridges[:20],2):
        if attempted>=96:break
        if not _separate(pair[0]['position'],pair[1]['position']):continue
        attempted+=1;chosen=list(pair)
        for layer in('middle','middle','front','front','front','front','front','front'):
            pool=[]
            for q in candidates:
                if q in chosen or not all(_separate(q['position'],p['position'])for p in chosen):continue
                if layer=='middle'and not 19<=q['depth']<=31:continue
                if layer=='front'and q['depth']<29:continue
                if sum(_distance(q['position'],p['position'])<=16 for p in chosen)<2:continue
                current=[sum(_distance(p['position'],t['position'])<=18 for p in chosen)for t in targets]
                gain=sum(max(0,3-a)-max(0,3-a-(_distance(q['position'],t['position'])<=18))for a,t in zip(current,targets))
                score=(gain,q['coverage'],-q['center_distance'],q['depth'])if layer=='front'else(q['depth'],gain,q['coverage'],-q['center_distance'])
                pool.append((score,_point(q['position']),q))
            if not pool:break
            chosen.append(max(pool,key=lambda row:(row[0],row[1]))[2])
        if len(chosen)==count and wave_graph([q['position']for q in chosen],rears,targets):return chosen
    return None


class NestSweep(NestAssault):
    """One complete paid wave. The older paused NestAssault checkpoint is untouched."""
    def __init__(self,game,bootstrap,builder,catalog,perimeter,*,ammo_source=None):
        self.game,self.bootstrap,self.builder,self.catalog,self.perimeter=game,bootstrap,builder,catalog,perimeter
        self.ammo_source=ammo_source
        self.path=Path(game.cfg.runtime_dir)/'nest-sweep.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.threats=NearbyThreats(game,catalog)

    def _choose_wave(self,obs,report,targets,operation,*,fully_stocked_rears=True):
        eligible=[]
        for q in report['friendly_guns']:
            minimum=5 if fully_stocked_rears else 1
            tiers=q.get('ammo',{}).get('by_item',{});name='piercing-rounds-magazine'if tiers.get('piercing-rounds-magazine',{}).get('full_normal_magazines',0)>=minimum else'firearm-magazine'
            full=tiers.get(name,{}).get('full_normal_magazines',q.get('ammo',{}).get('full_normal_magazines',0))
            if q.get('health',0)>=q.get('max_health',1)*.5 and full>=(5 if fully_stocked_rears else 1):eligible.append(dict(q,ammo_item=name))
        center={k:sum(t['position'][k]for t in targets)/len(targets)for k in('x','y')}
        rears=sorted(eligible,key=lambda q:(_distance(q['position'],center),q['unit_number']))[:2]
        if len(rears)<2:return {'ok':False,'reason':'sweep_two_current_armed_rear_anchors_required'}
        raw,center=wave_candidates(targets,rears)
        protected=protected_factory_areas(self.game,obs['world_id'],self.catalog.fingerprint)
        protected += [q['area']for q in mining_reservations(self.game,obs['world_id'])]
        raw=[q for q in raw if not any(overlap(_box(entity('gun-turret',**q['position'])),r)for r in protected)]
        candidates=[dict(q,entities=[entity('gun-turret',**q['position'])])for q in raw]
        surveyed=self.game.query('local x=helpers.json_to_table('+_payload({'world_id':obs['world_id'],'candidates':candidates})+');'+CANDIDATE_SURVEY_LUA)
        if not surveyed.get('ok'):return surveyed
        legal=[raw[i-1]for i in surveyed.get('legal',[])if type(i)is int and 1<=i<=len(raw)]
        whole=None;selected=None
        for attempt in range(3):
            selected=choose_wave(legal,rears,targets)
            if not selected:break
            es=[entity('gun-turret',**q['position'])for q in selected];whole=preflight(self.game,self.builder,obs,es)
            if whole.get('ok'):break
            collision=whole.get('position')
            if whole.get('reason')!='block_protected_entity_obstruction'or not isinstance(collision,dict):break
            # Roving enemies can enter a formerly clear footprint between read-only queries.
            # Choose another complete formation; no entity is moved or removed.
            legal=[q for q in legal if _distance(q['position'],collision)>3]
        if not selected:return {'ok':False,'reason':'no_concentrated_ten_gun_wave_with_native_land_and_whole_support',
            'surveyed_candidates':len(raw),'native_legal_candidates':len(legal),'legal_sample':[q['position']for q in legal[:20]],
            'rear_witnesses':[{k:q[k]for k in('unit_number','position','ammo')}for q in rears],'target_center':center,
            'target_units':[q['unit_number']for q in targets]}
        if not whole or not whole.get('ok'):return {'ok':False,'reason':'sweep_whole_native_preflight_failed','evidence':whole,
            'positions':[q['position']for q in selected]}
        area=[[min(q['position'][k]for q in selected)-1 for k in('x','y')],[max(q['position'][k]for q in selected)+1 for k in('x','y')]]
        scope_id='sweep:'+operation[:18];posts=[dict(name=scope_id+':'+str(i),position=q['position'],coverage_scope=scope_id,
            local_walk_access=False,commissioned=False,ammo_item=AMMO_ITEM)for i,q in enumerate(selected)]
        names=[p['name']for p in posts];scope=dict(areas=[area],cells=[],effective_range=16,native_range=18,
            covering_posts=names,new_posts=names,commissioned=False,access_scope='assisted normal paid whole-wave deployment; actor remains at rear')
        wave=dict(phase='deploy',scope_id=scope_id,block='defense_'+scope_id,plan=_plan(es,[],reserved_area=area,
            clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy='single-concentrated-projectile-wave-v2'),
            posts=posts,scope=scope,hotspot=center,bindings={},deployment_ids={},ammo_item=AMMO_ITEM,
            backstop_specs=[{k:deepcopy(q[k])for k in('name','unit_number','position','ammo_item')}for q in rears],
            target_coverage={str(t['unit_number']):sum(_distance(q['position'],t['position'])<=18 for q in selected)for t in targets})
        wave['perimeter']=dict(world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,posts=deepcopy(posts),coverage_scopes={scope_id:deepcopy(scope)})
        return {'ok':True,'wave':wave,'surveyed_candidates':len(raw),'native_legal_candidates':len(legal)}

    def prepare(self,obs):
        if self.state:return deepcopy(self.state)if self._identity(obs)else{'ok':False,'reason':'sweep_world_catalog_or_actor_changed'}
        if getattr(self.game,'backend',None)!='assisted':return {'ok':False,'reason':'sweep_requires_normal_assisted_execution'}
        report=self.threats.survey(obs)
        if not report.get('ok'):return report
        if report.get('gun',{}).get('range')!=18:return {'ok':False,'reason':'sweep_native_gun_range_requires_geometry_review'}
        if not report.get('recipes_enabled',{}).get(AMMO_ITEM):return {'ok':False,'reason':'sweep_native_piercing_recipe_required'}
        if report.get('truncated'):return {'ok':False,'reason':'sweep_complete_charted_threat_survey_required'}
        close=[q for q in report['targets']if q.get('dangerously_close')and q['role']in('worm','spawner')]
        if not close:return {'ok':False,'reason':'sweep_no_close_stationary_nest'}
        primary=min(close,key=lambda q:(q['factory_distance'],q['unit_number']))
        cluster=[q for q in report['targets']if q['role']in('worm','spawner')and _distance(q['position'],primary['position'])<=48]
        target_source={'kind':'fresh_charted_stationary_cluster'}
        prior_path=self.path.parent/'nest-assault.json'
        if prior_path.exists():
            try:prior=json.loads(prior_path.read_text(encoding='utf-8'))
            except(ValueError,OSError):prior={}
            witnessed={q['unit_number']:q for q in report['targets']}
            expected=prior.get('targets',[])
            compatible=(prior.get('world_id')==obs['world_id']and prior.get('catalog_fingerprint')==self.catalog.fingerprint
                and prior.get('actor_unit')==obs['actor_unit_number']and not prior.get('started_tick')
                and prior.get('phase')in('stock','deploy','register')and 1<=len(expected)<=12
                and prior.get('target_set_hash')==_hash(expected))
            if compatible:
                for q in expected:
                    actual=witnessed.get(q['unit_number'])
                    if not actual or any(actual.get(k)!=q.get(k)for k in('name','position','world_id','surface_index','force_index')):compatible=False;break
            if compatible:
                cluster=[witnessed[q['unit_number']]for q in expected]
                target_source={'kind':'compatible_unlaunched_prior_intent','operation_id':prior['id'],'target_set_hash':prior['target_set_hash']}
            else:target_source['prior_intent_ignored']='not compatible with current world/catalog/actor/native target identities or already launched'
        if not 1<=len(cluster)<=12:return {'ok':False,'reason':'sweep_twelve_target_limit'}
        if any(q['role']=='worm'and q.get('combat',{}).get('range',math.inf)>25 for q in cluster):return {'ok':False,'reason':'sweep_reviewed_worm_range_exceeded'}
        targets=[{**{k:deepcopy(q[k])for k in('name','unit_number','position','world_id','surface_index','force_index')},
            'reviewed_range':q.get('combat',{}).get('range')}for q in sorted(cluster,key=lambda q:q['unit_number'])]
        operation=_hash(['single-sweep',obs['world_id'],self.catalog.fingerprint,targets,report['tick']]);selected=self._choose_wave(obs,report,targets,operation,fully_stocked_rears=False)
        if not selected.get('ok'):return selected
        return dict(ok=True,id=operation,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,
            actor_unit=obs['actor_unit_number'],phase='stock',targets=targets,target_set_hash=_hash(targets),wave=selected['wave'],
            ammo_item=AMMO_ITEM,requirements={'gun-turret':POST_COUNT,AMMO_ITEM:STOCK_MAGAZINES},
            force_profile=report['force_modifiers'],prepared_tick=report['tick'],handcrafted_magazines_issued=0,
            target_source=target_source,
            setup_crafting_outside_combat_clock=True,maximum_waves=1,post_count=POST_COUNT,
            preparation_evidence={k:selected[k]for k in('surveyed_candidates','native_legal_candidates')})

    def inspect(self,obs):
        if not self._identity(obs):return {'ok':False,'reason':'sweep_world_catalog_or_actor_changed'}
        wave=self.state['wave'];posts=[dict(p,deployment_id=wave['deployment_ids'].get(p['name']))for p in wave['posts']]
        args={k:self.state[k]for k in('world_id','catalog_fingerprint','actor_unit','targets','target_set_hash','ammo_item')}
        args.update(operation_id=self.state['id'],posts=posts)
        r=self.game.query('local x=helpers.json_to_table('+_payload(args)+');'+SWEEP_INSPECT_LUA)
        if not isinstance(r,dict)or not r.get('ok'):return r if isinstance(r,dict)else{'ok':False,'reason':'sweep_native_inspection_invalid'}
        if(r.get('world_id')!=self.state['world_id']or r.get('actor_unit')!=self.state['actor_unit']or type(r.get('tick'))is not int
                or not isinstance(r.get('targets'),list)or len(r['targets'])!=len(self.state['targets'])
                or {q.get('unit_number')for q in r['targets']}!={q['unit_number']for q in self.state['targets']}
                or not isinstance(r.get('posts'),list)or len(r['posts'])!=POST_COUNT
                or {q.get('name')for q in r['posts']}!={q['name']for q in posts}):
            return {'ok':False,'reason':'sweep_native_inspection_invalid'}
        return r

    def _stock_wave(self,obs,native):
        if native['main_gun_turrets']<POST_COUNT:return self.bootstrap.ensure_item(obs,'gun-turret',POST_COUNT)
        held=native['main_ammo']['full_normal_magazines']
        if obs.get('crafting_queue'):return {'status':'waiting','reason':'sweep_waiting_normally_paid_crafting_queue'}
        if held>=STOCK_MAGAZINES:return None
        source=self.ammo_source
        evidence={'required_capability':'piercing_ammo','item':AMMO_ITEM,'required_full_stock':STOCK_MAGAZINES,
                  'held_full_stock':held,'missing_full_stock':STOCK_MAGAZINES-held,'production_policy':'factory_only_ammunition'}
        if source is None or not source.commissioned:
            return {'status':'waiting','reason':'sweep_commissioned_piercing_ammo_factory_required',**evidence}
        action=source.ensure_full_magazines(obs,STOCK_MAGAZINES)
        if action is None:return {'status':'waiting','reason':'sweep_reobserve_normally_assembled_factory_stock',**evidence}
        if action.get('type'):
            if(action['type']!='take_factory_magazines'or action.get('item')!=AMMO_ITEM
                    or type(action.get('count'))is not int or not 1<=action['count']<=50):
                return {'status':'blocked','reason':'sweep_factory_provider_must_supply_guarded_full_piercing_magazines',**evidence}
            return action
        return {**action,**evidence}

    def _start_action(self,obs):
        return dict(type='start_nest_sweep',operation_id=self.state['id'],target_set_hash=self.state['target_set_hash'],
            expected_world_id=obs['world_id'],expected_actor_unit=obs['actor_unit_number'],reason='start one funded bounded native projectile wave journal')

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs):return {'status':'blocked','reason':'sweep_world_catalog_or_actor_changed'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:return {'status':'blocked','reason':'sweep_checkpoint_changed'}
        pending=self._reconcile_craft()
        if pending:return pending
        native=self.inspect(obs)
        if not native.get('ok'):return {'status':'blocked',**native}
        proof=self._sweep_proof(native)
        if proof:self.state.update(phase='verified',verification=proof);self._save();return {'status':'succeeded','evidence':proof}
        if native.get('native_started'):
            self.state.update(started_tick=native['started_tick'],deadline_tick=native['deadline_tick']);self._save()
            if not native['listener_owned']:
                if native.get('listener_state')=='foreign':return {'status':'blocked','reason':'sweep_existing_native_handler_cannot_be_overwritten'}
                return self._start_action(obs)
            if native['tick']>native['deadline_tick']:
                self.state['phase']='stopped';self._save();return {'status':'blocked','reason':'sweep_finite_combat_deadline','evidence':native}
        wave=self.state['wave']
        if native.get('wave_group_id'):
            if(native.get('group_phase')!='complete'or native.get('group_paid_turrets')!=POST_COUNT
                    or native.get('group_loaded_magazines')!=STOCK_MAGAZINES or native.get('group_loaded_rounds')!=STOCK_MAGAZINES*10
                    or native.get('group_same_tick')is not True):
                return {'status':'blocked','reason':'sweep_incomplete_native_group_requires_exact_reconciliation','evidence':native}
            for p in native['posts']:
                if not p.get('paid'):return {'status':'blocked','reason':'sweep_complete_group_missing_paid_post_receipt','evidence':p}
                unit=p.get('unit_number')or p.get('marker_unit')
                if not p.get('present')and not native.get('lost_posts',{}).get(str(unit)):
                    return {'status':'blocked','reason':'sweep_post_absent_without_native_loss_record','evidence':p}
                wave['bindings'][p['name']]=unit
            self.state['phase']='fight';wave['phase']='fight';self._save()
            alive=[p for p in native['posts']if p.get('present')]
            if not alive:return {'status':'blocked','reason':'sweep_single_wave_lost_no_forward_rebuild','evidence':native}
            if not any(p.get('ammo',{}).get('normal_rounds',0)>0 for p in alive):
                return {'status':'blocked','reason':'sweep_prepaid_rounds_exhausted_no_unbounded_refill','evidence':native}
            if all(not q['present']for q in native['targets']):return {'status':'blocked','reason':'sweep_target_death_attribution_incomplete','evidence':native}
            return {'status':'waiting','reason':'one_concentrated_paid_wave_firing_native_projectiles','evidence':native}
        stock=self._stock_wave(obs,native)
        if stock:return stock
        if not self.state.get('funded_geometry_refresh_tick'):
            report=self.threats.survey(obs)
            if not report.get('ok'):return {'status':'blocked',**report}
            selected=self._choose_wave(obs,report,self.state['targets'],self.state['id'])
            if not selected.get('ok'):return {'status':'waiting',**selected}
            self.state['wave']=selected['wave'];self.state['force_profile']=report['force_modifiers']
            self.state['funded_geometry_refresh_tick']=report['tick'];self._save()
            return {'status':'waiting','reason':'entire_wave_funded_and_rebound_to_current_native_geometry'}
        clear=self.builder.ensure_plan(obs,_plan([],[],clear_obstacles=wave['plan']['clear_obstacles'],clearance_world_id=obs['world_id']))
        if clear.get('status')!='succeeded':return clear
        whole=preflight(self.game,self.builder,obs,wave['plan']['entities'])
        if not whole.get('ok'):
            status='waiting'if whole.get('entity')in('small-biter','medium-biter','big-biter','small-spitter','medium-spitter','big-spitter')else'blocked'
            return {'status':status,'reason':'sweep_waiting_ordinary_clear_footprints_before_group_payment','evidence':whole}
        registered=self._register(obs,wave)
        if registered:return registered
        if not native.get('native_started'):return self._start_action(obs)
        if 'underfire_policy'not in wave:
            wave['underfire_policy']=dict(mode='committed_projectile_wave_v2',operation_id=self.state['id'],post_count=POST_COUNT,
                ammo_item=AMMO_ITEM,targets=deepcopy(self.state['targets']),target_set_hash=self.state['target_set_hash'],
                started_tick=native['started_tick'],deadline_tick=native['deadline_tick'],max_enemy_range=25,coverage_margin=2,
                min_planned_neighbors=2,min_target_coverage=2,min_backstop_count=2,min_backstop_health_fraction=.5,min_backstop_full_mags=5,
                planned_posts=deepcopy(wave['posts']),backstop_specs=wave['backstop_specs'],force_profile=self.state['force_profile'])
        self.state['phase']='deploy';self._save()
        from .defense_deployment import bastion_group_deployment_action
        action=bastion_group_deployment_action(self.game,obs,owner_kind='nest_sweep')
        wave['group_id']=action['group_id']
        for q in action['deployments']:wave['deployment_ids'][q['post_name']]=q['deployment_id']
        self._save();return action

    def _sweep_proof(self,native):
        if not native.get('native_started')or not native.get('listener_owned'):return None
        if(not native.get('wave_group_id')or native.get('group_phase')!='complete'
                or native.get('group_paid_turrets')!=POST_COUNT or native.get('group_loaded_magazines')!=STOCK_MAGAZINES
                or native.get('group_loaded_rounds')!=STOCK_MAGAZINES*10 or native.get('group_same_tick')is not True):return None
        if(len(native.get('targets',[]))!=len(self.state['targets'])or
                {q.get('unit_number')for q in native['targets']}!={q['unit_number']for q in self.state['targets']}):return None
        deaths=native.get('deaths')or{}
        if any(q.get('present')or not deaths.get(str(q['unit_number']),{}).get('own_gun_projectile')for q in native['targets']):return None
        return dict(tick=native['tick'],world_id=self.state['world_id'],target_set_hash=self.state['target_set_hash'],
            targets_cleared=len(self.state['targets']),native_projectile_deaths=[deaths[str(q['unit_number'])]for q in native['targets']],
            single_wave=True,group_id=native.get('wave_group_id'),post_losses=native.get('loss_count',0),
            surviving_posts=[p for p in native['posts']if p.get('present')],ammo_item=AMMO_ITEM,
            scope='exact original nest identities died to actual native friendly-gun physical projectiles; no whole-map safety claim')

    def proof(self,obs):
        native=self.inspect(obs)if self.state else{}
        return self._sweep_proof(native)if native.get('ok')else None
