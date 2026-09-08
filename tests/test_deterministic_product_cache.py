from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.factory_templates import build_template


def port(item="coal", x=.5):
    return {"kind": "item", "item": item, "direction": "output",
            "position": {"x": x, "y": .5}, "facing": 4}


def ready(item="coal", **evidence):
    return {"status": "succeeded", "reason": "source observed",
            "evidence": {"ports": [port(item)], **evidence}}


class ProductResultTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)),
                                    query=Mock(return_value={"ok": True, "covered": 0}))
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(fingerprint="catalog-a", recipes={}, entities={}, technologies={})
        self.catalog.recipe_for_product = lambda item: self.catalog.recipes.get(item)
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.ensure_plan = Mock(return_value=ready())
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 100, "entities": [], "enabled_recipes": {}}
        self.factory._sync(self.obs)
        self.factory._source_endpoint = Mock(side_effect=lambda obs, item: ready(item))

    def check(self, item="coal", *, obs=None, stack=(), rate=None):
        return self.factory.ensure_product(self.obs if obs is None else obs, item, stack,
                                           rate_per_minute=rate)

    def test_stable_success_is_reused_for_the_same_item(self):
        first = self.check()
        self.assertEqual(self.check(), first)
        self.assertEqual(self.check("stone"), ready("stone"))
        self.assertEqual(self.check("stone"), ready("stone"))
        self.assertEqual(self.factory._source_endpoint.call_count, 2)
        self.game.query.assert_not_called()

    def test_new_observation_rechecks_changed_source_even_at_the_same_tick(self):
        self.assertEqual(self.check()["status"], "succeeded")
        self.factory._source_endpoint.side_effect = None
        self.factory._source_endpoint.return_value = {"type": "build", "name": "inserter"}
        self.assertEqual(self.check(obs=deepcopy(self.obs))["type"], "build")
        self.assertEqual(self.factory._source_endpoint.call_count, 2)

    def test_reused_observation_invalidates_on_tick_rollback_world_and_catalog(self):
        self.check()
        self.obs["tick"] += 1
        self.check()
        self.obs["tick"] = 90
        self.check()
        self.obs["world_id"] = "two"
        self.check()
        self.factory._fingerprint = "catalog-b"
        self.check()
        self.factory.catalog = deepcopy(self.catalog)
        self.check()
        self.assertEqual(self.factory._source_endpoint.call_count, 6)

    def test_runtime_dependency_replacement_invalidates_stable_results(self):
        self.check()
        for attribute in ("graph", "builder", "fluids"):
            with self.subTest(dependency=attribute):
                previous_calls = self.factory._source_endpoint.call_count
                setattr(self.factory, attribute, Mock())
                self.assertEqual(self.check()["status"], "succeeded")
                self.assertEqual(self.factory._source_endpoint.call_count, previous_calls + 1)
                self.check()
                self.assertEqual(self.factory._source_endpoint.call_count, previous_calls + 1)

    def test_rate_requests_do_not_reuse_other_nominal_capacity(self):
        self.factory._expand_raw_source = Mock(side_effect=lambda obs, item, rate, source:
                                               ready(item, requested_rate_per_minute=rate))
        for rate in (None, 0, 20.0, 20.001):
            with self.subTest(rate=rate):
                first = self.check(rate=rate)
                self.assertEqual(self.check(rate=rate), first)
                if rate is not None:
                    self.assertEqual(first["evidence"]["requested_rate_per_minute"], rate)
        self.assertEqual(self.factory._source_endpoint.call_count, 4)
        self.assertEqual(self.factory._expand_raw_source.call_count, 3)

    def test_pending_failure_and_action_results_are_rechecked(self):
        outcomes = [{"status": status, "reason": status}
                    for status in ("running", "waiting", "blocked", "failed")]
        outcomes += [{"type": "build", "name": "inserter"},
                     {"status": "succeeded", "type": "build", "name": "inserter"}]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                observation = deepcopy(self.obs)
                self.factory._source_endpoint.reset_mock()
                self.factory._source_endpoint.side_effect = [deepcopy(outcome), ready()]
                self.assertEqual(self.check(obs=observation), outcome)
                self.assertEqual(self.check(obs=observation)["status"], "succeeded")
                self.assertEqual(self.check(obs=observation)["status"], "succeeded")
                self.assertEqual(self.factory._source_endpoint.call_count, 2)

    def test_success_that_establishes_ownership_does_not_hide_next_upgrade(self):
        upgrade = {"type": "mine", "name": "burner-mining-drill", "count": 1}

        def source(observation, item):
            if self.factory.state.get("automated_burners"):
                return deepcopy(upgrade)
            self.factory.state["automated_burners"] = ["owned-burner"]
            self.factory._save()
            return ready(item)

        self.factory._source_endpoint.side_effect = source
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(self.check(), upgrade)
        self.assertEqual(self.factory._source_endpoint.call_count, 2)

    def test_late_saved_reservation_exposes_new_construction(self):
        missing = {"type": "build", "name": "transport-belt"}
        self.factory._source_endpoint.side_effect = lambda obs, item: (
            deepcopy(missing) if "late" in self.factory.state["blocks"] else ready(item))
        self.assertEqual(self.check()["status"], "succeeded")
        self.check()
        reservation = {"ok": True, "entities": [
            {"name": "transport-belt", "position": {"x": 10.5, "y": .5}, "direction": 4}]}
        self.assertTrue(self.factory.register_plan("late", reservation, self.obs)["ok"])
        self.assertEqual(self.check(), missing)
        self.assertEqual(self.factory._source_endpoint.call_count, 2)

    def test_returned_ports_cannot_mutate_cache_or_factory_state(self):
        owned = {"ok": True, "entities": [], "ports": [port()]}
        self.factory.state["blocks"]["source:coal"] = owned
        self.factory._save()
        self.factory._source_endpoint.side_effect = lambda obs, item: ready(ports=owned["ports"])
        first = self.check()
        first["evidence"]["ports"][0]["position"]["x"] = 99
        first["evidence"]["ports"].append(port("stone"))
        self.assertEqual(owned["ports"], [port()])
        second = self.check()
        self.assertEqual(second["evidence"]["ports"], [port()])
        second["evidence"]["ports"][0]["position"]["x"] = -99
        self.assertEqual(self.check()["evidence"]["ports"], [port()])
        self.assertEqual(owned["ports"], [port()])
        self.factory._source_endpoint.assert_called_once()

    def test_primed_success_does_not_bypass_dependency_cycle(self):
        self.assertEqual(self.check()["status"], "succeeded")
        result = self.check(stack=("coal",))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "production dependency cycle")
        self.factory._source_endpoint.assert_called_once()

    def test_results_require_the_exact_ancestor_stack(self):
        stacks = ((), ("gear",), ("circuit", "gear"), ("gear", "circuit"))
        for stack in stacks:
            with self.subTest(stack=stack):
                self.check(stack=stack)
                self.check(stack=stack)
        self.assertEqual(self.factory._source_endpoint.call_count, len(stacks))

    def install_fluid_recipe(self):
        self.catalog.recipes["plastic-bar"] = {
            "name": "plastic-bar", "energy": 1,
            "ingredients": [{"name": "petroleum-gas", "type": "fluid", "amount": 20}],
            "products": [{"name": "plastic-bar", "type": "item", "amount": 2}]}
        self.obs["enabled_recipes"]["plastic-bar"] = True
        self.factory.fluids = SimpleNamespace(ensure_source=Mock(return_value=ready("plastic-bar")))

    def test_actual_fluid_delegation_is_always_rechecked(self):
        self.install_fluid_recipe()
        self.assertEqual(self.check("plastic-bar")["status"], "succeeded")
        self.factory.fluids.ensure_source.return_value = ready("plastic-bar", available=7)
        self.assertEqual(self.check("plastic-bar")["evidence"]["available"], 7)
        self.assertEqual(self.factory.fluids.ensure_source.call_count, 2)

    def test_successful_solid_parent_of_fluid_dependency_is_rechecked(self):
        self.install_fluid_recipe()
        self.catalog.recipes["advanced-circuit"] = {
            "name": "advanced-circuit", "energy": 6,
            "ingredients": [{"name": "plastic-bar", "type": "item", "amount": 2}],
            "products": [{"name": "advanced-circuit", "type": "item", "amount": 1}]}
        self.obs["enabled_recipes"]["advanced-circuit"] = True
        self.factory.graph = SimpleNamespace(machines_for_recipe=Mock(return_value=[
            {"name": "assembling-machine-1", "crafting_speed": .5}]))
        plan = build_template("assembler_row", recipe="advanced-circuit",
                              inputs=["plastic-bar"], output="advanced-circuit")
        self.factory.state["blocks"]["recipe:advanced-circuit"] = plan
        self.factory._save()
        self.factory.ensure_power_connection = Mock(return_value=ready())
        self.factory.connect_input = Mock(return_value=ready())
        self.assertEqual(self.check("advanced-circuit")["status"], "succeeded")
        self.assertEqual(self.check("advanced-circuit")["status"], "succeeded")
        self.assertEqual(self.factory.fluids.ensure_source.call_count, 2)
        self.assertEqual(self.builder.ensure_plan.call_count, 2)
        self.factory.fluids.ensure_source.return_value = {"status": "waiting", "reason": "empty output"}
        self.assertEqual(self.check("advanced-circuit")["status"], "waiting")
        self.assertEqual(self.factory.fluids.ensure_source.call_count, 3)


if __name__ == "__main__":
    unittest.main()
