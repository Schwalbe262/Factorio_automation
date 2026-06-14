from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from .llm_log import record_llm_decision, strategy_request_summary
from .config import AppConfig, REPO_ROOT
from .layout_validation import layout_validation_feedback_summary
from .models import (
    ActionValidationError,
    PlannerDecision,
    distance,
    player_position,
    total_item_count,
    validate_action,
)
from .modless_lua import ModlessLuaController
from .monitor import summarize_factory
from .run_journal import (
    record_autopilot_cycle_journal,
    record_layout_loop_journal,
    record_layout_result_insight,
    record_skill_run_journal,
)
from .planner import (
    AutomationScienceSkill,
    BeltSmeltingLineSkill,
    BuildItemMallSkill,
    CircuitAutomationSkill,
    CoalFuelFeedSkill,
    CoalSupplySkill,
    CopperPlateSkill,
    ElectronicCircuitSkill,
    ExpandCopperSmeltingSkill,
    ExpandIronSmeltingSkill,
    FactoryLayoutImprovementSkill,
    GearBeltMallLogisticsSkill,
    IronPlateSkill,
    ResearchAutomationSkill,
    ResearchTechnologySkill,
    SetupPowerSkill,
    StoneSupplySkill,
    StarterDefenseSkill,
)
from .rcon import FactorioRconClient
from .site_selection import load_selected_improvement_site
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy, make_strategy_payload, reconcile_strategy_decision, skill_catalog_payload
from .targets import load_targets
from .world_memory import (
    load_world_map_memory,
    merge_world_map_memory_into_observation,
    planning_sites_are_fresh,
    planning_sites_from_memory,
    update_world_map_memory,
)


@dataclass
class RunSummary:
    ok: bool
    reason: str
    steps: int
    item_count: int
    log_path: Path
    item_name: str

    @property
    def iron_plate_count(self) -> int:
        return self.item_count

    @property
    def copper_plate_count(self) -> int:
        return self.item_count

    @property
    def electronic_circuit_count(self) -> int:
        return self.item_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "steps": self.steps,
            "itemCount": self.item_count,
            "itemName": self.item_name,
            "logPath": str(self.log_path),
        }


@dataclass
class StrategyStepSummary:
    ok: bool
    reason: str
    objective: str
    selected_skill: str
    strategy: dict[str, Any]
    run: RunSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "selectedSkill": self.selected_skill,
            "strategy": self.strategy,
            "run": self.run.to_dict() if self.run else None,
        }


@dataclass
class AutopilotLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    log_path: Path
    last_step: StrategyStepSummary | None = None
    failures: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "logPath": str(self.log_path),
            "lastStep": self.last_step.to_dict() if self.last_step else None,
            "failures": self.failures,
            "interrupted": self.interrupted,
        }


@dataclass
class CodexWaitLayoutLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    log_path: Path
    active_skill: str = ""
    wait_active: bool = False
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "logPath": str(self.log_path),
            "activeSkill": self.active_skill,
            "waitActive": self.wait_active,
            "interrupted": self.interrupted,
        }


@dataclass
class IdleLayoutLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    idle_cycles: int
    busy_cycles: int
    log_path: Path
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "idleCycles": self.idle_cycles,
            "busyCycles": self.busy_cycles,
            "logPath": str(self.log_path),
            "interrupted": self.interrupted,
        }


_PLANNING_SITE_RETRY_MARKERS = (
    "cannot find a buildable water site",
    "cannot find a powered or wireable lab site",
    "cannot find a powered or wireable site",
)
DEFAULT_PLANNING_SITE_CACHE_SECONDS = 180.0


def _planning_site_retry_needed(decision: PlannerDecision) -> bool:
    if decision.done or decision.action is not None:
        return False
    reason = decision.reason.lower()
    return any(marker in reason for marker in _PLANNING_SITE_RETRY_MARKERS)


def _planning_site_cache_seconds() -> float:
    try:
        return max(
            0.0,
            float(os.getenv("FACTORIO_AI_PLANNING_SITE_CACHE_SECONDS", str(DEFAULT_PLANNING_SITE_CACHE_SECONDS))),
        )
    except (TypeError, ValueError):
        return DEFAULT_PLANNING_SITE_CACHE_SECONDS


def _automation_researched_in_observation(observation: dict[str, Any]) -> bool:
    research = observation.get("research")
    if not isinstance(research, dict):
        return False
    technologies = research.get("technologies")
    if not isinstance(technologies, dict):
        return False
    automation = technologies.get("automation")
    return bool(isinstance(automation, dict) and automation.get("researched"))


def _guard_post_automation_handcraft(observation: dict[str, Any], decision: PlannerDecision) -> PlannerDecision:
    action = decision.action
    if (
        isinstance(action, dict)
        and action.get("type") == "craft"
        and action.get("recipe") == "iron-gear-wheel"
        and _automation_researched_in_observation(observation)
    ):
        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            "blocked direct iron-gear-wheel handcraft after Automation; use gear mall or a logistic line instead",
        )
    return decision


