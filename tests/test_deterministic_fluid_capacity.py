from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_fluids import FluidProduction
from test_deterministic_fluids import catalog, fluid


READY = {"status": "succeeded", "reason": "observed", "evidence": {}}


class FluidCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.builder = Mock()
        self.builder.ensure_plan.return_value = READY
        self.catalog = catalog()
        self.catalog.fingerprint = "catalog"
        self.factory = Mock()
        self.factory.reserve_site.side_effect = self.reserve
        self.factory.ensure_power_connection.return_value = READY
        self.factory.connect_input.return_value = READY
        self.factory._merge_output.return_value = READY
        self.factory.ensure_product.side_effect = lambda obs, item, **kwargs: self.source(item, "item")
        self.fluids = FluidProduction(self.game, self.builder, self.catalog)
        self.fluids.factory = self.factory
        self.fluids._connect_pipe = Mock(return_value=READY)
        self.fluids._ensure_raw_source = Mock(side_effect=lambda obs, item, amount: self.source(item, "fluid"))
        self.fluids._raw_capacity = Mock(side_effect=lambda obs, item, rate, result: result)
        self.fluids._source_evidence = Mock(return_value={"product": "petroleum-gas", "available": 10})
        self.obs = {"world_id": "fixture", "tick": 100, "entities": [], "inventory": {},
                    "enabled_recipes": {name: True for name in self.catalog.recipes | self.catalog.entities}}
        self.reservations = {}

    def reserve(self, origin, key, observation, reference=None):
        if key not in self.reservations:
            plan = deepcopy(origin)
            offset = len(self.reservations) * 24
            for entity in plan["entities"] + plan["ports"]:
                entity["position"]["x"] += offset
            plan["key"] = key
            self.reservations[key] = plan
        return self.reservations[key]

    @staticmethod
    def source(item, kind):
        return {**READY, "evidence": {"ports": [{"kind": kind, "item": item, "direction": "output",
                    "position": {"x": -20.5, "y": .5}, "facing": 4}]}}

    def gas(self, rate=None):
        return self.fluids.ensure_source(self.obs, "petroleum-gas", rate_per_minute=rate)

    def test_existing_refinery_gets_two_normal_reserved_cells_with_all_feeds_and_output_merges(self):
        self.assertEqual(self.gas()["status"], "succeeded")
        primary = deepcopy(self.fluids.state["sources"]["basic-oil-processing"])
        self.fluids._connect_pipe.reset_mock()
        result = self.gas(1125)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["machines_constructed"], 3)
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 1620)
        self.assertEqual(result["evidence"]["requested_rate_per_minute"], 1125)
        self.assertFalse(result["evidence"]["throughput_verified"])
        self.assertTrue(result["evidence"]["raw_source_capacity_verified"])
        self.assertEqual(self.fluids.state["sources"]["basic-oil-processing"], primary)
        self.assertEqual(result["evidence"]["ports"], [p for p in primary["ports"] if p["direction"] == "output"])
        connections = self.fluids._connect_pipe.call_args_list
        self.assertEqual(len(connections), 5)  # Three crude feeds and two petroleum joins.
        self.assertEqual(sum(c.args[1]["item"] == "crude-oil" for c in connections), 3)
        merges = [c for c in connections if ":output:" in c.args[3]]
        self.assertEqual(len(merges), 2)
        destination = result["evidence"]["ports"][0]
        self.assertTrue(all(c.args[2] == destination for c in merges))
        self.assertEqual({c.args[1] for c in self.factory.ensure_power_connection.call_args_list},
                         {"fluid:basic-oil-processing", "fluid:basic-oil-processing:capacity:1", "fluid:basic-oil-processing:capacity:2"})

    def test_build_power_and_disconnected_output_never_claim_nominal_ready_capacity(self):
        self.gas()
        self.builder.ensure_plan.side_effect = lambda obs, plan: {"type": "build", "name": "oil-refinery"} if plan.get("capacity_key") else READY
        result = self.gas(1125)
        self.assertEqual(result["type"], "build")
        self.assertNotIn("nominal_capacity_per_minute", result["evidence"])
        self.assertFalse(result["evidence"]["flow_verified"])
        self.builder.ensure_plan.side_effect = None
        self.factory.ensure_power_connection.side_effect = lambda obs, key, plan: {"type": "build", "name": "small-electric-pole"} if plan.get("capacity_key") else READY
        self.assertEqual(self.gas(1125)["name"], "small-electric-pole")
        self.factory.ensure_power_connection.side_effect = None
        self.fluids._connect_pipe.side_effect = lambda obs, source, destination, key, plan: (
            {"status": "blocked", "reason": "actual segment disconnected"} if ":output:" in key else READY)
        self.assertEqual(self.gas(1125)["status"], "blocked")

    def test_rate_reduction_restart_and_rollback_reobserve_without_shrinking_or_duplicate_sites(self):
        self.gas(1125)
        saved = deepcopy(self.fluids.state["sources"])
        self.assertEqual(self.gas(100)["evidence"]["machines_constructed"], 3)
        resumed = FluidProduction(self.game, self.builder, self.catalog)
        resumed.factory = self.factory
        resumed._connect_pipe = self.fluids._connect_pipe
        resumed._ensure_raw_source = self.fluids._ensure_raw_source
        resumed._raw_capacity = self.fluids._raw_capacity
        resumed._source_evidence = self.fluids._source_evidence
        self.obs["tick"] = 50
        self.builder.ensure_plan.side_effect = lambda obs, plan: {"type": "build", "name": "oil-refinery"} if plan.get("capacity_key", "").endswith(":2") else READY
        result = resumed.ensure_source(self.obs, "petroleum-gas", rate_per_minute=1125)
        self.assertEqual(result["type"], "build")
        self.assertEqual(resumed.state["sources"], saved)
        self.assertEqual(self.factory.reserve_site.call_count, 3)

    def test_legacy_multi_machine_row_outputs_all_join_the_primary_port(self):
        self.fluids._sync(self.obs)
        primary = self.fluids.plan("basic-oil-processing", "oil-refinery", {"x": .5, "y": .5}, count=3)
        self.fluids.state["sources"]["basic-oil-processing"] = deepcopy(primary)
        result = self.gas(1125)
        self.factory.reserve_site.assert_not_called()
        self.assertEqual(self.fluids.state["sources"]["basic-oil-processing"], primary)
        self.assertEqual(result["evidence"]["machines_constructed"], 3)
        self.assertEqual(sum(":output:" in c.args[3] for c in self.fluids._connect_pipe.call_args_list), 2)

    def test_empty_output_is_waiting_even_when_all_nominal_capacity_is_constructed(self):
        self.fluids._source_evidence.return_value = {"product": "petroleum-gas", "available": 0}
        result = self.gas(1125)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 1620)
        self.assertFalse(result["evidence"]["flow_verified"])
        self.assertFalse(result["evidence"]["throughput_verified"])

    def test_recipe_ingredient_rates_propagate_once_for_whole_requested_output(self):
        self.catalog.recipes["plastic-bar"] = {"categories": ["chemistry"], "energy": 1,
            "ingredients": [{"type": "item", "name": "coal", "amount": 1}, fluid("petroleum-gas", 20)],
            "products": [{"type": "item", "name": "plastic-bar", "amount": 2}]}
        self.obs["enabled_recipes"]["plastic-bar"] = True
        result = self.fluids.ensure_source(self.obs, "plastic-bar", rate_per_minute=90)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["machines_constructed"], 3)
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 120)  # Output arms allow only 40 per cell.
        self.factory.ensure_product.assert_called_once_with(self.obs, "coal", rate_per_minute=45)
        gas_cells = [p for key, p in self.fluids.state["sources"].items() if key.startswith("basic-oil-processing")]
        self.assertEqual(len(gas_cells), 2)  # 90 plastic needs 900 gas per minute.
        self.assertEqual(self.factory._merge_output.call_count, 2)
        self.assertEqual(result["evidence"]["input_rates_per_minute"], {"coal": 45, "petroleum-gas": 900})
        self.fluids._raw_capacity.assert_called_with(self.obs, "crude-oil", 900 * 100 / 45, self.source("crude-oil", "fluid"))

    def test_every_expanded_refinery_coproduct_is_buffered_before_its_feed(self):
        events = []
        self.fluids._ensure_buffer = Mock(side_effect=lambda obs, item, plan: events.append((plan.get("capacity_key", "primary"), "buffer", item)) or READY)
        self.fluids._connect_pipe.side_effect = lambda obs, source, dest, key, plan: events.append((key, "pipe", source["item"])) or READY
        result = self.fluids.ensure_source(self.obs, "light-oil", rate_per_minute=1200)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.fluids._ensure_buffer.call_count, 9)
        for key in ("primary", "advanced-oil-processing:capacity:1", "advanced-oil-processing:capacity:2"):
            positions = [i for i, row in enumerate(events) if row[0] == key and row[1] == "buffer"]
            feed_key = "fluid:" + ("advanced-oil-processing" if key == "primary" else key) + ":0:crude-oil"
            feed = next(i for i, row in enumerate(events) if row[0] == feed_key)
            self.assertEqual(len(positions), 3)
            self.assertLess(max(positions), feed)

    def test_coproduct_buffer_routes_have_distinct_persistent_cell_keys(self):
        self.fluids._sync(self.obs)
        self.fluids.state["buffers"]["heavy-oil"] = {"entities": [], "ports": [{"direction": "input", "position": {"x": 90, "y": 90}}]}
        for key in (None, "advanced-oil-processing:capacity:1"):
            plan = self.fluids.plan("advanced-oil-processing", "oil-refinery", {"x": .5, "y": .5})
            if key:
                plan["capacity_key"] = key
            self.fluids._ensure_buffer(self.obs, "heavy-oil", plan)
        self.assertEqual([c.args[3] for c in self.fluids._connect_pipe.call_args_list],
                         ["fluid:buffer:advanced-oil-processing:heavy-oil", "fluid:buffer:advanced-oil-processing:capacity:1:heavy-oil"])

    def test_missing_saved_item_port_arm_fails_before_expansion(self):
        self.fluids._sync(self.obs)
        plan = self.fluids.plan("sulfuric-acid", "chemical-plant", {"x": .5, "y": .5})
        plan["entities"] = [e for e in plan["entities"] if e["name"] != "inserter"]
        self.fluids.state["sources"]["sulfuric-acid"] = plan
        result = self.fluids.ensure_source(self.obs, "sulfuric-acid", rate_per_minute=1000)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("port geometry", result["reason"])
        self.factory.reserve_site.assert_not_called()
        self.builder.ensure_plan.assert_not_called()

    def test_huge_rate_is_bounded_without_reserving_dozens_of_new_cells(self):
        self.gas()
        reservations = deepcopy(self.reservations)
        result = self.gas(10 ** 9)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("64-machine", result["reason"])
        self.assertEqual(self.reservations, reservations)

    def test_legacy_row_requires_each_machines_item_ports_not_just_one_complete_set(self):
        self.fluids._sync(self.obs)
        original = self.fluids.plan("sulfuric-acid", "chemical-plant", {"x": .5, "y": .5}, count=2)
        for mode in ("missing", "unindexed", "invalid-index"):
            with self.subTest(mode=mode):
                plan = deepcopy(original)
                if mode == "missing":
                    plan["ports"] = [p for p in plan["ports"] if not (p["kind"] == "item" and p.get("machine_index") == 1)]
                else:
                    for port in plan["ports"]:
                        if port["kind"] == "item" and port.get("machine_index") == 1:
                            if mode == "unindexed":
                                port.pop("machine_index")
                            else:
                                port["machine_index"] = 2
                self.fluids.state["sources"]["sulfuric-acid"] = plan
                result = self.fluids.ensure_source(self.obs, "sulfuric-acid", rate_per_minute=700)
                self.assertEqual(result["status"], "blocked")
        self.factory.reserve_site.assert_not_called()
        self.builder.ensure_plan.assert_not_called()


