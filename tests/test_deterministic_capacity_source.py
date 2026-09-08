from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_capacity_source import adopt_capacity_source
from factorio_ai.deterministic_factory import DeterministicFactory


def ready(**evidence):
    return {"status": "succeeded", "evidence": evidence}


class CapacitySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)))
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value=ready()))
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.bootstrap = Mock()
        self.factory = DeterministicFactory(game, self.bootstrap, self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 1, "enabled_recipes": {"electric-mining-drill": True}}
        self.factory._sync(self.obs)
        self.key = "source:iron-plate:capacity:1"
        self.plan = self.factory._electric_source_plan("iron-plate", 20, 20)
        self.factory.state["blocks"][self.key] = self.plan
        self.primary = {"ok": True, "source_receiver": {"name": "stone-furnace", "position": {"x": 0, "y": 0}},
                        "ports": [{"kind": "item", "direction": "output", "item": "iron-plate",
                                   "position": {"x": 3.5, "y": -.5}, "facing": 4}], "entities": []}
        self.factory.state["blocks"]["source:iron-plate"] = self.primary
        self.cell = {"ok": True, "complete": True, "electric": True, "operating": True, "remaining": 500,
                     "drill": deepcopy(self.plan["entities"][0]), "receiver": deepcopy(self.plan["entities"][1])}
        self.coal = {"kind": "item", "direction": "output", "item": "coal", "position": {"x": 0, "y": 4}, "facing": 4}
        self.factory.ensure_power_connection = Mock(return_value=ready())
        self.factory.ensure_product = Mock(return_value=ready(ports=[self.coal]))
        self.factory.connect_input = Mock(return_value=ready())
        self.factory._merge_output = Mock(return_value=ready())
        self.factory._reserve_relocated_source = Mock()
        self.bootstrap.discover_cell.return_value = self.cell

    def adopt(self):
        return adopt_capacity_source(self.factory, self.obs, "iron-plate", self.cell, self.primary)

    def test_discovered_capacity_source_reuses_its_paid_routes_and_preserves_canonical_bus(self):
        before = deepcopy(self.primary["ports"])
        result = self.factory._source_endpoint(self.obs, "iron-plate")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["evidence"]["reused_capacity_cell"])
        self.assertFalse(result["evidence"]["flow_verified"])
        self.factory._reserve_relocated_source.assert_not_called()
        self.assertEqual(set(self.factory.state["blocks"]), {"source:iron-plate", self.key})
        self.assertEqual(self.primary["ports"], before)
        self.factory.connect_input.assert_called_once_with(self.obs, self.coal, self.plan["ports"][1], self.key + ":fuel")
        self.factory._merge_output.assert_called_once_with(self.obs, self.plan["ports"][0], before[0], self.key + ":output")
        saved = DeterministicFactory(self.factory.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(saved.state["blocks"]["source:iron-plate"]["active_source"]["extraction_block"], self.key)
        self.assertIn("stone-furnace:20,18", saved.state["automated_burners"])

    def test_resume_rechecks_power_fuel_and_output_instead_of_trusting_old_success(self):
        self.assertEqual(self.adopt()["status"], "succeeded")
        failure = {"status": "blocked", "reason": "output arm is missing"}
        self.factory._merge_output.return_value = failure
        self.assertIs(self.adopt(), failure)
        self.assertEqual(self.factory._merge_output.call_count, 2)
        self.assertEqual(self.factory.connect_input.call_count, 2)

    def test_unowned_or_different_drill_receiver_pair_does_not_create_an_association(self):
        for field in ("drill", "receiver"):
            with self.subTest(field=field):
                original = deepcopy(self.cell[field])
                self.cell[field]["position"]["x"] += 1
                self.assertIsNone(self.adopt())
                self.cell[field] = original
        self.plan["resource_cell"] = False
        self.assertIsNone(self.adopt())
        self.builder.ensure_plan.assert_not_called()
        self.assertNotIn("active_source", self.primary)

    def test_ambiguous_owner_and_cross_world_adoption_are_rejected(self):
        self.factory.state["blocks"]["source:iron-plate:capacity:2"] = deepcopy(self.plan)
        self.assertEqual(self.adopt()["status"], "blocked")
        del self.factory.state["blocks"]["source:iron-plate:capacity:2"]
        self.obs["world_id"] = "other"
        self.assertEqual(self.adopt()["status"], "blocked")
        self.builder.ensure_plan.assert_not_called()

    def test_foreign_input_or_output_cannot_be_adopted(self):
        for index in (0, 1):
            with self.subTest(index=index):
                original = deepcopy(self.plan["ports"])
                self.plan["ports"][index]["item"] = "copper-plate"
                self.assertEqual(self.adopt()["status"], "blocked")
                self.plan["ports"] = original
        self.builder.ensure_plan.assert_not_called()

    def test_repair_actions_do_not_mark_an_unconnected_source_as_active(self):
        for owner, method in ((self.builder, "ensure_plan"), (self.factory, "ensure_power_connection"),
                              (self.factory, "connect_input"), (self.factory, "_merge_output")):
            with self.subTest(method=method):
                pending = {"type": "build", "name": "transport-belt"}
                mock = getattr(owner, method)
                mock.return_value = pending
                self.assertIs(self.adopt(), pending)
                self.assertNotIn("active_source", self.primary)
                mock.return_value = ready()

    def test_reused_cell_is_not_also_counted_as_a_separate_starter(self):
        self.catalog.entities = {"iron-ore": {"mining_time": 1},
            "electric-mining-drill": {"mining_speed": .5}, "burner-mining-drill": {"mining_speed": .25},
            "stone-furnace": {"crafting_speed": 1}}
        self.catalog.recipe_for_product = lambda _: {"energy": 3.2}
        self.factory._raw_capacity_site = Mock(side_effect=lambda obs, item, key:
            self.factory._electric_source_plan(item, int(key.rsplit(":", 1)[1]) * 12, 20))
        baseline = self.factory._expand_raw_source(self.obs, "iron-plate", 60, self.primary["ports"][0])
        self.assertEqual(baseline["evidence"]["additional_cells"], 3)
        self.assertEqual(self.adopt()["status"], "succeeded")
        result = self.factory._expand_raw_source(self.obs, "iron-plate", 60, self.primary["ports"][0])
        self.assertEqual(result["evidence"]["additional_cells"], 4)
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 75)


if __name__ == "__main__":
    unittest.main()
