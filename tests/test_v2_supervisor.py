from copy import deepcopy
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from factorio_agent.jobs import _admission_error
from factorio_agent.supervisor import (
    Supervisor,
    WorkQueue,
    CLIBridge,
    CLIError,
    STATE_FILE,
    STOP_FILE,
    SOURCE_ROOT,
    REPO_ROOT,
    stable_job_key,
    validate_decision,
)


REAL_LOAD_AUTOPILOT = Supervisor._load_autopilot
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
        self.ran = []
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
            if key not in self.jobs:
                refusal = _admission_error(self.jobs, get("--capability"))
                if refusal:
                    raise CLIError(refusal)
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
                    priority=int(get("--priority")),
                    actions=0,
                    last_tick=self.tick,
                    created_at=self.clock(),
                ),
            )
            return deepcopy(self.jobs[key])
        if cmd == "run":
            self.assertion(args[1] == "--all", "the supervisor runs every active job together")
            active = [j for j in self.jobs.values() if j["status"] in ("pending", "running", "waiting")]
            self.ran.append([j["key"] for j in active])
            if self.stop_on_run:
                (self.root / STOP_FILE).write_text(
                    json.dumps(dict(request_id="user-stop", world_id="w", source="lifecycle"))
                )
                if stop_requested and stop_requested():
                    on_stop()
            for job in active:
                if self.stop_on_run:
                    job.update(status="waiting", reason="operator_stop_requested")
                else:
                    job.update(
                        status=self.run_status,
                        reason="native_postcondition" if self.run_status == "succeeded" else "waiting_for_resources",
                    )
                job["actions"] = job.get("actions", 0) + 1
            self.clock.advance(float(get("--seconds")))
            return dict(
                status="running" if active else "idle",
                jobs={j["key"]: {k: j.get(k) for k in ("status", "reason", "actions", "capability")} for j in active},
                actions=len(active),
                last_tick=self.tick,
            )
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

    @staticmethod
    def assertion(condition, message):
        if not condition:
            raise AssertionError(message)

    def ran_keys(self):
        return [key for keys in self.ran for key in keys]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "world-settings.json").write_text(json.dumps({"world_id": "w"}))
        self.clock = Clock()
        self.cli = FakeCLI(self.root, self.clock)
        self.client = SimpleClient(decision(nodes=[node("starter"), node("power", "power", ["starter"])]))
        # The framework autopilot is exercised explicitly in AutopilotTests only.
        autopilot = patch.object(Supervisor, "_load_autopilot", return_value=None)
        autopilot.start()
        self.addCleanup(autopilot.stop)
        self.s = Supervisor(
            self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance, max_seconds=200
        )


class SimpleClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.called = threading.Event()
        self.last_usage = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}
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
                    message = str(error.exception)
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

    def test_snapshot_context_is_observation_status_capabilities_and_actor_only(self):
        observation, status, capabilities, bound = self.s._snapshot()
        self.assertEqual(observation["enabled_recipes"], ["assembling-machine-1"])
        self.assertEqual(status["world_id"], "w")
        self.assertIn("requires", capabilities["power"])
        self.assertEqual(bound["owner_name"], "NEC")
        self.assertEqual(self.s.technology_order, ["automation"])

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

    def test_slow_pause_or_retry_keeps_original_job_key_and_does_not_touch_successor(self):
        for operation in ("pause", "retry"):
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
                self.cli.ran = []
                self.assertTrue(self.s._run_authorized(self.s._snapshot()))
                self.assertEqual(self.cli.ran, [["b"]])

    def test_untargeted_continue_is_a_noop_even_with_a_paused_job_or_a_new_job(self):
        self.cli.jobs["a"] = dict(key="a", world_id="w", capability="starter", parameters={}, status="paused")
        context = self.s._snapshot()
        self.cli.jobs["b"] = dict(key="b", world_id="w", capability="power", parameters={}, status="waiting")
        with patch.object(self.s, "_event") as event:
            self.s._apply(decision("continue"), context)
        self.assertTrue(any(c.args[0] == "decision_noop" for c in event.call_args_list))
        self.assertFalse(any(c[0] in ("pause", "resume", "retry") for c in self.cli.calls))
        self.assertEqual(self.cli.jobs["a"]["status"], "paused")

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

    def blocked_incident(self, **job_fields):
        """One queue node whose job stays blocked past the grace period; returns its open incident."""
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        a = self.s.work.data["nodes"][0]
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"], world_id="w", capability="starter", parameters={}, status="blocked", **job_fields
        )
        self.s._check_incidents(self.s._snapshot())
        self.clock.advance(61)
        inputs, roadmap = self.s._check_incidents(self.s._snapshot())
        incident = next(q for q in self.s.incidents.open.values() if q.kind == "blocked_job")
        return incident, self.s._document(incident, inputs, roadmap)

    def test_blocked_incident_keeps_material_amounts_and_failure_but_excludes_raw_private_payloads(self):
        incident, document = self.blocked_incident(
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
        evidence = json.dumps(incident.facts["evidence"])
        self.assertEqual(incident.facts["evidence"]["last_failure"], "insufficient_inventory")
        for text in ('"have": 3', '"need": 40', "automation", "layout_planning"):
            self.assertIn(text, evidence)
        encoded = json.dumps(document)
        for forbidden in ("STEAL_THIS", '"position"', '"x"', '"y"', "rcon_password", '"command"'):
            self.assertNotIn(forbidden, encoded)
        self.assertLess(len(encoded), 6000)

    def test_power_wait_incident_distinguishes_missing_coal_from_live_water_and_steam(self):
        facts = dict(drill_fuel=0, drill_powered=False, water=200, steam=400, energized_engines=2, coal_on_belts=141)
        incident, document = self.blocked_incident(
            reason="waiting for dedicated coal, water, and steam flow", evidence=dict(facts, rcon_password="secret")
        )
        self.assertEqual(incident.facts["evidence"]["evidence"], facts)
        self.assertNotIn("secret", json.dumps(document))

    def test_accepted_decision_clears_the_latest_planner_error(self):
        self.s._save(last_error="queue task identity is immutable; use a new node id")
        context = self.s._snapshot()
        results = queue.Queue()
        results.put((True, decision(nodes=[node("fresh")])))
        self.s.pending_decision = {"result": results, "context": context, "started_at": self.clock()}
        self.s.last_decision_time = self.clock()
        self.s._poll_decision()
        self.assertIsNone(self.s.state["last_error"])


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
        self.s._apply(decision("continue"), context)
        self.assertFalse(any(c[0] == "resume" for c in self.cli.calls))
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
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual(self.cli.ran, [["a"]])

    def test_wait_changes_nothing_and_ready_queue_dispatches_immediately(self):
        self.s._apply(decision("wait"), self.s._snapshot())
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
        self.assertEqual(self.cli.ran, [[b["job_key"]]])

    def test_approved_successor_dispatches_while_luna_call_is_still_pending(self):
        self.s.work.merge([node("a"), node("b", "power", ["a"])], CAPS, self.cli.validate_requests, 1)
        a = self.s.work.data["nodes"][0]
        self.cli.jobs[a["job_key"]] = dict(
            key=a["job_key"], world_id="w", capability="starter", parameters={}, status="succeeded"
        )
        self.s.pending_decision = {"result": queue.Queue(), "context": None, "started_at": self.clock()}
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        b = self.s.work.data["nodes"][1]
        self.assertEqual(self.cli.ran, [[b["job_key"]]])
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
        self.assertFalse(self.s._run_authorized(self.s._snapshot()))
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "request"]), 1)
        self.assertEqual(self.s.work.data["nodes"][0]["status"], "blocked")

    def test_safe_stop_waits_run_boundary_pauses_and_checkpoints_without_new_work(self):
        self.s.work.merge([node("starter")], CAPS, self.cli.validate_requests, 1)
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


MANY = {"c" + str(i): {"parameters": {}, "requires": [], "effect": "independent"} for i in range(6)}


def job(key, capability="starter", status="waiting", **extra):
    return dict(key=key, world_id="w", capability=capability, parameters={}, status=status, actions=0, **extra)


