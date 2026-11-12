"""Normally prepaid relocation of conflicting new engine coverage posts."""
from __future__ import annotations

from copy import copy,deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .defense_coverage import coverage_report,native_candidate_survey
from .defense_perimeter import AMMO_TARGET,protected_factory_areas
from .engine_production import SCOPE_ID,engine_core
from .planning import overlap,preflight
from .smelting import merge_entities
from .transport import VECTORS,surface_pipe_proof,xy


def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _payload(value):return json.dumps(json.dumps(value,separators=(',',':')))
def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _box(p):return [[p['x']-1,p['y']-1],[p['x']+1,p['y']+1]]
def _identity(obs,state,catalog):return(obs.get('ok',True)and state.get('world_id')==obs.get('world_id')
    and bool(obs.get('world_id'))and state.get('catalog_fingerprint')==catalog.fingerprint)
def _engine_guard(state):return _hash({k:state.get(k)for k in('plan','core','parameters',
    'iron_source','steel_source','steel_plan_revision','steel_furnace_units')})


def _referencing_scopes(perimeter,name):
    return {key:value for key,value in perimeter.get('coverage_scopes',{}).items()
            if name in set(value.get('covering_posts',[]))|set(value.get('new_posts',[]))}


def _coverage_guards(perimeter,name):
    by={p['name']:p for p in perimeter['posts']};result=[]
    for key,scope in _referencing_scopes(perimeter,name).items():
        names=sorted(set(scope['covering_posts'])|set(scope['new_posts']))
        result.append({'name':key,'cells':scope['cells'],'effective_range':scope['effective_range'],
            'posts':[{k:by[n][k]for k in('name','position','unit_number')}for n in names]})
    return result


def _perimeter_matches(state,perimeter):
    expected={p['name']:p for p in state['initial_posts']}
    current={p['name']:p for p in perimeter.get('posts',[])}
    if len(current)!=len(perimeter.get('posts',[]))or set(current)!=set(expected):return False
    for entry in state['entries']:
        if entry['phase']in('retire','done'):expected[entry['name']]=entry['new']
        elif entry['phase']=='publish'and current[entry['name']]==entry['new']:expected[entry['name']]=entry['new']
    if current!=expected:return False
    for entry in state['entries']:
        refs=_referencing_scopes(perimeter,entry['name'])
        if set(refs)!=set(entry['scope_revisions'])or any(_hash(refs[k])!=v for k,v in entry['scope_revisions'].items()):return False
    return True


def validate_relocation_action(action,kind,world=None):
    required={'type','transaction_id','post_name','expected_old_unit','expected_actor_unit','expected_world_id'}
    extra={'position'}if kind=='build_defense_replacement'else{'expected_new_unit'}
    if(not isinstance(action,dict)or set(action)-required-extra-{'reason'}or not required<=set(action)
            or action.get('type')!=kind or kind not in('build_defense_replacement','retire_defense_post')
            or any(type(action.get(k))is not int or action[k]<1 for k in('expected_old_unit','expected_actor_unit'))
            or any(not isinstance(action.get(k),str)or not action[k]or len(action[k])>180
                   for k in('transaction_id','post_name','expected_world_id'))
            or('reason'in action and not isinstance(action['reason'],str))):
        raise ValueError('defense relocation requires exact transaction, post, actor and world identities')
    if world is not None and action['expected_world_id']!=world:raise ValueError('defense relocation must target the bound world')
    if kind=='build_defense_replacement':
        p=action.get('position')
        if(not isinstance(p,dict)or set(p)!={'x','y'}or any(type(v)not in(int,float)or not math.isfinite(v)or v%1 for v in p.values())):
            raise ValueError('replacement turret requires an exact finite integer-center position')
    elif type(action.get('expected_new_unit'))is not int or action['expected_new_unit']<1:
        raise ValueError('retirement requires the exact paid replacement unit')


