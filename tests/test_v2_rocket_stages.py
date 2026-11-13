"""The rocket tail: ``rocket_silo``, ``rocket_parts``, ``rocket_launch`` (rocket_stages, rocket_feed)."""

import importlib.util
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import plan_needs, postmortem, remedy, rocket_feed, rocket_stages
from factorio_agent.core.deterministic_game import DeterministicGame, _without_line_comments
from factorio_agent.core.deterministic_launch import LAUNCH_LUA, OBSERVE_LAUNCH_LUA
from factorio_agent.jobs import CAPABILITIES, Ledger, Request, control, run_slice, submit, summary
from factorio_agent.site.geometry import Rect
from factorio_agent.site.planzones import Allocation
from test_v2_rocket_plan import space_age_catalog

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

WORLD = "world"
CHEST = "steel-chest"


def catalog():
    cat = space_age_catalog()
    for name in ("processing-unit", "steel-plate", "pipe", "iron-plate", "copper-plate", "copper-cable"):
        cat.items.setdefault(name, {"type": "item"})["stack_size"] = 100
    cat.items["low-density-structure"]["stack_size"] = 50
    cat.items["rocket-fuel"]["stack_size"] = 20
    return cat


class FakeSite:
    def __init__(self, count=1):
        self.count, self.kept, self.allocated = count, [], []

    def allocate_candidates(self, kind, along, depth, *, name, count, returns_to_bus, world_id):
        self.allocated.append((kind, along, depth, name, count))
        blocks = []
        for index in range(min(count, self.count)):
            x0 = 100 + 20 * index
            rect = Rect(x0, 50, x0 + along, 50 + depth)
            tap = Rect(x0, 47, x0 + along, 50)  # tap band north of the block
            blocks.append(
                Allocation(
                    f"{name}#{index}",
                    kind,
                    "S",
                    0,
                    along,
                    depth,
                    rect,
                    "E",
                    "N",
                    tap,
                    "N",
                    "S",
                    (x0, 50.0 + depth),
                    False,
                )
            )
        return blocks

    def keep_only(self, prefix, keep, world_id=None):
        self.kept.append((prefix, keep))


class FakeBootstrap:
    """``_unreserved_stock_observation`` keeps every entity; ``ensure_item`` takes from a chest or reports."""

    def __init__(self):
        self.calls = []

    def _unreserved_stock_observation(self, obs, item):
        return obs

    def ensure_item(self, obs, item, count):
        self.calls.append((item, count))
        have = int((obs.get("inventory") or {}).get(item, 0))
        if have >= count:
            return {"status": "succeeded", "reason": "required inventory available"}
        for e in obs.get("entities", []):
            if e.get("name") == CHEST and e.get("inventory", {}).get(item):
                return {
                    "type": "take",
                    "name": CHEST,
                    "position": e["position"],
                    "item": item,
                    "count": min(50, count - have, e["inventory"][item]),
                    "reason": f"collect produced {item}",
                }
        return {"status": "blocked", "reason": "required item needs a production machine", "item": item}


def chest(item, count, x=10.5, unit=900):
    return {"name": CHEST, "position": {"x": x, "y": 0.5}, "unit_number": unit, "inventory": {item: count}}


