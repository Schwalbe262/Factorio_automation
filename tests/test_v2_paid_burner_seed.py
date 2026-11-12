from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.world_catalog import WorldCatalog
from factorio_agent.paid_burner_seed import PaidBurnerSeed,execute_paid_burner_seed,SEED_LUA,STATE_FILE

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


class SeedFixture(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);self.root=Path(directory.name)
        self.catalog=WorldCatalog.from_dict({})
        (self.root/'catalog.json').write_text(json.dumps({}),encoding='utf-8')
        self.spec=dict(name='burner-mining-drill',position={'x':5,'y':6},direction=4)
        self.owner=dict(world_id='w',catalog_fingerprint=self.catalog.fingerprint,seeds={'coal_drill':{'observed':True}},coal_plan={'drill':self.spec})
        (self.root/'factory-builder.json').write_text(json.dumps(self.owner),encoding='utf-8')
        (self.root/'jobs.json').write_text(json.dumps({'schema_version':2,'world_id':'w','jobs':{'power':{'world_id':'w'}}}),encoding='utf-8')
        self.rows=[];self.pair(1,{'type':'build',**self.spec},{'ok':True,'status':'succeeded','unit_number':100})
        self.pair(2,{'type':'insert',**self.spec,'item':'coal','inventory':'fuel','count':8,'reason':'one-time power bootstrap fuel'}, {'ok':True,'moved':8})
        self.pair(20,{'type':'build',**self.spec},{'ok':True,'status':'succeeded','unit_number':200})
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),expected_world='w',backend='assisted',query=Mock())
        self.controller=PaidBurnerSeed(self.game,self.catalog)
        self.obs=dict(world_id='w',tick=100,inventory={'coal':8},entities=[{**deepcopy(self.spec),'unit_number':200,'inventory':{},'remaining_burning_fuel':0}])
        self.bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type':'take','item':'coal','count':8}))

    def pair(self,tick,action,result):
        self.rows +=[dict(key='power',event='action_intent',tick=tick,action=action),dict(key='power',event='action_result',tick=tick,result=result)]
        (self.root/'job-events.jsonl').write_text(''.join(json.dumps(r)+'\n'for r in self.rows),encoding='utf-8')

    def action(self):return self.controller.step(self.obs,'coal_drill',self.spec,8,bootstrap=self.bootstrap,move=lambda *_:None)

    def receipt(self,action,moved):
        return dict(ok=True,status='succeeded'if moved else'waiting',moved=moved,total_moved=action['expected_moved']+moved,
            unit_number=200,world_id='w',operation_id=action['seed_operation_id'],birth_receipt_id=action['birth_receipt_id'],tick=100)

    def test_legacy_observed_replacement_gets_one_paid_budget_without_changing_old_flag(self):
        before=(self.root/'factory-builder.json').read_bytes();preview=self.controller.prepare(self.obs,'coal_drill',self.spec)
        self.assertTrue(preview['eligible']);self.assertFalse((self.root/STATE_FILE).exists())
        action=self.action();self.game.query.return_value=self.receipt(action,8)
        result=execute_paid_burner_seed(self.game,action)
        self.assertEqual((result['moved'],result['total_moved']),(8,8));self.assertEqual((self.root/'factory-builder.json').read_bytes(),before)
        self.assertIn('closed_requires_continuous_fuel',self.action()['reason']);self.assertEqual(self.game.query.call_count,1)

    def test_partial_known_receipt_only_exposes_original_remainder_and_unknown_never_replays(self):
        action=self.action();self.game.query.return_value=self.receipt(action,3);execute_paid_burner_seed(self.game,action)
        second=self.action();self.assertEqual((second['count'],second['expected_moved']),(5,3))
        self.game.query.side_effect=TimeoutError('possible effect')
        with self.assertRaisesRegex(RuntimeError,'paid_burner_seed_outcome_unknown'):execute_paid_burner_seed(self.game,second)
        self.assertIn('outcome_unknown',self.action()['reason'])
        with self.assertRaises(ValueError):execute_paid_burner_seed(self.game,second)
        self.assertEqual(self.game.query.call_count,2)

    def test_same_unit_started_before_or_world_catalog_rollback_does_not_reopen_allowance(self):
        self.obs['entities'][0]['inventory']={'coal':1};self.assertIsNone(self.action())
        self.obs['entities'][0]['inventory']={};self.assertEqual(self.action()['status'],'waiting')
        original=(self.root/STATE_FILE).read_bytes()
        self.obs['tick']=99;self.assertEqual(self.action()['status'],'blocked');self.assertEqual((self.root/STATE_FILE).read_bytes(),original)
        self.obs['tick']=100;self.obs['world_id']='other';self.assertEqual(self.action()['status'],'blocked')
        self.obs['world_id']='w';self.controller.catalog=SimpleNamespace(fingerprint='changed');self.assertEqual(self.action()['status'],'blocked')
        self.assertEqual((self.root/STATE_FILE).read_bytes(),original)

    def test_reused_build_is_not_a_paid_birth_and_invalid_envelopes_never_query(self):
        self.rows=[];self.pair(20,{'type':'build',**self.spec},{'ok':True,'status':'succeeded','unit_number':200,'reused':True})
        self.assertEqual(self.action()['status'],'blocked')
        with self.assertRaises(ValueError):execute_paid_burner_seed(self.game,{'type':'seed_paid_burner','count':9})
        self.game.query.assert_not_called()

    def test_unexpected_boolean_cumulative_or_status_retains_unknown_intent(self):
        for changes in ({'total_moved':True},{'status':'running'}):
            action=self.action();self.game.query.return_value={**self.receipt(action,1),**changes}
            with self.assertRaisesRegex(RuntimeError,'paid_burner_seed_outcome_unknown'):execute_paid_burner_seed(self.game,action)
            self.assertIn('outcome_unknown',self.action()['reason'])
            (self.root/STATE_FILE).unlink();self.controller.state={}


