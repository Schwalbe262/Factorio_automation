"""Temporary hand-fed supply cell: design, site, use (ensure_item, feeding), planner, retirement."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent import supply_cell as sc
from factorio_agent.construction_supply import HANDCRAFT_LIMIT, V2Bootstrap, _waiting_pays
from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.progression import autopilot_nodes
from v2_blocks_fixture import _recipe, fake_catalog

BELT, INS = "transport-belt", "inserter"
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def catalog(speed=0.5):
    data = fake_catalog().to_dict()
    data["recipes"]["transport-belt"] = _recipe(0.5, {"iron-plate": 1, "iron-gear-wheel": 1}, {"transport-belt": 2})
    data["recipes"]["wooden-chest"] = _recipe(0.5, {"wood": 2}, {"wooden-chest": 1})
    data["recipes"]["pipe"] = _recipe(0.5, {"iron-plate": 1}, {"pipe": 1})
    data["recipes"]["assembling-machine-1"] = _recipe(
        0.5, {"iron-plate": 9, "iron-gear-wheel": 5, "electronic-circuit": 3}, {"assembling-machine-1": 1}
    )
    data["entities"]["assembling-machine-1"]["crafting_speed"] = speed
    return WorldCatalog.from_dict(data)


ENABLED = {
    name: True
    for name in (
        "assembling-machine-1",
        "iron-gear-wheel",
        "transport-belt",
        "inserter",
        "electronic-circuit",
        "copper-cable",
        "wooden-chest",
        "small-electric-pole",
    )
}


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.c = catalog()

    def test_recipe_tree_from_the_catalog(self):
        belt = sc.recipe_tree(self.c, BELT)
        self.assertEqual((belt["recipe"], belt["crafts"], belt["raw"]), (BELT, 0.5, {"iron-plate": 0.5}))
        self.assertEqual([(n["item"], n["raw"]) for n in belt["children"]], [("iron-gear-wheel", {"iron-plate": 1.0})])
        ins = sc.recipe_tree(self.c, INS)
        items = sorted(n["item"] for n in sc._nodes(ins))
        self.assertEqual(items, ["copper-cable", "electronic-circuit", "inserter", "iron-gear-wheel"])
        cable = next(n for n in sc._nodes(ins) if n["item"] == "copper-cable")
        self.assertEqual(cable["raw"], {"copper-plate": 1.5})
        with self.assertRaises(ValueError):
            sc.recipe_tree(self.c, INS, enabled={"inserter"})  # locked intermediates

    def test_layout_for_belt_and_inserter(self):
        d = sc.layout(self.c, [BELT, INS], chest="wooden-chest")
        names = [e["name"] for e in d["entities"]]
        self.assertEqual(names.count("assembling-machine-1"), 6)
        self.assertEqual(names.count("inserter"), 12)
        self.assertEqual(names.count("wooden-chest"), 8)
        tiles = [GEOMETRY._occupied_by_plan([e]) for e in d["entities"]]
        self.assertEqual(sum(map(len, tiles)), len(set().union(*tiles)))  # no overlap
        poles = [sc._xy(e) for e in d["entities"] if e["name"] == "small-electric-pole"]
        for e in d["entities"]:
            if e["name"] in ("assembling-machine-1", "inserter"):
                self.assertTrue(any(sc._covers(self.c, p, e) for p in poles), e)
        outputs = [p for p in d["ports"] if p["direction"] == "output"]
        self.assertEqual([p["item"] for p in outputs], [BELT, INS])
        inputs = {p["item"] for p in d["ports"] if p["direction"] == "input"}
        self.assertEqual(inputs, {"iron-plate", "copper-plate"})
        # every inserter faces a chest on its pickup side or a machine: pickup = position + direction vector
        by_tile = {t: e["name"] for e in d["entities"] for t in GEOMETRY._occupied_by_plan([e])}
        for e in d["entities"]:
            if e["name"] == "inserter":
                vx, vy = sc.VECTORS[e["direction"]]
                pick = (e["position"]["x"] + vx, e["position"]["y"] + vy)
                drop = (e["position"]["x"] - vx, e["position"]["y"] - vy)
                self.assertIn(by_tile.get(pick), ("wooden-chest", "assembling-machine-1"))
                self.assertIn(by_tile.get(drop), ("wooden-chest", "assembling-machine-1"))
                self.assertNotEqual(by_tile.get(pick), by_tile.get(drop))


class RuleTests(unittest.TestCase):
    def test_qualifying_products_and_reasons(self):
        c = catalog()
        self.assertTrue(sc.evaluate(c, BELT, 464)["qualifies"])
        self.assertFalse(sc.evaluate(c, BELT, HANDCRAFT_LIMIT)["qualifies"])
        small = sc.evaluate(c, INS, 16)
        self.assertFalse(small["qualifies"])
        self.assertIn("cheaper", small["reason"])
        self.assertTrue(sc.evaluate(c, INS, 64)["qualifies"])
        slow = sc.evaluate(catalog(speed=0.05), BELT, 464)
        self.assertFalse(slow["qualifies"])
        self.assertIn("wait factor", slow["reason"])
        self.assertIn("no recipe", sc.evaluate(c, "wood", 50)["reason"])

    def test_time_rule(self):
        row = {"stock": 0, "wait_seconds_per_unit": 1.2, "hand_seconds_per_unit": 0.5, "wait_factor": 3.0}
        self.assertTrue(_waiting_pays(row, 40))
        self.assertFalse(_waiting_pays(dict(row, wait_seconds_per_unit=2.0), 40))
        self.assertTrue(_waiting_pays(dict(row, wait_seconds_per_unit=2.0, stock=30), 40))
        self.assertTrue(_waiting_pays({"status": "producing"}, 40))  # produce rows: unchanged

    def test_parameters(self):
        sc.validate_parameters({"products": [BELT]})
        sc.validate_parameters({"retire": True})
        for bad in ({}, {"products": []}, {"products": [BELT, BELT]}, {"retire": False}, {"products": [BELT], "x": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sc.validate_parameters(bad)


class FakeGame:
    def __init__(self, root, legal=None):
        self.cfg = SimpleNamespace(runtime_dir=str(root))
        self.legal = legal
        self.payloads = []

    def query(self, body):
        if "supports_bar" in body:
            return {"ok": True, "slots": sc.OUTPUT_STACKS, "size": 16}
        plans = json.loads(json.loads(body.split("helpers.json_to_table(")[1].split(");local legal")[0]))
        self.payloads.append(plans)
        legal = list(range(1, len(plans) + 1)) if self.legal is None else self.legal
        return {"ok": True, "legal": legal, "clearable": {}}


def zone_world(rects):
    rect = lambda r: SimpleNamespace(x0=r[0], y0=r[1], x1=r[2], y1=r[3])
    world = SimpleNamespace(zones=lambda: [(f"z{i}", rect(r)) for i, r in enumerate(rects)])
    return SimpleNamespace(world=lambda world_id=None: world)


POWER = [
    {"name": "steam-engine", "energy": 5, "electric_network_id": 7, "position": {"x": 0, "y": 0}, "unit_number": 1},
    {"name": "small-electric-pole", "electric_network_id": 7, "position": {"x": 0.5, "y": 0.5}, "unit_number": 2},
]


class Cell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = catalog()
        self.game = FakeGame(self.root)
        self.builder = Mock()
        self.builder._occupied_by_plan = GEOMETRY._occupied_by_plan
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.bootstrap = V2Bootstrap(Mock(backend="assisted", cfg=self.game.cfg))
        self.bootstrap.catalog = self.c
        self.cell = sc.SupplyCell(self.game, self.bootstrap, self.builder, self.c, site=zone_world([]))

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, extra=(), inventory=None, tick=1000):
        return {
            "world_id": "w",
            "tick": tick,
            "entities": POWER + list(extra),
            "inventory": inventory or {},
            "enabled_recipes": ENABLED,
        }

    def built(self, products=(BELT,)):
        result = self.cell.step(self.obs(), {"products": list(products)})
        self.assertNotEqual(result.get("status"), "blocked", result)
        return self.cell.state()

    def live(self, state, *, stock=0, finished=1, inside=None):
        rows, unit = [], 100
        outputs = {(r["output"]["x"], r["output"]["y"]): r["item"] for r in state["products"]}
        for e in state["plan"]["entities"]:
            unit += 1
            row = dict(e, unit_number=unit)
            if e["name"] == "assembling-machine-1":
                row["products_finished"] = finished
            if e["name"] == "wooden-chest":
                key = (e["position"]["x"], e["position"]["y"])
                row["inventory"] = {outputs[key]: stock} if key in outputs else dict(inside or {})
            rows.append(row)
        return rows


class SiteTests(Cell):
    def test_prepare_registers_the_block_and_avoids_zones(self):
        self.cell.site = zone_world([[-30, 4, 30, 40]])  # planned land south of the pole
        state = self.built()
        for e in state["plan"]["entities"]:
            self.assertFalse(sc._overlaps(sc._area([e], 0), [[-30, 4], [30, 40]]), e)
        self.assertFalse(sc._overlaps(state["plan"]["reserved_area"], [[-30, 4], [30, 40]]))
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"][sc.BLOCK], state["plan"])

    def test_refused_when_every_site_is_planned_or_illegal(self):
        self.cell.site = zone_world([[-200, -200, 200, 200]])
        result = self.cell.step(self.obs(), {"products": [BELT]})
        self.assertEqual(result["reason"], "no_supply_cell_site_off_ore_and_plan_zones")
        self.game.legal = []  # the native survey found ore or buildings everywhere
        self.cell.site = zone_world([])
        result = self.cell.step(self.obs(), {"products": [BELT]})
        self.assertEqual(result["reason"], "no_clear_supply_cell_site")
        self.assertFalse(self.cell.path.exists())

    def test_native_survey_choice_is_kept(self):
        self.game.legal = [3]
        state = self.built()
        self.assertEqual(state["plan"]["reserved_area"], self.game.payloads[0][2]["reserved_area"])

    def test_other_blocks_are_avoided(self):
        board = {
            "world_id": "w",
            "catalog_fingerprint": self.c.fingerprint,
            "blocks": {"startup_science": {"entities": [], "reserved_area": [[-30, -30], [30, 30]]}},
        }
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        state = self.built()
        self.assertFalse(sc._overlaps(state["plan"]["reserved_area"], [[-30, -30], [30, 30]]))


try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional native test dependency
    LuaRuntime = None


class SurveyLuaTests(unittest.TestCase):
    @unittest.skipUnless(LuaRuntime, "Lua runtime required")
    def test_ore_refuses_trees_are_clearable_free_land_is_legal(self):
        plans = [
            {"reserved_area": [[0, 0], [4, 4]], "entities": [{"name": "inserter", "position": {"x": 1.5, "y": 1.5}}]},
            {
                "reserved_area": [[10, 0], [14, 4]],
                "entities": [{"name": "inserter", "position": {"x": 11.5, "y": 1.5}}],
            },
            {
                "reserved_area": [[20, 0], [24, 4]],
                "entities": [{"name": "inserter", "position": {"x": 21.5, "y": 1.5}}],
            },
        ]
        stubs = """
