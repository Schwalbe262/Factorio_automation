"""SiteService: cached survey + master plan per runtime."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.service import MAP_FILE, PLAN_FILE, SiteService
from factorio_agent.site.worldmap import Feature

SEED = 3


class FakeGame:
    def __init__(self, root, world_id="w1"):
        self.cfg = SimpleNamespace(runtime_dir=root)
        self.world_id = world_id
        self.queries = 0

    def query(self, body):
        self.queries += 1
        return {"ok": True, "world_id": self.world_id, "x": 0, "y": 0}


class Counter:
    def __init__(self, extra=None):
        self.calls = 0
        self.extra = extra

    def survey(self, game, rect):
        self.calls += 1
        world = make_world(SEED, size=512)
        if self.extra is not None:
            self.extra(world)
        return world


def service(root, world_id="w1", counter=None, **kwargs):
    counter = counter or Counter()
    game = FakeGame(root, world_id)
    return SiteService(game, half_size=256, surveyor=counter.survey, **kwargs), counter, game


class SiteServiceTests(unittest.TestCase):
    def test_lazy_survey_is_cached_per_world(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, counter, game = service(root)
            self.assertEqual(counter.calls, 0)
            plan = first.plan()
            self.assertEqual(counter.calls, 1)
            self.assertIsNotNone(first.zone("smelting"))
            self.assertEqual(json.loads((root / PLAN_FILE).read_text())["world_id"], "w1")
            self.assertEqual(json.loads((root / MAP_FILE).read_text())["world_id"], "w1")
            again, counter2, _ = service(root)
            self.assertEqual(again.plan("w1").to_dict(), plan.to_dict())
            self.assertEqual(counter2.calls, 0)
            other, counter3, _ = service(root, world_id="w2")
            other.plan("w2")
            self.assertEqual(counter3.calls, 1)
            self.assertEqual(json.loads((root / PLAN_FILE).read_text())["world_id"], "w2")

    def test_wrong_expected_world_raises_instead_of_planning(self):
        with TemporaryDirectory() as directory:
            site, counter, _ = service(Path(directory), world_id="w1")
            with self.assertRaises(Exception):
                site.plan("other")
            self.assertEqual(counter.calls, 0)

    def test_allocations_persist_and_repeat_calls_are_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site, _, _ = service(root)
            block = site.allocate("science", 32, 32, name="red", returns_to_bus=False)
            self.assertEqual(site.allocate("science", 32, 32, name="red"), block)
            options = site.allocate_candidates("green", 32, 32, name="green", count=3)
            self.assertEqual(len({o.rect for o in options}), len(options))
            site.keep_only("green#", options[-1].name)
            reloaded, counter, _ = service(root)
            names = [a.name for a in reloaded.plan("w1").allocations]
            self.assertEqual(names, ["red", options[-1].name])
            self.assertEqual(counter.calls, 0)
            self.assertEqual(reloaded.plan().validate(reloaded.world()), [])
            reloaded.release("red")
            self.assertIsNone(service(root)[0].allocation("red", "w1"))

    def test_existing_entities_are_occupied_tiles(self):
        with TemporaryDirectory() as directory:
            site, _, _ = service(Path(directory) / "a")
            free = site.allocate("x", 24, 16, name="a")

            def build_there(world):
                world.add_feature(
                    Feature(
                        "entity", "wooden-chest", Rect(free.rect.x0, free.rect.y0, free.rect.x0 + 1, free.rect.y0 + 1)
                    )
                )

            blocked, _, _ = service(Path(directory) / "b", counter=Counter(build_there))
            moved = blocked.allocate("x", 24, 16, name="a")
            self.assertFalse(moved.rect.contains(free.rect.x0, free.rect.y0))

    def test_refresh_resurveys_and_saves(self):
        with TemporaryDirectory() as directory:
            seen = []
            site, _, _ = service(Path(directory), updater=lambda world, game, rect: seen.append(rect))
            site.plan()
            site.refresh(Rect(0, 0, 32, 32))
            self.assertEqual(seen, [Rect(0, 0, 32, 32)])
            self.assertEqual(site.surveys, 2)


if __name__ == "__main__":
    unittest.main()
