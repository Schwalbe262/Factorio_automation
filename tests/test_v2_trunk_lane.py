"""Second trunk lane with a 2-2 balanced splitter, trunk traces through taps, no double-counted banks."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import mine_trunk
from factorio_agent.bus_factory import trace_corridor
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.outposts import MiningOutposts
from factorio_agent.paired_mining import mining_pair
from factorio_agent.transport import xy
from factorio_agent.trunk_lane import TrunkLane

RUN = 24


def patch_bank():
    plan = mining_pair(-40.5, -40.5, 1, "coal")
    return {
        "ok": True,
        "ready": True,
        "plan": plan,
        "reserved_area": [[-42, -42], [-37, -35]],
        "nominal_rate_per_minute": 60.0,
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


class LaneGeometry(unittest.TestCase):
    def test_the_second_lane_lies_away_from_the_taps_with_a_balanced_splitter(self):
        bank = patch_bank()
        obs = {"world_id": "w", "entities": trunk_belts(bank)}
        tap = mine_trunk.plan_tap(bank, obs, owner="a", name="a:coal", demand=30, sides=("right",))["taps"][0]
        planned = mine_trunk.lane_plan(bank, obs, stations=[tap])
        self.assertTrue(planned["ok"], planned)
        lane = planned["lane"]
        port_y = bank["plan"]["ports"][0]["position"]["y"]
        # Flow east: taps on the right (south), the lane on the left (north), beside the trunk.
        self.assertEqual(lane["tap_side"], "right")
        belts = [e for e in lane["entities"] if e["name"] == "transport-belt"]
        self.assertTrue(belts)
        self.assertTrue(all(e["position"]["y"] == port_y - 1 and e["direction"] == 4 for e in belts))
        splitter = lane["splitter"]
        self.assertEqual((splitter["position"]["y"], splitter["direction"]), (port_y - 0.5, 4))
        self.assertEqual(
            mine_trunk.tap_priorities(lane), {"splitter_input_priority": "none", "splitter_output_priority": "none"}
        )
        self.assertGreaterEqual(abs(lane["along"] - tap["along"]), mine_trunk.WINDOW_ROWS + 1)
        self.assertEqual(lane["remove"], [{"x": splitter["position"]["x"], "y": port_y}])
        self.assertEqual(lane["port"]["position"]["y"], port_y - 1)  # the head where a second row lane joins
        self.assertEqual(lane["balancer"]["kind"], "splitter_2_2")

    def test_taps_on_both_sides_leave_no_lane_and_a_built_lane_keeps_taps_on_the_near_side(self):
        bank = patch_bank()
        obs = {"world_id": "w", "entities": trunk_belts(bank)}
        right = mine_trunk.plan_tap(bank, obs, owner="a", name="a", demand=1, sides=("right",))["taps"][0]
        left = dict(right, side="left", along=right["along"] + 10)
        self.assertEqual(
            mine_trunk.lane_plan(bank, obs, stations=[right, left])["reason"], "mine_trunk_lane_both_sides_tapped"
        )
        lane = mine_trunk.lane_plan(bank, obs, stations=[right])["lane"]
        later = mine_trunk.plan_tap(bank, obs, owner="b", name="b", demand=1, stations=[right, lane])
        self.assertTrue(later["ok"])
        self.assertEqual({t["side"] for t in later["taps"]}, {"right"})

    def test_more_than_two_lanes_stay_reserved_and_recorded(self):
        self.assertEqual(mine_trunk.balancer_plan(3)["kind"], "reserved_n_n")
        self.assertFalse(mine_trunk.balancer_plan(3)["implemented"])


class LaneBuild(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = patch_bank()
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": "w", "banks": [self.bank]}))
        belts = trunk_belts(self.bank)
        district = {"ok": True, "entities": [{k: e[k] for k in ("name", "position", "direction")} for e in belts]}
        (self.root / "smelting-district.json").write_text(json.dumps({"world_id": "w", "plan": district}))
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {"smelting_district": district}}
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            _occupied_by_plan=geometry._occupied_by_plan,
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={"status": "succeeded"})),
        )
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.obs = {"world_id": "w", "tick": 10, "inventory": {"splitter": 1, "transport-belt": 30}, "entities": belts}
        self.smelting = SimpleNamespace(state={"stale": True})
        patch("factorio_agent.planning.preflight", return_value={"ok": True, "clear_obstacles": []}).start()
        self.addCleanup(patch.stopall)

    def test_the_lane_is_recorded_and_released_before_the_one_trunk_belt_is_mined(self):
        controller = TrunkLane(self.game, self.builder, None, smelting=self.smelting)
        action = controller.step(self.obs, {"resource": "coal"})
        ledger = json.loads((self.root / mine_trunk.STATE_FILE).read_text())
        lane = ledger["trunks"][mine_trunk.trunk_key(self.bank)]["lanes"]["1"]
        self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", lane["remove_units"][0]))
        district = json.loads((self.root / "smelting-district.json").read_text())["plan"]
        self.assertNotIn(xy({"position": lane["remove"][0]}), {xy(e) for e in district["entities"]})
        self.assertEqual(self.smelting.state["plan"], district)
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertIn("mine_trunk_lane:" + lane["trunk"], board["blocks"])
        # Built: the receipt counts two lanes; a lane node is no longer proposed.
        removed = {xy({"position": p}) for p in lane["remove"]}
        self.obs["entities"] = [e for e in self.obs["entities"] if xy(e) not in removed]
        for i, e in enumerate(lane["entities"]):
            self.obs["entities"].append(
                dict({k: v for k, v in e.items() if not k.startswith("_")}, unit_number=900 + i)
            )
        result = controller.step(self.obs, {"resource": "coal"})
        self.assertEqual(result["status"], "succeeded", result)
        trunk = json.loads((self.root / mine_trunk.STATE_FILE).read_text())["trunks"][lane["trunk"]]
        self.assertEqual(mine_trunk.lanes_built(trunk), 2)
        head = mine_trunk.lane_head(self.root, self.obs, self.bank)
        self.assertEqual(head["destination"], lane["port"])
        self.assertEqual(head["target"]["unit_number"], 901)
        # The trunk trace of the district plan passes the lane splitter (and any tap splitter).
        source = self.bank["plan"]["ports"][0]
        end = district["entities"][-1]
        self.assertFalse(trace_corridor(district, source, end)["ok"])
        traced = trace_corridor(
            district, source, end, passthrough=mine_trunk.tap_passthrough(self.root, "w", self.bank)
        )
        self.assertTrue(traced["ok"], traced)
        self.assertIn("splitter", {e["name"] for e in traced["entities"]})

    def test_the_receipt_asks_for_the_lane_once(self):
        tap = {
            "name": "big:coal",
            "trunk": mine_trunk.trunk_key(self.bank),
            "demand_per_minute": 900,
            "primary_demand": 60,
        }
        mine_trunk.record_tap(self.root, "w", tap)
        receipt = mine_trunk.trunk_receipt(self.root, [], {"world_id": "w"})
        row = receipt["trunks"][0]
        self.assertEqual((row["lanes_needed"], row["lanes_built"], row["second_lane_required"]), (2, 1, True))
        nodes = mine_trunk.mine_trunk_nodes([], [], {"mine_trunks": receipt}, CAPABILITIES)
        lane = [n for n in nodes if n["capability"] == mine_trunk.LANE_CAPABILITY]
        self.assertEqual([n["parameters"] for n in lane], [{"resource": "coal"}])
        again = [{"capability": mine_trunk.LANE_CAPABILITY, "parameters": {"resource": "coal"}, "status": "blocked"}]
        nodes = mine_trunk.mine_trunk_nodes(again, [], {"mine_trunks": receipt}, CAPABILITIES)
        self.assertFalse([n for n in nodes if n["capability"] == mine_trunk.LANE_CAPABILITY])


class OutpostRateCheck(unittest.TestCase):
    def test_dedicated_material_banks_do_not_count_toward_the_outpost_target(self):
        outposts = MiningOutposts.__new__(MiningOutposts)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        outposts.path = Path(temp.name) / "mining-outposts.json"
        material = dict(patch_bank(), material_supply_id="dedicated-material:w:coal")
        outposts.state = {"world_id": "w", "banks": [material]}
        outposts.bank_proof = Mock(return_value={"nominal_rate_per_minute": 60.0})
        self.assertIsNone(outposts.proof({"tick": 1}, {"resource": "coal", "rate_per_minute": 60}))
        outposts.state["banks"].append(deepcopy(patch_bank()))
        proof = outposts.proof({"tick": 1}, {"resource": "coal", "rate_per_minute": 60})
        self.assertEqual(proof["nominal_rate_per_minute"], 60.0)


if __name__ == "__main__":
    unittest.main()
