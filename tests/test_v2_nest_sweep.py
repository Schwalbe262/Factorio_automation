from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.nest_sweep import (NestSweep,wave_candidates,choose_wave,wave_graph,deployment_wave,
    execute_start_sweep,SWEEP_START_STOCK_LUA,SWEEP_DEPLOYMENT_GUARD_LUA,_hash)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=SimpleNamespace(fingerprint='c',recipes={
            'piercing-rounds-magazine':{'energy':6,'ingredients':[{'name':'copper-plate','amount':2,'type':'item'},
                {'name':'steel-plate','amount':1,'type':'item'},{'name':'firearm-magazine','amount':2,'type':'item'}],
                'products':[{'name':'piercing-rounds-magazine','amount':2}]},
            'firearm-magazine':{'ingredients':[{'name':'iron-plate','amount':4,'type':'item'}],
                'products':[{'name':'firearm-magazine','amount':1}]}})
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),expected_world='w',backend='assisted',query=Mock())
        self.bootstrap=Mock();self.builder=Mock();self.perimeter=Mock();self.perimeter.ammo_source=None
        self.op=NestSweep(self.game,self.bootstrap,self.builder,self.cat,self.perimeter)
        self.target=dict(name='small-worm-turret',unit_number=1,position={'x':40,'y':0},world_id='w',surface_index=1,force_index=2,reviewed_range=25)
        self.posts=[dict(name='wave:'+str(i),position={'x':20+2*i,'y':0})for i in range(10)]
        self.op.state=dict(id='operation',world_id='w',catalog_fingerprint='c',actor_unit=11,phase='stock',targets=[self.target],
            target_set_hash=_hash([self.target]),ammo_item='piercing-rounds-magazine',handcrafted_magazines_issued=0,
            wave=dict(phase='deploy',posts=self.posts,bindings={},deployment_ids={},backstop_specs=[]))
        self.obs=dict(ok=True,world_id='w',actor_unit_number=11,inventory={'gun-turret':11,'iron-plate':1000,
            'steel-plate':100,'copper-plate':100,'piercing-rounds-magazine':50},entities=[])

    def test_generic_rotation_translation_free_geometry_has_ten_mutually_rooted_guns_and_target_coverage(self):
        targets=[{'position':{'x':40+dx,'y':dy}}for dx,dy in((-8,-4),(-4,4),(4,-5),(6,5),(0,0))]
        rears=[{'position':{'x':0,'y':-3}},{'position':{'x':0,'y':3}}]
        candidates,_=wave_candidates(targets,rears);self.assertLessEqual(len(candidates),256)
        selected=choose_wave(candidates,rears,targets);self.assertIsNotNone(selected)
        self.assertEqual(len(selected),10);self.assertTrue(wave_graph([q['position']for q in selected],rears,targets))
        moved_targets=[{'position':{k:t['position'][k]+v for k,v in(('x',100),('y',-200))}}for t in targets]
        moved_rears=[{'position':{k:t['position'][k]+v for k,v in(('x',100),('y',-200))}}for t in rears]
        moved,_=wave_candidates(moved_targets,moved_rears)
        self.assertIsNotNone(choose_wave(moved,moved_rears,moved_targets))

    def test_graph_rejects_uncovered_target_or_a_single_bridge_choke_point(self):
        positions=[q['position']for q in self.posts]
        self.assertFalse(wave_graph(positions,[{'position':{'x':0,'y':0}},{'position':{'x':0,'y':4}}],[self.target]))
        self.assertNotIn('op.loss_count>=2',SWEEP_DEPLOYMENT_GUARD_LUA)
        self.assertIn('sweep_target_not_covered_by_two_planned_guns',SWEEP_DEPLOYMENT_GUARD_LUA)

    def test_existing_red_stock_is_kept_and_missing_factory_is_actionable_without_handcraft(self):
        self.op._save();before=self.op.path.read_bytes()
        self.perimeter.ammo_source=SimpleNamespace(commissioned=True,ensure_full_magazines=Mock())
        with patch.object(self.op,'_craft')as craft:
            result=self.op._stock_wave(self.obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':100}})
            craft.assert_not_called()
        self.assertEqual((result['status'],result['required_capability']),('waiting','piercing_ammo'))
        self.assertEqual((result['held_full_stock'],result['missing_full_stock']),(100,100))
        self.assertEqual(self.op.path.read_bytes(),before);self.bootstrap.ensure_item.assert_not_called()
        self.perimeter.ammo_source.ensure_full_magazines.assert_not_called()

    def test_factory_provider_collects_bounded_red_batches_even_after_old_craft_budget(self):
        action={'type':'take_factory_magazines','item':'piercing-rounds-magazine','count':50}
        provider=SimpleNamespace(commissioned=True,ensure_full_magazines=Mock(return_value=action))
        self.op.ammo_source=provider;self.op.state['crafted_by_item']={'piercing-rounds-magazine':200,'firearm-magazine':200}
        self.op._save();before=self.op.path.read_bytes()
        with patch.object(self.op,'_craft')as craft:
            self.assertEqual(self.op._stock_wave(self.obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':100}}),action)
            craft.assert_not_called()
        provider.ensure_full_magazines.assert_called_once_with(self.obs,200)
        self.assertEqual(self.op.path.read_bytes(),before);self.bootstrap.ensure_item.assert_not_called()

    def test_provider_cannot_request_red_or_yellow_crafting_or_unbounded_transfers(self):
        provider=SimpleNamespace(commissioned=True,ensure_full_magazines=Mock());self.op.ammo_source=provider
        for action in({'type':'craft','recipe':'piercing-rounds-magazine','count':25},
                       {'type':'craft','recipe':'firearm-magazine','count':50},
                       {'type':'take_factory_magazines','item':'firearm-magazine','count':50},
                       {'type':'take_factory_magazines','item':'piercing-rounds-magazine','count':51}):
            provider.ensure_full_magazines.return_value=action
            result=self.op._stock_wave(self.obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':100}})
            self.assertEqual(result['status'],'blocked');self.bootstrap.ensure_item.assert_not_called()

    def test_factory_wait_and_complete_paid_stock_issue_no_supplement(self):
        provider=SimpleNamespace(commissioned=True,ensure_full_magazines=Mock(return_value={'status':'waiting','reason':'factory_empty'}))
        self.op.ammo_source=provider
        result=self.op._stock_wave(self.obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':100}})
        self.assertEqual(result['reason'],'factory_empty')
        provider.ensure_full_magazines.reset_mock()
        self.assertIsNone(self.op._stock_wave(self.obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':200}}))
        provider.ensure_full_magazines.assert_not_called();self.bootstrap.ensure_item.assert_not_called()

    def test_active_queue_wait_is_cooperative_and_keeps_checkpoint_byte_identical(self):
        self.op._save();before=self.op.path.read_bytes();obs=dict(self.obs,crafting_queue=[{'recipe':'piercing-rounds-magazine','count':25}])
        result=self.op._stock_wave(obs,{'main_gun_turrets':11,'main_ammo':{'full_normal_magazines':50}})
        self.assertEqual(result['status'],'waiting');self.assertEqual(self.op.path.read_bytes(),before)
        self.assertNotIn('pending_craft',self.op.state);self.perimeter.inspect.assert_not_called()

    def test_legacy_started_batch_reconciles_once_then_waits_without_rewriting_paid_history(self):
        action=self.op._craft('piercing-rounds-magazine',25,'old policy already-paid native batch fixture')
        rows=[dict(event='action_intent',key='same-sweep',tick=100,action=action),
              dict(event='action_result',key='same-sweep',tick=100,result={'ok':True,'started':25})]
        (self.root/'job-events.jsonl').write_text(''.join(json.dumps(q)+'\n'for q in rows))
        native=dict(ok=True,native_started=False,main_gun_turrets=11,main_ammo={'full_normal_magazines':100},posts=[],targets=[])
        obs=dict(self.obs,crafting_queue=[{'recipe':'piercing-rounds-magazine','count':25}])
        with patch.object(self.op,'inspect',return_value=native),patch.object(self.op,'_craft')as new_craft:
            self.assertEqual(self.op.step(obs)['reason'],'sweep_waiting_normally_paid_crafting_queue')
            saved=deepcopy(self.op.state);self.assertEqual(saved['crafted_by_item']['piercing-rounds-magazine'],50)
            self.assertNotIn('pending_craft',saved)
            self.assertEqual(self.op.step(obs)['reason'],'sweep_waiting_normally_paid_crafting_queue')
            self.assertEqual(self.op.state,saved);new_craft.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def native_committed(self,losses=3):
        posts=[];lost={}
        for i,p in enumerate(self.posts):
            present=i>=losses;posts.append(dict(p,paid=True,present=present,unit_number=100+i if present else None,
                marker_unit=100+i,ammo={'normal_rounds':100},health=400))
            if not present:lost[str(100+i)]=150
        return dict(ok=True,native_started=True,listener_owned=True,tick=200,started_tick=100,deadline_tick=10900,
            wave_group_id='paid',wave_launch_count=1,group_phase='complete',group_paid_turrets=10,group_loaded_magazines=200,
            group_loaded_rounds=2000,group_same_tick=True,loss_count=losses,posts=posts,lost_posts=lost,
            targets=[dict(self.target,present=True,health=200)],deaths={})

    def test_committed_wave_keeps_firing_after_three_losses_and_never_launches_or_rebuilds_another(self):
        self.op._save()
        with patch.object(self.op,'inspect',return_value=self.native_committed(3)):
            result=self.op.step(self.obs)
        self.assertEqual(result['status'],'waiting');self.assertEqual(result['reason'],'one_concentrated_paid_wave_firing_native_projectiles')
        self.bootstrap.ensure_item.assert_not_called();self.builder.ensure_plan.assert_not_called();self.perimeter._save.assert_not_called()

    def test_all_guns_lost_blocks_without_repayment_or_forward_rebuild(self):
        self.op._save()
        with patch.object(self.op,'inspect',return_value=self.native_committed(10)):
            result=self.op.step(self.obs)
        self.assertEqual(result['reason'],'sweep_single_wave_lost_no_forward_rebuild')
        self.bootstrap.ensure_item.assert_not_called();self.builder.ensure_plan.assert_not_called()

    def test_exact_native_deaths_required_absence_or_wrong_ids_never_prove_victory(self):
        native=self.native_committed(0);native['targets'][0]['present']=False
        self.assertIsNone(self.op._sweep_proof(native));native['deaths']={'1':{'own_gun_projectile':True}}
        self.assertEqual(self.op._sweep_proof(native)['targets_cleared'],1)
        native['targets'][0]['unit_number']=2;native['deaths']['2']={'own_gun_projectile':True}
        self.assertIsNone(self.op._sweep_proof(native))

    def test_prelaunch_support_only_deaths_never_claim_a_committed_single_wave(self):
        native=self.native_committed(0);native['targets'][0]['present']=False;native['deaths']={'1':{'own_gun_projectile':True}}
        self.assertIsNotNone(self.op._sweep_proof(native))
        for key,value in(('wave_group_id',None),('group_phase','intent'),('group_paid_turrets',0),
                ('group_loaded_magazines',0),('group_loaded_rounds',1999),('group_same_tick',False)):
            changed=dict(native,**{key:value})
            self.assertIsNone(self.op._sweep_proof(changed),key)

    def test_old_paused_checkpoint_is_not_rewritten_and_no_prior_checkpoint_is_required(self):
        old=self.root/'nest-assault.json';old.write_text('old paused history')
        self.op._save();self.assertEqual(old.read_text(),'old paused history')
        exported=deployment_wave(self.op.state);self.assertEqual(exported['world_id'],'w')
        self.op.state['phase']='stopped'
        with self.assertRaises(ValueError):deployment_wave(self.op.state)

    def test_optional_compatible_unlaunched_intent_is_pinned_other_world_is_ignored_without_rewriting_history(self):
        report=dict(ok=True,tick=100,gun={'range':18},force_modifiers={'ammo_damage':.2,'turret_damage':.2,'gun_speed':.3},
            recipes_enabled={'piercing-rounds-magazine':True},targets=[dict(self.target,role='worm',dangerously_close=True,
                factory_distance=20,combat={'range':25})])
        self.op.state={};self.op.threats.survey=Mock(return_value=report)
        selected={'ok':True,'wave':{'posts':[]},'surveyed_candidates':10,'native_legal_candidates':10}
        prior=dict(id='old',world_id='w',catalog_fingerprint='c',actor_unit=11,phase='stock',targets=[self.target],target_set_hash=_hash([self.target]))
        path=self.root/'nest-assault.json';path.write_text(json.dumps(prior));before=path.read_bytes()
        with patch.object(self.op,'_choose_wave',return_value=selected):r=self.op.prepare(self.obs)
        self.assertEqual(r['target_source']['kind'],'compatible_unlaunched_prior_intent');self.assertEqual(path.read_bytes(),before)
        prior['world_id']='another-world';path.write_text(json.dumps(prior));before=path.read_bytes()
        with patch.object(self.op,'_choose_wave',return_value=selected):r=self.op.prepare(self.obs)
        self.assertEqual(r['target_source']['kind'],'fresh_charted_stationary_cluster');self.assertEqual(path.read_bytes(),before)
        path.unlink()
        with patch.object(self.op,'_choose_wave',return_value=selected):r=self.op.prepare(self.obs)
        self.assertEqual(r['target_source']['kind'],'fresh_charted_stationary_cluster');self.assertFalse(self.op.path.exists())

    def test_unknown_start_outcome_never_succeeds_or_silently_retries(self):
        self.op._save();a=dict(type='start_nest_sweep',operation_id='operation',target_set_hash=self.op.state['target_set_hash'],
            expected_world_id='w',expected_actor_unit=11)
        self.game.query.side_effect=TimeoutError()
        self.assertEqual(execute_start_sweep(self.game,a)['reason'],'nest_sweep_initialization_outcome_unknown')

    @unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
    def test_native_start_clock_requires_two_hundred_full_red_and_ten_plain_guns_without_normalizing_partial(self):
        lua=LuaRuntime();lua.execute('''d={};x={operation_id="operation",expected_actor_unit=11};a={valid=true,unit_number=11,crafting_queue_size=0}
main={{valid_for_read=true,name="gun-turret",quality={name="normal"},count=10,health=1,spoil_tick=0},
 {valid_for_read=true,name="piercing-rounds-magazine",quality={name="normal"},count=200,health=1,spoil_tick=0,ammo=7,prototype={magazine_size=10}}}
a.get_main_inventory=function()return main end''')
        run=lambda:lua.execute('return(function() '+' '.join(SWEEP_START_STOCK_LUA.splitlines())+' end)()')
        self.assertEqual(run()['reason'],'sweep_launch_requires_idle_full_stock')
        self.assertEqual(lua.globals().main[2].ammo,7);self.assertIsNone(lua.globals().d.nest_assaults)
        lua.execute('main[2].ammo=10');self.assertIsNone(run())
        lua.execute('a.crafting_queue_size=1');self.assertEqual(run()['reason'],'sweep_launch_requires_idle_full_stock')


if __name__=='__main__':unittest.main()
