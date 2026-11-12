import unittest

from factorio_agent.jobs import CAPABILITIES
from factorio_agent.progression import (
    BULK_THRESHOLD,
    CAPACITY_PRIORITY,
    PRODUCE_PRIORITY,
    autopilot_nodes,
    bulk_item_demand,
    produce_nodes,
)
from test_v2_production_layout import catalog

DONE = [
    {"capability": "starter", "status": "succeeded", "parameters": {}},
    {"capability": "power", "status": "succeeded", "parameters": {}},
    {"capability": "smelting_district", "status": "succeeded", "parameters": {}},
]


def supply(iron=6.0, copper=6.0):
    return {
        "iron-plate": {"evidence": {"net_per_minute": iron}},
        "copper-plate": {"evidence": {"net_per_minute": copper}},
    }


def observation(demand, **extra):
    return {"bulk_item_demand": demand, "resource_supply_options": supply(), **extra}


BELTS = {"transport-belt": {"count": 30, "raw_per_item": {"iron-plate": 1.5}}}


class BulkDemandTests(unittest.TestCase):
    def test_finished_items_of_the_next_steps_without_raw_plates(self):
        demand = bulk_item_demand(catalog(), [], {}, horizon=4)
        self.assertEqual(demand["transport-belt"]["count"], 30)  # power step
        self.assertEqual(demand["transport-belt"]["raw_per_item"], {"iron-plate": 1.5})
        self.assertEqual(demand["pipe"]["count"], 8)
        self.assertNotIn("coal", demand)
        self.assertNotIn("boiler", demand)  # no recipe in this catalog


class ProduceNodeTests(unittest.TestCase):
    def test_need_above_threshold_proposes_a_row_capped_by_plate_net(self):
        nodes = produce_nodes(DONE, [], observation(BELTS), CAPABILITIES)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["capability"], "produce")
        # 30 belts / 5 min = 6/min, but 6 iron/min net carries only 6 / 1.5 = 4 belts/min.
        self.assertEqual(node["parameters"], {"item": "transport-belt", "rate_per_minute": 4.0})
        self.assertEqual(node["priority"], PRODUCE_PRIORITY)
        self.assertLess(PRODUCE_PRIORITY, CAPACITY_PRIORITY)

    def test_not_justified_cases_propose_nothing(self):
        small = {"pipe": {"count": BULK_THRESHOLD, "raw_per_item": {"iron-plate": 1}}}
        cases = {
            "small need": (DONE, [], observation(small)),
            "prerequisites": (DONE[:2], [], observation(BELTS)),
            "unmeasured plate": (DONE, [], {"bulk_item_demand": BELTS}),
            "starved plate": (DONE, [], dict(observation(BELTS), resource_supply_options=supply(iron=0))),
            "already queued": (
                DONE,
                [{"capability": "produce", "parameters": {"item": "transport-belt", "rate_per_minute": 2}}],
                observation(BELTS),
            ),
            "already built": (
                DONE + [{"capability": "produce", "status": "succeeded", "parameters": {"item": "transport-belt"}}],
                [],
                observation(BELTS),
            ),
        }
        for name, (jobs, queue, obs) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(produce_nodes(jobs, queue, obs, CAPABILITIES), [])

    def test_autopilot_keeps_its_signature_and_limit(self):
        rows = autopilot_nodes(DONE, [], observation(BELTS), CAPABILITIES, limit=3)
        self.assertIn("auto_produce_transport-belt", [q["id"] for q in rows])
        self.assertLessEqual(len(rows), 3)
        self.assertEqual(autopilot_nodes(DONE, [], observation(BELTS), CAPABILITIES, limit=0), [])


if __name__ == "__main__":
    unittest.main()
