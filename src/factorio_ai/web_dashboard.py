from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from .config import AppConfig
from .controller import FactorioController
from .item_icons import read_item_icon_png
from .llm_log import llm_decision_summary
from .monitor import summarize_factory
from .networking import dashboard_urls
from .modless_lua import ModlessLuaController
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy, make_layout_improvement_context
from .targets import TARGET_ITEMS, load_targets, parse_target_form, save_targets
from .token_usage import token_usage_summary


FACTORIO_ROUTE = "/factorio"
LEGACY_FACTORIO_ROUTE = "/팩토리오"
FACTORIO_ROUTES = {FACTORIO_ROUTE, LEGACY_FACTORIO_ROUTE}
ICON_ROUTE_PREFIX = "/factorio/icon/"
API_ROUTE = "/api/factorio"
BLUEPRINT_API_ROUTE = "/api/factorio/blueprint"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = {"en", "ko"}
DEFAULT_PUBLIC_DASHBOARD_BASE_URL = "http://27.115.156.173:8787"
KST = timezone(timedelta(hours=9), "KST")
DEFAULT_WEB_CACHE_SECONDS = 30.0
_DASHBOARD_STATE_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_STATE_REFRESHING: set[str] = set()
_DASHBOARD_STATE_CACHE_LOCK = threading.Lock()


TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Factorio AI Factory Monitor",
        "connection": "Connection",
        "objective": "Objective",
        "tick": "Tick",
        "updated": "Updated",
        "strategic_recommendation": "Strategic Recommendation",
        "desired_targets": "Desired Production Targets",
        "estimated_production": "Estimated Production",
        "factory_sites": "Factory Sites",
        "logistics_links": "Logistics Links",
        "threats": "Threats / Defense",
        "recent_damage": "Recent Damage",
        "bottlenecks": "Target Deficits / Bottlenecks",
        "inventory": "Inventory / Machine Contents",
        "technology_chain": "Technology Chain",
        "dependency_tree": "Dependency Tree",
        "item": "Item",
        "per_min": "/ min",
        "usable_per_min": "Usable / min",
        "target_per_min": "Target / min",
        "estimated_per_min": "Estimated / min",
        "isolated_per_min": "Isolated / min",
        "deficit_per_min": "Deficit / min",
        "producers": "Producers",
        "confidence": "Confidence",
        "reason": "Reason",
        "stock": "Stock",
        "count": "Count",
        "kind": "Kind",
        "status": "Status",
        "position": "Position",
        "automation": "Automation",
        "machines": "Machines",
        "from": "From",
        "to": "To",
        "length": "Length",
        "technology": "Technology",
        "prerequisites": "Prerequisites",
        "unlocks": "Unlocks",
        "save_targets": "Save Targets",
        "priority": "priority",
        "blockers": "Blockers",
        "no_production": "No active producers inferred yet.",
        "no_sites": "No factory sites inferred yet.",
        "no_links": "No logistics links inferred yet.",
        "no_threats": "No hostile threat context observed yet.",
        "no_recent_damage": "No enemy damage to the factory observed yet.",
        "no_bottleneck": "No bottleneck inferred for the current objective.",
        "no_inventory": "No tracked inventory yet.",
        "no_tech": "No technology requirement inferred for this objective yet.",
        "no_targets": "No production targets are configured yet.",
        "targets_satisfied": (
            "All user production targets are satisfied. The strategic LLM may raise targets "
            "or choose the next rocket-program item."
        ),
        "targets_unsatisfied": "Some production targets are below the desired rate.",
        "executor_missing": "Executor missing. Codex must implement this skill before the AI can run it safely.",
        "executor": "Executor",
        "language": "Language",
        "danger_level": "Danger Level",
        "enemy_count": "Enemies",
        "nearest_enemy": "Nearest Enemy",
        "nearest_spawner": "Nearest Spawner",
        "armed_turrets": "Armed Turrets",
        "unarmed_turrets": "Unarmed Turrets",
        "spawner_pollution": "Max Spawner Pollution",
        "recommended_actions": "Recommended Actions",
        "cause": "Cause",
        "damage": "Damage",
        "health": "Health",
        "token_usage": "Codex Token Usage",
        "no_token_usage": "No Codex token usage samples recorded yet.",
        "latest_tokens": "Latest Tokens",
        "total_delta_tokens": "Turn Delta",
        "sample_count": "Samples",
        "last_sample": "Last Sample",
        "power_networks": "Power Networks",
        "no_power_networks": "No electric power networks inferred yet.",
        "llm_decisions": "LLM Decision Log",
        "no_llm_decisions": "No LLM strategy attempts recorded yet.",
        "provider": "Provider",
        "source": "Source",
        "latency_ms": "Latency ms",
        "error": "Error",
        "network": "Network",
        "generation_kw": "Generation kW",
        "demand_kw": "Demand kW",
        "satisfaction": "Supply",
        "unconnected": "Unconnected",
    },
    "ko": {
        "title": "팩토리오 공장 모니터링",
        "connection": "연결",
        "objective": "목표",
        "tick": "틱",
        "updated": "갱신",
        "strategic_recommendation": "전략 추천",
        "desired_targets": "희망 생산량",
        "estimated_production": "추정 생산량",
        "bottlenecks": "목표 부족분 / 병목",
        "inventory": "인벤토리 / 기계 내용물",
        "technology_chain": "기술 체인",
        "dependency_tree": "의존성 트리",
        "item": "품목",
        "per_min": "분당",
        "usable_per_min": "사용 가능 / 분",
        "target_per_min": "목표 / 분",
        "estimated_per_min": "추정 / 분",
        "isolated_per_min": "고립 / 분",
        "deficit_per_min": "부족 / 분",
        "producers": "생산기",
        "confidence": "신뢰도",
        "reason": "이유",
        "stock": "재고",
        "count": "수량",
        "technology": "기술",
        "prerequisites": "선행 기술",
        "unlocks": "해금",
        "save_targets": "목표 저장",
        "priority": "우선순위",
        "blockers": "막힌 항목",
        "no_production": "아직 추정된 활성 생산기가 없습니다.",
        "no_bottleneck": "현재 목표에서 추정된 병목이 없습니다.",
        "no_inventory": "추적 중인 인벤토리가 없습니다.",
        "no_tech": "현재 목표에 대해 추정된 기술 요구사항이 없습니다.",
        "no_targets": "설정된 생산 목표가 없습니다.",
        "targets_satisfied": "사용자 생산 목표가 모두 충족되었습니다. 전략 LLM이 다음 로켓 프로그램 목표를 고를 수 있습니다.",
        "targets_unsatisfied": "일부 생산 목표가 희망 생산량보다 낮습니다.",
        "executor_missing": "실행 로직이 없습니다. AI가 안전하게 실행하기 전에 Codex가 이 스킬을 구현해야 합니다.",
        "executor": "실행기",
        "language": "언어",
        "token_usage": "Codex 토큰 사용량",
        "no_token_usage": "아직 기록된 Codex 토큰 사용량 샘플이 없습니다.",
        "latest_tokens": "최근 토큰",
        "total_delta_tokens": "증가량",
        "sample_count": "샘플",
        "last_sample": "최근 기록",
        "power_networks": "전력망",
        "no_power_networks": "아직 추정된 전력망이 없습니다.",
        "llm_decisions": "LLM 판단 로그",
        "no_llm_decisions": "아직 기록된 LLM 전략 시도가 없습니다.",
        "provider": "제공자",
        "source": "소스",
        "latency_ms": "지연 ms",
        "error": "오류",
        "network": "전력망",
        "generation_kw": "발전 kW",
        "demand_kw": "수요 kW",
        "satisfaction": "공급률",
        "unconnected": "미연결",
    },
}

