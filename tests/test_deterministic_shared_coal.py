from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_energy import EnergyExpansion


class SharedCoalTests(unittest.TestCase):
    def setUp(self):
        self.energy = object.__new__(EnergyExpansion)
        self.energy.catalog = SimpleNamespace(items={"coal": {"fuel_value": 4000000}}, entities={
            "steam-engine": {"energy_production": 15000}, "burner-mining-drill": {"energy_usage": 2500},
            "burner-inserter": {"energy_usage": 2400}})
        self.energy.game = SimpleNamespace(query=Mock())
        self.rows = []
        def entity(name, x, y, direction=0, **extra):
            row = {"name": name, "position": {"x": x, "y": y}, "direction": direction,
                   "unit_number": len(self.rows) + 1, "coal_per_minute": 450 if name == "transport-belt" else 40, **extra}
            self.rows.append(row)
            return row
        trunk = [entity("transport-belt", x + .5, .5, 4) for x in range(5)]
        branch = entity("burner-inserter", 1.5, 1.5, 0,
                        pickup_position={"x": 1.5, "y": .5}, drop_position={"x": 1.5, "y": 2.69921875})
        first = entity("transport-belt", 1.5, 2.5, 4)
        crossing = entity("long-handed-inserter", 1.5, 4.5, 0,
                          pickup_position={"x": 1.5, "y": 2.5}, drop_position={"x": 1.5, "y": 6.69921875})
        last = entity("transport-belt", 1.5, 6.5, 4)
        self.branch, self.crossing = branch, crossing
        self.banks = []
        for index, (x, y) in enumerate(((4.5, .5), (1.5, 6.5))):
            arm = entity("burner-inserter", x, y + 1, 0,
                         pickup_position={"x": x, "y": y}, drop_position={"x": x, "y": y + 2.2})
            boiler = entity("boiler", x, y + 2.5)
            arm["boiler_unit"] = boiler["unit_number"]
            engines = [entity("steam-engine", 20.5 + index * 10, 10.5 + i * 4) for i in range(2)]
            pole = entity("small-electric-pole", 20.5 + index * 10, 20.5)
            self.banks.append({"entities": [arm, pole, boiler] + engines + ([trunk[-1]] if index == 0 else [last]),
                "ports": [{"kind": "item", "item": "coal", "direction": "input", "facing": 4,
                           "position": {"x": x, "y": y}}]})
        feeds = []
        for i in range(4):
            drill = entity("burner-mining-drill", -10 - i * 4, -10, 4, drop_position=trunk[0]["position"])
            feeds.append({"bank": 0, "complete": True, "plan": {"drill": drill, "entities": [drill] + trunk,
                "ports": [{"kind": "item", "item": "coal", "direction": "output", "facing": 4,
                           "position": trunk[0]["position"]}]}})
        self.energy.state = {"world_id": "coal-test", "banks": self.banks, "feeds": feeds,
            "coal_links": {"1": {"entities": [branch, first, crossing, last],
                "source_port": {"position": trunk[1]["position"], "unit_number": trunk[1]["unit_number"], "facing": 4}}}}
        self.obs = {"world_id": "coal-test", "tick": 100, "entities": deepcopy(self.rows)}
        self.energy.game.query.side_effect = lambda body: {"ok": True, "world_id": "coal-test", "rows": deepcopy(self.rows)}
        self.evidence = {"target_kw": 1857.24, "feeds": [
            {"remaining": 1000, "fuel": 4000000, "gross_coal_per_minute": 15} for _ in feeds]}

    def capacity(self, count=3):
        self.evidence["coal_routes"] = self.energy._coal_routes(self.obs)
        self.evidence["feeds"][3]["remaining"] = 1000 if count == 4 else 0
        return self.energy.capacity(self.evidence)

    def test_three_active_feeds_count_once_and_pay_both_boilers_and_branch(self):
        result = self.capacity()
        self.assertEqual(result["total_kw"], 1686)
        self.assertEqual(result["fuel_backed_kw"], [1686, 0])
        self.assertEqual(len(self.evidence["coal_routes"]["2"]), 2)

    def test_fourth_shared_feed_fills_existing_banks_with_real_net_budget(self):
        result = self.capacity(4)
        self.assertEqual(result["total_kw"], 2392)
        self.assertEqual(result["fuel_backed_kw"], [1800, 592])

    def test_noncentral_real_arm_drop_is_resolved_to_receiving_belt(self):
        routes = self.energy._coal_routes(self.obs)
        names = [e["name"] for e in routes["0"]["1"]]
        self.assertEqual(names.count("burner-inserter"), 2)
        self.assertEqual(names.count("long-handed-inserter"), 1)

    def test_stale_branch_source_identity_cannot_back_other_bank(self):
        self.energy.state["coal_links"]["1"]["source_port"]["unit_number"] = 9000
        routes = self.energy._coal_routes(self.obs)
        self.assertTrue(routes)
        self.assertTrue(all("1" not in banks for banks in routes.values()))

    def test_missing_unpowered_or_foreign_arm_fails_closed_when_live_proof_excludes_it(self):
        self.rows.remove(self.crossing)
        routes = self.energy._coal_routes(self.obs)
        self.assertTrue(all("1" not in banks for banks in routes.values()))

    def test_missing_engine_on_bank_invalidates_its_routes(self):
        self.rows.remove(next(e for e in self.banks[1]["entities"] if e["name"] == "steam-engine"))
        self.assertTrue(all("1" not in banks for banks in self.energy._coal_routes(self.obs).values()))

    def test_world_or_live_identity_proof_failure_never_becomes_capacity(self):
        self.obs["world_id"] = "other"
        self.assertIsNone(self.energy._coal_routes(self.obs))
        self.energy.game.query.assert_not_called()
        self.obs["world_id"] = "coal-test"
        self.energy.game.query.side_effect = lambda body: {"ok": False}
        self.assertIsNone(self.energy._coal_routes(self.obs))

    def test_disconnected_bank_losses_can_only_be_paid_by_feeds_reaching_them(self):
        routes = self.energy._coal_routes(self.obs)
        self.evidence["coal_routes"] = {"0": {"0": routes["0"]["0"]}, "1": {"1": routes["1"]["1"]}}
        result = self.energy.capacity(self.evidence)
        self.assertEqual(result["fuel_backed_kw"], [562, 418])

    def test_failed_live_query_blocks_instead_of_requesting_unjustified_new_capacity(self):
        self.energy.game.query.side_effect = [
            {"ok": True, "consumers": [], "demand_kw": 100}, {"ok": False}]
        evidence = self.energy.evidence(self.obs)
        self.assertFalse(evidence["ok"])
        self.assertIn("proof is unavailable", evidence["reason"])

    def test_owned_boiler_must_receive_actual_intake_drop(self):
        next(e for e in self.banks[1]["entities"] if e["name"] == "burner-inserter")["boiler_unit"] = 9999
        self.assertTrue(all("1" not in banks for banks in self.energy._coal_routes(self.obs).values()))

    def test_live_belt_neighbours_override_apparent_tile_adjacency(self):
        self.rows[0]["belt_outputs"] = []
        self.assertEqual(self.energy._coal_routes(self.obs), {})

    def test_drill_must_physically_output_to_owned_coal_conveyor(self):
        for row in self.rows:
            if row["name"] == "burner-mining-drill":
                row["drop_position"] = {"x": 100.5, "y": 100.5}
        self.assertEqual(self.energy._coal_routes(self.obs), {})

    def test_unlinked_legacy_bank_retains_its_dedicated_capacity(self):
        self.energy.state["banks"].append({"entities": [], "ports": []})
        self.energy.state["feeds"][3]["bank"] = 2
        result = self.capacity(4)
        self.assertEqual(result["fuel_backed_kw"], [1686, 0, 418])

    def test_duplicate_drill_checkpoint_does_not_duplicate_coal(self):
        self.energy.state["feeds"][1]["plan"]["drill"] = self.energy.state["feeds"][0]["plan"]["drill"]
        self.assertEqual(self.capacity()["total_kw"], 980)

    def test_shared_belt_throughput_is_spent_once_across_all_sources_and_banks(self):
        next(e for e in self.rows if e["name"] == "transport-belt")["coal_per_minute"] = 15
        self.assertEqual(self.capacity(4)["total_kw"], 568)

    def test_link_and_powered_arm_tail_can_be_inherited_by_new_requested_bank_feed(self):
        self.energy._coal_routes(self.obs)
        intact, intakes = self.energy._coal_transit(self.energy._coal_observation, 1)
        path = self.energy._coal_tail({"x": .5, "y": .5}, intact, intakes)
        self.assertIsNotNone(path)
        self.assertIn(self.crossing, path)
        self.assertEqual(path[-1]["position"], {"x": 1.5, "y": 6.5})
        self.assertTrue(all("_coal_transfers" not in row for row in path))


if __name__ == "__main__":
    unittest.main()
