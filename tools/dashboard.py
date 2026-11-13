"""Local read-only web dashboard for one v2 runtime and the fleet runs.

Usage: python -B tools/dashboard.py [--runtime runtime/v2/<name>] [--base runtime] [--port 8765] [--host 0.0.0.0]

Tab "병렬 실행" polls ``/api/fleets``: every ``<base>/fleets/*.json`` manifest
with per-run milestones (``fleet.milestones`` over job-events read
incrementally), blocked-reason histograms (``fleet.aggregate``) and the
round-to-round trend of name series such as ``auto-*``. ``/api/state`` and
``/api/layout.svg`` take ``?runtime=<folder>`` (a strict folder name under
``<base>/v2``) to show any run in the first tab.

Serves http://127.0.0.1:<port>/ (loopback only). The page polls
``/api/state`` every 5 s and fetches ``/api/layout.svg`` only when the site
files changed. Everything comes from files in the runtime folder: no RCON, no
locks taken, nothing written. JSONL logs are read incrementally (only the new
bytes per refresh) and a JSON file that is missing or half-written keeps its
last good value. Metrics come from ``tools/report_run.report``; the layout is
``site.planrender.render_dashboard_svg``. See docs/v2/README.md (Dashboard).
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import statistics
import sys
import threading
import time
from urllib.parse import parse_qs

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet as fleet_tool  # noqa: E402
import report_run  # noqa: E402
from factorio_agent import actor_client, progression  # noqa: E402
from factorio_agent.site.masterplan import MasterPlan  # noqa: E402
from factorio_agent.site.planrender import (  # noqa: E402
    ALLOCATION_COLOUR,
    BOARD_COLOUR,
    INPUT_COLOUR,
    OUTPUT_COLOUR,
    RAIL_COLOUR,
    START_COLOUR,
    board_rect,
    dashboard_focus,
    lane_colour,
    render_dashboard_svg,
)
from factorio_agent.site.render import COLOURS  # noqa: E402
from factorio_agent.site.worldmap import WorldMap  # noqa: E402

PAGE = Path(__file__).resolve().parent / "dashboard.html"
SCALE = 2
RECENT_ACTIONS = 40
# A supervisor that logged nothing for this long counts as not alive when its process cannot be checked.
FRESH_SECONDS = 120.0
TERMINAL = {"succeeded", "blocked", "failed"}
OPEN_NODE = {"pending", "ready", "running", "waiting"}
STATUS_LABEL = {
    "succeeded": "완료",
    "completed": "완료",
    "running": "진행 중",
    "waiting": "대기 중",
    "pending": "대기",
    "ready": "준비됨",
    "paused": "일시정지",
    "blocked": "막힘",
    "failed": "실패",
    "queued": "대기열",
    "active": "진행 중",
    "done": "완료",
}


def _text(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class JsonFile:
    """A JSON file re-read only when its mtime/size change; missing or corrupt keeps the last good value."""

    def __init__(self, path):
        self.path = Path(path)
        self.signature = None
        self.value = None
        self.error = None

    def get(self):
        try:
            stat = self.path.stat()
        except OSError:
            self.error = "missing"
            return self.value
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != self.signature:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                self.error = "unreadable"
                return self.value
            self.value, self.signature, self.error = value, signature, None
        return self.value

    def dict(self):
        value = self.get()
        return value if isinstance(value, dict) else {}


class JsonlTail:
    """Rows of an append-only JSONL file, parsing only the bytes added since the last call.

    A file that shrank (rotated or replaced) is read again from the start.
    A trailing line without a newline is kept until it is complete JSON.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.partial = b""
        self.rows: list[dict] = []
        self.changes = 0

    def update(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return self.rows
        if size < self.offset:
            self.offset, self.partial = 0, b""
            self._reset()
            self.changes += 1
        if size == self.offset:
            return self.rows
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(size - self.offset)
        except OSError:
            return self.rows
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        if self.partial and report_run.parse_row(self.partial.decode("utf-8", "replace")) is not None:
            lines.append(self.partial)
            self.partial = b""
        for line in lines:
            row = report_run.parse_row(line.decode("utf-8", "replace"))
            if row is not None:
                self._add(row)
        self.changes += 1
        return self.rows

    # Subclasses that keep a digest instead of every row override these two.
    def _reset(self):
        self.rows = []

    def _add(self, row):
        self.rows.append(row)


def process_alive(pid, creation_time):
    """True/False for an exact (pid, creation time) process on Windows; None when it cannot be checked."""
    if type(pid) is not int or type(creation_time) is not int or os.name != "nt":
        return None
    from factorio_agent.core.deterministic_character import _identity

    return _identity(pid) == creation_time


def summarize(value, depth=0):
    """A small JSON-able digest of an evidence payload: scalars, short lists, three levels deep."""
    if isinstance(value, dict):
        if depth >= 3:
            return f"{{{len(value)} keys}}"
        out = {}
        for key, item in value.items():
            if len(out) >= 10:
                out["…"] = f"+{len(value) - 10} keys"
                break
            out[str(key)] = summarize(item, depth + 1)
        return out
    if isinstance(value, list):
        if depth >= 3 or len(value) > 6:
            return f"[{len(value)} items]"
        return [summarize(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _text(value, 120)
    return value


def default_runtime(repo=REPO, base=None) -> Path:
    """The runtime ``lifecycle status`` uses: the ``runtime`` of ``<base>/active-runtime.json`` (base: repo/runtime)."""
    pointer = Path(base if base is not None else repo / "runtime") / "active-runtime.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8-sig")).get("runtime")
    except (OSError, ValueError, AttributeError):
        value = None
    if not value:
        raise SystemExit(f"no active runtime pointer at {pointer}; pass --runtime runtime/v2/<name>")
    return Path(value)


class Dashboard:
    """Builds the page payload and the layout SVG from one runtime folder (read-only)."""

    def __init__(self, root, *, clock=time.time, alive=process_alive):
        self.root = Path(root).resolve()
        self.clock = clock
        self.alive = alive
        self.lock = threading.Lock()
        files = {
            "run": "run.json",
            "world": "world-settings.json",
            "jobs": "jobs.json",
            "queue": "work_queue.json",
            "board": "pcb-board.json",
            "plan": "site-plan.json",
            "map": "site-map.json",
            "state": "logs/controller-state.json",
            "process": "supervisor-process.json",
            "pause": "lifecycle-pause.json",
        }
        self.files = {key: JsonFile(self.root / name) for key, name in files.items()}
        self.job_events = JsonlTail(self.root / "job-events.jsonl")
        self.controller = JsonlTail(self.root / "logs" / "controller-events.jsonl")
        self._report = None
        self._report_key = None
        self._layout = {"version": None, "svg": None, "info": None, "key": None}

    # ----- inputs ----------------------------------------------------------
    def _jobs(self) -> dict:
        jobs = self.files["jobs"].dict().get("jobs")
        return {k: v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}

    def _nodes(self) -> list[dict]:
        nodes = self.files["queue"].dict().get("nodes")
        return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []

    def _metrics(self, jobs, job_events, controller) -> dict:
        key = (self.job_events.changes, self.controller.changes, self.files["jobs"].signature)
        if key != self._report_key or self._report is None:
            try:
                self._report = report_run.report(self.root, job_events=job_events, controller=controller, jobs=jobs)
                self._report_key = key
            except Exception as error:  # a half-shaped file must not break the page
                if self._report is None:
                    self._report = {"report_error": _text(repr(error))}
        return self._report

    # ----- payload ---------------------------------------------------------
    def payload(self) -> dict:
        with self.lock:
            now = self.clock()
            jobs = self._jobs()
            nodes = self._nodes()
            job_events = self.job_events.update()
            controller = self.controller.update()
            metrics = self._metrics(jobs, job_events, controller)
            capability = {key: job.get("capability") for key, job in jobs.items()}
            layout = self._layout_info()
            return {
                "generated_at": now,
                "header": self._header(now, metrics, job_events, controller),
                "now": self._now(now, jobs, nodes, controller),
                "done": self._done(jobs, nodes, metrics),
                "activity": self._activity(now, job_events, controller, capability, metrics),
                "layout": layout,
                # Missing files are normal early in a run; only unreadable ones are reported.
                "files": {k: f.error for k, f in self.files.items() if f.error == "unreadable"},
            }

    def _header(self, now, metrics, job_events, controller) -> dict:
        run = self.files["run"].dict()
        state = self.files["state"].dict()
        world = self.files["world"].dict().get("world_id")
        last_success = next(
            (
                r["time"]
                for r in reversed(job_events)
                if r.get("event") == "terminal" and r.get("status") == "succeeded"
            ),
            None,
        )
        return {
            "runtime": self.root.name,
            "path": str(self.root),
            "seed": run.get("seed"),
            "world_id": world,
            "wall_minutes": metrics.get("wall_minutes"),
            "game_minutes": metrics.get("game_minutes"),
            "planner_mode": metrics.get("planner_mode") or state.get("planner"),
            "test_mode": metrics.get("test_mode") or run.get("test_mode"),
            "jobs_succeeded": metrics.get("jobs_succeeded"),
            "idle_gap_share": metrics.get("idle_gap_share"),
            "minutes_since_last_success": round((now - last_success) / 60, 1) if last_success else None,
            "supervisor": self._supervisor(now, state, controller),
            "game_pause": self.files["pause"].dict().get("phase"),
            "report_error": metrics.get("report_error"),
        }

    def _supervisor(self, now, state, controller) -> dict:
        record = self.files["process"].dict()
        candidates = [
            (state.get("pid"), state.get("native_creation_time")),
            (record.get("pid"), record.get("creation_time")),
        ]
        checks = [self.alive(pid, created) for pid, created in candidates if pid is not None]
        alive_pid = next((pid for (pid, _), ok in zip(candidates, checks) if ok), None)
        last_event = controller[-1]["time"] if controller else None
        updated = _num(state.get("updated_at"))
        latest = max([t for t in (last_event, updated) if t is not None], default=None)
        age = round(now - latest, 1) if latest is not None else None
        if any(c is True for c in checks):
            alive, basis = True, f"프로세스 확인 (pid {alive_pid})"
        elif checks and all(c is False for c in checks):
            alive, basis = False, "프로세스 없음"
        else:
            alive = age is not None and age < FRESH_SECONDS
            basis = "최근 이벤트 기준" if age is not None else "기록 없음"
        return {
            "alive": alive,
            "basis": basis,
            "status": state.get("status"),
            "last_event_seconds_ago": age,
            "last_error": _text(state.get("last_error"), 200) if state.get("last_error") else None,
        }

    def _now(self, now, jobs, nodes, controller) -> dict:
        state = self.files["state"].dict()
        active_keys = set(state.get("active_job_keys") or [])
        if state.get("active_job_key"):
            active_keys.add(state["active_job_key"])
        rows = []
        for key, job in jobs.items():
            status = job.get("status")
            if status == "succeeded":
                continue
            created = _num(job.get("created_at"))
            progress = _num(job.get("progress_at_time"))
            row = {
                "key": key,
                "short": key[-8:],
                "capability": job.get("capability"),
                "status": status,
                "status_label": STATUS_LABEL.get(status, status),
                "reason": _text(job.get("reason") or job.get("slice_yield_reason") or "", 300),
                "actions": job.get("actions", 0),
                "failures": job.get("failures", 0),
                "age_minutes": round((now - created) / 60, 1) if created else None,
                "minutes_without_progress": round((now - progress) / 60, 1) if progress else None,
                "selected": key in active_keys,
                "priority": job.get("priority"),
            }
            if status in ("blocked", "failed"):
                evidence = job.get("evidence")
                row["evidence"] = _text(summarize(evidence), 600) if evidence else None
                row["last_failure"] = _text(job.get("last_failure"), 300) if job.get("last_failure") else None
            rows.append(row)
        order = {"running": 0, "waiting": 1, "pending": 2, "paused": 3, "blocked": 4, "failed": 5}
        rows.sort(key=lambda r: (order.get(r["status"], 9), r["priority"] if _num(r["priority"]) is not None else 0))
        queue = [
            {
                "id": node.get("id"),
                "capability": node.get("capability"),
                "status": node.get("status"),
                "status_label": STATUS_LABEL.get(node.get("status"), node.get("status")),
                "priority": node.get("priority"),
                "reason": _text(node.get("reason") or "", 200),
                "last_reason": _text(node.get("last_reason") or "", 200),
            }
            for node in nodes
            if node.get("status") != "completed"
        ]
        queue.sort(key=lambda n: n["priority"] if _num(n["priority"]) is not None else 0)
        autopilot = [
            {
                "minutes_ago": round((now - r["time"]) / 60, 1),
                "event": r.get("event"),
                "tasks": r.get("task_ids")
                or [t for t in (r.get("task_id"), r.get("capability")) if isinstance(t, str)][:1],
                "reason": _text(r.get("reason") or r.get("blocked_reason") or "", 160),
            }
            for r in controller[-4000:]
            if r.get("event") in ("autopilot", "dispatch_deferred", "auto_retry")
        ][-8:][::-1]
        return {
            "jobs": rows,
            "queue": queue,
            "autopilot": autopilot,
            "queue_completed": sum(n.get("status") == "completed" for n in nodes),
        }

    def _done(self, jobs, nodes, metrics) -> dict:
        jobs_list = list(jobs.values())

        def matching(step):
            return [j for j in jobs_list if progression._same(j, step)]

        satisfied = {}
        for step in progression.ROADMAP:
            satisfied[step.id] = any(j.get("status") == "succeeded" for j in matching(step))
        steps = []
        for index, step in enumerate(progression.ROADMAP):
            found = sorted(matching(step), key=lambda j: _num(j.get("created_at")) or 0)
            latest = found[-1] if found else None
            queued = any(
                n.get("status") in OPEN_NODE
                and (n.get("id") == progression.NODE_PREFIX + step.id or progression._same(n, step))
                for n in nodes
            )
            if satisfied[step.id]:
                state = "done"
            elif any(j.get("status") not in TERMINAL for j in found):
                state = "active"
            elif queued:
                state = "queued"
            elif any(j.get("status") in ("blocked", "failed") for j in found):
                state = "blocked"
            elif all(satisfied.get(name, False) for name in step.after):
                state = "ready"
            else:
                state = "pending"
            steps.append(
                {
                    "index": index + 1,
                    "id": step.id,
                    "capability": step.capability,
                    "state": state,
                    "state_label": STATUS_LABEL.get(state, "대기") if state != "pending" else "미착수",
                    "reason": _text(latest.get("reason") or "", 160) if latest and state != "done" else "",
                    "after": list(step.after),
                }
            )
        done = sum(s["state"] == "done" for s in steps)
        return {
            "milestones": metrics.get("milestones", []),
            "roadmap": steps,
            "roadmap_done": done,
            "roadmap_total": len(steps),
            "next_step": next((s["id"] for s in steps if s["state"] != "done"), None),
        }

    def _activity(self, now, job_events, controller, capability, metrics) -> dict:
        window = job_events[-(RECENT_ACTIONS * 8) :]
        recent = []
        open_intents = {}
        for row in window:
            if row.get("event") == "action_intent":
                action = row.get("action") if isinstance(row.get("action"), dict) else {}
                entry = {
                    "minutes_ago": round((now - row["time"]) / 60, 1),
                    "job": capability.get(row.get("key")) or row.get("key"),
                    "type": action.get("type"),
                    "item": action.get("item")
                    or action.get("name")
                    or action.get("technology")
                    or action.get("recipe"),
                    "count": action.get("count"),
                    "reason": _text(action.get("reason") or "", 100),
                    "result": None,
                }
                recent.append(entry)
                open_intents[row.get("key")] = entry
            elif row.get("event") == "action_result" and row.get("key") in open_intents:
                result = row.get("result") if isinstance(row.get("result"), dict) else {}
                open_intents.pop(row.get("key"))["result"] = result.get("status") or (
                    "ok" if result.get("ok") else None
                )
        state = self.files["state"].dict()
        incidents = []
        for item in state.get("open_incidents") or []:
            if not isinstance(item, dict):
                continue
            since = _num(item.get("since"))
            incidents.append(
                {
                    "kind": item.get("kind"),
                    "subject": capability.get(item.get("subject")) or item.get("subject"),
                    "minutes_open": round((now - since) / 60, 1) if since else None,
                }
            )
        errors = [
            {"minutes_ago": round((now - r["time"]) / 60, 1), "reason": _text(r.get("reason") or "", 220)}
            for r in controller[-4000:]
            if r.get("event") == "error"
        ][-3:][::-1]
        return {
            "recent": recent[-RECENT_ACTIONS:][::-1],
            "repeated": metrics.get("recent_100_actions_top", []),
            "repeat_share": metrics.get("recent_repeat_share"),
            "incidents": incidents,
            "incidents_by_kind": metrics.get("incidents_by_kind", {}),
            "errors": errors,
            "error_counts": metrics.get("controller_errors", {}),
        }

    # ----- layout ----------------------------------------------------------
    def _layout_info(self) -> dict:
        plan_file, map_file, board_file = self.files["plan"], self.files["map"], self.files["board"]
        stored_plan, stored_map, board = plan_file.get(), map_file.get(), board_file.dict()
        world = self.files["world"].dict().get("world_id")
        key = (plan_file.signature, map_file.signature, board_file.signature, world)
        if key == self._layout["key"]:
            return self._layout["info"]
        if not isinstance(stored_plan, dict) or not isinstance(stored_map, dict):
            info = {
                "available": False,
                "message": "마스터 플랜이 아직 없습니다 (site-plan.json / site-map.json 없음). "
                "첫 사이트 할당 때 SiteService가 만듭니다.",
            }
            self._layout.update(key=key, info=info)
            return info
        warnings = []
        if world and stored_plan.get("world_id") != world:
            warnings.append("site-plan.json의 world_id가 현재 월드와 다릅니다 (이전 맵의 계획).")
        blocks = board.get("blocks") if isinstance(board.get("blocks"), dict) else {}
        if board and board.get("world_id") not in (None, stored_plan.get("world_id")):
            warnings.append("pcb-board.json이 다른 월드의 것이라 블록을 그리지 않았습니다.")
            blocks = {}
        try:
            worldmap = WorldMap.from_json(json.dumps(stored_map["map"]))
            plan = MasterPlan.from_dict(stored_plan["plan"])
            svg = render_dashboard_svg(worldmap, plan, blocks, scale=SCALE)
            focus = dashboard_focus(worldmap, plan, blocks, scale=SCALE)
        except Exception as error:  # keep the last good drawing
            info = dict(self._layout["info"] or {"available": False}, error=_text(repr(error), 300))
            if not info.get("available"):
                info["message"] = "레이아웃 파일을 읽지 못했습니다: " + info["error"]
            self._layout.update(key=key, info=info)
            return info
        version = hashlib.sha1(repr(key).encode()).hexdigest()[:12]
        bounds = worldmap.bounds()
        info = {
            "available": True,
            "version": version,
            "focus": focus,
            "full": [0, 0, bounds.width * SCALE, bounds.height * SCALE] if bounds else focus,
            "scale": SCALE,
            "origin": [bounds.x0, bounds.y0] if bounds else [0, 0],
            "warnings": warnings + list(plan.warnings),
            "bus": {
                "direction": plan.bus.direction,
                "length": plan.bus.length,
                "width": plan.bus.width,
                "lanes": [
                    {"material": m, "count": n, "colour": lane_colour(m)}
                    for m, n in Counter(lane.material for lane in plan.bus.lanes if lane.material).items()
                ],
                "spare": sum(1 for lane in plan.bus.lanes if not lane.material),
            },
            "zones": [{"name": z.name, "kind": z.kind, "rect": z.rect.to_list()} for z in plan.zones],
            "allocations": [{"name": a.name, "kind": a.kind, "rect": a.rect.to_list()} for a in plan.allocations],
            "blocks": [
                {
                    "name": name,
                    "template": b.get("template") or b.get("layout_policy"),
                    "rect": board_rect(b).to_list() if board_rect(b) else None,
                    "entities": len(b.get("entities") or []),
                    "items": sum(v for v in (b.get("required_items") or {}).values() if _num(v)),
                }
                for name, b in sorted(blocks.items())
                if isinstance(b, dict)
            ],
            "legend": [
                {"label": "블록 (보드에 등록됨)", "colour": BOARD_COLOUR, "shape": "fill"},
                {"label": "할당 (계획)", "colour": ALLOCATION_COLOUR, "shape": "dash"},
                {"label": "입력 변", "colour": INPUT_COLOUR, "shape": "line"},
                {"label": "출력 변", "colour": OUTPUT_COLOUR, "shape": "line"},
                {"label": "철도 예약", "colour": RAIL_COLOUR, "shape": "dash"},
                {"label": "물", "colour": COLOURS["water"], "shape": "fill"},
                {"label": "철광석", "colour": COLOURS["iron-ore"], "shape": "fill"},
                {"label": "구리광석", "colour": COLOURS["copper-ore"], "shape": "fill"},
                {"label": "석탄", "colour": COLOURS["coal"], "shape": "fill"},
                {"label": "돌", "colour": COLOURS["stone"], "shape": "fill"},
                {"label": "나무", "colour": COLOURS["tree"], "shape": "fill"},
                {"label": "시작점", "colour": START_COLOUR, "shape": "dot"},
            ],
        }
        self._layout.update(key=key, info=info, svg=svg, version=version)
        return info

    def layout_svg(self) -> str | None:
        with self.lock:
            self._layout_info()
            return self._layout["svg"]


# ----- fleets (tab "병렬 실행") ------------------------------------------------
FLEET_COLUMNS = (*fleet_tool.COLUMNS, ("green", "logistic_science"))
COLUMN_LABEL = {
    "power": "전력",
    "auto": "자동화 연구",
    "emd": "전기 채굴기 연구",
    "logi": "물류",
    "iron": "철 전초기지",
    "copr": "구리 전초기지",
    "coal": "석탄 전초기지",
    "smelt": "제련 구역",
    "bus": "메인 버스",
    "red": "빨강 과학",
    "green": "초록 과학",
}
RECENT_FLEETS = 5
TOP_REASONS = 10
# A runtime folder name: no separators, no "..", no leading/trailing dot (Windows strips those).
RUNTIME_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,118}[A-Za-z0-9])?")
# Rounds of one automatic loop share a prefix: auto-1113-1618, auto-1113-1708 -> "auto".
SERIES = re.compile(r"([A-Za-z][A-Za-z_]*)-\d[\d-]*")
COMMIT = re.compile(r"\bcommit\s+([0-9a-f]{6,40})\b")
PROCESS_FILES = ("server.json", "supervisor-process.json", "agent-client/client-process.json", "actor-client.json")


