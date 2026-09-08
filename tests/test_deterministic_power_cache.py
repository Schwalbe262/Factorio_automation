from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_factory import DeterministicFactory


class PowerEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)),
                                    query=Mock(return_value={"ok": True, "connected": 1, "live": []}))
        self.catalog = SimpleNamespace(fingerprint="catalog-a", recipes={}, entities={}, technologies={})
        self.builder = Mock()
        self.factory = DeterministicFactory(self.game, None, self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 100, "entities": []}
        self.plan = {"ok": True, "entities": [
            {"name": "small-electric-pole", "position": {"x": .5, "y": .5}}]}

    def check(self, key="source", plan=None, obs=None):
        return self.factory.ensure_power_connection(obs or self.obs, key, plan or self.plan)

    def test_recursive_dependencies_share_only_the_same_poles(self):
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(self.check("consumer")["status"], "succeeded")
        self.game.query.assert_called_once()
        self.plan["entities"][0]["position"]["x"] = 5.5
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(self.game.query.call_count, 2)

    def powered_observation(self):
        return {**deepcopy(self.obs), "ok": True, "surface": "nauvis", "entities": [
            {"name": "steam-engine", "type": "generator", "electric_network_id": 7},
            {**deepcopy(self.plan["entities"][0]), "type": "electric-pole", "electric_network_id": 7}]}

    def test_observed_generator_membership_avoids_a_duplicate_power_query(self):
        self.assertEqual(self.check(obs=self.powered_observation())["status"], "succeeded")
        self.game.query.assert_not_called()

    def test_disconnected_fresh_snapshot_still_performs_live_power_inspection(self):
        self.check(obs=self.powered_observation())
        obs = self.powered_observation()
        obs["entities"][1]["electric_network_id"] = 8
        self.game.query.return_value = {"ok": True, "connected": 0, "live": []}
        self.assertEqual(self.check(obs=obs)["status"], "blocked")
        self.game.query.assert_called_once()

    def test_incomplete_or_ambiguous_snapshot_cannot_prove_connected_power(self):
        def variants():
            for value in (None, 0, True, "7"):
                obs = self.powered_observation()
                for row in obs["entities"]:
                    row["electric_network_id"] = value
                yield obs
            obs = self.powered_observation()
            obs["entities"].append(deepcopy(obs["entities"][1]))
            yield obs
            obs = self.powered_observation()
            obs["entities"][1]["type"] = "ghost"
            yield obs
            obs = self.powered_observation()
            obs["entities"].append(None)
            yield obs
            for field in ("ok", "surface", "tick"):
                obs = self.powered_observation()
                obs.pop(field)
                yield obs
        self.game.query.return_value = {"ok": True, "connected": 0, "live": []}
        for obs in variants():
            with self.subTest(obs=obs):
                before = self.game.query.call_count
                self.assertEqual(self.check(obs=obs)["status"], "blocked")
                self.assertEqual(self.game.query.call_count, before + 1)

    def test_new_observation_rechecks_power_loss_even_at_the_same_tick(self):
        self.assertEqual(self.check()["status"], "succeeded")
        self.game.query.return_value = {"ok": True, "connected": 0, "live": []}
        result = self.check(obs=deepcopy(self.obs))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no generator-connected", result["reason"])
        self.assertEqual(self.game.query.call_count, 2)

    def test_reused_observation_invalidates_on_tick_rollback_world_and_catalog(self):
        self.check()
        self.obs["tick"] += 1
        self.check()
        self.obs["tick"] = 90
        self.check()
        self.obs["world_id"] = "two"
        self.check()
        self.factory._fingerprint = "catalog-b"
        self.check()
        self.factory.catalog = deepcopy(self.catalog)
        self.check()
        self.assertEqual(self.game.query.call_count, 6)

    def test_query_failure_is_not_cached(self):
        self.game.query.side_effect = [{"ok": False, "reason": "disconnected"},
                                       {"ok": True, "connected": 1, "live": []}]
        self.assertEqual(self.check()["status"], "blocked")
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(self.game.query.call_count, 2)

    def test_disconnected_evidence_and_pending_construction_are_rechecked(self):
        self.game.query.return_value = {"ok": True, "connected": 0, "live": [{"x": 9.5, "y": .5}]}
        self.factory._sync(self.obs)
        self.factory.state["power_links"]["source"] = self.plan
        self.builder.ensure_plan.side_effect = [
            {"type": "build", "name": "small-electric-pole"},
            {"status": "succeeded"}]
        self.assertEqual(self.check()["type"], "build")
        self.assertEqual(self.check()["status"], "waiting")
        self.assertEqual(self.game.query.call_count, 2)
        self.assertEqual(self.builder.ensure_plan.call_count, 2)

    def test_separate_crossing_poles_receive_independent_persisted_connections(self):
        second = {"name": "small-electric-pole", "position": {"x": 20.5, "y": .5}}
        plan = {"ok": True, "entities": [self.plan["entities"][0], second]}
        connected = set()
        self.factory._power_grid = Mock(side_effect=lambda obs, poles: {
            "ok": True, "connected": sum(p["position"]["x"] in connected for p in poles),
            "live": [{"x": -9.5, "y": .5}]})
        self.factory._power_route = Mock(side_effect=lambda source, destination: {
            "ok": True, "path": [source, destination]})
        self.builder.can_place.return_value = {"ok": True}
        self.builder.ensure_plan.return_value = {"type": "build", "name": "small-electric-pole"}
        self.assertEqual(self.check(plan=plan)["type"], "build")
        connected.add(.5)
        self.assertEqual(self.check(plan=plan)["type"], "build")
        self.assertEqual(set(self.factory.state["power_links"]), {"source:pole:0.5,0.5", "source:pole:20.5,0.5"})
        connected.add(20.5)
        self.assertEqual(self.check(plan=plan)["status"], "succeeded")
        self.assertEqual(self.builder.ensure_plan.call_count, 2)

    def test_existing_multi_pole_wire_path_is_maintained_before_new_connections(self):
        second = {"name": "small-electric-pole", "position": {"x": 20.5, "y": .5}}
        plan = {"ok": True, "entities": [self.plan["entities"][0], second]}
        self.factory._sync(self.obs)
        legacy = {"ok": True, "entities": [{"name": "small-electric-pole", "position": {"x": -4.5, "y": .5}}]}
        self.factory.state["power_links"]["source"] = legacy
        self.game.query.return_value = {"ok": True, "connected": 0, "live": [{"x": -9.5, "y": .5}]}
        self.builder.ensure_plan.return_value = {"type": "build", "name": "small-electric-pole"}
        self.assertEqual(self.check(plan=plan)["type"], "build")
        self.builder.ensure_plan.assert_called_once_with(self.obs, legacy)
        self.assertEqual(self.factory.state["power_links"], {"source": legacy})


if __name__ == "__main__":
    unittest.main()
