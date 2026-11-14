"""Run N independent hidden-window maps in parallel and read them in one report.

Usage: python -B tools/fleet.py start --count N [--seeds a,b,c | random] [--minutes M] [--name PREFIX]
       python -B tools/fleet.py start --from FLEET:SEED[,FLEET:SEED...] --minutes M [--name PREFIX]
       python -B tools/fleet.py status [--name NAME] [--json]
       python -B tools/fleet.py report [--name NAME] [--window 0-30] [--json]
       python -B tools/fleet.py stop [--name NAME]
       python -B tools/fleet.py cost
       python -B tools/fleet.py inspect --name NAME --seed SEED [--gui]
       python -B tools/fleet.py revive --name NAME --seed SEED [--keep-timeout MIN]
       python -B tools/fleet.py postmortem --name NAME --seed SEED

Post-mortem (docs/v2/DEBUGGING_A_STALL.md "Post-mortem of a fleet run"): at the
target minute the watcher safe-stops the supervisor and pauses the world, then
triages the run (``fleet_triage``); a problem run gets a read-only snapshot in
``<run>/postmortem/`` (``factorio_agent.postmortem``) while its server is up.
``--keep-problem N`` (default 1) leaves the N worst problem runs paused with
their server up (``kept``) until ``--keep-timeout`` minutes or until a new
``start`` needs the capacity; every other run's server is stopped as before.

Each run is a new folder ``runtime/v2/<name>-<seed>`` with its own seed and its
own server/RCON port pair (allocated above the interactive range). By default
(``--client none``) a run is a client-free TEST run: a dedicated server and a
``--planner none`` supervisor only, labelled ``test_mode: client_free`` (the
lab trigger is script-credited after the real craft and the map is charted and
generated around the actor as a player would, docs/v2/HEADLESS.md).
``--client always`` (``--keep-client``, proof runs) adds the per-run automation
client with its window hidden, as ``lifecycle start --no-gui`` does;
``until_lab_trigger`` closes it once the lab trigger fired. ``runtime/active-runtime.json`` and every existing runtime are never
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
import shutil
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
from report_run import STALL_DRY_MINUTES  # noqa: E402

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
    ("silo", "rocket_silo"),
    ("launch", "rocket_launch"),
)
# Generated tail stages (rocket_plan.tail, not in progression.ROADMAP) whose success is a milestone:
# a job of this capability marks the step of the same id; rocket_launch is the final milestone.
TAIL_STAGES = ("rocket_silo", "rocket_parts", "rocket_launch")

# Problem runs kept alive for inspection (post-mortem). A paused client-free run is its
# server only: ~0.37-0.45 GB and ~0.01 cores (tick paused, supervisor stopped).
KEEP_PROBLEM = 1
KEEP_TIMEOUT_MINUTES = 60.0
PAUSED_PROBLEM, KEPT, REVIVED = "paused_problem", "kept", "revived"
HELD = (PAUSED_PROBLEM, KEPT, REVIVED)
RELEASE_SETTLE_SECONDS = 2.0

# Stall timeout (watcher, ``Fleet.enforce_stall``): a running run whose observed game tick has not
# advanced for this long is finished as a problem run, so a round always completes
# (all-1114-0107: a dead actor looped on agent_dead for 75 real minutes). Default: the fleet's own
# pace (median real minutes per game minute of its runs) times STALL_PACE_FACTOR, at least
# STALL_FLOOR_MINUTES; ``start --stall-minutes`` stores a fixed value in the manifest.
STALL_FLOOR_MINUTES = 10.0
STALL_PACE_FACTOR = 5.0
STALL_MIN_GAME_MINUTES = 2.0

# Branch copies (``start --from``): top-level entries of a source run that are not copied. Process
# records, the controller start snapshot and the client folder belong to the source's processes;
# run.json is rewritten for the new folder; post-mortems describe the source.
BRANCH_SKIP_TOP = {
    "run.json",
    "server.json",
    "supervisor-process.json",
    "controller-config.json",
    "agent-client",
    "postmortem",
}


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
        minute = round(end[0] / TICKS_PER_MINUTE, 1)
        for step in roadmap:
            if same(job, step):
                reached[step.id] = min(minute, reached.get(step.id, minute))
        if job.get("capability") in TAIL_STAGES:
            stage = job["capability"]
            reached[stage] = min(minute, reached.get(stage, minute))
    furthest = None
    for index, step in enumerate(roadmap):
        if step.id in reached:
            furthest = (index, step.id, reached[step.id])
    for offset, stage in enumerate(TAIL_STAGES, start=len(roadmap)):
        if stage in reached:
            furthest = (offset, stage, reached[stage])
    return reached, furthest


def stall_limit_minutes(fleet, ticks, now):
    """Real minutes without game-tick progress after which the watcher ends a run.

    ``fleet["stall_minutes"]`` when set; else max(``STALL_FLOOR_MINUTES``, ``STALL_PACE_FACTOR``
    x the median real minutes per game minute of the fleet's runs that played at least
    ``STALL_MIN_GAME_MINUTES``). ``ticks``: seed -> observed tick."""
    fixed = fleet.get("stall_minutes")
    if isinstance(fixed, (int, float)) and fixed > 0:
        return float(fixed)
    paces = []
    for run in fleet.get("runs", []):
        tick, started = ticks.get(run.get("seed")), run.get("started_at")
        if type(tick) is int:
            tick -= run.get("checkpoint_tick") or 0  # a branch plays from its checkpoint, not from tick 0
        if (
            type(tick) is int
            and isinstance(started, (int, float))
            and tick >= STALL_MIN_GAME_MINUTES * TICKS_PER_MINUTE
        ):
            paces.append((now - started) / 60 / (tick / TICKS_PER_MINUTE))
    pace = statistics.median(paces) if paces else 0.0
    return max(STALL_FLOOR_MINUTES, STALL_PACE_FACTOR * pace)


def stalled_runs(fleet, ticks, memory, now):
    """Running runs whose tick stood still for the stall limit: (runs, limit minutes).

    ``memory`` (seed -> [tick, since]) is the watcher's record of each run's last tick change."""
    limit = stall_limit_minutes(fleet, ticks, now)
    due = []
    for run in fleet.get("runs", []):
        seed = run.get("seed")
        if run.get("status") != "running":
            memory.pop(seed, None)
            continue
        tick = ticks.get(seed)
        seen = memory.get(seed)
        if seen is None or seen[0] != tick:
            memory[seed] = [tick, now]
        elif now - seen[1] >= 60 * limit:
            due.append(run)
    return due, limit


def parse_sources(text):
    """``fleet:seed[,fleet:seed...]`` -> [(fleet, seed text)] for ``start --from``."""
    sources = []
    for part in (text or "").split(","):
        fleet, _, seed = part.strip().rpartition(":")
        if not fleet or not seed.strip():
            raise ValueError(f"--from wants fleet:seed[,fleet:seed...], got {part.strip()!r}")
        sources.append((fleet.strip(), seed.strip()))
    if not sources:
        raise ValueError("--from wants at least one fleet:seed")
    return sources


def _checkpoint_ignore(source):
    """copytree ignore of a branch copy: the source's own process records, locks, controller start
    snapshot, post-mortems, client folder and the server's write-data (all recreated by the start)."""
    source = Path(source).resolve()

    def ignore(directory, names):
        here = Path(directory).resolve()
        skip = {n for n in names if n.endswith(".lock")}
        if here == source:
            skip |= set(names) & BRANCH_SKIP_TOP
        if here == source / "vanilla":
            skip |= set(names) & {"server-data"}
        return skip

    return ignore