TEXT["en"].update(
    {
        "factory_events": "Recent Factory Edits",
        "no_factory_events": "No player or robot factory edits observed yet.",
        "throughput_constraints": "Throughput Constraints",
        "no_throughput_constraints": "No throughput constraints inferred yet.",
        "required": "Required",
        "available": "Available",
        "actor": "Actor",
        "action": "Action",
        "threats": "Threats / Defense",
        "recent_damage": "Recent Damage",
        "no_threats": "No hostile threat context observed yet.",
        "no_recent_damage": "No enemy damage to the factory observed yet.",
        "danger_level": "Danger Level",
        "enemy_count": "Enemies",
        "nearest_enemy": "Nearest Enemy",
        "nearest_spawner": "Nearest Spawner",
        "armed_turrets": "Armed Turrets",
        "unarmed_turrets": "Unarmed Turrets",
        "spawner_pollution": "Max Spawner Pollution",
        "recommended_actions": "Recommended Actions",
        "cause": "Cause",
        "damage": "Damage",
        "health": "Health",
        "agent_activity": "AI Activity",
        "agent_kind": "Agent",
        "agent_position": "Current Position",
        "agent_target": "Target Position",
        "last_action": "Last Action",
        "last_detail": "Detail",
        "no_agent_activity": "No AI agent marker has been recorded yet.",
    }
)
TEXT["ko"].update(
    {
        "factory_events": "Recent Factory Edits",
        "no_factory_events": "No player or robot factory edits observed yet.",
        "throughput_constraints": "Throughput Constraints",
        "no_throughput_constraints": "No throughput constraints inferred yet.",
        "required": "Required",
        "available": "Available",
        "actor": "Actor",
        "action": "Action",
        "agent_activity": "AI 동작 위치",
        "agent_kind": "Agent",
        "agent_position": "현재 위치",
        "agent_target": "목표 위치",
        "last_action": "마지막 행동",
        "last_detail": "상세",
        "no_agent_activity": "아직 기록된 AI 위치 marker가 없습니다.",
    }
)
TEXT["en"].update(
    {
        "layout_improvement": "Layout Improvement",
        "layout_issues": "Issues",
        "layout_opportunities": "Opportunities",
        "layout_candidates": "Simulation Candidates",
        "no_layout_improvement": "No layout issues, optimization opportunities, or simulation candidates inferred yet.",
        "layout_background": "Background Layout Work",
        "no_layout_background": "No background layout work has been recorded yet.",
        "event": "Event",
        "active_skill": "Active Skill",
        "candidate": "Candidate",
        "pattern": "Pattern",
        "score": "Score",
        "before": "Before",
        "after": "After",
        "delta": "Delta",
        "not_applied": "Not Applied",
        "blueprint": "Blueprint",
        "copy_blueprint": "Copy blueprint",
        "copied": "Copied",
        "copy_failed": "Copy failed",
    }
)
TEXT["ko"].update(
    {
        "layout_improvement": "\ub808\uc774\uc544\uc6c3 \uac1c\uc120",
        "layout_issues": "\ubb38\uc81c",
        "layout_opportunities": "\uac1c\uc120 \uae30\ud68c",
        "layout_candidates": "\uc2dc\ubbac\ub808\uc774\uc158 \ud6c4\ubcf4",
        "no_layout_improvement": "\uc544\uc9c1 \ucd94\uc815\ub41c \ub808\uc774\uc544\uc6c3 \ubb38\uc81c, \uac1c\uc120 \uae30\ud68c, \uc2dc\ubbac\ub808\uc774\uc158 \ud6c4\ubcf4\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.",
        "layout_background": "\ubc31\uadf8\ub77c\uc6b4\ub4dc \ub808\uc774\uc544\uc6c3 \uc791\uc5c5",
        "no_layout_background": "\uc544\uc9c1 \uae30\ub85d\ub41c \ubc31\uadf8\ub77c\uc6b4\ub4dc \ub808\uc774\uc544\uc6c3 \uc791\uc5c5\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.",
        "event": "\uc774\ubca4\ud2b8",
        "active_skill": "\uc9c4\ud589 \uc2a4\ud0ac",
        "candidate": "\ud6c4\ubcf4",
        "pattern": "\ud328\ud134",
        "score": "\uc810\uc218",
        "before": "\uac1c\uc120 \uc804",
        "after": "\uac1c\uc120 \ud6c4",
        "delta": "\ubcc0\ud654",
        "not_applied": "\ubbf8\uc801\uc6a9",
        "blueprint": "\ube14\ub8e8\ud504\ub9b0\ud2b8",
        "copy_blueprint": "\ube14\ub8e8\ud504\ub9b0\ud2b8 \ubcf5\uc0ac",
        "copied": "\ubcf5\uc0ac\ub428",
        "copy_failed": "\ubcf5\uc0ac \uc2e4\ud328",
    }
)


def serve_dashboard(
    cfg: AppConfig,
    host: str = "0.0.0.0",
    port: int = 18889,
    objective: str = "launch_rocket_program",
) -> None:
    handler = make_dashboard_handler(cfg, objective)
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=_warm_dashboard_cache, args=(cfg, objective), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


