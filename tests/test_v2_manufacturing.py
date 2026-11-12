from types import SimpleNamespace
import unittest
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.manufacturing import green_core, raw_demand, lab_green_entry
from factorio_agent.science import lab_row
from factorio_agent.transport import plan_connection, xy
from factorio_agent.smelting import merge_entities


class GreenManufacturingTests(unittest.TestCase):
    def test_join_is_upstream_of_every_lab_pickup_and_tunnels_under_last_pole(self):
        labs = lab_row(17.5, -111.5, 8)
        entry = lab_green_entry(labs)
        pickup_x = [e["position"]["x"] for e in labs["entities"] if e["name"] == "inserter"]
        self.assertTrue(all(x < entry["join"]["x"] for x in pickup_x))
        self.assertEqual(entry["join"], {"x": 47.5, "y": -114.5})
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        boxes = [geometry._occupied_by_plan([e]) for e in labs["entities"] + entry["entities"]]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))

    def test_whole_production_core_has_no_overlapping_machine_belt_or_splitter_footprints(self):
        core = green_core(0.5, 0.5, 7)
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        boxes = [geometry._occupied_by_plan([e]) for e in core["entities"]]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        self.assertEqual(len(core["connections"]), 10)
        self.assertEqual(sum(e.get("recipe") == "logistic-science-pack" for e in core["entities"]), 7)

    def test_complete_internal_routes_fit_before_material_is_spent(self):
        core = green_core(0.5, 0.5, 7)
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        builder = SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan, can_place=lambda es: {"ok": True})
        game = SimpleNamespace(query=lambda _: {"ok": True, "blocked": [], "max_span": 5})
        es = core["entities"]
        area = core["reserved_area"]
        for a, b in core["connections"]:
            route = plan_connection(game, builder, {"world_id": "w", "entities": []}, a, b, es)
            self.assertTrue(route.get("ok"), (a, b, route))
            self.assertLessEqual(route["route_metrics"]["turns"], 6)
            es = merge_entities(es, route["entities"])
        for e in es:
            x, y = xy(e)
            self.assertTrue(area[0][0] <= x <= area[1][0] and area[0][1] <= y <= area[1][1], e)

    def test_catalog_math_includes_multi_output_belts_and_shared_gear_demand(self):
        recipes = {
            "green": {
                "energy": 6,
                "ingredients": [{"name": "belt", "amount": 1}, {"name": "inserter", "amount": 1}],
                "products": [{"name": "green", "amount": 1}],
            },
            "belt": {
                "energy": 0.5,
                "ingredients": [{"name": "iron-plate", "amount": 1}, {"name": "gear", "amount": 1}],
                "products": [{"name": "belt", "amount": 2}],
            },
            "inserter": {
                "energy": 0.5,
                "ingredients": [
                    {"name": "iron-plate", "amount": 1},
                    {"name": "gear", "amount": 1},
                    {"name": "circuit", "amount": 1},
                ],
                "products": [{"name": "inserter", "amount": 1}],
            },
            "gear": {
                "energy": 0.5,
                "ingredients": [{"name": "iron-plate", "amount": 2}],
                "products": [{"name": "gear", "amount": 1}],
            },
            "circuit": {
                "energy": 0.5,
                "ingredients": [{"name": "iron-plate", "amount": 1}, {"name": "wire", "amount": 3}],
                "products": [{"name": "circuit", "amount": 1}],
            },
            "wire": {
                "energy": 0.5,
                "ingredients": [{"name": "copper-plate", "amount": 1}],
                "products": [{"name": "wire", "amount": 2}],
            },
        }
        catalog = SimpleNamespace(
            entities={"assembling-machine-1": {"crafting_speed": 0.5}}, recipe_for_product=recipes.get
        )
        demand = raw_demand(catalog, "green", 35)
        self.assertEqual(demand["raw_items_per_minute"], {"iron-plate": 192.5, "copper-plate": 52.5})
        self.assertEqual(demand["machine_equivalents"]["gear"], 0.875)


if __name__ == "__main__":
    unittest.main()
