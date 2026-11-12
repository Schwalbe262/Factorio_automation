from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from factorio_agent.bus_budget import declared_bus_demand
from factorio_agent.jobs import Request, StarterExecutors


class BusBudgetTests(unittest.TestCase):
    def board(self):
        return {
            "shared_main_bus": {
                "ports": [
                    {"item": "iron-plate", "priority": "surplus", "demand_per_minute": 37.5},
                    {"item": "iron-plate", "priority": "critical", "demand_per_minute": 72},
                    {"item": "copper-plate", "priority": "critical", "demand_per_minute": 36},
                ]
            },
            "consumer_capacity_allocations": [
                dict(
                    receipt_id="first",
                    consumer="steel_production",
                    item="iron-plate",
                    old_per_minute=37.5,
                    new_per_minute=75,
                    delta_per_minute=37.5,
                ),
                dict(
                    receipt_id="second",
                    consumer="steel_production",
                    item="iron-plate",
                    old_per_minute=75,
                    new_per_minute=112.5,
                    delta_per_minute=37.5,
                ),
            ],
        }

    def test_repeated_additions_are_counted_once_and_pinned_ports_stay_identical(self):
        board = self.board()
        before = deepcopy(board)
        self.assertEqual(declared_bus_demand(board, "iron-plate"), 184.5)
        self.assertEqual(declared_bus_demand(board, "copper-plate"), 36)
        self.assertEqual(board, before)

    def test_duplicate_receipts_and_discontinuous_or_negative_allocations_fail_closed(self):
        for change in (
            {"receipt_id": "first"},
            {"old_per_minute": 70, "new_per_minute": 107.5},
            {"delta_per_minute": -1},
            {"delta_per_minute": float("nan")},
            {"new_per_minute": 115},
        ):
            with self.subTest(change=change):
                board = self.board()
                board["consumer_capacity_allocations"][1].update(change)
                with self.assertRaises(ValueError):
                    declared_bus_demand(board, "iron-plate")

    def test_legacy_board_without_allocations_keeps_original_total(self):
        board = self.board()
        board.pop("consumer_capacity_allocations")
        self.assertEqual(declared_bus_demand(board, "iron-plate"), 109.5)

    def test_one_expansion_can_allocate_two_different_materials(self):
        board = self.board()
        board["consumer_capacity_allocations"].append(
            dict(
                receipt_id="first",
                consumer="steel_production",
                item="copper-plate",
                old_per_minute=0,
                new_per_minute=12,
                delta_per_minute=12,
            )
        )
        self.assertEqual(declared_bus_demand(board, "iron-plate"), 184.5)
        self.assertEqual(declared_bus_demand(board, "copper-plate"), 48)


class SteelCapacityDispatchTests(unittest.TestCase):
    def test_request_requires_a_positive_finite_absolute_rate(self):
        Request("steel15", "world", 100, "steel_capacity", {"rate_per_minute": 15}).validate()
        for params in (
            {},
            {"rate_per_minute": True},
            {"rate_per_minute": float("inf")},
            {"rate_per_minute": 0},
            {"rate_per_minute": 15, "ignore_costs": True},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                Request("steel15", "world", 100, "steel_capacity", params).validate()

    def test_dispatch_and_independent_proof_use_the_same_capacity_controller(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        ex.builder = SimpleNamespace(_sync=Mock())
        ex.starter_observation = Mock(return_value={})
        ex.starter_upkeep = Mock(return_value={"status": "succeeded"})
        ex.normal_research = SimpleNamespace()
        ex.steel_capacity = SimpleNamespace(
            step=Mock(return_value={"type": "build_many"}), proof=Mock(return_value={"new_furnace_units": [10, 11]})
        )
        obs = {"tick": 100}
        params = {"rate_per_minute": 15}
        with patch("factorio_agent.upgrades.next_upgrade", return_value=None):
            self.assertEqual(ex.step("steel_capacity", obs, params)["type"], "build_many")
        ex.steel_capacity.step.assert_called_once_with(obs, params)
        self.assertEqual(ex.proof("steel_capacity", obs, {}, params)["new_furnace_units"], [10, 11])
        ex.steel_capacity.proof.assert_called_once_with(obs)


class EngineCapacityDispatchTests(unittest.TestCase):
    def test_request_rejects_invalid_or_extra_rate_parameters(self):
        Request("engine12", "world", 100, "engine_capacity", {"rate_per_minute": 12}).validate()
        for params in (
            {},
            {"rate_per_minute": True},
            {"rate_per_minute": float("nan")},
            {"rate_per_minute": -1},
            {"rate_per_minute": 12, "ignore_costs": True},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                Request("engine12", "world", 100, "engine_capacity", params).validate()

    def test_public_step_and_independent_proof_use_the_same_engine_extension(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        ex.builder = SimpleNamespace(_sync=Mock())
        ex.starter_observation = Mock(return_value={})
        ex.starter_upkeep = Mock(return_value={"status": "succeeded"})
        ex.normal_research = SimpleNamespace()
        ex.engine_capacity = SimpleNamespace(
            step=Mock(return_value={"type": "build_many"}), proof=Mock(return_value={"new_machine_units": [20, 21]})
        )
        obs = {"tick": 100}
        params = {"rate_per_minute": 12}
        with patch("factorio_agent.upgrades.next_upgrade", return_value=None):
            self.assertEqual(ex.step("engine_capacity", obs, params)["type"], "build_many")
        ex.engine_capacity.step.assert_called_once_with(obs, params)
        self.assertEqual(ex.proof("engine_capacity", obs, {}, params)["new_machine_units"], [20, 21])
        ex.engine_capacity.proof.assert_called_once_with(obs)
