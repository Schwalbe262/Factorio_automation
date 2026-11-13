"""basic_petroleum on the remedy ladder: needs, the dry run and the crude pipe through a waypoint.

eval-1575138435 (330 min): the crude field is ~470 tiles of pipe from the refinery site; a route
that only fits through a waypoint must not depend on luck.
"""

import unittest
from unittest.mock import patch

from factorio_agent import petroleum, plan_needs, postmortem, remedy

SOURCE = {"position": {"x": 316.5, "y": 70.5}, "facing": 8, "item": "crude-oil"}
DEST = {"position": {"x": 72.5, "y": -150.5}, "facing": 12, "item": "crude-oil"}
CORE = {"entities": [], "reserved_area": [[60, -160], [80, -140]]}


def pipe(x, y):
    return {"name": "pipe", "position": {"x": x, "y": y}}


class CrudeWaypointTests(unittest.TestCase):
    def legs(self, direct_ok=False):
        calls = []

        def leg(game, builder, obs, source, destination, core):
            calls.append((source["position"], destination["position"], len(core["entities"])))
            if source is SOURCE and destination is DEST:
                return {"ok": direct_ok, "reason": "no_clean_trunk_corridor", "entities": [], "underground_pairs": []}
            a, b = source["position"], destination["position"]
            return {"ok": True, "entities": [pipe(a["x"], a["y"] + 1), pipe(b["x"], b["y"])], "underground_pairs": []}

        return calls, leg

    def test_without_the_rung_a_failed_route_stays_failed(self):
        calls, leg = self.legs()
        with patch.object(petroleum, "_plan_crude_leg", side_effect=leg):
            result = petroleum.plan_crude_connection(None, None, {}, SOURCE, DEST, CORE)
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_the_waypoint_rung_joins_two_legs_on_one_tile(self):
        calls, leg = self.legs()
        with patch.object(petroleum, "_plan_crude_leg", side_effect=leg), remedy.applied({"route_waypoints": True}):
            result = petroleum.plan_crude_connection(None, None, {}, SOURCE, DEST, CORE)
        self.assertTrue(result["ok"])
        w = result["waypoint"]
        self.assertEqual(calls[1][1], {"x": w["x"], "y": w["y"]})
        self.assertEqual(calls[2][0], {"x": w["x"], "y": w["y"]})
        self.assertEqual(calls[2][2], 2)  # the second leg keeps off the first leg's pipes
        tiles = [(e["position"]["x"], e["position"]["y"]) for e in result["entities"]]
        self.assertEqual(tiles.count((w["x"], w["y"])), 1)
        self.assertEqual(result["ports"], [SOURCE, DEST])


class PetroleumNeedsTests(unittest.TestCase):
    def test_a_failed_crude_route_is_route_needs_and_the_ladder_has_its_rungs(self):
        failures = [{"stage": "crude_route", "detail": {"reason": "no_clean_trunk_corridor"}}] * 3
        needs = petroleum._petroleum_needs(failures, SOURCE, (72.5, -150.5))
        self.assertEqual((needs["kind"], needs["item"], needs["from"]), ("route", "crude-oil", [316.5, 70.5]))
        result = {"status": "blocked", "reason": "no_bounded_basic_petroleum_site", "plan_needs": needs}
        self.assertEqual(plan_needs.from_result("basic_petroleum", result)["kind"], "route")
        rungs = [r.name for r in remedy.ladder("basic_petroleum")]
        self.assertIn("relax:waypoint_route", rungs)
        self.assertIn("basic_petroleum", postmortem.DRY_RUN)

    def test_extent_and_power_failures_name_their_change(self):
        extent = petroleum._petroleum_needs([{"stage": "pipeline_extent", "detail": {}}], SOURCE, (0, 0))
        self.assertEqual(extent["reason"], "pipeline_requires_explicit_pump_segment")
        self.assertEqual(petroleum._petroleum_needs([{"stage": "power"}], SOURCE, (0, 0))["kind"], "power")
        self.assertEqual(petroleum._petroleum_needs([], SOURCE, (0, 0))["kind"], "site")