def make_dashboard_handler(cfg: AppConfig, default_objective: str) -> type[BaseHTTPRequestHandler]:
    class FactorioDashboardHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = normalized_path(parsed.path)
            query = parse_qs(parsed.query)
            if path not in FACTORIO_ROUTES:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return

            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            values = parse_qs(body)
            lang = request_language(path, query, values)
            targets = parse_target_form(values)
            save_targets(cfg.runtime_dir, targets)
            state = build_dashboard_state_cached(
                cfg,
                values.get("objective", [default_objective])[0],
                force_refresh=True,
            )
            html = render_dashboard(state, lang=lang).encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = normalized_path(parsed.path)
            query = parse_qs(parsed.query)
            objective = query.get("objective", [default_objective])[0]

            if path in {"", "/"}:
                self._redirect(dashboard_path(DEFAULT_LANG, objective))
                return

            if path == LEGACY_FACTORIO_ROUTE:
                self._redirect(dashboard_path("ko", objective))
                return

            if path == FACTORIO_ROUTE:
                lang = request_language(path, query)
                state = build_dashboard_state_cached(cfg, objective)
                body = render_dashboard(state, lang=lang)
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == API_ROUTE:
                state = build_dashboard_state_cached(cfg, objective)
                self._send(
                    200,
                    json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return

            if path == BLUEPRINT_API_ROUTE:
                state = build_dashboard_state_cached(cfg, objective)
                response = _candidate_blueprint_response(state, query.get("candidate_id", [""])[0])
                status = 200 if response.get("ok") else 404
                self._send(
                    status,
                    json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return

            if path.startswith(ICON_ROUTE_PREFIX):
                item_name = path[len(ICON_ROUTE_PREFIX) :].removesuffix(".png")
                icon = read_item_icon_png(cfg, item_name)
                if icon is None:
                    self._send(404, b"icon not found\n", "text/plain; charset=utf-8")
                    return
                self._send(200, icon, "image/png")
                return

            self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return FactorioDashboardHandler


def _warm_dashboard_cache(cfg: AppConfig, objective: str) -> None:
    try:
        build_dashboard_state_cached(cfg, objective, force_refresh=True)
    except Exception:
        return


def normalized_path(path: str) -> str:
    value = unquote(path).rstrip("/")
    return value or "/"


def is_factorio_route(path: str) -> bool:
    return normalized_path(path) in FACTORIO_ROUTES


def request_language(path: str, query: dict[str, list[str]], form: dict[str, list[str]] | None = None) -> str:
    form = form or {}
    raw = (form.get("lang") or query.get("lang") or [None])[0]
    if raw in SUPPORTED_LANGS:
        return str(raw)
    if normalized_path(path) == LEGACY_FACTORIO_ROUTE:
        return "ko"
    return DEFAULT_LANG


def dashboard_path(lang: str = DEFAULT_LANG, objective: str | None = None) -> str:
    params: dict[str, str] = {}
    if lang in SUPPORTED_LANGS and lang != DEFAULT_LANG:
        params["lang"] = lang
    if objective:
        params["objective"] = objective
    suffix = f"?{urlencode(params)}" if params else ""
    return f"{FACTORIO_ROUTE}{suffix}"


def public_dashboard_urls(host: str, port: int, lang: str = DEFAULT_LANG) -> list[str]:
    route = dashboard_path(lang)
    base_url = (
        os.getenv("FACTORIO_AI_WEB_BASE_URL")
        or os.getenv("FACTORIO_DASHBOARD_BASE_URL")
        or DEFAULT_PUBLIC_DASHBOARD_BASE_URL
    )
    return dashboard_urls(host, port, route, base_url=base_url)


def _candidate_blueprint_response(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    layout = state.get("layout_improvement") if isinstance(state.get("layout_improvement"), dict) else {}
    candidates = layout.get("simulation_candidates") if isinstance(layout.get("simulation_candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("candidate_id") or "") != candidate_id:
            continue
        blueprint = candidate.get("blueprint") if isinstance(candidate.get("blueprint"), dict) else {}
        exchange_string = blueprint.get("exchange_string")
        if not isinstance(exchange_string, str) or not exchange_string:
            break
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "label": str(blueprint.get("label") or candidate_id),
            "format": str(blueprint.get("format") or "factorio-blueprint-string"),
            "entity_count": int(blueprint.get("entity_count") or 0),
            "blueprint": exchange_string,
        }
    return {"ok": False, "error": "blueprint candidate not found", "candidate_id": candidate_id}


def clear_dashboard_state_cache() -> None:
    with _DASHBOARD_STATE_CACHE_LOCK:
        _DASHBOARD_STATE_CACHE.clear()
        _DASHBOARD_STATE_REFRESHING.clear()


def build_dashboard_state_cached(
    cfg: AppConfig,
    objective: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ttl = _web_cache_seconds()
    if ttl <= 0:
        return build_dashboard_state(cfg, objective)
    key = _dashboard_cache_key(cfg, objective)
    now = time.monotonic()
    with _DASHBOARD_STATE_CACHE_LOCK:
        cached = _DASHBOARD_STATE_CACHE.get(key)
        if not force_refresh and cached is not None:
            age = now - float(cached.get("stored_at") or 0.0)
            if age <= ttl:
                return _dashboard_cached_state(cached.get("state"), hit=True, age_seconds=age, ttl_seconds=ttl)
            _start_dashboard_cache_refresh_locked(cfg, objective, key)
            return _dashboard_cached_state(cached.get("state"), hit=True, age_seconds=age, ttl_seconds=ttl)

    state = build_dashboard_state(cfg, objective)
    with _DASHBOARD_STATE_CACHE_LOCK:
        _store_dashboard_state_locked(key, state)
    return _dashboard_cached_state(state, hit=False, age_seconds=0.0, ttl_seconds=ttl)


def _start_dashboard_cache_refresh_locked(cfg: AppConfig, objective: str, key: str) -> None:
    if key in _DASHBOARD_STATE_REFRESHING:
        return
    _DASHBOARD_STATE_REFRESHING.add(key)
    threading.Thread(target=_refresh_dashboard_cache, args=(cfg, objective, key), daemon=True).start()


def _refresh_dashboard_cache(cfg: AppConfig, objective: str, key: str) -> None:
    try:
        state = build_dashboard_state(cfg, objective)
        with _DASHBOARD_STATE_CACHE_LOCK:
            _store_dashboard_state_locked(key, state)
    finally:
        with _DASHBOARD_STATE_CACHE_LOCK:
            _DASHBOARD_STATE_REFRESHING.discard(key)


def _store_dashboard_state_locked(key: str, state: dict[str, Any]) -> None:
    _DASHBOARD_STATE_CACHE[key] = {"stored_at": time.monotonic(), "state": state}


def _dashboard_cache_key(cfg: AppConfig, objective: str) -> str:
    return "|".join([str(cfg.runtime_dir), str(cfg.log_dir), objective])


def _dashboard_cached_state(
    state: Any,
    *,
    hit: bool,
    age_seconds: float,
    ttl_seconds: float,
) -> dict[str, Any]:
    output = dict(state) if isinstance(state, dict) else {}
    output["cache"] = {
        "hit": hit,
        "age_seconds": round(max(0.0, age_seconds), 3),
        "ttl_seconds": round(ttl_seconds, 3),
    }
    return output


def _web_cache_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("FACTORIO_AI_WEB_CACHE_SECONDS", str(DEFAULT_WEB_CACHE_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_WEB_CACHE_SECONDS


def build_dashboard_state(cfg: AppConfig, objective: str) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    targets = load_targets(cfg.runtime_dir, objective)
    token_usage = token_usage_summary(cfg.log_dir)
    llm_decisions = llm_decision_summary(cfg.log_dir)
    layout_background = layout_background_summary(cfg.log_dir)
    try:
        observation, adapter = observe_dashboard_state(cfg)
        monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
        layout_improvement = make_layout_improvement_context(observation)
        strategy = annotate_strategy_with_skill_status(
            heuristic_strategy(objective, observation, targets.per_minute),
            runtime_dir=cfg.runtime_dir,
        )
        return {
            "ok": True,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "observation_tick": observation.get("tick"),
            "player": observation.get("player"),
            "agent_marker": observation.get("agent_marker"),
            "adapter": adapter,
            "monitor": monitor,
            "layout_improvement": layout_improvement,
            "layout_background": layout_background,
            "strategy": strategy,
            "token_usage": token_usage,
            "llm_decisions": llm_decisions,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "error": friendly_dashboard_error(exc),
            "layout_background": layout_background,
            "token_usage": token_usage,
            "llm_decisions": llm_decisions,
        }


def layout_background_summary(log_dir: Any, *, limit: int = 20) -> dict[str, Any]:
    path = log_dir / "layout-improvement-background.jsonl"
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict):
                    rows.append(raw)
    entries = rows[-limit:] if limit >= 0 else rows
    return {
        "entries": entries,
        "entry_count": len(rows),
        "latest": entries[-1] if entries else None,
        "log_path": str(path),
    }


def observe_dashboard_state(cfg: AppConfig) -> tuple[dict[str, Any], str]:
    mod_error: Exception | None = None
    try:
        return FactorioController(cfg).observe(), "custom-mod-rcon"
    except Exception as exc:  # noqa: BLE001
        mod_error = exc
    try:
        return ModlessLuaController(cfg).observe(), "no-mod-rcon-lua"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Factorio RCON is offline or neither the custom mod nor the no-mod Lua observation adapter responded. "
            f"custom_mod={type(mod_error).__name__}: {mod_error}; "
            f"no_mod={type(exc).__name__}: {exc}"
        ) from exc


def friendly_dashboard_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if "WinError 10061" in text or "Connection refused" in text or "actively refused" in text:
        return (
            "Factorio RCON server is not running. Start run_factorio_no_mod_server.bat "
            "or run_factorio_watch_gui.bat, then refresh this page."
        )
    return text


def render_dashboard(state: dict[str, Any], lang: str = DEFAULT_LANG) -> str:
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    title = _t(lang, "title")
    if not state.get("ok"):
        return _page(
            title,
            f"""
            <section class="panel">
              <h2>{_t(lang, "connection")}</h2>
              <p class="error">{escape(str(state.get("error") or "unknown error"))}</p>
            </section>
            {_llm_decision_panel(state.get("llm_decisions"), lang)}
            <section class="panel">
              <h2>{_t(lang, "layout_background")}</h2>
              {_layout_background_panel(state.get("layout_background"), lang)}
            </section>
            {_token_usage_panel(state.get("token_usage"), lang)}
            """,
            lang,
            state.get("objective"),
        )

    monitor = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
    strategy = state.get("strategy") if isinstance(state.get("strategy"), dict) else {}
    production = monitor.get("production") if isinstance(monitor.get("production"), list) else []
    throughput_constraints = (
        monitor.get("throughput_constraints") if isinstance(monitor.get("throughput_constraints"), list) else []
    )
    power_networks = monitor.get("power_networks") if isinstance(monitor.get("power_networks"), list) else []
    factory_sites = monitor.get("factory_sites") if isinstance(monitor.get("factory_sites"), list) else []
    logistics_links = monitor.get("logistics_links") if isinstance(monitor.get("logistics_links"), list) else []
    factory_events = monitor.get("factory_events") if isinstance(monitor.get("factory_events"), list) else []
    damage_events = monitor.get("damage_events") if isinstance(monitor.get("damage_events"), list) else []
    threats = monitor.get("threats") if isinstance(monitor.get("threats"), dict) else {}
    bottlenecks = monitor.get("bottlenecks") if isinstance(monitor.get("bottlenecks"), list) else []
    inventory = monitor.get("inventory") if isinstance(monitor.get("inventory"), dict) else {}
    technologies = monitor.get("technology_chain") if isinstance(monitor.get("technology_chain"), list) else []
    dependency = monitor.get("dependency_tree") if isinstance(monitor.get("dependency_tree"), list) else []
    target_status = monitor.get("target_status") if isinstance(monitor.get("target_status"), dict) else {}
    targets = state.get("targets") if isinstance(state.get("targets"), dict) else {}
    targets_per_minute = targets.get("per_minute") if isinstance(targets.get("per_minute"), dict) else {}
    token_usage = state.get("token_usage") if isinstance(state.get("token_usage"), dict) else {}
    llm_decisions = state.get("llm_decisions") if isinstance(state.get("llm_decisions"), dict) else {}
    agent_marker = state.get("agent_marker") if isinstance(state.get("agent_marker"), dict) else {}
    layout_improvement = state.get("layout_improvement") if isinstance(state.get("layout_improvement"), dict) else {}
    layout_background = state.get("layout_background") if isinstance(state.get("layout_background"), dict) else {}

    body = f"""
    <section class="summary">
      <div>
        <span class="label">{_t(lang, "objective")}</span>
        <strong>{escape(str(state.get("objective") or ""))}</strong>
      </div>
      <div>
        <span class="label">{_t(lang, "tick")}</span>
        <strong>{escape(str(state.get("observation_tick") or ""))}</strong>
      </div>
      <div>
        <span class="label">{_t(lang, "updated")}</span>
        <strong>{escape(_format_kst_timestamp(state.get("updated_at")))}</strong>
      </div>
      <div>
        <span class="label">Adapter</span>
        <strong>{escape(str(state.get("adapter") or ""))}</strong>
      </div>
    </section>

    {_agent_activity_panel(agent_marker, state.get("player"), lang)}

    <section class="panel">
      <h2>{_t(lang, "strategic_recommendation")}</h2>
      <div class="strategy">
        <strong>{escape(str(strategy.get("selected_skill") or ""))}</strong>
        <span>{_t(lang, "priority")} {escape(str(strategy.get("priority") or ""))}</span>
      </div>
      <p>{escape(str(strategy.get("reason") or ""))}</p>
      {_list(_t(lang, "blockers"), strategy.get("blockers"))}
      {_skill_status(strategy.get("skill_status"), lang)}
      <p class="muted">{_target_satisfied_text(target_status, lang)}</p>
    </section>

    {_llm_decision_panel(llm_decisions, lang)}

    <section class="panel">
      <h2>{_t(lang, "desired_targets")}</h2>
      {_target_form(targets_per_minute, target_status, lang, state.get("objective"))}
    </section>

    <section class="grid">
      <div class="panel">
        <h2>{_t(lang, "estimated_production")}</h2>
        {_production_table(production, lang)}
      </div>
      <div class="panel">
        <h2>{_t(lang, "bottlenecks")}</h2>
        {_bottleneck_table(bottlenecks, lang)}
      </div>
    </section>

    <section class="panel">
      <h2>{_t(lang, "throughput_constraints")}</h2>
      {_throughput_constraint_table(throughput_constraints, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "power_networks")}</h2>
      {_power_network_table(power_networks, lang)}
    </section>

    <section class="grid">
      <div class="panel">
        <h2>{_t(lang, "threats")}</h2>
        {_threat_summary(threats, lang)}
      </div>
      <div class="panel">
        <h2>{_t(lang, "recent_damage")}</h2>
        {_damage_event_table(damage_events, lang)}
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>{_t(lang, "factory_sites")}</h2>
        {_factory_site_table(factory_sites, lang)}
      </div>
      <div class="panel">
        <h2>{_t(lang, "logistics_links")}</h2>
        {_logistics_link_table(logistics_links, lang)}
      </div>
    </section>

    <section class="panel">
      <h2>{_t(lang, "layout_improvement")}</h2>
      {_layout_improvement_panel(layout_improvement, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "layout_background")}</h2>
      {_layout_background_panel(layout_background, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "factory_events")}</h2>
      {_factory_event_table(factory_events, lang)}
    </section>

    {_token_usage_panel(token_usage, lang)}

    <section class="grid">
      <div class="panel">
        <h2>{_t(lang, "inventory")}</h2>
        {_key_value_table(inventory, lang)}
      </div>
      <div class="panel">
        <h2>{_t(lang, "technology_chain")}</h2>
        {_tech_table(technologies, lang)}
      </div>
    </section>

    <section class="panel">
      <h2>{_t(lang, "dependency_tree")}</h2>
      <pre>{escape(json.dumps(dependency, ensure_ascii=False, indent=2))}</pre>
    </section>
    """
    return _page(title, body, lang, state.get("objective"))


def _page(title: str, body: str, lang: str, objective: Any = None) -> str:
    objective_text = str(objective or "")
    return f"""<!doctype html>
<html lang="{escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: "Segoe UI", Arial, sans-serif;
      background: #101214;
      color: #e8e8e8;
    }}
    body {{
      margin: 0;
      background: #101214;
    }}
    main {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 24px;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 650;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      font-weight: 650;
      color: #f0c46c;
    }}
    .lang-switch {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 0 0 auto;
    }}
    .lang-switch a {{
      border: 1px solid #35404b;
      border-radius: 4px;
      color: #d8e0e8;
      padding: 5px 8px;
      text-decoration: none;
      font-size: 12px;
      line-height: 1;
    }}
    .lang-switch a.active {{
      border-color: #6b8f3f;
      background: #27451c;
      color: #fff;
    }}
    .summary, .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .summary > div, .panel {{
      border: 1px solid #2d343b;
      background: #171b20;
      border-radius: 6px;
      padding: 14px;
    }}
    .label {{
      display: block;
      color: #9aa4af;
      font-size: 12px;
      margin-bottom: 6px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid #2a3036;
      padding: 7px 6px;
      vertical-align: middle;
    }}
    th {{
      color: #9aa4af;
      font-weight: 600;
    }}
    pre {{
      overflow: auto;
      margin: 0;
      padding: 12px;
      background: #0c0e10;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .strategy {{
      display: flex;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .strategy span, .muted {{
      color: #9aa4af;
    }}
    .error {{
      color: #ff7a7a;
    }}
    .item-name {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 26px;
    }}
    .item-icon {{
      width: 24px;
      height: 24px;
      object-fit: contain;
      image-rendering: pixelated;
      flex: 0 0 24px;
    }}
    input {{
      width: 92px;
      box-sizing: border-box;
      border: 1px solid #3a424c;
      border-radius: 4px;
      background: #0f1216;
      color: #f2f2f2;
      padding: 6px;
      font: inherit;
    }}
    button {{
      border: 1px solid #5b7a33;
      border-radius: 4px;
      background: #4f8f2f;
      color: white;
      padding: 8px 12px;
      font: inherit;
      cursor: pointer;
    }}
    .copy-blueprint {{
      white-space: nowrap;
      padding: 6px 9px;
      font-size: 12px;
    }}
    .actions {{
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .metric {{
      border: 1px solid #2a3036;
      border-radius: 4px;
      background: #101419;
      padding: 10px;
    }}
    .metric strong {{
      display: block;
      font-size: 18px;
    }}
    .token-chart {{
      display: block;
      width: 100%;
      height: 220px;
      margin: 8px 0 12px;
      border: 1px solid #2a3036;
      border-radius: 4px;
      background: #0c0e10;
    }}
    .token-chart .gridline {{
      stroke: #222a31;
      stroke-width: 1;
    }}
    .token-chart .usage-line {{
      fill: none;
      stroke: #75b843;
      stroke-width: 3;
    }}
    .token-chart .usage-point {{
      fill: #f0c46c;
      stroke: #101214;
      stroke-width: 1;
    }}
    .agent-layout {{
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }}
    .agent-map {{
      width: 112px;
      height: 112px;
      border: 1px solid #2a3036;
      border-radius: 4px;
      background: #0c0e10;
    }}
    .agent-map rect {{
      fill: #111820;
      stroke: #2a3036;
      stroke-width: 1;
    }}
    .agent-map line {{
      stroke: #62788f;
      stroke-width: 2;
      stroke-dasharray: 4 3;
    }}
    .agent-map .agent-current {{
      fill: #f0c46c;
      stroke: #101214;
      stroke-width: 1;
    }}
    .agent-map .agent-target {{
      fill: #75b843;
      stroke: #101214;
      stroke-width: 1;
    }}
    .agent-map text {{
      fill: #d8e0e8;
      font-size: 9px;
      font-weight: 700;
    }}
    @media (max-width: 640px) {{
      main {{
        padding: 14px;
      }}
      .topbar {{
        align-items: flex-start;
        flex-direction: column;
      }}
      table {{
        font-size: 12px;
      }}
      .agent-layout {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <h1>{escape(title)}</h1>
      {_language_switch(lang, objective_text)}
    </div>
    {body}
  </main>
  {_copy_blueprint_script(lang)}
</body>
</html>"""


def _language_switch(lang: str, objective: str) -> str:
    en_class = "active" if lang == "en" else ""
    ko_class = "active" if lang == "ko" else ""
    return (
        f"<nav class=\"lang-switch\" aria-label=\"{escape(_t(lang, 'language'))}\">"
        f"<a class=\"{en_class}\" href=\"{escape(dashboard_path('en', objective))}\">EN</a>"
        f"<a class=\"{ko_class}\" href=\"{escape(dashboard_path('ko', objective))}\">KR</a>"
        "</nav>"
    )


def _copy_blueprint_script(lang: str) -> str:
    copied = json.dumps(_t(lang, "copied"))
    failed = json.dumps(_t(lang, "copy_failed"))
    return f"""<script>
(() => {{
  const copyText = async (text) => {{
    if (navigator.clipboard && window.isSecureContext) {{
      await navigator.clipboard.writeText(text);
      return;
    }}
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
  }};
  document.addEventListener("click", async (event) => {{
    const button = event.target.closest(".copy-blueprint");
    if (!button) {{
      return;
    }}
    event.preventDefault();
    const candidateId = button.getAttribute("data-candidate-id") || "";
    const params = new URLSearchParams(window.location.search);
    const apiParams = new URLSearchParams({{ candidate_id: candidateId }});
    const objective = params.get("objective");
    if (objective) {{
      apiParams.set("objective", objective);
    }}
    const original = button.textContent;
    try {{
      const response = await fetch("{BLUEPRINT_API_ROUTE}?" + apiParams.toString(), {{ cache: "no-store" }});
      const data = await response.json();
      if (!response.ok || !data.ok || !data.blueprint) {{
        throw new Error(data.error || "blueprint unavailable");
      }}
      await copyText(data.blueprint);
      button.textContent = {copied};
    }} catch (error) {{
      button.textContent = {failed};
    }} finally {{
      window.setTimeout(() => {{
        button.textContent = original;
      }}, 1500);
    }}
  }});
}})();
</script>"""


def _production_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_production')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{_item_cell(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('usable_per_minute') if row.get('usable_per_minute') is not None else row.get('per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('producers') or 0))}</td>"
        f"<td>{escape(str(row.get('confidence') or 0))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'per_min')}</th>"
        f"<th>{_t(lang, 'usable_per_min')}</th>"
        f"<th>{_t(lang, 'producers')}</th><th>{_t(lang, 'confidence')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _bottleneck_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_bottleneck')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{_item_cell(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        f"<td>{escape(str(row.get('stock') or 0))}</td>"
        f"<td>{escape(str(row.get('estimated_per_minute') or 0))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'reason')}</th>"
        f"<th>{_t(lang, 'stock')}</th><th>{_t(lang, 'per_min')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _throughput_constraint_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_throughput_constraints')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{_item_cell(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('required_per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('available_per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('bottleneck') or ''))}</td>"
        f"<td>{escape('; '.join(str(item) for item in row.get('notes', [])[:2]))}</td>"
        "</tr>"
        for row in rows[:40]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'required')}</th>"
        f"<th>{_t(lang, 'available')}</th><th>{_t(lang, 'bottlenecks')}</th>"
        f"<th>{_t(lang, 'reason')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _power_network_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_power_networks')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('network_id') or ''))}</td>"
        f"<td>{escape(str(row.get('status') or ''))}</td>"
        f"<td>{escape(str(row.get('generation_kw') or 0))}</td>"
        f"<td>{escape(str(row.get('demand_kw') or 0))}</td>"
        f"<td>{escape(str(row.get('satisfaction') or 0))}</td>"
        f"<td>{escape(str(row.get('unconnected_consumers') or 0))}</td>"
        f"<td>{escape('; '.join(str(item) for item in row.get('notes', [])[:2]))}</td>"
        "</tr>"
        for row in rows[:40]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'network')}</th><th>{_t(lang, 'status')}</th>"
        f"<th>{_t(lang, 'generation_kw')}</th><th>{_t(lang, 'demand_kw')}</th>"
        f"<th>{_t(lang, 'satisfaction')}</th><th>{_t(lang, 'unconnected')}</th>"
        f"<th>{_t(lang, 'reason')}</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _layout_improvement_panel(layout: dict[str, Any], lang: str) -> str:
    if not layout:
        return f"<p class=\"muted\">{_t(lang, 'no_layout_improvement')}</p>"
    issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    opportunities = layout.get("opportunities") if isinstance(layout.get("opportunities"), list) else []
    candidates = layout.get("simulation_candidates") if isinstance(layout.get("simulation_candidates"), list) else []
    if not issues and not opportunities and not candidates:
        return f"<p class=\"muted\">{_t(lang, 'no_layout_improvement')}</p>"
    return (
        f"<h3>{_t(lang, 'layout_issues')}</h3>{_layout_item_table(issues, lang)}"
        f"<h3>{_t(lang, 'layout_opportunities')}</h3>{_layout_item_table(opportunities, lang)}"
        f"<h3>{_t(lang, 'layout_candidates')}</h3>{_layout_candidate_table(candidates, lang)}"
    )