def observation(**changes):
    obs = {
        "ok": True,
        "world_id": WORLD,
        "tick": 1000,
        "inventory": {},
        "entities": [
            {
                "name": "steam-engine",
                "position": {"x": 0.5, "y": 0.5},
                "energy": 10,
                "electric_network_id": 1,
                "unit_number": 1,
            },
            {
                "name": "small-electric-pole",
                "position": {"x": 3.5, "y": 0.5},
                "electric_network_id": 1,
                "unit_number": 2,
            },
        ],
        "enabled_recipes": {
            "rocket-silo": True,
            "pipe": True,
            "rocket-part": True,
            "space-platform-starter-pack": True,
        },
        "production": {},
    }
    obs.update(changes)
    return obs


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = catalog()
        self.board({})
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root),
            query=Mock(
                return_value={
                    "ok": True,
                    "offered_watts": 9e6,
                    "demand_watts": 2e6,
                    "headroom_watts": 7e6,
                    "launch_peak_watts": 3.99e6,
                }
            ),
            backend="assisted",
        )
        self.builder = Mock()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.bootstrap = FakeBootstrap()
        self.site = FakeSite()
        patches = (
            patch.object(rocket_stages, "connect_power_groups", return_value={"ok": True, "entities": []}),
            patch.object(rocket_stages, "preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch.object(rocket_stages, "foreign_keepouts", return_value=[]),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.silo = rocket_stages.RocketSilo(self.game, self.builder, self.bootstrap, self.catalog, site=self.site)

    def board(self, blocks):
        path = self.root / "pcb-board.json"
        path.write_text(
            json.dumps(
                {
                    "world_id": WORLD,
                    "catalog_fingerprint": self.catalog.fingerprint,
                    "blocks": blocks,
                    "connections": [],
                }
            ),
            encoding="utf-8",
        )

    def placed(self):
        """The prepared state saved and the live silo row of its plan."""
        self.silo.step(observation(), {})
        state = self.silo.state()
        spot = state["layout"]["silo"]["position"]
        row = {
            "name": "rocket-silo",
            "type": "rocket-silo",
            "position": dict(spot),
            "unit_number": 77,
            "energy": 100,
            "electric_network_connected": True,
            "electric_network_id": 1,
            "recipe": "rocket-part",
            "rocket_parts": 0,
            "rocket_silo_status": "building_rocket",
            "inventory": {},
        }
        return state, row


class RocketSiloTests(Base):
    def test_prepare_reserves_a_13x13_block_with_the_feed_side_on_the_tap_band(self):
        result = self.silo.prepare(observation())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.site.allocated[0][:3], ("production", 13, 13))
        (x0, y0), (x1, y1) = result["layout"]["reserved_area"]
        self.assertEqual((x1 - x0, y1 - y0), (13, 13))
        silo = result["layout"]["silo"]
        self.assertEqual(silo["position"], {"x": x0 + 6.5, "y": y0 + 6.5})
        self.assertNotIn("recipe", silo)  # fixed recipe: never a recipe action
        ports = result["layout"]["feed_ports"]
        self.assertEqual(sorted(p["item"] for p in ports), ["low-density-structure", "processing-unit", "rocket-fuel"])
        self.assertTrue(all(p["position"]["y"] == y0 + 0.5 for p in ports), ports)  # north: the tap band
        self.assertEqual([e["name"] for e in result["plan"]["entities"]][-1], "rocket-silo")
        self.assertEqual(self.site.kept[-1], ("rocket_silo#", "rocket_silo#0"))

    def test_no_site_returns_site_needs_and_the_ladder_has_rungs_and_a_dry_run(self):
        self.site.count = 0
        result = self.silo.step(observation(), {})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(plan_needs.from_result("rocket_silo", result)["kind"], "site")
        rungs = {r.name for r in remedy.ladder("rocket_silo")}
        self.assertTrue({"site:more_silo_blocks", "relax:narrow_silo_ring"} <= rungs)
        self.assertIn("rocket_silo", postmortem.DRY_RUN)
        self.assertIn("rocket_parts", postmortem.DRY_RUN)
        with remedy.applied({"rocket_silo_candidates": 99, "rocket_silo_ring": 0}):
            self.site.count = 1
            narrow = self.silo.prepare(observation())
        self.assertEqual(self.site.allocated[-1][1:3], (11, 11))  # ring clamps to RING_MIN
        self.assertEqual(self.site.allocated[-1][4], rocket_stages.CANDIDATES_MAX)
        self.assertTrue(narrow["ok"])

    def test_the_block_is_saved_and_published_before_the_first_action(self):
        obs = observation(entities=observation()["entities"] + [chest("steel-plate", 400)])
        result = self.silo.step(obs, {})
        self.assertEqual(result["type"], "take")
        self.assertEqual(result["item"], "steel-plate")
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertIn("rocket_silo", board["blocks"])
        self.assertTrue(self.silo.state()["power"]["ok"])

    def test_missing_ingredient_is_a_named_wait_with_item_producer_and_claims(self):
        obs = observation(inventory={"steel-plate": 1000, "pipe": 100})
        result = self.silo.step(obs, {})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "rocket_silo_waits_for_processing_unit")
        evidence = result["evidence"]
        self.assertEqual((evidence["item"], evidence["need"]), ("processing-unit", 200))
        self.assertEqual(evidence["producer"], "fluid_assembly")
        self.assertEqual(evidence["producer_status"], "missing")
        self.assertEqual(evidence["needs"]["steel-plate"], 1000)

    def test_pipes_are_hand_crafted_from_plates_and_the_silo_once_everything_is_carried(self):
        obs = observation(inventory={"steel-plate": 1000, "processing-unit": 200})
        obs["entities"].append(chest("iron-plate", 80))
        result = self.silo.step(obs, {})
        self.assertEqual((result["type"], result["item"]), ("take", "iron-plate"))
        held = {"steel-plate": 1000, "processing-unit": 200, "iron-plate": 100}
        self.silo.step(observation(inventory=held), {})
        self.assertEqual(self.bootstrap.calls[-1], ("rocket-silo", 1))  # the craft goes through ensure_item

    def test_published_row_stops_large_hand_crafts(self):
        self.board({"production_row_pipe": {"ports": [{"direction": "output", "item": "pipe", "name": CHEST}]}})
        obs = observation(inventory={"steel-plate": 1000, "processing-unit": 200, "iron-plate": 100})
        result = self.silo.step(obs, {})
        self.assertEqual(result["reason"], "rocket_silo_waits_for_pipe")
        self.assertEqual(result["evidence"]["producer_status"], "published_stock")

    def test_belt_output_rows_feed_their_consumer_and_are_never_collected(self):
        # A belt row (produce tail.mode == "belt") publishes a port without a chest name.
        belt = {"direction": "output", "item": "pipe", "position": {"x": 10.5, "y": 0.5}, "facing": 4}
        pu = dict(belt, item="processing-unit")
        self.board({"production_row_pipe": {"ports": [belt]}, "production_row_pu": {"ports": [pu]}})
        on_belt = {"name": "transport-belt", "position": {"x": 10.5, "y": 0.5}, "unit_number": 5}
        on_belt["belt_inventory"] = {"pipe": 8, "processing-unit": 8}
        obs = observation(inventory={"steel-plate": 1000, "processing-unit": 200})
        obs["entities"] += [on_belt, chest("iron-plate", 100)]
        result = self.silo.step(obs, {})
        # pipes are not capped by the belt row: their plates are collected for a hand craft
        self.assertEqual((result["type"], result["item"]), ("take", "iron-plate"))
        result = self.silo.step(observation(inventory={"steel-plate": 1000, "iron-plate": 100}), {})
        self.assertEqual(result["reason"], "rocket_silo_waits_for_processing_unit")
        self.assertEqual(result["evidence"]["producer_status"], "belt_row_only")
        row = rocket_stages.receipt(self.root, observation(), self.catalog)["rocket_silo"]
        statuses = {m["item"]: m["status"] for m in row["missing"]}
        self.assertEqual(statuses["processing-unit"], "belt_row_only")
        taken = [c for c in self.bootstrap.calls if c[0] in ("pipe", "processing-unit")]
        self.assertEqual(taken, [])  # nothing is taken off a belt row's port

    def test_power_shortfall_waits_and_reaches_the_receipt(self):
        self.game.query.return_value = {"ok": True, "offered_watts": 1e6, "demand_watts": 9.5e5, "headroom_watts": 5e4}
        result = self.silo.step(observation(), {})
        self.assertEqual(result["reason"], "rocket_silo_power_headroom_short")
        self.assertGreater(result["evidence"]["short_watts"], 0)
        self.assertEqual(self.silo.step(observation(), {})["reason"], "rocket_silo_power_headroom_short")
        self.assertEqual(self.game.query.call_count, 1)  # rechecked after POWER_RECHECK_TICKS only
        row = rocket_stages.receipt(self.root, observation(), self.catalog)["rocket_silo"]
        self.assertGreater(row["power_short_watts"], 0)
        body = self.game.query.call_args[0][0]
        self.assertNotIn("PAYLOAD", body)
        self.assertNotIn("--", " ".join(_without_line_comments(line) for line in body.splitlines()))

    def test_carried_silo_is_built_and_proved_on_a_fresh_observation(self):
        state, row = self.placed()
        self.builder.ensure_plan.return_value = {"type": "build", "name": "rocket-silo"}
        self.assertEqual(self.silo.step(observation(inventory={"rocket-silo": 1}), {})["type"], "build")
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        obs = observation()
        obs["entities"].append(row)
        result = self.silo.step(obs, {})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.silo.proof(obs)["recipe"], "rocket-part")
        for change in ({"energy": 0}, {"electric_network_connected": False}, {"recipe": None}):
            fresh = observation()
            fresh["entities"].append(dict(row, **change))
            self.assertIsNone(self.silo.proof(fresh), change)
        moved = observation()
        moved["entities"].append(dict(row, position={"x": 0.5, "y": 0.5}))
        self.assertIsNone(self.silo.proof(moved))

    def test_world_change_blocks(self):
        self.placed()
        result = self.silo.step(observation(world_id="other"), {})
        self.assertEqual(result["status"], "blocked")


