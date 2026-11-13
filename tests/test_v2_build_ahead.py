"""FactoryBuilder.ensure_plan builds carried plan entities while an earlier one waits for its item."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factorio_agent.core.deterministic_builder import BUILD_AHEAD_CHECKS, FactoryBuilder

WAIT = {"status": "waiting", "reason": "waiting for drill-fed iron-plate production"}
CATALOG = SimpleNamespace(
    fingerprint="c",
    entities={
        "transport-belt": {"type": "transport-belt"},
        "boiler": {"type": "boiler"},
        "steam-engine": {"type": "generator"},
        "inserter": {"type": "inserter"},
        "electric-mining-drill": {"type": "mining-drill"},
    },
)


class Game:
    backend = "assisted"

    def __init__(self, root, blocked=()):
        self.cfg = SimpleNamespace(runtime_dir=str(root))
        self.blocked = set(blocked)
        self.queries = 0

    def query(self, body):
        self.queries += 1
        return {"ok": not any(name in body for name in self.blocked)}


class Bootstrap:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def ensure_item(self, observation, item, count):
        self.calls.append((item, count))
        return dict(self.result)


def entity(name, x, y=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": 0}


class BuildAheadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def builder(self, result=WAIT, blocked=()):
        self.game = Game(self.root, blocked)
        self.bootstrap = Bootstrap(result)
        return FactoryBuilder(self.game, self.bootstrap, CATALOG)

    def obs(self, inventory):
        return {"world_id": "w", "tick": 10, "entities": [], "inventory": inventory}

    def plan(self, *entities):
        return {"ok": True, "entities": list(entities)}

    def test_waiting_for_belts_places_a_carried_boiler(self):
        plan = self.plan(entity("transport-belt", 0), entity("transport-belt", 1), entity("boiler", 5))
        action = self.builder().ensure_plan(self.obs({"boiler": 1}), plan)
        self.assertEqual((action["type"], action["name"], action["position"]), ("build", "boiler", {"x": 5, "y": 0}))
        self.assertEqual(self.bootstrap.calls, [("transport-belt", 2)])

    def test_a_real_acquisition_action_is_returned_unchanged(self):
        craft = {"type": "craft", "recipe": "transport-belt", "count": 1}
        plan = self.plan(entity("transport-belt", 0), entity("boiler", 5))
        self.assertEqual(self.builder(result=craft).ensure_plan(self.obs({"boiler": 1}), plan), craft)

    def test_blocked_acquisition_is_not_overridden(self):
        blocked = {"status": "blocked", "reason": "required recipe is locked"}
        plan = self.plan(entity("transport-belt", 0), entity("boiler", 5))
        self.assertEqual(self.builder(result=blocked).ensure_plan(self.obs({"boiler": 1}), plan), blocked)

    def test_drills_and_inserters_keep_plan_order(self):
        plan = self.plan(entity("transport-belt", 0), entity("inserter", 2), entity("electric-mining-drill", 6))
        result = self.builder().ensure_plan(self.obs({"inserter": 1, "electric-mining-drill": 1}), plan)
        self.assertEqual(result, WAIT)

    def test_obstructed_or_uncarried_later_entities_leave_the_wait(self):
        plan = self.plan(entity("transport-belt", 0), entity("boiler", 5), entity("steam-engine", 9))
        self.assertEqual(self.builder(blocked={"boiler"}).ensure_plan(self.obs({"boiler": 1}), plan), WAIT)
        self.assertEqual(self.builder().ensure_plan(self.obs({}), plan), WAIT)

    def test_placement_checks_are_bounded(self):
        boilers = [entity("boiler", 10 + 4 * i) for i in range(BUILD_AHEAD_CHECKS + 3)]
        plan = self.plan(entity("transport-belt", 0), *boilers)
        result = self.builder(blocked={"boiler"}).ensure_plan(self.obs({"boiler": 9}), plan)
        self.assertEqual(result, WAIT)
        self.assertEqual(self.game.queries, BUILD_AHEAD_CHECKS)


class BlockingGame(Game):
    """The batch placement query (``can_place`` of several specs) rejects the given positions."""

    def __init__(self, root, positions):
        super().__init__(root)
        self.positions = positions

    def query(self, body):
        self.queries += 1
        blocked = [
            {"name": "transport-belt", "position": {"x": x, "y": 0}, "reason": "terrain_or_entity_collision"}
            for x in self.positions
            if f'\\"x\\":{x},' in body
        ]
        return {"ok": not blocked, "blocked": blocked}


class BuildBatchTests(BuildAheadTests):
    """One build_many round trip for every carried plan entity (deterministic_builder._affordable_builds)."""

    def names(self, action):
        return [(row["name"], row["position"]["x"]) for row in action["actions"]]

    def test_missing_items_are_passed_over_not_a_stop(self):
        catalog = dict(CATALOG.entities, **{"stone-furnace": {"type": "furnace"}, "splitter": {"type": "splitter"}})
        plan = self.plan(
            entity("transport-belt", 0),
            entity("pipe", 1),  # not carried: passed over
            entity("transport-belt", 2),
            entity("stone-furnace", 3),
            entity("inserter", 5),  # carried, but behind a passed-over entity: keeps plan order
            entity("splitter", 6),
        )
        builder = self.builder()
        builder.catalog = SimpleNamespace(fingerprint="c", entities=catalog)
        action = builder.ensure_plan(
            self.obs({"transport-belt": 5, "stone-furnace": 1, "inserter": 1, "splitter": 1}), plan
        )
        self.assertEqual(action["type"], "build_many")
        self.assertEqual(
            self.names(action),
            [("transport-belt", 0), ("transport-belt", 2), ("stone-furnace", 3), ("splitter", 6)],
        )
        self.assertTrue(all(row["type"] == "build" for row in action["actions"]))  # ordinary paid builds

    def test_waiting_entity_lets_all_carried_belts_go_in_one_trip(self):
        belts = [entity("transport-belt", x) for x in range(1, 9)]
        plan = self.plan(entity("inserter", 0), *belts, entity("inserter", 20))
        action = self.builder().ensure_plan(self.obs({"transport-belt": 8, "inserter": 0}), plan)
        self.assertEqual(action["type"], "build_many")
        self.assertEqual([x for _, x in self.names(action)], list(range(1, 9)))
        self.assertIn("waits for its item", action["reason"])
        self.assertEqual(self.bootstrap.calls, [("inserter", 2)])

    def test_rejected_placements_leave_the_batch(self):
        self.builder()
        self.game = BlockingGame(self.root, positions=(2,))
        builder = FactoryBuilder(self.game, self.bootstrap, CATALOG)
        plan = self.plan(*(entity("transport-belt", x) for x in range(4)))
        action = builder.ensure_plan(self.obs({"transport-belt": 4}), plan)
        self.assertEqual(action["type"], "build_many")
        self.assertEqual([x for _, x in self.names(action)], [0, 1, 3])

    def test_deferred_items_are_passed_over(self):
        builder = self.builder()
        rows = builder._affordable_builds(
            self.obs({"transport-belt": 2, "inserter": 2}),
            [entity("transport-belt", 0), entity("inserter", 1), entity("transport-belt", 2)],
            skipped=frozenset({"pipe"}),
        )
        self.assertEqual([r["position"]["x"] for r in rows], [0, 2])

    def test_batch_is_bounded(self):
        from factorio_agent.core.deterministic_game import BUILD_BATCH_LIMIT

        plan = self.plan(*(entity("transport-belt", x) for x in range(BUILD_BATCH_LIMIT + 8)))
        action = self.builder().ensure_plan(self.obs({"transport-belt": 100}), plan)
        self.assertEqual(len(action["actions"]), BUILD_BATCH_LIMIT)


class CraftReserveTests(BuildAheadTests):
    """Fleet all-1114-0256: the district's underground pair waited while build ahead placed its belts."""

    def catalog(self):
        entities = dict(CATALOG.entities, **{"underground-belt": {"type": "underground-belt"}})
        recipes = {
            "underground-belt": {
                "ingredients": [
                    {"type": "item", "name": "iron-plate", "amount": 10},
                    {"type": "item", "name": "transport-belt", "amount": 5},
                ],
                "products": [{"name": "underground-belt", "amount": 2}],
            }
        }
        return SimpleNamespace(fingerprint="c", entities=entities, recipes=recipes)

    def plan_with_pair(self, belts):
        pair = [
            dict(entity("underground-belt", 0), belt_to_ground_type="input"),
            dict(entity("underground-belt", 3), belt_to_ground_type="output"),
        ]
        return self.plan(*pair, *(entity("transport-belt", 10 + x) for x in range(belts)))

    def test_the_waiting_craft_keeps_its_carried_ingredients(self):
        builder = self.builder()
        builder.catalog = self.catalog()
        self.assertEqual(builder.craft_reserve("underground-belt", 2), {"iron-plate": 10, "transport-belt": 5})
        self.assertEqual(builder.craft_reserve("underground-belt", 3), {"iron-plate": 20, "transport-belt": 10})
        # 5 carried belts are the pair's craft: nothing is built ahead, the wait stands
        result = builder.ensure_plan(self.obs({"transport-belt": 5}), self.plan_with_pair(4))
        self.assertEqual(result, WAIT)
        self.assertEqual(self.bootstrap.calls, [("underground-belt", 2)])

    def test_belts_beyond_the_reserve_are_still_built_ahead(self):
        builder = self.builder()
        builder.catalog = self.catalog()
        action = builder.ensure_plan(self.obs({"transport-belt": 8}), self.plan_with_pair(6))
        self.assertEqual(action["type"], "build_many")
        self.assertEqual([x for _, x in BuildBatchTests.names(None, action)], [10, 11, 12])

    def test_a_catalog_without_the_recipe_reserves_nothing(self):
        builder = self.builder()
        self.assertEqual(builder.craft_reserve("underground-belt", 2), {})
        action = builder.ensure_plan(self.obs({"transport-belt": 5}), self.plan_with_pair(4))
        self.assertEqual(action["type"], "build_many")


