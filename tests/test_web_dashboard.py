import unittest

from factorio_ai.networking import dashboard_urls
from factorio_ai.web_dashboard import (
    FACTORIO_ROUTE,
    dashboard_path,
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

    def test_dashboard_html_has_language_toggle_and_item_icons(self):
        html = render_dashboard(
            {
                "ok": True,
                "objective": "launch_rocket_program",
                "updated_at": "now",
                "observation_tick": 1,
                "targets": {"per_minute": {"iron-plate": 30.0}},
                "monitor": {
                    "production": [{"item": "iron-plate", "per_minute": 30.0, "producers": 1, "confidence": 0.5}],
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
                },
                "strategy": {"selected_skill": "produce_iron_plate", "priority": 95, "skill_status": {"implemented": True}},
            },
            lang="ko",
        )
        self.assertIn('href="/factorio?objective=launch_rocket_program"', html)
        self.assertIn('href="/factorio?lang=ko&amp;objective=launch_rocket_program"', html)
        self.assertIn('/factorio/icon/iron-plate.png', html)
        self.assertIn("build_item_mall", html)
        self.assertIn("smelting:9,0", html)
        self.assertIn("팩토리오 AI 공장 모니터", html)

    def test_dashboard_urls_use_lan_hosts_for_wildcard_bind(self):
        urls = dashboard_urls("0.0.0.0", 18889, "/factorio", base_url="http://10.0.0.5:18889")
        self.assertEqual(urls, ["http://10.0.0.5:18889/factorio"])


if __name__ == "__main__":
    unittest.main()
