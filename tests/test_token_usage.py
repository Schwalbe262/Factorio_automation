from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.token_usage import (
    current_codex_thread_usage,
    record_current_codex_thread_usage,
    record_token_usage,
    summarize_codex_session_usage,
    token_usage_summary,
)


class TokenUsageTests(unittest.TestCase):
    def test_records_token_usage_samples_and_delta_summary(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            first = record_token_usage(log_dir, 1000, label="start", timestamp="2026-06-13T00:00:00+00:00")
            second = record_token_usage(log_dir, 1250, label="ui work", timestamp="2026-06-13T00:01:00+00:00")

            self.assertEqual(first.delta_tokens, 0)
            self.assertEqual(second.delta_tokens, 250)

            summary = token_usage_summary(log_dir)
            self.assertEqual(summary["sample_count"], 2)
            self.assertEqual(summary["latest_tokens"], 1250)
            self.assertEqual(summary["total_delta_tokens"], 250)
            self.assertEqual(summary["samples"][-1]["label"], "ui work")

    def test_weekly_quota_percent_is_optional(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            record_token_usage(log_dir, 1000, label="start", timestamp="2026-06-13T00:00:00+00:00")
            record_token_usage(log_dir, 1250, label="ui work", timestamp="2026-06-13T00:01:00+00:00")

            with patch.dict("os.environ", {"FACTORIO_AI_WEEKLY_TOKEN_QUOTA": "1000"}):
                summary = token_usage_summary(log_dir)
            self.assertEqual(summary["weekly_quota_tokens"], 1000)
            self.assertEqual(summary["latest_delta_tokens"], 250)
            self.assertEqual(summary["latest_weekly_percent"], 25.0)
            self.assertEqual(summary["samples"][-1]["weekly_percent"], 25.0)

            with patch.dict("os.environ", {}, clear=True):
                summary_without_quota = token_usage_summary(log_dir)
            self.assertIsNone(summary_without_quota["weekly_quota_tokens"])
            self.assertIsNone(summary_without_quota["latest_weekly_percent"])

    def test_counter_reset_continues_cumulative_display_tokens(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            record_token_usage(log_dir, 1000, label="start", timestamp="2026-06-13T00:00:00+00:00")
            record_token_usage(log_dir, 1250, label="first work", timestamp="2026-06-13T00:01:00+00:00")
            reset = record_token_usage(
                log_dir,
                100,
                label="new counter work",
                timestamp="2026-06-13T00:02:00+00:00",
            )

            self.assertEqual(reset.delta_tokens, 100)

            summary = token_usage_summary(log_dir)
            self.assertEqual(summary["latest_raw_tokens"], 100)
            self.assertEqual(summary["latest_tokens"], 1350)
            self.assertEqual(summary["total_delta_tokens"], 350)
            self.assertEqual(summary["latest_delta_tokens"], 100)
            self.assertEqual(summary["counter_reset_count"], 1)
            self.assertTrue(summary["latest_counter_reset"])
            self.assertEqual(summary["samples"][-1]["cumulative_tokens"], 1350)
            self.assertEqual(summary["samples"][-1]["tokens_used"], 100)

    def test_codex_thread_source_starts_new_counter_basis(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root)
            record_token_usage(
                log_dir,
                17_735_896,
                label="goal counter sample",
                source="codex",
                timestamp="2026-06-15T09:46:39+00:00",
            )
            first_thread = record_token_usage(
                log_dir,
                547_398_662,
                label="thread counter baseline",
                source="codex_thread",
                timestamp="2026-06-15T10:04:54+00:00",
            )
            record_token_usage(
                log_dir,
                548_238_295,
                label="thread counter followup",
                source="codex_thread",
                timestamp="2026-06-15T10:06:59+00:00",
            )

            self.assertEqual(first_thread.delta_tokens, 529_662_766)

            summary = token_usage_summary(log_dir)
            self.assertEqual(summary["sample_basis_source"], "codex_thread")
            self.assertEqual(summary["ignored_older_basis_samples"], 1)
            self.assertEqual(summary["sample_count"], 2)
            self.assertEqual(summary["latest_raw_tokens"], 548_238_295)
            self.assertEqual(summary["latest_tokens"], 548_238_295)
            self.assertEqual(summary["total_delta_tokens"], 839_633)
            self.assertEqual(summary["latest_delta_tokens"], 839_633)
            self.assertEqual(summary["samples"][0]["delta_tokens"], 0)
            self.assertEqual(summary["samples"][0]["cumulative_tokens"], 547_398_662)

    def test_current_codex_thread_usage_selects_latest_factorio_thread(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "state_5.sqlite"
            _create_threads_fixture(db_path)

            thread = current_codex_thread_usage(
                state_db_path=db_path,
                cwd=r"C:\Users\NEC\Documents\Factorio",
            )

            self.assertEqual(thread.thread_id, "factorio-latest")
            self.assertEqual(thread.tokens_used, 2200)
            self.assertEqual(thread.updated_at_ms, 2000)

    def test_current_codex_thread_usage_prefers_thread_id(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "state_5.sqlite"
            _create_threads_fixture(db_path)

            thread = current_codex_thread_usage(
                state_db_path=db_path,
                cwd=r"C:\Users\NEC\Documents\Factorio",
                thread_id="other-cwd",
            )

            self.assertEqual(thread.thread_id, "other-cwd")
            self.assertEqual(thread.tokens_used, 3300)

    def test_records_current_codex_thread_usage_sample(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "state_5.sqlite"
            log_dir = Path(root) / "logs"
            _create_threads_fixture(db_path)

            sample, thread = record_current_codex_thread_usage(
                log_dir,
                state_db_path=db_path,
                cwd=r"C:\Users\NEC\Documents\Factorio",
                label="thread sample",
                timestamp="2026-06-15T00:00:00+00:00",
            )

            self.assertEqual(thread.thread_id, "factorio-latest")
            self.assertEqual(sample.tokens_used, 2200)
            self.assertEqual(sample.source, "codex_thread")
            self.assertEqual(token_usage_summary(log_dir)["latest_raw_tokens"], 2200)


class CodexSessionUsageTests(unittest.TestCase):
    def _write_session(self, root, *, thread="exact-thread", events=None):
        path = Path(root) / "sessions" / "2026" / "09" / "08" / f"rollout-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"type": "session_meta", "payload": {"id": thread, "cwd": "C:/Factorio"}}
        rows = [metadata] + (events if events is not None else [self._event(1500)])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def _event(self, total, limits=None):
        return {"type": "event_msg", "timestamp": "2026-09-08T00:00:00Z", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"total_tokens": total}} if total is not None else None,
            "rate_limits": limits,
        }}

    def test_missing_database_falls_back_to_exact_session(self):
        with TemporaryDirectory() as root:
            self._write_session(root)
            usage = current_codex_thread_usage(state_db_path=Path(root) / "state_5.sqlite", thread_id="exact-thread")
            self.assertEqual((usage.thread_id, usage.tokens_used, usage.source), ("exact-thread", 1500, "codex_session_jsonl"))

    def test_absent_database_row_uses_requested_thread_not_latest_checkout(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state_5.sqlite"
            _create_threads_fixture(path)
            self._write_session(root)
            usage = current_codex_thread_usage(state_db_path=path, thread_id="exact-thread")
            self.assertEqual(usage.tokens_used, 1500)
            self.assertEqual(usage.thread_id, "exact-thread")

    def test_malformed_database_falls_back_read_only(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state_5.sqlite"
            path.write_bytes(b"malformed database")
            self._write_session(root)
            usage = current_codex_thread_usage(state_db_path=path, thread_id="exact-thread")
            self.assertEqual(usage.tokens_used, 1500)
            self.assertEqual(path.read_bytes(), b"malformed database")

    def test_weekly_window_identified_by_duration_not_slot(self):
        for slot in ["primary", "secondary"]:
            with self.subTest(slot=slot), TemporaryDirectory() as root:
                limits = {"primary": {"window_minutes": 300, "used_percent": 90},
                          "secondary": {"window_minutes": 60, "used_percent": 99}}
                limits[slot] = {"window_minutes": 10080, "used_percent": 16.0, "resets_at": 1789435533}
                path = self._write_session(root, events=[self._event(100), self._event(1500, limits)])
                usage = summarize_codex_session_usage(path)
                self.assertEqual(usage["tokens_used"], 1500)
                self.assertEqual(usage["weekly_used_percent"], 16.0)
                self.assertEqual(usage["weekly_resets_at"], 1789435533)
                self.assertEqual(usage["weekly_percent_basis"], "account_usage")

    def test_nonweekly_secondary_is_not_mislabeled_weekly(self):
        with TemporaryDirectory() as root:
            path = self._write_session(root, events=[self._event(10, {"secondary": {"window_minutes": 300, "used_percent": 20}})])
            self.assertIsNone(summarize_codex_session_usage(path)["weekly_used_percent"])

    def test_new_rate_event_without_tokens_and_partial_line_keep_latest_counter(self):
        with TemporaryDirectory() as root:
            path = self._write_session(root, events=[self._event(100), self._event(900), self._event(None, {
                "primary": {"window_minutes": 10080, "used_percent": 17}
            })])
            with path.open("a", encoding="utf-8") as file:
                file.write('{"type":"event_msg","payload":')
            usage = summarize_codex_session_usage(path)
            self.assertEqual(usage["tokens_used"], 900)
            self.assertEqual(usage["weekly_used_percent"], 17)

    def test_wrong_metadata_never_reports_another_threads_tokens(self):
        with TemporaryDirectory() as root:
            path = self._write_session(root, thread="someone-else")
            with self.assertRaisesRegex(ValueError, "does not match"):
                current_codex_thread_usage(session_path=path, thread_id="exact-thread")

    def test_bounded_tail_does_not_scan_historical_counter(self):
        with TemporaryDirectory() as root:
            path = self._write_session(root, events=[self._event(900), {"type": "other", "payload": "x" * 4096}])
            with self.assertRaisesRegex(ValueError, "bounded"):
                summarize_codex_session_usage(path, max_tail_bytes=1024)

    def test_tail_aligned_on_line_boundary_keeps_complete_event(self):
        with TemporaryDirectory() as root:
            event = self._event(75)
            path = self._write_session(root, events=[event])
            usage = summarize_codex_session_usage(path, max_tail_bytes=len(path.read_bytes().splitlines(keepends=True)[-1]))
            self.assertEqual(usage["tokens_used"], 75)


def _create_threads_fixture(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, tokens_used INTEGER, updated_at_ms INTEGER, updated_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO threads (id, cwd, tokens_used, updated_at_ms, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("factorio-old", r"C:\Users\NEC\Documents\Factorio", 1000, 1000, 1),
                ("factorio-latest", r"\\?\C:\Users\NEC\Documents\Factorio", 2200, 2000, 2),
                ("other-cwd", r"C:\Users\NEC\Documents\Other", 3300, 3000, 3),
            ],
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
