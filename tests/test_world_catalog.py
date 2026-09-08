from copy import deepcopy
import re
import unittest

from factorio_ai.world_catalog import CatalogError, WorldCatalog


def material(name, amount=1, kind="item"):
    return {"type": kind, "name": name, "amount": amount}


def recipe(name, ingredients, products=None, **kw):
    return {"name": name, "enabled": True, "categories": ["crafting"], "energy": 1,
            "ingredients": ingredients, "products": products or [material(name)], **kw}


def technology(name, parents=(), unlocks=(), **kw):
    return {"name": name, "researched": False, "enabled": True,
            "prerequisites": list(parents), "unlocks": list(unlocks),
            "ingredients": [], "unit_count": 1, **kw}


def fixture():
    return {"version": "2.1.9", "active_mods": {"base": "2.1.9", "space-age": "2.1.9"},
            "recipes": {
                "iron-plate": recipe("iron-plate", [material("iron-ore")]),
                "gear": recipe("gear", [material("iron-plate", 2)]),
                "belt": recipe("belt", [material("gear"), material("iron-plate")], [material("belt", 2)]),
            }, "technologies": {}, "entities": {
                "ore": {"type": "resource", "mining_products": [material("iron-ore")]},
                "assembler": {"type": "assembling-machine", "collision_box": {
                    "left_top": {"x": -1.2, "y": -1.2}, "right_bottom": {"x": 1.2, "y": 1.2}},
                    "crafting_speed": 0.5, "crafting_categories": ["crafting"],
                    "fluidbox_prototypes": [{"index": 1, "production_type": "input", "volume": 1000,
                        "pipe_connections": [{"direction": 0, "positions": [
                            {"x": 0, "y": -1}, {"x": 1, "y": 0}, {"x": 0, "y": 1}, {"x": -1, "y": 0}]}]}]},
            }, "items": {}, "fluids": {}, "raw_sources": []}


