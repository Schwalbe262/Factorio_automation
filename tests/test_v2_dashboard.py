"""tools/dashboard.py: payload and layout SVG from a small runtime folder; missing/corrupt files never raise."""

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from factorio_agent import progression
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.masterplan import plan_site

TOOLS = Path(__file__).resolve().parents[1] / "tools"
NOW = 1_000_000.0
WORLD = "world-a"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load("dashboard")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def jsonl(path, rows):
    write(path, "".join(json.dumps(r) + "\n" for r in rows))


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        world = make_world(7, size=384)
        plan = plan_site(world)
        plan.allocate(world, "science", 32, 16, name="automation_science#0")
        cls.site_map = {"world_id": WORLD, "map": json.loads(world.to_json())}
        cls.site_plan = {"world_id": WORLD, "start": [0, 0], "half_size": 192, "plan": plan.to_dict()}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "eval-42"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, *, site=True):
        r = self.root
        starter, power = progression.ROADMAP[0], progression.ROADMAP[1]
        write(r / "run.json", {"seed": 42, "schema_version": 2})
        write(r / "world-settings.json", {"world_id": WORLD})
        jobs = {
            "job-starter": dict(
                capability=starter.capability,
                parameters=starter.parameters,
                status="succeeded",
                reason="starter built",
                created_at=NOW - 3000,
                actions=12,
            ),
            "job-power": dict(
                capability=power.capability,
                parameters=power.parameters,
                status="blocked",
                reason="no_water_site_found",
                evidence={"missing_items": {"offshore-pump": 1}, "cells": list(range(40))},
                created_at=NOW - 1200,
                progress_at_time=NOW - 600,
                actions=3,
                priority=-101,
            ),
        }
        write(r / "jobs.json", {"world_id": WORLD, "jobs": jobs})
        write(
            r / "work_queue.json",
            {
                "world_id": WORLD,
                "nodes": [
                    {"id": "auto_starter", "capability": "starter", "status": "completed", "priority": -100},
                    {"id": "auto_power", "capability": "power", "status": "blocked", "priority": -101},
                ],
            },
        )
        events = [
            {"key": "job-starter", "event": "submitted", "time": NOW - 3000, "tick": 60},
            {
                "key": "job-starter",
                "event": "action_intent",
                "time": NOW - 2900,
                "tick": 120,
                "action": {"type": "build", "name": "stone-furnace", "reason": "seed"},
            },
            {"key": "job-starter", "event": "action_result", "time": NOW - 2899, "result": {"status": "succeeded"}},
            {"key": "job-starter", "event": "terminal", "time": NOW - 2800, "status": "succeeded", "reason": "done"},
            {
                "key": "job-power",
                "event": "action_intent",
                "time": NOW - 700,
                "tick": 9000,
                "action": {"type": "take", "item": "coal", "count": 10},
            },
        ]
        jsonl(r / "job-events.jsonl", events)
        controller = [
            {"time": NOW - 3000, "event": "supervisor_started", "planner": "none"},
            {"time": NOW - 2990, "event": "autopilot", "task_ids": ["auto_starter", "auto_power"]},
            {"time": NOW - 30, "event": "slice", "reason": "waiting"},
        ]
        jsonl(r / "logs" / "controller-events.jsonl", controller)
        write(
            r / "logs" / "controller-state.json",
            {
                "status": "running",
                "planner": "none",
                "pid": 4321,
                "native_creation_time": 99,
                "updated_at": NOW - 20,
                "active_job_keys": ["job-power"],
                "open_incidents": [{"id": "i1", "kind": "blocked_job", "subject": "job-power", "since": NOW - 300}],
            },
        )
        if site:
            write(r / "site-map.json", self.site_map)
            write(r / "site-plan.json", self.site_plan)
            board = {
                "world_id": WORLD,
                "blocks": {
                    "startup_science": {
                        "reserved_area": [[-32.0, 32.0], [-16.0, 48.0]],
                        "template": "startup_science_cell",
                    },
                    "broken": {"reserved_area": "nonsense"},
                },
            }
            write(r / "pcb-board.json", board)

    def dash(self, alive=lambda pid, created: None):
        return dashboard.Dashboard(self.root, clock=lambda: NOW, alive=alive)


