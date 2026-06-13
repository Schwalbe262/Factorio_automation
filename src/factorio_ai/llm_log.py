from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


LLM_DECISION_LOG = "llm_decisions.jsonl"


@dataclass(frozen=True)
class LlmDecisionLogEntry:
    timestamp: str
    objective: str
    provider: str
    source: str
    ok: bool
    selected_skill: str
    priority: int | None
    reason: str
    blockers: list[str]
    expected_effect: str
    request_summary: dict[str, Any]
    error: str
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def llm_decision_log_path(log_dir: Path) -> Path:
    return Path(log_dir) / LLM_DECISION_LOG


def record_llm_decision(
    log_dir: Path,
    *,
    objective: str,
    provider: str,
    result: dict[str, Any] | None,
    request_summary: dict[str, Any] | None = None,
    error: str = "",
    latency_ms: int = 0,
    timestamp: str | None = None,
) -> LlmDecisionLogEntry:
    result = result if isinstance(result, dict) else {}
    blockers = result.get("blockers") if isinstance(result.get("blockers"), list) else []
    entry = LlmDecisionLogEntry(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        objective=objective,
        provider=provider,
        source=str(result.get("source") or ("error" if error else "")),
        ok=bool(result.get("source") == "llm" and not error),
        selected_skill=str(result.get("selected_skill") or result.get("selected_goal") or ""),
        priority=_optional_int(result.get("priority")),
        reason=str(result.get("reason") or ""),
        blockers=[str(item) for item in blockers],
        expected_effect=str(result.get("expected_effect") or ""),
        request_summary=request_summary or {},
        error=error,
        latency_ms=max(0, int(latency_ms or 0)),
    )
    path = llm_decision_log_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return entry


def load_llm_decisions(log_dir: Path, *, limit: int = 20) -> list[LlmDecisionLogEntry]:
    path = llm_decision_log_path(log_dir)
    if not path.exists():
        return []
    entries: list[LlmDecisionLogEntry] = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            entries.append(_entry_from_dict(raw))
    return entries[-limit:] if limit >= 0 else entries


def llm_decision_summary(log_dir: Path, *, limit: int = 20) -> dict[str, Any]:
    entries = load_llm_decisions(log_dir, limit=limit)
    return {
        "entries": [entry.to_dict() for entry in entries],
        "entry_count": len(entries),
        "latest": entries[-1].to_dict() if entries else None,
        "log_path": str(llm_decision_log_path(log_dir)),
    }


def strategy_request_summary(
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    inventory = observation.get("inventory") if isinstance(observation.get("inventory"), dict) else {}
    enemies = observation.get("enemies") if isinstance(observation.get("enemies"), list) else []
    research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
    technologies = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
    return {
        "tick": observation.get("tick"),
        "inventory": {str(key): inventory[key] for key in sorted(inventory)[:20]},
        "production_targets": dict(sorted((production_targets or {}).items())),
        "enemy_count": len(enemies),
        "current_research": research.get("current"),
        "researched_technologies": [
            name
            for name, value in sorted(technologies.items())
            if isinstance(value, dict) and value.get("researched")
        ][:20],
    }


def _entry_from_dict(raw: dict[str, Any]) -> LlmDecisionLogEntry:
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    request_summary = raw.get("request_summary") if isinstance(raw.get("request_summary"), dict) else {}
    return LlmDecisionLogEntry(
        timestamp=str(raw.get("timestamp") or ""),
        objective=str(raw.get("objective") or ""),
        provider=str(raw.get("provider") or ""),
        source=str(raw.get("source") or ""),
        ok=bool(raw.get("ok")),
        selected_skill=str(raw.get("selected_skill") or ""),
        priority=_optional_int(raw.get("priority")),
        reason=str(raw.get("reason") or ""),
        blockers=[str(item) for item in blockers],
        expected_effect=str(raw.get("expected_effect") or ""),
        request_summary=request_summary,
        error=str(raw.get("error") or ""),
        latency_ms=int(raw.get("latency_ms") or 0),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
