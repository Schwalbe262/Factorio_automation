"""Run N independent hidden-window maps in parallel and read them in one report.

Usage: python -B tools/fleet.py start --count N [--seeds a,b,c | random] [--minutes M] [--name PREFIX]
       python -B tools/fleet.py status [--name NAME] [--json]
       python -B tools/fleet.py report [--name NAME] [--window 0-30] [--json]
       python -B tools/fleet.py stop [--name NAME]
       python -B tools/fleet.py cost

Each run is a new folder ``runtime/v2/<name>-<seed>`` with its own seed and its
own server/RCON port pair (allocated above the interactive range). By default
(``--client always``) a run is the three processes of ``lifecycle start
--no-gui``: a dedicated server, the per-run automation client with its window
hidden and a ``--planner none`` supervisor. ``--client none`` makes a
client-free TEST run (server + supervisor only, labelled ``test_mode:
client_free``, the lab trigger script-credited after the real craft; a
player-less actor charts nothing, so it is equivalent only up to the
electric-mining-drill research, docs/v2/HEADLESS.md); ``until_lab_trigger``
closes the client once the lab trigger fired. ``runtime/active-runtime.json`` and every existing runtime are never
touched; the manifest is ``runtime/fleets/<name>.json``. ``stop`` uses the
lifecycle safe stop per run (supervisor ACK, world paused and saved, server
``/quit``, own client closed); nothing is deleted.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from dataclasses import replace
import json
import os
from pathlib import Path
import random
import re
import socket
import statistics
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))
from factorio_agent.core.deterministic_character import _identity  # noqa: E402
from factorio_agent.core.deterministic_state import RunLock, _atomic_json  # noqa: E402
from factorio_agent import actor_client  # noqa: E402

# Fleet ports start above the interactive lifecycle range (34261+/27061+, 100 slots).
UDP_BASE, TCP_BASE, PORT_SPAN = 34400, 27400, 400
# Measured on this machine (2026-11-13, see docs/v2/README.md); replaced by
# runtime/fleets/cost.json once `status` has measured live fleet processes.
DEFAULT_RUN_MB = 1500
DEFAULT_RUN_CORES = 1.5
RESERVE_MB = 6144
RESERVE_CORES = 4
WATCH_SECONDS = 30
STEADY_SECONDS, SAMPLE_SECONDS = 120, 3
TICKS_PER_MINUTE = 3600
ACTIVE = ("pending", "running", "waiting")

# Report columns: header -> roadmap step id (progression.ROADMAP).
COLUMNS = (
    ("power", "power"),
    ("auto", "starter_research_automation"),
    ("emd", "starter_research_electric_mining_drill"),
    ("logi", "bootstrap_logistics"),
    ("iron", "mining_outpost_iron"),
    ("copr", "mining_outpost_copper"),
    ("coal", "mining_outpost_coal"),
    ("smelt", "smelting_district"),
    ("bus", "main_bus"),
    ("red", "automation_science"),
)


# ---------------------------------------------------------------- pure parts


def parse_seeds(text, count, rng=None):
    """``random`` (or empty) -> ``count`` distinct unsigned 32-bit seeds; else a comma list."""
    rng = rng or random.SystemRandom()
    if not text or text == "random":
        if not count or count < 1:
            raise ValueError("--count N is required with random seeds")
        seeds = []
        while len(seeds) < count:
            seed = rng.getrandbits(32)
            if seed not in seeds:
                seeds.append(seed)
        return seeds
    seeds = [int(part) for part in text.split(",") if part.strip()]
    if any(not 0 <= s <= 2**32 - 1 for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be distinct unsigned 32-bit integers")
    if count and count != len(seeds):
        raise ValueError("--count differs from the number of --seeds")
    return seeds


def port_free(udp, tcp):
    """An exclusive local bind of both ports proves they are free now."""
    try:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as one,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as two,
        ):
            if os.name == "nt":
                one.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                two.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            one.bind(("127.0.0.1", udp))
            two.bind(("127.0.0.1", tcp))
        return True
    except OSError:
        return False


def allocate_ports(count, used, probe=port_free, udp_base=UDP_BASE, tcp_base=TCP_BASE, span=PORT_SPAN):
    """``count`` (udp, tcp) pairs that no runtime registered and that bind now."""
    pairs = []
    for offset in range(span):
        udp, tcp = udp_base + offset, tcp_base + offset
        if udp in used or tcp in used or not probe(udp, tcp):
            continue
        pairs.append((udp, tcp))
        if len(pairs) == count:
            return pairs
    raise RuntimeError(f"only {len(pairs)} free fleet port pairs in {udp_base}+{span}; {count} needed")


def registered_ports(base):
    """Every port a runtime, a fleet manifest or the interactive pointer already claims."""
    used = set()
    for path in [*Path(base).glob("v2/*/run.json"), Path(base) / "active-runtime.json"]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        used.update(value[k] for k in ("server_port", "rcon_port") if type(value.get(k)) is int)
    for path in Path(base).glob("fleets/*.json"):
        try:
            runs = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, ValueError, AttributeError):
            continue
        for run in runs:
            used.update(run[k] for k in ("server_port", "rcon_port") if type(run.get(k)) is int)
    return used


def run_cost(first, second, seconds):
    """One run's memory (MB, second sample) and cores (CPU seconds over the sample interval)."""
    parts = [dict(mb=b["mb"], cores=round(max(0.0, b["cpu"] - a["cpu"]) / seconds, 2)) for a, b in zip(first, second)]
    return dict(mb=sum(p["mb"] for p in parts), cores=round(sum(p["cores"] for p in parts), 2), parts=parts)


