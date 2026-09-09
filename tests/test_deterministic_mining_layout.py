from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_energy import EnergyExpansion
from factorio_ai.deterministic_layout import bounds, mining_clearances, mining_site_clear
from factorio_ai.deterministic_mining_upgrade import ensure_source_upgrade
from tests import test_deterministic_factory as factory_fixture
from tests import test_deterministic_mining_upgrade as upgrade_fixture


def production(factory, x=.5, y=-1.5):
    entity = {"name": "assembling-machine-1", "position": {"x": x, "y": y}, "direction": 0}
    footprint = factory.builder._occupied_by_plan([entity])
    plan = {"ok": True, "entities": [entity], "ports": [], "production_area": bounds(footprint, 2)}
    factory.state["blocks"]["recipe:existing"] = plan
    return plan


class MiningLayoutTests(unittest.TestCase):
    def setUp(self):
        factory_fixture.FactoryTests.setUp(self)
        self.owner = production(self.factory)
        self.game.query.return_value = {"ok": True, "sites": [{"x": 0, "y": 4}, {"x": 0, "y": 5}]}

    def choose(self, key="source:iron-plate:capacity:0"):
        return self.factory._raw_capacity_site(self.obs, "iron-plate", key)

    def test_real_cell_ore_edge_candidate_skipped_for_clear_alternative(self):
        # Four pure ore tiles at y4.5/5.5 can lie outside the existing block's
        # y<=4 survey. The y4 drill nevertheless occupies y3..6, inside its buffer.
        before = deepcopy(self.owner)
        bad = self.factory._electric_source_plan("iron-plate", 0, 4)
        self.assertFalse(self.builder._occupied_by_plan(bad["entities"])
                         & self.builder._occupied_by_plan(self.owner["entities"]))
        plan = self.choose()
        self.assertEqual(plan["entities"][0]["position"], {"x": .5, "y": 5.5})
        self.assertTrue(plan["resource_cell"])
        self.assertEqual(len(plan["entities"]), 10)
        self.assertEqual(self.factory.state["blocks"]["recipe:existing"], before)
        self.builder.can_place.assert_called_once_with(plan["entities"])

    def test_only_violating_site_reserves_nothing(self):
        self.game.query.return_value["sites"] = [{"x": 0, "y": 4}]
        before = deepcopy(self.factory.state)
        self.assertFalse(self.choose()["ok"])
        self.assertEqual(self.factory.state, before)
        self.builder.can_place.assert_not_called()

    def test_saved_paid_cell_and_output_route_are_returned_without_new_policy(self):
        key = "source:iron-plate:capacity:0"
        paid = self.factory._electric_source_plan("iron-plate", 0, 4)
        self.factory.state["blocks"][key] = paid
        self.factory.state["links"][key + ":output"] = {"entities": [paid["entities"][4]]}
        self.factory._save()
        before = deepcopy(self.factory.state), self.factory.path.read_bytes()
        self.assertIs(self.choose(key), paid)
        self.assertEqual((self.factory.state, self.factory.path.read_bytes()), before)
        self.game.query.assert_not_called()

    def test_legacy_production_is_protected_but_resource_cell_exception_remains(self):
        self.owner.pop("production_area")
        self.assertEqual(self.choose()["entities"][0]["position"]["y"], 5.5)
        self.factory.state["blocks"].pop("source:iron-plate:capacity:0")
        self.owner["entities"] = [{"name": "stone-furnace", "position": {"x": 0, "y": -1}}]
        self.owner["resource_cell"] = True
        self.assertEqual(self.choose()["entities"][0]["position"]["y"], 4.5)

    def test_transport_aisle_remains_open_but_new_support_cannot_occupy_it(self):
        clearances = mining_clearances(self.factory)
        for name in ("transport-belt", "fast-transport-belt", "underground-belt", "pipe", "pipe-to-ground"):
            entity = {"name": name, "position": {"x": .5, "y": .5}, "direction": 0}
            self.assertTrue(mining_site_clear(self.builder, [entity], clearances), name)
        pole = {"name": "small-electric-pole", "position": {"x": .5, "y": .5}, "direction": 0}
        self.assertFalse(mining_site_clear(self.builder, [pole], clearances))
        self.assertTrue(mining_site_clear(self.builder, [pole], clearances, existing=[deepcopy(pole)]))
        self.assertFalse(mining_site_clear(self.builder, [pole], clearances,
                                         existing=[{**pole, "name": "inserter"}]))


