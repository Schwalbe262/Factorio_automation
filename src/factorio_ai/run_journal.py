from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from .config import REPO_ROOT


RUN_NOTES_LOG = "run-notes.jsonl"
RUN_INSIGHTS_LOG = "run-insights.jsonl"
NOTE_MD = "note.md"
INSIGHT_MD = "insight.md"
GOAL_MD = "goal.md"
KST = timezone(timedelta(hours=9))


NOTE_HEADER = """# Factorio Loop Notes

All exploration, validation, documentation, UI, strategy, and factory execution loops are recorded chronologically.

## Record Template

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Loop N

- Part:
- Goal:
- Hypothesis:
- Actions:
- Candidates:
- Metrics:
- Result:
- Failure reason:
- Next action:
- Token usage:
```
"""


INSIGHT_HEADER = """# Factorio Insights

Only confirmed improvements are appended here in chronological order. Plain execution logs, failures, and unproven hypotheses stay in `note.md`.

## Record Template

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Insight N

- Source loop:
- Improvement:
- Before:
- After:
- Evidence:
- Remaining risk:
```
"""


@dataclass(frozen=True)
class RunNote:
    timestamp: str
    loop_type: str
    objective: str
    goal: str
    ok: bool
    reason: str
    steps: int
    item_name: str = ""
    item_count: int = 0
    log_path: str = ""
    duration_seconds: float = 0.0
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metadata"] = self.metadata or {}
        return data


@dataclass(frozen=True)
class RunInsight:
    timestamp: str
    objective: str
    goal: str
    kind: str
    detail: str
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = self.evidence or {}
        return data


def record_run_note(log_dir: Path, note: RunNote, *, repo_root: Path = REPO_ROOT) -> RunNote:
    note_log_path = Path(log_dir) / RUN_NOTES_LOG
    loop_number = _next_markdown_record_index(repo_root / NOTE_MD, "Loop", fallback=_next_jsonl_index(note_log_path))
    payload = note.to_dict()
    payload["loop_number"] = loop_number
    _append_jsonl(note_log_path, payload)
    _append_markdown(
        repo_root / NOTE_MD,
        NOTE_HEADER,
        _note_markdown(note, loop_number),
    )
    return note


def record_run_insight(log_dir: Path, insight: RunInsight, *, repo_root: Path = REPO_ROOT) -> RunInsight:
    insight_log_path = Path(log_dir) / RUN_INSIGHTS_LOG
    insight_number = _next_markdown_record_index(repo_root / INSIGHT_MD, "Insight", fallback=_next_jsonl_index(insight_log_path))
    payload = insight.to_dict()
    payload["insight_number"] = insight_number
    _append_jsonl(insight_log_path, payload)
    _append_markdown(
        repo_root / INSIGHT_MD,
        INSIGHT_HEADER,
        _insight_markdown(insight, insight_number),
    )
    return insight


def record_skill_run_journal(
    log_dir: Path,
    *,
    objective: str,
    goal: str,
    ok: bool,
    reason: str,
    steps: int,
    item_name: str,
    initial_item_count: int,
    final_item_count: int,
    target: int,
    max_steps: int,
    log_path: Path,
    duration_seconds: float,
    repo_root: Path = REPO_ROOT,
) -> list[RunInsight]:
    note_log_path = Path(log_dir) / RUN_NOTES_LOG
    source_loop = _next_markdown_record_index(repo_root / NOTE_MD, "Loop", fallback=_next_jsonl_index(note_log_path))
    note = RunNote(
        timestamp=_now(),
        loop_type="skill",
        objective=objective,
        goal=goal,
        ok=ok,
        reason=reason,
        steps=steps,
        item_name=item_name,
        item_count=final_item_count,
        log_path=str(log_path),
        duration_seconds=round(max(0.0, duration_seconds), 3),
        metadata={
            "target": target,
            "max_steps": max_steps,
            "initial_item_count": initial_item_count,
            "final_item_count": final_item_count,
            "delta_item_count": final_item_count - initial_item_count,
        },
    )
    record_run_note(log_dir, note, repo_root=repo_root)

    insights: list[RunInsight] = []
    delta = final_item_count - initial_item_count
    if delta > 0:
        insights.append(
            RunInsight(
                timestamp=note.timestamp,
                objective=objective,
                goal=goal,
                kind="item_count_increased",
                detail=f"{item_name} increased by {delta} during {goal}.",
                evidence={
                    "source_loop": source_loop,
                    "item": item_name,
                    "initial": initial_item_count,
                    "final": final_item_count,
                    "delta": delta,
                    "target": target,
                },
            )
        )
    if ok and not _skill_completion_is_diagnostic_only(goal, reason, item_name):
        insights.append(
            RunInsight(
                timestamp=note.timestamp,
                objective=objective,
                goal=goal,
                kind="skill_completed",
                detail=f"{goal} completed after {steps} step(s): {reason}",
                evidence={
                    "source_loop": source_loop,
                    "steps": steps,
                    "item": item_name,
                    "item_count": final_item_count,
                    "target": target,
                },
            )
        )
    for insight in insights:
        record_run_insight(log_dir, insight, repo_root=repo_root)
    return insights


