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


def gather_run_health(cfg: AppConfig, *, observe: bool = True) -> dict[str, Any]:
    runtime = Path(cfg.runtime_dir)
    autopilot = _read_json(runtime / "autopilot-heartbeat.json")
    live = _read_json(runtime / "live-skill-heartbeat.json")
    foundry = _read_json(runtime / "skill-foundry-loop.json")
    supervisor = _read_json(runtime / "unattended-llm-supervisor.json")
    progress_kpi = _read_json(runtime / "progress-kpi.json")
    supervisor_age = _age_seconds(supervisor.get("updated_at"))
    progress_age = _age_seconds(progress_kpi.get("updated_at"))
    warnings: list[dict[str, Any]] = []

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
            scheduler = {"checked": True, "error": f"{type(exc).__name__}", "vllm_services": None}

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
            "age_seconds": _age_seconds(autopilot.get("updated_at")),
        },
        "live_skill": {
            "skill": live.get("skill"),
            "step": live.get("step"),
            "state": live.get("state"),
            "reason": live.get("reason"),
            "age_seconds": _age_seconds(live.get("updated_at")),
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
            lines.append(
                f"scheduler: vLLM services={sch.get('vllm_services')} ids={sch.get('vllm_service_ids')}{warn}"
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
    lines.append(f"autopilot: state={ap.get('state')} cycle={ap.get('cycle')} age={_fmt_age(ap.get('age_seconds'))}")
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
    lines.append(
        f"live skill: {ls.get('skill')} step={ls.get('step')} state={ls.get('state')} "
        f"age={_fmt_age(ls.get('age_seconds'))} reason={ls.get('reason')}"
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
