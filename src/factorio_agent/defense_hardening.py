"""Paid, additive mutually covering turret bastions at observed loss hotspots."""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations,islice
import json
import hashlib
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_ai.world_catalog import _yield
from .board import entity
from .defense_coverage import coverage_cells,native_candidate_survey
from .defense_perimeter import AMMO_TARGET,protected_factory_areas
from .planning import overlap,preflight
from .transport import VECTORS,xy

INITIAL_STOCK=20

def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _box(e):
    p=e['position'];half=1 if e['name']=='gun-turret'else .5
    return [[p['x']-half,p['y']-half],[p['x']+half,p['y']+half]]
def _cluster_area(group):
    boxes=[_box(e)for b in group for e in b['entities']]
    return [[min(b[0][0]for b in boxes),min(b[0][1]for b in boxes)],
            [max(b[1][0]for b in boxes),max(b[1][1]for b in boxes)]]


def wall_bastion(position,facing=4):
    """Eight-wall front/sides with a two-tile open rear service approach."""
    if(not isinstance(position,dict)or set(position)!={'x','y'}or facing not in VECTORS
            or any(type(v)not in(int,float)or not math.isfinite(v)or v%1 for v in position.values())):
        raise ValueError('integer turret center and cardinal bastion heading required')
    x,y=position['x'],position['y'];vx,vy=VECTORS[facing]
    def point(u,v):return {'x':x+u*vx-v*vy,'y':y+u*vy+v*vx}
    walls=[entity('stone-wall',*point(1.5,v).values())for v in(-1.5,-.5,.5,1.5)]
    walls += [entity('stone-wall',*point(u,v).values())for u in(-.5,.5)for v in(-1.5,1.5)]
    return {'position':dict(position),'facing':facing,'entities':[entity('gun-turret',x,y)]+walls,
            'service_points':[point(-1.5,v)for v in(-.5,.5)]}


def mutual_coverage(bastions,hotspot,radius):
    if len(bastions)<2 or any(math.dist(xy(a),xy(b))>radius for a,b in combinations(bastions,2)):return False
    es=[e for b in bastions for e in b['entities']]
    if any(overlap(_box(a),_box(b))for a,b in combinations(es,2)):return False
    targets=[[[hotspot['x']-1,hotspot['y']-1],[hotspot['x']+1,hotspot['y']+1]]]+[_box(e)for e in es if e['name']=='stone-wall']
    for area in targets:
        corners=[(x,y)for x in(area[0][0],area[1][0])for y in(area[0][1],area[1][1])]
        if sum(all(math.dist(xy(b),p)<=radius+1e-9 for p in corners)for b in bastions)<2:return False
    for b in bastions:
        if not any(not any(overlap([[p['x']-.4,p['y']-.4],[p['x']+.4,p['y']+.4]],_box(e))for e in es)
                   for p in b['service_points']):return False
    return True


BASTION_SURVEY_LUA=r'''
local rows={};local turret=prototypes.entity["gun-turret"];local wall=prototypes.entity["stone-wall"]
if not turret or turret.tile_width~=2 or turret.tile_height~=2 or not wall or wall.tile_width~=1 or wall.tile_height~=1 then
 return {ok=false,reason="native_bastion_footprints_changed"}end
local enemies=s.find_entities_filtered{position=x.hotspot,radius=48,force="enemy",type="turret"}
for _,q in ipairs(x.candidates)do local legal=true
 for _,enemy in pairs(enemies)do local attack=enemy.prototype.attack_parameters
  if attack and attack.range and(enemy.position.x-q.position.x)^2+(enemy.position.y-q.position.y)^2<=(attack.range+2)^2 then legal=false;break end
 end
 if legal then for _,e in ipairs(q.entities)do local half=e.name=="gun-turret"and 1 or .5
  local p=e.position;local area={{p.x-half,p.y-half},{p.x+half,p.y+half}}
  if s.count_entities_filtered{area=area,type="resource"}>0 or not s.can_place_entity{name=e.name,position=p,direction=0,force=f}then legal=false;break end
 end end
 if legal then rows[#rows+1]=q end
end
return {ok=true,candidates=rows}
'''


