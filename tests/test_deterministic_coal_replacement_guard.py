from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_coal_replacement_guard import validate_coal_transit_replacement
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_navigation import CharacterNavigator


def replacement_action():
    def endpoint(name, unit, x, y, direction):
        return {"name": name, "unit_number": unit, "position": {"x": x, "y": y}, "direction": direction}
    return {"type": "mine", "name": "burner-inserter", "position": {"x": 34.5, "y": 16.5}, "count": 1,
            "expected_entity_unit": 3349, "expected_entity_world_id": "coal-world",
            "coal_transit_replacement": {"item": "coal", "replacement": "fast-inserter",
                "expected_actor_unit_number": 58, "expected_network_id": 1, "old_direction": 12,
                "pickup": endpoint("transport-belt", 1986, 33.5, 16.5, 8),
                "drop": endpoint("transport-belt", 3350, 35.5, 16.5, 4),
                "pole": endpoint("small-electric-pole", 3307, 33.5, 14.5, 0)}}


class CoalReplacementGuardTests(unittest.TestCase):
    def test_valid_guard_precedes_exactly_one_ordinary_mine(self):
        action = replacement_action()
        before = deepcopy(action)
        validate_coal_transit_replacement(action)
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)))
            game.query = Mock(return_value={"ok": True})
            game._record_action = Mock()
            game.act(action)
            body = game.query.call_args.args[0]
            self.assertEqual(body.count("a.mine_entity(e)"), 1)
            self.assertLess(body.index("coal_replacement_inventory_full"), body.index("a.mine_entity(e)"))
            self.assertNotIn("remaining_burning_fuel=", body)
            self.assertEqual(action, before)

    def test_character_refuses_before_queries_or_motion(self):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)), backend="character")
            game.query = Mock()
            navigator = CharacterNavigator(game)
            for run in (lambda: game.act(replacement_action()), lambda: navigator.execute(replacement_action(), {}),
                        lambda: navigator._input(replacement_action())):
                with self.assertRaisesRegex(ValueError, "assisted backend"):
                    run()
            game.query.assert_not_called()

    def test_bad_payload_identity_metadata_or_geometry_never_reaches_dispatch(self):
        invalid = []
        for key, value in (("type", "repair"), ("type", "finish_repair"), ("name", "inserter"),
                           ("count", True), ("count", 2), ("expected_entity_unit", True),
                           ("expected_entity_world_id", ""), ("quality", "rare")):
            action = replacement_action(); action[key] = value; invalid.append(action)
        for key, value in (("item", "wood"), ("replacement", "inserter"), ("expected_actor_unit_number", 3349),
                           ("expected_network_id", True), ("expected_network_id", 0), ("old_direction", 1), ("extra", True)):
            action = replacement_action(); action["coal_transit_replacement"][key] = value; invalid.append(action)
        for label in ("pickup", "drop", "pole"):
            for key, value in (("unit_number", 3349), ("unit_number", True), ("direction", 1),
                               ("position", {"x": float("nan"), "y": 0}), ("extra", 1)):
                action = replacement_action(); action["coal_transit_replacement"][label][key] = value; invalid.append(action)
        action = replacement_action(); action["coal_transit_replacement"]["pickup"]["position"]["x"] -= 1; invalid.append(action)
        action = replacement_action(); action["coal_transit_replacement"]["pole"]["name"] = "medium-electric-pole"; invalid.append(action)
        action = replacement_action(); action["coal_transit_replacement"].pop("expected_network_id"); invalid.append(action)
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)))
            game.query = Mock()
            for action in invalid:
                with self.subTest(action=action):
                    with self.assertRaises(ValueError):
                        game.act(action)
            game.query.assert_not_called()

    def test_unrelated_ordinary_actions_keep_existing_behavior(self):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)))
            game.query = Mock(return_value={"ok": True})
            game._record_action = Mock()
            game.act({"type": "mine", "name": "burner-inserter", "position": {"x": .5, "y": .5}, "count": 1})
            game.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
