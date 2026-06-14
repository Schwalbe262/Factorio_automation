from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.token_usage import record_token_usage, token_usage_summary


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


if __name__ == "__main__":
    unittest.main()
