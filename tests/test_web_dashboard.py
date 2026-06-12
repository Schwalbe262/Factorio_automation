import unittest

from factorio_ai.web_dashboard import FACTORIO_ROUTE, is_factorio_route


class WebDashboardTests(unittest.TestCase):
    def test_factorio_korean_route(self):
        self.assertEqual(FACTORIO_ROUTE, "/팩토리오")
        self.assertTrue(is_factorio_route("/팩토리오"))
        self.assertTrue(is_factorio_route("/%ED%8C%A9%ED%86%A0%EB%A6%AC%EC%98%A4"))
        self.assertFalse(is_factorio_route("/factorio"))


if __name__ == "__main__":
    unittest.main()
