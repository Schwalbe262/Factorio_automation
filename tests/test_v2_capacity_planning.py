"""Need-driven starter capacity: wait vs. expand time comparison, fuel rule and ordering."""

import math
import unittest

from factorio_agent.capacity_planning import CapacityConfig, plan_capacity

CELL_COST = {"coal": 8, "iron-plate": 9, "stone": 10}


def option(**evidence):
    return {"action": "wait", "reason": "test", "evidence": evidence}


def iron(**overrides):
    evidence = dict(
        shortfall=0,
        produced_per_minute=15.0,
        consumed_per_minute=0.0,
        net_per_minute=15.0,
        cell_capacity_per_minute=15.0,
        productive_cells=1,
        installed_live_cells=1,
        raw_cost_per_cell={"iron-plate": 9},
        available=0,
    )
    evidence.update(overrides)
    return option(**evidence)


def live_supply():
    """Live map numbers: one iron cell short by 133 plates, coal in surplus."""
    return {
        "iron-plate": iron(
            shortfall=133,
            produced_per_minute=15.1,
            consumed_per_minute=14.5,
            net_per_minute=0.6,
            raw_cost_per_cell=dict(CELL_COST),
        ),
        "coal": option(
            shortfall=0,
            produced_per_minute=23.7,
            consumed_per_minute=12.9,
            net_per_minute=10.8,
            cell_capacity_per_minute=None,
            productive_cells=4,
            installed_live_cells=4,
            raw_cost_per_cell={"iron-plate": 9, "stone": 5},
            available=40,
        ),
        "stone": option(
            shortfall=0,
            produced_per_minute=6.0,
            consumed_per_minute=0.0,
            net_per_minute=6.0,
            productive_cells=1,
            installed_live_cells=1,
            raw_cost_per_cell={"iron-plate": 9},
            available=30,
        ),
    }


