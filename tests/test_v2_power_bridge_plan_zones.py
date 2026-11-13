"""A mining bank's power bridge goes around the site plan's smelting zone when it can.

Fleet all-1113-2155 seed 852394612: the iron bank's bridge (23 poles) from the pole at
(-3.5, -37.5) to the bank at (40.5, 42.5) ran a column at x 16.5 and a diagonal straight across
the planned smelting zone [12, -32, 60, 32]. Seven of the eight district slots then stood on a
pole, the remaining sites failed at the corridor stage and the run was dead from minute 30.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.power_grid import plan_connection


def pole(x, y, unit):
    return {"name": "small-electric-pole", "position": {"x": x, "y": y}, "electric_network_id": 1, "unit_number": unit}


class BridgeZoneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {}}), encoding="utf-8")

        def query(lua):
            payload = lua.split("helpers.json_to_table(", 1)[1].split(");local clear", 1)[0]
            points = json.loads(json.loads(payload))["points"]
            return {"ok": True, "clear": list(range(1, len(points) + 1))}

        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=query)
        self.builder = SimpleNamespace(
            _occupied_by_plan=lambda entities: {(e["position"]["x"], e["position"]["y"]) for e in entities}
        )
        engine = {"name": "steam-engine", "position": {"x": -10, "y": -60}, "electric_network_id": 1, "energy": 1}
        self.obs = {"world_id": "w", "entities": [engine, pole(-3.5, -37.5, 1)]}

    def plan(self, zones):
        plan = {"zones": zones, "strips": []}
        (self.root / "site-plan.json").write_text(json.dumps({"world_id": "w", "plan": plan}), encoding="utf-8")
        return plan_connection(self.game, self.builder, self.obs, {"x": 40.5, "y": 42.5})

    def test_the_bridge_keeps_out_of_the_smelting_zone(self):
        smelting = {"kind": "smelting", "name": "smelting", "rect": [12, -32, 60, 32]}
        mine = {"kind": "mining", "name": "mining:iron-ore", "rect": [18, 33, 64, 80]}
        straight = self.plan([])
        self.assertTrue(straight["ok"], straight)
        inside = [e for e in straight["entities"] if 12 <= e["position"]["x"] <= 60 and -32 <= e["position"]["y"] <= 32]
        self.assertTrue(inside)  # without the plan the shortest chain crosses the zone
        around = self.plan([smelting, mine])
        self.assertTrue(around["ok"], around)
        for e in around["entities"]:
            p = e["position"]
            self.assertFalse(12 <= p["x"] <= 60 and -32 <= p["y"] <= 32, p)
        self.assertEqual(around["entities"][-1]["position"], {"x": 40.5, "y": 42.5})


if __name__ == "__main__":
    unittest.main()
