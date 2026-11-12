"""Paid exact-loss pipe recovery for registered Petroleum and GasDistribution."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .planning import overlap,preflight

OWNERS={'petroleum':('basic-petroleum.json','basic_petroleum'),'gas':('gas-distribution.json','gas_distribution')}
FILE='infrastructure-repair.json'
ACTION='repair_registered_infrastructure'


def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _key(q):return q['name']+':'+str(q['position']['x'])+':'+str(q['position']['y'])
def _payload(value):return json.dumps(json.dumps(value,separators=(',',':')))
def _read(path):return json.loads(path.read_text(encoding='utf-8'))
def _same_frame(value,world,catalog):return value.get('world_id')==world and value.get('catalog_fingerprint')==catalog


def _authority(root,world,catalog):
    board=_read(root/'pcb-board.json');registry=_read(root/'fluid-sources.json')
    if not all(_same_frame(q,world,catalog)for q in(board,registry)):raise ValueError('registered infrastructure world or catalog changed')
    owners={}
    for name,(filename,block)in OWNERS.items():
        path=root/filename
        if not path.exists():continue
        state=_read(path)
        registered=deepcopy(board.get('blocks',{}).get(block))
        if isinstance(registered,dict)and'defense_scope_id'not in state.get('plan',{}):
            scope=registered.pop('defense_scope_id',None)
            if scope is not None and scope!=state.get('defense_scope_id'):
                raise ValueError('registered infrastructure defense scope changed: '+name)
        if(not _same_frame(state,world,catalog)or state.get('phase')!='verified'
                or registered!=state.get('plan')):
            raise ValueError('registered infrastructure owner or PCB plan changed: '+name)
        owners[name]=state
    return owners,board,registry


def _graph(owners,registry):
    nodes={};edges=[]
    def add(q,unit,fluid,boundary=False):
        key=_key(q)
        if key in nodes:
            if nodes[key]['unit_number']!=unit or nodes[key].get('fluid')!=fluid:raise ValueError('registered fluid node ownership conflicts')
            return
        nodes[key]=dict(name=q['name'],position=deepcopy(q['position']),direction=q.get('direction',0),unit_number=unit,
                        fluid=fluid,boundary=boundary)
    for name,state in owners.items():
        if name=='petroleum':
            gas={_key(q)for q in state['core']['gas_pipes']}
            raw=state['fluid_nodes']+state['core']['machines'];local_edges=state['fluid_edges']
        else:gas={_key(q)for q in state['nodes']};raw=state['nodes'];local_edges=state['edges']
        for q in raw:
            unit=state['bindings'].get(_key(q),q.get('unit_number'))
            if not unit:
                unit=next((p.get('unit_number')for p in registry['sources'].values()if p.get('name')==q['name']and p.get('position')==q['position']),None)
            if type(unit)is not int or unit<1:raise ValueError('registered pipe identity is not bound')
            boundary=name=='petroleum'and _key(q)==_key(state['source'])
            add(q,unit,None if q['name']=='oil-refinery'else('petroleum-gas'if _key(q)in gas else'crude-oil'),boundary)
        edges.extend(deepcopy(local_edges))
    for by_fluid in registry.get('handoffs',{}).values():
        for fluid,handoff in by_fluid.items():
            source=handoff.get('source',{});key=_key(source)if source.get('name')and source.get('position')else None
            if key not in nodes:continue
            if handoff.get('phase')!='bound':continue
            other=handoff['first_neighbor'];unit=other.get('unit_number')
            if type(unit)is not int or unit<1:raise ValueError('registered consumer handoff identity missing')
            add(other,unit,fluid,True);edges.append(dict(a=source,b=other))
    seen=set();unique=[]
    for edge in edges:
        if _key(edge['a'])not in nodes or _key(edge['b'])not in nodes:raise ValueError('registered fluid edge endpoint missing')
        identity=tuple(sorted((_key(edge['a'])+'@'+str(edge['a'].get('fluidbox_index',1)),_key(edge['b'])+'@'+str(edge['b'].get('fluidbox_index',1)))))
        if identity not in seen:seen.add(identity);unique.append(edge)
    if len(nodes)>1024:raise ValueError('registered infrastructure native survey bound exceeded')
    return list(nodes.values()),unique


def _foreign_witnesses(root,units):
    """Never retarget another controller's exact native preservation witness."""
    hits=[]
    owner_files={q[0]for q in OWNERS.values()}
    def visit(value,path,filename):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='infrastructure_repair_history':continue
                if key=='bindings':
                    if path!=filename or filename not in owner_files:
                        for node,unit in item.items():
                            if type(unit)is int and unit in units:hits.append(path+'.bindings.'+node)
                    continue
                if 'unit'in key and type(item)is int and item in units:hits.append(path+'.'+key)
                visit(item,path+'.'+key,filename)
        elif isinstance(value,list):
            for i,item in enumerate(value):visit(item,path+'['+str(i)+']',filename)
    for path in root.glob('*.json'):
        # Job receipts are immutable observations, not current ownership pins.
        if path.name in(FILE,'catalog.json','jobs.json'):continue
        visit(_read(path),path.name,path.name)
    return hits[:16]


