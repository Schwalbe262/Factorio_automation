"""Two independent furnace rows feed opposite lanes of one shared output."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity,pole_chain
from .smelting import port,merge_entities,bank_capacities,FURNACE_NAMES
from .transport import xy,VECTORS,plan_connection,underground_proof
from .planning import preflight
from .power_grid import plan_connection as power_connection


def key(e):return e['name'],*xy(e),e.get('direction',0),e.get('belt_to_ground_type')


def paired_bank(x,y,count,resource,*,furnace_name='stone-furnace'):
    """Split the mixed INPUT before any consumer, never split final output."""
    product={'iron-ore':'iron-plate','copper-ore':'copper-plate'}[resource]
    es=[];rows={'right':[],'left':[]};outputs=[]
    for side,dy in (('right',0),('left',5)):
        for i in range(count):
            cx=x+4*i;furnace=entity(furnace_name,cx,y+dy);rows[side].append(furnace);es.append(furnace)
            if side=='right':input_y,output_y,pickup=y-1.5,y+1.5,0
            else:input_y,output_y,pickup=y+6.5,y+3.5,8
            es += [entity('inserter',cx+.5,input_y,pickup),entity('inserter',cx+.5,output_y,pickup),
                   entity('small-electric-pole',cx+1.5,y+dy+.5 if side=='right' else y+dy-.5)]
            outputs.append({'inserter':entity('inserter',cx+.5,output_y,pickup),'lane':side})
    es += [entity('transport-belt',x+dx+.5,y+2.5,4) for dx in range(23)]
    es += [entity('transport-belt',x+dx+.5,y-2.5,4) for dx in range(-6,-3)]
    es += [entity('transport-belt',x+dx+.5,y-2.5,4) for dx in range(-2,21)]
    es += [entity('transport-belt',x+dx+.5,y+7.5,4) for dx in range(-2,21)]
    es += [entity('transport-belt',x-1.5,y+dy,8) for dy in (-1.5,-.5,.5,1.5,2.5,3.5,4.5,5.5,6.5)]
    es += [dict(entity('splitter',x-2.5,y-2,4),_width=2,_height=1)]
    # Ore enters from north, coal from south, then splitter preserves both lanes
    # while giving each production row half of the available input flow.
    es += [entity('transport-belt',x-3.5,y-4.5,8),entity('transport-belt',x-3.5,y-3.5,8),
           entity('transport-belt',x-4.5,y-.5,0),entity('transport-belt',x-4.5,y-1.5,0)]
    ports=[port(resource,x-3.5,y-4.5,8),port('coal',x-4.5,y-.5,0),
           dict(port(product,x+22.5,y+2.5,4,'output'),lane_items={'left':product,'right':product})]
    return _plan(es,ports,resource=resource,product=product,count=2*count,per_side_count=count,capacity_slots=6,
                 producer_rows=rows,output_inserters=outputs,template='paired_furnaces_shared_output',
                 layout_policy='pcb-independent-producers-per-lane-v1')


def trace_owned(obs,old,start,end):
    entities={xy(e):e for e in obs['entities'] if e['name'] in ('transport-belt','underground-belt')}
    owned={key(e) for e in old['entities']};p=(start['x'],start['y']);target=(end['x'],end['y']);seen=set();result=[]
    for _ in range(256):
        e=entities.get(p)
        if not e or key(e) not in owned or p in seen:raise ValueError('owned supply route changed before migration')
        seen.add(p);result.append(e)
        if p==target:return result
        if e['name']=='underground-belt' and e.get('belt_to_ground_type')=='input':
            n=e.get('underground_neighbour',{})
            if not e.get('underground_pair_verified'):raise ValueError('old underground route is not paired')
            p=(n['position']['x'],n['position']['y'])
        else:
            v=VECTORS[e['direction']];p=(p[0]+v[0],p[1]+v[1])
    raise ValueError('owned route trace exceeded budget')


class PairedSmelting:
    def __init__(self,game,builder,catalog,smelting):
        self.game,self.builder,self.catalog,self.smelting=game,builder,catalog,smelting
        self.path=Path(game.cfg.runtime_dir)/'paired-smelting.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def owns_furnace(self,e,obs):
        return self.state.get('world_id')==obs['world_id'] and e['name'] in FURNACE_NAMES and any(
            w['name'] in FURNACE_NAMES and w['position']==e['position'] for w in self.state.get('plan',{}).get('entities',[]))

    def prepare(self,obs):
        old=self.smelting.state;root=self.path.parent
        lane=json.loads((root/'smelter-lanes.json').read_text(encoding='utf-8'))
        if old.get('world_id')!=obs['world_id'] or lane.get('world_id')!=obs['world_id']:
            return {'ok':False,'reason':'paired_migration_source_world_changed'}
        banks=[paired_bank(-40,-32,5,'iron-ore'),paired_bank(-40,-16,3,'copper-ore')]
        anchors=[port('iron-ore',-40.5,-47.5,12,'output'),port('copper-ore',13.5,-26.5,8,'output'),
                 port('coal',-44.5,-28.5,4,'output'),port('coal',-44.5,-27.5,4,'output')]
        old_ports=[next(p for b in old['banks'] for p in b['ports'] if p['item']==resource)
                   for resource in ('iron-ore','copper-ore')]
        old_ports += [next(p for p in b['ports'] if p['item']=='coal') for b in old['banks']]
        try:traces=[trace_owned(obs,old['plan'],a['position'],b['position']) for a,b in zip(anchors,old_ports)]
        except ValueError as error:return {'ok':False,'reason':str(error)}
        excluded={key(e) for b in old['banks'] for e in b['entities']}
        excluded.update(key(e) for trace in traces for e in trace[1:])
        owned_specs=old['plan']['entities']+lane['plan']['entities']
        base=[e for e in old['plan']['entities'] if key(e) not in excluded]
        es=merge_entities(base,*(b['entities'] for b in banks))
        lower=banks[0]['producer_rows']['left'][0]['position'];upper=banks[1]['producer_rows']['right'][0]['position']
        bridge=pole_chain({'x':lower['x']+1.5,'y':lower['y']-.5},
                          {'x':upper['x']+1.5,'y':upper['y']+.5})
        es=merge_entities(es,bridge)
        new_keys={key(e) for e in es}
        ignored={e['unit_number'] for e in obs['entities'] if any(key(e)==key(w) for w in owned_specs) and key(e) not in new_keys}
        virtual={**obs,'entities':[e for e in obs['entities'] if e.get('unit_number') not in ignored]}
        adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda specs:preflight(self.game,self.builder,obs,specs,ignored_units=ignored))
        check=preflight(self.game,self.builder,obs,es,ignored_units=ignored)
        if not check.get('ok'):return check
        obstacles={(e['name'],*xy(e)):e for e in check['clear_obstacles']};routes=[]
        destinations=[banks[0]['ports'][0],banks[1]['ports'][0],banks[0]['ports'][1],banks[1]['ports'][1]]
        connections=list(zip(anchors,destinations))
        supply_ports=[port('iron-plate',-9.5,-29.5,4,'output'),port('copper-plate',-9.5,-17.5,4,'output')]
        for bank,supply in zip(banks,supply_ports):connections.append((bank['ports'][-1],dict(supply,direction='input')))
        for source,destination in connections:
            route=plan_connection(self.game,adapter,virtual,source,destination,es,ignored_units=ignored)
            if not route.get('ok'):return {'ok':False,'reason':route.get('reason'),'connection':{'source':source,'destination':destination}}
            es=merge_entities(es,route['entities']);routes.append(route)
            obstacles.update({(e['name'],*xy(e)):e for e in route['clear_obstacles']})
        final=preflight(self.game,self.builder,obs,es,ignored_units=ignored)
        if not final.get('ok'):return final
        obstacles.update({(e['name'],*xy(e)):e for e in final['clear_obstacles']})
        keys={key(e) for e in es}
        remove=[e for e in obs['entities'] if any(key(e)==key(w) for w in owned_specs) and key(e) not in keys]
        retained_pairs=[p for p in old['plan'].get('underground_pairs',[]) if all(
            any(e['name']=='underground-belt' and xy(e)==tuple(p[side]) for e in es) for side in ('input','output'))]
        pairs=retained_pairs+[p for r in routes for p in r['underground_pairs']]
        plan=_plan(es,supply_ports,reserved_area=old['plan']['reserved_area'],underground_pairs=pairs,
                   clear_obstacles=list(obstacles.values()),clearance_world_id=obs['world_id'],layout_policy='pcb-paired-smelting-v1')
        return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                'plan':plan,'banks':banks,'remove':remove,'phase':'recover','old_smelting':deepcopy(old),
                'old_lane_state':lane,'route_metrics':[r['route_metrics'] for r in routes],
                'baseline_products':{str(e['unit_number']):e.get('products_finished',0) for e in obs['entities'] if e['name'] in FURNACE_NAMES}}

    def step(self,obs):
        if not self.state:
            prepared=self.prepare(obs)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;_atomic_json(self.path,self.state)
        if self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'paired_migration_world_or_catalog_changed'}
        if self.state['phase']=='verified':
            proof=self.proof(obs)
            return {'status':'succeeded' if proof else 'waiting','reason':'reverify current paired producer rows','evidence':proof or {}}
        if self.state['phase']=='recover':
            for wanted in self.state['remove']:
                current=next((e for e in obs['entities'] if e['name']==wanted['name'] and e['position']==wanted['position']),None)
                if current:
                    if current['unit_number']!=wanted['unit_number']:return {'status':'blocked','reason':'paired_recovery_identity_changed'}
                    return {'type':'mine','name':current['name'],'position':current['position'],'count':1,
                            'expected_entity_unit':current['unit_number'],'expected_entity_world_id':obs['world_id'],
                            'reason':'normally recover obsolete one-sided feed/output hardware before paired production'}
            self.state['phase']='build';_atomic_json(self.path,self.state)
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for bank in self.state['banks']:
            unpowered=[e for e in bank['entities'] if e['name']=='inserter'
                       and not actual[(e['name'],*xy(e))].get('energy',0)>0]
            if unpowered:
                pole=min((e for e in bank['entities'] if e['name']=='small-electric-pole'),
                         key=lambda p:abs(p['position']['x']-unpowered[0]['position']['x'])+abs(p['position']['y']-unpowered[0]['position']['y']))
                power=power_connection(self.game,self.builder,obs,pole['position'],
                    [e for e in self.state['plan']['entities'] if e['name']!='small-electric-pole'])
                if not power.get('ok'):return {'status':'blocked','reason':'paired_electric_island_needs_connection','detail':power}
                if not power['entities']:return {'status':'waiting','reason':'waiting for the actual powered pole network'}
                self.state['plan']['entities']=merge_entities(self.state['plan']['entities'],power['entities'])
                extra={k:v for k,v in self.state['plan'].items() if k not in ('ok','reason','entities','ports','required_items')}
                self.state['plan']=_plan(self.state['plan']['entities'],self.state['plan']['ports'],**extra)
                self.state.setdefault('power_repairs',[]).append({'tick':obs['tick'],'wire_hops':power['wire_hops']})
                _atomic_json(self.path,self.state)
                return {'status':'waiting','reason':'reserved normal paid pole bridge for disconnected production row'}
        # A pre-existing full output buffer must not masquerade as fresh
        # production. Recover at most two real plates per old furnace so the
        # independent engine can complete new cycles under a bounded load.
        for bank in self.state['banks']:
            for wanted in (e for e in bank['entities'] if e['name'] in FURNACE_NAMES):
                machine=actual[(wanted['name'],*xy(wanted))];unit=str(machine['unit_number'])
                if (unit in self.state['baseline_products'] and machine.get('products_finished',0)==self.state['baseline_products'][unit]
                        and machine.get('output_inventory',{}).get(bank['product'],0)>0
                        and unit not in self.state.get('commissioning_withdrawals',{})):
                    count=min(2,machine['output_inventory'][bank['product']])
                    self.state.setdefault('commissioning_withdrawals',{})[unit]=count;_atomic_json(self.path,self.state)
                    return {'type':'take','name':machine['name'],'position':machine['position'],'item':bank['product'],'count':count,
                            'reason':'normally recover bounded existing output to verify fresh independent production'}
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'waiting for independent upper/lower furnace production onto both shared-belt lanes'}
        self.publish(obs)
        return {'status':'succeeded','reason':'independent furnace rows directly feed opposite lanes of the shared output','evidence':proof}

    def publish(self,obs):
        self.smelting.state.update(plan=self.state['plan'],banks=self.state['banks'],paired=True,
            nominal_smelting_capacity_per_minute=bank_capacities(self.catalog,self.state['banks']))
        _atomic_json(self.smelting.path,self.smelting.state)
        board_path=self.path.parent/'pcb-board.json';board=json.loads(board_path.read_text(encoding='utf-8'))
        obsolete=[name for name in board['blocks'] if name.endswith('_lane_distribution')]
        for name in obsolete:board['blocks'].pop(name)
        board['connections']=[c for c in board.get('connections',[]) if c.get('owner') not in obsolete]
        board['blocks']['smelting_district']=self.state['plan'];_atomic_json(board_path,board)
        modules=[]
        for bank,p in zip(self.state['banks'],self.state['plan']['ports']):
            modules.append({'source_port':bank['ports'][-1],'ports':[dict(bank['ports'][-1],direction='input'),p],
                            'entities':[],'mode':'independent producer rows; no output splitter'})
        supply={'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,'modules':modules,
                'mode':'direct paired furnace production','plan':{'entities':[]}}
        _atomic_json(self.path.parent/'smelter-lanes.json',supply)
        self.state['phase']='verified';_atomic_json(self.path,self.state)

    def proof(self,obs):
        if not self.state or self.state['world_id']!=obs['world_id']:return None
        current=self.smelting.state if self.state['phase']=='verified' else self.state
        if current.get('world_id')!=obs['world_id'] or not underground_proof(obs,current['plan']):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        for wanted in current['plan']['entities']:
            e=actual.get((wanted['name'],*xy(wanted)))
            if not e or e['direction']!=wanted.get('direction',0):return None
            if e['name']=='inserter' and (not e.get('electric_network_connected') or e.get('energy',0)<=0):return None
        rows=[]
        for bank in current['banks']:
            row={'product':bank['product'],'production_by_lane':{}}
            for side,furnaces in bank['producer_rows'].items():
                machines=[actual[(e['name'],*xy(e))] for e in furnaces]
                if any(e.get('products_finished',0)<=self.state['baseline_products'].get(str(e['unit_number']),0) for e in machines):return None
                row['production_by_lane'][side]={'furnace_units':[e['unit_number'] for e in machines],
                    'products_finished':[e['products_finished'] for e in machines],
                    'catalog_capacity_per_minute':bank_capacities(self.catalog,[dict(bank,entities=furnaces)])[bank['resource']]}
            rows.append(row)
        payload=json.dumps(json.dumps([b['ports'][-1] for b in current['banks']],separators=(',',':')))
        result=self.game.query('''
local ports=helpers.json_to_table('''+payload+''');local rows={}
for _,p in ipairs(ports) do
 local belt=target(p.position,"transport-belt");if not belt then return {ok=false} end
 local lanes={};for _,index in ipairs({defines.transport_line.left_line,defines.transport_line.right_line}) do
  local counts={};for _,item in pairs(belt.get_transport_line(index).get_contents()) do counts[item.name]=(counts[item.name] or 0)+item.count end
  lanes[#lanes+1]=counts
 end
 rows[#rows+1]={item=p.item,position=p.position,left=lanes[1],right=lanes[2]}
end
return {ok=true,tick=game.tick,outputs=rows}
''')
        if not result.get('ok'):return None
        for row in result['outputs']:
            if any(not isinstance(row[side],dict) or row[side].get(row['item'],0)<1 or set(row[side])-{row['item']} for side in ('left','right')):return None
        return {'tick':result['tick'],'producer_rows':rows,'shared_output_lanes':result['outputs'],
                'power_repairs':self.state.get('power_repairs',[]),'commissioning_withdrawals':self.state.get('commissioning_withdrawals',{}),
                'scope':'independent upper/lower production directly feeds opposite shared-belt lanes; actual total rate remains limited by ore, fuel, power and downstream consumption'}
