import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factorio_agent.research import StarterResearch


class StartupResearchTests(unittest.TestCase):
    def test_reserved_science_does_not_look_exhausted_while_engine_queue_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "starter-research.json").write_text(
                json.dumps(
                    {
                        "world_id": "w",
                        "allowances": {"automation": {"budget": 10, "reserved_output": 10, "created_tick": 0}},
                    }
                ),
                encoding="utf-8",
            )
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            catalog = SimpleNamespace(
                technologies={
                    "automation": {
                        "prerequisites": ["automation-science-pack"],
                        "unit_count": 10,
                        "ingredients": [{"amount": 1, "name": "automation-science-pack", "type": "item"}],
                    }
                }
            )
            routine = StarterResearch(game, SimpleNamespace(), catalog)
            obs = {
                "world_id": "w",
                "tick": 100,
                "technologies": {"automation-science-pack": True},
                "research": "automation",
                "entities": [
                    {"name": "lab", "position": {"x": 0, "y": 0}, "electric_network_connected": True, "inventory": {}}
                ],
                "crafting_queue": [{"recipe": "automation-science-pack", "count": 10}],
                "inventory": {},
            }
            result = routine.step(obs, {"technology": "automation"})
            self.assertEqual(result["status"], "waiting")
            obs["crafting_queue"] = []
            obs["entities"][0]["status_name"] = "working"
            result = routine.step(obs, {"technology": "automation"})
            self.assertEqual(result["status"], "waiting")
            obs["entities"][0]["status_name"] = "no_research_in_progress"
            result = routine.step(obs, {"technology": "automation"})
            self.assertEqual(result["status"], "blocked")
            self.assertIn("allowance_exhausted", result["reason"])
            obs["inventory"] = {"automation-science-pack": 10}
            result = routine.step(obs, {"technology": "automation"})
            self.assertEqual((result["type"], result["count"]), ("insert", 10))

    def test_tiny_pack_inserts_fold_while_the_lab_has_packs_and_the_queue_crafts(self):
        # aud-1114-0930: 97 of 102 startup science inserts were 1-2 packs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            catalog = SimpleNamespace(
                technologies={
                    "automation": {
                        "prerequisites": ["automation-science-pack"],
                        "unit_count": 10,
                        "ingredients": [{"amount": 1, "name": "automation-science-pack", "type": "item"}],
                    }
                }
            )
            routine = StarterResearch(game, SimpleNamespace(), catalog)
            lab = {"name": "lab", "position": {"x": 0, "y": 0}, "electric_network_connected": True}
            obs = {
                "world_id": "w",
                "tick": 100,
                "technologies": {"automation-science-pack": True},
                "research": "automation",
                "entities": [dict(lab, inventory={"automation-science-pack": 1})],
                "crafting_queue": [{"recipe": "automation-science-pack", "count": 5}],
                "inventory": {"automation-science-pack": 1},
            }
            self.assertEqual(routine.step(obs, {"technology": "automation"})["status"], "waiting")
            obs["inventory"] = {"automation-science-pack": 3}
            self.assertEqual(routine.step(obs, {"technology": "automation"})["count"], 3)
            obs["inventory"] = {"automation-science-pack": 1}
            obs["entities"] = [dict(lab, inventory={})]  # the lab is out: the one pack goes in
            self.assertEqual(routine.step(obs, {"technology": "automation"})["count"], 1)
            obs["entities"] = [dict(lab, inventory={"automation-science-pack": 1})]
            obs["crafting_queue"] = []  # nothing more coming: insert what is held
            self.assertEqual(routine.step(obs, {"technology": "automation"})["count"], 1)


if __name__ == "__main__":
    unittest.main()
