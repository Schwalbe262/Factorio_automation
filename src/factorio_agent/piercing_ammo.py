"""Local yellow-to-piercing ammunition production with ordinary batch couriers."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_ai.world_catalog import _yield,WorldCatalog
from .board import entity
from .industrial import PlanningBudget
from .planning import overlap,preflight,survey_slots
from .power_grid import plan_connection as power_connection
from .radar_network import RADAR_POWER_LUA,planned_power_watts
from .smelting import merge_entities
from .transport import unbuilt_external_keepouts,xy

PRODUCT='piercing-rounds-magazine'
YELLOW='firearm-magazine'
TARGET=200
BLOCK='piercing_ammo'

def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))


def piercing_rates(catalog):
    speed=catalog.entities['assembling-machine-1']['crafting_speed'];r=catalog.recipes[PRODUCT];y=catalog.recipes[YELLOW]
    def product(recipe,item):
        if len(recipe['products'])!=1 or recipe['products'][0]['name']!=item:raise ValueError('one deterministic normal ammunition product required')
        return _yield(recipe['products'][0])
    yr,rr=product(y,YELLOW),product(r,PRODUCT)
    yi={i['name']:i['amount']for i in y['ingredients']if i.get('type','item')=='item'}
    ri={i['name']:i['amount']for i in r['ingredients']if i.get('type','item')=='item'}
    if set(yi)!={'iron-plate'}or set(ri)!={YELLOW,'steel-plate','copper-plate'}:raise ValueError('installed ordinary ammunition chain required')
    if any(i.get('type','item')!='item'for q in(y,r)for i in q['ingredients']):raise ValueError('item-only ammunition chain required')
    yellow_rate=2*60*speed/y['energy']*yr;red_rate=min(4*60*speed/r['energy']*rr,yellow_rate*rr/ri[YELLOW])
    raw={k:v/rr for k,v in ri.items()if k!=YELLOW};raw['iron-plate']=ri[YELLOW]/rr*yi['iron-plate']/yr
    return dict(local_yellow_nameplate_per_minute=yellow_rate,red_nameplate_per_minute=red_rate,
        raw_per_red=raw,raw_at_red_nameplate_per_minute={k:v*red_rate for k,v in raw.items()},
        raw_for_200_red={k:v*TARGET for k,v in raw.items()},measured_red_per_minute=None,
        scope='catalog machine potential; ordinary finite input-buffer couriers, no continuous belt or sustained-rate promise')


def piercing_core(catalog,x,y):
    if any(type(v)not in(int,float)or not math.isfinite(v)or v%1!=.5 for v in(x,y)):raise ValueError('half-tile factory anchor required')
    es=[];yellow=[];red=[];arms=[];feeders=[];buffers=[];poles=[];wire=[]
    def add(name,dx,dy,d=0,**extra):
        q=dict(entity(name,x+dx,y+dy,d),**extra);es.append(q);return q
    def arm(dx,dy,d):q=add('fast-inserter',dx,dy,d);arms.append(q);return q
    # Power and red-wire spine precede machines and remain outside future cells.
    for dy in(-2,2,6,10):
        for dx in(2,6,10,14):poles.append(add('small-electric-pole',dx,dy))
    def pole(dx,dy):return next(q for q in poles if xy(q)==(x+dx,y+dy))
    snake=[]
    for i,dy in enumerate((-2,2,6,10)):
        snake += [pole(dx,dy)for dx in((2,6,10,14)if i%2==0 else(14,10,6,2))]
    wire += [{'color':'red','a':a,'b':b}for a,b in zip(snake,snake[1:])]
    for dx in(0,4):
        machine=add('assembling-machine-1',dx,0,recipe=YELLOW);yellow.append(machine)
        chest=add('iron-chest',dx,-3);buffers.append(dict(chest=chest,caps={'iron-plate':128},bar_slots=2))
        inp=arm(dx,-2,0);out=arm(dx,2,0)
        wire += [dict(color='red',a=q,b=pole(dx+2,dy))for q,dy in((machine,-2),(inp,-2),(out,2))]
    ybelts=[add('transport-belt',dx,3,4)for dx in range(-1,15)]
    for dx in(0,4,8,12):
        machine=add('assembling-machine-1',dx,8,recipe=PRODUCT);red.append(machine)
        chest=add('iron-chest',dx,5);buffers.append(dict(chest=chest,caps={'copper-plate':16,'steel-plate':8},bar_slots=3))
        feed=arm(dx,4,0);feeders.append(dict(arm=feed,chest=chest,limit=8))
        wire.append(dict(color='green',a=feed,b=chest))
        inp=arm(dx,6,0);out=arm(dx,10,0)
        wire += [dict(color='red',a=q,b=pole(dx+2,dy))for q,dy in((machine,6),(inp,6),(out,10))]
    rbelts=[add('transport-belt',dx,11,4)for dx in range(-1,15)]
    collector=arm(15,11,12);chest=add('iron-chest',16,11)
    wire += [dict(color='red',a=q,b=pole(14,10))for q in(collector,chest)]
    output_arms=[q for q in arms if xy(q)[1]==y+10]+[collector]
    global_arms=[q for q in arms if q not in [f['arm']for f in feeders]]
    future=[entity('assembling-machine-1',x+dx,y,recipe=YELLOW)for dx in(8,12)]
    access=[entity('transport-belt',x+17,y+11)]
    return _plan(es,[dict(name='iron-chest',kind='item',item=PRODUCT,direction='output',position=deepcopy(chest['position']))],
        machines=yellow+red,yellow_machines=yellow,red_machines=red,arms=arms,global_arms=global_arms,feeders=feeders,
        input_buffers=buffers,chest=chest,output_arms=output_arms,yellow_belts=ybelts,red_belts=rbelts,poles=poles,wire_edges=wire,
        expansion_keepouts=future,output_access=access,target=TARGET,bar_slots=math.ceil(TARGET/catalog.items[PRODUCT]['stack_size']),
        reserved_area=[[x-2.5,y-4.5],[x+19.5,y+13.5]],rated=piercing_rates(catalog),
        flow='local yellow EAST; local red EAST to fixed chest',growth='add two yellow machines EAST in declared empty upper slots',
        layout_policy='local-yellow-red-batch-factory-v1')


def audit_piercing_core(core):
    geometry=FactoryBuilder.__new__(FactoryBuilder);boxes=[geometry._occupied_by_plan([q])for q in core['entities']];errors=[]
    if sum(map(len,boxes))!=len(set().union(*boxes)):errors.append('overlapping_factory_footprints')
    if geometry._occupied_by_plan(core['expansion_keepouts']+core['output_access'])&set().union(*boxes):errors.append('future_or_output_access_obstructed')
    poles=[xy(q)for q in core['poles']]
    for q in core['machines']+core['arms']:
        if not any(abs(xy(q)[0]-px)<=2.5 and abs(xy(q)[1]-py)<=2.5 for px,py in poles):errors.append('unpowered_consumer:'+str(xy(q)))
    if len(core['yellow_machines'])!=2 or len(core['red_machines'])!=4:errors.append('wrong_native_machine_chain')
    if any(math.dist(xy(e['a']),xy(e['b']))>7.5 for e in core['wire_edges']):errors.append('wire_edge_out_of_conservative_reach')
    return {'ok':not errors,'errors':errors}


class PiercingAmmo:
    def __init__(self,game,builder,catalog,perimeter,*,yellow_source=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.bootstrap=builder.bootstrap;self.path=Path(game.cfg.runtime_dir)/'piercing-ammo.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_failure=None
    @property
    def commissioned(self):return self.state.get('phase')=='verified'
    def _save(self):_atomic_json(self.path,self.state)
    def _identity(self,obs):return self.state.get('world_id')==obs.get('world_id')and self.state.get('catalog_fingerprint')==self.catalog.fingerprint

    def prepare(self,obs):
        if self.state:return deepcopy(self.state)if self._identity(obs)else{'ok':False,'reason':'piercing_factory_world_or_catalog_changed'}
        if not obs.get('technologies',{}).get('circuit-network'):return {'ok':False,'reason':'piercing_factory_requires_normal_circuit_network_research'}
        required=(PRODUCT,YELLOW,'assembling-machine-1','fast-inserter','iron-chest','small-electric-pole','transport-belt')
        if not all(obs.get('enabled_recipes',{}).get(q)for q in required):return {'ok':False,'reason':'piercing_factory_requires_normal_recipes'}
        try:
            board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
            if board.get('world_id')!=obs['world_id']or board.get('catalog_fingerprint')!=self.catalog.fingerprint:raise ValueError('factory board frame changed')
            areas=[b['reserved_area']for b in board['blocks'].values()if b.get('reserved_area')]
            hard=unbuilt_external_keepouts(self.builder,obs,board['blocks'])
            # Coherent16tile grid around original base producers, not one seed's eight offsets.
            producers=[q['position']for q in obs['entities']if q['name']in('assembling-machine-1','stone-furnace','steel-furnace')]
            centers=[p for p in producers if math.hypot(p['x'],p['y'])<=128]or producers
            if not centers:return {'ok':False,'reason':'piercing_existing_productive_base_required'}
            budget=PlanningBudget(self.game);failures=[]
            networks={q.get('electric_network_id')for q in obs['entities']if q['name']=='steam-engine'and q.get('energy',0)>0}
            supply=[xy(q)for q in obs['entities']if q['name']=='small-electric-pole'and q.get('electric_network_id')in networks]
            if not supply:return {'ok':False,'reason':'piercing_supplied_base_network_required'}
            bounds=[[math.floor((min(p[k]for p in centers)-96)/16)*16 for k in('x','y')],
                    [math.floor((max(p[k]for p in centers)+96)/16)*16 for k in('x','y')]]
            candidates=[]
            areas += [r['area']for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
            for x0 in range(bounds[0][0],bounds[1][0]+1,16):
                for y0 in range(bounds[0][1],bounds[1][1]+1,16):
                    core=piercing_core(self.catalog,x0+.5,y0+.5)
                    if any(overlap(core['reserved_area'],a)for a in areas):continue
                    if self.builder._occupied_by_plan(core['entities']+core['expansion_keepouts']+core['output_access'])&hard:continue
                    candidates.append((min(math.dist(xy(core['poles'][0]),p)for p in supply),x0,y0,core))
            candidates.sort(key=lambda q:q[:3]);candidates=candidates[:64]
            land=survey_slots(budget.game,[[q[3]['reserved_area']]for q in candidates])if candidates else{'ok':True,'legal':[]}
            if not land.get('ok'):return land
            legal=[candidates[i-1][3]for i in land.get('legal',[])if type(i)is int and 1<=i<=len(candidates)]
            for core in legal:
                if budget.expired():return budget.failure(stage='piercing_whole_preparation',failures=failures[-4:])
                audit=audit_piercing_core(core)
                if not audit['ok']:return {'ok':False,'reason':'piercing_core_audit_failed','evidence':audit}
                if any(overlap(core['reserved_area'],a)for a in areas):failures.append('registered_factory_reservation');continue
                if self.builder._occupied_by_plan(core['entities']+core['expansion_keepouts']+core['output_access'])&hard:failures.append('foreign_unbuilt_transport');continue
                bridge=power_connection(budget.game,self.builder,obs,core['poles'][0]['position'],
                    [q for q in core['entities']if q['name']!='small-electric-pole']+core['expansion_keepouts']+core['output_access'])
                if not bridge.get('ok'):failures.append(bridge.get('reason'));continue
                actual={(q['name'],*xy(q)):q for q in obs['entities']};source=next((q for q in obs['entities']if q.get('unit_number')==bridge.get('source_unit_number')),None)
                if not source:failures.append('exact_native_power_source_missing');continue
                preserved=[dict({k:source[k]for k in('name','position','unit_number','direction','electric_network_id')},world_id=obs['world_id'])]
                new=[]
                for q in bridge['entities']:
                    old=actual.get((q['name'],*xy(q)))
                    if old:preserved.append(dict({k:old[k]for k in('name','position','unit_number','direction','electric_network_id')},world_id=obs['world_id']))
                    else:new.append(q)
                es=merge_entities(core['entities'],new);whole=preflight(budget.game,self.builder,obs,es+core['expansion_keepouts']+core['output_access'])
                if not whole.get('ok'):failures.append(whole.get('reason'));continue
                watts=sum(self.catalog.entities[q['name']].get('energy_usage',0)*60 for q in core['machines']+core['arms'])
                power=budget.game.query(RADAR_POWER_LUA.replace('PAYLOAD',_payload(dict(world_id=obs['world_id'],source=preserved[0],preserved_poles=preserved,
                    scheduled_watts=planned_power_watts(self.catalog,obs,board),additional_watts=watts))))
                if not power.get('ok'):failures.append(power.get('reason'));continue
                scope='piercing-ammo-factory';defense=self.perimeter.prepare_coverage(obs,scope,[core['reserved_area']])
                if not defense.get('ok'):failures.append(defense.get('reason'));continue
                plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],
                    layout_policy=core['layout_policy'])
                return dict(ok=True,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='build',core=core,plan=plan,
                    bindings={},power_source=preserved[0],preserved_poles=preserved,power_preview=power,defense_scope_id=scope,defense_preview=defense,
                    input_logistics='assisted finite normal Iron/Copper/Steel buffer courier; both ammunition tiers produced by real assemblers',
                    rated=core['rated'],prepared_tick=obs['tick'])
            return {'ok':False,'reason':'no_bounded_powered_defended_piercing_factory_site','failures':failures[-8:],
                'candidate_count':len(candidates),'land_legal_count':len(legal)}
        except(OSError,KeyError,ValueError,TypeError)as exc:return {'ok':False,'reason':'piercing_factory_prepare_context_invalid','detail':str(exc)}

    def _bind(self,obs):
        actual={(q['name'],*xy(q)):q for q in obs['entities']};changed=False
        for q in self.state['plan']['entities']:
            e=actual.get((q['name'],*xy(q)));unit=self.state['bindings'].get(_key(q))
            if unit and(not e or e.get('unit_number')!=unit):return {'status':'blocked','reason':'piercing_owned_asset_missing_or_replaced','entity':q}
            if e:
                if type(e.get('unit_number'))is not int or e.get('direction',0)!=q.get('direction',0):return {'status':'blocked','reason':'piercing_owned_configuration_changed'}
                if not unit:self.state['bindings'][_key(q)]=e['unit_number'];changed=True
        if changed:self._save()
        return None

    def _native(self,obs,body):
        return self.game.query('local x=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],core=self.state['core'],
            bindings=self.state['bindings'],configure=False))+');'+body)

    def _power(self,obs,board):
        copy=deepcopy(board);copy['blocks'].pop(BLOCK,None)
        return self.game.query(RADAR_POWER_LUA.replace('PAYLOAD',_payload(dict(world_id=obs['world_id'],source=self.state['power_source'],
            preserved_poles=self.state['preserved_poles'],scheduled_watts=planned_power_watts(self.catalog,obs,copy),additional_watts=0))))

    def _supply(self,obs,native):
        if native['output_full_magazines']>=TARGET:return None
        rows={xy(q):q for q in native['input_buffers']}
        buffers=sorted(self.state['core']['input_buffers'],key=lambda q:'iron-plate'in q['caps'])
        for buffer in buffers:
            live=rows[xy(buffer['chest'])]
            for item,cap in buffer['caps'].items():
                current=live['contents'].get(item,0)
                if current>=max(2,cap//4):continue
                held=obs.get('inventory',{}).get(item,0)
                if not held:return self.bootstrap.ensure_item(obs,item,min(50,cap-current))
                return dict(type='supply_piercing_input',name='iron-chest',position=buffer['chest']['position'],item=item,
                    count=int(min(50,held,cap-current)),expected_entity_unit=live['unit_number'],expected_entity_world_id=obs['world_id'],
                    expected_actor_unit=obs.get('actor_unit_number'),expected_catalog_fingerprint=self.catalog.fingerprint,
                    reason='normally courier already produced raw plates into bounded local ammunition input buffer')
        return None

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;self._save()
        if not self._identity(obs):return {'status':'blocked','reason':'piercing_factory_world_or_catalog_changed'}
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:return {'status':'blocked','reason':'piercing_checkpoint_changed'}
        bound=self._bind(obs)
        if bound:return bound
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        old=board['blocks'].get(BLOCK)
        if old is not None and old!=self.state['plan']:return {'status':'blocked','reason':'piercing_registered_plan_changed'}
        if old is None:board['blocks'][BLOCK]=deepcopy(self.state['plan']);_atomic_json(board_path,board)
        defense=self.perimeter.ensure_coverage(obs,self.state['defense_scope_id'],[self.state['core']['reserved_area']])
        if defense is not None:return defense
        power=self._power(obs,board)
        if not power.get('ok'):return {'status':'blocked','reason':'piercing_native_power_margin_changed','evidence':power}
        if self.state['phase']=='build':
            ready=self.builder.ensure_plan(obs,self.state['plan'])
            if ready.get('status')!='succeeded':return ready
            self.state['phase']='configure';self._save()
        controls=self._native(obs,PIERCING_CONTROL_LUA)
        if not controls.get('ok'):
            return dict(type='configure_piercing_factory',expected_world_id=obs['world_id'],expected_actor_unit=obs.get('actor_unit_number'),
                expected_catalog_fingerprint=self.catalog.fingerprint,reason='configure owned native stock stops, real circuit wires and chest bars before inputs')
        native=self._native(obs,PIERCING_INSPECT_LUA)
        if not native.get('ok'):return {'status':'blocked','reason':'piercing_native_factory_inspection_failed','evidence':native}
        if self.state['phase']=='configure':
            if(any(q['products_finished']!=0 or any(q['output'].values())for q in native['machines'])or native['yellow_on_belts']
                    or native['red_on_belts']or native['output_full_magazines']or native['red_in_hands']):
                return {'status':'blocked','reason':'piercing_empty_paid_machine_baseline_required_before_input_delivery'}
            self.state['baseline']={str(q['unit_number']):q['products_finished']for q in native['machines']}
            self.state['phase']='feeding';self._save()
        if self.commissioned:
            proof=self.proof(obs)
            return {'status':'succeeded','evidence':proof}if proof else{'status':'blocked','reason':'piercing_commissioned_native_proof_missing'}
        proof=self.proof(obs)
        if not proof:
            supply=self._supply(obs,native)
            if supply:return supply
            return {'status':'waiting','reason':'waiting_for_real_local_yellow_and_all_four_red_machine_output','evidence':self.last_failure}
        self.state['phase']='verified';self.state['production_witness']=proof['production_witness'];self.state['commissioning_proof']=proof;self._save()
        return {'status':'succeeded','reason':'real_local_yellow_and_piercing_factory_commissioned_with_native_stock_caps','evidence':proof}

    def proof(self,obs):
        self.last_failure=None
        if not self.state or not self._identity(obs)or not self.state.get('baseline'):return None
        board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id']or board['blocks'].get(BLOCK)!=self.state['plan']:return None
        native=self._native(obs,PIERCING_INSPECT_LUA);controls=self._native(obs,PIERCING_CONTROL_LUA)
        if not native.get('ok')or not controls.get('ok'):self.last_failure=native if not native.get('ok')else controls;return None
        if any(q['products_finished']<=self.state['baseline'].get(str(q['unit_number']),math.inf)for q in native['machines']):
            self.last_failure={'reason':'all_six_actual_machine_product_counters_must_advance','machines':native['machines']};return None
        witness=self.state.get('production_witness')
        if not witness:
            if native['output_full_magazines']<=0:return None
            witness={'tick':native['tick'],'machine_units':[q['unit_number']for q in native['machines']],
                'red_chest_stock':native['output_full_magazines'],'yellow_transport_witness':native['yellow_on_belts']}
        coverage=self.perimeter.coverage_proof(obs,self.state['defense_scope_id'])
        if not coverage:return None
        chest=self.state['core']['chest']
        return dict(tick=native['tick'],source=dict(name='iron-chest',kind='item',item=PRODUCT,direction='output',
            position=chest['position'],unit_number=self.state['bindings'][_key(chest)],world_id=obs['world_id'],defense_scope_id=self.state['defense_scope_id']),
            native=native,limits=controls,rated=self.state['rated'],production_witness=witness,
            input_logistics=self.state['input_logistics'],defense_coverage=coverage,
            scope='actual native six-machine ammo production and capped owned output; finite assisted raw logistics, no sustained-rate certification')

    def upkeep(self,obs):
        if not self.commissioned:return None
        if not self._identity(obs):return {'status':'blocked','reason':'piercing_factory_world_or_catalog_changed'}
        native=self._native(obs,PIERCING_INSPECT_LUA)
        if not native.get('ok'):return {'status':'blocked','reason':'piercing_factory_needs_owned_asset_recovery','evidence':native}
        controls=self._native(obs,PIERCING_CONTROL_LUA)
        if not controls.get('ok'):return {'status':'blocked','reason':'piercing_factory_native_controls_changed','evidence':controls}
        return self._supply(obs,native)

    def ensure_full_magazines(self,obs,count):
        if type(count)is not int or not 1<=count<=500:raise ValueError('piercing target must be1..500 full normal magazines')
        if not self.commissioned:return {'status':'waiting','reason':'piercing_ammo_capability_must_commission_real_factory_first'}
        proof=self.proof(obs)
        if not proof:return {'status':'blocked','reason':'piercing_factory_fresh_proof_missing','evidence':self.last_failure}
        native=proof['native'];needed=count-native['actor_full_red']
        if needed<=0:return None
        if native['output_full_magazines']:
            from .factory_ammo_transfer import factory_magazine_action
            return factory_magazine_action(self.game,obs,min(50,needed,native['output_full_magazines']),native['output_slots'][:16])
        return {'status':'waiting','reason':'piercing_factory_output_empty_upkeep_required_before_launch'}


NATIVE_ENTITIES=r'''
local function reject(reason)return {ok=false,status="blocked",reason=reason}end
if not d or d.world_id~=x.world_id then return reject("piercing_world_changed")end
if x.configure and(not a or not a.valid or a.unit_number~=x.actor_unit or a.force~=f or a.surface~=s)then return reject("piercing_actor_changed")end
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local live={}
for _,q in ipairs(x.core.entities)do local e=target(q.position,q.name);local unit=x.bindings[key(q)]
 if not unit or not e or not e.valid or e.unit_number~=unit or e.force~=f or e.surface~=s
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"
  or(e.type~="electric-pole"and e.direction~=(q.direction or 0))then return reject("piercing_owned_asset_missing_or_changed")end
 if q.recipe then local r=e.get_recipe();if not r or r.name~=q.recipe then return reject("piercing_recipe_changed")end end
 live[key(q)]=e
end
'''

PIERCING_CONTROL_LUA=NATIVE_ENTITIES+r'''
if not f.technologies["circuit-network"]or not f.technologies["circuit-network"].researched then return reject("piercing_normal_circuit_research_required")end
local chest=live[key(x.core.chest)];local inv=chest.get_inventory(defines.inventory.chest)
local size=prototypes.item["piercing-rounds-magazine"].stack_size
local slots=math.ceil(200/size)
if slots~=x.core.bar_slots or not inv or #inv<slots then return reject("piercing_native_stock_capacity_changed")end
local expected={};local connectors={};local all_edges={}
local function node(e,color)
 local id=color=="red"and defines.wire_connector_id.circuit_red or defines.wire_connector_id.circuit_green
 local k=e.unit_number..":"..color;local conn=e.get_wire_connector(id,x.configure)
 if not conn then return nil,k end;connectors[k]=conn;return conn,k
end
for _,edge in ipairs(x.core.wire_edges)do local first=live[key(edge.a)];local last=live[key(edge.b)]
 local ca,ka=node(first,edge.color);local cb,kb=node(last,edge.color)
 if not ca or not cb then return reject("piercing_circuit_not_configured")end
 if not ca.can_wire_reach(cb)then return reject("piercing_wire_native_reach_changed")end
 expected[ka]=expected[ka]or{};expected[kb]=expected[kb]or{};expected[ka][kb]=true;expected[kb][ka]=true
 all_edges[#all_edges+1]={a=ca,b=cb}
end
for k,conn in pairs(connectors)do for _,link in pairs(conn.connections)do
 local other=link.target;local color=other.wire_connector_id==defines.wire_connector_id.circuit_red and"red"or"green"
 local other_key=other.owner.unit_number..":"..color
 if not expected[k]or not expected[k][other_key]then return reject("piercing_foreign_or_unplanned_circuit_connection")end
end end
for _,q in ipairs(x.core.entities)do local e=live[key(q)]
 for _,color in ipairs({"red","green"})do
  local id=color=="red"and defines.wire_connector_id.circuit_red or defines.wire_connector_id.circuit_green
  local conn=e.get_wire_connector(id,false)
  if conn and #conn.connections>0 and not expected[e.unit_number..":"..color]then return reject("piercing_foreign_or_unplanned_circuit_connection")end
 end
end
local function behavior(e)return x.configure and e.get_or_create_control_behavior()or e.get_control_behavior()end
local function condition(e,item,limit)
 local cb=behavior(e);if not cb then return false end
 if x.configure then
  cb.circuit_condition={first_signal={type="item",name=item,quality="normal"},comparator="<",constant=limit}
  cb.circuit_enable_disable=true
 end
 local q=cb.circuit_condition;q=q and(q.condition or q)
 local quality=q and q.first_signal and q.first_signal.quality
 local normal=quality==nil or quality=="normal"or(type(quality)~="string"and quality.name=="normal")
 return cb.circuit_enable_disable and q and q.first_signal and q.first_signal.name==item
  and normal and q.comparator=="<"and q.constant==limit and not q.second_signal
end
local function run()
 if x.configure then
  for _,edge in ipairs(all_edges)do if not edge.a.is_connected_to(edge.b)and not edge.a.connect_to(edge.b,true,defines.wire_origin.player)then error("wire_connection_failed")end end
  inv.set_bar(slots+1)
 end
 for _,edge in ipairs(all_edges)do if not edge.a.is_connected_to(edge.b)then return reject("piercing_circuit_not_configured")end end
 local cb=behavior(chest);if not cb then return reject("piercing_output_stock_not_read")end
 if x.configure then cb.read_contents=true end
 if not cb.read_contents or inv.get_bar()~=slots+1 then return reject("piercing_output_stock_limit_changed")end
 for _,q in ipairs(x.core.input_buffers)do local e=live[key(q.chest)];local inventory=e.get_inventory(defines.inventory.chest)
  if x.configure then inventory.set_bar(q.bar_slots+1)end
  if inventory.get_bar()~=q.bar_slots+1 then return reject("piercing_input_buffer_bar_changed")end
 end
 for _,q in ipairs(x.core.machines)do if not condition(live[key(q)],"piercing-rounds-magazine",200)then return reject("piercing_machine_stock_stop_changed")end end
 local out={};for _,q in ipairs(x.core.output_arms)do out[key(q)]=true end
 for _,q in ipairs(x.core.global_arms)do local e=live[key(q)]
  if x.configure then e.inserter_stack_size_override=1;local b=behavior(e);b.circuit_set_stack_size=false
   b.circuit_read_hand_contents=not not out[key(q)]
   if out[key(q)]then b.circuit_hand_read_mode=defines.control_behavior.inserter.hand_read_mode.hold end
  end
  if e.inserter_stack_size_override~=1 or not condition(e,"piercing-rounds-magazine",200)then return reject("piercing_arm_stock_stop_changed")end
 end
 for _,q in ipairs(x.core.feeders)do local e=live[key(q.arm)];local c=live[key(q.chest)];local b=behavior(c)
  if not b then return reject("piercing_local_yellow_stock_not_read")end
  if x.configure then b.read_contents=true;e.inserter_stack_size_override=1 end
  if not b.read_contents or e.inserter_stack_size_override~=1 or not condition(e,"firearm-magazine",q.limit)then return reject("piercing_local_yellow_limit_changed")end
 end
 return {ok=true,status="succeeded",tick=game.tick,target=200,chest_bar_capacity=slots*size,hand_stack_limit=1,
  local_yellow_limit=8,quantity_scope="native chest/hand signal stops plus hard output chest bar; internal machine buffers are separately reported"}
end
if x.configure then local ok,result=pcall(run);if not ok then return reject("piercing_configuration_outcome_unknown")end;return result end
return run()
'''

PIERCING_INSPECT_LUA=NATIVE_ENTITIES+r'''
local function inventory(inv,allowed)
 local result={}
 for i=1,#inv do local st=inv[i]
  if st.valid_for_read then
   if not allowed[st.name]or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
    or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data
    or(st.prototype.type=="ammo"and st.ammo~=st.prototype.magazine_size)then return nil end
   result[st.name]=(result[st.name]or 0)+st.count
  end
 end;return result
end
local rows={};local buffers={};local yellow=0;local red=0;local output_capacity=0;local hands=0
for _,q in ipairs(x.core.machines)do local e=live[key(q)]
 if not e.is_connected_to_electric_network()or e.energy<=0 then return reject("piercing_machine_unpowered")end
 local out=e.get_output_inventory();local values=inventory(out,{[q.recipe]=true})
 if not values then return reject("piercing_machine_output_contaminated")end
 output_capacity=output_capacity+#out*prototypes.item[q.recipe].stack_size
 rows[#rows+1]={unit_number=e.unit_number,recipe=q.recipe,products_finished=e.products_finished,output=values}
end
for _,q in ipairs(x.core.arms)do local e=live[key(q)]
 if not e.is_connected_to_electric_network()or e.energy<=0 then return reject("piercing_inserter_unpowered")end
 local st=e.held_stack;if st.valid_for_read and st.name=="piercing-rounds-magazine"then hands=hands+st.count end
end
for _,q in ipairs(x.core.input_buffers)do local allowed={};for item in pairs(q.caps)do allowed[item]=true end
 if q.caps["steel-plate"]then allowed["firearm-magazine"]=true end
 local e=live[key(q.chest)];local values=inventory(e.get_inventory(defines.inventory.chest),allowed)
 if not values then return reject("piercing_input_buffer_contaminated")end
 buffers[#buffers+1]={position=e.position,unit_number=e.unit_number,contents=values}
end
for _,q in ipairs(x.core.yellow_belts)do local e=live[key(q)];for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents())do
 if item.name~="firearm-magazine"or(item.quality and item.quality~="normal")then return reject("piercing_local_yellow_belt_contaminated")end;yellow=yellow+item.count
end end end
for _,q in ipairs(x.core.red_belts)do local e=live[key(q)];for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents())do
 if item.name~="piercing-rounds-magazine"or(item.quality and item.quality~="normal")then return reject("piercing_red_belt_contaminated")end;red=red+item.count
end end end
local chest=live[key(x.core.chest)];local inv=chest.get_inventory(defines.inventory.chest)
local values=inventory(inv,{["piercing-rounds-magazine"]=true})
if not values then return reject("piercing_output_chest_contaminated")end
local slots={};local full=0
for i=1,#inv do local st=inv[i];if st.valid_for_read then full=full+st.count;slots[#slots+1]=i end end
if full>200 then return reject("piercing_output_chest_exceeds_native_cap")end
local actor_full=0;local main=a and a.valid and a.get_main_inventory()
if main then for i=1,#main do local st=main[i]
 if st.valid_for_read and st.name=="piercing-rounds-magazine"and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
  and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data then
  actor_full=actor_full+st.count-(st.ammo<st.prototype.magazine_size and 1 or 0)end
end end
return {ok=true,tick=game.tick,machines=rows,input_buffers=buffers,yellow_on_belts=yellow,red_on_belts=red,
 output_full_magazines=full,output_slots=slots,actor_full_red=actor_full,red_in_hands=hands,machine_output_slot_capacity=output_capacity,
 output_chest_cap=200,scope="actual normal machine outputs and finite buffers; no measured sustained rate"}
'''


def _action_context(game,action,kind):
    base={'type','expected_world_id','expected_actor_unit','expected_catalog_fingerprint','reason'}
    if kind=='supply_piercing_input':
        base={'type','name','position','item','count','expected_entity_unit','expected_entity_world_id','expected_actor_unit','expected_catalog_fingerprint','reason'}
    if(not isinstance(action,dict)or set(action)-base or action.get('type')!=kind
            or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or getattr(game,'backend',None)not in('assisted','character')):raise ValueError('exact owned piercing factory action required')
    root=Path(game.cfg.runtime_dir);state=json.loads((root/'piercing-ammo.json').read_text(encoding='utf-8'))
    world=action.get('expected_world_id',action.get('expected_entity_world_id'))
    catalog=WorldCatalog.from_dict(json.loads((root/'catalog.json').read_text(encoding='utf-8')))
    board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
    if(not world or world!=getattr(game,'expected_world',None)or state.get('world_id')!=world
            or state.get('catalog_fingerprint')!=action.get('expected_catalog_fingerprint')or catalog.fingerprint!=state['catalog_fingerprint']
            or board.get('world_id')!=world or board.get('catalog_fingerprint')!=catalog.fingerprint or board['blocks'].get(BLOCK)!=state['plan']):
        raise ValueError('piercing factory known-file/board/world/catalog authority changed')
    return state


def execute_configure_piercing(game,action):
    state=_action_context(game,action,'configure_piercing_factory')
    if any(type(state['bindings'].get(_key(q)))is not int for q in state['core']['entities']):raise ValueError('all paid factory units must be bound before configuration')
    args=dict(world_id=state['world_id'],core=state['core'],bindings=state['bindings'],configure=True,actor_unit=action['expected_actor_unit'])
    try:result=game.query('local x=helpers.json_to_table('+_payload(args)+');'+PIERCING_CONTROL_LUA)
    except Exception:return {'ok':False,'status':'blocked','reason':'piercing_configuration_outcome_unknown'}
    if not isinstance(result,dict)or type(result.get('ok'))is not bool:return {'ok':False,'status':'blocked','reason':'piercing_configuration_outcome_unknown'}
    return result


def execute_supply_piercing_input(game,action):
    state=_action_context(game,action,'supply_piercing_input')
    if state.get('phase')not in('feeding','verified')or action.get('name')!='iron-chest'or type(action.get('count'))is not int or not 1<=action['count']<=50:
        raise ValueError('bounded plate courier only after empty native production baseline')
    matching=[q for q in state['core']['input_buffers']if q['chest']['position']==action.get('position')and action.get('item')in q['caps']]
    if len(matching)!=1:raise ValueError('raw item must target its exact registered input buffer')
    spec=matching[0];unit=state['bindings'].get(_key(spec['chest']))
    if type(unit)is not int or unit!=action.get('expected_entity_unit'):raise ValueError('piercing input buffer unit changed')
    from .storage_transfer import execute_deposit,DEPOSIT_LUA
    mapped={k:v for k,v in action.items()if k not in('expected_actor_unit','expected_catalog_fingerprint')};mapped['type']='deposit'
    guard=dict(actor=action['expected_actor_unit'],cap=spec['caps'][action['item']])
    prefix='local factory_guard=helpers.json_to_table('+_payload(guard)+''');
local function factory_reject(reason)return {ok=false,status="blocked",reason=reason,moved=0,requested=x.count,
 unit_number=x.expected_entity_unit,world_id=x.expected_entity_world_id,item=x.item,quality="normal"}end
if not a or not a.valid or a.unit_number~=factory_guard.actor then return factory_reject("storage_actor_changed")end
local buffer=target(x.position,x.name);local inventory=buffer and buffer.valid and buffer.get_inventory(defines.inventory.chest)
if not inventory or inventory.get_item_count{name=x.item,quality="normal"}+x.count>factory_guard.cap then return factory_reject("storage_chest_changed")end
'''
    class Guarded:
        def __getattr__(self,name):return getattr(game,name)
        def query(self,body):
            if body.count(DEPOSIT_LUA)!=1:raise ValueError('known conserved deposit helper changed')
            return game.query(body.replace(DEPOSIT_LUA,prefix+DEPOSIT_LUA,1))
    return execute_deposit(Guarded(),mapped)
