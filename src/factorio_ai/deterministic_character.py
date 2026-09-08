"""An isolated, dedicated GUI connection for engine-credited hand crafting.

The Steam user's profile and any existing observer window are never read or
controlled. The dedicated player's fresh join inventory is discarded when its
controller takes over the existing automation character: no second starter kit
enters the production economy.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from .deterministic_state import _atomic_json


PLAYER_NAME = "FactoryAutomaton"
_PROCESSES: dict[str, subprocess.Popen] = {}


BIND_CRAFTING_PLAYER_LUA = r'''
local name="FactoryAutomaton"
local p=game.get_player(name)
if not p or not p.connected then return {status="running",reason="dedicated_client_connecting",player_name=name} end
if d and (not a or not a.valid) and d.crafting_player_index==p.index and p.character and p.character.valid then
 if d.crafting_actor_unit_number and p.character.unit_number~=d.crafting_actor_unit_number then
  return {status="blocked",reason="dedicated_character_replaced",player_name=name}
 end
 d.actor=p.character
 a=d.actor
end
if not d or not a or not a.valid then return {status="blocked",reason="automation_character_missing",player_name=name} end
if a.player and a.player~=p then return {status="blocked",reason="automation_character_owned_by_another_player",player_name=name} end
local spawned=p.character
local discarded={}
if spawned~=a then
 if spawned and spawned.valid then
  for index=1,spawned.get_max_inventory_index() do
   local inventory=spawned.get_inventory(index)
   if inventory then
    for _,item in pairs(inventory.get_contents()) do discarded[item.name]=(discarded[item.name] or 0)+item.count end
   end
  end
 end
 p.set_controller{type=defines.controllers.character,character=a}
 if p.character~=a then return {status="blocked",reason="dedicated_character_bind_failed",player_name=name} end
 if spawned and spawned.valid and spawned~=a then spawned.destroy() end
end
d.crafting_player_index=p.index
d.crafting_actor_unit_number=a.unit_number
return {status="ready",player_name=p.name,player_index=p.index,connected=p.connected,
 character_unit_number=a.unit_number,discarded_join_inventory=discarded}
'''


def _identity(pid: int) -> int | None:
    """Process creation time prevents a reused PID from identifying our client."""
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        if exited.dwLowDateTime or exited.dwHighDateTime:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _hide_client_windows(pid: int) -> None:
    """Only windows belonging to the exact spawned client PID are hidden."""
    if os.name != "nt":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            user32.ShowWindow(hwnd, 0)
        return True
    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)


def _view_requested(root: Path, state: dict[str, Any]) -> bool:
    try:
        view = json.loads((root / "client-view.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(view, dict) and view.get("visible") is True
            and bool(state.get("creation_time"))
            and view.get("pid") == state.get("pid")
            and view.get("creation_time") == state["creation_time"])


def _show_client_windows(pid: int, creation_time: int) -> int:
    """Restore only game windows owned by this exact client, once on request."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    shown = 0
    def visit(hwnd, _):
        nonlocal shown
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or _identity(pid) != creation_time:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value == "Factorio" or title.value.startswith("Factorio:"):
            user32.ShowWindow(hwnd, 9)
            # Keep the current screen position; enlarge the tiny background view.
            user32.SetWindowPos(hwnd, None, 0, 0, 1600, 900, 0x0002 | 0x0004 | 0x0040)
            user32.SetForegroundWindow(hwnd)
            shown += bool(user32.IsWindowVisible(hwnd))
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return shown


