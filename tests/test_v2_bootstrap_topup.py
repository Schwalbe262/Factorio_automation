"""Closed-loop input top-up of the startup science cell (BOOTSTRAP_RESEARCH.md, closed-loop top-up).

Seed 1298193051 (fleet auto-1113-1758): logistics was funded with exactly
20 copper / 40 iron; the two hub-fed science machines finished 19 packs and
stood still with one copper plate in one machine and one gear in the other.
"""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from test_v2_research_capacity import RED, catalog

from factorio_agent.board import startup_science_cell
from factorio_agent.industry import CAPACITY_FILE, TOPUP_STALL_TICKS, BootstrapLogistics


class ClosedLoopTopupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(side_effect=AssertionError("no native query"))
        )
        self.plan = dict(startup_science_cell(10.5, 10.5, science_machines=2), reserved_area=[[2.0, 2.0], [18.0, 18.0]])
        board = dict(world_id="w", catalog_fingerprint="catalog", blocks={"startup_science": self.plan})
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        (self.root / CAPACITY_FILE).write_text(
            json.dumps(dict(world_id="w", decisions={"logistics": {"target_labs": 1}})), encoding="utf-8"
        )
        self.bootstrap = SimpleNamespace(
            ensure_item=Mock(side_effect=lambda obs, name, count: dict(type="take", item=name, count=count))
        )
        builder = SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"}), _sync=Mock())
        entities = [
            dict(spec, unit_number=i, electric_network_connected=True, energy=1000, products_finished=0, inventory={})
            for i, spec in enumerate(json.loads(json.dumps(self.plan["entities"])), 1)
        ]
        entities.append(
            dict(
                name="lab",
                unit_number=500,
                position={"x": 0.5, "y": 0.5},
                electric_network_connected=True,
                energy=1000,
                inventory={},
            )
        )
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=1000,
            entities=entities,
            inventory={"iron-plate": 2000, "copper-plate": 1000},
            production={RED: {"produced": 10, "consumed": 10}},
            research=None,
            research_progress=0,
            technologies={"automation": True, "automation-science-pack": True},
            enabled_recipes={
                name: True for name in ("assembling-machine-1", "inserter", "lab", RED, "iron-gear-wheel")
            },
        )
        self.controller = BootstrapLogistics(game, self.bootstrap, builder, catalog(), technology="logistics")

    def entity(self, position, name="wooden-chest"):
        return next(e for e in self.obs["entities"] if e["name"] == name and e["position"] == position)

    def reds(self):
        return [e for e in self.obs["entities"] if e.get("recipe") == RED]

    def act(self, expected_type="insert"):
        action = self.controller.step(self.obs)
        self.assertEqual(action.get("type"), expected_type, action)
        self.controller.record_action(action, {"ok": True, "moved": action["count"]})
        return action

    def run_exact_bill_to_19_packs(self):
        """Fund the exact bill, carry 19 packs into the lab, leave the 20th's ingredients split."""
        for item in ("iron-plate", "copper-plate"):
            self.assertEqual(self.act()["item"], item)
        state = self.controller.state
        self.assertEqual(state["required_inputs"], {"iron-plate": 40, "copper-plate": 20})
        self.assertEqual(state["allocated_inputs"], state["required_inputs"])
        self.obs["research"] = "logistics"
        output = self.entity(self.plan["ports"][-1]["position"])
        output["inventory"][RED] = 19
        self.assertEqual(self.act("take")["count"], 19)
        output["inventory"][RED] = 0
        self.obs["inventory"][RED] = 19
        self.assertEqual(self.act()["name"], "lab")
        self.obs["inventory"][RED] = 0
        self.reds()[0]["products_finished"] = 10
        self.reds()[1]["products_finished"] = 9
        self.obs["production"][RED]["consumed"] += 19
        self.obs["research_progress"] = 19 / 20
        # The 20th pack: its copper sits in one machine, its gear in the other.
        self.reds()[0]["inventory"] = {"copper-plate": 1}
        self.reds()[1]["inventory"] = {"iron-gear-wheel": 1}
        self.assertEqual((state["collected_packs"], state["supplied_packs"]), (19, 19))

    def test_exact_allowance_stalled_at_19_of_20_tops_up_one_pack_then_stops(self):
        self.run_exact_bill_to_19_packs()
        self.assertEqual(self.controller._cell_shortfall(self.obs, self.plan), (1, 0))
        wait = self.controller.step(self.obs)
        self.assertEqual(wait["status"], "waiting")
        self.assertNotIn("input_topups", self.controller.state)
        self.obs["tick"] += TOPUP_STALL_TICKS - 1
        self.assertEqual(self.controller.step(self.obs)["status"], "waiting")  # not still for long enough
        self.obs["tick"] += 1
        hub = self.plan["ports"][1]["position"]
        copper = self.act()
        self.assertEqual((copper["item"], copper["count"], copper["position"]), ("copper-plate", 1, hub))
        iron = self.act()
        self.assertEqual((iron["item"], iron["count"]), ("iron-plate", 2))
        [topup] = self.controller.state["input_topups"]
        self.assertEqual(topup["packs"], 1)
        self.assertEqual(topup["inputs"], topup["allocated_inputs"])
        self.assertEqual(topup["inputs"], {"iron-plate": 2, "copper-plate": 1})
        # The fixed bill is unchanged; the top-up is a separate, bounded allowance.
        self.assertEqual(self.controller.state["allocated_inputs"], {"iron-plate": 40, "copper-plate": 20})
        evidence = self.controller.step(self.obs)["evidence"]
        self.assertEqual(evidence["effective_input_allowance"], {"iron-plate": 42, "copper-plate": 21})
        # The plates are now in the cell: one more pack is deliverable, nothing more is bought.
        self.entity(hub)["inventory"]["copper-plate"] = 1
        self.entity(self.plan["ports"][0]["position"])["inventory"]["iron-plate"] = 2
        for _ in range(3):
            self.obs["tick"] += 2 * TOPUP_STALL_TICKS
            self.assertEqual(self.controller.step(self.obs)["status"], "waiting")
        self.assertEqual(len(self.controller.state["input_topups"]), 1)

    def test_satisfied_technology_gets_no_extra_input(self):
        self.run_exact_bill_to_19_packs()
        cases = {
            "last pack already in the lab": lambda: self.entity({"x": 0.5, "y": 0.5}, "lab")["inventory"].update(
                {RED: 1}
            ),
            "one machine holds a whole craft": lambda: self.reds()[0]["inventory"].update({"iron-gear-wheel": 1}),
            "technology researched": lambda: self.obs["technologies"].update(logistics=True),
        }
        for name, change in cases.items():
            with self.subTest(name):
                saved = json.loads(json.dumps(self.obs))
                change()
                for _ in range(3):
                    self.obs["tick"] += 2 * TOPUP_STALL_TICKS
                    result = self.controller.step(self.obs)
                    self.assertNotEqual(result.get("type"), "insert", result)
                self.assertNotIn("input_topups", self.controller.state)
                self.obs = saved
                self.controller.state.pop("topup_stall", None)

    def test_top_ups_are_capped_at_one_more_bill(self):
        self.run_exact_bill_to_19_packs()
        self.controller.state["input_topups"] = [
            dict(id="x" * 64, packs=20, inputs={"copper-plate": 20}, allocated_inputs={"copper-plate": 20})
        ]
        self.controller.step(self.obs)
        self.obs["tick"] += TOPUP_STALL_TICKS
        result = self.controller.step(self.obs)
        self.assertEqual(result["reason"], "bootstrap_research_topup_cap_reached")
        self.assertEqual(result["evidence"]["needed_packs"], 1)


if __name__ == "__main__":
    unittest.main()
