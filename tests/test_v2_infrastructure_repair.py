from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_state import _atomic_json
from factorio_agent.infrastructure_repair import (InfrastructureRepair,execute_repair_registered_infrastructure,
    REPAIR_INFRASTRUCTURE_LUA,FILE,_key)
try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


@unittest.skipUnless(LuaRuntime,'optional executable Lua fixture')
class InfrastructureRepairTests(unittest.TestCase):
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
            if keys and all(type(k)is int for k in keys)and sorted(keys)==list(range(1,len(keys)+1)):
                return [decode(v[i])for i in range(1,len(keys)+1)]
            return {k:decode(q)for k,q in v.items()}
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''
d={world_id="w"};f={name="player"};s={};game={tick=10};next_unit=200
prototypes={utility_constants={default_pipeline_extent=320},item={pipe={type="item"}}}
main={valid=true};local data={valid_for_read=true,name="pipe",count=10,prototype=prototypes.item.pipe,quality={name="normal"},health=1,spoil_tick=0}
main[1]=setmetatable({},{__index=data,__newindex=function(q,k,v)data[k]=v;if k=="count"and v==0 then data.valid_for_read=false end end})
a={valid=true,unit_number=11,force=f,surface=s,get_main_inventory=function()return main end}
world={};local proto={fluidbox_prototypes={{index=1}}}
local function k(p)return p.x..":"..p.y end
function pipe(unit,x,y)
 local e={valid=true,name="pipe",unit_number=unit,position={x=x,y=y},force=f,surface=s,direction=0,quality={name="normal"},prototype=proto}
 e.get_fluid_contents=function()return {["petroleum-gas"]=100}end
 e.get_fluid_box_neighbours=function(index)
  local rows={};for _,delta in ipairs{{1,0},{-1,0},{0,1},{0,-1}}do
   local other=world[k{x=x+delta[1],y=y+delta[2]}]
   if other and other.valid then rows[#rows+1]={entity=other,index=1}end
  end;return rows
 end
 e.has_fluid_segment=function()return true end
 e.get_fluid_segment_extent_bounding_box=function()return {left_top={x=0,y=0},right_bottom={x=8,y=1}}end
 world[k(e.position)]=e;return e
end
function target(p,name)return world[k(p)]end
s.can_place_entity=function(spec)return not world[k(spec.position)]end
s.count_entities_filtered=function()return resource_count or 0 end
s.find_entities_filtered=function(spec)
 local out={};for _,e in pairs(world)do local p=e.position
  if p.x>=spec.area[1][1]and p.y>=spec.area[1][2]and p.x<=spec.area[2][1]and p.y<=spec.area[2][2]then out[#out+1]=e end
 end;return out
end
s.create_entity=function(spec)next_unit=next_unit+1;return pipe(next_unit,spec.position.x,spec.position.y)end
pipe(100,.5,.5);pipe(103,3.5,.5);pipe(107,7.5,.5)
''')
        self.game=SimpleNamespace(expected_world='w',backend='assisted',cfg=SimpleNamespace(runtime_dir=self.root),query=lambda script:
            decode(self.lua.execute('return(function() '+' '.join(script.splitlines())+' end)()')))
        self.builder=SimpleNamespace(bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type':'craft','recipe':'pipe','count':5})))
        self.catalog=SimpleNamespace(fingerprint='c');self.obs=dict(ok=True,world_id='w',actor_unit_number=11,inventory={'pipe':10})
        def node(x):return dict(name='pipe',position=dict(x=x+.5,y=.5),direction=0)
        def port(x,unit):return dict(node(x),kind='fluid',item='petroleum-gas',direction='output',facing=4,fluidbox_index=1,unit_number=unit,world_id='w')
        def edges(nodes):return [dict(a=a,b=b)for a,b in zip(nodes,nodes[1:])]
        pnodes=[node(x)for x in range(4)];gnodes=[node(x)for x in range(3,8)];gnodes[0]=port(3,103)
        self.petroleum=dict(world_id='w',catalog_fingerprint='c',phase='verified',
            core=dict(gas_pipes=deepcopy(pnodes),machines=[]),fluid_nodes=deepcopy(pnodes),fluid_edges=edges(pnodes),source=port(0,100),
            plan=dict(entities=deepcopy(pnodes),ports=[port(3,103)],reserved_area=[[0,-1],[4,2]]),bindings={_key(q):100+i for i,q in enumerate(pnodes)})
        self.gas=dict(world_id='w',catalog_fingerprint='c',phase='verified',nodes=deepcopy(gnodes),edges=edges(gnodes),
            plan=dict(entities=deepcopy(gnodes[1:]),ports=[port(7,107)],reserved_area=[[4,-1],[9,2]]),bindings={_key(node(i)):100+i for i in range(4,8)})
        self.board=dict(world_id='w',catalog_fingerprint='c',blocks=dict(basic_petroleum=deepcopy(self.petroleum['plan']),gas_distribution=deepcopy(self.gas['plan'])))
        self.registry=dict(world_id='w',catalog_fingerprint='c',sources={'petroleum-gas':port(3,103),'sulfur-gas':port(7,107)},handoffs={})
        self.write('basic-petroleum.json',self.petroleum);self.write('gas-distribution.json',self.gas)
        self.write('pcb-board.json',self.board);self.write('fluid-sources.json',self.registry)
        self.refs=dict(petroleum=SimpleNamespace(state=deepcopy(self.petroleum)),gas=SimpleNamespace(state=deepcopy(self.gas)))
        self.controller=InfrastructureRepair(self.game,self.builder,self.catalog,**self.refs)
        self.checked=patch('factorio_agent.infrastructure_repair.preflight',return_value={'ok':True,'clear_obstacles':[]})
        self.checked.start();self.addCleanup(self.checked.stop)

    def write(self,name,value):(self.root/name).write_text(json.dumps(value))
    def read(self,name):return json.loads((self.root/name).read_text())

    def test_readonly_prepare_reports_all_five_exact_losses_and_whole_cost(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')}
        result=self.controller.prepare(self.obs);self.assertTrue(result['ok'],result)
        self.assertEqual(result['material_cost'],{'pipe':5})
        self.assertEqual([q['old_unit']for q in result['payload']['repairs']],[101,102,104,105,106])
        self.assertEqual(before,{p.name:p.read_bytes()for p in self.root.glob('*.json')})

    def test_exact_scope_annotation_and_nondirectional_saved_pipe_are_preserved(self):
        self.petroleum['defense_scope_id']='p-scope';self.board['blocks']['basic_petroleum']['defense_scope_id']='p-scope'
        for entity in self.petroleum['plan']['entities']:entity['direction']=8
        self.board['blocks']['basic_petroleum']['entities']=deepcopy(self.petroleum['plan']['entities'])
        self.write('basic-petroleum.json',self.petroleum);self.write('pcb-board.json',self.board)
        action=self.controller.step(self.obs);result=execute_repair_registered_infrastructure(self.game,action)
        self.assertTrue(result['ok'],result);self.assertEqual(self.lua.eval('world["1.5:0.5"].direction'),0)
        self.assertIsNone(self.controller.step(self.obs));self.assertTrue(self.controller.last_diagnostic['ok'])
        self.assertEqual(self.read('basic-petroleum.json')['plan'],self.petroleum['plan'])
        self.assertEqual(self.read('pcb-board.json'),self.board)
        self.board['blocks']['basic_petroleum']['defense_scope_id']='foreign';self.write('pcb-board.json',self.board)
        self.assertFalse(self.controller.scan(self.obs)['ok'])

    def test_paid_native_batch_and_restart_publish_only_binding_and_append_history(self):
        action=self.controller.step(self.obs);before_board=(self.root/'pcb-board.json').read_bytes();before_registry=(self.root/'fluid-sources.json').read_bytes()
        receipt=execute_repair_registered_infrastructure(self.game,action)
        self.assertTrue(receipt['ok'],receipt);self.assertEqual(receipt['paid_pipes'],5);self.assertEqual(self.lua.eval('main[1].count'),5)
        again=execute_repair_registered_infrastructure(self.game,action);self.assertTrue(again['reused_receipt']);self.assertEqual(self.lua.eval('main[1].count'),5)
        restarted=InfrastructureRepair(self.game,self.builder,self.catalog,**self.refs)
        self.assertIsNone(restarted.step(self.obs));self.assertTrue(restarted.last_diagnostic['ok'],restarted.last_diagnostic)
        for filename,old in(('basic-petroleum.json',self.petroleum),('gas-distribution.json',self.gas)):
            new=self.read(filename);history=new.pop('infrastructure_repair_history')
            self.assertEqual(history[0]['transaction_id'],action['transaction_id']);self.assertEqual(len(history),1)
            old=deepcopy(old);old['bindings']=new['bindings'];self.assertEqual(new,old)
        self.assertEqual(before_board,(self.root/'pcb-board.json').read_bytes());self.assertEqual(before_registry,(self.root/'fluid-sources.json').read_bytes())
        self.assertEqual(self.refs['gas'].state,self.read('gas-distribution.json'))
        self.assertTrue(restarted.scan(self.obs)['ok']);self.assertFalse(restarted.scan(self.obs)['repairs'])

    def test_all_sites_stock_and_known_neighbours_validate_before_any_cost(self):
        for change in('a.unit_number=12','d.world_id="other"','main[1].count=4','main[1].quality.name="rare"',
                       'resource_count=1','pipe(999,1.5,.5)','pipe(999,1.5,1.5)'):
            with self.subTest(change=change):
                self.setUp();action=self.controller.step(self.obs);self.lua.execute(change)
                before=self.lua.eval('main[1].count');result=execute_repair_registered_infrastructure(self.game,action)
                self.assertFalse(result['ok'],result);self.assertEqual(self.lua.eval('main[1].count'),before)
                self.assertTrue(self.lua.eval('d.infrastructure_repairs==nil'))

    def test_unproven_existing_pipe_never_adopted_and_failed_scan_leaves_defense_running(self):
        self.lua.execute('pipe(999,1.5,.5)')
        result=self.controller.prepare(self.obs);self.assertFalse(result['ok'])
        self.assertIsNone(self.controller.step(self.obs));self.assertFalse(self.controller.last_diagnostic['ok'])
        self.assertFalse((self.root/FILE).exists())

    def test_malformed_or_changed_authority_fails_before_native_io(self):
        action=self.controller.step(self.obs);self.game.query=Mock()
        for change in({'expected_actor_unit':False},{'expected_world_id':'other'},{'transaction_id':'x'*64},{'unknown':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):execute_repair_registered_infrastructure(self.game,{**action,**change})
        self.gas['plan']['new_revision']=True;self.write('gas-distribution.json',self.gas)
        with self.assertRaises(ValueError):execute_repair_registered_infrastructure(self.game,action)
        self.game.query.assert_not_called()

    def test_foreign_preservation_witness_and_missing_published_port_block(self):
        self.write('foreign-consumer.json',{'preserved_pipes':[{'unit_number':101}]})
        self.assertIn('preserved',self.controller.prepare(self.obs)['reason'])
        self.write('foreign-consumer.json',{'bindings':{'pipe:1.5:0.5':101}})
        self.assertIn('preserved',self.controller.prepare(self.obs)['reason'])
        (self.root/'foreign-consumer.json').unlink()
        self.lua.execute('world["7.5:0.5"]=nil')
        self.assertIn('published port',self.controller.prepare(self.obs)['reason'])

    def test_historical_job_receipts_do_not_pin_replacement_units_forever(self):
        self.write('jobs.json',{'jobs':{'old-watch':{'evidence':{'receipt':{'unit_number':101}}}}})
        self.assertTrue(self.controller.prepare(self.obs)['ok'])
        self.write('foreign-consumer.json',{'bindings':{'pipe:1.5:0.5':101}})
        self.assertIn('preserved',self.controller.prepare(self.obs)['reason'])

    def test_partial_native_effects_do_not_replay_or_rebind(self):
        action=self.controller.step(self.obs)
        self.lua.execute('local old=s.create_entity;local calls=0;s.create_entity=function(spec)calls=calls+1;if calls==2 then return nil end;return old(spec)end')
        result=execute_repair_registered_infrastructure(self.game,action);self.assertFalse(result['ok'])
        before=self.lua.eval('main[1].count')
        result=execute_repair_registered_infrastructure(self.game,action);self.assertFalse(result['ok']);self.assertEqual(result['reason'],'infrastructure_repair_outcome_unknown')
        self.assertEqual(result['original_reason'],'infrastructure_prior_effects_uncertain');self.assertGreater(result['paid_pipes'],0)
        self.assertEqual(before,self.lua.eval('main[1].count'));self.assertIsNone(self.controller.step(self.obs))
        self.assertEqual(self.read('basic-petroleum.json'),self.petroleum)

    def test_world_bound_dispatch_records_the_exact_native_paid_repair(self):
        from factorio_agent.runtime import WorldBoundGame
        action=self.controller.step(self.obs)
        game=WorldBoundGame.__new__(WorldBoundGame)
        game.expected_world=self.game.expected_world;game.backend=self.game.backend
        game.cfg=self.game.cfg;game.query=self.game.query
        game._record_action=Mock(side_effect=lambda request,receipt:receipt)
        receipt=game.act(action)
        self.assertTrue(receipt['ok'],receipt)
        self.assertEqual(receipt['paid_pipes'],5)
        game._record_action.assert_called_once_with(action,receipt)

    def test_after_build_proof_failure_and_transport_failure_preserve_unknown_outcome(self):
        action=self.controller.step(self.obs)
        self.lua.execute('local old=s.create_entity;s.create_entity=function(spec)local e=old(spec);e.get_fluid_box_neighbours=function()return {}end;return e end')
        result=execute_repair_registered_infrastructure(self.game,action)
        self.assertEqual(result['reason'],'infrastructure_repair_outcome_unknown');self.assertGreater(result['paid_pipes'],0)
        self.assertEqual(result['original_reason'],'infrastructure_reciprocal_fluid_edge_missing')
        self.game.query=Mock(side_effect=TimeoutError('unanswered native command'))
        self.assertEqual(execute_repair_registered_infrastructure(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')
        self.game.query=Mock(return_value={'ok':True,'replacements':{'untrusted':'shape'}})
        self.assertEqual(execute_repair_registered_infrastructure(self.game,action)['reason'],'infrastructure_repair_outcome_unknown')

    def test_interrupted_two_file_publication_resumes_idempotently(self):
        action=self.controller.step(self.obs);self.assertTrue(execute_repair_registered_infrastructure(self.game,action)['ok'])
        written=[]
        def interrupted(path,value):
            if path.name in('basic-petroleum.json','gas-distribution.json'):
                written.append(path.name)
                if len(written)==2:raise OSError('injected second owner publication interruption')
            _atomic_json(path,value)
        with patch('factorio_agent.infrastructure_repair._atomic_json',side_effect=interrupted):
            self.assertIsNone(self.controller.step(self.obs));self.assertFalse(self.controller.last_diagnostic['ok'])
        self.assertIsNone(self.controller.step(self.obs));self.assertTrue(self.controller.last_diagnostic['ok'],self.controller.last_diagnostic)
        self.assertEqual(len(self.read('basic-petroleum.json')['infrastructure_repair_history']),1)
        self.assertEqual(len(self.read('gas-distribution.json')['infrastructure_repair_history']),1)

    def test_publication_cas_never_overwrites_unrelated_owner_change(self):
        action=self.controller.step(self.obs);self.assertTrue(execute_repair_registered_infrastructure(self.game,action)['ok'])
        state=self.read('gas-distribution.json');state['foreign_progress']='keep';self.write('gas-distribution.json',state)
        self.assertIsNone(self.controller.step(self.obs));self.assertFalse(self.controller.last_diagnostic['ok'])
        self.assertEqual(self.read('gas-distribution.json')['foreign_progress'],'keep')
        self.assertEqual(self.read('basic-petroleum.json'),self.petroleum)

    def test_native_normal_stock_not_aggregate_observation_funds_repair_and_flattening_is_valid(self):
        self.lua.execute('main[1].count=4')
        self.assertEqual(self.controller.step(self.obs)['type'],'craft');self.assertFalse((self.root/FILE).exists())
        self.assertNotIn('--',REPAIR_INFRASTRUCTURE_LUA)
        self.assertNotIn('set_fluid',REPAIR_INFRASTRUCTURE_LUA)


if __name__=='__main__':unittest.main()
