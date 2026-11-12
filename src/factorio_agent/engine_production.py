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
from .transport import VECTORS,plan_connection,underground_proof,surface_pipe_proof,unbuilt_external_keepouts,xy


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
    # Empty inline predecessor makes the pipe corner a side load onto the right lane.
    for xx in range(-2,22):
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


def _clear_planned_power_group(game,core):
    """Order a copy around an already-planned physically clear pole."""
    poles=[e for e in core['entities']if e['name']=='small-electric-pole']
    if not poles or len(poles)>32:return {'ok':False,'reason':'bounded_existing_engine_pole_group_required'}
    result=game.query('''local poles=helpers.json_to_table('''+_payload(poles)+''');for index,q in ipairs(poles)do
 local e=target(q.position,"small-electric-pole")
 local owned=e and e.force==f and e.surface==s and e.position.x==q.position.x and e.position.y==q.position.y
 if owned or s.can_place_entity{name="small-electric-pole",position=q.position,force=f}then return {ok=true,pole_index=index}end
end;return {ok=false,reason="all_planned_engine_power_targets_obstructed"}''')
    index=result.get('pole_index')
    if not result.get('ok')or type(index)is not int or not 1<=index<=len(poles):
        return {'ok':False,'reason':result.get('reason','native_engine_power_target_unavailable')}
    chosen=poles[index-1];group=deepcopy(core)
    group['entities']=[deepcopy(chosen)]+[e for e in group['entities']if e!=chosen]
    return {'ok':True,'group':group}


def _plan_cells(builder,plan):
    result=builder._occupied_by_plan(plan['entities'])
    for pair in plan.get('underground_pairs',[]):
        dx,dy=VECTORS[pair['direction']]
        result.update((pair['input'][0]+i*dx,pair['input'][1]+i*dy)for i in range(pair['span']+1))
    return result


