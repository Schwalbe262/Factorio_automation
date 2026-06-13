from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import request, error

from .planner import (
    factory_layout_issues,
    factory_layout_opportunities,
    factory_layout_simulation_candidates,
    factory_layout_structure,
)
from .skill_registry import IMPLEMENTED_SKILLS
from .strategy import heuristic_strategy, make_strategy_payload, normalize_strategy_response


DEFAULT_POLL_SECONDS = 1.0
PLANNER_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_goal": {"type": "string"},
        "action_hint": {"type": ["object", "null"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["selected_goal", "action_hint", "confidence", "reason", "safety_notes"],
}
STRATEGY_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_skill": {"type": "string"},
        "priority": {"type": "integer"},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "expected_effect": {"type": "string"},
    },
    "required": ["selected_skill", "priority", "reason", "evidence", "blockers", "expected_effect"],
}
LAYOUT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_candidate_id": {"type": ["string", "null"]},
        "score": {"type": "integer"},
        "reasoning": {"type": "string"},
        "expected_improvements": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "next_simulation_focus": {"type": "string"},
        "build_ready": {"type": "boolean"},
        "no_apply": {"type": "boolean"},
    },
    "required": [
        "selected_candidate_id",
        "score",
        "reasoning",
        "expected_improvements",
        "risks",
        "next_simulation_focus",
        "build_ready",
        "no_apply",
    ],
}


