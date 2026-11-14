"""A starter cell in the electric coal footprint is mined first, not a dead end (ElectricCoal._clearable_starter).

Fleet aud-1114-0841 seed 1971789367: electric_coal blocked electric_upgrade_footprint_obstructed
by a starter cell's burner drill placed before the upgrade site was reserved.
"""

import json
import tempfile
import unittest
from pathlib import Path

from test_v2_electric_coal_layouts import (
    ELECTRIC_DROP_VECTOR,
    FakeGame,
    Harness,
    boundary_plan,
    east_drop,
    payload_of,
    world,
)

STARTER = {"name": "burner-mining-drill", "position": {"x": 64, "y": -31}, "direction": 4, "unit_number": 99}


class ObstructingGame(FakeGame):
    """The footprint holds ``blocker`` until its unit number is ignored."""

    def __init__(self, root, blocker):
        super().__init__(root)
        self.blocker = blocker

    def query(self, script):
        args = payload_of(script)
        self.surveys.append(args)
        if self.blocker["unit_number"] not in args["ignore"]:
            return {
                "ok": False,
                "reason": "electric_upgrade_footprint_obstructed",
                "entity": self.blocker["name"],
                "position": dict(self.blocker["position"]),
            }
        return {"ok": True, "drop": east_drop(args["new"]["position"], ELECTRIC_DROP_VECTOR), "remaining": 5000}


def harness(directory, board_owned=False):
    h = Harness(directory, boundary_plan())
    h.game = ObstructingGame(directory, STARTER)
    h.routine.game = h.game
    if board_owned:
        board = {"world_id": "w", "blocks": {"cell": {"entities": [{k: STARTER[k] for k in ("name", "position")}]}}}
        Path(directory, "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
    obs = world(h.builder.state["coal_plan"])
    obs["entities"].append(dict(STARTER))
    return h, obs


class StarterClearTests(unittest.TestCase):
    def test_unowned_starter_cell_is_mined_before_the_cutover(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = harness(d)
            action = h.step(obs)
            rows = h.routine.state["displaced_inserters"]
            self.assertEqual([(r["spec"]["name"], r["unit_number"]) for r in rows], [("burner-mining-drill", 99)])
            self.assertEqual((action["type"], action["position"]), ("mine", STARTER["position"]))
            self.assertEqual(action["expected_entity_unit"], 99)
            self.assertIn("burner-mining-drill", action["reason"])

    def test_a_block_owned_entity_still_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = harness(d, board_owned=True)
            result = h.step(obs)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "electric_upgrade_footprint_obstructed")


if __name__ == "__main__":
    unittest.main()
