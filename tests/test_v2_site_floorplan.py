"""Planned floor: slots by production chain, growth frames, unused candidates released."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from factorio_agent.floor_blocks import CAPABILITY_BLOCKS, floor_blocks
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.floorplan import FloorBlock, base_name, slot_of
from factorio_agent.site.floorsim import simulate
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import MasterPlan, plan_site
from factorio_agent.site.planmodel import SiteConfig, SiteError
from factorio_agent.site.service import PLAN_FILE, SiteService
from factorio_agent.site.worldmap import Feature, WorldMap


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def block(name, along, rank, *, growth=0, inputs=(), private=(), outputs=(), names=None, depth=20):
    return FloorBlock(
        names=names or (name,),
        kind="production",
        along=along,
        depth=depth,
        growth=growth,
        inputs=inputs,
        private=private,
        outputs=outputs,
        rank=rank,
    )


CHAIN = (
    block("science", 30, 0, inputs=("iron-plate",), outputs=("pack",)),
    block("steel", 30, 1, inputs=("iron-plate",), outputs=("steel-plate",)),
    block("filler_a", 30, 2, inputs=("iron-plate",), outputs=("a",)),
    block("oil", 30, 3, outputs=("petroleum-gas",)),
    block("filler_b", 30, 4, inputs=("iron-plate",), outputs=("b",)),
    block("plastic", 20, 5, inputs=("petroleum-gas",), private=("petroleum-gas",), outputs=("plastic",)),
    block(
        "engine",
        24,
        6,
        growth=18,
        inputs=("steel-plate",),
        outputs=("engine-unit",),
        names=("engine", "engine__belt"),
    ),
)


def placed(plan, name):
    return next(a for a in plan.allocations if base_name(a.name) == name)


class FloorAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.world = blank()
        self.plan = plan_site(self.world, floor_blocks=CHAIN)

    def test_private_link_partners_are_neighbours_on_one_side(self):
        oil, plastic = slot_of(self.plan, "oil"), slot_of(self.plan, "plastic")
        self.assertEqual(oil.side, plastic.side)
        self.assertEqual(plastic.along0, oil.along1 + self.plan.config.district_gap)

    def test_bus_lane_consumer_sits_downstream_of_its_producer(self):
        self.assertIn("steel-plate", {lane.material for lane in self.plan.bus.lanes})
        steel, engine = slot_of(self.plan, "steel"), slot_of(self.plan, "engine")
        self.assertGreaterEqual(engine.along0, steel.along0 + steel.built)

    def test_frame_includes_growth_and_early_blocks_sit_at_the_head(self):
        engine = slot_of(self.plan, "engine")
        self.assertEqual(engine.along1 - engine.along0, 24 + 18)
        self.assertEqual(slot_of(self.plan, "science").along0, 0)

    def test_floor_round_trips_and_old_plans_have_none(self):
        again = MasterPlan.from_json(self.plan.to_json())
        self.assertEqual(again.floor, self.plan.floor)
        data = self.plan.to_dict()
        data.pop("floor")
        self.assertIsNone(MasterPlan.from_dict(data).floor)
        data["floor"] = {"version": 999, "slots": []}
        self.assertIsNone(MasterPlan.from_dict(data).floor)

    def test_no_floor_blocks_keeps_first_fit(self):
        world = blank()
        plan = plan_site(world)
        self.assertIsNone(plan.floor)
        first = plan.allocate(world, "x", 40, 30, name="oil#0")
        self.assertEqual((first.along0, first.growth), (0, 0))


class FloorAllocationTests(unittest.TestCase):
    def setUp(self):
        self.world = blank()
        self.plan = plan_site(self.world, floor_blocks=CHAIN)

    def test_block_gets_its_slot_whatever_the_arrival_order(self):
        plastic = self.plan.allocate(self.world, "production", 20, 20, name="plastic#0")
        slot = slot_of(self.plan, "plastic")
        self.assertEqual((plastic.side, plastic.along0), (slot.side, slot.along0))
        other = self.plan.allocate(self.world, "production", 20, 20, name="plastic#1")
        self.assertNotEqual((other.side, other.along0), (slot.side, slot.along0))
        self.assertEqual(self.plan.validate(self.world), [])

    def test_unknown_block_keeps_off_unclaimed_slots(self):
        unknown = self.plan.allocate(self.world, "production", 16, 16, name="mall#0")
        for slot in self.plan.floor:
            if slot.side == unknown.side:
                self.assertTrue(
                    unknown.along1 + self.plan.config.district_gap <= slot.along0
                    or slot.along1 + self.plan.config.district_gap <= unknown.along0,
                    (unknown, slot),
                )

    def test_growth_room_is_held_and_extend_uses_it(self):
        engine = self.plan.allocate(self.world, "production", 24, 20, name="engine#0")
        self.assertEqual(engine.growth, 18)
        grown = self.plan.extend(self.world, "engine#0", 10)
        self.assertEqual((grown.along1, grown.growth), (engine.along1 + 10, 8))
        self.assertEqual(self.plan.validate(self.world), [])

    def test_belt_row_takes_the_frame_room_and_gives_it_back(self):
        engine = self.plan.allocate(self.world, "production", 24, 20, name="engine#0")
        belt = self.plan.allocate(self.world, "production", 14, 20, name="engine__belt#0")
        gap = self.plan.config.district_gap
        self.assertEqual((belt.side, belt.along0), (engine.side, engine.along1 + gap))
        self.assertEqual(placed(self.plan, "engine").growth, 0)
        self.plan.release(self.world, belt.name)
        self.assertEqual(placed(self.plan, "engine").growth, 18)

    def test_blocked_slot_moves_along_the_bus_on_its_side(self):
        slot = slot_of(self.plan, "plastic")
        cliff = self.plan.bus.side_rect(slot.side, slot.along0 + 2, slot.along0 + 4, 8, 10)
        self.world.add_feature(Feature("cliff", "cliff", cliff))
        plastic = self.plan.allocate(self.world, "production", 20, 20, name="plastic#0")
        self.assertEqual(plastic.side, slot.side)
        self.assertGreater(plastic.along0, slot.along0)
        self.assertFalse(plastic.rect.intersects(cliff))

    def test_unknown_blocks_take_late_slots_then_growth_room_before_failing(self):
        config = SiteConfig(bus_target_length=160, bus_min_length=160)
        world = blank()
        plan = plan_site(world, config=config, floor_blocks=CHAIN)
        plan.allocate(world, "production", 24, 20, name="engine#0")
        count = 0
        with self.assertRaises(SiteError):
            for index in range(20):
                plan.allocate(world, "production", 30, 20, name=f"late{index}#0")
                count += 1
        # The engine row stands at its slot (downstream of steel), so 2 x 160 tiles hold 7 blocks of
        # 30 beside it: every unclaimed slot and the engine's growth room were given up first.
        self.assertEqual(count, 7)
        engine = placed(plan, "engine")
        self.assertEqual(engine.growth, 0)
        self.assertTrue(any(a.along0 == engine.along1 + plan.config.district_gap for a in plan.allocations))
        self.assertEqual(plan.validate(world), [])


class FakeGame:
    def __init__(self, root):
        self.cfg = SimpleNamespace(runtime_dir=root)

    def query(self, body):
        return {"ok": True, "world_id": "w1", "x": 0, "y": 0}


def survey(game, rect):
    return make_world(3, size=512)


class ServiceTests(unittest.TestCase):
    def test_old_plan_gets_a_floor_without_moving_its_blocks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = SiteService(FakeGame(root), half_size=256, surveyor=survey)
            first = old.allocate_candidates("production", 30, 20, name="steel", count=2)
            old.keep_only("steel#", first[0].name)
            data = json.loads((root / PLAN_FILE).read_text())
            data["plan"].pop("floor")
            for row in data["plan"]["allocations"]:
                row.pop("settled", None)
            (root / PLAN_FILE).write_text(json.dumps(data))
            new = SiteService(FakeGame(root), half_size=256, surveyor=survey, floor_blocks=CHAIN)
            plan = new.plan()
            self.assertIsNotNone(plan.floor)
            steel = slot_of(plan, "steel")
            self.assertTrue(steel.fixed)
            self.assertEqual((steel.side, steel.along0), (first[0].side, first[0].along0))
            self.assertEqual(new.allocation(first[0].name).rect, first[0].rect)
            plastic = new.allocate_candidates("production", 20, 20, name="plastic", count=1)[0]
            self.assertEqual(
                (plastic.side, plastic.along0), (slot_of(plan, "plastic").side, slot_of(plan, "plastic").along0)
            )

    def test_unsettled_candidates_are_released_after_a_step(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = SiteService(FakeGame(root), half_size=256, surveyor=survey, floor_blocks=CHAIN)
            kept = site.allocate_candidates("production", 20, 20, name="plastic", count=3)
            site.keep_only("plastic#", kept[0].name)
            site.allocate_candidates("production", 30, 20, name="advanced", count=4)
            released = site.release_unsettled()
            self.assertEqual(len(released), 4)
            self.assertEqual([a.name for a in site.plan().allocations], [kept[0].name])
            self.assertIs(site.allocation(kept[0].name).settled, True)

    def test_legacy_candidate_groups_released_unless_a_state_names_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = SiteService(FakeGame(root), half_size=256, surveyor=survey)
            site.allocate_candidates("production", 30, 20, name="advanced", count=3)
            site.allocate_candidates("production", 30, 20, name="steel", count=2)
            data = json.loads((root / PLAN_FILE).read_text())
            for row in data["plan"]["allocations"]:
                row.pop("settled", None)
            (root / PLAN_FILE).write_text(json.dumps(data))
            (root / "planned-steel.json").write_text(json.dumps({"site_allocation": "steel#1"}))
            again = SiteService(FakeGame(root), half_size=256, surveyor=survey)
            again.plan()
            released = again.release_unsettled()
            self.assertEqual(sorted(released), ["advanced#0", "advanced#1", "advanced#2", "steel#0"])
            self.assertEqual([a.name for a in again.plan().allocations], ["steel#1"])


class FloorBlocksAndSweepTests(unittest.TestCase):
    def test_roadmap_blocks_in_rank_order_with_controller_names(self):
        blocks = floor_blocks()
        names = [b.name for b in blocks]
        self.assertEqual(names[:2], ["production_row_inserter", "production_row_transport-belt"])
        for spec in CAPABILITY_BLOCKS.values():
            self.assertIn(spec[0][0], names)
        self.assertEqual([b.rank for b in blocks], sorted(b.rank for b in blocks))
        engine = next(b for b in blocks if b.name == "production_row_engine-unit")
        self.assertEqual(engine.names[1], "production_row_engine-unit__belt")
        self.assertGreater(engine.growth, 14)

    def test_floor_beats_first_fit_offline(self):
        blocks = floor_blocks()
        config = SiteConfig()
        for seed in (2, 5):
            with self.subTest(seed=seed):
                before = simulate(make_world(seed, size=512), config, blocks, use_floor=False)
                after = simulate(make_world(seed, size=512), config, blocks, use_floor=True)
                self.assertEqual(after.notes, [])
                self.assertLessEqual(after.failures, before.failures)
                self.assertLess(after.link_sum, before.link_sum)
                self.assertLessEqual(after.crossings, before.crossings)
                self.assertGreater(after.grown, before.grown)


if __name__ == "__main__":
    unittest.main()
