import unittest

from factorio_ai import knowledge as k


class GameDataLoadTests(unittest.TestCase):
    def test_full_dataset_loaded(self):
        # Authoritative Space Age dump (tools/dump_game_data.py) — far bigger than curated.
        self.assertGreater(len(k.ALL_RECIPES), 500)
        self.assertGreater(len(k.ALL_TECHNOLOGIES), 100)

    def test_curated_recipes_preserved_for_planner(self):
        # The planner is tuned against the curated shapes; they must survive merge.
        for name, recipe in k.RECIPES.items():
            self.assertIn(name, k.ALL_RECIPES)
            self.assertEqual(k.ALL_RECIPES[name].ingredients, recipe.ingredients, name)

    def test_dependency_tree_is_closed(self):
        gaps = []
        for recipe in k.ALL_RECIPES.values():
            for ingredient in recipe.ingredients:
                if ingredient not in k.RAW_RESOURCES and k.recipe_for_product(ingredient) is None:
                    gaps.append((recipe.name, ingredient))
        self.assertEqual(gaps, [], f"unclosed ingredients: {gaps[:10]}")


class DisambiguationTests(unittest.TestCase):
    def test_canonical_recipe_prefers_standard_over_recycling(self):
        recipe = k.recipe_for_product("iron-plate")
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe.name, "iron-plate")
        self.assertFalse(recipe.name.endswith("-recycling"))

    def test_raw_resources_are_terminal(self):
        for raw in ["iron-ore", "water", "crude-oil", "scrap"]:
            self.assertIsNone(k.recipe_for_product(raw), raw)

    def test_multi_recipe_product_resolves(self):
        # petroleum-gas has no same-named recipe; must still resolve to an oil recipe.
        recipe = k.recipe_for_product("petroleum-gas")
        self.assertIsNotNone(recipe)
        self.assertIn("crude-oil", recipe.ingredients)


class RocketClosureTests(unittest.TestCase):
    def test_previously_dangling_items_now_required(self):
        required = k.required_items_for_objective("launch_rocket_program", max_depth=10)
        for item in ["concrete", "electric-engine-unit", "plastic-bar", "sulfuric-acid", "lubricant"]:
            self.assertIn(item, required, item)


class InfrastructureRootTests(unittest.TestCase):
    def test_production_buildings_surface_as_their_own_trees(self):
        trees = k.dependency_tree_for_objective("launch_rocket_program", max_depth=5)
        infra = {t["item"] for t in trees if t.get("infrastructure")}
        for building in ["assembling-machine-2", "electric-furnace", "boiler", "lab"]:
            self.assertIn(building, infra, building)

    def test_infrastructure_can_be_disabled(self):
        trees = k.dependency_tree_for_objective(
            "launch_rocket_program", max_depth=5, include_infrastructure=False
        )
        self.assertFalse(any(t.get("infrastructure") for t in trees))

    def test_roots_are_data_driven_and_cover_all_tiers(self):
        roots = set(k.INFRASTRUCTURE_ROOTS)
        # Every belt / underground / splitter / inserter / chest tier must be present
        # (the gap the hand list had), and recycling recipes must not leak in.
        for item in [
            "transport-belt", "fast-transport-belt", "express-transport-belt", "turbo-transport-belt",
            "underground-belt", "fast-underground-belt", "express-underground-belt", "turbo-underground-belt",
            "splitter", "fast-splitter", "express-splitter", "turbo-splitter",
            "burner-inserter", "stack-inserter", "iron-chest", "steel-chest",
        ]:
            self.assertIn(item, roots, item)
        self.assertFalse([r for r in roots if r.endswith("-recycling")])
        self.assertGreater(len(roots), 80)


class FlatDependencyMapTests(unittest.TestCase):
    def test_flat_map_is_one_hop_complete_and_non_redundant(self):
        m = k.flat_dependency_map()
        self.assertGreater(len(m), 200)
        # Direct ingredients only (names), not nested dicts -> one hop.
        self.assertIn("rocket-silo", m)
        self.assertIn("processing-unit", m["rocket-silo"])
        self.assertTrue(all(isinstance(x, str) for x in m["rocket-silo"]))
        # Each item appears once as its own key (so the LLM chains via lookups).
        self.assertIn("processing-unit", m)
        self.assertIn("turbo-transport-belt", m)  # completeness incl. tiers
        # Raw resources are omitted (no ingredients).
        self.assertNotIn("iron-ore", m)

    def test_scoped_map_only_includes_reachable(self):
        m = k.flat_dependency_map(roots=["electronic-circuit"])
        self.assertIn("electronic-circuit", m)
        self.assertIn("copper-cable", m)
        self.assertNotIn("rocket-silo", m)


class TechnologyMappingTests(unittest.TestCase):
    def test_recipe_technology_resolves_to_chain(self):
        recipe = k.ALL_RECIPES.get("advanced-circuit")
        self.assertIsNotNone(recipe)
        self.assertIsNotNone(recipe.technology)
        self.assertIn(recipe.technology, k.ALL_TECHNOLOGIES)
        chain = k.technology_chain_for_recipe("advanced-circuit")
        self.assertTrue(chain)


if __name__ == "__main__":
    unittest.main()
