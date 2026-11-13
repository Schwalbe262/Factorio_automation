"""Remote smelting (remote_smelting.py) and the pipe bridge (pipe_bridge.py), offline.

Regression data from the GUI map eval-1575138435 (fixture written from its board, site plan,
jobs and a read-only observe at about 370 game minutes): iron could not grow at home (the
patch had no bank site, a new module's ore had no corridor to the district), the next iron
patch lay east of the bus, and the crude pipeline ran along the east edge of the bus on the
column of every east port tile.
"""

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import pipe_bridge, plan_needs, planned_tap
from factorio_agent import remote_smelting as rs
from factorio_agent.core.deterministic_builder import FactoryBuilder

FIXTURE = Path(__file__).parent / "fixtures" / "remote_smelting_eval_1575138435.json.gz"
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def fixture():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def controller(root, data):
    board = {"world_id": data["world_id"], "blocks": {}, "connections": [], "shared_main_bus": data["shared_main_bus"]}
    (Path(root) / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
    ctl = rs.RemoteSmelting.__new__(rs.RemoteSmelting)
    ctl.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(root)), query=Mock())
    ctl.builder = GEOMETRY
    ctl.kind = rs.Kind("remote-iron-plate", rs.owner("iron-ore"), "iron-ore", "iron-plate", None, "coal", "x.json")
    ctl.root = Path(root)
    return ctl


class PatchRuleTests(unittest.TestCase):
    def test_the_nearest_patch_with_lasting_ore_is_chosen_and_every_patch_has_a_verdict(self):
        row = fixture()["iron_patches"]
        patches = [{k: p[k] for k in ("centroid", "bbox", "distance", "amount", "tile_count")} for p in row["others"]]
        accepted, rows = rs.choose_patches(patches, 180.0)
        self.assertEqual([round(c) for c in accepted[0]["centroid"]], [135, -109])  # 173 tiles, ~19 h of ore
        self.assertEqual([r["verdict"] for r in rows], ["accepted"] * 3)
        self.assertEqual(rows[0]["life_minutes"], round(172731 / 180.0))
        short, rows = rs.choose_patches(patches, 180.0, min_life=2000)
        self.assertEqual(rows[0]["verdict"], "ore_too_short_lived")
        self.assertEqual([round(c) for c in short[0]["centroid"]], [-282, -5])
        _, rows = rs.choose_patches(patches, 180.0, max_distance=200)
        self.assertEqual([r["verdict"] for r in rows], ["accepted", "beyond_reach", "beyond_reach"])

    def test_wait_vs_spend_receipt_names_the_unbounded_wait(self):
        receipt = rs.wait_vs_spend(488, 331, distance=173.6)
        self.assertIsNone(receipt["wait_seconds"])
        self.assertGreater(receipt["spend_seconds"], 488)


class TriggerTests(unittest.TestCase):
    def test_a_blocked_home_mining_job_proposes_one_remote_block(self):
        jobs = fixture()["blocked_mining_jobs"]
        self.assertTrue(any(j["reason"] == "no_clean_extra_bank_connection_to_owned_ore_trunk" for j in jobs))
        caps = {"remote_smelting": {}}
        nodes = rs.remote_smelting_nodes(jobs, [], {}, caps)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["capability"], "remote_smelting")
        self.assertEqual(node["parameters"], {"resource": "iron-ore", "rate_per_minute": 210})
        rs.validate_parameters(node["parameters"])
        # Never twice: a queued node, or an unfinished job of the ore, stops it.
        self.assertEqual(rs.remote_smelting_nodes(jobs, [dict(node, status="pending")], {}, caps), [])
        open_job = {"capability": "remote_smelting", "parameters": node["parameters"], "status": "blocked"}
        self.assertEqual(rs.remote_smelting_nodes(jobs + [open_job], [], {}, caps), [])
        self.assertEqual(rs.remote_smelting_nodes(jobs, [], {}, {}), [])
        gated = {"remote_smelting": {"requires": ["planned_main_bus"]}}
        self.assertEqual(rs.remote_smelting_nodes(jobs, [], {}, gated), [])  # no bus yet: nowhere to feed
        bus = {"capability": "planned_main_bus", "status": "succeeded"}
        self.assertEqual(len(rs.remote_smelting_nodes(jobs + [bus], [], {}, gated)), 1)
        coal = [dict(j, parameters={"resource": "coal", "rate_per_minute": 200}) for j in jobs]
        self.assertEqual(rs.remote_smelting_nodes(coal, [], {}, caps), [])

    def test_parameters(self):
        for bad in ({"resource": "coal", "rate_per_minute": 100}, {"resource": "iron-ore"}, {"x": 1}):
            with self.assertRaises(ValueError):
                rs.validate_parameters(bad)


