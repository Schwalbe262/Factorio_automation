import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_state import request_stop
from factorio_ai.factory_templates import build_template


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    backend="assisted", query=Mock())
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(entities={})
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True, "blocked": []})
        self.obs = {"world_id": "test-world", "tick": 100, "entities": [], "inventory": {},
                    "enabled_recipes": {}, "technologies": {"steam-power": True}, "position": {"x": 0, "y": 0}}

    def test_clearance_requires_reachable_route_and_returns_only_one_mining_action(self):
        rock = {"name": "big-rock", "type": "simple-entity", "force": "neutral", "minable": True,
                "position": {"x": 2.3, "y": .5}}
        self.game.query.return_value = {"ok": True, "blocked": [{"x": 2.5, "y": y} for y in (-.5, 1.5)],
            "clearable": [{"position": {"x": 2.5, "y": .5}, "entities": [rock]}]}
        args = ({"x": .5, "y": .5}, {"x": 4.5, "y": .5})
        result = self.builder.clear_route_obstacle(*args, [], margin=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"]["position"], rock["position"])
        self.assertEqual((result["action"]["type"], result["action"]["count"]), ("mine", 1))
        self.assertNotIn("segments", result)
        self.assertNotIn("path", result)
        # A planned machine still closes the only corridor; finding a nearby
        # rock is insufficient evidence to mine it when no valid route exists.
        blocked = self.builder.clear_route_obstacle(*args,
            [{"name": "transport-belt", "position": {"x": 2.5, "y": .5}, "direction": 0}], margin=1)
        self.assertFalse(blocked["ok"])
        self.assertNotIn("action", blocked)

    def test_stop_interrupts_each_read_only_route_or_placement_attempt(self):
        request_stop(Path(self.temp.name) / "stop.json")
        source, destination = {"x": .5, "y": .5}, {"x": 4.5, "y": .5}
        for attempt in (
            lambda: FactoryBuilder.can_place(self.builder, []),
            lambda: self.builder.route(source, destination, "transport-belt", []),
            lambda: self.builder.clear_route_obstacle(source, destination, []),
        ):
            with self.assertRaisesRegex(InterruptedError, "operator_stop_requested"):
                attempt()
        self.game.query.assert_not_called()
        self.assertFalse(self.builder.path.exists())

    def test_clearance_does_not_mine_off_route_rocks_or_return_unverified_entities(self):
        obstacle = {"name": "crash-site-spaceship-wreck-small-1", "type": "simple-entity",
                    "force": "neutral", "minable": True, "position": {"x": 1.5, "y": .5}}
        for point, entity in [({"x": 1.5, "y": .5}, obstacle),
                              ({"x": 1.5, "y": 1.5}, {**obstacle, "name": "big-rock"})]:
            with self.subTest(point=point):
                self.game.query.return_value = {"ok": True, "blocked": [],
                    "clearable": [{"position": point, "entities": [entity]}]}
                result = self.builder.clear_route_obstacle({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, [], margin=1)
                self.assertFalse(result["ok"])
                self.assertNotIn("action", result)

    def test_missing_construction_item_delegates_to_resource_backed_bootstrap(self):
        plan = build_template("labs_row", inputs=["automation-science-pack"])
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "lab", "count": 1}
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["type"], "craft")
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "lab", 1)
        self.game.query.assert_not_called()

    def test_material_batch_excludes_installed_entities_and_duplicate_reservations(self):
        belts = [{"name": "transport-belt", "position": {"x": index + .5, "y": .5}, "direction": 4}
                 for index in range(12)]
        self.obs["entities"] = belts[:8]
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "transport-belt", "count": 2}
        result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts + [belts[-1]]})
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "transport-belt", 4)
        self.assertEqual(result["type"], "craft")

    def test_long_infrastructure_routes_request_at_most_32_placement_items(self):
        belts = [{"name": "transport-belt", "position": {"x": index + .5, "y": .5}, "direction": 4}
                 for index in range(100)]
        self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts})
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "transport-belt", 32)

    def test_machine_batch_is_small_and_uses_only_same_placement_item(self):
        machines = [{"name": "assembling-machine-1", "position": {"x": index * 5 + .5, "y": .5}}
                    for index in range(10)]
        machines.append({"name": "lab", "position": {"x": .5, "y": 10.5}})
        self.builder.ensure_plan(self.obs, {"ok": True, "entities": machines})
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "assembling-machine-1", 2)

    def test_existing_construction_stock_is_used_before_procuring_another_batch(self):
        self.obs["inventory"] = {"transport-belt": 1}
        belts = [{"name": "transport-belt", "position": {"x": index + .5, "y": .5}, "direction": 4}
                 for index in range(40)]
        self.assertEqual(self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts})["type"], "build")
        self.bootstrap.ensure_item.assert_not_called()

    @staticmethod
    def belts(count):
        return [{"name": "transport-belt", "position": {"x": index + .5, "y": .5}, "direction": 4}
                for index in range(count)]

    def test_assisted_build_batch_is_bounded_and_spends_only_observed_inventory(self):
        belts = self.belts(100)
        for available, expected in ((7, 7), (100, 32)):
            with self.subTest(available=available):
                self.obs["inventory"] = {"transport-belt": available}
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts})
                self.assertEqual(action["type"], "build_many")
                self.assertEqual(len(action["actions"]), expected)
                self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in belts[:expected]])
                self.assertTrue(all(row["type"] == "build" for row in action["actions"]))
                self.assertEqual(self.obs["inventory"]["transport-belt"], available)
        self.bootstrap.ensure_item.assert_not_called()
        self.assertEqual(self.builder.can_place.call_count, 2)

    def test_build_batch_skips_observed_and_duplicate_entities_without_overspending(self):
        belts = self.belts(5)
        self.obs["entities"] = belts[:2]
        self.obs["inventory"] = {"transport-belt": 2, "small-electric-pole": 1}
        pole = {"name": "small-electric-pole", "position": {"x": 8.5, "y": 1.5}}
        action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts[:4] + [belts[3], pole, belts[4]]})
        self.assertEqual([row["position"] for row in action["actions"]], [belts[2]["position"], belts[3]["position"], pole["position"]])
        self.bootstrap.ensure_item.assert_not_called()

    def test_mixed_arm_batch_retains_power_pole_each_facing_and_per_item_inventory(self):
        pole = {"name": "small-electric-pole", "position": {"x": 2.5, "y": 2.5}, "direction": 0}
        arms = [{"name": name, "position": {"x": x, "y": y}, "direction": direction}
                for name, x, y, direction in (("inserter", .5, .5, 0),
                    ("long-handed-inserter", .5, 1.5, 8), ("fast-inserter", 1.5, .5, 12))]
        observed = {"name": "fast-inserter", "position": {"x": 4.5, "y": 2.5}, "direction": 4}
        extra = {**arms[1], "position": {"x": .5, "y": 4.5}}
        self.obs["entities"] = [observed]
        self.obs["inventory"] = {name: 1 for name in (pole["name"], *(arm["name"] for arm in arms))}
        entities = [pole, observed, *arms, arms[1], extra, self.belts(1)[0]]
        original = json.dumps({"entities": entities, "inventory": self.obs["inventory"]}, sort_keys=True)
        action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": entities})
        self.assertEqual(action["type"], "build_many")
        self.assertEqual(action["actions"], [{"type": "build", "item": row["name"], **row} for row in [pole, *arms]])
        self.assertEqual(json.dumps({"entities": entities, "inventory": self.obs["inventory"]}, sort_keys=True), original)
        self.bootstrap.ensure_item.assert_not_called()

    def test_arm_batch_stops_before_machine_or_pending_recipe_configuration(self):
        arms = [{"name": "inserter", "position": {"x": i + .5, "y": .5}, "direction": 4} for i in range(3)]
        machine = {"name": "assembling-machine-1", "position": {"x": 8.5, "y": .5}, "recipe": "iron-gear-wheel"}
        self.obs["inventory"] = {"inserter": 3, "assembling-machine-1": 1}
        for observed in ([], [{**machine, "recipe": None}]):
            with self.subTest(machine_observed=bool(observed)):
                self.obs["entities"] = observed
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": arms[:2] + [machine, arms[2]]})
                self.assertEqual(action["type"], "build_many")
                self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in arms[:2]])

    def test_arm_batch_limit_and_existing_or_duplicate_facing_conflicts(self):
        for name in ("inserter", "long-handed-inserter", "fast-inserter"):
            arms = [{"name": name, "position": {"x": i + .5, "y": .5}, "direction": 8} for i in range(40)]
            for mode, expected in (("limit", 32), ("existing_facing", 2), ("duplicate_facing", 2)):
                with self.subTest(name=name, mode=mode):
                    self.obs["inventory"] = {name: 40}
                    self.obs["entities"] = [{**arms[2], "direction": 0}] if mode == "existing_facing" else []
                    entities = arms[:2] + [{**arms[0], "direction": 0}] + arms[2:] if mode == "duplicate_facing" else arms
                    action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": entities})
                    self.assertEqual(action["type"], "build_many")
                    self.assertEqual(len(action["actions"]), expected)
                    self.assertTrue(all(row["direction"] == 8 for row in action["actions"]))

    def test_plain_pipe_plan_batches_only_affordable_prefix_without_changing_fluid_reservations(self):
        pipes = [{"name": "pipe", "position": {"x": i + .5, "y": .5}, "direction": 0, "_fluid": "water"}
                 for i in range(40)]
        original = json.dumps(pipes, sort_keys=True)
        for available, expected in ((5, 5), (40, 32)):
            with self.subTest(available=available):
                self.obs["inventory"] = {"pipe": available}
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": pipes})
                self.assertEqual(action["type"], "build_many")
                self.assertEqual(len(action["actions"]), expected)
                self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in pipes[:expected]])
                self.assertTrue(all(row["type"] == "build" and row["item"] == "pipe" and "_fluid" not in row
                                    for row in action["actions"]))
                self.assertEqual(self.obs["inventory"], {"pipe": available})
                self.assertEqual(json.dumps(pipes, sort_keys=True), original)
        self.bootstrap.ensure_item.assert_not_called()

    def test_mixed_pipe_batch_reuses_observed_pipe_and_stops_before_underground_or_missing_stock(self):
        pipes = [{"name": "pipe", "position": {"x": i + .5, "y": .5}, "direction": 0} for i in range(4)]
        pole = {"name": "small-electric-pole", "position": {"x": 5.5, "y": 1.5}}
        belt = {"name": "transport-belt", "position": {"x": 6.5, "y": 1.5}, "direction": 4}
        underground = {"name": "pipe-to-ground", "position": {"x": 7.5, "y": 1.5}, "direction": 8}
        self.obs["entities"] = [{**pipes[0], "direction": 12}]
        for barrier in (underground, pipes[3]):
            with self.subTest(barrier=barrier["name"]):
                self.obs["inventory"] = {"pipe": 2, "small-electric-pole": 1, "transport-belt": 2, "pipe-to-ground": 2}
                entities = [pipes[0], pipes[1], pipes[1], pole, pipes[2], belt, barrier, self.belts(1)[0]]
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": entities})
                self.assertEqual([row["name"] for row in action["actions"]], ["pipe", "small-electric-pole", "pipe", "transport-belt"])
                self.assertEqual([row["position"] for row in action["actions"]],
                                 [pipes[1]["position"], pole["position"], pipes[2]["position"], belt["position"]])
        self.bootstrap.ensure_item.assert_not_called()

    def test_build_batch_stops_before_machine_material_deficit_or_recipe_repair(self):
        belts = self.belts(3)
        machine = {"name": "assembling-machine-1", "position": {"x": 8.5, "y": .5}, "recipe": "iron-gear-wheel"}
        pole = {"name": "small-electric-pole", "position": {"x": 8.5, "y": 5.5}}
        for barrier, existing in ((machine, []), (pole, []), (machine, [{**machine, "recipe": None}])):
            with self.subTest(barrier=barrier["name"], existing=bool(existing)):
                self.obs["entities"] = existing
                self.obs["inventory"] = {"transport-belt": 10, "assembling-machine-1": 1}
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts[:2] + [barrier, belts[2]]})
                self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in belts[:2]])

    def test_build_batch_stops_before_existing_wrong_belt_direction(self):
        belts = self.belts(4)
        self.obs["entities"] = [{**belts[2], "direction": 0}]
        self.obs["inventory"] = {"transport-belt": 10}
        action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": belts})
        self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in belts[:2]])
        self.obs["entities"] = []
        contradictory = belts[:2] + [{**belts[0], "direction": 0}] + belts[2:]
        action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": contradictory})
        self.assertEqual([row["position"] for row in action["actions"]], [row["position"] for row in belts[:2]])

    def test_character_with_abundant_route_materials_still_builds_one_reachable_entity(self):
        self.game.backend = "character"
        for name in ("transport-belt", "pipe", "inserter", "long-handed-inserter", "fast-inserter"):
            with self.subTest(name=name):
                self.obs["inventory"] = {name: 100}
                entities = [{**row, "name": name} for row in self.belts(100)]
                action = self.builder.ensure_plan(self.obs, {"ok": True, "entities": entities})
                self.assertEqual(action["type"], "build")
                self.assertEqual(action["position"], {"x": .5, "y": .5})
                self.assertNotIn("actions", action)

    def test_reconstruction_reuses_observed_entities_and_resumes_first_missing(self):
        plan = {"ok": True, "entities": [{"name": "pipe", "position": {"x": .5, "y": .5}},
                                           {"name": "pipe", "position": {"x": 1.5, "y": .5}}]}
        self.obs["entities"] = [plan["entities"][0]]
        self.obs["inventory"] = {"pipe": 1}
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["type"], "build")
        self.assertEqual(result["position"], {"x": 1.5, "y": .5})
        resumed = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        resumed.can_place = self.builder.can_place
        self.assertEqual(resumed.ensure_plan(self.obs, plan), result)

    def test_recipe_is_set_only_when_unlocked_and_existing_entity_was_observed(self):
        machine = {"name": "assembling-machine-1", "position": {"x": .5, "y": .5}, "recipe": "iron-gear-wheel"}
        plan = {"ok": True, "entities": [machine]}
        self.obs["entities"] = [{"name": machine["name"], "position": machine["position"]}]
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.obs["enabled_recipes"] = {"iron-gear-wheel": True}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["type"], "recipe")
        self.obs["entities"][0]["recipe"] = "iron-gear-wheel"
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_collision_and_wrong_existing_orientation_fail_before_action(self):
        belt = {"name": "transport-belt", "position": {"x": .5, "y": .5}, "direction": 4}
        plan = {"ok": True, "entities": [belt]}
        self.obs["inventory"] = {"transport-belt": 1}
        self.builder.can_place.return_value = {"ok": False, "blocked": [belt]}
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "blocked")
        self.obs["entities"] = [{**belt, "direction": 0}]
        self.assertIn("direction", self.builder.ensure_plan(self.obs, plan)["reason"])

    def test_fluid_assembler_recipe_activates_ports_before_restoring_orientation(self):
        machine = {"name": "assembling-machine-2", "position": {"x": .5, "y": .5},
                   "direction": 8, "recipe": "rocket-fuel"}
        plan = {"ok": True, "entities": [machine]}
        self.obs["enabled_recipes"] = {"rocket-fuel": True}
        # Empty assemblers normalize direction to north until a fluid recipe exists.
        self.obs["entities"] = [{**machine, "recipe": None, "direction": 0}]
        action = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual(action["type"], "recipe")
        self.assertEqual(action["direction"], 8)
        self.obs["entities"][0]["recipe"] = "rocket-fuel"
        self.assertEqual(self.builder.ensure_plan(self.obs, plan), action)
        self.obs["entities"][0]["direction"] = 8
        self.assertEqual(self.builder.ensure_plan(self.obs, plan)["status"], "succeeded")

    def test_two_direction_generators_reuse_normalized_axis_but_reject_perpendicular_axis(self):
        for name in ("steam-engine", "steam-turbine"):
            for planned, actual in ((8, 0), (12, 4)):
                entity = {"name": name, "position": {"x": .5, "y": .5}, "direction": planned}
                self.obs["entities"] = [{**entity, "direction": actual}]
                result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
                self.assertEqual(result["status"], "succeeded")
                self.obs["entities"][0]["direction"] = (actual + 4) % 8
                result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
                self.assertEqual(result["status"], "blocked")

    def test_character_build_defers_live_reach_to_navigator(self):
        self.game.backend = "character"
        self.obs["inventory"] = {"pipe": 1}
        for distance in (6.5, 50.5):
            with self.subTest(distance=distance):
                entity = {"name": "pipe", "position": {"x": distance, "y": .5}}
                result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
                self.assertEqual((result["type"], result["position"]), ("build", entity["position"]))

    def test_character_recipe_keeps_existing_entity_approach(self):
        self.game.backend = "character"
        entity = {"name": "assembling-machine-1", "position": {"x": 6.5, "y": .5},
                  "recipe": "iron-gear-wheel"}
        self.obs["entities"] = [{**entity, "recipe": None}]
        self.obs["enabled_recipes"] = {"iron-gear-wheel": True}
        result = self.builder.ensure_plan(self.obs, {"ok": True, "entities": [entity]})
        self.assertEqual((result["type"], result["position"]), ("move", entity["position"]))

    def test_only_own_character_obstruction_delegates_to_navigator_sidestep(self):
        self.game.backend = "character"
        self.obs["inventory"] = {"offshore-pump": 1}
        pump = {"name": "offshore-pump", "position": {"x": 54.5, "y": -.5}, "direction": 4}
        plan = {"ok": True, "entities": [pump]}
        self.builder.can_place.return_value = {"ok": False, "blocked": [pump]}
        self.game.query.return_value = {"ok": True, "only_actor": True}
        result = self.builder.ensure_plan(self.obs, plan)
        self.assertEqual((result["type"], result["name"]), ("build", "offshore-pump"))
        self.assertEqual(result["position"], pump["position"])

    def test_character_sidestep_never_bypasses_other_entity_or_terrain_obstruction(self):
        self.game.backend = "character"
        self.obs["inventory"] = {"offshore-pump": 1}
        pump = {"name": "offshore-pump", "position": {"x": 54.5, "y": -.5}, "direction": 4}
        self.builder.can_place.return_value = {"ok": False, "blocked": [pump]}
        for survey in ({"ok": True, "only_actor": False}, {"ok": False, "reason": "survey failed"}):
            self.game.query.return_value = survey
            self.assertEqual(self.builder.ensure_plan(self.obs, {"ok": True, "entities": [pump]})["status"], "blocked")

    def test_routing_rejects_foreign_belt_side_inputs_and_incidental_pipe_connections(self):
        self.game.query.return_value = {"ok": True, "blocked": []}
        foreign_belt = {"name": "transport-belt", "position": {"x": 1.5, "y": -.5}, "direction": 8}
        route = self.builder.route({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, "transport-belt", [foreign_belt])
        self.assertTrue(route["ok"], route["reason"])
        self.assertNotIn({"x": 1.5, "y": .5}, route["path"])
        foreign_pipe = {"name": "pipe", "position": {"x": 1.5, "y": -.5}}
        route = self.builder.route({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, "pipe", [foreign_pipe])
        self.assertTrue(route["ok"], route["reason"])
        self.assertNotIn({"x": 1.5, "y": .5}, route["path"])

    def test_new_world_invalidates_site_and_seed_checkpoint(self):
        self.builder._sync(self.obs)
        self.builder.state["power_plan"] = {"old": True}
        self.builder._save()
        self.obs["world_id"] = "different-world"
        self.builder._sync(self.obs)
        self.assertNotIn("power_plan", self.builder.state)
        saved = json.loads(self.builder.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["world_id"], "different-world")

    def test_coal_drill_output_belt_and_self_feed_inserter_touch_correct_tiles(self):
        plan = self.builder._coal_plan({"x": 10, "y": 20})
        self.assertEqual(plan["entities"][1]["position"], {"x": 11.5, "y": 19.5})
        inserter = next(e for e in plan["entities"] if e["name"] == "burner-inserter")
        self.assertEqual(inserter["position"], {"x": 8.5, "y": 19.5})
        self.assertEqual(inserter["direction"], 12)  # picks from west, drops east into drill
        self.assertTrue(any(e["position"] == {"x": 7.5, "y": 19.5} for e in plan["entities"]))

    def _ready_power(self):
        power = build_template("steam_bank")
        coal = self.builder._coal_plan({"x": 20, "y": 20})
        self.builder._sync(self.obs)
        self.builder.state.update(power_plan=power, coal_plan=coal)
        self.obs["entities"] = [{**e, "inventory": {"coal": 8}, "remaining_burning_fuel": 100}
                                 for e in power["entities"] + coal["entities"]]
        evidence = {"ok": True, "water": 100, "steam": 100, "boiler_fuel": 100,
                    "drill_fuel": 100, "coal_on_belts": 4, "connected_engines": 2,
                    "energized_engines": 2, "generation_kw": 0, "tick": 100}
        self.builder.power_evidence = Mock(return_value=evidence)
        return power, coal, evidence

    def test_power_requires_flow_evidence_over_time_and_accepts_no_load_grid(self):
        power, coal, evidence = self._ready_power()
        first = self.builder.ensure_power(self.obs)
        self.assertEqual(first["status"], "waiting")
        self.assertIn("30 game seconds", first["reason"])
        self.obs["tick"] = evidence["tick"] = 1900
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["evidence"]["flow_verified"])
        self.assertEqual(result["evidence"]["generation_kw"], 0)

    def test_broken_power_evidence_never_succeeds_or_repeatedly_handfeeds(self):
        power, coal, evidence = self._ready_power()
        self.builder.ensure_power(self.obs)
        for row in self.obs["entities"]:
            row["inventory"], row["remaining_burning_fuel"] = {}, 0
        evidence["boiler_fuel"] = 0
        self.obs["tick"] = evidence["tick"] = 5000
        self.obs["inventory"] = {"coal": 100}
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("type", result)
        self.assertNotIn("power_sample_tick", self.builder.state)

    def test_verified_capability_survives_starvation_but_does_not_claim_current_flow(self):
        _, _, evidence = self._ready_power()
        self.builder.ensure_power(self.obs)
        self.assertNotIn("power_verified_once", self.builder.state)
        self.obs["tick"] = evidence["tick"] = 1900
        self.assertEqual(self.builder.ensure_power(self.obs)["status"], "succeeded")
        evidence["boiler_fuel"] = 0
        self.obs["tick"] = evidence["tick"] = 2000
        self.assertEqual(self.builder.ensure_power(self.obs)["status"], "waiting")
        self.assertTrue(self.builder.state["power_verified_once"])
        self.assertNotIn("power_sample_tick", self.builder.state)
        resumed = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.assertTrue(resumed.state["power_verified_once"])

    def test_power_capability_invalidates_on_world_catalog_change_or_rollback(self):
        for change in ("world", "catalog", "rollback"):
            with self.subTest(change=change):
                self.catalog.fingerprint = "a"
                self.builder.state = {"world_id": "test-world", "schema_version": 1, "seeds": {},
                                      "catalog_fingerprint": "a", "last_tick": 100,
                                      "power_sample_tick": 10, "power_verified_once": True}
                obs = {**self.obs, "tick": 100}
                if change == "world": obs["world_id"] = "another-world"
                elif change == "catalog": self.catalog.fingerprint = "b"
                else: obs["tick"] = 99
                self.builder._sync(obs)
                self.assertNotIn("power_verified_once", self.builder.state)
                self.assertNotIn("power_sample_tick", self.builder.state)

    def test_evidence_error_is_a_blocked_report_not_a_type_error(self):
        self._ready_power()
        self.builder.power_evidence.return_value = {"ok": False, "reason": "RCON failure"}
        result = self.builder.ensure_power(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["diagnostics"]["reason"], "RCON failure")

    def test_seed_uses_real_inventory_and_is_observed_before_marking_complete(self):
        entity = {"name": "boiler", "position": {"x": .5, "y": 1}}
        self.obs["entities"] = [entity]
        self.obs["inventory"] = {"coal": 8}
        self.builder._sync(self.obs)
        action = self.builder._seed(self.obs, "boiler", entity, 8)
        self.assertEqual(action["type"], "insert")
        self.assertFalse(self.builder.state["seeds"]["boiler"]["observed"])
        self.obs["entities"][0]["remaining_burning_fuel"] = 1000
        self.assertIsNone(self.builder._seed(self.obs, "boiler", entity, 8))
        self.assertTrue(self.builder.state["seeds"]["boiler"]["observed"])


if __name__ == "__main__":
    unittest.main()
