import unittest
from unittest.mock import patch

from factorio_ai import planner as planner_module
from factorio_ai.blueprints import decode_blueprint_string
from factorio_ai.planner import (
    AutomationScienceSkill,
    BeltSmeltingLineSkill,
    BuildItemMallSkill,
    CircuitAutomationSkill,
    CoalFuelFeedSkill,
    CoalSupplySkill,
    CopperPlateSkill,
    ElectronicCircuitSkill,
    ExpandCopperSmeltingSkill,
    ExpandIronSmeltingSkill,
    FactoryLayoutImprovementSkill,
    GearBeltMallLogisticsSkill,
    GearBeltMallRelocationSkill,
    IronPlateLogisticLineToGearMallSkill,
    IronPlateSkill,
    ResearchAutomationSkill,
    ResearchTechnologySkill,
    SetupPowerSkill,
    SiteInputLogisticLineSkill,
    StoneSupplySkill,
    StarterDefenseSkill,
    factory_layout_issues,
    factory_layout_opportunities,
    factory_layout_simulation_candidates,
)


def base_observation():
    return {
        "tick": 1,
        "player": {"position": {"x": 0, "y": 0}},
        "inventory": {
            "coal": 10,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
        },
        "craftable": {},
        "resources": [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ],
        "entities": [],
        "research": {
            "current": None,
            "progress": 0.0,
            "technologies": {
                "automation-science-pack": {
                    "researched": True,
                    "enabled": True,
                    "research_unit_count": 1,
                    "ingredients": {},
                },
                "automation": {
                    "researched": False,
                    "enabled": True,
                    "research_unit_count": 10,
                    "ingredients": {"automation-science-pack": 1},
                }
            },
        },
    }


def power_site():
    return {
        "distance": 10,
        "layout": {
            "offshore_pump": {
                "name": "offshore-pump",
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
            },
            "boiler": {
                "name": "boiler",
                "position": {"x": 12.5, "y": 9.5},
                "direction": 0,
            },
            "steam_engine": {
                "name": "steam-engine",
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
            },
            "small_electric_pole": {
                "name": "small-electric-pole",
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
            },
        },
    }


def power_site_at(x, y, distance_value):
    return {
        "distance": distance_value,
        "layout": {
            "offshore_pump": {
                "name": "offshore-pump",
                "position": {"x": x, "y": y},
                "direction": 12,
            },
            "boiler": {
                "name": "boiler",
                "position": {"x": x + 2, "y": y - 1},
                "direction": 0,
            },
            "steam_engine": {
                "name": "steam-engine",
                "position": {"x": x + 2, "y": y - 4},
                "direction": 0,
            },
            "small_electric_pole": {
                "name": "small-electric-pole",
                "position": {"x": x, "y": y - 4},
                "direction": 0,
            },
        },
    }


