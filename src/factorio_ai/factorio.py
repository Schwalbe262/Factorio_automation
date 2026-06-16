from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from .config import AppConfig, REPO_ROOT
from .rcon import FactorioRconClient, RconError
from .vanilla_gui import prepare_vanilla_mod_directory


MOD_NAME = "factorio_ai_autoplayer"
NO_MOD_SAVE_NAME = "no-mod-rcon.zip"
NO_MOD_MAP_GEN_SETTINGS_NAME = "safe-start-map-gen-settings.json"


def install_mod(cfg: AppConfig) -> Path:
    source = REPO_ROOT / "mods" / MOD_NAME
    if not source.exists():
        raise FileNotFoundError(f"mod source not found: {source}")
    cfg.mod_runtime_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.mod_runtime_dir / MOD_NAME
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    _write_mod_list(cfg.mod_runtime_dir)
    return target


def _write_mod_list(mod_dir: Path) -> None:
    mod_list_path = mod_dir / "mod-list.json"
    payload = {
        "mods": [
            {"name": "base", "enabled": True},
            {"name": MOD_NAME, "enabled": True},
        ]
    }
    mod_list_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_server_settings(cfg: AppConfig) -> Path:
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings_path = cfg.runtime_dir / "server-settings.json"
    payload = {
        "name": "Factorio AI Autoplayer",
        "description": "Local Factorio AI development server",
        "tags": ["ai", "local"],
        "max_players": 1,
        "visibility": {"public": False, "lan": False},
        "username": "",
        "token": "",
        "game_password": "",
        "require_user_verification": False,
        "max_upload_in_kilobytes_per_second": 0,
        "ignore_player_limit_for_returning_players": False,
        "allow_commands": "admins-only",
        "autosave_interval": 10,
        "autosave_slots": 5,
        "afk_autokick_interval": 0,
        "auto_pause": False,
        "only_admins_can_pause_the_game": True,
    }
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings_path


def no_mod_save_path(cfg: AppConfig) -> Path:
    return cfg.runtime_dir / "vanilla" / "saves" / NO_MOD_SAVE_NAME


def write_no_mod_map_gen_settings(cfg: AppConfig) -> Path:
    settings_path = cfg.runtime_dir / "vanilla" / NO_MOD_MAP_GEN_SETTINGS_NAME
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "width": 0,
        "height": 0,
        "starting_area": 4,
        "peaceful_mode": False,
        "autoplace_controls": {
            "coal": {"frequency": 1, "size": 1, "richness": 1},
            "stone": {"frequency": 1, "size": 1, "richness": 1},
            "copper-ore": {"frequency": 1, "size": 1, "richness": 1},
            "iron-ore": {"frequency": 1, "size": 1, "richness": 1},
            "uranium-ore": {"frequency": 1, "size": 1, "richness": 1},
            "crude-oil": {"frequency": 1, "size": 1, "richness": 1},
            "water": {"frequency": 1, "size": 1},
            "trees": {"frequency": 1, "size": 1},
            "enemy-base": {"frequency": 0.75, "size": 0.75},
        },
        "cliff_settings": {
            "name": "cliff",
            "cliff_elevation_0": 10,
            "cliff_elevation_interval": 0,
            "richness": 0,
        },
        "property_expression_names": {
            "control:moisture:frequency": "1",
            "control:moisture:bias": "0",
            "control:aux:frequency": "1",
            "control:aux:bias": "0",
        },
        "starting_points": [{"x": 0, "y": 0}],
        "seed": None,
    }
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings_path


def write_no_mod_server_settings(cfg: AppConfig) -> Path:
    server_dir = cfg.runtime_dir / "vanilla"
    server_dir.mkdir(parents=True, exist_ok=True)
    settings_path = server_dir / "server-settings.json"
    payload = {
        "name": "Factorio AI No-Mod RCON",
        "description": "Vanilla-compatible local/LAN server controlled by trusted RCON Lua commands.",
        "tags": ["ai", "vanilla", "rcon"],
        "max_players": 8,
        "visibility": {"public": False, "lan": True},
        "username": "",
        "token": "",
        "game_password": "",
        "require_user_verification": False,
        "max_upload_in_kilobytes_per_second": 0,
        "ignore_player_limit_for_returning_players": True,
        "allow_commands": "admins-only",
        "autosave_interval": 10,
        "autosave_slots": 5,
        "afk_autokick_interval": 0,
        "auto_pause": False,
        "only_admins_can_pause_the_game": True,
    }
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings_path