class MultiJobTests(Fixture):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(CAPS, MANY)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The test capabilities are not native job capabilities; queue validation is covered elsewhere.
        self.cli.validate_requests = lambda rows: None

    def requested(self):
        return [c for c in self.cli.calls if c[0] == "request"]

    def test_up_to_four_ready_nodes_dispatch_in_priority_order_then_one_interleaved_slice(self):
        nodes = [node("n" + str(i), "c" + str(i), priority=p) for i, p in enumerate((1, 6, 3, 5, 2, 4))]
        self.s.work.merge(nodes, CAPS, self.cli.validate_requests, 1)
        self.cli.run_status = "waiting"
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        requested = self.requested()
        self.assertEqual([c[c.index("--capability") + 1] for c in requested], ["c1", "c3", "c5", "c2"])
        self.assertEqual([c[c.index("--priority") + 1] for c in requested], ["6", "5", "4", "3"])
        self.assertEqual([c for c in self.cli.calls if c[0] == "run"], [["run", "--all", "--seconds", "25"]])
        self.assertEqual(len(self.cli.ran[0]), 4)
        self.assertEqual(len(self.s.state["active_job_keys"]), 4)
        self.assertEqual(self.s.state["active_job_key"], self.s.state["active_job_keys"][0])
        self.s._snapshot()
        self.assertEqual(sorted(q["id"] for q in self.s.work.ready_nodes()), ["n0", "n4"])
        self.assertEqual(self.s.state["active_job_key"], self.requested()[0][2])

    def test_blocked_job_is_retried_after_another_job_succeeds_and_only_a_few_times(self):
        self.cli.jobs["stuck"] = job("stuck", "c0", status="blocked", reason="coal_bank_required")
        self.cli.jobs["lost"] = job("lost", "c1", status="blocked", reason="action_outcome_unknown_reconcile_required")

        def retries():
            return [c for c in self.cli.calls if c[0] == "retry"]

        self.assertFalse(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual(retries(), [])
        self.cli.jobs["done1"] = job("done1", "c2", status="succeeded")
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual(retries(), [["retry", "--key", "stuck"]])
        # Blocked again with no new success: no further retry.
        self.cli.jobs["stuck"].update(status="blocked", reason="coal_bank_required")
        self.assertFalse(self.s._auto_retry(self.s._snapshot()))
        for index in range(2, 8):
            self.cli.jobs["done" + str(index)] = job("done" + str(index), "c3", status="succeeded")
            self.cli.jobs["stuck"].update(status="blocked", reason="coal_bank_required")
            self.s._auto_retry(self.s._snapshot())
        self.assertEqual(len(retries()), 3)
        self.assertNotIn(["retry", "--key", "lost"], retries())

    def test_stalled_job_is_retried_after_a_quiet_interval_even_without_a_success(self):
        blocked_at = self.clock()
        self.cli.jobs["stuck"] = job(
            "stuck", "c0", status="blocked", reason="objective_progress_stalled", blocked_at_time=blocked_at
        )
        self.cli.jobs["other"] = job(
            "other", "c1", status="blocked", reason="coal_bank_required", blocked_at_time=blocked_at
        )
        self.assertFalse(self.s._auto_retry(self.s._snapshot()))
        self.clock.advance(121)
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual([c for c in self.cli.calls if c[0] == "retry"], [["retry", "--key", "stuck"]])

    def test_same_capability_as_an_active_job_is_held_back_and_stays_ready(self):
        self.cli.jobs["manual"] = job("manual", "c0")
        self.s.work.merge(
            [node("a", "c0", priority=9), node("b", "c1", priority=5), node("c", "c1", priority=1)],
            CAPS,
            self.cli.validate_requests,
            1,
        )
        self.cli.run_status = "waiting"
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual([c[c.index("--capability") + 1] for c in self.requested()], ["c1"])
        self.s._snapshot()
        self.assertEqual([q["id"] for q in self.s.work.ready_nodes()], ["a", "c"])
        self.assertEqual(sorted(self.cli.ran[0]), sorted(["manual", self.s.work.data["nodes"][1]["job_key"]]))

    def test_retryable_admission_refusal_keeps_node_ready_but_other_refusals_block(self):
        self.s.work.merge([node("a", "c0")], CAPS, self.cli.validate_requests, 1)
        self.cli.fail_request = "another job must finish or pause first: at most 4 jobs may be active"
        self.assertFalse(self.s._run_authorized(self.s._snapshot()))
        a = self.s.work.data["nodes"][0]
        self.assertEqual(a["status"], "ready")
        self.assertNotIn("dispatch_blocked", a)
        self.cli.fail_request = "native prerequisite unavailable"
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(a["status"], "blocked")
        self.assertTrue(a["dispatch_blocked"])
        self.cli.fail_request = None
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len(self.requested()), 2)

    def test_wait_decision_does_not_stop_dispatch_of_ready_nodes_or_running_jobs(self):
        self.cli.jobs["running"] = job("running", "c0")
        self.s.work.merge([node("ready", "c1")], CAPS, self.cli.validate_requests, 1)
        self.cli.run_status = "waiting"
        self.s._apply(decision("wait"), self.s._snapshot())
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual(len(self.requested()), 1)
        self.assertEqual(sorted(self.cli.ran[0]), sorted(["running", self.s.work.data["nodes"][0]["job_key"]]))

    def test_several_active_jobs_continue_is_noop_and_untargeted_pause_must_name_its_target(self):
        self.cli.jobs = {"a": job("a", "c0"), "b": job("b", "c1")}
        context = self.s._snapshot()
        self.s._apply(decision("continue"), context)
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            self.s._apply(decision("pause"), context)
        self.assertFalse(any(c[0] in ("pause", "resume", "retry") for c in self.cli.calls))
        self.s._apply(decision("pause", "c1"), context)
        self.assertEqual(self.cli.jobs["b"]["status"], "paused")
        self.assertEqual(self.cli.jobs["a"]["status"], "waiting")

    def test_request_is_queued_while_other_jobs_are_active(self):
        self.cli.jobs["a"] = job("a", "c0")
        self.cli.run_status = "waiting"
        self.s._apply(decision("request", "c1"), self.s._snapshot())
        self.assertTrue(self.s._run_authorized(self.s._snapshot()))
        self.assertEqual([c[c.index("--capability") + 1] for c in self.requested()], ["c1"])
        self.assertEqual(len(self.cli.ran[0]), 2)

    def test_state_lists_every_active_job_and_not_finished_history(self):
        for i in range(14):
            self.cli.jobs["done" + str(i)] = job("done" + str(i), "c0", "succeeded", created_at=2000 + i)
        for i in range(3):
            self.cli.jobs["old" + str(i)] = job("old" + str(i), "c" + str(i + 1), created_at=i)
        self.s._snapshot()
        self.assertEqual(self.s.state["active_job_keys"], ["old0", "old1", "old2"])


