from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.registered_belt_repair import (RegisteredBeltRepair,execute_repair_registered_belts,FILE,REPAIR_BELTS_LUA,
    execute_reconcile_registered_belts,approved_reconciliation)
try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


@unittest.skipUnless(LuaRuntime,'optional executable Lua fixture')
class RegisteredBeltRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        def decode(v):
            if not hasattr(v,'items'):return v
            keys=list(v.keys())
            if keys and all(type(k)is int for k in keys)and sorted(keys)==list(range(1,len(keys)+1)):return [decode(v[i])for i in range(1,len(keys)+1)]
            return {k:decode(q)for k,q in v.items()}
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''
f={};s={};d={world_id="w"};game={tick=20};next_unit=200;world={};spills={};prototypes={entity={}}
main={valid=true};local data={valid_for_read=true,name="transport-belt",count=10,quality={name="normal"},prototype={type="item"},health=1,spoil_tick=0}
main[1]=setmetatable({},{__index=data,__newindex=function(q,k,v)data[k]=v;if k=="count"and v==0 then data.valid_for_read=false end end})
a={valid=true,force=f,surface=s,unit_number=11,get_main_inventory=function()return main end}
function key(p)return p.x..":"..p.y end
function belt(unit,y,created)
 local e={valid=true,name="transport-belt",type="transport-belt",position={x=.5,y=y},direction=0,unit_number=unit,force=f,surface=s,quality={name="normal"},health=150,max_health=150}
 e.items=created and{{},{}}or{{{name="plastic-bar",quality="normal",count=1}},{{name="plastic-bar",quality="normal",count=1}}}
 e.get_transport_line=function(lane)return {get_contents=function()return contamination and{{name="iron-plate",quality="normal",count=1}}or e.items[lane]end}end
 setmetatable(e,{__index=function(q,k)if k=="belt_neighbours"then
  local before=world[key{x=.5,y=y+1}];local after=world[key{x=.5,y=y-1}]
  return {inputs=before and{before}or{},outputs=after and{after}or{}}
 end end});world[key(e.position)]=e;return e
