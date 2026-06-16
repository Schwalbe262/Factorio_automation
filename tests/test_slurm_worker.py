import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_ai.slurm_worker import (
    _layout_improvement_prompt,
    call_llm_json_with_diagnostics,
    compact_layout_improvement_payload,
    normalize_layout_response,
    run_strategy_request,
    run_task_file,
    run_worker,
)
from factorio_ai.strategy import skill_catalog_payload


class FakeResponse:
    def __init__(self, body: dict[str, object]):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


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

    def test_llm_call_diagnostics_include_success_trace_metadata(self):
        response = FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"selected_skill":"research_automation","priority":90,'
                                '"reason":"feed labs","evidence":[],"blockers":[],"expected_effect":"research"}'
                            )
                        }
                    }
                ]
            }
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "FACTORIO_AI_LLM_BASE_URL": "http://127.0.0.1:8000/v1",
                    "FACTORIO_AI_LLM_MODEL": "Qwen/Qwen3.5-9B",
                    "FACTORIO_AI_LLM_MAX_TOKENS": "384",
                },
            ),
            patch("factorio_ai.slurm_worker.request.urlopen", return_value=response),
        ):
            parsed, diagnostics = call_llm_json_with_diagnostics(
                "system prompt",
                "input prompt",
                kind="strategy",
                task_id="strategy-test",
            )

        trace = diagnostics["llm_trace"]
        self.assertEqual(parsed["selected_skill"], "research_automation")
        self.assertTrue(trace["ok"])
        self.assertEqual(trace["kind"], "strategy")
        self.assertEqual(trace["model"], "Qwen/Qwen3.5-9B")
        self.assertEqual(trace["base_url"], "http://127.0.0.1:8000/v1")
        self.assertEqual(trace["task_id"], "strategy-test")
        self.assertEqual(trace["system_prompt"], "system prompt")
        self.assertEqual(trace["input_prompt"], "input prompt")
        self.assertIn("research_automation", trace["raw_output"])
        self.assertEqual(trace["parsed_json"]["selected_skill"], "research_automation")
        self.assertEqual(trace["max_tokens"], 384)
        self.assertEqual(trace["prompt_chars"], len("system prompt") + len("input prompt"))
        self.assertGreater(trace["response_chars"], 0)

    def test_llm_call_diagnostics_include_error_trace_metadata(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "FACTORIO_AI_LLM_BASE_URL": "http://127.0.0.1:8000/v1",
                    "FACTORIO_AI_LLM_MODEL": "Qwen/Qwen3.5-9B",
                },
            ),
            patch("factorio_ai.slurm_worker.request.urlopen", side_effect=TimeoutError("slow")),
        ):
            parsed, diagnostics = call_llm_json_with_diagnostics("system", "prompt", kind="layout")

        trace = diagnostics["llm_trace"]
        self.assertIsNone(parsed)
        self.assertFalse(trace["ok"])
        self.assertEqual(trace["kind"], "layout")
        self.assertIn("TimeoutError", trace["error"])
        self.assertEqual(trace["raw_output"], "")

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
            self.assertEqual(result["selected_skill"], "produce_iron_plate")
            self.assertEqual(result["source"], "heuristic")

    def test_strategy_request_guardrail_promotes_hand_circuit_llm_choice_to_red_science_research(self):
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
        self.assertEqual(result["selected_skill"], "research_logistics")
        self.assertEqual(result["guardrail_adjusted"]["to"], "research_logistics")

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
            self.assertIn("site_prebuild_gate=fail", " ".join(result["risks"]))
            self.assertIn("site_placement_search=blocked", " ".join(result["risks"]))

    def test_layout_prompt_repeats_json_only_instruction_after_payload(self):
        prompt = _layout_improvement_prompt(
            {
                "objective": "launch_rocket_program",
                "active_skill": "idle:test",
                "layout_improvement": {"simulation_candidates": [{"id": "candidate-a"}]},
            }
        )

        payload_index = prompt.index("Payload JSON for evaluation")
        final_index = prompt.index("Now answer with exactly one JSON object")
        self.assertGreater(final_index, payload_index)
        self.assertIn("Do not repeat, quote, summarize, or continue the payload", prompt[final_index:])
        self.assertIn("The first character of your response must be `{`", prompt[final_index:])

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
        self.assertTrue(compact["layout_learning"]["return_learned_skills"])
        self.assertTrue(compact["layout_learning"]["record_only_confirmed"])
        self.assertIn("learned_skills", " ".join(compact["rules"]))
        self.assertIn("corner tile", " ".join(compact["rules"]))
        self.assertIn("drop into the consumer", " ".join(compact["rules"]))

    def test_compact_layout_improvement_payload_preserves_selected_site(self):
        compact = compact_layout_improvement_payload(
            {
                "objective": "launch_rocket_program",
                "active_skill": "produce_iron_plate",
                "active_step": 1,
                "observation": {"inventory": {}, "entities": []},
                "production_targets": {},
                "factory_monitor": {"factory_sites": [], "logistics_links": []},
                "selected_improvement_site": {
                    "site_id": "build_item_mall:2,2",
                    "kind": "build_item_mall",
                    "item": "transport-belt",
                },
            }
        )

        self.assertEqual(compact["selected_improvement_site"]["site_id"], "build_item_mall:2,2")
        self.assertEqual(
            compact["layout_improvement"]["selected_improvement_site"]["site_id"],
            "build_item_mall:2,2",
        )
        self.assertEqual(compact["layout_improvement"]["opportunities"][0]["kind"], "operator_selected_site")

    def test_layout_payload_preserves_long_handed_candidate_supply_for_qwen(self):
        compact = compact_layout_improvement_payload(
            {
                "objective": "launch_rocket_program",
                "active_skill": "bootstrap_build_item_mall",
                "active_step": 2,
                "observation": {
                    "inventory": {},
                    "recipe_unlocks": {"long-handed-inserter": {"enabled": True}},
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
            }
        )

        candidates = compact["layout_improvement"]["simulation_candidates"]
        candidate = next(
            item
            for item in candidates
            if item["candidate_id"] == "green-circuit-long-handed-3-cable-2-circuit-cell"
        )
        self.assertIn("long-handed-inserter", candidate["uses_unlocked_items"])
        self.assertIn("long-handed-inserter", candidate["considered_unlocked_items"])
        self.assertTrue(candidate["used_unlocked_item_state"]["long-handed-inserter"]["recipe_unlocked"])
        self.assertEqual(candidate["used_unlocked_item_state"]["long-handed-inserter"]["stock"], 0)
        self.assertFalse(candidate["used_unlocked_item_state"]["long-handed-inserter"]["automated"])
        self.assertEqual(candidate["build_item_supply"]["status"], "fail")
        self.assertEqual(
            candidate["build_item_supply"]["used_unlocked_item_supply"]["long-handed-inserter"]["missing"],
            7,
        )

    def test_layout_payload_includes_sandbox_validation_feedback(self):
        feedback = {
            "entry_count": 1,
            "latest_by_candidate": {
                "green-circuit-3-cable-2-circuit-cell": {
                    "timestamp": "2026-06-14T00:00:00+00:00",
                    "candidate_id": "green-circuit-3-cable-2-circuit-cell",
                    "variant": "after",
                    "sandbox_validation": {
                        "status": "fail",
                        "reasons": ["expected output electronic-circuit was not observed after sandbox ticks"],
                        "observed_outputs": {"electronic-circuit": 0},
                        "ticks": 3600,
                        "checked_machines": 5,
                    },
                    "lesson": "Do not mark green circuit layouts build-ready until sandbox ticks prove output.",
                }
            },
        }
        compact = compact_layout_improvement_payload(
            {
                "objective": "launch_rocket_program",
                "active_skill": "bootstrap_build_item_mall",
                "active_step": 4,
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
                "layout_validation_feedback": feedback,
            }
        )

        candidate = compact["layout_improvement"]["simulation_candidates"][0]
        self.assertEqual(candidate["sandbox_validation"]["status"], "fail")
        self.assertEqual(candidate["site_prebuild_gate"]["status"], "fail")
        self.assertFalse(candidate["site_prebuild_gate"]["build_ready"])
        self.assertIn("build_items", candidate["site_prebuild_gate"]["checks"])
        self.assertEqual(candidate["site_placement_search"]["status"], "blocked")
        self.assertIn("selected_anchor", candidate["site_placement_search"])
        self.assertIn("sandbox validation feedback must pass", candidate["build_ready_blockers"][0])
        self.assertIn("sandbox ticks prove output", candidate["sandbox_validation_lesson"])
        self.assertEqual(compact["layout_validation_feedback"]["entry_count"], 1)
        self.assertIn("sandbox_validation is fail", " ".join(compact["rules"]))
        self.assertIn("site_prebuild_gate is fail", " ".join(compact["rules"]))
        self.assertIn("site_placement_search", " ".join(compact["rules"]))

    def test_normalize_layout_response_preserves_confirmed_learned_skills(self):
        result = normalize_layout_response(
            {
                "selected_candidate_id": "gear-belt-direct-transfer-cell",
                "score": 92,
                "reasoning": "direct transfer is shorter",
                "expected_improvements": ["removes unnecessary belt hop"],
                "risks": [],
                "next_simulation_focus": "try the same producer-consumer pattern on science packs",
                "learned_skills": [
                    {
                        "name": "direct assembler transfer",
                        "trigger": "nearby producer and consumer assemblers",
                        "lesson": "Use a direct inserter transfer when both assemblers are within inserter reach.",
                        "evidence": "The candidate keeps gears off belts and still feeds the belt assembler.",
                        "confirmed": True,
                        "confidence": 0.88,
                    },
                    "ignore unstructured strings",
                ],
                "build_ready": False,
                "no_apply": True,
            }
        )

        self.assertEqual(result["learned_skills"][0]["name"], "direct assembler transfer")
        self.assertTrue(result["learned_skills"][0]["confirmed"])
        self.assertAlmostEqual(result["learned_skills"][0]["confidence"], 0.88)


if __name__ == "__main__":
    unittest.main()
