import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.construction_supply import V2Bootstrap


class RegisteredStockOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bootstrap = V2Bootstrap(
            SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root)), SimpleNamespace(fingerprint="c", items={})
        )
        self.machine = dict(
            name="assembling-machine-1",
            position={"x": 5, "y": 6},
            unit_number=1,
            output_inventory={"iron-gear-wheel": 6},
            inventory={"iron-gear-wheel": 6},
        )
        self.chest = dict(name="wooden-chest", position={"x": 3, "y": 6}, unit_number=2, inventory={"iron-plate": 50})
        self.output = dict(
            name="wooden-chest", position={"x": 5, "y": 9}, unit_number=3, inventory={"automation-science-pack": 10}
        )
        self.furnace = dict(
            name="stone-furnace", position={"x": 12, "y": 6}, unit_number=4, output_inventory={"iron-plate": 20}
        )
        self.obs = dict(world_id="w", entities=[self.machine, self.chest, self.output, self.furnace])
        self.board = dict(
            world_id="w",
            catalog_fingerprint="c",
            blocks={
                "startup_science": dict(
                    entities=[self.machine, self.chest, self.output],
                    ports=[dict(position=self.output["position"], direction="output", item="automation-science-pack")],
                )
            },
        )
        self.save()

    def save(self):
        (self.root / "pcb-board.json").write_text(json.dumps(self.board), encoding="utf-8")

    def test_constructor_cannot_borrow_funded_gears_or_raw_input_chest(self):
        self.assertIsNone(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 10))
        action = self.bootstrap._take_output(self.obs, "iron-plate", 40)
        self.assertEqual([s["unit_number"] for s in action["sources"]], [4])
        self.assertEqual(self.chest["inventory"]["iron-plate"], 50)

    def test_generic_fallback_also_excludes_finite_science_outputs(self):
        with patch(
            "factorio_agent.core.deterministic_bootstrap.DeterministicBootstrap._take_output", return_value=None
        ) as fallback:
            self.bootstrap._take_output(self.obs, "automation-science-pack", 10)
        self.assertEqual([e["unit_number"] for e in fallback.call_args.args[0]["entities"]], [4])

    def test_other_factories_publish_only_their_declared_item_at_the_output_port(self):
        self.board["blocks"] = {
            "gear_stock": dict(
                entities=[self.machine, self.chest],
                ports=[dict(position=self.machine["position"], direction="output", item="iron-gear-wheel")],
            )
        }
        self.save()
        self.assertEqual(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 6)["sources"][0]["unit_number"], 1)
        self.assertEqual(
            [s["unit_number"] for s in self.bootstrap._take_output(self.obs, "iron-plate", 40)["sources"]], [4]
        )

    def test_changed_board_invalidates_cached_public_stock_permission(self):
        self.board["blocks"] = {}
        self.save()
        path = self.root / "pcb-board.json"
        stamp = path.stat().st_mtime_ns
        self.assertIsNotNone(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 6))
        self.board["blocks"] = {"startup_science": dict(entities=[self.machine], ports=[])}
        self.save()
        os.utime(path, ns=(stamp, stamp))
        self.assertIsNone(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 6))

    def test_same_shared_entity_uses_the_most_restrictive_owner(self):
        self.board["blocks"]["public_gear"] = dict(
            entities=[self.machine],
            ports=[dict(position=self.machine["position"], direction="output", item="iron-gear-wheel")],
        )
        self.save()
        self.assertIsNone(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 6))

    def test_foreign_world_or_catalog_fails_before_selecting_goods(self):
        for field in ["world_id", "catalog_fingerprint"]:
            original = self.board[field]
            self.board[field] = "other"
            self.save()
            with self.assertRaises(ValueError):
                self.bootstrap._take_output(self.obs, "iron-plate", 40)
            self.board[field] = original

    def test_no_registered_board_preserves_unreserved_early_stock_collection(self):
        (self.root / "pcb-board.json").unlink()
        self.assertIsNotNone(self.bootstrap._take_output(self.obs, "iron-gear-wheel", 6))