def write_client_config(cfg: AppConfig) -> Path:
    factorio_root = cfg.factorio_exe.parent.parent.parent
    read_data = factorio_root / "data"
    write_data = cfg.runtime_dir / "client-data"
    write_data.mkdir(parents=True, exist_ok=True)
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = cfg.runtime_dir / "client-config.ini"
    config_path.write_text(
        "\n".join(
            [
                "; version=13",
                "[path]",
                f"read-data={read_data.as_posix()}",
                f"write-data={write_data.as_posix()}",
                "",
                "[general]",
                "locale=auto",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def write_no_mod_server_config(cfg: AppConfig) -> Path:
    factorio_root = cfg.factorio_exe.parent.parent.parent
    read_data = factorio_root / "data"
    write_data = cfg.runtime_dir / "vanilla" / "server-data"
    write_data.mkdir(parents=True, exist_ok=True)
    config_path = cfg.runtime_dir / "vanilla" / "server-config.ini"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "; version=13",
                "[path]",
                f"read-data={read_data.as_posix()}",
                f"write-data={write_data.as_posix()}",
                "",
                "[general]",
                "locale=auto",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def create_save(cfg: AppConfig, overwrite: bool = False) -> Path:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    install_mod(cfg)
    cfg.save_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.save_path.exists() and not overwrite:
        return cfg.save_path
    if cfg.save_path.exists():
        cfg.save_path.unlink()
    command = [
        str(cfg.factorio_exe),
        "--mod-directory",
        str(cfg.mod_runtime_dir),
        "--create",
        str(cfg.save_path),
    ]
    subprocess.run(command, check=True, cwd=str(REPO_ROOT))
    return cfg.save_path


def build_create_no_mod_save_command(cfg: AppConfig, save_path: Path | None = None) -> list[str]:
    mod_dir = prepare_vanilla_mod_directory(cfg.runtime_dir)
    server_config = write_no_mod_server_config(cfg)
    map_gen_settings = write_no_mod_map_gen_settings(cfg)
    target_save = save_path or no_mod_save_path(cfg)
    return [
        str(cfg.factorio_exe),
        "--config",
        str(server_config),
        "--mod-directory",
        str(mod_dir),
        "--map-gen-settings",
        str(map_gen_settings),
        "--create",
        str(target_save),
    ]


def create_no_mod_save(cfg: AppConfig, overwrite: bool = False) -> Path:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    target_save = no_mod_save_path(cfg)
    target_save.parent.mkdir(parents=True, exist_ok=True)
    if target_save.exists() and not overwrite:
        return target_save
    if target_save.exists():
        target_save.unlink()
    subprocess.run(build_create_no_mod_save_command(cfg, target_save), check=True, cwd=str(REPO_ROOT))
    return target_save


def start_gui_client(
    cfg: AppConfig,
    window_size: str = "1600x900",
    connect: bool = True,
    ensure_mod: bool = True,
) -> subprocess.Popen[bytes]:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    if ensure_mod:
        install_mod(cfg)
    client_config = write_client_config(cfg)
    command = [
        str(cfg.factorio_exe),
        "--config",
        str(client_config),
        "--mod-directory",
        str(cfg.mod_runtime_dir),
        "--disable-migration-window",
        "--window-size",
        window_size,
    ]
    if connect:
        command.extend(["--mp-connect", f"{cfg.rcon_host}:{cfg.server_port}"])
    return subprocess.Popen(command, cwd=str(REPO_ROOT))


def start_no_mod_gui_client(
    cfg: AppConfig,
    window_size: str = "1600x900",
    connect: bool = True,
) -> subprocess.Popen[bytes]:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    client_config = write_client_config(cfg)
    mod_dir = prepare_vanilla_mod_directory(cfg.runtime_dir)
    command = [
        str(cfg.factorio_exe),
        "--config",
        str(client_config),
        "--mod-directory",
        str(mod_dir),
        "--disable-migration-window",
        "--window-size",
        window_size,
    ]
    if connect:
        command.extend(["--mp-connect", f"{cfg.rcon_host}:{cfg.server_port}"])
    return subprocess.Popen(command, cwd=str(REPO_ROOT))


def start_save_gui(cfg: AppConfig, window_size: str = "1600x900") -> subprocess.Popen[bytes]:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    install_mod(cfg)
    if not cfg.save_path.exists():
        raise FileNotFoundError(f"Factorio save not found: {cfg.save_path}")
    client_config = write_client_config(cfg)
    command = [
        str(cfg.factorio_exe),
        "--config",
        str(client_config),
        "--mod-directory",
        str(cfg.mod_runtime_dir),
        "--disable-migration-window",
        "--window-size",
        window_size,
        "--load-game",
        str(cfg.save_path),
    ]
    return subprocess.Popen(command, cwd=str(REPO_ROOT))


def start_server(cfg: AppConfig) -> subprocess.Popen[bytes]:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    if not cfg.save_path.exists():
        create_save(cfg)
    install_mod(cfg)
    server_settings = write_server_settings(cfg)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    console_log = cfg.log_dir / "factorio-server.log"
    command = [
        str(cfg.factorio_exe),
        "--mod-directory",
        str(cfg.mod_runtime_dir),
        "--start-server",
        str(cfg.save_path),
        "--server-settings",
        str(server_settings),
        "--port",
        str(cfg.server_port),
        "--rcon-port",
        str(cfg.rcon_port),
        "--rcon-password",
        cfg.rcon_password,
        "--console-log",
        str(console_log),
    ]
    return subprocess.Popen(command, cwd=str(REPO_ROOT))


def build_start_no_mod_server_command(
    cfg: AppConfig,
    *,
    save_path: Path | None = None,
    server_settings: Path | None = None,
    console_log: Path | None = None,
) -> list[str]:
    mod_dir = prepare_vanilla_mod_directory(cfg.runtime_dir)
    server_config = write_no_mod_server_config(cfg)
    target_save = save_path or no_mod_save_path(cfg)
    settings = server_settings or write_no_mod_server_settings(cfg)
    log_path = console_log or (cfg.log_dir / "factorio-no-mod-server.log")
    return [
        str(cfg.factorio_exe),
        "--config",
        str(server_config),
        "--mod-directory",
        str(mod_dir),
        "--start-server",
        str(target_save),
        "--server-settings",
        str(settings),
        "--port",
        str(cfg.server_port),
        "--rcon-port",
        str(cfg.rcon_port),
        "--rcon-password",
        cfg.rcon_password,
        "--console-log",
        str(log_path),
    ]


def start_no_mod_server(cfg: AppConfig) -> subprocess.Popen[bytes]:
    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    target_save = no_mod_save_path(cfg)
    if not target_save.exists():
        create_no_mod_save(cfg)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    command = build_start_no_mod_server_command(cfg, save_path=target_save)
    return subprocess.Popen(command, cwd=str(REPO_ROOT))


def wait_for_rcon(cfg: AppConfig, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with FactorioRconClient(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, timeout=5) as client:
                client.execute("/help", drain_seconds=0.05)
            return
        except (OSError, RconError) as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"RCON did not become ready within {timeout_seconds}s: {last_error}")


def save_no_mod_server(cfg: AppConfig, drain_seconds: float = 0.4) -> str:
    """Persist the live no-mod server state back into its save file via RCON ``/server-save``.

    The dedicated server is started with ``--start-server <no-mod-rcon.zip>`` and never writes that
    file back on its own (and a force-kill skips the graceful save), so without periodic saves every
    restart reloads the original map. Calling this keeps the named save current so restarts resume.
    """

    with FactorioRconClient(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, timeout=15) as client:
        return client.execute("/server-save", drain_seconds=drain_seconds)
