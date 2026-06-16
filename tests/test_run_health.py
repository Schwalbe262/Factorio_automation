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
        text = format_run_health(summary)
        self.assertIn("run health", text)
        self.assertIn("research_logistics", text)
        self.assertIn("foundry", text)


if __name__ == "__main__":
    unittest.main()
