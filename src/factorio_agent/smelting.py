"""Normally supplied PCB smelting district; actor never feeds these furnaces."""
from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .board import entity
from .power_grid import plan_connection as power_connection
from .transport import plan_connection as belt_connection,underground_proof,xy
from .planning import preflight

FURNACE_NAMES = frozenset({'stone-furnace', 'steel-furnace', 'electric-furnace'})


def smelting_site_anchors(sources):
    """Keep accepted legacy ordering, then search a bounded current-source grid."""
    old = ((24,-48),(-24,-48),(24,-64),(-24,-32),(32,-48),(-40,-32),(8,-16),(-40,-48),(24,-24))
    points = [p['position'] for p in sources.values()]
    bx = math.floor(sum(p['x'] for p in points)/len(points)/16)*16
    by = math.floor((sum(p['y'] for p in points)/len(points)-32)/16)*16
    offsets = ((0,0),(32,0),(-32,0),(0,-32),(32,-32),(-32,-32),(0,32),(32,32),
               (-32,32),(64,0),(-64,0),(0,-64),(0,64),(64,-32),(-64,-32),(32,-64))
    return tuple(dict.fromkeys((*old, *((bx+dx,by+dy)for dx,dy in offsets))))


def port(item,x,y,facing,direction='input'):
    return {'kind':'item','item':item,'position':{'x':x,'y':y},'facing':facing,'direction':direction}


def validate_capacity(parameters):
    if not isinstance(parameters,dict) or set(parameters)!={'resource','rate_per_minute'}:
        raise ValueError('smelting_capacity requires resource and rate_per_minute')
    if parameters['resource'] not in ('iron-ore','copper-ore'):
        raise ValueError('only iron/copper plate rows can expand here')
    rate=parameters['rate_per_minute']
    if type(rate) not in (int,float) or not math.isfinite(rate) or not 0<rate<=450:
        raise ValueError('smelting rate must be finite in (0,450]')


def furnace_bank(x,y,count,resource,*,furnace_name='stone-furnace'):
    """Separate ore/coal lanes side-load a straight shared furnace input belt."""
    product={'iron-ore':'iron-plate','copper-ore':'copper-plate'}[resource]
    es=[]
    for i in range(count):
        dx=4*i
        es.extend([entity(furnace_name,x+dx,y),entity('inserter',x+dx+.5,y-1.5,0),
                   entity('inserter',x+dx+.5,y+1.5,0),entity('small-electric-pole',x+dx+1.5,y+.5)])
    # The empty west stub makes the coal junction a straight belt, so the
    # south feed side-loads one lane instead of turning both lanes around a bend.
    es += [entity('transport-belt',x+dx+.5,y-2.5,4) for dx in range(-4,4*(count-1)+1)]
    # Keep the output at the six-slot block boundary as machines are added.
    es += [entity('transport-belt',x+dx+.5,y+2.5,4) for dx in range(23)]
    es += [entity('transport-belt',x-1.5,y-4.5,8),entity('transport-belt',x-1.5,y-3.5,8),
           entity('transport-belt',x-2.5,y-.5,0),entity('transport-belt',x-2.5,y-1.5,0)]
    ports=[port(resource,x-1.5,y-4.5,8),port('coal',x-2.5,y-.5,0),
           port(product,x+22.5,y+2.5,4,'output')]
    return _plan(es,ports,template='dual_lane_furnace_row',resource=resource,product=product,count=count,capacity_slots=6)


def merge_entities(*groups):
    result={}
    for group in groups:
        for e in group:
            key=e['name'],*xy(e)
            previous=result.get(key)
            if previous and any(previous.get(k)!=e.get(k) for k in ('direction','belt_to_ground_type','recipe')):
                raise ValueError('conflicting shared port configuration')
            result[key]=e
    return list(result.values())


