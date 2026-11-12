"""Paid critical iron tap, compact magazine assembler and guarded refill source."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .bus_tap import BusTap,reconstruct_bus
from .industrial import free_tap_parameters,industrial_site_anchors,PlanningBudget
from .manufacturing import connect_power_groups
from .planning import overlap,preflight,survey_slots
from .smelting import merge_entities,port
from .transport import plan_connection,underground_proof,xy


TAP_NAME='ammo-iron'
SCOPE_ID='ammo-production'


def ammo_rates(catalog):
    recipe=catalog.recipes['firearm-magazine'];ingredients=recipe['ingredients'];products=recipe['products']
    if (len(ingredients)!=1 or ingredients[0].get('name')!='iron-plate'or ingredients[0].get('type','item')!='item'
            or len(products)!=1 or products[0].get('name')!='firearm-magazine'
            or products[0].get('type','item')!='item'or products[0].get('probability',1)!=1
            or products[0].get('independent_probability',1)!=1 or recipe['energy']<=0):
        raise ValueError('ordinary deterministic iron-only magazine recipe required')
    cycles=60*catalog.entities['assembling-machine-1']['crafting_speed']/recipe['energy']
    return {'magazines_per_minute':cycles*products[0]['amount'],'iron_per_minute':cycles*ingredients[0]['amount'],
            'chest_magazine_limit':catalog.items['firearm-magazine']['stack_size'],
            'capacity_scope':'one native chest slot plus finite machine/arm buffers; rates are nominal'}


def ammo_core(catalog,x,y):
    rated=ammo_rates(catalog)
    if rated['iron_per_minute']>120 or rated['magazines_per_minute']>30:
        raise ValueError('one fast input120 / normal output30 conservative inserter budgets exceeded')
    chest=entity('wooden-chest',x+.5,y+3.5)
    poles=[entity('small-electric-pole',x-1.5,y-.5),entity('small-electric-pole',x-1.5,y+2.5)]
    belts=[entity('transport-belt',x+dx+.5,y-2.5,4)for dx in(-3,-2,-1,0)]
    input_arm=entity('fast-inserter',x+.5,y-1.5,0)
    output_arm=entity('inserter',x+.5,y+2.5,0)
    machine=entity('assembling-machine-1',x+.5,y+.5,recipe='firearm-magazine')
    access=[entity('transport-belt',x+dx+.5,y+3.5,4)for dx in(1,2)]
    ports=[port('iron-plate',x-2.5,y-2.5,4),{'kind':'item','item':'firearm-magazine','direction':'output',
        'name':'wooden-chest','position':deepcopy(chest['position'])}]
    return _plan([chest]+poles+belts+[input_arm,output_arm,machine],ports,reserved_area=[[x-3,y-3],[x+3,y+4]],
        machine=machine,input_arm=input_arm,output_arm=output_arm,chest=chest,output_access=access,rated=rated,
        layout_policy='compact-critical-magazine-v1')


def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])


class AmmoProduction:
    def __init__(self,game,builder,catalog,perimeter,*,bus_factory=None,science=None,green=None,tap=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.tap=tap if tap is not None else BusTap(game,builder,catalog,bus_factory=bus_factory,science=science,green=green)
        self.path=Path(game.cfg.runtime_dir)/'ammo-production.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}

    @property
    def commissioned(self):return self.state.get('phase')=='verified'

    def _identity(self,obs,value):
        return (obs.get('ok',True)and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
                and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==self.catalog.fingerprint)

    def _save(self):_atomic_json(self.path,self.state)

    def prepare(self,obs):
        """Read-only whole prospective tap, adapter, core, power and defense."""
        if self.state:return deepcopy(self.state)if self._identity(obs,self.state)else{'ok':False,'reason':'ammo_world_or_catalog_changed'}
        required=('firearm-magazine','assembling-machine-1','fast-inserter','inserter','wooden-chest')
        if not all(obs.get('enabled_recipes',{}).get(name)for name in required):
            return {'ok':False,'reason':'native_magazine_machine_and_fast_inserter_recipes_required'}
        try:
            rated=ammo_rates(self.catalog);docs=self.tap._documents();bus=docs['factory']['bus'];board=docs['board']
            reconstruct_bus(bus)
            if not all(self._identity(obs,doc)for doc in docs.values())or board.get('shared_main_bus')!=bus:
                return {'ok':False,'reason':'ammo_bus_authority_identity_changed'}
            if self.tap.state and self.tap.state.get('phase')!='verified'and self.tap.state.get('parameters',{}).get('name')!=TAP_NAME:
                return {'ok':False,'reason':'another_bus_tap_cutover_unfinished'}
            if any(p.get('name')==TAP_NAME for p in bus['ports']):
                return {'ok':False,'reason':'ammo_tap_exists_without_factory_checkpoint'}
            choices=free_tap_parameters(bus,name=TAP_NAME,item='iron-plate',demand_per_minute=rated['iron_per_minute'],priority='critical')
            anchors=industrial_site_anchors(bus,board,'steel-plate')[:8]
            north,_,east=reconstruct_bus(bus)[3:];near_x=math.ceil(east)+8;row=anchors[0][1]
            anchors=(((near_x,math.floor(north)-8),(near_x+8,math.floor(north)-8),
                      (near_x,row),(near_x,row+12),(near_x,row+24),(near_x+8,row+12))+tuple(anchors))[:8]
            cores=[ammo_core(self.catalog,x,y)for x,y in anchors]
        except(OSError,KeyError,TypeError,ValueError)as exc:
            return {'ok':False,'reason':'ammo_preparation_context_unavailable','detail':str(exc)}
        cores=[c for c in cores if not any(overlap(c['reserved_area'],b['reserved_area'])
               for b in board['blocks'].values()if b.get('reserved_area'))]
        budget=PlanningBudget(self.game)
        adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda entities:preflight(budget.game,self.builder,obs,entities))
        land=survey_slots(budget.game,[[c['reserved_area']]for c in cores])
        if not land.get('ok'):return land
        cores=[cores[i-1]for i in land.get('legal',[])]
        if not cores:return {'ok':False,'reason':'no_clear_compact_ammo_core_site'}
        nearest=sorted(choices,key=lambda row:min(abs(c['ports'][0]['position']['y']-(row['splitter_y']-1))
                                               +abs(c['ports'][0]['position']['x']-east)for c in cores))
        mixed=nearest[:3]+choices[:3]+nearest[3:];choices=[];seen=set()
        for row in mixed:
            if row['splitter_y']not in seen:choices.append(row);seen.add(row['splitter_y'])
        preview=BusTap(budget.game,adapter,self.catalog);failures=[]
        for parameters in choices[:12]:
            if budget.expired():return budget.failure(failures=failures[-4:])
            tap=preview.prepare(obs,parameters)
            if not tap.get('ok'):failures.append(tap.get('reason'));continue
            source=next(p for p in tap['new_bus']['ports']if p['name']==TAP_NAME)
            ranked=sorted(cores,key=lambda c:abs(xy(c['ports'][0])[0]-xy(source)[0])+abs(xy(c['ports'][0])[1]-xy(source)[1]))
            for core in ranked:
                if budget.expired():return budget.failure(failures=failures[-4:])
                check=preflight(budget.game,self.builder,obs,core['entities']+core['output_access'])
                if not check.get('ok'):failures.append(check.get('reason'));continue
                reserved=core['entities']+core['output_access']+tap['added_entities']
                route=plan_connection(budget.game,adapter,obs,source,core['ports'][0],reserved)
                if not route.get('ok'):failures.append(route.get('reason'));continue
                es=merge_entities(core['entities'],route['entities'])
                power=connect_power_groups(budget.game,adapter,obs,[core],es+core['output_access'])
                if not power.get('ok'):failures.append(power.get('reason'));continue
                es=merge_entities(es,power['entities'])
                whole=preflight(budget.game,self.builder,obs,merge_entities(es,tap['added_entities'],core['output_access']),
                                ignored_units=[tap['remove']['unit_number']])
                if not whole.get('ok'):failures.append(whole.get('reason'));continue
                defense=self.perimeter.prepare_coverage(obs,SCOPE_ID,[core['reserved_area']])
                if not defense.get('ok'):return {'ok':False,'reason':'ammo_defense_preflight_failed','defense':defense}
                plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],underground_pairs=route['underground_pairs'],
                    clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'],layout_policy='defended-critical-ammo-v1')
                return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                    'phase':'tap','parameters':parameters,'tap_installation':tap,'plan':plan,'core':core,
                    'rated':rated,'bindings':{},'defense_preview':defense,'route_metrics':route['route_metrics'],
                    'prepared_tick':obs.get('tick')}
        return {'ok':False,'reason':'no_compact_ammo_site_with_complete_critical_bus_route','failures':failures[-8:]}

    def _bind(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));old=self.state['bindings'].get(_key(spec))
            if old and(not live or live.get('unit_number')!=old):return {'status':'blocked','reason':'owned_ammo_asset_missing_or_replaced','position':spec['position']}
            if live:
                if type(live.get('unit_number'))is not int:return {'status':'blocked','reason':'ammo_native_unit_identity_missing'}
                if old is None:self.state['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def _reserve(self,obs):
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'))
        if not self._identity(obs,board):return {'status':'blocked','reason':'ammo_board_identity_changed'}
        old=board['blocks'].get('ammo_production');plan=self.state['plan']
        if old and old!=plan:return {'status':'blocked','reason':'ammo_reserved_plan_changed'}
        if any(k!='ammo_production'and overlap(plan['reserved_area'],b['reserved_area'])for k,b in board['blocks'].items()if b.get('reserved_area')):
            return {'status':'blocked','reason':'ammo_reservation_conflict'}
        if not old:board['blocks']['ammo_production']=plan;_atomic_json(path,board)
        return None

    def _buffer(self,obs):
        chest=self.state['core']['chest'];unit=self.state['bindings'].get(_key(chest))
        if not unit:return {'ok':False,'reason':'ammo_buffer_not_built'}
        payload=json.dumps(json.dumps(dict(chest,unit_number=unit),separators=(',',':')))
        return self.game.query('''local spec=helpers.json_to_table('''+payload+''');local e=target(spec.position,spec.name)
if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.position.x~=spec.position.x or e.position.y~=spec.position.y then return {ok=false,reason="ammo_buffer_identity_changed"}end
if not a or not a.valid or a.force~=f or a.surface~=s then return {ok=false,reason="ammo_actor_changed"}end
local size=prototypes.item["firearm-magazine"].magazine_size;local inv=e.get_inventory(defines.inventory.chest)
local slots={};local full=0;local actor_full=0
for i=1,#inv do local stack=inv[i];if stack.valid_for_read then
 if stack.name~="firearm-magazine"or stack.quality.name~="normal"or stack.health~=1 or stack.spoil_tick~=0
  or stack.is_item_with_tags or stack.is_item_with_inventory or stack.is_item_with_entity_data then return {ok=false,reason="ammo_buffer_item_quality_or_metadata_changed"}end
 local available=stack.count-(stack.ammo<size and 1 or 0);full=full+available;if available>0 then slots[#slots+1]=i end
end end
local actor=a.get_main_inventory();for i=1,#actor do local stack=actor[i]
 if stack.valid_for_read and stack.name=="firearm-magazine"and stack.quality.name=="normal"then actor_full=actor_full+stack.count-(stack.ammo<size and 1 or 0)end
end
return {ok=true,tick=game.tick,unit_number=e.unit_number,actor_unit=a.unit_number,full_magazines=full,source_slots=slots,
 actor_full_magazines=actor_full,insertable=actor.get_insertable_count{name="firearm-magazine",quality="normal"},bar_slots=inv.get_bar()-1}''')

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;self._save()
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'ammo_world_or_catalog_changed'}
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
            check=preflight(self.game,self.builder,obs,self.state['plan']['entities']+self.state['core']['output_access'])
            if not check.get('ok'):return {'status':'blocked','reason':'ammo_site_changed_after_defense','preflight':check}
            self.state['construction_authorized']=True;self._save()
        chest=self.state['core']['chest']
        if _key(chest)in self.state['bindings']:
            buffer=self._buffer(obs)
            if not buffer.get('ok'):return {'status':'blocked','reason':buffer['reason']}
            if buffer['bar_slots']!=1:
                return dict(name=chest['name'],position=chest['position'],type='configure_ammo_buffer',slots=1,expected_entity_unit=buffer['unit_number'],
                    expected_entity_world_id=obs['world_id'],expected_actor_unit=buffer['actor_unit'],reason='bound automatic ammo buffer to one native chest slot')
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'waiting for actual powered magazine production into the bounded normal output chest'}
        if not self.commissioned:self.state.update(phase='verified',commissioning_proof=proof);self._save()
        return {'status':'succeeded','reason':'normally paid automatic magazine factory commissioned','evidence':proof}

    def proof(self,obs):
        if not self.state or not self._identity(obs,self.state)or self.state.get('phase')=='tap':return None
        defense=self.perimeter.coverage_proof(obs,SCOPE_ID)
        if not defense or not underground_proof(obs,self.state['plan']):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for spec in self.state['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));expected=self.state['bindings'].get(_key(spec))
            if (not expected or not live or live.get('unit_number')!=expected
                    or(spec['name']!='small-electric-pole'and live.get('direction',0)!=spec.get('direction',0))):return None
        core=self.state['core'];specs=[core['machine'],core['input_arm'],core['output_arm']]
        if any(_key(e)not in self.state['bindings']for e in specs):return None
        buffer=self._buffer(obs)
        if not buffer.get('ok')or buffer['bar_slots']!=1 or buffer['full_magazines']<1:return None
        source=self.state['iron_source']
        payload=json.dumps(json.dumps({'specs':[dict(e,unit_number=self.state['bindings'][_key(e)])for e in specs],
                                       'source':source,'chest':dict(core['chest'],unit_number=buffer['unit_number'])},separators=(',',':')))
        native=self.game.query('''local args=helpers.json_to_table('''+payload+''');local machines={}
for _,spec in ipairs(args.specs)do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=(spec.direction or 0)
  or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="ammo_producer_identity_configuration_or_power_changed"}end
 machines[#machines+1]=e
end
local recipe=machines[1].get_recipe();if not recipe or recipe.name~="firearm-magazine"or machines[1].products_finished<1 then return {ok=false,reason="native_magazine_production_required"}end
local input=machines[2];local output=machines[3];local machine=machines[1]
local chest=target(args.chest.position,args.chest.name)
if not chest or chest.unit_number~=args.chest.unit_number or chest.force~=f or chest.surface~=s
 or chest.get_inventory(defines.inventory.chest).get_bar()~=2 then return {ok=false,reason="ammo_buffer_identity_or_bar_changed"}end
if input.drop_position.x<machine.position.x-1.5 or input.drop_position.x>machine.position.x+1.5
 or input.drop_position.y<machine.position.y-1.5 or input.drop_position.y>machine.position.y+1.5 then return {ok=false,reason="ammo_input_not_native_machine_feed"}end
local feed=target({x=math.floor(input.pickup_position.x)+.5,y=math.floor(input.pickup_position.y)+.5},"transport-belt")
if not feed or feed.direction~=4 then return {ok=false,reason="ammo_native_input_belt_missing"}end
for i=1,2 do for _,item in pairs(feed.get_transport_line(i).get_contents())do if item.name~="iron-plate"then return {ok=false,reason="ammo_native_input_contaminated"}end end end
if output.pickup_position.x<machine.position.x-1.5 or output.pickup_position.x>machine.position.x+1.5
 or output.pickup_position.y<machine.position.y-1.5 or output.pickup_position.y>machine.position.y+1.5
 or math.abs(output.drop_position.x-args.chest.position.x)>.49 or math.abs(output.drop_position.y-args.chest.position.y)>.49 then return {ok=false,reason="ammo_output_not_native_chest_feed"}end
local e=target(args.source.position,"transport-belt");if not e or e.unit_number~=args.source.unit_number or e.force~=f or e.direction~=4 then return {ok=false,reason="ammo_iron_tap_identity_changed"}end
for i=1,2 do for _,item in pairs(e.get_transport_line(i).get_contents())do if item.name~="iron-plate"then return {ok=false,reason="ammo_iron_tap_contaminated"}end end end
local split=args.source.priority_splitter;local p=target(split.position,"splitter")
if not p or p.unit_number~=split.unit_number or p.force~=f or p.direction~=0 or p.splitter_input_priority~="left"or p.splitter_output_priority~="right"then return {ok=false,reason="ammo_critical_priority_changed"}end
return {ok=true,tick=game.tick,assembler_unit=machine.unit_number,products_finished=machine.products_finished}''')
        if not native.get('ok'):return None
        return {'tick':native['tick'],'native':native,'buffer':buffer,'rated':self.state['rated'],'defense_coverage':defense,
                'output':dict(core['ports'][1],unit_number=buffer['unit_number'],world_id=obs['world_id']),
                'scope':'actual normal assembler output and bounded buffer; nominal30/min is not sustained-throughput proof'}

    def ensure_full_magazines(self,obs,count):
        if type(count)is not int or not 1<=count<=500:raise ValueError('bounded refill target must be1..500 full normal magazines; each transfer remains at most50')
        if not self.commissioned:return None
        if not self._identity(obs,self.state):return {'status':'blocked','reason':'ammo_world_or_catalog_changed'}
        buffer=self._buffer(obs)
        if not buffer.get('ok'):return {'status':'blocked','reason':buffer['reason']}
        if buffer['bar_slots']!=1:return {'status':'blocked','reason':'ammo_buffer_bar_changed'}
        needed=count-buffer['actor_full_magazines']
        if needed<=0:return None
        if not buffer['full_magazines']:return {'status':'waiting','reason':'waiting for normally assembled factory magazines'}
        if buffer['insertable']<=0:return {'status':'blocked','reason':'actor_inventory_has_no_ammo_capacity'}
        chest=self.state['core']['chest']
        action={k:chest[k]for k in('name','position')}
        return dict(action,type='take_full_magazines',item='firearm-magazine',count=min(50,needed,buffer['full_magazines'],buffer['insertable']),
            source_slots=buffer['source_slots'][:16],expected_entity_unit=buffer['unit_number'],
            expected_entity_world_id=obs['world_id'],expected_actor_unit=buffer['actor_unit'],
            reason='normally collect full magazines from the commissioned exact ammo buffer')