class FactorioController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._remote_llm_status_cache: dict[str, Any] | None = None
        self._remote_llm_status_cache_until = 0.0
        self._background_layout_task_name: str | None = None
        self._background_layout_last_submit = 0.0
        self._background_layout_thread: threading.Thread | None = None
        self._background_layout_thread_result: dict[str, Any] | None = None

    def observe(self) -> dict[str, Any]:
        with self._client() as client:
            response = client.execute_json_command("ai_observe", self._agent_parameter())
        if not response.get("ok"):
            raise RuntimeError(f"observe failed: {response}")
        return response

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
        action = self._agent_action(action)
        with self._client() as client:
            response = client.execute_json_command("ai_action", action)
        return response

    def stop_agent(self) -> dict[str, Any]:
        try:
            response = self.act({"type": "stop"})
            if response.get("ok"):
                return response
        except Exception as exc:
            response = {"ok": False, "reason": str(exc)}

        observation = self.observe()
        current_position = player_position(observation)
        fallback = self.act({"type": "move_to", "position": current_position})
        fallback["fallbackFor"] = "stop"
        fallback["previousStopResponse"] = response
        return fallback

    def wait(self, ticks: int) -> dict[str, Any]:
        with self._client() as client:
            response = client.execute_json_command("ai_wait", str(ticks))
        time.sleep(max(0.05, ticks / 60.0))
        return response

    def run_iron_mvp(
        self,
        target: int = 10,
        max_steps: int = 200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=IronPlateSkill(target),
            target_item="iron-plate",
            target=target,
            goal="produce_iron_plate",
            max_steps=max_steps,
            log_prefix="iron-mvp",
            log_path=log_path,
        )

    def run_science_mvp(
        self,
        target: int = 5,
        max_steps: int = 400,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=AutomationScienceSkill(target),
            target_item="automation-science-pack",
            target=target,
            goal="produce_automation_science_pack",
            max_steps=max_steps,
            log_prefix="science-mvp",
            log_path=log_path,
        )

    def run_copper_mvp(
        self,
        target: int = 10,
        max_steps: int = 250,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=CopperPlateSkill(target),
            target_item="copper-plate",
            target=target,
            goal="produce_copper_plate",
            max_steps=max_steps,
            log_prefix="copper-mvp",
            log_path=log_path,
        )

    def run_circuit_mvp(
        self,
        target: int = 5,
        max_steps: int = 500,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ElectronicCircuitSkill(target),
            target_item="electronic-circuit",
            target=target,
            goal="produce_electronic_circuit",
            max_steps=max_steps,
            log_prefix="circuit-mvp",
            log_path=log_path,
        )

    def run_belt_smelting_mvp(
        self,
        target: int = 10,
        max_steps: int = 700,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=BeltSmeltingLineSkill(target),
            target_item="iron-plate",
            target=target,
            goal="build_belt_smelting_line",
            max_steps=max_steps,
            log_prefix="belt-smelting-mvp",
            log_path=log_path,
        )

    def run_power_mvp(
        self,
        max_steps: int = 900,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=SetupPowerSkill(),
            target_item="steam",
            target=1,
            goal="setup_power",
            max_steps=max_steps,
            log_prefix="power-mvp",
            log_path=log_path,
        )

    def run_automation_research_mvp(
        self,
        max_steps: int = 1500,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ResearchAutomationSkill(),
            target_item="automation-science-pack",
            target=10,
            goal="research_automation",
            max_steps=max_steps,
            log_prefix="automation-research-mvp",
            log_path=log_path,
        )

    def run_circuit_automation_mvp(
        self,
        target: int = 5,
        max_steps: int = 1800,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=CircuitAutomationSkill(target),
            target_item="electronic-circuit",
            target=target,
            goal="automate_electronic_circuit_line",
            max_steps=max_steps,
            log_prefix="circuit-automation-mvp",
            log_path=log_path,
        )

    def run_logistics_research_mvp(
        self,
        max_steps: int = 2200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ResearchTechnologySkill("logistics"),
            target_item="automation-science-pack",
            target=20,
            goal="research_logistics",
            max_steps=max_steps,
            log_prefix="logistics-research-mvp",
            log_path=log_path,
        )

    def run_build_item_mall_mvp(
        self,
        target_item: str = "transport-belt",
        target: int = 20,
        max_steps: int = 1200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=BuildItemMallSkill(target_item, target),
            target_item=target_item,
            target=target,
            goal="bootstrap_build_item_mall",
            max_steps=max_steps,
            log_prefix=f"build-item-mall-{target_item}",
            log_path=log_path,
        )

    def run_expand_iron_smelting_mvp(
        self,
        target_rate: int = 90,
        max_steps: int = 2000,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ExpandIronSmeltingSkill(float(target_rate)),
            target_item="iron-plate",
            target=target_rate,
            goal="expand_iron_smelting",
            max_steps=max_steps,
            log_prefix="expand-iron-smelting-mvp",
            log_path=log_path,
        )

    def run_expand_copper_smelting_mvp(
        self,
        target_rate: int = 75,
        max_steps: int = 1600,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ExpandCopperSmeltingSkill(float(target_rate)),
            target_item="copper-plate",
            target=target_rate,
            goal="expand_copper_smelting",
            max_steps=max_steps,
            log_prefix="expand-copper-smelting-mvp",
            log_path=log_path,
        )

    def strategy_decision(self, objective: str, require_llm: bool = False) -> dict[str, Any]:
        observation = self.observe()
        production_targets = load_targets(self.cfg.runtime_dir, objective).per_minute
        selected_improvement_site = load_selected_improvement_site(self.cfg.runtime_dir, objective)
        request_summary = strategy_request_summary(observation, production_targets)
        result: dict[str, Any] | None = None
        if self.cfg.slurm_enabled:
            self._maybe_ensure_slurm_worker(reason="strategy_decision")
            started = time.monotonic()
            try:
                status = self._remote_llm_status(refresh=True)
                if not status.get("llm_ready"):
                    reason = self._remote_llm_unready_reason(status)
                    record_llm_decision(
                        self.cfg.log_dir,
                        objective=objective,
                        provider="remote_slurm",
                        result={"source": "remote_unavailable", "ok": False},
                        request_summary=request_summary,
                        error=reason,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                    if require_llm:
                        raise RuntimeError(f"remote Slurm LLM not ready: {reason}")
                else:
                    from . import remote_slurm

                    result = remote_slurm.request_strategy(
                        objective=objective,
                        observation=observation,
                        production_targets=production_targets,
                        selected_improvement_site=selected_improvement_site,
                        available_skills=skill_catalog_payload(),
                        timeout_seconds=30,
                    )
                    record_llm_decision(
                        self.cfg.log_dir,
                        objective=objective,
                        provider="remote_slurm",
                        result=result,
                        request_summary=request_summary,
                        error="" if result.get("source") == "llm" else "remote Slurm returned non-LLM fallback result",
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
            except Exception as exc:
                if require_llm and str(exc).startswith("remote Slurm LLM not ready:"):
                    raise
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="remote_slurm",
                    result=None,
                    request_summary=request_summary,
                    error=f"remote Slurm strategy request failed: {type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if require_llm:
                    raise

        if result is None:
            started = time.monotonic()
            try:
                from .slurm_worker import run_strategy_request

                result = run_strategy_request(
                    make_strategy_payload(
                        objective,
                        observation,
                        production_targets,
                        selected_improvement_site=selected_improvement_site,
                    )
                )
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="local_llm",
                    result=result,
                    request_summary=request_summary,
                    error="" if result.get("source") == "llm" else "LLM unavailable or invalid response; used heuristic fallback",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="local_llm",
                    result=None,
                    request_summary=request_summary,
                    error=f"local LLM strategy request failed: {type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if require_llm:
                    raise
                result = heuristic_strategy(
                    objective,
                    observation,
                    production_targets,
                    selected_improvement_site=selected_improvement_site,
                )
                result["source"] = "heuristic"
                result["ok"] = True
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="heuristic_fallback",
                    result=result,
                    request_summary=request_summary,
                    error="strategy fell back to local heuristic",
                    latency_ms=0,
                )

        before_guardrail = dict(result)
        result = reconcile_strategy_decision(result, objective, observation, production_targets)
        if result.get("guardrail_adjusted") and result != before_guardrail:
            record_llm_decision(
                self.cfg.log_dir,
                objective=objective,
                provider="strategy_guardrail",
                result=result,
                request_summary=request_summary,
                error="LLM strategy adjusted by deterministic feasibility guardrail",
                latency_ms=0,
            )

        if require_llm and result.get("source") != "llm":
            raise RuntimeError(f"LLM strategy was required but source was {result.get('source')}")
        return annotate_strategy_with_skill_status(result, runtime_dir=self.cfg.runtime_dir)

    def run_strategy_step(
        self,
        objective: str = "launch_rocket_program",
        require_llm: bool = False,
        target_count: int | None = None,
        max_steps: int | None = None,
    ) -> StrategyStepSummary:
        strategy = self.strategy_decision(objective, require_llm=require_llm)
        selected = str(strategy.get("selected_skill") or strategy.get("selected_goal") or "")
        status = strategy.get("skill_status") if isinstance(strategy.get("skill_status"), dict) else {}
        if status.get("codex_required"):
            self._record_codex_wait_state(
                objective,
                selected,
                "executor missing; waiting for Codex implementation",
                strategy,
            )
            self._maybe_progress_background_layout_for_blocked_strategy(
                objective,
                selected,
                "executor missing; waiting for Codex implementation",
            )
            return StrategyStepSummary(
                ok=False,
                reason=f"executor missing for selected skill: {selected}",
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
            )

        config = self._skill_run_config(selected, target_count=target_count, max_steps=max_steps)
        if config is None:
            self._record_codex_wait_state(
                objective,
                selected,
                "selected skill has no local runner; waiting for Codex implementation",
                strategy,
            )
            self._maybe_progress_background_layout_for_blocked_strategy(
                objective,
                selected,
                "selected skill has no local runner; waiting for Codex implementation",
            )
            return StrategyStepSummary(
                ok=False,
                reason=f"selected skill is not executable by the local runner: {selected}",
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
            )
        self._clear_codex_wait_state(selected)
        config["objective"] = objective
        run = self._run_skill(**config)
        return StrategyStepSummary(
            ok=run.ok,
            reason=run.reason,
            objective=objective,
            selected_skill=selected,
            strategy=strategy,
            run=run,
        )

    def run_autopilot_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        require_llm: bool = False,
        target_count: int | None = None,
        max_steps: int | None = None,
        cycles: int = 0,
        sleep_seconds: float = 5.0,
        continue_on_error: bool = True,
        log_path: Path | None = None,
    ) -> AutopilotLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"autopilot-{_timestamp()}.jsonl"
        completed = 0
        failures = 0
        last_step: StrategyStepSummary | None = None
        interrupted = False
        reason = "cycle limit reached" if cycles > 0 else "autopilot loop stopped"
        self._maybe_ensure_slurm_worker(reason="autopilot_start", force=True)
        self._write_autopilot_heartbeat(objective, "starting", cycle=0, reason=reason)

        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                while cycles <= 0 or completed < cycles:
                    self._maybe_ensure_slurm_worker(reason="autopilot_cycle")
                    self._write_autopilot_heartbeat(objective, "cycle_start", cycle=completed + 1)
                    self._maybe_progress_codex_wait_layout(objective)
                    started = time.monotonic()
                    try:
                        last_step = self.run_strategy_step(
                            objective=objective,
                            require_llm=require_llm,
                            target_count=target_count,
                            max_steps=max_steps,
                        )
                    except Exception as exc:
                        last_step = StrategyStepSummary(
                            ok=False,
                            reason=f"strategy cycle failed: {type(exc).__name__}: {exc}",
                            objective=objective,
                            selected_skill="",
                            strategy={},
                        )
                        self._maybe_progress_codex_wait_layout(objective, phase="cycle_error")
                        self._write_autopilot_heartbeat(
                            objective,
                            "cycle_error",
                            cycle=completed + 1,
                            reason=last_step.reason,
                        )
                    completed += 1
                    if not last_step.ok:
                        failures += 1
                    duration_seconds = round(time.monotonic() - started, 3)
                    payload = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "cycle": completed,
                        "objective": objective,
                        "ok": last_step.ok,
                        "duration_seconds": duration_seconds,
                        "step": last_step.to_dict(),
                    }
                    json.dump(payload, log_file, ensure_ascii=False, separators=(",", ":"))
                    log_file.write("\n")
                    log_file.flush()
                    record_autopilot_cycle_journal(
                        self.cfg.log_dir,
                        objective=objective,
                        cycle=completed,
                        selected_skill=last_step.selected_skill,
                        ok=last_step.ok,
                        reason=last_step.reason,
                        duration_seconds=duration_seconds,
                        strategy=last_step.strategy,
                        log_path=log_path,
                        repo_root=self._journal_repo_root(),
                    )
                    self._write_autopilot_heartbeat(
                        objective,
                        "cycle_complete" if last_step.ok else "cycle_failed",
                        cycle=completed,
                        reason=last_step.reason,
                    )
                    if not last_step.ok and not continue_on_error:
                        reason = last_step.reason
                        break
                    if cycles <= 0 or completed < cycles:
                        self._write_autopilot_heartbeat(
                            objective,
                            "sleeping",
                            cycle=completed,
                            reason=f"sleeping {sleep_seconds} seconds before next strategy cycle",
                        )
                        time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "autopilot interrupted by user"
            self._write_autopilot_heartbeat(objective, "interrupted", cycle=completed, reason=reason)

        ok = failures == 0 or (continue_on_error and cycles <= 0 and not interrupted)
        if cycles > 0 and completed >= cycles and failures == 0:
            reason = "cycle limit reached"
        elif failures > 0 and continue_on_error:
            reason = f"completed with {failures} failed cycle(s); continuing is enabled"
        self._write_autopilot_heartbeat(
            objective,
            "stopped" if not interrupted else "interrupted",
            cycle=completed,
            reason=reason,
        )
        return AutopilotLoopSummary(
            ok=ok,
            reason=reason,
            objective=objective,
            cycles=completed,
            log_path=log_path,
            last_step=last_step,
            failures=failures,
            interrupted=interrupted,
        )

    def run_codex_wait_layout_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        cycles: int = 0,
        sleep_seconds: float = 20.0,
    ) -> CodexWaitLayoutLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        completed = 0
        interrupted = False
        active_skill = ""
        reason = "no active Codex wait state"

        try:
            while cycles <= 0 or completed < cycles:
                state = self._read_codex_wait_state()
                if not state.get("active"):
                    break
                active_skill = str(state.get("active_skill") or f"codex_wait:{state.get('selected_skill') or ''}")
                wait_objective = str(state.get("objective") or objective)
                self._maybe_progress_codex_wait_layout(wait_objective, phase="wait_loop")
                completed += 1
                reason = "cycle limit reached" if cycles > 0 and completed >= cycles else "Codex wait layout loop running"
                record_layout_loop_journal(
                    self.cfg.log_dir,
                    loop_type="codex_wait_layout_cycle",
                    objective=wait_objective,
                    cycle=completed,
                    active_skill=active_skill,
                    ok=True,
                    reason=reason,
                    log_path=log_path,
                    metadata={"wait_active": True},
                    repo_root=self._journal_repo_root(),
                )
                if cycles > 0 and completed >= cycles:
                    break
                time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "Codex wait layout loop interrupted by user"

        state = self._read_codex_wait_state()
        wait_active = bool(state.get("active"))
        if completed > 0 and not wait_active and not interrupted:
            reason = "Codex wait state cleared"
        return CodexWaitLayoutLoopSummary(
            ok=not interrupted,
            reason=reason,
            objective=objective,
            cycles=completed,
            log_path=log_path,
            active_skill=active_skill,
            wait_active=wait_active,
            interrupted=interrupted,
        )

    def run_idle_layout_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        cycles: int = 0,
        sleep_seconds: float = 5.0,
        stale_seconds: float = 15.0,
        min_submit_interval_seconds: float = 0.0,
    ) -> IdleLayoutLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        self._maybe_ensure_slurm_worker(reason="idle_layout_start", force=True)
        process_path = self._idle_layout_process_path()
        existing = _read_json_file(process_path)
        existing_pid = _int_or_none(existing.get("pid")) if isinstance(existing, dict) else None
        if existing_pid and existing_pid != os.getpid() and _pid_is_running(existing_pid):
            self._write_background_layout_log(
                {
                    "event": "layout_idle_loop_already_running",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )
            return IdleLayoutLoopSummary(
                ok=True,
                reason="idle layout loop already running",
                objective=objective,
                cycles=0,
                idle_cycles=0,
                busy_cycles=0,
                log_path=log_path,
            )
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        with process_path.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "objective": objective,
                    "state": "running",
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
        completed = 0
        idle_cycles = 0
        busy_cycles = 0
        interrupted = False
        reason = "idle layout loop running"

        try:
            while cycles <= 0 or completed < cycles:
                self._maybe_ensure_slurm_worker(reason="idle_layout_cycle")
                idle, idle_reason, heartbeat = self._autopilot_idle_for_layout(stale_seconds)
                completed += 1
                if idle:
                    idle_cycles += 1
                    try:
                        observation = self._observe_for_idle_layout()
                    except Exception as exc:  # noqa: BLE001
                        self._write_background_layout_log(
                            {
                                "event": "layout_idle_observe_failed",
                                "active_skill": "idle:observe_failed",
                                "active_step": 0,
                                "idle_reason": idle_reason,
                                "heartbeat": heartbeat,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        record_layout_loop_journal(
                            self.cfg.log_dir,
                            loop_type="idle_layout_cycle",
                            objective=objective,
                            cycle=completed,
                            active_skill="idle:observe_failed",
                            ok=False,
                            reason=f"observe failed: {type(exc).__name__}: {exc}",
                            log_path=log_path,
                            metadata={"idle_reason": idle_reason},
                            repo_root=self._journal_repo_root(),
                        )
                    else:
                        active_skill = f"idle:{_slugify_reason(idle_reason)}"
                        self._write_background_layout_log(
                            {
                                "event": "layout_idle_scheduler_heartbeat",
                                "active_skill": active_skill,
                                "active_step": 0,
                                "idle_reason": idle_reason,
                                "heartbeat": heartbeat,
                            }
                        )
                        record_layout_loop_journal(
                            self.cfg.log_dir,
                            loop_type="idle_layout_cycle",
                            objective=objective,
                            cycle=completed,
                            active_skill=active_skill,
                            ok=True,
                            reason=idle_reason,
                            log_path=log_path,
                            metadata={"idle": True},
                            repo_root=self._journal_repo_root(),
                        )
                        self._maybe_progress_background_layout_work(
                            observation,
                            objective,
                            active_skill,
                            0,
                            minimum_interval_seconds=min_submit_interval_seconds,
                        )
                else:
                    busy_cycles += 1
                    self._write_background_layout_log(
                        {
                            "event": "layout_idle_scheduler_busy",
                            "active_skill": "autopilot",
                            "active_step": 0,
                            "idle_reason": idle_reason,
                            "heartbeat": heartbeat,
                        }
                    )
                    record_layout_loop_journal(
                        self.cfg.log_dir,
                        loop_type="idle_layout_cycle",
                        objective=objective,
                        cycle=completed,
                        active_skill="autopilot",
                        ok=True,
                        reason=idle_reason,
                        log_path=log_path,
                        metadata={"idle": False},
                        repo_root=self._journal_repo_root(),
                    )
                reason = "cycle limit reached" if cycles > 0 and completed >= cycles else "idle layout loop running"
                if cycles > 0 and completed >= cycles:
                    break
                time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "idle layout loop interrupted by user"

        final_state = _read_json_file(process_path)
        if _int_or_none(final_state.get("pid")) == os.getpid():
            final_state["state"] = "stopped" if not interrupted else "interrupted"
            final_state["stopped_at"] = datetime.now(timezone.utc).isoformat()
            with process_path.open("w", encoding="utf-8") as file:
                json.dump(final_state, file, ensure_ascii=False, indent=2)

        return IdleLayoutLoopSummary(
            ok=not interrupted,
            reason=reason,
            objective=objective,
            cycles=completed,
            idle_cycles=idle_cycles,
            busy_cycles=busy_cycles,
            log_path=log_path,
            interrupted=interrupted,
        )

    def _observe_for_idle_layout(self) -> dict[str, Any]:
        return self.observe()

    def begin_codex_work(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
    ) -> dict[str, Any]:
        selected_skill = selected_skill.strip()
        if not selected_skill:
            raise ValueError("selected_skill is required")
        reason = reason.strip() or "Codex is implementing a missing deterministic executor."
        strategy = {
            "selected_skill": selected_skill,
            "reason": reason,
            "source": "codex_manual_wait",
            "skill_status": {
                "name": selected_skill,
                "implemented": False,
                "executor": None,
                "codex_required": True,
            },
        }
        self._record_codex_wait_state(objective, selected_skill, reason, strategy)
        state = self._read_codex_wait_state()
        process = _read_json_file(self._codex_wait_layout_process_path())
        return {
            "ok": True,
            "objective": objective,
            "selectedSkill": selected_skill,
            "waitState": state,
            "layoutLoop": process,
        }

    def finish_codex_work(
        self,
        selected_skill: str,
        *,
        clear_reason: str = "Codex implementation completed",
    ) -> dict[str, Any]:
        selected_skill = selected_skill.strip()
        if not selected_skill:
            raise ValueError("selected_skill is required")
        cleared = self._clear_codex_wait_state(selected_skill, clear_reason=clear_reason)
        state = self._read_codex_wait_state()
        return {
            "ok": cleared,
            "selectedSkill": selected_skill,
            "waitState": state,
            "reason": clear_reason if cleared else "no matching active Codex wait state",
        }

    def _skill_run_config(
        self,
        skill_name: str,
        target_count: int | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any] | None:
        if skill_name == "produce_iron_plate":
            target = target_count or 10
            return {
                "skill": IronPlateSkill(target),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 200),
                "log_prefix": "strategy-iron",
            }
        if skill_name == "produce_copper_plate":
            target = target_count or 10
            return {
                "skill": CopperPlateSkill(target),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 250),
                "log_prefix": "strategy-copper",
            }
        if skill_name == "produce_automation_science_pack":
            target = target_count or 5
            return {
                "skill": AutomationScienceSkill(target),
                "target_item": "automation-science-pack",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 500),
                "log_prefix": "strategy-science",
            }
        if skill_name == "produce_electronic_circuit":
            target = target_count or 5
            return {
                "skill": ElectronicCircuitSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 500),
                "log_prefix": "strategy-circuit",
            }
        if skill_name == "build_belt_smelting_line":
            target = target_count or 10
            return {
                "skill": BeltSmeltingLineSkill(target),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 700),
                "log_prefix": "strategy-belt-smelting",
            }
        if skill_name == "setup_coal_supply":
            target = target_count or 16
            return {
                "skill": CoalSupplySkill(target),
                "target_item": "coal",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 800),
                "log_prefix": "strategy-coal-supply",
            }
        if skill_name == "setup_stone_supply":
            target = target_count or 16
            return {
                "skill": StoneSupplySkill(target),
                "target_item": "stone",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 800),
                "log_prefix": "strategy-stone-supply",
            }
        if skill_name == "connect_coal_fuel_feed":
            return {
                "skill": CoalFuelFeedSkill(),
                "target_item": "coal",
                "target": target_count or 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 600),
                "log_prefix": "strategy-coal-fuel-feed",
            }
        if skill_name == "expand_iron_smelting":
            target = target_count or 90
            return {
                "skill": ExpandIronSmeltingSkill(float(target)),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 2000),
                "log_prefix": "strategy-expand-iron-smelting",
            }
        if skill_name == "expand_copper_smelting":
            target = target_count or 75
            return {
                "skill": ExpandCopperSmeltingSkill(float(target)),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1600),
                "log_prefix": "strategy-expand-copper-smelting",
            }
        if skill_name == "setup_power":
            return {
                "skill": SetupPowerSkill(),
                "target_item": "steam",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-power",
            }
        if skill_name == "research_automation":
            return {
                "skill": ResearchAutomationSkill(),
                "target_item": "automation-science-pack",
                "target": 10,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1500),
                "log_prefix": "strategy-automation-research",
            }
        if skill_name == "automate_electronic_circuit_line":
            target = target_count or 50
            return {
                "skill": CircuitAutomationSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1800),
                "log_prefix": "strategy-circuit-automation",
            }
        if skill_name == "research_logistics":
            return {
                "skill": ResearchTechnologySkill("logistics"),
                "target_item": "automation-science-pack",
                "target": 20,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 2200),
                "log_prefix": "strategy-logistics-research",
            }
        if skill_name == "bootstrap_build_item_mall":
            target = target_count or 20
            target_item = "transport-belt"
            return {
                "skill": BuildItemMallSkill(target_item, target),
                "target_item": target_item,
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1200),
                "log_prefix": "strategy-build-item-mall",
            }
        if skill_name == "build_gear_belt_mall_logistics":
            target = target_count or 20
            return {
                "skill": GearBeltMallLogisticsSkill(target),
                "target_item": "transport-belt",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-gear-belt-mall",
            }
        if skill_name == "build_starter_defense":
            return {
                "skill": StarterDefenseSkill(),
                "target_item": "gun-turret",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-starter-defense",
            }
        if skill_name == "plan_factory_site":
            return {
                "skill": FactoryLayoutImprovementSkill(),
                "target_item": "layout-plan",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1),
                "log_prefix": "strategy-layout-improvement",
            }
        return None

    def _run_skill(
        self,
        skill: Any,
        target_item: str,
        target: int,
        goal: str,
        max_steps: int,
        log_prefix: str,
        objective: str = "launch_rocket_program",
        log_path: Path | None = None,
    ) -> RunSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"{log_prefix}-{_timestamp()}.jsonl"
        started_at = time.monotonic()
        initial_item_count: int | None = None

        def finish(ok: bool, reason: str, step: int, observation: dict[str, Any]) -> RunSummary:
            nonlocal initial_item_count
            final_item_count = total_item_count(observation, target_item)
            if initial_item_count is None:
                initial_item_count = final_item_count
            summary = RunSummary(ok, reason, step, final_item_count, log_path, target_item)
            record_skill_run_journal(
                self.cfg.log_dir,
                objective=objective,
                goal=goal,
                ok=ok,
                reason=reason,
                steps=step,
                item_name=target_item,
                initial_item_count=initial_item_count,
                final_item_count=final_item_count,
                target=target,
                max_steps=max_steps,
                log_path=log_path,
                duration_seconds=time.monotonic() - started_at,
                repo_root=self._journal_repo_root(),
            )
            return summary

        with log_path.open("a", encoding="utf-8") as log_file:
            for step in range(1, max_steps + 1):
                self._wait_for_review_window()
                observation = self._observe_for_skill_loop(goal, step)
                if initial_item_count is None:
                    initial_item_count = total_item_count(observation, target_item)
                self._maybe_progress_background_layout_work(observation, objective, goal, step)
                decision = skill.next_action(observation)
                observation, decision = self._maybe_retry_skill_with_planning_sites(skill, observation, decision)
                decision = _guard_post_automation_handcraft(observation, decision)
                self._write_log(log_file, step, observation, decision, None)
                if decision.done:
                    self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                    return finish(True, decision.reason, step, observation)
                if decision.action is None:
                    self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                    return finish(False, decision.reason, step, observation)

                action = self._maybe_apply_remote_hint(observation, decision, goal)
                response = self.act(action)
                self._write_log(log_file, step, observation, decision, response)
                if not response.get("ok"):
                    return finish(False, f"action failed: {response.get('reason')}", step, observation)
                if action.get("type") == "wait":
                    ticks = int(action.get("ticks") or 60)
                    time.sleep(max(0.05, ticks / 60.0))
                elif action.get("type") == "move_to":
                    arrived, reason = self._wait_for_move(action)
                    if not arrived:
                        observation = self._observe_for_skill_loop(goal, step)
                        return finish(False, reason, step, observation)
                else:
                    time.sleep(0.2)

        observation = self._observe_for_skill_loop(goal, max_steps)
        self._maybe_progress_background_layout_work(observation, objective, goal, max_steps, force_poll=True)
        return finish(False, f"max steps reached: {max_steps}", max_steps, observation)

    def _observe_for_skill_loop(self, goal: str, step: int) -> dict[str, Any]:
        return self.observe()

    def _maybe_retry_skill_with_planning_sites(
        self,
        skill: Any,
        observation: dict[str, Any],
        decision: PlannerDecision,
    ) -> tuple[dict[str, Any], PlannerDecision]:
        return observation, decision

    def _autopilot_heartbeat_path(self) -> Path:
        return self.cfg.runtime_dir / "autopilot-heartbeat.json"

    def _idle_layout_process_path(self) -> Path:
        return self.cfg.runtime_dir / "idle-layout-loop.json"

    def _write_autopilot_heartbeat(
        self,
        objective: str,
        state: str,
        *,
        cycle: int,
        reason: str = "",
    ) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "active": state not in {"stopped", "interrupted"},
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "cycle": cycle,
            "reason": reason,
            "pid": os.getpid(),
        }
        with self._autopilot_heartbeat_path().open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _read_autopilot_heartbeat(self) -> dict[str, Any]:
        return _read_json_file(self._autopilot_heartbeat_path())

    def _autopilot_idle_for_layout(self, stale_seconds: float) -> tuple[bool, str, dict[str, Any]]:
        heartbeat = self._read_autopilot_heartbeat()
        if not heartbeat:
            return True, "autopilot heartbeat missing", {}
        state = str(heartbeat.get("state") or "")
        if state in {"sleeping", "stopped", "interrupted", "cycle_failed", "cycle_error"}:
            return True, f"autopilot state is {state}", heartbeat
        updated_at = _parse_datetime(heartbeat.get("updated_at"))
        if updated_at is None:
            return True, "autopilot heartbeat has no valid timestamp", heartbeat
        age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        heartbeat = dict(heartbeat)
        heartbeat["age_seconds"] = round(max(0.0, age_seconds), 3)
        if age_seconds >= stale_seconds:
            return True, f"autopilot heartbeat stale for {age_seconds:.1f}s", heartbeat
        return False, f"autopilot is active: {state}", heartbeat

    def _codex_wait_path(self) -> Path:
        return self.cfg.runtime_dir / "codex-wait.json"

    def _record_codex_wait_state(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
        strategy: dict[str, Any],
    ) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = self._codex_wait_path()
        previous = self._read_codex_wait_state()
        started_at = previous.get("started_at") if previous.get("selected_skill") == selected_skill else None
        payload = {
            "active": True,
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "selected_skill": selected_skill,
            "active_skill": f"codex_wait:{selected_skill}",
            "reason": reason,
            "strategy": strategy,
            "layout_work": {
                "enabled": True,
                "mode": os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MODE", "attach"),
                "until": "Codex implements the missing deterministic executor and the skill becomes executable.",
            },
        }
        with path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        self._maybe_start_codex_wait_layout_loop(objective)

    def _read_codex_wait_state(self) -> dict[str, Any]:
        path = self._codex_wait_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _clear_codex_wait_state(
        self,
        selected_skill: str,
        *,
        clear_reason: str = "deterministic executor is now available",
    ) -> bool:
        state = self._read_codex_wait_state()
        if not state.get("active") or state.get("selected_skill") != selected_skill:
            return False
        state["active"] = False
        state["cleared_at"] = datetime.now(timezone.utc).isoformat()
        state["clear_reason"] = clear_reason
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self._codex_wait_path().open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        return True

    def _codex_wait_layout_process_path(self) -> Path:
        return self.cfg.runtime_dir / "codex-wait-layout-loop.json"

    def _codex_wait_layout_cli_command(self) -> str:
        return "run-codex-wait-layout-loop"

    def _maybe_start_codex_wait_layout_loop(self, objective: str) -> None:
        if not self.cfg.slurm_enabled:
            return
        if os.getenv("FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART", "0").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return

        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        process_path = self._codex_wait_layout_process_path()
        existing = _read_json_file(process_path)
        existing_pid = _int_or_none(existing.get("pid")) if isinstance(existing, dict) else None
        if existing_pid and _pid_is_running(existing_pid):
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_loop_already_running",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )
            return

        sleep_seconds = os.getenv(
            "FACTORIO_AI_CODEX_WAIT_LAYOUT_SLEEP_SECONDS",
            os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS", "20"),
        )
        command = [
            sys.executable,
            "-m",
            "factorio_ai.cli",
            self._codex_wait_layout_cli_command(),
            "--objective",
            objective,
            "--cycles",
            "0",
            "--sleep-seconds",
            sleep_seconds,
        ]
        env = os.environ.copy()
        src_path = str(REPO_ROOT / "src")
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_path if not current_pythonpath else f"{src_path}{os.pathsep}{current_pythonpath}"

        kwargs: dict[str, Any] = {
            "cwd": str(REPO_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess,
                "DETACHED_PROCESS",
                0,
            )
        try:
            proc = subprocess.Popen(command, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_loop_start_failed",
                    "objective": objective,
                    "command": command,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        state = {
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "command": command,
        }
        with process_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        self._write_background_layout_log(
            {
                "event": "layout_codex_wait_loop_started",
                "pid": proc.pid,
                "objective": objective,
                "command": command,
            }
        )

    def _maybe_progress_codex_wait_layout(self, objective: str, *, phase: str = "cycle_start") -> None:
        state = self._read_codex_wait_state()
        if not state.get("active"):
            return
        selected = str(state.get("selected_skill") or "")
        if not selected:
            return
        wait_objective = str(state.get("objective") or objective)
        try:
            observation = self.observe()
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_observe_failed",
                    "phase": phase,
                    "active_skill": f"codex_wait:{selected}",
                    "active_step": 0,
                    "block_reason": state.get("reason"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        self._write_background_layout_log(
            {
                "event": "layout_codex_wait_heartbeat",
                "phase": phase,
                "active_skill": f"codex_wait:{selected}",
                "active_step": 0,
                "block_reason": state.get("reason"),
            }
        )
        self._maybe_progress_background_layout_work(
            observation,
            wait_objective,
            f"codex_wait:{selected}",
            0,
        )

    def _maybe_progress_background_layout_for_blocked_strategy(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
    ) -> None:
        if os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_ON_BLOCKED_STRATEGY", "1").lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return
        try:
            observation = self.observe()
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_blocked_strategy_observe_failed",
                    "active_skill": f"codex_wait:{selected_skill}",
                    "active_step": 0,
                    "block_reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        self._write_background_layout_log(
            {
                "event": "layout_blocked_strategy_detected",
                "active_skill": f"codex_wait:{selected_skill}",
                "active_step": 0,
                "block_reason": reason,
            }
        )
        self._maybe_progress_background_layout_work(
            observation,
            objective,
            f"codex_wait:{selected_skill}",
            0,
        )

    def _maybe_progress_background_layout_work(
        self,
        observation: dict[str, Any],
        objective: str,
        active_skill: str,
        active_step: int,
        *,
        force_poll: bool = False,
        minimum_interval_seconds: float | None = None,
    ) -> None:
        if not self.cfg.slurm_enabled or active_skill == "plan_factory_site":
            return
        if os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
            return
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._maybe_ensure_slurm_worker(reason="background_layout_work")
            from . import remote_slurm

            if self._background_layout_thread is not None:
                if self._background_layout_thread.is_alive():
                    return
                thread_result = self._background_layout_thread_result or {}
                self._write_background_layout_log(thread_result)
                self._background_layout_thread = None
                self._background_layout_thread_result = None

            if self._background_layout_task_name:
                state, data, raw = remote_slurm.read_task_state(self._background_layout_task_name)
                if state in {"result", "failed", "missing", "unknown"} or force_poll:
                    if state == "result" and data is not None:
                        self._write_background_layout_log(
                            {
                                "event": "layout_result",
                                "task": self._background_layout_task_name,
                                "objective": objective,
                                "active_skill": active_skill,
                                "active_step": active_step,
                                "state": state,
                                "result": data,
                            }
                        )
                        self._background_layout_task_name = None
                    elif state in {"failed", "missing", "unknown"}:
                        self._write_background_layout_log(
                            {
                                "event": "layout_task_unavailable",
                                "task": self._background_layout_task_name,
                                "active_skill": active_skill,
                                "active_step": active_step,
                                "state": state,
                                "data": data,
                                "raw": raw[:500],
                            }
                        )
                        self._background_layout_task_name = None

            if self._background_layout_task_name:
                return
            if force_poll:
                return
            interval = (
                float(minimum_interval_seconds)
                if minimum_interval_seconds is not None
                else float(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS", "20"))
            )
            now = time.monotonic()
            if now - self._background_layout_last_submit < interval:
                return

            targets = load_targets(self.cfg.runtime_dir, objective)
            monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
            validation_feedback = layout_validation_feedback_summary(self.cfg.log_dir)
            selected_improvement_site = load_selected_improvement_site(self.cfg.runtime_dir, objective)
            self._background_layout_last_submit = now
            mode = os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MODE", "attach").strip().lower()
            if mode in {"attach", "attached", "srun"}:
                self._background_layout_thread_result = None
                self._background_layout_thread = threading.Thread(
                    target=self._background_layout_attached_worker,
                    args=(
                        objective,
                        active_skill,
                        active_step,
                        observation,
                        targets.per_minute,
                        monitor,
                        validation_feedback,
                        selected_improvement_site,
                    ),
                    daemon=True,
                )
                self._background_layout_thread.start()
                self._write_background_layout_log(
                    {
                        "event": "layout_attached_started",
                        "active_skill": active_skill,
                        "active_step": active_step,
                    }
                )
            else:
                task = {
                    "type": "layout_improvement_request",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "objective": objective,
                        "active_skill": active_skill,
                        "active_step": active_step,
                        "observation": observation,
                        "production_targets": targets.per_minute,
                        "factory_monitor": monitor,
                        "layout_validation_feedback": validation_feedback,
                        "selected_improvement_site": selected_improvement_site,
                    },
                }
                self._background_layout_task_name = remote_slurm.submit_task(task)
                self._write_background_layout_log(
                    {
                        "event": "layout_task_submitted",
                        "task": self._background_layout_task_name,
                        "active_skill": active_skill,
                        "active_step": active_step,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_background_error",
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def _background_layout_attached_worker(
        self,
        objective: str,
        active_skill: str,
        active_step: int,
        observation: dict[str, Any],
        production_targets: dict[str, float],
        monitor: dict[str, Any],
        validation_feedback: dict[str, Any],
        selected_improvement_site: dict[str, Any],
    ) -> None:
        try:
            from . import remote_slurm

            result = remote_slurm.request_layout_improvement(
                objective,
                active_skill,
                active_step,
                observation,
                production_targets=production_targets,
                factory_monitor=monitor,
                layout_validation_feedback=validation_feedback,
                selected_improvement_site=selected_improvement_site,
                timeout_seconds=int(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_TIMEOUT_SECONDS", "180")),
                force_attached=True,
            )
            self._background_layout_thread_result = {
                "event": "layout_result",
                "mode": "attach",
                "objective": objective,
                "active_skill": active_skill,
                "active_step": active_step,
                "result": result,
            }
        except Exception as exc:  # noqa: BLE001
            self._background_layout_thread_result = {
                "event": "layout_background_error",
                "mode": "attach",
                "active_skill": active_skill,
                "active_step": active_step,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _write_background_layout_log(self, payload: dict[str, Any]) -> None:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        row = {"time": datetime.now(timezone.utc).isoformat(), **payload}
        with path.open("a", encoding="utf-8") as file:
            json.dump(row, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if row.get("event") == "layout_result" and result:
            record_layout_result_insight(
                self.cfg.log_dir,
                objective=str(row.get("objective") or "launch_rocket_program"),
                active_skill=str(row.get("active_skill") or ""),
                result=result,
                repo_root=self._journal_repo_root(),
            )

    def _journal_repo_root(self) -> Path:
        runtime_dir = Path(self.cfg.runtime_dir)
        if runtime_dir.name == "runtime":
            return runtime_dir.parent
        return REPO_ROOT

    def _client(self) -> FactorioRconClient:
        return FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password)

    def _agent_parameter(self) -> dict[str, Any]:
        player_name = self._configured_agent_player_name()
        if player_name:
            return {"player_name": player_name}
        return {}

    def _agent_action(self, action: dict[str, Any]) -> dict[str, Any]:
        player_name = self._configured_agent_player_name()
        if not player_name or "player_name" in action:
            return action
        targeted = dict(action)
        targeted["player_name"] = player_name
        return targeted

    def _configured_agent_player_name(self) -> str:
        player_name = str(self.cfg.agent_player_name or "").strip()
        if player_name.lower() in {"auto", "connected", "first-connected", "*"}:
            return ""
        return player_name

    def _wait_for_move(self, action: dict[str, Any]) -> tuple[bool, str]:
        target = action.get("position")
        if not isinstance(target, dict):
            return False, "move_to did not include a target position"

        first_observation = self.observe()
        start_position = player_position(first_observation)
        initial_distance = distance(start_position, target)
        tolerance = float(action.get("tolerance") or 4.0)
        timeout_seconds = min(240.0, max(20.0, initial_distance * 3.0 + 10.0))
        stall_timeout_seconds = float(action.get("stall_timeout_seconds") or 8.0)
        deadline = time.monotonic() + timeout_seconds
        last_distance = initial_distance
        best_distance = initial_distance
        last_progress_at = time.monotonic()
        detour_attempts = 0
        max_detour_attempts = int(action.get("max_detour_attempts") or 8)

        while time.monotonic() < deadline:
            if self._review_lock_active():
                self.stop_agent()
                self._wait_for_review_window()
                return False, "move paused for GUI review"
            observation = self.observe()
            current_position = player_position(observation)
            current_distance = distance(current_position, target)
            last_distance = current_distance
            if current_distance <= tolerance:
                self.stop_agent()
                return True, f"arrived at move target within {current_distance:.2f} tiles"

            if current_distance < best_distance - 0.15:
                best_distance = current_distance
                detour_attempts = 0
                last_progress_at = time.monotonic()
            elif time.monotonic() - last_progress_at > stall_timeout_seconds:
                if detour_attempts < max_detour_attempts:
                    detour = _move_detour_action(current_position, target, detour_attempts)
                    detour_attempts += 1
                    refresh = self.act(detour)
                    if not refresh.get("ok"):
                        self.stop_agent()
                        return False, f"move detour failed: {refresh.get('reason')}"
                    time.sleep(0.5)
                    detour_observation = self.observe()
                    detour_position = player_position(detour_observation)
                    if distance(detour_position, current_position) > 0.2:
                        last_progress_at = time.monotonic()
                        last_distance = distance(detour_position, target)
                        continue
                self.stop_agent()
                return False, f"move made no progress; remaining distance {current_distance:.2f}"

            refresh = self.act(action)
            if not refresh.get("ok"):
                self.stop_agent()
                return False, f"move refresh failed: {refresh.get('reason')}"
            time.sleep(0.5)

        self.stop_agent()
        return False, f"move_to timed out; remaining distance {last_distance:.2f}"

    def _review_lock_active(self) -> bool:
        return (self.cfg.runtime_dir / "review.lock").exists()

    def _wait_for_review_window(self) -> None:
        if not self._review_lock_active():
            return
        self.stop_agent()
        while self._review_lock_active():
            time.sleep(1.0)

    def _maybe_apply_remote_hint(
        self,
        observation: dict[str, Any],
        decision: PlannerDecision,
        goal: str,
    ) -> dict[str, Any]:
        if not self.cfg.slurm_enabled or decision.action is None:
            return decision.action or {"type": "wait", "ticks": 60}
        if os.getenv("FACTORIO_AI_REMOTE_ACTION_HINT_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
            return decision.action
        try:
            from . import remote_slurm

            status = self._remote_llm_status()
            if not status.get("llm_ready"):
                return decision.action
            result = remote_slurm.request_plan(
                observation=observation,
                legal_actions=[decision.action],
                goal=goal,
                timeout_seconds=5,
            )
            hint = result.get("action_hint")
            if isinstance(hint, dict):
                validate_action(hint)
                return hint
        except (Exception, ActionValidationError):
            return decision.action
        return decision.action

    def _remote_llm_status(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._remote_llm_status_cache is not None and now < self._remote_llm_status_cache_until:
            return self._remote_llm_status_cache

        from . import remote_slurm

        status = remote_slurm.llm_status()
        self._remote_llm_status_cache = status
        self._remote_llm_status_cache_until = now + 30.0
        return status

    def _slurm_renewal_state_path(self) -> Path:
        return self.cfg.runtime_dir / "slurm-renewal.json"

    def _slurm_renewal_log_path(self) -> Path:
        return self.cfg.log_dir / "slurm-renewal.jsonl"

    def _maybe_ensure_slurm_worker(self, *, reason: str, force: bool = False) -> None:
        if not self.cfg.slurm_enabled:
            return
        if os.getenv("FACTORIO_AI_SLURM_AUTO_RENEW_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        interval_seconds = max(60.0, _float_env("FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS", 1800.0))
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        state_path = self._slurm_renewal_state_path()
        state = _read_json_file(state_path)
        now = datetime.now(timezone.utc)
        checked_at = _parse_datetime(state.get("checked_at"))
        if not force and checked_at is not None and (now - checked_at).total_seconds() < interval_seconds:
            return
        renew_before_minutes = _int_or_none(os.getenv("FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES"))
        payload: dict[str, Any] = {
            "event": "slurm_worker_renewal_check",
            "checked_at": now.isoformat(),
            "reason": reason,
            "renew_before_minutes": renew_before_minutes,
            "interval_seconds": interval_seconds,
        }
        try:
            from . import remote_slurm

            result = remote_slurm.ensure_worker_job(renew_before_minutes=renew_before_minutes)
            payload["ok"] = bool(result.get("ok"))
            payload["action"] = result.get("action")
            payload["result"] = result
        except Exception as exc:  # noqa: BLE001
            payload["ok"] = False
            payload["error"] = f"{type(exc).__name__}: {exc}"
        with state_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        with self._slurm_renewal_log_path().open("a", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")

    @staticmethod
    def _remote_llm_unready_reason(status: dict[str, Any]) -> str:
        missing = status.get("missing") if isinstance(status.get("missing"), list) else []
        parts = [str(item) for item in missing if str(item)]
        remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
        jobs = remote.get("pending_jobs") or remote.get("jobs")
        if isinstance(jobs, list) and jobs:
            job = jobs[0] if isinstance(jobs[0], dict) else {}
            if isinstance(job, dict):
                job_bits = [
                    str(job.get("id") or ""),
                    str(job.get("state") or ""),
                    str(job.get("reason") or ""),
                    str(job.get("start_time") or ""),
                ]
                job_text = " ".join(bit for bit in job_bits if bit)
                if job_text:
                    parts.append(f"job {job_text}")
        return "; ".join(parts) if parts else "remote Slurm LLM is not ready"

    @staticmethod
    def _write_log(
        log_file: Any,
        step: int,
        observation: dict[str, Any],
        decision: PlannerDecision,
        response: dict[str, Any] | None,
    ) -> None:
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "tick": observation.get("tick"),
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "done": decision.done,
            },
            "response": response,
            "inventory": observation.get("inventory"),
            "player": observation.get("player"),
        }
        json.dump(payload, log_file, ensure_ascii=False, separators=(",", ":"))
        log_file.write("\n")
        log_file.flush()


class ModlessFactorioController(FactorioController):
    """Reuse skill loops with the no-custom-mod RCON/Lua adapter."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._modless = ModlessLuaController(cfg)
        self._planning_sites_cache: dict[str, Any] = {}
        self._planning_sites_cache_loaded = False

    def run_strategy_step(
        self,
        objective: str = "launch_rocket_program",
        require_llm: bool = False,
        target_count: int | None = None,
        max_steps: int | None = None,
    ) -> StrategyStepSummary:
        if _real_player_execution_required():
            observation = self.observe()
            observation, recovery_problem = self._maybe_restore_real_player_controller(observation)
            if recovery_problem:
                return StrategyStepSummary(
                    ok=False,
                    reason=recovery_problem,
                    objective=objective,
                    selected_skill="",
                    strategy={
                        "selected_skill": "",
                        "source": "execution_guard",
                        "reason": recovery_problem,
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                    },
                )
            problem = _real_player_execution_problem(observation)
            if problem:
                return StrategyStepSummary(
                    ok=False,
                    reason=problem,
                    objective=objective,
                    selected_skill="",
                    strategy={
                        "selected_skill": "",
                        "source": "execution_guard",
                        "reason": problem,
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                    },
                )
            threat = _real_player_enemy_action_threat(observation, {"type": "wait", "ticks": 1})
            if threat:
                stop_response = self._modless.act(
                    {"type": "stop"},
                    player_name=self._configured_agent_player_name(),
                )
                return StrategyStepSummary(
                    ok=False,
                    reason=threat,
                    objective=objective,
                    selected_skill="build_starter_defense",
                    strategy={
                        "selected_skill": "build_starter_defense",
                        "source": "execution_guard",
                        "reason": threat,
                        "expected_effect": "Pause real-player movement before enemy contact kills the character.",
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                        "stop_response": stop_response,
                    },
                )
        return super().run_strategy_step(
            objective=objective,
            require_llm=require_llm,
            target_count=target_count,
            max_steps=max_steps,
        )

    def observe(self) -> dict[str, Any]:
        return self._observe_modless(include_planning_sites=False)

    def _observe_for_idle_layout(self) -> dict[str, Any]:
        return self._observe_modless(include_planning_sites=False)

    def _observe_for_skill_loop(self, goal: str, step: int) -> dict[str, Any]:
        return self._observe_modless(include_planning_sites=False)

    def _maybe_retry_skill_with_planning_sites(
        self,
        skill: Any,
        observation: dict[str, Any],
        decision: PlannerDecision,
    ) -> tuple[dict[str, Any], PlannerDecision]:
        if not _planning_site_retry_needed(decision):
            return observation, decision
        self._load_planning_sites_cache(observation)
        if self._planning_sites_cache_is_fresh():
            cached_observation = self._merge_cached_planning_sites(observation)
            cached_decision = skill.next_action(cached_observation)
            return cached_observation, cached_decision
        full_observation = self._observe_modless(include_planning_sites=True)
        return full_observation, skill.next_action(full_observation)

    def _observe_modless(self, *, include_planning_sites: bool) -> dict[str, Any]:
        response = self._modless.observe(
            player_name=self._configured_agent_player_name(),
            include_planning_sites=include_planning_sites,
        )
        if not response.get("ok"):
            raise RuntimeError(f"no-mod observe failed: {response}")
        memory = self._remember_world_map(response, include_planning_sites=include_planning_sites)
        if include_planning_sites:
            self._update_planning_sites_cache(response)
            return merge_world_map_memory_into_observation(
                response,
                memory,
                max_age_seconds=_planning_site_cache_seconds(),
            )
        self._load_planning_sites_cache(response)
        merged = self._merge_cached_planning_sites(response)
        return merge_world_map_memory_into_observation(
            merged,
            memory,
            max_age_seconds=_planning_site_cache_seconds(),
        )

    def _update_planning_sites_cache(self, observation: dict[str, Any]) -> None:
        for key in ("power_sites", "lab_sites", "automation_sites"):
            sites = observation.get(key)
            if isinstance(sites, list):
                self._planning_sites_cache[key] = sites
        self._planning_sites_cache["tick"] = observation.get("tick")
        self._planning_sites_cache["cached_at"] = time.time()
        self._persist_planning_sites_cache()

    def _merge_cached_planning_sites(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not self._planning_sites_cache or not self._planning_sites_cache_is_fresh():
            return observation
        merged = dict(observation)
        for key in ("power_sites", "lab_sites", "automation_sites"):
            if key in self._planning_sites_cache:
                merged[key] = self._planning_sites_cache[key]
        merged["planning_sites_cached_from_tick"] = self._planning_sites_cache.get("tick")
        cached_at = self._planning_sites_cache.get("cached_at")
        if isinstance(cached_at, (int, float)):
            merged["planning_sites_cache_age_seconds"] = round(max(0.0, time.time() - cached_at), 3)
        return merged

    def _planning_sites_cache_path(self) -> Path:
        return self.cfg.runtime_dir / "planning-sites-cache.json"

    def _load_planning_sites_cache(self, observation: dict[str, Any] | None = None) -> None:
        if self._planning_sites_cache_loaded:
            return
        self._planning_sites_cache_loaded = True
        path = self._planning_sites_cache_path()
        if not path.exists():
            self._load_planning_sites_cache_from_world_memory()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._load_planning_sites_cache_from_world_memory()
            return
        if not isinstance(data, dict):
            self._load_planning_sites_cache_from_world_memory()
            return
        cached_at = data.get("cached_at")
        if not isinstance(cached_at, (int, float)) or time.time() - cached_at > _planning_site_cache_seconds():
            self._load_planning_sites_cache_from_world_memory()
            return
        cached_tick = data.get("tick")
        observed_tick = observation.get("tick") if isinstance(observation, dict) else None
        if isinstance(cached_tick, (int, float)) and isinstance(observed_tick, (int, float)):
            if cached_tick > observed_tick + 600:
                self._load_planning_sites_cache_from_world_memory()
                return
        cache: dict[str, Any] = {"cached_at": cached_at, "tick": cached_tick}
        for key in ("power_sites", "lab_sites", "automation_sites"):
            sites = data.get(key)
            if isinstance(sites, list):
                cache[key] = sites
        if any(key in cache for key in ("power_sites", "lab_sites", "automation_sites")):
            self._planning_sites_cache = cache
        else:
            self._load_planning_sites_cache_from_world_memory()

    def _load_planning_sites_cache_from_world_memory(self) -> None:
        memory = load_world_map_memory(self.cfg.runtime_dir)
        if not planning_sites_are_fresh(memory, max_age_seconds=_planning_site_cache_seconds()):
            return
        cache = planning_sites_from_memory(memory)
        if any(key in cache for key in ("power_sites", "lab_sites", "automation_sites")):
            self._planning_sites_cache = cache

    def _planning_sites_cache_is_fresh(self) -> bool:
        cached_at = self._planning_sites_cache.get("cached_at")
        return isinstance(cached_at, (int, float)) and time.time() - cached_at <= _planning_site_cache_seconds()

    def _persist_planning_sites_cache(self) -> None:
        payload = {
            "cached_at": self._planning_sites_cache.get("cached_at"),
            "tick": self._planning_sites_cache.get("tick"),
        }
        for key in ("power_sites", "lab_sites", "automation_sites"):
            if key in self._planning_sites_cache:
                payload[key] = self._planning_sites_cache[key]
        try:
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._planning_sites_cache_path().write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _remember_world_map(self, observation: dict[str, Any], *, include_planning_sites: bool) -> dict[str, Any]:
        try:
            return update_world_map_memory(
                self.cfg.runtime_dir,
                observation,
                include_planning_sites=include_planning_sites,
                source="no-mod-full-planning-observe" if include_planning_sites else "no-mod-lightweight-observe",
            )
        except OSError:
            return load_world_map_memory(self.cfg.runtime_dir)

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
        if _real_player_execution_required():
            observation = self.observe()
            observation, recovery_problem = self._maybe_restore_real_player_controller(observation)
            if recovery_problem:
                return {
                    "ok": False,
                    "reason": recovery_problem,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            problem = _real_player_execution_problem(observation)
            if problem:
                return {
                    "ok": False,
                    "reason": problem,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            threat = _real_player_enemy_action_threat(observation, action)
            if threat:
                return {
                    "ok": False,
                    "reason": threat,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            if action.get("type") == "move_to" and _gui_input_movement_enabled():
                return self._act_gui_move_to(action, observation)
        return self._modless.act(self._agent_action(action), player_name=self._configured_agent_player_name())

    def _maybe_restore_real_player_controller(self, observation: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if not _real_player_controller_restore_needed(observation):
            return observation, ""
        restore = self._modless.act(
            {"type": "restore_character_controller"},
            player_name=self._configured_agent_player_name(),
        )
        if not restore.get("ok"):
            problem = _real_player_execution_problem(observation)
            return observation, f"{problem}; restore_character_controller failed: {restore.get('reason')}"
        refreshed = self.observe()
        if _real_player_controller_restore_needed(refreshed):
            problem = _real_player_execution_problem(refreshed)
            return refreshed, f"{problem}; restore_character_controller did not restore character controller"
        return refreshed, ""

    def _act_gui_move_to(self, action: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        target = action.get("position")
        if not isinstance(target, dict):
            return {"ok": False, "reason": "move_to requires numeric position", "mode": "gui-input"}
        current = player_position(observation)
        player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
        if player.get("controller_is_character") is False:
            return {
                "ok": False,
                "reason": "GUI movement requires the player to be in character controller mode; close map/remote view first",
                "mode": "gui-input",
                "player": player,
            }
        target_position = {"x": float(target.get("x") or 0.0), "y": float(target.get("y") or 0.0)}
        remaining = distance(current, target_position)
        tolerance = float(action.get("tolerance") or 0.75)
        if remaining <= tolerance:
            return {
                "ok": True,
                "action": "move_to",
                "status": "arrived",
                "mode": "gui-input",
                "position": current,
                "target": target_position,
                "distance": round(remaining, 3),
            }
        keys = _movement_keys(current, target_position)
        if not keys:
            return {"ok": False, "reason": "move_to could not derive movement keys", "mode": "gui-input"}
        duration = float(action.get("duration_seconds") or min(0.9, max(0.12, remaining / 12.0)))
        try:
            from .vanilla_gui import VanillaGuiDriver

            driver = VanillaGuiDriver(self.cfg)
            if not driver.activate_factorio(timeout_seconds=2.0):
                return {"ok": False, "reason": "Factorio GUI window could not be activated for movement", "mode": "gui-input"}
            driver.click_window_ratio(0.5, 0.5)
            time.sleep(0.05)
            driver.hold_keys(keys, duration)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"GUI movement failed: {type(exc).__name__}: {exc}", "mode": "gui-input"}
        return {
            "ok": True,
            "action": "move_to",
            "status": "moving",
            "mode": "gui-input",
            "keys": keys,
            "duration_seconds": round(duration, 3),
            "position": current,
            "target": target_position,
            "distance": round(remaining, 3),
            "agent": {"name": player.get("name"), "kind": player.get("kind"), "character_valid": player.get("character_valid")},
            "execution": {"mode": "player", "input": "gui-keyboard", "virtual": False},
        }

    def wait(self, ticks: int) -> dict[str, Any]:
        response = self.act({"type": "wait", "ticks": ticks})
        time.sleep(max(0.05, ticks / 60.0))
        return response

    def _codex_wait_layout_cli_command(self) -> str:
        return "run-no-mod-codex-wait-layout-loop"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _real_player_execution_required() -> bool:
    return os.getenv("FACTORIO_AI_REQUIRE_REAL_PLAYER", "").strip().lower() in {"1", "true", "yes", "on"}


def _gui_input_movement_enabled() -> bool:
    return os.getenv("FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT", "").strip().lower() in {"1", "true", "yes", "on"}


def _movement_keys(current: dict[str, float], target: dict[str, float]) -> list[str]:
    dx = float(target.get("x") or 0.0) - float(current.get("x") or 0.0)
    dy = float(target.get("y") or 0.0) - float(current.get("y") or 0.0)
    keys: list[str] = []
    if dy < -0.2:
        keys.append("w")
    elif dy > 0.2:
        keys.append("s")
    if dx < -0.2:
        keys.append("a")
    elif dx > 0.2:
        keys.append("d")
    return keys


def _move_detour_action(current: dict[str, float], target: dict[str, float], attempt: int) -> dict[str, Any]:
    dx = float(target.get("x") or 0.0) - float(current.get("x") or 0.0)
    dy = float(target.get("y") or 0.0) - float(current.get("y") or 0.0)
    sign = 1.0 if attempt % 2 == 0 else -1.0
    offset = min(12.0, 4.0 + float(attempt // 2) * 2.0)
    position = {"x": float(current.get("x") or 0.0), "y": float(current.get("y") or 0.0)}
    if abs(dy) >= abs(dx):
        position["x"] += sign * offset
    else:
        position["y"] += sign * offset
    return {
        "type": "move_to",
        "position": position,
        "duration_seconds": 0.8,
        "tolerance": 1.5,
        "max_detour_attempts": 0,
    }


def _real_player_enemy_action_threat(observation: dict[str, Any], action: dict[str, Any]) -> str:
    if action.get("type") == "stop":
        return ""
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        return ""
    enemy_positions: list[tuple[dict[str, Any], dict[str, float]]] = []
    for enemy in enemies:
        if not isinstance(enemy, dict):
            continue
        position = _position_or_none(enemy.get("position"))
        if position is not None:
            enemy_positions.append((enemy, position))
    if not enemy_positions:
        return ""

    current = player_position(observation)
    stop_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_STOP_RADIUS", 24.0)
    target_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_TARGET_RADIUS", 32.0)
    path_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_PATH_RADIUS", 20.0)

    nearest_current = min(
        ((distance(current, enemy_position), enemy) for enemy, enemy_position in enemy_positions),
        key=lambda item: item[0],
    )
    if nearest_current[0] <= stop_radius:
        return _enemy_threat_reason("near player", nearest_current[1], nearest_current[0], stop_radius)

    target = _action_target_position(action)
    if target is None:
        return ""
    nearest_target = min(
        ((distance(target, enemy_position), enemy) for enemy, enemy_position in enemy_positions),
        key=lambda item: item[0],
    )
    if nearest_target[0] <= target_radius:
        return _enemy_threat_reason("near action target", nearest_target[1], nearest_target[0], target_radius)

    if action.get("type") == "move_to":
        nearest_path = min(
            (
                (_distance_to_segment(enemy_position, current, target), enemy)
                for enemy, enemy_position in enemy_positions
            ),
            key=lambda item: item[0],
        )
        if nearest_path[0] <= path_radius:
            return _enemy_threat_reason("near movement path", nearest_path[1], nearest_path[0], path_radius)
    return ""


def _real_player_execution_problem(observation: dict[str, Any]) -> str:
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    kind = str(player.get("kind") or execution.get("agent_kind") or "").strip().lower()
    character_valid = bool(player.get("character_valid") or execution.get("character_valid"))
    if mode == "virtual" or kind == "server" or execution.get("virtual"):
        return (
            "real player execution required, but the no-mod adapter selected the virtual server agent; "
            "connect a GUI client as the configured AI player or disable FACTORIO_AI_REQUIRE_REAL_PLAYER for headless tests"
        )
    if not character_valid:
        name = str(player.get("name") or execution.get("agent_name") or "unknown")
        return f"real player execution required, but player {name} has no valid character"
    if player.get("controller_is_character") is False or execution.get("controller_is_character") is False:
        name = str(player.get("name") or execution.get("agent_name") or "unknown")
        return f"real player execution required, but player {name} is not in character controller mode"
    return ""


def _real_player_controller_restore_needed(observation: dict[str, Any]) -> bool:
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    kind = str(player.get("kind") or execution.get("agent_kind") or "").strip().lower()
    character_valid = bool(player.get("character_valid") or execution.get("character_valid"))
    if mode == "virtual" or kind == "server" or execution.get("virtual") or not character_valid:
        return False
    return player.get("controller_is_character") is False or execution.get("controller_is_character") is False


def _action_target_position(action: dict[str, Any]) -> dict[str, float] | None:
    for key in ("position", "near"):
        position = _position_or_none(action.get(key))
        if position is not None:
            return position
    return None


def _position_or_none(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {"x": float(value["x"]), "y": float(value["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def _distance_to_segment(point: dict[str, float], start: dict[str, float], end: dict[str, float]) -> float:
    sx = float(start["x"])
    sy = float(start["y"])
    ex = float(end["x"])
    ey = float(end["y"])
    px = float(point["x"])
    py = float(point["y"])
    dx = ex - sx
    dy = ey - sy
    if dx == 0 and dy == 0:
        return distance(point, start)
    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest = {"x": sx + t * dx, "y": sy + t * dy}
    return distance(point, closest)


def _enemy_threat_reason(scope: str, enemy: dict[str, Any], threat_distance: float, radius: float) -> str:
    name = str(enemy.get("name") or enemy.get("type") or "enemy")
    position = enemy.get("position") if isinstance(enemy.get("position"), dict) else {}
    return (
        "real player execution paused: "
        f"enemy {name} is {threat_distance:.1f} tiles {scope} "
        f"(limit {radius:.1f}) at {float(position.get('x') or 0.0):.1f},{float(position.get('y') or 0.0):.1f}"
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _slugify_reason(value: str) -> str:
    output = []
    for char in value.lower():
        if char.isalnum():
            output.append(char)
        elif output and output[-1] != "_":
            output.append("_")
    text = "".join(output).strip("_")
    return text[:80] or "idle"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_steps(value: int | None, default: int) -> int:
    if value is None:
        return default
    return max(0, int(value))


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
