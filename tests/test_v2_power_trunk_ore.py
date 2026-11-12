"""The regularized coal trunk must go around ore: the builder refuses belts on ore tiles."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent.power_layout import RegularizePower, compact_coal_cell


class TrunkAvoidsOreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        drill = {"x": 0, "y": 0}
        self.cell = compact_coal_cell(drill)
        source = self.cell["ports"][0]["position"]
        self.destination = {
            "item": "coal",
            "direction": "input",
            "position": {"x": source["x"] + 40, "y": source["y"]},
            "facing": 4,
        }
        self.ore = []
        self.replies = []

        def query(script):
            return {"ok": True, "blocked": [], "clearable": [], "ore": list(self.ore)}

        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=query)
        state = {
            "power_plan": {"entities": [], "ports": [self.destination]},
            "coal_plan": {"entities": [], "drill": {"position": drill}},
        }
        self.builder = SimpleNamespace(
            _sync=lambda obs: None,
            state=state,
            _occupied_by_plan=lambda entities: {(e["position"]["x"], e["position"]["y"]) for e in entities},
        )
        self.obs = {"world_id": "w", "tick": 1, "entities": []}

    def plan_tiles(self):
        controller = RegularizePower(self.game, self.builder)
        result = controller.prepare(self.obs)
        self.assertTrue(result.get("ok"), result)
        return {(p["x"], p["y"]) for p in controller.state["new_plan"]["route_path"]}

    def test_route_detours_around_an_ore_patch_on_the_direct_line(self):
        direct = self.plan_tiles()
        source = self.cell["ports"][0]["position"]
        self.ore = [{"x": source["x"] + 20, "y": source["y"] + dy} for dy in range(-6, 7)]
        self.assertTrue({(p["x"], p["y"]) for p in self.ore} & direct)
        detour = self.plan_tiles()
        self.assertFalse({(p["x"], p["y"]) for p in self.ore} & detour)

    def test_ore_under_the_coal_cell_itself_is_allowed(self):
        self.ore = [dict(e["position"]) for e in self.cell["entities"] if e["name"] != "burner-mining-drill"]
        self.plan_tiles()


if __name__ == "__main__":
    unittest.main()
