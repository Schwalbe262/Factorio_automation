"""Persistent observation/action loop with no runtime model dependencies."""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from .deterministic_game import DeterministicGame
from .deterministic_state import (RunLock, TaskResult, TaskStatus, load_run_state, _atomic_json,
    save_run_state, append_task_event, stop_requested, clear_stop)
from .world_catalog import WorldCatalog


def _write_json(path: Path, value: Any) -> None:
    _atomic_json(path, value)


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
        self.factory: Any = None
        self.fluids: Any = None
        self.energy: Any = None
        self.defense: Any = None
        self.armaments: Any = None
        self.rocket: Any = None
        self.navigator: Any = None
        self.attempt_started_tick: int | None = None

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
        if self.game.backend == "character":
            from .deterministic_navigation import CharacterNavigator
            self.navigator = CharacterNavigator(self.game)
            installed = self.navigator.install()
            if not installed.get("ok"):
                raise RuntimeError(installed.get("reason", "character_scenario_not_installed"))
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

    def prepare_production(self) -> None:
        if self.factory is None:
            from .deterministic_factory import DeterministicFactory
            from .deterministic_fluids import FluidProduction
            from .deterministic_energy import EnergyExpansion
            from .deterministic_defense import DeterministicDefense
            from .deterministic_armaments import Armaments
            from .deterministic_construction_materials import ConstructionMaterials
            self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
            self.builder.construction_materials = ConstructionMaterials(self.factory)
            self.fluids = FluidProduction(self.game, self.builder, self.catalog)
            self.factory.fluids = self.fluids
            self.fluids.factory = self.factory
            self.energy = EnergyExpansion(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
            self.defense = DeterministicDefense(self.game, self.bootstrap, self.catalog)
            self.armaments = Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
            self.defense.automatic_ammo = self.armaments.manages_turret

    def next_action(self, observation: dict[str, Any], until: str) -> dict[str, Any]:
        scope = getattr(type(self.bootstrap), "cell_survey_scope", None)
        if scope is None:
            return self._plan_action(observation, until)
        with scope(self.bootstrap):
            return self._plan_action(observation, until)

    def _plan_action(self, observation: dict[str, Any], until: str) -> dict[str, Any]:
        self.stage = "bootstrap"
        if self.builder is None:
            from .deterministic_builder import FactoryBuilder
            self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder._sync(observation)
        if until == "rocket" and ("power_sample_tick" in self.builder.state
                or self.builder.state.get("power_verified_once")
                or (self.root / "energy-expansion.json").exists()):
            # Restore automatic fuel ownership before bootstrap considers a
            # manual refill. Existing production must survive process restarts.
            self.prepare_production()
            self.factory._sync(observation)
        bootstrap_observation = {**observation, "entities": [e for e in observation.get("entities", [])
            if not self.builder.owns_automated_burner(e)
            and not (self.factory is not None and self.factory.owns_automated_burner(e))]}
        result = self.bootstrap.next_action(bootstrap_observation)
        if result.get("status") != "succeeded":
            return result
        if until == "bootstrap":
            return result
        # A healthy starter can research faster mining before funding expansion
        # for idle planned machines. Read current flow without issuing/discarding
        # a repair or seed action; cold supplies still enter energy recovery below.
        tech = observation.get("technologies", {})
        if (until == "rocket" and self.factory is not None and self.builder.state.get("power_verified_once")
                and tech.get("automation") and not tech.get("electric-mining-drill")
                and self.builder.state.get("power_plan") and self.builder.state.get("coal_plan")):
            from .deterministic_builder import plan_observed, power_flow_ready
            current = self.builder.power_evidence(self.builder.state["power_plan"], self.builder.state["coal_plan"])
            if (power_flow_ready(current) and all(plan_observed(observation, self.builder.state[key])
                    for key in ("power_plan", "coal_plan"))):
                bridge = self.factory.bootstrap_electric_mining(observation)
                if bridge and (bridge.get("type") or not bridge.get("evidence", {}).get("automatic_science_required")):
                    self.stage = "production"
                    return bridge
        # Repair established coal supply before starter verification can wait on
        # it. A saved controller also survives a cold restart or rollback after
        # the builder invalidates its transient flow proof; it reobserves assets.
        if until == "rocket" and self.energy is not None and (
                self.builder.state.get("power_verified_once")
                or self.energy.state.get("world_id") == observation.get("world_id")):
            self.stage = "energy"
            result = self.energy.next_action(observation)
            if result:
                return result
        self.stage = "power"
        result = self.builder.ensure_power(observation)
        if result.get("status") != "succeeded" or result.get("type"):
            return result
        if until == "power":
            return result
        self.stage = "production"
        self.prepare_production()
        self.factory.priority_research = self.defense.requirements(observation).get("research", [])
        armaments = self.armaments.next_action(observation)
        if armaments and (armaments.get("type") or armaments.get("status") in {"blocked", "failed"}):
            self.stage = "armaments"
            return armaments
        # Research can still advance before turrets unlock; urgent enemy pressure
        # or available defenses are handled before expanding exposed production.
        defense = self.defense.next_action(observation)
        if (defense.get("type") or defense.get("status") in {"blocked", "failed"}
                or (defense.get("evidence", {}).get("urgent") and defense.get("status") != "succeeded")):
            self.stage = "defense"
            return defense
        maintenance = self.fluids.maintain_coproducts(observation)
        if maintenance:
            return maintenance
        result = self.factory.next_action(observation)
        if result.get("status") != "succeeded" or result.get("type"):
            return result
        self.stage = "launch"
        if self.rocket is None:
            from .deterministic_rocket import DeterministicRocket
            self.rocket = DeterministicRocket(self.game, self.bootstrap, self.builder, self.catalog, self.factory)
        return self.rocket.next_action(observation)

    def write_status(self, observation: dict[str, Any], result: TaskResult, iteration: int) -> None:
        _write_json(self.root / "status.json", {
            "world_id": observation.get("world_id"), "stage": self.stage,
            "objective": "first-space-age-rocket", "backend": self.game.backend,
            "tick": observation.get("tick"), "cycle": iteration, **result.to_dict(),
            "inventory": observation.get("inventory", {}), "production": observation.get("production", {}),
            "technologies": sorted(observation.get("technologies", {})),
            "entities": observation.get("entities", []), "last_action": self.last_action,
            "crafting_client": self.client_status,
            "attempt_started_tick": self.attempt_started_tick,
            "model_calls": 0, "server_address": f"127.0.0.1:{self.game.cfg.server_port}"})

    def _observe_short_craft(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Refresh a just-started engine craft before expensive factory planning.

        A short craft can finish while the dependency walk still sees its old
        busy queue. Bound this grace period so maintenance is never held behind
        a long craft; the engine retains all material and timing authority.
        """
        world = observation.get("world_id")
        deadline = time.monotonic() + 2
        for _ in range(4):
            if (not observation.get("ok") or not observation.get("crafting_queue")
                    or observation.get("world_id") != world or time.monotonic() >= deadline
                    or stop_requested(self.root / "stop.json")):
                break
            time.sleep(.25)
            if time.monotonic() >= deadline or stop_requested(self.root / "stop.json"):
                break
            observation = self.game.observe()
        return observation

    def run(self, *, cycles: int = 0, until: str = "rocket", interval: float = 0.5) -> dict[str, Any]:
        if cycles < 0 or until not in {"bootstrap", "power", "rocket"}:
            raise ValueError("invalid cycle limit or milestone")
        with RunLock(self.root / "owner.lock"):
            self.attempt_started_tick = None
            clear_stop(self.root / "stop.json")
            observation: dict[str, Any] = {}
            last_save = last_print = time.monotonic()
            result = TaskResult(TaskStatus.RUNNING, "starting")
            iteration = 0
            try:
                observation = self.prepare()
                self.attempt_started_tick = int(observation["tick"])
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
                    pending = self.navigator.pending_action() if self.navigator is not None else None
                    choice = pending or self.next_action(observation, until)
                    if "type" in choice:
                        self.last_action = choice
                        outcome = self.navigator.execute(choice, observation) if self.navigator is not None else self.game.act(choice)
                        status = TaskStatus.RUNNING if outcome.get("ok") else TaskStatus.FAILED
                        if outcome.get("status") == "waiting":
                            status = TaskStatus.WAITING
                        elif outcome.get("status") == "blocked":
                            status = TaskStatus.BLOCKED
                        result = TaskResult(status, outcome.get("reason", choice.get("reason", choice["type"])),
                                            {"action": choice, "result": outcome})
                    else:
                        result = TaskResult(choice.get("status", "blocked"), choice.get("reason", ""),
                                            choice.get("evidence", {}))
                    completion_stage = {"bootstrap": "bootstrap", "power": "power", "rocket": "launch"}[until]
                    if result.status == TaskStatus.SUCCEEDED and self.stage != completion_stage:
                        result = TaskResult(TaskStatus.WAITING, "component ready; requested milestone continues",
                                            {"component": result.to_dict(), "requested_milestone": until})
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
                    if result.status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.SUCCEEDED}:
                        break
                    # Fuel chores temporarily select bootstrap while a later block
                    # is growing. Track the objective's evidence across those chores.
                    overall_progress = {stage: task.progress_key for stage, task in self.state.tasks.items()
                                        if stage != "objective"}
                    self.state.record_task("objective", result, tick=observation["tick"], progress=overall_progress)
                    # An explicit resume gets one finite retry window after code
                    # repairs. Keep the old gameplay-progress evidence unchanged.
                    stall_limit = 60 * 60 * 10
                    if (observation["tick"] - self.attempt_started_tick >= stall_limit
                            and self.state.is_stalled("objective", tick=observation["tick"], max_stall_ticks=stall_limit)):
                        result = TaskResult(TaskStatus.BLOCKED, "no objective progress for ten game minutes",
                            {"attempt_started_tick": self.attempt_started_tick,
                             "last_progress_tick": self.state.tasks["objective"].last_progress_tick})
                        break
                    if now-last_save >= 30:
                        self.game.save()
                        last_save = now
                    time.sleep(interval)
                    observation = self.game.observe()
                    if choice.get("type") == "craft":
                        observation = self._observe_short_craft(observation)
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
                stop_actor = self.navigator.stop if self.navigator is not None else lambda: self.game.act({"type": "stop"})
                for cleanup in (stop_actor, self.game.save):
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
                "installed": [(e.get("unit_number"), e.get("name"), e.get("recipe")) for e in observation.get("entities", [])
                              if not e.get("name", "").startswith("crash-site")],
                "technologies": sorted(observation.get("technologies", {})), "rockets_launched": observation.get("rockets_launched", 0)}
