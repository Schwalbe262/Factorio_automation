"""Threat-sized defense on a planned world: zero posts when quiet, threatened-side posts off planned land."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import progression
from factorio_agent.defense_perimeter import FUTURE_AREAS, POSTS, DefensePerimeter, growth_receipt, site_plan_areas
from factorio_agent.defense_policy import assess_risk, outside_plan
from factorio_agent.jobs import CAPABILITIES


def asset(unit, x, y):
    return dict(
        unit_number=unit, name="electric-furnace", type="furnace", position={"x": x, "y": y}, health=100, max_health=100
    )


def biter(x, y, unit=900):
    return dict(
        unit_number=unit, name="small-biter", type="unit", position={"x": x, "y": y}, health=15, max_health=15, range=1
    )


def zone(kind, name, rect):
    return {"kind": kind, "name": name, "rect": rect}


# Smelting zone hosts the producers near its east edge; the bus band lies to the south-east.
PLAN_ZONES = [zone("smelting", "smelting", [-30, -20, 10, 20]), zone("bus", "bus", [12, 10, 60, 30])]


def native_all_legal(game, candidates, areas, reserved, bounds):
    return {
        "ok": True,
        "range": 18,
        "magazine_size": 10,
        "candidates": [{"position": deepcopy(p), "local_walk_access": True} for p in candidates],
    }


class PlannedDefenseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write_plan(PLAN_ZONES)
        self.facts = dict(
            ok=True,
            world_id="w",
            tick=1000,
            surface_index=1,
            force_index=1,
            evolution_factor=0.01,
            gun_range=18,
            assets=[asset(1, 5, 0), asset(2, 5, 6)],
            guns=[],
            targets=[],
            technologies={"automation": True},
        )
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)), query=Mock(side_effect=self.query))
        self.bootstrap = SimpleNamespace(
            ensure_item=Mock(side_effect=lambda obs, item, count: {"type": "craft", "recipe": item, "count": count})
        )
        self.c = DefensePerimeter(self.game, self.bootstrap, SimpleNamespace(fingerprint="cat"))
        self.tick = 1000

    def write_plan(self, zones, *, world="w"):
        plan = {"zones": zones, "strips": [], "corridors": [], "allocations": []}
        (self.root / "site-plan.json").write_text(json.dumps({"world_id": world, "plan": plan}), encoding="utf-8")

    def query(self, lua):
        if "defense_policy_world_or_surface_changed" in lua:
            return deepcopy({**self.facts, "tick": self.tick})
        if "actor_full_magazines" in lua:
            posts = [
                {"name": p["name"], "position": p["position"], "missing": True, "ammo": 0, "ammo_rounds": 0}
                for p in self.c.state.get("posts", [])
            ]
            return {"ok": True, "tick": self.tick, "posts": posts or {}, "actor_full_magazines": 0}
        raise AssertionError("unexpected native query")

    def obs(self, step=1000):
        self.tick += step
        return {
            "ok": True,
            "world_id": "w",
            "tick": self.tick,
            "inventory": {},
            "entities": [],
            "technologies": {"automation": True},
            "enabled_recipes": {"gun-turret": True, "firearm-magazine": True},
        }

    def plan_boxes(self):
        return [row["area"] for row in site_plan_areas(self.root, "w")]

    def test_quiet_map_succeeds_with_no_posts_and_no_plates(self):
        result = self.c.step(self.obs())
        self.assertEqual((result["status"], result["reason"]), ("succeeded", "quiet map, no posts required"))
        self.assertEqual(result["evidence"]["active_post_names"], [])
        self.assertIn("quiet map, no posts required", result["evidence"]["sizing"])
        self.assertEqual(self.c.state["posts"], [])
        self.bootstrap.ensure_item.assert_not_called()
        self.assertIsNotNone(self.c.proof(self.obs(10)))

    def test_threat_on_one_side_places_posts_there_off_planned_land(self):
        self.facts["targets"] = [biter(62, 3)]  # 57 tiles east: approaching, two posts
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=native_all_legal):
            action = self.c.step(self.obs())
        self.assertEqual(action, {"type": "craft", "recipe": "firearm-magazine", "count": 10})
        posts = self.c.state["posts"]
        self.assertEqual(len(posts), 2)
        for post in posts:
            self.assertTrue(post["name"].startswith("adaptive_"))
            self.assertEqual(post["placement"], "strict")
            self.assertGreater(post["position"]["x"], post["focus"]["x"])  # threatened (east) side
            self.assertTrue(outside_plan(post["position"], self.plan_boxes()))
        anchors = {(a[0], a[1]) for _, a, _ in POSTS}
        self.assertFalse(any((p["position"]["x"], p["position"]["y"]) in anchors for p in posts))

    def test_deep_producer_falls_back_to_host_zone_but_never_the_bus_band(self):
        self.write_plan([zone("mining", "mining:iron-ore", [-60, -40, 40, 40]), zone("bus", "bus", [-20, 4, 40, 12])])
        self.facts["assets"] = [asset(1, -10, 0)]
        self.facts["targets"] = [biter(-10, 58)]
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=native_all_legal):
            self.c.step(self.obs())
        posts = self.c.state["posts"]
        self.assertEqual({p["placement"] for p in posts}, {"relaxed"})
        bus = [[-20, 4], [40, 12]]
        self.assertTrue(all(outside_plan(p["position"], [bus]) for p in posts))

    def test_rising_threat_grows_through_a_low_priority_node_not_other_jobs(self):
        self.assertEqual(self.c.step(self.obs())["status"], "succeeded")
        self.facts["targets"] = [biter(62, 3)]
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=native_all_legal):
            background = self.c.step(self.obs(), background=True)
        self.assertEqual(background["reason"], "defense_growth_deferred_to_low_priority_node")
        self.assertEqual(self.c.state["posts"], [])
        self.bootstrap.ensure_item.assert_not_called()

        risk = assess_risk({**self.facts, "tick": self.tick}, "w")
        receipt = growth_receipt(self.root, {"world_id": "w", "adaptive_defense_risk": risk})
        self.assertEqual((receipt["grow"], receipt["target_posts"], receipt["active_posts"]), (True, 2, 0))
        jobs = [{"capability": "defense_perimeter", "status": "succeeded", "parameters": {}}]
        observation = {"defense_growth": receipt, "tick": self.tick}
        nodes = progression.defense_growth_nodes(jobs, [], observation, CAPABILITIES)
        self.assertEqual(len(nodes), 1)
        self.assertEqual((nodes[0]["capability"], nodes[0]["parameters"]), ("defense_growth", {"posts": 2}))
        self.assertLess(nodes[0]["priority"], progression.TOP_PRIORITY - len(progression.ROADMAP))
        self.assertEqual(progression.defense_growth_nodes(jobs, nodes, observation, CAPABILITIES), [])

        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=native_all_legal):
            grown = self.c.step(self.obs(10))
        self.assertEqual(grown["recipe"], "firearm-magazine")
        self.assertEqual(len(self.c.state["posts"]), 2)

    def test_urgent_threat_is_built_even_from_another_jobs_background(self):
        self.assertEqual(self.c.step(self.obs())["status"], "succeeded")
        self.facts["targets"] = [biter(30, 3)]  # within 40 tiles: high
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=native_all_legal):
            action = self.c.step(self.obs(), background=True)
        self.assertEqual(action["recipe"], "firearm-magazine")
        self.assertEqual(len(self.c.state["posts"]), 3)

    def test_planned_world_has_no_legacy_anchors_or_future_areas(self):
        obs = self.obs()
        self.assertEqual(self.c._legacy_prepare(obs)["reason"], "legacy_fixed_posts_refused_on_planned_world")
        self.c.adaptive_enabled = False
        prepared = self.c.prepare(obs)  # a planned world never takes the legacy path
        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(prepared["posts"], [])
        self.assertIn("adaptive_policy", prepared)
        reserved = self.c._coverage_reservations(obs, [[[0, 0], [4, 4]]])
        self.assertFalse(any(box in reserved for box in FUTURE_AREAS))
        (self.root / "site-plan.json").unlink()
        reserved = self.c._coverage_reservations(obs, [[[0, 0], [4, 4]]])
        self.assertTrue(all(list(box) in reserved for box in FUTURE_AREAS))

    def test_other_world_plan_is_not_a_site_service(self):
        self.write_plan(PLAN_ZONES, world="other")
        self.assertIsNone(site_plan_areas(self.root, "w"))

    def test_dependants_of_the_perimeter_are_admitted_on_a_quiet_map(self):
        result = self.c.step(self.obs())
        self.assertEqual(result["status"], "succeeded")
        jobs = [{"capability": "defense_perimeter", "status": "succeeded", "parameters": {}}]
        step = next(s for s in progression.ROADMAP if s.id == "defense_perimeter")
        self.assertTrue(progression._Progress(tuple(jobs), frozenset()).satisfied(step))
        dependants = [name for name, spec in CAPABILITIES.items() if "defense_perimeter" in spec.get("requires", [])]
        self.assertIn("construction_mall", dependants)
        risk = assess_risk({**self.facts, "tick": self.tick}, "w")
        receipt = growth_receipt(self.root, {"world_id": "w", "adaptive_defense_risk": risk})
        self.assertFalse(receipt["grow"])
        self.assertEqual(
            progression.defense_growth_nodes(jobs, [], {"defense_growth": receipt, "tick": 1}, CAPABILITIES), []
        )


if __name__ == "__main__":
    unittest.main()
