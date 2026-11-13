"""Plate throughput growth: bottleneck stage, expansion packages, slack vs critical nodes."""

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
# eval-4177156614 at ~67 game min (observe receipt throughput_growth, iron-plate): taps 24/min, bill
# 937.7 plates, 6/24 furnaces (112.5/min) under 2/3 drill pairs (120/min). The single-step rule valued one
# furnace slot (+7.5/min, the ore binds next) and answered "waiting is as fast: wait 628s, expand 675s".
LIVE = replace(
    IRON,
    tap_rate=24.0,
    bill=937.7,
    furnace_nominal=112.5,
    per_side=3,
    ore_nominal=120.0,
    pairs_built=2,
    measured=148.9,
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
    def test_ore_limited_iron_grows_drills_first_then_the_furnaces_to_match(self):
        row = tg.decide(IRON)
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["capability"], "mining_outpost")
        self.assertEqual([s["capability"] for s in row["stages"]], ["mining_outpost", "smelting_capacity"])
        ore, furnaces = (s["parameters"]["rate_per_minute"] for s in row["stages"])
        self.assertLessEqual(furnaces, ore)  # smelting_capacity never runs above the connected ore
        self.assertGreater(row["times"]["saving_seconds"], tg.MIN_SAVING_SECONDS)
        # 180/min (+2 pairs +3 slots per row, the full reserved frame): the known 177.8/min already
        # exceeds the quickest 150/min package, so that one would mean a second trip (revisit 51 s).
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (2, 3))
        self.assertEqual(row["build_ahead"]["quickest"], "+2 drill pairs +4 furnaces -> 150/min")

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

    def test_time_comparison_rejects_a_package_that_does_not_pay(self):
        # Demand just above capacity: the build time of every package eats the saving.
        row = tg.decide(replace(COPPER, bill=420.0, furnace_step_entities=300, ore_step_entities=300))
        self.assertFalse(row["grow"])
        self.assertIn("waiting is as fast", row["reason"])
        # The wait is visible: the best package and its times are in the receipt.
        self.assertIn("best package", row["reason"])
        self.assertTrue(row["package"]["summary"])
        self.assertTrue(row["alternatives"])
        self.assertLess(row["times"]["saving_seconds"], 0)

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


