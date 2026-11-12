import unittest

from factorio_agent.verification import FlowWindow


class SustainedFlowTests(unittest.TestCase):
    def sample(self, window, tick, produced, consumed, *, world="a", healthy=True):
        return window.add(
            world_id=world,
            tick=tick,
            produced={"science": produced},
            consumed={"science": consumed},
            supplies_healthy=healthy,
        )

    def test_starting_inventory_and_lifetime_counts_cannot_prove_current_flow(self):
        window = FlowWindow({"science": 30})
        for tick in range(0, 18001, 600):
            result = self.sample(window, tick, 100000, 100000)
        self.assertEqual(result["status"], "insufficient_throughput")

    def test_five_minute_production_and_consumption_pass(self):
        window = FlowWindow({"science": 30})
        for tick in range(0, 18001, 600):
            result = self.sample(window, tick, tick / 120, tick / 120)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["intervals"]), 5)

    def test_stored_output_without_downstream_consumption_fails(self):
        window = FlowWindow({"science": 30})
        for tick in range(0, 18001, 600):
            result = self.sample(window, tick, tick / 120, 0)
        self.assertEqual(result["status"], "insufficient_throughput")

    def test_realistic_irregular_sampling_can_complete_without_interpolation(self):
        window = FlowWindow({"science": 30})
        for tick in range(0, 21001, 137):
            result = self.sample(window, tick, tick / 120, tick / 120)
        self.assertEqual(result["status"], "succeeded")
        self.assertGreaterEqual(result["to_tick"] - result["from_tick"], 18000)

    def test_fast_burst_cannot_hide_four_idle_minutes(self):
        window = FlowWindow({"science": 30})
        for tick in range(0, 18001, 600):
            result = self.sample(window, tick, min(tick, 3600) / 24, min(tick, 3600) / 24)
        self.assertEqual(result["status"], "insufficient_throughput")

    def test_observation_gap_supply_fault_rollback_and_world_change_reset_window(self):
        for change, reason in (
            ({"tick": 1000}, "observation_gap"),
            ({"tick": 0}, "tick_rollback_or_duplicate"),
            ({"world": "other", "tick": 600}, "world_changed"),
            ({"healthy": False, "tick": 600}, "supply_fault"),
        ):
            window = FlowWindow({"science": 30})
            self.sample(window, 0, 20, 20)
            arguments = {"tick": 600, "produced": 21, "consumed": 21, **change}
            result = self.sample(window, **arguments)
            self.assertEqual(result["status"], "sampling")
            self.assertEqual(result["reset_reason"], reason)


if __name__ == "__main__":
    unittest.main()
