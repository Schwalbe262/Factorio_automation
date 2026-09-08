from copy import deepcopy
from unittest.mock import Mock, patch
import unittest

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_raw_ore import _inspect
import test_deterministic_factory as factory_tests


ready = factory_tests.ready


class RawOreTests(unittest.TestCase):
    setUp = factory_tests.FactoryTests.setUp

    def source(self, *, item="iron-ore", rate=None):
        self.obs["enabled_recipes"]["electric-mining-drill"] = True
        self.factory._merge_output = Mock(return_value=ready())
        self.factory._source_endpoint = Mock(side_effect=AssertionError("raw ore must never discover a furnace-fed source"))
        def reserve(obs, item, key):
            if key not in self.factory.state["blocks"]:
                offset = len(self.factory.state["blocks"]) * 12
                self.factory.state["blocks"][key] = self.factory._electric_source_plan(item, 20 + offset, 20)
            return self.factory.state["blocks"][key]
        self.factory._raw_capacity_site = Mock(side_effect=reserve)
        self.proof = {"ok": True, "world_id": "one", "tick": 100, "remaining": 1000, "complete": True,
                      "powered": True, "drill_unit": 123, "nominal_rate_per_minute": 30, "output_items": 0, "chest_items": 0}
        self.inspection = patch("factorio_ai.deterministic_raw_ore._inspect", side_effect=lambda factory, obs, item, plan: {
            **self.proof, "drill_unit": int(plan["entities"][0]["position"]["x"] * 10)}).start()
        self.addCleanup(patch.stopall)
        return self.factory.ensure_product(self.obs, item, rate_per_minute=rate)

    def again(self, item="iron-ore", rate=None):
        self.obs["tick"] += 1
        return self.factory.ensure_product(self.obs, item, rate_per_minute=rate)

    def test_iron_ore_has_a_dedicated_electric_chest_cell_without_recipe_or_hand_ore(self):
        result = self.source()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 30)
        self.assertFalse(result["evidence"]["flow_verified"])
        self.assertFalse(result["evidence"]["input_handcarry"])
        plan = self.factory.state["blocks"]["source:iron-ore"]
        names = [e["name"] for e in plan["entities"]]
        self.assertEqual(names.count("electric-mining-drill"), 1)
        self.assertIn("wooden-chest", names)
        self.assertNotIn("stone-furnace", names)
        self.assertTrue(all(p["direction"] == "output" for p in plan["ports"]))
        self.factory._source_endpoint.assert_not_called()
        self.bootstrap.discover_cell.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_copper_ore_uses_the_same_dedicated_source(self):
        result = self.source(item="copper-ore")
        self.assertEqual(result["evidence"]["ports"][0]["item"], "copper-ore")
        self.assertIn("source:copper-ore", self.factory.state["blocks"])

    def test_electric_drill_unlock_is_requested_normally_before_reservation(self):
        self.factory.request_recipe_unlock = Mock(return_value={"status": "waiting", "reason": "normal research"})
        self.factory._raw_capacity_site = Mock()
        self.assertEqual(self.factory.ensure_product(self.obs, "iron-ore")["reason"], "normal research")
        self.factory.request_recipe_unlock.assert_called_once_with(self.obs, "electric-mining-drill")
        self.factory._raw_capacity_site.assert_not_called()

    def test_capacity_counts_real_electric_rate_once_and_constructs_only_one_additional_cell(self):
        self.assertEqual(self.source(rate=60)["status"], "waiting")
        result = self.again(rate=60)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 60)
        self.assertEqual(len(result["evidence"]["cells"]), 2)
        self.assertEqual(len(self.factory.state["blocks"]), 2)
        self.factory._merge_output.assert_called_once()

    def test_builder_material_action_is_returned_without_claiming_ore_production(self):
        self.builder.ensure_plan.return_value = {"type": "craft", "recipe": "electric-mining-drill", "count": 1}
        self.assertEqual(self.source()["recipe"], "electric-mining-drill")
        self.factory.ensure_power_connection.assert_not_called()

    def test_unpowered_or_incomplete_cell_waits_without_reserving_additional_cells(self):
        self.source()
        for field in ("complete", "powered"):
            self.proof[field] = False
            self.assertEqual(self.again()["status"], "waiting")
            self.assertEqual(len(self.factory.state["blocks"]), 1)
            self.proof[field] = True

    def test_depletion_preserves_original_output_and_does_not_rebuild_exhausted_drill(self):
        original = self.source()["evidence"]["ports"][0]
        self.proof["remaining"] = 0
        self.assertEqual(self.again()["status"], "waiting")
        maintained = self.builder.ensure_plan.call_args.args[1]
        self.assertNotIn("electric-mining-drill", [e["name"] for e in maintained["entities"]])
        self.inspection.side_effect = lambda factory, obs, item, plan: {
            **self.proof, "remaining": 0 if plan is self.factory.state["blocks"]["source:iron-ore"] else 1000}
        result = self.again()
        self.assertEqual(result["evidence"]["ports"], [original])
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 30)
        self.assertEqual(self.factory._merge_output.call_args.args[2], original)

    def test_failed_replacement_merge_does_not_claim_restored_capacity(self):
        self.source(rate=60)
        self.factory._merge_output.return_value = {"status": "blocked", "reason": "no continuity route"}
        self.assertEqual(self.again(rate=60)["reason"], "no continuity route")

    def test_saved_reservation_is_revalidated_on_reload_without_new_cell(self):
        self.source()
        self.factory._save()
        resumed = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        resumed.ensure_power_connection = Mock(return_value=ready())
        resumed._merge_output = Mock(return_value=ready())
        self.proof["powered"] = False
        result = resumed.ensure_product(self.obs, "iron-ore")
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(list(resumed.state["blocks"]), ["source:iron-ore"])

    def test_rollback_rechecks_retired_deposit_and_does_not_double_count_recovered_cell(self):
        self.source()
        self.proof["remaining"] = 0
        self.again()
        self.proof["remaining"] = 1000
        self.obs["tick"] = 50
        result = self.factory.ensure_product(self.obs, "iron-ore")
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 60)
        self.assertFalse(self.factory.state["blocks"]["source:iron-ore"]["raw_ore_retired"])

    def test_failed_purity_identity_geometry_or_power_query_is_not_capacity(self):
        self.source()
        for reason in ("mixed ore", "wrong force", "changed facing", "wrong drop", "foreign output", "query failed"):
            self.inspection.side_effect = None
            self.inspection.return_value = {"ok": False, "reason": reason}
            self.assertEqual(self.again()["reason"], reason)
        self.inspection.return_value = {**self.proof, "world_id": "another"}
        self.assertEqual(self.again()["status"], "blocked")

    def test_incompatible_furnace_plan_is_rejected_without_live_query(self):
        plan = self.factory._electric_source_plan("iron-plate", 20, 20)
        self.game.query.reset_mock()
        self.assertFalse(_inspect(self.factory, self.obs, "iron-ore", plan)["ok"])
        self.game.query.assert_not_called()

    def test_bad_requested_rate_never_reserves_a_cell(self):
        for value in (-1, float("nan"), float("inf")):
            self.assertEqual(self.factory.ensure_product(self.obs, "iron-ore", rate_per_minute=value)["status"], "blocked")
        self.assertEqual(self.factory.state["blocks"], {})

    def test_duplicate_live_drill_cannot_back_two_capacity_cells(self):
        self.source(rate=60)
        self.inspection.side_effect = None
        self.inspection.return_value = self.proof
        self.assertIn("counted twice", self.again(rate=60)["reason"])

    def test_live_negative_mining_effects_reduce_capacity_and_positive_bonuses_are_ignored(self):
        plan = self.factory._electric_source_plan("iron-ore", 20, 20)
        for speed, productivity, expected in ((-.5, -.2, 12), (1, .5, 30), (-2, 0, 0)):
            self.game.query.return_value = {"ok": True, "complete": True, "nominal_rate_per_minute": 30,
                                           "speed_bonus": speed, "productivity_bonus": productivity}
            result = _inspect(self.factory, self.obs, "iron-ore", plan)
            self.assertEqual(result["nominal_rate_per_minute"], expected)

    def test_missing_or_nonfinite_live_mining_effects_cannot_claim_capacity(self):
        plan = self.factory._electric_source_plan("iron-ore", 20, 20)
        for effect in (None, float("nan"), float("inf")):
            self.game.query.return_value = {"ok": True, "complete": True, "nominal_rate_per_minute": 30,
                                           "speed_bonus": effect, "productivity_bonus": 0}
            self.assertFalse(_inspect(self.factory, self.obs, "iron-ore", plan)["ok"])


if __name__ == "__main__":
    unittest.main()