def capacity(free_mb, cores, per_run_mb=DEFAULT_RUN_MB, per_run_cores=DEFAULT_RUN_CORES):
    """How many runs fit next to what already runs, keeping a reserve for the desktop."""
    by_ram = int(max(0, free_mb - RESERVE_MB) // per_run_mb)
    by_cpu = int(max(0, cores - RESERVE_CORES) // per_run_cores)
    return min(by_ram, by_cpu), dict(by_ram=by_ram, by_cpu=by_cpu)


def normalize_reason(reason, limit=64):
    """Group reasons that differ only in numbers, coordinates or keys."""
    text = re.sub(r"-?\d+(\.\d+)?", "#", str(reason or "?"))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def window(text):
    lo, _, hi = str(text).partition("-")
    lo, hi = float(lo), float(hi)
    if not 0 <= lo < hi:
        raise ValueError("--window LO-HI in game minutes, LO < HI")
    return lo, hi


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def tick_of(state, jobs):
    """Last observed game tick from loaded controller state and jobs: the state's, else the newest job tick."""
    tick = state.get("last_observed_tick")
    if type(tick) is int:
        return tick
    ticks = [j.get("last_tick") for j in jobs.values() if type(j.get("last_tick")) is int]
    return max(ticks) if ticks else None


def run_tick(root):
    """Last observed game tick: controller state, else the newest job tick."""
    state = _read(Path(root) / "logs" / "controller-state.json", {}) or {}
    if type(state.get("last_observed_tick")) is int:
        return state["last_observed_tick"]
    return tick_of(state, (_read(Path(root) / "jobs.json", {}) or {}).get("jobs", {}))


def quick_summary(root):
    """Cheap per-run state for `status`: jobs.json and controller state only."""
    root = Path(root)
    jobs = (_read(root / "jobs.json", {}) or {}).get("jobs", {})
    state = _read(root / "logs" / "controller-state.json", {}) or {}
    return job_summary(jobs, state)


def job_summary(jobs, state):
    """`quick_summary` from an already loaded ``jobs`` mapping and controller state (the dashboard's cache)."""
    done = [j for j in jobs.values() if j.get("status") == "succeeded" and type(j.get("last_tick")) is int]
    last = max(done, key=lambda j: j["last_tick"]) if done else None
    tick = tick_of(state, jobs)
    return dict(
        game_minutes=round(tick / TICKS_PER_MINUTE, 1) if tick is not None else None,
        last_milestone=last.get("capability") if last else None,
        active=sum(j.get("status") in ACTIVE for j in jobs.values()),
        blocked=sum(j.get("status") == "blocked" for j in jobs.values()),
        succeeded=len(done),
        controller=state.get("status"),
    )


def milestones(jobs, ends, roadmap, same):
    """Earliest succeeded game minute per roadmap step id, and the furthest step reached.

    ``jobs``: key -> job (capability, parameters); ``ends``: key -> (tick, status, reason)."""
    reached = {}
    for key, job in jobs.items():
        end = ends.get(key)
        if not end or end[1] != "succeeded" or type(end[0]) is not int:
            continue
        for step in roadmap:
            if same(job, step):
                minute = round(end[0] / TICKS_PER_MINUTE, 1)
                reached[step.id] = min(minute, reached.get(step.id, minute))
    furthest = None
    for index, step in enumerate(roadmap):
        if step.id in reached:
            furthest = (index, step.id, reached[step.id])
    return reached, furthest


def blocked_rows(jobs):
    """(normalized reason, capability) of every blocked or failed job."""
    return [
        (normalize_reason(j.get("reason")), j.get("capability"))
        for j in jobs.values()
        if j.get("status") in ("blocked", "failed")
    ]


def stats(values):
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return None
    return dict(min=min(values), median=round(statistics.median(values), 1), max=max(values), n=len(values))


def aggregate(runs):
    """Column stats, blocked-reason and error histograms over per-run summaries."""
    columns = {head: stats(r["milestones"].get(step) for r in runs) for head, step in COLUMNS}
    blocked = {}
    for r in runs:
        for reason, capability in r.get("blocked_reasons", []):
            row = blocked.setdefault(reason, dict(count=0, seeds=[], capability=capability))
            row["count"] += 1
            if r["seed"] not in row["seeds"]:
                row["seeds"].append(r["seed"])
    errors = {}
    for r in runs:
        for reason, count in (r.get("controller_errors") or {}).items():
            row = errors.setdefault(normalize_reason(reason, 80), dict(count=0, seeds=[]))
            row["count"] += count
            if r["seed"] not in row["seeds"]:
                row["seeds"].append(r["seed"])
    order = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1]["count"]))  # noqa: E731
    return dict(
        columns=columns,
        game_minutes=stats(r.get("game_minutes") for r in runs),
        lab_idle_seconds=stats(r.get("lab_idle_seconds") for r in runs),
        actor_critical_seconds=stats(r.get("actor_critical_seconds") for r in runs),
        actor_slack_seconds=stats(r.get("actor_slack_seconds") for r in runs),
        blocked_reasons=order(blocked),
        controller_errors=order(errors),
    )


