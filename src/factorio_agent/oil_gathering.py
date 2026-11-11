"""Normally paid, defended crude extraction and its natural research trigger."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .planning import overlap,preflight
from .power_grid import plan_connection as power_connection
from .resource_policy import mark_pumpjack
from .smelting import merge_entities
from .transport import VECTORS,xy


def oil_core(catalog,position,direction=0,power_offset=(0,2),pipe_count=3):
    """Use the installed four rotated INTERNAL port positions plus facing."""
    if direction not in VECTORS or type(pipe_count)is not int or not 1<=pipe_count<=8:
        raise ValueError('oil core requires cardinal orientation and one to eight pipes')
    prototype=catalog.entities['pumpjack'];box=prototype.get('selection_box',{})
    if (box.get('left_top')!={'x':-1.5,'y':-1.5} or box.get('right_bottom')!={'x':1.5,'y':1.5}):
        raise ValueError('installed pumpjack must have the supported three by three footprint')
    boxes=[b for b in prototype['fluidbox_prototypes']if b.get('production_type')=='output']
    if len(boxes)!=1 or boxes[0]['index']!=1:raise ValueError('one native crude output fluidbox required')
    connections=[c for c in boxes[0]['pipe_connections']if c.get('connection_type','normal')=='normal'
                 and c.get('flow_direction')=='output']
    if len(connections)!=1 or len(connections[0].get('positions',[]))!=4:
        raise ValueError('pumpjack must export four rotated native output positions')
    connection=connections[0];facing=(connection['direction']+direction)%16
    if facing not in VECTORS:raise ValueError('cardinal native fluid connection required')
    internal=connection['positions'][direction//4];dx,dy=VECTORS[facing];x,y=position['x'],position['y']
    if any(type(v)not in(int,float)or not math.isfinite(v)for v in(x,y)):
        raise ValueError('finite native oil well position required')
    pump=entity('pumpjack',x,y,direction);footprint=mark_pumpjack(pump)
    pipes=[entity('pipe',x+internal['x']+dx*(i+1),y+internal['y']+dy*(i+1))for i in range(pipe_count)]
    pole=entity('small-electric-pole',x+power_offset[0],y+power_offset[1])
    es=pipes+[pole,pump];geometry=FactoryBuilder.__new__(FactoryBuilder)
    cells=geometry._occupied_by_plan(es)
    area=[[min(a for a,b in cells)-.5,min(b for a,b in cells)-.5],
          [max(a for a,b in cells)+.5,max(b for a,b in cells)+.5]]
    port={'kind':'fluid','item':'crude-oil','direction':'output','facing':facing,
          'name':'pipe','position':deepcopy(pipes[-1]['position']),'fluidbox_index':1}
    return _plan(es,[port],pumpjack=pump,pipes=pipes,power_port=pole,resource='crude-oil',
                 mining_footprint=footprint,mining_reserved_area=footprint,reserved_area=area,
                 layout_policy='native-defended-crude-well-v1')


def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])


class OilGathering:
    def __init__(self,game,builder,catalog,perimeter):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.path=Path(game.cfg.runtime_dir)/'oil-gathering.json'
        self.sources_path=self.path.parent/'fluid-sources.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}

    def _identity(self,obs,value):
        return (obs.get('ok',True) and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
                and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def prepare(self,obs):
        """Read-only: at most eight wells, four rotations and four service poles."""
        if not obs.get('ok',True)or not isinstance(obs.get('world_id'),str)or not obs['world_id']:
            return {'ok':False,'reason':'oil_gathering_requires_valid_world_observation'}
        if not obs.get('technologies',{}).get('oil-gathering')or not obs.get('enabled_recipes',{}).get('pumpjack'):
            return {'ok':False,'reason':'natural_oil_gathering_and_enabled_pumpjack_required'}
        trigger=self.catalog.technologies.get('oil-processing',{}).get('research_trigger',{})
        if trigger.get('type')!='mine-entity'or trigger.get('entities')!=['crude-oil']:
            return {'ok':False,'reason':'installed_oil_processing_crude_extraction_trigger_required'}
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'oil_gathering_world_or_catalog_changed'}
        board_path=self.path.parent/'pcb-board.json'
        board=json.loads(board_path.read_text(encoding='utf-8'))if board_path.exists()else{}
        if not self._identity(obs,board):return {'ok':False,'reason':'oil_gathering_board_identity_changed'}
        reserved=[b['reserved_area']for b in board.get('blocks',{}).values()if b.get('reserved_area')]
        reserved += [r['area']for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
        wells=self.game.query('''local wells={};for _,e in pairs(s.find_entities_filtered{position={0,0},radius=384,name="crude-oil",limit=64})do
 if e.amount>0 then wells[#wells+1]={position=pos(e.position),amount=e.amount}end end
table.sort(wells,function(a,b)return a.position.x^2+a.position.y^2<b.position.x^2+b.position.y^2 end)
local selected={};for i=1,math.min(8,#wells)do selected[#selected+1]=wells[i]end
return {ok=true,wells=selected}''')
        if not wells.get('ok'):return wells
        failures=[]
        for well in wells.get('wells',[]):
            for direction in VECTORS:
                for offset in ((0,2),(-2,0),(2,0),(0,-2)):
                    try:core=oil_core(self.catalog,well['position'],direction,offset)
                    except(ValueError,KeyError)as exc:return {'ok':False,'reason':'unsupported_native_pumpjack_geometry','detail':str(exc)}
                    if any(overlap(core['reserved_area'],r)for r in reserved):continue
                    check=preflight(self.game,self.builder,obs,core['entities'])
                    if not check.get('ok'):failures.append(check.get('reason'));continue
                    payload=json.dumps(json.dumps(core['entities'],separators=(',',':')))
                    isolated=self.game.query('''local specs=helpers.json_to_table('''+payload+''')
for _,spec in ipairs(specs)do if spec.name=="pipe"then local p=spec.position
 for _,other in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}},force=f})do
  for _ in pairs(other.prototype.fluidbox_prototypes or {})do return {ok=false,reason="oil_outlet_near_existing_fluid_network"}end
 end end end;return {ok=true}''')
                    if not isolated.get('ok'):failures.append(isolated.get('reason'));continue
                    power=power_connection(self.game,self.builder,obs,core['power_port']['position'],
                                           [e for e in core['entities']if e['name']!='small-electric-pole'])
                    if not power.get('ok'):failures.append(power.get('reason'));continue
                    es=merge_entities(power['entities'],core['entities']);check=preflight(self.game,self.builder,obs,es)
                    if not check.get('ok'):failures.append(check.get('reason'));continue
                    scope='oil-'+hashlib.sha256(json.dumps([obs['world_id'],well['position'],core['ports']],sort_keys=True).encode()).hexdigest()[:20]
                    defense=self.perimeter.prepare_coverage(obs,scope,[core['reserved_area']])
                    if not defense.get('ok'):return {'ok':False,'reason':'oil_well_defense_preflight_failed','defense':defense}
                    plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],resource='crude-oil',
                        mining_reserved_area=core['mining_reserved_area'],clear_obstacles=check['clear_obstacles'],
                        clearance_world_id=obs['world_id'],layout_policy='native-defended-crude-well-v1')
                    actual={(e['name'],*xy(e)):e for e in obs['entities']}
                    bindings={_key(e):actual[(e['name'],*xy(e))]['unit_number']for e in es if(e['name'],*xy(e))in actual}
                    return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                        'phase':'build','prepared_tick':obs.get('tick'),'plan':plan,'core':core,'well':well,
                        'defense_scope_id':scope,'defense_preview':defense,'bindings':bindings,'wire_hops':power['wire_hops']}
        return {'ok':False,'reason':'no_defended_crude_well_with_native_outlet_and_power','candidate_failures':failures[-16:]}

    def _save(self):_atomic_json(self.path,self.state)

    def _bind(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));expected=self.state['bindings'].get(_key(spec))
            if expected and(not live or live.get('unit_number')!=expected):
                return {'status':'blocked','reason':'owned_oil_asset_missing_or_replaced','entity':spec['name'],'position':spec['position']}
            if live:
                if type(live.get('unit_number'))is not int or(spec['name']=='pumpjack'and live.get('direction')!=spec['direction']):
                    return {'status':'blocked','reason':'oil_asset_identity_or_direction_changed'}
                if expected is None:self.state['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def _reserve(self,obs):
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'))
        if not self._identity(obs,board):return {'status':'blocked','reason':'oil_gathering_board_identity_changed'}
        name='oil_gathering';area=self.state['core']['reserved_area'];old=board['blocks'].get(name)
        if old and old.get('defense_scope_id')!=self.state['defense_scope_id']:
            return {'status':'blocked','reason':'oil_gathering_reservation_identity_changed'}
        if old and any(old.get(k)!=self.state['plan'].get(k)for k in('entities','ports','reserved_area','mining_reserved_area')):
            return {'status':'blocked','reason':'oil_gathering_reservation_changed'}
        if any(k!=name and overlap(area,b['reserved_area'])for k,b in board['blocks'].items()if b.get('reserved_area')):
            return {'status':'blocked','reason':'oil_gathering_reservation_conflict'}
        board['blocks'][name]=dict(self.state['plan'],defense_scope_id=self.state['defense_scope_id'])
        _atomic_json(path,board);return None

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'oil_gathering_world_or_catalog_changed'}
        result=self._bind(obs)
        if result:return result
        result=self._reserve(obs)
        if result:return result
        result=self.perimeter.ensure_coverage(obs,self.state['defense_scope_id'],[self.state['core']['reserved_area']])
        if result is not None:return result
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        evidence=self.proof(obs)
        if not evidence:return {'status':'waiting','reason':'waiting for exact powered pumpjack, connected native crude and natural oil-processing research'}
        registry=json.loads(self.sources_path.read_text(encoding='utf-8'))if self.sources_path.exists()else{
            'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'sources':{}}
        if not self._identity(obs,registry):return {'status':'blocked','reason':'fluid_source_registry_identity_changed'}
        old=registry['sources'].get('crude-oil')
        if old and(old.get('unit_number')!=evidence['source']['unit_number']or old.get('defense_scope_id')!=self.state['defense_scope_id']):
            return {'status':'blocked','reason':'published_crude_output_identity_changed'}
        registry['sources']['crude-oil']=evidence['source'];_atomic_json(self.sources_path,registry)
        self.state.update(phase='verified',proof=evidence);self._save()
        return {'status':'succeeded','reason':'native crude extraction naturally unlocked oil processing','evidence':evidence}

    def proof(self,obs):
        if not self.state or not self._identity(obs,self.state):return None
        defense=self.perimeter.coverage_proof(obs,self.state['defense_scope_id'])
        if not defense:return None
        chain=[self.state['core']['pumpjack']]+self.state['core']['pipes']
        if any(_key(e)not in self.state['bindings']for e in chain):return None
        payload=json.dumps(json.dumps({'chain':[dict(e,unit_number=self.state['bindings'][_key(e)])for e in chain],
                                       'well':self.state['well']['position']},separators=(',',':')))
        result=self.game.query('''local args=helpers.json_to_table('''+payload+''');local previous=nil;local rows={}
local well=target(args.well,"crude-oil");if not well or well.amount<=0 or well.position.x~=args.well.x or well.position.y~=args.well.y then return {ok=false,reason="native_crude_well_missing"}end
for index,spec in ipairs(args.chain)do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return {ok=false,reason="native_oil_asset_identity_changed"}end
 if index==1 and(e.direction~=spec.direction or not e.is_connected_to_electric_network()or e.energy<=0)then return {ok=false,reason="pumpjack_not_normally_powered"}end
 if previous then local connected=false;for _,peer in pairs(previous.get_fluid_box_neighbours(1)or{})do if peer.entity==e and peer.index==1 then connected=true end end
  local reverse=false;for _,peer in pairs(e.get_fluid_box_neighbours(1)or{})do if peer.entity==previous and peer.index==1 then reverse=true end end
  if not connected or not reverse then return {ok=false,reason="native_crude_pipe_peer_missing"}end end
 local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~="crude-oil"then return {ok=false,reason="crude_output_contaminated"}end end
 rows[#rows+1]={name=e.name,unit_number=e.unit_number,position=pos(e.position),crude=fluids["crude-oil"]or 0};previous=e
end
if rows[#rows].crude<=0 or not f.technologies["oil-processing"].researched then return {ok=false,reason="native_crude_and_natural_research_required"}end
return {ok=true,tick=game.tick,chain=rows,oil_processing_researched=true}''')
        if not result.get('ok'):return None
        source=dict(self.state['plan']['ports'][0],unit_number=result['chain'][-1]['unit_number'],
                    world_id=obs['world_id'],defense_scope_id=self.state['defense_scope_id'])
        return {'tick':result['tick'],'source':source,'native':result,'defense_coverage':defense,
                'scope':'paid native extraction and research trigger; local finite pipe buffer, no sustained-throughput claim'}
