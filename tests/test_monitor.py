import unittest

from factorio_ai.monitor import (
    estimate_bottlenecks,
    estimate_factory_sites,
    estimate_logistics_links,
    estimate_net_rates,
    estimate_power_networks,
    estimate_production,
    estimate_threats,
    estimate_throughput_constraints,
    production_target_status,
    recent_damage_events,
    recent_factory_events,
    recipe_machine_ratio,
    summarize_factory,
)
from factorio_ai.monitor import ConsumptionEstimate, ProductionEstimate


class MonitorTests(unittest.TestCase):
    def test_target_status_detects_satisfied_and_deficit_items(self):
        production = [ProductionEstimate("iron-plate", 30.0, 2, 0.7, [])]
        status = production_target_status({"iron-plate": 20.0, "copper-plate": 10.0}, production)
        self.assertFalse(status["all_satisfied"])
        rows = {item["item"]: item for item in status["items"]}
        self.assertTrue(rows["iron-plate"]["satisfied"])
        self.assertEqual(rows["copper-plate"]["deficit_per_minute"], 10.0)

    def test_bottleneck_includes_target_deficit(self):
        bottlenecks = estimate_bottlenecks(
            "launch_rocket_program",
            {"inventory": {}, "entities": []},
            production=[ProductionEstimate("iron-plate", 5.0, 1, 0.7, [])],
            production_targets={"iron-plate": 20.0},
        )
        self.assertEqual(bottlenecks[0].item, "iron-plate")
        self.assertIn("target deficit", bottlenecks[0].reason)

    def test_net_rate_subtracts_consumption(self):
        net = estimate_net_rates(
            [ProductionEstimate("iron-plate", 30.0, 2, 0.7, [])],
            [ConsumptionEstimate("iron-plate", 12.0, 1, 0.7, [])],
        )
        self.assertEqual(net["iron-plate"], 18.0)

    def test_estimates_powered_assembler_recipe_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": True,
                        "inventories": {},
                    }
                ]
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("electronic-circuit", by_item)
        self.assertEqual(by_item["electronic-circuit"].per_minute, 60.0)

    def test_belt_line_resource_uses_mining_target_when_resource_list_is_remote(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 1,
                        "position": {"x": 0, "y": 0},
                        "direction": 4,
                        "mining_target": "copper-ore",
                        "status": 34,
                        "status_name": "waiting_for_space_in_destination",
                        "inventories": {},
                    },
                    {"name": "transport-belt", "unit_number": 2, "position": {"x": 2, "y": 0}, "direction": 4},
                    {"name": "transport-belt", "unit_number": 3, "position": {"x": 3, "y": 0}, "direction": 4},
                    {
                        "name": "burner-inserter",
                        "unit_number": 4,
                        "position": {"x": 4, "y": 0},
                        "inventories": {"1": {"coal": 1}},
                    },
                    {
                        "name": "stone-furnace",
                        "unit_number": 5,
                        "position": {"x": 5, "y": 0},
                        "inventories": {"1": {"coal": 2}, "3": {"copper-plate": 1}},
                    },
                ],
                "resources": [{"name": "iron-ore", "position": {"x": 100, "y": 100}, "distance": 140}],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("copper-ore", by_item)
        self.assertIn("copper-plate", by_item)
        self.assertNotIn("iron-plate", by_item)
        links = estimate_logistics_links(
            {
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 1,
                        "position": {"x": 0, "y": 0},
                        "direction": 4,
                        "mining_target": "copper-ore",
                        "status": 34,
                        "status_name": "waiting_for_space_in_destination",
                        "inventories": {},
                    },
                    {"name": "transport-belt", "unit_number": 2, "position": {"x": 2, "y": 0}, "direction": 4},
                    {"name": "transport-belt", "unit_number": 3, "position": {"x": 3, "y": 0}, "direction": 4},
                    {
                        "name": "burner-inserter",
                        "unit_number": 4,
                        "position": {"x": 4, "y": 0},
                        "inventories": {"1": {"coal": 1}},
                    },
                    {
                        "name": "stone-furnace",
                        "unit_number": 5,
                        "position": {"x": 5, "y": 0},
                        "inventories": {"1": {"coal": 2}, "3": {"copper-plate": 1}},
                    },
                ],
                "resources": [{"name": "iron-ore", "position": {"x": 100, "y": 100}, "distance": 140}],
            }
        )
        ore_links = [link for link in links if link.item == "copper-ore"]
        self.assertTrue(ore_links)
        self.assertNotIn("missing_source", ore_links[0].from_site)

    def test_unfueled_miner_is_not_counted_as_producer(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "burner-mining-drill",
                        "unit_number": 1,
                        "position": {"x": 0, "y": 0},
                        "direction": 4,
                        "mining_target": "coal",
                        "status": 53,
                        "status_name": "no_fuel",
                        "inventories": {},
                    }
                ],
                "resources": [],
            }
        )
        self.assertNotIn("coal", {item.item for item in estimates})

    def test_electronic_circuit_ratio_uses_recipe_speed(self):
        ratio = recipe_machine_ratio("copper-cable", "electronic-circuit", "copper-cable")
        self.assertEqual(ratio["producer"], 3.0)
        self.assertEqual(ratio["consumer"], 2.0)
        self.assertEqual(ratio["producer_per_minute"], 120.0)
        self.assertEqual(ratio["consumer_demand_per_minute"], 180.0)

    def test_throughput_constraints_detect_cable_deficit(self):
        constraints = estimate_throughput_constraints(
            {
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
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": True,
                    },
                ]
            }
        )
        cable = next(item for item in constraints if item.item == "copper-cable")
        self.assertEqual(cable.available_per_minute, 120.0)
        self.assertEqual(cable.required_per_minute, 360.0)
        self.assertIn("ratio is 3:2", " ".join(cable.notes))

    def test_estimates_power_networks_by_connected_network_id(self):
        networks = estimate_power_networks(
            {
                "entities": [
                    {
                        "name": "steam-engine",
                        "electric_network_connected": True,
                        "electric_network_id": 7,
                    },
                    {
                        "name": "assembling-machine-1",
                        "electric_network_connected": True,
                        "electric_network_id": 7,
                    },
                    {
                        "name": "electric-mining-drill",
                        "electric_network_connected": True,
                        "electric_network_id": 8,
                    },
                    {
                        "name": "assembling-machine-1",
                        "electric_network_connected": False,
                    },
                ]
            }
        )
        by_id = {item.network_id: item for item in networks}
        self.assertEqual(by_id["7"].generation_kw, 900.0)
        self.assertEqual(by_id["7"].demand_kw, 75.0)
        self.assertEqual(by_id["7"].status, "ok")
        self.assertEqual(by_id["8"].status, "unknown_generation")
        self.assertEqual(by_id["unconnected"].unconnected_consumers, 1)

    def test_throughput_constraints_include_power_shortage(self):
        constraints = estimate_throughput_constraints(
            {
                "entities": [
                    {"name": "solar-panel", "electric_network_connected": True, "electric_network_id": 1},
                    {"name": "electric-furnace", "electric_network_connected": True, "electric_network_id": 1},
                ]
            }
        )
        electricity = next(item for item in constraints if item.item == "electricity")
        self.assertEqual(electricity.available_per_minute, 60.0)
        self.assertEqual(electricity.required_per_minute, 180.0)
        self.assertIn("insufficient_generation", electricity.bottleneck)

    def test_ignores_unpowered_assembler_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": False,
                        "inventories": {},
                    }
                ]
            }
        )
        self.assertEqual(estimates, [])

    def test_estimates_complete_belt_iron_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("iron-plate", by_item)
        self.assertEqual(by_item["iron-plate"].per_minute, 18.75)
        self.assertNotIn("copper-plate", by_item)

    def test_ignores_unfueled_complete_belt_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 4, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {}},
                    {"name": "stone-furnace", "position": {"x": 9, "y": 0}, "inventories": {}},
                ],
                "resources": [],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertNotIn("iron-plate", by_item)

    def test_ignores_unfueled_stone_furnace_inventory_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "stone-furnace",
                        "inventories": {"2": {"iron-ore": 1}, "3": {"iron-plate": 1}},
                    }
                ]
            }
        )
        self.assertEqual(estimates, [])

    def test_estimates_complete_belt_copper_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 10, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 11, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 12, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "position": {"x": 13, "y": 0}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [{"name": "copper-ore", "position": {"x": 8, "y": 0}}],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("copper-plate", by_item)
        self.assertEqual(by_item["copper-plate"].per_minute, 18.75)
        self.assertNotIn("iron-plate", by_item)

    def test_factory_sites_include_burner_upgrade_notes(self):
        sites = estimate_factory_sites(
            {
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "unit_number": 2, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [{"name": "iron-ore", "position": {"x": 4, "y": 0}}],
            }
        )
        smelting = next(item for item in sites if item.kind == "plate_smelting_line")
        self.assertEqual(smelting.item, "iron-plate")
        self.assertEqual(smelting.automation_level, "burner-bootstrap")
        self.assertIn("electric mining drills", " ".join(smelting.notes))

    def test_factory_sites_group_adjacent_smelting_lines(self):
        sites = estimate_factory_sites(
            {
                "entities": [
                    {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "unit_number": 2, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "burner-mining-drill", "unit_number": 3, "position": {"x": 4, "y": 3}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 3}, "direction": 4, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 3}, "direction": 4, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 3}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "unit_number": 4, "position": {"x": 9, "y": 3}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [
                    {"name": "iron-ore", "position": {"x": 4, "y": 0}},
                    {"name": "iron-ore", "position": {"x": 4, "y": 3}},
                ],
            }
        )
        smelting_sites = [item for item in sites if item.kind == "plate_smelting_line" and item.item == "iron-plate"]
        self.assertEqual(len(smelting_sites), 1)
        self.assertIn("running", smelting_sites[0].status)
        self.assertIn("burner-mining-drill x2", smelting_sites[0].machines)
        self.assertIn("stone-furnace x2", smelting_sites[0].machines)
        self.assertIn("grouped", smelting_sites[0].notes[0])

    def test_factory_sites_group_adjacent_assembler_cells(self):
        sites = estimate_factory_sites(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 20,
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 21,
                        "position": {"x": 2, "y": 5},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                ],
                "resources": [],
            }
        )
        assembler_sites = [item for item in sites if item.kind == "assembler_cell"]
        self.assertEqual(len(assembler_sites), 1)
        self.assertEqual(assembler_sites[0].status, "unconfigured")
        self.assertIn("assembling-machine-1 x2", assembler_sites[0].machines)

    def test_factory_sites_group_lab_daisy_chain_block(self):
        sites = estimate_factory_sites(
            {
                "entities": [
                    {
                        "name": "lab",
                        "unit_number": 10,
                        "position": {"x": 0, "y": 0},
                        "electric_network_connected": True,
                        "inventories": {"1": {"automation-science-pack": 1}},
                    },
                    {
                        "name": "lab",
                        "unit_number": 11,
                        "position": {"x": 4, "y": 0},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                    {
                        "name": "inserter",
                        "unit_number": 12,
                        "position": {"x": 2, "y": 0},
                        "electric_network_connected": True,
                        "inventories": {},
                    },
                ]
            }
        )
        lab_sites = [item for item in sites if item.kind == "research_lab_block"]
        self.assertEqual(len(lab_sites), 1)
        self.assertEqual(lab_sites[0].automation_level, "daisy-chain")
        self.assertIn("lab x2", lab_sites[0].machines)

    def test_logistics_links_connect_producer_and_consumer_sites(self):
        observation = {
            "entities": [
                {"name": "boiler", "unit_number": 10, "position": {"x": 12, "y": 10}, "inventories": {"1": {"coal": 1}}},
                {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "stone-furnace", "unit_number": 2, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "burner-mining-drill", "unit_number": 3, "position": {"x": 20, "y": 0}, "inventories": {"1": {"coal": 1}}},
            ],
            "resources": [
                {"name": "iron-ore", "position": {"x": 4, "y": 0}},
                {"name": "coal", "position": {"x": 20, "y": 0}},
            ],
        }
        links = estimate_logistics_links(observation)
        iron_link = next(item for item in links if item.item == "iron-ore" and "plate_smelting_line" in item.to_site)
        self.assertIn("mining_patch", iron_link.from_site)
        self.assertEqual(iron_link.status, "route_observed")
        coal_smelting = [
            item
            for item in links
            if item.item == "coal" and "mining_patch" in item.from_site and "plate_smelting_line" in item.to_site
        ]
        coal_power = [
            item
            for item in links
            if item.item == "coal" and "mining_patch" in item.from_site and "power" in item.to_site
        ]
        self.assertEqual(len(coal_smelting), 1)
        self.assertEqual(len(coal_power), 1)
        self.assertEqual(coal_smelting[0].status, "route_observed")
        self.assertNotIn("manual", {item.kind for item in links})

    def test_coal_route_observed_requires_nearby_source(self):
        observation = {
            "entities": [
                {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "transport-belt", "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                {"name": "transport-belt", "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
                {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "direction": 12, "inventories": {"1": {"coal": 1}}},
                {"name": "stone-furnace", "unit_number": 2, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "burner-mining-drill", "unit_number": 3, "position": {"x": 200, "y": 0}, "inventories": {"1": {"coal": 1}}},
            ],
            "resources": [
                {"name": "iron-ore", "position": {"x": 4, "y": 0}},
                {"name": "coal", "position": {"x": 200, "y": 0}},
            ],
        }
        links = estimate_logistics_links(observation)
        coal_smelting = next(
            item
            for item in links
            if item.item == "coal" and "mining_patch" in item.from_site and "smelting" in item.to_site
        )
        self.assertEqual(coal_smelting.status, "route_needed")

    def test_logistics_links_group_adjacent_sites_into_one_link(self):
        observation = {
            "entities": [
                {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "transport-belt", "position": {"x": 6, "y": 0}, "direction": 4, "inventories": {}},
                {"name": "transport-belt", "position": {"x": 7, "y": 0}, "direction": 4, "inventories": {}},
                {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "stone-furnace", "unit_number": 2, "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                {"name": "burner-mining-drill", "unit_number": 3, "position": {"x": 4, "y": 3}, "inventories": {"1": {"coal": 1}}},
                {"name": "transport-belt", "position": {"x": 6, "y": 3}, "direction": 4, "inventories": {}},
                {"name": "transport-belt", "position": {"x": 7, "y": 3}, "direction": 4, "inventories": {}},
                {"name": "burner-inserter", "position": {"x": 8, "y": 3}, "inventories": {"1": {"coal": 1}}},
                {"name": "stone-furnace", "unit_number": 4, "position": {"x": 9, "y": 3}, "inventories": {"1": {"coal": 1}}},
            ],
            "resources": [
                {"name": "iron-ore", "position": {"x": 4, "y": 0}},
                {"name": "iron-ore", "position": {"x": 4, "y": 3}},
            ],
        }
        links = [
            item
            for item in estimate_logistics_links(observation)
            if item.item == "iron-ore" and "plate_smelting_line" in item.to_site
        ]
        self.assertEqual(len(links), 1)
        self.assertIn("group:iron-ore", links[0].from_site)
        self.assertIn("group:iron-plate", links[0].to_site)

    def test_summary_exposes_factory_sites_and_logistics_links(self):
        summary = summarize_factory(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 20,
                        "recipe": "transport-belt",
                        "position": {"x": 2, "y": 2},
                        "electric_network_connected": True,
                        "inventories": {},
                    }
                ],
                "resources": [],
            }
        )
        self.assertIn("factory_sites", summary)
        self.assertIn("logistics_links", summary)
        self.assertEqual(summary["factory_sites"][0]["kind"], "build_item_mall")

    def test_recent_factory_events_normalizes_player_builds(self):
        events = recent_factory_events(
            {
                "factory_events": [
                    {
                        "tick": 123,
                        "action": "built",
                        "actor": {"kind": "player", "name": "r1jae"},
                        "entity": "assembling-machine-1",
                        "unit_number": 42,
                        "position": {"x": 10, "y": 20},
                        "distance": 5,
                    }
                ]
            }
        )
        self.assertEqual(events[0]["actor"], "r1jae")
        self.assertEqual(events[0]["action"], "built")
        self.assertEqual(events[0]["entity"], "assembling-machine-1")

    def test_recent_damage_events_include_enemy_cause(self):
        events = recent_damage_events(
            {
                "factory_events": [
                    {
                        "tick": 220,
                        "action": "destroyed",
                        "actor": {"kind": "script", "name": "script"},
                        "entity": "transport-belt",
                        "cause": "small-biter",
                        "cause_force": "enemy",
                        "damage": 8,
                        "health": 0,
                        "position": {"x": 12, "y": 8},
                    },
                    {"tick": 200, "action": "built", "entity": "stone-furnace"},
                ]
            }
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "destroyed")
        self.assertEqual(events[0]["cause"], "small-biter")

    def test_threat_summary_recommends_defense_after_enemy_damage(self):
        threats = estimate_threats(
            {
                "entities": [],
                "enemies": [
                    {"name": "biter-spawner", "type": "unit-spawner", "distance": 120, "pollution": 15.5},
                ],
                "factory_events": [
                    {
                        "tick": 300,
                        "action": "damaged",
                        "entity": "stone-furnace",
                        "cause": "small-biter",
                        "cause_force": "enemy",
                    }
                ],
            }
        )
        self.assertEqual(threats.danger_level, "high")
        self.assertEqual(threats.recent_damage_count, 1)
        self.assertIn("run build_starter_defense", " ".join(threats.recommended_actions))

    def test_nearby_spawner_without_pollution_is_medium_not_active_attack(self):
        threats = estimate_threats(
            {
                "entities": [],
                "enemies": [
                    {"name": "biter-spawner", "type": "unit-spawner", "distance": 80, "pollution": 0},
                ],
                "factory_events": [],
            }
        )
        self.assertEqual(threats.danger_level, "medium")
        self.assertNotIn("run build_starter_defense", " ".join(threats.recommended_actions))


if __name__ == "__main__":
    unittest.main()
