import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.outposts import MiningOutposts, mining_bank, validate_parameters


class AlignedMiningBankTests(unittest.TestCase):
    def test_collector_is_outside_drill_footprints_and_power_poles_do_not_overlap(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        for count in (1, 2, 8):
            p = mining_bank(0.5, 0.5, count, "iron-ore")
            footprints = [geometry._occupied_by_plan([e]) for e in p["entities"]]
            self.assertEqual(sum(map(len, footprints)), len(set().union(*footprints)))
            self.assertEqual(p["required_items"]["electric-mining-drill"], count)
            self.assertEqual(p["ports"][0]["facing"], 4)

    def test_bad_resource_and_nonfinite_rate_rejected_before_game_access(self):
        for p in (
            {"resource": "uranium-ore", "rate_per_minute": 30},
            {"resource": "iron-ore", "rate_per_minute": float("nan")},
            {"resource": "iron-ore", "rate_per_minute": 0},
        ):
            with self.assertRaises(ValueError):
                validate_parameters(p)

    def test_upstream_extension_preserves_existing_entities_and_fixed_output_port(self):
        old = mining_bank(12.5, -64.5, 2, "iron-ore")
        extended = mining_bank(8.5, -64.5, 3, "iron-ore")
        self.assertEqual(old["ports"], extended["ports"])
        for entity in old["entities"]:
            self.assertIn(entity, extended["entities"])

    def test_power_loss_does_not_trigger_duplicate_mining_bank(self):
        outpost = MiningOutposts.__new__(MiningOutposts)
        plan = mining_bank(0.5, 0.5, 1, "iron-ore")
        outpost.state = {"world_id": "w", "banks": [{"plan": plan, "ready": True, "nominal_rate_per_minute": 30}]}
        obs = {
            "world_id": "w",
            "tick": 400,
            "technologies": {"electric-mining-drill": True},
            "entities": [
                dict(e, unit_number=i + 1, energy=0, electric_network_connected=False)
                for i, e in enumerate(plan["entities"])
            ],
        }
        result = outpost.step(obs, {"resource": "iron-ore", "rate_per_minute": 60})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(len(outpost.state["banks"]), 1)

    def test_depleted_drill_preserves_bank_health_but_reduces_available_capacity(self):
        outpost = MiningOutposts.__new__(MiningOutposts)
        plan = mining_bank(0.5, 0.5, 2, "iron-ore")
        bank = {"plan": plan, "nominal_rate_per_minute": 60}
        obs = {
            "world_id": "w",
            "tick": 400,
            "entities": [
                dict(e, unit_number=i + 1, energy=100, electric_network_connected=True, belt_inventory={"iron-ore": 4})
                for i, e in enumerate(plan["entities"])
            ],
        }
        drills = [e for e in obs["entities"] if e["name"] == "electric-mining-drill"]
        drills[0]["status_name"] = "no_minable_resources"
        with patch("factorio_agent.outposts.underground_proof", return_value=True):
            proof = outpost.bank_proof(obs, bank)
            self.assertEqual(proof["nominal_rate_per_minute"], 30)
            self.assertEqual(proof["installed_nominal_rate_per_minute"], 60)
            self.assertEqual(proof["exhausted_drill_units"], [drills[0]["unit_number"]])
            self.assertEqual(bank["nominal_rate_per_minute"], 60)
            drills[1]["status_name"] = "no_mineable_resources"
            self.assertEqual(outpost.bank_proof(obs, bank)["nominal_rate_per_minute"], 0)
            drills[0]["electric_network_connected"] = False
            self.assertIsNone(outpost.bank_proof(obs, bank))

    def test_capacity_goal_uses_remaining_rate_and_requests_expansion_after_depletion(self):
        outpost = MiningOutposts.__new__(MiningOutposts)
        plan = mining_bank(0.5, 0.5, 2, "iron-ore")
        bank = {"ready": True, "plan": plan, "nominal_rate_per_minute": 60}
        outpost.state = {"world_id": "w", "banks": [bank]}
        outpost.publish_reservations = Mock(return_value=None)
        outpost.bank_proof = Mock(return_value={"nominal_rate_per_minute": 30, "installed_nominal_rate_per_minute": 60})
        outpost.prepare_extension = Mock(return_value={"ok": False, "reason": "local ore exhausted"})
        outpost.prepare_connected_bank = Mock(return_value={"ok": False, "reason": "no safe additional bank"})
        obs = {
            "world_id": "w",
            "technologies": {"electric-mining-drill": True},
            "entities": [dict(e, unit_number=i + 1) for i, e in enumerate(plan["entities"])],
        }
        result = outpost.step(obs, {"resource": "iron-ore", "rate_per_minute": 60})
        self.assertEqual(result["status"], "blocked")
        outpost.prepare_extension.assert_called_once_with(obs, bank, 1)
        outpost.prepare_connected_bank.assert_called_once_with(obs, bank, 1)
        self.assertEqual(len(outpost.state["banks"]), 1)


if __name__ == "__main__":
    unittest.main()
