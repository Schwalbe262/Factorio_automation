from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_belt_crossings import _construct
from factorio_ai.deterministic_input_links import _geometry, _path


def entity(name, x, y, direction=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction}


def port(x, y, facing):
    return {"kind": "item", "direction": "output", "item": "iron-plate",
            "position": {"x": x, "y": y}, "facing": facing}


class OwnedOutputDropTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)), query=Mock())
        catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), catalog)
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, catalog)
        self.obs = {"world_id": "one", "tick": 100, "entities": []}
        self.factory._sync(self.obs)
        self.source, self.entry, self.bus = port(8.5, .5, 12), port(.5, .5, 8), port(.5, 2.5, 4)
        self.tail = [entity("transport-belt", .5, .5, 8), entity("transport-belt", .5, 1.5, 8),
                     entity("transport-belt", .5, 2.5, 4)]
        self.owner = {"entities": self.tail, "source_port": self.entry, "consumer_port": self.bus}
        self.factory.state["links"]["old-output"] = self.owner
        self.wall = [entity("transport-belt", 1.5, y + .5, 8) for y in range(-70, 71)]
        self.reserved = self.wall + self.tail
        self.proof = {"world_id": "one", "tick": 100, "unit_number": 10, "port": self.entry,
                      "bus_port": self.bus, "category": "links", "key": "old-output"}
        self.game.query.side_effect = lambda _: {"ok": True, "owned_drop_verified": True,
            "belts": deepcopy(self.reserved), "blocked": [e["position"] for e in self.reserved]}
        self.builder.can_place = Mock(side_effect=lambda rows: {"ok": all(
            e["direction"] == 8 for e in rows if e["name"] == "transport-belt" and e["position"] == self.entry["position"])})

    def route(self, proof=True):
        return self.factory._belt_bridge_route(self.source["position"], self.entry["position"], self.reserved,
            start_direction=12, **({"owned_drop": self.proof} if proof else {}))

    def test_sideways_arm_drop_preserves_live_belt_and_connected_copied_tail(self):
        self.assertFalse(self.route(False)["ok"])
        before = deepcopy(self.owner)
        result = self.route()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][-1], self.tail[0])
        arm = entity("long-handed-inserter", 2.5, .5, 4)
        self.assertIn(arm, result["segments"])
        self.assertTrue(any(e["name"] == "small-electric-pole" for e in result["segments"]))
        full = result["segments"][:-1] + self.tail
        belts, edges, _ = _geometry({"entities": full})
        self.assertIsNotNone(_path(belts, edges, (8.5, .5), (.5, 2.5)))
        self.assertEqual(self.owner, before)
        self.assertFalse(result["flow_verified"])

    def test_invalid_or_foreign_proof_rejects_before_query(self):
        changes = [{"world_id": "other"}, {"tick": 99}, {"tick": -1}, {"tick": True},
                   {"unit_number": 0}, {"unit_number": True}, {"key": "absent"}, {"category": "power_links"},
                   {"port": {**self.entry, "item": "coal"}}, {"port": {**self.entry, "facing": False}},
                   {"port": {**self.entry, "facing": 12}}, {"port": {**self.entry, "position": self.bus["position"]}}]
        for fields in changes:
            with self.subTest(fields=fields):
                original = self.proof
                self.proof = {**original, **fields}
                self.assertFalse(self.route()["ok"])
                self.proof = original
        self.game.query.assert_not_called()
        self.builder.can_place.assert_not_called()

    def test_changed_catalog_foreign_reservation_or_disconnected_suffix_rejects(self):
        for mode in ("catalog", "foreign", "path"):
            with self.subTest(mode=mode):
                state = deepcopy(self.factory.state)
                if mode == "catalog":
                    self.factory.state["catalog_fingerprint"] = "other"
                elif mode == "foreign":
                    self.factory.state["links"]["foreign"] = {"entities": [self.tail[0]],
                        "source_port": {**self.entry, "item": "coal"}}
                else:
                    self.factory.state["links"]["old-output"]["entities"][1]["direction"] = 0
                self.assertFalse(self.route()["ok"])
                self.factory.state = state
        self.game.query.assert_not_called()

    def test_missing_live_verification_identity_or_contamination_fails_closed(self):
        for response in ({"ok": False, "reason": "owned output drop identity changed"},
                         {"ok": False, "reason": "owned output drop carries another material"},
                         {"ok": True, "belts": self.reserved, "blocked": []}):
            with self.subTest(response=response):
                self.game.query.side_effect = None
                self.game.query.return_value = response
                self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_final_placement_denial_and_missing_reservation_reject(self):
        self.builder.can_place.return_value = {"ok": False}
        self.builder.can_place.side_effect = None
        self.assertFalse(self.route()["ok"])
        self.reserved = self.wall
        self.assertFalse(self.route()["ok"])

    def test_owned_surface_arrival_changes_only_the_terminal_facing(self):
        self.builder.can_place = Mock(return_value={"ok": True})
        bounds = {"min_x": -8.5, "max_x": 12.5, "min_y": -8.5, "max_y": 8.5}
        direct = _construct(self.factory, (8.5, .5), (.5, .5), (), set(), set(), bounds, 12, None, [25000])
        self.assertEqual(direct["segments"][-1]["direction"], 12)
        owned = _construct(self.factory, (8.5, .5), (.5, .5), (), set(), set(), bounds, 12, None, [25000], 8)
        self.assertEqual(owned["segments"][-1]["direction"], 8)
        self.assertEqual(owned["segments"][:-1], direct["segments"][:-1])
        edge = {"pickup": (4.5, .5), "drop": (.5, .5), "arm": (2.5, .5),
                "direction": 12, "over": (1.5, .5)}
        route = _construct(self.factory, (8.5, .5), (-3.5, .5), (edge,), set(), set(), bounds, 12, None, [25000], 8)
        self.assertIsNotNone(route)
        self.assertIn(entity("transport-belt", .5, .5, 12), route["segments"])
        self.assertEqual(route["segments"][-1]["direction"], 8)

    def test_owned_head_on_surface_arrival_remains_rejected(self):
        bounds = {"min_x": -8.5, "max_x": 12.5, "min_y": -8.5, "max_y": 8.5}
        self.assertIsNone(_construct(self.factory, (8.5, .5), (.5, .5), (), set(), set(),
                                     bounds, 12, None, [25000], 4))

    def test_normal_proven_side_feed_passes_placement_and_reaches_unchanged_bus(self):
        self.source, self.entry, self.bus = port(.5, 8.5, 0), port(.5, .5, 4), port(2.5, .5, 4)
        self.tail = [entity("transport-belt", x + .5, .5, 4) for x in range(3)]
        self.owner = {"entities": self.tail, "source_port": self.entry, "consumer_port": self.bus}
        self.factory.state["links"]["old-output"] = self.owner
        self.proof.update(port=self.entry, bus_port=self.bus)
        self.reserved = self.tail
        self.builder.can_place.side_effect = lambda rows: {"ok": all(
            row["direction"] == 4 for row in rows if row["position"] == self.entry["position"])}
        result = self.factory._belt_bridge_route(self.source["position"], self.entry["position"],
            self.reserved, start_direction=0, owned_drop=self.proof)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][-1], self.tail[0])
        full = result["segments"][:-1] + self.tail
        belts, edges, _ = _geometry({"entities": full})
        self.assertIsNotNone(_path(belts, edges, (.5, 8.5), (2.5, .5)))
        self.game.query.assert_called_once()
        self.builder.can_place.assert_called_once()

    def test_failed_leg_search_stays_bounded_and_extra_attempts_require_owned_proof(self):
        attempts = []
        for owned in (False, True):
            self.game.query.reset_mock()
            with patch("factorio_ai.deterministic_belt_crossings._construct", return_value=None) as construct:
                self.assertFalse(self.route(owned)["ok"])
            attempts.append(construct.call_count)
            self.game.query.assert_called_once()
            self.assertTrue(all(call.args[9][0] <= 100000 for call in construct.call_args_list))
        self.assertEqual(attempts, [24, 48])

    def test_owned_keyword_does_not_leak_into_direct_builder_route(self):
        self.builder.route = Mock(return_value={"ok": False, "reason": "route search budget exhausted"})
        self.factory._belt_bridge_route = Mock(return_value={"ok": True})
        self.factory._material_route(self.source["position"], self.entry["position"], self.reserved,
                                     start_direction=12, owned_drop=self.proof)
        self.assertNotIn("owned_drop", self.builder.route.call_args.kwargs)
        self.assertEqual(self.factory._belt_bridge_route.call_args.kwargs["owned_drop"], self.proof)

    def test_normal_merge_builds_then_reload_keeps_tail_and_waits_for_paid_power(self):
        self.factory.state["blocks"]["barrier"] = {"entities": self.wall, "ports": []}
        self.obs["entities"] = [{**deepcopy(e), "unit_number": 10 + i} for i, e in enumerate(self.tail)]
        self.builder.route = Mock(return_value={"ok": False, "reason": "no route within bounds"})
        self.factory._source_pickup_bridge_route = Mock(return_value={"ok": False})
        construction = {"type": "build", "name": "long-handed-inserter", "position": {"x": 2.5, "y": .5}, "direction": 4}
        self.builder.ensure_plan = Mock(return_value=construction)
        old = deepcopy(self.owner)
        self.assertEqual(self.factory._merge_output(self.obs, self.source, self.bus, "new-output"), construction)
        plan = deepcopy(self.factory.state["links"]["new-output"])
        self.assertEqual(plan["entities"][-1], self.tail[-1])
        self.assertEqual(plan["consumer_port"], self.bus)
        self.assertEqual(self.factory.state["links"]["old-output"], old)
        self.assertTrue(any(e["name"] == "small-electric-pole" for e in plan["entities"]))
        resumed = DeterministicFactory(self.game, Mock(), self.builder, self.factory.catalog)
        resumed._sync(self.obs)
        resumed._material_route = Mock(side_effect=AssertionError("saved merge must not be rerouted"))
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        waiting = {"status": "waiting", "reason": "new pole needs its normal connection"}
        resumed.ensure_power_connection = Mock(return_value=waiting)
        self.assertEqual(resumed._merge_output(self.obs, self.source, self.bus, "new-output"), waiting)
        self.assertEqual(resumed.state["links"]["new-output"], plan)
        resumed.ensure_power_connection.assert_called_once_with(self.obs, "merge:new-output", plan)


if __name__ == "__main__":
    unittest.main()
