from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from factorio_ai.llm_log import (
    llm_decision_summary,
    record_llm_decision,
    strategy_request_summary,
)


class LlmLogTests(unittest.TestCase):
    def test_records_llm_decision_attempts(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            entry = record_llm_decision(
                log_dir,
                objective="launch_rocket_program",
                provider="local_llm",
                result={
                    "source": "heuristic",
                    "selected_skill": "research_automation",
                    "priority": 90,
                    "reason": "LLM unavailable; fallback selected research",
                    "blockers": ["automation research"],
                    "expected_effect": "feed labs",
                },
                request_summary={"tick": 100},
                error="LLM unavailable or invalid response; used heuristic fallback",
                latency_ms=12,
                timestamp="2026-06-13T00:00:00+00:00",
            )

            self.assertFalse(entry.ok)
            self.assertEqual(entry.selected_skill, "research_automation")

            summary = llm_decision_summary(log_dir)
            self.assertEqual(summary["entry_count"], 1)
            self.assertEqual(summary["latest"]["provider"], "local_llm")
            self.assertEqual(summary["latest"]["latency_ms"], 12)

    def test_strategy_request_summary_keeps_operator_relevant_context(self):
        summary = strategy_request_summary(
            {
                "tick": 42,
                "inventory": {"iron-plate": 10},
                "enemies": [{"name": "small-biter"}],
                "research": {
                    "current": "automation",
                    "technologies": {"automation": {"researched": False}, "steam-power": {"researched": True}},
                },
            },
            {"automation-science-pack": 30.0},
        )
        self.assertEqual(summary["tick"], 42)
        self.assertEqual(summary["enemy_count"], 1)
        self.assertEqual(summary["current_research"], "automation")
        self.assertEqual(summary["researched_technologies"], ["steam-power"])


if __name__ == "__main__":
    unittest.main()