class PackageTests(unittest.TestCase):
    def test_live_shortage_grows_a_drill_pair_with_furnaces_instead_of_waiting(self):
        row = tg.decide(LIVE)
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["bottleneck"], "furnaces")
        stages = row["stages"]
        self.assertEqual([s["capability"] for s in stages], ["mining_outpost", "smelting_capacity"])
        self.assertEqual(stages[0]["parameters"], {"resource": "iron-ore", "rate_per_minute": 180.0})
        self.assertEqual(stages[1]["parameters"], {"resource": "iron-ore", "rate_per_minute": 150.0})
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (1, 1))
        self.assertGreater(row["times"]["saving_seconds"], tg.MIN_SAVING_SECONDS)
        # The single furnace slot (gain 7.5/min) is still evaluated, and loses.
        single = [a for a in row["alternatives"] if (a["pairs"], a["furnace_slots"]) == (0, 1)]
        self.assertEqual(len(single), 1)
        self.assertLess(single[0]["saving_seconds"], row["times"]["saving_seconds"])

    def test_package_capacity_stops_at_the_smallest_that_covers_the_demand(self):
        row = tg.decide(LIVE)
        cap = row["package"]["demand_cap_per_minute"]
        covering = [a["capacity_after"] for a in row["alternatives"] if a["capacity_after"] >= cap]
        self.assertEqual(covering, [150.0])  # no 180/min or 225/min package for a 117.8/min demand

    def test_small_shortfall_picks_one_furnace_slot(self):
        row = tg.decide(replace(COPPER, bill=420.0))
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (0, 1))
        self.assertEqual([s["capability"] for s in row["stages"]], ["smelting_capacity"])

    def test_demand_already_covered_grows_nothing(self):
        row = tg.decide(replace(LIVE, bill=200.0))  # 44/min < 112.5 / 1.1
        self.assertFalse(row["grow"])
        self.assertIn("covered", row["reason"])
        self.assertNotIn("package", row)

    def test_reserved_pairs_full_proposes_the_next_module(self):
        facts = replace(
            LIVE, pairs_built=3, ore_nominal=180.0, furnace_nominal=187.5, per_side=5, bill=3000.0, patch_rows=6
        )
        row = tg.decide(facts)
        self.assertTrue(row["grow"], row["reason"])
        first = row["stages"][0]
        self.assertEqual(first["capability"], "mining_outpost")
        self.assertIn("connected module", first["unit"])
        self.assertIn("patch_grid 6 rows", first["unit"])
        self.assertGreater(first["parameters"]["rate_per_minute"], 180.0)

    def test_reserved_pairs_fill_before_the_module(self):
        row = tg.decide(replace(IRON, bill=6000.0))
        self.assertTrue(row["grow"], row["reason"])
        units = [s["unit"] for s in row["stages"] if s["capability"] == "mining_outpost"]
        self.assertTrue(units[0].startswith("drill pair"), units)
        for unit in units[1:]:
            self.assertIn("connected module", unit)

    def test_critical_bill_starvation_makes_the_package_lose(self):
        # Critical jobs claim 900 plates: a slack growth job gets its own plates after them.
        stocks = {**LIVE.stocks, "iron-plate": (0.0, 112.5)}
        self.assertTrue(tg.decide(replace(LIVE, stocks=stocks))["grow"])
        row = tg.decide(replace(LIVE, claimed=900.0, stocks=stocks))
        self.assertFalse(row["grow"], row["reason"])
        self.assertIn("waiting is as fast", row["reason"])
        self.assertEqual(row["demand"]["claimed_by_critical_jobs"], 900.0)

    def test_wait_longer_than_the_package_payback_is_not_accepted_on_a_thin_margin(self):
        # Drill packages are very expensive: one furnace slot remains, saving < MIN_SAVING_SECONDS,
        # yet the 636 s wait outlasts the slot's payback, so it grows.
        row = tg.decide(replace(LIVE, ore_step_entities=400, module_step_entities=400))
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (0, 1))
        self.assertLess(row["times"]["saving_seconds"], tg.MIN_SAVING_SECONDS)
        self.assertGreater(row["times"]["wait_seconds"], row["times"]["payback_seconds"])
        self.assertIn("payback", row["reason"])

    def test_taps_above_every_package_take_the_largest(self):
        row = tg.decide(replace(IRON, bill=0.0, tap_rate=2000.0))
        self.assertTrue(row["grow"], row["reason"])
        self.assertIsNone(row["times"]["wait_seconds"])  # never served by waiting
        self.assertLessEqual(row["package"]["capacity_after"], tg.SMELTING_RATE_MAX)

    def test_every_package_is_efficient(self):
        for package in tg.packages(LIVE, 112.5, "furnaces"):
            k, f = package["pairs"], package["furnace_slots"]
            if k:
                self.assertLess(tg._after(LIVE, k - 1, f)[2], package["capacity_after"])
            if f:
                self.assertLess(tg._after(LIVE, k, f - 1)[2], package["capacity_after"])


# The live shape with the roadmap's next block quantified: steel_production taps 37.5 iron/min (what
# eval-4177156614 added ten minutes after the ~67 min receipt).
RISING = replace(LIVE, expected_growth=37.5, growth_sources=(("roadmap steel_production", 37.5),))


