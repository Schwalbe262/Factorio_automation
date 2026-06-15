from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.token_usage import (
    current_codex_thread_usage,
    record_current_codex_thread_usage,
    record_token_usage,
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

    def test_current_codex_thread_usage_matches_slash_and_case_cwd_variant(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "state_5.sqlite"
            _create_threads_fixture(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO threads (id, cwd, tokens_used, updated_at_ms, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("factorio-slash-case", r"C:/USERS/NEC/Documents/Factorio", 4400, 4000, 4),
                )
                conn.commit()
            finally:
                conn.close()

            thread = current_codex_thread_usage(
                state_db_path=db_path,
                cwd=r"\\?\C:\Users\NEC\Documents\Factorio",
            )

            self.assertEqual(thread.thread_id, "factorio-slash-case")
            self.assertEqual(thread.tokens_used, 4400)

    def test_current_codex_thread_usage_queries_selected_cwd_row(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "state_5.sqlite"
            db_path.write_bytes(b"")
            conn = _RecordingThreadConnection(
                {
                    "id": "factorio-latest",
                    "cwd": r"\\?\C:\Users\NEC\Documents\Factorio",
                    "tokens_used": 2200,
                    "updated_at_ms": 2000,
                    "updated_at": 2,
                }
            )

            with patch("factorio_ai.token_usage.sqlite3.connect", return_value=conn):
                thread = current_codex_thread_usage(
                    state_db_path=db_path,
                    cwd=r"C:\Users\NEC\Documents\Factorio",
                )

            self.assertEqual(thread.thread_id, "factorio-latest")
            self.assertEqual(thread.tokens_used, 2200)
            self.assertTrue(conn.closed)
            self.assertEqual(len(conn.statements), 1)
            self.assertIn("WHERE", conn.statements[0].upper())
            self.assertIn("LIMIT 1", conn.statements[0].upper())

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


class _RecordingThreadConnection:
    def __init__(self, row: dict[str, object]):
        self.row = row
        self.row_factory = None
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str, params: tuple[object, ...] = ()) -> "_RecordingThreadCursor":
        compact_statement = " ".join(statement.split())
        if "SELECT id, cwd, tokens_used, updated_at_ms, updated_at FROM threads" not in compact_statement:
            raise AssertionError(statement)
        self.statements.append(statement)
        return _RecordingThreadCursor(self.row)

    def close(self) -> None:
        self.closed = True


class _RecordingThreadCursor:
    def __init__(self, row: dict[str, object]):
        self.row = row

    def fetchone(self) -> dict[str, object]:
        return self.row

    def fetchall(self) -> list[dict[str, object]]:
        raise AssertionError("current Codex thread selection must not fetch all thread rows")


if __name__ == "__main__":
    unittest.main()
