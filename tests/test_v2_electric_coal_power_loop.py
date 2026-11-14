"""The electric coal drill feeds the boiler that powers it (electric_mining power loop).

Fleet aud-1114-0930: replacement_electric_drill_has_no_power on 4/12 seeds. 2118231430, 3317405034
and 3416914852: one boiler, ~20 electric drills, the plant in brownout; the coal drill read
``low_power`` (buffer sometimes 0) while coal still arrived, and one sample blocked the upgrade.
2363694308: mining_outpost added drills right after the cutover, the boiler ran dry (``no_fuel``),
the drill stood ``no_power`` and nothing restarted the plant.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factorio_agent import electric_mining, plan_needs, postmortem, remedy
from factorio_agent.electric_mining import drill_power, poles_on_plant, replacement_for
from factorio_agent.power_layout import upgrade_tiles
from test_v2_electric_coal_layouts import DRILL, ENGINE, Harness, boundary_plan, without, world
from test_v2_electric_coal_starter_clear import harness as obstructed_harness

RECEIVER = {"x": DRILL["x"] + 1.5, "y": DRILL["y"] - 0.5}


def electric(status, energy, network=1):
    return dict(
        replacement_for(DRILL),
        unit_number=30,
        electric_network_connected=True,
        electric_network_id=network,
        energy=energy,
        status_name=status,
    )


def cut_over(h, obs):
    """Run the cutover to the built drill: mine the burner, build the electric drill."""
    h.step(obs)  # mine burner
    return without(obs, "burner-mining-drill")


def engine_state(obs, status):
    for e in obs["entities"]:
        if e["name"] == "steam-engine":
            e["status_name"] = status


class DrillPowerTests(unittest.TestCase):
    def test_classification(self):
        obs = {"entities": [dict(ENGINE)]}
        self.assertEqual(drill_power(obs, electric("working", 1000)), "powered")
        self.assertEqual(drill_power(obs, electric("low_power", 0)), "brownout")
        self.assertEqual(drill_power(obs, electric("no_power", 0)), "brownout")  # engines still generate
        dead = {"entities": [dict(ENGINE, status_name="no_input_fluid")]}
        self.assertEqual(drill_power(dead, electric("no_power", 0)), "dead")
        self.assertEqual(
            drill_power(obs, dict(electric("working", 0), electric_network_connected=False)), "unconnected"
        )

    def test_poles_must_be_on_a_generating_network(self):
        plan = {"entities": [{"name": "small-electric-pole", "position": {"x": 1.5, "y": 1.5}}]}
        pole = {"name": "small-electric-pole", "position": {"x": 1.5, "y": 1.5}, "electric_network_id": 1}
        self.assertTrue(poles_on_plant({"entities": [dict(ENGINE), pole]}, plan))
        self.assertFalse(poles_on_plant({"entities": [dict(ENGINE, status_name="no_input_fluid"), pole]}, plan))
        self.assertFalse(poles_on_plant({"entities": [dict(ENGINE), dict(pole, electric_network_id=2)]}, plan))
        self.assertTrue(poles_on_plant({"entities": []}, {"entities": []}))


class OrderOfOperationsTests(unittest.TestCase):
    def test_burner_feed_stays_until_the_poles_are_on_a_generating_plant(self):
        with tempfile.TemporaryDirectory() as d:
            plan = boundary_plan()
            h = Harness(d, plan)
            obs = world(plan)
            engine_state(obs, "no_input_fluid")
            result = h.step(obs)
            self.assertEqual(result["status"], "waiting")
            self.assertIn("generating network", result["reason"])
            self.assertEqual(h.routine.state["phase"], "fund_and_power")
            engine_state(obs, "working")
            self.assertEqual((h.step(obs)["type"]), "mine")


class AfterCutoverTests(unittest.TestCase):
    def built(self, d, drill):
        plan = boundary_plan()
        h = Harness(d, plan)
        obs = cut_over(h, world(plan))
        h.step(obs)  # build
        obs["entities"].append(drill)
        return h, obs

    def test_brownout_does_not_block_and_the_proof_accepts_delivered_coal(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = self.built(d, electric("low_power", 0))
            action = h.step(obs)
            self.assertEqual((action["type"], action["position"]), ("take", RECEIVER))
            obs["tick"] += 400
            for e in obs["entities"]:
                if e["position"] == RECEIVER and e["name"] == "transport-belt":
                    e["belt_inventory"] = {"coal": 2}
            action = h.step(obs)
            self.assertEqual((action["type"], action["name"]), ("mine", "burner-inserter"))
            result = h.step(without(obs, "burner-inserter"))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["evidence"]["drill_power"], "brownout")

    def test_dead_plant_is_restarted_by_one_fill(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = self.built(d, electric("no_power", 0))
            engine_state(obs, "no_input_fluid")
            fill = {"type": "insert", "item": "coal", "inventory": "fuel", "count": 5}
            with mock.patch("factorio_agent.power_coal_feed.dead_plant_restart", return_value=fill) as restart:
                self.assertIs(h.step(obs), fill)
            restart.assert_called_once()

    def test_dead_plant_without_fuel_blocks_with_a_power_need(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = self.built(d, electric("no_power", 0))
            engine_state(obs, "no_input_fluid")
            with mock.patch("factorio_agent.power_coal_feed.dead_plant_restart", return_value=None):
                result = h.step(obs)
            self.assertEqual(
                (result["status"], result["reason"]), ("blocked", "replacement_electric_drill_has_no_power")
            )
            needs = plan_needs.from_result("electric_coal", result)
            self.assertEqual(needs["kind"], "power")
            self.assertIn("dead_plant_restart", needs["change"])

    def test_unconnected_drill_re_ensures_its_pole_chain(self):
        with tempfile.TemporaryDirectory() as d:
            drill = dict(electric("working", 0), electric_network_connected=False)
            h, obs = self.built(d, drill)
            before = len(h.builder.ensured)
            result = h.step(obs)
            self.assertEqual(len(h.builder.ensured), before + 1)
            self.assertEqual(result["reason"], "replacement_electric_drill_not_connected")


class ObstructionNeedsTests(unittest.TestCase):
    def test_block_owned_blocker_names_its_owner(self):
        with tempfile.TemporaryDirectory() as d:
            h, obs = obstructed_harness(d, board_owned=True)
            result = h.step(obs)
            needs = plan_needs.from_result("electric_coal", result)
            self.assertEqual(needs["kind"], "preflight")
            self.assertEqual(needs["blockers"][0]["owner"], "cell")
            self.assertFalse((Path(d) / "electric-coal.json").exists())

    def test_clear_rung_relocates_a_pole_of_ours_before_the_cutover(self):
        from test_v2_electric_coal_starter_clear import ObstructingGame

        pole = {"name": "small-electric-pole", "position": {"x": 64.5, "y": -30.5}, "unit_number": 77}
        with tempfile.TemporaryDirectory() as d:
            plan = boundary_plan()
            h = Harness(d, plan)
            h.game = ObstructingGame(d, pole)
            h.routine.game = h.game
            obs = world(plan)
            obs["entities"].append(dict(pole, electric_network_id=1))
            self.assertEqual(h.step(obs)["reason"], "electric_upgrade_footprint_obstructed")
            moving = {"type": "build", "name": "small-electric-pole", "position": {"x": 60.5, "y": -30.5}}
            relocation = mock.MagicMock()
            relocation.return_value.step.return_value = moving
            with mock.patch("factorio_agent.pole_relocation.PoleRelocation", relocation):
                with remedy.applied({electric_mining.RELOCATE_POLE_OPTION: True}):
                    self.assertIs(h.step(obs), moving)
            self.assertEqual(h.routine.state["relocate_poles"], [{"position": pole["position"], "unit_number": 77}])
            fault = relocation.return_value.step.call_args.args[1]
            self.assertEqual(fault["unit_number"], 77)
            gone = [e for e in obs["entities"] if e.get("unit_number") != 77]
            relocation.return_value.active.return_value = False
            with mock.patch("factorio_agent.pole_relocation.PoleRelocation", relocation):
                self.assertEqual(h.step(dict(obs, entities=gone))["type"], "mine")


class LadderTests(unittest.TestCase):
    def test_electric_coal_is_on_the_ladder_replan_site_clear(self):
        self.assertIn("electric_coal", remedy.dry_runs())
        self.assertIsNotNone(postmortem.DRY_RUN["electric_coal"].saved)
        self.assertEqual([r.kind for r in remedy.ladder("electric_coal")], ["replan", "site", "clear"])
        rung, _ = remedy.choose(
            "electric_coal", {"kind": "preflight", "reason": "electric_upgrade_footprint_obstructed"}
        )
        self.assertEqual(rung.name, "replan")
        tried = [{"rung": "replan", "signature": plan_needs.signature({"kind": "preflight", "reason": "x"})}]
        rung, _ = remedy.choose("electric_coal", {"kind": "preflight", "reason": "x"}, tried)
        self.assertEqual(rung.options, {electric_mining.RELOCATE_POLE_OPTION: True})

    def test_plan_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            plan = boundary_plan()
            h = Harness(d, plan)
            with mock.patch.object(electric_mining, "plan_connection", h.connection):
                result = h.routine.plan(world(plan))
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"]["phase"], "fund_and_power")
            self.assertFalse((Path(d) / "electric-coal.json").exists())
            self.assertEqual(h.routine.state, {})


class FeedSiteTests(unittest.TestCase):
    def test_upgrade_tiles_cover_the_extra_row_and_column(self):
        from factorio_agent.core.deterministic_builder import FactoryBuilder
        from factorio_agent.power_layout import compact_coal_cell

        builder = FactoryBuilder.__new__(FactoryBuilder)
        tiles = upgrade_tiles(builder, compact_coal_cell({"x": 7, "y": -69}))
        self.assertEqual(len(tiles), 5)
        # 3990724461: the starter cell's burner drill at (5,-69) covers (5.5,-69.5) and (5.5,-68.5).
        starter = builder._occupied_by_plan([{"name": "burner-mining-drill", "position": {"x": 5, "y": -69}}])
        self.assertTrue(tiles & starter)


if __name__ == "__main__":
    unittest.main()
