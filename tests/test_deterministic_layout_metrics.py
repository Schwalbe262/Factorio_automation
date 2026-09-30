import unittest

from factorio_ai.deterministic_layout_metrics import layout_metrics, checkpoint_metrics
from factorio_ai.world_catalog import WorldCatalog


def catalog():
    def recipe(name, ingredients, amount=1):
        return {"ingredients": [{"type": "item", "name": k, "amount": v} for k, v in ingredients.items()],
                "products": [{"type": "item", "name": name, "amount": amount}], "categories": ["crafting"], "energy": 1, "enabled": True}
    return WorldCatalog.from_dict({"raw_sources": [{"type": "item", "name": "iron-ore"}],
        "recipes": {"iron-plate": recipe("iron-plate", {"iron-ore": 1}),
                    "transport-belt": recipe("transport-belt", {"iron-plate": 2}, 2),
                    "underground-belt": recipe("underground-belt", {"iron-plate": 10, "transport-belt": 5}, 2)}})


def belt(x, name="transport-belt"):
    return {"name": name, "position": {"x": x + .5, "y": .5}, "direction": 4}


class LayoutMetricTests(unittest.TestCase):
    def test_underground_pair_costs_more_than_three_surface_tiles(self):
        c = catalog()
        surface = layout_metrics([belt(i) for i in range(3)], c)
        tunnel = layout_metrics([belt(0, "underground-belt"), belt(2, "underground-belt")], c)
        self.assertLess(tunnel["entities"], surface["entities"])
        self.assertGreater(tunnel["transport"]["raw_item_units"], surface["transport"]["raw_item_units"])

    def test_shared_owned_endpoints_are_priced_once(self):
        result = layout_metrics([belt(-5), belt(-5), belt(-4)], catalog())
        self.assertEqual(result["entities"], 2)
        self.assertEqual(result["construction"]["raw_item_units"], 2)
        self.assertEqual(result["area"], 2)

    def test_assembly_is_separate_from_long_resource_routes(self):
        state = {"links": {"source:iron": {"entities": [belt(i) for i in range(20)]},
                            "recipe:gear:iron": {"entities": [belt(30), belt(31)]}}}
        result = checkpoint_metrics(state, catalog())
        self.assertEqual(result["assembly_transport"]["entities"], 2)
        self.assertEqual(result["factory"]["entities"], 22)
        self.assertFalse(result["sustained_flow_proved"])

    def test_unknown_recipe_is_not_silently_priced_as_one(self):
        with self.assertRaises(ValueError):
            layout_metrics([belt(0, "unpriced-transport-belt")], catalog())