def _action_context(game,action,kind):
    validate_relocation_action(action,kind,getattr(game,'expected_world',None))
    if getattr(game,'expected_world',None)!=action['expected_world_id']:raise ValueError('defense relocation requires a bound game world')
    if getattr(game,'backend',None)!='assisted':raise ValueError('guarded post relocation requires the ordinary assisted backend')
    root=Path(game.cfg.runtime_dir);state=json.loads((root/'defense-relocation.json').read_text(encoding='utf-8'))
    engine=json.loads((root/'engine-production.json').read_text(encoding='utf-8'))
    perimeter=json.loads((root/'defense-perimeter.json').read_text(encoding='utf-8'))
    matches=[e for e in state.get('entries',[])if e.get('name')==action['post_name']]
    if(state.get('id')!=action['transaction_id']or state.get('world_id')!=action['expected_world_id']or len(matches)!=1
            or _engine_guard(engine)!=state.get('engine_guard')or engine.get('phase')!='build'or engine.get('bindings')!=state.get('shared_bindings')
            or any(v.get('world_id')!=state['world_id']or v.get('catalog_fingerprint')!=state['catalog_fingerprint']for v in(engine,perimeter))):
        raise ValueError('defense action is restricted to the current unchanged engine prebuild transaction')
    entry=matches[0];expected_phase='build'if kind=='build_defense_replacement'else'retire'
    if entry.get('phase')!=expected_phase or entry['old']['unit_number']!=action['expected_old_unit']:
        raise ValueError('defense action must match the exact persisted phase and old unit')
    if not _perimeter_matches(state,perimeter):raise ValueError('known defense post or referenced scope metadata changed')
    current=[p for p in perimeter['posts']if p['name']==entry['name']]
    wanted=entry['old']if kind=='build_defense_replacement'else entry['new']
    if len(current)!=1 or current[0]!=wanted:raise ValueError('logical defense post publication identity changed')
    for name,revision in entry['scope_revisions'].items():
        if _hash(perimeter.get('coverage_scopes',{}).get(name))!=revision:raise ValueError('referencing defense scope changed')
    marker=state['id']+':'+entry['name'];payload=dict(action,marker=marker,old=entry['old'],new=entry['new'],
        sources=[dict(engine[k],name='transport-belt')for k in('iron_source','steel_source')],
        consumer_specs=[{k:e[k]for k in('name','position')}for e in engine['plan']['entities']if _key(e)not in state['shared_bindings']])
    if kind=='build_defense_replacement':
        if action['position']!=entry['new']['position']:raise ValueError('paid replacement site changed')
        protected=protected_factory_areas(game,state['world_id'],state['catalog_fingerprint'])
        if any(overlap(_box(action['position']),area)for area in protected):raise ValueError('replacement site overlaps protected production or port access')
        payload['protected']=protected
    else:
        if entry['new'].get('unit_number')!=action['expected_new_unit']:raise ValueError('paid replacement identity changed')
        if entry.get('publication')!={'old_revision':_hash(entry['old']),'new_revision':_hash(entry['new'])}:
            raise ValueError('persisted replacement publication intent required before retirement')
        payload['coverage']=_coverage_guards(perimeter,entry['name'])
        if not payload['coverage']:raise ValueError('referencing scope proof is required before retirement')
    return payload


