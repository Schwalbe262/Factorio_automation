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

from .cell_library import library_summary
from .config import AppConfig
from .controller import FactorioController
from .item_icons import read_item_icon_png
from .layout_llm_settings import load_layout_llm_settings, save_layout_llm_settings
from .layout_validation import layout_validation_feedback_summary, merge_sandbox_validation_feedback
from .llm_log import llm_decision_summary, llm_io_trace_summary
from .monitor import summarize_factory
from .networking import dashboard_urls
from .modless_lua import ModlessLuaController
from .run_journal import run_journal_summary
from .skill_registry import annotate_strategy_with_skill_status
from .site_selection import (
    clear_selected_improvement_site,
    load_selected_improvement_site,
    save_selected_improvement_site,
    selected_improvement_site_from_form,
)
from .strategy import heuristic_strategy, make_layout_improvement_context
from .targets import TARGET_ITEMS, load_targets, parse_target_form, save_targets
from .token_usage import token_usage_summary
from .trace_archive import trace_archive_summary
from .world_memory import load_world_map_memory, summarize_world_map_memory


FACTORIO_ROUTE = "/factorio"
FACTORIO_LLM_ROUTE = "/factorio/llm"
FACTORIO_LAYOUTS_ROUTE = "/factorio/layouts"
LEGACY_FACTORIO_ROUTE = "/팩토리오"
FACTORIO_ROUTES = {FACTORIO_ROUTE, LEGACY_FACTORIO_ROUTE}
ICON_ROUTE_PREFIX = "/factorio/icon/"
API_ROUTE = "/api/factorio"
LLM_API_ROUTE = "/api/factorio/llm"
BLUEPRINT_API_ROUTE = "/api/factorio/blueprint"
FACTORIO_BLUEPRINT_ROUTE = "/factorio/blueprint"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = {"en", "ko"}
DEFAULT_PUBLIC_DASHBOARD_BASE_URL = "http://27.115.156.173:8787"
KST = timezone(timedelta(hours=9), "KST")
DEFAULT_WEB_CACHE_SECONDS = 60.0
DEFAULT_WEB_REFRESH_SECONDS = 15.0
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
        "tokens_per_hour": "Tokens / hour",
        "sample_count": "Samples",
        "last_sample": "Last Sample",
        "power_networks": "Power Networks",
        "no_power_networks": "No electric power networks inferred yet.",
        "llm_decisions": "LLM Decision Log",
        "no_llm_decisions": "No LLM strategy attempts recorded yet.",
        "llm_io_traces": "LLM I/O Traces",
        "no_llm_io_traces": "No LLM input/output traces recorded yet.",
        "dashboard": "Dashboard",
        "prompt": "Prompt",
        "system_prompt": "System Prompt",
        "raw_output": "Raw Output",
        "parsed_json": "Parsed JSON",
        "trace_id": "Trace ID",
        "llm_worker_comparison": "LLM Worker Comparison",
        "no_llm_worker_comparison": "No Slurm LLM worker comparison recorded yet.",
        "worker": "Worker",
        "model": "Model",
        "ready": "Ready",
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
        "tokens_per_hour": "시간당 토큰",
        "sample_count": "샘플",
        "last_sample": "최근 기록",
        "power_networks": "전력망",
        "no_power_networks": "아직 추정된 전력망이 없습니다.",
        "llm_decisions": "LLM 판단 로그",
        "no_llm_decisions": "아직 기록된 LLM 전략 시도가 없습니다.",
        "llm_io_traces": "LLM I/O Traces",
        "no_llm_io_traces": "No LLM input/output traces recorded yet.",
        "dashboard": "Dashboard",
        "prompt": "Prompt",
        "system_prompt": "System Prompt",
        "raw_output": "Raw Output",
        "parsed_json": "Parsed JSON",
        "trace_id": "Trace ID",
        "llm_worker_comparison": "LLM Worker Comparison",
        "no_llm_worker_comparison": "No Slurm LLM worker comparison recorded yet.",
        "worker": "Worker",
        "model": "Model",
        "ready": "Ready",
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
        "execution_mode": "Execution Mode",
        "character_valid": "Character",
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
        "execution_mode": "실행 모드",
        "character_valid": "캐릭터",
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
        "generated_skills": "Generated Skills (self-developed)",
        "no_generated_skills": "The local LLM has not registered any self-developed skill executors yet.",
        "foundry_state": "Foundry State",
        "foundry_queue": "Generation Queue",
        "registered_skills": "Registered Skills",
        "foundry_gates": "Gates Passed",
        "foundry_failures": "Recent Failures / Quarantine",
        "self_repair_overrides": "Self-Repair Overrides (active)",
        "version": "Version",
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
        "copy_before_blueprint": "Copy before",
        "copy_after_blueprint": "Copy after",
        "validation": "Validation",
        "sandbox_validation": "Sandbox",
        "prebuild_gate": "Pre-build",
        "placement_search": "Placement",
        "validation_pass": "Pass",
        "validation_warning": "Warning",
        "validation_fail": "Fail",
        "placement_found": "Found",
        "placement_blocked": "Blocked",
        "copied": "Copied",
        "copy_failed": "Copy failed",
        "manual_copy": "Manual copy opened",
        "close": "Close",
        "site_logistics": "Site Logistics",
        "unassigned_logistics": "Unassigned Logistics",
        "inbound": "In",
        "outbound": "Out",
        "linked": "Link",
        "improve_site": "Improve",
        "select_improvement_site": "Select",
        "selected_improvement_site": "Selected",
        "selected_improvement_target": "Selected improvement target",
        "clear_improvement_site": "Cancel",
        "world_map_memory": "World Map Memory",
        "memory_encoding": "Encoding",
        "memory_age": "Age",
        "memory_feature_index": "Feature Index",
        "known_water_sites": "Known water anchors",
        "resource_patches": "Resource patches",
        "factory_zones": "Factory zones",
        "no_world_map_memory": "No spatial memory has been recorded yet.",
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
        "generated_skills": "\uc0dd\uc131\ub41c \uc2a4\ud0ac (\uc790\uac00 \uac1c\ubc1c)",
        "no_generated_skills": "\ub85c\uceec LLM\uc774 \uc544\uc9c1 \uc790\uac00 \uac1c\ubc1c\ud55c \uc2a4\ud0ac \uc2e4\ud589\uae30\ub97c \ub4f1\ub85d\ud558\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4.",
        "foundry_state": "\ud30c\uc6b4\ub4dc\ub9ac \uc0c1\ud0dc",
        "foundry_queue": "\uc0dd\uc131 \ub300\uae30\uc5f4",
        "registered_skills": "\ub4f1\ub85d\ub41c \uc2a4\ud0ac",
        "foundry_gates": "\ud1b5\uacfc\ud55c \uac8c\uc774\ud2b8",
        "foundry_failures": "\ucd5c\uadfc \uc2e4\ud328 / \uaca9\ub9ac",
        "self_repair_overrides": "\uc790\uac00 \uc218\uc815 \uc624\ubc84\ub77c\uc774\ub4dc (\ud65c\uc131)",
        "version": "\ubc84\uc804",
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
        "copy_before_blueprint": "\uac1c\uc120 \uc804 \ubcf5\uc0ac",
        "copy_after_blueprint": "\uac1c\uc120 \ud6c4 \ubcf5\uc0ac",
        "validation": "\uac80\uc99d",
        "sandbox_validation": "\uc0cc\ub4dc\ubc15\uc2a4",
        "prebuild_gate": "\uc0ac\uc804 \ube4c\ub4dc",
        "placement_search": "\ubc30\uce58",
        "validation_pass": "\ud1b5\uacfc",
        "validation_warning": "\uacbd\uace0",
        "validation_fail": "\uc2e4\ud328",
        "placement_found": "\ucc3e\uc74c",
        "placement_blocked": "\ucc28\ub2e8",
        "copied": "\ubcf5\uc0ac\ub428",
        "copy_failed": "\ubcf5\uc0ac \uc2e4\ud328",
        "manual_copy": "\uc218\ub3d9 \ubcf5\uc0ac \ucc3d \uc5f4\ub9bc",
        "close": "\ub2eb\uae30",
        "site_logistics": "\uc0ac\uc774\ud2b8 \ubb3c\ub958",
        "unassigned_logistics": "\ubbf8\uc5f0\uacb0 \ubb3c\ub958",
        "inbound": "\uc785\ub825",
        "outbound": "\ucd9c\ub825",
        "linked": "\uc5f0\uacb0",
        "improve_site": "\uac1c\uc120",
        "select_improvement_site": "\uc120\ud0dd",
        "selected_improvement_site": "\uc120\ud0dd\ub428",
        "selected_improvement_target": "\uc120\ud0dd\ub41c \uac1c\uc120 \ub300\uc0c1",
        "clear_improvement_site": "\ucde8\uc18c",
        "world_map_memory": "World Map Memory",
        "memory_encoding": "Encoding",
        "memory_age": "Age",
        "memory_feature_index": "Feature Index",
        "known_water_sites": "Known water anchors",
        "resource_patches": "Resource patches",
        "factory_zones": "Factory zones",
        "no_world_map_memory": "No spatial memory has been recorded yet.",
    }
)
TEXT["en"].update(
    {
        "goal_plan": "Goal Plan",
        "recent_run_notes": "Recent Loop Notes",
        "recent_run_insights": "Recent Insights",
        "no_goal_plan": "No goal.md has been created yet.",
        "no_run_notes": "No loop notes have been recorded yet.",
        "no_run_insights": "No improvement insights have been recorded yet.",
        "loop_type": "Loop",
        "latest_delta_tokens": "Latest Delta",
        "weekly_quota": "Weekly Quota",
        "weekly_percent": "Weekly %",
        "counter_resets": "Counter Resets",
        "trace_archives": "Training Trace Archives",
        "no_trace_archives": "No training trace archive has been created yet.",
        "archive": "Archive",
        "archive_root": "Archive Root",
        "high_value": "High Value",
        "categories": "Categories",
        "layout_llm_settings": "Local LLM Layout Jobs",
        "max_active_layout_tasks": "Max active jobs",
        "layout_llm_hint": "Each job requests one GPU. The idle layout loop keeps submitting until this limit is full.",
        "save_layout_llm_settings": "Save LLM Setting",
        "layout_library": "Layout Library",
        "no_layouts": "No optimized cell layouts have been saved yet.",
    }
)
TEXT["ko"].update(
    {
        "layout_library": "레이아웃 라이브러리",
        "no_layouts": "아직 저장된 최적 셀 레이아웃이 없습니다.",
        "goal_plan": "Goal Plan",
        "recent_run_notes": "Recent Loop Notes",
        "recent_run_insights": "Recent Insights",
        "no_goal_plan": "No goal.md has been created yet.",
        "no_run_notes": "No loop notes have been recorded yet.",
        "no_run_insights": "No improvement insights have been recorded yet.",
        "loop_type": "Loop",
        "latest_delta_tokens": "Latest Delta",
        "weekly_quota": "Weekly Quota",
        "weekly_percent": "Weekly %",
        "counter_resets": "카운터 리셋",
        "trace_archives": "Training Trace Archives",
        "no_trace_archives": "No training trace archive has been created yet.",
        "archive": "Archive",
        "archive_root": "Archive Root",
        "high_value": "High Value",
        "categories": "Categories",
        "layout_llm_settings": "Local LLM Layout Jobs",
        "max_active_layout_tasks": "Max active jobs",
        "layout_llm_hint": "Each job requests one GPU. The idle layout loop keeps submitting until this limit is full.",
        "save_layout_llm_settings": "Save LLM Setting",
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
            objective = values.get("objective", [default_objective])[0]
            _handle_dashboard_post_values(cfg, objective, values)
            state = build_dashboard_state_cached(cfg, objective, force_refresh=True)
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

            if path == FACTORIO_LLM_ROUTE:
                lang = request_language(path, query)
                summary = llm_trace_api_response(cfg.log_dir, limit=_llm_trace_limit(query))
                kind_filter = (query.get("kind") or [""])[0]
                body = render_llm_trace_page(summary, lang=lang, objective=objective, kind=kind_filter)
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == FACTORIO_LAYOUTS_ROUTE:
                lang = request_language(path, query)
                summary = library_summary(cfg.runtime_dir)
                avail = _current_available_machines(cfg.runtime_dir)
                body = render_layouts_page(summary, lang=lang, objective=objective, available_machines=avail)
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

            if path == LLM_API_ROUTE:
                response = llm_trace_api_response(cfg.log_dir, limit=_llm_trace_limit(query))
                self._send(
                    200,
                    json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return

            if path in {BLUEPRINT_API_ROUTE, FACTORIO_BLUEPRINT_ROUTE}:
                state = build_dashboard_state_cached(cfg, objective)
                response = _blueprint_response(
                    state,
                    candidate_id=query.get("candidate_id", [""])[0],
                    site_id=query.get("site_id", [""])[0],
                    variant=query.get("variant", ["after"])[0],
                )
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


def _handle_dashboard_post_values(cfg: AppConfig, objective: str, values: dict[str, list[str]]) -> None:
    action = str((values.get("action") or ["save_targets"])[0] or "save_targets")
    if action == "select_improvement_site":
        save_selected_improvement_site(
            cfg.runtime_dir,
            objective,
            selected_improvement_site_from_form(values),
        )
        return
    if action == "clear_improvement_site":
        clear_selected_improvement_site(cfg.runtime_dir, objective)
        return
    if action == "save_layout_llm_settings":
        save_layout_llm_settings(
            cfg.runtime_dir,
            (values.get("max_active_layout_tasks") or [""])[0],
        )
        return
    targets = parse_target_form(values)
    save_targets(cfg.runtime_dir, targets)


def _warm_dashboard_cache(cfg: AppConfig, objective: str) -> None:
    try:
        build_dashboard_state_cached(cfg, objective, force_refresh=True)
    except Exception:
        return


def normalized_path(path: str) -> str:
    value = unquote(path).rstrip("/")
    return value or "/"


def is_factorio_route(path: str) -> bool:
    return normalized_path(path) in FACTORIO_ROUTES | {FACTORIO_LLM_ROUTE}


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


def llm_trace_path(lang: str = DEFAULT_LANG, objective: str | None = None) -> str:
    params: dict[str, str] = {}
    if lang in SUPPORTED_LANGS and lang != DEFAULT_LANG:
        params["lang"] = lang
    if objective:
        params["objective"] = objective
    suffix = f"?{urlencode(params)}" if params else ""
    return f"{FACTORIO_LLM_ROUTE}{suffix}"


def layouts_path(lang: str = DEFAULT_LANG, objective: str | None = None) -> str:
    params: dict[str, str] = {}
    if lang in SUPPORTED_LANGS and lang != DEFAULT_LANG:
        params["lang"] = lang
    if objective:
        params["objective"] = objective
    suffix = f"?{urlencode(params)}" if params else ""
    return f"{FACTORIO_LAYOUTS_ROUTE}{suffix}"


def public_dashboard_urls(host: str, port: int, lang: str = DEFAULT_LANG) -> list[str]:
    route = dashboard_path(lang)
    base_url = (
        os.getenv("FACTORIO_AI_WEB_BASE_URL")
        or os.getenv("FACTORIO_DASHBOARD_BASE_URL")
        or DEFAULT_PUBLIC_DASHBOARD_BASE_URL
    )
    return dashboard_urls(host, port, route, base_url=base_url)


def llm_trace_api_response(log_dir: Any, *, limit: int = 50) -> dict[str, Any]:
    return llm_io_trace_summary(log_dir, limit=limit)


def _llm_trace_limit(query: dict[str, list[str]]) -> int:
    try:
        raw = int((query.get("limit") or ["50"])[0])
    except (TypeError, ValueError):
        raw = 50
    return max(1, min(200, raw))


def _blueprint_response(
    state: dict[str, Any],
    *,
    candidate_id: str = "",
    site_id: str = "",
    variant: str = "after",
) -> dict[str, Any]:
    if site_id:
        return _site_blueprint_response(state, site_id)
    return _candidate_blueprint_response(state, candidate_id, variant=variant)


def _candidate_blueprint_response(
    state: dict[str, Any],
    candidate_id: str,
    *,
    variant: str = "after",
) -> dict[str, Any]:
    layout = state.get("layout_improvement") if isinstance(state.get("layout_improvement"), dict) else {}
    candidates = layout.get("simulation_candidates") if isinstance(layout.get("simulation_candidates"), list) else []
    variant = "before" if str(variant).lower() in {"before", "current"} else "after"
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("candidate_id") or "") != candidate_id:
            continue
        blueprint = _candidate_blueprint_for_variant(candidate, variant)
        exchange_string = blueprint.get("exchange_string")
        if not isinstance(exchange_string, str) or not exchange_string:
            break
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "variant": variant,
            "label": str(blueprint.get("label") or candidate_id),
            "format": str(blueprint.get("format") or "factorio-blueprint-string"),
            "entity_count": int(blueprint.get("entity_count") or 0),
            "blueprint": exchange_string,
        }
    return {"ok": False, "error": f"{variant} blueprint candidate not found", "candidate_id": candidate_id}


def _candidate_blueprint_for_variant(candidate: dict[str, Any], variant: str) -> dict[str, Any]:
    keys = ["before_blueprint"] if variant == "before" else ["after_blueprint", "blueprint"]
    for key in keys:
        blueprint = candidate.get(key)
        if isinstance(blueprint, dict):
            return blueprint
    return {}


def _site_blueprint_response(state: dict[str, Any], site_id: str) -> dict[str, Any]:
    monitor = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
    sites = monitor.get("factory_sites") if isinstance(monitor.get("factory_sites"), list) else []
    for site in sites:
        if not isinstance(site, dict):
            continue
        if str(site.get("site_id") or "") != site_id:
            continue
        blueprint = site.get("blueprint") if isinstance(site.get("blueprint"), dict) else {}
        exchange_string = blueprint.get("exchange_string")
        if not isinstance(exchange_string, str) or not exchange_string:
            break
        return {
            "ok": True,
            "site_id": site_id,
            "label": str(blueprint.get("label") or site_id),
            "format": str(blueprint.get("format") or "factorio-blueprint-string"),
            "entity_count": int(blueprint.get("entity_count") or 0),
            "blueprint": exchange_string,
        }
    return {"ok": False, "error": "blueprint site not found", "site_id": site_id}


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


def _web_refresh_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("FACTORIO_AI_WEB_REFRESH_SECONDS", str(DEFAULT_WEB_REFRESH_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_WEB_REFRESH_SECONDS


def build_dashboard_state(cfg: AppConfig, objective: str) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    targets = load_targets(cfg.runtime_dir, objective)
    selected_improvement_site = load_selected_improvement_site(cfg.runtime_dir, objective)
    token_usage = token_usage_summary(cfg.log_dir)
    llm_decisions = llm_decision_summary(cfg.log_dir)
    worker_comparison = strategy_worker_comparison_summary(cfg.log_dir)
    layout_background = layout_background_summary(cfg.log_dir)
    layout_validation_feedback = layout_validation_feedback_summary(cfg.log_dir)
    layout_llm_settings = load_layout_llm_settings(cfg.runtime_dir)
    run_journal = run_journal_summary(cfg.log_dir)
    trace_archives = trace_archive_summary(cfg.runtime_dir / "trace_archives")
    world_map_memory = summarize_world_map_memory(load_world_map_memory(cfg.runtime_dir))
    generated_skills = generated_skills_summary(cfg)
    try:
        observation, adapter = observe_dashboard_state(cfg)
        monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
        layout_improvement = merge_sandbox_validation_feedback(
            make_layout_improvement_context(
                observation,
                selected_improvement_site=selected_improvement_site,
            ),
            layout_validation_feedback,
        )
        strategy = annotate_strategy_with_skill_status(
            heuristic_strategy(
                objective,
                observation,
                targets.per_minute,
                selected_improvement_site=selected_improvement_site,
            ),
            runtime_dir=cfg.runtime_dir,
        )
        return {
            "ok": True,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "observation_tick": observation.get("tick"),
            "player": observation.get("player"),
            "execution": observation.get("execution"),
            "agent_marker": observation.get("agent_marker"),
            "adapter": adapter,
            "monitor": monitor,
            "selected_improvement_site": selected_improvement_site,
            "layout_improvement": layout_improvement,
            "layout_background": layout_background,
            "layout_validation_feedback": layout_validation_feedback,
            "layout_llm_settings": layout_llm_settings,
            "strategy": strategy,
            "token_usage": token_usage,
            "llm_decisions": llm_decisions,
            "strategy_worker_comparison": worker_comparison,
            "run_journal": run_journal,
            "trace_archives": trace_archives,
            "world_map_memory": world_map_memory,
            "generated_skills": generated_skills,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "updated_at": timestamp,
            "objective": objective,
            "targets": targets.to_dict(),
            "selected_improvement_site": selected_improvement_site,
            "error": friendly_dashboard_error(exc),
            "layout_background": layout_background,
            "layout_validation_feedback": layout_validation_feedback,
            "layout_llm_settings": layout_llm_settings,
            "token_usage": token_usage,
            "llm_decisions": llm_decisions,
            "strategy_worker_comparison": worker_comparison,
            "run_journal": run_journal,
            "trace_archives": trace_archives,
            "world_map_memory": world_map_memory,
            "generated_skills": generated_skills,
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


def generated_skills_summary(cfg: AppConfig) -> dict[str, Any]:
    """Self-development status: registered Qwen-authored executors, the generation queue,
    recent failures/quarantine, and the foundry loop heartbeat."""

    from . import skill_foundry

    registry = skill_foundry.load_registry()
    skills = registry.get("skills") if isinstance(registry.get("skills"), dict) else {}
    registered: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for name, entry in sorted(skills.items()):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        row = {
            "skill_name": name,
            "status": status,
            "version": entry.get("version"),
            "class_name": entry.get("class_name"),
            "target_item": entry.get("target_item"),
            "gates_passed": entry.get("gates_passed") if isinstance(entry.get("gates_passed"), list) else [],
            "updated_at": entry.get("updated_at"),
            "attempts": entry.get("attempts"),
            "last_failure_reason": entry.get("last_failure_reason") or "",
        }
        if status == "override_registered":
            overrides.append(row)
        elif status == "registered":
            registered.append(row)
        elif status in {"failed", "quarantined", "disabled"}:
            failures.append(row)
    try:
        queue = skill_foundry.load_foundry_queue(cfg.runtime_dir)
    except Exception:  # noqa: BLE001
        queue = []
    heartbeat: dict[str, Any] = {}
    hb_path = cfg.runtime_dir / "skill-foundry-loop.json"
    if hb_path.exists():
        try:
            raw = json.loads(hb_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                heartbeat = raw
        except (OSError, json.JSONDecodeError):
            heartbeat = {}
    return {
        "registered": registered,
        "overrides": overrides,
        "failures": failures,
        "queue": queue,
        "heartbeat": heartbeat,
        "registered_count": len(registered),
        "override_count": len(overrides),
        "queue_count": len(queue),
    }


def strategy_worker_comparison_summary(log_dir: Any, *, limit: int = 10) -> dict[str, Any]:
    path = log_dir / "strategy-worker-comparison.jsonl"
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
        return ModlessLuaController(cfg).observe(include_planning_sites=False), "no-mod-rcon-lua"
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


def render_llm_trace_page(
    summary: dict[str, Any],
    lang: str = DEFAULT_LANG,
    objective: Any = None,
    kind: str = "",
) -> str:
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    title = _t(lang, "llm_io_traces")
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    entries = [row for row in entries if isinstance(row, dict)]
    api_query = {"limit": str(summary.get("entry_count") or 50)}
    if lang in SUPPORTED_LANGS and lang != DEFAULT_LANG:
        api_query["lang"] = lang
    if objective:
        api_query["objective"] = str(objective)
    api_href = f"{LLM_API_ROUTE}?{urlencode(api_query)}"
    if not entries:
        body = (
            "<section class=\"panel\">"
            f"<h2>{escape(title)}</h2>"
            f"<p class=\"muted\">{escape(str(summary.get('log_path') or ''))}</p>"
            f"<p class=\"muted\">{_t(lang, 'no_llm_io_traces')}</p>"
            "</section>"
        )
        return _page(title, body, lang, objective)
    # Per-kind filter so strategy / skill_foundry / layout / planner traces can be viewed
    # individually. Counts come from the full (unfiltered) entry set.
    selected_kind = str(kind or "").strip()
    filter_html = _llm_trace_kind_filter_html(entries, selected_kind, lang, objective, summary)
    shown = [row for row in entries if str(row.get("kind") or "llm") == selected_kind] if selected_kind else entries
    cards = "".join(_llm_trace_card(row, lang) for row in shown)
    if not cards:
        cards = f"<p class=\"muted\">{_t(lang, 'no_llm_io_traces')}</p>"
    body = (
        "<section class=\"panel\">"
        f"<h2>{escape(title)}</h2>"
        "<div class=\"actions\">"
        f"<a class=\"nav-link\" href=\"{escape(dashboard_path(lang, str(objective or '')), quote=True)}\">"
        f"{escape(_t(lang, 'dashboard'))}</a>"
        f"<a class=\"nav-link\" href=\"{escape(api_href, quote=True)}\">{escape(LLM_API_ROUTE)}</a>"
        "</div>"
        f"<p class=\"muted\">{escape(str(summary.get('log_path') or ''))}</p>"
        f"{filter_html}"
        f"{cards}"
        "</section>"
    )
    return _page(title, body, lang, objective)


def _llm_trace_kind_filter_html(
    entries: list[dict[str, Any]],
    selected_kind: str,
    lang: str,
    objective: Any,
    summary: dict[str, Any],
) -> str:
    counts: dict[str, int] = {}
    for row in entries:
        key = str(row.get("kind") or "llm")
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""

    def _href(kind_value: str) -> str:
        params: dict[str, str] = {}
        if lang in SUPPORTED_LANGS and lang != DEFAULT_LANG:
            params["lang"] = lang
        if objective:
            params["objective"] = str(objective)
        limit_value = summary.get("entry_count")
        if limit_value:
            params["limit"] = str(limit_value)
        if kind_value:
            params["kind"] = kind_value
        suffix = f"?{urlencode(params)}" if params else ""
        return f"{FACTORIO_LLM_ROUTE}{suffix}"

    chips = [
        "<a class=\"nav-link{active}\" href=\"{href}\">{label}</a>".format(
            active="" if selected_kind else " trace-filter-active",
            href=escape(_href(""), quote=True),
            label=escape(f"all ({len(entries)})"),
        )
    ]
    for key in sorted(counts):
        chips.append(
            "<a class=\"nav-link{active}\" href=\"{href}\">{label}</a>".format(
                active=" trace-filter-active" if selected_kind == key else "",
                href=escape(_href(key), quote=True),
                label=escape(f"{key} ({counts[key]})"),
            )
        )
    return f"<div class=\"actions trace-filter\">{''.join(chips)}</div>"


def _llm_trace_card(row: dict[str, Any], lang: str) -> str:
    status_class = "trace-ok" if row.get("ok") else "trace-error"
    status_text = "ok" if row.get("ok") else "error"
    meta = [
        (_t(lang, "updated"), _format_kst_timestamp(row.get("timestamp"))),
        (_t(lang, "trace_id"), row.get("trace_id")),
        (_t(lang, "kind"), row.get("kind")),
        (_t(lang, "provider"), row.get("provider")),
        (_t(lang, "model"), row.get("model")),
        ("Task", row.get("task_id")),
        (_t(lang, "latency_ms"), row.get("duration_ms")),
        ("Prompt chars", row.get("prompt_chars")),
        ("Response chars", row.get("response_chars")),
        ("Max tokens", row.get("max_tokens")),
    ]
    meta_html = "".join(
        f"<span><b>{escape(str(label))}</b> {escape(str(value or ''))}</span>"
        for label, value in meta
        if value not in (None, "")
    )
    parsed_json = row.get("parsed_json") if isinstance(row.get("parsed_json"), dict) else None
    parsed_text = json.dumps(parsed_json, ensure_ascii=False, indent=2) if parsed_json is not None else ""
    error = str(row.get("error") or "")
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    # Stable per-entry key so the client can persist each block's open state across
    # the dashboard's auto-refresh (timestamp is fixed per entry).
    _ek = "trace:" + str(row.get("timestamp") or row.get("time") or row.get("task_id") or "")
    return (
        "<article class=\"trace-entry\">"
        "<div class=\"trace-header\">"
        f"<strong>{escape(str(row.get('kind') or 'llm'))}</strong>"
        f"<span class=\"{status_class}\">{escape(status_text)}</span>"
        "</div>"
        f"<div class=\"trace-meta\">{meta_html}</div>"
        f"{error_html}"
        f"{_trace_text_block(_t(lang, 'system_prompt'), row.get('system_prompt'), block_id=f'{_ek}:sys')}"
        f"{_trace_text_block(_t(lang, 'prompt'), row.get('input_prompt'), block_id=f'{_ek}:prompt')}"
        f"{_trace_text_block(_t(lang, 'raw_output'), row.get('raw_output'), block_id=f'{_ek}:raw')}"
        f"{_trace_text_block(_t(lang, 'parsed_json'), parsed_text, block_id=f'{_ek}:parsed')}"
        "</article>"
    )


def _trace_text_block(label: str, value: Any, *, limit: int = 6000, block_id: str = "") -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    omitted = max(0, len(text) - limit)
    visible = text[:limit]
    tail = f"<p class=\"muted\">truncated {omitted:,} chars; full text is preserved in JSONL</p>" if omitted else ""
    id_attr = f' id="{escape(block_id)}"' if block_id else ""
    return (
        f"<details class=\"trace-block\"{id_attr}>"
        f"<summary>{escape(label)} <span>{len(text):,} chars</span></summary>"
        f"<pre>{escape(visible)}</pre>"
        f"{tail}"
        "</details>"
    )


def _current_available_machines(runtime_dir: Any) -> set[str] | None:
    """Machines the current save can build with, from the cached observation snapshot
    (runtime/latest-observe.json). Returns None when it can't be read, so the layout page simply
    omits the Active/Locked badge instead of guessing."""
    try:
        from pathlib import Path as _Path
        from .cell_autodesign import _available_machines
        # PowerShell writes this snapshot with a UTF-8 BOM, so decode with utf-8-sig.
        obs = json.loads((_Path(runtime_dir) / "latest-observe.json").read_text(encoding="utf-8-sig"))
        return set(_available_machines(obs))
    except Exception:  # noqa: BLE001 - missing/stale snapshot just means no badge
        return None


def render_layouts_page(summary: dict[str, Any], lang: str = DEFAULT_LANG, objective: Any = None,
                        available_machines: set[str] | None = None) -> str:
    """The cell-library page (C8): every saved optimal layout with its description, spec table,
    and a copy-blueprint button. Designs buildable with the current save's machines are flagged
    'Active' (green); ones needing not-yet-researched machines are 'Locked'."""
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    title = _t(lang, "layout_library")
    designs = summary.get("designs") if isinstance(summary.get("designs"), list) else []
    if not designs:
        body = (
            "<section class=\"panel\">"
            f"<h2>{escape(title)}</h2>"
            f"<p class=\"muted\">{escape(str(summary.get('library_path') or ''))}</p>"
            f"<p class=\"muted\">{_t(lang, 'no_layouts')}</p>"
            "</section>"
        )
        return _page(title, body, lang, objective)
    cards = "".join(_layout_card(d, lang, available_machines) for d in designs if isinstance(d, dict))
    body = (
        "<section class=\"panel\">"
        f"<h2>{escape(title)} <span class=\"muted\">({len(designs)})</span></h2>"
        "<div class=\"actions\">"
        f"<a class=\"nav-link\" href=\"{escape(dashboard_path(lang, str(objective or '')), quote=True)}\">"
        f"{escape(_t(lang, 'dashboard'))}</a>"
        "</div>"
        f"<p class=\"muted\">{escape(str(summary.get('library_path') or ''))}</p>"
        f"{cards}"
        "</section>"
    )
    return _page(title, body, lang, objective)


def _footprint_text(design: dict[str, Any], fp: dict[str, Any]) -> str:
    """Footprint = the bounding rectangle the cell occupies (width x height incl. empty space),
    not the sum of machine/belt tiles. Prefer the placed required_box; fall back to footprint dims."""
    rb = design.get("required_box") if isinstance(design.get("required_box"), dict) else {}
    w = rb.get("width") if isinstance(rb.get("width"), (int, float)) else fp.get("width")
    h = rb.get("height") if isinstance(rb.get("height"), (int, float)) else fp.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)):
        return f"{w:g} x {h:g} = {round(w * h):g} tiles"
    return f"~{fp.get('area', 0)} tiles"


def _active_badge(design: dict[str, Any], available_machines: set[str] | None) -> str:
    """Green 'Active' badge when every machine the design needs is available in the current save;
    a muted 'Locked' badge when some required machine is not yet researched."""
    if available_machines is None:
        return ""
    req = set(design.get("required_machines") or [])
    if not req:
        return ""
    if req.issubset(available_machines):
        return "<span class=\"badge-active\">Active</span>"
    missing = ", ".join(sorted(req - available_machines))
    return f"<span class=\"badge-locked\" title=\"needs: {escape(missing, quote=True)}\">Locked</span>"


def _layout_card(design: dict[str, Any], lang: str, available_machines: set[str] | None = None) -> str:
    item = str(design.get("item") or "")
    blueprint = design.get("blueprint") if isinstance(design.get("blueprint"), dict) else {}
    exchange = str(blueprint.get("exchange_string") or "")
    status = str(design.get("sandbox_status") or "")
    status_class = "trace-ok" if ("pass" in status) else ("trace-error" if "fail" in status else "")
    fp = design.get("footprint") if isinstance(design.get("footprint"), dict) else {}
    belts_in = ", ".join(
        f"{i.get('item')} {i.get('per_minute')}/min ({i.get('belt_tier') or 'pipe'})"
        for i in (design.get("inputs") or []) if isinstance(i, dict)
    )
    belts_out = ", ".join(
        f"{o.get('item')} {o.get('per_minute')}/min ({o.get('belt_tier') or 'pipe'})"
        for o in (design.get("outputs") or []) if isinstance(o, dict)
    )
    rows = [
        ("Item", item),
        ("Rate / min", f"{design.get('target_rate')} → {design.get('achieved_rate')}"),
        (_t(lang, "machines"), f"{design.get('machine_count')} x {design.get('machine')}"),
        ("Modules", ", ".join(design.get("modules") or []) or "—"),
        ("On-site", ", ".join(f"{s.get('machine_count')}x {s.get('item')}" for s in (design.get('substages') or []) if isinstance(s, dict)) or "—"),
        ("Inputs", belts_in),
        ("Outputs", belts_out),
        (_t(lang, "demand_kw"), f"{design.get('total_power_kw')} kW"),
        ("Footprint", _footprint_text(design, fp)),
        ("Added", str(design.get("added_at") or "—")),
    ]
    meta_html = "".join(
        f"<tr><td class=\"muted\">{escape(str(k))}</td><td>{escape(str(v))}</td></tr>"
        for k, v in rows if v not in (None, "")
    )
    copy_button = (
        f"<button type=\"button\" class=\"copy-blueprint\" data-blueprint=\"{escape(exchange, quote=True)}\">"
        f"{escape(_t(lang, 'copy_blueprint'))}</button>"
        if exchange else ""
    )
    # Show only the short layout type (e.g. "belt_row"), not the full internal source string
    # ("regen-curtech:belt_row").
    arch_label = str(status).split(":")[-1].strip() or str(status)
    return (
        "<article class=\"trace-entry\">"
        "<div class=\"layout-card-head\">"
        f"<strong>{escape(str(design.get('description') or item))}</strong>"
        "<span class=\"layout-card-actions\">"
        f"{_active_badge(design, available_machines)}"
        f"<span class=\"{status_class}\">{escape(arch_label)}</span>"
        f"{copy_button}"
        "</span>"
        "</div>"
        f"<table class=\"kv\"><tbody>{meta_html}</tbody></table>"
        "</article>"
    )


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
            {_strategy_worker_comparison_panel(state.get("strategy_worker_comparison"), lang)}
            {_generated_skills_panel(state.get("generated_skills"), lang)}
            <section class="panel">
              <h2>{_t(lang, "layout_background")}</h2>
              {_layout_background_panel(state.get("layout_background"), lang)}
            </section>
            <section class="panel">
              <h2>{_t(lang, "layout_llm_settings")}</h2>
              {_layout_llm_settings_panel(state.get("layout_llm_settings"), lang, state.get("objective"))}
            </section>
            {_goal_panel(state.get("run_journal"), lang)}
            {_run_notes_panel(state.get("run_journal"), lang)}
            {_run_insights_panel(state.get("run_journal"), lang)}
            {_trace_archive_panel(state.get("trace_archives"), lang)}
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
    dependency_map = monitor.get("dependency_map") if isinstance(monitor.get("dependency_map"), dict) else {}
    crafting_facilities = monitor.get("crafting_facilities") if isinstance(monitor.get("crafting_facilities"), dict) else {}
    target_status = monitor.get("target_status") if isinstance(monitor.get("target_status"), dict) else {}
    targets = state.get("targets") if isinstance(state.get("targets"), dict) else {}
    targets_per_minute = targets.get("per_minute") if isinstance(targets.get("per_minute"), dict) else {}
    token_usage = state.get("token_usage") if isinstance(state.get("token_usage"), dict) else {}
    llm_decisions = state.get("llm_decisions") if isinstance(state.get("llm_decisions"), dict) else {}
    worker_comparison = (
        state.get("strategy_worker_comparison") if isinstance(state.get("strategy_worker_comparison"), dict) else {}
    )
    agent_marker = state.get("agent_marker") if isinstance(state.get("agent_marker"), dict) else {}
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    layout_improvement = state.get("layout_improvement") if isinstance(state.get("layout_improvement"), dict) else {}
    layout_background = state.get("layout_background") if isinstance(state.get("layout_background"), dict) else {}
    layout_llm_settings = (
        state.get("layout_llm_settings") if isinstance(state.get("layout_llm_settings"), dict) else {}
    )
    run_journal = state.get("run_journal") if isinstance(state.get("run_journal"), dict) else {}
    trace_archives = state.get("trace_archives") if isinstance(state.get("trace_archives"), dict) else {}
    world_map_memory = state.get("world_map_memory") if isinstance(state.get("world_map_memory"), dict) else {}
    generated_skills = state.get("generated_skills") if isinstance(state.get("generated_skills"), dict) else {}
    selected_improvement_site = (
        state.get("selected_improvement_site") if isinstance(state.get("selected_improvement_site"), dict) else {}
    )

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

    {_agent_activity_panel(agent_marker, state.get("player"), execution, lang)}

    {_goal_panel(run_journal, lang)}

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

    {_strategy_worker_comparison_panel(worker_comparison, lang)}

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

    <section class="panel">
      <h2>{_t(lang, "factory_sites")}</h2>
      {_selected_improvement_site_panel(selected_improvement_site, lang, state.get("objective"))}
      {_factory_site_table(factory_sites, logistics_links, selected_improvement_site, lang, state.get("objective"))}
      {_unassigned_logistics_table(factory_sites, logistics_links, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "world_map_memory")}</h2>
      {_world_map_memory_panel(world_map_memory, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "layout_improvement")}</h2>
      {_layout_improvement_panel(layout_improvement, lang)}
    </section>

    {_generated_skills_panel(generated_skills, lang)}

    <section class="panel">
      <h2>{_t(lang, "layout_background")}</h2>
      {_layout_background_panel(layout_background, lang)}
    </section>

    <section class="panel">
      <h2>{_t(lang, "layout_llm_settings")}</h2>
      {_layout_llm_settings_panel(layout_llm_settings, lang, state.get("objective"))}
    </section>

    {_run_notes_panel(run_journal, lang)}

    {_run_insights_panel(run_journal, lang)}

    {_trace_archive_panel(trace_archives, lang)}

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
      {_dependency_map_html(dependency_map, crafting_facilities) if dependency_map else _dependency_tree_html(dependency)}
    </section>
    """
    return _page(title, body, lang, state.get("objective"))


def _page(title: str, body: str, lang: str, objective: Any = None) -> str:
    objective_text = str(objective or "")
    refresh_seconds = _web_refresh_seconds()
    return f"""<!doctype html>
<html lang="{escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds:g}">
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
    .nav-link {{
      border: 1px solid #35404b;
      border-radius: 4px;
      color: #d8e0e8;
      padding: 6px 9px;
      text-decoration: none;
      font-size: 12px;
    }}
    .trace-filter {{
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}
    table.kv {{
      border-collapse: collapse;
      margin: 8px 0;
      font-size: 12px;
    }}
    table.kv td {{
      padding: 3px 10px 3px 0;
      vertical-align: top;
    }}
    .copy-blueprint {{
      border: 1px solid #3c9a6b;
      background: #1c3a2b;
      color: #d8f5e4;
      border-radius: 4px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: 12px;
    }}
    .nav-link.trace-filter-active {{
      background: #2f6f4f;
      border-color: #3c9a6b;
      color: #f2fff8;
      font-weight: 600;
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
    .deptree {{ font-size: 13px; line-height: 1.6; }}
    .deptree details {{ margin-left: 14px; }}
    .deptree > details {{ margin-left: 0; }}
    .deptree summary {{ cursor: pointer; user-select: none; }}
    .deptree summary:hover {{ color: #8ab4f8; }}
    .dep-leaf {{ margin-left: 28px; }}
    .dep-amt {{ color: #c9a227; }}
    .dep-tech {{ color: #7aa2f7; font-size: 11px; }}
    .dep-raw {{ color: #6b7280; font-size: 11px; }}
    .dep-infra {{ margin-top: 10px; border-top: 1px solid #23262b; padding-top: 8px; }}
    .trace-entry {{
      border-top: 1px solid #2a3036;
      padding-top: 14px;
      margin-top: 14px;
    }}
    .trace-header, .trace-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      align-items: center;
    }}
    .trace-header {{
      margin-bottom: 8px;
    }}
    .layout-card-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      margin-bottom: 8px;
    }}
    .layout-card-actions {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }}
    .trace-meta {{
      color: #9aa4af;
      font-size: 12px;
      margin-bottom: 10px;
    }}
    .trace-meta b {{
      color: #d8e0e8;
      font-weight: 600;
    }}
    .trace-ok, .trace-error {{
      border-radius: 4px;
      padding: 3px 7px;
      font-size: 12px;
      line-height: 1;
    }}
    .trace-ok {{
      background: #243f22;
      color: #9bd17b;
    }}
    .trace-error {{
      background: #4a2020;
      color: #ff9c9c;
    }}
    .badge-active, .badge-locked {{
      border-radius: 4px;
      padding: 3px 7px;
      font-size: 12px;
      line-height: 1;
      font-weight: 600;
      margin-left: 6px;
    }}
    .badge-active {{
      background: #1b7e3c;
      color: #ffffff;
    }}
    .badge-locked {{
      background: #4a4a4a;
      color: #c9c9c9;
    }}
    .trace-block {{
      margin-top: 8px;
    }}
    .trace-block summary {{
      cursor: pointer;
      color: #f0c46c;
      font-size: 13px;
      margin-bottom: 6px;
    }}
    .trace-block summary span {{
      color: #9aa4af;
      margin-left: 6px;
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
    .site-logistics-row td {{
      background: #101419;
      padding: 10px 12px 12px 28px;
      vertical-align: top;
    }}
    .site-logistics-list {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
    }}
    .site-logistics-title {{
      color: #f0c46c;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .site-logistics-link {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
      min-width: 0;
      line-height: 1.35;
    }}
    .site-logistics-direction {{
      border: 1px solid #3a424c;
      border-radius: 4px;
      color: #d8e0e8;
      font-size: 12px;
      padding: 2px 6px;
      white-space: nowrap;
    }}
    .site-logistics-kind, .site-logistics-status, .site-logistics-length, .site-logistics-more {{
      color: #9aa4af;
      font-size: 12px;
    }}
    .site-logistics-path {{
      color: #d8e0e8;
      overflow-wrap: anywhere;
    }}
    .site-logistics-unmatched {{
      margin-top: 12px;
    }}
    .site-logistics-unmatched h3 {{
      margin: 0 0 8px;
      color: #f0c46c;
      font-size: 14px;
    }}
    .site-improvement-form {{
      margin: 0;
    }}
    .layout-llm-settings-form {{
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      gap: 10px 12px;
      margin: 0 0 8px;
    }}
    .layout-llm-settings-form label {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      color: #9aa4af;
      font-size: 12px;
    }}
    .layout-llm-settings-form input {{
      width: 110px;
    }}
    .site-improvement-summary {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 12px;
      margin: 0 0 12px;
      padding: 8px 0 10px;
      border-bottom: 1px solid #2a3036;
      color: #d8e0e8;
      font-size: 13px;
    }}
    .site-improvement-selected {{
      display: inline-flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }}
    .site-improvement-button {{
      white-space: nowrap;
      padding: 6px 9px;
      font-size: 12px;
    }}
    .site-improvement-cancel-button {{
      border-color: #46515c;
      background: #2a3036;
      color: #d8e0e8;
      white-space: nowrap;
      padding: 6px 9px;
      font-size: 12px;
    }}
    .site-selected-badge {{
      display: inline-block;
      border: 1px solid #6b8f3f;
      border-radius: 4px;
      background: #27451c;
      color: #fff;
      padding: 4px 7px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .site-selected-row td {{
      background: rgba(79, 143, 47, 0.12);
    }}
    .layout-candidate-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 12px;
    }}
    .layout-candidate-card {{
      border: 1px solid #2a3036;
      border-radius: 6px;
      background: #101419;
      padding: 12px;
      min-width: 0;
    }}
    .layout-candidate-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }}
    .layout-candidate-id {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .layout-candidate-score {{
      color: #f0c46c;
      white-space: nowrap;
      font-weight: 700;
    }}
    .layout-candidate-pattern {{
      margin: 0 0 10px;
      color: #c8d1da;
      overflow-wrap: anywhere;
    }}
    .layout-validation {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid #28323b;
      border-radius: 4px;
      padding: 7px 8px;
      margin-bottom: 10px;
      background: #0c0f13;
      font-size: 12px;
    }}
    .layout-validation strong {{
      white-space: nowrap;
    }}
    .layout-validation span {{
      min-width: 0;
      overflow-wrap: anywhere;
      color: #c8d1da;
      text-align: right;
    }}
    .layout-validation-pass strong {{
      color: #78c850;
    }}
    .layout-validation-warning strong {{
      color: #f0c46c;
    }}
    .layout-validation-fail strong {{
      color: #ff6b6b;
    }}
    .layout-candidate-metrics {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .layout-metric-box {{
      border: 1px solid #252c33;
      border-radius: 4px;
      padding: 8px;
      background: #0c0f13;
      min-width: 0;
    }}
    .layout-metric-box strong {{
      display: block;
      color: #9aa4af;
      font-size: 12px;
      margin-bottom: 6px;
    }}
    .metric-row {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      border-top: 1px solid #1f252b;
      padding-top: 5px;
      margin-top: 5px;
      font-size: 12px;
    }}
    .metric-row:first-of-type {{
      border-top: 0;
      padding-top: 0;
      margin-top: 0;
    }}
    .metric-key, .metric-value {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .metric-key {{
      color: #9aa4af;
    }}
    .layout-candidate-footer {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 10px;
    }}
    .blueprint-actions {{
      display: inline-flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
    }}
    .manual-copy-overlay {{
      position: fixed;
      inset: 0;
      z-index: 9999;
      background: rgba(0, 0, 0, 0.68);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .manual-copy-box {{
      width: min(760px, 100%);
      border: 1px solid #3a424c;
      border-radius: 6px;
      background: #171b20;
      padding: 14px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.45);
    }}
    .manual-copy-box textarea {{
      width: 100%;
      height: 240px;
      box-sizing: border-box;
      margin: 10px 0;
      border: 1px solid #3a424c;
      border-radius: 4px;
      background: #0c0e10;
      color: #f2f2f2;
      font: 12px ui-monospace, SFMono-Regular, Consolas, monospace;
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
  {_details_persist_script()}
</body>
</html>"""


def _language_switch(lang: str, objective: str) -> str:
    en_class = "active" if lang == "en" else ""
    ko_class = "active" if lang == "ko" else ""
    return (
        f"<nav class=\"lang-switch\" aria-label=\"{escape(_t(lang, 'language'))}\">"
        f"<a href=\"{escape(dashboard_path(lang, objective))}\">{escape(_t(lang, 'dashboard'))}</a>"
        f"<a href=\"{escape(llm_trace_path(lang, objective))}\">{escape(_t(lang, 'llm_io_traces'))}</a>"
        f"<a href=\"{escape(layouts_path(lang, objective))}\">{escape(_t(lang, 'layout_library'))}</a>"
        f"<a class=\"{en_class}\" href=\"{escape(dashboard_path('en', objective))}\">EN</a>"
        f"<a class=\"{ko_class}\" href=\"{escape(dashboard_path('ko', objective))}\">KR</a>"
        "</nav>"
    )


def _details_persist_script() -> str:
    # Persist every <details id=...> open/closed state in localStorage so the
    # dashboard's full-page auto-refresh (meta refresh) doesn't snap subtrees shut
    # while the operator is reading them. Plain (non-f) string: no brace escaping.
    return """<script>
(function () {
  var KEY = 'factorioDetailsOpen';
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
  function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} }
  function init() {
    var state = load();
    document.querySelectorAll('details[id]').forEach(function (d) {
      if (Object.prototype.hasOwnProperty.call(state, d.id)) { d.open = !!state[d.id]; }
      d.addEventListener('toggle', function () {
        var s = load(); s[d.id] = d.open; save(s);
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>"""


def _copy_blueprint_script(lang: str) -> str:
    copied = json.dumps(_t(lang, "copied"))
    failed = json.dumps(_t(lang, "copy_failed"))
    manual = json.dumps(_t(lang, "manual_copy"))
    close = json.dumps(_t(lang, "close"))
    copy_label = json.dumps(_t(lang, "copy_blueprint"))
    return f"""<script>
(() => {{
  const showManualCopyDialog = (text) => {{
    const previous = document.querySelector(".manual-copy-overlay");
    if (previous) {{
      previous.remove();
    }}
    const overlay = document.createElement("div");
    overlay.className = "manual-copy-overlay";
    const box = document.createElement("div");
    box.className = "manual-copy-box";
    const title = document.createElement("strong");
    title.textContent = {copy_label};
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.textContent = {close};
    closeButton.addEventListener("click", () => overlay.remove());
    box.appendChild(title);
    box.appendChild(textarea);
    box.appendChild(closeButton);
    overlay.appendChild(box);
    overlay.addEventListener("click", (event) => {{
      if (event.target === overlay) {{
        overlay.remove();
      }}
    }});
    document.body.appendChild(overlay);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
  }};
  const copyText = async (text) => {{
    if (navigator.clipboard && window.isSecureContext) {{
      try {{
        await navigator.clipboard.writeText(text);
        return "clipboard";
      }} catch (error) {{
        // Fall through to the legacy path. HTTP dashboards on LAN/public IPs
        // often have the Clipboard API disabled even after a user click.
      }}
    }}
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "0";
    area.style.top = "0";
    area.style.width = "1px";
    area.style.height = "1px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, text.length);
    const copied = document.execCommand("copy");
    document.body.removeChild(area);
    if (copied) {{
      return "execCommand";
    }}
    showManualCopyDialog(text);
    return "manual";
  }};
  document.addEventListener("click", async (event) => {{
    const button = event.target.closest(".copy-blueprint");
    if (!button) {{
      return;
    }}
    event.preventDefault();
    // Library page embeds the blueprint inline (data-blueprint) -> copy directly, no fetch.
    const inlineBlueprint = button.getAttribute("data-blueprint") || "";
    if (inlineBlueprint) {{
      const originalInline = button.textContent;
      const inlineTitle = button.getAttribute("title") || "";
      const mode = await copyText(inlineBlueprint);
      button.textContent = mode === "manual" ? {manual} : {copied};
      window.setTimeout(() => {{ button.textContent = originalInline; button.setAttribute("title", inlineTitle); }}, 1500);
      return;
    }}
    const candidateId = button.getAttribute("data-candidate-id") || "";
    const siteId = button.getAttribute("data-site-id") || "";
    const variant = button.getAttribute("data-variant") || "";
    const params = new URLSearchParams(window.location.search);
    const apiParams = new URLSearchParams();
    if (candidateId) {{
      apiParams.set("candidate_id", candidateId);
    }}
    if (siteId) {{
      apiParams.set("site_id", siteId);
    }}
    if (variant) {{
      apiParams.set("variant", variant);
    }}
    const objective = params.get("objective");
    if (objective) {{
      apiParams.set("objective", objective);
    }}
    const original = button.textContent;
    const originalTitle = button.getAttribute("title") || "";
    try {{
      const endpoints = ["{FACTORIO_BLUEPRINT_ROUTE}", "{BLUEPRINT_API_ROUTE}"];
      let data = null;
      let lastError = null;
      for (const endpoint of endpoints) {{
        try {{
          const response = await fetch(endpoint + "?" + apiParams.toString(), {{ cache: "no-store" }});
          const candidate = await response.json();
          if (response.ok && candidate.ok && candidate.blueprint) {{
            data = candidate;
            break;
          }}
          lastError = new Error(candidate.error || ("blueprint unavailable at " + endpoint));
        }} catch (error) {{
          lastError = error;
        }}
      }}
      if (!data || !data.blueprint) {{
        throw lastError || new Error("blueprint unavailable");
      }}
      const mode = await copyText(data.blueprint);
      button.textContent = mode === "manual" ? {manual} : {copied};
      button.setAttribute("title", mode === "manual" ? "Clipboard API blocked; blueprint text is selected in the manual copy dialog." : originalTitle);
    }} catch (error) {{
      button.textContent = {failed};
      button.setAttribute("title", error && error.message ? error.message : "copy failed");
    }} finally {{
      window.setTimeout(() => {{
        button.textContent = original;
        button.setAttribute("title", originalTitle);
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


def _world_map_memory_panel(memory: dict[str, Any], lang: str) -> str:
    if not memory or not memory.get("schema_version"):
        return f"<p class=\"muted\">{_t(lang, 'no_world_map_memory')}</p>"
    resources = memory.get("resources") if isinstance(memory.get("resources"), dict) else {}
    factory = memory.get("factory") if isinstance(memory.get("factory"), dict) else {}
    spatial_index = memory.get("spatial_index") if isinstance(memory.get("spatial_index"), dict) else {}
    water_sites = memory.get("known_water_sites") if isinstance(memory.get("known_water_sites"), list) else []
    patches = resources.get("patches") if isinstance(resources.get("patches"), list) else []
    zones = factory.get("zones") if isinstance(factory.get("zones"), list) else []
    candidate_counts = memory.get("candidate_counts") if isinstance(memory.get("candidate_counts"), dict) else {}
    summary_rows = {
        _t(lang, "memory_encoding"): memory.get("encoding") or "",
        _t(lang, "updated"): _format_kst_timestamp(memory.get("updated_at")),
        _t(lang, "memory_age"): f"{memory.get('updated_age_seconds', 0)}s",
        _t(lang, "memory_feature_index"): (
            f"{spatial_index.get('feature_count', 0)} features / "
            f"{spatial_index.get('cell_count', 0)} cells @ {spatial_index.get('cell_size', '')} tiles"
        ),
        "Planning candidates": ", ".join(f"{key}={value}" for key, value in sorted(candidate_counts.items())),
    }
    water_body = "".join(
        "<tr>"
        f"<td>{_position_text(row.get('position'))}</td>"
        f"<td>{escape(str(row.get('direction') or ''))}</td>"
        f"<td>{escape(str(row.get('distance') or ''))}</td>"
        "</tr>"
        for row in water_sites[:8]
        if isinstance(row, dict)
    )
    patch_body = "".join(
        "<tr>"
        f"<td>{_item_cell(str(row.get('name') or ''))}</td>"
        f"<td>{_position_text(row.get('center'))}</td>"
        f"<td>{escape(str(row.get('sample_count') or 0))}</td>"
        f"<td>{escape(str(row.get('total_amount') or 0))}</td>"
        "</tr>"
        for row in patches[:8]
        if isinstance(row, dict)
    )
    zone_body = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('id') or ''))}</td>"
        f"<td>{_position_text(row.get('center'))}</td>"
        f"<td>{escape(str(row.get('entity_count') or 0))}</td>"
        f"<td>{escape(', '.join(list((row.get('entity_counts') or {}).keys())[:5]))}</td>"
        "</tr>"
        for row in zones[:8]
        if isinstance(row, dict)
    )
    water_table = (
        f"<table><thead><tr><th>{_t(lang, 'position')}</th><th>Direction</th><th>{_t(lang, 'length')}</th></tr></thead><tbody>{water_body}</tbody></table>"
        if water_body
        else f"<p class=\"muted\">{_t(lang, 'no_sites')}</p>"
    )
    patch_table = (
        f"<table><thead><tr><th>{_t(lang, 'item')}</th><th>{_t(lang, 'position')}</th><th>Samples</th><th>Amount</th></tr></thead><tbody>{patch_body}</tbody></table>"
        if patch_body
        else f"<p class=\"muted\">{_t(lang, 'no_inventory')}</p>"
    )
    zone_table = (
        f"<table><thead><tr><th>ID</th><th>{_t(lang, 'position')}</th><th>{_t(lang, 'count')}</th><th>{_t(lang, 'machines')}</th></tr></thead><tbody>{zone_body}</tbody></table>"
        if zone_body
        else f"<p class=\"muted\">{_t(lang, 'no_sites')}</p>"
    )
    return (
        _key_value_table(summary_rows, lang)
        + f"<h3>{escape(_t(lang, 'known_water_sites'))}</h3>{water_table}"
        + f"<h3>{escape(_t(lang, 'resource_patches'))}</h3>{patch_table}"
        + f"<h3>{escape(_t(lang, 'factory_zones'))}</h3>{zone_table}"
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
        status = _t(lang, "not_applied") if row.get("not_applied") else ""
        body += (
            "<article class=\"layout-candidate-card\">"
            "<div class=\"layout-candidate-head\">"
            f"<span class=\"layout-candidate-id\">{escape(str(row.get('candidate_id') or ''))}</span>"
            f"<span class=\"layout-candidate-score\">{escape(str(simulation.get('score') or 0))}</span>"
            "</div>"
            f"<p class=\"layout-candidate-pattern\">{escape(str(row.get('target_pattern') or ''))}</p>"
            f"{_layout_unlock_panel(row)}"
            f"{_layout_build_item_supply_panel(row)}"
            f"{_layout_validation_panel(row.get('validation'), lang)}"
            f"{_layout_validation_panel(row.get('sandbox_validation'), lang, title_key='sandbox_validation')}"
            f"{_layout_validation_panel(row.get('site_prebuild_gate'), lang, title_key='prebuild_gate')}"
            f"{_layout_placement_panel(row.get('site_placement_search'), lang)}"
            "<div class=\"layout-candidate-metrics\">"
            f"{_layout_metric_box(_t(lang, 'before'), simulation.get('before'))}"
            f"{_layout_metric_box(_t(lang, 'after'), simulation.get('after'))}"
            f"{_layout_metric_box(_t(lang, 'delta'), simulation.get('delta'))}"
            "</div>"
            "<div class=\"layout-candidate-footer\">"
            f"<span class=\"muted\">{escape(status)}</span>"
            f"{_candidate_blueprint_copy_cells(row, lang)}"
            "</div>"
            "</article>"
        )
    return f"<div class=\"layout-candidate-grid\">{body}</div>"


def _layout_unlock_panel(row: dict[str, Any]) -> str:
    used = [str(item) for item in row.get("uses_unlocked_items") or [] if item]
    considered = [str(item) for item in row.get("considered_unlocked_items") or [] if item]
    unused = [str(item) for item in row.get("unused_unlocked_items") or [] if item]
    unlocks = row.get("layout_unlocks_considered") if isinstance(row.get("layout_unlocks_considered"), dict) else {}
    if not considered and unlocks:
        considered = _layout_unlock_names_from_context(unlocks)
        unused = [item for item in considered if item not in set(used)]
    if not used and not considered and not unused:
        return ""
    detail_parts = []
    if considered:
        detail_parts.append(f"considered={', '.join(considered[:8])}")
    if used:
        detail_parts.append(f"used={', '.join(used[:8])}")
    if unused:
        detail_parts.append(f"not_used={', '.join(unused[:8])}")
    return (
        "<div class=\"layout-validation layout-validation-warning layout-unlocks\">"
        "<strong>Unlock-aware</strong>"
        f"<span>{escape('; '.join(detail_parts))}</span>"
        "</div>"
    )


def _layout_unlock_names_from_context(unlocks: dict[str, Any]) -> list[str]:
    names: list[str] = []
    long_handed = unlocks.get("long_handed_inserter") if isinstance(unlocks.get("long_handed_inserter"), dict) else {}
    if bool(long_handed.get("available")):
        names.append("long-handed-inserter")
    for group_name in ("modules", "machines", "furnaces", "beacons"):
        group = unlocks.get(group_name) if isinstance(unlocks.get(group_name), dict) else {}
        for name, state in group.items():
            if isinstance(state, dict) and bool(state.get("available")):
                names.append(str(name))
    return sorted(dict.fromkeys(names))


def _layout_build_item_supply_panel(row: dict[str, Any]) -> str:
    supply = row.get("build_item_supply") if isinstance(row.get("build_item_supply"), dict) else {}
    if not supply:
        return ""
    status = str(supply.get("status") or "warning")
    css_status = status if status in {"pass", "warning", "fail"} else "warning"
    missing = supply.get("missing") if isinstance(supply.get("missing"), dict) else {}
    unlocked_supply = (
        supply.get("used_unlocked_item_supply")
        if isinstance(supply.get("used_unlocked_item_supply"), dict)
        else {}
    )
    detail_parts: list[str] = []
    if missing:
        missing_text = ", ".join(f"{name} x{count}" for name, count in list(missing.items())[:5])
        if len(missing) > 5:
            missing_text += f" (+{len(missing) - 5} more)"
        detail_parts.append(f"missing={missing_text}")
    else:
        detail_parts.append("all required blueprint items available")
    unlocked_missing = {
        str(name): int(state.get("missing") or 0)
        for name, state in unlocked_supply.items()
        if isinstance(state, dict) and int(state.get("missing") or 0) > 0
    }
    if unlocked_missing:
        unlocked_text = ", ".join(f"{name} x{count}" for name, count in list(unlocked_missing.items())[:5])
        detail_parts.append(f"unlocked_tool_shortage={unlocked_text}")
    summary = str(supply.get("summary") or "")
    if summary and summary not in detail_parts:
        detail_parts.append(summary)
    return (
        f"<div class=\"layout-validation layout-validation-{escape(css_status, quote=True)} layout-build-items\">"
        "<strong>Build items</strong>"
        f"<span>{escape('; '.join(detail_parts))}</span>"
        "</div>"
    )


def _layout_validation_panel(value: Any, lang: str, *, title_key: str = "validation") -> str:
    if not isinstance(value, dict):
        return ""
    status = str(value.get("status") or "warning")
    label_key = f"validation_{status}"
    label = _t(lang, label_key) if label_key in TEXT.get(lang, {}) else status
    messages = value.get("errors") if status == "fail" else value.get("warnings")
    if not isinstance(messages, list):
        messages = value.get("reasons") if isinstance(value.get("reasons"), list) else []
    detail = "; ".join(str(item) for item in messages[:2])
    summary = str(value.get("summary") or "")
    checked = value.get("checked_machines")
    powered = value.get("powered_machines")
    inserters = value.get("checked_inserters")
    powered_inserters = value.get("powered_inserters")
    detail_parts = []
    if summary:
        detail_parts.append(summary)
    if detail and detail not in detail_parts:
        detail_parts.append(detail)
    detail_text = "; ".join(detail_parts)
    if detail_text:
        detail_text += " "
    if checked is not None:
        detail_text += f"machines={checked}"
    if powered is not None:
        detail_text += f" powered={powered}"
    if inserters is not None:
        detail_text += f" inserters={inserters}"
    if powered_inserters is not None:
        detail_text += f" inserter_powered={powered_inserters}"
    return (
        f"<div class=\"layout-validation layout-validation-{escape(status, quote=True)}\">"
        f"<strong>{escape(_t(lang, title_key))}: {escape(label)}</strong>"
        f"<span>{escape(detail_text.strip())}</span>"
        "</div>"
    )


def _layout_placement_panel(value: Any, lang: str) -> str:
    if not isinstance(value, dict):
        return ""
    status = str(value.get("status") or "blocked")
    css_status = "pass" if status == "found" else "fail"
    label_key = f"placement_{status}"
    label = _t(lang, label_key) if label_key in TEXT.get(lang, {}) else status
    summary = str(value.get("summary") or "")
    selected_anchor = value.get("selected_anchor")
    anchor_text = _position_text(selected_anchor) if isinstance(selected_anchor, dict) else ""
    evaluated = value.get("evaluated_anchors")
    parts = [summary]
    if anchor_text:
        parts.append(f"anchor={anchor_text}")
    if evaluated is not None:
        parts.append(f"evaluated={evaluated}")
    return (
        f"<div class=\"layout-validation layout-validation-{escape(css_status, quote=True)}\">"
        f"<strong>{escape(_t(lang, 'placement_search'))}: {escape(label)}</strong>"
        f"<span>{escape('; '.join(part for part in parts if part))}</span>"
        "</div>"
    )


def _layout_metric_box(title: str, value: Any) -> str:
    return f"<div class=\"layout-metric-box\"><strong>{escape(title)}</strong>{_layout_metric_rows(value)}</div>"


def _layout_metric_rows(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return f"<div class=\"metric-row\"><span class=\"metric-value\">{escape(_compact_json_text(value))}</span></div>"
    body = ""
    for key, row_value in list(value.items())[:6]:
        body += (
            "<div class=\"metric-row\">"
            f"<span class=\"metric-key\">{escape(str(key).replace('_', ' '))}</span>"
            f"<span class=\"metric-value\">{escape(_compact_json_text(row_value))}</span>"
            "</div>"
        )
    if len(value) > 6:
        body += (
            "<div class=\"metric-row\">"
            f"<span class=\"metric-key\">...</span><span class=\"metric-value\">+{len(value) - 6}</span>"
            "</div>"
        )
    return body


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


def _candidate_blueprint_copy_cells(row: dict[str, Any], lang: str) -> str:
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate_id:
        return ""
    buttons: list[str] = []
    for variant, key, label_key in [
        ("before", "before_blueprint", "copy_before_blueprint"),
        ("after", "after_blueprint", "copy_after_blueprint"),
    ]:
        blueprint = row.get(key) if isinstance(row.get(key), dict) else None
        if blueprint is None and variant == "after":
            blueprint = row.get("blueprint") if isinstance(row.get("blueprint"), dict) else None
        if not isinstance(blueprint, dict) or not blueprint.get("exchange_string"):
            continue
        label = str(blueprint.get("label") or f"{candidate_id}:{variant}")
        buttons.append(
            f"<button type=\"button\" class=\"copy-blueprint\" "
            f"data-candidate-id=\"{escape(candidate_id, quote=True)}\" "
            f"data-variant=\"{escape(variant, quote=True)}\" "
            f"title=\"{escape(label, quote=True)}\">{escape(_t(lang, label_key))}</button>"
        )
    return "<span class=\"blueprint-actions\">" + "".join(buttons) + "</span>" if buttons else ""


def _layout_llm_settings_panel(value: Any, lang: str, objective: Any) -> str:
    settings = value if isinstance(value, dict) else {}
    current = int(settings.get("max_active_layout_tasks") or 2)
    minimum = int(settings.get("min_active_layout_tasks") or 1)
    maximum = int(settings.get("max_allowed_active_layout_tasks") or 8)
    source = str(settings.get("source") or "")
    return (
        f"<form class=\"layout-llm-settings-form\" method=\"post\" "
        f"action=\"{escape(dashboard_path(lang, str(objective or '')), quote=True)}\">"
        "<input type=\"hidden\" name=\"action\" value=\"save_layout_llm_settings\">"
        f"<input type=\"hidden\" name=\"lang\" value=\"{escape(lang, quote=True)}\">"
        f"<input type=\"hidden\" name=\"objective\" value=\"{escape(str(objective or ''), quote=True)}\">"
        f"<label>{escape(_t(lang, 'max_active_layout_tasks'))}"
        f"<input name=\"max_active_layout_tasks\" type=\"number\" min=\"{minimum}\" max=\"{maximum}\" "
        f"step=\"1\" value=\"{current}\"></label>"
        f"<button type=\"submit\">{escape(_t(lang, 'save_layout_llm_settings'))}</button>"
        "</form>"
        f"<p class=\"muted\">{escape(_t(lang, 'layout_llm_hint'))} {escape(source)}</p>"
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


def _goal_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    goal = summary.get("goal") if isinstance(summary.get("goal"), dict) else {}
    if not goal.get("exists"):
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'goal_plan')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_goal_plan')}</p>"
            "</section>"
        )
    rows = "".join(
        f"<li>{escape(str(item))}</li>"
        for item in (goal.get("summary") if isinstance(goal.get("summary"), list) else [])[:8]
    )
    title = str(goal.get("title") or "goal.md")
    path = str(goal.get("path") or "")
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'goal_plan')}</h2>"
        f"<p><strong>{escape(title)}</strong></p>"
        f"<ul>{rows}</ul>"
        f"<p class=\"muted\">{escape(path)}</p>"
        "</section>"
    )


def _run_notes_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    rows = summary.get("notes") if isinstance(summary.get("notes"), list) else []
    if not rows:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'recent_run_notes')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_run_notes')}</p>"
            "</section>"
        )
    body = "".join(
        "<tr>"
        f"<td>{escape(_format_kst_timestamp(row.get('timestamp')))}</td>"
        f"<td>{escape(str(row.get('loop_type') or ''))}</td>"
        f"<td>{escape(str(row.get('goal') or ''))}</td>"
        f"<td>{escape(_yes_no(row.get('ok'), lang))}</td>"
        f"<td>{escape(str(row.get('steps') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        f"<td>{escape(str(row.get('log_path') or ''))}</td>"
        "</tr>"
        for row in reversed(rows[-12:])
        if isinstance(row, dict)
    )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'recent_run_notes')}</h2>"
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'loop_type')}</th>"
        f"<th>{_t(lang, 'executor')}</th><th>{_t(lang, 'status')}</th><th>{_t(lang, 'count')}</th>"
        f"<th>{_t(lang, 'reason')}</th><th>Log</th></tr></thead><tbody>{body}</tbody></table>"
        "</section>"
    )


def _run_insights_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    rows = summary.get("insights") if isinstance(summary.get("insights"), list) else []
    if not rows:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'recent_run_insights')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_run_insights')}</p>"
            "</section>"
        )
    body = "".join(
        "<tr>"
        f"<td>{escape(_format_kst_timestamp(row.get('timestamp')))}</td>"
        f"<td>{escape(str(row.get('kind') or ''))}</td>"
        f"<td>{escape(str(row.get('goal') or ''))}</td>"
        f"<td>{escape(str(row.get('detail') or ''))}</td>"
        f"<td>{escape(_compact_json_text(row.get('evidence') or {}))}</td>"
        "</tr>"
        for row in reversed(rows[-12:])
        if isinstance(row, dict)
    )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'recent_run_insights')}</h2>"
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'kind')}</th>"
        f"<th>{_t(lang, 'executor')}</th><th>{_t(lang, 'reason')}</th><th>Evidence</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
        "</section>"
    )


def _generated_skills_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    registered = summary.get("registered") if isinstance(summary.get("registered"), list) else []
    failures = summary.get("failures") if isinstance(summary.get("failures"), list) else []
    queue = summary.get("queue") if isinstance(summary.get("queue"), list) else []
    overrides = summary.get("overrides") if isinstance(summary.get("overrides"), list) else []
    heartbeat = summary.get("heartbeat") if isinstance(summary.get("heartbeat"), dict) else {}

    parts: list[str] = [f"<h2>{_t(lang, 'generated_skills')}</h2>"]

    if heartbeat:
        state_bits = [
            f"<span class=\"label\">{_t(lang, 'foundry_state')}</span> "
            f"<strong>{escape(str(heartbeat.get('state') or 'unknown'))}</strong>"
        ]
        if heartbeat.get("current_skill"):
            state_bits.append(escape(str(heartbeat.get("current_skill"))))
        if heartbeat.get("generated_total") is not None or heartbeat.get("failed_total") is not None:
            state_bits.append(
                f"+{escape(str(heartbeat.get('generated_total') or 0))} / "
                f"-{escape(str(heartbeat.get('failed_total') or 0))}"
            )
        if heartbeat.get("reason"):
            state_bits.append(escape(str(heartbeat.get("reason"))))
        if heartbeat.get("updated_at"):
            state_bits.append(escape(_format_kst_timestamp(heartbeat.get("updated_at"))))
        parts.append(f"<p class=\"muted\">{' &middot; '.join(state_bits)}</p>")

    if not registered and not queue and not failures and not overrides:
        parts.append(f"<p class=\"muted\">{_t(lang, 'no_generated_skills')}</p>")
        return "<section class=\"panel\">" + "".join(parts) + "</section>"

    if overrides:
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('skill_name') or ''))}</td>"
            f"<td>{escape(str(row.get('class_name') or ''))}</td>"
            f"<td>v{escape(str(row.get('version') or 0))}</td>"
            f"<td>{escape(', '.join(str(g) for g in row.get('gates_passed') or []))}</td>"
            f"<td>{escape(_format_kst_timestamp(row.get('updated_at')))}</td>"
            "</tr>"
            for row in overrides
            if isinstance(row, dict)
        )
        parts.append(
            f"<h3 class=\"subhead\">{_t(lang, 'self_repair_overrides')}</h3>"
            f"<table><thead><tr><th>{_t(lang, 'generated_skills')}</th><th>Class</th>"
            f"<th>{_t(lang, 'version')}</th><th>{_t(lang, 'foundry_gates')}</th>"
            f"<th>{_t(lang, 'updated')}</th></tr></thead><tbody>{body}</tbody></table>"
        )

    if registered:
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('skill_name') or ''))}</td>"
            f"<td>{escape(str(row.get('class_name') or ''))}</td>"
            f"<td>v{escape(str(row.get('version') or 0))}</td>"
            f"<td>{escape(', '.join(str(g) for g in row.get('gates_passed') or []))}</td>"
            f"<td>{escape(str(row.get('target_item') or ''))}</td>"
            f"<td>{escape(_format_kst_timestamp(row.get('updated_at')))}</td>"
            "</tr>"
            for row in registered
            if isinstance(row, dict)
        )
        parts.append(
            f"<h3 class=\"subhead\">{_t(lang, 'registered_skills')}</h3>"
            f"<table><thead><tr><th>{_t(lang, 'generated_skills')}</th><th>Class</th>"
            f"<th>{_t(lang, 'version')}</th><th>{_t(lang, 'foundry_gates')}</th>"
            f"<th>{_t(lang, 'item')}</th><th>{_t(lang, 'updated')}</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    if queue:
        items = "".join(
            "<li>"
            f"{escape(str(item.get('skill_name') or ''))}"
            f" <span class=\"muted\">(p{escape(str(item.get('priority') or 0))}"
            f"{(' &middot; ' + escape(str(item.get('reason')))) if item.get('reason') else ''})</span>"
            "</li>"
            for item in queue[:12]
            if isinstance(item, dict)
        )
        parts.append(f"<h3 class=\"subhead\">{_t(lang, 'foundry_queue')}</h3><ul>{items}</ul>")

    if failures:
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('skill_name') or ''))}</td>"
            f"<td>{escape(str(row.get('status') or ''))}</td>"
            f"<td>{escape(str(row.get('attempts') or 0))}</td>"
            f"<td>{escape(str(row.get('last_failure_reason') or ''))}</td>"
            "</tr>"
            for row in failures[:12]
            if isinstance(row, dict)
        )
        parts.append(
            f"<h3 class=\"subhead\">{_t(lang, 'foundry_failures')}</h3>"
            f"<table><thead><tr><th>{_t(lang, 'generated_skills')}</th><th>{_t(lang, 'foundry_state')}</th>"
            f"<th>{_t(lang, 'count')}</th><th>{_t(lang, 'reason')}</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    return "<section class=\"panel\">" + "".join(parts) + "</section>"


def _trace_archive_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    rows = summary.get("archives") if isinstance(summary.get("archives"), list) else []
    if not rows:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'trace_archives')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_trace_archives')}</p>"
            f"<p class=\"muted\">{escape(str(summary.get('archive_root') or ''))}</p>"
            "</section>"
        )
    body = "".join(
        "<tr>"
        f"<td>{escape(_format_kst_timestamp(row.get('created_at')) or str(row.get('created_at_kst') or ''))}</td>"
        f"<td>{escape(str(row.get('label') or ''))}</td>"
        f"<td>{escape(str(row.get('source_count') or 0))}</td>"
        f"<td>{escape(str(row.get('high_value_files') or 0))}</td>"
        f"<td>{escape(_compact_json_text(row.get('category_counts') or {}))}</td>"
        f"<td>{escape(str(row.get('archive_dir') or ''))}</td>"
        "</tr>"
        for row in rows[:8]
        if isinstance(row, dict)
    )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'trace_archives')}</h2>"
        f"<p class=\"muted\">{_t(lang, 'archive_root')}: {escape(str(summary.get('archive_root') or ''))}</p>"
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'archive')}</th>"
        f"<th>{_t(lang, 'count')}</th><th>{_t(lang, 'high_value')}</th>"
        f"<th>{_t(lang, 'categories')}</th><th>Path</th></tr></thead><tbody>{body}</tbody></table>"
        "</section>"
    )