class PlannerTests(unittest.TestCase):
    def test_coal_supply_places_output_belt_before_drill(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 8, "y": 0})
        self.assertEqual(decision.action["direction"], 4)

    def test_coal_supply_uses_existing_output_belt_then_places_drill(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 0, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 8, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 6, "y": 0})
        self.assertEqual(decision.action["required_resource"], "coal")

    def test_coal_supply_fuels_existing_drill(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 8, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 11)

    def test_coal_supply_stocks_drill_with_longer_fuel_reserve(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 16}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 8, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 12)

    def test_coal_supply_done_when_fueled_and_belted(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 8, "y": 0}, "direction": 4, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 12}},
            },
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIn("coal supply site is active", decision.reason)

    def test_stone_supply_places_output_chest_before_drill(self):
        obs = base_observation()
        obs["inventory"] = {"wooden-chest": 1, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        decision = StoneSupplySkill(target_count=8).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "wooden-chest")
        self.assertEqual(decision.action["position"], {"x": 8.0, "y": 0.0})

    def test_stone_supply_places_drill_after_chest(self):
        obs = base_observation()
        obs["inventory"] = {"burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {}},
        ]
        decision = StoneSupplySkill(target_count=8).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 0.0})
        self.assertEqual(decision.action["required_resource"], "stone")

    def test_stone_supply_takes_stone_from_output_chest(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "stone",
                "inventories": {"1": {"coal": 3}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"stone": 12}}},
        ]
        decision = StoneSupplySkill(target_count=16).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "stone")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_coal_fuel_feed_extends_coal_output_belt(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "burner-inserter": 1, "stone-furnace": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 7, "y": 0})

    def test_coal_fuel_feed_places_inserter_after_belt_extension(self):
        obs = base_observation()
        obs["inventory"] = {"burner-inserter": 1, "stone-furnace": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-inserter")
        self.assertEqual(decision.action["position"], {"x": 8, "y": 0})

    def test_coal_fuel_feed_places_furnace_consumer(self):
        obs = base_observation()
        obs["inventory"] = {"stone-furnace": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 23, "position": {"x": 8, "y": 0}, "direction": 12, "inventories": {"1": {"coal": 1}}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 9, "y": 0})

    def test_coal_fuel_feed_fuels_burner_inserter_before_waiting(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 23, "position": {"x": 8, "y": 0}, "direction": 12, "inventories": {}},
            {"name": "stone-furnace", "unit_number": 24, "position": {"x": 9, "y": 0}, "inventories": {}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 23)

    def test_coal_fuel_feed_done_when_consumer_has_coal(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 23, "position": {"x": 8, "y": 0}, "direction": 12, "inventories": {"1": {"coal": 1}}},
            {"name": "stone-furnace", "unit_number": 24, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIn("coal fuel feed is active", decision.reason)

    def test_coal_fuel_feed_refuels_source_drill_before_done(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 23, "position": {"x": 8, "y": 0}, "direction": 12, "inventories": {"1": {"coal": 1}}},
            {"name": "stone-furnace", "unit_number": 24, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
        ]
        decision = CoalFuelFeedSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 20)

    def test_coal_fuel_feed_extends_coal_belt_to_boiler_before_furnace_receiver(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 4, "burner-inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 3.0, "y": 0.0})
        self.assertEqual(decision.action["direction"], 4)
        self.assertIn("boiler", decision.reason)

    def test_coal_fuel_feed_places_boiler_inserter_after_belt_route(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "burner-inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 3, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 5, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-inserter")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 0.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_coal_fuel_feed_primes_boiler_feed_inserter_not_boiler(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 3, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 5, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 25, "position": {"x": 6, "y": 0}, "direction": 12, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 25)
        self.assertEqual(decision.action["name"], "burner-inserter")

    def test_coal_fuel_feed_done_when_boiler_receives_belt_fed_coal(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 3, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 5, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 25, "position": {"x": 6, "y": 0}, "direction": 12, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "working", "inventories": {"1": {"coal": 1}}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertTrue(decision.done)
        self.assertIn("boiler coal fuel feed is active", decision.reason)

    def test_factory_layout_flags_remote_manual_power_site(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}}
        obs["player"]["position"] = {"x": 0, "y": 0}
        obs["entities"] = [
            {"name": "boiler", "unit_number": 900, "position": {"x": 240, "y": 0}, "inventories": {}},
        ]
        issues = factory_layout_issues(obs)
        kinds = {item["kind"] for item in issues}
        self.assertIn("remote_power_block", kinds)
        self.assertIn("manual_power_fuel", kinds)
        decision = FactoryLayoutImprovementSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIn("layout improvement plan", decision.reason)

    def test_factory_layout_flags_power_expansion_clearance_risk(self):
        obs = base_observation()
        obs["entities"] = [
            {"name": "offshore-pump", "unit_number": 901, "position": {"x": 0, "y": 0}, "inventories": {}},
            {"name": "boiler", "unit_number": 902, "position": {"x": 2, "y": 0}, "inventories": {"1": {"coal": 2}}},
            {"name": "steam-engine", "unit_number": 903, "position": {"x": 4, "y": 0}, "inventories": {}, "electric_network_connected": True},
            {
                "name": "assembling-machine-1",
                "unit_number": 904,
                "recipe": "transport-belt",
                "position": {"x": 8, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "power_expansion_clearance_risk")

        self.assertEqual(issue["item"], "electric-power")
        self.assertEqual(issue["parameters"]["neighbor_kind"], "build_item_mall")
        self.assertLessEqual(issue["parameters"]["distance_tiles"], 12.0)
        self.assertIn("placement cost", issue["recommendation"])

    def test_factory_layout_flags_remote_starter_smelting_site(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["entities"] = complete_belt_smelting_entities(260, 0, 910, resource="copper-ore", product="copper-plate")
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 260, "y": 0}, "distance": 260}]
        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "remote_starter_site")
        self.assertEqual(issue["item"], "copper-plate")
        self.assertIn("before rail logistics", issue["detail"])

    def test_factory_layout_flags_distant_manual_site_input_after_automation(self):
        obs = powered_automation_observation()
        obs["entities"].extend(complete_belt_smelting_entities(0, 0, 920, resource="copper-ore", product="copper-plate"))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 940,
                "recipe": "automation-science-pack",
                "position": {"x": 180, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 0, "y": 0}, "distance": 0}]

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "manual_site_logistics_gap")
        self.assertEqual(issue["item"], "copper-plate")
        self.assertIn("no site-to-site route", issue["detail"])

    def test_factory_layout_flags_long_gear_mall_iron_source_when_belts_are_exhausted(self):
        obs = base_observation()
        obs["research"] = {"technologies": {"automation": {"researched": True}}}
        obs["inventory"] = {}
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 100,
                "recipe": "iron-gear-wheel",
                "position": {"x": 0.5, "y": 0.5},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 101,
                "recipe": "transport-belt",
                "position": {"x": 3.5, "y": 0.5},
                "electric_network_connected": True,
                "inventories": {"2": {"iron-gear-wheel": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 200,
                "recipe": "iron-plate",
                "position": {"x": 153.0, "y": 0.5},
                "inventories": {"2": {"iron-plate": 24}},
            },
        ]

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "distant_gear_mall_iron_source")

        self.assertEqual(issue["item"], "iron-plate")
        self.assertEqual(issue["parameters"]["gear_assembler_unit"], 100)
        self.assertEqual(issue["parameters"]["iron_source_unit"], 200)
        self.assertEqual(issue["parameters"]["source_distance_tiles"], 152.5)
        self.assertEqual(issue["parameters"]["belt_route_cost"], 153.0)
        self.assertEqual(issue["parameters"]["relocation_power_poles_estimate"], 20)
        self.assertEqual(issue["parameters"]["relocation_cost"], 58.0)
        self.assertEqual(issue["parameters"]["route_cost_preference"], "relocate_mall_to_iron_source")
        self.assertIn("route cost favors relocation", issue["detail"])

    def test_gear_belt_mall_relocation_waits_for_power_poles_before_teardown(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 1}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("needs 21 small-electric-pole", decision.reason)
        self.assertIn("before mining the existing mall", decision.reason)

    def test_gear_belt_mall_relocation_builds_power_corridor_before_teardown(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIs(decision.action["allow_nearby"], True)
        self.assertIn("power corridor before mining existing mall", decision.reason)

    def test_gear_belt_mall_relocation_detours_power_corridor_around_crash_artifact(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        first_position = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)[0]
        obs["entities"].append(
            {
                "name": "crash-site-spaceship",
                "unit_number": 999,
                "type": "container",
                "position": {"x": first_position["x"] + 5.0, "y": first_position["y"]},
                "inventories": {},
            }
        )

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIs(decision.action["allow_nearby"], True)
        self.assertNotEqual(decision.action["position"], first_position)
        self.assertGreater(
            planner_module.distance(decision.action["position"], {"x": first_position["x"] + 5.0, "y": first_position["y"]}),
            6.0,
        )
        self.assertIn("detoured", decision.reason)

    def test_gear_belt_mall_relocation_recovers_existing_assembler_when_corridor_exists(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}
        _add_existing_relocation_power_corridor(obs)

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 100)
        self.assertIn("costed relocation", decision.reason)

    def test_gear_belt_mall_relocation_does_not_require_spare_poles_after_corridor_exists(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}
        _add_existing_relocation_power_corridor(obs)

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 100)
        self.assertIn("costed relocation", decision.reason)

    def test_gear_belt_mall_relocation_rebuilds_from_inventory_after_reaching_source(self):
        obs = long_gear_mall_relocation_observation()
        _add_existing_relocation_power_corridor(obs)
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        obs["inventory"] = {"assembling-machine-1": 2}
        obs["player"]["position"] = {"x": 158.5, "y": -4.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 158.5, "y": -4.5})
        self.assertIn("place relocated gear assembler", decision.reason)

    def test_gear_belt_mall_relocation_sets_recipe_after_target_rebuild(self):
        obs = long_gear_mall_relocation_observation()
        _add_existing_relocation_power_corridor(obs)
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 800,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 158.5, "y": -4.0},
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 801,
                    "position": {"x": 161.5, "y": -4.0},
                    "inventories": {},
                },
            ]
        )
        obs["inventory"] = {}
        obs["player"]["position"] = {"x": 158.5, "y": -4.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["unit_number"], 801)
        self.assertEqual(decision.action["recipe"], "transport-belt")
        self.assertIn("set relocated belt assembler recipe", decision.reason)

    def test_gear_belt_mall_relocation_continues_after_first_assembler_recovered(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23, "assembling-machine-1": 1}
        obs["player"]["position"] = {"x": 3.5, "y": 0.5}
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("unit_number") != 100]
        for entity in obs["entities"]:
            if entity.get("unit_number") == 101:
                entity["recipe"] = "small-electric-pole"
                entity["inventories"] = {}
        _add_existing_relocation_power_corridor(obs)

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 101)
        self.assertIn("costed relocation", decision.reason)

    def test_power_pole_mall_reuses_existing_powered_assembler_before_crafting_new_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"wood": 4, "copper-cable": 8}
        obs["craftable"] = {"assembling-machine-1": 0, "iron-gear-wheel": 4}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 100,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 0.5, "y": 0.5},
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 101,
                    "recipe": "transport-belt",
                    "position": {"x": 3.5, "y": 0.5},
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 200,
                    "recipe": "iron-plate",
                    "position": {"x": 153.0, "y": 0.5},
                    "inventories": {"2": {"iron-plate": 24}},
                },
            ]
        )

        decision = BuildItemMallSkill("small-electric-pole", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "small-electric-pole")
        self.assertNotIn("iron-plate logistic line", decision.reason)

    def test_gear_mall_does_not_steal_power_pole_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["craftable"] = {}
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 100,
                "recipe": "small-electric-pole",
                "position": {"x": 0.5, "y": 0.5},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        if decision.action is not None:
            self.assertNotEqual(
                (decision.action.get("type"), decision.action.get("unit_number"), decision.action.get("recipe")),
                ("set_recipe", 100, "iron-gear-wheel"),
            )
        self.assertNotIn("set build item mall assembler recipe to iron-gear-wheel", decision.reason)

    def test_factory_layout_flags_production_on_resource_patch(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 901,
                "position": {"x": 4, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]
        obs["resources"] = [{"name": "iron-ore", "position": {"x": 4, "y": 0}}]
        issues = factory_layout_issues(obs)
        resource_issue = next(item for item in issues if item["kind"] == "resource_tile_blocked")
        self.assertEqual(resource_issue["item"], "iron-ore")

    def test_factory_layout_simulates_green_circuit_pattern_without_applying_it(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 910,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]
        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(item for item in candidates if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell")
        self.assertTrue(candidate["simulation_only"])
        self.assertTrue(candidate["not_applied"])
        self.assertEqual(candidate["validation"]["status"], "pass")
        self.assertTrue(candidate["requires_site_prebuild_gate"])
        self.assertFalse(candidate["build_ready"])
        self.assertIn("sandbox validation feedback must pass", candidate["build_ready_blockers"][0])
        task_kinds = {task["kind"] for task in candidate["prerequisite_tasks"]}
        self.assertIn("sandbox_validation", task_kinds)
        self.assertIn("supply_build_items", task_kinds)
        self.assertIn("extend_power_to_anchor", task_kinds)
        self.assertIn("connect_input_logistics", task_kinds)
        placement = candidate["site_placement_search"]
        self.assertEqual(placement["status"], "blocked")
        self.assertIn("selected_anchor", placement)
        self.assertGreater(placement["evaluated_anchors"], 1)
        site_gate = candidate["site_prebuild_gate"]
        self.assertEqual(site_gate["status"], "fail")
        self.assertFalse(site_gate["build_ready"])
        self.assertIn("build_items", site_gate["checks"])
        self.assertIn("collision", site_gate["checks"])
        self.assertIn("power_reach", site_gate["checks"])
        self.assertIn("input_logistics", site_gate["checks"])
        self.assertEqual(site_gate["checks"]["build_items"]["status"], "fail")
        self.assertEqual(site_gate["checks"]["collision"]["status"], "pass")
        self.assertEqual(site_gate["checks"]["power_reach"]["status"], "fail")
        self.assertEqual(site_gate["checks"]["input_logistics"]["status"], "fail")
        self.assertGreater(candidate["simulation"]["after"]["electronic_circuit_per_minute"], candidate["simulation"]["before"]["electronic_circuit_per_minute"])
        blueprint = candidate["blueprint"]
        self.assertEqual(blueprint["format"], "factorio-blueprint-string")
        self.assertEqual(candidate["after_blueprint"]["exchange_string"], blueprint["exchange_string"])
        self.assertEqual(candidate["before_blueprint"]["format"], "factorio-blueprint-string")
        before_entities = decode_blueprint_string(candidate["before_blueprint"]["exchange_string"])["blueprint"]["entities"]
        self.assertTrue(
            any(
                entity["name"] == "assembling-machine-1" and entity.get("recipe") == "electronic-circuit"
                for entity in before_entities
            )
        )
        decoded = decode_blueprint_string(blueprint["exchange_string"])
        entities = decoded["blueprint"]["entities"]
        self.assertTrue(
            any(
                entity["name"] == "assembling-machine-1" and entity.get("recipe") == "copper-cable"
                for entity in entities
            )
        )
        self.assertTrue(
            any(
                entity["name"] == "assembling-machine-1" and entity.get("recipe") == "electronic-circuit"
                for entity in entities
            )
        )
        inserter_directions = {
            (entity["position"]["x"], entity["position"]["y"]): entity.get("direction")
            for entity in entities
            if entity["name"] == "inserter"
        }
        self.assertEqual(inserter_directions[(-2.0, 0.0)], 12)
        self.assertEqual(inserter_directions[(2.0, 0.0)], 12)
        self.assertEqual(inserter_directions[(4.0, 1.0)], 12)
        self.assertEqual(inserter_directions[(8.0, 1.0)], 4)
        self.assertEqual(inserter_directions[(6.0, 3.0)], 0)

    def test_factory_layout_reranks_green_circuit_when_long_handed_inserter_unlocked(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 910,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]
        standard = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell"
        )

        obs["research"]["technologies"]["long-inserters"] = {"researched": True}
        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(
            item
            for item in candidates
            if item["candidate_id"] == "green-circuit-long-handed-3-cable-2-circuit-cell"
        )

        self.assertGreater(candidate["simulation"]["score"], standard["simulation"]["score"])
        self.assertEqual(candidate["validation"]["status"], "pass")
        self.assertIn("long-handed-inserter", candidate["uses_unlocked_items"])
        self.assertTrue(candidate["simulation"]["delta"]["unlock_aware_rerank"])
        decoded = decode_blueprint_string(candidate["blueprint"]["exchange_string"])
        entities = decoded["blueprint"]["entities"]
        self.assertTrue(any(entity["name"] == "long-handed-inserter" for entity in entities))

    def test_factory_layout_marks_available_modules_as_considered_not_used(self):
        obs = base_observation()
        obs["inventory"]["speed-module"] = 1
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 920,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]

        candidate = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell"
        )

        self.assertIn("speed-module", candidate["considered_unlocked_items"])
        self.assertIn("speed-module", candidate["unused_unlocked_items"])
        self.assertTrue(candidate["simulation"]["delta"]["unlock_aware_considered"])

    def test_factory_layout_reranks_when_long_handed_recipe_is_enabled_without_technology(self):
        obs = base_observation()
        obs["recipe_unlocks"] = {"long-handed-inserter": {"enabled": True}}
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 925,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]

        candidate = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"] == "green-circuit-long-handed-3-cable-2-circuit-cell"
        )

        self.assertTrue(candidate["layout_unlocks_considered"]["long_handed_inserter"]["recipe_unlocked"])
        self.assertIn("long-handed-inserter", candidate["uses_unlocked_items"])
        self.assertTrue(candidate["used_unlocked_item_state"]["long-handed-inserter"]["recipe_unlocked"])
        self.assertEqual(candidate["used_unlocked_item_state"]["long-handed-inserter"]["stock"], 0)
        self.assertFalse(candidate["used_unlocked_item_state"]["long-handed-inserter"]["automated"])
        supply = candidate["build_item_supply"]
        self.assertEqual(supply["used_unlocked_item_supply"]["long-handed-inserter"]["required"], 7)
        self.assertEqual(supply["used_unlocked_item_supply"]["long-handed-inserter"]["available"], 0)
        self.assertEqual(supply["used_unlocked_item_supply"]["long-handed-inserter"]["missing"], 7)
        self.assertIn("long-handed-inserter", supply["missing"])

    def test_factory_layout_adds_unlock_reassessment_when_long_handed_changes_site_options(self):
        obs = base_observation()
        obs["recipe_unlocks"] = {"long-handed-inserter": {"enabled": True}}
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 927,
                "recipe": "inserter",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 928,
                "position": {"x": 10, "y": -3},
                "direction": 4,
                "inventories": {},
            },
            {
                "name": "inserter",
                "unit_number": 929,
                "position": {"x": 10, "y": -2},
                "direction": 0,
                "inventories": {},
            },
        ]

        opportunities = factory_layout_opportunities(obs)
        opportunity = next(item for item in opportunities if item["kind"] == "unlock_layout_reassessment")

        self.assertIn("long-handed-inserter", opportunity["parameters"]["retool_tools"])
        self.assertIn("build_item_mall", opportunity["parameters"]["affected_site_kinds"])

    def test_factory_layout_unlock_reassessment_candidate_records_retool_tools(self):
        obs = base_observation()
        obs["recipe_unlocks"] = {
            "long-handed-inserter": {"enabled": True},
            "assembling-machine-2": {"enabled": True},
        }
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 930,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]

        candidate = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"].startswith("unlock-aware-site-rerank")
        )

        self.assertIn("long-handed-inserter", candidate["uses_unlocked_items"])
        self.assertIn("assembling-machine-2", candidate["uses_unlocked_items"])
        self.assertTrue(candidate["simulation"]["delta"]["unlock_aware_rerank"])
        self.assertTrue(candidate["simulation"]["delta"]["requires_bottleneck_recheck"])

    def test_factory_layout_uses_higher_tier_assembler_when_recipe_is_enabled(self):
        obs = base_observation()
        obs["recipe_unlocks"] = {"assembling-machine-2": {"enabled": True}}
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 926,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        ]

        candidate = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell"
        )

        self.assertIn("assembling-machine-2", candidate["uses_unlocked_items"])
        self.assertEqual(candidate["simulation"]["after"]["assembler_tier"], "assembling-machine-2")
        decoded = decode_blueprint_string(candidate["blueprint"]["exchange_string"])
        entities = decoded["blueprint"]["entities"]
        self.assertTrue(any(entity["name"] == "assembling-machine-2" for entity in entities))

    def test_factory_layout_uses_long_handed_starter_mall_variant_after_unlock(self):
        obs = base_observation()
        obs["research"]["technologies"]["long-inserters"] = {"researched": True}
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 930 + index,
                "recipe": recipe,
                "position": {"x": index * 16, "y": index * 2},
                "electric_network_connected": True,
                "inventories": {},
            }
            for index, recipe in enumerate(
                ["transport-belt", "inserter", "burner-inserter", "stone-furnace", "small-electric-pole"]
            )
        ]

        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(item for item in candidates if item["candidate_id"] == "starter-mall-row-long-handed-inputs")

        self.assertIn("long-handed-inserter", candidate["uses_unlocked_items"])
        self.assertIn("long-handed-inserter", candidate["used_unlocked_item_state"])
        self.assertTrue(candidate["simulation"]["delta"]["unlock_aware_rerank"])
        self.assertEqual(candidate["simulation"]["after"]["shared_input_lanes"], 2)
        self.assertIn("build_item_supply", candidate)
        self.assertIn("long-handed-inserter", candidate["build_item_supply"]["used_unlocked_item_supply"])
        decoded = decode_blueprint_string(candidate["blueprint"]["exchange_string"])
        entities = decoded["blueprint"]["entities"]
        self.assertTrue(any(entity["name"] == "long-handed-inserter" for entity in entities))

    def test_green_circuit_placement_search_finds_clear_powered_anchor(self):
        obs = base_observation()
        obs["inventory"].update(
            {
                "assembling-machine-1": 5,
                "inserter": 12,
                "iron-chest": 2,
                "small-electric-pole": 2,
                "transport-belt": 39,
                "iron-plate": 20,
                "copper-plate": 20,
            }
        )
        obs["resources"] = []
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 910,
                "recipe": "electronic-circuit",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "big-electric-pole",
                "unit_number": 920,
                "position": {"x": 12, "y": 22},
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(item for item in candidates if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell")

        self.assertFalse(candidate["build_ready"])
        self.assertEqual(candidate["build_ready_blockers"], ["sandbox validation feedback must pass before build-ready"])
        placement = candidate["site_placement_search"]
        self.assertEqual(placement["status"], "found")
        self.assertEqual(placement["selected_anchor"], {"x": 10.0, "y": 8.0})
        self.assertTrue(placement["top_candidates"][0]["placement_ready"])
        site_gate = candidate["site_prebuild_gate"]
        self.assertEqual(site_gate["status"], "pass")
        self.assertTrue(site_gate["build_ready"])
        self.assertEqual({key: row["status"] for key, row in site_gate["checks"].items()}, {
            "build_items": "pass",
            "collision": "pass",
            "resource_preservation": "pass",
            "power_reach": "pass",
            "input_logistics": "pass",
        })

    def test_green_circuit_before_blueprint_does_not_pull_adjacent_power_site(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 910,
                "recipe": "copper-cable",
                "position": {"x": 10, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 911,
                "recipe": "electronic-circuit",
                "position": {"x": 14, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 930, "position": {"x": 10, "y": 10}, "inventories": {"1": {"coal": 5}}},
            {"name": "steam-engine", "unit_number": 931, "position": {"x": 10, "y": 13}, "inventories": {}},
            {"name": "offshore-pump", "unit_number": 932, "position": {"x": 8, "y": 10}, "inventories": {}},
        ]
        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(item for item in candidates if item["candidate_id"] == "green-circuit-3-cable-2-circuit-cell")
        before_entities = decode_blueprint_string(candidate["before_blueprint"]["exchange_string"])["blueprint"]["entities"]
        names = {entity["name"] for entity in before_entities}
        self.assertIn("assembling-machine-1", names)
        self.assertNotIn("boiler", names)
        self.assertNotIn("steam-engine", names)
        self.assertNotIn("offshore-pump", names)

    def test_smelting_before_blueprint_uses_compact_representative_cluster(self):
        obs = base_observation()
        obs["entities"] = []
        for base_x in (0, 400):
            obs["entities"].extend(
                [
                    {"name": "burner-mining-drill", "unit_number": base_x + 1, "position": {"x": base_x - 2, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "unit_number": base_x + 2, "position": {"x": base_x, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "transport-belt", "unit_number": base_x + 3, "position": {"x": base_x + 1, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "burner-inserter", "unit_number": base_x + 4, "position": {"x": base_x + 2, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "unit_number": base_x + 5, "position": {"x": base_x + 3, "y": 0}, "inventories": {"1": {"coal": 1}, "2": {"iron-ore": 1}}},
                ]
            )
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": -2, "y": 0}, "distance": 2},
            {"name": "iron-ore", "position": {"x": 398, "y": 0}, "distance": 398},
        ]
        candidates = factory_layout_simulation_candidates(obs)
        candidate = next(item for item in candidates if item["candidate_id"] == "iron-plate-stone-furnace-parallel-smelting-columns")
        before_entities = decode_blueprint_string(candidate["before_blueprint"]["exchange_string"])["blueprint"]["entities"]
        xs = [float(entity["position"]["x"]) for entity in before_entities]
        self.assertLess(max(xs) - min(xs), 80.0)

    def test_factory_layout_uses_higher_tier_furnace_when_recipe_is_enabled(self):
        obs = base_observation()
        obs["recipe_unlocks"] = {"steel-furnace": {"enabled": True}}
        obs["entities"] = []
        for base_x in (0, 400):
            obs["entities"].extend(
                [
                    {"name": "burner-mining-drill", "unit_number": base_x + 1, "position": {"x": base_x - 2, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "unit_number": base_x + 2, "position": {"x": base_x, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "transport-belt", "unit_number": base_x + 3, "position": {"x": base_x + 1, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "burner-inserter", "unit_number": base_x + 4, "position": {"x": base_x + 2, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "unit_number": base_x + 5, "position": {"x": base_x + 3, "y": 0}, "inventories": {"1": {"coal": 1}, "2": {"iron-ore": 1}}},
                ]
            )
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": -2, "y": 0}, "distance": 2},
            {"name": "iron-ore", "position": {"x": 398, "y": 0}, "distance": 398},
        ]

        candidate = next(
            item
            for item in factory_layout_simulation_candidates(obs)
            if item["candidate_id"] == "iron-plate-steel-furnace-parallel-smelting-columns"
        )

        self.assertIn("steel-furnace", candidate["uses_unlocked_items"])
        self.assertEqual(candidate["simulation"]["after"]["furnace_tier"], "steel-furnace")
        self.assertGreater(candidate["simulation"]["delta"]["plate_per_minute"], 0)

    def test_factory_layout_simulates_belt_capacity_bottleneck(self):
        obs = base_observation()
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": i, "position": {"x": i * 2, "y": 0}, "inventories": {"1": {"coal": 1}}}
            for i in range(40)
        ] + [
            {"name": "transport-belt", "position": {"x": 4, "y": 0}, "inventories": {}},
            {"name": "stone-furnace", "unit_number": 999, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
        ]
        obs["resources"] = [{"name": "iron-ore", "position": {"x": i * 2, "y": 0}} for i in range(40)]
        candidates = factory_layout_simulation_candidates(obs)
        belt_candidates = [item for item in candidates if item["candidate_id"] == "belt-capacity-iron-ore"]
        self.assertTrue(belt_candidates)
        self.assertTrue(belt_candidates[0]["simulation"]["delta"]["prevents_transport_bottleneck"])

    def test_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["iron-plate"] = 10
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_places_burner_mining_drill_first(self):
        decision = IronPlateSkill(target_count=10).next_action(base_observation())
        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertTrue(decision.action["allow_nearby"])
        self.assertEqual(decision.action["required_resource"], "iron-ore")

    def test_moves_next_to_ore_before_building_distant_drill(self):
        obs = base_observation()
        obs["resources"][0]["position"] = {"x": 100, "y": 0}
        obs["resources"][0]["distance"] = 100
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 102.0, "y": 0.0})

    def test_iron_skill_ignores_drills_not_on_iron_patch(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 501,
                "position": {"x": 2, "y": 0},
                "inventories": {"1": {"coal": 4}},
            }
        ]
        obs["resources"] = [
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
            {"name": "iron-ore", "position": {"x": 40, "y": 0}, "distance": 40},
        ]
        decision = IronPlateSkill(target_count=5).next_action(obs, target_count=5, inventory_only=True)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertIn("iron ore", decision.reason)

    def test_iron_skill_refuses_hand_smelting_when_no_drill_is_available(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 40, "y": 0}, "distance": 40},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        decision = IronPlateSkill(target_count=5).next_action(obs, target_count=5, inventory_only=True)
        self.assertIsNone(decision.action)
        self.assertNotIn("hand-smelting", decision.reason)
        self.assertNotIn("iron-ore", decision.reason)

    def test_iron_skill_completes_direct_furnace_instead_of_hand_mining_ore(self):
        obs = base_observation()
        obs["inventory"].pop("burner-mining-drill")
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 5, "y": 0},
                "distance": 5,
                "inventories": {},
            },
        ]
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 0.0})

    def test_iron_skill_waits_for_direct_furnace_instead_of_hand_mining_ore(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 4,
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {"1": {"coal": 3}},
            },
        ]
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "wait")
        self.assertNotIn("iron ore", decision.reason.lower())

    def test_iron_skill_uses_wood_fuel_before_mining_coal(self):
        obs = base_observation()
        obs["inventory"] = {"wood": 5}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 4,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
        ]

        decision = IronPlateSkill(target_count=10).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "wood")
        self.assertNotEqual(decision.action.get("name"), "coal")

    def test_iron_skill_refuels_direct_cell_before_taking_output_for_total_target(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 4, "y": 0}}
        obs["inventory"] = {"wood": 5}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 0,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 2,
                "inventories": {"3": {"iron-plate": 5}},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "wood")
        self.assertEqual(decision.action["unit_number"], 101)

    def test_iron_skill_inventory_only_can_take_furnace_output(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 6, "y": 0}}
        obs["inventory"] = {"wood": 5}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 2,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 0,
                "inventories": {"3": {"iron-plate": 5}},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs, target_count=20, inventory_only=True)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 102)

    def test_iron_skill_does_not_mix_wood_into_furnace_with_existing_coal(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 6, "y": 0}}
        obs["inventory"] = {"wood": 5}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 2,
                "inventories": {"1": {"wood": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 0,
                "inventories": {"1": {"coal": 2}, "3": {"iron-plate": 5}},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("before mixing burner fuel", decision.reason)

    def test_iron_skill_ignores_distant_empty_furnace(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 102, "y": 0}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["resources"][0]["position"] = {"x": 100, "y": 0}
        obs["resources"][0]["distance"] = 2
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 100, "y": 0},
                "distance": 2,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 0, "y": 0},
                "distance": 102,
                "inventories": {},
            },
        ]
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 102.0, "y": 0.0})

    def test_iron_skill_ignores_remote_furnace_output_before_rail(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1, "burner-mining-drill": 1}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 910,
                "position": {"x": 420, "y": -400},
                "distance": 580,
                "inventories": {"2": {"iron-plate": 20}},
            }
        ]
        decision = IronPlateSkill(target_count=30).next_action(obs)
        self.assertNotEqual(decision.action.get("position"), {"x": 420.0, "y": -400.0})
        if decision.action["type"] == "move_to":
            self.assertLess(abs(decision.action["position"]["x"]), 50)

    def test_science_skill_crafts_science_when_prerequisites_exist(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "iron-gear-wheel": 1,
            "copper-plate": 1,
        }
        obs["craftable"] = {"automation-science-pack": 1}
        decision = AutomationScienceSkill(target_count=1).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "automation-science-pack")

    def test_science_skill_replenishes_inventory_iron_before_missing_gears(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 1, "coal": 8, "burner-mining-drill": 1, "stone-furnace": 1}
        obs["craftable"] = {}
        decision = AutomationScienceSkill(target_count=5).next_action(obs)
        self.assertIn(decision.action["type"], {"build", "insert", "take", "wait", "move_to"})
        self.assertNotIn("missing iron gear wheels", decision.reason)

    def test_science_skill_builds_direct_copper_drill_before_furnace(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "coal": 8,
        }
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 201,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"2": {"iron-plate": 1}},
            }
        ]
        decision = AutomationScienceSkill(target_count=1).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["required_resource"], "copper-ore")

    def test_science_skill_takes_copper_from_furnace_before_waiting(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "iron-gear-wheel": 1,
            "coal": 8,
            "automation-science-pack": 4,
        }
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 201,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"3": {"copper-plate": 4}},
            }
        ]
        decision = AutomationScienceSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-plate")

    def test_copper_skill_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["copper-plate"] = 10
        decision = CopperPlateSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_copper_skill_starts_direct_smelting_cell_instead_of_hand_mining_ore(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 8,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
        }
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["required_resource"], "copper-ore")
        self.assertNotEqual(decision.action.get("type"), "mine")

    def test_copper_skill_places_furnace_at_direct_drill_output(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 701,
                "position": {"x": 8, "y": 0},
                "direction": 4,
                "distance": 8,
                "mining_target": "copper-ore",
                "inventories": {"1": {"coal": 3}},
            }
        ]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})
        self.assertFalse(decision.action["allow_nearby"])

    def test_direct_smelting_layout_keeps_furnace_touching_drill_output(self):
        cases = {
            "east": {"x": 10.0, "y": 0.0},
            "west": {"x": 6.0, "y": 0.0},
            "south": {"x": 8.0, "y": 2.0},
            "north": {"x": 8.0, "y": -2.0},
        }
        for orientation, expected in cases.items():
            with self.subTest(orientation=orientation):
                layout = planner_module._direct_smelting_layout_from_drill_position(
                    {"x": 8, "y": 0},
                    "copper-ore",
                    orientation=orientation,
                )
                self.assertEqual(layout["furnace_position"], expected)

    def test_copper_skill_does_not_accept_one_tile_gap_direct_furnace(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 701,
                "position": {"x": 8, "y": 0},
                "direction": 4,
                "distance": 8,
                "mining_target": "copper-ore",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 702,
                "position": {"x": 11, "y": 0},
                "distance": 11,
                "inventories": {"1": {"coal": 3}},
            },
        ]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})
        self.assertFalse(decision.action["allow_nearby"])

    def test_copper_skill_waits_for_direct_cell_instead_of_hand_mining_ore(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 701,
                "position": {"x": 8, "y": 0},
                "direction": 4,
                "distance": 8,
                "mining_target": "copper-ore",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 702,
                "position": {"x": 10, "y": 0},
                "distance": 10,
                "inventories": {"1": {"coal": 3}},
            },
        ]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "wait")

    def test_copper_skill_uses_belt_line_after_belt_automation_is_ready(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "coal": 8,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})

    def test_copper_skill_places_missing_burner_drill_on_copper_line_after_belt_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"coal": 8, "burner-mining-drill": 1}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        obs["entities"].extend(complete_belt_smelting_entities(8, 0, 700, resource="copper-ore", product="copper-plate"))
        obs["entities"] = [entity for entity in obs["entities"] if entity["name"] != "burner-mining-drill"]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["required_resource"], "copper-ore")

    def test_copper_skill_ignores_remote_furnace_output_before_rail(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1, "burner-mining-drill": 1}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 911,
                "position": {"x": 420, "y": -400},
                "distance": 580,
                "inventories": {"2": {"copper-plate": 20}},
            }
        ]
        decision = CopperPlateSkill(target_count=30).next_action(obs)
        self.assertNotEqual(decision.action.get("position"), {"x": 420.0, "y": -400.0})
        if decision.action["type"] == "move_to":
            self.assertLess(abs(decision.action["position"]["x"]), 50)

    def test_electronic_circuit_skill_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["electronic-circuit"] = 5
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_electronic_circuit_skill_crafts_circuits_when_ready(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "copper-cable": 15}
        obs["craftable"] = {"electronic-circuit": 5}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "electronic-circuit")

    def test_electronic_circuit_skill_crafts_cable_before_circuit(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "copper-plate": 8}
        obs["craftable"] = {"copper-cable": 8}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "copper-cable")

    def test_electronic_circuit_skill_takes_iron_from_furnace_for_inventory_target(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "copper-cable": 15}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 401,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"3": {"iron-plate": 5}},
            }
        ]
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")

    def test_electronic_circuit_skill_requests_copper_plate_when_cable_cannot_be_crafted(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "coal": 8, "stone-furnace": 1, "burner-mining-drill": 1}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertIn(decision.action["type"], {"build", "mine", "move_to", "insert", "take", "wait"})

    def test_belt_smelting_skill_crafts_belt_when_missing(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "iron-plate": 2,
            "iron-gear-wheel": 1,
        }
        obs["craftable"] = {"transport-belt": 1}
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "transport-belt")

    def test_belt_smelting_skill_uses_belt_mall_instead_of_handcrafting_gear_after_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "iron-plate": 3,
            "electronic-circuit": 7,
        }
        obs["craftable"] = {"iron-gear-wheel": 5, "transport-belt": 1}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"copper-plate": 4}))

        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)

        self.assertFalse(
            decision.action["type"] == "craft"
            and decision.action.get("recipe") in {"iron-gear-wheel", "transport-belt"}
        )
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")

    def test_belt_smelting_skill_refuses_gear_handcraft_when_mall_reports_done_after_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "iron-plate": 3,
            "electronic-circuit": 7,
        }
        obs["craftable"] = {"iron-gear-wheel": 5, "transport-belt": 1}

        class DoneGearMall:
            def __init__(self, *args, **kwargs):
                pass

            def next_action(self, *args, **kwargs):
                return planner_module.PlannerDecision(None, "gear mall target reached", done=True)

        with patch("factorio_ai.planner.BuildItemMallSkill", DoneGearMall):
            decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("refusing hand-crafted iron gears after Automation", decision.reason)

    def test_belt_smelting_skill_places_belt_first_when_parts_exist(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["direction"], 4)

    def test_belt_smelting_skill_places_inserter_with_pickup_from_belt(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
        }
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 502,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-inserter")
        self.assertEqual(decision.action["direction"], 12)

    def test_belt_smelting_skill_done_after_line_outputs_plates(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 505,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
            {
                "name": "burner-inserter",
                "unit_number": 502,
                "position": {"x": 8, "y": 0},
                "distance": 8,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 503,
                "position": {"x": 9, "y": 0},
                "distance": 9,
                "inventories": {"3": {"iron-plate": 10}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 504,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_belt_smelting_skill_done_after_line_starts_and_inventory_has_target(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12, "iron-plate": 10}
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 505,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
            {
                "name": "burner-inserter",
                "unit_number": 502,
                "position": {"x": 8, "y": 0},
                "distance": 8,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 503,
                "position": {"x": 9, "y": 0},
                "distance": 9,
                "inventories": {"2": {"iron-ore": 1}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 504,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_expand_iron_smelting_places_new_belt_when_below_capacity(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")

    def test_expand_iron_smelting_done_when_capacity_target_reached(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True) + complete_belt_smelting_entities(
            14,
            0,
            600,
            reserve_fuel=True,
        )
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_expand_iron_smelting_does_not_count_copper_lines_as_iron_capacity(self):
        obs = base_observation()
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        obs["resources"] = []
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertFalse(decision.done)
        self.assertIn("iron-ore", decision.reason)

    def test_expand_iron_smelting_fuels_complete_line_before_counting_capacity(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["name"], "burner-inserter")

    def test_expand_smelting_refuels_low_reserve_before_reporting_capacity_done(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 16}
        obs["entities"] = complete_belt_smelting_entities(
            8,
            0,
            700,
            resource="copper-ore",
            product="copper-plate",
            reserve_fuel=True,
        )
        obs["entities"].extend(
            complete_belt_smelting_entities(
                18,
                0,
                800,
                resource="copper-ore",
                product="copper-plate",
                reserve_fuel=True,
            )
        )
        for entity in obs["entities"]:
            if entity["name"] == "stone-furnace" and entity["unit_number"] == 704:
                entity["inventories"]["1"] = {"coal": 1}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["count"], 7)

    def test_expand_smelting_uses_remaining_coal_before_mining_more(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
            if entity["name"] == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 1}}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertIn(decision.action["name"], {"burner-inserter", "stone-furnace"})

    def test_expand_smelting_mines_coal_only_when_empty(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "coal")

    def test_expand_smelting_recovers_surplus_coal_before_far_fuel_logistics(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 300, "y": 0}, "distance": 300},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        obs["entities"].append(
            {
                "name": "boiler",
                "unit_number": 900,
                "position": {"x": 0, "y": 5},
                "distance": 5,
                "inventories": {"1": {"coal": 12}},
            }
        )
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
            if entity["name"] == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 6}, "2": {"copper-ore": 1}}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["name"], "boiler")
        self.assertIn("surplus coal", decision.reason)

    def test_expand_smelting_does_not_ping_pong_fuel_inside_same_line(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 300, "y": 0}, "distance": 300},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {"1": {"coal": 6}}
            if entity["name"] == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 1}, "2": {"copper-ore": 1}}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("fuel logistics", decision.reason)

    def test_expand_smelting_does_not_steal_fuel_from_adjacent_smelting_line(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "copper-ore", "position": {"x": 4, "y": 3}, "distance": 5},
            {"name": "coal", "position": {"x": 300, "y": 0}, "distance": 300},
        ]
        obs["entities"] = complete_belt_smelting_entities(
            4,
            0,
            500,
            resource="copper-ore",
            product="copper-plate",
            reserve_fuel=True,
        )
        obs["entities"].extend(
            complete_belt_smelting_entities(4, 3, 600, resource="copper-ore", product="copper-plate")
        )
        for entity in obs["entities"]:
            if entity["unit_number"] == 600:
                entity["inventories"] = {"1": {"coal": 1}}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("fuel logistics", decision.reason)

    def test_expand_smelting_mines_batch_coal_before_long_refuel_trip(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 270, "y": 0}}
        obs["inventory"] = {"coal": 2}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 266},
            {"name": "coal", "position": {"x": 270, "y": 0}, "distance": 0},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "coal")
        self.assertEqual(decision.action["count"], 16)

    def test_expand_smelting_mines_walkable_coal_instead_of_tiny_surplus_trips(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}}
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 80, "y": 0}, "distance": 80},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
            if entity["name"] == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 4}, "2": {"copper-ore": 1}}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 80, "y": 0})
        self.assertEqual(decision.action["tolerance"], 7.5)
        self.assertIn("move near coal", decision.reason)

    def test_expand_smelting_stops_when_fuel_logistics_are_too_far(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 300, "y": 0}, "distance": 300},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("fuel logistics", decision.reason)

    def test_expansion_uses_base_local_resource_when_agent_is_at_remote_patch(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["player"] = {"position": {"x": 620, "y": -420}}
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 6, "y": 0}, "distance": 744},
            {"name": "copper-ore", "position": {"x": 620, "y": -420}, "distance": 0},
            {"name": "coal", "position": {"x": 0, "y": 4}, "distance": 748},
        ]
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertLess(abs(decision.action["position"]["x"]), 20)
        self.assertLess(abs(decision.action["position"]["y"]), 20)

    def test_expansion_ignores_remote_incomplete_line_before_rail_outposts(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["player"] = {"position": {"x": 620, "y": -420}}
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 6, "y": 0}, "distance": 744},
            {"name": "copper-ore", "position": {"x": 620, "y": -420}, "distance": 0},
            {"name": "coal", "position": {"x": 0, "y": 4}, "distance": 748},
        ]
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 900,
                "position": {"x": 622, "y": -420},
                "direction": 4,
                "distance": 2,
                "inventories": {},
            }
        ]
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertLess(abs(decision.action["position"]["x"]), 20)
        self.assertLess(abs(decision.action["position"]["y"]), 20)

    def test_expansion_refuses_remote_only_starter_resource_without_rail(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["player"] = {"position": {"x": 620, "y": -420}}
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 620, "y": -420}, "distance": 0},
            {"name": "coal", "position": {"x": 0, "y": 4}, "distance": 748},
        ]
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("cannot find open copper-ore site", decision.reason)

    def test_expand_iron_smelting_clears_blocking_rock(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["entities"] = [
            {
                "name": "huge-rock",
                "type": "simple-entity",
                "position": {"x": 6, "y": 0},
                "distance": 6,
            }
        ]
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "huge-rock")

    def test_expand_copper_smelting_places_new_belt_on_copper(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})

    def test_expand_copper_smelting_done_when_capacity_target_reached(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"] = complete_belt_smelting_entities(
            8,
            0,
            700,
            resource="copper-ore",
            product="copper-plate",
            reserve_fuel=True,
        )
        obs["entities"].extend(
            complete_belt_smelting_entities(
                18,
                0,
                800,
                resource="copper-ore",
                product="copper-plate",
                reserve_fuel=True,
            )
        )
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_expand_copper_smelting_ignores_incomplete_iron_line(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 901,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "distance": 6,
                "inventories": {},
            }
        ]
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})

    def test_expand_copper_smelting_requires_copper_for_new_drill(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12, "burner-mining-drill": 1}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ]
        obs["entities"] = complete_belt_smelting_entities(8, 0, 700, resource="copper-ore", product="copper-plate")
        obs["entities"] = [entity for entity in obs["entities"] if entity["name"] != "burner-mining-drill"]
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["required_resource"], "copper-ore")

    def test_starter_defense_places_turret_on_factory_perimeter_toward_nearest_enemy(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 66, "y": 0}}
        obs["inventory"] = {"gun-turret": 1, "firearm-magazine": 10}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 949,
                "position": {"x": 50, "y": 0},
                "distance": 16,
                "inventories": {},
            }
        ]
        obs["enemies"] = [{"name": "small-biter", "type": "unit", "position": {"x": 100, "y": 0}, "distance": 34}]
        decision = StarterDefenseSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "gun-turret")
        self.assertEqual(decision.action["position"], {"x": 68.0, "y": 0.0})

    def test_starter_defense_arms_existing_turret(self):
        obs = base_observation()
        obs["inventory"] = {"firearm-magazine": 10}
        obs["enemies"] = [{"name": "small-biter", "type": "unit", "position": {"x": -20, "y": 0}, "distance": 20}]
        obs["entities"] = [
            {
                "name": "gun-turret",
                "unit_number": 950,
                "position": {"x": -8, "y": 0},
                "distance": 8,
                "inventories": {},
            }
        ]
        decision = StarterDefenseSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "firearm-magazine")
        self.assertEqual(decision.action["unit_number"], 950)

    def test_starter_defense_done_when_turret_is_armed(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["enemies"] = [{"name": "small-biter", "type": "unit", "position": {"x": -20, "y": 0}, "distance": 20}]
        obs["entities"] = [
            {
                "name": "gun-turret",
                "unit_number": 951,
                "position": {"x": -8, "y": 0},
                "distance": 8,
                "inventories": {"1": {"firearm-magazine": 5}},
            }
        ]
        decision = StarterDefenseSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_blocking_obstacle_skips_starter_crash_site_artifact(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["entities"] = [
            {
                "name": "crash-site-spaceship-wreck-big-1",
                "type": "simple-entity",
                "position": {"x": 4, "y": 0},
                "distance": 4,
            }
        ]

        blocker = planner_module._blocking_obstacle_near(obs, {"x": 4, "y": 0})

        self.assertIsNone(blocker)

    def test_setup_power_skill_places_offshore_pump_first_when_parts_exist(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 10,
            "offshore-pump": 1,
            "boiler": 1,
            "steam-engine": 1,
            "small-electric-pole": 1,
        }
        obs["power_sites"] = [power_site()]
        decision = SetupPowerSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "offshore-pump")
        self.assertEqual(decision.action["direction"], 12)

    def test_setup_power_skill_prefers_base_local_water_site(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {
            "coal": 10,
            "offshore-pump": 1,
            "boiler": 1,
            "steam-engine": 1,
            "small-electric-pole": 1,
        }
        obs["power_sites"] = [
            power_site_at(320.5, -240.5, 400),
            power_site_at(10.5, 10.5, 15),
        ]
        decision = SetupPowerSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "offshore-pump")
        self.assertLess(decision.action["position"]["x"], 40)

    def test_setup_power_skill_refuses_remote_water_site_when_no_local_site_exists(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["player"] = {"position": {"x": 190.5, "y": -15.5}}
        obs["inventory"] = {
            "coal": 10,
            "offshore-pump": 1,
            "boiler": 1,
            "steam-engine": 1,
            "small-electric-pole": 1,
        }
        obs["power_sites"] = [
            power_site_at(420.5, -240.5, 485),
            power_site_at(190.5, -20.5, 191),
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertIsNone(decision.action)
        self.assertFalse(decision.done)
        self.assertIn("cannot use remote water", decision.reason)

    def test_setup_power_skill_skips_already_built_pump_from_selected_site(self):
        obs = base_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["player"] = {"position": {"x": 10.5, "y": 10.5}}
        obs["inventory"] = {
            "coal": 10,
            "offshore-pump": 1,
            "boiler": 1,
            "steam-engine": 1,
            "small-electric-pole": 1,
        }
        obs["power_sites"] = [power_site_at(10.5, 10.5, 15)]
        obs["entities"] = [
            {
                "name": "offshore-pump",
                "unit_number": 258,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 6,
                "inventories": {},
                "fluids": {},
            }
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "boiler")
        self.assertEqual(decision.action["position"], {"x": 12.5, "y": 9.5})

    def test_setup_power_skill_mines_tree_when_pole_needs_wood(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 10,
            "offshore-pump": 1,
            "boiler": 1,
            "steam-engine": 1,
            "copper-cable": 2,
        }
        obs["power_sites"] = [power_site()]
        obs["entities"] = [
            {
                "name": "tree-07",
                "type": "tree",
                "position": {"x": 3, "y": 0},
                "distance": 3,
            }
        ]
        decision = SetupPowerSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "tree-07")

    def test_setup_power_skill_uses_wood_before_mining_coal_for_boiler(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 12.5, "y": 10.0}}
        obs["inventory"] = {"wood": 8}
        obs["entities"] = [
            {
                "name": "offshore-pump",
                "unit_number": 601,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 2,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 602,
                "position": {"x": 12.5, "y": 10.0},
                "direction": 0,
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "wood")
        self.assertEqual(decision.action["count"], 8)
        self.assertEqual(decision.action["unit_number"], 602)

    def test_setup_power_skill_fills_boiler_fuel_reserve_when_wood_is_available(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 12.5, "y": 10.0}}
        obs["inventory"] = {"wood": 16}
        obs["entities"] = [
            {
                "name": "offshore-pump",
                "unit_number": 601,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 2,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 602,
                "position": {"x": 12.5, "y": 10.0},
                "direction": 0,
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "wood")
        self.assertEqual(decision.action["count"], 10)
        self.assertEqual(decision.action["unit_number"], 602)

    def test_setup_power_skill_takes_surplus_fuel_for_bounded_emergency_when_feed_materials_missing(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 9.5}}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 9.5}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 620, "position": {"x": 0, "y": 9.5}, "direction": 4, "inventories": {"1": {"coal": 9}}},
            {"name": "transport-belt", "unit_number": 621, "position": {"x": 2, "y": 9.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {
                "name": "offshore-pump",
                "unit_number": 601,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 2,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 602,
                "position": {"x": 12.5, "y": 9.5},
                "direction": 0,
                "status_name": "no_fuel",
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 605,
                "recipe": "small-electric-pole",
                "position": {"x": 11, "y": 7},
                "status_name": "no_power",
                "inventories": {},
            },
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 620)
        self.assertLessEqual(decision.action["count"], 5)
        self.assertTrue(decision.action["emergency_bootstrap"])
        self.assertIn("one-time emergency boiler bootstrap", decision.reason)

    def test_setup_power_skill_inserts_carried_emergency_fuel_into_boiler(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 12.5, "y": 10.0}}
        obs["inventory"] = {"coal": 5}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 9.5}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 620, "position": {"x": 0, "y": 9.5}, "direction": 4, "inventories": {"1": {"coal": 4}}},
            {"name": "transport-belt", "unit_number": 621, "position": {"x": 2, "y": 9.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {
                "name": "offshore-pump",
                "unit_number": 601,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 2,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 602,
                "position": {"x": 12.5, "y": 9.5},
                "direction": 0,
                "status_name": "no_fuel",
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 605,
                "recipe": "small-electric-pole",
                "position": {"x": 11, "y": 7},
                "status_name": "no_power",
                "inventories": {},
            },
        ]

        decision = SetupPowerSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 602)
        self.assertEqual(decision.action["count"], 5)
        self.assertTrue(decision.action["emergency_bootstrap"])
        self.assertIn("one-time emergency boiler fuel bootstrap", decision.reason)

    def test_setup_power_skill_done_when_engine_has_steam_and_pole_connected(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "offshore-pump",
                "unit_number": 601,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 12,
                "distance": 10,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 602,
                "position": {"x": 12.5, "y": 10},
                "direction": 0,
                "distance": 12,
                "inventories": {"1": {"coal": 10}},
                "fluids": {
                    "1": {"name": "water", "amount": 200},
                    "2": {"name": "steam", "amount": 20},
                },
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 6.5},
                "direction": 0,
                "status": 1,
                "distance": 13,
                "inventories": {},
                "fluids": {"1": {"name": "steam", "amount": 80}},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 6.5},
                "direction": 0,
                "distance": 12,
                "inventories": {},
                "fluids": {},
            },
        ]
        decision = SetupPowerSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_setup_power_skill_recognizes_non_west_pump_layout(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "offshore-pump",
                "unit_number": 611,
                "position": {"x": 10.5, "y": 10.5},
                "direction": 4,
                "distance": 10,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 100}},
            },
            {
                "name": "boiler",
                "unit_number": 612,
                "position": {"x": 8.5, "y": 11.5},
                "direction": 8,
                "distance": 12,
                "inventories": {"1": {"coal": 10}},
                "fluids": {
                    "1": {"name": "water", "amount": 200},
                    "2": {"name": "steam", "amount": 20},
                },
            },
            {
                "name": "steam-engine",
                "unit_number": 613,
                "position": {"x": 8.5, "y": 14.5},
                "direction": 8,
                "status": 1,
                "distance": 13,
                "inventories": {},
                "fluids": {"1": {"name": "steam", "amount": 80}},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 614,
                "position": {"x": 10.5, "y": 14.5},
                "direction": 0,
                "distance": 12,
                "inventories": {},
                "fluids": {},
            },
        ]
        decision = SetupPowerSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_research_automation_done_when_technology_researched(self):
        obs = powered_research_observation()
        obs["research"]["technologies"]["automation"]["researched"] = True
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_research_automation_sets_current_research_after_power(self):
        obs = powered_research_observation()
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "research")
        self.assertEqual(decision.action["technology"], "automation")

    def test_research_automation_builds_lab_before_selecting_research(self):
        obs = powered_research_observation()
        obs["inventory"]["lab"] = 1
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "lab_position": {"x": 13.5, "y": 6.5},
                "distance": 3,
            }
        ]
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "lab")

    def test_research_automation_unlocks_science_pack_trigger_after_lab(self):
        obs = powered_research_observation()
        obs["research"]["technologies"]["automation-science-pack"]["researched"] = False
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "research")
        self.assertEqual(decision.action["technology"], "automation-science-pack")

    def test_research_automation_repairs_unpowered_lab(self):
        obs = powered_research_observation()
        obs["inventory"]["small-electric-pole"] = 1
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": False,
                "inventories": {"2": {"automation-science-pack": 10}},
            }
        )
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")

    def test_research_automation_continues_when_current_research_is_not_observable(self):
        obs = powered_research_observation()
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        skill = ResearchAutomationSkill()
        first = skill.next_action(obs)
        self.assertEqual(first.action["type"], "research")
        obs["inventory"]["automation-science-pack"] = 10
        second = skill.next_action(obs)
        self.assertEqual(second.action["type"], "insert")
        self.assertEqual(second.action["item"], "automation-science-pack")

    def test_research_automation_builds_lab_when_item_and_site_exist(self):
        obs = powered_research_observation()
        obs["research"]["current"] = "automation"
        obs["inventory"]["lab"] = 1
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "lab_position": {"x": 13.5, "y": 6.5},
                "distance": 3,
            }
        ]
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "lab")

    def test_research_automation_ignores_remote_lab_site_before_rail(self):
        obs = powered_research_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["research"]["current"] = "automation"
        obs["inventory"]["lab"] = 1
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 420.5, "y": -400.5},
                "lab_position": {"x": 423.5, "y": -400.5},
                "distance": 3,
            }
        ]
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("cannot find a powered or wireable lab site", decision.reason)

    def test_research_automation_does_not_build_lab_until_circuits_are_ready(self):
        obs = powered_research_observation()
        obs["research"]["current"] = "automation"
        obs["inventory"].update(
            {
                "electronic-circuit": 2,
                "iron-gear-wheel": 10,
                "transport-belt": 4,
            }
        )
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "lab_position": {"x": 13.5, "y": 6.5},
                "distance": 3,
            }
        ]
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertNotEqual(decision.action and decision.action.get("name"), "lab")

    def test_research_automation_extends_pole_before_lab_when_needed(self):
        obs = powered_research_observation()
        obs["research"]["current"] = "automation"
        obs["inventory"]["lab"] = 1
        obs["inventory"]["small-electric-pole"] = 1
        obs["lab_sites"] = [
            {
                "powered": True,
                "source_pole_unit_number": 604,
                "pole_position": {"x": 15.5, "y": 6.5},
                "lab_position": {"x": 18.5, "y": 6.5},
                "distance": 8,
            }
        ]
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")

    def test_research_automation_inserts_science_into_lab(self):
        obs = powered_research_observation()
        obs["research"]["current"] = "automation"
        obs["inventory"]["automation-science-pack"] = 10
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "automation-science-pack")

    def test_research_automation_distributes_science_to_empty_daisy_chain_lab(self):
        obs = powered_research_observation()
        obs["research"]["current"] = "automation"
        obs["inventory"]["automation-science-pack"] = 10
        obs["entities"].extend(
            [
                {
                    "name": "lab",
                    "unit_number": 701,
                    "position": {"x": 13.5, "y": 6.5},
                    "distance": 5,
                    "electric_network_connected": True,
                    "inventories": {"1": {"automation-science-pack": 4}},
                },
                {
                    "name": "lab",
                    "unit_number": 702,
                    "position": {"x": 17.5, "y": 6.5},
                    "distance": 8,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )
        decision = ResearchAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 702)

    def test_research_logistics_done_when_technology_researched(self):
        obs = powered_logistics_observation()
        obs["research"]["technologies"]["logistics"]["researched"] = True
        decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_research_logistics_sets_current_research(self):
        obs = powered_logistics_observation()
        decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertEqual(decision.action["type"], "research")
        self.assertEqual(decision.action["technology"], "logistics")

    def test_research_logistics_continues_when_current_research_is_not_observable(self):
        obs = powered_logistics_observation()
        skill = ResearchTechnologySkill("logistics")
        first = skill.next_action(obs)
        self.assertEqual(first.action["type"], "research")
        obs["inventory"]["automation-science-pack"] = 20
        second = skill.next_action(obs)
        self.assertEqual(second.action["type"], "insert")
        self.assertEqual(second.action["item"], "automation-science-pack")

    def test_research_logistics_inserts_red_science_into_lab(self):
        obs = powered_logistics_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["research"]["current"] = "logistics"
        obs["inventory"]["automation-science-pack"] = 20
        decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "automation-science-pack")

    def test_research_electric_mining_drill_uses_knowledge_fallback_when_observation_lacks_unit_metadata(self):
        obs = powered_logistics_observation()
        obs["research"]["current"] = "electric-mining-drill"
        obs["research"]["technologies"]["electric-mining-drill"] = {"researched": False}
        obs["inventory"]["automation-science-pack"] = 25

        decision = ResearchTechnologySkill("electric-mining-drill").next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "automation-science-pack")

    def test_research_logistics_produces_red_science_when_missing(self):
        obs = powered_logistics_observation()
        obs["research"]["current"] = "logistics"
        obs["inventory"] = {"iron-plate": 20, "iron-gear-wheel": 1, "copper-plate": 1}
        obs["craftable"] = {"automation-science-pack": 1}
        decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertFalse(
            decision.action["type"] == "craft"
            and decision.action.get("recipe") == "automation-science-pack"
        )

    def test_research_logistics_repairs_existing_lab_adjacent_remote_power_block(self):
        obs = powered_logistics_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["research"]["current"] = "logistics"
        obs["inventory"] = {"coal": 10}
        obs["power_sites"] = []
        for entity in obs["entities"]:
            if entity.get("name") in {"offshore-pump", "boiler", "steam-engine", "small-electric-pole", "lab"}:
                entity["position"] = {
                    "x": float(entity["position"]["x"]),
                    "y": float(entity["position"]["y"]) - 800.0,
                }
                entity["distance"] = 800
        boiler = next(entity for entity in obs["entities"] if entity.get("name") == "boiler")
        boiler["inventories"] = {}
        boiler["fluids"] = {"1": {"name": "water", "amount": 200}}
        engine = next(entity for entity in obs["entities"] if entity.get("name") == "steam-engine")
        engine["status"] = 5
        engine["status_name"] = "no_input_fluid"
        engine["fluids"] = {}
        obs["player"] = {"position": dict(boiler["position"])}

        setup_decision = SetupPowerSkill().next_action(obs)
        self.assertIn("cannot find a buildable water site", setup_decision.reason)

        research_decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertEqual(research_decision.action["type"], "insert")
        self.assertEqual(research_decision.action["name"], "boiler")
        self.assertEqual(research_decision.action["item"], "coal")

    def test_research_logistics_uses_lab_adjacent_remote_mall_site_after_power_is_ready(self):
        obs = powered_logistics_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["research"]["current"] = "logistics"
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["power_sites"] = []
        for entity in obs["entities"]:
            if entity.get("name") in {"offshore-pump", "boiler", "steam-engine", "small-electric-pole", "lab"}:
                entity["position"] = {
                    "x": float(entity["position"]["x"]),
                    "y": float(entity["position"]["y"]) - 800.0,
                }
                entity["distance"] = 800
        obs["automation_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": -793.5},
                "cable_assembler_position": {"x": 13.5, "y": -793.5},
                "circuit_assembler_position": {"x": 17.5, "y": -793.5},
                "transfer_inserter_position": {"x": 15.5, "y": -793.5},
                "transfer_inserter_direction": 4,
                "distance": 3,
            }
        ]
        obs["player"] = {"position": {"x": 13.5, "y": -793.5}}

        mall_decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)
        self.assertIn("cannot find a buildable water site", mall_decision.reason)

        research_decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertEqual(research_decision.action["type"], "build")
        self.assertEqual(research_decision.action["name"], "assembling-machine-1")
        self.assertEqual(research_decision.action["position"], {"x": 13.5, "y": -793.5})

    def test_research_logistics_recovers_lab_adjacent_unassigned_remote_mall_assembler(self):
        obs = powered_logistics_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["research"]["current"] = "logistics"
        obs["inventory"] = {}
        obs["power_sites"] = []
        obs["automation_sites"] = []
        for entity in obs["entities"]:
            if entity.get("name") in {"offshore-pump", "boiler", "steam-engine", "small-electric-pole", "lab"}:
                entity["position"] = {
                    "x": float(entity["position"]["x"]),
                    "y": float(entity["position"]["y"]) - 800.0,
                }
                entity["distance"] = 800
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 801,
                    "position": {"x": 18.5, "y": -799.5},
                    "distance": 4,
                    "recipe": "copper-cable",
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 802,
                    "position": {"x": 22.5, "y": -799.5},
                    "distance": 5,
                    "recipe": "electronic-circuit",
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 803,
                    "position": {"x": 18.5, "y": -803.5},
                    "distance": 4,
                    "recipe": None,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )
        obs["player"] = {"position": {"x": 18.5, "y": -803.5}}

        decision = ResearchTechnologySkill("logistics").next_action(obs)
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "automation-science-pack")
        self.assertEqual(decision.action["unit_number"], 803)

    def test_circuit_automation_delegates_until_automation_is_researched(self):
        obs = powered_research_observation()
        obs["entities"].append(
            {
                "name": "lab",
                "unit_number": 701,
                "position": {"x": 13.5, "y": 6.5},
                "distance": 5,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "research")
        self.assertEqual(decision.action["technology"], "automation")

    def test_circuit_automation_builds_missing_pole_before_cell(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "small-electric-pole": 1,
            "assembling-machine-1": 2,
            "inserter": 1,
        }
        obs["automation_sites"][0].pop("pole_unit_number")
        obs["automation_sites"][0]["source_pole_unit_number"] = 604
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")

    def test_circuit_automation_crafts_assembler_when_missing(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 6,
            "iron-gear-wheel": 10,
            "iron-plate": 18,
            "inserter": 1,
        }
        obs["craftable"] = {"assembling-machine-1": 2}
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "assembling-machine-1")

    def test_circuit_automation_uses_existing_mall_assembler_for_assembler_bootstrap(self):
        obs = powered_automation_observation()
        obs["player"]["position"] = {"x": -40.5, "y": 15.5}
        obs["inventory"] = {
            "electronic-circuit": 6,
            "iron-gear-wheel": 10,
            "iron-plate": 18,
            "inserter": 1,
        }
        obs["craftable"] = {"assembling-machine-1": 2}
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 918,
                "position": {"x": -40.5, "y": 15.5},
                "distance": 43,
                "recipe": "iron-gear-wheel",
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertNotEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "assembling-machine-1")
        self.assertEqual(decision.action["unit_number"], 918)

    def test_circuit_automation_uses_gear_mall_before_handcrafting_assembler_prerequisite(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 6,
            "iron-plate": 18,
            "inserter": 1,
        }
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 5}))

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertNotEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("refusing player collection of iron gear wheels", decision.reason)

    def test_circuit_automation_uses_inserter_mall_instead_of_handcrafting_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "electronic-circuit": 2}
        obs["craftable"] = {"iron-gear-wheel": 4, "inserter": 1}
        for entity in circuit_cell_entities():
            if entity["name"] != "inserter":
                obs["entities"].append(entity)
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 819,
                "position": {"x": 14, "y": 2},
                "distance": 14,
                "recipe": None,
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertNotEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "inserter")
        self.assertEqual(decision.action["unit_number"], 819)

    def test_circuit_automation_places_cable_assembler_first(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"assembling-machine-1": 2, "inserter": 1}
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 2.0})

    def test_circuit_automation_sets_assembler_recipe(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities(cable_recipe=None, circuit_recipe="electronic-circuit"))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "copper-cable")

    def test_circuit_automation_recognizes_unassigned_assembler_pair(self):
        obs = powered_automation_observation()
        obs["player"]["position"] = {"x": 6, "y": 2}
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities(cable_recipe=None, circuit_recipe=None))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "copper-cable")

    def test_circuit_automation_connects_unpowered_cell(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities(powered=False))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "connect_power")
        self.assertEqual(decision.action["unit_number"], 804)

    def test_circuit_automation_inserts_copper_into_cable_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities(circuit_inventory={"iron-plate": 4, "copper-cable": 6}))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "copper-plate")

    def test_circuit_automation_seeds_existing_copper_cable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-cable": 12, "copper-plate": 8, "iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities())
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "copper-cable")

    def test_circuit_automation_inserts_iron_into_circuit_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 8}
        entities = circuit_cell_entities(cable_inventory={"copper-plate": 4})
        obs["entities"].extend(entities)
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")

    def test_circuit_automation_prioritizes_circuit_iron_before_more_copper(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 8, "iron-plate": 5}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")

    def test_circuit_automation_takes_cable_output_for_circuit_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(
            circuit_cell_entities(
                cable_inventory={"copper-cable": 40},
                circuit_inventory={"iron-plate": 4},
            )
        )
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-cable")

    def test_circuit_automation_scaling_waits_for_transfer_inserter_instead_of_taking_cable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            circuit_cell_entities(
                cable_inventory={"copper-cable": 40},
                circuit_inventory={"iron-plate": 4},
            )
        )
        decision = CircuitAutomationSkill(target_count=50).next_action(obs)
        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("transfer inserter", decision.reason)
        self.assertNotEqual(decision.action.get("type"), "take")

    def test_circuit_automation_scaling_refuses_manual_cable_seed(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-cable": 12}
        obs["entities"].extend(circuit_cell_entities(circuit_inventory={"iron-plate": 4}))
        decision = CircuitAutomationSkill(target_count=50).next_action(obs)
        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("refusing hand-seeded cable", decision.reason)

    def test_circuit_automation_scaling_refuses_manual_iron_input_when_belts_ready(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 980,
                "position": {"x": 12, "y": 2},
                "distance": 12,
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = CircuitAutomationSkill(target_count=50).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("automated iron-plate input line", decision.reason)
        self.assertIn("refusing repeated player hand-carry", decision.reason)

    def test_circuit_automation_takes_output_from_circuit_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(circuit_cell_entities(circuit_inventory={"electronic-circuit": 5}))
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_circuit_automation_done_when_cell_is_running_and_target_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"electronic-circuit": 5}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-plate": 4}, circuit_inventory={"iron-plate": 4}))
        decision = CircuitAutomationSkill(target_count=5).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_circuit_automation_ignores_remote_existing_cell_before_rail(self):
        obs = powered_automation_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {"assembling-machine-1": 2, "inserter": 1}
        for entity in circuit_cell_entities():
            remote = dict(entity)
            remote["position"] = {
                "x": entity["position"]["x"] + 420,
                "y": entity["position"]["y"] - 400,
            }
            remote["distance"] = 580
            obs["entities"].append(remote)
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 2.0})

    def test_circuit_automation_ignores_remote_planned_site_before_rail(self):
        obs = powered_automation_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {"assembling-machine-1": 2, "inserter": 1}
        obs["automation_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 424, "y": -400},
                "cable_assembler_position": {"x": 422, "y": -398},
                "circuit_assembler_position": {"x": 426, "y": -398},
                "transfer_inserter_position": {"x": 424, "y": -398},
                "transfer_inserter_direction": 4,
                "distance": 4,
            }
        ]
        decision = CircuitAutomationSkill().next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("cannot find a powered or wireable site", decision.reason)

    def test_build_item_mall_builds_missing_pole_before_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "small-electric-pole": 1,
            "assembling-machine-1": 1,
        }
        obs["automation_sites"][0].pop("pole_unit_number")
        obs["automation_sites"][0]["source_pole_unit_number"] = 604
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")

    def test_build_item_mall_crafts_assembler_when_missing(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-gear-wheel": 5,
            "iron-plate": 9,
        }
        obs["craftable"] = {"assembling-machine-1": 1}
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "assembling-machine-1")

    def test_build_item_mall_places_assembler_when_site_ready(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"assembling-machine-1": 1}
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 2.0})

    def test_build_item_mall_sets_recipe_on_existing_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10, "iron-gear-wheel": 10}
        obs["entities"].append(mall_assembler(recipe=None))
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "transport-belt")

    def test_build_item_mall_clears_retooled_small_pole_assembler_before_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"small-electric-pole": 12, "iron-plate": 10, "iron-gear-wheel": 10}
        obs["entities"].append(mall_assembler(recipe="small-electric-pole", inventory={"copper-cable": 4}))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-cable")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertIn("before setting transport-belt", decision.reason)

    def test_build_item_mall_retools_stocked_small_pole_assembler_for_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"small-electric-pole": 12, "iron-plate": 10, "iron-gear-wheel": 10}
        obs["entities"].append(mall_assembler(recipe="small-electric-pole", inventory={}))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 901)

    def test_build_item_mall_preserves_belt_assembler_when_bootstrapping_gears(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={}))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 902,
                "position": {"x": 8.0, "y": 2.0},
                "distance": 8,
                "recipe": "automation-science-pack",
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 902)

    def test_build_item_mall_can_retool_gear_assembler_before_power_window(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10}
        obs["craftable"] = {"iron-gear-wheel": 5}
        for entity in obs["entities"]:
            if entity.get("name") == "boiler":
                entity["status_name"] = "no_fuel"
                entity["inventories"] = {}
                entity["fluids"] = {"1": {"name": "water", "amount": 200}}
            if entity.get("name") == "steam-engine":
                entity["status"] = 5
                entity["fluids"] = {}
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={}))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 902,
                "position": {"x": 8.0, "y": 2.0},
                "distance": 8,
                "recipe": "automation-science-pack",
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 902)

    def test_build_item_mall_recovers_unassigned_assembler_outside_planned_site(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10, "iron-gear-wheel": 10}
        obs["automation_sites"] = []
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 902,
                "position": {"x": 88.5, "y": -3.5},
                "distance": 90,
                "recipe": None,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 88.5, "y": -3.5})

    def test_build_item_mall_ignores_remote_unassigned_assembler_before_rail(self):
        obs = powered_automation_observation()
        obs["base"] = {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}}
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 902,
                "position": {"x": 420.5, "y": -400.5},
                "distance": 580,
                "recipe": None,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 2.0})

    def test_build_item_mall_inserts_recipe_ingredients(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10, "iron-gear-wheel": 10}
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertIn(decision.action["item"], {"iron-gear-wheel", "iron-plate"})

    def test_power_pole_mall_takes_existing_copper_output_before_refueling_cell(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 6, "y": 0}}
        obs["inventory"] = {}
        obs["craftable"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="small-electric-pole"),
                {
                    "name": "burner-mining-drill",
                    "unit_number": 930,
                    "position": {"x": 4, "y": 0},
                    "direction": 4,
                    "distance": 2,
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 931,
                    "position": {"x": 6, "y": 0},
                    "distance": 0,
                    "recipe": "copper-plate",
                    "status_name": "no_fuel",
                    "inventories": {"3": {"copper-plate": 11}},
                },
            ]
        )

        decision = BuildItemMallSkill("small-electric-pole", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-plate")
        self.assertEqual(decision.action["unit_number"], 931)

    def test_power_pole_mall_mines_tree_when_recipe_needs_wood(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4, "y": 4}}
        obs["inventory"] = {}
        obs["craftable"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="small-electric-pole", inventory={"copper-cable": 8}),
                {
                    "name": "tree-11",
                    "type": "tree",
                    "position": {"x": 5, "y": 4},
                    "distance": 1,
                },
            ]
        )

        decision = BuildItemMallSkill("small-electric-pole", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "tree-11")
        self.assertIn("build item mall wood", decision.reason)

    def test_build_item_mall_builds_gear_assembler_instead_of_handcrafting_science_input(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": -2.0, "y": 2.0}}
        obs["resources"] = []
        obs["inventory"] = {"assembling-machine-1": 1, "iron-plate": 10}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"copper-plate": 4}))

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertNotEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 5.0, "y": -1.0})

    def test_build_item_mall_uses_existing_gear_assembler_for_science_input(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"copper-plate": 4}))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 902,
                "position": {"x": -2.0, "y": 2.0},
                "distance": 2,
                "recipe": "iron-gear-wheel",
                "electric_network_connected": True,
                "inventories": {"1": {"iron-gear-wheel": 4}},
            }
        )

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("refusing player collection of iron gear wheels", decision.reason)

    def test_build_item_mall_refuses_player_gear_output_collection_after_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 4}))

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("refusing player collection of iron gear wheels", decision.reason)

    def test_build_item_mall_repurposes_existing_assembler_for_gears_before_handcrafting(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10, "electronic-circuit": 7}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"copper-plate": 4}))

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 901)

    def test_build_item_mall_blocks_repeated_hand_carry_from_distant_source_site(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-plate": 20, "iron-gear-wheel": 20}
        obs["entities"].extend(complete_belt_smelting_entities(0, 0, 960, resource="copper-ore", product="copper-plate"))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 970,
                "recipe": "automation-science-pack",
                "position": {"x": 180, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["player"] = {"position": {"x": 180, "y": 0}}

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("logistic line", decision.reason)
        self.assertIn("refusing repeated hand-carry", decision.reason)

    def test_build_item_mall_blocks_remote_iron_plate_prerequisite_hand_carry(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel"))
        obs["entities"].extend(
            [
                {
                    "name": "transport-belt",
                    "unit_number": 930,
                    "position": {"x": 3, "y": 5},
                    "direction": 4,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 931,
                    "position": {"x": 4, "y": 5},
                    "direction": 4,
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 980,
                    "position": {"x": 120, "y": 0},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
            ]
        )

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("iron-plate logistic line", decision.reason)
        self.assertIn("refusing repeated hand-carry", decision.reason)

    def test_build_item_mall_takes_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"transport-belt": 20}))
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "transport-belt")

    def test_build_item_mall_done_when_running_and_target_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}))
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_gear_belt_mall_sets_reusable_assembler_to_transport_belt(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 3, "burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(gear_belt_mall_entities())

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 911)

    def test_gear_belt_mall_builds_short_belt_lane_without_taking_gears(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 3, "burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotEqual(decision.action.get("type"), "take")

    def test_gear_belt_mall_uses_bottom_lane_when_top_lane_hits_machine_footprint(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 3, "burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 912,
                "position": {"x": 4, "y": -2},
                "distance": 4,
                "recipe": "copper-cable",
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 3, "y": 5})

    def test_gear_belt_mall_places_burner_output_inserter_after_lane(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-inserter")
        self.assertEqual(decision.action["position"], {"x": 3, "y": 0})
        self.assertEqual(decision.action["direction"], 8)

    def test_gear_belt_mall_removes_misoriented_output_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].append(
            {
                "name": "burner-inserter",
                "unit_number": 940,
                "position": {"x": 3, "y": 0},
                "direction": 0,
                "inventories": {"1": {"coal": 1}},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 940)
        self.assertIn("misoriented", decision.reason)

    def test_gear_belt_mall_seeds_iron_without_gear_handcraft_after_logistics(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["craftable"] = {"iron-gear-wheel": 4}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertNotEqual(decision.action.get("recipe"), "iron-gear-wheel")

    def test_gear_belt_mall_does_not_require_coal_for_waiting_burner_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["craftable"] = {"iron-gear-wheel": 4}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "status_name": "waiting_for_source_items",
                    "inventories": {},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertNotIn("coal starter fuel", decision.reason)

    def test_gear_belt_mall_relocates_existing_inserter_instead_of_handcrafting_gear(self):
        obs = powered_automation_observation()
        obs["player"]["position"] = {"x": 8, "y": 2}
        obs["inventory"] = {"iron-plate": 8}
        obs["craftable"] = {"iron-gear-wheel": 4, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 942,
                    "position": {"x": 8, "y": 0},
                    "direction": 4,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 942)
        self.assertNotEqual(decision.action.get("recipe"), "iron-gear-wheel")

    def test_gear_belt_mall_powers_relocated_input_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"small-electric-pole": 1, "iron-plate": 8}
        obs["craftable"] = {"iron-gear-wheel": 4}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "small-electric-pole",
                    "unit_number": 939,
                    "position": {"x": 4, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": False,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 0.0})
        self.assertNotEqual(decision.action.get("recipe"), "iron-gear-wheel")

    def test_gear_belt_mall_recovers_local_plate_seed_without_remote_hand_carry(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["craftable"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"iron-gear-wheel": 3},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 942,
                    "position": {"x": 0, "y": 5},
                    "recipe": "electronic-circuit",
                    "inventories": {"2": {"iron-plate": 4}},
                    "electric_network_connected": True,
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 943,
                    "position": {"x": 96, "y": 0},
                    "inventories": {"3": {"iron-plate": 20}},
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 942)
        self.assertIn("local iron plates", decision.reason)

    def test_gear_belt_mall_seeds_belt_assembler_first_when_it_already_has_gears(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4}
        obs["craftable"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"iron-gear-wheel": 3},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 911)

    def test_gear_belt_mall_done_when_belt_assembler_has_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"transport-belt": 4},
            )
        )
        for index, x in enumerate((3, 4), start=930):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": {"x": x, "y": -1},
                    "direction": 4,
                    "inventories": {},
                }
            )
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 4, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_iron_plate_logistic_line_takes_belts_from_belt_mall_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"transport-belt": 8},
            )
        )
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {"3": {"iron-plate": 20}},
            }
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 911)
        self.assertNotEqual(decision.action.get("item"), "iron-plate")
        self.assertNotEqual(decision.action.get("recipe"), "iron-gear-wheel")

    def test_iron_plate_logistic_line_places_belt_without_plate_or_gear_handcarry(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {"3": {"iron-plate": 20}},
            }
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotIn(decision.action.get("item"), {"iron-plate", "iron-gear-wheel"})
        self.assertNotEqual(decision.action.get("recipe"), "iron-gear-wheel")

    def test_iron_plate_logistic_line_clears_tree_without_rerouting(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {"3": {"iron-plate": 20}},
            }
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        next_position = layout["segments"][0]["position"]
        obs["entities"].append(
            {
                "name": "tree-02",
                "type": "tree",
                "position": {"x": next_position["x"], "y": next_position["y"] + 1.0},
                "inventories": {},
            }
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "tree-02")
        self.assertIn("clear blocking tree-02", decision.reason)

    def test_iron_plate_logistic_line_does_not_mine_source_furnace_as_blocker(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 8, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 951,
                    "position": {"x": 10.5, "y": 2.5},
                    "direction": 4,
                    "inventories": {},
                },
                {
                    "name": "burner-mining-drill",
                    "unit_number": 952,
                    "position": {"x": 7, "y": -1},
                    "direction": 4,
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        self.assertFalse(
            any(
                abs(segment["position"]["x"] - 7.0) < 0.1 and abs(segment["position"]["y"] + 1.0) < 0.1
                for segment in layout["segments"]
            )
        )
        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertNotEqual(decision.action.get("type"), "mine")
        self.assertNotEqual(decision.action.get("unit_number"), 950)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertGreater(decision.action["position"]["x"], 10)

    def test_iron_plate_logistic_line_reports_missing_belts_without_gear_handcraft(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["craftable"] = {"iron-gear-wheel": 4, "transport-belt": 2}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {"3": {"iron-plate": 20}},
            }
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("refusing gear handcraft", decision.reason)

    def test_site_input_logistic_line_takes_belts_from_belt_mall_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 8},
                    "recipe": "copper-plate",
                    "inventories": {"3": {"copper-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 8, "y": 8},
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertNotEqual(decision.action.get("item"), "copper-plate")

    def test_site_input_logistic_line_places_belt_without_item_handcarry(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 8},
                    "recipe": "copper-plate",
                    "inventories": {"3": {"copper-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 8, "y": 8},
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotEqual(decision.action.get("item"), "copper-plate")

    def test_site_input_logistic_line_refuses_without_belt_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 8},
                    "recipe": "copper-plate",
                    "inventories": {"3": {"copper-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 8, "y": 8},
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("automated transport-belt production", decision.reason)

    def test_expansion_prefers_high_coverage_patch_drill_position(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 2, "y": 0}, "distance": 2},
            {"name": "iron-ore", "position": {"x": 30, "y": 0}, "distance": 30},
            {"name": "iron-ore", "position": {"x": 31, "y": 0}, "distance": 31},
            {"name": "iron-ore", "position": {"x": 30, "y": 1}, "distance": 30.1},
            {"name": "iron-ore", "position": {"x": 31, "y": 1}, "distance": 31.1},
            {"name": "coal", "position": {"x": 0, "y": 2}, "distance": 2},
        ]
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertGreater(decision.action["position"]["x"], 20)

    def test_expansion_aligns_new_drill_with_existing_patch_line(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "iron-ore", "position": {"x": 4, "y": 3}, "distance": 5},
            {"name": "coal", "position": {"x": 0, "y": 2}, "distance": 2},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        decision = ExpandIronSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 3.0})


def powered_research_observation():
    obs = base_observation()
    obs["inventory"] = {"coal": 10}
    obs["entities"] = [
        {
            "name": "offshore-pump",
            "unit_number": 601,
            "position": {"x": 10.5, "y": 10.5},
            "direction": 12,
            "distance": 10,
            "inventories": {},
            "fluids": {"1": {"name": "water", "amount": 100}},
        },
        {
            "name": "boiler",
            "unit_number": 602,
            "position": {"x": 12.5, "y": 10},
            "direction": 0,
            "distance": 12,
            "inventories": {"1": {"coal": 5}},
            "fluids": {
                "1": {"name": "water", "amount": 200},
                "2": {"name": "steam", "amount": 20},
            },
        },
        {
            "name": "steam-engine",
            "unit_number": 603,
            "position": {"x": 12.5, "y": 6.5},
            "direction": 0,
            "status": 1,
            "distance": 13,
            "inventories": {},
            "fluids": {"1": {"name": "steam", "amount": 80}},
        },
        {
            "name": "small-electric-pole",
            "unit_number": 604,
            "position": {"x": 10.5, "y": 6.5},
            "direction": 0,
            "distance": 12,
            "electric_network_connected": True,
            "inventories": {},
            "fluids": {},
        },
    ]
    return obs


def powered_automation_observation():
    obs = powered_research_observation()
    obs["research"]["technologies"]["automation"]["researched"] = True
    obs["automation_sites"] = [
        {
            "powered": True,
            "pole_unit_number": 604,
            "pole_position": {"x": 4, "y": 0},
            "cable_assembler_position": {"x": 2, "y": 2},
            "circuit_assembler_position": {"x": 6, "y": 2},
            "transfer_inserter_position": {"x": 4, "y": 2},
            "transfer_inserter_direction": 4,
            "distance": 4,
        }
    ]
    return obs


def powered_logistics_observation():
    obs = powered_automation_observation()
    obs["entities"].append(
        {
            "name": "lab",
            "unit_number": 701,
            "position": {"x": 13.5, "y": 6.5},
            "distance": 5,
            "electric_network_connected": True,
            "inventories": {},
        }
    )
    obs["research"]["current"] = None
    obs["research"]["technologies"]["logistics"] = {
        "researched": False,
        "enabled": True,
        "research_unit_count": 20,
        "ingredients": {"automation-science-pack": 1},
    }
    return obs


def circuit_cell_entities(
    cable_recipe="copper-cable",
    circuit_recipe="electronic-circuit",
    cable_inventory=None,
    circuit_inventory=None,
    powered=True,
):
    return [
        {
            "name": "assembling-machine-1",
            "unit_number": 801,
            "position": {"x": 2, "y": 2},
            "distance": 2,
            "recipe": cable_recipe,
            "electric_network_connected": powered,
            "inventories": {"1": cable_inventory or {}},
        },
        {
            "name": "assembling-machine-1",
            "unit_number": 802,
            "position": {"x": 6, "y": 2},
            "distance": 6,
            "recipe": circuit_recipe,
            "electric_network_connected": powered,
            "inventories": {"1": circuit_inventory or {}},
        },
        {
            "name": "inserter",
            "unit_number": 803,
            "position": {"x": 4, "y": 2},
            "distance": 4,
            "direction": 4,
            "electric_network_connected": powered,
            "inventories": {},
        },
        {
            "name": "small-electric-pole",
            "unit_number": 804,
            "position": {"x": 4, "y": 0},
            "distance": 4,
            "electric_network_connected": powered,
            "inventories": {},
        },
    ]


def mall_assembler(recipe="transport-belt", inventory=None, powered=True):
    return {
        "name": "assembling-machine-1",
        "unit_number": 901,
        "position": {"x": 2, "y": 2},
        "distance": 2,
        "recipe": recipe,
        "electric_network_connected": powered,
        "inventories": {"1": inventory or {}},
    }


def gear_belt_mall_entities(belt_recipe="automation-science-pack", gear_inventory=None, belt_inventory=None):
    return [
        {
            "name": "assembling-machine-1",
            "unit_number": 910,
            "position": {"x": 2, "y": 2},
            "distance": 2,
            "recipe": "iron-gear-wheel",
            "electric_network_connected": True,
            "inventories": {"1": gear_inventory or {}},
        },
        {
            "name": "assembling-machine-1",
            "unit_number": 911,
            "position": {"x": 5, "y": 2},
            "distance": 5,
            "recipe": belt_recipe,
            "electric_network_connected": True,
            "inventories": {"1": belt_inventory or {}},
        },
    ]


def long_gear_mall_relocation_observation():
    obs = base_observation()
    obs["research"] = {"technologies": {"automation": {"researched": True}}}
    obs["inventory"] = {}
    obs["craftable"] = {}
    obs["entities"] = [
        {
            "name": "small-electric-pole",
            "unit_number": 90,
            "position": {"x": 4.0, "y": 0.5},
            "electric_network_connected": True,
            "inventories": {},
        },
        {
            "name": "assembling-machine-1",
            "unit_number": 100,
            "recipe": "iron-gear-wheel",
            "position": {"x": 0.5, "y": 0.5},
            "electric_network_connected": True,
            "inventories": {},
        },
        {
            "name": "assembling-machine-1",
            "unit_number": 101,
            "recipe": "transport-belt",
            "position": {"x": 3.5, "y": 0.5},
            "electric_network_connected": True,
            "inventories": {"2": {"iron-gear-wheel": 3}},
        },
        {
            "name": "stone-furnace",
            "unit_number": 200,
            "recipe": "iron-plate",
            "position": {"x": 153.0, "y": 0.5},
            "inventories": {"2": {"iron-plate": 24}},
        },
    ]
    return obs


def _add_existing_relocation_power_corridor(obs):
    layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
    positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)
    start_unit = 7000
    for index, position in enumerate(positions):
        obs["entities"].append(
            {
                "name": "small-electric-pole",
                "unit_number": start_unit + index,
                "position": position,
                "electric_network_connected": True,
                "inventories": {},
            }
        )


