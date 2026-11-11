"""Automatic stone-brick production using registered stone and coal belts."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .manufacturing import connect_power_groups
from .planning import overlap, preflight, survey_slots
from .smelting import merge_entities
from .steel_production import audit_steel_core, paired_smelting_core, smelting_rates
from .transport import VECTORS, plan_connection, underground_proof, xy


def brick_rates(catalog, furnace_count=2):
    rates=smelting_rates(catalog,'stone','stone-brick',furnace_count)
    return {**rates,'brick_per_minute':rates['product_per_minute'],'stone_per_minute':rates['raw_per_minute']}


def brick_core(catalog, x=82, y=-18, *, per_side_count=1, reserved_pairs=3):
    plan=paired_smelting_core(catalog,x,y,raw_item='stone',product_item='stone-brick',
        input_inserter='fast-inserter',per_side_count=per_side_count,reserved_pairs=reserved_pairs)
    plan.update(template='paired_brick_fixed_ports_v1',layout_policy='pcb-paired-brick-v1',
                rated=brick_rates(catalog,2*per_side_count))
    return plan


def audit_brick_core(plan, catalog):
    audit=audit_steel_core(plan,catalog,raw_item='stone',product_item='stone-brick',input_inserter='fast-inserter')
    errors=set(audit['errors']);rates=brick_rates(catalog,audit['furnaces'])
    if 'fast-inserter' not in catalog.entities or 'inserter' not in catalog.entities:
        errors.add('brick_catalog_native_inserters_missing')
    if ([(p['item'],p['direction']) for p in plan['ports']] !=
            [('stone','input'),('coal','input'),('stone-brick','output')]
            or plan.get('input_lane_items')!={'left':'stone','right':'coal'}
            or plan.get('recipe_name')!='stone-brick'):
        errors.add('brick_typed_ports_or_recipe_changed')
    inputs=(rates['stone_per_minute']+rates['coal_per_minute_at_max_catalog_usage'])/audit['furnaces']
    outputs=rates['brick_per_minute']/audit['furnaces']
    # Match the existing compact factory's conservative short-arm budgets.
    # These budgets are planning limits, not catalog or measured throughput.
    if inputs>120:errors.add('brick_fast_input_inserter_budget_exceeded')
    if outputs>30:errors.add('brick_output_inserter_budget_exceeded')
    return {**audit,'ok':not errors,'errors':sorted(errors),'rated':rates,
            'inserter_budget':{'input_items_per_minute_each':inputs,'input_limit':120,
                               'output_items_per_minute_each':outputs,'output_limit':30,
                               'scope':'conservative project planning budgets; native commissioning still required'}}


class BrickProduction:
    def __init__(self, game, builder, catalog, *, stone_source=None, coal_source=None):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.stone_source,self.coal_source=stone_source,coal_source
        self.path=Path(game.cfg.runtime_dir)/'brick-production.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _board(self, obs):
        path=self.path.parent/'pcb-board.json'
        if not path.exists():return None
        board=json.loads(path.read_text(encoding='utf-8'))
        if (board.get('world_id')!=obs['world_id'] or board.get('catalog_fingerprint')!=self.catalog.fingerprint
                or not isinstance(board.get('blocks'),dict)):return None
        return board

    def _sources(self, obs, stone, coal):
        if stone is None or coal is None:
            path=self.path.parent/'material-sources.json'
            if not path.exists():return {'ok':False,'reason':'brick_material_sources_not_commissioned'}
            registry=json.loads(path.read_text(encoding='utf-8'))
            if (registry.get('world_id')!=obs['world_id'] or registry.get('catalog_fingerprint')!=self.catalog.fingerprint):
                return {'ok':False,'reason':'brick_material_registry_identity_changed'}
            stone=stone if stone is not None else registry.get('sources',{}).get('stone')
            coal=coal if coal is not None else registry.get('sources',{}).get('brick-coal')
        sources=[stone,coal]
        for spec,item in zip(sources,('stone','coal')):
            if (not isinstance(spec,dict) or spec.get('item')!=item or spec.get('direction')!='output'
                    or spec.get('world_id')!=obs['world_id'] or not spec.get('material_supply_id')
                    or type(spec.get('unit_number')) is not int or spec['unit_number']<1
                    or type(spec.get('facing')) is not int or spec['facing'] not in VECTORS
                    or not isinstance(spec.get('position'),dict) or set(spec['position'])!={'x','y'}
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in spec['position'].values())
                    or spec.get('mining_resource') not in (None,item)):
                return {'ok':False,'reason':'brick_exact_registered_typed_sources_required'}
        return {'ok':True,'sources':deepcopy(sources)}

    def _inspect_sources(self, sources):
        payload=json.dumps(json.dumps(sources,separators=(',',':')))
        return self.game.query('''local specs=helpers.json_to_table('''+payload+''')
for _,spec in ipairs(specs) do local e=target(spec.position,"transport-belt")
 if not e or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or e.direction~=spec.facing
  or e.position.x~=spec.position.x or e.position.y~=spec.position.y then
  return {ok=false,reason="brick_registered_source_changed"} end
 for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do
  if v.name~=spec.item then return {ok=false,reason="brick_registered_source_contaminated"} end
 end end
end
return {ok=true,tick=game.tick}''')

    def prepare(self, obs, *, stone_source=None, coal_source=None, anchors=((82,-18),(98,-18),(82,-2))):
        """Read-only whole-layout preparation; no actor feed or source surgery."""
        if not obs.get('enabled_recipes',{}).get('stone-brick'):
            return {'ok':False,'reason':'native_stone_brick_recipe_required'}
        if not obs.get('enabled_recipes',{}).get('fast-inserter'):
            return {'ok':False,'reason':'native_fast_input_inserters_required'}
        sources=self._sources(obs,stone_source if stone_source is not None else self.stone_source,
                                   coal_source if coal_source is not None else self.coal_source)
        if not sources['ok']:return sources
        sources=sources['sources'];native=self._inspect_sources(sources)
        if not native.get('ok'):return native
        board=self._board(obs)
        if board is None:return {'ok':False,'reason':'brick_board_identity_changed'}
        try:
            candidates=[brick_core(self.catalog,x,y) for x,y in anchors]
            audits=[audit_brick_core(core,self.catalog) for core in candidates]
        except (ValueError,KeyError,TypeError) as exc:
            return {'ok':False,'reason':'brick_catalog_or_geometry_unsupported','detail':str(exc)}
        if any(not audit['ok'] for audit in audits):
            return {'ok':False,'reason':'brick_core_audit_failed','audits':audits}
        candidates=[core for core in candidates if not any(overlap(core['reserved_area'],block['reserved_area'])
                    for block in board['blocks'].values() if 'reserved_area' in block)]
        legal=survey_slots(self.game,[[core['reserved_area']] for core in candidates])
        if not legal.get('ok'):return legal
        failures=[]
        for index in legal.get('legal',[]):
            core=candidates[index-1];entities=core['entities'][:];routes=[]
            for source,destination in zip(sources,core['ports'][:2]):
                route=plan_connection(self.game,self.builder,obs,source,destination,entities)
                if not route.get('ok'):failures.append(route);break
                routes.append(route);entities=merge_entities(entities,route['entities'])
            if len(routes)!=2:continue
            power=connect_power_groups(self.game,self.builder,obs,[core],entities)
            if not power.get('ok'):failures.append(power);continue
            entities=merge_entities(entities,power['entities'])
            check=preflight(self.game,self.builder,obs,entities)
            if not check.get('ok'):failures.append(check);continue
            obstacles={(e['name'],*xy(e)):e for row in routes+[check] for e in row['clear_obstacles']}
            plan=_plan(entities,core['ports'],reserved_area=core['reserved_area'],
                underground_pairs=[pair for route in routes for pair in route['underground_pairs']],
                clear_obstacles=list(obstacles.values()),clearance_world_id=obs['world_id'],layout_policy='pcb-brick-v1')
            ceiling=None;stone_rate=sources[0].get('nominal_shared_rate_per_minute')
            if type(stone_rate) in (int,float) and math.isfinite(stone_rate) and stone_rate>0:
                ceiling=min(core['rated']['brick_per_minute'],stone_rate*core['rated']['recipe_yield']/
                            self.catalog.recipes['stone-brick']['ingredients'][0]['amount'])
            return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                'phase':'build','plan':plan,'core':core,'audit':audit_brick_core(core,self.catalog),'sources':sources,
                'route_metrics':[route['route_metrics'] for route in routes],
                'stone_supply_ceiling_brick_per_minute':ceiling,
                'capacity_scope':'nominal shared stone upper bound; excludes competition, fuel, power and measured throughput'}
        return {'ok':False,'reason':'no_brick_site_with_complete_native_connections','failures':failures}

    def step(self, obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return {'status':'blocked',**ready}
            self.state=ready;_atomic_json(self.path,self.state)
        if (self.state['world_id']!=obs['world_id'] or self.state['catalog_fingerprint']!=self.catalog.fingerprint):
            return {'status':'blocked','reason':'brick_world_or_catalog_changed'}
        if not obs.get('enabled_recipes',{}).get('stone-brick') or not obs.get('enabled_recipes',{}).get('fast-inserter'):
            return {'status':'blocked','reason':'brick_native_recipes_changed'}
        native=self._inspect_sources(self.state['sources'])
        if not native.get('ok'):return {'status':'blocked',**native}
        board=self._board(obs)
        if board is None:return {'status':'blocked','reason':'brick_board_identity_changed'}
        if board['blocks'].get('brick_production')!=self.state['plan']:
            if 'brick_production' in board['blocks']:
                return {'status':'blocked','reason':'brick_reserved_block_changed'}
            if any(overlap(self.state['plan']['reserved_area'],block['reserved_area'])
                   for block in board['blocks'].values() if 'reserved_area' in block):
                return {'status':'blocked','reason':'brick_reservation_no_longer_available'}
            board['blocks']['brick_production']=self.state['plan']
            _atomic_json(self.path.parent/'pcb-board.json',board)
        result=self.builder.ensure_plan(obs,self.state['plan'])
        if result.get('status')!='succeeded':return result
        evidence=self.proof(obs)
        if evidence is None:return {'status':'waiting','reason':'waiting_for_automatic_bricks_on_both_native_output_lanes'}
        self.state['phase']='verified';_atomic_json(self.path,self.state)
        return {'status':'succeeded','reason':'normally belt-supplied opposing brick furnaces commissioned','evidence':evidence}

    def proof(self, obs):
        if (not self.state or self.state['world_id']!=obs['world_id']
                or self.state['catalog_fingerprint']!=self.catalog.fingerprint
                or not underground_proof(obs,self.state['plan'])):return None
        if not self._inspect_sources(self.state['sources']).get('ok'):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']};furnaces=[]
        for wanted in self.state['plan']['entities']:
            live=actual.get((wanted['name'],*xy(wanted)))
            if not live or (wanted['name']!='stone-furnace' and live.get('direction')!=wanted['direction']):return None
            if wanted['name']=='stone-furnace':
                if live.get('recipe')!='stone-brick' or live.get('products_finished',0)<=0:return None
                if live.get('remaining_burning_fuel',0)<=0 and live.get('inventory',{}).get('coal',0)<=0:return None
                furnaces.append(live)
            if wanted['name'] in ('inserter','fast-inserter') and (not live.get('electric_network_connected') or live.get('energy',0)<=0):return None
            if wanted['name']=='splitter' and any(live.get(key)!=wanted[key] for key in ('splitter_input_priority','splitter_output_priority')):return None
        core=self.state['core'];first=core['producer_rows']['right'][0]['position']
        groups={'output':[],'input':[]}
        for wanted in core['entities']:
            if wanted['name']!='transport-belt' or wanted['direction']!=4:continue
            if wanted['position']['y']==core['ports'][-1]['position']['y']:groups['output'].append(dict(wanted))
            if wanted['position']['x']>=first['x']-1.5 and wanted['position']['y'] in (first['y']-2.5,first['y']+7.5):groups['input'].append(dict(wanted))
        for specs in groups.values():
            for spec in specs:spec['unit_number']=actual[(spec['name'],*xy(spec))]['unit_number']
        payload=json.dumps(json.dumps(groups,separators=(',',':')))
        lanes=self.game.query('''local args=helpers.json_to_table('''+payload+''');local result={ok=true,tick=game.tick}
for _,group in ipairs({"input","output"}) do local lanes={{},{}}
 for _,spec in ipairs(args[group]) do local e=target(spec.position,"transport-belt")
  if not e or e.unit_number~=spec.unit_number or e.force~=f or e.surface~=s or e.direction~=4 then return {ok=false} end
  for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do lanes[lane][v.name]=(lanes[lane][v.name] or 0)+v.count end end
 end
 if group=="input" then result.input_left=lanes[1];result.input_right=lanes[2] else result.left=lanes[1];result.right=lanes[2] end
end
return result''')
        if not lanes.get('ok'):return None
        for side,item in (('left','stone-brick'),('right','stone-brick'),('input_left','stone'),('input_right','coal')):
            content=lanes.get(side,{})
            if set(content)-{item} or content.get(item,0)<=0:return None
        return {'tick':obs['tick'],'furnace_units':[e['unit_number'] for e in furnaces],
            'products_finished':[e['products_finished'] for e in furnaces],'native_lanes':lanes,
            'rated':core['rated'],'stone_supply_ceiling_brick_per_minute':self.state.get('stone_supply_ceiling_brick_per_minute'),
            'scope':'ordinary automatic brick commissioning; no sustained-rate acceptance'}