def git_commit(code) -> str | None:
    """Short hash of the code a fleet runs (``+dirty`` with uncommitted tracked changes), or None."""
    try:
        run = dict(cwd=str(code), capture_output=True, text=True, timeout=10)
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], **run)
        if head.returncode != 0 or not head.stdout.strip():
            return None
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], **run)
        return head.stdout.strip() + ("+dirty" if dirty.returncode == 0 and dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def _cell(value, width):
    text = "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))
    return text.rjust(width)


def format_report(fleet, runs, agg, *, window_text, top=8):
    """The compact table: one header, one line per run, 3 stat lines, histograms."""
    heads = [h for h, _ in COLUMNS]
    lines = [
        f"fleet {fleet.get('name')} commit {fleet.get('commit') or '?'}: {len(runs)} runs,"
        f" target {fleet.get('target_minutes') or '-'} game min,"
        f" critical-path window {window_text} min; milestone cells = game minute the step succeeded"
    ]
    tests = [r for r in runs if r.get("test_mode")]
    if tests:
        lines[0] += f" | TEST MODE client_free in {len(tests)} runs (script-credited hand-craft triggers; not a proof)"
    lines.append(
        "seed".ljust(11)
        + " up "
        + " cli "
        + " gmin"
        + "".join(h.rjust(6) for h in heads)
        + "  lab_idle crit/slack_s  furthest"
    )
    short = dict(live="on", none="none", detached="det", down="-")
    for r in runs:
        far = r.get("furthest")
        lines.append(
            str(r["seed"]).ljust(11)
            + (" ok " if r.get("alive") else " -- ")
            + short.get(r.get("client"), "-").rjust(4)
            + " "
            + _cell(r.get("game_minutes"), 5)
            + "".join(_cell(r["milestones"].get(step), 6) for _, step in COLUMNS)
            + _cell(r.get("lab_idle_seconds"), 10)
            + f" {_cell(r.get('actor_critical_seconds'), 5).strip()}/{_cell(r.get('actor_slack_seconds'), 5).strip()}".ljust(
                13
            )
            + (f" {far['step']}@{far['minute']}" if far else " -")
        )
    for key in ("min", "median", "max"):
        cells = [(agg["columns"][h] or {}).get(key) for h in heads]
        lab = (agg["lab_idle_seconds"] or {}).get(key)
        gmin = (agg["game_minutes"] or {}).get(key)
        lines.append(key.ljust(20) + _cell(gmin, 5) + "".join(_cell(c, 6) for c in cells) + _cell(lab, 10))
    reached = [(agg["columns"][h] or {}).get("n", 0) for h in heads]
    lines.append("reached".ljust(25) + "".join(_cell(n, 6) for n in reached))
    lines.append(f"blocked/failed jobs now (reason -> count -> example seed), top {top}:")
    rows = list(agg["blocked_reasons"].items())[:top]
    lines += [f"  {v['count']:3d}  {reason}  [{v['capability']}] e.g. {v['seeds'][0]}" for reason, v in rows] or [
        "  none"
    ]
    lines.append("controller errors (count -> example seed), top 5:")
    rows = list(agg["controller_errors"].items())[:5]
    lines += [f"  {v['count']:3d}  {reason}  e.g. {v['seeds'][0]}" for reason, v in rows] or ["  none"]
    if tests:
        credits = [
            f"{r['seed']}:"
            + ",".join(f"{c.get('technology')}@{c.get('tick')}" for c in r.get("script_credited_triggers") or [])
            for r in tests
        ]
        lines.append("test-mode credits (seed:technology@tick): " + " ".join(credits)[:400])
    failed = [r for r in runs if r.get("launch_error")]
    for r in failed[:4]:
        lines.append(f"launch failed {r['seed']}: {r['launch_error'][:100]}")
    return lines


# ---------------------------------------------------------------- processes


def alive(record):
    return bool(
        isinstance(record, dict)
        and type(record.get("pid")) is int
        and record.get("creation_time")
        and _identity(record["pid"]) == record["creation_time"]
    )


def run_records(root):
    """The run's own process registries (the lifecycle's source of truth)."""
    root = Path(root)
    client = _read(root / "agent-client" / "client-process.json", {}) or {}
    if actor_client.is_closed_client(root, client):
        client = {}  # closed on purpose by the recorded detach
    return dict(
        server=_read(root / "server.json", {}) or {},
        client=client,
        supervisor=_read(root / "supervisor-process.json", {}) or {},
    )


def client_column(root, records):
    """'live', 'none' (client-free test run), 'detached' (closed by the transition) or 'down'."""
    return actor_client.client_state(root, alive(records.get("client")))


def run_alive(root, records):
    """Every expected process is alive; a client-free or detached run expects no client."""
    if not any(records.values()):
        return False
    needed = [k for k in records if k != "client" or not actor_client.client_free(root)]
    return all(alive(records.get(k)) for k in needed)


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_cost(record):
    """Working set MB, CPU seconds and age, for an exact pid + creation time."""
    if os.name != "nt" or not alive(record):
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x1000 | 0x0400 | 0x0010, False, record["pid"])
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        value = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime  # noqa: E731
        if value(times[0]) != record["creation_time"]:
            return None
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        now = time.time() * 10**7 + 116444736000000000  # FILETIME now
        elapsed = max(1.0, (now - value(times[0])) / 10**7)
        cpu = (value(times[2]) + value(times[3])) / 10**7
        return dict(mb=round(counters.WorkingSetSize / 2**20), cpu=cpu, seconds=round(elapsed))
    finally:
        kernel.CloseHandle(handle)


