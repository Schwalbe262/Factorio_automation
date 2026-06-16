from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from factorio_ai.llm_log import (
    llm_io_trace_log_path,
    llm_io_trace_summary,
    llm_decision_summary,
    make_llm_io_trace,
    record_llm_decision,
    record_llm_io_trace,
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

    def test_records_and_loads_llm_io_trace_with_full_text(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            full_prompt = "prompt-" + ("x" * 7000)
            full_output = '{"selected_skill":"research_automation","reason":"' + ("y" * 7000) + '"}'
            trace = make_llm_io_trace(
                timestamp="2026-06-13T00:00:00+00:00",
                trace_id="trace-a",
                kind="strategy",
                provider="local_llm",
                model="Qwen/Qwen3.5-9B",
                base_url="http://127.0.0.1:8000/v1",
                task_id="strategy-1",
                system_prompt="Return JSON",
                input_prompt=full_prompt,
                raw_output=full_output,
                parsed_json={"selected_skill": "research_automation"},
                duration_ms=123,
                max_tokens=512,
                ok=True,
            )

            entry = record_llm_io_trace(log_dir, trace)
            self.assertEqual(entry.trace_id, "trace-a")
            self.assertEqual(entry.prompt_chars, len("Return JSON") + len(full_prompt))
            self.assertEqual(entry.response_chars, len(full_output))

            summary = llm_io_trace_summary(log_dir)
            self.assertEqual(summary["entry_count"], 1)
            self.assertEqual(summary["latest"]["raw_output"], full_output)
            self.assertEqual(summary["entries"][0]["input_prompt"], full_prompt)
            self.assertEqual(summary["log_path"], str(llm_io_trace_log_path(log_dir)))

    def test_llm_io_trace_summary_returns_newest_first(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            for trace_id in ("old", "new"):
                record_llm_io_trace(
                    log_dir,
                    make_llm_io_trace(
                        trace_id=trace_id,
                        kind="strategy",
                        provider="local_llm",
                        model="model",
                        base_url="http://localhost",
                        system_prompt="system",
                        input_prompt=trace_id,
                        raw_output="{}",
                        parsed_json={},
                        ok=True,
                    ),
                )

            summary = llm_io_trace_summary(log_dir)
            self.assertEqual([row["trace_id"] for row in summary["entries"]], ["new", "old"])

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
