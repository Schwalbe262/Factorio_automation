from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_armaments import Armaments
from factorio_ai.deterministic_intake_order import inherited_pole, order_intakes, reusable_poles
from tests import test_deterministic_armaments as fixtures


def pole(x, y):
    return {"name": "small-electric-pole", "position": {"x": x, "y": y}, "direction": 0}


def plan(x=0, pole_position=(-2, 0)):
    return {"ok": True, "entities": [
        {"name": "transport-belt", "position": {"x": x, "y": 0}, "direction": 4}, pole(*pole_position)],
        "ports": [{"kind": "item", "item": "firearm-magazine", "direction": "input",
                   "position": {"x": x, "y": 0}, "facing": 4}]}


class IntakeOrderTests(unittest.TestCase):
    def setUp(self):
        self.turret = {"name": "gun-turret", "position": {"x": 0, "y": 0}, "unit_number": 5}
        self.game = SimpleNamespace(query=Mock(return_value={"ok": True, "blocked": [], "fronts": [], "tick": 100}))
        self.driver = SimpleNamespace(game=self.game, state={"world_id": "one", "last_tick": 100},
            catalog=SimpleNamespace(fingerprint="catalog"), _key=Armaments._key,
            builder=SimpleNamespace(_occupied_by_plan=lambda entities: {
                (e["position"]["x"], e["position"]["y"]) for e in entities}),
            factory=SimpleNamespace(state={"blocks": {}, "links": {}, "power_links": {}},
                _reserved=Mock(return_value=[]), _port_clearances=Mock(return_value=set())))

    def test_equipment_and_pole_close_one_entrance_without_removing_any_fallback(self):
        old, better = plan(), plan(pole_position=(-2, 2))
        original = deepcopy([old, better])
        self.game.query.return_value["blocked"] = [{"x": -1, "y": -1}, {"x": -1, "y": 1}]
        ranked, old_score = order_intakes(self.driver, self.turret, [old, better], old)
        self.assertEqual(old_score, (0, 1))
        self.assertEqual(ranked[0][0], better)
        self.assertGreater(ranked[0][1], old_score)
        self.assertEqual([old, better], original)
        self.assertEqual(sorted(json.dumps(p, sort_keys=True) for p, _ in ranked), sorted(json.dumps(p, sort_keys=True) for p in original))

    def test_belt_fronts_and_other_reserved_footprints_are_part_of_connectivity(self):
        old = plan(pole_position=(-2, 2))
        self.game.query.return_value["fronts"] = [{"x": -1, "y": -1}, {"x": -1, "y": 1}]
        self.driver.factory._reserved.return_value = [pole(-2, 0)]
        ranked, score = order_intakes(self.driver, self.turret, [old], old)
        self.assertEqual(score, (0, 1))
        self.assertEqual(len(ranked), 1)

    def test_try_another_port_before_repeating_pole_variants_and_preserve_every_plan(self):
        plans = [plan(x, (x + 2 + variant, 2)) for x in range(0, 57, 8) for variant in range(3)]
        ranked, _ = order_intakes(self.driver, self.turret, plans)
        self.assertEqual(len({p["ports"][0]["position"]["x"] for p, _ in ranked[:8]}), 8)
        self.assertEqual(len(ranked), len(plans))
        self.assertEqual({json.dumps(p, sort_keys=True) for p, _ in ranked}, {json.dumps(p, sort_keys=True) for p in plans})

    def test_unknown_invalid_or_rollback_survey_retains_original_order(self):
        plans = [plan(), plan(8, (10, 2))]
        for result in ({}, {"ok": False}, {"ok": True, "blocked": [], "fronts": []},
                       {"ok": True, "tick": 99, "blocked": [], "fronts": []},
                       {"ok": True, "tick": 100, "blocked": [{"x": .1, "y": 0}], "fronts": []}):
            with self.subTest(result=result):
                self.game.query.return_value = result
                self.assertEqual(order_intakes(self.driver, self.turret, plans, plans[0]), ([(p, None) for p in plans], None))

    def test_reuse_is_limited_to_actual_powered_poles_in_another_owned_reservation(self):
        self.driver.factory.state["links"]["iron-feed"] = {"entities": [pole(2, 1)]}
        self.game.query.return_value = {"ok": True, "tick": 100, "poles": [
            {"position": {"x": 2, "y": 1}, "unit_number": 77, "reach": 2.5}]}
        proof = reusable_poles(self.driver, self.turret)[(2, 1)]
        self.assertEqual((proof["owner_group"], proof["owner_key"], proof["unit_number"]), ("links", "iron-feed", 77))
        planned = {**pole(2, 1), "_shared_power": proof}
        self.assertTrue(inherited_pole(self.driver, {"world_id": "one"}, planned, {"unit_number": 77}))
        for world, unit in (("other", 77), ("one", 78)):
            self.assertFalse(inherited_pole(self.driver, {"world_id": world}, planned, {"unit_number": unit}))
        self.assertFalse(inherited_pole(self.driver, {"world_id": "one"}, planned, {"unit_number": 77}, "iron-feed"))
        proof["catalog_fingerprint"] = "other"
        self.assertFalse(inherited_pole(self.driver, {"world_id": "one"}, planned, {"unit_number": 77}))
        proof["catalog_fingerprint"] = "catalog"
        self.driver.factory.state["links"].clear()
        self.assertFalse(inherited_pole(self.driver, {"world_id": "one"}, planned, {"unit_number": 77}))
        self.game.query.return_value["poles"][0]["position"] = {"x": 3, "y": 3}
        self.driver.factory.state["links"]["iron-feed"] = {"entities": [pole(2, 1)]}
        self.assertEqual(reusable_poles(self.driver, self.turret), {})

    def test_unknown_unpowered_or_stale_live_pole_evidence_never_authorizes_reuse(self):
        self.driver.factory.state["links"]["iron-feed"] = {"entities": [pole(2, 1)]}
        for reply in ({"ok": False}, {"ok": True, "tick": 100, "poles": {}},
                      {"ok": True, "tick": 99, "poles": [{"position": {"x": 2, "y": 1}, "unit_number": 77, "reach": 2.5}]},
                      {"ok": True, "tick": 100, "poles": [{"position": {"x": 2, "y": 1}, "unit_number": 0, "reach": 2.5}]}):
            with self.subTest(reply=reply):
                self.game.query.return_value = reply
                self.assertEqual(reusable_poles(self.driver, self.turret), {})