class FeedTests(unittest.TestCase):
    def test_east_feeds_meet_the_crude_pipe_and_a_bridged_port_beyond_it_fits(self):
        data = fixture()
        obs = {"world_id": data["world_id"], "entities": data["entities"]}
        with tempfile.TemporaryDirectory() as root:
            ctl = controller(root, data)
            blocked = {}
            rows = ctl._feeds(obs, data["shared_main_bus"], 5, blocked, sides=("right",))
        self.assertTrue(rows)
        self.assertEqual(blocked["feed_meets_standing_hardware"]["blockers"][0]["entity"], "pipe")
        feed = rows[0]
        self.assertEqual(feed["along"], 44)  # nearest the head: upstream of green science, steel, engines
        self.assertEqual(feed["port"]["position"], {"x": 112.5, "y": -93.5})  # one tile beyond the pipe
        bridge = feed["pipe_bridge"]
        self.assertEqual(
            [e["position"] for e in bridge["remove"]], [{"x": 111.5, "y": y} for y in (-92.5, -93.5, -94.5)]
        )
        ptg = {e["position"]["y"]: e["direction"] for e in bridge["add"]}
        self.assertEqual(ptg, {-92.5: 8, -94.5: 0})  # surface sides face away from the crossing
        # The feed's own belt stands on the bridged tile and nowhere else on the pipe column.
        on_pipe = [e for e in feed["entities"] if e["position"]["x"] == 111.5]
        self.assertEqual([e["position"]["y"] for e in on_pipe], [-93.5])
        self.assertEqual(set(rs._feed_units(feed)) >= {e["unit_number"] for e in bridge["remove"]}, True)

    def test_failure_needs_name_the_feed_blocker(self):
        failures = [
            {
                "reason": "no_bus_feed_row_for_remote_plates",
                "item": "iron-plate",
                "side": "right",
                "feeds": {
                    "feed_meets_standing_hardware": {
                        "count": 10,
                        "blockers": [{"entity": "pipe", "position": {"x": 111.5, "y": -93.5}}],
                    }
                },
            }
        ]
        needs = rs.failure_needs(failures, "iron-ore", "iron-plate")
        self.assertEqual((needs["kind"], needs["reason"]), ("preflight", "feed_meets_standing_hardware"))
        self.assertEqual(needs["blockers"][0]["entity"], "pipe")
        self.assertEqual(
            plan_needs.from_result("remote_smelting", {"ok": False, "plan_needs": needs})["kind"], "preflight"
        )
        route = rs.failure_needs([{"reason": "no_clean_trunk_corridor", "item": "coal"}], "iron-ore", "iron-plate")
        self.assertEqual((route["kind"], route["item"]), ("route", "coal"))
        self.assertEqual(rs.failure_needs([{"reason": "x"}], "iron-ore", "iron-plate")["kind"], "site")