def bank_capacities(catalog, banks):
    """Derive planned product/minute from unique furnace entities, never row counts.

    These are catalog nameplate capacities, independent of current ore, fuel,
    power or downstream load. Rates retain the resource keys used by the state.
    """
    supported = {'stone-furnace', 'steel-furnace', 'electric-furnace'}
    rates, seen = {}, {}
    for bank in banks:
        resource, product = bank['resource'], bank['product']
        recipe = catalog.recipes.get(product)
        if not recipe:
            raise ValueError('missing smelting product recipe: '+product)
        duration = recipe.get('energy', 0)
        products = [p for p in recipe.get('products', []) if p['name'] == product]
        if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
                or any(p.get('probability', 1) != 1 or p.get('independent_probability', 1) != 1
                       or type(p.get('amount')) not in (int, float) for p in products)):
            raise ValueError('deterministic smelting recipe required: '+product)
        amount = sum(p['amount'] for p in products)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('positive smelting product yield required: '+product)
        rates.setdefault(resource, 0.0)
        found = False
        for furnace in bank['entities']:
            name = furnace['name']
            prototype = catalog.entities.get(name, {})
            if name not in supported:
                if prototype.get('type') == 'furnace':
                    raise ValueError('unsupported planned furnace: '+name)
                continue
            found = True
            key = name, *xy(furnace)
            owner = resource, product
            if key in seen:
                if seen[key] != owner:
                    raise ValueError('one furnace assigned to multiple smelting products')
                continue
            speed = prototype.get('crafting_speed', 0)
            if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
                raise ValueError('missing furnace crafting speed: '+name)
            seen[key] = owner
            rates[resource] += speed*60/duration*amount
        if not found:
            raise ValueError('smelting bank has no supported furnace entities: '+resource)
    return rates


