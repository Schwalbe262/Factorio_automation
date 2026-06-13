from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .llm_log import record_llm_decision, strategy_request_summary
from .config import AppConfig
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
from .planner import (
    AutomationScienceSkill,
    BeltSmeltingLineSkill,
    BuildItemMallSkill,
    CircuitAutomationSkill,
    CoalSupplySkill,
    CopperPlateSkill,
    ElectronicCircuitSkill,
    ExpandCopperSmeltingSkill,
    ExpandIronSmeltingSkill,
    FactoryLayoutImprovementSkill,
    IronPlateSkill,
    ResearchAutomationSkill,
    ResearchTechnologySkill,
    SetupPowerSkill,
    StarterDefenseSkill,
)
from .rcon import FactorioRconClient
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy, make_strategy_payload, reconcile_strategy_decision, skill_catalog_payload
from .targets import load_targets


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
        request_summary = strategy_request_summary(observation, production_targets)
        result: dict[str, Any] | None = None
        if self.cfg.slurm_enabled:
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

                result = run_strategy_request(make_strategy_payload(objective, observation, production_targets))
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
                result = heuristic_strategy(objective, observation, production_targets)
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

        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                while cycles <= 0 or completed < cycles:
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
                    completed += 1
                    if not last_step.ok:
                        failures += 1
                    payload = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "cycle": completed,
                        "objective": objective,
                        "ok": last_step.ok,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "step": last_step.to_dict(),
                    }
                    json.dump(payload, log_file, ensure_ascii=False, separators=(",", ":"))
                    log_file.write("\n")
                    log_file.flush()
                    if not last_step.ok and not continue_on_error:
                        reason = last_step.reason
                        break
                    if cycles <= 0 or completed < cycles:
                        time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "autopilot interrupted by user"

        ok = failures == 0 or (continue_on_error and cycles <= 0 and not interrupted)
        if cycles > 0 and completed >= cycles and failures == 0:
            reason = "cycle limit reached"
        elif failures > 0 and continue_on_error:
            reason = f"completed with {failures} failed cycle(s); continuing is enabled"
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
                "max_steps": max_steps or 200,
                "log_prefix": "strategy-iron",
            }
        if skill_name == "produce_copper_plate":
            target = target_count or 10
            return {
                "skill": CopperPlateSkill(target),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 250,
                "log_prefix": "strategy-copper",
            }
        if skill_name == "produce_automation_science_pack":
            target = target_count or 5
            return {
                "skill": AutomationScienceSkill(target),
                "target_item": "automation-science-pack",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 500,
                "log_prefix": "strategy-science",
            }
        if skill_name == "produce_electronic_circuit":
            target = target_count or 5
            return {
                "skill": ElectronicCircuitSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 500,
                "log_prefix": "strategy-circuit",
            }
        if skill_name == "build_belt_smelting_line":
            target = target_count or 10
            return {
                "skill": BeltSmeltingLineSkill(target),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 700,
                "log_prefix": "strategy-belt-smelting",
            }
        if skill_name == "setup_coal_supply":
            target = target_count or 16
            return {
                "skill": CoalSupplySkill(target),
                "target_item": "coal",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 800,
                "log_prefix": "strategy-coal-supply",
            }
        if skill_name == "expand_iron_smelting":
            target = target_count or 90
            return {
                "skill": ExpandIronSmeltingSkill(float(target)),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 2000,
                "log_prefix": "strategy-expand-iron-smelting",
            }
        if skill_name == "expand_copper_smelting":
            target = target_count or 75
            return {
                "skill": ExpandCopperSmeltingSkill(float(target)),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 1600,
                "log_prefix": "strategy-expand-copper-smelting",
            }
        if skill_name == "setup_power":
            return {
                "skill": SetupPowerSkill(),
                "target_item": "steam",
                "target": 1,
                "goal": skill_name,
                "max_steps": max_steps or 900,
                "log_prefix": "strategy-power",
            }
        if skill_name == "research_automation":
            return {
                "skill": ResearchAutomationSkill(),
                "target_item": "automation-science-pack",
                "target": 10,
                "goal": skill_name,
                "max_steps": max_steps or 1500,
                "log_prefix": "strategy-automation-research",
            }
        if skill_name == "automate_electronic_circuit_line":
            target = target_count or 5
            return {
                "skill": CircuitAutomationSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": max_steps or 1800,
                "log_prefix": "strategy-circuit-automation",
            }
        if skill_name == "research_logistics":
            return {
                "skill": ResearchTechnologySkill("logistics"),
                "target_item": "automation-science-pack",
                "target": 20,
                "goal": skill_name,
                "max_steps": max_steps or 2200,
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
                "max_steps": max_steps or 1200,
                "log_prefix": "strategy-build-item-mall",
            }
        if skill_name == "build_starter_defense":
            return {
                "skill": StarterDefenseSkill(),
                "target_item": "gun-turret",
                "target": 1,
                "goal": skill_name,
                "max_steps": max_steps or 900,
                "log_prefix": "strategy-starter-defense",
            }
        if skill_name == "plan_factory_site":
            return {
                "skill": FactoryLayoutImprovementSkill(),
                "target_item": "layout-plan",
                "target": 1,
                "goal": skill_name,
                "max_steps": max_steps or 1,
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

        with log_path.open("a", encoding="utf-8") as log_file:
            for step in range(1, max_steps + 1):
                self._wait_for_review_window()
                observation = self.observe()
                self._maybe_progress_background_layout_work(observation, objective, goal, step)
                decision = skill.next_action(observation)
                item_count = total_item_count(observation, target_item)
                self._write_log(log_file, step, observation, decision, None)
                if decision.done:
                    self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                    return RunSummary(True, decision.reason, step, item_count, log_path, target_item)
                if decision.action is None:
                    self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                    return RunSummary(False, decision.reason, step, item_count, log_path, target_item)

                action = self._maybe_apply_remote_hint(observation, decision, goal)
                response = self.act(action)
                self._write_log(log_file, step, observation, decision, response)
                if not response.get("ok"):
                    return RunSummary(False, f"action failed: {response.get('reason')}", step, item_count, log_path, target_item)
                if action.get("type") == "wait":
                    ticks = int(action.get("ticks") or 60)
                    time.sleep(max(0.05, ticks / 60.0))
                elif action.get("type") == "move_to":
                    arrived, reason = self._wait_for_move(action)
                    if not arrived:
                        observation = self.observe()
                        return RunSummary(
                            False,
                            reason,
                            step,
                            total_item_count(observation, target_item),
                            log_path,
                            target_item,
                        )
                else:
                    time.sleep(0.2)

        observation = self.observe()
        self._maybe_progress_background_layout_work(observation, objective, goal, max_steps, force_poll=True)
        return RunSummary(
            False,
            f"max steps reached: {max_steps}",
            max_steps,
            total_item_count(observation, target_item),
            log_path,
            target_item,
        )

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

    def _read_codex_wait_state(self) -> dict[str, Any]:
        path = self._codex_wait_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _clear_codex_wait_state(self, selected_skill: str) -> None:
        state = self._read_codex_wait_state()
        if not state.get("active") or state.get("selected_skill") != selected_skill:
            return
        state["active"] = False
        state["cleared_at"] = datetime.now(timezone.utc).isoformat()
        state["clear_reason"] = "deterministic executor is now available"
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self._codex_wait_path().open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)

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
    ) -> None:
        if not self.cfg.slurm_enabled or active_skill == "plan_factory_site":
            return
        if os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
            return
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        try:
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
            interval = float(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS", "20"))
            now = time.monotonic()
            if now - self._background_layout_last_submit < interval:
                return

            targets = load_targets(self.cfg.runtime_dir, objective)
            monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
            self._background_layout_last_submit = now
            mode = os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MODE", "attach").strip().lower()
            if mode in {"attach", "attached", "srun"}:
                self._background_layout_thread_result = None
                self._background_layout_thread = threading.Thread(
                    target=self._background_layout_attached_worker,
                    args=(objective, active_skill, active_step, observation, targets.per_minute, monitor),
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
                timeout_seconds=int(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_TIMEOUT_SECONDS", "180")),
                force_attached=True,
            )
            self._background_layout_thread_result = {
                "event": "layout_result",
                "mode": "attach",
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

    def _client(self) -> FactorioRconClient:
        return FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password)

    def _agent_parameter(self) -> dict[str, Any]:
        if self.cfg.agent_player_name:
            return {"player_name": self.cfg.agent_player_name}
        return {}

    def _agent_action(self, action: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.agent_player_name or "player_name" in action:
            return action
        targeted = dict(action)
        targeted["player_name"] = self.cfg.agent_player_name
        return targeted

    def _wait_for_move(self, action: dict[str, Any]) -> tuple[bool, str]:
        target = action.get("position")
        if not isinstance(target, dict):
            return False, "move_to did not include a target position"

        first_observation = self.observe()
        start_position = player_position(first_observation)
        initial_distance = distance(start_position, target)
        tolerance = float(action.get("tolerance") or 0.75)
        timeout_seconds = min(240.0, max(20.0, initial_distance * 3.0 + 10.0))
        deadline = time.monotonic() + timeout_seconds
        last_distance = initial_distance

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
                return True, f"arrived at move target within {current_distance:.2f} tiles"

            player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
            move = player.get("move") if isinstance(player.get("move"), dict) else {}
            if move and move.get("active") is False and current_distance > tolerance:
                return False, f"move stopped before target; remaining distance {current_distance:.2f}"
            time.sleep(0.5)

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

    def observe(self) -> dict[str, Any]:
        response = self._modless.observe()
        if not response.get("ok"):
            raise RuntimeError(f"no-mod observe failed: {response}")
        return response

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
        return self._modless.act(self._agent_action(action))

    def wait(self, ticks: int) -> dict[str, Any]:
        response = self.act({"type": "wait", "ticks": ticks})
        time.sleep(max(0.05, ticks / 60.0))
        return response


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
