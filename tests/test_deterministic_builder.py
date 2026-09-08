import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.factory_templates import build_template


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    backend="assisted", query=Mock())
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(entities={})
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True, "blocked": []})
        self.obs = {"world_id": "test-world", "tick": 100, "entities": [], "inventory": {},
                    "enabled_recipes": {}, "technologies": {"steam-power": True}, "position": {"x": 0, "y": 0}}

    def test_missing_construction_item_delegates_to_resource_backed_bootstrap(self):
        plan = build_template("labs_row", inputs=["automation-science-pack"])
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "lab", "count": 1}
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["type"], "craft")
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "lab", 1)
        self.game.query.assert_not_called()

    def test_reconstruction_reuses_observed_entities_and_resumes_first_missing(self):
        plan = {"ok": True, "entities": [{"name": "pipe", "position": {"x": .5, "y": .5}},
                                           {"name": "pipe", "position": {"x": 1.5, "y": .5}}]}
        self.obs["entities"] = [plan["entities"][0]]
        self.obs["inventory"] = {"pipe": 1}
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["type"], "build")
        self.assertEqual(result["position"], {"x": 1.5, "y": .5})
        resumed = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        resumed.can_place = self.builder.can_place
        self.assertEqual(resumed.ensure_plan(self.obs, plan), result)

    def test_recipe_is_set_only_when_unlocked_and_existing_entity_was_observed(self):
        machine = {"name": "assembling-machine-1", "position": {"x": .5, "y": .5}, "recipe": "iron-gear-wheel"}
        plan = {"ok": True, "entities": [machine]}
        self.obs["entities"] = [{"name": machine["name"], "position": machine["position"]}]
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.obs["enabled_recipes"] = {"iron-gear-wheel": True}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["type"], "recipe")
        self.obs["entities"][0]["recipe"] = "iron-gear-wheel"
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_collision_and_wrong_existing_orientation_fail_before_action(self):
        belt = {"name": "transport-belt", "position": {"x": .5, "y": .5}, "direction": 4}
        plan = {"ok": True, "entities": [belt]}
        self.obs["inventory"] = {"transport-belt": 1}
        self.builder.can_place.return_value = {"ok": False, "blocked": [belt]}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.obs["entities"] = [{**belt, "direction": 0}]
        self.assertIn("direction", self.builder.ensure_plan(self.obs, plan)["reason"])

    def test_two_direction_generators_reuse_normalized_axis_but_reject_perpendicular_axis(self):
        for name in ("steam-engine", "steam-turbine"):
            for planned, actual in ((8, 0), (12, 4)):
                entity = {"name": name, "position": {"x": .5, "y": .5}, "direction": planned}
                self.obs["entities"] = [{**entity, "direction": actual}]
                result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
                self.assertEqual(result["status"], "succeeded")
                self.obs["entities"][0]["direction"] = (actual + 4) % 8
                result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
                self.assertEqual(result["status"], "blocked")

    def test_character_mode_moves_before_out_of_reach_construction(self):
        self.game.backend = "character"
        self.obs["inventory"] = {"pipe": 1}
        plan = {"ok": True, "entities": [{"name": "pipe", "position": {"x": 50.5, "y": .5}}]}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["type"], "move")

    def test_routing_rejects_foreign_belt_side_inputs_and_incidental_pipe_connections(self):
        self.game.query.return_value = {"ok": True, "blocked": []}
        foreign_belt = {"name": "transport-belt", "position": {"x": 1.5, "y": -.5}, "direction": 8}
        route = self.builder.route({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, "transport-belt", [foreign_belt])
        self.assertTrue(route["ok"], route["reason"])
        self.assertNotIn({"x": 1.5, "y": .5}, route["path"])
        foreign_pipe = {"name": "pipe", "position": {"x": 1.5, "y": -.5}}
        route = self.builder.route({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, "pipe", [foreign_pipe])
        self.assertTrue(route["ok"], route["reason"])
        self.assertNotIn({"x": 1.5, "y": .5}, route["path"])

    def test_new_world_invalidates_site_and_seed_checkpoint(self):
        self.builder._sync(self.obs)
        self.builder.state["power_plan"] = {"old": True}
        self.builder._save()
        self.obs["world_id"] = "different-world"
        self.builder._sync(self.obs)
        self.assertNotIn("power_plan", self.builder.state)
        saved = json.loads(self.builder.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["world_id"], "different-world")

    def test_coal_drill_output_belt_and_self_feed_inserter_touch_correct_tiles(self):
        plan = self.builder._coal_plan({"x": 10, "y": 20})
        self.assertEqual(plan["entities"][1]["position"], {"x": 11.5, "y": 19.5})
        inserter = next(e for e in plan["entities"] if e["name"] == "burner-inserter")
        self.assertEqual(inserter["position"], {"x": 8.5, "y": 19.5})
        self.assertEqual(inserter["direction"], 12)  # picks from west, drops east into drill
        self.assertTrue(any(e["position"] == {"x": 7.5, "y": 19.5} for e in plan["entities"]))

    def _ready_power(self):
        power = build_template("steam_bank")
        coal = self.builder._coal_plan({"x": 20, "y": 20})
        self.builder._sync(self.obs)
        self.builder.state.update(power_plan=power, coal_plan=coal)
        self.obs["entities"] = [{**e, "inventory": {"coal": 8}, "remaining_burning_fuel": 100}
                                 for e in power["entities"] + coal["entities"]]
        evidence = {"ok": True, "water": 100, "steam": 100, "boiler_fuel": 100,
                    "drill_fuel": 100, "coal_on_belts": 4, "connected_engines": 2,
                    "energized_engines": 2, "generation_kw": 0, "tick": 100}
        self.builder.power_evidence = Mock(return_value=evidence)
        return power, coal, evidence

    def test_power_requires_flow_evidence_over_time_and_accepts_no_load_grid(self):
        power, coal, evidence = self._ready_power()
        first = self.builder.ensure_power(self.obs)
        self.assertEqual(first["status"], "waiting")
        self.assertIn("30 game seconds", first["reason"])
        self.obs["tick"] = evidence["tick"] = 1900
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["evidence"]["flow_verified"])
        self.assertEqual(result["evidence"]["generation_kw"], 0)

    def test_broken_power_evidence_never_succeeds_or_repeatedly_handfeeds(self):
        power, coal, evidence = self._ready_power()
        self.builder.ensure_power(self.obs)
        for row in self.obs["entities"]:
            row["inventory"], row["remaining_burning_fuel"] = {}, 0
        evidence["boiler_fuel"] = 0
        self.obs["tick"] = evidence["tick"] = 5000
        self.obs["inventory"] = {"coal": 100}
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("type", result)
        self.assertNotIn("power_sample_tick", self.builder.state)

    def test_evidence_error_is_a_blocked_report_not_a_type_error(self):
        self._ready_power()
        self.builder.power_evidence.return_value = {"ok": False, "reason": "RCON failure"}
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["diagnostics"]["reason"], "RCON failure")

    def test_seed_uses_real_inventory_and_is_observed_before_marking_complete(self):
        entity = {"name": "boiler", "position": {"x": .5, "y": 1}}
        self.obs["entities"] = [entity]
        self.obs["inventory"] = {"coal": 8}
        self.builder._sync(self.obs)
        action = self.builder._seed(self.obs, "boiler", entity, 8)
        self.assertEqual(action["type"], "insert")
        self.assertFalse(self.builder.state["seeds"]["boiler"]["observed"])
        self.obs["entities"][0]["remaining_burning_fuel"] = 1000
        self.assertIsNone(self.builder._seed(self.obs, "boiler", entity, 8))
        self.assertTrue(self.builder.state["seeds"]["boiler"]["observed"])


if __name__ == "__main__":
    unittest.main()