def copy_checkpoint(source, target, *, server_port, rcon_port, branched_from):
    """Copy a safe-stopped run (save + state files) into the new folder ``target``; the source is only read.

    ``run.json`` is rewritten for the new folder (root, ports, a new RCON password) and records
    ``branched_from``; world identity (``world-settings.json`` and ``storage`` in the save) is kept as is:
    the branch is the same world at the same tick, so every world-bound query stays bound."""
    import secrets

    source, target = Path(source).resolve(), Path(target).resolve()
    shutil.copytree(source, target, ignore=_checkpoint_ignore(source))  # refuses an existing target
    run = _read(source / "run.json", {}) or {}
    run.update(
        root=str(target),
        server_port=server_port,
        rcon_port=rcon_port,
        rcon_password=secrets.token_urlsafe(24),
        branched_from=branched_from,
    )
    _atomic_json(target / "run.json", run)
    return target


def checkpoint_facts(root):
    """What a branch needs from a stopped run, or ValueError: why its save cannot be continued.

    The world must have been paused by the lifecycle safe stop (``lifecycle-pause.json`` phase
    ``paused`` for the run's world) and the save written after that pause; otherwise the state files
    may be newer than the save and the branch would start in a tick rollback."""
    from factorio_agent.core.factorio import NO_MOD_SAVE_NAME

    root = Path(root)
    world = (_read(root / "world-settings.json", {}) or {}).get("world_id")
    marker = _read(root / "lifecycle-pause.json", {}) or {}
    save = root / "vanilla" / "saves" / NO_MOD_SAVE_NAME
    if not (root / "run.json").exists() or not world:
        raise ValueError(f"{root} is not a started v2 runtime (run.json / world-settings.json)")
    if marker.get("kind") != "lifecycle-operator-pause-v1" or marker.get("phase") != "paused":
        raise ValueError(f"{root.name}: no safe-stop pause marker; the save may be older than the state")
    if marker.get("world_id") != world:
        raise ValueError(f"{root.name}: pause marker is for another world")
    if not save.exists() or save.stat().st_mtime_ns < (root / "lifecycle-pause.json").stat().st_mtime_ns:
        raise ValueError(f"{root.name}: no save written after the safe-stop pause")
    if actor_client.mode(root) != actor_client.NONE:
        raise ValueError(f"{root.name}: only client-free (--client none) runs can be branched")
    if death_stopped(root):
        # Its controller state says "stopped after an actor death": the watcher would end the branch at once.
        raise ValueError(f"{root.name}: the supervisor stopped after an unrecovered actor death")
    jobs = (_read(root / "jobs.json", {}) or {}).get("jobs", {})
    tick = marker.get("paused_tick") if type(marker.get("paused_tick")) is int else run_tick(root)
    return dict(
        world_id=world,
        tick=tick,
        # Unknown outcomes stay in jobs.json untouched: the branch's supervisor reconciles them, never retries.
        pending_actions=sorted(
            k
            for k, j in jobs.items()
            if j.get("pending_action") is not None or "outcome_unknown" in str(j.get("reason", ""))
        ),
        blocked=sorted(
            f"{j.get('capability')}: {normalize_reason(j.get('reason'))}"
            for j in jobs.values()
            if j.get("status") == "blocked"
        ),
    )


def branch_note(runs):
    """One header note naming the checkpoints of branched runs ('' when none is branched)."""
    rows = [r.get("branched_from") for r in runs if r.get("branched_from")]
    if not rows:
        return ""
    head = ", ".join(f"{b.get('fleet')}:{b.get('seed')}@{b.get('minute')}" for b in rows[:4])
    more = f" +{len(rows) - 4}" if len(rows) > 4 else ""
    return f" | BRANCHED from checkpoints {head}{more} (TEST runs, never proof; milestones in absolute game min)"


def death_stopped(root):
    """The run's supervisor safe-stopped itself after an unrecovered actor death (``Supervisor._stop_for_death``)."""
    state = _read(Path(root) / "logs" / "controller-state.json", {}) or {}
    reason = str(state.get("stop_reason") or "")
    return state.get("state") == "stopped" and reason.startswith(("actor_death_unrecoverable", "actor_dead"))


def death_lines(runs, top=6):
    """``actor deaths: n, causes`` over the runs' ``deaths`` (``actor_death.deaths``)."""
    rows = [(r["seed"], d) for r in runs for d in r.get("deaths") or []]
    if not rows:
        return ["actor deaths: 0"]
    causes = {}
    for seed, d in rows:
        cause = causes.setdefault(str(d.get("cause") or "unknown")[:90], dict(count=0, seeds=[], lost=0.0))
        cause["count"] += 1
        cause["lost"] += d.get("lost_seconds") or 0
        if seed not in cause["seeds"]:
            cause["seeds"].append(seed)
    lost = sum(d.get("lost_seconds") or 0 for _, d in rows)
    lines = [f"actor deaths: {len(rows)} in {len({s for s, _ in rows})} runs, {lost:.0f} game s lost; causes:"]
    for cause, row in sorted(causes.items(), key=lambda kv: -kv[1]["count"])[:top]:
        lines.append(f"  {row['count']:3d}  {cause}  ({row['lost']:.0f} s) e.g. {row['seeds'][0]}")
    return lines


def layout_settings(root, explore):
    """Write the run's ``layout-settings.json`` (``layout_selection.settings``) before its supervisor starts.

    ``explore`` True: candidate layouts are explored, deterministically per (world id, block);
    False writes an explicit off, so an inherited ``FACTORIO_LAYOUT_EXPLORE`` cannot switch it on.
    """
    from factorio_agent.layout_selection import SETTINGS_FILE

    path = Path(root) / SETTINGS_FILE
    _atomic_json(path, {"explore": bool(explore)})
    return path


def explore_requested(flag, env=None):
    """``--explore``, or the older switch ``FACTORIO_LAYOUT_EXPLORE=1`` in the environment of ``start``."""
    from factorio_agent.layout_selection import settings

    return bool(flag) or settings(None, env=env).explore


# Progress-stall columns from ``report_run.progress_stall`` (game minutes; a run is ``stalled`` when
# ``dry_now_min`` > ``report_run.STALL_DRY_MINUTES``). Not the watcher's tick stall (``stalled_runs``).
STALL_KEYS = ("last_success_min", "longest_dry_min", "longest_dry_start_min", "dry_now_min")
# Report cells after the milestones: header, row key, width.
EXTRA_COLUMNS = (("last", "last_success_min", 6), ("dry", "longest_dry_min", 6), ("drynow", "dry_now_min", 7))


def _tidy_cell(share, width=5):
    """``tidy`` cell: per cent of the board's belts outside their block's reservation."""
    return ("-" if not isinstance(share, (int, float)) else f"{round(100 * share)}%").rjust(width)


