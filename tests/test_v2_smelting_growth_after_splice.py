"""Paired-bank growth on a real district after the bus feed splice (fleet pace1, seed 3794581402).

The bus splice replaces one belt of each bank's output line by the bus feed
splitter in the district plan; the banks keep their original entity lists.
``SmeltingDistrict.proof`` looked those belts up in the world and raised
``KeyError(('transport-belt', 64.5, -113.5))`` on every ``smelting_capacity`` job.
"""

import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.smelting import FURNACE_NAMES, SmeltingDistrict, feed_lane_positions, grown_bank, merge_entities
from factorio_agent.transport import xy

FIXTURE = Path(__file__).parent / "fixtures" / "pace1_3794581402_smelting_district.json.gz"
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def load():
    with gzip.open(FIXTURE, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def key(e):
    return e["name"], *xy(e)


def built_world(plan):
    """Every plan entity built, producing and powered (what the proof reads)."""
    rows = []
    for i, e in enumerate(plan["entities"]):
        row = dict(e, unit_number=i + 1, direction=e.get("direction", 0))
        if e["name"] in FURNACE_NAMES:
            row.update(products_finished=3, remaining_burning_fuel=1)
        elif e["name"] == "small-electric-pole":
            row["electric_network_id"] = 1
        elif e["name"] == "inserter":
            row.update(electric_network_connected=True, energy=1)
        elif e["name"] == "transport-belt":
            row["belt_inventory"] = {"iron-plate": 1, "copper-plate": 1}
        rows.append(row)
    return rows


class GrowthAfterSpliceTests(unittest.TestCase):
    def setUp(self):
        self.state = load()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        world, fingerprint = self.state["world_id"], self.state["catalog_fingerprint"]
        (self.root / "smelting-district.json").write_text(json.dumps(self.state), encoding="utf-8")
        board = {
            "world_id": world,
            "catalog_fingerprint": fingerprint,
            "blocks": {"smelting_district": self.state["plan"]},
        }
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        mining = {
            "world_id": world,
            "banks": [
                {"ready": True, "nominal_rate_per_minute": 450, "plan": {"resource": r}}
                for r in ("iron-ore", "copper-ore")
            ],
        }
        (self.root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
        lanes = {
            "ok": True,
            "banks": [
                {"resource": b["resource"], "lanes": [{b["resource"]: 4}, {"coal": 4}]} for b in self.state["banks"]
            ],
        }
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value=lanes))
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        catalog = SimpleNamespace(
            fingerprint=fingerprint,
            recipes={
                p: {"energy": 3.2, "products": [{"name": p, "amount": 1}]} for p in ("iron-plate", "copper-plate")
            },
            entities={"stone-furnace": {"crafting_speed": 1, "type": "furnace"}},
        )
        self.routine = SmeltingDistrict(game, None, self.builder, catalog)

    def tearDown(self):
        self.directory.cleanup()

    def test_the_saved_district_has_bank_belts_replaced_by_the_bus_feed_splitter(self):
        planned = {key(e) for e in self.state["plan"]["entities"]}
        replaced = [key(e) for b in self.state["banks"] for e in b["entities"] if key(e) not in planned]
        self.assertEqual(replaced, [("transport-belt", 64.5, -113.5), ("transport-belt", 64.5, -94.5)])
        splitters = {xy(e) for e in self.state["plan"]["entities"] if e["name"] == "splitter"}
        self.assertTrue({(64.5, -113.0), (64.5, -94.0)} <= splitters)

    def test_grown_bank_proves_against_the_spliced_district(self):
        obs = {"world_id": self.state["world_id"], "tick": 10, "entities": []}
        with patch("factorio_agent.smelting.preflight", return_value={"ok": True, "clear_obstacles": []}):
            self.builder.ensure_plan.return_value = {"status": "waiting", "reason": "building"}
            first = self.routine.capacity_step(obs, {"resource": "copper-ore", "rate_per_minute": 112.5})
            self.assertEqual(first["status"], "waiting", first)
            copper = next(b for b in self.routine.state["banks"] if b["resource"] == "copper-ore")
            self.assertEqual(copper["per_side_count"], 3)
            # The splitters stay in the plan; growth only adds reserved slot entities.
            splitters = [e for e in self.routine.state["plan"]["entities"] if e["name"] == "splitter"]
            self.assertEqual(len(splitters), 5)
            self.builder.ensure_plan.return_value = {"status": "succeeded"}
            obs["entities"] = built_world(self.routine.state["plan"])
            result = self.routine.capacity_step(obs, {"resource": "copper-ore", "rate_per_minute": 112.5})
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(result["evidence"]["banks"]), 2)

    def test_every_slot_grows_without_overlap_and_feeds_stay_plain_belts(self):
        plan = self.state["plan"]["entities"]
        splitter_tiles = set().union(*(GEOMETRY._occupied_by_plan([e]) for e in plan if e["name"] == "splitter"))
        for bank in self.state["banks"]:
            before = {key(e) for e in bank["entities"]}
            for per_row in range(bank["per_side_count"] + 1, bank["capacity_slots"] + 1):
                new = grown_bank(bank, per_row)
                added = [e for e in new["entities"] if key(e) not in before]
                es = merge_entities(plan, added)
                boxes = [GEOMETRY._occupied_by_plan([e]) for e in es]
                self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)), (bank["resource"], per_row))
                feeds = {(p["x"], p["y"]) for p in feed_lane_positions(new)}
                self.assertFalse(feeds & splitter_tiles, (bank["resource"], per_row))


class SiteSearchBudgetTests(unittest.TestCase):
    def test_a_search_that_ran_out_of_budget_waits_instead_of_blocking(self):
        with TemporaryDirectory() as directory:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(directory)), query=Mock())
            routine = SmeltingDistrict(game, None, Mock(), SimpleNamespace(fingerprint="c"))
            exhausted = {"ok": False, "reason": "no_clear_pcb_smelting_district_with_corridors"}
            routine.prepare = Mock(return_value=dict(exhausted, evidence={"planning_budget_exhausted": True}))
            self.assertEqual(routine.step({"world_id": "w"})["status"], "waiting")
            routine.prepare = Mock(return_value=dict(exhausted, evidence={"planning_budget_exhausted": False}))
            self.assertEqual(routine.step({"world_id": "w"})["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
