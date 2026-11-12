"""New mining banks exit toward their consumer on the site master plan.

The acceptance geometry copies seed 2150100594 (the live map where every
east-facing bank exit was blocked): ore patches as row runs, the early burner
hardware that stands between the patches (the old electric banks are left
out: they are what is being re-planned) and the master plan's mining zones,
smelting entry ports and feeder corridors. Nothing here reads ``runtime/``.

The exit is chosen by the real native selector (``MINING_EXIT_LUA`` inside
``plan_offore_exit``) running in lupa against a fake surface built from the
same map. ``plan_connection`` itself needs a live game for its corridor
survey, so the route is checked with the real router ``route_trunks`` on the
map-derived blocked tiles, under its unchanged turn/detour limits and the
search box ``plan_connection`` would use (``guided_bounds`` with the feeder
corridor as guide).
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.mining_layout_policy import (
    FLOWS,
    VECTORS,
    candidate_sites,
    layout_contract,
    make_policy,
    oriented_pair_core,
    prepare_extension,
    prepare_new_bank,
    validate_extension_contract,
    validate_policy,
)
from factorio_agent.mining_site import entry_tile, plan_exit, site_exit
from factorio_agent.resource_policy import (
    annotate_mining_exit,
    exit_directions,
    mining_exit_candidates,
    validate_annotations,
)
from factorio_agent.routing import route_trunks
from factorio_agent.site.geometry import Rect, plan_rect
from factorio_agent.site.planzones import Corridor, Port, Segment, Zone
from factorio_agent.site.worldmap import Feature, WorldMap
from factorio_agent.transport import guided_bounds, plan_connection

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


# Row runs "y:x0:x1" (tiles x0 <= x < x1) of seed 2150100594 around the start.
LIVE_ORE = {
    "coal": (
        "4:-74:-72 5:-75:-71 6:-76:-69 7:-81:-66 8:-82:-65 9:-83:-65 10:-83:-65 11:-83:-64 12:-83:-63 "
        "13:-82:-61 14:-82:-61 15:-81:-61 16:-81:-61 17:-81:-60 18:-80:-60 19:-80:-60 20:-79:-61 21:-78:-61 "
        "22:-78:-61 23:-78:-62 24:-77:-63 25:-74:-66 26:-71:-68 "
    ),
    "copper-ore": (
        "14:-44:-39 15:-45:-38 16:-45:-38 17:-45:-37 18:-45:-34 18:-33:-31 19:-46:-28 20:-47:-28 21:-49:-27 "
        "22:-51:-26 23:-52:-25 24:-52:-25 25:-52:-24 26:-51:-24 27:-50:-24 28:-50:-24 29:-50:-24 30:-50:-23 "
        "31:-50:-23 32:-49:-23 33:-47:-23 34:-46:-23 35:-45:-23 36:-45:-24 37:-44:-25 38:-44:-25 39:-43:-25 "
        "40:-42:-25 41:-41:-25 42:-41:-26 43:-41:-28 44:-41:-30 45:-40:-31 "
    ),
    "iron-ore": (
        "39:11:14 40:10:15 41:10:15 42:9:15 43:-2:0 43:9:15 44:-2:4 44:9:14 45:-2:6 45:8:14 46:-2:15 47:-2:18 "
        "48:-2:19 49:-2:20 50:-9:-8 50:-3:21 51:-9:-6 51:-5:22 52:-9:22 53:-10:22 54:-11:22 55:-11:-6 "
        "55:-5:21 56:-11:-7 56:-3:20 57:-11:-9 57:-3:19 58:-3:18 59:-3:18 60:-6:17 61:-7:17 62:-7:17 63:-7:17 "
        "64:-6:17 65:-6:17 66:-6:17 67:-6:18 68:-6:-3 68:-2:18 69:1:5 69:13:17 70:2:4 71:2:4 72:2:5 73:2:5 "
        "74:3:4 "
    ),
}
# Mean live amounts per tile.
LIVE_AMOUNT = {"coal": 1095, "copper-ore": 774, "iron-ore": 376}
# Early burner hardware (belts, burner drills, furnaces, chests) "x0:y0:x1:y1".
LIVE_ENTITIES = (
    "-79:0:-78:1 -78:0:-77:1 -77:0:-76:1 -76:0:-75:1 -75:0:-74:1 -74:0:-73:1 -73:0:-72:1 -72:0:-71:1 "
    "-71:0:-70:1 -70:0:-69:1 -69:0:-68:1 -68:0:-67:1 -67:0:-66:1 -66:0:-65:1 -65:0:-64:1 -64:0:-63:1 "
    "-64:12:-63:13 -64:13:-62:15 -64:15:-63:16 -64:16:-62:18 -63:0:-62:1 -62:0:-61:1 -62:14:-61:15 "
    "-62:15:-60:17 -62:17:-61:18 -62:18:-60:20 -61:0:-60:1 -61:20:-59:22 -60:0:-59:1 -59:0:-58:1 "
    "-59:20:-58:21 -59:21:-58:22 -58:0:-57:1 -58:20:-57:21 -58:21:-57:22 -58:22:-57:23 -57:0:-56:1 "
    "-57:22:-56:23 -56:0:-55:1 -56:1:-55:2 -56:2:-55:3 -56:3:-55:4 -56:4:-55:5 -56:5:-55:6 -56:6:-55:7 "
    "-56:7:-55:8 -56:8:-55:9 -56:9:-55:10 -56:10:-55:11 -56:11:-55:12 -56:12:-55:13 -56:13:-55:14 "
    "-56:14:-55:15 -56:15:-55:16 -56:16:-55:17 -56:17:-55:18 -56:18:-55:19 -56:19:-55:20 -56:20:-55:21 "
    "-56:21:-55:22 -56:22:-55:23 -27:21:-25:23 -27:23:-25:25 0:43:2:45 0:45:2:47 2:43:4:45 2:45:4:47 "
    "4:44:6:46 4:46:6:48 11:39:13:41 11:41:13:43 13:39:15:41 13:41:15:43 "
)
EXPECTED_FACING = {"coal": 8, "copper-ore": 8, "iron-ore": 12}


def live_world():
    world = WorldMap()
    world.mark_surveyed(Rect(-128, -32, 64, 128))
    for name, runs in LIVE_ORE.items():
        for run in runs.split():
            y, x0, x1 = map(int, run.split(":"))
            for x in range(x0, x1):
                world.set_ore(x, y, name, LIVE_AMOUNT[name])
    for item in LIVE_ENTITIES.split():
        world.add_feature(Feature("entity", "burner-hardware", Rect(*map(int, item.split(":")))))
    return world


def live_plan():
    """The live master plan's smelting zone, primary mining zones and feeder corridors."""

    def zone(kind, name, rect, side, ports):
        return Zone(kind, name, Rect(*rect), "", "", side, tuple(Port(*p) for p in ports))

    zones = (
        Zone(
            "smelting",
            "smelting",
            Rect(-88, 52, -24, 100),
            "N",
            "N",
            "S",
            (
                Port("coal", "in", "N", (-72.0, 52.0)),
                Port("copper-ore", "in", "N", (-40.0, 52.0)),
                Port("iron-ore", "in", "E", (-24.0, 78.0)),
            ),
        ),
        zone("mining", "mining:coal", (-85, 2, -58, 29), "S", [("coal", "out", "S", (-72.0, 29.0))]),
        zone("mining", "mining:copper-ore", (-58, 8, -17, 52), "S", [("copper-ore", "out", "S", (-40.0, 52.0))]),
        zone("mining", "mining:iron-ore", (-17, 33, 28, 81), "W", [("iron-ore", "out", "W", (-17.0, 78.0))]),
        zone("outpost", "mining:iron-ore:2", (202, -202, 241, -165), "", []),
    )

    def feeder(material, source, side_from, side_to, segments):
        return Corridor(
            f"corridor:{material}->smelting",
            "feeder",
            material,
            source,
            "smelting",
            4,
            side_from,
            side_to,
            tuple(Segment(Rect(*r), d) for r, d in segments),
        )

    corridors = (
        feeder("iron-ore", "mining:iron-ore", "W", "E", [((-24, 76, -17, 80), "W")]),
        feeder("copper-ore", "mining:copper-ore", "S", "N", []),
        feeder("coal", "mining:coal", "S", "N", [((-74, 29, -70, 52), "S")]),
    )

    class Plan:
        def zone(self, name):
            return next((z for z in zones if z.name == name), None)

        def corridor(self, name):
            return next((c for c in corridors if c.name == name), None)

    plan = Plan()
    plan.zones = zones
    plan.corridors = corridors
    return plan


