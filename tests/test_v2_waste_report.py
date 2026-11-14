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

    def test_dispatch_latency_names_late_proposal_admission_wait_and_research_lane(self):
        class Run:
            start = {"r": 0, "l": 400 * S}
            end = {"r": (390 * S, "succeeded", "")}

            @staticmethod
            def capability(key):
                return {"r": "bootstrap_research", "l": "bootstrap_logistics"}[key]

        cause = tool._dispatch_cause(Run, 30 * S, 10 * S, 400 * S, "bootstrap_logistics")
        self.assertIn("proposed +20s, admitted 370s after that", cause)
        self.assertIn("research lane: bootstrap_research", cause)
        self.assertIn("proposal time unknown", tool._dispatch_cause(Run, None, 0, 1, "ammo_production"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = [{"job_key": "l", "created_at": 105.0}, {"job_key": "x"}]
            (root / "work_queue.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
            clock = type("R", (), {"clock": [(100.0, 6000), (110.0, 6600)]})
            self.assertEqual(tool._proposal_ticks(clock, root), {"l": 6000})

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


M = 3600  # ticks per game minute


def end(key, minute):
    return {
        "key": key,
        "event": "terminal",
        "time": minute * M,
        "status": "succeeded",
        "evidence": {"tick": minute * M},
    }


def result(key, tick, **values):
    return {"key": key, "event": "action_result", "time": tick, "tick": tick, "result": values}


class CriticalChainTests(unittest.TestCase):
    """The critical chain section: segments to the 20-minute target and what bound each."""

    def chain_run(self, root: Path, labs: int, last_minute: float):
        run = root / f"chain-{labs}"
        jobs = {
            "p": {"capability": "power", "parameters": {}},
            "e": {"capability": "bootstrap_research", "parameters": {"technology": "electric-mining-drill"}},
            "l": {"capability": "bootstrap_logistics", "parameters": {}},
            "i": {"capability": "mining_outpost", "parameters": {"resource": "iron-ore", "rate_per_minute": 60}},
            "d": {"capability": "smelting_district", "parameters": {}},
            "b": {"capability": "planned_main_bus", "parameters": {}},
        }
        furnace = {"name": "stone-furnace", "position": {"x": 0, "y": 0}}
        splitter = {"name": "splitter", "position": {"x": 1.5, "y": 3}}
        copper = {"name": "stone-furnace", "position": {"x": 9, "y": 0}}
        events = [end("p", 7)]
        events += [
            act("e", 8 * M + n, type="build", name="lab", position={"x": 20 + 3 * n, "y": 0}) for n in range(labs)
        ]
        events += [act("e", 11 * M, type="research", technology="electric-mining-drill"), end("e", 14)]
        events += [act("l", 15 * M, type="research", technology="logistics"), end("l", 18), end("i", 16)]
        events += [act("d", 15 * M, type="build", **furnace), act("d", int(last_minute * M), type="build", **splitter)]
        events += [act("d", 20 * M, type="build", **copper)]
        for minute in (21, 22, 23):  # starter furnaces empty: every take moves less than asked
            events += [
                act("d", minute * M, type="take_many", item="iron-plate", count=30),
                result("d", minute * M, moved=9),
            ]
        events += [end("d", 24), end("b", 26)]
        write_run(run, jobs, events, [])
        catalog = {
            "technologies": {
                "electric-mining-drill": {"unit_count": 25, "unit_energy": 600},
                "logistics": {"unit_count": 20, "unit_energy": 900},
            }
        }
        (run / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        banks = [
            {"resource": "iron-ore", "entities": [furnace, splitter]},
            {"resource": "copper-ore", "entities": [copper]},
        ]
        district = {"plan": {"entities": [furnace, splitter, copper]}, "banks": banks}
        (run / "smelting-district.json").write_text(json.dumps(district), encoding="utf-8")
        return run

    def test_segments_and_binding_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = {r["segment"]: r for r in tool.critical_chain(self.chain_run(Path(directory), 1, 18.5))}
            self.assertEqual(rows["research"]["binding"], "labs: 1 lab")
            self.assertEqual((rows["research"]["start"], rows["research"]["end"]), (11.0, 18.0))
            self.assertEqual(rows["producer"]["binding"], "logistics research")  # splitter 0.5 min after logistics
            self.assertEqual((rows["producer"]["start"], rows["producer"]["end"]), (15.0, 18.5))
            self.assertEqual(rows["district"]["binding"], "plates")
            self.assertEqual(rows["district"]["minutes"], 5.5)
            self.assertEqual(rows["bus"]["binding"], "not plates")

    def test_a_late_splitter_is_its_own_wait_and_two_labs_are_lab_time_or_funding(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.chain_run(Path(directory), 2, 21.0)
            rows = {r["segment"]: r for r in tool.critical_chain(run)}
            self.assertEqual(rows["producer"]["binding"], "waits for splitter")
            self.assertEqual(rows["research"]["binding"], "pack supply / funding")  # 420 s against 275 s of lab time
            totals = {t["segment"]: t for t in tool.chain_totals([{"critical_chain": tool.critical_chain(run)}])}
            self.assertEqual(totals["producer"]["binding"], {"waits for splitter": 1})
            self.assertEqual(totals["bus"]["median_end"], 26.0)

    def test_plate_supply_bound_from_iron_cells_and_district_furnaces(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.chain_run(Path(directory), 1, 18.5)
            events = (run / "job-events.jsonl").read_text(encoding="utf-8")
            drills = [  # two iron cells from minute 0, one coal cell (not iron supply)
                act("p", n, type="build", name="burner-mining-drill", position={"x": n, "y": 9}, reason=reason)
                for n, reason in ((1, "build direct iron-ore supply cell"), (2, "expand iron-ore starter capacity"))
            ]
            drills.append(
                act("p", 3, type="build", name="burner-mining-drill", position={"x": 3, "y": 9}, reason="coal")
            )
            (run / "job-events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in drills) + events, encoding="utf-8"
            )
            bound = tool.plate_supply_bound(run)
            # 3 takes of 9 plates by the district end: 27 plates; 2 cells x 15/min pay them by 0.9 min.
            self.assertEqual(bound["district"], {"actual": 24.0, "bound": 0.9, "bill": 27})
            self.assertEqual(bound["bus"]["bill"], 27)
            # Supply starts: 2 iron cells, then the district iron furnace once the prefix (18.5) is complete.
            starts = tool._iron_supply_starts(tool.cp.Run(run), run, int(18.5 * 3600))
            self.assertEqual([rate for _, rate in starts], [15.0, 15.0, 18.75])
            self.assertEqual(tool._bound_tick([(0, 15.0)], 30), 2 * 3600)
            self.assertIsNone(tool._bound_tick([], 5))


if __name__ == "__main__":
    unittest.main()
