"""Power-link poles of fixed pole chains and supply-cell sites (fleet aud-1114-0526).

* seed 1723389324: the supply cell's power link put a pole at (-11.5, 38.5), inside the startup
  science cell's second lab slot (lab at (-11.5, 37.5)); the cell now starts with two labs
  (``industry.start_floor``), the lab was built at tick 43966 after the supply cell was planned at
  ~38647, and the supply cell blocked ``reserved block placement is obstructed`` for 30 minutes.
  Lab slots (and the west extension) are reserved growth: planned hardware for every other planner.
* seed 1032498969: the startup science cell's grid chain ran over iron ore at (33.5, 12.5) and
  (33.5, 10.5) outside its reserved area (the survey checked ore only inside the area); the
  builder refused it (``new_entity_over_resource_or_mining_reservation``) and research stopped.
  Every fixed chain pole is now checked natively for ore and mining reservations (``LINK_POLE_LUA``).
* seed 4058652641: the supply cell's 31 candidates within 48 tiles of a lakeside power pole all
  stood in the lake (``no_clear_supply_cell_site``); the straight links of the few dry sites crossed
  plan zones. The site map now drops water/ore origins before the survey, the reach is 80 tiles,
  and the nearest link-blocked sites get a pole-hop link that keeps out of belt corridors.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent import supply_cell as sc
from factorio_agent.board import (
    LINK_POLE_LUA,
    Board,
    _plan,
    entity,
    hardware_conflicts,
    lab_slot_entities,
    planned_unbuilt_hardware,
    reserved_growth_hardware,
    startup_science_cell,
)
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.power_grid import plan_connection

from test_v2_supply_cell import ENABLED, catalog

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)

try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional native test dependency
    LuaRuntime = None


def science_block(x, y, machines=2):
    base = startup_science_cell(x, y, science_machines=machines)
    extra = {k: base[k] for k in ("science_machines", "cell_origin") if k in base}
    return _plan(
        base["entities"],
        base["ports"],
        template="startup_science_cell",
        layout_policy="pcb-block-v1",
        reserved_area=[[x - 8.5, y - 8.5], [x + 7.5, y + 7.5]],
        **extra,
    )


class LabSlotsArePlannedHardwareTests(unittest.TestCase):
    def test_lab_slots_and_the_extension_are_reserved_growth_until_built(self):
        hub = science_block(-11.5 - 4, 37.5)  # slots at origin + (4, -3), (4, 0), (4, 3)
        slots = [s["lab"]["position"] for s in lab_slot_entities(hub)]
        self.assertIn({"x": -11.5, "y": 37.5}, slots)
        tiles = planned_unbuilt_hardware(GEOMETRY, {"entities": []}, {"startup_science": hub})
        self.assertEqual(tiles[(-11.5, 38.5)], {"block": "startup_science", "name": "lab", "position": slots[1]})
        # The supply cell's pole of seed 1723389324 now conflicts; a built lab drops out.
        pole = entity("small-electric-pole", -11.5, 38.5)
        self.assertTrue(hardware_conflicts(GEOMETRY, [pole], tiles))
        built = {"entities": [dict(entity("lab", -11.5, 37.5), unit_number=528)]}
        self.assertNotIn((-11.5, 38.5), planned_unbuilt_hardware(GEOMETRY, built, {"startup_science": hub}))
        # A one-machine cell also reserves its slot pole and the west extension.
        one = science_block(8.5, 8.5, machines=1)
        names = sorted({e["name"] for e in reserved_growth_hardware(one)})
        self.assertEqual(names, ["assembling-machine-1", "inserter", "lab", "small-electric-pole", "wooden-chest"])
        self.assertEqual(reserved_growth_hardware({"entities": [], "growth_entities": [pole]}), [pole])


class SupplyLinkAvoidsLabSlotTests(unittest.TestCase):
    def offered(self, block):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        c = catalog()
        board = {"world_id": "w", "catalog_fingerprint": c.fingerprint, "blocks": {"startup_science": block}}
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        payloads = []

        def query(body):
            plans = json.loads(json.loads(body.split("helpers.json_to_table(")[1].split(");local legal")[0]))
            payloads.append(plans)
            return {"ok": True, "legal": list(range(1, len(plans) + 1)), "clearable": {}}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(root)), query=query)
        builder = Mock()
        builder._occupied_by_plan = GEOMETRY._occupied_by_plan
        site = SimpleNamespace(world=lambda world_id=None: SimpleNamespace(zones=lambda: []))
        cell = sc.SupplyCell(game, None, builder, c, site=site)
        obs = {
            "world_id": "w",
            "tick": 1,
            "entities": [
                {"name": "steam-engine", "energy": 5, "electric_network_id": 7, "position": {"x": 0, "y": 0}},
                {"name": "small-electric-pole", "electric_network_id": 7, "position": {"x": 2.5, "y": 0.5}},
            ],
            "inventory": {},
            "enabled_recipes": ENABLED,
        }
        result = cell.prepare(obs, ["transport-belt"])
        self.assertTrue(result.get("ok"), result)
        return payloads[0], result

    def test_no_offered_link_pole_stands_on_a_lab_slot(self):
        # Only the cell's lab column, slots at (-2.5, -4.5), (-2.5, -1.5), (-2.5, 1.5): the nearest
        # site west of the pole (2.5, 0.5) links along y -1.5 with a pole at (-2.5, -1.5).
        block = {
            "template": "startup_science_cell",
            "science_machines": 2,
            "cell_origin": {"x": -6.5, "y": -1.5},
            "reserved_area": [[-4, -6], [-1, 4]],
            "entities": [],
        }
        slot_tiles = set().union(*[GEOMETRY._occupied_by_plan([s["lab"]]) for s in lab_slot_entities(block)])
        unreserved = dict(block, template="old_cell_without_slots")
        before, _ = self.offered(unreserved)
        self.assertTrue(  # the geometry does exercise the slot: a chain along y 0.5 hops onto (12.5, 0.5)
            any(GEOMETRY._occupied_by_plan([e]) & slot_tiles for plan in before for e in plan["entities"])
        )
        after, result = self.offered(block)
        for plan in after:
            for e in plan["entities"]:
                self.assertFalse(GEOMETRY._occupied_by_plan([e]) & slot_tiles, e)
        self.assertFalse(
            any(GEOMETRY._occupied_by_plan([e]) & slot_tiles for e in result["plan"]["entities"]),
        )


@unittest.skipUnless(LuaRuntime, "Lua runtime required")
class LinkPoleLuaTests(unittest.TestCase):
    def closed(self, entities, zones, *, ore=(), poles=()):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(
            "f={};ORE={};POLES={}\n"
            "s={count_entities_filtered=function(a) local n=0 for _,p in ipairs(ORE) do"
            " if p[1]>a.area[1][1] and p[1]<a.area[2][1] and p[2]>a.area[1][2] and p[2]<a.area[2][2] then n=n+1 end end"
            " return n end}\n"
            "function target(p,name) for _,q in ipairs(POLES) do if q[1]==p.x and q[2]==p.y then"
            " return {force=f,position={x=q[1],y=q[2]}} end end return nil end\n"
        )
        for x, y in ore:
            lua.execute(f"ORE[#ORE+1]={{{x},{y}}}")
        for x, y in poles:
            lua.execute(f"POLES[#POLES+1]={{{x},{y}}}")
        to_lua = lua.eval("function(text) return load('return '..text)() end")
        check = lua.execute(" ".join((LINK_POLE_LUA + "\nreturn link_closed").splitlines()))
        return check(to_lua(_lua(entities)), to_lua(_lua(zones)))

    def test_ore_and_mining_reservations_close_a_link_pole_an_existing_pole_is_adopted(self):
        chain = [entity("small-electric-pole", 33.5, 17.5), entity("small-electric-pole", 33.5, 12.5)]
        self.assertFalse(self.closed(chain, []))
        self.assertTrue(self.closed(chain, [], ore=[(33.5, 12.5)]))  # seed 1032498969
        self.assertFalse(self.closed(chain, [], ore=[(33.5, 12.5)], poles=[(33.5, 12.5)]))
        mine = [{"area": [[30, 10], [40, 14]], "resource": "iron-ore"}]
        self.assertTrue(self.closed(chain, mine))
        self.assertFalse(self.closed([entity("inserter", 33.5, 12.5)], mine, ore=[(33.5, 12.5)]))
        annotated = [dict(entity("small-electric-pole", 33.5, 12.5), mining_resource="iron-ore")]
        self.assertFalse(self.closed(annotated, mine, ore=[(33.5, 12.5)]))


def _lua(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={_lua(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "{" + ",".join(_lua(v) for v in value) + "}"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


class StartupCellLinkSurveyTests(unittest.TestCase):
    def test_the_survey_checks_link_poles_and_a_cell_left_only_with_ore_links_needs_power(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        mine = {
            "resource": "iron-ore",
            "reserved_area": [[100, 100], [120, 120]],
            "entities": [entity("electric-mining-drill", 110.5, 110.5)],
        }
        board = {"schema_version": 1, "world_id": "w", "catalog_fingerprint": "c", "blocks": {"iron-ore_mining": mine}}
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        bodies = []

        def query(body):
            bodies.append(body)
            plans = json.loads(json.loads(body.split("helpers.json_to_table(", 1)[1].split(");", 1)[0]))
            return {"ok": True, "legal": [], "roomy": [], "clearable": [], "ore_links": list(range(1, len(plans) + 1))}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=query)
        obs = {
            "world_id": "w",
            "entities": [
                {"name": "steam-engine", "position": {"x": 20.5, "y": 10.5}, "energy": 5, "electric_network_id": 1},
                {"name": "small-electric-pole", "position": {"x": 24.5, "y": 10.5}, "electric_network_id": 1},
            ],
        }
        result = Board(game, GEOMETRY, SimpleNamespace(fingerprint="c")).reserve_startup_science(obs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_clear_reserved_science_slot")
        self.assertEqual(result["plan_needs"]["kind"], "power")
        self.assertIn("link_closed(p.entities,zones)", bodies[0])
        self.assertIn('\\"resource\\":\\"iron-ore\\"', bodies[0])  # the mining reservation is passed


class FakeWorld:
    """A site map: ``water`` tiles, ``ore`` tiles and named zones."""

    def __init__(self, water=(), ore=(), zones=()):
        self.water, self._ore, self._zones = set(water), dict(ore), list(zones)

    def zones(self):
        return [(owner, SimpleNamespace(x0=r[0], y0=r[1], x1=r[2], y1=r[3])) for owner, r in self._zones]

    def ore_tiles(self):
        return dict(self._ore)

    def peek_terrain(self, x, y):
        return "water" if (x, y) in self.water else "land"


class SupplySiteLandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c = catalog()
        self.payloads = []
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)), query=self.query)
        self.builder = Mock()
        self.builder._occupied_by_plan = GEOMETRY._occupied_by_plan
        self.obs = {
            "world_id": "w",
            "tick": 1,
            "entities": [
                {"name": "steam-engine", "energy": 5, "electric_network_id": 7, "position": {"x": 0, "y": 0}},
                {"name": "small-electric-pole", "electric_network_id": 7, "position": {"x": 0.5, "y": 0.5}},
            ],
            "inventory": {},
            "enabled_recipes": ENABLED,
        }

    def query(self, body):
        plans = json.loads(json.loads(body.split("helpers.json_to_table(")[1].split(");local legal")[0]))
        self.payloads.append(plans)
        return {"ok": True, "legal": list(range(1, len(plans) + 1)), "clearable": {}}

    def prepare(self, world):
        cell = sc.SupplyCell(self.game, None, self.builder, self.c, site=SimpleNamespace(world=lambda w=None: world))
        return cell.prepare(self.obs, ["transport-belt"])

    def test_a_lake_around_the_power_pole_is_skipped_on_the_site_map(self):
        # Seed 4058652641: every origin within 48 tiles of the plant pole stood in the lake.
        # A one-tile dry causeway (tile row y 0) crosses the lake: links run along it, no cell fits on it.
        lake = {(x, y) for x in range(-50, 50) for y in range(-50, 50) if y != 0}
        result = self.prepare(FakeWorld(water=lake))
        self.assertTrue(result["ok"], result)
        for plan in self.payloads[0]:
            for e in plan["entities"]:
                if e["name"] != "small-electric-pole":
                    self.assertFalse({(int(x // 1), int(y // 1)) for x, y in GEOMETRY._occupied_by_plan([e])} & lake, e)
        nearest = min(
            abs(e["position"]["x"]) + abs(e["position"]["y"])
            for e in result["plan"]["entities"]
            if e["name"] != "small-electric-pole"
        )
        self.assertGreater(nearest, 45)

    def test_ore_inside_a_cell_area_is_skipped_and_every_rejection_is_counted(self):
        ore = {(x, y): ("stone", 100) for x in range(-200, 200) for y in range(-200, 200)}
        result = self.prepare(FakeWorld(ore=ore))
        self.assertEqual(result["reason"], "no_supply_cell_site_off_ore_and_plan_zones")
        self.assertEqual(result["rejected_by"], {"ore_in_area": 441})
        self.assertEqual(result["plan_needs"]["kind"], "site")
        self.assertEqual(self.payloads, [])

    def test_link_blocked_sites_get_a_pole_hop_link_that_keeps_out_of_corridors(self):
        # Every site east of a belt corridor; every straight chain crosses the corridor or a zone.
        zones = [("power", [-400, -400, 2, 400]), ("corridor:power->smelting", [2, -400, 6, 400])]
        calls = []

        def hop(game, builder, obs, destination, reserved=(), *, keep_out=()):
            calls.append((dict(destination), [list(map(list, a)) for a in keep_out]))
            return {
                "ok": True,
                "entities": [entity("small-electric-pole", 7.5, 0.5), entity("small-electric-pole", **destination)],
            }

        with patch.object(sc, "plan_connection", side_effect=hop):
            result = self.prepare(FakeWorld(zones=zones))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), sc.HOP_LINK_SITES)
        self.assertEqual(calls[0][1], [[[2, -400], [6, 400]]])  # only the corridor is a hard zone for hops
        poles = [e["position"] for e in result["plan"]["entities"] if e["name"] == "small-electric-pole"]
        self.assertIn({"x": 7.5, "y": 0.5}, poles)

    def test_the_late_zone_relaxation_opens_district_and_rail_zones(self):
        from factorio_agent.remedy import applied, ladder

        zones = [("districts:left", [-400, -400, 400, 400])]
        self.assertEqual(self.prepare(FakeWorld(zones=zones))["reason"], "no_supply_cell_site_off_ore_and_plan_zones")
        with applied({"supply_cell_late_zones": True}):
            self.assertTrue(self.prepare(FakeWorld(zones=zones))["ok"])
        self.assertIn("relax:supply_cell_late_zones", [r.name for r in ladder("supply_cell")])


class HopKeepOutTests(unittest.TestCase):
    def test_no_hop_pole_touches_a_keep_out_area(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {}}), encoding="utf-8")

        def query(lua):
            payload = lua.split("helpers.json_to_table(", 1)[1].split(");local clear", 1)[0]
            return {"ok": True, "clear": list(range(1, len(json.loads(json.loads(payload))["points"]) + 1))}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=query)
        builder = SimpleNamespace(_occupied_by_plan=lambda es: {(e["position"]["x"], e["position"]["y"]) for e in es})
        engine = {"name": "steam-engine", "position": {"x": -10, "y": 0}, "electric_network_id": 1, "energy": 1}
        pole = {"name": "small-electric-pole", "position": {"x": 0.5, "y": 0.5}, "electric_network_id": 1}
        obs = {"world_id": "w", "entities": [engine, pole]}
        corridor = [[10, -100], [14, 100]]
        straight = plan_connection(game, builder, obs, {"x": 24.5, "y": 0.5})
        self.assertTrue(any(10 < e["position"]["x"] < 14 for e in straight["entities"]))
        around = plan_connection(game, builder, obs, {"x": 24.5, "y": 0.5}, keep_out=[corridor])
        self.assertTrue(around["ok"], around)
        for e in around["entities"]:
            self.assertFalse(9.5 < e["position"]["x"] < 14.5, e)


if __name__ == "__main__":
    unittest.main()
