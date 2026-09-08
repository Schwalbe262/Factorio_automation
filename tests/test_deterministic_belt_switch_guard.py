from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_belt_switch_guard import validate_belt_route_replacement
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_navigation import CharacterNavigator


def endpoint(unit, x, y, direction=0, role=None):
    result = {"name": "underground-belt" if role else "transport-belt", "unit_number": unit,
              "position": {"x": x, "y": y}, "direction": direction}
    if role:
        result["belt_to_ground_type"] = role
    return result


def switch_action():
    pieces = [endpoint(21, 1.5, .5), endpoint(22, 1.5, -.5, role="input"),
              endpoint(23, 1.5, -4.5, role="output"), endpoint(24, 1.5, -5.5, 12)]
    return {"type": "mine", "name": "transport-belt", "position": {"x": .5, "y": .5}, "count": 1,
            "expected_entity_unit": 11, "expected_entity_world_id": "switch-world",
            "belt_route_replacement": {"item": "copper-plate", "entry_direction": 0,
                "expected_actor_unit_number": 58, "exit": endpoint(12, .5, -5.5), "entities": pieces,
                "pairs": [{"input": deepcopy(pieces[1]), "output": deepcopy(pieces[2]), "max_distance": 5}]}}


class BeltSwitchGuardTests(unittest.TestCase):
    def test_valid_bounded_guard_keeps_the_ordinary_mining_action(self):
        action = switch_action()
        validate_belt_route_replacement(action)
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)))
            game.query = Mock(return_value={"ok": True, "status": "succeeded"})
            game.act(action)
            body = game.query.call_args.args[0]
            self.assertIn("belt_switch_pair_changed", body)
            self.assertEqual(body.count("a.mine_entity(e)"), 1)

    def test_character_rejects_before_queries_navigation_or_motion_persistence(self):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)), backend="character")
            game.query = Mock()
            navigator = CharacterNavigator(game)
            for attempt in (lambda: game.act(switch_action()), lambda: navigator.execute(switch_action(), {}),
                            lambda: navigator._input(switch_action())):
                with self.assertRaisesRegex(ValueError, "assisted backend"):
                    attempt()
            game.query.assert_not_called()

    def test_malformed_identity_or_geometry_fails_before_any_query(self):
        invalid = []
        for key, value in (("expected_entity_unit", True), ("expected_entity_world_id", ""),
                           ("count", 2), ("name", "stone-furnace"), ("type", "build"),
                           ("type", "repair"), ("type", "finish_repair")):
            action = switch_action(); action[key] = value; invalid.append(action)
        for key, value in (("item", ""), ("expected_actor_unit_number", True), ("entry_direction", 1),
                           ("entities", []), ("pairs", [])):
            action = switch_action(); action["belt_route_replacement"][key] = value; invalid.append(action)
        action = switch_action(); action["position"]["x"] = float("nan"); invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["unexpected"] = True; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["entities"] *= 9; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["entities"][0]["unit_number"] = 11; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["entities"][0]["position"] = action["position"]; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["entities"][0]["belt_to_ground_type"] = "input"; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["pairs"][0]["output"]["unit_number"] = 25; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["pairs"] *= 2; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["pairs"][0]["max_distance"] = 3; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["pairs"][0]["max_distance"] = True; invalid.append(action)
        action = switch_action(); action["belt_route_replacement"]["pairs"][0]["input"]["belt_to_ground_type"] = "output"; invalid.append(action)
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)))
            game.query = Mock()
            for action in invalid:
                with self.subTest(action=action):
                    with self.assertRaises(ValueError):
                        game.act(action)
            game.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
