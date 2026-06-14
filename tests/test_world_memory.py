import tempfile
import time
import unittest
from pathlib import Path

from factorio_ai.world_memory import (
    load_world_map_memory,
    merge_world_map_memory_into_observation,
    planning_sites_from_memory,
    summarize_world_map_memory,
    update_world_map_memory,
    world_map_memory_path,
)


class WorldMemoryTests(unittest.TestCase):
    def test_world_memory_stores_sparse_feature_graph_not_tile_grid(self):
        observation = {
            "ok": True,
            "tick": 100,
            "resources": [
                {"unit_number": 1, "name": "iron-ore", "amount": 500, "position": {"x": 10, "y": 10}, "distance": 3},
                {"unit_number": 2, "name": "iron-ore", "amount": 700, "position": {"x": 13, "y": 12}, "distance": 4},
                {"unit_number": 3, "name": "copper-ore", "amount": 900, "position": {"x": 120, "y": 35}, "distance": 70},
            ],
            "entities": [
                {"unit_number": 10, "name": "stone-furnace", "type": "furnace", "force": "player", "position": {"x": 0, "y": 0}},
                {"unit_number": 11, "name": "burner-mining-drill", "type": "mining-drill", "force": "player", "position": {"x": 4, "y": 1}},
                {"unit_number": 12, "name": "tree-01", "type": "tree", "position": {"x": 1, "y": 1}},
            ],
            "power_sites": [
                {
                    "distance": 20,
                    "distance_from_agent": 5,
                    "layout": {
                        "offshore_pump": {
                            "name": "offshore-pump",
                            "position": {"x": 55.5, "y": -814.5},
                            "direction": 0,
                        }
                    },
                }
            ],
            "lab_sites": [],
            "automation_sites": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            memory = update_world_map_memory(
                runtime,
                observation,
                include_planning_sites=True,
                source="test-full-scan",
            )
            loaded = load_world_map_memory(runtime)
            summary = summarize_world_map_memory(loaded)
            raw_text = world_map_memory_path(runtime).read_text(encoding="utf-8")

        self.assertEqual(memory["encoding"], "sparse_feature_graph")
        self.assertEqual(loaded["resources"]["encoding"], "cluster_bounds")
        self.assertEqual(loaded["resources"]["resource_counts"], {"copper-ore": 1, "iron-ore": 2})
        self.assertEqual(len(loaded["resources"]["patches"]), 2)
        iron_patch = next(row for row in loaded["resources"]["patches"] if row["name"] == "iron-ore")
        self.assertEqual(iron_patch["sample_count"], 2)
        self.assertEqual(len(loaded["known_water_sites"]), 1)
        self.assertEqual(loaded["factory"]["zone_count"], 1)
        self.assertGreaterEqual(loaded["spatial_index"]["feature_count"], 4)
        self.assertNotIn("unit_number", raw_text)
        self.assertEqual(summary["candidate_counts"]["power_sites"], 1)

    def test_fresh_memory_can_supply_planning_site_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            memory = update_world_map_memory(
                runtime,
                {
                    "ok": True,
                    "tick": 22,
                    "resources": [],
                    "entities": [],
                    "power_sites": [{"layout": {"offshore_pump": {"position": {"x": 1, "y": 2}}}}],
                    "lab_sites": [],
                    "automation_sites": [],
                },
                include_planning_sites=True,
                source="test-full-scan",
            )
            observation = merge_world_map_memory_into_observation(
                {"ok": True, "tick": 24, "power_sites": []},
                memory,
                max_age_seconds=60,
            )
            cache = planning_sites_from_memory(memory)

        self.assertEqual(observation["planning_sites_cached_from_tick"], 22)
        self.assertEqual(len(observation["power_sites"]), 1)
        self.assertEqual(cache["tick"], 22)
        self.assertIn("power_sites", cache)

    def test_stale_memory_does_not_supply_planning_sites(self):
        memory = {
            "planning_sites_updated_at_epoch": time.time() - 600,
            "planning_sites": {"power_sites": [{"layout": {}}]},
        }
        observation = merge_world_map_memory_into_observation(
            {"ok": True, "tick": 1, "power_sites": []},
            memory,
            max_age_seconds=60,
        )

        self.assertEqual(observation["power_sites"], [])
        self.assertIn("world_map_memory", observation)


if __name__ == "__main__":
    unittest.main()
