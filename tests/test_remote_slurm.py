import json
import os
import subprocess
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
    request_layout_improvement,
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

    def test_attached_strategy_task_retries_timeout(self):
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
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remote_result = {
            "ok": True,
            "source": "llm",
            "selected_skill": "setup_power",
            "priority": 90,
            "reason": "restore power before belt automation",
        }
        with (
            patch("factorio_ai.remote_slurm.resolve_remote_dir", return_value="/remote/factorio-ai-worker"),
            patch("factorio_ai.remote_slurm._run_scp"),
            patch(
                "factorio_ai.remote_slurm._run_remote",
                side_effect=[
                    subprocess.TimeoutExpired(["ssh"], timeout=30),
                    json.dumps(remote_result),
                ],
            ) as run_remote,
            patch.dict(
                "os.environ",
                {
                    "FACTORIO_AI_SLURM_MODE": "attach",
                    "FACTORIO_AI_SLURM_ATTACHED_TASK_ATTEMPTS": "2",
                    "FACTORIO_AI_SLURM_ATTACHED_TASK_RETRY_DELAY_SECONDS": "0",
                },
                clear=False,
            ),
        ):
            result = request_strategy(
                "launch_rocket_program",
                {"inventory": {}, "entities": []},
                cfg=cfg,
                timeout_seconds=30,
            )

        self.assertEqual(result["selected_skill"], "setup_power")
        self.assertEqual(run_remote.call_count, 2)
        retry_script = run_remote.call_args_list[1].args[0]
        self.assertIn('elif [[ ! -f "$TASK_PATH" ]]', retry_script)

    def test_request_layout_improvement_adds_learning_context(self):
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
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        with (
            patch("factorio_ai.remote_slurm.submit_task", return_value="layout-task.json") as submit_task,
            patch("factorio_ai.remote_slurm.read_task_state", return_value=("result", {"ok": True}, "")),
        ):
            result = request_layout_improvement(
                "launch_rocket_program",
                "idle:autopilot_stale",
                0,
                {"inventory": {}, "entities": []},
                cfg=cfg,
                timeout_seconds=1,
            )

        self.assertTrue(result["ok"])
        task = submit_task.call_args.args[0]
        learning = task["payload"]["layout_learning"]
        self.assertTrue(learning["return_learned_skills"])
        self.assertTrue(learning["record_only_confirmed"])
        self.assertIn("direct assembler-to-assembler inserter transfer", " ".join(learning["skill_targets"]))
        self.assertIn("corner belt tile", " ".join(learning["skill_targets"]))
        self.assertIn("input inserters", " ".join(learning["skill_targets"]))

    def test_scheduler_layout_improvement_waits_when_gpu_not_ready(self):
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
            partition="gpu",
            cpus_per_task=8,
            gpus_per_node=1,
            gres="gpu:1",
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        status = {
            "llm_ready": False,
            "missing": ["ready scheduler GPU allocation"],
            "remote": {
                "scheduler_ready_free_gpus": 0,
                "pending_gpu_tasks": 2,
                "pending_gpu_allocations": [{"id": 9, "state": "pending"}],
            },
        }
        with (
            patch.dict(os.environ, {"FACTORIO_AI_SLURM_MODE": "scheduler"}),
            patch("factorio_ai.remote_slurm._scheduler_status_payload", return_value=status) as status_payload,
            patch("factorio_ai.remote_slurm._request_task_via_scheduler") as request_task,
        ):
            result = request_layout_improvement(
                "launch_rocket_program",
                "idle:autopilot_stale",
                0,
                {"inventory": {}, "entities": []},
                cfg=cfg,
                timeout_seconds=1,
            )

        self.assertFalse(result["ok"])
        self.assertIn("ready scheduler GPU allocation", result["missing"])
        self.assertEqual(result["remote"]["pending_gpu_tasks"], 2)
        request_task.assert_not_called()
        status_payload.assert_called_once_with("layout_improvement_request", max_active_layout_tasks=None)

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
            patch.dict(os.environ, {"FACTORIO_AI_ALLOW_LEGACY_DIRECT_SLURM": "1"}),
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
            patch.dict(os.environ, {"FACTORIO_AI_ALLOW_LEGACY_DIRECT_SLURM": "1"}),
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

    def test_legacy_direct_ensure_requires_explicit_opt_in(self):
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
            patch.dict(os.environ, {}, clear=True),
            patch("factorio_ai.remote_slurm._run_remote") as run_remote,
        ):
            result = ensure_worker_job(cfg, renew_before_minutes=180)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "legacy_direct_slurm_disabled")
        run_remote.assert_not_called()

    def test_scheduler_mode_ensure_does_not_submit_factorio_worker(self):
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
            patch.dict(os.environ, {"FACTORIO_AI_SLURM_MODE": "scheduler"}),
            patch("factorio_ai.remote_slurm.resolve_remote_dir") as resolve_remote_dir,
            patch("factorio_ai.remote_slurm.submit_worker_job") as submit_worker_job,
            patch("factorio_ai.remote_slurm._scheduler_status_payload", return_value={"llm_ready": True}) as status,
        ):
            result = ensure_worker_job(cfg, renew_before_minutes=180)

        self.assertEqual(result["action"], "scheduler_managed_no_direct_worker")
        self.assertEqual(result["account"], "r1jae262")
        resolve_remote_dir.assert_not_called()
        submit_worker_job.assert_not_called()
        status.assert_called_once()

    def test_scheduler_task_submission_targets_configured_account_and_gpu(self):
        from factorio_ai import remote_slurm

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
        posted: list[dict] = []
        uploaded: list[dict] = []
        remote_scripts: list[str] = []

        def fake_post(path, data, timeout=0):
            posted.append(data)
            return "ok"

        def fake_rows():
            return [{"id": 42, "name": posted[0]["name"], "status": "queued"}] if posted else []

        def fake_run_remote(script, _cfg, timeout=0):
            remote_scripts.append(script)
            if "SCHEDULER_CWD_RAW" in script:
                return "/remote/factorio-ai"
            return ""

        def fake_scp(local_path, remote_target, _cfg, timeout=0):
            uploaded.append({"target": remote_target, "payload": local_path.read_text(encoding="utf-8")})

        large_blob = "x" * 200_000
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "rtx3090",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "rtx3090",
                },
            ),
            patch("factorio_ai.remote_slurm._run_remote", side_effect=fake_run_remote),
            patch("factorio_ai.remote_slurm._run_scp", side_effect=fake_scp),
            patch("factorio_ai.remote_slurm._scheduler_post_form", side_effect=fake_post),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", side_effect=fake_rows),
        ):
            row = remote_slurm._submit_scheduler_task(
                {"id": "t1", "type": "layout_improvement_request", "payload": {"blob": large_blob}},
                cfg,
            )

        self.assertEqual(row["id"], 42)
        self.assertEqual(posted[0]["remote_cwd"], "/remote/factorio-ai")
        self.assertEqual(posted[0]["account_name"], "r1jae262")
        self.assertEqual(posted[0]["gpu_model"], "rtx3090")
        self.assertEqual(posted[0]["priority"], 80)
        self.assertLess(len(posted[0]["command"]), 5_000)
        self.assertNotIn(large_blob[:100], posted[0]["command"])
        self.assertIn(".factorio-ai-scheduler-tasks/t1.json", posted[0]["command"])
        self.assertEqual(json.loads(uploaded[0]["payload"])["payload"]["blob"], large_blob)
        self.assertIn("/remote/factorio-ai/.factorio-ai-scheduler-tasks/.t1.", uploaded[0]["target"])
        self.assertTrue(any("mv \"$REMOTE_TEMP\" \"$REMOTE_TARGET\"" in script for script in remote_scripts))

    def test_scheduler_task_command_requires_vllm_endpoint_when_model_requested(self):
        from factorio_ai import remote_slurm

        with patch.dict("os.environ", {"FACTORIO_AI_VLLM_STARTUP_SECONDS": "7"}, clear=True):
            command = remote_slurm._scheduler_task_command(
                {"id": "layout-test", "type": "layout_improvement_request", "payload": {}}
            )

        self.assertIn("python -m vllm.entrypoints.openai.api_server", command)
        self.assertIn("vllm command/module not found after conda activation", command)
        self.assertIn("vllm endpoint not ready before FACTORIO_AI_VLLM_STARTUP_SECONDS", command)
        self.assertIn("VLLM_LOG_BASE=logs/vllm-scheduler-layout-test", command)
        self.assertIn("$VLLM_LOG_BASE.err", command)
        self.assertIn("export VLLM_USE_FLASHINFER_SAMPLER", command)
        self.assertIn("export CUDA_VISIBLE_DEVICES", command)
        self.assertIn("export HF_HOME", command)
        self.assertIn("trap cleanup_vllm EXIT", command)
        self.assertIn('VLLM_PID="$!"', command)
        self.assertIn("exit 1", command)

    def test_scheduler_task_command_uses_persistent_vllm_service_node(self):
        from factorio_ai import remote_slurm

        with patch.dict(
            "os.environ",
            {
                "FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED": "1",
                "FACTORIO_AI_SLURM_SCHEDULER_GPUS": "1",
                "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000ada,a6000",
                "FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS": "1",
                "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                "FACTORIO_AI_VLLM_STARTUP_SECONDS": "7",
            },
            clear=True,
        ):
            with patch("factorio_ai.remote_slurm._scheduler_running_vllm_service_node_name", return_value="n104"):
                command = remote_slurm._scheduler_task_command(
                    {"id": "strategy-test", "type": "strategy_request", "payload": {}}
                )
                client_resources = remote_slurm._scheduler_task_resources(
                    "strategy_request",
                    gpu_model="a6000",
                )
                service_resources = remote_slurm._scheduler_task_resources(
                    remote_slurm.VLLM_SERVICE_TASK_TYPE,
                    gpu_model="a6000",
                )

        self.assertEqual(client_resources["gpus"], 1)
        self.assertEqual(client_resources["gpu_model"], "a6000ada,a6000")
        self.assertEqual(client_resources["node_name"], "n104")
        self.assertEqual(service_resources["gpus"], 1)
        self.assertEqual(service_resources["gpu_model"], "a6000ada,a6000")
        self.assertIn("VLLM_SERVICE_ENABLED=1", command)
        self.assertIn('if [ "$VLLM_SERVICE_ENABLED" != "1" ]; then', command)
        self.assertIn("vllm service endpoint not ready before FACTORIO_AI_VLLM_STARTUP_SECONDS", command)

    def test_scheduler_running_vllm_service_node_name_uses_service_allocation(self):
        from factorio_ai import remote_slurm

        tasks = [
            {
                "id": 82,
                "name": "factorio-vllm-service-qwen-test-abcd1234",
                "status": "running",
                "account_name": "r1jae262",
                "allocation_id": 40,
                "gpu_model": "a6000",
            }
        ]
        allocations = [
            {
                "id": 40,
                "account_name": "r1jae262",
                "state": "active",
                "gpu_model": "a6000",
                "node_name": "n104",
            }
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000ada,a6000",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", return_value=tasks),
            patch("factorio_ai.remote_slurm._scheduler_api_json_retry", return_value=allocations),
        ):
            node_name = remote_slurm._scheduler_running_vllm_service_node_name()

        self.assertEqual(node_name, "n104")

    def test_ensure_vllm_service_does_not_submit_when_active_service_is_ready(self):
        from factorio_ai import remote_slurm

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
        rows = [
            {
                "id": 77,
                "name": "factorio-vllm-service-qwen-test-abcd1234",
                "status": "running",
                "account_name": "r1jae262",
                "gpu_model": "a6000",
            }
        ]
        heartbeat = {"state": "ready", "updated_at": "2026-01-01T00:00:00+00:00", "model": "Qwen/test"}
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", return_value=rows),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=heartbeat),
            patch("factorio_ai.remote_slurm._iso_age_seconds", return_value=1.0),
            patch("factorio_ai.remote_slurm._submit_scheduler_task") as submit_task,
        ):
            result = remote_slurm.ensure_vllm_service(cfg, duration_seconds=10800)

        self.assertEqual(result["action"], "already_active")
        self.assertTrue(result["status"]["service_ready"])
        submit_task.assert_not_called()

    def test_ensure_vllm_service_submits_when_no_active_service_exists(self):
        from factorio_ai import remote_slurm

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
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", return_value=[]),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=None),
            patch(
                "factorio_ai.remote_slurm._submit_scheduler_task",
                return_value={
                    "id": 78,
                    "name": "factorio-vllm-service-qwen-test-efgh5678",
                    "status": "queued",
                    "account_name": "r1jae262",
                    "gpu_model": "a6000",
                },
            ) as submit_task,
        ):
            result = remote_slurm.ensure_vllm_service(cfg, duration_seconds=10800)

        self.assertEqual(result["action"], "submitted")
        task = submit_task.call_args.args[0]
        self.assertEqual(task["type"], remote_slurm.VLLM_SERVICE_TASK_TYPE)
        self.assertEqual(task["payload"]["duration_seconds"], 10800)
        self.assertEqual(task["payload"]["model"], "Qwen/test")

    def test_ensure_vllm_service_replaces_stale_queued_service(self):
        from factorio_ai import remote_slurm

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
        queued = {
            "id": 77,
            "name": "factorio-vllm-service-qwen-test-stale",
            "status": "queued",
            "account_name": "r1jae262",
            "gpu_model": "a6000",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        submitted = {
            "id": 78,
            "name": "factorio-vllm-service-qwen-test-new",
            "status": "queued",
            "account_name": "r1jae262",
            "gpu_model": "a6000",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                    "FACTORIO_AI_SCHEDULER_VLLM_SERVICE_QUEUE_STALE_SECONDS": "180",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", side_effect=[[queued], [], []]),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=None),
            patch("factorio_ai.remote_slurm._iso_age_seconds", return_value=181.0),
            patch("factorio_ai.remote_slurm._scheduler_cancel_task", return_value={"ok": True}) as cancel_task,
            patch("factorio_ai.remote_slurm._submit_scheduler_task", return_value=submitted) as submit_task,
        ):
            result = remote_slurm.ensure_vllm_service(cfg, duration_seconds=10800)

        self.assertEqual(result["action"], "replaced_stale_queue")
        cancel_task.assert_called_once_with(77)
        submit_task.assert_called_once()

    def test_vllm_service_heartbeat_reader_expands_tilde_remote_path(self):
        from factorio_ai import remote_slurm

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
        with patch("factorio_ai.remote_slurm._run_remote", return_value='{"state":"ready"}') as run_remote:
            heartbeat = remote_slurm._read_scheduler_vllm_service_heartbeat(cfg)

        script = run_remote.call_args.args[0]
        self.assertEqual(heartbeat["state"], "ready")
        self.assertIn("HEARTBEAT_PATH_RAW=", script)
        self.assertIn('HEARTBEAT_PATH="$HOME/${HEARTBEAT_PATH_RAW:2}"', script)

    def test_scheduler_task_polling_uses_detail_endpoint_after_submit(self):
        from factorio_ai import remote_slurm

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
        completed = {
            "id": 77,
            "name": "factorio-layout-improvement-request-test",
            "status": "completed",
            "account_name": "r1jae262",
            "stdout_path": "stdout.log",
        }
        with (
            patch.dict(os.environ, {"FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262"}),
            patch(
                "factorio_ai.remote_slurm._submit_scheduler_task",
                return_value={"id": 77, "name": completed["name"], "status": "queued"},
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_detail", return_value=completed) as detail,
            patch("factorio_ai.remote_slurm._scheduler_task_rows", side_effect=AssertionError("used task list")),
            patch("factorio_ai.remote_slurm._read_scheduler_task_stdout", return_value='{"ok": true, "source": "llm"}'),
        ):
            result = remote_slurm._request_task_via_scheduler(
                {"id": "t1", "type": "layout_improvement_request", "payload": {}},
                cfg,
                timeout_seconds=5,
            )

        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["scheduler_task_id"], 77)
        self.assertEqual(result["scheduler_task_name"], completed["name"])
        detail.assert_called_once_with(77)

    def test_layout_scheduler_task_submission_selects_ready_a6000_candidate(self):
        from factorio_ai import remote_slurm

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
        posted: list[dict] = []

        def fake_post(path, data, timeout=0):
            posted.append(data)
            return "ok"

        def fake_rows():
            return [{"id": 43, "name": posted[0]["name"], "status": "queued"}] if posted else []

        def fake_api(path, timeout=0):
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "pending",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "a6000ada",
                    },
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "a6000",
                    },
                ]
            if path == "/api/gpu-capacity":
                return [
                    {"gpu_model": "a6000ada", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 0},
                    {"gpu_model": "a6000", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1},
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "A6000ada,A6000",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
            patch("factorio_ai.remote_slurm._run_remote", return_value="/remote/factorio-ai"),
            patch("factorio_ai.remote_slurm._run_scp"),
            patch("factorio_ai.remote_slurm._scheduler_post_form", side_effect=fake_post),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", side_effect=fake_rows),
        ):
            row = remote_slurm._submit_scheduler_task(
                {"id": "layout1", "type": "layout_improvement_request", "payload": {}},
                cfg,
            )

        self.assertEqual(row["id"], 43)
        self.assertEqual(posted[0]["gpu_model"], "a6000ada,a6000")
        self.assertEqual(posted[0]["cpus"], 3)
        self.assertEqual(posted[0]["priority"], 80)

    def test_layout_scheduler_status_accepts_a6000_candidates(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "a6000",
                    },
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "rtx3090",
                    },
                ]
            if path == "/api/gpu-capacity":
                return [
                    {
                        "gpu_model": "a6000ada",
                        "scheduler_owned_gpus": 1,
                        "scheduler_free_gpus": 0,
                        "pending_gpu_tasks": 1,
                    },
                    {
                        "gpu_model": "a6000",
                        "scheduler_owned_gpus": 1,
                        "scheduler_free_gpus": 1,
                        "pending_gpu_tasks": 0,
                    },
                    {"gpu_model": "rtx3090", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1},
                ]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-old",
                        "status": "queued",
                        "account_name": "r1jae262",
                        "gpu_model": "a6000",
                        "cpus": 4,
                        "memory_mb": 32768,
                        "gpus": 1,
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "a6000ada,a6000",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.layout_improvement_status()

        self.assertTrue(status["llm_ready"])
        self.assertEqual(status["remote"]["gpu_model_candidates"], ["a6000ada", "a6000"])
        self.assertEqual(status["remote"]["selected_gpu_model"], "a6000")
        self.assertEqual(status["remote"]["resources"]["gpu_model"], "a6000ada,a6000")
        self.assertEqual(status["remote"]["resources"]["cpus"], 3)
        self.assertEqual(status["remote"]["scheduler_ready_free_gpus"], 1)
        self.assertEqual(status["remote"]["resource_fit_pending_gpu_tasks"], 0)

    def test_layout_scheduler_status_waits_when_resource_fit_pending_task_fills_slot(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "total_cpus": 3,
                        "free_cpus": 3,
                        "total_memory_mb": 32768,
                        "free_memory_mb": 32768,
                        "gpu_model": "a6000",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "a6000", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1}]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-current",
                        "status": "queued",
                        "account_name": "r1jae262",
                        "gpu_model": "a6000",
                        "cpus": 3,
                        "memory_mb": 32768,
                        "gpus": 1,
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "a6000ada,a6000",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.layout_improvement_status()

        self.assertFalse(status["llm_ready"])
        self.assertEqual(status["missing"], ["scheduler GPU queue capacity"])
        self.assertEqual(status["remote"]["scheduler_ready_gpu_slots"], 1)
        self.assertEqual(status["remote"]["resource_fit_pending_gpu_tasks"], 1)

    def test_layout_scheduler_status_waits_when_layout_task_capacity_full(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 2,
                        "free_gpus": 1,
                        "total_cpus": 3,
                        "free_cpus": 3,
                        "total_memory_mb": 32768,
                        "free_memory_mb": 32768,
                        "gpu_model": "a6000",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "a6000", "scheduler_owned_gpus": 2, "scheduler_free_gpus": 1}]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-current",
                        "status": "running",
                        "account_name": "r1jae262",
                        "gpu_model": "a6000",
                        "cpus": 3,
                        "memory_mb": 32768,
                        "gpus": 1,
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "a6000ada,a6000",
                    "FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS": "1",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.layout_improvement_status()

        self.assertFalse(status["llm_ready"])
        self.assertEqual(status["missing"], ["active scheduler layout task capacity"])
        self.assertEqual(status["remote"]["active_layout_tasks"], 1)
        self.assertEqual(status["remote"]["max_active_layout_tasks"], 1)

    def test_layout_scheduler_status_counts_running_layout_without_gpus_field(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "total_cpus": 3,
                        "free_cpus": 3,
                        "total_memory_mb": 32768,
                        "free_memory_mb": 32768,
                        "gpu_model": "a6000",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "a6000", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1}]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-current",
                        "status": "running",
                        "account_name": "r1jae262",
                        "gpu_model": "a6000",
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "a6000ada,a6000",
                    "FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS": "1",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.layout_improvement_status()

        self.assertFalse(status["llm_ready"])
        self.assertEqual(status["missing"], ["active scheduler layout task capacity"])
        self.assertEqual(status["remote"]["active_layout_tasks"], 1)

    def test_layout_scheduler_status_allows_running_layout_task_below_capacity(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 2,
                        "free_gpus": 1,
                        "total_cpus": 6,
                        "free_cpus": 3,
                        "total_memory_mb": 65536,
                        "free_memory_mb": 32768,
                        "gpu_model": "a6000",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "a6000", "scheduler_owned_gpus": 2, "scheduler_free_gpus": 1}]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-current",
                        "status": "running",
                        "account_name": "r1jae262",
                        "gpu_model": "a6000",
                        "cpus": 3,
                        "memory_mb": 32768,
                        "gpus": 1,
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                    "FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS": "a6000ada,a6000",
                    "FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS": "2",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.layout_improvement_status()

        self.assertTrue(status["llm_ready"])
        self.assertEqual(status["remote"]["active_layout_tasks"], 1)
        self.assertEqual(status["remote"]["max_active_layout_tasks"], 2)
        self.assertEqual(status["remote"]["active_layout_capacity_remaining"], 1)

    def test_scheduler_status_reports_ready_with_vllm_and_scheduler_gpu(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "rtx3090",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "rtx3090", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1}]
            if path == "/api/tasks":
                return []
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.llm_status()

        self.assertTrue(status["llm_ready"])
        self.assertEqual(status["provider"], "slurm_scheduler")
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["remote"]["scheduler_ready_free_gpus"], 1)

    def test_scheduler_status_reports_ready_with_persistent_vllm_service_without_free_gpu(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "id": 40,
                        "account_name": "r1jae262",
                        "state": "active",
                        "total_gpus": 1,
                        "free_gpus": 0,
                        "gpu_model": "a6000",
                        "node_name": "n104",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "a6000", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 0}]
            if path == "/api/tasks":
                return [
                    {
                        "id": 8224,
                        "name": "factorio-vllm-service-qwen-test-abcd1234",
                        "status": "running",
                        "account_name": "r1jae262",
                        "allocation_id": 40,
                        "gpu_model": "a6000",
                    }
                ]
            raise AssertionError(path)

        heartbeat = {"state": "ready", "updated_at": "2026-01-01T00:00:00+00:00", "model": "Qwen/test"}
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000ada,a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                    "FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED": "1",
                    "FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS": "1",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=heartbeat),
            patch("factorio_ai.remote_slurm._iso_age_seconds", return_value=1.0),
        ):
            status = remote_slurm.llm_status()

        self.assertTrue(status["llm_ready"])
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["remote"]["scheduler_ready_free_gpus"], 0)
        self.assertTrue(status["remote"]["vllm_service_ready_for_clients"])
        self.assertEqual(status["remote"]["active_vllm_services"][0]["id"], 8224)

    def test_scheduler_status_selects_ready_strategy_gpu_candidate(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "a6000",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [
                    {"gpu_model": "rtx3090", "scheduler_owned_gpus": 0, "scheduler_free_gpus": 0},
                    {"gpu_model": "a6000", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1},
                ]
            if path == "/api/tasks":
                return []
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "rtx3090,a6000ada,a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.llm_status()

        self.assertTrue(status["llm_ready"])
        self.assertEqual(status["remote"]["gpu_model_candidates"], ["rtx3090", "a6000ada", "a6000"])
        self.assertEqual(status["remote"]["selected_gpu_model"], "a6000")
        self.assertEqual(status["remote"]["resources"]["gpu_model"], "rtx3090,a6000ada,a6000")

    def test_scheduler_status_reports_pending_gpu_not_ready(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "pending",
                        "total_gpus": 2,
                        "free_gpus": 2,
                        "gpu_model": "rtx3090",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "rtx3090", "scheduler_owned_gpus": 2, "scheduler_free_gpus": 2}]
            if path == "/api/tasks":
                return [{"name": "factorio-layout-improvement-request-test", "status": "queued", "gpus": 1}]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.llm_status()

        self.assertFalse(status["llm_ready"])
        self.assertEqual(status["missing"], ["ready scheduler GPU allocation"])
        self.assertEqual(status["remote"]["scheduler_ready_free_gpus"], 0)
        self.assertEqual(len(status["remote"]["pending_gpu_allocations"]), 1)
        self.assertNotIn("command", status["remote"]["recent_tasks"][0])
        self.assertNotIn("env_setup", status["remote"]["recent_tasks"][0])

    def test_scheduler_status_waits_when_pending_tasks_fill_ready_capacity(self):
        from factorio_ai import remote_slurm

        def fake_api(path, timeout=0):
            if path == "/api/health":
                return {"ok": True}
            if path == "/api/allocations":
                return [
                    {
                        "account_name": "r1jae262",
                        "state": "warm",
                        "total_gpus": 1,
                        "free_gpus": 1,
                        "gpu_model": "rtx3090",
                    }
                ]
            if path == "/api/gpu-capacity":
                return [{"gpu_model": "rtx3090", "scheduler_owned_gpus": 1, "scheduler_free_gpus": 1, "pending_gpu_tasks": 1}]
            if path == "/api/tasks":
                return [
                    {
                        "name": "factorio-layout-improvement-request-test",
                        "status": "queued",
                        "account_name": "r1jae262",
                        "gpu_model": "rtx3090",
                        "cpus": 4,
                        "memory_mb": 32768,
                        "gpus": 1,
                    }
                ]
            raise AssertionError(path)

        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B",
                },
            ),
            patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=fake_api),
        ):
            status = remote_slurm.llm_status()

        self.assertFalse(status["llm_ready"])
        self.assertEqual(status["missing"], ["scheduler GPU queue capacity"])
        self.assertFalse(status["remote"]["scheduler_gpu_queue_capacity"])

    def test_local_gpu_needed_for_vllm_or_loopback_endpoint(self):
        self.assertTrue(_status_needs_local_gpu({"FACTORIO_AI_VLLM_MODEL": "Qwen/Qwen3.5-4B"}))
        self.assertTrue(_status_needs_local_gpu({"FACTORIO_AI_LLM_BASE_URL": "http://127.0.0.1:8000/v1"}))
        self.assertFalse(_status_needs_local_gpu({"FACTORIO_AI_LLM_BASE_URL": "https://llm.example/v1"}))

    def test_attached_env_setup_loads_remote_worker_config(self):
        setup = _attached_env_setup("/home/user/kakao-bot-worker")
        self.assertIn("/home/user/kakao-bot-worker/config.env", setup)
        self.assertIn(
            "FACTORIO_AI_LLM_*|FACTORIO_AI_VLLM_*|FACTORIO_AI_SCHEDULER_VLLM_SERVICE_*|FACTORIO_AI_CONDA_ENV",
            setup,
        )
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

        def fake_call(system, prompt, schema=None, **kwargs):
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
                        "inventories": {"1": {"transport-belt": 2}},
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
                            "inventories": {"1": {"transport-belt": 2}},
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

    def test_scheduler_mode_request_strategy_uses_scheduler_task(self):
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
        observation = {"inventory": {}, "entities": [], "resources": [], "research": {"technologies": {}}}
        with (
            patch.dict(os.environ, {"FACTORIO_AI_SLURM_MODE": "scheduler"}),
            patch("factorio_ai.remote_slurm._request_task_via_scheduler", return_value={"ok": True, "source": "llm"}) as request,
            patch("factorio_ai.remote_slurm._request_task_via_attached_srun") as attached,
        ):
            result = request_strategy(
                "launch_rocket_program",
                observation,
                production_targets={},
                available_skills=skill_catalog_payload(),
                cfg=cfg,
                timeout_seconds=1,
            )

        self.assertEqual(result["source"], "llm")
        task = request.call_args.args[0]
        self.assertEqual(task["type"], "strategy_request")
        attached.assert_not_called()

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
        self.assertIn("bootstrap_power_pole_mall", compact["allowed_skill_names"])
        self.assertIn("research_electric_mining_drill", compact["allowed_skill_names"])
        self.assertIn("bootstrap_electric_mining_drill_mall", compact["allowed_skill_names"])
        self.assertIn("relocate_gear_belt_mall_to_iron_source", compact["allowed_skill_names"])
        self.assertNotIn("launch_rocket_program", compact["allowed_skill_names"])
        circuit_skill = next(item for item in compact["available_skills"] if item["name"] == "produce_electronic_circuit")
        self.assertIn("bootstrap stock", circuit_skill["role"])
        automation_skill = next(
            item for item in compact["available_skills"] if item["name"] == "automate_electronic_circuit_line"
        )
        self.assertIn("sustained green-circuit throughput", automation_skill["role"])
        layout_skill = next(item for item in compact["available_skills"] if item["name"] == "plan_factory_site")
        self.assertIn("inefficient site placement", layout_skill["role"])
        pole_skill = next(item for item in compact["available_skills"] if item["name"] == "bootstrap_power_pole_mall")
        self.assertIn("small electric poles", pole_skill["role"])
        electric_research_skill = next(
            item for item in compact["available_skills"] if item["name"] == "research_electric_mining_drill"
        )
        self.assertIn("electric mining drills", electric_research_skill["role"])
        electric_mall_skill = next(
            item for item in compact["available_skills"] if item["name"] == "bootstrap_electric_mining_drill_mall"
        )
        self.assertIn("electric mining drill production", electric_mall_skill["role"])
        relocation_skill = next(
            item for item in compact["available_skills"] if item["name"] == "relocate_gear_belt_mall_to_iron_source"
        )
        self.assertIn("route cost favors relocation", relocation_skill["role"])

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
                "inventory": {"productivity-module": 1, "assembling-machine-2": 1, "steel-furnace": 1},
                "research": {"technologies": {"long-inserters": {"researched": True}}},
                "recipe_unlocks": {"long-handed-inserter": {"enabled": True}, "beacon": {"enabled": True}},
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
        self.assertTrue(layout["layout_capabilities"]["long_handed_inserter"]["available"])
        self.assertTrue(layout["layout_capabilities"]["modules"]["productivity-module"]["available"])
        self.assertTrue(layout["layout_capabilities"]["machines"]["assembling-machine-2"]["available"])
        self.assertTrue(layout["layout_capabilities"]["furnaces"]["steel-furnace"]["available"])
        self.assertTrue(layout["layout_capabilities"]["beacons"]["beacon"]["available"])
        self.assertTrue(layout["layout_capabilities"]["long_handed_inserter"]["recipe_unlocked"])
        self.assertTrue(layout["layout_capabilities"]["rerank_trigger"])
        self.assertIn("rebalance_green_circuit_ratio", {item["kind"] for item in layout["opportunities"]})