class BuildAheadTests(unittest.TestCase):
    def test_rising_lookahead_demand_builds_the_larger_package_in_one_go(self):
        row = tg.decide(RISING)
        self.assertTrue(row["grow"], row["reason"])
        # Expected 117.8 + 37.5 = 155.3/min x 1.1 = 170.8: +1 drill pair +2 slots per row -> 180/min
        # (T1 496 s, no revisit) beats the quickest +1 pair +1 slot -> 150/min (T1 533 s + revisit 34 s).
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (1, 2))
        self.assertEqual(row["package"]["capacity_after"], 180.0)
        self.assertEqual(row["stages"][1]["parameters"], {"resource": "iron-ore", "rate_per_minute": 180.0})
        ahead = row["build_ahead"]
        self.assertEqual(ahead["expected_demand_per_minute"], 155.27)
        self.assertEqual(ahead["margin_per_minute"], 37.5)
        self.assertEqual(ahead["quickest"], "+1 drill pair +2 furnaces -> 150/min")
        self.assertGreater(ahead["quickest_revisit_seconds"], 0)
        self.assertEqual(ahead["chosen_revisit_seconds"], 0)
        self.assertIn("build ahead", row["reason"])
        self.assertEqual(row["expected"]["sources"], [["roadmap steel_production", 37.5]])
        json.dumps(row, allow_nan=False)

    def test_flat_demand_keeps_the_minimal_package(self):
        row = tg.decide(LIVE)
        # Nothing expected beyond the known 117.8/min: 150/min already reaches 117.8 x 1.1.
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (1, 1))
        self.assertEqual(row["build_ahead"]["margin_per_minute"], 0.0)
        self.assertIn("already reaches", row["build_ahead"]["why"])
        self.assertNotIn("build ahead", row["reason"])

    def test_small_shortfall_grows_ahead_only_when_demand_rises(self):
        flat = tg.decide(replace(COPPER, bill=420.0))
        # 42/min known demand: one furnace slot per row -> 60/min covers it and nothing more comes.
        self.assertEqual((flat["package"]["pairs"], flat["package"]["furnace_slots"]), (0, 1))
        small = tg.decide(replace(COPPER, bill=420.0, expected_growth=10.0))
        # 52/min expected x 1.1 = 57.2: the same slot still covers it, no drill pair ahead.
        self.assertEqual((small["package"]["pairs"], small["package"]["furnace_slots"]), (0, 1))
        rising = tg.decide(replace(COPPER, bill=420.0, expected_growth=30.0))
        # 72/min expected x 1.1 = 79.2: +1 drill pair +2 slots per row -> 112.5/min in one go
        # (T1 374 s) beats the slot (T1 458 s + revisit 91 s).
        self.assertTrue(rising["grow"], rising["reason"])
        self.assertEqual((rising["package"]["pairs"], rising["package"]["furnace_slots"]), (1, 2))

    def test_unquantified_upcoming_step_adds_the_default_allowance(self):
        row = tg.decide(replace(LIVE, growth_unquantified=("new_block",)))
        expected = row["expected"]
        self.assertAlmostEqual(expected["allowance_per_minute"], 117.77 * tg.DEFAULT_GROWTH_SHARE, places=1)
        self.assertEqual(expected["unquantified_steps"], ["new_block"])
        # 147.2/min x 1.1 = 161.9 is above 150: the 180/min package is built in one go.
        self.assertEqual(row["package"]["capacity_after"], 180.0)

    def test_margin_is_bounded_by_the_reserved_frame(self):
        row = tg.decide(replace(LIVE, expected_growth=400.0))
        # 517.8/min expected, but only one reserved drill pair is free: 180/min is the frame,
        # no connected module is built ahead (the quickest needs none).
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["package"]["capacity_after"], 180.0)
        self.assertFalse(any("connected module" in s["unit"] for s in row["stages"]))
        self.assertEqual(row["build_ahead"]["full_package"], "+1 drill pair +4 furnaces -> 180/min")

    def test_critical_claims_make_the_oversized_package_lose(self):
        stocks = {**LIVE.stocks, "iron-plate": (0.0, 112.5)}
        self.assertEqual(tg.decide(replace(RISING, stocks=stocks))["package"]["capacity_after"], 180.0)
        row = tg.decide(replace(RISING, stocks=stocks, claimed=50.0))
        # A critical job claims 50 plates: the quickest still pays, the larger package's extra
        # plates would wait behind the claim, so it loses.
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual((row["package"]["pairs"], row["package"]["furnace_slots"]), (1, 1))
        self.assertIn("critical claim", row["build_ahead"]["why"])

    def test_covered_demand_never_grows_for_expected_growth_alone(self):
        row = tg.decide(replace(LIVE, bill=200.0, expected_growth=300.0))
        # 44/min known demand is covered by 112.5 / 1.1: build ahead only sizes a justified growth.
        self.assertFalse(row["grow"])
        self.assertIn("covered", row["reason"])

    def test_revisit_cost(self):
        self.assertEqual(tg.revisit_seconds(180.0, 180.0, 155.0, 1, 14), 0.0)
        # One trip again, plus the 3.4 % of 155/min unserved while settle + trip + 14 entities pass.
        short = 1 - 150.0 / 155.0
        self.assertAlmostEqual(tg.revisit_seconds(150.0, 180.0, 155.0, 1, 14), 30.0 + short * (60.0 + 30.0 + 42.0))


