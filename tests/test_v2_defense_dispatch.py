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

    def test_empty_base_ammo_buffer_does_not_prevent_native_offensive_progress(self):
        ex=self.executor();ex.perimeter.upkeep=Mock(return_value={'status':'waiting','reason':'factory_empty'})
        self.assertEqual(ex.step('nest_assault',{})['recipe'],'piercing-rounds-magazine')
        ex.perimeter.upkeep.return_value={'type':'insert','item':'firearm-magazine'}
        self.assertEqual(ex.step('nest_assault',{})['item'],'firearm-magazine')


if __name__=='__main__':unittest.main()
