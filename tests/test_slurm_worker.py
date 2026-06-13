import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.slurm_worker import run_task_file, run_worker


class SlurmWorkerTests(unittest.TestCase):
    def test_worker_processes_planner_request_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "queue").mkdir()
            task = {
                "id": "planner-test",
                "type": "planner_request",
                "payload": {
                    "goal": "produce_iron_plate",
                    "observation": {"inventory": {}},
                    "legal_actions": [{"type": "wait", "ticks": 60}],
                },
            }
            (root / "queue" / "planner-test.json").write_text(json.dumps(task), encoding="utf-8")
            run_worker(root, once=True)
            result = json.loads((root / "results" / "planner-test.json").read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "heuristic")
            self.assertEqual(result["action_hint"]["type"], "wait")

    def test_run_task_file_writes_result_for_auto_dispatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_path = root / "planner-test.json"
            result_path = root / "planner-result.json"
            task = {
                "id": "planner-test",
                "type": "planner_request",
                "payload": {
                    "goal": "produce_iron_plate",
                    "observation": {"inventory": {}},
                    "legal_actions": [{"type": "wait", "ticks": 60}],
                },
            }
            task_path.write_text(json.dumps(task), encoding="utf-8")
            result = run_task_file(task_path, result_path)
            self.assertTrue(result["ok"])
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["source"], "heuristic")

    def test_run_task_file_handles_strategy_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_path = root / "strategy-test.json"
            result_path = root / "strategy-result.json"
            task = {
                "id": "strategy-test",
                "type": "strategy_request",
                "payload": {
                    "objective": "전자회로를 만들어야함",
                    "observation": {"inventory": {"iron-plate": 1, "copper-plate": 40}, "entities": []},
                    "available_skills": [],
                },
            }
            task_path.write_text(json.dumps(task), encoding="utf-8")
            result = run_task_file(task_path, result_path)
            self.assertTrue(result["ok"])
            self.assertEqual(result["selected_skill"], "expand_iron_smelting")
            self.assertEqual(result["source"], "heuristic")

    def test_run_task_file_handles_layout_improvement_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_path = root / "layout-test.json"
            result_path = root / "layout-result.json"
            task = {
                "id": "layout-test",
                "type": "layout_improvement_request",
                "payload": {
                    "objective": "launch_rocket_program",
                    "active_skill": "bootstrap_build_item_mall",
                    "active_step": 3,
                    "observation": {
                        "inventory": {},
                        "entities": [
                            {
                                "name": "assembling-machine-1",
                                "unit_number": 10,
                                "recipe": "electronic-circuit",
                                "position": {"x": 0, "y": 0},
                                "electric_network_connected": True,
                                "inventories": {},
                            }
                        ],
                        "resources": [],
                    },
                    "production_targets": {},
                },
            }
            task_path.write_text(json.dumps(task), encoding="utf-8")
            result = run_task_file(task_path, result_path)
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "heuristic")
            self.assertEqual(result["selected_candidate_id"], "green-circuit-3-cable-2-circuit-cell")
            self.assertTrue(result["no_apply"])
            self.assertFalse(result["build_ready"])


if __name__ == "__main__":
    unittest.main()
