import unittest

from factorio_ai.models import (
    ActionValidationError,
    inventory_count,
    total_item_count,
    validate_action,
)


class ModelTests(unittest.TestCase):
    def test_validate_action_accepts_build(self):
        action = {"type": "build", "name": "stone-furnace", "position": {"x": 1, "y": 2}}
        self.assertEqual(validate_action(action), action)

    def test_validate_action_rejects_unsupported_type(self):
        with self.assertRaises(ActionValidationError):
            validate_action({"type": "raw_lua", "code": "game.print('no')"})

    def test_total_item_count_includes_machine_inventory(self):
        observation = {
            "inventory": {"iron-plate": 3},
            "entities": [
                {
                    "name": "stone-furnace",
                    "inventories": {"2": {"iron-plate": 7}},
                }
            ],
        }
        self.assertEqual(inventory_count(observation, "iron-plate"), 3)
        self.assertEqual(total_item_count(observation, "iron-plate"), 10)


if __name__ == "__main__":
    unittest.main()