class RocketPartsTests(Base):
    def setUp(self):
        super().setUp()
        self.state, self.row = self.placed()
        self.parts = rocket_feed.RocketParts(self.game, self.builder, self.bootstrap, self.catalog, self.silo)

    def obs(self, carried=None, **row):
        obs = observation(inventory=carried or {})
        obs["entities"].append(dict(self.row, **row))
        return obs

    def test_hand_feed_moves_carried_stacks_into_the_crafter_input(self):
        result = self.parts.step(self.obs({"processing-unit": 120, "rocket-fuel": 30}, rocket_parts=10), {})
        self.assertEqual(result["type"], "feed_rocket_silo")
        rocket_feed.validate_feed(result)
        items = {r["item"]: r["count"] for r in result["items"]}
        self.assertEqual(items, {"processing-unit": 40, "rocket-fuel": 20})  # 40 parts left; a fuel stack is 20
        self.assertEqual(result["unit_number"], 77)

    def test_a_half_full_input_is_not_topped_up_and_the_next_item_is_collected(self):
        obs = self.obs({"processing-unit": 50}, inventory={"processing-unit": 60})
        obs["entities"].append(chest("low-density-structure", 50))
        result = self.parts.step(obs, {})
        self.assertEqual((result["type"], result["item"]), ("take", "low-density-structure"))

    def test_wait_names_the_missing_ingredient_and_its_producer(self):
        result = self.parts.step(self.obs(), {})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "rocket_parts_waits_for_low_density_structure")
        # The producer is the rocket bill's classification of the item (rocket_plan.classify).
        from factorio_agent import rocket_plan

        bill = rocket_plan.rocket_bill(self.catalog, researched=())
        row = next(r for r in bill["items"] if r["item"] == "low-density-structure")
        self.assertEqual(result["evidence"]["producer"], row["producer"]["capability"])
        self.assertNotEqual(result["evidence"]["producer"], "unknown")

    def test_everything_delivered_waits_for_the_silo(self):
        full = {"processing-unit": 50, "low-density-structure": 50, "rocket-fuel": 50}
        result = self.parts.step(self.obs(inventory=full), {})
        self.assertEqual(result["reason"], "rocket_parts_silo_crafting")

    def test_the_rocket_sequence_and_rocket_ready(self):
        self.assertEqual(
            self.parts.step(self.obs(rocket_silo_status="doors_opening"), {})["reason"],
            "rocket_parts_rocket_being_prepared",
        )
        ready = self.obs(rocket_silo_status="rocket_ready", rocket_parts=0)
        self.assertEqual(self.parts.step(ready, {})["status"], "succeeded")
        self.assertEqual(self.parts.proof(ready)["rocket_silo_status"], "rocket_ready")
        self.assertIsNone(self.parts.proof(self.obs(rocket_parts=50)))  # a counter is no proof, the status is

    def test_wait_versus_spend_prefers_the_hand_feed_for_one_rocket(self):
        per = {"processing-unit": 1, "low-density-structure": 1, "rocket-fuel": 1}
        stacks = {"processing-unit": 100, "low-density-structure": 50, "rocket-fuel": 20}
        self.assertEqual(rocket_feed.feed_choice(50, per, stacks, 120)["mode"], "hand")
        self.assertEqual(rocket_feed.feed_choice(500, per, stacks, 120)["mode"], "belt")
        self.assertEqual(rocket_feed.feed_choice(500, per, stacks, None)["mode"], "hand")

    def test_belt_feed_is_planned_from_free_belt_ports_published_then_built(self):
        ports = [
            {"direction": "output", "item": item, "position": {"x": 200.5 + i, "y": 0.5}, "facing": 8}
            for i, item in enumerate(("processing-unit", "low-density-structure", "rocket-fuel"))
        ]
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        board["blocks"]["rows"] = {"ports": ports}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        route = {
            "ok": True,
            "entities": [{"name": "transport-belt", "position": {"x": 300.5, "y": 0.5}, "direction": 8}],
        }
        with (
            patch.object(rocket_feed, "plan_connection", return_value=route),
            patch.object(rocket_feed, "preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch.object(
                rocket_feed, "feed_choice", return_value={"mode": "belt", "hand_seconds": 1, "belt_seconds": 0}
            ),
        ):
            self.builder.ensure_plan.return_value = {"type": "build", "name": "inserter"}
            result = self.parts.step(self.obs(), {})
        self.assertEqual(result["type"], "build")
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(len(board["blocks"]["rocket_silo"]["ports"]), 3)
        self.assertEqual({c["owner"] for c in board["connections"]}, {"rocket_silo"})

    def test_unknown_feed_outcome_is_reconciled_from_the_observation_never_replayed(self):
        jobs = {
            "silo": {
                "key": "silo",
                "world_id": WORLD,
                "capability": "rocket_silo",
                "parameters": {},
                "status": "succeeded",
                "reason": "",
                "created_tick": 0,
                "last_tick": 0,
            }
        }
        (self.root / "jobs.json").write_text(
            json.dumps({"schema_version": 2, "jobs": jobs, "world_id": WORLD, "last_tick": 0}), encoding="utf-8"
        )
        live = self.obs({"processing-unit": 50, "low-density-structure": 50, "rocket-fuel": 20})

        class Game:
            def __init__(self):
                self.observation, self.actions, self.lose = live, [], True

            def observe(self):
                return json.loads(json.dumps(self.observation))

            def act(self, action):
                self.actions.append(action)
                if self.lose:
                    raise TimeoutError("feed outcome unknown")
                return {"ok": True, "moved": 1, "status": "succeeded"}

            def save(self):
                pass

        parts = self.parts

        class Executor:
            def step(self, capability, obs, parameters=None):
                return parts.step(obs, parameters)

            def proof(self, capability, obs, result, parameters=None):
                return parts.proof(obs)

        game = Game()
        submit(self.root, game, Request("parts", WORLD, 1000, "rocket_parts"))
        with self.assertRaises(TimeoutError):
            run_slice(self.root, game, Executor(), "parts")
        job = Ledger(self.root).data["jobs"]["parts"]
        self.assertEqual(job["reason"], "action_outcome_unknown_reconcile_required")
        self.assertEqual(job["pending_action"]["type"], "feed_rocket_silo")
        with self.assertRaisesRegex(ValueError, "known blocked"):
            control(self.root, game, "parts", "retry")
        control(self.root, game, "parts", "reconcile")
        # The feed did happen: the silo input holds the items, the actor no longer carries them.
        moved = {"processing-unit": 50, "low-density-structure": 50, "rocket-fuel": 20}
        silo_row = dict(self.row, inventory=moved)
        game.observation = dict(live, inventory={}, entities=observation()["entities"] + [silo_row])
        game.lose = False
        run_slice(self.root, game, Executor(), "parts", seconds=0.001)
        self.assertEqual(len(game.actions), 1)  # replanned from the observation: nothing to feed again
        job = Ledger(self.root).data["jobs"]["parts"]
        self.assertTrue(job["reason"].startswith("rocket_parts_"), job["reason"])


