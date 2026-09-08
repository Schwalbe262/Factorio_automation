from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_energy import EnergyExpansion
from tests import test_deterministic_shared_coal as fixtures


class TransitLossTests(unittest.TestCase):
    def setUp(self):
        self.energy = object.__new__(EnergyExpansion)
        self.energy.catalog = SimpleNamespace(items={"coal": {"fuel_value": 4000000}}, entities={
            "steam-engine": {"energy_production": 15000}, "burner-mining-drill": {"energy_usage": 2500},
            "burner-inserter": {"energy_usage": 2400}})
        def row(unit, name="burner-inserter", rate=40.766867587011475):
            return {"unit_number": unit, "name": name, "coal_per_minute": rate}
        self.trunk = row(1, "transport-belt", 450)
        branch = row(3349)
        crossing = row(3358, "long-handed-inserter", 62.102425876010784)
        self.paths = {
            "0": [self.trunk, row(182)],
            "1": [self.trunk, branch, crossing, row(3303)],
            "2": [self.trunk, row(4827), row(4833, "long-handed-inserter", 62.102425876010784), row(4820)],
            "3": [self.trunk, branch, crossing, row(6817), row(6843, "long-handed-inserter", 62.102425876010784), row(6808)],
        }
        feeds = [{"bank": 0, "complete": True, "primary": i == 0, "plan": {
            "drill": {"name": "burner-mining-drill", "position": {"x": i * 3, "y": 0}}}}
            for i in range(11)]
        self.energy.state = {"banks": [{"entities": []} for _ in range(4)], "feeds": feeds,
                             "coal_links": {str(i): {} for i in (1, 2, 3)}}
        self.evidence = {"ok": True, "target_kw": 5429.88, "feeds": [
            {"remaining": 1000, "fuel": 4000000, "belt_coal": 8, "gross_coal_per_minute": 15} for _ in feeds],
            "coal_routes": {str(i): deepcopy(self.paths) for i in range(len(feeds))},
            "banks": [{"water": 200, "steam": 600, "fuel": 4000000} for _ in range(4)]}

    def test_four_banks_pay_only_downstream_losses_and_keep_real_shared_branch_cap(self):
        # Seven burners globally cost 1008 kW, but bank0's intake only feeds itself.
        # The shared bank1/3 arm still carries fuel for four distinct burners.
        before = deepcopy((self.energy.state, self.evidence))
        result = self.energy.capacity(self.evidence)
        self.assertEqual(result["fuel_backed_kw"][:3], [1800, 1800, 1800])
        shared_net = 40.766867587011475 * 4000000 / 60000 - 4 * 144
        self.assertAlmostEqual(result["fuel_backed_kw"][3], shared_net - 1800)
        self.assertAlmostEqual(result["total_kw"], 5741.791172467432)
        self.assertGreater(result["total_kw"], result["target_kw"])
        self.assertEqual((self.energy.state, self.evidence), before)
        self.energy.state = json.loads(json.dumps(self.energy.state))
        reloaded = json.loads(json.dumps(self.evidence))
        for paths in reloaded["coal_routes"].values():
            reordered = dict(reversed(list(paths.items())))
            paths.clear(); paths.update(reordered)
        self.assertEqual(self.energy.capacity(reloaded), result)

    def test_source_fuel_loss_still_paid_once_and_new_feed_does_not_mint_transit_capacity(self):
        for i in range(8, 11):
            self.evidence["feeds"][i]["remaining"] = 0
        self.assertAlmostEqual(self.energy.capacity(self.evidence)["total_kw"], 8 * 706 - 7 * 144)
        for row in self.evidence["feeds"]:
            row["remaining"] = 1000
        original = self.energy.capacity(self.evidence)
        self.energy.state["feeds"].append(deepcopy(self.energy.state["feeds"][-1]))
        self.energy.state["feeds"][-1]["plan"]["drill"]["position"]["x"] = 999
        self.evidence["feeds"].append(deepcopy(self.evidence["feeds"][-1]))
        self.evidence["coal_routes"]["11"] = deepcopy(self.paths)
        self.assertEqual(self.energy.capacity(self.evidence), original)

    def test_shared_upstream_edge_reserves_all_branch_losses_once(self):
        for paths in self.evidence["coal_routes"].values():
            for path in paths.values():
                path[0]["coal_per_minute"] = 90
        # 6000 kW crosses this trunk, including all seven downstream burners.
        self.assertAlmostEqual(self.energy.capacity(self.evidence)["total_kw"], 6000 - 7 * 144)

    def test_downstream_source_merge_does_not_pay_unrelated_branch_losses(self):
        self.energy.state["banks"] = self.energy.state["banks"][:2]
        self.energy.state["feeds"] = self.energy.state["feeds"][:3]
        self.energy.state["coal_links"] = {"1": {}}
        self.evidence["feeds"] = self.evidence["feeds"][:3]
        choke = {**self.trunk, "coal_per_minute": 10}
        self.evidence["coal_routes"] = {
            "0": {"0": [choke, self.paths["0"][-1]]},
            "1": {"0": [{**self.trunk, "unit_number": 2}, self.paths["0"][-1]]},
            "2": {"1": [{**self.trunk, "unit_number": 3}, self.paths["1"][-1]]},
        }
        result = self.energy.capacity(self.evidence)
        # Source1 joins after source0's choke. Bank1's separate burner neither
        # uses that choke nor increases the fuel it must carry for bank0.
        self.assertAlmostEqual(result["fuel_backed_kw"][0], 10 * 4000000 / 60000 - 144 + 706)
        self.assertEqual(result["fuel_backed_kw"][1], 706 - 144)

    def test_healthy_completed_energy_returns_to_production_without_another_feed(self):
        self.energy._sync = Mock(return_value=True)
        self.energy._ensure_bank = Mock(return_value=None)
        self.energy._ensure_feed = Mock(return_value=None)
        self.energy._managed = Mock()
        self.energy.evidence = Mock(return_value=self.evidence)
        self.energy.factory = SimpleNamespace(register_plan=Mock(return_value={"ok": True}))
        self.energy._save = Mock()
        self.energy._reserve_feed = Mock()
        self.energy._reserve_bank = Mock()
        self.assertIsNone(self.energy.next_action({"world_id": "coal-test", "tick": 100}))
        self.energy._reserve_feed.assert_not_called()
        self.energy._reserve_bank.assert_not_called()
        self.assertAlmostEqual(self.energy.state["last_evidence"]["total_kw"], 5741.791172467432)

    def test_real_owned_route_emitter_orders_branch_before_boiler_intake(self):
        fixtures.SharedCoalTests.setUp(self)
        routes = self.energy._coal_routes(self.obs)
        for paths in routes.values():
            path = paths["1"]
            self.assertEqual(path[0]["name"], "transport-belt")
            self.assertLess(path.index(self.branch), path.index(self.crossing))
            self.assertEqual(path[-1]["name"], "burner-inserter")
            self.assertEqual(path[-1]["boiler_unit"], next(e["unit_number"] for e in self.banks[1]["entities"] if e["name"] == "boiler"))


if __name__ == "__main__":
    unittest.main()