def blocked_rows(jobs, statuses=("blocked", "failed")):
    """(normalized reason, capability) of every blocked or failed job (or of the given ``statuses``)."""
    return [
        (normalize_reason(j.get("reason")), j.get("capability")) for j in jobs.values() if j.get("status") in statuses
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
        **{key: stats(r.get(key) for r in runs) for key in STALL_KEYS + ("tidy",)},
        stalled=sum(bool(r.get("stalled")) for r in runs),
        query_load={
            key: stats((r.get("query_load") or {}).get(key) for r in runs)
            for key in ("queries_per_minute", "share_percent", "per_slice", "ms_per_action")
        },
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


# Owner's oldest target: a human player has the smelting area and the main bus within 20 game minutes.
PACE_TARGET_MINUTES = 20.0
PACE_STEPS = (("smelt", "smelting_district"), ("bus", "main_bus"))
# PACE_STEPS column -> the waste_report.plate_supply_bound segment of the same step.
PACE_BOUND = {"smelt": "district", "bus": "bus"}


def pace_line(runs, target=PACE_TARGET_MINUTES):
    """``pace vs target``: per PACE_STEPS column, median minus ``target`` and the share of runs under it.

    A run that never reached the step counts as over the target (it is in the
    share's denominator, not in the median). With ``plate_bound`` rows
    (``waste_report.plate_supply_bound``), each column adds the median plate-supply
    bound (the minute the run's own iron supply could have paid what it spent up to
    the step) and the median of actual minus bound over the runs that reached it: a
    bound above the target means the supply cannot make it (plate-bound), a large
    actual-minus-bound means plates stood ready (rule-bound).
    """
    parts = []
    for head, step in PACE_STEPS:
        minutes = [r["milestones"].get(step) for r in runs]
        reached = [m for m in minutes if isinstance(m, (int, float))]
        under = sum(1 for m in reached if m <= target)
        if reached:
            median = round(statistics.median(reached), 1)
            gap = f"{median} ({median - target:+.1f})"
        else:
            gap = "- (never)"
        text = f"{head} reached {len(reached)}/{len(runs)}, median {gap}, {under}/{len(runs)} under"
        rows = [((r.get("plate_bound") or {}).get(PACE_BOUND[head]) or {}) for r in runs]
        rows = [
            b for b in rows if isinstance(b.get("bound"), (int, float)) and isinstance(b.get("actual"), (int, float))
        ]
        if rows:
            bound = round(statistics.median(b["bound"] for b in rows), 1)
            excess = round(statistics.median(b["actual"] - b["bound"] for b in rows), 1)
            text += f", plate bound {bound} (actual {excess:+.1f})"
        parts.append(text)
    return f"pace vs target {target:g} min: " + "; ".join(parts)


def _cell(value, width):
    text = "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))
    return text.rjust(width)


def _width(head):
    """Milestone column width: 6, or wider so a long head keeps a space before it (``silo launch``)."""
    return max(6, len(head) + 1)


def format_report(fleet, runs, agg, *, window_text, top=8):
    """The compact table: one header, one line per run, 3 stat lines, histograms."""
    heads = [h for h, _ in COLUMNS]
    lines = [
        f"fleet {fleet.get('name')} commit {fleet.get('commit') or '?'}: {len(runs)} runs,"
        f" target {'+' if fleet.get('branch') else ''}{fleet.get('target_minutes') or '-'} game min,"
        f" critical-path window {window_text} min; milestone cells = game minute the step succeeded"
    ]
    lines[0] += branch_note(fleet.get("runs") or [])
    tests = [r for r in runs if r.get("test_mode")]
    if tests:
        lines[0] += f" | TEST MODE client_free in {len(tests)} runs (script-credited hand-craft triggers; not a proof)"
    if fleet.get("respawn") or any(r.get("respawn_emulation") for r in runs):
        lines[0] += " | respawn emulation on"
    if fleet.get("explore"):
        lines[0] += " | layout EXPLORATION on (runner-up variants by seed)"
    stalled = [r for r in runs if r.get("stalled")]
    lines[0] += (
        f" | stalled {len(stalled)}/{len(runs)} (no job success for > {STALL_DRY_MINUTES:g} game min at the end)"
    )
    lines.append(
        "seed".ljust(11)
        + " up "
        + " cli "
        + " gmin"
        + "".join(h.rjust(_width(h)) for h in heads)
        + "".join(h.rjust(w) for h, _, w in EXTRA_COLUMNS)
        + "tidy".rjust(5)
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
            + "".join(_cell(r["milestones"].get(step), _width(h)) for h, step in COLUMNS)
            + "".join(_cell(r.get(key), w) for _, key, w in EXTRA_COLUMNS)
            + _tidy_cell(r.get("tidy"))
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
        extra = "".join(_cell((agg.get(k) or {}).get(key), w) for _, k, w in EXTRA_COLUMNS)
        tidy = _tidy_cell((agg.get("tidy") or {}).get(key))
        lines.append(
            key.ljust(20)
            + _cell(gmin, 5)
            + "".join(_cell(c, _width(h)) for h, c in zip(heads, cells))
            + extra
            + tidy
            + _cell(lab, 10)
        )
    # Same 25-character lead as the header (seed, up, cli, gmin) so each count sits under its head.
    reached = [f"{(agg['columns'][h] or {}).get('n', 0)}/{len(runs)}" for h in heads]
    lines.append("reached".ljust(25) + "".join(_cell(n, _width(h)) for h, n in zip(heads, reached)))
    lines.append(pace_line(runs))
    load = agg.get("query_load") or {}
    if any(load.values()):
        median = {key: (row or {}).get("median") for key, row in load.items()}
        lines.append(
            "rcon query median: {queries_per_minute}/min, {share_percent}% of command time,"
            " {per_slice}/slice, {ms_per_action} ms/action".format(**median)
        )
    lines.append(f"blocked/failed jobs now (reason -> count -> example seed), top {top}:")
    rows = list(agg["blocked_reasons"].items())[:top]
    lines += [f"  {v['count']:3d}  {reason}  [{v['capability']}] e.g. {v['seeds'][0]}" for reason, v in rows] or [
        "  none"
    ]
    lines.append("controller errors (count -> example seed), top 5:")
    rows = list(agg["controller_errors"].items())[:5]
    lines += [f"  {v['count']:3d}  {reason}  e.g. {v['seeds'][0]}" for reason, v in rows] or ["  none"]
    lines += death_lines(runs)
    if tests:
        credits = [
            f"{r['seed']}:"
            + ",".join(f"{c.get('technology')}@{c.get('tick')}" for c in r.get("script_credited_triggers") or [])
            for r in tests
        ]
        lines.append("test-mode credits (seed:technology@tick): " + " ".join(credits)[:400])
    layouts = [row for r in runs for row in r.get("layouts") or []]
    if layouts:  # layout candidates (docs/v2/LAYOUT_PRINCIPLES.md): which variant each block used
        from factorio_agent.layout_selection import format_variants

        lines += format_variants(layouts)
    from factorio_agent.bandwidth_audit import format_counts

    lines += format_counts(runs)  # belt lane bandwidth findings per block type (bandwidth_audit)
    from factorio_agent.bottleneck_audit import format_idle

    lines += format_idle(runs)  # "idle with work: seconds, top gates" (bottleneck_audit idle watchdog)
    failed = [r for r in runs if r.get("launch_error")]
    for r in failed[:4]:
        lines.append(f"launch failed {r['seed']}: {r['launch_error'][:100]}")
    lines += problem_lines(runs)
    lines += stalled_lines(runs)
    return lines


def stalled_lines(runs, top=12, reasons=3):
    """Problem runs that stopped progressing: last success, dry span, blocked reasons active at the end."""
    rows = sorted((r for r in runs if r.get("stalled")), key=lambda r: -(r.get("dry_now_min") or 0))
    if not rows:
        return []
    lines = [f"stalled runs (no job success for > {STALL_DRY_MINUTES:g} game min; blocked/failed reasons at the end):"]
    for r in rows[:top]:
        lines.append(
            f"  {str(r['seed']).ljust(11)} last success {_cell(r.get('last_success_min'), 1).strip()}"
            f", dry {_cell(r.get('dry_now_min'), 1).strip()} min at gmin {_cell(r.get('game_minutes'), 1).strip()}"
        )
        shown = list(dict.fromkeys(r.get("blocked_now") or r.get("blocked_reasons") or []))
        for reason, capability in shown[:reasons]:
            lines.append(f"      [{capability}] {reason}")
        if len(shown) > reasons:
            lines.append(f"      ... {len(shown) - reasons} more")
    return lines


def problem_lines(runs, top=12):
    """The "problem runs" section: class, blocking job and reason, kept ports, postmortem folder."""
    rows = [r for r in runs if (r.get("problem") or {}).get("class") not in (None, "healthy")]
    if not rows:
        return []
    lines = ["problem runs (class: blocking job reason | cause from the post-mortem | folder):"]
    for r in sorted(rows, key=lambda r: -((r["problem"].get("score")) or 0))[:top]:
        p = r["problem"]
        block = p.get("blocking") or {}
        text = f"  {str(r['seed']).ljust(11)} {p['class']:<8}"
        text += (
            f" {block.get('capability')}: {str(block.get('reason'))[:70]}"
            if block
            else f" {'; '.join(p.get('reasons') or [])[:80]}"
        )
        if p.get("state") in HELD:
            text += f" [{p['state']} udp {p.get('server_port')} rcon {p.get('rcon_port')}"
            text += f" until {time.strftime('%H:%M', time.localtime(p['keep_until']))}]" if p.get("keep_until") else "]"
        lines.append(text)
        if p.get("cause"):
            lines.append(f"      cause: {p['cause'][:150]}")
        if p.get("folder"):
            lines.append(f"      {p['folder']}")
    return lines


def problem_row(run, root):
    """A run's triage verdict (manifest summary, else ``postmortem/triage.json``) plus its post-mortem."""
    from factorio_agent import postmortem

    verdict = run.get("triage") or _read(Path(root) / "postmortem" / "triage.json")
    if not isinstance(verdict, dict):
        return None
    latest = postmortem.latest(root) or {}
    probes = latest.get("probes") or []
    return {
        "class": verdict.get("class"),
        "reasons": (verdict.get("reasons") or [])[:3],
        "blocking": verdict.get("blocking"),
        "score": verdict.get("score"),
        "state": run.get("status"),
        "server_port": run.get("server_port"),
        "rcon_port": run.get("rcon_port"),
        "keep_until": run.get("keep_until"),
        "folder": run.get("postmortem") or latest.get("folder"),
        "cause": probes[0].get("cause") if probes else None,
    }


def layout_svg(root):
    """The dashboard's layout map of a runtime (read-only), for the post-mortem folder."""
    import dashboard

    return dashboard.Dashboard(root).layout_svg()


def gui_command(cfg, gui_dir, save):
    """A visible client command on ``save`` (single player) with its own profile under ``gui_dir``."""
    from factorio_agent.core.deterministic_character import prepare_client

    command, client_root = prepare_client(replace(cfg, runtime_dir=Path(gui_dir)))
    out, skip = [], 0
    for part in command:
        if skip:
            skip -= 1
        elif part in ("--mp-connect", "--port", "--password", "--window-size"):
            skip = 1
        else:
            out.append(part)
    return out + ["--window-size", "1600x900", "--load-game", str(save)], client_root


def _spawn_visible(command, cwd):
    from factorio_agent.core.process_launch import independent_process_options

    options = independent_process_options()
    child = subprocess.Popen(command, cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    return child.pid


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
        self.client = "none"
        # Post-mortem: problem runs get a read-only snapshot before their server stops;
        # the keep_problem worst stay paused with the server up for keep_timeout minutes.
        self.postmortem = True
        self.keep_problem = KEEP_PROBLEM
        self.keep_timeout = KEEP_TIMEOUT_MINUTES
        self._milestone_cache = {}

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

    def start(
        self, count, seeds_text, minutes, name, *, force=False, parallel=4, stagger=10.0, explore=False, out=print
    ):
        seeds = parse_seeds(seeds_text, count)
        name = name or time.strftime("fleet-%Y%m%d-%H%M%S")
        path = self.manifest_path(name)
        if path.exists():
            raise FileExistsError("fleet manifest exists: " + str(path))
        cost = self.cost()
        fits, why, host = self.release_for_capacity(len(seeds), cost, out=out)
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
            explore=bool(explore),
            postmortem=self.postmortem,
            keep_problem=self.keep_problem,
            keep_timeout_minutes=self.keep_timeout,
            stall_minutes=getattr(self, "stall_minutes", None),
            respawn=bool(getattr(self, "respawn", False)) and self.client == actor_client.NONE,
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
                    if self.client == actor_client.NONE and getattr(self, "respawn", False):
                        actor_client.set_respawn(root, True)  # optional, owner approval pending (HEADLESS.md)
                layout_settings(root, explore)
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

    def branch_source(self, fleet_name, seed):
        """The checkpoint of one stopped fleet run for ``start --from`` (ValueError/RuntimeError: why not)."""
        fleet, run = self.find_run(fleet_name, seed)
        root = Path(run["runtime"])
        live = [k for k, v in run_records(root).items() if alive(v)]
        if live:
            raise RuntimeError(f"{fleet['name']}:{run['seed']} is not stopped (live {', '.join(live)})")
        facts = checkpoint_facts(root)
        return dict(
            fleet=fleet["name"],
            seed=run["seed"],
            runtime=str(root),
            tick=facts["tick"],
            minute=round(facts["tick"] / TICKS_PER_MINUTE, 1) if type(facts["tick"]) is int else None,
            commit=fleet.get("commit"),
            world_id=facts["world_id"],
            pending_actions=facts["pending_actions"],
            blocked=facts["blocked"],
        )

    def branch(self, sources, minutes, name, *, force=False, parallel=4, stagger=10.0, out=print):
        """New runs that continue stopped runs' saves with THIS code (``start --from``; docs/v2/README.md).

        Each source is copied into ``runtime/v2/<name>-<seed>`` (``copy_checkpoint``; the source is only
        read), gets fresh ports and is started by the ordinary lifecycle start (server from the copied save,
        same world identity, ``--planner none`` supervisor resuming jobs.json). ``minutes`` counts game
        minutes from the checkpoint tick (per run ``target_tick``). Branch runs are TEST runs (never proof)."""
        if not minutes:
            raise ValueError("start --from needs --minutes (game minutes after the checkpoint)")
        found = [self.branch_source(f, s) for f, s in parse_sources(sources)]
        seeds = [b["seed"] for b in found]
        if len(set(seeds)) != len(seeds):
            raise ValueError("two checkpoints share a seed; branch them into separate fleets (--name)")
        name = name or time.strftime("branch-%Y%m%d-%H%M%S")
        path = self.manifest_path(name)
        if path.exists():
            raise FileExistsError("fleet manifest exists: " + str(path))
        cost = self.cost()
        fits, _why, _host = self.release_for_capacity(len(found), cost, out=out)
        if len(found) > fits and not force:
            raise RuntimeError(f"{len(found)} runs exceed the {fits} this machine can hold now; use fewer or --force")
        roots = [self.base / "v2" / f"{name}-{seed}" for seed in seeds]
        taken = [str(r) for r in roots if r.exists()]
        if taken:
            raise FileExistsError("runtime folder already exists: " + ", ".join(taken))
        pairs = allocate_ports(len(found), registered_ports(self.base))
        self.dir.mkdir(parents=True, exist_ok=True)
        runs = []
        for source, root, (udp, tcp) in zip(found, roots, pairs):
            origin = {k: source[k] for k in ("fleet", "seed", "runtime", "tick", "minute", "commit", "world_id")}
            runs.append(
                dict(
                    seed=source["seed"],
                    runtime=str(root),
                    server_port=udp,
                    rcon_port=tcp,
                    status="planned",
                    branched_from=origin,
                    checkpoint_tick=source["tick"] or 0,
                    target_tick=int((source["tick"] or 0) + minutes * TICKS_PER_MINUTE),
                    pending_actions_at_branch=source["pending_actions"],
                    blocked_at_branch=source["blocked"],
                )
            )
        manifest = dict(
            kind="factorio-fleet-v1",
            name=name,
            base=str(self.base),
            code=str(self.code),
            commit=git_commit(self.code),
            created_at=time.time(),
            target_minutes=minutes,
            branch=True,
            branched_from=[r["branched_from"] for r in runs],
            planner=self.planner,
            client=actor_client.NONE,
            explore=False,
            postmortem=self.postmortem,
            keep_problem=self.keep_problem,
            keep_timeout_minutes=self.keep_timeout,
            stall_minutes=getattr(self, "stall_minutes", None),
            respawn=False,
            runs=runs,
        )
        with RunLock(self.dir / (name + ".lock")):
            _atomic_json(path, manifest)
        options = self.options()

        def launch(index, run):
            time.sleep(index * stagger if index < parallel else 0)
            root, origin = Path(run["runtime"]), run["branched_from"]
            try:
                self._set_run(name, run["seed"], status="creating")
                copy_checkpoint(
                    origin["runtime"],
                    root,
                    server_port=run["server_port"],
                    rcon_port=run["rcon_port"],
                    branched_from=dict(origin, branch_fleet=name),
                )
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
                note = f", {len(run['pending_actions_at_branch'])} unknown outcomes to reconcile" * bool(
                    run["pending_actions_at_branch"]
                )
                out(
                    f"  {run['seed']}: branched from {origin['fleet']} at {origin['minute']} game min, running on"
                    f" udp {run['server_port']} / rcon {run['rcon_port']}; {len(run['blocked_at_branch'])} jobs"
                    f" blocked at the checkpoint{note}"
                )
            except Exception as exc:  # recorded, the other runs continue
                self._set_run(name, run["seed"], status="launch_failed", launch_error=str(exc)[:300])
                out(f"  {run['seed']}: branch failed: {str(exc)[:200]}")

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            list(pool.map(lambda pair: launch(*pair), enumerate(manifest["runs"])))
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
                cwd=str(self.base),  # not the code snapshot: the fleet loop removes it after the round
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
            self.release_expired(fleet["name"], out=out)
            fleet = self.load(fleet["name"])
        rows, measured = [], []
        for run in fleet["runs"]:
            root = Path(run["runtime"])
            records = run_records(root)
            up = {k: alive(v) for k, v in records.items()}
            row = dict(seed=run["seed"], runtime=run["runtime"], status=run["status"], processes=up)
            if run.get("branched_from"):
                row["branched_from"] = run["branched_from"]
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
        return dict(
            name=fleet["name"],
            target_minutes=fleet.get("target_minutes"),
            explore=fleet.get("explore"),
            branch=fleet.get("branch"),
            runs=rows,
        )

    def enforce(self, name, out=print, *, postmortem=False):
        """Stop every running run that reached the fleet's target game minutes.

        ``postmortem`` (the watcher): triage each run first and snapshot problem runs (``finish_run``)."""
        fleet = self.load(name)
        target = fleet.get("target_minutes")
        if not target:
            return []
        due = []
        for run in fleet["runs"]:
            tick = run_tick(run["runtime"]) if run["status"] == "running" else None
            # A branched run stops M game minutes after its checkpoint (``target_tick``).
            limit = run.get("target_tick") if type(run.get("target_tick")) is int else target * TICKS_PER_MINUTE
            if tick is not None and tick >= limit:
                due.append(run)
        reason = f"reached {'+' if fleet.get('branch') else ''}{target} game min"
        if postmortem and (fleet.get("postmortem", True) or fleet.get("keep_problem")):
            return self.finish_runs(name, due, reason=reason, out=out)
        return [self.stop_run(name, run, reason=reason, out=out) for run in due]

    def stop_run(self, name, run, *, reason="stop requested", out=print):
        if run.get("status") in HELD:  # paused problem run: shut down without a second safe stop
            return self.release_run(name, run, why=reason, out=out)
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

    def watch(self, name, *, sleep=time.sleep):
        # Load what the stop and release paths import now: the fleet loop removes this code
        # snapshot after the round while kept runs may still wait here for their timeout.
        import fleet_triage  # noqa: F401
        import waste_report  # noqa: F401
        from factorio_agent import postmortem  # noqa: F401

        self.lifecycle()
        while True:
            self.enforce(name, postmortem=True)
            self.enforce_stall(name)
            fleet = self.load(name)
            if not any(r["status"] in ("planned", "creating", "starting", "running") for r in fleet["runs"]):
                self.settle_kept(name)
                self.release_expired(name)
                if not any(r["status"] in HELD for r in self.load(name)["runs"]):
                    return 0
            sleep(WATCH_SECONDS)

    def enforce_stall(self, name, *, now=None, out=print):
        """Watcher: finish every running run whose game tick stood still for the stall limit (``stalled_runs``).

        The run is paused, triaged (its last controller error, e.g. ``agent_dead``, leads the
        verdict) and snapshotted like a run at the target minute, so the round always completes."""
        fleet = self.load(name)
        now = time.time() if now is None else now
        ticks = {r["seed"]: run_tick(r["runtime"]) for r in fleet["runs"] if r.get("status") == "running"}
        memory = self.__dict__.setdefault("_stall_memory", {})
        dead = [r for r in fleet["runs"] if r.get("status") == "running" and death_stopped(r["runtime"])]
        if dead:
            # The supervisor stopped itself after an unrecovered actor death: end it now, no stall wait.
            for run in dead:
                memory.pop(run["seed"], None)
            return self._finish_due(
                name, fleet, dead, "errored: agent_dead (supervisor stopped after an actor death)", out
            )
        due, limit = stalled_runs(fleet, ticks, memory, now)
        if not due:
            return []
        for run in due:
            memory.pop(run["seed"], None)
            self._set_run(name, run["seed"], stalled=dict(tick=ticks.get(run["seed"]), limit_minutes=round(limit, 1)))
        return self._finish_due(name, fleet, due, f"stalled: no game-time progress for {limit:.0f} real min", out)

    def _finish_due(self, name, fleet, due, reason, out):
        if fleet.get("postmortem", True) or fleet.get("keep_problem"):
            return self.finish_runs(name, due, reason=reason, out=out)
        return [self.stop_run(name, run, reason=reason, out=out) for run in due]

    # post-mortem (docs/v2/DEBUGGING_A_STALL.md "Post-mortem of a fleet run") ------
    def finish_runs(self, name, due, *, reason, out=print):
        """Pause every due run (supervisor safe stop, world paused and saved), then triage each, snapshot
        problem runs while their server is up, and shut down the healthy ones; problem runs stay
        ``paused_problem`` when the fleet keeps any (``settle_kept`` picks the worst at the round end)."""
        paused = [(run, self._pause_run(name, run, out=out)) for run in due]
        return [self._after_pause(name, run, state, reason=reason, out=out) for run, state in paused]

    def _pause_run(self, name, run, *, out=print):
        root = Path(run["runtime"])
        records = run_records(root)
        if not (root / "run.json").exists() or not any(alive(v) for v in records.values()):
            return None
        was_alive = run_alive(root, records)
        try:
            lock = RunLock(root / "fleet-stop.lock").acquire()
        except RuntimeError:
            return {"error": "stop_in_progress"}
        try:
            return dict(stopped=self.lifecycle()._stop(root), alive=was_alive)
        except Exception as exc:  # preserved for inspection; the plain stop below records it
            return {"error": str(exc)[:300]}
        finally:
            lock.release()

    def _after_pause(self, name, run, state, *, reason, out=print):
        import fleet_triage

        if not state or state.get("error"):
            return self.stop_run(name, run, reason=reason, out=out)
        root, seed = Path(run["runtime"]), run["seed"]
        fleet = self.load(name)
        try:
            verdict = self.triage_run(fleet, run, alive_before=state["alive"])
        except Exception as exc:  # the run is still stopped; the verdict says why it has no triage
            verdict = dict(flags=["errored"], reasons=[f"triage failed: {exc}"[:200]], jobs=[], score=1.0)
            verdict["class"] = "errored"
        folder = error = None
        if verdict["class"] != "healthy" and fleet.get("postmortem", True):
            folder, error = self.snapshot_run(run, verdict)
        fields = dict(
            stopped_at=time.time(),
            stop_reason=reason,
            stop_result={k: state["stopped"].get(k) for k in ("status", "game_paused", "note")},
            final_tick=run_tick(root),
            triage=fleet_triage.summary(verdict),
            postmortem=folder,
            postmortem_error=error,
        )
        if verdict["class"] != "healthy" and (fleet.get("keep_problem") or 0) > 0:
            self._set_run(name, seed, status=PAUSED_PROBLEM, paused_at=time.time(), **fields)
            out(f"  {seed}: paused ({verdict['class']}), server kept up until the round ends")
            return dict(seed=seed, status=PAUSED_PROBLEM)
        out(f"  {seed}: {verdict['class']}" + (f", post-mortem {folder}" if folder else ""))
        return self.release_run(name, run, why=reason, fields=fields, out=out)

    def _milestones_of(self, run):
        import critical_path as cp_tool
        from factorio_agent.progression import ROADMAP, _same

        root = Path(run["runtime"])
        events = root / "job-events.jsonl"
        if not (root / "jobs.json").exists() or not events.exists():
            return {}
        key = (str(root), events.stat().st_size, (root / "jobs.json").stat().st_mtime_ns)
        if key not in self._milestone_cache:
            timeline = cp_tool.Run(root)
            self._milestone_cache[key] = milestones(timeline.jobs, timeline.end, ROADMAP, _same)[0]
        return self._milestone_cache[key]

    def triage_run(self, fleet, run, *, alive_before=True):
        """``fleet_triage.classify`` against the fleet's current milestone medians; writes postmortem/triage.json."""
        import fleet_triage

        root = Path(run["runtime"])
        maps = {}
        for other in fleet["runs"]:
            try:
                maps[other["seed"]] = self._milestones_of(other)
            except Exception:  # a half-written run must not hide the others
                maps[other["seed"]] = {}
        medians = fleet_triage.fleet_medians(list(maps.values()), [step for _, step in COLUMNS])
        verdict = fleet_triage.classify(
            fleet_triage.run_facts(root),
            milestones=maps.get(run["seed"]) or {},
            medians=medians,
            alive=alive_before,
            waste=fleet_triage.waste_evidence(root),
        )
        verdict.update(seed=run["seed"], fleet=fleet["name"], triaged_at=time.time(), medians=medians)
        (root / "postmortem").mkdir(parents=True, exist_ok=True)
        _atomic_json(root / "postmortem" / "triage.json", verdict)
        return verdict

    def snapshot_run(self, run, verdict):
        """(folder, error) of a read-only post-mortem snapshot of the paused run."""
        from factorio_agent import postmortem

        try:
            manifest = postmortem.snapshot(Path(run["runtime"]), triage=verdict, layout=layout_svg)
        except Exception as exc:  # the stop goes on; the error is recorded on the run
            return None, f"{type(exc).__name__}: {exc}"[:300]
        errors = "; ".join(f"{k}: {v}" for k, v in manifest["errors"].items())[:300]
        return manifest["folder"], errors or None

    def release_run(self, name, run, *, why, fields=None, out=print):
        """Shut down a paused (held) run's server without a second safe stop; the save stays."""
        root, seed = Path(run["runtime"]), run["seed"]
        try:
            lock = RunLock(root / "fleet-stop.lock").acquire()
        except RuntimeError:
            return dict(seed=seed, status="stop_in_progress")
        try:
            records = run_records(root)
            control = self.lifecycle()
            if alive(records.get("supervisor")):
                control._shutdown(root)  # not paused by us: the ordinary safe stop first
            elif alive(records.get("server")) or alive(records.get("client")):
                control._shutdown(root, dict(status="stopped", game_paused=True, note="paused before the stop"))
            self._set_run(name, seed, status="stopped", released_at=time.time(), release_reason=why, **(fields or {}))
            out(f"  {seed}: stopped ({why})")
            return dict(seed=seed, status="stopped")
        except Exception as exc:  # preserved for inspection; never killed blindly
            self._set_run(name, seed, stop_error=str(exc)[:300])
            out(f"  {seed}: stop failed, processes preserved: {str(exc)[:200]}")
            return dict(seed=seed, status="stop_failed", reason=str(exc)[:300])
        finally:
            lock.release()

    def settle_kept(self, name, out=print):
        """Round over: keep the ``keep_problem`` worst paused problem runs, shut the other ones down."""
        import fleet_triage

        fleet = self.load(name)
        if any(r["status"] in ("planned", "creating", "starting", "running") for r in fleet["runs"]):
            return []
        paused = [r for r in fleet["runs"] if r["status"] == PAUSED_PROBLEM]
        keep = set(
            fleet_triage.select_kept([(r["seed"], r.get("triage")) for r in paused], fleet.get("keep_problem") or 0)
        )
        until = time.time() + 60 * float(fleet.get("keep_timeout_minutes") or KEEP_TIMEOUT_MINUTES)
        results = []
        for run in paused:
            if run["seed"] in keep:
                self._set_run(name, run["seed"], status=KEPT, keep_until=until)
                out(f"  {run['seed']}: kept for inspection (udp {run.get('server_port')}, rcon {run.get('rcon_port')})")
                results.append(dict(seed=run["seed"], status=KEPT))
            else:
                results.append(self.release_run(name, run, why="not among the worst problem runs", out=out))
        return results

    def release_expired(self, name, *, now=None, out=print):
        """Shut down held runs past their keep deadline; mark held runs whose server is gone as stopped."""
        fleet = self.load(name)
        now = time.time() if now is None else now
        timeout = 60 * float(fleet.get("keep_timeout_minutes") or KEEP_TIMEOUT_MINUTES)
        results = []
        for run in fleet["runs"]:
            if run["status"] not in HELD:
                continue
            if not any(alive(v) for v in run_records(run["runtime"]).values()):
                self._set_run(name, run["seed"], status="stopped", release_reason="server already down")
                continue
            deadline = run.get("keep_until") or (run.get("paused_at") or run.get("revived_at") or now) + timeout
            if now >= deadline:
                results.append(self.release_run(name, run, why="keep timeout", out=out))
        return results

    def held_runs(self):
        """(fleet name, run) of every paused/kept/revived run in any manifest."""
        rows = []
        for path in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            value = _read(path)
            if path.name == "cost.json" or not isinstance(value, dict) or value.get("kind") != "factorio-fleet-v1":
                continue
            rows += [(value["name"], r) for r in value.get("runs", []) if r.get("status") in HELD]
        return rows

    def release_for_capacity(self, count, cost, *, out=print):
        """Capacity for ``count`` new runs: expired held runs go first, then the oldest held runs as needed."""
        for name in dict.fromkeys(n for n, _ in self.held_runs()):
            self.settle_kept(name, out=out)  # a finished round whose watcher has not settled yet
            self.release_expired(name, out=out)
        host = machine()
        fits, why = capacity(host["free_mb"], host["cores"], cost["per_run_mb"], cost["per_run_cores"])
        # Unselected paused runs first, then the least interesting, then the oldest.
        held = sorted(
            self.held_runs(),
            key=lambda nr: (
                nr[1]["status"] != PAUSED_PROBLEM,
                (nr[1].get("triage") or {}).get("score") or 0,
                nr[1].get("paused_at") or nr[1].get("revived_at") or 0,
            ),
        )
        while count > fits and held:
            name, run = held.pop(0)
            self.release_run(name, run, why="capacity for a new fleet", out=out)
            time.sleep(RELEASE_SETTLE_SECONDS)  # the server's memory is returned after its exit
            host = machine()
            fits, why = capacity(host["free_mb"], host["cores"], cost["per_run_mb"], cost["per_run_cores"])
        return fits, why, host

    def find_run(self, name, seed):
        fleet = self.load(name)
        for run in fleet["runs"]:
            if str(run["seed"]) == str(seed):
                return fleet, run
        raise LookupError(f"no run {seed} in fleet {fleet['name']}")

    def revive(self, name, seed, *, timeout_minutes=None, out=print):
        """Start a stopped run's server again from its save, paused, without supervisor or client."""
        from factorio_agent import runtime

        fleet, run = self.find_run(name, seed)
        root = Path(run["runtime"])
        records = run_records(root)
        if alive(records.get("supervisor")) or alive(records.get("server")):
            raise RuntimeError(f"run {seed} still has a live server or supervisor (status {run['status']})")
        control = self.lifecycle()
        with RunLock(root / "fleet-stop.lock"):
            control._ensure_server(root)  # runtime.start: the run's own save, ports and world identity
            world = runtime.game_for(root).query("return {ok=true,tick=game.tick,paused=game.tick_paused}")
            if not world.get("paused"):
                with RunLock(root / "writer.lock"):
                    world = control._game_pause(root, True)
        minutes = float(timeout_minutes or fleet.get("keep_timeout_minutes") or KEEP_TIMEOUT_MINUTES)
        self._set_run(
            name,
            run["seed"],
            status=REVIVED,
            revived_at=time.time(),
            keep_until=time.time() + 60 * minutes,
            revived_tick=world.get("tick"),
        )
        if not alive(self.load(name).get("watcher")):
            self.spawn_watcher(fleet["name"])  # releases it at keep_until
        out(f"  {seed}: server up and paused at tick {world.get('tick')} for {minutes:g} min")
        return dict(seed=run["seed"], status=REVIVED, tick=world.get("tick"))

    def postmortem_run(self, name, seed, *, out=print):
        """A new snapshot of a held (paused, server up, supervisor stopped) run."""
        fleet, run = self.find_run(name, seed)
        root = Path(run["runtime"])
        records = run_records(root)
        if not alive(records.get("server")):
            raise RuntimeError(f"run {seed} has no live server; revive it first (fleet.py revive)")
        if alive(records.get("supervisor")):
            raise RuntimeError(f"run {seed} is still playing; a post-mortem needs the paused world")
        verdict = self.triage_run(fleet, run)
        folder, error = self.snapshot_run(run, verdict)
        self._set_run(name, run["seed"], postmortem=folder, postmortem_error=error)
        out(f"  {seed}: post-mortem {folder or 'failed: ' + str(error)}")
        return dict(seed=run["seed"], folder=folder, error=error)

    def inspect_lines(self, name, seed):
        """How to look at one run read-only (and how the owner opens it in a GUI)."""
        from factorio_agent import postmortem

        fleet, run = self.find_run(name, seed)
        root = Path(run["runtime"])
        up = {k: alive(v) for k, v in run_records(root).items()}
        n, s = fleet["name"], run["seed"]
        lines = [
            f"run {s} of fleet {n}: status {run['status']}; server {'up' if up.get('server') else 'down'},"
            f" supervisor {'up' if up.get('supervisor') else 'stopped'}",
            f"runtime {root}",
            f"ports: game udp {run.get('server_port')}, rcon 127.0.0.1:{run.get('rcon_port')}"
            " (password: runtime.configuration(root).rcon_password)",
        ]
        if run.get("keep_until") and run["status"] in HELD:
            lines.append("held until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(run["keep_until"])))
        verdict = run.get("triage") or _read(root / "postmortem" / "triage.json") or {}
        if verdict:
            lines.append(
                f"triage: {verdict.get('class')} {verdict.get('flags')}: "
                + "; ".join(verdict.get("reasons") or [])[:300]
            )
        latest = postmortem.latest(root) or {}
        if latest:
            lines.append(f"post-mortem: {latest.get('folder')} (manifest.json, observe.json, layout.svg, probes/)")
            lines += [
                f"  {p.get('capability')} {p.get('key')}: {p.get('cause')}"[:220] for p in latest.get("probes") or []
            ]
        if up.get("server") and not up.get("supervisor"):
            lines += [
                "read-only access (the world is paused; queries only, never game.act):",
                "  python -B -c \"import sys; sys.path.insert(0, 'src'); from pathlib import Path;"
                " from factorio_agent import runtime; g = runtime.game_for(Path(r'" + str(root) + "'));"
                " print(g.query('return {ok=true,tick=game.tick,paused=game.tick_paused}'))\"",
                f"new snapshot: python -B tools/fleet.py postmortem --name {n} --seed {s}",
            ]
        elif up.get("supervisor"):
            lines.append("the run is still playing: read-only queries only (DEBUGGING_A_STALL.md section 3)")
        else:
            lines.append(
                f"server is down: python -B tools/fleet.py revive --name {n} --seed {s}  (paused, from its save)"
            )
        lines.append(
            f"owner GUI: python -B tools/fleet.py inspect --name {n} --seed {s} --gui  (a visible single-player client"
            " on a COPY of the run's save; the run's server and files are not touched; do not join the server)"
        )
        return lines

    def open_gui(self, name, seed, *, spawn=None, out=print):
        """Open a copy of the run's save in a visible client (own profile under postmortem/gui)."""
        from factorio_agent import runtime
        from factorio_agent.core.factorio import no_mod_save_path

        _fleet, run = self.find_run(name, seed)
        root = Path(run["runtime"])
        cfg = runtime.configuration(root)
        save = no_mod_save_path(cfg)
        if not save.exists():
            raise FileNotFoundError("no save of this run: " + str(save))
        gui = root / "postmortem" / "gui"
        gui.mkdir(parents=True, exist_ok=True)
        copy = gui / f"inspect-{run['seed']}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        shutil.copy2(save, copy)
        command, cwd = gui_command(cfg, gui, copy)
        pid = (spawn or _spawn_visible)(command, cwd)
        out(f"  {run['seed']}: GUI client pid {pid} on {copy} (single player; unpause with the console if needed)")
        return dict(pid=pid, save=str(copy), command=command)

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
            branched_from=run.get("branched_from"),
            milestones={},
            furthest=None,
            blocked_reasons=[],
            controller_errors={},
        )
        label = actor_client.test_mode(root) if root.exists() else None
        if label:
            row.update(label)
        if root.exists():
            from factorio_agent.actor_death import deaths

            row["deaths"] = deaths(root)
        row["problem"] = problem_row(run, root) if root.exists() else None
        if not (root / "jobs.json").exists():
            return row
        from factorio_agent.layout_selection import run_layouts

        from factorio_agent.bandwidth_audit import run_counts

        row["bandwidth"] = run_counts(root)
        from factorio_agent.bottleneck_audit import run_idle

        row["idle_with_work"] = run_idle(root)  # idle watchdog seconds and gates (bottleneck_audit)
        tick = run_tick(root)
        row["game_minutes"] = round(tick / TICKS_PER_MINUTE, 1) if tick is not None else None
        timeline = cp_tool.Run(root)
        reached, far = milestones(timeline.jobs, timeline.end, ROADMAP, _same)
        row["milestones"] = reached
        row["layouts"] = run_layouts(root, milestones=reached)
        row["furthest"] = dict(step=far[1], minute=far[2], index=far[0]) if far else None
        row["blocked_reasons"] = blocked_rows(timeline.jobs)
        row["blocked_now"] = blocked_rows(timeline.jobs, statuses=("blocked",))
        try:  # the minute the run's own iron supply could have paid each step (waste_report)
            import waste_report

            row["plate_bound"] = waste_report.plate_supply_bound(root, timeline)
        except Exception:  # a report column never breaks the report
            row["plate_bound"] = {}
        summary = report_run.report(root, jobs=timeline.jobs)
        row.update(
            controller_errors=summary.get("controller_errors") or {},
            wall_minutes=summary.get("wall_minutes"),
            actions=summary.get("actions"),
            query_load=summary.get("query_load"),
            **{key: summary.get(key) for key in STALL_KEYS},
            stalled=bool(summary.get("stalled")),
            tidy=((summary.get("tidiness") or {}).get("totals") or {}).get("outside_share"),
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
            self.release_expired(fleet["name"], out=out)
            fleet = self.load(fleet["name"])
        runs = [self.run_report(run, lo, hi) for run in fleet["runs"]]
        return fleet, runs, aggregate(runs)


# ---------------------------------------------------------------- CLI


def _status_lines(value):
    lines = [
        f"fleet {value['name']} commit {value.get('commit') or '?'}"
        f" (target {'+' if value.get('branch') else ''}{value.get('target_minutes') or '-'} game min)"
        + branch_note(value["runs"])
    ]
    if any(r.get("test_mode") for r in value["runs"]):
        lines[0] += " TEST MODE client_free (C: n = no client by design, d = detached)"
    if any(r.get("respawn_emulation") for r in value["runs"]):
        lines[0] += " | respawn emulation on"
    if value.get("explore"):
        lines[0] += " | layout EXPLORATION on"
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
    start.add_argument(
        "--minutes",
        type=float,
        help="stop each run after this many game minutes (with --from: game minutes after the checkpoint)",
    )
    start.add_argument(
        "--from",
        dest="branch_from",
        metavar="FLEET:SEED[,FLEET:SEED...]",
        help="branch: continue stopped runs from their saves with the current code (new folders, fresh ports;"
        " TEST runs, never proof; --count/--seeds/--client/--explore/--respawn do not apply)",
    )
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
        default=actor_client.NONE,
        help="none (default): client-free TEST run (lab trigger script-credited after the real craft, map charted"
        " around the actor as a player would); always: a hidden client per run (proof runs); until_lab_trigger",
    )
    start.add_argument("--keep-client", action="store_true", help="same as --client always")
    start.add_argument(
        "--explore",
        action="store_true",
        help="explore layout candidates: each run writes layout-settings.json {explore: true}; a seed from"
        " (world id, block) picks among the best candidates within the margin (recorded in the manifest)",
    )
    start.add_argument(
        "--keep-problem",
        type=int,
        default=KEEP_PROBLEM,
        help="the N worst problem runs stay paused with their server up after the round (default 1, ~0.45 GB each)",
    )
    start.add_argument("--keep-timeout", type=float, default=KEEP_TIMEOUT_MINUTES, help="minutes a kept run stays up")
    start.add_argument("--no-postmortem", action="store_true", help="no triage snapshot of problem runs at the stop")
    start.add_argument(
        "--stall-minutes",
        type=float,
        help="real minutes without game-time progress before the watcher ends a run (default: from the fleet's pace)",
    )
    start.add_argument(
        "--respawn",
        action="store_true",
        help="client-free runs: emulate the player respawn after an actor death (off by default; owner approval"
        " pending, docs/v2/HEADLESS.md)",
    )
    for command in ("inspect", "revive", "postmortem"):
        one = sub.add_parser(command)
        one.add_argument("--name", required=True)
        one.add_argument("--seed", required=True)
        if command == "inspect":
            one.add_argument("--gui", action="store_true", help="open a copy of the save in a visible client")
        if command == "revive":
            one.add_argument("--keep-timeout", type=float, help="minutes the revived server stays up")
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
        fleet.keep_problem = max(0, args.keep_problem)
        fleet.keep_timeout = args.keep_timeout
        fleet.postmortem = not args.no_postmortem
        fleet.stall_minutes = args.stall_minutes
        fleet.respawn = args.respawn
        if args.branch_from:
            if (
                args.count is not None
                or args.seeds != "random"
                or args.client != actor_client.NONE
                or args.keep_client
                or args.explore
                or args.respawn
            ):
                parser.error("--from keeps each source's seed and settings: drop --count/--seeds/--client/--explore")
            value = fleet.branch(
                args.branch_from,
                args.minutes,
                args.name,
                force=args.force,
                parallel=args.parallel,
                stagger=args.stagger,
            )
            print(f"fleet {value['name']} (branch): manifest {fleet.manifest_path(value['name'])}")
            for run in value["runs"]:
                print(f"  {run['seed']}  {run['status']}  {run['runtime']}")
            return 0 if all(r["status"] == "running" for r in value["runs"]) else 1
        value = fleet.start(
            args.count,
            args.seeds,
            args.minutes,
            args.name,
            force=args.force,
            parallel=args.parallel,
            stagger=args.stagger,
            explore=explore_requested(args.explore),
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
    if args.command == "inspect":
        print("\n".join(fleet.inspect_lines(args.name, args.seed)))
        if args.gui:
            fleet.open_gui(args.name, args.seed)
        return 0
    if args.command == "revive":
        fleet.revive(args.name, args.seed, timeout_minutes=args.keep_timeout)
        return 0
    if args.command == "postmortem":
        return 0 if fleet.postmortem_run(args.name, args.seed).get("folder") else 1
    if args.command == "cost":
        print(json.dumps(dict(machine=machine(), **fleet.cost()), indent=1))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
