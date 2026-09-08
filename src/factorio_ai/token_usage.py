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
DEFAULT_CODEX_STATE_DB = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "state_5.sqlite"


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
    weekly_used_percent: float | None = None
    weekly_resets_at: int | None = None
    source: str = "codex_state_db"

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
    session_path: Path | None = None,
    sessions_dir: Path | None = None,
) -> CodexThreadUsage:
    db_path = Path(state_db_path) if state_db_path is not None else DEFAULT_CODEX_STATE_DB
    # On a live default call the exact thread is authoritative, never whichever
    # concurrent thread most recently touched this checkout. Explicit DB callers
    # retain the older cwd-based lookup API (useful for offline reports).
    exact_id = thread_id or (os.getenv("CODEX_THREAD_ID") if state_db_path is None else None)
    db_error: Exception | None = None
    if session_path is None:
        try:
            return _current_codex_db_usage(db_path, cwd=cwd, thread_id=exact_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            db_error = exc
    exact_id = exact_id or os.getenv("CODEX_THREAD_ID")
    if session_path is None and exact_id:
        root = Path(sessions_dir) if sessions_dir is not None else db_path.parent / "sessions"
        session_path = _find_exact_codex_session(root, exact_id)
    if session_path is not None:
        summary = summarize_codex_session_usage(session_path)
        if exact_id and summary["thread_id"] != exact_id:
            raise ValueError("Codex session metadata does not match the requested thread")
        if not thread_id and cwd is not None and _normalized_codex_cwd(summary["cwd"]) != _normalized_codex_cwd(cwd):
            raise ValueError("Codex session cwd does not match the requested checkout")
        return CodexThreadUsage(
            thread_id=summary["thread_id"], cwd=summary["cwd"], tokens_used=summary["tokens_used"],
            updated_at_ms=summary["updated_at_ms"], weekly_used_percent=summary["weekly_used_percent"],
            weekly_resets_at=summary["weekly_resets_at"], source="codex_session_jsonl",
        )
    if db_error is not None:
        raise db_error
    raise ValueError("exact Codex session path or thread ID is required")


def _current_codex_db_usage(db_path: Path, *, cwd: Path | str | None, thread_id: str | None) -> CodexThreadUsage:
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


def _find_exact_codex_session(root: Path, thread_id: str) -> Path | None:
    # Enumerate filenames for this ID only; do not read unrelated private sessions.
    if not root.exists() or not thread_id or any(ch in thread_id for ch in "/*?[]\\"):
        return None
    matches = sorted(root.rglob(f"*{thread_id}.jsonl"))
    if len(matches) > 1:
        raise ValueError("multiple session files match the exact Codex thread; supply session_path")
    return matches[0] if matches else None


def summarize_codex_session_usage(path: Path, *, max_tail_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    """Read one session's metadata and bounded tail, never prompts or tool output.

    The reported token total is the latest cumulative counter, not a sum of repeated
    events. Weekly percentage is the account meter with an explicit 10080-minute
    window, which may be primary OR secondary; it is not this thread's quota share.
    """
    if max_tail_bytes < 1:
        raise ValueError("max_tail_bytes must be positive")
    path = Path(path)
    with path.open("rb") as file:
        header = file.readline(128 * 1024)
        file.seek(0, os.SEEK_END)
        size = file.tell()
        start = max(0, size - max_tail_bytes)
        if start:
            file.seek(start - 1)
            starts_at_line = file.read(1) == b"\n"
        else:
            starts_at_line = True
        file.seek(start)
        tail = file.read(max_tail_bytes)
    try:
        metadata = json.loads(header)
        if metadata.get("type") != "session_meta":
            raise ValueError("session metadata missing")
        meta = metadata["payload"]
        thread_id = meta["id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("session thread ID missing")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid Codex session metadata") from exc
    lines = tail.splitlines()
    # A seek into the middle of a JSON line must not reinterpret its suffix.
    if not starts_at_line:
        lines = lines[1:]
    tokens = updated_at_ms = weekly_percent = weekly_resets_at = None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        totals = info.get("total_token_usage") if isinstance(info, dict) else None
        if tokens is None and isinstance(totals, dict):
            value = totals.get("total_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                tokens = value
                try:
                    updated_at_ms = int(datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00")).timestamp() * 1000)
                except (ValueError, TypeError, OverflowError):
                    pass
        limits = payload.get("rate_limits")
        if weekly_percent is None and isinstance(limits, dict):
            for window in limits.values():
                if not isinstance(window, dict) or window.get("window_minutes") != 10080:
                    continue
                value = window.get("used_percent")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                    weekly_percent = float(value)
                    resets = window.get("resets_at")
                    weekly_resets_at = resets if isinstance(resets, int) and not isinstance(resets, bool) else None
                    break
        if tokens is not None and weekly_percent is not None:
            break
    if tokens is None:
        raise ValueError("no cumulative token_count event in the bounded Codex session tail")
    return {"thread_id": thread_id, "cwd": str(meta.get("cwd") or ""), "tokens_used": tokens,
            "updated_at_ms": updated_at_ms, "weekly_used_percent": weekly_percent,
            "weekly_resets_at": weekly_resets_at, "source": "codex_session_jsonl",
            "session_path": str(path), "weekly_percent_basis": "account_usage"}


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
