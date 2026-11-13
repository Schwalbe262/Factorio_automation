"""Starter science packs are hand-crafted ahead of the lab, in small batches."""

import unittest
from types import SimpleNamespace

from factorio_agent.precraft import PACK, PRECRAFT_BATCH, starter_science


def catalog():
    recipes = {
        PACK: {"ingredients": [{"name": "copper-plate", "amount": 1}, {"name": "iron-gear-wheel", "amount": 1}]},
        "iron-gear-wheel": {"ingredients": [{"name": "iron-plate", "amount": 2}]},
    }
    for name, recipe in recipes.items():
        recipe["products"] = [{"name": name, "amount": 1}]
    return SimpleNamespace(
        technologies={"automation": {"unit_count": 10}},
        recipes=recipes,
        recipe_for_product=lambda item: recipes.get(item),
    )


def observation(**changes):
    obs = {
        "enabled_recipes": {PACK: True},
        "technologies": {},
        "crafting_queue": [],
        "inventory": {"iron-plate": 20, "copper-plate": 10},
        "entities": [],
    }
    obs.update(changes)
    return obs


class StarterSciencePrecraftTests(unittest.TestCase):
    def test_small_batch_while_nothing_else_is_queued(self):
        action = starter_science(catalog(), observation())
        self.assertEqual((action["type"], action["recipe"], action["count"]), ("craft", PACK, PRECRAFT_BATCH))

    def test_only_up_to_the_technology_pack_count(self):
        held = observation(inventory={"iron-plate": 20, "copper-plate": 10, PACK: 9})
        self.assertEqual(starter_science(catalog(), held)["count"], 1)
        held["inventory"][PACK] = 10
        self.assertIsNone(starter_science(catalog(), held))

    def test_not_while_the_queue_is_busy_or_plates_are_missing(self):
        self.assertIsNone(starter_science(catalog(), observation(crafting_queue=[{"recipe": "pipe", "count": 5}])))
        self.assertIsNone(starter_science(catalog(), observation(inventory={"iron-plate": 3, "copper-plate": 10})))

    def test_not_once_a_lab_exists_or_the_technology_is_done(self):
        self.assertIsNone(starter_science(catalog(), observation(entities=[{"name": "lab"}])))
        self.assertIsNone(starter_science(catalog(), observation(technologies={"automation": True})))
        self.assertIsNone(starter_science(catalog(), observation(enabled_recipes={})))


if __name__ == "__main__":
    unittest.main()
