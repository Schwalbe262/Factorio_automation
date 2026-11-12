"""Rules added after stalls on live maps; each test pins one of them."""

import unittest

from factorio_agent.construction_supply import FUEL_SKIP_STOCK
from factorio_agent.core.deterministic_builder import COAL_RICHNESS_TIERS, FactoryBuilder
from factorio_agent.jobs import RESEARCH_CAPABILITIES, _owns_research
from factorio_agent.mining import StarterCapacity


def furnace_cell(x, *, fuel=0, burning=False, plates=0, finished=0):
    drill = {"name": "burner-mining-drill", "position": {"x": x, "y": 2}}
    furnace = {
        "name": "stone-furnace",
        "position": {"x": x, "y": 0},
        "output_inventory": {"copper-plate": plates} if plates else {},
        "products_finished": finished,
    }
    cell = {"drill": drill, "receiver": {"name": "stone-furnace", "position": {"x": x, "y": 0}}, "fuel": fuel}
    cell["burning"] = burning
    return cell, furnace


class StockedCellProofTests(unittest.TestCase):
    def capacity(self, cells):
        controller = StarterCapacity.__new__(StarterCapacity)
        controller.cells = lambda resource: cells
        return controller

    def test_unfuelled_cell_with_enough_finished_output_counts_as_productive(self):
        running, furnace_a = furnace_cell(0, fuel=2, finished=30)
        idle, furnace_b = furnace_cell(4, plates=FUEL_SKIP_STOCK, finished=80)
        obs = {"tick": 1, "entities": [furnace_a, furnace_b]}
        proof = self.capacity([running, idle]).proof(obs, {"resource": "copper-ore", "cells": 2})
        self.assertEqual(proof["productive_cells"], 2)

    def test_unfuelled_cell_without_stock_still_does_not_count(self):
        running, furnace_a = furnace_cell(0, fuel=2, finished=30)
        empty, furnace_b = furnace_cell(4, plates=3, finished=3)
        obs = {"tick": 1, "entities": [furnace_a, furnace_b]}
        self.assertIsNone(self.capacity([running, empty]).proof(obs, {"resource": "copper-ore", "cells": 2}))


class CoalRichnessOrderTests(unittest.TestCase):
    def test_richer_sites_come_first_and_thin_ones_remain_as_last_resort(self):
        amounts = {(0, 0): 300, (5, 0): 5000, (9, 0): 1200, (20, 0): 2500}
        builder = FactoryBuilder.__new__(FactoryBuilder)
        builder.coal_sites = lambda minimum=0: [
            {"x": x, "y": y} for (x, y), amount in amounts.items() if amount >= minimum
        ]
        order = [(site["x"], site["y"]) for site in builder.coal_sites_richest_first()]
        self.assertEqual(order, [(5, 0), (20, 0), (9, 0), (0, 0)])
        self.assertEqual(COAL_RICHNESS_TIERS[-1], 0)


class ResearchOwnershipTests(unittest.TestCase):
    def test_idle_lane_and_admission_agree_on_who_owns_research(self):
        for capability in RESEARCH_CAPABILITIES:
            self.assertTrue(_owns_research(capability), capability)
        self.assertFalse(_owns_research("power"))


class SharedErrorClassTests(unittest.TestCase):
    def test_supervisor_run_as_a_script_catches_the_bridge_error(self):
        """A refused submit raised by the in-process bridge must be the class the supervisor catches.

        Running ``python -m factorio_agent.supervisor`` loads supervisor.py a
        second time under another module name; a class defined inside it would
        then differ from the one the bridge raises, and every deferred dispatch
        became a controller error with up to 300 s of backoff.
        """
        import runpy

        from factorio_agent import errors
        from factorio_agent.kernel import session

        as_script = runpy.run_module("factorio_agent.supervisor", run_name="supervisor_as_script")
        self.assertIs(as_script["CLIError"], errors.CLIError)
        self.assertIs(session.CLIError, errors.CLIError)


if __name__ == "__main__":
    unittest.main()
