"""Persistent observation/action loop with no runtime model dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from .deterministic_game import DeterministicGame
from .deterministic_state import (RunLock, TaskResult, TaskStatus, load_run_state,
    save_run_state, append_task_event, stop_requested, clear_stop)
from .world_catalog import WorldCatalog


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class DeterministicSupervisor:
    def __init__(self, game: DeterministicGame):
        self.game = game
        self.root = game.cfg.runtime_dir
        self.stage = "bootstrap"
        self.bootstrap: Any = None
        self.builder: Any = None
        self.catalog: WorldCatalog | None = None
        self.last_action: dict[str, Any] | None = None
        self.state: Any = None
        self.client_status: dict[str, Any] = {}

    def connect_character(self) -> dict[str, Any]:
        from .deterministic_character import ensure_crafting_player
        deadline = time.monotonic() + 125
        while True:
            if stop_requested(self.root / "stop.json"):
                raise InterruptedError("operator_stop_requested")
            self.client_status = ensure_crafting_player(self.game)
            if self.client_status.get("status") == "ready":
                return self.client_status
            if self.client_status.get("status") != "running" or time.monotonic() >= deadline:
                raise RuntimeError(self.client_status.get("reason", "dedicated_client_connection_failed"))
            self.write_status({}, TaskResult(TaskStatus.WAITING, self.client_status.get("reason", "connecting")), 0)
            time.sleep(0.4)

    def prepare(self) -> dict[str, Any]:
        self.stage = "connecting"
        existing = self.game.query("return {initialized=storage.deterministic_player~=nil}")
        if "initialized" not in existing:
            raise RuntimeError(existing.get("reason", "world_probe_failed"))
        if not existing["initialized"]:
            initialized = self.game.initialize()
            if not initialized.get("ok"):
                raise RuntimeError(initialized.get("reason", "initialization_failed"))
        # An offline player's LuaEntity reference can be invalid. Rebind the exact
        # dedicated player before initialize/observe; never create another actor.
        self.connect_character()
        initialized = self.game.initialize()
        if not initialized.get("ok"):
            raise RuntimeError(initialized.get("reason", "initialization_failed"))
        self.catalog = WorldCatalog.from_game(self.game.query)
        _write_json(self.root / "catalog.json", self.catalog.to_dict())
        _write_json(self.root / "rocket-requirements.json", self.catalog.first_rocket_bom())
        from .deterministic_bootstrap import DeterministicBootstrap
        self.bootstrap = DeterministicBootstrap(self.game, self.catalog)
        observation = self.game.observe()
        if not observation.get("ok"):
            raise RuntimeError(observation.get("reason", "observation_failed"))
        self.state = load_run_state(self.root / "checkpoint.json", world_id=observation["world_id"],
                                    game_fingerprint=self.catalog.fingerprint, tick=observation["tick"])
        return observation

    def next_action(self, observation: dict[str, Any], until: str) -> dict[str, Any]:
        self.stage = "bootstrap"
        if self.builder is None:
            from .deterministic_builder import FactoryBuilder
            self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        bootstrap_observation = {**observation, "entities": [e for e in observation.get("entities", [])
            if not self.builder.owns_automated_burner(e)]}
        result = self.bootstrap.next_action(bootstrap_observation)
        if result.get("status") != "succeeded":
            return result
        if until == "bootstrap":
            return result
        self.stage = "power"
        result = self.builder.ensure_power(observation)
        if result.get("status") != "succeeded" or result.get("type"):
            return result
        if until == "power":
            return result
        self.stage = "production"
        return {"status": "blocked", "reason": "production network executor is not installed yet",
                "evidence": {"missing_capability": "production_network"}}

    def write_status(self, observation: dict[str, Any], result: TaskResult, iteration: int) -> None:
        _write_json(self.root / "status.json", {
            "world_id": observation.get("world_id"), "stage": self.stage,
            "objective": "first-space-age-rocket", "backend": self.game.backend,
            "tick": observation.get("tick"), "cycle": iteration, **result.to_dict(),
            "inventory": observation.get("inventory", {}), "production": observation.get("production", {}),
            "technologies": sorted(observation.get("technologies", {})),
            "entities": observation.get("entities", []), "last_action": self.last_action,
            "crafting_client": self.client_status,
            "model_calls": 0, "server_address": f"127.0.0.1:{self.game.cfg.server_port}"})

    def run(self, *, cycles: int = 0, until: str = "rocket", interval: float = 0.5) -> dict[str, Any]:
        if cycles < 0 or until not in {"bootstrap", "power", "rocket"}:
            raise ValueError("invalid cycle limit or milestone")
        with RunLock(self.root / "owner.lock"):
            clear_stop(self.root / "stop.json")
            observation: dict[str, Any] = {}
            last_save = last_print = time.monotonic()
            result = TaskResult(TaskStatus.RUNNING, "starting")
            iteration = 0
            try:
                observation = self.prepare()
                while not cycles or iteration < cycles:
                    if stop_requested(self.root / "stop.json"):
                        result = TaskResult(TaskStatus.WAITING, "operator_stop_requested")
                        break
                    iteration += 1
                    from .deterministic_character import ensure_crafting_player
                    self.client_status = ensure_crafting_player(self.game)
                    if self.client_status.get("status") != "ready":
                        self.stage = "connecting"
                        self.connect_character()
                        observation = self.game.observe()
                    if not observation.get("ok"):
                        result = TaskResult(TaskStatus.FAILED, observation.get("reason", "observation_failed"))
                        break
                    self.state.reconcile(observation["world_id"], self.catalog.fingerprint, observation["tick"])
                    choice = self.next_action(observation, until)
                    if "type" in choice:
                        self.last_action = choice
                        outcome = self.game.act(choice)
                        status = TaskStatus.RUNNING if outcome.get("ok") else TaskStatus.FAILED
                        if outcome.get("status") == "waiting":
                            status = TaskStatus.WAITING
                        result = TaskResult(status, outcome.get("reason", choice.get("reason", choice["type"])),
                                            {"action": choice, "result": outcome})
                    else:
                        result = TaskResult(choice.get("status", "blocked"), choice.get("reason", ""),
                                            choice.get("evidence", {}))
                    # Technology/required manufacturing evidence only; coal accumulation
                    # and unrelated character inventory changes cannot mask a stall.
                    progress = self.progress_evidence(observation)
                    result = self.state.record_task(self.stage, result, tick=observation["tick"], progress=progress)
                    append_task_event(self.game.cfg.log_dir / "supervisor.jsonl", self.state, self.stage, result)
                    self.write_status(observation, result, iteration)
                    save_run_state(self.root / "checkpoint.json", self.state)
                    now = time.monotonic()
                    if now-last_print >= 15:
                        print(json.dumps({"stage": self.stage, "status": result.status.value, "reason": result.reason,
                                          "tick": observation["tick"], "entities": len(observation.get("entities", []))}), flush=True)
                        last_print = now
                    if result.status in {TaskStatus.BLOCKED, TaskStatus.SUCCEEDED}:
                        break
                    # Fuel chores temporarily select bootstrap while a later block
                    # is growing. Track the objective's evidence across those chores.
                    overall_progress = {stage: task.progress_key for stage, task in self.state.tasks.items()
                                        if stage != "objective"}
                    self.state.record_task("objective", result, tick=observation["tick"], progress=overall_progress)
                    if self.state.is_stalled("objective", tick=observation["tick"], max_stall_ticks=60*60*10):
                        result = TaskResult(TaskStatus.BLOCKED, "no objective progress for ten game minutes")
                        break
                    if now-last_save >= 30:
                        self.game.save()
                        last_save = now
                    time.sleep(interval)
                    observation = self.game.observe()
                else:
                    result = TaskResult(TaskStatus.WAITING, "cycle_limit_reached")
            except KeyboardInterrupt:
                result = TaskResult(TaskStatus.WAITING, "operator_interrupted")
            except InterruptedError as exc:
                result = TaskResult(TaskStatus.WAITING, str(exc))
            except Exception as exc:
                result = TaskResult(TaskStatus.FAILED, str(exc), {"exception_type": type(exc).__name__})
            finally:
                cleanup_errors = []
                for cleanup in (lambda: self.game.act({"type": "stop"}), self.game.save):
                    try:
                        cleanup()
                    except Exception as exc:
                        cleanup_errors.append(str(exc))
                if cleanup_errors:
                    result = TaskResult(TaskStatus.FAILED, "run cleanup failed",
                                        {"prior_result": result.to_dict(), "errors": cleanup_errors})
                if self.state is not None:
                    save_run_state(self.root / "checkpoint.json", self.state)
                self.write_status(observation, result, iteration)
            return {"stage": self.stage, **result.to_dict(), "cycles": iteration}

    def progress_evidence(self, observation: dict[str, Any]) -> dict[str, Any]:
        if self.stage == "bootstrap":
            production = observation.get("production", {})
            return {"technologies": [t for t in ("steam-power", "electronics", "automation-science-pack")
                                      if observation.get("technologies", {}).get(t)],
                    "iron": min(50, production.get("iron-plate", {}).get("produced", 0)),
                    "copper": min(10, production.get("copper-plate", {}).get("produced", 0)),
                    "drills": min(4, sum(e.get("name") == "burner-mining-drill" for e in observation.get("entities", []))),
                    "lab": bool(observation.get("inventory", {}).get("lab"))}
        if self.stage == "power":
            return {"installed": [(e.get("unit_number"), e.get("name")) for e in observation.get("entities", [])
                                   if e.get("name") in {"boiler", "steam-engine", "offshore-pump", "transport-belt", "pipe", "burner-inserter"}],
                    "water": any(e.get("name") == "boiler" and e.get("fluids", {}).get("water", 0) > 0 for e in observation.get("entities", [])),
                    "steam": any(e.get("name") == "steam-engine" and e.get("fluids", {}).get("steam", 0) > 0 for e in observation.get("entities", []))}
        return {"research": observation.get("research"), "research_progress": observation.get("research_progress"),
                "technologies": sorted(observation.get("technologies", {})), "rockets_launched": observation.get("rockets_launched", 0)}
