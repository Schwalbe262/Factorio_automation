from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from .config import AppConfig
from .controller import FactorioController
from .item_icons import read_item_icon_png
from .monitor import summarize_factory
from .networking import dashboard_urls
from .modless_lua import ModlessLuaController
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy
from .targets import TARGET_ITEMS, load_targets, parse_target_form, save_targets


FACTORIO_ROUTE = "/factorio"
LEGACY_FACTORIO_ROUTE = "/팩토리오"
FACTORIO_ROUTES = {FACTORIO_ROUTE, LEGACY_FACTORIO_ROUTE}
ICON_ROUTE_PREFIX = "/factorio/icon/"
API_ROUTE = "/api/factorio"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = {"en", "ko"}
DEFAULT_PUBLIC_DASHBOARD_BASE_URL = "http://27.115.156.173:8787"


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
        "target_per_min": "Target / min",
        "estimated_per_min": "Estimated / min",
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
    },
    "ko": {
        "title": "팩토리오 AI 공장 모니터",
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
        "target_per_min": "목표 / 분",
        "estimated_per_min": "추정 / 분",
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
            state = build_dashboard_state(cfg, values.get("objective", [default_objective])[0])
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
                state = build_dashboard_state(cfg, objective)
                body = render_dashboard(state, lang=lang)
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == API_ROUTE:
                state = build_dashboard_state(cfg, objective)
                self._send(
                    200,
                    json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
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


def build_dashboard_state(cfg: AppConfig, objective: str) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    targets = load_targets(cfg.runtime_dir, objective)
    try:
        observation, adapter = observe_dashboard_state(cfg)
        monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
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
            "adapter": adapter,
            "monitor": monitor,
            "strategy": strategy,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "error": friendly_dashboard_error(exc),
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
        <strong>{escape(str(state.get("updated_at") or ""))}</strong>
      </div>
      <div>
        <span class="label">Adapter</span>
        <strong>{escape(str(state.get("adapter") or ""))}</strong>
      </div>
    </section>

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
      <h2>{_t(lang, "factory_events")}</h2>
      {_factory_event_table(factory_events, lang)}
    </section>

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
    .actions {{
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
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


def _production_table(rows: list[Any], lang: str) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_production')}</p>"
    body = "".join(
        "<tr>"
        f"<td>{_item_cell(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('producers') or 0))}</td>"
        f"<td>{escape(str(row.get('confidence') or 0))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return (
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'per_min')}</th>"
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
        deficit = status.get("deficit_per_minute", 0)
        rows.append(
            "<tr>"
            f"<td>{_item_cell(item)}</td>"
            f"<td><input name=\"{escape(item)}\" type=\"number\" min=\"0\" step=\"1\" value=\"{escape(str(target))}\"></td>"
            f"<td>{escape(str(estimated))}</td>"
            f"<td>{escape(str(deficit))}</td>"
            "</tr>"
        )
    return (
        f"<form method=\"post\" action=\"{escape(dashboard_path(lang, str(objective or '')))}\">"
        f"<input type=\"hidden\" name=\"lang\" value=\"{escape(lang)}\">"
        f"<input type=\"hidden\" name=\"objective\" value=\"{escape(str(objective or ''))}\">"
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'target_per_min')}</th>"
        f"<th>{_t(lang, 'estimated_per_min')}</th><th>{_t(lang, 'deficit_per_min')}</th></tr></thead>"
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


def _t(lang: str, key: str) -> str:
    return TEXT.get(lang, TEXT[DEFAULT_LANG]).get(key, TEXT[DEFAULT_LANG].get(key, key))
