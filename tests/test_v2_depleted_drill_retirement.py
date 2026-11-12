from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.depleted_drill_retirement import (DepletedDrillRetirement,FILE,MODE,UNKNOWN,
    RETIRE_LUA,SURVEY_LUA,MINING_POTENTIAL_LUA,_hash,load_retirements,effective_mining_plan,
    guard_drill_rebuild,execute_retire_depleted_drill,registered_drills,mining_capacity_summary)
from factorio_agent.paired_mining import mining_pair

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


def fixture(root):
    plan=mining_pair(8.5,-64.5,1,'iron-ore')
    obs=dict(world_id='w',tick=10,actor_unit_number=1,entities=[dict(q,unit_number=100+i)for i,q in enumerate(plan['entities'])])
    drills=[q for q in obs['entities']if q['name']=='electric-mining-drill']
    owner=dict(kind='outpost',index=0,plan=deepcopy(plan),plan_revision=_hash(plan),known_drill_units=[q['unit_number']for q in drills])
    ownerid=_hash(owner);target=dict(drills[0],owner_revision=ownerid,resource='iron-ore',historically_pinned=True,phase='armed')
    target['retirement_id']=_hash([MODE,'w','c',target['unit_number'],target['position'],target['direction'],ownerid])
    state=dict(mode=MODE,world_id='w',catalog_fingerprint='c',phase='recover',targets=[target],authorities={ownerid:owner},receipts=[],
        protected_units=[{k:q[k]for k in('name','position','direction','unit_number')}for q in obs['entities'][1:]])
    bank=dict(plan=plan,ready=True,nominal_rate_per_minute=60,output_evidence={'drill_units':owner['known_drill_units']})
    (root/'mining-outposts.json').write_text(json.dumps(dict(world_id='w',catalog_fingerprint='c',banks=[bank])))
    (root/'catalog.json').write_text('{}');(root/FILE).write_text(json.dumps(state))
    game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root),expected_world='w',backend='assisted',query=Mock())
    return game,plan,obs,state,target


def receipt(target):
    return dict(ok=True,status='succeeded',phase='complete',mode=MODE,retirement_id=target['retirement_id'],world_id='w',catalog_fingerprint='c',
        actor_unit=1,unit_number=target['unit_number'],position=target['position'],direction=target['direction'],radius=2.49,
        remaining_any_resource=0,before_drills=3,after_drills=4,normal_drills_recovered=1,removed=True,started_tick=10,completed_tick=10,force_index=1,surface_index=1)


def completed(state,target):
    state=deepcopy(state);row=dict(retirement_id=target['retirement_id'],native=receipt(target),previous_receipt=None)
    row['receipt_id']=_hash(row);state['receipts']=[row];state['targets'][0]['phase']='complete';state['phase']='verified';return state