class EngineProduction:
    def __init__(self,game,builder,catalog,perimeter,*,bus_factory=None,science=None,green=None,tap=None,steel=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.tap=tap if tap is not None else BusTap(game,builder,catalog,bus_factory=bus_factory,science=science,green=green)
        self.steel=steel if steel is not None else SteelProduction(game,builder,catalog)
        self.path=Path(game.cfg.runtime_dir)/'engine-production.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_proof_failure=None
        self.relocation=None

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
        contexts=[]
        for core in cores:
            x,y=xy({'position':core['anchor']});future=engine_core(self.catalog,x,y,4)
            reserved=merge_entities(core['entities'],future['entities'])+_core_keepout(core)
            ranked=sorted(choices,key=lambda p:abs(p['splitter_y']-1-core['ports'][0]['position']['y']))
            contexts.append({'core':core,'future':future,'reserved':reserved,'ranked':ranked[:4],'steel_route':None})
        for rank in range(4):
            for context in contexts:
                core,future,reserved=context['core'],context['future'],context['reserved']
                if rank>=len(context['ranked'])or context.get('steel_failed'):continue
                if budget.expired()or routes>=12:return budget.failure(failures=failures[-6:],route_attempts=routes)
                steel_route=context['steel_route']
                if steel_route is None:
                    steel_route=plan_connection(budget.game,adapter,obs,steel['source'],core['ports'][1],reserved);routes+=1
                    context['steel_route']=steel_route
                    if not steel_route.get('ok'):
                        context['steel_failed']=True;failures.append({'stage':'steel_route','anchor':core['anchor'],'detail':steel_route});continue
                if budget.expired()or routes>=12:return budget.failure(failures=failures[-6:],route_attempts=routes)
                parameters=context['ranked'][rank]
                tap=preview.prepare(obs,parameters)
                if not tap.get('ok'):failures.append({'stage':'tap','parameters':parameters,'detail':tap});continue
                iron=next(p for p in tap['new_bus']['ports']if p['name']==TAP_NAME)
                iron_route=plan_connection(budget.game,adapter,obs,iron,core['ports'][0],reserved+steel_route['entities']+tap['added_entities']);routes+=1
                if not iron_route.get('ok'):failures.append({'stage':'iron_route','anchor':core['anchor'],'parameters':parameters,'detail':iron_route});continue
                es=merge_entities(core['entities'],steel_route['entities'],iron_route['entities'])
                group=_clear_planned_power_group(budget.game,core)
                if not group.get('ok'):failures.append({'stage':'power_target','anchor':core['anchor'],'detail':group});continue
                power=connect_power_groups(budget.game,adapter,obs,[group['group']],merge_entities(es,future['entities']))
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

    def _feed_guard(self):
        mutable={'plan','feed_replan','route_metrics','route_attempts','prepared_tick','construction_authorized','preserved_foreign_assets'}
        return _hash({k:v for k,v in self.state.items()if k not in mutable})

    def _prebuild_core_upgrade(self):
        """Accept only the canonical core or its exact previous one-belt-less form."""
        old=self.state['core'];x,y=xy({'position':old['anchor']})
        core=engine_core(self.catalog,x,y,len(old['engine_machines']))
        tail=entity('transport-belt',x-2,y+7,4)
        legacy=deepcopy(core);legacy['entities'].remove(tail)
        legacy['required_items']['transport-belt']-=1
        if old!=core and old!=legacy:return {'ok':False,'reason':'engine_prebuild_core_is_not_exact_canonical_or_legacy_tail'}
        audit=audit_engine_core(core,self.catalog)
        if not audit['ok']:return {'ok':False,'reason':'engine_prebuild_canonical_core_audit_failed','evidence':audit}
        return {'ok':True,'core':core,'audit':audit,'old_core_revision':_hash(old),'new_core_revision':_hash(core)}

    def _foreign_authorities(self,obs,board):
        """Only exact verified producer plans and saved unit bindings identify a foreign asset."""
        result={};root=self.path.parent
        filenames=('chemical-production.json','advanced-circuits.json','ammo-production.json','steel-production.json',
                   'brick-production.json','basic-petroleum.json','water-gathering.json','oil-gathering.json')
        for filename in filenames:
            path=root/filename
            if not path.exists():continue
            document=json.loads(path.read_text(encoding='utf-8'))
            if not self._identity(obs,document):continue
            entries=list(document.get('products',{}).values())if filename=='chemical-production.json'else[document]
            for entry in entries:
                if entry.get('phase')!='verified'or not entry.get('plan'):continue
                owners=[name for name,plan in board['blocks'].items()if name!='engine_production'and plan==entry['plan']]
                if len(owners)!=1:continue
                for e in entry['plan']['entities']:
                    unit=entry.get('bindings',{}).get(_key(e))
                    if type(unit)is int and unit>0:
                        result[(e['name'],*xy(e),unit)]={'owner':owners[0],'name':e['name'],'position':e['position'],
                            'unit_number':unit,'direction':e.get('direction',0),'world_id':obs['world_id']}
        return result

    def _feed_replan_needed(self,obs):
        if self.state.get('phase')!='build'or self.state.get('construction_authorized'):return False
        shared={_key(dict(self.state[k],name='transport-belt'))for k in('iron_source','steel_source')}
        if set(self.state.get('bindings',{}))-shared:return False
        upgrade=self._prebuild_core_upgrade()
        if not upgrade.get('ok')or upgrade['old_core_revision']!=upgrade['new_core_revision']:return True
        actual={(e['name'],*xy(e))for e in obs.get('entities',[])}
        if any(_key(e)not in shared and(e['name'],*xy(e))in actual for e in self.state['plan']['entities']):return True
        try:
            board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
            if not self._identity(obs,board):return True
            future=unbuilt_external_keepouts(self.builder,obs,board['blocks'],released_block='engine_production')
            return bool(_plan_cells(self.builder,self.state['plan'])&future)
        except(OSError,ValueError,KeyError,TypeError):return True

    def _fixed_tap_authority(self,obs):
        s=self.state
        if self.tap.state.get('phase')=='verified'and self.tap.state.get('parameters')==s['parameters']:
            proof=self.tap.proof(obs)
            return bool(proof and proof.get('port')==s['iron_source'])
        docs=self.tap._documents();bus=docs['factory']['bus']
        if(not all(self._identity(obs,d)for d in docs.values())or docs['factory'].get('phase')!='verified'
                or docs['factory'].get('remove')or docs['factory'].get('cleanup')or docs['board'].get('shared_main_bus')!=bus):return False
        source=s['iron_source'];contract={k:v for k,v in source.items()if k not in('unit_number','priority_splitter','world_id')}
        ports=[p for p in bus['ports']if p.get('name')==source['name']]
        stations=[p for p in bus['stations']if p.get('name')==source['name']]
        if(len(ports)!=1 or any(ports[0].get(k)!=v for k,v in contract.items())or len(stations)!=1
                or stations[0].get('priority')!='surplus'or stations[0].get('splitter',{}).get('position')!=source['priority_splitter']['position']):return False
        return any(e['name']=='splitter'and e['position']==source['priority_splitter']['position']and e['direction']==0
            and e.get('splitter_input_priority')=='left'and e.get('splitter_output_priority')=='left'for e in bus['entities'])

    def _prebuild_feed_context(self,obs,*,publication=False):
        s=self.state;record=s.get('feed_replan',{})
        if not self._identity(obs,s)or s.get('phase')!='build':return {'ok':False,'reason':'engine_feed_replan_requires_same_unbuilt_phase'}
        if record and(type(record.get('attempt'))is not int or record['attempt']!=1):
            return {'ok':False,'reason':'invalid_engine_feed_replan_attempt_identity'}
        if record and record.get('phase')not in(('publishing',)if publication else('planning',)):
            return {'ok':False,'reason':'engine_feed_replan_already_attempted'}
        relocation=getattr(self,'relocation',None)
        if relocation is not None and relocation.state:return {'ok':False,'reason':'engine_feed_replan_must_precede_relocation_costs'}
        try:
            if json.loads(self.path.read_text(encoding='utf-8'))!=s:return {'ok':False,'reason':'engine_feed_replan_checkpoint_changed'}
            board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
            if not self._identity(obs,board):return {'ok':False,'reason':'engine_feed_replan_board_identity_changed'}
            old_revision=_hash(s['plan']);registered=_hash(board.get('blocks',{}).get('engine_production'))
            allowed={old_revision}
            if publication:
                if(record.get('old_plan_revision')!=old_revision or record.get('guard')!=self._feed_guard()
                        or record.get('new_plan_revision')!=_hash(record.get('candidate',{}).get('plan'))):
                    return {'ok':False,'reason':'engine_feed_publication_intent_changed'}
                allowed.add(record['new_plan_revision'])
            if registered not in allowed:return {'ok':False,'reason':'engine_feed_replan_registered_plan_changed'}
            environment=_hash({'blocks':{k:v for k,v in board['blocks'].items()if k!='engine_production'},
                               'shared_main_bus':board.get('shared_main_bus')})
            if publication and environment!=record['candidate']['environment_revision']:
                return {'ok':False,'reason':'engine_feed_replan_foreign_environment_changed'}
            shared={_key(dict(s[k],name='transport-belt')):s[k]['unit_number']for k in('iron_source','steel_source')}
            if s.get('bindings')!=shared:return {'ok':False,'reason':'engine_owned_consumer_construction_already_started'}
            upgrade=self._prebuild_core_upgrade()
            if not upgrade.get('ok'):return upgrade
            if publication:
                candidate=record['candidate']
                if any(candidate.get(k)!=upgrade[k]for k in('core','audit','old_core_revision','new_core_revision')):
                    return {'ok':False,'reason':'engine_feed_publication_core_upgrade_changed'}
            actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])};core_keys={_key(e)for e in upgrade['core']['entities']}
            if any((e['name'],*xy(e))in actual for e in upgrade['core']['entities']):
                return {'ok':False,'reason':'engine_core_construction_already_started'}
            authorities=self._foreign_authorities(obs,board);foreign={}
            for e in s['plan']['entities']:
                live=actual.get((e['name'],*xy(e)))
                if not live or _key(e)in shared:continue
                if _key(e)in core_keys:return {'ok':False,'reason':'engine_core_construction_already_started','entity':e}
                retained=authorities.get((e['name'],*xy(e),live.get('unit_number')))
                if not retained or live.get('direction',0)!=retained['direction']:
                    return {'ok':False,'reason':'unbound_engine_asset_is_not_exact_verified_foreign_asset','entity':e}
                foreign[(retained['name'],*xy(retained))]=retained
            for name in('iron_source','steel_source'):
                q=s[name];live=actual.get(('transport-belt',*xy(q)))
                if not live or live.get('unit_number')!=q['unit_number']or live.get('direction')!=q['facing']:
                    return {'ok':False,'reason':'engine_fixed_feed_source_changed','source':name}
            steel=self._steel_source(obs)
            if(not steel.get('ok')or steel['source']!=s['steel_source']or steel['plan_revision']!=s['steel_plan_revision']
                    or sorted(steel['furnace_units'])!=sorted(s['steel_furnace_units'])):
                return {'ok':False,'reason':'engine_fixed_steel_production_authority_changed'}
            if not self._fixed_tap_authority(obs):return {'ok':False,'reason':'engine_fixed_tap_proof_missing'}
            coverage=self.perimeter.coverage_proof(obs,SCOPE_ID)
            if not coverage or not isinstance(coverage.get('posts'),list):return {'ok':False,'reason':'engine_existing_coverage_proof_required'}
            posts=sorted(({k:p[k]for k in('name','position','unit_number')}for p in coverage['posts']),key=lambda p:p['name'])
            scope=self.perimeter.state.get('coverage_scopes',{}).get(SCOPE_ID,{})
            eligible={p['unit_number']:p for p in self.perimeter.state.get('posts',[])if p.get('coverage_scope')==SCOPE_ID
                      and p['name']in scope.get('new_posts',[])and p.get('commissioned')}
            cells=_plan_cells(self.builder,s['plan']);ignored=[]
            for live in obs.get('entities',[]):
                if live['name']!='gun-turret':continue
                p=live['position'];hit=any(p['x']-1<x+.5 and p['x']+1>x-.5 and p['y']-1<y+.5 and p['y']+1>y-.5 for x,y in cells)
                if not hit:continue
                owned=eligible.get(live.get('unit_number'))
                if not owned or owned['position']!=p:return {'ok':False,'reason':'engine_feed_replan_conflicting_post_is_not_owned_new_scope'}
                ignored.append(live['unit_number'])
            if len(ignored)>4:return {'ok':False,'reason':'engine_feed_replan_old_post_budget_exceeded'}
            if publication and(sorted(ignored)!=record['candidate']['ignored_old_posts']or posts!=record['candidate']['coverage_units']):
                return {'ok':False,'reason':'engine_feed_replan_defense_identity_changed'}
            retained=list(foreign.values())
            if publication and retained!=record['candidate']['preserved_foreign_assets']:
                return {'ok':False,'reason':'engine_feed_replan_foreign_authority_changed'}
            units=[dict(s[k],name='transport-belt',direction=s[k]['facing'])for k in('iron_source','steel_source')]+retained
            native=self.game.query('''local args=helpers.json_to_table('''+_payload({'rows':units,'splitter':s['iron_source']['priority_splitter']})+''');for _,q in ipairs(args.rows)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x
  or e.position.y~=q.position.y or(e.type~="electric-pole"and e.direction~=q.direction)then return {ok=false,reason="engine_feed_retained_native_identity_changed"}end
 if q.item and q.name=="transport-belt"then for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents())do
  local quality=type(item.quality)=="string"and item.quality or(item.quality and item.quality.name or"normal")
  if item.name~=q.item or quality~="normal"then return {ok=false,reason="engine_feed_source_contaminated"}end
 end end end
end;local q=args.splitter;local e=target(q.position,"splitter")
if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=0
 or e.position.x~=q.position.x or e.position.y~=q.position.y or e.splitter_input_priority~="left"or e.splitter_output_priority~="left"then return {ok=false,reason="engine_feed_surplus_splitter_changed"}end
return {ok=true}''')
            if not native.get('ok'):return native
            return {'ok':True,'board':board,'old_plan_revision':old_revision,'guard':self._feed_guard(),'bindings':shared,
                    'ignored_old_posts':sorted(ignored),'coverage_units':posts,'preserved_foreign_assets':retained,
                    'environment_revision':environment,**{k:upgrade[k]for k in('core','audit','old_core_revision','new_core_revision')}}
        except(OSError,ValueError,KeyError,TypeError,AttributeError)as exc:
            return {'ok':False,'reason':'invalid_engine_feed_replan_authority','detail':str(exc)}

    def prepare_prebuild_feed_replan(self,obs):
        """Readonly alternative releasing only this receiver's old unbuilt external feeds."""
        context=self._prebuild_feed_context(obs)
        if not context.get('ok'):return context
        core=context['core'];x,y=xy({'position':core['anchor']});future=engine_core(self.catalog,x,y,4)
        budget=PlanningBudget(self.game);adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda entities:preflight(budget.game,self.builder,obs,entities,ignored_units=context['ignored_old_posts']))
        reserved=merge_entities(core['entities'],future['entities'])+_core_keepout(core)
        arguments={'ignored_units':context['ignored_old_posts'],'released_unbuilt_receiver':{
            'block':'engine_production','plan_revision':context['old_plan_revision']}}
        routes=0
        def failed(stage,detail):return {'ok':False,'reason':'engine_prebuild_feed_replan_failed',
            'evidence':{'stage':stage,'detail':{k:(v[:8]if k=='blocked'and isinstance(v,list)else v)for k,v in detail.items()
                if k in('reason','visited','blocked','position','entity')},'route_attempts':routes}}
        steel=plan_connection(budget.game,adapter,obs,self.state['steel_source'],core['ports'][1],reserved,**arguments)
        routes+=1
        if not steel.get('ok'):return failed('steel_route',steel)
        iron=plan_connection(budget.game,adapter,obs,self.state['iron_source'],core['ports'][0],reserved+steel['entities'],**arguments)
        routes+=1
        if not iron.get('ok'):return failed('iron_route',iron)
        es=merge_entities(core['entities'],steel['entities'],iron['entities'])
        group=_clear_planned_power_group(budget.game,core)
        if not group.get('ok'):return failed('power_target',group)
        power=connect_power_groups(budget.game,adapter,obs,[group['group']],merge_entities(es,future['entities']))
        if not power.get('ok'):return failed('power',power)
        es=merge_entities(es,power['entities'])
        whole=preflight(budget.game,self.builder,obs,es,ignored_units=context['ignored_old_posts'])
        if not whole.get('ok'):return failed('whole_preflight',whole)
        check=preflight(budget.game,self.builder,obs,merge_entities(es,future['entities']),ignored_units=context['ignored_old_posts'])
        if not check.get('ok'):return failed('future_preflight',check)
        plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],underground_pairs=core['underground_pairs']+steel['underground_pairs']+iron['underground_pairs'],
            clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy=self.state['plan']['layout_policy'])
        actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])}
        if any((e['name'],*xy(e))in actual and _key(e)not in context['bindings']for e in es):return failed('bindings',{'reason':'replanned_engine_would_adopt_foreign_or_consumer_asset'})
        pipes=surface_pipe_proof(budget.game,obs,plan)
        if not pipes.get('ok'):return failed('preserved_pipes',pipes)
        if budget.expired():return budget.failure(route_attempts=2)
        return {k:v for k,v in context.items()if k!='board'}|{'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
            'plan':plan,'new_plan_revision':_hash(plan),'prepared_tick':obs.get('tick'),
            'route_attempts':2,'route_metrics':[dict(item='steel-plate',**steel['route_metrics']),dict(item='iron-plate',**iron['route_metrics'])]}

    def _publish_feed_replan(self,obs):
        context=self._prebuild_feed_context(obs,publication=True)
        if not context.get('ok'):return {'status':'blocked',**context}
        record=self.state['feed_replan'];candidate=record['candidate'];plan=candidate['plan']
        x,y=xy({'position':candidate['core']['anchor']});future=engine_core(self.catalog,x,y,4)
        whole=preflight(self.game,self.builder,obs,plan['entities'],ignored_units=candidate['ignored_old_posts'])
        future_check=preflight(self.game,self.builder,obs,merge_entities(plan['entities'],future['entities']),ignored_units=candidate['ignored_old_posts'])
        pipes=surface_pipe_proof(self.game,obs,plan)
        if not whole.get('ok')or not future_check.get('ok')or not pipes.get('ok'):
            return {'status':'blocked','reason':'engine_feed_publication_prospective_proof_changed','evidence':{'whole':whole,'future':future_check,'pipes':pipes}}
        board=context['board']
        if _hash(board['blocks']['engine_production'])==record['old_plan_revision']:
            board['blocks']['engine_production']=deepcopy(plan);_atomic_json(self.path.parent/'pcb-board.json',board)
        updated=deepcopy(self.state);updated.update(plan=deepcopy(plan),core=deepcopy(candidate['core']),audit=deepcopy(candidate['audit']),
            route_metrics=candidate['route_metrics'],route_attempts=2,
            prepared_tick=candidate['prepared_tick'],preserved_foreign_assets=candidate['preserved_foreign_assets'])
        updated.pop('construction_authorized',None);updated['feed_replan']={k:v for k,v in record.items()if k!='candidate'}
        updated['feed_replan'].update(phase='adopted',adopted_tick=obs.get('tick'))
        _atomic_json(self.path,updated);self.state=updated
        return {'status':'waiting','reason':'engine_unbuilt_feeds_replanned_before_defense_relocation','evidence':{
            'old_plan_revision':record['old_plan_revision'],'new_plan_revision':record['new_plan_revision'],
            'old_core_revision':candidate['old_core_revision'],'new_core_revision':candidate['new_core_revision'],
            'preserved_foreign_assets':candidate['preserved_foreign_assets'],'ignored_only_future_old_post_recoveries':candidate['ignored_old_posts']}}

    def _attempt_feed_replan(self,obs):
        context=self._prebuild_feed_context(obs)
        if not context.get('ok'):return {'status':'blocked',**context}
        self.state['feed_replan']={'attempt':1,'phase':'planning','old_plan_revision':context['old_plan_revision'],'guard':context['guard']}
        self._save();candidate=self.prepare_prebuild_feed_replan(obs)
        if json.loads(self.path.read_text(encoding='utf-8'))!=self.state:
            return {'status':'blocked','reason':'engine_feed_checkpoint_changed_during_prepare'}
        board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        if not self._identity(obs,board)or _hash(board['blocks'].get('engine_production'))!=context['old_plan_revision']:
            return {'status':'blocked','reason':'engine_feed_board_changed_during_prepare'}
        if not candidate.get('ok'):
            self.state['feed_replan'].update(phase='blocked',failure=candidate);self._save()
            return {'status':'blocked',**candidate}
        self.state['feed_replan'].update(phase='publishing',candidate=candidate,new_plan_revision=candidate['new_plan_revision'])
        self._save();return self._publish_feed_replan(obs)

    def _preserved_foreign_valid(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs.get('entities',[])}
        return all(actual.get((q['name'],*xy(q)),{}).get('unit_number')==q['unit_number']
            and actual[(q['name'],*xy(q))].get('direction',0)==q['direction']for q in self.state.get('preserved_foreign_assets',[]))

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
            if not ready.get('ok'):
                evidence=ready.get('evidence')or{key:ready[key]for key in('failures','queries','elapsed_seconds','planning_seconds_budget','route_attempts')if key in ready}
                return {'status':'blocked',**ready,'evidence':evidence}
            self.state=ready;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'engine_world_or_catalog_changed'}
        if _hash(self.steel.state.get('plan'))!=self.state['steel_plan_revision']:
            return {'status':'blocked','reason':'engine_bound_steel_plan_changed'}
        feed=self.state.get('feed_replan',{})
        if feed.get('phase')=='publishing':return self._publish_feed_replan(obs)
        if feed.get('phase')=='planning':return {'status':'blocked','reason':'engine_feed_replan_interrupted_before_publication'}
        if feed.get('phase')=='blocked':return {'status':'blocked','reason':'engine_feed_replan_already_attempted','evidence':feed.get('failure')}
        relocation=getattr(self,'relocation',None)
        if self.state['phase']=='tap':
            if self.tap.state.get('parameters')!=self.state['parameters']:
                if self.tap.state and self.tap.state.get('phase')!='verified':return {'status':'blocked','reason':'another_bus_tap_cutover_unfinished'}
                self.tap.state=deepcopy(self.state['tap_installation']);self.tap._save()
            result=self.tap.step(obs,self.state['parameters'])
            if result.get('status')!='succeeded':return result
            self.state['iron_source']=deepcopy(result['evidence']['port']);self.state['phase']='build';self._save()
        if not self._preserved_foreign_valid(obs):return {'status':'blocked','reason':'engine_preserved_foreign_asset_changed'}
        if self._feed_replan_needed(obs):return self._attempt_feed_replan(obs)
        if relocation is not None and relocation.active:
            result=relocation.step(obs)
            return {'status':'waiting','reason':'defense_relocation_complete_reobserve_engine_site'}if result.get('status')=='succeeded'else result
        result=self._bind(obs)
        if result:return result
        result=self._reserve(obs)
        if result:return result
        result=self.perimeter.ensure_coverage(obs,SCOPE_ID,[self.state['core']['reserved_area']])
        if result is not None:return result
        if not self.state.get('construction_authorized'):
            check=preflight(self.game,self.builder,obs,self.state['plan']['entities'])
            if not check.get('ok'):
                if relocation is not None:
                    result=relocation.step(obs)
                    return {'status':'waiting','reason':'defense_relocation_complete_reobserve_engine_site'}if result.get('status')=='succeeded'else result
                return {'status':'blocked','reason':'engine_site_changed_after_defense','evidence':check}
            self.state['construction_authorized']=True;self._save()
        pipes=surface_pipe_proof(self.game,obs,self.state['plan'])
        if not pipes.get('ok'):return {'status':'blocked','reason':'engine_preserved_pipe_network_changed','evidence':pipes}
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'waiting for normally supplied engine, gear and pipe production','evidence':self.last_proof_failure}
        self.state.update(phase='verified',commissioning_proof=proof);self._save()
        return {'status':'succeeded','reason':'normally supplied automatic engine production commissioned','evidence':proof}

    def proof(self,obs):
        self.last_proof_failure=None
        if not self.state or not self._identity(obs,self.state)or self.state.get('phase')=='tap':return None
        if not self._preserved_foreign_valid(obs):self.last_proof_failure={'reason':'engine_preserved_foreign_asset_changed'};return None
        defense=self.perimeter.coverage_proof(obs,SCOPE_ID)
        if not defense:self.last_proof_failure={'reason':'engine_defense_coverage_missing'};return None
        if not underground_proof(obs,self.state['plan']):self.last_proof_failure={'reason':'engine_native_underground_pairs_unverified'};return None
        pipes=surface_pipe_proof(self.game,obs,self.state['plan'])
        if not pipes.get('ok'):self.last_proof_failure=pipes;return None
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