class WorldCatalogTests(unittest.TestCase):
    def test_round_trip_preserves_live_geometry_and_copies_inputs(self):
        data = fixture()
        catalog = WorldCatalog.from_dict(data)
        data["recipes"]["gear"]["ingredients"][0]["amount"] = 999
        self.assertEqual(catalog.recipes["gear"]["ingredients"][0]["amount"], 2)
        recovered = WorldCatalog.from_dict(catalog.to_dict())
        self.assertEqual(catalog.fingerprint, recovered.fingerprint)
        self.assertEqual(recovered.entities["assembler"]["fluidbox_prototypes"][0]["pipe_connections"][0]["positions"][1], {"x": 1, "y": 0})

    def test_fingerprint_separates_force_availability_from_structure(self):
        catalog = WorldCatalog.from_dict(fixture())
        data = catalog.to_dict()
        data["recipes"]["gear"]["enabled"] = False
        changed = WorldCatalog.from_dict(data)
        self.assertEqual(catalog.fingerprint, changed.fingerprint)
        self.assertNotEqual(catalog.state_fingerprint, changed.state_fingerprint)
        data["recipes"]["gear"]["ingredients"][0]["amount"] = 3
        self.assertNotEqual(catalog.fingerprint, WorldCatalog.from_dict(data).fingerprint)

    def test_fingerprint_preserves_data_and_tracks_in_place_changes(self):
        data = fixture()
        data["technologies"]["gearing"] = technology("gearing", unlocks=["gear"])
        catalog = WorldCatalog.from_dict(data)
        original = catalog.to_dict()
        state = catalog.state_fingerprint
        fingerprint = catalog.fingerprint
        self.assertEqual(catalog.to_dict(), original)
        self.assertEqual(catalog.state_fingerprint, state)

        catalog.recipes["gear"]["enabled"] = False
        catalog.technologies["gearing"]["researched"] = True
        self.assertEqual(catalog.fingerprint, fingerprint)
        self.assertNotEqual(catalog.state_fingerprint, state)
        catalog.recipes["gear"]["ingredients"][0]["amount"] = 3
        self.assertNotEqual(catalog.fingerprint, fingerprint)
        geometry_fingerprint = catalog.fingerprint
        catalog.entities["assembler"]["collision_box"]["left_top"]["x"] = -1.5
        self.assertNotEqual(catalog.fingerprint, geometry_fingerprint)

    def test_shared_ingredients_are_aggregated_before_batch_rounding(self):
        catalog = WorldCatalog.from_dict(fixture())
        bom = catalog.bill_of_materials({"belt": 3, "gear": 1})
        self.assertEqual(bom["recipe_batches"], {"belt": 2, "gear": 3, "iron-plate": 8})
        self.assertEqual(bom["raw_materials"], [material("iron-ore", 8)])
        self.assertEqual(bom["surplus"], [material("belt")])

    def test_item_and_fluid_with_same_name_do_not_share_inventory_balance(self):
        data = fixture()
        data["raw_sources"] = [material("same", kind="fluid"), material("same")]
        data["recipes"]["mixed"] = recipe("mixed", [material("same", 2), material("same", 5, "fluid")])
        bom = WorldCatalog.from_dict(data).bill_of_materials({"mixed": 1})
        self.assertEqual(bom["raw_materials"], [material("same", 5, "fluid"), material("same", 2)])

    def test_research_order_uses_prerequisites_and_reports_trigger(self):
        data = fixture()
        data["technologies"] = {"early": technology("early", research_trigger={"type": "craft-item", "item": "iron-plate", "count": 50}),
                                "later": technology("later", ["early"], ["gear"])}
        data["recipes"]["gear"]["enabled"] = False
        catalog = WorldCatalog.from_dict(data)
        self.assertEqual(catalog.technology_order("later"), ["early", "later"])
        self.assertIsNone(catalog.recipe_for_product("gear", unlocked_only=True))
        self.assertEqual(catalog.dependency_closure({"gear": 1})["technologies"], ["early", "later"])
        self.assertEqual(catalog.technologies["early"]["research_trigger"]["count"], 50)

    def test_researched_technology_needs_no_science_again(self):
        data = fixture()
        data["technologies"] = {"early": technology("early", researched=True), "later": technology("later", ["early"])}
        catalog = WorldCatalog.from_dict(data)
        self.assertEqual(catalog.technology_order("later"), ["later"])
        self.assertEqual(catalog.technology_order("later", include_researched=True), ["early", "later"])

    def test_natural_resource_stays_terminal_despite_offworld_recipe(self):
        data = fixture()
        data["recipes"]["iron-ore"] = recipe("iron-ore", [material("bacteria")])
        self.assertIsNone(WorldCatalog.from_dict(data).recipe_for_product("iron-ore"))

    def test_live_surface_sources_exclude_offworld_deposits_and_wreckage_loot(self):
        data = fixture()
        data["raw_sources_authoritative"] = True
        data["raw_sources"] = [material("iron-ore")]
        data["entities"]["offworld-geyser"] = {"type": "resource", "mining_products": [material("acid", kind="fluid")]}
        data["entities"]["wreckage"] = {"type": "simple-entity", "mining_products": [material("gear")]}
        catalog = WorldCatalog.from_dict(data)
        self.assertEqual(catalog.bill_of_materials({"gear": 1})["raw_materials"], [material("iron-ore", 2)])
        with self.assertRaisesRegex(CatalogError, "no local recipe or extraction source"):
            catalog.bill_of_materials([material("acid", kind="fluid")])

    def test_recycling_and_inverse_packaging_are_not_production_sources(self):
        data = fixture()
        data["raw_sources"] = [material("crude-oil", kind="fluid")]
        data["recipes"].update({
            "unpack": recipe("unpack", [material("filled")], [material("gas", 50, "fluid")]),
            "pack": recipe("pack", [material("gas", 50, "fluid")], [material("filled")]),
            "oil": recipe("oil", [material("crude-oil", 100, "fluid")], [material("gas", 45, "fluid")]),
            "gear-recycling": recipe("gear-recycling", [material("belt")], [material("gear")], categories=["recycling"]),
        })
        catalog = WorldCatalog.from_dict(data)
        self.assertEqual(catalog.recipe_for_product("gas", product_type="fluid")["name"], "oil")
        self.assertEqual(catalog.recipe_for_product("gear")["name"], "gear")
        self.assertEqual(catalog.bill_of_materials([material("gas", 46, "fluid")])["raw_materials"], [material("crude-oil", 200, "fluid")])

    def test_surface_conditions_filter_offworld_crafting(self):
        data = fixture()
        data["surface_properties"] = {"pressure": 1000}
        data["recipes"]["offworld"] = recipe("offworld", [], [material("exotic")], surface_conditions=[{"property": "pressure", "min": 4000}])
        self.assertIsNone(WorldCatalog.from_dict(data).recipe_for_product("exotic"))

    def test_coproducts_are_credited_and_surplus_is_reported(self):
        data = fixture()
        data["raw_sources"] = [material("crude", kind="fluid")]
        data["recipes"]["refine"] = recipe("refine", [material("crude", 100, "fluid")], [material("light", 45, "fluid"), material("heavy", 25, "fluid")])
        bom = WorldCatalog.from_dict(data).bill_of_materials([material("light", 45, "fluid"), material("heavy", 20, "fluid")])
        self.assertEqual(bom["recipe_batches"], {"refine": 1})
        self.assertEqual(bom["surplus"], [material("heavy", 5, "fluid")])

    def test_missing_sources_and_probabilistic_yields_fail_explicitly(self):
        data = fixture()
        data["recipes"]["unknown"] = recipe("unknown", [material("missing")])
        catalog = WorldCatalog.from_dict(data)
        with self.assertRaisesRegex(CatalogError, "no local recipe or extraction source"):
            catalog.bill_of_materials({"unknown": 1})
        data["recipes"]["gear"]["products"][0]["independent_probability"] = 0.5
        with self.assertRaisesRegex(CatalogError, "stochastic yield"):
            WorldCatalog.from_dict(data).bill_of_materials({"gear": 1})

    def test_technology_cycle_and_invalid_amount_are_rejected(self):
        data = fixture()
        data["technologies"] = {"one": technology("one", ["two"]), "two": technology("two", ["one"])}
        catalog = WorldCatalog.from_dict(data)
        with self.assertRaisesRegex(CatalogError, "cycle"):
            catalog.technology_order("one")
        with self.assertRaisesRegex(CatalogError, "invalid material quantity"):
            catalog.bill_of_materials({"belt": float("nan")})

    def test_first_rocket_uses_live_part_count_recipes_and_research_cost(self):
        data = fixture()
        data["entities"]["rocket-silo"] = {"type": "rocket-silo", "rocket_parts_required": 7}
        for name, amount in (("rocket-silo", 2), ("rocket-part", 3), ("space-platform-starter-pack", 5)):
            data["recipes"][name] = recipe(name, [material("iron-plate", amount)], enabled=False)
        data["recipes"]["science"] = recipe("science", [material("iron-plate")])
        data["technologies"]["silo-tech"] = technology("silo-tech", unlocks=["rocket-silo", "rocket-part", "space-platform-starter-pack"],
                                                       ingredients=[material("science", 2)], unit_count=4)
        bom = WorldCatalog.from_dict(data).first_rocket_bom()
        self.assertEqual(bom["recipe_batches"]["rocket-part"], 7)
        self.assertEqual(bom["science_packs"], {"science": 8})
        self.assertEqual(bom["raw_materials"], [material("iron-ore", 36)])
        self.assertEqual(bom["technology_order"], ["silo-tech"])

    def test_empty_lua_arrays_and_chunked_exports_are_supported(self):
        data = fixture()
        data["entities"]["ore"]["crafting_categories"] = {}
        calls = []
        def query(body):
            calls.append(body)
            if "names={recipes=" in body:
                return {**{k: deepcopy(v) for k, v in data.items() if k not in ("recipes", "technologies", "entities", "items", "fluids")},
                        "names": {key: sorted(data[key]) or {} for key in ("recipes", "technologies", "entities", "items", "fluids")}}
            names = re.search(r'for _, name in ipairs\(\{(.*?)\}\)', body).group(1)
            selected = re.findall(r'"([^"]+)"', names)
            table = next(key for key, source in (("recipes", "force.recipes"), ("technologies", "force.technologies"),
                                                 ("entities", "prototypes.entity"), ("items", "prototypes.item"), ("fluids", "prototypes.fluid")) if f"export({source}[name])" in body)
            return {"entries": {name: deepcopy(data[table][name]) for name in selected}}
        catalog = WorldCatalog.from_game(query, chunk_size=2)
        self.assertEqual(set(catalog.recipes), set(data["recipes"]))
        self.assertEqual(catalog.entities["ore"]["crafting_categories"], [])
        self.assertEqual(len(calls), 4)

    def test_incomplete_live_export_is_rejected(self):
        def query(body):
            if "names={recipes=" in body:
                return {"names": {"recipes": ["lost"]}}
            return {"entries": {}}
        with self.assertRaisesRegex(CatalogError, "incomplete live export"):
            WorldCatalog.from_game(query)


if __name__ == "__main__":
    unittest.main()
