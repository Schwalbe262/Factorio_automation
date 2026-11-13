"""Power's boiler coal as an output-priority tap on the coal patch trunk (power_coal_tap.py)."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import mine_trunk
from factorio_agent import power_coal_tap as pct
from factorio_agent.core.deterministic_builder import BELT_FED, FactoryBuilder
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.paired_mining import mining_pair
from factorio_agent.transport import xy

RUN = 20
CATALOG = SimpleNamespace(
    fingerprint="c",
    entities={"boiler": {"energy_usage": 30000}},
    items={"coal": {"fuel_value": 4_000_000, "stack_size": 50}},
)
BOILER = {"name": "boiler", "position": {"x": 12.0, "y": 10.5}, "direction": 4}
POWER = {
    "ok": True,
    "entities": [
        BOILER,
        {"name": "steam-engine", "position": {"x": 16.5, "y": 10.5}, "direction": 4},
        {"name": "steam-engine", "position": {"x": 21.5, "y": 10.5}, "direction": 4},
        {"name": "small-electric-pole", "position": {"x": 16.5, "y": 13.5}},
    ],
    "ports": [
        {"kind": "power", "item": "electricity", "position": {"x": 16.5, "y": 13.5}},
        {"kind": "item", "item": "coal", "direction": "input", "facing": 4, "position": {"x": 8.5, "y": 10.5}},
    ],
}
OLD_DRILL = {"name": "electric-mining-drill", "position": {"x": -1.0, "y": 10.0}, "direction": 4}
OLD_POLE = {"name": "small-electric-pole", "position": {"x": -3.5, "y": 8.5}}
OLD_BELTS = [{"name": "transport-belt", "position": {"x": x + 0.5, "y": 10.5}, "direction": 4} for x in range(0, 9)]
COAL_PLAN = {
    "ok": True,
    "entities": [OLD_DRILL, OLD_POLE] + OLD_BELTS,
    "ports": [{"kind": "item", "item": "coal", "direction": "output", "facing": 4, "position": {"x": 0.5, "y": 10.5}}],
    "drill": OLD_DRILL,
    "template": "dedicated_boundary_coal_feed_v2",
}
READY = {
    "ok": True,
    "water": 200,
    "steam": 400,
    "boiler_fuel": 8_000_000,
    "drill_fuel": 0,
    "drill_powered": True,
    "coal_on_belts": 3,
    "connected_engines": 2,
}


def patch_bank():
    plan = mining_pair(-40.5, -40.5, 1, "coal")
    return {"ok": True, "ready": True, "plan": plan, "reserved_area": [[-42, -42], [-37, -35]]}


def trunk_belts(bank, start_unit=500):
    port = bank["plan"]["ports"][0]["position"]
    return [
        {"name": "transport-belt", "position": {"x": port["x"] + i, "y": port["y"]}, "direction": 4}
        | {"unit_number": start_unit + i}
        for i in range(RUN)
    ]


def fake_route(game, builder, obs, source, destination, reserved, **kw):
    end = {"name": "transport-belt", "position": dict(destination["position"]), "direction": destination["facing"]}
    return {"ok": True, "entities": [end], "underground_pairs": [], "clear_obstacles": []}


class PowerCoalTapFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = patch_bank()
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": [self.bank]}))
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {"power_coal_feed": {"entities": []}}}
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        b = FactoryBuilder.__new__(FactoryBuilder)
        b.catalog = CATALOG
        b.path = self.root / "factory-builder.json"
        b.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        b.bootstrap = SimpleNamespace(ensure_item=Mock(return_value={"status": "succeeded"}))
        b.state = {
            "world_id": "w",
            "seeds": {},
            "power_plan": deepcopy(POWER),
            "coal_plan": deepcopy(COAL_PLAN),
            "power_mode": BELT_FED,
        }
        b._save()
        b.ensure_plan = Mock(return_value={"status": "succeeded"})
        b.power_evidence = Mock(return_value=dict(READY, tick=100))
        self.builder = b
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        units = iter(range(10, 100))
        self.old = [dict(e, unit_number=next(units)) for e in COAL_PLAN["entities"]]
        self.obs = {
            "world_id": "w",
            "tick": 100,
            "inventory": {"splitter": 2, "transport-belt": 10},
            "inventory_empty_slots": 20,
            "entities": [dict(BOILER, inventory={"coal": 5}, unit_number=7)] + self.old + trunk_belts(self.bank),
        }
        patch("factorio_agent.transport.plan_connection", side_effect=fake_route).start()
        patch("factorio_agent.planning.preflight", return_value={"ok": True, "clear_obstacles": []}).start()
        self.addCleanup(patch.stopall)

    def controller(self):
        return pct.PowerCoalTap(self.game, self.builder, CATALOG)

    def build_tap(self, controller):
        tap = controller.state["tap"]
        removed = {(p["x"], p["y"]) for p in tap["remove"]}
        self.obs["entities"] = [e for e in self.obs["entities"] if xy(e) not in removed]
        for i, e in enumerate(tap["entities"] + controller.state["branch"]["entities"]):
            live = {k: v for k, v in e.items() if not k.startswith("_")}
            if e["name"] == "splitter":
                live.update(mine_trunk.tap_priorities(tap))
            self.obs["entities"].append(dict(live, unit_number=900 + i))

    def test_the_tap_serves_the_boilers_first_and_the_old_feed_keeps_running(self):
        controller = self.controller()
        action = controller.step(self.obs)
        tap = controller.state["tap"]
        self.assertEqual(tap["serve"], "first")
        self.assertEqual(mine_trunk.priority_side(tap), "branch")
        # Cut-over: one trunk belt, by unit; the old feed is untouched.
        self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", tap["remove_units"][0]))
        self.assertEqual(self.builder.state["coal_plan"], COAL_PLAN)
        ledger = json.loads((self.root / mine_trunk.STATE_FILE).read_text())
        self.assertIn(pct.TAP_NAME, ledger["trunks"][tap["trunk"]]["taps"])
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertIn(pct.BOARD_BLOCK, board["blocks"])
        # The branch side-loads onto a straight feed belt; everything from the join to the boiler is kept.
        join = controller.state["join"]
        self.assertEqual(xy(controller.state["branch"]["entities"][-1]), xy(join["port"]))
        self.assertEqual(controller.state["kept"][-1]["position"], POWER["ports"][1]["position"])

    def test_the_old_feed_is_retired_only_after_the_tap_is_proven(self):
        controller = self.controller()
        controller.step(self.obs)
        self.build_tap(controller)
        self.builder.power_evidence.return_value = dict(READY, coal_on_belts=0, tick=100)
        result = controller.step(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.builder.state["coal_plan"], COAL_PLAN)  # no trunk coal at the join yet
        self.builder.power_evidence.return_value = dict(READY, tick=200)
        self.assertEqual(controller.step(self.obs)["status"], "waiting")
        self.assertEqual(self.builder.state["coal_plan"], COAL_PLAN)  # proven, but not for 30 s
        self.builder.power_evidence.return_value = dict(READY, tick=200 + pct.VERIFY_TICKS)
        action = controller.step(self.obs)
        # Adopted plan saved first: no old drill in it, measured on a bank drill.
        saved = json.loads((self.root / "factory-builder.json").read_text())["coal_plan"]
        self.assertEqual(saved["template"], pct.TEMPLATE)
        self.assertEqual(saved["drill"]["name"], "electric-mining-drill")
        self.assertNotIn(xy(OLD_DRILL), {xy(e) for e in saved["entities"] if e["name"] == "electric-mining-drill"})
        self.assertIn(xy(OLD_POLE), {xy(e) for e in saved["entities"]})
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertNotIn(pct.FEED_BLOCK, board["blocks"])
        # Then the old drill goes first, by unit and world.
        old_drill = next(e for e in self.old if e["name"] == "electric-mining-drill")
        self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", old_drill["unit_number"]))
        # Never while the boiler is out of fuel.
        boiler = self.obs["entities"][0]
        boiler["inventory"] = {}
        self.assertEqual(controller.step(self.obs)["reason"], "power_coal_tap_boiler_unfuelled_before_retirement")
        boiler["inventory"] = {"coal": 5}
        retired = {pct._key(e) for e in controller.state["retire"]}
        self.assertEqual(
            retired,
            {pct._key(OLD_DRILL)} | {pct._key(e) for e in OLD_BELTS[: controller.state["join"]["index"]]},
        )
        self.obs["entities"] = [e for e in self.obs["entities"] if pct._key(e) not in retired]
        result = controller.step(self.obs)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["evidence"]["priority"], "branch")
        self.assertIsNotNone(self.controller().proof(self.obs))  # a restarted controller proves it too

    def test_the_wood_splitter_on_the_feed_is_passed_and_kept(self):
        wood = {"name": "splitter", "position": {"x": 7.5, "y": 10.0}, "direction": 4}
        plan = dict(COAL_PLAN, entities=[e for e in COAL_PLAN["entities"] if e["position"] != {"x": 7.5, "y": 10.5}])
        plan["entities"].append(wood)
        trace = pct.feed_trace(plan, POWER)
        self.assertTrue(trace["ok"], trace)
        rows = trace["entities"]
        self.assertIn(wood, rows)
        joins = pct.join_candidates(rows, set())
        self.assertTrue(joins)
        self.assertTrue(all(j["index"] < rows.index(wood) for j in joins))
        kept = rows[joins[0]["index"] :]
        self.assertNotIn(pct._key(wood), {pct._key(e) for e in pct.retired_entities(plan, kept)})

    def test_no_patch_bank_keeps_the_starter_feed(self):
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": []}))
        result = self.controller().step(self.obs)
        self.assertEqual(result["reason"], "power_coal_tap_no_coal_patch_bank")
        self.assertEqual(self.builder.state["coal_plan"], COAL_PLAN)


class PowerCoalTapPlanner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = patch_bank()

    def write(self, banks, mode=BELT_FED, template=None):
        plan = dict(COAL_PLAN, **({"template": template} if template else {}))
        builder = {"world_id": "w", "power_plan": POWER, "coal_plan": plan, "power_mode": mode}
        (self.root / "factory-builder.json").write_text(json.dumps(builder))
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": banks}))

    def test_receipt_waits_for_a_belt_fed_boiler_and_a_patch_bank(self):
        obs = {"world_id": "w", "entities": trunk_belts(self.bank)}
        self.write([], mode="hand_fed")
        self.assertIsNone(pct.tap_receipt(self.root, obs))
        self.write([])
        self.assertEqual(pct.tap_receipt(self.root, obs)["reason"], "no_coal_patch_bank")
        self.write([self.bank])
        self.assertEqual(pct.tap_receipt(self.root, obs), {"ready": True, "trunk": mine_trunk.trunk_key(self.bank)})
        self.write([self.bank], template=pct.TEMPLATE)
        self.assertTrue(pct.tap_receipt(self.root, obs)["adopted"])

    def test_node_is_proposed_once_after_its_requirements(self):
        receipt = {"mine_trunks": {"trunks": [], "power_tap": {"ready": True, "trunk": "coal@1,1"}}}
        done = [{"capability": c, "status": "succeeded"} for c in CAPABILITIES[pct.CAPABILITY]["requires"]]
        nodes = pct.power_coal_tap_nodes(done, [], receipt, CAPABILITIES)
        self.assertEqual([n["capability"] for n in nodes], [pct.CAPABILITY])
        self.assertEqual(pct.power_coal_tap_nodes(done[:-1], [], receipt, CAPABILITIES), [])
        again = done + [{"capability": pct.CAPABILITY, "status": "blocked"}]
        self.assertEqual(pct.power_coal_tap_nodes(again, [], receipt, CAPABILITIES), [])

    def test_trunk_receipt_carries_the_power_row_and_counts_independent_groups(self):
        self.write([self.bank])
        supply = {
            "world_id": "w",
            "supplies": {"coal": {"second_group_reason": {"reason": "mine_trunk_tap_unavailable"}}},
        }
        (self.root / "material-supply.json").write_text(json.dumps(supply))
        receipt = mine_trunk.trunk_receipt(self.root, [], {"world_id": "w", "entities": trunk_belts(self.bank)})
        self.assertTrue(receipt["power_tap"]["ready"])
        self.assertEqual(
            receipt["independent_groups"],
            [{"state": "material-supply.json", "resource": "coal", "reason": "mine_trunk_tap_unavailable"}],
        )


class TrunkSearchBeforeFallback(unittest.TestCase):
    def test_a_turned_trunk_is_searched_past_its_turn(self):
        bank = patch_bank()
        port = bank["plan"]["ports"][0]["position"]
        east = [
            {
                "name": "transport-belt",
                "position": {"x": port["x"] + i, "y": port["y"]},
                "direction": 4,
                "unit_number": i,
            }
            for i in range(4)
        ]
        corner = {"x": port["x"] + 4, "y": port["y"]}
        south = [
            {
                "name": "transport-belt",
                "position": {"x": corner["x"], "y": corner["y"] + i},
                "direction": 8,
                "unit_number": 100 + i,
            }
            for i in range(RUN)
        ]
        obs = {"world_id": "w", "entities": east + south}
        segments = mine_trunk.trunk_segments(obs, bank["plan"]["ports"][0])
        self.assertEqual([(s[0]["facing"], len(s[1])) for s in segments], [(4, 4), (8, RUN)])
        planned = mine_trunk.plan_tap(bank, obs, owner="a", name="a:coal", demand=10)
        self.assertTrue(planned["ok"], planned)
        self.assertEqual(planned["taps"][0]["segment"], 1)
        self.assertEqual(planned["taps"][0]["splitter"]["direction"], 8)

    def test_a_side_loaded_line_ends_the_trunk(self):
        bank = patch_bank()
        port = bank["plan"]["ports"][0]["position"]
        east = [
            {"name": "transport-belt", "position": {"x": port["x"] + i, "y": port["y"]}, "direction": 4}
            for i in range(4)
        ]
        line = [
            {"name": "transport-belt", "position": {"x": port["x"] + 4, "y": port["y"] + d}, "direction": 8}
            for d in (-1, 0, 1)
        ]
        segments = mine_trunk.trunk_segments({"entities": east + line}, bank["plan"]["ports"][0])
        self.assertEqual(len(segments), 1)

    def test_more_rows_are_tried_before_giving_up(self):
        bank = patch_bank()
        obs = {"world_id": "w", "entities": trunk_belts(bank)}
        seen = []

        def accept(tap):
            seen.append(tap["along"])
            return len(seen) > 2 * mine_trunk.ATTEMPTS  # every first-pass row is refused

        planned = mine_trunk.plan_tap(bank, obs, owner="a", name="a:coal", demand=10, accept=accept)
        self.assertTrue(planned["ok"], planned)
        self.assertGreater(len(seen), 2 * mine_trunk.ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