class ExpectedGrowthTests(unittest.TestCase):
    def catalog(self):
        from test_v2_research_capacity import catalog, item, recipe

        cat = catalog()
        green = "logistic-science-pack"
        cat.recipes[green] = recipe(green, 6, [item("inserter", 1), item("transport-belt", 1)])
        return cat

    def test_upcoming_blocks_unbuilt_taps_and_science_growth(self):
        cat = self.catalog()
        jobs, researched = [], {}
        for step in pg.ROADMAP:
            if step.id == "research_logistic_science_pack":
                break
            jobs.append({"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"})
        stations = [
            {"name": "automation_science:iron-plate", "owner": "automation_science", "item": "iron-plate",
             "demand_per_minute": 24.0, "built": True},
            {"name": "chem:iron-plate", "owner": "chem", "item": "iron-plate", "demand_per_minute": 10.0, "built": False},
            {"name": "production_row_gear:iron-plate", "owner": "production_row_gear", "item": "iron-plate",
             "demand_per_minute": 99.0, "built": False},
        ]  # fmt: skip
        obs = {"technologies": researched, "science_growth": {"grow": True, "machines": 2, "target_machines": 3}}
        rows = tg.expected_growth(cat, jobs, obs, stations)
        iron, sources, unknown = rows["iron-plate"]
        # logistic_science at 30/min: (inserter 4 + belt 1.5) iron x 30 = 165; the unbuilt chem tap 10;
        # +1 red machine 6 packs/min x 2 iron = 12. Built taps and stock rows are not growth.
        self.assertEqual(
            dict(sources),
            {"roadmap logistic_science": 165.0, "unbuilt tap chem:iron-plate": 10.0,
             "science_growth: +1 automation-science-pack machines": 12.0},
        )  # fmt: skip
        self.assertAlmostEqual(iron, 187.0)
        self.assertEqual(unknown, ())
        copper = dict(rows["copper-plate"][1])
        self.assertEqual(
            copper, {"roadmap logistic_science": 45.0, "science_growth: +1 automation-science-pack machines": 6.0}
        )

    def test_unknown_block_is_unquantified_and_neutral_blocks_add_nothing(self):
        cat = self.catalog()
        self.assertIsNone(tg.upcoming_rates(cat, SimpleNamespace(capability="new_block", parameters={})))
        self.assertEqual(tg.upcoming_rates(cat, SimpleNamespace(capability="steam_expansion", parameters={})), {})


