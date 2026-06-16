from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


SETTINGS_FILENAME = "layout-llm-settings.json"
DEFAULT_MAX_ACTIVE_LAYOUT_TASKS = 2
MIN_MAX_ACTIVE_LAYOUT_TASKS = 1
MAX_MAX_ACTIVE_LAYOUT_TASKS = 8


def clamp_max_active_layout_tasks(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_ACTIVE_LAYOUT_TASKS
    return max(MIN_MAX_ACTIVE_LAYOUT_TASKS, min(MAX_MAX_ACTIVE_LAYOUT_TASKS, parsed))


def default_max_active_layout_tasks() -> int:
    return clamp_max_active_layout_tasks(
        os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS")
        or os.getenv("FACTORIO_AI_LAYOUT_LLM_MAX_ACTIVE_TASKS")
        or DEFAULT_MAX_ACTIVE_LAYOUT_TASKS
    )


def layout_llm_settings_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / SETTINGS_FILENAME


def load_layout_llm_settings(runtime_dir: str | Path) -> dict[str, Any]:
    path = layout_llm_settings_path(runtime_dir)
    value = default_max_active_layout_tasks()
    source = "env-default"
    updated_at = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and "max_active_layout_tasks" in data:
            value = clamp_max_active_layout_tasks(data.get("max_active_layout_tasks"))
            source = "runtime"
            updated_at = data.get("updated_at")
    return {
        "max_active_layout_tasks": value,
        "min_active_layout_tasks": MIN_MAX_ACTIVE_LAYOUT_TASKS,
        "max_allowed_active_layout_tasks": MAX_MAX_ACTIVE_LAYOUT_TASKS,
        "settings_path": str(path),
        "source": source,
        "updated_at": updated_at,
    }


def save_layout_llm_settings(runtime_dir: str | Path, max_active_layout_tasks: Any) -> dict[str, Any]:
    path = layout_llm_settings_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = clamp_max_active_layout_tasks(max_active_layout_tasks)
    payload = {
        "max_active_layout_tasks": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_layout_llm_settings(runtime_dir)
