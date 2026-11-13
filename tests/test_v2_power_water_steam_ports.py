"""The starter water pipe never joins a steam box (fleet aud-1114-0703 seed 477133967).

The pump at (-7.5, 59.5) had to go around the two engines: the routed pipe went north along
x = -9.5 and passed (-9.5, 51.5), the free end of the second engine. Water and steam became
one fluid system, the boiler never got water (water 0, steam 0, 84 MJ of coal in it) and the
power job waited "for water, steam and a fuelled hand-fed boiler" from 8 to 45 game minutes:
the run reached no milestone at all.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import FactoryBuilder, steam_port_tiles


def e(name, x, y, d=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": d}


# The saved plan of that run (factory-builder.json power_plan), without its routed pipes.
PUMP = e("offshore-pump", -7.5, 59.5, 8)
BANK = [
    e("boiler", 2.0, 51.5, 12),
    e("steam-engine", -1.5, 51.5, 12),
    e("steam-engine", -6.5, 51.5, 12),
    *[e("transport-belt", 4.5, y, 8) for y in (51.5, 50.5, 49.5, 48.5, 47.5)],
    e("burner-inserter", 3.5, 51.5, 4),
    e("pipe", 2.5, 54.5),
    e("pipe", 2.5, 53.5),
]
SOURCE = {"x": -7.5, "y": 58.5}
WATER_PORT = {"x": 2.5, "y": 55.5}
# The lake south of the pump and trees east of it: the route has to go north past the engines.
BLOCKED = [{"x": -6.5, "y": y + 0.5} for y in range(52, 59)]
BLOCKED += [{"x": x + 0.5, "y": y + 0.5} for x in range(-30, 10) for y in range(59, 72)]


def builder(root: Path) -> FactoryBuilder:
    b = FactoryBuilder.__new__(FactoryBuilder)
    b.path = root / "factory-builder.json"
    b.state = {}
    b.game = SimpleNamespace(query=lambda script: {"ok": True, "blocked": BLOCKED, "clearable": []})
    return b


class SteamPortTiles(unittest.TestCase):
    def test_the_free_engine_end_and_the_boiler_steam_tile(self):
        ports = steam_port_tiles(BANK)
        self.assertIn((-9.5, 51.5), ports)  # second engine, west end: the tile of the bug
        self.assertIn((0.5, 51.5), ports)  # boiler steam output (inside the first engine)
        self.assertNotIn((2.5, 53.5), ports)  # the boiler's water tile stays usable

    def test_a_north_facing_engine(self):
        self.assertEqual(steam_port_tiles([e("steam-engine", 0.5, 0.5)]), {(0.5, -2.5), (0.5, 3.5)})


class WaterRoute(unittest.TestCase):
    def test_the_pump_pipe_keeps_off_every_steam_port(self):
        with tempfile.TemporaryDirectory() as root:
            route = builder(Path(root)).route(SOURCE, WATER_PORT, "pipe", BANK + [PUMP])
        self.assertTrue(route["ok"], route)
        tiles = {(p["x"], p["y"]) for p in route["path"]}
        self.assertFalse(tiles & steam_port_tiles(BANK), route["path"])

    def test_a_belt_route_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            route = builder(Path(root)).route({"x": -9.5, "y": 49.5}, {"x": -9.5, "y": 53.5}, "transport-belt", BANK)
        self.assertTrue(route["ok"], route)
        self.assertIn({"x": -9.5, "y": 51.5}, route["path"])


if __name__ == "__main__":
    unittest.main()
