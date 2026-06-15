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

    def test_diagnostic_layout_plan_stays_out_of_insights(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            insights = record_skill_run_journal(
                log_dir,
                objective="launch_rocket_program",
                goal="plan_factory_site",
                ok=True,
                reason="layout improvement plan: best_candidate=lab-short-daisy-chain-feed score=64.0 not_applied=true",
                steps=1,
                item_name="layout-plan",
                initial_item_count=0,
                final_item_count=0,
                target=1,
                max_steps=1,
                log_path=log_dir / "layout.jsonl",
                duration_seconds=0.25,
                repo_root=repo_root,
            )

            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(insights, [])
            self.assertEqual(summary["note_count"], 1)
            self.assertEqual(summary["insight_count"], 0)

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

    def test_skill_insight_source_loop_follows_existing_markdown_loop_number(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            (repo_root / "note.md").write_text(
                "# Factorio Loop Notes\n\n## 2026-06-15 00:00:00 +09:00 - Loop 41\n\n- Part: existing\n",
                encoding="utf-8",
            )

            record_skill_run_journal(
                log_dir,
                objective="launch_rocket_program",
                goal="build_gear_belt_mall_logistics",
                ok=True,
                reason="gear-fed belt mall logistics produced transport belts in assembler output: 2",
                steps=3,
                item_name="transport-belt",
                initial_item_count=0,
                final_item_count=2,
                target=20,
                max_steps=12,
                log_path=log_dir / "skill.jsonl",
                duration_seconds=1.25,
                repo_root=repo_root,
            )

            insight_rows = [json.loads(line) for line in (log_dir / "run-insights.jsonl").read_text().splitlines()]
            self.assertEqual(insight_rows[0]["evidence"]["source_loop"], 42)
            insight_text = (repo_root / "insight.md").read_text(encoding="utf-8")
            self.assertIn("- Source loop: Loop 42", insight_text)

    def test_markdown_append_separates_existing_entry_without_blank_line(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            (repo_root / "insight.md").write_text(
                "# Factorio Insights\n\n## 2026-06-15 00:00:00 +09:00 - Insight 7\n- Remaining risk: existing risk.",
                encoding="utf-8",
            )

            record_skill_run_journal(
                log_dir,
                objective="launch_rocket_program",
                goal="build_gear_belt_mall_logistics",
                ok=False,
                reason="still running",
                steps=3,
                item_name="transport-belt",
                initial_item_count=0,
                final_item_count=2,
                target=20,
                max_steps=12,
                log_path=log_dir / "skill.jsonl",
                duration_seconds=1.25,
                repo_root=repo_root,
            )

            insight_text = (repo_root / "insight.md").read_text(encoding="utf-8")
            self.assertIn("existing risk.\n\n##", insight_text)

    def test_layout_result_without_confirmed_before_after_stays_out_of_insights(self):
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

            self.assertIsNone(insight)
            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(summary["insight_count"], 0)

    def test_layout_result_records_confirmed_improvement_insight(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            insight = record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="optimize_green_circuit_cell",
                result={
                    "confirmed_improvement": True,
                    "source": "validation",
                    "score": 91,
                    "selected_candidate_id": "green-circuit-3-cable-2-circuit-cell",
                    "improvement": "throughput rose without adding a hand-carry dependency",
                    "before_metrics": {"throughput_per_minute": 30, "hand_carry_links": 1},
                    "after_metrics": {"throughput_per_minute": 45, "hand_carry_links": 0},
                },
                repo_root=repo_root,
            )

            self.assertIsNotNone(insight)
            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(summary["insight_count"], 1)
            self.assertEqual(summary["insights"][0]["kind"], "layout_improvement_confirmed")
            insight_text = (repo_root / "insight.md").read_text(encoding="utf-8")
            self.assertIn('"throughput_per_minute":30', insight_text)
            self.assertIn('"throughput_per_minute":45', insight_text)

    def test_layout_result_records_confirmed_learned_skill(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            insight = record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="idle:autopilot_stale",
                result={
                    "source": "llm",
                    "score": 88,
                    "selected_candidate_id": "gear-belt-direct-transfer-cell",
                    "learned_skills": [
                        {
                            "name": "direct assembler transfer",
                            "trigger": "an intermediate item is consumed by a nearby assembler",
                            "lesson": "Use an inserter directly between adjacent assemblers before adding an unnecessary belt lane.",
                            "evidence": "The selected gear-to-belt candidate moves iron gear wheels by inserter without a belt hop.",
                            "confirmed": True,
                            "confidence": 0.91,
                        }
                    ],
                },
                repo_root=repo_root,
            )

            self.assertIsNotNone(insight)
            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertEqual(summary["insight_count"], 1)
            self.assertEqual(summary["insights"][0]["kind"], "layout_skill_learned")
            self.assertEqual(summary["insights"][0]["evidence"]["source"], "layout_idle_learning")
            insight_text = (repo_root / "insight.md").read_text(encoding="utf-8")
            self.assertIn("direct assembler transfer", insight_text)

    def test_layout_result_skips_unconfirmed_or_duplicate_learned_skill(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            unconfirmed = record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="idle:autopilot_stale",
                result={
                    "source": "llm",
                    "learned_skills": [
                        {
                            "name": "direct assembler transfer",
                            "trigger": "nearby assemblers",
                            "lesson": "Use a direct inserter transfer between adjacent assemblers.",
                            "evidence": "candidate",
                            "confirmed": False,
                            "confidence": 0.5,
                        }
                    ],
                },
                repo_root=repo_root,
            )

            self.assertIsNone(unconfirmed)
            confirmed_result = {
                "source": "llm",
                "selected_candidate_id": "gear-belt-direct-transfer-cell",
                "learned_skills": [
                    {
                        "name": "direct assembler transfer",
                        "trigger": "nearby assemblers",
                        "lesson": "Use a direct inserter transfer between adjacent assemblers.",
                        "evidence": "The candidate keeps both assemblers in inserter reach and removes one belt hop.",
                        "confirmed": True,
                        "confidence": 0.9,
                    }
                ],
            }
            record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="idle:autopilot_stale",
                result=confirmed_result,
                repo_root=repo_root,
            )
            duplicate = record_layout_result_insight(
                log_dir,
                objective="launch_rocket_program",
                active_skill="idle:autopilot_stale",
                result=confirmed_result,
                repo_root=repo_root,
            )

            summary = run_journal_summary(log_dir, repo_root=repo_root)
            self.assertIsNone(duplicate)
            self.assertEqual(summary["insight_count"], 1)


if __name__ == "__main__":
    unittest.main()
