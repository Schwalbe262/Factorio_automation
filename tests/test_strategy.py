import unittest

from factorio_ai.strategy import (
    heuristic_strategy,
    make_layout_improvement_context,
    make_strategy_payload,
    normalize_strategy_response,
    reconcile_strategy_decision,
    skill_catalog_payload,
)


def gear_mall_needs_plate_line_observation() -> dict:
    return {
        "inventory": {"iron-plate": 20},
        "entities": [
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
                "position": {"x": 4.5, "y": 0.5},
                "electric_network_connected": True,
                "inventories": {"1": {"transport-belt": 8}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 200,
                "recipe": "iron-plate",
                "position": {"x": 96.5, "y": 0.5},
                "inventories": {"2": {"iron-plate": 24}},
            },
        ],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "logistics": {"researched": True},
            }
        },
    }


def gear_belt_mall_needs_bootstrap_observation() -> dict:
    return {
        "inventory": {"iron-plate": 40},
        "entities": [
            {
                "name": "assembling-machine-1",
                "unit_number": 100,
                "recipe": "iron-gear-wheel",
                "position": {"x": 0.5, "y": 0.5},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 101,
                "recipe": "transport-belt",
                "position": {"x": 3.5, "y": 0.5},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {"2": {"iron-gear-wheel": 1}},
            },
            {
                "name": "stone-furnace",
                "unit_number": 200,
                "recipe": "iron-plate",
                "position": {"x": 96.5, "y": 0.5},
                "inventories": {"2": {"iron-plate": 24}},
            },
        ],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "logistics": {"researched": True},
            }
        },
    }


def gear_belt_mall_has_local_plate_seed_observation() -> dict:
    observation = gear_belt_mall_needs_bootstrap_observation()
    observation["inventory"] = {}
    observation["entities"][0]["inventories"] = {}
    observation["entities"][1]["inventories"] = {"2": {"iron-gear-wheel": 3}}
    observation["entities"].append(
        {
            "name": "assembling-machine-1",
            "unit_number": 102,
            "recipe": "electronic-circuit",
            "position": {"x": 8.5, "y": 2.5},
            "electric_network_connected": True,
            "status_name": "item_ingredient_shortage",
            "inventories": {"2": {"iron-plate": 4}},
        }
    )
    return observation


def gear_belt_mall_transfer_connection_missing_observation() -> dict:
    return {
        "player": {"position": {"x": 52.5, "y": 54.5}, "character_valid": False},
        "inventory": {"iron-plate": 1},
        "entities": [
            {
                "name": "assembling-machine-1",
                "unit_number": 214,
                "recipe": "iron-gear-wheel",
                "position": {"x": 52.5, "y": 58.5},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 217,
                "recipe": "transport-belt",
                "position": {"x": 56.5, "y": 58.5},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 15,
                "recipe": "iron-plate",
                "position": {"x": 47.5, "y": 58.5},
                "inventories": {"2": {"iron-plate": 8}},
            },
            {
                "name": "wooden-chest",
                "unit_number": 300,
                "position": {"x": 90.5, "y": 90.5},
                "inventories": {"1": {"transport-belt": 10}},
            },
        ],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "logistics": {"researched": False, "progress": 0.05},
            }
        },
    }


def gear_mall_output_logistics_blocked_observation() -> dict:
    return {
        "player": {"position": {"x": 0, "y": 0}},
        "inventory": {"iron-plate": 8, "transport-belt": 3},
        "entities": [
            {
                "name": "assembling-machine-1",
                "unit_number": 146,
                "recipe": "iron-gear-wheel",
                "position": {"x": 0.5, "y": 0.5},
                "electric_network_connected": True,
                "status_name": "full_output",
                "inventories": {"3": {"iron-gear-wheel": 5}},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 1779,
                "recipe": "transport-belt",
                "position": {"x": 4.5, "y": 0.5},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {"2": {"iron-gear-wheel": 3}},
            },
        ],
        "resources": [{"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2}],
        "research": {"technologies": {"automation": {"researched": True}}},
    }


def gear_mall_short_site_input_route_observation() -> dict:
    return {
        "inventory": {"transport-belt": 20},
        "entities": [
            {
                "name": "stone-furnace",
                "unit_number": 1458,
                "recipe": "iron-plate",
                "position": {"x": -12, "y": 2},
                "status_name": "no_fuel",
                "inventories": {"2": {"iron-ore": 2}},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 146,
                "recipe": "iron-gear-wheel",
                "position": {"x": 8, "y": 8},
                "electric_network_connected": True,
                "status_name": "item_ingredient_shortage",
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 1779,
                "recipe": "transport-belt",
                "position": {"x": 12, "y": 8},
                "electric_network_connected": True,
                "inventories": {"1": {"transport-belt": 8}},
            },
        ],
        "resources": [{"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2}],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "logistics": {"researched": True},
            }
        },
    }


def gear_mall_needs_plate_line_without_belts_observation() -> dict:
    observation = gear_mall_needs_plate_line_observation()
    observation["inventory"] = {}
    observation["entities"][1]["inventories"] = {}
    observation["entities"][2]["position"] = {"x": 33.0, "y": 0.5}
    return observation


def gear_mall_needs_long_plate_line_without_belts_observation() -> dict:
    observation = gear_mall_needs_plate_line_without_belts_observation()
    observation["entities"][2]["position"] = {"x": 153.0, "y": 0.5}
    return observation


def gear_mall_relocation_with_downstream_power_block_observation() -> dict:
    observation = gear_mall_needs_long_plate_line_without_belts_observation()
    observation["inventory"] = {"small-electric-pole": 20}
    observation["entities"].extend(
        [
            {
                "name": "boiler",
                "unit_number": 300,
                "position": {"x": -2, "y": 0},
                "status_name": "no_fuel",
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 301,
                "recipe": "electronic-circuit",
                "position": {"x": 8.5, "y": 2.5},
                "electric_network_connected": True,
                "status_name": "no_power",
                "inventories": {},
            },
        ]
    )
    return observation


def gear_mall_inventory_rebuild_with_downstream_power_block_observation() -> dict:
    observation = gear_mall_relocation_with_downstream_power_block_observation()
    observation["inventory"] = {"small-electric-pole": 20, "assembling-machine-1": 2}
    observation["entities"] = [
        entity
        for entity in observation["entities"]
        if entity.get("unit_number") not in {100, 101}
    ]
    return observation


def gear_mall_target_rebuild_with_downstream_power_block_observation() -> dict:
    observation = gear_mall_inventory_rebuild_with_downstream_power_block_observation()
    observation["inventory"] = {"small-electric-pole": 20}
    observation["entities"].extend(
        [
            {
                "name": "assembling-machine-1",
                "unit_number": 401,
                "recipe": "iron-gear-wheel",
                "position": {"x": 158.5, "y": -4.0},
                "electric_network_connected": False,
                "status_name": "no_power",
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 402,
                "position": {"x": 161.5, "y": -4.0},
                "electric_network_connected": False,
                "status_name": "no_recipe",
                "inventories": {},
            },
        ]
    )
    return observation


def partial_gear_mall_relocation_observation() -> dict:
    observation = gear_mall_needs_long_plate_line_without_belts_observation()
    observation["inventory"] = {"small-electric-pole": 20, "assembling-machine-1": 1}
    observation["entities"] = [entity for entity in observation["entities"] if entity.get("unit_number") != 100]
    observation["entities"][0]["recipe"] = "small-electric-pole"
    observation["entities"][0]["inventories"] = {}
    return observation


def partial_gear_belt_mall_relocation_observation() -> dict:
    return {
        "inventory": {"transport-belt": 10, "iron-plate": 1},
        "entities": [
            {
                "name": "small-electric-pole",
                "unit_number": 90,
                "position": {"x": 48.5, "y": 52.5},
                "electric_network_connected": True,
                "electric_network_id": 1,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 200,
                "recipe": "iron-plate",
                "position": {"x": 47.0, "y": 63.0},
                "inventories": {"2": {"iron-plate": 24}},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 800,
                "recipe": "iron-gear-wheel",
                "position": {"x": 52.5, "y": 58.5},
                "electric_network_connected": True,
                "electric_network_id": 1,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 801,
                "recipe": "transport-belt",
                "position": {"x": 52.5, "y": 62.5},
                "electric_network_connected": True,
                "electric_network_id": 1,
                "inventories": {},
            },
        ],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "steam-power": {"researched": True},
            }
        },
        "player": {"kind": "server", "character_valid": False, "position": {"x": 52.5, "y": 62.5}},
    }


def burner_drill_replacement_observation(*, electric_researched: bool = False) -> dict:
    return {
        "inventory": {"automation-science-pack": 25, "iron-plate": 40, "copper-plate": 20},
        "entities": [
            {
                "name": "steam-engine",
                "unit_number": 10,
                "position": {"x": 0, "y": 0},
                "electric_network_connected": True,
                "fluids": {"1": {"name": "steam", "amount": 80}},
                "inventories": {},
            },
            {
                "name": "small-electric-pole",
                "unit_number": 11,
                "position": {"x": 2, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "lab",
                "unit_number": 12,
                "position": {"x": 3, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 20,
                "position": {"x": 6, "y": 0},
                "mining_target": "iron-ore",
                "inventories": {"1": {"coal": 2}},
            },
        ],
        "resources": [{"name": "iron-ore", "position": {"x": 6, "y": 0}}],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "electric-mining-drill": {"researched": electric_researched},
                "logistics": {"researched": False},
            }
        },
    }


