from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from factorio_ai.trace_archive import archive_training_traces, classify_trace_file, trace_archive_summary


class TraceArchiveTests(unittest.TestCase):
    def test_archive_training_traces_copies_raw_files_and_indexes_layout_sources(self):
        with TemporaryDirectory() as root:
            repo_root = Path(root)
            log_dir = repo_root / "logs"
            output_root = repo_root / "runtime" / "trace_archives"
            log_dir.mkdir(parents=True)
            (repo_root / "docs").mkdir()
            (repo_root / "note.md").write_text("# Factorio Loop Notes\n\n## Loop 1\n", encoding="utf-8")
            (repo_root / "insight.md").write_text("# Factorio Insights\n\n## Insight 1\n", encoding="utf-8")
            (repo_root / "goal.md").write_text("# Goal\n\n- Launch rocket.\n", encoding="utf-8")
            (repo_root / "docs" / "CLI_HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
            (log_dir / "layout-improvement-background.jsonl").write_text(
                json.dumps(
                    {
                        "time": "2026-06-15T00:00:00+00:00",
                        "event": "layout_result",
                        "result": {"selected_candidate_id": "compact-green-cell", "score": 91},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (log_dir / "layout-validation-feedback.jsonl").write_text(
                json.dumps({"timestamp": "2026-06-15T00:01:00+00:00", "candidate_id": "compact-green-cell"})
                + "\n",
                encoding="utf-8",
            )
            (log_dir / "llm_decisions.jsonl").write_text(
                json.dumps({"timestamp": "2026-06-15T00:02:00+00:00", "selected_skill": "plan_factory_site"})
                + "\n",
                encoding="utf-8",
            )
            (log_dir / "factorio-no-mod-server.log").write_text("server started\n", encoding="utf-8")

            result = archive_training_traces(
                log_dir,
                output_root,
                repo_root=repo_root,
                label="Part 75 Trace Preservation",
            )

            archive_dir = Path(result["archive_dir"])
            manifest = json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))
            index_rows = [
                json.loads(line)
                for line in (archive_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(result["high_value_files"], 5)
            self.assertEqual(manifest["category_counts"]["layout_background"], 1)
            self.assertEqual(manifest["category_counts"]["layout_validation"], 1)
            self.assertEqual(manifest["category_counts"]["llm_decisions"], 1)
            self.assertTrue((archive_dir / "raw" / "logs" / "layout-improvement-background.jsonl").exists())
            self.assertTrue(any(row["category"] == "layout_background" for row in index_rows))
            self.assertTrue(any(row["source_relative_path"] == "note.md" for row in index_rows))

            summary = trace_archive_summary(output_root)
            self.assertEqual(summary["archive_count"], 1)
            self.assertEqual(summary["latest"]["label"], "Part 75 Trace Preservation")
            self.assertEqual(summary["latest"]["high_value_files"], 5)

    def test_classifies_layout_and_operator_relevant_sources_as_high_value(self):
        self.assertEqual(classify_trace_file(Path("layout-improvement-background.jsonl"))[1], "high")
        self.assertEqual(classify_trace_file(Path("layout-validation-feedback.jsonl"))[0], "layout_validation")
        self.assertEqual(classify_trace_file(Path("strategy-layout-improvement-20260614-031028.jsonl"))[0], "layout_strategy")
        self.assertEqual(classify_trace_file(Path("llm_decisions.jsonl"))[0], "llm_decisions")
        self.assertEqual(classify_trace_file(Path("llm_io_traces.jsonl"))[0], "llm_io_traces")
        self.assertEqual(classify_trace_file(Path("operator-intervention-20260615.jsonl"))[0], "operator_intervention")
        self.assertEqual(classify_trace_file(Path("manual-layout-comparison.jsonl"))[1], "high")


if __name__ == "__main__":
    unittest.main()