@unittest.skipIf(LuaRuntime is None,'optional isolated lupa dependency unavailable')
class NativeSeedTests(SeedFixture):
    def lua_fixture(self,action):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
 d={world_id="w"};f={};s={};game={tick=100}
 main={{valid_for_read=true,name="coal",count=3,quality={name="normal"},prototype={type="item"}},
       {valid_for_read=true,name="coal",count=20,quality={name="rare"},prototype={type="item"}}}
 fuel={count=0};fuel.get_item_count=function(_)return fuel.count end
 fuel.insert=function(q)fuel.count=fuel.count+q.count;return q.count end
 a={valid=true,force=f,surface=s,get_main_inventory=function()return main end,can_reach_entity=function(_)return true end}
 e={valid=true,name="burner-mining-drill",unit_number=200,force=f,surface=s,position={x=5,y=6},direction=4,
    burner={remaining_burning_fuel=0},get_fuel_inventory=function()return fuel end}
 target=function(_,_)return e end;helpers={json_to_table=function(_)return payload end}
 ''')
        payload={**action,'birth_tick':20,'character_backend':False};lua.globals().payload=lua.table_from(payload,recursive=True)
        return lua,SEED_LUA.replace('PAYLOAD','"payload"')

    def test_actual_lua_normal_coal_conservation_partial_and_native_once_budget(self):
        action=self.action();lua,code=self.lua_fixture(action);result=lua.execute(code)
        self.assertEqual((result['moved'],result['total_moved']),(3,3))
        self.assertEqual(lua.eval('main[1].count+fuel.count'),3);self.assertEqual(lua.eval('main[2].count'),20)
        lua.execute('main[1].count=5;payload.expected_moved=3;payload.count=5;payload.seed_operation_id="second"')
        result=lua.execute(code);self.assertEqual((result['moved'],result['total_moved']),(5,8))
        self.assertEqual(lua.eval('main[1].count+fuel.count'),8)
        lua.execute('payload.expected_moved=8;payload.count=1;payload.seed_operation_id="third"')
        self.assertEqual(lua.execute(code)['reason'],'seed_native_allowance_closed')

    def test_actual_lua_world_unit_force_and_character_reach_reject_before_cost(self):
        for mutation in ('d.world_id="foreign"','e.unit_number=201','e.force={}',
                'payload.character_backend=true;a.can_reach_entity=function(_)return false end'):
            with self.subTest(mutation=mutation):
                action=self.action();lua,code=self.lua_fixture(action);lua.execute(mutation)
                result=lua.execute(code);self.assertFalse(result['ok']);self.assertTrue(result['pre_effect'])
                self.assertEqual(lua.eval('main[1].count'),3);self.assertEqual(lua.eval('fuel.count'),0)
                # Each context uses a new never-executed prepared action.
                self.controller.state={};(self.root/STATE_FILE).unlink()


if __name__=='__main__':unittest.main()
