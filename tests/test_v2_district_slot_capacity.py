"""A queued district that waits only for a job slot gets it before new plate starter capacity.

aud-1114-0841: the district was proposed early at ~11.9 min, but new iron/copper starter
capacity nodes (CAPACITY_PRIORITY, above every roadmap step) took each freed slot, so the
district was admitted only when the last outpost ended (17.6-20.9 min in three runs).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent import progression as pg
from factorio_agent.jobs import CAPABILITIES


def job(capability, status, **parameters):
    return {"capability": capability, "status": status, "parameters": parameters}


BASE = [job("starter", "succeeded"), job("power", "succeeded")]
OUTPOSTS = [job("mining_outpost", "running", resource=r) for r in ("iron-ore", "copper-ore", "coal")]
DISTRICT_NODE = {"id": "auto_smelting_district", "capability": "smelting_district", "status": "ready"}


def decision(resource, target, current):
    return SimpleNamespace(resource=resource, target_cells=target, current_cells=current, reason="need")


class DistrictSlotTests(unittest.TestCase):
    def test_waits_for_a_slot_only_once_every_missing_requirement_is_active(self):
        jobs = BASE + OUTPOSTS + [job("bootstrap_research", "running", technology="electric-mining-drill")]
        # logistics (a strict requirement) has no job yet: the early start keeps the slot free anyway
        self.assertFalse(pg.district_waits_for_slot(jobs, [DISTRICT_NODE], CAPABILITIES))
        jobs = BASE + OUTPOSTS + [job("bootstrap_logistics", "running")]
        self.assertTrue(pg.district_waits_for_slot(jobs, [DISTRICT_NODE], CAPABILITIES))
        self.assertFalse(pg.district_waits_for_slot(jobs, [], CAPABILITIES))
        self.assertFalse(pg.district_waits_for_slot(jobs, [dict(DISTRICT_NODE, status="blocked")], CAPABILITIES))
        self.assertFalse(pg.district_waits_for_slot(jobs, [dict(DISTRICT_NODE, paused=True)], CAPABILITIES))

    def test_no_new_plate_capacity_while_the_district_waits_for_a_slot(self):
        jobs = BASE + OUTPOSTS + [job("bootstrap_logistics", "running")]
        decisions = [decision("iron-ore", 12, 8), decision("copper-ore", 4, 3), decision("stone", 3, 2)]
        observation = {"technologies": {}}
        with (
            patch.object(pg, "plan_capacity", return_value=decisions),
            patch("factorio_agent.capacity_planning.demand_driven", return_value=True),
        ):
            free = pg.capacity_nodes(jobs, [], observation, CAPABILITIES)
            held = pg.capacity_nodes(jobs, [DISTRICT_NODE], observation, CAPABILITIES)
        self.assertEqual({n["parameters"]["resource"] for n in free}, {"iron-ore", "copper-ore", "stone"})
        self.assertEqual({n["parameters"]["resource"] for n in held}, {"stone"})

    def test_same_pass_district_drops_plate_capacity_nodes(self):
        proposed = [
            {"capability": "starter_capacity", "parameters": {"resource": "iron-ore", "cells": 12}},
            {"capability": "starter_capacity", "parameters": {"resource": "coal", "cells": 5}},
            dict(DISTRICT_NODE),
        ]
        kept = pg._without_plate_capacity(proposed)
        self.assertEqual([n["capability"] for n in kept], ["starter_capacity", "smelting_district"])
        self.assertEqual(kept[0]["parameters"]["resource"], "coal")


if __name__ == "__main__":
    unittest.main()
