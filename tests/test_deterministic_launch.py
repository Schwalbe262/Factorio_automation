import re
import unittest

from factorio_ai.deterministic_launch import LAUNCH_LUA, OBSERVE_LAUNCH_LUA, LaunchStage
from factorio_ai.world_catalog import WorldCatalog
from tests.test_deterministic_production import production_fixture


def observation(parts=7, pack=1):
    return {"inventory": {"space-platform-starter-pack": pack}, "rockets_launched": 0,
            "entities": [{"name": "rocket-silo", "unit_number": 10, "position": {"x": 2, "y": 3},
                          "rocket_parts": parts, "inventory": {}}]}


class LaunchStageTests(unittest.TestCase):
    def setUp(self):
        self.stage = LaunchStage(WorldCatalog.from_dict(production_fixture()))

    def test_real_materials_and_live_part_requirement_gate_launch(self):
        result = self.stage.next_action(observation(parts=5))
        self.assertEqual(result["requirements"][0]["count"], 2)
        self.assertIsNone(result["action"])
        result = self.stage.next_action(observation(pack=0))
        self.assertEqual(result["reason"], "starter_pack_required")
        self.assertIsNone(result["action"])
        self.assertEqual(self.stage.next_action(observation())["action"],
                         {"type": "launch", "name": "rocket-silo", "position": {"x": 2, "y": 3}})

    def test_manufactured_starter_pack_is_collected_from_factory(self):
        state = observation(pack=0)
        state["entities"].append({"name": "assembling-machine-2", "position": {"x": 4, "y": 5},
                                  "inventory": {"space-platform-starter-pack": 1}})
        result = self.stage.next_action(state)
        self.assertEqual(result["action"]["type"], "take")
        self.assertEqual(result["action"]["count"], 1)

    def test_assembled_rocket_status_takes_precedence_over_consumed_parts(self):
        state = observation(parts=0)
        state["entities"][0]["rocket_silo_status"] = "rocket_ready"
        self.assertEqual(self.stage.next_action(state)["action"]["type"], "launch")

    def test_counter_alone_never_claims_success(self):
        state = observation()
        state["rockets_launched"] = 20
        self.assertNotEqual(self.stage.next_action(state)["status"], "succeeded")
        state["launch"] = {"ordered": True, "baseline": 19, "platform_valid": True,
                           "platform_hub_valid": False, "rockets_launched": 20}
        self.assertEqual(self.stage.next_action(state)["status"], "running")
        state["launch"]["platform_hub_valid"] = True
        self.assertEqual(self.stage.next_action(state)["status"], "succeeded")

    def test_lost_platform_blocks_instead_of_creating_second_launch(self):
        state = observation()
        state["launch"] = {"ordered": True, "platform_valid": False}
        result = self.stage.next_action(state)
        self.assertEqual(result["reason"], "launch_platform_lost")
        self.assertIsNone(result["action"])

    def test_launch_helpers_never_write_progress_or_apply_free_starter_pack(self):
        for body in (LAUNCH_LUA, OBSERVE_LAUNCH_LUA):
            self.assertIsNone(re.search(r"\.(?:rockets_launched|rocket_parts|researched|enabled|crafting_progress)\s*=(?!=)", body))
            self.assertNotIn("apply_starter_pack", body)
            self.assertNotIn("force_finish", body)


if __name__ == "__main__":
    unittest.main()
