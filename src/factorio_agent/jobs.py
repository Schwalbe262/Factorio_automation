"""Bounded, resumable model-selected jobs with a single world writer."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from factorio_ai.deterministic_state import RunLock, _atomic_json


TERMINAL = {"succeeded", "blocked", "failed"}
CAPABILITIES = {
    "starter": {"parameters": {}, "requires": [],
                "effect": "drill-fed iron/copper, coal/stone starter cells; natural early unlocks"},
    "power": {"parameters": {}, "requires": ["starter"],
              "effect": "steam power with real self-fueling coal belt; verify 30 game seconds"},
    "starter_capacity": {"parameters": {"resource": "iron-ore|copper-ore|coal|stone", "cells": "integer 1..8, absolute target"},
                         "requires": ["starter"], "effect": "expand productive starter cells to fund construction faster"},
    "starter_research": {"parameters": {"technology": "automation|electric-mining-drill"}, "requires": ["power"],
                         "effect": "finite normally handcrafted startup science; natural research, maximum one budget per technology"},
    "regularize_power_layout": {"parameters": {}, "requires": ["starter"],
                               "effect": "recover obsolete unfinished coal feed and install validated straight trunks; no hot cutover"},
}


@dataclass(frozen=True)
class Request:
    key: str
    world_id: str
    observed_tick: int
    capability: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.key):
            raise ValueError("key must contain 1..80 ASCII letters, digits, _ or -")
        if not isinstance(self.world_id, str) or not self.world_id:
            raise ValueError("world_id is required")
        if type(self.observed_tick) is not int or self.observed_tick < 0:
            raise ValueError("observed_tick must be a nonnegative integer")
        if self.capability not in CAPABILITIES:
            raise ValueError("capability_not_implemented: " + str(self.capability))
        if self.capability == "starter_capacity":
            from .mining import validate_parameters
            validate_parameters(self.parameters)
        elif self.capability == "starter_research":
            from .research import validate_parameters
            validate_parameters(self.parameters)
        elif not isinstance(self.parameters, dict) or self.parameters:
            raise ValueError("this capability accepts no parameters")

    def identity(self) -> dict:
        # Retrying a request after observing again retains the same effect ID.
        return {"key": self.key, "world_id": self.world_id,
                "capability": self.capability, "parameters": self.parameters}


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        path = self.root / "jobs.json"
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 2, "jobs": {}, "world_id": None, "last_tick": 0}
        if self.data.get("schema_version") != 2:
            raise ValueError("unsupported jobs checkpoint")

    def save(self):
        _atomic_json(self.root / "jobs.json", self.data)

    def event(self, key: str, kind: str, **fields):
        with (self.root / "job-events.jsonl").open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps({"key": key, "event": kind, "time": time.time(), **fields},
                                    ensure_ascii=False, allow_nan=False) + "\n")

    def sync(self, obs: dict):
        if not obs.get("ok"):
            raise RuntimeError(obs.get("reason", "observation_failed"))
        if self.data["world_id"] not in (None, obs["world_id"]):
            raise ValueError("world identity changed; use a new runtime")
        self.data["world_id"] = obs["world_id"]
        if obs["tick"] < self.data["last_tick"]:
            for job in self.data["jobs"].values():
                job.update(status="blocked", reason="world_rollback_requires_reconciliation", evidence={})
            self.data["rollback_tick"] = obs["tick"]
        self.data["last_tick"] = obs["tick"]
        self.save()


def summary(obs: dict, ledger: Ledger) -> dict:
    return {"world_id": obs["world_id"], "tick": obs["tick"],
            "inventory": obs.get("inventory", {}),
            "entities": dict(Counter(e["name"] for e in obs.get("entities", []))),
            "production": obs.get("production", {}), "technologies": sorted(obs.get("technologies", {})),
            "research": obs.get("research"), "enemies_near_start": obs.get("enemies"),
            "research_progress": obs.get("research_progress"), "electricity": obs.get("electricity"),
            "capabilities": CAPABILITIES,
            "jobs": [{k: v for k, v in job.items() if k in
                      {"key", "capability", "status", "reason", "evidence", "actions", "last_tick"}}
                     for job in ledger.data["jobs"].values()],
            "rocket_launch_verified": False}


def submit(root: Path, game, request: Request) -> dict:
    request.validate()
    with RunLock(root / "writer.lock"):
        obs = game.observe()
        ledger = Ledger(root)
        ledger.sync(obs)
        if request.world_id != obs["world_id"]:
            raise ValueError("request targets another world")
        old = ledger.data["jobs"].get(request.key)
        if old:
            if old["request"] != request.identity():
                raise ValueError("idempotency key already belongs to another request")
            return old
        if not 0 <= obs["tick"] - request.observed_tick <= 3600:
            raise ValueError("observation is stale or newer than the game; observe again")
        if any(j["status"] not in TERMINAL | {"paused"} for j in ledger.data["jobs"].values()):
            raise ValueError("another job is active; finish it before submitting another")
        if "rollback_tick" in ledger.data:
            raise ValueError("world rollback requires explicit reconciliation")
        completed = {j["capability"] for j in ledger.data["jobs"].values() if j["status"] == "succeeded"}
        if not set(CAPABILITIES[request.capability]["requires"]).issubset(completed):
            raise ValueError("capability prerequisites have not been verified")
        job = {**request.identity(), "request": request.identity(), "status": "pending",
               "reason": "model_requested", "created_tick": obs["tick"], "last_tick": obs["tick"],
               "created_at": time.time(), "actions": 0, "evidence": {}, "failures": 0,
               "progress_at_tick": obs["tick"], "progress_at_time": time.time()}
        ledger.data["jobs"][request.key] = job
        ledger.save()
        ledger.event(request.key, "submitted", capability=request.capability, tick=obs["tick"])
        return job


def control(root: Path, game, key: str, operation: str) -> dict:
    if operation not in {"pause", "resume", "reconcile"}:
        raise ValueError("unsupported job control")
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        ledger.sync(game.observe())
        job = ledger.data["jobs"].get(key)
        if job is None:
            raise ValueError("unknown job key")
        if operation == "pause":
            if job["status"] not in {"pending", "running", "waiting", "paused"}:
                raise ValueError("only an unfinished job can be paused")
            if job["status"] != "paused":
                job.update(paused_at_tick=ledger.data["last_tick"], paused_at_time=time.time())
            job.update(status="paused", reason="model_requested_pause")
        else:
            if any(j["key"] != key and j["status"] not in TERMINAL | {"paused"}
                   for j in ledger.data["jobs"].values()):
                raise ValueError("another job is active")
            if operation == "resume" and job["status"] != "paused":
                raise ValueError("only a paused job can be resumed")
            if operation == "resume":
                if "paused_at_tick" not in job:
                    # Upgrade receipts created before pause-budget accounting.
                    # Search only the matching event; never replay any action.
                    with (root / "job-events.jsonl").open(encoding="utf-8") as events:
                        for line in events:
                            event = json.loads(line)
                            if event.get("key") == key and event.get("event") == "pause":
                                job.update(paused_at_tick=event["tick"], paused_at_time=event["time"])
                pause_ticks = max(0, ledger.data["last_tick"] - job.pop("paused_at_tick", ledger.data["last_tick"]))
                pause_seconds = max(0, time.time() - job.pop("paused_at_time", time.time()))
                job["paused_ticks"] = job.get("paused_ticks", 0) + pause_ticks
                job["paused_seconds"] = job.get("paused_seconds", 0) + pause_seconds
                job["progress_at_tick"] += pause_ticks
                job["progress_at_time"] += pause_seconds
            if operation == "reconcile":
                if (job["reason"] != "action_outcome_unknown_reconcile_required"
                        or "rollback_tick" in ledger.data):
                    raise ValueError("only an uncertain action in the same live world can be reconciled")
                # These registered executors replan from observed inventories,
                # entity identities and crafting queues; no old action is replayed.
                ledger.event(key, "unknown_action_reconciled", old_intent=job.pop("pending_action", None),
                             tick=ledger.data["last_tick"])
            job.update(status="waiting", reason="fresh_observation_before_replanning")
        ledger.save()
        ledger.event(key, operation, status=job["status"], tick=ledger.data["last_tick"])
        return job


class StarterExecutors:
    """Explicit temporary v1 executor reuse, with no v1 strategic supervisor."""
    def __init__(self, game, catalog):
        from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
        from .power_layout import V2FactoryBuilder
        self.bootstrap = DeterministicBootstrap(game, catalog)
        self.builder = V2FactoryBuilder(game, self.bootstrap, catalog)
        from .mining import StarterCapacity
        from .research import StarterResearch
        self.capacity = StarterCapacity(game, self.bootstrap)
        self.research = StarterResearch(game, self.bootstrap, catalog)
        from .power_layout import RegularizePower
        self.power_layout = RegularizePower(game, self.builder)

    def step(self, capability: str, obs: dict, parameters: dict | None = None) -> dict:
        if capability == "starter":
            return self.bootstrap.next_action(obs)
        if capability == "regularize_power_layout":
            self.builder._sync(obs)
            fuel_obs = {**obs, "entities": [e for e in obs.get("entities", [])
                        if not self.builder.owns_automated_burner(e)]}
            fuel = self.bootstrap.maintain_fuel(fuel_obs)
            if fuel is not None:
                return fuel
            return self.power_layout.step(obs)
        # Retain starter supplies during power construction without handfeeding
        # burners now owned by the automatic power plan.
        self.builder._sync(obs)
        filtered = {**obs, "entities": [e for e in obs.get("entities", [])
                                       if not self.builder.owns_automated_burner(e)]}
        result = self.bootstrap.next_action(filtered)
        if result.get("status") != "succeeded":
            return result
        if capability == "starter_capacity":
            return self.capacity.step(filtered, parameters or {})
        if capability == "power":
            return self.builder.ensure_power(obs)
        if capability == "starter_research":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.research.step(obs, parameters or {})
        raise ValueError("no executor for capability: " + capability)

    def proof(self, capability: str, obs: dict, result: dict, parameters: dict | None = None) -> dict | None:
        if capability == "starter_capacity":
            return self.capacity.proof(obs, parameters or {})
        if capability == "starter_research":
            return self.research.proof(obs, parameters or {})
        if capability == "regularize_power_layout":
            return self.power_layout.proof(obs)
        produced = obs.get("production", {})
        if not all(produced.get(name, {}).get("produced", 0) > 0 for name in ("iron-plate", "copper-plate")):
            return None
        if capability == "starter":
            techs = {"steam-power", "electronics", "automation-science-pack"}
            if not techs.issubset(obs.get("technologies", {})):
                return None
            return {"tick": obs["tick"], "natural_technologies": sorted(techs),
                    "machine_production": {n: produced[n] for n in ("iron-plate", "copper-plate")},
                    **result.get("evidence", {})}
        evidence = result.get("evidence", {})
        if evidence.get("flow_verified") and evidence.get("verification_ticks", 0) >= 1800:
            return {"tick": obs["tick"], **evidence}
        return None


def progress_key(obs: dict, capability: str) -> str:
    # Relevant effects only: unrelated inventory churn must not hide a stall.
    value = {"production": {n: obs.get("production", {}).get(n) for n in ("iron-plate", "copper-plate", "coal")},
             "techs": [n for n in ("steam-power", "electronics", "automation-science-pack")
                       if obs.get("technologies", {}).get(n)],
             "entities": [(e.get("unit_number"), e.get("name")) for e in obs.get("entities", [])]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run_slice(root: Path, game, executors, key: str, *, seconds: float = 25) -> dict:
    if not isinstance(seconds, (int, float)) or not 0 < seconds <= 50:
        raise ValueError("slice duration must be greater than zero and at most 50 seconds")
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        if key not in ledger.data["jobs"]:
            raise ValueError("unknown job key")
        job = ledger.data["jobs"][key]
        deadline = time.monotonic() + seconds
        while True:
            obs = game.observe()
            ledger.sync(obs)
            if job["status"] in TERMINAL | {"paused"}:
                # A terminal receipt is historical; callers must not assume it
                # proves present production after damage or world rollback.
                return job
            job["last_tick"] = obs["tick"]
            if job.get("pending_action"):
                # A prior process may have died after sending a mutation. Stop
                # rather than replaying an action whose outcome is unknown.
                job.update(status="blocked", reason="action_outcome_unknown_reconcile_required")
                ledger.save()
                return job
            if (obs["tick"] - job["created_tick"] - job.get("paused_ticks", 0) > 108000
                    or time.time() - job["created_at"] - job.get("paused_seconds", 0) > 3600):
                job.update(status="blocked", reason="job_deadline_exceeded")
                ledger.save()
                return job
            if (root / "stop.json").exists():
                job.update(status="waiting", reason="operator_stop_requested")
                ledger.save()
                return job
            progress = progress_key(obs, job["capability"])
            if progress != job.get("progress"):
                job.update(progress=progress, progress_at_tick=obs["tick"], progress_at_time=time.time())
            if obs["tick"] - job["progress_at_tick"] > 18000:
                job.update(status="blocked", reason="objective_progress_stalled")
                ledger.save()
                return job
            started = time.perf_counter()
            result = executors.step(job["capability"], obs, job["parameters"])
            job["planning_seconds"] = job.get("planning_seconds", 0) + time.perf_counter() - started
            if result.get("type"):
                job.update(status="running", reason=result.get("reason", result["type"]),
                           pending_action=result)
                ledger.save()
                ledger.event(key, "action_intent", tick=obs["tick"], action=result)
                started = time.perf_counter()
                try:
                    receipt = game.act(result)
                except Exception:
                    # Durable intent survives connection errors; explicit
                    # reconciliation is necessary before further mutations.
                    job.update(status="blocked", reason="action_outcome_unknown_reconcile_required")
                    ledger.save()
                    raise
                job["execution_seconds"] = job.get("execution_seconds", 0) + time.perf_counter() - started
                job.pop("pending_action")
                job["actions"] += 1
                failure = None if receipt.get("ok") else receipt.get("reason", "action_failed")
                job["failures"] = job["failures"] + 1 if failure == job.get("last_failure") and failure else int(bool(failure))
                job["last_failure"] = failure
                if job["failures"] >= 3:
                    job.update(status="blocked", reason="repeated_executor_failure:" + str(failure))
                ledger.event(key, "action_result", tick=obs["tick"], result=receipt)
            else:
                status = result.get("status")
                if status not in {"running", "waiting", "blocked", "failed", "succeeded"}:
                    raise ValueError("executor returned no recognized action or status")
                job.update(status=status, reason=result.get("reason", ""), evidence=result.get("evidence", {}))
                if status == "succeeded":
                    fresh = game.observe()
                    ledger.sync(fresh)
                    proof = executors.proof(job["capability"], fresh, result, job["parameters"])
                    if proof is None:
                        job.update(status="blocked", reason="postcondition_not_verified", evidence={})
                    else:
                        job["evidence"] = proof
                    game.save()
            ledger.save()
            if job["status"] in TERMINAL:
                ledger.event(key, "terminal", status=job["status"], reason=job["reason"], evidence=job["evidence"])
                return job
            if time.monotonic() >= deadline:
                job.update(status="waiting", reason="execution_slice_complete")
                ledger.save()
                game.save()
                return job
            if not result.get("type") or result.get("type") == "wait":
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
