"""Keep game services independent of the console that launched them."""

import os
import subprocess
import json
import sys
from pathlib import Path


def independent_process_options():
    options = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return options


class DetachedChild:
    """Keep a real process handle even though the console-free broker exits."""

    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes

        self.pid = pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not self.handle:
            raise RuntimeError("detached process receipt has no live handle")

    def poll(self):
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise OSError("cannot inspect detached process handle")
        return None if code.value == 259 else code.value

    def __del__(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)


def spawn_console_independent(command, *, log_path, cwd=None, env=None, hide_window=True):
    """Factorio can attach to its parent console; give it a console-free parent.

    Command arguments (including RCON credentials) cross a private stdin pipe,
    never a temporary file or the broker command line. Uncertain launches are
    left for inspection instead of starting another process.
    """
    if os.name != "nt":
        with Path(log_path).open("ab") as output:
            return subprocess.Popen(
                command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, **independent_process_options()
            )
    payload = dict(
        command=command,
        log_path=str(Path(log_path).resolve()),
        cwd=str(cwd) if cwd is not None else None,
        hide_window=hide_window,
    )
    options = independent_process_options()
    options["stdin"] = subprocess.PIPE
    broker = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--broker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        **options,
    )
    try:
        output, _ = broker.communicate(json.dumps(payload), timeout=15)
        receipt = json.loads(output)
        if broker.returncode != 0 or type(receipt.get("pid")) is not int or receipt["pid"] < 1:
            raise ValueError("invalid launch receipt")
        return DetachedChild(receipt["pid"])
    except (subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("independent launch outcome unknown; inspect existing processes before retry") from exc


def _broker():
    payload = json.loads(sys.stdin.read())
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0 if payload["hide_window"] else 1
    with Path(payload["log_path"]).open("ab") as output:
        child = subprocess.Popen(
            payload["command"],
            cwd=payload["cwd"],
            stdout=output,
            stderr=subprocess.STDOUT,
            startupinfo=startupinfo,
            **independent_process_options(),
        )
    print(json.dumps({"pid": child.pid}), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] != ["--broker"]:
        raise SystemExit(2)
    _broker()
