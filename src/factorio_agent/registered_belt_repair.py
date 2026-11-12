"""Paid recovery of exact Advanced Circuit belt losses, preserving all debris."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .infrastructure_repair import _hash,_key,_read,_payload,_same_frame
from .bus_factory import trace_corridor
from .compact_green import _transport
from .planning import overlap
from .resource_policy import RESOURCE_CHECK_LUA,mining_reservations,policy_specs
from .smelting import merge_entities

FILE='registered-belt-repair.json'
OWNER='advanced-circuits.json'
BLOCK='advanced_circuit_production'
ACTION='repair_registered_belts'
RECONCILE='reconcile_registered_belts'


def _authority(root,world,catalog):
    owner=_read(root/OWNER);board=_read(root/'pcb-board.json')
    if(not all(_same_frame(q,world,catalog)for q in(owner,board))or owner.get('phase')!='verified'
            or board.get('blocks',{}).get(BLOCK)!=owner.get('plan')):
        raise ValueError('registered Advanced Circuit owner or PCB plan changed')
    return owner,board


def _witnesses(root,units):
    hits=[]
    def walk(value,path,filename):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='registered_belt_repair_history':continue
                if key=='bindings':
                    if filename!=OWNER or path!=filename:
                        for name,unit in item.items():
                            if type(unit)is int and unit in units:hits.append(path+'.bindings.'+name)
                    continue
                if 'unit'in key and type(item)is int and item in units:hits.append(path+'.'+key)
                walk(item,path+'.'+key,filename)
        elif isinstance(value,list):
            for i,item in enumerate(value):walk(item,path+'['+str(i)+']',filename)
    for path in root.glob('*.json'):
        if path.name in(FILE,'catalog.json','jobs.json'):continue
        walk(_read(path),path.name,path.name)
    return hits[:16]


def _contracts(owner):
    sources=list(owner['tap_sources'].values())+[owner['plastic_source']]
    es=merge_entities(owner['plan']['entities'],[dict(name='transport-belt',position=q['position'],direction=q['facing'])for q in sources])
    types={_key(q):set(q['allowed_items'])for q in owner['audit']['belt_contracts']}
    for source in sources:
        destination=next(q for q in owner['core']['ports']if q['item']==source['item']and q['direction']=='input')
        route=trace_corridor(dict(owner['plan'],entities=es),source,destination)
        if not route.get('ok'):raise ValueError('saved typed source corridor is not traceable: '+str(route.get('reason')))
        for q in route['entities']:types.setdefault(_key(q),set()).add(source['item'])
    _,graph=_transport(es,owner['plan'].get('underground_pairs',[]))
    by={tuple(q['position'][a]for a in('x','y')):q for q in es if q['name']in('transport-belt','underground-belt')}
    edges=[dict(a=_key(by[a]),b=_key(by[b]))for a,(b,_)in graph.items()]
    return {k:sorted(v)for k,v in types.items()},sources,edges


def _context(game,action,*,allow_legacy=False):
    fields={'type','transaction_id','expected_world_id','expected_actor_unit','expected_catalog_fingerprint'}
    if(not isinstance(action,dict)or set(action)-fields-{'reason'}or not fields<=set(action)or action.get('type')!=ACTION
            or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or action.get('expected_world_id')!=getattr(game,'expected_world',None)
            or not isinstance(action.get('expected_catalog_fingerprint'),str)or not action['expected_catalog_fingerprint']
            or not isinstance(action.get('transaction_id'),str)or len(action['transaction_id'])!=64
            or any(c not in'0123456789abcdef'for c in action['transaction_id'])or getattr(game,'backend',None)!='assisted'):
        raise ValueError('exact registered assisted belt repair envelope required')
    root=Path(game.cfg.runtime_dir);ledger=_read(root/FILE);txn=ledger.get('transactions',{}).get(action['transaction_id'],{})
    payload=txn.get('payload',{});world,catalog=action['expected_world_id'],action['expected_catalog_fingerprint']
    phases=('prepared','native_complete','verified','reconciled_legacy')if allow_legacy else('prepared','native_complete','verified')
    if(not _same_frame(ledger,world,catalog)or txn.get('phase')not in phases
            or _hash(payload)!=action['transaction_id']or payload.get('action')!={k:v for k,v in action.items()if k not in('reason','transaction_id')}):
        raise ValueError('immutable belt repair intent missing or changed')
    owner,board=_authority(root,world,catalog)
    if _hash(board)!=payload['board_revision']or _hash(owner)not in(payload['owner_revision'],txn.get('publication',{}).get('after_revision')):
        raise ValueError('belt repair owner or PCB revision changed')
    repairs=payload['repairs']
    if not 1<=len(repairs)<=8 or len({q['old_unit']for q in repairs})!=len(repairs):raise ValueError('bounded distinct belt loss identities required')
    ports={_key(dict(q,name='transport-belt'))for q in owner['core']['ports']}
    for q in repairs:
        if(q['entity']['name']!='transport-belt'or q['entity']not in owner['plan']['entities']or _key(q['entity'])in ports
                or type(q['old_unit'])is not int or q['old_unit']<1):raise ValueError('only exact registered internal ordinary belt losses may be recovered')
    if _witnesses(root,{q['old_unit']for q in repairs}):raise ValueError('lost belt is pinned by another current controller')
    return dict(payload,transaction_id=action['transaction_id'])


def _valid(receipt,payload,key):
    rows=receipt.get('replacements',[])if isinstance(receipt,dict)else None
    if(not isinstance(rows,list)or len(rows)!=len(payload['repairs'])or any(not isinstance(q,dict)or type(q.get('unit_number'))is not int for q in rows)
            or any(type(receipt.get(k))is not int or receipt[k]<0 for k in('paid_belts','actor_unit','before','after','start_tick','tick'))
            or receipt.get('ok')is not True or receipt.get('transaction_id')!=key or receipt.get('world_id')!=payload['action']['expected_world_id']
            or receipt.get('actor_unit')!=payload['action']['expected_actor_unit']or receipt.get('catalog_fingerprint')!=payload['action']['expected_catalog_fingerprint']
            or receipt.get('paid_belts')!=len(rows)or type(receipt.get('before'))is not int or type(receipt.get('after'))is not int
            or receipt['before']-receipt['after']!=len(rows)or receipt.get('start_tick')!=receipt.get('tick')
            or len({q['unit_number']for q in rows})!=len(rows)):return False
    before_goods=receipt.get('before_goods',{}).get('combined');after_goods=receipt.get('after_goods',{}).get('combined')
    if(receipt.get('witness_version')!=2 or not isinstance(before_goods,dict)or not isinstance(after_goods,dict)
            or before_goods!=after_goods or any(type(v)is not int or v<0 for v in before_goods.values())):return False
    return all(row.get('key')==_key(q['entity'])and row.get('old_unit')==q['old_unit']and row['unit_number']>0
               and row['unit_number']!=q['old_unit']and row.get('direction')==q['entity']['direction']for row,q in zip(rows,payload['repairs']))


def _failure_evidence(root,job_key,key,payload):
    job=_read(root/'jobs.json')['jobs'][job_key];failure=job.get('uncertain_batch_receipt',{})
    if(job.get('world_id')!=payload['action']['expected_world_id']or failure.get('reason')!='infrastructure_repair_outcome_unknown'
            or failure.get('original_reason')!='registered_belt_spilled_goods_changed'
            or failure.get('paid_belts')!=len(payload['repairs'])):raise ValueError('exact historical failed paid-belt receipt missing')
    path=root/'job-events.jsonl';start=max(0,path.stat().st_size-4*1024*1024)
    with path.open('rb')as stream:
        stream.seek(start)
        if start:stream.readline()
        rows=stream.read().splitlines()
    intents=[]
    for line in rows:
        try:row=json.loads(line)
        except(ValueError,UnicodeDecodeError):continue
        if(row.get('key')==job_key and row.get('event')=='action_intent'
                and row.get('action',{}).get('type')==ACTION and row['action'].get('transaction_id')==key):intents.append(row)
    expected=dict(payload['action'],transaction_id=key)
    if(len(intents)!=1 or{k:v for k,v in intents[0]['action'].items()if k!='reason'}!=expected
            or len(failure.get('replacements',[]))!=len(payload['repairs'])):
        raise ValueError('unique exact historical belt action intent missing')
    return dict(job_key=job_key,intent_sha256=_hash(intents[0]),failure_sha256=_hash(failure),failure_reason=failure['original_reason'],
                replacements=deepcopy(failure['replacements']))


def _legacy_context(game,action):
    if not isinstance(action,dict)or set(action)-{'type','transaction_id','epoch_id','expected_world_id','expected_actor_unit','expected_catalog_fingerprint','reason'}or action.get('type')!=RECONCILE:
        raise ValueError('exact reviewed belt reconciliation envelope required')
    old=dict(action,type=ACTION);old.pop('epoch_id',None);payload=_context(game,old,allow_legacy=True)
    state=_read(Path(game.cfg.runtime_dir)/FILE);txn=state['transactions'][action['transaction_id']]
    epoch=next((q for q in txn.get('reconciliation_epochs',[])if q['epoch_id']==action.get('epoch_id')),None)
    if not epoch or txn.get('active_reconciliation')!=action.get('epoch_id')or epoch['phase']!='accepted':
        raise ValueError('explicitly accepted belt reconciliation epoch required')
    proposal=epoch['proposal'];unsigned={k:v for k,v in proposal.items()if k!='epoch_id'}
    if _hash(unsigned)!=action['epoch_id']or proposal['transaction_id']!=action['transaction_id']:
        raise ValueError('immutable reconciliation proposal changed')
    if _failure_evidence(Path(game.cfg.runtime_dir),proposal['evidence']['job_key'],action['transaction_id'],payload)!=proposal['evidence']:
        raise ValueError('historical reconciliation evidence changed')
    return dict(payload,reconciliation=proposal,epoch_id=action['epoch_id'])


def approved_reconciliation(root,job_key,transaction_id):
    """Read-only frontend-clear prerequisite; no native receipt is fabricated."""
    try:
        root=Path(root);state=_read(root/FILE);txn=state['transactions'][transaction_id]
        active=txn.get('active_reconciliation');epoch=next(q for q in txn.get('reconciliation_epochs',[])if q['epoch_id']==active)
        proposal=epoch['proposal'];owner,board=_authority(root,state['world_id'],state['catalog_fingerprint'])
        return(epoch['phase']=='accepted'and _hash({k:v for k,v in proposal.items()if k!='epoch_id'})==active
               and _hash(txn['payload'])==transaction_id and proposal['evidence']['job_key']==job_key
               and _failure_evidence(root,job_key,transaction_id,txn['payload'])==proposal['evidence']
               and _hash(board)==proposal['board_revision']
               and _hash(owner)in(proposal['owner_revision'],txn.get('publication',{}).get('after_revision')))
    except(OSError,ValueError,KeyError,TypeError,StopIteration):return False


def _valid_legacy(receipt,payload,key,epoch):
    if not isinstance(receipt,dict):return False
    rows=receipt.get('replacements',[])
    if(not isinstance(rows,list)or len(rows)!=len(payload['repairs'])or any(not isinstance(q,dict)or type(q.get('unit_number'))is not int for q in rows)
            or len({q['unit_number']for q in rows})!=len(rows)
            or any(q.get('old_unit')!=old['old_unit']or q.get('key')!=_key(old['entity'])or q.get('direction')!=old['entity']['direction']
                   or q['unit_number']<=0 or q['unit_number']==old['old_unit']for q,old in zip(rows,payload['repairs']))):return False
    return(receipt.get('ok')is True and receipt.get('mode')=='legacy_paid_belt_reconciliation_v1'and receipt.get('transaction_id')==key
           and receipt.get('epoch_id')==epoch and receipt.get('world_id')==payload['action']['expected_world_id']
           and receipt.get('actor_unit')==payload['action']['expected_actor_unit']and receipt.get('catalog_fingerprint')==payload['action']['expected_catalog_fingerprint']
           and type(receipt.get('observed_tick'))is int and receipt['observed_tick']>=0
           and receipt.get('ground_baseline_available')is False and receipt.get('material_baseline_available')is False
           and receipt.get('historical_ground_conservation')=='unknown'and receipt.get('historical_material_conservation')=='unknown'
           and type(receipt.get('paid_belts'))is int and receipt['paid_belts']==len(payload['repairs'])
           and receipt.get('replacements')==payload['reconciliation']['marker']['replacements']
           and receipt.get('intent_sha256')==payload['reconciliation']['evidence']['intent_sha256']
           and receipt.get('failure_sha256')==payload['reconciliation']['evidence']['failure_sha256']
           and receipt.get('source_build_tick')==payload['reconciliation']['marker']['tick']
           and not any(k in receipt for k in('before','after','before_goods')))


def execute_reconcile_registered_belts(game,action):
    payload=_legacy_context(game,action)
    try:r=game.query('local x=helpers.json_to_table('+_payload(payload)+');'+RESOURCE_CHECK_LUA+BELT_GUARD_LUA+RECONCILE_BELTS_LUA)
    except Exception as exc:return dict(ok=False,reason='infrastructure_repair_outcome_unknown',original_reason=str(exc))
    if not _valid_legacy(r,payload,action['transaction_id'],action['epoch_id']):
        return dict(ok=False,reason='infrastructure_repair_outcome_unknown',original_reason=r.get('reason','invalid_legacy_receipt')if isinstance(r,dict)else'invalid_legacy_receipt')
    return r


def execute_repair_registered_belts(game,action):
    payload=_context(game,action)
    try:r=game.query('local x=helpers.json_to_table('+_payload(payload)+');'+RESOURCE_CHECK_LUA+BELT_GUARD_LUA+REPAIR_BELTS_LUA)
    except Exception as exc:return dict(ok=False,reason='infrastructure_repair_outcome_unknown',original_reason=type(exc).__name__+': '+str(exc))
    if not isinstance(r,dict)or type(r.get('ok'))is not bool:return dict(ok=False,reason='infrastructure_repair_outcome_unknown',original_reason='invalid_belt_repair_receipt')
    if r['ok']and not _valid(r,payload,action['transaction_id']):return dict(r,ok=False,reason='infrastructure_repair_outcome_unknown',original_reason='paid_belt_receipt_invalid')
    if not r['ok']and(r.get('effects_started')or type(r.get('effects_started'))is not bool):
        return dict(r,reason='infrastructure_repair_outcome_unknown',original_reason=r.get('reason'))
    return r


class RegisteredBeltRepair:
    def __init__(self,game,builder,catalog,*,advanced=None):
        self.game,self.builder,self.catalog,self.advanced=game,builder,catalog,advanced
        self.path=Path(game.cfg.runtime_dir)/FILE;self.last_diagnostic=None

    def _ledger(self,obs):
        if not obs.get('ok',True)or type(obs.get('actor_unit_number'))is not int:raise ValueError('fresh actor observation required')
        state=_read(self.path)if self.path.exists()else dict(world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,transactions={})
        if not _same_frame(state,obs['world_id'],self.catalog.fingerprint):raise ValueError('belt recovery checkpoint world or catalog changed')
        return state

    def _query(self,payload,mode):
        return self.game.query('local x=helpers.json_to_table('+_payload(dict(payload,mode=mode))+');'+RESOURCE_CHECK_LUA+BELT_GUARD_LUA+SCAN_BELTS_LUA)

    def prepare_reconciliation(self,obs,job_key,transaction_id):
        """Read-only proposal; a missing old conservation baseline remains unknown."""
        try:
            state=self._ledger(obs);txn=state['transactions'][transaction_id]
            if txn['phase']!='prepared'or txn.get('active_reconciliation'):raise ValueError('legacy transaction is not awaiting explicit reconciliation')
            payload=_context(self.game,dict(txn['payload']['action'],transaction_id=transaction_id))
            evidence=_failure_evidence(self.path.parent,job_key,transaction_id,payload)
            native=self._query(dict(payload,reconciliation={'preview':True}),'reconciliation_preview')
            if not native.get('ok'):return native
            marker=native['legacy_marker']
            if(marker.get('version')is not None or marker.get('before')is not None or marker['phase']!='building'
                    or type(marker.get('tick'))is not int or marker['tick']<0
                    or marker['paid_belts']!=len(payload['repairs'])or marker['replacements']!=evidence['replacements']):
                raise ValueError('legacy full paid prefix does not match historical action evidence')
            proposal=dict(transaction_id=transaction_id,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,
                ledger_revision=_hash(state),owner_revision=payload['owner_revision'],board_revision=payload['board_revision'],
                marker=marker,evidence=evidence,review_snapshot=native,
                ground_baseline_available=False,material_baseline_available=False)
            proposal['epoch_id']=_hash(proposal);return dict(ok=True,proposal=proposal)
        except(OSError,ValueError,KeyError,TypeError)as exc:return dict(ok=False,reason=str(exc))

    def activate_reconciliation(self,proposal):
        """Explicit metadata-only activation; Root calls under its writer lock."""
        unsigned={k:v for k,v in proposal.items()if k!='epoch_id'}
        if _hash(unsigned)!=proposal.get('epoch_id'):raise ValueError('reviewed reconciliation proposal changed')
        if(not _same_frame(proposal,getattr(self.game,'expected_world',None),self.catalog.fingerprint)
                or proposal.get('ground_baseline_available')is not False or proposal.get('material_baseline_available')is not False):
            raise ValueError('reconciliation baseline or bound world changed')
        state=_read(self.path);key=proposal['transaction_id'];txn=state['transactions'][key]
        if _hash(state)!=proposal['ledger_revision']or txn.get('active_reconciliation'):raise ValueError('reconciliation activation CAS changed')
        owner,board=_authority(self.path.parent,proposal['world_id'],proposal['catalog_fingerprint'])
        if _hash(owner)!=proposal['owner_revision']or _hash(board)!=proposal['board_revision']:raise ValueError('reconciliation owner authority changed')
        if _failure_evidence(self.path.parent,proposal['evidence']['job_key'],key,txn['payload'])!=proposal['evidence']:
            raise ValueError('reconciliation historical evidence changed')
        txn.setdefault('reconciliation_epochs',[]).append(dict(epoch_id=proposal['epoch_id'],phase='accepted',proposal=deepcopy(proposal)))
        txn['active_reconciliation']=proposal['epoch_id'];_atomic_json(self.path,state)
        return dict(ok=True,epoch_id=proposal['epoch_id'],effects='metadata_only',historical_ground_conservation='unknown')

    def prepare(self,obs):
        try:
            self._ledger(obs);owner,board=_authority(self.path.parent,obs['world_id'],self.catalog.fingerprint)
            types,sources,edges=_contracts(owner)
            nodes=[dict(q,unit_number=owner['bindings'][_key(q)])for q in owner['plan']['entities']]
            for q in sources:
                if _key(q)not in{_key(e)for e in nodes}:nodes.append(dict(name='transport-belt',position=q['position'],direction=q['facing'],unit_number=q['unit_number']))
            args=dict(action=dict(type=ACTION,expected_world_id=obs['world_id'],expected_actor_unit=obs['actor_unit_number'],expected_catalog_fingerprint=self.catalog.fingerprint),
                nodes=nodes,typed=types,edges=edges,pairs=owner['plan'].get('underground_pairs',[]),repairs=[],specs=[],zones=mining_reservations(self.game,obs['world_id']))
            native=self._query(args,'scan')
            if not native.get('ok'):return native
            by={_key(q):q for q in owner['plan']['entities']};ports={_key(dict(q,name='transport-belt'))for q in owner['core']['ports']}
            repairs=[]
            for missing in native.get('missing',[])or[]:
                q=by.get(missing['key'])
                if not q or q['name']!='transport-belt'or missing['key']in ports or missing['key']not in types:
                    raise ValueError('missing asset is not a typed internal ordinary belt')
                repairs.append(dict(entity=deepcopy(q),old_unit=owner['bindings'][missing['key']]))
            if not repairs:return dict(ok=True,needed=False,native=native)
            if len(repairs)>8:raise ValueError('belt recovery exceeds eight exact losses')
            if _witnesses(self.path.parent,{q['old_unit']for q in repairs}):raise ValueError('lost belt has a foreign current unit witness')
            for q in repairs:
                p=q['entity']['position'];box=[[p['x']-.5,p['y']-.5],[p['x']+.5,p['y']+.5]]
                if any(overlap(box,v['reserved_area'])for k,v in board['blocks'].items()if k!=BLOCK and v.get('reserved_area')):
                    raise ValueError('lost belt footprint now overlaps foreign future production')
            args.update(repairs=repairs,specs=policy_specs(self.builder,obs,[q['entity']for q in repairs]),owner_revision=_hash(owner),board_revision=_hash(board),created_tick=native['tick'])
            checked=self._query(args,'preflight')
            if not checked.get('ok'):return checked
            return dict(ok=True,needed=True,payload=args,transaction_id=_hash(args),cost={'transport-belt':len(repairs)},native=checked)
        except(OSError,ValueError,KeyError,TypeError)as exc:return dict(ok=False,reason=str(exc))

    def _publish(self,state,key,txn):
        path=self.path.parent/OWNER;owner=_read(path)
        if 'publication'not in txn:
            if _hash(owner)!=txn['payload']['owner_revision']:raise ValueError('belt owner publication baseline changed')
            after=deepcopy(owner)
            for row in txn['receipt']['replacements']:
                if after['bindings'].get(row['key'])!=row['old_unit']:raise ValueError('old belt binding changed')
                after['bindings'][row['key']]=row['unit_number']
            history=dict(transaction_id=key,receipt=deepcopy(txn['receipt']))
            after.setdefault('registered_belt_repair_history',[]).append(history)
            txn['publication']=dict(before_revision=_hash(owner),after_revision=_hash(after),value=after);_atomic_json(self.path,state)
        intent=txn['publication'];current=_hash(owner)
        if current==intent['before_revision']:_atomic_json(path,intent['value'])
        elif current!=intent['after_revision']:raise ValueError('belt binding publication CAS changed')
        txn['phase']='reconciled_legacy'if txn['receipt'].get('mode')=='legacy_paid_belt_reconciliation_v1'else'verified';_atomic_json(self.path,state)
        if self.advanced is not None:self.advanced.state=_read(path)

    def step(self,obs):
        try:
            state=self._ledger(obs);pending=[(k,v)for k,v in state['transactions'].items()if v['phase']not in('verified','reconciled_legacy')]
            if len(pending)>1:raise ValueError('multiple unresolved belt repair transactions')
            if pending:
                key,txn=pending[0];action=dict(txn['payload']['action'],transaction_id=key)
                legacy=txn.get('active_reconciliation')
                if legacy:
                    action.update(type=RECONCILE,epoch_id=legacy);payload=_legacy_context(self.game,action)
                else:payload=_context(self.game,action)
                result=self._query(payload,'inspect')
                if not result.get('ok'):raise ValueError(result.get('reason','belt paid-marker inspection failed'))
                if result.get('receipt'):
                    valid=(_valid_legacy(result['receipt'],payload,key,legacy)if legacy else _valid(result['receipt'],txn['payload'],key))
                    if not valid:raise ValueError('native paid belt receipt changed')
                    txn.update(phase='native_complete',receipt=result['receipt']);_atomic_json(self.path,state)
                    self._publish(state,key,txn)
                    self.last_diagnostic=dict(ok=True,reason='reviewed legacy replacement published; historical conservation unknown'if legacy else'registered belts normally recovered',receipt=txn['receipt']);return None
                count=len(txn['payload']['repairs'])
                if legacy:return dict(action,reason='record reviewed legacy paid belt replacement with historical conservation unknown')
                if result['actor_belts']<count:
                    funded=self.builder.bootstrap.ensure_item(obs,'transport-belt',count)
                    self.last_diagnostic=dict(reason='normally fund registered belt replacements',required=count)
                    return funded if funded and funded.get('type')else None
                return dict(action,reason='normally restore exact registered typed belt losses')
            ready=self.prepare(obs)
            if not ready.get('ok')or not ready.get('needed'):self.last_diagnostic=ready;return None
            count=ready['cost']['transport-belt']
            if ready['native']['actor_belts']<count:
                funded=self.builder.bootstrap.ensure_item(obs,'transport-belt',count)
                self.last_diagnostic=dict(reason='normally fund registered belt replacements',required=count)
                return funded if funded and funded.get('type')else None
            key=ready['transaction_id'];state['transactions'][key]=dict(phase='prepared',payload=ready['payload']);_atomic_json(self.path,state)
            return dict(ready['payload']['action'],transaction_id=key,reason='normally restore exact registered typed belt losses')
        except(OSError,ValueError,KeyError,TypeError)as exc:self.last_diagnostic=dict(ok=False,reason=str(exc));return None


BELT_GUARD_LUA=r'''
local marker=nil
local function reject(reason)return {ok=false,status="blocked",reason=reason,effects_started=marker~=nil,paid_belts=marker and marker.paid_belts or 0,replacements=marker and marker.replacements or nil}end
local frame=x.action
if not d or d.world_id~=frame.expected_world_id then return reject("registered_belt_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=frame.expected_actor_unit then return reject("registered_belt_actor_changed")end
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
marker=x.transaction_id and d.registered_belt_repairs and d.registered_belt_repairs[x.transaction_id]
if marker and((marker.phase~="complete"and not x.reconciliation)or marker.actor_unit~=frame.expected_actor_unit or marker.world_id~=frame.expected_world_id)then return reject("registered_belt_prior_effects_uncertain")end
local function same(a,b)
 if type(a)~=type(b)then return false end;if type(a)~="table"then return a==b end
 for k,v in pairs(a)do if not same(v,b[k])then return false end end
 for k in pairs(b)do if a[k]==nil then return false end end;return true
end
local legacy_projection=marker and{phase=marker.phase,world_id=marker.world_id,actor_unit=marker.actor_unit,paid_belts=marker.paid_belts,
 replacements=marker.replacements,tick=marker.tick,version=marker.version,before=marker.before}or nil
if x.reconciliation then
 if not marker or marker.phase~="building"or marker.version or marker.before or marker.paid_belts~=#x.repairs
  or #marker.replacements~=#x.repairs then return reject("registered_belt_legacy_prefix_not_exact")end
 if not x.reconciliation.preview and not same(legacy_projection,x.reconciliation.marker)then return reject("registered_belt_reviewed_native_marker_changed")end
end
local effective_receipt=marker and marker.receipt or nil
if x.reconciliation and marker and marker.reconciliations then effective_receipt=marker.reconciliations[x.epoch_id]end
local replacement={};if marker then for _,q in ipairs(marker.receipt and marker.receipt.replacements or marker.replacements)do replacement[q.key]=q.unit_number end end
local live={};local missing={};local damaged={};local main=a.get_main_inventory()
if not main or not main.valid then return reject("registered_belt_actor_inventory_missing")end
local function stock()
 local n=0;for i=1,#main do local st=main[i];if st.valid_for_read and st.name=="transport-belt"then
  if st.quality.name~="normal"or st.prototype.type~="item"or st.health~=1 or st.spoil_tick~=0 or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return nil end
  n=n+st.count
 end end;return n
end
local belts=stock();if not belts then return reject("registered_belt_actor_stock_metadata_unsupported")end
for _,q in ipairs(x.nodes)do local k=key(q);local e=target(q.position,q.name)
 if not e or not e.valid then
  if q.name~="transport-belt"or marker then return reject("registered_belt_other_owned_asset_missing")end
  missing[#missing+1]={key=k,unit_number=q.unit_number}
 else
  if e.unit_number~=(replacement[k]or q.unit_number)or e.force~=f or e.surface~=s or e.position.x~=q.position.x or e.position.y~=q.position.y
   or e.quality.name~="normal"or(q.name~="small-electric-pole"and e.direction~=(q.direction or 0))then return reject("registered_belt_owned_identity_or_direction_changed")end
  if q.recipe then local r=e.get_recipe();if not r or r.name~=q.recipe then return reject("registered_belt_owned_recipe_changed")end end
  if q.name=="transport-belt"then local n=e.belt_neighbours
   if not n or not n.inputs or not n.outputs then return reject("registered_belt_native_neighbour_api_missing")end
  end
  if e.health and e.health<e.max_health and #damaged<64 then damaged[#damaged+1]={name=e.name,unit_number=e.unit_number,position=e.position,health=e.health,max_health=e.max_health}end
  if x.typed[k]then local allowed={};for _,item in ipairs(x.typed[k])do allowed[item]=true end
   for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents())do
    if not allowed[item.name]or(item.quality and item.quality~="normal")then return reject("registered_belt_typed_lane_contaminated")end
   end end
  end;live[k]=e
 end
end
for _,pair in ipairs(x.pairs)do
 local first=target({x=pair.input[1],y=pair.input[2]},"underground-belt");local last=target({x=pair.output[1],y=pair.output[2]},"underground-belt")
 if not first or not last or first.underground_belt_neighbour~=last or last.underground_belt_neighbour~=first then return reject("registered_belt_underground_pair_changed")end
end
local debris={};local corpses={}
local function site_checks()
 for i,q in ipairs(x.repairs)do local spec=x.specs[i];local p=q.entity.position
  local why=resource_violation(spec,x.zones,{},x.specs);if why then return false,why end
  if target(p,"transport-belt")or not s.can_place_entity{name="transport-belt",position=p,direction=q.entity.direction,force=f}then return false,"registered_belt_missing_site_occupied"end
  for _,e in pairs(s.find_entities_filtered{area=spec.survey_area})do
   if e.type=="corpse"then corpses[#corpses+1]={entity=e,name=e.name,position={x=e.position.x,y=e.position.y}}
   elseif e~=a then
    if e.type~="item-entity"then return false,"registered_belt_site_foreign_obstruction"end
    local st=e.stack
    if not st or not st.valid_for_read or st.prototype.type~="item"or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
     or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return false,"registered_belt_spill_metadata_unsupported"end
    local allowed=false;for _,item in ipairs(x.typed[key(q.entity)]or{})do if st.name==item then allowed=true end end
    if not allowed then return false,"registered_belt_spill_wrong_typed_item"end
    debris[#debris+1]={entity=e,item=st.name,count=st.count,position={x=e.position.x,y=e.position.y}}
   end
  end
  local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}};local out=vectors[q.entity.direction]
  for _,other in pairs(s.find_entities_filtered{area={{p.x-1.1,p.y-1.1},{p.x+1.1,p.y+1.1}},type={"transport-belt","underground-belt","splitter"}})do
   if other.type~="item-entity"and other.type~="corpse"and not live[key(other)]then
    local v=vectors[other.direction]
    if(other.position.x==p.x+out[1]and other.position.y==p.y+out[2])or(v and other.position.x+v[1]==p.x and other.position.y+v[2]==p.y)then
     return false,"registered_belt_foreign_future_sidefeed"
    end
   end
  end
 end;return true
end
local function belt_proof()
 for _,q in ipairs(x.repairs)do local k=key(q.entity);local e=live[k]
  if not e then return false,"registered_belt_paid_replacement_missing"end
  local expected_in,expected_out={},{}
  for _,edge in ipairs(x.edges)do
   if edge.a==k and live[edge.b]then expected_out[live[edge.b].unit_number]=true end
   if edge.b==k and live[edge.a]then expected_in[live[edge.a].unit_number]=true end
  end
  local neighbours=e.belt_neighbours
  for _,pair in ipairs{{expected_in,neighbours.inputs},{expected_out,neighbours.outputs}}do
   local seen={};for _,other in pairs(pair[2])do if not pair[1][other.unit_number]then return false,"registered_belt_unexpected_native_neighbour"end;seen[other.unit_number]=true end
   for unit in pairs(pair[1])do if not seen[unit]then return false,"registered_belt_native_edge_missing"end end
  end
 end;return true
end
local function observe_goods()
 local floor,transport,combined,stacks={},{},{},{}
 local function add(dst,item,n)dst[item]=(dst[item]or 0)+n end
 for k,e in pairs(live)do if x.typed[k]then
  local allowed={};for _,item in ipairs(x.typed[k])do allowed[item]=true end
  for lane=1,2 do for _,item in pairs(e.get_transport_line(lane).get_contents())do
   if not allowed[item.name]or(item.quality and item.quality~="normal")then return nil end
   add(transport,item.name,item.count);add(combined,item.name,item.count)
  end end
 end end
 local seen={}
 for _,q in ipairs(x.repairs)do local p=q.entity.position
  for _,e in pairs(s.find_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="item-entity"})do
   if e.type=="item-entity"and not seen[e]then local st=e.stack;seen[e]=true
    if not st or not st.valid_for_read or st.prototype.type~="item"or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
     or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return nil end
    add(floor,st.name,st.count);add(combined,st.name,st.count)
    stacks[#stacks+1]={name=st.name,count=st.count,quality="normal",health=st.health,spoil_tick=st.spoil_tick,position={x=e.position.x,y=e.position.y}}
   end
  end
 end
 return {floor=floor,transport=transport,combined=combined,ground_stacks=stacks}
end
'''

SCAN_BELTS_LUA=r'''
if x.mode=="preflight"then local good,why=site_checks();if not good then return reject(why)end end
if marker then local good,why=belt_proof();if not good then return reject(why)end end
local threats={}
if #x.repairs>0 then local p=x.repairs[1].entity.position
 local lo,hi={x=p.x-32,y=p.y-32},{x=p.x+32,y=p.y+32}
 for _,q in ipairs(x.repairs)do lo.x=math.min(lo.x,q.entity.position.x-32);lo.y=math.min(lo.y,q.entity.position.y-32);hi.x=math.max(hi.x,q.entity.position.x+32);hi.y=math.max(hi.y,q.entity.position.y+32)end
 for _,e in pairs(s.find_entities_filtered{area={lo,hi},force="enemy",type={"unit","turret","unit-spawner"}})do
  if #threats>=64 then break end
  local r=e.prototype.attack_parameters
  threats[#threats+1]={name=e.name,unit_number=e.unit_number,position=e.position,health=e.health,range=r and r.range or nil}
 end
end
return {ok=true,tick=game.tick,missing=missing,actor_belts=belts,debris_count=#debris,damaged=damaged,hostiles=threats,
 receipt=effective_receipt,legacy_marker=x.reconciliation and legacy_projection or nil,current_goods=x.reconciliation and observe_goods()or nil}
'''

REPAIR_BELTS_LUA=r'''
if marker then local good,why=belt_proof();if not good then return reject(why)end;local r={};for k,v in pairs(marker.receipt)do r[k]=v end;r.reused_receipt=true;return r end
if #missing~=#x.repairs or belts<#x.repairs then return reject("registered_belt_all_holes_and_full_material_prepayment_required")end
local good,why=site_checks();if not good then return reject(why)end
local before_goods=observe_goods();if not before_goods then return reject("registered_belt_before_goods_invalid")end
d.registered_belt_repairs=d.registered_belt_repairs or{}
marker={version=2,phase="building",world_id=frame.expected_world_id,actor_unit=frame.expected_actor_unit,paid_belts=0,replacements={},tick=game.tick,
 before={main_belts=belts,goods=before_goods,corpses={}}}
for _,q in ipairs(corpses)do marker.before.corpses[#marker.before.corpses+1]={name=q.name,position=q.position}end
d.registered_belt_repairs[x.transaction_id]=marker
for _,q in ipairs(x.repairs)do
 local index=nil;for i=1,#main do if main[i].valid_for_read and main[i].name=="transport-belt"and main[i].count>0 then index=i;break end end
 if not index then return reject("registered_belt_material_changed_inside_operation")end
 main[index].count=main[index].count-1;marker.paid_belts=marker.paid_belts+1
 local e=s.create_entity{name="transport-belt",position=q.entity.position,direction=q.entity.direction,force=f,quality="normal",raise_built=true,player=a.player,preserve_ghosts_and_corpses=true}
 if not e then return reject("registered_belt_paid_build_uncertain")end
 local k=key(q.entity);live[k]=e;marker.replacements[#marker.replacements+1]={key=k,old_unit=q.old_unit,unit_number=e.unit_number,direction=e.direction}
end
local good,why=belt_proof();if not good then return reject(why)end
for _,q in ipairs(debris)do local e=q.entity;local st=e.valid and e.stack
 if e.valid and(not st or not st.valid_for_read or st.name~=q.item or st.count>q.count or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
  or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data
  or e.position.x~=q.position.x or e.position.y~=q.position.y)then return reject("registered_belt_spilled_goods_changed")end
end
for _,q in ipairs(corpses)do if not q.entity.valid or q.entity.name~=q.name or q.entity.position.x~=q.position.x or q.entity.position.y~=q.position.y then return reject("registered_belt_remnants_changed")end end
local after=stock();local after_goods=observe_goods()
if not after or belts-after~=#x.repairs or game.tick~=marker.tick or not after_goods then return reject("registered_belt_material_conservation_failed")end
if not same(before_goods.combined,after_goods.combined)then return reject("registered_belt_floor_and_line_conservation_failed")end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",transaction_id=x.transaction_id,world_id=frame.expected_world_id,
 catalog_fingerprint=frame.expected_catalog_fingerprint,actor_unit=frame.expected_actor_unit,replacements=marker.replacements,paid_belts=marker.paid_belts,
 before=belts,after=after,start_tick=marker.tick,tick=game.tick,preserved_spill_stacks=#debris,witness_version=2,before_goods=before_goods,after_goods=after_goods}
return marker.receipt
'''

RECONCILE_BELTS_LUA=r'''
local good,why=belt_proof();if not good then return reject(why)end
if effective_receipt then return effective_receipt end
local current=observe_goods();if not current then return reject("registered_belt_reconciliation_current_goods_invalid")end
local receipt={ok=true,status="succeeded",mode="legacy_paid_belt_reconciliation_v1",transaction_id=x.transaction_id,epoch_id=x.epoch_id,
 world_id=frame.expected_world_id,catalog_fingerprint=frame.expected_catalog_fingerprint,actor_unit=frame.expected_actor_unit,
 observed_tick=game.tick,paid_belts=marker.paid_belts,replacements=marker.replacements,current_goods=current,
 source_build_tick=marker.tick,
 ground_baseline_available=false,material_baseline_available=false,historical_ground_conservation="unknown",historical_material_conservation="unknown",
 original_failure=x.reconciliation.evidence.failure_reason,intent_sha256=x.reconciliation.evidence.intent_sha256,failure_sha256=x.reconciliation.evidence.failure_sha256}
marker.reconciliations=marker.reconciliations or{};marker.reconciliations[x.epoch_id]=receipt
return receipt
'''