def events(root, kind=None):
    path = root / "logs/controller-events.jsonl"
    rows = [json.loads(q) for q in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [q for q in rows if kind is None or q["event"] == kind]


class IncidentTriggerTests(Fixture):
    """The model is consulted only for open incidents, once per incident per retry window."""

    def blocked(self, name="b", **extra):
        self.s.work.merge([node(name)], CAPS, self.cli.validate_requests, 1)
        q = next(q for q in self.s.work.data["nodes"] if q["id"] == name)
        self.cli.jobs[q["job_key"]] = job(q["job_key"], status="blocked", reason="coal_bank_required", **extra)
        return q

    def check(self):
        context = self.s._snapshot()
        inputs, roadmap = self.s._check_incidents(context)
        return context, inputs, roadmap

    def consult(self, result):
        """Start the due consultation, let the fake model answer and accept the answer."""
        self.client.result = result
        context, inputs, roadmap = self.check()
        incident = self.s._incident_due()
        self.assertIsNotNone(incident)
        self.s._start_decision(context, incident, self.s._document(incident, inputs, roadmap))
        for _ in range(500):
            if self.s._poll_decision():
                return incident
            time.sleep(0.01)
        self.fail("fake model did not answer")

    def test_running_work_without_incident_never_calls_the_model(self):
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        self.cli.run_status = "waiting"
        for _ in range(5):
            self.s._cycle()
            self.clock.advance(120)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.s.incidents.open, {})

    def test_blocked_job_opens_after_grace_and_is_consulted_once_per_retry_window(self):
        target = self.blocked()
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.assertIsNone(self.s._incident_due())
        self.clock.advance(61)
        incident = self.consult(dict(decision("wait"), reason="coal bank must finish first"))
        self.assertEqual((incident.kind, incident.subject), ("blocked_job", target["job_key"]))
        self.assertEqual(len(self.client.calls), 1)
        sent = self.client.calls[0]
        self.assertEqual(sent["incident"]["kind"], "blocked_job")
        self.assertIn("retry", [r["remedy"] for r in sent["remedies"]])
        row = events(self.root, "incident_decision")[0]
        self.assertEqual((row["remedy"], row["accepted"], row["input_tokens"]), ("wait", True, 100))
        self.assertEqual(row["cached_input_tokens"], 40)
        self.assertIn("latency_seconds", row)
        self.clock.advance(299)
        self.check()
        self.assertIsNone(self.s._incident_due())
        self.clock.advance(2)
        self.check()
        self.assertEqual(self.s._incident_due().id, incident.id)
        recorded = (self.root / "logs/incidents.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(json.loads(recorded[0])["document"], sent)

    def test_most_severe_incident_first_and_minimum_interval_between_calls(self):
        self.blocked()
        self.check()
        self.clock.advance(61)
        self.check()
        self.assertEqual({q.kind for q in self.s.incidents.open.values()}, {"empty_queue", "blocked_job"})
        first = self.consult(dict(decision("wait"), reason="nothing safe"))
        self.assertEqual(first.kind, "empty_queue")
        self.check()
        self.assertIsNone(self.s._incident_due())
        self.clock.advance(60)
        self.check()
        self.assertEqual(self.s._incident_due().kind, "blocked_job")

    def test_decision_outside_the_menu_is_rejected_and_logged_once_per_window_without_backoff(self):
        self.blocked()
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.clock.advance(61)
        self.consult(decision("request", "power"))
        self.assertEqual(self.s.errors, 0)
        self.assertEqual(self.s.backoff_until, 0)
        self.assertFalse(any(c[0] in ("request", "retry") for c in self.cli.calls))
        incident = next(iter(self.s.incidents.open.values()))
        self.s._reject(incident, "again")
        rejected = events(self.root, "decision_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertIn("request is not on the menu", rejected[0]["reason"])
        self.assertFalse(events(self.root, "incident_decision")[0]["accepted"])

    def test_retry_remedy_runs_on_the_menu_target_and_the_outcome_is_resolved(self):
        target = self.blocked()
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.clock.advance(61)
        self.consult(dict(decision("retry"), target_task_id="b"))
        self.assertEqual([c for c in self.cli.calls if c[0] == "retry"], [["retry", "--key", target["job_key"]]])
        self.clock.advance(5)
        self.check()
        outcome = events(self.root, "incident_outcome")
        self.assertEqual(
            [(q["outcome"], q["remedy"], q["incident_kind"]) for q in outcome], [("resolved", "retry", "blocked_job")]
        )
        self.assertEqual(outcome[0]["seconds"], 5)

    def test_unknown_outcome_job_has_no_retry_remedy_and_a_retry_is_rejected(self):
        self.blocked(pending_action={"type": "build"})
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.clock.advance(61)
        incident = self.consult(dict(decision("retry"), target_task_id="b"))
        self.assertNotIn("retry", [r.kind for r in incident.remedies])
        self.assertFalse(any(c[0] == "retry" for c in self.cli.calls))
        self.assertEqual(len(events(self.root, "decision_rejected")), 1)

    def test_incident_closed_while_the_model_thinks_is_a_stale_decision(self):
        target = self.blocked()
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.clock.advance(61)
        context, inputs, roadmap = self.check()
        incident = self.s._incident_due()
        self.cli.jobs[target["job_key"]]["status"] = "waiting"
        self.check()
        results = queue.Queue()
        results.put((True, dict(decision("retry"), target_task_id="b")))
        self.s.pending_decision = dict(result=results, context=context, started_at=self.clock(), incident=incident)
        self.assertTrue(self.s._poll_decision())
        self.assertEqual(len(events(self.root, "stale_decision")), 1)
        self.assertFalse(any(c[0] == "retry" for c in self.cli.calls))

    def test_open_incident_expires_after_outcome_minutes_and_reopens_as_a_new_episode(self):
        self.blocked()
        self.s.work.merge([node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        self.check()
        self.clock.advance(61)
        self.check()
        self.clock.advance(600)
        self.check()
        self.assertEqual([q["outcome"] for q in events(self.root, "incident_outcome")], ["expired"])
        self.assertEqual(len(events(self.root, "incident_opened")), 2)
        self.assertEqual(len(self.s.incidents.open), 1)

    def test_recent_intents_read_only_a_bounded_tail(self):
        path = self.root / "job-events.jsonl"
        rows = [dict(key="k", event="action_intent", time=i, action={"type": "mine"}) for i in range(5000)]
        path.write_text("".join(json.dumps(q) + "\n" for q in rows), encoding="utf-8")
        intents = self.s._recent_intents()
        self.assertEqual(len(intents), 100)
        self.assertEqual(intents[-1]["time"], 4999)


class PlannerNoneTests(Fixture):
    def setUp(self):
        super().setUp()
        self.s = Supervisor(
            self.root, None, cli=self.cli, planner="none", clock=self.clock, sleep=self.clock.advance, max_seconds=200
        )

    def test_constructor_requires_a_client_exactly_for_luna(self):
        for planner, client in (("luna", None), ("none", self.client), ("other", None)):
            with self.subTest(planner=planner), self.assertRaises(ValueError):
                Supervisor(self.root, client, cli=self.cli, planner=planner, clock=self.clock)
        self.assertEqual((self.s.state["planner"], self.s.state["model"]), ("none", None))

    def test_dispatch_autopilot_and_auto_retry_run_without_any_model_call(self):
        rows = [node("auto_power", "power")]
        with patch.object(Supervisor, "_load_autopilot", return_value=lambda *a, **k: deepcopy(rows)):
            self.cli.jobs["s"] = job("s", status="succeeded")
            self.cli.jobs["stuck"] = job("stuck", "c9", status="blocked", reason="coal_bank_required")
            self.s._auto_retries = {"stuck": {"successes": 0, "count": 0}}
            self.cli.run_status = "waiting"
            self.assertTrue(self.s._cycle())
        self.assertIn(["retry", "--key", "stuck"], self.cli.calls)
        requested = [c[c.index("--capability") + 1] for c in self.cli.calls if c[0] == "request"]
        self.assertEqual(requested, ["power"])
        self.assertIsNone(self.s.pending_decision)
        self.assertEqual(self.client.calls, [])

    def test_unresolved_situation_is_recorded_as_an_incident_handled_by_none(self):
        self.s._check_incidents(self.s._snapshot())
        self.clock.advance(61)
        for _ in range(3):
            self.s._cycle()
            self.clock.advance(10)
        opened = events(self.root, "incident_opened")
        self.assertEqual([(q["incident_kind"], q["handled_by"]) for q in opened], [("empty_queue", "none")])
        self.assertIsNone(self.s._incident_due())
        self.assertFalse(any(q["event"] == "incident_decision" for q in events(self.root)))
        recorded = (self.root / "logs/incidents.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(json.loads(recorded[0])["handled_by"], "none")
        self.s.work.merge([node("a")], CAPS, self.cli.validate_requests, 1)
        self.s._check_incidents(self.s._snapshot())
        self.assertEqual([q["outcome"] for q in events(self.root, "incident_outcome")], ["resolved"])

    def test_main_with_planner_none_never_imports_the_model_client(self):
        from factorio_agent import supervisor as module

        seen = {}

        def run(instance):
            seen.update(planner=instance.planner, client=instance.client)
            return dict(status="safe_stopped", world_id="w", saved=True, stop_request_id="x")

        with (
            patch.dict(sys.modules, {"factorio_agent.luna_client": None}),
            patch.object(Supervisor, "run", run),
            patch("builtins.print"),
        ):
            module.main(["--runtime", str(self.root), "--planner", "none", "--bridge", "cli"])
        self.assertEqual(seen, dict(planner="none", client=None))


class AutopilotTests(Fixture):
    def install(self, rows):
        calls = []

        def planner(jobs, queue_nodes, observation, capabilities, limit=3):
            calls.append(dict(jobs=jobs, nodes=queue_nodes, tick=observation["tick"], limit=limit))
            return deepcopy(rows)

        patcher = patch.object(Supervisor, "_load_autopilot", return_value=planner)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_low_queue_merges_autopilot_nodes_through_queue_validation(self):
        calls = self.install([node("auto_starter", priority=50), node("not_auto"), node("auto_bad", "unknown")])
        self.assertFalse(self.s._autopilot(self.s._snapshot()))
        events = [json.loads(q)["event"] for q in self.s.event_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("autopilot_rejected", events)
        self.assertEqual(self.s.work.data["nodes"], [])
        self.assertEqual(self.s.errors, 0)
        calls = self.install([node("auto_starter", priority=50), node("not_auto")])
        self.assertTrue(self.s._autopilot(self.s._snapshot()))
        self.assertEqual([q["id"] for q in self.s.work.data["nodes"]], ["auto_starter"])
        self.assertEqual(self.s.work.ready()["id"], "auto_starter")
        self.assertEqual(calls[0]["limit"], 3)

    def test_full_queue_does_not_ask_autopilot(self):
        calls = self.install([node("auto_starter")])
        self.s.work.merge([node("a"), node("b", "power", ["a"])], CAPS, self.cli.validate_requests, 1)
        self.assertFalse(self.s._autopilot(self.s._snapshot()))
        self.assertEqual(calls, [])

    def test_missing_progression_module_or_function_means_no_autopilot(self):
        with patch.dict(sys.modules, {"factorio_agent.progression": None}):
            self.assertIsNone(REAL_LOAD_AUTOPILOT(self.s))
        with patch.dict(sys.modules, {"factorio_agent.progression": types.ModuleType("progression")}):
            self.assertIsNone(REAL_LOAD_AUTOPILOT(self.s))

    def test_luna_node_with_same_capability_and_parameters_is_skipped_and_its_dependents_redirected(self):
        self.install([node("auto_starter", priority=50)])
        self.s._autopilot(self.s._snapshot())
        self.s._apply(decision(nodes=[node("starter"), node("power", "power", ["starter"])]), self.s._snapshot())
        ids = {q["id"]: q for q in self.s.work.data["nodes"]}
        self.assertEqual(sorted(ids), ["auto_starter", "power"])
        self.assertEqual(ids["power"]["depends_on"], ["auto_starter"])
        self.s._run_authorized(self.s._snapshot())
        self.assertEqual(len([c for c in self.cli.calls if c[0] == "request"]), 1)
        events = [json.loads(q) for q in self.s.event_path.read_text(encoding="utf-8").splitlines()]
        skipped = [q for q in events if q["event"] == "queue_duplicates_skipped"]
        self.assertEqual(skipped[0]["task_ids"], ["starter"])

    def test_completed_duplicate_is_skipped_but_a_blocked_one_can_be_replaced(self):
        self.s.work.merge([node("auto_starter")], CAPS, self.cli.validate_requests, 1)
        key = self.s.work.data["nodes"][0]["job_key"]
        self.cli.jobs[key] = job(key, status="succeeded")
        self.s._apply(decision(nodes=[node("again")]), self.s._snapshot())
        self.assertEqual([q["id"] for q in self.s.work.data["nodes"]], ["auto_starter"])
        self.cli.jobs[key]["status"] = "blocked"
        self.s._apply(decision(nodes=[node("again")]), self.s._snapshot())
        self.assertEqual([q["id"] for q in self.s.work.data["nodes"]], ["auto_starter", "again"])

    def test_cycle_dispatches_autopilot_work_without_consulting_the_model(self):
        self.install([node("auto_starter")])
        self.cli.run_status = "waiting"
        self.assertTrue(self.s._cycle())
        self.assertEqual(self.client.calls, [])
        self.assertIsNone(self.s.pending_decision)
        self.assertEqual(len(self.cli.ran), 1)


class SafeStopTests(Fixture):
    def test_safe_stop_pauses_every_active_job_and_restart_resumes_all_of_them(self):
        self.cli.jobs = {k: job(k, "c" + k) for k in ("1", "2", "3")}
        self.cli.jobs["done"] = job("done", status="succeeded")
        self.s.stop_marker = dict(request_id="safe", world_id="w")
        result = self.s.safe_stop()
        self.assertTrue(result["saved"])
        commands = [c for c in self.cli.calls if c[0] in ("pause", "checkpoint")]
        self.assertEqual(sorted(c[2] for c in commands[:3]), ["1", "2", "3"])
        self.assertEqual(commands[-1], ["checkpoint"])
        self.assertEqual(len(result["pause_receipts"]), 3)
        self.assertEqual(result["pause_receipt"], result["pause_receipts"][0])
        self.assertEqual(result["active_job_keys"], [])
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        self.assertEqual(sorted(restarted.resume_job_keys), ["1", "2", "3"])
        self.assertTrue(restarted._resume_known(restarted._snapshot()))
        self.assertEqual(sorted(c[2] for c in self.cli.calls if c[0] == "resume"), ["1", "2", "3"])
        self.assertTrue(all(self.cli.jobs[k]["status"] == "waiting" for k in ("1", "2", "3")))
        self.assertEqual(restarted.state["resume_job_keys"], [])
        self.assertFalse(any(c[0] == "request" for c in self.cli.calls))

    def test_missing_saved_job_blocks_resume_and_unresumed_keys_survive_another_stop(self):
        self.cli.jobs = {"1": job("1", status="paused")}
        path = self.root / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        receipts = [{"key": "1"}, {"key": "gone"}]
        path.write_text(json.dumps(dict(status="safe_stopped", saved=True, world_id="w", pause_receipts=receipts)))
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        with self.assertRaisesRegex(ValueError, "identity missing"):
            restarted._resume_known(restarted._snapshot())
        self.assertEqual(restarted.resume_job_keys, ["gone"])
        restarted.resume_job_keys = ["1"]
        self.cli.jobs["1"]["status"] = "paused"
        restarted.stop_marker = dict(request_id="again", world_id="w")
        result = restarted.safe_stop()
        self.assertEqual([q["key"] for q in result["pause_receipts"]], ["1"])


class LegacyQueueTests(Fixture):
    def test_queue_written_with_scheduler_pause_fields_loads_without_them(self):
        self.s.work.merge([node("low"), node("high", "power")], CAPS, self.cli.validate_requests, 1)
        data = json.loads(self.s.work.path.read_text(encoding="utf-8"))
        data["nodes"][0].update(
            status="scheduler_paused",
            scheduler_pause={"phase": "paused", "higher_task_id": "high"},
            scheduler_pause_history=[{"phase": "released"}],
        )
        self.s.work.path.write_text(json.dumps(data), encoding="utf-8")
        loaded = WorkQueue(self.s.work.path, "w")
        low = loaded.data["nodes"][0]
        self.assertNotIn("scheduler_pause", low)
        self.assertNotIn("scheduler_pause_history", low)
        self.assertEqual(low["status"], "blocked")
        loaded.save()
        self.assertNotIn("scheduler_pause", loaded.path.read_text(encoding="utf-8"))
        restarted = Supervisor(self.root, self.client, cli=self.cli, clock=self.clock, sleep=self.clock.advance)
        restarted._snapshot()
        self.assertNotIn("scheduler_pause", json.dumps(restarted.work.data["nodes"]))


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