def run_worker(root: Path, poll_seconds: float = DEFAULT_POLL_SECONDS, once: bool = False) -> None:
    for folder in ("queue", "running", "results", "failed", "logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    while True:
        write_status(root, "idle")
        task_path = next_task(root)
        if task_path is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        run_one(root, task_path)
        if once:
            return


def next_task(root: Path) -> Path | None:
    candidates = sorted((root / "queue").glob("*.json"))
    if not candidates:
        return None
    source = candidates[0]
    target = root / "running" / source.name
    try:
        source.replace(target)
    except FileNotFoundError:
        return None
    return target


def run_one(root: Path, task_path: Path) -> None:
    write_status(root, f"running={task_path.name}")
    progress_path = task_path.with_name(f"{task_path.name}.progress")
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        atomic_json(progress_path, {"running": True, "id": task.get("id"), "started_at": started_at})
        result = execute_task(task)
        result.setdefault("id", task.get("id"))
        result.setdefault("type", task.get("type"))
        result.setdefault("ok", True)
        result["started_at"] = started_at
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(root / "results" / task_path.name, result)
    except Exception as exc:  # noqa: BLE001
        failed = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json(root / "failed" / task_path.name, failed)
    finally:
        try:
            task_path.unlink()
        except FileNotFoundError:
            pass
        try:
            progress_path.unlink()
        except FileNotFoundError:
            pass


def run_task_file(task_path: Path, result_path: Path) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        result = execute_task(task)
        result.setdefault("id", task.get("id"))
        result.setdefault("type", task.get("type"))
        result.setdefault("ok", True)
        result["started_at"] = started_at
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "id": None,
            "type": None,
            "error": f"{type(exc).__name__}: {exc}",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    atomic_json(result_path, result)
    return result


def execute_task(task: dict[str, Any]) -> dict[str, Any]:
    task_type = task.get("type")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if task_type == "planner_request":
        return run_planner_request(payload)
    if task_type == "strategy_request":
        return run_strategy_request(payload)
    if task_type == "layout_improvement_request":
        return run_layout_improvement_request(payload)
    if task_type == "strategy_model_benchmark":
        return run_strategy_model_benchmark(payload)
    raise ValueError(f"unsupported task type: {task_type}")


def run_planner_request(payload: dict[str, Any]) -> dict[str, Any]:
    llm_result = try_llm_planner(payload)
    if llm_result is not None:
        llm_result["source"] = "llm"
        return llm_result
    result = heuristic_planner(payload)
    result["source"] = "heuristic"
    return result


def run_strategy_request(payload: dict[str, Any]) -> dict[str, Any]:
    llm_result, llm_diagnostics = try_llm_strategy_with_diagnostics(payload)
    if llm_result is not None:
        llm_result["source"] = "llm"
        llm_result.update(llm_diagnostics)
        return llm_result
    result = heuristic_strategy(
        objective=str(payload.get("objective") or payload.get("goal") or "launch_rocket_program"),
        observation=payload.get("observation") if isinstance(payload.get("observation"), dict) else {},
        production_targets=payload.get("production_targets") if isinstance(payload.get("production_targets"), dict) else {},
    )
    result["source"] = "heuristic"
    result["ok"] = True
    result.update({key: value for key, value in llm_diagnostics.items() if value not in (None, "")})
    return result


def run_layout_improvement_request(payload: dict[str, Any]) -> dict[str, Any]:
    compact = compact_layout_improvement_payload(payload)
    prompt = (
        "You are a Factorio factory layout design reviewer running in the background while another "
        "executor skill continues playing. Evaluate simulation-only site layout candidates. "
        "Do not ask to build, mine, craft, demolish, or move anything. Pick one candidate or a next "
        "simulation focus. Return strict JSON only matching the schema.\n\n"
        f"Payload:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    parsed, diagnostics = call_llm_json_with_diagnostics(
        system="Return strict JSON only. This is simulation-only design work; do not apply changes.",
        prompt=prompt,
        schema=LAYOUT_RESPONSE_SCHEMA,
    )
    if parsed is None:
        result = heuristic_layout_improvement(compact)
        result["source"] = "heuristic"
        result["ok"] = True
        result.update({key: value for key, value in diagnostics.items() if value not in (None, "")})
        result["llm_prompt_chars"] = len(prompt)
        return result
    result = normalize_layout_response(parsed)
    result["source"] = "llm"
    result["ok"] = True
    result["llm_prompt_chars"] = len(prompt)
    result.update(diagnostics)
    return result


def compact_layout_improvement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
    monitor = payload.get("factory_monitor") if isinstance(payload.get("factory_monitor"), dict) else {}
    factory_sites = monitor.get("factory_sites") if isinstance(monitor.get("factory_sites"), list) else []
    logistics_links = monitor.get("logistics_links") if isinstance(monitor.get("logistics_links"), list) else []
    layout_improvement = _layout_improvement_context(observation, factory_sites, logistics_links)
    return {
        "objective": payload.get("objective") or "launch_rocket_program",
        "active_skill": payload.get("active_skill"),
        "active_step": payload.get("active_step"),
        "instruction": (
            "The active skill is still running. Keep improving site layout designs in simulation only "
            "until the active skill reports completion."
        ),
        "production_targets": _dict_head(payload.get("production_targets"), 16),
        "layout_improvement": layout_improvement,
        "rules": [
            "Do not apply or build the design.",
            "Prefer candidates that reduce bottlenecks, footprint, transport distance, or ratio error.",
            "Flag belt-capacity risk when required item flow approaches or exceeds belt capacity.",
            "The output can be used later by a deterministic executor after explicit approval.",
        ],
    }


def normalize_layout_response(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("evaluation"), dict):
        raw = raw["evaluation"]
    selected_candidate = raw.get("selected_candidate_id") or raw.get("candidate_id")
    expected_improvements = raw.get("expected_improvements") or raw.get("improvements") or raw.get("expected_improvement")
    risks = raw.get("risks") or raw.get("risk") or raw.get("safety_notes")
    return {
        "selected_candidate_id": selected_candidate,
        "score": max(0, min(100, _int_value(raw.get("score"), 50))),
        "reasoning": str(raw.get("reasoning") or raw.get("recommendation") or ""),
        "expected_improvements": _string_list(expected_improvements),
        "risks": _string_list(risks),
        "next_simulation_focus": str(raw.get("next_simulation_focus") or raw.get("next_focus") or ""),
        "build_ready": bool(raw.get("build_ready")),
        "no_apply": True,
    }


def heuristic_layout_improvement(compact: dict[str, Any]) -> dict[str, Any]:
    layout = compact.get("layout_improvement") if isinstance(compact.get("layout_improvement"), dict) else {}
    candidates = layout.get("simulation_candidates") if isinstance(layout.get("simulation_candidates"), list) else []
    selected = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    simulation = selected.get("simulation") if isinstance(selected.get("simulation"), dict) else {}
    return {
        "selected_candidate_id": selected.get("candidate_id"),
        "score": _int_value(simulation.get("score"), 50),
        "reasoning": "Selected the highest-scored simulation-only layout candidate.",
        "expected_improvements": [str(simulation.get("delta") or "layout simulation delta")],
        "risks": ["Static simulation only; exact tiles, belts, inserters, power, and collisions are not validated here."],
        "next_simulation_focus": "Generate another candidate or refine the current one against real site/link data.",
        "build_ready": False,
        "no_apply": True,
    }


def run_strategy_model_benchmark(payload: dict[str, Any]) -> dict[str, Any]:
    models = payload.get("models") if isinstance(payload.get("models"), list) else []
    strategy_payload = payload.get("strategy_payload") if isinstance(payload.get("strategy_payload"), dict) else payload
    original_model = os.getenv("FACTORIO_AI_LLM_MODEL")
    rows = []
    try:
        for model in [str(item) for item in models if str(item).strip()]:
            os.environ["FACTORIO_AI_LLM_MODEL"] = model
            started = time.monotonic()
            result = run_strategy_request(strategy_payload)
            latency_ms = int((time.monotonic() - started) * 1000)
            rows.append(
                {
                    "model": model,
                    "latency_ms": latency_ms,
                    "source": result.get("source"),
                    "selected_skill": result.get("selected_skill"),
                    "priority": result.get("priority"),
                    "ok": bool(result.get("ok", True)),
                    "reason": result.get("reason"),
                    "llm_error": result.get("llm_error", ""),
                    "llm_prompt_chars": result.get("llm_prompt_chars"),
                    "llm_response_snippet": result.get("llm_response_snippet", ""),
                }
            )
    finally:
        if original_model is None:
            os.environ.pop("FACTORIO_AI_LLM_MODEL", None)
        else:
            os.environ["FACTORIO_AI_LLM_MODEL"] = original_model
    return {
        "ok": True,
        "type": "strategy_model_benchmark",
        "base_url_configured": bool(os.getenv("FACTORIO_AI_LLM_BASE_URL")),
        "models": rows,
    }


def try_llm_planner(payload: dict[str, Any]) -> dict[str, Any] | None:
    base_url = os.getenv("FACTORIO_AI_LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("FACTORIO_AI_LLM_MODEL", "")
    if not base_url or not model:
        return None
    prompt = (
        "You are a Factorio planning assistant. Choose one safe high-level goal or action hint. "
        "Return only JSON matching the schema.\n\n"
        f"Payload:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    request_payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return strict JSON only. Do not directly mutate game state."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if os.getenv("FACTORIO_AI_LLM_GUIDED_JSON", "").lower() in {"1", "true", "yes", "on"}:
        request_payload["guided_json"] = PLANNER_RESPONSE_SCHEMA
    req = request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    api_key = os.getenv("FACTORIO_AI_LLM_API_KEY")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with request.urlopen(req, timeout=float(os.getenv("FACTORIO_AI_LLM_TIMEOUT", "60"))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError, TimeoutError):
        return None
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    parsed = parse_json_object_from_content(content)
    if isinstance(parsed, dict):
        return normalize_planner_response(parsed)
    return None


def try_llm_strategy(payload: dict[str, Any]) -> dict[str, Any] | None:
    result, _diagnostics = try_llm_strategy_with_diagnostics(payload)
    return result


def try_llm_strategy_with_diagnostics(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    objective = str(payload.get("objective") or payload.get("goal") or "launch_rocket_program")
    observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
    production_targets = payload.get("production_targets") if isinstance(payload.get("production_targets"), dict) else {}
    base_payload = make_strategy_payload(objective, observation, production_targets)
    if isinstance(payload.get("available_skills"), list):
        base_payload["available_skills"] = payload["available_skills"]
    base_payload = compact_strategy_payload(base_payload)
    base_payload["rule"] = (
        "Select one high-level skill. Follow the dependency tree backward from the objective, "
        "then use factory_monitor production estimates to identify the current bottleneck. "
        "Never emit tick-level actions. selected_skill must exactly match one entry from allowed_skill_names. "
        "Do not answer a per-minute production deficit with repeated hand crafting when an automation skill exists."
    )
    prompt = (
        "You are the strategic layer for a Factorio autoplayer. "
        "Pick the next high-level skill and justify it from the observation. "
        "Only choose selected_skill from allowed_skill_names. "
        "Return strict JSON only matching the schema.\n\n"
        f"Payload:\n{json.dumps(base_payload, ensure_ascii=False)}"
    )
    diagnostics: dict[str, Any] = {
        "llm_prompt_chars": len(prompt),
    }
    parsed, call_diagnostics = call_llm_json_with_diagnostics(
        system="Return strict JSON only. You choose strategy, not direct world actions.",
        prompt=prompt,
        schema=STRATEGY_RESPONSE_SCHEMA,
    )
    diagnostics.update(call_diagnostics)
    if parsed is None:
        diagnostics.setdefault("llm_error", "LLM unavailable or invalid JSON response")
        return None, diagnostics
    normalized = normalize_strategy_response(parsed, fallback_objective=objective)
    allowed = set(base_payload.get("allowed_skill_names") if isinstance(base_payload.get("allowed_skill_names"), list) else [])
    if allowed and normalized.get("selected_skill") not in allowed:
        diagnostics["llm_error"] = f"selected_skill not allowed: {normalized.get('selected_skill')}"
        return None, diagnostics
    return normalized, diagnostics


def call_llm_json(system: str, prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any] | None:
    parsed, _diagnostics = call_llm_json_with_diagnostics(system, prompt, schema)
    return parsed


def call_llm_json_with_diagnostics(
    system: str,
    prompt: str,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    base_url = os.getenv("FACTORIO_AI_LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("FACTORIO_AI_LLM_MODEL", "")
    if not base_url or not model:
        return None, {"llm_error": "LLM base URL or model is not configured"}
    request_payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if schema and os.getenv("FACTORIO_AI_LLM_GUIDED_JSON", "").lower() in {"1", "true", "yes", "on"}:
        request_payload["guided_json"] = schema
    req = request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    api_key = os.getenv("FACTORIO_AI_LLM_API_KEY")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with request.urlopen(req, timeout=float(os.getenv("FACTORIO_AI_LLM_TIMEOUT", "60"))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except OSError:
            details = ""
        return None, {"llm_error": f"LLM HTTP {exc.code}: {_snippet(details)}"}
    except (OSError, error.URLError, TimeoutError) as exc:
        return None, {"llm_error": f"{type(exc).__name__}: {exc}"}
    except json.JSONDecodeError as exc:
        return None, {"llm_error": f"LLM returned non-JSON HTTP body: {exc}"}
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, {"llm_error": "LLM response missing choices[0].message.content"}
    parsed = parse_json_object_from_content(content)
    if not isinstance(parsed, dict):
        return None, {
            "llm_error": "LLM response content is not a JSON object",
            "llm_response_snippet": _snippet(content),
        }
    return parsed, {"llm_response_snippet": _snippet(content)}


def compact_strategy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    monitor = payload.get("factory_monitor") if isinstance(payload.get("factory_monitor"), dict) else {}
    target_status = monitor.get("target_status") if isinstance(monitor.get("target_status"), dict) else {}
    target_items = target_status.get("items") if isinstance(target_status.get("items"), list) else []
    bottlenecks = monitor.get("bottlenecks") if isinstance(monitor.get("bottlenecks"), list) else []
    factory_sites = monitor.get("factory_sites") if isinstance(monitor.get("factory_sites"), list) else []
    logistics_links = monitor.get("logistics_links") if isinstance(monitor.get("logistics_links"), list) else []
    observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
    layout_improvement = _layout_improvement_context(observation, factory_sites, logistics_links)
    research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
    technologies = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
    available_skills = payload.get("available_skills") if isinstance(payload.get("available_skills"), list) else []
    executable_skills = [
        item
        for item in available_skills
        if isinstance(item, dict) and str(item.get("name") or "") in IMPLEMENTED_SKILLS
    ]
    return {
        "objective": payload.get("objective"),
        "inventory": _dict_head(monitor.get("inventory") if isinstance(monitor.get("inventory"), dict) else observation.get("inventory"), 24),
        "production_targets": _dict_head(payload.get("production_targets"), 24),
        "target_status": [_compact_dict(item, ("item", "target_per_minute", "estimated_per_minute", "deficit_per_minute", "satisfied")) for item in target_items[:12] if isinstance(item, dict)],
        "bottlenecks": [_compact_dict(item, ("item", "reason", "stock", "estimated_per_minute", "severity", "required_by")) for item in bottlenecks[:6] if isinstance(item, dict)],
        "factory_site_summary": _factory_site_summary(factory_sites),
        "factory_sites": _compact_factory_sites(factory_sites),
        "logistics_links": _compact_logistics_links(logistics_links),
        "layout_improvement": layout_improvement,
        "power_networks": payload.get("power_networks") if isinstance(payload.get("power_networks"), list) else [],
        "researched_technologies": [
            str(name)
            for name, value in sorted(technologies.items())
            if isinstance(value, dict) and value.get("researched")
        ][:30],
        "current_research": research.get("current"),
        "threats": _compact_dict(
            payload.get("threats") if isinstance(payload.get("threats"), dict) else {},
            ("enemy_count", "danger_level", "nearest_enemy", "nearest_spawner", "armed_gun_turret_count", "recent_enemy_damage_count", "max_spawner_pollution"),
        ),
        "spatial_planning": _compact_spatial_planning(payload.get("spatial_planning")),
        "research_planning": _compact_dict(
            payload.get("research_planning") if isinstance(payload.get("research_planning"), dict) else {},
            ("lab_count", "powered_lab_count", "layout_patterns", "recommended_next"),
        ),
        "build_item_supply": _compact_build_item_supply(payload.get("build_item_supply")),
        "dependency_targets": _dependency_items(payload.get("goal_dependency_tree"), limit=40),
        "allowed_skill_names": [str(item.get("name")) for item in executable_skills if item.get("name")],
        "available_skills": _compact_skill_catalog(executable_skills),
        "skill_selection_guidance": [
            "Use produce_* skills for immediate stock bootstrap only, not to solve per-minute throughput deficits.",
            "Use expand_* or automate_* skills when target_status shows production/minute deficits.",
            "After automation is researched, prefer powered assembler automation over repeated hand crafting.",
            "If build items such as inserters, belts, poles, or assemblers block expansion, choose bootstrap_build_item_mall.",
            "If layout_improvement has an issue/opportunity/candidate severity or score >= 75, choose plan_factory_site before adding more inefficient blocks.",
            "When no urgent build/research/defense task exists, use idle LLM cycles for site layout simulation and improvement planning.",
        ],
        "decision_rule": payload.get("decision_rule"),
    }


def _compact_dict(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: _compact_value(value[key]) for key in keys if key in value}


def _dict_head(value: Any, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _compact_value(value[key]) for key in sorted(value, key=str)[:limit]}


def _compact_value(value: Any, *, string_limit: int = 220, list_limit: int = 8) -> Any:
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[: string_limit - 3] + "..."
    if isinstance(value, list):
        return [_compact_value(item, string_limit=string_limit, list_limit=list_limit) for item in value[:list_limit]]
    if isinstance(value, dict):
        return {str(key): _compact_value(item, string_limit=string_limit, list_limit=list_limit) for key, item in list(value.items())[:list_limit]}
    return value


def _compact_build_item_supply(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    items = value.get("items") if isinstance(value.get("items"), list) else []
    return {
        "recommended_skill": value.get("recommended_skill"),
        "items": [
            _compact_dict(item, ("item", "stock", "needs_mall", "producer_count", "consumer_count"))
            for item in items[:12]
            if isinstance(item, dict)
        ],
    }


def _compact_skill_catalog(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        row = _compact_dict(item, ("name", "executor"))
        role = _skill_role_hint(name)
        if role:
            row["role"] = role
        output.append(row)
    return output


def _skill_role_hint(name: str) -> str:
    hints = {
        "produce_iron_plate": "bootstrap stock; use expand_iron_smelting for throughput",
        "produce_copper_plate": "bootstrap stock; use expand_copper_smelting for throughput",
        "produce_electronic_circuit": "bootstrap stock; use automate_electronic_circuit_line for throughput",
        "produce_automation_science_pack": "bootstrap stock/research; automate science once assemblers are ready",
        "expand_iron_smelting": "increase iron-plate production rate",
        "expand_copper_smelting": "increase copper-plate production rate",
        "build_belt_smelting_line": "build site-level ore-to-plate logistics",
        "setup_power": "build or repair steam power before powered automation",
        "research_automation": "unlock assembling-machine automation",
        "automate_electronic_circuit_line": "build powered circuit assemblers for sustained green-circuit throughput",
        "bootstrap_build_item_mall": "automate expansion items such as belts, inserters, poles, assemblers, furnaces",
        "build_starter_defense": "build turret and ammo defense around factory sites; do not clear nests early",
        "plan_factory_site": "diagnose inefficient site placement, factory graph gaps, and layout parameters before building more",
        "research_logistics": "advance tech after science and lab feeding are available",
    }
    return hints.get(name, "")


def _layout_improvement_context(
    observation: dict[str, Any],
    factory_sites: list[Any],
    logistics_links: list[Any],
) -> dict[str, Any]:
    issues = factory_layout_issues(observation)
    opportunities = factory_layout_opportunities(observation)
    candidates = factory_layout_simulation_candidates(observation)
    if not issues:
        issues = _layout_issues_from_monitor(factory_sites, logistics_links)
    compact_issues = [_compact_layout_item(item) for item in issues[:5] if isinstance(item, dict)]
    compact_opportunities = [_compact_layout_item(item) for item in opportunities[:5] if isinstance(item, dict)]
    compact_candidates = [_compact_layout_candidate(item) for item in candidates[:4] if isinstance(item, dict)]
    top_severity = max(
        [int(item.get("severity") or 0) for item in compact_issues + compact_opportunities],
        default=0,
    )
    top_score = max(
        [
            int((item.get("simulation") if isinstance(item.get("simulation"), dict) else {}).get("score") or 0)
            for item in compact_candidates
        ],
        default=0,
    )
    return {
        "recommended_skill": "plan_factory_site" if max(top_severity, top_score) >= 75 else None,
        "site_structure": _compact_layout_structure(factory_layout_structure(observation)),
        "issues": compact_issues,
        "opportunities": compact_opportunities,
        "simulation_candidates": compact_candidates,
        "improvement_parameters": [
            "site distance from spawn/current factory centroid",
            "producer-to-consumer link completeness and length",
            "machine ratio versus known reusable design patterns",
            "machine density and footprint size",
            "manual fuel/feed dependencies",
            "power network reach and pole corridor length",
            "resource-tile preservation for future miner coverage",
            "reserved belt, rail, and expansion corridors",
        ],
    }


def _layout_issues_from_monitor(factory_sites: list[Any], logistics_links: list[Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for site in factory_sites:
        if not isinstance(site, dict):
            continue
        kind = str(site.get("kind") or "")
        status = str(site.get("status") or "")
        automation = str(site.get("automation_level") or "")
        notes = " ".join(str(item) for item in site.get("notes", []) if item)
        if kind == "steam_power" and ("manual_fuel" in status or "manual_fuel" in notes):
            issues.append(
                _monitor_layout_issue(
                    "manual_power_fuel",
                    76,
                    site,
                    "steam power still depends on manual fuel",
                    "add coal mining, belt/inserter feed, and a short pole corridor before scaling powered blocks",
                )
            )
        if any(marker in status for marker in ("incomplete", "unfueled", "unconfigured")):
            issues.append(
                _monitor_layout_issue(
                    "incomplete_site",
                    72,
                    site,
                    f"{kind} site is {status}",
                    "finish inputs, outputs, recipe, fuel, and power before placing additional similar sites",
                )
            )
        if "burner-bootstrap" in automation:
            issues.append(
                _monitor_layout_issue(
                    "burner_bootstrap_layout",
                    58,
                    site,
                    "site is still in burner bootstrap mode",
                    "reserve room for electric miners, belt-fed inputs, and a scalable furnace/assembler row",
                )
            )
    for link in logistics_links:
        if not isinstance(link, dict):
            continue
        status = str(link.get("status") or "")
        if any(marker in status for marker in ("missing", "incomplete", "blocked")):
            issues.append(
                {
                    "kind": "incomplete_logistics_link",
                    "severity": 82,
                    "site_id": f"{link.get('from_site')}->{link.get('to_site')}",
                    "item": link.get("item"),
                    "detail": f"{link.get('from_site')} -> {link.get('to_site')} is {status}",
                    "recommendation": "complete site-to-site logistics before adding more downstream consumers",
                }
            )
        try:
            length = float(link.get("length_tiles") or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        if length > 180 and str(link.get("kind") or "") != "rail":
            issues.append(
                {
                    "kind": "long_nonrail_logistics",
                    "severity": 70,
                    "site_id": f"{link.get('from_site')}->{link.get('to_site')}",
                    "item": link.get("item"),
                    "detail": f"{link.get('from_site')} -> {link.get('to_site')} is {length:.0f} tiles without rail",
                    "recommendation": "reserve a rail or trunk belt corridor instead of extending ad-hoc local belts",
                }
            )
    issues.sort(key=lambda item: int(item.get("severity") or 0), reverse=True)
    return issues[:12]


def _compact_layout_structure(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "site_count": value.get("site_count"),
        "site_kinds": _dict_head(value.get("site_kinds"), 10),
        "site_items": _dict_head(value.get("site_items"), 10),
        "link_status": _dict_head(value.get("link_status"), 8),
        "machine_counts": _dict_head(value.get("machine_counts"), 12),
        "factory_centroid": _compact_position(value.get("factory_centroid")),
        "footprint": _compact_dict(value.get("footprint"), ("width", "height", "area")),
        "estimated_density": value.get("estimated_density"),
    }


def _compact_layout_item(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": value.get("kind"),
        "severity": value.get("severity"),
        "site_id": _compact_value(value.get("site_id"), string_limit=90),
        "item": value.get("item"),
        "position": _compact_position(value.get("position")),
        "detail": _compact_value(value.get("detail"), string_limit=120),
        "recommendation": _compact_value(value.get("recommendation"), string_limit=140),
        "parameters": _compact_value(value.get("parameters"), string_limit=80, list_limit=5),
    }


def _compact_layout_candidate(value: dict[str, Any]) -> dict[str, Any]:
    simulation = value.get("simulation") if isinstance(value.get("simulation"), dict) else {}
    return {
        "candidate_id": _compact_value(value.get("candidate_id"), string_limit=90),
        "simulation_only": bool(value.get("simulation_only")),
        "not_applied": bool(value.get("not_applied")),
        "target_pattern": _compact_value(value.get("target_pattern"), string_limit=120),
        "requires_build_command": bool(value.get("requires_build_command")),
        "simulation": {
            "score": simulation.get("score"),
            "before": _compact_value(simulation.get("before"), string_limit=80, list_limit=5),
            "after": _compact_value(simulation.get("after"), string_limit=80, list_limit=5),
            "delta": _compact_value(simulation.get("delta"), string_limit=80, list_limit=5),
        },
    }


def _monitor_layout_issue(
    kind: str,
    severity: int,
    site: dict[str, Any],
    detail: str,
    recommendation: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "site_id": site.get("site_id"),
        "item": site.get("item"),
        "position": site.get("position"),
        "detail": detail,
        "recommendation": recommendation,
    }


def _compact_spatial_planning(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    site_selection = value.get("site_selection") if isinstance(value.get("site_selection"), dict) else {}
    rail_network = value.get("rail_network") if isinstance(value.get("rail_network"), dict) else {}
    current_inputs = site_selection.get("current_inputs") if isinstance(site_selection.get("current_inputs"), dict) else {}
    planning_inputs = rail_network.get("planning_inputs") if isinstance(rail_network.get("planning_inputs"), dict) else {}
    resource_rows = []
    for key in ("resource_patches", "known_remote_resources"):
        source = current_inputs if key == "resource_patches" else planning_inputs
        rows = source.get(key) if isinstance(source.get(key), list) else []
        resource_rows.extend(item for item in rows if isinstance(item, dict))
    return {
        "player_position": _compact_position(current_inputs.get("player_position")),
        "factory_centroid": _compact_position(current_inputs.get("factory_centroid")),
        "resource_summary": _resource_patch_summary(resource_rows),
        "site_constraints": _compact_value(site_selection.get("constraints"), string_limit=100, list_limit=4),
        "rail_constraints": _compact_value(rail_network.get("constraints"), string_limit=100, list_limit=3),
    }


def _resource_patch_summary(rows: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        entry = grouped.setdefault(
            name,
            {
                "name": name,
                "tiles": 0,
                "total_amount": 0,
                "nearest_distance": None,
                "nearest_position": None,
            },
        )
        entry["tiles"] += 1
        try:
            entry["total_amount"] += int(float(row.get("amount") or 0))
        except (TypeError, ValueError):
            pass
        distance = _round_float(row.get("distance_from_player") or row.get("distance"), 1)
        if distance is not None and (entry["nearest_distance"] is None or distance < entry["nearest_distance"]):
            entry["nearest_distance"] = distance
            entry["nearest_position"] = _compact_position(row.get("position"))
    output = sorted(
        grouped.values(),
        key=lambda item: (
            item["nearest_distance"] is None,
            item["nearest_distance"] if item["nearest_distance"] is not None else 999999.0,
            str(item["name"]),
        ),
    )
    return output[:limit]


def _factory_site_summary(value: Any, limit: int = 20) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    counts: Counter[tuple[str, str, str]] = Counter()
    for item in value:
        if not isinstance(item, dict):
            continue
        counts[
            (
                str(item.get("kind") or ""),
                str(item.get("item") or ""),
                str(item.get("status") or ""),
            )
        ] += 1
    return [
        {"kind": kind, "item": item or None, "status": status, "count": count}
        for (kind, item, status), count in counts.most_common(limit)
    ]


def _compact_factory_sites(value: Any, limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = [item for item in value if isinstance(item, dict)]
    rows.sort(key=_factory_site_priority)
    return [_compact_factory_site(item) for item in rows[:limit]]


def _factory_site_priority(item: dict[str, Any]) -> tuple[int, str, str]:
    status = str(item.get("status") or "")
    if any(marker in status for marker in ("incomplete", "unfueled", "manual", "unconfigured")):
        rank = 0
    elif "running" in status or "automated" in status:
        rank = 2
    else:
        rank = 1
    return (rank, str(item.get("kind") or ""), str(item.get("site_id") or ""))


def _compact_factory_site(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "site_id": item.get("site_id"),
        "kind": item.get("kind"),
        "item": item.get("item"),
        "status": item.get("status"),
        "position": _compact_position(item.get("position")),
        "automation_level": item.get("automation_level"),
        "machines": _compact_value(item.get("machines"), string_limit=80, list_limit=6),
        "notes": _compact_value(item.get("notes"), string_limit=80, list_limit=2),
    }


def _compact_logistics_links(value: Any, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = [item for item in value if isinstance(item, dict)]
    rows.sort(key=_logistics_link_priority)
    return [_compact_logistics_link(item) for item in rows[:limit]]


def _logistics_link_priority(item: dict[str, Any]) -> tuple[int, str, float]:
    status = str(item.get("status") or "")
    if any(marker in status for marker in ("missing", "incomplete", "blocked")):
        rank = 0
    elif "complete" in status:
        rank = 2
    else:
        rank = 1
    try:
        length = float(item.get("length_tiles") or 0.0)
    except (TypeError, ValueError):
        length = 0.0
    return (rank, str(item.get("item") or ""), length)


def _compact_logistics_link(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": item.get("kind"),
        "item": item.get("item"),
        "from_site": item.get("from_site"),
        "to_site": item.get("to_site"),
        "status": item.get("status"),
        "length_tiles": _round_float(item.get("length_tiles"), 1),
        "notes": _compact_value(item.get("notes"), string_limit=80, list_limit=2),
    }


def _compact_position(value: Any) -> dict[str, float] | Any:
    if not isinstance(value, dict):
        return value
    output: dict[str, float] = {}
    for key in ("x", "y"):
        if key in value:
            rounded = _round_float(value.get(key), 1)
            if rounded is not None:
                output[key] = rounded
    return output


def _round_float(value: Any, digits: int) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any, fallback: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _dependency_items(value: Any, limit: int = 40) -> list[str]:
    seen: list[str] = []

    def visit(node: Any) -> None:
        if len(seen) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        item_name = node.get("item")
        if item_name and str(item_name) not in seen:
            seen.append(str(item_name))
        ingredients = node.get("ingredients") if isinstance(node.get("ingredients"), list) else []
        for ingredient in ingredients:
            if isinstance(ingredient, dict):
                visit(ingredient.get("dependency"))

    visit(value)
    return seen


def parse_json_object_from_content(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return None
    parsed = _try_parse_json_object(text)
    if parsed is not None:
        return parsed
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return _try_parse_json_object(text[start : end + 1])


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _snippet(value: Any, limit: int = 300) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def heuristic_planner(payload: dict[str, Any]) -> dict[str, Any]:
    legal_actions = payload.get("legal_actions") if isinstance(payload.get("legal_actions"), list) else []
    action_hint = legal_actions[0] if legal_actions and isinstance(legal_actions[0], dict) else None
    return {
        "ok": True,
        "selected_goal": str(payload.get("goal") or "produce_iron_plate"),
        "action_hint": action_hint,
        "confidence": 0.35,
        "reason": "LLM unavailable; returned first legal local-planner action hint.",
        "safety_notes": ["Local controller must validate the action before execution."],
    }


def normalize_planner_response(raw: dict[str, Any]) -> dict[str, Any]:
    confidence = raw.get("confidence", 0.0)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    safety = raw.get("safety_notes")
    if not isinstance(safety, list):
        safety = [str(safety)] if safety else []
    action_hint = raw.get("action_hint")
    if action_hint is not None and not isinstance(action_hint, dict):
        action_hint = None
    return {
        "ok": True,
        "selected_goal": str(raw.get("selected_goal") or "produce_iron_plate"),
        "action_hint": action_hint,
        "confidence": max(0.0, min(1.0, confidence_value)),
        "reason": str(raw.get("reason") or ""),
        "safety_notes": [str(item) for item in safety],
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(path)


def write_status(root: Path, message: str) -> None:
    status = {
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    atomic_json(root / "status.txt", status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Factorio AI Slurm queue worker.")
    parser.add_argument("--root", default=os.getenv("ROOT") or os.getcwd())
    parser.add_argument("--task", help="Run one task file and write --result, for AUTO worker dispatch.")
    parser.add_argument("--result", help="Result path for --task.")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.task:
        if not args.result:
            raise SystemExit("--result is required with --task")
        result = run_task_file(Path(args.task), Path(args.result))
        if not result.get("ok"):
            raise SystemExit(1)
        return
    run_worker(Path(args.root), poll_seconds=args.poll_seconds, once=args.once)


if __name__ == "__main__":
    main()
