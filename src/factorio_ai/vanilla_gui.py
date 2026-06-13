from __future__ import annotations

from dataclasses import dataclass
import ctypes
import ctypes.wintypes
import os
from pathlib import Path
import json
import re
import shutil
import struct
import subprocess
import time
from typing import Iterable

from .config import AppConfig


FACTORIO_STEAM_APP_ID = "427520"
OFFICIAL_VANILLA_MODS = ("base", "elevated-rails", "quality", "space-age")
FORBIDDEN_ACHIEVEMENT_ARGS = {
    "--rcon-port",
    "--rcon-password",
    "--start-server",
    "--start-server-load-scenario",
    "--load-scenario",
    "--create",
    "--map2scenario",
    "--scenario2map",
}
ACHIEVEMENT_SAFE_ARGS_WITH_VALUE = {
    "--window-size",
}
ACHIEVEMENT_SAFE_FLAGS: set[str] = set()
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_MINIMIZE = 6
SW_RESTORE = 9
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
PW_RENDERFULLCONTENT = 0x00000002
VK_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "escape": 0x1B,
    "space": 0x20,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "w": 0x57,
    "a": 0x41,
    "s": 0x53,
    "d": 0x44,
    "e": 0x45,
    "q": 0x51,
    "r": 0x52,
    "f": 0x46,
    "m": 0x4D,
    "i": 0x49,
}
HBITMAP = ctypes.wintypes.HANDLE
HGDIOBJ = ctypes.wintypes.HANDLE
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class AchievementPolicyError(ValueError):
    pass


class GuiAutomationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class VanillaWindowSnapshot:
    hwnd: int
    title: str
    rect: WindowRect
    path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "rect": {
                "left": self.rect.left,
                "top": self.rect.top,
                "right": self.rect.right,
                "bottom": self.rect.bottom,
                "width": self.rect.width,
                "height": self.rect.height,
            },
            "path": str(self.path),
        }


@dataclass(frozen=True)
class VanillaProbeReport:
    window_found: bool
    title: str | None
    minimized: bool
    visible_capture: str | None
    minimized_capture: str | None
    minimized_capture_ok: bool
    background_key_posted: bool
    background_input_verified: bool
    can_run_minimized: bool
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "window_found": self.window_found,
            "title": self.title,
            "minimized": self.minimized,
            "visible_capture": self.visible_capture,
            "minimized_capture": self.minimized_capture,
            "minimized_capture_ok": self.minimized_capture_ok,
            "background_key_posted": self.background_key_posted,
            "background_input_verified": self.background_input_verified,
            "can_run_minimized": self.can_run_minimized,
            "notes": self.notes,
        }


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_byte),
        ("rgbGreen", ctypes.c_byte),
        ("rgbRed", ctypes.c_byte),
        ("rgbReserved", ctypes.c_byte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]


def validate_achievement_safe_args(args: Iterable[str]) -> list[str]:
    normalized = [str(arg) for arg in args]
    expecting_value_for: str | None = None
    for index, arg in enumerate(normalized):
        if expecting_value_for is not None:
            if arg.startswith("--"):
                raise AchievementPolicyError(f"{expecting_value_for} requires a value")
            expecting_value_for = None
            continue

        option = arg.split("=", 1)[0]
        if option in FORBIDDEN_ACHIEVEMENT_ARGS:
            raise AchievementPolicyError(f"argument is not achievement-compatible: {option}")
        if index > 0 and normalized[index - 1] in FORBIDDEN_ACHIEVEMENT_ARGS:
            raise AchievementPolicyError(f"argument follows forbidden option: {normalized[index - 1]}")
        if option in ACHIEVEMENT_SAFE_ARGS_WITH_VALUE:
            if "=" not in arg:
                expecting_value_for = option
            continue
        if option in ACHIEVEMENT_SAFE_FLAGS:
            continue
        if arg.startswith("--"):
            raise AchievementPolicyError(f"argument is not explicitly achievement-safe: {option}")
        raise AchievementPolicyError(f"bare argument is not achievement-compatible: {arg}")
    if expecting_value_for is not None:
        raise AchievementPolicyError(f"{expecting_value_for} requires a value")
    return normalized


