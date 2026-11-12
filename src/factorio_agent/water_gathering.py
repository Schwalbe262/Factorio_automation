"""Independent normally paid shore intake; existing steam water stays intact."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .industrial import PlanningBudget
from .planning import overlap,preflight
from .resource_policy import mining_reservations
from .transport import VECTORS,xy


def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _payload(value):return json.dumps(json.dumps(value,separators=(',',':')))


def water_core(catalog,position,direction=0):
    """Derive the rotated native output and place three straight normal pipes."""
    proto=catalog.entities['offshore-pump']
    if proto.get('type')!='offshore-pump'or proto.get('electric')or proto.get('burner')or direction not in VECTORS:
        raise ValueError('installed offshore pump must require no external electric or burner supply')
    boxes=proto['fluidbox_prototypes']
    if len(boxes)!=1 or boxes[0]['index']!=1 or boxes[0].get('production_type')!='output':
        raise ValueError('one native water output fluid box required')
    connections=[c for c in boxes[0]['pipe_connections']if c.get('connection_type','normal')=='normal']
    if len(connections)!=1 or len(connections[0].get('positions',[]))!=4:raise ValueError('four rotated native water port positions required')
    c=connections[0];inside=c['positions'][direction//4];facing=(c['direction']+direction)%16
    dx,dy=VECTORS[facing];x,y=position['x'],position['y']
    if any(type(v)not in(int,float)or not math.isfinite(v)or abs(v%1-.5)>1e-6 for v in(x,y)):
        raise ValueError('half-tile finite native pump center required')
    pump=entity('offshore-pump',x,y,direction)
    pipes=[entity('pipe',x+inside['x']+dx*i,y+inside['y']+dy*i)for i in range(1,4)]
    access=[{'x':pipes[-1]['position']['x']+dx*i,'y':pipes[-1]['position']['y']+dy*i}for i in(1,2)]
    box=proto['selection_box'];corners=[]
    for xx in(box['left_top']['x'],box['right_bottom']['x']):
        for yy in(box['left_top']['y'],box['right_bottom']['y']):
            u,v=xx,yy
            for _ in range(direction//4):u,v=-v,u
            corners.append((x+u,y+v))
    pump_area=[[min(a for a,b in corners),min(b for a,b in corners)],
               [max(a for a,b in corners),max(b for a,b in corners)]]
    points=[xy(e)for e in pipes]+[(p['x'],p['y'])for p in access]
    area=[[min(pump_area[0][0],min(a for a,b in points)-.5),min(pump_area[0][1],min(b for a,b in points)-.5)],
          [max(pump_area[1][0],max(a for a,b in points)+.5),max(pump_area[1][1],max(b for a,b in points)+.5)]]
    output={'kind':'fluid','item':'water','name':'pipe','position':deepcopy(pipes[-1]['position']),
            'direction':'output','facing':facing,'fluidbox_index':1}
    return _plan(pipes+[pump],[output],pump=pump,pipes=pipes,output_access=access,pump_survey_area=pump_area,
                 reserved_area=area,layout_policy='independent-native-water-intake-v1')


class WaterGathering:
    def __init__(self,game,builder,catalog,perimeter):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.path=Path(game.cfg.runtime_dir)/'water-gathering.json';self.sources_path=self.path.parent/'fluid-sources.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_proof_failure=None

    def _identity(self,obs,value):
        return(obs.get('ok',True)and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
               and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def prepare(self,obs):
        if not obs.get('ok',True)or not isinstance(obs.get('world_id'),str)or not obs['world_id']:
            return {'ok':False,'reason':'water_requires_valid_world_observation'}
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'water_world_or_catalog_changed'}
        if not all(obs.get('enabled_recipes',{}).get(r)for r in('offshore-pump','pipe')):
            return {'ok':False,'reason':'native_water_pump_and_pipe_recipes_required'}
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))if board_path.exists()else{}
        if not self._identity(obs,board):return {'ok':False,'reason':'water_board_identity_changed'}
        if self.sources_path.exists():
            registry=json.loads(self.sources_path.read_text(encoding='utf-8'))
            if not self._identity(obs,registry):return {'ok':False,'reason':'water_registry_identity_changed'}
            if registry.get('sources',{}).get('water'):return {'ok':False,'reason':'water_source_exists_without_intake_checkpoint'}
        areas=[b['reserved_area']for b in board.get('blocks',{}).values()if b.get('reserved_area')]
        areas += [r['area']for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
        industrial=[b['reserved_area']for name,b in board.get('blocks',{}).items()
                    if name in('basic_petroleum','steel_production','brick_production','engine_production')and b.get('reserved_area')]
        goal={'x':sum((a[0][0]+a[1][0])/2 for a in industrial)/len(industrial),
              'y':sum((a[0][1]+a[1][1])/2 for a in industrial)/len(industrial)}if industrial else{'x':0,'y':0}
        budget=PlanningBudget(self.game)
        sites=budget.game.query(WATER_SITES_LUA.replace('PAYLOAD',_payload({'goal':goal})))
        if not sites.get('ok'):return sites
        failures=[]
        for site in sites.get('sites',[])[:24]:
            if budget.expired():return budget.failure(candidate_failures=failures[-6:])
            try:core=water_core(self.catalog,site['position'],site['direction'])
            except(KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'unsupported_native_water_intake','detail':str(exc)}
            if any(overlap(core['reserved_area'],a)for a in areas):continue
            legal=budget.game.query(WATER_CORE_PREFLIGHT_LUA.replace('PAYLOAD',_payload({'core':core,
                'zones':mining_reservations(self.game,obs['world_id'])})))
            if not legal.get('ok'):failures.append(legal);continue
            whole=preflight(budget.game,self.builder,obs,core['entities'])
            if not whole.get('ok'):failures.append(whole);continue
            scope='water-'+hashlib.sha256(json.dumps([obs['world_id'],core['ports']],sort_keys=True).encode()).hexdigest()[:20]
            defense=self.perimeter.prepare_coverage(obs,scope,[core['reserved_area']])
            if budget.expired():return budget.failure(candidate_failures=failures[-6:],stage='defense_preview')
            if not defense.get('ok'):failures.append({'stage':'defense','detail':defense});continue
            plan=_plan(core['entities'],core['ports'],reserved_area=core['reserved_area'],
                defense_scope_id=scope,clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy=core['layout_policy'])
            return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                'phase':'build','plan':plan,'core':core,'bindings':{},'prepared_tick':obs.get('tick'),
                'preserved_pumps':sites.get('preserved_pumps',[]),'defense_scope_id':scope,'defense_preview':defense}
        return {'ok':False,'reason':'no_independent_resource_free_defended_water_intake',
                'evidence':{'candidate_count':len(sites.get('sites',[])),'candidate_failures':failures[-6:]}}

    def _save(self):_atomic_json(self.path,self.state)

    def _bind(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));old=self.state['bindings'].get(_key(spec))
            if old and(not live or live.get('unit_number')!=old):return {'status':'blocked','reason':'water_asset_missing_or_replaced','entity':spec}
            if live:
                if type(live.get('unit_number'))is not int or(spec['name']=='offshore-pump'and live.get('direction')!=spec['direction']):
                    return {'status':'blocked','reason':'water_asset_identity_or_direction_changed'}
                if old is None:self.state['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def _reserve(self,obs):
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'))
        if not self._identity(obs,board):return {'status':'blocked','reason':'water_board_identity_changed'}
        old=board['blocks'].get('water_gathering');plan=self.state['plan']
        scope=self.state['defense_scope_id']
        if plan.get('defense_scope_id')is None and self.state.get('phase')=='verified':
            desired=dict(plan,defense_scope_id=scope)
            if old not in(plan,desired):return {'status':'blocked','reason':'water_scope_upgrade_reservation_changed'}
            if not self.proof(obs):return {'status':'blocked','reason':'water_scope_upgrade_native_proof_required'}
            if old!=desired:
                board['blocks']['water_gathering']=desired;_atomic_json(path,board)
            self.state['plan']=desired;self._save();old=plan=desired
        if plan.get('defense_scope_id')!=scope:return {'status':'blocked','reason':'water_reserved_defense_scope_changed'}
        if old and old!=plan:return {'status':'blocked','reason':'water_reservation_changed'}
        if any(k!='water_gathering'and overlap(plan['reserved_area'],b['reserved_area'])for k,b in board['blocks'].items()if b.get('reserved_area')):
            return {'status':'blocked','reason':'water_reservation_conflict'}
        if not old:board['blocks']['water_gathering']=plan;_atomic_json(path,board)
        return None

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'water_world_or_catalog_changed'}
        result=self._bind(obs)
        if result:return result
        result=self._reserve(obs)
        if result:return result
        result=self.perimeter.ensure_coverage(obs,self.state['defense_scope_id'],[self.state['core']['reserved_area']])
        if result is not None:return result
        if not self.state.get('construction_authorized'):
            coast=self.game.query(WATER_CORE_PREFLIGHT_LUA.replace('PAYLOAD',_payload({'core':self.state['core'],
                'zones':mining_reservations(self.game,obs['world_id'])})))
            if not coast.get('ok'):return {'status':'blocked','reason':'water_coast_changed_after_defense','evidence':coast}
            check=preflight(self.game,self.builder,obs,self.state['plan']['entities'])
            if not check.get('ok'):return {'status':'blocked','reason':'water_site_changed_after_defense','evidence':check}
            self.state['construction_authorized']=True;self._save()
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        evidence=self.proof(obs)
        if not evidence:return {'status':'waiting','reason':'waiting for independent native water and reciprocal pipe peers','evidence':self.last_proof_failure}
        registry=json.loads(self.sources_path.read_text(encoding='utf-8'))if self.sources_path.exists()else{
            'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'sources':{}}
        if not self._identity(obs,registry):return {'status':'blocked','reason':'water_registry_identity_changed'}
        old=registry['sources'].get('water')
        if old and old!=evidence['source']:return {'status':'blocked','reason':'published_water_output_identity_changed'}
        registry['sources']['water']=evidence['source'];_atomic_json(self.sources_path,registry)
        self.state.update(phase='verified',proof=evidence);self._save()
        return {'status':'succeeded','reason':'independent normally paid native water intake commissioned','evidence':evidence}

    def proof(self,obs):
        self.last_proof_failure=None
        if not self.state or not self._identity(obs,self.state):return None
        defense=self.perimeter.coverage_proof(obs,self.state['defense_scope_id'])
        if not defense:self.last_proof_failure={'reason':'water_defense_coverage_missing'};return None
        chain=[self.state['core']['pump']]+self.state['core']['pipes']
        if any(_key(e)not in self.state['bindings']for e in chain):return None
        result=self.game.query(WATER_PROOF_LUA.replace('PAYLOAD',_payload({
            'chain':[dict(e,unit_number=self.state['bindings'][_key(e)])for e in chain],
            'preserved_pumps':self.state['preserved_pumps']})))
        if not result.get('ok'):self.last_proof_failure=result;return None
        source=dict(self.state['core']['ports'][0],unit_number=result['outlet_unit'],world_id=obs['world_id'],
                    defense_scope_id=self.state['defense_scope_id'])
        return {'tick':result['tick'],'source':source,'native':result,'defense_coverage':defense,
                'scope':'independent native shore intake and finite water buffer; no measured sustained rate claim'}


WATER_SITES_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local proto=prototypes.entity["offshore-pump"]
if proto.electric_energy_source_prototype or proto.burner_prototype then return {ok=false,reason="water_intake_external_energy_source_unsupported"}end
local pumps=s.find_entities_filtered{name="offshore-pump",force=f,limit=4};local preserved={};local anchors={}
for _,e in pairs(pumps)do local peers={};for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do peers[#peers+1]={unit_number=p.entity.unit_number,index=p.index}end
 preserved[#preserved+1]={name=e.name,position=pos(e.position),direction=e.direction,unit_number=e.unit_number,peers=peers};anchors[#anchors+1]=e.position end
if #anchors==0 then anchors={{x=0,y=0}}end
local seen={};local rows={};local vectors={{0,-1,0},{1,0,4},{0,1,8},{-1,0,12}}
for _,origin in ipairs(anchors)do for _,tile in pairs(s.find_tiles_filtered{area={{origin.x-32,origin.y-32},{origin.x+32,origin.y+32}},name={"water","deepwater","water-green","deepwater-green"},limit=5000})do
 for _,v in ipairs(vectors)do local p={x=tile.position.x+.5-v[1],y=tile.position.y+.5-v[2]};local key=p.x..":"..p.y..":"..v[3]
  if not seen[key]then seen[key]=true;if s.can_place_entity{name="offshore-pump",position=p,direction=v[3],force=f}then
   rows[#rows+1]={position=p,direction=v[3],score=math.abs(p.x-args.goal.x)+math.abs(p.y-args.goal.y)}end end
 end
end end
table.sort(rows,function(a,b)if a.score~=b.score then return a.score<b.score end;if a.position.x~=b.position.x then return a.position.x<b.position.x end;if a.position.y~=b.position.y then return a.position.y<b.position.y end;return a.direction<b.direction end)
local selected={};for i=1,math.min(24,#rows)do selected[#selected+1]=rows[i]end
return {ok=true,sites=selected,preserved_pumps=preserved}
'''


WATER_CORE_PREFLIGHT_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local core=args.core
local function resource_free(area)
 if s.count_entities_filtered{area=area,type="resource"}>0 then return false end
 for _,zone in ipairs(args.zones)do local b=zone.area;if area[1][1]<b[2][1]and area[2][1]>b[1][1]and area[1][2]<b[2][2]and area[2][2]>b[1][2]then return false end end;return true end
if not resource_free(core.pump_survey_area)then return {ok=false,reason="water_pump_body_over_resource_or_mining_growth"}end
for _,q in ipairs(core.entities)do local p=q.position
 if not s.can_place_entity{name=q.name,position=p,direction=q.direction,force=f}then return {ok=false,reason="water_intake_native_geometry_blocked",position=p}end
 local radius=q.name=="pipe"and 1.49 or 2.49
 for _,e in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}}})do
  for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="water_intake_near_existing_fluid_network",position=p}end
 end
end
for _,p in ipairs(core.output_access)do
 if not resource_free({{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}})or not s.can_place_entity{name="pipe",position=p,force=f}then return {ok=false,reason="water_output_has_no_clear_resource_free_continuation"}end
 for _,e in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}}})do
  for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="water_output_continuation_near_foreign_fluid_network"}end
 end
end
return {ok=true}
'''


WATER_PROOF_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local previous=nil;local rows={};local steam_segments={}
for _,q in ipairs(args.preserved_pumps)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="existing_steam_water_pump_changed"}end
 local peers={};for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do peers[p.entity.unit_number..":"..p.index]=true
  if p.entity.has_fluid_segment(p.index)then steam_segments[p.entity.get_fluid_segment_id(p.index)]=true end end
 local count=0;for _ in pairs(peers)do count=count+1 end;if count~=#q.peers then return {ok=false,reason="existing_steam_water_pump_peers_changed"}end
 for _,p in ipairs(q.peers)do if not peers[p.unit_number..":"..p.index]then return {ok=false,reason="existing_steam_water_pump_peers_changed"}end end
end
for index,q in ipairs(args.chain)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y or(index==1 and e.direction~=q.direction)then return {ok=false,reason="native_water_asset_identity_changed"}end
 local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~="water"then return {ok=false,reason="native_water_source_contaminated"}end end
 if previous then local forward=false;local reverse=false
  for _,p in pairs(previous.get_fluid_box_neighbours(1)or{})do if p.entity==e and p.index==1 then forward=true end end
  for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do if p.entity==previous and p.index==1 then reverse=true end end
  if not forward or not reverse then return {ok=false,reason="native_water_reciprocal_peer_missing"}end
 end
 rows[#rows+1]={unit_number=e.unit_number,name=e.name,position=pos(e.position),water=fluids.water or 0};previous=e
end
if rows[1].water<=0 or rows[#rows].water<=0 then return {ok=false,reason="native_independent_water_required"}end
if not previous.has_fluid_segment(1)or steam_segments[previous.get_fluid_segment_id(1)]then return {ok=false,reason="new_water_intake_merged_with_existing_steam_segment"}end
local extent=previous.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
if extent.right_bottom.x-extent.left_top.x>limit or extent.right_bottom.y-extent.left_top.y>limit then return {ok=false,reason="water_native_pipeline_extent_exceeded"}end
return {ok=true,tick=game.tick,outlet_unit=rows[#rows].unit_number,chain=rows,preserved_pumps=#args.preserved_pumps}
'''