f={};defines={build_check_type={manual_ghost=1}}
helpers={json_to_table=function(t) return PLANS end}
local function lo(a) return a.area and a.area[1][1] or a.position.x end
s={count_entities_filtered=function(a) return (lo(a)<5) and 1 or 0 end,
   find_entities_filtered=function(a) return {} end,
   can_place_entity=function(a) if a.position.x>20 then return a.forced==true end return true end}
function target() return nil end
"""
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("PLANS=" + _lua_literal(plans) + stubs)
        result = lua.execute(sc.SURVEY_LUA.replace("PAYLOAD", '""'))
        self.assertEqual(list(result["legal"].values()), [2])
        self.assertEqual(list(result["clearable"].values()), [3])


def _lua_literal(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={_lua_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "{" + ",".join(_lua_literal(v) for v in value) + "}"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


class BuildAndProofTests(Cell):
    def test_build_bar_feed_then_proof(self):
        state = self.built()
        live = self.live(state, finished=0)
        result = self.cell.step(self.obs(live, inventory={"iron-plate": 40}), {"products": [BELT]})
        self.assertEqual(result["type"], "insert")  # first batch from hand
        self.assertIsNone(self.cell.proof(self.obs(live)))
        live = self.live(state, stock=4, finished=3)
        result = self.cell.step(self.obs(live), {"products": [BELT]})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.cell.state()["phase"], "verified")
        self.assertEqual(result["evidence"]["products"][0]["chest_stock"], 4)

    def test_world_change_blocks(self):
        self.built()
        result = self.cell.step(dict(self.obs(), world_id="other"), {"products": [BELT]})
        self.assertEqual(result["reason"], "supply_cell_world_or_catalog_changed")


class UseFixture(Cell):
    def setUp(self):
        super().setUp()
        self.state = self.built()
        self.state["phase"] = "verified"
        self.cell._save(self.state)
        board = json.loads((self.root / "pcb-board.json").read_text())
        board["catalog_fingerprint"] = self.c.fingerprint
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        self.bootstrap.machine_stock = sc.chain_stock(None, self.cell.stock)
        self.bootstrap.game.cfg = self.game.cfg
        self.bootstrap.game.expected_world = "w"

    def ensure(self, obs, count, item=BELT):
        craft = {"type": "craft", "recipe": item, "count": count, "reason": "craft"}
        with patch.object(DeterministicBootstrap, "ensure_item", return_value=craft) as parent:
            return self.bootstrap.ensure_item(obs, item, count), parent


class UseTests(UseFixture):
    def test_ensure_item_collects_from_the_cell_chest(self):
        result, parent = self.ensure(self.obs(self.live(self.state, stock=30)), 40)
        parent.assert_not_called()
        self.assertEqual((result["type"], result["count"]), ("take_many", 40))
        output = self.state["products"][0]["output"]
        self.assertEqual([s["position"] for s in result["sources"]], [output])  # input chests stay owned

    def test_ensure_item_waits_instead_of_hand_crafting(self):
        inside = {"iron-plate": 50}
        result, parent = self.ensure(self.obs(self.live(self.state, stock=2, inside=inside)), 40)
        parent.assert_not_called()
        self.assertEqual(result["reason"], f"waiting for machine-made {BELT}")
        result, parent = self.ensure(self.obs(self.live(self.state, stock=0, inside=inside)), HANDCRAFT_LIMIT)
        parent.assert_called_once()  # small requests still hand craft

    def test_starved_cell_is_fed_from_hand(self):
        first = self.obs(self.live(self.state, finished=5), tick=1000)
        self.cell.stock(first, BELT)  # progress seen at tick 1000
        later = self.obs(
            self.live(self.state, finished=5), inventory={"iron-plate": 60}, tick=1000 + 2 * sc.PROGRESS_WRITE_TICKS
        )
        result, parent = self.ensure(later, 40)
        parent.assert_not_called()
        self.assertEqual((result["type"], result["item"]), ("insert", "iron-plate"))

    def test_stalled_cell_falls_back_to_hand_crafting(self):
        inside = {"iron-plate": 50}
        self.cell.stock(self.obs(self.live(self.state, finished=5, inside=inside), tick=1000), BELT)
        late = self.obs(self.live(self.state, finished=5, inside=inside), tick=1001 + sc.STALL_TICKS)
        self.assertEqual(self.cell.stock(late, BELT)["status"], "stalled")
        result, parent = self.ensure(late, 40)
        parent.assert_called_once()
        self.assertIn("stalled: hand-craft fallback", result["reason"])

    def test_feed_targets_follow_demand(self):
        live = self.live(self.state)
        targets = self.cell.feed_targets(self.obs(live), self.state, demand={BELT: 40})
        self.assertEqual(sorted((d, i) for d, i, _ in targets), [(20, "iron-plate"), (40, "iron-plate")])
        topped = self.live(self.state, inside={"iron-plate": 35})
        self.assertEqual(self.cell.feed_targets(self.obs(topped), self.state, demand={BELT: 40}), [])  # < a batch short
        capped = self.cell.feed_targets(self.obs(live), self.state, demand={BELT: 1000})
        self.assertEqual(max(d for d, _, _ in capped), sc.FEED_CAP)
        none = self.cell.feed_targets(self.obs(live, inventory={BELT: 50}), self.state, demand={BELT: 40})
        self.assertEqual(none, [])  # carried products cover the demand

    def test_upkeep_feeds_only_plates_beyond_the_reserve(self):
        live = self.live(self.state)
        with patch.object(sc.SupplyCell, "demand", return_value={BELT: 200}):
            self.assertIsNone(self.cell.upkeep(self.obs(live, inventory={"iron-plate": sc.FEED_RESERVE})))
            feed = self.cell.upkeep(self.obs(live, inventory={"iron-plate": sc.FEED_RESERVE + 30}))
            self.assertEqual((feed["type"], feed["count"]), ("insert", 30))
            # a producing produce row is the permanent source: no more feeding
            self.cell.rows_stock = lambda obs, item: {"status": "producing"}
            self.assertIsNone(self.cell.upkeep(self.obs(live, inventory={"iron-plate": 500})))


class PlannerTests(unittest.TestCase):
    def receipt(self, products=(BELT,), demand=None):
        return {
            "enabled_recipes": ENABLED,
            "supply_cell_candidates": {
                "products": list(products),
                "demand": demand if demand is not None else {BELT: 464},
                "rows": {BELT: {"count": 464}},
            },
        }

    def test_proposed_once_after_power(self):
        jobs = [{"capability": "power", "status": "succeeded"}]
        nodes = sc.supply_cell_nodes(jobs, [], self.receipt(), CAPABILITIES)
        self.assertEqual([(n["id"], n["parameters"]) for n in nodes], [(sc.NODE_ID, {"products": [BELT]})])
        self.assertEqual(sc.supply_cell_nodes([], [], self.receipt(), CAPABILITIES), [])  # power first
        self.assertEqual(sc.supply_cell_nodes(jobs, nodes, self.receipt(), CAPABILITIES), [])  # queued
        running = jobs + [{"capability": sc.CAPABILITY, "status": "running", "parameters": {"products": [BELT]}}]
        self.assertEqual(sc.supply_cell_nodes(running, [], self.receipt(), CAPABILITIES), [])
        self.assertEqual(sc.supply_cell_nodes(jobs, [], self.receipt(products=()), CAPABILITIES), [])
        locked = dict(self.receipt(), enabled_recipes={})
        self.assertEqual(sc.supply_cell_nodes(jobs, [], locked, CAPABILITIES), [])
        ids = [n["id"] for n in autopilot_nodes(jobs, [], self.receipt(), CAPABILITIES)]
        self.assertIn(sc.NODE_ID, ids)

    def test_retired_when_demand_ends_or_a_row_takes_over(self):
        built = [
            {"capability": "power", "status": "succeeded"},
            {"capability": sc.CAPABILITY, "status": "succeeded", "parameters": {"products": [BELT]}},
        ]
        self.assertEqual(sc.supply_cell_nodes(built, [], self.receipt(), CAPABILITIES), [])  # still needed
        ended = sc.supply_cell_nodes(built, [], self.receipt(demand={BELT: 4}), CAPABILITIES)
        self.assertEqual([(n["id"], n["parameters"]) for n in ended], [(sc.RETIRE_NODE_ID, {"retire": True})])
        self.assertLess(ended[0]["priority"], -500)
        row = built + [{"capability": "produce", "status": "succeeded", "parameters": {"item": BELT}}]
        self.assertEqual(len(sc.supply_cell_nodes(row, [], self.receipt(), CAPABILITIES)), 1)
        retired = built + [{"capability": sc.CAPABILITY, "status": "succeeded", "parameters": {"retire": True}}]
        self.assertEqual(sc.supply_cell_nodes(retired, [], self.receipt(demand={}), CAPABILITIES), [])  # no loop
        self.assertEqual(sc.supply_cell_nodes(retired, [], self.receipt(products=[BELT, INS]), CAPABILITIES), [])


class RetireTests(UseFixture):
    def test_waits_for_dependent_jobs_then_mines_and_releases(self):
        jobs = {
            "world_id": "w",
            "jobs": {
                "a": {
                    "world_id": "w",
                    "capability": "smelting_district",
                    "status": "waiting",
                    "reason": f"waiting for machine-made {BELT}",
                }
            },
        }
        (self.root / "jobs.json").write_text(json.dumps(jobs))
        live = self.live(self.state, stock=5)
        result = self.cell.step(self.obs(live), {"retire": True})
        self.assertEqual(result["reason"], "supply_cell_retire_waits_for_dependent_jobs")
        self.assertEqual(self.cell.state()["phase"], "retiring")
        self.assertIsNone(self.cell.upkeep(self.obs(live, inventory={"iron-plate": 500})))  # feeding stopped
        (self.root / "jobs.json").write_text(json.dumps({"world_id": "w", "jobs": {}}))
        result = self.cell.step(self.obs(live), {"retire": True})
        self.assertEqual((result["type"], result["name"]), ("mine", "inserter"))
        self.assertEqual(result["expected_entity_world_id"], "w")
        poles = [e for e in live if e["name"] == "small-electric-pole"]
        result = self.cell.step(self.obs(poles), {"retire": True})
        self.assertEqual(result["status"], "succeeded")
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertNotIn(sc.BLOCK, board["blocks"])
        self.assertEqual(self.cell.state()["phase"], "retired")
        self.assertIsNotNone(self.cell.retire_proof(self.obs(poles)))
        self.assertIsNone(self.cell.stock(self.obs(poles), BELT))  # no longer a stock source
        result = self.cell.step(self.obs(poles), {"products": [BELT]})
        self.assertEqual(result["reason"], "supply_cell_retired")


if __name__ == "__main__":
    unittest.main()