POST_GUARD_LUA=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if not d or d.world_id~=x.expected_world_id then return reject("defense_relocation_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.expected_actor_unit then return reject("defense_relocation_actor_changed")end
if prototypes.entity["gun-turret"].tile_width~=2 or prototypes.entity["gun-turret"].tile_height~=2 then return reject("defense_turret_footprint_changed")end
for _,q in ipairs(x.sources or{})do local e=target(q.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x
  or e.position.y~=q.position.y or e.direction~=q.facing then return reject("defense_relocation_engine_source_changed")end
end
for _,q in ipairs(x.consumer_specs or{})do if target(q.position,q.name)then return reject("engine_consumer_construction_already_started")end end
local function exact(q)
 local e=target(q.position,"gun-turret")
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"then return nil end
 return e end
local ammo_proto=prototypes.item["firearm-magazine"];local size=ammo_proto and ammo_proto.magazine_size
if not ammo_proto or ammo_proto.type~="ammo"or not size or size<=0 then return reject("defense_magazine_prototype_missing")end
local function plain(stack,name)
 return stack.valid_for_read and stack.name==name and stack.quality.name=="normal"and stack.health==1
  and stack.spoil_tick==0 and not stack.is_item_with_tags and not stack.is_item_with_inventory and not stack.is_item_with_entity_data end
local function ammo(e,full_only)
 local inv=e.get_inventory(defines.inventory.turret_ammo);if not inv or not inv.valid then return nil end
 local count,full=0,0
 for i=1,#inv do local stack=inv[i];if stack.valid_for_read then
  if not plain(stack,"firearm-magazine")or stack.prototype.type~="ammo"or stack.ammo<1 or stack.ammo>size
   or stack.ammo%1~=0 or(full_only and stack.ammo~=size)then return nil end
  count=count+stack.count;full=full+stack.count-(stack.ammo<size and 1 or 0)
 end end
 return {count=count,full=full}end
local main=a.get_main_inventory();if not main or not main.valid then return reject("defense_actor_inventory_missing")end
local marker=d.defense_relocations and d.defense_relocations[x.marker]or nil
if marker and(marker.old_unit~=x.old.unit_number or marker.old_position.x~=x.old.position.x
 or marker.old_position.y~=x.old.position.y or marker.position.x~=x.new.position.x or marker.position.y~=x.new.position.y)then
 return reject("defense_relocation_receipt_identity_changed")end
'''


BUILD_REPLACEMENT_LUA=POST_GUARD_LUA+r'''
if marker then
 local e=exact({position=marker.position,unit_number=marker.unit_number})
 if not e then return reject("paid_replacement_missing_or_replaced")end
 return {ok=true,status="succeeded",unit_number=e.unit_number,reused_receipt=true}end
if not exact(x.old)then return reject("old_defense_post_identity_changed")end
if target(x.new.position,"gun-turret")then return reject("unexpected_replacement_post_before_paid_build")end
local p=x.new.position;local area={{p.x-1,p.y-1},{p.x+1,p.y+1}}
for _,r in ipairs(x.protected)do if area[1][1]<r[2][1]and area[2][1]>r[1][1]and area[1][2]<r[2][2]and area[2][2]>r[1][2]then return reject("replacement_site_protected")end end
if s.count_entities_filtered{area=area,type="resource"}>0 or not s.can_place_entity{name="gun-turret",position=p,direction=0,force=f}then return reject("replacement_site_changed")end
local slot=nil
for i=1,#main do local stack=main[i];if plain(stack,"gun-turret")and stack.prototype.type=="item"then slot=i;break end end
if not slot then return reject("paid_normal_turret_missing")end
local before=main.get_item_count{name="gun-turret",quality="normal"};local stack=main[slot];stack.count=stack.count-1
local e=s.create_entity{name="gun-turret",position=p,direction=0,force=f,quality="normal",raise_built=true,player=a.player}
if not e then main.insert{name="gun-turret",quality="normal",count=1};return reject("normal_turret_build_failed_refunded")end
if main.get_item_count{name="gun-turret",quality="normal"}~=before-1 then return reject("paid_turret_count_conservation_failed")end
d.defense_relocations=d.defense_relocations or{}
d.defense_relocations[x.marker]={old_unit=x.old.unit_number,old_position=x.old.position,unit_number=e.unit_number,position=p,paid_turrets=1,actor_unit=a.unit_number}
return {ok=true,status="succeeded",unit_number=e.unit_number,paid_turrets=1}
'''


RETIRE_POST_LUA=POST_GUARD_LUA+r'''
if not marker or marker.unit_number~=x.new.unit_number then return reject("paid_replacement_receipt_required")end
local replacement=exact(x.new);local loaded=replacement and ammo(replacement,false)
if not replacement or replacement.health~=replacement.max_health or not loaded or loaded.full<10 then return reject("replacement_requires_ten_full_normal_magazines")end
for _,scope in ipairs(x.coverage)do
 if scope.effective_range>prototypes.entity["gun-turret"].attack_parameters.range-2 then return reject("defense_native_range_changed")end
 local posts={}
 for _,q in ipairs(scope.posts)do local e=exact(q);local stock=e and ammo(e,false)
  if not e or e.health~=e.max_health or not stock or stock.full<5 then return reject("referencing_scope_post_unhealthy")end
  posts[#posts+1]=e end
 for _,cell in ipairs(scope.cells)do local covered=false
  for _,e in ipairs(posts)do local fits=true
   for _,p in ipairs(cell.corners)do if(e.position.x-p[1])^2+(e.position.y-p[2])^2>scope.effective_range^2+1e-9 then fits=false;break end end
   if fits then covered=true;break end
  end
  if not covered then return reject("referencing_scope_coverage_missing")end
 end
end
local old=target(x.old.position,"gun-turret")
if marker.retired then if old then return reject("retired_post_location_reoccupied")end;return {ok=true,status="succeeded",reused_receipt=true}end
old=exact(x.old);if not old then return reject("old_defense_post_identity_changed")end
local stock=ammo(old,true)
if old.health~=old.max_health or not stock then return reject("old_post_partial_or_nonplain_ammo_requires_explicit_handling")end
local empty=0
for i=1,#main do if not main[i].valid_for_read and(not main.supports_filters()or not main.get_filter(i))then empty=empty+1 end end
local required=1+math.ceil(stock.count/ammo_proto.stack_size)
if empty<required or main.get_insertable_count{name="gun-turret",quality="normal"}<1
 or main.get_insertable_count{name="firearm-magazine",quality="normal"}<stock.count then return reject("aggregate_post_recovery_inventory_capacity_missing")end
local function totals()
 local guns=main.get_item_count{name="gun-turret",quality="normal"};local count,rounds=0,0
 for i=1,#main do local st=main[i];if st.valid_for_read and st.name=="firearm-magazine"and st.quality.name=="normal"then
  count=count+st.count;rounds=rounds+(st.count-1)*size+st.ammo end end
 return guns,count,rounds end
local guns,count,rounds=totals()
if not a.mine_entity(old)then return reject("ordinary_defense_post_mining_failed")end
local guns2,count2,rounds2=totals()
if target(x.old.position,"gun-turret")or guns2~=guns+1 or count2~=count+stock.count or rounds2~=rounds+stock.count*size then return reject("post_recovery_count_or_round_conservation_failed")end
marker.retired=true;marker.recovered_magazines=stock.count;marker.recovered_rounds=stock.count*size
return {ok=true,status="succeeded",recovered_turrets=1,recovered_magazines=stock.count,recovered_rounds=stock.count*size}
'''


def execute_build_replacement(game,action):
    payload=_action_context(game,action,'build_defense_replacement')
    return game.query('local x=helpers.json_to_table('+_payload(payload)+');'+BUILD_REPLACEMENT_LUA)


def execute_retire_post(game,action):
    payload=_action_context(game,action,'retire_defense_post')
    return game.query('local x=helpers.json_to_table('+_payload(payload)+');'+RETIRE_POST_LUA)


class DefenseRelocation:
    def __init__(self,game,bootstrap,builder,catalog,perimeter,engines):
        self.game,self.bootstrap,self.builder,self.catalog,self.perimeter,self.engines=game,bootstrap,builder,catalog,perimeter,engines
        self.path=Path(game.cfg.runtime_dir)/'defense-relocation.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}

    def _save(self):_atomic_json(self.path,self.state)

    @property
    def active(self):return bool(self.state and self.state.get('phase')!='verified')

    def _engine_ready(self,obs,*,allow_constructed=False):
        s=self.engines.state
        if not _identity(obs,s,self.catalog)or(s.get('phase')!='build'and not allow_constructed):
            return {'ok':False,'reason':'defense_relocation_requires_same_engine_prebuild'}
        if self.state and _engine_guard(s)!=self.state['engine_guard']:
            return {'ok':False,'reason':'defense_relocation_engine_plan_or_source_changed'}
        shared={_key(dict(s[k],name='transport-belt')):s[k]['unit_number']for k in('iron_source','steel_source')}
        if not allow_constructed and s.get('bindings')!=shared:
            return {'ok':False,'reason':'defense_relocation_consumer_bindings_already_present'}
        actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])}
        for spec in s['plan']['entities']:
            if not allow_constructed and _key(spec)not in shared and(spec['name'],*xy(spec))in actual:
                return {'ok':False,'reason':'defense_relocation_consumer_asset_already_built','entity':spec}
        for name in('iron_source','steel_source'):
            q=s[name];live=actual.get(('transport-belt',*xy(q)))
            if not live or live.get('unit_number')!=q['unit_number']or live.get('direction')!=q['facing']:
                return {'ok':False,'reason':'defense_relocation_fixed_engine_source_changed','source':name}
        return {'ok':True,'bindings':shared}

    def prepare(self,obs):
        """Read-only finite replacement selection; never alters posts or plans."""
        if self.state:
            if not _identity(obs,self.state,self.catalog):return {'ok':False,'reason':'defense_relocation_world_or_catalog_changed'}
            return deepcopy(self.state)if _engine_guard(self.engines.state)==self.state['engine_guard']else{'ok':False,'reason':'defense_relocation_engine_plan_or_source_changed'}
        ready=self._engine_ready(obs)
        if not ready.get('ok'):return ready
        if self.perimeter._identity_error(obs):return {'ok':False,'reason':'defense_relocation_perimeter_identity_changed'}
        report=self.perimeter.inspect(obs)
        if not self.perimeter._proof(report):return {'ok':False,'reason':'healthy_existing_defense_required_before_relocation'}
        scope=self.perimeter.state.get('coverage_scopes',{}).get(SCOPE_ID)
        if not scope or not scope.get('commissioned')or scope['areas']!=[self.engines.state['core']['reserved_area']]:
            return {'ok':False,'reason':'commissioned_exact_engine_coverage_scope_required'}
        try:
            protected=protected_factory_areas(self.game,obs['world_id'],self.catalog.fingerprint)
            engine=self.engines.state;geometry=self.builder._occupied_by_plan(engine['plan']['entities'])
            conflict_areas=[engine['core']['reserved_area']]+[[[x-.5,y-.5],[x+.5,y+.5]]for x,y in geometry]
            for p in engine['core']['ports']:
                dx,dy=VECTORS[p['facing']]
                if p['direction']=='input':dx,dy=-dx,-dy
                conflict_areas += [[[p['position']['x']+i*dx-.5,p['position']['y']+i*dy-.5],
                                    [p['position']['x']+i*dx+.5,p['position']['y']+i*dy+.5]]for i in range(3)]
            actual={e['unit_number']:e for e in obs.get('entities',[])if e['name']=='gun-turret'}
            owned={p.get('unit_number'):p for p in self.perimeter.state['posts']}
            conflicts=[]
            for unit,e in actual.items():
                if not any(overlap(_box(e['position']),a)for a in conflict_areas):continue
                post=owned.get(unit)
                if(not post or post.get('coverage_scope')!=SCOPE_ID or post['name']not in scope['new_posts']
                        or post['position']!=e['position']or not post.get('commissioned')):
                    return {'ok':False,'reason':'conflicting_post_is_not_owned_new_engine_scope','unit_number':unit}
                conflicts.append(post)
            if len(conflicts)>4:return {'ok':False,'reason':'engine_relocation_conflict_budget_exceeded'}
            if not conflicts:return {'ok':False,'reason':'no_owned_new_engine_post_conflict'}
            conflicts=sorted(conflicts,key=lambda p:p['name'])
            areas=[engine['core']['reserved_area']];radius=self.perimeter.state['range']
            bounds=[[math.floor(areas[0][0][0]-radius-3),math.floor(areas[0][0][1]-radius-3)],
                    [math.ceil(areas[0][1][0]+radius+3),math.ceil(areas[0][1][1]+radius+3)]]
            local=[a for a in protected if overlap(a,bounds)]
            positions={ (int(p['position']['x'])+dx,int(p['position']['y'])+dy)
                for p in conflicts for dx in range(-8,9)for dy in range(-8,9)if 2<=math.hypot(dx,dy)<=8}
            candidates=[{'x':x,'y':y}for x,y in sorted(positions)if not any(overlap(_box({'x':x,'y':y}),a)for a in local)]
            native=native_candidate_survey(self.game,candidates,areas,local,bounds)
            if not native.get('ok')or native.get('range')!=radius or native.get('magazine_size')!=10:
                return {'ok':False,'reason':'native_defense_replacement_site_survey_failed','evidence':native}
            legal=native.get('candidates',[])
            if legal=={}:legal=[]
            posts=deepcopy(self.perimeter.state['posts']);entries=[]
            for old in conflicts:
                refs=_referencing_scopes(self.perimeter.state,old['name']);chosen=None
                if not refs:return {'ok':False,'reason':'engine_post_has_no_registered_coverage_reference'}
                for row in sorted(legal,key=lambda p:(math.dist(xy(p),xy(old)),p['position']['x'],p['position']['y'])):
                    p=row['position']
                    if math.dist(xy(row),xy(old))>8 or any(other['name']!=old['name']and math.dist(xy(row),xy(other))<3 for other in posts):continue
                    trial=[dict(post,position=p)if post['name']==old['name']else post for post in posts]
                    if all(coverage_report(s['cells'],[q for q in trial if q['name']in set(s['covering_posts'])|set(s['new_posts'])],s['effective_range'])['complete']for s in refs.values()):
                        chosen=p;posts=trial;break
                if chosen is None:return {'ok':False,'reason':'no_protected_free_replacement_preserves_all_referenced_scopes','post':old['name']}
                new=deepcopy(old);new.update(position=deepcopy(chosen));new.pop('unit_number',None)
                entries.append({'name':old['name'],'old':deepcopy(old),'new':new,'phase':'build',
                    'scope_revisions':{name:_hash(value)for name,value in refs.items()}})
            planned_posts=[{'name':'gun-turret','position':e['new']['position'],'direction':0,'_width':2,'_height':2}for e in entries]
            ignored=[e['old']['unit_number']for e in entries]
            x,y=xy({'position':engine['core']['anchor']});future=engine_core(self.catalog,x,y,4)
            whole=preflight(self.game,self.builder,obs,merge_entities(engine['plan']['entities'],planned_posts),ignored_units=ignored)
            if not whole.get('ok'):return {'ok':False,'reason':'replacement_post_and_engine_whole_preflight_failed','evidence':whole}
            future_check=preflight(self.game,self.builder,obs,merge_entities(engine['plan']['entities'],future['entities'],planned_posts),ignored_units=ignored)
            if not future_check.get('ok'):return {'ok':False,'reason':'replacement_post_and_engine_future_preflight_failed','evidence':future_check}
            pipes=surface_pipe_proof(self.game,obs,engine['plan'])
            if not pipes.get('ok'):return {'ok':False,'reason':'engine_pipe_preservation_proof_missing','evidence':pipes}
            return {'ok':True,'id':'engine-posts:'+obs['world_id']+':'+_hash(engine['plan'])[:12],
                'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'phase':'relocate',
                'engine_guard':_engine_guard(engine),'shared_bindings':ready['bindings'],'entries':entries,
                'initial_posts':deepcopy(self.perimeter.state['posts']),'prepared_tick':obs.get('tick'),
                'prospective_whole_preflight':True,'prospective_future_preflight':True,
                'requirements':{'gun-turret':len(entries),'firearm-magazine':AMMO_TARGET*len(entries)}}
        except(OSError,ValueError,KeyError,TypeError)as exc:
            return {'ok':False,'reason':'invalid_defense_relocation_authority','detail':str(exc)}

    def _probe(self,obs,entry):
        x={'expected_world_id':obs['world_id'],'expected_actor_unit':obs['actor_unit_number'],
            'marker':self.state['id']+':'+entry['name'],'old':entry['old'],'new':entry['new']}
        return self.game.query('local x=helpers.json_to_table('+_payload(x)+');'+POST_GUARD_LUA+r'''
local old=target(x.old.position,"gun-turret")
if old and not exact(x.old)then return reject("old_defense_post_identity_changed")end
local stock=old and ammo(old,true)
if old and(old.health~=old.max_health or not stock)then return reject("old_post_partial_or_nonplain_ammo_requires_explicit_handling")end
if not old and not(marker and marker.retired)then return reject("old_post_missing_without_retirement_receipt")end
local new=nil
if marker then
 new=exact({position=marker.position,unit_number=marker.unit_number})
 if not new or marker.paid_turrets~=1 then return reject("paid_replacement_missing_or_replaced")end
else if target(x.new.position,"gun-turret")then return reject("unexpected_replacement_post_before_paid_build")end end
local turrets,empty=0,0
for i=1,#main do local st=main[i]
 if plain(st,"gun-turret")and st.prototype.type=="item"then turrets=turrets+st.count end
 if not st.valid_for_read and(not main.supports_filters()or not main.get_filter(i))then empty=empty+1 end
end
return {ok=true,new_unit=new and new.unit_number or nil,retired=marker and marker.retired or false,
 old_present=old~=nil,old_magazines=stock and stock.count or 0,normal_turrets=turrets,empty_slots=empty}
''')

    def _virtual(self,obs,entry):
        virtual=copy(self.perimeter);virtual.state=deepcopy(self.perimeter.state)
        virtual.state['posts']=[deepcopy(entry['new'])if p['name']==entry['name']else p for p in virtual.state['posts']]
        report=virtual.inspect(obs);proofs={}
        if not report.get('ok'):return None,report
        for name in entry['scope_revisions']:
            proof=virtual._coverage_evidence(report,virtual.state['coverage_scopes'][name])
            if not proof:return None,report
            proofs[name]=proof
        return proofs,report

    def _publish(self,obs,entry):
        intent=entry.get('publication')
        if entry.get('phase')!='publish'or intent!={'old_revision':_hash(entry['old']),'new_revision':_hash(entry['new'])}:
            return {'status':'blocked','reason':'persisted_defense_publication_intent_required'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:
            return {'status':'blocked','reason':'defense_relocation_checkpoint_changed'}
        path=self.perimeter.path;disk=json.loads(path.read_text(encoding='utf-8'))
        if not _identity(obs,disk,self.catalog):return {'status':'blocked','reason':'defense_publication_world_or_catalog_changed'}
        if not _perimeter_matches(self.state,disk):return {'status':'blocked','reason':'known_defense_publication_identity_changed'}
        found=[p for p in disk['posts']if p['name']==entry['name']]
        if len(found)!=1 or found[0]not in(entry['old'],entry['new']):return {'status':'blocked','reason':'logical_defense_post_publication_changed'}
        if set(_referencing_scopes(disk,entry['name']))!=set(entry['scope_revisions'])or any(
                _hash(disk['coverage_scopes'][k])!=v for k,v in entry['scope_revisions'].items()):
            return {'status':'blocked','reason':'referencing_defense_scope_publication_changed'}
        proofs,report=self._virtual(obs,entry)
        if not proofs:return {'status':'blocked','reason':'replacement_all_scope_coverage_proof_missing','evidence':report}
        target=next(p for p in report['posts']if p['name']==entry['name'])
        if target.get('full_magazines',0)<AMMO_TARGET:return {'status':'blocked','reason':'replacement_requires_ten_full_normal_magazines'}
        if found[0]!=entry['new']:
            disk['posts']=[deepcopy(entry['new'])if p['name']==entry['name']else p for p in disk['posts']]
            _atomic_json(path,disk)
        self.perimeter.state=disk;entry.update(phase='retire',coverage_proof=proofs);self._save()
        return {'status':'waiting','reason':'paid_armed_replacement_published_before_old_post_recovery'}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;self._save()
        if not _identity(obs,self.state,self.catalog):return {'status':'blocked','reason':'defense_relocation_world_or_catalog_changed'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:
            return {'status':'blocked','reason':'defense_relocation_checkpoint_changed'}
        if self.state.get('phase')=='verified':
            proof=self.proof(obs);return {'status':'succeeded','evidence':proof}if proof else{'status':'blocked','reason':'verified_defense_relocation_proof_missing'}
        ready=self._engine_ready(obs)
        if not ready.get('ok'):return {'status':'blocked',**ready}
        disk=json.loads(self.perimeter.path.read_text(encoding='utf-8'))
        if not _identity(obs,disk,self.catalog)or not _perimeter_matches(self.state,disk):
            return {'status':'blocked','reason':'known_defense_post_or_scope_metadata_changed'}
        self.perimeter.state=disk
        entry=next((e for e in self.state['entries']if e['phase']!='done'),None)
        if entry is None:
            proof=self.proof(obs)
            if not proof:return {'status':'blocked','reason':'post_relocation_engine_future_or_coverage_preflight_failed'}
            self.state.update(phase='verified',proof=proof);self._save()
            return {'status':'succeeded','reason':'normally_paid_engine_defense_relocation_verified','evidence':proof}
        probe=self._probe(obs,entry)
        if not probe.get('ok'):return {'status':'blocked',**probe}
        action={'transaction_id':self.state['id'],'post_name':entry['name'],'expected_old_unit':entry['old']['unit_number'],
                'expected_actor_unit':obs['actor_unit_number'],'expected_world_id':obs['world_id']}
        if entry['phase']=='build':
            if probe.get('new_unit'):
                entry['new']['unit_number']=probe['new_unit'];entry['phase']='arm';self._save()
                return {'status':'waiting','reason':'paid_replacement_unit_bound_from_atomic_receipt'}
            if probe['normal_turrets']<1:
                return self.bootstrap.ensure_item({**obs,'inventory':{**obs.get('inventory',{}),'gun-turret':probe['normal_turrets']}},'gun-turret',1)
            report=self.perimeter.inspect(obs)
            if report.get('actor_full_magazines',0)<AMMO_TARGET:return self.perimeter._ensure_magazines(obs,report,AMMO_TARGET)
            return dict(action,type='build_defense_replacement',position=entry['new']['position'],reason='normally prepay replacement before relocating '+entry['name'])
        if probe.get('new_unit')!=entry['new'].get('unit_number'):return {'status':'blocked','reason':'paid_defense_replacement_binding_changed'}
        if entry['phase']=='arm':
            proofs,report=self._virtual(obs,entry)
            target=next((p for p in report.get('posts',[])if p['name']==entry['name']),{})
            needed=AMMO_TARGET-target.get('full_magazines',0)
            if needed>0:
                held=report.get('actor_full_magazines',0)
                if held<1:return self.perimeter._ensure_magazines(obs,report,needed)
                return {'type':'insert','name':'gun-turret','position':entry['new']['position'],'item':'firearm-magazine',
                    'inventory':'turret_ammo','count':min(held,needed),'expected_entity_unit':entry['new']['unit_number'],
                    'expected_entity_world_id':obs['world_id'],'reason':'load ten full normal magazines in paid defense replacement'}
            if not proofs:return {'status':'blocked','reason':'replacement_all_scope_coverage_proof_missing','evidence':report}
            entry['phase']='publish';entry['publication']={'old_revision':_hash(entry['old']),'new_revision':_hash(entry['new'])};self._save()
        if entry['phase']=='publish':return self._publish(obs,entry)
        if entry['phase']=='retire':
            if probe.get('retired')and not probe.get('old_present'):
                entry['phase']='done';self._save();return {'status':'waiting','reason':'old_post_recovered_from_exact_atomic_receipt'}
            proofs,_=self._virtual(obs,entry)
            if not proofs:return {'status':'blocked','reason':'replacement_all_scope_coverage_proof_missing'}
            required=1+math.ceil(probe['old_magazines']/self.catalog.items['firearm-magazine']['stack_size'])
            if probe['empty_slots']<required:return {'status':'blocked','reason':'aggregate_post_recovery_inventory_capacity_missing','required_empty_slots':required}
            return dict(action,type='retire_defense_post',expected_new_unit=entry['new']['unit_number'],reason='normally recover exact old post after paid armed replacement coverage')
        return {'status':'blocked','reason':'unknown_defense_relocation_phase'}

    def proof(self,obs):
        if not self.state or not _identity(obs,self.state,self.catalog)or any(e['phase']!='done'for e in self.state['entries']):return None
        ready=self._engine_ready(obs,allow_constructed=self.state.get('phase')=='verified')
        if not ready.get('ok'):return None
        for entry in self.state['entries']:
            probe=self._probe(obs,entry)
            if not probe.get('ok')or probe.get('old_present')or not probe.get('retired')or probe.get('new_unit')!=entry['new']['unit_number']:return None
            current=next((p for p in self.perimeter.state['posts']if p['name']==entry['name']),None)
            if current!=entry['new']:return None
            proofs,_=self._virtual(obs,entry)
            if not proofs:return None
        plan=self.engines.state['plan'];x,y=xy({'position':self.engines.state['core']['anchor']})
        future=engine_core(self.catalog,x,y,4)
        if not preflight(self.game,self.builder,obs,plan['entities']).get('ok'):return None
        if not preflight(self.game,self.builder,obs,merge_entities(plan['entities'],future['entities'])).get('ok'):return None
        if not surface_pipe_proof(self.game,obs,plan).get('ok'):return None
        return {'tick':obs.get('tick'),'relocated':[{'name':e['name'],'old_unit':e['old']['unit_number'],
            'replacement_unit':e['new']['unit_number'],'position':e['new']['position']}for e in self.state['entries']],
            'engine_plan_unchanged':True,'future_core_preflight':True,'scope':'geometric armed coverage and ordinary conserved recovery; no combat victory claim'}
