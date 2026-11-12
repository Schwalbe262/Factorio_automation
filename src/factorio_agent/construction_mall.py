"""Modular, normally supplied construction cells with native circuit stock stops."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import _yield, WorldCatalog
from .board import entity
from .industrial import PlanningBudget
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .power_grid import plan_connection as power_connection
from .radar_network import RADAR_POWER_LUA, planned_power_watts
from .routing import VECTORS
from .smelting import merge_entities, port
from .transport import plan_connection, surface_pipe_proof, underground_proof, unbuilt_external_keepouts, xy
from .bus_tap import BusTap, extend_bus, signature
from .mall_bus_extension import MallBusExtension, propose_mall_taps
from .bus_budget import declared_bus_demand


TARGETS={'transport-belt':200,'inserter':50,'medium-electric-pole':50,'gun-turret':8,'stone-wall':100,
         'underground-belt':50,'splitter':20,'assembling-machine-1':20}
ACTIVE_PRODUCTS=('transport-belt','gun-turret','underground-belt','splitter','assembling-machine-1','inserter')
LOCAL={'transport-belt':('iron-gear-wheel',),'gun-turret':('iron-gear-wheel',),
       'inserter':('iron-gear-wheel','electronic-circuit','copper-cable'),
       'underground-belt':('transport-belt','iron-gear-wheel'),
       'splitter':('transport-belt','iron-gear-wheel','electronic-circuit','copper-cable'),
       'assembling-machine-1':('iron-gear-wheel','electronic-circuit','copper-cable'),
       'medium-electric-pole':('iron-stick','copper-cable'),'stone-wall':()}
RAW={'iron-plate','copper-plate','steel-plate','stone-brick','firearm-magazine'}


def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _product(parameters):
    if parameters in (None,{}):return 'gun-turret'
    if not isinstance(parameters,dict)or set(parameters)!={'product'}or parameters.get('product')not in ACTIVE_PRODUCTS:
        raise ValueError('construction_mall requires one supported product')
    return parameters['product']


def mall_rates(catalog,product):
    if product not in TARGETS:raise ValueError('supported construction product required')
    from .mall_cells import PRODUCTS,recipe_rates
    if product in PRODUCTS:return recipe_rates(catalog,product,TARGETS[product])
    speed=catalog.entities['assembling-machine-1']['crafting_speed'];recipes={};needs={};raw={}
    if type(speed)not in(int,float)or not math.isfinite(speed)or speed<=0:raise ValueError('positive native assembler speed required')
    def visit(item,amount,trail=()):
        if item in RAW:raw[item]=raw.get(item,0)+amount;return
        if item in trail or item not in(product,)+LOCAL[product]:raise ValueError('unsupported local mall dependency')
        recipe=catalog.recipes[item]
        if(len(recipe['products'])!=1 or recipe['products'][0]['name']!=item or recipe['products'][0].get('type','item')!='item'
                or any(p.get('type','item')!='item'for p in recipe['ingredients'])
                or recipe.get('category')not in catalog.entities['assembling-machine-1'].get('crafting_categories',[])
                or type(recipe['energy'])not in(int,float)or not math.isfinite(recipe['energy'])or recipe['energy']<=0):
            raise ValueError('ordinary installed deterministic item recipes required')
        yield_=_yield(recipe['products'][0]);recipes[item]={'recipe':recipe,'yield':yield_}
        needs[item]=needs.get(item,0)+amount
        for ingredient in recipe['ingredients']:visit(ingredient['name'],amount*ingredient['amount']/yield_,trail+(item,))
    visit(product,1)
    rate=min(60*speed/row['recipe']['energy']*row['yield']/needs[name]for name,row in recipes.items())
    return {'nameplate_per_minute':rate,'raw_per_product':raw,'raw_nameplate_per_minute':{k:v*rate for k,v in raw.items()},
            'target_items':TARGETS[product],'raw_per_target_fill':{k:v*TARGETS[product]for k,v in raw.items()},
            'recipes':recipes,'scope':'catalog cell capacity and finite fill cost; upstream surplus limits actual production'}


def mall_core(catalog,product,x,y):
    """Direct intermediate insertion and separate straight raw headers; no cable bus."""
    from .mall_cells import PRODUCTS,recipe_cell
    if product in PRODUCTS:return recipe_cell(catalog,product,x,y,TARGETS[product])
    rated=mall_rates(catalog,product)
    if any(type(v)not in(int,float)or not math.isfinite(v)or v%1!=.5 for v in(x,y)):
        raise ValueError('half-tile mall anchors required')
    es=[];machines=[];arms=[];input_arms=[];edges=[];ports=[];raw_feeds=[]
    def add(name,dx,dy,d=0,**kwargs):
        e=dict(entity(name,x+dx,y+dy,d),**kwargs);es.append(e);return e
    def machine(recipe,dx):
        e=add('assembling-machine-1',dx,0,recipe=recipe);machines.append(e);return e
    def arm(dx,dy,d,long=False):
        e=add('long-handed-inserter'if long else'fast-inserter',dx,dy,d);arms.append(e);input_arms.append(e);return e
    def header(item,points,input_point,facing,target,arm_spec):
        nodes=[add('transport-belt',dx,dy,d)for dx,dy,d in points]
        p=port(item,x+input_point[0],y+input_point[1],facing);ports.append(p)
        a=arm(*arm_spec[:3],long=bool(arm_spec[3])if len(arm_spec)>3 else False)
        raw_feeds.append({'item':item,'arm':a,'machine':target,'belts':nodes})
        return nodes
    final=machine(product,0);locals_={}
    for recipe,dx in({'transport-belt':(('iron-gear-wheel',-4),),'gun-turret':(('iron-gear-wheel',-4),),
        'inserter':(('iron-gear-wheel',-4),('electronic-circuit',4),('copper-cable',8)),
        'medium-electric-pole':(('iron-stick',-4),('copper-cable',4))}.get(product,())):
        locals_[recipe]=machine(recipe,dx)
    def direct(source,target,dx,d):
        a=arm(dx,0,d);edges.append({'source':locals_[source],'destination':final if target==product else locals_[target],
                                  'arm':a,'item':source})
    if product in('transport-belt','gun-turret','inserter'):direct('iron-gear-wheel',product,-2,12)
    if product=='inserter':
        direct('copper-cable','electronic-circuit',6,4);direct('electronic-circuit',product,2,4)
    if product=='medium-electric-pole':direct('iron-stick',product,-2,12);direct('copper-cable',product,2,4)
    if product in('transport-belt','gun-turret','inserter'):
        end=4 if product=='inserter'else 0
        nodes=[add('transport-belt',dx,-3,4)for dx in range(-6,end+1)]
        ports.append(port('iron-plate',x-6,y-3,4))
        for recipe,dx in([(product,0),('iron-gear-wheel',-4)]+([('electronic-circuit',4)]if product=='inserter'else [])):
            target=final if recipe==product else locals_[recipe]
            raw_feeds.append({'item':'iron-plate','arm':arm(dx,-2,0),'machine':target,'belts':nodes})
    if product=='gun-turret':header('copper-plate',[(i,3,4)for i in range(-6,1)],(-6,3),4,final,(0,2,8))
    if product=='inserter':header('copper-plate',[(i,5,4)for i in range(-6,9)],(-6,5),4,locals_['copper-cable'],(8,3,8,True))
    if product=='medium-electric-pole':
        header('iron-plate',[(i,-5,4)for i in range(-6,-3)],(-6,-5),4,locals_['iron-stick'],(-4,-3,0,True))
        header('copper-plate',[(i,-3,12)for i in range(6,3,-1)],(6,-3),12,locals_['copper-cable'],(4,-2,0))
        header('steel-plate',[(i,3,4)for i in range(-6,1)],(-6,3),4,final,(0,2,8))
    if product=='stone-wall':header('stone-brick',[(i,-3,4)for i in range(-6,1)],(-6,-3),4,final,(0,-2,0))
    if product=='piercing-rounds-magazine':
        header('firearm-magazine',[(i,-3,4)for i in range(-6,1)],(-6,-3),4,final,(0,-2,0))
        header('steel-plate',[(i,3,4)for i in range(-6,1)],(-6,3),4,final,(0,2,8))
        header('copper-plate',[(i,0,12)for i in range(5,2,-1)],(5,0),12,final,(2,0,4))
    if product=='inserter':out=(0,2,0);chest=(0,3);access=(0,4)
    elif product=='medium-electric-pole':out=(0,-2,8);chest=(0,-3);access=(0,-4)
    elif product=='piercing-rounds-magazine':out=(-2,0,4);chest=(-3,0);access=(-4,0)
    else:out=(2,0,12);chest=(3,0);access=(4,0)
    output_arm=add('fast-inserter',*out);arms.append(output_arm)
    stock=add('iron-chest',*chest)
    poles=[]
    for dy in(-2,2):
        for dx in range(-6,11 if product=='inserter'else 7,4):poles.append(add('small-electric-pole',dx,dy))
    spine=sorted(poles,key=lambda q:(q['position']['y'],q['position']['x']if q['position']['y']==y-2 else-q['position']['x']))
    wires=[dict(color='red',a=a,b=b)for a,b in zip(spine,spine[1:])]
    for q in machines+arms+[stock]:
        nearest=min(poles,key=lambda p:math.dist(xy(p),xy(q)))
        wires.append(dict(color='red',a=q,b=nearest))
    max_x=8 if product=='inserter'else 4 if product=='medium-electric-pole'else 0
    future=[entity('assembling-machine-1',x+max_x+8,y,recipe=product),entity('iron-chest',x+max_x+11,y)]
    output={'kind':'item','item':product,'direction':'output','name':'iron-chest','position':deepcopy(stock['position'])}
    slots=math.ceil(TARGETS[product]/catalog.items[product]['stack_size'])
    return _plan(es,ports+[output],machines=machines,final_machine=final,arms=arms,input_arms=input_arms,
        output_arm=output_arm,chest=stock,raw_feeds=raw_feeds,direct_edges=edges,product=product,rated=rated,
        target=TARGETS[product],bar_slots=slots,output_access=[entity('transport-belt',x+access[0],y+access[1])],
        expansion_keepouts=future,reserved_area=[[x-6.5,y-6.5],[x+max_x+13.5,y+6.5]],
        poles=poles,wire_edges=wires,output_arms=[output_arm],global_arms=arms,input_buffers=[],feeders=[],
        recipe_inputs={q['recipe']:[i['name']for i in catalog.recipes[q['recipe']]['ingredients']]for q in machines},
        flow='separate raw headers EAST; direct local gears; fixed output chest',growth='EAST reserved second final cell; ports stay fixed',
        layout_policy='circuit-limited-direct-insertion-mall-v2')


def audit_mall_core(core):
    geometry=FactoryBuilder.__new__(FactoryBuilder);boxes=[geometry._occupied_by_plan([q])for q in core['entities']];errors=[]
    occupied=set().union(*boxes)
    if sum(map(len,boxes))!=len(occupied):errors.append('overlapping_factory_footprints')
    if geometry._occupied_by_plan(core['expansion_keepouts']+core['output_access'])&occupied:errors.append('future_or_output_access_blocked')
    for q in core['machines']+core['arms']:
        if not any(max(abs(xy(q)[0]-xy(p)[0]),abs(xy(q)[1]-xy(p)[1]))<=2.5 for p in core['poles']):errors.append('unpowered_consumer')
    if any(math.dist(xy(edge['a']),xy(edge['b']))>7.5 for edge in core['wire_edges']):errors.append('wire_edge_exceeds_conservative_reach')
    for q in core['arms']:
        dx,dy=VECTORS[q['direction']];length=2 if q['name']=='long-handed-inserter'else 1
        # Machine footprints, rather than their centers, receive ordinary arms.
        if(xy(q)[0]+dx*length,xy(q)[1]+dy*length)not in occupied or(xy(q)[0]-dx*length,xy(q)[1]-dy*length)not in occupied:
            errors.append('inserter_endpoint_missing')
    if core.get('component_layout'):
        from .mall_cells import audit_recipe_cell
        errors+=audit_recipe_cell(core)
    return {'ok':not errors,'errors':errors}


def _control_entities(core,entities,routes):
    """Build every paid core asset before controls, leaving external feeds open."""
    core_keys={signature(q)for q in core['entities']}
    return [q for q in entities if q not in routes or signature(q)in core_keys]


class ConstructionMall:
    """One product per job; resolver is a trusted producer/priority adapter, not AI parameters.

    resolver(obs, item, consumer_id) returns a fresh receipt with source,
    producer_id, revision, consumer_id, exclusive_edge, priority_verified,
    available_per_minute, burst_only and tick. Physical sources must be separately
    approved surplus belt handoffs; this class never steals an existing edge.
    """
    def __init__(self,game,builder,catalog,perimeter,*,bus_factory=None,science=None,green=None,smelting=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        components=dict(bus_factory=bus_factory,science=science,green=green)
        self.tap=BusTap(game,builder,catalog,**components)
        self.extension=MallBusExtension(game,builder,catalog,**components)
        self.path=Path(game.cfg.runtime_dir)/'construction-mall.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_proof_failure=None

    @property
    def commissioned_items(self):return tuple(k for k,v in self.state.get('products',{}).items()if v.get('phase')=='verified')

    @property
    def pending_items(self):return tuple(k for k,v in self.state.get('products',{}).items()if v.get('phase')!='verified')

    def has_stock_cell(self,item):return item in self.commissioned_items

    def _identity(self,obs,value):
        return(obs.get('ok',True)and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
            and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def _save(self):_atomic_json(self.path,self.state)

    def _board(self,obs):
        board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        if not self._identity(obs,board):raise ValueError('mall_board_identity_changed')
        return board

    def _sources(self,obs,product,board):
        rated=mall_rates(self.catalog,product);sources={};budgets={};bus=board['shared_main_bus']
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        for item,peak in rated['raw_nameplate_per_minute'].items():
            name='mall-'+product+'-'+item
            p=next((q for q in bus['ports']if q['name']==name),None)
            station=next((q for q in bus['stations']if q['name']==name),None)
            if not p or not station or p.get('priority')!='surplus':return {'ok':False,'reason':'mall_paid_surplus_tap_not_yet_installed','item':item}
            live=actual.get(('transport-belt',*xy(p)));split=actual.get(('splitter',*xy(station['splitter'])))
            if not live or not split:return {'ok':False,'reason':'mall_bound_bus_handoff_missing','item':item}
            source=dict(p,tap_name=name,name='transport-belt',world_id=obs['world_id'],unit_number=live['unit_number'],
                priority_splitter=dict(name='splitter',position=split['position'],unit_number=split['unit_number'],direction=0,input_priority='left',output_priority='left'))
            sources[item]=source
            profile=bus['capacity']['materials'][item];supply=profile['supply_ceiling_per_minute']
            declared=declared_bus_demand(board,item)
            budgets[item]=dict(source_nominal_per_minute=supply,all_declared_demand_per_minute=declared,
                design_deficit_per_minute=max(0,declared-supply),cell_peak_per_minute=peak,burst_only=True,
                guaranteed_surplus_per_minute=0,scope='native downstream surplus priority; capped finite fill, no sustained-rate guarantee')
        native=self.game.query(MALL_SOURCE_LUA.replace('PAYLOAD',_payload({'world_id':obs['world_id'],'sources':sources})))
        return dict(native,sources=sources,budgets=budgets)

    def _bus_preview(self,obs,product,board):
        bus=board['shared_main_bus'];old=deepcopy(bus);tasks=[];north=None
        docs=self.tap._documents()
        if(docs['factory'].get('phase')!='verified'or docs['factory'].get('bus')!=old
                or any(q.get('world_id')!=obs['world_id']or q.get('catalog_fingerprint')!=self.catalog.fingerprint for q in docs.values())
                or not underground_proof(obs,old)):
            return {'ok':False,'reason':'mall_requires_matching_commissioned_native_shared_bus'}
        span=self.game.query('return {ok=true,max_span=prototypes.entity["underground-belt"].max_underground_distance}')
        if not span.get('ok'):return span
        result=propose_mall_taps(old,mall_rates(self.catalog,product)['raw_nameplate_per_minute'],product,obs,self.builder,board,maximum_span=span['max_span'])
        if not result.get('ok'):return result
        bus,tasks,north=result['bus'],result['tasks'],result['north_y']
        original={signature(q)for q in old['entities']};final={signature(q)for q in bus['entities']}
        removed=[q for q in old['entities']if signature(q)not in final]
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        try:ignored=[actual[(q['name'],*xy(q))]['unit_number']for q in removed]
        except KeyError:return {'ok':False,'reason':'mall_preview_old_corner_missing'}
        retained=[]
        for spec in old['entities']:
            live=actual.get((spec['name'],*xy(spec)))
            if not live or type(live.get('unit_number'))is not int or any(live.get(k)!=spec.get(k)for k in('direction','belt_to_ground_type','splitter_input_priority','splitter_output_priority')):
                return {'ok':False,'reason':'mall_original_native_bus_changed','position':spec['position']}
            if live['unit_number']not in ignored:retained.append(dict(spec,unit_number=live['unit_number']))
        return dict(ok=True,bus=bus,tasks=tasks,north_y=north,old_bus=old,ignored_units=ignored,retained_units=retained,
                    additions=[q for q in bus['entities']if signature(q)not in original],
                    sources={item:dict(next(q for q in bus['ports']if q['name']=='mall-'+product+'-'+item))
                             for item in mall_rates(self.catalog,product)['raw_per_product']})

    def _geometry(self,obs,board,core,entities):
        blocks={k:v for k,v in board['blocks'].items()if k!='construction_mall_'+core['product']}
        areas=[b['reserved_area']for b in blocks.values()if b.get('reserved_area')]
        areas += [r['area']for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
        if any(overlap(core['reserved_area'],a)for a in areas):return {'ok':False,'reason':'mall_reserved_area_conflict'}
        hard=unbuilt_external_keepouts(self.builder,obs,blocks)
        if self.builder._occupied_by_plan(entities+core['expansion_keepouts']+core['output_access'])&hard:
            return {'ok':False,'reason':'mall_unbuilt_external_transport_conflict'}
        return {'ok':True}

    def _power(self,game,obs,board,entry):
        other=deepcopy(board);other['blocks']={k:v for k,v in board['blocks'].items()if not k.startswith('construction_mall_')}
        reserve=sum(self.catalog.entities[e['name']].get('energy_usage',0)*60 for e in entry['core']['machines']+entry['core']['arms'])
        for product,old in self.state.get('products',{}).items():
            if product!=entry['core']['product']:
                reserve += sum(self.catalog.entities[e['name']].get('energy_usage',0)*60 for e in old['core']['machines']+old['core']['arms'])
        scheduled=planned_power_watts(self.catalog,obs,other)
        for name in('radar_base','radar_oil_future'):
            for spec in other['blocks'].get(name,{}).get('entities',[]):
                if not any(e['name']==spec['name']and e['position']==spec['position']for e in obs['entities']):
                    scheduled+=self.catalog.entities[spec['name']].get('energy_usage',0)*60
        return game.query(MALL_POWER_LUA.replace('PAYLOAD',_payload({'world_id':obs['world_id'],'source':entry['power_source'],
            'preserved_poles':entry['preserved_poles'],'scheduled_watts':scheduled,'additional_watts':reserve})))

    def prepare(self,obs,parameters=None):
        product=_product(parameters)
        if not self._identity(obs,{'world_id':obs.get('world_id'),'catalog_fingerprint':self.catalog.fingerprint}):
            return {'ok':False,'reason':'mall_valid_world_observation_required'}
        if self.state and not self._identity(obs,self.state):return {'ok':False,'reason':'mall_world_or_catalog_changed'}
        old=self.state.get('products',{}).get(product)
        if old:return deepcopy(old)
        if self.pending_items:return {'ok':False,'reason':'another_mall_cell_must_commission_first','product':self.pending_items[0]}
        if not obs.get('technologies',{}).get('circuit-network'):
            return {'ok':False,'reason':'mall_native_circuit_network_research_required'}
        if(product=='medium-electric-pole'and not obs.get('technologies',{}).get('electric-energy-distribution-1')):
            return {'ok':False,'reason':'mall_native_medium_pole_research_required'}
        if(product=='piercing-rounds-magazine'and not obs.get('technologies',{}).get('military-2')):
            return {'ok':False,'reason':'mall_native_military_2_research_required'}
        required=set((product,)+LOCAL[product]+('assembling-machine-1','fast-inserter','iron-chest','small-electric-pole'))
        if product in('inserter','medium-electric-pole'):required.add('long-handed-inserter')
        if not all(obs.get('enabled_recipes',{}).get(k)for k in required):
            return {'ok':False,'reason':'mall_native_product_component_and_build_recipes_required'}
        try:
            board=self._board(obs);preview=self._bus_preview(obs,product,board)
            if not preview.get('ok'):return preview
            planning_board=deepcopy(board);planning_board['shared_main_bus']=preview['bus']
            sources={'sources':preview['sources']}
            budget=PlanningBudget(self.game);failures=[]
            producers=[xy(q)for q in obs['entities']if q['name']in('assembling-machine-1','stone-furnace','steel-furnace')]
            if not producers:return {'ok':False,'reason':'mall_existing_productive_base_required'}
            networks={q.get('electric_network_id')for q in obs['entities']if q['name']=='steam-engine'and q.get('energy',0)>0}
            supply=[xy(q)for q in obs['entities']if q['name']=='small-electric-pole'and q.get('electric_network_id')in networks]
            if not supply:return {'ok':False,'reason':'mall_supplied_base_network_required'}
            bounds=[[math.floor((min(p[i]for p in producers)-64)/16)*16 for i in(0,1)],
                    [math.ceil((max(p[i]for p in producers)+64)/16)*16 for i in(0,1)]]
            candidates=[]
            for x in range(bounds[0][0],bounds[1][0]+1,16):
                for y in range(bounds[0][1],bounds[1][1]+1,16):
                    core=mall_core(self.catalog,product,x+.5,y+.5)
                    if not self._geometry(obs,planning_board,core,core['entities'])['ok']:continue
                    if self.builder._occupied_by_plan(core['entities']+core['expansion_keepouts'])&self.builder._occupied_by_plan(preview['additions']):continue
                    route_distance=sum(math.dist(xy(p),xy(sources['sources'][p['item']]))for p in core['ports']if p['direction']=='input')
                    candidates.append((route_distance+min(math.dist(xy(core['poles'][0]),p)for p in supply),x,y,core))
            candidates.sort(key=lambda q:q[:3]);candidates=candidates[:64]
            land=survey_slots(budget.game,[[q[3]['reserved_area']]for q in candidates])if candidates else{'ok':True,'legal':[]}
            if not land.get('ok'):return land
            legal=[candidates[i-1][3]for i in land.get('legal',[])if type(i)is int and 1<=i<=len(candidates)]
            for core in legal[:8]:
                if budget.expired():return budget.failure(candidate_failures=failures[-6:])
                audit=audit_mall_core(core)
                if not audit['ok']:return {'ok':False,'reason':'mall_core_audit_failed','evidence':audit}
                geometry=self._geometry(obs,planning_board,core,core['entities'])
                if not geometry['ok']:failures.append(geometry['reason']);continue
                es=core['entities'][:];routes=[];pairs=[]
                adapter=type('MallAdapter',(),{'_occupied_by_plan':self.builder._occupied_by_plan,
                    'can_place':lambda _,specs:preflight(budget.game,self.builder,obs,specs)})()
                for destination in [p for p in core['ports']if p['direction']=='input']:
                    source=sources['sources'][destination['item']]
                    route=plan_connection(budget.game,adapter,obs,source,destination,es+core['expansion_keepouts']+preview['bus']['entities'])
                    if not route.get('ok'):
                        failures.append(dict(stage='typed_input_route',anchor=core['final_machine']['position'],item=destination['item'],
                            source=source['position'],destination=destination['position'],
                            **{k:route[k]for k in('reason','visited','routing_attempts','layer_candidates_rejected')if k in route}));break
                    added=[e for e in route['entities']if xy(e)!=xy(source)]
                    es=merge_entities(es,added);routes+=added;pairs+=route['underground_pairs']
                else:
                    bridge=power_connection(budget.game,self.builder,obs,core['entities'][next(i for i,e in enumerate(core['entities'])if e['name']=='small-electric-pole')]['position'],
                                            [e for e in es if e['name']!='small-electric-pole']+core['expansion_keepouts'])
                    if not bridge.get('ok'):failures.append(bridge.get('reason','mall_power_route_failed'));continue
                    actual={(e['name'],*xy(e)):e for e in obs['entities']};preserved=[];new_poles=[]
                    source_live=next((e for e in obs['entities']if e.get('unit_number')==bridge.get('source_unit_number')and e['position']==bridge.get('source_position')),None)
                    if not source_live:failures.append('mall_exact_power_source_missing');continue
                    for spec in [source_live]+bridge['entities']:
                        old=actual.get((spec['name'],*xy(spec)))
                        if old:
                            preserved.append(dict({k:old[k]for k in('name','position','unit_number','direction','electric_network_id')},world_id=obs['world_id']))
                        else:new_poles.append(spec)
                    es=merge_entities(es,new_poles)
                    geometry=self._geometry(obs,planning_board,core,es)
                    if not geometry['ok']:failures.append(geometry['reason']);continue
                    check=preflight(budget.game,self.builder,obs,es+core['expansion_keepouts']+core['output_access']+preview['additions'],ignored_units=preview['ignored_units'])
                    if not check.get('ok'):failures.append(check.get('reason','mall_whole_preflight_failed'));continue
                    plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],underground_pairs=pairs,
                        clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'],
                        defense_scope_id='construction-mall:'+product,layout_policy=core['layout_policy'])
                    control_es=_control_entities(core,es,routes)
                    controls=_plan(control_es,[],clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'])
                    entry={'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'phase':'bus',
                        'core':core,'plan':plan,'control_plan':controls,'internal_plan':_plan([e for e in es if e not in routes],[]),
                        'sources':{},'source_budgets':{},'bus_preview':preview,'bus_task_index':0,
                        'power_source':preserved[0],'preserved_poles':preserved,'rated':core['rated'],'bindings':{},
                        'defense_scope_id':plan['defense_scope_id'],'prepared_tick':obs.get('tick')}
                    power=self._power(budget.game,obs,board,entry)
                    if not power.get('ok'):failures.append(power.get('reason','mall_native_power_headroom_missing'));continue
                    defense=self.perimeter.prepare_coverage(obs,entry['defense_scope_id'],[core['reserved_area']])
                    if not defense.get('ok'):failures.append(defense.get('reason','mall_defense_preview_failed'));continue
                    if budget.expired():return budget.failure(stage='mall_whole_preparation')
                    entry['power_preview']=power;entry['defense_preview']=defense;return entry
            return {'ok':False,'reason':'no_bounded_defended_mall_site','evidence':{'candidate_count':len(candidates),'land_legal_count':len(legal),'failures':failures[-6:]}}
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'ok':False,'reason':'mall_context_or_geometry_invalid','detail':str(exc)}

    def _bind(self,obs,entry):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in entry['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));old=entry['bindings'].get(_key(spec))
            if old and(not live or live.get('unit_number')!=old):return {'status':'blocked','reason':'mall_owned_asset_missing_or_replaced'}
            if live:
                if type(live.get('unit_number'))is not int or live.get('direction',0)!=spec.get('direction',0):
                    return {'status':'blocked','reason':'mall_owned_asset_configuration_changed'}
                if old is None:entry['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def _controls(self,obs,entry,*,proof=False):
        if any(_key(e)not in entry['bindings']for e in entry['core']['entities']):return {'ok':False,'reason':'mall_control_assets_not_built'}
        return self._native(obs,entry,MALL_CONTROL_LUA)

    def _native(self,obs,entry,body):
        return self.game.query('local x=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],core=entry['core'],
            bindings=entry['bindings'],configure=False))+');'+body)

    def _bus_step(self,obs,entry):
        preview=entry['bus_preview'];north=preview['north_y']
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        for q in preview['retained_units']:
            live=actual.get((q['name'],*xy(q)))
            if not live or live.get('unit_number')!=q['unit_number']or any(live.get(k)!=q.get(k)for k in('direction','belt_to_ground_type','splitter_input_priority','splitter_output_priority')):
                return {'status':'blocked','reason':'mall_original_retained_bus_unit_changed','position':q['position']}
        board=self._board(obs)
        if north is not None and board['shared_main_bus']['trunks'][0]['north_y']>north:
            result=self.extension.step(obs,north)
            return {'status':'waiting','reason':'mall_frontier_extended_fresh_observation_required'}if result.get('status')=='succeeded'else result
        while entry['bus_task_index']<len(preview['tasks']):
            task=preview['tasks'][entry['bus_task_index']];parameters=task['parameters'];bus=board['shared_main_bus']
            if(self.tap.state and self.tap.state.get('parameters')=={**parameters,'line':0}and self.tap.state.get('phase')!='verified'):
                result=self.tap.step(obs,parameters)
                return {'status':'waiting','reason':'mall_tap_verified_fresh_observation_required'}if result.get('status')=='succeeded'else result
            if not any(p['name']==parameters['name']for p in bus['ports']):
                if not self.tap.state or self.tap.state.get('parameters')!={**parameters,'line':0}:
                    if self.tap.state and self.tap.state.get('phase')!='verified':return {'status':'blocked','reason':'mall_other_bus_tap_unfinished'}
                    fresh=self.tap.prepare(obs,parameters)
                    if not fresh.get('ok'):return dict(fresh,status='blocked')
                    if({signature(q)for q in fresh['added_entities']}!={signature(q)for q in task['added_entities']}
                            or next(p for p in fresh['new_bus']['ports']if p['name']==parameters['name'])!=task['tap_port']):
                        return {'status':'blocked','reason':'mall_fresh_tap_geometry_differs_from_whole_preview'}
                    self.tap.state=fresh;self.tap._save()
                result=self.tap.step(obs,parameters)
                if result.get('status')!='succeeded':return result
                return {'status':'waiting','reason':'mall_tap_commissioned_fresh_observation_required'}
            station=next(q for q in bus['stations']if q['name']==parameters['name'])
            if station['priority']!='surplus' or station['splitter']['position']['y']!=parameters['splitter_y']:
                return {'status':'blocked','reason':'mall_installed_tap_contract_changed'}
            entry['bus_task_index']+=1;self._save()
        fresh=self._sources(obs,entry['core']['product'],board)
        if not fresh.get('ok'):return dict(fresh,status='blocked')
        entry['sources']=fresh['sources'];entry['source_budgets']=fresh['budgets'];entry['phase']='controls';self._save()
        return None

    def _future_corner_units(self,obs,entry):
        """Admit only paid extension corners awaiting ordinary BusTap recovery."""
        preview=entry['bus_preview'];ignored=list(preview['ignored_units']);s=self.extension.state
        if not s or preview['north_y']is None:return {'ok':True,'ignored_units':ignored}
        if s.get('world_id')!=obs['world_id']or s.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'ok':False,'reason':'mall_future_corner_extension_authority_changed'}
        if s.get('parameters')!={'north_y':preview['north_y']}:
            if s.get('phase')=='verified':return {'ok':True,'ignored_units':ignored}
            return {'ok':False,'reason':'mall_future_corner_extension_authority_changed'}
        if s.get('phase')in('stock','build'):
            bound=self.extension.bind_added(obs)
            if bound:return dict(bound,ok=False)
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        added={xy(q):q for q in s['added_entities']}
        columns={q['item']:q['x']for q in preview['bus']['trunks']if q['line']==0}
        for task in preview['tasks']:
            p=task['parameters'];corner=(columns[p['item']],p['splitter_y'])
            live=actual.get(('transport-belt',*corner))
            if corner not in added or not live:continue
            unit=s.get('added_bindings',{}).get(str(corner))
            if(type(unit)is not int or live.get('unit_number')!=unit or live.get('direction')!=0):
                return {'ok':False,'reason':'mall_future_corner_paid_unit_changed','position':live['position']}
            ignored.append(unit)
        return {'ok':True,'ignored_units':list(dict.fromkeys(ignored))}

    def step(self,obs,parameters=None):
        product=_product(parameters)
        if self.state and not self._identity(obs,self.state):return {'status':'blocked','reason':'mall_world_or_catalog_changed'}
        entry=self.state.get('products',{}).get(product)
        if not entry:
            entry=self.prepare(obs,parameters)
            if not entry.get('ok'):return dict(entry,status='blocked')
            if not self.state:self.state={'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'products':{}}
            self.state['products'][product]=entry;self._save()
        try:
            if entry['phase']=='bus':
                board=self._board(obs);name='construction_mall_'+product
                if board['blocks'].get(name)not in(None,entry['plan']):return {'status':'blocked','reason':'mall_registered_plan_changed'}
                if name not in board['blocks']:
                    board['blocks'][name]=entry['plan'];_atomic_json(self.path.parent/'pcb-board.json',board)
                corners=self._future_corner_units(obs,entry)
                if not corners.get('ok'):return dict(corners,status='blocked')
                check=preflight(self.game,self.builder,obs,entry['plan']['entities']+entry['core']['expansion_keepouts']+entry['core']['output_access']
                    +entry['bus_preview']['additions'],ignored_units=corners['ignored_units'])
                if not check.get('ok'):return dict(check,status='blocked')
                result=self._bus_step(obs,entry)
                if result:return result
            board=self._board(obs);fresh=self._sources(obs,product,board)
            if not fresh.get('ok')or fresh['sources']!=entry['sources']:
                return {'status':'blocked','reason':'mall_bound_surplus_source_changed','evidence':fresh}
            result=self._bind(obs,entry)
            if result:return result
            name='construction_mall_'+product;old=board['blocks'].get(name)
            if old and old!=entry['plan']:return {'status':'blocked','reason':'mall_registered_plan_changed'}
            geometry=self._geometry(obs,board,entry['core'],entry['plan']['entities'])
            if not geometry.get('ok'):return dict(geometry,status='blocked')
            if not old:board['blocks'][name]=entry['plan'];_atomic_json(self.path.parent/'pcb-board.json',board)
            defense=self.perimeter.ensure_coverage(obs,entry['defense_scope_id'],[entry['core']['reserved_area']])
            if defense is not None:return defense
            power=self._power(self.game,obs,board,entry)
            if not power.get('ok'):return {'status':'blocked','reason':'mall_power_margin_changed','evidence':power}
            pipes=surface_pipe_proof(self.game,obs,entry['plan'])
            if not pipes.get('ok'):return {'status':'blocked','reason':'mall_preserved_pipe_changed','evidence':pipes}
            check=preflight(self.game,self.builder,obs,entry['plan']['entities']+entry['core']['expansion_keepouts']+entry['core']['output_access'])
            if not check.get('ok'):return {'status':'blocked','reason':'mall_prebuild_geometry_changed','evidence':check}
            if entry['phase']=='controls':
                result=self.builder.ensure_plan(obs,entry['control_plan'])
                if result.get('status')!='succeeded':return result
            controls=self._controls(obs,entry)
            if not controls.get('ok'):
                return {'type':'mall_stock_limit','product':product,'expected_world_id':obs['world_id'],
                        'expected_catalog_fingerprint':self.catalog.fingerprint,'expected_actor_unit':obs.get('actor_unit_number'),
                        'reason':'configure exact circuit stock stop before connecting raw feeds'}
            if entry['phase']=='controls':
                native=self._native(obs,entry,MALL_PROOF_LUA)
                if not native.get('ok'):return {'status':'blocked','reason':'mall_empty_paid_baseline_invalid','evidence':native}
                if any(q['products_finished']!=0 or q['output']for q in native['machines'])or native['output_stock']:
                    return {'status':'blocked','reason':'mall_empty_machine_baseline_required_before_belt_connection'}
                entry['baseline']={str(q['unit_number']):q['products_finished']for q in native['machines']}
                entry['phase']='build';self._save()
            result=self.builder.ensure_plan(obs,entry['plan'])
            if result.get('status')!='succeeded':return result
            proof=self.proof(obs,parameters)
            if not proof:return {'status':'waiting','reason':'waiting for normally supplied limited mall output','evidence':self.last_proof_failure}
            entry['phase']='verified';entry['production_witness']=proof['production_witness'];entry['proof']=proof;self._save()
            return {'status':'succeeded','reason':'normally supplied circuit-limited construction stock produced','evidence':proof}
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'status':'blocked','reason':'mall_runtime_context_changed','detail':str(exc)}

    def proof(self,obs,parameters=None):
        product=_product(parameters);self.last_proof_failure=None
        if not self.state or not self._identity(obs,self.state):return None
        entry=self.state.get('products',{}).get(product)
        if not entry:return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        if any(not entry['bindings'].get(_key(e))or actual.get((e['name'],*xy(e)),{}).get('unit_number')!=entry['bindings'][_key(e)]
               for e in entry['plan']['entities']):return None
        try:
            board=self._board(obs)
            if board['blocks'].get('construction_mall_'+product)!=entry['plan']:return None
            sources=self._sources(obs,product,board)
            if not sources.get('ok')or sources['sources']!=entry['sources']:return None
            defense=self.perimeter.coverage_proof(obs,entry['defense_scope_id'])
            if not defense or not underground_proof(obs,entry['plan'])or not surface_pipe_proof(self.game,obs,entry['plan']).get('ok'):return None
            power=self._power(self.game,obs,board,entry)
            if not power.get('ok'):self.last_proof_failure=power;return None
            controls=self._controls(obs,entry,proof=True)
            if not controls.get('ok'):self.last_proof_failure=controls;return None
            native=self._native(obs,entry,MALL_PROOF_LUA)
            if not native.get('ok'):self.last_proof_failure=native;return None
            if not entry.get('baseline')or any(q['products_finished']<=entry['baseline'].get(str(q['unit_number']),math.inf)for q in native['machines']):
                self.last_proof_failure={'reason':'mall_all_actual_product_counters_must_advance','machines':native['machines']};return None
            witness=entry.get('production_witness')
            if not witness:
                if native['output_stock']<=0:return None
                witness={'tick':native['tick'],'machine_units':[q['unit_number']for q in native['machines']],'actual_chest_stock':native['output_stock']}
        except(OSError,ValueError,KeyError,TypeError):return None
        return {'tick':native['tick'],'product':product,'source':dict(entry['core']['ports'][-1],world_id=obs['world_id'],
            unit_number=entry['bindings'][_key(entry['core']['chest'])],defense_scope_id=entry['defense_scope_id']),
            'native':native,'limits':controls,'rated':entry['rated'],'source_budgets':sources['budgets'],'power':power,'production_witness':witness,
            'defense_coverage':defense,'scope':'actual circuit-limited automatic stock; finite buffers and burst surplus, no sustained-rate promise'}

    def upkeep(self,obs):
        for product in self.commissioned_items:
            if not self.proof(obs,{'product':product}):return {'status':'blocked','reason':'mall_commissioned_cell_needs_recovery','product':product,'evidence':self.last_proof_failure}
        return None

    def ensure_stock(self,obs,item,count):
        if type(count)is not int or not 1<=count<=1000:raise ValueError('mall target must be1..1000 actual actor items')
        if not self.has_stock_cell(item):return None
        needed=count-obs.get('inventory',{}).get(item,0)
        if needed<=0:return None
        proof=self.proof(obs,{'product':item})
        if not proof:return {'status':'blocked','reason':'mall_fresh_production_proof_missing','item':item,'evidence':self.last_proof_failure}
        if not proof['native']['output_stock']:return {'status':'waiting','reason':'mall_capped_surplus_output_empty','item':item,'source_budgets':proof['source_budgets']}
        from .mall_stock_transfer import mall_stock_action
        return mall_stock_action(self.game,obs,item,min(50,needed,proof['native']['output_stock']),proof['native']['output_slots'][:16])


def execute_mall_stock_limit(game,action):
    """Only configure exact saved mall assets, using normally unlocked free circuit wires."""
    allowed={'type','product','expected_world_id','expected_actor_unit','expected_catalog_fingerprint','reason'}
    if(not isinstance(action,dict)or set(action)-allowed or action.get('type')!='mall_stock_limit'or action.get('product')not in ACTIVE_PRODUCTS
            or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or getattr(game,'backend',None)not in('assisted','character')):
        raise ValueError('exact mall_stock_limit action required')
    root=Path(game.cfg.runtime_dir);state=json.loads((root/'construction-mall.json').read_text(encoding='utf-8'))
    product=action['product'];entry=state['products'][product];core=entry['core']
    board=json.loads((root/'pcb-board.json').read_text(encoding='utf-8'))
    catalog=WorldCatalog.from_dict(json.loads((root/'catalog.json').read_text(encoding='utf-8')))
    if(action.get('expected_world_id')!=getattr(game,'expected_world',None)or any(q.get('world_id')!=action.get('expected_world_id')
            or q.get('catalog_fingerprint')!=catalog.fingerprint for q in(state,entry,board))
            or catalog.fingerprint!=action.get('expected_catalog_fingerprint')or board['blocks'].get('construction_mall_'+product)!=entry['plan']
            or core!=mall_core(catalog,product,*xy(core['final_machine']))):
        raise ValueError('mall configuration known producer/PCB/catalog authority changed')
    if any(type(entry['bindings'].get(_key(e)))is not int for e in core['entities']):raise ValueError('all paid mall core units must be bound')
    args=dict(world_id=state['world_id'],actor_unit=action['expected_actor_unit'],core=core,bindings=entry['bindings'],configure=True)
    try:result=game.query('local x=helpers.json_to_table('+_payload(args)+');'+MALL_CONTROL_LUA)
    except Exception:return {'ok':False,'status':'blocked','reason':'mall_configuration_outcome_unknown'}
    if not isinstance(result,dict)or type(result.get('ok'))is not bool:return {'ok':False,'status':'blocked','reason':'mall_configuration_outcome_unknown'}
    return result


def execute_take_mall_stock(game,action):
    from .mall_stock_transfer import execute_take_mall_stock as execute
    return execute(game,action)


MALL_POWER_LUA=RADAR_POWER_LUA.replace('radar_','mall_')

# Reuse the actually commissioned wire/reach/foreign-connection/hand-stop
# implementation, with an immutable product/target from the known mall core.
from .piercing_ammo import PIERCING_CONTROL_LUA, NATIVE_ENTITIES
MALL_CONTROL_LUA=(PIERCING_CONTROL_LUA.replace('"piercing-rounds-magazine"','x.core.product')
    .replace('200','x.core.target').replace('local_yellow_limit=8,','').replace('piercing_','mall_'))
MALL_CONTROL_LUA=MALL_CONTROL_LUA.replace('if e.inserter_stack_size_override~=1 or not condition',
    'local arm_b=behavior(e);if out[key(q)]and(not arm_b.circuit_read_hand_contents or arm_b.circuit_hand_read_mode~=defines.control_behavior.inserter.hand_read_mode.hold)then return reject("mall_output_hand_signal_changed")end; if e.inserter_stack_size_override~=1 or not condition')

MALL_PROOF_LUA=NATIVE_ENTITIES.replace('piercing_','mall_')+r'''
local function inventory(inv,allowed)
 local values={};if not inv then return nil end
 for i=1,#inv do local st=inv[i];if st.valid_for_read then
  if not allowed[st.name]or st.prototype.type~="item"or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
   or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return nil end
  values[st.name]=(values[st.name]or 0)+st.count
 end end;return values
end
local rows={};local wip=0
for _,q in ipairs(x.core.machines)do local e=live[key(q)]
 if not e.is_connected_to_electric_network()or e.energy<=0 then return reject("mall_machine_unpowered")end
 local allowed={};for _,item in ipairs(x.core.recipe_inputs[q.recipe])do allowed[item]=true end
 if not inventory(e.get_inventory(defines.inventory.crafter_input),allowed)then return reject("mall_machine_input_contaminated")end
 local output=inventory(e.get_output_inventory(),{[q.recipe]=true})
 if not output then return reject("mall_machine_output_contaminated")end
 if q.recipe==x.core.product then wip=wip+(output[q.recipe]or 0)end
 rows[#rows+1]={unit_number=e.unit_number,recipe=q.recipe,products_finished=e.products_finished,output=output}
end
for _,q in ipairs(x.core.arms)do local e=live[key(q)]
 if not e.is_connected_to_electric_network()or e.energy<=0 then return reject("mall_inserter_unpowered")end
 local st=e.held_stack;if st.valid_for_read and st.name==x.core.product then wip=wip+st.count end
end
for _,q in ipairs(x.core.arm_connections or{})do local e=live[key(q.arm)]
 local p,r=e.pickup_position,e.drop_position
 if not p or not r or math.floor(p.x)~=math.floor(q.pickup_position.x)or math.floor(p.y)~=math.floor(q.pickup_position.y)
  or math.floor(r.x)~=math.floor(q.drop_position.x)or math.floor(r.y)~=math.floor(q.drop_position.y)then return reject("mall_ordinary_inserter_target_changed")end
 local st=e.held_stack
 if st.valid_for_read and(st.name~=q.item or st.quality.name~="normal"or st.prototype.type~="item"or st.health~=1 or st.spoil_tick~=0
  or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data)then return reject("mall_arm_holds_unexpected_item")end
end
for _,q in ipairs(x.core.raw_feeds)do for _,belt in ipairs(q.belts)do local e=live[key(belt)]
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=q.item or(row.quality and row.quality~="normal")then return reject("mall_typed_raw_header_contaminated")end
 end end
end end
local inv=live[key(x.core.chest)].get_inventory(defines.inventory.chest)
local values=inventory(inv,{[x.core.product]=true});if not values then return reject("mall_output_chest_contaminated")end
local stock=values[x.core.product]or 0
if stock>x.core.target then return reject("mall_output_exceeds_configured_stock_target")end
local slots={};for i=1,#inv do if inv[i].valid_for_read then slots[#slots+1]=i end end
return {ok=true,tick=game.tick,machines=rows,output_stock=stock,output_slots=slots,internal_product_wip=wip,
 output_chest_target=x.core.target,measured_per_minute=nil,scope="actual normal production; capped chest stock and separately reported internal WIP"}
'''


MALL_SOURCE_LUA=r'''
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.world_id then return {ok=false,reason="mall_source_world_changed"}end
for item,q in pairs(args.sources)do local e=target(q.position,"transport-belt")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.facing
  or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="mall_native_source_identity_changed",item=item}end
 for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~=item or(row.quality and row.quality~="normal")then return {ok=false,reason="mall_native_source_contaminated",item=item}end
 end end
 local spec=q.priority_splitter;local split=target(spec.position,"splitter")
 if not split or split.force~=f or split.surface~=s or split.unit_number~=spec.unit_number
  or split.direction~=(spec.direction or 0)or split.splitter_input_priority~=(spec.input_priority or"left")
  or split.splitter_output_priority~=(spec.output_priority or"left")then return {ok=false,reason="mall_native_surplus_priority_changed",item=item}end
end
return {ok=true,tick=game.tick}
'''
