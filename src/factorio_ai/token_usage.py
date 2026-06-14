from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


TOKEN_USAGE_LOG = "token_usage.jsonl"


@dataclass(frozen=True)
class TokenUsageSample:
    timestamp: str
    tokens_used: int
    delta_tokens: int
    label: str
    source: str

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
    enriched = _samples_with_context(all_samples, weekly_quota)
    latest_enriched = enriched[-1]
    total_delta = sum(max(0, int(sample.get("delta_tokens") or 0)) for sample in enriched)
    latest_delta = max(0, int(latest_enriched.get("delta_tokens") or 0))
    return {
        "samples": enriched[-limit:] if limit >= 0 else enriched,
        "sample_count": len(all_samples),
        "latest_tokens": int(latest_enriched.get("cumulative_tokens") or 0),
        "latest_raw_tokens": all_samples[-1].tokens_used,
        "total_delta_tokens": total_delta,
        "latest_delta_tokens": latest_delta,
        "weekly_quota_tokens": weekly_quota,
        "latest_weekly_percent": _weekly_percent(latest_delta, weekly_quota),
        "counter_reset_count": sum(1 for sample in enriched if sample.get("counter_reset")),
        "latest_counter_reset": bool(latest_enriched.get("counter_reset")),
        "updated_at": all_samples[-1].timestamp,
        "log_path": str(token_usage_path(log_dir)),
    }


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
