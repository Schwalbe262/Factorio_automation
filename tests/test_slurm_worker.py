import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.slurm_worker import run_worker


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


if __name__ == "__main__":
    unittest.main()