class UpgradeMiningLayoutTests(unittest.TestCase):
    def setUp(self):
        upgrade_fixture.MiningUpgradeTests.setUp(self)

    def call(self):
        return ensure_source_upgrade(self.factory, self.obs, "coal", "coal")

    def test_new_upgrade_waits_for_candidate_outside_saved_production_buffer(self):
        production(self.factory, 9.5, .5)
        self.assertIsNone(self.call())
        self.assertNotIn("source_upgrades", self.factory.state)
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.state["blocks"].pop("recipe:existing")
        self.assertEqual(self.call()["type"], "craft")
        self.assertEqual(self.factory.state["source_upgrades"]["coal"]["drill"], self.drill)

    def test_existing_upgrade_resumes_normal_paid_preparation_without_relocation(self):
        self.assertEqual(self.call()["type"], "craft")
        saved = deepcopy(self.factory.state["source_upgrades"]["coal"])
        production(self.factory, 9.5, .5)
        self.assertEqual(self.call()["type"], "craft")
        self.assertEqual(self.factory.state["source_upgrades"]["coal"], saved)


class CoalMiningLayoutTests(unittest.TestCase):
    def setUp(self):
        factory_fixture.FactoryTests.setUp(self)
        self.owner = production(self.factory, .5, 5.5)
        target = {"x": -10.5, "y": 7.5}
        belt = {"name": "transport-belt", "position": target, "direction": 4}
        bank = {"ok": True, "entities": [belt], "ports": [{"kind": "item", "item": "coal",
                "direction": "input", "position": target, "facing": 4}]}
        self.factory.state["blocks"]["energy:bank:0"] = bank
        self.obs["entities"] = [deepcopy(belt)]
        self.energy = object.__new__(EnergyExpansion)
        self.energy.game, self.energy.builder, self.energy.factory = self.game, self.builder, self.factory
        self.energy.state = {"banks": [bank], "feeds": [], "coal_links": {}}
        self.energy._managed, self.energy._save = Mock(), Mock()
        self.site = {"x": 10, "y": 6}
        self.builder.coal_sites = Mock(return_value=[self.site])
        self.builder._coal_plan = FactoryBuilder._coal_plan
        self.game.query.return_value = {"ok": True, "sites": [{"position": self.site, "remaining": 5000}]}
        # A directed paid surface route passes through the open upper aisle,
        # without touching the assembler's physical footprint or reversing a join.
        self.segments = [{"position": {"x": 7.5, "y": 7.5}, "direction": 8}]
        self.segments += [{"position": {"x": x + .5, "y": 8.5}, "direction": 12} for x in range(7, -12, -1)]
        self.segments[-1]["direction"] = 0
        self.segments.append({"position": target, "direction": 4})
        self.builder.route = Mock(return_value={"ok": True, "segments": self.segments})

    def test_new_coal_reservation_allows_transport_through_production_aisle(self):
        before = deepcopy(self.factory.state["blocks"])
        result = self.energy._reserve_feed(self.obs, 0)
        self.assertEqual(result["status"], "waiting", result)
        plan = self.energy.state["feeds"][0]["plan"]
        aisle = mining_clearances(self.factory)[1]
        routed = [{"name": "transport-belt", **row} for row in self.segments]
        self.assertTrue(self.builder._occupied_by_plan(routed) & aisle)
        self.assertTrue(all(row in plan["entities"] for row in routed))
        self.assertEqual({k: self.factory.state["blocks"][k] for k in before}, before)

    def test_coal_drill_buffer_violation_stops_before_route_and_preserves_state(self):
        self.site["x"] = 4
        before = deepcopy(self.factory.state)
        result = self.energy._reserve_feed(self.obs, 0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.energy.state["feeds"], [])
        self.assertEqual(self.factory.state, before)
        self.builder.route.assert_not_called()

    def test_new_coal_route_support_in_aisle_is_not_reserved(self):
        self.segments.append({"name": "small-electric-pole", "position": {"x": .5, "y": 7.5}, "direction": 0})
        # Keep the verified destination last, as the real crossing helper does.
        self.segments[-2], self.segments[-1] = self.segments[-1], self.segments[-2]
        result = self.energy._reserve_feed(self.obs, 0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.energy.state["feeds"], [])
        self.assertNotIn("energy:feed:0", self.factory.state["blocks"])


if __name__ == "__main__":
    unittest.main()
