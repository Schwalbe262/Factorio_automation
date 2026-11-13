"""One mining block per patch: consumers tap the patch bank's trunk (mine_trunk.py)."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import mine_trunk
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.material_supply import TRUNK_MODE, DedicatedMaterials
from factorio_agent.outposts import MiningOutposts
from factorio_agent.paired_mining import mining_pair
from factorio_agent.transport import xy

RUN = 20  # observed straight trunk tiles east of the bank's output port


def patch_bank():
    plan = mining_pair(-40.5, -40.5, 1, "coal")
    return {
        "ok": True,
        "ready": True,
        "plan": plan,
        "reserved_area": [[-42.0, -42.0], [-37.0, -35.0]],
        "reserved_pair_slots": 3,
        "nominal_rate_per_minute": 60.0,
        "wire_hops": 0,
    }


def trunk_belts(bank, start_unit=500):
    port = bank["plan"]["ports"][0]["position"]
    return [
        {
            "name": "transport-belt",
            "position": {"x": port["x"] + i, "y": port["y"]},
            "direction": 4,
            "unit_number": start_unit + i,
        }
        for i in range(RUN)
    ]


class MineTrunkRules(unittest.TestCase):
    def test_lane_count_follows_bandwidth(self):
        rate = mine_trunk.belt_rate("transport-belt")
        self.assertEqual(mine_trunk.lane_count(60, rate), 1)
        self.assertEqual(mine_trunk.lane_count(rate, rate), 1)
        self.assertEqual(mine_trunk.lane_count(rate + 1, rate), 2)
        self.assertEqual(mine_trunk.lane_count(2.5 * rate, rate), 3)
        self.assertAlmostEqual(mine_trunk.lane_saturation(30, 30, rate), 1.0)

    def test_two_lanes_get_an_input_balanced_merge(self):
        self.assertIsNone(mine_trunk.balancer_plan(1))
        self.assertEqual(
            mine_trunk.balancer_plan(2), {"inputs": 2, "outputs": 2, "kind": "splitter_2_2", "implemented": True}
        )
        self.assertEqual(mine_trunk.balancer_plan(4)["implemented"], False)

    def test_patch_bank_is_the_oldest_non_material_bank_on_the_patch(self):
        first, material, far = patch_bank(), patch_bank(), patch_bank()
        material["material_supply_id"] = "dedicated-material:w:coal"
        far["mining_reserved_area"] = [[400, 400], [410, 410]]
        seed = {"x": -40, "y": -40}
        self.assertIs(mine_trunk.patch_bank([material, first], "coal", seed), first)
        self.assertIsNone(mine_trunk.patch_bank([far], "coal", seed))
        self.assertIsNone(mine_trunk.patch_bank([first], "iron-ore", seed))
        unready = dict(first, ready=False)
        self.assertIsNone(mine_trunk.patch_bank([unready], "coal", seed))

    def test_tap_priority_power_first_or_after_downstream(self):
        bank = patch_bank()
        obs = {"world_id": "w", "entities": trunk_belts(bank)}
        first = mine_trunk.plan_tap(bank, obs, owner="power", name="power:coal", demand=40, serve="first")
        after = mine_trunk.plan_tap(bank, obs, owner="bricks", name="bricks:coal", demand=40, serve="after")
        self.assertTrue(first["ok"] and after["ok"])
        self.assertEqual(mine_trunk.priority_side(first["taps"][0]), "branch")
        self.assertEqual(mine_trunk.priority_side(after["taps"][0]), "trunk")
        tap = first["taps"][0]
        # One trunk belt is replaced by the splitter; the branch leaves sideways without crossing a lane.
        self.assertEqual(len(tap["remove"]), 1)
        self.assertFalse(any(e["name"] == "underground-belt" for e in tap["entities"]))
        self.assertEqual(
            tap["splitter"]["splitter_output_priority"], mine_trunk.tap_priorities(tap)["splitter_output_priority"]
        )

    def test_a_later_tap_keeps_the_window_gap(self):
        bank = patch_bank()
        obs = {"world_id": "w", "entities": trunk_belts(bank)}
        first = mine_trunk.plan_tap(bank, obs, owner="a", name="a:coal", demand=30)["taps"][0]
        second = mine_trunk.plan_tap(bank, obs, owner="b", name="b:coal", demand=30, stations=[first])["taps"][0]
        self.assertGreaterEqual(abs(second["along"] - first["along"]), mine_trunk.WINDOW_ROWS + 1)

    def test_no_observed_trunk_is_a_precise_failure(self):
        result = mine_trunk.plan_tap(patch_bank(), {"world_id": "w", "entities": []}, owner="a", name="a", demand=1)
        self.assertEqual(result["reason"], "mine_trunk_not_observed")


class MineTrunkGrowth(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = patch_bank()
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": [self.bank]}))

    def record(self, name, demand, primary=60.0):
        tap = {"name": name, "trunk": mine_trunk.trunk_key(self.bank), "demand_per_minute": demand}
        tap["primary_demand"] = primary
        mine_trunk.record_tap(self.root, "w", tap)

    def test_demand_above_supply_grows_the_same_bank(self):
        self.record("bricks:coal", 60)
        obs = {"world_id": "w"}
        receipt = mine_trunk.trunk_receipt(self.root, [], obs)
        row = receipt["trunks"][0]
        self.assertEqual((row["demand_per_minute"], row["supply_per_minute"], row["grow"]), (120.0, 60.0, True))
        self.assertEqual(row["lanes_needed"], 1)
        nodes = mine_trunk.mine_trunk_nodes([], [], {"mine_trunks": receipt}, {"mining_outpost": {}})
        # Build ahead: 120 x (1 + DEFAULT_GROWTH_SHARE 0.25) = 150/min, inside the frame of 3 reserved
        # pairs x 2 drills x 30/min = 180/min (the trunk knows no lookahead: the allowance applies).
        self.assertEqual(row["target_per_minute"], 150.0)
        self.assertEqual(
            [(n["capability"], n["parameters"]) for n in nodes],
            [("mining_outpost", {"resource": "coal", "rate_per_minute": 150})],
        )
        running = [{"capability": "mining_outpost", "parameters": {"resource": "coal", "rate_per_minute": 30}}]
        running[0]["status"] = "running"
        self.assertEqual(mine_trunk.mine_trunk_nodes(running, [], {"mine_trunks": receipt}, {"mining_outpost": {}}), [])

    def test_build_ahead_stays_inside_the_reserved_pairs(self):
        # 170 x 1.25 = 212.5 is above the 180/min frame (3 reserved pairs): the target stops at the frame.
        self.assertEqual(mine_trunk.ahead_target(170.0, 60.0, self.bank), 180.0)
        # Beyond the frame (a connected module) only the known demand grows.
        self.assertEqual(mine_trunk.ahead_target(200.0, 60.0, self.bank), 200.0)
        # A covered trunk has no growth to size.
        self.assertEqual(mine_trunk.ahead_target(50.0, 60.0, self.bank), 50.0)

    def test_growth_does_not_loop_and_a_later_tap_adds_only_its_demand(self):
        self.record("bricks:coal", 60)
        self.bank["nominal_rate_per_minute"] = 120.0  # grown in place
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": [self.bank]}))
        self.record("chem:coal", 60, primary=120.0)  # planned after the growth
        row = mine_trunk.trunk_receipt(self.root, [], {"world_id": "w"})["trunks"][0]
        self.assertEqual(row["demand_per_minute"], 180.0)
        self.record("big:coal", 900)
        row = mine_trunk.trunk_receipt(self.root, [], {"world_id": "w"})["trunks"][0]
        self.assertEqual(row["lanes_needed"], 2)
        self.assertTrue(row["second_lane_required"])
        self.assertEqual(row["balancer"]["kind"], "splitter_2_2")


class MaterialSupplyOnTrunk(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(fingerprint="catalog")
        self.game = Mock()
        self.game.cfg = SimpleNamespace(runtime_dir=self.root)
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            _occupied_by_plan=geometry._occupied_by_plan,
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={"status": "succeeded"})),
        )
        self.bank = patch_bank()
        self.outposts = MiningOutposts.__new__(MiningOutposts)
        self.outposts.path = self.root / "mining-outposts.json"
        self.outposts.state = {"world_id": "w", "banks": [deepcopy(self.bank)]}
        self.outposts.path.write_text(json.dumps(self.outposts.state))
        self.outposts.bank_proof = Mock(side_effect=self.native_proof)
        self.outposts.publish_reservations = Mock(return_value=None)
        belts = trunk_belts(self.bank)
        # The trunk belongs to the smelting district's saved plan (its coal route).
        district_plan = {"ok": True, "entities": [{k: e[k] for k in ("name", "position", "direction")} for e in belts]}
        (self.root / "smelting-district.json").write_text(json.dumps({"world_id": "w", "plan": district_plan}))
        board = {"world_id": "w", "catalog_fingerprint": "catalog", "blocks": {"smelting_district": district_plan}}
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        drills = [
            dict(e, unit_number=10 + i, energy=1, electric_network_connected=True)
            for i, e in enumerate(self.bank["plan"]["entities"])
            if e["name"] == "electric-mining-drill"
        ]
        self.obs = {
            "world_id": "w",
            "tick": 100,
            "technologies": {"electric-mining-drill": True},
            "inventory": {"splitter": 4, "transport-belt": 20},
            "entities": drills + belts,
        }
        self.district = SimpleNamespace(state={"stale": True})
        self.new_bank = patch("factorio_agent.material_supply.prepare_new_bank").start()
        patch("factorio_agent.material_supply.preflight", return_value={"ok": True, "clear_obstacles": []}).start()
        self.addCleanup(patch.stopall)

    def native_proof(self, obs, bank):
        units = sorted(e["unit_number"] for e in obs["entities"] if e["name"] == "electric-mining-drill")
        lanes = {"ok": True, "left": {"coal": 1}, "right": {"coal": 1}}
        return {"drill_units": units, "native_producer_lanes": lanes}

    def controller(self, purpose=None):
        perimeter = None
        if purpose:
            perimeter = Mock()
            perimeter.prepare_coverage.return_value = {"ok": True}
            perimeter.ensure_coverage.return_value = None
            perimeter.coverage_proof.return_value = {"ok": True}
        return DedicatedMaterials(
            self.game,
            self.builder,
            self.catalog,
            self.outposts,
            perimeter=perimeter,
            purpose=purpose,
            smelting=self.district,
        )

    def banks(self):
        return json.loads(self.outposts.path.read_text())["banks"]

    def test_second_consumer_taps_the_trunk_and_opens_no_second_group(self):
        controller = self.controller()
        action = controller.step(self.obs, {"resource": "coal"})
        self.new_bank.assert_not_called()
        self.assertEqual(len(self.banks()), 1)
        entry = controller.state["supplies"]["coal"]
        self.assertEqual(entry["mode"], TRUNK_MODE)
        self.assertEqual(set(entry["ports"]), {"coal", "brick-coal"})
        tap = entry["tap"]
        # Only one trunk belt is recovered, by unit, after the owner's plan dropped it.
        self.assertEqual(action["type"], "mine")
        self.assertEqual(action["expected_entity_unit"], tap["remove_units"][0])
        district = json.loads((self.root / "smelting-district.json").read_text())["plan"]
        removed = {(p["x"], p["y"]) for p in tap["remove"]}
        self.assertFalse(any(xy(e) in removed for e in district["entities"]))
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"]["smelting_district"], district)
        self.assertEqual(self.district.state["plan"], district)  # the slice's controller sees the new plan
        self.assertIn("material_tap:" + entry["material_supply_id"], board["blocks"])
        # No belt leaving the patch crosses another: the branch and hub have no underground.
        hardware = tap["entities"] + entry["hub"]["entities"]
        self.assertFalse(any(e["name"] == "underground-belt" for e in hardware))

    def build(self, controller, resource="coal"):
        entry = controller.state["supplies"][resource]
        tap = entry["tap"]
        removed = {(p["x"], p["y"]) for p in tap["remove"]}
        self.obs["entities"] = [e for e in self.obs["entities"] if xy(e) not in removed]
        for i, e in enumerate(tap["entities"] + (entry.get("hub") or {}).get("entities", [])):
            live = {k: v for k, v in e.items() if not k.startswith("_")}
            if e is tap["splitter"]:
                live.update(mine_trunk.tap_priorities(tap))
            self.obs["entities"].append(dict(live, unit_number=900 + i))

    def test_built_tap_publishes_typed_sources(self):
        controller = self.controller()
        controller.step(self.obs, {"resource": "coal"})
        self.build(controller)
        result = controller.step(self.obs, {"resource": "coal"})
        self.assertEqual(result["status"], "succeeded", result)
        sources = json.loads((self.root / "material-sources.json").read_text())["sources"]
        self.assertEqual(set(sources), {"coal", "brick-coal"})
        self.assertEqual(sources["coal"]["nominal_shared_rate_per_minute"], 60.0)
        self.assertEqual(len(self.banks()), 1)

    def test_consumer_added_later_gets_a_second_tap_on_the_same_trunk(self):
        bricks = self.controller()
        bricks.step(self.obs, {"resource": "coal"})
        self.build(bricks)
        bricks.step(self.obs, {"resource": "coal"})
        chemistry = self.controller("chemistry")
        chemistry.step(self.obs, {"resource": "coal"})
        self.new_bank.assert_not_called()
        self.assertEqual(len(self.banks()), 1)
        first = bricks.state["supplies"]["coal"]["tap"]
        second = chemistry.state["supplies"]["coal"]["tap"]
        self.assertEqual(first["trunk"], second["trunk"])
        self.assertGreaterEqual(abs(second["along"] - first["along"]), mine_trunk.WINDOW_ROWS + 1)
        ledger = json.loads((self.root / mine_trunk.STATE_FILE).read_text())
        self.assertEqual(len(ledger["trunks"][first["trunk"]]["taps"]), 2)

    def test_without_a_tap_site_the_second_group_is_a_recorded_exception(self):
        self.obs["entities"] = [e for e in self.obs["entities"] if e["name"] != "transport-belt"]
        self.new_bank.return_value = {"ok": False, "reason": "no_site"}
        result = self.controller().step(self.obs, {"resource": "coal"})
        self.new_bank.assert_called()
        self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
