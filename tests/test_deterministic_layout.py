from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_layout import RESOURCE_MARGIN, bounds, production_plan
from factorio_ai.factory_templates import build_template


def overlaps(a, b):
    return all(a[0][axis] < b[1][axis] and b[0][axis] < a[1][axis] for axis in (0, 1))


class ProductionLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hazards = []
        self.surveys = []
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock(side_effect=self.query))
        self.catalog = SimpleNamespace(fingerprint="catalog", entities={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 1, "position": {"x": 0, "y": 0}, "entities": []}
        self.origin = build_template("assembler_row", recipe="iron-gear-wheel", inputs=["iron-plate"], output="iron-gear-wheel")
        self.factory._sync(self.obs)

    def query(self, body):
        if "local areas=helpers.json_to_table(" not in body:
            return {"ok": True, "covered": 0}
        payload = body.split("local areas=helpers.json_to_table(", 1)[1]
        encoded, _ = json.JSONDecoder().raw_decode(payload)
        areas = json.loads(encoded)
        self.surveys.extend(areas)
        return {"ok": True, "clear": [i for i, area in enumerate(areas, 1)
                                      if not any(overlaps(area, hazard) for hazard in self.hazards)]}

    def assert_separated(self, plan):
        area = bounds(self.builder._occupied_by_plan(plan["entities"]), RESOURCE_MARGIN)
        self.assertFalse(any(overlaps(area, hazard) for hazard in self.hazards))

    def test_patch_and_depleted_mining_neighborhood_have_transport_space(self):
        self.hazards = [[[-12, -12], [12, 12]], [[-27, -2], [-24, 1]]]
        plan = self.factory.reserve_site(self.origin, "gear", self.obs)
        self.assertTrue(plan["ok"], plan)
        self.assert_separated(plan)
        anchor = self.factory.state["production_layout"]["anchor"]
        self.assertLessEqual(max(abs(anchor[axis]) for axis in ("x", "y")), 64)
        expansion = [[anchor["x"] - 12, anchor["y"] - 12], [anchor["x"] + 12, anchor["y"] + 12]]
        self.assertFalse(any(overlaps(expansion, hazard) for hazard in self.hazards))
        self.assertIn('type="mining-drill"', self.game.query.call_args.args[0])

    def test_new_block_avoids_resources_beside_machine_and_input_output_ports(self):
        self.factory.state["production_layout"] = {"anchor": {"x": 0, "y": 0}}
        self.hazards = [[[6, 0], [7, 1]]]
        plan = self.factory.reserve_site(self.origin, "gear", self.obs)
        self.assertTrue(plan["ok"], plan)
        self.assertNotEqual(plan["entities"][0]["position"], self.origin["entities"][0]["position"])
        self.assert_separated(plan)

    def test_anchor_survives_source_changes_reload_and_keeps_aisles(self):
        first = self.factory.reserve_site(self.origin, "gear", self.obs)
        anchor = deepcopy(self.factory.state["production_layout"])
        second = self.factory.reserve_site(self.origin, "circuits", self.obs, {"x": 1000, "y": 1000})
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.factory.state["production_layout"], anchor)
        self.assertTrue(all(not (first["production_area"][0][0] <= x < first["production_area"][1][0]
                                and first["production_area"][0][1] <= y < first["production_area"][1][1])
                            for x, y in self.builder._occupied_by_plan(second["entities"])))
        self.assertLessEqual(max(abs(second["entities"][0]["position"][axis] - anchor["anchor"][axis])
                                 for axis in ("x", "y")), 16)
        restored = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.game.query.reset_mock()
        self.assertEqual(restored.reserve_site(self.origin, "gear", self.obs, {"x": -999, "y": -999}), first)
        self.game.query.assert_not_called()
        self.assertEqual(restored.state["production_layout"], anchor)
        restored._sync({**self.obs, "world_id": "two"})
        self.assertNotIn("production_layout", restored.state)

    def test_existing_clean_assembly_area_is_preferred_over_ore_edge(self):
        for key, position in (("unsafe", {"x": -40.5, "y": .5}), ("clean", {"x": 16.5, "y": 32.5})):
            self.factory.state["blocks"][key] = build_template("assembler_row", recipe=key, inputs=["iron-plate"], output=key, anchor=position)
        before = deepcopy(self.factory.state["blocks"])
        self.hazards = [[[-50, -5], [-35, 5]]]
        plan = self.factory.reserve_site(self.origin, "new", self.obs, {"x": -40, "y": 0})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(self.factory.state["production_layout"]["anchor"], {"x": 16, "y": 32})
        self.assertEqual({key: self.factory.state["blocks"][key] for key in before}, before)

    def test_anchor_rejects_water_in_expansion_space_and_checks_actual_placement(self):
        self.hazards = [[[10, 10], [11, 11]]]
        self.builder.can_place.side_effect = [{"ok": False}, {"ok": True}]
        plan = self.factory.reserve_site(self.origin, "gear", self.obs)
        self.assertTrue(plan["ok"], plan)
        self.assertNotEqual(self.factory.state["production_layout"]["anchor"], {"x": 0, "y": 0})
        self.assertEqual(self.builder.can_place.call_count, 2)
        self.assertIn('s.count_tiles_filtered', self.game.query.call_args.args[0])

    def test_exhausted_or_failed_survey_never_creates_anchor_or_block(self):
        self.hazards = [[[-1000, -1000], [1000, 1000]]]
        result = self.factory.reserve_site(self.origin, "gear", self.obs)
        self.assertFalse(result["ok"])
        self.assertEqual(self.game.query.call_count, 13)
        self.assertNotIn("production_layout", self.factory.state)
        self.builder.can_place.assert_not_called()
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": False, "reason": "unavailable"}
        self.assertFalse(self.factory.reserve_site(self.origin, "gear", self.obs)["ok"])
        self.assertEqual(self.factory.state["blocks"], {})

    def test_empty_lua_array_advances_search_but_missing_evidence_fails_closed(self):
        self.game.query.side_effect = [{"ok": True, "clear": {}}, {"ok": True, "clear": [1]}]
        result = self.factory.reserve_site(self.origin, "gear", self.obs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.game.query.call_count, 2)
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True}
        failed = self.factory.reserve_site(self.origin, "next", self.obs)
        self.assertFalse(failed["ok"])
        self.assertNotIn("next", self.factory.state["blocks"])

    def test_all_downstream_types_use_policy_but_resource_cells_and_steam_do_not(self):
        for name in ("lab", "chemical-plant", "oil-refinery", "rocket-silo", "storage-tank", "iron-chest", "steel-furnace"):
            with self.subTest(name=name):
                self.assertTrue(production_plan(self.factory, {"entities": [{"name": name}]}))
        self.assertFalse(production_plan(self.factory, {"resource_cell": True, "entities": [{"name": "stone-furnace"}]}))
        self.assertFalse(production_plan(self.factory, build_template("steam_bank")))


if __name__ == "__main__":
    unittest.main()