SERVICE_ACCESS_LUA=r'''
local function overlaps(a,b)return a[1][1]<b[2][1]and a[2][1]>b[1][1]and a[1][2]<b[2][2]and a[2][2]>b[1][2]end
local boxes={};for _,e in ipairs(x.entities)do local h=e.name=="gun-turret"and 1 or .5;local p=e.position
 boxes[#boxes+1]={{p.x-h,p.y-h},{p.x+h,p.y+h}}end
local cache={};local reached={};local queue={};local head=1;local b=x.bounds
local function key(i,j)return i..":"..j end
local function walk(i,j)
 if i<b[1][1]or i>b[2][1]or j<b[1][2]or j>b[2][2]then return false end
 local k=key(i,j);if cache[k]~=nil then return cache[k]end
 local area={{i,j},{i+1,j+1}};local clear=true
 for _,r in ipairs(x.reserved)do if overlaps(area,r)then clear=false;break end end
 if clear then for _,r in ipairs(boxes)do if overlaps(area,r)then clear=false;break end end end
 if clear then clear=s.can_place_entity{name="character",position={i+.5,j+.5},force=f}end
 cache[k]=clear;return clear end
for i=b[1][1],b[2][1]do for j=b[1][2],b[2][2]do
 if(i+.5-x.hotspot.x)^2+(j+.5-x.hotspot.y)^2<=9 and walk(i,j)then
  reached[key(i,j)]=true;queue[#queue+1]={i,j}end
end end
while head<=#queue do local p=queue[head];head=head+1
 for _,v in ipairs{{1,0},{-1,0},{0,1},{0,-1}}do local i=p[1]+v[1];local j=p[2]+v[2];local k=key(i,j)
  if not reached[k]and walk(i,j)then reached[k]=true;queue[#queue+1]={i,j}end
 end
end
for _,q in ipairs(x.bastions)do local access=false
 for _,p in ipairs(q.service_points)do if reached[key(p.x-.5,p.y-.5)]then access=true;break end end
 if not access then return {ok=false,reason="prospective_bastion_rear_service_disconnected",position=q.position}end
end
return {ok=true,scope="four-neighbour native walkable service component with all prospective walls and turrets blocked"}
'''


HARDENING_PROOF_LUA=r'''
local rows={};local magazine=prototypes.item["firearm-magazine"].magazine_size
for _,q in ipairs(x.entities)do local e=target(q.position,q.name)
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"or e.health<=0 then return {ok=false,reason="hardening_owned_asset_missing_or_changed",entity=q}end
 if q.name=="gun-turret"then local inv=e.get_inventory(defines.inventory.turret_ammo);local full=0
  for i=1,#inv do local st=inv[i];if st.valid_for_read then
   if st.name~="firearm-magazine"or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
    or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data
    or st.ammo<1 or st.ammo>magazine or st.ammo%1~=0 then return {ok=false,reason="hardening_magazine_metadata_or_type_changed"}end
   full=full+st.count-(st.ammo<magazine and 1 or 0)
  end end
  if full<x.minimum_full then return {ok=false,reason="hardening_post_full_normal_magazines_or_health_missing"}end
  rows[#rows+1]={unit_number=e.unit_number,position=e.position,full_magazines=full,health=e.health,
   max_health=e.max_health,damaged=e.health<e.max_health}
 end
end
return {ok=true,tick=game.tick,posts=rows}
'''