class PayloadTests(Fixture):
    def test_payload_sections_from_fixture(self):
        self.build()
        data = self.dash(alive=lambda pid, created: pid == 4321 and created == 99).payload()
        json.dumps(data)  # JSON-able
        header = data["header"]
        self.assertEqual((header["runtime"], header["seed"], header["planner_mode"]), ("eval-42", 42, "none"))
        self.assertTrue(header["supervisor"]["alive"])
        self.assertEqual(header["minutes_since_last_success"], round(2800 / 60, 1))
        [job] = data["now"]["jobs"]
        self.assertEqual((job["capability"], job["status"], job["reason"]), ("power", "blocked", "no_water_site_found"))
        self.assertIn("offshore-pump", job["evidence"])
        self.assertIn("[40 items]", job["evidence"])
        self.assertTrue(job["selected"])
        self.assertEqual([q["id"] for q in data["now"]["queue"]], ["auto_power"])
        self.assertEqual(data["now"]["autopilot"][0]["tasks"], ["auto_starter", "auto_power"])
        roadmap = data["done"]["roadmap"]
        self.assertEqual(len(roadmap), len(progression.ROADMAP))
        self.assertEqual([s["state"] for s in roadmap[:2]], ["done", "blocked"])
        self.assertEqual(data["done"]["roadmap_done"], 1)
        self.assertEqual([m["capability"] for m in data["done"]["milestones"]], ["starter"])
        recent = data["activity"]["recent"]
        self.assertEqual(
            [(r["job"], r["type"], r["result"]) for r in recent],
            [("power", "take", None), ("starter", "build", "succeeded")],
        )
        self.assertEqual(data["activity"]["incidents"][0]["subject"], "power")
        layout = data["layout"]
        self.assertTrue(layout["available"])
        self.assertEqual([b["name"] for b in layout["blocks"]], ["broken", "startup_science"])
        self.assertEqual(layout["allocations"][0]["name"], "automation_science#0")
        self.assertEqual(len(layout["focus"]), 4)

    def test_svg_has_plan_and_board_blocks(self):
        self.build()
        svg = self.dash().layout_svg()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('data-kind="board" data-name="startup_science"', svg)
        self.assertNotIn('data-name="broken"', svg)
        self.assertIn('data-kind="allocation" data-name="automation_science#0"', svg)
        self.assertIn('data-kind="lane"', svg)
        self.assertIn("plan-arrow", svg)  # planrender corridor arrows
        self.assertIn(">bus<", svg)  # zone label from render_svg

    def test_supervisor_falls_back_to_event_freshness(self):
        self.build()
        self.assertTrue(self.dash().payload()["header"]["supervisor"]["alive"])
        self.assertFalse(self.dash(alive=lambda pid, created: False).payload()["header"]["supervisor"]["alive"])


class RobustnessTests(Fixture):
    def test_empty_folder_does_not_raise(self):
        dash = self.dash()
        data = dash.payload()
        self.assertFalse(data["layout"]["available"])
        self.assertIn("site-plan.json", data["layout"]["message"])
        self.assertEqual(data["now"]["jobs"], [])
        self.assertEqual(data["done"]["roadmap_done"], 0)
        self.assertIsNone(dash.layout_svg())

    def test_placeholder_before_the_plan_exists(self):
        self.build(site=False)
        data = self.dash().payload()
        self.assertFalse(data["layout"]["available"])

    def test_corrupt_files_keep_last_good_value(self):
        self.build()
        dash = self.dash()
        first = dash.payload()
        write(self.root / "jobs.json", '{"jobs": {"half')
        write(self.root / "site-plan.json", "{")
        write(self.root / "logs" / "controller-state.json", "[1, 2")
        again = dash.payload()
        self.assertEqual(again["now"]["jobs"], first["now"]["jobs"])
        self.assertTrue(again["layout"]["available"])
        self.assertEqual(again["files"].get("jobs"), "unreadable")
        self.assertIsNotNone(dash.layout_svg())

    def test_corrupt_from_the_start_does_not_raise(self):
        for name in ("run.json", "jobs.json", "work_queue.json", "site-plan.json", "site-map.json", "pcb-board.json"):
            write(self.root / name, "{not json")
        write(self.root / "logs" / "controller-state.json", '"a string"')
        write(self.root / "job-events.jsonl", 'garbage\n{"event": "terminal"}\n[1]\n')
        data = self.dash().payload()
        self.assertFalse(data["layout"]["available"])
        self.assertEqual(data["activity"]["recent"], [])

    def test_wrong_shapes_do_not_raise(self):
        self.build()
        write(self.root / "site-plan.json", {"world_id": WORLD, "plan": {"format": "other"}})
        write(self.root / "pcb-board.json", {"world_id": "other-world", "blocks": {"x": {}}})
        data = self.dash().payload()
        self.assertFalse(data["layout"]["available"])
        self.assertIn("error", data["layout"])


