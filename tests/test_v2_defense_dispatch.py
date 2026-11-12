from types import SimpleNamespace
from unittest.mock import Mock
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
        ex.starter_upkeep=Mock(return_value={'status':'blocked','reason':'old_hotspot_missing'})
        return ex

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


if __name__=='__main__':unittest.main()
