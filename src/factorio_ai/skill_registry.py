from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


IMPLEMENTED_SKILLS = {
    "produce_iron_plate": "IronPlateSkill",
    "produce_automation_science_pack": "AutomationScienceSkill",
}


@dataclass(frozen=True)
class SkillImplementationStatus:
    name: str
    implemented: bool
    executor: str | None
    codex_required: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def skill_status(skill_name: str) -> SkillImplementationStatus:
    executor = IMPLEMENTED_SKILLS.get(skill_name)
    return SkillImplementationStatus(
        name=skill_name,
        implemented=executor is not None,
        executor=executor,
        codex_required=executor is None,
    )


def annotate_strategy_with_skill_status(strategy: dict[str, Any], runtime_dir: Path | None = None) -> dict[str, Any]:
    selected = str(strategy.get("selected_skill") or strategy.get("selected_goal") or "")
    status = skill_status(selected)
    annotated = dict(strategy)
    annotated["skill_status"] = status.to_dict()
    if status.codex_required and runtime_dir is not None:
        backlog_path = append_missing_skill_backlog(runtime_dir, annotated)
        annotated["missing_skill_backlog_path"] = str(backlog_path)
    return annotated


def append_missing_skill_backlog(runtime_dir: Path, strategy: dict[str, Any]) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "missing-skills.jsonl"
    selected_skill = strategy.get("selected_skill")
    if path.exists() and selected_skill:
        text = path.read_text(encoding="utf-8")
        if f'"selected_skill":"{selected_skill}"' in text:
            return path
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selected_skill": selected_skill,
        "reason": strategy.get("reason"),
        "blockers": strategy.get("blockers"),
        "expected_effect": strategy.get("expected_effect"),
        "skill_status": strategy.get("skill_status"),
    }
    with path.open("a", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")
    return path