def _layout_item_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_layout_improvement')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('kind') or ''))}</td>"
        f"<td>{escape(str(row.get('severity') or 0))}</td>"
        f"<td>{_item_cell(str(row.get('item') or '')) if row.get('item') else ''}</td>"
        f"<td>{escape(str(row.get('site_id') or ''))}</td>"
        f"<td>{escape(str(row.get('detail') or ''))}</td>"
        f"<td>{escape(str(row.get('recommendation') or ''))}</td>"
        "</tr>"
        for row in rows[:20]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'kind')}</th><th>{_t(lang, 'score')}</th>"
        f"<th>{_t(lang, 'item')}</th><th>Site</th><th>{_t(lang, 'reason')}</th>"
        f"<th>{_t(lang, 'recommended_actions')}</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _layout_candidate_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_layout_improvement')}</p>"
    body = ""
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        simulation = row.get("simulation") if isinstance(row.get("simulation"), dict) else {}
        body += (
            "<tr>"
            f"<td>{escape(str(row.get('candidate_id') or ''))}</td>"
            f"<td>{escape(str(row.get('target_pattern') or ''))}</td>"
            f"<td>{escape(str(simulation.get('score') or 0))}</td>"
            f"<td>{escape(_compact_json_text(simulation.get('before')))}</td>"
            f"<td>{escape(_compact_json_text(simulation.get('after')))}</td>"
            f"<td>{escape(_compact_json_text(simulation.get('delta')))}</td>"
            f"<td>{escape(_t(lang, 'not_applied') if row.get('not_applied') else '')}</td>"
            f"<td>{_blueprint_copy_cell(row, lang)}</td>"
            "</tr>"
        )
    return (
        f"<table><thead><tr><th>{_t(lang, 'candidate')}</th><th>{_t(lang, 'pattern')}</th>"
        f"<th>{_t(lang, 'score')}</th><th>{_t(lang, 'before')}</th><th>{_t(lang, 'after')}</th>"
        f"<th>{_t(lang, 'delta')}</th><th>{_t(lang, 'status')}</th><th>{_t(lang, 'blueprint')}</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


def _blueprint_copy_cell(row: dict[str, Any], lang: str) -> str:
    blueprint = row.get("blueprint") if isinstance(row.get("blueprint"), dict) else {}
    if not isinstance(blueprint.get("exchange_string"), str) or not blueprint.get("exchange_string"):
        return ""
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate_id:
        return ""
    label = str(blueprint.get("label") or candidate_id)
    return (
        f"<button type=\"button\" class=\"copy-blueprint\" "
        f"data-candidate-id=\"{escape(candidate_id, quote=True)}\" "
        f"title=\"{escape(label, quote=True)}\">{escape(_t(lang, 'copy_blueprint'))}</button>"
    )


def _layout_background_panel(value: Any, lang: str) -> str:
    if not isinstance(value, dict):
        return f"<p class=\"muted\">{_t(lang, 'no_layout_background')}</p>"
    rows = value.get("entries") if isinstance(value.get("entries"), list) else []
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_layout_background')}</p>"
    body = ""
    for row in rows[-20:]:
        if not isinstance(row, dict):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        detail = (
            row.get("error")
            or result.get("reasoning")
            or result.get("next_simulation_focus")
            or row.get("block_reason")
            or row.get("task")
            or row.get("state")
            or ""
        )
        body += (
            "<tr>"
            f"<td>{escape(_format_kst_timestamp(row.get('time')))}</td>"
            f"<td>{escape(str(row.get('event') or ''))}</td>"
            f"<td>{escape(str(row.get('active_skill') or ''))}</td>"
            f"<td>{escape(str(result.get('source') or row.get('mode') or ''))}</td>"
            f"<td>{escape(str(result.get('score') or ''))}</td>"
            f"<td>{escape(str(result.get('selected_candidate_id') or ''))}</td>"
            f"<td>{escape(str(detail))}</td>"
            "</tr>"
        )
    return (
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'event')}</th>"
        f"<th>{_t(lang, 'active_skill')}</th><th>{_t(lang, 'source')}</th>"
        f"<th>{_t(lang, 'score')}</th><th>{_t(lang, 'candidate')}</th>"
        f"<th>{_t(lang, 'reason')}</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _compact_json_text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 180 else text[:177] + "..."


