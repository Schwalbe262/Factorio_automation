"""Starter science packs are hand-crafted ahead of the lab, in small batches."""

import unittest
from types import SimpleNamespace

from factorio_agent.precraft import INTERMEDIATE_BATCH, PACK, PRECRAFT_BATCH, research_deferred, starter_science


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


def splitter_catalog():
    def row(name, ingredients, made=1):
        return {
            "name": name,
            "ingredients": [{"type": "item", "name": n, "amount": a} for n, a in ingredients.items()],
            "products": [{"type": "item", "name": name, "amount": made}],
        }

    recipes = {
        "splitter": row("splitter", {"electronic-circuit": 5, "iron-plate": 5, "transport-belt": 4}),
        "electronic-circuit": row("electronic-circuit", {"iron-plate": 1, "copper-cable": 3}),
        "copper-cable": row("copper-cable", {"copper-plate": 1}, made=2),
        "transport-belt": row("transport-belt", {"iron-plate": 1}, made=2),
    }
    return SimpleNamespace(recipes=recipes, recipe_for_product=lambda item: recipes.get(item))


class ResearchDeferredPrecraftTests(unittest.TestCase):
    def obs(self, **inventory):
        return {
            "enabled_recipes": {"electronic-circuit": True, "copper-cable": True, "transport-belt": True},
            "crafting_queue": [],
            "inventory": inventory,
        }

    def test_circuits_for_locked_splitters_in_small_batches_from_carried_plates(self):
        obs = self.obs(**{"iron-plate": 50, "copper-plate": 50})
        action = research_deferred(splitter_catalog(), obs, {"splitter": 5}, stocked={"transport-belt"})
        self.assertEqual((action["recipe"], action["count"]), ("electronic-circuit", INTERMEDIATE_BATCH))

    def test_carried_circuits_count_and_copper_rounds_up(self):
        catalog = splitter_catalog()
        obs = self.obs(**{"iron-plate": 50, "copper-plate": 7, "electronic-circuit": 22})
        # 25 needed, 22 carried: 3 circuits = 9 cables = 4.5 copper crafts -> 5 copper; 7 is enough.
        action = research_deferred(catalog, obs, {"splitter": 5}, stocked={"transport-belt"})
        self.assertEqual((action["recipe"], action["count"]), ("electronic-circuit", 3))
        obs["inventory"]["copper-plate"] = 4
        self.assertIsNone(research_deferred(catalog, obs, {"splitter": 5}, stocked={"transport-belt"}))

    def test_nothing_while_the_queue_runs_or_the_plates_are_contested(self):
        catalog = splitter_catalog()
        busy = dict(self.obs(**{"iron-plate": 50, "copper-plate": 50}), crafting_queue=[{"recipe": "pipe"}])
        self.assertIsNone(research_deferred(catalog, busy, {"splitter": 5}))
        obs = self.obs(**{"iron-plate": 50, "copper-plate": 50})
        self.assertIsNone(research_deferred(catalog, obs, {"splitter": 5}, contested={"iron-plate"}))
        self.assertIsNone(research_deferred(catalog, obs, {}))


if __name__ == "__main__":
    unittest.main()
