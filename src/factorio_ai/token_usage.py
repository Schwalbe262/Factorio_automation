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
    delta = max(0, tokens_used - last_tokens)
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
    samples = all_samples[-limit:] if limit >= 0 else all_samples
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
            "updated_at": None,
            "log_path": str(token_usage_path(log_dir)),
        }
    first = all_samples[0]
    latest = all_samples[-1]
    total_delta = max(0, latest.tokens_used - first.tokens_used)
    latest_delta = max(0, latest.delta_tokens)
    return {
        "samples": [_sample_with_weekly_percent(sample, weekly_quota) for sample in samples],
        "sample_count": len(all_samples),
        "latest_tokens": latest.tokens_used,
        "total_delta_tokens": total_delta,
        "latest_delta_tokens": latest_delta,
        "weekly_quota_tokens": weekly_quota,
        "latest_weekly_percent": _weekly_percent(latest_delta, weekly_quota),
        "updated_at": latest.timestamp,
        "log_path": str(token_usage_path(log_dir)),
    }


def _weekly_token_quota() -> int | None:
    raw = os.getenv("FACTORIO_AI_WEEKLY_TOKEN_QUOTA")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        quota = int(raw)
    except (TypeError, ValueError):
        return None
    return quota if quota > 0 else None


def _sample_with_weekly_percent(sample: TokenUsageSample, weekly_quota: int | None) -> dict[str, Any]:
    data = sample.to_dict()
    data["weekly_percent"] = _weekly_percent(max(0, sample.delta_tokens), weekly_quota)
    return data


def _weekly_percent(delta_tokens: int, weekly_quota: int | None) -> float | None:
    if weekly_quota is None or weekly_quota <= 0:
        return None
    return round((max(0, delta_tokens) / weekly_quota) * 100.0, 4)