def factory_power_down_before_electric_research_observation() -> dict:
    observation = burner_drill_replacement_observation()
    observation["inventory"] = {"iron-plate": 40, "copper-plate": 20, "coal": 8}
    observation["entities"].extend(
        [
            {
                "name": "boiler",
                "unit_number": 30,
                "position": {"x": -2, "y": 0},
                "status_name": "no_fuel",
                "fluids": {"1": {"name": "water", "amount": 200}},
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 31,
                "recipe": "automation-science-pack",
                "position": {"x": 4, "y": 0},
                "status": 54,
                "status_name": "no_power",
                "electric_network_connected": True,
                "inventories": {},
            },
        ]
    )
    for entity in observation["entities"]:
        if entity.get("name") == "steam-engine":
            entity["status_name"] = "no_input_fluid"
            entity["fluids"] = {}
    return observation


class StrategyTests(unittest.TestCase):
    def test_electronic_circuit_goal_detects_iron_bottleneck(self):
        result = heuristic_strategy(
            "전자회로를 만들어야함",
            {
                "inventory": {"iron-plate": 2, "copper-plate": 40},
                "entities": [],
            },
        )
        self.assertEqual(result["selected_skill"], "produce_iron_plate")
        self.assertIn("iron plate throughput", result["blockers"])

    def test_rocket_goal_researches_automation_after_iron(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
            },
        )
        self.assertEqual(result["selected_skill"], "research_automation")

    def test_rocket_goal_researches_automation_before_electronic_circuit_targets(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 20},
                "entities": [],
            },
            production_targets={"electronic-circuit": 30.0},
        )

        self.assertEqual(result["selected_skill"], "research_automation")
        self.assertIn("automation research", result["blockers"])

    def test_rocket_goal_researches_logistics_after_automation_researched(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "research_logistics")

    def test_rocket_goal_researches_electric_drills_before_logistics_when_burner_drills_remain(self):
        result = heuristic_strategy("launch_rocket_program", burner_drill_replacement_observation())

        self.assertEqual(result["selected_skill"], "research_electric_mining_drill")
        self.assertIn("electric mining drill research", result["blockers"])
        self.assertIn("burner_mining_drill_count=1", result["evidence"])
        self.assertIn("electric_mining_drill_researched=false", result["evidence"])

    def test_rocket_goal_repairs_factory_power_before_electric_drill_research(self):
        result = heuristic_strategy("launch_rocket_program", factory_power_down_before_electric_research_observation())

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertIn("factory power", result["blockers"])
        self.assertIn("factory_power_unit=31", result["evidence"])
        self.assertIn("factory_power_recipe=automation-science-pack", result["evidence"])

    def test_reconcile_retools_belt_mall_before_repeating_emergency_power(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 80,
                "reason": "layout diagnostics",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"small-electric-pole": 23},
                "entities": [
                    {
                        "name": "boiler",
                        "unit_number": 272,
                        "position": {"x": -43.5, "y": 19},
                        "status_name": "no_fuel",
                        "inventories": {},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 318,
                        "recipe": "small-electric-pole",
                        "position": {"x": -40.5, "y": 15.5},
                        "electric_network_connected": True,
                        "inventories": {"2": {"copper-cable": 4}},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 341,
                        "recipe": "copper-cable",
                        "position": {"x": -37.5, "y": 11.5},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 342,
                        "recipe": "electronic-circuit",
                        "position": {"x": -33.5, "y": 11.5},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["target_item"], "transport-belt")
        self.assertEqual(result["target_count"], 20)
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("transport-belt mall retooling before boiler fuel route", result["blockers"])
        self.assertIn("retool_assembler_unit=318", result["evidence"])
        self.assertIn("clear_item=copper-cable", result["evidence"])

    def test_reconcile_retools_gear_assembler_without_overwriting_belt_assembler(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_power",
                "priority": 94,
                "reason": "boiler no fuel",
                "evidence": [],
                "blockers": ["factory power"],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"small-electric-pole": 23},
                "entities": [
                    {
                        "name": "boiler",
                        "unit_number": 272,
                        "position": {"x": -43.5, "y": 19},
                        "status_name": "no_fuel",
                        "inventories": {},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 318,
                        "recipe": "transport-belt",
                        "position": {"x": -40.5, "y": 15.5},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 537,
                        "recipe": "automation-science-pack",
                        "position": {"x": -36.5, "y": 15.5},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["target_item"], "transport-belt")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_power")
        self.assertIn("iron-gear assembler retooling before repeated power bootstrap", result["blockers"])
        self.assertIn("belt_assembler_unit=318", result["evidence"])
        self.assertIn("gear_retool_assembler_unit=537", result["evidence"])
        self.assertIn("preserve_transport_belt_assembler=true", result["evidence"])

    def test_rocket_goal_does_not_force_electric_drill_research_without_red_science_supply(self):
        observation = burner_drill_replacement_observation()
        observation["inventory"] = {"iron-plate": 40, "copper-plate": 20}

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertNotEqual(result["selected_skill"], "research_electric_mining_drill")

    def test_rocket_goal_bootstraps_electric_drill_mall_after_research(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            burner_drill_replacement_observation(electric_researched=True),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_electric_mining_drill_mall")
        self.assertIn("electric mining drill mall", result["blockers"])
        self.assertIn("electric_mining_drill_researched=true", result["evidence"])

    def test_rocket_goal_requests_circuit_line_after_early_red_science_research(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")

    def test_rocket_goal_prioritizes_gear_mall_plate_logistics(self):
        result = heuristic_strategy("launch_rocket_program", gear_mall_needs_plate_line_observation())

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("iron-plate logistic line to gear mall", result["blockers"])
        self.assertIn("transport_belts_available_for_mall_logistics=true", result["evidence"])

    def test_rocket_goal_keeps_gear_mall_plate_logistics_visible_without_belts(self):
        result = heuristic_strategy("launch_rocket_program", gear_mall_needs_plate_line_without_belts_observation())

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("iron-plate logistic line to gear mall", result["blockers"])
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])

    def test_rocket_goal_bootstraps_power_poles_before_long_gear_mall_relocation(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            gear_mall_needs_long_plate_line_without_belts_observation(),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertIn("small-electric-pole supply for mall relocation", result["blockers"])
        self.assertIn("source_distance_tiles=152.5", result["evidence"])
        self.assertIn("relocation_power_poles_estimate=20", result["evidence"])
        self.assertIn("small_electric_poles_available=0", result["evidence"])
        self.assertIn("small_electric_pole_deficit=20", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])

    def test_rocket_goal_relocates_long_gear_mall_plate_route_when_power_poles_ready(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("source_distance_tiles=152.5", result["evidence"])
        self.assertIn("belt_route_cost=153.0", result["evidence"])
        self.assertIn("relocation_cost=58.0", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])

    def test_rocket_goal_relocates_long_gear_mall_before_repeating_emergency_power(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}
        observation["entities"].append(
            {
                "name": "boiler",
                "unit_number": 300,
                "position": {"x": -2, "y": 0},
                "status_name": "no_fuel",
                "inventories": {},
            }
        )

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("small_electric_pole_deficit=0", result["evidence"])

    def test_rocket_goal_relocates_long_gear_mall_before_repeating_mall_power_repair(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}
        observation["entities"][1]["status_name"] = "no_power"

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("small_electric_pole_deficit=0", result["evidence"])

    def test_rocket_goal_relocates_long_gear_mall_when_power_recovery_waits_on_belt_mall(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            gear_mall_relocation_with_downstream_power_block_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("small_electric_pole_deficit=0", result["evidence"])
        self.assertIn("power_recovery_waits_on_belt_mall=true", result["evidence"])

    def test_rocket_goal_continues_inventory_rebuild_relocation_before_power_repair(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            gear_mall_inventory_rebuild_with_downstream_power_block_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("gear_assembler_unit=inventory", result["evidence"])
        self.assertIn("relocation_in_progress=true", result["evidence"])

    def test_rocket_goal_continues_target_rebuild_relocation_before_power_repair(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            gear_mall_target_rebuild_with_downstream_power_block_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("gear_assembler_unit=401", result["evidence"])
        self.assertIn("relocation_in_progress=true", result["evidence"])

    def test_rocket_goal_bootstraps_belt_mall_when_belts_are_exhausted(self):
        result = heuristic_strategy("launch_rocket_program", gear_belt_mall_needs_bootstrap_observation())

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertIn("transport-belt mall bootstrap before iron-plate logistics", result["blockers"])
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])
        self.assertIn("gear_handcraft_blocked=true", result["evidence"])
        self.assertEqual(result["factory_readiness"]["failure_root"], "belt_line_unbuildable")
        self.assertEqual(result["factory_readiness"]["repair_skill"], "bootstrap_build_item_mall")

    def test_rocket_goal_uses_belt_output_chest_as_available_mall_stock(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"][1]["position"] = {"x": 4.5, "y": 0.5}
        observation["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 300,
                "position": {"x": 6.5, "y": 0.5},
                "inventories": {"1": {"transport-belt": 48}},
            }
        )

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("transport_belts_available_for_mall_logistics=true", result["evidence"])

    def test_rocket_goal_bootstraps_power_poles_when_belt_output_pair_requires_relocation(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 300,
                "position": {"x": 6.5, "y": 0.5},
                "inventories": {"1": {"transport-belt": 48}},
            }
        )

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])
        self.assertIn("small_electric_pole_deficit=13", result["evidence"])

    def test_rocket_goal_raises_power_pole_target_above_default_when_corridor_needs_more(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"][1]["position"] = {"x": 0.5, "y": -2.5}
        observation["entities"][2]["position"] = {"x": 180.5, "y": 0.5}
        observation["inventory"] = {"iron-plate": 40, "transport-belt": 20, "small-electric-pole": 23}

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertEqual(result["target_item"], "small-electric-pole")
        self.assertEqual(result["target_count"], 24)
        self.assertIn("small_electric_poles_available=23", result["evidence"])
        self.assertIn("small_electric_pole_deficit=1", result["evidence"])

    def test_reconcile_raises_power_pole_target_above_default_when_corridor_needs_more(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"][1]["position"] = {"x": 0.5, "y": -2.5}
        observation["entities"][2]["position"] = {"x": 180.5, "y": 0.5}
        observation["inventory"] = {"iron-plate": 40, "transport-belt": 20, "small-electric-pole": 23}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_iron_plate_logistic_line_to_gear_mall",
                "priority": 50,
                "reason": "Need the long iron plate line.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertEqual(result["target_item"], "small-electric-pole")
        self.assertEqual(result["target_count"], 24)
        self.assertIn("small_electric_poles_available=23", result["evidence"])
        self.assertIn("small_electric_pole_deficit=1", result["evidence"])

    def test_rocket_goal_bootstraps_belt_mall_from_local_plate_seed_before_circuit(self):
        result = heuristic_strategy("launch_rocket_program", gear_belt_mall_has_local_plate_seed_observation())

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertIn("transport-belt mall bootstrap before iron-plate logistics", result["blockers"])
        self.assertIn("local_iron_plate_seed_source_unit=102", result["evidence"])

    def test_rocket_goal_bootstraps_when_mall_assemblers_are_unpowered(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["player"] = {"name": "server", "kind": "server", "position": {"x": 0, "y": 0}, "character_valid": False}
        for entity in observation["entities"]:
            if entity.get("name") == "assembling-machine-1":
                entity["electric_network_connected"] = False
                entity["status_name"] = "no_power"
        observation["inventory"] = {}
        observation["entities"].extend(
            [
                {
                    "name": "burner-mining-drill",
                    "unit_number": 300,
                    "position": {"x": 20, "y": 0},
                    "direction": 4,
                    "resource_name": "coal",
                    "inventories": {"1": {"coal": 3}},
                },
                {
                    "name": "boiler",
                    "unit_number": 301,
                    "position": {"x": 24, "y": 0},
                    "status_name": "no_fuel",
                    "inventories": {},
                },
            ]
        )

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["factory_readiness"]["failure_root"], "gear_mall_missing")
        self.assertIn("factory_readiness_repair_skill=bootstrap_build_item_mall", result["evidence"])

    def test_strategy_payload_includes_shared_factory_readiness(self):
        payload = make_strategy_payload("launch_rocket_program", gear_belt_mall_needs_bootstrap_observation())

        readiness = payload["factory_readiness"]
        self.assertFalse(readiness["belt_line_buildable"])
        self.assertEqual(readiness["failure_root"], "belt_line_unbuildable")
        self.assertEqual(readiness["repair_skill"], "bootstrap_build_item_mall")

    def test_rocket_goal_finishes_gear_output_logistics_before_smelting_expansion(self):
        result = heuristic_strategy("launch_rocket_program", gear_mall_output_logistics_blocked_observation())

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertIn("gear mall output logistics", result["blockers"])
        self.assertIn("gear_handcraft_blocked=true", result["evidence"])
        self.assertIn("gear_assembler_unit=146", result["evidence"])

    def test_rocket_goal_uses_short_site_input_route_for_gear_mall_plate_before_coal(self):
        result = heuristic_strategy("launch_rocket_program", gear_mall_short_site_input_route_observation())

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("iron-plate logistic line to gear mall", result["blockers"])
        self.assertIn("site_input_status=route_needed", result["evidence"])
        self.assertIn("source_distance_tiles=20.9", result["evidence"])
        self.assertIn("transport_belts_available_for_mall_logistics=true", result["evidence"])

    def test_rocket_goal_repairs_power_before_unpowered_gear_mall_logistics(self):
        observation = gear_mall_needs_plate_line_observation()
        for entity in observation["entities"]:
            if entity.get("name") == "assembling-machine-1":
                entity["status"] = 54
                entity["status_name"] = "no_power"

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertIn("gear/belt mall power", result["blockers"])

    def test_power_issue_is_prioritized_before_more_electric_expansion(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 2},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": False,
                    }
                ],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertIn("electric power network", result["blockers"])

    def test_coal_supply_is_prioritized_before_more_burner_expansion(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20, "coal": 2},
                "entities": [
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            },
        )
        self.assertEqual(result["selected_skill"], "setup_coal_supply")
        self.assertIn("automated coal fuel supply", result["blockers"])

    def test_reconcile_routes_research_automation_to_coal_supply_when_supply_missing(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "research_automation",
                "priority": 70,
                "reason": "Research Automation next.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20, "coal": 1},
                "entities": [
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
                "research": {"technologies": {"automation": {"researched": False}}},
            },
        )

        self.assertEqual(result["selected_skill"], "setup_coal_supply")
        self.assertEqual(result["guardrail_adjusted"]["from"], "research_automation")
        self.assertIn("automated coal fuel supply", result["blockers"])

    def test_reconcile_advances_past_satisfied_produce_iron_plate(self):
        # The local LLM re-picks produce_iron_plate even when iron is already stocked (plates sit in
        # the furnace output), stalling the run. Reconcile should defer to the deterministic planner.
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"iron-plate": 22, "coal": 1},
            "entities": [
                {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
            ],
            "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            "research": {"technologies": {"automation": {"researched": False}}},
        }
        result = reconcile_strategy_decision(
            {
                "selected_skill": "produce_iron_plate",
                "priority": 60,
                "reason": "Make more iron.",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )
        self.assertNotEqual(result["selected_skill"], "produce_iron_plate")
        self.assertEqual(result["guardrail_adjusted"]["from"], "produce_iron_plate")

    def test_reconcile_keeps_produce_iron_plate_when_planner_agrees(self):
        # When the planner also wants iron (none stocked), the choice must be left alone.
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {},
            "entities": [],
            "resources": [{"name": "iron-ore", "position": {"x": 6, "y": 0}, "distance": 6}],
            "research": {"technologies": {"automation": {"researched": False}}},
        }
        decision = {"selected_skill": "produce_iron_plate", "priority": 60, "source": "llm"}
        result = reconcile_strategy_decision(dict(decision), "launch_rocket_program", observation)
        if heuristic_strategy("launch_rocket_program", observation, {}).get("selected_skill") == "produce_iron_plate":
            self.assertEqual(result["selected_skill"], "produce_iron_plate")

    def test_coal_fuel_feed_is_prioritized_after_coal_supply_exists(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20, "coal": 4},
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                    {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 1, "y": 3},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    },
                ],
                "resources": [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )
        self.assertEqual(result["selected_skill"], "connect_coal_fuel_feed")
        self.assertIn("coal fuel feed route", result["blockers"])

    def test_coal_fuel_feed_waits_for_belt_automation(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20, "coal": 4},
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                    {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )
        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertIn("transport-belt automation before site links", result["blockers"])

    def test_boiler_no_fuel_prefers_coal_feed_when_belts_are_automated(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"coal": 1},
            "entities": [
                {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
                {
                    "name": "assembling-machine-1",
                    "unit_number": 22,
                    "recipe": "transport-belt",
                    "position": {"x": 1, "y": 3},
                    "electric_network_connected": True,
                    "inventories": {"1": {"transport-belt": 4}},
                },
                {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
                {"name": "lab", "unit_number": 31, "position": {"x": 10, "y": 0}, "status": 54, "status_name": "no_power", "inventories": {}},
            ],
            "resources": [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "connect_coal_fuel_feed")
        self.assertIn("boiler coal fuel feed", result["blockers"])
        self.assertIn("boiler_no_fuel=true", result["evidence"])

    def test_reconcile_promotes_power_repair_to_boiler_coal_feed_when_ready(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"coal": 1},
            "entities": [
                {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 0, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                {"name": "transport-belt", "unit_number": 21, "position": {"x": 2, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 1}}},
                {
                    "name": "assembling-machine-1",
                    "unit_number": 22,
                    "recipe": "transport-belt",
                    "position": {"x": 1, "y": 3},
                    "electric_network_connected": True,
                    "inventories": {"1": {"transport-belt": 4}},
                },
                {"name": "boiler", "unit_number": 30, "position": {"x": 8, "y": 0}, "status_name": "no_fuel", "inventories": {}},
                {"name": "lab", "unit_number": 31, "position": {"x": 10, "y": 0}, "status": 54, "status_name": "no_power", "inventories": {}},
            ],
            "resources": [{"name": "coal", "position": {"x": 0, "y": 0}, "distance": 0}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }

        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 80,
                "reason": "Improve layout.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "connect_coal_fuel_feed")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("boiler coal fuel feed", result["blockers"])

    def test_coal_supply_still_needed_when_coal_drill_has_no_output(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": -500, "y": -500}},
                "inventory": {"iron-plate": 20, "coal": 0},
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 20,
                        "position": {"x": 260, "y": -370},
                        "direction": 4,
                        "mining_target": "coal",
                        "status": 34,
                        "status_name": "waiting_for_space_in_destination",
                        "inventories": {},
                    },
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": -10, "y": -30}, "distance": 700}],
            },
        )
        self.assertEqual(result["selected_skill"], "setup_coal_supply")

    def test_coal_supply_ready_uses_mining_target_with_output_chest(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": -500, "y": -500}},
                "inventory": {"iron-plate": 20, "coal": 0},
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 20,
                        "position": {"x": 260, "y": -370},
                        "direction": 4,
                        "mining_target": "coal",
                        "status": 34,
                        "status_name": "waiting_for_space_in_destination",
                        "inventories": {},
                    },
                    {"name": "wooden-chest", "unit_number": 21, "position": {"x": 262, "y": -370}, "inventories": {"1": {"coal": 4}}},
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": -10, "y": -30}, "distance": 700}],
            },
        )
        self.assertNotEqual(result["selected_skill"], "setup_coal_supply")

    def test_unfueled_coal_drill_still_requests_coal_supply(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20, "coal": 0},
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 20,
                        "position": {"x": 8, "y": 0},
                        "direction": 4,
                        "mining_target": "coal",
                        "status": 53,
                        "status_name": "no_fuel",
                        "inventories": {},
                    },
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            },
        )
        self.assertEqual(result["selected_skill"], "setup_coal_supply")

    def test_rocket_goal_researches_logistics_after_circuit_cell_ready(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 6},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "copper-cable",
                        "electric_network_connected": True,
                    },
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": True,
                    },
                ],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "research_logistics")

    def test_electronic_circuit_bottleneck_researches_logistics_first(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 1},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    },
                },
            },
            production_targets={"electronic-circuit": 20.0},
        )
        self.assertEqual(result["selected_skill"], "research_logistics")

    def test_electronic_circuit_bottleneck_uses_automation_after_logistics(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 1},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    },
                },
            },
            production_targets={"electronic-circuit": 20.0},
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")

    def test_reconcile_promotes_hand_circuit_choice_for_rate_deficit(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "produce_electronic_circuit",
                "priority": 50,
                "reason": "Need more circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
                "heuristic_selected_skill": "research_logistics",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 20},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": True}, "logistics": {"researched": True}}},
            },
            production_targets={"electronic-circuit": 20.0},
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "produce_electronic_circuit")
        self.assertIn("assembler-based electronic circuit production", result["blockers"])

    def test_reconcile_promotes_circuit_choice_to_automation_research_before_automation(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "produce_electronic_circuit",
                "priority": 50,
                "reason": "Need circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
                "heuristic_selected_skill": "research_logistics",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 20},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": False}}},
            },
            production_targets={"electronic-circuit": 20.0},
        )

        self.assertEqual(result["selected_skill"], "research_automation")
        self.assertEqual(result["guardrail_adjusted"]["from"], "produce_electronic_circuit")
        self.assertIn("automation research", result["blockers"])

    def test_reconcile_promotes_circuit_choice_to_logistics_research_before_circuit_line(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "produce_electronic_circuit",
                "priority": 50,
                "reason": "Need circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 20},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": True}, "logistics": {"researched": False}}},
            },
            production_targets={"electronic-circuit": 20.0},
        )

        self.assertEqual(result["selected_skill"], "research_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "produce_electronic_circuit")
        self.assertIn("logistics research", result["blockers"])

    def test_reconcile_promotes_hand_circuit_even_when_isolated_cell_exists(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "produce_electronic_circuit",
                "priority": 50,
                "reason": "Need more circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 20},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "copper-cable",
                        "position": {"x": 400, "y": -400},
                        "electric_network_connected": True,
                    },
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "position": {"x": 404, "y": -400},
                        "electric_network_connected": True,
                    },
                ],
                "research": {"technologies": {"automation": {"researched": True}, "logistics": {"researched": True}}},
            },
            production_targets={"electronic-circuit": 1000.0},
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertIn("hand_crafting_not_rate_solution=true", result["evidence"])

    def test_reconcile_promotes_circuit_choice_to_gear_mall_plate_logistics(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "automate_electronic_circuit_line",
                "priority": 50,
                "reason": "Need circuits next.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_needs_plate_line_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "automate_electronic_circuit_line")
        self.assertIn("gear_handcraft_blocked=true", result["evidence"])

    def test_reconcile_bootstraps_belt_mall_before_iron_line_when_belts_exhausted(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_iron_plate_logistic_line_to_gear_mall",
                "priority": 50,
                "reason": "Need the long iron plate line.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_belt_mall_needs_bootstrap_observation(),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])
        self.assertIn("gear_handcraft_blocked=true", result["evidence"])

    def test_reconcile_routes_smelting_expansion_to_gear_output_logistics(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "expand_copper_smelting",
                "priority": 50,
                "reason": "Need more copper plates.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_output_logistics_blocked_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "expand_copper_smelting")
        self.assertIn("gear mall output logistics", result["blockers"])
        self.assertIn("gear_assembler_unit=146", result["evidence"])

    def test_reconcile_finishes_gear_belt_transfer_before_iron_line(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_iron_plate_logistic_line_to_gear_mall",
                "priority": 80,
                "reason": "Build the iron route.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_belt_mall_transfer_connection_missing_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertEqual(result["factory_readiness"]["failure_root"], "gear_belt_logistics_incomplete")
        self.assertIn("gear/belt mall transfer logistics", result["blockers"])
        self.assertIn("gear_belt_logistics_connection_ready=false", result["evidence"])

    def test_heuristic_finishes_gear_belt_transfer_before_iron_line(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            gear_belt_mall_transfer_connection_missing_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["factory_readiness"]["failure_root"], "gear_belt_logistics_incomplete")
        self.assertIn("gear_belt_logistics_connection_ready=false", result["evidence"])

    def test_reconcile_routes_smelting_expansion_to_short_gear_mall_plate_route(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "expand_copper_smelting",
                "priority": 50,
                "reason": "Need more copper plates.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_short_site_input_route_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "expand_copper_smelting")
        self.assertIn("iron-plate logistic line to gear mall", result["blockers"])
        self.assertIn("site_input_status=route_needed", result["evidence"])
        self.assertIn("gear_assembler_unit=146", result["evidence"])

    def test_reconcile_promotes_coal_supply_to_gear_belt_bootstrap_when_belts_are_exhausted(self):
        observation = gear_belt_mall_needs_bootstrap_observation()

        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_coal_supply",
                "priority": 50,
                "reason": "Need coal before more burner expansion.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "heuristic",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_coal_supply")
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])

    def test_reconcile_promotes_unseedable_iron_line_to_gear_belt_relocation(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["inventory"] = {"small-electric-pole": 20}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_iron_plate_logistic_line_to_gear_mall",
                "priority": 50,
                "reason": "Need the long iron plate line.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "heuristic",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "build_iron_plate_logistic_line_to_gear_mall")
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])

    def test_reconcile_relocates_non_logistics_long_iron_route_even_with_belt_stock(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["inventory"] = {"iron-plate": 40, "transport-belt": 20, "small-electric-pole": 80}
        observation["entities"][2]["position"] = {"x": 180.5, "y": 0.5}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_coal_supply",
                "priority": 50,
                "reason": "Need coal before more burner expansion.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "heuristic",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_coal_supply")
        self.assertIn("transport_belts_available_for_mall_logistics=true", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])

    def test_reconcile_bootstraps_belt_mall_when_relocation_layout_is_missing(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"] = [
            entity for entity in observation["entities"] if entity.get("recipe") != "transport-belt"
        ]
        observation["entities"][1]["position"] = {"x": 180.5, "y": 0.5}
        observation["inventory"] = {"iron-plate": 40, "transport-belt": 20, "small-electric-pole": 80}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_iron_plate_logistic_line_to_gear_mall",
                "priority": 50,
                "reason": "Need the long iron plate line.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["target_item"], "transport-belt")
        self.assertEqual(result["factory_readiness"]["failure_root"], "belt_mall_missing")
        self.assertIn("relocation_layout_ready=false", result["evidence"])
        self.assertIn("belt_mall_exists=false", result["evidence"])

    def test_rocket_goal_bootstraps_belt_mall_when_relocation_layout_is_missing(self):
        observation = gear_belt_mall_needs_bootstrap_observation()
        observation["entities"] = [
            entity for entity in observation["entities"] if entity.get("recipe") != "transport-belt"
        ]
        observation["entities"][1]["position"] = {"x": 180.5, "y": 0.5}
        observation["inventory"] = {"iron-plate": 40, "transport-belt": 20, "small-electric-pole": 80}

        result = heuristic_strategy("launch_rocket_program", observation)

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["factory_readiness"]["failure_root"], "belt_mall_missing")
        self.assertIn("relocation_layout_ready=false", result["evidence"])

    def test_reconcile_repairs_power_even_when_belt_mall_has_output(self):
        observation = gear_mall_needs_plate_line_observation()
        for entity in observation["entities"]:
            if entity.get("name") == "assembling-machine-1":
                entity["status"] = 54
                entity["status_name"] = "no_power"
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout planning next.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertIn("gear/belt mall power", result["blockers"])

    def test_reconcile_repairs_unpowered_gear_mall_before_coal_supply_or_iron_line(self):
        observation = gear_mall_needs_plate_line_observation()
        for entity in observation["entities"]:
            if entity.get("name") == "assembling-machine-1":
                entity["status_name"] = "no_power"
                entity["electric_network_connected"] = False

        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_coal_supply",
                "priority": 50,
                "reason": "Need coal before more burner expansion.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "heuristic",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_coal_supply")
        self.assertIn("gear/belt mall power", result["blockers"])

    def test_reconcile_repairs_power_before_circuit_when_gear_mall_unpowered(self):
        observation = gear_mall_needs_plate_line_observation()
        for entity in observation["entities"]:
            if entity.get("name") == "assembling-machine-1":
                entity["status"] = 54
                entity["status_name"] = "no_power"
        result = reconcile_strategy_decision(
            {
                "selected_skill": "automate_electronic_circuit_line",
                "priority": 50,
                "reason": "Need circuits next.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertEqual(result["guardrail_adjusted"]["from"], "automate_electronic_circuit_line")
        self.assertIn("gear/belt mall power", result["blockers"])

    def test_reconcile_blocks_circuit_when_gear_mall_plate_line_lacks_belts(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "automate_electronic_circuit_line",
                "priority": 84,
                "reason": "Need more circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_needs_plate_line_without_belts_observation(),
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "automate_electronic_circuit_line")
        self.assertIn("transport_belts_available_for_mall_logistics=false", result["evidence"])
        self.assertIn("factory_readiness_failure_root=belt_line_unbuildable", result["evidence"])

    def test_reconcile_routes_plan_site_to_power_pole_mall_for_long_gear_mall_plate_route_without_belts(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 84,
                "reason": "Layout should be compacted before more construction.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_needs_long_plate_line_without_belts_observation(),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("small-electric-pole supply for mall relocation", result["blockers"])
        self.assertIn("source_distance_tiles=152.5", result["evidence"])
        self.assertIn("small_electric_pole_deficit=20", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])

    def test_reconcile_routes_downstream_to_power_pole_mall_for_long_gear_mall_plate_route_without_belts(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "automate_electronic_circuit_line",
                "priority": 84,
                "reason": "Need more circuits.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_needs_long_plate_line_without_belts_observation(),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_power_pole_mall")
        self.assertEqual(result["guardrail_adjusted"]["from"], "automate_electronic_circuit_line")
        self.assertIn("small-electric-pole supply for mall relocation", result["blockers"])
        self.assertIn("source_distance_tiles=152.5", result["evidence"])
        self.assertIn("small_electric_pole_deficit=20", result["evidence"])
        self.assertIn("route_cost_preference=relocate_mall_to_iron_source", result["evidence"])

    def test_reconcile_routes_to_relocation_when_power_poles_are_ready(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 84,
                "reason": "Layout should be compacted before more construction.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("costed gear/belt mall relocation", result["blockers"])
        self.assertIn("source_distance_tiles=152.5", result["evidence"])

    def test_reconcile_routes_setup_power_to_relocation_when_power_window_keeps_expiring(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}
        observation["entities"].append(
            {
                "name": "boiler",
                "unit_number": 300,
                "position": {"x": -2, "y": 0},
                "status_name": "no_fuel",
                "inventories": {},
            }
        )

        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_power",
                "priority": 94,
                "reason": "Boiler is empty again.",
                "evidence": [],
                "blockers": ["factory power"],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_power")
        self.assertIn("costed gear/belt mall relocation before repeated emergency power", result["blockers"])
        self.assertIn("small_electric_pole_deficit=0", result["evidence"])

    def test_reconcile_routes_setup_power_to_relocation_when_mall_power_is_out(self):
        observation = gear_mall_needs_long_plate_line_without_belts_observation()
        observation["inventory"] = {"small-electric-pole": 20}
        observation["entities"][1]["status_name"] = "no_power"

        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_power",
                "priority": 94,
                "reason": "Belt mall has no power.",
                "evidence": [],
                "blockers": ["factory power", "gear/belt mall power"],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_power")
        self.assertIn("costed gear/belt mall relocation before repeated emergency power", result["blockers"])
        self.assertIn("small_electric_pole_deficit=0", result["evidence"])

    def test_reconcile_routes_power_block_to_relocation_when_power_needs_belt_mall(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 84,
                "reason": "Layout issue.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_relocation_with_downstream_power_block_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("costed gear/belt mall relocation before repeated emergency power", result["blockers"])
        self.assertIn("power_recovery_waits_on_belt_mall=true", result["evidence"])

    def test_reconcile_continues_inventory_rebuild_relocation_before_power_repair(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_power",
                "priority": 94,
                "reason": "Circuit block has no power.",
                "evidence": [],
                "blockers": ["factory power"],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            gear_mall_inventory_rebuild_with_downstream_power_block_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_power")
        self.assertIn("gear_assembler_unit=inventory", result["evidence"])
        self.assertIn("relocation_in_progress=true", result["evidence"])

    def test_reconcile_keeps_partial_relocation_ahead_of_electric_drill_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "research_electric_mining_drill",
                "priority": 90,
                "reason": "Burner drills remain.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            partial_gear_mall_relocation_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "research_electric_mining_drill")
        self.assertIn("gear_assembler_unit=inventory", result["evidence"])
        self.assertIn("costed gear/belt mall relocation", result["blockers"])

    def test_reconcile_finishes_partial_gear_belt_relocation_before_bootstrap_mall(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "bootstrap_build_item_mall",
                "priority": 93,
                "reason": "Belt mall is missing.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            partial_gear_belt_mall_relocation_observation(),
        )

        self.assertEqual(result["selected_skill"], "relocate_gear_belt_mall_to_iron_source")
        self.assertEqual(result["guardrail_adjusted"]["from"], "bootstrap_build_item_mall")
        self.assertIn("partial gear/belt mall relocation", result["blockers"])
        self.assertIn("gear_belt_mall_relocation_in_progress=true", result["evidence"])

    def test_reconcile_repairs_factory_power_before_electric_drill_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "research_electric_mining_drill",
                "priority": 90,
                "reason": "Burner drills remain.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            factory_power_down_before_electric_research_observation(),
        )

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertEqual(result["guardrail_adjusted"]["from"], "research_electric_mining_drill")
        self.assertIn("factory power", result["blockers"])
        self.assertIn("factory_power_recipe=automation-science-pack", result["evidence"])

    def test_reconcile_does_not_force_electric_drill_research_without_red_science_supply(self):
        observation = burner_drill_replacement_observation()
        observation["inventory"] = {"iron-plate": 40, "copper-plate": 20}

        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 90,
                "reason": "Fix science logistics first.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
        )

        self.assertEqual(result["selected_skill"], "research_logistics")
        self.assertNotIn("electric mining drill research", result["blockers"])

    def test_reconcile_promotes_fuel_dependent_expansion_to_coal_supply(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "expand_iron_smelting",
                "priority": 80,
                "reason": "Need more iron throughput.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"coal": 4},
                "entities": [
                    {"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            },
        )
        self.assertEqual(result["selected_skill"], "setup_coal_supply")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "expand_iron_smelting")
        self.assertIn("automated coal fuel supply", result["blockers"])

    def test_reconcile_promotes_fuel_dependent_expansion_to_coal_feed_link(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "setup_power",
                "priority": 80,
                "reason": "Need power.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"coal": 4},
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                    {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 1, "y": 3},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    },
                ],
                "resources": [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )
        self.assertEqual(result["selected_skill"], "connect_coal_fuel_feed")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "setup_power")
        self.assertIn("coal fuel feed route", result["blockers"])

    def test_reconcile_blocks_direct_coal_feed_before_belt_mall(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "connect_coal_fuel_feed",
                "priority": 90,
                "reason": "Connect coal to furnace.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {"coal": 4},
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 20, "position": {"x": 4, "y": 0}, "direction": 4, "inventories": {"1": {"coal": 3}}},
                    {"name": "transport-belt", "unit_number": 21, "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                ],
                "resources": [{"name": "coal", "position": {"x": 4, "y": 0}, "distance": 4}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )
        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "connect_coal_fuel_feed")
        self.assertIn("transport-belt automation before site links", result["blockers"])

    def test_reconcile_bootstraps_iron_before_item_mall_and_automation_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "bootstrap_build_item_mall",
                "priority": 50,
                "reason": "Build missing expansion items.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"burner-mining-drill": 1, "stone-furnace": 1},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": False}}},
            },
        )

        self.assertEqual(result["selected_skill"], "produce_iron_plate")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "bootstrap_build_item_mall")
        self.assertIn("basic iron supply", result["blockers"])
        self.assertIn("iron_plate_total=0", result["evidence"])

    def test_reconcile_recomputes_remote_item_mall_guardrail_for_fresh_starter(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "research_automation",
                "priority": 90,
                "reason": "Remote worker already adjusted before local code was updated.",
                "evidence": ["guardrail_adjusted_from=bootstrap_build_item_mall"],
                "blockers": ["automation research"],
                "expected_effect": "Research Automation.",
                "source": "llm",
                "guardrail_adjusted": {
                    "from": "bootstrap_build_item_mall",
                    "to": "research_automation",
                    "reason": "older remote worker guardrail",
                },
            },
            "launch_rocket_program",
            {
                "inventory": {"burner-mining-drill": 1, "stone-furnace": 1},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": False}}},
            },
        )

        self.assertEqual(result["selected_skill"], "produce_iron_plate")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "bootstrap_build_item_mall")
        self.assertIn("basic iron supply", result["blockers"])
        self.assertIn("iron_plate_total=0", result["evidence"])

    def test_reconcile_blocks_item_mall_before_automation_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "bootstrap_build_item_mall",
                "priority": 50,
                "reason": "Build missing expansion items.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": False}}},
            },
        )
        self.assertEqual(result["selected_skill"], "research_automation")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "bootstrap_build_item_mall")
        self.assertIn("automation research", result["blockers"])

    def test_reconcile_blocks_item_mall_when_existing_site_inputs_are_missing(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "bootstrap_build_item_mall",
                "priority": 50,
                "reason": "Build missing expansion items.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"electronic-circuit": 20},
                "entities": _distant_copper_source_and_science_consumer_entities(),
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "bootstrap_build_item_mall")
        self.assertIn("transport-belt automation before site input line", result["blockers"])
        self.assertIn("hand_carry_seed_risk=true", result["evidence"])

    def test_reconcile_builds_site_input_line_when_belt_automation_ready(self):
        entities = _distant_copper_source_and_science_consumer_entities()
        entities.append(
            {
                "name": "assembling-machine-1",
                "unit_number": 300,
                "recipe": "transport-belt",
                "position": {"x": 20, "y": 0},
                "electric_network_connected": True,
                "inventories": {"1": {"transport-belt": 8}},
            }
        )

        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout can be improved.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {},
                "entities": entities,
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "build_site_input_logistic_line")
        self.assertEqual(result["input_item"], "copper-plate")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("site input logistic line", result["blockers"])
        self.assertIn("transport_belt_automation_ready=true", result["evidence"])

    def test_heuristic_builds_copper_source_before_missing_source_site_route(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            _missing_copper_source_site_input_observation(),
        )

        self.assertEqual(result["selected_skill"], "expand_copper_smelting")
        self.assertIn("copper-plate source", result["blockers"])
        self.assertIn("site_input_status=missing_source", result["evidence"])
        self.assertIn("source_builder_skill=expand_copper_smelting", result["evidence"])

    def test_reconcile_redirects_site_input_route_when_source_is_missing(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "build_site_input_logistic_line",
                "priority": 50,
                "reason": "Build the copper route.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            _missing_copper_source_site_input_observation(),
        )

        self.assertEqual(result["selected_skill"], "expand_copper_smelting")
        self.assertEqual(result["guardrail_adjusted"]["from"], "build_site_input_logistic_line")
        self.assertIn("copper-plate source", result["blockers"])
        self.assertIn("site_input_status=missing_source", result["evidence"])

    def test_reconcile_promotes_layout_planning_to_actionable_target_deficit(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout can be improved.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    }
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            production_targets={"iron-plate": 90.0},
        )

        self.assertEqual(result["selected_skill"], "expand_iron_smelting")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("iron-plate", result["blockers"])
        self.assertIn("starter-usable estimated", result["reason"])
        self.assertTrue(any("iron-plate_starter_usable_per_minute" in item for item in result["evidence"]))

    def test_reconcile_promotes_diagnostic_layout_planning_to_logistics_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout diagnostics can run during idle time.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
                "heuristic_selected_skill": "research_logistics",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 50, "copper-plate": 50, "automation-science-pack": 10},
                "entities": [
                    {"name": "lab", "position": {"x": 0, "y": 0}, "electric_network_connected": True}
                ],
                "resources": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "research_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("manual_site_logistics_preemption=false", result["evidence"])

    def test_reconcile_promotes_layout_planning_to_electric_drill_research_when_burners_remain(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout diagnostics can run during idle time.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            burner_drill_replacement_observation(),
        )

        self.assertEqual(result["selected_skill"], "research_electric_mining_drill")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("electric mining drill research", result["blockers"])

    def test_reconcile_promotes_layout_planning_to_electric_drill_mall_after_research(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "research_logistics",
                "priority": 50,
                "reason": "Research logistics next.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            burner_drill_replacement_observation(electric_researched=True),
        )

        self.assertEqual(result["selected_skill"], "bootstrap_electric_mining_drill_mall")
        self.assertEqual(result["guardrail_adjusted"]["from"], "research_logistics")
        self.assertIn("electric mining drill mall", result["blockers"])

    def test_reconcile_promotes_layout_planning_to_active_logistics_without_heuristic_hint(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout diagnostics look severe.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 50, "copper-plate": 50, "automation-science-pack": 5},
                "entities": [
                    {"name": "lab", "position": {"x": 0, "y": 0}, "electric_network_connected": True}
                ],
                "resources": [],
                "research": {
                    "current": "logistics",
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    },
                },
            },
        )

        self.assertEqual(result["selected_skill"], "research_logistics")
        self.assertEqual(result["guardrail_adjusted"]["to"], "research_logistics")

    def test_reconcile_routes_confirmed_manual_site_logistics_to_belt_prerequisite(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 90,
                "reason": "Manual site logistics must be fixed.",
                "evidence": [],
                "blockers": ["site-to-site logistic line"],
                "expected_effect": "",
                "source": "llm",
                "heuristic_selected_skill": "research_logistics",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 50, "copper-plate": 50},
                "entities": _distant_copper_source_and_science_consumer_entities(),
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("transport-belt automation before site input line", result["blockers"])

    def test_reconcile_promotes_post_logistics_diagnostic_plan_to_executable_heuristic_step(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout diagnostics look severe after Logistics.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 7},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("logistics_researched=true", result["evidence"])
        self.assertIn("heuristic_selected_skill=automate_electronic_circuit_line", result["evidence"])

    def test_reconcile_promotes_post_logistics_plan_even_with_production_targets(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 78,
                "reason": "Layout diagnostics look severe after Logistics.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 7},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    }
                },
            },
            production_targets={"electronic-circuit": 45.0},
        )

        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")
        self.assertIn("heuristic_selected_skill=automate_electronic_circuit_line", result["evidence"])

    def test_reconcile_promotes_post_logistics_layout_ratio_plan_to_circuit_executor(self):
        observation = {
            "inventory": {"iron-plate": 40, "copper-plate": 40, "electronic-circuit": 7},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "recipe": "copper-cable",
                    "position": {"x": 2, "y": 2},
                    "electric_network_connected": True,
                },
                {
                    "name": "assembling-machine-1",
                    "recipe": "electronic-circuit",
                    "position": {"x": 6, "y": 2},
                    "electric_network_connected": True,
                },
            ],
            "research": {
                "technologies": {
                    "automation": {"researched": True},
                    "logistics": {"researched": True},
                }
            },
        }
        heuristic = heuristic_strategy("launch_rocket_program", observation, production_targets={"electronic-circuit": 45.0})
        self.assertEqual(heuristic["selected_skill"], "plan_factory_site")

        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 78,
                "reason": "Green circuit layout ratio should be improved.",
                "evidence": ["layout_kind=rebalance_green_circuit_ratio"],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            observation,
            production_targets={"electronic-circuit": 45.0},
        )

        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertIn("layout_executable_fallback=rebalance_green_circuit_ratio", result["evidence"])
        self.assertEqual(result["guardrail_adjusted"]["from"], "plan_factory_site")

    def test_reconcile_recomputes_stale_remote_plan_guardrail(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "expand_iron_smelting",
                "priority": 90,
                "reason": "old remote guardrail said estimated 75/min",
                "evidence": ["iron-plate_target_deficit=15.0", "iron-plate_estimated_per_minute=75.0"],
                "blockers": ["iron-plate"],
                "expected_effect": "",
                "source": "llm",
                "guardrail_adjusted": {
                    "from": "plan_factory_site",
                    "to": "expand_iron_smelting",
                    "reason": "old remote guardrail",
                },
            },
            "launch_rocket_program",
            {
                "base": {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20},
                "entities": [
                    {"name": "stone-furnace", "position": {"x": 500, "y": 0}, "inventories": {"1": {"coal": 1}, "2": {"iron-ore": 1}}},
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    },
                ],
                "resources": [{"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance_from_base": 4}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            production_targets={"iron-plate": 90.0},
        )

        self.assertEqual(result["selected_skill"], "expand_iron_smelting")
        self.assertIn("starter-usable estimated 0.0/min", result["reason"])
        self.assertNotIn("estimated 75/min", result["reason"])
        self.assertFalse(any(item == "iron-plate_target_deficit=15.0" for item in result["evidence"]))
        self.assertFalse(any(item == "iron-plate_estimated_per_minute=75.0" for item in result["evidence"]))
        self.assertTrue(any(item == "iron-plate_starter_usable_per_minute=0.0" for item in result["evidence"]))

    def test_layout_context_includes_operator_selected_site(self):
        selected = {
            "site_id": "build_item_mall:2,2",
            "kind": "build_item_mall",
            "item": "transport-belt",
        }
        layout = make_layout_improvement_context(
            {"inventory": {}, "entities": []},
            selected_improvement_site=selected,
        )

        self.assertEqual(layout["selected_improvement_site"]["site_id"], "build_item_mall:2,2")
        self.assertEqual(layout["recommended_skill"], "plan_factory_site")
        self.assertEqual(layout["opportunities"][0]["kind"], "operator_selected_site")
        self.assertEqual(layout["opportunities"][0]["site_id"], "build_item_mall:2,2")

    def test_reconcile_prioritizes_iron_deficit_before_copper_deficit(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Layout can be improved.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "base": {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    }
                ],
                "resources": [
                    {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance_from_base": 4},
                    {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance_from_base": 8},
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            production_targets={"copper-plate": 70.0, "iron-plate": 90.0},
        )

        self.assertEqual(result["selected_skill"], "expand_iron_smelting")
        self.assertIn("iron-plate", result["reason"])

    def test_reconcile_keeps_layout_planning_when_starter_resource_is_remote(self):
        result = reconcile_strategy_decision(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "reason": "Remote starter sites need layout repair.",
                "evidence": [],
                "blockers": [],
                "expected_effect": "",
                "source": "llm",
            },
            "launch_rocket_program",
            {
                "base": {"spawn_position": {"x": 0, "y": 0}, "anchor_position": {"x": 0, "y": 0}},
                "inventory": {"iron-plate": 20},
                "entities": [],
                "resources": [
                    {"name": "iron-ore", "position": {"x": 480, "y": -290}, "distance_from_base": 560}
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            production_targets={"iron-plate": 90.0},
        )

        self.assertEqual(result["selected_skill"], "plan_factory_site")
        self.assertNotIn("guardrail_adjusted", result)

    def test_copper_target_bottleneck_uses_direct_bootstrap_before_belt_automation(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 1},
                "entities": [],
            },
            production_targets={"copper-plate": 45.0},
        )
        self.assertEqual(result["selected_skill"], "produce_copper_plate")

    def test_copper_target_bottleneck_expands_copper_smelting_after_belt_automation(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 1},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {"1": {"transport-belt": 2}},
                    }
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            production_targets={"copper-plate": 45.0},
        )
        self.assertEqual(result["selected_skill"], "expand_copper_smelting")

    def test_normalize_rejects_unknown_skill(self):
        result = normalize_strategy_response({"selected_skill": "teleport_to_rocket", "priority": 100})
        self.assertEqual(result["selected_skill"], "launch_rocket_program")

    def test_normalize_uses_llm_justification_as_reason(self):
        result = normalize_strategy_response(
            {
                "selected_skill": "plan_factory_site",
                "priority": 50,
                "justification": "Remote starter sites need layout repair before expansion.",
            }
        )
        self.assertEqual(result["reason"], "Remote starter sites need layout repair before expansion.")

    def test_catalog_exposes_llm_scope(self):
        catalog = skill_catalog_payload()
        self.assertTrue(any(item["name"] == "produce_electronic_circuit" for item in catalog))
        self.assertTrue(any(item["name"] == "build_belt_smelting_line" for item in catalog))
        self.assertTrue(any(item["name"] == "setup_coal_supply" for item in catalog))
        self.assertTrue(any(item["name"] == "setup_stone_supply" for item in catalog))
        self.assertTrue(any(item["name"] == "connect_coal_fuel_feed" for item in catalog))
        self.assertTrue(any(item["name"] == "expand_copper_smelting" for item in catalog))
        self.assertTrue(any(item["name"] == "research_automation" for item in catalog))
        self.assertTrue(any(item["name"] == "automate_electronic_circuit_line" for item in catalog))
        self.assertTrue(any(item["name"] == "bootstrap_build_item_mall" for item in catalog))
        self.assertTrue(any(item["name"] == "bootstrap_power_pole_mall" for item in catalog))
        self.assertTrue(any(item["name"] == "research_electric_mining_drill" for item in catalog))
        self.assertTrue(any(item["name"] == "bootstrap_electric_mining_drill_mall" for item in catalog))
        self.assertTrue(any(item["name"] == "relocate_gear_belt_mall_to_iron_source" for item in catalog))
        self.assertTrue(any(item["name"] == "build_iron_plate_logistic_line_to_gear_mall" for item in catalog))
        self.assertTrue(any(item["name"] == "build_site_input_logistic_line" for item in catalog))
        self.assertTrue(any(item["name"] == "research_logistics" for item in catalog))
        self.assertTrue(any(item["name"] == "build_starter_defense" for item in catalog))
        self.assertTrue(any(item["name"] == "build_rail_supply_line" for item in catalog))
        self.assertTrue(any(item["name"] == "plan_oil_outpost" for item in catalog))
        self.assertEqual(
            next(item for item in catalog if item["name"] == "produce_electronic_circuit")["executor"],
            "ElectronicCircuitSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "bootstrap_build_item_mall")["executor"],
            "BuildItemMallSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "bootstrap_power_pole_mall")["executor"],
            "BuildItemMallSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "research_electric_mining_drill")["executor"],
            "ResearchTechnologySkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "bootstrap_electric_mining_drill_mall")["executor"],
            "BuildItemMallSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "setup_coal_supply")["executor"],
            "CoalSupplySkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "connect_coal_fuel_feed")["executor"],
            "CoalFuelFeedSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "build_iron_plate_logistic_line_to_gear_mall")["executor"],
            "IronPlateLogisticLineToGearMallSkill",
        )
        self.assertEqual(
            next(item for item in catalog if item["name"] == "build_site_input_logistic_line")["executor"],
            "SiteInputLogisticLineSkill",
        )
        self.assertTrue(all("llm_scope" in item for item in catalog))

    def test_strategy_payload_exposes_spatial_planning_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {},
                "entities": [{"name": "stone-furnace", "position": {"x": 10, "y": 4}}],
                "resources": [{"name": "iron-ore", "position": {"x": 120, "y": 0}, "amount": 1000}],
            },
        )
        self.assertIn("spatial_planning", payload)
        self.assertIn("site_selection", payload["spatial_planning"])
        self.assertIn("rail_network", payload["spatial_planning"])
        self.assertEqual(
            payload["spatial_planning"]["rail_network"]["planning_inputs"]["rail_candidate_distance_tiles"],
            160,
        )
        self.assertEqual(
            payload["spatial_planning"]["rail_network"]["planning_inputs"]["known_remote_resources"][0]["name"],
            "iron-ore",
        )

    def test_layout_context_marks_long_handed_inserter_as_layout_capability(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {"speed-module": 1, "assembling-machine-2": 1, "beacon": 1},
                "entities": [],
                "resources": [],
                "research": {"technologies": {"long-inserters": {"researched": True}}},
            },
        )

        capability = payload["layout_improvement"]["layout_capabilities"]["inserters"]["long-handed-inserter"]
        self.assertTrue(capability["available"])
        self.assertTrue(capability["researched"])
        self.assertIn("denser", capability["layout_impact"])
        self.assertTrue(payload["layout_improvement"]["layout_capabilities"]["modules"]["speed-module"]["available"])
        self.assertTrue(payload["layout_improvement"]["layout_capabilities"]["machines"]["assembling-machine-2"]["available"])
        self.assertTrue(payload["layout_improvement"]["layout_capabilities"]["beacons"]["beacon"]["available"])
        self.assertTrue(payload["layout_improvement"]["layout_capabilities"]["rerank_trigger"])

    def test_layout_context_uses_recipe_unlock_when_technology_name_is_missing(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {},
                "entities": [],
                "resources": [],
                "research": {"technologies": {}},
                "recipe_unlocks": {
                    "long-handed-inserter": {"enabled": True},
                    "assembling-machine-2": {"enabled": True},
                    "beacon": {"enabled": True},
                },
            },
        )

        capabilities = payload["layout_improvement"]["layout_capabilities"]
        self.assertTrue(capabilities["inserters"]["long-handed-inserter"]["available"])
        self.assertTrue(capabilities["inserters"]["long-handed-inserter"]["recipe_unlocked"])
        self.assertFalse(capabilities["inserters"]["long-handed-inserter"]["researched"])
        self.assertTrue(capabilities["machines"]["assembling-machine-2"]["available"])
        self.assertTrue(capabilities["beacons"]["beacon"]["available"])
        self.assertTrue(capabilities["rerank_trigger"])

    def test_layout_context_counts_higher_tier_assembler_recipe_automation(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {},
                "entities": [
                    {
                        "name": "assembling-machine-2",
                        "recipe": "long-handed-inserter",
                        "position": {"x": 0, "y": 0},
                        "electric_network_connected": True,
                        "inventories": {},
                    }
                ],
                "resources": [],
                "research": {"technologies": {}},
            },
        )

        capability = payload["layout_improvement"]["layout_capabilities"]["inserters"]["long-handed-inserter"]
        self.assertTrue(capability["available"])
        self.assertTrue(capability["automated"])
        self.assertTrue(payload["layout_improvement"]["layout_capabilities"]["rerank_trigger"])

    def test_strategy_payload_exposes_build_item_supply_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100, "copper-plate": 100},
                "entities": [],
                "resources": [],
                "recipe_unlocks": {"long-handed-inserter": {"enabled": True}},
            },
        )
        supply = payload["build_item_supply"]
        self.assertEqual(supply["recommended_skill"], "bootstrap_build_item_mall")
        items = {item["item"]: item for item in supply["items"]}
        self.assertTrue(items["transport-belt"]["needs_mall"])
        self.assertTrue(items["long-handed-inserter"]["needs_mall"])
        self.assertTrue(items["assembling-machine-1"]["needs_mall"])
        self.assertTrue(items["electric-mining-drill"]["needs_mall"])

    def test_normalize_preserves_build_item_target(self):
        result = normalize_strategy_response(
            {
                "selected_skill": "bootstrap_build_item_mall",
                "target_item": "long-handed-inserter",
                "target_count": 12,
                "priority": 80,
            }
        )

        self.assertEqual(result["selected_skill"], "bootstrap_build_item_mall")
        self.assertEqual(result["target_item"], "long-handed-inserter")
        self.assertEqual(result["target_count"], 12)

    def test_normalize_preserves_site_input_target(self):
        result = normalize_strategy_response(
            {
                "selected_skill": "build_site_input_logistic_line",
                "item": "copper-plate",
                "priority": 80,
            }
        )

        self.assertEqual(result["selected_skill"], "build_site_input_logistic_line")
        self.assertEqual(result["input_item"], "copper-plate")

    def test_strategy_payload_exposes_automation_policy_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {},
                "entities": _distant_copper_source_and_science_consumer_entities(),
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
        )
        policy = payload["automation_policy"]
        self.assertEqual(policy["recommended_skill"], "plan_factory_site")
        self.assertTrue(any(link["item"] == "copper-plate" for link in policy["route_needed_links"]))

    def test_heuristic_prioritizes_site_logistics_over_repeated_hand_carry_after_automation(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 50, "copper-plate": 50},
                "entities": _distant_copper_source_and_science_consumer_entities(),
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    }
                },
            },
        )
        self.assertEqual(result["selected_skill"], "build_gear_belt_mall_logistics")
        self.assertIn("transport-belt automation before site input line", result["blockers"])

    def test_heuristic_builds_site_input_line_after_belt_automation_ready(self):
        entities = _distant_copper_source_and_science_consumer_entities()
        entities.append(
            {
                "name": "assembling-machine-1",
                "unit_number": 300,
                "recipe": "transport-belt",
                "position": {"x": 20, "y": 0},
                "electric_network_connected": True,
                "inventories": {"1": {"transport-belt": 8}},
            }
        )

        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 50, "copper-plate": 50},
                "entities": entities,
                "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}}],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": True},
                    }
                },
            },
        )

        self.assertEqual(result["selected_skill"], "build_site_input_logistic_line")
        self.assertEqual(result["input_item"], "copper-plate")
        self.assertIn("site input logistic line", result["blockers"])
        self.assertIn("transport_belt_automation_ready=true", result["evidence"])

    def test_strategy_payload_exposes_research_daisy_chain_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "inventory": {"automation-science-pack": 10},
                "entities": [
                    {"name": "lab", "position": {"x": 0, "y": 0}, "electric_network_connected": True, "inventories": {}},
                    {"name": "lab", "position": {"x": 4, "y": 0}, "electric_network_connected": True, "inventories": {}},
                    {"name": "inserter", "position": {"x": 2, "y": 0}, "inventories": {}},
                ],
                "resources": [],
            },
        )
        research = payload["research_planning"]
        self.assertEqual(research["lab_count"], 2)
        self.assertEqual(research["powered_lab_count"], 2)
        self.assertTrue(any("daisy chains" in item for item in research["layout_patterns"]))
        self.assertTrue(any(site["kind"] == "research_lab_block" for site in research["lab_sites"]))

    def test_strategy_payload_exposes_enemy_threat_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {},
                "entities": [],
                "resources": [],
                "enemies": [
                    {"name": "small-biter", "type": "unit", "distance": 22, "position": {"x": 20, "y": 0}},
                    {"name": "biter-spawner", "type": "unit-spawner", "distance": 140, "position": {"x": 140, "y": 0}},
                ],
            },
        )
        self.assertEqual(payload["threats"]["enemy_count"], 2)
        self.assertEqual(payload["threats"]["danger_level"], "critical")
        self.assertEqual(payload["threats"]["nearest_enemy"]["name"], "small-biter")
        self.assertEqual(payload["threats"]["nearest_spawner"]["name"], "biter-spawner")
        self.assertEqual(payload["threats"]["armed_gun_turret_count"], 0)

    def test_critical_enemy_threat_requests_defense_skill(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [],
                "enemies": [{"name": "small-biter", "type": "unit", "distance": 20}],
            },
        )
        self.assertEqual(result["selected_skill"], "build_starter_defense")
        self.assertIn("enemy threat", result["blockers"])

    def test_recent_enemy_damage_requests_defense_skill(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [],
                "enemies": [],
                "factory_events": [
                    {
                        "tick": 500,
                        "action": "destroyed",
                        "entity": "transport-belt",
                        "cause": "small-biter",
                        "cause_force": "enemy",
                    }
                ],
            },
        )
        self.assertEqual(result["selected_skill"], "build_starter_defense")
        self.assertIn("recent_enemy_damage_count=1", result["evidence"])

    def test_nearby_spawner_without_pollution_does_not_force_starter_defense(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [],
                "enemies": [{"name": "biter-spawner", "type": "unit-spawner", "distance": 80, "pollution": 0}],
            },
        )
        self.assertNotEqual(result["selected_skill"], "build_starter_defense")

    def test_rocket_program_bootstraps_iron_before_target_bottlenecks(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {},
                "entities": [],
                "resources": [],
                "enemies": [],
            },
            production_targets={"copper-plate": 70.0, "iron-plate": 90.0},
        )
        self.assertEqual(result["selected_skill"], "produce_iron_plate")

    def test_armed_starter_turret_prevents_repeated_defense_loop(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [
                    {
                        "name": "gun-turret",
                        "unit_number": 10,
                        "position": {"x": 0, "y": 0},
                        "inventories": {"1": {"firearm-magazine": 10}},
                    }
                ],
                "enemies": [{"name": "small-biter", "type": "unit", "distance": 70}],
            },
        )
        self.assertNotEqual(result["selected_skill"], "build_starter_defense")


