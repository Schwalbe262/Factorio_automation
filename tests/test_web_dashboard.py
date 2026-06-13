import unittest

from factorio_ai.networking import dashboard_urls
from factorio_ai.web_dashboard import (
    FACTORIO_ROUTE,
    _token_usage_svg,
    dashboard_path,
    friendly_dashboard_error,
    is_factorio_route,
    render_dashboard,
    request_language,
)


class WebDashboardTests(unittest.TestCase):
    def test_factorio_routes(self):
        self.assertEqual(FACTORIO_ROUTE, "/factorio")
        self.assertTrue(is_factorio_route("/factorio"))
        self.assertTrue(is_factorio_route("/factorio/"))
        self.assertTrue(is_factorio_route("/팩토리오"))
        self.assertTrue(is_factorio_route("/%ED%8C%A9%ED%86%A0%EB%A6%AC%EC%98%A4"))

    def test_legacy_route_defaults_to_korean(self):
        self.assertEqual(request_language("/팩토리오", {}), "ko")
        self.assertEqual(dashboard_path("ko"), "/factorio?lang=ko")

    def test_dashboard_html_has_monitor_sections_and_item_icons(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "adapter": "no-mod-rcon-lua",
                "player": {"name": "server", "kind": "server", "position": {"x": 1, "y": 2}},
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
                        }
                    ],
                    "logistics_links": [
                        {
                            "kind": "belt",
                            "item": "iron-ore",
                            "from_site": "mining_patch:4,0",
                            "to_site": "smelting:9,0",
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
            },
            lang="ko",
        )
        self.assertIn("팩토리오 공장 모니터링", html)
        self.assertIn('href="/factorio?objective=launch_rocket_program"', html)
        self.assertIn('href="/factorio?lang=ko&amp;objective=launch_rocket_program"', html)
        self.assertIn("/factorio/icon/iron-plate.png", html)
        self.assertIn("build_item_mall", html)
        self.assertIn("smelting:9,0", html)
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
        self.assertIn("AI 동작 위치", html)
        self.assertIn("copper-ore", html)
        self.assertIn("agent-map", html)

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

    def test_connection_refused_error_is_rendered_as_operator_guidance(self):
        message = friendly_dashboard_error(ConnectionRefusedError(10061, "actively refused"))
        self.assertIn("Factorio RCON server is not running", message)

    def test_dashboard_urls_use_lan_hosts_for_wildcard_bind(self):
        urls = dashboard_urls("0.0.0.0", 18889, "/factorio", base_url="http://10.0.0.5:18889")
        self.assertEqual(urls, ["http://10.0.0.5:18889/factorio"])


if __name__ == "__main__":
    unittest.main()
