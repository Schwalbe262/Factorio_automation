from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import AppConfig
from .controller import FactorioController
from .monitor import summarize_factory
from .skill_registry import annotate_strategy_with_skill_status
from .strategy import heuristic_strategy
from .targets import TARGET_ITEMS, load_targets, parse_target_form, save_targets


FACTORIO_ROUTE = "/\ud329\ud1a0\ub9ac\uc624"


def serve_dashboard(cfg: AppConfig, host: str = "127.0.0.1", port: int = 18889, objective: str = "launch_rocket_program") -> None:
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
            path = unquote(parsed.path).rstrip("/")
            if not is_factorio_route(path):
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return

            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            values = parse_qs(body)
            targets = parse_target_form(values)
            save_targets(cfg.runtime_dir, targets)
            state = build_dashboard_state(cfg, values.get("objective", [default_objective])[0])
            html = render_dashboard(state).encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            objective = query.get("objective", [default_objective])[0]

            if path in {"", "/"}:
                self.send_response(302)
                self.send_header("Location", FACTORIO_ROUTE)
                self.end_headers()
                return

            if is_factorio_route(path):
                state = build_dashboard_state(cfg, objective)
                body = render_dashboard(state)
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == "/api/factorio":
                state = build_dashboard_state(cfg, objective)
                self._send(200, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8")
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

    return FactorioDashboardHandler


def is_factorio_route(path: str) -> bool:
    return unquote(path).rstrip("/") == FACTORIO_ROUTE


def build_dashboard_state(cfg: AppConfig, objective: str) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    targets = load_targets(cfg.runtime_dir, objective)
    try:
        observation = FactorioController(cfg).observe()
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
            "monitor": monitor,
            "strategy": strategy,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "error": f"{type(exc).__name__}: {exc}",
        }


