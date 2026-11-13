"""Plans without a board block are keep-outs for every planner that reads planned hardware.

Fleet all-1113-2155 seed 2064942095: ``electric_coal`` planned its pole chain (saved only in
``electric-coal.json``) in the same slice in which the smelting district routed its coal belt;
the pole at (-53.5, 49.5) was built on the district's belt tile, the district's build stopped on
``reserved block placement is obstructed`` and the run was dead to the end.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.board import _Footprints, entity, planned_unbuilt_hardware, unboarded_plans
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.smelting import SmeltingDistrict

from test_v2_smelting_plan_site import PrepareOnPlanTests

POLE = entity("small-electric-pole", -53.5, 49.5)


def write_electric_coal(root, world_id="w"):
    state = {"world_id": world_id, "power_plan": {"entities": [POLE, entity("small-electric-pole", -47.5, 55.5)]}}
    (root / "electric-coal.json").write_text(json.dumps(state), encoding="utf-8")


class UnboardedPlanTests(unittest.TestCase):
    def test_electric_coal_poles_are_planned_hardware_until_built(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_electric_coal(root)
            blocks = unboarded_plans(root, {"world_id": "w"})
            self.assertIn("electric-coal:power_plan", blocks)
            tiles = planned_unbuilt_hardware(_Footprints(), {"world_id": "w", "entities": []}, blocks)
            self.assertIn((-53.5, 49.5), tiles)
            built = {"world_id": "w", "entities": [dict(POLE, unit_number=7)]}
            self.assertNotIn((-53.5, 49.5), planned_unbuilt_hardware(_Footprints(), built, blocks))
            self.assertEqual(unboarded_plans(root, {"world_id": "other"}), {})


class GrowthPreflightEvidenceTests(unittest.TestCase):
    def test_a_failed_growth_preflight_names_its_blockers_in_the_evidence(self):
        # Fleet all-1113-2107 seed 2616807070: blocked with evidence {} - nothing to derive a retry from.
        from factorio_agent.smelting import _preflight_evidence

        native = {
            "ok": False,
            "reason": "terrain_or_entity_collision",
            "blocked": [
                {"name": "stone-furnace", "position": {"x": 1, "y": 2}, "reason": "terrain_or_entity_collision"}
            ],
        }
        evidence = _preflight_evidence(native)
        self.assertEqual(evidence["blockers"][0]["position"], {"x": 1, "y": 2})
        self.assertEqual(evidence["native_reason"], "terrain_or_entity_collision")
        self.assertEqual(_preflight_evidence({"ok": False}), {})


class CoalUpgradeKeepoutTests(unittest.TestCase):
    def test_starter_cells_keep_off_the_electric_replacement_of_the_feed_drill(self):
        # Seed 3910665204: feed drill at (-34, 19), a starter coal cell drill placed at (-34, 17).
        from factorio_agent.board import keepout_tiles, starter_keepouts

        with TemporaryDirectory() as directory:
            root = Path(directory)
            drill = dict(entity("burner-mining-drill", -34, 19, 4), mining_resource="coal")
            state = {"world_id": "w", "coal_plan": {"drill": drill, "entities": [drill]}}
            (root / "factory-builder.json").write_text(json.dumps(state), encoding="utf-8")
            keep = starter_keepouts(root, {"world_id": "w", "entities": []})
            self.assertIn([-36.0, 17.0, -33.0, 20.0], keep["areas"])
            self.assertIn((-34.5, 17.5), keepout_tiles(keep))  # the starter drill's upper tiles
            # The feed planner itself (its own block excluded) and an upgraded drill add nothing.
            self.assertNotIn(
                [-36.0, 17.0, -33.0, 20.0],
                starter_keepouts(root, {"world_id": "w"}, exclude=("power_coal_feed",))["areas"],
            )
            state["coal_plan"]["drill"] = dict(drill, name="electric-mining-drill")
            (root / "factory-builder.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(starter_keepouts(root, {"world_id": "w", "entities": []})["areas"], [])


class DistrictKeepsOffUnboardedHardwareTests(unittest.TestCase):
    def test_a_route_over_an_unbuilt_electric_coal_pole_fails_the_site_and_the_next_one_is_taken(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            PrepareOnPlanTests().prepare(root, SimpleNamespace(plan=Mock(side_effect=RuntimeError("files only"))))
            write_electric_coal(root)
            seen = []

            def belts(game, builder, obs, source, destination, reserved=(), **kwargs):
                seen.append(reserved)
                if len(seen) <= 3:  # the first site's routes run over the pole tile
                    return _plan([entity("transport-belt", -53.5, 49.5, 0)], [], underground_pairs=[], route_metrics={})
                return _plan([], [], underground_pairs=[], route_metrics={"length": 5})

            catalog = SimpleNamespace(
                fingerprint="c",
                recipes={"iron-plate": {"energy": 3.2}, "copper-plate": {"energy": 3.2}},
                entities={"stone-furnace": {"crafting_speed": 1}},
            )
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "legal": [1, 2]})
            )
            routine = SmeltingDistrict(game, None, FactoryBuilder.__new__(FactoryBuilder), catalog, site=None)
            obs = {"world_id": "w", "technologies": {"logistics": True}, "entities": []}
            with (
                patch("factorio_agent.smelting.belt_connection", side_effect=belts),
                patch("factorio_agent.smelting.power_connection", return_value=_plan([], [])),
                patch("factorio_agent.smelting.preflight", return_value={"ok": True, "clear_obstacles": []}),
            ):
                result = routine.prepare(obs)
        self.assertTrue(result["ok"], result)
        self.assertGreater(len(seen), 3)  # the first site was dropped
        # The router is told about the unbuilt pole, so it can keep off it.
        self.assertTrue(any(e == POLE for e in seen[0]))


if __name__ == "__main__":
    unittest.main()
