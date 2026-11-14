"""A belt-fed boiler that ran dry is restarted by one hand fill (power_coal_feed.dead_plant_restart).

Fleets aud-1114-0752 (889907428) and aud-1114-0841 (1606534421): the electric coal drill feeding
the boiler needs the power the boiler makes; the boiler ran dry, the drill stopped, the plant
stayed dead and electric_coal blocked replacement_electric_drill_has_no_power.
"""

import tempfile
import unittest
from unittest.mock import Mock

from factorio_agent import power_coal_feed as pcf
from factorio_agent.core.deterministic_builder import BELT_FED, HAND_FED
from factorio_agent.supervisor import SUPPLY_RETRY_REASONS
from test_v2_power_coal_feed import CATALOG, COAL_PLAN, boiler_row, builder, obs


def belt_fed(d):
    return builder(d, coal_plan=COAL_PLAN, power_mode=BELT_FED, power_sample_tick=0)


class DeadPlantRestartTests(unittest.TestCase):
    def test_dry_belt_fed_boiler_gets_carried_coal(self):
        with tempfile.TemporaryDirectory() as d:
            b = belt_fed(d)
            action = pcf.dead_plant_restart(b, b.bootstrap, CATALOG, obs(coal_carried=40, boiler=boiler_row(coal=0)))
            self.assertEqual((action["type"], action["item"], action["inventory"]), ("insert", "coal", "fuel"))
            self.assertEqual(action["count"], min(40, pcf.boiler_sizing(CATALOG)["target"]))
            self.assertEqual(action["reason"], pcf.RESTART_REASON)

    def test_wood_when_no_coal_is_carried(self):
        with tempfile.TemporaryDirectory() as d:
            b = belt_fed(d)
            state = dict(obs(boiler=boiler_row(coal=0)), inventory={"wood": 30})
            action = pcf.dead_plant_restart(b, b.bootstrap, CATALOG, state)
            self.assertEqual((action["item"], action["count"]), ("wood", 30))

    def test_nothing_carried_collects_coal_or_waits(self):
        with tempfile.TemporaryDirectory() as d:
            b = belt_fed(d)
            self.assertIsNone(pcf.dead_plant_restart(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=0))))
            b.bootstrap.ensure_item.assert_called_once()
            take = {"type": "take", "item": "coal", "count": 8}
            b.bootstrap.ensure_item = Mock(return_value=take)
            self.assertIs(pcf.dead_plant_restart(b, b.bootstrap, CATALOG, obs(boiler=boiler_row(coal=0))), take)

    def test_a_running_or_hand_fed_boiler_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            b = belt_fed(d)
            for boiler in (boiler_row(coal=3), boiler_row(coal=0, burning=1e6)):
                self.assertIsNone(pcf.dead_plant_restart(b, b.bootstrap, CATALOG, obs(coal_carried=40, boiler=boiler)))
            hand = builder(d, coal_plan=COAL_PLAN, power_mode=HAND_FED, power_sample_tick=0)
            self.assertIsNone(
                pcf.dead_plant_restart(hand, hand.bootstrap, CATALOG, obs(coal_carried=40, boiler=boiler_row(coal=0)))
            )

    def test_boiler_upkeep_restarts_and_the_blocked_upgrade_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            b = belt_fed(d)
            ex = Mock(builder=b, bootstrap=b.bootstrap, catalog=CATALOG)
            action = pcf.boiler_upkeep(ex, obs(coal_carried=40, boiler=boiler_row(coal=0)))
            self.assertEqual(action["reason"], pcf.RESTART_REASON)
        self.assertIn("replacement_electric_drill_has_no_power", SUPPLY_RETRY_REASONS)


if __name__ == "__main__":
    unittest.main()
