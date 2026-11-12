from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.paired_mining import mining_pair
from factorio_agent.transport import _prospective_mining_source, plan_connection


class ProspectiveMiningSourceTests(unittest.TestCase):
    def setUp(self):
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.game = SimpleNamespace(query=Mock())
        self.core = mining_pair(0.5, 0.5, 1, "iron-ore")
        self.source = self.core["ports"][0]
        self.obs = {"ok": True, "world_id": "w", "entities": []}
        self.check = patch(
            "factorio_agent.planning.preflight", return_value={"ok": True, "clear_obstacles": []}
        ).start()
        self.addCleanup(patch.stopall)

    def prepare(self, entities=None, obs=None):
        return _prospective_mining_source(
            self.game,
            self.builder,
            obs or self.obs,
            self.source,
            self.core["entities"] if entities is None else entities,
        )

    def test_exact_generated_core_is_native_preflighted_without_ignored_assets(self):
        before = deepcopy(self.core)
        result = self.prepare()
        self.assertTrue(result["ok"])
        self.assertEqual(result["entities"], self.core["entities"])
        self.assertEqual(self.check.call_args.args[3], self.core["entities"])
        self.assertEqual(self.check.call_args.kwargs, {})
        self.assertEqual(self.core, before)

    def test_owned_source_path_requires_no_new_declaration_or_extra_preflight(self):
        obs = {
            **self.obs,
            "entities": [
                dict(next(e for e in self.core["entities"] if e["position"] == self.source["position"]), unit_number=91)
            ],
        }
        self.assertEqual(
            self.prepare([{"name": "fake", "position": self.source["position"]}], obs), {"ok": True, "entities": []}
        )
        self.check.assert_not_called()

    def test_missing_original_source_remains_undeclared(self):
        result = self.prepare([])
        self.assertEqual(result, {"ok": True, "entities": []})
        self.check.assert_not_called()

    def test_missing_drills_wrong_footprint_or_unapproved_tags_fail_before_native_io(self):
        missing = [e for e in self.core["entities"] if e["name"] != "electric-mining-drill"]
        wrong = deepcopy(self.core["entities"])
        for e in wrong:
            if e["name"] == "electric-mining-drill":
                e["mining_footprint"] = [[-2, -2], [2, 7]]
        forbidden = deepcopy(self.core["entities"]) + [dict(self.core["entities"][0], name="assembling-machine-1")]
        for entities in (missing, wrong, forbidden):
            self.assertFalse(self.prepare(entities)["ok"])
        self.check.assert_not_called()
        self.game.query.assert_not_called()

    def test_native_core_obstruction_does_not_gain_a_foreign_asset_exemption(self):
        self.check.return_value = {"ok": False, "reason": "block_protected_entity_obstruction"}
        result = self.prepare()
        self.assertEqual(result["reason"], "prospective_mining_core_native_preflight_failed")
        self.assertEqual(self.check.call_args.kwargs, {})

    def test_invalid_world_or_nonoutput_port_cannot_declare_prospective_source(self):
        self.assertFalse(self.prepare(obs={**self.obs, "ok": False})["ok"])
        self.source["direction"] = "input"
        self.assertFalse(self.prepare()["ok"])
        self.check.assert_not_called()

    def test_native_selector_receives_only_preflighted_original_tagged_core(self):
        source = self.source
        self.game.query.return_value = {"ok": False, "reason": "no_safe_bounded_mining_exit"}
        destination = {"item": "iron-ore", "position": {"x": 12.5, "y": 10.5}, "facing": 4}
        result = plan_connection(
            self.game, self.builder, self.obs, source, destination, self.core["entities"], ignored_units=[900]
        )
        self.assertEqual(result["reason"], "no_safe_bounded_mining_exit")
        script = self.game.query.call_args.args[0]
        self.assertIn("select_mining_exit(args.source,args.zones,ignored,args.prospective_core", script)
        self.assertIn("prospective_mining_core_has_no_resource", script)
        self.assertNotIn("--", " ".join(script.splitlines()))
        self.assertEqual(self.check.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
