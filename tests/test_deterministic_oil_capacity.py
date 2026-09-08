from copy import deepcopy
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_fluids import FluidProduction
from factorio_ai.deterministic_oil_capacity import PRIMARY, PREFIX, _survey, _pipeline_extent
from factorio_ai.factory_templates import route_orthogonal
from test_deterministic_fluids import catalog, entity, box


READY = {"status": "succeeded", "evidence": {}}


class OilCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    query=Mock(side_effect=self.query), backend="assisted")
        self.catalog = catalog()
        self.catalog.fingerprint = "fixture"
        self.catalog.entities["pumpjack"] = entity(3, [], [box(1, "output", 1, -1, 0)])
        self.bootstrap = Mock()
        self.bootstrap.ensure_item.return_value = {"status": "blocked", "reason": "normal construction materials needed"}
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder._move = Mock(return_value=None)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.route = Mock(side_effect=self.route)
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.ensure_power_connection = Mock(return_value=READY)
        self.fluids = FluidProduction(self.game, self.builder, self.catalog)
        self.fluids.factory = self.factory
        self.fluids._connect_pipe = Mock(return_value=READY)
        self.fluids._network_taps = Mock(side_effect=lambda *args: {"taps": [e["position"]
            for e in self.obs["entities"] if e["name"] == "pipe"]})
        self.obs = {"world_id": "fixture", "tick": 100, "entities": [], "inventory": {}, "enabled_recipes": {"pumpjack": True}}
        self.fluids._sync(self.obs)
        self.factory._sync(self.obs)
        primary = self.fluids._utility_plan("pumpjack", {"x": .5, "y": .5}, 0, "crude-oil")
        self.fluids.state["sources"][PRIMARY] = primary
        self.factory.register_plan("fluid:" + PRIMARY, primary, self.obs)
        self.obs["entities"] = deepcopy(primary["entities"])
        self.original = deepcopy(primary)
        self.wells = {(.5, .5): 746.01, (12.5, .5): 900, (24.5, .5): 1000, (36.5, .5): 600}
        self.foreign = set()
        self.occupied = set()
        self.error = None
        self.extent_limit = 320
        self.extent_members = []

    def query(self, body):
        if "oil_pipeline_extent" in body:
            bounds = None
            if self.extent_members:
                points = [e["position"] for e in self.extent_members]
                bounds = {"left_top": {axis: min(p[axis] for p in points)-.5 for axis in ("x", "y")},
                          "right_bottom": {axis: max(p[axis] for p in points)+.5 for axis in ("x", "y")}}
            return {"ok": True, "world_id": "fixture", "limit": self.extent_limit,
                    "source_present": bool(self.extent_members), "bounds": bounds}
        if "oil_exploration_standing" in body:
            point = json.loads(json.loads(re.search(r'helpers.json_to_table\(("(?:[^"\\]|\\.)*")\)', body).group(1)))
            return {"ok": True, "world_id": "fixture", "position": getattr(self, "standing_override", point)}
        self.assertIn("oil_well_capacity", body)
        args = json.loads(json.loads(re.search(r'helpers.json_to_table\(("(?:[^"\\]|\\.)*")\)', body).group(1)))
        if self.error:
            return self.error
        owned = []
        used = set()
        for row in args["rows"]:
            p = (row["position"]["x"], row["position"]["y"])
            if p not in self.wells or p in self.foreign:
                return {"ok": False, "reason": "missing or foreign saved oil well"}
            used.add(p)
            built = any(e["name"] == "pumpjack" and e["position"] == row["position"] for e in self.obs["entities"])
            owned.append({"key": row["key"], "position": row["position"], "built": built,
                          "nominal_capacity_per_minute": self.wells[p]})
        candidates = [{"position": {"x": p[0], "y": p[1]}, "nominal_capacity_per_minute": rate}
                      for p, rate in self.wells.items() if p not in used | self.occupied | self.foreign] if args["discover"] else []
        return {"ok": True, "world_id": "fixture", "owned": owned, "candidates": candidates or {}, "search_truncated": False}

    def route(self, source, destination, name, reserved):
        self.assertEqual(name, "pipe")
        occupied = self.builder._occupied_by_plan(reserved)
        occupied.discard((source["x"], source["y"]))
        occupied.discard((destination["x"], destination["y"]))
        return route_orthogonal(source, destination, occupied=occupied,
            bounds={"min_x": -10, "min_y": -10, "max_x": 60, "max_y": 10})

    def ensure(self, rate=2500):
        return self.fluids._raw_capacity(self.obs, "crude-oil", rate, self.fluids._decorate(READY, self.original))

    def finish_saved(self):
        unique = {(e["name"], e["position"]["x"], e["position"]["y"]): e
                  for plan in self.fluids.state["sources"].values() for e in plan["entities"]}
        self.obs["entities"] = deepcopy(list(unique.values()))
        self.obs["tick"] += 1

    def expand_two(self):
        self.ensure()
        self.finish_saved()
        self.ensure()
        self.finish_saved()

    def test_two_paid_wells_merge_to_stable_bus_without_crediting_unbuilt_yield(self):
        result = self.ensure()
        self.assertEqual(result["reason"], "normal construction materials needed")
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "pumpjack", 1)
        self.assertEqual(len(self.fluids.state["sources"]), 2)
        cell = self.fluids.state["sources"][PREFIX + "1"]
        self.assertEqual(cell["required_items"]["pumpjack"], 1)
        self.assertEqual(cell["required_items"]["small-electric-pole"], 4)
        self.assertGreater(cell["required_items"]["pipe"], 1)
        self.assertTrue(all(e.get("_fluid") == "crude-oil" for e in cell["entities"] if e["name"] == "pipe"))
        self.assertEqual(self.fluids.state["sources"][PRIMARY], self.original)
        self.obs["inventory"]["pumpjack"] = 1
        paid = self.ensure()
        self.assertEqual((paid["type"], paid["name"]), ("build", "pumpjack"))
        self.assertEqual(paid["position"], {"x": 12.5, "y": .5})
        self.finish_saved()
        self.ensure()
        self.finish_saved()
        done = self.ensure()
        self.assertEqual(done["status"], "succeeded")
        self.assertAlmostEqual(done["evidence"]["nominal_capacity_per_minute"], 2646.01)
        self.assertEqual(done["evidence"]["wells_observed"], 3)
        self.assertFalse(done["evidence"]["throughput_verified"])
        self.assertEqual(done["evidence"]["ports"], self.fluids._decorate(READY, self.original)["evidence"]["ports"])
        self.assertTrue(all(call.args[2] == self.original["ports"][0] for call in self.fluids._connect_pipe.call_args_list))

    def test_yield_decline_adds_one_well_and_lower_demand_keeps_all_cells(self):
        self.expand_two()
        self.assertEqual(self.ensure()["status"], "succeeded")
        self.wells[(24.5, .5)] = 500
        self.ensure()
        self.assertEqual(len(self.fluids.state["sources"]), 4)
        self.finish_saved()
        self.assertEqual(self.ensure()["status"], "succeeded")
        self.fluids._connect_pipe.reset_mock()
        self.assertEqual(self.ensure(100)["evidence"]["wells_observed"], 4)
        self.assertEqual(self.fluids._connect_pipe.call_count, 3)

    def test_reload_and_tick_rollback_rebuild_missing_cell_without_duplicate_well(self):
        self.expand_two()
        self.fluids._save()
        restored = FluidProduction(self.game, self.builder, self.catalog)
        restored.factory = self.factory
        restored._connect_pipe = self.fluids._connect_pipe
        self.fluids = restored
        before = deepcopy(restored.state["sources"])
        self.obs["tick"] = 1
        self.obs["entities"] = deepcopy(self.original["entities"])
        self.obs["inventory"]["pumpjack"] = 1
        self.assertEqual(self.ensure()["type"], "build")
        self.assertEqual(restored.state["sources"], before)
        self.fluids._connect_pipe.reset_mock()
        self.finish_saved()
        self.assertEqual(self.ensure()["status"], "succeeded")
        self.assertEqual(self.fluids._connect_pipe.call_count, 2)

    def test_power_and_actual_pipe_connection_are_required_after_construction(self):
        self.expand_two()
        self.factory.ensure_power_connection.return_value = {"status": "waiting", "reason": "unpowered"}
        self.assertEqual(self.ensure()["reason"], "unpowered")
        self.factory.ensure_power_connection.return_value = READY
        self.fluids._connect_pipe.return_value = {"status": "blocked", "reason": "segments disconnected"}
        self.assertEqual(self.ensure()["reason"], "segments disconnected")

    def test_occupied_and_unroutable_candidates_are_not_reserved(self):
        self.occupied.add((12.5, .5))
        self.builder.route.side_effect = None
        self.builder.route.return_value = {"ok": False}
        done = self.ensure()
        self.assertEqual(done["status"], "blocked")
        self.assertIn("live actor position", done["reason"])
        self.assertEqual(len(self.fluids.state["sources"]), 1)
        self.bootstrap.ensure_item.assert_not_called()

    def test_factory_only_checkpoint_is_recovered_without_reserving_another_well(self):
        self.ensure()
        cell = deepcopy(self.fluids.state["sources"].pop(PREFIX + "1"))
        self.fluids._save()
        self.ensure()
        self.assertEqual(self.fluids.state["sources"][PREFIX + "1"], cell)
        self.assertEqual(len(self.fluids.state["sources"]), 2)

    def test_fluid_only_checkpoint_restores_shared_reservation_before_rebuild(self):
        self.ensure()
        self.factory.state["blocks"].pop("fluid:" + PREFIX + "1")
        self.ensure()
        self.assertIn("fluid:" + PREFIX + "1", self.factory.state["blocks"])

    def test_duplicate_saved_well_and_foreign_saved_pumpjack_fail_closed(self):
        self.ensure()
        self.fluids.state["sources"][PREFIX + "2"] = deepcopy(self.fluids.state["sources"][PREFIX + "1"])
        self.assertIn("unique well", self.ensure()["reason"])
        self.fluids.state["sources"].pop(PREFIX + "2")
        self.foreign.add((12.5, .5))
        self.assertIn("foreign", self.ensure()["reason"])

    def test_live_survey_rejects_wrong_world_missing_row_invalid_rate_and_duplicate_candidate(self):
        plans = [(PRIMARY, self.original)]
        valid = {"ok": True, "world_id": "fixture", "owned": [{"key": PRIMARY,
            "position": {"x": .5, "y": .5}, "built": True, "nominal_capacity_per_minute": 746.01}], "candidates": []}
        cases = [dict(valid, world_id="another"), dict(valid, owned=[])]
        for amount in (True, float("nan"), 0, -1):
            cases.append(dict(valid, owned=[dict(valid["owned"][0], nominal_capacity_per_minute=amount)]))
        cases.append(dict(valid, candidates=[{"position": {"x": .5, "y": .5}, "nominal_capacity_per_minute": 1000}]))
        for error in cases:
            self.error = error
            with self.subTest(error=error), self.assertRaises(ValueError):
                _survey(self.fluids, self.obs, plans, discover=True)

    def test_declined_exhausted_reservoir_is_explicit_and_never_claims_throughput(self):
        self.wells = {(.5, .5): 600}
        done = self.ensure()
        self.assertEqual(done["status"], "blocked")
        self.assertEqual(done["evidence"]["nominal_capacity_per_minute"], 600)
        self.assertEqual(done["evidence"]["candidates_observed"], 0)
        self.assertFalse(done["evidence"]["raw_source_capacity_verified"])
        self.assertFalse(done["evidence"]["throughput_verified"])

    def test_water_never_calls_oil_discovery(self):
        self.fluids.state["sources"]["raw:water"] = {"entities": [{"name": "offshore-pump", "position": {"x": 1, "y": 1}}], "ports": []}
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True, "world_id": "fixture", "nominal_capacity_per_minute": 72000}
        done = self.fluids._raw_capacity(self.obs, "water", 225, READY)
        self.assertEqual(done["status"], "succeeded")
        self.assertNotIn("oil_well_capacity", self.game.query.call_args.args[0])

    def test_well_limit_stops_expansion_with_truthful_existing_nominal_capacity(self):
        with patch("factorio_ai.deterministic_oil_capacity.MAX_WELLS", 2):
            self.ensure()
            self.finish_saved()
            self.game.query.reset_mock()
            done = self.ensure()
        self.assertEqual(done["reason"], "bounded oil well capacity is insufficient")
        self.assertAlmostEqual(done["evidence"]["nominal_capacity_per_minute"], 1646.01)
        self.assertEqual(self.game.query.call_count, 2)  # Capacity and current full segment extent.
        self.assertEqual(len(self.fluids.state["sources"]), 2)

    def explore(self):
        self.wells = {(.5, .5): 746.01}
        self.obs.setdefault("position", {"x": .5, "y": .5})
        return self.ensure()

    def test_exploration_uses_deterministic_backend_moves_and_requires_fresh_arrival(self):
        first = self.explore()
        self.assertEqual(first["type"], "move")
        self.assertEqual(first["position"], {"x": .5, "y": -127.5})
        self.obs["position"] = deepcopy(first["position"])
        self.assertEqual(self.explore()["type"], "move")  # Same tick cannot prove arrival.
        self.assertEqual(self.fluids.state["oil_exploration"]["index"], 0)
        self.obs["tick"] += 1
        self.assertEqual(self.explore()["status"], "waiting")
        self.assertEqual(self.fluids.state["oil_exploration"]["arrived"], [0])
        self.obs["tick"] += 1
        second = self.explore()
        self.assertEqual(second["position"], {"x": 128.5, "y": .5})
        self.assertFalse(second["evidence"]["raw_source_capacity_verified"])

    def test_pending_exploration_resumes_on_reload_and_resets_arrivals_on_rollback(self):
        first = self.explore()
        self.obs["position"] = first["position"]
        self.obs["tick"] += 1
        self.explore()
        self.obs["tick"] += 1
        second = self.explore()
        restored = FluidProduction(self.game, self.builder, self.catalog)
        restored.factory = self.factory
        self.fluids = restored
        self.obs["tick"] += 1
        self.assertEqual(self.explore()["position"], second["position"])
        self.obs["tick"] = 1
        self.obs["position"] = {"x": .5, "y": .5}
        self.assertEqual(self.explore()["position"], first["position"])
        self.assertEqual(self.fluids.state["oil_exploration"]["arrived"], [])

    def test_reobserved_charted_well_is_built_before_more_exploration(self):
        self.explore()
        self.wells[(12.5, .5)] = 1900
        done = self.ensure()
        self.assertEqual(done["reason"], "normal construction materials needed")
        self.assertEqual(len(self.fluids.state["sources"]), 2)
        self.assertEqual(self.fluids.state["oil_exploration"]["index"], 0)

    def test_strict_backend_uses_same_move_action_and_observed_nearby_arrival(self):
        self.game.backend = "character"
        first = self.explore()
        self.obs["position"] = {"x": first["position"]["x"] + 10, "y": first["position"]["y"]}
        self.obs["tick"] += 1
        self.assertEqual(self.explore()["status"], "waiting")
        self.assertEqual(self.fluids.state["oil_exploration"]["arrived"], [0])

    def test_unreachable_and_water_waypoints_are_recorded_without_false_arrival(self):
        self.standing_override = None
        self.assertIn("no standing position", self.explore()["reason"])
        state = self.fluids.state["oil_exploration"]
        self.assertEqual(state["unreached"], [0])
        self.assertEqual(state["arrived"], [])
        del self.standing_override
        self.explore()
        self.obs["tick"] += 7200
        self.assertIn("timed out", self.explore()["reason"])
        self.assertEqual(state["unreached"], [0, 1])
        self.assertEqual(state["arrived"], [])

    def test_exploration_has_finite_waypoints_and_honest_exhaustion(self):
        positions = []
        for _ in range(16):
            move = self.explore()
            positions.append(move["position"])
            self.obs["position"] = deepcopy(move["position"])
            self.obs["tick"] += 1
            self.explore()
            self.obs["tick"] += 1
        done = self.explore()
        self.assertEqual(done["status"], "blocked")
        self.assertIn("exploration exhausted", done["reason"])
        self.assertEqual(done["evidence"]["waypoints_arrived"], 16)
        self.assertEqual(len({(p["x"], p["y"]) for p in positions}), 16)
        self.assertTrue(all((p["x"]-.5)**2 + (p["y"]-.5)**2 < 320**2 for p in positions))

    def test_far_candidate_and_live_crude_segment_union_must_fit_engine_limit(self):
        candidate = self.fluids._utility_plan("pumpjack", {"x": 200.5, "y": .5}, 0, "crude-oil")
        self.extent_members = [{"name": "pipe", "position": {"x": -150.5, "y": .5}}]
        extent = _pipeline_extent(self.fluids, self.obs, self.original, candidate)
        self.assertFalse(extent["ok"])
        self.assertGreater(extent["width"], 320)
        self.extent_limit = 400
        self.assertTrue(_pipeline_extent(self.fluids, self.obs, self.original, candidate)["ok"])

    def test_saved_unbuilt_crude_route_is_included_in_pipeline_extent(self):
        self.factory.register_plan("future-crude", {"ok": True, "entities": [{"name": "pipe",
            "position": {"x": 330.5, "y": .5}, "_fluid": "crude-oil"}]}, self.obs)
        self.assertFalse(_pipeline_extent(self.fluids, self.obs, self.original)["ok"])
        done = self.ensure()
        self.assertIn("exceed live extent", done["reason"])
        self.assertNotIn("oil_exploration", self.fluids.state)

    def test_overextended_candidate_is_not_reserved_and_missing_extent_fails_closed(self):
        self.extent_limit = 10
        self.obs["position"] = {"x": .5, "y": .5}
        done = self.ensure()
        self.assertEqual(done["type"], "move")
        self.assertIn("exceed live extent", done["evidence"]["placement_issue"])
        self.assertEqual(len(self.fluids.state["sources"]), 1)
        self.extent_limit = None
        self.assertEqual(self.ensure()["status"], "blocked")

    def test_exploration_cannot_reuse_another_worlds_frontier_or_escape_its_waypoint(self):
        self.explore()
        self.fluids._sync({"world_id": "new-world", "tick": 500})
        self.assertNotIn("oil_exploration", self.fluids.state)
        self.fluids._sync(self.obs)
        self.fluids.state["sources"][PRIMARY] = deepcopy(self.original)
        self.standing_override = {"x": 900, "y": 900}
        self.assertIn("escaped waypoint bound", self.explore()["reason"])
        self.assertEqual(self.fluids.state["oil_exploration"]["index"], 0)

    def test_adjusted_assisted_standing_position_is_saved_and_actual_arrival_checked(self):
        self.standing_override = {"x": 4.5, "y": -127.5}
        move = self.explore()
        self.assertEqual(move["position"], self.standing_override)
        self.obs["position"] = {"x": 12, "y": -127.5}
        self.obs["tick"] += 1
        self.assertEqual(self.explore()["status"], "waiting")
        self.assertEqual(self.fluids.state["oil_exploration"]["arrived"], [0])


if __name__ == "__main__":
    unittest.main()