class TimeComparisonTests(unittest.TestCase):
    def test_user_example_hundred_plates(self):
        decisions = plan_capacity({"iron-plate": iron(shortfall=100)}, {})
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        evidence = decision.evidence
        self.assertAlmostEqual(evidence["wait_seconds"], 400.0)
        # k=1: fund 9/15 min = 36 s, build 15 s, then 109 plates at 30/min = 218 s.
        self.assertAlmostEqual(evidence["options"][0]["seconds"], 36 + 15 + 218)
        # k=2: fund 72 s, build 30 s, then 118 plates at 45/min = 157.33 s.
        self.assertAlmostEqual(evidence["options"][1]["seconds"], 72 + 30 + 118 / 45 * 60)
        self.assertEqual(evidence["chosen_extra_cells"], 2)
        self.assertEqual((decision.resource, decision.current_cells, decision.target_cells), ("iron-ore", 1, 3))
        self.assertAlmostEqual(evidence["seconds_saved"], 400 - (102 + 118 / 45 * 60))
        self.assertIn("T(0)=400s", decision.reason)
        self.assertIn("T(2)=259s", decision.reason)

    def test_small_need_waits(self):
        self.assertEqual(plan_capacity({"iron-plate": iron(shortfall=10)}, {}), [])

    def test_unfundable_expansion_waits(self):
        supply = {
            "iron-plate": iron(shortfall=50, produced_per_minute=0.0, net_per_minute=0.0, available=0),
        }
        self.assertEqual(plan_capacity(supply, {}), [])

    def test_cross_item_without_producer_or_stock_is_impossible(self):
        supply = {"iron-plate": iron(shortfall=500, raw_cost_per_cell=dict(CELL_COST))}
        self.assertEqual(plan_capacity(supply, {}), [])
        supply["stone"] = option(produced_per_minute=0.0, available=0)
        supply["coal"] = option(produced_per_minute=30.0, consumed_per_minute=0.0, net_per_minute=30.0, available=99)
        self.assertEqual([d.item for d in plan_capacity(supply, {})], [])
        supply["stone"] = option(produced_per_minute=0.0, available=20)
        self.assertEqual([d.item for d in plan_capacity(supply, {})], ["iron-plate"])

    def test_surplus_funds_the_bill_in_time(self):
        supply = {"iron-plate": iron(shortfall=60, produced_per_minute=60.0, net_per_minute=60.0)}
        self.assertEqual(plan_capacity(supply, {}), [])

    def test_lookahead_demand_adds_to_shortfall(self):
        self.assertEqual(plan_capacity({"iron-plate": iron(shortfall=10)}, {}), [])
        decisions = plan_capacity({"iron-plate": iron(shortfall=10)}, {"iron-plate": 240})
        self.assertEqual(decisions[0].evidence["required"], 250)
        self.assertEqual(decisions[0].target_cells, 3)

    def test_none_rates_and_partial_receipts_do_not_raise(self):
        supply = {
            "iron-plate": option(shortfall=100, produced_per_minute=None, net_per_minute=None),
            "copper-plate": {"action": "wait"},
            "coal": None,
            "stone": option(),
        }
        self.assertEqual(plan_capacity(supply, None), [])
        self.assertEqual(plan_capacity(None, {}), [])
        self.assertEqual(plan_capacity({}, {"iron-plate": 100}), [])

    def test_spare_stock_covers_upcoming_demand(self):
        stocked = iron(shortfall=0, available=195, required=21, net_per_minute=-5, produced_per_minute=15)
        self.assertEqual(plan_capacity({"iron-plate": stocked}, {"iron-plate": 62}), [])
        short = iron(shortfall=0, available=30, required=21, net_per_minute=-5, produced_per_minute=15)
        self.assertEqual(len(plan_capacity({"iron-plate": short}, {"iron-plate": 400})), 1)

    def test_cells_installed_but_not_yet_producing_count_at_nominal_rate(self):
        # Seed 2099054105: coal went 1 -> 3 -> 5 in two minutes because the two
        # new cells had not produced yet when the planner looked again.
        smelting = iron(productive_cells=5, installed_live_cells=5, produced_per_minute=75.0, net_per_minute=10.0)
        coal = dict(
            shortfall=0,
            produced_per_minute=17.0,
            consumed_per_minute=18.7,
            net_per_minute=-1.7,
            productive_cells=1,
            installed_live_cells=3,
            cell_capacity_per_minute=15.0,
            raw_cost_per_cell={"wood": 2},
            available=3,
        )
        self.assertEqual(plan_capacity({"iron-plate": smelting, "coal": option(**coal)}, {"coal": 16}), [])
        short = dict(coal, installed_live_cells=1)
        decisions = plan_capacity({"iron-plate": smelting, "coal": option(**short)}, {"coal": 16})
        self.assertEqual([(d.item, d.current_cells) for d in decisions], [("coal", 1)])
        self.assertLessEqual(decisions[0].target_cells, 2)

    def test_max_cells_cap(self):
        full = dict(productive_cells=7, produced_per_minute=105.0, net_per_minute=5.0)
        decisions = plan_capacity({"iron-plate": iron(shortfall=1000, installed_live_cells=7, **full)}, {})
        self.assertEqual(decisions[0].target_cells, 8)
        self.assertEqual(plan_capacity({"iron-plate": iron(shortfall=1000, installed_live_cells=8)}, {}), [])
        tight = CapacityConfig(max_plate_cells=4)
        decisions = plan_capacity({"iron-plate": iron(shortfall=1000, installed_live_cells=3)}, {}, config=tight)
        self.assertEqual(decisions[0].target_cells, 4)

    def test_plate_cells_follow_lookahead_demand_past_four(self):
        """Seed 283895142: 4 iron cells at 60/min against 1000+ iron of upcoming steps."""
        four = iron(
            shortfall=0, installed_live_cells=4, productive_cells=4, produced_per_minute=60.0, net_per_minute=20.0
        )
        decisions = plan_capacity({"iron-plate": four}, {"iron-plate": 1200})
        self.assertEqual((decisions[0].current_cells, decisions[0].target_cells), (4, 6))

    def test_build_seconds_from_evidence(self):
        decision = plan_capacity({"iron-plate": iron(shortfall=100, build_seconds=60)}, {})[0]
        self.assertAlmostEqual(decision.evidence["options"][1]["build_seconds"], 120)

    def test_margin_settings_can_refuse_expansion(self):
        config = CapacityConfig(min_saving_seconds=200)
        self.assertEqual(plan_capacity({"iron-plate": iron(shortfall=100)}, {}, config=config), [])


class LiveCaseTests(unittest.TestCase):
    def test_iron_short_coal_surplus_expands_iron_only(self):
        decisions = plan_capacity(live_supply(), {})
        self.assertEqual([(d.item, d.target_cells) for d in decisions], [("iron-plate", 3)])
        self.assertTrue(math.isclose(decisions[0].evidence["wait_seconds"], 133 / 0.6 * 60))

    def test_coal_cells_follow_the_burner_cells_not_the_measured_rate(self):
        # User rule: one coal cell fuels several drills and furnaces, so coal
        # never gets more cells than the burners need, whatever the rates say.
        supply = live_supply()
        supply["iron-plate"] = iron(shortfall=0, produced_per_minute=0.0, net_per_minute=0.0, available=300)
        supply["coal"]["evidence"].update(produced_per_minute=14.0, consumed_per_minute=13.0, net_per_minute=1.0)
        self.assertEqual(plan_capacity(supply, {}), [])
        busy = dict(productive_cells=6, installed_live_cells=6, produced_per_minute=90.0, net_per_minute=20.0)
        supply["iron-plate"] = iron(shortfall=0, available=300, **busy)
        supply["coal"]["evidence"].update(installed_live_cells=1, productive_cells=1, raw_cost_per_cell={"wood": 2})
        decisions = plan_capacity(supply, {})
        # 6 smelting cells burn 21.6/min; x1.2 / 12.75 net per coal cell -> 3 coal cells.
        self.assertEqual([(d.item, d.current_cells, d.target_cells) for d in decisions], [("coal", 1, 2)])
        self.assertTrue(decisions[0].evidence["fuel_deficit"])
        self.assertIn("needed by the burner cells", decisions[0].reason)

    def test_coal_surplus_with_no_bill_is_not_expanded(self):
        supply = live_supply()
        supply["iron-plate"] = iron(shortfall=0)
        self.assertEqual(plan_capacity(supply, {}), [])