def _compact_json_text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 180 else text[:177] + "..."


def _selected_improvement_site_panel(selected_improvement_site: dict[str, Any], lang: str, objective: Any) -> str:
    if not selected_improvement_site:
        return ""
    details: list[str] = []
    kind = str(selected_improvement_site.get("kind") or "")
    item = str(selected_improvement_site.get("item") or "")
    position_text = _position_text(selected_improvement_site.get("position"))
    selected_at = _format_kst_timestamp(selected_improvement_site.get("selected_at"))
    if kind:
        details.append(f"<span>{escape(kind)}</span>")
    if item:
        details.append(_item_cell(item))
    if position_text:
        details.append(f"<span>{escape(position_text)}</span>")
    if selected_at:
        details.append(f"<span>{escape(selected_at)}</span>")
    return (
        "<div class=\"site-improvement-summary\">"
        f"<strong>{escape(_t(lang, 'selected_improvement_target'))}</strong>"
        f"<span class=\"site-selected-badge\">{escape(_t(lang, 'selected_improvement_site'))}</span>"
        f"{''.join(details)}"
        f"{_clear_improvement_site_form(lang, objective)}"
        "</div>"
    )


def _factory_site_table(
    rows: list[Any],
    links: list[Any],
    selected_improvement_site: dict[str, Any],
    lang: str,
    objective: Any,
) -> str:
    if not rows:
        return f"<p class=\"muted\">{_t(lang, 'no_sites')}</p>"
    link_rows = [row for row in links if isinstance(row, dict)]
    body_parts: list[str] = []
    for row in rows[:80]:
        if not isinstance(row, dict):
            continue
        machines = row.get("machines") if isinstance(row.get("machines"), list) else []
        subitems = row.get("subitems") if isinstance(row.get("subitems"), list) else []
        item_html = _item_cell(str(row.get("item") or "")) if row.get("item") else ""
        if subitems:
            item_html += (
                "<br><span class=\"muted\">"
                f"subitems: {escape(', '.join(str(item) for item in subitems[:5]))}"
                "</span>"
            )
        related = _site_logistics_links(row, link_rows)
        is_selected = str(selected_improvement_site.get("site_id") or "") == str(row.get("site_id") or "")
        row_class = " class=\"site-selected-row\"" if is_selected else ""
        body_parts.append(
            f"<tr{row_class}>"
            f"<td>{escape(str(row.get('kind') or ''))}</td>"
            f"<td>{item_html}</td>"
            f"<td>{escape(str(row.get('status') or ''))}</td>"
            f"<td>{escape(_position_text(row.get('position')))}</td>"
            f"<td>{escape(str(row.get('automation_level') or ''))}</td>"
            f"<td>{escape(', '.join(str(item) for item in machines[:5]))}</td>"
            f"<td>{_site_blueprint_copy_cell(row, lang)}</td>"
            f"<td>{_site_improvement_select_cell(row, selected_improvement_site, lang, objective)}</td>"
            "</tr>"
        )
        if related:
            body_parts.append(
                "<tr class=\"site-logistics-row\">"
                f"<td colspan=\"8\">{_site_logistics_list(row, related, lang)}</td>"
                "</tr>"
            )
    return (
        f"<table><thead><tr><th>{_t(lang, 'kind')}</th><th>{_t(lang, 'item')}</th>"
        f"<th>{_t(lang, 'status')}</th><th>{_t(lang, 'position')}</th>"
        f"<th>{_t(lang, 'automation')}</th><th>{_t(lang, 'machines')}</th>"
        f"<th>{_t(lang, 'blueprint')}</th><th>{_t(lang, 'improve_site')}</th></tr></thead>"
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def _site_logistics_links(site: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact_aliases, position_aliases = _site_link_aliases(site)
    if not exact_aliases and not position_aliases:
        return []
    return [
        row
        for row in links
        if _link_endpoint_matches_site(row.get("from_site"), exact_aliases, position_aliases)
        or _link_endpoint_matches_site(row.get("to_site"), exact_aliases, position_aliases)
    ][:12]


def _site_link_aliases(site: dict[str, Any]) -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    position_aliases = _position_aliases(site.get("position"))
    site_id = str(site.get("site_id") or "").strip()
    kind = str(site.get("kind") or "").strip()
    item = str(site.get("item") or "").strip()
    if site_id:
        exact.add(site_id)
    for position in position_aliases:
        if kind:
            exact.add(f"{kind}:{position}")
        if kind and item:
            exact.add(f"{kind}:group:{item}:{position}")
        if kind == "plate_smelting_line":
            exact.add(f"smelting:{position}")
            if item:
                exact.add(f"smelting:group:{item}:{position}")
        if kind == "mining_patch" and item:
            exact.add(f"mining_patch:group:{item}:{position}")
    return exact, position_aliases


def _position_aliases(value: Any) -> set[str]:
    pair = _position_pair(value)
    if pair is None:
        return set()
    x, y = pair
    return {f"{x:.1f},{y:.1f}", f"{x:g},{y:g}"}


def _link_endpoint_matches_site(value: Any, exact_aliases: set[str], position_aliases: set[str]) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text in exact_aliases:
        return True
    endpoint_position = text.rsplit(":", 1)[-1]
    return bool(endpoint_position in position_aliases)


def _site_logistics_list(site: dict[str, Any], rows: list[dict[str, Any]], lang: str) -> str:
    entries = "".join(_site_logistics_entry(site, row, lang) for row in rows[:8])
    more = ""
    if len(rows) > 8:
        more = f"<div class=\"site-logistics-more\">+{len(rows) - 8}</div>"
    return (
        "<div class=\"site-logistics-list\">"
        f"<div class=\"site-logistics-title\">{escape(_t(lang, 'site_logistics'))}</div>"
        f"{entries}{more}"
        "</div>"
    )


def _site_logistics_entry(site: dict[str, Any], row: dict[str, Any], lang: str) -> str:
    direction = _site_logistics_direction(site, row, lang)
    kind = str(row.get("kind") or "")
    item = str(row.get("item") or "")
    status = str(row.get("status") or "")
    length = row.get("length_tiles")
    length_text = "" if length in (None, "") else f"{_t(lang, 'length')}={length}"
    path = f"{row.get('from_site') or ''} -> {row.get('to_site') or ''}"
    return (
        "<div class=\"site-logistics-link\">"
        f"<span class=\"site-logistics-direction\">{escape(direction)}</span>"
        f"<span class=\"site-logistics-kind\">{escape(kind)}</span>"
        f"{_item_cell(item) if item else ''}"
        f"<span class=\"site-logistics-path\">{escape(path)}</span>"
        f"<span class=\"site-logistics-status\">{escape(status)}</span>"
        f"<span class=\"site-logistics-length\">{escape(length_text)}</span>"
        "</div>"
    )


def _site_logistics_direction(site: dict[str, Any], row: dict[str, Any], lang: str) -> str:
    exact_aliases, position_aliases = _site_link_aliases(site)
    from_match = _link_endpoint_matches_site(row.get("from_site"), exact_aliases, position_aliases)
    to_match = _link_endpoint_matches_site(row.get("to_site"), exact_aliases, position_aliases)
    if from_match and not to_match:
        return _t(lang, "outbound")
    if to_match and not from_match:
        return _t(lang, "inbound")
    return _t(lang, "linked")


def _unassigned_logistics_table(sites: list[Any], links: list[Any], lang: str) -> str:
    link_rows = [row for row in links if isinstance(row, dict)]
    if not link_rows:
        return ""
    matched_ids: set[int] = set()
    for site in sites:
        if not isinstance(site, dict):
            continue
        matched_ids.update(id(row) for row in _site_logistics_links(site, link_rows))
    unmatched = [row for row in link_rows if id(row) not in matched_ids]
    if not unmatched:
        return ""
    return (
        "<div class=\"site-logistics-unmatched\">"
        f"<h3>{escape(_t(lang, 'unassigned_logistics'))}</h3>"
        f"{_logistics_link_table(unmatched, lang)}"
        "</div>"
    )


def _site_improvement_select_cell(
    row: dict[str, Any],
    selected_improvement_site: dict[str, Any],
    lang: str,
    objective: Any,
) -> str:
    site_id = str(row.get("site_id") or "")
    if not site_id:
        return ""
    selected_site_id = str(selected_improvement_site.get("site_id") or "")
    if selected_site_id == site_id:
        return (
            "<div class=\"site-improvement-selected\">"
            f"<span class=\"site-selected-badge\">{escape(_t(lang, 'selected_improvement_site'))}</span>"
            f"{_clear_improvement_site_form(lang, objective)}"
            "</div>"
        )
    position = _position_pair(row.get("position"))
    position_inputs = ""
    if position is not None:
        position_inputs = (
            f"<input type=\"hidden\" name=\"site_position_x\" value=\"{position[0]:.3f}\">"
            f"<input type=\"hidden\" name=\"site_position_y\" value=\"{position[1]:.3f}\">"
        )
    return (
        f"<form class=\"site-improvement-form\" method=\"post\" "
        f"action=\"{escape(dashboard_path(lang, str(objective or '')), quote=True)}\">"
        "<input type=\"hidden\" name=\"action\" value=\"select_improvement_site\">"
        f"<input type=\"hidden\" name=\"lang\" value=\"{escape(lang, quote=True)}\">"
        f"<input type=\"hidden\" name=\"objective\" value=\"{escape(str(objective or ''), quote=True)}\">"
        f"<input type=\"hidden\" name=\"site_id\" value=\"{escape(site_id, quote=True)}\">"
        f"<input type=\"hidden\" name=\"site_kind\" value=\"{escape(str(row.get('kind') or ''), quote=True)}\">"
        f"<input type=\"hidden\" name=\"site_item\" value=\"{escape(str(row.get('item') or ''), quote=True)}\">"
        f"<input type=\"hidden\" name=\"site_status\" value=\"{escape(str(row.get('status') or ''), quote=True)}\">"
        f"<input type=\"hidden\" name=\"site_automation_level\" value=\"{escape(str(row.get('automation_level') or ''), quote=True)}\">"
        f"{position_inputs}"
        f"<button type=\"submit\" class=\"site-improvement-button\">{escape(_t(lang, 'select_improvement_site'))}</button>"
        "</form>"
    )


def _clear_improvement_site_form(lang: str, objective: Any) -> str:
    return (
        f"<form class=\"site-improvement-form\" method=\"post\" "
        f"action=\"{escape(dashboard_path(lang, str(objective or '')), quote=True)}\">"
        "<input type=\"hidden\" name=\"action\" value=\"clear_improvement_site\">"
        f"<input type=\"hidden\" name=\"lang\" value=\"{escape(lang, quote=True)}\">"
        f"<input type=\"hidden\" name=\"objective\" value=\"{escape(str(objective or ''), quote=True)}\">"
        f"<button type=\"submit\" class=\"site-improvement-cancel-button\">{escape(_t(lang, 'clear_improvement_site'))}</button>"
        "</form>"
    )


def _site_blueprint_copy_cell(row: dict[str, Any], lang: str) -> str:
    blueprint = row.get("blueprint") if isinstance(row.get("blueprint"), dict) else {}
    if not isinstance(blueprint.get("exchange_string"), str) or not blueprint.get("exchange_string"):
        return ""
    site_id = str(row.get("site_id") or "")
    if not site_id:
        return ""
    label = str(blueprint.get("label") or site_id)
    return (
        f"<button type=\"button\" class=\"copy-blueprint\" "
        f"data-site-id=\"{escape(site_id, quote=True)}\" "
        f"title=\"{escape(label, quote=True)}\">{escape(_t(lang, 'copy_blueprint'))}</button>"
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


def _agent_activity_panel(marker: dict[str, Any], player: Any, execution: dict[str, Any], lang: str) -> str:
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
        (_t(lang, "execution_mode"), _execution_mode_text(execution, player)),
        (_t(lang, "character_valid"), _character_valid_text(execution, player, lang)),
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


def _execution_mode_text(execution: dict[str, Any], player: Any) -> str:
    mode = str(execution.get("mode") or "").strip()
    if mode:
        return mode
    if isinstance(player, dict):
        kind = str(player.get("kind") or "").strip()
        if kind == "server":
            return "virtual"
        if kind:
            return "player"
    return ""


def _character_valid_text(execution: dict[str, Any], player: Any, lang: str) -> str:
    value = execution.get("character_valid")
    if value is None and isinstance(player, dict):
        value = player.get("character_valid")
    if value is None:
        return ""
    if bool(value):
        return "valid" if lang == "en" else "유효"
    return "missing" if lang == "en" else "없음"


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
    basis = _token_usage_basis_description(lang)
    if not samples:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'token_usage')}</h2>"
            f"{basis}"
            f"<p class=\"muted\">{_t(lang, 'no_token_usage')}</p>"
            "</section>"
        )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'token_usage')}</h2>"
        f"{basis}"
        "<div class=\"metrics\">"
        f"{_metric(_t(lang, 'latest_tokens'), _format_token_count(usage.get('latest_tokens')))}"
        f"{_metric(_t(lang, 'total_delta_tokens'), _format_token_count(usage.get('total_delta_tokens')))}"
        f"{_metric(_t(lang, 'latest_delta_tokens'), _format_token_count(usage.get('latest_delta_tokens')))}"
        f"{_metric(_t(lang, 'weekly_quota'), _format_token_count_or_unknown(usage.get('weekly_quota_tokens')))}"
        f"{_metric(_t(lang, 'weekly_percent'), _format_percent_or_unknown(usage.get('latest_weekly_percent')))}"
        f"{_metric(_t(lang, 'sample_count'), _format_int(usage.get('sample_count')))}"
        f"{_metric(_t(lang, 'counter_resets'), _format_int(usage.get('counter_reset_count')))}"
        f"{_metric(_t(lang, 'last_sample'), _format_kst_timestamp(usage.get('updated_at')))}"
        "</div>"
        f"{_token_usage_svg(samples)}"
        f"{_token_usage_table(samples, lang)}"
        "</section>"
    )


