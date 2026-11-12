import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent.defense_perimeter import DefensePerimeter, protected_factory_areas
from factorio_agent.planning import overlap


class ProtectedDefensePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        self.catalog = SimpleNamespace(fingerprint="c")
        self.board = {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "blocks": {
                "refinery": {
                    "reserved_area": [[123, -63], [153, -51]],
                    "entities": [entity("oil-refinery", 128.5, -57.5)],
                    "ports": [
                        dict(
                            kind="fluid",
                            item="petroleum-gas",
                            direction="output",
                            facing=4,
                            name="pipe",
                            position={"x": 150.5, "y": -61.5},
                            fluidbox_index=1,
                        )
                    ],
                },
                "engine": {
                    "reserved_area": [[114, -115], [145, -96]],
                    "entities": [entity("transport-belt", 134.5, -95.5, 4)],
                    "ports": [
                        dict(
                            kind="item",
                            item="steel-plate",
                            direction="input",
                            facing=12,
                            position={"x": 143.5, "y": -108.5},
                        )
                    ],
                },
            },
            "shared_main_bus": {"reservations": [{"item": "iron-plate", "area": [[-16, -119], [-9.5, -12]]}]},
        }
        self.path = self.root / "pcb-board.json"
        self.path.write_text(json.dumps(self.board))

    def blocked(self, x, y, areas):
        return any(overlap([[x - 1, y - 1], [x + 1, y + 1]], r) for r in areas)

    def test_all_other_empty_producer_future_areas_and_bus_bands_are_protected(self):
        areas = protected_factory_areas(self.game, "w", "c")
        self.assertTrue(self.blocked(145, -57, areas))
        self.assertTrue(self.blocked(-15, -60, areas))
        self.assertFalse(self.blocked(180, -80, areas))

    def test_external_planned_route_and_fixed_input_apron_reject_original_engine_sites(self):
        areas = protected_factory_areas(self.game, "w", "c")
        self.assertTrue(self.blocked(135, -95, areas))
        self.assertTrue(self.blocked(146, -109, areas))
        self.assertFalse(self.blocked(135, -93, areas))
        self.assertFalse(self.blocked(146, -111, areas))

    def test_gas_first_outside_cell_and_independent_published_source_exits_stay_clear(self):
        (self.root / "material-sources.json").write_text(
            json.dumps(
                {
                    "world_id": "w",
                    "catalog_fingerprint": "c",
                    "sources": {
                        "plastic-coal": dict(
                            kind="item",
                            item="coal",
                            direction="output",
                            facing=4,
                            name="transport-belt",
                            position={"x": -45.5, "y": 24.5},
                            unit_number=2,
                            world_id="w",
                        )
                    },
                }
            )
        )
        areas = protected_factory_areas(self.game, "w", "c")
        self.assertTrue(self.blocked(154, -61, areas))
        self.assertTrue(self.blocked(-43, 24, areas))
        self.assertFalse(self.blocked(156, -61, areas))

    def test_prepare_reservations_are_readonly_and_preserve_authority_guards(self):
        before = self.path.read_bytes()
        c = DefensePerimeter(self.game, None, self.catalog)
        c.state = {"coverage_scopes": {}}
        with patch("factorio_agent.defense_perimeter._atomic_json") as write:
            areas = c._coverage_reservations({"world_id": "w"}, [[[20, 20], [24, 24]]])
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.blocked(145, -57, areas))
        self.board["catalog_fingerprint"] = "other"
        self.path.write_text(json.dumps(self.board))
        with self.assertRaises(ValueError):
            protected_factory_areas(self.game, "w", "c")

    def test_long_planned_pipe_is_coalesced_without_losing_footprints(self):
        self.board["blocks"]["pipe"] = {
            "entities": [entity("pipe", 70.5, y + 0.5) for y in range(-300, -100)],
            "ports": [],
        }
        self.path.write_text(json.dumps(self.board))
        areas = protected_factory_areas(self.game, "w", "c")
        self.assertLess(len(areas), 20)
        self.assertTrue(self.blocked(70, -250, areas))
        self.assertTrue(self.blocked(70, -101, areas))


if __name__ == "__main__":
    unittest.main()
