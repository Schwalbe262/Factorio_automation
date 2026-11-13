"""tools/dashboard.py: payload and layout SVG from a small runtime folder; missing/corrupt files never raise."""

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
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
        server = dashboard.serve(self.root, port=0)
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


if __name__ == "__main__":
    unittest.main()
