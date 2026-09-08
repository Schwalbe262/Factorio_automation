"""Supervisor ordering around an already persisted copper-route cutover."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_supervisor import DeterministicSupervisor
from tests import test_deterministic_supervisor as fixtures


class InputBypassHookTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = []
        self.game = fixtures.fake_game(self.root)
        self.obs = fixtures.observation(100, actor_unit_number=58)
        self.supervisor = DeterministicSupervisor(self.game)
        self.supervisor.catalog = fixtures.fake_catalog()
        self.bootstrap_result = {"status": "waiting", "reason": "ordinary bootstrap work"}
        self.supervisor.bootstrap = SimpleNamespace(next_action=Mock(
            side_effect=lambda obs: self.events.append("bootstrap") or self.bootstrap_result))
        self.builder = SimpleNamespace(state={}, _sync=Mock(side_effect=lambda obs: self.events.append("builder_sync")),
            owns_automated_burner=Mock(return_value=False))
        self.supervisor.builder = self.builder
        record = {"phase": "switching", "link_key": "copper", "world_id": "fixture",
                  "catalog_fingerprint": "catalog-fixture", "actor_unit_number": 58, "last_tick": 100}
        self.factory = SimpleNamespace(state={"world_id": "fixture", "catalog_fingerprint": "catalog-fixture",
            "input_bypasses": {"copper": record}}, _fingerprint="catalog-fixture", game=self.game,
            _sync=Mock(side_effect=lambda obs: self.events.append("factory_sync")),
            owns_automated_burner=Mock(return_value=False), _save=Mock())
        self.supervisor.factory = self.factory
        self.supervisor.prepare_production = Mock(side_effect=lambda: self.events.append("restore"))

    def choose(self):
        with patch("factorio_ai.deterministic_repair_control.pending_repair", return_value=None):
            return self.supervisor.next_action(self.obs, "rocket")

    def assert_normal_planners_untouched(self):
        self.builder._sync.assert_not_called()
        self.factory._sync.assert_not_called()
        self.supervisor.bootstrap.next_action.assert_not_called()
        self.game.query.assert_not_called()
        self.game.act.assert_not_called()

    def test_critical_action_block_and_publication_wait_all_end_the_decision(self):
        choices = [{"type": "build", "name": "transport-belt", "direction": 4, "position": {"x": 12.5, "y": 22.5}},
                   {"status": "blocked", "reason": "replacement identity changed"},
                   {"status": "waiting", "reason": "canonical route published; reobserve"}]
        for choice in choices:
            with self.subTest(choice=choice), patch("factorio_ai.deterministic_input_bypass.resume_input_bypass",
                    side_effect=lambda factory, obs, **kw: self.events.append("resume") or choice) as resume:
                self.assertIs(self.choose(), choice)
                resume.assert_called_once_with(self.factory, self.obs, critical_only=True)
                self.assertEqual(self.supervisor.stage, "production")
                self.assert_normal_planners_untouched()
        self.assertEqual(self.events, ["restore", "resume"] * 3)

    def test_cold_restart_restores_saved_transaction_before_any_sync_without_power_flags(self):
        saved = json.dumps(self.factory.state).encode()
        path = self.root / "factory-production.json"
        path.write_bytes(saved)
        self.supervisor.factory = None
        self.supervisor.builder = None
        def restore():
            self.events.append("restore")
            self.supervisor.factory = self.factory
        self.supervisor.prepare_production.side_effect = restore
        choice = {"type": "build", "name": "transport-belt", "position": {"x": 12.5, "y": 22.5}, "direction": 4}
        with patch("factorio_ai.deterministic_builder.FactoryBuilder", side_effect=lambda *args:
                self.events.append("builder_create") or self.builder), patch(
                "factorio_ai.deterministic_input_bypass.resume_input_bypass", side_effect=lambda *args, **kw:
                self.events.append("resume") or choice):
            self.assertIs(self.choose(), choice)
        self.assertEqual(self.events, ["builder_create", "restore", "resume"])
        self.assertEqual(path.read_bytes(), saved)
        self.assert_normal_planners_untouched()

    def test_real_identity_rejection_preserves_record_before_destructive_factory_sync(self):
        for key, value in (("world_id", "other-world"), ("catalog_fingerprint", "other-catalog"),
                           ("actor_unit_number", 59)):
            original = deepcopy(self.factory.state)
            self.factory.state["input_bypasses"]["copper"][key] = value
            before = deepcopy(self.factory.state)
            result = self.choose()  # Uses the real transaction identity check.
            self.assertEqual(result["status"], "blocked", result)
            self.assertIn("identity changed", result["reason"])
            self.assertEqual(self.factory.state, before)
            self.factory._save.assert_not_called()
            self.assert_normal_planners_untouched()
            self.factory.state = original

    def test_pending_repair_cleanup_precedes_cold_restore_and_transaction(self):
        self.supervisor.builder = None
        cleanup = {"type": "finish_repair"}
        with patch("factorio_ai.deterministic_repair_control.pending_repair", return_value=cleanup), patch(
                "factorio_ai.deterministic_input_bypass.resume_input_bypass") as resume, patch(
                "factorio_ai.deterministic_builder.FactoryBuilder") as builder:
            self.assertIs(self.supervisor.next_action(self.obs, "rocket"), cleanup)
        self.supervisor.prepare_production.assert_not_called()
        resume.assert_not_called()
        builder.assert_not_called()
        self.assertEqual(self.supervisor.stage, "repair")
        self.assert_normal_planners_untouched()

    def test_no_transaction_preserves_ordinary_power_restore_sync_and_bootstrap_order(self):
        self.factory.state["input_bypasses"] = {}
        self.builder.state["power_verified_once"] = True
        with patch("factorio_ai.deterministic_input_bypass.resume_input_bypass") as resume:
            self.assertIs(self.choose(), self.bootstrap_result)
        resume.assert_not_called()
        self.assertEqual(self.events, ["builder_sync", "restore", "factory_sync", "bootstrap"])
        self.game.query.assert_not_called()
        self.game.act.assert_not_called()

    def test_noncritical_preparation_may_fall_through_without_retaining_a_stale_action(self):
        self.factory.state["input_bypasses"]["copper"]["phase"] = "preparing"
        with patch("factorio_ai.deterministic_input_bypass.resume_input_bypass", return_value=None) as resume:
            self.assertIs(self.choose(), self.bootstrap_result)
        resume.assert_called_once_with(self.factory, self.obs, critical_only=True)
        self.assertEqual(self.events, ["restore", "builder_sync", "bootstrap"])


if __name__ == "__main__":
    unittest.main()
