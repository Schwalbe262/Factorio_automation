"""eval-1575138435 at 257 game minutes: the iron growth job's rules (live evidence, read-only probes).

Iron 300/min blocked ``owned_ore_trunk_trace_failed`` at ``(47.5, -25.5)``: the proven iron banks were
the district trunk bank (180/min, port 41.5,-29.5), a bank with its own consumer connection and an
extra bank whose output belt ends on the patch (port 47.5,-25.5). The two first banks had no proof:
their quiet-start defense deferral had lapsed (native tier ``mid``) and no turret stood, so the job
counted 60/min and connected a third module to the first proven bank, the dead end.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.outposts import MiningOutposts


def bank(port, *, ready=True, area=((27.0, -33.0), (45.0, -26.0)), scope=None):
    drill = {"name": "electric-mining-drill", "position": {"x": port[0] - 3, "y": port[1] + 2}}
    return {
        "ready": ready,
        "plan": {
            "resource": "iron-ore",
            "ports": [{"position": {"x": port[0], "y": port[1]}, "facing": 4, "item": "iron-ore"}],
            "entities": [drill],
        },
        "mining_reserved_area": [list(area[0]), list(area[1])],
        "nominal_rate_per_minute": 60.0,
        **({"defense_scope_id": scope} if scope else {}),
    }


def outposts(banks):
    found = MiningOutposts.__new__(MiningOutposts)
    found.state = {"world_id": "w", "banks": banks}
    return found


OBS = {"world_id": "w", "tick": 1, "resources": {"iron-ore": {"position": {"x": 36, "y": -29}}}, "entities": []}


class TrunkPrimaryTests(unittest.TestCase):
    def test_a_new_module_feeds_the_patch_trunk_not_a_dead_end_bank(self):
        trunk = bank((41.5, -29.5))
        dead_end = bank((47.5, -25.5), area=((39, -29.0), (48.0, -21)))
        found = outposts([trunk, dead_end])
        calls = []

        def connected(obs, primary, count):
            calls.append(primary)
            if primary is dead_end:
                return {"ok": False, "reason": "owned_ore_trunk_trace_failed", "trace": {"position": [47.5, -25.5]}}
            return {"ok": True, "plan": {"resource": "iron-ore"}}

        with (
            patch.object(
                MiningOutposts,
                "prepare_extension",
                return_value={"ok": False, "reason": "paired_mining_reserved_columns_exhausted"},
            ),
            patch.object(MiningOutposts, "prepare_connected_bank", side_effect=connected),
        ):
            growth = found.plan_growth(OBS, "iron-ore", 300, 60, [dead_end])
        self.assertIs(calls[0], trunk)  # the patch's trunk bank first, though only the dead end proved
        self.assertTrue(growth["connected"]["ok"])

    def test_a_trunk_failure_after_the_trace_is_not_retried_on_another_bank(self):
        trunk, other = bank((41.5, -29.5)), bank((22.5, -21.5), area=((19, -33), (26, -18.0)))
        found = outposts([trunk, other])
        with (
            patch.object(MiningOutposts, "prepare_extension", return_value={"ok": False, "reason": "x"}),
            patch.object(
                MiningOutposts,
                "prepare_connected_bank",
                return_value={"ok": False, "reason": "no_clean_extra_bank_connection_to_owned_ore_trunk"},
            ) as connect,
        ):
            growth = found.plan_growth(OBS, "iron-ore", 300, 60, [trunk, other])
        self.assertEqual(connect.call_count, 1)  # the site search is expensive: once
        self.assertEqual(growth["failure"]["upstream_extension_failure"], "x")


class LapsedDefenseTests(unittest.TestCase):
    def test_an_unproven_bank_with_a_lapsed_deferral_is_armed_before_growth(self):
        armed = {"type": "take_full_magazines", "item": "firearm-magazine", "count": 10}
        found = outposts([bank((41.5, -29.5), scope="mining-9832b1cf645497eef02f")])
        found.path = SimpleNamespace(parent=None)
        found.builder = SimpleNamespace(catalog=None)
        obs = dict(OBS, technologies={"electric-mining-drill": True})
        with (
            patch.object(MiningOutposts, "publish_reservations", return_value=None),
            patch("factorio_agent.depleted_drill_retirement.effective_mining_plan", side_effect=lambda g, o, p: p),
            patch.object(MiningOutposts, "find", return_value={"name": "electric-mining-drill"}),
            patch.object(MiningOutposts, "bank_proof", return_value=None),
            patch.object(MiningOutposts, "bank_powered", return_value=True),
            patch.object(MiningOutposts, "_construction_coverage", return_value=armed) as cover,
            patch.object(MiningOutposts, "plan_growth") as growth,
        ):
            result = found.step(obs, {"resource": "iron-ore", "rate_per_minute": 300.0})
        self.assertEqual(result, armed)
        cover.assert_called_once()
        growth.assert_not_called()


class WaypointRouteTests(unittest.TestCase):
    """steel_supply blocked 77 min: the coal route 52.5,34.5 -> 67.5,-126.5 (176 tiles across the base)
    had no corridor on every candidate; 52.5,34.5 -> 58.5,-60.5 and 58.5,-61.5 -> 67.5,-126.5 both fit."""

    SOURCE = {"item": "coal", "direction": "output", "position": {"x": 52.5, "y": 34.5}, "facing": 4}
    DESTINATION = {"item": "coal", "direction": "input", "position": {"x": 67.5, "y": -126.5}, "facing": 4}

    def test_waypoints_lie_beside_the_source_line_toward_the_destination(self):
        from factorio_agent.transport import waypoints

        found = waypoints(self.SOURCE, self.DESTINATION)
        self.assertEqual(found[0], (58.5, -45.5, 0))  # half way north, six tiles east of the source line
        self.assertTrue(all(f == 0 for _, _, f in found))
        near = dict(self.DESTINATION, position={"x": 60.5, "y": 0.5})
        self.assertEqual(waypoints(self.SOURCE, near), [])  # short routes never take legs

    def test_two_legs_join_straight_and_the_second_avoids_the_first(self):
        from factorio_agent import remedy, transport

        calls = []

        def leg(game, builder, obs, source, destination, reserved=(), **options):
            calls.append((source["position"], destination["position"], len(reserved)))
            if len(calls) == 1:
                return {"ok": False, "reason": "no_clean_trunk_corridor"}  # the direct route
            a, b = source["position"], destination["position"]
            belt = {"name": "transport-belt", "position": dict(a), "direction": source["facing"]}
            span = abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])
            return {
                "ok": True,
                "entities": [belt, {"name": "transport-belt", "position": dict(b), "direction": 0}],
                "required_items": {"transport-belt": 2},
                "route_metrics": {"length": span, "turns": 1},
            }

        with patch.object(transport, "_plan_connection", side_effect=leg):
            plain = transport.plan_connection(None, None, {}, self.SOURCE, self.DESTINATION)
            calls.clear()
            with remedy.applied({"route_waypoints": True}):
                legs = transport.plan_connection(None, None, {}, self.SOURCE, self.DESTINATION)
        self.assertFalse(plain["ok"])
        self.assertTrue(legs["ok"])
        (_, w1, _), (w2, _, reserved) = calls[1], calls[2]
        self.assertEqual((w2["x"], w2["y"]), (w1["x"], w1["y"] - 1))  # the second leg starts one tile on
        self.assertEqual(reserved, 2)  # the first leg's belts are reserved for the second
        self.assertEqual(legs["required_items"], {"transport-belt": 4})
        self.assertEqual(legs["ports"], [self.SOURCE, self.DESTINATION])


class PowerGrowthTests(unittest.TestCase):
    """257 min: one pair (2 engines, 1.8 MW) "working" with energy 0, 29.5 % of the electric consumers
    low_power; the live receipt said demand 2139.5 kW, need 4140 kW, 2 pairs short."""

    CATALOG = {
        "entities": {
            "steam-engine": {"energy_production": 15000},
            "lab": {"energy_usage": 1000},
            "assembling-machine-1": {"energy_usage": 1250},
            "electric-mining-drill": {"energy_usage": 1500},
            "inserter": {"energy_usage": 245},
            "rocket-silo": {"energy_usage": 66500},
        }
    }

    def obs(self):
        engines = [
            {"name": "steam-engine", "status_name": "working", "energy": 0, "electric_network_id": 1} for _ in range(2)
        ]
        machines = [{"name": "lab", "status_name": "low_power", "electric_network_connected": True}] * 8
        machines += [
            {"name": "assembling-machine-1", "status_name": "low_power", "electric_network_connected": True}
        ] * 8
        machines += [
            {"name": "assembling-machine-1", "status_name": "full_output", "electric_network_connected": True}
        ] * 14
        machines += [
            {"name": "electric-mining-drill", "status_name": "working", "electric_network_connected": True}
        ] * 8
        return {"tick": 1, "entities": engines + machines}

    def test_a_browned_out_plant_is_short_and_the_next_stage_follows_the_first(self):
        from factorio_agent import power_growth as pg

        jobs = [{"capability": "steam_expansion", "parameters": {}, "status": "blocked"}]
        receipt = pg.power_receipt(self.obs(), self.CATALOG, jobs)
        self.assertEqual(receipt["capacity_kw"], 1800.0)  # engines at their limit still generate
        self.assertEqual(receipt["demand_kw"], 8 * 60 + 8 * 75 + 8 * 90)
        self.assertTrue(receipt["grow"])
        self.assertGreaterEqual(receipt["pairs_short"], 1)
        caps = {"steam_expansion": {"requires": []}}
        # The first pair is the roadmap's (blocked here: the ladder owns it); no stage 2 beside it.
        self.assertEqual(pg.power_growth_nodes(jobs, [], {"power_growth": receipt}, caps), [])
        done = [{"capability": "steam_expansion", "parameters": {}, "status": "succeeded"}]
        receipt = pg.power_receipt(self.obs(), self.CATALOG, done)
        (node,) = pg.power_growth_nodes(done, [], {"power_growth": receipt}, caps)
        self.assertEqual((node["capability"], node["parameters"]), ("steam_expansion", {"stage": 2}))
        queued = [dict(node, status="pending")]
        self.assertEqual(pg.power_growth_nodes(done, queued, {"power_growth": receipt}, caps), [])

    def test_a_queued_rocket_silo_counts_as_an_upcoming_load(self):
        from factorio_agent import power_growth as pg

        quiet = {
            "tick": 1,
            "entities": [{"name": "steam-engine", "status_name": "working", "energy": 5, "electric_network_id": 1}] * 2,
        }
        idle = pg.power_receipt(quiet, self.CATALOG, [])
        self.assertFalse(idle["grow"])
        silo = pg.power_receipt(quiet, self.CATALOG, [], [{"capability": "rocket_silo", "status": "pending"}])
        self.assertEqual(silo["upcoming_kw"], 3990.0)
        self.assertTrue(silo["grow"])
        self.assertEqual(silo["pairs_short"], 2)  # 3990 x 1.15 = 4588.5 kW over 1800 kW
        headroom = pg.power_receipt(
            quiet,
            self.CATALOG,
            [{"capability": "rocket_silo_x", "status": "waiting", "reason": "rocket_silo_power_headroom_short"}],
        )
        self.assertTrue(headroom["grow"])

    def test_stage_parameters(self):
        from factorio_agent.power_growth import stage_of

        self.assertEqual(stage_of({}), 1)
        self.assertEqual(stage_of({"stage": 3}), 3)
        for bad in ({"stage": 1}, {"stage": "2"}, {"stage": 2, "x": 1}, {"stage": 99}):
            with self.assertRaises(ValueError):
                stage_of(bad)


if __name__ == "__main__":
    unittest.main()