class AggregateFluidDemandTests(unittest.TestCase):
    def test_blue_science_sizes_combined_petroleum_demand_before_individual_consumers(self):
        factory = DeterministicFactory.__new__(DeterministicFactory)
        factory.state = {}
        factory._sync = Mock()
        factory._save = Mock()
        rows = catalog().recipes
        rows.update({"plastic-bar": {"energy": 1, "products": [{"name": "plastic-bar", "amount": 2}]},
                     "sulfur": {"energy": 1, "products": [{"name": "sulfur", "amount": 2}]}})
        factory.catalog = SimpleNamespace(recipes=rows, items={})
        factory.graph = SimpleNamespace(science_rate_per_minute=30,
            _continuous_rates=Mock(return_value=({"basic-oil-processing": 25, "plastic-bar": 45, "sulfur": 7.5}, {})),
            machines_for_recipe=Mock(return_value=[]))
        calls = []
        factory.fluids = SimpleNamespace(ensure_source=Mock(side_effect=lambda obs, name, **kw: calls.append(("fluid", name, kw)) or READY))
        factory.ensure_product = Mock(side_effect=lambda obs, name, **kw: calls.append(("item", name, kw)) or READY)
        result = factory.ensure_capacity({}, ["chemical-science-pack"])
        self.assertEqual(calls[0], ("fluid", "petroleum-gas", {"rate_per_minute": 1125}))
        self.assertEqual(result["evidence"]["fluid_requirements_per_minute"], {"petroleum-gas": 1125})
        self.assertFalse(result["evidence"]["flow_verified"])
        calls.clear()
        factory.graph._continuous_rates.return_value = (factory.graph._continuous_rates.return_value[0],
                                                        {("fluid", "crude-oil"): 2500, ("fluid", "water"): 225})
        factory.ensure_capacity({}, ["chemical-science-pack"])
        self.assertEqual(calls[:3], [("fluid", "crude-oil", {"rate_per_minute": 2500}),
                                    ("fluid", "petroleum-gas", {"rate_per_minute": 1125}),
                                    ("fluid", "water", {"rate_per_minute": 225})])