class TailTests(Fixture):
    def test_reads_only_appended_complete_lines(self):
        path = self.root / "log.jsonl"
        tail = dashboard.JsonlTail(path)
        self.assertEqual(tail.update(), [])
        write(path, json.dumps({"time": 1, "n": 1}) + "\n" + '{"time": 2, "n"')
        self.assertEqual([r["n"] for r in tail.update()], [1])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(': 2}\n{"time": 3, "n": 3}')
        self.assertEqual([r["n"] for r in tail.update()], [1, 2, 3])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertEqual(len(tail.update()), 3)
        write(path, json.dumps({"time": 9, "n": 9}) + "\n")  # replaced by a shorter file
        self.assertEqual([r["n"] for r in tail.update()], [9])


class ServerTests(Fixture):
    def test_endpoints(self):
        self.build()
        server = dashboard.serve(self.root, port=0, base=Path(self.tmp.name))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            self.assertEqual(server.server_address[0], "127.0.0.1")
            page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
            self.assertIn("레이아웃", page)
            state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=10).read())
            self.assertTrue(state["layout"]["available"])
            svg = urllib.request.urlopen(base + "/api/layout.svg", timeout=10).read().decode("utf-8")
            self.assertIn("startup_science", svg)
        finally:
            server.shutdown()
            server.server_close()


STEP = {s.id: s for s in progression.ROADMAP}


def job(step_id, status, last_tick=None, reason="ok"):
    step = STEP[step_id]
    return dict(
        capability=step.capability, parameters=step.parameters, status=status, reason=reason, last_tick=last_tick
    )