class GroundItemGame(Game):
    """Placement fails (an item stack lies on the tile); the footprint survey finds only that stack."""

    def query(self, body):
        self.queries += 1
        if "find_entities_filtered" in body:
            stack = {"name": "item-on-ground", "position": {"x": 97.5, "y": -40.5}, "item": "firearm-magazine"}
            return {"ok": True, "world_id": "w", "ground": [dict(stack, quality="normal", count=1)]}
        return {"ok": False, "blocked": [{"name": "iron-chest", "reason": "terrain_or_entity_collision"}]}


class ChestOrderTests(BuildAheadTests):
    """Seeds 1024125955 / 1188507449 (long-1114-0039): the magazine row's chest blocked for good."""

    def row(self):
        arm = dict(entity("inserter", 97.5, -39.5), direction=8)
        chest = entity("iron-chest", 97.5, -40.5)
        pole = entity("small-electric-pole", 96.5, -39.5)
        return arm, chest, pole

    def test_a_chest_is_built_before_the_inserter_that_drops_into_it(self):
        from factorio_agent.core.deterministic_builder import containers_before_their_inserters

        arm, chest, pole = self.row()
        belt = entity("transport-belt", 90.5, -30.5)
        self.assertEqual(containers_before_their_inserters([belt, arm, chest, pole]), [belt, chest, arm, pole])
        aside = entity("iron-chest", 99.5, -39.5)  # not on the inserter's axis: order kept
        self.assertEqual(containers_before_their_inserters([arm, aside]), [arm, aside])
        builder = self.builder()
        action = builder.ensure_plan(self.obs({"inserter": 1, "iron-chest": 1}), self.plan(arm, chest, pole))
        first = action["actions"][0] if action["type"] == "build_many" else action
        self.assertEqual((first["name"], first["position"]), ("iron-chest", chest["position"]))

    def test_items_lying_on_a_reserved_tile_are_picked_up_first(self):
        _, chest, _ = self.row()
        builder = self.builder()
        builder.game = GroundItemGame(self.root)
        action = builder.ensure_plan(self.obs({"iron-chest": 1}), self.plan(chest))
        self.assertEqual(
            (action["type"], action["item"], action["position"]), ("take", "firearm-magazine", chest["position"])
        )
        self.assertIn("reserved iron-chest tile", action["reason"])


if __name__ == "__main__":
    unittest.main()
