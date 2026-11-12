from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import tests.test_v2_defense_perimeter as existing
import tests.test_v2_defense_coverage as coverage


class DefenseAmmoSourceTests(unittest.TestCase):
    def test_precommission_keeps_the_normal_bootstrap_fallback(self):
        c, obs, report = existing.DefensePerimeterTests().perimeter(ammo=4, held=0)
        c.ammo_source = SimpleNamespace(commissioned=False, ensure_full_magazines=Mock())
        self.assertEqual(c.upkeep(obs)["recipe"], "firearm-magazine")
        c.ammo_source.ensure_full_magazines.assert_not_called()

    def test_commissioned_source_delegates_refill_without_actor_handcraft(self):
        c, obs, report = existing.DefensePerimeterTests().perimeter(ammo=4, held=0)
        action = {"type": "take_full_magazines", "count": 6}
        c.ammo_source = SimpleNamespace(commissioned=True, ensure_full_magazines=Mock(return_value=action))
        self.assertEqual(c.upkeep(obs), action)
        c.ammo_source.ensure_full_magazines.assert_called_once_with(obs, 6)
        c.bootstrap.ensure_item.assert_not_called()

    def test_commissioned_empty_or_changed_factory_never_falls_back_to_handcraft(self):
        c, obs, report = existing.DefensePerimeterTests().perimeter(ammo=0, held=0)
        c.ammo_source = SimpleNamespace(commissioned=True, ensure_full_magazines=Mock())
        for result in (
            {"status": "waiting", "reason": "factory buffer empty"},
            {"status": "blocked", "reason": "known factory chest changed"},
        ):
            c.ammo_source.ensure_full_magazines.return_value = result
            self.assertEqual(c.upkeep(obs), result)
        c.bootstrap.ensure_item.assert_not_called()

    def test_new_post_stock_uses_commissioned_source_before_build(self):
        c, obs, report = existing.DefensePerimeterTests().perimeter(missing=True, held=2)
        action = {"type": "take_full_magazines", "count": 8}
        c.ammo_source = SimpleNamespace(commissioned=True, ensure_full_magazines=Mock(return_value=action))
        self.assertEqual(c.step(obs), action)
        c.ammo_source.ensure_full_magazines.assert_called_once_with(obs, 10)
        c.game.query.assert_not_called()
        c.bootstrap.ensure_item.assert_not_called()

    def test_fresh_actor_stock_does_not_open_an_unarmed_scope_gate(self):
        helper = coverage.DefenseCoverageTests()
        c, obs = helper.controller()
        name = helper.install(c, obs)
        c.live[name] = {"unit_number": 201, "ammo": 7, "full_magazines": 7, "ammo_rounds": 70}
        c.held = 0
        c.ammo_source = SimpleNamespace(commissioned=True, ensure_full_magazines=Mock(return_value=None))
        result = c.ensure_coverage(obs, "stone-1", helper.AREAS)
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(c.state["coverage_scopes"]["stone-1"]["commissioned"])
        c.bootstrap.ensure_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
