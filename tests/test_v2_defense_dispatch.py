from types import SimpleNamespace
from unittest.mock import Mock,patch
from pathlib import Path
import json
import tempfile
import unittest

from factorio_agent.jobs import StarterExecutors


class EmergencyDispatchTests(unittest.TestCase):
    def executor(self):
        ex=StarterExecutors.__new__(StarterExecutors)
        ex.builder=SimpleNamespace(_sync=Mock())
        ex.hardening=SimpleNamespace(step=Mock(return_value={'type':'build','name':'gun-turret'}),upkeep=Mock(return_value=None))
        ex.perimeter=SimpleNamespace(step=Mock(return_value={'status':'succeeded'}))
        ex.defense_repairs=SimpleNamespace(next_action=Mock(return_value=None))
        ex.normal_research=SimpleNamespace(catalog=SimpleNamespace(technologies={}))
        ex.bastion_maintenance=SimpleNamespace(step=Mock(return_value=None))
        ex.nest_assault=SimpleNamespace(step=Mock(return_value={'type':'craft','recipe':'piercing-rounds-magazine'}))
        ex.nest_sweep=SimpleNamespace(step=Mock(return_value={'type':'craft','recipe':'piercing-rounds-magazine'}))
        ex.radars=SimpleNamespace(step=Mock(return_value={'type':'build','name':'radar'}))
        ex.piercing_ammo=SimpleNamespace(step=Mock(return_value={'type':'build','name':'assembling-machine-1'}))
        ex.construction_mall=SimpleNamespace(step=Mock(return_value={'type':'build','name':'assembling-machine-1'}))
        ex.water_distribution=SimpleNamespace(step=Mock(return_value={'type':'build','name':'pipe'}))
        ex.starter_upkeep=Mock(return_value={'status':'blocked','reason':'old_hotspot_missing'})
        return ex

    def approved_initial(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name)
        ex=self.executor();ex.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
        ex.perimeter.state={};ex.perimeter.step.return_value={'type':'build','name':'gun-turret'}
        ex.starter_observation=Mock(side_effect=lambda obs:obs)
        obs={'world_id':'w','enabled_recipes':{'gun-turret':True,'firearm-magazine':True}}
        ledger={'schema_version':2,'world_id':'w','jobs':{'defense':{
            'key':'defense','world_id':'w','capability':'defense_perimeter','parameters':{},
            'status':'blocked','reason':'native_gun_turret_and_normal_magazine_recipes_required'}}}
        path=root/'jobs.json';path.write_text(json.dumps(ledger),encoding='utf-8')
        return ex,obs,ledger,path

    def test_explicit_perimeter_build_bypasses_damaged_starter_and_remote_power(self):
        ex=self.executor();ex.builder.ensure_power=Mock(side_effect=AssertionError('no power needed for guns'))
        self.assertEqual(ex.step('defense_perimeter',{})['status'],'succeeded')
        ex.starter_upkeep.assert_not_called();ex.builder.ensure_power.assert_not_called()
        ex.perimeter.step.assert_called_once_with({})

    def test_waiting_initial_defense_maintains_fuel_without_recursive_upkeep_or_remote_power(self):
        ex,obs,ledger,path=self.approved_initial()
        waiting={'status':'waiting','reason':'waiting for drill-fed iron-plate production','have':39,'need':40}
        ex.perimeter.step.return_value=waiting
        filtered={'world_id':'w','entities':[{'name':'burner-mining-drill','unit_number':1165}]}
        ex.starter_observation.return_value=filtered;ex.starter_observation.side_effect=None
        fuel={'type':'insert','item':'coal','count':1,'name':'burner-mining-drill'}
        ex.bootstrap=SimpleNamespace(maintain_fuel=Mock(return_value=fuel))
        ex.builder.ensure_power=Mock(side_effect=AssertionError('no remote power repair'))
        self.assertEqual(ex.step('starter_capacity',obs,{'resource':'copper-ore','cells':8}),fuel)
        ex.bootstrap.maintain_fuel.assert_called_once_with(filtered)
        ex.starter_upkeep.assert_not_called();ex.builder.ensure_power.assert_not_called()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),ledger)

    def test_explicit_defense_wait_uses_only_a_real_fuel_action_and_keeps_original_wait_otherwise(self):
        ex=self.executor();obs={'world_id':'w'}
        waiting={'status':'waiting','reason':'engine crafting queue is busy'}
        ex.perimeter.step.return_value=waiting;ex.starter_observation=Mock(return_value=obs)
        fuel={'type':'insert','item':'coal','count':1}
        ex.bootstrap=SimpleNamespace(maintain_fuel=Mock(return_value=fuel))
        self.assertEqual(ex.step('defense_perimeter',obs),fuel)
        for unavailable in (None,{'status':'waiting','reason':'coal not available'},{'status':'succeeded'}):
            ex.bootstrap.maintain_fuel.return_value=unavailable
            self.assertIs(ex.step('defense_perimeter',obs),waiting)
        ex.starter_upkeep.assert_not_called()

    def test_real_defense_actions_success_and_blockers_never_invoke_fuel_fallback(self):
        ex=self.executor();ex.bootstrap=SimpleNamespace(maintain_fuel=Mock(side_effect=AssertionError('defense first')))
        for result in ({'type':'build','name':'gun-turret'},{'type':'craft','recipe':'gun-turret'},
                {'type':'insert','item':'firearm-magazine'},{'status':'succeeded'},{'status':'blocked','reason':'site_changed'}):
            ex.perimeter.step.return_value=result
            self.assertIs(ex.step('defense_perimeter',{}),result)
        ex.bootstrap.maintain_fuel.assert_not_called();ex.starter_upkeep.assert_not_called()

    def test_waiting_known_post_ammo_upkeep_can_restart_its_fuel_prerequisite(self):
        ex,obs,ledger,path=self.approved_initial();ex.perimeter.state={'phase':'armed'}
        ex.perimeter.upkeep=Mock(return_value={'status':'waiting','reason':'waiting for iron to craft ammunition'})
        fuel={'type':'insert','item':'coal','count':1};ex.bootstrap=SimpleNamespace(maintain_fuel=Mock(return_value=fuel))
        self.assertEqual(ex.step('bootstrap_research',obs,{'technology':'electric-mining-drill'}),fuel)
        ex.perimeter.step.assert_not_called();ex.starter_upkeep.assert_not_called()

    def test_approved_previously_locked_defense_precedes_ordinary_work_without_fake_ledger_success(self):
        ex,obs,ledger,path=self.approved_initial();before=path.read_bytes()
        self.assertEqual(ex.step('starter_capacity',obs,{'resource':'copper-ore','cells':8})['name'],'gun-turret')
        ex.starter_upkeep.assert_not_called();self.assertEqual(path.read_bytes(),before)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['jobs']['defense']['status'],'blocked')
        ex.perimeter.step.return_value={'status':'succeeded','reason':'real armed perimeter'}
        ex.starter_upkeep.return_value={'status':'succeeded'};ex._continue_background_construction=Mock(return_value=None)
        ex.capacity=SimpleNamespace(step=Mock(return_value={'type':'take','item':'coal'}))
        self.assertEqual(ex.step('starter_capacity',obs,{'resource':'copper-ore','cells':8}),{'type':'take','item':'coal'})
        self.assertEqual(path.read_bytes(),before)

    def test_no_approval_world_mismatch_native_recipe_or_unknown_status_never_initiates_defense(self):
        ex,obs,ledger,path=self.approved_initial()
        variants=[{**ledger,'jobs':{}},{**ledger,'world_id':'foreign'},
            {**ledger,'jobs':{'defense':{**ledger['jobs']['defense'],'world_id':'foreign'}}},
            {**ledger,'jobs':{'defense':{**ledger['jobs']['defense'],'status':'unrecognized'}}}]
        for value in variants:
            path.write_text(json.dumps(value),encoding='utf-8')
            self.assertIsNone(ex._approved_initial_defense(obs))
        path.write_text(json.dumps(ledger),encoding='utf-8')
        for item in ('gun-turret','firearm-magazine'):
            obs['enabled_recipes'][item]=False;self.assertIsNone(ex._approved_initial_defense(obs));obs['enabled_recipes'][item]=True
        ex.perimeter.step.assert_not_called()

    def test_any_same_world_uncertain_intent_even_empty_blocks_background_defense(self):
        ex,obs,ledger,path=self.approved_initial()
        for extra in ({'pending_action':{}},{'pending_action':{'type':'build'}},
                {'reason':'action_outcome_unknown_reconcile_required'}, {'reason':'world_rollback_requires_reconciliation'}):
            with self.subTest(extra=extra):
                ledger['jobs']['other']={'key':'other','world_id':'w','capability':'mining_outpost','status':'blocked',**extra}
                path.write_text(json.dumps(ledger),encoding='utf-8');before=path.read_bytes()
                self.assertIsNone(ex._approved_initial_defense(obs));self.assertEqual(path.read_bytes(),before)
        ex.perimeter.step.assert_not_called()

    def test_armed_configuration_is_not_recommissioned_and_all_known_approval_statuses_work(self):
        ex,obs,ledger,path=self.approved_initial()
        ex.perimeter.state={'phase':'armed','posts':[{'name':'owned','unit_number':51,'commissioned':True}]}
        self.assertIsNone(ex._approved_initial_defense(obs));ex.perimeter.step.assert_not_called()
        ex.perimeter.state={}
        for status in ('pending','running','waiting','paused','blocked','failed','succeeded'):
            ledger['jobs']['defense']['status']=status;path.write_text(json.dumps(ledger),encoding='utf-8')
            self.assertEqual(ex._approved_initial_defense(obs)['name'],'gun-turret')

    def test_non_gun_research_finishes_approved_defense_first_but_gun_proof_is_not_preempted(self):
        ex,obs,ledger,path=self.approved_initial()
        target=SimpleNamespace(step=Mock(return_value={'status':'waiting','reason':'machine target proof'}))
        ex._bootstrap_research=Mock(return_value=target)
        for technology in ('stone-wall','electric-mining-drill'):
            self.assertEqual(ex.step('bootstrap_research',obs,{'technology':technology})['name'],'gun-turret')
        ex._bootstrap_research.assert_not_called();ex.perimeter.step.reset_mock()
        self.assertEqual(ex.step('bootstrap_research',obs,{'technology':'gun-turret'})['reason'],'machine target proof')
        ex.perimeter.step.assert_not_called();ex.starter_upkeep.assert_not_called()

    def test_known_armed_posts_keep_normal_ammo_upkeep_during_long_non_gun_machine_research(self):
        ex,obs,ledger,path=self.approved_initial()
        ex.perimeter.state={'phase':'armed'}
        ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine','count':5})
        ex._bootstrap_research=Mock(side_effect=AssertionError('refill existing approved posts first'))
        result=ex.step('bootstrap_research',obs,{'technology':'electric-mining-drill'})
        self.assertEqual((result['type'],result['item']),('insert','firearm-magazine'))
        ex.perimeter.step.assert_not_called();ex.starter_upkeep.assert_not_called()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),ledger)

    def test_partial_reserved_perimeter_reuses_same_posts_and_builds_only_the_missing_gun(self):
        from factorio_agent.defense_perimeter import DefensePerimeter
        ex,obs,ledger,path=self.approved_initial()
        perimeter=DefensePerimeter.__new__(DefensePerimeter)
        perimeter.state={'phase':'deploy','posts':[
            {'name':'existing','position':{'x':1,'y':2},'unit_number':51,'commissioned':True},
            {'name':'reserved','position':{'x':5,'y':2},'commissioned':False}]}
        perimeter.prepare=Mock(side_effect=AssertionError('reserved posts must not be resurveyed'))
        perimeter._save=Mock();perimeter.game=SimpleNamespace(query=Mock(return_value={'ok':True,'resources':0}))
        perimeter.inspect=Mock(return_value={'ok':True,'changed':[],'actor_full_magazines':10,'posts':[
            {'name':'existing','unit_number':51,'full_magazines':10}, {'name':'reserved','missing':True}]})
        ex.perimeter=perimeter;obs['inventory']={'gun-turret':1,'firearm-magazine':10}
        result=ex.step('starter_capacity',obs,{'resource':'copper-ore','cells':8})
        self.assertEqual((result['type'],result['name'],result['position']),('build','gun-turret',{'x':5,'y':2}))
        self.assertEqual(perimeter.state['posts'][0]['unit_number'],51);perimeter.prepare.assert_not_called()
        ex.starter_upkeep.assert_not_called()

    def test_new_mutual_bastion_construction_is_not_deadlocked_by_old_missing_post(self):
        ex=self.executor()
        self.assertEqual(ex.step('defense_hardening',{})['type'],'build')
        ex.starter_upkeep.assert_not_called();ex.hardening.step.assert_called_once()

    def test_watch_repairs_after_arming_and_keeps_waiting_when_quiet(self):
        ex=self.executor();ex.defense_repairs.next_action.return_value={'type':'repair'}
        self.assertEqual(ex.step('defense_watch',{})['type'],'repair')
        ex.defense_repairs.next_action.return_value=None
        self.assertEqual(ex.step('defense_watch',{})['status'],'waiting')

    def test_interrupted_native_tool_cleanup_precedes_emergency_build_or_inventory_use(self):
        ex=self.executor()
        obs=dict(world_id='w',actor_unit_number=11,repair_pending=dict(request='r'))
        self.assertEqual(ex.step('defense_hardening',obs)['type'],'finish_repair')
        ex.hardening.step.assert_not_called();ex.builder._sync.assert_not_called()

    def test_concentrated_sweep_converts_reserved_precursors_before_base_topoff(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'status':'waiting','reason':'factory_empty'})
        self.assertEqual(ex.step('nest_sweep',{})['recipe'],'piercing-rounds-magazine')
        ex.perimeter.upkeep.assert_not_called()
        ex.perimeter.upkeep.return_value={'type':'insert','item':'firearm-magazine'}
        ex.nest_sweep.step.return_value={'status':'waiting','reason':'native_crafting_queue_busy'}
        self.assertEqual(ex.step('nest_sweep',{})['item'],'firearm-magazine')

    def test_old_staged_capability_cannot_resume_harassment(self):
        from factorio_agent.jobs import CAPABILITIES
        self.assertNotIn('nest_assault',CAPABILITIES)
        ex=self.executor()
        self.assertEqual(ex.step('nest_assault',{})['reason'],'staged_assault_retired_use_concentrated_nest_sweep')
        ex.nest_assault.step.assert_not_called()

    def test_locally_defended_radar_build_is_not_starved_by_global_refill(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine'})
        self.assertEqual(ex.step('radar_network',{})['name'],'radar')
        ex.perimeter.upkeep.assert_not_called();ex.starter_upkeep.assert_not_called()
        ex.radars.step.return_value={'status':'waiting','reason':'waiting_native_crafting'}
        self.assertEqual(ex.step('radar_network',{})['item'],'firearm-magazine')

    def test_bulk_ammo_factory_build_precedes_topoff_and_services_base_while_waiting(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine'})
        self.assertEqual(ex.step('piercing_ammo',{})['name'],'assembling-machine-1')
        ex.perimeter.upkeep.assert_not_called();ex.starter_upkeep.assert_not_called()
        ex.piercing_ammo.step.return_value={'status':'waiting','reason':'native_assembler_production'}
        self.assertEqual(ex.step('piercing_ammo',{})['item'],'firearm-magazine')

    def test_construction_mall_uses_its_product_and_keeps_base_supplied_while_producing(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine'})
        self.assertEqual(ex.step('construction_mall',{}, {'product':'transport-belt'})['type'],'build')
        ex.construction_mall.step.assert_called_once_with({}, {'product':'transport-belt'})
        ex.perimeter.upkeep.assert_not_called()
        ex.construction_mall.step.return_value={'status':'waiting','reason':'normal_machine_production'}
        self.assertEqual(ex.step('construction_mall',{}, {'product':'transport-belt'})['item'],'firearm-magazine')

    def test_bootstrap_stock_provider_uses_only_a_verified_matching_mall_cell(self):
        ex=self.executor()
        factory=ex.construction_mall
        factory.has_stock_cell=Mock(return_value=False)
        factory.ensure_stock=Mock(return_value={'status':'waiting'})
        self.assertIsNone(ex._construction_stock({},'transport-belt',200))
        factory.ensure_stock.assert_not_called()
        factory.has_stock_cell.return_value=True
        self.assertEqual(ex._construction_stock({},'transport-belt',200),{'status':'waiting'})
        factory.ensure_stock.assert_called_once_with({},'transport-belt',200)

    def test_factory_inputs_are_serviced_during_stock_wait_but_not_committed_combat(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value=None)
        ex.nest_sweep.state={};ex.nest_sweep.step.return_value={'status':'waiting'}
        ex.piercing_ammo.commissioned=True
        ex.piercing_ammo.upkeep=Mock(return_value={'type':'supply_piercing_input','item':'iron-plate'})
        self.assertEqual(ex.step('nest_sweep',{})['type'],'supply_piercing_input')
        ex.piercing_ammo.upkeep.reset_mock();ex.nest_sweep.state={'started_tick':100}
        self.assertEqual(ex.step('nest_sweep',{})['status'],'waiting')
        ex.piercing_ammo.upkeep.assert_not_called()

    def test_watch_keeps_defense_alive_when_a_factory_needs_recovery(self):
        ex=self.executor();ex.piercing_ammo.commissioned=True
        ex.piercing_ammo.upkeep=Mock(return_value={'status':'blocked','reason':'owned_factory_damaged'})
        result=ex.step('defense_watch',{})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['evidence']['piercing_factory']['reason'],'owned_factory_damaged')

    def test_watch_automatically_restores_an_actually_depleted_original_coal_source(self):
        ex=self.executor()
        ex.base_coal_recovery=SimpleNamespace(state={},
            depletion_diagnostic=Mock(return_value={'ok':True,'depleted':True}),
            step=Mock(return_value={'type':'build_many','reason':'restore normal boiler coal feed'}))
        result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['type'],'build_many')
        ex.perimeter.step.assert_not_called()

    def test_healthy_original_coal_source_does_not_start_an_unneeded_branch(self):
        ex=self.executor()
        ex.base_coal_recovery=SimpleNamespace(state={},
            depletion_diagnostic=Mock(return_value={'ok':True,'depleted':False}),step=Mock())
        self.assertEqual(ex.step('defense_watch',{'tick':100})['status'],'waiting')
        ex.base_coal_recovery.step.assert_not_called()

    def test_failed_coal_preparation_keeps_watch_running_and_throttles_heavy_replans(self):
        ex=self.executor()
        ex.base_coal_recovery=SimpleNamespace(state={},
            depletion_diagnostic=Mock(return_value={'ok':True,'depleted':True}),
            step=Mock(return_value={'status':'blocked','reason':'coal_route_requires_review'}))
        result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['evidence']['coal_recovery']['reason'],'coal_route_requires_review')
        ex.step('defense_watch',{'tick':101})
        self.assertEqual(ex.base_coal_recovery.step.call_count,1)

    def test_mall_power_guard_can_restore_coal_in_its_existing_job(self):
        ex=self.executor()
        ex.construction_mall.step.return_value={'status':'blocked','reason':'mall_power_margin_changed'}
        ex.base_coal_recovery=SimpleNamespace(state={'phase':'build'},
            verified=False,
            step=Mock(return_value={'type':'build','name':'transport-belt'}))
        result=ex.step('construction_mall',{'tick':100},{'product':'transport-belt'})
        self.assertEqual(result['type'],'build')
        ex.base_coal_recovery.step.return_value={'status':'waiting','reason':'sampling four engines'}
        self.assertEqual(ex.step('construction_mall',{'tick':101},{'product':'transport-belt'})['status'],'waiting')

    def test_mall_finishes_coal_sampling_and_publication_even_if_power_is_already_back(self):
        ex=self.executor()
        ex.base_coal_recovery=SimpleNamespace(state={'phase':'commission'},verified=False,
            step=Mock(return_value={'status':'waiting','reason':'sampling four engines'}))
        result=ex.step('construction_mall',{'tick':100},{'product':'transport-belt'})
        self.assertEqual(result['reason'],'mall_waiting_for_coal_recovery_publication')
        ex.construction_mall.step.assert_not_called()

    def test_paid_water_header_precedes_unrelated_upkeep_and_keeps_defenses_during_wait(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine'})
        self.assertEqual(ex.step('water_distribution',{})['name'],'pipe')
        ex.perimeter.upkeep.assert_not_called()
        ex.water_distribution.step.return_value={'status':'waiting','reason':'waiting for actual water'}
        self.assertEqual(ex.step('water_distribution',{})['item'],'firearm-magazine')

    def test_pipe_repair_runs_after_perimeter_arming_and_before_routine_factory_input(self):
        ex=self.executor()
        ex.infrastructure_repair=SimpleNamespace(last_diagnostic=None,
            step=Mock(return_value={'type':'repair_registered_infrastructure'}))
        self.assertEqual(ex.step('defense_watch',{'tick':100})['type'],'repair_registered_infrastructure')
        ex.perimeter.step.assert_called_once()
        ex.infrastructure_repair.step.reset_mock()
        ex.perimeter.step.return_value={'type':'insert','item':'firearm-magazine'}
        self.assertEqual(ex.step('defense_watch',{'tick':101})['type'],'insert')
        ex.infrastructure_repair.step.assert_not_called()

    def test_failed_pipe_survey_is_diagnostic_and_does_not_stop_defense_watch(self):
        ex=self.executor()
        ex.infrastructure_repair=SimpleNamespace(last_diagnostic={'ok':False,'reason':'foreign identity changed'},
            step=Mock(return_value=None))
        result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['evidence']['infrastructure']['reason'],'foreign identity changed')
        ex.step('defense_watch',{'tick':101})
        ex.infrastructure_repair.step.assert_called_once()

    def test_chemical_job_recovers_owned_upstream_pipes_before_requesting_new_consumer_work(self):
        ex=self.executor();ex.starter_upkeep.return_value={'status':'succeeded'}
        ex.starter_observation=Mock(return_value={})
        ex.chemicals=SimpleNamespace(step=Mock(return_value={'status':'waiting'}))
        ex.infrastructure_repair=SimpleNamespace(last_diagnostic=None,
            step=Mock(return_value={'type':'take_many','item':'pipe','count':6}))
        self.assertEqual(ex.step('chemical_production',{'tick':100},{'product':'sulfur'})['count'],6)
        ex.chemicals.step.assert_not_called()

    def test_paid_pipe_publication_completes_before_consumer_proof_is_reused(self):
        ex=self.executor();ex.starter_upkeep.return_value={'status':'succeeded'}
        ex.starter_observation=Mock(return_value={})
        ex.chemicals=SimpleNamespace(step=Mock(return_value={'status':'waiting'}))
        ex.infrastructure_repair=SimpleNamespace(last_diagnostic={'ok':True,'reason':'paid registered pipe recovery published'},
            step=Mock(return_value=None))
        self.assertEqual(ex.step('chemical_production',{'tick':100},{'product':'sulfur'})['status'],'waiting')
        ex.infrastructure_repair.step.assert_called_once()
        ex.chemicals.step.assert_called_once()

    def test_registered_belt_recovery_runs_after_arming_before_routine_wall_upkeep(self):
        ex=self.executor()
        ex.registered_belt_repair=SimpleNamespace(last_diagnostic=None,
            step=Mock(return_value={'type':'repair_registered_belts'}))
        self.assertEqual(ex.step('defense_watch',{'tick':100})['type'],'repair_registered_belts')
        ex.hardening.upkeep.assert_not_called()
        ex.registered_belt_repair.step.reset_mock()
        ex.perimeter.step.return_value={'type':'insert','item':'firearm-magazine'}
        self.assertEqual(ex.step('defense_watch',{'tick':101})['type'],'insert')
        ex.registered_belt_repair.step.assert_not_called()

    def test_foreign_belt_pin_stays_a_bounded_diagnostic_while_defense_continues(self):
        ex=self.executor()
        ex.registered_belt_repair=SimpleNamespace(last_diagnostic={'ok':False,'reason':'foreign current pin'},
            step=Mock(return_value=None))
        result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['evidence']['registered_belts']['reason'],'foreign current pin')
        ex.step('defense_watch',{'tick':101})
        ex.registered_belt_repair.step.assert_called_once()

    def test_blue_job_repairs_its_registered_ingredient_transport_before_planning(self):
        ex=self.executor();ex.starter_upkeep.return_value={'status':'succeeded'}
        ex.starter_observation=Mock(return_value={});ex.blue=SimpleNamespace(step=Mock())
        ex.registered_belt_repair=SimpleNamespace(last_diagnostic=None,
            step=Mock(return_value={'type':'take_mall_stock','item':'transport-belt','count':6}))
        self.assertEqual(ex.step('chemical_science',{'tick':100})['count'],6)
        ex.blue.step.assert_not_called()

    def test_world_bound_belt_dispatch_records_the_native_receipt(self):
        from factorio_agent.runtime import WorldBoundGame
        game=WorldBoundGame.__new__(WorldBoundGame)
        for name in ('repair_registered_belts','reconcile_registered_belts'):
            with self.subTest(name=name):
                action={'type':name};receipt={'ok':True,'paid_belts':6}
                game._record_action=Mock(return_value=receipt)
                with patch('factorio_agent.registered_belt_repair.execute_'+name,return_value=receipt)as execute:
                    self.assertEqual(game.act(action),receipt)
                execute.assert_called_once_with(game,action)
                game._record_action.assert_called_once_with(action,receipt)


if __name__=='__main__':unittest.main()
