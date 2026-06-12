from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from .config import AppConfig
from .models import (
    ActionValidationError,
    PlannerDecision,
    distance,
    player_position,
    total_item_count,
    validate_action,
)
from .planner import AutomationScienceSkill, BeltSmeltingLineSkill, CopperPlateSkill, ElectronicCircuitSkill, IronPlateSkill, SetupPowerSkill
from .rcon import FactorioRconClient
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy, make_strategy_payload, skill_catalog_payload
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


class FactorioController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

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

    def strategy_decision(self, objective: str, require_llm: bool = False) -> dict[str, Any]:
        observation = self.observe()
        production_targets = load_targets(self.cfg.runtime_dir, objective).per_minute
        result: dict[str, Any] | None = None
        if self.cfg.slurm_enabled:
            try:
                from . import remote_slurm

                result = remote_slurm.request_strategy(
                    objective=objective,
                    observation=observation,
                    production_targets=production_targets,
                    available_skills=skill_catalog_payload(),
                    timeout_seconds=30,
                )
            except Exception:
                if require_llm:
                    raise

        if result is None:
            try:
                from .slurm_worker import run_strategy_request

                result = run_strategy_request(make_strategy_payload(objective, observation, production_targets))
            except Exception:
                if require_llm:
                    raise
                result = heuristic_strategy(objective, observation, production_targets)
                result["source"] = "heuristic"
                result["ok"] = True

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
            return StrategyStepSummary(
                ok=False,
                reason=f"executor missing for selected skill: {selected}",
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
            )

        config = self._skill_run_config(selected, target_count=target_count, max_steps=max_steps)
        if config is None:
            return StrategyStepSummary(
                ok=False,
                reason=f"selected skill is not executable by the local runner: {selected}",
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
            )
        run = self._run_skill(**config)
        return StrategyStepSummary(
            ok=run.ok,
            reason=run.reason,
            objective=objective,
            selected_skill=selected,
            strategy=strategy,
            run=run,
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
        if skill_name == "setup_power":
            return {
                "skill": SetupPowerSkill(),
                "target_item": "steam",
                "target": 1,
                "goal": skill_name,
                "max_steps": max_steps or 900,
                "log_prefix": "strategy-power",
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
        log_path: Path | None = None,
    ) -> RunSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"{log_prefix}-{_timestamp()}.jsonl"

        with log_path.open("a", encoding="utf-8") as log_file:
            for step in range(1, max_steps + 1):
                observation = self.observe()
                decision = skill.next_action(observation)
                item_count = total_item_count(observation, target_item)
                self._write_log(log_file, step, observation, decision, None)
                if decision.done:
                    return RunSummary(True, decision.reason, step, item_count, log_path, target_item)
                if decision.action is None:
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
        return RunSummary(
            False,
            f"max steps reached: {max_steps}",
            max_steps,
            total_item_count(observation, target_item),
            log_path,
            target_item,
        )

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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
