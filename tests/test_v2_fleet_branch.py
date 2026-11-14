"""tools/fleet.py ``start --from``: branch runs that continue a stopped run's save with the current code."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("fleet_tool_branch", TOOLS / "fleet.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)

MIN = 3600
WORLD = "w-1"
SILENT = lambda *_: None  # noqa: E731


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def stopped_run(root, *, seed=7, tick=300 * MIN, client="none", jobs=None):
    """A safe-stopped client-free run folder: state, pause marker, then the save written after it."""
    write(root / "run.json", dict(schema_version=2, root=str(root), seed=seed, server_port=34400, rcon_port=27400))
    write(root / "world-settings.json", dict(world_id=WORLD))
    write(root / "actor-client.json", dict(client=client, test_mode="client_free"))
    write(root / "jobs.json", dict(jobs=jobs or {}))
    write(root / "work_queue.json", dict(nodes=[]))
    write(root / "site-map.json", dict(sites=[1]))
    write(root / "server.json", dict(pid=1, up=False))
    write(root / "supervisor-process.json", dict(pid=2, up=False))
    write(root / "controller-config.json", dict(runtime=str(root)))
    write(root / "logs" / "controller-state.json", dict(status="safe_stopped", last_observed_tick=tick))
    (root / "writer.lock").write_text("x", encoding="utf-8")
    write(root / "postmortem" / "triage.json", dict(cls="blocked"))
    write(root / "vanilla" / "server-data" / "temp" / "x.json", {})
    write(root / "vanilla" / "server-settings.json", dict(name="n"))
    write(
        root / "lifecycle-pause.json",
        dict(kind="lifecycle-operator-pause-v1", phase="paused", world_id=WORLD, paused_tick=tick),
    )
    save = root / "vanilla" / "saves" / "no-mod-rcon.zip"
    save.parent.mkdir(parents=True, exist_ok=True)
    save.write_bytes(b"zip")
    marker = (root / "lifecycle-pause.json").stat().st_mtime_ns
    os.utime(save, ns=(marker + 10**9, marker + 10**9))
    return root


def digest(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class FakeLifecycle:
    def __init__(self, log):
        self.log = log

    def _start(self, root, *, visible, automation_config, select):
        self.log.append(("start", Path(root).name, visible, select))
        write(Path(root) / "supervisor-process.json", dict(pid=99, creation_time=5, up=True))
        return dict(status="running", supervisor_pid=99)

    def _shutdown(self, root, stopped=None):
        self.log.append(("shutdown", Path(root).name))
        return dict(status="shutdown")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sources_parse_fleet_and_seed(self):
        self.assertEqual(fleet.parse_sources("long-1114.a:12, b:3"), [("long-1114.a", "12"), ("b", "3")])
        for bad in ("", "noseed", ":5", "a:"):
            with self.assertRaises(ValueError):
                fleet.parse_sources(bad)

    def test_facts_name_the_checkpoint_tick_unknown_outcomes_and_blocked_jobs(self):
        jobs = {
            "a": dict(status="blocked", capability="steel", reason="action_outcome_unknown_reconcile_required"),
            "b": dict(status="running", capability="oil", pending_action={"type": "build"}),
            "c": dict(status="succeeded", capability="bus"),
        }
        facts = fleet.checkpoint_facts(stopped_run(self.base / "src", jobs=jobs))
        self.assertEqual(facts["tick"], 300 * MIN)
        self.assertEqual(facts["world_id"], WORLD)
        self.assertEqual(facts["pending_actions"], ["a", "b"])
        self.assertEqual(len(facts["blocked"]), 1)
        self.assertTrue(facts["blocked"][0].startswith("steel: "))

    def test_a_run_without_a_safe_stop_save_is_refused(self):
        root = stopped_run(self.base / "src")
        (root / "lifecycle-pause.json").unlink()
        with self.assertRaisesRegex(ValueError, "pause marker"):
            fleet.checkpoint_facts(root)
        root = stopped_run(self.base / "older")
        save = root / "vanilla" / "saves" / "no-mod-rcon.zip"
        os.utime(save, ns=(0, 0))  # the save predates the pause: state may be newer (tick rollback)
        with self.assertRaisesRegex(ValueError, "after the safe-stop pause"):
            fleet.checkpoint_facts(root)
        root = stopped_run(self.base / "other")
        marker = json.loads((root / "lifecycle-pause.json").read_text(encoding="utf-8"))
        write(root / "lifecycle-pause.json", dict(marker, world_id="w-2"))
        with self.assertRaisesRegex(ValueError, "another world"):
            fleet.checkpoint_facts(root)
        with self.assertRaisesRegex(ValueError, "client-free"):
            fleet.checkpoint_facts(stopped_run(self.base / "gui", client="always"))
        root = stopped_run(self.base / "dead")
        write(root / "logs" / "controller-state.json", dict(state="stopped", stop_reason="actor_dead"))
        with self.assertRaisesRegex(ValueError, "actor death"):
            fleet.checkpoint_facts(root)

    def test_copy_keeps_save_state_and_world_but_not_the_source_processes(self):
        source = stopped_run(self.base / "src")
        before = digest(source)
        target = self.base / "v2" / "b-7"
        fleet.copy_checkpoint(source, target, server_port=34500, rcon_port=27500, branched_from=dict(fleet="f"))
        self.assertEqual(digest(source), before)  # the source is only read
        for kept in (
            "jobs.json",
            "work_queue.json",
            "site-map.json",
            "world-settings.json",
            "lifecycle-pause.json",
            "actor-client.json",
            "logs/controller-state.json",
            "vanilla/saves/no-mod-rcon.zip",
            "vanilla/server-settings.json",
        ):
            self.assertEqual((target / kept).read_bytes(), (source / kept).read_bytes(), kept)
        for dropped in (
            "server.json",
            "supervisor-process.json",
            "controller-config.json",
            "writer.lock",
            "postmortem",
            "vanilla/server-data",
        ):
            self.assertFalse((target / dropped).exists(), dropped)
        run = json.loads((target / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["root"], run["server_port"], run["rcon_port"]), (str(target.resolve()), 34500, 27500))
        self.assertEqual((run["seed"], run["branched_from"]), (7, dict(fleet="f")))
        self.assertTrue(run["rcon_password"])
        with self.assertRaises(FileExistsError):
            fleet.copy_checkpoint(source, target, server_port=1, rcon_port=2, branched_from={})


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tool = fleet.Fleet(base=self.base)
        self.tool.dir.mkdir(parents=True)
        self.log = []
        self.tool.lifecycle = lambda: FakeLifecycle(self.log)
        self.tool.options = lambda: None
        self.tool.spawn_watcher = lambda name: self.log.append(("watch", name))
        self.saved = fleet.alive, fleet.run_alive, fleet.machine, fleet.port_free, fleet.git_commit
        fleet.alive = lambda record: bool(isinstance(record, dict) and record.get("up"))
        fleet.run_alive = lambda root, records: True
        fleet.machine = lambda: dict(free_mb=10**6, total_mb=10**6, cores=64)
        fleet.port_free = lambda udp, tcp: True
        fleet.git_commit = lambda code: "newcode"
        self.source = stopped_run(self.base / "v2" / "src-7")
        source_fleet = dict(seed=7, runtime=str(self.source), status="stopped", server_port=34400, rcon_port=27400)
        write(
            self.tool.manifest_path("src"),
            dict(kind="factorio-fleet-v1", name="src", commit="old", runs=[source_fleet]),
        )

    def tearDown(self):
        fleet.alive, fleet.run_alive, fleet.machine, fleet.port_free, fleet.git_commit = self.saved
        self.tmp.cleanup()

    def test_branch_copies_starts_and_labels_the_run(self):
        before = digest(self.source)
        value = self.tool.branch("src:7", 30, "br", stagger=0, out=SILENT)
        run = value["runs"][0]
        self.assertEqual(run["status"], "running")
        self.assertEqual(Path(run["runtime"]).name, "br-7")
        self.assertNotEqual((run["server_port"], run["rcon_port"]), (34400, 27400))
        self.assertEqual((run["checkpoint_tick"], run["target_tick"]), (300 * MIN, 330 * MIN))
        origin = run["branched_from"]
        self.assertEqual(
            (origin["fleet"], origin["seed"], origin["minute"], origin["commit"]), ("src", 7, 300.0, "old")
        )
        self.assertTrue(value["branch"])
        self.assertEqual(value["branched_from"], [origin])
        self.assertEqual(value["commit"], "newcode")
        saved = json.loads((Path(run["runtime"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["branched_from"]["fleet"], "src")
        self.assertEqual(saved["branched_from"]["branch_fleet"], "br")
        self.assertIn(("start", "br-7", False, False), self.log)
        self.assertIn(("watch", "br"), self.log)
        self.assertEqual(digest(self.source), before)
        # The status header and the report header say it is a branch (TEST runs, absolute game time).
        status = self.tool.status("br", enforce=False, out=SILENT)
        self.assertIn("BRANCHED from checkpoints src:7@300.0", fleet._status_lines(status)[0])
        self.assertIn("target +30", fleet._status_lines(status)[0])
        header = fleet.format_report(self.tool.load("br"), [], fleet.aggregate([]), window_text="0-30")[0]
        self.assertIn("BRANCHED from checkpoints src:7@300.0", header)

    def test_a_live_or_unknown_source_is_refused_before_anything_is_written(self):
        write(self.source / "server.json", dict(pid=1, up=True))
        with self.assertRaisesRegex(RuntimeError, "not stopped"):
            self.tool.branch("src:7", 30, "br", stagger=0, out=SILENT)
        with self.assertRaises(LookupError):
            self.tool.branch("src:8", 30, "br", stagger=0, out=SILENT)
        with self.assertRaisesRegex(ValueError, "--minutes"):
            self.tool.branch("src:7", None, "br", stagger=0, out=SILENT)
        self.assertFalse(self.tool.manifest_path("br").exists())
        self.assertFalse((self.base / "v2" / "br-7").exists())

    def test_the_watcher_stops_a_branch_minutes_after_its_checkpoint(self):
        self.tool.branch("src:7", 30, "br", stagger=0, out=SILENT)
        root = Path(self.tool.load("br")["runs"][0]["runtime"])
        write(root / "logs" / "controller-state.json", dict(last_observed_tick=320 * MIN))
        self.assertEqual(self.tool.enforce("br", out=SILENT), [])  # 300 + 20 < 300 + 30
        write(root / "logs" / "controller-state.json", dict(last_observed_tick=330 * MIN))
        self.assertEqual([r["status"] for r in self.tool.enforce("br", out=SILENT)], ["stopped"])
        self.assertEqual(self.tool.load("br")["runs"][0]["stop_reason"], "reached +30 game min")

    def test_stall_pace_counts_game_time_from_the_checkpoint(self):
        run = dict(seed=7, status="running", started_at=0.0, checkpoint_tick=300 * MIN)
        limit = fleet.stall_limit_minutes(dict(runs=[run]), {7: 310 * MIN}, now=60 * 60.0)
        self.assertEqual(limit, fleet.STALL_PACE_FACTOR * 6.0)  # 60 real min over 10 game min


if __name__ == "__main__":
    unittest.main()