class RawFluidCapacityTests(unittest.TestCase):
    def setUp(self):
        extent = patch("factorio_ai.deterministic_oil_capacity._pipeline_extent", return_value={"ok": True})
        extent.start()
        self.addCleanup(extent.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.fluids = FluidProduction(self.game, Mock(), catalog())
        self.fluids.factory = Mock()
        self.fluids.factory.state = {"blocks": {}}
        self.fluids.factory.ensure_power_connection.return_value = READY
        self.fluids.builder.ensure_plan.return_value = READY
        self.obs = {"world_id": "fixture", "tick": 1}
        self.fluids._sync(self.obs)
        for fluid_name, name in (("crude-oil", "pumpjack"), ("water", "offshore-pump")):
            self.fluids.state["sources"]["raw:" + fluid_name] = {"entities": [
                {"name": name, "position": {"x": .5, "y": .5}}], "ports": [
                {"kind": "fluid", "item": fluid_name, "direction": "output", "position": {"x": 1.5, "y": 1.5}}]}
        self.fluids._ensure_raw_source = Mock(side_effect=lambda obs, item, amount: FluidCapacityTests.source(item, "fluid"))

    def test_current_well_yield_cannot_be_replaced_by_refinery_nominal_capacity(self):
        self.game.query.return_value = {"ok": True, "world_id": "fixture", "owned": [{"key": "raw:crude-oil",
            "position": {"x": .5, "y": .5}, "built": True, "nominal_capacity_per_minute": 1200}],
            "candidates": [], "search_truncated": False}
        result = self.fluids.ensure_source(self.obs, "crude-oil", rate_per_minute=2500)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 1200)
        self.assertEqual(result["evidence"]["requested_rate_per_minute"], 2500)
        self.assertFalse(result["evidence"]["raw_source_capacity_verified"])
        self.assertIn("live actor position", result["reason"])

    def test_sufficient_source_is_nominal_only_and_rechecked_after_yield_declines(self):
        self.game.query.return_value = {"ok": True, "world_id": "fixture", "owned": [{"key": "raw:crude-oil",
            "position": {"x": .5, "y": .5}, "built": True, "nominal_capacity_per_minute": 3000}],
            "candidates": [], "search_truncated": False}
        result = self.fluids.ensure_source(self.obs, "crude-oil", rate_per_minute=2500)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["evidence"]["throughput_verified"])
        self.assertTrue(result["evidence"]["raw_source_capacity_verified"])
        self.game.query.return_value["owned"][0]["nominal_capacity_per_minute"] = 2000
        self.assertEqual(self.fluids.ensure_source(self.obs, "crude-oil", rate_per_minute=2500)["status"], "blocked")
        self.assertEqual(self.game.query.call_count, 3)  # Decline triggers a fresh bounded well survey.

    def test_unknown_foreign_or_wrong_world_source_does_not_credit_capacity(self):
        for survey in ({"ok": False, "reason": "foreign source"},
                       {"ok": True, "world_id": "different", "nominal_capacity_per_minute": 72000},
                       {"ok": True, "world_id": "fixture", "nominal_capacity_per_minute": float("nan")},
                       {"ok": True, "world_id": "fixture", "nominal_capacity_per_minute": 0}):
            self.game.query.return_value = survey
            self.assertEqual(self.fluids.ensure_source(self.obs, "water", rate_per_minute=100)["status"], "blocked")

    def test_startup_oil_trigger_and_unfinished_raw_construction_do_not_require_rate_proof(self):
        self.assertEqual(self.fluids.ensure_source(self.obs, "crude-oil")["status"], "succeeded")
        self.game.query.assert_not_called()
        self.fluids._ensure_raw_source.side_effect = None
        self.fluids._ensure_raw_source.return_value = {"type": "build", "name": "pumpjack"}
        self.assertEqual(self.fluids.ensure_source(self.obs, "crude-oil", rate_per_minute=2500)["type"], "build")
        self.game.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