class MeasuredSplitTests(unittest.TestCase):
    def test_force_wide_counter_splits_into_connected_and_off_bus(self):
        # eval-4177156614: 148.9/min measured while six district furnaces make 112.5/min.
        connected, off_bus = tg._measured_split(148.9, ["working"] * 6, 18.75, 112.5)
        self.assertAlmostEqual(connected, 112.5)
        self.assertAlmostEqual(off_bus, 36.4)
        self.assertEqual(tg._measured_split(None, [], 18.75, 112.5), (None, None))
        connected, _ = tg._measured_split(148.9, ["working"] * 3 + ["no_ingredients"] * 3, 18.75, 112.5)
        self.assertAlmostEqual(connected, 56.25)  # idle district furnaces are not connected output

    def test_receipt_reports_both_and_compares_the_district_only(self):
        row = tg.decide(replace(LIVE, connected_measured=112.5, off_bus_measured=36.4, starter_phase="retiring"))
        self.assertEqual(row["supply"]["measured_per_minute"], 148.9)
        self.assertEqual(row["supply"]["measured_connected_per_minute"], 112.5)
        self.assertEqual(row["supply"]["measured_off_bus_per_minute"], 36.4)
        self.assertEqual(row["supply"]["starter_cells"], "retiring")
        self.assertEqual(row["supply"]["capacity_per_minute"], 112.5)

    def test_retiring_starter_cells_fund_nothing(self):
        iron = replace(
            LIVE,
            connected_measured=112.5,
            off_bus_measured=36.4,
            starter_phase="retiring",
            stocks={"iron-plate": (0.0, 148.9)},
        )
        copper = replace(
            COPPER,
            connected_measured=37.5,
            off_bus_measured=10.0,
            stocks={"iron-plate": (0.0, 148.9), "copper-plate": (0.0, 47.5)},
        )
        funded = tg._bus_funding({"iron-plate": iron, "copper-plate": copper})
        self.assertEqual(funded["iron-plate"].stocks["iron-plate"], (0.0, 112.5))
        self.assertEqual(funded["copper-plate"].stocks["iron-plate"], (0.0, 112.5))
        self.assertEqual(funded["copper-plate"].stocks["copper-plate"], (0.0, 47.5))  # its cells still run

    def test_critical_claims_come_from_the_critical_path_allocation(self):
        step = pg.ROADMAP[0]
        job = {
            "key": "a",
            "capability": step.capability,
            "status": "waiting",
            "parameters": dict(step.parameters),
            "reason": "waiting for iron-plate",
            "evidence": {"item": "iron-plate", "need": 300},
        }
        self.assertEqual(tg.critical_plate_claims(None, [job], {"technologies": {}}), {"iron-plate": 300.0})
        self.assertEqual(tg.critical_plate_claims(None, [], {}), {})

    def test_starter_phase_from_jobs_and_state(self):
        job = {"capability": "starter_retirement", "status": "succeeded", "parameters": {"resource": "iron-ore"}}
        self.assertEqual(tg._starter_phase({}, "iron-ore", [job]), "retired")
        state = {"resources": {"coal": {"phase": "retiring"}}}
        self.assertEqual(tg._starter_phase(state, "coal", []), "retiring")
        self.assertEqual(tg._starter_phase(state, "copper-ore", []), "")


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
        # Iron: its package (drills, then furnaces); copper: one furnace stage.
        self.assertEqual(
            [(n["capability"], n["parameters"]["resource"]) for n in nodes],
            [("mining_outpost", "iron-ore"), ("smelting_capacity", "iron-ore"), ("smelting_capacity", "copper-ore")],
        )
        self.assertEqual(nodes[0]["depends_on"], [])
        self.assertEqual(nodes[1]["depends_on"], [nodes[0]["id"]])
        self.assertTrue(all(n["priority"] == tg.SLACK_PRIORITY for n in nodes))
        self.assertEqual(tg.throughput_growth_nodes(self.done, [], obs, self.capabilities, critical=True), [])
        # One package per plate in flight: a queued iron stage blocks every iron stage.
        again = tg.throughput_growth_nodes(self.done, nodes[:1], obs, self.capabilities, critical=False)
        self.assertEqual([n["parameters"]["resource"] for n in again], ["copper-ore"])

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
        self.assertEqual(len(nodes), 2)  # the package's two stages
        self.assertTrue(all(n["priority"] == tg.CRITICAL_PRIORITY for n in nodes))

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
        self.assertEqual([n["capability"] for n in growth], ["mining_outpost", "smelting_capacity"])
        self.assertGreater(len(nodes), 2)
        self.assertEqual(growth[0]["parameters"], tg.decide(IRON)["parameters"])


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