def _validate(action,world=None):
    fields={'type','transaction_id','expected_world_id','expected_actor_unit','expected_catalog_fingerprint'}
    if(not isinstance(action,dict)or set(action)-fields-{'reason'}or not fields<=set(action)or action.get('type')!=ACTION
            or type(action.get('expected_actor_unit'))is not int or action['expected_actor_unit']<1
            or any(not isinstance(action.get(k),str)or not action[k]for k in('expected_world_id','expected_catalog_fingerprint'))
            or not isinstance(action.get('transaction_id'),str)or len(action['transaction_id'])!=64
            or any(c not in'0123456789abcdef'for c in action['transaction_id'])
            or('reason'in action and not isinstance(action['reason'],str))):raise ValueError('exact registered infrastructure repair envelope required')
    if world is not None and action['expected_world_id']!=world:raise ValueError('infrastructure repair world changed')


def _context(game,action):
    _validate(action,getattr(game,'expected_world',None))
    if getattr(game,'backend',None)!='assisted':raise ValueError('registered paid pipe repair requires ordinary assisted backend')
    root=Path(game.cfg.runtime_dir);ledger=_read(root/FILE);world=action['expected_world_id'];catalog=action['expected_catalog_fingerprint']
    if not _same_frame(ledger,world,catalog):raise ValueError('infrastructure repair ledger changed world or catalog')
    txn=ledger.get('transactions',{}).get(action['transaction_id'],{});payload=txn.get('payload',{})
    if(txn.get('phase')not in('prepared','native_complete','published','verified')or _hash(payload)!=action['transaction_id']
            or payload.get('action')!={k:v for k,v in action.items()if k not in('transaction_id','reason')}):
        raise ValueError('exact immutable infrastructure repair intent missing')
    owners,board,registry=_authority(root,world,catalog)
    if _hash(registry)!=payload['registry_revision']or _hash(board)!=payload['board_revision']:
        raise ValueError('fluid registry or PCB reservations changed during infrastructure repair')
    for name,expected in payload['owners'].items():
        actual=_hash(owners[name]);publication=txn.get('publication',{}).get(name,{})
        if actual not in(expected,publication.get('after_revision')):raise ValueError('registered infrastructure owner changed during repair')
    repairs=payload.get('repairs')
    if(not isinstance(repairs,list)or not 1<=len(repairs)<=8
            or len({_key(q['entity'])for q in repairs})!=len(repairs)
            or len({q.get('old_unit')for q in repairs})!=len(repairs)):raise ValueError('one to eight exact distinct registered pipe losses required')
    for q in repairs:
        spec=q['entity'];p=spec.get('position')
        if(q.get('owner')not in OWNERS or spec.get('name')!='pipe'
                or type(q.get('old_unit'))is not int or q['old_unit']<1
                or not isinstance(p,dict)or set(p)!={'x','y'}or any(type(v)not in(int,float)or not math.isfinite(v)for v in p.values())
                or spec not in owners[q['owner']]['plan']['entities']):raise ValueError('repair must restore an exact registered ordinary pipe footprint')
    if _foreign_witnesses(root,{q['old_unit']for q in repairs}):raise ValueError('lost pipe has an exact preservation witness requiring explicit revalidation')
    return dict(payload,transaction_id=action['transaction_id'])


