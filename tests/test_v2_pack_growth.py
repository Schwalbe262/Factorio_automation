"""Pack block growth beside the red row (pack_growth.py), the 30/min green request, the lab feed audit.

eval-1575138435 (~185 game min): 8 labs, 4 red machines (24/min, backing up), one green machine
(5/min) of a split row with 5 reserved slots; the red receipt said "a lab misses packs while packs
back up: the lab feed, not a stage" and nothing built the reserved green slots.
"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent import bandwidth_audit as ba
from factorio_agent import lab_feed
from factorio_agent import pack_growth as pg
from factorio_agent import plan_needs
from factorio_agent import progression as prog
from factorio_agent import research_capacity as rc
from factorio_agent import science_growth as sg
from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.green_layout import FRAME, split_block
from factorio_agent.green_science import LogisticScience, green_cluster
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.postmortem import DRY_RUN
from factorio_agent.remedy import RELAXATIONS
from factorio_agent.science import lab_row, validate_growth

from test_v2_planned_products import catalog
from test_v2_science_growth import state as red_state

RED, GREEN = rc.RED_PACK, rc.GREEN_PACK
WOOD = {"wood": 50}
AMPLE = {
    "iron-plate": {"available": 3000, "produced_per_minute": 300, "net_per_minute": 200},
    "copper-plate": {"available": 3000, "produced_per_minute": 300, "net_per_minute": 200},
}
SHORT = {
    "iron-plate": {"available": 40, "produced_per_minute": 300, "net_per_minute": 1},
    "copper-plate": {"available": 40, "produced_per_minute": 150, "net_per_minute": 1},
}
# The live queue: engine, electric-energy-distribution-1, fluid-handling, oil-gathering (red + green).
QUEUE = [
    rc.ResearchTarget("engine", 90, 15.0, {RED: 1, GREEN: 1}),
    rc.ResearchTarget("electric-energy-distribution-1", 120, 30.0, {RED: 1, GREEN: 1}),
    rc.ResearchTarget("fluid-handling", 50, 15.0, {RED: 1, GREEN: 1}),
    rc.ResearchTarget("oil-gathering", 100, 30.0, {RED: 1, GREEN: 1}),
]


def key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"])


def green_state(c, *, form="split"):
    """A verified green block as ``LogisticScience.prepare`` saves it (taps of one built machine)."""
    core = split_block(c, origin=(100, 0)) if form == "split" else green_cluster(c, (100.5, 0.5))
    taps = [
        {"item": "iron-plate", "demand_per_minute": 27.5, "owner": "logistic_science", "built": True},
        {"item": "copper-plate", "demand_per_minute": 7.5, "owner": "logistic_science", "built": True},
    ]
    return {
        "world_id": "w",
        "catalog_fingerprint": c.fingerprint,
        "phase": "verified",
        "core": core,
        "plan": _plan(list(core["entities"]), core["ports"], reserved_area=core["reserved_area"]),
        "taps": taps,
        "requested_rate": 30,
        "nominal_machine_rate": 5.0,
    }


def live_observation(science, green, *, green_status="working"):
    """The receipt shape at ~185 min: red machines full, red on the lab belt, five labs without green."""
    entities = []
    for e in sg.build_plan(science)["entities"] + green["plan"]["entities"]:
        live = dict(deepcopy(e), unit_number=len(entities) + 1, electric_network_connected=True, energy=1000)
        live["inventory"] = {}
        if e.get("recipe") == RED:
            live.update(status_name="full_output", products_finished=40)
        elif e.get("recipe"):
            live.update(status_name=green_status if e["recipe"] == GREEN else "working", products_finished=40)
        entities.append(live)
    labs = [e for e in entities if e["name"] == "lab"]
    for i, lab in enumerate(labs):
        lab.update(
            status_name="working" if i < 3 else "missing_science_packs",
            inventory={RED: 5, **({GREEN: 1} if i < 3 else {})},
        )
    lab_belts = {key(e) for e in science["labs"]["entities"] if e["name"] == "transport-belt"}
    next(e for e in entities if key(e) in lab_belts)["belt_inventory"] = {RED: 14}
    return {"world_id": "w", "tick": 670000, "entities": entities, "research": "engine", "inventory": dict(WOOD)}


class LiveReceiptTests(unittest.TestCase):
    def setUp(self):
        self.c = catalog()
        self.science = red_state(count=6, labs=8, machines=4)
        self.green = green_state(self.c)
        self.obs = live_observation(self.science, self.green)

    def decide(self, evidence=AMPLE, **kwargs):
        return pg.growth_decision(
            self.c, GREEN, self.green, self.science, self.obs, QUEUE, deepcopy(evidence), **kwargs
        )

    def test_the_live_shape_grows_the_reserved_green_slots_with_a_positive_saving(self):
        row = self.decide()
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["chain"], "packs")
        self.assertGreater(row["target_machines"], 1)
        self.assertLessEqual(row["target_machines"], FRAME)
        self.assertGreater(row["decision"]["seconds_saved"], 60)
        self.assertGreaterEqual(row["wanted_machines"], 4)  # 8 labs draw ~20 green/min over the queue
        json.dumps(row, allow_nan=False)

    def test_build_ahead_fills_the_frame_for_the_longer_horizon_rate(self):
        row = self.decide(expected={"rate_per_minute": 30, "labs": 8})
        self.assertTrue(row["grow"], row["reason"])
        self.assertEqual(row["target_machines"], FRAME)

    def test_sizing_uses_the_real_slot_unit(self):
        row = self.decide()
        self.assertEqual(row["unit"]["bill"], {"assembling-machine-1": 1, "inserter": 2, "small-electric-pole": 1})
        self.assertEqual(row["unit"]["machines"], 1)
        # Without a reserved slot (a cluster) the unit is a whole second cluster: six machines.
        cluster = pg.block_view(green_state(self.c, form="cluster"), GREEN, self.c)
        bill = pg.unit_bill(self.c, cluster)
        costs = rc.expansion_costs(self.c, GREEN, unit_bill=bill)
        ratio = rc.expansion_costs(self.c, GREEN, current_assemblers=1)
        self.assertEqual(costs["machines_per_assembler"], 6)
        self.assertEqual(ratio["machines_per_assembler"], 1)  # the shared-upstream rule: "+1 assembler"
        self.assertGreater(sum(costs["assembler"].values()), 3 * sum(ratio["assembler"].values()))

    def test_the_district_spare_lifts_a_measured_net_capped_by_consumption(self):
        lifted = rc.with_spare(SHORT, {"iron-plate": 120.0, "coal": 5.0})
        self.assertEqual(lifted["iron-plate"]["net_per_minute"], 120.0)
        self.assertEqual(lifted["copper-plate"], SHORT["copper-plate"])
        self.assertNotIn("coal", lifted)  # no evidence row to lift
        self.assertEqual(SHORT["iron-plate"]["net_per_minute"], 1)  # the caller's evidence is not changed

    def test_growth_declares_its_plate_draw_before_the_build(self):
        row = self.decide()
        machines = row["target_machines"]
        self.assertEqual(row["plate_draw_per_minute"]["iron-plate"], round(27.5 * machines, 3))
        self.assertEqual(row["undeclared_plate_draw_per_minute"]["iron-plate"], round(27.5 * (machines - 1), 3))
        self.assertEqual(pg.undeclared_plate_draw({GREEN: row})["copper-plate"], round(7.5 * (machines - 1), 3))

    def test_short_plates_hold_the_build_but_declare_what_the_labs_want(self):
        row = self.decide(SHORT)
        self.assertFalse(row["grow"])
        self.assertIn("throughput_growth", row["reason"])
        wanted = row["wanted_machines"]
        self.assertGreater(wanted, 1)
        self.assertEqual(row["undeclared_plate_draw_per_minute"]["iron-plate"], round(27.5 * (wanted - 1), 3))
        # The district's unclaimed capacity (furnaces at full output) lifts the cap.
        spared = self.decide(SHORT, plate_spare={"iron-plate": 200.0, "copper-plate": 60.0})
        self.assertTrue(spared["grow"], spared["reason"])

    def test_backed_up_green_is_not_the_limit(self):
        for lab in (e for e in self.obs["entities"] if e["name"] == "lab"):
            lab.update(status_name="working", inventory={RED: 5, GREEN: 4})
        row = self.decide()
        self.assertFalse(row["grow"])
        self.assertEqual(row["undeclared_plate_draw_per_minute"]["iron-plate"], 0.0)

    def test_a_cluster_has_no_slot_and_names_the_next_unit(self):
        cluster = green_state(self.c, form="cluster")
        obs = live_observation(self.science, cluster)
        row = pg.growth_decision(self.c, GREEN, cluster, self.science, obs, QUEUE, deepcopy(AMPLE))
        self.assertFalse(row["grow"])
        step = row["next_step"]
        self.assertFalse(step["implemented"])
        names = {o["option"] for o in step["options"]}
        self.assertIn("second_unit_1", names)
        self.assertIn("rebuild_split_2", names)
        self.assertNotEqual(step["choice"], "wait", step["reason"])  # 5/min against ~20/min pays

    def test_the_red_receipt_names_green_instead_of_the_lab_feed(self):
        row = sg.growth_decision(self.c, self.science, self.obs, QUEUE, deepcopy(AMPLE))
        self.assertFalse(row["grow"])
        self.assertEqual(row["chain"], "packs:" + GREEN)
        self.assertIn(GREEN, row["reason"])
        self.assertNotIn("lab feed, not a stage", row["reason"])

    def test_the_bottleneck_hook_routes_every_pack_and_the_labs(self):
        green = self.decide()
        red = sg.growth_decision(self.c, self.science, self.obs, QUEUE, deepcopy(AMPLE))
        observation = dict(self.obs, pack_growth={GREEN: green}, science_growth=red)
        state = {"jobs": [{"capability": "automation_science", "status": "succeeded"}], "queue_nodes": []}
        state["capabilities"] = CAPABILITIES
        fields = {"provider", "kind", "capability", "parameters", "nodes", "gain_per_minute", "cost"}
        fields |= {"seconds_saved", "feasible", "reason"}
        for bottleneck in ({"item": GREEN}, {"block": "laboratories", "kind": "consumer_capacity"}):
            options = sg.growth_options(bottleneck, observation, state)
            self.assertEqual(set(options[0]), fields)
            self.assertTrue(options[0]["feasible"], options[0]["reason"])
            self.assertEqual(options[0]["parameters"], {"pack": GREEN, "machines": green["target_machines"]})
            self.assertEqual(len(options[0]["nodes"]), 1)
            self.assertGreater(options[0]["gain_per_minute"], 0)
        # Labs without research in progress are a research-queue problem, never a lab or pack surplus.
        idle = sg.growth_options({"block": "laboratories"}, dict(observation, research=None), state)
        self.assertFalse(idle[0]["feasible"])
        self.assertIn("research queue", idle[0]["reason"])

    def test_the_autopilot_proposes_the_pack_growth_node(self):
        row = self.decide()
        observation = {"pack_growth": {GREEN: row}}
        jobs = [{"capability": "automation_science", "status": "succeeded"}]
        nodes = prog.science_growth_nodes(jobs, [], observation, CAPABILITIES)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["parameters"], {"pack": GREEN, "machines": row["target_machines"]})
        validate_growth(nodes[0]["parameters"])
        stalled = jobs + [{"capability": "science_growth", "status": "blocked", "parameters": nodes[0]["parameters"]}]
        self.assertEqual(prog.science_growth_nodes(stalled, [], observation, CAPABILITIES), [])
        validate_growth({"pack": "chemical-science-pack", "machines": 3})  # ChemicalScience.grow extends in place
        with self.assertRaises(ValueError):
            validate_growth({"pack": "utility-science-pack", "machines": 2})  # no growing block yet


class GrowControllerTests(unittest.TestCase):
    def test_grow_saves_republishes_builds_and_commits_the_slots(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = green_state(c)
            (root / "logistic-science.json").write_text(json.dumps(state), encoding="utf-8")
            board = {"world_id": "w", "blocks": {"logistic_science": state["plan"]}}
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            builds = []

            def ensure_plan(obs, plan):
                builds.append(plan)
                return {"status": "succeeded"}

            builder = SimpleNamespace(ensure_plan=ensure_plan)
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            green = LogisticScience(game, builder, c, None, None, site=None)
            parameters = {"pack": GREEN, "machines": 3}
            with patch("factorio_agent.green_science.preflight", return_value={"ok": True, "clear_obstacles": []}):
                planned = green.plan_growth({"world_id": "w", "entities": []}, parameters)
                self.assertTrue(planned["ok"])
                self.assertFalse((root / "logistic-science.json").read_text(encoding="utf-8").count('"growth"'))
                grown = planned["plan"]["entities"]
                obs = {
                    "world_id": "w",
                    "tick": 5,
                    "entities": [
                        dict(e, electric_network_connected=True, energy=1, unit_number=i) for i, e in enumerate(grown)
                    ],
                }
                result = green.grow(obs, parameters)
            self.assertEqual(result["status"], "succeeded", result)
            saved = json.loads((root / "logistic-science.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["core"]["count"], 3)
            self.assertEqual(len(saved["core"]["growth_slots"]), FRAME - 3)
            self.assertEqual(sum(e.get("recipe") == GREEN for e in saved["plan"]["entities"]), 3)
            self.assertNotIn("growth", saved)
            published = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))["blocks"]["logistic_science"]
            self.assertEqual(published, saved["plan"])
            self.assertEqual(len(builds), 1)
            self.assertEqual(green.growth_proof(obs, parameters)["machines"], 3)
            self.assertEqual(len(saved["grown_machines"]), 2)

    def test_no_slot_is_a_site_need_and_a_slot_conflict_a_preflight_need(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = green_state(c, form="cluster")
            (root / "logistic-science.json").write_text(json.dumps(state), encoding="utf-8")
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            green = LogisticScience(game, None, c, None, None, site=None)
            blocked = green.plan_growth({"world_id": "w", "entities": []}, {"pack": GREEN, "machines": 2})
            self.assertEqual(plan_needs.from_result("science_growth", blocked)["kind"], "site")
            split = LogisticScience(game, None, c, None, None, site=None)
            split.state = green_state(c)
            failed = {"ok": False, "failed_check": "collision", "blocked": [{"name": "tree", "position": [1, 2]}]}
            with patch("factorio_agent.green_science.preflight", return_value=failed):
                blocked = split.plan_growth({"world_id": "w", "entities": []}, {"pack": GREEN, "machines": 2})
            self.assertEqual(plan_needs.from_result("science_growth", blocked)["kind"], "preflight")
        self.assertIn("science_growth", DRY_RUN)
        self.assertIn("science_growth", RELAXATIONS)


class GreenRequestTests(unittest.TestCase):
    """A 30/min request never yields the 5/min cluster (its ceiling) while the split row reaches it."""

    def test_thirty_per_minute_plans_the_split_row(self):
        from test_v2_green_site import KINDS, World

        with TemporaryDirectory() as directory:
            world = World(Path(directory), KINDS[0])
            result = world.prepare(rate=30)
            self.assertTrue(result["ok"], (result.get("reason"), result.get("failures", [])[:2]))
            core = result["core"]
            self.assertEqual(core["form"], "split")
            self.assertEqual(core["count"] + len(core["growth_slots"]), FRAME)
            layout = result["layout"]
            self.assertEqual(layout["horizon"], 30.0)
            self.assertTrue(any(row["ceiling"] < 30 for row in layout["excluded"]))
            # Each plate input declares its own sub-cluster's draw at the built rate (5 packs/min).
            demand = sorted((t["item"], t["demand_per_minute"]) for t in result["taps"])
            self.assertEqual(demand, [("copper-plate", 7.5), ("iron-plate", 7.5), ("iron-plate", 20.0)])

    def test_lab_consumption_raises_a_small_request_to_a_reachable_horizon(self):
        from test_v2_green_site import KINDS, World

        from factorio_agent import green_science as gs
        from factorio_agent.manufacturing import lab_green_entry_outside
        from factorio_agent.site.transform import in_canonical

        with TemporaryDirectory() as directory:
            world = World(Path(directory), KINDS[0])
            lab_port = in_canonical(world.labs, lab_green_entry_outside)["port"]
            low = gs.planned_green_layouts(world.site, "w1", world.green.catalog, lab_port, rate=5, tick=100)
            self.assertEqual(low[0]["layout"]["genome"]["form"], "cluster")
            gs.settle_green(world.site, "w1", None)
            labs = gs.planned_green_layouts(
                world.site, "w1", world.green.catalog, lab_port, rate=5, tick=100, horizon=20
            )
            self.assertEqual(labs[0]["layout"]["genome"]["form"], "split")
            self.assertNotIn("cluster", {c["layout"]["genome"]["form"] for c in labs})
            gs.settle_green(world.site, "w1", None)


def feed(*, green_join=True, red_from_north=False):
    """Three labs on a westbound lab belt: red from behind the head, green side-loaded from the south."""
    labs = lab_row(0.5, 10.5, 3)
    head = labs["ports"][0]["position"]  # (30.5, 7.5)
    x, y = head["x"], head["y"]
    es = [entity("transport-belt", x + 1, y, 12), entity("transport-belt", x + 2, y, 12)]
    if red_from_north:
        es += [entity("assembling-machine-1", x + 2, y - 3, recipe=RED), entity("inserter", x + 2, y - 1, 0)]
    else:
        es += [entity("assembling-machine-1", x + 2, y + 3, recipe=RED), entity("inserter", x + 2, y + 1, 8)]
    science = {"labs": labs, "plan": _plan(list(labs["entities"]) + es, [])}
    green = [
        entity("assembling-machine-1", x - 3, y + 2, recipe=GREEN),
        entity("inserter", x - 1, y + 2, 12),
        entity("transport-belt", x, y + 2, 0),
    ]
    if green_join:
        green.append(entity("transport-belt", x, y + 1, 0))
    return science, {"world_id": "w", "plan": _plan(green, [])}


class LabFeedTests(unittest.TestCase):
    def test_every_lab_gets_both_packs_on_their_own_lanes(self):
        science, green = feed()
        row = lab_feed.audit(science, [green], per_lab=1.0)
        self.assertEqual(row["counts"], {})
        self.assertEqual({p: v["cause"] for p, v in row["packs"].items()}, {RED: "ok", GREEN: "ok"})

    def test_too_few_green_machines_is_the_source_not_the_feed(self):
        science, green = feed()
        row = lab_feed.audit(science, [green], per_lab=1.7)  # 3 labs draw 5.1 green/min, one machine makes 5
        self.assertEqual(row["packs"][GREEN]["cause"], "source_short")
        self.assertEqual(row["packs"][RED]["cause"], "ok")
        self.assertIsNone(lab_feed.rework({"findings": row["findings"]}))

    def test_a_broken_join_starves_every_lab_and_proposes_a_rework(self):
        science, green = feed(green_join=False)
        row = lab_feed.audit(science, [green], per_lab=1.0)
        self.assertEqual(row["packs"][GREEN]["cause"], "lane_starvation")
        self.assertEqual(lab_feed.rework(dict(row, block="laboratories"))["action"], "rework_lab_feed")

    def test_two_packs_on_one_lane_are_found(self):
        science, green = feed(red_from_north=True)
        row = lab_feed.audit(science, [green], per_lab=1.0)
        self.assertIn("shared_lane", row["counts"])

    def test_the_bandwidth_audit_reads_the_lab_feed_of_a_runtime(self):
        science, green = feed()
        science = dict(science, world_id="w", labs=dict(science["labs"]))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "automation-science.json").write_text(json.dumps(science), encoding="utf-8")
            (root / "logistic-science.json").write_text(json.dumps(green), encoding="utf-8")
            rows = [r for r in ba.audit_root(root) if r["type"] == "lab_feed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["packs"][GREEN]["cause"], "source_short")  # 3 labs x 2/min > 5/min


if __name__ == "__main__":
    unittest.main()
