"""Recover a depleted original boiler source with one paid shared coal fork."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .bus_factory import trace_corridor
from .industrial import PlanningBudget
from .mining_layout_policy import diagnose_bank_capacity
from .planning import overlap,preflight
from .shared_coal_tap import replace_header
from .smelting import merge_entities,port
from .transport import plan_connection,surface_pipe_proof,underground_proof,xy

VECTORS={0:(0,-1),4:(1,0),8:(0,1),12:(-1,0)}


def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _key(q):return q['name']+':'+str(xy(q))
def _bound(q):return {k:q[k]for k in('name','position','direction','unit_number')}


def preserved_pipe_specs(specs,actual):
    """Survey immutable normal pipes using their actual orientation.

    Old startup templates used8 for ordinary nondirectional pipes; native0 is
    canonical. Do not rewrite the saved power plan or rotate any real entity.
    """
    result=[]
    for q in specs:
        if q['name']=='pipe':
            live=actual.get(('pipe',*xy(q)))
            if not live or type(live.get('unit_number'))is not int:raise ValueError('exact preserved original pipe missing')
            result.append(dict(q,direction=live['direction']))
        else:result.append(q)
    return result


def power_forks(plan,source,destination,join):
    traced=trace_corridor(plan,source,destination)
    if not traced.get('ok'):raise ValueError(traced['reason'])
    result=[];rows=traced['entities']
    for before,current,after in zip(rows,rows[1:],rows[2:]):
        direction=current['direction'];vx,vy=VECTORS[direction];x,y=xy(current)
        if(any(q['name']!='transport-belt'or q['direction']!=direction for q in(before,current,after))
                or xy(before)!=(x-vx,y-vy)or xy(after)!=(x+vx,y+vy)):continue
        lx,ly=vy,-vx
        for sign in(1,-1):
            side=(lx*sign,ly*sign)
            splitter=dict(entity('splitter',x+side[0]*.5,y+side[1]*.5,direction),_width=2,_height=1,
                          splitter_input_priority='right'if sign==1 else'left',splitter_output_priority='none')
            outlet=entity('transport-belt',x+vx+side[0],y+vy+side[1],direction)
            result.append(dict(old=current,splitter=splitter,outlet=outlet,through=after))
    result.sort(key=lambda h:(h['old']['direction']!=(join['direction']+8)%16,
        abs(xy(h['outlet'])[0]-xy(join)[0])+abs(xy(h['outlet'])[1]-xy(join)[1])))
    return result


def coal_budget(catalog,boilers,active_coal):
    fuel=catalog.items['coal']['fuel_value']
    watts=sum(catalog.entities[q['name']]['energy_usage']*60 for q in boilers)
    demand=watts*60/fuel
    return dict(shared_source_potential_per_minute=active_coal,boiler_max_coal_per_minute=demand,
        spare_potential_per_minute=active_coal-demand,source_count=1,consumer_boilers=len(boilers),
        measured_per_minute=None,scope='one shared resource-backed bank; output aliases do not multiply supply')


def active_coal_plan(state):
    """Explicit active-source adoption; no obsolete predecessor can be rebuilt."""
    active=merge_entities(state['final_bank_plan']['entities'],state['downstream'])
    extras={k:deepcopy(v)for k,v in state['final_bank_plan'].items()if k not in('ok','reason','entities','ports','required_items')}
    result=_plan(active,state['old_bank_plan']['ports'],**extras)
    result.update(drill=deepcopy(state['old_steam']['bank']['plan']['producer_rows']['left'][0]),
        retired_source=dict(diagnostic=state['depleted_source'],drill=state['old_builder']['coal_plan']['drill'],
                            inactive_ports=state['old_builder']['coal_plan']['ports'],removed_predecessor=state['dead_predecessor']),
        source_adopted_by='base-coal-recovery.json',shared_capacity=deepcopy(state['shared_capacity']))
    return result


DEPLETION_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="base_coal_world_changed"}end
local e=target(x.drill.position,x.drill.name)
if not e or e.force~=f or e.surface~=s or e.unit_number~=x.drill.unit_number then return {ok=false,reason="original_coal_drill_changed"}end
local radius=e.prototype.mining_drill_radius;local amount=0
for _,q in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},{e.position.x+radius,e.position.y+radius}},type="resource"})do
 if q.name~="coal"then return {ok=false,reason="original_coal_patch_mixed_resource"}end;amount=amount+q.amount
end
return {ok=true,tick=game.tick,unit_number=e.unit_number,position=e.position,depleted=amount==0,
 remaining_coal=amount,powered=e.is_connected_to_electric_network()and e.energy>0,status=e.status,
 scope="actual original drill depletion; not a production source"}
'''


class BaseCoalRecovery:
    def __init__(self,game,builder,catalog,perimeter,*,steam=None,outposts=None,reroute=None):
        self.game,self.builder,self.catalog,self.perimeter,self.steam=game,builder,catalog,perimeter,steam
        self.outposts,self.reroute=outposts,reroute
        self.path=Path(game.cfg.runtime_dir)/'base-coal-recovery.json'
        self.state=json.loads(self.path.read_text())if self.path.exists()else{}
        self.last_failure=None

    def _save(self):_atomic_json(self.path,self.state)
    @property
    def verified(self):return self.state.get('phase')=='verified'
    def _docs(self):
        return {k:json.loads((self.path.parent/n).read_text())for k,n in{
            'builder':'factory-builder.json','steam':'steam-expansion.json','reroute':'steam-coal-reroute.json',
            'mining':'mining-outposts.json','board':'pcb-board.json'}.items()}

    def depletion_diagnostic(self,obs):
        try:
            doc=self.state.get('old_builder')or self._docs()['builder'];spec=doc['coal_plan']['drill']
            live=next((q for q in obs['entities']if q['name']==spec['name']and xy(q)==xy(spec)),None)
            if doc['world_id']!=obs['world_id']or not live:return {'ok':False,'reason':'original_coal_source_missing'}
            return self.game.query('local x=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],drill=_bound(live)))+');'+DEPLETION_LUA)
        except(OSError,KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'original_coal_diagnostic_context_invalid','detail':str(exc)}

    def _capacity(self,obs,bank):return diagnose_bank_capacity(self.game,obs,self.catalog,bank)

    def prepare(self,obs):
        try:
            docs=self._docs()
            if any(q.get('world_id')!=obs['world_id']or q.get('catalog_fingerprint',self.catalog.fingerprint)!=self.catalog.fingerprint for q in docs.values()):
                return {'ok':False,'reason':'base_coal_authority_identity_changed'}
            if docs['reroute'].get('phase')!='verified':return {'ok':False,'reason':'base_coal_requires_verified_working_steam_route'}
            depletion=self.depletion_diagnostic(obs)
            if not depletion.get('ok')or not depletion.get('depleted'):return dict(depletion,ok=False,reason='original_coal_source_not_confirmed_depleted')
            steam=docs['steam'];bank=steam['bank'];capacity=self._capacity(obs,bank)
            if not capacity.get('ok')or not capacity.get('nominal_profile_complete')or not capacity.get('all_installed_drop_lanes_verified'):
                return {'ok':False,'reason':'base_coal_shared_producer_not_freshly_verified','evidence':capacity}
            actual={(q['name'],*xy(q)):q for q in obs['entities']}
            source=docs['reroute']['source_port'];live_source=actual.get(('transport-belt',*xy(source)))
            if(not live_source or live_source.get('unit_number')!=source['unit_number']or live_source.get('direction')!=source['facing']
                    or xy(source)!=xy(bank['plan']['ports'][0])):
                return {'ok':False,'reason':'base_coal_exact_working_source_port_changed'}
            if set(live_source.get('belt_inventory',{}))-{'coal'}or live_source.get('belt_inventory',{}).get('coal',0)<=0:
                return {'ok':False,'reason':'base_coal_working_source_waiting_for_pure_coal_stock'}
            oldpower=docs['builder']['power_plan'];boilers=[q for q in oldpower['entities']+steam['core']['entities']if q['name']=='boiler']
            budget=coal_budget(self.catalog,boilers,capacity['active_collector_ceiling_per_minute'])
            if budget['spare_potential_per_minute']<0:return {'ok':False,'reason':'expand_shared_power_coal_before_fork','evidence':budget}
            if docs['board']['blocks'].get('steam_expansion')!=dict(steam['plan'],reserved_area=steam['core']['reserved_area']):
                return {'ok':False,'reason':'base_coal_steam_authorities_disagree'}
            candidates=[q for q in docs['mining']['banks']if q['plan']['ports']==bank['plan']['ports']and q['plan']['resource']=='coal']
            if len(candidates)!=1 or candidates[0]['plan']!=bank['plan']:return {'ok':False,'reason':'base_coal_canonical_bank_changed'}
            # Preserve the original W-facing final turn; the new feed becomes
            # its explicit same-coal inline input while the depleted route stays.
            oldport=next(q for q in oldpower['ports']if q.get('item')=='coal')
            oldrows=docs['builder']['coal_plan']['entities'];dx,dy={0:(0,-1),4:(1,0),8:(0,1),12:(-1,0)}[oldport['facing']]
            # A declared older turn can receive a new inline coal input. Prefer
            # its vertical approach over forcing a new W approach into shoreline.
            joins=[(i,q)for i,q in enumerate(oldrows)if i and q['name']=='transport-belt'and oldrows[i-1]['name']=='transport-belt'
                   and q['direction']!=oldport['facing']and q['direction']!=oldrows[i-1]['direction']]
            join_index,join=joins[-1]if joins else next(((i,q)for i,q in enumerate(oldrows)if q['name']=='transport-belt'and xy(q)==(xy(oldport)[0]-dx,xy(oldport)[1]-dy)and q['direction']==oldport['facing']),(None,None))
            if not join:return {'ok':False,'reason':'base_coal_owned_final_inline_merge_missing'}
            downstream=oldrows[join_index:]
            predecessor=oldrows[join_index-1]
            pred_live=actual.get(('transport-belt',*xy(predecessor)))
            if(not pred_live or pred_live.get('direction')!=predecessor['direction']or pred_live.get('belt_inventory',{})):
                return {'ok':False,'reason':'base_coal_dead_predecessor_must_be_exact_empty_belt'}
            target=port('coal',*xy(join),join['direction'])
            target_live=actual.get(('transport-belt',*xy(join)))
            if not target_live:return {'ok':False,'reason':'base_coal_final_merge_unit_missing'}
            headers=power_forks(steam['plan'],bank['plan']['ports'][0],steam['core']['ports'][0],join)
            areas=[q['reserved_area']for q in docs['board']['blocks'].values()if q.get('reserved_area')]
            occupied=self.builder._occupied_by_plan(obs['entities']);legal=[]
            for h in headers:
                sx,sy=xy(h['splitter']);hw,hh=(1,.5)if h['splitter']['direction']%8==0 else(.5,1)
                if any(overlap([[sx-hw,sy-hh],[sx+hw,sy+hh]],area)for area in areas):continue
                x,y=xy(h['outlet']);vx,vy=VECTORS[h['outlet']['direction']]
                if {(x,y),(x+vx,y+vy)}&occupied:continue
                if any(any(a[0][0]<px+.49 and a[1][0]>px-.49 and a[0][1]<py+.49 and a[1][1]>py-.49 for a in areas)for px,py in[(x,y),(x+vx,y+vy)]):continue
                legal.append(h)
            headers=legal
            clock=PlanningBudget(self.game);failures=[]
            for header in headers[:6]:
                if clock.expired():return clock.failure(stage='base_coal_branch',failures=failures[-4:])
                old=actual.get(('transport-belt',*xy(header['old'])))
                if not old or old['direction']!=header['old']['direction']:continue
                outlet=header['outlet'];new=[header['splitter'],outlet];ignored=(old['unit_number'],pred_live['unit_number'])
                check=preflight(clock.game,self.builder,obs,new,ignored_units=ignored)
                if not check.get('ok'):failures.append(dict(stage='fork',position=header['old']['position'],reason=check.get('reason')));continue
                source=port('coal',*xy(outlet),outlet['direction'],'output')
                adapter=SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan,
                    can_place=lambda es:preflight(clock.game,self.builder,obs,es,ignored_units=ignored))
                virtual=dict(obs,entities=[q for q in obs['entities']if q.get('unit_number')not in ignored])
                preserved_power=preserved_pipe_specs(oldpower['entities'],actual)
                reserved=merge_entities([q for q in steam['plan']['entities']if xy(q)!=xy(header['old'])],preserved_power,downstream,new)
                route=plan_connection(clock.game,adapter,virtual,source,target,reserved,ignored_units=ignored)
                if not route.get('ok'):failures.append(dict(stage='branch',reason=route.get('reason'),source=source['position'],destination=target['position']));continue
                added=merge_entities(new,[q for q in route['entities']if xy(q)not in{xy(source),xy(target)}])
                final=replace_header(steam['plan'],header['old'],added)
                final['underground_pairs']=steam['plan'].get('underground_pairs',[])+route['underground_pairs']
                whole=preflight(clock.game,self.builder,obs,merge_entities(final['entities'],preserved_power,downstream),ignored_units=ignored)
                if not whole.get('ok'):failures.append(dict(stage='whole',reason=whole.get('reason'),position=whole.get('position')));continue
                protected=[]
                for spec in merge_entities(steam['plan']['entities'],oldpower['entities'],oldrows):
                    if spec['name']==predecessor['name']and xy(spec)==xy(predecessor):continue
                    if spec['name']=='transport-belt'and xy(spec)==xy(header['old']):continue
                    live=actual.get((spec['name'],*xy(spec)))
                    if not live:return {'ok':False,'reason':'base_coal_preserved_asset_missing','position':spec['position']}
                    protected.append(_bound(live))
                final_bank=replace_header(bank['plan'],header['old'],added)
                final_bank['underground_pairs']=bank['plan'].get('underground_pairs',[])+route['underground_pairs']
                return dict(ok=True,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='stock',
                    old=_bound(old),old_spec=header['old'],dead_predecessor=_bound(pred_live),recoveries=[_bound(pred_live),_bound(old)],recovery_index=0,
                    old_builder=deepcopy(docs['builder']),new_entities=added,splitter=header['splitter'],outlet=outlet,
                    through=_bound(actual[('transport-belt',*xy(header['through']))]),join=_bound(target_live),downstream=downstream,protected=protected,bindings={},
                    plan=_plan(added,[source,target],underground_pairs=route['underground_pairs'],defense_scope_id='base-coal-recovery',
                               clear_obstacles=whole['clear_obstacles'],clearance_world_id=obs['world_id']),
                    old_steam=deepcopy(steam),final_steam_plan=final,old_bank_plan=bank['plan'],
                    final_bank_plan=final_bank,old_reroute=deepcopy(docs['reroute']),
                    shared_capacity=budget,source_capacity=capacity,depleted_source=depletion,source_port=source,
                    required_stock=dict(Counter(q['name']for q in added)),prepared_tick=obs['tick'],recovery_started=False,samples=[])
            return {'ok':False,'reason':'no_bounded_shared_power_coal_fork','failures':failures[-6:]}
        except(OSError,KeyError,TypeError,ValueError)as exc:return {'ok':False,'reason':'base_coal_prepare_context_invalid','detail':str(exc)}

    def _check(self,obs):
        s=self.state;actual={(q['name'],*xy(q)):q for q in obs['entities']}
        if s.get('world_id')!=obs['world_id']or s.get('catalog_fingerprint')!=self.catalog.fingerprint:return {'status':'blocked','reason':'base_coal_world_or_catalog_changed'}
        for spec in s['protected']:
            live=actual.get((spec['name'],*xy(spec)))
            if not live or live.get('unit_number')!=spec['unit_number']or live.get('direction')!=spec['direction']:
                return {'status':'blocked','reason':'base_coal_retained_unit_changed','position':spec['position']}
        for spec in s['new_entities']:
            live=actual.get((spec['name'],*xy(spec)));prior=s['bindings'].get(_key(spec))
            if prior and(not live or live.get('unit_number')!=prior):return {'status':'blocked','reason':'base_coal_paid_branch_unit_changed'}
            if live:
                if live.get('direction')!=spec['direction']or type(live.get('unit_number'))is not int:return {'status':'blocked','reason':'base_coal_branch_configuration_changed'}
                s['bindings'][_key(spec)]=live['unit_number']
        return None

    def _sample(self,obs):
        s=self.state
        capacity=self._capacity(obs,self._docs()['steam']['bank'])
        if not capacity.get('ok')or capacity.get('active_collector_ceiling_per_minute',0)<s['shared_capacity']['boiler_max_coal_per_minute']:
            return {'ok':False,'reason':'base_coal_shared_source_no_longer_resource_backed','evidence':capacity}
        if not underground_proof(obs,s['plan'])or not surface_pipe_proof(self.game,obs,s['plan']).get('ok'):return None
        if any(_key(q)not in s['bindings']for q in s['new_entities']):return None
        args=dict(world_id=obs['world_id'],splitter=dict(s['splitter'],unit_number=s['bindings'][_key(s['splitter'])]),
            rows=[dict(s['outlet'],unit_number=s['bindings'][_key(s['outlet'])]),s['through'],s['join']],
            machines=[q for q in s['protected']if q['name']in('boiler','steam-engine')])
        return self.game.query('local x=helpers.json_to_table('+_payload(args)+');'+COAL_HEALTH_LUA)

    def _publish(self,obs):
        if self.state.get('publication_intent'):return self._finish_publication()
        docs=self._docs();s=self.state;old=s['old_spec'];new=s['new_entities']
        if _hash(docs['steam']['plan'])not in(_hash(s['old_steam']['plan']),_hash(s['final_steam_plan'])):
            return {'status':'blocked','reason':'base_coal_publish_steam_revision_changed'}
        banks=[q for q in docs['mining']['banks']if q['plan']['ports']==s['old_bank_plan']['ports']and q['plan'].get('resource')=='coal']
        if len(banks)!=1 or _hash(banks[0]['plan'])not in(_hash(s['old_bank_plan']),_hash(s['final_bank_plan'])):
            return {'status':'blocked','reason':'base_coal_publish_bank_revision_changed'}
        docs['steam']['plan']=deepcopy(s['final_steam_plan']);docs['steam']['bank']['plan']=deepcopy(s['final_bank_plan'])
        banks[0]['plan']=deepcopy(s['final_bank_plan'])
        docs['board']['blocks']['steam_expansion']=dict(s['final_steam_plan'],reserved_area=s['old_steam']['core']['reserved_area'])
        for block in docs['board']['blocks'].values():
            if block.get('resource')=='coal'and block.get('ports')==s['old_bank_plan']['ports']:
                block.update(deepcopy(s['final_bank_plan']))
        rr=docs['reroute'];before=s['old_reroute']
        if _hash(rr.get('final_plan'))not in(_hash(before['final_plan']),_hash(s['final_steam_plan'])):
            return {'status':'blocked','reason':'base_coal_publish_reroute_revision_changed'}
        rr['final_plan']=deepcopy(s['final_steam_plan']);rr['final_bank_plan']=deepcopy(s['final_bank_plan'])
        rr['route']=replace_header(before['route'],old,new)
        rr['route']['underground_pairs']=s['final_steam_plan']['underground_pairs']
        rr['added']=[q for q in before['added']if not(q['name']==old['name']and xy(q)==xy(old))]+[q for q in new if q['name']!='splitter']
        rr['protected']=[q for q in before['protected']if q['unit_number']!=s['old']['unit_number']]
        rr['new_units']=[q for q in before['new_units']if q['unit_number']!=s['old']['unit_number']]
        actual={(q['name'],*xy(q)):q for q in obs['entities']}
        rr['new_units'] += [_bound(actual[(q['name'],*xy(q))])for q in new]
        rr['base_coal_adoption']={'tick':obs['tick'],'old_unit':s['old']['unit_number'],'shared_capacity':s['shared_capacity']}
        if _hash(docs['builder']['coal_plan'])!=_hash(s['old_builder']['coal_plan']):
            return {'status':'blocked','reason':'base_coal_publish_original_source_revision_changed'}
        docs['builder']['coal_plan']=active_coal_plan(s)
        names={'builder':'factory-builder.json','steam':'steam-expansion.json','mining':'mining-outposts.json','board':'pcb-board.json','reroute':'steam-coal-reroute.json'}
        s['publication_intent']={name:dict(old_hash=_hash(json.loads((self.path.parent/name).read_text())),new_hash=_hash(docs[key]),value=docs[key])for key,name in names.items()}
        self._save()
        return self._finish_publication()

    def _finish_publication(self):
        for name,row in self.state['publication_intent'].items():
            current=json.loads((self.path.parent/name).read_text())
            if _hash(current)not in(row['old_hash'],row['new_hash']):return {'status':'blocked','reason':'base_coal_publication_compare_and_swap_failed','document':name}
        for name,row in self.state['publication_intent'].items():
            if _hash(json.loads((self.path.parent/name).read_text()))==row['old_hash']:_atomic_json(self.path.parent/name,row['value'])
        values={name:row['value']for name,row in self.state['publication_intent'].items()}
        self.builder.state=values['factory-builder.json']
        for component,name in[(self.steam,'steam-expansion.json'),(self.outposts,'mining-outposts.json'),(self.reroute,'steam-coal-reroute.json')]:
            if component is not None:component.state=values[name]
        self.state['published']=True;self._save()
        return None

    def step(self,obs):
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return dict(ready,status='blocked')
            self.state=ready;self._save()
        s=self.state;issue=self._check(obs)
        if issue:return issue
        self._save()
        board=self._docs()['board'];registered=board['blocks'].get('base_coal_recovery')
        if registered not in(None,s['plan']):return {'status':'blocked','reason':'base_coal_registered_plan_changed'}
        if registered is None:board['blocks']['base_coal_recovery']=s['plan'];_atomic_json(self.path.parent/'pcb-board.json',board)
        if s['phase']=='stock':
            diagnostic=self.depletion_diagnostic(obs)
            if not diagnostic.get('ok')or not diagnostic.get('depleted'):return {'status':'blocked','reason':'base_coal_original_source_depletion_changed_before_cost','evidence':diagnostic}
            pred=next((q for q in obs['entities']if q['name']=='transport-belt'and xy(q)==xy(s['dead_predecessor'])),None)
            if not pred or pred.get('unit_number')!=s['dead_predecessor']['unit_number']or pred.get('belt_inventory',{}):
                return {'status':'blocked','reason':'base_coal_dead_predecessor_changed_before_cost'}
            for item,count in s['required_stock'].items():
                if obs.get('inventory',{}).get(item,0)<count:return self.builder.bootstrap.ensure_item(obs,item,count)
            prebuild=[q for q in s['new_entities']if q['name']!='splitter']
            result=self.builder.ensure_plan(obs,_plan(prebuild,[],clear_obstacles=s['plan']['clear_obstacles'],clearance_world_id=obs['world_id']))
            if result.get('status')!='succeeded':return result
            s['phase']='cutover';self._save()
        if s['phase']=='cutover':
            if obs.get('inventory',{}).get('splitter',0)<1:return self.builder.bootstrap.ensure_item(obs,'splitter',1)
            check=preflight(self.game,self.builder,obs,s['new_entities'],ignored_units=tuple(q['unit_number']for q in s['recoveries']))
            if not check.get('ok'):return dict(check,status='blocked')
            while s['recovery_index']<len(s['recoveries']):
                wanted=s['recoveries'][s['recovery_index']]
                live=next((q for q in obs['entities']if q['name']=='transport-belt'and xy(q)==xy(wanted)),None)
                if not live:
                    if wanted['unit_number']not in s.get('started_recoveries',[]):return {'status':'blocked','reason':'base_coal_unstarted_corner_missing'}
                    s['recovery_index']+=1;self._save();continue
                if live['unit_number']!=wanted['unit_number']or live['direction']!=wanted['direction']:return {'status':'blocked','reason':'base_coal_cutover_unit_changed'}
                if wanted['unit_number']==s['dead_predecessor']['unit_number']and live.get('belt_inventory',{}):return {'status':'blocked','reason':'base_coal_dead_predecessor_no_longer_empty'}
                capacity=self._capacity(obs,self._docs()['steam']['bank'])
                if not capacity.get('ok')or capacity.get('active_collector_ceiling_per_minute',0)<s['shared_capacity']['boiler_max_coal_per_minute']:
                    return {'status':'blocked','reason':'base_coal_shared_supply_changed_before_cutover','evidence':capacity}
                if wanted['unit_number']not in s.setdefault('started_recoveries',[]):s['started_recoveries'].append(wanted['unit_number'])
                self._save()
                return dict(type='mine',name='transport-belt',position=live['position'],count=1,expected_entity_unit=live['unit_number'],
                            expected_entity_world_id=obs['world_id'],reason='recover one exact stocked shared coal header for both automatic boiler feeds')
            s['phase']='build';self._save()
        if s['phase']=='build':
            result=self.builder.ensure_plan(obs,s['plan'])
            if result.get('status')!='succeeded':return result
            s['phase']='commission';self._save()
        sample=self._sample(obs)
        if not sample or not sample.get('ok'):return {'status':'waiting','reason':'waiting_for_both_automatic_coal_feeds_and_four_loaded_engines','evidence':sample}
        history=s['samples']
        if history and not 0<sample['tick']-history[-1]['tick']<=600:history.clear()
        history.append(sample)
        while len(history)>2 and history[1]['tick']<=sample['tick']-1800:history.pop(0)
        self._save()
        if len(history)<2 or history[-1]['tick']-history[0]['tick']<1800:return {'status':'waiting','reason':'sampling_shared_coal_and_four_engines_for_thirty_game_seconds'}
        if not s.get('published'):
            result=self._publish(obs)
            if result:return result
        s['phase']='verified';self._save()
        return {'status':'succeeded','reason':'both boilers automatically share one paid resource-backed coal source','evidence':self.proof(obs)}

    def proof(self,obs):
        if not self.state or not self.state.get('published')or self._check(obs):return None
        sample=self._sample(obs);history=self.state['samples']
        if not sample or not sample.get('ok')or len(history)<2 or history[-1]['tick']-history[0]['tick']<1800 or not 0<=sample['tick']-history[-1]['tick']<=600:return None
        return dict(tick=sample['tick'],from_tick=history[0]['tick'],to_tick=history[-1]['tick'],native=sample,
            shared_capacity=self.state['shared_capacity'],depleted_original_source=self.state['depleted_source'],
            scope='actual coal on both preserved boiler feeds and four-engine generation over sampled30s; no full-load or sustained source-rate certification')


COAL_HEALTH_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="base_coal_world_changed"}end
local split=target(x.splitter.position,"splitter")
if not split or split.unit_number~=x.splitter.unit_number or split.force~=f or split.surface~=s or split.direction~=x.splitter.direction
 or split.splitter_input_priority~=x.splitter.splitter_input_priority or split.splitter_output_priority~="none"then return {ok=false,reason="base_coal_native_priority_changed"}end
local counts={};for _,q in ipairs(x.rows)do local e=target(q.position,"transport-belt")
 if not e or e.unit_number~=q.unit_number or e.force~=f or e.surface~=s or e.direction~=q.direction then return {ok=false,reason="base_coal_feed_unit_changed"}end
 local n=0;for lane=1,2 do for _,row in pairs(e.get_transport_line(lane).get_contents())do
  if row.name~="coal"or(row.quality and row.quality~="normal")then return {ok=false,reason="base_coal_feed_contaminated"}end;n=n+row.count
 end end;counts[#counts+1]=n;if n==0 then return {ok=false,reason="base_coal_feed_waiting_for_real_coal"}end
end
local engines={};local boilers={}
for _,q in ipairs(x.machines)do local e=target(q.position,q.name)
 if not e or e.unit_number~=q.unit_number or e.force~=f or e.surface~=s then return {ok=false,reason="base_coal_plant_unit_changed"}end
 local fluids=e.get_fluid_contents()
 if e.name=="boiler"then local inv=e.get_fuel_inventory();local count=inv and inv.get_item_count{name="coal",quality="normal"}or 0
  if count<=0 and(not e.burner or e.burner.remaining_burning_fuel<=0)or(fluids.water or 0)<=0 then return {ok=false,reason="base_coal_boiler_waiting_for_automatic_fuel"}end
  boilers[#boilers+1]={unit_number=e.unit_number,coal=count,water=fluids.water}
 else
  if not e.is_connected_to_electric_network()or(fluids.steam or 0)<=0 or e.energy_generated_last_tick<=0 then return {ok=false,reason="base_coal_four_engines_waiting_for_steam_or_load"}end
  engines[#engines+1]={unit_number=e.unit_number,generated_joules_last_tick=e.energy_generated_last_tick,steam=fluids.steam}
 end
end
if #boilers~=2 or #engines~=4 then return {ok=false,reason="base_coal_expected_two_boilers_four_engines"}end
return {ok=true,tick=game.tick,feed_coal=counts,boilers=boilers,engines=engines}
'''