def _distant_copper_source_and_science_consumer_entities():
    return [
        {
            "name": "burner-mining-drill",
            "unit_number": 100,
            "position": {"x": 0, "y": 0},
            "mining_target": "copper-ore",
            "inventories": {"1": {"coal": 3}},
        },
        {"name": "transport-belt", "unit_number": 101, "position": {"x": 2, "y": 0}, "inventories": {}},
        {"name": "transport-belt", "unit_number": 102, "position": {"x": 3, "y": 0}, "inventories": {}},
        {"name": "burner-inserter", "unit_number": 103, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 2}}},
        {
            "name": "stone-furnace",
            "unit_number": 104,
            "position": {"x": 5, "y": 0},
            "inventories": {"1": {"coal": 3}, "2": {"copper-ore": 1}, "3": {"copper-plate": 1}},
        },
        {
            "name": "assembling-machine-1",
            "unit_number": 200,
            "recipe": "automation-science-pack",
            "position": {"x": 180, "y": 0},
            "electric_network_connected": True,
            "inventories": {},
        },
    ]


def _missing_copper_source_site_input_observation():
    return {
        "inventory": {"iron-plate": 20, "copper-plate": 0},
        "entities": [
            {
                "name": "assembling-machine-1",
                "unit_number": 200,
                "recipe": "automation-science-pack",
                "position": {"x": 180, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 300,
                "recipe": "transport-belt",
                "position": {"x": 20, "y": 0},
                "electric_network_connected": True,
                "inventories": {"1": {"transport-belt": 8}},
            },
        ],
        "resources": [{"name": "copper-ore", "position": {"x": 0, "y": 0}, "distance_from_base": 20}],
        "research": {
            "technologies": {
                "automation": {"researched": True},
                "logistics": {"researched": True},
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
