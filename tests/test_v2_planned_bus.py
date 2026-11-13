"""Main bus trunk built from the site plan (planned_bus.py), checked offline."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import planned_bus as pb
from factorio_agent import production_sources
from factorio_agent import progression as pg
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.site.busframe import Bus, lane_table
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.masterplan import plan_site
from factorio_agent.smelting_site import plan_candidates
from factorio_agent.transport import _production_keepouts, xy

COUNTS = {"iron-ore": 4, "copper-ore": 2}
# Seeds whose plans cover all four bus directions, mirrored and plain districts.
SEEDS = {1: "W", 2: "N", 4: "E", 7: "S"}
PLANS = {seed: plan_site(make_world(seed, size=512)) for seed in SEEDS}
ORDER = ("iron-plate", "iron-plate", "copper-plate", "copper-plate", "coal", "stone")


def district_plan(master):
    """The best oriented district candidate as a registered district plan."""
    site = plan_candidates(master, COUNTS)[0]
    ports = [p for bank in site["banks"] for p in bank["ports"] if p["direction"] == "output"]
    return _plan(site["entities"], ports, reserved_area=site["area"], site_transform=site["site_transform"])


def at(position):
    return (position["x"], position["y"])


# Factorio: a NORTH splitter's left is west; turning the splitter turns its sides with it.
RIGHT_HAND = {0: (1, 0), 4: (0, 1), 8: (-1, 0), 12: (0, -1)}


def native_side(splitter, point):
    """Independent oracle: the native splitter side ``point`` lies on."""
    rx, ry = RIGHT_HAND[splitter["direction"]]
    p = splitter["position"]
    lateral = (point["x"] - p["x"]) * rx + (point["y"] - p["y"]) * ry
    return "right" if lateral > 0 else "left"


class GeometryTests(unittest.TestCase):
    def test_plans_cover_every_direction(self):
        self.assertEqual({p.bus.direction for p in PLANS.values()}, {"N", "E", "S", "W"})

    def test_built_lanes_are_the_first_lane_of_each_plate_in_the_plan_table(self):
        for master in PLANS.values():
            lanes = pb.built_lanes(master.bus)
            self.assertEqual([lane.material for lane in lanes], list(pb.BUILT_MATERIALS))
            for lane in lanes:
                first = next(row for row in master.bus.lanes if row.material == lane.material)
                self.assertEqual(lane, first)

    def test_trunk_runs_on_the_lane_band_in_the_flow_direction(self):
        for direction in ("N", "E", "S", "W"):
            bus = Bus((10, -20), direction, 28, 100, lane_table(28, ORDER, 4, 2))
            for lane in pb.built_lanes(bus):
                segments = pb.trunk_segments(bus, lane, 12)
                self.assertEqual({s["direction"] for s in segments}, {pb.FACING[direction]})
                for along, segment in enumerate(segments):
                    x, y = segment["position"]["x"], segment["position"]["y"]
                    self.assertEqual(bus.local_tile(int(x - 0.5), int(y - 0.5)), (along, lane.offset))
                band = pb.lane_band(bus, lane.material)
                self.assertTrue(all(pb._inside(band, s["position"]) for s in segments))
                tail = segments[-1]["position"]
                self.assertTrue(bus.rect().contains(int(tail["x"] - 0.5), int(tail["y"] - 0.5)))

    def test_trunk_crosses_a_foreign_belt_underground(self):
        bus = Bus((0, 0), "E", 28, 100, lane_table(28, ORDER, 4, 2))
        lane = pb.built_lanes(bus)[0]
        cross = pb.lane_tile(bus, lane, 5)
        foreign = [{"name": "transport-belt", "position": cross, "direction": 0}]
        trunk = pb.trunk_plan(bus, lane, 10, foreign, 4)
        self.assertTrue(trunk["ok"])
        self.assertEqual(len(trunk["underground_pairs"]), 1)
        self.assertNotIn(at(cross), {xy(e) for e in trunk["entities"]})

    def test_feed_branch_for_turned_and_mirrored_districts(self):
        for seed, master in PLANS.items():
            plan = district_plan(master)
            owned = {xy(e): e for e in plan["entities"]}
            occupied = FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan
            used = occupied(plan["entities"])
            for port in plan["ports"]:
                feed = pb.feed_branch(plan, port)
                anchor = owned.get(at(feed["anchor"]))
                self.assertIsNotNone(anchor, seed)
                self.assertEqual(anchor["name"], "transport-belt")
                self.assertEqual(anchor["direction"], feed["splitter"]["direction"])
                self.assertEqual(feed["source"]["facing"], port["facing"])
                self.assertFalse(pb._inside(plan["reserved_area"], feed["source"]["position"]))
                new = occupied([feed["splitter"], *feed["belts"]])
                self.assertEqual(new & used, occupied([anchor]), seed)
                # Green science splits one tile upstream of the port on the other side.
                upstream = owned.get((port["position"]["x"], port["position"]["y"]))
                self.assertIsNotNone(upstream)

    def test_feed_splitter_output_priority_is_the_bus_branch_for_turned_and_mirrored_districts(self):
        mirrored = set()
        for seed, master in PLANS.items():
            plan = district_plan(master)
            if plan["site_transform"].get("mirror"):
                mirrored.add(seed)
            for port in plan["ports"]:
                feed = pb.feed_branch(plan, port)
                splitter = feed["splitter"]
                # The first branch belt sits one tile downstream of the splitter's branch half.
                first = feed["belts"][0]["position"]
                expected = native_side(splitter, first)
                self.assertEqual(splitter["splitter_output_priority"], expected, seed)
                # Canonical frame: facing east, branch south = right; a mirror swaps the sides.
                self.assertEqual(expected, "left" if plan["site_transform"].get("mirror") else "right", seed)
                self.assertEqual(splitter["splitter_input_priority"], "none")
                self.assertEqual(pb.feed_priorities(feed)["splitter_output_priority"], expected)
                # The district port (red science's side) is the other output.
                self.assertNotEqual(native_side(splitter, feed["district_port"]), expected, seed)
        self.assertTrue(mirrored)

    def test_splice_replaces_only_the_anchor_and_is_idempotent(self):
        plan = district_plan(PLANS[4])
        feeds = [pb.feed_branch(plan, port) for port in plan["ports"]]
        once = pb.spliced_district_plan(plan, feeds)
        self.assertEqual(len(once["entities"]), len(plan["entities"]))
        self.assertEqual(pb.spliced_district_plan(once, feeds), once)
        names = {xy(e): e["name"] for e in once["entities"]}
        for feed in feeds:
            self.assertEqual(names[xy(feed["splitter"])], "splitter")
        self.assertEqual(once["ports"], plan["ports"])
        self.assertEqual(once["site_transform"], plan["site_transform"])


class Harness:
    """A prepared controller on a fake world: district registered, anchors observed."""

    def __init__(self, root, master):
        self.root = Path(root)
        self.master = master
        self.plan = district_plan(master)
        unit = iter(range(1000, 100000))
        self.entities = [dict(e, unit_number=next(unit)) for e in self.plan["entities"]]
        board = {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "blocks": {"smelting_district": self.plan},
            "connections": [],
        }
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        smelting_state = {"world_id": "w", "plan": self.plan}
        (self.root / "smelting-district.json").write_text(json.dumps(smelting_state), encoding="utf-8")
        self.smelting = SimpleNamespace(state=smelting_state, path=self.root / "smelting-district.json")
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=str(self.root)), query=Mock(return_value={"ok": True, "max_span": 4})
        )
        self.builder = Mock()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.builder.bootstrap.ensure_item.return_value = {"type": "craft", "item": "splitter", "count": 2}
        self.check = Mock(return_value={"ok": True, "clear_obstacles": []})
        self.route = Mock(side_effect=self._route)
        self.site = SimpleNamespace(plan=lambda world_id: master)
        self.bus = pb.PlannedBus(
            self.game, self.builder, SimpleNamespace(fingerprint="c"), self.smelting, self.site,
            route=self.route, check=self.check,
        )  # fmt: skip

    @staticmethod
    def _route(game, builder, obs, source, destination, reserved, **kwargs):
        ends = [
            dict(name="transport-belt", position=p["position"], direction=p["facing"]) for p in (source, destination)
        ]
        return {"ok": True, "entities": ends, "underground_pairs": [], "route_metrics": {"length": 2}}

    def obs(self, extra=(), inventory=None, drop=()):
        entities = [e for e in self.entities if xy(e) not in drop] + list(extra)
        return {"world_id": "w", "tick": 100, "entities": entities, "inventory": inventory or {}}


class ControllerTests(unittest.TestCase):
    def test_prepare_plans_trunks_feeds_and_a_staged_plan_without_splitters(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[1])
            prepared = h.bus.prepare(h.obs())
            self.assertTrue(prepared["ok"], prepared)
            self.assertEqual([t["item"] for t in prepared["trunks"]], list(pb.BUILT_MATERIALS))
            self.assertEqual({t["length"] for t in prepared["trunks"]}, {pb.TRUNK_TILES})
            names = [e["name"] for e in prepared["plan"]["entities"]]
            self.assertEqual(names.count("splitter"), 2)
            self.assertNotIn("splitter", [e["name"] for e in prepared["staged_plan"]["entities"]])
            guide = h.route.call_args.kwargs["guide"]
            self.assertEqual(guide, pb.corridor_box(h.master.corridor(pb.HEADER_CORRIDOR)))
            ignored = h.check.call_args.kwargs["ignored_units"]
            self.assertEqual(sorted(ignored), sorted(f["anchor_unit"] for f in prepared["feeds"]))
            heads = {at(p["position"]) for p in prepared["plan"]["ports"] if p["role"] == "head"}
            bus = h.master.bus
            self.assertEqual(heads, {at(pb.lane_tile(bus, lane, 0)) for lane in pb.built_lanes(bus)})

    def test_prepare_requires_a_site_plan_and_the_observed_anchor(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[2])
            h.bus.site = None
            self.assertEqual(h.bus.prepare(h.obs())["reason"], "planned_bus_requires_site_plan")
            h.bus.site = h.site
            anchor = pb.feed_branch(h.plan, h.plan["ports"][0])["anchor"]
            self.assertEqual(h.bus.prepare(h.obs(drop={at(anchor)}))["reason"], "planned_bus_feed_anchor_missing")

    def test_second_route_order_is_tried_when_the_first_fails(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[4])
            calls = []

            def route(game, builder, obs, source, destination, reserved, **kwargs):
                calls.append(source["item"])
                if calls == ["iron-plate", "copper-plate"]:
                    return {"ok": False, "reason": "no_clean_trunk_corridor"}
                return Harness._route(game, builder, obs, source, destination, reserved)

            h.bus.route = route
            self.assertTrue(h.bus.prepare(h.obs())["ok"])
            self.assertEqual(calls[2:], ["copper-plate", "iron-plate"])

    def test_step_builds_splices_cuts_over_and_proves(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[7])
            h.builder.ensure_plan.return_value = {"type": "build", "name": "transport-belt"}
            self.assertEqual(h.bus.step(h.obs())["type"], "build")
            state = json.loads((h.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "build")
            self.assertEqual(h.builder.ensure_plan.call_args.args[1], state["staged_plan"])
            board = json.loads((h.root / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertEqual(board["shared_main_bus"]["template"], pb.TEMPLATE)
            self.assertEqual({c["owner"] for c in board["connections"]}, {pb.OWNER})
            # Staged plan built: the district plan gets the splitters, splitter stock comes first.
            h.builder.ensure_plan.return_value = {"status": "succeeded"}
            self.assertEqual(h.bus.step(h.obs())["type"], "craft")
            spliced = json.loads((h.root / "smelting-district.json").read_text(encoding="utf-8"))["plan"]
            before = sum(e["name"] == "splitter" for e in h.plan["entities"])
            self.assertEqual(sum(e["name"] == "splitter" for e in spliced["entities"]), before + 2)
            board = json.loads((h.root / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertEqual(board["blocks"]["smelting_district"], spliced)
            action = h.bus.step(h.obs(inventory={"splitter": 2}))
            self.assertEqual(action["type"], "mine")
            self.assertEqual(action["expected_entity_unit"], h.bus.state["feeds"][0]["anchor_unit"])
            # Anchors mined and splitters built: commissioning waits for plates at the tails.
            anchors = {at(f["anchor"]) for f in h.bus.state["feeds"]}
            built = self.built(h, plates=False)
            result = h.bus.step(h.obs(extra=built, drop=anchors))
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(h.bus.state["phase"], "commission")
            result = h.bus.step(h.obs(extra=self.built(h, plates=True), drop=anchors))
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual([row["item"] for row in result["evidence"]["lanes"]], list(pb.BUILT_MATERIALS))
            self.assertEqual(h.bus.state["phase"], "verified")

    def test_proof_rejects_a_mixed_lane_and_a_missing_belt(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[1])
            h.bus.state = h.bus.prepare(h.obs())
            h.bus.state["phase"] = "commission"
            built = self.built(h, plates=True)
            self.assertIsNotNone(h.bus.proof(h.obs(extra=built)))
            iron = next(t for t in h.bus.state["trunks"] if t["item"] == "iron-plate")
            mixed = [
                dict(e, belt_inventory={"copper-plate": 1}) if at(e["position"]) == at(iron["head"]) else e
                for e in built
            ]
            self.assertIsNone(h.bus.proof(h.obs(extra=mixed)))
            self.assertIsNone(h.bus.proof(h.obs(extra=built[1:])))
            self.assertIsNone(h.bus.proof(dict(h.obs(extra=built), world_id="other")))

    def test_commission_configures_each_feed_splitter_once_and_proof_requires_the_priorities(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[2])
            h.bus.state = h.bus.prepare(h.obs())
            h.bus.state["phase"] = "commission"
            h.bus._save()
            plain = [
                dict(e, splitter_input_priority="none", splitter_output_priority="none")
                if e["name"] == "splitter"
                else e
                for e in self.built(h, plates=True)
            ]
            self.assertIsNone(h.bus.proof(h.obs(extra=plain)))
            action = h.bus.step(h.obs(extra=plain))
            feed = h.bus.state["feeds"][0]
            live = next(
                e for e in plain if e["name"] == "splitter" and at(e["position"]) == at(feed["splitter"]["position"])
            )
            self.assertEqual(action["type"], "configure_splitter")
            self.assertEqual(action["expected_splitter_unit"], live["unit_number"])
            self.assertEqual(action["expected_splitter_world_id"], "w")
            self.assertEqual(
                (action["input_priority"], action["output_priority"]),
                ("none", native_side(feed["splitter"], feed["source"]["position"])),
            )
            # Configured: no further action, the proof passes.
            done = [dict(e, **pb.feed_priorities(feed)) if e is live else e for e in plain]
            other = h.bus.step(h.obs(extra=done))
            self.assertEqual(other["type"], "configure_splitter")
            self.assertNotEqual(at(other["position"]), at(feed["splitter"]["position"]))
            matched = self.built(h, plates=True)
            result = h.bus.step(h.obs(extra=matched))
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(len(result["evidence"]["splitter_priorities"]), 2)

    def test_an_older_state_without_priorities_is_annotated_and_configured(self):
        with TemporaryDirectory() as root:
            h = Harness(root, PLANS[7])
            state = h.bus.prepare(h.obs())
            for e in [f["splitter"] for f in state["feeds"]] + state["plan"]["entities"]:
                for key in pb.PRIORITY_KEYS:
                    e.pop(key, None)
            state["phase"] = "verified"
            h.bus.state = state
            h.bus._save()
            unset = self.built(h, plates=True)  # observed splitters carry no priorities
            action = h.bus.step(h.obs(extra=unset))
            self.assertEqual(action["type"], "configure_splitter")
            saved = json.loads((h.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            for row in [f["splitter"] for f in saved["feeds"]] + [
                e for e in saved["plan"]["entities"] if e["name"] == "splitter"
            ]:
                self.assertIn(row["splitter_output_priority"], ("left", "right"))
            self.assertEqual(h.bus.state, saved)

    @staticmethod
    def built(h, *, plates):
        """Observed entities for the whole bus plan; trunk belts carry their lane's plate."""
        items = {}
        bus = Bus.from_dict(h.bus.state["bus"])
        lanes = {lane.index: lane for lane in bus.lanes}
        for trunk in h.bus.state["trunks"]:
            for row in pb.trunk_segments(bus, lanes[trunk["lane"]], trunk["length"]):
                items[at(row["position"])] = trunk["item"]
        rows = []
        for e in h.bus.state["plan"]["entities"]:
            row = dict(e, unit_number=len(rows) + 1)
            if e["name"] == "transport-belt":
                item = items.get(xy(e))
                row["belt_inventory"] = {item: 2} if plates and item else {}
            rows.append(row)
        return rows