def _factory_site_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_sites')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('kind') or ''))}</td>"
        f"<td>{_item_cell(str(row.get('item') or '')) if row.get('item') else ''}</td>"
        f"<td>{escape(str(row.get('status') or ''))}</td>"
        f"<td>{escape(_position_text(row.get('position')))}</td>"
        f"<td>{escape(str(row.get('automation_level') or ''))}</td>"
        f"<td>{escape(', '.join(str(item) for item in row.get('machines', [])[:5]))}</td>"
        "</tr>"
        for row in rows[:80]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'kind')}</th><th>{_t(lang, 'item')}</th>"
        f"<th>{_t(lang, 'status')}</th><th>{_t(lang, 'position')}</th>"
        f"<th>{_t(lang, 'automation')}</th><th>{_t(lang, 'machines')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _logistics_link_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_links')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('kind') or ''))}</td>"
        f"<td>{_item_cell(str(row.get('item') or '')) if row.get('item') else ''}</td>"
        f"<td>{escape(str(row.get('from_site') or ''))}</td>"
        f"<td>{escape(str(row.get('to_site') or ''))}</td>"
        f"<td>{escape(str(row.get('status') or ''))}</td>"
        f"<td>{escape(str(row.get('length_tiles') or 0))}</td>"
        "</tr>"
        for row in rows[:80]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'kind')}</th><th>{_t(lang, 'item')}</th>"
        f"<th>{_t(lang, 'from')}</th><th>{_t(lang, 'to')}</th>"
        f"<th>{_t(lang, 'status')}</th><th>{_t(lang, 'length')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _factory_event_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_factory_events')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('tick') or ''))}</td>"
        f"<td>{escape(str(row.get('actor') or ''))}</td>"
        f"<td>{escape(str(row.get('action') or ''))}</td>"
        f"<td>{escape(str(row.get('entity') or ''))}</td>"
        f"<td>{escape(_position_text(row.get('position')))}</td>"
        "</tr>"
        for row in rows[:40]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'tick')}</th><th>{_t(lang, 'actor')}</th>"
        f"<th>{_t(lang, 'action')}</th><th>{_t(lang, 'item')}</th>"
        f"<th>{_t(lang, 'position')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _threat_summary(threats: dict[str, Any], lang: str) -> str:
    if not threats:
        return f"<p class=\"muted\">{_t(lang, 'no_threats')}</p>"
    recommended = threats.get("recommended_actions") if isinstance(threats.get("recommended_actions"), list) else []
    rows = [
        (_t(lang, "danger_level"), threats.get("danger_level")),
        (_t(lang, "enemy_count"), threats.get("enemy_count")),
        (_t(lang, "nearest_enemy"), _entity_summary(threats.get("nearest_enemy"))),
        (_t(lang, "nearest_spawner"), _entity_summary(threats.get("nearest_spawner"))),
        (_t(lang, "armed_turrets"), threats.get("armed_gun_turret_count")),
        (_t(lang, "unarmed_turrets"), threats.get("unarmed_gun_turret_count")),
        (_t(lang, "recent_damage"), threats.get("recent_damage_count")),
        (_t(lang, "spawner_pollution"), threats.get("max_spawner_pollution")),
        (_t(lang, "recommended_actions"), "; ".join(str(item) for item in recommended[:4])),
    ]
    body = "".join(
        f"<tr><th>{escape(str(label))}</th><td>{escape(str(value or ''))}</td></tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _entity_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    name = str(value.get("name") or "")
    entity_type = str(value.get("type") or "")
    distance = value.get("distance")
    position = _position_text(value.get("position"))
    parts = [item for item in [name, entity_type, f"{distance} tiles" if distance is not None else "", position] if item]
    return " / ".join(parts)


def _damage_event_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_recent_damage')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('tick') or ''))}</td>"
        f"<td>{escape(str(row.get('action') or ''))}</td>"
        f"<td>{escape(str(row.get('entity') or ''))}</td>"
        f"<td>{escape(str(row.get('cause') or row.get('cause_force') or ''))}</td>"
        f"<td>{escape(str(row.get('damage') or ''))}</td>"
        f"<td>{escape(str(row.get('health') or ''))}</td>"
        f"<td>{escape(_position_text(row.get('position')))}</td>"
        "</tr>"
        for row in rows[:40]
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'tick')}</th><th>{_t(lang, 'action')}</th>"
        f"<th>{_t(lang, 'item')}</th><th>{_t(lang, 'cause')}</th>"
        f"<th>{_t(lang, 'damage')}</th><th>{_t(lang, 'health')}</th>"
        f"<th>{_t(lang, 'position')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _agent_activity_panel(marker: dict[str, Any], player: Any, lang: str) -> str:
    if not marker:
        if isinstance(player, dict):
            marker = {
                "kind": player.get("kind"),
                "name": player.get("name"),
                "position": player.get("position"),
                "target_position": player.get("position"),
                "last_action": "observe",
                "detail": "current observed agent position",
            }
        else:
            return (
                "<section class=\"panel\">"
                f"<h2>{_t(lang, 'agent_activity')}</h2>"
                f"<p class=\"muted\">{_t(lang, 'no_agent_activity')}</p>"
                "</section>"
            )
    position = marker.get("position")
    target = marker.get("target_position") or position
    rows = [
        (_t(lang, "agent_kind"), f"{marker.get('kind') or ''} {marker.get('name') or ''}".strip()),
        (_t(lang, "agent_position"), _position_text(position)),
        (_t(lang, "agent_target"), _position_text(target)),
        (_t(lang, "last_action"), marker.get("last_action")),
        (_t(lang, "last_detail"), marker.get("detail")),
        (_t(lang, "tick"), marker.get("tick")),
    ]
    body = "".join(
        f"<tr><th>{escape(str(label))}</th><td>{escape(str(value or ''))}</td></tr>"
        for label, value in rows
    )
    return (
        "<section class=\"panel agent-panel\">"
        f"<h2>{_t(lang, 'agent_activity')}</h2>"
        "<div class=\"agent-layout\">"
        f"{_agent_activity_svg(position, target)}"
        f"<table><tbody>{body}</tbody></table>"
        "</div>"
        "</section>"
    )