end
for _,i in ipairs{0,2,4,6}do belt(100+i,i+.5)end
for _,i in ipairs{1,3,5}do spills[#spills+1]={valid=true,name="item-on-ground",type="item-entity",position={x=.6,y=i+.6},stack={valid_for_read=true,name="plastic-bar",count=4,quality={name="normal"},prototype={type="item"},health=1,spoil_tick=0}}end
remnant={valid=true,name="transport-belt-remnants",type="corpse",position={x=.5,y=1.5}};spills[#spills+1]=remnant
function target(p,name)return world[key(p)]end
s.can_place_entity=function(spec)return not world[key(spec.position)]end
s.find_entities_filtered=function(spec)
 if spec.force=="enemy"then return {}end
 if spec.type=="resource"then return resources and{{name="iron-ore"}}or{}end
 local out={};for _,e in ipairs(spills)do local p=e.position
  if e.valid and p.x>=spec.area[1][1]and p.x<=spec.area[2][1]and p.y>=spec.area[1][2]and p.y<=spec.area[2][2]then out[#out+1]=e end
 end;return out
end
s.create_entity=function(spec)
 if not spec.preserve_ghosts_and_corpses then remnant.valid=false end
 next_unit=next_unit+1;local e=belt(next_unit,spec.position.y,true)
 if auto_pickup then for _,ground in ipairs(spills)do
  if ground.valid and ground.type=="item-entity"and math.abs(ground.position.y-spec.position.y)<.49 then
   e.items[2][#e.items[2]+1]={name=ground.stack.name,quality="normal",count=ground.stack.count};ground.valid=false
  end
 end end;return e
end
''')
        self.game=SimpleNamespace(expected_world='w',backend='assisted',cfg=SimpleNamespace(runtime_dir=self.root),query=lambda script:
            decode(self.lua.execute('return(function() '+' '.join(script.splitlines())+' end)()')))
        self.cat=WorldCatalog({'version':'2.1.20'});self.builder=FactoryBuilder(self.game,SimpleNamespace(ensure_item=Mock(return_value={'type':'take_mall_stock'})),self.cat)
        nodes=[dict(name='transport-belt',position=dict(x=.5,y=i+.5),direction=0)for i in range(7)]
        inlet=dict(kind='item',item='plastic-bar',direction='input',facing=0,position=nodes[0]['position'])
        self.owner=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,phase='verified',plan=dict(entities=nodes,ports=[inlet],underground_pairs=[]),
            core=dict(ports=[inlet]),tap_sources={},plastic_source=dict(name='transport-belt',kind='item',item='plastic-bar',direction='output',facing=0,position=nodes[-1]['position'],unit_number=106),
            audit=dict(belt_contracts=[dict(q,allowed_items=['plastic-bar'])for q in nodes]),bindings={q['name']+':0.5:'+str(i+.5):100+i for i,q in enumerate(nodes)})
        self.board=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,blocks={'advanced_circuit_production':deepcopy(self.owner['plan'])})
        self.write('advanced-circuits.json',self.owner);self.write('pcb-board.json',self.board)
        self.obs=dict(ok=True,world_id='w',actor_unit_number=11,entities=[]);self.ref=SimpleNamespace(state=deepcopy(self.owner))
        self.ctrl=RegisteredBeltRepair(self.game,self.builder,self.cat,advanced=self.ref)

    def write(self,name,value):(self.root/name).write_text(json.dumps(value))
    def read(self,name):return json.loads((self.root/name).read_text())

    def test_prepare_is_readonly_and_accepts_protected_plain_spills_without_removing_them(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')};p=self.ctrl.prepare(self.obs)
        self.assertTrue(p['ok'],p);self.assertEqual(p['cost'],{'transport-belt':3});self.assertEqual(p['native']['debris_count'],3)
        self.assertEqual(before,{p.name:p.read_bytes()for p in self.root.glob('*.json')});self.assertEqual(self.lua.eval('spills[1].stack.count'),4)

    def test_normal_engine_pickup_conserves_combined_floor_and_lines_with_durable_before(self):
        self.lua.execute('auto_pickup=true');action=self.ctrl.step(self.obs)
        receipt=execute_repair_registered_belts(self.game,action);self.assertTrue(receipt['ok'],receipt)
        self.assertEqual(receipt['before_goods']['combined'],receipt['after_goods']['combined'])
        self.assertEqual(receipt['before_goods']['floor'],{'plastic-bar':12});self.assertEqual(receipt['after_goods']['floor'],{})
        self.assertEqual(receipt['witness_version'],2)
        self.assertEqual(self.lua.eval('d.registered_belt_repairs['+json.dumps(action['transaction_id'])+'].before.main_belts'),10)
        self.assertEqual(self.lua.eval('#d.registered_belt_repairs['+json.dumps(action['transaction_id'])+'].before.goods.ground_stacks'),3)

    def legacy_fixture(self):
        action=self.ctrl.step(self.obs);receipt=execute_repair_registered_belts(self.game,action)
        self.assertTrue(receipt['ok'],receipt)
        # Model the first installed version's saved marker, which had no before
        # snapshot and failed its per-ground-entity check after ordinary pickup.
        self.lua.execute('local m=d.registered_belt_repairs['+json.dumps(action['transaction_id'])+'];m.phase="building";m.version=nil;m.before=nil;m.receipt=nil')
        failure=dict(ok=False,reason='infrastructure_repair_outcome_unknown',original_reason='registered_belt_spilled_goods_changed',
            paid_belts=receipt['paid_belts'],replacements=receipt['replacements'])
        self.write('jobs.json',{'jobs':{'watch':dict(world_id='w',uncertain_batch_receipt=deepcopy(failure))}})
        (self.root/'job-events.jsonl').write_text(json.dumps(dict(key='watch',event='action_intent',tick=20,action=action))+'\n')
        return action,failure

    def test_reviewed_legacy_epoch_is_metadata_only_and_never_invents_old_baselines(self):
        action,failure=self.legacy_fixture();key=action['transaction_id'];original=self.read(FILE)['transactions'][key]['payload']
        self.assertFalse(approved_reconciliation(self.root,'watch',key));self.assertIsNone(self.ctrl.step(self.obs))
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')}
        proposal=self.ctrl.prepare_reconciliation(self.obs,'watch',key)
        self.assertTrue(proposal['ok'],proposal);self.assertEqual(before,{p.name:p.read_bytes()for p in self.root.glob('*.json')})
        self.assertTrue(self.ctrl.activate_reconciliation(proposal['proposal'])['ok'])
        self.assertTrue(approved_reconciliation(self.root,'watch',key));self.assertFalse(approved_reconciliation(self.root,'other',key))
        reconcile=self.ctrl.step(self.obs);self.assertEqual(reconcile['type'],'reconcile_registered_belts')
        # run_slice journals the new metadata action before invoking its handler.
        # That new intent must not be mistaken for a second original build.
        with (self.root/'job-events.jsonl').open('a')as out:
            out.write(json.dumps(dict(key='watch',event='action_intent',tick=21,action=reconcile))+'\n')
        count=self.lua.eval('main[1].count');r=execute_reconcile_registered_belts(self.game,reconcile)
        self.assertTrue(r['ok'],r);self.assertEqual(self.lua.eval('main[1].count'),count)
        self.assertEqual(r['mode'],'legacy_paid_belt_reconciliation_v1')
        self.assertEqual(r['historical_ground_conservation'],'unknown');self.assertFalse(r['material_baseline_available'])
        self.assertNotIn('before',r);self.assertNotIn('before_goods',r)
        self.assertEqual(self.lua.eval('d.registered_belt_repairs['+json.dumps(key)+'].phase'),'building')
        self.assertIsNone(self.ctrl.step(self.obs));self.assertTrue(self.ctrl.last_diagnostic['ok'],self.ctrl.last_diagnostic)
        txn=self.read(FILE)['transactions'][key];self.assertEqual(txn['phase'],'reconciled_legacy');self.assertEqual(txn['payload'],original)
        self.assertEqual(self.read('jobs.json')['jobs']['watch']['uncertain_batch_receipt'],failure)
        history=self.read('advanced-circuits.json')['registered_belt_repair_history']
        self.assertEqual(history[0]['receipt']['historical_ground_conservation'],'unknown')
        self.assertTrue(execute_reconcile_registered_belts(self.game,reconcile)['ok']);self.assertEqual(self.lua.eval('main[1].count'),count)
        self.assertEqual(len(self.read('advanced-circuits.json')['registered_belt_repair_history']),1)

    def test_legacy_review_rejects_changed_native_or_unreviewed_epoch_and_activation_cas(self):
        action,_=self.legacy_fixture();key=action['transaction_id']
        self.lua.execute('world["0.5:1.5"].unit_number=999')
        self.assertFalse(self.ctrl.prepare_reconciliation(self.obs,'watch',key)['ok'])
        self.setUp();action,_=self.legacy_fixture();key=action['transaction_id']
        proposal=self.ctrl.prepare_reconciliation(self.obs,'watch',key)['proposal']
        state=self.read(FILE);state['outside_progress']='keep';self.write(FILE,state)
        with self.assertRaises(ValueError):self.ctrl.activate_reconciliation(proposal)
        self.assertFalse(approved_reconciliation(self.root,'watch',key))
        with self.assertRaises(ValueError):execute_reconcile_registered_belts(self.game,dict(action,type='reconcile_registered_belts',epoch_id='0'*64))

    def test_reviewed_epoch_fails_when_historical_failure_or_marker_changes(self):
        action,_=self.legacy_fixture();key=action['transaction_id']
        proposal=self.ctrl.prepare_reconciliation(self.obs,'watch',key)['proposal'];self.ctrl.activate_reconciliation(proposal)
        reconcile=self.ctrl.step(self.obs)
        jobs=self.read('jobs.json');jobs['jobs']['watch']['uncertain_batch_receipt']['paid_belts']=2;self.write('jobs.json',jobs)
        self.assertFalse(approved_reconciliation(self.root,'watch',key))
        with self.assertRaises(ValueError):execute_reconcile_registered_belts(self.game,reconcile)
        self.setUp();action,_=self.legacy_fixture();key=action['transaction_id']
        proposal=self.ctrl.prepare_reconciliation(self.obs,'watch',key)['proposal'];self.ctrl.activate_reconciliation(proposal)
        reconcile=self.ctrl.step(self.obs);self.lua.execute('d.registered_belt_repairs['+json.dumps(key)+'].tick=21')
        self.assertEqual(execute_reconcile_registered_belts(self.game,reconcile)['reason'],'infrastructure_repair_outcome_unknown')

    def test_paid_batch_exact_native_graph_replay_and_binding_publication(self):
        action=self.ctrl.step(self.obs);r=execute_repair_registered_belts(self.game,action)
        self.assertTrue(r['ok'],r);self.assertEqual((r['paid_belts'],r['before'],r['after']),(3,10,7));self.assertEqual(r['preserved_spill_stacks'],3)
        self.assertTrue(execute_repair_registered_belts(self.game,action)['reused_receipt']);self.assertEqual(self.lua.eval('main[1].count'),7)
        self.assertIsNone(self.ctrl.step(self.obs));self.assertTrue(self.ctrl.last_diagnostic['ok'])
        after=self.read('advanced-circuits.json');after.pop('registered_belt_repair_history');old=deepcopy(self.owner);old['bindings']=after['bindings'];self.assertEqual(after,old)
        self.assertEqual(self.read('pcb-board.json'),self.board);self.assertEqual(self.ref.state,self.read('advanced-circuits.json'))
        self.assertTrue(self.lua.eval('remnant.valid'));self.assertEqual(self.lua.eval('remnant.position.y'),1.5)
        self.assertFalse(self.ctrl.prepare(self.obs)['needed'])

    def test_before_cost_guards_reject_changed_identity_resource_stock_and_spill_metadata(self):
        for code in('a.unit_number=12','d.world_id="other"','main[1].count=2','resources=true','spills[1].stack.quality.name="rare"','spills[1].stack.name="iron-plate"','world["0.5:2.5"].unit_number=999','contamination=true'):
            with self.subTest(code=code):
                self.setUp();action=self.ctrl.step(self.obs);self.lua.execute(code);n=self.lua.eval('main[1].count')
                r=execute_repair_registered_belts(self.game,action);self.assertFalse(r['ok'],r)
                self.assertEqual(n,self.lua.eval('main[1].count'));self.assertTrue(self.lua.eval('d.registered_belt_repairs==nil'))

    def test_foreign_unit_pins_block_but_job_history_does_not(self):
        self.write('jobs.json',dict(evidence={'unit_number':101}));self.assertTrue(self.ctrl.prepare(self.obs)['ok'])
        self.write('foreign.json',dict(preserved=[{'unit_number':101}]))
        self.assertFalse(self.ctrl.prepare(self.obs)['ok']);self.assertIsNone(self.ctrl.step(self.obs))
        (self.root/'foreign.json').unlink();self.write('foreign.json',dict(bindings={'shared-belt':101}))
        self.assertFalse(self.ctrl.prepare(self.obs)['ok'])

    def test_published_port_loss_and_unmarked_replacement_never_adopted(self):
        self.lua.execute('world["0.5:0.5"]=nil');self.assertFalse(self.ctrl.prepare(self.obs)['ok'])
        self.setUp();self.lua.execute('belt(999,1.5)');self.assertFalse(self.ctrl.prepare(self.obs)['ok']);self.assertFalse((self.root/FILE).exists())

    def test_prefix_or_postbuild_debris_change_is_unknown_and_never_replayed(self):
        action=self.ctrl.step(self.obs)
        self.lua.execute('local old=s.create_entity;local n=0;s.create_entity=function(q)n=n+1;if n==2 then return nil end;return old(q)end')
        r=execute_repair_registered_belts(self.game,action);self.assertEqual(r['reason'],'infrastructure_repair_outcome_unknown');self.assertGreater(r['paid_belts'],0)
        n=self.lua.eval('main[1].count');self.assertEqual(execute_repair_registered_belts(self.game,action)['reason'],'infrastructure_repair_outcome_unknown');self.assertEqual(n,self.lua.eval('main[1].count'))
        self.assertIsNone(self.ctrl.step(self.obs));self.assertEqual(self.read('advanced-circuits.json'),self.owner)
        self.setUp();action=self.ctrl.step(self.obs)
        self.lua.execute('local old=s.create_entity;s.create_entity=function(q)local e=old(q);spills[1].stack.count=3;return e end')
        self.assertEqual(execute_repair_registered_belts(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')

    def test_pending_without_effects_funds_stock_normally_and_unknown_lua_errors_stay_unknown(self):
        action=self.ctrl.step(self.obs);self.lua.execute('main[1].count=0')
        self.assertEqual(self.ctrl.step(self.obs)['type'],'take_mall_stock')
        self.assertEqual(self.read(FILE)['transactions'][action['transaction_id']]['phase'],'prepared')
        self.game.query=Mock(return_value={'ok':False,'reason':'unexpected native exception'})
        self.assertEqual(execute_repair_registered_belts(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')

    def test_malformed_time_receipts_and_changed_remnants_or_item_metadata_fail_closed(self):
        for field,value in(('tick',None),('start_tick',True),('paid_belts',True),('actor_unit',False)):
            with self.subTest(field=field):
                self.setUp();action=self.ctrl.step(self.obs);receipt=execute_repair_registered_belts(self.game,action)
                receipt[field]=value;self.game.query=Mock(return_value=receipt)
                self.assertEqual(execute_repair_registered_belts(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')
        for mutation in('remnant.valid=false','spills[1].stack.quality.name="rare"'):
            with self.subTest(mutation=mutation):
                self.setUp();action=self.ctrl.step(self.obs)
                self.lua.execute('local old=s.create_entity;s.create_entity=function(q)local e=old(q);'+mutation+';return e end')
                self.assertEqual(execute_repair_registered_belts(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')

    def test_cas_interrupt_resume_and_unrelated_state_change_not_overwritten(self):
        action=self.ctrl.step(self.obs);self.assertTrue(execute_repair_registered_belts(self.game,action)['ok'])
        def interrupted(path,value):
            _atomic_json(path,value)
            if path.name=='advanced-circuits.json':raise OSError('interrupted after owner rename')
        with patch('factorio_agent.registered_belt_repair._atomic_json',side_effect=interrupted):self.assertIsNone(self.ctrl.step(self.obs))
        self.assertIsNone(self.ctrl.step(self.obs));self.assertTrue(self.ctrl.last_diagnostic['ok'],self.ctrl.last_diagnostic)
        self.assertEqual(len(self.read('advanced-circuits.json')['registered_belt_repair_history']),1)
        self.setUp();action=self.ctrl.step(self.obs);self.assertTrue(execute_repair_registered_belts(self.game,action)['ok'])
        changed=self.read('advanced-circuits.json');changed['foreign']='retain';self.write('advanced-circuits.json',changed)
        self.assertIsNone(self.ctrl.step(self.obs));self.assertEqual(self.read('advanced-circuits.json')['foreign'],'retain')

    def test_funding_uses_normal_factory_hook_and_invalid_envelopes_never_issue_io(self):
        self.lua.execute('main[1].count=0');self.assertEqual(self.ctrl.step(self.obs)['type'],'take_mall_stock')
        self.assertFalse((self.root/FILE).exists());self.setUp();action=self.ctrl.step(self.obs);self.game.query=Mock()
        for changes in({'type':'build'},{'expected_actor_unit':True},{'expected_world_id':'other'},{'unknown':'field'},{'transaction_id':'z'*64}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):execute_repair_registered_belts(self.game,{**action,**changes})
        self.game.query.assert_not_called();self.assertNotIn('--',REPAIR_BELTS_LUA);self.assertNotIn('.destroy(',REPAIR_BELTS_LUA)


if __name__=='__main__':unittest.main()