class FeedEnvelopeTests(unittest.TestCase):
    def action(self, **changes):
        action = {
            "type": "feed_rocket_silo",
            "name": "rocket-silo",
            "position": {"x": 10.5, "y": 10.5},
            "unit_number": 7,
            "world_id": WORLD,
            "items": [{"item": "processing-unit", "count": 10}],
        }
        action.update(changes)
        return action

    def test_malformed_envelopes_fail_before_native_io(self):
        game = SimpleNamespace(query=Mock(), expected_world=WORLD, backend="assisted")
        for change in (
            {"items": []},
            {"items": [{"item": "x", "count": 0}]},
            {"items": [{"item": "x", "count": 1}, {"item": "x", "count": 1}]},
            {"unit_number": True},
            {"world_id": "other"},
            {"position": {"x": float("nan"), "y": 0}},
            {"extra": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                rocket_feed.execute_feed_silo(game, self.action(**change))
        game.query.assert_not_called()

    def test_an_inconsistent_receipt_is_an_unknown_outcome(self):
        game = SimpleNamespace(
            query=Mock(
                return_value={
                    "ok": True,
                    "moved": 5,
                    "status": "succeeded",
                    "items": [{"item": "processing-unit", "moved": 4}],
                }
            ),
            expected_world=WORLD,
            backend="assisted",
        )
        with self.assertRaisesRegex(RuntimeError, "reconcile"):
            rocket_feed.execute_feed_silo(game, self.action())

    def test_world_bound_game_dispatches_the_feed(self):
        from factorio_agent.runtime import WorldBoundGame

        game = WorldBoundGame.__new__(WorldBoundGame)
        game.expected_world, game.backend = WORLD, "assisted"
        game.query = Mock(
            return_value={
                "ok": True,
                "moved": 0,
                "status": "waiting",
                "items": [{"item": "processing-unit", "moved": 0}],
            }
        )
        game._record_action = Mock(side_effect=lambda action, result: result)
        self.assertEqual(game.act(self.action())["moved"], 0)
        self.assertEqual(game._record_action.call_count, 1)


# Lua bodies that may run in the game: none writes a part counter, a silo status, research or items out of nothing.
FORBIDDEN = (
    r"\.rocket_parts\s*=[^=]",
    r"\.rocket_silo_status\s*=[^=]",
    r"apply_starter_pack",
    r"create_entity",
    r"\.researched\s*=[^=]",
    r"set_research",
    r"inv\.insert",
    r"products_finished\s*=[^=]",
    r"crafting_progress\s*=[^=]",
    r"\.insert_fluid",
)


class NoGrantTests(unittest.TestCase):
    def test_new_bodies_grant_nothing_and_survive_the_one_line_form(self):
        bodies = {
            "feed": rocket_feed.FEED_SILO_LUA,
            "power": rocket_stages.POWER_LUA,
            "launch": LAUNCH_LUA,
            "observe_launch": OBSERVE_LAUNCH_LUA,
        }
        for name, body in bodies.items():
            one_line = " ".join(_without_line_comments(line) for line in body.splitlines())
            with self.subTest(body=name):
                self.assertNotIn("--", one_line)
                for pattern in FORBIDDEN:
                    self.assertIsNone(re.search(pattern, body), pattern)
        self.assertNotIn("insert", rocket_stages.POWER_LUA)
        self.assertNotIn("remove", rocket_stages.POWER_LUA)


def _lua_tools(lua):
    def table(v):
        if isinstance(v, dict):
            return lua.table_from({k: table(a) for k, a in v.items()})
        if isinstance(v, list):
            return lua.table_from([table(a) for a in v])
        return v

    def decode(v):
        if not hasattr(v, "items"):
            return v
        keys = list(v.keys())
        if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [decode(v[i]) for i in range(1, len(keys) + 1)]
        return {k: decode(a) for k, a in v.items()}

    lua.globals().helpers = lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
    return decode


def _one_line(body):
    return "return (function() " + " ".join(_without_line_comments(line) for line in body.splitlines()) + " end)()"


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativeFeedTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.decode = _lua_tools(self.lua)
        self.lua.execute(
            """
d={world_id="w"};f={};s={};defines={inventory={crafter_input=2}}
function success(t) t=t or {};t.ok=true;return t end
function failure(r) return {ok=false,reason=r} end
carried={["processing-unit"]=30,["rocket-fuel"]=5,["iron-plate"]=9}
inside={}
cap={["processing-unit"]=100,["rocket-fuel"]=20}
inv={valid=true}
function inv.get_item_count(spec) return carried[spec.name] or 0 end
function inv.remove(spec) local n=math.min(spec.count,carried[spec.name] or 0);carried[spec.name]=(carried[spec.name] or 0)-n;return n end
dest={valid=true}
function dest.insert(spec) local room=(cap[spec.name] or 0)-(inside[spec.name] or 0);local n=math.min(spec.count,room)
 if n>0 then inside[spec.name]=(inside[spec.name] or 0)+n end;return math.max(n,0) end
function dest.remove(spec) inside[spec.name]=(inside[spec.name] or 0)-spec.count;return spec.count end
recipe={ingredients={{type="item",name="processing-unit",amount=1},{type="item",name="low-density-structure",amount=1},{type="item",name="rocket-fuel",amount=1}}}
silo={valid=true,type="rocket-silo",force=f,unit_number=7,rocket_parts=12}
function silo.get_recipe() return recipe end
function silo.get_inventory(i) if i==defines.inventory.crafter_input then return dest end end
a={valid=true,force=f,get_main_inventory=function() return inv end,can_reach_entity=function() return true end}
function target(p,name) return silo end
"""
        )

        def query(body):
            return self.decode(self.lua.execute(_one_line(body)))

        self.game = SimpleNamespace(query=query, expected_world="w", backend="assisted")

    def action(self, items):
        return {
            "type": "feed_rocket_silo",
            "name": "rocket-silo",
            "position": {"x": 1.5, "y": 1.5},
            "unit_number": 7,
            "world_id": "w",
            "items": items,
        }

    def test_moves_exactly_what_the_input_accepts_and_charges_the_actor(self):
        receipt = rocket_feed.execute_feed_silo(
            self.game, self.action([{"item": "processing-unit", "count": 30}, {"item": "rocket-fuel", "count": 5}])
        )
        self.assertEqual(receipt["moved"], 35)
        self.assertEqual(receipt["rocket_parts"], 12)  # read, never written
        self.assertEqual(self.lua.eval('carried["processing-unit"]'), 0)
        self.assertEqual(self.lua.eval('inside["rocket-fuel"]'), 5)
        self.lua.execute('cap["rocket-fuel"]=7;carried["rocket-fuel"]=5')
        receipt = rocket_feed.execute_feed_silo(self.game, self.action([{"item": "rocket-fuel", "count": 5}]))
        self.assertEqual(receipt["moved"], 2)
        self.assertEqual(self.lua.eval('carried["rocket-fuel"]'), 3)
        self.assertEqual(self.lua.eval("silo.rocket_parts"), 12)

    def test_refuses_foreign_items_a_changed_silo_and_a_changed_world(self):
        self.assertEqual(
            rocket_feed.execute_feed_silo(self.game, self.action([{"item": "iron-plate", "count": 9}]))["reason"],
            "rocket_feed_item_not_an_ingredient:iron-plate",
        )
        self.assertEqual(self.lua.eval('carried["iron-plate"]'), 9)
        self.lua.execute("silo.unit_number=8")
        self.assertEqual(
            rocket_feed.execute_feed_silo(self.game, self.action([{"item": "processing-unit", "count": 1}]))["reason"],
            "rocket_silo_changed",
        )
        self.lua.execute('silo.unit_number=7;d.world_id="other"')
        self.assertEqual(
            rocket_feed.execute_feed_silo(self.game, self.action([{"item": "processing-unit", "count": 1}]))["reason"],
            "rocket_feed_world_changed",
        )


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativePowerTests(unittest.TestCase):
    def test_power_query_reads_offered_and_requested_power(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        decode = _lua_tools(lua)
        lua.execute(
            """
f={};game={tick=5}
flow={primary_output=100000,secondary_output=0,solar_output=0,primary_demand=40000,secondary_demand=0,consumption_satisfaction=1}
net={valid=true,flow_last_tick=flow}
pole={valid=true,force=f,electric_network={parent_network=net}}
prototypes={entity={["rocket-silo"]={active_energy_usage=66500}}}
function target(p,name) return pole end
"""
        )
        body = rocket_stages.power_payload(
            rocket_stages.POWER_LUA,
            {"position": {"x": 1.5, "y": 1.5}, "name": "small-electric-pole", "silo": "rocket-silo"},
        )
        result = decode(lua.execute(_one_line(body)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["headroom_watts"], 60000 * 60)
        self.assertEqual(result["launch_peak_watts"], 66500 * 60)


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.decode = _lua_tools(self.lua)
        self.lua.execute(
            """
storage={}
d={world_id="w"};game={tick=100};s={name="nauvis"}
defines={rocket_silo_status={rocket_ready=13,building_rocket=1},inventory={rocket_silo_rocket=5},cargo_destination={space_platform=3}}
function success(t) t=t or {};t.ok=true;return t end
function failure(r) return {ok=false,reason=r} end
calls={create=0,launch=0,apply=0}
unlocked=true
platform={valid=true,index=4,hub=nil,state=1}
function platform.apply_starter_pack() calls.apply=calls.apply+1 end
f={rockets_launched=0,technologies={["rocket-silo"]={researched=true}}}
function f.is_space_platforms_unlocked() return unlocked end
function f.create_space_platform(spec) calls.create=calls.create+1;return platform end
carried={["space-platform-starter-pack"]=1}
inv={valid=true}
function inv.get_item_count(name) return carried[name] or 0 end
function inv.remove(spec) local n=math.min(spec.count,carried[spec.name] or 0);carried[spec.name]=(carried[spec.name] or 0)-n;return n end
cargo_items={}
cargo={valid=true}
function cargo.get_contents() local out={};for k,v in pairs(cargo_items) do out[#out+1]={name=k,count=v} end;return out end
function cargo.get_item_count(name) return cargo_items[name] or 0 end
function cargo.insert(spec) cargo_items[spec.name]=(cargo_items[spec.name] or 0)+spec.count;return spec.count end
function cargo.remove(spec) cargo_items[spec.name]=(cargo_items[spec.name] or 0)-spec.count;return spec.count end
silo={valid=true,type="rocket-silo",unit_number=9,rocket_silo_status=13,rocket_parts=0}
silo.force=f
function silo.get_inventory(i) if i==defines.inventory.rocket_silo_rocket then return cargo end end
function silo.launch_rocket(dest) calls.launch=calls.launch+1;calls.dest=dest;return true end
a={valid=true,force=f,get_main_inventory=function() return inv end}
function target(p,name) return silo end
"""
        )

    def run_launch(self):
        game = DeterministicGame.__new__(DeterministicGame)
        game.backend = "assisted"
        captured = {}
        game.query = lambda body: captured.setdefault("body", body) and {"ok": True}
        game._record_action = lambda action, result: result
        game.act({"type": "launch", "position": {"x": 1.5, "y": 1.5}, "name": "rocket-silo"})
        return self.decode(self.lua.execute(_one_line(captured["body"])))

    def test_the_pack_is_loaded_paid_the_platform_requested_and_the_launch_ordered(self):
        result = self.run_launch()
        self.assertEqual(result["reason"], "launch_ordered", result)
        self.assertEqual(self.lua.eval('carried["space-platform-starter-pack"]'), 0)
        self.assertEqual(self.lua.eval('cargo_items["space-platform-starter-pack"]'), 1)
        self.assertEqual(self.lua.eval("calls.create"), 1)
        self.assertEqual(self.lua.eval("calls.launch"), 1)
        self.assertEqual(self.lua.eval("calls.dest.type"), 3)
        self.assertEqual(self.lua.eval("calls.apply"), 0)
        self.assertEqual(self.run_launch()["reason"], "starter_pack_in_transit")  # no second launch
        self.lua.execute("platform.hub={valid=true};f.rockets_launched=1")
        self.assertEqual(self.run_launch()["status"], "succeeded")
        observed = self.decode(self.lua.execute(_one_line(OBSERVE_LAUNCH_LUA)))
        self.assertEqual(observed["status"], "succeeded")
        self.assertTrue(rocket_feed.launch_verified({"launch": observed}))

    def test_locked_platforms_or_a_hub_before_delivery_refuse_the_launch(self):
        self.lua.execute("unlocked=false")
        self.assertEqual(self.run_launch()["reason"], "space_platforms_locked")
        self.assertEqual(self.lua.eval("calls.launch"), 0)
        self.lua.execute("unlocked=true;platform.hub={valid=true}")
        self.assertEqual(self.run_launch()["reason"], "platform_hub_exists_before_delivery")
        self.assertEqual(self.lua.eval("calls.launch"), 0)

    def test_a_missing_pack_is_never_created(self):
        self.lua.execute('carried["space-platform-starter-pack"]=0')
        self.assertEqual(self.run_launch()["reason"], "starter_pack_required")
        self.assertEqual(self.lua.eval("calls.create"), 0)


class RocketLaunchTests(Base):
    def setUp(self):
        super().setUp()
        self.state, self.row = self.placed()
        self.launch = rocket_feed.RocketLaunch(self.game, self.bootstrap, self.catalog, self.silo)

    def obs(self, launch=None, inventory=None, **row):
        obs = observation(inventory=inventory or {}, launch=launch or {"status": "not_started", "rockets_launched": 0})
        obs["entities"].append(dict(self.row, **row))
        return obs

    def test_waits_for_parts_then_collects_or_crafts_the_pack_then_launches(self):
        self.assertEqual(self.launch.step(self.obs(), {})["reason"], "rocket_launch_waits_for_rocket_parts")
        ready = self.obs(rocket_silo_status="rocket_ready")
        waiting = self.launch.step(ready, {})
        self.assertEqual(waiting["reason"], "rocket_launch_waits_for_steel_plate")
        self.assertEqual(waiting["evidence"]["producer"], "steel_production")
        carried = self.obs(inventory={"space-platform-starter-pack": 1}, rocket_silo_status="rocket_ready")
        action = self.launch.step(carried, {})
        self.assertEqual((action["type"], action["position"]), ("launch", self.row["position"]))
        self.launch.record_action(carried, action, {"ok": True, "status": "waiting", "reason": "launch_not_ready"})
        self.assertEqual(self.launch.step(carried, {})["reason"], "rocket_launch_launch_not_ready")

    def test_proof_needs_the_live_launch_receipt(self):
        done = {
            "status": "succeeded",
            "ordered": True,
            "platform_hub_valid": True,
            "baseline": 0,
            "rockets_launched": 1,
        }
        obs = self.obs(launch=done)
        self.assertEqual(self.launch.step(obs, {})["status"], "succeeded")
        self.assertEqual(self.launch.proof(obs)["rockets_launched"], 1)
        for change in ({"platform_hub_valid": False}, {"rockets_launched": 0}, {"ordered": False}):
            self.assertIsNone(self.launch.proof(self.obs(launch=dict(done, **change))), change)

    def test_summary_reports_the_launch_from_the_observation(self):
        ledger = Ledger(self.root)
        obs = self.obs()
        self.assertFalse(summary(obs, ledger)["rocket_launch_verified"])
        done = {
            "status": "succeeded",
            "ordered": True,
            "platform_hub_valid": True,
            "baseline": 0,
            "rockets_launched": 1,
        }
        self.assertTrue(summary(self.obs(launch=done), ledger)["rocket_launch_verified"])
        claimed = {
            "status": "succeeded",
            "ordered": True,
            "platform_hub_valid": True,
            "baseline": 2,
            "rockets_launched": 2,
        }
        self.assertFalse(summary(self.obs(launch=claimed), ledger)["rocket_launch_verified"])


class RegistryAndReceiptTests(Base):
    def test_capabilities_are_registered_with_requires_and_native_recipes(self):
        self.assertEqual(CAPABILITIES["rocket_parts"]["requires"], ["rocket_silo"])
        self.assertEqual(CAPABILITIES["rocket_launch"]["requires"], ["rocket_parts"])
        for stage in rocket_stages.STAGES:
            Request("k", WORLD, 0, stage, {}).validate()
            with self.assertRaises(ValueError):
                Request("k", WORLD, 0, stage, {"x": 1}).validate()

    def test_storage_keeps_the_materials_an_active_stage_collected(self):
        from factorio_agent.executors import StarterExecutors

        ex = StarterExecutors.__new__(StarterExecutors)
        jobs = [
            {"capability": "rocket_silo", "status": "waiting", "evidence": {"needs": {"steel-plate": 1000}}},
            {"capability": "rocket_launch", "status": "succeeded", "evidence": {"needs": {"copper-plate": 600}}},
            {"capability": "produce", "status": "waiting", "evidence": {"needs": {"pipe": 50}}},
        ]
        ex._same_world_jobs = lambda obs: jobs
        self.assertEqual(ex._rocket_claims({}), {"steel-plate": 1000})

    def test_receipt_gates_on_published_producers(self):
        row = rocket_stages.receipt(self.root, observation(), self.catalog)
        self.assertFalse(row["rocket_silo"]["ready"])
        self.assertEqual({m["item"] for m in row["rocket_silo"]["missing"]} >= {"steel-plate", "processing-unit"}, True)
        out = lambda item: {"direction": "output", "item": item}  # noqa: E731
        self.board(
            {
                "smelting_district": {"ports": [out("iron-plate"), out("copper-plate")]},
                "steel": {"ports": [dict(out("steel-plate"), name=CHEST)]},
                "pu": {"ports": [dict(out("processing-unit"), name=CHEST)]},
            }
        )
        row = rocket_stages.receipt(self.root, observation(), self.catalog)
        self.assertTrue(row["rocket_silo"]["ready"], row["rocket_silo"])
        self.assertTrue(row["rocket_launch"]["ready"], row["rocket_launch"])
        self.assertFalse(row["rocket_parts"]["ready"])  # low density structure and rocket fuel

    def test_report_and_fleet_show_the_launch_as_the_final_milestone(self):
        tools = Path(__file__).resolve().parents[1] / "tools"
        spec = importlib.util.spec_from_file_location("report_run_tool", tools / "report_run.py")
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
        milestones = [{"minute": 300.0, "capability": "rocket_launch", "reason": "x"}]
        jobs = {
            "k": {
                "capability": "rocket_launch",
                "status": "succeeded",
                "evidence_status": "verified_postcondition",
                "evidence": {"rockets_launched": 1},
            }
        }
        self.assertEqual(report.rocket_launch(milestones, jobs)["verified"], True)
        self.assertIsNone(report.rocket_launch([], {}))
        spec = importlib.util.spec_from_file_location("fleet_tool_rocket", tools / "fleet.py")
        fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        reached, furthest = fleet.milestones(
            {"k": {"capability": "rocket_launch", "parameters": {}}},
            {"k": (3600 * 300, "succeeded", "")},
            (),
            lambda j, s: False,
        )
        self.assertEqual(reached["rocket_launch"], 300.0)
        self.assertEqual(furthest[1], "rocket_launch")
        self.assertIn(("launch", "rocket_launch"), fleet.COLUMNS)


if __name__ == "__main__":
    unittest.main()
