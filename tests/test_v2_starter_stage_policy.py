from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.mining import StarterCapacity


class StarterStageTests(unittest.TestCase):
    def setUp(self):
        self.c = StarterCapacity.__new__(StarterCapacity)
        self.c.state = {"world_id": "w", "pending": {}}
        self.c.cells = Mock(return_value=[])
        self.c.proof = Mock(return_value=None)
        self.c.bootstrap = SimpleNamespace(
            maintain_fuel=Mock(return_value={"type": "insert", "item": "coal", "count": 2})
        )
        self.obs = {"world_id": "w", "tick": 1, "technologies": {}}

    def test_completed_target_yields_before_unrelated_fuel_upkeep(self):
        self.c.cells.return_value = [{}] * 8
        self.c.proof.return_value = {"productive_cells": 8}
        result = self.c.step(self.obs, {"resource": "copper-ore", "cells": 8})
        self.assertEqual(result["status"], "succeeded")
        self.c.bootstrap.maintain_fuel.assert_not_called()

    def test_fresh_early_tier_can_still_build_its_original_starter_target(self):
        result = self.c.step(self.obs, {"resource": "copper-ore", "cells": 8})
        self.assertEqual(result["type"], "insert")
        self.c.bootstrap.maintain_fuel.assert_called_once()

    def test_native_unlock_stops_new_bulk_burner_investment_without_deleting_paid_work(self):
        self.obs["technologies"]["electric-mining-drill"] = True
        self.c.state["pending"]["copper-ore"] = {"paid": "preserved"}
        result = self.c.step(self.obs, {"resource": "copper-ore", "cells": 8})
        # Closed, not blocked: no leftover blocked job holding the lane (pace1 fleet).
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["reason"], "starter_capacity_closed_by_electric_mining_unlock")
        self.c.bootstrap.maintain_fuel.assert_not_called()
        self.assertEqual(self.c.state["pending"]["copper-ore"], {"paid": "preserved"})

    def test_the_closed_target_has_a_proof_that_names_the_unlock(self):
        c = StarterCapacity.__new__(StarterCapacity)
        c.cells = Mock(return_value=[])
        obs = {"tick": 5, "entities": [], "technologies": {"electric-mining-drill": True}}
        proof = c.proof(obs, {"resource": "iron-ore", "cells": 6})
        self.assertEqual(proof["closed_by_technology"], "electric-mining-drill")
        self.assertEqual(proof["productive_cells"], 0)
        self.assertIsNone(c.proof(obs, {"resource": "iron-ore", "cells": 2}))
        self.assertIsNone(c.proof(dict(obs, technologies={}), {"resource": "iron-ore", "cells": 6}))

    def test_a_coal_chest_emptied_by_collection_still_proves_by_its_working_drill(self):
        c = StarterCapacity.__new__(StarterCapacity)
        drill = {"name": "burner-mining-drill", "position": {"x": 0, "y": 0}}
        chest = {"name": "wooden-chest", "position": {"x": -0.5, "y": -1.5}}
        c.cells = Mock(return_value=[{"drill": drill, "receiver": chest, "fuel": 3}])
        live_chest = dict(chest, inventory={})
        for status, expected in (("working", 1), ("waiting_for_space_in_destination", 1), ("no_fuel", None)):
            obs = {"tick": 5, "entities": [dict(drill, status_name=status), live_chest]}
            proof = c.proof(obs, {"resource": "coal", "cells": 1})
            self.assertEqual(proof and proof["productive_cells"], expected, status)

    def test_two_cell_recovery_and_other_resources_remain_available(self):
        self.obs["technologies"]["electric-mining-drill"] = True
        for resource, count in (("copper-ore", 2), ("iron-ore", 1), ("coal", 8), ("stone", 8)):
            with self.subTest(resource=resource):
                self.assertEqual(self.c.step(self.obs, {"resource": resource, "cells": count})["type"], "insert")

    def test_existing_full_target_keeps_its_native_proof_after_unlock(self):
        self.obs["technologies"]["electric-mining-drill"] = True
        self.c.cells.return_value = [{}] * 8
        self.c.proof.return_value = {"productive_cells": 8}
        self.assertEqual(self.c.step(self.obs, {"resource": "copper-ore", "cells": 8})["status"], "succeeded")
