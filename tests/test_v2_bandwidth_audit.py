"""Belt bandwidth audit (bandwidth_audit.py) and the furnace feed head chosen by it (planned_furnaces)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from factorio_agent import bandwidth_audit as ba
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.planned_furnaces import (
    KINDS,
    SLOTS,
    feed_shortfalls,
    furnace_block,
    growth_evidence,
    head_bandwidth,
    head_choice,
)
from factorio_agent.smelting import growth_tiles

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
# smelting_rates for two stone furnaces (catalog numbers of the live map's planned-*.json "rated").
BRICK = {
    "furnace_count": 2,
    "raw_per_minute": 75.0,
    "coal_per_minute_at_max_catalog_usage": 2.7,
    "product_per_minute": 37.5,
}
STEEL = {
    "furnace_count": 2,
    "raw_per_minute": 37.5,
    "coal_per_minute_at_max_catalog_usage": 2.7,
    "product_per_minute": 7.5,
}
SHORT = ("single_lane_feed", "capacity_shortfall", "lane_starvation")


def kinds(result):
    return sorted({f["kind"] for f in result["findings"]})


class FurnaceHeadTests(unittest.TestCase):
    def test_shared_head_halves_a_lane_twice_and_starves_a_full_brick_row(self):
        # The screenshot shape (eval-4177156614 brick block): raw and coal side-loaded onto one
        # short belt, then a splitter: each row gets a quarter belt of stone, its six furnaces
        # need exactly that, so the margin is not met.
        result = head_bandwidth(KINDS["brick"], BRICK, "shared")
        self.assertEqual(kinds(result), ["side_load_before_splitter", "single_lane_feed"])
        short = [f for f in result["findings"] if f["kind"] == "single_lane_feed"]
        self.assertEqual({(f["delivered_per_minute"], f["demand_per_minute"]) for f in short}, {(225.0, 225.0)})
        waste = [f for f in result["findings"] if f["kind"] == "side_load_before_splitter"]
        self.assertEqual({f["item"] for f in waste}, {"stone", "coal"})

    def test_split_head_feeds_every_row_a_full_lane(self):
        for kind, rates in (("brick", BRICK), ("steel", STEEL)):
            with self.subTest(kind=kind):
                self.assertEqual(head_bandwidth(KINDS[kind], rates, "split")["findings"], [])
        # Per row: the first input belt of each row carries a full lane of raw and of coal.
        from factorio_agent import belt_flow as bf

        bank = furnace_block(KINDS["brick"], 0, 0, SLOTS, "split")
        sources, _sinks, _ports = ba.port_sources(bank["ports"])
        flow = bf.solve(bank["entities"], sources, sinks=[(8, 1), (8, 11)])
        for tile in ((8, 1), (8, 11)):
            self.assertEqual(flow.at(tile), {"left": {"stone": 450.0}, "right": {"coal": 450.0}})

    def test_steel_head_is_sufficient_but_wasteful_and_the_choice_takes_the_split_head(self):
        shared = head_bandwidth(KINDS["steel"], STEEL, "shared")
        self.assertEqual(kinds(shared), ["side_load_before_splitter"])
        for kind, rates in (("brick", BRICK), ("steel", STEEL)):
            with self.subTest(kind=kind):
                choice = head_choice(KINDS[kind], rates)
                self.assertEqual(choice["head"], "split")
                self.assertEqual([c["head"] for c in choice["candidates"]], ["shared", "split"])

    def test_split_head_fits_the_frame_and_keeps_the_growth_slots_free(self):
        for kind in KINDS.values():
            bank = furnace_block(kind, 0, 0, head="split")
            boxes = [GEOMETRY._occupied_by_plan([e]) for e in bank["entities"] + bank["growth_entities"]]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertFalse(set().union(*boxes[: len(bank["entities"])]) & growth_tiles(GEOMETRY, [bank]))
            raw, coal, product = bank["ports"]
            self.assertEqual(
                (raw["position"], coal["position"], coal["facing"]), ({"x": 0.5, "y": 0.5}, {"x": 5.5, "y": 11.5}, 0)
            )
            self.assertEqual(
                bank["underground_pairs"], [{"input": [6.5, 7.5], "output": [6.5, 4.5], "direction": 0, "span": 3}]
            )
            self.assertEqual(bank["feed_head"], "split")
            self.assertEqual(bank["required_items"]["splitter"], 2)


class GrowthGuardTests(unittest.TestCase):
    def state(self, head, per_row):
        bank = furnace_block(KINDS["brick"], 0, 0, per_row, head)
        return {"bank": bank, "rated": BRICK, "raw_per_craft": 2}

    def test_growth_stops_where_the_head_cannot_feed_the_next_slot(self):
        self.assertEqual(feed_shortfalls(self.state("shared", 5), 5), [])
        self.assertTrue(feed_shortfalls(self.state("shared", 5), 6))
        self.assertEqual(feed_shortfalls(self.state("split", 5), 6), [])
        state = self.state("shared", 5)
        bank = state["bank"]
        raw, _coal, product = bank["ports"]
        rows = [dict(e, inventory={"stone": 10}) for row in bank["producer_rows"].values() for e in row]
        rows.append(dict(name="transport-belt", position=raw["position"], belt_inventory={"stone": 8}))
        rows.append(dict(name="transport-belt", position=product["position"], belt_inventory={}))
        verdict = growth_evidence(state, {"entities": rows})
        self.assertEqual(verdict["reason"], "feed_head_bandwidth_below_next_slot_demand")
        self.assertFalse(verdict["grow"])
        state = self.state("split", 5)
        rows[-2]["position"] = state["bank"]["ports"][0]["position"]
        self.assertTrue(growth_evidence(state, {"entities": rows})["grow"])


class RuntimeAuditTests(unittest.TestCase):
    def write(self, root, name, data):
        (Path(root) / name).write_text(json.dumps(data), encoding="utf-8")

    def test_receipt_counts_findings_per_block_and_proposes_a_rework_for_a_built_short_block(self):
        with TemporaryDirectory() as root:
            for kind, rates, head in (("brick", BRICK, "shared"), ("steel", STEEL, "split")):
                bank = furnace_block(KINDS[kind], 0, 20 * (kind == "steel"), head=head)
                self.write(root, f"planned-{kind}.json", {"bank": bank, "rated": rates, "phase": "verified"})
            receipt = ba.receipt(root)
            blocks = {b["block"]: b for b in receipt["blocks"]}
            self.assertEqual(
                blocks["brick_production"]["counts"], {"single_lane_feed": 2, "side_load_before_splitter": 2}
            )
            self.assertEqual(blocks["steel_production"]["counts"], {})
            self.assertEqual(
                receipt["counts_by_type"], {"furnace_block": {"single_lane_feed": 2, "side_load_before_splitter": 2}}
            )
            self.assertEqual([r["block"] for r in receipt["rework"]], ["brick_production"])
            rework = receipt["rework"][0]
            self.assertEqual((rework["status"], rework["cause"]), ("proposed", "side_load_before_splitter"))
            self.assertEqual(ba.run_counts(root), receipt["counts_by_type"])
            lines = ba.format_counts([{"bandwidth": ba.run_counts(root)}, {"bandwidth": {}}])
            self.assertEqual(lines[0], "belt bandwidth findings (block type / finding: count in runs):")
            self.assertEqual(len(lines), 3)

    def test_a_tap_without_its_trunk_gets_a_full_trunk_and_serves_after_the_primary_consumer(self):
        # Material-supply coal hub (eval-4177156614): a "serve after" tap cut into the coal trunk,
        # branch head-on into an even hub splitter with two outputs.
        tap = {
            "item": "coal",
            "demand_per_minute": 60.0,
            "primary_demand": 60.0,
            "port": {"position": {"x": -97.5, "y": 29.5}},
            "splitter": {
                "name": "splitter",
                "direction": 12,
                "position": {"x": -95.5, "y": 28.0},
                "splitter_output_priority": "right",
            },
            "entities": [
                {
                    "name": "splitter",
                    "direction": 12,
                    "position": {"x": -95.5, "y": 28.0},
                    "splitter_output_priority": "right",
                },
                {"name": "transport-belt", "direction": 12, "position": {"x": -96.5, "y": 28.5}},
                {"name": "transport-belt", "direction": 8, "position": {"x": -97.5, "y": 28.5}},
                {"name": "transport-belt", "direction": 8, "position": {"x": -97.5, "y": 29.5}},
            ],
        }
        hub = {
            "entities": [
                {"name": "splitter", "direction": 8, "position": {"x": -98.0, "y": 30.5}},
                {"name": "transport-belt", "direction": 8, "position": {"x": -97.5, "y": 31.5}},
                {"name": "transport-belt", "direction": 8, "position": {"x": -98.5, "y": 31.5}},
            ],
            "ports": {
                "coal": {"direction": "output", "item": "coal", "kind": "item", "position": {"x": -97.5, "y": 31.5}},
                "brick-coal": {
                    "direction": "output",
                    "item": "coal",
                    "kind": "item",
                    "position": {"x": -98.5, "y": 31.5},
                },
            },
        }
        with TemporaryDirectory() as root:
            self.write(root, "material-supply.json", {"supplies": {"coal": {"tap": tap, "hub": hub}}})
            (row,) = ba.feed_rows(Path(root))
            self.assertEqual(row["findings"], [])
            self.assertEqual([p["per_minute"] for p in row["ports"]], [417.0, 417.0])  # (900 - 60 x 1.1) / 2

    def test_a_broken_record_is_a_row_not_a_crash(self):
        with TemporaryDirectory() as root:
            self.write(
                root,
                "planned-brick.json",
                {"bank": {"entities": [{"name": "transport-belt", "position": {"x": "?", "y": 0}}]}},
            )
            rows = ba.audit_root(root)
            self.assertEqual([r["type"] for r in rows], ["error"])


if __name__ == "__main__":
    unittest.main()
