import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai.run_health import format_run_health, gather_run_health


class RunHealthTests(unittest.TestCase):
    def test_gather_and_format_without_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (runtime / "autopilot-heartbeat.json").write_text(
                json.dumps({"state": "cycle_start", "cycle": 3, "updated_at": "2026-06-16T20:00:00+00:00"}),
                encoding="utf-8",
            )
            (runtime / "unattended-llm-supervisor.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "autopilot_gate": "ready",
                        "autopilot_processes": [1234],
                        "updated_at": "2026-06-16T20:00:00.1234567+00:00",
                    }
                ),
                encoding="utf-8-sig",
            )
            (runtime / "progress-kpi.json").write_text(
                json.dumps(
                    {
                        "researched": 1,
                        "current_research": "logistics",
                        "research_progress": 0.4,
                        "stall_count": 3,
                        "stuck": True,
                        "failure_root": "belt_line_unbuildable",
                        "repair_skill": "build_gear_belt_mall_logistics",
                        "seed_count": 2,
                        "updated_at": "2026-06-16T20:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "skill-foundry-priority.json").write_text(
                json.dumps({"queue": [{"skill_name": "connect_coal_fuel_feed", "mode": "new", "priority": 90}]}),
                encoding="utf-8",
            )
            (runtime / "live-skill-heartbeat.json").write_text(
                json.dumps({"skill": "research_logistics", "step": 1, "state": "failed", "updated_at": "2026-06-16T20:00:00+00:00"}),
                encoding="utf-8",
            )
            (runtime / "skill-foundry-loop.json").write_text(
                json.dumps({"state": "sleeping", "updated_at": "2026-06-16T20:00:00+00:00"}),
                encoding="utf-8",
            )
            (logs / "operator-intervention-layout-learning.jsonl").write_text(
                json.dumps(
                    {
                        "event": "operator_intervention_candidate",
                        "time": "2026-06-16T20:00:00+00:00",
                        "learning_label": "pending_human_review",
                        "active_skill": "idle",
                        "delta_summary": {"added": 1, "removed": 0, "changed": 0},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            summary = gather_run_health(cfg, observe=False)

        self.assertFalse(summary["server_reachable"])
        self.assertEqual(summary["supervisor"]["state"], "running")
        self.assertEqual(summary["supervisor"]["autopilot_gate"], "ready")
        self.assertEqual(summary["supervisor"]["autopilot_processes"], [1234])
        self.assertIsInstance(summary["supervisor"]["age_seconds"], float)
        self.assertEqual(summary["autopilot"]["cycle"], 3)
        self.assertEqual(summary["live_skill"]["skill"], "research_logistics")
        self.assertIn("registered", summary["generated_skills"])
        self.assertEqual(summary["progress"]["failure_root"], "belt_line_unbuildable")
        self.assertIn("connect_coal_fuel_feed", summary["generated_skills"]["stale_implemented_queue"])
        self.assertTrue(any(item["kind"] == "failure_root_loop" for item in summary["warnings"]))
        self.assertTrue(any(item["kind"] == "implemented_skill_stuck_in_foundry_queue" for item in summary["warnings"]))
        self.assertEqual(summary["human_layout_learning"]["learning_label"], "pending_human_review")
        self.assertTrue(any(item["kind"] == "operator_layout_learning_pending" for item in summary["warnings"]))
        text = format_run_health(summary)
        self.assertIn("run health", text)
        self.assertIn("supervisor: state=running gate=ready", text)
        self.assertIn("research_logistics", text)
        self.assertIn("foundry", text)
        self.assertIn("failure_root=belt_line_unbuildable", text)
        self.assertIn("stale implemented queue", text)
        self.assertIn("operator layout learning", text)

    def test_scheduler_api_timeout_falls_back_to_supervisor_vllm_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (runtime / "unattended-llm-supervisor.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "autopilot_gate": "ready",
                        "updated_at": "2026-06-16T20:00:00.1234567+00:00",
                        "vllm_service_status": {
                            "ok": True,
                            "service_ready": True,
                            "checked_at": "2026-06-16T20:00:01+00:00",
                            "active_services": [{"id": 42, "status": "running"}],
                        },
                        "scheduler_llm_status": {
                            "ok": True,
                            "llm_ready": True,
                            "checked_at": "2026-06-16T20:00:01+00:00",
                        },
                    }
                ),
                encoding="utf-8-sig",
            )
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            with patch("factorio_ai.remote_slurm._scheduler_api_json", side_effect=TimeoutError("slow")):
                with patch("factorio_ai.modless_lua.ModlessLuaController.observe", side_effect=RuntimeError("no server")):
                    summary = gather_run_health(cfg, observe=True)

        self.assertEqual(summary["scheduler"]["source"], "supervisor_heartbeat")
        self.assertEqual(summary["scheduler"]["vllm_services"], 1)
        self.assertEqual(summary["scheduler"]["vllm_service_ids"], [42])
        self.assertTrue(summary["scheduler"]["healthy"])
        self.assertEqual(summary["scheduler"]["api_error"], "TimeoutError")
        text = format_run_health(summary)
        self.assertIn("source=supervisor_heartbeat", text)
        self.assertIn("scheduler api slow: TimeoutError", text)
        self.assertNotIn("vLLM services=unavailable", text)

    def test_active_live_skill_keeps_autopilot_health_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stale = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
            fresh = datetime.now(timezone.utc).isoformat()
            (runtime / "autopilot-heartbeat.json").write_text(
                json.dumps({"state": "cycle_start", "cycle": 1, "pid": 4321, "updated_at": stale}),
                encoding="utf-8",
            )
            (runtime / "live-skill-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "skill": "relocate_gear_belt_mall_to_iron_source",
                        "step": 146,
                        "state": "step",
                        "pid": 4321,
                        "updated_at": fresh,
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "unattended-llm-supervisor.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "autopilot_gate": "ready",
                        "autopilot_processes": [4321],
                        "updated_at": fresh,
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            summary = gather_run_health(cfg, observe=False)

        self.assertEqual(summary["autopilot"]["age_source"], "live_skill")
        self.assertLess(summary["autopilot"]["age_seconds"], 30)
        self.assertGreater(summary["autopilot"]["heartbeat_age_seconds"], 900)
        text = format_run_health(summary)
        self.assertIn("source=live_skill", text)
        self.assertIn("heartbeat_age=", text)

    def test_live_skill_from_old_autopilot_pid_is_marked_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            fresh = datetime.now(timezone.utc).isoformat()
            (runtime / "autopilot-heartbeat.json").write_text(
                json.dumps({"state": "cycle_start", "cycle": 1, "pid": 2222, "updated_at": fresh}),
                encoding="utf-8",
            )
            (runtime / "live-skill-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "skill": "relocate_gear_belt_mall_to_iron_source",
                        "step": 282,
                        "state": "step",
                        "pid": 1111,
                        "updated_at": fresh,
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "unattended-llm-supervisor.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "autopilot_gate": "ready",
                        "autopilot_processes": [2222],
                        "updated_at": fresh,
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            summary = gather_run_health(cfg, observe=False)

        self.assertEqual(summary["autopilot"]["age_source"], "autopilot")
        self.assertIn("not a current autopilot process", summary["live_skill"]["stale_reason"])
        self.assertTrue(any(item["kind"] == "stale_live_skill_pid" for item in summary["warnings"]))
        self.assertIn("stale=live skill pid", format_run_health(summary))

    def test_non_pending_operator_layout_event_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "operator-intervention-layout-learning.jsonl").write_text(
                json.dumps(
                    {
                        "event": "operator_intervention_candidate_retracted",
                        "time": "2026-06-16T20:00:00+00:00",
                        "learning_label": "retracted_agent_action",
                        "active_skill": "connect_coal_fuel_feed",
                        "delta_summary": {"added": 14, "removed": 0, "changed": 0},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            summary = gather_run_health(cfg, observe=False)

        self.assertEqual(summary["human_layout_learning"]["learning_label"], "retracted_agent_action")
        self.assertFalse(any(item["kind"] == "operator_layout_learning_pending" for item in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
