from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import sqlite3
from typing import Any


TOKEN_USAGE_LOG = "token_usage.jsonl"
DEFAULT_CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"


@dataclass(frozen=True)
class TokenUsageSample:
    timestamp: str
    tokens_used: int
    delta_tokens: int
    label: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodexThreadUsage:
    thread_id: str
    cwd: str
    tokens_used: int
    updated_at_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def token_usage_path(log_dir: Path) -> Path:
    return Path(log_dir) / TOKEN_USAGE_LOG


def record_token_usage(
    log_dir: Path,
    tokens_used: int,
    *,
    label: str = "",
    source: str = "codex",
    timestamp: str | None = None,
) -> TokenUsageSample:
    if tokens_used < 0:
        raise ValueError("tokens_used must be non-negative")
    path = token_usage_path(log_dir)
    previous = load_token_usage(log_dir)
    last_tokens = previous[-1].tokens_used if previous else tokens_used
    delta = _sample_delta_tokens(tokens_used, last_tokens)
    sample = TokenUsageSample(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        tokens_used=int(tokens_used),
        delta_tokens=int(delta),
        label=label,
        source=source,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(sample.to_dict(), ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return sample


def record_current_codex_thread_usage(
    log_dir: Path,
    *,
    state_db_path: Path | None = None,
    cwd: Path | str | None = None,
    thread_id: str | None = None,
    label: str = "",
    source: str = "codex_thread",
    timestamp: str | None = None,
) -> tuple[TokenUsageSample, CodexThreadUsage]:
    thread = current_codex_thread_usage(
        state_db_path=state_db_path,
        cwd=cwd,
        thread_id=thread_id,
    )
    sample = record_token_usage(
        log_dir,
        thread.tokens_used,
        label=label,
        source=source,
        timestamp=timestamp,
    )
    return sample, thread


def current_codex_thread_usage(
    *,
    state_db_path: Path | None = None,
    cwd: Path | str | None = None,
    thread_id: str | None = None,
) -> CodexThreadUsage:
    db_path = Path(state_db_path) if state_db_path is not None else DEFAULT_CODEX_STATE_DB
    if not db_path.exists():
        raise FileNotFoundError(f"Codex state DB not found: {db_path}")

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if thread_id:
            row = conn.execute(
                "SELECT id, cwd, tokens_used, updated_at_ms, updated_at FROM threads WHERE id = ? LIMIT 1",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Codex thread not found: {thread_id}")
            return _codex_thread_usage_from_row(row)

        target_cwd = _normalized_codex_cwd(cwd if cwd is not None else Path.cwd())
        rows = conn.execute("SELECT id, cwd, tokens_used, updated_at_ms, updated_at FROM threads").fetchall()
        candidates = [
            _codex_thread_usage_from_row(row)
            for row in rows
            if _normalized_codex_cwd(str(row["cwd"] or "")) == target_cwd
        ]
    finally:
        conn.close()

    if not candidates:
        raise ValueError(f"Codex thread not found for cwd: {cwd if cwd is not None else Path.cwd()}")
    candidates.sort(key=lambda item: ((item.updated_at_ms or 0), item.thread_id), reverse=True)
    return candidates[0]


def _codex_thread_usage_from_row(row: sqlite3.Row) -> CodexThreadUsage:
    updated_at_ms = row["updated_at_ms"]
    if updated_at_ms is None and row["updated_at"] is not None:
        updated_at_ms = int(row["updated_at"]) * 1000
    return CodexThreadUsage(
        thread_id=str(row["id"]),
        cwd=str(row["cwd"] or ""),
        tokens_used=int(row["tokens_used"] or 0),
        updated_at_ms=int(updated_at_ms) if updated_at_ms is not None else None,
    )


def _normalized_codex_cwd(value: Path | str) -> str:
    text = str(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))


def load_token_usage(log_dir: Path, *, limit: int | None = None) -> list[TokenUsageSample]:
    path = token_usage_path(log_dir)
    if not path.exists():
        return []
    samples: list[TokenUsageSample] = []
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
            try:
                samples.append(
                    TokenUsageSample(
                        timestamp=str(raw.get("timestamp") or ""),
                        tokens_used=int(raw.get("tokens_used") or 0),
                        delta_tokens=int(raw.get("delta_tokens") or 0),
                        label=str(raw.get("label") or ""),
                        source=str(raw.get("source") or "codex"),
                    )
                )
            except (TypeError, ValueError):
                continue
    if limit is not None and limit >= 0:
        return samples[-limit:]
    return samples


def token_usage_summary(log_dir: Path, *, limit: int = 120) -> dict[str, Any]:
    all_samples = load_token_usage(log_dir)
    weekly_quota = _weekly_token_quota()
    if not all_samples:
        return {
            "samples": [],
            "sample_count": 0,
            "latest_tokens": 0,
            "total_delta_tokens": 0,
            "latest_delta_tokens": 0,
            "weekly_quota_tokens": weekly_quota,
            "latest_weekly_percent": None,
            "counter_reset_count": 0,
            "latest_counter_reset": False,
            "updated_at": None,
            "log_path": str(token_usage_path(log_dir)),
        }
    display_samples = _active_counter_basis_samples(all_samples)
    enriched = _samples_with_context(display_samples, weekly_quota)
    latest_enriched = enriched[-1]
    total_delta = sum(max(0, int(sample.get("delta_tokens") or 0)) for sample in enriched)
    latest_delta = max(0, int(latest_enriched.get("delta_tokens") or 0))
    return {
        "samples": enriched[-limit:] if limit >= 0 else enriched,
        "sample_count": len(display_samples),
        "latest_tokens": int(latest_enriched.get("cumulative_tokens") or 0),
        "latest_raw_tokens": display_samples[-1].tokens_used,
        "total_delta_tokens": total_delta,
        "latest_delta_tokens": latest_delta,
        "weekly_quota_tokens": weekly_quota,
        "latest_weekly_percent": _weekly_percent(latest_delta, weekly_quota),
        "counter_reset_count": sum(1 for sample in enriched if sample.get("counter_reset")),
        "latest_counter_reset": bool(latest_enriched.get("counter_reset")),
        "updated_at": display_samples[-1].timestamp,
        "log_path": str(token_usage_path(log_dir)),
        "sample_basis_source": display_samples[-1].source,
        "ignored_older_basis_samples": max(0, len(all_samples) - len(display_samples)),
    }


def _active_counter_basis_samples(samples: list[TokenUsageSample]) -> list[TokenUsageSample]:
    if not samples:
        return []
    if samples[-1].source != "codex_thread":
        return samples
    start = len(samples) - 1
    while start > 0 and samples[start - 1].source == "codex_thread":
        start -= 1
    return samples[start:]


def _sample_delta_tokens(tokens_used: int, last_tokens: int) -> int:
    if tokens_used < last_tokens:
        return tokens_used
    return tokens_used - last_tokens


def _weekly_token_quota() -> int | None:
    raw = os.getenv("FACTORIO_AI_WEEKLY_TOKEN_QUOTA")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        quota = int(raw)
    except (TypeError, ValueError):
        return None
    return quota if quota > 0 else None


def _samples_with_context(samples: list[TokenUsageSample], weekly_quota: int | None) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    session_index = 0
    previous_tokens: int | None = None
    cumulative_tokens: int | None = None
    for sample in samples:
        counter_reset = previous_tokens is not None and sample.tokens_used < previous_tokens
        if counter_reset:
            session_index += 1
        if previous_tokens is None:
            delta = 0
        else:
            delta = max(0, int(sample.delta_tokens))
        if delta == 0 and previous_tokens is not None:
            delta = _sample_delta_tokens(sample.tokens_used, previous_tokens)
        if cumulative_tokens is None:
            cumulative_tokens = sample.tokens_used
        else:
            cumulative_tokens += delta
        data = sample.to_dict()
        data["delta_tokens"] = delta
        data["cumulative_tokens"] = cumulative_tokens
        data["counter_reset"] = counter_reset
        data["counter_session"] = session_index
        data["weekly_percent"] = _weekly_percent(delta, weekly_quota)
        enriched.append(data)
        previous_tokens = sample.tokens_used
    return enriched


def _weekly_percent(delta_tokens: int, weekly_quota: int | None) -> float | None:
    if weekly_quota is None or weekly_quota <= 0:
        return None
    return round((max(0, delta_tokens) / weekly_quota) * 100.0, 4)
