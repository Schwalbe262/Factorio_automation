from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest

from factorio_agent.jobs import Request,StarterExecutors


class BackgroundConstructionTests(unittest.TestCase):
    def executor(self):
        ex=StarterExecutors.__new__(StarterExecutors)
        ex.builder=SimpleNamespace(_sync=Mock())
        ex.normal_research=SimpleNamespace(catalog=SimpleNamespace(technologies={}))
        ex.bastion_maintenance=SimpleNamespace(step=Mock(return_value=None))
        ex.perimeter=SimpleNamespace(step=Mock(return_value={'status':'succeeded'}))
        ex.hardening=SimpleNamespace(upkeep=Mock(return_value=None))
        ex.defense_repairs=SimpleNamespace(next_action=Mock(return_value=None))
        ex.construction_mall=SimpleNamespace(pending_items=(),has_stock_cell=lambda p:p=='transport-belt',
            step=Mock(return_value={'type':'build_many','reason':'new capped cell'}))
        ex.radar_coverage=SimpleNamespace(state={'phase':'build'},
            upkeep=Mock(return_value={'type':'build_many','reason':'native radar coverage'}))
        ex.mall_auto_enabled=True;ex.radar_auto_enabled=True
        return ex

    def test_requested_restock_family_starts_with_underground_belts(self):
        ex=self.executor()
        self.assertEqual(ex._mall_queue_upkeep({'tick':100})['type'],'build_many')
        ex.construction_mall.step.assert_called_once_with({'tick':100},{'product':'underground-belt'})

    def test_incomplete_cell_precedes_a_new_radar_board_snapshot(self):
        ex=self.executor();ex.construction_mall.pending_items=('inserter',)
        self.assertEqual(ex._continue_background_construction({'tick':100},'chemical_science')['type'],'build_many')
        ex.construction_mall.step.assert_called_once_with({'tick':100},{'product':'inserter'})
        ex.radar_coverage.upkeep.assert_not_called()

    def test_active_radar_wait_still_holds_construction_during_poll_cooldown(self):
        ex=self.executor();ex.radar_retry_tick=200
        ex.radar_diagnostic={'status':'waiting','reason':'waiting for native scan'}
        self.assertEqual(ex._continue_background_construction({'tick':100},'chemical_science'),ex.radar_diagnostic)
        ex.construction_mall.step.assert_not_called();ex.radar_coverage.upkeep.assert_not_called()

    def test_power_recovery_is_not_deadlocked_behind_a_radar_wait(self):
        ex=self.executor()
        self.assertIsNone(ex._continue_background_construction({'tick':100},'power'))
        ex.radar_coverage.upkeep.assert_not_called()

    def test_watch_builds_radar_before_starting_an_unrelated_mall_cell(self):
        ex=self.executor()
        with patch('factorio_agent.upgrades.next_upgrade',return_value=None):
            result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['reason'],'native radar coverage')
        ex.construction_mall.step.assert_not_called()

    def test_watch_keeps_defenses_while_waiting_without_changing_radar_foreign_board(self):
        ex=self.executor();ex.radar_coverage.upkeep.return_value={'status':'waiting','reason':'normal radar material wait'}
        with patch('factorio_agent.upgrades.next_upgrade',return_value=None):
            result=ex.step('defense_watch',{'tick':100})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['evidence']['radar_coverage']['reason'],'normal radar material wait')
        ex.perimeter.step.assert_called_once();ex.construction_mall.step.assert_not_called()

    def test_radar_contract_does_not_accept_coordinates_or_visibility_overrides(self):
        Request('radars','world',100,'radar_coverage').validate()
        with self.assertRaises(ValueError):
            Request('radars','world',100,'radar_coverage',{'force_visible':True}).validate()

    def test_radar_replan_arms_low_full_magazine_posts_before_second_guard_planning(self):
        ex=self.executor();ex.radar_coverage.state={'phase':'verified'}
        ex.radar_coverage.step=Mock()
        ex.perimeter.upkeep=Mock(return_value={'type':'insert','item':'firearm-magazine','count':6})
        self.assertEqual(ex.step('radar_coverage',{'tick':100})['count'],6)
        ex.radar_coverage.step.assert_not_called()
