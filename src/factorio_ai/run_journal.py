from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .config import REPO_ROOT


RUN_NOTES_LOG = "run-notes.jsonl"
RUN_INSIGHTS_LOG = "run-insights.jsonl"
NOTE_MD = "note.md"
INSIGHT_MD = "insight.md"
GOAL_MD = "goal.md"


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
    _append_jsonl(Path(log_dir) / RUN_NOTES_LOG, note.to_dict())
    _append_markdown(
        repo_root / NOTE_MD,
        "# Factorio Run Notes\n\nChronological human-readable loop execution journal.\n",
        _note_markdown(note),
    )
    return note


def record_run_insight(log_dir: Path, insight: RunInsight, *, repo_root: Path = REPO_ROOT) -> RunInsight:
    _append_jsonl(Path(log_dir) / RUN_INSIGHTS_LOG, insight.to_dict())
    _append_markdown(
        repo_root / INSIGHT_MD,
        "# Factorio Run Insights\n\nChronological record of meaningful factory or agent improvements.\n",
        _insight_markdown(insight),
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
                    "item": item_name,
                    "initial": initial_item_count,
                    "final": final_item_count,
                    "delta": delta,
                    "target": target,
                },
            )
        )
    if ok:
        insights.append(
            RunInsight(
                timestamp=note.timestamp,
                objective=objective,
                goal=goal,
                kind="skill_completed",
                detail=f"{goal} completed after {steps} step(s): {reason}",
                evidence={
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
    candidate_id = str(result.get("selected_candidate_id") or "")
    focus = str(result.get("next_simulation_focus") or "")
    if not candidate_id and not focus:
        return None
    detail = candidate_id or focus
    insight = RunInsight(
        timestamp=_now(),
        objective=objective,
        goal=active_skill,
        kind="layout_candidate_improved",
        detail=f"Layout work produced a candidate/focus for {active_skill}: {detail}",
        evidence={
            "selected_candidate_id": candidate_id,
            "score": result.get("score"),
            "source": result.get("source"),
            "next_simulation_focus": focus,
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
    with path.open("a", encoding="utf-8") as file:
        file.write(entry.rstrip())
        file.write("\n\n")


def _note_markdown(note: RunNote) -> str:
    status = "ok" if note.ok else "failed"
    parts = [
        f"## {note.timestamp} - {note.loop_type} - {status}",
        f"- Objective: `{note.objective}`",
        f"- Goal: `{note.goal}`",
        f"- Steps: {note.steps}",
        f"- Reason: {note.reason}",
    ]
    if note.item_name:
        parts.append(f"- Item: `{note.item_name}` = {note.item_count}")
    if note.duration_seconds:
        parts.append(f"- Duration: {note.duration_seconds:.3f}s")
    if note.log_path:
        parts.append(f"- Log: `{note.log_path}`")
    metadata = note.metadata or {}
    if metadata:
        compact = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        parts.append(f"- Metadata: `{compact}`")
    return "\n".join(parts)


def _insight_markdown(insight: RunInsight) -> str:
    parts = [
        f"## {insight.timestamp} - {insight.kind}",
        f"- Objective: `{insight.objective}`",
        f"- Goal: `{insight.goal}`",
        f"- Detail: {insight.detail}",
    ]
    evidence = insight.evidence or {}
    if evidence:
        compact = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        parts.append(f"- Evidence: `{compact}`")
    return "\n".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
