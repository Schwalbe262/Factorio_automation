"""eval-1575138435 at ~440 game minutes: plastics research waited for good on the green proof.

Labs held green packs and stood ``no_research_in_progress`` while the job waited "for commissioned
green supply and powered labs to recover". The green proof failed on a bus trunk belt of the green
block's own lane extension (82.5,-119.5) that the steel block's later tap (splitter 82.0,-119.5)
had replaced. A read-only probe with the fix: the green proof passes and the step queues plastics.
"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent import planned_tap
from factorio_agent.green_science import LogisticScience, starved_inputs
from factorio_agent.normal_research import NormalResearch

SPLITTER = {"name": "splitter", "position": {"x": 82.0, "y": -119.5}, "direction": 0}
TRUNK_BELT = {"name": "transport-belt", "position": {"x": 82.5, "y": -119.5}, "direction": 0}


def bus_state(built=True):
    return {
        "world_id": "w",
        "taps": [
            {
                "name": "steel_production:iron-plate",
                "owner": "steel_production",
                "built": built,
                "splitter": dict(SPLITTER, splitter_output_priority="left"),
                "remove": [{"x": 82.5, "y": -119.5}],
            }
        ],
    }


class ReleasedTileTests(unittest.TestCase):
    def test_a_built_tap_releases_the_trunk_belt_it_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / planned_tap.STATE_FILE).write_text(json.dumps(bus_state()), encoding="utf-8")
            released = planned_tap.released_tiles(folder, "w")
            self.assertEqual(released, {(82.5, -119.5): SPLITTER})
            actual = {("splitter", 82.0, -119.5): dict(SPLITTER)}
            self.assertTrue(planned_tap.released_belt_stands(actual, released, TRUNK_BELT))
            self.assertFalse(planned_tap.released_belt_stands({}, released, TRUNK_BELT))
            self.assertEqual(planned_tap.released_tiles(folder, "other"), {})

    def test_an_unbuilt_tap_releases_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / planned_tap.STATE_FILE).write_text(json.dumps(bus_state(False)), encoding="utf-8")
            self.assertEqual(planned_tap.released_tiles(folder, "w"), {})

    def test_the_green_proof_passes_a_trunk_belt_a_later_tap_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / planned_tap.STATE_FILE).write_text(json.dumps(bus_state()), encoding="utf-8")
            green = LogisticScience.__new__(LogisticScience)
            green.path = root / "logistic-science.json"
            green.state = {
                "world_id": "w",
                "plan": {"entities": [TRUNK_BELT]},
                "lab_join": {
                    "name": "transport-belt",
                    "position": {"x": 144.5, "y": -74.5},
                    "direction": 12,
                    "unit_number": 2394,
                },
            }
            obs = {"world_id": "w", "entities": [dict(SPLITTER, unit_number=7000)]}
            self.assertIsNone(green.proof(obs))
            # Past the plan check: the next check (the lab join, absent here) names the failure.
            self.assertEqual(green.last_proof_failure["reason"], "green_lab_join_changed")
            (root / planned_tap.STATE_FILE).write_text(json.dumps(bus_state(False)), encoding="utf-8")
            self.assertIsNone(green.proof(obs))
            self.assertEqual(green.last_proof_failure["reason"], "green_planned_entity_missing")


CATALOG = SimpleNamespace(
    recipes={
        "logistic-science-pack": {"ingredients": [{"name": "inserter"}, {"name": "transport-belt"}]},
        "inserter": {
            "ingredients": [{"name": "electronic-circuit"}, {"name": "iron-gear-wheel"}, {"name": "iron-plate"}]
        },
        "iron-gear-wheel": {"ingredients": [{"name": "iron-plate"}]},
    },
    recipe_for_product=lambda item: {
        "transport-belt": {"ingredients": [{"name": "iron-gear-wheel"}, {"name": "iron-plate"}]},
        "iron-gear-wheel": {"ingredients": [{"name": "iron-plate"}]},
        "electronic-circuit": {"ingredients": [{"name": "copper-cable"}, {"name": "iron-plate"}]},
    }.get(item),
)


def research():
    found = NormalResearch.__new__(NormalResearch)
    found.catalog = CATALOG
    return found


class SupplyGateTests(unittest.TestCase):
    def test_starved_green_machines_name_the_plate_gate(self):
        machines = [
            {"recipe": "inserter", "status_name": "item_ingredient_shortage"},
            {"recipe": "logistic-science-pack", "status_name": "working"},
        ]
        failure = {"reason": "no_green_on_the_lab_lane", **starved_inputs(machines)}
        wait = research()._supply_wait("green", "logistic-science-pack", failure)
        self.assertEqual(wait["status"], "waiting")
        self.assertEqual(wait["evidence"]["gate"], "plates_wait")
        self.assertEqual(wait["evidence"]["plates"], ["iron-plate"])
        # tools/waste_report.py counts "waiting for .*-plate" reasons as plate-bound.
        self.assertRegex(wait["reason"], r"waiting for .*-plate")

    def test_a_broken_green_block_names_the_failed_check(self):
        wait = research()._supply_wait("green", "logistic-science-pack", {"reason": "green_planned_entity_missing"})
        self.assertEqual(wait["evidence"]["gate"], "green_proof")
        self.assertIn("green_planned_entity_missing", wait["reason"])
        self.assertNotRegex(wait["reason"], r"waiting for .*-plate")

    def test_empty_labs_without_starved_machines_wait_for_packs(self):
        wait = research()._supply_wait("green", "logistic-science-pack", {"reason": "labs_without_green_packs"})
        self.assertEqual(wait["evidence"]["gate"], "packs_wait")

    def test_starved_inputs_reads_native_status(self):
        self.assertEqual(starved_inputs([{"recipe": "inserter", "status_name": "full_output"}]), {})
        self.assertEqual(
            starved_inputs([{"recipe": "iron-gear-wheel", "status_name": "no_ingredients"}]),
            {"starved_recipes": ["iron-gear-wheel"]},
        )


if __name__ == "__main__":
    unittest.main()
