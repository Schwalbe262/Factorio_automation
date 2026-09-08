from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_fuel_intake import validate_adjacent_fuel_intake
from tests import test_deterministic_fuel_intake as fixtures


class AdjacentFuelPoleTests(unittest.TestCase):
    reserve = fixtures.AdjacentFuelIntakeTests.reserve
    validate = fixtures.AdjacentFuelIntakeTests.validate

    def setUp(self):
        fixtures.AdjacentFuelIntakeTests.setUp(self)
        self.uncovered = {k: deepcopy(v) for k, v in self.row.items() if k not in {"pole", "pole_unit"}}
        self.proof.update(options=[], uncovered=[self.uncovered], new_pole_reach=2, tick=100)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory._intake_poles = Mock(return_value=[self.pole])

    def pending(self, **fields):
        self.proof["options"] = [{**self.row, "pole_unit": None, "powered": False, **fields}]
        self.proof["uncovered"] = []

    def test_new_pole_is_normally_reserved_without_inventing_identity_or_ownership(self):
        before = deepcopy(self.factory.state["links"][self.parent])
        self.assertTrue(self.reserve())
        plan = self.factory.state["blocks"][self.key]
        self.assertEqual(plan["entities"], [self.arm, self.pole])
        self.assertTrue(plan["adjacent_fuel_intake"]["new_pole"])
        self.assertNotIn("pole_unit", plan["adjacent_fuel_intake"])
        self.assertEqual(self.factory.state["links"][self.parent], before)
        self.assertEqual(self.factory.state["links"][self.key]["entities"], [self.belt])
        self.assertEqual(self.factory.state.get("automated_burners", []), [])
        self.builder.can_place.assert_called_once_with([self.arm, self.pole])

    def test_existing_powered_pole_is_preferred_without_trying_new_placements(self):
        self.proof["options"] = [self.row]
        self.assertTrue(self.reserve())
        self.assertNotIn("new_pole", self.factory.state["blocks"][self.key]["adjacent_fuel_intake"])
        self.builder.can_place.assert_not_called()
        self.factory._intake_poles.assert_not_called()

    def test_conflicting_uncovered_arm_or_pole_and_placement_failure_leave_no_plan(self):
        for kind in ("arm", "pole", "engine", "reach"):
            with self.subTest(kind=kind):
                self.factory.state["blocks"].pop("other", None)
                self.proof["new_pole_reach"] = 2
                self.builder.can_place.return_value = {"ok": kind != "engine"}
                if kind in {"arm", "pole"}:
                    self.factory.state["blocks"]["other"] = {"entities": [self.arm if kind == "arm" else self.pole]}
                elif kind == "reach":
                    self.proof["new_pole_reach"] = 1
                self.assertFalse(self.reserve())
                self.assertNotIn(self.key, self.factory.state["blocks"])

    def test_partial_construction_and_observed_units_survive_reload_without_early_ownership(self):
        self.assertTrue(self.reserve())
        self.pending(arm_unit=4)
        self.assertIsNone(self.validate())
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.factory.catalog)
        record = self.factory.state["blocks"][self.key]["adjacent_fuel_intake"]
        self.assertEqual(record["arm_unit"], 4)
        self.assertNotIn("pole_unit", record)
        build = {"type": "build", "name": "small-electric-pole", "position": self.pole["position"]}
        self.builder.ensure_plan = Mock(return_value=build)
        self.assertEqual(self.factory._fuel_burner(self.obs, self.burner, self.source), build)
        self.pending(arm_unit=4, pole_unit=5)
        self.assertIsNone(self.validate())
        self.assertEqual(record["pole_unit"], 5)
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        connection = {"type": "build", "name": "small-electric-pole", "position": {"x": 20, "y": 20}}
        self.factory.ensure_power_connection = Mock(return_value=connection)
        self.assertEqual(self.factory._fuel_burner(self.obs, self.burner, self.source), connection)
        self.factory.ensure_power_connection.return_value = {"status": "succeeded"}
        self.factory.connect_input = Mock(return_value={"status": "succeeded"})
        self.assertEqual(self.factory._fuel_burner(self.obs, self.burner, self.source)["status"], "waiting")
        self.factory.connect_input.assert_not_called()
        self.assertEqual(self.factory.state.get("automated_burners", []), [])
        self.pending(arm_unit=4, pole_unit=5, powered=True)
        self.assertEqual(self.factory._fuel_burner(self.obs, self.burner, self.source)["status"], "succeeded")
        self.assertIn(self.factory._entity_key(self.burner), self.factory.state["automated_burners"])

    def test_rollback_changed_saved_geometry_and_foreign_pole_reservation_fail_closed(self):
        self.assertTrue(self.reserve())
        self.pending()
        original = deepcopy(self.factory.state)
        for defect in ("rollback", "geometry", "reservation", "live_tick"):
            with self.subTest(defect=defect):
                self.factory.state = deepcopy(original)
                self.obs["tick"] = 100
                self.proof["tick"] = 100
                if defect == "rollback":
                    self.obs["tick"] = 99
                elif defect == "geometry":
                    self.factory.state["blocks"][self.key]["entities"][1]["position"]["x"] += 1
                elif defect == "reservation":
                    self.factory.state["blocks"]["other"] = {"entities": [self.pole]}
                else:
                    self.proof["tick"] = 99
                self.assertEqual(self.validate()["status"], "blocked")

    def test_bound_live_pole_can_be_shared_by_another_normal_power_connection(self):
        self.assertTrue(self.reserve())
        self.pending(arm_unit=4, pole_unit=5, powered=True)
        self.assertIsNone(self.validate())
        self.obs["entities"].append({**deepcopy(self.pole), "unit_number": 5})
        self.factory.state["power_links"] = {"other": {"entities": [deepcopy(self.pole)]}}
        self.assertIsNone(self.validate())
        self.obs["entities"][-1]["unit_number"] = 99
        self.assertEqual(self.validate()["status"], "blocked")
        self.obs["entities"][-1]["unit_number"] = 5
        self.factory.state["blocks"]["other"] = {"entities": [{**deepcopy(self.pole), "name": "inserter"}]}
        self.assertEqual(self.validate()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
