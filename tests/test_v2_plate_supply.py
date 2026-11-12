"""Red science takes its plates from the smelting district's registered output ports."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.plate_supply import plate_sources
from factorio_agent.science import AutomaticScience
from factorio_agent.site.transform import Transform, apply
from factorio_agent.smelting_site import district_candidate, oriented
from factorio_agent.transport import xy

WORLD, CATALOG = "w1", "cat"
COUNTS = {"iron-ore": 4, "copper-ore": 2}
# Identity, quarter/half/three-quarter turns and mirrored turns, as the site fit uses them.
TURNS = ((0, None), (4, None), (8, None), (12, None), (0, "x"), (4, "x"), (12, "y"))


def district_plan(transform=Transform()):
    """The district plan as ``SmeltingDistrict.prepare`` registers it (output ports only)."""
    site = district_candidate(40, 40, COUNTS)
    if not transform.identity:
        site = oriented(apply(transform, site), transform)
    ports = [p for b in site["banks"] for p in b["ports"] if p["direction"] == "output"]
    return _plan(site["entities"], ports, reserved_area=site["area"], layout_policy="pcb-smelting-district-v1")


def board(plan=None, connections=()):
    blocks = {"smelting_district": plan} if plan else {}
    return {"world_id": WORLD, "catalog_fingerprint": CATALOG, "blocks": blocks, "connections": list(connections)}


def lanes_file(root, world=WORLD):
    module = {
        "ports": [
            {
                "kind": "item",
                "item": "iron-plate",
                "position": {"x": -9.5, "y": -29.5},
                "facing": 4,
                "direction": "input",
            },
            {
                "kind": "item",
                "item": "iron-plate",
                "position": {"x": -9.5, "y": -29.5},
                "facing": 4,
                "direction": "output",
            },
        ]
    }
    copper = {"ports": [dict(p, item="copper-plate", position={"x": -9.5, "y": -17.5}) for p in module["ports"]]}
    data = {"world_id": world, "catalog_fingerprint": CATALOG, "modules": [module, copper]}
    (Path(root) / "smelter-lanes.json").write_text(json.dumps(data), encoding="utf-8")


class PlateSourceTests(unittest.TestCase):
    def test_district_output_ports_are_the_sources_in_any_orientation(self):
        for rotation, mirror in TURNS:
            turn = Transform(rotation, mirror, (56, 56))
            facing = turn.direction(4)  # canonical plates leave east
            plan = district_plan(turn)
            with TemporaryDirectory() as root:
                found = plate_sources(root, board(plan), WORLD, CATALOG, owner="automation_science")
            self.assertTrue(found["ok"], (rotation, mirror))
            self.assertEqual(found["origin"], {"iron-plate": "smelting_district", "copper-plate": "smelting_district"})
            for item, port in found["ports"].items():
                self.assertEqual(port["facing"], facing, (rotation, mirror, item))
                # The port is the last belt of the output row, heading out of the district.
                belt = next(e for e in plan["entities"] if e["name"] == "transport-belt" and xy(e) == xy(port))
                self.assertEqual(belt["direction"], facing)
                (x0, y0), (x1, y1) = plan["reserved_area"]
                self.assertTrue(x0 <= port["position"]["x"] <= x1 and y0 <= port["position"]["y"] <= y1)

    def test_saved_district_plan_is_used_before_the_board_block_exists(self):
        with TemporaryDirectory() as root:
            state = {"world_id": WORLD, "plan": district_plan()}
            (Path(root) / "smelting-district.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertTrue(plate_sources(root, board(), WORLD, CATALOG, owner="automation_science")["ok"])
            other = plate_sources(root, board(), "w2", CATALOG, owner="automation_science")
        self.assertEqual(other, {"ok": False, "reason": "plate_source_port_missing", "item": "iron-plate"})

    def test_a_port_taken_by_another_consumer_is_not_handed_out_twice(self):
        plan = district_plan()
        iron = next(p for p in plan["ports"] if p["item"] == "iron-plate")
        taken = {"item": "iron-plate", "source": iron["position"], "destination": {"x": 0, "y": 0}, "owner": "produce"}
        with TemporaryDirectory() as root:
            result = plate_sources(root, board(plan, [taken]), WORLD, CATALOG, owner="automation_science")
            self.assertEqual(result, {"ok": False, "reason": "plate_source_port_taken", "item": "iron-plate"})
            # Its own earlier connection does not hide the port from red science.
            mine = dict(taken, owner="automation_science")
            again = plate_sources(root, board(plan, [mine]), WORLD, CATALOG, owner="automation_science")
        self.assertEqual(again["ports"]["iron-plate"]["position"], iron["position"])

    def test_old_lane_file_is_only_a_same_world_fallback(self):
        plan = district_plan()
        with TemporaryDirectory() as root:
            lanes_file(root)
            first = plate_sources(root, board(plan), WORLD, CATALOG, owner="automation_science")
            self.assertEqual(set(first["origin"].values()), {"smelting_district"})
            legacy = plate_sources(root, board(), WORLD, CATALOG, owner="automation_science")
            self.assertEqual(legacy["ports"]["iron-plate"]["position"], {"x": -9.5, "y": -29.5})
            self.assertEqual(set(legacy["origin"].values()), {"smelter_lanes"})
            lanes_file(root, world="old")
            stale = plate_sources(root, board(), WORLD, CATALOG, owner="automation_science")
        self.assertEqual(stale["reason"], "plate_source_port_missing")


class SciencePrepareSourceTests(unittest.TestCase):
    def test_fresh_map_without_lane_file_reports_missing_plates_instead_of_crashing(self):
        with TemporaryDirectory() as root:
            (Path(root) / "pcb-board.json").write_text(json.dumps(board()), encoding="utf-8")
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            science = AutomaticScience(game, None, SimpleNamespace(fingerprint=CATALOG), site=None)
            result = science.prepare({"world_id": WORLD}, {"rate_per_minute": 30})
        self.assertEqual(result, {"ok": False, "reason": "plate_source_port_missing", "item": "iron-plate"})


if __name__ == "__main__":
    unittest.main()
