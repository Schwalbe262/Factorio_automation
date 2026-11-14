"""Electric coal cutover on every supported starter coal cell layout."""

import json
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factorio_agent import electric_mining
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.electric_mining import (
    ElectricCoal,
    classify_coal_layout,
    east_drop,
    inside_footprint,
    matching_belt,
    replacement_for,
)
from factorio_agent.power_layout import V2FactoryBuilder, compact_coal_cell

# Vanilla electric-mining-drill vector_to_place_result (north-facing).
ELECTRIC_DROP_VECTOR = (0, -1.85)
DRILL = {"x": 66, "y": -29}
# The plant's generating engine (power_grid.generating) on network 1.
ENGINE = {
    "name": "steam-engine",
    "position": {"x": 0.5, "y": 0.5},
    "unit_number": 900,
    "energy": 0,
    "status_name": "working",
    "electric_network_id": 1,
}


def boundary_plan(position=DRILL):
    """The plan V2FactoryBuilder._choose_coal_plan saves: compact cell plus route."""
    cell = compact_coal_cell(position)
    x, y = position["x"], position["y"]
    route = [
        {"name": "transport-belt", "position": {"x": x + 4.5, "y": y + 1.5}, "direction": 4},
        {"name": "transport-belt", "position": {"x": x + 5.5, "y": y + 1.5}, "direction": 8},
    ]
    return _plan(
        cell["entities"] + route,
        cell["ports"],
        drill=cell["drill"],
        template="dedicated_boundary_coal_feed_v2",
        mining_footprint=cell["mining_footprint"],
        layout_policy="pcb-starter-boundary-coal-v2",
    )


def legacy_plan(position=DRILL):
    """The plan RegularizePower saves: compact cell with policy pcb-trunks-v1."""
    cell = compact_coal_cell(position)
    return _plan(
        cell["entities"], cell["ports"], drill=cell["drill"], template="pcb_starter_coal", layout_policy="pcb-trunks-v1"
    )


def inner_arm_plan(position=DRILL):
    """The plan the base FactoryBuilder._choose_coal_plan saves."""
    cell = FactoryBuilder._coal_plan(dict(position))
    return _plan(cell["entities"], cell["ports"], drill=cell["drill"], template="dedicated_coal_feed")


def payload_of(script):
    """Decode the doubly JSON-encoded argument table of a survey query."""
    match = re.search(r'json_to_table\(("(?:[^"\\]|\\.)*")\)', script)
    return json.loads(json.loads(match.group(1)))


class FakeGame:
    def __init__(self, root):
        self.cfg = SimpleNamespace(runtime_dir=str(root))
        self.surveys = []

    def query(self, script):
        args = payload_of(script)
        self.surveys.append(args)
        center = args["new"]["position"]
        return {"ok": True, "drop": east_drop(center, ELECTRIC_DROP_VECTOR), "remaining": 5000}


class FakeBuilder:
    _occupied_by_plan = FactoryBuilder._occupied_by_plan

    def __init__(self, coal_plan):
        self.state = {
            "world_id": "w",
            "coal_plan": coal_plan,
            "power_plan": {"ok": True, "entities": [], "ports": []},
        }
        self.saves = 0
        self.ensured = []

    def _sync(self, obs):
        assert obs["world_id"] == "w"

    def _save(self):
        self.saves += 1

    def ensure_plan(self, obs, plan):
        """Succeeds as if every plan pole stood: they appear in ``obs`` on the generating network 1."""
        self.ensured.append(plan)
        have = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in obs["entities"]}
        for e in plan.get("entities", []):
            if (e["name"], e["position"]["x"], e["position"]["y"]) not in have:
                obs["entities"].append(dict(deepcopy(e), unit_number=500 + len(obs["entities"]), electric_network_id=1))
        return {"status": "succeeded"}

    def power_evidence(self, power, coal):
        return {"ok": True, "boiler_fuel": 8_000_000, "coal_on_belts": 8}


