import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.blocks.geometry import xy
from factorio_agent.jobs import CAPABILITIES, Request
from factorio_agent.production_rows import (
    BLOCK_PREFIX,
    ProductionRows,
    choose_chest,
    choose_inserter,
    validate_parameters,
)
from factorio_agent.site.geometry import Rect
from factorio_agent.site.planzones import Allocation
from test_v2_production_layout import catalog

SITE = "factorio_agent.production_site."
IRON_PORT = {
    "kind": "item",
    "item": "iron-plate",
    "position": {"x": 60.5, "y": 40.5},
    "facing": 4,
    "direction": "output",
}


def route(game, builder, obs, source, destination, entities):
    return {"ok": True, "entities": [], "clear_obstacles": [], "underground_pairs": [], "route_metrics": {"n": 1}}


class FakeSite:
    """Hands out one west-fed block per candidate, far from everything else."""

    def __init__(self):
        self.allocated, self.kept, self.released = [], [], []

    def allocate_candidates(self, kind, along, depth, *, name, count, returns_to_bus, world_id):
        self.allocated.append((kind, along, depth, name, returns_to_bus))
        rect = Rect(100, 50, 100 + along, 50 + depth)
        tap = Rect(97, 50, 100, 50 + depth)
        block = Allocation(
            f"{name}#0", kind, "E", 0, along, depth, rect, "N", "W", tap, "W", "E", (100.0 + along, 50.0), False
        )
        return [block]

    def keep_only(self, prefix, keep, world_id=None):
        self.kept.append((prefix, keep))

    def release(self, name, world_id=None):
        self.released.append(name)


class ProductionRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog = catalog()
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.tmp.name), query=Mock())
        self.game.query.return_value = {"ok": True, "slots": 2, "size": 32}
        self.builder = Mock()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.site = FakeSite()
        self.rows = ProductionRows(self.game, self.builder, self.catalog, site=self.site)
        self.board(science=True)
        self.patches = [
            patch(SITE + "plan_connection", side_effect=route),
            patch(SITE + "connect_power_groups", return_value={"ok": True, "entities": []}),
            patch(SITE + "preflight", return_value={"ok": True, "clear_obstacles": []}),
        ]
        self.routes = self.patches[0].start()
        for p in self.patches[1:]:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def board(self, *, science=True, ports=(IRON_PORT,), bus=None):
        blocks = {"smelting_district": {"ports": list(ports), "reserved_area": [[50, 30], [62, 45]]}}
        if science:
            blocks["automation_science"] = {"ports": [], "reserved_area": [[-50, -50], [-40, -40]]}
        data = {"world_id": "w", "catalog_fingerprint": self.catalog.fingerprint, "blocks": blocks, "connections": []}
        if bus is not None:
            data["shared_main_bus"] = bus
        (self.root / "pcb-board.json").write_text(json.dumps(data), encoding="utf-8")

    def obs(self, entities=(), **extra):
        enabled = {name: True for name in self.catalog.recipes}
        enabled.update({"small-electric-pole": True, "iron-chest": True, "long-handed-inserter": True})
        return {"world_id": "w", "tick": 600, "enabled_recipes": enabled, "entities": list(entities), **extra}

    def live(self, plan, item, stock=5):
        rows = []
        for index, e in enumerate(plan["entities"], 1):
            row = dict(e, unit_number=index, electric_network_connected=True, energy=1, products_finished=2)
            if e["name"] == "iron-chest":
                row["inventory"] = {item: stock}
            rows.append(row)
        return rows

    def test_parameters_and_capability_contract(self):
        validate_parameters({"item": "pipe", "rate_per_minute": 30})
        for bad in ({"item": "pipe"}, {"item": "Pipe!", "rate_per_minute": 3}, {"item": "pipe", "rate_per_minute": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_parameters(bad)
        self.assertEqual(CAPABILITIES["produce"]["requires"], ["smelting_district", "power"])
        Request("k", "w", 1, "produce", {"item": "pipe", "rate_per_minute": 20}).validate()
        with self.assertRaises(ValueError):
            Request("k", "w", 1, "produce", {"item": "pipe", "rate_per_minute": 9999}).validate()
        self.assertEqual(choose_inserter(["inserter", "fast-inserter", "long-handed-inserter"]), "fast-inserter")
        self.assertEqual(choose_chest(["wooden-chest", "iron-chest"]), "iron-chest")

    def test_prepare_allocates_orients_routes_from_the_free_plate_port_and_settles(self):
        state = self.rows.prepare(self.obs(), {"item": "pipe", "rate_per_minute": 30})
        self.assertTrue(state["ok"], state)
        kind, along, depth, name, to_bus = self.site.allocated[0]
        self.assertEqual((kind, name, to_bus), ("production", BLOCK_PREFIX + "pipe", False))
        self.assertEqual(self.site.kept, [(BLOCK_PREFIX + "pipe#", BLOCK_PREFIX + "pipe#0")])
        source, destination = self.routes.call_args.args[3:5]
        self.assertEqual(source["position"], IRON_PORT["position"])
        self.assertEqual(destination["item"], "iron-plate")
        # Inputs face the west tap band: the input port is on the block's west side.
        self.assertLess(destination["position"]["x"], 100 + along / 2)
        chest = state["line"]["tail"]["chest"]
        self.assertIn(chest, state["plan"]["entities"])
        self.assertIn({**state["line"]["output"]}, state["plan"]["ports"])
        self.assertEqual(state["connections"][0]["source"], IRON_PORT["position"])

    def test_smelting_ports_wait_for_red_science_and_missing_sources_are_reported(self):
        self.board(science=False)
        result = self.rows.step(self.obs(), {"item": "pipe", "rate_per_minute": 30})
        self.assertEqual((result["status"], result["reason"]), ("blocked", "production_row_input_source_missing"))
        self.assertEqual(result["item"], "iron-plate")
        self.board(science=True, ports=())
        result = self.rows.step(self.obs(), {"item": "pipe", "rate_per_minute": 30})
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.rows.path("pipe").exists())

    def test_locked_recipes_block_with_a_clear_reason(self):
        obs = self.obs()
        obs["enabled_recipes"]["pipe"] = False
        result = self.rows.step(obs, {"item": "pipe", "rate_per_minute": 30})
        self.assertEqual(result["reason"], "production_row_recipes_locked")
        self.assertIn("pipe", result["missing"])

    def test_step_publishes_builds_sets_the_bar_and_proves_chest_stock(self):
        parameters = {"item": "pipe", "rate_per_minute": 30}
        self.game.query.return_value = {"ok": True, "slots": 32, "size": 32}
        action = self.rows.step(self.obs(), parameters)
        self.assertEqual((action["type"], action["slots"]), ("bar", 2))
        state = json.loads(self.rows.path("pipe").read_text(encoding="utf-8"))
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"][BLOCK_PREFIX + "pipe"], state["plan"])
        self.assertEqual(board["connections"], state["connections"])
        self.builder.ensure_plan.assert_called_with(self.obs(), state["plan"])
        self.game.query.return_value = {"ok": True, "slots": 2, "size": 32}
        waiting = self.rows.step(self.obs(), parameters)
        self.assertEqual(waiting, {"status": "waiting", "reason": "waiting for machine-made pipe in the stock chest"})
        done = self.rows.step(self.obs(self.live(state["plan"], "pipe")), parameters)
        self.assertEqual(done["status"], "succeeded", done)
        self.assertEqual(done["evidence"]["chest_stock"], 5)
        self.assertEqual(self.rows.state("pipe")["phase"], "verified")
        empty = self.live(state["plan"], "pipe", stock=0)
        self.assertIsNone(self.rows.proof(self.obs(empty), parameters))
        self.assertEqual(self.rows.last_proof_failure["reason"], "stock_chest_empty")

    def test_higher_rate_extends_in_place_or_blocks_beyond_reserved_slots(self):
        copper = dict(IRON_PORT, item="copper-plate", position={"x": 60.5, "y": 42.5})
        self.board(science=True, ports=(IRON_PORT, copper))
        item = "automation-science-pack"
        self.rows.step(self.obs(), {"item": item, "rate_per_minute": 6})
        before = self.rows.state(item)
        self.assertEqual(before["line"]["spec"]["sub_item"], "iron-gear-wheel")
        self.assertEqual(self.routes.call_count, 2)
        result = self.rows.step(self.obs(), {"item": item, "rate_per_minute": 12})
        after = self.rows.state(item)
        self.assertNotIn(result.get("reason"), ("production_row_extension_exceeds_reserved_slots",))
        self.assertEqual(after["line"]["main"]["count"], before["line"]["main"]["count"] + 1)
        self.assertEqual(after["line"]["inputs"], before["line"]["inputs"])
        old = set(map(json.dumps, before["plan"]["entities"]))
        self.assertTrue(old < set(map(json.dumps, after["plan"]["entities"])))
        self.assertEqual(self.routes.call_count, 2)  # no new routes for an in-place extension
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"][BLOCK_PREFIX + item], after["plan"])
        tight = json.loads(json.dumps(after))
        tight["line"]["main"]["growth"]["remaining_slots"] = 0
        self.rows.save(item, tight)
        blocked = self.rows.step(self.obs(), {"item": item, "rate_per_minute": 18})
        self.assertEqual(blocked["reason"], "production_row_extension_exceeds_reserved_slots")

    def test_missing_plate_with_a_bus_drives_one_bus_tap(self):
        self.board(science=True, ports=(), bus={"ports": [], "stations": [], "entities": []})
        tap = SimpleNamespace(state={}, prepare=Mock(return_value={"ok": True, "phase": "stock"}), _save=Mock())
        tap.step = Mock(return_value={"type": "build"})
        self.rows.tap_factory = lambda: tap
        parameters = {"name": "produce-pipe-iron-plate", "item": "iron-plate", "splitter_y": 3.5}
        with patch("factorio_agent.production_rows.tap_parameters", return_value=parameters):
            result = self.rows.step(self.obs(), {"item": "pipe", "rate_per_minute": 30})
        self.assertEqual(result, {"type": "build"})
        tap.prepare.assert_called_once()
        self.assertEqual(tap.state, {"ok": True, "phase": "stock"})
        self.assertFalse(self.rows.path("pipe").exists())

    def test_installed_bus_tap_is_the_preferred_source(self):
        tap_port = {"name": "produce-pipe-iron-plate", "item": "iron-plate", "position": {"x": 90.5, "y": 60.5}}
        self.board(science=True, bus={"ports": [dict(tap_port, facing=4)], "stations": [], "entities": []})
        state = self.rows.prepare(self.obs(), {"item": "pipe", "rate_per_minute": 30})
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["sources"][0]["kind"], "bus_tap")
        self.assertEqual(xy(self.routes.call_args.args[3]), (90.5, 60.5))


if __name__ == "__main__":
    unittest.main()