def launch_vanilla_gui(
    cfg: AppConfig,
    *,
    via_steam: bool = True,
    args: Iterable[str] = (),
    prepare_steam_mod_list: bool = True,
) -> subprocess.Popen[bytes] | None:
    safe_args = validate_achievement_safe_args(args)
    if via_steam:
        if safe_args:
            raise AchievementPolicyError("Steam vanilla launch must not include custom Factorio args")
        if prepare_steam_mod_list:
            prepare_steam_vanilla_mod_list(cfg.runtime_dir)
        os.startfile(f"steam://rungameid/{FACTORIO_STEAM_APP_ID}")  # type: ignore[attr-defined]
        return None

    if not cfg.factorio_exe.exists():
        raise FileNotFoundError(f"Factorio executable not found: {cfg.factorio_exe}")
    mod_dir = prepare_vanilla_mod_directory(cfg.runtime_dir)
    launch_args = [*safe_args, "--mod-directory", str(mod_dir)]
    return subprocess.Popen([str(cfg.factorio_exe), *launch_args], cwd=str(Path.cwd()))


def prepare_vanilla_mod_directory(runtime_dir: Path) -> Path:
    mod_dir = runtime_dir / "vanilla" / "mods"
    mod_dir.mkdir(parents=True, exist_ok=True)
    unsafe_children = [child.name for child in mod_dir.iterdir() if child.name != "mod-list.json"]
    if unsafe_children:
        raise AchievementPolicyError(
            "vanilla mod directory must contain only mod-list.json; "
            f"remove these files before launching: {', '.join(sorted(unsafe_children))}"
        )
    payload = {"mods": [{"name": name, "enabled": True} for name in OFFICIAL_VANILLA_MODS]}
    (mod_dir / "mod-list.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return mod_dir


def factorio_user_mod_list_path(appdata_dir: str | Path | None = None) -> Path:
    root = Path(appdata_dir) if appdata_dir else Path(os.environ.get("APPDATA", "")) / "Factorio"
    if not str(root):
        raise AchievementPolicyError("APPDATA is not set; cannot locate Factorio user mod-list.json")
    return root / "mods" / "mod-list.json"


def official_vanilla_mod_list_payload(installed_mod_names: Iterable[str] = ()) -> dict[str, object]:
    official = set(OFFICIAL_VANILLA_MODS)
    names = sorted(official | {name for name in installed_mod_names if name})
    return {"mods": [{"name": name, "enabled": name in official} for name in names]}


def prepare_steam_vanilla_mod_list(runtime_dir: Path, *, appdata_dir: str | Path | None = None) -> dict[str, object]:
    mod_list_path = factorio_user_mod_list_path(appdata_dir)
    mod_list_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if mod_list_path.exists():
        backup_dir = runtime_dir / "vanilla" / "steam-mod-list-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"mod-list-{timestamp}.json"
        shutil.copy2(mod_list_path, backup_path)
    payload = official_vanilla_mod_list_payload(_installed_user_mod_names(mod_list_path.parent))
    mod_list_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    disabled = [
        item["name"]
        for item in payload["mods"]
        if isinstance(item, dict) and not item.get("enabled")
    ]
    return {
        "modListPath": str(mod_list_path),
        "backupPath": str(backup_path) if backup_path else None,
        "officialMods": list(OFFICIAL_VANILLA_MODS),
        "disabledInstalledModCount": len(disabled),
        "disabledInstalledModsSample": disabled[:10],
    }


def restore_latest_steam_mod_list(runtime_dir: Path, *, appdata_dir: str | Path | None = None) -> dict[str, object]:
    backup_dir = runtime_dir / "vanilla" / "steam-mod-list-backups"
    backups = sorted(backup_dir.glob("mod-list-*.json")) if backup_dir.exists() else []
    if not backups:
        raise FileNotFoundError(f"no Steam mod-list backup found under {backup_dir}")
    latest = backups[-1]
    mod_list_path = factorio_user_mod_list_path(appdata_dir)
    mod_list_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(latest, mod_list_path)
    return {"modListPath": str(mod_list_path), "restoredFrom": str(latest)}


def _installed_user_mod_names(mod_dir: Path) -> list[str]:
    names: set[str] = set()
    if not mod_dir.exists():
        return []
    for child in mod_dir.iterdir():
        if child.name == "mod-list.json":
            continue
        if child.is_file() and child.suffix.lower() == ".zip":
            name = _mod_name_from_archive(child.stem)
            if name:
                names.add(name)
        elif child.is_dir():
            names.add(child.name)
    return sorted(names)


def _mod_name_from_archive(stem: str) -> str:
    name, separator, version = stem.rpartition("_")
    if separator and version[:1].isdigit():
        return name
    return stem


class VanillaGuiDriver:
    """Windows keyboard/mouse executor for the no-mod achievement track."""

    def __init__(self, cfg: AppConfig) -> None:
        if os.name != "nt":
            raise GuiAutomationError("vanilla GUI driver currently supports Windows only")
        self.cfg = cfg
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self._configure_ctypes()

    def launch(self) -> None:
        launch_vanilla_gui(self.cfg, via_steam=True)

    def activate_factorio(self, timeout_seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            window = self.find_factorio_window()
            if window:
                hwnd, _title = window
                self.user32.ShowWindow(hwnd, SW_RESTORE)
                self.user32.SetForegroundWindow(hwnd)
                return True
            time.sleep(0.5)
        return False

    def factorio_window_state(self) -> dict[str, object]:
        window = self.find_factorio_window()
        if window is None:
            return {"found": False}
        hwnd, title = window
        rect = self._window_rect(hwnd)
        return {
            "found": True,
            "hwnd": hwnd,
            "title": title,
            "minimized": self.is_minimized(hwnd),
            "rect": _rect_dict(rect),
        }

    def factorio_windows(self) -> list[tuple[int, str]]:
        windows: list[tuple[int, str]] = []
        for hwnd, title in self._top_level_windows():
            normalized = title.strip()
            process_path = self._window_process_path(hwnd)
            if is_factorio_game_window_title(normalized) and is_factorio_process_path(process_path):
                windows.append((hwnd, normalized))
        return windows

    def factorio_window_diagnostics(self) -> dict[str, object]:
        accepted: list[dict[str, object]] = []
        factorio_process_windows: list[dict[str, object]] = []
        rejected_factorio_title_windows: list[dict[str, object]] = []
        for hwnd, title in self._top_level_windows():
            normalized = title.strip()
            process_path = self._window_process_path(hwnd)
            is_factorio_exe = is_factorio_process_path(process_path)
            is_factorio_title = is_factorio_game_window_title(normalized)
            record = self._window_record(hwnd, normalized, process_path)
            if is_factorio_exe:
                factorio_process_windows.append(record)
            if is_factorio_title and is_factorio_exe:
                accepted.append(record)
            elif is_factorio_title:
                rejected_factorio_title_windows.append(record)
        return {
            "acceptedGameWindows": accepted,
            "factorioProcessWindows": factorio_process_windows,
            "rejectedFactorioTitleWindows": rejected_factorio_title_windows,
        }

    def find_factorio_window(self) -> tuple[int, str] | None:
        windows = self.factorio_windows()
        if not windows:
            return None
        windows.sort(key=lambda item: 0 if "Space Age" in item[1] or item[1] == "Factorio" else 1)
        return windows[0]

    def click(self, x: int, y: int) -> None:
        self.user32.SetCursorPos(int(x), int(y))
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def click_window_ratio(self, x_ratio: float, y_ratio: float) -> None:
        window = self.find_factorio_window()
        if window is None:
            raise GuiAutomationError("Factorio window was not found")
        hwnd, _title = window
        rect = self._window_rect(hwnd)
        self.click(rect.left + int(rect.width * x_ratio), rect.top + int(rect.height * y_ratio))

    def start_space_age_freeplay_from_main_menu(self, *, skip_intro: bool = True) -> dict[str, object]:
        if not self.activate_factorio(timeout_seconds=5.0):
            raise GuiAutomationError("Factorio window was not found")
        steps = []
        sequence = [
            ("single_player", 0.50, 0.405, 1.0),
            ("new_game", 0.50, 0.470, 1.0),
            ("freeplay_next", 0.80, 0.875, 1.0),
            ("map_generator_play", 0.61, 0.975, 10.0),
        ]
        for name, x_ratio, y_ratio, delay in sequence:
            self.click_window_ratio(x_ratio, y_ratio)
            steps.append(name)
            time.sleep(delay)
        if skip_intro:
            self.press_key("tab", duration_seconds=0.2)
            steps.append("skip_intro_tab")
            time.sleep(2.0)
        return {"steps": steps, "skipIntro": skip_intro}

    def press(self, virtual_key: int, duration_seconds: float = 0.05) -> None:
        self._send_key(virtual_key, key_up=False)
        time.sleep(duration_seconds)
        self._send_key(virtual_key, key_up=True)

    def press_key(self, key: str, duration_seconds: float = 0.05) -> None:
        self.press(_virtual_key(key), duration_seconds=duration_seconds)

    def hold_key(self, key: str, duration_seconds: float) -> None:
        virtual_key = _virtual_key(key)
        self._send_key(virtual_key, key_up=False)
        time.sleep(max(0.0, duration_seconds))
        self._send_key(virtual_key, key_up=True)

    def post_key_to_factorio(self, key: str, duration_seconds: float = 0.05) -> bool:
        window = self.find_factorio_window()
        if window is None:
            return False
        hwnd, _title = window
        virtual_key = _virtual_key(key)
        down_ok = bool(self.user32.PostMessageW(hwnd, WM_KEYDOWN, virtual_key, 0))
        time.sleep(max(0.0, duration_seconds))
        up_ok = bool(self.user32.PostMessageW(hwnd, WM_KEYUP, virtual_key, 0))
        return down_ok and up_ok

    def minimize_factorio(self) -> bool:
        window = self.find_factorio_window()
        if window is None:
            return False
        return bool(self.user32.ShowWindow(window[0], SW_MINIMIZE))

    def restore_factorio(self) -> bool:
        window = self.find_factorio_window()
        if window is None:
            return False
        return bool(self.user32.ShowWindow(window[0], SW_RESTORE))

    def is_minimized(self, hwnd: int) -> bool:
        return bool(self.user32.IsIconic(hwnd))

    def capture_factorio_window(
        self,
        output_path: str | Path | None = None,
        *,
        method: str = "auto",
    ) -> VanillaWindowSnapshot:
        window = self.find_factorio_window()
        if window is None:
            raise GuiAutomationError("Factorio window was not found")
        hwnd, title = window
        rect = self._window_rect(hwnd)
        if rect.width <= 0 or rect.height <= 0:
            raise GuiAutomationError(f"Factorio window has invalid dimensions: {rect}")
        output = Path(output_path) if output_path else _default_screenshot_path(self.cfg.runtime_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        capture_method = _capture_method(method, minimized=self.is_minimized(hwnd))
        if capture_method == "window":
            pixels = self._capture_window_bgra(hwnd, rect)
        else:
            pixels = self._capture_screen_rect_bgra(rect)
        output.write_bytes(encode_bgra_bmp(rect.width, rect.height, pixels))
        return VanillaWindowSnapshot(hwnd=hwnd, title=title, rect=rect, path=output)

    def probe_background_capabilities(
        self,
        output_dir: str | Path | None = None,
        *,
        minimize_check: bool = False,
        background_key: str | None = None,
    ) -> VanillaProbeReport:
        window = self.find_factorio_window()
        if window is None:
            return VanillaProbeReport(
                window_found=False,
                title=None,
                minimized=False,
                visible_capture=None,
                minimized_capture=None,
                minimized_capture_ok=False,
                background_key_posted=False,
                background_input_verified=False,
                can_run_minimized=False,
                notes=["Factorio window was not found"],
            )

        hwnd, title = window
        was_minimized = self.is_minimized(hwnd)
        output_root = Path(output_dir) if output_dir else self.cfg.runtime_dir / "vanilla" / "probe"
        output_root.mkdir(parents=True, exist_ok=True)
        notes = [
            "Foreground SendInput remains the reliable achievement-compatible executor.",
            "PostMessage can target a background window, but this probe cannot prove Factorio consumed it as game input.",
            "Minimized DirectX windows may return a black or stale frame even if the capture API succeeds.",
        ]

        visible_capture = None
        minimized_capture = None
        minimized_capture_ok = False
        if not was_minimized:
            try:
                visible = self.capture_factorio_window(output_root / "visible.bmp", method="screen")
                visible_capture = str(visible.path)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"visible capture failed: {type(exc).__name__}: {exc}")

        background_key_posted = False
        if background_key:
            try:
                background_key_posted = self.post_key_to_factorio(background_key)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"background key post failed: {type(exc).__name__}: {exc}")

        if minimize_check:
            try:
                self.user32.ShowWindow(hwnd, SW_MINIMIZE)
                time.sleep(0.5)
                minimized = self.capture_factorio_window(output_root / "minimized.bmp", method="window")
                minimized_capture = str(minimized.path)
                minimized_capture_ok = minimized.rect.width >= 320 and minimized.rect.height >= 240
                if not minimized_capture_ok:
                    notes.append(
                        f"minimized capture is too small for gameplay recognition: {minimized.rect.width}x{minimized.rect.height}"
                    )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"minimized capture failed: {type(exc).__name__}: {exc}")
            finally:
                if not was_minimized:
                    self.user32.ShowWindow(hwnd, SW_RESTORE)

        can_run_minimized = minimized_capture_ok and False
        if minimized_capture_ok:
            notes.append(
                "Minimized capture API completed, but semantic screen recognition and background movement still need a live-game verification."
            )
        else:
            notes.append("Do not minimize Factorio for vanilla automation until a live-game minimized probe passes.")

        return VanillaProbeReport(
            window_found=True,
            title=title,
            minimized=was_minimized,
            visible_capture=visible_capture,
            minimized_capture=minimized_capture,
            minimized_capture_ok=minimized_capture_ok,
            background_key_posted=background_key_posted,
            background_input_verified=False,
            can_run_minimized=can_run_minimized,
            notes=notes,
        )

    def click_steam_continue_prompt(self, timeout_seconds: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            hwnd = self._find_steam_prompt()
            if hwnd:
                rect = self._window_rect(hwnd)
                self.user32.SetForegroundWindow(hwnd)
                self.click(rect.left + int(rect.width * 0.66), rect.top + int(rect.height * 0.88))
                return True
            time.sleep(0.25)
        return False

    def _find_steam_prompt(self) -> int | None:
        candidates = []
        for hwnd, title in self._top_level_windows():
            lowered = title.lower()
            if "사용자 지정 인수" in title or "custom arguments" in lowered or title == "Steam":
                rect = self._window_rect(hwnd)
                if 350 <= rect.width <= 900 and 180 <= rect.height <= 500:
                    candidates.append(hwnd)
        return candidates[0] if candidates else None

    def _configure_ctypes(self) -> None:
        self.user32.GetDC.restype = ctypes.wintypes.HDC
        self.user32.GetDC.argtypes = [ctypes.wintypes.HWND]
        self.user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
        self.user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
        self.user32.PostMessageW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
        self.user32.PrintWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC, ctypes.wintypes.UINT]
        self.user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        self.user32.MapVirtualKeyW.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = ctypes.wintypes.UINT
        self.user32.SendInput.argtypes = [ctypes.wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = ctypes.wintypes.UINT
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
        self.kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        self.gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC
        self.gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]
        self.gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]
        self.gdi32.DeleteObject.argtypes = [HGDIOBJ]
        self.gdi32.CreateCompatibleBitmap.restype = HBITMAP
        self.gdi32.CreateCompatibleBitmap.argtypes = [ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi32.SelectObject.restype = HGDIOBJ
        self.gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, HGDIOBJ]
        self.gdi32.BitBlt.argtypes = [
            ctypes.wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.DWORD,
        ]
        self.gdi32.GetDIBits.argtypes = [
            ctypes.wintypes.HDC,
            HBITMAP,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.UINT,
        ]

    def _send_key(self, virtual_key: int, *, key_up: bool) -> None:
        scan_code = self.user32.MapVirtualKeyW(virtual_key, MAPVK_VK_TO_VSC)
        if not scan_code:
            self.user32.keybd_event(virtual_key, 0, KEYEVENTF_KEYUP if key_up else 0, 0)
            return
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        input_event = INPUT(
            type=1,
            union=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=0,
                    wScan=scan_code,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        sent = self.user32.SendInput(1, ctypes.byref(input_event), ctypes.sizeof(INPUT))
        if sent != 1:
            self.user32.keybd_event(virtual_key, 0, KEYEVENTF_KEYUP if key_up else 0, 0)

    def _window_process_path(self, hwnd: int) -> str | None:
        pid = ctypes.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return None
        try:
            size = ctypes.wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            self.kernel32.CloseHandle(handle)

    def _window_record(self, hwnd: int, title: str, process_path: str | None) -> dict[str, object]:
        try:
            rect = self._window_rect(hwnd)
            rect_payload = _rect_dict(rect)
        except GuiAutomationError as exc:
            rect_payload = {"error": str(exc)}
        return {
            "hwnd": hwnd,
            "title": title,
            "processPath": process_path,
            "rect": rect_payload,
        }

    def _find_window_containing(self, text: str) -> int | None:
        needle = text.lower()
        for hwnd, title in self._top_level_windows():
            if needle in title.lower():
                return hwnd
        return None

    def _capture_screen_rect_bgra(self, rect: WindowRect) -> bytes:
        width = rect.width
        height = rect.height
        screen_dc = self.user32.GetDC(0)
        if not screen_dc:
            raise GuiAutomationError("GetDC failed")
        memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            self.user32.ReleaseDC(0, screen_dc)
            raise GuiAutomationError("CreateCompatibleDC failed")
        bitmap = self.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(0, screen_dc)
            raise GuiAutomationError("CreateCompatibleBitmap failed")

        old_object = self.gdi32.SelectObject(memory_dc, bitmap)
        try:
            copied = self.gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, SRCCOPY)
            if not copied:
                raise GuiAutomationError("BitBlt failed")
            return self._bitmap_pixels(memory_dc, bitmap, width, height)
        finally:
            if old_object:
                self.gdi32.SelectObject(memory_dc, old_object)
            self.gdi32.DeleteObject(bitmap)
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(0, screen_dc)

    def _capture_window_bgra(self, hwnd: int, rect: WindowRect) -> bytes:
        width = rect.width
        height = rect.height
        screen_dc = self.user32.GetDC(0)
        if not screen_dc:
            raise GuiAutomationError("GetDC failed")
        memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            self.user32.ReleaseDC(0, screen_dc)
            raise GuiAutomationError("CreateCompatibleDC failed")
        bitmap = self.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(0, screen_dc)
            raise GuiAutomationError("CreateCompatibleBitmap failed")

        old_object = self.gdi32.SelectObject(memory_dc, bitmap)
        try:
            printed = self.user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
            if not printed:
                raise GuiAutomationError("PrintWindow failed")
            return self._bitmap_pixels(memory_dc, bitmap, width, height)
        finally:
            if old_object:
                self.gdi32.SelectObject(memory_dc, old_object)
            self.gdi32.DeleteObject(bitmap)
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(0, screen_dc)

    def _bitmap_pixels(self, memory_dc: int, bitmap: int, width: int, height: int) -> bytes:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = self.gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), DIB_RGB_COLORS)
        if lines != height:
            raise GuiAutomationError(f"GetDIBits captured {lines} lines instead of {height}")
        return buffer.raw

    def _top_level_windows(self) -> list[tuple[int, str]]:
        windows: list[tuple[int, str]] = []

        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, length + 1)
            windows.append((int(hwnd), buffer.value))
            return True

        self.user32.EnumWindows(enum_proc_type(callback), 0)
        return windows

    def _window_rect(self, hwnd: int) -> WindowRect:
        rect = ctypes.wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise GuiAutomationError("GetWindowRect failed")
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)


