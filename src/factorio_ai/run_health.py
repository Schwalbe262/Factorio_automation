"""One-shot health digest of an unattended autonomous run.

Reads the runtime heartbeats, the generated-skill registry/queue, recent LLM
decisions, and (optionally) a lightweight live observation, so an operator can
check "how is the run doing / where is it stuck" at a glance without digging
through logs. Read-only; never mutates anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from .config import AppConfig


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            data = json.loads(path.read_text(encoding=encoding))
            return data if isinstance(data, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    return {}


def _age_seconds(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", text)
    if match and len(match.group(2)) > 6:
        text = f"{match.group(1)}.{match.group(2)[:6]}{match.group(3)}"
    try:
        ts = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round(max(0.0, (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()), 1)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_pid_set(values: Any) -> set[int]:
    if not isinstance(values, list):
        return set()
    result: set[int] = set()
    for value in values:
        pid = _int_or_none(value)
        if pid is not None:
            result.add(pid)
    return result


def _scheduler_from_supervisor(supervisor: dict[str, Any], *, api_error: str | None = None) -> dict[str, Any] | None:
    service = supervisor.get("vllm_service_status") if isinstance(supervisor.get("vllm_service_status"), dict) else {}
    scheduler_llm = supervisor.get("scheduler_llm_status") if isinstance(supervisor.get("scheduler_llm_status"), dict) else {}
    active_services = service.get("active_services") if isinstance(service.get("active_services"), list) else []
    service_ids = [row.get("id") for row in active_services if isinstance(row, dict)]
    ready = bool(service.get("service_ready") or scheduler_llm.get("llm_ready") or service.get("ok") or scheduler_llm.get("ok"))
    if not ready and not service_ids:
        return None
    try:
        expected_services = max(1, int(os.getenv("FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT", "1")))
    except (TypeError, ValueError):
        expected_services = 1
    result = {
        "checked": True,
        "source": "supervisor_heartbeat",
        "vllm_services": len(service_ids) if service_ids else (1 if ready else 0),
        "vllm_service_ids": service_ids,
        "expected_services": expected_services,
        "healthy": ready and (not service_ids or len(service_ids) <= expected_services),
        "heartbeat_age_seconds": _age_seconds(
            service.get("checked_at")
            or scheduler_llm.get("checked_at")
            or (service.get("heartbeat") if isinstance(service.get("heartbeat"), dict) else {}).get("updated_at")
        ),
    }
    if api_error:
        result["api_error"] = api_error
    return result


def gather_run_health(cfg: AppConfig, *, observe: bool = True) -> dict[str, Any]:
    runtime = Path(cfg.runtime_dir)
    autopilot = _read_json(runtime / "autopilot-heartbeat.json")
    live = _read_json(runtime / "live-skill-heartbeat.json")
    foundry = _read_json(runtime / "skill-foundry-loop.json")
    supervisor = _read_json(runtime / "unattended-llm-supervisor.json")
    progress_kpi = _read_json(runtime / "progress-kpi.json")
    supervisor_age = _age_seconds(supervisor.get("updated_at"))
    progress_age = _age_seconds(progress_kpi.get("updated_at"))
    autopilot_heartbeat_age = _age_seconds(autopilot.get("updated_at"))
    live_age = _age_seconds(live.get("updated_at"))
    autopilot_age = autopilot_heartbeat_age
    autopilot_age_source = "autopilot"
    autopilot_pid = _int_or_none(autopilot.get("pid"))
    live_pid = _int_or_none(live.get("pid"))
    supervisor_autopilot_pids = _coerce_pid_set(supervisor.get("autopilot_processes"))
    live_matches_autopilot = (
        live_pid is not None
        and (live_pid == autopilot_pid or live_pid in supervisor_autopilot_pids)
    )
    if live.get("active") and live_matches_autopilot and live_age is not None:
        if autopilot_age is None or live_age < autopilot_age:
            autopilot_age = live_age
            autopilot_age_source = "live_skill"
    warnings: list[dict[str, Any]] = []
    live_stale_reason = None
    if live.get("active") and live_pid is not None and supervisor_autopilot_pids and live_pid not in supervisor_autopilot_pids:
        live_stale_reason = f"live skill pid {live_pid} is not a current autopilot process"
        warnings.append(
            {
                "kind": "stale_live_skill_pid",
                "detail": live_stale_reason,
            }
        )

    registered: list[str] = []
    failed: list[str] = []
    overrides: list[str] = []
    queue: list[Any] = []
    stale_implemented_queue: list[str] = []
    try:
        from . import skill_foundry
        from .skill_registry import IMPLEMENTED_SKILLS

        skills = skill_foundry.load_registry().get("skills") or {}
        if isinstance(skills, dict):
            registered = sorted(n for n, e in skills.items() if isinstance(e, dict) and e.get("status") == "registered")
            overrides = sorted(
                n for n, e in skills.items() if isinstance(e, dict) and e.get("status") == "override_registered"
            )
            failed = sorted(
                n for n, e in skills.items()
                if isinstance(e, dict) and e.get("status") in {"failed", "quarantined", "disabled"}
            )
        queue_items = [i for i in skill_foundry.load_foundry_queue(runtime) if isinstance(i, dict)]
        queue = [i.get("skill_name") for i in queue_items]
        stale_implemented_queue = sorted(
            str(i.get("skill_name"))
            for i in queue_items
            if i.get("skill_name") in IMPLEMENTED_SKILLS and str(i.get("mode") or "new").lower() != "override"
        )
    except Exception:  # noqa: BLE001
        pass

    if isinstance(supervisor_age, (int, float)) and supervisor_age > 900:
        warnings.append(
            {
                "kind": "stale_supervisor",
                "detail": f"unattended supervisor heartbeat is stale ({supervisor_age}s)",
            }
        )
    if stale_implemented_queue:
        warnings.append(
            {
                "kind": "implemented_skill_stuck_in_foundry_queue",
                "skills": stale_implemented_queue,
                "detail": "implemented skills are still queued as new foundry work; only override-mode self-repair should generate them",
            }
        )
    failure_root = progress_kpi.get("failure_root")
    repair_skill = progress_kpi.get("repair_skill")
    if failure_root and (progress_kpi.get("stuck") or int(progress_kpi.get("stall_count") or 0) >= 2):
        warnings.append(
            {
                "kind": "failure_root_loop",
                "failure_root": failure_root,
                "repair_skill": repair_skill,
                "detail": f"progress loop is stuck on {failure_root}; recovery should run {repair_skill}",
            }
        )

    recent: list[dict[str, Any]] = []
    try:
        from .llm_log import llm_decision_summary

        entries = llm_decision_summary(cfg.log_dir).get("entries") or []
        recent = [
            {
                "time": d.get("timestamp"),
                "skill": d.get("selected_skill"),
                "source": d.get("source"),
                "ok": d.get("ok"),
            }
            for d in entries[-6:]
            if isinstance(d, dict)
        ]
    except Exception:  # noqa: BLE001
        pass

    scheduler: dict[str, Any] = {"checked": False}
    if observe:
        try:
            from . import remote_slurm

            tasks = remote_slurm._scheduler_api_json("/api/tasks", timeout=8)
            active_states = {"queued", "pending", "attaching", "starting", "running"}
            services = [
                t
                for t in (tasks if isinstance(tasks, list) else [])
                if isinstance(t, dict)
                and str(t.get("name") or "").startswith("factorio-vllm-service")
                and str(t.get("status") or "") in active_states
            ]
            try:
                expected_services = max(1, int(os.getenv("FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT", "1")))
            except (TypeError, ValueError):
                expected_services = 1
            scheduler = {
                "checked": True,
                "vllm_services": len(services),
                "vllm_service_ids": [t.get("id") for t in services],
                "expected_services": expected_services,
                # The configured count of warm services is expected; more than that means a pileup.
                "healthy": len(services) <= expected_services,
            }
        except Exception as exc:  # noqa: BLE001 - scheduler API is sometimes slow; never hang the digest
            scheduler = _scheduler_from_supervisor(supervisor, api_error=f"{type(exc).__name__}") or {
                "checked": True,
                "error": f"{type(exc).__name__}",
                "vllm_services": None,
            }

    game: dict[str, Any] = {"reachable": False}
    if observe:
        try:
            from .modless_lua import ModlessLuaController

            obs = ModlessLuaController(cfg).observe(include_planning_sites=False)
            inventory = obs.get("inventory") if isinstance(obs.get("inventory"), dict) else {}
            research = obs.get("research") if isinstance(obs.get("research"), dict) else {}
            techs = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
            researched = sorted(n for n, t in techs.items() if isinstance(t, dict) and t.get("researched"))
            game = {
                "reachable": True,
                "tick": obs.get("tick"),
                "inventory": inventory,
                "researched_count": len(researched),
                "researched": researched,
                "entity_count": len(obs.get("entities") or []),
            }
        except Exception as exc:  # noqa: BLE001
            game = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server_reachable": bool(game.get("reachable")),
        "game": game,
        "supervisor": {
            "state": supervisor.get("state"),
            "autopilot_gate": supervisor.get("autopilot_gate"),
            "age_seconds": supervisor_age,
            "autopilot_processes": supervisor.get("autopilot_processes"),
            "idle_layout_processes": supervisor.get("idle_layout_processes"),
            "skill_foundry_processes": supervisor.get("skill_foundry_processes"),
        },
        "autopilot": {
            "state": autopilot.get("state"),
            "cycle": autopilot.get("cycle"),
            "reason": autopilot.get("reason"),
            "age_seconds": autopilot_age,
            "heartbeat_age_seconds": autopilot_heartbeat_age,
            "age_source": autopilot_age_source,
        },
        "live_skill": {
            "active": live.get("active"),
            "skill": live.get("skill"),
            "step": live.get("step"),
            "state": live.get("state"),
            "reason": live.get("reason"),
            "pid": live_pid,
            "stale_reason": live_stale_reason,
            "age_seconds": live_age,
        },
        "foundry": {
            "state": foundry.get("state"),
            "current_skill": foundry.get("current_skill"),
            "generated_total": foundry.get("generated_total"),
            "failed_total": foundry.get("failed_total"),
            "age_seconds": _age_seconds(foundry.get("updated_at")),
        },
        "scheduler": scheduler,
        "progress": {
            "researched": progress_kpi.get("researched"),
            "current_research": progress_kpi.get("current_research"),
            "research_progress": progress_kpi.get("research_progress"),
            "stall_count": progress_kpi.get("stall_count"),
            "stuck": progress_kpi.get("stuck"),
            "failure_root": progress_kpi.get("failure_root"),
            "repair_skill": progress_kpi.get("repair_skill"),
            "seed_count": progress_kpi.get("seed_count"),
            "key_items": progress_kpi.get("key_items"),
            "age_seconds": progress_age,
        },
        "generated_skills": {
            "registered": registered,
            "overrides": overrides,
            "queue": queue,
            "failed": failed,
            "stale_implemented_queue": stale_implemented_queue,
        },
        "recent_decisions": recent,
        "warnings": warnings,
    }


def _fmt_age(value: Any) -> str:
    if value is None:
        return "n/a"
    flag = " (stale)" if isinstance(value, (int, float)) and value > 900 else ""
    return f"{value}s{flag}"


def format_run_health(summary: dict[str, Any]) -> str:
    lines = [f"=== Factorio AI run health @ {summary.get('generated_at')} ==="]

    game = summary.get("game") or {}
    if summary.get("server_reachable"):
        inv = game.get("inventory") if isinstance(game.get("inventory"), dict) else {}
        keys = ["iron-plate", "copper-plate", "coal", "stone", "electronic-circuit", "automation-science-pack", "transport-belt"]
        shown = ", ".join(f"{k}={inv.get(k, 0)}" for k in keys if k in inv) or "(empty)"
        lines.append(f"server   : UP  tick={game.get('tick')}  entities={game.get('entity_count')}  researched={game.get('researched_count')}")
        lines.append(f"  inventory: {shown}")
    else:
        lines.append(f"server   : DOWN ({game.get('error') or 'no RCON / use --no-observe'})")

    sch = summary.get("scheduler") or {}
    if sch.get("checked"):
        if sch.get("vllm_services") is None:
            lines.append(f"scheduler: vLLM services=unavailable (api slow: {sch.get('error')})")
        else:
            warn = "" if sch.get("healthy") else "  (!) MULTIPLE (pileup)"
            source = f" source={sch.get('source')}" if sch.get("source") else ""
            api = f" (scheduler api slow: {sch.get('api_error')})" if sch.get("api_error") else ""
            lines.append(
                f"scheduler: vLLM services={sch.get('vllm_services')} ids={sch.get('vllm_service_ids')}{source}{api}{warn}"
            )

    sup = summary.get("supervisor") or {}
    lines.append(
        f"supervisor: state={sup.get('state')} gate={sup.get('autopilot_gate')} age={_fmt_age(sup.get('age_seconds'))}"
    )
    lines.append(
        "  procs: autopilot={a} idle_layout={i} foundry={f}".format(
            a=sup.get("autopilot_processes"), i=sup.get("idle_layout_processes"), f=sup.get("skill_foundry_processes")
        )
    )

    ap = summary.get("autopilot") or {}
    age_source = ap.get("age_source")
    source_text = f" source={age_source}" if age_source and age_source != "autopilot" else ""
    heartbeat_text = ""
    if age_source == "live_skill":
        heartbeat_text = f" heartbeat_age={_fmt_age(ap.get('heartbeat_age_seconds'))}"
    lines.append(
        f"autopilot: state={ap.get('state')} cycle={ap.get('cycle')} "
        f"age={_fmt_age(ap.get('age_seconds'))}{source_text}{heartbeat_text}"
    )
    pr = summary.get("progress") or {}
    if pr.get("updated_at") is not None or pr.get("researched") is not None:
        stuck = " STUCK" if pr.get("stuck") else ""
        lines.append(
            f"progress : researched={pr.get('researched')} research={pr.get('current_research')}"
            f"({pr.get('research_progress')}) stall={pr.get('stall_count')}{stuck} age={_fmt_age(pr.get('age_seconds'))}"
        )
        if pr.get("failure_root") or pr.get("repair_skill") or pr.get("seed_count"):
            lines.append(
                "  recovery: failure_root={r} repair_skill={s} seed_count={c}".format(
                    r=pr.get("failure_root"), s=pr.get("repair_skill"), c=pr.get("seed_count")
                )
            )
        if isinstance(pr.get("key_items"), dict) and pr.get("key_items"):
            shown = ", ".join(f"{k}={v}" for k, v in pr["key_items"].items())
            lines.append(f"  key items: {shown}")
    ls = summary.get("live_skill") or {}
    stale_text = f" stale={ls.get('stale_reason')}" if ls.get("stale_reason") else ""
    lines.append(
        f"live skill: {ls.get('skill')} step={ls.get('step')} state={ls.get('state')} "
        f"age={_fmt_age(ls.get('age_seconds'))}{stale_text} reason={ls.get('reason')}"
    )

    fo = summary.get("foundry") or {}
    lines.append(
        f"foundry  : state={fo.get('state')} current={fo.get('current_skill')} "
        f"+{fo.get('generated_total') or 0}/-{fo.get('failed_total') or 0} age={_fmt_age(fo.get('age_seconds'))}"
    )
    gs = summary.get("generated_skills") or {}
    lines.append(f"  generated: registered={gs.get('registered')} queue={gs.get('queue')} failed={gs.get('failed')}")
    lines.append(f"  self-repair overrides (active): {gs.get('overrides')}")
    if gs.get("stale_implemented_queue"):
        lines.append(f"  stale implemented queue: {gs.get('stale_implemented_queue')}")

    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            if isinstance(warning, dict):
                lines.append(f"  {warning.get('kind')}: {warning.get('detail')}")

    lines.append("recent decisions (oldest->newest):")
    decisions = summary.get("recent_decisions") or []
    if not decisions:
        lines.append("  (none recorded)")
    for d in decisions:
        lines.append(f"  {d.get('time')}  src={d.get('source')}  skill={d.get('skill')}  ok={d.get('ok')}")
    return "\n".join(lines)
