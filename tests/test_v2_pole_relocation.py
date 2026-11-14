"""A built pole on a route's forced end tile is relocated by the clear rung.

aud-1114-0526 seed 2446866888: the iron bank's power bridge pole (23.5,10.5) (unit 806, in
``mining-outposts.json`` banks[0].plan and retained_units and on the board block
``iron-ore_mining``) stood on the tile in front of the iron lane head (24.5,10.5); the bus feed
(21.5,16.5) -> (24.5,10.5) found no corridor for 25 minutes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import planned_bus as pb
from factorio_agent import pole_relocation as pr
from factorio_agent import remedy
from factorio_agent.board import entity
from factorio_agent.routing import VECTORS

POLE_AT = {"x": 23.5, "y": 10.5}
NEXT_POLE = {"x": 23.5, "y": 16.5}  # the bridge pole towards the bank (cut without the moved pole)


def live_pole(position=POLE_AT, unit=806):
    return dict(entity("small-electric-pole", position["x"], position["y"]), unit_number=unit)


class Reads:
    """The network read: first while planning, then the verify read."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return self.answers.pop(0) if self.answers else {"ok": True, "cut": [], "orphans": [], "reached": []}


class RelocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        pole_row = entity("small-electric-pole", POLE_AT["x"], POLE_AT["y"])
        self.board = {"world_id": "w", "blocks": {"iron-ore_mining": {"entities": [pole_row], "reserved_area": None}}}
        self.banks = {
            "world_id": "w",
            "banks": [
                {
                    "plan": {"resource": "iron-ore", "entities": [pole_row]},
                    "retained_units": [dict(pole_row, unit_number=806)],
                }
            ],
        }
        (self.root / "pcb-board.json").write_text(json.dumps(self.board), encoding="utf-8")
        (self.root / "mining-outposts.json").write_text(json.dumps(self.banks), encoding="utf-8")
        self.builder = Mock()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.chain = [entity("small-electric-pole", 20.5, 13.5)]
        self.connect = Mock(
            return_value={"ok": True, "entities": self.chain + [entity("small-electric-pole", **NEXT_POLE)]}
        )

    def controller(self, reads):
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)), query=reads)
        return pr.PoleRelocation(game, self.builder, SimpleNamespace(items={}), connect=self.connect)

    def obs(self, pole=True):
        entities = [live_pole(NEXT_POLE, 807)] + ([live_pole()] if pole else [])
        return {"world_id": "w", "tick": 1, "entities": entities, "inventory_empty_slots": 10}

    def test_the_faults_name_a_built_pole_on_a_route_end_tile(self):
        faults = pr.pole_faults(self.obs(), [(23.5, 10.5), (30.5, 10.5)])
        self.assertEqual([(f["position"], f["unit_number"]) for f in faults], [(POLE_AT, 806)])

    def test_replacement_first_then_verify_then_the_guarded_mine(self):
        reads = Reads(
            {
                "ok": True,
                "cut": [{"position": NEXT_POLE, "unit_number": 807}],
                "orphans": [],
                "reached": [{"x": 3.5, "y": 2.5}],
            },
            {"ok": True, "cut": [], "orphans": [], "reached": []},
        )
        relocation = self.controller(reads)
        fault = pr.pole_faults(self.obs(), [(23.5, 10.5)])[0]
        action = relocation.step(self.obs(), fault, keep_out=pr.tile_keep_out(POLE_AT))
        # The chain starts only from poles that keep power without the moved pole, off its tile.
        kwargs = self.connect.call_args.kwargs
        self.assertEqual(kwargs["sources"], {(3.5, 2.5)})
        self.assertIn([[22.5, 9.5], [24.5, 11.5]], kwargs["keep_out"])
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]["entities"], self.chain)
        self.assertEqual(action["type"], "mine")
        self.assertEqual((action["expected_entity_unit"], action["expected_entity_world_id"]), (806, "w"))
        # The owner plans were republished before anything was built: pole out, chain in.
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        names = [(e["name"], e["position"]) for e in board["blocks"]["iron-ore_mining"]["entities"]]
        self.assertEqual(names, [("small-electric-pole", {"x": 20.5, "y": 13.5})])
        bank = json.loads((self.root / "mining-outposts.json").read_text(encoding="utf-8"))["banks"][0]
        self.assertEqual([e["position"] for e in bank["plan"]["entities"]], [{"x": 20.5, "y": 13.5}])
        self.assertEqual(bank["retained_units"], [])
        # Once the pole is gone the relocation is finished.
        self.assertIsNone(relocation.step(self.obs(pole=False), None))
        self.assertFalse(relocation.active(self.obs(pole=False)))

    def test_no_mine_while_the_replacement_does_not_carry_the_cut_poles(self):
        reads = Reads(
            {"ok": True, "cut": [{"position": NEXT_POLE, "unit_number": 807}], "orphans": [], "reached": []},
            {"ok": True, "cut": [{"position": NEXT_POLE, "unit_number": 807}], "orphans": [], "reached": []},
        )
        relocation = self.controller(reads)
        out = relocation.step(self.obs(), pr.pole_faults(self.obs(), [(23.5, 10.5)])[0])
        self.assertEqual(out["reason"], "pole_relocation_replacement_not_connected")
        self.assertTrue(relocation.active(self.obs()))

    def test_machines_only_the_pole_powers_refuse_the_relocation_and_nothing_is_written(self):
        reads = Reads({"ok": True, "cut": [], "orphans": [{"name": "inserter", "position": {"x": 24.5, "y": 11.5}}]})
        relocation = self.controller(reads)
        out = relocation.step(self.obs(), pr.pole_faults(self.obs(), [(23.5, 10.5)])[0])
        self.assertEqual(out["reason"], "pole_relocation_would_unpower")
        self.assertEqual(json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8")), self.board)
        self.builder.ensure_plan.assert_not_called()

    def test_an_owner_plan_that_cannot_be_republished_refuses(self):
        other = {
            "world_id": "w",
            "power_plan": {"entities": [entity("small-electric-pole", POLE_AT["x"], POLE_AT["y"])]},
        }
        (self.root / "electric-coal.json").write_text(json.dumps(other), encoding="utf-8")
        relocation = self.controller(Reads({"ok": True, "cut": [], "orphans": [], "reached": []}))
        out = relocation.step(self.obs(), pr.pole_faults(self.obs(), [(23.5, 10.5)])[0])
        self.assertEqual(out["reason"], "pole_relocation_owner_not_republishable:electric-coal.json")
        self.assertEqual(json.loads((self.root / "mining-outposts.json").read_text(encoding="utf-8")), self.banks)


class BusDiagnosisTests(unittest.TestCase):
    def harness(self, root):
        from test_v2_planned_bus import PLANS, Harness

        h = Harness(root, PLANS[1])
        h.route.side_effect = lambda *a, **k: {"ok": False, "reason": "no_clean_trunk_corridor"}
        bus = h.master.bus
        lane = pb.built_lanes(bus)[0]
        head = pb.lane_tile(bus, lane, 0)
        dx, dy = VECTORS[pb.FACING[bus.direction]]
        return h, {"x": head["x"] - dx, "y": head["y"] - dy}

    def test_a_pole_in_front_of_a_lane_head_is_a_built_fault(self):
        with tempfile.TemporaryDirectory() as root:
            h, approach = self.harness(root)
            prepared = h.bus.prepare(h.obs(extra=[live_pole(approach, 99999)]))
            self.assertEqual(prepared["reason"], "planned_bus_feed_route_failed")
            self.assertEqual(prepared["plan_needs"]["kind"], "built")
            self.assertEqual(prepared["pole_faults"][0]["position"], approach)
            self.assertNotIn("pole_faults", h.bus.prepare(h.obs()))

    def test_the_clear_rung_starts_the_relocation_and_without_it_the_bus_only_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            h, approach = self.harness(root)
            obs = dict(h.obs(extra=[live_pole(approach, 99999)]), inventory_empty_slots=10)
            self.assertEqual(h.bus.step(obs)["status"], "blocked")
            h.game.query = Mock(return_value={"ok": True, "max_span": 4, "cut": [], "orphans": [], "reached": []})
            with remedy.applied({"repair_built_block": True}):
                action = h.bus.step(obs)
            self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 99999))

    def test_the_what_if_of_a_repair_rung_is_answered_by_the_retry(self):
        job = {"capability": "planned_main_bus", "parameters": {}, "status": "blocked"}
        out = remedy.what_if("unused", None, None, job, {"repair_built_block": True})
        self.assertIsNone(out["fits"])
        rung, _ = remedy.choose("planned_main_bus", {"kind": "built", "reason": "built_block_fault:x"}, [])
        self.assertEqual(rung.name, "replan")
        rung, _ = remedy.choose(
            "planned_main_bus",
            {"kind": "built", "reason": "built_block_fault:x"},
            [
                {
                    "rung": "replan",
                    "signature": remedy.plan_needs.signature({"kind": "built", "reason": "built_block_fault:x"}),
                }
            ],
        )
        self.assertEqual(rung.name, remedy.CLEAR_REBUILD.name)


if __name__ == "__main__":
    unittest.main()
