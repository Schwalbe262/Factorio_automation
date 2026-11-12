import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.smelting import SmeltingDistrict, furnace_bank, merge_entities, smelting_site_anchors
from factorio_agent.jobs import StarterExecutors
from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import _plan


class FurnaceBankGeometryTests(unittest.TestCase):
    def test_dual_lane_feed_and_plate_output_do_not_overlap_machines(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        for resource, count in (("iron-ore", 4), ("copper-ore", 2)):
            bank = furnace_bank(24, -48, count, resource)
            boxes = [geometry._occupied_by_plan([e]) for e in bank["entities"]]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertEqual(bank["required_items"]["stone-furnace"], count)
            self.assertEqual(len(bank["ports"]), 3)

    def test_conflicting_shared_belt_orientation_is_not_silently_overwritten(self):
        with self.assertRaises(ValueError):
            merge_entities([entity("transport-belt", 0.5, 0.5, 4)], [entity("transport-belt", 0.5, 0.5, 0)])

    def test_output_port_is_at_reserved_boundary_for_every_furnace_count(self):
        plans = [furnace_bank(-40, -32, count, "iron-ore") for count in range(1, 7)]
        self.assertTrue(all(p["ports"][-1] == plans[0]["ports"][-1] for p in plans))
        for before, after in zip(plans, plans[1:]):
            for placed in before["entities"]:
                self.assertIn(placed, after["entities"])

    def test_automatic_furnace_is_excluded_from_starter_upkeep_and_old_fuel_is_recovered(self):
        with TemporaryDirectory() as directory:
            routine = SmeltingDistrict.__new__(SmeltingDistrict)
            routine.path = Path(directory) / "district.json"
            furnace = entity("stone-furnace", -40, -32)
            routine.state = {"world_id": "w", "plan": {"entities": [furnace]}}
            owned = dict(furnace, unit_number=1, inventory={"coal": 8}, products_finished=0, remaining_burning_fuel=0)
            starter = dict(entity("stone-furnace", 10, -60), unit_number=2, inventory={})
            obs = {"world_id": "w", "tick": 100, "entities": [owned, starter]}
            executor = StarterExecutors.__new__(StarterExecutors)
            executor.builder = SimpleNamespace(owns_automated_burner=lambda e: False, _sync=lambda obs: None)
            executor.smelting = routine
            self.assertEqual(executor.starter_observation(obs)["entities"], [starter])
            executor.bootstrap = SimpleNamespace(next_action=lambda filtered: filtered)
            self.assertEqual(executor.step("starter", obs)["entities"], [starter])
            action = routine.clean_feed_baseline(obs)
            self.assertEqual((action["type"], action["inventory"], action["count"]), ("take", "fuel", 8))
            self.assertNotIn("automatic_feed_baseline", routine.state)
            owned["inventory"] = {}
            self.assertIsNone(routine.clean_feed_baseline(obs))
            self.assertTrue(routine.state["automatic_feed_baseline"]["fuel_inventory_empty"])


class SmeltingPreparationTests(unittest.TestCase):
    def test_source_relative_fallback_is_bounded_grid_and_preserves_legacy_preference(self):
        sources = {
            n: {"position": {"x": x, "y": y}}
            for n, x, y in (("iron", 74.5, 18.5), ("copper", -38.5, 50.5), ("coal", -42.5, 26.5))
        }
        before = smelting_site_anchors(sources)
        moved = smelting_site_anchors(
            {
                k: {"position": {"x": p["position"]["x"] + 160, "y": p["position"]["y"] + 160}}
                for k, p in sources.items()
            }
        )
        self.assertEqual(before[:9], moved[:9])
        self.assertNotEqual(before[9:], moved[9:])
        self.assertLessEqual(len(before), 25)
        self.assertTrue(all(x % 16 == 0 and y % 16 == 0 for x, y in before[9:]))

    def prepare(self, root, whole):
        c = SimpleNamespace(
            fingerprint="c",
            recipes={"iron-plate": {"energy": 3.2}, "copper-plate": {"energy": 3.2}},
            entities={"stone-furnace": {"crafting_speed": 1}},
        )
        mining = {
            "world_id": "w",
            "banks": [
                {
                    "ready": True,
                    "nominal_rate_per_minute": 60,
                    "plan": {
                        "resource": r,
                        "ports": [
                            {
                                "item": r,
                                "kind": "item",
                                "direction": "output",
                                "facing": 4,
                                "position": {"x": -0.5, "y": i + 0.5},
                            }
                        ],
                    },
                }
                for i, r in enumerate(("iron-ore", "copper-ore", "coal"))
            ],
        }
        (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
        (root / "pcb-board.json").write_text(
            json.dumps({"world_id": "w", "catalog_fingerprint": "c", "blocks": {}}), encoding="utf-8"
        )
        game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "legal": [1]})
        )
        builder = FactoryBuilder.__new__(FactoryBuilder)
        # This was the old failure: raw placement still sees an approved tree.
        builder.can_place = Mock(
            return_value={"ok": False, "blocked": [{"name": "transport-belt", "reason": "terrain_or_entity_collision"}]}
        )
        routine = SmeltingDistrict(game, None, builder, c)
        with (
            patch(
                "factorio_agent.smelting.belt_connection",
                return_value=_plan([], [], underground_pairs=[], route_metrics={"length": 5}),
            ),
            patch("factorio_agent.smelting.power_connection", return_value=_plan([], [])),
            patch("factorio_agent.smelting.preflight", return_value=whole) as native,
        ):
            result = routine.prepare({"world_id": "w", "technologies": {"logistics": True}, "entities": []})
        return result, builder, native

    def test_whole_native_preflight_retains_exact_tree_clearance_for_normal_paid_building(self):
        with TemporaryDirectory() as directory:
            tree = {"name": "tree-05", "position": {"x": 4.5, "y": 8.5}, "unit_number": 9}
            result, builder, native = self.prepare(Path(directory), {"ok": True, "clear_obstacles": [tree]})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plan"]["clear_obstacles"], [tree])
            self.assertEqual(result["plan"]["clearance_world_id"], "w")
            self.assertEqual(len(result["routes"]), 5)
            builder.can_place.assert_not_called()
            native.assert_called_once()
            self.assertFalse((Path(directory) / "smelting-district.json").exists())

    def test_resource_or_foreign_obstruction_still_rejects_and_exposes_bounded_stage_diagnostics(self):
        with TemporaryDirectory() as directory:
            result, _, _ = self.prepare(
                Path(directory), {"ok": False, "reason": "new_entity_over_resource_or_mining_reservation"}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["evidence"]["failure_stage_counts"], {"whole_preflight": 1})
            self.assertEqual(
                result["evidence"]["last_failure_reason"], "new_entity_over_resource_or_mining_reservation"
            )


if __name__ == "__main__":
    unittest.main()
