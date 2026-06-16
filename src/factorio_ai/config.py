from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FACTORIO_EXE = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
)


@dataclass(frozen=True)
class AppConfig:
    factorio_exe: Path
    runtime_dir: Path
    mod_runtime_dir: Path
    save_path: Path
    rcon_host: str
    rcon_port: int
    rcon_password: str
    server_port: int
    log_dir: Path
    agent_player_name: str
    slurm_enabled: bool


def _repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def _bool_value(value: Any, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _scheduler_mode_enabled() -> bool:
    mode = os.getenv("FACTORIO_AI_SLURM_MODE", "").strip().lower()
    if mode in {"scheduler", "slurm_scheduler", "scheduler_tasks"}:
        return True
    return _bool_value(os.getenv("FACTORIO_AI_SLURM_SCHEDULER_ENABLED"), False)


def _int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a JSON object: {path}")
    return data


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else REPO_ROOT / "config.json"
    example_path = REPO_ROOT / "config.example.json"
    raw = _load_json(example_path)
    raw.update(_load_json(config_path))

    factorio_exe = Path(
        os.getenv("FACTORIO_AI_EXE")
        or raw.get("factorio_exe")
        or str(DEFAULT_FACTORIO_EXE)
    )
    runtime_dir = _repo_path(os.getenv("FACTORIO_AI_RUNTIME_DIR") or raw.get("runtime_dir") or "runtime")
    mod_runtime_dir = _repo_path(
        os.getenv("FACTORIO_AI_MOD_RUNTIME_DIR") or raw.get("mod_runtime_dir") or runtime_dir / "mods"
    )
    save_path = _repo_path(os.getenv("FACTORIO_AI_SAVE_PATH") or raw.get("save_path") or runtime_dir / "saves" / "ai-mvp.zip")
    log_dir = _repo_path(os.getenv("FACTORIO_AI_LOG_DIR") or raw.get("log_dir") or "logs")

    slurm_default = _scheduler_mode_enabled() or _bool_value(raw.get("slurm_enabled"), False)

    return AppConfig(
        factorio_exe=factorio_exe,
        runtime_dir=runtime_dir,
        mod_runtime_dir=mod_runtime_dir,
        save_path=save_path,
        rcon_host=str(os.getenv("FACTORIO_AI_RCON_HOST") or raw.get("rcon_host") or "127.0.0.1"),
        rcon_port=_int_value(os.getenv("FACTORIO_AI_RCON_PORT") or raw.get("rcon_port"), 27015),
        rcon_password=str(os.getenv("FACTORIO_AI_RCON_PASSWORD") or raw.get("rcon_password") or "factorio-ai"),
        server_port=_int_value(os.getenv("FACTORIO_AI_SERVER_PORT") or raw.get("server_port"), 34197),
        log_dir=log_dir,
        agent_player_name=str(os.getenv("FACTORIO_AI_AGENT_PLAYER") or raw.get("agent_player_name") or "AI"),
        slurm_enabled=_bool_value(os.getenv("FACTORIO_AI_SLURM_ENABLED"), slurm_default),
    )
