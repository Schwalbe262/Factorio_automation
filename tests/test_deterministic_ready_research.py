from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_ready_research import ready_research
from factorio_ai.deterministic_supervisor import DeterministicSupervisor
from factorio_ai.factory_templates import build_template
from tests import test_deterministic_factory as factory_tests
from tests.test_deterministic_factory import ready, port


RED = "automation-science-pack"
GREEN = "logistic-science-pack"
GEAR = "iron-gear-wheel"


class ReadyResearchTests(unittest.TestCase):
    def setUp(self):
        factory_tests.FactoryTests.setUp(self)
        self.catalog.entities["assembling-machine-1"] = {"crafting_speed": .5}
        self.catalog.recipes.update({
            RED: {"name": RED, "energy": 5, "categories": ["crafting"],
                  "ingredients": [{"name": "copper-plate", "amount": 1}, {"name": GEAR, "amount": 1}],
                  "products": [{"name": RED, "amount": 1}]},
            GEAR: {"name": GEAR, "energy": .5, "categories": ["crafting"],
                   "ingredients": [{"name": "iron-plate", "amount": 2}],
                   "products": [{"name": GEAR, "amount": 1}]}})
        for name, ingredients in (("fast-inserter", [RED]), ("automation-2", [RED, GREEN]), ("logistics", [RED])):
            self.catalog.technologies[name] = {"name": name, "enabled": True, "researched": False,
                "prerequisites": [RED], "unit_count": 30, "unit_energy": 900,
                "ingredients": [{"name": item, "amount": 1, "type": "item"} for item in ingredients],
                "unlocks": [name]}
        self.catalog.technology_order = lambda names, **kwargs: [names] if isinstance(names, str) else list(names)
        self.factory.graph.science_rate_per_minute = 30
        self.factory.graph.for_first_rocket.return_value = {
            "technology_order": ["automation-2"], "bom": {"science_packs": {RED: 10, GREEN: 10}}}
        self.factory.graph.next_research.return_value = {"kind": "research", "technology": "automation-2"}
        self.factory.graph._continuous_rates.return_value = ({RED: 30, GEAR: 30}, {})
        self.lab = build_template("labs_row", inputs=[RED, GREEN], anchor={"x": 12.5, "y": 4.5})
        self.red = build_template("assembler_row", recipe=RED, inputs=["copper-plate", GEAR], output=RED)
        self.gear = build_template("assembler_row", recipe=GEAR, inputs=["iron-plate"], output=GEAR,
                                   anchor={"x": -15.5, "y": -10.5})
        for key, plan in (("research:labs", self.lab), ("recipe:" + RED, self.red), ("recipe:" + GEAR, self.gear)):
            plan["key"] = key
            self.factory.state["blocks"][key] = plan
        source = next(p for p in self.red["ports"] if p["direction"] == "output")
        consumer = next(p for p in self.lab["ports"] if p["item"] == RED)
        self.link = {"ok": True, "source_port": deepcopy(source), "consumer_port": deepcopy(consumer), "ports": [],
            "entities": [{"name": "transport-belt", "position": {"x": x + .5, "y": .5},
                          "direction": 8 if x == 11 else 4} for x in range(4, 12)]}
        self.factory.state["links"]["lab:" + RED] = self.link
        entities = {}
        for plan in (self.lab, self.red, self.gear, self.link):
            for spec in plan["entities"]:
                identity = (spec["name"], spec["position"]["x"], spec["position"]["y"])
                if identity not in entities:
                    entity = {**deepcopy(spec), "unit_number": len(entities) + 1, "health": 100,
                              "energy": 1000, "electric_network_connected": True, "electric_network_id": 1,
                              "inventory": {}, "output_inventory": {}, "status_name": "working"}
                    if spec["name"] == "transport-belt":
                        entity["belt_inventory"] = {}
                    if spec.get("recipe"):
                        entity.update(products_finished=20, output_inventory={spec["recipe"]: 4})
                    entities[identity] = entity
        self.obs.update(ok=True, enemies=0, research=None, entities=list(entities.values()),
            enabled_recipes={RED: True, GEAR: True, "electric-mining-drill": True},
            technologies={RED: True, "automation": True, "electric-mining-drill": True, "logistics": True},
            production={RED: {"produced": 20, "consumed": 5}})
        self.live_lab()["inventory"] = {RED: 2}
        self.factory._sync = Mock()
        self.factory._save = Mock()

    def live_lab(self):
        return next(e for e in self.obs["entities"] if e["name"] == "lab")

    def live_maker(self):
        return next(e for e in self.obs["entities"] if e.get("recipe") == RED)

    def live_belt(self):
        return next(e for e in self.obs["entities"] if e["name"] == "transport-belt"
                    and e["position"] == {"x": 7.5, "y": .5})

    def assert_rejected_without_writes(self):
        before, observation = deepcopy(self.factory.state), deepcopy(self.obs)
        self.factory._save.reset_mock()
        self.assertIsNone(ready_research(self.factory, self.obs))
        self.assertEqual(self.factory.state, before)
        self.assertEqual(self.obs, observation)
        self.factory._sync.assert_not_called()
        self.factory._save.assert_not_called()
        self.game.query.assert_not_called()

    def test_missing_green_skips_automation_two_and_queues_normal_fast_research_once(self):
        original = deepcopy(self.obs)
        action = ready_research(self.factory, self.obs)
        self.assertEqual((action["type"], action["technology"]), ("research", "fast-inserter"))
        self.assertEqual(self.factory.state["capability_research"], ["fast-inserter"])
        self.factory._save.assert_called_once_with()
        self.assertEqual(ready_research(self.factory, self.obs)["technology"], "fast-inserter")
        self.factory._save.assert_called_once_with()
        self.assertEqual(self.obs, original)
        self.factory._sync.assert_not_called()
        self.game.query.assert_not_called()
        self.obs["research"] = "fast-inserter"
        self.assert_rejected_without_writes()

    def test_ready_roadmap_research_precedes_fast_capability(self):
        self.obs["technologies"].pop("logistics")
        self.factory.graph.for_first_rocket.return_value["technology_order"].insert(0, "logistics")
        self.factory.graph.next_research.return_value = {"kind": "research", "technology": "logistics"}
        self.assertEqual(ready_research(self.factory, self.obs)["technology"], "logistics")

    def test_fast_research_requires_an_existing_rate_bottleneck(self):
        self.factory.graph._continuous_rates.return_value = ({RED: 30, GEAR: 10}, {})
        self.assert_rejected_without_writes()
        self.factory.graph._continuous_rates.return_value = ({RED: 30, GEAR: 30}, {})
        self.factory.state["blocks"].pop("recipe:" + GEAR)
        self.assert_rejected_without_writes()

    def test_unknown_unsafe_or_old_observations_never_preempt_research(self):
        original = deepcopy(self.obs)
        for field, value in (("ok", False), ("research", "automation-2"), ("enemies", 1), ("enemies", None),
                             ("enemies", False), ("world_id", "other"), ("tick", 99), ("tick", 0)):
            with self.subTest(field=field, value=value):
                self.obs = {**deepcopy(original), field: value}
                self.assert_rejected_without_writes()
        self.obs = original
        self.factory.state["catalog_fingerprint"] = "other-catalog"
        self.assert_rejected_without_writes()

    def test_hand_science_and_stale_production_cannot_replace_machine_proof(self):
        original = deepcopy(self.obs)
        for change in (lambda: self.live_maker().update(products_finished=0),
                       lambda: self.live_maker().update(recipe=GEAR),
                       lambda: self.live_maker().update(unit_number=0),
                       lambda: self.live_lab().update(inventory={}),
                       lambda: self.obs["entities"].append(deepcopy(self.live_maker()))):
            with self.subTest(change=change):
                self.obs = deepcopy(original)
                self.obs["inventory"] = {RED: 1000}
                change()
                self.assert_rejected_without_writes()

    def test_broken_unpowered_contaminated_or_unproven_feed_is_rejected(self):
        original = deepcopy(self.obs)
        for change in (lambda: self.obs["entities"].remove(self.live_belt()),
                       lambda: self.live_belt().update(direction=12),
                       lambda: self.live_belt().update(belt_inventory={"iron-plate": 1}),
                       lambda: self.live_belt().pop("belt_inventory"),
                       lambda: self.live_lab().update(energy=0),
                       lambda: self.live_maker().update(electric_network_connected=False),
                       lambda: next(e for e in self.obs["entities"] if e["name"] == "inserter").update(energy=0)):
            with self.subTest(change=change):
                self.obs = deepcopy(original)
                change()
                self.assert_rejected_without_writes()
        self.obs = original
        # Consistent plans/observations still cannot certify a disconnected belt.
        planned = next(e for e in self.link["entities"] if e["position"] == self.live_belt()["position"])
        planned["direction"] = self.live_belt()["direction"] = 12
        self.assert_rejected_without_writes()
        planned["direction"] = self.live_belt()["direction"] = 4
        for metadata in ("upstream_tap", "consumer_entry"):
            self.link[metadata] = {"unverified": True}
            self.assert_rejected_without_writes()
            del self.link[metadata]
        self.link["consumer_port"]["position"]["x"] += 1
        self.assert_rejected_without_writes()

    def test_research_requires_prerequisites_and_finite_positive_catalog_units(self):
        technology = self.catalog.technologies["fast-inserter"]
        for field, value in (("prerequisites", ["missing"]), ("unit_energy", 0), ("unit_count", float("nan")),
                             ("ingredients", []), ("research_trigger", {"type": "craft-item", "item": RED})):
            with self.subTest(field=field):
                original = deepcopy(technology)
                technology[field] = value
                self.assert_rejected_without_writes()
                technology.clear()
                technology.update(original)

    def test_retired_migration_requires_same_world_new_identity_and_old_absence(self):
        migration = {"state": "retired", "world_id": "one", "replacement_unit_number": self.live_lab()["unit_number"],
                     "old_unit_number": 999, "old_plan": {"entities": [{"name": "lab", "position": {"x": 80.5, "y": .5}}]}}
        self.factory.state["lab_migration"] = migration
        self.assertEqual(ready_research(self.factory, self.obs)["technology"], "fast-inserter")
        for field, value in (("state", "retiring"), ("world_id", "other"), ("replacement_unit_number", 998)):
            previous = migration[field]
            migration[field] = value
            self.assert_rejected_without_writes()
            migration[field] = previous
        self.obs["entities"].append({**deepcopy(self.live_lab()), "unit_number": 999, "position": {"x": 80.5, "y": .5}})
        self.assert_rejected_without_writes()

    def test_normal_factory_selection_preserves_chosen_fast_research(self):
        self.assertEqual(ready_research(self.factory, self.obs)["technology"], "fast-inserter")
        self.factory.bootstrap_electric_mining = Mock(return_value=None)
        self.factory._ensure_startup_iron = Mock(return_value=ready())
        self.factory._ensure_lab = Mock(return_value=ready())
        self.factory.ensure_product = Mock(side_effect=lambda obs, item: ready(ports=[port(item)]))
        self.factory.connect_input = Mock(return_value=ready())
        with patch("factorio_ai.deterministic_construction_buffer.ensure_construction_buffer", return_value=None):
            result = self.factory.next_action(self.obs)
        self.assertEqual((result["type"], result["technology"]), ("research", "fast-inserter"))

    def test_supervisor_emits_ready_research_before_calling_construction_planners(self):
        supervisor = DeterministicSupervisor(self.game)
        supervisor.bootstrap = SimpleNamespace(next_action=Mock(return_value=ready()))
        supervisor.builder = SimpleNamespace(state={}, _sync=Mock(), ensure_power=Mock(return_value=ready()),
                                             owns_automated_burner=Mock(return_value=False))
        supervisor.factory = self.factory
        supervisor.prepare_production = Mock()
        supervisor.defense = SimpleNamespace(requirements=Mock(return_value={"research": []}), next_action=Mock())
        supervisor.armaments = SimpleNamespace(next_action=Mock())
        result = supervisor.next_action(self.obs, "rocket")
        self.assertEqual((result["type"], result["technology"]), ("research", "fast-inserter"))
        supervisor.builder.ensure_power.assert_called_once_with(self.obs)
        supervisor.armaments.next_action.assert_not_called()
        supervisor.defense.next_action.assert_not_called()

    def test_active_research_leaves_next_construction_action_unchanged(self):
        supervisor = DeterministicSupervisor(self.game)
        supervisor.bootstrap = SimpleNamespace(next_action=Mock(return_value=ready()))
        supervisor.builder = SimpleNamespace(state={}, _sync=Mock(), ensure_power=Mock(return_value=ready()),
                                             owns_automated_burner=Mock(return_value=False))
        supervisor.factory = self.factory
        supervisor.prepare_production = Mock()
        supervisor.defense = SimpleNamespace(requirements=Mock(return_value={"research": []}), next_action=Mock())
        action = {"type": "build", "name": "transport-belt", "position": {"x": 1, "y": 1}}
        supervisor.armaments = SimpleNamespace(next_action=Mock(return_value=action))
        self.obs["research"] = "fast-inserter"
        self.assertEqual(supervisor.next_action(self.obs, "rocket"), action)
        supervisor.armaments.next_action.assert_called_once_with(self.obs)
        self.factory._save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
