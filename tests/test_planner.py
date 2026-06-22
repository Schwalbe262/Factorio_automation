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
    def test_coal_supply_places_drill_before_output_chest_before_belt_automation(self):
        obs = base_observation()
        obs["inventory"] = {"burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 6, "y": 0})
        self.assertEqual(decision.action["required_resource"], "coal")

    def test_coal_supply_places_drill_before_output_belt_after_belt_automation(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 9,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {"3": {"transport-belt": 2}},
            },
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 6, "y": 0})
        self.assertEqual(decision.action["required_resource"], "coal")

    def test_coal_supply_uses_existing_output_chest_then_places_drill(self):
        obs = base_observation()
        obs["inventory"] = {"burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 6, "y": 0})
        self.assertEqual(decision.action["required_resource"], "coal")

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

    def test_coal_supply_repairs_misoriented_output_belt_before_fueling_drill(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 7.5, "y": 0.5}
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 0, "inventories": {}},
            {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 10)
        self.assertIn("misoriented coal supply output belt", decision.reason)

    def test_coal_supply_integer_center_drill_uses_lower_east_output_tile(self):
        drill = {"name": "burner-mining-drill", "position": {"x": 106, "y": 24}, "direction": planner_module.EAST}

        output_position = planner_module._burner_drill_output_position(drill)

        self.assertEqual(output_position, {"x": 107.5, "y": 24.5})

    def test_coal_supply_prefers_live_drill_drop_position_for_output_tile(self):
        drill = {
            "name": "burner-mining-drill",
            "position": {"x": 106, "y": 24},
            "direction": planner_module.EAST,
            "drop_position": {"x": 107.3, "y": 23.5},
        }

        output_position = planner_module._burner_drill_output_position(drill)

        self.assertEqual(output_position, {"x": 107.5, "y": 23.5})

    def test_coal_supply_finds_existing_belt_at_live_drop_output_tile(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 108, "y": 24}
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 106.5, "y": 23.5}, "distance": 3}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 405,
                "position": {"x": 106, "y": 24},
                "direction": planner_module.EAST,
                "drop_position": {"x": 107.3, "y": 23.5},
                "mining_target": "coal",
                "status_name": "no_fuel",
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 646,
                "position": {"x": 107.5, "y": 23.5},
                "direction": planner_module.EAST,
                "inventories": {},
            },
        ]

        layout = planner_module._find_coal_supply_layout(obs)
        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(layout["output_belt"]["unit_number"], 646)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 405)
        self.assertNotEqual(decision.action["type"], "build")

    def test_boiler_feed_source_uses_corrected_integer_center_drill_output(self):
        obs = base_observation()
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 6}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 5.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 6.5, "y": 0.5}, "direction": planner_module.NORTH, "inventories": {}},
        ]

        sources = planner_module._coal_supply_output_belt_sources(obs)

        self.assertEqual(sources[0]["belt"]["unit_number"], 21)

    def test_coal_supply_recovers_misplaced_upper_row_output_belt(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 7.5, "y": -0.5}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": -0.5}, "direction": planner_module.EAST, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": planner_module.EAST,
                "inventories": {"1": {"coal": 3}},
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 10)
        self.assertIn("misplaced coal supply output belt", decision.reason)

    def test_coal_supply_takes_belt_from_belt_mall_output_after_drill_exists(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 89,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 1}},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 90,
                "position": {"x": 1, "y": 1},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {"3": {"transport-belt": 2}},
            }
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 90)
        self.assertIn("belt mall output", decision.reason)

    def test_coal_supply_relocates_idle_non_coal_drill_when_no_drill_inventory(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 0, "y": 6}
        obs["inventory"] = {"transport-belt": 1}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6},
            {"name": "stone", "position": {"x": 0, "y": 6}, "distance": 6},
        ]
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 90,
                "position": {"x": 1, "y": 1},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {"3": {"transport-belt": 2}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 91,
                "position": {"x": 0, "y": 6},
                "direction": 4,
                "mining_target": "stone",
                "status_name": "no_fuel",
                "inventories": {},
            }
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 91)
        self.assertIn("relocate idle burner mining drill", decision.reason)

    def test_coal_supply_fuels_existing_drill(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 11)

    def test_coal_supply_moves_within_reach_before_output_chest_build(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 0, "y": 8}
        obs["inventory"] = {"wooden-chest": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 20}},
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "move_to")
        self.assertIn("planned coal output chest", decision.reason)

    def test_coal_supply_stocks_drill_with_longer_fuel_reserve(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 16}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 16)

    def test_coal_supply_accepts_fueled_drill_without_more_matching_fuel(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"wood": 4}},
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertTrue(decision.done)
        self.assertIn("coal supply site is active", decision.reason)

    def test_coal_supply_counts_currently_burning_drill_as_fueled(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {},
                "burner": {"currently_burning": "coal", "remaining_burning_fuel": 1900000},
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 12)
        self.assertEqual(decision.action["unit_number"], 11)

    def test_coal_supply_done_when_fueled_and_belted(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 20}},
            },
        ]
        decision = CoalSupplySkill().next_action(obs)
        self.assertTrue(decision.done)
        self.assertIn("coal supply site is active", decision.reason)

    def test_coal_supply_expands_parallel_burner_drills_before_done(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 15, "y": 0}
        obs["inventory"] = {"burner-mining-drill": 1, "coal": 30}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6},
            {"name": "coal", "position": {"x": 12, "y": 0}, "distance": 12},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 20}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 4}}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 12.0, "y": 0.0})
        self.assertIn("parallel", decision.reason)

    def test_coal_supply_does_not_expand_burner_drills_after_electric_drill_research(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 30}
        obs["research"]["technologies"]["electric-mining-drill"] = {"researched": True}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6},
            {"name": "coal", "position": {"x": 12, "y": 0}, "distance": 12},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 20}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 4}}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_coal_supply_does_not_steal_iron_or_copper_drills_for_parallel_coal(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 0, "y": 6}
        obs["inventory"] = {"coal": 30}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6},
            {"name": "coal", "position": {"x": 12, "y": 0}, "distance": 12},
            {"name": "iron-ore", "position": {"x": 0, "y": 6}, "distance": 6},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 20}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 4}}},
            {
                "name": "burner-mining-drill",
                "unit_number": 91,
                "position": {"x": 0, "y": 6},
                "direction": 4,
                "mining_target": "iron-ore",
                "inventories": {},
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertFalse(
            decision.action
            and decision.action.get("type") == "mine"
            and decision.action.get("unit_number") == 91,
            decision.reason,
        )

    def test_coal_supply_recovers_drill_with_no_minable_resources(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 6, "y": 0}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}]
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "status_name": "no_minable_resources",
                "inventories": {"1": {"coal": 3}},
                "mining_target": "coal",
            },
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 11)
        self.assertIn("no minable resources", decision.reason)

    def test_coal_supply_takes_coal_from_output_chest(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 12}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 8}}},
        ]
        decision = CoalSupplySkill(target_count=16).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_coal_supply_done_when_fueled_and_chested(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 16}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 20}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 4}}},
        ]
        decision = CoalSupplySkill(target_count=16).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIn("output chest", decision.reason)

    def test_coal_supply_takes_belt_from_mall_chest_to_replace_output_chest(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 5, "y": 2}
        obs["inventory"] = {}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 12}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "inventories": {"1": {"coal": 8}}},
            mall_assembler(recipe="transport-belt", inventory={}),
            {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {"1": {"transport-belt": 8}}},
            {"name": "inserter", "unit_number": 981, "position": {"x": 4.0, "y": 2.0}, "direction": 12, "electric_network_connected": True, "inventories": {}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertEqual(decision.action["item"], "transport-belt")
        self.assertIn("output chest", decision.reason)

    def test_coal_supply_replaces_output_chest_with_belt_when_belts_available(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 7.5, "y": 0.5}
        obs["inventory"] = {"transport-belt": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 12}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "inventories": {"1": {"coal": 8}}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 10)
        self.assertIn("before placing coal output belt", decision.reason)

    def test_coal_supply_fuels_drill_before_replacing_empty_output_chest(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 6, "y": 0}
        obs["inventory"] = {"transport-belt": 1, "coal": 12}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "coal",
                "status_name": "no_fuel",
                "inventories": {},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 7.5, "y": 0.5}, "inventories": {}},
        ]

        decision = CoalSupplySkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 11)
        self.assertIn("before output chest replacement", decision.reason)

    def test_fuel_burner_takes_coal_from_logistic_output_before_hand_mining(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        target = {"name": "burner-mining-drill", "unit_number": 11, "position": {"x": 0, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 2, "y": 0}, "inventories": {"1": {"coal": 4}}},
        ]
        decision = planner_module._fuel_burner_line_entity(
            obs,
            {"x": 0, "y": 0},
            target,
            entity_name="burner-mining-drill",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_fuel_burner_takes_surplus_from_coal_supply_drill_before_hand_mining(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 18, "y": 0}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 20, "y": 0}, "distance": 2}]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 18, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 8}},
            },
        ]
        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_fuel_burner_waits_for_established_coal_output_before_hand_mining(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 20, "y": 0}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 20},
            {"name": "coal", "position": {"x": 20, "y": 0}, "distance": 0},
        ]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "transport-belt",
                "unit_number": 12,
                "position": {"x": 1.5, "y": 0.5},
                "direction": planner_module.EAST,
                "inventories": {},
            },
        ]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("established coal supply output", decision.reason)
        self.assertIn("refusing repeated hand-mining", decision.reason)

    def test_fuel_burner_takes_coal_from_established_output_belt_before_waiting(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 20, "y": 0}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 20},
            {"name": "coal", "position": {"x": 20, "y": 0}, "distance": 0},
        ]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "transport-belt",
                "unit_number": 12,
                "position": {"x": 1.5, "y": 0.5},
                "direction": planner_module.EAST,
                "inventories": {"1": {"coal": 6}},
            },
        ]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 12)
        self.assertEqual(decision.action["item"], "coal")
        self.assertIn("supply belt", decision.reason)

    def test_fuel_burner_allows_virtual_bootstrap_seed_when_coal_output_empty(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 20, "y": 0}, "character_valid": False}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 20},
            {"name": "coal", "position": {"x": 20, "y": 0}, "distance": 0},
        ]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "transport-belt",
                "unit_number": 12,
                "position": {"x": 1.5, "y": 0.5},
                "direction": planner_module.EAST,
                "inventories": {},
            },
        ]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="iron source furnace for gear mall plate logistics",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
            allow_bootstrap_seed=True,
        )

        self.assertEqual(decision.action["type"], "mine")
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "gear_mall_source_fuel_seed")
        self.assertIn("one-time bootstrap fuel seed", decision.reason)

    def test_fuel_burner_takes_small_surplus_before_mining_with_established_coal_output(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 20, "y": 0}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 20},
            {"name": "coal", "position": {"x": 20, "y": 0}, "distance": 0},
        ]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "transport-belt",
                "unit_number": 12,
                "position": {"x": 1.5, "y": 0.5},
                "direction": planner_module.EAST,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 13,
                "position": {"x": 18, "y": 0},
                "inventories": {"1": {"coal": 4}},
            },
        ]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 13)

    def test_fuel_burner_with_existing_coal_takes_matching_surplus_before_waiting(self):
        obs = base_observation()
        obs["inventory"] = {"wood": 5}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        target = {
            "name": "burner-mining-drill",
            "unit_number": 11,
            "position": {"x": 0, "y": 0},
            "inventories": {"1": {"coal": 1}},
        }
        obs["entities"] = [
            target,
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 6, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 8}},
            },
        ]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            obs["player"]["position"],
            target,
            entity_name="burner-mining-drill",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
            wait_for_existing_fuel=True,
        )

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_direct_smelting_takes_surplus_from_coal_supply_drill_before_hand_mining(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 2, "y": 0}
        obs["inventory"] = {"burner-mining-drill": 1, "stone-furnace": 1}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 10, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 2},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 10,
                "position": {"x": 2, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "inventories": {"1": {"coal": 8}},
            },
        ]

        decision = IronPlateSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["unit_number"], 10)

    def test_fuel_burner_bootstraps_coal_supply_before_hand_mining(self):
        obs = base_observation()
        obs["inventory"] = {"wooden-chest": 1, "burner-mining-drill": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 6, "y": 0}, "distance": 6}]
        target = {"name": "stone-furnace", "unit_number": 11, "position": {"x": 20, "y": 0}, "inventories": {}}
        obs["entities"] = [target]

        decision = planner_module._fuel_burner_line_entity(
            obs,
            {"x": 0, "y": 0},
            target,
            entity_name="stone-furnace",
            threshold=3,
            insert_count=5,
            context="test burner",
            support_skill=IronPlateSkill(),
            far_fuel_reason="too far",
        )

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertIn("coal supply patch", decision.reason)

    def test_stone_supply_places_output_chest_before_drill(self):
        obs = base_observation()
        obs["inventory"] = {"wooden-chest": 1, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        decision = StoneSupplySkill(target_count=8).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "wooden-chest")
        self.assertEqual(decision.action["position"], {"x": 8.0, "y": 0.0})

    def test_stone_supply_clears_output_chest_rock_before_building(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 8.0, "y": 0.0}
        obs["inventory"] = {"wooden-chest": 1, "burner-mining-drill": 1, "coal": 8}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "big-rock",
                "type": "simple-entity",
                "unit_number": 90,
                "position": {"x": 8.0, "y": 0.0},
            },
        ]

        decision = StoneSupplySkill(target_count=8).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 90)
        self.assertIn("starter stone output chest", decision.reason)

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

    def test_stone_supply_waits_when_drill_has_one_fuel(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "stone",
                "status_name": "working",
                "inventories": {"1": {"coal": 1}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {}},
        ]

        decision = StoneSupplySkill(target_count=16).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("wait for starter stone drill", decision.reason)

    def test_stone_supply_recovers_drill_with_no_minable_resources(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 6, "y": 0}
        obs["inventory"] = {"coal": 4}
        obs["resources"] = [{"name": "stone", "position": {"x": 6, "y": 0}, "distance": 6}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 11,
                "position": {"x": 6, "y": 0},
                "direction": 4,
                "mining_target": "stone",
                "status_name": "no_minable_resources",
                "inventories": {"1": {"coal": 1}},
            },
            {"name": "wooden-chest", "unit_number": 10, "position": {"x": 8, "y": 0}, "inventories": {}},
        ]

        decision = StoneSupplySkill(target_count=16).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 11)
        self.assertIn("no minable resources", decision.reason)

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

    def test_coal_fuel_feed_clears_chest_blocking_next_belt(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 6.5, "y": 0.5}
        obs["inventory"] = {"transport-belt": 1, "burner-inserter": 1, "stone-furnace": 1, "coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 6}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 5.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "wooden-chest", "unit_number": 22, "position": {"x": 6.5, "y": 0.5}, "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 22)
        self.assertIn("clear blocking wooden-chest", decision.reason)

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
        self.assertEqual(decision.action["count"], 8)

    def test_coal_fuel_feed_extends_coal_belt_to_boiler_before_furnace_receiver(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 5, "burner-inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        # The clean belt run to the boiler is routed in one connect_entities call (FLE-style),
        # starting at the first missing segment.
        self.assertEqual(decision.action["type"], "connect_entities")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["tiles"][0]["position"], {"x": 2.5, "y": 0.5})
        self.assertEqual(decision.action["tiles"][0]["direction"], 4)
        self.assertIn("boiler", decision.reason)

    def test_coal_fuel_feed_prefers_expandable_boiler_bus_opposite_steam_engine(self):
        obs = base_observation()
        obs["resources"] = [{"name": "coal", "position": {"x": 20, "y": 0}, "distance": 20}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 20, "y": 0},
                "direction": planner_module.WEST,
                "mining_target": "coal",
                "drop_position": {"x": 18.5, "y": 0.5},
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "transport-belt",
                "unit_number": 21,
                "position": {"x": 18.5, "y": 0.5},
                "direction": planner_module.WEST,
                "inventories": {"1": {"coal": 1}},
            },
            {
                "name": "boiler",
                "unit_number": 30,
                "position": {"x": 8, "y": 0},
                "status_name": "no_fuel",
                "inventories": {},
            },
            {
                "name": "steam-engine",
                "unit_number": 31,
                "position": {"x": 8, "y": -3.5},
                "direction": planner_module.NORTH,
                "inventories": {},
            },
        ]

        layout = planner_module._coal_boiler_fuel_feed_layout(obs)

        self.assertEqual(layout["target_belt_position"], {"x": 8.5, "y": 2.5})
        self.assertEqual(layout["target_inserter"]["position"], {"x": 8.0, "y": 1.0})
        self.assertEqual(layout["target_inserter"]["direction"], planner_module.SOUTH)

    def test_coal_fuel_feed_repairs_existing_feed_instead_of_routing_over_offshore_pump(self):
        obs = base_observation()
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "drop_position": {"x": 1.3, "y": 0.5},
                "inventories": {"1": {"coal": 3}},
            },
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {"name": "steam-engine", "unit_number": 31, "position": {"x": 8, "y": -3.5}, "direction": planner_module.NORTH, "inventories": {}},
            {"name": "offshore-pump", "unit_number": 32, "position": {"x": 6.5, "y": 0.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "inserter", "unit_number": 33, "position": {"x": 8.5, "y": 2.5}, "direction": planner_module.SOUTH, "inventories": {}},
            {"name": "transport-belt", "unit_number": 34, "position": {"x": 8.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
        ]

        layout = planner_module._coal_boiler_fuel_feed_layout(obs)

        self.assertEqual(layout["target_belt_position"], {"x": 8.5, "y": 2.5})
        self.assertEqual(layout["target_inserter"]["position"], {"x": 8.0, "y": 1.0})
        self.assertEqual(layout["target_inserter"]["direction"], planner_module.SOUTH)

    def test_coal_fuel_feed_refuses_partial_long_boiler_route_when_belt_mall_stock_is_short(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 2, "burner-inserter": 1, "coal": 16}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 8}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 50, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        missing = planner_module._boiler_coal_feed_missing_belt_count(obs)
        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertGreater(missing, 2)
        self.assertIsNone(decision.action)
        self.assertFalse(decision.done)
        self.assertEqual(decision.metadata["repair_skill"], "bootstrap_build_item_mall")
        self.assertEqual(decision.metadata["required_transport_belts"], missing)
        self.assertIn("before partial route extension", decision.reason)

    def test_belt_mall_target_for_boiler_route_ignores_already_placed_belts(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 2, "burner-inserter": 1, "coal": 16}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 8}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 50, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 982,
                "position": {"x": -4, "y": 4},
                "recipe": "iron-gear-wheel",
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "inserter",
                "unit_number": 983,
                "position": {"x": -2, "y": 4},
                "direction": planner_module.WEST,
                "electric_network_connected": True,
                "inventories": {},
            },
        ]
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 5000,
                "position": {"x": 200, "y": 200},
                "inventories": {"1": {"transport-belt": 120}},
            }
        )

        missing = planner_module._boiler_coal_feed_missing_belt_count(obs)
        target = BuildItemMallSkill("transport-belt", 20)._effective_target_count(obs)

        self.assertGreater(planner_module.total_item_count(obs, "transport-belt"), missing)
        self.assertEqual(target, missing + 4)

    def test_belt_mall_target_for_boiler_route_waits_for_automated_mall_input(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 2, "burner-inserter": 1, "coal": 16}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 8}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 50, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        missing = planner_module._boiler_coal_feed_missing_belt_count(obs)
        target = BuildItemMallSkill("transport-belt", 20)._effective_target_count(obs)

        self.assertGreater(missing, 20)
        self.assertEqual(target, 20)

    def test_belt_mall_target_caps_large_llm_target_before_automated_input(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 2, "burner-inserter": 1, "coal": 16}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 8}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 50, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        target = BuildItemMallSkill("transport-belt", 100)._effective_target_count(obs)

        self.assertEqual(target, planner_module.BOOTSTRAP_TRANSPORT_BELT_SEED_TARGET_CAP)

    def test_coal_fuel_feed_prefers_local_receiver_when_boiler_is_working(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 4, "burner-inserter": 1, "coal": 1, "stone-furnace": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {
                "name": "boiler",
                "unit_number": 30,
                "position": {"x": 20, "y": 0},
                "status_name": "working",
                "inventories": {"1": {"wood": 8}},
                "fluids": {"2": {"name": "steam", "amount": 200}},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 2.5, "y": 0.5})
        self.assertIn("coal fuel feed", decision.reason)
        self.assertNotIn("boiler", decision.reason)

    def test_coal_fuel_feed_takes_buffered_gears_for_local_feed_inserter(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "iron-plate": 1, "electronic-circuit": 1, "coal": 1}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "wooden-chest", "unit_number": 28, "position": {"x": 0, "y": 4}, "inventories": {"1": {"iron-gear-wheel": 3}}},
            {
                "name": "boiler",
                "unit_number": 30,
                "position": {"x": 20, "y": 0},
                "status_name": "working",
                "inventories": {"1": {"wood": 8}},
                "fluids": {"2": {"name": "steam", "amount": 200}},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 28)
        self.assertIn("buffered gears", decision.reason)

    def test_coal_fuel_feed_refuels_inactive_coal_source_before_boiler_hand_fuel(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 0, "y": 0}
        obs["inventory"] = {"coal": 8}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "status_name": "no_fuel",
                "mining_target": "coal",
                "drop_position": {"x": 1.3, "y": 0.5},
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 21,
                "position": {"x": 1.5, "y": 0.5},
                "direction": planner_module.EAST,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 20)
        self.assertIn("coal supply site", decision.reason)

    def test_coal_fuel_feed_reuses_existing_boiler_line_with_safe_join(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 20, "burner-inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 7, "y": 7}, "distance": 7}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 7, "y": 7},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "drop_position": {"x": 8.3, "y": 6.5},
                "inventories": {"1": {"coal": 3}},
            },
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 8.5, "y": 6.5}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 9.5, "y": 6.5}, "direction": planner_module.SOUTH, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 9.5, "y": 7.5}, "direction": planner_module.SOUTH, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 9.5, "y": 8.5}, "direction": planner_module.SOUTH, "inventories": {}},
            {"name": "transport-belt", "unit_number": 25, "position": {"x": 9.5, "y": 9.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 8.5, "y": 9.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 7.5, "y": 9.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 40, "position": {"x": 5.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 41, "position": {"x": 4.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 42, "position": {"x": 3.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 43, "position": {"x": 2.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 44, "position": {"x": 1.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 45, "position": {"x": 0.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 46, "position": {"x": -0.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 47, "position": {"x": -1.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 48, "position": {"x": -2.5, "y": 3.5}, "direction": planner_module.WEST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 49, "position": {"x": -3.5, "y": 3.5}, "direction": planner_module.NORTH, "inventories": {}},
            {"name": "transport-belt", "unit_number": 50, "position": {"x": -3.5, "y": 2.5}, "direction": planner_module.NORTH, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": -6.5, "y": 2}, "status_name": "no_fuel", "inventories": {}},
        ]

        layout = planner_module._coal_boiler_fuel_feed_layout(obs)
        missing = [segment for segment in layout["segments"] if not isinstance(segment.get("entity"), dict)]
        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertLessEqual(len(missing), 10)
        # The missing segments are routed in one connect_entities call, beginning at the safe join.
        self.assertEqual(decision.action["type"], "connect_entities")
        self.assertLessEqual(len(decision.action["tiles"]), 8)
        self.assertEqual(decision.action["tiles"][0]["position"], {"x": 6.5, "y": 9.5})
        self.assertEqual(decision.action["tiles"][0]["direction"], planner_module.WEST)
        self.assertNotIn({"x": 6.5, "y": 8.5}, [segment["position"] for segment in missing])

    def test_coal_fuel_feed_takes_belts_from_belt_mall_output_chest_for_boiler_feed(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 5, "y": 2}
        obs["inventory"] = {"burner-inserter": 1, "coal": 1}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            mall_assembler(recipe="transport-belt", inventory={}),
            {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {"1": {"transport-belt": 8}}},
            {
                "name": "inserter",
                "unit_number": 981,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 12,
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["name"], "wooden-chest")
        self.assertEqual(decision.action["item"], "transport-belt")
        self.assertIn("output chest", decision.reason)

    def test_coal_fuel_feed_does_not_repeat_boiler_hand_fuel_after_route_started(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 8, "y": 0}
        obs["inventory"] = {"coal": 5}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0, "y": 4},
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertIsNone(decision.action)
        self.assertFalse(decision.done)
        self.assertIn("route already started", decision.reason)
        self.assertIn("refusing repeated boiler hand-fueling", decision.reason)

    def test_coal_fuel_feed_clears_chest_blocking_boiler_belt_route(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 3, "y": 0}
        obs["inventory"] = {"transport-belt": 4, "burner-inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "wooden-chest", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 22)
        self.assertIn("clear blocking wooden-chest", decision.reason)

    def test_coal_fuel_feed_places_boiler_inserter_after_belt_route(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "inserter": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 7.0, "y": 0.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_coal_fuel_feed_takes_assembler_gears_for_boiler_feed_inserter(self):
        obs = base_observation()
        obs["inventory"] = {"transport-belt": 1, "iron-plate": 1, "electronic-circuit": 1, "coal": 1}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "assembling-machine-1",
                "unit_number": 981,
                "position": {"x": 0.5, "y": 6.5},
                "distance": 6,
                "recipe": "iron-gear-wheel",
                "electric_network_connected": True,
                "inventories": {"1": {"iron-gear-wheel": 3}},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertIn("boiler coal feed construction", decision.reason)

    def test_coal_fuel_feed_gets_powered_inserter_materials_before_retiring_burner_feed_inserter(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 1, "electronic-circuit": 1, "coal": 1}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 25, "position": {"x": 7.0, "y": 0.5}, "direction": 12, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "wooden-chest",
                "unit_number": 981,
                "position": {"x": 0.5, "y": 6.5},
                "inventories": {"1": {"iron-gear-wheel": 3}},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 981)
        self.assertIn("boiler coal feed construction", decision.reason)

    def test_coal_fuel_feed_relocates_existing_inserter_for_boiler_feed(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": -8, "y": -8}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 28, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 27,
                "position": {"x": -8, "y": -8},
                "direction": 4,
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 27)
        self.assertIn("relocate existing inserter", decision.reason)

    def test_coal_fuel_feed_primes_boiler_feed_inserter_not_boiler(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "burner-inserter", "unit_number": 25, "position": {"x": 7.0, "y": 0.5}, "direction": 12, "inventories": {}},
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertIsNone(decision.action)
        self.assertFalse(decision.done)
        self.assertIn("powered inserter", decision.reason)
        self.assertIn("refusing to fuel burner inserter", decision.reason)

    def test_coal_fuel_feed_detects_tile_centered_boiler_inserter_after_build(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 25,
                "position": {"x": 7.0, "y": 0.5},
                "direction": 12,
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertNotEqual(decision.action["type"], "mine")

    def test_coal_fuel_feed_powers_tile_centered_boiler_inserter_before_waiting(self):
        obs = base_observation()
        obs["inventory"] = {"small-electric-pole": 1, "coal": 1}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 25,
                "position": {"x": 7.0, "y": 0.5},
                "direction": 12,
                "electric_network_connected": False,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIn("boiler coal feed", decision.reason)

    def test_coal_fuel_feed_power_pole_avoids_offshore_pump_shoreline(self):
        obs = base_observation()
        obs["inventory"] = {"small-electric-pole": 1}
        obs["resources"] = []
        obs["entities"] = [
            {
                "name": "inserter",
                "unit_number": 217,
                "position": {"x": -37.5, "y": 25.5},
                "direction": planner_module.WEST,
                "electric_network_connected": False,
            },
            {
                "name": "offshore-pump",
                "unit_number": 9,
                "position": {"x": -39.5, "y": 23.5},
                "direction": planner_module.WEST,
                "electric_network_connected": False,
            },
            {
                "name": "small-electric-pole",
                "unit_number": 107,
                "position": {"x": -39.5, "y": 19.5},
                "electric_network_connected": False,
            },
            {"name": "boiler", "unit_number": 26, "position": {"x": -37.5, "y": 23.0}},
            {"name": "stone-furnace", "unit_number": 221, "position": {"x": -36.0, "y": 25.0}},
            {"name": "stone-furnace", "unit_number": 225, "position": {"x": -35.0, "y": 23.0}},
            {"name": "stone-furnace", "unit_number": 226, "position": {"x": -34.0, "y": 25.0}},
        ]

        position = planner_module._select_mall_inserter_power_pole_position(
            obs,
            {"x": -37.5, "y": 25.5},
        )

        self.assertEqual(position, {"x": -38.5, "y": 25.5})

    def test_coal_fuel_feed_done_when_boiler_receives_belt_fed_coal(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": 4, "inventories": {"1": {"coal": 1}}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": 4, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 25,
                "position": {"x": 7.0, "y": 0.5},
                "direction": 12,
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "working", "inventories": {"1": {"coal": 1}}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertTrue(decision.done)
        self.assertIn("boiler coal fuel feed is active", decision.reason)

    def test_coal_fuel_feed_seeds_boiler_once_when_electric_feed_lacks_power(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 8, "y": 0}
        obs["inventory"] = {"coal": 2}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "drop_position": {"x": 1.3, "y": 0.5},
                "inventories": {"1": {"coal": 3}},
            },
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 25,
                "position": {"x": 7.0, "y": 0.5},
                "direction": planner_module.WEST,
                "status_name": "no_power",
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["name"], "boiler")
        self.assertEqual(decision.action["unit_number"], 30)
        self.assertEqual(decision.action["item"], "coal")
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "boiler_coal_feed_power_seed")

    def test_coal_fuel_feed_takes_existing_fuel_before_power_seed_when_inventory_empty(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 0, "y": -3}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "mining_target": "coal",
                "drop_position": {"x": 1.3, "y": 0.5},
                "inventories": {"1": {"coal": 1}},
            },
            {"name": "transport-belt", "unit_number": 21, "position": {"x": 1.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 22, "position": {"x": 2.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 23, "position": {"x": 3.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 24, "position": {"x": 4.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 26, "position": {"x": 5.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {"name": "transport-belt", "unit_number": 27, "position": {"x": 6.5, "y": 0.5}, "direction": planner_module.EAST, "inventories": {}},
            {
                "name": "inserter",
                "unit_number": 25,
                "position": {"x": 7.0, "y": 0.5},
                "direction": planner_module.WEST,
                "status_name": "no_power",
                "electric_network_connected": True,
                "inventories": {},
            },
            {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
            {
                "name": "stone-furnace",
                "unit_number": 40,
                "position": {"x": 0, "y": -3},
                "inventories": {"1": {"coal": 5}},
            },
        ]

        decision = CoalFuelFeedSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 1)
        self.assertEqual(decision.action["unit_number"], 40)
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "boiler_coal_feed_power_seed")

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

    def test_factory_layout_flags_small_power_pole_clutter(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "small-electric-pole",
                "unit_number": 910 + index,
                "position": {"x": float(index % 4), "y": float(index // 4)},
                "electric_network_connected": True,
            }
            for index in range(12)
        ]

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "power_pole_clutter")

        self.assertEqual(issue["item"], "electric-power")
        self.assertEqual(issue["parameters"]["small_pole_count"], 12)
        self.assertEqual(issue["parameters"]["cleanup_policy"], "validate_power_coverage_before_mining_poles")
        self.assertIn("pole mesh", issue["detail"])

    def test_factory_layout_flags_belt_crossing_without_underground(self):
        obs = base_observation()
        obs["research"]["technologies"]["logistics"] = {"researched": True, "enabled": True}
        obs["entities"] = [
            {"name": "transport-belt", "unit_number": 930, "position": {"x": -1, "y": 0}, "direction": planner_module.EAST},
            {"name": "transport-belt", "unit_number": 931, "position": {"x": 1, "y": 0}, "direction": planner_module.EAST},
            {"name": "transport-belt", "unit_number": 932, "position": {"x": 2, "y": 0}, "direction": planner_module.EAST},
            {"name": "transport-belt", "unit_number": 933, "position": {"x": 0, "y": -1}, "direction": planner_module.SOUTH},
            {"name": "transport-belt", "unit_number": 934, "position": {"x": 0, "y": 1}, "direction": planner_module.SOUTH},
            {"name": "transport-belt", "unit_number": 935, "position": {"x": 0, "y": 2}, "direction": planner_module.SOUTH},
        ]

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "belt_crossing_without_underground")

        self.assertEqual(issue["item"], "transport-belt")
        self.assertTrue(issue["parameters"]["underground_belt_available"])
        self.assertEqual(issue["parameters"]["required_item"], "underground-belt")
        self.assertIn("underground-belt", issue["recommendation"])

    def test_factory_layout_recommends_main_bus_corridor_side(self):
        obs = base_observation()
        obs["research"]["technologies"]["logistics"] = {"researched": True, "enabled": True}
        obs["resources"].append({"name": "iron-ore", "position": {"x": 10, "y": 12}, "distance": 12})
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 950 + index,
                "position": {"x": float(index * 2), "y": 0.0},
                "direction": planner_module.EAST,
            }
            for index in range(12)
        ]
        obs["entities"].extend(
            {
                "name": "assembling-machine-1",
                "unit_number": 980 + index,
                "recipe": "transport-belt",
                "position": {"x": float(index * 4), "y": 4.0},
                "electric_network_connected": True,
            }
            for index in range(4)
        )

        issues = factory_layout_issues(obs)
        issue = next(item for item in issues if item["kind"] == "main_bus_corridor_candidate")

        self.assertEqual(issue["item"], "factory_layout")
        self.assertEqual(issue["parameters"]["axis"], "east_west")
        self.assertEqual(issue["parameters"]["side"], "north")
        self.assertEqual(issue["parameters"]["crossing_policy"], "underground_or_splitter_only")
        self.assertIn("main bus corridor", issue["detail"])

    def test_local_seed_source_does_not_steal_assembler_input_materials(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 970,
                "recipe": "inserter",
                "position": {"x": 1.0, "y": 0.0},
                "inventories": {"2": {"iron-plate": 3}},
                "electric_network_connected": True,
            },
            {
                "name": "stone-furnace",
                "unit_number": 971,
                "position": {"x": 3.0, "y": 0.0},
                "inventories": {"3": {"iron-plate": 12}},
            },
        ]

        source = planner_module._nearest_local_item_seed_source(obs, "iron-plate", {"x": 0.0, "y": 0.0})

        self.assertEqual(source["unit_number"], 971)

    def test_local_seed_source_allows_assembler_product_output(self):
        obs = base_observation()
        obs["entities"] = [
            {
                "name": "assembling-machine-1",
                "unit_number": 972,
                "recipe": "transport-belt",
                "position": {"x": 1.0, "y": 0.0},
                "inventories": {"3": {"transport-belt": 8}},
                "electric_network_connected": True,
            },
        ]

        source = planner_module._nearest_local_item_seed_source(obs, "transport-belt", {"x": 0.0, "y": 0.0})

        self.assertEqual(source["unit_number"], 972)

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
        self.assertIn("needs 23 small-electric-pole", decision.reason)
        self.assertIn("before mining the existing mall", decision.reason)

    def test_gear_belt_mall_relocation_repairs_power_pole_mall_before_teardown(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 1, "wood": 4, "copper-cable": 8}
        obs["automation_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 9604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "cable_assembler_position": {"x": 2, "y": 2},
                "circuit_assembler_position": {"x": 6, "y": 2},
                "transfer_inserter_position": {"x": 4, "y": 2},
                "transfer_inserter_direction": 4,
                "distance": 4,
            }
        ]
        for entity in powered_research_observation()["entities"]:
            copied = dict(entity)
            copied["unit_number"] = int(copied.get("unit_number") or 0) + 9000
            obs["entities"].append(copied)
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 903,
                "recipe": "small-electric-pole",
                "position": {"x": 2, "y": 2},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "copper-cable")
        self.assertEqual(decision.action["unit_number"], 903)
        self.assertIn("bootstrap small-electric-pole", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "relocation_power_pole_shortage")
        self.assertEqual(decision.metadata["repair_skill"], "bootstrap_build_item_mall")
        self.assertEqual(decision.metadata["target_item"], "small-electric-pole")

    def test_gear_belt_mall_relocation_takes_buffered_power_poles_before_teardown(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 1}
        obs["player"]["position"] = {"x": 7.0, "y": 0.5}
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 901,
                "position": {"x": 7.0, "y": 0.5},
                "inventories": {"1": {"small-electric-pole": 22}},
            }
        )

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "small-electric-pole")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertIn("buffered small electric poles", decision.reason)

    def test_gear_belt_mall_relocation_recovers_assembler_once_power_pole_materials_exist(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 100)
        self.assertIn("after relocation power corridor materials are available", decision.reason)

    def test_gear_belt_mall_relocation_builds_power_corridor_after_assemblers_recovered(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23, "assembling-machine-1": 2}
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIs(decision.action["allow_nearby"], False)
        self.assertIn("power corridor before mining existing mall", decision.reason)

    def test_gear_belt_mall_relocation_power_corridor_rounding_stays_in_wire_reach(self):
        obs = long_gear_mall_relocation_observation()
        obs["entities"][0]["position"] = {"x": -43.5, "y": 15.5}
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)
        anchor = planner_module._nearest_connected_power_anchor(
            obs,
            planner_module._gear_belt_mall_relocation_power_target(layout),
        )
        previous = planner_module._position(anchor)
        gaps = []
        for position in positions:
            gaps.append(planner_module.distance(previous, position))
            previous = position

        self.assertLessEqual(max(gaps), planner_module._power_wire_reach("small-electric-pole"))

    def test_power_anchor_uses_network_id_pole_on_powered_network(self):
        obs = {
            "entities": [
                {
                    "name": "steam-engine",
                    "unit_number": 900,
                    "position": {"x": 0.5, "y": 0.5},
                    "electric_network_connected": True,
                    "electric_network_id": 1,
                    "inventories": {},
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": 901,
                    "position": {"x": 30.5, "y": 0.5},
                    "electric_network_connected": False,
                    "electric_network_id": 1,
                    "inventories": {},
                },
            ]
        }

        anchor = planner_module._nearest_connected_power_anchor(obs, {"x": 40.5, "y": 0.5})

        self.assertEqual(anchor["unit_number"], 901)

    def test_gear_belt_mall_relocation_power_corridor_starts_with_generator_side_pole(self):
        obs = long_gear_mall_relocation_observation()
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("name") != "small-electric-pole"]
        obs["entities"].append(
            {
                "name": "steam-engine",
                "unit_number": 900,
                "position": {"x": -43.5, "y": 15.5},
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)

        self.assertEqual(positions[0], {"x": -45.5, "y": 15.5})
        self.assertLessEqual(
            planner_module.distance(positions[0], positions[1]),
            planner_module._power_wire_reach("small-electric-pole"),
        )

    def test_gear_belt_mall_relocation_detours_power_corridor_around_crash_artifact(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23, "assembling-machine-1": 2}
        obs["player"]["position"] = {"x": 0.5, "y": 0.5}
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
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
        self.assertIs(decision.action["allow_nearby"], False)
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

    def test_gear_belt_mall_relocation_bridges_snapped_power_corridor_gap(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 1, "assembling-machine-1": 2}
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)
        split_index = len(positions) - 2
        previous_actual = None
        shifted_actual = None
        for index, position in enumerate(positions):
            actual = dict(position)
            network_id = 1
            if index == split_index:
                actual = {"x": position["x"] + 0.5, "y": position["y"] + 1.5}
                shifted_actual = actual
                network_id = 2
            elif index > split_index:
                network_id = 2
            elif index == split_index - 1:
                previous_actual = actual
            obs["entities"].append(
                {
                    "name": "small-electric-pole",
                    "unit_number": 7000 + index,
                    "position": actual,
                    "electric_network_connected": False,
                    "electric_network_id": network_id,
                    "inventories": {},
                }
            )
        obs["player"]["position"] = previous_actual

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertLessEqual(
            planner_module.distance(decision.action["position"], previous_actual),
            planner_module._power_wire_reach("small-electric-pole"),
        )
        self.assertLessEqual(
            planner_module.distance(decision.action["position"], shifted_actual),
            planner_module._power_wire_reach("small-electric-pole"),
        )
        self.assertIn("power corridor before mining existing mall", decision.reason)

    def test_gear_belt_mall_relocation_does_not_rebuild_existing_detour_pole(self):
        obs = long_gear_mall_relocation_observation()
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)
        desired = positions[3]
        obs["entities"].append(
            {
                "name": "tree-02-red",
                "unit_number": 7100,
                "type": "tree",
                "position": desired,
                "inventories": {},
            }
        )
        first_detour = planner_module._select_power_corridor_build_position(obs, positions, desired)
        obs["entities"].append(
            {
                "name": "small-electric-pole",
                "unit_number": 7101,
                "position": first_detour,
                "electric_network_connected": False,
                "inventories": {},
            }
        )

        second_detour = planner_module._select_power_corridor_build_position(obs, positions, desired)

        self.assertNotEqual(second_detour, first_detour)
        self.assertIsNone(
            planner_module._existing_power_connector_near_position(obs, second_detour, radius=0.75)
        )

    def test_gear_belt_mall_relocation_clears_recoverable_power_corridor_blocker(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23, "assembling-machine-1": 2}
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        positions = planner_module._gear_belt_mall_relocation_power_corridor_positions(obs, layout)
        blocked_position = positions[0]
        obs["player"]["position"] = blocked_position
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 7102,
                "position": blocked_position,
                "inventories": {},
            }
        )

        with patch("factorio_ai.planner._select_power_corridor_build_position", return_value=blocked_position):
            decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 7102)
        self.assertIn("clear blocking stone-furnace", decision.reason)

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
                    "position": {"x": 162.5, "y": -4.0},
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

    def test_gear_belt_mall_relocation_continues_after_first_target_assembler_placed(self):
        obs = long_gear_mall_relocation_observation()
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        _add_existing_relocation_power_corridor(obs)
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        target_position = {
            "x": layout["target_gear_position"]["x"],
            "y": layout["target_gear_position"]["y"] + 0.5,
        }
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 800,
                "position": target_position,
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["player"]["position"] = target_position

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["unit_number"], 800)
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertIn("set relocated gear assembler recipe", decision.reason)

    def test_gear_belt_mall_relocation_does_not_recover_already_placed_target_assembler(self):
        obs = long_gear_mall_relocation_observation()
        for entity in obs["entities"]:
            if entity.get("unit_number") == 200:
                entity["position"] = {"x": 47.0, "y": 63.0}
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
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
                    "position": layout["target_gear_position"],
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 801,
                    "recipe": "transport-belt",
                    "position": {"x": 52.5, "y": 62.5},
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )
        obs["inventory"] = {}
        obs["player"]["position"] = {"x": 52.5, "y": 62.5}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 801)
        self.assertNotEqual(decision.action["unit_number"], 800)
        self.assertIn("existing belt assembler", decision.reason)

    def test_gear_belt_mall_relocation_clears_tree_blocking_target_assembler(self):
        obs = long_gear_mall_relocation_observation()
        for entity in obs["entities"]:
            if entity.get("unit_number") == 200:
                entity["position"] = {"x": 47.0, "y": 63.0}
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
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
                    "position": layout["target_gear_position"],
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "tree-02-red",
                    "type": "tree",
                    "position": {
                        "x": layout["target_belt_position"]["x"] + 1.5,
                        "y": layout["target_belt_position"]["y"] + 0.4,
                    },
                    "inventories": {},
                },
            ]
        )
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["player"]["position"] = layout["target_belt_position"]

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "tree-02-red")
        self.assertIn("clear blocking tree-02-red", decision.reason)

    def test_gear_belt_mall_relocation_accepts_wire_reachable_corridor_before_done(self):
        obs = long_gear_mall_relocation_observation()
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        _add_existing_relocation_power_corridor(obs)
        obs["entities"] = [
            entity for entity in obs["entities"] if entity.get("unit_number") not in {100, 101}
        ]
        first_corridor_pole_position = None
        for entity in obs["entities"]:
            if entity.get("name") == "small-electric-pole" and entity.get("unit_number", 0) >= 7000:
                entity["electric_network_connected"] = False
            if entity.get("unit_number") == 7000:
                first_corridor_pole_position = entity["position"]
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 800,
                    "recipe": "iron-gear-wheel",
                    "position": layout["target_gear_position"],
                    "electric_network_connected": False,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 801,
                    "recipe": "transport-belt",
                    "position": layout["target_belt_position"],
                    "electric_network_connected": False,
                    "inventories": {},
                },
            ]
        )
        obs["player"]["position"] = first_corridor_pole_position
        obs["inventory"] = {}

        decision = GearBeltMallRelocationSkill(20).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIn("gear/belt mall assemblers are relocated", decision.reason)

    def test_gear_belt_mall_relocation_leaves_inserter_gap_between_assemblers(self):
        obs = long_gear_mall_relocation_observation()
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        self.assertEqual(
            layout["target_belt_position"]["x"] - layout["target_gear_position"]["x"],
            4.0,
        )

    def test_gear_belt_mall_relocation_accepts_vertical_existing_pair(self):
        obs = long_gear_mall_relocation_observation()
        obs["entities"][2]["position"] = {"x": 0.5, "y": -2.5}

        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        self.assertIsNotNone(layout)
        self.assertEqual(layout["gear_assembler"]["unit_number"], 100)
        self.assertEqual(layout["belt_assembler"]["unit_number"], 101)
        self.assertEqual(layout["route_cost_preference"], "relocate_mall_to_iron_source")
        self.assertEqual(
            layout["target_belt_position"]["x"] - layout["target_gear_position"]["x"],
            4.0,
        )

    def test_gear_belt_mall_relocation_avoids_resource_under_target_machine(self):
        obs = long_gear_mall_relocation_observation()
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 158.5, "y": -4.5}, "distance": 8},
        ]

        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        self.assertNotEqual(layout["target_gear_position"], {"x": 158.5, "y": -4.5})
        self.assertFalse(
            planner_module._planned_machine_over_protected_resource(
                obs,
                layout["target_gear_position"],
            )
        )

    def test_gear_belt_mall_relocation_targets_use_entity_centers_for_integer_source_y(self):
        obs = long_gear_mall_relocation_observation()
        for entity in obs["entities"]:
            if entity.get("unit_number") == 200:
                entity["position"] = {"x": 47.0, "y": 63.0}

        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)

        self.assertEqual(layout["target_gear_position"]["y"], 58.5)
        self.assertEqual(layout["target_belt_position"]["y"], 58.5)
        self.assertEqual(layout["target_gear_position"]["x"], 52.5)
        self.assertEqual(layout["target_belt_position"]["x"], 56.5)

    def test_gear_belt_mall_relocation_ignores_unconnected_power_anchor(self):
        obs = long_gear_mall_relocation_observation()
        layout = planner_module._find_gear_belt_mall_relocation_layout(obs)
        target = planner_module._gear_belt_mall_relocation_power_target(layout)
        obs["entities"].append(
            {
                "name": "small-electric-pole",
                "unit_number": 999,
                "position": {"x": target["x"] - 1.0, "y": target["y"]},
                "electric_network_connected": False,
                "inventories": {},
            }
        )

        anchor = planner_module._nearest_connected_power_anchor(obs, target)

        self.assertEqual(anchor["unit_number"], 90)

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

    def test_gear_belt_mall_relocation_recovers_unpowered_remaining_belt_assembler(self):
        obs = long_gear_mall_relocation_observation()
        obs["inventory"] = {"small-electric-pole": 23, "assembling-machine-1": 1}
        obs["player"]["position"] = {"x": 3.5, "y": 0.5}
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("unit_number") != 100]
        for entity in obs["entities"]:
            if entity.get("unit_number") == 101:
                entity["electric_network_connected"] = False
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

    def test_transport_belt_mall_inserts_plate_when_gears_are_already_buffered(self):
        obs = powered_automation_observation()
        obs["player"]["position"] = {"x": 86.5, "y": -57.5}
        obs["inventory"] = {"iron-plate": 1}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 146,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 82.5, "y": -57.5},
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 147,
                    "recipe": "transport-belt",
                    "position": {"x": 86.5, "y": -57.5},
                    "electric_network_connected": True,
                    "inventories": {"2": {"iron-gear-wheel": 3}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 2).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 147)
        self.assertIn("transport-belt mall assembler", decision.reason)

    def test_transport_belt_mall_does_not_ping_pong_between_partial_cells(self):
        obs = powered_automation_observation()
        obs["player"]["position"] = {"x": 2.0, "y": 2.0}
        obs["inventory"] = {"iron-plate": 1, "iron-gear-wheel": 1}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 147,
                    "recipe": "transport-belt",
                    "position": {"x": 2.0, "y": 2.0},
                    "distance": 200,
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-gear-wheel": 1}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 148,
                    "recipe": "transport-belt",
                    "position": {"x": 24.0, "y": 2.0},
                    "distance": 1,
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-plate": 1}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 2).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 147)
        self.assertNotEqual(decision.action.get("type"), "move_to")

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

    def test_factory_layout_adds_unlock_reassessment_when_electric_drills_replace_burner_mining(self):
        obs = base_observation()
        obs["research"]["technologies"]["electric-mining-drill"] = {"researched": True}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 1,
                "position": {"x": 4, "y": 0},
                "inventories": {"1": {"coal": 1}},
            },
            {"name": "transport-belt", "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
            {"name": "transport-belt", "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
            {
                "name": "burner-inserter",
                "position": {"x": 8, "y": 0},
                "direction": 4,
                "inventories": {"1": {"coal": 1}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 2,
                "position": {"x": 9, "y": 0},
                "inventories": {"1": {"coal": 1}},
            },
        ]

        opportunities = factory_layout_opportunities(obs)
        opportunity = next(item for item in opportunities if item["kind"] == "unlock_layout_reassessment")

        self.assertIn("electric-mining-drill", opportunity["parameters"]["retool_tools"])
        self.assertIn("plate_smelting_line", opportunity["parameters"]["affected_site_kinds"])
        self.assertTrue(
            any("burner-mining-drill" in pattern for pattern in opportunity["parameters"]["obsolete_patterns"])
        )

    def test_factory_layout_flags_splitter_fanout_for_one_source_two_consumers(self):
        class Link:
            def __init__(self, row):
                self.row = row

            def to_dict(self):
                return self.row

        obs = base_observation()
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        links = [
            Link(
                {
                    "from_site": "gear:1",
                    "to_site": "science:1",
                    "item": "iron-gear-wheel",
                    "status": "route_needed",
                }
            ),
            Link(
                {
                    "from_site": "gear:1",
                    "to_site": "inserter:1",
                    "item": "iron-gear-wheel",
                    "status": "route_needed",
                }
            ),
        ]

        with patch("factorio_ai.planner.estimate_logistics_links", return_value=links):
            opportunities = factory_layout_opportunities(obs)

        opportunity = next(item for item in opportunities if item["kind"] == "splitter_output_fanout_needed")
        self.assertEqual(opportunity["item"], "iron-gear-wheel")
        self.assertEqual(opportunity["parameters"]["required_item"], "splitter")
        self.assertIn("do not pull two separate belts", opportunity["recommendation"])

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
        self.assertFalse(decision.action["allow_nearby"])
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

    def test_iron_skill_recovers_temporary_non_coal_drill_when_no_new_drill_is_available(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 12, "y": 0}}
        obs["inventory"] = {
            "coal": 8,
            "stone": 5,
            "stone-furnace": 1,
            "iron-gear-wheel": 3,
        }
        obs["resources"] = [
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 10},
            {"name": "stone", "position": {"x": 12, "y": 0}, "distance": 0},
            {"name": "copper-ore", "position": {"x": 18, "y": 0}, "distance": 6},
            {"name": "iron-ore", "position": {"x": 40, "y": 0}, "distance": 28},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 2, "y": 0},
                "mining_target": "coal",
                "inventories": {"1": {"coal": 4}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 30,
                "position": {"x": 12, "y": 0},
                "mining_target": "stone",
                "status_name": "no_fuel",
                "inventories": {},
            },
        ]

        decision = IronPlateSkill(target_count=5).next_action(obs, target_count=5, inventory_only=True)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 30)
        self.assertIn("temporary stone burner mining drill", decision.reason)

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
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["unit_number"], 102)
        self.assertIn("misplaced direct iron-plate furnace", decision.reason)

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
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 8)
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

    def test_iron_skill_buffers_direct_cell_with_large_coal_insert(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 4, "y": 0}}
        obs["inventory"] = {"coal": 30}
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 0,
                "mining_target": "iron-ore",
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 2,
                "inventories": {},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 20)
        self.assertEqual(decision.action["unit_number"], 101)

    def test_iron_skill_mines_large_coal_batch_for_low_direct_cell(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 2, "y": 0}}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 2},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 0},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 2,
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 4,
                "inventories": {"1": {"coal": 3}},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "coal")
        self.assertEqual(decision.action["count"], 30)

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
                "inventories": {"1": {"wood": 20}},
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

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "coal")
        self.assertEqual(decision.action["count"], 30)

    def test_iron_skill_recovers_direct_drill_with_no_minable_resources(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 75, "y": -69}}
        obs["inventory"] = {"wood": 5}
        obs["resources"] = [{"name": "iron-ore", "position": {"x": 78, "y": -69}, "distance": 3}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 14,
                "position": {"x": 75, "y": -69},
                "direction": planner_module.EAST,
                "status_name": "no_minable_resources",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 15,
                "position": {"x": 77, "y": -69},
                "status_name": "no_ingredients",
                "inventories": {"1": {"wood": 3}},
            },
        ]

        decision = IronPlateSkill(target_count=20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 14)
        self.assertIn("no minable resources", decision.reason)

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

    def test_science_skill_uses_seed_aware_mall_after_automation(self):
        obs = powered_automation_observation()
        obs["player"]["character_valid"] = False
        obs["execution"] = {"virtual": True}
        obs["inventory"] = {"iron-plate": 10, "copper-plate": 4, "electronic-circuit": 1}
        obs["craftable"] = {"iron-gear-wheel": 4}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="automation-science-pack",
                gear_inventory={},
                belt_inventory={"copper-plate": 4},
            )
        )
        obs["entities"].extend(
            [
                {
                    "name": "wooden-chest",
                    "unit_number": 980,
                    "position": {"x": 5.0, "y": 2.0},
                    "inventories": {"1": {"iron-gear-wheel": 37}},
                },
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = AutomationScienceSkill(target_count=5).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertNotIn("wait for iron gear wheel mall output", decision.reason)

    def test_science_mall_takes_automated_output_gears_for_input(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["craftable"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="automation-science-pack",
                gear_inventory={},
                belt_inventory={"copper-plate": 4},
            )
        )
        obs["entities"].extend(
            [
                {
                    "name": "wooden-chest",
                    "unit_number": 980,
                    "position": {"x": 5.0, "y": 2.0},
                    "inventories": {"1": {"iron-gear-wheel": 12}},
                },
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("automation-science-pack", 5).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertIn("chest-buffered iron gears", decision.reason)

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

    def test_direct_smelting_drill_from_resource_tile_uses_entity_center(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 47, "y": 67}}
        obs["inventory"] = {"coal": 8, "burner-mining-drill": 1, "stone-furnace": 1}
        obs["resources"] = [{"name": "iron-ore", "position": {"x": 46.5, "y": 66.5}, "distance": 2}]

        decision = IronPlateSkill(target_count=5).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 47, "y": 67})
        self.assertFalse(decision.action["allow_nearby"])

    def test_direct_smelting_skips_orientation_blocked_by_existing_belts(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 47, "y": 67}}
        obs["inventory"] = {"coal": 8, "burner-mining-drill": 1, "stone-furnace": 1}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 46.5, "y": 66.5}, "distance": 2},
            {"name": "iron-ore", "position": {"x": 39.5, "y": 62.5}, "distance": 9},
        ]
        obs["entities"] = [
            {"name": "transport-belt", "type": "transport-belt", "unit_number": 850, "position": {"x": 49.5, "y": 66.5}},
            {"name": "inserter", "type": "inserter", "unit_number": 893, "position": {"x": 48.5, "y": 63.5}},
        ]

        decision = IronPlateSkill(target_count=5).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 47, "y": 67})
        self.assertEqual(decision.action["direction"], 12)

    def test_direct_smelting_recovers_unpaired_drill_when_no_open_layout_exists(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 0, "y": 0}, "distance": 0},
            {"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 701,
                "position": {"x": 0, "y": 0},
                "direction": planner_module.EAST,
                "distance": 0,
                "mining_target": "iron-ore",
                "inventories": {},
            },
            {"name": "transport-belt", "unit_number": 702, "position": {"x": 2, "y": 0}, "inventories": {}},
            {"name": "transport-belt", "unit_number": 703, "position": {"x": -2, "y": 0}, "inventories": {}},
            {"name": "transport-belt", "unit_number": 704, "position": {"x": 0, "y": 2}, "inventories": {}},
            {"name": "transport-belt", "unit_number": 705, "position": {"x": 0, "y": -2}, "inventories": {}},
        ]

        decision = IronPlateSkill(target_count=10).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 701)
        self.assertIn("recover incomplete direct iron-ore burner mining drill", decision.reason)

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

    def test_iron_skill_expands_second_direct_cell_instead_of_waiting(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 4, "y": 6}}
        obs["inventory"] = {"coal": 20, "burner-mining-drill": 1, "stone-furnace": 1}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 6},
            {"name": "iron-ore", "position": {"x": 4, "y": 6}, "distance": 0},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 8},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 6,
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 20}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 8,
                "inventories": {"1": {"coal": 20}, "2": {"iron-ore": 1}},
            },
        ]

        decision = IronPlateSkill(target_count=40).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["position"], {"x": 4, "y": 6})
        self.assertNotEqual(decision.action["type"], "wait")

    def test_iron_skill_does_not_expand_burner_cells_after_electric_drill_research(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 4, "y": 6}}
        obs["inventory"] = {"coal": 20, "burner-mining-drill": 1, "stone-furnace": 1}
        obs["research"]["technologies"]["electric-mining-drill"] = {"researched": True}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 6},
            {"name": "iron-ore", "position": {"x": 4, "y": 6}, "distance": 0},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 8},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "direction": 4,
                "distance": 6,
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 20}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 6, "y": 0},
                "distance": 8,
                "inventories": {"1": {"coal": 20}, "2": {"iron-ore": 1}},
            },
        ]

        decision = IronPlateSkill(target_count=40).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("direct iron-plate", decision.reason)

    def test_copper_skill_does_not_accept_one_tile_gap_direct_furnace(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 11, "y": 0}}
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
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["unit_number"], 702)
        self.assertIn("misplaced direct copper-plate furnace", decision.reason)

    def test_iron_skill_recovers_shifted_direct_drill_before_rebuilding_cell(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 47, "y": 67}}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["resources"] = [{"name": "iron-ore", "position": {"x": 39.5, "y": 62.5}, "distance": 9}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 1019,
                "position": {"x": 47, "y": 67},
                "direction": 4,
                "drop_position": {"x": 48.296875, "y": 66.5},
                "distance": 0,
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 3}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 15,
                "position": {"x": 47, "y": 63},
                "distance": 4,
                "recipe": "iron-plate",
                "status_name": "no_ingredients",
                "inventories": {"1": {"coal": 3}},
            },
        ]

        decision = IronPlateSkill(target_count=5).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["unit_number"], 1019)
        self.assertIn("misplaced direct iron-plate mining drill", decision.reason)
        self.assertNotIn("wait for direct", decision.reason)

    def test_iron_skill_recovers_nearby_unpaired_drill_before_far_refuel_trip(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 78, "y": -23}}
        obs["inventory"] = {}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 77},
            {"name": "iron-ore", "position": {"x": 76, "y": -23}, "distance": 2},
            {"name": "coal", "position": {"x": 9.5, "y": -5.5}, "distance": 71},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 891,
                "position": {"x": 4, "y": 0},
                "direction": planner_module.EAST,
                "distance": 77,
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 1}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 892,
                "position": {"x": 6, "y": 0},
                "distance": 76,
                "inventories": {"1": {"coal": 1}, "2": {"iron-ore": 1}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 896,
                "position": {"x": 76, "y": -23},
                "direction": planner_module.EAST,
                "distance": 2,
                "mining_target": "iron-ore",
                "status_name": "waiting_for_space_in_destination",
                "inventories": {"1": {"coal": 4}},
            },
        ]

        decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["unit_number"], 896)
        self.assertIn("recover incomplete direct iron-ore burner mining drill", decision.reason)
        self.assertNotIn("move near coal", decision.reason)

    def test_iron_skill_recovers_nearby_unpaired_drill_with_no_open_layout_before_refuel(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 78, "y": -23}}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 9.5, "y": -5.5}, "distance": 71}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 891,
                "position": {"x": 76, "y": -23},
                "direction": planner_module.EAST,
                "distance": 2,
                "mining_target": "iron-ore",
                "status_name": "waiting_for_space_in_destination",
                "inventories": {"1": {"coal": 20}},
            }
        ]

        decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["unit_number"], 891)
        self.assertIn("recover incomplete direct iron-ore burner mining drill", decision.reason)
        self.assertNotIn("move near coal", decision.reason)

    def test_iron_skill_recovers_nearby_unpaired_drill_even_when_drill_inventory_exists(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 78, "y": -23}}
        obs["inventory"] = {"burner-mining-drill": 1, "coal": 20}
        obs["resources"] = [{"name": "coal", "position": {"x": 9.5, "y": -5.5}, "distance": 71}]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 896,
                "position": {"x": 79, "y": -25},
                "direction": planner_module.EAST,
                "distance": 2,
                "mining_target": "iron-ore",
                "status_name": "waiting_for_space_in_destination",
                "inventories": {"1": {"coal": 4}},
            }
        ]

        decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertEqual(decision.action["unit_number"], 896)
        self.assertIn("recover incomplete direct iron-ore burner mining drill", decision.reason)

    def test_iron_skill_does_not_recover_belt_smelting_drill_as_direct_cell(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4, "y": 0}}
        obs["inventory"] = {"coal": 30, "iron-plate": 4}
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 0},
            {"name": "coal", "position": {"x": 0, "y": 2}, "distance": 4},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))

        decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertNotEqual(decision.action.get("type"), "mine")
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 500)
        self.assertIn("expanded iron-plate smelting", decision.reason)

    def test_iron_skill_does_not_recover_belt_smelting_furnace_as_direct_cell(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}}
        obs["inventory"] = {
            "iron-plate": 4,
            "coal": 30,
            "small-electric-pole": 1,
            "burner-mining-drill": 1,
        }
        obs["resources"] = [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "coal", "position": {"x": 0, "y": 2}, "distance": 2},
        ]
        obs["entities"] = complete_belt_smelting_entities(1, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
                entity["status"] = 54
                entity["inventories"] = {}

        decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotEqual(decision.action.get("name"), "stone-furnace")
        self.assertIn("expanded iron-plate smelting input inserter", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "direct_iron_smelting_site_blocked")

    def test_iron_skill_mines_nearby_coal_before_misplaced_furnace_repair(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}}
        obs["inventory"] = {
            "stone-furnace": 1,
            "burner-mining-drill": 1,
        }
        obs["resources"] = [
            {"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0},
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 4},
        ]
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 901,
                "position": {"x": 4, "y": 0},
                "direction": planner_module.EAST,
                "distance": 4,
                "mining_target": "iron-ore",
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 902,
                "position": {"x": 5, "y": 0},
                "distance": 5,
                "inventories": {},
            },
        ]

        decision = IronPlateSkill(target_count=10).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "coal")
        self.assertIn("mine coal", decision.reason)
        self.assertNotIn("misplaced direct iron-plate furnace", decision.reason)

    def test_iron_skill_routes_blocked_direct_site_to_expand_smelting_after_belt_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4}
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        direct_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for direct burner-drill smelting cell",
        )
        expansion = planner_module.PlannerDecision(
            {"type": "craft", "recipe": "transport-belt", "count": 1},
            "craft transport-belt for line",
        )

        with (
            patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_blocked),
            patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion),
        ):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "transport-belt")
        self.assertIn("expanded belt smelting", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "direct_iron_smelting_site_blocked")
        self.assertEqual(decision.metadata["repair_skill"], "expand_iron_smelting")

    def test_iron_skill_repairs_unpowered_expanded_smelting_after_direct_site_blocked(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {"iron-plate": 4, "small-electric-pole": 1, "coal": 30}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
                entity["status"] = 54
                entity["inventories"] = {}
        direct_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for direct burner-drill smelting cell",
        )

        with patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_blocked):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotEqual(decision.action["type"], "wait")
        self.assertIn("expanded iron-plate smelting input inserter", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "direct_iron_smelting_site_blocked")
        self.assertEqual(decision.metadata["repair_skill"], "expand_iron_smelting")

    def test_iron_skill_waits_on_active_expanded_smelting_when_no_more_iron_site_is_open(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4, "coal": 30}
        obs["resources"] = []
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        direct_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for direct burner-drill smelting cell",
        )

        with patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_blocked):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("wait for existing expanded iron-plate smelting output", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "direct_iron_smelting_site_blocked")
        self.assertEqual(decision.metadata["repair_skill"], "expand_iron_smelting")

    def test_iron_skill_waits_on_active_expanded_smelting_instead_of_far_direct_coal_trip(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4}
        obs["resources"] = [{"name": "coal", "position": {"x": 80.0, "y": 0.0}, "distance": 80}]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        direct_support = planner_module.PlannerDecision(
            {"type": "move_to", "position": {"x": 80.0, "y": 0.0}, "tolerance": 7.5},
            "move near coal",
        )

        with patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_support):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("existing expanded iron-plate smelting", decision.reason)
        self.assertNotEqual(decision.action.get("position"), {"x": 80.0, "y": 0.0})

    def test_iron_skill_repairs_fuel_logistics_instead_of_direct_coal_trip(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4, "transport-belt": 4}
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        direct_support = planner_module.PlannerDecision(
            {"type": "move_to", "position": {"x": 80.0, "y": 0.0}, "tolerance": 7.5},
            "move near coal",
        )
        expansion_blocked = planner_module.PlannerDecision(
            None,
            "expanded iron-plate smelting needs fuel logistics before more walking refuels",
        )
        coal_feed = planner_module.PlannerDecision(
            {
                "type": "build",
                "name": "transport-belt",
                "position": {"x": 8.0, "y": 4.0},
            },
            "extend coal belt toward boiler without player coal shuttle",
        )

        with (
            patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_support),
            patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion_blocked),
            patch("factorio_ai.planner.CoalFuelFeedSkill.next_action", return_value=coal_feed),
        ):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertIn("repairing coal fuel feed first", decision.reason)
        self.assertNotEqual(decision.action.get("position"), {"x": 80.0, "y": 0.0})

    def test_iron_skill_prefers_expanded_smelting_power_over_direct_stone_support(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {"iron-plate": 4, "small-electric-pole": 1, "coal": 30}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
                entity["status"] = 54
                entity["inventories"] = {}
        direct_support = planner_module.PlannerDecision(
            {"type": "move_to", "position": {"x": -20.0, "y": -84.0}},
            "move near burner-mining-drill to fuel starter stone supply",
        )

        with patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_support):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotEqual(decision.action["position"], {"x": -20.0, "y": -84.0})
        self.assertIn("expanded iron-plate smelting input inserter", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "direct_iron_smelting_support_diverted")

    def test_iron_skill_routes_expansion_fuel_logistics_to_coal_feed_repair(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 4}
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        direct_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for direct burner-drill smelting cell",
        )
        expansion_blocked = planner_module.PlannerDecision(
            None,
            "expanded iron-plate smelting needs fuel logistics before more walking refuels",
        )
        coal_feed = planner_module.PlannerDecision(
            {
                "type": "build",
                "name": "transport-belt",
                "position": {"x": 8.0, "y": 4.0},
            },
            "extend coal belt toward boiler without player coal shuttle",
        )

        with (
            patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_blocked),
            patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion_blocked),
            patch("factorio_ai.planner.CoalFuelFeedSkill.next_action", return_value=coal_feed),
        ):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertIn("repairing coal fuel feed first", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "smelting_fuel_logistics")
        self.assertEqual(decision.metadata["repair_skill"], "connect_coal_fuel_feed")

    def test_expand_iron_smelting_refuels_reserve_from_existing_source_when_fuel_remains(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 14, "y": 0}}
        obs["inventory"] = {}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500)
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 980,
                "position": {"x": 14, "y": 0},
                "inventories": {"1": {"coal": 20}},
            }
        )

        decision = ExpandIronSmeltingSkill(target_rate_per_minute=90).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertIn("expanded iron-plate smelting reserve", decision.reason)
        self.assertNotIn("fuel logistics", decision.reason)

    def test_iron_skill_keeps_direct_blocked_failure_before_belt_automation(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 4}
        direct_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for direct burner-drill smelting cell",
        )

        with (
            patch("factorio_ai.planner._direct_plate_smelting_decision", return_value=direct_blocked),
            patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action") as expand,
        ):
            decision = IronPlateSkill(target_count=90).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertEqual(decision.reason, direct_blocked.reason)
        expand.assert_not_called()

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
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["count"], 8)

    def test_copper_skill_uses_belt_line_after_belt_automation_is_ready(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "coal": 8,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "inserter": 1,
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
            "inserter": 1,
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
            "inserter": 1,
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
        self.assertEqual(decision.action["name"], "inserter")
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

    def test_belt_smelting_skill_extends_incomplete_parallel_line_before_waiting(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 25, "y": 0}}
        obs["inventory"] = {"coal": 12, "stone-furnace": 1, "burner-mining-drill": 1, "transport-belt": 10}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 17},
            {"name": "copper-ore", "position": {"x": 20, "y": 0}, "distance": 5},
        ]
        obs["entities"] = complete_belt_smelting_entities(
            8,
            0,
            500,
            resource="copper-ore",
            product="copper-plate",
        )
        obs["entities"][-1]["inventories"] = {"1": {"coal": 3}}
        obs["entities"].extend(
            [
                {
                    "name": "transport-belt",
                    "unit_number": 600,
                    "position": {"x": 22, "y": 0},
                    "distance": 3,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 601,
                    "position": {"x": 23, "y": 0},
                    "distance": 2,
                    "inventories": {},
                },
                {
                    "name": "inserter",
                    "unit_number": 602,
                    "position": {"x": 24, "y": 0},
                    "distance": 1,
                    "direction": planner_module.EAST,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
            inventory_only=True,
        ).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 25, "y": 0})
        self.assertNotIn("wait for belt smelting line", decision.reason)

    def test_belt_smelting_skill_ignores_unknown_resource_line_for_copper(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 25, "y": 0}}
        obs["inventory"] = {
            "coal": 12,
            "stone-furnace": 1,
            "burner-mining-drill": 1,
            "inserter": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 20, "y": 0}, "distance": 5}]
        obs["entities"] = complete_belt_smelting_entities(
            8,
            0,
            500,
            resource="iron-ore",
            product="iron-plate",
        )
        obs["entities"][0].pop("mining_target")
        obs["entities"][0]["status_name"] = "no_minable_resources"
        obs["entities"][-1]["inventories"] = {"1": {"coal": 3}}

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
            inventory_only=True,
        ).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 22.0, "y": 0.0})
        self.assertNotIn("wait for belt smelting line", decision.reason)

    def test_belt_smelting_skill_does_not_treat_support_iron_done_as_copper_done(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 20, "y": 0}}
        obs["inventory"] = {
            "coal": 12,
            "iron-plate": 30,
            "iron-gear-wheel": 1,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 20, "y": 0}, "distance": 0}]
        obs["craftable"] = {}

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
            inventory_only=True,
        ).next_action(obs)

        self.assertFalse(decision.done)
        self.assertIsNone(decision.action)
        self.assertIn("cannot obtain inserter for belt smelting line yet", decision.reason)

    def test_belt_smelting_skill_takes_buffered_inserter_output_for_line_construction(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 20, "y": 0}}
        obs["inventory"] = {
            "coal": 12,
            "iron-plate": 30,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "transport-belt": 2,
        }
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 20, "y": 0}, "distance": 0}]
        obs["craftable"] = {"inserter": 0, "iron-gear-wheel": 0}
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 992,
                "position": {"x": 30.0, "y": 8.0},
                "distance": 12,
                "recipe": "inserter",
                "electric_network_connected": True,
                "inventories": {"3": {"inserter": 1}},
            }
        )

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
            inventory_only=True,
        ).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "inserter")
        self.assertEqual(decision.action["unit_number"], 992)
        self.assertIn("buffered inserter output", decision.reason)

    def test_belt_smelting_skill_relocates_existing_inserter_when_craft_needs_first_circuit(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 20.0, "y": 0.0}}
        obs["inventory"] = {
            "coal": 12,
            "iron-plate": 30,
            "iron-gear-wheel": 1,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "transport-belt": 2,
        }
        obs["craftable"] = {"inserter": 0}
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 20, "y": 0}, "distance": 0}]
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 991,
                "position": {"x": 20.0, "y": 3.0},
                "direction": planner_module.EAST,
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
            inventory_only=True,
        ).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 991)
        self.assertIn("relocate existing inserter for belt smelting line", decision.reason)

    def test_belt_smelting_skill_powers_unpowered_input_inserter_before_waiting(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {"small-electric-pole": 1}
        obs["entities"] = complete_belt_smelting_entities(
            4,
            0,
            500,
            resource="copper-ore",
            product="copper-plate",
        )
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
                entity["status"] = 54
                entity["inventories"] = {}

        decision = BeltSmeltingLineSkill(
            target_count=90,
            resource_name="copper-ore",
            product_name="copper-plate",
        ).next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotEqual(decision.action["type"], "wait")
        self.assertIn("copper-plate belt smelting input inserter", decision.reason)

    def test_copper_plate_skill_recovers_existing_direct_cell_before_belt_smelting(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {
            "coal": 12,
            "iron-plate": 30,
            "iron-gear-wheel": 1,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "transport-belt": 2,
        }
        obs["craftable"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "burner-mining-drill",
                    "unit_number": 960,
                    "position": {"x": 8.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "status_name": "no_fuel",
                    "status": 53,
                    "mining_target": "copper-ore",
                    "inventories": {},
                    "burner": {"remaining_burning_fuel": 0},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 961,
                    "position": {"x": 10.0, "y": 0.0},
                    "status_name": "no_ingredients",
                    "status": 18,
                    "inventories": {"1": {"coal": 4}},
                },
            ]
        )

        inventory_decision = CopperPlateSkill(target_count=5).next_action(obs, target_count=2, inventory_only=True)
        regular_decision = CopperPlateSkill(target_count=5).next_action(obs)

        for decision in (inventory_decision, regular_decision):
            self.assertEqual(decision.action["type"], "insert")
            self.assertEqual(decision.action["item"], "coal")
            self.assertEqual(decision.action["unit_number"], 960)
            self.assertIn("direct copper-plate smelting cell", decision.reason)

    def test_expand_iron_smelting_places_new_belt_when_below_capacity(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "inserter": 1,
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

    def test_expand_iron_smelting_powers_unpowered_line_inserter_before_waiting(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {"small-electric-pole": 1, "coal": 30}
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True)
        obs["entities"].append(mall_assembler(recipe="transport-belt"))
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
                entity["status"] = 54
                entity["inventories"] = {}

        decision = ExpandIronSmeltingSkill(target_rate_per_minute=90).next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotEqual(decision.action["type"], "wait")
        self.assertIn("expanded iron-plate smelting input inserter", decision.reason)

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
        self.assertEqual(decision.action["count"], 16)

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

    def test_expand_smelting_harvests_direct_cell_before_fuel_logistics_failure(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 22.0, "y": 0.0}}
        obs["inventory"] = {"coal": 0}
        obs["resources"] = [
            {"name": "copper-ore", "position": {"x": 4, "y": 0}, "distance": 18},
            {"name": "coal", "position": {"x": 300, "y": 0}, "distance": 278},
        ]
        obs["entities"] = complete_belt_smelting_entities(4, 0, 500, resource="copper-ore", product="copper-plate")
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["inventories"] = {}
            if entity["name"] == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 1}, "2": {"copper-ore": 1}}
        obs["entities"].extend(
            [
                {
                    "name": "burner-mining-drill",
                    "unit_number": 900,
                    "position": {"x": 20.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "mining_target": "copper-ore",
                    "status_name": "no_fuel",
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 901,
                    "position": {"x": 22.0, "y": 0.0},
                    "status_name": "full_output",
                    "inventories": {"1": {"coal": 1}, "2": {"copper-ore": 30}, "3": {"copper-plate": 80}},
                },
            ]
        )

        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-plate")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertTrue(decision.metadata["expanded_smelting_recovery"])
        self.assertIn("recover direct copper-plate cell", decision.reason)

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
        self.assertEqual(decision.action["count"], 30)

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
            "inserter": 1,
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
            "inserter": 1,
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
            "inserter": 1,
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
            "inserter": 1,
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
            "inserter": 1,
            "transport-belt": 2,
        }
        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 10.0, "y": 0.0})

    def test_expand_copper_smelting_places_regular_inserter_after_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "inserter": 1,
        }
        obs["resources"] = [{"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8}]
        layout = planner_module._select_belt_smelting_layout(obs, "copper-ore")
        self.assertIsNotNone(layout)
        obs["entities"].extend(
            [
                {
                    "name": "transport-belt",
                    "unit_number": 990,
                    "position": layout["belt1_position"],
                    "direction": layout["belt_direction"],
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 991,
                    "position": layout["belt2_position"],
                    "direction": layout["belt_direction"],
                    "inventories": {},
                },
            ]
        )

        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=37).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertNotEqual(decision.action["name"], "burner-inserter")

    def test_expand_smelting_regular_inserter_does_not_require_fuel(self):
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
        for entity in obs["entities"]:
            if entity["name"] == "burner-inserter":
                entity["name"] = "inserter"
                entity["electric_network_connected"] = True
                entity["inventories"] = {}

        decision = ExpandCopperSmeltingSkill(target_rate_per_minute=18).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

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
            "inserter": 1,
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

        # Self-calibrating: the boiler is placed so the game finds the tile that actually connects to
        # the pump (no hardcoded per-direction offset), so the action is place_fluid_connected with
        # the pump as the connection target rather than a fixed build position.
        self.assertEqual(decision.action["type"], "place_fluid_connected")
        self.assertEqual(decision.action["name"], "boiler")
        self.assertEqual(decision.action["target_position"], {"x": 10.5, "y": 10.5})

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
                "position": {"x": 12.5, "y": 11},
                "direction": 8,
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 14.5},
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
                "position": {"x": 12.5, "y": 11},
                "direction": 8,
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 14.5},
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
                "position": {"x": 12.5, "y": 11},
                "direction": 8,
                "status_name": "no_fuel",
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 14.5},
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
                "position": {"x": 12.5, "y": 11},
                "direction": 8,
                "status_name": "no_fuel",
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "status": 5,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 14.5},
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

    def test_build_item_mall_allows_one_time_boiler_seed_for_starter_belt_mall(self):
        obs = base_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 9.5}, "character_valid": False}
        obs["inventory"] = {}
        obs["resources"] = [{"name": "coal", "position": {"x": 0, "y": 9.5}, "distance": 0}]
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["entities"] = [
            {"name": "burner-mining-drill", "unit_number": 620, "position": {"x": 0, "y": 9.5}, "direction": 4, "inventories": {"1": {"coal": 3}}},
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
                "position": {"x": 12.5, "y": 11},
                "direction": 8,
                "status_name": "no_fuel",
                "distance": 0,
                "inventories": {},
                "fluids": {"1": {"name": "water", "amount": 200}},
            },
            {
                "name": "steam-engine",
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
                "position": {"x": 10.5, "y": 14.5},
                "direction": 0,
                "distance": 4,
                "inventories": {},
                "fluids": {},
            },
        ]

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 620)
        self.assertEqual(decision.action["count"], 2)
        self.assertTrue(decision.action["emergency_bootstrap"])
        self.assertIn("one-time emergency boiler bootstrap", decision.reason)

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
                "position": {"x": 12.5, "y": 11},
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
                "unit_number": 603,
                "position": {"x": 12.5, "y": 14.5},
                "direction": 8,
                "status": 1,
                "distance": 13,
                "inventories": {},
                "fluids": {"1": {"name": "steam", "amount": 80}},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 604,
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
                "position": {"x": 8.5, "y": 10},
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
                "unit_number": 613,
                "position": {"x": 8.5, "y": 6.5},
                "direction": 0,
                "status": 1,
                "distance": 13,
                "inventories": {},
                "fluids": {"1": {"name": "steam", "amount": 80}},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 614,
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

    def test_lab_site_prefers_science_pack_factory_over_power_plant_adjacency(self):
        obs = powered_research_observation()
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 710,
                "position": {"x": 50.0, "y": 50.0},
                "recipe": "automation-science-pack",
                "electric_network_connected": True,
                "inventories": {},
            }
        )
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "lab_position": {"x": 13.5, "y": 6.5},
                "distance": 3,
            },
            {
                "powered": True,
                "pole_unit_number": 605,
                "pole_position": {"x": 49.0, "y": 50.0},
                "lab_position": {"x": 52.0, "y": 50.0},
                "distance": 80,
            },
        ]

        selected = planner_module._select_lab_site(obs)

        self.assertEqual(selected["lab_position"], {"x": 52.0, "y": 50.0})

    def test_lab_site_leaves_power_clearance_when_science_pack_factory_is_not_built(self):
        obs = powered_research_observation()
        obs["lab_sites"] = [
            {
                "powered": True,
                "pole_unit_number": 604,
                "pole_position": {"x": 10.5, "y": 6.5},
                "lab_position": {"x": 13.5, "y": 6.5},
                "distance": 3,
            },
            {
                "powered": True,
                "pole_unit_number": 605,
                "pole_position": {"x": 21.5, "y": 6.5},
                "lab_position": {"x": 24.5, "y": 6.5},
                "distance": 14,
            },
        ]

        selected = planner_module._select_lab_site(obs)

        self.assertEqual(selected["lab_position"], {"x": 24.5, "y": 6.5})

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

    def test_circuit_automation_feeds_assembler_mall_before_handcrafting_assemblers(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 6,
            "iron-gear-wheel": 10,
            "iron-plate": 18,
            "inserter": 1,
        }
        obs["craftable"] = {"assembling-machine-1": 2}
        obs["entities"].append(mall_assembler(recipe="assembling-machine-1", inventory={}))

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertNotEqual(decision.action.get("type"), "craft")
        self.assertNotIn("take iron-plate", decision.reason)

    def test_circuit_automation_takes_buffered_assembler_before_feeding_mall(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 6,
            "iron-gear-wheel": 10,
            "iron-plate": 18,
            "inserter": 1,
        }
        obs["entities"].append(mall_assembler(recipe="assembling-machine-1", inventory={"iron-gear-wheel": 1}))
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 917,
                "position": {"x": 4.0, "y": 2.0},
                "distance": 4,
                "inventories": {"1": {"assembling-machine-1": 3}},
            }
        )

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "assembling-machine-1")
        self.assertEqual(decision.action["unit_number"], 917)
        self.assertIn("buffered assembling-machine-1", decision.reason)

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

    def test_circuit_automation_builds_sidecar_when_automation_sites_missing(self):
        obs = powered_automation_observation()
        obs["automation_sites"] = {}
        obs["inventory"] = {
            "small-electric-pole": 1,
            "assembling-machine-1": 2,
            "inserter": 1,
        }
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 819,
                "position": {"x": 8.5, "y": 6.5},
                "distance": 8,
                "recipe": "transport-belt",
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertIsNotNone(decision.action)
        self.assertNotIn("cannot find a powered or wireable site", decision.reason)
        self.assertEqual(decision.action["type"], "build")
        self.assertIn(decision.action["name"], {"small-electric-pole", "assembling-machine-1"})

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

    def test_circuit_automation_scaling_routes_manual_cable_seed_to_automated_prerequisite(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"copper-cable": 12}
        obs["entities"].extend(circuit_cell_entities(circuit_inventory={"iron-plate": 4}))
        decision = CircuitAutomationSkill(target_count=50).next_action(obs)
        self.assertIsNone(decision.action)
        self.assertIn("gear/belt mall logistics", decision.reason)

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
        self.assertIn("small electric poles", decision.reason)

    def test_circuit_automation_scaling_builds_site_input_line_for_missing_iron(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "transport-belt": 4}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 990,
                "position": {"x": -20, "y": 2},
                "recipe": "iron-plate",
                "inventories": {"3": {"iron-plate": 30}},
            }
        )

        site_input_decision = planner_module.PlannerDecision(
            {
                "type": "build",
                "name": "transport-belt",
                "position": {"x": 4.0, "y": 4.0},
                "direction": planner_module.EAST,
            },
            "extend iron-plate site input line for circuit automation",
        )
        with patch("factorio_ai.planner.SiteInputLogisticLineSkill.next_action", return_value=site_input_decision):
            decision = CircuitAutomationSkill(target_count=50).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertIn("iron-plate site input line", decision.reason)

    def test_circuit_automation_relocates_existing_inserter_when_first_circuit_blocks_crafting(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": -12.0, "y": 0.0}}
        obs["inventory"] = {
            "assembling-machine-1": 2,
            "small-electric-pole": 1,
            "iron-plate": 8,
            "iron-gear-wheel": 1,
            "copper-plate": 8,
            "copper-cable": 1,
        }
        obs["craftable"] = {"inserter": 0}
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 990,
                "position": {"x": -12.0, "y": 0.0},
                "direction": planner_module.EAST,
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = CircuitAutomationSkill().next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 990)
        self.assertIn("circuit automation bootstrap", decision.reason)

    def test_circuit_automation_does_not_relocate_active_boiler_feed_inserter(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 10.0, "y": 12.0}}
        obs["inventory"] = {"assembling-machine-1": 2}
        obs["craftable"] = {"inserter": 0}
        for entity in circuit_cell_entities():
            if entity["name"] != "inserter":
                obs["entities"].append(entity)
        obs["entities"].extend(
            [
                {
                    "name": "boiler",
                    "unit_number": 989,
                    "position": {"x": 10.0, "y": 10.0},
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 990,
                    "position": {"x": 10.0, "y": 12.0},
                    "direction": planner_module.SOUTH,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = CircuitAutomationSkill().next_action(obs)

        if decision.action is not None:
            self.assertNotEqual(decision.action.get("unit_number"), 990)
        self.assertNotIn("relocate existing inserter for circuit automation bootstrap", decision.reason)

    def test_circuit_automation_scaling_expands_iron_when_no_site_input_source_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "transport-belt": 4}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}))
        expansion_decision = planner_module.PlannerDecision(
            {
                "type": "build",
                "name": "stone-furnace",
                "position": {"x": 12.0, "y": 4.0},
            },
            "expand iron smelting before scaled circuit input routing",
        )

        with patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion_decision):
            decision = CircuitAutomationSkill(target_count=50).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertIn("expand iron smelting", decision.reason)

    def test_circuit_automation_scaling_repairs_coal_feed_when_iron_expansion_needs_fuel_logistics(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "transport-belt": 4}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}))
        expansion_blocked = planner_module.PlannerDecision(
            None,
            "expanded iron-plate smelting needs fuel logistics before more walking refuels",
        )
        coal_feed = planner_module.PlannerDecision(
            {
                "type": "build",
                "name": "transport-belt",
                "position": {"x": 8.0, "y": 4.0},
            },
            "extend coal belt toward boiler without player coal shuttle",
        )

        with (
            patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion_blocked),
            patch("factorio_ai.planner.CoalFuelFeedSkill.next_action", return_value=coal_feed),
        ):
            decision = CircuitAutomationSkill(target_count=50).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertIn("repairing coal fuel feed first", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "smelting_fuel_logistics")
        self.assertEqual(decision.metadata["repair_skill"], "connect_coal_fuel_feed")

    def test_circuit_automation_scaling_waits_on_active_iron_smelting_when_no_more_site_is_open(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(circuit_cell_entities(cable_inventory={"copper-cable": 40}))
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}))
        obs["entities"].extend(complete_belt_smelting_entities(4, 0, 500, reserve_fuel=True))
        expansion_blocked = planner_module.PlannerDecision(
            None,
            "cannot find open iron-ore site for another smelting line",
        )

        with patch("factorio_ai.planner.ExpandIronSmeltingSkill.next_action", return_value=expansion_blocked):
            decision = CircuitAutomationSkill(target_count=50).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("wait for existing expanded iron-plate smelting output", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "scaled_circuit_input_smelting_site_blocked")
        self.assertEqual(decision.metadata["repair_skill"], "expand_iron_smelting")

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

    def test_build_item_mall_allows_one_time_gears_for_first_assembler_bootstrap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-plate": 19,
        }
        obs["craftable"] = {"iron-gear-wheel": 5}

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertEqual(decision.action["count"], 5)
        self.assertIs(decision.action["allow_first_assembler_bootstrap"], True)
        self.assertIn("first assembling-machine-1 bootstrap", decision.reason)

    def test_build_item_mall_does_not_allow_first_assembler_gear_bootstrap_when_assembler_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-plate": 19,
        }
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"copper-plate": 4}))

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertNotEqual(decision.action and decision.action.get("type"), "craft")
        self.assertFalse(decision.action and decision.action.get("allow_first_assembler_bootstrap"))

    def test_build_item_mall_takes_assembler_made_gears_for_next_assembler_bootstrap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-plate": 9,
            "small-electric-pole": 1,
        }
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 5}))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["count"], 5)
        self.assertIn("assembler-produced gears", decision.reason)

    def test_build_item_mall_takes_output_chest_gears_for_next_assembler_bootstrap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-plate": 9,
            "small-electric-pole": 1,
        }
        assembler = mall_assembler(recipe="iron-gear-wheel", inventory={})
        obs["entities"].append(assembler)
        output_layout = planner_module._build_item_mall_output_layout(obs, assembler["position"])
        self.assertIsNotNone(output_layout)
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 990,
                "position": output_layout["output_chest_position"],
                "inventories": {"1": {"iron-gear-wheel": 69, "transport-belt": 10}},
            }
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["count"], 5)
        self.assertEqual(decision.action["unit_number"], 990)
        self.assertIn("chest-buffered assembler gears", decision.reason)

    def test_build_item_mall_retools_existing_assembler_for_next_assembler_bootstrap_gears(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-plate": 19,
            "small-electric-pole": 1,
        }
        obs["entities"].append(mall_assembler(recipe="automation-science-pack"))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")

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

    def test_build_item_mall_recovers_plate_directly_when_belt_bootstrap_has_no_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        assembler = mall_assembler(recipe="transport-belt", inventory={"iron-gear-wheel": 10})
        furnace = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 5.0, "y": 0.0},
            "recipe": "iron-plate",
            "inventories": {"3": {"iron-plate": 8}},
        }
        obs["entities"].extend([assembler, furnace])
        layout = {
            "item": "iron-plate",
            "source": furnace,
            "consumer": assembler,
            "segments": [{"position": {"x": 4.0, "y": 1.0}, "direction": planner_module.EAST, "entity": None}],
            "source_inserter": {"position": {"x": 4.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
            "target_inserter": {"position": {"x": 3.0, "y": 2.0}, "direction": planner_module.EAST, "entity": None},
        }

        with (
            patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout),
            patch(
                "factorio_ai.planner.SiteInputLogisticLineSkill.next_action",
                side_effect=AssertionError("site-input should not run without belts"),
            ),
        ):
            decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("furnace output", decision.reason)

    def test_build_item_mall_bootstraps_gear_mall_instead_of_gear_site_input_when_no_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        belt_assembler = mall_assembler(recipe="transport-belt", inventory={})
        gear_assembler = {
            "name": "assembling-machine-1",
            "unit_number": 902,
            "position": {"x": 6.0, "y": 2.0},
            "distance": 6,
            "recipe": "iron-gear-wheel",
            "electric_network_connected": True,
            "inventories": {"1": {}},
        }
        furnace = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 5.0, "y": 0.0},
            "recipe": "iron-plate",
            "inventories": {"3": {"iron-plate": 8}},
        }
        obs["entities"].extend([belt_assembler, gear_assembler, furnace])
        layout = {
            "item": "iron-gear-wheel",
            "source": gear_assembler,
            "consumer": belt_assembler,
            "segments": [{"position": {"x": 4.0, "y": 2.0}, "direction": planner_module.EAST, "entity": None}],
            "source_inserter": {"position": {"x": 5.0, "y": 2.0}, "direction": planner_module.WEST, "entity": None},
            "target_inserter": {"position": {"x": 3.0, "y": 2.0}, "direction": planner_module.EAST, "entity": None},
        }

        with (
            patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout),
            patch(
                "factorio_ai.planner.SiteInputLogisticLineSkill.next_action",
                side_effect=AssertionError("gear site-input should not run without belts"),
            ),
        ):
            decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("furnace output", decision.reason)

    def test_build_item_mall_bridges_disconnected_power_corridor_gap(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 35.5, "y": 0.5}}
        obs["inventory"] = {"small-electric-pole": 1, "iron-plate": 10, "iron-gear-wheel": 10}
        obs["entities"].extend(
            [
                {
                    "name": "small-electric-pole",
                    "unit_number": 980,
                    "position": {"x": 30.5, "y": 0.5},
                    "electric_network_connected": True,
                    "electric_network_id": 1,
                    "inventories": {},
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": 981,
                    "position": {"x": 40.5, "y": 0.5},
                    "electric_network_connected": False,
                    "electric_network_id": 3,
                    "inventories": {},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 982,
                    "position": {"x": 42.5, "y": 0.5},
                    "distance": 42,
                    "recipe": "transport-belt",
                    "electric_network_connected": False,
                    "electric_network_id": 3,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertNotEqual(decision.action.get("type"), "connect_power")
        self.assertIn("bridge disconnected transport-belt mall power corridor", decision.reason)

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

    def test_build_item_mall_places_belt_sidecar_from_existing_gear_cell_without_planning_sites(self):
        obs = powered_automation_observation()
        obs["automation_sites"] = []
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={}))
        obs["entities"].append(
            {
                "name": "small-electric-pole",
                "unit_number": 990,
                "position": {"x": 4.0, "y": 0.0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 2.0})

    def test_build_item_mall_uses_vertical_belt_sidecar_when_horizontal_slots_are_blocked(self):
        obs = powered_automation_observation()
        obs["automation_sites"] = []
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="iron-gear-wheel", inventory={}),
                {
                    "name": "wooden-chest",
                    "unit_number": 990,
                    "position": {"x": 5.0, "y": 2.0},
                    "inventories": {"1": {"iron-gear-wheel": 20}},
                },
                {
                    "name": "lab",
                    "unit_number": 991,
                    "position": {"x": -1.0, "y": 3.0},
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": 992,
                    "position": {"x": 4.0, "y": 4.0},
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 6.0})

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
                    "position": {"x": 5, "y": 0},
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

    def test_power_pole_mall_takes_remote_copper_output_before_stone_bootstrap_wait(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}}
        obs["inventory"] = {"stone": 1}
        obs["craftable"] = {}
        obs["resources"] = [
            {"name": "stone", "position": {"x": -18, "y": -84}, "distance": 90},
            {"name": "copper-ore", "position": {"x": 72, "y": -60}, "distance": 94},
        ]
        obs["entities"].extend(
            [
                mall_assembler(recipe="small-electric-pole"),
                {
                    "name": "stone-furnace",
                    "unit_number": 932,
                    "position": {"x": 74, "y": -60},
                    "distance": 95,
                    "recipe": "copper-plate",
                    "status_name": "no_fuel",
                    "inventories": {"3": {"copper-plate": 57}},
                },
                {
                    "name": "burner-mining-drill",
                    "unit_number": 933,
                    "position": {"x": -18, "y": -84},
                    "direction": 4,
                    "mining_target": "stone",
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 934,
                    "position": {"x": -17, "y": -84},
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("small-electric-pole", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 74.0, "y": -60.0})
        self.assertIn("copper-plate furnace output", decision.reason)
        self.assertNotIn("starter stone drill", decision.reason)

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

    def test_build_item_mall_routes_gear_input_to_distant_science_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 6, "inserter": 2}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 930,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 931,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {"copper-plate": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 932,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
            ]
        )

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertNotEqual(decision.action["type"], "wait")
        self.assertIn("site input", decision.reason)

    def test_gear_mall_output_routes_to_referenced_science_consumer(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 6, "inserter": 2}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 940,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 941,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {"copper-plate": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 942,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
            ]
        )

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(
            obs,
            reference_position={"x": 8.0, "y": 0.0},
        )

        self.assertNotEqual(decision.action["type"], "wait")
        self.assertIn("site input", decision.reason)

    def test_site_input_logistics_uses_underground_bridge_for_crossing_belt_line(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4.0, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2, "underground-belt": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        obs["recipe_unlocks"] = {"underground-belt": {"enabled": True}}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 943,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 944,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 945,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 946,
                    "position": {"x": 4.5, "y": 0.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "underground-belt")
        self.assertEqual(decision.action["position"], {"x": 3.5, "y": 0.5})
        self.assertEqual(decision.action["direction"], planner_module.EAST)
        self.assertEqual(decision.action["underground_type"], "input")
        self.assertIn("crosses another belt line", decision.reason)

    def test_site_input_logistics_places_splitter_for_one_source_two_consumers_after_logistics(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": -4.0, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 12, "inserter": 4, "splitter": 1}
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        obs["recipe_unlocks"] = {"splitter": {"enabled": True}}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 960,
                    "position": {"x": -8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 8}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 961,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 962,
                    "position": {"x": 8.0, "y": 8.0},
                    "distance": 12,
                    "recipe": "inserter",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 963,
                    "position": {"x": -8.0, "y": 12.0},
                    "distance": 12,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertEqual(layout["fanout_consumer_count"], 2)
        self.assertIsNotNone(layout["splitter"])
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "splitter")
        self.assertIn("fan out", decision.reason)

    def test_site_input_logistics_crafts_splitter_before_second_fanout_branch(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": -4.0, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 12, "inserter": 4}
        obs["craftable"] = {"splitter": 1}
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        obs["recipe_unlocks"] = {"splitter": {"enabled": True}}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 960,
                    "position": {"x": -8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 8}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 961,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 962,
                    "position": {"x": 8.0, "y": 8.0},
                    "distance": 12,
                    "recipe": "inserter",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 963,
                    "position": {"x": -8.0, "y": 12.0},
                    "distance": 12,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "splitter")
        self.assertIn("branching one site input source", decision.reason)

    def test_site_input_logistics_detours_around_crossing_belt_before_logistics_research(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 3.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": False}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 956,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 957,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 958,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 959,
                    "position": {"x": 4.5, "y": 0.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertFalse(
            any(
                segment["position"] == {"x": 4.5, "y": 0.5}
                for segment in layout["segments"]
            )
        )
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotEqual(decision.action["position"], {"x": 4.5, "y": 0.5})
        self.assertNotIn("underground-belt bridge after logistics research", decision.reason)

    def test_site_input_logistics_detours_same_column_crossing_before_logistics_research(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 3.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": False}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 960,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 961,
                    "position": {"x": 6.0, "y": -4.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 962,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 963,
                    "position": {"x": 3.5, "y": -2.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertFalse(
            any(
                segment["position"] == {"x": 3.5, "y": -2.5}
                for segment in layout["segments"]
            )
        )
        self.assertTrue(any(segment["position"]["x"] != 3.5 for segment in layout["segments"]))
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotEqual(decision.action["position"], {"x": 3.5, "y": -2.5})

    def test_site_input_logistics_detour_does_not_mine_existing_assembler_blocker(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 3.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": False}
        blocker = {
            "name": "assembling-machine-1",
            "unit_number": 964,
            "position": {"x": 4.5, "y": -1.5},
            "distance": 5,
            "recipe": "small-electric-pole",
            "electric_network_connected": True,
            "inventories": {},
        }
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 965,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 966,
                    "position": {"x": 6.0, "y": -4.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 967,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 968,
                    "position": {"x": 3.5, "y": -2.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
                blocker,
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertFalse(
            any(planner_module._point_inside_machine(segment["position"], blocker) for segment in layout["segments"])
        )
        self.assertNotEqual(decision.action and decision.action.get("unit_number"), 964)

    def test_site_input_logistics_detours_instead_of_mining_power_pole(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 3.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": False}
        blocker = {
            "name": "small-electric-pole",
            "unit_number": 969,
            "position": {"x": 4.5, "y": -1.5},
            "distance": 5,
            "electric_network_connected": True,
            "inventories": {},
        }
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 970,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 971,
                    "position": {"x": 6.0, "y": -4.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 972,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                blocker,
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertFalse(
            any(planner_module.distance(segment["position"], blocker["position"]) < 0.45 for segment in layout["segments"])
        )
        self.assertNotEqual(decision.action and decision.action.get("type"), "mine")
        self.assertNotEqual(decision.action and decision.action.get("unit_number"), 969)

    def test_site_input_logistics_relocates_power_pole_when_no_clear_route_remains(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 2.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 2}
        source = {
            "name": "wooden-chest",
            "unit_number": 970,
            "position": {"x": 0.5, "y": 0.5},
            "inventories": {"1": {"iron-gear-wheel": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 971,
            "position": {"x": 8.0, "y": 0.5},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        blocker = {
            "name": "small-electric-pole",
            "unit_number": 972,
            "position": {"x": 2.5, "y": 0.5},
            "electric_network_connected": True,
            "inventories": {},
        }
        layout = {
            "item": "iron-gear-wheel",
            "source": source,
            "consumer": consumer,
            "consumer_recipe": "automation-science-pack",
            "source_inserter": {"position": {"x": 1.5, "y": 0.5}, "direction": planner_module.WEST, "entity": None},
            "target_inserter": {"position": {"x": 7.5, "y": 0.5}, "direction": planner_module.EAST, "entity": None},
            "segments": [{"position": {"x": 2.5, "y": 0.5}, "direction": planner_module.EAST, "entity": None}],
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, blocker])

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 972)
        self.assertIn("blocking small-electric-pole", decision.reason)

    def test_site_input_logistics_detour_avoids_opposite_direction_existing_belt(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 3.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": False}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 969,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 970,
                    "position": {"x": 6.0, "y": -4.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 971,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 972,
                    "position": {"x": 3.5, "y": -2.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 973,
                    "position": {"x": 4.5, "y": -2.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertIsNotNone(layout)
        self.assertFalse(any(segment["position"] == {"x": 4.5, "y": -2.5} for segment in layout["segments"]))
        self.assertNotEqual(decision.action and decision.action.get("unit_number"), 973)

    def test_site_input_logistics_finishes_underground_bridge_output(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 5.0, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2, "underground-belt": 1}
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        obs["recipe_unlocks"] = {"underground-belt": {"enabled": True}}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 947,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 948,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 949,
                    "position": {"x": 0.0, "y": 8.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "underground-belt",
                    "unit_number": 950,
                    "position": {"x": 3.5, "y": 0.5},
                    "direction": planner_module.EAST,
                    "belt_to_ground_type": "input",
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 951,
                    "position": {"x": 4.5, "y": 0.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "underground-belt")
        self.assertEqual(decision.action["position"], {"x": 5.5, "y": 0.5})
        self.assertEqual(decision.action["underground_type"], "output")

    def test_site_input_underground_bridge_preserves_north_direction_zero(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.5, "y": -3.5}}
        obs["inventory"] = {"transport-belt": 10, "inserter": 2, "underground-belt": 2}
        obs["research"]["technologies"]["logistics"] = {"researched": True}
        obs["recipe_unlocks"] = {"underground-belt": {"enabled": True}}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 0.0, "y": 0.0},
                    "distance": 4,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"3": {"iron-gear-wheel": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 953,
                    "position": {"x": 0.0, "y": -8.0},
                    "distance": 8,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {"2": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 954,
                    "position": {"x": 8.0, "y": 0.0},
                    "distance": 8,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 955,
                    "position": {"x": 0.5, "y": -3.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
            ]
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "underground-belt")
        self.assertEqual(decision.action["direction"], planner_module.NORTH)
        self.assertEqual(decision.action["underground_type"], "input")

    def test_build_item_mall_buffers_gear_output_after_automation(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"wooden-chest": 1}
        obs["craftable"] = {"iron-gear-wheel": 5}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 4}))

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "wooden-chest")
        self.assertIn("output chest for iron-gear-wheel mall", decision.reason)

    def test_build_item_mall_gear_target_ignores_non_output_gear_stock(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10}
        obs["entities"].extend(gear_belt_mall_entities(gear_inventory={}, belt_inventory={"iron-gear-wheel": 20}))
        layout = planner_module._build_item_mall_output_layout(obs, {"x": 2, "y": 2})
        self.assertIsNotNone(layout)
        obs["entities"].extend(
            [
                {
                    "name": "wooden-chest",
                    "unit_number": 990,
                    "position": layout["output_chest_position"],
                    "inventories": {},
                },
                {
                    "name": "inserter",
                    "unit_number": 991,
                    "position": layout["output_inserter_position"],
                    "direction": layout["output_inserter_direction"],
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 910)
        self.assertIn("insert iron-plate into iron-gear-wheel mall assembler", decision.reason)

    def test_build_item_mall_recovers_empty_gear_site_input_source_before_waiting(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 10, "transport-belt": 10}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 910,
                    "position": {"x": 2, "y": 2},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 990,
                    "position": {"x": 8, "y": 2},
                    "recipe": "inserter",
                    "electric_network_connected": True,
                    "inventories": {"2": {"electronic-circuit": 4, "iron-plate": 3}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 989,
                    "position": {"x": 12, "y": 2},
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"3": {"transport-belt": 8}},
                },
            ]
        )
        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-gear-wheel")
        self.assertIsNotNone(layout)
        unit_number = 991
        for segment in layout["segments"]:
            belt = {
                "name": "transport-belt",
                "unit_number": unit_number,
                "position": segment["position"],
                "direction": segment["direction"],
                "inventories": {},
            }
            segment["entity"] = belt
            obs["entities"].append(belt)
            unit_number += 1
        source_inserter = {
            "name": "inserter",
            "unit_number": unit_number,
            "position": layout["source_inserter"]["position"],
            "direction": layout["source_inserter"]["direction"],
            "electric_network_connected": True,
            "inventories": {},
        }
        target_inserter = {
            "name": "inserter",
            "unit_number": unit_number + 1,
            "position": layout["target_inserter"]["position"],
            "direction": layout["target_inserter"]["direction"],
            "electric_network_connected": True,
            "inventories": {},
        }
        layout["source_inserter"]["entity"] = source_inserter
        layout["target_inserter"]["entity"] = target_inserter
        obs["entities"].extend(
            [
                source_inserter,
                target_inserter,
            ]
        )

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = BuildItemMallSkill("inserter", 4).next_action(obs, reference_position=layout["consumer"]["position"])

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 910)
        self.assertIn("before waiting for iron gear wheel site input logistics", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "site_input_source_starved")
        self.assertEqual(decision.metadata["repair_skill"], "bootstrap_build_item_mall")
        self.assertEqual(decision.metadata["target_item"], "iron-gear-wheel")

    def test_build_item_mall_completes_first_assembler_before_more_gear_seed(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 2, "y": 2}, "character_valid": False}
        obs["inventory"] = {"iron-plate": 18}
        obs["entities"].append(
            mall_assembler(
                recipe="assembling-machine-1",
                inventory={"electronic-circuit": 6, "iron-gear-wheel": 9},
            )
        )

        decision = BuildItemMallSkill("assembling-machine-1", 2).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["count"], 9)
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.action["seed_reason"], "assembling-machine-1_mall_first_craft_plate_seed")
        self.assertNotIn("gear seed", decision.reason)

    def test_build_item_mall_does_not_count_placed_assemblers_as_mall_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "electronic-circuit": 3,
            "iron-gear-wheel": 5,
            "iron-plate": 9,
        }
        obs["entities"].append(mall_assembler(recipe="assembling-machine-1", inventory={}))
        obs["entities"].extend(circuit_cell_entities())

        decision = BuildItemMallSkill("assembling-machine-1", 2).next_action(obs)

        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["unit_number"], 901)
        self.assertNotIn("target reached", decision.reason)

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

    def test_build_item_mall_virtual_agent_handcarries_when_belt_line_unbuildable(self):
        # Live deadlock regression (2026-06-19, autopilot cycle 130-133): the virtual RCON agent
        # (character_valid=False, instant move) must NOT refuse the far iron-plate hand-carry when it
        # has too few transport-belts to build the line. Refusing starves the belt mall -> 0 belts ->
        # the boiler coal feed refuses hand-crafted belts -> power stalls -> the whole factory
        # deadlocks. Hand-carry is free for the virtual agent, so it seeds the mall instead.
        obs = powered_automation_observation()
        obs["inventory"] = {}  # 0 spare belts -> a 120-tile belt line is unbuildable
        obs["player"] = {"position": {"x": 0, "y": 0}, "character_valid": False}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel"))
        obs["entities"].append({
            "name": "stone-furnace", "unit_number": 980, "position": {"x": 120, "y": 0},
            "recipe": "iron-plate", "inventories": {"3": {"iron-plate": 20}},
        })

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(obs)

        self.assertNotIn("refusing repeated hand-carry", decision.reason or "")

    def test_manual_site_input_blocker_threshold_for_virtual_agent(self):
        # Directly exercise the threshold: virtual agent + far source. Too few belts -> allow
        # (return None, no deadlock); enough belts to span the gap -> defer to the belt line (refuse).
        from factorio_ai.planner import _manual_site_input_logistics_blocker

        def _obs(belt_count):
            obs = powered_automation_observation()
            obs["player"] = {"position": {"x": 120, "y": 0}, "character_valid": False}
            obs["inventory"] = {"transport-belt": belt_count} if belt_count else {}
            obs["entities"].append({
                "name": "stone-furnace", "unit_number": 980, "position": {"x": 0, "y": 0},
                "recipe": "iron-plate", "inventories": {"3": {"iron-plate": 20}},
            })
            return obs

        consumer = {"x": 120, "y": 0}
        few = _manual_site_input_logistics_blocker(_obs(0), "iron-plate", consumer, consumer_label="belt mall")
        self.assertIsNone(few)  # can't build a 120-tile line with 0 belts -> hand-carry, no deadlock
        plenty = _manual_site_input_logistics_blocker(_obs(400), "iron-plate", consumer, consumer_label="belt mall")
        self.assertIsNotNone(plenty)  # 400 belts span the gap -> prefer the line
        self.assertIn("refusing repeated hand-carry", plenty.reason)

    def test_build_item_mall_places_output_chest_for_user_consumed_item(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"wooden-chest": 1, "inserter": 1}
        obs["entities"].append(mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "wooden-chest")
        self.assertEqual(decision.action["position"], {"x": 5.0, "y": 2.0})
        self.assertIn("output chest", decision.reason)

    def test_build_item_mall_places_output_inserter_to_chest_for_user_consumed_item(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 4.0, "y": 2.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_build_item_mall_removes_output_inserter_pointing_from_chest_to_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 4,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 981)
        self.assertIn("misoriented", decision.reason)

    def test_build_item_mall_crafts_regular_output_inserter_instead_of_using_burner_when_available(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1, "iron-plate": 1, "iron-gear-wheel": 1, "electronic-circuit": 1}
        obs["craftable"] = {"burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "inserter")

    def test_build_item_mall_does_not_use_burner_output_inserter_fallback(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1}

        self.assertIsNone(planner_module._available_build_item_mall_output_inserter_name(obs))
        self.assertEqual(planner_module._build_item_mall_missing_output_inserter_item(obs), "inserter")

    def test_build_item_mall_takes_assembler_gears_for_output_inserter_infrastructure(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 1, "electronic-circuit": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 981,
                    "position": {"x": 0.5, "y": 6.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-gear-wheel": 1}},
                },
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["count"], 1)
        self.assertIn("infrastructure", decision.reason)

    def test_build_item_mall_takes_assembler_gears_for_local_input_bootstrap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-gear-wheel": 3}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 981,
                    "position": {"x": 0.5, "y": 6.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-gear-wheel": 4}},
                },
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
                {
                    "name": "inserter",
                    "unit_number": 982,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertIn("transport-belt mall input bootstrap", decision.reason)

    def test_build_item_mall_takes_chest_buffered_gears_for_transport_belt_input_bootstrap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 981,
                    "position": {"x": 0.5, "y": 6.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-plate": 1}},
                },
                {
                    "name": "inserter",
                    "unit_number": 982,
                    "position": {"x": 2.5, "y": 6.5},
                    "direction": 4,
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 983,
                    "position": {"x": 3.5, "y": 6.5},
                    "inventories": {"1": {"iron-gear-wheel": 4}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 983)
        self.assertIn("chest-buffered assembler gears", decision.reason)

    def test_build_item_mall_does_not_take_gears_back_from_target_belt_assembler(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-gear-wheel": 2}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 981,
                    "position": {"x": 0.5, "y": 6.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-plate": 4}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertFalse(
            decision.action
            and decision.action.get("type") == "take"
            and decision.action.get("item") == "iron-gear-wheel"
            and decision.action.get("unit_number") == 901,
            decision.reason,
        )
        self.assertNotIn("chest-buffered assembler gears", decision.reason)

    def test_build_item_mall_finishes_started_iron_site_input_before_repeating_plate_seed(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4.0, "y": 6.0}, "character_valid": False}
        obs["inventory"] = {"iron-plate": 1, "inserter": 1, "transport-belt": 4}
        source = {
            "name": "wooden-chest",
            "unit_number": 980,
            "position": {"x": 0.0, "y": 6.0},
            "inventories": {"1": {"iron-plate": 20}},
        }
        gear_assembler = {
            "name": "assembling-machine-1",
            "unit_number": 981,
            "position": {"x": 8.0, "y": 6.0},
            "recipe": "iron-gear-wheel",
            "electric_network_connected": True,
            "inventories": {},
            "status_name": "item_ingredient_shortage",
        }
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-gear-wheel": 1}),
                source,
                gear_assembler,
                {
                    "name": "transport-belt",
                    "unit_number": 983,
                    "position": {"x": 2.0, "y": 6.0},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
            ]
        )
        layout = {
            "item": "iron-plate",
            "source": source,
            "consumer": gear_assembler,
            "source_inserter": {
                "position": {"x": 1.0, "y": 6.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 6.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 6.0},
                    "direction": planner_module.EAST,
                    "entity": obs["entities"][-1],
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 1.0, "y": 6.0})
        self.assertNotEqual(decision.action.get("bootstrap_seed"), True)
        self.assertIn("site source output inserter", decision.reason)

    def test_build_item_mall_finishes_started_gear_mall_iron_line_before_repeating_plate_seed(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4.0, "y": 0.0}, "character_valid": False}
        obs["inventory"] = {"iron-plate": 1, "transport-belt": 1}
        source = {
            "name": "wooden-chest",
            "unit_number": 980,
            "position": {"x": 0.0, "y": 0.0},
            "inventories": {"1": {"iron-plate": 20}},
        }
        gear_assembler = {
            "name": "assembling-machine-1",
            "unit_number": 981,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "iron-gear-wheel",
            "electric_network_connected": True,
            "inventories": {},
            "status_name": "item_ingredient_shortage",
        }
        built_segment = {
            "name": "transport-belt",
            "unit_number": 982,
            "position": {"x": 2.0, "y": 0.0},
            "direction": planner_module.EAST,
            "inventories": {},
        }
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-gear-wheel": 1}),
                source,
                gear_assembler,
                built_segment,
            ]
        )
        layout = {
            "source": source,
            "gear_assembler": gear_assembler,
            "belt_assembler": obs["entities"][-4],
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "target_inserter": {
                "position": {"x": 7.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": built_segment,
                },
                {
                    "position": {"x": 3.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": None,
                },
            ],
        }

        with patch("factorio_ai.planner._find_iron_plate_logistic_line_to_gear_mall_layout", return_value=layout):
            decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 3.0, "y": 0.0})
        self.assertNotEqual(decision.action.get("bootstrap_seed"), True)
        self.assertIn("iron-plate belt logistics", decision.reason)

    def test_build_item_mall_does_not_count_remote_or_consumer_gears_as_local_prerequisite(self):
        obs = base_observation()
        obs["inventory"] = {}
        obs["player"]["position"] = {"x": 56.5, "y": 58.5}
        obs["player"]["character_valid"] = False
        obs["execution"] = {"virtual": True}
        obs["research"]["technologies"]["automation"]["researched"] = True
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 217,
                    "position": {"x": 56.5, "y": 58.5},
                    "distance": 2,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-gear-wheel": 2}},
                    "status_name": "item_ingredient_shortage",
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 214,
                    "position": {"x": 52.5, "y": 58.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-plate": 1}},
                    "status_name": "item_ingredient_shortage",
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 15,
                    "position": {"x": 47, "y": 63},
                    "distance": 3,
                    "recipe": "iron-plate",
                    "inventories": {"1": {}, "2": {"iron-plate": 84}},
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 124,
                    "position": {"x": -13.5, "y": -62.5},
                    "distance": 140,
                    "inventories": {"1": {"iron-gear-wheel": 14}},
                },
            ]
        )

        decision = BuildItemMallSkill("iron-gear-wheel", 4).next_action(
            obs,
            allow_existing_remote=False,
            reference_position={"x": 56.5, "y": 58.5},
        )

        self.assertFalse(decision.done)
        self.assertNotIn("target reached: 16/4", decision.reason)

    def test_build_item_mall_replaces_output_burner_inserter_when_regular_is_usable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
                {
                    "name": "burner-inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "inventories": {"1": {"coal": 1}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 981)
        self.assertIn("replace", decision.reason)

    def test_build_item_mall_waits_for_output_buffer_instead_of_taking_user_consumed_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 20}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("buffer items into chest", decision.reason)

    def test_build_item_mall_takes_non_user_consumed_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].append(mall_assembler(recipe="automation-science-pack", inventory={"automation-science-pack": 20}))
        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "automation-science-pack")

    def test_build_item_mall_done_when_running_and_target_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"iron-plate": 4, "iron-gear-wheel": 4}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {"1": {"transport-belt": 20}}},
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )
        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_build_item_mall_removes_obsolete_empty_buffer_chest(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 2.0, "y": 5.0}}
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 20}),
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {"1": {"transport-belt": 20}}},
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {"name": "wooden-chest", "unit_number": 990, "position": {"x": 2.0, "y": 5.0}, "inventories": {}},
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 990)
        self.assertIn("obsolete empty", decision.reason)

    def test_build_item_mall_preserves_neighbor_output_chest_during_science_cleanup(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 5.0, "y": 2.0}}
        obs["inventory"] = {"copper-plate": 4}
        obs["entities"].extend(
            [
                mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 4}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 902,
                    "position": {"x": 0.5, "y": 3.5},
                    "distance": 4,
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {"name": "wooden-chest", "unit_number": 980, "position": {"x": 5.0, "y": 2.0}, "inventories": {}},
            ]
        )

        decision = BuildItemMallSkill("automation-science-pack", 20).next_action(obs)

        self.assertFalse(
            decision.action
            and decision.action.get("type") == "mine"
            and decision.action.get("unit_number") == 980,
            decision.reason,
        )

    def test_build_item_mall_places_transport_belt_assembler_with_direct_inserter_gap(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"assembling-machine-1": 1}
        obs["entities"].append(mall_assembler(recipe="iron-gear-wheel", inventory={}))

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "assembling-machine-1")
        self.assertEqual(decision.action["position"], {"x": 6.0, "y": 2.0})

    def test_build_item_mall_recovers_cramped_transport_belt_assembler_next_to_gear_mall(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 5, "y": 2}}
        obs["entities"].extend(
            [
                mall_assembler(recipe="iron-gear-wheel", inventory={}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 902,
                    "position": {"x": 5, "y": 2},
                    "distance": 5,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"1": {"iron-gear-wheel": 3}},
                },
            ]
        )

        decision = BuildItemMallSkill("transport-belt", 20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 902)
        self.assertIn("direct inserter gap", decision.reason)

    def test_gear_belt_mall_sets_reusable_assembler_to_transport_belt(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 3, "burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(gear_belt_mall_entities())

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "set_recipe")
        self.assertEqual(decision.action["recipe"], "transport-belt")
        self.assertEqual(decision.action["unit_number"], 911)

    def test_gear_belt_mall_removes_obsolete_empty_buffer_chest(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 2.0, "y": 5.0}}
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
                belt_inventory={"transport-belt": 20},
            )
        )
        obs["entities"].extend(
            [
                {
                    "name": "inserter",
                    "unit_number": 912,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 4,
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {"name": "wooden-chest", "unit_number": 990, "position": {"x": 2.0, "y": 5.0}, "inventories": {}},
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 990)
        self.assertIn("obsolete empty", decision.reason)

    def test_gear_belt_mall_preserves_nonempty_buffer_chest(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 2.0, "y": 5.0}}
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
                belt_inventory={"transport-belt": 20},
            )
        )
        obs["entities"].extend(
            [
                {
                    "name": "inserter",
                    "unit_number": 912,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 4,
                    "electric_network_connected": True,
                    "inventories": {},
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 990,
                    "position": {"x": 2.0, "y": 5.0},
                    "inventories": {"1": {"iron-gear-wheel": 2}},
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertFalse(
            decision.action
            and decision.action.get("type") == "mine"
            and decision.action.get("unit_number") == 990,
            decision.reason,
        )

    def test_gear_belt_mall_builds_short_belt_lane_without_taking_gears_when_direct_slot_is_blocked(self):
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
                "name": "small-electric-pole",
                "unit_number": 929,
                "position": {"x": 4.0, "y": 2.0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotEqual(decision.action.get("type"), "take")

    def test_gear_belt_mall_prefers_direct_assembler_transfer_even_when_belts_are_available(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 3, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 4.0, "y": 2.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_gear_belt_mall_prefers_direct_transfer_over_existing_belt_lane(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 4.0, "y": 2.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_gear_belt_mall_crafts_bootstrap_gear_for_direct_transfer_when_no_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "electronic-circuit": 1}
        obs["craftable"] = {"iron-gear-wheel": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "iron-gear-wheel")
        self.assertIn("direct gear-to-belt transfer inserter", decision.reason)
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "direct gear-to-belt transfer inserter_gear_seed")

    def test_gear_belt_mall_repairs_missing_direct_transfer_inserter_via_inserter_mall(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 1, "copper-plate": 30, "transport-belt": 6}
        obs["craftable"] = {"copper-cable": 15}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt", gear_inventory={"iron-gear-wheel": 4}))
        obs["entities"].append(
            {
                "name": "assembling-machine-1",
                "unit_number": 990,
                "position": {"x": 12, "y": 2},
                "recipe": "inserter",
                "electric_network_connected": True,
                "inventories": {"1": {}},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "copper-cable")
        self.assertIn("direct gear-to-belt transfer inserter", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "gear_belt_transfer_inserter_shortage")
        self.assertEqual(decision.metadata["repair_skill"], "bootstrap_build_item_mall")
        self.assertEqual(decision.metadata["target_item"], "inserter")

    def test_gear_belt_mall_places_direct_transfer_inserter_when_no_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 4.0, "y": 2.0})
        self.assertEqual(decision.action["direction"], 12)

    def test_gear_belt_mall_places_vertical_direct_transfer_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="iron-gear-wheel", inventory={"iron-gear-wheel": 4}),
                {
                    "name": "assembling-machine-1",
                    "unit_number": 902,
                    "position": {"x": 2.0, "y": 6.0},
                    "distance": 6,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {},
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 2.0, "y": 4.0})
        self.assertEqual(decision.action["direction"], 0)

    def test_gear_belt_mall_seeds_iron_after_direct_transfer_exists(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 930,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 12,
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "gear_mall_iron_plate_seed")

    def test_gear_belt_mall_builds_iron_input_line_before_repeating_gear_seed(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8, "transport-belt": 8}
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 920,
                    "position": {"x": -8, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                *gear_belt_mall_entities(belt_recipe="transport-belt"),
                {
                    "name": "inserter",
                    "unit_number": 930,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertNotIn("bootstrap_seed", decision.action)
        self.assertIn("iron-plate", decision.reason)

    def test_gear_belt_mall_topoffs_partial_iron_seed_with_missing_count(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-plate": 1},
            )
        )
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 930,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 12,
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["count"], 1)
        self.assertTrue(decision.action["bootstrap_seed"])
        self.assertEqual(decision.metadata["seed_reason"], "gear_mall_iron_plate_seed")

    def test_gear_belt_mall_replaces_reversed_direct_transfer_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 930,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 4,
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 930)
        self.assertIn("misoriented direct gear-to-belt transfer inserter", decision.reason)

    def test_gear_belt_mall_recovers_bootstrap_gears_when_direct_transfer_is_blocked(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
                belt_inventory={"iron-plate": 4},
            )
        )
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 930,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 12,
                "status_name": "waiting_for_space_in_destination",
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertIn("bootstrap gears", decision.reason)

    def test_gear_belt_mall_switches_from_direct_transfer_to_belt_lane_after_bootstrap_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 2, "burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 930,
                "position": {"x": 4.0, "y": 2.0},
                "direction": 12,
                "status_name": "waiting_for_space_in_destination",
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 3, "y": -1})

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
        obs["entities"].append(
            {
                "name": "small-electric-pole",
                "unit_number": 913,
                "position": {"x": 4.0, "y": 2.0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["position"], {"x": 3, "y": 5})

    def test_gear_belt_mall_places_regular_output_inserter_after_lane_when_available(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                "name": "small-electric-pole",
                "unit_number": 950,
                "position": {"x": 4.0, "y": 2.0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "inserter")
        self.assertEqual(decision.action["position"], {"x": 3, "y": 0})
        self.assertEqual(decision.action["direction"], 8)

    def test_gear_belt_mall_refuses_new_burner_output_inserter_when_regular_unavailable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                "name": "small-electric-pole",
                "unit_number": 950,
                "position": {"x": 4.0, "y": 2.0},
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("missing inserter for gear mall output inserter", decision.reason)

    def test_gear_belt_mall_crafts_regular_inserter_instead_of_burner_when_regular_is_usable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"burner-inserter": 1, "iron-plate": 1, "iron-gear-wheel": 1, "electronic-circuit": 1}
        obs["craftable"] = {"burner-inserter": 1, "inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "inserter")
        self.assertNotEqual(decision.action.get("recipe"), "burner-inserter")

    def test_gear_belt_mall_replaces_burner_output_inserter_when_regular_is_usable(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"inserter": 1}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                "direction": 8,
                "inventories": {"1": {"coal": 1}},
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 940)
        self.assertIn("replace burner", decision.reason)

    def test_gear_belt_mall_removes_misoriented_output_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "name": "inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {},
                    "electric_network_connected": True,
                },
                {
                    "name": "inserter",
                    "unit_number": 941,
                    "position": {"x": 5, "y": 0},
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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "name": "inserter",
                    "unit_number": 940,
                    "position": {"x": 3, "y": 0},
                    "direction": 8,
                    "inventories": {},
                    "electric_network_connected": True,
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

    def test_gear_belt_mall_does_not_relocate_direct_transfer_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 8}
        obs["craftable"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-gear-wheel": 4},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                "name": "inserter",
                "unit_number": 940,
                "position": {"x": 4, "y": 2},
                "direction": planner_module.WEST,
                "status_name": "waiting_for_source_items",
                "inventories": {},
                "electric_network_connected": True,
            }
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("missing inserter for gear mall output inserter", decision.reason)

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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": False,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertEqual(decision.action["position"], {"x": 7.0, "y": 0.0})
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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 942,
                    "position": {"x": 0, "y": 5},
                    "inventories": {"1": {"iron-plate": 4}},
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
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
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

    def test_gear_belt_mall_done_when_available_belt_target_reached(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"transport-belt": 20},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)
        self.assertIn("available belt target reached", decision.reason)

    def test_gear_belt_mall_waits_when_available_belts_below_target(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                gear_inventory={"iron-plate": 2},
                belt_inventory={"transport-belt": 4, "iron-plate": 2, "iron-gear-wheel": 1},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertFalse(decision.done)
        self.assertEqual(decision.action, {"type": "wait", "ticks": 600})
        self.assertIn("accumulate transport belts: 4/20", decision.reason)

    def test_gear_belt_mall_counts_buffered_belts_as_available_construction_stock(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            gear_belt_mall_entities(
                belt_recipe="transport-belt",
                belt_inventory={"transport-belt": 2},
            )
        )
        for index, x in enumerate((3, 4, 5), start=930):
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
                    "position": {"x": 5, "y": 0},
                    "direction": 0,
                    "inventories": {},
                    "electric_network_connected": True,
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 980,
                    "position": {"x": 30.0, "y": 30.0},
                    "inventories": {"1": {"transport-belt": 18}},
                },
            ]
        )

        decision = GearBeltMallLogisticsSkill(20).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIn("available belt target reached: 20/20", decision.reason)

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

    def test_iron_plate_logistic_line_takes_belts_from_belt_mall_output_chest(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 912,
                "position": {"x": 9, "y": 2},
                "inventories": {"1": {"transport-belt": 8}},
            }
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
        self.assertEqual(decision.action["unit_number"], 912)
        self.assertIn("output chest", decision.reason)

    def test_iron_plate_logistic_line_takes_buffered_chest_belts(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 913,
                "position": {"x": -13, "y": -12},
                "inventories": {"1": {"transport-belt": 8}},
            }
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
        self.assertEqual(decision.action["unit_number"], 913)
        self.assertIn("buffered transport belts", decision.reason)

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

    def test_iron_plate_logistic_line_takes_source_plate_for_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "electronic-circuit": 1}
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
        for index, segment in enumerate(layout["segments"]):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": 3000 + index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("iron source iron-plate", decision.reason)

    def test_iron_plate_logistic_line_clears_wrong_source_furnace_output(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "electronic-circuit": 1}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {
                    "1": {"coal": 4},
                    "2": {"iron-ore": 10},
                    "3": {"copper-plate": 20},
                },
            }
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        for index, segment in enumerate(layout["segments"]):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": 3300 + index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-plate")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("iron source furnace output", decision.reason)

    def test_iron_plate_logistic_line_waits_for_source_plate_after_output_clear(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "electronic-circuit": 1}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "inventories": {
                    "1": {"coal": 4},
                    "2": {"iron-ore": 10},
                    "3": {},
                },
            }
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        for index, segment in enumerate(layout["segments"]):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": 3400 + index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "wait")
        self.assertIn("wait for iron source furnace", decision.reason)

    def test_iron_plate_logistic_line_refuels_source_furnace_before_done(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "coal": 2}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].append(
            {
                "name": "stone-furnace",
                "unit_number": 950,
                "position": {"x": -8, "y": 2},
                "recipe": "iron-plate",
                "status_name": "no_fuel",
                "inventories": {
                    "2": {"iron-ore": 10},
                    "3": {},
                },
            }
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1
        for spec in (layout["source_inserter"], layout["target_inserter"]):
            obs["entities"].append(
                {
                    "name": "inserter",
                    "unit_number": unit_number,
                    "position": spec["position"],
                    "direction": spec["direction"],
                    "inventories": {},
                    "electric_network_connected": True,
                }
            )
            unit_number += 1

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("iron source furnace", decision.reason)

    def test_iron_plate_logistic_line_takes_buffered_gears_for_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "electronic-circuit": 1, "iron-plate": 1}
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
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 951,
                "position": {"x": -3, "y": -2},
                "inventories": {"1": {"iron-gear-wheel": 4}},
            }
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        for index, segment in enumerate(layout["segments"]):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": 3100 + index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 951)
        self.assertIn("buffered iron-gear-wheel", decision.reason)

    def test_iron_plate_logistic_line_crafts_endpoint_inserter_from_buffered_materials(self):
        obs = powered_automation_observation()
        obs["inventory"] = {
            "transport-belt": 20,
            "electronic-circuit": 1,
            "iron-plate": 1,
            "iron-gear-wheel": 1,
        }
        obs["craftable"] = {"inserter": 1}
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
        for index, segment in enumerate(layout["segments"]):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": 3200 + index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "inserter")
        self.assertIn("buffered construction materials", decision.reason)

    def test_iron_plate_logistic_line_uses_tile_centered_belt_positions(self):
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

        self.assertTrue(layout["segments"])
        for segment in layout["segments"][:3]:
            self.assertAlmostEqual(segment["position"]["x"] % 1, 0.5)
            self.assertAlmostEqual(segment["position"]["y"] % 1, 0.5)
        self.assertAlmostEqual(layout["source_inserter"]["position"]["x"] % 1, 0.5)
        self.assertAlmostEqual(layout["target_inserter"]["position"]["y"] % 1, 0.5)

    def test_iron_plate_logistic_line_avoids_existing_gear_output_side(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "inserter",
                    "unit_number": 930,
                    "position": {"x": 3.5, "y": 0.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 931,
                    "position": {"x": 3.5, "y": -0.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
            ]
        )

        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)

        self.assertNotEqual(layout["target_inserter"]["position"], {"x": 3.5, "y": 0.5})
        self.assertNotEqual(layout["target_belt"], {"x": 3.5, "y": -0.5})

    def test_iron_plate_logistic_line_detours_around_existing_gear_output_lane(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 910,
                    "position": {"x": 82.5, "y": -57.5},
                    "distance": 2,
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 911,
                    "position": {"x": 86.5, "y": -57.5},
                    "distance": 6,
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 77.5, "y": -68.5},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "inserter",
                    "unit_number": 930,
                    "position": {"x": 83.5, "y": -59.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 931,
                    "position": {"x": 83.5, "y": -60.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        route_positions = {planner_module._position_tuple(segment["position"]) for segment in layout["segments"]}

        self.assertNotEqual(layout["target_inserter"]["position"], {"x": 83.5, "y": -59.5})
        self.assertNotIn((83.5, -60.5), route_positions)
        self.assertNotIn((83.5, -59.5), route_positions)

    def test_iron_plate_logistic_line_prefers_wider_detour_over_power_pole(self):
        start = {"x": 79.5, "y": -68.5}
        end = {"x": 83.5, "y": -54.5}
        offsets = (1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 5.0, -5.0, 7.0, -7.0, 9.0, -9.0, 11.0, -11.0)
        blocked_lanes = {
            round(base_x + offset, 3)
            for base_x in (start["x"], end["x"])
            for offset in offsets
        } | {start["x"], end["x"]}
        obs = {"entities": []}
        unit_number = 930
        for lane_x in sorted(blocked_lanes - {90.5, 92.5, 94.5}):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": {"x": lane_x, "y": -60.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                }
            )
            unit_number += 1
        obs["entities"].extend(
            [
                {
                    "name": "inserter",
                    "unit_number": unit_number,
                    "position": {"x": 83.5, "y": -59.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {},
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": unit_number + 1,
                    "position": {"x": 90.5, "y": -63.5},
                    "inventories": {},
                },
            ]
        )

        segments = planner_module._iron_plate_line_segments(obs, start, end, center_tiles=True)
        route_positions = {planner_module._position_tuple(segment["position"]) for segment in segments}

        self.assertNotIn((90.5, -63.5), route_positions)
        self.assertIn((92.5, -63.5), route_positions)

    def test_connect_entities_tiles_straight_belt_flows_east(self):
        tiles = planner_module.connect_entities_tiles(
            {"entities": []}, {"x": 0.5, "y": 0.5}, {"x": 5.5, "y": 0.5}, "transport-belt"
        )
        self.assertEqual(len(tiles), 6)
        self.assertTrue(all(tile["direction"] == planner_module.EAST for tile in tiles))
        self.assertEqual(planner_module._position_tuple(tiles[0]["position"]), (0.5, 0.5))
        self.assertEqual(planner_module._position_tuple(tiles[-1]["position"]), (5.5, 0.5))

    def test_connect_entities_tiles_belt_flows_west_toward_consumer(self):
        # Regression: a lane feeding a consumer to the west must flow WEST, not EAST.
        tiles = planner_module.connect_entities_tiles(
            {"entities": []},
            {"x": 5.5, "y": 0.5},
            {"x": 0.5, "y": 0.5},
            "transport-belt",
            start_direction=planner_module.WEST,
            end_direction=planner_module.WEST,
        )
        self.assertTrue(tiles)
        self.assertEqual({tile["direction"] for tile in tiles}, {planner_module.WEST})

    def test_connect_entities_tiles_l_shaped_has_two_axes(self):
        tiles = planner_module.connect_entities_tiles(
            {"entities": []}, {"x": 0.5, "y": 0.5}, {"x": 4.5, "y": 3.5}, "transport-belt"
        )
        directions = {tile["direction"] for tile in tiles}
        self.assertIn(planner_module.EAST, directions)
        self.assertIn(planner_module.SOUTH, directions)

    def test_connect_entities_tiles_pipe_is_undirected(self):
        tiles = planner_module.connect_entities_tiles(
            {"entities": []}, {"x": 0.5, "y": 0.5}, {"x": 4.5, "y": 0.5}, "pipe"
        )
        self.assertTrue(tiles)
        self.assertEqual({tile["direction"] for tile in tiles}, {0})

    def test_connect_entities_tiles_poles_spaced_under_wire_reach(self):
        tiles = planner_module.connect_entities_tiles(
            {"entities": []}, {"x": 0.5, "y": 0.5}, {"x": 20.5, "y": 0.5}, "small-electric-pole"
        )
        xs = [tile["position"]["x"] for tile in tiles]
        gaps = [round(xs[index + 1] - xs[index], 3) for index in range(len(xs) - 1)]
        self.assertTrue(gaps)
        self.assertLessEqual(max(gaps), 7.5)
        self.assertEqual(xs[0], 0.5)
        self.assertEqual(xs[-1], 20.5)

    def test_connect_entities_tiles_respects_avoid_positions(self):
        start = {"x": 0.5, "y": 0.5}
        end = {"x": 6.5, "y": 4.5}
        base = planner_module.connect_entities_tiles({"entities": []}, start, end, "transport-belt")
        endpoints = {(0.5, 0.5), (6.5, 4.5)}
        interior = next(
            planner_module._position_tuple(tile["position"])
            for tile in base
            if planner_module._position_tuple(tile["position"]) not in endpoints
        )
        rerouted = planner_module.connect_entities_tiles(
            {"entities": []}, start, end, "transport-belt", avoid_positions={interior}
        )
        route = {planner_module._position_tuple(tile["position"]) for tile in rerouted}
        self.assertNotIn(interior, route)

    def test_iron_plate_logistic_line_treats_nearby_big_rock_as_blocker(self):
        obs = {
            "entities": [
                {
                    "name": "big-rock",
                    "type": "simple-entity",
                    "position": {"x": 85.69, "y": -53.31},
                    "inventories": {},
                }
            ]
        }

        blocker = planner_module._belt_line_position_blocker(obs, {"x": 86.5, "y": -54.5})

        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["name"], "big-rock")

    def test_iron_plate_logistic_line_keeps_empty_furnace_with_output_belt_as_source(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 2},
                    "status_name": "no_ingredients",
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "burner-inserter",
                    "unit_number": 951,
                    "position": {"x": -6.5, "y": 2.5},
                    "direction": planner_module.WEST,
                    "inventories": {},
                },
                {
                    "name": "transport-belt",
                    "unit_number": 952,
                    "position": {"x": -5.5, "y": 2.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
            ]
        )

        sources = planner_module._iron_plate_source_furnaces(obs)
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)

        self.assertEqual([source["unit_number"] for source in sources], [950])
        self.assertIsNotNone(layout)
        self.assertEqual(layout["source"]["unit_number"], 950)

    def test_iron_plate_logistic_line_endpoint_inserters_move_items_out_and_in(self):
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
        source_inserter = {
            "name": "inserter",
            "position": layout["source_inserter"]["position"],
            "direction": layout["source_inserter"]["direction"],
        }
        target_inserter = {
            "name": "inserter",
            "position": layout["target_inserter"]["position"],
            "direction": layout["target_inserter"]["direction"],
        }
        source_pickup, source_drop = planner_module._inserter_endpoints(source_inserter)
        target_pickup, target_drop = planner_module._inserter_endpoints(target_inserter)

        self.assertTrue(planner_module._point_inside_machine(source_pickup, layout["source"]))
        self.assertFalse(planner_module._point_inside_machine(source_drop, layout["source"]))
        self.assertFalse(planner_module._point_inside_machine(target_pickup, layout["gear_assembler"]))
        self.assertTrue(planner_module._point_inside_machine(target_drop, layout["gear_assembler"]))

    def test_iron_plate_logistic_line_keeps_existing_correct_target_endpoint(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 910,
                    "position": {"x": 82.5, "y": -57.5},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 911,
                    "position": {"x": 86.5, "y": -57.5},
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 77.5, "y": -68.5},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "burner-inserter",
                    "unit_number": 951,
                    "position": {"x": 83.5, "y": -55.5},
                    "direction": planner_module.SOUTH,
                    "inventories": {"1": {"coal": 1}},
                },
            ]
        )

        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)

        self.assertEqual(layout["target_inserter"]["position"], {"x": 83.5, "y": -55.5})
        self.assertEqual(layout["target_inserter"]["direction"], planner_module.SOUTH)

    def test_iron_plate_logistic_line_refuses_to_fuel_burner_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"coal": 2}
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
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 980,
                    "position": layout["source_inserter"]["position"],
                    "direction": layout["source_inserter"]["direction"],
                    "inventories": {"1": {"coal": 1}},
                },
                {
                    "name": "burner-inserter",
                    "unit_number": 981,
                    "position": layout["target_inserter"]["position"],
                    "direction": layout["target_inserter"]["direction"],
                    "status_name": "no_fuel",
                    "inventories": {},
                },
            ]
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("gear mall iron input inserter needs a powered inserter", decision.reason)
        self.assertIn("refusing to fuel burner inserter", decision.reason)

    def test_iron_plate_logistic_line_repairs_unpowered_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
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
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1
        obs["entities"].extend(
            [
                {
                    "name": "inserter",
                    "unit_number": 980,
                    "position": layout["source_inserter"]["position"],
                    "direction": layout["source_inserter"]["direction"],
                    "inventories": {},
                    "electric_network_connected": False,
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": 982,
                    "position": {
                        "x": layout["source_inserter"]["position"]["x"] + 2.0,
                        "y": layout["source_inserter"]["position"]["y"],
                    },
                    "electric_network_connected": False,
                    "inventories": {},
                },
            ]
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "connect_power")
        self.assertEqual(decision.action["unit_number"], 982)
        self.assertIn("iron source output inserter", decision.reason)

    def test_iron_plate_logistic_line_gets_pole_material_before_unpowered_endpoint_repair(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["craftable"] = {}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "tree-01",
                    "type": "tree",
                    "unit_number": 951,
                    "position": {"x": 1, "y": 0},
                    "distance": 1,
                    "inventories": {},
                },
            ]
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 980,
                "position": layout["source_inserter"]["position"],
                "direction": layout["source_inserter"]["direction"],
                "inventories": {},
                "electric_network_connected": False,
                "status_name": "no_power",
            }
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "tree-01")
        self.assertIn("pole wood", decision.reason)
        self.assertIn("iron source output inserter", decision.reason)

    def test_iron_plate_logistic_line_repairs_missing_endpoint_inserter_via_inserter_mall(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 1, "copper-plate": 30, "transport-belt": 20}
        obs["craftable"] = {"copper-cable": 15}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 990,
                    "position": {"x": 12, "y": 2},
                    "recipe": "inserter",
                    "electric_network_connected": True,
                    "inventories": {"1": {}},
                },
            ]
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "copper-cable")
        self.assertIn("iron source output inserter", decision.reason)
        self.assertEqual(decision.metadata["failure_root"], "logistics_endpoint_inserter_shortage")
        self.assertEqual(decision.metadata["repair_skill"], "bootstrap_build_item_mall")
        self.assertEqual(decision.metadata["target_item"], "inserter")

    def test_iron_plate_logistic_line_refuels_source_drill_before_endpoint_completion(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20, "coal": 2}
        obs["entities"].extend(gear_belt_mall_entities(belt_recipe="transport-belt"))
        obs["entities"].extend(
            [
                {
                    "name": "burner-mining-drill",
                    "unit_number": 949,
                    "position": {"x": -10, "y": 2},
                    "direction": planner_module.EAST,
                    "status_name": "no_fuel",
                    "inventories": {},
                },
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -8, "y": 2},
                    "recipe": "iron-plate",
                    "status_name": "no_ingredients",
                    "inventories": {"1": {"coal": 1}},
                },
            ]
        )
        layout = planner_module._find_iron_plate_logistic_line_to_gear_mall_layout(obs)
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 949)
        self.assertIn("iron source drill", decision.reason)

    def test_iron_plate_logistic_line_does_not_refuel_burning_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
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
        unit_number = 960
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
            unit_number += 1
        obs["entities"].extend(
            [
                {
                    "name": "burner-inserter",
                    "unit_number": 980,
                    "position": layout["source_inserter"]["position"],
                    "direction": layout["source_inserter"]["direction"],
                    "status_name": "waiting_for_source_items",
                    "inventories": {},
                },
                {
                    "name": "burner-inserter",
                    "unit_number": 981,
                    "position": layout["target_inserter"]["position"],
                    "direction": layout["target_inserter"]["direction"],
                    "status_name": "waiting_for_source_items",
                    "inventories": {},
                },
            ]
        )

        decision = IronPlateLogisticLineToGearMallSkill(20).next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

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

    def test_site_input_logistic_line_takes_belts_from_belt_mall_output_chest(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "wooden-chest",
                    "unit_number": 980,
                    "position": {"x": 5.0, "y": 2.0},
                    "inventories": {"1": {"transport-belt": 8}},
                },
                {
                    "name": "inserter",
                    "unit_number": 981,
                    "position": {"x": 4.0, "y": 2.0},
                    "direction": 12,
                    "electric_network_connected": True,
                    "inventories": {},
                },
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
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertIn("output chest", decision.reason)

    def test_site_input_logistic_line_takes_belts_from_buffered_chest(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "wooden-chest",
                    "unit_number": 980,
                    "position": {"x": 0.0, "y": 20.0},
                    "inventories": {"1": {"transport-belt": 12}},
                },
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
        self.assertEqual(decision.action["unit_number"], 980)
        self.assertIn("buffered transport belts", decision.reason)

    def test_site_input_logistic_line_done_when_completed_route_no_longer_has_layout_candidate(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": -8, "y": 8},
            "recipe": "copper-plate",
            "inventories": {"3": {"copper-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8, "y": 8},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer])
        layout = planner_module._find_site_input_logistic_line_layout(obs, item="copper-plate")
        self.assertIsNotNone(layout)

        next_unit = 970
        for segment in layout["segments"]:
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": next_unit,
                    "position": segment["position"],
                    "direction": segment["direction"],
                }
            )
            next_unit += 1
        for endpoint_key in ("source_inserter", "target_inserter"):
            spec = layout[endpoint_key]
            obs["entities"].append(
                {
                    "name": "inserter",
                    "unit_number": next_unit,
                    "position": spec["position"],
                    "direction": spec["direction"],
                    "electric_network_connected": True,
                }
            )
            next_unit += 1

        self.assertIsNone(planner_module._find_site_input_logistic_line_layout(obs, item="copper-plate"))
        decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)
        self.assertIn("already observed", decision.reason)

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

    def test_site_input_logistic_line_refuses_to_mine_existing_inserter_on_route(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "inventories": {"3": {"copper-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        production_inserter = {
            "name": "inserter",
            "unit_number": 960,
            "position": {"x": 2.0, "y": 0.0},
            "direction": planner_module.NORTH,
            "electric_network_connected": True,
            "pickup_position": {"x": 2.0, "y": -1.0},
            "drop_position": {"x": 2.0, "y": 1.2},
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, production_inserter])
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": {"name": "inserter", "unit_number": 952, "direction": planner_module.EAST},
            },
            "target_inserter": {
                "position": {"x": 7.0, "y": 0.0},
                "direction": planner_module.WEST,
                "entity": {"name": "inserter", "unit_number": 953, "direction": planner_module.WEST},
            },
            "segments": [
                {"position": {"x": 2.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("blocked by existing inserter", decision.reason)
        self.assertIn("reroute", decision.reason)

    def test_site_input_logistic_line_refuses_reserved_inserter_slot_on_route(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "inventories": {"3": {"copper-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        unrelated_assembler = {
            "name": "assembling-machine-1",
            "unit_number": 960,
            "position": {"x": 2.0, "y": 2.0},
            "recipe": "assembling-machine-1",
            "electric_network_connected": True,
            "inventories": {"2": {"iron-plate": 4}},
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, unrelated_assembler])
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": {"name": "inserter", "unit_number": 952, "direction": planner_module.EAST},
            },
            "target_inserter": {
                "position": {"x": 7.0, "y": 0.0},
                "direction": planner_module.WEST,
                "entity": {"name": "inserter", "unit_number": 953, "direction": planner_module.WEST},
            },
            "segments": [
                {"position": {"x": 2.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertIsNone(decision.action)
        self.assertIn("reserved inserter slot", decision.reason)
        self.assertIn("reroute", decision.reason)

    def test_site_input_logistic_line_builds_reachable_segment_before_walking_to_far_span(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 35.0, "y": 0.0}}
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 0.0, "y": 0.0},
                    "recipe": "copper-plate",
                    "inventories": {"3": {"copper-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 60.0, "y": 0.0},
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )
        layout = {
            "item": "copper-plate",
            "source": obs["entities"][-2],
            "consumer": obs["entities"][-1],
            "source_inserter": {"position": {"x": 1.0, "y": 0.0}, "direction": 4, "entity": {"unit_number": 952}},
            "target_inserter": {"position": {"x": 59.0, "y": 0.0}, "direction": 4, "entity": {"unit_number": 953}},
            "segments": [
                {"position": {"x": 0.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
                {"position": {"x": 35.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["position"], {"x": 35.0, "y": 0.0})
        self.assertIn("reachable copper-plate site input belt", decision.reason)

    def test_site_input_logistic_line_moves_to_nearest_buildable_segment_not_route_first(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 0.0}}
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 0.0, "y": 0.0},
                    "recipe": "copper-plate",
                    "inventories": {"3": {"copper-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 100.0, "y": 0.0},
                    "recipe": "automation-science-pack",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )
        layout = {
            "item": "copper-plate",
            "source": obs["entities"][-2],
            "consumer": obs["entities"][-1],
            "source_inserter": {"position": {"x": 1.0, "y": 0.0}, "direction": 4, "entity": {"unit_number": 952}},
            "target_inserter": {"position": {"x": 99.0, "y": 0.0}, "direction": 4, "entity": {"unit_number": 953}},
            "segments": [
                {"position": {"x": 100.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
                {"position": {"x": 40.0, "y": 0.0}, "direction": planner_module.EAST, "entity": None},
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 43.0, "y": 0.0})
        self.assertIn("nearest buildable site input logistics belt", decision.reason)

    def test_site_input_logistic_line_repairs_unpowered_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 4.0, "y": 0.0}}
        obs["inventory"] = {"transport-belt": 4, "small-electric-pole": 1}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "inventories": {"3": {"copper-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        unpowered_inserter = {
            "name": "inserter",
            "unit_number": 953,
            "position": {"x": 4.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": False,
            "status_name": "no_power",
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, unpowered_inserter])
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": {
                    "name": "inserter",
                    "unit_number": 952,
                    "position": {"x": 1.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "electric_network_connected": True,
                },
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": unpowered_inserter,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 954, "direction": planner_module.EAST},
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertIn(decision.action["type"], {"build", "connect_power"})
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIn("site consumer input inserter", decision.reason)

    def test_site_input_logistic_line_fuels_empty_copper_source_before_done(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 0.0}}
        obs["inventory"] = {"coal": 12, "transport-belt": 4}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "status_name": "no_fuel",
            "status": 52,
            "inventories": {"2": {"copper-ore": 28}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        source_inserter = {
            "name": "inserter",
            "unit_number": 952,
            "position": {"x": 1.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        target_inserter = {
            "name": "inserter",
            "unit_number": 953,
            "position": {"x": 4.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, source_inserter, target_inserter])
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": source_inserter,
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": target_inserter,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 954, "direction": planner_module.EAST},
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("copper source furnace", decision.reason)

    def test_site_input_logistic_line_uses_stocked_copper_source_before_drill_refuel(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 0.0}}
        obs["inventory"] = {"coal": 12, "transport-belt": 4}
        source_drill = {
            "name": "burner-mining-drill",
            "unit_number": 949,
            "position": {"x": -2.0, "y": 0.0},
            "direction": planner_module.EAST,
            "status_name": "no_fuel",
            "status": 53,
            "inventories": {},
            "mining_target": "copper-ore",
        }
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "status_name": "no_fuel",
            "status": 52,
            "inventories": {"2": {"copper-ore": 28}, "3": {"copper-plate": 80}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        source_inserter = {
            "name": "inserter",
            "unit_number": 952,
            "position": {"x": 1.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        target_inserter = {
            "name": "inserter",
            "unit_number": 953,
            "position": {"x": 4.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        obs["entities"].extend(
            [mall_assembler(recipe="transport-belt"), source_drill, source, consumer, source_inserter, target_inserter]
        )
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": source_inserter,
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": target_inserter,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 954, "direction": planner_module.EAST},
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertIsNone(decision.action)
        self.assertTrue(decision.done)
        self.assertIn("copper-plate site input logistics line is built", decision.reason)

    def test_site_input_logistic_line_fuels_copper_source_before_collecting_belts(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 0.0}}
        obs["inventory"] = {"coal": 12}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "status_name": "no_fuel",
            "status": 52,
            "inventories": {"2": {"copper-ore": 28}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt", inventory={"transport-belt": 8}),
                source,
                consumer,
            ]
        )
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": None,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": None,
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("copper source furnace", decision.reason)

    def test_build_item_mall_follows_copper_site_input_source_recovery(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 0.0, "y": 0.0}}
        obs["inventory"] = {"coal": 12, "transport-belt": 4, "iron-gear-wheel": 1}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "status_name": "no_fuel",
            "status": 52,
            "inventories": {"2": {"copper-ore": 28}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {"1": {"iron-gear-wheel": 1}},
        }
        source_inserter = {
            "name": "inserter",
            "unit_number": 952,
            "position": {"x": 1.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        target_inserter = {
            "name": "inserter",
            "unit_number": 953,
            "position": {"x": 4.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": True,
        }
        obs["entities"].extend([mall_assembler(recipe="transport-belt"), source, consumer, source_inserter, target_inserter])
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": source_inserter,
            },
            "target_inserter": {
                "position": {"x": 4.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": target_inserter,
            },
            "segments": [
                {
                    "position": {"x": 2.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 954, "direction": planner_module.EAST},
                }
            ],
        }

        with (
            patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout),
            patch("factorio_ai.planner._obsolete_build_item_mall_buffer_cleanup_decision", return_value=None),
        ):
            decision = BuildItemMallSkill("automation-science-pack", 20).next_action(
                obs,
                reference_position=consumer["position"],
            )

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "coal")
        self.assertEqual(decision.action["unit_number"], 950)
        self.assertIn("copper source furnace", decision.reason)

    def test_build_item_mall_feeds_missing_science_gear_before_copper_batch_topup(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 8.0, "y": 0.0}}
        obs["inventory"] = {"iron-gear-wheel": 1}
        assembler = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {"2": {"copper-plate": 3}},
        }
        obs["entities"].append(assembler)

        with patch("factorio_ai.planner._obsolete_build_item_mall_buffer_cleanup_decision", return_value=None):
            decision = BuildItemMallSkill("automation-science-pack", 20).next_action(
                obs,
                reference_position=assembler["position"],
            )

        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 951)
        self.assertIn("insert iron-gear-wheel into automation-science-pack mall assembler", decision.reason)

    def test_site_input_logistic_line_builds_remote_endpoint_power_corridor(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 34.5, "y": 0.5}}
        obs["inventory"] = {"transport-belt": 4, "small-electric-pole": 1}
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 0.0, "y": 0.0},
            "recipe": "copper-plate",
            "inventories": {"3": {"copper-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 42.0, "y": 0.0},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        unpowered_inserter = {
            "name": "inserter",
            "unit_number": 953,
            "position": {"x": 40.0, "y": 0.0},
            "direction": planner_module.EAST,
            "electric_network_connected": False,
            "status_name": "no_power",
        }
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "steam-engine",
                    "unit_number": 990,
                    "position": {"x": 0.5, "y": 0.5},
                    "electric_network_connected": True,
                    "electric_network_id": 1,
                    "inventories": {},
                },
                {
                    "name": "small-electric-pole",
                    "unit_number": 991,
                    "position": {"x": 30.5, "y": 0.5},
                    "electric_network_connected": False,
                    "electric_network_id": 1,
                    "inventories": {},
                },
                source,
                consumer,
                unpowered_inserter,
            ]
        )
        layout = {
            "item": "copper-plate",
            "source": source,
            "consumer": consumer,
            "source_inserter": {
                "position": {"x": 1.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": {
                    "name": "inserter",
                    "unit_number": 952,
                    "position": {"x": 1.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "electric_network_connected": True,
                },
            },
            "target_inserter": {
                "position": {"x": 40.0, "y": 0.0},
                "direction": planner_module.EAST,
                "entity": unpowered_inserter,
            },
            "segments": [
                {
                    "position": {"x": 39.0, "y": 0.0},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 954, "direction": planner_module.EAST},
                }
            ],
        }

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="copper-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "small-electric-pole")
        self.assertIs(decision.action["allow_nearby"], False)
        self.assertIn("power corridor for site consumer input inserter", decision.reason)

    def test_site_input_logistic_line_ignores_nearby_consumer_input_inventory_as_source(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 4}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -12, "y": 8},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 951,
                    "position": {"x": 0, "y": 8},
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {"2": {"iron-plate": 4}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 8, "y": 8},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        decision = SiteInputLogisticLineSkill(20, item="iron-plate").next_action(obs)

        self.assertEqual(layout["source"]["unit_number"], 950)
        self.assertTrue(all(abs(segment["position"]["x"] % 1 - 0.5) < 0.001 for segment in layout["segments"]))
        self.assertTrue(all(abs(segment["position"]["y"] % 1 - 0.5) < 0.001 for segment in layout["segments"]))
        self.assertAlmostEqual(layout["source_inserter"]["position"]["x"] % 1, 0.5, places=3)
        self.assertAlmostEqual(layout["source_inserter"]["position"]["y"] % 1, 0.5, places=3)
        self.assertAlmostEqual(layout["target_inserter"]["position"]["x"] % 1, 0.5, places=3)
        self.assertAlmostEqual(layout["target_inserter"]["position"]["y"] % 1, 0.5, places=3)
        self.assertEqual(layout["source_inserter"]["direction"], planner_module.WEST)
        self.assertEqual(layout["target_inserter"]["direction"], planner_module.WEST)
        self.assertLess(layout["source"]["position"]["x"], layout["source_inserter"]["position"]["x"])
        self.assertLess(layout["source_inserter"]["position"]["x"], layout["segments"][0]["position"]["x"])
        self.assertLess(layout["segments"][-1]["position"]["x"], layout["target_inserter"]["position"]["x"])
        self.assertLess(layout["target_inserter"]["position"]["x"], layout["consumer"]["position"]["x"])
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")

    def test_site_input_logistic_line_endpoint_inserters_move_items_out_and_in(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -12, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 8, "y": 8},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        source_inserter = {
            "name": "inserter",
            "position": layout["source_inserter"]["position"],
            "direction": layout["source_inserter"]["direction"],
        }
        target_inserter = {
            "name": "inserter",
            "position": layout["target_inserter"]["position"],
            "direction": layout["target_inserter"]["direction"],
        }
        source_pickup, source_drop = planner_module._inserter_endpoints(source_inserter)
        target_pickup, target_drop = planner_module._inserter_endpoints(target_inserter)

        self.assertTrue(planner_module._point_inside_machine(source_pickup, layout["source"]))
        self.assertFalse(planner_module._point_inside_machine(source_drop, layout["source"]))
        self.assertFalse(planner_module._point_inside_machine(target_pickup, layout["consumer"]))
        self.assertTrue(planner_module._point_inside_machine(target_drop, layout["consumer"]))

    def test_site_input_logistic_line_routes_corner_belt_with_turn_direction(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -12, "y": 2},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 8, "y": 8},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        first_corner = next(
            segment
            for segment in layout["segments"]
            if abs(segment["position"]["x"] + 2.5) < 0.001 and abs(segment["position"]["y"] - 2.5) < 0.001
        )
        before_first_corner = next(
            segment
            for segment in layout["segments"]
            if abs(segment["position"]["x"] + 3.5) < 0.001 and abs(segment["position"]["y"] - 2.5) < 0.001
        )
        second_corner = next(
            segment
            for segment in layout["segments"]
            if abs(segment["position"]["x"] + 2.5) < 0.001 and abs(segment["position"]["y"] - 8.5) < 0.001
        )

        self.assertEqual(layout["segments"][0]["direction"], planner_module.EAST)
        self.assertEqual(layout["segments"][-1]["direction"], planner_module.EAST)
        self.assertEqual(before_first_corner["direction"], planner_module.EAST)
        self.assertEqual(first_corner["direction"], planner_module.SOUTH)
        self.assertEqual(second_corner["direction"], planner_module.EAST)

    def test_site_input_logistic_line_keeps_vertical_output_and_input_directions(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"transport-belt": 20}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 0, "y": 0},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 5, "y": 10},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )

        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")

        self.assertEqual(layout["source_inserter"]["direction"], planner_module.NORTH)
        self.assertEqual(layout["target_inserter"]["direction"], planner_module.NORTH)
        self.assertEqual(layout["segments"][0]["direction"], planner_module.SOUTH)
        self.assertEqual(layout["segments"][-1]["direction"], planner_module.SOUTH)
        self.assertTrue(any(segment["direction"] == planner_module.EAST for segment in layout["segments"][2:-2]))

    def test_site_input_logistic_line_offsets_negative_coordinate_furnace_endpoint_outside_footprint(self):
        source = {
            "name": "stone-furnace",
            "unit_number": 950,
            "position": {"x": 79, "y": -20},
            "recipe": "iron-plate",
            "inventories": {"3": {"iron-plate": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 952,
            "position": {"x": 79, "y": -35},
            "recipe": "iron-gear-wheel",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }

        endpoint = planner_module._site_input_line_endpoint_candidates(source, consumer)[0]
        source_inserter = planner_module._tile_center_position(endpoint["source_inserter"])
        source_belt = planner_module._tile_center_position(endpoint["source_belt"])

        self.assertEqual(source_inserter, {"x": 79.5, "y": -21.5})
        self.assertEqual(source_belt, {"x": 79.5, "y": -22.5})
        self.assertEqual(endpoint["source_direction"], planner_module.SOUTH)

    def test_site_input_logistic_line_removes_belt_blocking_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["player"] = {"position": {"x": 1.5, "y": 0.5}}
        obs["inventory"] = {"inserter": 1}
        source = {
            "name": "wooden-chest",
            "unit_number": 950,
            "position": {"x": 0.5, "y": 0.5},
            "inventories": {"1": {"iron-gear-wheel": 20}},
        }
        consumer = {
            "name": "assembling-machine-1",
            "unit_number": 951,
            "position": {"x": 8.0, "y": 0.5},
            "recipe": "automation-science-pack",
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {},
        }
        layout = {
            "item": "iron-gear-wheel",
            "source": source,
            "consumer": consumer,
            "consumer_recipe": "automation-science-pack",
            "source_inserter": {"position": {"x": 1.5, "y": 0.5}, "direction": planner_module.WEST, "entity": None},
            "target_inserter": {
                "position": {"x": 7.5, "y": 0.5},
                "direction": planner_module.EAST,
                "entity": {"name": "inserter", "unit_number": 970, "position": {"x": 7.5, "y": 0.5}, "direction": planner_module.EAST},
            },
            "segments": [
                {
                    "position": {"x": 2.5, "y": 0.5},
                    "direction": planner_module.EAST,
                    "entity": {"name": "transport-belt", "unit_number": 971, "position": {"x": 2.5, "y": 0.5}, "direction": planner_module.EAST},
                }
            ],
        }
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                source,
                consumer,
                {
                    "name": "transport-belt",
                    "unit_number": 972,
                    "position": {"x": 1.5, "y": 0.5},
                    "direction": planner_module.EAST,
                    "inventories": {},
                },
                layout["target_inserter"]["entity"],
                layout["segments"][0]["entity"],
            ]
        )

        with patch("factorio_ai.planner._find_site_input_logistic_line_layout", return_value=layout):
            decision = SiteInputLogisticLineSkill(20, item="iron-gear-wheel").next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], 972)
        self.assertIn("blocking transport-belt", decision.reason)

    def test_site_input_logistic_line_repairs_misoriented_belt_before_missing_segment_when_belts_empty(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": 0, "y": 0},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 5, "y": 10},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )
        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        wrong_unit = 970
        for index, segment in enumerate(layout["segments"]):
            if index == 2:
                continue
            direction = segment["direction"]
            unit_number = 980 + index
            if index == 7:
                direction = planner_module.NORTH if direction != planner_module.NORTH else planner_module.EAST
                unit_number = wrong_unit
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": unit_number,
                    "position": segment["position"],
                    "direction": direction,
                    "inventories": {},
                }
            )
        obs["player"]["position"] = layout["segments"][7]["position"]

        decision = SiteInputLogisticLineSkill(20, item="iron-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["unit_number"], wrong_unit)
        self.assertIn("misoriented transport belt", decision.reason)

    def test_site_input_logistic_line_does_not_relocate_existing_source_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -12, "y": 8},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 8, "y": 8},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
            ]
        )
        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        for index, segment in enumerate(layout["segments"], start=980):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
        obs["entities"].append(
            {
                "name": "burner-inserter",
                "unit_number": 970,
                "position": layout["source_inserter"]["position"],
                "direction": layout["source_inserter"]["direction"],
                "inventories": {"1": {"coal": 1}},
            }
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")
        self.assertNotEqual(decision.action.get("unit_number"), 970)
        self.assertIn("site consumer input inserter", decision.reason)

    def test_site_input_logistic_line_takes_buffered_gears_for_endpoint_inserter(self):
        obs = powered_automation_observation()
        obs["inventory"] = {"iron-plate": 1, "electronic-circuit": 1}
        obs["entities"].extend(
            [
                mall_assembler(recipe="transport-belt"),
                {
                    "name": "stone-furnace",
                    "unit_number": 950,
                    "position": {"x": -12, "y": 8},
                    "recipe": "iron-plate",
                    "inventories": {"3": {"iron-plate": 20}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 952,
                    "position": {"x": 8, "y": 8},
                    "recipe": "iron-gear-wheel",
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {},
                },
                {
                    "name": "wooden-chest",
                    "unit_number": 990,
                    "position": {"x": 0, "y": 8},
                    "inventories": {"1": {"iron-gear-wheel": 8}},
                },
            ]
        )
        layout = planner_module._find_site_input_logistic_line_layout(obs, item="iron-plate")
        for index, segment in enumerate(layout["segments"], start=980):
            obs["entities"].append(
                {
                    "name": "transport-belt",
                    "unit_number": index,
                    "position": segment["position"],
                    "direction": segment["direction"],
                    "inventories": {},
                }
            )
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 970,
                "position": layout["source_inserter"]["position"],
                "direction": layout["source_inserter"]["direction"],
                "electric_network_connected": True,
                "inventories": {},
            }
        )

        decision = SiteInputLogisticLineSkill(20, item="iron-plate").next_action(obs)

        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-gear-wheel")
        self.assertEqual(decision.action["unit_number"], 990)
        self.assertIn("site consumer input inserter", decision.reason)

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
            "inserter": 1,
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
            "inserter": 1,
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
            # verified power-block geometry (WEST pump turns=0): boiler at pump+{2,0.5} dir SOUTH
            "name": "boiler",
            "unit_number": 602,
            "position": {"x": 12.5, "y": 11},
            "direction": 8,
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
            "position": {"x": 12.5, "y": 14.5},  # pump+{2,4}
            "direction": 8,
            "status": 1,
            "distance": 13,
            "inventories": {},
            "fluids": {"1": {"name": "steam", "amount": 80}},
        },
        {
            "name": "small-electric-pole",
            "unit_number": 605,
            "position": {"x": 10.5, "y": 14.5},  # power-block pole, pump+{0,4}
            "direction": 0,
            "distance": 12,
            "electric_network_connected": True,
            "inventories": {},
            "fluids": {},
        },
        {
            "name": "small-electric-pole",
            "unit_number": 604,
            "position": {"x": 10.5, "y": 6.5},  # separate pole for lab-site tests (lab_sites refs 604)
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
                    "position": {"x": 6, "y": 2},
                    "distance": 6,
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
    drill_coal = 20 if reserve_fuel else 3
    inserter_coal = 4 if reserve_fuel else 2
    furnace_coal = 20 if reserve_fuel else 3
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
