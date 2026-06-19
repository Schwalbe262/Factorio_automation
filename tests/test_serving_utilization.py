import unittest
from datetime import datetime

from tools.serving_utilization import serving_utilization


def _task(name, started, finished, state="finished"):
    return {"name": name, "started_at": started, "finished_at": finished, "state": state}


class ServingUtilizationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 19, 9, 30, 0)

    def test_busy_fraction_serial_requests(self):
        # 3 strategy requests of 60s each in a 10-min window -> 180s busy / 600s = 30%
        tasks = [
            _task("factorio-strategy-1", "2026-06-19 09:21:00", "2026-06-19 09:22:00"),
            _task("factorio-strategy-2", "2026-06-19 09:24:00", "2026-06-19 09:25:00"),
            _task("factorio-strategy-3", "2026-06-19 09:27:00", "2026-06-19 09:28:00"),
        ]
        rep = serving_utilization(tasks, self.now, window_minutes=10)
        self.assertEqual(rep["requests"], 3)
        self.assertEqual(rep["busy_fraction"], 0.3)
        self.assertEqual(rep["idle_fraction"], 0.7)
        self.assertEqual(rep["avg_latency_seconds"], 60.0)

    def test_excludes_stale_old_run(self):
        # a request that started before the window is ignored (avoids polluting from an old run)
        tasks = [
            _task("factorio-strategy-old", "2026-06-19 08:00:00", "2026-06-19 08:04:00"),
            _task("factorio-strategy-new", "2026-06-19 09:25:00", "2026-06-19 09:26:00"),
        ]
        rep = serving_utilization(tasks, self.now, window_minutes=10)
        self.assertEqual(rep["requests"], 1)  # only the new one

    def test_excludes_service_task_and_detects_running(self):
        tasks = [
            _task("factorio-vllm-service-p8000", "2026-06-19 09:00:00", None, state="running"),
            _task("factorio-foundry-1", "2026-06-19 09:28:00", "2026-06-19 09:29:00"),
        ]
        rep = serving_utilization(tasks, self.now, window_minutes=10)
        self.assertTrue(rep["service_running"])
        self.assertEqual(rep["requests"], 1)  # service not counted as a request
        self.assertEqual(rep["by_kind"], {"foundry": 1})

    def test_busy_capped_at_window(self):
        # overlapping/long requests can't report >100% busy
        tasks = [
            _task("factorio-strategy-a", "2026-06-19 09:20:00", "2026-06-19 09:29:00"),
            _task("factorio-layout-b", "2026-06-19 09:20:00", "2026-06-19 09:29:00"),
        ]
        rep = serving_utilization(tasks, self.now, window_minutes=10)
        self.assertLessEqual(rep["busy_fraction"], 1.0)


if __name__ == "__main__":
    unittest.main()
