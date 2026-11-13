"""A feeder route may follow the plan's reserved corridor even when that corridor detours.

Fleet all-1113-2155 seed 2741767595: the coal bank exits west at (-67.5, 14.5); the plan's coal
corridor runs west to x -100, south around the iron patch (x -93..-57, y 22..57) and back east to
the smelting zone's coal entry (-68, 74): 117 tiles of segments for a 60-73 tile straight
distance. ``route_trunks``' fixed 1.35 detour cap refused every such trunk, so 16 of 19 district
candidates failed at the corridor stage with 15 legal sites, and the run was dead from minute 24.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent.routing import route_trunks
from factorio_agent.site.geometry import Rect
from factorio_agent.site.planzones import Corridor, Segment
from factorio_agent.smelting_site import CORRIDOR_SLACK, ROUTE_DETOUR, corridor_detour


def coal_plan():
    corridor = Corridor(
        "corridor:coal->smelting",
        "feeder",
        "coal",
        "mining:coal",
        "smelting",
        4,
        "W",
        "W",
        (
            Segment(Rect(-100, 18, -73, 22), "W"),
            Segment(Rect(-104, 18, -100, 76), "S"),
            Segment(Rect(-100, 72, -68, 76), "E"),
        ),
    )
    return SimpleNamespace(corridor=lambda name: corridor if name == corridor.name else None)


class CorridorDetourTests(unittest.TestCase):
    def test_the_detour_cap_is_the_planned_corridor_path(self):
        start, end = (-67.5, 14.5), (-52.5, 72.5)  # coal port, a district's coal input
        ratio = corridor_detour(coal_plan(), "coal", start, end)
        planned = (5.5 + 3.5) + (27 + 58 + 32) + 15.5  # to the first segment, along, from the last
        self.assertAlmostEqual(ratio, CORRIDOR_SLACK * planned / 73)
        self.assertGreater(ratio, 1.77)  # the trunk the live map needed
        self.assertEqual(corridor_detour(coal_plan(), "iron-ore", start, end), ROUTE_DETOUR)
        self.assertEqual(corridor_detour(None, "coal", start, end), ROUTE_DETOUR)

    def test_a_trunk_around_an_ore_patch_routes_only_with_the_corridor_cap(self):
        # The iron patch between the coal exit and the district, as on the live map.
        iron = {(x + 0.5, y + 0.5) for x in range(-93, -57) for y in range(22, 58)}
        copper = {(x + 0.5, y + 0.5) for x in range(-48, -24) for y in range(27, 53)}
        coal = {(x + 0.5, y + 0.5) for x in range(-67, -44) for y in range(3, 27)} - {(-67.5, 14.5)}
        blocked = iron | copper | coal
        start, end = {"x": -67.5, "y": 14.5}, {"x": -67.5, "y": 74.5}
        bounds = {"min_x": -110.5, "max_x": -40.5, "min_y": -1.5, "max_y": 90.5}
        kwargs = dict(occupied=blocked, bounds=bounds, start_direction=12, end_direction=4, max_nodes=150000)
        self.assertFalse(route_trunks(start, end, **kwargs)["ok"])
        ratio = corridor_detour(coal_plan(), "coal", (start["x"], start["y"]), (end["x"], end["y"]))
        route = route_trunks(start, end, max_detour_ratio=ratio, **kwargs)
        self.assertTrue(route["ok"], route.get("reason"))
        self.assertLessEqual(route["metrics"]["detour_ratio"], ratio)

    def test_plan_connection_hands_the_cap_to_the_router(self):
        from factorio_agent import transport

        seen = {}

        def fake_route(*args, **kwargs):
            seen.update(kwargs)
            return {"ok": False, "reason": "no_clean_trunk_corridor"}

        game = SimpleNamespace(query=lambda body: {"ok": True, "blocked": [], "clearable": [], "max_span": 4})
        builder = SimpleNamespace(_occupied_by_plan=lambda es: set())
        obs = {"world_id": "w", "entities": []}
        source = {"item": "coal", "position": {"x": 0.5, "y": 0.5}, "facing": 4}
        destination = {"item": "coal", "position": {"x": 10.5, "y": 0.5}, "facing": 4}
        with patch.object(transport, "route_trunks", side_effect=fake_route):
            transport.plan_connection(game, builder, obs, source, destination, max_detour_ratio=1.9)
        self.assertEqual(seen.get("max_detour_ratio"), 1.9)


if __name__ == "__main__":
    unittest.main()
