"""Add paired steel furnaces without moving any installed producer or port."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .bus_tap import signature
from .bus_budget import declared_bus_demand
from .mining_layout_policy import diagnose_bank_capacity
from .planning import overlap,preflight
from .radar_network import RADAR_POWER_LUA,planned_power_watts
from .smelting import merge_entities
from .steel_production import SteelProduction,steel_core,steel_rates,audit_steel_core
from .transport import surface_pipe_proof,underground_proof,unbuilt_external_keepouts,xy


MODE='additive_steel_capacity_v1'
ALLOWED_ADDITIONS={'stone-furnace','inserter','small-electric-pole','transport-belt'}


def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _key(q):return q['name']+':'+str(xy(q))
def _bound(q):return {k:q[k]for k in('name','position','direction','unit_number')}


def validate_parameters(parameters):
    if(not isinstance(parameters,dict)or set(parameters)!={'rate_per_minute'}
            or type(parameters['rate_per_minute'])not in(int,float)or not math.isfinite(parameters['rate_per_minute'])
            or parameters['rate_per_minute']<=0):raise ValueError('steel_capacity requires positive finite absolute rate_per_minute')


def additive_layout(catalog,old_core,old_plan,pairs):
    first=old_core['producer_rows']['right'][0]['position']
    core=steel_core(catalog,first['x'],first['y'],per_side_count=pairs,reserved_pairs=old_core['reserved_pairs'])
    if(core['ports']!=old_core['ports']or core['reserved_area']!=old_core['reserved_area']
            or not {signature(q)for q in old_core['entities']}<={signature(q)for q in core['entities']}):
        raise ValueError('steel extension would change an installed core entity or fixed interface')
    audit=audit_steel_core(core,catalog)
    if not audit['ok']:raise ValueError('expanded steel core audit failed:'+str(audit['errors']))
    old_keys={signature(q)for q in old_plan['entities']};added=[q for q in core['entities']if signature(q)not in old_keys]
    if any(q['name']not in ALLOWED_ADDITIONS for q in added):raise ValueError('unsupported additive steel asset')
    extra={k:deepcopy(v)for k,v in old_plan.items()if k not in('ok','reason','entities','ports','required_items')}
    plan=_plan(merge_entities(old_plan['entities'],added),deepcopy(old_plan['ports']),**extra)
    if not {signature(q)for q in old_plan['entities']}<={signature(q)for q in plan['entities']}:raise ValueError('old steel plan is not retained')
    input_positions=set()
    for side,row in core['producer_rows'].items():
        for q in row:
            x,y=xy(q);input_positions.add((x+.5,y-1.5 if side=='right'else y+1.5))
    inputs=[q for q in added if q['name']=='inserter'and xy(q)in input_positions]
    furnaces=[q for q in added if q['name']=='stone-furnace']
    if len(inputs)!=len(furnaces):raise ValueError('every added furnace needs one isolated cold input arm')
    return dict(core=core,plan=plan,added=added,new_furnaces=furnaces,new_input_arms=inputs,
                cold_plan=_plan([q for q in added if q not in inputs],[]),audit=audit)


def _source(core,obs):
    p=next(q for q in core['ports']if q.get('direction')=='output')
    live=next((q for q in obs['entities']if q['name']=='transport-belt'and xy(q)==xy(p)),None)
    if not live or type(live.get('unit_number'))is not int or live.get('direction')!=p['facing']:raise ValueError('fixed native steel output missing')
    return dict(p,name='transport-belt',unit_number=live['unit_number'],world_id=obs['world_id'])


def verify_steel_capacity(game,obs,catalog,steel,*,pinned_plan_revision,pinned_source,pinned_furnace_units):
    """Read-only consumer hook: validate additive ancestry, never rewrite a pin."""
    try:
        state=steel.state
        if(state.get('world_id')!=obs.get('world_id')or state.get('catalog_fingerprint')!=catalog.fingerprint
                or state.get('phase')!='verified'or not pinned_furnace_units
                or len(set(pinned_furnace_units))!=len(pinned_furnace_units)
                or any(type(u)is not int or u<1 for u in pinned_furnace_units)):
            raise ValueError('commissioned steel and original exact consumer furnace IDs required')
        if hasattr(steel,'path'):
            root=Path(steel.path).parent
            if json.loads(Path(steel.path).read_text())!=state:raise ValueError('steel producer in-memory checkpoint stale')
            board=json.loads((root/'pcb-board.json').read_text());industrial=json.loads((root/'industrial-operations.json').read_text())
            if(any(q.get('world_id')!=obs['world_id']or q.get('catalog_fingerprint')!=catalog.fingerprint for q in(board,industrial))
                    or board.get('blocks',{}).get('steel_production')!=state['plan']
                    or industrial.get('producer_authorizations',{}).get('steel',{}).get('plan_revision')!=_hash(state['plan'])):
                raise ValueError('steel capacity publication is incomplete or canonical authorities changed')
        source=_source(state['core'],obs)
        if source!=pinned_source:raise ValueError('pinned full steel output descriptor changed')
        current=_hash(state['plan']);revision=pinned_plan_revision;chain=[]
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        while revision!=current:
            rows=[q for q in state.get('capacity_revisions',[])if q.get('old_plan_revision')==revision]
            if len(rows)!=1 or len(chain)>=8:raise ValueError('missing or ambiguous steel capacity ancestry')
            row=rows[0]
            unsigned={k:v for k,v in row.items()if k!='receipt_id'}
            old_keys={signature(q)for q in row['old_plan']['entities']};new_keys={signature(q)for q in row['new_plan']['entities']}
            new_assets=[q for q in row['new_plan']['entities']if signature(q)not in old_keys]
            retained_keys={(q['name'],*xy(q),q['direction'])for q in row['retained_units']}
            new_furnaces=[q for q in new_assets if q['name']=='stone-furnace'];witness=row.get('production_witness',{})
            produced=witness.get('furnaces',[])
            if(row.get('mode')!=MODE or row.get('world_id')!=obs['world_id']or row.get('catalog_fingerprint')!=catalog.fingerprint
                    or _hash(unsigned)!=row.get('receipt_id')
                    or _hash(row['old_plan'])!=revision or _hash(row['new_plan'])!=row['new_plan_revision']
                    or row['old_plan']['ports']!=row['new_plan']['ports']or row['source']!=pinned_source
                    or not old_keys<=new_keys or any(q['name']not in ALLOWED_ADDITIONS for q in new_assets)
                    or {(q['name'],*xy(q),q.get('direction',0))for q in row['old_plan']['entities']}!=retained_keys
                    or row['old_plan'].get('reserved_area')!=row['new_plan'].get('reserved_area')
                    or not set(pinned_furnace_units)<=set(row['old_furnace_units'])<=set(row['current_furnace_units'])
                    or len(set(row['current_furnace_units']))!=len(row['current_furnace_units'])
                    or len(new_furnaces)not in(2,4)or len(produced)!=len(new_furnaces)or not witness.get('ok')
                    or any(q.get('products_finished',0)<=0 for q in produced)
                    or {q['unit_number']for q in produced}!=set(row.get('added_furnace_units',[]))
                    or set(row['current_furnace_units'])-set(row['old_furnace_units'])!=set(row.get('added_furnace_units',[]))
                    or any(row.get('native_output_lanes',{}).get(side,{}).get('steel-plate',0)<=0 for side in('left','right'))):
                raise ValueError('steel capacity receipt is not an interface-preserving addition')
            for q in row['retained_units']:
                live=actual.get((q['name'],*xy(q)))
                if not live or live.get('unit_number')!=q['unit_number']or live.get('direction')!=q['direction']:
                    raise ValueError('an original paid steel asset was replaced')
            revision=row['new_plan_revision'];chain.append(row['receipt_id'])
        furnaces=[q for q in state['plan']['entities']if q['name']=='stone-furnace']
        units=[actual[(q['name'],*xy(q))]['unit_number']for q in furnaces]
        if len(set(units))!=len(units)or not set(pinned_furnace_units)<=set(units):raise ValueError('original consumer furnaces no longer installed')
        proof=steel.proof(obs,consumer_furnace_units=units)
        if not proof or sorted(proof['furnace_units'])!=sorted(units):raise ValueError('current native automatic steel health missing')
        return dict(ok=True,current_plan_revision=current,original_furnace_units=list(pinned_furnace_units),
                    current_furnace_units=units,source=source,proof=proof,rated=proof['rated'],chain=chain)
    except(OSError,KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'steel_additive_capacity_not_verified','detail':str(exc)}


class SteelCapacity:
    def __init__(self,game,builder,catalog,perimeter,*,steel,industrial=None):
        self.game,self.builder,self.catalog,self.perimeter,self.steel,self.industrial=game,builder,catalog,perimeter,steel,industrial
        self.path=Path(game.cfg.runtime_dir)/'steel-capacity.json'
        self.state=json.loads(self.path.read_text())if self.path.exists()else{}
        self.last_failure=None

    def _save(self):_atomic_json(self.path,self.state)
    def _docs(self):return {k:json.loads((self.path.parent/n).read_text())for k,n in{
        'steel':'steel-production.json','board':'pcb-board.json','industrial':'industrial-operations.json'}.items()}

    def _upstream(self,obs,core,old_core):
        root=self.path.parent;bus_doc=json.loads((root/'bus-factory.json').read_text())
        if bus_doc.get('world_id')!=obs['world_id']or bus_doc.get('catalog_fingerprint')!=self.catalog.fingerprint:
            return {'ok':False,'reason':'steel_capacity_bus_budget_frame_changed'}
        bus=bus_doc['bus']
        source=next(q for q in self.steel.state['sources']if q['item']=='iron-plate')
        typed=next((q for q in bus['ports']if q.get('position')==source['position']and q.get('item')=='iron-plate'),None)
        if not typed or typed.get('priority')!='surplus':return {'ok':False,'reason':'steel_capacity_registered_surplus_iron_missing'}
        supply=bus['capacity']['materials']['iron-plate']['supply_ceiling_per_minute'];iron=core['rated']['iron_per_minute']
        if supply<iron or iron>450:return {'ok':False,'reason':'expand_iron_supply_before_steel_capacity','available':supply,'required':iron}
        coal=next(q for q in self.steel.state['sources']if q['item']=='coal')
        registry=json.loads((root/'material-sources.json').read_text())
        if(registry.get('world_id')!=obs['world_id']or registry.get('catalog_fingerprint')!=self.catalog.fingerprint
                or registry.get('sources',{}).get('steel-coal')!=coal):
            return {'ok':False,'reason':'steel_capacity_exact_registered_coal_handoff_changed'}
        supplies=json.loads((root/'material-supply.json').read_text())
        rows=[q for q in supplies['supplies'].values()if q.get('material_supply_id')==coal.get('material_supply_id')]
        if(len(rows)!=1 or rows[0].get('phase')!='verified'or supplies.get('world_id')!=obs['world_id']or supplies.get('catalog_fingerprint')!=self.catalog.fingerprint):
            return {'ok':False,'reason':'steel_capacity_shared_coal_producer_authority_missing'}
        capacity=diagnose_bank_capacity(self.game,obs,self.catalog,rows[0]['bank'])
        brick=json.loads((root/'brick-production.json').read_text())
        if(brick.get('world_id')!=obs['world_id']or brick.get('catalog_fingerprint')!=self.catalog.fingerprint
                or brick.get('phase')!='verified'or not any(q.get('item')=='coal'and q.get('material_supply_id')==coal['material_supply_id']for q in brick.get('sources',[]))):
            return {'ok':False,'reason':'steel_capacity_shared_brick_consumer_frame_changed'}
        other=brick.get('core',{}).get('rated',{}).get('coal_per_minute_at_max_catalog_usage')
        required=core['rated']['coal_per_minute_at_max_catalog_usage']
        if(type(other)not in(int,float)or not capacity.get('ok')or not capacity.get('all_installed_drop_lanes_verified')
                or capacity.get('active_collector_ceiling_per_minute',0)<required+other):
            return {'ok':False,'reason':'expand_shared_industrial_coal_before_steel_capacity','evidence':capacity,'required_with_brick':required+(other or 0)}
        claimed=declared_bus_demand(json.loads((root/'pcb-board.json').read_text()),'iron-plate')
        delta=iron-old_core['rated']['iron_per_minute']
        return dict(ok=True,iron_source_nominal=supply,iron_required=iron,iron_additive_delta=delta,
            all_claimed_iron_peak_after=claimed+delta,design_iron_deficit=max(0,claimed+delta-supply),
            guaranteed_surplus_per_minute=0,shared_coal_potential=capacity['active_collector_ceiling_per_minute'],
            coal_required_with_brick=required+other,scope='shared nominal budgets; surplus backpressure and current source potential, no sustained-rate claim')

    def _power(self,obs,old,added,board):
        actual={(q['name'],*xy(q)):q for q in obs['entities']};pole=next(q for q in old['core']['entities']if q['name']=='small-electric-pole')
        live=actual[('small-electric-pole',*xy(pole))];source=dict(_bound(live),electric_network_id=live['electric_network_id'],world_id=obs['world_id'])
        watts=sum(self.catalog.entities[q['name']].get('energy_usage',0)*60 for q in added if q['name']=='inserter')
        return self.game.query(RADAR_POWER_LUA.replace('PAYLOAD',_payload(dict(world_id=obs['world_id'],source=source,preserved_poles=[source],
            scheduled_watts=planned_power_watts(self.catalog,obs,board),additional_watts=watts))))

    def prepare(self,obs,parameters):
        validate_parameters(parameters)
        try:
            if(not obs.get('technologies',{}).get('steel-processing')or not all(obs.get('enabled_recipes',{}).get(q)
                    for q in('steel-plate','stone-furnace','inserter','small-electric-pole','transport-belt'))):
                return {'ok':False,'reason':'steel_capacity_normal_research_and_build_recipes_required'}
            docs=self._docs();old=docs['steel'];core=old['core']
            if any(q.get('world_id')!=obs['world_id']or q.get('catalog_fingerprint')!=self.catalog.fingerprint for q in docs.values()):
                return {'ok':False,'reason':'steel_capacity_world_or_catalog_changed'}
            if old.get('phase')!='verified'or old!=self.steel.state or docs['board']['blocks'].get('steel_production')!=old['plan']:
                return {'ok':False,'reason':'steel_capacity_requires_matching_commissioned_producer'}
            target=parameters['rate_per_minute'];pairs=next((p for p in range(core['per_side_count'],core['reserved_pairs']+1)if steel_rates(self.catalog,2*p)['steel_per_minute']>=target),None)
            if pairs is None:return {'ok':False,'reason':'steel_capacity_exceeds_fixed_reserved_pairs','maximum':steel_rates(self.catalog,2*core['reserved_pairs'])['steel_per_minute']}
            if pairs==core['per_side_count']:return {'ok':False,'reason':'steel_capacity_target_already_installed','rated':core['rated']}
            layout=additive_layout(self.catalog,core,old['plan'],pairs)
            actual={(q['name'],*xy(q)):q for q in obs['entities']};retained=[]
            for spec in old['plan']['entities']:
                live=actual.get((spec['name'],*xy(spec)))
                if not live or type(live.get('unit_number'))is not int or live.get('direction')!=spec.get('direction',0):
                    return {'ok':False,'reason':'steel_capacity_old_native_asset_changed','position':spec['position']}
                retained.append(_bound(live))
            units=[actual[(q['name'],*xy(q))]['unit_number']for q in core['entities']if q['name']=='stone-furnace']
            if not self.steel.proof(obs,consumer_furnace_units=units):return {'ok':False,'reason':'steel_capacity_current_automatic_health_missing','evidence':getattr(self.steel,'last_proof_failure',None)}
            foreign={k:v for k,v in docs['board']['blocks'].items()if k!='steel_production'}
            if any(overlap(layout['core']['reserved_area'],q['reserved_area'])for q in foreign.values()if q.get('reserved_area')):
                return {'ok':False,'reason':'steel_capacity_existing_reservation_conflict'}
            if self.builder._occupied_by_plan(layout['added'])&unbuilt_external_keepouts(self.builder,obs,foreign):
                return {'ok':False,'reason':'steel_capacity_foreign_future_transport_conflict'}
            budget=self._upstream(obs,layout['core'],core)
            if not budget.get('ok'):return budget
            power=self._power(obs,old,layout['added'],docs['board'])
            if not power.get('ok'):return power
            if not surface_pipe_proof(self.game,obs,layout['plan']).get('ok'):return {'ok':False,'reason':'steel_capacity_preserved_pipe_changed'}
            check=preflight(self.game,self.builder,obs,layout['plan']['entities'])
            if not check.get('ok'):return check
            layout['cold_plan'].update(clear_obstacles=check['clear_obstacles'],clearance_world_id=obs['world_id'])
            return dict(layout,ok=True,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='cold',parameters=deepcopy(parameters),
                old_state=old,retained_units=retained,old_furnace_units=units,source=_source(core,obs),bindings={},baseline={},budget=budget,
                prepared_tick=obs['tick'],scope_id='industrial:steel',power_preview=power)
        except(OSError,KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'steel_capacity_prepare_context_invalid','detail':str(exc)}

    def _check(self,obs):
        s=self.state;actual={(q['name'],*xy(q)):q for q in obs['entities']}
        if s['world_id']!=obs['world_id']or s['catalog_fingerprint']!=self.catalog.fingerprint:return {'status':'blocked','reason':'steel_capacity_identity_changed'}
        for q in s['retained_units']:
            live=actual.get((q['name'],*xy(q)))
            if not live or live.get('unit_number')!=q['unit_number']or live.get('direction')!=q['direction']:return {'status':'blocked','reason':'steel_capacity_retained_unit_changed','position':q['position']}
        for q in s['added']:
            live=actual.get((q['name'],*xy(q)));unit=s['bindings'].get(_key(q))
            if unit and(not live or live.get('unit_number')!=unit):return {'status':'blocked','reason':'steel_capacity_added_unit_changed'}
            if live:
                if type(live.get('unit_number'))is not int or live.get('direction')!=q['direction']:return {'status':'blocked','reason':'steel_capacity_added_configuration_changed'}
                s['bindings'][_key(q)]=live['unit_number']
        if _source(s['core'],obs)!=s['source']:return {'status':'blocked','reason':'steel_capacity_fixed_output_changed'}
        return None

    def _preview(self):
        preview=SteelProduction(self.game,self.builder,self.catalog)
        preview.state=dict(deepcopy(self.state['old_state']),core=self.state['core'],plan=self.state['plan'])
        return preview

    def _native_new(self,obs):
        specs=[dict(q,unit_number=self.state['bindings'].get(_key(q)))for q in self.state['new_furnaces']]
        return self.game.query('local x=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],furnaces=specs))+');'+NEW_FURNACE_LUA)

    def _publish(self,obs,proof):
        s=self.state
        if not s.get('publication_intent'):
            docs=self._docs();old=s['old_state'];new_revision=_hash(s['plan']);old_revision=_hash(old['plan'])
            if(docs['steel']!=old or docs['board']['blocks'].get('steel_production')!=old['plan']):return {'status':'blocked','reason':'steel_capacity_publish_original_revision_changed'}
            receipt=dict(mode=MODE,world_id=s['world_id'],catalog_fingerprint=s['catalog_fingerprint'],old_plan_revision=old_revision,new_plan_revision=new_revision,
                old_plan=deepcopy(old['plan']),new_plan=deepcopy(s['plan']),source=s['source'],retained_units=s['retained_units'],
                old_furnace_units=s['old_furnace_units'],current_furnace_units=proof['production']['furnace_units'],added_furnace_units=proof['new_furnace_units'],
                commissioning_tick=obs['tick'],production_witness=proof['new_furnace_witness'],native_output_lanes=proof['production']['native_output_lanes'],
                rated=s['core']['rated'],upstream_budget=s['budget'])
            receipt['receipt_id']=_hash(receipt)
            new=dict(deepcopy(old),core=s['core'],plan=s['plan'],audit=s['audit'],phase='verified',
                     capacity_revisions=deepcopy(old.get('capacity_revisions',[]))+[receipt])
            docs['steel']=new;docs['board']['blocks']['steel_production']=s['plan']
            docs['board'].setdefault('consumer_capacity_allocations',[]).append(dict(receipt_id=receipt['receipt_id'],consumer='steel_production',item='iron-plate',
                old_per_minute=old['core']['rated']['iron_per_minute'],new_per_minute=s['core']['rated']['iron_per_minute'],delta_per_minute=s['budget']['iron_additive_delta']))
            auth=docs['industrial'].get('producer_authorizations',{}).get('steel')
            if not auth or auth['plan_revision']!=old_revision:return {'status':'blocked','reason':'steel_capacity_industrial_authorization_changed'}
            auth.update(plan_revision=new_revision,authorized_by=MODE,capacity_receipt=receipt['receipt_id'])
            names={'steel':'steel-production.json','board':'pcb-board.json','industrial':'industrial-operations.json'}
            s['publication_intent']={name:dict(old_hash=_hash(json.loads((self.path.parent/name).read_text())),new_hash=_hash(docs[key]),value=docs[key])for key,name in names.items()}
            s['receipt']=receipt;self._save()
        for name,row in s['publication_intent'].items():
            if _hash(json.loads((self.path.parent/name).read_text()))not in(row['old_hash'],row['new_hash']):return {'status':'blocked','reason':'steel_capacity_publication_compare_and_swap_failed','document':name}
        for name,row in s['publication_intent'].items():
            if _hash(json.loads((self.path.parent/name).read_text()))==row['old_hash']:_atomic_json(self.path.parent/name,row['value'])
        self.steel.state=s['publication_intent']['steel-production.json']['value']
        if self.industrial is not None:self.industrial.state=s['publication_intent']['industrial-operations.json']['value']
        s['published']=True;self._save();return None

    def step(self,obs,parameters):
        validate_parameters(parameters)
        if self.state and self.state.get('parameters')!=parameters:
            if self.state.get('phase')!='verified':return {'status':'blocked','reason':'another_steel_capacity_change_unfinished'}
            if parameters['rate_per_minute']<=self.state['core']['rated']['steel_per_minute']:
                proof=self.proof(obs)
                return {'status':'succeeded','evidence':proof}if proof else{'status':'blocked','reason':'steel_capacity_verified_native_health_missing'}
            self.state={}
        if not self.state:
            ready=self.prepare(obs,parameters)
            if not ready.get('ok'):return dict(ready,status='blocked')
            self.state=ready;self._save()
        s=self.state;issue=self._check(obs)
        if issue:return issue
        self._save()
        if s.get('publication_intent')and not s.get('published'):
            result=self._publish(obs,{})
            if result:return result
            s['phase']='verified';self._save()
        if s['phase']=='verified':
            proof=self.proof(obs);return {'status':'succeeded','evidence':proof}if proof else{'status':'blocked','reason':'steel_capacity_verified_native_health_missing','evidence':self.last_failure}
        budget=self._upstream(obs,s['core'],s['old_state']['core'])
        if not budget.get('ok'):return dict(budget,status='blocked')
        s['budget']=budget;self._save()
        defense=self.perimeter.ensure_coverage(obs,s['scope_id'],[s['core']['reserved_area']])
        if defense is not None:return defense
        if not s.get('post_defense_checked'):
            check=preflight(self.game,self.builder,obs,s['plan']['entities'])
            if not check.get('ok'):return dict(check,status='blocked')
            s['post_defense_checked']=True;self._save()
        pipes=surface_pipe_proof(self.game,obs,s['plan'])
        if not pipes.get('ok'):return dict(pipes,status='blocked')
        if s['phase']=='cold':
            result=self.builder.ensure_plan(obs,s['cold_plan'])
            if result.get('status')!='succeeded':return result
            empty=self._native_new(obs)
            if not empty.get('ok')or any(q['products_finished']!=0 or not q['empty']for q in empty['furnaces']):return {'status':'blocked','reason':'steel_capacity_empty_new_furnace_baseline_required','evidence':empty}
            s['baseline']={str(q['unit_number']):0 for q in empty['furnaces']};s['phase']='feed';self._save()
        result=self.builder.ensure_plan(obs,_plan(s['added'],[]))
        if result.get('status')!='succeeded':return result
        proof=self.proof(obs)
        if not proof:return {'status':'waiting','reason':'waiting_for_all_new_steel_furnaces_and_both_output_lanes','evidence':self.last_failure}
        result=self._publish(obs,proof)
        if result:return result
        s['phase']='verified';self._save()
        return {'status':'succeeded','reason':'paid additive paired steel capacity commissioned with old interfaces preserved','evidence':proof}

    def proof(self,obs):
        self.last_failure=None
        if not self.state or self._check(obs):return None
        s=self.state
        if any(_key(q)not in s['bindings']for q in s['added']):return None
        native=self._native_new(obs)
        if not native.get('ok')or not s.get('baseline')or any(q['products_finished']<=s['baseline'].get(str(q['unit_number']),math.inf)for q in native['furnaces']):
            self.last_failure={'reason':'steel_capacity_every_new_counter_must_advance','native':native};return None
        preview=self._preview()
        all_units=[q['unit_number']for q in obs['entities']if q['name']=='stone-furnace'and xy(q)in{xy(e)for e in s['core']['entities']if e['name']=='stone-furnace'}]
        production=preview.proof(obs,consumer_furnace_units=all_units)if s.get('published')else preview.proof(obs)
        if not production:self.last_failure={'reason':'steel_capacity_complete_automatic_production_health_missing','evidence':getattr(preview,'last_proof_failure',None)};return None
        coverage=self.perimeter.coverage_proof(obs,s['scope_id'])
        if not coverage:return None
        if not underground_proof(obs,s['plan'])or not surface_pipe_proof(self.game,obs,s['plan']).get('ok'):return None
        return dict(tick=obs['tick'],production=production,new_furnace_units=[q['unit_number']for q in native['furnaces']],new_furnace_witness=native,
            retained_furnace_units=s['old_furnace_units'],source=s['source'],rated=s['core']['rated'],upstream_budget=s['budget'],coverage=coverage,
            scope='all new normally supplied furnaces produced; old units and fixed ports retained, dual lanes verified; no sustained-rate claim')


NEW_FURNACE_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="steel_capacity_world_changed"}end
local rows={}
for _,q in ipairs(x.furnaces)do local e=target(q.position,"stone-furnace")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.quality.name~="normal"then return {ok=false,reason="steel_capacity_new_furnace_unit_changed"}end
 local input=e.get_inventory(defines.inventory.crafter_input);local output=e.get_output_inventory();local fuel=e.get_fuel_inventory()
 local empty=true;local fuel_items=0
 for index,inv in ipairs({input,output,fuel})do for i=1,#inv do local st=inv[i];if st.valid_for_read then
  if index<3 then empty=false else fuel_items=fuel_items+st.count end
  local wanted=index==1 and"iron-plate"or index==2 and"steel-plate"or"coal"
  if st.name~=wanted or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
   or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return {ok=false,reason="steel_capacity_new_furnace_contaminated"}end
 end end end
 rows[#rows+1]={unit_number=e.unit_number,products_finished=e.products_finished,empty=empty,fuel_items=fuel_items}
end
return {ok=true,tick=game.tick,furnaces=rows}
'''
