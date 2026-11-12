from copy import deepcopy
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from factorio_agent.supervisor import (
    Supervisor,
    WorkQueue,
    CLIBridge,
    CLIError,
    STATE_FILE,
    STOP_FILE,
    SOURCE_ROOT,
    REPO_ROOT,
    actor_binding,
    compact_snapshot,
    stable_job_key,
    validate_decision,
)


CAPS = {
    "starter": {"parameters": {}, "requires": [], "effect": "paid starter"},
    "power": {"parameters": {}, "requires": ["starter"], "effect": "ordinary power"},
}


def node(name, cap="starter", deps=(), priority=0):
    return dict(
        id=name, capability=cap, parameters={}, depends_on=list(deps), priority=priority, reason="정상 비용 작업"
    )


def decision(operation="plan", capability=None, nodes=()):
    return dict(operation=operation, capability=capability, parameters={}, reason="typed decision", queue=list(nodes))


def actor(world="w", unit=18):
    pointer = dict(valid=True, unit_number=unit, surface_index=1, force_index=1)
    return dict(
        ok=True,
        world_id=world,
        surface_index=1,
        old_unit=unit,
        old_pointer=deepcopy(pointer),
        successor=deepcopy(pointer),
        pinned_owner_index=1,
        owner={"name": "NEC", "index": 1},
        native_repair_pending=False,
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, n):
        self.now += n


class FakeCLI:
    def __init__(self, root, clock):
        self.root = root
        self.clock = clock
        self.calls = []
        self.jobs = {}
        self.tick = 1
        self.actor = actor()
        self.stop_on_run = False
        self.run_status = "succeeded"
        self.fail_request = None
        self.bad_checkpoint = False

    def validate_requests(self, rows):
        from factorio_agent.jobs import Request

        for row in rows:
            Request(**row).validate()

    def invoke(self, args, *, stop_requested=None, on_stop=None):
        self.calls.append(list(args))
        cmd = args[0]

        def get(flag):
            return args[args.index(flag) + 1]

        if cmd == "observe":
            self.tick += 1
            return dict(
                world_id="w",
                tick=self.tick,
                technologies=["automation"],
                enabled_recipes=["assembling-machine-1"],
                inventory={"iron-plate": 8},
                entities={"stone-furnace": 1},
                electricity={"offered_watts": 0},
                available_research=[],
            )
        if cmd == "actor-status":
            return deepcopy(self.actor)
        if cmd == "capabilities":
            return deepcopy(CAPS)
        if cmd == "status":
            return dict(world_id="w", last_tick=self.tick, jobs=deepcopy(self.jobs))
        if cmd == "request":
            if self.fail_request:
                raise CLIError(self.fail_request)
            key = get("--key")
            params = json.loads(get("--parameters"))
            self.jobs.setdefault(
                key,
                dict(
                    key=key,
                    world_id=get("--world-id"),
                    capability=get("--capability"),
                    parameters=params,
                    status="pending",
                    reason="model_requested",
                    actions=0,
                    last_tick=self.tick,
                    created_at=self.clock(),
                ),
            )
            return deepcopy(self.jobs[key])
        if cmd == "run":
            job = self.jobs[get("--key")]
            if self.stop_on_run:
                (self.root / STOP_FILE).write_text(
                    json.dumps(dict(request_id="user-stop", world_id="w", source="lifecycle"))
                )
                if stop_requested and stop_requested():
                    on_stop()
                job.update(status="waiting", reason="operator_stop_requested")
            else:
                job.update(
                    status=self.run_status,
                    reason="native_postcondition" if self.run_status == "succeeded" else "waiting_for_resources",
                )
            job["actions"] += 1
            self.clock.advance(float(get("--seconds")))
            return deepcopy(job)
        if cmd in ("pause", "resume", "retry"):
            job = self.jobs[get("--key")]
            if cmd == "pause":
                if job["status"] != "paused":
                    job.update(paused_at_tick=self.tick, paused_at_time=self.clock())
                job.update(status="paused", reason="model_requested_pause")
            elif cmd == "resume":
                if job["status"] != "paused":
                    raise CLIError("only paused can resume")
                job.update(status="waiting", reason="fresh_observation_before_replanning")
            else:
                if job.get("pending_action") or job["status"] != "blocked":
                    raise CLIError("only known blocked can retry")
                job.update(status="waiting", reason="fresh_observation_before_replanning")
            with (self.root / "job-events.jsonl").open("a", encoding="utf-8") as events:
                events.write(
                    json.dumps(dict(event=cmd, key=job["key"], tick=self.tick, status=job["status"], time=self.clock()))
                    + "\n"
                )
            return deepcopy(job)
        if cmd == "stop-worker":
            return {"ok": True, "status": "stop_requested"}
        if cmd == "checkpoint":
            return dict(
                status="checkpoint_requested", world_id="other" if self.bad_checkpoint else "w", observed_tick=self.tick
            )
        raise AssertionError("unexpected command " + repr(args))


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "world-settings.json").write_text(json.dumps({"world_id": "w"}))
        self.clock = Clock()
        self.cli = FakeCLI(self.root, self.clock)
        self.client = SimpleClient(decision(nodes=[node("starter"), node("power", "power", ["starter"])]))
        self.s = Supervisor(
            self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance, max_seconds=200
        )


class SimpleClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.called = threading.Event()
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        self.last_session_id = "fake"

    def decide(self, snapshot):
        self.calls.append(deepcopy(snapshot))
        self.called.set()
        return deepcopy(self.result)