class _Memory(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def machine():
    """Free/total physical MB and logical processors."""
    cores = os.cpu_count() or 1
    if os.name != "nt":
        return dict(free_mb=0, total_mb=0, cores=cores)
    memory = _Memory()
    memory.dwLength = ctypes.sizeof(_Memory)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    return dict(free_mb=memory.ullAvailPhys // 2**20, total_mb=memory.ullTotalPhys // 2**20, cores=cores)


# ---------------------------------------------------------------- fleet


class Fleet:
    def __init__(self, base=None, code=REPO):
        self.code = Path(code).resolve()
        self.base = Path(base or self.code / "runtime").resolve()
        self.dir = self.base / "fleets"
        self._lock = threading.Lock()
        # "none" = rules in code only; "luna" = the low-tier model answers incidents
        # (needed only to compare the two on the same seeds).
        self.planner = "none"
        # actor_client mode of every run: "none" = client-free test run (only the
        # server and the supervisor; labelled test_mode client_free), "always" =
        # a hidden client for the whole run, "until_lab_trigger" = client until
        # the lab trigger fired (docs/v2/HEADLESS.md).
        self.client = "always"

    # manifest ----------------------------------------------------------
    def manifest_path(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", name or ""):
            raise ValueError("fleet name: letters, digits, '-', '_', '.' (max 41)")
        return self.dir / (name + ".json")

    def latest(self):
        paths = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime) if self.dir.exists() else []
        paths = [p for p in paths if p.name != "cost.json"]
        if not paths:
            raise FileNotFoundError("no fleet manifest in " + str(self.dir))
        return paths[-1].stem

    def load(self, name=None):
        name = name or self.latest()
        value = _read(self.manifest_path(name))
        if not isinstance(value, dict) or value.get("kind") != "factorio-fleet-v1":
            raise FileNotFoundError("no fleet manifest " + name)
        return value

    def update(self, name, change):
        """Read-modify-write the manifest under its lock file (watcher and CLI share it)."""
        lock = RunLock(self.dir / (name + ".lock"))
        deadline = time.monotonic() + 30
        while True:
            try:
                lock.acquire()
                break
            except RuntimeError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        try:
            with self._lock:
                value = self.load(name)
                change(value)
                _atomic_json(self.manifest_path(name), value)
                return value
        finally:
            lock.release()

    def _set_run(self, name, seed, **fields):
        def change(value):
            for run in value["runs"]:
                if run["seed"] == seed:
                    run.update(fields)

        return self.update(name, change)

    # lifecycle ----------------------------------------------------------
    def lifecycle(self):
        from factorio_agent.lifecycle import Lifecycle

        control = Lifecycle(self.code)
        # Runtime folders live under the base (the main repository's runtime/);
        # the code (PYTHONPATH, supervisor cwd) is this checkout.
        control.base = self.base
        control.pointer = self.base / "active-runtime.json"
        return control

    def options(self):
        from factorio_agent.automation_config import load_automation_config

        options = load_automation_config(self.code / "automation_config.json")
        return replace(options, controller=replace(options.controller, planner=self.planner))

    # start --------------------------------------------------------------
    def cost(self):
        """Per-run estimate: the larger of the last measured fleet run and the late-game default.

        A fresh run measures ~1.1 GB and ~0.4 cores; a 90-minute map ~1.2 GB and ~1.5
        cores (the server's update cost grows with the factory), so the default floors it."""
        saved = _read(self.dir / "cost.json", {}) or {}
        number = lambda key: saved.get(key) if isinstance(saved.get(key), (int, float)) else 0  # noqa: E731
        return dict(
            per_run_mb=max(number("per_run_mb"), DEFAULT_RUN_MB),
            per_run_cores=max(number("per_run_cores"), DEFAULT_RUN_CORES),
            measured_mb=saved.get("per_run_mb"),
            measured_cores=saved.get("per_run_cores"),
            source="max(cost.json, default)" if saved else "default",
        )

    def start(self, count, seeds_text, minutes, name, *, force=False, parallel=4, stagger=10.0, out=print):
        seeds = parse_seeds(seeds_text, count)
        name = name or time.strftime("fleet-%Y%m%d-%H%M%S")
        path = self.manifest_path(name)
        if path.exists():
            raise FileExistsError("fleet manifest exists: " + str(path))
        cost, host = self.cost(), machine()
        fits, why = capacity(host["free_mb"], host["cores"], cost["per_run_mb"], cost["per_run_cores"])
        out(
            f"machine: {host['free_mb']} MB free of {host['total_mb']}, {host['cores']} logical cpus; one run "
            f"~{cost['per_run_mb']} MB, ~{cost['per_run_cores']} cores ({cost['source']}); fits {fits} more "
            f"(ram {why['by_ram']}, cpu {why['by_cpu']})"
        )
        if len(seeds) > fits and not force:
            raise RuntimeError(f"{len(seeds)} runs exceed the {fits} this machine can hold now; use fewer or --force")
        roots = [self.base / "v2" / f"{name}-{seed}" for seed in seeds]
        taken = [str(r) for r in roots if r.exists()]
        if taken:
            raise FileExistsError("runtime folder already exists: " + ", ".join(taken))
        pairs = allocate_ports(len(seeds), registered_ports(self.base))
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest = dict(
            kind="factorio-fleet-v1",
            name=name,
            base=str(self.base),
            code=str(self.code),
            commit=git_commit(self.code),
            created_at=time.time(),
            target_minutes=minutes,
            planner=self.planner,
            client=self.client,
            runs=[
                dict(seed=s, runtime=str(r), server_port=u, rcon_port=t, status="planned")
                for s, r, (u, t) in zip(seeds, roots, pairs)
            ],
        )
        with RunLock(self.dir / (name + ".lock")):
            _atomic_json(path, manifest)
        options = self.options()
        create_lock = threading.Lock()  # one map generation at a time
        from factorio_agent import runtime

        def launch(index, run):
            time.sleep(index * stagger if index < parallel else 0)
            root = Path(run["runtime"])
            try:
                with create_lock:
                    self._set_run(name, run["seed"], status="creating")
                    runtime.create(
                        root,
                        seed=run["seed"],
                        server_port=run["server_port"],
                        rcon_port=run["rcon_port"],
                        automation_config=options,
                    )
                if self.client != actor_client.ALWAYS:
                    actor_client.set_mode(root, self.client)
                self._set_run(name, run["seed"], status="starting")
                result = self.lifecycle()._start(root, visible=False, automation_config=options, select=False)
                records = run_records(root)
                self._set_run(
                    name,
                    run["seed"],
                    status="running",
                    started_at=time.time(),
                    world_id=(_read(root / "world-settings.json", {}) or {}).get("world_id"),
                    processes={k: {f: v.get(f) for f in ("pid", "creation_time")} for k, v in records.items() if v},
                    supervisor_pid=result.get("supervisor_pid"),
                )
                out(f"  {run['seed']}: running on udp {run['server_port']} / rcon {run['rcon_port']}")
            except Exception as exc:  # recorded, the other runs continue
                self._set_run(name, run["seed"], status="launch_failed", launch_error=str(exc)[:300])
                out(f"  {run['seed']}: launch failed: {str(exc)[:200]}")

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            list(pool.map(lambda pair: launch(*pair), enumerate(manifest["runs"])))
        if minutes:
            self.spawn_watcher(name)
        return self.load(name)

    def spawn_watcher(self, name):
        from factorio_agent.core.process_launch import independent_process_options

        log = self.dir / (name + "-watch.log")
        with log.open("ab") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--base",
                    str(self.base),
                    "watch",
                    "--name",
                    name,
                ],
                cwd=str(self.code),
                stdout=output,
                stderr=subprocess.STDOUT,
                **independent_process_options(),
            )
        self.update(name, lambda v: v.update(watcher=dict(pid=child.pid, creation_time=_identity(child.pid))))

    # status / stop -------------------------------------------------------
    def status(self, name=None, *, enforce=True, out=print):
        fleet = self.load(name)
        if enforce and fleet.get("target_minutes") and not alive(fleet.get("watcher")):
            self.enforce(fleet["name"], out=out)
            fleet = self.load(fleet["name"])
        rows, measured = [], []
        for run in fleet["runs"]:
            root = Path(run["runtime"])
            records = run_records(root)
            up = {k: alive(v) for k, v in records.items()}
            row = dict(seed=run["seed"], runtime=run["runtime"], status=run["status"], processes=up)
            row["client"] = client_column(root, records)
            if root.exists():
                row.update(quick_summary(root))
                row.update(actor_client.test_mode(root) or {})
            if run_alive(root, records):
                live = [v for k, v in records.items() if up[k]]
                first = [process_cost(v) for v in live]
                # Start-up (map load, client assets) is not the steady cost.
                if all(first) and min(c["seconds"] for c in first) >= STEADY_SECONDS:
                    measured.append((live, first))
            rows.append(row)
        costs = []
        if measured:
            time.sleep(SAMPLE_SECONDS)
            for records, first in measured:
                second = [process_cost(v) for v in records]
                if all(second):
                    costs.append(run_cost(first, second, SAMPLE_SECONDS))
        if costs:
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(
                self.dir / "cost.json",
                dict(
                    per_run_mb=round(max(c["mb"] for c in costs)),
                    per_run_cores=round(max(c["cores"] for c in costs), 2),
                    runs_measured=len(costs),
                    measured_at=time.time(),
                    fleet=fleet["name"],
                    parts_of_largest=max(costs, key=lambda c: c["mb"])["parts"],
                ),
            )
        return dict(name=fleet["name"], target_minutes=fleet.get("target_minutes"), runs=rows)

    def enforce(self, name, out=print):
        """Stop every running run that reached the fleet's target game minutes."""
        fleet = self.load(name)
        target = fleet.get("target_minutes")
        if not target:
            return []
        due = []
        for run in fleet["runs"]:
            tick = run_tick(run["runtime"]) if run["status"] == "running" else None
            if tick is not None and tick >= target * TICKS_PER_MINUTE:
                due.append(run)
        return [self.stop_run(name, run, reason=f"reached {target} game min", out=out) for run in due]

    def stop_run(self, name, run, *, reason="stop requested", out=print):
        root = Path(run["runtime"])
        if not (root / "run.json").exists():
            self._set_run(name, run["seed"], status="stopped", stop_result="no runtime created")
            return dict(seed=run["seed"], status="stopped")
        try:
            lock = RunLock(root / "fleet-stop.lock").acquire()
        except RuntimeError:
            return dict(seed=run["seed"], status="stop_in_progress")
        try:
            control = self.lifecycle()
            records = run_records(root)
            if not any(alive(v) for v in records.values()):
                result = dict(status="stopped", note="no live process")
            else:
                result = control._shutdown(root)
            self._set_run(
                name,
                run["seed"],
                status="stopped",
                stopped_at=time.time(),
                stop_reason=reason,
                stop_result={k: result.get(k) for k in ("status", "game_paused", "note")},
                final_tick=run_tick(root),
            )
            out(f"  {run['seed']}: stopped ({reason})")
            return dict(seed=run["seed"], status="stopped")
        except Exception as exc:  # preserved for inspection; never killed blindly
            self._set_run(name, run["seed"], stop_error=str(exc)[:300])
            out(f"  {run['seed']}: stop failed, processes preserved: {str(exc)[:200]}")
            return dict(seed=run["seed"], status="stop_failed", reason=str(exc)[:300])
        finally:
            lock.release()

    def stop(self, name=None, *, parallel=4, out=print):
        fleet = self.load(name)
        runs = [
            r
            for r in fleet["runs"]
            if r["status"] != "stopped" or any(alive(v) for v in run_records(r["runtime"]).values())
        ]
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            results = list(pool.map(lambda r: self.stop_run(fleet["name"], r, out=out), runs))
        return dict(name=fleet["name"], results=results)

    def watch(self, name):
        while True:
            self.enforce(name)
            fleet = self.load(name)
            if not any(r["status"] in ("planned", "creating", "starting", "running") for r in fleet["runs"]):
                return 0
            time.sleep(WATCH_SECONDS)

    # report --------------------------------------------------------------
    def run_report(self, run, lo, hi):
        import critical_path as cp_tool
        import report_run
        from factorio_agent.progression import ROADMAP, _same

        root = Path(run["runtime"])
        records = run_records(root)
        row = dict(
            seed=run["seed"],
            runtime=run["runtime"],
            status=run["status"],
            alive=run_alive(root, records),
            client=client_column(root, records),
            launch_error=run.get("launch_error"),
            milestones={},
            furthest=None,
            blocked_reasons=[],
            controller_errors={},
        )
        label = actor_client.test_mode(root) if root.exists() else None
        if label:
            row.update(label)
        if not (root / "jobs.json").exists():
            return row
        tick = run_tick(root)
        row["game_minutes"] = round(tick / TICKS_PER_MINUTE, 1) if tick is not None else None
        timeline = cp_tool.Run(root)
        reached, far = milestones(timeline.jobs, timeline.end, ROADMAP, _same)
        row["milestones"] = reached
        row["furthest"] = dict(step=far[1], minute=far[2], index=far[0]) if far else None
        row["blocked_reasons"] = blocked_rows(timeline.jobs)
        summary = report_run.report(root, jobs=timeline.jobs)
        row.update(
            controller_errors=summary.get("controller_errors") or {},
            wall_minutes=summary.get("wall_minutes"),
            actions=summary.get("actions"),
        )
        top = min(hi, row["game_minutes"] or hi)
        if top > lo:
            try:
                path = cp_tool.report(root, lo, top, [])
            except Exception as exc:  # a half-written run must not hide the others
                row["critical_path_error"] = str(exc)[:120]
            else:
                row["lab_idle_seconds"] = path["lab"].get("lab_idle_seconds")
                row["actor_critical_seconds"] = sum(path["actor_seconds"]["critical"].values())
                row["actor_slack_seconds"] = sum(path["actor_seconds"]["slack"].values())
        return row

    def report(self, name=None, lo=0.0, hi=30.0, *, enforce=True, out=print):
        fleet = self.load(name)
        if enforce and fleet.get("target_minutes") and not alive(fleet.get("watcher")):
            self.enforce(fleet["name"], out=out)
            fleet = self.load(fleet["name"])
        runs = [self.run_report(run, lo, hi) for run in fleet["runs"]]
        return fleet, runs, aggregate(runs)


