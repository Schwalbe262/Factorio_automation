"""Automatic engines, shared iron and normally produced steel, fixed PCB ports."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_ai.world_catalog import _yield
from .board import entity
from .bus_tap import BusTap,reconstruct_bus
from .compact_green import _machine_at,_spread,_transport
from .industrial import PlanningBudget,free_tap_parameters
from .manufacturing import connect_power_groups
from .planning import overlap,preflight,survey_slots
from .smelting import merge_entities,port
from .steel_production import SteelProduction
from .transport import VECTORS,plan_connection,underground_proof,xy


TAP_NAME='engine-iron'
SCOPE_ID='engine-production'


def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _payload(value):return json.dumps(json.dumps(value,separators=(',',':')))
def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def engine_rates(catalog,count=2):
    if type(count)is not int or not 1<=count<=4:raise ValueError('one to four engine assemblers supported')
    expected={'engine-unit':{'steel-plate','iron-gear-wheel','pipe'},'iron-gear-wheel':{'iron-plate'},'pipe':{'iron-plate'}}
    machine=catalog.entities['assembling-machine-1'];speed=machine['crafting_speed'];recipes={}
    if type(speed)not in(int,float)or not math.isfinite(speed)or speed<=0:raise ValueError('positive finite native crafting speed required')
    for name,items in expected.items():
        recipe=catalog.recipes[name]
        if (len(recipe['products'])!=1 or recipe['products'][0]['name']!=name
                or recipe['products'][0].get('type','item')!='item'
                or {p['name']for p in recipe['ingredients']}!=items
                or any(p.get('type','item')!='item'or type(p['amount'])not in(int,float)or not math.isfinite(p['amount'])or p['amount']<=0 for p in recipe['ingredients'])
                or type(recipe['energy'])not in(int,float)or not math.isfinite(recipe['energy'])or recipe['energy']<=0
                or recipe.get('category')not in machine.get('crafting_categories',[])):
            raise ValueError('installed ordinary engine, gear and pipe recipes required')
        recipes[name]=(recipe,{p['name']:p['amount']for p in recipe['ingredients']},_yield(recipe['products'][0]))
    recipe,ingredients,yield_=recipes['engine-unit'];cycles=60*speed/recipe['energy']
    needs={name:amount*cycles*count for name,amount in ingredients.items()};iron=0;component_nominal_iron=0
    for name in('iron-gear-wheel','pipe'):
        component,raw,yield_component=recipes[name];component_cycles=needs[name]/yield_component
        raw_rate=raw['iron-plate']*component_cycles;iron+=raw_rate
        component_nominal_iron+=60*speed/component['energy']*raw['iron-plate']
        if raw_rate>30 or needs[name]>30 or component_cycles>60*speed/component['energy']:
            raise ValueError('one normal input/output inserter and one component assembler cannot meet engine demand')
    if cycles*(ingredients['iron-gear-wheel']+ingredients['pipe'])>30 or cycles*ingredients['steel-plate']>30 or cycles*yield_>30:
        raise ValueError('normal engine inserter budgets exceeded')
    return {'engines_per_minute':cycles*count*yield_,'iron_per_minute':iron,'steel_per_minute':needs['steel-plate'],
            'gear_per_minute':needs['iron-gear-wheel'],'pipe_per_minute':needs['pipe'],
            'engine_assembler_count':count,'component_assembler_count':2,
            'component_nominal_iron_per_minute':component_nominal_iron,'conservative_inserter_budget_per_minute':30,
            'capacity_scope':'catalog nameplate, subject to shared iron/steel supply; no measured rate certification'}


def engine_core(catalog,x,y,count=2):
    """Four future engine slots; initial two engines plus shared gear/pipe machines."""
    rated=engine_rates(catalog,count)
    if any(type(v)not in(int,float)or not math.isfinite(v)or abs(v%1-.5)>1e-6 for v in(x,y)):
        raise ValueError('half-tile engine anchors required')
    es=[];pairs=[]
    def add(name,dx,dy,direction=0,**extra):
        e=dict(entity(name,x+dx,y+dy,direction),**extra);es.append(e);return e
    def belt(dx,dy,d=4):return add('transport-belt',dx,dy,d)
    def arm(dx,dy,d=0,long=False):return add('long-handed-inserter'if long else'inserter',dx,dy,d)
    def pair(ax,bx,yy,d):
        add('underground-belt',ax,yy,d,belt_to_ground_type='input')
        add('underground-belt',bx,yy,d,belt_to_ground_type='output')
        pairs.append({'input':[x+ax,y+yy],'output':[x+bx,y+yy],'direction':d,'span':abs(bx-ax)})
    for xx in range(-3,10):belt(xx,0)
    gear=add('assembling-machine-1',3,3,recipe='iron-gear-wheel')
    pipe=add('assembling-machine-1',9,3,recipe='pipe')
    arm(3,1);arm(9,1);arm(1,3,4);arm(11,3,12)
    for yy in range(3,7):belt(0,yy,8)
    for yy in range(3,8):belt(12,yy,8)
    # Pipe returns below the mixer, crossing native input arms in two straight spans.
    for xx in(12,11,6,5):belt(xx,8,12)
    pair(10,7,8,12);pair(4,1,8,12);belt(0,8,12);belt(-1,8,0)
    for xx in range(-1,22):
        if xx not in(11,12,13):belt(xx,7)
    pair(11,13,7,4)
    # Steel approaches from the east. Long arms pick this dedicated belt over the mixer.
    for xx in range(2,26):
        if xx not in(11,12,13):belt(xx,6,12)
    pair(13,11,6,12)
    engines=[];engine_arms=[]
    for i in range(count):
        xx=3+6*i;engines.append(add('assembling-machine-1',xx,10,recipe='engine-unit'))
        engine_arms += [arm(xx,8),arm(xx-1,8,0,long=True),arm(xx+2,10,12)]
        for yy in range(10,16):belt(xx+3,yy,8)
        add('small-electric-pole',xx-2,9);add('small-electric-pole',xx+2,9)
    for xx in range(6,27):belt(xx,16)
    for xx,yy in((1,1),(5,5),(7,1),(11,1)):add('small-electric-pole',xx,yy)
    ports=[port('iron-plate',x-3,y,4),port('steel-plate',x+25,y+6,12),
           dict(port('engine-unit',x+26,y+16,4),direction='output')]
    return _plan(es,ports,machines=[gear,pipe]+engines,engine_machines=engines,engine_arms=engine_arms,
        underground_pairs=pairs,reserved_area=[[x-4.5,y-.5],[x+26.5,y+18.5]],rated=rated,
        mixed_feed_lanes={'left':'iron-gear-wheel','right':'pipe'},anchor={'x':x,'y':y},
        extension_slots=[entity('assembling-machine-1',x+3+6*i,y+10,recipe='engine-unit')for i in range(count,4)],
        layout_policy='fixed-engine-component-headers-v1')


def audit_engine_core(plan,catalog):
    errors=[];es=plan['entities'];geometry=FactoryBuilder.__new__(FactoryBuilder)
    boxes=[geometry._occupied_by_plan([e])for e in es]
    if sum(map(len,boxes))!=len(set().union(*boxes)):errors.append('prospective_footprint_overlap')
    area=plan['reserved_area']
    if any(not(area[0][0]<=x-.5 and x+.5<=area[1][0]and area[0][1]<=y-.5 and y+.5<=area[1][1])for box in boxes for x,y in box):
        errors.append('entity_outside_reservation')
    for slot in plan['extension_slots']:
        if geometry._occupied_by_plan([slot])&set().union(*boxes):errors.append('future_engine_slot_occupied')
    belts,graph=_transport(es,plan['underground_pairs']);declared=set()
    for pair in plan['underground_pairs']:
        a,b=tuple(pair['input']),tuple(pair['output']);d=pair['direction'];dx,dy=VECTORS[d];span=pair['span']
        if not 2<=span<=5 or b!=(a[0]+span*dx,a[1]+span*dy):errors.append('invalid_underground_span')
        for p,role in((a,'input'),(b,'output')):
            declared.add(p);e=belts.get(p,{})
            if(e.get('name'),e.get('direction'),e.get('belt_to_ground_type'))!=('underground-belt',d,role):errors.append('missing_underground_mouth')
        if belts.get((a[0]-dx,a[1]-dy),{}).get('direction')!=d or belts.get((b[0]+dx,b[1]+dy),{}).get('direction')!=d:
            errors.append('underground_requires_straight_mouths')
        for p,e in belts.items():
            if p in(a,b)or e['name']!='underground-belt'or e['direction']!=d:continue
            ahead=(p[0]-a[0])*dx+(p[1]-a[1])*dy
            if(p[0]-a[0])*dy==(p[1]-a[1])*dx and 0<ahead<=span:errors.append('underground_pair_can_be_stolen')
    if declared!={p for p,e in belts.items()if e['name']=='underground-belt'}:errors.append('undeclared_underground_mouth')
    machines=plan['machines'];arms=[e for e in es if e['name']in('inserter','long-handed-inserter')]
    poles=[xy(e)for e in es if e['name']=='small-electric-pole']
    for e in machines+arms:
        if not any(abs(xy(e)[0]-px)<=2.5 and abs(xy(e)[1]-py)<=2.5 for px,py in poles):errors.append('unpowered_consumer:'+str(xy(e)))
    connected={poles[0]}if poles else set()
    while True:
        more={p for p in poles if any(math.dist(p,q)<=7 for q in connected)}
        if more<=connected:break
        connected|=more
    if len(connected)!=len(poles):errors.append('disconnected_internal_poles')
    lanes=defaultdict(set);supplied=defaultdict(set);ready=set();arm_targets=[]
    for p in plan['ports']:
        if p['direction']=='input':
            for lane in('left','right'):lanes[xy(p),lane].add(p['item'])
    for _ in range(len(machines)+2):
        _spread(lanes,graph)
        for arm in arms:
            p=xy(arm);dx,dy=VECTORS[arm['direction']];reach=2 if arm['name']=='long-handed-inserter'else 1
            pickup=(p[0]+dx*reach,p[1]+dy*reach);drop=(p[0]-dx*reach,p[1]-dy*reach)
            source,dest=_machine_at(machines,pickup),_machine_at(machines,drop)
            items={source['recipe']}if source and xy(source)in ready else set()
            if pickup in belts:items|=lanes[pickup,'left']|lanes[pickup,'right']
            if dest:
                items&={i['name']for i in catalog.recipes[dest['recipe']]['ingredients']};supplied[xy(dest)]|=items
            elif drop in belts:
                vx,vy=VECTORS[belts[drop]['direction']];side=(p[0]-drop[0])*vy-(p[1]-drop[1])*vx
                lanes[drop,'right'if side>0 else'left']|=items
        ready|={xy(m)for m in machines if{i['name']for i in catalog.recipes[m['recipe']]['ingredients']}<=supplied[xy(m)]}
    _spread(lanes,graph)
    for m in machines:
        if xy(m)not in ready:errors.append('unreachable_ingredients:'+str(xy(m)))
    for m in plan['engine_machines']:
        pickup=(xy(m)[0],xy(m)[1]-3)
        for lane,item in plan['mixed_feed_lanes'].items():
            if lanes[pickup,lane]!={item}:errors.append('engine_component_lane_mismatch:'+str(pickup)+':'+lane)
    output=plan['ports'][-1]
    if(lanes[xy(output),'left']|lanes[xy(output),'right'])!={'engine-unit'}:errors.append('engine_output_unreachable_or_contaminated')
    for arm in arms:
        dx,dy=VECTORS[arm['direction']];reach=2 if arm['name']=='long-handed-inserter'else 1
        p=xy(arm);pickup={'x':p[0]+dx*reach,'y':p[1]+dy*reach};drop={'x':p[0]-dx*reach,'y':p[1]-dy*reach}
        source=_machine_at(machines,(pickup['x'],pickup['y']));dest=_machine_at(machines,(drop['x'],drop['y']))
        items={source['recipe']}if source else lanes[(pickup['x'],pickup['y']),'left']|lanes[(pickup['x'],pickup['y']),'right']
        if dest:items&={i['name']for i in catalog.recipes[dest['recipe']]['ingredients']}
        arm_targets.append(dict(arm,pickup_position=pickup,drop_position=drop,allowed_items=sorted(items)))
    return {'ok':not errors,'errors':errors,'machine_count':len(machines),'arm_targets':arm_targets,
            'belt_contracts':[dict(e,allowed_items=sorted(lanes[p,'left']|lanes[p,'right']))for p,e in belts.items()],
            'rated':engine_rates(catalog,len(plan['engine_machines']))}


def _core_keepout(core):
    """Virtual routing footprints only; never included in a construction plan."""
    a,b=core['reserved_area'];allowed=set()
    for p in core['ports'][:2]:
        px,py=xy(p)
        for i in range(int(b[0]-a[0])):
            xx=a[0]+.5+i
            if(p['facing']==4 and xx<=px)or(p['facing']==12 and xx>=px):allowed.add((xx,py))
    return [entity('pcb-reserved',a[0]+.5+i,a[1]+.5+j)
            for i in range(int(b[0]-a[0]))for j in range(int(b[1]-a[1]))
            if(a[0]+.5+i,a[1]+.5+j)not in allowed]


class EngineProduction:
    def __init__(self,game,builder,catalog,perimeter,*,bus_factory=None,science=None,green=None,tap=None,steel=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.tap=tap if tap is not None else BusTap(game,builder,catalog,bus_factory=bus_factory,science=science,green=green)
        self.steel=steel if steel is not None else SteelProduction(game,builder,catalog)
        self.path=Path(game.cfg.runtime_dir)/'engine-production.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_proof_failure=None

    def _identity(self,obs,value):
        return(obs.get('ok',True)and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
               and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def _save(self):_atomic_json(self.path,self.state)

    def _steel_source(self,obs):
        if not self._identity(obs,self.steel.state)or self.steel.state.get('phase')!='verified':
            return {'ok':False,'reason':'commissioned_normal_steel_factory_required'}
        proof=self.steel.proof(obs)
        if not proof:return {'ok':False,'reason':'current_native_steel_production_proof_required','detail':getattr(self.steel,'last_proof_failure',None)}
        ports=[p for p in self.steel.state['core']['ports']if p.get('direction')=='output'and p['item']=='steel-plate']
        if len(ports)!=1:return {'ok':False,'reason':'one_fixed_typed_steel_output_required'}
        source=ports[0];live=next((e for e in obs['entities']if e['name']=='transport-belt'and xy(e)==xy(source)),None)
        if not live or type(live.get('unit_number'))is not int or live.get('direction')!=source['facing']:
            return {'ok':False,'reason':'exact_native_steel_output_required'}
        return {'ok':True,'source':dict(source,name='transport-belt',unit_number=live['unit_number'],world_id=obs['world_id']),
                'furnace_units':proof['furnace_units'],'plan_revision':_hash(self.steel.state['plan']),
                'nominal_steel_per_minute':proof['rated']['steel_per_minute']}

    def prepare(self,obs):
        """Whole prospective tap, both adapters, future slots, power and defense."""
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'engine_world_or_catalog_changed'}
        required=('engine-unit','iron-gear-wheel','pipe','assembling-machine-1','inserter','long-handed-inserter')
        if not obs.get('technologies',{}).get('engine')or not all(obs.get('enabled_recipes',{}).get(r)for r in required):
            return {'ok':False,'reason':'native_engine_technology_and_component_machine_recipes_required'}
        steel=self._steel_source(obs)
        if not steel.get('ok'):return steel
        try:
            docs=self.tap._documents();bus=docs['factory']['bus'];board=docs['board'];reconstruct_bus(bus)
            if not all(self._identity(obs,d)for d in docs.values())or board.get('shared_main_bus')!=bus:
                return {'ok':False,'reason':'engine_bus_authority_identity_changed'}
            if self.tap.state and self.tap.state.get('phase')!='verified':return {'ok':False,'reason':'another_bus_tap_cutover_unfinished'}
            if any(p.get('name')==TAP_NAME for p in bus['ports']):return {'ok':False,'reason':'engine_tap_exists_without_factory_checkpoint'}
            rated=engine_rates(self.catalog);choices=free_tap_parameters(bus,name=TAP_NAME,item='iron-plate',
                demand_per_minute=rated['iron_per_minute'],priority='surplus')
            steel_area=self.steel.state['core']['reserved_area']
            x=math.ceil(steel_area[1][0])+8.5;y=math.floor(steel_area[0][1])-46.5
            anchors=[(x+dx,y+dy)for dx,dy in((0,0),(32,0),(0,-22),(32,-22),(0,22),(32,22))]
            cores=[engine_core(self.catalog,a,b)for a,b in anchors]
            for core in cores:
                audit=audit_engine_core(core,self.catalog)
                if not audit['ok']:return {'ok':False,'reason':'engine_core_audit_failed','evidence':audit}
        except(OSError,KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'engine_preparation_context_unavailable','detail':str(exc)}
        cores=[c for c in cores if not any(overlap(c['reserved_area'],b['reserved_area'])for b in board['blocks'].values()if b.get('reserved_area'))]
        budget=PlanningBudget(self.game);adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda entities:preflight(budget.game,self.builder,obs,entities))
        native=budget.game.query('''local rows={};for _,name in ipairs({"inserter","long-handed-inserter"})do
 local p=prototypes.entity[name];local reach=name=="inserter"and 1 or 2;local pickup=p.inserter_pickup_position;local drop=p.inserter_drop_position
 if math.floor(.5+(pickup.x or pickup[1]))~=0 or math.floor(.5+(pickup.y or pickup[2]))~=-reach
 or math.floor(.5+(drop.x or drop[1]))~=0 or math.floor(.5+(drop.y or drop[2]))~=reach then return {ok=false,reason="unsupported_native_engine_inserter_vectors"}end end
return {ok=true}''')
        if not native.get('ok'):return native
        land=survey_slots(budget.game,[[c['reserved_area']]for c in cores])
        if not land.get('ok'):return land
        cores=[cores[i-1]for i in land.get('legal',[])];failures=[];routes=0
        preview=BusTap(budget.game,adapter,self.catalog)
        for core in cores:
            if budget.expired():return budget.failure(failures=failures[-6:],route_attempts=routes)
            x,y=xy({'position':core['anchor']});future=engine_core(self.catalog,x,y,4)
            reserved=merge_entities(core['entities'],future['entities'])+_core_keepout(core)
            steel_route=plan_connection(budget.game,adapter,obs,steel['source'],core['ports'][1],reserved);routes+=1
            if not steel_route.get('ok'):
                failures.append({'stage':'steel_route','anchor':core['anchor'],'detail':steel_route});continue
            ranked=sorted(choices,key=lambda p:abs(p['splitter_y']-1-core['ports'][0]['position']['y']))
            for parameters in ranked[:4]:
                if budget.expired()or routes>=12:return budget.failure(failures=failures[-6:],route_attempts=routes)
                tap=preview.prepare(obs,parameters)
                if not tap.get('ok'):failures.append({'stage':'tap','parameters':parameters,'detail':tap});continue
                iron=next(p for p in tap['new_bus']['ports']if p['name']==TAP_NAME)
                iron_route=plan_connection(budget.game,adapter,obs,iron,core['ports'][0],reserved+steel_route['entities']+tap['added_entities']);routes+=1
                if not iron_route.get('ok'):failures.append({'stage':'iron_route','parameters':parameters,'detail':iron_route});continue
                es=merge_entities(core['entities'],steel_route['entities'],iron_route['entities'])
                power=connect_power_groups(budget.game,adapter,obs,[core],merge_entities(es,future['entities']))
                if not power.get('ok'):failures.append({'stage':'power','detail':power});continue
                es=merge_entities(es,power['entities'])
                whole=preflight(budget.game,self.builder,obs,merge_entities(es,tap['added_entities']),ignored_units=[tap['remove']['unit_number']])
                if not whole.get('ok'):failures.append({'stage':'whole_preflight','detail':whole});continue
                # Full reservation was surveyed, and the future hardware must also remain collision-free.
                future_check=preflight(budget.game,self.builder,obs,merge_entities(future['entities'],steel_route['entities'],iron_route['entities'],power['entities'],tap['added_entities']),ignored_units=[tap['remove']['unit_number']])
                if not future_check.get('ok'):failures.append({'stage':'future_preflight','detail':future_check});continue
                defense=self.perimeter.prepare_coverage(obs,SCOPE_ID,[core['reserved_area']])
                if not defense.get('ok'):return {'ok':False,'reason':'engine_defense_preflight_failed','evidence':defense}
                plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],
                    underground_pairs=core['underground_pairs']+steel_route['underground_pairs']+iron_route['underground_pairs'],
                    clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy='defended-shared-engine-v1')
                actual={(e['name'],*xy(e)):e for e in obs['entities']}
                bindings={_key(e):actual[(e['name'],*xy(e))]['unit_number']for e in es if(e['name'],*xy(e))in actual}
                return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'phase':'tap',
                    'plan':plan,'core':core,'audit':audit_engine_core(core,self.catalog),'rated':rated,
                    'parameters':parameters,'tap_installation':tap,'steel_source':steel['source'],
                    'steel_plan_revision':steel['plan_revision'],'steel_furnace_units':steel['furnace_units'],
                    'shared_steel_nominal_per_minute':steel['nominal_steel_per_minute'],'bindings':bindings,
                    'route_metrics':[dict(item='steel-plate',**steel_route['route_metrics']),dict(item='iron-plate',**iron_route['route_metrics'])],
                    'defense_preview':defense,'prepared_tick':obs.get('tick'),'route_attempts':routes}
        return {'ok':False,'reason':'no_engine_site_with_complete_native_material_connections',
                'evidence':{'failures':failures[-8:],'legal_sites':len(cores),'route_attempts':routes}}

    def _bind(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));old=self.state['bindings'].get(_key(spec))
            if old and(not live or live.get('unit_number')!=old):return {'status':'blocked','reason':'owned_engine_asset_missing_or_replaced','entity':spec}
            if live:
                if type(live.get('unit_number'))is not int:return {'status':'blocked','reason':'engine_native_unit_identity_missing'}
                if old is None:self.state['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def _reserve(self,obs):
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'))
        if not self._identity(obs,board):return {'status':'blocked','reason':'engine_board_identity_changed'}
        old=board['blocks'].get('engine_production');plan=self.state['plan']
        if old and old!=plan:return {'status':'blocked','reason':'engine_reserved_plan_changed'}
        if any(k!='engine_production'and overlap(plan['reserved_area'],b['reserved_area'])for k,b in board['blocks'].items()if b.get('reserved_area')):
            return {'status':'blocked','reason':'engine_reservation_conflict'}
        if not old:board['blocks']['engine_production']=plan;_atomic_json(path,board)
        return None

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'engine_world_or_catalog_changed'}
        if _hash(self.steel.state.get('plan'))!=self.state['steel_plan_revision']:
            return {'status':'blocked','reason':'engine_bound_steel_plan_changed'}
        if self.state['phase']=='tap':
            if self.tap.state.get('parameters')!=self.state['parameters']:
                if self.tap.state and self.tap.state.get('phase')!='verified':return {'status':'blocked','reason':'another_bus_tap_cutover_unfinished'}
                self.tap.state=deepcopy(self.state['tap_installation']);self.tap._save()
            result=self.tap.step(obs,self.state['parameters'])
            if result.get('status')!='succeeded':return result
            self.state['iron_source']=deepcopy(result['evidence']['port']);self.state['phase']='build';self._save()
        result=self._bind(obs)
        if result:return result
        result=self._reserve(obs)
        if result:return result
        result=self.perimeter.ensure_coverage(obs,SCOPE_ID,[self.state['core']['reserved_area']])
        if result is not None:return result
        if not self.state.get('construction_authorized'):
            check=preflight(self.game,self.builder,obs,self.state['plan']['entities'])
            if not check.get('ok'):return {'status':'blocked','reason':'engine_site_changed_after_defense','evidence':check}
            self.state['construction_authorized']=True;self._save()
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'waiting for normally supplied engine, gear and pipe production','evidence':self.last_proof_failure}
        self.state.update(phase='verified',commissioning_proof=proof);self._save()
        return {'status':'succeeded','reason':'normally supplied automatic engine production commissioned','evidence':proof}

    def proof(self,obs):
        self.last_proof_failure=None
        if not self.state or not self._identity(obs,self.state)or self.state.get('phase')=='tap':return None
        defense=self.perimeter.coverage_proof(obs,SCOPE_ID)
        if not defense:self.last_proof_failure={'reason':'engine_defense_coverage_missing'};return None
        if not underground_proof(obs,self.state['plan']):self.last_proof_failure={'reason':'engine_native_underground_pairs_unverified'};return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));unit=self.state['bindings'].get(_key(spec))
            if(not live or type(unit)is not int or live.get('unit_number')!=unit
                    or(spec['name']!='small-electric-pole'and live.get('direction')!=spec['direction'])):
                self.last_proof_failure={'reason':'engine_asset_identity_or_direction_changed','entity':spec};return None
        steel_proof=self.steel.proof(obs)
        if _hash(self.steel.state.get('plan'))!=self.state['steel_plan_revision']or not steel_proof:
            self.last_proof_failure={'reason':'engine_normal_steel_source_proof_missing'};return None
        if sorted(steel_proof.get('furnace_units',[]))!=sorted(self.state['steel_furnace_units']):
            self.last_proof_failure={'reason':'engine_bound_steel_furnace_units_changed'};return None
        def bound(e):return dict(e,unit_number=self.state['bindings'][_key(e)])
        args={'machines':[bound(e)for e in self.state['core']['machines']],
              'arms':[bound(e)for e in self.state['audit']['arm_targets']],
              'belts':[bound(e)for e in self.state['audit']['belt_contracts']],
              'iron_source':dict(self.state['iron_source'],name='transport-belt'),'steel_source':self.state['steel_source'],
              'outlet':bound(self.state['core']['ports'][-1]|{'name':'transport-belt'}),
              'component_feed':[{'x':xy(m)[0],'y':xy(m)[1]-3}for m in self.state['core']['engine_machines']],
              'ingredients':{r:{p['name']:True for p in self.catalog.recipes[r]['ingredients']}for r in('engine-unit','iron-gear-wheel','pipe')}}
        result=self.game.query(ENGINE_PROOF_LUA.replace('PAYLOAD',_payload(args)))
        if not result.get('ok'):self.last_proof_failure=result;return None
        return {'tick':result['tick'],'native':result,'rated':self.state['rated'],
            'shared_steel_nominal_per_minute':self.state['shared_steel_nominal_per_minute'],'defense_coverage':defense,
            'output':dict(self.state['core']['ports'][-1],name='transport-belt',unit_number=result['outlet_unit'],world_id=obs['world_id']),
            'scope':'native normally supplied automatic engines; nominal rate and shared steel ceiling are not measured throughput'}


ENGINE_PROOF_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local finished={}
local function exact(q)local e=target(q.position,q.name or"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e end
local function quality(q)return type(q)=="string"and q or(q and q.name)end
local function tile_equal(p,q)return math.floor(p.x)+.5==q.x and math.floor(p.y)+.5==q.y end
for _,q in ipairs(args.machines)do local e=exact(q);local recipe=e and e.get_recipe()
 if not e or not recipe or recipe.name~=q.recipe or e.products_finished<=0 or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="engine_machine_recipe_products_or_power_missing",position=q.position}end
 local input=e.get_inventory(defines.inventory.crafter_input);local output=e.get_output_inventory()
 if not input or not output then return {ok=false,reason="engine_native_machine_inventory_missing"}end
 for i=1,#input do local stack=input[i];if stack.valid_for_read and(not args.ingredients[q.recipe][stack.name]or stack.quality.name~="normal")then return {ok=false,reason="engine_machine_input_contaminated",position=q.position}end end
 for i=1,#output do local stack=output[i];if stack.valid_for_read and(stack.name~=q.recipe or stack.quality.name~="normal")then return {ok=false,reason="engine_machine_output_contaminated",position=q.position}end end
 finished[#finished+1]={unit_number=e.unit_number,recipe=recipe.name,products_finished=e.products_finished}
end
for _,q in ipairs(args.arms)do local e=exact(q)
 if not e or e.direction~=q.direction or not e.is_connected_to_electric_network()or e.energy<=0
 or not tile_equal(e.pickup_position,q.pickup_position)or not tile_equal(e.drop_position,q.drop_position)then return {ok=false,reason="engine_native_inserter_handoff_or_power_changed",position=q.position}end
 local stack=e.held_stack;if stack and stack.valid_for_read then local allowed={};for _,item in ipairs(q.allowed_items)do allowed[item]=true end
  if not allowed[stack.name]or stack.quality.name~="normal"then return {ok=false,reason="engine_inserter_held_item_contaminated",position=q.position}end
 end
end
local raw={};for _,q in ipairs(args.belts)do local e=exact(q);if not e or e.direction~=q.direction then return {ok=false,reason="engine_core_belt_identity_or_direction_changed"}end
 local allowed={};for _,name in ipairs(q.allowed_items)do allowed[name]=true end
 for lane=1,e.get_max_transport_line_index() do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if not allowed[row.name]or quality(row.quality)~="normal"then return {ok=false,reason="engine_core_belt_contaminated",position=q.position}end
  raw[row.name]=(raw[row.name]or 0)+row.count
 end end
end
for _,q in ipairs({args.iron_source,args.steel_source})do local e=exact(q)
 if not e or e.direction~=q.facing then return {ok=false,reason="engine_raw_source_identity_changed",item=q.item}end
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=q.item or quality(row.quality)~="normal"then return {ok=false,reason="engine_raw_source_contaminated",item=q.item}end
 end end
end
local q=args.iron_source.priority_splitter;local split=q and target(q.position,"splitter")
if not split or split.force~=f or split.surface~=s or split.unit_number~=q.unit_number or split.direction~=0
 or split.splitter_input_priority~="left"or split.splitter_output_priority~="left"then return {ok=false,reason="engine_surplus_priority_changed"}end
local components={{},{} };for _,p in ipairs(args.component_feed)do local e=target(p,"transport-belt")
 if not e or e.force~=f or e.direction~=4 then return {ok=false,reason="engine_native_component_feed_missing"}end
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=(lane==1 and"iron-gear-wheel"or"pipe")or quality(row.quality)~="normal"then return {ok=false,reason="engine_component_lane_contaminated"}end
  components[lane][row.name]=(components[lane][row.name]or 0)+row.count
 end end
end
if not components[1]["iron-gear-wheel"]or not components[2].pipe or not raw["iron-plate"]or not raw["steel-plate"]then return {ok=false,reason="engine_raw_or_component_buffers_empty"}end
local outlet=exact(args.outlet);if not outlet then return {ok=false,reason="engine_output_identity_changed"}end
local count=0;for lane=1,2 do for _,row in pairs(outlet.get_transport_line(lane).get_contents())do
 if row.name~="engine-unit"or quality(row.quality)~="normal"then return {ok=false,reason="engine_output_contaminated"}end;count=count+row.count end end
if count<=0 then return {ok=false,reason="engine_output_empty"}end
return {ok=true,tick=game.tick,machines=finished,engine_units=count,outlet_unit=outlet.unit_number,normal_core_items=raw}
'''