class QueueTests(Fixture):
    def test_dag_validated_before_acceptance_and_stable_job_keys_survive_restart(self):
        work = self.s.work
        work.merge([node("a"), node("b", "power", ["a"])], CAPS, self.cli.validate_requests, 1)
        before = [q["job_key"] for q in work.data["nodes"]]
        reloaded = WorkQueue(work.path, "w")
        self.assertEqual([q["job_key"] for q in reloaded.data["nodes"]], before)
        self.assertNotEqual(stable_job_key("other", node("a")), before[0])
        self.assertIn("정상", work.path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            WorkQueue(work.path, "other")

    def test_cycles_unknown_refs_unknown_cap_and_raw_parameters_leave_file_unchanged(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        before = self.s.work.path.read_bytes()
        for rows in (
            [node("b", "power", ["missing"])],
            [node("b", "power", ["c"]), node("c", "power", ["b"])],
            [node("b", "unknown")],
            [dict(node("b"), parameters={"position": {"x": 0, "y": 0}})],
        ):
            with self.assertRaises(ValueError):
                self.s.work.merge(rows, CAPS, self.cli.validate_requests, 1)
            self.assertEqual(self.s.work.path.read_bytes(), before)

    def test_native_capability_prerequisites_are_required_even_without_explicit_dag_edge(self):
        self.s.work.merge([node("power", "power")], CAPS, self.cli.validate_requests, 1)
        self.s.work.refresh([], CAPS)
        self.assertIsNone(self.s.work.ready())
        self.assertEqual(self.s.work.data["nodes"][0]["missing_prerequisites"], ["starter"])
        self.s.work.refresh(
            [dict(key="old", world_id="w", capability="starter", parameters={}, status="succeeded")], CAPS
        )
        self.assertEqual(self.s.work.ready()["id"], "power")

    def test_completion_promotes_next_ready_node_and_priority_is_deterministic(self):
        self.s.work.merge(
            [node("a"), node("b", "power", ["a"], 5), node("c", "power", ["a"], 10)],
            CAPS,
            self.cli.validate_requests,
            1,
        )
        a = self.s.work.data["nodes"][0]
        job = dict(key=a["job_key"], world_id="w", capability="starter", parameters={}, status="succeeded")
        self.s.work.refresh([job], CAPS)
        self.assertEqual(a["status"], "completed")
        self.assertEqual(self.s.work.ready()["id"], "c")

    def test_existing_node_identity_cannot_be_retargeted_or_borrow_another_job_receipt(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        with self.assertRaises(ValueError):
            self.s.work.merge([node("a", "power")], CAPS, self.cli.validate_requests, 1)
        key = self.s.work.data["nodes"][0]["job_key"]
        with self.assertRaises(ValueError):
            self.s.work.refresh(
                [dict(key=key, world_id="w", capability="power", parameters={}, status="succeeded")], CAPS
            )

    def test_dependency_duplicates_and_order_are_idempotent_for_running_and_completed_nodes(self):
        work = self.s.work
        work.merge([node("a"), node("b"), node("c", "power", ["b", "a", "a"])], CAPS, self.cli.validate_requests, 1)
        c = work.data["nodes"][2]
        self.assertEqual(c["depends_on"], ["a", "b"])
        key = c["job_key"]
        for status in ("waiting", "succeeded"):
            with self.subTest(status=status):
                jobs = [
                    dict(
                        key=q["job_key"],
                        world_id="w",
                        capability=q["capability"],
                        parameters=q["parameters"],
                        status=status if q["id"] == "c" else "succeeded",
                    )
                    for q in work.data["nodes"]
                ]
                work.refresh(jobs, CAPS)
                work.merge([node("c", "power", ["b", "b", "a"])], CAPS, self.cli.validate_requests, 2)
                c = work.node_for_job(key)
                self.assertEqual(c["depends_on"], ["a", "b"])
                self.assertEqual(c["job_key"], key)
                self.assertEqual(c["status"], "running" if status == "waiting" else "completed")

    def test_started_dependency_addition_or_removal_is_not_silently_dropped_even_when_completed(self):
        work = self.s.work
        work.merge(
            [node("a"), node("b"), node("d"), node("c", "power", ["a", "b"])], CAPS, self.cli.validate_requests, 1
        )
        for status in ("waiting", "succeeded"):
            self.cli.jobs = {
                q["job_key"]: dict(
                    key=q["job_key"],
                    world_id="w",
                    capability=q["capability"],
                    parameters=q["parameters"],
                    status=status if q["id"] == "c" else "succeeded",
                )
                for q in work.data["nodes"]
            }
            self.s._snapshot()
            before = work.path.read_bytes()
            for deps, added, removed in ((["a", "b", "d"], "d", "none"), (["a"], "none", "b")):
                with self.subTest(status=status, deps=deps):
                    with self.assertRaises(ValueError) as error:
                        work.merge(
                            [node("new_independent"), node("c", "power", deps)], CAPS, self.cli.validate_requests, 2
                        )
                    self.assertEqual(work.path.read_bytes(), before)
                    self.s._save(last_error=str(error.exception))
                    message = self.s._snapshot()[4]["last_planner_error"]
                    self.assertIn("c; added=" + added + "; removed=" + removed, message)
                    self.assertNotIn("new_independent", [q["id"] for q in work.data["nodes"]])

    def test_pending_dependencies_can_change_but_self_edges_and_unknown_duplicate_edges_still_fail(self):
        work = self.s.work
        work.merge([node("a"), node("b"), node("c", "power", ["a"])], CAPS, self.cli.validate_requests, 1)
        work.merge([node("c", "power", ["b", "b"])], CAPS, self.cli.validate_requests, 2)
        self.assertEqual(work.data["nodes"][2]["depends_on"], ["b"])
        before = work.path.read_bytes()
        for deps in (["c", "c"], ["missing", "missing"]):
            with self.assertRaises(ValueError):
                work.merge([node("c", "power", deps)], CAPS, self.cli.validate_requests, 2)
            self.assertEqual(work.path.read_bytes(), before)


class DecisionTests(Fixture):
    def test_schema_rejects_raw_shell_coords_and_unknown_operations(self):
        for value in (
            dict(decision(), shell="rm"),
            dict(decision(), operation="reconcile"),
            dict(decision("request", "starter"), parameters={"x": 1}),
            dict(decision(), reason=7),
        ):
            with self.assertRaises(ValueError):
                validate_decision(value)
        self.assertEqual(validate_decision(decision("wait"))["operation"], "wait")

    def test_snapshot_contains_current_native_unlocks_and_compact_queue_without_entities_or_geometry(self):
        context = self.s._snapshot()
        snapshot = context[4]
        self.assertEqual(snapshot["native_enabled_recipes"], ["assembling-machine-1"])
        self.assertEqual(snapshot["native_researched_technologies"], ["automation"])
        self.assertEqual(snapshot["actor"]["owner_name"], "NEC")
        self.assertIn("requires", snapshot["capabilities"]["power"])
        self.assertEqual(snapshot["entity_counts"], {"stone-furnace": 1})
        self.assertNotIn("entities", snapshot)
        self.assertNotIn("rcon_password", json.dumps(snapshot))

    def test_world_or_actor_changes_during_decision_prevent_acceptance_and_requests(self):
        context = self.s._snapshot()
        self.cli.actor = actor(unit=19)
        with self.assertRaises(ValueError):
            self.s._apply(decision(nodes=[node("a")]), context)
        self.assertEqual(self.s.work.data["nodes"], [])
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))
        self.cli.actor = actor()
        (self.root / "world-settings.json").write_text(json.dumps({"world_id": "other"}))
        with self.assertRaises(ValueError):
            self.s._apply(decision(), context)

    def test_uncertain_job_cannot_be_retried_and_pause_targets_current_active_job(self):
        self.cli.jobs["unknown"] = dict(
            key="unknown",
            world_id="w",
            capability="starter",
            parameters={},
            status="blocked",
            reason="action_outcome_unknown_reconcile_required",
            pending_action={"type": "build"},
        )
        context = self.s._snapshot()
        with self.assertRaises(ValueError):
            self.s._apply(decision("retry", "starter"), context)
        self.assertFalse(any(c[0] == "retry" for c in self.cli.calls))
        self.cli.jobs = {
            "active": dict(key="active", world_id="w", capability="starter", parameters={}, status="waiting"),
            "paused": dict(key="paused", world_id="w", capability="power", parameters={}, status="paused"),
        }
        self.s._apply(decision("pause"), self.s._snapshot())
        self.assertEqual(self.cli.jobs["active"]["status"], "paused")

    def test_one_world_cannot_adopt_two_active_jobs(self):
        self.cli.jobs = {
            str(i): dict(key=str(i), world_id="w", capability="starter", parameters={}, status="waiting")
            for i in (1, 2)
        }
        with self.assertRaises(ValueError):
            self.s._snapshot()
        self.assertFalse(any(c[0] == "run" for c in self.cli.calls))

    def test_slow_continue_or_pause_keeps_original_job_key_and_does_not_touch_successor(self):
        for operation in ("continue", "pause", "retry"):
            with self.subTest(operation=operation):
                self.cli.jobs = {
                    "a": dict(
                        key="a",
                        world_id="w",
                        capability="starter",
                        parameters={},
                        status="blocked" if operation == "retry" else "waiting",
                    )
                }
                context = self.s._snapshot()
                self.cli.jobs["a"]["status"] = "succeeded"
                self.cli.jobs["b"] = dict(
                    key="b", world_id="w", capability="power", parameters={}, status="waiting", actions=0
                )
                results = queue.Queue()
                results.put((True, decision(operation)))
                self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
                self.s.last_decision_time = self.clock()
                self.s.errors = 2
                self.s.backoff_until = self.clock() + 30
                self.cli.calls = []
                with patch.object(self.s, "_event") as event:
                    self.assertTrue(self.s._poll_decision())
                stale = [q for q in event.call_args_list if q.args[0] == "stale_decision"]
                self.assertEqual(len(stale), 1)
                self.assertEqual(stale[0].kwargs["key"], "a")
                self.assertFalse(any(c[0] in ("pause", "resume", "retry") for c in self.cli.calls))
                self.assertEqual(self.s.errors, 0)
                self.assertEqual(self.s.backoff_until, 0)
                self.assertTrue(self.s._run_authorized(self.s._snapshot()))
                self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"], ["b"])

    def test_continue_with_no_prompt_target_is_harmless_when_new_job_started_while_thinking(self):
        context = self.s._snapshot()
        self.cli.jobs["b"] = dict(key="b", world_id="w", capability="starter", parameters={}, status="waiting")
        with patch.object(self.s, "_event") as event:
            self.s._apply(decision("continue"), context)
        self.assertTrue(
            any(
                c.args[0] == "stale_decision" and c.kwargs["reason"] == "no_target_in_decision_snapshot"
                for c in event.call_args_list
            )
        )
        self.assertFalse(any(c[0] in ("pause", "resume", "retry") for c in self.cli.calls))

    def test_request_uses_fresh_idle_status_after_previous_job_completed(self):
        self.cli.jobs["a"] = dict(key="a", world_id="w", capability="starter", parameters={}, status="waiting")
        context = self.s._snapshot()
        self.cli.jobs["a"]["status"] = "succeeded"
        self.s._apply(decision("request", "power"), context)
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        requested = [c for c in self.cli.calls if c[0] == "request"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0][requested[0].index("--capability") + 1], "power")

    def test_retry_checks_fresh_unknown_outcome_and_missing_original_identity(self):
        self.cli.jobs["a"] = dict(key="a", world_id="w", capability="starter", parameters={}, status="blocked")
        context = self.s._snapshot()
        self.cli.jobs["a"]["pending_action"] = {"type": "build"}
        with self.assertRaisesRegex(ValueError, "uncertain outcome"):
            self.s._apply(decision("retry"), context)
        self.cli.jobs.pop("a")
        self.cli.jobs["b"] = dict(key="b", world_id="w", capability="starter", parameters={}, status="blocked")
        with self.assertRaisesRegex(ValueError, "original decision target"):
            self.s._apply(decision("retry"), context)
        self.assertFalse(any(c[0] == "retry" for c in self.cli.calls))

    def test_running_changes_do_not_trigger_terminal_event_but_new_terminal_and_blocker_do(self):
        self.cli.jobs = {
            "old": dict(key="old", world_id="w", capability="starter", parameters={}, status="succeeded"),
            "b": dict(key="b", world_id="w", capability="power", parameters={}, status="waiting"),
        }
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((True, decision()))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        self.s.last_decision_time = self.clock()
        self.s._poll_decision()
        self.clock.advance(11)
        self.cli.jobs["b"]["status"] = "running"
        self.assertFalse(self.s._decision_due(self.s._snapshot()[1]))
        self.cli.jobs["b"].update(status="blocked", reason="missing_iron")
        self.assertTrue(self.s._decision_due(self.s._snapshot()[1]))
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((True, decision()))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        self.s.last_decision_time = self.clock()
        self.s._poll_decision()
        self.clock.advance(11)
        self.assertFalse(self.s._decision_due(self.s._snapshot()[1]))
        self.cli.jobs["b"]["reason"] = "missing_power"
        self.assertTrue(self.s._decision_due(self.s._snapshot()[1]))

    def test_terminal_change_while_luna_thinks_remains_a_new_event(self):
        self.cli.jobs["a"] = dict(key="a", world_id="w", capability="starter", parameters={}, status="waiting")
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((True, decision("continue")))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        self.s.last_decision_time = self.clock()
        self.cli.jobs["a"]["status"] = "succeeded"
        self.s._poll_decision()
        self.clock.advance(11)
        self.assertTrue(self.s._decision_due(self.s._snapshot()[1]))

    def test_blocker_snapshot_keeps_material_amounts_and_failure_but_excludes_raw_private_payloads(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        a = self.s.work.data["nodes"][0]
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"],
            world_id="w",
            capability="starter",
            parameters={},
            status="waiting",
            reason="waiting_for_materials",
            last_failure="insufficient_inventory",
            evidence={
                "supply": {
                    "item": "iron-plate",
                    "have": 3,
                    "need": 40,
                    "required_technology": "automation",
                    "failure_category": "layout_planning",
                },
                "required_items": {"iron-plate": 40},
                "position": {"x": 123, "y": 456},
                "command": "powershell STEAL_THIS",
                "rcon_password": "STEAL_THIS",
                "secret": {"item": "STEAL_THIS"},
                "details": {"reason": "password=STEAL_THIS", "item": "C:\\private\\STEAL_THIS"},
                "oversized": [{"item": "copper-plate", "have": i, "need": 100} for i in range(1000)],
            },
        )
        snapshot = self.s._snapshot()[4]
        row = snapshot["jobs"][0]
        self.assertEqual(row["last_failure"], "insufficient_inventory")
        self.assertIn('"have": 3', json.dumps(row["evidence"]))
        self.assertIn('"need": 40', json.dumps(row["evidence"]))
        self.assertIn("automation", json.dumps(row["evidence"]))
        self.assertIn("layout_planning", json.dumps(row["evidence"]))
        encoded = json.dumps({"jobs": snapshot["jobs"], "queue": snapshot["work_queue"]})
        for forbidden in ("STEAL_THIS", '"position"', '"x"', '"y"', "rcon_password", '"command"'):
            self.assertNotIn(forbidden, encoded)
        self.assertLess(len(encoded), 6000)
        self.assertIn("insufficient_inventory", json.dumps(snapshot["work_queue"][0]["blocker"]))

    def test_recent_completed_identities_and_latest_planner_error_are_bounded_and_cleared_after_acceptance(self):
        self.s.work.merge([node("a" + str(i)) for i in range(25)], CAPS, self.cli.validate_requests, 1)
        for q in self.s.work.data["nodes"]:
            self.cli.jobs[q["job_key"]] = dict(
                key=q["job_key"], world_id="w", capability="starter", parameters={}, status="succeeded"
            )
        self.s._save(last_error="queue task identity is immutable; use a new node id")
        snapshot = self.s._snapshot()[4]
        completed = snapshot["recent_completed_queue"]
        self.assertEqual(len(completed), 16)
        self.assertEqual(
            completed[-1], dict(id="a24", capability="starter", parameters={}, status="completed", depends_on=[])
        )
        self.assertEqual(snapshot["work_queue"], [])
        self.assertIn("immutable", snapshot["last_planner_error"])
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((True, decision(nodes=[node("fresh")])))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        self.s.last_decision_time = self.clock()
        self.s._poll_decision()
        self.assertIsNone(self.s._snapshot()[4]["last_planner_error"])

    def test_power_wait_snapshot_distinguishes_missing_coal_from_live_water_and_steam(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        a = self.s.work.data["nodes"][0]
        facts = dict(drill_fuel=0, drill_powered=False, water=200, steam=400, energized_engines=2, coal_on_belts=141)
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"],
            world_id="w",
            capability="starter",
            parameters={},
            status="waiting",
            reason="waiting for dedicated coal, water, and steam flow",
            evidence=dict(facts, rcon_password="secret"),
        )
        snapshot = self.s._snapshot()[4]
        self.assertEqual(snapshot["jobs"][0]["evidence"], facts)
        self.assertNotIn("secret", json.dumps(snapshot))