def snapshot(world, resource, area):
    """What ``survey_patch`` returns for ``area`` on ``world`` (every resource tile)."""
    tiles = []
    for (x, y), (name, amount) in world.ore_tiles().items():
        if area[0][0] <= x + 0.5 < area[1][0] and area[0][1] <= y + 0.5 < area[1][1]:
            tiles.append(
                {
                    "name": name,
                    "position": {"x": x + 0.5, "y": y + 0.5},
                    "amount": amount,
                    "world_id": "w",
                    "surface_index": 1,
                }
            )
    return dict(
        ok=True,
        tick=100,
        world_id="w",
        catalog_fingerprint="c",
        resource=resource,
        area=area,
        tiles=tiles,
        surface_index=1,
        native=dict(
            mining_speed=0.5, mining_radius=2.49, mining_time=1, belt_speed=0.03125, force_productivity_bonus=0.0
        ),
    )


def patch_seed(world, resource):
    """Centre tile of the resource patch, as ``obs["resources"]`` reports it."""
    tiles = [t for t, (name, _) in world.ore_tiles().items() if name == resource]
    return {"x": sum(x for x, _ in tiles) // len(tiles) + 0.5, "y": sum(y for _, y in tiles) // len(tiles) + 0.5}


def catalog():
    return SimpleNamespace(
        fingerprint="c",
        entities={r: {"mining_products": [{"name": r, "amount": 1, "type": "item"}]} for r in LIVE_AMOUNT},
    )


def blocked_tiles(world, entities):
    """Router obstacles: every ore tile, built hardware and the new bank's non-belt entities."""
    blocked = {(x + 0.5, y + 0.5) for x, y in world.ore_tiles()}
    for feature in world.features("entity"):
        blocked.update((x + 0.5, y + 0.5) for x, y in feature.rect.tiles())
    for e in entities:
        if e["name"] != "transport-belt":
            blocked.update((x + 0.5, y + 0.5) for x, y in plan_rect(e).tiles())
    return blocked


SURFACE_LUA = r"""
f={name="player"};a=nil;d={world_id="w"};game={tick=100}
defines={build_check_type={manual_ghost=1}}
prototypes={entity={["transport-belt"]={collision_mask={layers={object=true}}}}}
local neutral={name="neutral"}
local ore={};local hardware={}
function add_ore(x,y,name,amount)ore[x..":"..y]={name=name,type="resource",position={x=x,y=y},amount=amount,force=neutral}end
function add_hardware(x0,y0,x1,y1,unit)
 hardware[#hardware+1]={name="burner-hardware",type="container",unit_number=unit,force=f,
  position={x=(x0+x1)/2,y=(y0+y1)/2},box={x0,y0,x1,y1},prototype={collision_mask={colliding_with_tiles_only=false}}}
end
function target(p,name)return nil end
s={}
function s.find_entities_filtered(q)
 local rows={};local area=q.area
 if q.type==nil or q.type=="resource" then
  for x=math.floor(area[1][1]),math.floor(area[2][1])do for y=math.floor(area[1][2]),math.floor(area[2][2])do
   local e=ore[(x+.5)..":"..(y+.5)]
   if e and x+.5>area[1][1]and x+.5<area[2][1]and y+.5>area[1][2]and y+.5<area[2][2]then rows[#rows+1]=e end
  end end
 end
 if q.type==nil then
  for _,e in ipairs(hardware)do local b=e.box
   if b[1]<area[2][1]and area[1][1]<b[3]and b[2]<area[2][2]and area[1][2]<b[4]then rows[#rows+1]=e end
  end
 end
 return rows
end
function s.can_place_entity(q)
 local p=q.position
 for _,e in ipairs(s.find_entities_filtered{area={{p.x-.4,p.y-.4},{p.x+.4,p.y+.4}}})do
  if e.type~="resource" then return false end
 end
 return true
end
function s.get_tile(p)return {collides_with=function()return false end}end
"""


def to_lua(lua, value):
    if isinstance(value, dict):
        return lua.table_from({k: to_lua(lua, v) for k, v in value.items()})
    if isinstance(value, list):
        return lua.table_from([to_lua(lua, v) for v in value])
    return value


def to_python(value):
    if not hasattr(value, "items"):
        return value
    rows = dict(value.items())
    if rows and set(rows) == set(range(1, len(rows) + 1)):
        return [to_python(rows[i]) for i in range(1, len(rows) + 1)]
    return {k: to_python(v) for k, v in rows.items()}


class LuaGame:
    """Runs query bodies in lupa (joined onto one line, as the RCON client does)."""

    def __init__(self, setup):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("helpers={}")
        self.lua.globals().helpers.json_to_table = lambda raw: to_lua(self.lua, json.loads(raw))
        self.lua.execute(setup)
        self.queries = 0

    def query(self, body):
        self.queries += 1
        return to_python(self.lua.execute("return(function()" + " ".join(body.splitlines()) + " end)()"))


class LuaSurfaceGame(LuaGame):
    """A surface built from a WorldMap: its ore and built hardware."""

    def __init__(self, world):
        super().__init__(SURFACE_LUA)
        add_ore = self.lua.globals().add_ore
        for (x, y), (name, amount) in world.ore_tiles().items():
            add_ore(x + 0.5, y + 0.5, name, amount)
        add_hardware = self.lua.globals().add_hardware
        for unit, feature in enumerate(world.features("entity"), start=1000):
            r = feature.rect
            add_hardware(r.x0 + 0.1, r.y0 + 0.1, r.x1 - 0.1, r.y1 - 0.1, unit)


def map_preflight(world):
    """A preflight that rejects any planned tile on built hardware (the live check's collision part)."""

    def preflight(game, builder, obs, entities, **_):
        for e in entities:
            for x, y in plan_rect(e).tiles():
                if any(f.kind == "entity" for f in world.features_at(x, y)):
                    return {"ok": False, "reason": "terrain_or_entity_collision", "position": [x, y]}
        return {"ok": True, "clear_obstacles": []}

    return preflight


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class LiveGeometryAcceptanceTests(unittest.TestCase):
    """Seed 2150100594: coal and copper banks exit south, iron west, each routable to smelting."""

    def setUp(self):
        self.world = live_world()
        self.plan = live_plan()
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(catalog=catalog(), _occupied_by_plan=geometry._occupied_by_plan)

    def plan_bank(self, resource):
        record = plan_exit(self.plan, resource)
        seed = patch_seed(self.world, resource)
        obs = {
            "ok": True,
            "world_id": "w",
            "tick": 100,
            "entities": [],
            "resources": {resource: {"position": seed}},
        }
        game = LuaSurfaceGame(self.world)

        def survey(planning, obs, catalog, resource, area, **_):
            return snapshot(self.world, resource, area)

        with (
            patch("factorio_agent.mining_layout_policy.survey_patch", side_effect=survey),
            patch("factorio_agent.mining_layout_policy.preflight", side_effect=map_preflight(self.world)),
            patch(
                "factorio_agent.mining_layout_policy.resource_survey",
                return_value={"ok": True, "rate_per_drill": 30, "unique_remaining_resource": 5000},
            ),
            patch(
                "factorio_agent.mining_layout_policy.power_connection",
                return_value={"ok": True, "entities": [], "wire_hops": 1},
            ),
        ):
            bank = prepare_new_bank(game, self.builder, obs, resource, 4, [], site_exit=record)
        return record, bank

    def test_new_banks_exit_toward_smelting_and_route_with_unchanged_limits(self):
        report = {}
        for resource, facing in EXPECTED_FACING.items():
            with self.subTest(resource=resource):
                record, bank = self.plan_bank(resource)
                self.assertTrue(bank["ok"], bank)
                plan = bank["plan"]
                port = plan["ports"][0]
                collector = plan["collector_port"]
                self.assertEqual((record["facing"], port["facing"], collector["facing"]), (facing,) * 3)
                self.assertEqual(bank["layout_contract"]["policy"]["flow_direction"], facing)
                self.assertEqual(plan["site_exit"]["exit_position"], port["position"])
                ux, uy = VECTORS[facing]
                ore = self.world.ore_tiles()

                def tile(p):
                    return (math.floor(p["x"]), math.floor(p["y"]))

                # The exit belts leave the bank's own ore and end on the first clear tile.
                exit_cells = [e for e in plan["entities"] if e.get("mining_exit")]
                path = exit_cells[0]["mining_exit"]["path"]
                self.assertEqual(path[0], collector["position"])
                self.assertEqual(path[-1], port["position"])
                self.assertTrue(all(ore.get(tile(p), ("",))[0] == resource for p in path[:-1]))
                self.assertNotIn(tile(port["position"]), ore)
                validate_annotations(plan["entities"], self.builder)
                # Fringe: a clear run continues in the facing out of the patch zone (the feeder side).
                zone = self.plan.zone("mining:" + resource).rect
                x, y = port["position"]["x"], port["position"]["y"]
                run = 0
                while zone.x0 <= x < zone.x1 and zone.y0 <= y < zone.y1:
                    self.assertNotIn(tile({"x": x, "y": y}), ore)
                    self.assertFalse(self.world.features_at(math.floor(x), math.floor(y)))
                    x, y = x + ux, y + uy
                    run += 1
                # Route from the exit to the smelting entry port.
                destination, entry_facing = entry_tile(record["entry_port"])
                a, b = port["position"], destination
                bounds = {
                    "min_x": min(a["x"], b["x"]) - 16,
                    "max_x": max(a["x"], b["x"]) + 16,
                    "min_y": min(a["y"], b["y"]) - 16,
                    "max_y": max(a["y"], b["y"]) + 16,
                }
                guide = tuple(record["guide"]) if record["guide"] else None
                bounds = guided_bounds(bounds, guide)
                blocked = blocked_tiles(self.world, plan["entities"]) - {
                    (a["x"], a["y"]),
                    (b["x"], b["y"]),
                }
                route = route_trunks(
                    a,
                    b,
                    occupied=blocked,
                    bounds=bounds,
                    start_direction=port["facing"],
                    end_direction=entry_facing,
                    max_nodes=150000,
                )
                self.assertTrue(route["ok"], route.get("reason"))
                self.assertLessEqual(route["metrics"]["turns"], 6)
                self.assertLessEqual(route["metrics"]["detour_ratio"], 1.35)
                report[resource] = (port["position"], port["facing"], run, route["metrics"])
        self.assertEqual(set(report), set(EXPECTED_FACING))

    def test_without_a_plan_new_banks_stay_east(self):
        """Control: the same coal patch without a plan gives the historical EAST candidates."""
        seed = patch_seed(self.world, "coal")
        area = [[math.floor(seed[k]) - 48 for k in ("x", "y")], [math.floor(seed[k]) + 48 for k in ("x", "y")]]
        sites = candidate_sites(snapshot(self.world, "coal", area), make_policy("w", "c", "coal"), 2, [])
        self.assertTrue(sites)
        self.assertTrue(all(q["plan"]["ports"][0]["facing"] == 4 for q in sites))


class OrientedCoreTests(unittest.TestCase):
    def test_turned_rows_still_feed_both_lanes_of_their_collector(self):
        """Each row drops on its own lane of a collector flowing in the bank's facing.

        A drill's drop tile is two tiles in front of its centre (3 x 3 drill).
        "left" drills must lie on the left of the flow (north of EAST, east of
        SOUTH, south of WEST, west of NORTH) and "right" drills opposite.
        """
        for flow, facing in FLOWS.items():
            with self.subTest(flow=flow):
                core = oriented_pair_core({"x": 10.5, "y": -6.5}, 3, "coal", flow)
                ux, uy = VECTORS[facing]
                normal = (uy, -ux)
                belts = {(e["position"]["x"], e["position"]["y"]): e for e in core["entities"] if "belt" in e["name"]}
                self.assertEqual(core["ports"][0]["facing"], facing)
                self.assertEqual(core["ports"][0]["position"], {"x": 10.5, "y": -6.5})
                for side, sign in (("left", 1), ("right", -1)):
                    rows = core["producer_rows"][side]
                    self.assertEqual(len(rows), 3)
                    for drill in rows:
                        dx, dy = VECTORS[drill["direction"]]
                        drop = (drill["position"]["x"] + 2 * dx, drill["position"]["y"] + 2 * dy)
                        self.assertEqual(belts[drop]["direction"], facing)
                        offset = (drill["position"]["x"] - drop[0]) * normal[0] + (
                            drill["position"]["y"] - drop[1]
                        ) * normal[1]
                        self.assertEqual(math.copysign(1, offset), sign)
                # Growth is opposite the flow: one more pair adds cells behind the bank only.
                bigger = oriented_pair_core({"x": 10.5, "y": -6.5}, 4, "coal", flow)
                added = [e for e in bigger["entities"] if e not in core["entities"]]
                self.assertTrue(
                    all((e["position"]["x"] - 10.5) * ux + (e["position"]["y"] + 6.5) * uy < 0 for e in added)
                )

    @unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
    def test_native_lane_proofs_follow_the_collector_facing(self):
        """``lane_evidence`` and the capacity diagnostic accept a turned bank's real drops.

        Drills drop 1.85 tiles in front of their centre (the vanilla
        electric drill), so each lands on its own lane of the collector.
        """
        from factorio_agent.mining_layout_policy import diagnose_bank_capacity
        from factorio_agent.paired_mining import lane_evidence

        for flow in ("north_to_south", "east_to_west", "south_to_north"):
            with self.subTest(flow=flow):
                plan = oriented_pair_core({"x": 4.5, "y": 4.5}, 2, "coal", flow)
                plan["collector_port"] = deepcopy(plan["ports"][0])
                bank = {"plan": plan}
                game = LuaGame("""f={mining_drill_productivity_bonus=0};s={};game={tick=7};d={world_id="w"};entities={}
prototypes={entity={["electric-mining-drill"]={mining_speed=.5,mining_drill_radius=2.49},
 coal={mineable_properties={mining_time=1}},["transport-belt"]={belt_speed=.03125}}}
defines={entity_status={working=1}}
function pos(p)return p end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end
function s.find_entities_filtered()return {{name="coal",amount=500}}end
""")
                lua = game.lua
                add = lua.execute(
                    """return function(name,x,y,direction,unit,dx,dy)
 local e={name=name,position={x=x,y=y},direction=direction,unit_number=unit,force=f,surface=s,energy=1,
  quality={name="normal"},speed_bonus=0,productivity_bonus=0,status=1,
  is_connected_to_electric_network=function()return true end,
  drop_position={x=x+1.85*dx,y=y+1.85*dy},
  get_transport_line=function()return {get_contents=function()return {}end}end}
 entities[name..":"..x..":"..y]=e end"""
                )
                obs = {"ok": True, "world_id": "w", "entities": []}
                for unit, e in enumerate(plan["entities"], start=1):
                    dx, dy = VECTORS.get(e.get("direction", 0), (0, 0))
                    add(e["name"], e["position"]["x"], e["position"]["y"], e.get("direction", 0), unit, dx, dy)
                    obs["entities"].append(dict(e, unit_number=unit))
                proof = lane_evidence(game, obs, bank)
                self.assertTrue(proof["ok"], proof)
                capacity = diagnose_bank_capacity(game, obs, catalog(), bank)
                self.assertTrue(capacity["ok"], capacity)
                self.assertTrue(capacity["all_installed_drop_lanes_verified"])
                # A drill rotated away from its collector is caught by the turned check.
                lua.execute(
                    "for _,e in pairs(entities)do if e.name=='electric-mining-drill' then "
                    "e.drop_position={x=e.position.x+3,y=e.position.y+3} break end end"
                )
                self.assertFalse(lane_evidence(game, obs, bank)["ok"])

    def test_east_core_is_the_canonical_core_unchanged(self):
        from factorio_agent.paired_mining import mining_pair

        core = oriented_pair_core({"x": 20.5, "y": 30.5}, 2, "coal")
        canonical = mining_pair(18.5, 28.5, 2, "coal")
        self.assertEqual(core["entities"], canonical["entities"])
        self.assertEqual(core["ports"], canonical["ports"])


class OrientedContractTests(unittest.TestCase):
    def test_site_policy_is_explicit_and_old_policies_validate_unchanged(self):
        old = make_policy("w", "c", "coal")
        self.assertNotIn("orientation_source", old)
        site = make_policy("w", "c", "coal", flow="north_to_south", site_oriented=True)
        self.assertTrue(site["orientation_execution_supported"])
        self.assertEqual(site["orientation_source"], "site-plan")
        validate_policy(site, "w", "c", "coal")
        validate_policy(old, "w", "c", "coal")
        forged = dict(make_policy("w", "c", "coal", flow="north_to_south"), orientation_execution_supported=True)
        with self.assertRaises(ValueError):
            validate_policy(forged, "w", "c", "coal")

    def south_bank(self):
        policy = make_policy("w", "c", "coal", flow="north_to_south", site_oriented=True)
        plan = oriented_pair_core({"x": 0.5, "y": 0.5}, 1, "coal", "north_to_south")
        bank = dict(plan=plan, reserved_pair_slots=3, reserved_area=[[-3, -12], [4, 2]])
        bank["layout_contract"] = layout_contract(bank, policy)
        return bank

    def test_south_bank_extends_behind_its_collector_and_keeps_its_port(self):
        bank = self.south_bank()
        self.assertEqual(bank["layout_contract"]["output_access"][0], {"x": 0.5, "y": 1.5})
        bigger = oriented_pair_core({"x": 0.5, "y": 0.5}, 2, "coal", "north_to_south")
        self.assertTrue(validate_extension_contract(bank, bigger)["ok"])
        obs = {
            "ok": True,
            "world_id": "w",
            "entities": [dict(e, unit_number=i + 1) for i, e in enumerate(bank["plan"]["entities"])],
        }
        game = SimpleNamespace(
            query=lambda _: {"ok": True, "belt_speed": 0.03125, "force_productivity_bonus": 0.0},
        )
        builder = SimpleNamespace(catalog=catalog())
        with (
            patch("factorio_agent.mining_layout_policy.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch(
                "factorio_agent.mining_layout_policy.resource_survey",
                return_value={"ok": True, "rate_per_drill": 30, "unique_remaining_resource": 5000},
            ),
        ):
            ready = prepare_extension(game, builder, obs, bank, 2)
        self.assertTrue(ready["ok"], ready)
        self.assertEqual(ready["plan"]["ports"], bank["plan"]["ports"])
        drills = [e for e in ready["plan"]["entities"] if e["name"] == "electric-mining-drill"]
        self.assertEqual(len(drills), 4)
        self.assertTrue(all(e["position"]["y"] < 0.5 for e in drills))

    def test_old_east_only_policy_cannot_execute_a_turned_flow(self):
        bank = self.south_bank()
        bank["layout_contract"]["policy"] = make_policy("w", "c", "coal", flow="north_to_south")
        result = prepare_extension(
            SimpleNamespace(query=None), SimpleNamespace(catalog=catalog()), {"world_id": "w"}, bank, 2
        )
        self.assertFalse(result["ok"])


class OrientedExitMetadataTests(unittest.TestCase):
    def setUp(self):
        self.builder = FactoryBuilder.__new__(FactoryBuilder)

    def test_candidates_turn_with_the_facing_and_east_stays_historical(self):
        east = mining_exit_candidates({"x": 0.5, "y": 0.5})
        self.assertEqual([exit_directions(p) for p in east], [[4] * 12, [4] + [0] * 11, [4] + [8] * 11])
        west = mining_exit_candidates({"x": 0.5, "y": 0.5}, facing=12)
        self.assertEqual([exit_directions(p, 12) for p in west], [[12] * 12, [12] + [8] * 11, [12] + [0] * 11])
        south = mining_exit_candidates({"x": 0.5, "y": 0.5}, facing=8)
        self.assertEqual(south[0][3], {"x": 0.5, "y": 3.5})
        with self.assertRaises(ValueError):
            exit_directions(south[0])
        with self.assertRaises(ValueError):
            mining_exit_candidates({"x": 0.5, "y": 0.5}, facing=2)

    def test_south_exit_annotation_validates_and_rejects_forged_direction(self):
        path = mining_exit_candidates({"x": 0.5, "y": 0.5}, facing=8)[0][:5]
        es = [entity("transport-belt", p["x"], p["y"], 8) for p in path]
        annotate_mining_exit(es, "coal", path[0], 3, path=path, facing=8)
        validate_annotations(es, self.builder)
        self.assertEqual({e["mining_exit"]["facing"] for e in es if e.get("mining_exit")}, {8})
        straight = [entity("transport-belt", 0.5, 0.5 + i, 8) for i in range(6)]
        annotate_mining_exit(straight, "coal", {"x": 0.5, "y": 0.5}, 3, facing=8)
        validate_annotations(straight, self.builder)
        broken = deepcopy(es)
        broken[1]["direction"] = 4
        with self.assertRaises(ValueError):
            validate_annotations(broken, self.builder)

    def test_transport_accepts_a_registered_south_mining_source(self):
        path = mining_exit_candidates({"x": 0.5, "y": 0.5}, facing=8)[0][:6]
        survey = {"ok": True, "blocked": [], "clearable": [], "max_span": 5, "exit_prefix": path, "exit_length": 4}
        game = SimpleNamespace(query=lambda _: survey)
        builder = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan, can_place=lambda _: {"ok": True})
        source = {"item": "coal", "position": path[0], "facing": 8, "mining_resource": "coal"}
        destination = {"item": "coal", "position": {"x": 12.5, "y": 20.5}, "facing": 8}
        result = plan_connection(game, builder, {"world_id": "w", "entities": []}, source, destination)
        self.assertTrue(result["ok"], result)
        validate_annotations(result["entities"], self.builder)
        tagged = [e for e in result["entities"] if e.get("mining_exit")]
        self.assertEqual({e["mining_exit"]["facing"] for e in tagged}, {8})
        bad = dict(source, facing=3)
        self.assertEqual(
            plan_connection(game, builder, {"world_id": "w", "entities": []}, bad, destination)["reason"],
            "invalid_registered_mining_exit_source",
        )