def _virtual_key(key: str) -> int:
    normalized = key.strip().lower()
    if normalized not in VK_KEYS:
        raise GuiAutomationError(f"unsupported vanilla key: {key}")
    return VK_KEYS[normalized]


def is_factorio_game_window_title(title: str) -> bool:
    normalized = title.strip()
    if normalized == "Factorio":
        return True
    if normalized.startswith("Factorio:"):
        return True
    return re.match(r"^Factorio \d+\.\d+\.\d+(?:\b|$)", normalized) is not None


def is_factorio_process_path(path: str | None) -> bool:
    if not path:
        return False
    normalized = path.replace("/", "\\").lower()
    return normalized.endswith("\\factorio.exe")


def _rect_dict(rect: WindowRect) -> dict[str, int]:
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.width,
        "height": rect.height,
    }


def _capture_method(method: str, *, minimized: bool) -> str:
    normalized = method.strip().lower()
    if normalized == "auto":
        return "window"
    if normalized in {"screen", "window"}:
        return normalized
    raise GuiAutomationError(f"unsupported capture method: {method}")


def _default_screenshot_path(runtime_dir: Path) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return runtime_dir / "vanilla" / "screenshots" / f"factorio-{timestamp}.bmp"


def encode_bgra_bmp(width: int, height: int, bgra_pixels: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("BMP dimensions must be positive")
    expected = width * height * 4
    if len(bgra_pixels) != expected:
        raise ValueError(f"expected {expected} BGRA bytes, got {len(bgra_pixels)}")
    file_header_size = 14
    dib_header_size = 40
    pixel_offset = file_header_size + dib_header_size
    file_size = pixel_offset + len(bgra_pixels)
    file_header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, pixel_offset)
    dib_header = struct.pack(
        "<IiiHHIIiiII",
        dib_header_size,
        width,
        height,
        1,
        32,
        BI_RGB,
        len(bgra_pixels),
        0,
        0,
        0,
        0,
    )
    return file_header + dib_header + bgra_pixels
