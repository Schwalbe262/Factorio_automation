import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_ai import cell_autodesign, cell_library
from factorio_ai.targets import ProductionTargets, save_targets


class CellAutodesignTests(unittest.TestCase):
    def _obs(self):
        return {"tick": 1, "entities": [], "inventory": {}, "enemies": [], "research": {"technologies": {}}}

    def test_designs_top_deficits_and_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            save_targets(runtime, ProductionTargets({"electronic-circuit": 60, "iron-plate": 120}))
            cfg = SimpleNamespace(runtime_dir=runtime)
            res = cell_autodesign.design_cells(cfg, self._obs(), top_n=3)
            self.assertTrue(res["ok"])
            self.assertEqual(res["count"], 2)
            items = {d["item"] for d in res["designed"]}
            self.assertEqual(items, {"electronic-circuit", "iron-plate"})
            # both stored in the library
            designs = cell_library.load_designs(runtime)
            self.assertEqual(len(designs), 2)
            self.assertTrue(all(d.get("mode") == "new" for d in res["designed"]))  # no existing sites

    def test_no_targets_is_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            save_targets(runtime, ProductionTargets({}))
            cfg = SimpleNamespace(runtime_dir=runtime)
            res = cell_autodesign.design_cells(cfg, self._obs())
            self.assertFalse(res["ok"])
            self.assertEqual(res["designed"], [])

    def test_top_n_limits_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            save_targets(runtime, ProductionTargets({"electronic-circuit": 60, "iron-plate": 120, "copper-plate": 90}))
            cfg = SimpleNamespace(runtime_dir=runtime)
            res = cell_autodesign.design_cells(cfg, self._obs(), top_n=1)
            self.assertEqual(res["count"], 1)
            # the largest deficit (iron-plate 120) is chosen first
            self.assertEqual(res["designed"][0]["item"], "iron-plate")


if __name__ == "__main__":
    unittest.main()
