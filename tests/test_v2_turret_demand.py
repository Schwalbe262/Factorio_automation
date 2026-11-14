"""A job stopped on missing turret recipes pulls the gun-turret research forward (progression.turret_demand).

Fleets aud-1114-0752/0841: growth outposts and material banks blocked on
native_gun_turret_and_normal_magazine_recipes_required from ~24 min while gun-turret waited
for its roadmap place (queued at 42.8 min).
"""

import unittest

from factorio_agent.progression import URGENT_DEFENSE_PRIORITY, defense_urgency_nodes, turret_demand
from factorio_agent.supervisor import RETRY_CLEARED_BY

REASON = "native_gun_turret_and_normal_magazine_recipes_required"
CAPS = {
    "research": {"parameters": {}, "requires": []},
    "defense_perimeter": {"parameters": {}, "requires": ["starter"]},
}


def observation(technologies=None, level=0):
    return dict(
        tick=100000,
        technologies=technologies or {"automation-science-pack": True},
        adaptive_defense_risk=dict(level=level, risk="quiet", sizing=""),
        available_research={"technologies": [{"technology": "gun-turret"}]},
    )


class TurretDemandTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            dict(capability="starter", status="succeeded", parameters={}),
            dict(capability="dedicated_materials", status="blocked", reason=REASON, parameters={}),
        ]

    def test_blocked_job_names_the_demand(self):
        self.assertEqual(turret_demand(self.jobs), ["dedicated_materials"])
        done = [dict(j, status="succeeded") for j in self.jobs]
        self.assertEqual(turret_demand(done), [])

    def test_demand_pulls_only_the_research_forward(self):
        nodes = defense_urgency_nodes(self.jobs, [], observation(), CAPS)
        self.assertEqual(
            [(n["id"], n["capability"], n["priority"]) for n in nodes],
            [("auto_research_gun_turret", "research", URGENT_DEFENSE_PRIORITY)],
        )
        self.assertIn("dedicated_materials", nodes[0]["reason"])
        self.assertEqual(defense_urgency_nodes(self.jobs, nodes, observation(), CAPS), [])
        # Researched: the perimeter is not pulled forward by demand alone (no attack expected).
        done = observation(technologies={"automation-science-pack": True, "gun-turret": True})
        self.assertEqual(defense_urgency_nodes(self.jobs, [], done, CAPS), [])

    def test_no_demand_no_risk_keeps_the_roadmap_order(self):
        self.assertEqual(defense_urgency_nodes(self.jobs[:1], [], observation(), CAPS), [])

    def test_research_success_retries_the_blocked_job(self):
        self.assertIn("research", RETRY_CLEARED_BY[REASON])


if __name__ == "__main__":
    unittest.main()
