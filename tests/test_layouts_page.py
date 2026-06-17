import tempfile
import unittest
from pathlib import Path

from factorio_ai import cell_pipeline, cell_compiler as cc, cell_library, web_dashboard as wd


class LayoutsPageTests(unittest.TestCase):
    def test_page_lists_designs_with_copy_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            cell_pipeline.build_and_store(
                runtime, "electronic-circuit", 60,
                available_machines=["assembling-machine-2"],
                power_situation=cc.PowerSituation(available_headroom_kw=2000),
            )
            summary = cell_library.library_summary(runtime)
            html = wd.render_layouts_page(summary, "en", "launch_rocket_program")
        self.assertIn("Layout Library", html)
        self.assertIn("electronic-circuit", html)
        self.assertIn("class=\"copy-blueprint\"", html)
        self.assertIn("data-blueprint=", html)  # blueprint embedded for client-side copy
        self.assertIn("465 kW", html)  # spec table rendered

    def test_empty_library_shows_placeholder(self):
        html = wd.render_layouts_page({"designs": [], "library_path": "x"}, "en")
        self.assertIn("No optimized cell layouts", html)

    def test_layouts_path_and_nav(self):
        self.assertEqual(wd.layouts_path("en"), "/factorio/layouts")
        nav = wd._language_switch("en", "launch_rocket_program")
        self.assertIn("/factorio/layouts", nav)


if __name__ == "__main__":
    unittest.main()