class FakeConnection:
    """plan_connection stand-in; refuses goals listed in ``refused``."""

    def __init__(self, refused=()):
        self.refused = {tuple(p) for p in refused}
        self.goals = []

    def __call__(self, game, builder, obs, destination, reserved=()):
        goal = (destination["x"], destination["y"])
        self.goals.append(goal)
        if goal in self.refused:
            return {"ok": False, "reason": "power_target_footprint_obstructed"}
        return _plan(
            [{"name": "small-electric-pole", "position": dict(destination), "direction": 0}],
            [],
            template="power_grid_extension",
            destination=dict(destination),
            wire_hops=2,
        )


def world(plan):
    """Observed entities for every plan entity, with stable unit numbers."""
    rows = []
    for index, e in enumerate(plan["entities"], start=10):
        row = dict(deepcopy(e), unit_number=index)
        if e["name"] == "burner-mining-drill":
            row["unit_number"] = 1
        if e["name"] == "burner-inserter":
            row["unit_number"] = 2
        rows.append(row)
    rows.append(dict(ENGINE))
    return {
        "world_id": "w",
        "tick": 1000,
        "technologies": {"electric-mining-drill": True},
        "inventory": {"electric-mining-drill": 1},
        "entities": rows,
    }


def without(obs, name):
    obs = deepcopy(obs)
    obs["entities"] = [e for e in obs["entities"] if e["name"] != name]
    return obs


class Harness:
    def __init__(self, directory, plan, connection=None):
        self.game = FakeGame(directory)
        self.builder = FakeBuilder(plan)
        self.bootstrap = SimpleNamespace(ensure_item=lambda obs, item, count: {"type": "craft", "item": item})
        self.connection = connection or FakeConnection()
        self.routine = ElectricCoal(self.game, self.bootstrap, self.builder)

    def step(self, obs):
        with mock.patch.object(electric_mining, "plan_connection", self.connection):
            return self.routine.step(obs)


class LayoutGeometryTests(unittest.TestCase):
    """Facts the cutover relies on, for each cell generator."""

    def test_v2_builder_uses_the_east_facing_compact_cell(self):
        self.assertIs(V2FactoryBuilder._coal_plan, compact_coal_cell)
        for position in ({"x": 66, "y": -29}, {"x": -48, "y": 15}, {"x": 0, "y": 0}):
            self.assertEqual(compact_coal_cell(position)["drill"]["direction"], 4)
            self.assertEqual(FactoryBuilder._coal_plan(dict(position))["drill"]["direction"], 4)

    def test_electric_drop_lands_on_a_retained_plan_belt(self):
        for make in (boundary_plan, legacy_plan, inner_arm_plan):
            for position in ({"x": 66, "y": -29}, {"x": -48, "y": 15}, {"x": 3, "y": 120}):
                plan = make(position)
                center = replacement_for(plan["drill"]["position"])["position"]
                belt = matching_belt(plan["entities"], east_drop(center, ELECTRIC_DROP_VECTOR))
                self.assertIsNotNone(belt, make.__name__)
                self.assertEqual(belt["position"], {"x": position["x"] + 1.5, "y": position["y"] - 0.5})

    def test_footprint_holds_only_the_old_drill_and_any_inner_fuel_arm(self):
        for make, inside in ((boundary_plan, []), (legacy_plan, []), (inner_arm_plan, ["burner-inserter"])):
            plan = make()
            center = replacement_for(plan["drill"]["position"])["position"]
            names = sorted(
                e["name"]
                for e in plan["entities"]
                if e["name"] != "burner-mining-drill" and inside_footprint(center, e["position"])
            )
            self.assertEqual(names, inside, make.__name__)

    def test_classification(self):
        self.assertEqual(classify_coal_layout(legacy_plan())["variant"], "legacy_trunk")
        boundary = classify_coal_layout(boundary_plan())
        self.assertEqual(boundary["variant"], "boundary_compact")
        self.assertEqual(boundary["displaced"], [])
        self.assertEqual([e["position"] for e in boundary["obsolete"]], [{"x": 67.5, "y": -28.5}])
        inner = classify_coal_layout(inner_arm_plan())
        self.assertEqual(inner["variant"], "inner_fuel_arm")
        self.assertEqual([e["position"] for e in inner["displaced"]], [{"x": 64.5, "y": -29.5}])
        self.assertEqual(inner["obsolete"], [])

    def test_unknown_or_altered_layouts_are_refused_precisely(self):
        self.assertEqual(classify_coal_layout(None)["reason"], "starter_coal_plan_required")
        unknown = boundary_plan()
        unknown["template"] = "something_else"
        self.assertEqual(classify_coal_layout(unknown)["reason"], "electric_coal_unsupported_coal_layout")
        moved = boundary_plan()
        for e in moved["entities"]:
            if e["name"] == "burner-inserter":
                e["position"] = {"x": 64.5, "y": -29.5}
        self.assertEqual(classify_coal_layout(moved)["reason"], "electric_coal_fuel_inserter_not_at_template_position")
        crossed = boundary_plan()
        crossed["entities"].append({"name": "transport-belt", "position": {"x": 64.5, "y": -30.5}, "direction": 0})
        self.assertEqual(classify_coal_layout(crossed)["reason"], "electric_coal_footprint_holds_retained_plan_entity")
        turned = boundary_plan()
        turned["drill"]["direction"] = 8
        self.assertEqual(classify_coal_layout(turned)["reason"], "electric_coal_drill_facing_unsupported")


