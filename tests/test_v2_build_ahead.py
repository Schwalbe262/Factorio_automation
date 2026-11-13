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


if __name__ == "__main__":
    unittest.main()