class SmeltingDistrict:
    def __init__(self,game,bootstrap,builder,catalog):
        self.game,self.bootstrap,self.builder,self.catalog=game,bootstrap,builder,catalog
        self.path=Path(game.cfg.runtime_dir)/'smelting-district.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def owns_furnace(self,e,obs):
        if self.state.get('world_id')!=obs['world_id'] or e['name'] not in FURNACE_NAMES:return False
        return any(w['name'] in FURNACE_NAMES and w['position']==e['position'] for w in self.state.get('plan',{}).get('entities',[]))

    def clean_feed_baseline(self,obs):
        if self.state.get('automatic_feed_baseline'):return None
        furnaces=[e for e in obs['entities'] if self.owns_furnace(e,obs)]
        if any(e.get('products_finished',0)>0 or e.get('remaining_burning_fuel',0)>0 for e in furnaces):
            return {'status':'blocked','reason':'prebaseline_furnace_activity_requires_explicit_review'}
        for e in furnaces:
            for item in ('coal','wood'):
                count=e.get('inventory',{}).get(item,0)
                if count:return {'type':'take','name':e['name'],'position':e['position'],'inventory':'fuel',
                                 'item':item,'count':count,'reason':'normally recover legacy bootstrap fuel before automatic-feed baseline'}
        self.state['automatic_feed_baseline']={'tick':obs['tick'],'preexisting_units':[e['unit_number'] for e in furnaces],
            'products_finished':0,'remaining_burning_fuel':0,'fuel_inventory_empty':True}
        _atomic_json(self.path,self.state)
        return None

    def capacity_step(self,obs,parameters):
        validate_capacity(parameters)
        if not self.state or self.state.get('world_id')!=obs['world_id']:
            return {'status':'blocked','reason':'current_world_smelting_district_required'}
        resource=parameters['resource'];rate=parameters['rate_per_minute']
        mining=json.loads((self.path.parent/'mining-outposts.json').read_text(encoding='utf-8'))
        available=sum(b['nominal_rate_per_minute'] for b in mining.get('banks',[]) if b.get('ready') and b['plan']['resource']==resource)
        if mining.get('world_id')!=obs['world_id'] or available<rate:
            return {'status':'blocked','reason':'expand_connected_mining_before_smelting','available_nominal_ore_rate':available}
        old=next(b for b in self.state['banks'] if b['resource']==resource)
        existing={xy(e):e['name'] for e in old['entities'] if e['name'] in FURNACE_NAMES}
        if not existing or set(existing.values())-{'stone-furnace','steel-furnace'}:
            return {'status':'blocked','reason':'row_expansion_requires_existing_two_by_two_burner_furnaces'}
        preferred=old.get('furnace_name',next(iter(existing.values())))
        if preferred not in {'stone-furnace','steel-furnace'}:
            return {'status':'blocked','reason':'unsupported_expansion_furnace_type'}
        each=self.catalog.entities[preferred]['crafting_speed']*60/self.catalog.recipes[old['product']]['energy']
        paired=self.state.get('paired',False)
        current=bank_capacities(self.catalog,[old])[resource]
        count=old.get('per_side_count',old['count'])+max(0,math.ceil((rate-current)/each/(2 if paired else 1)))
        if count>6:return {'status':'blocked','reason':'reserved_row_has_six_furnace_slots'}
        first=next(e for e in old['entities'] if e['name'] in FURNACE_NAMES)
        if paired:
            from .paired_smelting import paired_bank
            new=paired_bank(first['position']['x'],first['position']['y'],count,resource,furnace_name=preferred)
        else:new=furnace_bank(first['position']['x'],first['position']['y'],count,resource,furnace_name=preferred)
        for e in new['entities']:
            if e['name'] in FURNACE_NAMES and xy(e) in existing:e['name']=existing[xy(e)]
        for row in new.get('producer_rows',{}).values():
            for e in row:
                if xy(e) in existing:e['name']=existing[xy(e)]
        extra={k:v for k,v in new.items() if k not in ('ok','reason','entities','ports','required_items')}
        if preferred!='stone-furnace':extra['furnace_name']=preferred
        new=_plan(new['entities'],new['ports'],**extra)
        if new!=old:
            board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
            if board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
                return {'status':'blocked','reason':'board_identity_or_catalog_changed'}
            if old['ports'][-1]!=new['ports'][-1] and any(c['source']==old['ports'][-1]['position'] for c in board.get('connections',[])):
                return {'status':'blocked','reason':'old_movable_output_has_registered_consumer_requires_parallel_cutover'}
            es=merge_entities(self.state['plan']['entities'],new['entities'])
            boxes=[self.builder._occupied_by_plan([e]) for e in es]
            if sum(map(len,boxes))!=len(set().union(*boxes)):
                return {'status':'blocked','reason':'expanded_row_prospective_overlap'}
            native=self.builder.can_place(es)
            if not native.get('ok'):return {'status':'blocked','reason':'expanded_row_native_preflight_failed','blocked':native.get('blocked')}
            banks=[new if b['resource']==resource else b for b in self.state['banks']]
            extra={k:v for k,v in self.state['plan'].items() if k not in ('ok','reason','entities','ports','required_items')}
            ports=self.state['plan']['ports'] if paired else [p for b in banks for p in b['ports'] if p['direction']=='output']
            plan=_plan(es,ports,**extra)
            self.state.update(plan=plan,banks=banks)
            self.state['nominal_smelting_capacity_per_minute']=bank_capacities(self.catalog,banks)
            _atomic_json(self.path,self.state)
            board['blocks']['smelting_district']=plan;_atomic_json(board_path,board)
        rates=bank_capacities(self.catalog,self.state['banks'])
        if self.state.get('nominal_smelting_capacity_per_minute')!=rates:
            self.state['nominal_smelting_capacity_per_minute']=rates
            _atomic_json(self.path,self.state)
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if proof:return {'status':'succeeded','reason':'expanded furnace row produces with fixed boundary output','evidence':proof}
        return {'status':'waiting','reason':'waiting for added furnaces to smelt ordinary belt inputs'}

    def prepare(self,obs):
        from .industrial import PlanningBudget
        budget=PlanningBudget(self.game);game=budget.game
        if not obs.get('technologies',{}).get('logistics'):
            return {'ok':False,'reason':'native_logistics_research_required'}
        p=self.path.parent/'mining-outposts.json'
        mining=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
        if mining.get('world_id')!=obs['world_id']:return {'ok':False,'reason':'current_world_mining_banks_required'}
        sources={}
        for resource,rate in (('iron-ore',60),('copper-ore',30),('coal',30)):
            bank=next((b for b in mining['banks'] if b.get('ready') and b['plan']['resource']==resource
                       and b['nominal_rate_per_minute']>=rate),None)
            if not bank:return {'ok':False,'reason':f'one_ready_{resource}_bank_with_nominal_{rate}_required'}
            sources[resource]=bank['plan']['ports'][0]
        board_path=self.path.parent/'pcb-board.json'
        board=json.loads(board_path.read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'ok':False,'reason':'board_identity_or_catalog_changed'}
        counts={r:math.ceil(rate*self.catalog.recipes[product]['energy']/60/self.catalog.entities['stone-furnace']['crafting_speed'])
                for r,product,rate in (('iron-ore','iron-plate',60),('copper-ore','copper-plate',30))}
        candidates=[]
        for x,y in smelting_site_anchors(sources):
            area=[[x-8,y-8],[x+24,y+24]]
            if any(area[0][0]<b['reserved_area'][1][0] and area[1][0]>b['reserved_area'][0][0]
                   and area[0][1]<b['reserved_area'][1][1] and area[1][1]>b['reserved_area'][0][1]
                   for b in board.get('blocks',{}).values()if b.get('reserved_area')):continue
            banks=[furnace_bank(x,y,counts['iron-ore'],'iron-ore'),furnace_bank(x,y+12,counts['copper-ore'],'copper-ore')]
            splitter=dict(entity('splitter',x-5.5,y+4,4),_width=2,_height=1)
            split=[splitter,entity('transport-belt',x-7.5,y+3.5,4),entity('transport-belt',x-6.5,y+3.5,4),
                   entity('transport-belt',x-4.5,y+3.5,4),entity('transport-belt',x-4.5,y+4.5,4)]
            es=merge_entities(*(b['entities'] for b in banks),split)
            candidates.append({'area':area,'banks':banks,'entities':es,
                'coal_input':port('coal',x-7.5,y+3.5,4),
                'coal_branches':[port('coal',x-4.5,y+3.5,4,'output'),port('coal',x-4.5,y+4.5,4,'output')]})
        args=json.dumps(json.dumps(candidates,separators=(',',':')))
        survey=game.query('''
local plans=helpers.json_to_table('''+args+''');local legal={}
for i,plan in ipairs(plans) do
 local clear=s.count_entities_filtered{area=plan.area,type="resource"}==0
 if clear then for _,e in pairs(s.find_entities_filtered{area=plan.area}) do
  if e.type~="electric-pole" and e.type~="character" and not e.prototype.collision_mask.colliding_with_tiles_only then clear=false;break end
 end end
 if clear then for _,tile in pairs(s.find_tiles_filtered{area=plan.area}) do
  for layer in pairs(prototypes.entity["stone-furnace"].collision_mask.layers) do
   if tile.collides_with(layer) then clear=false;break end
  end
  if not clear then break end
 end end
 if clear then for _,e in ipairs(plan.entities) do
  local old=target(e.position,e.name)
  if not (old and old.force==f and old.type=="electric-pole")
   and not s.can_place_entity{name=e.name,position=e.position,direction=e.direction,force=f} then clear=false;break end
 end end
 if clear then legal[#legal+1]=i end
end
return {ok=true,legal=legal}
''')
        if not survey.get('ok'):return survey
        failures=[]
        for index in survey.get('legal',[]):
            if budget.expired():break
            site=candidates[index-1];es=site['entities'];routes=[];good=True
            connections=[]
            for bank,branch in zip(site['banks'],site['coal_branches']):
                connections.append((branch,next(p for p in bank['ports'] if p['item']=='coal')))
                connections.append((sources[bank['resource']],next(p for p in bank['ports'] if p['item']==bank['resource'])))
            connections.append((sources['coal'],site['coal_input']))
            for source,destination in connections:
                if budget.expired():good=False;failures.append({'stage':'planning_budget','reason':'smelting_planning_budget_exhausted'});break
                route=belt_connection(game,self.builder,obs,source,destination,es)
                if not route.get('ok'):
                    failures.append({'stage':'corridor','area':site['area'],'item':source['item'],'source':source['position'],
                                     'destination':destination['position'],'reason':route.get('reason')});good=False;break
                routes.append(route);es=merge_entities(es,route['entities'])
            if not good:continue
            for bank in site['banks']:
                pole=next(e for e in bank['entities'] if e['name']=='small-electric-pole')
                power=power_connection(game,self.builder,obs,pole['position'],[e for e in es if e['name']!='small-electric-pole'])
                if not power.get('ok'):
                    failures.append({'stage':'power','area':site['area'],'reason':power.get('reason')});good=False;break
                es=merge_entities(es,power['entities'])
            if not good:continue
            boxes=[self.builder._occupied_by_plan([e]) for e in es]
            if sum(map(len,boxes))!=len(set().union(*boxes)):
                failures.append({'stage':'whole_geometry','area':site['area'],'reason':'whole_prospective_plan_overlaps'});continue
            # Corridors may include exact approved natural clearances. A raw
            # can_place repeat rejects those trees and discards its own earlier
            # route approval. Recheck the complete resource/ownership footprint
            # with the same native preflight used by paid clearance/building.
            whole=preflight(game,self.builder,obs,es)
            if not whole.get('ok'):
                failures.append({'stage':'whole_preflight','area':site['area'],'reason':whole.get('reason')});continue
            pairs=[pair for route in routes for pair in route.get('underground_pairs',[])]
            plan=_plan(es,[p for b in site['banks'] for p in b['ports'] if p['direction']=='output'],
                reserved_area=site['area'],layout_policy='pcb-smelting-district-v1',underground_pairs=pairs,
                clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id'])
            return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'plan':plan,
                    'banks':site['banks'],'routes':[{'metrics':r['route_metrics'],'pairs':r['underground_pairs']} for r in routes],
                    'nominal_input_rates':{'iron-ore':60,'copper-ore':30},'coal_splitter_policy':'ordinary balanced outputs; existing power feed remains separate'}
        stages={stage:sum(f['stage']==stage for f in failures)for stage in dict.fromkeys(f['stage']for f in failures)}
        return {'ok':False,'reason':'no_clear_pcb_smelting_district_with_corridors','legal_sites':len(survey.get('legal',[])),
                'candidate_count':len(candidates),'failures':failures[-6:],
                'evidence':{'legal_sites':len(survey.get('legal',[])),'candidate_count':len(candidates),
                    'failure_stage_counts':stages,'last_failure_reason':failures[-1]['reason']if failures else'no_legal_land_site',
                    'planning_budget_exhausted':budget.expired()}}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;_atomic_json(self.path,self.state)
        if self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'smelting_world_or_catalog_changed'}
        # Republish after a crash between state persistence and board publication.
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        if board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'board_identity_or_catalog_changed'}
        if board['blocks'].get('smelting_district')!=self.state['plan']:
            if 'smelting_district' in board['blocks']:
                return {'status':'blocked','reason':'smelting_reservation_changed'}
            board['blocks']['smelting_district']=self.state['plan'];_atomic_json(board_path,board)
        cleanup=self.clean_feed_baseline(obs)
        if cleanup is not None:return cleanup
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if proof:return {'status':'succeeded','reason':'mined ore and split coal normally smelted onto fixed plate ports','evidence':proof}
        return {'status':'waiting','reason':'waiting for normal belt-fed ore/coal and native furnace production'}

    def proof(self,obs):
        if not self.state or not self.state.get('automatic_feed_baseline') or not underground_proof(obs,self.state['plan']):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for wanted in self.state['plan']['entities']:
            found=actual.get((wanted['name'],*xy(wanted)))
            if found is None:return None
            if wanted['name'] in {'transport-belt','underground-belt','inserter','splitter'} and found['direction']!=wanted['direction']:return None
            if wanted['name']=='inserter' and (not found.get('electric_network_connected') or found.get('energy',0)<=0):return None
        evidence=[]
        for bank in self.state['banks']:
            furnaces=[actual[(e['name'],*xy(e))] for e in bank['entities'] if e['name'] in FURNACE_NAMES]
            if not furnaces or any(e.get('products_finished',0)<1 for e in furnaces):return None
            for furnace in furnaces:
                if furnace['name'] in {'stone-furnace','steel-furnace'}:
                    if not (furnace.get('remaining_burning_fuel',0)>0 or any(furnace.get('inventory',{}).get(fuel,0)>0 for fuel in ('coal','wood'))):return None
                elif not furnace.get('electric_network_connected') or furnace.get('energy',0)<=0:return None
            poles=[actual[(e['name'],*xy(e))] for e in bank['entities'] if e['name']=='small-electric-pole']
            if any(not e.get('electric_network_id') for e in poles):return None
            plate_count=sum(actual[(e['name'],*xy(e))].get('belt_inventory',{}).get(bank['product'],0)
                            for e in bank['entities'] if e['name']=='transport-belt')
            if not plate_count:return None
            evidence.append({'product':bank['product'],'furnace_units':[e['unit_number'] for e in furnaces],
                'machine_products_finished':[e['products_finished'] for e in furnaces],'plates_on_belts':plate_count})
        payload=json.dumps(json.dumps([{'resource':b['resource'],'positions':[e['position'] for e in b['entities']
            if e['name']=='transport-belt' and e['position']['y']==b['entities'][0]['position']['y']-2.5
            and e['position']['x']>=b['entities'][0]['position']['x']+.5]} for b in self.state['banks']],separators=(',',':')))
        lanes=self.game.query('''
local banks=helpers.json_to_table('''+payload+''');local result={}
for _,bank in ipairs(banks) do
 local counts={{},{}}
 for _,p in ipairs(bank.positions) do
  local belt=target(p,"transport-belt");if not belt then return {ok=false,reason="feed_belt_missing"} end
  for lane=1,2 do for _,item in pairs(belt.get_transport_line(lane).get_contents()) do
   counts[lane][item.name]=(counts[lane][item.name] or 0)+item.count
  end end
 end
 result[#result+1]={resource=bank.resource,lanes=counts}
end
return {ok=true,banks=result}
''')
        if not lanes.get('ok'):return None
        for row in lanes['banks']:
            kinds=[set(lane) for lane in row['lanes']]
            if any(len(k)!=1 for k in kinds) or kinds[0]&kinds[1] or set.union(*kinds)!={row['resource'],'coal'}:return None
        return {'tick':obs['tick'],'automatic_feed_baseline':self.state['automatic_feed_baseline'],
                'reserved_area':self.state['plan']['reserved_area'],'banks':evidence,'native_feed_lanes':lanes['banks'],
                'underground_pairs':len(self.state['plan']['underground_pairs']),
                'splitter_count':sum(e['name']=='splitter' for e in self.state['plan']['entities']),
                'scope':'ordinary mined/belt-fed smelting and output, no actor furnace feeding; sustained rate requires a downstream load and timed proof'}