class PublicationTests(unittest.TestCase):
    def published(self, root, master):
        h = Harness(root, master)
        h.bus.state = h.bus.prepare(h.obs())
        return h, pb.publication(h.bus.state)

    def test_lanes_and_reservations_follow_the_plan_lane_table(self):
        with TemporaryDirectory() as root:
            h, bus = self.published(root, PLANS[2])
            table = h.master.bus.lanes
            self.assertEqual([row["index"] for row in bus["lanes"]], [lane.index for lane in table])
            built = [row for row in bus["lanes"] if row["status"] == "built"]
            self.assertEqual([row["material"] for row in built], list(pb.BUILT_MATERIALS))
            reserved = {row["material"] for row in bus["lanes"] if row["status"] == "reserved"}
            self.assertIn("coal", reserved)
            self.assertEqual(
                {r["item"] for r in bus["reservations"]}, {lane.material for lane in table if lane.material}
            )
            self.assertTrue(all(r["area"] for r in bus["reservations"]))
            self.assertEqual(bus["flow_direction"], pb.FACING[h.master.bus.direction])

    def test_production_sources_and_transport_accept_the_published_bus(self):
        with TemporaryDirectory() as root:
            h, bus = self.published(root, PLANS[4])
            board = {"world_id": "w", "blocks": {}, "connections": [], "shared_main_bus": bus}
            self.assertIsNone(production_sources.bus_tap_port(board, "iron-gear-wheel", "iron-plate"))
            self.assertIsNone(production_sources.tap_parameters(board, "iron-gear-wheel", "iron-plate", 10))
            found = production_sources.resolve(board, "iron-gear-wheel", "iron-plate", owner="produce-x")
            self.assertEqual((found["ok"], found["kind"], found["source"]), (True, "planned_bus_tap", None))
            # Coal has only a reserved lane: no trunk, so no tap.
            self.assertFalse(production_sources.resolve(board, "x", "coal", owner="produce-x")["ok"])
            (h.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            builder = FactoryBuilder.__new__(FactoryBuilder)
            band = next(r["area"] for r in bus["reservations"] if r["item"] == "coal")
            (x0, y0), (x1, y1) = band
            a = {"x": x0 - 20.5, "y": y0 - 20.5}
            b = {"x": x1 + 20.5, "y": y1 + 20.5}
            bounds = {"min_x": a["x"], "max_x": b["x"], "min_y": a["y"], "max_y": b["y"]}
            keepouts = _production_keepouts(
                h.game, builder, {"world_id": "w", "entities": []}, {"position": a}, {"position": b}, [], bounds
            )
            self.assertTrue(keepouts["ok"], keepouts)
            self.assertIn((x0 + 0.5, y0 + 0.5), keepouts["hard"])


class RoadmapTests(unittest.TestCase):
    def test_planned_bus_replaces_the_development_map_migration(self):
        steps = {s.id: s for s in pg.ROADMAP}
        self.assertNotIn("main_bus_factory", {s.capability for s in pg.ROADMAP})
        self.assertEqual(steps["main_bus"].capability, "planned_main_bus")
        ids = [s.id for s in pg.ROADMAP]
        # The bus comes straight after the district; red science waits for it.
        self.assertEqual(ids.index("main_bus"), ids.index("smelting_district") + 1)
        self.assertEqual(steps["automation_science"].after, ("main_bus",))
        self.assertEqual(steps["dedicated_materials_coal"].after, ("main_bus",))
        self.assertEqual(CAPABILITIES["planned_main_bus"]["requires"], ["smelting_district"])
        self.assertIn("planned_main_bus", CAPABILITIES["steel_supply"]["requires"])

    def done_through(self, last):
        jobs = []
        for step in pg.ROADMAP:
            jobs.append({"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"})
            if step.id == last:
                return jobs
        raise AssertionError(last)

    def test_bus_is_proposed_once_the_district_succeeded_and_red_science_after_it(self):
        enabled = {
            "enabled_recipes": {name: True for name in ("assembling-machine-1", "automation-science-pack", "lab")}
        }
        nodes = pg.autopilot_nodes(self.done_through("smelting_district"), [], enabled, CAPABILITIES, limit=10)
        names = [n["id"] for n in nodes]
        self.assertIn("auto_main_bus", names)
        self.assertNotIn("auto_automation_science", names)
        nodes = pg.autopilot_nodes(self.done_through("main_bus"), [], enabled, CAPABILITIES, limit=10)
        self.assertIn("auto_automation_science", [n["id"] for n in nodes])


class TrunkBeltReplacedBySplitterTests(unittest.TestCase):
    def test_a_same_direction_splitter_over_the_tile_counts_as_the_trunk_belt(self):
        # Seed 2644599892: a consumer cut a tap in and then blocked; the bus
        # proof kept failing on the one trunk belt its splitter had replaced.
        from factorio_agent.planned_bus import _splitter_over

        belt = {"name": "transport-belt", "position": {"x": -50.5, "y": 80.5}, "direction": 8}
        splitter = {"name": "splitter", "position": {"x": -50.0, "y": 80.5}, "direction": 8}
        live = {("splitter", -50.0, 80.5): splitter}
        self.assertIs(_splitter_over(live, belt), splitter)
        self.assertIsNone(_splitter_over(live, dict(belt, direction=4)))
        self.assertIsNone(_splitter_over({}, belt))


if __name__ == "__main__":
    unittest.main()