class TaskTargetTests(Fixture):
    def duplicates(self, status="paused", *, parameters=None):
        parameters = {} if parameters is None else parameters
        capability = "starter_research" if parameters else "starter"
        if capability not in CAPS:
            patcher = patch.dict(
                CAPS, {capability: {"parameters": {"technology": "automation|electric-mining-drill"}, "requires": []}}
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        nodes = [dict(node(name, capability), parameters=deepcopy(parameters)) for name in ("original", "duplicate")]
        self.s.work.merge(nodes, CAPS, self.cli.validate_requests, 1)
        for q in self.s.work.data["nodes"]:
            self.cli.jobs[q["job_key"]] = dict(
                key=q["job_key"],
                world_id="w",
                capability=capability,
                parameters=deepcopy(parameters),
                status=status,
                reason="known_material_wait",
                actions=7,
                evidence={"paid_assets": 5},
            )
        return self.s.work.data["nodes"]

    def test_exact_paused_funded_task_resumes_without_duplicating_or_selecting_another_match(self):
        original, duplicate = self.duplicates(parameters={"technology": "electric-mining-drill"})
        context = self.s._snapshot()
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            self.s._apply(decision("continue"), context)
        value = dict(
            decision("continue"), target_task_id="original", parameters={"technology": "electric-mining-drill"}
        )
        self.s._apply(validate_decision(value), context)
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [original["job_key"]])
        self.assertEqual(self.cli.jobs[duplicate["job_key"]]["status"], "paused")
        self.assertEqual(self.cli.jobs[original["job_key"]]["evidence"], {"paid_assets": 5})
        self.assertEqual(len(self.s.work.data["nodes"]), 2)
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))

    def test_exact_blocked_task_retry_does_not_pick_another_identical_capability(self):
        original, duplicate = self.duplicates("blocked")
        self.s._apply(dict(decision("retry"), target_task_id="duplicate"), self.s._snapshot())
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "retry"], [duplicate["job_key"]])
        self.assertEqual(self.cli.jobs[original["job_key"]]["status"], "blocked")

    def test_targeted_pause_keeps_the_other_active_job_and_owns_only_selected_paused_task(self):
        original, duplicate = self.duplicates()
        self.cli.jobs[duplicate["job_key"]]["status"] = "waiting"
        self.s._apply(dict(decision("pause"), target_task_id="original"), self.s._snapshot())
        self.assertTrue(self.s.work.node_for_job(original["job_key"])["paused"])
        self.assertEqual(self.cli.jobs[duplicate["job_key"]]["status"], "waiting")
        self.assertFalse(any(c[0] in ("pause", "resume", "retry") for c in self.cli.calls))

    def test_unknown_id_or_mismatched_capability_or_exact_parameters_never_retargets(self):
        self.duplicates(parameters={"technology": "electric-mining-drill"})
        context = self.s._snapshot()
        base = dict(decision("continue"), target_task_id="original", parameters={"technology": "electric-mining-drill"})
        for change in (
            {"target_task_id": "missing"},
            {"capability": "power"},
            {"parameters": {}},
            {"parameters": {"technology": "automation"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.s._apply({**base, **change}, context)
        self.assertFalse(any(c[0] in ("resume", "retry", "pause", "request") for c in self.cli.calls))

    def test_target_not_in_prompt_is_stale_and_completed_original_never_controls_successor(self):
        original, duplicate = self.duplicates()
        empty_context = self.s._snapshot()
        empty_context = (empty_context[0], {"world_id": "w", "jobs": {}}, *empty_context[2:])
        with patch.object(self.s, "_event") as event:
            self.s._apply(dict(decision("continue"), target_task_id="original"), empty_context)
        self.assertTrue(any(c.args[0] == "stale_decision" for c in event.call_args_list))
        context = self.s._snapshot()
        self.cli.jobs[original["job_key"]]["status"] = "succeeded"
        self.cli.jobs[duplicate["job_key"]]["status"] = "waiting"
        with patch.object(self.s, "_event") as event:
            self.s._apply(dict(decision("continue"), target_task_id="original"), context)
        self.assertTrue(
            any(
                c.args[0] == "stale_decision" and c.kwargs.get("key") == original["job_key"]
                for c in event.call_args_list
            )
        )
        self.assertFalse(any(c[0] in ("resume", "retry", "pause") for c in self.cli.calls))

    def test_selected_unknown_outcome_including_empty_intent_is_never_retried(self):
        original, _ = self.duplicates("blocked")
        for extra in (
            {"pending_action": {}},
            {"pending_action": {"type": "build"}},
            {"reason": "mine_batch_outcome_unknown"},
        ):
            self.cli.jobs[original["job_key"]].pop("pending_action", None)
            self.cli.jobs[original["job_key"]].update({"reason": "known_blocked", **extra})
            with self.assertRaisesRegex(ValueError, "uncertain outcome"):
                self.s._apply(dict(decision("retry"), target_task_id="original"), self.s._snapshot())
        self.assertFalse(any(c[0] == "retry" for c in self.cli.calls))

    def test_target_schema_is_nullable_bounded_and_control_only(self):
        self.assertIsNone(validate_decision(dict(decision(), target_task_id=None))["target_task_id"])
        for target in ("", "a" * 65, "invalid id", {"x": 1}, True):
            with self.assertRaises(ValueError):
                validate_decision(dict(decision("continue"), target_task_id=target))
        for operation in ("plan", "request", "wait"):
            with self.assertRaises(ValueError):
                validate_decision(dict(decision(operation, "starter"), target_task_id="original"))


class ExecutionTests(Fixture):
    def test_wait_keeps_running_active_approved_job(self):
        self.cli.jobs["a"] = dict(
            key="a", world_id="w", capability="starter", parameters={}, status="waiting", actions=0
        )
        self.cli.run_status = "waiting"
        self.s._apply(decision("wait"), self.s._snapshot())
        self.assertGreater(self.s.wait_until, self.clock())
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"], ["a"])

    def test_wait_delays_only_idle_planning_and_ready_queue_dispatches_immediately(self):
        self.s._apply(decision("wait"), self.s._snapshot())
        self.assertFalse(self.s._decision_due(self.s._snapshot()[1]))
        self.s.work.merge([node("approved")], CAPS, self.cli.validate_requests, 1)
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "request"]), 1)

    def test_pausing_one_job_does_not_delay_independent_ready_work(self):
        self.s.work.merge([node("a"), node("b")], CAPS, self.cli.validate_requests, 1)
        a, b = self.s.work.data["nodes"]
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"], world_id="w", capability="starter", parameters={}, status="waiting"
        )
        self.s._apply(decision("pause"), self.s._snapshot())
        self.assertEqual(self.cli.jobs[a["job_key"]]["status"], "paused")
        self.assertTrue(self.s.work.node_for_job(a["job_key"])["paused"])
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"], [b["job_key"]])

    def test_approved_successor_dispatches_while_luna_call_is_still_pending(self):
        self.s.work.merge([node("a"), node("b", "power", ["a"])], CAPS, self.cli.validate_requests, 1)
        a = self.s.work.data["nodes"][0]
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"], world_id="w", capability="starter", parameters={}, status="succeeded"
        )
        self.s.pending_decision = {"result": queue.Queue(), "context": None, "started_at": self.clock()}
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        runs = [c for c in self.cli.calls if c[0] == "run"]
        self.assertEqual(len(runs), 1)
        b = self.s.work.data["nodes"][1]
        self.assertIn(b["job_key"], runs[0])
        self.assertEqual(self.cli.jobs[b["job_key"]]["capability"], "power")
        self.assertIsNotNone(self.s.pending_decision)

    def test_duplicate_dispatch_adopts_existing_same_key_instead_of_new_request(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        self.cli.run_status = "waiting"
        self.s._run_authorized(self.s._snapshot())
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "request"]), 1)
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "run"]), 2)

    def test_known_false_request_blocks_that_node_without_blind_repeat(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        self.cli.fail_request = "native prerequisite unavailable"
        with self.assertRaises(CLIError):
            self.s._run_authorized(self.s._snapshot())
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "request"]), 1)
        self.assertEqual(self.s.work.data["nodes"][0]["status"], "blocked")

    def test_safe_stop_waits_run_boundary_pauses_and_checkpoints_without_new_work(self):
        self.cli.stop_on_run = True
        result = self.s.run()
        self.assertEqual(result["status"], "safe_stopped")
        self.assertTrue(result["saved"])
        self.assertEqual(result["stop_request_id"], "user-stop")
        self.assertTrue(result["worker_stop_owned"])
        self.assertEqual(
            result["worker_stop_sha256"],
            __import__("hashlib").sha256((self.root / "stop.json").read_bytes()).hexdigest(),
        )
        commands = [q[0] for q in self.cli.calls]
        stop = commands.index("stop-worker")
        self.assertNotIn("run", commands[stop + 1 :])
        self.assertNotIn("request", commands[stop + 1 :])
        self.assertLess(commands.index("pause"), commands.index("checkpoint"))
        disk = json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(disk["state"], "stopped")
        self.assertTrue((self.root / "controller.lock").exists())

    def test_stop_before_late_model_result_discards_it_and_never_submits(self):
        context = self.s._snapshot()
        results = queue.Queue()
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        (self.root / STOP_FILE).write_text(json.dumps(dict(request_id="during-model", world_id="w")))
        results.put((True, decision(nodes=[node("late")])))
        self.assertFalse(self.s._poll_decision())
        self.assertEqual(self.s.safe_stop()["status"], "safe_stopped")
        self.assertEqual(self.s.work.data["nodes"], [])
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))

    def test_other_operators_stop_marker_is_not_claimed_or_overwritten(self):
        path = self.root / "stop.json"
        path.write_bytes(b'{"source":"another-operator"}')
        before = path.read_bytes()
        self.s._worker_stop()
        self.assertFalse(self.s.state["worker_stop_owned"])
        self.assertEqual(path.read_bytes(), before)

    def test_failed_checkpoint_or_foreign_stop_never_acknowledges_saved(self):
        self.cli.bad_checkpoint = True
        result = self.s.safe_stop()
        self.assertFalse(result["saved"])
        self.assertEqual(result["status"], "blocked")
        self.s.stop_marker = {"request_id": "foreign", "world_id": "other"}
        self.cli.calls = []
        self.assertFalse(self.s.safe_stop()["saved"])
        self.assertFalse(any(c[0] == "checkpoint" for c in self.cli.calls))

    def test_start_resumes_only_exact_previously_safe_paused_job(self):
        self.cli.jobs["old"] = dict(key="old", world_id="w", capability="starter", parameters={}, status="paused")
        path = self.root / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                dict(status="safe_stopped", saved=True, world_id="w", pause_receipt={"key": "old"}, decision_sequence=3)
            )
        )
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        self.assertTrue(restarted._resume_known(restarted._snapshot()))
        self.assertEqual(self.cli.jobs["old"]["status"], "waiting")
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))

    def test_client_failure_has_no_fallback_model_or_new_task(self):
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((False, RuntimeError("Luna unavailable")))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        with self.assertRaises(RuntimeError):
            self.s._poll_decision()
        self.assertEqual(self.s.state["model"], "gpt-6-luna")
        self.assertEqual(self.s.work.data["nodes"], [])
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))