def render_dashboard(state: dict[str, Any]) -> str:
    title = "Factorio AI Factory Monitor"
    if not state.get("ok"):
        return _page(
            title,
            f"""
            <section class="panel">
              <h2>Connection</h2>
              <p class="error">{escape(str(state.get("error") or "unknown error"))}</p>
            </section>
            """,
        )

    monitor = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
    strategy = state.get("strategy") if isinstance(state.get("strategy"), dict) else {}
    production = monitor.get("production") if isinstance(monitor.get("production"), list) else []
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
        <span class="label">Objective</span>
        <strong>{escape(str(state.get("objective") or ""))}</strong>
      </div>
      <div>
        <span class="label">Tick</span>
        <strong>{escape(str(state.get("observation_tick") or ""))}</strong>
      </div>
      <div>
        <span class="label">Updated</span>
        <strong>{escape(str(state.get("updated_at") or ""))}</strong>
      </div>
    </section>

    <section class="panel">
      <h2>Strategic Recommendation</h2>
      <div class="strategy">
        <strong>{escape(str(strategy.get("selected_skill") or ""))}</strong>
        <span>priority {escape(str(strategy.get("priority") or ""))}</span>
      </div>
      <p>{escape(str(strategy.get("reason") or ""))}</p>
      {_list("Blockers", strategy.get("blockers"))}
      {_skill_status(strategy.get("skill_status"))}
      <p class="muted">{_target_satisfied_text(target_status)}</p>
    </section>

    <section class="panel">
      <h2>Desired Production Targets</h2>
      {_target_form(targets_per_minute, target_status)}
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Estimated Production</h2>
        {_production_table(production)}
      </div>
      <div class="panel">
        <h2>Target Deficits / Bottlenecks</h2>
        {_bottleneck_table(bottlenecks)}
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Inventory / Machine Contents</h2>
        {_key_value_table(inventory)}
      </div>
      <div class="panel">
        <h2>Technology Chain</h2>
        {_tech_table(technologies)}
      </div>
    </section>

    <section class="panel">
      <h2>Dependency Tree</h2>
      <pre>{escape(json.dumps(dependency, ensure_ascii=False, indent=2))}</pre>
    </section>
    """
    return _page(title, body)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
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
    h1 {{
      margin: 0 0 20px;
      font-size: 24px;
      font-weight: 650;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      font-weight: 650;
      color: #f0c46c;
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
      vertical-align: top;
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
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""


def _production_table(rows: list[Any]) -> str:
    if not rows:
        return "<p class=\"muted\">No active producers inferred yet.</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('per_minute') or 0))}</td>"
        f"<td>{escape(str(row.get('producers') or 0))}</td>"
        f"<td>{escape(str(row.get('confidence') or 0))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return f"<table><thead><tr><th>Item</th><th>/ min</th><th>Producers</th><th>Confidence</th></tr></thead><tbody>{body}</tbody></table>"


def _bottleneck_table(rows: list[Any]) -> str:
    if not rows:
        return "<p class=\"muted\">No bottleneck inferred for the current objective.</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('item') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        f"<td>{escape(str(row.get('stock') or 0))}</td>"
        f"<td>{escape(str(row.get('estimated_per_minute') or 0))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return f"<table><thead><tr><th>Item</th><th>Reason</th><th>Stock</th><th>/ min</th></tr></thead><tbody>{body}</tbody></table>"


def _target_form(targets: dict[str, Any], target_status: dict[str, Any]) -> str:
    status_rows = {
        str(row.get("item")): row
        for row in target_status.get("items", [])
        if isinstance(row, dict) and row.get("item")
    } if isinstance(target_status.get("items"), list) else {}
    rows = []
    for item in TARGET_ITEMS:
        target = float(targets.get(item) or 0.0)
        status = status_rows.get(item, {})
        estimated = status.get("estimated_per_minute", 0)
        deficit = status.get("deficit_per_minute", 0)
        rows.append(
            "<tr>"
            f"<td>{escape(item)}</td>"
            f"<td><input name=\"{escape(item)}\" type=\"number\" min=\"0\" step=\"1\" value=\"{escape(str(target))}\"></td>"
            f"<td>{escape(str(estimated))}</td>"
            f"<td>{escape(str(deficit))}</td>"
            "</tr>"
        )
    return (
        "<form method=\"post\" action=\"/팩토리오\">"
        "<table><thead><tr><th>Item</th><th>Target / min</th><th>Estimated / min</th><th>Deficit / min</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "<div class=\"actions\"><button type=\"submit\">Save Targets</button></div>"
        "</form>"
    )


def _target_satisfied_text(target_status: dict[str, Any]) -> str:
    if not target_status:
        return "No production targets are configured yet."
    if target_status.get("all_satisfied"):
        return "All user production targets are satisfied. The strategic LLM may raise targets or choose the next rocket-program item."
    return "Some production targets are below the desired rate."


def _skill_status(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("implemented"):
        return f"<p class=\"muted\">Executor: {escape(str(value.get('executor') or ''))}</p>"
    return (
        "<p class=\"error\">Executor missing. Codex must implement this skill before the AI can run it safely.</p>"
    )


def _key_value_table(values: dict[str, Any]) -> str:
    if not values:
        return "<p class=\"muted\">No tracked inventory yet.</p>"
    body = "".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in sorted(values.items())
    )
    return f"<table><thead><tr><th>Item</th><th>Count</th></tr></thead><tbody>{body}</tbody></table>"


def _tech_table(rows: list[Any]) -> str:
    if not rows:
        return "<p class=\"muted\">No technology requirement inferred for this objective yet.</p>"
    body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('name') or ''))}</td>"
        f"<td>{escape(', '.join(str(item) for item in row.get('prerequisites', [])))}</td>"
        f"<td>{escape(', '.join(str(item) for item in row.get('unlocks', [])))}</td>"
        "</tr>"
        for row in rows
        if isinstance(row, dict)
    )
    return f"<table><thead><tr><th>Technology</th><th>Prerequisites</th><th>Unlocks</th></tr></thead><tbody>{body}</tbody></table>"


def _list(title: str, values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    items = "".join(f"<li>{escape(str(item))}</li>" for item in values)
    return f"<div><span class=\"label\">{escape(title)}</span><ul>{items}</ul></div>"