def _agent_activity_svg(position: Any, target: Any) -> str:
    current = _position_pair(position)
    goal = _position_pair(target)
    if current is None and goal is None:
        return ""
    if current is None:
        current = goal
    if goal is None:
        goal = current
    assert current is not None and goal is not None
    min_x = min(current[0], goal[0])
    max_x = max(current[0], goal[0])
    min_y = min(current[1], goal[1])
    max_y = max(current[1], goal[1])
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)

    def scale(point: tuple[float, float]) -> tuple[float, float]:
        x = 12 + ((point[0] - min_x) / span_x) * 76
        y = 88 - ((point[1] - min_y) / span_y) * 76
        return x, y

    cx, cy = scale(current)
    tx, ty = scale(goal)
    return (
        "<svg class=\"agent-map\" viewBox=\"0 0 100 100\" role=\"img\" aria-label=\"AI position map\">"
        "<rect x=\"1\" y=\"1\" width=\"98\" height=\"98\" rx=\"4\"/>"
        f"<line x1=\"{cx:.1f}\" y1=\"{cy:.1f}\" x2=\"{tx:.1f}\" y2=\"{ty:.1f}\"/>"
        f"<circle class=\"agent-current\" cx=\"{cx:.1f}\" cy=\"{cy:.1f}\" r=\"5\"/>"
        f"<circle class=\"agent-target\" cx=\"{tx:.1f}\" cy=\"{ty:.1f}\" r=\"4\"/>"
        "<text x=\"8\" y=\"14\">AI</text>"
        "</svg>"
    )


