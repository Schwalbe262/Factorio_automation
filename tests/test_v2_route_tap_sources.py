"""A taken block port serves a second consumer through a splitter on its route (eval-1575138435).

At ~440 game minutes the engine stock row (``production_row_engine-unit``) owned the steel block's
only output port (72.5,-150.5 facing north, route to 71.5,-162.5), and the engine belt row that blue
science needs blocked ``production_row_input_source_missing``. The live belts below are that route.
"""

import json
from pathlib import Path
import tempfile
import unittest

from factorio_agent import production_sources as sources
from factorio_agent.production_rows import ProductionRows

STEEL_PORT = {
    "direction": "output",
    "facing": 0,
    "item": "steel-plate",
    "kind": "item",
    "lane_items": {"left": "steel-plate", "right": "steel-plate"},
    "position": {"x": 72.5, "y": -150.5},
}
BOARD = {
    "world_id": "w",
    "blocks": {
        "steel_production": {"ports": [STEEL_PORT]},
        "production_row_engine-unit": {
            "ports": [
                {
                    "direction": "output",
                    "item": "engine-unit",
                    "name": "steel-chest",
                    "position": {"x": 77.5, "y": -180.5},
                }
            ]
        },
    },
    "connections": [
        {
            "owner": "production_row_engine-unit",
            "item": "steel-plate",
            "source": {"x": 72.5, "y": -150.5},
            "destination": {"x": 71.5, "y": -162.5},
        }
    ],
}


def belt(x, y, direction, unit, name="transport-belt", kind=None):
    row = {"name": name, "position": {"x": x, "y": y}, "direction": direction, "unit_number": unit}
    if kind:
        row["belt_to_ground_type"] = kind
    return row


# Live entities around the steel route (observation of the GUI map, read-only probe).
LIVE = [
    belt(69.5, -155.5, 12, 6953),
    belt(71.5, -156.5, 0, 6969, "underground-belt", "output"),
    belt(70.5, -156.5, 0, 5083),
    belt(71.5, -154.5, 0, 6968, "underground-belt", "input"),
    belt(70.5, -155.5, 12, 6952),
    belt(71.5, -155.5, 12, 6951),
    {"name": "small-electric-pole", "position": {"x": 70.5, "y": -154.5}, "unit_number": 4825},
    belt(71.5, -153.5, 0, 6962),
    belt(71.5, -152.5, 0, 6961),
    belt(71.5, -151.5, 0, 6960),
    belt(72.5, -156.5, 8, 6949),
    belt(73.5, -156.5, 12, 6948),
    belt(72.5, -155.5, 12, 6950),
    belt(72.5, -151.5, 12, 6959),
    belt(72.5, -150.5, 0, 4526),
    belt(72.5, -149.5, 0, 4525),
    belt(71.5, -157.5, 0, 6970),
    belt(71.5, -158.5, 0, 6971),
]
OBS = {"world_id": "w", "tick": 1, "entities": LIVE}


class ResolveTests(unittest.TestCase):
    def test_a_taken_port_is_a_route_tap_source_for_another_row(self):
        found = sources.resolve(BOARD, "engine-unit", "steel-plate", owner="production_row_engine-unit__belt")
        self.assertTrue(found["ok"])
        self.assertEqual(found["kind"], sources.ROUTE_TAP)
        self.assertEqual(found["block"], "steel_production")
        self.assertEqual(found["source"]["position"], {"x": 72.5, "y": -150.5})
        self.assertEqual(found["tapped_connection"]["owner"], "production_row_engine-unit")

    def test_the_owner_of_the_route_does_not_tap_itself(self):
        found = sources.resolve(BOARD, "engine-unit", "steel-plate", owner="production_row_engine-unit")
        self.assertFalse(found["ok"])
        self.assertEqual(found["reason"], "production_row_input_source_missing")

    def test_a_free_port_still_comes_before_a_route_tap(self):
        board = json.loads(json.dumps(BOARD))
        board["connections"] = []
        found = sources.resolve(board, "engine-unit", "steel-plate", owner="production_row_engine-unit__belt")
        self.assertEqual(found["kind"], "board_port")

    def test_the_smelting_district_port_is_never_route_tapped(self):
        board = json.loads(json.dumps(BOARD))
        board["blocks"] = {"smelting_district": board["blocks"]["steel_production"]}
        self.assertEqual(sources.tapped_board_ports(board, "steel-plate"), [])


class RouteTapSiteTests(unittest.TestCase):
    def test_sites_on_the_observed_steel_route(self):
        sites = sources.route_tap_sites(OBS, STEEL_PORT, item="steel-plate", name="t")
        first = sites[0]
        # The first plain belt fed from behind by the run, the free tile east of it.
        self.assertEqual(first["remove"], [{"x": 71.5, "y": -152.5}])
        self.assertEqual(first["remove_units"], [6961])
        self.assertEqual(first["splitter"]["position"], {"x": 72.0, "y": -152.5})
        self.assertEqual(first["splitter"]["direction"], 0)
        # Branch first: the new consumer takes what it uses, the stock row keeps the rest.
        self.assertEqual(first["splitter"]["splitter_output_priority"], "right")
        self.assertEqual(first["port"], {**first["port"], "position": {"x": 72.5, "y": -153.5}, "facing": 0})
        # Never a site whose branch meets standing hardware (the pole at 70.5,-154.5).
        for site in sites:
            tiles = {(e["position"]["x"], e["position"]["y"]) for e in site["entities"] if e["name"] != "splitter"}
            self.assertNotIn((70.5, -154.5), tiles)
            self.assertNotIn((72.5, -155.5), tiles)
        # An underground belt is never replaced.
        self.assertFalse(any(s["remove"][0]["y"] in (-154.5, -156.5) for s in sites))

    def test_a_turned_branch_side_loads_away_from_the_run(self):
        sites = sources.route_tap_sites(OBS, STEEL_PORT, item="steel-plate", name="t")
        turned = [s for s in sites if s["port"]["facing"] != 0]
        self.assertTrue(turned)
        for site in turned:
            self.assertEqual(site["port"]["facing"], 4 if site["side"] == "right" else 12)


class ReleaseTests(unittest.TestCase):
    def test_the_tapped_belt_leaves_the_owner_rows_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rows = ProductionRows.__new__(ProductionRows)
            rows.root = root
            stock = {
                "world_id": "w",
                "plan": {"entities": [belt(71.5, -152.5, 0, 6961), belt(71.5, -153.5, 0, 6962)]},
            }
            (root / "production-row-engine-unit.json").write_text(json.dumps(stock), encoding="utf-8")
            (root / "production-row-engine-unit__belt.json").write_text(
                json.dumps({"world_id": "w", "plan": {"entities": [belt(71.5, -152.5, 0, 1)]}}), encoding="utf-8"
            )
            tap = {"name": "t", "remove": [{"x": 71.5, "y": -152.5}]}
            changed = rows._release_row_belts(OBS, "engine-unit__belt", tap)
            self.assertEqual(changed, ["engine-unit"])
            saved = json.loads((root / "production-row-engine-unit.json").read_text(encoding="utf-8"))
            self.assertEqual([e["position"]["y"] for e in saved["plan"]["entities"]], [-153.5])
            self.assertEqual(saved["plan"]["released_to_route_taps"], ["t"])
            own = json.loads((root / "production-row-engine-unit__belt.json").read_text(encoding="utf-8"))
            self.assertEqual(len(own["plan"]["entities"]), 1)
            self.assertEqual(rows._release_row_belts(OBS, "engine-unit__belt", tap), [])


if __name__ == "__main__":
    unittest.main()
