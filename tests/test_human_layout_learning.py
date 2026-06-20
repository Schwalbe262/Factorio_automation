import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.human_layout_learning import (
    human_layout_learning_trace_path,
    record_human_layout_observation,
    remember_agent_layout_action,
)


def observation_with_entities(*entities):
    return {
        "tick": 1,
        "player": {"position": {"x": 0, "y": 0}},
        "inventory": {},
        "entities": list(entities),
        "resources": [],
        "research": {"technologies": {}},
    }


class HumanLayoutLearningTests(unittest.TestCase):
    def test_records_pending_sample_for_unexplained_site_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities()
            after = observation_with_entities(
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "position": {"x": 4, "y": 2},
                    "recipe": "transport-belt",
                    "electric_network_connected": True,
                    "inventories": {},
                }
            )

            first = record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="idle",
                active_step=0,
                source="test",
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="idle",
                active_step=1,
                source="test",
            )

            rows = [json.loads(line) for line in human_layout_learning_trace_path(logs).read_text(encoding="utf-8").splitlines()]

        self.assertIsNone(first)
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "operator_intervention_candidate")
        self.assertEqual(event["learning_label"], "pending_human_review")
        self.assertEqual(event["delta_summary"]["added"], 1)
        self.assertIn("before_structure", event)
        self.assertEqual(rows[-1]["delta"]["added"][0]["recipe"], "transport-belt")

    def test_does_not_record_agent_explained_single_build_as_human_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities()
            after = observation_with_entities(
                {
                    "name": "wooden-chest",
                    "unit_number": 11,
                    "position": {"x": 2.5, "y": 0.5},
                    "inventories": {},
                }
            )

            record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="setup_coal_supply",
                active_step=1,
                source="test",
            )
            remember_agent_layout_action(
                runtime,
                {"type": "build", "name": "wooden-chest", "position": {"x": 2.5, "y": 0.5}},
                objective="launch_rocket_program",
                active_skill="setup_coal_supply",
                active_step=1,
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="setup_coal_supply",
                active_step=2,
                source="test",
            )

        self.assertIsNone(event)
        self.assertFalse(human_layout_learning_trace_path(logs).exists())

    def test_does_not_record_nearby_agent_build_adjustment_as_human_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities()
            after = observation_with_entities(
                {
                    "name": "small-electric-pole",
                    "unit_number": 72,
                    "position": {"x": -39.5, "y": 17.5},
                    "inventories": {},
                }
            )

            record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="research_automation",
                active_step=44,
                source="test",
            )
            remember_agent_layout_action(
                runtime,
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": {"x": -37.5, "y": 19.5},
                    "allow_nearby": True,
                },
                objective="launch_rocket_program",
                active_skill="research_automation",
                active_step=45,
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="research_automation",
                active_step=46,
                source="test",
            )

        self.assertIsNone(event)
        self.assertFalse(human_layout_learning_trace_path(logs).exists())

    def test_tick_reset_refreshes_baseline_without_human_intervention_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities(
                {"name": "stone-furnace", "unit_number": 20, "position": {"x": 10, "y": 0}, "inventories": {}},
            )
            before["tick"] = 5000
            after = observation_with_entities(
                {"name": "burner-mining-drill", "unit_number": 1, "position": {"x": 0, "y": 0}, "inventories": {}},
            )
            after["tick"] = 10

            record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="idle",
                active_step=0,
                source="test",
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="idle",
                active_step=1,
                source="test",
            )

        self.assertIsNone(event)
        self.assertFalse(human_layout_learning_trace_path(logs).exists())

    def test_ignores_furnace_recipe_observation_flicker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities(
                {
                    "name": "stone-furnace",
                    "unit_number": 20,
                    "position": {"x": 10, "y": 0},
                    "recipe": None,
                    "inventories": {},
                },
            )
            after = observation_with_entities(
                {
                    "name": "stone-furnace",
                    "unit_number": 20,
                    "position": {"x": 10, "y": 0},
                    "recipe": "iron-plate",
                    "inventories": {},
                },
            )

            record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="research_automation",
                active_step=3,
                source="test",
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="research_automation",
                active_step=4,
                source="test",
            )

        self.assertIsNone(event)
        self.assertFalse(human_layout_learning_trace_path(logs).exists())

    def test_ignores_environment_entities_entering_observe_radius(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            logs = root / "logs"
            before = observation_with_entities(
                {"name": "big-rock", "position": {"x": 10, "y": 0}},
            )
            after = observation_with_entities(
                {"name": "big-rock", "position": {"x": 10, "y": 0}},
                {"name": "tree-08-brown", "position": {"x": 12, "y": 0}},
                {"name": "dead-grey-trunk", "position": {"x": 14, "y": 0}},
            )

            record_human_layout_observation(
                runtime,
                logs,
                before,
                objective="launch_rocket_program",
                active_skill="produce_iron_plate",
                active_step=1,
                source="test",
            )
            event = record_human_layout_observation(
                runtime,
                logs,
                after,
                objective="launch_rocket_program",
                active_skill="produce_iron_plate",
                active_step=2,
                source="test",
            )

        self.assertIsNone(event)
        self.assertFalse(human_layout_learning_trace_path(logs).exists())


if __name__ == "__main__":
    unittest.main()