def complete_belt_smelting_entities(drill_x, drill_y, base_unit, resource="iron-ore", product="iron-plate", reserve_fuel=False):
    drill_coal = 8 if reserve_fuel else 3
    inserter_coal = 4 if reserve_fuel else 2
    furnace_coal = 8 if reserve_fuel else 3
    return [
        {
            "name": "burner-mining-drill",
            "unit_number": base_unit,
            "position": {"x": drill_x, "y": drill_y},
            "distance": 4,
            "mining_target": resource,
            "inventories": {"1": {"coal": drill_coal}},
        },
        {
            "name": "transport-belt",
            "unit_number": base_unit + 1,
            "position": {"x": drill_x + 2, "y": drill_y},
            "distance": 6,
            "inventories": {},
        },
        {
            "name": "transport-belt",
            "unit_number": base_unit + 2,
            "position": {"x": drill_x + 3, "y": drill_y},
            "distance": 7,
            "inventories": {},
        },
        {
            "name": "burner-inserter",
            "unit_number": base_unit + 3,
            "position": {"x": drill_x + 4, "y": drill_y},
            "distance": 8,
            "inventories": {"1": {"coal": inserter_coal}},
        },
        {
            "name": "stone-furnace",
            "unit_number": base_unit + 4,
            "position": {"x": drill_x + 5, "y": drill_y},
            "distance": 9,
            "inventories": {"2": {resource: 1}, "1": {"coal": furnace_coal}, "3": {product: 1}},
        },
    ]


if __name__ == "__main__":
    unittest.main()
