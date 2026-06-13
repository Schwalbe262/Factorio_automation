import unittest

from factorio_ai.networking import dashboard_urls
from factorio_ai.web_dashboard import (
    FACTORIO_ROUTE,
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
            },
            lang="ko",
        )
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

    def test_connection_refused_error_is_rendered_as_operator_guidance(self):
        message = friendly_dashboard_error(ConnectionRefusedError(10061, "actively refused"))
        self.assertIn("Factorio RCON server is not running", message)

    def test_dashboard_urls_use_lan_hosts_for_wildcard_bind(self):
        urls = dashboard_urls("0.0.0.0", 18889, "/factorio", base_url="http://10.0.0.5:18889")
        self.assertEqual(urls, ["http://10.0.0.5:18889/factorio"])


if __name__ == "__main__":
    unittest.main()
