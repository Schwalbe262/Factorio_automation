"""Paid additive north water header; original intake and outlet remain fixed."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .chemical_production import ChemicalProduction
from .fluid_handoffs import bound_handoffs
from .joint_fluid_routing import ProspectiveWaterSource,revision
from .petroleum import compile_pipe_crossings
from .planning import overlap,preflight
from .resource_policy import mining_reservations
from .routing import route_trunks,VECTORS
from .transport import xy,PIPE_WITNESS_LUA,surface_pipe_proof,unbuilt_external_keepouts
from .water_gathering import WaterGathering


def _key(q):return q['name']+':'+str(q['position']['x'])+':'+str(q['position']['y'])
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _producer_revision(s):return revision({k:s.get(k)for k in('world_id','catalog_fingerprint','core','plan','bindings','defense_scope_id')})


def header_core(source,length,soft,maximum):
    if type(length)is not int or not 28<=length<=40:raise ValueError('bounded28..40tile north header required')
    x,y=xy(source);path=route_trunks((x,y),(x,y-length),start_direction=0,end_direction=0)
    result=compile_pipe_crossings(path,set(soft),maximum)
    if not result.get('ok'):return result
    es=[q for q in result['entities']if xy(q)!=(x,y)]
    output=dict(kind='fluid',item='water',name='pipe',position={'x':x,'y':y-length},direction='output',facing=0,fluidbox_index=1)
    return _plan(es,[output],source=deepcopy(source),full_path=result['full_path'],
        underground_pairs=result['underground_pairs'],maximum_underground_span=maximum,output_access=[entity('pipe',x,y-length-i)for i in(1,2,3)],
        reserved_area=[[x-.5,y-length-2.5],[x+.5,y-1.5]],
        source_apron=[{'x':x,'y':y-1}],flow='NORTH',growth='fixed outlet; north consumer corridor',
        layout_policy='owned-intake-additive-north-water-v1')


def _edges(nodes,pairs):
    by={xy(q):q for q in nodes};edges=[];seen=set()
    def add(a,b):
        k=tuple(sorted((_key(a),_key(b))))
        if k not in seen:seen.add(k);edges.append(dict(a=a,b=b))
    for p,a in by.items():
        for d,(dx,dy)in VECTORS.items():
            b=by.get((p[0]+dx,p[1]+dy))
            if b and(a['name']!='pipe-to-ground'or a['direction']==d)and(b['name']!='pipe-to-ground'or b['direction']==(d+8)%16):add(a,b)
    for pair in pairs:add(by[tuple(pair['first'])],by[tuple(pair['last'])])
    return edges


SURVEY_LUA=PIPE_WITNESS_LUA+r'''
if not d or d.world_id~=args.world_id then return {ok=false,reason="water_header_world_changed"}end
local old={};for _,q in ipairs(args.original)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="water_header_original_asset_changed"}end
 for fluid in pairs(e.get_fluid_contents())do if fluid~="water"then return {ok=false,reason="water_header_original_contaminated"}end end;old[e.unit_number]=true end
local soft={};local pipes={};local assets={};local spans={}
for _,box in pairs(prototypes.entity["pipe-to-ground"].fluidbox_prototypes)do for _,c in pairs(box.pipe_connections)do
 if c.connection_type=="underground"then spans[#spans+1]=c.max_underground_distance end end end
if #spans~=1 then return {ok=false,reason="water_header_native_span_unknown"}end
for _,p in ipairs(args.path)do local area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}}
 if s.count_entities_filtered{area=area,type="resource"}>0 then return {ok=false,reason="water_header_resource_obstruction",position=p}end
 for _,zone in ipairs(args.zones)do local b=zone.area;if p.x+.49>b[1][1]and p.x-.49<b[2][1]and p.y+.49>b[1][2]and p.y-.49<b[2][2]then return {ok=false,reason="water_header_mining_reservation_obstruction"}end end
 for _,tile in pairs(s.find_tiles_filtered{area=area})do for layer in pairs(prototypes.entity.pipe.collision_mask.layers)do
  if tile.collides_with(layer)then return {ok=false,reason="water_header_terrain_obstruction",position=p}end end end
 for _,e in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}}})do
  local natural=e.force.name=="neutral"and e.minable and(e.type=="tree"or e.type=="simple-entity")
  if not old[e.unit_number]and e~=a and e.type~="resource"and e.type~="corpse"and not natural and not e.prototype.collision_mask.colliding_with_tiles_only then
   local near=math.abs(e.position.x-p.x)<.5 and math.abs(e.position.y-p.y)<.5
   if e.name=="pipe"and e.force==f then
    local witness=surface_pipe_witness(e,{position=pos(e.position),unit_number=e.unit_number})
    if not witness then return {ok=false,reason="water_header_foreign_pipe_identity_unverified"}end
    pipes[e.unit_number]=witness;soft[p.x..":"..p.y]=p
   elseif near and e.name=="transport-belt"and e.force==f then
    assets[e.unit_number]={name=e.name,position=pos(e.position),direction=e.direction,unit_number=e.unit_number};soft[p.x..":"..p.y]=p
   elseif near or next(e.prototype.fluidbox_prototypes or{})then return {ok=false,reason="water_header_foreign_asset_obstruction",position=p}end
  end
 end
end
local rows={};for _,q in pairs(soft)do rows[#rows+1]=q end;local ps={};for _,q in pairs(pipes)do ps[#ps+1]=q end;local bs={};for _,q in pairs(assets)do bs[#bs+1]=q end
return {ok=true,maximum_span=spans[1],soft=rows,pipes=ps,assets=bs}
'''


HEADER_PROOF_LUA=r'''
if not d or d.world_id~=args.world_id then return {ok=false,reason="water_header_world_changed"}end
local live={};local expected={};local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
for _,q in ipairs(args.nodes)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.quality.name~="normal"or e.position.x~=q.position.x or e.position.y~=q.position.y
  or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="water_header_native_unit_changed"}end
 for name in pairs(e.get_fluid_contents())do if name~="water"then return {ok=false,reason="water_header_mixed_fluid"}end end
 if e.name~="offshore-pump"then
  if not e.has_fluid_segment(1)then return {ok=false,reason="water_header_native_extent_changed"}end
  local b=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
  if b.right_bottom.x-b.left_top.x>limit or b.right_bottom.y-b.left_top.y>limit then return {ok=false,reason="water_header_native_extent_changed"}end end
 live[key(q)]=e
end
local function edge(a,b)local forward=false;local reverse=false
 for _,p in pairs(a.get_fluid_box_neighbours(1)or{})do if p.entity==b and p.index==1 then forward=true end end
 for _,p in pairs(b.get_fluid_box_neighbours(1)or{})do if p.entity==a and p.index==1 then reverse=true end end
 if not forward or not reverse then return false end
 expected[a.unit_number]=expected[a.unit_number]or{};expected[a.unit_number][b.unit_number]=true
 expected[b.unit_number]=expected[b.unit_number]or{};expected[b.unit_number][a.unit_number]=true;return true end
for _,q in ipairs(args.edges)do if not edge(live[key(q.a)],live[key(q.b)])then return {ok=false,reason="water_header_reciprocal_edge_missing"}end end
for _,q in ipairs(args.downstream)do local a=live[key(q.source)];local b=target(q.first_neighbor.position,q.first_neighbor.name)
 if not b or b.force~=f or b.surface~=s or b.unit_number~=q.first_neighbor.unit_number or not edge(a,b)then return {ok=false,reason="water_header_downstream_identity_changed"}end end
for _,e in pairs(live)do for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do
 if p.index~=1 or not(expected[e.unit_number]or{})[p.entity.unit_number]then return {ok=false,reason="water_header_undeclared_fluid_peer"}end end end
local outlet=live[key(args.outlet)];local source=live[key(args.source)];local amount=outlet.get_fluid_contents().water or 0
if amount<=0 or outlet.get_fluid_segment_id(1)~=source.get_fluid_segment_id(1)then return {ok=false,reason="water_header_actual_connected_water_required"}end
for _,q in ipairs(args.foreign_pipes)do local e=target(q.position,"pipe")
 if not e or e.unit_number~=q.unit_number or(e.has_fluid_segment(1)and e.get_fluid_segment_id(1)==outlet.get_fluid_segment_id(1))then return {ok=false,reason="water_header_foreign_network_merged"}end end
return {ok=true,tick=game.tick,water=amount,outlet_unit=outlet.unit_number,reciprocal_edges=#args.edges,
 scope="one existing intake shared by unchanged old outlet and paid new header; no multiplied supply or measured throughput claim"}
'''


HEADER_GUARD_LUA=r'''
if not d or d.world_id~=args.world_id then return {ok=false,reason="water_header_world_changed"}end
local allowed={};local water={}
for _,q in ipairs(args.nodes)do local e=target(q.position,q.name)
 if q.unit_number then
  if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.quality.name~="normal"or e.direction~=q.direction
   or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="water_header_source_or_owned_unit_changed"}end
  for name in pairs(e.get_fluid_contents())do if name~="water"then return {ok=false,reason="water_header_contamination"}end end
  allowed[e.unit_number]=true;water[e.unit_number]=true
 elseif e then return {ok=false,reason="water_header_unbound_asset_present"}end
end
for _,q in ipairs(args.assets)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction then return {ok=false,reason="water_header_preserved_belt_changed"}end;allowed[e.unit_number]=true end
for _,q in ipairs(args.pipes)do allowed[q.unit_number]=true end
for _,p in ipairs(args.path)do local area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}}
 if s.count_entities_filtered{area=area,type="resource"}>0 then return {ok=false,reason="water_header_resource_changed"}end
 for _,zone in ipairs(args.zones)do local b=zone.area
  if p.x+.49>b[1][1]and p.x-.49<b[2][1]and p.y+.49>b[1][2]and p.y-.49<b[2][2]then return {ok=false,reason="water_header_mining_growth_changed"}end end
 for _,e in pairs(s.find_entities_filtered{area=area})do
  local nature=e.force.name=="neutral"and e.minable and(e.type=="tree"or e.type=="simple-entity")
  if e~=a and e.type~="corpse"and e.type~="resource"and not nature and not e.prototype.collision_mask.colliding_with_tiles_only
   and not allowed[e.unit_number]then return {ok=false,reason="water_header_unrecorded_corridor_asset"}end
 end
end
for _,q in ipairs(args.new_nodes)do local p=q.position
 for _,e in pairs(s.find_entities_filtered{area={{p.x-1.49,p.y-1.49},{p.x+1.49,p.y+1.49}}})do
  if not water[e.unit_number]then for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="water_header_incidental_fluid_peer"}end end
 end
 if q.name=="pipe-to-ground"then for _,e in pairs(s.find_entities_filtered{position=p,radius=args.maximum,name="pipe-to-ground"})do
  if(e.position.x==p.x or e.position.y==p.y)and not water[e.unit_number]then return {ok=false,reason="water_header_foreign_buried_peer"}end end end
end
return {ok=true}
'''


class WaterDistribution:
    def __init__(self,game,builder,catalog,perimeter,*,water=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.water=water if water is not None else WaterGathering(game,builder,catalog,perimeter)
        self.path=Path(game.cfg.runtime_dir)/'water-distribution.json';self.registry_path=self.path.parent/'fluid-sources.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{};self.last_proof_failure=None

    def _identity(self,obs,d):return d.get('world_id')==obs.get('world_id')and d.get('catalog_fingerprint')==self.catalog.fingerprint
    def _save(self):_atomic_json(self.path,self.state)

    def _authority(self,obs):
        producer=json.loads(self.water.path.read_text(encoding='utf-8'));board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        registry=json.loads(self.registry_path.read_text(encoding='utf-8'))
        if any(not self._identity(obs,d)for d in(producer,board,registry))or producer.get('phase')!='verified':raise ValueError('verified original water producer/board/registry required')
        if board['blocks'].get('water_gathering')!=producer['plan']:raise ValueError('original water PCB plan changed')
        old=registry['sources']['water'];port=producer['core']['ports'][0]
        if any(old.get(k)!=port.get(k)for k in port)or old.get('unit_number')!=producer['bindings'].get(_key(port)):
            raise ValueError('original published water source changed')
        if self.state and(_producer_revision(producer)!=self.state['producer_revision']or old!=self.state['old_source']):raise ValueError('bound original water producer changed')
        if not self.state and registry['sources'].get('sulfur-water'):raise ValueError('sulfur water alias exists without checkpoint')
        return producer,board,registry,old

    def prepare(self,obs):
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'water_distribution_world_changed'}
        try:
            producer,board,registry,old=self._authority(obs)
            if not self.water.proof(obs):return {'ok':False,'reason':'water_header_existing_intake_proof_required'}
            original=[dict(q,unit_number=producer['bindings'][_key(q)])for q in producer['plan']['entities']]
            tap=deepcopy(producer['core']['pipes'][len(producer['core']['pipes'])//2]);tap['unit_number']=producer['bindings'][_key(tap)]
            x,y=xy(tap);points=[{'x':x,'y':y-i}for i in range(1,36)]
            survey=self.game.query('local args=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],original=original,path=points,zones=mining_reservations(self.game,obs['world_id'])))+');'+SURVEY_LUA)
            if not survey.get('ok'):return survey
            soft={(q['x'],q['y'])for q in survey.get('soft',[])}
            if soft:
                low=min(p[1]for p in soft);high=max(p[1]for p in soft)+1
                if high-low+2<=survey['maximum_span']:soft={(x,low+i)for i in range(int(high-low)+1)}
            core=header_core(tap,32,soft,survey['maximum_span'])
            if not core.get('ok'):return core
            protected=[p['reserved_area']for name,p in board['blocks'].items()if name!='water_gathering'and p.get('reserved_area')]
            protected += [q['area']for q in board.get('shared_main_bus',{}).get('reservations',[])if q.get('area')]
            if any(overlap(core['reserved_area'],p)for p in protected):return {'ok':False,'reason':'water_header_reserved_corridor_conflict'}
            future=unbuilt_external_keepouts(self.builder,obs,board['blocks'])
            if any((q['x'],q['y'])in future for q in core['full_path'][1:]):return {'ok':False,'reason':'water_header_foreign_future_corridor'}
            whole=preflight(self.game,self.builder,obs,core['entities']+core['output_access'])
            if not whole.get('ok'):return whole
            # Explicit planning-only descriptor: zero is never a produced source or registry entry.
            preview_source=ProspectiveWaterSource(obs['world_id'],self.catalog.fingerprint,old,core['ports'][0],_producer_revision(producer))
            chemical=ChemicalProduction(self.game,self.builder,self.catalog,self.perimeter)
            preview=chemical.prepare(obs,{'product':'sulfur'},_prospective_water=preview_source)
            if not preview.get('ok'):return {'ok':False,'reason':'water_header_complete_sulfur_preview_required','evidence':preview}
            scope='water-distribution:sulfur';defense=self.perimeter.prepare_coverage(obs,scope,[core['reserved_area']])
            if not defense.get('ok'):return {'ok':False,'reason':'water_header_defense_preview_required','evidence':defense}
            for pair in core['underground_pairs']:pair['preserved_pipes']=deepcopy(survey.get('pipes',[]))
            plan=_plan(core['entities'],core['ports'],reserved_area=core['reserved_area'],defense_scope_id=scope,
                underground_pairs=core['underground_pairs'],clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'])
            return dict(ok=True,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='build',core=core,plan=plan,
                producer_revision=_producer_revision(producer),old_source=old,original=original,tap=tap,bindings={},
                foreign_assets=survey.get('assets',[]),foreign_pipes=survey.get('pipes',[]),defense_scope_id=scope,
                defense_preview=defense,consumer_preview=preview,prepared_tick=obs['tick'])
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'ok':False,'reason':'water_header_context_invalid','detail':str(exc)}

    def _guard(self,obs):
        check=surface_pipe_proof(self.game,obs,self.state['plan'])
        if not check.get('ok'):return check
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        for q in self.state['original']+self.state['foreign_assets']:
            live=actual.get((q['name'],*xy(q)))
            if not live or live.get('unit_number')!=q['unit_number']or live.get('direction',0)!=q.get('direction',0):return {'ok':False,'reason':'water_header_preserved_asset_changed','entity':q}
        _,board,_,_=self._authority(obs)
        own_apron={(p['x'],p['y'])for p in self.state['core']['source_apron']}|{xy(self.state['tap'])}
        for p in self.state['core']['full_path'][1:]:
            cell=[[p['x']-.49,p['y']-.49],[p['x']+.49,p['y']+.49]]
            for name,block in board['blocks'].items():
                if name=='water_distribution'or(name=='water_gathering'and(p['x'],p['y'])in own_apron):continue
                if block.get('reserved_area')and overlap(cell,block['reserved_area']):return {'ok':False,'reason':'water_header_registered_corridor_changed'}
        new=[dict(q,unit_number=self.state['bindings'].get(_key(q)))for q in self.state['plan']['entities']]
        args=dict(world_id=obs['world_id'],nodes=self.state['original']+new,new_nodes=new,
            assets=self.state['foreign_assets'],pipes=self.state['foreign_pipes'],path=self.state['core']['full_path'],
            zones=mining_reservations(self.game,obs['world_id']),maximum=self.state['core']['maximum_underground_span'])
        return self.game.query('local args=helpers.json_to_table('+_payload(args)+');'+HEADER_GUARD_LUA)

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return dict(prepared,status='blocked')
            self.state=prepared;self._save()
        try:
            _,board,_,_=self._authority(obs)
            if not self._identity(obs,self.state):raise ValueError('water header world changed')
            actual={(q['name'],*xy(q)):q for q in obs['entities']}
            for q in self.state['plan']['entities']:
                live=actual.get((q['name'],*xy(q)));unit=self.state['bindings'].get(_key(q))
                if unit and(not live or live.get('unit_number')!=unit):return {'status':'blocked','reason':'water_header_owned_asset_lost_or_replaced'}
                if live:self.state['bindings'][_key(q)]=live['unit_number']
            self._save();name='water_distribution';old=board['blocks'].get(name)
            if old not in(None,self.state['plan']):raise ValueError('water header registered plan changed')
            if not old:board['blocks'][name]=self.state['plan'];_atomic_json(self.path.parent/'pcb-board.json',board)
            result=self.perimeter.ensure_coverage(obs,self.state['defense_scope_id'],[self.state['core']['reserved_area']])
            if result is not None:return result
            guard=self._guard(obs)
            if not guard.get('ok'):return dict(guard,status='blocked')
            check=preflight(self.game,self.builder,obs,self.state['plan']['entities']+self.state['core']['output_access'])
            if not check.get('ok'):return dict(check,status='blocked')
            result=self.builder.ensure_plan(obs,self.state['plan'])
            if result.get('status')!='succeeded':return result
            proof=self.proof(obs)
            if not proof:return {'status':'waiting','reason':'waiting_for_exact_connected_native_water_header','evidence':self.last_proof_failure}
            _,_,registry,_=self._authority(obs);old=registry['sources'].get('sulfur-water')
            if old not in(None,proof['source']):raise ValueError('published sulfur water changed')
            registry['sources']['sulfur-water']=proof['source'];_atomic_json(self.registry_path,registry)
            self.state.update(phase='verified',proof=proof);self._save();return {'status':'succeeded','evidence':proof}
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'status':'blocked','reason':'water_header_authority_changed','detail':str(exc)}

    def proof(self,obs):
        self.last_proof_failure=None
        try:
            if not self.state or not self._identity(obs,self.state):return None
            _,_,registry,_=self._authority(obs);guard=self._guard(obs)
            if not guard.get('ok'):self.last_proof_failure=guard;return None
            defense=self.perimeter.coverage_proof(obs,self.state['defense_scope_id'])
            if not defense or not self.water.proof(obs):return None
            if any(_key(q)not in self.state['bindings']for q in self.state['plan']['entities']):return None
            nodes=self.state['original']+[dict(q,unit_number=self.state['bindings'][_key(q)])for q in self.state['plan']['entities']]
            output=dict(self.state['core']['ports'][0],unit_number=self.state['bindings'][_key(self.state['core']['ports'][0])],
                world_id=obs['world_id'],defense_scope_id=self.state['defense_scope_id'])
            downstream=[]
            for source in(self.state['old_source'],output):
                if source in registry['sources'].values():
                    downstream += [{'source':source,'first_neighbor':q['first_neighbor']}for q in bound_handoffs(registry,obs,self.catalog,source)]
            args=dict(world_id=obs['world_id'],nodes=nodes,edges=_edges(nodes,self.state['core']['underground_pairs']),
                source=self.state['tap'],outlet=output,foreign_pipes=self.state['foreign_pipes'],downstream=downstream)
            native=self.game.query('local args=helpers.json_to_table('+_payload(args)+');'+HEADER_PROOF_LUA)
            if not native.get('ok'):self.last_proof_failure=native;return None
            return {'tick':native['tick'],'source':output,'native':native,'defense':defense,'original_outlet_preserved':self.state['old_source']}
        except(OSError,ValueError,KeyError,TypeError)as exc:self.last_proof_failure={'reason':'water_header_proof_context_changed','detail':str(exc)};return None