class DefenseHardening:
    def __init__(self,game,bootstrap,builder,catalog,perimeter):
        self.game,self.bootstrap,self.builder,self.catalog,self.perimeter=game,bootstrap,builder,catalog,perimeter
        self.path=Path(game.cfg.runtime_dir)/'defense-hardening.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.wall_repair=None

    def _identity(self,obs,value):return(obs.get('ok',True)and value.get('world_id')==obs.get('world_id')
        and bool(obs.get('world_id'))and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def _save(self):_atomic_json(self.path,self.state)

    def _receipts(self):
        path=self.path.parent/'job-events.jsonl'
        if not path.exists():return []
        pending={};rows=[]
        with path.open('rb')as stream:
            start=max(0,path.stat().st_size-3*1024*1024);stream.seek(start)
            if start:stream.readline()
            for raw in stream:
                row=json.loads(raw);key=row.get('key')
                if row.get('event')=='action_intent':pending[key]=(row,hashlib.sha256(raw).hexdigest())
                elif row.get('event')=='action_result'and key in pending:
                    before,digest=pending.pop(key)
                    if before.get('tick')!=row.get('tick')or not row.get('result',{}).get('ok'):continue
                    rows.append(dict(key=key,tick=row['tick'],action=before['action'],result=row['result'],
                        intent_sha256=digest,result_sha256=hashlib.sha256(raw).hexdigest()))
        return rows

    def _paid_build(self,unit,spec,receipts):
        prior=self.state.get('unit_build_receipts',{}).get(str(unit))
        if(prior and not prior.get('result',{}).get('reused')and prior['action'].get('name')==spec['name']
                and prior['action'].get('position')==spec['position']):return prior
        matches=[r for r in receipts if r['action'].get('type')=='build'and r['action'].get('name')==spec['name']
                 and r['action'].get('position')==spec['position']and r['result'].get('unit_number')==unit
                 and not r['result'].get('reused')]
        return matches[0]if len(matches)==1 else None

    def _observe_delivery(self,post,unit,full,receipts):
        saved=self.state.setdefault('stock_delivery_receipts',{}).setdefault(str(unit),{})
        for row in receipts:
            a,r=row['action'],row['result'];moved=r.get('moved')
            if(a.get('type')=='insert'and a.get('name')=='gun-turret'and a.get('item')=='firearm-magazine'
                    and a.get('inventory')=='turret_ammo'and a.get('position')==post['position']
                    and a.get('expected_entity_world_id')==self.state['world_id']and a.get('expected_entity_unit')==unit
                    and type(moved)is int and 0<moved<=a.get('count',0)):
                saved[row['result_sha256']]={k:row[k]for k in('key','tick','intent_sha256','result_sha256')}|{'moved':moved}
                if sum(v['moved']for v in saved.values())>=INITIAL_STOCK:break
        delivered=sum(v['moved']for v in saved.values())
        if delivered>=INITIAL_STOCK or full>=INITIAL_STOCK:
            self.state.setdefault('stocked_units',{})[post['name']]=unit
            self.state['stocked_posts']=sorted(self.state['stocked_units'])

    def _bind_paid_posts(self,obs,disk,*,include_walls=True):
        receipts=self._receipts();actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])}
        changed=False
        canonical_losses=[]
        for expected in self.state['posts']:
            matches=[p for p in disk['posts']if p.get('name')==expected['name']]
            if len(matches)!=1 or any(matches[0].get(k)!=expected[k]for k in('name','position','coverage_scope','local_walk_access')):
                return {'status':'blocked','reason':'hardening_registered_post_geometry_changed'}
            post=matches[0];spec=dict(expected,name='gun-turret');unit=post.get('unit_number')
            if unit is not None and(type(unit)is not int or unit<1):return {'status':'blocked','reason':'hardening_canonical_post_unit_invalid'}
            if not unit or actual.get(('gun-turret',*xy(spec))):continue
            paid=self._paid_build(unit,spec,receipts);bound=self.state['bindings'].get(_key(spec))
            if not paid:return {'status':'blocked','reason':'hardening_missing_canonical_paid_build_proof_required'}
            if bound and not self._paid_build(bound,spec,receipts):
                return {'status':'blocked','reason':'hardening_lost_unit_paid_build_proof_missing'}
            canonical_losses.append({'old_unit':unit,'position':post['position'],'paid_build':paid,
                'observed_tick':obs.get('tick'),'kind':'registered_paid_post_missing','post_name':post['name'],
                'source_binding_unit':bound,'source_binding_paid_build':self._paid_build(bound,spec,receipts)if bound else None})
        if canonical_losses:
            native=self.game.query('local rows=helpers.json_to_table('+_payload(canonical_losses)+''');for _,q in ipairs(rows)do
if target(q.position,"gun-turret")then return {ok=false,reason="hardening_canonical_missing_post_native_presence_changed"}end
end;return {ok=true}''')
            if not native.get('ok'):return {'status':'blocked',**native}
            self.state['pending_canonical_losses']=canonical_losses
            for loss in canonical_losses:
                post=next(p for p in disk['posts']if p['name']==loss['post_name']);unit=loss['old_unit']
                self.state.setdefault('unit_build_receipts',{})[str(unit)]=loss['paid_build']
                if not any(v.get('kind')==loss['kind']and v['old_unit']==unit for v in self.state.get('loss_history',[])):
                    self.state.setdefault('loss_history',[]).append(loss)
                history=list(post.get('lost_unit_numbers',[]))
                for old in(post.get('lost_unit_number'),unit):
                    if type(old)is int and old not in history:history.append(old)
                post['lost_unit_numbers']=history[-16:];post.setdefault('lost_unit_origin',loss['source_binding_unit']or history[0])
                post.update(lost_unit_number=unit,commissioned=False);post.pop('unit_number',None)
            self.state['loss_history']=self.state['loss_history'][-32:]
            self._save();_atomic_json(self.perimeter.path,disk);changed=True
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));bound=self.state['bindings'].get(_key(spec))
            if spec['name']!='gun-turret':
                if not include_walls:continue
                if bound and(not live or live.get('unit_number')!=bound):return {'status':'blocked','reason':'hardening_owned_asset_lost_or_replaced','entity':spec}
                if live and not bound:self.state['bindings'][_key(spec)]=live['unit_number'];changed=True
                continue
            expected=next(p for p in self.state['posts']if p['position']==spec['position'])
            matches=[p for p in disk['posts']if p.get('name')==expected['name']]
            if len(matches)!=1 or any(matches[0].get(k)!=expected[k]for k in('name','position','coverage_scope','local_walk_access')):
                return {'status':'blocked','reason':'hardening_registered_post_geometry_changed'}
            post=matches[0]
            if bound and(not live or live.get('unit_number')!=bound):
                paid=self._paid_build(bound,spec,receipts)
                if not paid:return {'status':'blocked','reason':'hardening_lost_unit_paid_build_proof_missing','entity':spec}
                native=self.game.query('local q=helpers.json_to_table('+_payload(dict(spec,old_unit=bound,new_unit=live and live.get('unit_number')))+''');local e=target(q.position,q.name)
if q.new_unit then return {ok=e and e.force==f and e.surface==s and e.unit_number==q.new_unit and e.quality.name=="normal"and e.position.x==q.position.x and e.position.y==q.position.y}
end;return {ok=e==nil}''')
                if not native.get('ok'):return {'status':'blocked','reason':'hardening_lost_or_repaired_native_identity_changed'}
                replacement=self._paid_build(live['unit_number'],spec,receipts)if live else None
                if live and(not replacement or replacement['tick']<=paid['tick']or post.get('unit_number')!=live['unit_number']):
                    return {'status':'blocked','reason':'hardening_replacement_requires_exact_registered_paid_repair'}
                loss={'old_unit':bound,'position':spec['position'],'paid_build':paid,'observed_tick':obs.get('tick'),
                    'replacement_unit':live and live['unit_number'],'replacement_paid_build':replacement}
                self.state['pending_loss_publication']=loss;self._save()
                if not live and post.get('unit_number')not in(None,bound):return {'status':'blocked','reason':'hardening_canonical_loss_identity_changed'}
                history=list(post.get('lost_unit_numbers',[]))
                for unit in(post.get('lost_unit_number'),bound):
                    if type(unit)is int and unit not in history:history.append(unit)
                post['lost_unit_numbers']=history[-16:];post.setdefault('lost_unit_origin',bound)
                if not live:
                    if post.get('unit_number')==bound or not post.get('lost_unit_number'):post['lost_unit_number']=bound
                    post.pop('unit_number',None);post['commissioned']=False
                _atomic_json(self.perimeter.path,disk)
                self.state.setdefault('loss_history',[]).append(loss);self.state['loss_history']=self.state['loss_history'][-32:]
                self.state['bindings'].pop(_key(spec));self.state.setdefault('stocked_units',{}).pop(post['name'],None)
                self.state['stocked_posts']=sorted(self.state['stocked_units'])
                self.state.pop('pending_loss_publication',None);changed=True
            if live and(self.state['bindings'].get(_key(spec))!=live['unit_number']or post.get('unit_number')!=live['unit_number']):
                paid=self._paid_build(live['unit_number'],spec,receipts)
                if not paid:return {'status':'blocked','reason':'hardening_new_post_paid_build_proof_missing'}
                if post.get('unit_number')not in(None,live['unit_number']):return {'status':'blocked','reason':'hardening_canonical_post_identity_changed'}
                native=self.game.query('local q=helpers.json_to_table('+_payload(dict(spec,unit_number=live['unit_number']))+''');local e=target(q.position,q.name)
return {ok=e and e.force==f and e.surface==s and e.unit_number==q.unit_number and e.quality.name=="normal"and e.position.x==q.position.x and e.position.y==q.position.y}''')
                if not native.get('ok'):return {'status':'blocked','reason':'hardening_new_post_native_identity_changed'}
                post['unit_number']=live['unit_number'];self.state['bindings'][_key(spec)]=live['unit_number']
                self.state.setdefault('unit_build_receipts',{})[str(live['unit_number'])]=paid;changed=True
        if changed:
            self.state.pop('pending_canonical_losses',None)
            _atomic_json(self.perimeter.path,disk);self.perimeter.state=disk;self._save()
        self._current_receipts=receipts
        return None

    def prepare_recovery(self,obs):
        """Readonly exact paid lineage preview; it never adopts or changes units."""
        if not self.state or not self._identity(obs,self.state):return {'ok':False,'reason':'hardening_world_or_catalog_changed'}
        disk=json.loads(self.perimeter.path.read_text(encoding='utf-8'));receipts=self._receipts()
        if not self._identity(obs,disk):return {'ok':False,'reason':'hardening_perimeter_identity_changed'}
        actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])};recovery=[]
        for spec in self.state['plan']['entities']:
            old=self.state['bindings'].get(_key(spec));live=actual.get((spec['name'],*xy(spec)))
            if spec['name']!='gun-turret'or not old or(live and live.get('unit_number')==old):continue
            original=self._paid_build(old,spec,receipts)
            if not original:return {'ok':False,'reason':'hardening_lost_unit_paid_build_proof_missing'}
            post=next((p for p in disk['posts']if p.get('coverage_scope')==self.state['scope_id']and p['position']==spec['position']),None)
            replacement=self._paid_build(live['unit_number'],spec,receipts)if live else None
            if live and(not replacement or not post or post.get('unit_number')!=live['unit_number']or replacement['tick']<=original['tick']):
                return {'ok':False,'reason':'hardening_replacement_requires_exact_registered_paid_repair'}
            recovery.append(dict(old_unit=old,new_unit=live and live['unit_number'],position=spec['position'],
                original_paid_build=original,replacement_paid_build=replacement))
        report=self.perimeter.inspect(obs)
        return {'ok':True,'recoveries':recovery,'actor_full_magazines':report.get('actor_full_magazines'),
                'initial_stock_per_post':INITIAL_STOCK,'scope':'exact ledger lineage preview; native step rechecks identity before publication'}

    def _prepay_deployment(self,obs):
        report=self.perimeter.inspect(obs)
        if not report.get('ok'):return {'status':'blocked','reason':'hardening_prepayment_native_inventory_required'}
        rows={p['name']:p for p in report['posts']};missing=0;ammunition=0
        for post in self.state['posts']:
            unit=self.state['bindings'].get(_key(dict(post,name='gun-turret')));row=rows.get(post['name'],{})
            if not unit:missing+=1;ammunition+=INITIAL_STOCK;continue
            full=row.get('full_magazines',0);self._observe_delivery(post,unit,full,self._current_receipts)
            if self.state.get('stocked_units',{}).get(post['name'])!=unit:ammunition+=max(0,INITIAL_STOCK-full)
            elif full<AMMO_TARGET:ammunition+=INITIAL_STOCK-full
        self._save()
        if not missing:return None
        if obs.get('inventory',{}).get('gun-turret',0)<missing:return self.bootstrap.ensure_item(obs,'gun-turret',missing)
        if report.get('actor_full_magazines',0)<ammunition:return self.perimeter._ensure_magazines(obs,report,ammunition)
        self.state['deployment_prepayment']={'tick':obs.get('tick'),'remaining_turrets':missing,'full_magazines':report['actor_full_magazines']}
        self._save();return None

    def _access(self,state):
        payload={k:state[k]for k in('hotspot','bounds','reserved','bastions')};payload['entities']=state['plan']['entities']
        return self.game.query('local x=helpers.json_to_table('+_payload(payload)+');'+SERVICE_ACCESS_LUA)

    def upkeep(self,obs):
        """After post restocking, ordinarily rebuild one known wall or delegate repair."""
        if not self.state or self.state.get('phase')!='verified':return None
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'hardening_world_or_catalog_changed'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:return {'status':'blocked','reason':'hardening_checkpoint_changed'}
        disk=json.loads(self.perimeter.path.read_text(encoding='utf-8'))
        if not self._identity(obs,disk):return {'status':'blocked','reason':'hardening_perimeter_identity_changed'}
        self.perimeter.state=disk;bound=self._bind_paid_posts(obs,disk,include_walls=False)
        if bound is not None:return bound
        actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])}
        pending=self.state.get('wall_rebuild_intent')
        walls=[e for e in self.state['plan']['entities']if e['name']=='stone-wall']
        if pending:walls.sort(key=lambda e:e!=pending['entity'])
        for spec in walls:
            key=_key(spec);unit=self.state['bindings'].get(key);live=actual.get((spec['name'],*xy(spec)))
            if pending and pending['entity']==spec:
                if live:
                    receipt=self._paid_build(live['unit_number'],spec,self._current_receipts)
                    if not receipt:return {'status':'blocked','reason':'hardening_wall_rebuild_paid_receipt_missing'}
                    native=self.game.query('local q=helpers.json_to_table('+_payload(dict(spec,unit_number=live['unit_number']))+''');local e=target(q.position,q.name)
return {ok=e and e.force==f and e.surface==s and e.unit_number==q.unit_number and e.quality.name=="normal"and e.position.x==q.position.x and e.position.y==q.position.y}''')
                    if not native.get('ok'):return {'status':'blocked','reason':'hardening_rebuilt_wall_native_identity_changed'}
                    self.state['bindings'][key]=live['unit_number']
                    self.state.setdefault('wall_recovery_history',[]).append(dict(pending,new_unit=live['unit_number'],paid_build=receipt))
                    self.state['wall_recovery_history']=self.state['wall_recovery_history'][-32:]
                    self.state.pop('wall_rebuild_intent');self._save()
                    return {'status':'waiting','reason':'normally_paid_wall_rebuild_bound_reobserve'}
                return self.builder.ensure_plan(obs,_plan([spec],[],clear_obstacles=[],clearance_world_id=obs['world_id']))
            if not unit:return {'status':'blocked','reason':'hardening_wall_original_unit_unknown'}
            if live and live.get('unit_number')!=unit:return {'status':'blocked','reason':'hardening_wall_unexpected_replacement'}
            if not live:
                native=self.game.query('local q=helpers.json_to_table('+_payload(spec)+');return {ok=target(q.position,q.name)==nil}')
                if not native.get('ok'):return {'status':'blocked','reason':'hardening_wall_native_absence_changed'}
                self.state['wall_rebuild_intent']={'entity':deepcopy(spec),'old_unit':unit,'world_id':obs['world_id'],'tick':obs.get('tick')}
                self._save();self.state['bindings'].pop(key);self._save()
                return self.builder.ensure_plan(obs,_plan([spec],[],clear_obstacles=[],clearance_world_id=obs['world_id']))
            if live.get('health',0)<live.get('max_health',0)and self.wall_repair is not None:
                return self.wall_repair(obs,spec,unit)
        return None

    def _arm_scope(self,obs,*,final=False):
        report=self.perimeter.inspect(obs)
        if not report.get('ok'):return {'status':'blocked','reason':'hardening_post_observation_failed'}
        live={p['name']:p for p in report['posts']};scope=self.perimeter.state['coverage_scopes'][self.state['scope_id']]
        missing=False;all_present=all(_key(dict(p,name='gun-turret'))in self.state['bindings']for p in self.state['posts'])
        for post in self.state['posts']:
            row=live.get(post['name'],{})
            bound=self.state['bindings'].get(_key(dict(post,name='gun-turret')))
            if not bound:missing=True;continue
            if row.get('missing')or row.get('identity_changed')or row.get('unit_number')!=bound:
                return {'status':'blocked','reason':'hardening_registered_post_missing_or_changed'}
            full=row.get('full_magazines',0)
            self._observe_delivery(post,bound,full,self._current_receipts);self._save()
            floor=AMMO_TARGET if final else 1
            initial_reserve_needed=not all_present and self.state.get('stocked_units',{}).get(post['name'])!=bound
            needed=INITIAL_STOCK-full if initial_reserve_needed or full<floor else 0
            if needed>0:
                held=report.get('actor_full_magazines',0)
                if not held:return self.perimeter._ensure_magazines(obs,report,needed)
                return {'type':'insert','name':'gun-turret','position':post['position'],'item':'firearm-magazine',
                    'inventory':'turret_ammo','count':min(held,needed),'expected_entity_unit':row['unit_number'],
                    'expected_entity_world_id':obs['world_id'],'reason':'normally stock mutual bastion with twenty full magazines and retain commissioning floor ten'}
        if missing:return None
        guns=[dict(e,unit_number=self.state['bindings'][_key(e)])for e in self.state['plan']['entities']if e['name']=='gun-turret']
        proof=self.game.query('local x=helpers.json_to_table('+_payload(dict(entities=guns,
            minimum_full=AMMO_TARGET if final else 1,initial=not scope.get('commissioned')))+');'+HARDENING_PROOF_LUA)
        if not proof.get('ok'):return {'status':'waiting','reason':'hardening_new_posts_waiting_for_fresh_armed_observation','evidence':proof}
        self.perimeter._bind(report)
        if(not scope.get('commissioned')and len(proof.get('posts',[]))==len(self.state['posts'])
                and all(p['full_magazines']>=AMMO_TARGET for p in proof['posts'])):
            self.state['initial_armed_observation']=proof;self._save()
            scope.update(commissioned=True,commissioned_tick=obs.get('tick'));self.perimeter._save()
        return None

    def prepare(self,obs,parameters=None):
        if parameters:return {'ok':False,'reason':'hardening_selects_observed_hotspots_without_coordinate_overrides'}
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'hardening_world_or_catalog_changed'}
        if not self._identity(obs,self.perimeter.state):return {'ok':False,'reason':'known_defense_perimeter_required'}
        if not all(obs.get('enabled_recipes',{}).get(n)for n in('gun-turret','firearm-magazine','stone-wall')):
            return {'ok':False,'reason':'native_turret_magazine_and_stone_wall_research_required'}
        try:
            recipe=self.catalog.recipes['stone-wall']
            if(recipe['ingredients']!=[{'name':'stone-brick','amount':5,'type':'item'}]or len(recipe['products'])!=1
                    or recipe['products'][0]['name']!='stone-wall'or _yield(recipe['products'][0])!=1):
                return {'ok':False,'reason':'installed_ordinary_five_brick_wall_recipe_required'}
            report=self.perimeter.inspect(obs)
            if not report.get('ok')or report.get('changed'):return {'ok':False,'reason':'known_defense_native_identity_required'}
            actual={p['name']:p for p in report['posts']};rank=[]
            for p in self.perimeter.state['posts']:
                if p.get('coverage_scope','').startswith('hardening:'):continue
                live=actual[p['name']];lost=set(p.get('lost_unit_numbers',[]))
                if p.get('lost_unit_number'):lost.add(p['lost_unit_number'])
                damage=max(0,live.get('max_health',0)-live.get('health',0));pressure=live.get('nearby_enemies',0)
                if lost or damage:rank.append((-len(lost),-pressure,-damage,p['name'],p,live))
            if not rank:return {'ok':False,'reason':'no_observed_defense_loss_or_damage_hotspot'}
            _,_,_,_,post,live=min(rank,key=lambda r:r[:4]);hotspot=post['position']
            losses=len(set(post.get('lost_unit_numbers',[]))|({post['lost_unit_number']}if post.get('lost_unit_number')else set()))
            count=3 if losses>=2 or live.get('nearby_enemies',0)>=8 else 2
            facing=4
            threat=self.game.query('local p=helpers.json_to_table('+_payload(hotspot)+''');local sx,sy,n=0,0,0
for _,e in pairs(s.find_entities_filtered{position=p,radius=48,force="enemy",type={"unit","unit-spawner","turret"}})do sx=sx+e.position.x;sy=sy+e.position.y;n=n+1 end
return {ok=true,x=sx,y=sy,count=n}''')
            if not threat.get('ok'):return threat
            if threat.get('count',0):
                dx,dy=threat['x']/threat['count']-hotspot['x'],threat['y']/threat['count']-hotspot['y']
                facing=(4 if dx>=0 else 12)if abs(dx)>=abs(dy)else(8 if dy>=0 else 0)
            reserved=protected_factory_areas(self.game,obs['world_id'],self.catalog.fingerprint)
            x,y=hotspot['x'],hotspot['y'];bounds=[[x-20,y-20],[x+20,y+20]]
            candidates=[{'x':x+dx,'y':y+dy}for dx in range(-14,15,2)for dy in range(-14,15,2)if 16<=dx*dx+dy*dy<=196]
            native=native_candidate_survey(self.game,candidates,[[[x-1,y-1],[x+1,y+1]]],reserved,bounds)
            if not native.get('ok'):return native
            radius=native['range']-2
            bastions=[wall_bastion(p['position'],facing)for p in native['candidates']]
            bastions=[b for b in bastions if not any(overlap(_box(e),r)for e in b['entities']for r in reserved)]
            surveyed=self.game.query('local x=helpers.json_to_table('+_payload({'hotspot':hotspot,'candidates':bastions})+');'+BASTION_SURVEY_LUA)
            if not surveyed.get('ok'):return surveyed
            choices=sorted(surveyed['candidates'],key=lambda b:(math.dist(xy(b),(x,y)),xy(b)))[:48]
            chosen=None;groups=0
            for group in islice(combinations(choices,count),4096):
                groups+=1
                if(mutual_coverage(group,hotspot,radius)and not any(overlap(_cluster_area(group),r)for r in reserved)):
                    chosen=deepcopy(list(group));break
            if chosen is None:return {'ok':False,'reason':'no_finite_ore_free_mutual_bastions','native_candidates':len(choices),'groups_checked':groups}
            scope_id='hardening:'+post['name']+':1';posts=[]
            for i,b in enumerate(chosen):
                posts.append(dict(name='bastion_'+post['name']+'_'+str(i+1),position=b['position'],
                    coverage_scope=scope_id,local_walk_access=True,commissioned=False))
            es=[e for b in chosen for e in b['entities']];area=_cluster_area(chosen)
            whole=preflight(self.game,self.builder,obs,es)
            if not whole.get('ok'):return {'ok':False,'reason':'whole_bastion_native_preflight_failed','evidence':whole}
            plan=_plan(es,[],reserved_area=area,clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy='mutual-open-rear-bastions-v1')
            cells=coverage_cells([[[x-1,y-1],[x+1,y+1]]]);names=[p['name']for p in posts]
            scope=dict(areas=[[[x-1,y-1],[x+1,y+1]]],cells=cells,effective_range=radius,native_range=native['range'],
                covering_posts=names,new_posts=names,commissioned=False,access_scope='prospective wall-aware rear service component')
            state=dict(ok=True,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='register',
                scope_id=scope_id,block='defense_'+scope_id,hotspot=deepcopy(hotspot),hotspot_name=post['name'],
                losses_observed=losses,nearby_enemies_observed=live.get('nearby_enemies',0),bastions=chosen,posts=posts,scope=scope,
                plan=plan,bindings={},reserved=reserved,bounds=bounds,prepared_tick=obs.get('tick'),
                requirements={'gun-turret':count,'firearm-magazine':INITIAL_STOCK*count,'stone-wall':8*count,'stone-brick':40*count},
                initial_stock_per_post=INITIAL_STOCK,commissioning_full_magazine_floor=AMMO_TARGET)
            access=self._access(state)
            if not access.get('ok'):return access
            return state
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'ok':False,'reason':'invalid_hardening_plan_authority','detail':str(exc)}

    def step(self,obs,parameters=None):
        if not self.state:
            ready=self.prepare(obs,parameters)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'hardening_world_or_catalog_changed'}
        if not obs.get('enabled_recipes',{}).get('stone-wall'):return {'status':'blocked','reason':'native_stone_wall_research_required'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:return {'status':'blocked','reason':'hardening_checkpoint_changed'}
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        if not self._identity(obs,board):return {'status':'blocked','reason':'hardening_board_identity_changed'}
        old=board['blocks'].get(self.state['block'])
        if old is not None and old!=self.state['plan']:return {'status':'blocked','reason':'hardening_registered_plan_changed'}
        if old is None:board['blocks'][self.state['block']]=deepcopy(self.state['plan']);_atomic_json(board_path,board)
        disk=json.loads(self.perimeter.path.read_text(encoding='utf-8'))
        if not self._identity(obs,disk):return {'status':'blocked','reason':'hardening_perimeter_identity_changed'}
        prior=disk.get('coverage_scopes',{}).get(self.state['scope_id'])
        if prior is None:
            if any(p['name']in {q['name']for q in disk['posts']}for p in self.state['posts']):return {'status':'blocked','reason':'hardening_post_name_conflict'}
            disk['posts'].extend(deepcopy(self.state['posts']));disk.setdefault('coverage_scopes',{})[self.state['scope_id']]=deepcopy(self.state['scope'])
            _atomic_json(self.perimeter.path,disk)
        elif any(prior.get(k)!=self.state['scope'].get(k)for k in('areas','cells','effective_range','covering_posts','new_posts')):
            return {'status':'blocked','reason':'hardening_registered_scope_changed'}
        self.perimeter.state=disk
        bound=self._bind_paid_posts(obs,disk)
        if bound is not None:return bound
        if self.state['phase']=='verified':
            proof=self.proof(obs)
            return {'status':'succeeded','evidence':proof}if proof else{'status':'blocked','reason':'commissioned_hardening_native_proof_missing'}
        guns=[e for e in self.state['plan']['entities']if e['name']=='gun-turret']
        prepaid=self._prepay_deployment(obs)
        if prepaid is not None:return prepaid
        final=all(_key(e)in self.state['bindings']for e in self.state['plan']['entities']if e['name']=='stone-wall')
        armed=self._arm_scope(obs,final=final)
        if armed is not None:return armed
        missing=next((e for e in guns if _key(e)not in self.state['bindings']),None)
        if missing is not None:
            return self.builder.ensure_plan(obs,_plan([missing],[],clear_obstacles=self.state['plan']['clear_obstacles'],clearance_world_id=obs['world_id']))
        self.state['phase']='walls';self._save()
        check=preflight(self.game,self.builder,obs,self.state['plan']['entities'])
        if not check.get('ok'):return {'status':'blocked','reason':'hardening_site_changed_after_arming','evidence':check}
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if not proof:return {'status':'blocked','reason':'hardening_native_proof_missing'}
        self.state.update(phase='verified',commissioning_proof=proof);self._save()
        return {'status':'succeeded','reason':'normally_paid_mutual_bastions_commissioned','evidence':proof}

    def proof(self,obs):
        if not self.state or not self._identity(obs,self.state):return None
        if any(_key(e)not in self.state['bindings']for e in self.state['plan']['entities']):return None
        if not mutual_coverage(self.state['bastions'],self.state['hotspot'],self.state['scope']['effective_range']):return None
        scope=self.perimeter.state.get('coverage_scopes',{}).get(self.state['scope_id'],{})
        if not scope.get('commissioned')or any(scope.get(k)!=self.state['scope'][k]for k in('areas','cells','effective_range','covering_posts','new_posts')):return None
        es=[dict(e,unit_number=self.state['bindings'][_key(e)])for e in self.state['plan']['entities']]
        payload=dict(entities=es,minimum_full=5 if self.state.get('phase')=='verified'else AMMO_TARGET,initial=not scope.get('commissioned'))
        result=self.game.query('local x=helpers.json_to_table('+_payload(payload)+');'+HARDENING_PROOF_LUA)
        if not result.get('ok')or not self._access(self.state).get('ok'):return None
        return {'tick':result['tick'],'native':result,'posts':len(self.state['posts']),'walls':sum(e['name']=='stone-wall'for e in es),
            'mutual_wall_and_hotspot_coverage':True,'rear_service_access':True,
            'scope':'geometric dual-gun coverage and normally armed intact bastions; no combat victory or nest destruction claim'}