class RetirementFixture(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.game,self.plan,self.obs,self.state,self.target=fixture(self.root)


class LedgerTests(RetirementFixture):

    def test_only_paid_receipt_removes_a_node_from_active_view_preserving_canonical_grid_ports(self):
        before=deepcopy(self.plan)
        self.assertEqual(effective_mining_plan(self.game,self.obs,self.plan),self.plan)
        state=completed(self.state,self.target);(self.root/FILE).write_text(json.dumps(state))
        missing=dict(self.obs,entities=self.obs['entities'][1:]);active=effective_mining_plan(self.game,missing,self.plan,catalog_fingerprint='c')
        self.assertEqual(len(active['entities']),len(self.plan['entities'])-1);self.assertEqual(active['effective_drill_count'],1)
        self.assertEqual(active['ports'],self.plan['ports']);self.assertEqual(active['pair_count'],self.plan['pair_count'])
        self.assertEqual(active['mining_footprint'],self.plan['mining_footprint']);self.assertEqual(self.plan,before)
        self.assertEqual(active['producer_rows']['left'],[]);self.assertEqual(len(active['producer_rows']['right']),1)
        # A larger canonical grid retains the historical slot but never rebuilds
        # it; the recovered physical item may be used at a different new column.
        future=mining_pair(8.5,-64.5,2,'iron-ore');view=effective_mining_plan(self.game,missing,future)
        self.assertEqual(view['effective_drill_count'],3);self.assertEqual(view['pair_count'],2)

    def test_direct_and_batched_rebuild_is_denied_and_unreconciled_absence_is_never_adopted(self):
        build=dict(type='build',name='electric-mining-drill',position=self.target['position'])
        self.assertEqual(guard_drill_rebuild(self.game,build)['reason'],'retired_depleted_drill_position_rebuild_forbidden')
        self.assertIsNotNone(guard_drill_rebuild(self.game,{'type':'build_many','actions':[dict(build,position={'x':20.5,'y':20.5}),build]}))
        self.assertIsNone(guard_drill_rebuild(self.game,dict(build,position={'x':20.5,'y':20.5})))
        missing=dict(self.obs,entities=self.obs['entities'][1:])
        with self.assertRaises(ValueError):effective_mining_plan(self.game,missing,self.plan)
        self.assertEqual(self.game.query.call_count,0)

    def test_unitless_flag_forged_owner_chain_or_inventory_delta_cannot_retire(self):
        for change in(lambda d:d['targets'][0].update(phase='complete'),lambda d:d['authorities'].clear()):
            bad=deepcopy(self.state);change(bad);(self.root/FILE).write_text(json.dumps(bad))
            with self.assertRaises(ValueError):load_retirements(self.game)
        bad=completed(self.state,self.target);bad['receipts'][0]['native']['after_drills']=9
        bad['receipts'][0]['receipt_id']=_hash({k:v for k,v in bad['receipts'][0].items()if k!='receipt_id'})
        (self.root/FILE).write_text(json.dumps(bad))
        with self.assertRaises(ValueError):load_retirements(self.game)

    def test_effective_view_rejects_a_rebuilt_original_position_or_changed_world(self):
        (self.root/FILE).write_text(json.dumps(completed(self.state,self.target)))
        with self.assertRaises(ValueError):effective_mining_plan(self.game,self.obs,self.plan)
        with self.assertRaises(ValueError):effective_mining_plan(self.game,dict(self.obs,world_id='other'),self.plan)


class ControllerTests(RetirementFixture):
    def setUp(self):
        super().setUp();self.c=SimpleNamespace(fingerprint='c');self.builder=SimpleNamespace(ensure_plan=Mock())
        self.p=DepletedDrillRetirement(self.game,self.builder,self.c)

    def test_native_known_completion_is_appended_once_without_a_second_mine(self):
        native=receipt(self.target);self.game.query.return_value=dict(ok=True,records=[dict(retirement_id=self.target['retirement_id'],receipt=native)])
        obs=dict(self.obs,entities=self.obs['entities'][1:]);result=self.p.step(obs)
        self.assertEqual(result['status'],'succeeded');self.assertEqual(result['evidence']['normal_drills_recovered'],1)
        self.assertEqual(len(json.loads((self.root/FILE).read_text())['receipts']),1);self.builder.ensure_plan.assert_not_called()
        proof=self.p.proof(obs);self.assertEqual(proof['retired_drill_units'],[self.target['unit_number']])

    def test_missing_without_receipt_and_partial_native_marker_block_no_retry(self):
        self.game.query.return_value={'ok':True,'records':[dict(retirement_id=self.target['retirement_id'],receipt=False)]}
        obs=dict(self.obs,entities=self.obs['entities'][1:]);self.assertEqual(self.p.step(obs)['reason'],UNKNOWN)
        self.game.query.return_value={'ok':True,'records':[dict(retirement_id=self.target['retirement_id'],receipt={'phase':'intent'})]}
        self.assertEqual(self.p.step(self.obs)['reason'],UNKNOWN);self.builder.ensure_plan.assert_not_called()

    def test_exact_actor_unit_envelope_is_emitted_after_durable_arming(self):
        self.game.query.return_value={'ok':True,'records':[dict(retirement_id=self.target['retirement_id'],receipt=False)]}
        action=self.p.step(self.obs);self.assertEqual(action['type'],'retire_depleted_drill')
        self.assertEqual(action['expected_entity_unit'],self.target['unit_number']);self.assertEqual(action['expected_actor_unit'],1)
        self.assertEqual(json.loads((self.root/FILE).read_text())['targets'][0]['phase'],'armed')

    def test_lost_active_drill_or_collector_blocks_before_any_mine(self):
        obs=dict(self.obs,entities=self.obs['entities'][:-1]);self.assertEqual(self.p.step(obs)['reason'],'drill_retirement_preserved_source_asset_changed')
        self.game.query.assert_not_called()

    def test_prepare_is_full_radius_and_read_only_repair_or_contents_block_normal_retirement(self):
        (self.root/FILE).unlink();self.p=DepletedDrillRetirement(self.game,self.builder,self.c)
        drills=[q for q in self.obs['entities']if q['name']=='electric-mining-drill']
        rows=[dict(unit_number=q['unit_number'],position=q['position'],direction=q['direction'],radius=2.49,remaining_any_resource=0 if i==0 else 100,
            normal_quality=True,full_health=True,minable=True,empty_modules=True,empty_output=True)for i,q in enumerate(drills)]
        self.game.query.return_value=dict(ok=True,tick=10,drills=rows)
        prepared=self.p.prepare(self.obs);self.assertTrue(prepared['ok'],prepared);self.assertEqual(len(prepared['targets']),1)
        self.assertFalse((self.root/FILE).exists());self.builder.ensure_plan.assert_not_called()
        rows[0]['empty_modules']=False
        prepared=self.p.prepare(self.obs);self.assertEqual(prepared['targets'],[]);self.assertEqual(len(prepared['blocked_cleanup']),1)
        self.assertIn(drills[0]['unit_number'],[q['unit_number']for q in prepared['protected_units']])

    def test_interrupted_metadata_ack_resumes_native_known_mine_without_emitting_it_again(self):
        native=receipt(self.target);self.game.query.return_value=dict(ok=True,records=[dict(retirement_id=self.target['retirement_id'],receipt=native)])
        obs=dict(self.obs,entities=self.obs['entities'][1:]);self.p._save=Mock(side_effect=OSError('metadata interrupted'))
        with self.assertRaises(OSError):self.p.step(obs)
        self.assertEqual(json.loads((self.root/FILE).read_text())['receipts'],[])
        restarted=DepletedDrillRetirement(self.game,self.builder,self.c)
        self.assertEqual(restarted.step(obs)['status'],'succeeded')
        self.assertEqual(len(json.loads((self.root/FILE).read_text())['receipts']),1)
        self.builder.ensure_plan.assert_not_called()


class TypedActionTests(RetirementFixture):
    def action(self):return dict(type='retire_depleted_drill',retirement_id=self.target['retirement_id'],expected_entity_unit=self.target['unit_number'],expected_entity_world_id='w',expected_actor_unit=1)

    def test_entire_identity_and_owner_catalog_are_validated_before_native_query(self):
        action=self.action();fake=SimpleNamespace(fingerprint='c',entities={'electric-mining-drill':{'mining_products':[dict(name='electric-mining-drill',amount=1)]}})
        self.game.query.return_value=receipt(self.target)
        with patch('factorio_agent.core.world_catalog.WorldCatalog.from_dict',return_value=fake):
            self.assertTrue(execute_retire_depleted_drill(self.game,action)['ok'])
            self.game.query.reset_mock();bad=dict(action,position={'x':0,'y':0})
            with self.assertRaises(ValueError):execute_retire_depleted_drill(self.game,bad)
            self.game.query.assert_not_called()

    def test_uncertain_native_result_or_exception_never_becomes_known_success(self):
        fake=SimpleNamespace(fingerprint='c',entities={'electric-mining-drill':{'mining_products':[dict(name='electric-mining-drill',amount=1)]}})
        with patch('factorio_agent.core.world_catalog.WorldCatalog.from_dict',return_value=fake):
            for result in(None,{'ok':True},dict(receipt(self.target),after_drills=90)):
                self.game.query.return_value=result;self.assertEqual(execute_retire_depleted_drill(self.game,self.action())['reason'],UNKNOWN)
            self.game.query.side_effect=TimeoutError('no receipt');self.assertEqual(execute_retire_depleted_drill(self.game,self.action())['reason'],UNKNOWN)
        self.game.backend='character';self.assertEqual(execute_retire_depleted_drill(self.game,self.action())['reason'],'depleted_drill_retirement_requires_assisted_backend')


class IntegrationTests(RetirementFixture):
    def test_known_inactive956_comes_from_base_recovery_history_without_claiming_live_source(self):
        spec=entity('electric-mining-drill',-48.5,14.5,4);old={'entities':[spec],'ports':[],'drill':spec}
        base=dict(world_id='w',catalog_fingerprint='c',phase='verified',published=True,depleted_source={'unit_number':956,'depleted':True,'remaining_coal':0},old_builder={'coal_plan':old})
        builder={'coal_plan':{'entities':[],'ports':[],'retired_source':{'diagnostic':{'unit_number':956},'drill':spec}}}
        for name,value in{'base-coal-recovery.json':base,'factory-builder.json':builder}.items():(self.root/name).write_text(json.dumps(value))
        before=(self.root/'factory-builder.json').read_bytes();obs=dict(self.obs,entities=self.obs['entities']+[dict(spec,unit_number=956)])
        rows,authorities,_=registered_drills(self.game,obs,SimpleNamespace(fingerprint='c'))
        old=next(q for q in rows if q['unit_number']==956)
        self.assertTrue(old['historically_pinned']);self.assertEqual(authorities[old['owner_revision']]['kind'],'inactive-base-coal')
        self.assertEqual((self.root/'factory-builder.json').read_bytes(),before)
        (self.root/FILE).unlink()
        # BaseCoalRecovery already removed this producer from service. Even if
        # it vanished before our recovery ledger existed, legacy ElectricCoal
        # must not recreate it. This does not assert its item was recovered.
        self.assertEqual(guard_drill_rebuild(self.game,dict(type='build',**spec))['reason'],'retired_depleted_drill_position_rebuild_forbidden')
        obs['entities'][-1]['unit_number']=957
        with self.assertRaises(ValueError):registered_drills(self.game,obs,SimpleNamespace(fingerprint='c'))

    def test_one_query_source_potential_uses_current_active_lane_not_historical60(self):
        (self.root/FILE).write_text(json.dumps(completed(self.state,self.target)))
        remaining=next(q for q in self.obs['entities']if q['name']=='electric-mining-drill'and q['unit_number']!=self.target['unit_number'])
        c=SimpleNamespace(fingerprint='c',entities={'iron-ore':{'mining_products':[dict(name='iron-ore',amount=1)]}})
        self.game.query.return_value=dict(ok=True,tick=20,belt_speed=.03125,banks=[dict(index=0,resource='iron-ore',left=0,right=33,installed_left=0,installed_right=33,
            drills=[dict(unit_number=remaining['unit_number'],drop_lane_verified=True,remaining=100,powered=True,working=False)])])
        before=(self.root/'mining-outposts.json').read_bytes();result=mining_capacity_summary(self.game,c)
        self.assertTrue(result['ok'],result);self.assertEqual(result['resources']['iron-ore'],33)
        self.assertEqual(result['effective_registered_drills'],1);self.assertEqual(result['historical_registered_drills'],2)
        self.assertIsNone(result['measured_per_minute']);self.assertEqual((self.root/'mining-outposts.json').read_bytes(),before)
        self.game.query.assert_called_once()

    def test_profile_furnace375_is_capped_by_live_ore264_after_receipted_retirement(self):
        from factorio_agent.capacity_profiles import _source_capacities
        c=SimpleNamespace(fingerprint='c',entities={'steel-furnace':{'crafting_speed':2}},recipes={'iron-plate':dict(energy=3.2,
            ingredients=[dict(name='iron-ore',amount=1)],products=[dict(name='iron-plate',amount=1)])})
        source={'banks':[dict(resource='iron-ore',product='iron-plate',entities=[entity('steel-furnace',i*4,0)for i in range(10)])]}
        before=deepcopy(source)
        with patch('factorio_agent.depleted_drill_retirement.mining_capacity_summary',return_value={'ok':True,'resources':{'iron-ore':264}}):
            capacities,evidence=_source_capacities(self.game,c,source)
        self.assertEqual(capacities['iron-ore'],264);self.assertEqual(source,before)
        with patch('factorio_agent.depleted_drill_retirement.mining_capacity_summary',return_value={'ok':False,'reason':'source_unpowered'}):
            with self.assertRaises(ValueError):_source_capacities(self.game,c,source)

    def test_duplicate_physical_drill_cannot_multiply_two_declared_bank_capacities(self):
        doc=json.loads((self.root/'mining-outposts.json').read_text());doc['banks'].append(deepcopy(doc['banks'][0]))
        (self.root/'mining-outposts.json').write_text(json.dumps(doc))
        rows=[dict(index=i,resource='iron-ore',left=33,right=33,installed_left=33,installed_right=33,
            drills=[dict(unit_number=q['unit_number'],drop_lane_verified=True)for q in self.obs['entities']if q['name']=='electric-mining-drill'])for i in(0,1)]
        self.game.query.return_value=dict(ok=True,tick=20,belt_speed=.03125,banks=rows)
        c=SimpleNamespace(fingerprint='c',entities={'iron-ore':{'mining_products':[dict(name='iron-ore',amount=1)]}})
        self.assertFalse(mining_capacity_summary(self.game,c)['ok'])

    def test_fully_receipted_bank_is_inactive_zero_not_a_missing_drill_rebuild(self):
        state=completed(self.state,self.target);second=next(q for q in self.obs['entities']if q['name']=='electric-mining-drill'and q['unit_number']!=self.target['unit_number'])
        target=dict(state['targets'][0],position=second['position'],unit_number=second['unit_number'],direction=second['direction'],phase='complete')
        target['retirement_id']=_hash([MODE,'w','c',target['unit_number'],target['position'],target['direction'],target['owner_revision']]);state['targets'].append(target)
        row=dict(retirement_id=target['retirement_id'],native=receipt(target),previous_receipt=state['receipts'][-1]['receipt_id']);row['receipt_id']=_hash(row);state['receipts'].append(row)
        (self.root/FILE).write_text(json.dumps(state))
        from factorio_agent.mining_layout_policy import diagnose_bank_capacity
        c=SimpleNamespace(fingerprint='c',entities={'iron-ore':{'mining_products':[dict(name='iron-ore',amount=1)]}})
        self.game.query.return_value=dict(ok=True,tick=20,drills=[],mining_speed=.5,mining_time=1,belt_speed=.03125,force_productivity_bonus=.1)
        obs=dict(self.obs,entities=[q for q in self.obs['entities']if q['name']!='electric-mining-drill'])
        result=diagnose_bank_capacity(self.game,obs,c,{'plan':self.plan})
        self.assertTrue(result['ok'],result);self.assertEqual(result['active_collector_ceiling_per_minute'],0)
        self.assertEqual(result['counts']['planned'],0);self.assertEqual(result['historical_planned_drill_count'],2)
        self.assertEqual(set(result['paid_retired_drill_units']),{self.target['unit_number'],second['unit_number']})


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('''d={world_id="w"};f={index=1};s={index=1};game={tick=10};pos=function(p)return{x=p.x,y=p.y}end
x={world_id="w",catalog_fingerprint="c",actor_unit=1,mode="paid_depleted_electric_drill_v1",retirement_id="r",position={x=.5,y=.5},direction=8,unit_number=100}
prototypes={entity={["electric-mining-drill"]={mining_drill_radius=2.49}}}
e={valid=true,name="electric-mining-drill",position=x.position,direction=8,unit_number=100,force=f,surface=s,quality={name="normal"},health=300,max_health=300,minable=true,prototype=prototypes.entity["electric-mining-drill"]}
e.get_module_inventory=function()return nil end;e.get_output_inventory=function()return nil end;e.is_connected_to_electric_network=function()return false end
ore={};s.find_entities_filtered=function(q)assert(q.type=="resource");assert(q.area[1][1]==.5-2.49);return ore end
inv={{valid_for_read=true,name="electric-mining-drill",quality={name="normal"},count=3}};inv.valid=true
inv.get_insertable_count=function(q)assert(q.name=="electric-mining-drill"and q.quality=="normal");return 10 end
a={valid=true,force=f,surface=s,unit_number=1};a.get_main_inventory=function()return inv end
a.mine_entity=function(q)assert(q==e);inv[1].count=inv[1].count+1;e.valid=false;return true end
target=function()return e.valid and e or nil end''')

    def test_normal_engine_mining_conserves_exact_one_drill_and_known_replay_never_duplicates(self):
        result=self.lua.execute(RETIRE_LUA);self.assertTrue(result['ok']);self.assertEqual(result['before_drills'],3);self.assertEqual(result['after_drills'],4)
        self.assertEqual(result['remaining_any_resource'],0);self.assertEqual(result['force_index'],1)
        again=self.lua.execute(RETIRE_LUA);self.assertTrue(again['ok']);self.assertEqual(self.lua.eval('inv[1].count'),4)

    def test_any_positive_resource_even_foreign_and_unpowered_idle_is_not_depleted(self):
        self.lua.execute('ore={{name="copper-ore",amount=1}}')
        self.assertEqual(self.lua.execute(RETIRE_LUA)['reason'],'drill_retirement_any_resource_still_in_native_radius')
        self.assertTrue(self.lua.eval('e.valid'));self.assertEqual(self.lua.eval('inv[1].count'),3)
        self.lua.execute('x.drills={{name="electric-mining-drill",position=x.position,direction=8,unit_number=100}}')
        result=self.lua.execute(SURVEY_LUA);self.assertFalse(result['drills'][1]['powered']);self.assertEqual(result['drills'][1]['remaining_any_resource'],1)

    def test_damage_modules_output_quality_or_inventory_capacity_never_discards_goods(self):
        for mutation in('e.health=299','e.quality.name="uncommon"','e.get_module_inventory=function()return{is_empty=function()return false end}end',
                'e.get_output_inventory=function()return{is_empty=function()return false end}end','inv.get_insertable_count=function()return 0 end'):
            self.setUp();self.lua.execute(mutation);self.assertFalse(self.lua.execute(RETIRE_LUA)['ok'])
            self.assertTrue(self.lua.eval('e.valid'));self.assertEqual(self.lua.eval('inv[1].count'),3)

    def test_exception_after_possible_effect_latches_unknown_without_second_mine(self):
        self.lua.execute('a.mine_entity=function(q)inv[1].count=inv[1].count+1;e.valid=false;error("unexpected after mining")end')
        self.assertEqual(self.lua.execute(RETIRE_LUA)['reason'],UNKNOWN)
        self.assertEqual(self.lua.execute(RETIRE_LUA)['reason'],UNKNOWN);self.assertEqual(self.lua.eval('inv[1].count'),4)

    def test_wrong_actor_world_surface_unit_or_position_fails_before_payment(self):
        for mutation in('a.unit_number=2','d.world_id="other"','e.surface={}','e.unit_number=99','e.position={x=1.5,y=.5}'):
            self.setUp()
            self.lua.execute(mutation);self.assertFalse(self.lua.execute(RETIRE_LUA)['ok']);self.assertEqual(self.lua.eval('inv[1].count'),3)

    def test_extension_richness_is_required_only_for_new_drills_but_union_stays_unique(self):
        from factorio_agent.paired_mining import resource_survey
        specs=[entity('electric-mining-drill',.5,.5,8),entity('electric-mining-drill',4.5,.5,8)]
        game=SimpleNamespace(query=Mock());resource_survey(game,{'resource':'iron-ore','entities':specs},required_drills=specs[1:])
        script=game.query.call_args.args[0]
        encoded=script.split('helpers.json_to_table(',1)[1].split(');local proto',1)[0]
        args=json.loads(json.loads(encoded));self.assertEqual([q['require_coverage']for q in args['drills']],[False,True])
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''args={resource="iron-ore",drills={{position={x=.5,y=.5},require_coverage=false},{position={x=4.5,y=.5},require_coverage=true}}}
helpers={json_to_table=function()return args end};s={};prototypes={entity={["electric-mining-drill"]={mining_drill_radius=2.49,mining_speed=.5},["iron-ore"]={mineable_properties={mining_time=1}}}}
shared={name="iron-ore",amount=2,position={x=2.5,y=.5}}
s.find_entities_filtered=function(q)if q.area[1][1]<0 then return{shared}else return{shared,{name="iron-ore",amount=999,position={x=3.5,y=.5}},{name="iron-ore",amount=999,position={x=4.5,y=.5}},{name="iron-ore",amount=999,position={x=5.5,y=.5}}}end end''')
        result=lua.execute(script);self.assertTrue(result['ok']);self.assertEqual(result['unique_remaining_resource'],2999)
        lua.execute('s.find_entities_filtered=function()return{shared}end')
        self.assertEqual(lua.execute(script)['reason'],'paired_mining_insufficient_resource')


if __name__=='__main__':unittest.main()