class PreemptionTests(Fixture):
    def prepared(self, high_priority=200, extra=()):
        self.s.work.merge(
            [node("low", priority=100), node("high", priority=high_priority), *extra],
            CAPS,
            self.cli.validate_requests,
            1,
        )
        low = self.s.work.data["nodes"][0]
        high = self.s.work.data["nodes"][1]
        self.cli.jobs[low["job_key"]] = dict(
            key=low["job_key"],
            world_id="w",
            capability="starter",
            parameters={},
            status="waiting",
            reason="execution_slice_complete",
            actions=7,
            last_tick=1,
            created_tick=1,
            created_at=self.clock(),
            evidence={"paid_assets": 5},
        )
        self.cli.run_status = "waiting"
        return low, high

    def autonomous(self):
        low, high = self.prepared(10)
        self.cli.jobs[low["job_key"]].update(
            evidence={"autonomous_wait": True, "background_work_safe": True},
            evidence_status="observation_result",
            evidence_observed_tick=1,
        )
        return low, high

    def test_autonomous_wait_runs_lower_priority_approved_preparation_and_resumes_exact_owner(self):
        with patch.dict(CAPS["starter"], {"opportunistic": True}):
            owner, work = self.autonomous()
            self.assertTrue(self.s._run_authorized(self.s._snapshot()))
            self.assertEqual(self.cli.jobs[owner["job_key"]]["status"], "paused")
            marker = self.s.work.node_for_job(owner["job_key"])["scheduler_pause"]
            self.assertEqual(marker["yield_reason"], "autonomous_wait")
            self.assertIn(work["job_key"], [c[2] for c in self.cli.calls if c[0] == "run"])
            self.cli.jobs[work["job_key"]]["status"] = "succeeded"
            self.s._run_authorized(self.s._snapshot())
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [owner["job_key"]])

    def test_lower_priority_normal_ready_row_does_not_mask_eligible_preparation(self):
        with patch.dict(CAPS, {"prepare_components": {"parameters": {}, "requires": [], "opportunistic": True}}):
            owner, ordinary = self.autonomous()
            prep = node("prep", "prepare_components", priority=5)
            prep["parameters"] = {"items": {"pipe": 1}}
            self.s.work.merge([prep], CAPS, self.cli.validate_requests, 1)
            target = self.s.work.node_for_job(
                next(q["job_key"] for q in self.s.work.data["nodes"] if q["id"] == "prep")
            )
            self.s._run_authorized(self.s._snapshot())
            self.assertEqual(self.cli.jobs[owner["job_key"]]["status"], "paused")
            self.assertIn(target["job_key"], self.cli.jobs)
            self.assertNotIn(ordinary["job_key"], self.cli.jobs)
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"], [target["job_key"]])

    def test_resource_waiting_preparation_is_preserved_paused_after_one_slice_then_owner_resumes(self):
        with patch.dict(CAPS["starter"], {"opportunistic": True}):
            owner, child = self.autonomous()
            self.s._run_authorized(self.s._snapshot())
            self.cli.jobs[child["job_key"]]["evidence"] = {"paid_stock": 7}
            self.s._run_authorized(self.s._snapshot())
            self.assertEqual(self.cli.jobs[child["job_key"]]["status"], "paused")
            self.assertEqual(self.cli.jobs[child["job_key"]]["actions"], 1)
            self.assertEqual(self.cli.jobs[child["job_key"]]["evidence"], {"paid_stock": 7})
            self.assertTrue(self.s.work.node_for_job(child["job_key"])["paused"])
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [owner["job_key"]])
            marker = self.s.work.node_for_job(owner["job_key"])["scheduler_pause"]
            self.assertEqual(marker["phase"], "resumed")
            returned = marker["opportunistic_return"]
            self.assertEqual(returned["phase"], "child_paused")
            self.assertEqual(returned["pause_binding"]["key"], child["job_key"])
            self.assertEqual(returned["pause_event"]["event"], "pause")
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"].count(child["job_key"]), 1)

    def test_opportunistic_work_has_a_bounded_slice_even_with_a_higher_numeric_priority(self):
        with patch.dict(CAPS["starter"], {"opportunistic": True}):
            owner, child = self.autonomous()
            child["priority"] = 200
            self.s.work.save()
            self.s._run_authorized(self.s._snapshot())
            self.s._run_authorized(self.s._snapshot())
            self.assertEqual(self.cli.jobs[child["job_key"]]["status"], "paused")
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [owner["job_key"]])

    def test_unknown_child_outcome_or_stop_never_triggers_automatic_child_pause(self):
        for change in ("unknown", "stop"):
            self.setUp()
            with patch.dict(CAPS["starter"], {"opportunistic": True}):
                owner, child = self.autonomous()
                self.s._run_authorized(self.s._snapshot())
                if change == "unknown":
                    self.cli.jobs[child["job_key"]]["pending_action"] = {}
                else:
                    (self.root / STOP_FILE).write_text(json.dumps({"world_id": "w", "request_id": "stop"}))
                self.assertFalse(self.s._scheduler_yield_opportunistic(self.s._snapshot()))
                self.assertEqual([c[2] for c in self.cli.calls if c[0] == "pause"], [owner["job_key"]])

    def test_explicit_child_control_releases_owned_return_and_is_not_overridden(self):
        with patch.dict(CAPS["starter"], {"opportunistic": True}):
            owner, child = self.autonomous()
            self.s._run_authorized(self.s._snapshot())
            self.cli.invoke(["pause", "--key", child["job_key"]])
            self.cli.invoke(["resume", "--key", child["job_key"]])
            self.assertFalse(self.s._scheduler_yield_opportunistic(self.s._snapshot()))
            self.assertEqual(self.s.work.node_for_job(owner["job_key"])["scheduler_pause"]["phase"], "released")
            self.assertEqual([c[2] for c in self.cli.calls if c[0] == "pause"], [owner["job_key"], child["job_key"]])

    def test_unverified_child_pause_intent_cannot_automatically_resume_the_owner(self):
        with patch.dict(CAPS["starter"], {"opportunistic": True}):
            owner, child = self.autonomous()
            self.s._run_authorized(self.s._snapshot())
            invoke = self.cli.invoke

            def changed(args, **kwargs):
                result = invoke(args, **kwargs)
                if args[:3] == ["pause", "--key", child["job_key"]]:
                    result["status"] = "waiting"
                return result

            with patch.object(self.cli, "invoke", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "child pause receipt"):
                    self.s._scheduler_yield_opportunistic(self.s._snapshot())
            self.assertIsNone(self.s._scheduler_owned(self.s._snapshot()))
            self.assertEqual(
                self.s.work.node_for_job(owner["job_key"])["scheduler_pause"]["opportunistic_return"]["phase"],
                "pause_intent",
            )
            self.cli.jobs[child["job_key"]]["status"] = "waiting"
            before = len([c for c in self.cli.calls if c[0] == "run"])
            with self.assertRaisesRegex(ValueError, "pause outcome requires reconciliation"):
                self.s._run_authorized(self.s._snapshot())
            self.assertEqual(len([c for c in self.cli.calls if c[0] == "run"]), before)

    def test_lower_priority_work_needs_fresh_authoritative_safe_wait_evidence(self):
        for change in (
            {"evidence_status": "retained_from_prior_observation"},
            {"evidence_observed_tick": -5000},
            {"evidence": {"autonomous_wait": True}},
            {"pending_action": {}},
        ):
            self.setUp()
            with patch.dict(CAPS["starter"], {"opportunistic": True}):
                owner, _ = self.autonomous()
                self.cli.jobs[owner["job_key"]].update(change)
                self.s._run_authorized(self.s._snapshot())
                self.assertFalse(any(c[0] == "pause" for c in self.cli.calls))

    def test_native_wait_does_not_authorize_unapproved_or_nonopportunistic_work(self):
        self.autonomous()
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "pause" for c in self.cli.calls))

    def test_strictly_higher_approved_work_pauses_exact_partial_job_then_dispatches(self):
        low, high = self.prepared()
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual(self.cli.jobs[low["job_key"]]["status"], "paused")
        self.assertEqual(self.cli.jobs[low["job_key"]]["evidence"], {"paid_assets": 5})
        self.assertEqual(self.cli.jobs[low["job_key"]]["actions"], 7)
        marker = self.s.work.node_for_job(low["job_key"])["scheduler_pause"]
        self.assertEqual(marker["phase"], "paused")
        self.assertEqual(marker["higher_task_id"], "high")
        self.assertEqual(marker["job_identity"]["key"], low["job_key"])
        actions = [c for c in self.cli.calls if c[0] in ("pause", "request", "run")]
        self.assertEqual([c[0] for c in actions], ["pause", "request", "run"])
        self.assertEqual(actions[0][2], low["job_key"])
        self.assertIn(high["job_key"], actions[1])

    def test_equal_or_lower_priorities_and_unready_dependencies_do_not_preempt(self):
        for priority in (100, 99):
            self.setUp()
            low, high = self.prepared(priority)
            self.s._run_authorized(self.s._snapshot())
            self.assertFalse(any(c[0] in ("pause", "request") for c in self.cli.calls))
            self.assertEqual(self.cli.jobs[low["job_key"]]["status"], "waiting")
        self.setUp()
        low, high = self.prepared(extra=[node("dep", priority=0)])
        self.s.work.merge([node("high", deps=["dep"], priority=200)], CAPS, self.cli.validate_requests, 1)
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] in ("pause", "request") for c in self.cli.calls))

    def test_unqueued_active_job_has_no_invented_default_priority(self):
        self.s.work.merge([node("high", priority=200)], CAPS, self.cli.validate_requests, 1)
        self.cli.jobs["manual"] = dict(
            key="manual", world_id="w", capability="starter", parameters={}, status="waiting", actions=0
        )
        self.cli.run_status = "waiting"
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] in ("pause", "request") for c in self.cli.calls))
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "run"], ["manual"])

    def test_pending_unknown_or_running_jobs_never_preempt(self):
        for field, value in (
            ("pending_action", {}),
            ("pending_action", {"type": "build"}),
            ("reason", "action_outcome_unknown_reconcile_required"),
            ("status", "running"),
        ):
            self.setUp()
            low, _ = self.prepared()
            self.cli.jobs[low["job_key"]][field] = value
            self.s._run_authorized(self.s._snapshot())
            self.assertFalse(any(c[0] in ("pause", "request") for c in self.cli.calls))

    def test_new_uncertain_intent_and_changed_world_are_rechecked_before_pause(self):
        low, _ = self.prepared()
        context = self.s._snapshot()
        self.cli.jobs[low["job_key"]]["pending_action"] = {}
        self.s._run_authorized(context)
        self.assertFalse(any(c[0] in ("pause", "request") for c in self.cli.calls))
        self.setUp()
        low, _ = self.prepared()
        context = self.s._snapshot()
        self.cli.jobs[low["job_key"]]["world_id"] = "other"
        with self.assertRaises(ValueError):
            self.s._run_authorized(context)
        self.assertFalse(any(c[0] == "pause" for c in self.cli.calls))

    def test_stop_between_pause_and_dispatch_preserves_owned_marker_without_new_run(self):
        low, _ = self.prepared()
        original = self.cli.invoke

        def stopping(args, **kwargs):
            r = original(args, **kwargs)
            if args[0] == "pause":
                (self.root / STOP_FILE).write_text(json.dumps(dict(request_id="boundary", world_id="w")))
            return r

        with patch.object(self.cli, "invoke", side_effect=stopping):
            self.assertFalse(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual(self.s.work.node_for_job(low["job_key"])["scheduler_pause"]["phase"], "paused")
        self.assertFalse(any(c[0] in ("request", "run") for c in self.cli.calls))

    def test_actor_changes_before_pause_or_resume_do_not_retarget(self):
        low, high = self.prepared()
        context = self.s._snapshot()
        self.cli.actor = actor(unit=19)
        with self.assertRaises(ValueError):
            self.s._run_authorized(context)
        self.assertFalse(any(c[0] == "pause" for c in self.cli.calls))
        self.setUp()
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.cli.actor = actor(unit=19)
        with self.assertRaises(ValueError):
            self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))

    def test_actor_change_after_pause_prevents_higher_dispatch(self):
        self.prepared()
        original = self.cli.invoke

        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "pause":
                self.cli.actor = actor(unit=19)
            return result

        with patch.object(self.cli, "invoke", side_effect=changed):
            with self.assertRaises(ValueError):
                self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] in ("request", "run") for c in self.cli.calls))

    def test_completed_higher_work_resumes_only_scheduler_owned_job_across_restart(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        self.assertTrue(restarted._run_authorized(restarted._snapshot()))
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [low["job_key"]])
        self.assertEqual(restarted.work.node_for_job(low["job_key"])["scheduler_pause"]["phase"], "resumed")
        self.assertEqual(self.cli.jobs[low["job_key"]]["evidence"], {"paid_assets": 5})

    def test_remaining_ready_priority_wins_before_lower_scheduler_resume(self):
        low, high = self.prepared(extra=[node("middle", priority=150)])
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.s._run_authorized(self.s._snapshot())
        middle = self.s.work.data["nodes"][2]
        self.assertEqual(self.cli.jobs[middle["job_key"]]["status"], "waiting")
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))
        self.cli.jobs[middle["job_key"]]["status"] = "succeeded"
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [low["job_key"]])

    def test_user_repeated_pause_even_with_identical_job_fields_releases_scheduler_ownership(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        before = deepcopy(self.cli.jobs[low["job_key"]])
        self.cli.invoke(["pause", "--key", low["job_key"]])
        self.assertEqual(before, self.cli.jobs[low["job_key"]])
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))
        self.assertEqual(self.s.work.node_for_job(low["job_key"])["scheduler_pause"]["phase"], "released")

    def test_explicit_model_pause_of_already_paused_job_cancels_automatic_resume(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.s._apply(decision("pause", "starter"), self.s._snapshot())
        self.s._run_authorized(self.s._snapshot())
        current = self.s.work.node_for_job(low["job_key"])
        self.assertTrue(current["paused"])
        self.assertEqual(current["scheduler_pause"]["phase"], "released")
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))

    def test_unconfirmed_pause_intent_after_restart_never_adopts_a_user_pause(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.s.work.node_for_job(low["job_key"])["scheduler_pause"]["phase"] = "pause_intent"
        self.s.work.save()
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        self.assertFalse(restarted._run_authorized(restarted._snapshot()))
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))

    def test_no_oscillation_while_higher_job_waits_and_unknown_paused_jobs_stay_paused(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        for _ in range(3):
            self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "pause"]), 1)
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.cli.jobs[low["job_key"]]["pending_action"] = {"type": "build"}
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))

    def test_changed_paused_identity_or_truncated_control_journal_never_resumes(self):
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        self.cli.jobs[low["job_key"]]["created_tick"] = 999
        self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))
        self.setUp()
        low, high = self.prepared()
        self.s._run_authorized(self.s._snapshot())
        self.cli.jobs[high["job_key"]]["status"] = "succeeded"
        (self.root / "job-events.jsonl").write_bytes(b"")
        with self.assertRaises(ValueError):
            self.s._run_authorized(self.s._snapshot())
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))

    def test_scheduler_paused_dependencies_remain_immutable_and_lifecycle_resume_stays_exact(self):
        low, high = self.prepared(extra=[node("dep", priority=0)])
        self.s._run_authorized(self.s._snapshot())
        with self.assertRaises(ValueError):
            self.s.work.merge([node("low", deps=["dep"], priority=100)], CAPS, self.cli.validate_requests, 1)
        self.s.stop_marker = dict(request_id="safe", world_id="w")
        self.assertTrue(self.s.safe_stop()["saved"])
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        self.assertTrue(restarted._resume_known(restarted._snapshot()))
        self.assertEqual([c[2] for c in self.cli.calls if c[0] == "resume"], [high["job_key"]])


class BridgeTests(unittest.TestCase):
    def test_every_slice_uses_fresh_python_with_absolute_current_source_and_no_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock(pid=7, returncode=0)
            process.communicate.return_value = ("{}", "")
            with patch("factorio_agent.supervisor.subprocess.Popen", return_value=process) as spawn:
                cli = CLIBridge(Path(directory).resolve())
                cli.invoke(["run", "--key", "a", "--seconds", "25"])
                cli.invoke(["run", "--key", "a", "--seconds", "25"])
            self.assertEqual(spawn.call_count, 2)
            for call in spawn.call_args_list:
                argv = call.args[0]
                self.assertTrue(Path(argv[0]).is_absolute())
                self.assertEqual(argv[1:4], ["-B", "-m", "factorio_agent"])
                self.assertEqual(call.kwargs["env"]["PYTHONPATH"], str(SOURCE_ROOT))
                self.assertEqual(call.kwargs["cwd"], str(REPO_ROOT))
                self.assertNotIn("shell", call.kwargs)


if __name__ == "__main__":
    unittest.main()
