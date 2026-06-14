import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_ai.slurm_worker import compact_layout_improvement_payload, run_strategy_request, run_task_file, run_worker
from factorio_ai.strategy import skill_catalog_payload


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

    def test_strategy_request_guardrail_promotes_hand_circuit_llm_choice(self):
        with patch(
            "factorio_ai.slurm_worker.call_llm_json_with_diagnostics",
            return_value=(
                {
                    "selected_skill": "produce_electronic_circuit",
                    "priority": 50,
                    "reason": "Need more electronic circuits.",
                    "evidence": [],
                    "blockers": [],
                    "expected_effect": "",
                },
                {},
            ),
        ):
            result = run_strategy_request(
                {
                    "objective": "launch_rocket_program",
                    "observation": {
                        "inventory": {"iron-plate": 20, "copper-plate": 20},
                        "entities": [],
                        "research": {"technologies": {"automation": {"researched": True}}},
                    },
                    "production_targets": {"electronic-circuit": 20.0},
                    "available_skills": [],
                }
            )

        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")
        self.assertEqual(result["guardrail_adjusted"]["to"], "automate_electronic_circuit_line")

    def test_strategy_request_uses_precomputed_strategy_payload_for_prompt(self):
        with patch(
            "factorio_ai.slurm_worker.call_llm_json_with_diagnostics",
            return_value=(
                {
                    "selected_skill": "expand_iron_smelting",
                    "priority": 95,
                    "reason": "Iron plate is the remaining target deficit.",
                    "evidence": ["iron-plate deficit"],
                    "blockers": ["iron-plate"],
                    "expected_effect": "Increase iron plate throughput.",
                },
                {},
            ),
        ) as llm:
            result = run_strategy_request(
                {
                    "objective": "launch_rocket_program",
                    "observation": {"inventory": {}, "entities": [], "research": {"technologies": {}}},
                    "production_targets": {"copper-plate": 70.0, "iron-plate": 90.0},
                    "strategy_payload": {
                        "objective": "launch_rocket_program",
                        "observation": {"inventory": {}, "entities": [], "research": {"technologies": {}}},
                        "production_targets": {"copper-plate": 70.0, "iron-plate": 90.0},
                        "factory_monitor": {
                            "inventory": {},
                            "target_status": {
                                "items": [
                                    {
                                        "item": "copper-plate",
                                        "target_per_minute": 70.0,
                                        "estimated_per_minute": 75.0,
                                        "deficit_per_minute": 0.0,
                                        "satisfied": True,
                                    },
                                    {
                                        "item": "iron-plate",
                                        "target_per_minute": 90.0,
                                        "estimated_per_minute": 75.0,
                                        "deficit_per_minute": 15.0,
                                        "satisfied": False,
                                    },
                                ]
                            },
                            "bottlenecks": [
                                {
                                    "item": "iron-plate",
                                    "reason": "target deficit: needs 90.0/min, estimated 75.0/min",
                                    "stock": 0,
                                    "estimated_per_minute": 75.0,
                                    "severity": 108,
                                    "required_by": ["transport-belt"],
                                }
                            ],
                            "factory_sites": [],
                            "logistics_links": [],
                        },
                        "available_skills": skill_catalog_payload(),
                    },
                    "available_skills": skill_catalog_payload(),
                }
            )

        prompt = llm.call_args.kwargs["prompt"]
        self.assertIn('"item": "copper-plate"', prompt)
        self.assertIn('"estimated_per_minute": 75.0', prompt)
        self.assertIn('"deficit_per_minute": 0.0', prompt)
        self.assertIn("Iron plate is the remaining target deficit", result["reason"])

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

    def test_layout_payload_marks_codex_wait_context(self):
        compact = compact_layout_improvement_payload(
            {
                "objective": "launch_rocket_program",
                "active_skill": "codex_wait:future_build_item_skill",
                "active_step": 0,
                "observation": {"inventory": {}, "entities": [], "resources": []},
                "production_targets": {},
            }
        )

        self.assertEqual(compact["active_skill"], "codex_wait:future_build_item_skill")
        self.assertIn("Codex reports that the executor work is complete", compact["instruction"])
        self.assertIn("simulation only", compact["instruction"])


if __name__ == "__main__":
    unittest.main()
