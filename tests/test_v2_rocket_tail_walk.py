"""Dry end-to-end walk of the generated rocket tail on the real catalog (fake successes).

Every tail node must become proposable by the autopilot in order, up to ``rocket_launch``: no
node waits on a capability or a fluid source that nothing in the tail provides. The three
rocket stages are excepted while their capabilities are not registered (another worker's).
"""

import unittest

from factorio_agent import progression as pg
from factorio_agent import rocket_plan as rp
from factorio_agent.jobs import CAPABILITIES, Request
from v2_blocks_fixture import real_catalog

STAGES = (rp.STAGE_SILO, rp.STAGE_PARTS, rp.STAGE_LAUNCH)


def walk(catalog, rounds=400):
    researched = {}
    receipt = rp.receipt(catalog, {"technologies": researched}, capabilities=CAPABILITIES)
    observation = {"technologies": researched, "rocket_plan": receipt}
    jobs = [{"capability": s.capability, "parameters": dict(s.parameters), "status": "succeeded"} for s in pg.ROADMAP]
    for step in pg.ROADMAP:
        if step.parameters.get("technology"):
            researched[step.parameters["technology"]] = True
    proposed = []
    for _ in range(rounds):
        nodes = pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=4)
        if not nodes:
            break
        for node in nodes:
            Request("k", "w", 1, node["capability"], node["parameters"]).validate()
            proposed.append(node["id"].removeprefix(pg.NODE_PREFIX))
            jobs.append({"capability": node["capability"], "parameters": node["parameters"], "status": "succeeded"})
            technology = node["parameters"].get("technology")
            if technology:
                researched[technology] = True
    return receipt["tail"], proposed


@unittest.skipIf(real_catalog() is None, "real catalog not available")
class RocketTailWalkTests(unittest.TestCase):
    def test_every_tail_node_becomes_proposable_in_order(self):
        catalog = real_catalog()
        if "rocket-silo" not in catalog.technologies:
            self.skipTest("catalog without a rocket silo")
        tail, proposed = walk(catalog)
        ids = [row["id"] for row in tail]
        expected = [i for i, row in zip(ids, tail) if row["capability"] in CAPABILITIES]
        missing = [i for i in expected if i not in proposed]
        self.assertEqual(missing, [], f"never proposed: {missing}")
        unregistered = {row["capability"] for row in tail if row["capability"] not in CAPABILITIES}
        self.assertLessEqual(unregistered, set(STAGES))
        # Proposed after everything its row waits on.
        position = {i: n for n, i in enumerate(proposed)}
        for row in tail:
            for before in row["after"]:
                if row["id"] in position and before in position:
                    self.assertLess(position[before], position[row["id"]], (before, row["id"]))
        # Every fluid source a row or block reads is published by an earlier tail row.
        published = set()
        for row in tail:
            parameters = row["parameters"]
            key = parameters.get("fluid_source")
            if key:
                self.assertIn(key, published, row["id"])
            if row["capability"] == "fluid_distribution":
                self.assertIn(parameters["source_key"], published, row["id"])
            if row["capability"] in ("water_gathering", "oil_gathering") and parameters.get("source_key"):
                published.add(parameters["source_key"])
            if row["capability"] == "fluid_distribution":
                published.update(parameters["aliases"])
            if row["capability"] == "advanced_petroleum":
                published.update(("heavy-oil", "light-oil", "advanced-petroleum-gas"))
            if row["capability"] == "fluid_product":
                published.add(parameters["product"])


if __name__ == "__main__":
    unittest.main()
