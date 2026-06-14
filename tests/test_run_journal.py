from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from factorio_ai.run_journal import (
    record_autopilot_cycle_journal,
    record_layout_result_insight,
    record_skill_run_journal,
    run_journal_summary,
)


class RunJournalTests(unittest.TestCase):
    def test_records_skill_note_and_improvement_insights(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            insights = record_skill_run_journal(
                log_dir,
                objective="launch_rocket_program",
                goal="research_logistics",
                ok=True,
                reason="logistics research completed",
                steps=7,
                item_name="automation-science-pack",
                initial_item_count=0,
                final_item_count=5,
                target=5,
                max_steps=20,
                log_path=log_dir / "skill.jsonl",
                duration_seconds=1.25,
                repo_root=repo_root,
            )

            self.assertEqual(len(insights), 2)
            notes = [json.loads(line) for line in (log_dir / "run-notes.jsonl").read_text().splitlines()]
            insight_rows = [json.loads(line) for line in (log_dir / "run-insights.jsonl").read_text().splitlines()]
            self.assertEqual(notes[0]["goal"], "research_logistics")
            self.assertEqual(notes[0]["loop_number"], 1)
            self.assertEqual(insight_rows[0]["kind"], "item_count_increased")
            self.assertEqual(insight_rows[0]["insight_number"], 1)
            self.assertIn("research_logistics", (repo_root / "note.md").read_text(encoding="utf-8"))
            insight_text = (repo_root / "insight.md").read_text(encoding="utf-8")
            self.assertIn("Insight 2", insight_text)
            self.assertIn("research_logistics completed", insight_text)

    def test_autopilot_notes_do_not_require_insight(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            record_autopilot_cycle_journal(
                log_dir,
                objective="launch_rocket_program",
                cycle=1,
                selected_skill="research_logistics",
                ok=False,
                reason="executor blocked",
                duration_seconds=0.5,
                strategy={"source": "heuristic", "priority": 92},
                repo_root=repo_root,
            )

            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(summary["note_count"], 1)
            self.assertEqual(summary["insight_count"], 0)
            self.assertEqual(summary["notes"][0]["loop_type"], "autopilot_cycle")

    def test_note_number_follows_existing_markdown_loop_number(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            (repo_root / "note.md").write_text(
                "# Factorio Loop Notes\n\n## 2026-06-15 00:00:00 +09:00 - Loop 5\n\n- Part: existing\n",
                encoding="utf-8",
            )

            record_autopilot_cycle_journal(
                log_dir,
                objective="launch_rocket_program",
                cycle=1,
                selected_skill="produce_iron_plate",
                ok=True,
                reason="selected initial iron bootstrap",
                duration_seconds=0.1,
                strategy={"source": "heuristic", "priority": 96},
                repo_root=repo_root,
            )

            note_text = (repo_root / "note.md").read_text(encoding="utf-8")
            self.assertIn("Loop 6", note_text)
            rows = [json.loads(line) for line in (log_dir / "run-notes.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["loop_number"], 6)

    def test_layout_result_records_candidate_insight(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            insight = record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="idle:autopilot_stale",
                result={
                    "source": "llm",
                    "score": 0.82,
                    "selected_candidate_id": "green-circuit-3-cable-2-circuit-cell",
                },
                repo_root=repo_root,
            )

            self.assertIsNotNone(insight)
            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(summary["insight_count"], 1)
            self.assertEqual(summary["insights"][0]["kind"], "layout_candidate_improved")


if __name__ == "__main__":
    unittest.main()
