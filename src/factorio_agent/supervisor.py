"""Framework-sequenced durable DAG; up to four interleaved jobs per slice; the model only handles incidents."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from importlib import import_module
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from factorio_agent.core.deterministic_character import _identity
from factorio_agent.core.deterministic_state import RunLock, _atomic_json
from factorio_agent.errors import CLIError
from factorio_agent.jobs import MAX_ACTIVE_JOBS, admission_lane
from factorio_agent.early_start import admission_requires, parked
from factorio_agent import incidents as incident_rules
from factorio_agent import actor_client


STATE_FILE = Path("logs/controller-state.json")
STOP_FILE = "supervisor-stop.json"
QUEUE_FILE = "work_queue.json"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SOURCE_ROOT.parent
ACTIVE = {"pending", "running", "waiting"}
TERMINAL = {"succeeded", "blocked", "failed"}
OPERATIONS = incident_rules.OPERATIONS
AUTO_RETRY_LIMIT = 3
AUTO_RETRY_STALL_SECONDS = 120
QUEUED = ("pending", "ready")
# Existing nodes in these states absorb a new node with the same capability and parameters.
DEDUPE_STATUSES = {"pending", "ready", "running", "completed"}
# Submit/control refusals that clear by themselves once another job finishes or a fresh observation exists.
RETRYABLE_ADMISSION = ("is already active", "jobs may be active", "observe again")
PLANNERS = ("luna", "none")
INCIDENT_LOG = Path("logs/incidents.jsonl")
# The incident log stops growing past this size; an event records the cut.
INCIDENT_LOG_BYTES = 5_000_000
# Bounded tail of job-events.jsonl read for the recent action sample.
INTENT_TAIL_BYTES = 262_144
LOW_QUEUE = 2
AUTOPILOT_LIMIT = 3
FORBIDDEN = {
    "x",
    "y",
    "position",
    "positions",
    "coordinates",
    "area",
    "entities",
    "entity",
    "lua",
    "rcon",
    "shell",
    "command",
    "cmd",
    "script",
    "code",
}
BLOCKER_FIELDS = {
    "reason",
    "last_failure",
    "failure_category",
    "stage",
    "phase",
    "item",
    "resource",
    "product",
    "technology",
    "recipe",
    "required_technology",
    "missing_technologies",
    "required_technologies",
    "required_recipe",
    "missing_recipes",
    "required_recipes",
    "have",
    "need",
    "available",
    "required",
    "missing",
    "count",
    "capacity",
    "supply_per_minute",
    "required_per_minute",
    "available_per_minute",
    "missing_items",
    "required_items",
    "requirements",
    "boiler_fuel",
    "drill_fuel",
    "drill_powered",
    "water",
    "steam",
    "connected_engines",
    "energized_engines",
    "coal_on_belts",
    "coal_mining_per_minute",
    "generation_kw",
    "autonomous_wait",
    "background_work_safe",
}
PRIVATE_FIELD = re.compile(
    r"password|secret|credential|token|auth|api.?key|(?:^|_)(?:path|file|env|position|coordinates|area|entities|entity|lua|rcon|shell|command|cmd|script|code)(?:_|$)",
    re.I,
)


def _diagnostic_text(value):
    if not isinstance(value, str):
        return None
    # Diagnostic strings are descriptions, never a channel for native payloads,
    # coordinates, file paths or credentials embedded in an otherwise safe key.
    if re.search(
        r"password|secret|credential|authorization|bearer|api.?key|\btoken\b|[A-Za-z]:[\\/]|(?:^|\s)/[A-Za-z]|[{}\[\]]|(?:\bx|\by)\s*[:=]|[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d|\b(?:rcon|powershell|shell|lua|cmd)\b",
        value,
        re.I,
    ):
        return "<redacted>"
    return value[:160]


def blocker_summary(job):
    """Small semantic diagnosis; arbitrary executor payloads stay out of prompts."""
    remaining = [20]

    def compact(value, depth=0, named_counts=False):
        if depth > 3 or remaining[0] <= 0:
            return None
        if isinstance(value, dict):
            result = {}
            details = []
            for key, item in list(value.items())[:48]:
                if not isinstance(key, str) or key.lower() in FORBIDDEN or PRIVATE_FIELD.search(key):
                    continue
                key = key.lower()
                if key in BLOCKER_FIELDS:
                    clean = compact(item, depth + 1, key in {"missing_items", "required_items", "requirements"})
                    if clean is not None:
                        result[key] = clean
                elif (
                    named_counts
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", key)
                    and type(item) in (int, float)
                    and math.isfinite(item)
                ):
                    remaining[0] -= 1
                    result[key] = item
                elif isinstance(item, (dict, list)) and len(details) < 3:
                    clean = compact(item, depth + 1)
                    if clean:
                        details.append(clean)
                if remaining[0] <= 0 or len(result) >= 8:
                    break
            if details:
                result["details"] = details
            return result or None
        if isinstance(value, list):
            rows = [compact(v, depth + 1) for v in value[:4]]
            return [v for v in rows if v is not None] or None
        if isinstance(value, str):
            remaining[0] -= 1
            return _diagnostic_text(value)
        if type(value) in (int, float, bool) and math.isfinite(value):
            remaining[0] -= 1
            return value
        return None

    return {
        key: clean for key in ("reason", "last_failure", "evidence") if (clean := compact(job.get(key))) is not None
    }


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _same_actor(a, b):
    return {k: v for k, v in a.items() if k != "native_repair_pending"} == {
        k: v for k, v in b.items() if k != "native_repair_pending"
    }


def _no_raw(value):
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k.lower() in FORBIDDEN for k in value):
            raise ValueError("raw coordinates or commands are forbidden")
        for q in value.values():
            _no_raw(q)
    elif isinstance(value, list):
        for q in value:
            _no_raw(q)
    elif type(value) not in (str, int, float, bool, type(None)) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("finite JSON parameters required")


def validate_decision(value):
    if not isinstance(value, dict) or set(value) - {
        "operation",
        "capability",
        "parameters",
        "reason",
        "queue",
        "target_task_id",
    }:
        raise ValueError("decision must contain only the structured task schema")
    if (
        value.get("operation") not in OPERATIONS
        or not isinstance(value.get("parameters"), dict)
        or not isinstance(value.get("reason"), str)
        or len(value["reason"]) > 2000
    ):
        raise ValueError("invalid operation, parameters or reason")
    cap = value.get("capability")
    if cap is not None and (not isinstance(cap, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", cap)):
        raise ValueError("invalid capability name")
    if value["operation"] == "request" and cap is None:
        raise ValueError("request needs a capability")
    target = value.get("target_task_id")
    if target is not None and (
        value["operation"] not in ("continue", "retry", "pause")
        or not isinstance(target, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", target)
    ):
        raise ValueError("target_task_id requires a stable queue ID and a control operation")
    nodes = value.get("queue", [])
    if not isinstance(nodes, list) or len(nodes) > 64:
        raise ValueError("queue decision accepts at most64 nodes")
    _no_raw(value["parameters"])
    return dict(value, queue=nodes)


def stable_job_key(world, node):
    return "lunaq_" + _hash([world, node["id"], node["capability"], node["parameters"]])[:48]


def _unknown(job):
    return job.get("pending_action") is not None or "outcome_unknown" in str(job.get("reason", ""))


def _node_identity(node):
    return _hash([node["capability"], node["parameters"]])


def _validated_node(node, capabilities, seen):
    """One typed queue node; DAG edges are a set, so order and repeats carry no meaning."""
    required = {"id", "capability", "parameters", "priority", "depends_on", "reason"}
    if not isinstance(node, dict) or set(node) != required:
        raise ValueError("queue node requires exactly id/capability/parameters/priority/depends_on/reason")
    name = node["id"]
    deps = node["depends_on"]
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
        or name in seen
        or node["capability"] not in capabilities
        or not isinstance(node["parameters"], dict)
        or type(node["priority"]) is not int
        or not -10000 <= node["priority"] <= 10000
        or not isinstance(deps, list)
        or any(not isinstance(d, str) for d in deps)
        or name in deps
        or not isinstance(node["reason"], str)
        or len(node["reason"]) > 2000
    ):
        raise ValueError("invalid unique typed queue node")
    _no_raw(node["parameters"])
    seen.add(name)
    return dict(deepcopy(node), depends_on=sorted(set(deps)))


def _lane(node) -> str:
    """Admission lane of a queue node (see ``jobs.admission_lane``)."""
    return admission_lane(node["capability"], node.get("parameters"))


class WorkQueue:
    """World-bound append/merge queue; submitted task identity never changes.

    Duplicate rule: a NEW node id whose capability and exact parameters equal an
    existing pending, ready, running or completed node is skipped, and incoming
    dependencies on it are redirected to that existing node. This keeps a Luna
    plan from re-running work the autopilot already queued (and vice versa).
    Blocked nodes do not absorb duplicates, so a fresh id can still replace them.
    Duplicates inside one merge batch are kept: they are one planner's choice.
    """

    def __init__(self, path, world):
        self.path, self.world = Path(path), world
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else dict(schema_version=1, world_id=world, nodes=[])
        )
        if self.data.get("schema_version") != 1 or self.data.get("world_id") != world:
            raise ValueError("work queue world or format changed")
        if not isinstance(self.data.get("nodes"), list):
            raise ValueError("invalid persistent work queue")
        if len(self.data["nodes"]) > 512 or len({q["id"] for q in self.data["nodes"]}) != len(self.data["nodes"]):
            raise ValueError("invalid unique queue history")
        for node in self.data["nodes"]:
            if node.get("job_key") != stable_job_key(world, node):
                raise ValueError("persistent queue job identity changed")
            _no_raw(node["parameters"])
            self._strip_legacy(node)

    @staticmethod
    def _strip_legacy(node):
        """Older versions stored a scheduler-owned pause protocol on nodes; it no longer exists."""
        node.pop("scheduler_pause", None)
        node.pop("scheduler_pause_history", None)
        if node.get("status") == "scheduler_paused":
            node["status"] = "blocked"

    def save(self):
        _atomic_json(self.path, self.data)

    def merge(self, nodes, capabilities, validator, tick, *, native_gates=None):
        """Validate and persist nodes atomically; return the skipped duplicate ids."""
        result = deepcopy(self.data)
        index = {q["id"]: q for q in result["nodes"]}
        seen = set()
        rows = [_validated_node(node, capabilities, seen) for node in nodes]
        aliases = self._duplicate_aliases(rows, index)
        incoming = set()
        for row in rows:
            if row["id"] in aliases:
                continue
            row["depends_on"] = sorted({aliases.get(d, d) for d in row["depends_on"]})
            if row["id"] in row["depends_on"]:
                raise ValueError("invalid unique typed queue node")
            incoming.add(row["id"])
            old = index.get(row["id"])
            if old:
                self._update_existing(old, row)
            else:
                new = dict(
                    row,
                    status="pending",
                    job_key=stable_job_key(self.world, row),
                    created_at=time.time(),
                    paused=False,
                )
                result["nodes"].append(new)
                index[row["id"]] = new
        if len(result["nodes"]) > 512:
            raise ValueError("persistent queue exceeds512 nodes; explicit archival needed")
        self._reject_cycles(index)
        from .capability_gates import reject_native_recipe_cycles

        reject_native_recipe_cycles(result["nodes"], native_gates, incoming)
        validator(
            [
                dict(
                    key=q["job_key"],
                    world_id=self.world,
                    observed_tick=tick,
                    capability=q["capability"],
                    parameters=q["parameters"],
                )
                for q in result["nodes"]
                if q["id"] in incoming
            ]
        )
        self.data = result
        self.save()
        return sorted(aliases)

    @staticmethod
    def _duplicate_aliases(rows, index):
        existing = {}
        for q in index.values():
            if q["status"] in DEDUPE_STATUSES:
                existing.setdefault(_node_identity(q), q["id"])
        return {
            row["id"]: existing[_node_identity(row)]
            for row in rows
            if row["id"] not in index and _node_identity(row) in existing
        }

    @staticmethod
    def _update_existing(old, row):
        """Priority/reason may change; identity never, and started dependencies never."""
        if old["capability"] != row["capability"] or old["parameters"] != row["parameters"]:
            raise ValueError("queue task identity is immutable; use a new node id")
        deps = row["depends_on"]
        if old["status"] in ("running", "completed") or old.get("submitted"):
            added = set(deps) - set(old["depends_on"])
            removed = set(old["depends_on"]) - set(deps)
            if added or removed:
                changes = (
                    "; added="
                    + (" ".join(sorted(added)[:4]) or "none")
                    + "; removed="
                    + (" ".join(sorted(removed)[:4]) or "none")
                )
                raise ValueError("cannot change dependencies after execution starts: " + row["id"] + changes)
            deps = list(old["depends_on"])
        old.update(priority=row["priority"], depends_on=list(deps), reason=row["reason"])
        if old["status"] not in ("running", "completed"):
            old.pop("dispatch_blocked", None)

    @staticmethod
    def _reject_cycles(index):
        visiting = set()
        done = set()

        def visit(name):
            if name not in index:
                raise ValueError("unknown queue dependency: " + name)
            if name in visiting:
                raise ValueError("queue dependency cycle")
            if name in done:
                return
            visiting.add(name)
            for d in index[name]["depends_on"]:
                visit(d)
            visiting.remove(name)
            done.add(name)

        for name in index:
            visit(name)

    def refresh(self, jobs, capabilities):
        by_key = {q["key"]: q for q in jobs}
        completed = {q["capability"] for q in jobs if q["status"] == "succeeded"}
        nodes = {q["id"]: q for q in self.data["nodes"]}
        # First reconcile receipts, then calculate dependency readiness so the
        # insertion order of a valid DAG does not add an unnecessary poll delay.
        for q in nodes.values():
            job = by_key.get(q["job_key"])
            if job:
                if (
                    job.get("world_id") != self.world
                    or job.get("capability") != q["capability"]
                    or job.get("parameters") != q["parameters"]
                ):
                    raise ValueError("queue/native job identity differs")
                q["submitted"] = True
                q["last_job_status"] = job["status"]
                q["last_reason"] = job.get("reason", "")
                q["last_blocker"] = blocker_summary(job) if job["status"] in {"waiting", "blocked", "failed"} else {}
                q["status"] = (
                    "completed" if job["status"] == "succeeded" else "running" if job["status"] in ACTIVE else "blocked"
                )
            elif q.get("submitted"):
                q.update(status="blocked", last_reason="submitted_job_receipt_missing")
            elif q.get("status") not in ("completed", "blocked"):
                q["status"] = "pending"
        for q in nodes.values():
            if q["job_key"] in by_key or q["status"] == "completed":
                continue
            if q.get("dispatch_blocked"):
                q["status"] = "blocked"
                continue
            if q["capability"] not in capabilities:
                q.update(status="blocked", last_reason="capability_no_longer_available")
                continue
            missing = [d for d in q["depends_on"] if nodes[d]["status"] != "completed"]
            # early_start.admission_requires: an early-start capability is admitted on its early requires.
            prerequisites = [p for p in admission_requires(capabilities, q["capability"]) if p not in completed]
            q["missing_dependencies"] = missing
            q["missing_prerequisites"] = prerequisites
            q["status"] = "ready" if not missing and not prerequisites and not q.get("paused") else "pending"
        self.save()

    def ready_nodes(self):
        """Ready nodes in dispatch order: highest priority, then oldest, then id."""
        rows = [q for q in self.data["nodes"] if q["status"] == "ready"]
        return sorted(rows, key=lambda q: (-q["priority"], q["created_at"], q["id"]))

    def ready(self):
        rows = self.ready_nodes()
        return rows[0] if rows else None

    def queued_count(self):
        return sum(q["status"] in QUEUED for q in self.data["nodes"])

    def node_for_job(self, key):
        return next((q for q in self.data["nodes"] if q["job_key"] == key), None)


class CLIBridge:
    """Fixed argv, never a shell; each CLI call imports current source anew."""

    def __init__(self, runtime, *, on_spawn=None, on_exit=None):
        self.runtime = Path(runtime).resolve()
        self.on_spawn = on_spawn
        self.on_exit = on_exit

    def _execute(self, args, *, payload=None, stop_requested=None, on_stop=None):
        env = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(Path(sys.executable).resolve()), "-B", "-m", *args],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        if self.on_spawn:
            self.on_spawn(process, args)
        sent = False
        stopped = False
        while True:
            if stop_requested and stop_requested() and not stopped:
                stopped = True
                if on_stop:
                    try:
                        on_stop()
                    except Exception:
                        # Never abandon a possibly mutating child at this
                        # boundary. Let it finish its safe slice, then the
                        # supervisor will pause/save or report a failed ACK.
                        pass
            try:
                stdout, stderr = process.communicate(input=payload if not sent else None, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                sent = True
        if self.on_exit:
            self.on_exit(process, args, stdout, stderr)
        # A CLI receipt is exactly one JSON object. Worker streaming is never
        # used; run is one bounded slice with a final durable job receipt.
        try:
            result = json.loads(stdout.strip())
        except ValueError as exc:
            raise CLIError("invalid typed CLI receipt: " + stderr[-800:]) from exc
        if process.returncode or not isinstance(result, dict) or result.get("status") == "error":
            raise CLIError(
                str(result.get("reason", "typed CLI failure")) if isinstance(result, dict) else "invalid CLI object"
            )
        return result

    def invoke(self, args, *, stop_requested=None, on_stop=None):
        return self._execute(
            ["factorio_agent", "--runtime", str(self.runtime), *args], stop_requested=stop_requested, on_stop=on_stop
        )

    def validate_requests(self, rows):
        if not rows:
            return
        result = self._execute(
            ["factorio_agent.supervisor", "--validate-requests"], payload=json.dumps(rows, allow_nan=False)
        )
        if result.get("ok") is not True:
            raise ValueError("typed request batch rejected")


# Client-free test mode: wall seconds between player-presence emulation updates (actor_client).
PRESENCE_SECONDS = 5


class ActorDeadWithoutPlayer(ValueError):
    """The detached actor died: no player can respawn it (actor_client)."""


def _detached_binding(value, old, owner):
    """The actor recorded at the client detach, with the identity checks of the pinned-owner case."""
    record = value.get("detached_record") or {}
    if not old.get("valid"):
        raise ActorDeadWithoutPlayer(actor_client.DEAD_WITHOUT_PLAYER)
    if (
        value.get("detached") is not True
        or type(old.get("unit_number")) is not int
        or old["unit_number"] < 1
        or value.get("old_unit") != old["unit_number"]
        or record.get("unit_number") != old["unit_number"]
        or record.get("world_id") != value.get("world_id")
        or old.get("player_index") is not None
        or record.get("player_index") != value.get("pinned_owner_index")
        or (
            # client-free (mode none) never had a player; until_lab_trigger keeps the offline owner
            value.get("pinned_owner_index") is not None
            and (owner.get("index") != value.get("pinned_owner_index") or not owner.get("name"))
        )
        or old.get("surface_index") != value.get("surface_index")
        or record.get("surface_index") != old.get("surface_index")
        or record.get("force_index") != old.get("force_index")
        or not old.get("force_index")
    ):
        raise ValueError("current framework actor is not the exact detached actor recorded at the client detach")
    return dict(
        unit_number=old["unit_number"],
        owner_index=owner.get("index"),
        owner_name=owner.get("name"),
        surface_index=old["surface_index"],
        force_index=old["force_index"],
        native_repair_pending=bool(value.get("native_repair_pending")),
        detached=True,
        client_mode=record.get("mode") or actor_client.UNTIL_LAB_TRIGGER,
    )


def actor_binding(value, world):
    if value.get("ok") is not True or value.get("world_id") != world:
        raise ValueError("native actor/world inspection failed")
    old = value.get("old_pointer", {})
    if value.get("detached_record") is not None:
        return _detached_binding(value, old, value.get("owner") or {})
    successor = value.get("successor", {})
    owner = value.get("owner", {})
    if (
        not old.get("valid")
        or type(old.get("unit_number")) is not int
        or old["unit_number"] < 1
        or value.get("old_unit") != old["unit_number"]
        or not successor.get("valid")
        or successor.get("unit_number") != old["unit_number"]
        or owner.get("index") != value.get("pinned_owner_index")
        or not owner.get("name")
        or old.get("surface_index") != value.get("surface_index")
        or successor.get("surface_index") != old.get("surface_index")
        or successor.get("force_index") != old.get("force_index")
        or not old.get("force_index")
    ):
        raise ValueError("current framework actor is not the exact native pinned owner character")
    return dict(
        unit_number=old["unit_number"],
        owner_index=owner["index"],
        owner_name=owner["name"],
        surface_index=old["surface_index"],
        force_index=old["force_index"],
        native_repair_pending=bool(value.get("native_repair_pending")),
    )


def _active_keys(jobs):
    """Active job keys in the same order ``run --all`` advances them."""
    active = [j for j in jobs if j["status"] in ACTIVE]
    active.sort(key=lambda j: (-j.get("priority", 0), j.get("created_at", 0), j["key"]))
    return [j["key"] for j in active]


def _retryable_admission(reason):
    return any(text in reason for text in RETRYABLE_ADMISSION)


def _usage(value):
    """Model token usage as reported by the CLI: input, cached input and output tokens."""
    value = value if isinstance(value, dict) else {}
    return dict(
        input_tokens=value.get("input_tokens"),
        cached_input_tokens=value.get("cached_input_tokens"),
        output_tokens=value.get("output_tokens"),
    )


def _resume_keys(prior, world):
    """Job keys paused by the preceding successful safe stop of this world."""
    if prior.get("world_id") != world or prior.get("saved") is not True or prior.get("status") != "safe_stopped":
        return []
    receipts = prior.get("pause_receipts")
    if not isinstance(receipts, list):
        # Older controller states stored one pause receipt.
        single = prior.get("pause_receipt")
        receipts = [single] if isinstance(single, dict) else []
    return [r["key"] for r in receipts if isinstance(r, dict) and isinstance(r.get("key"), str)]


class Supervisor:
    """Durable queue dispatch sequenced by the framework; the model only handles incidents.

    Each cycle: snapshot -> resume jobs paused by the last safe stop -> merge
    autopilot nodes when the queue is low -> auto-retry blocked jobs after a
    success -> accept a finished incident decision -> update incidents and maybe
    consult the model -> submit ready nodes -> one ``run --all`` slice.

    Up to ``MAX_ACTIVE_JOBS`` jobs are active; the job layer interleaves them per
    action. The model never gates dispatch.

    Planner rule: ``planner="none"`` creates no model client and never starts a
    decision thread; incidents are still opened, logged (``handled_by: none``)
    and closed. ``planner="luna"`` consults the model only for an open incident
    not consulted within ``incident_retry_seconds`` (default 300), one incident
    per call, most severe first, never sooner than ``decision_interval`` seconds
    after the previous call and never during error backoff.
    """

    def __init__(
        self,
        runtime,
        client=None,
        *,
        cli=None,
        planner="luna",
        decision_interval=60,
        slice_seconds=25,
        max_seconds=None,
        incident_config=None,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.root = Path(runtime)
        if not self.root.is_absolute() or not self.root.is_dir():
            raise ValueError("supervisor requires an existing absolute runtime")
        if (
            not _positive(decision_interval)
            or not _positive(slice_seconds)
            or slice_seconds > 50
            or (max_seconds is not None and not _positive(max_seconds))
        ):
            raise ValueError("positive decision interval, slice<=50 and optional duration required")
        if planner not in PLANNERS:
            raise ValueError("planner must be one of " + ", ".join(PLANNERS))
        if (planner == "luna") != (client is not None):
            raise ValueError("planner luna needs a model client; planner none takes none")
        self.root = self.root.resolve()
        self.clock, self.sleep = clock, sleep
        self.client = client
        self.planner = planner
        self.decision_interval = decision_interval
        self.slice_seconds = slice_seconds
        self.max_seconds = max_seconds
        self.world = json.loads((self.root / "world-settings.json").read_text(encoding="utf-8"))["world_id"]
        if not isinstance(self.world, str) or not self.world:
            raise ValueError("installed world identity required")
        self.work = WorkQueue(self.root / QUEUE_FILE, self.world)
        self.state_path = self.root / STATE_FILE
        self.event_path = self.root / "logs/controller-events.jsonl"
        self.incident_path = self.root / INCIDENT_LOG
        prior = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        self.resume_job_keys = _resume_keys(prior, self.world)
        self.controller_id = uuid.uuid4().hex
        created = _identity(os.getpid())
        now = self.clock()
        self.state = dict(
            schema_version=1,
            controller_id=self.controller_id,
            pid=os.getpid(),
            creation_time=created,
            native_creation_time=created,
            process_creation_time=created / 10000000 - 11644473600 if created else now,
            started_at=now,
            runtime=str(self.root),
            source_root=str(SOURCE_ROOT),
            world_id=self.world,
            status="starting",
            state="running",
            active_job_key=None,
            active_job_keys=[],
            planner=planner,
            decision_interval=decision_interval,
            slice_seconds=slice_seconds,
            model="gpt-6-luna" if planner == "luna" else None,
            reasoning_effort="medium" if planner == "luna" else None,
            last_decision_at=None,
            decision_sequence=int(prior.get("decision_sequence", 0)),
            open_incidents=[],
            worker_stop_owned=False,
            saved=False,
            resume_job_keys=list(self.resume_job_keys),
        )
        self.cli = cli or CLIBridge(self.root, on_spawn=self._child_spawn, on_exit=self._child_exit)
        self.stop_marker = None
        self.stopping = False
        self.errors = 0
        self.backoff_until = 0
        self.last_decision_time = None
        self.pending_decision = None
        self.incidents = incident_rules.IncidentTracker(
            incident_config, summarize=blocker_summary, text=_diagnostic_text, started_at=now
        )
        self.incident_config = self.incidents.config
        self.documents = {}
        self.recorded = {}
        self.rejections = {}
        self.technology_order = []
        self.incident_log_full = False

    def _save(self, **updates):
        self.state.update(updates, updated_at=self.clock())
        _atomic_json(self.state_path, self.state)

    def _event(self, kind, **data):
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8") as out:
            out.write(
                json.dumps(
                    dict(time=self.clock(), controller_id=self.controller_id, event=kind, **data),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )

    def _child_spawn(self, process, args):
        self._save(
            child=dict(
                pid=process.pid,
                native_creation_time=_identity(process.pid),
                command=args[-3:] if "--validate-requests" not in args else ["validate-requests"],
            )
        )

    def _child_exit(self, process, args, stdout, stderr):
        # Full job ledgers/observations are parsed in memory, never copied into
        # the model/event log. Keep bounded stderr for failed command diagnosis.
        self._event(
            "cli_exit",
            pid=process.pid,
            returncode=process.returncode,
            command="validate-requests"
            if "--validate-requests" in args
            else next(
                (
                    a
                    for a in args
                    if a
                    in {
                        "observe",
                        "status",
                        "capabilities",
                        "actor-status",
                        "request",
                        "run",
                        "pause",
                        "resume",
                        "retry",
                        "checkpoint",
                        "detach-client",
                        "stop-worker",
                    }
                ),
                "typed_cli",
            ),
            stderr=stderr[-1600:],
        )
        self._save(child=None)

    def _call_event(self, command, seconds, error):
        """Timing record for one in-process command; the run command also carries kernel totals."""
        data = dict(command=command, seconds=round(seconds, 4))
        if error:
            data["error"] = error[-1600:]
        if command == "run":
            metrics = getattr(getattr(self.cli, "session", None), "metrics", None)
            if callable(metrics):
                data["kernel"] = metrics()
        self._event("call", **data)

    def _world_guard(self):
        if json.loads((self.root / "world-settings.json").read_text(encoding="utf-8")).get("world_id") != self.world:
            raise ValueError("installed world changed; supervisor will not retarget")

    def _stop_requested(self):
        if self.stopping:
            return True
        path = self.root / STOP_FILE
        if path.exists():
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                marker = {"request_id": "invalid-marker", "world_id": self.world, "invalid": True}
            self.stop_marker = marker
            self.stopping = True
            self._save(status="stopping", stop_request_id=marker.get("request_id"), stop_requested_at=self.clock())
            return True
        if self.max_seconds is not None and self.clock() - self.state["started_at"] >= self.max_seconds:
            self.stop_marker = {
                "request_id": "duration-" + self.controller_id,
                "world_id": self.world,
                "source": "max_seconds",
            }
            self.stopping = True
            self._save(status="stopping", stop_request_id=self.stop_marker["request_id"])
            return True
        return False

    def _worker_stop(self):
        self._world_guard()
        path = self.root / "stop.json"
        payload = json.dumps(
            dict(
                source="gameplay-worker-cli",
                controller_id=self.controller_id,
                stop_request_id=(self.stop_marker or {}).get("request_id"),
                requested_at=self.clock(),
            ),
            separators=(",", ":"),
        ).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            self._save(worker_stop_owned=True, worker_stop_sha256=hashlib.sha256(payload).hexdigest())
        self.cli.invoke(["stop-worker"])

    def _snapshot(self):
        self._world_guard()
        observation = self.cli.invoke(["observe"])
        if actor_client.detach_due(self.root, observation):
            self._detach_client()
        self._client_free_presence()
        status = self.cli.invoke(["status"])
        capabilities = self.cli.invoke(["capabilities"])
        inspected = self.cli.invoke(["actor-status"])
        try:
            actor = actor_binding(inspected, self.world)
        except ActorDeadWithoutPlayer:
            self._actor_dead_without_player(inspected)
            raise
        if observation.get("world_id") != self.world or status.get("world_id") not in (None, self.world):
            raise ValueError("native observation/jobs disagree with installed world")
        tick = observation.get("tick")
        if (
            type(tick) is not int
            or tick < 0
            or tick < self.state.get("last_observed_tick", 0)
            or "rollback_tick" in status
        ):
            raise ValueError("world rollback requires explicit reconciliation")
        jobs = list(status.get("jobs", {}).values())
        self.work.refresh(jobs, capabilities)
        keys = _active_keys(jobs)
        self._save(
            last_observed_tick=tick,
            actor=actor,
            active_job_keys=keys,
            active_job_key=keys[0] if keys else None,
        )
        self._note_technologies(observation)
        return observation, status, capabilities, actor

    def _detach_client(self):
        """until_lab_trigger: detach the actor and close the client once the lab trigger fired."""
        receipt = self.cli.invoke(["detach-client"])
        reason = receipt.get("reason")
        if receipt.get("status") == "succeeded" or reason != self.state.get("last_detach_reason"):
            record = receipt.get("record") or {}
            self._event(
                "actor_detach",
                status=receipt.get("status"),
                reason=reason,
                tick=record.get("tick") or receipt.get("tick"),
                unit_number=record.get("unit_number"),
                closed_client=record.get("closed_client"),
            )
            self._save(last_detach_reason=reason)

    def _client_free_presence(self):
        """Client-free test mode: chart/generate around the actor as a connected player would (actor_client)."""
        if actor_client.mode(self.root) != actor_client.NONE:
            return
        now = self.clock()
        if now - getattr(self, "_presence_at", -1e9) < PRESENCE_SECONDS:
            return
        self._presence_at = now
        receipt = self.cli.invoke(["client-free-presence"])
        if receipt.get("first") or receipt.get("status") != "succeeded":
            self._event(
                "client_free_presence",
                status=receipt.get("status"),
                reason=receipt.get("reason"),
                tick=receipt.get("tick"),
            )

    def _actor_dead_without_player(self, inspected):
        """Death after the detach: record the terminal state and stop the run (actor_client)."""
        dead = actor_client.record_dead(self.root, inspected)
        self._event("actor_dead_without_player", **{k: dead.get(k) for k in ("tick", "unit_number", "rule")})
        if not self.stopping:
            self.stop_marker = {
                "request_id": "actor-dead-" + self.controller_id,
                "world_id": self.world,
                "source": actor_client.DEAD_WITHOUT_PLAYER,
            }
            self.stopping = True
            self._save(status="stopping", stop_reason=actor_client.DEAD_WITHOUT_PLAYER)

    def _note_technologies(self, observation):
        """Researched technologies in the order this controller first saw them; receipts list them sorted."""
        seen = set(self.technology_order)
        for name in observation.get("technologies") or []:
            if isinstance(name, str) and name not in seen:
                seen.add(name)
                self.technology_order.append(name)

    def _queue_low(self):
        return self.work.queued_count() < LOW_QUEUE

    def _load_roadmap(self):
        """The framework roadmap steps, or None while the progression module is unavailable."""
        try:
            return tuple(import_module(__package__ + ".progression").ROADMAP)
        except (ImportError, AttributeError, TypeError):
            return None

    def _recent_intents(self):
        """The last 100 ``action_intent`` rows from a bounded tail of job-events.jsonl."""
        path = self.root / "job-events.jsonl"
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - INTENT_TAIL_BYTES))
                data = source.read()
        except OSError:
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > INTENT_TAIL_BYTES:
            # The first line of a cut tail may be partial.
            lines = lines[1:]
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event") == "action_intent":
                rows.append(row)
        return rows[-100:]

    def _check_incidents(self, context):
        """Open and close incident episodes for this snapshot; log openings and outcomes."""
        observation, status, capabilities, _ = context
        jobs = list(status.get("jobs", {}).values())
        roadmap = self._load_roadmap()
        retries = {key: row["count"] for key, row in self.__dict__.get("_auto_retries", {}).items()}
        inputs = incident_rules.Inputs(
            now=self.clock(),
            jobs=jobs,
            nodes=self.work.data["nodes"],
            observation=observation,
            capabilities=capabilities,
            auto_retries=retries,
            intents=self._recent_intents,
            roadmap=roadmap,
            recent_technologies=self.technology_order[-10:],
        )
        changes = self.incidents.update(inputs)
        for incident in changes.opened:
            self._event("incident_opened", handled_by=self.planner, **self._incident_fields(incident))
            if self.planner == "none":
                self._record_incident(incident, self._document(incident, inputs, roadmap))
        for incident, outcome, seconds in changes.closed:
            self._event(
                "incident_outcome",
                handled_by=self.planner,
                outcome=outcome,
                seconds=seconds,
                remedy=incident.remedy,
                **self._incident_fields(incident),
            )
        if changes.opened or changes.closed:
            self._save(
                open_incidents=[
                    dict(id=q.id, kind=q.kind, subject=q.subject, since=q.since) for q in self.incidents.open.values()
                ]
            )
        return inputs, roadmap

    @staticmethod
    def _incident_fields(incident):
        return dict(
            incident_id=incident.id,
            incident_kind=incident.kind,
            subject=incident.subject,
            capability=incident.capability,
        )

    def _document(self, incident, inputs, roadmap):
        """The bounded consultation document for one incident (also recorded in planner none mode)."""
        rows = None
        if roadmap is not None:
            rows = incident_rules.roadmap_position(
                inputs.jobs, inputs.nodes, inputs.observation, inputs.capabilities, roadmap
            )
        factory = incident_rules.factory_status(
            inputs.observation, inputs.jobs, rows, inputs.recent_technologies, text=_diagnostic_text
        )
        return incident_rules.document(incident, factory)

    def _record_incident(self, incident, document):
        """Append the document to logs/incidents.jsonl, once per incident per retry window, size-capped."""
        now = self.clock()
        last = self.recorded.get(incident.id)
        if self.incident_log_full or (last is not None and now - last < self.incident_config.incident_retry_seconds):
            return
        self.incident_path.parent.mkdir(parents=True, exist_ok=True)
        if self.incident_path.exists() and self.incident_path.stat().st_size >= INCIDENT_LOG_BYTES:
            self.incident_log_full = True
            self._event("incident_log_full", bytes=INCIDENT_LOG_BYTES)
            return
        self.recorded[incident.id] = now
        row = dict(
            time=now,
            controller_id=self.controller_id,
            handled_by=self.planner,
            **self._incident_fields(incident),
            document=document,
        )
        with self.incident_path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, ensure_ascii=False, allow_nan=False, default=str) + "\n")

    def _incident_due(self):
        """The incident to consult the model about now, or None (see the class docstring)."""
        if self.planner != "luna" or self.pending_decision is not None:
            return None
        now = self.clock()
        if now < self.backoff_until:
            return None
        if self.last_decision_time is not None and now - self.last_decision_time < self.decision_interval:
            return None
        return self.incidents.due(now)

    def _start_decision(self, context, incident, document):
        result = queue.Queue(maxsize=1)

        def call():
            try:
                result.put((True, self.client.decide(document)))
            except BaseException as exc:
                result.put((False, exc))

        now = self.clock()
        self.last_decision_time = now
        self.incidents.mark_consulted(incident, now)
        self.state["decision_sequence"] += 1
        self._record_incident(incident, document)
        self.pending_decision = dict(result=result, context=context, started_at=now, incident=incident)
        self._save(status="deciding", luna_pending=True, last_decision_started_at=now, consulting_incident=incident.id)
        threading.Thread(target=call, daemon=True, name="luna-incident-decision").start()

    def _poll_decision(self):
        """Accept a finished model call: check it against the incident menu, then apply it.

        A decision outside the menu (or failing the schema) is logged as
        ``decision_rejected`` at most once per incident per
        ``rejection_log_seconds`` and does not enter error backoff. A decision
        for an incident that closed meanwhile is stale and not applied.
        Without an incident (tests only) the decision is applied directly.
        """
        if self.pending_decision is None:
            return False
        if self._stop_requested():
            return False
        pending = self.pending_decision
        try:
            ok, value = pending["result"].get_nowait()
        except queue.Empty:
            return False
        self.pending_decision = None
        self._save(luna_pending=False)
        incident = pending.get("incident")
        latency = round(self.clock() - pending["started_at"], 2)
        usage = _usage(getattr(self.client, "last_usage", None))
        fields = self._incident_fields(incident) if incident else {}
        if not ok:
            if incident:
                self._event(
                    "incident_decision",
                    remedy=None,
                    accepted=False,
                    error=str(value)[:300],
                    latency_seconds=latency,
                    **usage,
                    **fields,
                )
            raise RuntimeError(str(value)) from value
        rejected = None
        try:
            decision = validate_decision(value)
        except ValueError as exc:
            if incident is None:
                raise
            decision = None
            rejected = "invalid decision: " + str(exc)[:200]
        if decision is not None and incident is not None:
            rejected = incident_rules.check_decision(incident, decision)
        stale = incident is not None and rejected is None and incident.id not in self.incidents.open
        if incident is not None:
            self._event(
                "incident_decision",
                remedy=incident_rules.remedy_of(decision) if decision else None,
                target_task_id=(decision or {}).get("target_task_id"),
                accepted=rejected is None and not stale,
                reason=_diagnostic_text((decision or {}).get("reason", "")),
                latency_seconds=latency,
                **usage,
                **fields,
            )
        if rejected is not None:
            self._reject(incident, rejected)
        elif stale:
            self._event("stale_decision", reason="incident_closed_while_deciding", **fields)
        else:
            self._apply(decision, pending["context"])
            if incident is not None:
                incident.remedy = incident_rules.remedy_of(decision)
        self.errors = 0
        self.backoff_until = 0
        self._save(
            last_decision_at=self.clock(),
            last_decision=decision,
            status="running",
            last_error=None,
            last_luna_usage=getattr(self.client, "last_usage", None),
            last_luna_session_id=getattr(self.client, "last_session_id", None),
        )
        return True

    def _reject(self, incident, reason):
        """Log one ``decision_rejected`` per incident per ``rejection_log_seconds``."""
        now = self.clock()
        last = self.rejections.get(incident.id)
        if last is not None and now - last < self.incident_config.rejection_log_seconds:
            return
        self.rejections[incident.id] = now
        self._event("decision_rejected", reason=reason[:300], **self._incident_fields(incident))

    def _target(self, decision, status, allowed, *, missing_ok=False):
        target = decision.get("target_task_id")
        if target is not None:
            if self.work.world != self.world or self.work.data.get("world_id") != self.world:
                raise ValueError("target queue world changed")
            node = next((q for q in self.work.data["nodes"] if q["id"] == target), None)
            if node is None:
                raise ValueError("unknown target_task_id: " + target)
            if node.get("job_key") != stable_job_key(self.world, node):
                raise ValueError("target task job identity changed")
            if (
                decision.get("capability") not in (None, node["capability"])
                or decision["parameters"] != node["parameters"]
            ):
                raise ValueError("target_task_id capability or parameters do not match the accepted task")
            job = status.get("jobs", {}).get(node["job_key"])
            if job is None or job["status"] not in allowed:
                if missing_ok:
                    return None
                raise ValueError("target_task_id has no eligible job in the decision snapshot")
            if any(
                job.get(k) != expected
                for k, expected in (
                    ("key", node["job_key"]),
                    ("world_id", self.world),
                    ("capability", node["capability"]),
                    ("parameters", node["parameters"]),
                )
            ):
                raise ValueError("target_task_id prompt job identity changed")
            return job
        rows = [j for j in status.get("jobs", {}).values() if j["status"] in allowed]
        cap = decision.get("capability")
        params = decision.get("parameters", {})
        if cap is not None:
            rows = [j for j in rows if j["capability"] == cap]
        if params:
            rows = [j for j in rows if j.get("parameters") == params]
        active = [j for j in rows if j["status"] in ACTIVE]
        if active:
            rows = active
        if not rows and missing_ok:
            return None
        if len(rows) != 1:
            raise ValueError("operation requires one unambiguous existing job")
        if rows[0].get("world_id") != self.world:
            raise ValueError("job world identity changed")
        return rows[0]

    def _resume_known(self, context):
        """Resume every job the preceding safe stop paused, before any new dispatch.

        A saved key whose job is missing, belongs to another world or has an
        unknown action outcome stops the cycle; nothing unrelated replaces it.
        """
        if not self.resume_job_keys:
            return False
        jobs = context[1].get("jobs", {})
        for key in list(self.resume_job_keys):
            job = jobs.get(key)
            if not job or job.get("world_id") != self.world:
                raise ValueError("saved paused job identity missing; no unrelated replacement")
            if job.get("pending_action") or job.get("reason") == "action_outcome_unknown_reconcile_required":
                raise ValueError("saved job requires reviewed unknown-outcome reconciliation")
            if job["status"] == "paused":
                if self._stop_requested():
                    return False
                self.cli.invoke(["resume", "--key", key])
                self._event("resume_after_safe_start", key=key)
            elif job["status"] not in ACTIVE | TERMINAL:
                raise ValueError("saved job cannot be safely resumed")
            self.resume_job_keys.remove(key)
            self._save(resume_job_keys=list(self.resume_job_keys))
        return True

    def _load_autopilot(self):
        """The framework's default next-step planner, or None while it is unavailable."""
        try:
            return import_module(__package__ + ".progression").autopilot_nodes
        except (ImportError, AttributeError):
            return None

    def _autopilot(self, context):
        """When fewer than two nodes are pending or ready, merge framework default nodes.

        Autopilot nodes use Luna's queue node format with ids starting ``auto_``
        and pass the same merge validation. A rejected batch is logged and does
        not enter error backoff, so Luna planning and dispatch continue.
        """
        if not self._queue_low():
            return False
        planner = self._load_autopilot()
        if planner is None:
            return False
        observation, status, capabilities, _ = context
        jobs = list(status.get("jobs", {}).values())
        known = {q["id"] for q in self.work.data["nodes"]}
        try:
            rows = planner(jobs, deepcopy(self.work.data["nodes"]), observation, capabilities, limit=AUTOPILOT_LIMIT)
            rows = [
                q
                for q in (rows or [])[:AUTOPILOT_LIMIT]
                if isinstance(q, dict) and str(q.get("id", "")).startswith("auto_") and q.get("id") not in known
            ]
            if not rows:
                return False
            self._merge(rows, capabilities, observation, "autopilot")
        except Exception as exc:
            self._event("autopilot_rejected", reason=str(exc)[:600])
            return False
        self.work.refresh(jobs, capabilities)
        self._event("autopilot", task_ids=[q["id"] for q in rows])
        return True

    def _merge(self, nodes, capabilities, observation, source):
        skipped = self.work.merge(
            nodes,
            capabilities,
            self.cli.validate_requests,
            observation["tick"],
            native_gates=observation.get("native_capability_gates"),
        )
        if skipped:
            self._event("queue_duplicates_skipped", source=source, task_ids=skipped[:16])

    def _apply(self, decision, context):
        observation, prompt_status, _, actor = context
        if self._stop_requested():
            return
        # A potentially slow model call cannot authorize a stale actor/world.
        self._world_guard()
        fresh = self.cli.invoke(["observe"])
        fresh_actor = actor_binding(self.cli.invoke(["actor-status"]), self.world)
        if fresh.get("world_id") != self.world or not _same_actor(fresh_actor, actor):
            raise ValueError("world/actor changed during model decision; discard and observe again")
        status = self.cli.invoke(["status"])
        capabilities = self.cli.invoke(["capabilities"])
        if (
            status.get("world_id") not in (None, self.world)
            or "rollback_tick" in status
            or fresh.get("tick", -1) < observation["tick"]
        ):
            raise ValueError("fresh decision status world or tick changed")
        if self._stop_requested():
            return
        self.work.refresh(list(status.get("jobs", {}).values()), capabilities)
        self._merge(decision["queue"], capabilities, fresh, "luna")
        operation = decision["operation"]
        self._event(
            "decision",
            operation=operation,
            capability=decision.get("capability"),
            target_task_id=decision.get("target_task_id"),
            reason=decision["reason"],
            queue_nodes=len(decision["queue"]),
        )
        if operation == "request":
            self._queue_request(decision, capabilities, fresh)
        elif operation in ("continue", "retry", "pause"):
            self._control(decision, prompt_status, status)
        # ``wait`` changes nothing: the incident's retry window spaces the next consultation.

    def _queue_request(self, decision, capabilities, observation):
        """A request is one more queue node; admission happens at dispatch."""
        node = dict(
            id="now_"
            + str(self.state["decision_sequence"])
            + "_"
            + _hash([decision["capability"], decision["parameters"]])[:12],
            capability=decision["capability"],
            parameters=decision["parameters"],
            priority=0,
            depends_on=[],
            reason=decision["reason"],
        )
        self._merge([node], capabilities, observation, "luna_request")

    def _control(self, decision, prompt_status, status):
        """Apply continue/retry/pause to the exact job Luna saw, never a successor.

        Without ``target_task_id`` a ``continue`` is a no-op: every active job is
        already running. ``retry``/``pause`` without a target need exactly one
        matching job. Unknown action outcomes are never retried.
        """
        operation = decision["operation"]
        if operation == "continue" and decision.get("target_task_id") is None:
            self._event("decision_noop", operation=operation, reason="active_jobs_already_run")
            return
        allowed = ACTIVE | {"paused"} if operation in ("continue", "pause") else {"blocked"}
        intended = self._target(decision, prompt_status, allowed, missing_ok=True)
        if intended is None:
            self._event(
                "stale_decision",
                operation=operation,
                target_task_id=decision.get("target_task_id"),
                reason="no_target_in_decision_snapshot",
            )
            return
        job = status.get("jobs", {}).get(intended["key"])
        if not job or any(job.get(k) != intended.get(k) for k in ("key", "world_id", "capability", "parameters")):
            raise ValueError("original decision target identity missing or changed; will not retarget")
        node = self.work.node_for_job(job["key"])
        if operation == "pause" and job["status"] == "paused" and node:
            node["paused"] = True
            self.work.save()
        if job["status"] not in allowed or (operation == "pause" and job["status"] == "paused"):
            self._event(
                "stale_decision",
                operation=operation,
                key=job["key"],
                reason="original_target_no_longer_eligible",
                status=job["status"],
            )
            return
        if operation == "retry" and _unknown(job):
            raise ValueError("uncertain outcome requires reviewed reconciliation, never model retry")
        command = "resume" if operation == "continue" and job["status"] == "paused" else operation
        if command != "continue" and not self._control_command(command, job["key"]):
            return
        if node:
            node["paused"] = operation == "pause"
            self.work.save()

    def _control_command(self, command, key):
        """Run one job control; a retryable admission refusal is deferred, not an error."""
        if self._stop_requested():
            return False
        try:
            self.cli.invoke([command, "--key", key])
        except CLIError as exc:
            if not _retryable_admission(str(exc)):
                raise
            self._event("decision_deferred", operation=command, key=key, reason=str(exc)[:300])
            return False
        return True

    def _fresh_tick(self, actor):
        """A current tick for requests, after rechecking world and actor identity."""
        if (self.root / "stop.json").exists():
            raise ValueError("worker stop marker present; explicit lifecycle start/ownership check required")
        self._world_guard()
        fresh = self.cli.invoke(["observe"])
        binding = actor_binding(self.cli.invoke(["actor-status"]), self.world)
        if fresh.get("world_id") != self.world or not _same_actor(binding, actor):
            raise ValueError("world/actor changed before queued request")
        return fresh["tick"]

    def _dispatch(self, context):
        """Submit ready nodes, highest priority first, while fewer than MAX_ACTIVE_JOBS are active.

        A node whose admission lane (``jobs.admission_lane``) is already active stays ready, as does one whose
        submit is refused for a retryable admission reason; any other refusal
        blocks that node. Returns the active job keys after submission.
        """
        _, status, _, actor = context
        jobs = list(status.get("jobs", {}).values())
        active = _active_keys(jobs)
        busy = {admission_lane(j["capability"], j.get("parameters")) for j in jobs if j["status"] in ACTIVE}
        # A job parked for research (early_start.parked) keeps its lane but holds no slot.
        slots = sum(1 for j in jobs if j["status"] in ACTIVE and not parked(j))
        candidates = [q for q in self.work.ready_nodes() if _lane(q) not in busy]
        if slots >= MAX_ACTIVE_JOBS or not candidates or self._stop_requested():
            return active
        tick = self._fresh_tick(actor)
        for node in candidates:
            if slots >= MAX_ACTIVE_JOBS or self._stop_requested():
                break
            if _lane(node) in busy:
                continue
            if self._submit(node, tick):
                active.append(node["job_key"])
                busy.add(_lane(node))
                slots += 1
        return active

    def _submit(self, node, tick):
        try:
            self.cli.invoke(
                [
                    "request",
                    "--key",
                    node["job_key"],
                    "--world-id",
                    self.world,
                    "--observed-tick",
                    str(tick),
                    "--capability",
                    node["capability"],
                    "--parameters",
                    json.dumps(node["parameters"], separators=(",", ":"), allow_nan=False),
                    "--priority",
                    str(node["priority"]),
                ]
            )
        except CLIError as exc:
            reason = str(exc)
            if _retryable_admission(reason):
                self._event("dispatch_deferred", task_id=node["id"], reason=reason[:300])
                return False
            node.update(status="blocked", dispatch_blocked=True, last_reason=reason)
            self.work.save()
            self._event("dispatch_blocked", task_id=node["id"], reason=reason[:300])
            return False
        node.update(status="running", submitted=True)
        self.work.save()
        return True

    def _run_slice(self, active):
        """One interleaved ``run --all`` slice; False when no job was active."""
        if not active:
            return False
        if (self.root / "stop.json").exists():
            raise ValueError("worker stop marker present; no new run slice")
        if self._stop_requested():
            return False
        self._world_guard()
        self._save(status="running", active_job_keys=list(active), active_job_key=active[0])
        receipt = self.cli.invoke(
            ["run", "--all", "--seconds", str(self.slice_seconds)],
            stop_requested=self._stop_requested,
            on_stop=self._worker_stop,
        )
        jobs = receipt.get("jobs") or {}
        self._event(
            "slice",
            status=receipt.get("status"),
            actions=receipt.get("actions"),
            tick=receipt.get("last_tick"),
            jobs={k: {f: v.get(f) for f in ("status", "reason", "actions")} for k, v in list(jobs.items())[:8]},
        )
        return receipt.get("status") != "idle"

    def _run_authorized(self, context):
        """Dispatch ready approved nodes, then advance every active job for one slice."""
        if self._stop_requested():
            return False
        return self._run_slice(self._dispatch(context))

    def safe_stop(self):
        """Pause every active job, then checkpoint; ACK only after both succeed."""
        marker = self.stop_marker or {
            "request_id": "exit-" + self.controller_id,
            "world_id": self.world,
            "source": "supervisor_exit",
        }
        self._save(status="stopping", stop_request_id=marker.get("request_id"))
        self.pending_decision = None
        cancel = getattr(self.client, "cancel", None)
        if callable(cancel):
            cancel()
        try:
            self._world_guard()
            if marker.get("world_id") != self.world or marker.get("invalid"):
                raise ValueError("stop marker world or format changed")
            status = self.cli.invoke(["status"])
            if status.get("world_id") not in (None, self.world):
                raise ValueError("job world changed while stopping")
            jobs = status.get("jobs", {})
            active = _active_keys(list(jobs.values()))
            receipts = [self.cli.invoke(["pause", "--key", key]) for key in active]
            # Jobs paused by an earlier safe stop and not yet resumed stay owed a resume.
            receipts += [
                jobs[key]
                for key in self.resume_job_keys
                if key not in active and jobs.get(key, {}).get("status") == "paused"
            ]
            checkpoint = self.cli.invoke(["checkpoint"])
            if checkpoint.get("world_id") != self.world or checkpoint.get("status") != "checkpoint_requested":
                raise ValueError("native checkpoint receipt missing")
            self._save(
                status="safe_stopped",
                state="stopped",
                saved=True,
                pause_receipts=receipts,
                pause_receipt=receipts[0] if receipts else None,
                checkpoint_receipt=checkpoint,
                stop_request_id=marker.get("request_id"),
                active_job_key=None,
                active_job_keys=[],
                stopped_at=self.clock(),
            )
            self._event("safe_stopped", stop_request_id=marker.get("request_id"), saved=True, paused=len(receipts))
        except Exception as exc:
            self._save(status="blocked", state="stopping", saved=False, stop_error=str(exc))
            self._event("stop_failed", reason=str(exc))
        return deepcopy(self.state)

    def _cycle(self):
        """One supervisor cycle; False when no job ran and the loop should idle briefly."""
        context = self._snapshot()
        if self._stop_requested():
            return True
        if self._resume_known(context):
            context = self._snapshot()
        if self._autopilot(context):
            context = self._snapshot()
        if self._auto_retry(context):
            context = self._snapshot()
        if self._poll_decision():
            if self._stop_requested():
                return True
            context = self._snapshot()
        inputs, roadmap = self._check_incidents(context)
        incident = self._incident_due()
        if incident is not None:
            self._start_decision(context, incident, self._document(incident, inputs, roadmap))
        if self._stop_requested():
            return True
        # Approved queue work runs while a model call is pending.
        return self._run_authorized(context)

    def _auto_retry(self, context):
        """Retry a blocked job once another job has succeeded since it blocked.

        Most blockers are a missing prerequisite ("coal bank required") that a
        later success removes. Waiting for the planner to notice wastes minutes,
        so the framework retries by itself, at most AUTO_RETRY_LIMIT times per
        job. Unknown action outcomes, rollbacks and deadlines are never retried.
        """
        jobs = list(context[1].get("jobs", {}).values())
        succeeded = sum(job["status"] == "succeeded" for job in jobs)
        memory = self.__dict__.setdefault("_auto_retries", {})
        retried = False
        for job in jobs:
            key = job["key"]
            if job["status"] != "blocked":
                # Keep the count while the job is merely running again, so a job
                # that keeps blocking cannot be retried without limit.
                if job["status"] == "succeeded":
                    memory.pop(key, None)
                continue
            reason = str(job.get("reason", ""))
            if _unknown(job) or "rollback" in reason or "deadline" in reason:
                continue
            seen = memory.setdefault(key, {"successes": succeeded, "count": 0, "first_seen": self.clock()})
            if seen["count"] >= AUTO_RETRY_LIMIT:
                continue
            # A stalled job waited on something (fuel, plates) that upkeep may
            # since have fixed; with every job blocked no success can ever
            # arrive, so a stall is also retried after a quiet interval.
            # A site search that found nothing ("no_clear_...") is retried the same way:
            # cleared corridors, finished neighbours or a planning budget that ran out change
            # the answer without a job success (pace1 seed 2692229611: the district blocked at
            # 19.8 min, found its site on the first retry at 25.0, after the next success).
            stalled_long_enough = (
                (reason == "objective_progress_stalled" or reason.startswith("no_clear_"))
                # The stall path does not stamp blocked_at_time on the job, so
                # fall back to when this supervisor first saw it blocked.
                and self.clock() - float(job.get("blocked_at_time") or seen.get("first_seen", self.clock()))
                >= AUTO_RETRY_STALL_SECONDS
            )
            if succeeded <= seen["successes"] and not stalled_long_enough:
                continue
            if self._control_command("retry", key):
                seen.update(successes=succeeded, count=seen["count"] + 1, first_seen=self.clock())
                self._event("auto_retry", key=key, capability=job.get("capability"), blocked_reason=reason[:200])
                retried = True
            else:
                # A retry deferred for a job slot is not an attempt (fixpace1 seed 2692229611: two
                # slot refusals used up the district's retries; it never ran again).
                seen.update(successes=succeeded, first_seen=self.clock())
        return retried

    def _idle(self, seconds):
        until = self.clock() + seconds
        while self.clock() < until and not self._stop_requested():
            if self._poll_decision():
                break
            self.sleep(0.25)

    def run(self):
        with RunLock(self.root / "controller.lock"):
            self._save()
            self._event("supervisor_started", planner=self.planner, model=self.state["model"])
            try:
                while not self._stop_requested():
                    try:
                        if not self._cycle():
                            self._idle(2)
                    except Exception as exc:
                        self.errors += 1
                        self.backoff_until = self.clock() + min(300, 10 * 2 ** min(5, self.errors - 1))
                        self._save(status="backoff", last_error=str(exc), next_decision_at=self.backoff_until)
                        self._event("error", reason=str(exc), backoff_until=self.backoff_until)
                        # A rejected model response cannot invent a fallback.
                        # Already authorized work is revisited only after a new
                        # native snapshot, with bounded polling during backoff.
                        until = self.clock() + min(1, max(0, self.backoff_until - self.clock()))
                        while self.clock() < until and not self._stop_requested():
                            self.sleep(0.25)
            except KeyboardInterrupt:
                self.stopping = True
                self.stop_marker = {"request_id": "interrupt-" + self.controller_id, "world_id": self.world}
            finally:
                return self.safe_stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument(
        "--planner",
        choices=PLANNERS,
        default="luna",
        help="luna consults the model on incidents; none runs on framework rules only and never starts the model",
    )
    parser.add_argument("--decision-interval", type=float, default=60, help="minimum seconds between model calls")
    parser.add_argument("--slice-seconds", type=float, default=25)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--validate-requests", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--bridge",
        choices=("inprocess", "cli"),
        default="inprocess",
        help="inprocess keeps one game session in this process; cli starts a child process per command",
    )
    args = parser.parse_args(argv)
    if args.validate_requests:
        from .jobs import Request

        rows = json.load(sys.stdin)
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError("bounded typed request batch required")
        for row in rows:
            Request(**row).validate()
        print(json.dumps({"ok": True}))
        return
    if args.runtime is None:
        parser.error("--runtime ABS is required")
    client = None
    if args.planner == "luna":
        # Imported only when enabled: planner none never loads or starts the Codex CLI.
        from .luna_client import LunaDecisionClient

        client = LunaDecisionClient(model="gpt-6-luna", reasoning_effort="medium", timeout_seconds=120)
    supervisor = Supervisor(
        args.runtime,
        client,
        planner=args.planner,
        decision_interval=args.decision_interval,
        slice_seconds=args.slice_seconds,
        max_seconds=args.max_seconds,
    )
    if args.bridge == "inprocess":
        from .kernel import InProcessBridge

        supervisor.cli = InProcessBridge(supervisor.root, on_call=supervisor._call_event)
    result = supervisor.run()
    print(
        json.dumps({k: result.get(k) for k in ("status", "world_id", "saved", "stop_request_id")}, ensure_ascii=False)
    )


if __name__ == "__main__":
    try:
        main()
    except (Exception,) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
