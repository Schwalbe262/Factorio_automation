from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid


LLM_DECISION_LOG = "llm_decisions.jsonl"
LLM_IO_TRACE_LOG = "llm_io_traces.jsonl"


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


@dataclass(frozen=True)
class LlmIoTraceEntry:
    timestamp: str
    trace_id: str
    kind: str
    provider: str
    model: str
    base_url: str
    task_id: str
    system_prompt: str
    input_prompt: str
    raw_output: str
    parsed_json: dict[str, Any] | None
    duration_ms: int
    prompt_chars: int
    response_chars: int
    max_tokens: int | None
    ok: bool
    error: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def llm_decision_log_path(log_dir: Path) -> Path:
    return Path(log_dir) / LLM_DECISION_LOG


def llm_io_trace_log_path(log_dir: Path) -> Path:
    return Path(log_dir) / LLM_IO_TRACE_LOG


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


def make_llm_io_trace(
    *,
    kind: str,
    provider: str,
    model: str,
    base_url: str,
    system_prompt: str,
    input_prompt: str,
    raw_output: Any,
    parsed_json: dict[str, Any] | None = None,
    duration_ms: int = 0,
    max_tokens: int | None = None,
    ok: bool = False,
    error: str = "",
    task_id: str = "",
    timestamp: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    system_text = str(system_prompt or "")
    input_text = str(input_prompt or "")
    output_text = "" if raw_output is None else str(raw_output)
    entry = LlmIoTraceEntry(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        trace_id=trace_id or uuid.uuid4().hex,
        kind=str(kind or "llm"),
        provider=str(provider or ""),
        model=str(model or ""),
        base_url=str(base_url or ""),
        task_id=str(task_id or ""),
        system_prompt=system_text,
        input_prompt=input_text,
        raw_output=output_text,
        parsed_json=parsed_json if isinstance(parsed_json, dict) else None,
        duration_ms=max(0, int(duration_ms or 0)),
        prompt_chars=len(system_text) + len(input_text),
        response_chars=len(output_text),
        max_tokens=_optional_int(max_tokens),
        ok=bool(ok),
        error=str(error or ""),
    )
    return entry.to_dict()


def record_llm_io_trace(log_dir: Path, trace: dict[str, Any]) -> LlmIoTraceEntry:
    entry = _io_trace_from_dict(trace)
    path = llm_io_trace_log_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return entry


def extract_io_traces_from_result(result: Any) -> list[dict[str, Any]]:
    """Pull every embedded LLM I/O trace out of a worker result/diagnostics dict.

    Workers attach the trace(s) produced by :func:`make_llm_io_trace` under ``llm_trace``
    (single) and/or ``llm_traces`` (list). De-duplicates by ``trace_id`` so a result that
    carries both keys for the same call is not recorded twice.
    """

    traces: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return traces
    raw_traces = result.get("llm_traces")
    if isinstance(raw_traces, list):
        traces.extend(trace for trace in raw_traces if isinstance(trace, dict))
    raw_trace = result.get("llm_trace")
    seen = {str(trace.get("trace_id") or "") for trace in traces}
    if isinstance(raw_trace, dict) and str(raw_trace.get("trace_id") or "") not in seen:
        traces.append(raw_trace)
    return traces


def record_io_traces(log_dir: Path, traces: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Record each trace dict to the io-trace log. Returns ``(trace_ids, errors)``."""

    trace_ids: list[str] = []
    errors: list[str] = []
    for trace in traces:
        try:
            entry = record_llm_io_trace(log_dir, trace)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        trace_ids.append(entry.trace_id)
    return trace_ids, errors


def load_llm_io_traces(log_dir: Path, *, limit: int = 50) -> list[LlmIoTraceEntry]:
    path = llm_io_trace_log_path(log_dir)
    if not path.exists():
        return []
    entries: list[LlmIoTraceEntry] = []
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
            entries.append(_io_trace_from_dict(raw))
    return entries[-limit:] if limit >= 0 else entries


def llm_io_trace_summary(log_dir: Path, *, limit: int = 50) -> dict[str, Any]:
    entries = load_llm_io_traces(log_dir, limit=limit)
    return {
        "entries": [entry.to_dict() for entry in reversed(entries)],
        "entry_count": len(entries),
        "latest": entries[-1].to_dict() if entries else None,
        "log_path": str(llm_io_trace_log_path(log_dir)),
    }


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


def _io_trace_from_dict(raw: dict[str, Any]) -> LlmIoTraceEntry:
    system_prompt = str(raw.get("system_prompt") or "")
    input_prompt = str(raw.get("input_prompt") or "")
    raw_output = str(raw.get("raw_output") or "")
    parsed_json = raw.get("parsed_json") if isinstance(raw.get("parsed_json"), dict) else None
    prompt_chars = _optional_int(raw.get("prompt_chars"))
    response_chars = _optional_int(raw.get("response_chars"))
    return LlmIoTraceEntry(
        timestamp=str(raw.get("timestamp") or datetime.now(timezone.utc).isoformat()),
        trace_id=str(raw.get("trace_id") or uuid.uuid4().hex),
        kind=str(raw.get("kind") or "llm"),
        provider=str(raw.get("provider") or ""),
        model=str(raw.get("model") or ""),
        base_url=str(raw.get("base_url") or ""),
        task_id=str(raw.get("task_id") or ""),
        system_prompt=system_prompt,
        input_prompt=input_prompt,
        raw_output=raw_output,
        parsed_json=parsed_json,
        duration_ms=max(0, int(raw.get("duration_ms") or 0)),
        prompt_chars=prompt_chars if prompt_chars is not None else len(system_prompt) + len(input_prompt),
        response_chars=response_chars if response_chars is not None else len(raw_output),
        max_tokens=_optional_int(raw.get("max_tokens")),
        ok=bool(raw.get("ok")),
        error=str(raw.get("error") or ""),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
