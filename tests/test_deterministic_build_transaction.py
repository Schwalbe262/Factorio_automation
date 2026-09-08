"""Model engine ticks between RCON calls using the emitted Lua stop guard.

Placement and walking are fixture physics. The condition and position of the
stop block come from the actual query; a separate inert Lua replay verifies it
without changing the live character.
"""
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_navigation import CharacterNavigator


FREEZE = re.compile(
    r'if (?P<condition>[^\n]+) then\n'
    r'(?: --\[\[.*?\]\]\n)?'
    r' if d then d.motion=nil end;a.walking_state=\{walking=false\};a.mining_state=\{mining=false\}\nend',
    re.S,
)


class WalkingBuildFixture:
    def __init__(self, *, former_query=False, distant=False, obstruction=False):
        self.former_query = former_query
        self.obstruction = obstruction
        self.position = {"x": 20.0 if distant else 37.0, "y": -6.265625}
        self.motion = {"kind": "move", "path": [{"x": 38.5, "y": -7.0}]}
        self.original_motion = self.motion
        self.walking = True
        self.mining = False
        self.boilers = 1
        self.built = 0
        self.at_placement = []
        self.inputs = []
        self.queries = []
        self.reach_state = None
        self.game = SimpleNamespace(backend="character", query=Mock(side_effect=self.query),
                                    act=Mock(side_effect=self.act))
        self.navigator = CharacterNavigator(self.game)
        self.navigator.pending_action = Mock(return_value=None)
        self.navigator._input = Mock(side_effect=self.input)

    def clear(self):
        self.motion = None
        self.walking = self.mining = False

    def can_place(self):
        # Actual quantized boiler and actor boxes from the read-only diagnosis.
        x, y = self.position["x"], self.position["y"]
        overlap = (x + .19921875 >= 37.2109375 and x - .19921875 <= 39.7890625
                   and y + .19921875 >= -7.7890625 and y - .19921875 <= -6.2109375)
        self.at_placement.append((self.walking, self.motion))
        return not (overlap or self.obstruction)

    def query(self, body):
        self.queries.append(body)
        if "local actor_box=a.bounding_box" not in body:
            self.clear()  # Existing final cleanup query, after the reach query.
            return {"ok": True}
        encoded = re.search(r'helpers.json_to_table\(("(?:[^"\\]|\\.)*")\)', body).group(1)
        action = json.loads(json.loads(encoded))
        within = sum((action["position"][axis] - self.position[axis]) ** 2 for axis in ("x", "y")) <= 100
        if self.former_query:
            body = FREEZE.sub("", body, count=1)
        freeze = FREEZE.search(body)
        if freeze:
            if freeze.start() >= body.index("and not s.can_place_entity"):
                raise AssertionError("movement stop must precede placement observation")
            condition = freeze["condition"].replace("x.type", "kind")
            if eval(condition, {"__builtins__": {}}, {"within": within, "kind": action["type"]}):
                self.clear()
        placeable = self.can_place() if within and action["type"] == "build" else True
        self.reach_state = (self.walking, self.motion, placeable)
        # The engine can keep walking after this query returns, before the
        # navigator's separate cleanup/build commands reach the server.
        if self.walking:
            self.position["x"] += .9296875
        return {"ok": True, "within": within}

    def input(self, action):
        self.inputs.append(action)
        return {"ok": True, "status": "running", "path_reused": self.motion is self.original_motion}

    def act(self, action):
        if action["type"] != "build":
            return {"ok": True}
        if not self.can_place():
            return {"ok": False, "reason": "placement_blocked"}
        if self.boilers < 1:
            return {"ok": False, "reason": "missing_item:boiler"}
        self.boilers -= 1
        self.built += 1
        return {"ok": True, "status": "succeeded"}


class BuildTransactionTests(unittest.TestCase):
    action = {"type": "build", "name": "boiler", "item": "boiler",
              "position": {"x": 38.5, "y": -7.0}, "direction": 0}

    def test_former_query_allows_walk_into_footprint_before_build_but_new_query_stops_it(self):
        old = WalkingBuildFixture(former_query=True)
        self.assertEqual(old.navigator.execute(self.action, {})["reason"], "placement_blocked")
        self.assertTrue(old.reach_state[2])  # Placement was clear during reach.
        self.assertEqual((old.boilers, old.built), (1, 0))
        new = WalkingBuildFixture()
        self.assertEqual(new.navigator.execute(self.action, {})["status"], "succeeded")
        self.assertEqual(new.reach_state, (False, None, True))
        self.assertEqual(new.at_placement, [(False, None), (False, None)])
        self.assertEqual(new.position["x"], 37.0)
        self.assertEqual((new.boilers, new.built), (0, 1))
        new.game.act.assert_called_once_with(self.action)
        self.assertEqual(new.inputs, [])

    def test_distant_build_keeps_existing_walk_and_reuses_its_path(self):
        fixture = WalkingBuildFixture(distant=True)
        result = fixture.navigator.execute(self.action, {})
        self.assertTrue(result["path_reused"])
        self.assertEqual(fixture.inputs, [{"type": "move", "position": self.action["position"]}])
        self.assertIs(fixture.motion, fixture.original_motion)
        self.assertTrue(fixture.walking)
        fixture.game.act.assert_not_called()

    def test_non_build_reach_observations_do_not_stop_motion(self):
        for kind in ("take", "insert", "mine"):
            with self.subTest(kind=kind):
                fixture = WalkingBuildFixture()
                fixture.navigator.execute({**self.action, "type": kind}, {})
                self.assertTrue(fixture.reach_state[0])
                self.assertIs(fixture.reach_state[1], fixture.original_motion)

    def test_pending_finite_mining_batch_is_continued_before_any_build_query(self):
        fixture = WalkingBuildFixture()
        pending = {"type": "mine", "name": "coal", "position": {"x": 20, "y": 5}, "count": 24}
        fixture.navigator.pending_action.return_value = pending
        result = fixture.navigator.execute(self.action, {"inventory": {"coal": 1}})
        self.assertEqual(result["continued_action"], pending)
        self.assertEqual(fixture.inputs, [pending])
        fixture.game.query.assert_not_called()
        fixture.game.act.assert_not_called()

    def test_other_obstruction_still_fails_normal_placement_without_spending_item(self):
        fixture = WalkingBuildFixture(obstruction=True)
        result = fixture.navigator.execute(self.action, {})
        self.assertEqual(result["reason"], "placement_blocked")
        self.assertEqual((fixture.boilers, fixture.built), (1, 0))
        self.assertEqual(fixture.inputs, [])


if __name__ == "__main__":
    unittest.main()