def execute_repair_registered_infrastructure(game,action):
    payload=_context(game,action)
    try:receipt=game.query('local x=helpers.json_to_table('+_payload(payload)+');'+REPAIR_INFRASTRUCTURE_LUA)
    except Exception as exc:return dict(ok=False,status='blocked',reason='infrastructure_repair_outcome_unknown',original_reason=type(exc).__name__+': '+str(exc))
    if not isinstance(receipt,dict)or type(receipt.get('ok'))is not bool:
        return dict(ok=False,status='blocked',reason='infrastructure_repair_outcome_unknown',original_reason='invalid_native_repair_receipt')
    if receipt['ok']:
        if not _valid_receipt(receipt,payload,action['transaction_id']):
            return dict(receipt,ok=False,status='blocked',reason='infrastructure_repair_outcome_unknown',original_reason='native_paid_receipt_validation_failed')
    elif receipt.get('effects_started')or receipt.get('paid_pipes',0)>0 or receipt.get('reason')=='infrastructure_prior_effects_uncertain':
        return dict(receipt,reason='infrastructure_repair_outcome_unknown',original_reason=receipt.get('reason'))
    return receipt


class InfrastructureRepair:
    def __init__(self,game,builder,catalog,*,petroleum=None,gas=None):
        self.game,self.builder,self.catalog=game,builder,catalog
        self.path=Path(game.cfg.runtime_dir)/FILE;self.refs={'petroleum':petroleum,'gas':gas};self.last_diagnostic=None

    def _ledger(self,obs):
        if(not obs.get('ok',True)or not obs.get('world_id')or type(obs.get('actor_unit_number'))is not int):raise ValueError('live actor/world observation required')
        value=_read(self.path)if self.path.exists()else dict(world_id=obs['world_id'],catalog_fingerprint=self.catalog.fingerprint,transactions={})
        if not _same_frame(value,obs['world_id'],self.catalog.fingerprint):raise ValueError('infrastructure repair checkpoint frame changed')
        return value

    def scan(self,obs):
        """Bounded read-only exact native loss/peer survey of the registered owners."""
        try:
            self._ledger(obs);owners,board,registry=_authority(self.path.parent,obs['world_id'],self.catalog.fingerprint)
            nodes,edges=_graph(owners,registry)
            args=dict(action=dict(expected_world_id=obs['world_id'],expected_actor_unit=obs['actor_unit_number']),nodes=nodes,edges=edges,mode='scan',repairs=[])
            native=self.game.query('local x=helpers.json_to_table('+_payload(args)+');'+REPAIR_GUARD_LUA+SCAN_INFRASTRUCTURE_LUA)
            if not native.get('ok'):return native
            repairs=[]
            for missing in native.get('missing',[])or[]:
                matches=[(name,e,state['bindings'].get(_key(e)))for name,state in owners.items()for e in state['plan']['entities']if _key(e)==missing['key']]
                if len(matches)!=1 or matches[0][1]['name']!='pipe':return dict(ok=False,reason='missing infrastructure is not a single repairable registered pipe',entity=missing)
                name,e,unit=matches[0]
                if any(_key(p)==_key(e)for p in owners[name]['plan'].get('ports',[])):
                    return dict(ok=False,reason='published port replacement requires explicit source migration',entity=missing)
                repairs.append(dict(owner=name,entity=deepcopy(e),old_unit=unit))
            if len(repairs)>8:return dict(ok=False,reason='registered pipe loss batch exceeds eight',missing_count=len(repairs))
            if repairs:
                witnesses=_foreign_witnesses(self.path.parent,{q['old_unit']for q in repairs})
                if witnesses:return dict(ok=False,reason='lost pipe has a preserved identity witness',witnesses=witnesses)
                for q in repairs:
                    p=q['entity']['position'];area=[[p['x']-.5,p['y']-.5],[p['x']+.5,p['y']+.5]]
                    for name,block in board['blocks'].items():
                        if name!=OWNERS[q['owner']][1]and block.get('reserved_area')and overlap(area,block['reserved_area']):
                            return dict(ok=False,reason='lost pipe footprint now conflicts with another reserved production area',block=name)
            return dict(ok=True,owners=owners,registry=registry,nodes=nodes,edges=edges,repairs=repairs,native=native)
        except(ValueError,KeyError,TypeError,OSError)as exc:return dict(ok=False,reason=str(exc))

    def prepare(self,obs):
        ready=self.scan(obs)
        if not ready.get('ok')or not ready['repairs']:return ready
        checked=preflight(self.game,self.builder,obs,[q['entity']for q in ready['repairs']])
        if not checked.get('ok')or checked.get('clear_obstacles'):return dict(ok=False,reason='registered pipe replacement whole preflight failed',evidence=checked)
        action=dict(type=ACTION,expected_world_id=obs['world_id'],expected_actor_unit=obs['actor_unit_number'],expected_catalog_fingerprint=self.catalog.fingerprint)
        payload=dict(action=action,owners={k:_hash(v)for k,v in ready['owners'].items()},registry_revision=_hash(ready['registry']),
                     board_revision=_hash(_read(self.path.parent/'pcb-board.json')),
                     nodes=ready['nodes'],edges=ready['edges'],repairs=ready['repairs'],created_tick=ready['native']['tick'])
        return dict(ok=True,payload=payload,transaction_id=_hash(payload),material_cost={'pipe':len(ready['repairs'])},native=ready['native'])

    def _publish(self,ledger,key,txn):
        receipt=txn['receipt'];by_old={q['old_unit']:q for q in receipt['replacements']}
        if 'publication'not in txn:
            publication={}
            for name in {q['owner']for q in txn['payload']['repairs']}:
                state=_read(self.path.parent/OWNERS[name][0])
                if _hash(state)!=txn['payload']['owners'][name]:raise ValueError('owner publication CAS baseline changed')
                after=deepcopy(state);rows=[]
                for q in txn['payload']['repairs']:
                    if q['owner']!=name:continue
                    r=by_old[q['old_unit']];node=_key(q['entity'])
                    if after['bindings'].get(node)!=q['old_unit']:raise ValueError('lost pipe binding changed before publication')
                    after['bindings'][node]=r['unit_number'];rows.append(deepcopy(r))
                history=dict(transaction_id=key,world_id=receipt['world_id'],actor_unit=receipt['actor_unit'],tick=receipt['tick'],replacements=rows)
                after.setdefault('infrastructure_repair_history',[]).append(history)
                publication[name]=dict(before_revision=_hash(state),after_revision=_hash(after),history=history)
            txn['publication']=publication;_atomic_json(self.path,ledger)
        for name,intent in txn['publication'].items():
            path=self.path.parent/OWNERS[name][0];state=_read(path);actual=_hash(state)
            if actual==intent['after_revision']:continue
            if actual!=intent['before_revision']:raise ValueError('infrastructure owner publication changed outside explicit CAS')
            for row in intent['history']['replacements']:
                if state['bindings'].get(row['key'])!=row['old_unit']:raise ValueError('infrastructure publication old identity changed')
                state['bindings'][row['key']]=row['unit_number']
            state.setdefault('infrastructure_repair_history',[]).append(deepcopy(intent['history']))
            if _hash(state)!=intent['after_revision']:raise ValueError('infrastructure publication derived revision mismatch')
            _atomic_json(path,state)
        txn['phase']='published';_atomic_json(self.path,ledger)
        for name in txn['publication']:
            if self.refs[name]is not None:self.refs[name].state=_read(self.path.parent/OWNERS[name][0])

    def step(self,obs):
        """Watch hook: only actions interrupt upkeep; diagnostics never stop defense."""
        try:
            ledger=self._ledger(obs)
            pending=[(k,v)for k,v in ledger['transactions'].items()if v['phase']!='verified']
            if len(pending)>1:raise ValueError('multiple unresolved infrastructure transactions')
            if pending:
                key,txn=pending[0];action=dict(txn['payload']['action'],transaction_id=key)
                # The paid marker is the sole authority for adopting replacement units.
                context=_context(self.game,action);context['mode']='inspect'
                native=self.game.query('local x=helpers.json_to_table('+_payload(context)+');'+REPAIR_GUARD_LUA+SCAN_INFRASTRUCTURE_LUA)
                if not native.get('ok'):raise ValueError(native.get('reason','native infrastructure recovery inspection failed'))
                if native.get('receipt'):
                    if not _valid_receipt(native['receipt'],txn['payload'],key):raise ValueError('paid infrastructure receipt failed exact validation')
                    txn['receipt']=deepcopy(native['receipt']);txn['phase']='native_complete';_atomic_json(self.path,ledger)
                    self._publish(ledger,key,txn)
                    txn['phase']='verified';_atomic_json(self.path,ledger)
                    self.last_diagnostic=dict(ok=True,reason='paid registered pipe recovery published',receipt=txn['receipt']);return None
                if txn['phase']!='prepared':raise ValueError('published infrastructure replacement lost its native paid marker')
                count=len(txn['payload']['repairs'])
                if native.get('actor_plain_pipe_items',0)<count:
                    supplied=self.builder.bootstrap.ensure_item(obs,'pipe',count)
                    return supplied if supplied and supplied.get('type')else None
                return dict(action,reason='normally restore exact registered infrastructure losses with prepaid pipe items')
            ready=self.prepare(obs)
            if not ready.get('ok'):self.last_diagnostic=ready;return None
            if 'payload'not in ready:self.last_diagnostic=dict(ok=True,reason='registered fluid infrastructure intact');return None
            count=ready['material_cost']['pipe']
            if ready['native'].get('actor_plain_pipe_items',0)<count:
                supplied=self.builder.bootstrap.ensure_item(obs,'pipe',count)
                self.last_diagnostic=dict(reason='normally prepay registered pipe replacements',required=count)
                return supplied if supplied and supplied.get('type')else None
            key=ready['transaction_id'];ledger['transactions'][key]=dict(phase='prepared',payload=ready['payload'])
            _atomic_json(self.path,ledger)
            return dict(ready['payload']['action'],transaction_id=key,reason='normally restore exact registered infrastructure losses with prepaid pipe items')
        except(ValueError,KeyError,TypeError,OSError)as exc:
            self.last_diagnostic=dict(ok=False,reason=str(exc));return None


