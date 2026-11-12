from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent.diagnostics import (
    production_rates,
    construction_budget,
    smelting_throughput_bounds,
    factory_constraints,
)


class RateEvidenceTests(unittest.TestCase):
    def test_lifetime_counter_requires_two_timed_samples_and_rollback_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            o = {"world_id": "w", "tick": 10000, "production": {"iron-plate": {"produced": 5000, "consumed": 4000}}}
            self.assertEqual(production_rates(root, o)["status"], "sampling")
            o = {"world_id": "w", "tick": 13600, "production": {"iron-plate": {"produced": 5060, "consumed": 4030}}}
            r = production_rates(root, o)
            self.assertEqual(r["rates_per_game_minute"]["iron-plate"], {"produced": 60, "consumed": 30})
            o["tick"] = 9000
            r = production_rates(root, o)
            self.assertEqual(r["status"], "sampling")
            self.assertEqual(r["reset_reason"], "world_rollback")

    def test_pending_mining_plan_is_included_once_in_known_project_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drill = {"name": "electric-mining-drill", "position": {"x": 0.5, "y": 0.5}}
            plan = {"entities": [drill]}
            (root / "mining-outposts.json").write_text(
                json.dumps({"world_id": "w", "banks": [{"plan": plan}, {"plan": plan}]})
            )
            catalog = SimpleNamespace(
                recipe_for_product=lambda _: {
                    "products": [{"amount": 1}],
                    "ingredients": [{"name": "iron-plate", "amount": 23}],
                }
            )
            obs = {"world_id": "w", "inventory": {}, "entities": []}
            result = construction_budget(root, obs, catalog)
            self.assertEqual(result["remaining_placement_items"], {"electric-mining-drill": 1})
            self.assertEqual(result["base_material_upper_bound"], {"iron-plate": 23})

    def test_independent_lane_limits_do_not_credit_unused_capacity_on_other_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bank = {
                "resource": "iron-ore",
                "product": "iron-plate",
                "producer_rows": {
                    "left": [{"name": "stone-furnace", "position": {"x": i * 4, "y": 0}} for i in range(10)],
                    "right": [{"name": "stone-furnace", "position": {"x": 0, "y": 5}}],
                },
            }
            (root / "smelting-district.json").write_text(json.dumps({"world_id": "w", "banks": [bank]}))
            (root / "mining-outposts.json").write_text(
                json.dumps(
                    {
                        "world_id": "w",
                        "banks": [{"plan": {"resource": "iron-ore"}, "ready": True, "nominal_rate_per_minute": 1000}],
                    }
                )
            )
            catalog = SimpleNamespace(
                entities={"stone-furnace": {"crafting_speed": 1}},
                recipes={"iron-plate": {"energy": 1, "products": [{"name": "iron-plate", "amount": 1}]}},
            )
            obs = {
                "world_id": "w",
                "belt_lane_observation": {
                    "ports": [{"item": "iron-plate", "nominal_unstacked_items_per_second_per_lane": 7.5}]
                },
            }
            row = smelting_throughput_bounds(root, obs, catalog)["banks"][0]
            self.assertEqual(row["independent_producer_capacity_per_lane"], {"left": 600, "right": 60})
            self.assertEqual(row["partial_upper_bound_per_minute"], 510)

    def test_mixed_furnace_types_and_exhausted_miners_change_independent_upper_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bank = {
                "resource": "iron-ore",
                "product": "iron-plate",
                "producer_rows": {
                    "left": [{"name": "steel-furnace", "position": {"x": 0, "y": 0}}],
                    "right": [{"name": "stone-furnace", "position": {"x": 0, "y": 5}}],
                },
            }
            miners = [{"name": "electric-mining-drill", "position": {"x": i * 4, "y": 20}} for i in range(2)]
            (root / "smelting-district.json").write_text(json.dumps({"world_id": "w", "banks": [bank]}))
            (root / "mining-outposts.json").write_text(
                json.dumps(
                    {
                        "world_id": "w",
                        "banks": [
                            {
                                "plan": {"resource": "iron-ore", "entities": miners},
                                "ready": True,
                                "nominal_rate_per_minute": 60,
                            }
                        ],
                    }
                )
            )
            catalog = SimpleNamespace(
                entities={"stone-furnace": {"crafting_speed": 1}, "steel-furnace": {"crafting_speed": 2}},
                recipes={"iron-plate": {"energy": 3.2, "products": [{"name": "iron-plate", "amount": 1}]}},
            )
            obs = {
                "world_id": "w",
                "entities": [
                    dict(
                        e,
                        energy=100,
                        electric_network_connected=True,
                        status_name="no_minable_resources" if i == 0 else "working",
                    )
                    for i, e in enumerate(miners)
                ],
            }
            row = smelting_throughput_bounds(root, obs, catalog)["banks"][0]
            self.assertEqual(row["independent_producer_capacity_per_lane"], {"left": 37.5, "right": 18.75})
            self.assertEqual(row["installed_nominal_ore_supply"], 60)
            self.assertEqual(row["observed_remaining_nominal_ore_supply"], 30)
            self.assertEqual(row["partial_upper_bound_per_minute"], 30)

    def test_electrical_peak_excludes_burner_thermal_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = SimpleNamespace(
                entities={
                    "boiler": {"electric": False, "energy_usage": 30000},
                    "steam-engine": {"electric": True, "energy_production": 15000},
                    "assembling-machine-1": {"electric": True, "energy_usage": 1250},
                }
            )
            obs = {
                "world_id": "w",
                "entities": [
                    {"name": "boiler", "status_name": "no_fuel"},
                    {"name": "steam-engine"},
                    {"name": "assembling-machine-1", "status_name": "low_power"},
                    {"name": "stone-furnace", "status_name": "no_ingredients"},
                    {"name": "stone-furnace", "status_name": "full_output"},
                ],
            }
            result = factory_constraints(Path(directory), obs, catalog)
            self.assertEqual(result["installed_consumer_peak_kw"], 75)
            self.assertEqual(result["installed_generation_nameplate_kw"], 900)
            self.assertEqual(result["live_low_or_no_power_entities"], {"assembling-machine-1": 1})
            self.assertEqual(result["live_no_fuel_entities"], {"boiler": 1})
            self.assertEqual(result["live_waiting_for_ingredients_entities"], {"stone-furnace": 1})
            self.assertEqual(result["live_full_output_entities"], {"stone-furnace": 1})


if __name__ == "__main__":
    unittest.main()
