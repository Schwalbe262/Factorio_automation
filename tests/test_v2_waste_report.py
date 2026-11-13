"""tools/waste_report.py on small synthetic runtimes."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("waste_report_tool", TOOLS / "waste_report.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

S = 60  # ticks per second


def write_run(root: Path, jobs: dict, events: list, slices: list):
    (root / "logs").mkdir(parents=True)
    (root / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    (root / "job-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    rows = [dict(s, event="slice") for s in slices]
    (root / "logs" / "controller-events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in rows), encoding="utf-8"
    )


def act(key, tick, **action):
    return {"key": key, "event": "action_intent", "time": tick, "tick": tick, "action": action}


class WasteReportTests(unittest.TestCase):
    def synthetic(self, root: Path, seed: str):
        run = root / f"fleet-{seed}"
        jobs = {
            "b": {"capability": "power", "parameters": {}, "created_tick": 0},
            "w": {"capability": "planned_main_bus", "parameters": {}, "created_tick": 0},
            "x": {"capability": "supply_cell", "parameters": {}, "created_tick": 0},
        }
        boiler = {"x": 1.5, "y": 2.5}
        events = [{"key": "b", "event": "submitted", "time": 0, "tick": 0}]
        tick = 10
        for _ in range(4):  # wood in / wood out, the boiler loop
            events.append(
                act(
                    "b",
                    tick,
                    type="insert",
                    name="boiler",
                    item="wood",
                    count=3,
                    position=boiler,
                    reason="hand-fed boiler: refill",
                )
            )
            events.append(
                act(
                    "b",
                    tick + 60,
                    type="take",
                    name="boiler",
                    item="wood",
                    count=3,
                    position=boiler,
                    reason="take wood back",
                )
            )
            tick += 120
        events.append(act("x", tick, type="build", name="stone-furnace", position={"x": 9, "y": 9}, reason="build"))
        events.append(
            act("x", tick + 600, type="mine", name="stone-furnace", position={"x": 9, "y": 9}, reason="recover")
        )
        events.append(
            {
                "key": "x",
                "event": "terminal",
                "time": tick + 700,
                "status": "blocked",
                "reason": "no_site",
                "evidence": {"tick": tick + 700},
            }
        )
        events.append(act("b", 400 * S, type="build", name="pipe", position={"x": 0, "y": 0}, reason="build"))
        slices = [
            {"tick": t * S, "jobs": {"w": {"status": "waiting", "reason": "waiting for district plates"}}}
            for t in range(0, 301, 25)
        ]
        slices.append({"tick": 325 * S, "jobs": {"w": {"status": "succeeded", "reason": "done"}}})
        write_run(run, jobs, events, slices)
        return run

    def test_waste_types_from_logs_and_fleet_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.synthetic(root, "1")
            self.synthetic(root, "2")
            data = tool.analyse(run)
            waste = data["waste"]
            loop = waste["action_loop"]
            self.assertEqual(loop["count"], 8)
            self.assertIn("take/insert wood on boiler", loop["example"]["reason"])
            wait = waste["long_wait"]["example"]
            self.assertEqual(
                (wait["job"], wait["reason"], wait["seconds"]), ("planned_main_bus", "waiting for district plates", 300)
            )
            self.assertEqual(waste["rework"]["count"], 1)
            self.assertEqual(waste["blocked_unretried"]["example"]["job"], "supply_cell")
            self.assertGreater(waste["actor_idle"]["seconds"], 0)
            runs = tool.runs_for("fleet", root)
            self.assertEqual(len(runs), 2)
            total = tool.aggregate([tool.analyse(r) for r in runs])
            self.assertEqual(total["long_wait"]["total_seconds"], 600)
            self.assertEqual(total["long_wait"]["causes"][0]["runs"], 2)
            self.assertEqual(list(total)[0], max(total, key=lambda k: total[k]["total_seconds"]))


if __name__ == "__main__":
    unittest.main()
