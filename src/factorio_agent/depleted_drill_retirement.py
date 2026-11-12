"""Normal recovery of registered, completely exhausted electric mining drills."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_ai.deterministic_builder import _plan
from factorio_ai.deterministic_state import _atomic_json
from .transport import xy


FILE='depleted-drill-retirement.json'
MODE='paid_depleted_electric_drill_v1'
UNKNOWN='drill_retirement_outcome_unknown'
MAX_DRILLS=128


def _hash(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _payload(v):return json.dumps(json.dumps(v,separators=(',',':')))
def _position(p):return isinstance(p,dict)and set(p)=={'x','y'}and all(type(v)in(int,float)and math.isfinite(v)for v in p.values())
def _key(e):return e['name'],*xy(e)
def _root(game):
    value=getattr(getattr(game,'cfg',None),'runtime_dir',None)
    return Path(value)if value is not None else None


def _inactive_base_drill(game):
    """A retired source is not a claim that its physical item was recovered."""
    root=_root(game)
    if root is None or not(root/'base-coal-recovery.json').exists():return None
    doc=json.loads((root/'base-coal-recovery.json').read_text());world=getattr(game,'expected_world',None)
    if doc.get('phase')!='verified'or not doc.get('published')or(world is not None and doc.get('world_id')!=world):return None
    builder=json.loads((root/'factory-builder.json').read_text());retired=builder.get('coal_plan',{}).get('retired_source',{})
    spec=doc['old_builder']['coal_plan']['drill'];diagnostic=doc['depleted_source']
    if(retired.get('drill')!=spec or retired.get('diagnostic',{}).get('unit_number')!=diagnostic['unit_number']
            or not diagnostic.get('depleted')or diagnostic.get('remaining_coal')!=0):raise ValueError('inactive base source historical contract changed')
    return spec


def _validate_native(receipt,target,world,catalog):
    if(not isinstance(receipt,dict)or receipt.get('ok')is not True or receipt.get('phase')!='complete'
            or receipt.get('mode')!=MODE or receipt.get('retirement_id')!=target['retirement_id']
            or receipt.get('world_id')!=world or receipt.get('catalog_fingerprint')!=catalog
            or receipt.get('unit_number')!=target['unit_number']or receipt.get('position')!=target['position']
            or receipt.get('direction')!=target['direction']or receipt.get('remaining_any_resource')!=0
            or receipt.get('normal_drills_recovered')!=1 or receipt.get('removed')is not True
            or type(receipt.get('before_drills'))is not int or type(receipt.get('after_drills'))is not int
            or receipt['before_drills']<0 or receipt['after_drills']!=receipt['before_drills']+1
            or type(receipt.get('radius'))not in(int,float)or not 0<receipt['radius']<=16
            or type(receipt.get('actor_unit'))is not int or receipt['actor_unit']<1
            or any(type(receipt.get(k))is not int or receipt[k]<1 for k in('force_index','surface_index'))
            or any(type(receipt.get(k))is not int or receipt[k]<0 for k in('started_tick','completed_tick'))
            or receipt['completed_tick']<receipt['started_tick']):
        raise ValueError('invalid exact normal drill recovery receipt')


def load_retirements(game,*,world_id=None,catalog_fingerprint=None):
    root=_root(game)
    if root is None or not(root/FILE).exists():return None
    state=json.loads((root/FILE).read_text())
    world=world_id or getattr(game,'expected_world',None)
    if(state.get('mode')!=MODE or(world is not None and state.get('world_id')!=world)
            or(catalog_fingerprint is not None and state.get('catalog_fingerprint')!=catalog_fingerprint)):
        raise ValueError('drill retirement world/catalog/format changed')
    if not isinstance(state.get('world_id'),str)or not state['world_id']or not isinstance(state.get('catalog_fingerprint'),str):raise ValueError('pinned retirement frame required')
    targets=state.get('targets',[])
    if not isinstance(targets,list)or len(targets)>MAX_DRILLS:raise ValueError('bounded retirement targets required')
    ids=set();units=set();positions=set()
    for q in targets:
        if(q.get('name')!='electric-mining-drill'or not _position(q.get('position'))or type(q.get('unit_number'))is not int
                or q['unit_number']<1 or q.get('direction')not in(0,4,8,12)or q.get('retirement_id')in ids
                or q['unit_number']in units or xy(q)in positions):raise ValueError('invalid unique retirement identity')
        expected=_hash([MODE,state['world_id'],state['catalog_fingerprint'],q['unit_number'],q['position'],q['direction'],q['owner_revision']])
        if q.get('retirement_id')!=expected:raise ValueError('retirement target digest changed')
        owner=state.get('authorities',{}).get(q['owner_revision'])
        if(not isinstance(owner,dict)or _hash(owner)!=q['owner_revision']or _hash(owner['plan'])!=owner.get('plan_revision')
                or q['unit_number']not in owner.get('known_drill_units',[])
                or not any(_key(e)==_key(q)and e.get('direction',0)==q['direction']for e in owner['plan']['entities'])):
            raise ValueError('retirement target lacks exact registered owner history')
        ids.add(expected);units.add(q['unit_number']);positions.add(xy(q))
    by_id={q['retirement_id']:q for q in targets};previous=None;completed=set()
    receipts=state.get('receipts',[])
    if not isinstance(receipts,list)or len(receipts)>len(targets):raise ValueError('bounded retirement receipt list required')
    for row in receipts:
        identity=row.get('retirement_id')
        if identity not in by_id or identity in completed or row.get('previous_receipt')!=previous or _hash({k:v for k,v in row.items()if k!='receipt_id'})!=row.get('receipt_id'):
            raise ValueError('retirement receipt chain changed')
        _validate_native(row['native'],by_id[identity],state['world_id'],state['catalog_fingerprint'])
        completed.add(identity);previous=row['receipt_id']
    if any(q.get('phase')=='complete'and q['retirement_id']not in completed for q in targets):raise ValueError('retired flag has no paid recovery receipt')
    return state


def effective_mining_plan(game,obs,plan,*,catalog_fingerprint=None):
    """Receipt-only active view. Canonical geometry and growth remain untouched."""
    if not any(q.get('name')=='electric-mining-drill'for q in plan.get('entities',[])):return plan
    state=load_retirements(game,world_id=obs.get('world_id'),catalog_fingerprint=catalog_fingerprint)
    if state is None:return plan
    completed={q['retirement_id']for q in state.get('receipts',[])}
    targets={xy(q):q for q in state['targets']}
    actual={_key(q):q for q in obs.get('entities',[])};removed=[]
    for q in plan['entities']:
        if q['name']!='electric-mining-drill'or xy(q)not in targets:continue
        target=targets[xy(q)];live=actual.get(_key(q))
        if target['retirement_id']in completed:
            if q.get('direction',0)!=target['direction']:raise ValueError('a retired drill position cannot be rotated or rebuilt')
            if live:raise ValueError('paid retired drill position unexpectedly occupied')
            removed.append(q)
        elif target.get('phase')in('prepared','armed')and not live:
            raise ValueError('drill retirement outcome not reconciled; rebuilding forbidden')
    if not removed:return plan
    positions={xy(q)for q in removed};result=deepcopy(plan)
    result['entities']=[q for q in result['entities']if not(q['name']=='electric-mining-drill'and xy(q)in positions)]
    for side,rows in result.get('producer_rows',{}).items():result['producer_rows'][side]=[q for q in rows if xy(q)not in positions]
    extra={k:v for k,v in result.items()if k not in('ok','reason','entities','ports','required_items')}
    extra.update(retired_drill_units=[targets[p]['unit_number']for p in sorted(positions)],
        historical_drill_count=plan.get('drill_count',sum(q['name']=='electric-mining-drill'for q in plan['entities'])),
        effective_drill_count=sum(q['name']=='electric-mining-drill'for q in result['entities']))
    return _plan(result['entities'],result.get('ports',[]),**extra)


def guard_drill_rebuild(game,action):
    """Runtime entrypoint also covers controllers that emit direct builds."""
    if action.get('type')not in('build','build_many'):return None
    actions=action.get('actions',[])if action['type']=='build_many'else[action]
    actions=[q for q in actions if isinstance(q,dict)and q.get('name')=='electric-mining-drill']
    if not actions:return None
    try:
        state=load_retirements(game)
        retired={xy(q)for q in(state or{}).get('targets',[])if q.get('phase')in('prepared','armed','complete')}
        retired|={xy(q)for q in(state or{}).get('targets',[])if q['retirement_id']in{r['retirement_id']for r in(state or{}).get('receipts',[])}}
        old=_inactive_base_drill(game)
        if old:retired.add(xy(old))
        for q in actions:
            if q.get('name')=='electric-mining-drill'and _position(q.get('position'))and xy(q)in retired:
                return {'ok':False,'status':'blocked','reason':'retired_depleted_drill_position_rebuild_forbidden','position':q['position']}
    except(OSError,ValueError,KeyError,TypeError)as exc:
        return {'ok':False,'status':'blocked','reason':'drill_retirement_build_authority_invalid','detail':str(exc)}
    return None


SURVEY_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="drill_retirement_world_changed"}end
local rows={};local proto=prototypes.entity["electric-mining-drill"];local radius=proto.mining_drill_radius
if type(radius)~="number"or radius<=0 or radius>16 then return {ok=false,reason="drill_retirement_radius_unsupported"}end
for _,q in ipairs(x.drills)do local e=target(q.position,q.name)
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=q.direction
  or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="drill_retirement_registered_unit_changed"}end
 local amount=0;local tiles=0
 for _,ore in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},{e.position.x+radius,e.position.y+radius}},type="resource"})do
  if ore.amount>0 then amount=amount+ore.amount;tiles=tiles+1 end
 end
 local modules=e.get_module_inventory();local output=e.get_output_inventory()
 rows[#rows+1]={unit_number=e.unit_number,position=pos(e.position),direction=e.direction,radius=radius,
  remaining_any_resource=amount,resource_tiles=tiles,powered=e.is_connected_to_electric_network()and e.energy>0,
  normal_quality=e.quality.name=="normal",full_health=e.health>=e.max_health,minable=e.minable,
  empty_modules=not modules or modules.is_empty(),empty_output=not output or output.is_empty()}
end
return {ok=true,tick=game.tick,world_id=d.world_id,drills=rows}
'''


RETIRE_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="drill_retirement_world_changed"}end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.actor_unit then return {ok=false,reason="drill_retirement_actor_changed"}end
d.depleted_drill_retirements=d.depleted_drill_retirements or{}
local prior=d.depleted_drill_retirements[x.retirement_id]
if prior then
 if prior.phase=="complete"and prior.world_id==x.world_id and prior.catalog_fingerprint==x.catalog_fingerprint
  and prior.unit_number==x.unit_number and prior.actor_unit==x.actor_unit then return prior end
 return {ok=false,reason="drill_retirement_outcome_unknown",retirement_id=x.retirement_id}
end
local e=target(x.position,"electric-mining-drill")
if not e or e.unit_number~=x.unit_number or e.force~=f or e.surface~=s or e.direction~=x.direction
 or e.position.x~=x.position.x or e.position.y~=x.position.y then return {ok=false,reason="drill_retirement_registered_unit_changed"}end
if not e.minable or e.quality.name~="normal"or e.health<e.max_health then return {ok=false,reason="drill_retirement_repair_or_normal_quality_required"}end
local modules=e.get_module_inventory();local output=e.get_output_inventory()
if(modules and not modules.is_empty())or(output and not output.is_empty())then return {ok=false,reason="drill_retirement_empty_normal_cleanup_required"}end
local radius=e.prototype.mining_drill_radius
if type(radius)~="number"or radius<=0 or radius>16 then return {ok=false,reason="drill_retirement_radius_unsupported"}end
for _,ore in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},{e.position.x+radius,e.position.y+radius}},type="resource"})do
 if ore.amount>0 then return {ok=false,reason="drill_retirement_any_resource_still_in_native_radius"}end
end
local inv=a.get_main_inventory()
if not inv or not inv.valid or inv.get_insertable_count{name="electric-mining-drill",quality="normal"}<1 then return {ok=false,reason="drill_retirement_inventory_capacity_required"}end
local function normal_count()
 local total=0
 for i=1,#inv do local st=inv[i];if st.valid_for_read and st.name=="electric-mining-drill"and st.quality.name=="normal"then total=total+st.count end end
 return total
end
local before=normal_count()
local record={phase="intent",mode=x.mode,retirement_id=x.retirement_id,world_id=x.world_id,catalog_fingerprint=x.catalog_fingerprint,
 actor_unit=a.unit_number,unit_number=e.unit_number,position=pos(e.position),direction=e.direction,radius=radius,remaining_any_resource=0,
 before_drills=before,started_tick=game.tick,force_index=f.index,surface_index=s.index}
d.depleted_drill_retirements[x.retirement_id]=record
local ok,result=pcall(function()return a.mine_entity(e)end)
local after=normal_count()
if not ok or not result or e.valid or after~=before+1 then
 record.phase="unknown";record.after_drills=after;record.exception=tostring(result)
 return {ok=false,reason="drill_retirement_outcome_unknown",retirement_id=x.retirement_id}
end
record.ok=true;record.status="succeeded";record.phase="complete";record.after_drills=after;record.normal_drills_recovered=1
record.removed=true;record.completed_tick=game.tick
return record
'''


def execute_retire_depleted_drill(game,action):
    allowed={'type','retirement_id','expected_entity_unit','expected_entity_world_id','expected_actor_unit','reason'}
    if(set(action)-allowed or action.get('type')!='retire_depleted_drill'or type(action.get('expected_entity_unit'))is not int
            or action['expected_entity_unit']<1 or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or not isinstance(action.get('retirement_id'),str)or not action['retirement_id']or not isinstance(action.get('expected_entity_world_id'),str)
            or not action['expected_entity_world_id']):raise ValueError('retirement requires exact trusted identity envelope')
    if getattr(game,'backend','assisted')!='assisted':return {'ok':False,'status':'blocked','reason':'depleted_drill_retirement_requires_assisted_backend'}
    state=load_retirements(game,world_id=action['expected_entity_world_id'])
    if state is None:raise ValueError('trusted retirement checkpoint required')
    rows=[q for q in state['targets']if q['retirement_id']==action['retirement_id']]
    if len(rows)!=1 or rows[0]['unit_number']!=action['expected_entity_unit']or rows[0].get('phase')not in('armed','complete'):
        raise ValueError('retirement action is not the exact armed registered target')
    from factorio_ai.world_catalog import WorldCatalog
    catalog=WorldCatalog.from_dict(json.loads((_root(game)/'catalog.json').read_text()))
    products=catalog.entities['electric-mining-drill']['mining_products']
    if(catalog.fingerprint!=state['catalog_fingerprint']or len(products)!=1 or products[0].get('name')!='electric-mining-drill'
            or products[0].get('amount')!=1 or products[0].get('independent_probability',1)!=1):
        raise ValueError('pinned one ordinary drill mining yield required')
    q=rows[0];_authority_matches(game,{'world_id':state['world_id']},state,q)
    payload=dict(q,world_id=state['world_id'],catalog_fingerprint=state['catalog_fingerprint'],actor_unit=action['expected_actor_unit'],mode=MODE)
    try:
        receipt=game.query('local x=helpers.json_to_table('+_payload(payload)+');'+RETIRE_LUA)
        if not isinstance(receipt,dict):raise ValueError('unexpected native retirement receipt')
        if receipt.get('ok')is True:
            _validate_native(receipt,q,state['world_id'],state['catalog_fingerprint'])
            if receipt['actor_unit']!=action['expected_actor_unit']:raise ValueError('retirement receipt actor changed')
        elif receipt.get('ok')is not False or not isinstance(receipt.get('reason'),str):raise ValueError('unexpected native failure receipt')
        return receipt
    except Exception as exc:return {'ok':False,'status':'blocked','reason':UNKNOWN,'exception':str(exc)}


def _known_units(bank):
    result=set(bank.get('output_evidence',{}).get('drill_units',[]))
    result.update(q['unit_number']for q in bank.get('retained_units',[])if q.get('name')=='electric-mining-drill'and type(q.get('unit_number'))is int)
    values=bank.get('material_entity_units',{})
    if isinstance(values,dict):result.update(v for v in values.values()if type(v)is int)
    result.update(q['unit_number']for q in bank.get('native_drill_bindings',[])if type(q.get('unit_number'))is int)
    return {q for q in result if type(q)is int and q>0}


def registered_drills(game,obs,catalog):
    """Current bank geometry plus explicit inactive BaseCoalRecovery history."""
    root=_root(game);doc=json.loads((root/'mining-outposts.json').read_text())
    if doc.get('world_id')!=obs['world_id']or doc.get('catalog_fingerprint')!=catalog.fingerprint:raise ValueError('registered mining frame changed')
    actual={_key(q):q for q in obs['entities']};registered={};authorities={};protected={}
    ledger=load_retirements(game,world_id=obs['world_id'],catalog_fingerprint=catalog.fingerprint)
    paid={q['retirement_id']for q in(ledger or{}).get('receipts',[])}
    removed={_key(q)for q in(ledger or{}).get('targets',[])if q['retirement_id']in paid}
    base_path=root/'base-coal-recovery.json';base_history=json.loads(base_path.read_text())if base_path.exists()else{}
    known_protected=base_history.get('protected',[])if(base_history.get('phase')=='verified'and base_history.get('published')
        and base_history.get('world_id')==obs['world_id']and base_history.get('catalog_fingerprint')==catalog.fingerprint)else[]
    for index,bank in enumerate(doc.get('banks',[])):
        if not bank.get('ready'):continue
        plan=bank['plan'];positions={_key(q)for q in plan['entities']}
        pins=_known_units(bank)|{q['unit_number']for q in known_protected if q.get('name')=='electric-mining-drill'and _key(q)in positions}
        owner=dict(kind='outpost',index=index,plan=deepcopy(plan),plan_revision=_hash(plan),known_drill_units=sorted(pins))
        revision=_hash(owner);authorities[revision]=owner
        for spec in plan['entities']:
            live=actual.get(_key(spec))
            if not live:
                if _key(spec)in removed:continue
                raise ValueError('registered mining asset already missing; normal repair required before retirement')
            if spec['name']=='electric-mining-drill':
                if _key(spec)in registered:raise ValueError('ambiguous registered drill ownership')
                registered[_key(spec)]=dict(deepcopy(spec),unit_number=live['unit_number'],resource=plan['resource'],owner_revision=revision,
                    historically_pinned=live['unit_number']in owner['known_drill_units'])
            else:protected[_key(spec)]={k:deepcopy(live[k])for k in('name','position','unit_number','direction')if k in live}
    base=root/'base-coal-recovery.json'
    if base.exists():
        value=json.loads(base.read_text())
        if value.get('phase')=='verified'and value.get('published')and value.get('world_id')==obs['world_id']and value.get('catalog_fingerprint')==catalog.fingerprint:
            old=value['depleted_source'];spec=value['old_builder']['coal_plan']['drill'];live=actual.get(_key(spec))
            builder=json.loads((root/'factory-builder.json').read_text());history=builder.get('coal_plan',{}).get('retired_source',{})
            if(history.get('diagnostic',{}).get('unit_number')==old['unit_number']and history.get('drill')==spec and live):
                if live['unit_number']!=old['unit_number']:raise ValueError('inactive base coal historical unit replaced')
                owner=dict(kind='inactive-base-coal',plan=value['old_builder']['coal_plan'],plan_revision=_hash(value['old_builder']['coal_plan']),known_drill_units=[old['unit_number']])
                revision=_hash(owner);authorities[revision]=owner
                registered[_key(spec)]=dict(deepcopy(spec),unit_number=live['unit_number'],resource='coal',owner_revision=revision,historically_pinned=True)
    # Every other live registered drill is preserved even when it is currently
    # unpowered, backpressured or has no old per-position binding.
    return list(registered.values()),authorities,protected


def _authority_matches(game,obs,state,target):
    owner=state['authorities'][target['owner_revision']]
    if _hash(owner)!=target['owner_revision']or _hash(owner['plan'])!=owner['plan_revision']:raise ValueError('retirement owner snapshot changed')
    spec=next((q for q in owner['plan']['entities']if _key(q)==_key(target)),None)
    if not spec or spec.get('direction',0)!=target['direction']or target['unit_number']not in owner['known_drill_units']:
        raise ValueError('target lacks immutable registered owner/unit history')
    root=_root(game)
    if owner['kind']=='outpost':
        doc=json.loads((root/'mining-outposts.json').read_text())
        if doc.get('world_id')!=obs['world_id']or doc.get('catalog_fingerprint')!=state['catalog_fingerprint']or _hash(doc['banks'][owner['index']]['plan'])!=owner['plan_revision']:
            raise ValueError('retirement canonical bank changed before recovery')
    elif owner['kind']=='inactive-base-coal':
        doc=json.loads((root/'base-coal-recovery.json').read_text());builder=json.loads((root/'factory-builder.json').read_text())
        if(doc.get('world_id')!=obs['world_id']or doc.get('catalog_fingerprint')!=state['catalog_fingerprint']or not doc.get('published')or doc.get('phase')!='verified'
                or doc['depleted_source']['unit_number']!=target['unit_number']or builder['coal_plan']['retired_source']['diagnostic']['unit_number']!=target['unit_number']):
            raise ValueError('inactive base source ownership changed')
    else:raise ValueError('unsupported retirement owner')


def _native_records(game,world,ids):
    return game.query('local x=helpers.json_to_table('+_payload(dict(world_id=world,ids=ids))+r''');
if not d or d.world_id~=x.world_id then return {ok=false,reason="drill_retirement_world_changed"}end
local rows={}
for _,id in ipairs(x.ids)do local q=d.depleted_drill_retirements and d.depleted_drill_retirements[id]
 rows[#rows+1]={retirement_id=id,receipt=q or false}
end
return {ok=true,tick=game.tick,records=rows}
''')


class DepletedDrillRetirement:
    def __init__(self,game,builder,catalog,*,outposts=None,paired=None,steam=None):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.outposts,self.paired,self.steam=outposts,paired,steam
        self.path=Path(game.cfg.runtime_dir)/FILE
        self.state=load_retirements(game,catalog_fingerprint=catalog.fingerprint)or{};self.last_failure=None

    def _save(self):_atomic_json(self.path,self.state)

    def prepare(self,obs,parameters=None):
        if parameters not in(None,{}):raise ValueError('depleted_drill_retirement accepts no coordinates or parameters')
        try:
            original=load_retirements(self.game,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint)or{}
            if original and original.get('phase')!='verified'and any(q.get('phase')!='complete'for q in original['targets']):return {'ok':False,'reason':'existing_drill_retirement_must_resume'}
            drills,owners,protected=registered_drills(self.game,obs,self.catalog)
            if not 0<=len(drills)<=MAX_DRILLS:raise ValueError('bounded registered drill census required')
            native=self.game.query('local x=helpers.json_to_table('+_payload(dict(world_id=obs['world_id'],drills=drills))+');'+SURVEY_LUA)
            if not native.get('ok'):return native
            rows=native.get('drills',[])
            if len(rows)!=len(drills)or {q['unit_number']for q in rows}!={q['unit_number']for q in drills}:raise ValueError('incomplete native full-radius census')
            by_unit={q['unit_number']:q for q in drills};targets=deepcopy(original.get('targets',[]));blocked=[]
            for row in rows:
                q=by_unit[row['unit_number']]
                if row['remaining_any_resource']>0:
                    protected[_key(q)]={k:deepcopy(q[k])for k in('name','position','unit_number','direction')};continue
                if not q['historically_pinned']or not all(row.get(k)is True for k in('normal_quality','full_health','minable','empty_modules','empty_output')):
                    protected[_key(q)]={k:deepcopy(q[k])for k in('name','position','unit_number','direction')}
                    blocked.append(dict(unit_number=q['unit_number'],position=q['position'],historically_pinned=q['historically_pinned'],native=row));continue
                if any(t['unit_number']==q['unit_number']for t in targets):continue
                q=deepcopy(q);q['phase']='prepared';q['depletion_witness']=row
                q['retirement_id']=_hash([MODE,obs['world_id'],self.catalog.fingerprint,q['unit_number'],q['position'],q['direction'],q['owner_revision']]);targets.append(q)
            if len(targets)>MAX_DRILLS:raise ValueError('retirement history budget reached; explicit archival migration required')
            target_units={q['unit_number']for q in targets}
            protected={k:q for k,q in protected.items()if q['unit_number']not in target_units}
            return dict(ok=True,mode=MODE,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,phase='recover',targets=targets,
                authorities={**original.get('authorities',{}),**owners},receipts=deepcopy(original.get('receipts',[])),protected_units=list(protected.values()),
                prepared_tick=obs['tick'],census=native,blocked_cleanup=blocked,
                unregistered_drill_units=[q['unit_number']for q in obs['entities']if q['name']=='electric-mining-drill'and q['unit_number']not in by_unit])
        except(OSError,ValueError,KeyError,TypeError)as exc:return {'ok':False,'reason':'depleted_drill_retirement_context_invalid','detail':str(exc)}

    def _preserved(self,obs):
        actual={_key(q):q for q in obs['entities']}
        for q in self.state.get('protected_units',[]):
            live=actual.get(_key(q))
            if not live or any(live.get(k)!=q.get(k)for k in('unit_number','direction')):
                return {'status':'blocked','reason':'drill_retirement_preserved_source_asset_changed','entity':q}
        return None

    def step(self,obs,parameters=None):
        if parameters not in(None,{}):raise ValueError('depleted_drill_retirement accepts no coordinates or parameters')
        if self.state.get('phase')=='verified'or(self.state.get('blocked_cleanup')and all(q.get('phase')=='complete'for q in self.state['targets'])):
            ready=self.prepare(obs)
            if not ready.get('ok'):return dict(ready,status='blocked')
            self.state=ready;self._save()
        if not self.state:
            ready=self.prepare(obs)
            if not ready.get('ok'):return dict(ready,status='blocked')
            self.state=ready;self._save()
        if self.state['world_id']!=obs['world_id']or self.state['catalog_fingerprint']!=self.catalog.fingerprint:return {'status':'blocked','reason':'drill_retirement_world_or_catalog_changed'}
        issue=self._preserved(obs)
        if issue:return issue
        actual={_key(q):q for q in obs['entities']}
        for target in self.state['targets']:
            if target.get('phase')=='complete':continue
            try:_authority_matches(self.game,obs,self.state,target)
            except(OSError,ValueError,KeyError,TypeError)as exc:return {'status':'blocked','reason':'drill_retirement_registered_owner_changed','detail':str(exc)}
            sample=_native_records(self.game,obs['world_id'],[target['retirement_id']])
            if not sample.get('ok'):return dict(sample,status='blocked')
            receipt=sample['records'][0]['receipt']
            if receipt:
                try:_validate_native(receipt,target,obs['world_id'],self.catalog.fingerprint)
                except(ValueError,KeyError,TypeError)as exc:return {'status':'blocked','reason':UNKNOWN,'detail':str(exc)}
                row=dict(retirement_id=target['retirement_id'],native=receipt,previous_receipt=self.state['receipts'][-1]['receipt_id']if self.state['receipts']else None)
                row['receipt_id']=_hash(row);self.state['receipts'].append(row);target['phase']='complete';self._save();continue
            live=actual.get(_key(target))
            if not live:return {'status':'blocked','reason':UNKNOWN,'detail':'missing drill has no native normal recovery receipt'}
            if live.get('unit_number')!=target['unit_number']:return {'status':'blocked','reason':'drill_retirement_target_replaced'}
            if type(obs.get('actor_unit_number'))is not int or obs['actor_unit_number']<1:
                return {'status':'blocked','reason':'drill_retirement_live_bound_actor_required'}
            target['phase']='armed';self._save()
            return dict(type='retire_depleted_drill',retirement_id=target['retirement_id'],expected_entity_unit=target['unit_number'],
                expected_entity_world_id=obs['world_id'],expected_actor_unit=obs.get('actor_unit_number'),reason='normally recover exact registered fully exhausted electric drill; preserve collector/grid and recovered item')
        proof=self.proof(obs)
        if not proof:return {'status':'blocked','reason':'drill_retirement_postcondition_not_verified','evidence':self.last_failure}
        if self.state.get('blocked_cleanup'):return {'status':'blocked','reason':'depleted_drills_need_normal_repair_or_empty_inventory_cleanup','evidence':proof}
        self.state['phase']='verified';self._save()
        return {'status':'succeeded','reason':'normal drill items recovered with immutable receipts; original geometry retained','evidence':proof}

    def depletion_diagnostic(self,obs):
        if self.state and self.state.get('phase')!='verified':
            return dict(ok=True,applicable=True,active=True,pending_units=[q['unit_number']for q in self.state['targets']if q.get('phase')!='complete'])
        prepared=self.prepare(obs)
        if not prepared.get('ok'):return prepared
        complete={q['unit_number']for q in self.state.get('targets',[])if q.get('phase')=='complete'}
        new=[q['unit_number']for q in prepared['targets']if q['unit_number']not in complete]
        return dict(ok=True,applicable=bool(new),active=False,new_depleted_units=new,cleanup_required=prepared['blocked_cleanup'],
            unregistered_drill_units=prepared['unregistered_drill_units'],tick=prepared['census']['tick'],scope='read-only whole native radius, not drill status or working flag')

    def proof(self,obs,parameters=None):
        self.last_failure=None
        try:
            state=load_retirements(self.game,world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint)
            if not state or len(state['receipts'])!=len(state['targets']):return None
            issue=self._preserved(obs)
            if issue:self.last_failure=issue;return None
            actual={_key(q):q for q in obs['entities']}
            if any(_key(q)in actual for q in state['targets']):raise ValueError('retired position occupied again')
            native=_native_records(self.game,obs['world_id'],[q['retirement_id']for q in state['targets']])
            if not native.get('ok'):self.last_failure=native;return None
            if len(native.get('records',[]))!=len(state['targets']):raise ValueError('native retirement history missing')
            saved={q['retirement_id']:q['native']for q in state['receipts']}
            if any(q.get('receipt')!=saved.get(q['retirement_id'])for q in native['records']):raise ValueError('native normal recovery history changed')
            return dict(tick=obs['tick'],retired_drill_units=[q['unit_number']for q in state['targets']],normal_drills_recovered=len(state['receipts']),
                receipts=[q['receipt_id']for q in state['receipts']],preserved_assets=len(state['protected_units']),blocked_cleanup=state.get('blocked_cleanup',[]),
                scope='actual normal inventory recovery; original mine geometry/ports/history preserved; no remaining-resource or throughput guarantee')
        except(OSError,ValueError,KeyError,TypeError)as exc:self.last_failure={'reason':'drill_retirement_proof_invalid','detail':str(exc)};return None


MINING_POTENTIAL_LUA=r'''
if not d or d.world_id~=x.world_id then return {ok=false,reason="mining_potential_world_changed"}end
local rows={};local drill=prototypes.entity["electric-mining-drill"];local radius=drill.mining_drill_radius
for _,bank in ipairs(x.banks)do local ore=prototypes.entity[bank.resource]
 if not ore or not ore.mineable_properties or ore.mineable_properties.mining_time<=0 then return {ok=false,reason="mining_potential_recipe_changed"}end
 local row={resource=bank.resource,index=bank.index,left=0,right=0,installed_left=0,installed_right=0,drills={}}
 for _,q in ipairs(bank.drills)do local e=target(q.position,"electric-mining-drill")
  if not e or e.force~=f or e.surface~=s or e.direction~=q.direction or e.position.x~=q.position.x or e.position.y~=q.position.y then
   return {ok=false,reason="mining_potential_unreceipted_registered_drill_missing",position=q.position}
  end
  if e.quality.name~="normal"or type(e.speed_bonus)~="number"or e.speed_bonus<0 or type(e.productivity_bonus)~="number"or e.productivity_bonus<0 then
   return {ok=false,reason="mining_potential_normal_profile_required"}end
  local amount=0
  for _,r in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},{e.position.x+radius,e.position.y+radius}},type="resource"})do
   if r.amount>0 then if r.name~=bank.resource then return {ok=false,reason="mining_potential_mixed_resource"}end;amount=amount+r.amount end
  end
  local p=e.drop_position;local side=q.side
  if not side then if p.y<bank.collector_y then side="left"elseif p.y>bank.collector_y then side="right"end end
  local receiver=target({x=math.floor(p.x)+.5,y=bank.collector_y},"transport-belt")
  local drop=(side=="left"or side=="right")and math.abs(p.y-bank.collector_y)<.49
   and(side=="left"and p.y<bank.collector_y or side=="right"and p.y>bank.collector_y)
   and receiver and receiver.force==f and receiver.surface==s and receiver.direction==4 and receiver.quality.name=="normal"
  local powered=e.is_connected_to_electric_network()and e.energy>0
  local offered=drill.mining_speed*60/ore.mineable_properties.mining_time*(1+e.speed_bonus)*(1+e.productivity_bonus)*bank.product_yield
  if side=="left"or side=="right"then row["installed_"..side]=row["installed_"..side]+offered end
  if drop and powered and amount>0 then row[side]=row[side]+offered end
  row.drills[#row.drills+1]={unit_number=e.unit_number,remaining=amount,powered=powered,drop_lane_verified=drop or false,side=side,offered_per_minute=offered,working=e.status==defines.entity_status.working}
 end
 rows[#rows+1]=row
end
return {ok=true,tick=game.tick,banks=rows,belt_speed=prototypes.entity["transport-belt"].belt_speed}
'''


def mining_capacity_summary(game,catalog,*,obs=None):
    """One bounded RO query; live resource-backed potential, never old nameplate."""
    try:
        root=_root(game);doc=json.loads((root/'mining-outposts.json').read_text());world=(obs or{}).get('world_id')or getattr(game,'expected_world',None)or doc.get('world_id')
        if doc.get('world_id')!=world or doc.get('catalog_fingerprint')!=catalog.fingerprint:raise ValueError('mining potential canonical frame changed')
        ledger=load_retirements(game,world_id=world,catalog_fingerprint=catalog.fingerprint)
        ids={q['retirement_id']for q in(ledger or{}).get('receipts',[])}
        retired={xy(q)for q in(ledger or{}).get('targets',[])if q['retirement_id']in ids};banks=[];historical=0
        for i,b in enumerate(doc['banks']):
            if not b.get('ready'):continue
            plan=b['plan'];side={xy(q):lane for lane,rows in plan.get('producer_rows',{}).items()for q in rows}
            specs=[q for q in plan['entities']if q['name']=='electric-mining-drill'];historical+=len(specs)
            products=catalog.entities[plan['resource']]['mining_products']
            if len(products)!=1 or products[0]['name']!=plan['resource']or products[0].get('independent_probability',1)!=1 or products[0].get('amount',0)<=0:
                raise ValueError('one deterministic native mining product required')
            banks.append(dict(index=i,resource=plan['resource'],collector_y=xy(plan.get('collector_port',plan['ports'][0]))[1],product_yield=products[0]['amount'],
                drills=[dict(q,side=side.get(xy(q)))for q in specs if xy(q)not in retired]))
        if historical>MAX_DRILLS:raise ValueError('mining capacity drill budget exceeded')
        native=game.query('local x=helpers.json_to_table('+_payload(dict(world_id=world,banks=banks))+');'+MINING_POTENTIAL_LUA)
        if not native.get('ok'):return native
        if len(native.get('banks',[]))!=len(banks):raise ValueError('incomplete mining source receipt')
        lane=native['belt_speed']*60*4*60;resources={};rows=[];seen_units=set()
        if not math.isfinite(lane)or lane<=0:raise ValueError('positive finite native belt capacity required')
        for planned,row in zip(banks,native['banks']):
            if row['index']!=planned['index']or row['resource']!=planned['resource']or len(row['drills'])!=len(planned['drills']):raise ValueError('mining source receipt identity changed')
            if any(type(q.get('unit_number'))is not int or q['unit_number']in seen_units or not q.get('drop_lane_verified')for q in row['drills']):raise ValueError('native mining drop lane or unique source identity unverified')
            ids=[q['unit_number']for q in row['drills']]
            if len(ids)!=len(set(ids)):raise ValueError('duplicate physical mining drill cannot multiply source capacity')
            seen_units.update(ids)
            if any(type(row.get(k))not in(int,float)or not math.isfinite(row[k])or row[k]<0 for k in('left','right','installed_left','installed_right')):
                raise ValueError('finite native mining potential required')
            potential=sum(min(row[side],lane)for side in('left','right'))
            row=dict(row,active_collector_ceiling_per_minute=potential);rows.append(row)
            resources[row['resource']]=resources.get(row['resource'],0)+potential
        return dict(ok=True,tick=native['tick'],world_id=world,resources=resources,banks=rows,historical_registered_drills=historical,
            effective_registered_drills=sum(len(q['drills'])for q in banks),paid_retired_units=[q['unit_number']for q in(ledger or{}).get('targets',[])if q['retirement_id']in ids],
            lane_capacity_per_minute=lane,measured_per_minute=None,scope='powered resource-backed native source potential with verified drop lanes; full belts can be idle, no sustained delivery guarantee')
    except(OSError,KeyError,TypeError,ValueError,ZeroDivisionError)as exc:return {'ok':False,'reason':'mining_capacity_summary_invalid','detail':str(exc)}