def _skill_completion_is_diagnostic_only(goal: str, reason: str, item_name: str) -> bool:
    if goal == "plan_factory_site" or item_name == "layout-plan":
        return True
    return "not_applied=true" in reason


def record_autopilot_cycle_journal(
    log_dir: Path,
    *,
    objective: str,
    cycle: int,
    selected_skill: str,
    ok: bool,
    reason: str,
    duration_seconds: float,
    strategy: dict[str, Any] | None = None,
    log_path: Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> RunNote:
    strategy = strategy if isinstance(strategy, dict) else {}
    note = RunNote(
        timestamp=_now(),
        loop_type="autopilot_cycle",
        objective=objective,
        goal=selected_skill,
        ok=ok,
        reason=reason,
        steps=cycle,
        log_path=str(log_path or ""),
        duration_seconds=round(max(0.0, duration_seconds), 3),
        metadata={
            "cycle": cycle,
            "strategy_source": strategy.get("source"),
            "priority": strategy.get("priority"),
        },
    )
    return record_run_note(log_dir, note, repo_root=repo_root)


def record_layout_loop_journal(
    log_dir: Path,
    *,
    loop_type: str,
    objective: str,
    cycle: int,
    active_skill: str,
    ok: bool,
    reason: str,
    log_path: Path | None = None,
    metadata: dict[str, Any] | None = None,
    repo_root: Path = REPO_ROOT,
) -> RunNote:
    note = RunNote(
        timestamp=_now(),
        loop_type=loop_type,
        objective=objective,
        goal=active_skill,
        ok=ok,
        reason=reason,
        steps=cycle,
        log_path=str(log_path or ""),
        metadata=metadata or {},
    )
    return record_run_note(log_dir, note, repo_root=repo_root)


def record_layout_result_insight(
    log_dir: Path,
    *,
    objective: str,
    active_skill: str,
    result: dict[str, Any],
    repo_root: Path = REPO_ROOT,
) -> RunInsight | None:
    if not isinstance(result, dict):
        return None
    evidence = result.get("improvement_evidence") if isinstance(result.get("improvement_evidence"), dict) else {}
    confirmed = bool(
        result.get("confirmed_improvement")
        or result.get("applied_improvement")
        or evidence.get("confirmed")
    )
    before = result.get("before_metrics", result.get("before", evidence.get("before")))
    after = result.get("after_metrics", result.get("after", evidence.get("after")))
    if not confirmed or before in (None, "") or after in (None, ""):
        return None
    candidate_id = str(result.get("selected_candidate_id") or "")
    detail = str(result.get("improvement") or result.get("detail") or candidate_id or "confirmed layout improvement")
    insight = RunInsight(
        timestamp=_now(),
        objective=objective,
        goal=active_skill,
        kind="layout_improvement_confirmed",
        detail=f"Layout work improved {active_skill}: {detail}",
        evidence={
            "selected_candidate_id": candidate_id,
            "score": result.get("score"),
            "source": result.get("source"),
            "before": before,
            "after": after,
            **evidence,
        },
    )
    return record_run_insight(log_dir, insight, repo_root=repo_root)


def run_journal_summary(log_dir: Path, *, limit: int = 20, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    notes = _load_jsonl(Path(log_dir) / RUN_NOTES_LOG)
    insights = _load_jsonl(Path(log_dir) / RUN_INSIGHTS_LOG)
    return {
        "notes": notes[-limit:] if limit >= 0 else notes,
        "note_count": len(notes),
        "note_log_path": str(Path(log_dir) / RUN_NOTES_LOG),
        "note_md_path": str(repo_root / NOTE_MD),
        "insights": insights[-limit:] if limit >= 0 else insights,
        "insight_count": len(insights),
        "insight_log_path": str(Path(log_dir) / RUN_INSIGHTS_LOG),
        "insight_md_path": str(repo_root / INSIGHT_MD),
        "goal": goal_summary(repo_root=repo_root),
    }


def goal_summary(*, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    path = repo_root / GOAL_MD
    if not path.exists():
        return {"exists": False, "path": str(path), "title": "", "summary": []}
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), "")
    summary: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") and len(summary) < 8:
            summary.append(stripped[2:])
    return {"exists": True, "path": str(path), "title": title, "summary": summary}


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        file.write("\n")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                rows.append(raw)
    return rows


def _append_markdown(path: Path, header: str, entry: str) -> None:
    if not path.exists():
        path.write_text(header.rstrip() + "\n\n", encoding="utf-8")
    existing = path.read_text(encoding="utf-8")
    separator = ""
    if existing and not existing.endswith("\n\n"):
        separator = "\n" if existing.endswith("\n") else "\n\n"
    with path.open("a", encoding="utf-8") as file:
        file.write(separator)
        file.write(entry.rstrip())
        file.write("\n\n")


def _note_markdown(note: RunNote, loop_number: int) -> str:
    metadata = note.metadata or {}
    parts = [
        f"## {_format_kst(note.timestamp)} - Loop {loop_number}",
        f"- Part: {_note_part(note)}",
        f"- Goal: {_note_goal(note)}",
        f"- Hypothesis: {_note_hypothesis(note)}",
        "- Actions:",
        *_markdown_subitems(_note_actions(note)),
        "- Candidates:",
        *_markdown_subitems(_note_candidates(note)),
        "- Metrics:",
        *_markdown_subitems(_note_metrics(note)),
        f"- Result: {_note_result(note)}",
        f"- Failure reason: {_note_failure_reason(note)}",
        f"- Next action: {_note_next_action(note)}",
        f"- Token usage: {_note_token_usage(metadata)}",
    ]
    return "\n".join(parts)


def _insight_markdown(insight: RunInsight, insight_number: int) -> str:
    evidence = insight.evidence or {}
    parts = [
        f"## {_format_kst(insight.timestamp)} - Insight {insight_number}",
        f"- Source loop: {_insight_source_loop(insight)}",
        f"- Improvement: {insight.detail}",
        f"- Before: {_insight_before(insight)}",
        f"- After: {_insight_after(insight)}",
        f"- Evidence: `{json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}`" if evidence else "- Evidence: not recorded",
        f"- Remaining risk: {_insight_remaining_risk(insight)}",
    ]
    return "\n".join(parts)


def _next_jsonl_index(path: Path) -> int:
    return len(_load_jsonl(path)) + 1


def _next_markdown_record_index(path: Path, label: str, *, fallback: int) -> int:
    if not path.exists():
        return fallback
    pattern = re.compile(rf"\b{re.escape(label)}\s+(\d+)\b")
    numbers: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if match:
            numbers.append(int(match.group(1)))
    return (max(numbers) + 1) if numbers else fallback


def _format_kst(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S +09:00")


def _note_part(note: RunNote) -> str:
    metadata = note.metadata or {}
    value = metadata.get("part")
    return str(value) if value else note.loop_type


def _note_goal(note: RunNote) -> str:
    return f"{note.objective} / {note.goal}"


def _note_hypothesis(note: RunNote) -> str:
    metadata = note.metadata or {}
    if metadata.get("hypothesis"):
        return str(metadata["hypothesis"])
    if note.loop_type == "skill":
        return f"Running `{note.goal}` should move the factory toward `{note.objective}`; item counts and the raw action log verify progress."
    if note.loop_type == "autopilot_cycle":
        return "The selected strategic skill is the highest-priority next loop given current factory, research, threat, and layout state."
    if "layout" in note.loop_type:
        return "Idle or planning time can be used to identify safer, denser, more automated factory-site improvements."
    return "This loop should either advance the current goal or produce evidence for the next safe action."


def _note_actions(note: RunNote) -> list[str]:
    metadata = note.metadata or {}
    if isinstance(metadata.get("actions"), list):
        return [str(item) for item in metadata["actions"]]
    if note.loop_type == "skill":
        actions = [f"Ran deterministic skill `{note.goal}` for up to {metadata.get('max_steps', note.steps)} step(s)."]
        if note.item_name:
            actions.append(f"Tracked `{note.item_name}` from {metadata.get('initial_item_count', 'unknown')} to {metadata.get('final_item_count', note.item_count)}.")
        if note.log_path:
            actions.append(f"Wrote raw action trace to `{note.log_path}`.")
        return actions
    if note.loop_type == "autopilot_cycle":
        return [
            f"Ran autopilot cycle {metadata.get('cycle', note.steps)}.",
            f"Selected `{note.goal}` with priority `{metadata.get('priority', 'unknown')}` from `{metadata.get('strategy_source', 'unknown')}` strategy.",
        ]
    if "layout" in note.loop_type:
        actions = [f"Ran layout loop `{note.loop_type}` for active skill `{note.goal}`."]
        if note.log_path:
            actions.append(f"Stored layout loop trace at `{note.log_path}`.")
        return actions
    return [f"Recorded loop `{note.loop_type}` for `{note.goal}`."]


def _note_candidates(note: RunNote) -> list[str]:
    metadata = note.metadata or {}
    if isinstance(metadata.get("candidates"), list):
        return [str(item) for item in metadata["candidates"]]
    candidates = [f"Selected goal/skill: `{note.goal}`."]
    if metadata.get("priority") is not None:
        candidates.append(f"Strategy priority: `{metadata.get('priority')}`.")
    if metadata.get("target") is not None and note.item_name:
        candidates.append(f"Target item candidate: `{note.item_name}` target `{metadata.get('target')}`.")
    return candidates


def _note_metrics(note: RunNote) -> list[str]:
    metadata = note.metadata or {}
    metrics = [f"Steps: {note.steps}.", f"Status: {'ok' if note.ok else 'failed'}."]
    if note.duration_seconds:
        metrics.append(f"Duration: {note.duration_seconds:.3f}s.")
    if note.item_name:
        metrics.append(
            f"{note.item_name}: {metadata.get('initial_item_count', 'unknown')} -> {metadata.get('final_item_count', note.item_count)} "
            f"(delta {metadata.get('delta_item_count', 'unknown')})."
        )
    if note.log_path:
        metrics.append(f"Log: `{note.log_path}`.")
    if metadata:
        compact = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        metrics.append(f"Metadata: `{compact}`.")
    return metrics


def _note_result(note: RunNote) -> str:
    metadata = note.metadata or {}
    delta = metadata.get("delta_item_count")
    if note.ok:
        return f"Completed: {note.reason}"
    if isinstance(delta, int) and delta > 0:
        return f"Partial progress despite loop stop: {note.reason}"
    return f"Loop stopped: {note.reason}"


def _note_failure_reason(note: RunNote) -> str:
    return "None" if note.ok else note.reason


def _note_next_action(note: RunNote) -> str:
    metadata = note.metadata or {}
    if metadata.get("next_action"):
        return str(metadata["next_action"])
    reason = note.reason.lower()
    if "logistic line" in reason or "hand-carry" in reason:
        return "Plan or build the missing site-to-site logistic line before repeating the consumer loop."
    if "cannot find" in reason:
        return "Inspect the raw log and patch planner/site selection before retrying the same loop."
    if "max steps reached" in reason:
        delta = metadata.get("delta_item_count")
        if isinstance(delta, int) and delta > 0:
            return "Continue only if the next decision still respects automation and site-logistics guardrails."
        return "Inspect repeated actions in the raw log and remove the bottleneck before increasing max steps."
    if note.ok:
        return "Advance to the next highest-priority goal from `goal.md`."
    return "Use the failure evidence to choose the next planner, strategy, or layout fix."


def _note_token_usage(metadata: dict[str, Any]) -> str:
    value = metadata.get("token_usage") or metadata.get("tokens")
    return str(value) if value else "not recorded for this loop / weekly quota unavailable"


def _markdown_subitems(items: list[str]) -> list[str]:
    if not items:
        return ["  - None"]
    return [f"  - {item}" for item in items]


def _insight_source_loop(insight: RunInsight) -> str:
    evidence = insight.evidence or {}
    source_loop = evidence.get("source_loop")
    if source_loop:
        return f"Loop {source_loop}"
    return f"{insight.objective} / {insight.goal}"


def _insight_before(insight: RunInsight) -> str:
    evidence = insight.evidence or {}
    if "initial" in evidence:
        return f"{evidence.get('item', 'tracked item')} = {evidence.get('initial')}"
    if "before" in evidence:
        return _compact_evidence_value(evidence["before"])
    return "not recorded"


def _insight_after(insight: RunInsight) -> str:
    evidence = insight.evidence or {}
    if "final" in evidence:
        return f"{evidence.get('item', 'tracked item')} = {evidence.get('final')}"
    if "item_count" in evidence:
        return f"{evidence.get('item', 'tracked item')} = {evidence.get('item_count')}"
    if "after" in evidence:
        return _compact_evidence_value(evidence["after"])
    return "not recorded"


def _insight_remaining_risk(insight: RunInsight) -> str:
    evidence = insight.evidence or {}
    target = evidence.get("target")
    final = evidence.get("final", evidence.get("item_count"))
    if isinstance(target, int) and isinstance(final, int) and final < target:
        return f"Target is not complete yet: {final}/{target}."
    return "Needs continued validation in later loops."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_evidence_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)
