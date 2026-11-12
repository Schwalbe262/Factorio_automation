"""Permanent typed plastic/sulfur cells with native guarded fluid handoffs."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import time

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_ai.world_catalog import _yield
from .board import entity
from .manufacturing import connect_power_groups
from .petroleum import compile_pipe_crossings,pipeline_extent,basic_petroleum_rates
from .planning import overlap,preflight,survey_slots
from .resource_policy import mining_reservations
from .routing import VECTORS,route_trunks
from .smelting import merge_entities,port
from .transport import plan_connection as belt_connection,surface_pipe_proof,underground_proof,unbuilt_external_keepouts,xy


PRODUCTS={'plastic-bar':'plastics','sulfur':'sulfur-processing'}
SOURCE_KEYS={'petroleum-gas':'petroleum-gas','water':'water','coal':'plastic-coal'}


def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _key(e):return e['name']+':'+str(e['position']['x'])+':'+str(e['position']['y'])
def _identity(obs,value,catalog):return (obs.get('ok',True)and isinstance(obs.get('world_id'),str)and bool(obs['world_id'])
    and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==catalog.fingerprint)


def candidate_failure(candidate,stage,result,*,item=None,source=None,destination=None):
    """Bounded diagnostic receipt; never persist entire route paths or query logs."""
    row={'candidate':deepcopy(candidate),'stage':stage,'reason':result.get('reason')or stage+'_failed'}
    if item is not None:row['item']=item
    for name,port_ in(('source',source),('destination',destination)):
        if port_ is not None:row[name]={k:deepcopy(port_[k])for k in('kind','item','name','position','facing','unit_number')if k in port_}
    for key in('visited','native_reason','entity','position','route_metrics','width','height','native_limit'):
        if key in result:row[key]=deepcopy(result[key])
    for key in('preflight','power'):
        nested=result.get(key)
        if isinstance(nested,dict):row[key]={k:deepcopy(nested[k])for k in('reason','native_reason','entity','position')if k in nested}
    blocked=result.get('blocked')
    if isinstance(blocked,list):
        row['blocked_count']=len(blocked)
        if blocked:row['first_blocked']={k:deepcopy(blocked[0][k])for k in('reason','name','position')if k in blocked[0]}
    return row


def validate_chemical(parameters):
    if(not isinstance(parameters,dict)or set(parameters)!={'product'}
            or not isinstance(parameters['product'],str)or parameters['product']not in PRODUCTS):
        raise ValueError('chemical_production requires product plastic-bar or sulfur')


def chemical_rates(catalog,product):
    if product not in PRODUCTS:raise ValueError('supported permanent chemical product required')
    recipe=catalog.recipes[product];machine=catalog.entities['chemical-plant']
    if(recipe.get('category')not in machine['crafting_categories']or len(recipe['products'])!=1
            or recipe['products'][0]['name']!=product or recipe['products'][0].get('type','item')!='item'
            or recipe['products'][0].get('probability',1)!=1 or recipe['products'][0].get('independent_probability',1)!=1
            or type(recipe['energy'])not in(int,float)or recipe['energy']<=0):
        raise ValueError('one ordinary solid chemistry output required')
    crafts=60*machine['crafting_speed']/recipe['energy'];yield_=_yield(recipe['products'][0])
    if any(not math.isfinite(v)or v<=0 for v in(crafts,yield_)):raise ValueError('positive deterministic chemistry rates required')
    return {'product':product,'items_per_minute':crafts*yield_,
        'inputs_per_minute':{p['name']:crafts*p['amount']for p in recipe['ingredients']},
        'scope':'installed machine/recipe nameplate, independently limited by actual shared feed capacity'}


def blue_chemistry_demand(catalog,science_per_minute=20):
    if type(science_per_minute)not in(int,float)or not math.isfinite(science_per_minute)or science_per_minute<=0:
        raise ValueError('finite positive science rate required')
    wanted={'plastic-bar':0.0,'sulfur':0.0}
    def visit(item,count,stack=()):
        if item in wanted:wanted[item]+=count;return
        if item in('iron-plate','copper-plate','steel-plate','coal','stone'):return
        if item in stack:raise ValueError('cyclic science recipe dependency')
        recipe=catalog.recipe_for_product(item)
        if not recipe:raise ValueError('science dependency recipe missing: '+item)
        amount=sum(_yield(p)for p in recipe['products']if p['name']==item)
        for ingredient in recipe['ingredients']:
            if ingredient.get('type','item')!='item':raise ValueError('unexpected fluid before chemical demand boundary')
            visit(ingredient['name'],count/amount*ingredient['amount'],stack+(item,))
    visit('chemical-science-pack',science_per_minute)
    inputs={}
    for product,amount in wanted.items():
        recipe=catalog.recipes[product];yield_=_yield(recipe['products'][0])
        for ingredient in recipe['ingredients']:
            inputs[ingredient['name']]=inputs.get(ingredient['name'],0)+amount/yield_*ingredient['amount']
    per_refinery=basic_petroleum_rates(catalog)['petroleum_per_minute']
    return {'science_per_minute':science_per_minute,'products_per_minute':wanted,'inputs_per_minute':inputs,
            'basic_refineries_required_at_nameplate':math.ceil(inputs.get('petroleum-gas',0)/per_refinery),
            'scope':'recipe-derived consumption demand, not a throughput or source-capacity promise'}


def chemical_fluid_ports(catalog,product):
    recipe=catalog.recipes[product];boxes=sorted((b for b in catalog.entities['chemical-plant']['fluidbox_prototypes']
        if b.get('production_type')=='input'),key=lambda b:b['index']);ports={};ordinal=0
    for ingredient in recipe['ingredients']:
        if ingredient.get('type')!='fluid':continue
        ordinal+=1;slot=ingredient.get('fluidbox_index',ordinal)
        if type(slot)is not int or not 1<=slot<=len(boxes):raise ValueError('supported recipe-relative chemistry fluid index required')
        box=boxes[slot-1];connections=[c for c in box['pipe_connections']if c.get('connection_type','normal')=='normal']
        if len(connections)!=1 or len(connections[0].get('positions',[]))!=4:raise ValueError('one native chemistry port with four rotations required')
        c=connections[0];p=c['positions'][0];dx,dy=VECTORS[c['direction']]
        ports[ingredient['name']]={'fluidbox_index':box['index'],'offset':{'x':p['x']+dx,'y':p['y']+dy}}
    return ports


def chemical_core(catalog,product,x,y):
    rates=chemical_rates(catalog,product);bindings=chemical_fluid_ports(catalog,product)
    if any(type(v)not in(int,float)or not math.isfinite(v)or v%1!=.5 for v in(x,y)):
        raise ValueError('half-tile chemical plant centers required')
    expected={'petroleum-gas':{'x':-1,'y':-2}}if product=='plastic-bar'else{
        'water':{'x':-1,'y':-2},'petroleum-gas':{'x':1,'y':-2}}
    if {name:p['offset']for name,p in bindings.items()}!=expected:raise ValueError('installed chemistry physical input mapping changed')
    machine=entity('chemical-plant',x,y,recipe=product);groups={};fluid_ports={}
    for fluid,binding in bindings.items():
        dx=binding['offset']['x'];row=-6 if product=='sulfur'and fluid=='petroleum-gas'else-4
        groups[fluid]=[entity('pipe',x+i,y+row)for i in range(-6,dx+1)]
        groups[fluid]+=[entity('pipe',x+dx,y+i)for i in range(row+1,-1)]
        fluid_ports[fluid]={'kind':'fluid','item':fluid,'direction':'input','facing':4,'name':'pipe',
            'position':{'x':x-6,'y':y+row},'fluidbox_index':1,'machine_fluidbox_index':binding['fluidbox_index']}
    output_arm=entity('fast-inserter',x,y+2,0)
    es=merge_entities(*(groups.values()))+[entity('small-electric-pole',x-2,y+1)]
    coal_port=None;input_arm=None
    if product=='plastic-bar':
        input_arm=entity('fast-inserter',x-2,y,12);es += [input_arm]
        es += [entity('transport-belt',x+i,y,4)for i in range(-6,-2)]
        coal_port=port('coal',x-6,y,4)
    es += [entity('transport-belt',x+i,y+3,4)for i in range(10)]+[output_arm,machine]
    output=port(product,x+9,y+3,4,'output');access=[entity('transport-belt',x+i,y+3,4)for i in(10,11)]
    return _plan(es,list(fluid_ports.values())+([coal_port]if coal_port else[])+[output],product=product,machine=machine,
        fluid_groups=groups,fluid_ports=fluid_ports,fluidbox_bindings=bindings,coal_port=coal_port,input_arm=input_arm,
        output_arm=output_arm,output_port=output,output_access=access,rated=rates,
        reserved_area=[[x-6.5,y-6.5],[x+11.5,y+4.5]],
        expansion_slots=[{'position':{'x':x+6,'y':y},'built':False}],
        expansion_keepouts=[entity('chemical-plant',x+6,y)],layout_policy='fixed-typed-chemistry-cell-v1')


def typed_fluid_edges(nodes,pairs,machine,fluidbox,stub):
    by={xy(e):e for e in nodes};edges=[];seen=set()
    def add(a,ai,b,bi):
        key=tuple(sorted(((_key(a),ai),(_key(b),bi))))
        if key not in seen:seen.add(key);edges.append({'a':dict(a,fluidbox_index=ai),'b':dict(b,fluidbox_index=bi)})
    def surface(e,d):return e['name']!='pipe-to-ground'or e['direction']==d
    for p,e in by.items():
        for d,(dx,dy)in VECTORS.items():
            other=by.get((p[0]+dx,p[1]+dy))
            if other and surface(e,d)and surface(other,(d+8)%16):add(e,1,other,1)
    for pair in pairs:add(by[tuple(pair['first'])],1,by[tuple(pair['last'])],1)
    add(machine,fluidbox,by[xy(stub)],1)
    return edges


def source_owned_apron(builder,source,blocks,maximum_steps=4):
    """Producer-approved straight exit, never a wholesale reservation exemption."""
    if type(maximum_steps)is not int or not 1<=maximum_steps<=4:raise ValueError('source-owned apron is bounded to four outward cells')
    def overlaps_cell(p,area):return(p['x']+.49>area[0][0]and p['x']-.49<area[1][0]
        and p['y']+.49>area[0][1]and p['y']-.49<area[1][1])
    owners=[(index,block)for index,block in enumerate(blocks)if block.get('reserved_area')
            and overlaps_cell(source['position'],block['reserved_area'])]
    if not owners:return {'ok':True,'owner_index':None,'path':[]}
    if len(owners)!=1:return {'ok':False,'reason':'chemical_source_overlapping_owner_reservations'}
    index,block=owners[0]
    matching=[p for p in block.get('ports',[])if all(p.get(k)==source.get(k)
        for k in('kind','item','direction','facing','name','position','fluidbox_index'))]
    if(len(matching)!=1 or not source.get('defense_scope_id')or block.get('defense_scope_id')!=source['defense_scope_id']
            or source.get('facing')not in VECTORS):return {'ok':False,'reason':'chemical_source_apron_owner_or_typed_port_unverified'}
    occupied=builder._occupied_by_plan(block['entities']);dx,dy=VECTORS[source['facing']];path=[deepcopy(source['position'])]
    for step in range(1,maximum_steps+1):
        p={'x':source['position']['x']+step*dx,'y':source['position']['y']+step*dy}
        if(p['x'],p['y'])in occupied:return {'ok':False,'reason':'chemical_source_apron_prospective_producer_obstruction'}
        if any(i!=index and overlaps_cell(p,b['reserved_area'])for i,b in enumerate(blocks)if b.get('reserved_area')):
            return {'ok':False,'reason':'chemical_source_apron_hits_foreign_reservation'}
        path.append(p)
        if not overlaps_cell(p,block['reserved_area']):return {'ok':True,'owner_index':index+1,'path':path}
    return {'ok':False,'reason':'chemical_source_apron_has_no_exit_within_four_steps'}


CHEMICAL_PIPE_SURVEY_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local b=args.bounds;local hard={};local soft={};local apron={}
local function same(p,q)return p.x==q.x and p.y==q.y end
local function key(p)return p.x..":"..p.y end
for _,p in ipairs(args.apron_path)do apron[key(p)]=true end
for y=b.min_y,b.max_y do for x=b.min_x,b.max_x do local p={x=x,y=y};local area={{x-.49,y-.49},{x+.49,y+.49}};local blocked=false;local crossing=false
 for _,zone in ipairs(args.zones)do local z=zone.area;if x+.49>z[1][1]and x-.49<z[2][1]and y+.49>z[1][2]and y-.49<z[2][2]then blocked=true;break end end
 for index,z in ipairs(args.reservations)do
  if x+.49>z[1][1]and x-.49<z[2][1]and y+.49>z[1][2]and y-.49<z[2][2]
   and not(index==args.owner_index and apron[key(p)])then blocked=true;break end
 end
 if s.count_entities_filtered{area=area,type="resource"}>0 then blocked=true end
 for _,tile in pairs(s.find_tiles_filtered{area=area})do for layer in pairs(prototypes.entity.pipe.collision_mask.layers)do if tile.collides_with(layer)then blocked=true;break end end end
 for _,e in pairs(s.find_entities_filtered{area=area})do
  local rock=e.type=="simple-entity"and(e.name=="big-rock"or e.name=="huge-rock"or e.name=="big-sand-rock")
  local natural=e.force.name=="neutral"and e.minable and(e.type=="tree"or rock)
  if e~=a and e.type~="resource"and e.type~="corpse"and not natural and not e.prototype.collision_mask.colliding_with_tiles_only then crossing=true end
 end
 for _,e in pairs(s.find_entities_filtered{area={{x-1.49,y-1.49},{x+1.49,y+1.49}}})do
  if not(e.unit_number==args.source.unit_number and same(e.position,args.source.position))then
   for _ in pairs(e.prototype.fluidbox_prototypes or{})do blocked=true;break end
  end
 end
 if same(p,args.source.position)then blocked=false;crossing=false end
 if blocked then hard[#hard+1]=p elseif crossing then soft[#soft+1]=p end
end end;return {ok=true,hard=hard,soft=soft}
'''


def plan_typed_fluid_connection(game,builder,obs,source,destination,core,prior_groups=()):
    """Bounded ordinary pipe route. Source and all other fluid networks stay fixed."""
    if(source.get('item')!=destination.get('item')or source.get('kind')!='fluid'or source.get('name')!='pipe'
            or source.get('facing')not in VECTORS or type(source.get('unit_number'))is not int):
        return {'ok':False,'reason':'exact_matching_typed_fluid_source_required'}
    a,b=source['position'],destination['position'];bounds={'min_x':min(a['x'],b['x'])-12,'max_x':max(a['x'],b['x'])+12,
        'min_y':min(a['y'],b['y'])-12,'max_y':max(a['y'],b['y'])+12}
    if(bounds['max_x']-bounds['min_x']+1)*(bounds['max_y']-bounds['min_y']+1)>40000:return {'ok':False,'reason':'chemical_pipe_survey_budget_exceeded'}
    span=game.query('''local spans={};for _,b in pairs(prototypes.entity["pipe-to-ground"].fluidbox_prototypes)do
for _,c in pairs(b.pipe_connections)do if c.connection_type=="underground"then spans[#spans+1]=c.max_underground_distance end end end;return {ok=true,spans=spans}''')
    if not span.get('ok')or len(span.get('spans',[]))!=1 or type(span['spans'][0])is not int or span['spans'][0]<2:
        return {'ok':False,'reason':'native_pipe_span_unavailable'}
    board=json.loads((Path(game.cfg.runtime_dir)/'pcb-board.json').read_text(encoding='utf-8'))
    if board.get('world_id')!=obs['world_id']:return {'ok':False,'reason':'chemical_pipe_board_world_changed'}
    blocks=[p for p in board['blocks'].values()if p.get('reserved_area')]
    blocks += [{'reserved_area':r['area']}for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
    apron=source_owned_apron(builder,source,blocks)
    if not apron.get('ok'):return apron
    if apron['path']:
        clear=preflight(game,builder,obs,[entity('pipe',p['x'],p['y'])for p in apron['path'][1:]])
        if not clear.get('ok'):return {'ok':False,'reason':'chemical_source_apron_native_obstruction','preflight':clear}
    reservations=[p['reserved_area']for p in blocks]
    survey=game.query(CHEMICAL_PIPE_SURVEY_LUA.replace('PAYLOAD',_payload({'bounds':bounds,'source':source,
        'reservations':reservations,'owner_index':apron['owner_index'],'apron_path':apron['path'],
        'zones':mining_reservations(game,obs['world_id'])})))
    if not survey.get('ok'):return survey
    hard={(p['x'],p['y'])for p in survey.get('hard',[])};hard.update(builder._occupied_by_plan(core['entities']+core['output_access']))
    future_hard=unbuilt_external_keepouts(builder,obs,board['blocks'])
    if xy(source)in future_hard or xy(destination)in future_hard:
        return {'ok':False,'reason':'chemical_fluid_endpoint_conflicts_with_unbuilt_external_plan'}
    hard.update(future_hard)
    area=core['reserved_area']
    for ix in range(int(area[1][0]-area[0][0])):
        for iy in range(int(area[1][1]-area[0][1])):
            p=(area[0][0]+.5+ix,area[0][1]+.5+iy)
            if not(p[1]==b['y']and p[0]<=b['x']):hard.add(p)
    own=source['item']
    other=[e for name,es in core['fluid_groups'].items()if name!=own for e in es]+list(prior_groups)
    for e in other:
        p=xy(e);hard.add(p);hard.update((p[0]+dx,p[1]+dy)for dx,dy in VECTORS.values())
    hard -= {(a['x'],a['y']),(b['x'],b['y'])};soft={(p['x'],p['y'])for p in survey.get('soft',[])}
    maximum=span['spans'][0]
    def accepts(row):
        prefix=apron['path'];segments=row.get('segments',[])
        if prefix and(len(segments)<len(prefix)or any(xy(segments[i])!=(p['x'],p['y'])
                or(i<len(prefix)-1 and segments[i]['direction']!=source['facing'])for i,p in enumerate(prefix))):return False
        compiled=compile_pipe_crossings(row,soft,maximum)
        if not compiled.get('ok'):return False
        by={xy(e):e for e in compiled['entities']}
        return all(by.get((p['x'],p['y']),{}).get('name')=='pipe'for p in prefix)
    route=route_trunks(a,b,occupied=hard,bounds=bounds,start_direction=source['facing'],end_direction=destination['facing'],
        max_nodes=200000,max_turns=6,max_detour_ratio=1.35,candidate_validator=accepts)
    result=compile_pipe_crossings(route,soft,maximum)
    if not result.get('ok'):return result
    result['entities']=[e for e in result['entities']if xy(e)!=xy(source)]
    result['maximum_underground_span']=maximum
    first=(a['x']+VECTORS[source['facing']][0],a['y']+VECTORS[source['facing']][1])
    neighbor=next((e for e in result['entities']if xy(e)==first),None)
    if not neighbor:return {'ok':False,'reason':'chemical_source_fixed_first_neighbor_missing'}
    result['first_neighbor']=dict(neighbor,fluidbox_index=1)
    result['source_apron_exit']=apron
    return result


def chemical_plan_isolation(game,fluid_plans,core):
    """No prospective mixed network, incidental live peer or stolen buried pair."""
    groups={name:plan['nodes']for name,plan in fluid_plans.items()}
    for first,a in groups.items():
        for second,b in groups.items():
            if first>=second:continue
            for e in a:
                for other in b:
                    x,y=xy(e);qx,qy=xy(other)
                    if(x,y)==(qx,qy):return {'ok':False,'reason':'chemical_fluid_networks_overlap'}
                    for direction,(dx,dy)in VECTORS.items():
                        if((qx,qy)==(x+dx,y+dy)and(e['name']!='pipe-to-ground'or e['direction']==direction)
                                and(other['name']!='pipe-to-ground'or other['direction']==(direction+8)%16)):
                            return {'ok':False,'reason':'chemical_different_fluid_networks_touch'}
    mouths=[(name,e,plan['route']['maximum_underground_span'])for name,plan in fluid_plans.items()
            for e in plan['nodes']if e['name']=='pipe-to-ground']
    expected={}
    for name,plan in fluid_plans.items():
        for pair in plan['route']['underground_pairs']:
            first,last=tuple(pair['first']),tuple(pair['last']);expected[(name,first)]=last;expected[(name,last)]=first
    for name,e,span in mouths:
        x,y=xy(e);dx,dy=VECTORS[e['direction']];candidates=[]
        for other_name,other,_ in mouths:
            qx,qy=xy(other);distance=abs(qx-x)+abs(qy-y)
            if(other.get('direction')==(e['direction']+8)%16 and 0<distance<=span
                    and(qx,qy)==(x-dx*distance,y-dy*distance)):
                candidates.append((distance,other_name,(qx,qy)))
        nearest=min(candidates)if candidates else None
        if not nearest or nearest[1:]!=(name,expected.get((name,(x,y)))):
            return {'ok':False,'reason':'chemical_prospective_buried_pair_changed','position':e['position']}
    sources=[plan['source']['unit_number']for plan in fluid_plans.values()]
    nodes=merge_entities(*(groups.values()),[core['machine']])
    maximum=max(p['route']['maximum_underground_span']for p in fluid_plans.values())
    return game.query('''local args=helpers.json_to_table('''+_payload({'nodes':nodes,'sources':sources,'span':maximum})+''');local allowed={}
for _,unit in ipairs(args.sources)do allowed[unit]=true end
for _,q in ipairs(args.nodes)do if not(q.unit_number and allowed[q.unit_number])then
 local p=q.position;local radius=q.name=="chemical-plant"and 2.49 or 1.49
 for _,e in pairs(s.find_entities_filtered{area={{p.x-radius,p.y-radius},{p.x+radius,p.y+radius}}})do
  if not allowed[e.unit_number]then for _ in pairs(e.prototype.fluidbox_prototypes or{})do return {ok=false,reason="chemical_prospective_foreign_fluid_peer",position=p,foreign_unit=e.unit_number}end end
 end
 if q.name=="pipe-to-ground"then for _,e in pairs(s.find_entities_filtered{area={{p.x-args.span,p.y-args.span},{p.x+args.span,p.y+args.span}},name="pipe-to-ground"})do
  if(p.x==e.position.x or p.y==e.position.y)and math.abs(p.x-e.position.x)+math.abs(p.y-e.position.y)<=args.span then return {ok=false,reason="chemical_existing_buried_peer_conflict",position=p}end
 end end
end end;return {ok=true}''')


def _handoff(game,catalog,obs,consumer_id,fluid,source,neighbor,*,bind=False):
    from .fluid_handoffs import declare_handoff,bind_handoff
    try:return (bind_handoff if bind else declare_handoff)(game,catalog,obs,consumer_id,fluid,source,neighbor)
    except(ValueError,OSError,KeyError,TypeError)as exc:
        return {'ok':False,'reason':'chemical_fluid_handoff_context_invalid','detail':str(exc)}


class ChemicalProduction:
    def __init__(self,game,builder,catalog,perimeter,*,gas_source=None,coal_source=None,water_source=None):
        self.game,self.builder,self.catalog,self.perimeter=game,builder,catalog,perimeter
        self.overrides={'petroleum-gas':gas_source,'coal':coal_source,'water':water_source}
        self.path=Path(game.cfg.runtime_dir)/'chemical-production.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8'))if self.path.exists()else{}
        self.last_proof_failure=None

    def _save(self):_atomic_json(self.path,self.state)

    def _sources(self,obs,product):
        result={};snapshots={}
        wanted=['petroleum-gas']+(['coal']if product=='plastic-bar'else['water'])
        for item in wanted:
            fluid=item!='coal';path=self.path.parent/('fluid-sources.json'if fluid else'material-sources.json')
            registry=json.loads(path.read_text(encoding='utf-8'))if path.exists()else{}
            if not _identity(obs,registry,self.catalog):return {'ok':False,'reason':'chemical_source_registry_missing_or_changed','item':item}
            saved=self.state.get('products',{}).get(product,{}).get('sources',{}).get(item)
            if saved and self.overrides[item]is not None and self.overrides[item]!=saved:
                return {'ok':False,'reason':'chemical_bound_source_override_changed','item':item}
            source_key=SOURCE_KEYS[item]
            if item=='petroleum-gas':
                preferred='plastic-gas'if product=='plastic-bar'else'sulfur-gas'
                if preferred in registry.get('sources',{}):source_key=preferred
            source=saved or self.overrides[item]or registry.get('sources',{}).get(source_key)
            if not source:return {'ok':False,'reason':'explicit_usable_chemical_source_missing','item':item,'registry_key':source_key}
            if not isinstance(source,dict)or source not in registry.get('sources',{}).values():
                return {'ok':False,'reason':'chemical_source_not_exactly_published','item':item}
            if fluid:
                for owner,rows in registry.get('handoffs',{}).items():
                    used=rows.get(item)
                    if owner!='chemical:'+product and used and used.get('source',{}).get('unit_number')==source.get('unit_number'):
                        return {'ok':False,'reason':'chemical_fluid_source_requires_independent_authorized_branch',
                                'item':item,'existing_consumer':owner}
            if(source.get('kind')!=('fluid'if fluid else'item')or source.get('item')!=item or source.get('direction')!='output'
                    or source.get('world_id')!=obs['world_id']or type(source.get('unit_number'))is not int
                    or source['unit_number']<1 or source.get('name')!=('pipe'if fluid else'transport-belt')
                    or type(source.get('facing'))is not int or source.get('facing')not in VECTORS
                    or not isinstance(source.get('position'),dict)or set(source['position'])!={'x','y'}
                    or any(type(v)not in(int,float)or not math.isfinite(v)or v%1!=.5 for v in source['position'].values())
                    or(fluid and(type(source.get('fluidbox_index'))is not int or source.get('fluidbox_index')!=1))):
                return {'ok':False,'reason':'chemical_exact_typed_source_required','item':item}
            result[item]=deepcopy(source)
        native=self.game.query('''local sources=helpers.json_to_table('''+_payload(result)+''');local snapshots={}
for item,q in pairs(sources)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="chemical_source_identity_changed",item=item}end
 if q.kind=="fluid"then local fluids=e.get_fluid_contents();for name in pairs(fluids)do if name~=item then return {ok=false,reason="chemical_source_fluid_contaminated",item=item}end end
  if not e.has_fluid_segment(1)then return {ok=false,reason="chemical_source_pipeline_missing",item=item}end
  snapshots[item]={amount=fluids[item]or 0,extent=e.get_fluid_segment_extent_bounding_box(1),limit=prototypes.utility_constants.default_pipeline_extent}
 else if e.direction~=q.facing then return {ok=false,reason="chemical_coal_source_rotated"}end
  for i=1,2 do for _,stack in pairs(e.get_transport_line(i).get_contents())do if stack.name~=item then return {ok=false,reason="chemical_coal_source_contaminated"}end end end
 end
end;return {ok=true,snapshots=snapshots}''')
        return dict(native,sources=result)

    def prepare(self,obs,parameters):
        validate_chemical(parameters);product=parameters['product']
        if not obs.get('technologies',{}).get(PRODUCTS[product])or not all(obs.get('enabled_recipes',{}).get(r)for r in(product,'chemical-plant','fast-inserter')):
            return {'ok':False,'reason':'natural_chemical_technology_and_recipes_required','technology':PRODUCTS[product]}
        if self.state and not _identity(obs,self.state,self.catalog):return {'ok':False,'reason':'chemical_world_or_catalog_changed'}
        ready=self._sources(obs,product)
        if not ready.get('ok'):return ready
        existing=self.state.get('products',{}).get(product)
        if existing:
            return deepcopy(existing)if existing['sources']==ready['sources']else{'ok':False,'reason':'prepared_chemical_source_changed'}
        board=json.loads((self.path.parent/'pcb-board.json').read_text(encoding='utf-8'))
        if not _identity(obs,board,self.catalog):return {'ok':False,'reason':'chemical_board_identity_changed'}
        source=ready['sources']['petroleum-gas'];sx,sy=xy(source)
        anchors=[(sx+10+dx,sy+4+dy)for dx,dy in((0,0),(0,14),(22,0),(22,14),(0,-14),(22,-14))]
        reservations=[b['reserved_area']for b in board['blocks'].values()if b.get('reserved_area')]
        reservations += [r['area']for r in board.get('shared_main_bus',{}).get('reservations',[])if r.get('area')]
        future_hard=unbuilt_external_keepouts(self.builder,obs,board['blocks'])
        failures=[];started=time.monotonic();deadline=started+20;attempts=0;legal_sites=0
        for x,y in anchors:
            if time.monotonic()>deadline:break
            attempts+=1;candidate={'x':x,'y':y}
            core=chemical_core(self.catalog,product,x,y)
            if any(overlap(core['reserved_area'],r)for r in reservations):
                failures.append(candidate_failure(candidate,'core_reservation',{'reason':'candidate_reserved_area_conflict'}));continue
            a,b=core['reserved_area']
            if any(a[0]<px+.49 and px-.49<b[0]and a[1]<py+.49 and py-.49<b[1]for px,py in future_hard):
                failures.append(candidate_failure(candidate,'core_reservation',{'reason':'candidate_overlaps_unbuilt_external_plan'}));continue
            land=survey_slots(self.game,[[core['reserved_area']]])
            if not land.get('ok')or 1 not in land.get('legal',[]):
                failures.append(candidate_failure(candidate,'core_land',{'reason':land.get('reason')or'candidate_not_land_and_resource_clear'}));continue
            legal_sites+=1
            es=core['entities'][:];fluid_plans={};handoffs={};prior=[]
            for item,destination in core['fluid_ports'].items():
                route=plan_typed_fluid_connection(self.game,self.builder,obs,ready['sources'][item],destination,core,prior)
                if not route.get('ok'):
                    failures.append(candidate_failure(candidate,'fluid_route',route,item=item,source=ready['sources'][item],destination=destination));break
                nodes=merge_entities([ready['sources'][item]],route['entities'],core['fluid_groups'][item])
                extent_nodes=nodes+[entity('pipe',px,py)for px,py in self.builder._occupied_by_plan([core['machine']])]
                extent=pipeline_extent(ready['snapshots'][item]['extent'],extent_nodes,ready['snapshots'][item]['limit'])
                if not extent.get('ok'):
                    failures.append(candidate_failure(candidate,'pipeline_extent',extent,item=item,source=ready['sources'][item],destination=destination));break
                stub=next(e for e in core['fluid_groups'][item]if xy(e)==(x+core['fluidbox_bindings'][item]['offset']['x'],y-2))
                fluid_plans[item]={'source':ready['sources'][item],'nodes':nodes,'route':route,'extent':extent,
                    'edges':typed_fluid_edges(nodes,route['underground_pairs'],core['machine'],core['fluidbox_bindings'][item]['fluidbox_index'],stub)}
                handoffs[item]={'consumer_id':'chemical:'+product,'source':ready['sources'][item],'first_neighbor':route['first_neighbor']}
                prior += route['entities'];es=merge_entities(es,route['entities'])
            if len(fluid_plans)!=len(core['fluid_ports']):continue
            coal_route=None
            if core['coal_port']:
                coal_route=belt_connection(self.game,self.builder,obs,ready['sources']['coal'],core['coal_port'],es)
                if not coal_route.get('ok'):
                    failures.append(candidate_failure(candidate,'coal_route',coal_route,item='coal',source=ready['sources']['coal'],destination=core['coal_port']));continue
                es=merge_entities(es,coal_route['entities'])
            power=connect_power_groups(self.game,self.builder,obs,[core],es+core['output_access']+core['expansion_keepouts'])
            if not power.get('ok'):failures.append(candidate_failure(candidate,'power',power));continue
            es=merge_entities(es,power['entities']);check=preflight(self.game,self.builder,obs,es+core['output_access']+core['expansion_keepouts'])
            if not check.get('ok'):failures.append(candidate_failure(candidate,'whole_preflight',check));continue
            isolation=chemical_plan_isolation(self.game,fluid_plans,core)
            if not isolation.get('ok'):failures.append(candidate_failure(candidate,'fluid_isolation',isolation));continue
            scope='chemical:'+product;defense=self.perimeter.prepare_coverage(obs,scope,[core['reserved_area']])
            if not defense.get('ok'):
                failure=candidate_failure(candidate,'defense',defense)
                return {'ok':False,'reason':'chemical_defense_preflight_failed','defense':defense,'evidence':failure}
            starts=[h['first_neighbor']for h in handoffs.values()]
            es=merge_entities([{k:v for k,v in e.items()if k!='fluidbox_index'}for e in starts],es)
            plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],
                underground_pairs=coal_route['underground_pairs']if coal_route else[],
                clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'],defense_scope_id=scope,
                layout_policy='permanent-typed-chemistry-v1')
            return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'product':product,
                'phase':'build','core':core,'plan':plan,'sources':ready['sources'],'fluid_plans':fluid_plans,
                'fluid_handoffs':handoffs,'bindings':{},'defense_scope_id':scope,'defense_preview':defense,
                'blue_demand':blue_chemistry_demand(self.catalog),'rated':core['rated'],
                'planning_evidence':{'candidate':candidate,'attempted_candidates':attempts,'legal_sites':legal_sites,
                    'previous_failures':failures[-6:],'planning_seconds':round(time.monotonic()-started,3)}}
        now=time.monotonic();evidence={'product':product,'attempted_candidates':attempts,'legal_sites':legal_sites,
            'candidate_count':len(anchors),'planning_seconds':round(now-started,3),'planning_budget_seconds':20,
            'budget_exhausted':now>deadline,'candidate_failures':failures[-6:]}
        return {'ok':False,'reason':'no_permanent_chemical_site_with_complete_typed_inputs','failures':failures[-6:],'evidence':evidence}

    def _bind(self,obs,entry):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};changed=False
        for spec in entry['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));old=entry['bindings'].get(_key(spec))
            if old and(not live or live.get('unit_number')!=old):return {'status':'blocked','reason':'chemical_owned_asset_missing_or_replaced','entity':spec}
            if live:
                if type(live.get('unit_number'))is not int:return {'status':'blocked','reason':'chemical_native_unit_missing'}
                if old is None:entry['bindings'][_key(spec)]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def step(self,obs,parameters):
        validate_chemical(parameters);product=parameters['product']
        if self.state and not _identity(obs,self.state,self.catalog):return {'status':'blocked','reason':'chemical_world_or_catalog_changed'}
        entry=self.state.get('products',{}).get(product)
        if not entry:
            entry=self.prepare(obs,parameters)
            if not entry.get('ok'):
                evidence=entry.get('evidence')or{k:deepcopy(v)for k,v in entry.items()if k not in('ok','status')}
                return {'status':'blocked',**entry,'evidence':evidence}
            if not self.state:self.state={'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'products':{}}
            self.state['products'][product]=entry;self._save()
        sources=self._sources(obs,product)
        if not sources.get('ok')or sources.get('sources')!=entry['sources']:return {'status':'blocked','reason':'prepared_chemical_source_changed_or_unavailable','evidence':sources}
        result=self._bind(obs,entry)
        if result:return result
        path=self.path.parent/'pcb-board.json';board=json.loads(path.read_text(encoding='utf-8'));name='chemical_'+product
        if not _identity(obs,board,self.catalog):return {'status':'blocked','reason':'chemical_board_identity_changed'}
        old=board['blocks'].get(name)
        if old and old!=entry['plan']:return {'status':'blocked','reason':'chemical_registered_plan_changed'}
        if any(k!=name and overlap(entry['core']['reserved_area'],b['reserved_area'])for k,b in board['blocks'].items()if b.get('reserved_area')):
            return {'status':'blocked','reason':'chemical_reserved_area_conflict'}
        if not old:board['blocks'][name]=entry['plan'];_atomic_json(path,board)
        result=self.perimeter.ensure_coverage(obs,entry['defense_scope_id'],[entry['core']['reserved_area']])
        if result is not None:return result
        pipe_guard=surface_pipe_proof(self.game,obs,entry['plan'])
        if not pipe_guard.get('ok'):return {'status':'blocked','reason':'chemical_preserved_surface_pipe_changed','evidence':pipe_guard}
        for fluid,h in entry['fluid_handoffs'].items():
            planned={k:v for k,v in h['first_neighbor'].items()if k!='unit_number'}
            declared=_handoff(self.game,self.catalog,obs,h['consumer_id'],fluid,h['source'],planned)
            if not declared.get('ok'):return {'status':'blocked','reason':'chemical_fluid_handoff_not_authorized','evidence':declared}
            unit=entry['bindings'].get(_key(h['first_neighbor']))
            if unit:
                neighbor=dict(planned,unit_number=unit)
                bound=_handoff(self.game,self.catalog,obs,h['consumer_id'],fluid,h['source'],neighbor,bind=True)
                if not bound.get('ok'):return {'status':'blocked','reason':'chemical_fluid_handoff_not_bound','evidence':bound}
                if h['first_neighbor']!=neighbor:h['first_neighbor']=neighbor;self._save()
        if not entry.get('construction_authorized'):
            check=preflight(self.game,self.builder,obs,entry['plan']['entities']+entry['core']['output_access']+entry['core']['expansion_keepouts'])
            if not check.get('ok'):return {'status':'blocked','reason':'chemical_post_defense_preflight_failed','preflight':check}
            entry['construction_authorized']=True;self._save()
        result=self.builder.ensure_plan(obs,entry['plan'])
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs,parameters)
        if not proof:return {'status':'waiting','reason':'waiting for exact powered chemical recipe, native typed fluid peers and real solid output','evidence':self.last_proof_failure}
        entry.update(phase='verified',proof=proof);self._save()
        return {'status':'succeeded','reason':'permanent normally supplied chemical product commissioned','evidence':proof}

    def proof(self,obs,parameters):
        validate_chemical(parameters);self.last_proof_failure=None
        if not self.state or not _identity(obs,self.state,self.catalog):return None
        entry=self.state['products'].get(parameters['product'])
        if not entry:return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for spec in entry['plan']['entities']:
            live=actual.get((spec['name'],*xy(spec)));unit=entry['bindings'].get(_key(spec))
            if(not unit or not live or live.get('unit_number')!=unit
                    or(spec['name']not in('small-electric-pole','pipe')and live.get('direction',0)!=spec.get('direction',0))):return None
        if not underground_proof(obs,entry['plan']):return None
        pipe_guard=surface_pipe_proof(self.game,obs,entry['plan'])
        if not pipe_guard.get('ok'):self.last_proof_failure=pipe_guard;return None
        defense=self.perimeter.coverage_proof(obs,entry['defense_scope_id'])
        if not defense:return None
        sources=self._sources(obs,entry['product'])
        if not sources.get('ok')or sources.get('sources')!=entry['sources']:return None
        registry=json.loads((self.path.parent/'fluid-sources.json').read_text(encoding='utf-8'))
        for fluid,h in entry['fluid_handoffs'].items():
            row=registry.get('handoffs',{}).get(h['consumer_id'],{}).get(fluid,{})
            neighbor=dict(h['first_neighbor'],unit_number=entry['bindings'].get(_key(h['first_neighbor'])))
            if(not _identity(obs,row,self.catalog)or row.get('phase')!='bound'or row.get('source')!=h['source']or row.get('first_neighbor')!=neighbor):return None
        bindings=dict(entry['bindings']);groups={};edges=[]
        for fluid,p in entry['fluid_plans'].items():
            bindings[_key(p['source'])]=p['source']['unit_number']
            groups[fluid]=[dict(e,unit_number=bindings[_key(e)])for e in p['nodes']]
            edges += p['edges']
        core=entry['core'];machine=dict(core['machine'],unit_number=bindings[_key(core['machine'])])
        arms=[core['output_arm']]+([core['input_arm']]if core['input_arm']else[])
        args={'product':entry['product'],'groups':groups,'edges':edges,'bindings':bindings,'machine':machine,
            'prototype_box_count':len(self.catalog.entities['chemical-plant']['fluidbox_prototypes']),
            'source_units':[source['unit_number']for source in entry['sources'].values()if source['kind']=='fluid'],
            'physical_boxes':{str(p['fluidbox_index']):name for name,p in core['fluidbox_bindings'].items()},
            'arms':[dict(e,unit_number=bindings[_key(e)])for e in arms],
            'output':dict(core['output_port'],name='transport-belt',unit_number=bindings[_key(dict(core['output_port'],name='transport-belt'))])}
        native=self.game.query(CHEMICAL_PROOF_LUA.replace('PAYLOAD',_payload(args)))
        if not native.get('ok'):self.last_proof_failure=native;return None
        return {'tick':native['tick'],'product':entry['product'],'source':dict(core['output_port'],name='transport-belt',
            unit_number=native['output_unit'],world_id=obs['world_id'],defense_scope_id=entry['defense_scope_id']),'native':native,'rated':entry['rated'],
            'blue_demand':entry['blue_demand'],'defense_coverage':defense,
            'scope':'actual normally fed chemistry and exact native fluid peers; nameplate is not sustained-throughput proof'}


CHEMICAL_PROOF_LUA=r'''
local args=helpers.json_to_table(PAYLOAD);local live={};local expected={};local fluid_amounts={};local limit=prototypes.utility_constants.default_pipeline_extent
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local function id(q,index)return key(q).."@"..index end
local function exact(q)local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e end
for fluid,nodes in pairs(args.groups)do local amount=0;for _,q in ipairs(nodes)do local e=exact(q)
 if not e or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return {ok=false,reason="chemical_fluid_unit_or_direction_changed"}end
 for name,count in pairs(e.get_fluid_contents())do if name~=fluid then return {ok=false,reason="chemical_typed_fluid_contaminated"}end;amount=amount+count end
 if not e.has_fluid_segment(1)then return {ok=false,reason="chemical_pipeline_segment_missing"}end
 local extent=e.get_fluid_segment_extent_bounding_box(1)
 if extent.right_bottom.x-extent.left_top.x>limit or extent.right_bottom.y-extent.left_top.y>limit then return {ok=false,reason="chemical_pipeline_extent_exceeded"}end
 live[key(q)]=e
end;if amount<=0 then return {ok=false,reason="chemical_actual_input_fluid_required",fluid=fluid}end;fluid_amounts[fluid]=amount end
local machine=exact(args.machine);if not machine or machine.direction~=0 or not machine.is_connected_to_electric_network()or machine.energy<=0 then return {ok=false,reason="chemical_machine_missing_or_unpowered"}end
local recipe=machine.get_recipe();if not recipe or recipe.name~=args.product or machine.products_finished<=0 then return {ok=false,reason="native_chemical_product_required"}end
local runtime_indices={};local physical_to_runtime={};local active_boxes={}
for index=1,args.prototype_box_count do local good,prototype=pcall(function()return machine.get_fluid_box_prototype(index)end)
 if good and prototype then local allowed=nil;local indices={};local prototypes=prototype.index and{prototype}or prototype
  for _,p in pairs(prototypes)do
   if type(p.index)~="number"or p.index<1 or p.index>args.prototype_box_count or p.index%1~=0 or physical_to_runtime[tostring(p.index)]then return {ok=false,reason="chemical_active_fluidbox_prototype_invalid"}end
   physical_to_runtime[tostring(p.index)]=index;indices[#indices+1]=p.index
   local fluid=args.physical_boxes[tostring(p.index)]
   if fluid then if allowed and allowed~=fluid then return {ok=false,reason="chemical_different_fluids_merged_into_storage"}end;allowed=fluid end
  end
  if #indices==0 then return {ok=false,reason="chemical_empty_active_fluidbox_prototype"}end
  local fluid=machine.get_fluid(index)
  if fluid and fluid.amount>0 and fluid.name~=allowed then return {ok=false,reason="chemical_wrong_physical_fluidbox",index=index}end
  runtime_indices[#runtime_indices+1]=index;active_boxes[#active_boxes+1]={runtime_index=index,prototype_indices=indices,fluid=allowed}
 end
end
for physical in pairs(args.physical_boxes)do if not physical_to_runtime[physical]then return {ok=false,reason="chemical_required_physical_fluidbox_inactive",prototype_index=physical}end end
live[key(args.machine)]=machine
local function actual_index(q,index)if key(q)==key(args.machine)then return physical_to_runtime[tostring(index)]end;return index end
for _,edge in ipairs(args.edges)do local a=live[key(edge.a)];local b=live[key(edge.b)];local ai=actual_index(edge.a,edge.a.fluidbox_index);local bi=actual_index(edge.b,edge.b.fluidbox_index)
 if not a or not b then return {ok=false,reason="chemical_edge_entity_missing"}end
 local forward=false;local reverse=false
 for _,peer in pairs(a.get_fluid_box_neighbours(ai)or{})do if peer.entity==b and peer.index==bi then forward=true end end
 for _,peer in pairs(b.get_fluid_box_neighbours(bi)or{})do if peer.entity==a and peer.index==ai then reverse=true end end
 if not forward or not reverse then return {ok=false,reason="chemical_reciprocal_typed_peer_missing"}end
 expected[id(edge.a,ai)]=expected[id(edge.a,ai)]or{};expected[id(edge.a,ai)][b.unit_number..":"..bi]=true
 expected[id(edge.b,bi)]=expected[id(edge.b,bi)]or{};expected[id(edge.b,bi)][a.unit_number..":"..ai]=true
end
local upstream={};for _,unit in ipairs(args.source_units)do upstream[unit]=true end
for _,nodes in pairs(args.groups)do for _,q in ipairs(nodes)do if not upstream[q.unit_number]then
 local e=live[key(q)];for _,peer in pairs(e.get_fluid_box_neighbours(1)or{})do
  if not(expected[id(q,1)]or{})[peer.entity.unit_number..":"..peer.index]then return {ok=false,reason="chemical_unexpected_foreign_fluid_peer"}end
 end
end end end
for _,index in ipairs(runtime_indices)do for _,peer in pairs(machine.get_fluid_box_neighbours(index)or{})do
 if not(expected[id(args.machine,index)]or{})[peer.entity.unit_number..":"..peer.index]then return {ok=false,reason="chemical_unexpected_machine_fluid_peer"}end
end end
for _,q in ipairs(args.arms)do local e=exact(q)
 if not e or e.direction~=q.direction or not e.is_connected_to_electric_network()or e.energy<=0 then return {ok=false,reason="chemical_native_inserter_unpowered_or_changed"}end
 if q.direction==12 then
  local feed=target({x=math.floor(e.pickup_position.x)+.5,y=math.floor(e.pickup_position.y)+.5},"transport-belt")
  if not feed or feed.direction~=4 or math.abs(e.drop_position.x-machine.position.x)>1.5 or math.abs(e.drop_position.y-machine.position.y)>1.5 then return {ok=false,reason="chemical_native_coal_feed_missing"}end
  for i=1,2 do for _,item in pairs(feed.get_transport_line(i).get_contents())do if item.name~="coal"then return {ok=false,reason="chemical_native_coal_feed_contaminated"}end end end
 else
  local belt=target({x=math.floor(e.drop_position.x)+.5,y=math.floor(e.drop_position.y)+.5},"transport-belt")
  if not belt or belt.direction~=4 or math.abs(e.pickup_position.x-machine.position.x)>1.5 or math.abs(e.pickup_position.y-machine.position.y)>1.5 then return {ok=false,reason="chemical_native_solid_output_feed_missing"}end
 end
end
local outlet=exact(args.output);if not outlet or outlet.direction~=4 then return {ok=false,reason="chemical_fixed_output_changed"}end
local amount=0;for index=1,2 do for _,item in pairs(outlet.get_transport_line(index).get_contents())do
 if item.name~=args.product then return {ok=false,reason="chemical_solid_output_contaminated"}end;amount=amount+item.count
end end
if amount<=0 then return {ok=false,reason="chemical_actual_solid_output_required"}end
return {ok=true,tick=game.tick,output_unit=outlet.unit_number,count=amount,machine_unit=machine.unit_number,products_finished=machine.products_finished,reciprocal_edges=#args.edges,fluid_amounts=fluid_amounts,active_machine_fluidboxes=active_boxes}
'''