class BoundaryCutoverTests(unittest.TestCase):
    def test_boundary_layout_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = boundary_plan()
            h = Harness(directory, plan)
            obs = world(plan)
            action = h.step(obs)
            self.assertEqual(h.routine.state["layout_variant"], "boundary_compact")
            self.assertEqual(h.game.surveys[0]["ignore"], [1])
            self.assertEqual(h.connection.goals, [(67.5, -31.5)])
            self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 1))
            self.assertEqual(h.builder.state["electric_coal_cutover"]["old"]["position"], DRILL)
            obs = without(obs, "burner-mining-drill")
            action = h.step(obs)
            self.assertEqual(action["type"], "build")
            self.assertEqual(
                (action["name"], action["position"], action["direction"]),
                ("electric-mining-drill", {"x": 65.5, "y": -29.5}, 4),
            )
            drill = dict(
                replacement_for(DRILL),
                unit_number=30,
                electric_network_connected=True,
                energy=1000,
                status_name="working",
            )
            obs["entities"].append(drill)
            action = h.step(obs)
            self.assertEqual((action["type"], action["position"]), ("take", {"x": 67.5, "y": -29.5}))
            saved = h.builder.state["coal_plan"]
            self.assertEqual(saved["drill"]["name"], "electric-mining-drill")
            self.assertNotIn("burner-inserter", saved["required_items"])
            self.assertNotIn("burner-mining-drill", saved["required_items"])
            self.assertEqual(saved["layout_policy"], "pcb-starter-boundary-coal-v2")
            self.assertNotIn("electric_coal_cutover", h.builder.state)
            obs["tick"] += 400
            for e in obs["entities"]:
                if e["position"] == {"x": 67.5, "y": -29.5}:
                    e["belt_inventory"] = {"coal": 3}
            action = h.step(obs)
            self.assertEqual(
                (action["type"], action["name"], action["expected_entity_unit"]), ("mine", "burner-inserter", 2)
            )
            result = h.step(without(obs, "burner-inserter"))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["evidence"]["electric_drill_unit"], 30)

    def test_boundary_power_goal_falls_back_around_the_footprint(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = boundary_plan()
            connection = FakeConnection(refused=[(67.5, -31.5)])
            h = Harness(directory, plan, connection)
            h.step(world(plan))
            self.assertEqual(connection.goals[0], (67.5, -31.5))
            self.assertGreater(len(connection.goals), 1)
            pole = h.routine.state["power_plan"]["destination"]
            center = replacement_for(DRILL)["position"]
            self.assertEqual(max(abs(pole["x"] - center["x"]), abs(pole["y"] - center["y"])), 2)

    def test_unknown_layout_blocks_without_acting(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = boundary_plan()
            plan["layout_policy"] = "pcb-something-v9"
            h = Harness(directory, plan)
            result = h.step(world(plan))
            self.assertEqual(result, {"status": "blocked", "reason": "electric_coal_unsupported_coal_layout"})
            self.assertEqual(h.game.surveys, [])
            self.assertFalse((Path(directory) / "electric-coal.json").exists())


class InnerFuelArmCutoverTests(unittest.TestCase):
    def test_inner_inserter_leaves_plan_and_world_before_the_drill(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = inner_arm_plan()
            h = Harness(directory, plan)
            obs = world(plan)
            action = h.step(obs)
            self.assertEqual(h.game.surveys[0]["ignore"], [1, 2])
            # The first ring goal is a retained belt of this cell; it is skipped.
            self.assertNotIn((67.5, -31.5), h.connection.goals)
            self.assertEqual(
                (action["type"], action["name"], action["expected_entity_unit"]), ("mine", "burner-inserter", 2)
            )
            self.assertNotIn("burner-inserter", h.builder.state["coal_plan"]["required_items"])
            self.assertEqual(h.builder.state["coal_plan"]["template"], "dedicated_coal_feed")
            obs = without(obs, "burner-inserter")
            action = h.step(obs)
            self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 1))
            obs = without(obs, "burner-mining-drill")
            self.assertEqual(h.step(obs)["type"], "build")

    def test_changed_inner_inserter_identity_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = inner_arm_plan()
            h = Harness(directory, plan)
            obs = world(plan)
            h.step(obs)
            for e in obs["entities"]:
                if e["name"] == "burner-inserter":
                    e["unit_number"] = 99
            result = h.step(obs)
            self.assertEqual(result, {"status": "blocked", "reason": "displaced_fuel_inserter_identity_changed"})


class LegacyCutoverTests(unittest.TestCase):
    def test_legacy_trunk_keeps_its_single_goal_and_post_proof_inserter_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = legacy_plan()
            h = Harness(directory, plan)
            action = h.step(world(plan))
            self.assertEqual(h.routine.state["layout_variant"], "legacy_trunk")
            self.assertEqual(h.game.surveys[0]["ignore"], [1])
            self.assertEqual(h.connection.goals, [(67.5, -31.5)])
            self.assertEqual(h.routine.state["displaced_inserters"], [])
            self.assertEqual([e["unit_number"] for e in h.routine.state["obsolete_inserters"]], [2])
            self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 1))

    def test_legacy_power_goal_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = legacy_plan()
            connection = FakeConnection(refused=[(67.5, -31.5)])
            h = Harness(directory, plan, connection)
            result = h.step(world(plan))
            self.assertEqual((result["status"], result["reason"]), ("blocked", "power_target_footprint_obstructed"))
            self.assertEqual(result["plan_needs"]["kind"], "power")
            self.assertEqual(connection.goals, [(67.5, -31.5)])

    def test_site_rung_gives_the_legacy_cell_the_whole_goal_ring(self):
        from factorio_agent import remedy

        with tempfile.TemporaryDirectory() as directory:
            plan = legacy_plan()
            connection = FakeConnection(refused=[(67.5, -31.5)])
            h = Harness(directory, plan, connection)
            with remedy.applied({electric_mining.POWER_RING_OPTION: True}):
                action = h.step(world(plan))
            self.assertGreater(len(connection.goals), 1)
            self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", 1))


class PowerJobCutoverWaitTests(unittest.TestCase):
    def test_power_job_waits_instead_of_rebuilding_the_mined_burner(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = boundary_plan()
            builder = FactoryBuilder.__new__(FactoryBuilder)
            builder.catalog = SimpleNamespace(fingerprint="c")
            builder.path = Path(directory) / "factory-builder.json"
            power = {"ok": True, "entities": [{"name": "boiler", "position": {"x": 0, "y": 0}}], "ports": []}
            builder.state = {
                "schema_version": 1,
                "world_id": "w",
                "seeds": {},
                "catalog_fingerprint": "c",
                "power_plan": power,
                "coal_plan": plan,
                "electric_coal_cutover": {"old": plan["drill"], "new": replacement_for(DRILL)},
            }
            ensured = []
            builder.ensure_plan = lambda obs, p: ensured.append(p) or {"status": "succeeded"}
            obs = without(world(plan), "burner-mining-drill")
            obs["technologies"]["steam-power"] = True
            result = builder.ensure_power(obs)
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(ensured, [power])


if __name__ == "__main__":
    unittest.main()
