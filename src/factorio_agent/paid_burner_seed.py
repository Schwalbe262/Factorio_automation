"""One normal eight-coal startup allowance per proven paid burner birth."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json


STATE_FILE='paid-burner-seeds.json'
NAMES={'coal_drill':'burner-mining-drill','boiler':'boiler'}
BUDGET=8
MAX_AUDIT_BYTES=64*1024*1024


def _sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _read(path):return json.loads(path.read_text(encoding='utf-8'))if path.exists()else{}
def _position(p):return isinstance(p,dict)and set(p)=={'x','y'}and all(type(v)in(int,float)and math.isfinite(v)for v in p.values())
def _same(spec,row):return spec.get('name')==row.get('name')and spec.get('position')==row.get('position')


def _authority(root,world,catalog,key,spec):
    owner=_read(root/'factory-builder.json')
    if owner.get('world_id')!=world or owner.get('catalog_fingerprint')!=catalog:raise ValueError('burner seed builder world/catalog changed')
    expected=(owner.get('coal_plan',{}).get('drill')if key=='coal_drill'else
        next((e for e in owner.get('power_plan',{}).get('entities',[])if e.get('name')=='boiler'),None))
    if not expected or not _same(expected,spec)or expected.get('direction',0)!=spec.get('direction',0):
        raise ValueError('burner seed target is not the registered power burner')
    return owner


def paid_birth(root,world,spec,unit,tick):
    """Stream only relevant paired receipts; unbound actions.jsonl is not authority."""
    ledger=_read(root/'jobs.json');path=root/'job-events.jsonl'
    if ledger.get('world_id')!=world or ledger.get('rollback_tick')is not None:raise ValueError('burner birth ledger world/rollback changed')
    if not path.exists()or path.stat().st_size>MAX_AUDIT_BYTES:raise ValueError('bounded burner birth journal required')
    pending={};births=[];latest=None;fuel={};seed={}
    with path.open('rb')as stream:
        for raw in stream:
            try:row=json.loads(raw)
            except ValueError:raise ValueError('burner birth journal is incomplete')
            key=row.get('key');event=row.get('event');job=ledger.get('jobs',{}).get(key,{})
            if event=='action_intent':
                action=row.get('action',{})
                relevant=_same(spec,action)and(action.get('type')=='build'or(action.get('type')=='insert'and action.get('item')=='coal'and action.get('inventory')=='fuel'))
                if relevant:
                    if key in pending:raise ValueError('unresolved burner action intent')
                    pending[key]=(row,raw)
                continue
            if event!='action_result'or key not in pending:continue
            intent,intent_raw=pending.pop(key);action=intent['action'];result=row.get('result',{})
            when=intent.get('tick')
            if(job.get('world_id')!=world or type(when)is not int or not 0<=when<=tick or row.get('tick')!=when):
                raise ValueError('burner paid receipt world/tick changed')
            if result.get('ok')is not True:
                reason=result.get('reason','')
                if reason in {'placement_blocked','target_missing','out_of_reach','agent_dead','existing_direction_mismatch'}or reason.startswith('missing_item:'):continue
                raise ValueError('burner receipt outcome is not known successful')
            if action['type']=='build':
                if result.get('reused'):continue
                born=result.get('unit_number')
                if result.get('status')!='succeeded'or type(born)is not int or born<=0:raise ValueError('burner build receipt lacks paid native unit')
                latest=born
                births.append(dict(id=hashlib.sha256(intent_raw+b'\0'+raw).hexdigest(),unit_number=born,tick=when,job_key=key,
                    action_sha256=hashlib.sha256(intent_raw).hexdigest(),result_sha256=hashlib.sha256(raw).hexdigest()))
            else:
                moved=result.get('moved')
                if type(moved)is not int or not 0<=moved<=action.get('count',0):raise ValueError('burner fuel receipt count unknown')
                if latest is not None and moved:
                    fuel[latest]=fuel.get(latest,0)+moved
                    if action.get('reason')=='one-time power bootstrap fuel':seed[latest]=seed.get(latest,0)+moved
    if pending:raise ValueError('unresolved burner action outcome')
    matches=[b for b in births if b['unit_number']==unit]
    if len(matches)!=1 or latest!=unit:raise ValueError('current burner has no unique latest paid non-reused build receipt')
    birth=matches[0]
    return dict(birth=birth,already_fueled=fuel.get(unit,0)>0,legacy_seed_moved=min(BUDGET,seed.get(unit,0)),
        proven_replacement=any(b['unit_number']!=unit and b['tick']<birth['tick']and fuel.get(b['unit_number'],0)>0 for b in births))


class PaidBurnerSeed:
    def __init__(self,game,catalog):
        self.game,self.catalog=game,catalog;self.root=Path(game.cfg.runtime_dir);self.path=self.root/STATE_FILE
        self.state=_read(self.path)

    def prepare(self,obs,key,spec,count=8):
        try:
            self.state=_read(self.path)
            if key not in NAMES or spec.get('name')!=NAMES[key]or count!=BUDGET or not _position(spec.get('position')):
                raise ValueError('only registered power burners have an eight-coal startup budget')
            world=obs['world_id'];tick=obs['tick'];fingerprint=self.catalog.fingerprint
            if type(tick)is not int or tick<0:raise ValueError('burner seed requires native tick')
            owner=_authority(self.root,world,fingerprint,key,spec)
            if self.state and(self.state.get('schema_version')!=1 or self.state.get('world_id')!=world
                    or self.state.get('catalog_fingerprint')!=fingerprint or tick<self.state.get('last_tick',0)):
                raise ValueError('burner seed durable world/catalog/rollback guard')
            actual=next((e for e in obs.get('entities',[])if _same(spec,e)),None)
            if not actual or type(actual.get('unit_number'))is not int or actual['unit_number']<1:raise ValueError('native burner identity missing')
            if actual.get('direction',0)!=spec.get('direction',0):raise ValueError('native burner direction changed')
            identity=str(actual['unit_number']);entry=deepcopy(self.state.get('entries',{}).get(identity))
            if entry and(not _same(spec,entry)or entry.get('key')!=key):raise ValueError('native burner unit was rebound')
            if entry and(entry.get('intent')or entry.get('phase')in('executing','outcome_unknown')):
                raise ValueError('paid_burner_seed_outcome_unknown_reconcile_required')
            warm=actual.get('inventory',{}).get('coal',0)>0 or actual.get('remaining_burning_fuel',0)>0
            if not entry:
                entry=dict(key=key,name=spec['name'],position=deepcopy(spec['position']),direction=spec.get('direction',0),
                    unit_number=actual['unit_number'],budget=BUDGET,moved=0,sequence=0,phase='ready')
                if warm:entry['phase']='observed'
                else:
                    audit=paid_birth(self.root,world,spec,actual['unit_number'],tick);entry['birth']=audit['birth']
                    prior=any(e.get('unit_number')!=actual['unit_number']and _same(spec,e)and e.get('phase')in('observed','complete')for e in self.state.get('entries',{}).values())
                    legacy=owner.get('seeds',{}).get(key,{})
                    if audit['already_fueled']:
                        entry.update(phase='observed',moved=audit['legacy_seed_moved'],imported_legacy_fuel=True)
                    elif legacy.get('observed')and not(audit['proven_replacement']or prior):
                        raise ValueError('legacy observed burner has no proven fresh replacement birth')
            if warm and entry.get('moved',0)==0:entry['phase']='observed'
            if entry.get('moved',0)>=BUDGET:entry['phase']='complete'
            return dict(ok=True,entry=entry,world_id=world,catalog_fingerprint=fingerprint,tick=tick,
                eligible=entry['phase']=='ready',remaining=BUDGET-entry['moved'],warm=warm)
        except (KeyError,TypeError,ValueError,OSError)as exc:return dict(ok=False,reason=str(exc))

    def step(self,obs,key,spec,count=8,*,bootstrap,move):
        prepared=self.prepare(obs,key,spec,count)
        if not prepared.get('ok'):return {'status':'blocked','reason':prepared['reason']}
        if not self.state:self.state=dict(schema_version=1,world_id=prepared['world_id'],catalog_fingerprint=prepared['catalog_fingerprint'],last_tick=prepared['tick'],entries={})
        self.state['last_tick']=prepared['tick'];entry=prepared['entry'];self.state['entries'][str(entry['unit_number'])]=entry
        _atomic_json(self.path,self.state)
        if not prepared['eligible']:
            return None if prepared['warm']else dict(status='waiting',reason='paid_burner_seed_closed_requires_continuous_fuel',evidence={'unit_number':entry['unit_number'],'seed_moved':entry['moved'],'seed_budget':BUDGET})
        remaining=prepared['remaining']
        if obs.get('inventory',{}).get('coal',0)<remaining:return bootstrap.ensure_item(obs,'coal',remaining)
        movement=move(obs,spec['position'])
        if movement:return movement
        entry['sequence']+=1
        action=dict(type='seed_paid_burner',key=key,name=entry['name'],position=deepcopy(entry['position']),direction=entry['direction'],
            item='coal',inventory='fuel',count=remaining,expected_entity_unit=entry['unit_number'],expected_entity_world_id=self.state['world_id'],
            expected_catalog_fingerprint=self.state['catalog_fingerprint'],birth_receipt_id=entry['birth']['id'],expected_moved=entry['moved'],
            seed_operation_id=_sha([entry['birth']['id'],entry['unit_number'],entry['sequence'],entry['moved'],remaining]),reason='one paid burner birth startup, total eight normal coal')
        entry['intent']=deepcopy(action);_atomic_json(self.path,self.state);return action


SEED_LUA=r'''
local x=helpers.json_to_table(PAYLOAD)
local function reject(reason)return {ok=false,status="blocked",reason=reason,moved=0,pre_effect=true}end
if not d or d.world_id~=x.expected_entity_world_id then return reject("seed_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s then return reject("seed_actor_changed")end
local e=target(x.position,x.name)
if not e or not e.valid or e.unit_number~=x.expected_entity_unit or e.force~=f or e.surface~=s
 or e.position.x~=x.position.x or e.position.y~=x.position.y or e.direction~=x.direction or not e.burner then return reject("seed_burner_identity_changed")end
if x.character_backend and not a.can_reach_entity(e)then return reject("seed_out_of_reach")end
if game.tick<x.birth_tick then return reject("seed_birth_tick_rollback")end
local main=a.get_main_inventory();local fuel=e.get_fuel_inventory()
if not main or not fuel then return reject("seed_inventory_missing")end
d.paid_burner_seeds=d.paid_burner_seeds or{}
local id=tostring(e.unit_number);local row=d.paid_burner_seeds[id]
if row and(row.world_id~=x.expected_entity_world_id or row.catalog_fingerprint~=x.expected_catalog_fingerprint
 or row.birth_receipt_id~=x.birth_receipt_id or row.tick>game.tick)then return reject("seed_native_ledger_changed_or_rollback")end
if row and row.operations[x.seed_operation_id]then return reject("seed_operation_already_recorded_requires_reconciliation")end
if row and row.moved~=x.expected_moved then return reject("seed_native_cumulative_changed")end
if not row and x.expected_moved~=0 then return reject("seed_native_receipt_missing")end
if row and row.closed then return reject("seed_native_allowance_closed")end
if (row and row.moved or 0)+x.count>8 then return reject("seed_budget_exceeded")end
if not row then row={world_id=x.expected_entity_world_id,catalog_fingerprint=x.expected_catalog_fingerprint,birth_receipt_id=x.birth_receipt_id,moved=0,operations={},tick=game.tick};d.paid_burner_seeds[id]=row end
if row.moved==0 and(fuel.get_item_count{name="coal",quality="normal"}>0 or e.burner.remaining_burning_fuel>0)then
 row.closed=true;row.tick=game.tick
 return {ok=true,status="succeeded",already_fueled=true,moved=0,total_moved=0,unit_number=e.unit_number,world_id=d.world_id,operation_id=x.seed_operation_id,birth_receipt_id=x.birth_receipt_id,tick=game.tick}
end
row.operations[x.seed_operation_id]={phase="intent",moved=0}
local moved=0
for i=1,#main do
 if moved>=x.count then break end
 local stack=main[i]
 if stack.valid_for_read and stack.name=="coal"and stack.prototype.type=="item"and stack.quality.name=="normal"then
  local n=math.min(stack.count,x.count-moved);local inserted=fuel.insert{name="coal",quality="normal",count=n}
  if inserted>0 then stack.count=stack.count-inserted;moved=moved+inserted;row.moved=row.moved+inserted;row.operations[x.seed_operation_id].moved=moved end
 end
end
row.operations[x.seed_operation_id].phase="complete";row.tick=game.tick;row.closed=row.moved>=8
return {ok=true,status=moved>0 and"succeeded"or"waiting",moved=moved,total_moved=row.moved,unit_number=e.unit_number,
 world_id=d.world_id,operation_id=x.seed_operation_id,birth_receipt_id=x.birth_receipt_id,tick=game.tick}
'''


def execute_paid_burner_seed(game,action):
    allowed={'type','key','name','position','direction','item','inventory','count','expected_entity_unit','expected_entity_world_id',
        'expected_catalog_fingerprint','birth_receipt_id','expected_moved','seed_operation_id','reason'}
    if(not isinstance(action,dict)or set(action)-allowed or action.get('type')!='seed_paid_burner'or action.get('key')not in NAMES
            or action.get('name')!=NAMES[action['key']]or not _position(action.get('position'))or type(action.get('direction'))is not int or action['direction']not in(0,4,8,12)
            or action.get('item')!='coal'or action.get('inventory')!='fuel'or type(action.get('count'))is not int or not 1<=action['count']<=BUDGET
            or type(action.get('expected_moved'))is not int or not 0<=action['expected_moved']<BUDGET
            or type(action.get('expected_entity_unit'))is not int or action['expected_entity_unit']<1
            or any(not isinstance(action.get(k),str)or not re.fullmatch('[a-f0-9]{64}',action[k])for k in ('birth_receipt_id','seed_operation_id'))
            or not isinstance(action.get('expected_entity_world_id'),str)or not action['expected_entity_world_id']
            or not isinstance(action.get('expected_catalog_fingerprint'),str)or not action['expected_catalog_fingerprint']):raise ValueError('invalid paid burner seed envelope')
    root=Path(game.cfg.runtime_dir);path=root/STATE_FILE;state=_read(path)
    from factorio_agent.core.world_catalog import WorldCatalog
    fingerprint=WorldCatalog.from_dict(_read(root/'catalog.json')).fingerprint
    if(state.get('world_id')!=action.get('expected_entity_world_id')or state.get('catalog_fingerprint')!=fingerprint
            or action.get('expected_catalog_fingerprint')!=fingerprint or getattr(game,'expected_world',state['world_id'])!=state['world_id']):raise ValueError('paid seed world/catalog authority changed')
    entry=state.get('entries',{}).get(str(action['expected_entity_unit']))
    if not entry or entry.get('phase')!='ready'or entry.get('intent')!=action:raise ValueError('paid seed has no exact unexecuted intent')
    _authority(root,state['world_id'],fingerprint,action['key'],action)
    audit=paid_birth(root,state['world_id'],action,action['expected_entity_unit'],state['last_tick'])
    if audit['birth']!=entry['birth']or audit['already_fueled']:raise ValueError('paid seed birth or prior fuel authority changed')
    if action['expected_moved']!=entry['moved']or action['count']!=BUDGET-entry['moved']:raise ValueError('paid seed cumulative budget changed')
    entry['phase']='executing';_atomic_json(path,state)
    payload={**action,'birth_tick':entry['birth']['tick'],'character_backend':getattr(game,'backend','assisted')=='character'}
    encoded=json.dumps(json.dumps(payload,separators=(',',':')))
    try:
        result=game.query(SEED_LUA.replace('PAYLOAD',encoded))
        if result.get('ok')is False and result.get('pre_effect')is True and result.get('moved')==0:
            entry.update(phase='ready',last_failure=result.get('reason'));entry.pop('intent');_atomic_json(path,state);return result
        moved=result.get('moved');total=result.get('total_moved')
        if(result.get('ok')is not True or type(moved)is not int or not 0<=moved<=action['count']
                or type(total)is not int or total!=entry['moved']+moved or not 0<=total<=BUDGET
                or result.get('status')!=('succeeded'if moved or result.get('already_fueled')is True else'waiting')
                or ('already_fueled'in result and (result['already_fueled']is not True or moved!=0 or total!=0))
                or type(result.get('unit_number'))is not int or result['unit_number']!=action['expected_entity_unit']or result.get('world_id')!=state['world_id']
                or result.get('operation_id')!=action['seed_operation_id']or result.get('birth_receipt_id')!=action['birth_receipt_id']
                or type(result.get('tick'))is not int or result['tick']<state['last_tick']):raise ValueError('unexpected paid seed receipt')
        entry.update(moved=total,phase='observed'if result.get('already_fueled')else'complete'if total==BUDGET else'ready',last_receipt=deepcopy(result))
        entry.pop('intent');state['last_tick']=result['tick'];_atomic_json(path,state);return result
    except Exception as exc:
        entry.update(phase='outcome_unknown',last_failure='paid_burner_seed_outcome_unknown');_atomic_json(path,state)
        raise RuntimeError('paid_burner_seed_outcome_unknown: reconciliation required')from exc
