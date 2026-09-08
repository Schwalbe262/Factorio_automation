from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_fluids import FluidProduction
from factorio_ai.deterministic_petroleum_bus import ensure_petroleum_bus
from test_deterministic_fluids import catalog


READY = {"status": "succeeded"}


class PetroleumBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.obs = {"world_id": "petroleum-test", "tick": 100, "entities": [],
                    "enabled_recipes": {"oil-refinery": True, "basic-oil-processing": True,
                                        "advanced-oil-processing": True}}
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    query=Mock(return_value={"ok": True, "world_id": "petroleum-test"}))
        self.builder = Mock()
        self.builder.ensure_plan.return_value = READY
        self.factory = Mock()
        self.factory.ensure_power_connection.return_value = READY
        self.factory.register_plan.side_effect = lambda key, plan, obs: plan
        self.factory._reserved.return_value = []
        self.factory._port_clearances.return_value = set()
        self.fluids = FluidProduction(self.game, self.builder, catalog())
        self.fluids.factory = self.factory
        self.fluids._sync(self.obs)
        self.basic = self.fluids.plan("basic-oil-processing", "oil-refinery", {"x": .5, "y": .5})
        self.advanced = self.fluids.plan("advanced-oil-processing", "oil-refinery", {"x": 30.5, "y": .5})
        self.fluids.state["sources"].update({"basic-oil-processing": self.basic,
                                            "advanced-oil-processing": self.advanced})
        self.old_port = next(p for p in self.basic["ports"] if p["item"] == "petroleum-gas")
        self.new_port = next(p for p in self.advanced["ports"] if p["item"] == "petroleum-gas")

    def test_existing_consumer_survives_source_switch_only_after_observed_paid_join(self):
        destination = {**self.old_port, "position": {"x": 12.5, "y": -8.5}, "direction": "input"}
        old_link = {"ok": True, "entities": [{"name": "pipe", "position": destination["position"],
                                                "_fluid": "petroleum-gas"}], "ports": []}
        consumer_key = "fluid:plastic-bar:0:petroleum-gas"
        self.fluids.state["links"][consumer_key] = deepcopy(old_link)
        joined = False
        self.fluids._network_taps = Mock(side_effect=lambda source, destination:
            {"connected": source["position"] == self.old_port["position"] or joined})
        self.fluids._machine_connection_obstacles = Mock(return_value=[])
        self.builder.route.return_value = {"ok": True, "path": [{"x": 20.5, "y": -4.5}]}
        before = self.fluids._connect_pipe(self.obs, self.old_port, destination, consumer_key, {})
        self.assertEqual(before["status"], "succeeded")
        self.assertEqual(self.fluids._pick_recipe(self.obs, "petroleum-gas"), "advanced-oil-processing")
        self.assertEqual(self.fluids._connect_pipe(self.obs, self.new_port, destination, consumer_key, {})["status"], "blocked")

        unjoined = ensure_petroleum_bus(self.fluids, self.obs, self.advanced)
        self.assertEqual(unjoined["status"], "blocked")
        self.assertEqual(self.builder.route.call_count, 1)
        bus = self.fluids.state["links"]["petroleum-bus:advanced-to-basic"]
        self.assertTrue(all(e["_fluid"] == "petroleum-gas" for e in bus["entities"]))
        self.assertIn(bus, [call.args[1] for call in self.factory.register_plan.call_args_list])
        self.assertIn(bus, [call.args[1] for call in self.builder.ensure_plan.call_args_list])

        joined = True
        self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["status"], "succeeded")
        self.assertEqual(self.fluids._connect_pipe(self.obs, self.new_port, destination, consumer_key, {})["status"], "succeeded")
        self.assertEqual(self.fluids.state["links"][consumer_key], old_link)
        self.assertEqual(self.builder.route.call_count, 1)

        resumed = FluidProduction(self.game, self.builder, self.fluids.catalog)
        resumed.factory = self.factory
        resumed._network_taps = self.fluids._network_taps
        self.assertEqual(ensure_petroleum_bus(resumed, self.obs, resumed.state["sources"]["advanced-oil-processing"])["status"], "succeeded")
        self.builder.ensure_plan.side_effect = lambda obs, plan: (
            {"type": "build", "name": "pipe"} if plan == bus else READY)
        repair = ensure_petroleum_bus(resumed, self.obs, resumed.state["sources"]["advanced-oil-processing"])
        self.assertEqual((repair["type"], repair["name"]), ("build", "pipe"))
        self.builder.ensure_plan.side_effect = None
        joined = False
        self.assertEqual(ensure_petroleum_bus(resumed, self.obs, resumed.state["sources"]["advanced-oil-processing"])["status"], "blocked")
        self.assertEqual(self.builder.route.call_count, 1)

    def test_all_coproduct_buffers_and_bus_precede_advanced_feed(self):
        self.fluids._ensure_buffer = Mock(return_value=READY)
        self.fluids._ensure_raw_source = Mock()
        self.fluids._connect_pipe = Mock(return_value={"type": "build", "name": "pipe"})
        result = self.fluids.ensure_source(self.obs, "light-oil")
        self.assertEqual(result["type"], "build")
        self.assertEqual([c.args[1] for c in self.fluids._ensure_buffer.call_args_list],
                         ["heavy-oil", "light-oil", "petroleum-gas"])
        self.assertEqual(self.fluids._connect_pipe.call_args.args[3], "petroleum-bus:advanced-to-basic")
        self.fluids._ensure_raw_source.assert_not_called()
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_enclosed_empty_advanced_outlet_can_use_verified_segment_tap(self):
        tap = {"x": 25.5, "y": -4.5}
        self.fluids._machine_connection_obstacles = Mock(return_value=[])
        self.builder.route.side_effect = [{"ok": False, "reason": "outlet enclosed"},
                                         {"ok": True, "path": [tap]}]
        self.fluids._network_taps = Mock(side_effect=[{"connected": False, "taps": [tap]}, {"connected": True}])
        result = ensure_petroleum_bus(self.fluids, self.obs, self.advanced)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.builder.route.call_args_list[1].args[0], tap)
        self.assertTrue(self.fluids._network_taps.call_args_list[0].args[0]["allow_empty_segment"])
        self.assertNotIn("allow_empty_segment", self.new_port)
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_missing_old_endpoint_is_rebuilt_before_join(self):
        self.builder.ensure_plan.return_value = {"type": "build", "name": "pipe", "position": self.old_port["position"]}
        self.fluids._connect_pipe = Mock()
        self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["type"], "build")
        self.builder.ensure_plan.assert_called_once_with(self.obs, self.basic)
        self.factory.ensure_power_connection.assert_not_called()
        self.game.query.assert_not_called()
        self.fluids._connect_pipe.assert_not_called()

    def test_old_power_wait_is_not_reported_as_ready(self):
        self.factory.ensure_power_connection.return_value = {"status": "waiting", "reason": "power not observed"}
        self.fluids._connect_pipe = Mock()
        self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["status"], "waiting")
        self.game.query.assert_not_called()
        self.fluids._connect_pipe.assert_not_called()

    def test_unavailable_wrong_world_or_untyped_endpoint_never_builds_join(self):
        self.fluids._connect_pipe = Mock()
        for proof in (None, {"ok": False, "reason": "foreign"}, {"ok": False, "reason": "another fluid"},
                      {"ok": True, "world_id": "other"}):
            with self.subTest(proof=proof):
                self.game.query.return_value = proof
                self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["status"], "blocked")
        self.fluids._connect_pipe.assert_not_called()

    def test_advanced_first_world_does_not_create_basic_refinery(self):
        del self.fluids.state["sources"]["basic-oil-processing"]
        self.fluids._connect_pipe = Mock()
        self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["status"], "succeeded")
        self.builder.ensure_plan.assert_not_called()
        self.game.query.assert_not_called()
        self.fluids._connect_pipe.assert_not_called()

    def test_invalid_saved_primary_port_fails_before_paid_repairs(self):
        self.basic["ports"].append(deepcopy(self.old_port))
        self.assertEqual(ensure_petroleum_bus(self.fluids, self.obs, self.advanced)["status"], "blocked")
        self.builder.ensure_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
