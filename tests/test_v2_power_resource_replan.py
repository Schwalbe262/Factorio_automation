"""Starter power never stays blocked on a plan entity over ore (fleet all-1113-2332 seed 1561968568).

The boiler's coal stub belt (32.5, 37.5) was planned on ore: the plan search used only the
native ``can_place`` (belts may stand on ore), the resource policy of ``ensure_plan`` then refused
the saved plan on every retry and the run did nothing useful from 4 to 45 game minutes.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent import supervisor
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.power_layout import V2FactoryBuilder

SITE = {"direction": 8, "position": {"x": 30.5, "y": 47.5}, "water_out": {"x": 30.5, "y": 46.5}, "distance": 40}


def builder(cls=FactoryBuilder):
    b = cls.__new__(cls)
    b.state = {"world_id": "w", "seeds": {}}
    b.game = SimpleNamespace(query=lambda script: {"ok": True})
    b._save = lambda: None
    b._sync = lambda obs: None
    return b


def bank(x):
    belt = {"name": "transport-belt", "position": {"x": x, "y": 37.5}, "direction": 8}
    boiler = {"name": "boiler", "position": {"x": x - 2.5, "y": 39.5}, "direction": 12}
    ports = [{"item": "water", "position": {"x": x - 2, "y": 43.5}}]
    return {"ok": True, "entities": [belt, boiler], "ports": ports}


class PlanSearchTests(unittest.TestCase):
    def test_a_candidate_on_ore_is_passed_over(self):
        b = builder()
        b.water_sites = lambda: [SITE]
        b._steam_bank_variants = lambda anchor, rotation: [bank(32.5), bank(40.5)]
        b.can_place = lambda entities: {"ok": True}
        b.route = lambda *a, **k: {"ok": True, "path": []}
        on_ore = {32.5}
        b._power_plan_clear = lambda entities: not any(e["position"]["x"] in on_ore for e in entities)
        plan = b._choose_power_plan()
        self.assertTrue(plan["ok"])
        self.assertNotIn(32.5, {e["position"]["x"] for e in plan["entities"]})

    def test_the_v2_builder_checks_ore_and_reservations_with_the_resource_policy(self):
        b = builder(V2FactoryBuilder)
        es = bank(32.5)["entities"]
        refused = {"ok": False, "reason": "new_entity_over_resource_or_mining_reservation"}
        with patch("factorio_agent.resource_policy.resource_preflight", return_value=refused) as check:
            self.assertFalse(b._power_plan_clear(es))
        self.assertEqual(check.call_args.args[2], {"world_id": "w", "entities": []})
        self.assertEqual(check.call_args.args[3], es)
        with patch("factorio_agent.resource_policy.resource_preflight", return_value={"ok": True}):
            self.assertTrue(b._power_plan_clear(es))


class SavedPlanTests(unittest.TestCase):
    REFUSED = {
        "status": "blocked",
        "reason": "new_entity_over_resource_or_mining_reservation",
        "entity": "transport-belt",
        "position": {"x": 32.5, "y": 37.5},
    }

    def obs(self, entities=()):
        return {
            "world_id": "w",
            "tick": 14051,
            "technologies": {"steam-power": True},
            "entities": list(entities),
        }

    def test_an_unbuilt_refused_plan_is_dropped_and_chosen_again(self):
        b = builder()
        old = _plan(bank(32.5)["entities"], [], template="starter_power")
        new = _plan(bank(40.5)["entities"], [], template="starter_power")
        b.state["power_plan"] = old
        b.ensure_plan = lambda obs, plan: self.REFUSED if plan is old else {"status": "waiting", "reason": "build"}
        b._choose_power_plan = lambda: new
        first = b.ensure_power(self.obs())
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["evidence"]["refused"]["position"], {"x": 32.5, "y": 37.5})
        self.assertNotIn("power_plan", b.state)
        self.assertEqual(b.ensure_power(self.obs())["reason"], "build")
        self.assertIs(b.state["power_plan"], new)

    def test_a_partly_built_plan_stays_blocked(self):
        b = builder()
        old = _plan(bank(32.5)["entities"], [], template="starter_power")
        b.state["power_plan"] = old
        b.ensure_plan = lambda obs, plan: self.REFUSED
        built = dict(old["entities"][1], unit_number=7)
        result = b.ensure_power(self.obs([built]))
        self.assertEqual(result["reason"], self.REFUSED["reason"])
        self.assertIs(b.state["power_plan"], old)

    def test_the_reason_is_a_replanning_blocker(self):
        self.assertTrue(supervisor._replannable("new_entity_over_resource_or_mining_reservation"))
        self.assertTrue(supervisor._replannable("new_underground_tunnel_over_resource_or_mining_reservation"))


if __name__ == "__main__":
    unittest.main()
