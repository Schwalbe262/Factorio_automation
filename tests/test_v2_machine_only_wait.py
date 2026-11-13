"""A machine-only ingredient a built machine makes is a wait, not a dead end.

eval-1575138435 at 320 min: the engine row (planned_products, phase build) needed a
steel chest (8 steel plates); the commissioned steel furnaces had produced nothing out
yet, and ``ensure_item`` blocked the job "required item needs a production machine".
"""

import unittest

from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap


def recipe(name, ingredients, products, handcraftable=True):
    return {
        "ok": True,
        "name": name,
        "enabled": True,
        "handcraftable": handcraftable,
        "ingredients": [{"type": "item", "name": n, "amount": a} for n, a in ingredients],
        "products": [{"type": "item", "name": n, "amount": a} for n, a in products],
    }


class NoQueryGame:
    def query(self, body):
        raise AssertionError("unexpected live query")


def observation(furnace_status="working"):
    furnace = {
        "name": "stone-furnace",
        "type": "furnace",
        "unit_number": 4493,
        "position": {"x": 70, "y": -130},
        "status_name": furnace_status,
        "recipe": "steel-plate",
        "inventory": {"coal": 5, "iron-plate": 9},
        "output_inventory": {},
    }
    return {
        "world_id": "w",
        "tick": 1148821,
        "inventory": {},
        "position": {"x": 80.5, "y": -751.5},
        "crafting_queue": [],
        "enabled_recipes": {"steel-chest": True, "steel-plate": True},
        "entities": [furnace, dict(furnace, unit_number=4492, position={"x": 75, "y": -130})],
    }


class MachineOnlyWaitTests(unittest.TestCase):
    def bootstrap(self):
        b = DeterministicBootstrap(NoQueryGame())
        b._recipes = {
            "steel-chest": recipe("steel-chest", [("steel-plate", 8)], [("steel-chest", 1)]),
            "steel-plate": recipe("steel-plate", [("iron-plate", 5)], [("steel-plate", 1)], handcraftable=False),
        }
        return b

    def test_steel_chest_waits_for_the_working_steel_furnaces(self):
        result = self.bootstrap().ensure_item(observation(), "steel-chest", 1)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "waiting for automated steel-plate output")
        ev = result["evidence"]
        self.assertEqual((ev["item"], ev["producers"], ev["need"]), ("steel-plate", 2, 8))

    def test_an_idle_furnace_holding_claimed_steel_is_a_producer(self):
        # 330 min: furnaces no_ingredients (no recipe observed), 85 steel inside, the slack view
        # (critical_path.slack_view) hid the claimed output; the job blocked again.
        from factorio_agent.critical_path import slack_view

        obs = observation("no_ingredients")
        for e in obs["entities"]:
            e.pop("recipe")
            e["inventory"] = {"coal": 5, "steel-plate": 85}
            e["output_inventory"] = {"steel-plate": 85}
        view = slack_view(obs, {"steel-plate": {"units": 8}})
        result = self.bootstrap().ensure_item(view, "steel-chest", 1)
        self.assertEqual((result["status"], result["evidence"]["producers"]), ("waiting", 2))
        # An assembler that only holds the item as an ingredient is no producer.
        obs["entities"] = [dict(obs["entities"][0], type="assembling-machine", recipe="engine-unit")]
        self.assertEqual(
            self.bootstrap().ensure_item(slack_view(obs, {"steel-plate": {}}), "steel-chest", 1)["status"], "blocked"
        )

    def test_no_producer_on_the_map_still_blocks(self):
        obs = observation()
        obs["entities"] = []
        result = self.bootstrap().ensure_item(obs, "steel-chest", 1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "required item needs a production machine")

    def test_unpowered_producers_do_not_count(self):
        result = self.bootstrap().ensure_item(observation("no_power"), "steel-chest", 1)
        self.assertEqual(result["status"], "blocked")

    def test_steel_on_a_belt_is_collected(self):
        obs = observation()
        obs["entities"].append(
            {"name": "transport-belt", "position": {"x": 72.5, "y": -140.5}, "belt_inventory": {"steel-plate": 6}}
        )
        result = self.bootstrap().ensure_item(obs, "steel-chest", 1)
        self.assertEqual((result["type"], result["item"], result["count"]), ("take", "steel-plate", 6))


if __name__ == "__main__":
    unittest.main()