class IntakeRerankingTests(unittest.TestCase):
    def setUp(self):
        fixtures.ArmamentsTests.setUp(self)
        self.key = "armaments:" + self.armaments._key(self.turret)
        self.row = self.armaments._track(self.turret)
        candidates = list(self.armaments._intake_candidates(self.turret))
        self.old = candidates[0]
        self.better = next(p for p in candidates if p["ports"] != self.old["ports"])
        self.row["plan"] = self.factory.register_plan(self.key, self.old, self.obs)
        self.row.update(seed_remaining=0, last_seed_tick=70)
        self.armaments._save()
        self.source = {"kind": "item", "item": "firearm-magazine", "direction": "output", "position": {"x": 20.5, "y": .5}, "facing": 4}
        self.armaments._intake_order = Mock(return_value=([(self.better, (1, 80)), (self.old, (0, 1))], (0, 1)))
        self.action = {"type": "build", "name": "transport-belt"}
        self.factory.connect_input = Mock(return_value=self.action)

    def call(self):
        return self.armaments._ensure_intake(self.obs, self.turret, self.row, self.source)

    def test_validated_better_entrance_replaces_only_unbuilt_owned_plan_before_expensive_routing(self):
        self.assertEqual(self.call(), self.action)
        self.factory.connect_input.assert_called_once()
        self.assertEqual(self.factory.connect_input.call_args.args[2], self.better["ports"][0])
        self.assertEqual(self.row["plan"]["ports"], self.better["ports"])
        self.assertEqual(self.factory.state["blocks"][self.key], self.row["plan"])
        self.assertEqual((self.row["seed_remaining"], self.row["last_seed_tick"]), (0, 70))
        reloaded = Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        self.assertEqual(reloaded.state["turrets"][self.armaments._key(self.turret)]["plan"], self.row["plan"])

    def test_failed_better_entrance_keeps_original_candidate_as_fallback(self):
        self.factory.connect_input.side_effect = [{"status": "blocked", "reason": "route unavailable"}, self.action]
        self.assertEqual(self.call(), self.action)
        self.assertEqual([call.args[2] for call in self.factory.connect_input.call_args_list],
                         [self.better["ports"][0], self.old["ports"][0]])

    def test_invalid_first_pole_variant_cannot_restore_closed_entrance_before_valid_better_choice(self):
        invalid = deepcopy(self.better)
        invalid["entities"][-1]["position"]["x"] += 1
        self.armaments._intake_order.return_value = ([(invalid, (1, 90)), (self.old, (0, 1)),
                                                      (self.better, (1, 80))], (0, 1))
        self.builder.can_place = Mock(side_effect=lambda entities: {"ok": invalid["entities"][-1] not in entities})
        self.assertEqual(self.call(), self.action)
        self.assertEqual(self.factory.connect_input.call_args.args[2], self.better["ports"][0])

    def test_saved_link_or_paid_intake_equipment_prevents_reranking(self):
        self.factory.state["links"][self.key] = {"entities": []}
        self.assertEqual(self.call(), self.action)
        self.armaments._intake_order.assert_not_called()
        self.factory.state["links"].pop(self.key)
        self.obs["entities"].append(deepcopy(self.old["entities"][1]))
        self.assertEqual(self.call(), self.action)
        self.armaments._intake_order.assert_not_called()

    def test_exact_inherited_pole_does_not_turn_unbuilt_intake_into_paid_hardware(self):
        inherited = deepcopy(next(e for e in self.old["entities"] if e["name"] == "small-electric-pole"))
        self.factory.state["power_links"]["older-source"] = {"entities": [deepcopy(inherited)]}
        proof = {"world_id": self.obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint,
                 "owner_group": "power_links", "owner_key": "older-source", "unit_number": 77}
        inherited["_shared_power"] = proof
        self.row["plan"]["entities"][-1] = inherited
        self.factory.state["blocks"][self.key] = deepcopy(self.row["plan"])
        actual = {**deepcopy(inherited), "unit_number": 77}
        self.obs["entities"].append(actual)
        self.assertFalse(self.armaments._intake_hardware_present(self.obs, self.row["plan"]))
        belt = deepcopy(next(e for e in self.old["entities"] if e["name"] == "transport-belt"))
        self.obs["entities"].append(belt)
        self.assertTrue(self.armaments._intake_hardware_present(self.obs, self.row["plan"]))
        self.obs["entities"].remove(belt)
        self.assertTrue(self.armaments._discard_unbuilt_intake(self.obs, self.turret, self.key, self.row))
        self.assertIn("older-source", self.factory.state["power_links"])
        self.assertIn(actual, self.obs["entities"])
        actual["unit_number"] = 78
        self.assertTrue(self.armaments._intake_hardware_present(self.obs, {"entities": [inherited]}))


if __name__ == "__main__":
    unittest.main()
