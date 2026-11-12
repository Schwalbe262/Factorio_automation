from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.jobs import CAPABILITIES, Request, StarterExecutors


class PreparationDispatchTests(unittest.TestCase):
    def test_typed_building_stock_contract_rejects_raw_or_unbounded_targets(self):
        Request(
            "prepare", "world", 1, "prepare_components", {"items": {"electric-mining-drill": 2, "transport-belt": 20}}
        ).validate()
        for items in ({"iron-plate": 20}, {"electric-mining-drill": 200}, {"electric-mining-drill": True}):
            with self.subTest(items=items), self.assertRaises(ValueError):
                Request("prepare", "world", 1, "prepare_components", {"items": items}).validate()
        self.assertTrue(CAPABILITIES["prepare_components"]["opportunistic"])

    def test_preparation_uses_its_own_stock_proof_instead_of_rebuilding_starter(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        ex._approved_initial_defense = Mock(return_value=None)
        action = {"type": "craft", "recipe": "transport-belt", "count": 5}
        proof = {"world_id": "w", "tick": 1, "held": {"transport-belt": 10}}
        component = SimpleNamespace(step=Mock(return_value=action), proof=Mock(return_value=proof))
        ex._component_reserve = Mock(return_value=component)
        ex.starter_upkeep = Mock(side_effect=AssertionError("preparation must not rebuild unrelated starter machinery"))
        obs = {"world_id": "w", "tick": 1}
        parameters = {"items": {"transport-belt": 10}}
        self.assertEqual(ex.step("prepare_components", obs, parameters), action)
        self.assertEqual(ex.proof("prepare_components", obs, {}, parameters), proof)

    def test_no_speculative_work_before_primary_research_inputs_are_finished(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        for result in (
            {"status": "waiting", "reason": "supply inputs"},
            {"type": "insert", "item": "automation-science-pack"},
            {"status": "succeeded", "evidence": {"autonomous_wait": True}},
        ):
            self.assertIs(ex._science_wait_action({}, None, result), result)
        self.assertFalse(hasattr(ex, "science_pipeline"))