# ---------------------------------------------------------------- CLI


def _status_lines(value):
    lines = [
        f"fleet {value['name']} commit {value.get('commit') or '?'}"
        f" (target {value.get('target_minutes') or '-'} game min)"
    ]
    if any(r.get("test_mode") for r in value["runs"]):
        lines[0] += " TEST MODE client_free (C: n = no client by design, d = detached)"
    lines.append("seed        state          S C V  gmin  act blk done  last milestone")
    for r in value["runs"]:
        mark = dict(none="n ", detached="d ")
        up = "".join(
            "o " if r["processes"].get(k) else (mark.get(r.get("client"), "- ") if k == "client" else "- ")
            for k in ("server", "client", "supervisor")
        )
        lines.append(
            f"{str(r['seed']).ljust(11)} {r['status'][:14].ljust(14)} {up}"
            f"{_cell(r.get('game_minutes'), 5)} {_cell(r.get('active'), 4)}{_cell(r.get('blocked'), 4)}"
            f"{_cell(r.get('succeeded'), 5)}  {r.get('last_milestone') or '-'}"
        )
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", type=Path, help="runtime base folder (default: <repo>/runtime)")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--count", type=int)
    start.add_argument("--seeds", default="random")
    start.add_argument("--minutes", type=float, help="stop each run after this many game minutes")
    start.add_argument("--name")
    start.add_argument("--parallel", type=int, default=4, help="runs starting at once (map generation is serial)")
    start.add_argument("--stagger", type=float, default=10.0, help="seconds between the first launches")
    start.add_argument("--force", action="store_true", help="start even above the measured machine capacity")
    start.add_argument(
        "--planner",
        choices=("none", "luna"),
        default="none",
        help="controller planner for every run of this fleet (compare by starting two fleets on the same --seeds)",
    )
    start.add_argument(
        "--client",
        choices=actor_client.MODES,
        default=actor_client.ALWAYS,
        help="always (default): a hidden client per run; none: client-free TEST run (lab trigger script-credited"
        " after the real craft; no charting, see HEADLESS.md); until_lab_trigger: client until the lab trigger",
    )
    start.add_argument("--keep-client", action="store_true", help="same as --client always")
    for command in ("status", "report", "stop", "watch"):
        one = sub.add_parser(command)
        one.add_argument("--name")
        one.add_argument("--json", action="store_true")
        if command == "report":
            one.add_argument("--window", default="0-30")
    sub.add_parser("cost")
    args = parser.parse_args(argv)
    fleet = Fleet(args.base)
    quiet = getattr(args, "json", False)
    out = (lambda *_: None) if quiet else print
    if args.command == "start":
        fleet.planner = args.planner
        fleet.client = actor_client.ALWAYS if args.keep_client else args.client
        value = fleet.start(
            args.count,
            args.seeds,
            args.minutes,
            args.name,
            force=args.force,
            parallel=args.parallel,
            stagger=args.stagger,
        )
        print(f"fleet {value['name']}: manifest {fleet.manifest_path(value['name'])}")
        for run in value["runs"]:
            print(f"  {run['seed']}  {run['status']}  {run['runtime']}")
        return 0 if all(r["status"] == "running" for r in value["runs"]) else 1
    if args.command == "status":
        value = fleet.status(args.name, out=out)
        print(json.dumps(value, indent=1) if args.json else "\n".join(_status_lines(value)))
        return 0
    if args.command == "report":
        lo, hi = window(args.window)
        info, runs, agg = fleet.report(args.name, lo, hi, out=out)
        if args.json:
            print(json.dumps(dict(name=info["name"], window=[lo, hi], runs=runs, aggregate=agg), indent=1))
        else:
            print("\n".join(format_report(info, runs, agg, window_text=args.window)))
        return 0
    if args.command == "stop":
        value = fleet.stop(args.name, out=out)
        if args.json:
            print(json.dumps(value, indent=1))
        return 0 if all(r["status"] == "stopped" for r in value["results"]) else 1
    if args.command == "watch":
        return fleet.watch(args.name)
    if args.command == "cost":
        print(json.dumps(dict(machine=machine(), **fleet.cost()), indent=1))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