class FleetFixture(unittest.TestCase):
    """A runtime base with fleets/ manifests and v2/<name>-<seed> run folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "runtime"
        (self.base / "fleets").mkdir(parents=True)
        self.live = set()  # pids that count as alive

    def tearDown(self):
        self.tmp.cleanup()

    def alive(self, record):
        return record.get("pid") in self.live

    def manifest(self, name, created, seeds, statuses=None, **extra):
        runs = [
            dict(seed=s, runtime=str(self.base / "v2" / f"{name}-{s}"), status=(statuses or {}).get(s, "stopped"))
            for s in seeds
        ]
        write(
            self.base / "fleets" / (name + ".json"),
            dict(kind="factorio-fleet-v1", name=name, created_at=created, target_minutes=45, planner="none", runs=runs)
            | extra,
        )

    def make_run(self, name, seed, *, power, auto=None, blocked=None, green=False, pid=None, tick=60000):
        root = self.base / "v2" / f"{name}-{seed}"
        jobs = {"j-power": job("power", "succeeded", power * 3600)}
        events = [
            {"key": "j-power", "event": "action_result", "time": 10.0, "tick": power * 3600},
            # no tick: the last earlier row's tick
            {"key": "j-power", "event": "terminal", "time": 11.0, "status": "succeeded"},
        ]
        if auto is not None:
            jobs["j-auto"] = job("starter_research_automation", "succeeded")
            # no row tick: the evidence tick wins
            events.append(
                {
                    "key": "j-auto",
                    "event": "terminal",
                    "time": 12.0,
                    "status": "succeeded",
                    "evidence": {"tick": auto * 3600},
                }
            )
        if blocked:
            jobs["j-bus"] = job("main_bus", "blocked", 50000, reason=blocked)
            events.append({"key": "j-bus", "event": "terminal", "time": 13.0, "status": "blocked", "tick": 50000})
        if green:
            jobs["j-green"] = job("logistic_science", "running")
        write(root / "jobs.json", {"jobs": jobs})
        jsonl(root / "job-events.jsonl", events)
        write(root / "logs" / "controller-state.json", {"status": "running", "last_observed_tick": tick})
        if pid:
            write(root / "server.json", {"pid": pid, "creation_time": 5})
            write(root / "supervisor-process.json", {"pid": pid + 1, "creation_time": 5})
            self.live.update({pid, pid + 1})
        return root

    def build(self):
        self.manifest("pace1", 100.0, [1, 2])
        self.make_run("pace1", 1, power=8, auto=11, blocked="no_route at 12,34")
        self.make_run("pace1", 2, power=9)
        self.manifest("auto-1113-1618", 200.0, [3, 4], commit="aaa1111")
        self.make_run("auto-1113-1618", 3, power=10, auto=14, blocked="no_route at 5,6")
        self.make_run("auto-1113-1618", 4, power=12, auto=16)
        self.manifest("auto-1113-1708", 300.0, [5, 6, 7], statuses={5: "running", 7: "planned"})
        write(self.base / "fleets" / "reports" / "auto-1113-1708.report.txt", "fleet auto-1113-1708 commit bbb2222\n")
        self.make_run("auto-1113-1708", 5, power=6, auto=9, pid=500, green=True)
        self.make_run("auto-1113-1708", 6, power=7, blocked="planner_fault 3")
        # seed 7: no folder yet
        write(self.base / "fleets" / "cost.json", {"per_run_mb": 1})

    def board(self):
        return dashboard.FleetBoard(self.base, clock=lambda: NOW, alive=self.alive)


class FleetPayloadTests(FleetFixture):
    def test_fleets_newest_first_with_strip_stats_and_health(self):
        self.build()
        data = self.board().payload()
        json.dumps(data)
        self.assertEqual([f["name"] for f in data["fleets"]], ["auto-1113-1708", "auto-1113-1618", "pace1"])
        self.assertEqual(data["default_open"], "auto-1113-1708")  # the newest fleet with a live run
        newest = data["fleets"][0]
        self.assertEqual(
            (newest["commit"], newest["runs_total"], newest["running"], newest["stopped"]), ("bbb2222", 3, 1, 2)
        )
        self.assertEqual(newest["columns"][-1]["head"], "green")  # a logistic_science job exists
        self.assertNotIn("green", [c["head"] for c in data["fleets"][1]["columns"]])
        run5, run6, run7 = newest["runs"]
        self.assertTrue(run5["alive"] and run5["server"] and run5["supervisor"] and run5["link"])
        self.assertEqual(run5["runtime"], "auto-1113-1708-5")
        self.assertEqual(run5["milestones"], {"power": 6.0, "starter_research_automation": 9.0})
        self.assertEqual(run5["furthest"]["step"], "starter_research_automation")
        self.assertEqual((run5["game_minutes"], run5["active"], run5["blocked"]), (round(60000 / 3600, 1), 1, 0))
        self.assertFalse(run6["alive"])
        self.assertEqual(run6["blocked_now"], {"capability": "planned_main_bus", "reason": "planner_fault 3"})
        self.assertFalse(run7["link"])
        self.assertEqual(run7["milestones"], {})
        self.assertEqual(newest["stats"]["power"], {"min": 6.0, "median": 6.5, "max": 7.0, "n": 2})
        self.assertEqual(newest["stats"]["auto"]["n"], 1)
        self.assertEqual(newest["runs_with_data"], 2)
        self.assertIn("실행 중 1/3", newest["health"])
        self.assertIn("막힌 런 1", newest["health"])
        self.assertEqual(newest["health_level"], "warn")

    def test_blocked_histograms_per_fleet_and_recent(self):
        self.build()
        data = self.board().payload()
        pace = data["fleets"][2]
        [row] = pace["blocked"]
        self.assertEqual((row["reason"], row["count"], row["example"]), ("no_route at #,#", 1, 1))
        self.assertEqual(row["example_runtime"], "pace1-1")
        top = data["recent_blocked"][0]
        self.assertEqual((top["reason"], top["count"], top["runs"]), ("no_route at #,#", 2, 2))
        self.assertIn(top["example_runtime"], ("pace1-1", "auto-1113-1618-3"))

    def test_trend_by_round_for_a_name_series(self):
        self.build()
        [trend] = self.board().payload()["trend"]
        self.assertEqual(trend["prefix"], "auto")
        rounds = trend["rounds"]
        self.assertEqual([r["name"] for r in rounds], ["auto-1113-1618", "auto-1113-1708"])
        self.assertEqual([r["commit"] for r in rounds], ["aaa1111", "bbb2222"])
        self.assertEqual([r["median"]["power"] for r in rounds], [11.0, 6.5])
        self.assertEqual([r["reached"]["auto"] for r in rounds], [2, 1])
        self.assertEqual([r["finished"] for r in rounds], [False, True])

    def test_job_events_are_read_incrementally(self):
        self.build()
        board = self.board()
        board.payload()
        root = self.base / "v2" / "pace1-2"
        with (root / "job-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": "j-auto", "event": "terminal", "time": 20.0, "status": "succeeded"}))
        write(root / "jobs.json", {"jobs": {"j-auto": job("starter_research_automation", "succeeded")}})
        card = board.cards[str(root.resolve())]
        offset = card.ends.offset
        run = board.payload()["fleets"][2]["runs"][1]
        self.assertGreater(card.ends.offset, offset)
        self.assertEqual(run["milestones"].get("starter_research_automation"), 9.0)  # last earlier tick

    def test_missing_and_corrupt_files_do_not_raise(self):
        self.build()
        write(self.base / "fleets" / "broken.json", '{"kind": "factorio-fleet-v1", "na')
        write(self.base / "fleets" / "other.json", {"kind": "something-else", "name": "other"})
        board = self.board()
        before = board.payload()
        root = self.base / "v2" / "pace1-1"
        write(root / "jobs.json", '{"jobs": {"half')
        write(root / "logs" / "controller-state.json", "[1,")
        with (root / "job-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('garbage\n{"event": "terminal", "time": 1, "key": "j-power"')
        write(root / "server.json", "{")
        after = board.payload()
        self.assertEqual([f["name"] for f in after["fleets"]], [f["name"] for f in before["fleets"]])
        self.assertEqual(after["fleets"][2]["runs"][0]["milestones"], before["fleets"][2]["runs"][0]["milestones"])
        self.assertEqual(dashboard.FleetBoard(Path(self.tmp.name) / "nothing").payload()["fleets"], [])


class RuntimeParameterTests(FleetFixture):
    def test_strict_runtime_folder_names(self):
        self.build()
        ok = dashboard.runtime_folder(self.base, "pace1-1")
        self.assertEqual(ok, (self.base / "v2" / "pace1-1").resolve())
        for bad in (
            "",
            "..",
            "../fleets",
            "a/b",
            "a\\b",
            ".hidden",
            "pace1-1.",
            "C:x",
            "/abs",
            "a..b",
            "x" * 200,
            None,
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dashboard.runtime_folder(self.base, bad)
        for absent in ("pace1-9", "fleets"):
            with self.assertRaises(LookupError):
                dashboard.runtime_folder(self.base, absent)

    def test_endpoints_with_runtime_parameter(self):
        self.build()
        default = self.base / "v2" / "pace1-1"
        server = dashboard.serve(default, port=0, base=self.base)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path):
            try:
                with urllib.request.urlopen(url + path, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            status, body = get("/api/fleets")
            self.assertEqual((status, json.loads(body)["fleets"][0]["name"]), (200, "auto-1113-1708"))
            status, body = get("/api/state")
            self.assertEqual(json.loads(body)["header"]["runtime"], "pace1-1")
            status, body = get("/api/state?runtime=auto-1113-1708-5")
            self.assertEqual((status, json.loads(body)["header"]["runtime"]), (200, "auto-1113-1708-5"))
            for bad in ("..%2Ffleets", "%2e%2e", "a%5Cb", "pace1-1&runtime=pace1-2", ""):
                self.assertEqual(get("/api/state?runtime=" + bad)[0], 400, bad)
            self.assertEqual(get("/api/state?runtime=pace1-9")[0], 404)
            self.assertEqual(get("/api/layout.svg?runtime=..")[0], 400)
            self.assertEqual(get("/api/layout.svg?runtime=pace1-2&v=1")[0], 404)  # no plan yet
            page = get("/")[1].decode("utf-8")
            self.assertIn("병렬 실행", page)
            self.assertIn("/api/fleets", page)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