def pipe_run(x=10.5, ys=range(-5, 6), unit=100, side=None):
    entities = [{"name": "pipe", "position": {"x": x, "y": y + 0.5}, "unit_number": unit + i} for i, y in enumerate(ys)]
    if side:
        entities.append({"name": "pipe", "position": {"x": x + 1, "y": side}, "unit_number": 999})
    return entities


class PipeBridgeTests(unittest.TestCase):
    def test_plan_replaces_three_pipes_by_a_pair_facing_away(self):
        obs = {"entities": pipe_run()}
        bridge = pipe_bridge.plan(obs, (10.5, 0.5), (0, 1))
        self.assertTrue(bridge["ok"], bridge)
        self.assertEqual([e["position"]["y"] for e in bridge["remove"]], [-0.5, 0.5, 1.5])
        self.assertEqual([(e["position"]["y"], e["direction"]) for e in bridge["add"]], [(-0.5, 0), (1.5, 8)])
        side = pipe_bridge.plan({"entities": pipe_run(side=1.5)}, (10.5, 0.5), (0, 1))
        self.assertEqual(side["reason"], "pipe_bridge_meets_a_side_connection")
        end = pipe_bridge.plan({"entities": pipe_run(ys=range(-1, 3))}, (10.5, 0.5), (0, 1))
        self.assertEqual(end["reason"], "pipe_bridge_run_does_not_continue")

    def test_release_rewrites_the_owner_plan_graph_and_board_then_binds_the_pair(self):
        pipes = pipe_run()
        bridge = pipe_bridge.plan({"entities": pipes}, (10.5, 0.5), (0, 1))
        specs = [{k: e[k] for k in ("name", "position")} | {"direction": 0} for e in pipes]
        edges = [
            {"a": dict(specs[i], fluidbox_index=1), "b": dict(specs[i + 1], fluidbox_index=1)}
            for i in range(len(specs) - 1)
        ]
        state = {
            "world_id": "w",
            "plan": {"entities": list(specs), "layout_policy": "guarded-basic-petroleum-v1"},
            "route": {"entities": list(specs)},
            "fluid_nodes": list(specs),
            "fluid_edges": edges,
            "bindings": {pipe_bridge._key(e): e["unit_number"] for e in pipes},
        }
        board = {
            "world_id": "w",
            "blocks": {"basic_petroleum": {"entities": list(specs), "layout_policy": "guarded-basic-petroleum-v1"}},
        }
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "basic-petroleum.json").write_text(json.dumps(state), encoding="utf-8")
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            self.assertIsNone(pipe_bridge.release(root, "w", bridge))
            self.assertIsNone(pipe_bridge.release(root, "w", bridge))  # idempotent
            saved = json.loads((root / "basic-petroleum.json").read_text(encoding="utf-8"))
            names = sorted(e["name"] for e in saved["plan"]["entities"])
            self.assertEqual(names.count("pipe-to-ground"), 2)
            self.assertEqual(names.count("pipe"), len(pipes) - 3)
            touching = [row for row in saved["fluid_edges"] if "pipe-to-ground" in (row["a"]["name"], row["b"]["name"])]
            self.assertEqual(len(touching), 3)  # before-pair, the underground pair, pair-after
            self.assertEqual(len(saved["fluid_edges"]), len(edges) - 4 + 3)
            self.assertNotIn(pipe_bridge._key(bridge["remove"][1]), saved["bindings"])
            board_now = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertEqual(board_now["blocks"]["basic_petroleum"]["entities"], saved["plan"]["entities"])
            # An owner whose state shape is unknown is a blocker, never a silent change.
            other = dict(board_now, blocks={"someone": {"entities": list(specs)}})
            (root / "pcb-board.json").write_text(json.dumps(other), encoding="utf-8")
            self.assertEqual(pipe_bridge.release(root, "w", bridge)["reason"], "pipe_bridge_owner_state_unknown")
            (root / "pcb-board.json").write_text(json.dumps(board_now), encoding="utf-8")
            live = [e for e in pipes if e["position"]["y"] not in (-0.5, 0.5, 1.5)]
            live += [dict(e, unit_number=500 + i) for i, e in enumerate(bridge["add"])]
            pipe_bridge.bind(root, "w", bridge, {"entities": live})
            saved = json.loads((root / "basic-petroleum.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["bindings"][pipe_bridge._key(bridge["add"][0])], 500)

    def test_step_stocks_mines_by_unit_then_builds_the_pair(self):
        pipes = pipe_run()
        bridge = pipe_bridge.plan({"entities": pipes}, (10.5, 0.5), (0, 1))
        builder = Mock()
        builder.bootstrap.ensure_item.return_value = {"type": "craft", "item": "pipe-to-ground", "count": 2}
        obs = {"world_id": "w", "entities": pipes, "inventory": {}}
        self.assertEqual(pipe_bridge.step(builder, obs, bridge)["type"], "craft")
        obs["inventory"] = {"pipe-to-ground": 2}
        action = pipe_bridge.step(builder, obs, bridge)
        self.assertEqual((action["type"], action["expected_entity_unit"]), ("mine", bridge["remove"][0]["unit_number"]))
        # The first pipe is mined; the crossing pipe was replaced by someone else: never mined blind.
        changed = [
            dict(e, unit_number=7) if e["position"]["y"] == 0.5 else e for e in pipes if e["position"]["y"] != -0.5
        ]
        self.assertEqual(
            pipe_bridge.step(builder, dict(obs, entities=changed), bridge)["reason"],
            "pipe_bridge_pipe_identity_changed",
        )
        gone = [e for e in pipes if e["position"]["y"] not in (-0.5, 0.5, 1.5)]
        builder.ensure_plan.return_value = {"type": "build", "name": "pipe-to-ground"}
        self.assertEqual(pipe_bridge.step(builder, dict(obs, entities=gone), bridge)["type"], "build")
        built = gone + [dict(e, unit_number=1) for e in bridge["add"]]
        self.assertIsNone(pipe_bridge.step(builder, dict(obs, entities=built), bridge))


class StepTests(unittest.TestCase):
    def test_publish_first_then_bank_block_and_the_feed_last(self):
        from test_v2_planned_tap import bus_state

        from factorio_agent import planned_bus as pb
        from factorio_agent.core.deterministic_builder import _plan

        state = bus_state("N")
        units = iter(range(500, 100000))
        live = [dict(e, unit_number=next(units)) for e in state["plan"]["entities"]]
        view = planned_tap.BusView(pb.publication(state))
        feed = planned_tap.feed_layout(view, "iron-plate", "right", 20, span=5)
        tiles = {(e["position"]["x"], e["position"]["y"]): e for e in live}
        feed.update(
            name="remote_smelting:iron-ore:iron-plate",
            owner=rs.owner("iron-ore"),
            demand_per_minute=0,
            supply_per_minute=150.0,
            built=False,
            remove_units=[tiles[(p["x"], p["y"])]["unit_number"] for p in feed["remove"]],
        )
        bank_plan = _plan([{"name": "electric-mining-drill", "position": {"x": 300.5, "y": 0.5}, "direction": 0}], [])
        block_plan = _plan([{"name": "stone-furnace", "position": {"x": 320.0, "y": 0.0}, "direction": 0}], [])
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / pb.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
            board = {"world_id": "w", "catalog_fingerprint": "f", "blocks": {}, "connections": []}
            board["shared_main_bus"] = pb.publication(state)
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            ctl = rs.RemoteSmelting.__new__(rs.RemoteSmelting)
            ctl.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(root)), query=Mock())
            ctl.builder = Mock()
            ctl.catalog = SimpleNamespace(fingerprint="f")
            ctl.kind = rs.Kind(
                "remote-iron-plate", rs.owner("iron-ore"), "iron-ore", "iron-plate", None, "coal", "s.json"
            )
            ctl.root, ctl.resource, ctl.path = root, "iron-ore", root / "s.json"
            ctl.state = {
                "world_id": "w",
                "catalog_fingerprint": "f",
                "plan": block_plan,
                "mining_bank": {"plan": bank_plan, "reserved_area": [[299, -1], [302, 2]]},
                "taps": [feed],
                "coal": {"kind": "board_port", "source": {"position": {"x": 0.5, "y": 0.5}}, "tap": None},
                "connections": [{"item": "iron-plate", "source": {"x": 1, "y": 1}, "destination": {"x": 2, "y": 2}}],
            }
            obs = {
                "world_id": "w",
                "tick": 1,
                "entities": live,
                "inventory": {},
                "enabled_recipes": {"iron-plate": True},
            }
            ctl.builder.ensure_plan.return_value = {"type": "build", "name": "electric-mining-drill"}
            self.assertEqual(ctl.step(obs, {"resource": "iron-ore", "rate_per_minute": 150})["type"], "build")
            self.assertEqual(ctl.builder.ensure_plan.call_args[0][1], bank_plan)  # the bank before the block
            saved = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertIn(rs.owner("iron-ore"), saved["blocks"])
            self.assertIn(rs.bank_block("iron-ore"), saved["blocks"])
            stations = saved["shared_main_bus"]["stations"]
            self.assertEqual([s["kind"] for s in stations if s["name"] == feed["name"]], ["planned_feed"])
            ctl.builder.ensure_plan.return_value = {"status": "succeeded"}
            ctl.builder.bootstrap.ensure_item.return_value = {"type": "craft", "item": "splitter", "count": 1}
            action = ctl.step(obs, {"resource": "iron-ore", "rate_per_minute": 150})
            self.assertEqual(action["type"], "craft")  # the feed cut-over stocks before a trunk belt is mined
            obs["enabled_recipes"] = {}
            self.assertEqual(ctl.step(obs, {})["reason"], "native_furnace_recipe_locked")


