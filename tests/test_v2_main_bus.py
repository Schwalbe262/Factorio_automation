import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.main_bus import (
    BusMaterial,
    ConsumerTap,
    SourceMerge,
    capacity_report,
    plan_main_bus,
    preflight_main_bus,
)


class MainBusTests(unittest.TestCase):
    def materials(self, copper_lines=(112.5,)):
        return (
            BusMaterial("iron-plate", -11.5, (225,), reserved_lines=2),
            BusMaterial("copper-plate", -3.5, copper_lines, reserved_lines=2),
        )

    def plan(self, taps=(), **options):
        return plan_main_bus(self.materials(), taps, north_y=-118.5, south_y=-12.5, port_x=12.5, **options)

    def test_two_consumers_share_one_real_trunk_per_material(self):
        taps = (
            ConsumerTap("green-iron", "iron-plate", -76.5, demand_per_minute=192.5),
            ConsumerTap("red-iron", "iron-plate", -96.5, demand_per_minute=72),
        )
        plan = self.plan(taps)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(len(plan["trunks"]), 2)
        self.assertEqual(plan["required_items"]["splitter"], 2)
        self.assertEqual(
            {e["position"]["x"] for e in plan["entities"] if e["name"] == "transport-belt" and e["direction"] == 0},
            {-11.5, -3.5},
        )
        self.assertEqual(plan["capacity"]["materials"]["iron-plate"]["lines"][0]["producer_deficit_per_minute"], 39.5)
        self.assertEqual(plan, json.loads(json.dumps(plan)))

    def test_priority_uses_native_splitter_fields_with_north_relative_sides(self):
        taps = tuple(
            ConsumerTap(priority, "iron-plate", -30.5 - i * 12, priority)
            for i, priority in enumerate(("critical", "normal", "surplus"))
        )
        plan = self.plan(taps)
        splitters = [row["splitter"] for row in plan["stations"]]
        self.assertEqual([e["splitter_input_priority"] for e in splitters], ["left"] * 3)
        self.assertEqual([e["splitter_output_priority"] for e in splitters], ["right", "none", "left"])
        self.assertTrue(all(e["direction"] == 0 and e["_width"] == 2 for e in splitters))
        self.assertEqual(plan["priority_inversions"], [])

    def test_later_critical_tap_does_not_claim_global_preemption(self):
        plan = self.plan(
            (
                ConsumerTap("mall", "iron-plate", -30.5, "surplus"),
                ConsumerTap("science", "iron-plate", -60.5, "critical"),
            )
        )
        self.assertEqual(plan["priority_inversions"], [{"earlier": "mall", "later": "science"}])
        self.assertIn("local", plan["priority_scope"])

    def test_extra_source_merge_has_explicit_input_and_single_output_priority(self):
        for priority in ("left", "right", "none"):
            plan = self.plan(merges=(SourceMerge("iron-bank-2", "iron-plate", -40.5, priority),))
            self.assertTrue(plan["ok"])
            splitter = plan["stations"][0]["splitter"]
            self.assertEqual(splitter["splitter_input_priority"], priority)
            self.assertEqual(splitter["splitter_output_priority"], "left")
            port = next(p for p in plan["ports"] if p["name"] == "iron-bank-2")
            self.assertEqual(
                (port["direction"], port["facing"], port["position"]), ("input", 0, {"x": -10.5, "y": -39.5})
            )
            self.assertNotIn(entity("transport-belt", -10.5, -41.5, 0), plan["entities"])

    def test_taps_cross_other_material_and_reserved_future_columns_underground(self):
        plan = self.plan((ConsumerTap("iron", "iron-plate", -76.5),))
        pairs = plan["underground_pairs"]
        self.assertEqual(len(pairs), 2)
        self.assertEqual({tuple(p["input"]) for p in pairs}, {(-8.5, -77.5), (-4.5, -77.5)})
        positions = {(e["position"]["x"], e["position"]["y"]) for e in plan["entities"]}
        self.assertNotIn((-7.5, -77.5), positions)  # reserved future copper line, still unbuilt
        self.assertIn((-3.5, -77.5), positions)  # existing copper trunk stays on the surface
        self.assertEqual(plan["reservations"][1]["future_columns"], [-7.5])

    def test_activation_of_reserved_parallel_line_preserves_existing_entities_and_ports(self):
        taps = (ConsumerTap("iron", "iron-plate", -76.5), ConsumerTap("copper", "copper-plate", -79.5))
        before = self.plan(taps)
        after = plan_main_bus(self.materials((112.5, 112.5)), taps, north_y=-118.5, south_y=-12.5, port_x=12.5)
        self.assertTrue(after["ok"], after)
        self.assertTrue(all(e in after["entities"] for e in before["entities"]))
        self.assertTrue(all(p in after["ports"] for p in before["ports"]))
        self.assertEqual(after["capacity"]["materials"]["copper-plate"]["supply_ceiling_per_minute"], 225)

    def test_consumer_ports_match_compact_green_without_moving_bus_trunks(self):
        plan = plan_main_bus(
            self.materials(),
            (ConsumerTap("green-copper", "copper-plate", -79.5), ConsumerTap("green-iron", "iron-plate", -76.5)),
            north_y=-118.5,
            south_y=-12.5,
            port_x=40.5,
        )
        ports = {p["name"]: p for p in plan["ports"]}
        self.assertEqual(ports["green-copper"]["position"], {"x": 40.5, "y": -80.5})
        self.assertEqual(ports["green-iron"]["position"], {"x": 40.5, "y": -77.5})
        self.assertEqual({t["x"] for t in plan["trunks"]}, {-11.5, -3.5})
        self.assertTrue(all(b["route_metrics"]["turns"] == 0 for b in plan["branches"]))

    def test_complete_plan_footprints_do_not_overlap(self):
        plan = self.plan(
            (ConsumerTap("iron", "iron-plate", -76.5), ConsumerTap("copper", "copper-plate", -79.5)),
            merges=(SourceMerge("bank2", "iron-plate", -40.5),),
        )
        builder = FactoryBuilder.__new__(FactoryBuilder)
        boxes = [builder._occupied_by_plan([e]) for e in plan["entities"]]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))

    def test_foreign_belt_is_preserved_by_real_paired_underground_trunk_crossing(self):
        foreign = [entity("transport-belt", -11.5, -48.5, 4)]
        plan = self.plan(foreign_entities=foreign)
        self.assertTrue(plan["ok"], plan)
        self.assertNotIn(foreign[0]["position"], [e["position"] for e in plan["entities"]])
        self.assertIn(
            {"input": [-11.5, -47.5], "output": [-11.5, -49.5], "direction": 0, "span": 2}, plan["underground_pairs"]
        )

    def test_tap_at_foreign_crossing_fails_without_maze_fallback(self):
        plan = self.plan(
            (ConsumerTap("conflict", "iron-plate", -48.5),),
            foreign_entities=[entity("transport-belt", -11.5, -48.5, 4)],
        )
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "bus_station_intersects_underground_crossing")

    def test_capacity_keeps_production_belt_and_lane_limits_separate(self):
        material = BusMaterial("iron-plate", 0.5, (1800, 100), reserved_lines=2)
        report = capacity_report((material,), (ConsumerTap("high", "iron-plate", -10.5, demand_per_minute=1500),))[
            "materials"
        ]["iron-plate"]
        self.assertEqual(report["supply_ceiling_per_minute"], 1000)
        self.assertEqual(report["lines"][0]["producer_deficit_per_minute"], 0)
        self.assertEqual(report["lines"][0]["belt_deficit_per_minute"], 600)
        self.assertEqual(report["lines"][0]["single_lane_capacity_per_minute"], 450)

    def test_reserved_lines_are_not_built_or_counted_as_production(self):
        plan = self.plan()
        self.assertEqual(plan["capacity"]["materials"]["iron-plate"]["active_lines"], 1)
        self.assertEqual(plan["capacity"]["materials"]["iron-plate"]["supply_ceiling_per_minute"], 225)
        self.assertFalse(any(e["position"]["x"] in {-15.5, -7.5} for e in plan["entities"]))

    def test_invalid_specs_fail_before_native_queries(self):
        with self.assertRaises(ValueError):
            plan_main_bus(
                (
                    BusMaterial("iron", 0.5, (225,), reserved_lines=1),
                    BusMaterial("copper", 4.5, (112.5,), reserved_lines=2),
                ),
                (),
                north_y=-20.5,
                south_y=0.5,
                port_x=12.5,
            )
        with self.assertRaises(ValueError):
            self.plan((ConsumerTap("missing-item", "steel-plate", -30.5),))
        with self.assertRaises(ValueError):
            self.plan((ConsumerTap("too-close", "iron-plate", -30.5), ConsumerTap("collision", "copper-plate", -31.5)))
        with self.assertRaises(ValueError):
            self.plan((ConsumerTap("inactive", "iron-plate", -30.5, line=1),))

    def test_preflight_is_read_only_and_passes_exact_virtual_removal_units(self):
        plan = self.plan()
        game, builder, obs = object(), object(), {"world_id": "w"}
        with patch("factorio_agent.main_bus.preflight", return_value={"ok": True}) as native:
            self.assertEqual(preflight_main_bus(game, builder, obs, plan, ignored_units=(123, 456)), {"ok": True})
            native.assert_called_once_with(game, builder, obs, plan["entities"], ignored_units=(123, 456))
        self.assertEqual(
            preflight_main_bus(game, builder, obs, {"ok": False, "reason": "blocked"}),
            {"ok": False, "reason": "blocked"},
        )


if __name__ == "__main__":
    unittest.main()