def _valid_receipt(receipt,payload,key):
    if not isinstance(receipt,dict):return False
    rows=receipt.get('replacements',[]);repairs=payload['repairs'];action=payload['action']
    if(not isinstance(rows,list)or any(not isinstance(q,dict)for q in rows)
            or any(type(receipt.get(k))is not int or receipt[k]<0 for k in('paid_pipes','main_before','main_after','actor_unit','tick'))
            or any(type(q.get('unit_number'))is not int for q in rows)
            or receipt.get('ok')is not True or receipt.get('transaction_id')!=key or receipt.get('world_id')!=action['expected_world_id']
            or receipt.get('catalog_fingerprint')!=action['expected_catalog_fingerprint']or receipt.get('actor_unit')!=action['expected_actor_unit']
            or receipt.get('paid_pipes')!=len(repairs)or receipt.get('main_before',0)-receipt.get('main_after',0)!=len(repairs)
            or len(rows)!=len(repairs)or len({q.get('unit_number')for q in rows})!=len(rows)):return False
    return all(row.get('old_unit')==old['old_unit']and row.get('key')==_key(old['entity'])and row.get('owner')==old['owner']
               and row.get('position')==old['entity']['position']and row.get('paid_pipes')==1
               and type(row.get('unit_number'))is int and row['unit_number']>0 and row['unit_number']!=old['old_unit']
               for row,old in zip(rows,repairs))


