from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_game import validate_mine_guard
from factorio_ai.deterministic_machine_ports import _ports, cell_capacity, ensure_machine_ports
from factorio_ai.factory_templates import build_template
from tests import test_deterministic_factory as factory_tests
from tests.test_deterministic_factory import ready, port


class MachinePortTests(unittest.TestCase):
    def setUp(self):
        factory_tests.FactoryTests.setUp(self)
        self.recipe = {"name": "gear", "energy": .5, "ingredients": [{"name": "iron-plate", "amount": 2}],
                       "products": [{"name": "gear", "amount": 1}]}
        self.catalog.recipes["gear"] = self.recipe
        self.catalog.entities["assembling-machine-1"] = {"crafting_speed": .5}
        self.catalog.technologies["fast-inserter"] = {"name": "fast-inserter", "unlocks": ["fast-inserter"],
            "prerequisites": [], "ingredients": [{"name": "automation-science-pack", "amount": 1}]}
        self.plan = build_template("assembler_row", machine="assembling-machine-1", recipe="gear",
                                   inputs=["iron-plate"], output="gear")
        self.plan["key"] = "recipe:gear"
        self.factory.state["blocks"][self.plan["key"]] = self.plan
        self.obs["enabled_recipes"].update(gear=True, **{"fast-inserter": True})
        self.obs["inventory"]["fast-inserter"] = 1
        self.arm = _ports(self.plan, self.recipe, "gear")[0]["arm"]
        self.obs["entities"] = [{**deepcopy(self.arm), "unit_number": 71}]
        self.proof = {"ok": True, "world_id": "one", "rows": [{"name": "inserter", "unit_number": 71, "valid": True}]}
        survey_patch = patch("factorio_ai.deterministic_machine_ports._survey", side_effect=lambda *args: deepcopy(self.proof))
        self.survey = survey_patch.start()
        self.addCleanup(survey_patch.stop)

    def call(self, rate=37.5):
        return ensure_machine_ports(self.factory, self.obs, self.plan, self.recipe, "gear", rate_per_minute=rate)

    def test_gear_capacity_is_input_limited_and_saved_machine_speed_is_authoritative(self):
        self.assertEqual(cell_capacity(self.factory, self.obs, self.plan, self.recipe, "gear"), 20)
        self.assertEqual(cell_capacity(self.factory, self.obs, self.plan, self.recipe, "gear", prefer_fast=True), 50)
        self.recipe["energy"] = 5
        self.factory.graph.machines_for_recipe.return_value = [{"name": "assembling-machine-2", "crafting_speed": .75}]
        self.assertEqual(cell_capacity(self.factory, self.obs, self.plan, self.recipe, "gear"), 6)

    def test_two_yield_cable_is_output_limited_and_circuit_is_three_cable_input_limited(self):
        for item, input_item, input_amount, output_amount, basic, fast in [
                ("copper-cable", "copper-plate", 1, 2, 40, 100),
                ("electronic-circuit", "copper-cable", 3, 1, 40 / 3, 100 / 3)]:
            recipe = {"name": item, "energy": .5, "ingredients": [{"name": input_item, "amount": input_amount}],
                      "products": [{"name": item, "amount": output_amount}]}
            plan = build_template("assembler_row", machine="assembling-machine-1", recipe=item, inputs=[input_item], output=item)
            self.assertEqual(cell_capacity(self.factory, self.obs, plan, recipe, item), basic)
            self.assertEqual(cell_capacity(self.factory, self.obs, plan, recipe, item, prefer_fast=True), fast)

    def test_locked_rate_queues_research_without_changing_bootstrap_plan(self):
        self.obs["enabled_recipes"].pop("fast-inserter")
        self.assertEqual(self.call()["status"], "waiting")
        self.assertEqual(self.factory.state["capability_research"], ["fast-inserter"])
        self.assertNotIn("machine_port_upgrades", self.factory.state)
        self.assertIsNone(self.call(None))
        self.survey.assert_not_called()
        self.assertEqual(self.arm["name"], "inserter")

    def test_research_request_reaches_scheduler_after_automatic_red_and_lab_link(self):
        self.obs["enabled_recipes"].pop("fast-inserter")
        self.call()
        factory_tests.FactoryTests.seed_lab(self)
        self.obs["technologies"] = {"automation": True, "electric-mining-drill": True, "logistics": True}
        self.factory._ensure_startup_iron = Mock(return_value=ready())
        self.factory.ensure_product = Mock(side_effect=lambda obs, item: ready(ports=[port(item)]))
        self.factory.connect_input = Mock(return_value=ready())
        self.catalog.technology_order = lambda names, **kwargs: names
        result = self.factory.next_action(self.obs)
        self.assertEqual((result["type"], result["technology"]), ("research", "fast-inserter"))
        self.factory.connect_input.assert_called_once()
        self.factory.connect_input.return_value = {"type": "build", "name": "transport-belt"}
        self.assertEqual(self.factory.next_action(self.obs)["type"], "build")

    def test_replacement_is_crafted_before_guarded_mining_and_upgrade_is_persisted(self):
        self.obs["inventory"] = {}
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "fast-inserter", "count": 1}
        self.assertEqual(self.call()["recipe"], "fast-inserter")
        self.assertNotIn("machine_port_upgrades", self.factory.state)
        self.obs["inventory"]["fast-inserter"] = 1
        action = self.call()
        validate_mine_guard(action)
        self.assertEqual((action["type"], action["expected_entity_unit"], action["expected_entity_world_id"]), ("mine", 71, "one"))
        self.assertEqual(self.arm["name"], "inserter")
        resumed = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertEqual(next(iter(resumed.state["machine_port_upgrades"].values()))["old_unit_number"], 71)

    def test_mined_empty_lua_array_builds_fast_normally_before_changing_plan(self):
        self.call()
        self.proof["rows"] = {}
        self.builder.ensure_plan.return_value = {"type": "build", "name": "fast-inserter"}
        self.assertEqual(self.call(None)["name"], "fast-inserter")
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]["entities"][0]["name"], "fast-inserter")
        self.assertEqual(self.arm["name"], "inserter")
        self.proof["rows"] = [{"name": "fast-inserter", "unit_number": 99, "valid": True}]
        self.assertIsNone(self.call(None))
        self.assertEqual(self.arm["name"], "fast-inserter")
        record = next(iter(self.factory.state["machine_port_upgrades"].values()))
        self.assertEqual((record["state"], record["observed_unit_number"]), ("observed", 99))
        self.assertEqual(cell_capacity(self.factory, self.obs, self.plan, self.recipe, "gear"), 40)

    def test_resume_rejects_changed_old_unit_and_foreign_world(self):
        self.call()
        self.proof["rows"][0]["unit_number"] = 72
        self.assertEqual(self.call()["reason"], "owned machine port identity changed")
        self.proof["rows"][0]["unit_number"] = 71
        self.proof["world_id"] = "other"
        self.assertEqual(self.call()["status"], "blocked")

    def test_ownership_requires_observed_unit_matching_survey(self):
        self.obs["entities"][0]["unit_number"] = 72
        self.assertEqual(self.call()["reason"], "machine port upgrade requires the observed owned arm")
        self.assertNotIn("machine_port_upgrades", self.factory.state)

    def test_changed_geometry_or_multiple_occupants_blocks_mining(self):
        for rows in [[{"name": "inserter", "unit_number": 71, "valid": False}],
                     [{"name": "inserter", "unit_number": 71, "valid": True}] * 2, None]:
            self.proof["rows"] = rows
            self.assertEqual(self.call()["status"], "blocked")

    def test_shared_arm_is_never_mined(self):
        self.factory.state["blocks"]["other"] = {"entities": [deepcopy(self.arm)]}
        self.assertEqual(self.call()["reason"], "machine port arm is shared with another reserved plan")
        self.survey.assert_not_called()

    def test_rollback_invalidates_fast_proof_and_repeats_guarded_mining(self):
        self.call()
        self.proof["rows"] = [{"name": "fast-inserter", "unit_number": 99, "valid": True}]
        self.call(None)
        self.obs["tick"] = 10
        self.factory._sync(self.obs)
        self.proof["rows"] = [{"name": "inserter", "unit_number": 71, "valid": True}]
        self.assertEqual(self.call(None)["expected_entity_unit"], 71)
        record = next(iter(self.factory.state["machine_port_upgrades"].values()))
        self.assertNotIn("observed_unit_number", record)

    def test_rollback_before_research_restores_basic_plan_without_blocking_research(self):
        self.test_rollback_invalidates_fast_proof_and_repeats_guarded_mining()
        self.obs["enabled_recipes"].pop("fast-inserter")
        self.assertIsNone(self.call(None))
        self.assertEqual(self.arm["name"], "inserter")
        self.assertEqual(self.call()["status"], "waiting")

    def test_rate_above_fast_budget_is_not_claimed(self):
        self.assertEqual(self.call(60)["status"], "blocked")
        self.survey.assert_not_called()

    def test_plan_key_is_only_required_when_a_port_upgrade_is_needed(self):
        self.plan.pop("key")
        self.assertIsNone(self.call(None))
        self.assertIsNone(self.call(20))
        self.assertEqual(self.call()["reason"], "machine port upgrade requires a saved plan key")
        self.survey.assert_not_called()


if __name__ == "__main__":
    unittest.main()
