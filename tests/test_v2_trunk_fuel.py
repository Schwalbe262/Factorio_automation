"""Hand-fuel coal comes off a proven coal bank's belts in one full fetch (construction_supply.trunk_fuel)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.construction_supply import (
    FUEL_BATCH,
    TRUNK_FUEL_TILE_MIN,
    V2Bootstrap,
    validate_take_many,
)

WORLD = "w-1"


def belt(x, y, coal, unit):
    return {
        "name": "transport-belt",
        "position": {"x": x, "y": y},
        "unit_number": unit,
        "direction": 8,
        "belt_inventory": {"coal": coal} if coal else {},
    }


class TrunkFuelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write_bank(ready=True)
        self.bootstrap = V2Bootstrap.__new__(V2Bootstrap)
        self.bootstrap.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)))
        self.bootstrap.catalog = None
        self.belts = [belt(10.5, y + 0.5, 8, 100 + y) for y in range(6)]
        self.obs = {"world_id": WORLD, "inventory": {}, "entities": list(self.belts)}

    def tearDown(self):
        self.tmp.cleanup()

    def write_bank(self, *, ready, resource="coal", world=WORLD):
        plan = {
            "resource": resource,
            "entities": [
                {"name": "electric-mining-drill", "position": {"x": 12, "y": 2}},
                *({"name": "transport-belt", "position": {"x": 10.5, "y": y + 0.5}} for y in range(6)),
            ],
            "ports": [{"item": resource, "direction": "output", "position": {"x": 40.5, "y": 40.5}, "facing": 8}],
        }
        state = {"world_id": world, "banks": [{"ready": ready, "plan": plan}]}
        (self.root / "mining-outposts.json").write_text(json.dumps(state), encoding="utf-8")

    def test_full_fetch_from_the_bank_belts_fullest_first(self):
        self.belts[3]["belt_inventory"] = {"coal": 7}
        action = self.bootstrap.trunk_fuel(self.obs, "coal", 48)
        validate_take_many(action)
        self.assertEqual(action["type"], "take_many")
        self.assertEqual(action["count"], 47)  # what the belts hold, at most the request
        self.assertEqual(action["sources"][-1]["unit_number"], 103)  # the 7-coal tile last
        self.assertTrue(all(s["kind"] == "belt" for s in action["sources"]))

    def test_thin_tiles_and_unproven_banks_are_no_source(self):
        for b in self.belts:
            b["belt_inventory"] = {"coal": TRUNK_FUEL_TILE_MIN - 1}
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "coal", 24))
        self.assertEqual(self.bootstrap.trunk_coal(self.obs), 0)
        for b in self.belts:
            b["belt_inventory"] = {"coal": 8}
        self.write_bank(ready=False)
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "coal", 24))
        self.write_bank(ready=True, world="other")
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "coal", 24))
        self.write_bank(ready=True, resource="iron-ore")
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "coal", 24))

    def test_never_a_trip_smaller_than_a_batch(self):
        self.obs["entities"] = self.belts[:1]  # 8 coal
        self.assertIsNotNone(self.bootstrap.trunk_fuel(self.obs, "coal", FUEL_BATCH))
        self.obs["entities"] = []
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "coal", FUEL_BATCH))
        self.assertIsNone(self.bootstrap.trunk_fuel(self.obs, "wood", 24))

    def test_fuel_take_prefers_the_trunk_over_a_chest_with_a_few_pieces(self):
        from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap

        self.bootstrap._unreserved_stock_observation = lambda obs, item: dict(
            obs, entities=[]
        )  # ownership hides the bank
        chest = {"type": "take", "name": "wooden-chest", "item": "coal", "count": FUEL_BATCH}
        with patch.object(DeterministicBootstrap, "_take_output", return_value=chest):
            action = self.bootstrap._take_output(self.obs, "coal", 40)
            self.assertEqual((action["type"], action["count"]), ("take_many", 40))
            full = dict(chest, count=40)
        with patch.object(DeterministicBootstrap, "_take_output", return_value=full):
            self.assertEqual(self.bootstrap._take_output(self.obs, "coal", 40), full)

    def test_maintain_fuel_fetches_from_the_trunk_while_the_starter_chest_is_short(self):
        drill = {"name": "burner-mining-drill", "position": {"x": 1, "y": 1}, "inventory": {}}
        chest = {"name": "wooden-chest", "position": {"x": 1.5, "y": -0.5}, "inventory": {"coal": 2}}
        coal_drill = {"name": "burner-mining-drill", "position": {"x": 5, "y": 1}, "inventory": {"coal": 3}}
        self.obs.update(resources={"coal": {}}, entities=[drill, chest, coal_drill, *self.belts])
        cells = [
            {
                "drill": {"name": coal_drill["name"], "position": coal_drill["position"]},
                "receiver": {"name": chest["name"], "position": chest["position"]},
                "remaining": 1000,
                "fuel": 3,
                "burning": True,
            }
        ]
        self.bootstrap._existing_cells = Mock(return_value={"ok": True, "cells": cells})
        self.bootstrap.ensure_item = Mock(return_value={"type": "take_many", "item": "coal", "count": 24})
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["type"], "take_many")
        self.assertEqual(self.bootstrap.ensure_item.call_args.args[1:], ("coal", 3 * FUEL_BATCH))
        self.obs["entities"] = [drill, chest, coal_drill]  # no trunk: wait for the chest batch
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))

    def test_fuel_take_many_only_from_belts(self):
        action = self.bootstrap.trunk_fuel(self.obs, "coal", 24)
        action["sources"][0] = dict(action["sources"][0], kind="chest", name="wooden-chest")
        with self.assertRaises(ValueError):
            validate_take_many(action)


if __name__ == "__main__":
    unittest.main()
