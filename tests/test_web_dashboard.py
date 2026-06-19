import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_ai.networking import dashboard_urls
from factorio_ai.web_dashboard import (
    FACTORIO_LLM_ROUTE,
    FACTORIO_ROUTE,
    FACTORIO_BLUEPRINT_ROUTE,
    LLM_API_ROUTE,
    _candidate_blueprint_response,
    _generated_skills_panel,
    _handle_dashboard_post_values,
    _site_blueprint_response,
    _token_usage_panel,
    _token_usage_table,
    _token_usage_svg,
    build_dashboard_state,
    build_dashboard_state_cached,
    clear_dashboard_state_cache,
    dashboard_path,
    friendly_dashboard_error,
    generated_skills_summary,
    is_factorio_route,
    llm_trace_api_response,
    llm_trace_path,
    observe_dashboard_state,
    render_dashboard,
    render_llm_trace_page,
    request_language,
)
from factorio_ai.llm_log import make_llm_io_trace, record_llm_io_trace
from factorio_ai.site_selection import load_selected_improvement_site, save_selected_improvement_site


class WebDashboardTests(unittest.TestCase):
    def test_factorio_routes(self):
        self.assertEqual(FACTORIO_ROUTE, "/factorio")
        self.assertEqual(FACTORIO_LLM_ROUTE, "/factorio/llm")
        self.assertEqual(LLM_API_ROUTE, "/api/factorio/llm")
        self.assertTrue(is_factorio_route("/factorio"))
        self.assertTrue(is_factorio_route("/factorio/"))
        self.assertTrue(is_factorio_route("/factorio/llm"))
        self.assertTrue(is_factorio_route("/팩토리오"))
        self.assertTrue(is_factorio_route("/%ED%8C%A9%ED%86%A0%EB%A6%AC%EC%98%A4"))
        self.assertEqual(llm_trace_path("en", "launch_rocket_program"), "/factorio/llm?objective=launch_rocket_program")

    def test_legacy_route_defaults_to_korean(self):
        self.assertEqual(request_language("/팩토리오", {}), "ko")
        self.assertEqual(dashboard_path("ko"), "/factorio?lang=ko")

    def test_llm_trace_page_renders_escaped_collapsible_blocks(self):
        long_output = "<script>alert(1)</script>" + ("x" * 7000)
        html = render_llm_trace_page(
            {
                "entries": [
                    {
                        "timestamp": "2026-06-13T00:00:00+00:00",
                        "trace_id": "trace-a",
                        "kind": "strategy",
                        "provider": "local_llm",
                        "model": "Qwen/Qwen3.5-9B",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "task_id": "strategy-1",
                        "system_prompt": "Return JSON",
                        "input_prompt": "choose next skill",
                        "raw_output": long_output,
                        "parsed_json": {"selected_skill": "research_automation"},
                        "duration_ms": 42,
                        "prompt_chars": 24,
                        "response_chars": len(long_output),
                        "max_tokens": 512,
                        "ok": True,
                        "error": "",
                    }
                ],
                "entry_count": 1,
                "latest": {},
                "log_path": "logs/llm_io_traces.jsonl",
            },
            "en",
            "launch_rocket_program",
        )

        self.assertIn("LLM I/O Traces", html)
        self.assertIn("trace-a", html)
        self.assertIn("Qwen/Qwen3.5-9B", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("truncated", html)
        self.assertIn("&quot;selected_skill&quot;: &quot;research_automation&quot;", html)
        self.assertIn('href="/factorio?objective=launch_rocket_program"', html)
        self.assertIn(LLM_API_ROUTE, html)

    def test_llm_trace_page_shows_all_kinds_and_filters_by_kind(self):
        def _entry(trace_id: str, kind: str) -> dict:
            return {
                "timestamp": f"2026-06-13T00:00:0{trace_id[-1]}+00:00",
                "trace_id": trace_id,
                "kind": kind,
                "provider": "local_llm",
                "model": "Qwen",
                "base_url": "http://127.0.0.1:8000/v1",
                "task_id": trace_id,
                "system_prompt": "sys",
                "input_prompt": f"prompt-{trace_id}",
                "raw_output": "{}",
                "parsed_json": {},
                "duration_ms": 1,
                "prompt_chars": 3,
                "response_chars": 2,
                "max_tokens": 64,
                "ok": True,
                "error": "",
            }

        summary = {
            "entries": [_entry("t-strat", "strategy"), _entry("t-found", "skill_foundry"), _entry("t-lay", "layout")],
            "entry_count": 3,
            "latest": {},
            "log_path": "logs/llm_io_traces.jsonl",
        }

        # Unfiltered: all kinds present plus filter chips with counts.
        html_all = render_llm_trace_page(summary, "en", "launch_rocket_program")
        self.assertIn("t-strat", html_all)
        self.assertIn("t-found", html_all)
        self.assertIn("t-lay", html_all)
        self.assertIn("skill_foundry (1)", html_all)
        self.assertIn("layout (1)", html_all)
        self.assertIn("all (3)", html_all)
        self.assertIn("kind=skill_foundry", html_all)

        # Filtered to skill_foundry: only that entry's card renders.
        html_foundry = render_llm_trace_page(summary, "en", "launch_rocket_program", kind="skill_foundry")
        self.assertIn("t-found", html_foundry)
        self.assertNotIn("prompt-t-strat", html_foundry)
        self.assertNotIn("prompt-t-lay", html_foundry)

    def test_llm_trace_api_response_loads_recent_entries_newest_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "logs"
            for trace_id in ("old", "new"):
                record_llm_io_trace(
                    log_dir,
                    make_llm_io_trace(
                        trace_id=trace_id,
                        kind="strategy",
                        provider="local_llm",
                        model="model",
                        base_url="http://localhost",
                        system_prompt="system",
                        input_prompt=trace_id,
                        raw_output='{"ok":true}',
                        parsed_json={"ok": True},
                        ok=True,
                    ),
                )

            response = llm_trace_api_response(log_dir, limit=2)

        self.assertEqual(response["entry_count"], 2)
        self.assertEqual([row["trace_id"] for row in response["entries"]], ["new", "old"])
        self.assertEqual(response["entries"][0]["input_prompt"], "new")

    def test_dashboard_html_has_monitor_sections_and_item_icons(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "no-mod-rcon-lua",
                "player": {"name": "server", "kind": "server", "position": {"x": 1, "y": 2}},
                "execution": {"mode": "virtual", "virtual": True, "character_valid": False},
                "agent_marker": {
                    "name": "server",
                    "kind": "server",
                    "position": {"x": 1, "y": 2},
                    "target_position": {"x": 8, "y": 9},
                    "last_action": "mine",
                    "detail": "copper-ore",
                    "tick": 1,
                },
                "targets": {"per_minute": {"iron-plate": 30.0}},
                "monitor": {
                    "production": [{"item": "iron-plate", "per_minute": 30.0, "producers": 1, "confidence": 0.5}],
                    "throughput_constraints": [
                        {
                            "item": "copper-cable",
                            "required_per_minute": 180.0,
                            "available_per_minute": 120.0,
                            "bottleneck": "copper-cable assembler ratio",
                            "notes": ["assembling-machine-1 copper-cable:electronic-circuit ratio is 3:2"],
                        }
                    ],
                    "bottlenecks": [],
                    "power_networks": [
                        {
                            "network_id": "7",
                            "generation_kw": 900.0,
                            "demand_kw": 75.0,
                            "satisfaction": 1.0,
                            "status": "ok",
                            "producers": 1,
                            "consumers": 1,
                            "unconnected_consumers": 0,
                            "notes": ["power is shared only inside one connected electric network"],
                        }
                    ],
                    "inventory": {"iron-plate": 3},
                    "technology_chain": [],
                    "dependency_tree": [],
                    "target_status": {"all_satisfied": True, "items": []},
                    "factory_sites": [
                        {
                            "kind": "build_item_mall",
                            "item": "transport-belt",
                            "status": "running",
                            "position": {"x": 2, "y": 2},
                            "automation_level": "powered",
                            "machines": ["assembling-machine-1"],
                            "site_id": "build_item_mall:2,2",
                            "blueprint": {
                                "label": "build item mall",
                                "format": "factorio-blueprint-string",
                                "entity_count": 1,
                                "exchange_string": "0SITEBLUEPRINTSTRING",
                            },
                        }
                    ],
                    "logistics_links": [
                        {
                            "kind": "belt",
                            "item": "iron-ore",
                            "from_site": "smelting:9,0",
                            "to_site": "build_item_mall:2,2",
                            "status": "complete",
                            "length_tiles": 5,
                        }
                    ],
                    "factory_events": [
                        {
                            "tick": 10,
                            "actor": "r1jae",
                            "action": "built",
                            "entity": "assembling-machine-1",
                            "position": {"x": 4, "y": 5},
                        }
                    ],
                    "damage_events": [
                        {
                            "tick": 12,
                            "action": "damaged",
                            "entity": "stone-furnace",
                            "cause": "small-biter",
                            "damage": 5,
                            "health": 45,
                            "position": {"x": 6, "y": 7},
                        }
                    ],
                    "threats": {
                        "danger_level": "high",
                        "enemy_count": 1,
                        "nearest_enemy": {"name": "small-biter", "type": "unit", "distance": 20},
                        "nearest_spawner": None,
                        "armed_gun_turret_count": 0,
                        "unarmed_gun_turret_count": 0,
                        "recent_damage_count": 1,
                        "max_spawner_pollution": 0,
                        "recommended_actions": ["run build_starter_defense before expanding production"],
                    },
                },
                "strategy": {"selected_skill": "produce_iron_plate", "priority": 95, "skill_status": {"implemented": True}},
                "layout_improvement": {
                    "issues": [],
                    "opportunities": [
                        {
                            "kind": "rebalance_green_circuit_ratio",
                            "severity": 78,
                            "item": "electronic-circuit",
                            "site_id": "circuit_automation:green",
                            "detail": "ratio mismatch",
                            "recommendation": "use 3 cable assemblers per 2 circuit assemblers",
                        }
                    ],
                    "simulation_candidates": [
                        {
                            "candidate_id": "green-circuit-3-cable-2-circuit-cell",
                            "target_pattern": "3 copper-cable assemblers feeding 2 electronic-circuit assemblers",
                            "not_applied": True,
                            "considered_unlocked_items": ["long-handed-inserter", "speed-module"],
                            "uses_unlocked_items": ["long-handed-inserter"],
                            "unused_unlocked_items": ["speed-module"],
                            "build_item_supply": {
                                "status": "fail",
                                "missing": {"long-handed-inserter": 7, "assembling-machine-1": 5},
                                "used_unlocked_item_supply": {
                                    "long-handed-inserter": {
                                        "required": 7,
                                        "available": 0,
                                        "missing": 7,
                                        "sufficient": False,
                                    }
                                },
                                "summary": "missing build items: long-handed-inserter x7, assembling-machine-1 x5",
                            },
                            "layout_unlocks_considered": {
                                "long_handed_inserter": {"available": True, "researched": True, "stock": 0},
                                "modules": {"speed-module": {"available": True, "researched": False, "stock": 1}},
                            },
                            "before_blueprint": {
                                "label": "before-green-circuit",
                                "format": "factorio-blueprint-string",
                                "entity_count": 1,
                                "exchange_string": "0BEFOREBLUEPRINTSTRING",
                            },
                            "after_blueprint": {
                                "label": "green-circuit-3-cable-2-circuit-cell",
                                "format": "factorio-blueprint-string",
                                "entity_count": 2,
                                "exchange_string": "0SECRETBLUEPRINTSTRING",
                            },
                            "blueprint": {
                                "label": "green-circuit-3-cable-2-circuit-cell",
                                "format": "factorio-blueprint-string",
                                "entity_count": 2,
                                "exchange_string": "0SECRETBLUEPRINTSTRING",
                            },
                            "validation": {
                                "status": "pass",
                                "checked_machines": 5,
                                "errors": [],
                                "warnings": [],
                            },
                            "sandbox_validation": {
                                "status": "fail",
                                "reasons": ["expected output electronic-circuit was not observed after sandbox ticks"],
                                "observed_outputs": {"electronic-circuit": 0},
                                "ticks": 3600,
                                "checked_machines": 5,
                                "powered_machines": 5,
                            },
                            "site_prebuild_gate": {
                                "status": "fail",
                                "build_ready": False,
                                "summary": "sandbox-proven layout still needs site-specific build checks",
                                "errors": ["missing build items: assembling-machine-1 x5"],
                                "warnings": [],
                                "checks": {
                                    "build_items": {
                                        "status": "fail",
                                        "summary": "missing build items: assembling-machine-1 x5",
                                    }
                                },
                            },
                            "site_placement_search": {
                                "status": "found",
                                "summary": "found candidate build anchor at 10.0,8.0",
                                "selected_anchor": {"x": 10.0, "y": 8.0},
                                "evaluated_anchors": 113,
                                "top_candidates": [
                                    {
                                        "anchor": {"x": 10.0, "y": 8.0},
                                        "placement_ready": True,
                                        "failed_checks": ["build_items"],
                                    }
                                ],
                            },
                            "simulation": {
                                "score": 88,
                                "before": {"electronic_circuit_per_minute": 10},
                                "after": {"electronic_circuit_per_minute": 60},
                                "delta": {"electronic_circuit_per_minute": 50},
                            },
                        }
                    ],
                },
                "layout_background": {
                    "entries": [
                        {
                            "time": "2026-06-13T00:03:00+00:00",
                            "event": "layout_result",
                            "active_skill": "codex_wait:bootstrap_build_item_mall",
                            "result": {
                                "source": "llm",
                                "score": 91,
                                "selected_candidate_id": "compact-green-circuit-cell",
                                "reasoning": "Reduce footprint before building more cells.",
                            },
                        }
                    ],
                    "entry_count": 1,
                },
                "llm_decisions": {
                    "entries": [
                        {
                            "timestamp": "2026-06-13T00:02:00+00:00",
                            "objective": "launch_rocket_program",
                            "provider": "local_llm",
                            "source": "heuristic",
                            "ok": False,
                            "selected_skill": "research_automation",
                            "priority": 90,
                            "reason": "LLM unavailable; fallback selected research",
                            "blockers": ["automation research"],
                            "expected_effect": "feed labs",
                            "request_summary": {"tick": 1},
                            "error": "LLM unavailable or invalid response; used heuristic fallback",
                            "latency_ms": 12,
                        }
                    ],
                    "entry_count": 1,
                },
                "strategy_worker_comparison": {
                    "latest": {
                        "created_at": "2026-06-13T00:03:00+00:00",
                        "workers": [
                            {
                                "label": "4b",
                                "model": "Qwen/Qwen3.5-4B",
                                "llm_ready": True,
                                "source": "llm",
                                "selected_skill": "expand_copper_smelting",
                                "priority": 50,
                                "reason": "",
                                "latency_ms": 23,
                            },
                            {
                                "label": "27b",
                                "model": "Qwen/Qwen3.6-27B-FP8",
                                "llm_ready": False,
                                "error": "LLM endpoint",
                                "latency_ms": 3,
                            },
                        ],
                    },
                    "entry_count": 1,
                },
                "token_usage": {
                    "samples": [
                        {
                            "timestamp": "2026-06-13T00:00:00+00:00",
                            "tokens_used": 1000,
                            "delta_tokens": 0,
                            "label": "start",
                            "source": "codex",
                        },
                        {
                            "timestamp": "2026-06-13T00:01:00+00:00",
                            "tokens_used": 1250,
                            "delta_tokens": 250,
                            "label": "ui work",
                            "source": "codex",
                        },
                    ],
                    "sample_count": 2,
                    "latest_tokens": 1250,
                    "total_delta_tokens": 250,
                    "updated_at": "2026-06-13T00:01:00+00:00",
                },
                "trace_archives": {
                    "archive_root": "runtime/trace_archives",
                    "archive_count": 1,
                    "latest": {
                        "label": "part75-scattered-map-traces",
                        "source_count": 12,
                        "high_value_files": 8,
                    },
                    "archives": [
                        {
                            "created_at": "2026-06-15T00:20:00+00:00",
                            "label": "part75-scattered-map-traces",
                            "source_count": 12,
                            "high_value_files": 8,
                            "category_counts": {
                                "layout_background": 1,
                                "layout_validation": 1,
                                "strategy_run": 4,
                            },
                            "archive_dir": "runtime/trace_archives/20260615-002000-part75-scattered-map-traces",
                        }
                    ],
                },
            },
            lang="ko",
        )
        self.assertIn("팩토리오 공장 모니터링", html)
        self.assertIn('href="/factorio?objective=launch_rocket_program"', html)
        self.assertIn('href="/factorio?lang=ko&amp;objective=launch_rocket_program"', html)
        self.assertIn("/factorio/icon/iron-plate.png", html)
        self.assertIn("build_item_mall", html)
        self.assertIn("smelting:9,0", html)
        self.assertIn("site-logistics-row", html)
        self.assertIn("site-logistics-link", html)
        self.assertIn('name="action" value="select_improvement_site"', html)
        self.assertIn('name="site_id" value="build_item_mall:2,2"', html)
        self.assertNotIn("<h2>Logistics Links</h2>", html)
        self.assertIn("copper-cable assembler ratio", html)
        self.assertIn("r1jae", html)
        self.assertIn("Threats / Defense", html)
        self.assertIn("small-biter", html)
        self.assertIn("Recent Damage", html)
        self.assertIn("stone-furnace", html)
        self.assertIn("no-mod-rcon-lua", html)
        self.assertLess(html.index("희망 생산량"), html.index("Threats / Defense"))
        self.assertLess(html.index("추정 생산량"), html.index("Threats / Defense"))
        self.assertIn("전력망", html)
        self.assertLess(html.index("Throughput Constraints"), html.index("전력망"))
        self.assertLess(html.index("전력망"), html.index("Threats / Defense"))
        self.assertIn("Codex 토큰 사용량", html)
        self.assertIn("token-chart", html)
        self.assertIn("1,250", html)
        self.assertIn("2026-06-13 09:01:00 KST", html)
        self.assertIn("LLM 판단 로그", html)
        self.assertIn("local_llm", html)
        self.assertIn("LLM unavailable", html)
        self.assertIn("2026-06-13 09:02:00 KST", html)
        self.assertIn("LLM Worker Comparison", html)
        self.assertIn("Qwen/Qwen3.5-4B", html)
        self.assertIn("Qwen/Qwen3.6-27B-FP8", html)
        self.assertIn("LLM endpoint", html)
        self.assertIn("AI 동작 위치", html)
        self.assertIn("copper-ore", html)
        self.assertIn("virtual", html)
        self.assertIn("agent-map", html)
        self.assertIn("green-circuit-3-cable-2-circuit-cell", html)
        self.assertIn("Unlock-aware", html)
        self.assertIn("considered=long-handed-inserter, speed-module", html)
        self.assertIn("used=long-handed-inserter", html)
        self.assertIn("not_used=speed-module", html)
        self.assertIn("Build items", html)
        self.assertIn("unlocked_tool_shortage=long-handed-inserter x7", html)
        self.assertIn("copy-blueprint", html)
        self.assertIn("layout-candidate-grid", html)
        self.assertIn("layout-candidate-card", html)
        self.assertIn("layout-validation-pass", html)
        self.assertIn("layout-validation-fail", html)
        self.assertIn("expected output electronic-circuit", html)
        self.assertIn("sandbox-proven layout still needs site-specific build checks", html)
        self.assertIn("missing build items", html)
        self.assertIn("found candidate build anchor", html)
        self.assertIn("evaluated=113", html)
        self.assertIn("powered=5", html)
        self.assertIn("machines=5", html)
        self.assertIn("manual-copy-overlay", html)
        self.assertIn(FACTORIO_BLUEPRINT_ROUTE, html)
        self.assertIn('data-site-id="build_item_mall:2,2"', html)
        self.assertIn('data-candidate-id="green-circuit-3-cable-2-circuit-cell"', html)
        self.assertIn('data-variant="before"', html)
        self.assertIn('data-variant="after"', html)
        self.assertIn("개선 전 복사", html)
        self.assertIn("개선 후 복사", html)
        self.assertNotIn("0SECRETBLUEPRINTSTRING", html)
        self.assertNotIn("0BEFOREBLUEPRINTSTRING", html)
        self.assertNotIn("0SITEBLUEPRINTSTRING", html)
        self.assertIn("rebalance_green_circuit_ratio", html)
        self.assertIn("3 copper-cable assemblers", html)
        self.assertIn("codex_wait:bootstrap_build_item_mall", html)
        self.assertIn("compact-green-circuit-cell", html)
        self.assertIn("Reduce footprint", html)
        self.assertIn("Training Trace Archives", html)
        self.assertIn("part75-scattered-map-traces", html)
        self.assertIn("layout_background", html)
        self.assertIn("runtime/trace_archives/20260615-002000-part75-scattered-map-traces", html)

    def test_dashboard_marks_selected_improvement_site(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "test",
                "selected_improvement_site": {"site_id": "build_item_mall:2,2"},
                "monitor": {
                    "factory_sites": [
                        {
                            "kind": "build_item_mall",
                            "item": "transport-belt",
                            "status": "running",
                            "position": {"x": 2, "y": 2},
                            "automation_level": "powered",
                            "machines": ["assembling-machine-1"],
                            "site_id": "build_item_mall:2,2",
                        }
                    ],
                    "logistics_links": [],
                },
            },
            lang="en",
        )

        self.assertIn("site-selected-badge", html)
        self.assertIn("site-selected-row", html)
        self.assertIn("Selected improvement target", html)
        self.assertIn("Selected", html)
        self.assertIn('name="action" value="clear_improvement_site"', html)
        self.assertIn("site-improvement-cancel-button", html)
        self.assertIn(">Cancel<", html)
        self.assertNotIn('name="site_id" value="build_item_mall:2,2"', html)

    def test_dashboard_post_selects_improvement_site_without_saving_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(runtime_dir=Path(temp_dir), log_dir=Path(temp_dir) / "logs")
            _handle_dashboard_post_values(
                cfg,
                "launch_rocket_program",
                {
                    "action": ["select_improvement_site"],
                    "site_id": ["build_item_mall:2,2"],
                    "site_kind": ["build_item_mall"],
                    "site_item": ["transport-belt"],
                    "site_position_x": ["2"],
                    "site_position_y": ["2"],
                },
            )

            selected = load_selected_improvement_site(cfg.runtime_dir, "launch_rocket_program")
            self.assertEqual(selected["site_id"], "build_item_mall:2,2")
            self.assertFalse((cfg.runtime_dir / "production-targets.json").exists())

    def test_dashboard_post_clears_improvement_site_without_saving_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(runtime_dir=Path(temp_dir), log_dir=Path(temp_dir) / "logs")
            save_selected_improvement_site(
                cfg.runtime_dir,
                "launch_rocket_program",
                {"site_id": "build_item_mall:2,2", "kind": "build_item_mall"},
                selected_at="2026-06-14T00:00:00+00:00",
            )

            _handle_dashboard_post_values(
                cfg,
                "launch_rocket_program",
                {
                    "action": ["clear_improvement_site"],
                },
            )

            self.assertEqual(load_selected_improvement_site(cfg.runtime_dir, "launch_rocket_program"), {})
            self.assertFalse((cfg.runtime_dir / "production-targets.json").exists())

    def test_dashboard_post_saves_layout_llm_settings_without_saving_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(runtime_dir=Path(temp_dir), log_dir=Path(temp_dir) / "logs")
            _handle_dashboard_post_values(
                cfg,
                "launch_rocket_program",
                {
                    "action": ["save_layout_llm_settings"],
                    "max_active_layout_tasks": ["3"],
                },
            )

            settings = (cfg.runtime_dir / "layout-llm-settings.json").read_text(encoding="utf-8")
            self.assertIn('"max_active_layout_tasks": 3', settings)
            self.assertFalse((cfg.runtime_dir / "production-targets.json").exists())

    def test_dashboard_state_feeds_selected_site_into_layout_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(runtime_dir=Path(temp_dir), log_dir=Path(temp_dir) / "logs")
            save_selected_improvement_site(
                cfg.runtime_dir,
                "launch_rocket_program",
                {"site_id": "build_item_mall:2,2", "kind": "build_item_mall", "item": "transport-belt"},
                selected_at="2026-06-14T00:00:00+00:00",
            )
            with patch(
                "factorio_ai.web_dashboard.observe_dashboard_state",
                return_value=({"inventory": {}, "entities": [], "research": {"technologies": {}}}, "test"),
            ):
                state = build_dashboard_state(cfg, "launch_rocket_program")

        self.assertTrue(state["ok"])
        self.assertEqual(state["selected_improvement_site"]["site_id"], "build_item_mall:2,2")
        self.assertEqual(
            state["layout_improvement"]["selected_improvement_site"]["site_id"],
            "build_item_mall:2,2",
        )
        self.assertEqual(state["layout_improvement"]["opportunities"][0]["kind"], "operator_selected_site")

    def test_factory_site_logistics_match_position_aliases(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "test",
                "monitor": {
                    "factory_sites": [
                        {
                            "kind": "plate_smelting_line",
                            "item": "iron-plate",
                            "status": "running",
                            "position": {"x": 9, "y": 0},
                            "automation_level": "burner-bootstrap",
                            "machines": ["stone-furnace"],
                            "site_id": "plate_smelting_line:group:iron-plate:9.0,0.0",
                        }
                    ],
                    "logistics_links": [
                        {
                            "kind": "belt",
                            "item": "iron-ore",
                            "from_site": "mining_patch:4,0",
                            "to_site": "smelting:9,0",
                            "status": "route_observed",
                            "length_tiles": 5,
                        }
                    ],
                },
            },
            lang="en",
        )

        self.assertIn("site-logistics-row", html)
        self.assertIn("smelting:9,0", html)
        self.assertNotIn('<div class="site-logistics-unmatched">', html)

    def test_candidate_blueprint_response_returns_copy_payload(self):
        response = _candidate_blueprint_response(
            {
                "layout_improvement": {
                    "simulation_candidates": [
                        {
                            "candidate_id": "green-circuit-3-cable-2-circuit-cell",
                            "blueprint": {
                                "label": "green-circuit-3-cable-2-circuit-cell",
                                "format": "factorio-blueprint-string",
                                "entity_count": 2,
                                "exchange_string": "0SECRETBLUEPRINTSTRING",
                            },
                            "before_blueprint": {
                                "label": "before-green-circuit",
                                "format": "factorio-blueprint-string",
                                "entity_count": 1,
                                "exchange_string": "0BEFOREBLUEPRINTSTRING",
                            },
                        }
                    ]
                }
            },
            "green-circuit-3-cable-2-circuit-cell",
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["format"], "factorio-blueprint-string")
        self.assertEqual(response["entity_count"], 2)
        self.assertEqual(response["blueprint"], "0SECRETBLUEPRINTSTRING")

        before = _candidate_blueprint_response(
            {
                "layout_improvement": {
                    "simulation_candidates": [
                        {
                            "candidate_id": "green-circuit-3-cable-2-circuit-cell",
                            "blueprint": {
                                "label": "green-circuit-3-cable-2-circuit-cell",
                                "format": "factorio-blueprint-string",
                                "entity_count": 2,
                                "exchange_string": "0SECRETBLUEPRINTSTRING",
                            },
                            "before_blueprint": {
                                "label": "before-green-circuit",
                                "format": "factorio-blueprint-string",
                                "entity_count": 1,
                                "exchange_string": "0BEFOREBLUEPRINTSTRING",
                            },
                        }
                    ]
                }
            },
            "green-circuit-3-cable-2-circuit-cell",
            variant="before",
        )
        self.assertTrue(before["ok"])
        self.assertEqual(before["variant"], "before")
        self.assertEqual(before["entity_count"], 1)
        self.assertEqual(before["blueprint"], "0BEFOREBLUEPRINTSTRING")

    def test_site_blueprint_response_returns_copy_payload(self):
        response = _site_blueprint_response(
            {
                "monitor": {
                    "factory_sites": [
                        {
                            "site_id": "build_item_mall:2,2",
                            "blueprint": {
                                "label": "build item mall",
                                "format": "factorio-blueprint-string",
                                "entity_count": 1,
                                "exchange_string": "0SITEBLUEPRINTSTRING",
                            },
                        }
                    ]
                }
            },
            "build_item_mall:2,2",
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["format"], "factorio-blueprint-string")
        self.assertEqual(response["entity_count"], 1)
        self.assertEqual(response["blueprint"], "0SITEBLUEPRINTSTRING")

    def test_token_usage_chart_uses_timestamp_spacing(self):
        svg = _token_usage_svg(
            [
                {"timestamp": "2026-06-13T00:00:00+00:00", "tokens_used": 100},
                {"timestamp": "2026-06-13T00:01:00+00:00", "tokens_used": 150},
                {"timestamp": "2026-06-13T01:00:00+00:00", "tokens_used": 200},
            ]
        )
        self.assertIn('cx="53.7"', svg)
        self.assertIn('cx="744.0"', svg)
        self.assertIn("06-13 09:00", svg)
        self.assertIn("06-13 10:00", svg)

    def test_token_usage_chart_uses_cumulative_tokens_after_counter_reset(self):
        svg = _token_usage_svg(
            [
                {
                    "timestamp": "2026-06-13T00:00:00+00:00",
                    "tokens_used": 1000,
                    "cumulative_tokens": 1000,
                },
                {
                    "timestamp": "2026-06-13T00:01:00+00:00",
                    "tokens_used": 1250,
                    "cumulative_tokens": 1250,
                },
                {
                    "timestamp": "2026-06-13T00:02:00+00:00",
                    "tokens_used": 100,
                    "cumulative_tokens": 1350,
                    "counter_reset": True,
                },
            ]
        )

        self.assertIn(">1,000<", svg)
        self.assertIn(">1,350<", svg)
        self.assertNotIn(">100<", svg)

    def test_token_usage_table_includes_hourly_rate(self):
        html = _token_usage_table(
            [
                {
                    "timestamp": "2026-06-13T00:00:00+00:00",
                    "tokens_used": 1000,
                    "delta_tokens": 0,
                    "label": "start",
                    "weekly_percent": None,
                },
                {
                    "timestamp": "2026-06-13T00:30:00+00:00",
                    "tokens_used": 1250,
                    "delta_tokens": 250,
                    "label": "ui work",
                    "weekly_percent": 2.5,
                },
            ],
            "en",
        )

        self.assertIn("Tokens / hour", html)
        self.assertIn(">500<", html)
        self.assertIn("Weekly %", html)
        self.assertIn("2.5000%", html)

    def test_token_usage_panel_describes_codex_thread_counter_basis(self):
        html = _token_usage_panel(
            {
                "samples": [
                    {
                        "timestamp": "2026-06-13T00:00:00+00:00",
                        "tokens_used": 548_238_295,
                        "delta_tokens": 1_234_567,
                        "label": "sample",
                    }
                ],
                "latest_tokens": 548_238_295,
                "total_delta_tokens": 839_633,
                "latest_delta_tokens": 1_234_567,
                "weekly_quota_tokens": 2_000_000_000,
                "sample_count": 1,
            },
            "ko",
        )

        self.assertIn("현재 Factorio Codex thread", html)
        self.assertIn("threads.tokens_used", html)
        self.assertIn("548.2M", html)
        self.assertIn("1.2M", html)
        self.assertIn("2.0B", html)
        self.assertIn("839,633", html)

    def test_token_usage_table_uses_cumulative_tokens_after_counter_reset(self):
        html = _token_usage_table(
            [
                {
                    "timestamp": "2026-06-13T00:00:00+00:00",
                    "tokens_used": 1250,
                    "cumulative_tokens": 1250,
                    "delta_tokens": 250,
                    "label": "before reset",
                    "weekly_percent": None,
                },
                {
                    "timestamp": "2026-06-13T00:30:00+00:00",
                    "tokens_used": 100,
                    "cumulative_tokens": 1350,
                    "delta_tokens": 100,
                    "label": "after reset",
                    "weekly_percent": None,
                },
            ],
            "en",
        )

        self.assertIn(">1,350<", html)
        self.assertIn("after reset</td><td>100</td><td>unknown</td><td>200</td><td>1,350</td>", html)

    def test_dashboard_renders_goal_notes_and_insights(self):
        html = render_dashboard(
            {
                "ok": False,
                "objective": "launch_rocket_program",
                "updated_at": "2026-06-13T00:00:00+00:00",
                "error": "offline",
                "layout_background": {"entries": []},
                "token_usage": {
                    "samples": [],
                    "sample_count": 0,
                    "latest_tokens": 0,
                    "total_delta_tokens": 0,
                    "latest_delta_tokens": 0,
                    "weekly_quota_tokens": None,
                    "latest_weekly_percent": None,
                },
                "llm_decisions": {"entries": []},
                "strategy_worker_comparison": {"latest": {}},
                "run_journal": {
                    "goal": {
                        "exists": True,
                        "title": "Factorio Autoplayer Goal",
                        "summary": ["Launch the first rocket."],
                        "path": "goal.md",
                    },
                    "notes": [
                        {
                            "timestamp": "2026-06-13T00:00:00+00:00",
                            "loop_type": "skill",
                            "goal": "research_logistics",
                            "ok": True,
                            "steps": 3,
                            "reason": "done",
                            "log_path": "logs/skill.jsonl",
                        }
                    ],
                    "insights": [
                        {
                            "timestamp": "2026-06-13T00:01:00+00:00",
                            "kind": "skill_completed",
                            "goal": "research_logistics",
                            "detail": "research progressed",
                            "evidence": {"steps": 3},
                        }
                    ],
                },
            },
            "en",
        )
        self.assertIn("Goal Plan", html)
        self.assertIn("Recent Loop Notes", html)
        self.assertIn("Recent Insights", html)

    def test_dashboard_renders_strategy_factory_readiness(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "2026-06-13T00:00:00+00:00",
                "monitor": {},
                "targets": {},
                "layout_background": {"entries": []},
                "layout_llm_settings": {},
                "llm_decisions": {"entries": []},
                "strategy_worker_comparison": {"latest": {}},
                "run_journal": {},
                "trace_archives": {},
                "token_usage": {"samples": []},
                "strategy": {
                    "selected_skill": "build_gear_belt_mall_logistics",
                    "priority": 92,
                    "reason": "repair belt output",
                    "blockers": ["transport-belt mall bootstrap"],
                    "factory_readiness": {
                        "gear_mall_exists": True,
                        "belt_mall_can_output": False,
                        "iron_plate_source_ready": True,
                        "belt_line_buildable": False,
                        "boiler_feed_buildable": False,
                        "bootstrap_seed_allowed": True,
                        "failure_root": "belt_line_unbuildable",
                        "repair_skill": "build_gear_belt_mall_logistics",
                        "blocked_by": ["construction transport belts"],
                        "seed_reason": "virtual seed",
                        "expected_followup": "belt output increases",
                    },
                },
            },
            "en",
        )

        self.assertIn("Factory readiness", html)
        self.assertIn("failure_root=belt_line_unbuildable", html)
        self.assertIn("construction transport belts", html)

    def test_connection_refused_error_is_rendered_as_operator_guidance(self):
        message = friendly_dashboard_error(ConnectionRefusedError(10061, "actively refused"))
        self.assertIn("Factorio RCON server is not running", message)

    def test_dashboard_urls_use_lan_hosts_for_wildcard_bind(self):
        urls = dashboard_urls("0.0.0.0", 18889, "/factorio", base_url="http://10.0.0.5:18889")
        self.assertEqual(urls, ["http://10.0.0.5:18889/factorio"])

    def test_dashboard_state_cache_reuses_recent_state(self):
        cfg = SimpleNamespace(runtime_dir=Path("runtime"), log_dir=Path("logs"))
        clear_dashboard_state_cache()
        with (
            patch.dict(os.environ, {"FACTORIO_AI_WEB_CACHE_SECONDS": "60"}),
            patch(
                "factorio_ai.web_dashboard.build_dashboard_state",
                side_effect=[
                    {"ok": True, "updated_at": "first"},
                    {"ok": True, "updated_at": "second"},
                ],
            ) as build_state,
        ):
            first = build_dashboard_state_cached(cfg, "launch_rocket_program")
            second = build_dashboard_state_cached(cfg, "launch_rocket_program")
            refreshed = build_dashboard_state_cached(cfg, "launch_rocket_program", force_refresh=True)

        self.assertEqual(build_state.call_count, 2)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["updated_at"], "first")
        self.assertFalse(refreshed["cache"]["hit"])
        self.assertEqual(refreshed["updated_at"], "second")

    def test_no_mod_dashboard_observe_uses_lightweight_planning_site_mode(self):
        cfg = SimpleNamespace(runtime_dir=Path("runtime"), log_dir=Path("logs"))

        with (
            patch("factorio_ai.web_dashboard.FactorioController") as factorio_controller,
            patch("factorio_ai.web_dashboard.ModlessLuaController") as modless_controller,
        ):
            factorio_controller.return_value.observe.side_effect = RuntimeError("custom mod unavailable")
            modless_controller.return_value.observe.return_value = {"ok": True, "tick": 1}

            observation, adapter = observe_dashboard_state(cfg)

        self.assertEqual(observation["tick"], 1)
        self.assertEqual(adapter, "no-mod-rcon-lua")
        modless_controller.return_value.observe.assert_called_once_with(include_planning_sites=False)

    def test_dashboard_refresh_interval_is_configurable(self):
        with patch.dict(os.environ, {"FACTORIO_AI_WEB_REFRESH_SECONDS": "20"}):
            html = render_dashboard({"ok": False, "objective": "launch_rocket_program", "error": "offline"})

        self.assertIn('http-equiv="refresh" content="20"', html)

    def test_dashboard_renders_world_map_memory_panel(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "no-mod-rcon-lua",
                "targets": {"per_minute": {}},
                "monitor": {},
                "strategy": {},
                "world_map_memory": {
                    "schema_version": 1,
                    "encoding": "sparse_feature_graph",
                    "updated_at": "2026-06-14T17:34:17+00:00",
                    "updated_age_seconds": 1.0,
                    "candidate_counts": {"power_sites": 1, "lab_sites": 0, "automation_sites": 0},
                    "known_water_sites": [{"position": {"x": 55.5, "y": -814.5}, "direction": 0, "distance": 20}],
                    "resources": {
                        "patches": [
                            {
                                "name": "iron-ore",
                                "center": {"x": 10, "y": 11},
                                "sample_count": 2,
                                "total_amount": 1200,
                            }
                        ]
                    },
                    "factory": {
                        "zones": [
                            {
                                "id": "factory_zone:1",
                                "center": {"x": 1, "y": 2},
                                "entity_count": 2,
                                "entity_counts": {"stone-furnace": 1},
                            }
                        ]
                    },
                    "spatial_index": {"feature_count": 3, "cell_count": 2, "cell_size": 64},
                },
            }
        )

        self.assertIn("World Map Memory", html)
        self.assertIn("sparse_feature_graph", html)
        self.assertIn("Known water anchors", html)
        self.assertIn("Resource patches", html)
        self.assertIn("Factory zones", html)