class OrderingTests(unittest.TestCase):
    def test_worst_bottleneck_first_when_costs_do_not_compete(self):
        stone_only = {"stone": 10}
        supply = {
            "iron-plate": iron(shortfall=200, net_per_minute=5.0, raw_cost_per_cell=stone_only),
            "copper-plate": iron(shortfall=200, net_per_minute=1.0, raw_cost_per_cell=stone_only),
            "stone": option(produced_per_minute=10.0, available=100),
        }
        self.assertEqual([d.item for d in plan_capacity(supply, {})], ["copper-plate", "iron-plate"])

    def test_one_expansion_when_the_second_spends_the_first_bottleneck(self):
        supply = {
            "iron-plate": iron(shortfall=200, net_per_minute=1.0),
            "copper-plate": iron(shortfall=200, net_per_minute=5.0, raw_cost_per_cell={"iron-plate": 9}),
        }
        self.assertEqual([d.item for d in plan_capacity(supply, {})], ["iron-plate"])

    def test_new_smelting_cells_count_toward_the_coal_cells_needed(self):
        supply = live_supply()
        supply["coal"]["evidence"].update(
            produced_per_minute=15.0,
            consumed_per_minute=5.0,
            net_per_minute=10.0,
            installed_live_cells=1,
            productive_cells=1,
            raw_cost_per_cell={"stone": 5},
        )
        decisions = plan_capacity(supply, {})
        # Iron goes 1 -> 3 cells: 3 x 3.6 x 1.2 / 12.75 -> 2 coal cells needed, 1 installed.
        self.assertEqual([(d.item, d.target_cells) for d in decisions], [("iron-plate", 3), ("coal", 2)])
        coal = decisions[1].evidence
        self.assertAlmostEqual(coal["extra_fuel_burn_per_minute"], 7.2)
        self.assertTrue(coal["fuel_deficit"])


class CoalCellWoodCostTests(unittest.TestCase):
    """Seed 283895142: a coal cell's wooden chest costs wood, which has no supply row."""

    def supply(self):
        cell = {"coal": 8, "iron-plate": 9, "stone": 5}
        return {
            "iron-plate": iron(
                produced_per_minute=60.0,
                consumed_per_minute=55.0,
                net_per_minute=5.0,
                productive_cells=4,
                installed_live_cells=4,
                raw_cost_per_cell=cell,
                available=20,
            ),
            "coal": option(
                shortfall=0,
                produced_per_minute=15.0,
                consumed_per_minute=15.0,
                net_per_minute=0.0,
                productive_cells=1,
                installed_live_cells=1,
                raw_cost_per_cell=dict(cell, wood=2),
                available=3,
            ),
            "stone": option(available=30, raw_cost_per_cell=dict(cell, wood=2)),
        }

    def test_coal_deficit_expands_although_the_chest_needs_wood(self):
        decisions = plan_capacity(self.supply(), {})
        self.assertEqual([(d.item, d.current_cells, d.target_cells) for d in decisions], [("coal", 1, 2)])
        self.assertTrue(decisions[0].evidence["fuel_deficit"])
        # 2 wood at 2 s each is part of the fund time.
        self.assertGreaterEqual(decisions[0].evidence["options"][0]["fund_seconds"], 4.0)

    def test_without_the_hand_gather_rule_coal_never_expanded(self):
        self.assertEqual(plan_capacity(self.supply(), {}, config=CapacityConfig(hand_gathered_seconds={})), [])

    def test_unknown_raw_input_still_makes_the_option_impossible(self):
        supply = self.supply()
        supply["coal"]["evidence"]["raw_cost_per_cell"] = {"coal": 8, "unobtainium": 1}
        self.assertEqual(plan_capacity(supply, {}), [])


if __name__ == "__main__":
    unittest.main()
