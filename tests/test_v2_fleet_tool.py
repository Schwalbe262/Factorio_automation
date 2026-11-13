"""tools/fleet.py: the pure parts (ports, seeds, capacity, manifest, report aggregation and table)."""

import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("fleet_tool", TOOLS / "fleet.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)

MIN = 3600


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture_run(root, seed, *, ends, blocked=(), errors=0, tick=None):
    """A runtime folder with jobs, terminal events and controller state; ``ends``: capability -> minute."""
    jobs, events = {}, []
    params = {
        "starter_research": {"technology": "automation"},
        "bootstrap_research": {"technology": "electric-mining-drill"},
    }
    for n, (capability, minute) in enumerate(ends.items()):
        key = f"k{n}"
        capability, _, resource = capability.partition(":")
        parameters = dict(params.get(capability, {}))
        if resource:
            parameters = {"resource": resource, "rate_per_minute": 30 if resource == "coal" else 60}
        jobs[key] = dict(key=key, capability=capability, parameters=parameters, status="succeeded", created_tick=0)
        jobs[key]["last_tick"] = int(minute * MIN)
        events.append(dict(key=key, event="submitted", time=n, tick=0))
        events.append(
            dict(key=key, event="terminal", time=n + 0.5, status="succeeded", evidence={"tick": int(minute * MIN)})
        )
    for n, (capability, reason) in enumerate(blocked):
        key = f"b{n}"
        jobs[key] = dict(key=key, capability=capability, parameters={}, status="blocked", reason=reason)
    write(root / "jobs.json", {"jobs": jobs})
    (root / "job-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    rows = [dict(time=100 + i, event="error", reason="bridge timeout 12") for i in range(errors)]
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "controller-events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    write(root / "logs" / "controller-state.json", dict(status="running", last_observed_tick=tick))
    return dict(seed=seed, runtime=str(root), status="running")


class PureTests(unittest.TestCase):
    def test_ports_skip_registered_and_busy_pairs(self):
        busy = {(34401, 27401)}
        pairs = fleet.allocate_ports(3, {34400}, probe=lambda u, t: (u, t) not in busy)
        self.assertEqual(pairs, [(34402, 27402), (34403, 27403), (34404, 27404)])
        with self.assertRaises(RuntimeError):
            fleet.allocate_ports(5, set(), probe=lambda u, t: u < 34402, span=10)

    def test_registered_ports_reads_runtimes_manifests_and_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write(base / "v2" / "eval-1" / "run.json", dict(server_port=34261, rcon_port=27061))
            write(base / "active-runtime.json", dict(server_port=34262, rcon_port=27062))
            write(base / "fleets" / "f.json", dict(runs=[dict(server_port=34400, rcon_port=27400)]))
            (base / "fleets" / "bad.json").write_text("{", encoding="utf-8")
            self.assertEqual(fleet.registered_ports(base), {34261, 27061, 34262, 27062, 34400, 27400})

    def test_seeds(self):
        seeds = fleet.parse_seeds("random", 4, random.Random(1))
        self.assertEqual(len(set(seeds)), 4)
        self.assertEqual(fleet.parse_seeds("5,7", None), [5, 7])
        for text, count in (("5,5", None), ("5,7", 3), ("-1", None), ("random", None)):
            with self.assertRaises(ValueError):
                fleet.parse_seeds(text, count)

    def test_capacity_keeps_a_reserve(self):
        fits, why = fleet.capacity(36000, 32, per_run_mb=1500, per_run_cores=1.5)
        self.assertEqual(why, dict(by_ram=(36000 - fleet.RESERVE_MB) // 1500, by_cpu=(32 - fleet.RESERVE_CORES) // 1.5))
        self.assertEqual(fits, min(why.values()))
        self.assertEqual(fleet.capacity(1000, 2)[0], 0)

    def test_reason_normalization_groups_numbers(self):
        self.assertEqual(
            fleet.normalize_reason("site at (12.5,-30) blocked by 3 entities"),
            fleet.normalize_reason("site at (1,7) blocked by 14 entities"),
        )
        self.assertEqual(fleet.window("0-30"), (0.0, 30.0))
        with self.assertRaises(ValueError):
            fleet.window("30-10")

    def test_run_cost_uses_the_sample_interval(self):
        first = [dict(mb=300, cpu=10.0), dict(mb=600, cpu=20.0)]
        second = [dict(mb=350, cpu=10.6), dict(mb=700, cpu=21.5)]
        self.assertEqual(
            fleet.run_cost(first, second, 3),
            dict(mb=1050, cores=0.7, parts=[dict(mb=350, cores=0.2), dict(mb=700, cores=0.5)]),
        )


class ManifestTests(unittest.TestCase):
    def test_update_and_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = fleet.Fleet(base=directory)
            tool.dir.mkdir(parents=True)
            write(tool.manifest_path("a"), dict(kind="factorio-fleet-v1", name="a", runs=[dict(seed=1, status="x")]))
            write(tool.dir / "cost.json", dict(per_run_mb=2900, per_run_cores=0.4))
            self.assertEqual(tool.latest(), "a")
            tool._set_run("a", 1, status="running", server_port=34400)
            self.assertEqual(tool.load()["runs"][0], dict(seed=1, status="running", server_port=34400))
            cost = tool.cost()
            self.assertEqual((cost["per_run_mb"], cost["per_run_cores"]), (2900, fleet.DEFAULT_RUN_CORES))
            with self.assertRaises(ValueError):
                tool.manifest_path("../escape")

    def test_start_refuses_above_capacity_and_existing_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = fleet.Fleet(base=directory)
            original = fleet.machine
            fleet.machine = lambda: dict(free_mb=fleet.RESERVE_MB + 1600, total_mb=8000, cores=32)
            try:
                with self.assertRaisesRegex(RuntimeError, "exceed"):
                    tool.start(2, "1,2", None, "t", out=lambda *_: None)
                (Path(directory) / "v2" / "t-1").mkdir(parents=True)
                with self.assertRaisesRegex(FileExistsError, "t-1"):
                    tool.start(1, "1", None, "t", out=lambda *_: None)
            finally:
                fleet.machine = original
            self.assertFalse(tool.manifest_path("t").exists())


class ReportTests(unittest.TestCase):
    def test_aggregate_and_table_from_fixture_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runs = [
                fixture_run(
                    base / "a",
                    11,
                    ends={"starter": 2, "power": 10, "starter_research": 14, "mining_outpost:iron-ore": 20},
                    blocked=[("mining_outpost", "no site within 120 tiles of (3,4)")],
                    errors=2,
                    tick=25 * MIN,
                ),
                fixture_run(
                    base / "b",
                    22,
                    ends={"starter": 1, "power": 12, "starter_research": 18, "bootstrap_research": 21},
                    blocked=[("smelting_district", "no site within 80 tiles of (9,9)")],
                    tick=22 * MIN,
                ),
                fixture_run(base / "c", 33, ends={"starter": 3, "power": 14}, tick=8 * MIN),
            ]
            tool = fleet.Fleet(base=directory)
            rows = [tool.run_report(run, 0, 30) for run in runs]
            self.assertEqual(rows[0]["milestones"]["power"], 10.0)
            self.assertEqual(rows[0]["furthest"]["step"], "mining_outpost_iron")
            self.assertEqual(rows[1]["furthest"]["step"], "starter_research_electric_mining_drill")
            self.assertEqual(rows[2]["game_minutes"], 8.0)
            agg = fleet.aggregate(rows)
            self.assertEqual(agg["columns"]["power"], dict(min=10.0, median=12.0, max=14.0, n=3))
            self.assertEqual(agg["columns"]["auto"]["n"], 2)
            self.assertIsNone(agg["columns"]["red"])
            reasons = agg["blocked_reasons"]
            self.assertEqual(len(reasons), 1)
            only = next(iter(reasons.values()))
            self.assertEqual((only["count"], only["seeds"]), (2, [11, 22]))
            self.assertEqual(next(iter(agg["controller_errors"].values()))["count"], 2)
            info = dict(name="f", target_minutes=30)
            lines = fleet.format_report(info, rows, agg, window_text="0-30")
            self.assertLess(len(lines), 60)
            self.assertTrue(lines[2].startswith("11 "))
            self.assertIn("mining_outpost_iron@20.0", lines[2])
            self.assertTrue(any(line.startswith("median") for line in lines))
            self.assertTrue(any("e.g. 11" in line for line in lines))

    def test_the_report_header_names_the_commit_the_fleet_ran(self):
        rows = [dict(seed=1, alive=True, game_minutes=30.0, milestones={"power": 10.0}, furthest=None)]
        agg = fleet.aggregate(rows)
        header = fleet.format_report(dict(name="f", commit="6f5e8e5"), rows, agg, window_text="0-30")[0]
        self.assertTrue(header.startswith("fleet f commit 6f5e8e5: 1 runs"), header)
        old = fleet.format_report(dict(name="f"), rows, agg, window_text="0-30")[0]
        self.assertTrue(old.startswith("fleet f commit ?:"), old)  # manifests from before the field
        commit = fleet.git_commit(fleet.REPO)
        self.assertRegex(commit or "", r"^[0-9a-f]{7,}(\+dirty)?$")
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(fleet.git_commit(directory))

    def test_exploration_is_switched_per_fleet_and_shown(self):
        from factorio_agent import layout_selection as selection

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fleet.layout_settings(root, True)
            self.assertTrue(selection.settings(root, env={}).explore)
            fleet.layout_settings(root, False)  # an explicit off wins over an inherited environment switch
            self.assertFalse(selection.settings(root, env={selection.ENV_EXPLORE: "1"}).explore)
        self.assertTrue(fleet.explore_requested(True, env={}))
        self.assertFalse(fleet.explore_requested(False, env={}))
        self.assertTrue(fleet.explore_requested(False, env={selection.ENV_EXPLORE: "1"}))
        rows = [dict(seed=1, alive=True, game_minutes=30.0, milestones={}, furthest=None)]
        header = fleet.format_report(dict(name="f", explore=True), rows, fleet.aggregate(rows), window_text="0-30")[0]
        self.assertIn("layout EXPLORATION on", header)
        status = fleet._status_lines(dict(name="f", explore=True, runs=[]))[0]
        self.assertIn("EXPLORATION", status)

    def test_the_report_prints_the_median_rcon_query_load(self):
        import report_run

        kernel = {
            "commands": {"run": {"count": 36, "seconds": 900.0}, "observe": {"count": 10, "seconds": 60.0}},
            "queries": {"count": 27715, "seconds": 433.0},
        }
        load = report_run.query_load(kernel, 866)
        self.assertEqual(load["per_slice"], 769.9)
        self.assertEqual(load["share_percent"], 45.1)
        self.assertEqual(load["queries_per_minute"], 1732.2)
        self.assertEqual(load["ms_per_action"], 500)
        self.assertIsNone(report_run.query_load(None, 3))
        rows = [
            dict(seed=s, alive=True, game_minutes=30.0, milestones={}, furthest=None, query_load=load) for s in (1, 2)
        ]
        lines = fleet.format_report(dict(name="f"), rows, fleet.aggregate(rows), window_text="0-30")
        self.assertIn("rcon query median: 1732.2/min, 45.1% of command time, 769.9/slice, 500.0 ms/action", lines)
        quiet = [dict(seed=1, alive=True, game_minutes=30.0, milestones={}, furthest=None)]
        lines = fleet.format_report(dict(name="f"), quiet, fleet.aggregate(quiet), window_text="0-30")
        self.assertFalse(any(line.startswith("rcon query") for line in lines))

    def test_sixteen_runs_fit_in_sixty_lines(self):
        rows = [
            dict(seed=i, alive=True, game_minutes=30.0, milestones={"power": 10.0 + i}, furthest=None)
            for i in range(16)
        ]
        agg = fleet.aggregate(rows)
        for i in range(16):
            rows[i]["blocked_reasons"] = [(f"reason {chr(97 + i)}", "power")]
        agg = fleet.aggregate(rows)
        lines = fleet.format_report(dict(name="f"), rows, agg, window_text="0-30")
        self.assertLessEqual(len(lines), 40)

    def test_quick_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_run(root, 1, ends={"starter": 2, "power": 9}, blocked=[("x", "y")], tick=10 * MIN)
            value = fleet.quick_summary(root)
            self.assertEqual(
                {k: value[k] for k in ("game_minutes", "last_milestone", "blocked", "succeeded")},
                dict(game_minutes=10.0, last_milestone="power", blocked=1, succeeded=2),
            )


class PaceTests(unittest.TestCase):
    def test_pace_line_names_the_gap_to_the_target_and_the_share_under_it(self):
        rows = [
            dict(seed=1, milestones={"smelting_district": 19.5, "main_bus": 21.0}),
            dict(seed=2, milestones={"smelting_district": 24.0, "main_bus": 27.0}),
            dict(seed=3, milestones={"smelting_district": 28.0}),
        ]
        line = fleet.pace_line(rows)
        self.assertEqual(
            line, "pace vs target 20 min: smelt median 24.0 (+4.0), 1/3 under; bus median 24.0 (+4.0), 0/3 under"
        )
        self.assertIn("bus median - (never), 0/1 under", fleet.pace_line([dict(seed=4, milestones={})]))
        lines = fleet.format_report(dict(name="f"), rows, fleet.aggregate(rows), window_text="0-30")
        self.assertIn(line, lines)


if __name__ == "__main__":
    unittest.main()