def _token_usage_panel(value: Any, lang: str) -> str:
    usage = value if isinstance(value, dict) else {}
    samples = usage.get("samples") if isinstance(usage.get("samples"), list) else []
    if not samples:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'token_usage')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_token_usage')}</p>"
            "</section>"
        )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'token_usage')}</h2>"
        "<div class=\"metrics\">"
        f"{_metric(_t(lang, 'latest_tokens'), _format_int(usage.get('latest_tokens')))}"
        f"{_metric(_t(lang, 'total_delta_tokens'), _format_int(usage.get('total_delta_tokens')))}"
        f"{_metric(_t(lang, 'sample_count'), _format_int(usage.get('sample_count')))}"
        f"{_metric(_t(lang, 'last_sample'), _format_kst_timestamp(usage.get('updated_at')))}"
        "</div>"
        f"{_token_usage_svg(samples)}"
        f"{_token_usage_table(samples[-8:], lang)}"
        "</section>"
    )


def _llm_decision_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    if not entries:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'llm_decisions')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_llm_decisions')}</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td>{escape(_format_kst_timestamp(row.get('timestamp')))}</td>"
        f"<td>{escape(str(row.get('provider') or ''))}</td>"
        f"<td>{escape(str(row.get('source') or ''))}</td>"
        f"<td>{escape(str(row.get('selected_skill') or ''))}</td>"
        f"<td>{escape(str(row.get('priority') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        f"<td>{escape('; '.join(str(item) for item in row.get('blockers', [])[:4]))}</td>"
        f"<td>{escape(str(row.get('error') or ''))}</td>"
        f"<td>{escape(str(row.get('latency_ms') or 0))}</td>"
        "</tr>"
        for row in reversed(entries[-20:])
        if isinstance(row, dict)
    )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'llm_decisions')}</h2>"
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'provider')}</th>"
        f"<th>{_t(lang, 'source')}</th><th>{_t(lang, 'executor')}</th><th>{_t(lang, 'priority')}</th>"
        f"<th>{_t(lang, 'reason')}</th><th>{_t(lang, 'blockers')}</th><th>{_t(lang, 'error')}</th>"
        f"<th>{_t(lang, 'latency_ms')}</th></tr></thead><tbody>{rows}</tbody></table>"
        "</section>"
    )


def _metric(label: str, value: str) -> str:
    return (
        "<div class=\"metric\">"
        f"<span class=\"label\">{escape(label)}</span>"
        f"<strong>{escape(value)}</strong>"
        "</div>"
    )


