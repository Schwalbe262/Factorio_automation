import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.construction_supply import HANDCRAFT_LIMIT, V2Bootstrap, validate_take_many
from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap
from factorio_agent.machine_stock import STALL_TICKS, MachineStock
from factorio_agent.production_rows import BLOCK_PREFIX, ProductionRows

ITEM = "transport-belt"
CHEST = {"name": "iron-chest", "position": {"x": 10.5, "y": 0.5}, "direction": 0}
MACHINE = {"name": "assembling-machine-1", "position": {"x": 2.5, "y": 0.5}, "direction": 0, "recipe": ITEM}
BELT = {"name": "transport-belt", "position": {"x": 8.5, "y": 0.5}, "direction": 4}
CRAFT = {"type": "craft", "recipe": ITEM, "count": 25, "reason": "engine craft transport-belt from live recipe"}


def live(e, unit, **extra):
    return dict(e, unit_number=unit, **extra)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.game = Mock(backend="assisted", expected_world="w")
        self.game.cfg = SimpleNamespace(runtime_dir=self.tmp.name)
        catalog = SimpleNamespace(fingerprint="fp", items={})
        self.rows = ProductionRows(self.game, Mock(), catalog)
        plan = {
            "entities": [MACHINE, BELT, CHEST],
            "ports": [
                {
                    "kind": "item",
                    "item": ITEM,
                    "direction": "output",
                    "name": "iron-chest",
                    "position": CHEST["position"],
                }
            ],
        }
        board = {"world_id": "w", "catalog_fingerprint": "fp", "blocks": {BLOCK_PREFIX + ITEM: plan}}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.state = {
            "world_id": "w",
            "phase": "verified",
            "item": ITEM,
            "line": {"tail": {"chest": CHEST}, "main": {"entities": [MACHINE]}},
        }
        self.rows.save(ITEM, self.state)
        self.stock = MachineStock(self.rows)
        self.bootstrap = V2Bootstrap(self.game)
        self.bootstrap.catalog = catalog
        self.bootstrap.machine_stock = self.stock

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, chest=0, belt=0, finished=1, tick=100, inventory=None):
        return {
            "world_id": "w",
            "tick": tick,
            "inventory": inventory or {},
            "entities": [
                live(MACHINE, 1, products_finished=finished),
                live(BELT, 2, belt_inventory={ITEM: belt}),
                live(CHEST, 3, inventory={ITEM: chest}),
            ],
        }


class MachineStockTests(Fixture):
    def test_absent_building_producing_stalled_broken(self):
        self.assertIsNone(self.stock(self.obs(), "pipe"))
        self.assertEqual(self.stock(self.obs(chest=4), ITEM)["status"], "producing")
        self.assertEqual(self.stock(self.obs(tick=100 + STALL_TICKS), ITEM)["status"], "producing")
        self.assertEqual(self.stock(self.obs(tick=200 + STALL_TICKS), ITEM)["status"], "stalled")
        self.assertEqual(self.stock(self.obs(finished=2, tick=300 + STALL_TICKS), ITEM)["status"], "producing")
        broken = self.obs()
        broken["entities"].pop()
        self.assertEqual(self.stock(broken, ITEM)["status"], "broken")
        self.rows.save(ITEM, dict(self.state, phase="build"))
        self.assertEqual(self.stock(self.obs(), ITEM)["status"], "building")
        self.rows.save(ITEM, dict(self.state, world_id="other"))
        self.assertIsNone(self.stock(self.obs(), ITEM))


class HandcraftCapTests(Fixture):
    def ensure(self, obs, count):
        with patch.object(DeterministicBootstrap, "ensure_item", return_value=dict(CRAFT)) as parent:
            return self.bootstrap.ensure_item(obs, ITEM, count), parent

    def test_above_the_limit_takes_a_batch_from_the_row_chest_only(self):
        result, parent = self.ensure(self.obs(chest=30, belt=8), 40)
        parent.assert_not_called()
        self.assertEqual((result["type"], result["item"], result["count"]), ("take_many", ITEM, 40))
        self.assertEqual([s["unit_number"] for s in result["sources"]], [3])  # the row belt stays owned
        validate_take_many(result)

    def test_above_the_limit_with_a_small_stock_waits_instead_of_hand_crafting(self):
        result, parent = self.ensure(self.obs(chest=3), 40)
        parent.assert_not_called()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], f"waiting for machine-made {ITEM}")
        self.assertEqual(result["evidence"]["limit"], HANDCRAFT_LIMIT)

    def test_at_or_below_the_limit_hand_crafting_is_unchanged(self):
        result, parent = self.ensure(self.obs(chest=3), HANDCRAFT_LIMIT)
        parent.assert_called_once()
        self.assertEqual(result, CRAFT)

    def test_held_inventory_needs_no_source(self):
        result, parent = self.ensure(self.obs(inventory={ITEM: 50}), 40)
        parent.assert_called_once()

    def test_no_row_means_todays_behaviour(self):
        self.bootstrap.machine_stock = Mock(return_value=None)
        result, parent = self.ensure(self.obs(chest=30), 40)
        self.assertEqual(result, CRAFT)

    def test_stalled_or_building_rows_fall_back_and_say_so(self):
        for status in ("stalled", "building", "broken"):
            with self.subTest(status=status):
                self.bootstrap.machine_stock = Mock(return_value={"status": status})
                result, parent = self.ensure(self.obs(chest=0), 40)
                parent.assert_called_once()
                self.assertEqual(result["type"], "craft")
                self.assertIn(f"machine-made {ITEM} {status}: hand-craft fallback", result["reason"])


if __name__ == "__main__":
    unittest.main()