def set_client_visibility(cfg: Any, *, visible: bool = True) -> dict[str, Any]:
    """Watch the existing automation connection without a second Steam login."""
    if os.name != "nt":
        return {"status": "blocked", "reason": "window_control_requires_windows"}
    root = (Path(cfg.runtime_dir) / "agent-client").resolve()
    try:
        state = json.loads((root / "client-process.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "blocked", "reason": "no_owned_client"}
    if not isinstance(state, dict):
        return {"status": "blocked", "reason": "no_owned_client"}
    pid, created = state.get("pid"), state.get("creation_time")
    if not pid or not created or _identity(pid) != created:
        return {"status": "blocked", "reason": "owned_client_not_running"}
    _atomic_json(root / "client-view.json", {"pid": pid, "creation_time": created, "visible": visible})
    if visible:
        shown = _show_client_windows(pid, created)
        if not shown:
            return {"status": "running", "reason": "client_window_not_ready", "pid": pid}
    elif _identity(pid) == created:
        _hide_client_windows(pid)
    return {"status": "ready", "pid": pid, "visible": visible,
            "address": f"{cfg.rcon_host}:{cfg.server_port}"}


def stop_crafting_client(cfg: Any) -> dict[str, Any]:
    """Close only the client whose saved PID and creation time both match."""
    root = (Path(cfg.runtime_dir) / "agent-client").resolve()
    registry = root / "client-process.json"
    if not registry.exists():
        return {"status": "stopped", "reason": "no_owned_client"}
    state = json.loads(registry.read_text(encoding="utf-8"))
    pid = state.get("pid")
    if not pid or not state.get("creation_time") or _identity(pid) != state["creation_time"]:
        return {"status": "stopped", "reason": "owned_client_not_running"}
    # subprocess on Windows uses TerminateProcess for terminate; operate only on
    # a newly opened handle after the creation-time ownership check.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1001, False, pid)
    if not handle:
        return {"status": "blocked", "reason": "owned_client_stop_failed", "pid": pid}
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return {"status": "blocked", "reason": "owned_client_identity_unavailable", "pid": pid}
        identity = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if identity != state["creation_time"]:
            return {"status": "stopped", "reason": "owned_client_not_running"}
        if not kernel.TerminateProcess(handle, 0):
            return {"status": "blocked", "reason": "owned_client_stop_failed", "pid": pid}
    finally:
        kernel.CloseHandle(handle)
    _PROCESSES.pop(str(root), None)
    return {"status": "stopped", "pid": pid}


def prepare_client(cfg: Any) -> tuple[list[str], Path]:
    """Prepare only runtime/agent-client; never copy an authenticated profile."""
    root = (Path(cfg.runtime_dir) / "agent-client").resolve()
    root.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    data.mkdir(exist_ok=True)
    player_data = data / "player-data.json"
    if not player_data.exists():
        player_data.write_text(json.dumps({"service-username": PLAYER_NAME, "service-token": ""}), encoding="utf-8")
    # Steamworks honors this only in this process's working directory. It keeps
    # Steam from relaunching the executable with a different PID/show state.
    # The installed, already licensed Steam binary still initializes Steam.
    (root / "steam_appid.txt").write_text("427520", encoding="ascii")
    mods = root / "mods"
    mods.mkdir(exist_ok=True)
    (mods / "mod-list.json").write_text(json.dumps({"mods": [{"name": name, "enabled": True}
        for name in ("base", "elevated-rails", "recycler", "quality", "space-age")]}), encoding="utf-8")
    config = root / "client-config.ini"
    install = Path(cfg.factorio_exe).parent.parent.parent
    if not config.exists():
        config.write_text("\n".join(["; version=13", "[path]", f"read-data={(install / 'data').as_posix()}",
                                   f"write-data={data.as_posix()}", "", "[general]", "locale=en", "",
                                   "[other]", "check-updates=false", "enable-blueprint-storage-cloud-sync=false",
                                   "", "[graphics]", "full-screen=false", ""]), encoding="utf-8")
    command = [str(cfg.factorio_exe), "--config", str(config), "--mod-directory", str(mods),
               "--disable-migration-window", "--disable-audio", "--force-graphics-preset", "very-low",
               "--window-size", "800x600", "--port", "0", "--mp-connect", f"{cfg.rcon_host}:{cfg.server_port}"]
    return command, root


def ensure_crafting_player(game: Any) -> dict[str, Any]:
    """Start/poll our own client and bind its controller to the conserved actor.

    Nonblocking: call again while status is running. Failed clients are retried
    with a delay, and connection failure is explicit after 120 seconds.
    """
    cfg = game.cfg
    result = game.query(BIND_CRAFTING_PLAYER_LUA)
    root = (Path(cfg.runtime_dir) / "agent-client").resolve()
    registry = root / "client-process.json"
    state = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
    process = _PROCESSES.get(str(root))
    live = process is not None and process.poll() is None
    if not live and state.get("pid") and state.get("creation_time"):
        live = _identity(int(state["pid"])) == state["creation_time"]
    owned = bool(state.get("pid") and state.get("creation_time")
                 and (process is None or process.pid == state["pid"])
                 and _identity(int(state["pid"])) == state["creation_time"])
    if owned and not _view_requested(root, state):
        _hide_client_windows(int(state["pid"]))
    if result.get("status") == "ready":
        return dict(result, pid=state.get("pid"))
    if result.get("status") == "blocked":
        return result
    if live:
        if time.time() - state.get("started_at", time.time()) > 120:
            return {"status": "blocked", "reason": "dedicated_client_connection_timeout", "player_name": PLAYER_NAME,
                    "pid": state["pid"], "log_path": str(root / "data" / "factorio-current.log")}
        return dict(result, pid=state["pid"])
    if time.time() - state.get("started_at", 0) < 10:
        return {"status": "running", "reason": "dedicated_client_restart_backoff", "player_name": PLAYER_NAME}
    command, root = prepare_client(cfg)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    with (root / "client-process.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=str(root), stdout=log, stderr=subprocess.STDOUT,
                                   startupinfo=startupinfo, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    _PROCESSES[str(root)] = process
    state = {"pid": process.pid, "creation_time": _identity(process.pid), "started_at": time.time(), "player_name": PLAYER_NAME}
    registry.write_text(json.dumps(state), encoding="utf-8")
    return {"status": "running", "reason": "dedicated_client_started", **state}