class RefinerySiteTests(unittest.TestCase):
    """The refinery anchor comes from the site service on a planned world (block_sites), not from six
    fixed offsets around the industrial district."""

    def prepare(self, site, blocks=None):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        found = petroleum.BasicPetroleum.__new__(petroleum.BasicPetroleum)
        found.site, found.state, found.game, found.builder = site, {}, None, None
        found.catalog = SimpleNamespace(fingerprint="c")
        found.path = Path(tmp.name) / "basic-petroleum.json"
        (found.path.parent / "pcb-board.json").write_text(
            '{"world_id": "w", "catalog_fingerprint": "c", "blocks": %s}' % (blocks or "{}"), encoding="utf-8"
        )
        cores = []

        def core(catalog, x, y):
            cores.append((x, y))
            return {"reserved_area": [[x - 0.5, y - 0.5], [x + 9.5, y + 12.5]], "ports": [DEST]}

        obs = {
            "world_id": "w",
            "technologies": {"oil-processing": True},
            "enabled_recipes": {"basic-oil-processing": True, "oil-refinery": True},
        }
        with (
            patch.object(petroleum, "refinery_core", side_effect=core),
            patch.object(petroleum, "_identity", return_value=True),
            patch.object(petroleum.BasicPetroleum, "_source", return_value={"ok": True, "source": SOURCE}),
            patch.object(petroleum, "survey_slots", return_value={"ok": True, "legal": []}),
        ):
            result = found.prepare(obs)
        return result, cores[1:]  # the first core call is block_sites' footprint probe (or a legacy anchor)

    def test_planned_world_anchors_are_the_allocated_district_blocks(self):
        from types import SimpleNamespace

        rect = SimpleNamespace(x0=200, y0=-80)
        site = SimpleNamespace(
            allocate_candidates=lambda kind, w, h, **kw: [SimpleNamespace(rect=rect, name="production#3")]
        )
        result, cores = self.prepare(site)
        self.assertEqual(cores, [(200.5, -79.5)])
        self.assertEqual(result["evidence"]["failures"][0]["position"], [200.5, -79.5])
        self.assertIn("site:more_blocks", [r.name for r in remedy.ladder("basic_petroleum")])

    def test_no_allocation_is_a_site_need_and_the_development_map_keeps_its_offsets(self):
        from types import SimpleNamespace

        result, cores = self.prepare(SimpleNamespace(allocate_candidates=lambda *a, **kw: []))
        self.assertEqual(cores, [])
        self.assertEqual(result["plan_needs"]["kind"], "site")
        result, _ = self.prepare(None)
        self.assertEqual(result["evidence"]["attempts"], len(petroleum.LEGACY_ANCHOR_OFFSETS))


class PumpSegmentTests(unittest.TestCase):
    """A crude pipe longer than one native segment (default_pipeline_extent 320) gets inline pumps:
    the owner reported maps whose oil lies outside the survey radius (rings up to 1024 tiles)."""

    BOX = {"left_top": {"x": 0, "y": -1}, "right_bottom": {"x": 3, "y": 2}}

    def straight(self, length):
        path = [(1.5 + k, 0.5) for k in range(length)]
        return path, {p: "pipe" for p in path[1:]}

    def test_a_700_tile_pipe_gets_two_pumps_and_every_segment_fits(self):
        path, kinds = self.straight(700)
        split = petroleum.pump_segments(path, kinds, self.BOX, [path[-1]], 320)
        self.assertTrue(split["ok"])
        self.assertEqual(len(split["pumps"]), 2)
        cuts = [0] + split["pumps"] + [len(path)]
        for a, b in zip(cuts, cuts[1:]):
            start = 0 if a == 0 else a + 2
            xs = [p[0] for p in path[start:b]]
            self.assertLessEqual(max(xs) - min(xs) + 1, 320)

    def test_a_short_pipe_needs_no_pump_and_a_pump_never_sits_on_an_underground_mouth(self):
        path, kinds = self.straight(200)
        self.assertEqual(petroleum.pump_segments(path, kinds, self.BOX, [], 320)["pumps"], [])
        path, kinds = self.straight(400)
        for p in path[250:330]:
            kinds[p] = "pipe-to-ground"
        split = petroleum.pump_segments(path, kinds, self.BOX, [], 320)
        self.assertTrue(split["ok"])
        self.assertTrue(all(kinds[path[i]] == "pipe" and kinds[path[i + 1]] == "pipe" for i in split["pumps"]))
        self.assertLess(split["pumps"][0], 250)

    def test_pump_entities_face_the_flow_and_join_the_pipes_on_both_sides(self):
        path, _ = self.straight(10)
        pumps, replaced, poles = petroleum.pump_entities(path, [4])
        pump = pumps[0]
        self.assertEqual((pump["name"], pump["position"], pump["direction"]), ("pump", {"x": 6.0, "y": 0.5}, 4))
        self.assertEqual(replaced, {path[4], path[5]})
        self.assertEqual(poles[0][0], {"x": 5.5, "y": 1.5})
        nodes = [petroleum.entity("pipe", *p) for p in path if p not in replaced] + pumps
        edges = petroleum._fluid_edges(nodes, [], {"machines": []}, None, pumps)
        joined = sorted(e["b"]["position"]["x"] for e in edges if e["a"]["name"] == "pump")
        self.assertEqual(joined, [4.5, 7.5])
        # The pipes beside the pump are not joined to each other through it.
        self.assertFalse(any({e["a"]["position"]["x"], e["b"]["position"]["x"]} == {4.5, 7.5} for e in edges))


if __name__ == "__main__":
    unittest.main()
