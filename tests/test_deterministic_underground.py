from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder, plan_observed
from factorio_ai.deterministic_game import DeterministicGame, run_config, BUILD_BATCH_NAMES
from factorio_ai.deterministic_navigation import CharacterNavigator
from factorio_ai.deterministic_underground import underground_fields
from tests import test_deterministic_builder as fixtures


def mouth(role="input", **changes):
    return {"name": "underground-belt", "position": {"x": .5, "y": .5}, "direction": 4,
            "belt_to_ground_type": role, **changes}


class UndergroundAdapterTests(unittest.TestCase):
    def setUp(self):
        fixtures.BuilderTests.setUp(self)
        self.obs["inventory"]["underground-belt"] = 4

    def test_invalid_metadata_fails_before_queries_material_collection_or_navigation(self):
        invalid = [mouth(role) for role in (None, "both", "", True)]
        invalid += [mouth(name="transport-belt"), mouth(direction=True), mouth(direction=2)]
        missing = mouth();missing.pop("belt_to_ground_type");invalid.append(missing)
        game = DeterministicGame(run_config(runtime=self.game.cfg.runtime_dir), backend="character")
        game.query = Mock()
        navigator = CharacterNavigator(game)
        for spec in invalid:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    underground_fields(spec)
                with self.assertRaises(ValueError):
                    game.act({"type": "build", **spec})
                with self.assertRaises(ValueError):
                    navigator.execute({"type": "build", **spec}, self.obs)
                self.assertFalse(FactoryBuilder.can_place(self.builder, [spec])["ok"])
                self.assertEqual(self.builder.ensure_plan(self.obs, {"ok": True, "entities": [spec]})["status"], "blocked")
                self.assertFalse(plan_observed({**self.obs, "entities": [spec]}, {"ok": True, "entities": [spec]}))
        game.query.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_new_pair_builds_one_paid_endpoint_and_preserves_role_on_both_backends(self):
        self.assertNotIn("underground-belt", BUILD_BATCH_NAMES)
        plan = {"ok": True, "entities": [mouth(), mouth("output", position={"x": 4.5, "y": .5})]}
        for backend in ("assisted", "character"):
            self.game.backend = backend
            action = self.builder.ensure_plan(self.obs, plan)
            self.assertEqual(action, {"type": "build", "item": "underground-belt", **mouth()})
            self.obs["entities"] = [{**mouth(), "unit_number": 4}]
            action = self.builder.ensure_plan(self.obs, plan)
            self.assertEqual(action["belt_to_ground_type"], "output")
            self.assertEqual(action["position"], {"x": 4.5, "y": .5})
            self.obs["entities"] = []

    def test_batch_rejects_metadata_on_later_child_before_any_execution(self):
        game = DeterministicGame(run_config(runtime=self.game.cfg.runtime_dir))
        game.query = Mock()
        ordinary = {"type": "build", "name": "transport-belt", "position": {"x": .5, "y": .5}, "direction": 4}
        for invalid in ({**ordinary, "belt_to_ground_type": "input"}, {"type": "build", **mouth()}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    game.act({"type": "build_many", "actions": [ordinary, invalid]})
        game.query.assert_not_called()

    def test_wrong_role_or_nearby_different_position_cannot_complete_saved_plan(self):
        spec = mouth()
        plan = {"ok": True, "entities": [spec]}
        for existing in (mouth("output"), mouth(position={"x": .6, "y": .5}), mouth(direction=8)):
            self.obs["entities"] = [existing]
            self.assertFalse(plan_observed(self.obs, plan))
            self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.obs["entities"] = [deepcopy(spec)]
        self.assertTrue(plan_observed(self.obs, plan))
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "succeeded")

    def test_actor_clearance_build_keeps_output_role(self):
        self.game.backend = "character"
        self.builder.can_place.return_value = {"ok": False}
        self.game.query.return_value = {"ok": True, "only_actor": True}
        action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [mouth("output")]})
        self.assertEqual(action["type"], "build")
        self.assertEqual(action["belt_to_ground_type"], "output")
        self.assertIn("type=x.belt_to_ground_type", self.game.query.call_args.args[0])

    def test_valid_role_is_forwarded_to_placement_and_game_create(self):
        self.game.query.return_value = {"ok": True}
        FactoryBuilder.can_place(self.builder, [mouth("output")])
        self.assertIn("type=x.belt_to_ground_type", self.game.query.call_args.args[0])
        game = DeterministicGame(run_config(runtime=self.game.cfg.runtime_dir))
        game.query = Mock(return_value={"ok": True, "status": "succeeded", "unit_number": 1})
        game.act({"type": "build", **mouth("output")})
        body = game.query.call_args.args[0]
        self.assertIn("existing.belt_to_ground_type~=x.belt_to_ground_type", body)
        self.assertEqual(body.count("type=x.belt_to_ground_type"), 2)


if __name__ == "__main__":
    unittest.main()
