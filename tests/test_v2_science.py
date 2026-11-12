import unittest
from types import SimpleNamespace
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.science import science_row, lab_row, validate_rate
from factorio_agent.jobs import StarterExecutors


class AutomaticScienceGeometryTests(unittest.TestCase):
    def test_six_red_machines_gear_cell_and_labs_have_no_self_overlap(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        for plan in (science_row(-39.5, -65.5, 6), lab_row(-46.5, -79.5)):
            boxes = [geometry._occupied_by_plan([e]) for e in plan["entities"]]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        science = science_row(-39.5, -65.5, 6)
        self.assertEqual(science["required_items"]["assembling-machine-1"], 7)
        self.assertEqual(sum(e["name"] == "inserter" and e["direction"] == 12 for e in science["entities"]), 2)

    def test_invalid_rate_cannot_reach_world(self):
        for rate in (True, 0, float("nan"), 31):
            with self.assertRaises(ValueError):
                validate_rate({"rate_per_minute": rate})

    def test_industrial_upkeep_does_not_rebuild_old_starter_cells(self):
        executor = StarterExecutors.__new__(StarterExecutors)
        executor.industrial_mode = True
        executor.smelting = SimpleNamespace(state={"world_id": "w"}, owns_furnace=lambda e, o: False)
        executor.builder = SimpleNamespace(owns_automated_burner=lambda e: False)
        executor.bootstrap = SimpleNamespace(
            maintain_fuel=lambda o: None,
            next_action=lambda o: (_ for _ in ()).throw(AssertionError("legacy cells must not be rebuilt")),
        )
        obs = {
            "world_id": "w",
            "entities": [],
            "technologies": {"steam-power": True, "electronics": True, "automation-science-pack": True},
        }
        self.assertEqual(executor.starter_upkeep(obs)["status"], "succeeded")
        obs["world_id"] = "changed"
        self.assertEqual(executor.starter_upkeep(obs)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
