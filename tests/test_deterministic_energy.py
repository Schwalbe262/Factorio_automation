from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_energy import EnergyExpansion
from factorio_ai.factory_templates import build_template


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.bootstrap, self.builder, self.factory = Mock(), Mock(), Mock()
        self.builder.state = {"world_id": "energy-test", "seeds": {}, "power_sample_tick": 10,
                              "power_plan": build_template("steam_bank"),
                              "coal_plan": FactoryBuilder._coal_plan({"x": -20, "y": 0})}
        self.factory.state = {}
        self.factory.register_plan.side_effect = lambda key, plan, obs: plan
        self.factory._entity_key.side_effect = lambda e: e["name"] + str(e["position"])
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.builder._seed.return_value = None
        self.catalog = SimpleNamespace(items={"coal": {"fuel_value": 4000000}}, entities={
            "steam-engine": {"energy_production": 15000}, "burner-mining-drill": {"energy_usage": 2500},
            "burner-inserter": {"energy_usage": 2400}, "chemical-plant": {"electric": True, "energy_usage": 3500}})
        self.energy = EnergyExpansion(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        self.obs = {"world_id": "energy-test", "tick": 100, "entities": []}
        self.raw_evidence = {"ok": True, "demand_kw": 300, "generation_kw": 300, "connected_engines": 2,
                             "consumers": [{"name": "electric-energy-interface", "nominal_kw": 300}],
                             "feeds": [{"remaining": 1000, "fuel": 8000000, "belt_coal": 8, "gross_coal_per_minute": 15}],
                             "banks": [{"water": 200, "steam": 400, "fuel": 8000000}]}
        self.game.query.side_effect = lambda body: deepcopy(self.raw_evidence)

    def test_waits_for_established_starter_before_owning_energy(self):
        self.builder.state.pop("power_sample_tick")
        self.assertIsNone(self.energy.next_action(self.obs))
        self.builder.ensure_plan.assert_not_called()

    def test_cold_restart_after_verified_power_can_initialize_repair(self):
        self.builder.state.pop("power_sample_tick")
        self.builder.state["power_verified_once"] = True
        self.raw_evidence["feeds"][0]["remaining"] = 0
        self.energy._reserve_feed = Mock(return_value={"status": "waiting", "reason": "replace depleted coal"})
        self.assertEqual(self.energy.next_action(self.obs)["reason"], "replace depleted coal")
        self.energy._reserve_feed.assert_called_once()

    def test_healthy_small_factory_yields_without_extra_construction_or_fuel(self):
        self.assertIsNone(self.energy.next_action(self.obs))
        self.assertEqual(self.builder.state["seeds"]["energy:feed:0"], {"observed": True, "attempts": 0})
        self.assertAlmostEqual(self.energy.state["last_evidence"]["total_kw"], 562)
        self.bootstrap.ensure_item.assert_not_called()

    def test_live_prototype_capacity_accounts_for_burner_self_consumption(self):
        self.energy._sync(self.obs)
        evidence = self.energy.evidence(self.obs)
        expected = [562, 1268, 1800]
        for count in (1, 2, 3):
            self.energy.state["feeds"] = [deepcopy(self.energy.state["feeds"][0]) for _ in range(count)]
            evidence["feeds"] = [deepcopy(evidence["feeds"][0]) for _ in range(count)]
            self.assertAlmostEqual(self.energy.capacity(evidence)["total_kw"], expected[count-1])

    def test_starved_or_depleted_drill_does_not_count_as_power_capacity(self):
        self.energy._sync(self.obs)
        evidence = self.energy.evidence(self.obs)
        for field in ("remaining", "fuel"):
            broken = deepcopy(evidence);broken["feeds"][0][field] = 0
            self.assertEqual(self.energy.capacity(broken)["total_kw"], 0)

    def test_coal_expands_before_another_bank_when_existing_engines_can_cover_demand(self):
        self.raw_evidence["consumers"][0]["nominal_kw"] = 1200
        self.energy._reserve_feed = Mock(return_value={"status": "waiting", "reason": "new feed"})
        self.energy._reserve_bank = Mock()
        self.assertEqual(self.energy.next_action(self.obs)["reason"], "new feed")
        self.energy._reserve_feed.assert_called_once_with(self.obs, 0)
        self.energy._reserve_bank.assert_not_called()

    def test_new_bank_is_needed_only_beyond_existing_fueled_engine_capacity(self):
        self.energy._sync(self.obs)
        self.energy.state["feeds"] *= 3
        self.raw_evidence["feeds"] *= 3
        self.raw_evidence["consumers"][0]["nominal_kw"] = 2000
        self.energy._reserve_feed = Mock()
        self.energy._reserve_bank = Mock(return_value={"status": "waiting", "reason": "new bank"})
        self.assertEqual(self.energy.next_action(self.obs)["reason"], "new bank")
        self.energy._reserve_bank.assert_called_once_with(self.obs)
        self.energy._reserve_feed.assert_not_called()

    def test_primary_depletion_requests_replacement_instead_of_reseeding_old_drill(self):
        self.raw_evidence["feeds"][0]["remaining"] = 0
        self.energy._reserve_feed = Mock(return_value={"status": "waiting", "reason": "replacement"})
        self.assertEqual(self.energy.next_action(self.obs)["reason"], "replacement")
        self.energy._reserve_feed.assert_called_once_with(self.obs, 0, replacement=True)
        self.builder._seed.assert_not_called()

    def test_completed_replacement_becomes_primary_only_after_real_fuel_and_belt_coal(self):
        self.energy._sync(self.obs)
        plan = FactoryBuilder._coal_plan({"x": -40, "y": 0})
        feed = {"plan": plan, "bank": 0, "primary": False, "replacement": True, "complete": False}
        self.energy.state["feeds"].append(feed)
        empty = {"remaining": 1000, "fuel": 8000000, "belt_coal": 0}
        self.assertEqual(self.energy._ensure_feed(self.obs, 1, empty)["status"], "waiting")
        self.assertNotEqual(self.builder.state["coal_plan"]["drill"]["position"], plan["drill"]["position"])
        self.assertIsNone(self.energy._ensure_feed(self.obs, 1, {**empty, "belt_coal": 2}))
        self.assertEqual(self.builder.state["coal_plan"]["drill"]["position"], plan["drill"]["position"])
        self.assertTrue(feed["primary"])
        self.assertFalse(self.energy.state["feeds"][0]["primary"])

    def test_missing_existing_entity_is_rebuilt_before_claiming_energy_capacity(self):
        self.builder.ensure_plan.return_value = {"type": "build", "name": "transport-belt"}
        self.assertEqual(self.energy.next_action(self.obs)["type"], "build")
        self.game.query.assert_not_called()

    def test_new_bank_cannot_claim_connection_from_its_own_isolated_generators(self):
        self.energy._sync(self.obs)
        self.energy.state["banks"].append(build_template("steam_bank", anchor={"x": 20.5, "y": .5}))
        poles = [next(e for e in plan["entities"] if e["name"] == "small-electric-pole") for plan in self.energy.state["banks"]]
        self.obs["entities"] = [{**poles[0], "electric_network_id": 1}, {**poles[1], "electric_network_id": 2}]
        self.factory._power_route.return_value = {"ok": True, "path": [e["position"] for e in poles]}
        self.assertEqual(self.energy._connect_bank_power(self.obs, 1)["status"], "waiting")
        self.factory._power_route.assert_called_once()
        self.obs["entities"][1]["electric_network_id"] = 1
        self.assertEqual(self.energy._connect_bank_power(self.obs, 1)["status"], "succeeded")

    def test_nominal_connected_demand_survives_low_actual_consumption(self):
        self.energy._sync(self.obs)
        self.raw_evidence["demand_kw"] = 10
        self.raw_evidence["consumers"] = [{"name": "chemical-plant", "nominal_kw": 0}]
        evidence = self.energy.evidence(self.obs)
        self.assertEqual(evidence["nominal_demand_kw"], 210)
        self.assertEqual(evidence["target_kw"], 252)

    def test_configurable_interface_uses_live_load_instead_of_infinite_prototype_limit(self):
        self.energy._sync(self.obs)
        self.catalog.entities["electric-energy-interface"] = {"electric": True, "energy_usage": float("inf")}
        self.assertEqual(self.energy.evidence(self.obs)["target_kw"], 360)

    def test_rollback_invalidates_capacity_proof_but_keeps_observed_seed_guard(self):
        self.energy.next_action(self.obs)
        self.builder.state["seeds"] = {}  # builder clears transient seed state on rollback
        self.energy._sync({**self.obs, "tick": 50})
        self.assertNotIn("last_evidence", self.energy.state)
        self.assertFalse(self.energy.state["feeds"][0]["complete"])
        self.assertTrue(self.builder.state["seeds"]["energy:feed:0"]["observed"])
        cold = {"remaining": 1000, "fuel": 0, "belt_coal": 0}
        self.builder._seed.reset_mock()
        self.assertIsNone(self.energy._ensure_feed(self.obs, 0, cold))
        self.builder._seed.assert_not_called()
        self.assertTrue(self.energy.state["feeds"][0]["retired"])

    def test_catalog_change_reobserves_existing_assets_before_counting_capacity(self):
        self.energy.next_action(self.obs)
        self.catalog.fingerprint = "changed-structural-catalog"
        resumed = EnergyExpansion(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        resumed._sync(self.obs)
        self.assertEqual(resumed.state["catalog_fingerprint"], self.catalog.fingerprint)
        self.assertNotIn("last_evidence", resumed.state)
        self.assertFalse(resumed.state["feeds"][0]["complete"])

    def test_new_bank_reserves_coal_approach_before_routing_water(self):
        self.energy._sync(self.obs)
        self.energy.state["banks"].append(build_template("steam_bank", anchor={"x": 20.5, "y": .5}))
        self.energy._connect_bank_power = Mock(return_value={"status": "succeeded"})
        self.energy._connect_bank_coal = Mock(return_value={"type": "build", "name": "burner-inserter"})
        result = self.energy._ensure_bank(self.obs, 1)
        self.assertEqual(result["type"], "build")
        self.factory.fluids.ensure_source.assert_not_called()

    def depleted_trunk(self):
        self.energy._sync(self.obs)
        feed = self.energy.state["feeds"][0]
        trunk = [{"name": "transport-belt", "position": {"x": x + .5, "y": 3.5}, "direction": 4}
                 for x in range(-17, -3)]
        feed["plan"]["entities"] = [feed["plan"]["drill"]] + trunk
        feed["depleted"] = True
        self.obs["entities"] = deepcopy(feed["plan"]["entities"] + self.energy.state["banks"][0]["entities"])
        return trunk

    def reserve_replacement_at_trunk(self):
        trunk = self.depleted_trunk()
        self.factory._port_clearances.return_value = set()
        self.factory._reserved.return_value = self.obs["entities"]
        self.builder._occupied_by_plan.return_value = set()
        site = {"x": -20, "y": 8}
        self.builder.coal_sites.return_value = [site]
        self.builder._coal_plan.side_effect = FactoryBuilder._coal_plan
        self.builder.can_place.return_value = {"ok": True}
        self.game.query.side_effect = lambda body: {"ok": True, "sites": [{"position": site, "remaining": 5000}]}
        self.builder.route.side_effect = lambda start, end, *args, **kwargs: {
            "ok": True, "segments": [{"position": start, "direction": 0}, {"position": end, "direction": 4}]}
        result = self.energy._reserve_feed(self.obs, 0, replacement=True)
        self.assertEqual(result["status"], "waiting")
        return trunk, self.energy.state["feeds"][1]

    def test_depleted_trunk_reuse_reserves_downstream_repair_without_old_drill(self):
        trunk, replacement = self.reserve_replacement_at_trunk()
        self.assertEqual(self.builder.route.call_args.args[1], trunk[0]["position"])
        for row in trunk:
            self.assertIn(row, replacement["plan"]["entities"])
        old_drill = self.energy.state["feeds"][0]["plan"]["drill"]
        self.assertNotIn(old_drill, replacement["plan"]["entities"])
        self.assertTrue(replacement["replacement"])
        self.assertEqual(replacement["plan"]["entities"][-1], trunk[-1])

    def test_live_replacement_repairs_inherited_belt_and_excludes_exhausted_drill_capacity(self):
        trunk, replacement = self.reserve_replacement_at_trunk()
        replacement.update(seeded=True, complete=True)
        missing = trunk[6]
        self.obs["entities"] = [deepcopy(row) for row in replacement["plan"]["entities"] if row != missing]
        self.obs["inventory"] = {"transport-belt": 1}
        self.game.backend = "assisted"
        real_builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        real_builder.can_place = Mock(return_value={"ok": True})
        self.builder.ensure_plan.side_effect = real_builder.ensure_plan
        live = {"remaining": 5000, "fuel": 8000000, "belt_coal": 8, "gross_coal_per_minute": 15}
        action = self.energy._ensure_feed(self.obs, 1, live)
        self.assertEqual((action["type"], action["name"], action["position"]),
                         ("build", "transport-belt", missing["position"]))
        self.builder._seed.assert_not_called()
        evidence = {"target_kw": 300, "feeds": [{**live, "remaining": 0}, live]}
        self.assertEqual(self.energy.capacity(evidence)["total_kw"], 562)

    def test_trunk_transit_requires_observed_matching_belts_to_bank(self):
        trunk = self.depleted_trunk()
        pristine = deepcopy(self.obs["entities"])
        for damage in ("missing", "reversed", "unreserved", "conflicting_reservation"):
            with self.subTest(damage=damage):
                self.obs["entities"] = deepcopy(pristine)
                original = deepcopy(self.energy.state["feeds"])
                if damage == "missing":
                    self.obs["entities"] = [e for e in self.obs["entities"] if e != trunk[6]]
                elif damage == "reversed":
                    next(e for e in self.obs["entities"] if e == trunk[6])["direction"] = 12
                elif damage == "unreserved":
                    self.energy.state["feeds"][0]["plan"]["entities"].remove(trunk[6])
                else:
                    self.energy.state["feeds"].append({"plan": {"entities": [{**trunk[6], "direction": 12}]}})
                intact, intakes = self.energy._coal_transit(self.obs)
                self.assertIsNone(self.energy._coal_tail(trunk[0]["position"], intact, intakes))
                self.energy.state["feeds"] = original

    def test_trunk_tail_rejects_cycles_and_preserves_each_turn(self):
        rows = [{"name": "transport-belt", "position": {"x": .5, "y": .5}, "direction": 4},
                {"name": "transport-belt", "position": {"x": 1.5, "y": .5}, "direction": 8},
                {"name": "transport-belt", "position": {"x": 1.5, "y": 1.5}, "direction": 12}]
        intact = {(e["position"]["x"], e["position"]["y"]): e for e in rows}
        self.assertEqual(self.energy._coal_tail(rows[0]["position"], intact, {(1.5, 1.5)}), rows)
        rows[1]["direction"] = 12
        self.assertIsNone(self.energy._coal_tail(rows[0]["position"], intact, {(1.5, 1.5)}))

    def test_join_downstream_of_declared_bank_port_retains_bank_repair_ownership(self):
        self.energy._sync(self.obs)
        bank = self.energy.state["banks"][0]
        terminal = next(e for e in bank["entities"] if e["name"] == "transport-belt" and e["position"]["x"] == .5)
        incoming = {"name": "transport-belt", "position": {"x": .5, "y": 2.5}, "direction": 8}
        isolated = {"name": "transport-belt", "position": {"x": 10.5, "y": 10.5}, "direction": 4}
        bank["entities"].append(isolated)
        self.energy.state["feeds"][0]["plan"]["entities"].append(incoming)
        self.obs["entities"] = bank["entities"] + [incoming]
        intact, intakes = self.energy._coal_transit(self.obs)
        self.assertEqual(self.energy._coal_tail(incoming["position"], intact, intakes), [incoming, terminal])
        self.assertNotIn((10.5, 10.5), intakes)


if __name__ == "__main__":
    unittest.main()
