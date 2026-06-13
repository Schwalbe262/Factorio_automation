import unittest

from factorio_ai.remote_slurm import RemoteSlurmConfig, _llm_status_remediation
from factorio_ai.slurm_worker import run_strategy_model_benchmark


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
            time_limit="24:00:00",
            setup_timeout_seconds=60,
            task_timeout_seconds=30,
        )
        remediation = _llm_status_remediation(["FACTORIO_AI_LLM_BASE_URL"], cfg, True)
        self.assertIsNotNone(remediation)
        self.assertIn("FACTORIO_AI_LLM_BASE_URL", remediation["required_remote_env"])
        self.assertEqual(remediation["job_name"], "AUTO")
        self.assertTrue(remediation["vllm_available_in_job"])

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


if __name__ == "__main__":
    unittest.main()