REPAIR_GUARD_LUA=r'''
local operation_marker=nil
local function reject(reason)return {ok=false,status="blocked",reason=reason,
 paid_pipes=operation_marker and operation_marker.paid_pipes or 0,
 effects_started=operation_marker~=nil,replacements=operation_marker and operation_marker.replacements or nil}end
local frame=x.action
if not d or d.world_id~=frame.expected_world_id then return reject("infrastructure_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=frame.expected_actor_unit then return reject("infrastructure_actor_changed")end
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
local repairs={};for _,q in ipairs(x.repairs)do repairs[key(q.entity)]=q end
local marker=x.transaction_id and d.infrastructure_repairs and d.infrastructure_repairs[x.transaction_id]
operation_marker=marker
if marker and(marker.world_id~=frame.expected_world_id or marker.actor_unit~=frame.expected_actor_unit)then return reject("infrastructure_paid_marker_identity_changed")end
if marker and marker.phase~="complete"then return reject("infrastructure_prior_effects_uncertain")end
local replacements={}
if marker then for _,q in ipairs(marker.receipt.replacements)do replacements[q.key]=q.unit_number end end
local live={};local maps={};local missing={};local expected={}
local main=a.get_main_inventory()
local function pipe_count()
 if not main or not main.valid then return nil end
 local count=0
 for i=1,#main do local st=main[i];if st.valid_for_read and st.name=="pipe"then
  if st.quality.name~="normal"or st.prototype.type~="item"or st.health~=1 or st.spoil_tick~=0
   or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return nil end
  count=count+st.count
 end end;return count
end
for _,q in ipairs(x.nodes)do
 local k=key(q);local e=target(q.position,q.name);local unit=replacements[k]or q.unit_number
 if not e or not e.valid then
  if q.name~="pipe"or marker or(x.mode~="scan"and not repairs[k])then return reject("infrastructure_required_node_missing")end
  missing[#missing+1]={key=k,unit_number=unit}
 else
  if e.force~=f or e.surface~=s or e.unit_number~=unit or e.position.x~=q.position.x or e.position.y~=q.position.y
   or e.quality.name~="normal"or(q.name=="pipe-to-ground"and e.direction~=q.direction)then return reject("infrastructure_known_node_replaced_or_changed")end
  for name in pairs(e.get_fluid_contents())do if q.fluid and name~=q.fluid then return reject("infrastructure_typed_node_contaminated")end end
  live[k]=e
  if q.name=="oil-refinery"then
   local recipe=e.get_recipe();if not recipe or recipe.name~="basic-oil-processing"then return reject("infrastructure_refinery_recipe_changed")end
   local map={};for i=1,5 do local ok,proto=pcall(function()return e.get_fluid_box_prototype(i)end)
    if ok and proto then for _,p in pairs(proto.index and{proto}or proto)do
     if(p.index~=2 and p.index~=5)or map[p.index]then return reject("infrastructure_runtime_fluidbox_mapping_changed")end
     map[p.index]=i
    end end
   end
   if not map[2]or not map[5]or map[2]==map[5]then return reject("infrastructure_runtime_fluidbox_mapping_changed")end
   maps[k]=map
  end
 end
end
local function edge_index(q)if maps[key(q)]then return maps[key(q)][q.fluidbox_index or 1]end;return q.fluidbox_index or 1 end
local function proof()
 local count=0
 for _,edge in ipairs(x.edges)do local ak,bk=key(edge.a),key(edge.b);local ea,eb=live[ak],live[bk]
  if ea and eb then
   local ai,bi=edge_index(edge.a),edge_index(edge.b);if not ai or not bi then return false,"infrastructure_runtime_fluidbox_mapping_missing"end
   local forward=false;local reverse=false
   for _,p in pairs(ea.get_fluid_box_neighbours(ai)or{})do if p.entity==eb and p.index==bi then forward=true end end
   for _,p in pairs(eb.get_fluid_box_neighbours(bi)or{})do if p.entity==ea and p.index==ai then reverse=true end end
   if not forward or not reverse then return false,"infrastructure_reciprocal_fluid_edge_missing"end
   expected[ak.."@"..ai]=expected[ak.."@"..ai]or{};expected[ak.."@"..ai][eb.unit_number..":"..bi]=true
   expected[bk.."@"..bi]=expected[bk.."@"..bi]or{};expected[bk.."@"..bi][ea.unit_number..":"..ai]=true;count=count+1
  end
 end
 for _,q in ipairs(x.nodes)do local k=key(q);local e=live[k]
  if e and not q.boundary then
   local indices=q.name=="oil-refinery"and{1,2}or{1}
   for _,i in ipairs(indices)do for _,p in pairs(e.get_fluid_box_neighbours(i)or{})do
    if not(expected[k.."@"..i]or{})[p.entity.unit_number..":"..p.index]then return false,"infrastructure_undeclared_fluid_peer"end
   end end
  end
  if e and q.name~="oil-refinery"then
   if not e.has_fluid_segment(1)then return false,"infrastructure_native_segment_missing"end
   local extent=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
   if extent.right_bottom.x-extent.left_top.x>limit or extent.right_bottom.y-extent.left_top.y>limit then return false,"infrastructure_pipeline_extent_exceeded"end
  end
 end
 return true,count
end
'''