class GeneratedSkillsPanelTests(unittest.TestCase):
    def test_panel_empty_state(self):
        html = _generated_skills_panel({}, "en")
        self.assertIn("Generated Skills (self-developed)", html)
        self.assertIn("has not registered any self-developed", html)

    def test_panel_renders_registered_queue_and_failures(self):
        html = _generated_skills_panel(
            {
                "registered": [
                    {
                        "skill_name": "stockpile_wood",
                        "class_name": "StockpileWoodSkill",
                        "version": 2,
                        "gates_passed": ["static_safety", "offline_replay", "sandbox_dryrun"],
                        "target_item": "wood",
                        "updated_at": "2026-06-17T00:00:00+00:00",
                    }
                ],
                "failures": [
                    {
                        "skill_name": "build_rail_supply_line",
                        "status": "quarantined",
                        "attempts": 4,
                        "last_failure_reason": "offline replay: invalid action",
                    }
                ],
                "queue": [
                    {"skill_name": "plan_rail_network", "priority": 70, "reason": "far resource patch"}
                ],
                "heartbeat": {
                    "state": "generating",
                    "current_skill": "plan_rail_network",
                    "generated_total": 1,
                    "failed_total": 1,
                    "updated_at": "2026-06-17T00:01:00+00:00",
                },
            },
            "en",
        )
        self.assertIn("stockpile_wood", html)
        self.assertIn("StockpileWoodSkill", html)
        self.assertIn("sandbox_dryrun", html)
        self.assertIn("plan_rail_network", html)
        self.assertIn("build_rail_supply_line", html)
        self.assertIn("quarantined", html)
        self.assertIn("generating", html)

    def test_dashboard_includes_generated_skills_panel_in_korean(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "no-mod-rcon-lua",
                "monitor": {},
                "strategy": {},
                "generated_skills": {
                    "registered": [],
                    "failures": [],
                    "queue": [{"skill_name": "plan_rail_network", "priority": 50}],
                    "heartbeat": {"state": "idle", "reason": "queue empty"},
                },
            },
            lang="ko",
        )
        self.assertIn("생성된 스킬 (자가 개발)", html)
        self.assertIn("plan_rail_network", html)

    def test_generated_skills_summary_reads_registry_queue_and_heartbeat(self):
        from factorio_ai import skill_foundry

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            gen_dir = runtime / "generated_skills"
            gen_dir.mkdir(parents=True, exist_ok=True)
            prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(gen_dir)
            try:
                skill_foundry.update_skill(
                    "stockpile_wood",
                    status="registered",
                    class_name="StockpileWoodSkill",
                    version=1,
                    gates_passed=["static_safety", "offline_replay"],
                    target_item="wood",
                )
                skill_foundry.update_skill(
                    "build_rail_supply_line",
                    status="quarantined",
                    last_failure_reason="repeated live failures",
                    attempts=3,
                )
                skill_foundry.enqueue_foundry_request(
                    runtime, "plan_rail_network", reason="far patch", priority=70
                )
                (runtime / "skill-foundry-loop.json").write_text(
                    '{"state": "sleeping", "updated_at": "2026-06-17T00:00:00+00:00"}',
                    encoding="utf-8",
                )
                cfg = SimpleNamespace(runtime_dir=runtime, log_dir=runtime / "logs")
                summary = generated_skills_summary(cfg)
            finally:
                if prev is None:
                    os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
                else:
                    os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = prev

        self.assertEqual(summary["registered_count"], 1)
        self.assertEqual(summary["registered"][0]["skill_name"], "stockpile_wood")
        self.assertEqual(summary["queue_count"], 1)
        self.assertEqual(summary["queue"][0]["skill_name"], "plan_rail_network")
        self.assertEqual(summary["failures"][0]["skill_name"], "build_rail_supply_line")
        self.assertEqual(summary["heartbeat"]["state"], "sleeping")


class DependencyTreePersistenceTests(unittest.TestCase):
    def test_tree_nodes_have_stable_ids_and_persist_script_present(self):
        from factorio_ai.web_dashboard import _dependency_tree_html, _details_persist_script

        forest = [
            {
                "item": "rocket-silo",
                "technology": "rocket-silo",
                "ingredients": [
                    {
                        "item": "steel-plate",
                        "amount": 1000,
                        "dependency": {
                            "item": "steel-plate",
                            "ingredients": [
                                {"item": "iron-plate", "amount": 5, "dependency": {"item": "iron-plate", "ingredients": []}}
                            ],
                        },
                    }
                ],
            },
            {"item": "assembling-machine-2", "infrastructure": True, "ingredients": []},
        ]
        html = _dependency_tree_html(forest)
        self.assertIn('id="dep:rocket-silo"', html)
        self.assertIn('id="dep:rocket-silo/steel-plate"', html)
        self.assertIn('id="dep:__infra__"', html)
        script = _details_persist_script()
        self.assertIn("localStorage", script)
        self.assertIn("details[id]", script)
        self.assertIn("toggle", script)


if __name__ == "__main__":
    unittest.main()