class VllmServiceDetectionTests(unittest.TestCase):
    def test_active_service_rows_match_comma_joined_gpu_model(self):
        # A vLLM service task is submitted with gpu_model = "a6000ada,a6000" (the joined candidate
        # list). Detection must still recognize it against the split wanted set, otherwise
        # ensure_vllm_service keeps submitting duplicate services that pile up and starve GPUs.
        from factorio_ai import remote_slurm

        rows = [
            {
                "name": "factorio-vllm-service-qwen-x",
                "account_name": "r1jae262",
                "status": "running",
                "gpu_model": "a6000ada,a6000",
            },
            {
                "name": "factorio-vllm-service-qwen-y",
                "account_name": "r1jae262",
                "status": "queued",
                "gpu_model": "a6000",
            },
            {
                "name": "some-other-job",
                "account_name": "r1jae262",
                "status": "running",
                "gpu_model": "a6000ada",
            },
            {
                "name": "factorio-vllm-service-qwen-z",
                "account_name": "r1jae262",
                "status": "running",
                "gpu_model": "rtx3090",
            },
        ]
        matched = remote_slurm._scheduler_active_vllm_service_rows(
            rows, "r1jae262", {"a6000ada", "a6000"}
        )
        names = {row["name"] for row in matched}
        self.assertIn("factorio-vllm-service-qwen-x", names)  # comma-joined -> matched
        self.assertIn("factorio-vllm-service-qwen-y", names)  # single member -> matched
        self.assertNotIn("some-other-job", names)             # not a service task
        self.assertNotIn("factorio-vllm-service-qwen-z", names)  # different model -> excluded

    def test_cancelable_rows_match_all_service_models_queued_first(self):
        from factorio_ai import remote_slurm

        rows = [
            {"id": 3, "name": "factorio-vllm-service-a", "account_name": "r1jae262", "status": "running", "gpu_model": "rtx3090"},
            {"id": 2, "name": "factorio-vllm-service-b", "account_name": "r1jae262", "status": "queued", "gpu_model": "a6000ada,a6000"},
            {"id": 9, "name": "other-job", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000"},
            {"id": 5, "name": "factorio-vllm-service-c", "account_name": "someone-else", "status": "running", "gpu_model": "a6000"},
        ]
        out = remote_slurm._cancelable_vllm_service_rows(rows, "r1jae262")
        ids = [r["id"] for r in out]
        self.assertEqual(set(ids), {2, 3})       # any model, this account, service tasks only
        self.assertEqual(ids[0], 2)              # queued before running

    def test_running_vllm_service_node_resolves_from_actual_node_name(self):
        # The scheduler leaves task["node_name"] empty and reports the real node in actual_node_name.
        # The resolver must read it so client tasks can be pinned to the service node (CPU-only attach).
        from unittest.mock import patch

        from factorio_ai import remote_slurm

        task = {
            "name": "factorio-vllm-service-qwen-x",
            "account_name": "r1jae262",
            "status": "running",
            "gpu_model": "a6000ada,a6000",
            "node_name": "",
            "actual_node_name": "n104",
            "allocation_id": 40,
        }
        with patch.dict(
            "os.environ",
            {"FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262", "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000ada,a6000"},
            clear=False,
        ), patch.object(remote_slurm, "_scheduler_task_rows", return_value=[task]), patch.object(
            remote_slurm, "_scheduler_api_json_retry", return_value=[]
        ):
            node = remote_slurm._scheduler_running_vllm_service_node_name()
        self.assertEqual(node, "n104")

    def test_running_vllm_service_task_id_returns_running_service(self):
        from unittest.mock import patch

        from factorio_ai import remote_slurm

        tasks = [
            {"name": "factorio-vllm-service-x", "account_name": "r1jae262", "status": "queued", "gpu_model": "a6000", "id": 8744},
            {"name": "factorio-vllm-service-y", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000ada,a6000", "id": 8743},
        ]
        with patch.dict(
            "os.environ",
            {"FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262", "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000ada,a6000"},
            clear=False,
        ), patch.object(remote_slurm, "_scheduler_task_rows", return_value=tasks):
            task_id = remote_slurm._scheduler_running_vllm_service_task_id()
        self.assertEqual(task_id, 8743)  # the running one, not the queued one

    def test_active_layout_task_count_matches_comma_joined_gpu_model(self):
        from factorio_ai import remote_slurm

        rows = [
            {"name": "factorio-layout-improvement-request-a", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000ada,a6000", "gpus": 1},
            {"name": "factorio-layout-improvement-request-b", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "gpus": 1},
            {"name": "factorio-layout-improvement-request-c", "account_name": "r1jae262", "status": "running", "gpu_model": "rtx3090", "gpus": 1},
            {"name": "other-job", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "gpus": 1},
        ]
        count = remote_slurm._scheduler_active_layout_task_count(rows, {"a6000ada", "a6000"}, "r1jae262")
        self.assertEqual(count, 2)  # comma-joined + single-match counted; rtx3090 + non-layout excluded


class MultiVllmServiceTests(unittest.TestCase):
    def _cfg(self) -> RemoteSlurmConfig:
        return RemoteSlurmConfig(
            enabled=True, ssh_path="ssh", scp_path="scp", host="example", user="user", port=22,
            key_path="key", remote_dir="~/factorio-ai-worker", job_name="factorio-ai-worker",
            conda_env="factorio-ai", partition="gpu4", cpus_per_task=8, gpus_per_node=1, gres="gpu:1",
            time_limit="24:00:00", setup_timeout_seconds=60, task_timeout_seconds=30,
        )

    def test_service_count_env_parses_and_clamps(self):
        from factorio_ai import remote_slurm

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT", None)
            self.assertEqual(remote_slurm._scheduler_vllm_service_count(), 1)
        with patch.dict(os.environ, {"FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT": "2"}, clear=False):
            self.assertEqual(remote_slurm._scheduler_vllm_service_count(), 2)
        with patch.dict(os.environ, {"FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT": "0"}, clear=False):
            self.assertEqual(remote_slurm._scheduler_vllm_service_count(), 1)  # min 1
        with patch.dict(os.environ, {"FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT": "99"}, clear=False):
            self.assertEqual(remote_slurm._scheduler_vllm_service_count(), 8)  # max 8

    def test_running_service_task_id_round_robins_across_services(self):
        from factorio_ai import remote_slurm

        tasks = [
            {"name": "factorio-vllm-service-a", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "id": 100},
            {"name": "factorio-vllm-service-b", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "id": 200},
            {"name": "factorio-vllm-service-c", "account_name": "r1jae262", "status": "queued", "gpu_model": "a6000", "id": 300},
        ]
        with patch.dict(
            os.environ,
            {"FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262", "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000"},
            clear=False,
        ), patch.object(remote_slurm, "_scheduler_task_rows", return_value=tasks):
            remote_slurm._VLLM_SERVICE_RR["i"] = 0
            picks = [remote_slurm._scheduler_running_vllm_service_task_id() for _ in range(4)]
        self.assertEqual(picks, [100, 200, 100, 200])  # round-robin, queued excluded

    def test_ensure_submits_deficit_to_reach_target_count(self):
        from factorio_ai import remote_slurm

        running = {"name": "factorio-vllm-service-a", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "id": 100}
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                    "FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT": "2",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", return_value=[running]),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=None),
            patch(
                "factorio_ai.remote_slurm._submit_scheduler_task",
                return_value={"id": 101, "name": "factorio-vllm-service-b", "status": "queued", "account_name": "r1jae262", "gpu_model": "a6000"},
            ) as submit_task,
        ):
            result = remote_slurm.ensure_vllm_service(self._cfg(), duration_seconds=10800)
        self.assertEqual(result["action"], "submitted")
        self.assertEqual(result["target"], 2)
        self.assertEqual(result["active"], 1)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(submit_task.call_count, 1)  # only the deficit, not a full 2

    def test_ensure_is_noop_when_target_count_already_active(self):
        from factorio_ai import remote_slurm

        rows = [
            {"name": "factorio-vllm-service-a", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "id": 100},
            {"name": "factorio-vllm-service-b", "account_name": "r1jae262", "status": "running", "gpu_model": "a6000", "id": 200},
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "FACTORIO_AI_SLURM_MODE": "scheduler",
                    "FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT": "r1jae262",
                    "FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL": "a6000",
                    "FACTORIO_AI_VLLM_MODEL": "Qwen/test",
                    "FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT": "2",
                },
                clear=True,
            ),
            patch("factorio_ai.remote_slurm._scheduler_task_rows", return_value=rows),
            patch("factorio_ai.remote_slurm._read_scheduler_vllm_service_heartbeat", return_value=None),
            patch("factorio_ai.remote_slurm._submit_scheduler_task") as submit_task,
        ):
            result = remote_slurm.ensure_vllm_service(self._cfg(), duration_seconds=10800)
        self.assertEqual(result["action"], "already_active")
        self.assertEqual(result["active"], 2)
        submit_task.assert_not_called()


