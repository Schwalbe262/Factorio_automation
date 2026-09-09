from copy import deepcopy
import math
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_builder as module
from factorio_ai.deterministic_builder import _find, _plan_lookup, plan_observed
from tests import test_deterministic_builder as fixtures


def belt(x=0, y=0, **extra):
    return {"name": "transport-belt", "position": {"x": x, "y": y}, "direction": 0, **extra}


class PlanLookupTests(unittest.TestCase):
    def test_first_object_wins_across_different_buckets(self):
        first, second = belt(1.1, 1.1, unit_number=1), belt(.9, .9, unit_number=2)
        obs = {"entities": [first, second, belt(.99, .99, unit_number=3)]}
        wanted = belt(.99, .99)
        self.assertIs(_find(obs, wanted), first)
        self.assertIs(_plan_lookup(obs)(wanted), first)

    def test_strict_position_threshold_negative_tiles_and_boundary_rounding(self):
        for base in (0, -.99, .99, -1, 1, -123.5):
            for delta in (0, .2, -.2, math.nextafter(.2, 0), math.nextafter(.2, 1),
                          math.nextafter(-.2, 0), math.nextafter(-.2, -1)):
                for axis in ("x", "y"):
                    with self.subTest(base=base, delta=delta, axis=axis):
                        row, wanted = belt(base, base), belt(base, base)
                        row["position"][axis] += delta
                        obs = {"entities": [row]}
                        self.assertIs(_plan_lookup(obs)(wanted), _find(obs, wanted))
        self.assertIsNone(_plan_lookup({"entities": [belt(.2)]})(belt()))
        self.assertIsNotNone(_plan_lookup({"entities": [belt(math.nextafter(.2, 0))]})(belt()))

    def test_malformed_unrelated_rows_and_exotic_coordinates_keep_old_behavior(self):
        wanted, good = belt(), belt(unit_number=7)
        for bad in ({"name": "stone-furnace"}, {"name": "stone-furnace", "position": None},
                    {"name": "stone-furnace", "position": {"x": float("nan"), "y": 0}},
                    {"name": "stone-furnace", "position": {"x": float("inf"), "y": 0}},
                    {"name": "stone-furnace", "position": {"x": "0", "y": 0}}):
            with self.subTest(bad=bad):
                obs = {"entities": [bad, good]}
                self.assertIs(_plan_lookup(obs)(wanted), _find(obs, wanted))
        for row in (belt(2**60 + 1), belt(float("nan")), belt(float("inf"))):
            obs = {"entities": [row]}
            self.assertIs(_plan_lookup(obs)(deepcopy(row)), _find(obs, deepcopy(row)))
        for obs, wanted in (({"entities": [{"name": "transport-belt"}]}, belt()),
                            ({"entities": [None]}, belt()), ({"entities": [belt()]}, {})):
            with self.subTest(obs=obs, wanted=wanted):
                try:
                    _find(obs, wanted)
                except Exception as error:
                    with self.assertRaises(type(error)):
                        _plan_lookup(obs)(wanted)
                else:
                    self.fail("fixture must preserve an existing lookup error")

    def test_plan_observed_rebuilds_after_same_list_row_position_name_and_order_changes(self):
        plan = {"ok": True, "entities": [belt()]}
        row = belt()
        obs = {"entities": [row]}
        self.assertTrue(plan_observed(obs, plan))
        row["position"]["x"] = 1
        self.assertFalse(plan_observed(obs, plan))
        row["position"] = {"x": 0, "y": 0}
        self.assertTrue(plan_observed(obs, plan))
        row["name"] = "pipe"
        self.assertFalse(plan_observed(obs, plan))
        obs["entities"].append(belt())
        self.assertTrue(plan_observed(obs, plan))
        obs["entities"].insert(0, belt(direction=4))
        self.assertFalse(plan_observed(obs, plan))
        obs["entities"].reverse()
        self.assertTrue(plan_observed(obs, plan))
        obs["entities"] = []
        self.assertFalse(plan_observed(obs, plan))

    def test_full_plan_recipe_facing_and_missing_entities_remain_required(self):
        machine = {"name": "assembling-machine-1", "position": {"x": -3.5, "y": 2.5},
                   "direction": 4, "recipe": "iron-gear-wheel"}
        plan = {"ok": True, "entities": [belt(), machine]}
        obs = {"entities": deepcopy(plan["entities"])}
        self.assertTrue(plan_observed(obs, plan))
        for change in ("missing", "facing", "recipe"):
            changed = deepcopy(obs)
            if change == "missing": changed["entities"].pop()
            elif change == "facing": changed["entities"][-1]["direction"] = 8
            else: changed["entities"][-1]["recipe"] = "copper-cable"
            self.assertFalse(plan_observed(changed, plan), change)


class BuilderLookupTests(unittest.TestCase):
    def setUp(self):
        fixtures.BuilderTests.setUp(self)

    def test_ensure_plan_reobserves_mutable_entities_and_counts_current_missing_material(self):
        plan = {"ok": True, "entities": [belt(.5, .5), belt(1.5, .5)]}
        self.obs["entities"] = deepcopy(plan["entities"])
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "succeeded")
        self.obs["entities"].pop()
        self.bootstrap.ensure_item.return_value = {"type": "craft", "name": "transport-belt", "count": 1}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["type"], "craft")
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "transport-belt", 1)
        self.obs["entities"].clear()
        self.builder.ensure_plan(self.obs, plan)
        self.bootstrap.ensure_item.assert_called_with(self.obs, "transport-belt", 2)
        self.obs["entities"] = deepcopy(plan["entities"])
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "succeeded")

    def test_external_placement_query_does_not_reuse_local_index_for_batch_checks(self):
        first, second = belt(.5, .5), belt(1.5, .5)
        self.obs["inventory"] = {"transport-belt": 2}
        self.builder.can_place.side_effect = lambda rows: self.obs["entities"].append(deepcopy(second)) or {"ok": True}
        with patch.object(module, "_find", wraps=_find) as original_find:
            result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [first, second]})
        self.assertEqual(result["type"], "build")
        self.assertEqual(result["position"], first["position"])
        self.assertTrue(any(call.args[1] == second for call in original_find.call_args_list))

    def test_same_observation_world_and_tick_rollback_still_run_builder_sync(self):
        plan = {"ok": True, "entities": [belt()]}
        self.obs["entities"] = deepcopy(plan["entities"])
        self.builder.ensure_plan(self.obs, plan)
        self.builder.state["seeds"] = {"stale": {"observed": True}}
        self.obs["tick"] -= 1
        self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(self.builder.state["seeds"], {})
        self.obs["world_id"] = "another-world"
        self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(self.builder.state["world_id"], "another-world")


if __name__ == "__main__":
    unittest.main()
