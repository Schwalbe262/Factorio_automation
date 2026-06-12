from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from .config import AppConfig
from .models import ActionValidationError, PlannerDecision, total_item_count, validate_action
from .planner import IronPlateSkill
from .rcon import FactorioRconClient


@dataclass
class RunSummary:
    ok: bool
    reason: str
    steps: int
    iron_plate_count: int
    log_path: Path


class FactorioController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def observe(self) -> dict[str, Any]:
        with self._client() as client:
            response = client.execute_json_command("ai_observe")
        if not response.get("ok"):
            raise RuntimeError(f"observe failed: {response}")
        return response

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
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
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"iron-mvp-{_timestamp()}.jsonl"
        skill = IronPlateSkill(target)

        with log_path.open("a", encoding="utf-8") as log_file:
            for step in range(1, max_steps + 1):
                observation = self.observe()
                decision = skill.next_action(observation)
                iron_count = total_item_count(observation, "iron-plate")
                self._write_log(log_file, step, observation, decision, None)
                if decision.done:
                    return RunSummary(True, decision.reason, step, iron_count, log_path)
                if decision.action is None:
                    return RunSummary(False, decision.reason, step, iron_count, log_path)

                action = self._maybe_apply_remote_hint(observation, decision)
                response = self.act(action)
                self._write_log(log_file, step, observation, decision, response)
                if not response.get("ok"):
                    return RunSummary(False, f"action failed: {response.get('reason')}", step, iron_count, log_path)
                if action.get("type") == "wait":
                    ticks = int(action.get("ticks") or 60)
                    time.sleep(max(0.05, ticks / 60.0))
                else:
                    time.sleep(0.2)

        observation = self.observe()
        return RunSummary(
            False,
            f"max steps reached: {max_steps}",
            max_steps,
            total_item_count(observation, "iron-plate"),
            log_path,
        )

    def _client(self) -> FactorioRconClient:
        return FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password)

    def _maybe_apply_remote_hint(self, observation: dict[str, Any], decision: PlannerDecision) -> dict[str, Any]:
        if not self.cfg.slurm_enabled or decision.action is None:
            return decision.action or {"type": "wait", "ticks": 60}
        try:
            from . import remote_slurm

            result = remote_slurm.request_plan(
                observation=observation,
                legal_actions=[decision.action],
                goal="produce_iron_plate",
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