class SchedulerTaskCancelTests(unittest.TestCase):
    def _cfg(self) -> RemoteSlurmConfig:
        return RemoteSlurmConfig(
            enabled=True, ssh_path="ssh", scp_path="scp", host="example", user="user", port=22,
            key_path="key", remote_dir="~/factorio-ai-worker", job_name="factorio-ai-worker",
            conda_env="factorio-ai", partition="gpu4", cpus_per_task=8, gpus_per_node=1, gres="gpu:1",
            time_limit="24:00:00", setup_timeout_seconds=60, task_timeout_seconds=30,
        )

    def test_client_cancels_scheduler_task_on_timeout(self):
        # Orphan prevention: a client that gives up waiting must cancel the task so it
        # cannot linger as a zombie "running" job (the dozens-of-orphans churn).
        from factorio_ai import remote_slurm

        with (
            patch.object(remote_slurm, "_submit_scheduler_task", return_value={"id": 555, "name": "factorio-strategy-request-x"}),
            patch.object(remote_slurm, "_scheduler_task_detail", return_value={"status": "running", "account_name": ""}),
            patch.object(remote_slurm, "_scheduler_cancel_task") as cancel,
            patch("time.sleep", lambda *a, **k: None),
        ):
            with self.assertRaises(TimeoutError):
                remote_slurm._request_task_via_scheduler({"id": "t", "type": "strategy_request"}, self._cfg(), timeout_seconds=0.05)
        cancel.assert_called_once_with(555)


if __name__ == "__main__":
    unittest.main()