SCAN_INFRASTRUCTURE_LUA=r'''
local good,detail=proof();if not good then return reject(detail)end
local stock=pipe_count();if not stock then return reject("infrastructure_actor_pipe_metadata_unsupported")end
return {ok=true,tick=game.tick,missing=missing,reciprocal_edges=detail,actor_plain_pipe_items=stock,receipt=marker and marker.receipt or nil}
'''

REPAIR_INFRASTRUCTURE_LUA=REPAIR_GUARD_LUA+r'''
if marker then local good,detail=proof();if not good then return reject(detail)end
 local result={};for k,v in pairs(marker.receipt)do result[k]=v end;result.reused_receipt=true;return result end
if #missing~=#x.repairs then return reject("infrastructure_all_exact_losses_must_be_absent")end
local before=pipe_count();if not before or before<#x.repairs then return reject("infrastructure_prepaid_plain_pipe_items_required")end
for _,q in ipairs(x.repairs)do local p=q.entity.position
 if target(p,"pipe")or not s.can_place_entity{name="pipe",position=p,direction=0,force=f,quality="normal"}
  or s.count_entities_filtered{area={{p.x-.49,p.y-.49},{p.x+.49,p.y+.49}},type="resource"}>0 then return reject("infrastructure_missing_footprint_no_longer_clear")end
 for _,other in pairs(s.find_entities_filtered{area={{p.x-1.6,p.y-1.6},{p.x+1.6,p.y+1.6}}})do
  if other.valid and next(other.prototype.fluidbox_prototypes or{})and not live[key(other)]then
   return reject("infrastructure_new_foreign_fluid_neighbour")
  end
  if other.valid and other.name=="pipe"and math.abs(other.position.x-p.x)+math.abs(other.position.y-p.y)==1 then
   local declared=false
   for _,edge in ipairs(x.edges)do
    if(key(edge.a)==key(q.entity)and key(edge.b)==key(other))or(key(edge.b)==key(q.entity)and key(edge.a)==key(other))then declared=true;break end
   end
   if not declared then return reject("infrastructure_new_undeclared_known_pipe_peer")end
  end
 end
end
local good,detail=proof();if not good then return reject(detail)end
d.infrastructure_repairs=d.infrastructure_repairs or{}
marker={phase="intent",world_id=frame.expected_world_id,actor_unit=frame.expected_actor_unit,transaction_id=x.transaction_id,
 main_before=before,replacements={},paid_pipes=0,tick=game.tick};d.infrastructure_repairs[x.transaction_id]=marker
operation_marker=marker;marker.phase="building"
for _,q in ipairs(x.repairs)do
 local index=nil;for i=1,#main do if main[i].valid_for_read and main[i].name=="pipe"and main[i].count>0 then index=i;break end end
 if not index then return reject("infrastructure_paid_pipe_source_changed")end
 main[index].count=main[index].count-1;marker.paid_pipes=marker.paid_pipes+1
 local e=s.create_entity{name="pipe",position=q.entity.position,direction=0,force=f,quality="normal",raise_built=true,player=a.player}
 if not e then marker.phase="uncertain_build";return reject("infrastructure_paid_pipe_build_outcome_unknown")end
 local k=key(q.entity);live[k]=e
 marker.replacements[#marker.replacements+1]={key=k,owner=q.owner,position=q.entity.position,old_unit=q.old_unit,unit_number=e.unit_number,paid_pipes=1}
end
local after=pipe_count();local good,detail=proof()
if not good or not after or before-after~=#x.repairs or game.tick~=marker.tick then return reject(good and"infrastructure_material_conservation_failed"or detail)end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",transaction_id=x.transaction_id,world_id=frame.expected_world_id,
 catalog_fingerprint=frame.expected_catalog_fingerprint,actor_unit=frame.expected_actor_unit,replacements=marker.replacements,
 paid_pipes=marker.paid_pipes,main_before=before,main_after=after,tick=game.tick,reciprocal_edges=detail}
return marker.receipt
'''
