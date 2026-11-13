from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from factorio_agent.normal_research import NormalResearch, ready_options


class EngineResearchTests(unittest.TestCase):
    def test_observation_exposes_ready_natural_trigger_without_offering_science_research(self):
        catalog = SimpleNamespace(
            technologies={
                "oil-processing": {
                    "enabled": True,
                    "prerequisites": ["oil-gathering"],
                    "ingredients": [],
                    "research_trigger": {"type": "mine-entity", "entities": ["crude-oil"]},
                    "unlocks": ["oil-refinery"],
                },
                "advanced-oil-processing": {
                    "enabled": True,
                    "prerequisites": ["chemical-science-pack"],
                    "ingredients": [{"name": "chemical-science-pack", "amount": 1}],
                },
            }
        )
        obs = {"technologies": {}}
        self.assertEqual(ready_options(catalog, obs)["natural_triggers"], [])
        obs["technologies"]["oil-gathering"] = True
        result = ready_options(catalog, obs)
        self.assertEqual(result["technologies"], [])
        self.assertEqual(result["natural_triggers"][0]["technology"], "oil-processing")
        self.assertEqual(result["natural_triggers"][0]["trigger"]["entities"], ["crude-oil"])
        result["natural_triggers"][0]["trigger"]["entities"].append("stone")
        self.assertEqual(catalog.technologies["oil-processing"]["research_trigger"]["entities"], ["crude-oil"])
        obs["technologies"]["oil-processing"] = True
        self.assertEqual(ready_options(catalog, obs)["natural_triggers"], [])

    def test_research_emits_no_pack_crafting_or_insertion_and_requires_actual_consumption(self):
        with TemporaryDirectory() as directory:
            obj = NormalResearch.__new__(NormalResearch)
            obj.path = Path(directory) / "research.json"
            obj.state = {}
            obj.catalog = SimpleNamespace(
                fingerprint="f",
                technologies={
                    "gun-turret": {
                        "prerequisites": [],
                        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                    }
                },
            )
            obj.science = SimpleNamespace(proof=lambda o: {"science_units": [1, 2, 3]})
            obs = {
                "world_id": "w",
                "tick": 10,
                "technologies": {},
                "research": None,
                "production": {"automation-science-pack": {"consumed": 55}},
            }
            self.assertEqual(obj.step(obs, {"technology": "gun-turret"})["type"], "research_queue")
            obs["technologies"]["gun-turret"] = True
            self.assertIsNone(obj.proof(obs, {"technology": "gun-turret"}))
            obs["production"]["automation-science-pack"]["consumed"] = 65
            self.assertEqual(
                obj.proof(obs, {"technology": "gun-turret"})["science_consumed_since_first_request"],
                {"automation-science-pack": 10},
            )

    def test_green_technology_requires_commissioned_machine_green_supply(self):
        with TemporaryDirectory() as directory:
            obj = NormalResearch.__new__(NormalResearch)
            obj.path = Path(directory) / "research.json"
            obj.state = {}
            obj.catalog = SimpleNamespace(
                fingerprint="f",
                technologies={
                    "advanced-material-processing": {
                        "prerequisites": [],
                        "ingredients": [
                            {"name": "automation-science-pack", "amount": 1},
                            {"name": "logistic-science-pack", "amount": 1},
                        ],
                    }
                },
            )
            obj.science = SimpleNamespace(proof=lambda o: {"science_units": [1]})
            obj.green = SimpleNamespace(proof=lambda o: None)
            obs = {
                "world_id": "w",
                "tick": 10,
                "technologies": {},
                "research": None,
                "production": {"automation-science-pack": {"consumed": 10}, "logistic-science-pack": {"consumed": 0}},
            }
            request = {"technology": "advanced-material-processing"}
            waiting = obj.step(obs, request)
            self.assertEqual(waiting["reason"], "waiting_for_science_pack_supply")
            # eval-4177156614: the ledger showed evidence {}; the wait now names the missing producer.
            self.assertEqual(
                waiting["evidence"],
                {
                    "technology": "advanced-material-processing",
                    "packs": ["logistic-science-pack"],
                    "producers": ["logistic_science"],
                },
            )
            obj.green = SimpleNamespace(proof=lambda o: {"science_units": [2]})
            self.assertEqual(obj.step(obs, request)["type"], "research_queue")

    def test_blue_research_requires_fresh_supply_and_records_actual_consumption(self):
        with TemporaryDirectory() as directory:
            obj = NormalResearch.__new__(NormalResearch)
            obj.path = Path(directory) / "research.json"
            obj.state = {}
            names = ("automation-science-pack", "logistic-science-pack", "chemical-science-pack")
            obj.catalog = SimpleNamespace(
                fingerprint="f",
                technologies={
                    "processing-unit": {
                        "prerequisites": [],
                        "ingredients": [{"name": name, "amount": 1} for name in names],
                    }
                },
            )
            obj.science = SimpleNamespace(proof=lambda o: {"science_units": [1]})
            obj.green = SimpleNamespace(proof=lambda o: {"science_units": [2]})
            obs = {
                "world_id": "w",
                "tick": 10,
                "technologies": {},
                "research": None,
                "production": {name: {"consumed": 10} for name in names},
            }
            request = {"technology": "processing-unit"}
            self.assertEqual(obj.step(obs, request)["packs"], ["chemical-science-pack"])
            obj.blue = SimpleNamespace(state={"phase": "verified"}, proof=lambda o: None)
            self.assertEqual(obj.step(obs, request)["status"], "waiting")
            self.assertFalse(obj.path.exists())
            obj.blue.proof = lambda o: {
                "science_units": [3, 4],
                "lab_units": list(range(20, 28)),
                "actual_blue_in_all_labs": True,
            }
            self.assertEqual(obj.step(obs, request)["type"], "research_queue")
            obs["technologies"]["processing-unit"] = True
            self.assertIsNone(obj.proof(obs, request))
            for name in names:
                obs["production"][name]["consumed"] = 11
            proof = obj.proof(obs, request)
            self.assertEqual(proof["blue_supply_units_at_start"], [3, 4])
            self.assertEqual(proof["science_consumed_since_first_request"], {name: 1 for name in names})

    def test_uncommissioned_blue_is_an_explicit_unsupported_tier(self):
        with TemporaryDirectory() as directory:
            obj = NormalResearch(
                SimpleNamespace(cfg=SimpleNamespace(runtime_dir=directory)),
                SimpleNamespace(
                    fingerprint="f",
                    technologies={
                        "next": {"prerequisites": [], "ingredients": [{"name": "chemical-science-pack", "amount": 1}]}
                    },
                ),
                None,
                blue=SimpleNamespace(state={}, proof=lambda o: None),
            )
            obj.path = Path(directory) / "research.json"
            result = obj.step({"world_id": "w", "technologies": {}}, {"technology": "next"})
            self.assertEqual(result["reason"], "waiting_for_science_pack_supply")
            self.assertEqual(result["packs"], ["chemical-science-pack"])


if __name__ == "__main__":
    unittest.main()
