"""tools/critical_path.py on a small synthetic runtime."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("critical_path_tool", TOOLS / "critical_path.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

MIN = 3600


def job(key, capability, created, parameters=None):
    return {"key": key, "capability": capability, "parameters": parameters or {}, "created_tick": created}


class CriticalPathToolTests(unittest.TestCase):
    def test_timeline_waits_actor_split_and_lab_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            jobs = {
                "s": job("s", "starter", 0),
                "p": job("p", "power", 1 * MIN),
                "c": job("c", "starter_capacity", 2 * MIN, {"resource": "iron-ore", "cells": 3}),
            }
            (root / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            events = [
                {"key": "s", "event": "submitted", "time": 0, "tick": 0},
                {"key": "s", "event": "action_intent", "time": 1, "tick": 10, "action": {"type": "build"}},
                {"key": "s", "event": "terminal", "time": 2, "status": "succeeded", "evidence": {"tick": MIN}},
                {"key": "p", "event": "submitted", "time": 3, "tick": MIN},
                {"key": "c", "event": "submitted", "time": 4, "tick": 2 * MIN},
                {"key": "c", "event": "action_intent", "time": 5, "tick": 2 * MIN, "action": {"type": "build"}},
                {
                    "key": "p",
                    "event": "action_intent",
                    "time": 6,
                    "tick": 3 * MIN,
                    "action": {"type": "build", "name": "lab"},
                },
                {"key": "p", "event": "terminal", "time": 7, "status": "succeeded", "evidence": {"tick": 4 * MIN}},
                {
                    "key": "p",
                    "event": "action_intent",
                    "time": 8,
                    "tick": 5 * MIN,
                    "action": {"type": "insert", "name": "lab", "count": 6},
                },
            ]
            (root / "job-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            slices = [
                {"event": "slice", "tick": MIN, "jobs": {"p": {"status": "waiting", "reason": "x"}}},
                {
                    "event": "slice",
                    "tick": 2 * MIN,
                    "jobs": {"p": {"status": "waiting", "reason": "waiting for drill-fed iron-plate production"}},
                },
            ]
            (root / "logs" / "controller-events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in slices), encoding="utf-8"
            )
            data = tool.report(root, 0, 6, [0.5, 1.5])
            # A window off the 10 s sample grid sees the same split (was: everything slack).
            shifted = tool.report(root, 2.95, 6, [])
        self.assertIn("power", shifted["actor_seconds"]["critical"])
        self.assertEqual(data["critical_path_at"]["0.5"]["ready"], ["starter"])
        self.assertEqual(data["critical_path_at"]["1.5"]["frontier"][0], "power")
        rows = {row["job"]: row for row in data["critical_jobs"]}
        self.assertEqual(rows["power"]["end_min"], 4.0)
        self.assertEqual(rows["power"]["wait_seconds"], {"waiting for drill-fed iron-plate production": 60})
        self.assertIn("starter_capacity", data["actor_seconds"]["slack"])  # capacity is slack work
        self.assertIn("power", data["actor_seconds"]["critical"])
        # power at 4 min, 6 packs of 10 s on one lab from 5 min: 60 s idle, then busy until 6 min
        self.assertEqual(data["lab"]["lab_idle_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
