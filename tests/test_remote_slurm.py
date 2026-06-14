import json
import unittest
from unittest.mock import patch

from factorio_ai.remote_slurm import (
    RemoteSlurmConfig,
    _attached_env_setup,
    _gpu_allocation_visible,
    _llm_status_remediation,
    _slurm_time_left_seconds,
    _status_needs_local_gpu,
    _worker_env_values,
    compare_strategy_workers,
    config,
    ensure_worker_job,
    llm_status,
    parse_strategy_worker_specs,
    request_strategy,
)
from factorio_ai.slurm_worker import (
    compact_strategy_payload,
    parse_json_object_from_content,
    run_strategy_model_benchmark,
    run_strategy_request,
    try_llm_strategy_with_diagnostics,
)
from factorio_ai.strategy import make_strategy_payload, skill_catalog_payload


class RemoteSlurmTests(unittest.TestCase):
    def test_llm_status_remediation_explains_required_env(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/kakao-bot-worker",
            job_name="AUTO",
            conda_env="factorio-ai",
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remediation = _llm_status_remediation(["FACTORIO_AI_LLM_BASE_URL"], cfg, True, {"count": 1})
        self.assertIsNotNone(remediation)
        self.assertIn("FACTORIO_AI_LLM_BASE_URL", remediation["required_remote_env"])
        self.assertEqual(remediation["job_name"], "AUTO")
        self.assertTrue(remediation["vllm_available_in_job"])
        self.assertEqual(remediation["required_gpu_allocation"]["sbatch_option"], "--gres=gpu:1")

    def test_llm_status_remediation_marks_missing_gpu(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/kakao-bot-worker",
            job_name="AUTO",
            conda_env="factorio-ai",
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remediation = _llm_status_remediation(
            ["GPU allocation"],
            cfg,
            False,
            {"count": 0, "env": {"CUDA_VISIBLE_DEVICES": "none"}},
        )
        self.assertTrue(remediation["required_gpu_allocation"]["needed"])
        self.assertIn("FACTORIO_AI_SLURM_GPUS_PER_NODE=1", remediation["required_gpu_allocation"]["factorio_worker_env"])

    def test_llm_status_retries_transient_attach_failure(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="AUTO",
            conda_env="factorio-ai",
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remote_payload = {
            "env": {"FACTORIO_AI_LLM_BASE_URL": True, "FACTORIO_AI_LLM_MODEL": True},
            "env_values": {
                "FACTORIO_AI_LLM_BASE_URL": "http://127.0.0.1:8000/v1",
                "FACTORIO_AI_LLM_MODEL": "Qwen/Qwen3.5-4B",
            },
            "vllm_command": False,
            "factorio_ai_deployed": True,
            "llm_endpoint": {"configured": True, "models_ok": True, "model_visible": True},
            "gpu": {"count": 1, "env": {"CUDA_VISIBLE_DEVICES": "0"}},
        }
        with (
            patch(
                "factorio_ai.remote_slurm._run_remote",
                side_effect=["/home/user/factorio-ai-worker", RuntimeError("temporary srun busy"), json.dumps(remote_payload)],
            ),
            patch("factorio_ai.remote_slurm.time.sleep") as sleep,
        ):
            status = llm_status(cfg)

        self.assertTrue(status["llm_ready"])
        sleep.assert_called_once_with(2)

    def test_llm_status_remediation_marks_pending_gpu_allocation(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/kakao-bot-worker",
            job_name="AUTO",
            conda_env="factorio-ai",
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=3,
            gres="gpu:a6000ada:3",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remediation = _llm_status_remediation(
            ["Slurm worker job pending GPU allocation", "GPU allocation"],
            cfg,
            False,
            None,
        )
        self.assertIn("not allocated the requested GPUs yet", remediation["why"])
        self.assertTrue(remediation["required_gpu_allocation"]["needed"])

    def test_config_defaults_to_factorio_owned_worker(self):
        with patch.dict("os.environ", {"USERPROFILE": "C:\\Users\\Test"}, clear=True):
            cfg = config()
        self.assertEqual(cfg.remote_dir, "~/factorio-ai-worker")
        self.assertEqual(cfg.job_name, "factorio-ai-worker")
        self.assertEqual(cfg.gres, "gpu:1")

    def test_config_prefers_factorio_remote_dir_and_typed_gres(self):
        with patch.dict(
            "os.environ",
            {
                "SUPERCOMPUTER_WORKER_REMOTE_DIR": "~/kakao-bot-worker",
                "FACTORIO_AI_SLURM_REMOTE_DIR": "~/factorio-ai-worker",
                "FACTORIO_AI_SLURM_GPUS_PER_NODE": "3",
                "FACTORIO_AI_SLURM_GRES": "gpu:a6000ada:3",
                "USERPROFILE": "C:\\Users\\Test",
            },
            clear=True,
        ):
            cfg = config()
        self.assertEqual(cfg.remote_dir, "~/factorio-ai-worker")
        self.assertEqual(cfg.gpus_per_node, 3)
        self.assertEqual(cfg.gres, "gpu:a6000ada:3")

    def test_worker_env_values_derives_loopback_endpoint_for_vllm(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="factorio-ai-worker",
            conda_env="factorio-ai",
            partition="gpu3",
            cpus_per_task=8,
            gpus_per_node=3,
            gres="gpu:a6000ada:3",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        with patch.dict("os.environ", {"FACTORIO_AI_VLLM_MODEL": "Qwen/test", "FACTORIO_AI_VLLM_PORT": "8001"}, clear=True):
            values = _worker_env_values(cfg)
        self.assertEqual(values["FACTORIO_AI_SLURM_CONDA_ENV"], "factorio-ai")
        self.assertEqual(values["FACTORIO_AI_LLM_MODEL"], "Qwen/test")
        self.assertEqual(values["FACTORIO_AI_LLM_BASE_URL"], "http://127.0.0.1:8001/v1")

    def test_gpu_allocation_visible_from_nvidia_or_slurm_env(self):
        self.assertTrue(_gpu_allocation_visible({"count": 1, "env": {}}))
        self.assertTrue(_gpu_allocation_visible({"count": 0, "env": {"SLURM_JOB_GPUS": "0"}}))
        self.assertFalse(_gpu_allocation_visible({"count": 0, "env": {"CUDA_VISIBLE_DEVICES": "none"}}))

    def test_slurm_time_left_parser_supports_squeue_formats(self):
        self.assertEqual(_slurm_time_left_seconds("13:29"), 809)
        self.assertEqual(_slurm_time_left_seconds("01:02:03"), 3723)
        self.assertEqual(_slurm_time_left_seconds("1-00:00:05"), 86405)
        self.assertIsNone(_slurm_time_left_seconds("UNLIMITED"))

    def test_ensure_worker_submits_dependent_successor_near_expiration(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="factorio-ai-worker",
            conda_env="factorio-ai",
            partition="gpu4",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        calls = []

        def fake_run_remote(script, _cfg, timeout=0):
            calls.append(script)
            if "squeue" in script:
                return "677569|RUNNING|23:46:31|13:29|n053|gres/gpu:1\n"
            return "submitted_job_id=677600\n"

        with (
            patch("factorio_ai.remote_slurm.deploy", return_value={"ok": True}),
            patch("factorio_ai.remote_slurm._run_remote", side_effect=fake_run_remote),
        ):
            result = ensure_worker_job(cfg, renew_before_minutes=180)

        self.assertEqual(result["action"], "submitted_dependent_successor")
        self.assertEqual(result["dependencyJobId"], "677569")
        self.assertIn("--dependency=afterany:677569", calls[-1])

    def test_ensure_worker_does_not_submit_when_pending_successor_exists(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="factorio-ai-worker",
            conda_env="factorio-ai",
            partition="gpu4",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        with (
            patch("factorio_ai.remote_slurm.deploy") as deploy,
            patch(
                "factorio_ai.remote_slurm._run_remote",
                return_value=(
                    "677569|RUNNING|23:46:31|13:29|n053|gres/gpu:1\n"
                    "677600|PENDING|0:00|1-00:00:00|Dependency|gres/gpu:1\n"
                ),
            ),
        ):
            result = ensure_worker_job(cfg, renew_before_minutes=180)

        self.assertEqual(result["action"], "pending_successor_exists")
        deploy.assert_not_called()

    def test_local_gpu_needed_for_vllm_or_loopback_endpoint(self):
        self.assertTrue(_status_needs_local_gpu({"FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B"}))
        self.assertTrue(_status_needs_local_gpu({"FACTORIO_AI_LLM_BASE_URL": "http://127.0.0.1:8000/v1"}))
        self.assertFalse(_status_needs_local_gpu({"FACTORIO_AI_LLM_BASE_URL": "https://llm.example/v1"}))

    def test_attached_env_setup_loads_remote_worker_config(self):
        setup = _attached_env_setup("/home/user/kakao-bot-worker")
        self.assertIn("/home/user/kakao-bot-worker/config.env", setup)
        self.assertIn("FACTORIO_AI_LLM_*|FACTORIO_AI_VLLM_*|FACTORIO_AI_CONDA_ENV", setup)
        self.assertIn('export "$key=$value"', setup)

    def test_strategy_model_benchmark_runs_same_payload_per_model(self):
        result = run_strategy_model_benchmark(
            {
                "models": ["Qwen/test-3B", "Qwen/test-7B"],
                "strategy_payload": {
                    "objective": "launch_rocket_program",
                    "observation": {"inventory": {}, "entities": [], "enemies": []},
                    "production_targets": {},
                },
            }
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["base_url_configured"])
        self.assertEqual([row["model"] for row in result["models"]], ["Qwen/test-3B", "Qwen/test-7B"])
        self.assertTrue(all(row["source"] == "heuristic" for row in result["models"]))
        self.assertTrue(all(row["llm_error"] for row in result["models"]))
        self.assertTrue(all(row["llm_prompt_chars"] for row in result["models"]))

    def test_strategy_llm_retries_with_ultra_compact_payload_on_context_limit(self):
        calls = []

        def fake_call(system, prompt, schema=None):
            calls.append(prompt)
            if len(calls) == 1:
                return None, {"llm_error": "This model's maximum context length is 4096 tokens"}
            return {"selected_skill": "expand_copper_smelting", "priority": 88, "reason": "copper deficit"}, {
                "llm_response_snippet": "{}"
            }

        payload = {
            "objective": "launch_rocket_program",
            "observation": {
                "inventory": {},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "transport-belt",
                        "electric_network_connected": True,
                    }
                ],
                "enemies": [],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            "production_targets": {"copper-plate": 70.0},
            "strategy_payload": {
                "objective": "launch_rocket_program",
                "observation": {
                    "inventory": {},
                    "entities": [
                        {
                            "name": "assembling-machine-1",
                            "recipe": "transport-belt",
                            "electric_network_connected": True,
                        }
                    ],
                    "enemies": [],
                    "research": {"technologies": {"automation": {"researched": True}}},
                },
                "production_targets": {"copper-plate": 70.0},
                "factory_monitor": {
                    "target_status": {
                        "items": [
                            {
                                "item": "copper-plate",
                                "target_per_minute": 70.0,
                                "estimated_per_minute": 0.0,
                                "deficit_per_minute": 70.0,
                            }
                        ]
                    },
                    "bottlenecks": [{"item": "copper-plate", "severity": 95, "reason": "target deficit"}],
                },
                "available_skills": skill_catalog_payload(),
            },
            "available_skills": skill_catalog_payload(),
        }

        with patch("factorio_ai.slurm_worker.call_llm_json_with_diagnostics", side_effect=fake_call):
            result, diagnostics = try_llm_strategy_with_diagnostics(payload)

        self.assertIsNotNone(result)
        self.assertEqual(result["selected_skill"], "expand_copper_smelting")
        self.assertEqual(diagnostics["llm_retry"], "ultra_compact_strategy_payload")
        self.assertIn("maximum context", diagnostics["llm_initial_error"])
        self.assertEqual(len(calls), 2)
        self.assertLess(len(calls[1]), len(calls[0]))

    def test_strategy_request_attaches_heuristic_support_for_sparse_llm_reasoning(self):
        observation = {
            "inventory": {"copper-plate": 1},
            "entities": [
                {"name": "stone-furnace", "inventories": {"1": {"coal": 1}, "2": {"copper-ore": 1}}},
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        with (
            patch(
                "factorio_ai.slurm_worker.try_llm_strategy_with_diagnostics",
                return_value=({"selected_skill": "expand_copper_smelting", "priority": 50, "reason": "", "evidence": []}, {}),
            ),
            patch(
                "factorio_ai.slurm_worker.heuristic_strategy",
                return_value={
                    "selected_skill": "expand_copper_smelting",
                    "priority": 95,
                    "reason": "Current factory monitor reports bottleneck for copper-plate",
                    "evidence": ["copper-plate_per_minute=0.0"],
                    "blockers": ["copper-plate"],
                },
            ),
        ):
            result = run_strategy_request(
                {
                    "objective": "launch_rocket_program",
                    "observation": observation,
                    "production_targets": {"copper-plate": 70.0},
                }
            )

        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["selected_skill"], "expand_copper_smelting")
        self.assertEqual(result["reason_source"], "heuristic_support")
        self.assertIn("copper-plate", result["reason"])
        self.assertTrue(result["quality_warning"])

    def test_request_strategy_sends_locally_computed_strategy_payload(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="factorio-ai-worker",
            conda_env="factorio-ai",
            partition="gpu4",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        observation = {
            "inventory": {},
            "entities": [
                {"name": "stone-furnace", "inventories": {"1": {"coal": 1}, "2": {"copper-ore": 1}}},
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        with (
            patch("factorio_ai.remote_slurm._use_attached_srun", return_value=True),
            patch("factorio_ai.remote_slurm._request_task_via_attached_srun", return_value={"ok": True}) as request,
        ):
            request_strategy(
                "launch_rocket_program",
                observation,
                production_targets={"copper-plate": 10.0},
                available_skills=skill_catalog_payload(),
                cfg=cfg,
                timeout_seconds=1,
            )

        task = request.call_args.args[0]
        payload = task["payload"]
        self.assertIn("strategy_payload", payload)
        self.assertEqual(payload["strategy_payload"]["factory_monitor"]["target_status"]["items"][0]["item"], "copper-plate")
        self.assertEqual(payload["strategy_payload"]["factory_monitor"]["target_status"]["items"][0]["estimated_per_minute"], 18.75)

    def test_parse_strategy_worker_specs_defaults_and_custom_specs(self):
        defaults = parse_strategy_worker_specs(None)
        self.assertEqual([item.label for item in defaults], ["4b", "9b", "27b"])

        specs = parse_strategy_worker_specs("small=~/factorio-ai-worker@factorio-ai-worker,big=~/big-worker@big-job")
        self.assertEqual(specs[0].label, "small")
        self.assertEqual(specs[0].remote_dir, "~/factorio-ai-worker")
        self.assertEqual(specs[0].job_name, "factorio-ai-worker")
        self.assertEqual(specs[1].label, "big")
        self.assertEqual(specs[1].remote_dir, "~/big-worker")
        self.assertEqual(specs[1].job_name, "big-job")

    def test_compare_strategy_workers_records_ready_and_unready_workers(self):
        cfg = RemoteSlurmConfig(
            enabled=True,
            ssh_path="ssh",
            scp_path="scp",
            host="example",
            user="user",
            port=22,
            key_path="key",
            remote_dir="~/factorio-ai-worker",
            job_name="factorio-ai-worker",
            conda_env="factorio-ai",
            partition="gpu4",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        specs = parse_strategy_worker_specs("4b=~/factorio-ai-worker@factorio-ai-worker,27b=~/factorio-ai-worker-27b@factorio-ai-worker-27b")

        def fake_status(worker_cfg):
            if worker_cfg.job_name == "factorio-ai-worker":
                return {
                    "llm_ready": True,
                    "remote": {
                        "env_values": {"FACTORIO_AI_LLM_MODEL": "Qwen/Qwen3.5-4B"},
                        "gpu": {"count": 1},
                    },
                }
            return {
                "llm_ready": False,
                "missing": ["LLM endpoint"],
                "remote": {
                    "env_values": {"FACTORIO_AI_LLM_MODEL": "Qwen/Qwen3.6-27B-FP8"},
                    "gpu": {"count": 3},
                },
            }

        with (
            patch("factorio_ai.remote_slurm.llm_status", side_effect=fake_status),
            patch(
                "factorio_ai.remote_slurm.request_strategy",
                return_value={"source": "llm", "selected_skill": "automate_electronic_circuit_line", "priority": 95},
            ) as request,
        ):
            result = compare_strategy_workers(
                objective="launch_rocket_program",
                observation={"inventory": {}, "entities": [], "enemies": []},
                workers=specs,
                cfg=cfg,
                timeout_seconds=1,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["workers"][0]["model"], "Qwen/Qwen3.5-4B")
        self.assertEqual(result["workers"][0]["selected_skill"], "automate_electronic_circuit_line")
        self.assertEqual(result["workers"][1]["model"], "Qwen/Qwen3.6-27B-FP8")
        self.assertIn("LLM endpoint", result["workers"][1]["error"])
        request.assert_called_once()

    def test_llm_content_parser_extracts_json_object_from_text(self):
        parsed = parse_json_object_from_content(
            'Here is the decision:\\n{"selected_skill":"produce_iron_plate","priority":"high"}\\nDone.'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["selected_skill"], "produce_iron_plate")
        self.assertEqual(parsed["priority"], "high")

    def test_compact_strategy_payload_keeps_prompt_small(self):
        verbose = {
            "objective": "launch_rocket_program",
            "observation": {
                "inventory": {"iron-plate": 20},
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            "production_targets": {"electronic-circuit": 20.0},
            "factory_monitor": {
                "inventory": {"iron-plate": 20},
                "target_status": {"items": [{"item": f"item-{i}", "deficit_per_minute": i} for i in range(30)]},
                "bottlenecks": [
                    {"item": f"bottleneck-{i}", "reason": "x" * 200, "required_by": ["a", "b"], "severity": 100}
                    for i in range(80)
                ],
                "factory_sites": [
                    {
                        "site_id": f"site-{i}",
                        "kind": "plate_smelting_line",
                        "item": "iron-plate",
                        "status": "running",
                        "position": {"x": i, "y": -i},
                        "machines": ["stone-furnace", "transport-belt"],
                        "automation_level": "burner-bootstrap",
                        "notes": ["x" * 200],
                    }
                    for i in range(60)
                ],
                "logistics_links": [
                    {
                        "kind": "belt",
                        "item": "iron-ore",
                        "from_site": f"mining-{i}",
                        "to_site": f"smelting-{i}",
                        "status": "complete",
                        "length_tiles": i,
                        "notes": ["x" * 200],
                    }
                    for i in range(80)
                ],
            },
            "goal_dependency_tree": [{"item": f"dep-{i}", "ingredients": []} for i in range(100)],
            "available_skills": [
                {"name": f"skill-{i}", "description": "x" * 200, "executor": "Exec", "llm_scope": "scope"}
                for i in range(40)
            ],
        }
        compact = compact_strategy_payload(verbose)
        encoded = json.dumps(compact, ensure_ascii=False)
        self.assertLess(len(encoded), 16000)
        self.assertLessEqual(len(compact["bottlenecks"]), 10)
        self.assertLessEqual(len(compact["dependency_targets"]), 40)
        self.assertLessEqual(len(compact["factory_sites"]), 18)
        self.assertLessEqual(len(compact["logistics_links"]), 24)

    def test_compact_strategy_payload_only_allows_executable_skills(self):
        payload = make_strategy_payload("launch_rocket_program", {"inventory": {}, "entities": []}, {})
        payload["available_skills"] = skill_catalog_payload()
        compact = compact_strategy_payload(payload)
        self.assertIn("produce_iron_plate", compact["allowed_skill_names"])
        self.assertIn("automate_electronic_circuit_line", compact["allowed_skill_names"])
        self.assertIn("plan_factory_site", compact["allowed_skill_names"])
        self.assertNotIn("launch_rocket_program", compact["allowed_skill_names"])
        circuit_skill = next(item for item in compact["available_skills"] if item["name"] == "produce_electronic_circuit")
        self.assertIn("bootstrap stock", circuit_skill["role"])
        automation_skill = next(
            item for item in compact["available_skills"] if item["name"] == "automate_electronic_circuit_line"
        )
        self.assertIn("sustained green-circuit throughput", automation_skill["role"])
        layout_skill = next(item for item in compact["available_skills"] if item["name"] == "plan_factory_site")
        self.assertIn("inefficient site placement", layout_skill["role"])

    def test_compact_strategy_payload_includes_site_level_logistics(self):
        payload = {
            "objective": "launch_rocket_program",
            "observation": {"inventory": {}, "research": {"technologies": {}}},
            "production_targets": {},
            "factory_monitor": {
                "factory_sites": [
                    {
                        "site_id": "group:iron-mining",
                        "kind": "mining_patch",
                        "item": "iron-ore",
                        "status": "running",
                        "position": {"x": 10.234, "y": -3.876},
                        "machines": ["electric-mining-drill x30"],
                        "automation_level": "powered",
                        "notes": ["grouped 30 adjacent site records"],
                    },
                    {
                        "site_id": "group:iron-smelting",
                        "kind": "plate_smelting_line",
                        "item": "iron-plate",
                        "status": "incomplete",
                        "position": {"x": 40, "y": -3},
                        "machines": ["stone-furnace x24", "transport-belt x2"],
                        "automation_level": "belt-fed",
                        "notes": ["missing ore input lane"],
                    },
                ],
                "logistics_links": [
                    {
                        "kind": "belt",
                        "item": "iron-ore",
                        "from_site": "group:iron-mining",
                        "to_site": "group:iron-smelting",
                        "status": "incomplete",
                        "length_tiles": 30.456,
                        "notes": ["site-level logistics link inferred from producer and consumer sites"],
                    }
                ],
            },
            "available_skills": skill_catalog_payload(),
        }
        compact = compact_strategy_payload(payload)
        self.assertEqual(compact["factory_site_summary"][0]["kind"], "mining_patch")
        self.assertEqual(compact["factory_sites"][0]["status"], "incomplete")
        self.assertEqual(compact["factory_sites"][0]["position"], {"x": 40.0, "y": -3.0})
        self.assertEqual(compact["logistics_links"][0]["from_site"], "group:iron-mining")
        self.assertEqual(compact["logistics_links"][0]["to_site"], "group:iron-smelting")
        self.assertEqual(compact["logistics_links"][0]["length_tiles"], 30.5)

    def test_compact_strategy_payload_includes_layout_opportunities(self):
        payload = {
            "objective": "launch_rocket_program",
            "observation": {
                "inventory": {},
                "research": {"technologies": {}},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 50,
                        "recipe": "electronic-circuit",
                        "position": {"x": 10, "y": 0},
                        "electric_network_connected": True,
                        "inventories": {},
                    }
                ],
                "resources": [],
            },
            "production_targets": {},
            "available_skills": skill_catalog_payload(),
        }
        compact = compact_strategy_payload(payload)
        layout = compact["layout_improvement"]
        self.assertEqual(layout["recommended_skill"], "plan_factory_site")
        self.assertEqual(layout["site_structure"]["machine_counts"]["assembling-machine-1"], 1)
        self.assertIn("rebalance_green_circuit_ratio", {item["kind"] for item in layout["opportunities"]})


if __name__ == "__main__":
    unittest.main()
