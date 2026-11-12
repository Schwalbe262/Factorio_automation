"""Replace only an owned ore delivery suffix after its final aggregation join."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .bus_factory import identity, trace_corridor
from .planning import preflight
from .resource_reroute import _bound, _pair_geometry, _replace_route, partition_routes
from .smelting import merge_entities, port
from .transport import plan_connection, underground_proof, xy


FURNACE_NAMES=frozenset(('stone-furnace','steel-furnace','electric-furnace'))


def plan_revision(plan):
    value={key:plan.get(key,[]) for key in ('entities','ports','underground_pairs')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def source_revision(saved):
    banks=[{'plan':bank['plan'],'connection':bank.get('consumer_connection'),'rate':bank['nominal_rate_per_minute']}
           for bank in saved['banks'] if bank['plan']['resource'] in ('iron-ore','copper-ore')]
    return hashlib.sha256(json.dumps(banks,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def delivery_context(smelting, outposts, resource='iron-ore'):
    """Trace the canonical source and locate the last registered native join."""
    banks=[bank for bank in outposts['banks'] if bank['plan']['resource']==resource]
    primaries=[bank for bank in banks if not bank.get('consumer_connection')]
    consumers=[bank for bank in smelting['banks'] if bank['resource']==resource]
    if len(primaries)!=1 or len(consumers)!=1:raise ValueError('one canonical source and furnace bank required')
    source=primaries[0]['plan']['ports'][0]
    destination=next(p for p in consumers[0]['ports'] if p['item']==resource and p['direction']=='input')
    trace=trace_corridor(smelting['plan'],source,destination)
    if not trace.get('ok'):raise ValueError(trace['reason'])
    indices={xy(e):index for index,e in enumerate(trace['entities'])}
    joins=[]
    for bank in banks:
        connection=bank.get('consumer_connection')
        if not connection:continue
        target=connection['target'];index=indices.get(xy(target))
        if (connection['item']!=resource or index is None or identity(trace['entities'][index])!=identity(target)
                or type(target.get('unit_number')) is not int):
            raise ValueError('registered source aggregation is outside canonical delivery')
        joins.append(target)
    last=max([indices[xy(target)] for target in joins],default=0)
    return {'trace':trace,'last_join_index':last,'joins':joins,'banks':banks,'destination':destination,
            'primary_port':source,'furnace_bank':consumers[0]}


class OreTransportReroute:
    def __init__(self,game,builder,catalog,smelting,outposts,*,bus_factory=None,green=None):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.smelting,self.outposts,self.bus_factory,self.green=smelting,outposts,bus_factory,green
        root=Path(game.cfg.runtime_dir)
        self.path=root/'ore-transport-reroute.json'
        self.paths={'smelting':Path(smelting.path),'outposts':Path(outposts.path),'board':root/'pcb-board.json',
                    'factory':root/'bus-factory.json','green':root/'logistic-science.json'}
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def _documents(self):
        return {key:json.loads(path.read_text(encoding='utf-8')) for key,path in self.paths.items()}

    def _save(self):_atomic_json(self.path,self.state)

    @staticmethod
    def _plans(docs):
        return {'smelting':docs['smelting']['plan'],'board':docs['board']['blocks']['smelting_district'],
                'factory':docs['factory']['source_plan'],'green':docs['green']['source_plan']}

    @staticmethod
    def _exact(actual,wanted):
        live=actual.get((wanted['name'],*xy(wanted)))
        return live if live and live.get('unit_number')==wanted['unit_number'] and identity(live)==identity(wanted) else None

    def _first_clear(self,obs,context,actual):
        candidates=context['trace']['entities'][context['last_join_index']:context['last_join_index']+13]
        rows=[_bound(spec,actual[(spec['name'],*xy(spec))]) for spec in candidates]
        payload=json.dumps(json.dumps(rows,separators=(',',':')))
        return self.game.query('''local rows=helpers.json_to_table('''+payload+''');local original=rows[1]
for index,spec in ipairs(rows) do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=spec.direction then
  return {ok=false,reason="ore_local_escape_identity_changed"} end
 if e.name~="transport-belt" or e.direction~=original.direction then
  return {ok=false,reason="ore_local_escape_must_remain_existing_straight_belts"} end
 local resources=s.find_entities_filtered{area=e.bounding_box,type="resource"}
 if #resources==0 then return {ok=true,index=index,tick=game.tick} end
 for _,ore in ipairs(resources) do if ore.name~="iron-ore" then
  return {ok=false,reason="ore_local_escape_crosses_foreign_resource"} end end
end
return {ok=false,reason="ore_local_escape_has_no_off_patch_tile_within_twelve_steps"}''')

    def prepare(self,obs,parameters=None):
        if parameters not in (None,{}, {'resource':'iron-ore'}):
            return {'ok':False,'reason':'ore_reroute_supports_current_iron_delivery_only'}
        try:
            docs=self._documents()
            if any(doc.get('world_id')!=obs['world_id'] or doc.get('catalog_fingerprint')!=self.catalog.fingerprint for doc in docs.values()):
                return {'ok':False,'reason':'ore_reroute_document_identity_changed'}
            plans=self._plans(docs);old_plan=plans['smelting'];revision=plan_revision(old_plan)
            if any(plan_revision(plan)!=revision for plan in plans.values()):
                return {'ok':False,'reason':'ore_source_plan_copies_disagree'}
            context=delivery_context(docs['smelting'],docs['outposts'])
            owned=merge_entities(old_plan['entities'],*(bank['plan']['entities'] for bank in docs['outposts']['banks']
                                  if bank['plan']['resource'] in ('iron-ore','copper-ore')))
            actual={(e['name'],*xy(e)):e for e in obs['entities']};bindings={}
            for spec in owned:
                live=actual.get((spec['name'],*xy(spec)))
                if not live or identity(live)!=identity(spec) or type(live.get('unit_number')) is not int:
                    return {'ok':False,'reason':'owned_ore_source_or_smelting_asset_changed','position':spec['position']}
                bindings[identity(spec)]=_bound(spec,live)
            if any(not self._exact(actual,target) for target in context['joins']):
                return {'ok':False,'reason':'registered_ore_aggregation_unit_changed'}
            clear=self._first_clear(obs,context,actual)
            if not clear.get('ok'):return clear
            index=clear.get('index')
            if type(index) is not int or not 1<=index<=13:return {'ok':False,'reason':'invalid_off_patch_source_survey'}
            offset=context['last_join_index']+index-1
            start=context['trace']['entities'][offset]
            source=port('iron-ore',*xy(start),start['direction'],'output')
            tail=context['trace']['entities'][offset:];positions={xy(e) for e in tail}
            old_route={'entities':tail,'underground_pairs':[pair for pair in old_plan.get('underground_pairs',[])
                if tuple(pair['input']) in positions and tuple(pair['output']) in positions]}
            if not underground_proof(obs,old_route):return {'ok':False,'reason':'old_ore_underground_pair_not_verified'}
            old_ids={identity(e) for e in tail};ignored=[bindings[identity(e)]['unit_number'] for e in tail[1:-1]]
            fixed=[e for e in old_plan['entities'] if identity(e) not in old_ids]
            virtual=dict(obs,entities=[e for e in obs['entities'] if e.get('unit_number') not in ignored])
            adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
                can_place=lambda es:preflight(self.game,self.builder,obs,es,ignored_units=ignored))
            route=plan_connection(self.game,adapter,virtual,source,context['destination'],fixed,ignored_units=ignored)
            if not route.get('ok'):return dict(route,stage='bounded_resource_avoiding_ore_delivery')
            quality=route['route_metrics']
            if quality['turns']>6 or quality['length']>1.35*quality['manhattan_distance']+4 or not _pair_geometry(route):
                return {'ok':False,'reason':'ore_alternate_corridor_quality_or_pair_gate_failed'}
            traced=trace_corridor(route,source,context['destination'])
            if not traced.get('ok') or len(traced['entities'])!=len(route['entities']):
                return {'ok':False,'reason':'ore_alternate_not_one_complete_corridor'}
            for spec in route['entities']:
                live=actual.get((spec['name'],*xy(spec)))
                if live and live['unit_number'] not in {bindings[key]['unit_number'] for key in old_ids}:
                    return {'ok':False,'reason':'ore_alternate_would_claim_foreign_asset','position':spec['position']}
            portions=partition_routes(old_route,route,self.builder)
            if not portions['added'] or len(portions['conflicts'])>8:
                return {'ok':False,'reason':'ore_alternate_requires_one_to_eight_owned_cutover_assets'}
            # Every registered source/aggregation and both fixed endpoints must
            # survive with the same native unit. No new on-patch scope is added.
            retained_ids={identity(e) for e in portions['retained']}
            if identity(tail[0]) not in retained_ids or identity(tail[-1]) not in retained_ids:
                return {'ok':False,'reason':'ore_fixed_delivery_endpoints_would_change'}
            removed=portions['conflicts']+portions['obsolete']
            removed_units=[bindings[identity(e)]['unit_number'] for e in removed]
            final=_replace_route(old_plan,old_route,route)
            for stage,entities,units in (
                ('whole_alternate_ore_plan',final['entities'],removed_units),
                ('ore_cutover_coexistence',route['entities'],[bindings[identity(e)]['unit_number'] for e in portions['conflicts']]),
                ('ore_prebuild_coexistence',portions['prebuild'],[])):
                check=preflight(self.game,self.builder,obs,entities,ignored_units=units)
                if not check.get('ok'):return dict(check,stage=stage)
            obstacles={(e['name'],*xy(e)):e for e in route.get('clear_obstacles',[])}
            removed_ids={identity(e) for e in removed}
            protected=[binding for key,binding in bindings.items() if key not in removed_ids]
            return {'ok':True,'world_id':obs['world_id'],'catalog_fingerprint':self.catalog.fingerprint,
                'phase':'stock','prepared_tick':obs['tick'],'old_route':old_route,'route':route,'final_plan':final,
                'old_revision':revision,'new_revision':plan_revision(final),'source_revision':source_revision(docs['outposts']),
                'source_port':source,'destination_port':context['destination'],'protected':protected,
                'preserved_local_escape':[bindings[identity(e)] for e in context['trace']['entities'][context['last_join_index']:offset]],
                'registered_joins':context['joins'],'source_banks':context['banks'],'iron_bank':context['furnace_bank'],
                'pending_cutover':[bindings[identity(e)] for e in portions['conflicts']],
                'pending_retirement':[bindings[identity(e)] for e in portions['obsolete']],
                'prebuild':_plan(portions['prebuild'],[]),'required_stock':dict(Counter(e['name'] for e in portions['added'])),
                'deferred_stock':dict(Counter(e['name'] for e in portions['deferred'])),
                'clearance':_plan([],[],clear_obstacles=list(obstacles.values()),clearance_world_id=obs['world_id']),
                'added':portions['added'],'new_units':[],'source_ports':[bank['plan']['ports'] for bank in context['banks']]}
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return {'ok':False,'reason':'ore_reroute_saved_delivery_incompatible','detail':str(exc)}

    def _check(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        if any(not self._exact(actual,spec) for spec in self.state['protected']):
            return {'status':'blocked','reason':'preserved_ore_source_join_or_smelting_unit_changed'}
        known={identity(e):e for e in self.state['new_units']}
        for spec in self.state['added']:
            live=actual.get((spec['name'],*xy(spec)));previous=known.get(identity(spec))
            if previous and not self._exact(actual,previous):return {'status':'blocked','reason':'new_ore_route_unit_changed'}
            if live and identity(live)==identity(spec) and not previous:self.state['new_units'].append(_bound(spec,live))
        return None

    def _stock(self,obs,needed):
        if not needed:return None
        payload=json.dumps(json.dumps(list(needed),separators=(',',':')))
        stock=self.game.query('local names=helpers.json_to_table('+payload+''');local result={};local inv=a.get_main_inventory()
for _,name in ipairs(names) do result[name]=inv.get_item_count{name=name,quality="normal"} end
return {stock=result}''').get('stock',{})
        for name,count in needed.items():
            if stock.get(name,0)<count:
                normal=dict(obs,inventory=dict(obs.get('inventory',{})));normal['inventory'][name]=stock.get(name,0)
                return self.builder.bootstrap.ensure_item(normal,name,count)
        return None

    def _recover(self,obs,key):
        actual={(e['name'],*xy(e)):e for e in obs['entities']};pending=self.state.get('recovering',[])
        pending_units={e['unit_number'] for e in pending};remaining=[]
        for wanted in self.state[key]:
            live=actual.get((wanted['name'],*xy(wanted)))
            if live and not self._exact(actual,wanted):return {'status':'blocked','reason':'ore_recovery_will_not_retarget_new_unit'}
            if not live and wanted['unit_number'] not in pending_units:
                return {'status':'blocked','reason':'unstarted_owned_ore_recovery_asset_missing'}
            if live:remaining.append(wanted)
        self.state[key]=remaining;self.state['recovering']=[e for e in pending if self._exact(actual,e)]
        if not self.state['recovering']:self.state['recovering']=remaining[:8]
        self._save()
        actions=[dict(type='mine',name=e['name'],position=e['position'],count=1,
                      expected_entity_unit=e['unit_number'],expected_entity_world_id=obs['world_id']) for e in self.state['recovering']]
        return {'type':'mine_many','actions':actions,'reason':'normally recover only exact superseded ore delivery assets'} if actions else None

    def _publish(self):
        docs=self._documents();s=self.state
        if any(doc.get('world_id')!=s['world_id'] or doc.get('catalog_fingerprint')!=s['catalog_fingerprint'] for doc in docs.values()):
            return {'status':'blocked','reason':'ore_publication_document_identity_changed'}
        if source_revision(docs['outposts'])!=s['source_revision']:
            return {'status':'blocked','reason':'ore_source_bank_ports_or_connections_changed'}
        plans=self._plans(docs)
        if any(plan_revision(plan) not in (s['old_revision'],s['new_revision']) for plan in plans.values()):
            return {'status':'blocked','reason':'ore_source_plan_changed_during_migration'}
        for name,plan in plans.items():
            if plan_revision(plan)==s['old_revision']:plans[name]=_replace_route(plan,s['old_route'],s['route'])
        docs['smelting']['plan']=plans['smelting'];docs['board']['blocks']['smelting_district']=plans['board']
        docs['factory']['source_plan']=plans['factory'];docs['green']['source_plan']=plans['green']
        docs['smelting']['ore_delivery_reroutes']={'iron-ore':{'source_port':s['source_port'],
            'destination_port':s['destination_port'],'suffix_route_metrics':s['route']['route_metrics']}}
        for name in ('smelting','factory','green','board'):_atomic_json(self.paths[name],docs[name])
        self.smelting.state=docs['smelting']
        if self.bus_factory is not None:self.bus_factory.state=docs['factory']
        if self.green is not None:self.green.state=docs['green']
        s['published']=True;self._save()
        return None

    def _furnace_counts(self,obs):
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        return {str(actual[(e['name'],*xy(e))]['unit_number']):actual[(e['name'],*xy(e))].get('products_finished',0)
                for e in self.state['iron_bank']['entities'] if e['name'] in FURNACE_NAMES}

    def step(self,obs,parameters=None):
        if parameters not in (None,{}, {'resource':'iron-ore'}):
            return {'status':'blocked','reason':'ore_reroute_supports_current_iron_delivery_only'}
        if not self.state:
            prepared=self.prepare(obs,parameters)
            if not prepared.get('ok'):return {'status':'blocked',**prepared}
            self.state=prepared;self._save()
        s=self.state
        if s['world_id']!=obs['world_id'] or s['catalog_fingerprint']!=self.catalog.fingerprint:
            return {'status':'blocked','reason':'ore_reroute_world_or_catalog_changed'}
        result=self._check(obs)
        if result:return result
        self._save()
        if s['phase'] not in ('retire','verified'):
            actual={(e['name'],*xy(e)):e for e in obs['entities']}
            if any(not self._exact(actual,e) for e in s['pending_retirement']):
                return {'status':'blocked','reason':'old_ore_buffer_changed_before_new_route_proof'}
        if s['phase']=='stock':
            result=self._stock(obs,s['required_stock'])
            if result:return result
            result=self.builder.ensure_plan(obs,s['clearance'])
            if result.get('status')!='succeeded':return result
            s['phase']='prebuild';self._save()
        if s['phase']=='prebuild':
            result=self.builder.ensure_plan(obs,s['prebuild'])
            if result.get('status')!='succeeded':return result
            s['phase']='cutover';self._save()
        if s['phase']=='cutover':
            result=self._stock(obs,s['deferred_stock'])
            if result:return result
            check=preflight(self.game,self.builder,obs,s['route']['entities'],ignored_units=[e['unit_number'] for e in s['pending_cutover']])
            if not check.get('ok'):return {'status':'blocked',**check}
            result=self._recover(obs,'pending_cutover')
            if result:return result
            s['phase']='build';self._save()
        if s['phase']=='build':
            result=self.builder.ensure_plan(obs,s['route'])
            if result.get('status')!='succeeded':return result
            if not underground_proof(obs,s['route']):return {'status':'waiting','reason':'waiting_for_new_ore_underground_pairs'}
            s['baseline']={'tick':obs['tick'],'counts':self._furnace_counts(obs)};s['phase']='publish';self._save()
        if s['phase']=='publish':
            result=self._publish()
            if result:return result
            s['phase']='commission';self._save()
        if s['phase'] in ('commission','retire','verified'):
            evidence=self.proof(obs)
            if evidence is None:return {'status':'waiting','reason':'waiting_for_loaded_new_ore_path_fresh_source_and_iron_production'}
            s['evidence']=evidence;s['phase']='retire';self._save()
            result=self._recover(obs,'pending_retirement')
            if result:return result
            s['phase']='verified';self._save()
            return {'status':'succeeded','reason':'iron delivery moved off the copper patch; fixed mining aggregation preserved','evidence':evidence}
        return {'status':'blocked','reason':'unknown_ore_reroute_phase'}

    def proof(self,obs):
        s=self.state
        if (not s or not s.get('published') or s['world_id']!=obs['world_id']
                or s['catalog_fingerprint']!=self.catalog.fingerprint or self._check(obs)
                or not underground_proof(obs,s['route'])):return None
        actual={(e['name'],*xy(e)):e for e in obs['entities']}
        if any((e['name'],*xy(e)) not in actual or identity(actual[(e['name'],*xy(e))])!=identity(e) for e in s['route']['entities']):return None
        current=self._furnace_counts(obs);baseline=s.get('baseline',{})
        old=baseline.get('counts',{})
        if (set(current)!=set(old) or obs['tick']<=baseline.get('tick',obs['tick'])
                or any(current[unit]<old[unit] for unit in current) or sum(current.values())<=sum(old.values())):return None
        banks=[self.outposts.bank_proof(obs,deepcopy(bank)) for bank in s['source_banks']]
        if any(not bank for bank in banks):return None
        downstream=self.smelting.proof(obs)
        if not downstream:return None
        rows=[_bound(e,actual[(e['name'],*xy(e))]) for e in s['added']]
        payload=json.dumps(json.dumps(rows,separators=(',',':')))
        raw=self.game.query('''local rows=helpers.json_to_table('''+payload+''');local count=0
for _,spec in ipairs(rows) do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=spec.unit_number or e.direction~=spec.direction then return {ok=false} end
 for lane=1,2 do for _,v in pairs(e.get_transport_line(lane).get_contents()) do
  if v.name~="iron-ore" then return {ok=false,reason="ore_alternate_contaminated"} end
  count=count+v.count
 end end
end
return {ok=true,tick=game.tick,count=count}''')
        if not raw.get('ok') or raw.get('count',0)<=0:return None
        return {'tick':obs['tick'],'added_path_raw_ore':raw,'source_banks':banks,'downstream_smelting':downstream,
            'iron_finished_since_cutover':sum(current.values())-sum(old.values()),'route_metrics':s['route']['route_metrics'],
            'preserved_join_units':[e['unit_number'] for e in s['registered_joins']],
            'scope':'loaded added iron path, native mining/smelting and new iron production; no sustained-rate acceptance'}
