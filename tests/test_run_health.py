import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            cfg = SimpleNamespace(runtime_dir=runtime, log_dir=logs)

            summary = gather_run_health(cfg, observe=False)

        self.assertFalse(summary["server_reachable"])
        self.assertEqual(summary["autopilot"]["cycle"], 3)
        self.assertEqual(summary["live_skill"]["skill"], "research_logistics")
        self.assertIn("registered", summary["generated_skills"])
        self.assertEqual(summary["progress"]["failure_root"], "belt_line_unbuildable")
        self.assertIn("connect_coal_fuel_feed", summary["generated_skills"]["stale_implemented_queue"])
        self.assertTrue(any(item["kind"] == "failure_root_loop" for item in summary["warnings"]))
        self.assertTrue(any(item["kind"] == "implemented_skill_stuck_in_foundry_queue" for item in summary["warnings"]))
        text = format_run_health(summary)
        self.assertIn("run health", text)
        self.assertIn("research_logistics", text)
        self.assertIn("foundry", text)
        self.assertIn("failure_root=belt_line_unbuildable", text)
        self.assertIn("stale implemented queue", text)


if __name__ == "__main__":
    unittest.main()