def _token_usage_basis_description(lang: str) -> str:
    if lang == "ko":
        text = "현재 Factorio Codex thread의 threads.tokens_used 기준"
    else:
        text = "Based on threads.tokens_used for the current Factorio Codex thread"
    return f"<p class=\"muted\">{escape(text)}</p>"


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


def _strategy_worker_comparison_panel(value: Any, lang: str) -> str:
    summary = value if isinstance(value, dict) else {}
    latest = summary.get("latest") if isinstance(summary.get("latest"), dict) else {}
    workers = latest.get("workers") if isinstance(latest.get("workers"), list) else []
    if not workers:
        return (
            "<section class=\"panel\">"
            f"<h2>{_t(lang, 'llm_worker_comparison')}</h2>"
            f"<p class=\"muted\">{_t(lang, 'no_llm_worker_comparison')}</p>"
            "</section>"
        )
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('label') or ''))}</td>"
        f"<td>{escape(str(row.get('model') or ''))}</td>"
        f"<td>{escape(_yes_no(row.get('llm_ready'), lang))}</td>"
        f"<td>{escape(str(row.get('source') or ''))}</td>"
        f"<td>{escape(str(row.get('selected_skill') or ''))}</td>"
        f"<td>{escape(str(row.get('priority') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        f"<td>{escape(str(row.get('error') or row.get('llm_error') or ''))}</td>"
        f"<td>{escape(str(row.get('latency_ms') or 0))}</td>"
        "</tr>"
        for row in workers
        if isinstance(row, dict)
    )
    return (
        "<section class=\"panel\">"
        f"<h2>{_t(lang, 'llm_worker_comparison')}</h2>"
        f"<p class=\"muted\">{escape(_format_kst_timestamp(latest.get('created_at')))}</p>"
        f"<table><thead><tr><th>{_t(lang, 'worker')}</th><th>{_t(lang, 'model')}</th>"
        f"<th>{_t(lang, 'ready')}</th><th>{_t(lang, 'source')}</th><th>{_t(lang, 'executor')}</th>"
        f"<th>{_t(lang, 'priority')}</th><th>{_t(lang, 'reason')}</th><th>{_t(lang, 'error')}</th>"
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
            value = _token_usage_display_value(sample)
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
        f"<text x=\"{left}\" y=\"{top + plot_height - 4:.1f}\" fill=\"#9aa4af\" font-size=\"11\">{escape(_format_token_count(min_value))}</text>"
        f"<text x=\"{left}\" y=\"14\" fill=\"#9aa4af\" font-size=\"11\">{escape(_format_token_count(max_value))}</text>"
        f"{axis_labels}"
        f"<polyline class=\"usage-line\" points=\"{' '.join(points)}\" />"
        f"{''.join(circles)}"
        "</svg>"
    )


