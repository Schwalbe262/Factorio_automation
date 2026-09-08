from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_fluids import FluidProduction
from tests import test_deterministic_fluids as fixtures


class ReceiverEscapeTests(unittest.TestCase):
    def setUp(self):
        fixtures.FluidProductionTests.setUp(self)
        fixtures.FluidProductionTests.underground_geometry(self)
        self.source = {"kind": "fluid", "item": "water", "facing": 12, "position": {"x": 39.5, "y": 19.5}}
        self.destination = {"kind": "fluid", "item": "water", "facing": 4, "position": {"x": 28.5, "y": -10.5}}
        self.tap = {"x": 28.5, "y": 5.5}
        self.receiver = {"x": 29.5, "y": -10.5}
        self.fluids._sync(self.obs)

    def connect(self):
        return self.fluids._connect_pipe(self.obs, self.source, self.destination, "energy:water:3", {"entities": []})

    def test_south_receiver_escape_builds_typed_pair_before_source_retries_and_resumes(self):
        self.fluids._network_taps = Mock(return_value={"taps": [self.tap], "destination_taps": [self.receiver]})
        def place(rows):
            # The south corridor has room beyond its belt barrier at span four.
            return {"ok": rows[0]["position"] == {"x": 29.5, "y": -9.5}
                    and rows[1]["position"] == {"x": 29.5, "y": -5.5}}
        self.builder.can_place.side_effect = place
        def route(start, end, *args):
            return {"ok": end == {"x": 29.5, "y": -4.5}, "path": [start, end]}
        self.builder.route.side_effect = route
        self.builder.ensure_plan.return_value = {"type": "build", "name": "pipe-to-ground"}
        with self.subTest(stage="first ordinary construction"):
            self.assertEqual(self.connect()["type"], "build")
            self.assertEqual(self.builder.route.call_count, 3)
        link = deepcopy(self.fluids.state["links"]["energy:water:3"])
        pair = [e for e in link["entities"] if e["name"] == "pipe-to-ground"]
        self.assertEqual([(e["position"], e["direction"]) for e in pair],
                         [({"x": 29.5, "y": -9.5}, 0), ({"x": 29.5, "y": -5.5}, 8)])
        self.assertTrue(all(e["_fluid"] == "water" for e in link["entities"]))
        self.factory.register_plan.assert_called_once_with("fluid-link:energy:water:3", link, self.obs)
        self.fluids = FluidProduction(self.game, self.builder, self.catalog)
        self.fluids.factory = self.factory
        self.fluids._network_taps = Mock(return_value={"connected": False})
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.builder.route.reset_mock()
        self.assertEqual(self.connect()["status"], "blocked")
        self.assertEqual(self.fluids.state["links"]["energy:water:3"], link)
        self.builder.route.assert_not_called()
        self.fluids._network_taps.return_value = {"connected": True}
        self.assertEqual(self.connect()["status"], "succeeded")

    def test_receiver_representatives_are_distinct_verified_and_bounded(self):
        receivers = [{"x": x, "y": 0} for x in range(15)]
        pairs = [(self.tap, {"x": -1, "y": 0})] + [(tap, receiver) for receiver in receivers
                    for tap in (self.tap, {"x": 100, "y": 0})]
        original = deepcopy(pairs)
        self.fluids._underground_escape = Mock(return_value={"ok": False})
        self.fluids._destination_escape(self.obs, self.source, self.destination, [], pairs, receivers)
        calls = self.fluids._underground_escape.call_args_list
        self.assertEqual(len(calls), 12 * 4)
        self.assertEqual([c.args[2]["position"] for c in calls[::4]], receivers[:12])
        self.assertEqual([c.args[2]["facing"] for c in calls[:4]], [4, 0, 8, 12])
        self.assertTrue(all(c.args[1]["facing"] is None and c.args[1]["position"] == self.tap for c in calls))
        self.assertEqual(pairs, original)

    def test_unverified_receiver_is_not_promoted_and_locked_recipe_stops_early(self):
        self.fluids._underground_escape = Mock(return_value={"ok": False, "needs_recipe": "pipe-to-ground"})
        pairs = [(self.tap, self.receiver)]
        self.fluids._destination_escape(self.obs, self.source, self.destination, [], pairs, [])
        self.fluids._underground_escape.assert_not_called()
        result = self.fluids._destination_escape(self.obs, self.source, self.destination, [], pairs, [self.receiver])
        self.assertEqual(result["needs_recipe"], "pipe-to-ground")
        self.fluids._underground_escape.assert_called_once()

    def test_unsafe_receiver_rejects_every_escape_without_persisting(self):
        self.builder.route.return_value = {"ok": False}
        self.game.query.return_value = {"ok": False, "unsafe": True, "reason": "receiver contains steam"}
        self.fluids._underground_escape = Mock()
        self.assertEqual(self.connect()["reason"], "receiver contains steam")
        self.fluids._underground_escape.assert_not_called()
        self.factory.register_plan.assert_not_called()

    def test_continuation_cannot_use_discarded_fluid_adjacency_or_belt_front(self):
        source = {**self.source, "position": self.tap, "facing": None}
        destination = {**self.destination, "position": self.receiver, "facing": 0}
        self.catalog.entities["pipe-to-ground"]["fluidbox_prototypes"][0]["pipe_connections"][1]["max_underground_distance"] = 2
        # At span two, continuation is (29.5,-6.5).
        for name, position, direction in (("pipe", {"x": 30.5, "y": -6.5}, 0),
                                          ("pipe-to-ground", {"x": 30.5, "y": -6.5}, 0),
                                          ("transport-belt", {"x": 30.5, "y": -6.5}, 12)):
            for saved in (False, True):
                with self.subTest(name=name, saved=saved):
                    row = {"name": name, "position": position, "direction": direction}
                    self.obs["entities"] = [] if saved else [row]
                    self.builder.route.reset_mock()
                    self.assertFalse(self.fluids._underground_escape(self.obs, source, destination, [row] if saved else [])["ok"])
                    self.builder.route.assert_not_called()

    def test_unused_machine_connection_remains_a_reserved_continuation(self):
        source = {**self.source, "position": self.tap, "facing": None}
        destination = {**self.destination, "position": self.receiver, "facing": 0}
        self.catalog.entities["pipe-to-ground"]["fluidbox_prototypes"][0]["pipe_connections"][1]["max_underground_distance"] = 2
        marker = {"name": "reserved-fluid-connection", "position": {"x": 29.5, "y": -6.5}}
        self.builder._occupied_by_plan.side_effect = lambda rows: {(r["position"]["x"], r["position"]["y"]) for r in rows}
        self.assertFalse(self.fluids._underground_escape(self.obs, source, destination, [marker])["ok"])
        self.builder.route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