class SupplyTests(unittest.TestCase):
    def test_a_built_feed_counts_as_plate_supply_and_the_auditor_offers_the_remote_block(self):
        from factorio_agent import growth_providers
        from factorio_agent.throughput_growth import feed_supply

        stations = [
            {"kind": "planned_tap", "item": "iron-plate", "demand_per_minute": 40, "built": True},
            {"kind": "planned_feed", "item": "iron-plate", "supply_per_minute": 187.5, "built": True},
            {"kind": "planned_feed", "item": "iron-plate", "supply_per_minute": 99, "built": False},
            {"kind": "planned_feed", "item": "copper-plate", "supply_per_minute": 50, "built": True},
        ]
        self.assertEqual(feed_supply(stations, "iron-plate"), 187.5)
        jobs = fixture()["blocked_mining_jobs"]
        state = {"jobs": jobs, "queue_nodes": [], "capabilities": {"remote_smelting": {}}}
        bottleneck = {"item": "iron-plate", "kind": "at_capacity", "block": "smelting_district"}
        options = growth_providers.plates_options(bottleneck, {}, state)
        remote = [o for o in options if o.get("capability") == "remote_smelting"]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0]["nodes"][0]["parameters"]["resource"], "iron-ore")


class FeedStationTests(unittest.TestCase):
    def test_a_feed_publishes_as_supply_not_demand(self):
        data = fixture()
        view = planned_tap.BusView(data["shared_main_bus"])
        feed = planned_tap.feed_layout(view, "iron-plate", "right", 44, span=5, reach=1)
        self.assertTrue(feed["ok"], feed)
        row = planned_tap.station(dict(feed, name="f", owner="o", supply_per_minute=187.5))
        self.assertEqual((row["kind"], row["demand_per_minute"], row["supply_per_minute"]), ("planned_feed", 0, 187.5))


if __name__ == "__main__":
    unittest.main()
