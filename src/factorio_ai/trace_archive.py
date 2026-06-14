from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .config import REPO_ROOT


KST = timezone(timedelta(hours=9))
ARCHIVE_DIR_NAME = "trace_archives"
MANIFEST_FILE = "manifest.json"
INDEX_FILE = "index.jsonl"
README_FILE = "README.md"


def archive_training_traces(
    log_dir: Path,
    output_root: Path,
    *,
    repo_root: Path = REPO_ROOT,
    label: str = "",
    copy_raw: bool = True,
    include_text_logs: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Archive local traces that are useful for GEPA/Qwen fine-tuning work."""

    source_files = _source_files(
        Path(log_dir),
        repo_root=Path(repo_root),
        include_text_logs=include_text_logs,
    )
    if limit is not None:
        source_files = source_files[: max(0, limit)]

    archive_dir = _archive_dir(Path(output_root), label)
    raw_dir = archive_dir / "raw"
    archive_dir.mkdir(parents=True, exist_ok=False)
    if copy_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for source_path in source_files:
        entry = _file_entry(source_path, repo_root=Path(repo_root), log_dir=Path(log_dir))
        if copy_raw:
            archive_relative = _raw_archive_path(source_path, repo_root=Path(repo_root), log_dir=Path(log_dir))
            destination = raw_dir / archive_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            entry["archive_path"] = str(Path("raw") / archive_relative)
        entries.append(entry)

    category_counts = Counter(str(entry.get("category") or "unknown") for entry in entries)
    priority_counts = Counter(str(entry.get("training_priority") or "unknown") for entry in entries)
    manifest = {
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S +09:00"),
        "archive_dir": str(archive_dir),
        "source_log_dir": str(Path(log_dir)),
        "repo_root": str(Path(repo_root)),
        "copy_raw": copy_raw,
        "include_text_logs": include_text_logs,
        "source_count": len(entries),
        "category_counts": dict(sorted(category_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
        "high_value_files": sum(1 for entry in entries if entry.get("training_priority") == "high"),
        "fine_tuning_use": {
            "gepa": "Use layout, strategy, LLM decision, validation, and journal traces as prompt-optimizer eval cases.",
            "qwen_lora": "Convert selected high-value traces into instruction/response examples after redacting secrets.",
            "layout_dataset": "Prioritize layout background, layout validation, and strategy-layout files for before/after reasoning.",
        },
        "files": entries,
    }

    manifest_path = archive_dir / MANIFEST_FILE
    index_path = archive_dir / INDEX_FILE
    readme_path = archive_dir / README_FILE
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with index_path.open("w", encoding="utf-8") as file:
        for entry in entries:
            file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            file.write("\n")
    readme_path.write_text(_archive_readme(manifest), encoding="utf-8")

    return {
        "archive_dir": str(archive_dir),
        "manifest_path": str(manifest_path),
        "index_path": str(index_path),
        "readme_path": str(readme_path),
        "source_count": len(entries),
        "category_counts": dict(sorted(category_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
        "high_value_files": manifest["high_value_files"],
        "copy_raw": copy_raw,
    }


def trace_archive_summary(output_root: Path, *, limit: int = 5) -> dict[str, Any]:
    root = Path(output_root)
    archives: list[dict[str, Any]] = []
    if root.exists():
        for manifest_path in sorted(root.glob(f"*/{MANIFEST_FILE}"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            archives.append(
                {
                    "label": raw.get("label") or manifest_path.parent.name,
                    "created_at": raw.get("created_at"),
                    "created_at_kst": raw.get("created_at_kst"),
                    "archive_dir": str(manifest_path.parent),
                    "source_count": raw.get("source_count", 0),
                    "high_value_files": raw.get("high_value_files", 0),
                    "category_counts": raw.get("category_counts") if isinstance(raw.get("category_counts"), dict) else {},
                    "priority_counts": raw.get("priority_counts") if isinstance(raw.get("priority_counts"), dict) else {},
                    "manifest_path": str(manifest_path),
                    "index_path": str(manifest_path.parent / INDEX_FILE),
                    "readme_path": str(manifest_path.parent / README_FILE),
                }
            )
    entries = archives[:limit] if limit >= 0 else archives
    return {
        "archive_root": str(root),
        "archive_count": len(archives),
        "archives": entries,
        "latest": entries[0] if entries else None,
    }


def _source_files(log_dir: Path, *, repo_root: Path, include_text_logs: bool) -> list[Path]:
    sources: list[Path] = []
    if log_dir.exists():
        for path in log_dir.iterdir():
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            if path.suffix == ".jsonl":
                sources.append(path)
            elif include_text_logs and path.suffix in {".log", ".err", ".out"}:
                sources.append(path)
    for relative in [
        "goal.md",
        "note.md",
        "insight.md",
        "docs/CLI_HANDOFF.md",
        "docs/TRACE_ARCHIVE.md",
    ]:
        path = repo_root / relative
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            sources.append(path)
    return sorted(sources, key=_source_sort_key)


def _source_sort_key(path: Path) -> tuple[int, float, str]:
    category, priority, _use = classify_trace_file(path)
    priority_rank = {"high": 0, "medium": 1, "low": 2}.get(priority, 3)
    category_rank = {
        "layout_background": 0,
        "layout_validation": 1,
        "layout_strategy": 2,
        "llm_decisions": 3,
        "strategy_run": 4,
        "loop_journal": 5,
        "insight_journal": 6,
        "goal_document": 7,
        "handoff_document": 8,
    }.get(category, 20)
    try:
        mtime = -path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (priority_rank, category_rank, mtime, path.name)


def classify_trace_file(path: Path) -> tuple[str, str, str]:
    name = path.name.lower()
    stem = path.stem.lower()
    if name == "layout-improvement-background.jsonl":
        return (
            "layout_background",
            "high",
            "Layout-improvement loop scheduling/results; key data for GEPA evals and layout LoRA examples.",
        )
    if name == "layout-validation-feedback.jsonl":
        return (
            "layout_validation",
            "high",
            "Sandbox pass/fail evidence for layout candidates; key supervised signal for build-ready decisions.",
        )
    if name.startswith("strategy-layout") or "layout-improvement" in name:
        return (
            "layout_strategy",
            "high",
            "Strategy trace for choosing layout planning or compaction candidates.",
        )
    if name == "llm_decisions.jsonl":
        return (
            "llm_decisions",
            "high",
            "LLM/heuristic prompt, decision, fallback, latency, and error trace.",
        )
    if "operator-intervention" in name or "manual-layout" in name or "factory-events" in name:
        return (
            "operator_intervention",
            "high",
            "Human factory edits and before/after layout comparisons; accept as insight only when metrics improve.",
        )
    if name.startswith("strategy-") and name.endswith(".jsonl"):
        return (
            "strategy_run",
            "high",
            "Objective, observation, selected skill, execution result, and failure trace for strategic policy learning.",
        )
    if name == "run-notes.jsonl":
        return ("loop_journal", "high", "Structured loop descriptions aligned to note.md.")
    if name == "run-insights.jsonl":
        return ("insight_journal", "high", "Structured confirmed improvements aligned to insight.md.")
    if name == "token_usage.jsonl":
        return ("token_usage", "medium", "Cost/usage signal for scheduling and budget-aware experiments.")
    if stem == "goal":
        return ("goal_document", "medium", "Current mission, quality criteria, and learning roadmap.")
    if stem == "note":
        return ("loop_notes_markdown", "high", "Human-readable loop notes with context that raw traces may not contain.")
    if stem == "insight":
        return ("insights_markdown", "high", "Human-readable confirmed improvements with before/after evidence.")
    if path.as_posix().lower().endswith("docs/cli_handoff.md"):
        return ("handoff_document", "medium", "Implementation history, commands, and known next steps.")
    if path.as_posix().lower().endswith("docs/trace_archive.md"):
        return ("trace_archive_document", "medium", "Trace archive operating procedure.")
    if path.suffix.lower() in {".log", ".err", ".out"}:
        return ("runtime_text_log", "low", "Runtime/server diagnostics. Preserve for audit; usually not direct fine-tuning input.")
    return ("other_trace", "medium", "Unclassified trace; inspect before dataset conversion.")


def _file_entry(path: Path, *, repo_root: Path, log_dir: Path) -> dict[str, Any]:
    category, priority, fine_tune_use = classify_trace_file(path)
    stat = path.stat()
    entry: dict[str, Any] = {
        "source_path": str(path),
        "source_relative_path": _relative_path(path, repo_root=repo_root, log_dir=log_dir),
        "category": category,
        "training_priority": priority,
        "fine_tune_use": fine_tune_use,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": _sha256(path),
    }
    if path.suffix == ".jsonl":
        entry.update(_jsonl_stats(path))
    else:
        entry.update(_text_stats(path))
    return entry


def _jsonl_stats(path: Path) -> dict[str, Any]:
    line_count = 0
    valid_records = 0
    invalid_records = 0
    keys: set[str] = set()
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    first_timestamp: Any = None
    last_timestamp: Any = None
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            if not line.strip():
                continue
            line_count += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                invalid_records += 1
                continue
            if not isinstance(raw, dict):
                invalid_records += 1
                continue
            valid_records += 1
            keys.update(str(key) for key in raw.keys())
            compact = _compact_value(raw)
            if first is None:
                first = compact
                first_timestamp = _record_timestamp(raw)
            last = compact
            last_timestamp = _record_timestamp(raw) or last_timestamp
    return {
        "line_count": line_count,
        "valid_json_records": valid_records,
        "invalid_json_records": invalid_records,
        "json_keys": sorted(keys)[:80],
        "first_record": first or {},
        "last_record": last or {},
        "first_record_timestamp": first_timestamp,
        "last_record_timestamp": last_timestamp,
    }


def _text_stats(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8", errors="replace") as file:
        lines = file.readlines()
    non_empty = [line.rstrip() for line in lines if line.strip()]
    return {
        "line_count": len(lines),
        "valid_json_records": 0,
        "invalid_json_records": 0,
        "first_record": {"text": _truncate(non_empty[0])} if non_empty else {},
        "last_record": {"text": _truncate(non_empty[-1])} if non_empty else {},
    }


def _archive_dir(output_root: Path, label: str) -> Path:
    slug = _slug(label) or "training-traces"
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    candidate = output_root / f"{stamp}-{slug}"
    if not candidate.exists():
        return candidate
    suffix = 2
    while True:
        retry = output_root / f"{stamp}-{slug}-{suffix}"
        if not retry.exists():
            return retry
        suffix += 1


def _raw_archive_path(path: Path, *, repo_root: Path, log_dir: Path) -> Path:
    try:
        return Path("logs") / path.relative_to(log_dir)
    except ValueError:
        pass
    try:
        return Path("repo") / path.relative_to(repo_root)
    except ValueError:
        return Path("external") / path.name


def _relative_path(path: Path, *, repo_root: Path, log_dir: Path) -> str:
    try:
        return str(Path("logs") / path.relative_to(log_dir))
    except ValueError:
        pass
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_timestamp(row: dict[str, Any]) -> Any:
    for key in ("timestamp", "time", "created_at", "updated_at"):
        value = row.get(key)
        if value:
            return value
    return None


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return _truncate(str(value), limit=120)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, key in enumerate(sorted(value.keys(), key=str)):
            if index >= 24:
                result["..."] = f"{len(value) - index} more keys"
                break
            result[str(key)] = _compact_value(value[key], depth=depth + 1)
        return result
    if isinstance(value, list):
        compact = [_compact_value(item, depth=depth + 1) for item in value[:8]]
        if len(value) > 8:
            compact.append(f"... {len(value) - 8} more items")
        return compact
    if isinstance(value, str):
        return _truncate(value)
    return value


def _truncate(value: str, *, limit: int = 240) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", label.strip().lower()).strip("-")
    return slug[:80]


def _archive_readme(manifest: dict[str, Any]) -> str:
    category_lines = "\n".join(
        f"- {category}: {count}" for category, count in sorted((manifest.get("category_counts") or {}).items())
    )
    priority_lines = "\n".join(
        f"- {priority}: {count}" for priority, count in sorted((manifest.get("priority_counts") or {}).items())
    )
    return f"""# Factorio Training Trace Archive

- Label: {manifest.get("label") or "(none)"}
- Created: {manifest.get("created_at_kst")}
- Source count: {manifest.get("source_count")}
- High-value files: {manifest.get("high_value_files")}
- Raw copies included: {manifest.get("copy_raw")}

## Files

- `manifest.json`: full archive metadata and file summaries.
- `index.jsonl`: one machine-readable row per archived source file.
- `raw/`: copied source traces when `copy_raw` is true.

## Categories

{category_lines or "- none"}

## Priorities

{priority_lines or "- none"}

## Dataset Use

- GEPA: build prompt-evaluation cases from layout, strategy, validation, LLM decision, and journal traces.
- Qwen LoRA: convert selected high-priority rows into instruction/response records after redaction.
- Layout tuning: prefer `layout_background`, `layout_validation`, and `layout_strategy` categories for before/after examples.

This archive is local runtime data. Git tracks the exporter and documentation, not raw gameplay logs.
"""