def _token_usage_svg(samples: list[Any]) -> str:
    rows: list[tuple[int, float | None]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        try:
            value = int(sample.get("tokens_used") or 0)
        except (TypeError, ValueError):
            continue
        rows.append((value, _timestamp_seconds(sample.get("timestamp"))))
    if not rows:
        return ""
    numeric = [value for value, _timestamp in rows]
    valid_times = [timestamp for _value, timestamp in rows if timestamp is not None]
    width = 760
    height = 240
    left = 42
    right = 16
    top = 16
    bottom = 46
    plot_width = width - left - right
    plot_height = height - top - bottom
    min_value = min(numeric)
    max_value = max(numeric)
    span = max(max_value - min_value, 1)
    count = max(len(rows) - 1, 1)
    min_time = min(valid_times) if len(valid_times) >= 2 else None
    max_time = max(valid_times) if len(valid_times) >= 2 else None
    time_span = (max_time - min_time) if min_time is not None and max_time is not None else 0
    points = []
    circles = []
    for index, (value, timestamp) in enumerate(rows):
        if min_time is not None and timestamp is not None and time_span > 0:
            x = left + (plot_width * (timestamp - min_time) / time_span)
        else:
            x = left + (plot_width * index / count)
        y = top + plot_height - (plot_height * (value - min_value) / span)
        points.append(f"{x:.1f},{y:.1f}")
        circles.append(f"<circle class=\"usage-point\" cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"3\" />")
    gridlines = "".join(
        f"<line class=\"gridline\" x1=\"{left}\" y1=\"{top + plot_height * step / 4:.1f}\" "
        f"x2=\"{width - right}\" y2=\"{top + plot_height * step / 4:.1f}\" />"
        for step in range(5)
    )
    axis_labels = ""
    if min_time is not None and max_time is not None and time_span > 0:
        axis_labels = (
            f"<text x=\"{left}\" y=\"{height - 12}\" fill=\"#9aa4af\" font-size=\"11\">"
            f"{escape(_format_chart_time(min_time))}</text>"
            f"<text x=\"{width - right}\" y=\"{height - 12}\" fill=\"#9aa4af\" font-size=\"11\" text-anchor=\"end\">"
            f"{escape(_format_chart_time(max_time))}</text>"
        )
    return (
        f"<svg class=\"token-chart\" viewBox=\"0 0 {width} {height}\" role=\"img\" "
        "aria-label=\"Codex token usage line chart\">"
        f"{gridlines}"
        f"<text x=\"{left}\" y=\"{top + plot_height - 4:.1f}\" fill=\"#9aa4af\" font-size=\"11\">{escape(_format_int(min_value))}</text>"
        f"<text x=\"{left}\" y=\"14\" fill=\"#9aa4af\" font-size=\"11\">{escape(_format_int(max_value))}</text>"
        f"{axis_labels}"
        f"<polyline class=\"usage-line\" points=\"{' '.join(points)}\" />"
        f"{''.join(circles)}"
        "</svg>"
    )


def _token_usage_table(samples: list[Any], lang: str) -> str:
    body = "".join(
        "<tr>"
        f"<td>{escape(_format_kst_timestamp(row.get('timestamp')))}</td>"
        f"<td>{escape(str(row.get('label') or ''))}</td>"
        f"<td>{escape(_format_int(row.get('delta_tokens')))}</td>"
        f"<td>{escape(_format_int(row.get('tokens_used')))}</td>"
        "</tr>"
        for row in samples
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'reason')}</th>"
        f"<th>{_t(lang, 'total_delta_tokens')}</th><th>{_t(lang, 'latest_tokens')}</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _target_form(targets: dict[str, Any], target_status: dict[str, Any], lang: str, objective: Any) -> str:
    status_rows = (
        {
            str(row.get("item")): row
            for row in target_status.get("items", [])
            if isinstance(row, dict) and row.get("item")
        }
        if isinstance(target_status.get("items"), list)
        else {}
    )
    rows = []
    for item in TARGET_ITEMS:
        target = float(targets.get(item) or 0.0)
        status = status_rows.get(item, {})
        estimated = status.get("estimated_per_minute", 0)
        isolated = status.get("isolated_per_minute", 0)
        deficit = status.get("deficit_per_minute", 0)
        rows.append(
            "<tr>"
            f"<td>{_item_cell(item)}</td>"
            f"<td><input name=\"{escape(item)}\" type=\"number\" min=\"0\" step=\"1\" value=\"{escape(str(target))}\"></td>"
            f"<td>{escape(str(estimated))}</td>"
            f"<td>{escape(str(isolated))}</td>"
            f"<td>{escape(str(deficit))}</td>"
            "</tr>"
        )
    return (
        f"<form method=\"post\" action=\"{escape(dashboard_path(lang, str(objective or '')))}\">"
        f"<input type=\"hidden\" name=\"lang\" value=\"{escape(lang)}\">"
        f"<input type=\"hidden\" name=\"objective\" value=\"{escape(str(objective or ''))}\">"
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'target_per_min')}</th>"
        f"<th>{_t(lang, 'estimated_per_min')}</th><th>{_t(lang, 'isolated_per_min')}</th>"
        f"<th>{_t(lang, 'deficit_per_min')}</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<div class=\"actions\"><button type=\"submit\">{_t(lang, 'save_targets')}</button></div>"
        "</form>"
    )


def _target_satisfied_text(target_status: dict[str, Any], lang: str) -> str:
    if not target_status:
        return _t(lang, "no_targets")
    if target_status.get("all_satisfied"):
        return _t(lang, "targets_satisfied")
    return _t(lang, "targets_unsatisfied")


def _skill_status(value: Any, lang: str) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("implemented"):
        return f"<p class=\"muted\">{_t(lang, 'executor')}: {escape(str(value.get('executor') or ''))}</p>"
    return f"<p class=\"error\">{_t(lang, 'executor_missing')}</p>"


def _key_value_table(values: dict[str, Any], lang: str) -> str:
    if not values:
        return f"<p class=\"muted\">{_t(lang, 'no_inventory')}</p>"
    body = "".join(
        f"<tr><td>{_item_cell(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in sorted(values.items())
    )
    return f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'count')}</th></tr></thead><tbody>{body}</tbody></table>"


def _tech_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_tech')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('name') or ''))}</td>"
        f"<td>{escape(', '.join(str(item) for item in row.get('prerequisites', [])))}</td>"
        f"<td>{escape(', '.join(str(item) for item in row.get('unlocks', [])))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'technology')}</th><th>{_t(lang, 'prerequisites')}</th>"
        f"<th>{_t(lang, 'unlocks')}</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _list(title: str, values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    items = "".join(f"<li>{escape(str(item))}</li>" for item in values)
    return f"<div><span class=\"label\">{escape(title)}</span><ul>{items}</ul></div>"


def _item_cell(item: str) -> str:
    item = item.strip()
    if not item:
        return ""
    src = f"{ICON_ROUTE_PREFIX}{quote(item)}.png"
    return (
        "<span class=\"item-name\">"
        f"<img class=\"item-icon\" src=\"{escape(src)}\" alt=\"\" loading=\"lazy\" "
        "onerror=\"this.style.display='none'\">"
        f"<span>{escape(item)}</span>"
        "</span>"
    )


def _position_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    try:
        return f"{float(value.get('x') or 0.0):.1f}, {float(value.get('y') or 0.0):.1f}"
    except (TypeError, ValueError):
        return ""


def _position_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("x") or 0.0), float(value.get("y") or 0.0)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_kst_timestamp(value: Any) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _timestamp_seconds(value: Any) -> float | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return parsed.timestamp()


def _format_chart_time(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, KST).strftime("%m-%d %H:%M")


def _format_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _t(lang: str, key: str) -> str:
    return TEXT.get(lang, TEXT[DEFAULT_LANG]).get(key, TEXT[DEFAULT_LANG].get(key, key))
