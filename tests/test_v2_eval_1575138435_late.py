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


if __name__ == "__main__":
    unittest.main()
