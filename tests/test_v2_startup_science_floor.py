"""The startup science cell starts with 2 labs and the machines they consume (industry.start_floor)."""

import unittest

from factorio_agent.industry import start_floor
from factorio_agent.research_capacity import (
    ResearchCapacityConfig,
    ResearchHardware,
    ResearchMaterials,
    ResearchTarget,
    plan_research_capacity,
)

RED = "automation-science-pack"
QUEUE = [
    ResearchTarget("electric-mining-drill", 25, 10.0, {RED: 1.0}),
    ResearchTarget("logistics", 20, 15.0, {RED: 1.0}),
]
ASSEMBLER = {"iron-plate": 22.0, "copper-plate": 4.5}
LAB = {"iron-plate": 36.0, "copper-plate": 15.0}
CONFIG = ResearchCapacityConfig(max_assemblers=2, max_labs=4)


def decide(iron_per_minute):
    evidence = {
        "iron-plate": {"available": 0, "produced_per_minute": iron_per_minute, "net_per_minute": 0.0},
        "copper-plate": {"available": 0, "produced_per_minute": 30.0, "net_per_minute": 0.0},
    }
    decision = plan_research_capacity(
        QUEUE,
        ResearchHardware({RED: 1}, {RED: 6.0}, 1),
        ResearchMaterials(
            evidence=evidence,
            assembler_cost={RED: ASSEMBLER},
            lab_cost=LAB,
            machines_per_assembler={RED: 3.0},
        ),
        config=CONFIG,
    )
    return decision.as_dict()


class StartFloorTests(unittest.TestCase):
    def test_a_kept_one_lab_cell_starts_with_two_labs_and_two_machines(self):
        # fleet all-1114-0256 seed 2697835185: "keep 1 assemblers and 1 labs; wait 560s"
        row = decide(iron_per_minute=15.0)
        self.assertEqual((row["target_assemblers"], row["target_labs"]), (1, 1))
        raised = start_floor(row, QUEUE, 6.0, CONFIG)
        self.assertEqual((raised["target_assemblers"], raised["target_labs"]), (2, 2))
        self.assertTrue(raised["expand"])
        self.assertEqual(raised["start_floor"]["planner_target"], [1, 1])
        self.assertIn("2 labs first", raised["reason"])
        option = next(o for o in row["options"] if (o["extra_assemblers"], o["extra_labs"]) == (1, 1))
        self.assertEqual(raised["chosen_seconds"], option["seconds"])

    def test_the_planner_decides_above_the_floor(self):
        row = dict(decide(iron_per_minute=15.0), target_assemblers=2, target_labs=3)
        self.assertIs(start_floor(row, QUEUE, 6.0, CONFIG), row)

    def test_only_labs_the_machines_keep_busy(self):
        row = decide(iron_per_minute=15.0)
        # one machine (6/min) keeps 1.2 labs busy at 4.9 packs/min per lab: no second lab
        self.assertIs(start_floor(row, QUEUE, 6.0, ResearchCapacityConfig(max_assemblers=1, max_labs=4)), row)
        # 5 s units (logistic-science-pack: 12 packs/min per lab): two machines keep one lab busy
        fast = [ResearchTarget("logistic-science-pack", 75, 5.0, {RED: 1.0})]
        self.assertIs(start_floor(row, fast, 6.0, CONFIG), row)

    def test_an_unfundable_start_keeps_the_decision(self):
        row = decide(iron_per_minute=15.0)
        row["options"] = [dict(o, seconds=None) for o in row["options"]]
        self.assertIs(start_floor(row, QUEUE, 6.0, CONFIG), row)

    def test_a_cell_without_lab_slots_is_unchanged(self):
        row = decide(iron_per_minute=15.0)
        self.assertIs(start_floor(row, QUEUE, 6.0, ResearchCapacityConfig(max_assemblers=2, max_labs=1)), row)


if __name__ == "__main__":
    unittest.main()
