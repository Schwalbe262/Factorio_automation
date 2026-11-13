"""Wood burner feed: chest -> burner arm -> wood-first splitter on the coal feed (wood_burner_feed)."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent import carried_surplus as cs
from factorio_agent import wood_burner_feed as wbf
from factorio_agent.core.deterministic_builder import BELT_FED, HAND_FED
from factorio_agent.jobs import CAPABILITIES

WORLD = "w"
CATALOG = SimpleNamespace(fingerprint="c", items={"wood": {"stack_size": 100}}, entities={})
DRILL = {"name": "burner-mining-drill", "position": {"x": 0.0, "y": 0.0}, "direction": 4}
BELTS = [{"name": "transport-belt", "position": {"x": x + 0.5, "y": -0.5}, "direction": 4} for x in range(1, 13)]
COAL_PLAN = {
    "ok": True,
    "entities": [DRILL, {"name": "burner-inserter", "position": {"x": 1.5, "y": 0.5}, "direction": 4}, *BELTS],
    "ports": [{"kind": "item", "item": "coal", "direction": "output", "position": {"x": 2.5, "y": -0.5}}],
    "drill": DRILL,
}
POWER = {"entities": [], "ports": [{"item": "coal", "direction": "input", "position": {"x": 14.5, "y": -0.5}}]}
READY = {
    "ok": True,
    "water": 200,
    "steam": 400,
    "boiler_fuel": 8e6,
    "drill_fuel": 1,
    "coal_on_belts": 9,
    "connected_engines": 2,
}


def live(spec, unit, **extra):
    return dict(deepcopy(spec), unit_number=unit, **extra)


class LayoutTests(unittest.TestCase):
    def test_splitter_input_priority_is_the_wood_side_and_output_the_feed_side(self):
        rows = wbf.layouts(COAL_PLAN)
        self.assertTrue(rows)
        for row in rows:
            x, y = row["belt"]["position"]["x"], row["belt"]["position"]["y"]
            self.assertGreater(x - 0.5, wbf.CELL_CLEARANCE)  # never next to the feed cell's drill or arm
            wood_belt, arm, chest = row["entities"]
            south = wood_belt["position"]["y"] > y
            self.assertEqual(row["splitter"]["splitter_input_priority"], "right" if south else "left")  # facing east
            self.assertEqual(row["splitter"]["splitter_output_priority"], "left" if south else "right")
            # Column along the side, against the flow: input belt, arm (pickup = chest side), chest.
            self.assertEqual([e["position"]["x"] for e in row["entities"]], [x - 1, x - 2, x - 3])
            self.assertEqual((wood_belt["direction"], arm["direction"], arm["name"]), (4, 12, "burner-inserter"))
            self.assertEqual(row["free"]["x"], x + 1)  # the unused splitter output stays free

    def test_splice_replaces_exactly_the_feed_belt(self):
        row = next(r for r in wbf.layouts(COAL_PLAN) if r["belt"]["position"]["x"] == 11.5)
        spliced = wbf.spliced_coal_plan(COAL_PLAN, row)
        belts = [e for e in spliced["entities"] if e["name"] == "transport-belt"]
        self.assertEqual(len(belts), len(BELTS) - 1)
        self.assertIn(row["splitter"], spliced["entities"])
        self.assertEqual(spliced["required_items"]["splitter"], 1)
        self.assertEqual(wbf.spliced_coal_plan(spliced, row), spliced)  # idempotent
        self.assertEqual((spliced["drill"], spliced["ports"]), (DRILL, COAL_PLAN["ports"]))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), backend="assisted")
        self.builder = SimpleNamespace(
            state={"world_id": WORLD, "power_mode": BELT_FED, "coal_plan": deepcopy(COAL_PLAN), "power_plan": POWER},
            _save=Mock(),
            _move=lambda o, p: None,
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={"status": "succeeded"})),
            power_evidence=Mock(return_value=READY),
        )
        self.feed = wbf.WoodBurnerFeed(game, self.builder, CATALOG)
        self.entities = [live(b, 100 + i) for i, b in enumerate(BELTS)]

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, wood=0, splitter=0):
        return {
            "world_id": WORLD,
            "tick": 5,
            "position": {"x": 0, "y": 0},
            "inventory": {"wood": wood, "splitter": splitter},
            "entities": self.entities,
        }

    def test_build_splice_seed_stock_and_proof(self):
        with patch("factorio_agent.planning.preflight", return_value={"ok": True, "clear_obstacles": []}):
            first = self.feed.step(self.obs(wood=200, splitter=1))
        layout = self.feed.state["layout"]
        self.assertEqual(layout["belt"]["position"], {"x": 11.5, "y": -0.5})  # nearest the boilers
        self.assertEqual((first["type"], first["expected_entity_unit"]), ("mine", 110))  # side built, then the splice
        self.assertIn(layout["splitter"], self.builder.state["coal_plan"]["entities"])  # plan republished first
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"]["wood_burner_feed"]["owner"], "power")
        # The world after the splice: belt mined, splitter and the wood side standing.
        self.entities = [e for e in self.entities if e["unit_number"] != 110]
        wood_belt, arm, chest = layout["entities"]
        self.entities += [
            live(layout["splitter"], 200),
            live(wood_belt, 201),
            live(arm, 202, inventory={}),
            live(chest, 203, inventory={}),
        ]
        seed = self.feed.step(self.obs(wood=200))
        self.assertEqual((seed["type"], seed["name"], seed["inventory"], seed["count"]), ("insert", wbf.ARM, "fuel", 2))
        self.entities[-2]["inventory"] = {"wood": 2}
        deposit = self.feed.step(self.obs(wood=198))
        self.assertEqual((deposit["type"], deposit["expected_entity_unit"], deposit["count"]), ("deposit", 203, 198))
        self.assertEqual(self.feed.state["chest"]["unit_number"], 203)  # registered as the wood depot
        self.entities[-1]["inventory"] = {"wood": 198}
        done = self.feed.step(self.obs())
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["evidence"]["input_priority"], layout["splitter"]["splitter_input_priority"])
        # The feed proof counts the remaining belts; the splitter belongs to the feed plan now.
        power, coal = self.builder.power_evidence.call_args.args
        self.assertEqual(sum(e["name"] == "splitter" for e in coal["entities"]), 1)
        self.assertNotIn(layout["belt"], coal["entities"])
        # Wrong priority on the live splitter: no proof.
        self.entities[-4]["splitter_input_priority"] = "none"
        self.assertIsNone(self.feed.proof(self.obs()))

    def test_the_chest_is_the_wood_sink_after_coal_filled_boilers(self):
        self.feed.state = {"world_id": WORLD, "chest": {"name": "wooden-chest", "unit_number": 9}}
        chest = {"name": "wooden-chest", "position": {"x": 4.5, "y": 4.5}, "unit_number": 9, "inventory": {}}
        boiler = {"name": "boiler", "position": {"x": 14, "y": 2}, "unit_number": 1, "inventory": {"coal": 40}}
        surplus = cs.CarriedSurplus(CATALOG, wood_feed=self.feed)
        state = dict(self.obs(wood=300), entities=[boiler, chest])
        action = surplus.step(state, idle=True)
        self.assertEqual((action["type"], action["expected_entity_unit"], action["count"]), ("deposit", 9, 300))

    def test_hand_fed_or_reserved_site_is_refused(self):
        self.builder.state["power_mode"] = HAND_FED
        self.assertEqual(self.feed.prepare(self.obs())["reason"], "wood_feed_requires_belt_fed_coal_feed")


class NodeTests(unittest.TestCase):
    def nodes(self, wood=40, built=False, recipes=("splitter",), jobs=None):
        jobs = jobs if jobs is not None else [{"capability": "power_coal_feed", "status": "succeeded"}]
        observation = {"wood_burner_feed": {"wood": wood, "built": built}, "enabled_recipes": list(recipes)}
        return wbf.wood_burner_feed_nodes(jobs, [], observation, CAPABILITIES)

    def test_proposed_once_when_wood_accumulates_on_a_belt_fed_plant_with_splitters(self):
        self.assertEqual([n["id"] for n in self.nodes()], [wbf.NODE_ID])
        self.assertEqual(self.nodes(wood=wbf.WOOD_BATCH - 1), [])
        self.assertEqual(self.nodes(built=True), [])
        self.assertEqual(self.nodes(recipes=()), [])  # splitters need logistics
        self.assertEqual(self.nodes(jobs=[]), [])  # the coal feed must be belt-fed first
        taken = [{"capability": "power_coal_feed", "status": "succeeded"}, {"capability": wbf.CAPABILITY}]
        self.assertEqual(self.nodes(jobs=taken), [])

    def test_receipt_counts_carried_and_depot_wood_only_when_belt_fed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            builder = {"world_id": WORLD, "power_mode": BELT_FED, "coal_plan": COAL_PLAN}
            (root / "factory-builder.json").write_text(json.dumps(builder), encoding="utf-8")
            storage = {"world_id": WORLD, "chests": [{"name": "iron-chest", "unit_number": 4}]}
            (root / "inventory-storage.json").write_text(json.dumps(storage), encoding="utf-8")
            depot = {"name": "iron-chest", "unit_number": 4, "inventory": {"wood": 30}}
            observation = {"world_id": WORLD, "inventory": {"wood": 12}, "entities": [depot]}
            self.assertEqual(wbf.receipt(root, observation), {"wood": 42, "built": False})
            builder["power_mode"] = HAND_FED
            (root / "factory-builder.json").write_text(json.dumps(builder), encoding="utf-8")
            self.assertIsNone(wbf.receipt(root, observation))


if __name__ == "__main__":
    unittest.main()