def _token_usage_table(samples: list[Any], lang: str) -> str:
    rows = [row for row in samples if isinstance(row, dict)]
    body_parts: list[str] = []
    for index, row in list(enumerate(rows))[-8:]:
        previous = rows[index - 1] if index > 0 else None
        body_parts.append(
            "<tr>"
            f"<td>{escape(_format_kst_timestamp(row.get('timestamp')))}</td>"
            f"<td>{escape(str(row.get('label') or ''))}</td>"
            f"<td>{escape(_format_token_count(row.get('delta_tokens')))}</td>"
            f"<td>{escape(_format_percent_or_unknown(row.get('weekly_percent')))}</td>"
            f"<td>{escape(_format_token_rate_per_hour(row, previous))}</td>"
            f"<td>{escape(_format_token_count(_token_usage_display_value(row)))}</td>"
            "</tr>"
        )
    return (
        f"<table><thead><tr><th>{_t(lang, 'updated')}</th><th>{_t(lang, 'reason')}</th>"
        f"<th>{_t(lang, 'total_delta_tokens')}</th><th>{_t(lang, 'weekly_percent')}</th>"
        f"<th>{_t(lang, 'tokens_per_hour')}</th>"
        f"<th>{_t(lang, 'latest_tokens')}</th></tr></thead>"
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def _format_token_rate_per_hour(row: dict[str, Any], previous: dict[str, Any] | None) -> str:
    if not isinstance(previous, dict):
        return ""
    current_time = _timestamp_seconds(row.get("timestamp"))
    previous_time = _timestamp_seconds(previous.get("timestamp"))
    if current_time is None or previous_time is None:
        return ""
    elapsed_seconds = current_time - previous_time
    if elapsed_seconds <= 0:
        return ""
    delta = _token_delta(row, previous)
    if delta is None:
        return ""
    return _format_token_count(round(delta * 3600.0 / elapsed_seconds))


def _token_usage_display_value(row: dict[str, Any]) -> int:
    return int(row.get("cumulative_tokens") or row.get("tokens_used") or 0)


def _token_delta(row: dict[str, Any], previous: dict[str, Any]) -> int | None:
    try:
        delta = int(row.get("delta_tokens"))
    except (TypeError, ValueError):
        delta = 0
    if delta > 0:
        return delta
    try:
        fallback = int(row.get("tokens_used") or 0) - int(previous.get("tokens_used") or 0)
    except (TypeError, ValueError):
        return None
    return fallback if fallback > 0 else None


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


def _dep_amount(amount: Any) -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return escape(str(amount))
    return str(int(value)) if value == int(value) else f"{value:g}"


def _dep_node_html(node: Any, amount: Any = None, path: str = "") -> str:
    if not isinstance(node, dict):
        return ""
    name = str(node.get("item") or "?")
    item = escape(name)
    node_path = f"{path}/{name}" if path else name
    amt = "" if amount is None else f' <span class="dep-amt">x{_dep_amount(amount)}</span>'
    if node.get("cycle_or_depth_limit"):
        return f'<div class="dep-leaf">{item}{amt} <span class="muted">...</span></div>'
    tag = ""
    if node.get("raw_resource"):
        tag = ' <span class="dep-raw">raw</span>'
    elif node.get("technology"):
        tag = f' <span class="dep-tech">[{escape(str(node.get("technology")))}]</span>'
    children = node.get("ingredients") if isinstance(node.get("ingredients"), list) else []
    if not children:
        return f'<div class="dep-leaf">{item}{amt}{tag}</div>'
    inner = "".join(
        _dep_node_html(
            child.get("dependency") if isinstance(child, dict) else None,
            child.get("amount") if isinstance(child, dict) else None,
            node_path,
        )
        for child in children
    )
    # Stable id (recipe tree is structural, so the path is stable across refreshes)
    # lets the client persist open/closed state -- see the persistence script in _page.
    det_id = escape(f"dep:{node_path}")
    return f'<details class="dep-node" id="{det_id}"><summary>{item}{amt}{tag}</summary>{inner}</details>'


def _dependency_tree_html(forest: Any) -> str:
    if not isinstance(forest, list) or not forest:
        return '<p class="muted">(none)</p>'
    roots = [n for n in forest if isinstance(n, dict) and not n.get("infrastructure")]
    infra = [n for n in forest if isinstance(n, dict) and n.get("infrastructure")]
    parts = ['<div class="deptree">']
    for node in roots:
        name = str(node.get("item") or "?")
        item = escape(name)
        tech = node.get("technology")
        tag = f' <span class="dep-tech">[{escape(str(tech))}]</span>' if tech else ""
        children = node.get("ingredients") if isinstance(node.get("ingredients"), list) else []
        inner = "".join(
            _dep_node_html(
                child.get("dependency") if isinstance(child, dict) else None,
                child.get("amount") if isinstance(child, dict) else None,
                name,
            )
            for child in children
        ) or '<div class="dep-leaf muted">(raw / no recipe)</div>'
        det_id = escape(f"dep:{name}")
        parts.append(f'<details class="dep-node" id="{det_id}" open><summary>{item}{tag}</summary>{inner}</details>')
    if infra:
        parts.append('<details class="dep-infra" id="dep:__infra__"><summary><strong>production buildings</strong></summary>')
        for node in infra:
            parts.append(_dep_node_html(node, path="infra"))
        parts.append("</details>")
    parts.append("</div>")
    return "".join(parts)


def _dependency_map_html(flat_map: Any, facilities: Any = None) -> str:
    """Full recipe map view: EVERY item -> direct ingredients + amounts, output count,
    crafting category, and fluid flag. A compact category->facility legend is shown once
    on top (not repeated per item). Each item is a collapsed <details> with a stable id so
    open/closed state survives the auto-refresh.
    """
    if not isinstance(flat_map, dict) or not flat_map:
        return '<p class="muted">(none)</p>'
    parts = [
        f'<p class="muted">{len(flat_map)} items '
        f'(item: ingredient&times;amount; [category] -&gt; facility legend; out&times;N if &gt;1; fluid marked)</p>'
    ]
    if isinstance(facilities, dict) and facilities:
        rows = "".join(
            f"<tr><td>{escape(str(cat))}</td><td>{escape(', '.join(str(m) for m in machines))}</td></tr>"
            for cat, machines in sorted(facilities.items())
        )
        parts.append(
            '<details class="dep-infra" id="depmap:__facilities__"><summary><strong>crafting facilities '
            f'(category &rarr; machines)</strong></summary><table><thead><tr><th>category</th><th>facilities'
            f'</th></tr></thead><tbody>{rows}</tbody></table></details>'
        )
    parts.append('<div class="deptree">')
    for item in sorted(flat_map):
        entry = flat_map[item]
        item_e = escape(str(item))
        det_id = escape(f"depmap:{item}")
        if isinstance(entry, dict):
            ings = entry.get("in") if isinstance(entry.get("in"), dict) else {}
            ing_html = ", ".join(f"{escape(str(k))}&times;{escape(str(v))}" for k, v in ings.items()) or "(raw inputs)"
            tags = ""
            if entry.get("out") not in (None, 1):
                tags += f' <span class="dep-amt">out&times;{escape(str(entry.get("out")))}</span>'
            if entry.get("cat"):
                tags += f' <span class="dep-tech">[{escape(str(entry.get("cat")))}]</span>'
            if entry.get("fluid"):
                tags += ' <span class="dep-raw">fluid</span>'
            parts.append(
                f'<details class="dep-node" id="{det_id}"><summary>{item_e}{tags}</summary>'
                f'<div class="dep-leaf">{ing_html}</div></details>'
            )
        elif isinstance(entry, list):  # legacy flat name list
            ing_html = ", ".join(escape(str(i)) for i in entry) or "(raw)"
            parts.append(
                f'<details class="dep-node" id="{det_id}"><summary>{item_e}</summary>'
                f'<div class="dep-leaf">{ing_html}</div></details>'
            )
    parts.append("</div>")
    return "".join(parts)


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


def _format_int_or_unknown(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    return _format_int(value)


def _format_token_count(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{sign}{absolute / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:.1f}M"
    return f"{number:,}"


def _format_token_count_or_unknown(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    return _format_token_count(value)


def _format_percent_or_unknown(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    try:
        return f"{float(value):.4f}%"
    except (TypeError, ValueError):
        return "unknown"


def _yes_no(value: Any, lang: str) -> str:
    if lang == "ko":
        return "yes" if bool(value) else "no"
    return "yes" if bool(value) else "no"


def _t(lang: str, key: str) -> str:
    return TEXT.get(lang, TEXT[DEFAULT_LANG]).get(key, TEXT[DEFAULT_LANG].get(key, key))
