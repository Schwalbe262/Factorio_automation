"""Plate throughput growth: bottleneck stage, one paid step at a time, slack vs critical nodes."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factorio_agent import progression as pg
from factorio_agent import throughput_growth as tg
from factorio_agent.jobs import CAPABILITIES

# The live shape at minute ~36 of eval-2644599892: iron 2 drills (60/min) under
# 4 furnaces (75/min), a 1778-plate bill, no continuous taps.
IRON = tg.PlateFacts(
    plate="iron-plate",
    resource="iron-ore",
    tap_rate=0.0,
    bill=1778.0,
    furnace_nominal=75.0,
    furnace_each=18.75,
    per_side=2,
    capacity_slots=12,
    ore_nominal=60.0,
    drill_rate=30.0,
    pairs_built=1,
    pair_slots=3,
    belt_cap=900.0,
    ore_step_cost={"iron-plate": 52.0, "copper-plate": 10.0, "wood": 1.0},
    ore_step_entities=8,
    furnace_step_cost={"iron-plate": 25.0, "copper-plate": 7.0, "stone": 10.0, "wood": 1.0},
    furnace_step_entities=14,
    stocks={"copper-plate": (50.0, 37.5), "iron-plate": (0.0, 72.0), "stone": (50.0, 15.0)},
)
# Copper: 2 furnaces (37.5/min) under 2 drills (60/min).
COPPER = replace(
    IRON,
    plate="copper-plate",
    resource="copper-ore",
    bill=380.0,
    furnace_nominal=37.5,
    per_side=1,
    stocks={"iron-plate": (100.0, 72.0), "stone": (50.0, 15.0)},
)


def receipt(*rows):
    return {"throughput_growth": {"plates": list(rows)}, "tick": 200000}


class BottleneckTests(unittest.TestCase):
    def test_ore_limited_by_nameplate(self):
        self.assertEqual(tg.bottleneck(IRON)[0], "ore")

    def test_furnace_limited_by_nameplate(self):
        self.assertEqual(tg.bottleneck(COPPER)[0], "furnaces")

    def test_furnaces_idle_for_ore_mean_ore_even_when_the_nameplate_says_furnaces(self):
        facts = replace(COPPER, furnaces=2, furnaces_idle_for_ore=2)
        self.assertEqual(tg.bottleneck(facts)[0], "ore")

    def test_backed_up_drills_mean_furnaces(self):
        facts = replace(IRON, drills=2, drills_backed=2)
        self.assertEqual(tg.bottleneck(facts)[0], "furnaces")

    def test_full_furnace_output_is_demand_limited_and_grows_nothing(self):
        row = tg.decide(replace(IRON, furnaces=4, furnaces_output_full=1))
        self.assertEqual(row["bottleneck"], "demand")
        self.assertFalse(row["grow"])

    def test_idle_furnaces_with_backed_drills_blame_the_ore_belt(self):
        row = tg.decide(replace(IRON, furnaces=4, furnaces_idle_for_ore=4, drills=2, drills_backed=2))
        self.assertEqual(row["bottleneck"], "transport")
        self.assertFalse(row["grow"])

    def test_equal_stages_grow_ore_first(self):
        facts = replace(IRON, furnace_nominal=60.0, furnace_each=15.0)
        row = tg.decide(facts)
        self.assertEqual(row["bottleneck"], "balanced")
        self.assertEqual(row["capability"], "mining_outpost")


class DecisionTests(unittest.TestCase):
    def test_ore_limited_iron_adds_one_drill_pair(self):
        row = tg.decide(IRON)
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["capability"], "mining_outpost")
        self.assertEqual(row["parameters"], {"resource": "iron-ore", "rate_per_minute": 120.0})
        # Valued with the furnace slot the pair enables (75 -> 112.5), never stacked with it.
        self.assertEqual(row["step"]["gain_per_minute"], 52.5)
        self.assertEqual(len(row["step"]["valued_with"]), 1)
        self.assertGreater(row["times"]["saving_seconds"], tg.MIN_SAVING_SECONDS)

    def test_furnace_limited_copper_adds_one_slot_per_row_within_the_ore(self):
        row = tg.decide(COPPER)
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["capability"], "smelting_capacity")
        # smelting_capacity refuses rate > connected ore; 60 still builds per_side 2.
        self.assertEqual(row["parameters"], {"resource": "copper-ore", "rate_per_minute": 60.0})

    def test_never_grows_furnaces_when_they_starve_for_ore(self):
        row = tg.decide(replace(COPPER, furnaces=2, furnaces_idle_for_ore=2))
        self.assertNotEqual(row.get("capability"), "smelting_capacity")

    def test_supply_covering_demand_with_margin_grows_nothing(self):
        row = tg.decide(replace(IRON, bill=500.0))  # 50/min < 60 / 1.1
        self.assertFalse(row["grow"])
        self.assertIn("covered", row["reason"])

    def test_continuous_taps_count_as_demand(self):
        row = tg.decide(replace(IRON, bill=0.0, tap_rate=150.0))
        self.assertEqual(row["demand"]["per_minute"], 150.0)
        self.assertTrue(row["grow"], row["reason"])

    def test_time_comparison_rejects_a_step_that_does_not_pay(self):
        # Demand just above capacity: the step's cost and build time eat the saving.
        row = tg.decide(replace(COPPER, bill=420.0, furnace_step_entities=60))
        self.assertFalse(row["grow"])
        self.assertIn("waiting is as fast", row["reason"])

    def test_unfundable_input_waits(self):
        row = tg.decide(replace(IRON, stocks={}))
        self.assertFalse(row["grow"])
        self.assertIn("cannot be funded", row["reason"])

    def test_full_furnace_bank_names_the_next_unit_and_grows_nothing(self):
        row = tg.decide(replace(COPPER, per_side=12, furnace_nominal=450.0, ore_nominal=600.0, bill=8000.0))
        self.assertFalse(row["grow"])
        self.assertIn("paired_bank_full_next_growth_is_another_bank", row["reason"])
        self.assertEqual(row["next_unit"], "another_bank")

    def test_full_mining_bank_grows_through_a_connected_module(self):
        row = tg.decide(replace(IRON, pairs_built=3))
        self.assertTrue(row["grow"], row["reason"])
        self.assertIn("another 2-drill module", row["step"]["unit"])

    def test_settling_after_a_step_grows_nothing(self):
        row = tg.decide(replace(IRON, settling="mining_outpost for iron-ore finished 100 ticks ago"))
        self.assertFalse(row["grow"])
        self.assertIn("settling", row["reason"])

    def test_receipt_carries_the_numbers(self):
        row = tg.decide(IRON)
        for key in ("demand", "supply", "signals", "bottleneck", "step", "times", "reason"):
            self.assertIn(key, row)
        json.dumps(row)


class SettlingTests(unittest.TestCase):
    def test_open_or_recent_growth_job_of_the_resource_settles(self):
        job = {"capability": "smelting_capacity", "parameters": {"resource": "iron-ore"}, "status": "running"}
        self.assertTrue(tg._settling("iron-ore", [job], 1000))
        done = dict(job, status="succeeded", last_tick=1000)
        self.assertTrue(tg._settling("iron-ore", [done], 1000 + tg.SETTLE_TICKS - 1))
        self.assertFalse(tg._settling("iron-ore", [done], 1000 + tg.SETTLE_TICKS))
        self.assertFalse(tg._settling("copper-ore", [job], 1000))


class NodeTests(unittest.TestCase):
    capabilities = CAPABILITIES
    done = [
        {"capability": c, "status": "succeeded", "parameters": {}}
        for c in ("power", "bootstrap_logistics", "mining_outpost", "smelting_district")
    ]

    def test_priorities_follow_the_progression_classes(self):
        self.assertEqual(tg.SLACK_PRIORITY, pg.SCIENCE_GROWTH_PRIORITY + 1)
        self.assertEqual(tg.CRITICAL_PRIORITY, pg.SMELTING_CAPACITY_PRIORITY)
        self.assertLess(tg.SLACK_PRIORITY, pg.TOP_PRIORITY - len(pg.ROADMAP))

    def test_slack_node_is_low_priority_and_single_per_plate(self):
        obs = receipt(tg.decide(IRON), tg.decide(COPPER))
        nodes = tg.throughput_growth_nodes(self.done, [], obs, self.capabilities, critical=False)
        self.assertEqual([n["capability"] for n in nodes], ["mining_outpost", "smelting_capacity"])
        self.assertTrue(all(n["priority"] == tg.SLACK_PRIORITY for n in nodes))
        self.assertEqual(tg.throughput_growth_nodes(self.done, [], obs, self.capabilities, critical=True), [])
        # Queued: nothing more for that resource.
        again = tg.throughput_growth_nodes(self.done, nodes[:1], obs, self.capabilities, critical=False)
        self.assertEqual([n["capability"] for n in again], ["smelting_capacity"])

    def test_critical_when_the_bus_waits_for_district_plates(self):
        bus = {
            "capability": "planned_main_bus",
            "status": "waiting",
            "parameters": {},
            "reason": "waiting for district plates to reach every built bus lane tail",
        }
        self.assertTrue(tg.bottleneck_blocks_critical_path("iron-plate", self.done + [bus]))
        obs = receipt(tg.decide(IRON))
        nodes = tg.throughput_growth_nodes(self.done + [bus], [], obs, self.capabilities, critical=True)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["priority"], tg.CRITICAL_PRIORITY)

    def test_open_or_stalled_jobs_block_the_node(self):
        row = tg.decide(IRON)
        obs = receipt(row)
        roadmap = {
            "capability": "mining_outpost",
            "status": "running",
            "parameters": {"resource": "iron-ore", "rate_per_minute": 60},
        }
        self.assertEqual(
            tg.throughput_growth_nodes(self.done + [roadmap], [], obs, self.capabilities, critical=False), []
        )
        stalled = {"capability": "mining_outpost", "status": "blocked", "parameters": row["parameters"]}
        self.assertEqual(
            tg.throughput_growth_nodes(self.done + [stalled], [], obs, self.capabilities, critical=False), []
        )

    def test_no_node_before_the_district(self):
        obs = receipt(tg.decide(IRON))
        self.assertEqual(tg.throughput_growth_nodes(self.done[:3], [], obs, self.capabilities, critical=False), [])

    def test_autopilot_proposes_growth_beside_the_roadmap(self):
        obs = dict(receipt(tg.decide(IRON)), technologies={})
        outposts = [
            {"capability": "mining_outpost", "status": "succeeded", "parameters": step.parameters}
            for step in pg.ROADMAP
            if step.capability == "mining_outpost"
        ]
        nodes = pg.autopilot_nodes(self.done + outposts, [], obs, self.capabilities, limit=50)
        growth = [n for n in nodes if n["id"].startswith(tg.NODE_PREFIX)]
        self.assertEqual(len(growth), 1)
        self.assertGreater(len(nodes), 1)
        self.assertEqual(growth[0]["parameters"], {"resource": "iron-ore", "rate_per_minute": 120.0})


class ReceiptTests(unittest.TestCase):
    def test_saved_state_shapes(self):
        world = "w"
        slot = lambda row, i: {  # noqa: E731
            "row": row,
            "slot": i,
            "entities": [{"name": "stone-furnace", "position": {"x": i, "y": 0 if row == "left" else 9}}]
            + [{"name": "inserter", "position": {"x": i, "y": k}} for k in (1, 2)],
        }
        district = {
            "world_id": world,
            "banks": [
                {
                    "resource": "copper-ore",
                    "product": "copper-plate",
                    "per_side_count": 1,
                    "capacity_slots": 12,
                    "entities": [
                        {"name": "stone-furnace", "position": {"x": 0, "y": 0}},
                        {"name": "stone-furnace", "position": {"x": 0, "y": 9}},
                    ],
                    "growth_slots": [slot(r, i) for r in ("left", "right") for i in range(1, 12)],
                }
            ],
        }
        drill = {"name": "electric-mining-drill", "position": {"x": 50, "y": 50}}
        mining = {
            "world_id": world,
            "banks": [
                {
                    "ready": True,
                    "nominal_rate_per_minute": 60.0,
                    "reserved_pair_slots": 3,
                    "capacity_profile": {"planned_per_drill_per_minute": 30.0},
                    "layout_contract": {"growth_entities": [{"name": "electric-mining-drill"}] * 4},
                    "plan": {"resource": "copper-ore", "pair_count": 1, "entities": [drill]},
                }
            ],
        }
        board = {
            "world_id": world,
            "shared_main_bus": {
                "stations": [
                    {"item": "copper-plate", "owner": "red_science", "demand_per_minute": 30.0, "built": True},
                    {
                        "item": "copper-plate",
                        "owner": "production_row_copper-cable",
                        "demand_per_minute": 300.0,
                        "built": True,
                    },
                ]
            },
        }
        obs = {
            "world_id": world,
            "tick": 100000,
            "entities": [
                {"name": "stone-furnace", "position": {"x": 0, "y": 0}, "status_name": "working"},
                {"name": "stone-furnace", "position": {"x": 0, "y": 9}, "status_name": "working"},
                dict(drill, status_name="waiting_for_space_in_destination"),
            ],
            "smelting_throughput_bounds": {
                "banks": [
                    {
                        "resource": "copper-ore",
                        "nominal_capacity_per_minute": 37.5,
                        "connected_nominal_ore_supply": 60.0,
                        "unstacked_belt_capacity_per_lane": 450.0,
                    }
                ]
            },
            "roadmap_material_demand": {"copper-plate": 100.0},
            "resource_supply_options": {"iron-plate": {"evidence": {"available": 500, "produced_per_minute": 60}}},
        }
        catalog = SimpleNamespace(recipe_for_product=lambda name: None)
        with tempfile.TemporaryDirectory() as root:
            for name, value in (
                ("smelting-district.json", district),
                ("mining-outposts.json", mining),
                ("pcb-board.json", board),
            ):
                Path(root, name).write_text(json.dumps(value), encoding="utf-8")
            result = tg.growth_receipt(root, catalog, [], obs)
        (row,) = result["plates"]
        # The stock row's tap is construction stock (in the bill), the science tap is continuous.
        self.assertEqual(row["demand"]["taps_per_minute"], 30.0)
        self.assertEqual(row["signals"]["drills_backed"], 1)
        self.assertEqual(row["bottleneck"], "furnaces")
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["parameters"], {"resource": "copper-ore", "rate_per_minute": 60.0})


class SummaryCarriesTheReceiptTests(unittest.TestCase):
    def test_the_observe_summary_passes_the_growth_receipt_to_the_autopilot(self):
        # Seed 2644599892: the rule decided to grow, but the summary dropped the
        # receipt, so no node was ever proposed and the bus waited 40 minutes.
        import inspect

        from factorio_agent import jobs

        self.assertIn('"throughput_growth": obs.get("throughput_growth")', inspect.getsource(jobs))


if __name__ == "__main__":
    unittest.main()