class SiteExitRecordTests(unittest.TestCase):
    def test_plan_exit_reads_side_corridor_entry_and_guide(self):
        plan = live_plan()
        coal = plan_exit(plan, "coal")
        self.assertEqual((coal["side"], coal["flow"], coal["facing"]), ("S", "north_to_south", 8))
        self.assertEqual(coal["corridor_start"], [-72.0, 29.0])
        self.assertEqual(coal["guide"], [-74, 29, -70, 52])
        self.assertEqual(coal["entry_port"], {"zone": "smelting", "side": "N", "point": [-72.0, 52.0]})
        self.assertIsNone(plan_exit(plan, "copper-ore")["guide"])
        self.assertEqual(plan_exit(plan, "iron-ore")["flow"], "east_to_west")
        self.assertIsNone(plan_exit(plan, "stone"))
        self.assertEqual(entry_tile(coal["entry_port"]), ({"x": -72.5, "y": 52.5}, 8))
        self.assertEqual(entry_tile(plan_exit(plan, "iron-ore")["entry_port"]), ({"x": -24.5, "y": 77.5}, 12))

    def test_site_exit_falls_back_to_none_without_a_usable_service(self):
        self.assertIsNone(site_exit(None, "w", "coal"))

        class Broken:
            def plan(self, world_id):
                raise RuntimeError("no survey")

        self.assertIsNone(site_exit(Broken(), "w", "coal"))
        good = SimpleNamespace(plan=lambda world_id: live_plan())
        self.assertEqual(site_exit(good, "w", "coal")["facing"], 8)

    def test_outposts_pass_the_site_exit_only_when_the_plan_has_one(self):
        import tempfile
        from pathlib import Path

        from factorio_agent.outposts import MiningOutposts

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)))
        site = SimpleNamespace(plan=lambda world_id: live_plan())
        outposts = MiningOutposts(game, None, None, site=site)
        obs = {"world_id": "w"}
        with patch("factorio_agent.paired_mining.prepare_new_bank", return_value={"ok": True}) as prepare:
            outposts.prepare(obs, "coal", 2)
            self.assertEqual(prepare.call_args.kwargs["site_exit"]["facing"], 8)
            outposts.prepare(obs, "stone", 2)
            self.assertEqual(prepare.call_args.kwargs, {})
        plain = MiningOutposts(game, None, None)
        with patch("factorio_agent.paired_mining.prepare_new_bank", return_value={"ok": True}) as prepare:
            plain.prepare(obs, "coal", 2)
            self.assertEqual(prepare.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
