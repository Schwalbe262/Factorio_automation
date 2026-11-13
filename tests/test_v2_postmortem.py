"""Post-mortem of fleet runs: triage classes, read-only snapshot, keep-problem selection and release.

Fakes only: no server, no fleet. ``factorio_agent.postmortem``, ``tools/fleet_triage.py``,
the post-mortem part of ``tools/fleet.py`` and the cause in ``tools/waste_report.py``.
"""

from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent import evidence_capture, postmortem
from factorio_agent.planning import preflight

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("fleet_tool_pm", TOOLS / "fleet.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)
import fleet_triage as triage  # noqa: E402

MIN = 3600


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def run_folder(root, *, jobs, slices=(), errors=0, tick=40 * MIN):
    write(root / "run.json", {"seed": 1})
    write(root / "jobs.json", {"jobs": jobs})
    rows = [dict(event="error", reason="bridge timeout") for _ in range(errors)]
    rows += [dict(event="slice", **s) for s in slices]
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "controller-events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    write(root / "logs" / "controller-state.json", dict(status="running", last_observed_tick=tick))
    return root


def busy_slices(until, step=MIN):
    return [dict(tick=t, actions=3, jobs={"a": {"status": "running"}}) for t in range(step, until + 1, step)]


class TriageTests(unittest.TestCase):
    def facts(self, directory, **kw):
        return triage.run_facts(run_folder(Path(directory), **kw))

    def test_healthy_run(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = {"a": dict(capability="power", status="succeeded", last_tick=39 * MIN)}
            verdict = triage.classify(self.facts(d, jobs=jobs, slices=busy_slices(40 * MIN)))
            self.assertEqual(verdict["class"], "healthy")
            self.assertEqual(verdict["score"], 0.0)

    def test_blocked_job_names_capability_and_reason(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = {
                "a": dict(capability="power", status="succeeded", last_tick=39 * MIN),
                "b": dict(
                    capability="planned_main_bus",
                    status="blocked",
                    reason="planned_bus_feed_route_failed",
                    blocked_at_tick=30 * MIN,
                    evidence={"route_failures": "iron-plate: no_clean_trunk_corridor"},
                ),
            }
            verdict = triage.classify(self.facts(d, jobs=jobs, slices=busy_slices(40 * MIN)))
            self.assertEqual(verdict["class"], "blocked")
            self.assertEqual(verdict["blocking"]["capability"], "planned_main_bus")
            self.assertIn("planned_bus_feed_route_failed", verdict["reasons"][0])
            self.assertEqual(verdict["jobs"], ["b"])

    def test_a_fresh_block_is_not_yet_a_blocked_run(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = {"b": dict(capability="produce", status="blocked", reason="x", blocked_at_tick=39 * MIN)}
            verdict = triage.classify(self.facts(d, jobs=jobs, slices=busy_slices(40 * MIN)))
            self.assertEqual(verdict["class"], "healthy")

    def test_stalled_without_action_and_with_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as d:
            slices = busy_slices(20 * MIN) + [dict(tick=t * MIN, actions=0, jobs={}) for t in range(21, 41)]
            jobs = {"w": dict(capability="research", status="waiting", reason="waiting", progress_at_tick=10 * MIN)}
            verdict = triage.classify(self.facts(d, jobs=jobs, slices=slices))
            self.assertEqual(verdict["class"], "stalled")
            self.assertIn("no action or success for 20.0 game min", verdict["reasons"][0])
            self.assertIn("empty queue", verdict["reasons"][0])
            self.assertEqual(verdict["jobs"], ["w"])

    def test_errored_by_controller_errors_or_executor_failure(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = {
                "x": dict(
                    capability="produce",
                    status="blocked",
                    reason="repeated_executor_failure:build_failed",
                    blocked_at_tick=30 * MIN,
                )
            }
            verdict = triage.classify(self.facts(d, jobs=jobs, slices=busy_slices(40 * MIN), errors=4))
            self.assertEqual(verdict["class"], "errored")
            self.assertEqual(set(verdict["flags"]), {"errored", "blocked"})
            self.assertIn("4 controller errors", verdict["reasons"])

    def test_slow_against_the_fleet_median(self):
        maps = [{"power": 7.0, "smelting_district": 27.0} for _ in range(4)] + [{"power": 7.5}]
        medians = triage.fleet_medians(maps, ["power", "smelting_district"])
        self.assertEqual(medians["smelting_district"], dict(median=27.0, reached=4, runs=5))
        with tempfile.TemporaryDirectory() as d:
            jobs = {"a": dict(capability="power", status="succeeded", last_tick=39 * MIN)}
            facts = self.facts(d, jobs=jobs, slices=busy_slices(40 * MIN))
            verdict = triage.classify(facts, milestones={"power": 7.5}, medians=medians)
            self.assertEqual(verdict["class"], "slow")
            self.assertEqual(verdict["late"][0]["step"], "smelting_district")
            self.assertIsNone(verdict["late"][0]["minute"])
            early = triage.classify(facts, milestones={"power": 7.0, "smelting_district": 30.0}, medians=medians)
            self.assertEqual(early["class"], "healthy")  # 3 min late is inside the 6.75 min margin

    def test_select_kept_takes_the_worst_and_never_a_healthy_run(self):
        rows = [
            (1, dict(score=1100.0, **{"class": "slow"})),
            (2, dict(score=3150.0, **{"class": "blocked"})),
            (3, dict(score=4050.0, **{"class": "stalled"})),
            (4, dict(score=0.0, **{"class": "healthy"})),
        ]
        self.assertEqual(triage.select_kept(rows, 1), [3])
        self.assertEqual(triage.select_kept(rows, 2), [3, 2])
        self.assertEqual(triage.select_kept(rows, 9), [3, 2, 1])
        self.assertEqual(triage.select_kept(rows, 0), [])
        self.assertGreater(
            triage.keep_score("stalled", ["stalled"], 1, 3), triage.keep_score("blocked", ["blocked"], 30, 3)
        )


# ----- read-only snapshot -------------------------------------------------------------------
@dataclass(frozen=True)
class Cfg:
    runtime_dir: Path
    log_dir: Path


class FakeLive:
    """The run's live game: answers queries, fails the test on any action or save."""

    def __init__(self, root):
        self.cfg = Cfg(root, root / "logs")
        self.expected_world = "w1"
        self.queries = []
        self.writes = []

    def query(self, body):
        self.queries.append(body)
        if "find_entities_filtered{area=area}" in body:
            return dict(
                ok=True,
                entities=[{"name": "stone-furnace", "type": "furnace", "position": {"x": 10.5, "y": 4.5}}],
                resources=[{"name": "iron-ore", "position": {"x": 10.5, "y": 4.5}, "amount": 300}],
                water=[],
                cliffs=[],
            )
        if "tick_paused" in body:
            return dict(ok=True, tick=162000, tick_paused=True, world_id="w1")
        return dict(ok=True, rows=[1, 2, 3])

    def observe(self, radius=384):
        return dict(ok=True, world_id="w1", tick=162000, entities=[], technologies={})

    def act(self, action):
        self.writes.append(action)
        raise AssertionError("a post-mortem must never act")

    def save(self):
        self.writes.append("save")
        raise AssertionError("a post-mortem must never save")


class FakeBus:
    def __init__(self, game):
        self.game = game

    def prepare(self, obs):
        self.game.query("return {ok=true,survey=true}")
        # A planner that writes its own state writes into the sandbox, never into the run.
        (Path(self.game.cfg.runtime_dir) / "planned-main-bus.json").write_text("{}", encoding="utf-8")
        evidence_capture.note(
            "route",
            item="iron-plate",
            source={"x": 2.5, "y": 4.5},
            destination={"x": 20.5, "y": 4.5},
            outcome={"ok": False, "reason": "no_clean_trunk_corridor"},
        )
        evidence_capture.note(
            "preflight",
            outcome={
                "ok": False,
                "reason": "block_protected_entity_obstruction",
                "entity": "stone-furnace",
                "position": {"x": 10.5, "y": 4.5},
            },
        )
        return {"ok": False, "reason": "planned_bus_feed_route_failed", "failures": [{"item": "iron-plate"}]}

    def step(self, obs, parameters):
        return {"type": "build", "name": "transport-belt", "reason": "build the bus"}


class ActingBus(FakeBus):
    def prepare(self, obs):
        return self.game.act({"type": "build", "name": "splitter"})


def jobs_fixture():
    return {
        "bus": dict(
            capability="planned_main_bus", parameters={}, status="blocked", reason="planned_bus_feed_route_failed"
        ),
        "ok": dict(capability="power", parameters={}, status="succeeded", reason=""),
    }


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "run-1"
        run_folder(self.root, jobs=jobs_fixture(), slices=busy_slices(40 * MIN))
        write(self.root / "planned-main-bus.json", {"original": True})
        (self.root / "vanilla").mkdir()
        self.live = FakeLive(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, bus=FakeBus, observe=None):
        def observe_receipt(sandbox, game, catalog, factory):
            (Path(sandbox) / "rate-samples.json").write_text("[]", encoding="utf-8")
            return {"world_id": "w1", "tick": 162000, "resource_supply_options": {"iron-ore": []}}

        return postmortem.snapshot(
            self.root,
            triage={"class": "blocked", "jobs": ["bus"]},
            game=self.live,
            catalog=object(),
            layout=lambda root: "<svg/>",
            executors_factory=lambda game, catalog: SimpleNamespace(planned_bus=bus(game)),
            observe=observe or observe_receipt,
            clock=lambda: 1_800_000_000.0,
        )

    def test_snapshot_writes_only_into_postmortem_and_never_acts(self):
        before = postmortem.fingerprint(self.root, skip=("postmortem",))
        manifest = self.snapshot()
        self.assertEqual(self.live.writes, [])
        self.assertEqual(manifest["run_files_changed"], [])
        self.assertEqual(postmortem.fingerprint(self.root, skip=("postmortem",)), before)
        self.assertEqual(json.loads((self.root / "planned-main-bus.json").read_text()), {"original": True})
        self.assertFalse((self.root / "rate-samples.json").exists())
        folder = Path(manifest["folder"])
        self.assertTrue(folder.is_relative_to(self.root / "postmortem"))
        for name in ("triage.json", "observe-raw.json", "observe.json", "layout.svg", "probes/bus.json"):
            self.assertTrue((folder / name).exists(), name)
        self.assertFalse((folder / "sandbox").exists())
        self.assertTrue(manifest["world_paused"])
        latest = postmortem.latest(self.root)
        self.assertEqual(latest["folder"], str(folder))

    def test_probe_records_dry_runs_notes_areas_and_a_concrete_cause(self):
        manifest = self.snapshot()
        probe = json.loads((Path(manifest["folder"]) / "probes" / "bus.json").read_text())
        self.assertTrue(probe["dry_run_supported"])
        prepare, step = probe["dry_runs"]
        self.assertEqual(prepare["result"]["reason"], "planned_bus_feed_route_failed")
        self.assertEqual(prepare["sandbox_writes"], ["planned-main-bus.json"])
        self.assertEqual([n["stage"] for n in prepare["notes"]], ["route", "preflight"])
        self.assertEqual(step["would_act"]["type"], "build")
        self.assertTrue(probe["areas"] and all(a["receipt"]["ok"] for a in probe["areas"]))
        blocker = next(b for b in probe["blockers"] if b["at"] == [10.5, 4.5])
        self.assertIn("stone-furnace@10.5,4.5", blocker["entities"])
        self.assertIn("block_protected_entity_obstruction", probe["cause"]["summary"])
        self.assertIn("stone-furnace", probe["cause"]["summary"])
        self.assertEqual(manifest["probes"][0]["cause"], probe["cause"]["summary"])
        queries = (Path(manifest["folder"]) / "probes" / prepare["queries_file"]).read_text().splitlines()
        self.assertIn("survey=true", json.loads(queries[0])["script"])

    def test_a_planner_that_needs_an_action_is_recorded_not_executed(self):
        manifest = self.snapshot(bus=ActingBus)
        probe = json.loads((Path(manifest["folder"]) / "probes" / "bus.json").read_text())
        self.assertEqual(self.live.writes, [])
        self.assertIn("read_only_violation", probe["dry_runs"][0]["error"])
        self.assertEqual(probe["refused"][0], {"kind": "act", "action": "build"})

    def test_waste_report_turns_a_blocked_row_into_the_postmortem_cause(self):
        import waste_report

        self.snapshot()
        cause = waste_report.postmortem_cause(self.root, "bus")
        self.assertIn("stone-furnace", cause["cause"])
        self.assertTrue(cause["cause_file"].endswith("bus.json"))
        self.assertEqual(waste_report.postmortem_cause(self.root, "missing"), {})


class ReadOnlyGameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = FakeLive(Path(self.tmp.name))
        self.game = postmortem.ReadOnlyGame(self.live, Path(self.tmp.name) / "sandbox")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_pass_and_writes_are_refused_before_sending(self):
        reads = (
            "local rows={};table.insert(rows,1);return {ok=true,rows=rows}",
            "if d.world_id~=x then return {} end;return {ok=true,tick_paused=game.tick_paused,w=d.world_id}",
            "local r={};r.status_name='x';return {ok=true}",
        )
        for body in reads:
            self.assertTrue(self.game.query(body)["ok"], body)
        writes = (
            "s.create_entity{name='stone-furnace',position={0,0}}",
            "game.tick_paused=false",
            "e.insert{name='coal',count=5}",
            "d.motion=nil",
            "storage.x=1",
            "e.destroy()",
        )
        for body in writes:
            with self.assertRaises(postmortem.ReadOnlyViolation, msg=body):
                self.game.query(body)
        self.assertEqual(len(self.live.queries), len(reads))
        with self.assertRaises(postmortem.ReadOnlyViolation):
            self.game.act({"type": "mine"})
        with self.assertRaises(postmortem.ReadOnlyViolation):
            self.game.save()
        self.assertEqual(self.live.writes, [])
        self.assertEqual(self.game.cfg.runtime_dir, Path(self.tmp.name) / "sandbox")


class EvidenceCaptureTests(unittest.TestCase):
    def test_note_is_a_no_op_outside_capture_and_bounded_inside(self):
        evidence_capture.note("x", value=1)  # no capture: nothing kept, no error
        with evidence_capture.capturing() as rows:
            evidence_capture.note("x", items=list(range(500)), text="a" * 5000, inf=float("inf"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["items"][-1], {"_truncated_items": 500 - evidence_capture.MAX_ITEMS})
        self.assertLess(len(rows[0]["text"]), 1600)
        self.assertEqual(rows[0]["inf"], "inf")
        self.assertFalse(evidence_capture.active())

    def test_preflight_notes_its_failure_only_while_captured(self):
        builder = SimpleNamespace(_occupied_by_plan=lambda es: {(0, 0)})
        entities = [{"name": "stone-furnace", "position": {"x": 0.5, "y": 0.5}}] * 2
        with evidence_capture.capturing() as rows:
            result = preflight(None, builder, {"world_id": "w"}, entities)
        self.assertEqual(result["reason"], "whole_block_prospective_overlap")
        self.assertEqual(rows[0]["stage"], "preflight")
        self.assertEqual(rows[0]["outcome"]["reason"], "whole_block_prospective_overlap")
        self.assertEqual(rows[0]["box"], [[0.5, 0.5], [0.5, 0.5]])


# ----- keep-problem selection and capacity release (tools/fleet.py) ------------------------------
class FakeLifecycle:
    def __init__(self, log):
        self.log = log

    def _stop(self, root):
        self.log.append(("stop", Path(root).name))
        return dict(status="stopped", game_paused=True)

    def _shutdown(self, root, stopped=None):
        self.log.append(("shutdown", Path(root).name, bool(stopped)))
        write(Path(root) / "server.json", {"up": False})
        return dict(status="shutdown")


class KeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tool = fleet.Fleet(base=self.base)
        self.tool.dir.mkdir(parents=True)
        self.log = []
        self.tool.lifecycle = lambda: FakeLifecycle(self.log)
        self.saved = fleet.alive, fleet.run_alive, fleet.machine, fleet.RELEASE_SETTLE_SECONDS
        fleet.alive = lambda record: bool(isinstance(record, dict) and record.get("up"))
        fleet.run_alive = lambda root, records: True
        fleet.RELEASE_SETTLE_SECONDS = 0

    def tearDown(self):
        fleet.alive, fleet.run_alive, fleet.machine, fleet.RELEASE_SETTLE_SECONDS = self.saved
        self.tmp.cleanup()

    def manifest(self, runs, **extra):
        rows = []
        for seed, status, fields in runs:
            root = self.base / "v2" / f"f-{seed}"
            write(root / "run.json", {"seed": seed})
            write(root / "server.json", {"up": True})
            rows.append(dict(seed=seed, runtime=str(root), status=status, server_port=34400 + seed, **fields))
        value = dict(kind="factorio-fleet-v1", name="f", keep_problem=1, keep_timeout_minutes=60, runs=rows, **extra)
        write(self.tool.manifest_path("f"), value)

    def statuses(self):
        return {r["seed"]: r["status"] for r in self.tool.load("f")["runs"]}

    def test_round_end_keeps_the_worst_problem_run_and_releases_the_rest(self):
        verdict = lambda c, s: {"class": c, "score": s}  # noqa: E731
        self.manifest(
            [
                (1, fleet.PAUSED_PROBLEM, dict(triage=verdict("blocked", 3100.0))),
                (2, fleet.PAUSED_PROBLEM, dict(triage=verdict("stalled", 4050.0))),
                (3, fleet.PAUSED_PROBLEM, dict(triage=verdict("slow", 1100.0))),
                (4, "stopped", {}),
            ]
        )
        self.tool.settle_kept("f", out=lambda *_: None)
        self.assertEqual(self.statuses(), {1: "stopped", 2: fleet.KEPT, 3: "stopped", 4: "stopped"})
        self.assertGreater(self.tool.load("f")["runs"][1]["keep_until"], 0)
        # Paused runs are shut down with the stored stop result: no second safe stop.
        self.assertEqual(sorted(self.log), [("shutdown", "f-1", True), ("shutdown", "f-3", True)])

    def test_nothing_is_settled_while_a_run_still_plays(self):
        self.manifest([(1, fleet.PAUSED_PROBLEM, dict(triage={"class": "blocked", "score": 3000})), (2, "running", {})])
        self.assertEqual(self.tool.settle_kept("f", out=lambda *_: None), [])
        self.assertEqual(self.statuses()[1], fleet.PAUSED_PROBLEM)

    def test_a_kept_run_is_released_after_its_timeout(self):
        self.manifest([(1, fleet.KEPT, dict(keep_until=1000.0)), (2, fleet.REVIVED, dict(keep_until=5000.0))])
        self.tool.release_expired("f", now=2000.0, out=lambda *_: None)
        self.assertEqual(self.statuses(), {1: "stopped", 2: fleet.REVIVED})
        self.assertEqual(self.tool.load("f")["runs"][0]["release_reason"], "keep timeout")

    def test_a_new_round_releases_held_runs_oldest_first_until_it_fits(self):
        self.manifest(
            [
                (1, fleet.KEPT, dict(keep_until=9e12, paused_at=200.0)),
                (2, fleet.KEPT, dict(keep_until=9e12, paused_at=100.0)),
            ]
        )
        per_run = fleet.DEFAULT_RUN_MB
        fleet.machine = lambda: dict(
            free_mb=fleet.RESERVE_MB + per_run * (1 + sum(e[0] == "shutdown" for e in self.log)), total_mb=0, cores=64
        )
        fits, _why, _host = self.tool.release_for_capacity(2, self.tool.cost(), out=lambda *_: None)
        self.assertEqual(fits, 2)
        self.assertEqual(self.log, [("shutdown", "f-2", True)])
        self.assertEqual(self.statuses(), {1: fleet.KEPT, 2: "stopped"})

    def test_explicit_stop_of_a_kept_run_skips_the_second_safe_stop(self):
        self.manifest([(1, fleet.KEPT, dict(keep_until=9e12))])
        self.tool.stop("f", out=lambda *_: None)
        self.assertEqual(self.log, [("shutdown", "f-1", True)])

    def test_finish_pauses_triages_snapshots_and_holds_only_problem_runs(self):
        self.manifest([(1, "running", {}), (2, "running", {})])
        good = self.base / "v2" / "f-1"
        bad = self.base / "v2" / "f-2"
        run_folder(
            good,
            jobs={"a": dict(capability="power", status="succeeded", last_tick=39 * MIN)},
            slices=busy_slices(40 * MIN),
        )
        run_folder(
            bad,
            jobs={
                "b": dict(
                    capability="produce",
                    status="blocked",
                    reason="production_row_input_source_missing",
                    blocked_at_tick=20 * MIN,
                )
            },
            slices=busy_slices(40 * MIN),
        )
        for root in (good, bad):
            write(root / "server.json", {"up": True})
        snapshots = []
        self.tool.snapshot_run = lambda run, verdict: snapshots.append((run["seed"], verdict["class"])) or ("pm", None)
        runs = self.tool.load("f")["runs"]
        self.tool.finish_runs("f", runs, reason="reached 45 game min", out=lambda *_: None)
        self.assertEqual(self.statuses(), {1: "stopped", 2: fleet.PAUSED_PROBLEM})
        self.assertEqual(snapshots, [(2, "blocked")])
        self.assertEqual(self.log, [("stop", "f-1"), ("stop", "f-2"), ("shutdown", "f-1", True)])
        held = self.tool.load("f")["runs"][1]
        self.assertEqual(held["triage"]["blocking"]["capability"], "produce")
        self.assertEqual(held["postmortem"], "pm")
        self.assertTrue((bad / "postmortem" / "triage.json").exists())

    def test_report_section_lists_problem_runs_with_ports_and_folder(self):
        runs = [
            dict(
                seed=7,
                problem=dict(
                    score=3000,
                    blocking={"capability": "mining_outpost", "reason": "no_policy_compliant_paired_bank"},
                    state=fleet.KEPT,
                    server_port=34407,
                    rcon_port=27407,
                    keep_until=None,
                    folder="runtime/v2/f-7/postmortem/x",
                    cause="preflight block_terrain_or_immovable_obstruction at (3,4) [12 water tiles]",
                    **{"class": "blocked"},
                ),
            ),
            dict(seed=8, problem={"class": "healthy"}),
        ]
        lines = fleet.problem_lines(runs)
        self.assertEqual(len(lines), 4)
        self.assertIn("mining_outpost", lines[1])
        self.assertIn("udp 34407 rcon 27407", lines[1])
        self.assertIn("water tiles", lines[2])
        self.assertIn("postmortem/x", lines[3])

    def test_gui_command_loads_a_save_copy_instead_of_joining_the_server(self):
        @dataclass(frozen=True)
        class ClientCfg:
            runtime_dir: Path
            factorio_exe: Path
            rcon_host: str = "127.0.0.1"
            server_port: int = 34400

        cfg = ClientCfg(self.base / "run", self.base / "Factorio" / "bin" / "x64" / "factorio.exe")
        command, cwd = fleet.gui_command(cfg, self.base / "gui", self.base / "gui" / "copy.zip")
        self.assertNotIn("--mp-connect", command)
        self.assertEqual(command[-2:], ["--load-game", str(self.base / "gui" / "copy.zip")])
        self.assertTrue(Path(cwd).is_relative_to(self.base / "gui"))


if __name__ == "__main__":
    unittest.main()