def runtime_folder(base, name) -> Path:
    """``base/v2/<name>`` for a strict folder name; ValueError for a bad name, LookupError when absent."""
    if not isinstance(name, str) or not RUNTIME_NAME.fullmatch(name) or ".." in name:
        raise ValueError("bad runtime name")
    v2 = (Path(base) / "v2").resolve()
    root = (v2 / name).resolve()
    if root.parent != v2 or root.name != name or not root.is_dir():
        raise LookupError("no runtime folder " + name)
    return root


def _signature(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


class TerminalTail(JsonlTail):
    """``job-events.jsonl`` digested incrementally to ``critical_path.Run.end``: key -> (tick, status, reason).

    A terminal row without a tick takes its evidence tick, else the tick of the last earlier row that had one."""

    def __init__(self, path):
        super().__init__(path)
        self.end = {}
        self.tick = None

    def _reset(self):
        self.rows, self.end, self.tick = [], {}, None

    def _add(self, row):
        tick = row.get("tick")
        if type(tick) is int:
            self.tick = tick
        if row.get("event") != "terminal":
            return
        if type(tick) is not int:
            evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
            tick = evidence["tick"] if type(evidence.get("tick")) is int else self.tick
        if type(tick) is int:
            self.end[row.get("key")] = (tick, row.get("status"), str(row.get("reason") or ""))


class TextHead:
    """The first few KB of a text file, re-read only when it changes; None when missing."""

    def __init__(self, path, size=2048):
        self.path, self.size, self.signature, self.value = Path(path), size, None, None

    def get(self):
        signature = _signature(self.path)
        if signature != self.signature:
            try:
                with self.path.open("rb") as handle:
                    self.value = handle.read(self.size).decode("utf-8", "replace")
            except OSError:
                self.value = None
            self.signature = signature
        return self.value if self.signature else None


class RunCard:
    """One fleet run's summary from its folder, recomputed only when jobs, controller state or job events change."""

    def __init__(self, root, alive):
        self.root = Path(root)
        self.alive = alive
        self.jobs = JsonFile(self.root / "jobs.json")
        self.state = JsonFile(self.root / "logs" / "controller-state.json")
        self.ends = TerminalTail(self.root / "job-events.jsonl")
        self._key = self._progress = self._live_key = self._live = None

    def liveness(self) -> dict:
        key = tuple(_signature(self.root / name) for name in PROCESS_FILES)
        # A dead process does not come back without a new registry file; a live one is checked every time.
        if key == self._live_key and self._live is not None and not any(self._live["up"].values()):
            return self._live
        try:
            records = fleet_tool.run_records(self.root)
            up = {name: bool(record) and bool(self.alive(record)) for name, record in records.items()}
            client = actor_client.client_state(self.root, up.get("client", False))
            label = actor_client.test_mode(self.root) or {}
        except Exception:  # a half-written registry must not break the page
            up, client, label = {"server": False, "client": False, "supervisor": False}, "down", {}
        self._live = {"up": up, "client": client, "test_mode": label.get("test_mode")}
        self._live_key = key
        return self._live

    def progress(self) -> dict:
        value = self.jobs.dict().get("jobs")
        jobs = {k: v for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}
        state = self.state.dict()
        self.ends.update()
        key = (self.jobs.signature, self.state.signature, self.ends.changes)
        if key == self._key and self._progress is not None:
            return self._progress
        try:
            reached, far = fleet_tool.milestones(jobs, self.ends.end, progression.ROADMAP, progression._same)
            counts = fleet_tool.job_summary(jobs, state)
        except Exception as error:  # odd shapes keep the last good summary
            if self._progress is None:
                self._progress = {"milestones": {}, "blocked_reasons": [], "error": _text(repr(error), 120)}
            return self._progress
        stuck = [j for j in jobs.values() if j.get("status") in ("blocked", "failed")]
        latest = max(stuck, key=lambda j: _num(j.get("last_tick")) or _num(j.get("created_at")) or 0, default=None)
        self._progress = {
            "game_minutes": counts["game_minutes"],
            "active": counts["active"],
            "blocked": len(stuck),
            "succeeded": counts["succeeded"],
            "milestones": reached,
            "furthest": {"step": far[1], "minute": far[2], "index": far[0]} if far else None,
            "blocked_reasons": fleet_tool.blocked_rows(jobs),
            "blocked_now": {"capability": latest.get("capability"), "reason": _text(latest.get("reason") or "?", 90)}
            if latest
            else None,
            "has_green": any(j.get("capability") == "logistic_science" for j in jobs.values()),
        }
        self._key = key
        return self._progress


def _histogram(rows, label=None):
    """``fleet.aggregate``'s blocked/failed reason histogram as a list (reason -> count -> example)."""
    agg = fleet_tool.aggregate(rows)
    out = []
    for reason, value in list(agg["blocked_reasons"].items())[:TOP_REASONS]:
        example = value["seeds"][0] if value["seeds"] else None
        out.append(
            {
                "reason": reason,
                "count": value["count"],
                "capability": value.get("capability"),
                "runs": len(value["seeds"]),
                "example": example,
                "example_runtime": (label or {}).get(example, example if label is None else None),
            }
        )
    return out


class FleetBoard:
    """Every fleet manifest under ``<base>/fleets`` with per-run progress (read-only, cached per file)."""

    def __init__(self, base, *, clock=time.time, alive=None):
        self.base = Path(base).resolve()
        self.dir = self.base / "fleets"
        self.clock = clock
        self.alive = alive or fleet_tool.alive
        self.lock = threading.Lock()
        self.manifests: dict[Path, JsonFile] = {}
        self.heads: dict[Path, TextHead] = {}
        self.cards: dict[str, RunCard] = {}

    def _manifests(self):
        try:
            paths = [p for p in self.dir.glob("*.json") if p.name != "cost.json"]
        except OSError:
            paths = []
        out = []
        for path in paths:
            value = self.manifests.setdefault(path, JsonFile(path)).get()
            if (
                isinstance(value, dict)
                and value.get("kind") == "factorio-fleet-v1"
                and isinstance(value.get("name"), str)
            ):
                out.append(value)
        return out

    def _head(self, path):
        return self.heads.setdefault(path, TextHead(path)).get()

    def _commit(self, manifest):
        if isinstance(manifest.get("commit"), str) and manifest["commit"].strip():
            return manifest["commit"].strip()[:12]
        for suffix in (".report.txt", ".start.txt"):
            match = COMMIT.search(self._head(self.dir / "reports" / (manifest["name"] + suffix)) or "")
            if match:
                return match.group(1)[:12]
        return None

    def _run(self, run):
        seed = run.get("seed")
        name = Path(str(run.get("runtime") or "")).name
        row = {
            "seed": seed,
            "runtime": name or None,
            "link": False,
            "status": run.get("status"),
            "launch_error": _text(run["launch_error"], 120) if run.get("launch_error") else None,
            "server": False,
            "supervisor": False,
            "client": "down",
            "alive": False,
            "test_mode": None,
            "game_minutes": None,
            "milestones": {},
            "furthest": None,
            "active": None,
            "blocked": None,
            "blocked_now": None,
            "blocked_reasons": [],
            "has_green": False,
        }
        try:
            root = runtime_folder(self.base, name)
        except (ValueError, LookupError):
            return row  # only folders under <base>/v2 are read (and linkable)
        card = self.cards.get(str(root))
        if card is None:
            card = self.cards[str(root)] = RunCard(root, self.alive)
        live, progress = card.liveness(), card.progress()
        up = live["up"]
        row.update(
            link=True,
            server=up.get("server", False),
            supervisor=up.get("supervisor", False),
            client=live["client"],
            alive=bool(up.get("server") and up.get("supervisor")),
            test_mode=live["test_mode"],
            **{k: progress.get(k) for k in ("game_minutes", "furthest", "active", "blocked", "blocked_now")},
            milestones=progress.get("milestones") or {},
            blocked_reasons=progress.get("blocked_reasons") or [],
            has_green=progress.get("has_green", False),
        )
        return row

    def _fleet(self, manifest):
        runs = [self._run(r) for r in manifest.get("runs") or [] if isinstance(r, dict)]
        with_data = [r for r in runs if r["link"]]
        green = any(r["has_green"] or "logistic_science" in r["milestones"] for r in runs)
        columns = [(h, s) for h, s in FLEET_COLUMNS if h != "green" or green]
        stats = {}
        for head, step in columns:
            value = fleet_tool.stats(r["milestones"].get(step) for r in with_data)
            stats[head] = dict(value or {"min": None, "median": None, "max": None, "n": 0})
        running = sum(r["alive"] for r in runs)
        tests = sum(bool(r["test_mode"]) for r in runs)
        failed = sum(r["status"] == "launch_failed" for r in runs)
        blocked_runs = sum(bool(r["blocked"]) for r in runs)
        lost = sum(r["status"] == "running" and not r["alive"] for r in runs)
        histogram = _histogram(runs, {r["seed"]: r["runtime"] for r in runs if r["link"]})
        indices = [r["furthest"]["index"] for r in with_data if r["furthest"]]
        median_step = progression.ROADMAP[int(statistics.median_low(indices))].id if indices else None
        health = [f"실행 중 {running}/{len(runs)}"]
        if median_step:
            health.append(f"중간 진행 {median_step}")
        if blocked_runs:
            health.append(f"막힌 런 {blocked_runs}" + (f" (최다: {histogram[0]['reason']})" if histogram else ""))
        if failed:
            health.append(f"시작 실패 {failed}")
        if lost:
            health.append(f"프로세스 없음 {lost} (manifest는 running)")
        level = "bad" if failed or lost else ("warn" if blocked_runs else "ok")
        created = _num(manifest.get("created_at"))
        series = SERIES.fullmatch(manifest["name"])
        return {
            "name": manifest["name"],
            "created_at": created,
            "commit": self._commit(manifest),
            "target_minutes": manifest.get("target_minutes"),
            "planner": manifest.get("planner"),
            "client": manifest.get("client"),
            "test_runs": tests,
            "runs_total": len(runs),
            "running": running,
            "stopped": len(runs) - running,
            "round_report": self._head(self.dir / "reports" / (manifest["name"] + ".report.txt")) is not None,
            "series": series.group(1) if series else None,
            "health": " · ".join(health),
            "health_level": level,
            "columns": [{"head": h, "step": s, "label": COLUMN_LABEL.get(h, h)} for h, s in columns],
            "stats": stats,
            "runs_with_data": len(with_data),
            "blocked": histogram,
            "runs": [{k: v for k, v in r.items() if k not in ("blocked_reasons", "has_green")} for r in runs],
            "_rows": runs,
        }

    def payload(self) -> dict:
        with self.lock:
            fleets = [self._fleet(m) for m in self._manifests()]
            fleets.sort(key=lambda f: (f["created_at"] or 0, f["name"]), reverse=True)
            recent = fleets[:RECENT_FLEETS]
            rows = [dict(r, seed=r["runtime"]) for f in recent for r in f["_rows"] if r["link"]]
            recent_blocked = _histogram(rows)
            trend = self._trend(fleets)
            for f in fleets:
                f.pop("_rows")
            open_name = next((f["name"] for f in fleets if f["running"]), fleets[0]["name"] if fleets else None)
            return {
                "generated_at": self.clock(),
                "base": str(self.base),
                "fleets": fleets,
                "default_open": open_name,
                "recent_fleets": [f["name"] for f in recent],
                "recent_blocked": recent_blocked,
                "trend": trend,
                "columns": [{"head": h, "step": s, "label": COLUMN_LABEL.get(h, h)} for h, s in FLEET_COLUMNS],
            }

    @staticmethod
    def _trend(fleets):
        """Median minute per milestone by round, for every name prefix with two or more fleets."""
        groups: dict[str, list] = {}
        for f in fleets:
            if f["series"]:
                groups.setdefault(f["series"], []).append(f)
        out = []
        for prefix, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            members = sorted(members, key=lambda f: (f["created_at"] or 0, f["name"]))
            heads = [h for h, _ in FLEET_COLUMNS if any(h in f["stats"] for f in members)]
            out.append(
                {
                    "prefix": prefix,
                    "heads": [{"head": h, "label": COLUMN_LABEL.get(h, h)} for h in heads],
                    "rounds": [
                        {
                            "name": f["name"],
                            "created_at": f["created_at"],
                            "commit": f["commit"],
                            "runs": f["runs_with_data"],
                            "running": f["running"],
                            "finished": f["round_report"],
                            "median": {h: (f["stats"].get(h) or {}).get("median") for h in heads},
                            "reached": {h: (f["stats"].get(h) or {}).get("n", 0) for h in heads},
                        }
                        for f in members
                    ],
                }
            )
        return out


class Runtimes:
    """The default runtime's Dashboard plus a few for ``?runtime=<folder>`` under ``<base>/v2``."""

    KEEP = 6

    def __init__(self, default_root, base, follow=False):
        self.default = Dashboard(default_root)
        self.base = Path(base).resolve()
        self.others: dict[Path, Dashboard] = {}
        self.lock = threading.Lock()
        # follow: no --runtime was given, so the default tracks <base>/active-runtime.json
        # (``lifecycle new-map`` moves the pointer; the page then shows the new map).
        self.follow = follow

    def _follow(self):
        try:
            root = default_runtime(base=self.base).resolve()
        except (SystemExit, OSError):
            return
        if root != self.default.root and root.is_dir():
            with self.lock:
                if root != self.default.root:
                    self.default = self.others.pop(root, None) or Dashboard(root)

    def get(self, name=None) -> Dashboard:
        if name is None:
            if self.follow:
                self._follow()
            return self.default
        root = runtime_folder(self.base, name)
        if root == self.default.root:
            return self.default
        with self.lock:
            dash = self.others.pop(root, None) or Dashboard(root)
            self.others[root] = dash  # most recent last
            while len(self.others) > self.KEEP:
                self.others.pop(next(iter(self.others)))
            return dash


class Handler(BaseHTTPRequestHandler):
    runtimes: Runtimes = None  # set by serve()
    fleets: FleetBoard = None

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, kind: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value):
        self._send(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8"
        )

    def _dashboard(self, query):
        """The runtime of ``?runtime=<folder>`` (strict name under <base>/v2), else the default; None after an error reply."""
        names = parse_qs(query, keep_blank_values=True).get("runtime")
        try:
            if names is not None and len(names) != 1:
                raise ValueError("one runtime parameter")
            return self.runtimes.get(names[0] if names else None)
        except ValueError:
            self._send(b"bad runtime name", "text/plain; charset=utf-8", 400)
        except LookupError:
            self._send(b"no such runtime", "text/plain; charset=utf-8", 404)
        return None

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                dash = self._dashboard(query)
                if dash is not None:
                    self._json(dash.payload())
            elif path == "/api/fleets":
                self._json(self.fleets.payload())
            elif path == "/api/layout.svg":
                dash = self._dashboard(query)
                if dash is None:
                    return
                svg = dash.layout_svg()
                if svg is None:
                    self._send(b"no layout", "text/plain; charset=utf-8", 404)
                else:
                    self._send(svg.encode("utf-8"), "image/svg+xml; charset=utf-8")
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(root, port=8765, host="127.0.0.1", base=None, follow=False) -> ThreadingHTTPServer:
    """``root``: the default runtime; ``base``: the runtime base whose ``fleets/`` and ``v2/`` the fleet tab reads."""
    base = Path(base) if base is not None else REPO / "runtime"
    attrs = {"runtimes": Runtimes(root, base, follow=follow), "fleets": FleetBoard(base)}
    handler = type("DashboardHandler", (Handler,), attrs)
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", type=Path, help="runtime folder (default: <base>/active-runtime.json)")
    parser.add_argument("--base", type=Path, default=REPO / "runtime", help="runtime base (fleets/, v2/)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address; 0.0.0.0 serves the read-only page to the LAN"
    )
    args = parser.parse_args(argv)
    base = args.base if args.base.is_absolute() else (Path.cwd() / args.base).resolve()
    root = args.runtime if args.runtime is not None else default_runtime(base=base)
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    if not root.is_dir():
        raise SystemExit(f"runtime folder not found: {root}")
    server = serve(root, args.port, host=args.host, base=base, follow=args.runtime is None)
    print(
        f"dashboard for {root} (fleets of {base}) at http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C stops)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
