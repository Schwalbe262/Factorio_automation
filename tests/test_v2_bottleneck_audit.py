"""Bottleneck auditor: sampled evidence, chain diagnosis, proposals, idle watchdog (PLANNERS.md "Bottleneck auditor").

The fixtures rebuild the live cases of eval-4177156614 / eval-1575138435 the
owner named (machine states and the receipts the planners gave then), so
each test pins the bottleneck the auditor names and the growth it proposes.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from factorio_agent import bottleneck_audit as ba
from factorio_agent import growth_providers as gp
from factorio_agent import throughput_growth as tg
from factorio_agent.jobs import CAPABILITIES

WORLD = "world-1"


def recipe(name, ingredients, product=None, category="crafting"):
    product = product or name
    return {
        "name": name,
        "category": category,
        "ingredients": [{"name": i, "amount": a, "type": "item"} for i, a in ingredients],
        "products": [{"name": product, "amount": 1, "type": "item"}],
        "main_product": product,
    }


CATALOG = {
    "recipes": {
        r["name"]: r
        for r in (
            recipe("iron-plate", [("iron-ore", 1)], category="smelting"),
            recipe("copper-plate", [("copper-ore", 1)], category="smelting"),
            recipe("stone-brick", [("stone", 2)], category="smelting"),
            recipe("steel-plate", [("iron-plate", 5)], category="smelting"),
            recipe("iron-gear-wheel", [("iron-plate", 2)]),
            recipe("automation-science-pack", [("copper-plate", 1), ("iron-gear-wheel", 1)]),
            recipe("inserter", [("iron-plate", 1), ("iron-gear-wheel", 1)]),
            recipe("transport-belt", [("iron-plate", 1), ("iron-gear-wheel", 1)]),
            recipe("logistic-science-pack", [("inserter", 1), ("transport-belt", 1)]),
        )
    },
    "technologies": {
        "automation-2": {"name": "automation-2", "ingredients": [{"name": "automation-science-pack", "amount": 1}]},
        "logistics-2": {
            "name": "logistics-2",
            "ingredients": [
                {"name": "automation-science-pack", "amount": 1},
                {"name": "logistic-science-pack", "amount": 1},
            ],
        },
    },
    "items": {
        n: {"name": n}
        for n in (
            "iron-ore",
            "copper-ore",
            "stone",
            "coal",
            "iron-plate",
            "copper-plate",
            "steel-plate",
            "stone-brick",
            "iron-gear-wheel",
            "automation-science-pack",
            "logistic-science-pack",
            "inserter",
            "transport-belt",
            "science",
        )
    },
}

TYPES = {
    "stone-furnace": "furnace",
    "electric-mining-drill": "mining-drill",
    "assembling-machine-1": "assembling-machine",
    "lab": "lab",
    "transport-belt": "transport-belt",
}


class Scene:
    """Entities placed in board blocks; ``observation`` at a tick; ``run`` samples a window through ``receipt``."""

    def __init__(self):
        self.blocks = {}
        self.entities = []
        self.x = 0

    def add(self, block, name, count, status, *, recipe=None, inventory=None, resource=None, product=None, **extra):
        row = self.blocks.setdefault(block, {"entities": []})
        if resource:
            row["resource"] = resource
        if product:
            row["product"] = product
        for _ in range(count):
            self.x += 4
            position = {"x": self.x + 0.5, "y": 10.5}
            row["entities"].append({"name": name, "position": position})
            entity = {
                "name": name,
                "type": TYPES[name],
                "position": position,
                "status_name": status,
                "inventory": dict(inventory or {}),
                **extra,
            }
            if recipe:
                entity["recipe"] = recipe
            self.entities.append(entity)
        return self

    def observation(self, tick, **receipts):
        return {"world_id": WORLD, "tick": tick, "entities": self.entities, **receipts}

    def run(self, jobs, *, start=240000, samples=4, queue=None, **receipts):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "pcb-board.json").write_text(json.dumps({"world_id": WORLD, "blocks": self.blocks}))
            if queue is not None:
                Path(root, "work_queue.json").write_text(json.dumps({"nodes": queue}))
            out = None
            for i in range(samples):
                out = ba.receipt(root, CATALOG, jobs, self.observation(start + i * 600, **receipts))
            return out


def job(capability, status="succeeded", key=None, last_tick=1000, **fields):
    return {
        "key": key or f"{capability}-{status}",
        "capability": capability,
        "status": status,
        "parameters": fields.pop("parameters", {}),
        "last_tick": last_tick,
        "actions": fields.pop("actions", 10),
        **fields,
    }


BASE_JOBS = [
    job("power"),
    job("smelting_district"),
    job("automation_science"),
    job("mining_outpost", parameters={"resource": "iron-ore", "rate_per_minute": 120}),
]

# eval-4177156614 at ~67 game min (throughput_growth receipt, iron-plate): taps 24/min, bill 937.7 plates,
# 6 district furnaces (112.5/min) under 2/3 drill pairs (120/min); the single-step rule said wait.
IRON = tg.PlateFacts(
    plate="iron-plate",
    resource="iron-ore",
    tap_rate=24.0,
    bill=937.7,
    furnace_nominal=112.5,
    furnace_each=18.75,
    per_side=3,
    capacity_slots=12,
    ore_nominal=120.0,
    drill_rate=30.0,
    pairs_built=2,
    pair_slots=3,
    belt_cap=900.0,
    ore_step_cost={"iron-plate": 52.0, "copper-plate": 10.0, "wood": 1.0},
    ore_step_entities=8,
    furnace_step_cost={"iron-plate": 25.0, "copper-plate": 7.0, "stone": 10.0, "wood": 1.0},
    furnace_step_entities=14,
    stocks={"copper-plate": (50.0, 37.5), "iron-plate": (0.0, 72.0), "stone": (50.0, 15.0)},
    measured=148.9,
)


def top(receipt):
    rows = receipt["bottlenecks"]
    assert rows, receipt
    return rows[0]


class LiveCaseTests(unittest.TestCase):
    def test_iron_bus_shortage_67_min_names_the_district_and_grows_iron(self):
        scene = (
            Scene()
            .add("smelting_district", "stone-furnace", 6, "working", recipe="iron-plate", inventory={"iron-ore": 3})
            .add("iron-ore_mining", "electric-mining-drill", 4, "working", resource="iron-ore")
        )
        waiting = job(
            "planned_main_bus",
            "waiting",
            reason="waiting for iron-plate (district plates)",
            evidence={"item": "iron-plate", "need": 120},
        )
        receipt = scene.run(BASE_JOBS + [waiting], throughput_growth={"plates": [tg.decide(IRON)]})
        b = top(receipt)
        self.assertEqual(
            (b["group"], b["kind"], b["item"]), ("smelting_district|iron-plate", "at_capacity", "iron-plate")
        )
        self.assertEqual(b["consumers"][0]["group"], "job:planned_main_bus:planned_main_bus-waiting")
        self.assertEqual(b["lost_seconds_per_minute"], 60.0)
        proposal = b["proposal"]
        self.assertEqual((proposal["provider"], proposal["status"]), ("plates", "propose"), proposal)
        self.assertEqual(proposal["option"]["capability"], "mining_outpost")
        self.assertEqual(proposal["seconds_saved"], 103)
        self.assertTrue(proposal["nodes"][0].startswith("auto_throughput_iron-ore_mining_outpost_"))

    def test_furnaces_starving_for_ore_walk_up_to_the_mine(self):
        scene = (
            Scene()
            .add("smelting_district", "stone-furnace", 6, "no_ingredients", recipe="iron-plate")
            .add("iron-ore_mining", "electric-mining-drill", 4, "working", resource="iron-ore")
        )
        waiting = job("steel_production", "waiting", reason="waiting for iron-plate", evidence={"item": "iron-plate"})
        b = top(scene.run(BASE_JOBS + [waiting]))
        self.assertEqual((b["group"], b["kind"], b["item"]), ("iron-ore_mining|iron-ore", "at_capacity", "iron-ore"))
        self.assertEqual([link["item"] for link in b["chain"]], ["iron-plate", "iron-ore"])
        self.assertEqual(b["proposal"]["provider"], "plates")  # the plate's ore: throughput_growth's row
        self.assertEqual(b["proposal"]["gate"], "plates_wait")

    def test_labs_short_of_red_82_min_grow_the_pack_machines(self):
        # eval-4177156614 at 82 min: lab_status [missing_science_packs, working], both machines working.
        scene = (
            Scene()
            .add("laboratories", "lab", 1, "missing_science_packs")
            .add("laboratories", "lab", 1, "working", inventory={"automation-science-pack": 2})
            .add("automation_science", "assembling-machine-1", 2, "working", recipe="automation-science-pack")
            .add("automation_science", "assembling-machine-1", 1, "working", recipe="iron-gear-wheel")
        )
        growth = {
            "grow": True,
            "labs": 2,
            "machines": 2,
            "target_labs": 2,
            "target_machines": 3,
            "chain": "packs",
            "reason": "labs starve for packs",
        }
        receipt = scene.run(BASE_JOBS, research="automation-2", science_growth=growth)
        b = top(receipt)
        self.assertEqual((b["group"], b["kind"]), ("automation_science|automation-science-pack", "at_capacity"))
        self.assertEqual(b["consumers"][0]["group"], "laboratories|research")
        self.assertEqual(b["lost_seconds_per_minute"], 60.0)  # one of two labs starves all window
        self.assertEqual(b["proposal"]["status"], "propose", b["proposal"])
        self.assertEqual(b["proposal"]["nodes"], ["auto_science_growth_2_3"])

    def test_labs_limited_with_packs_on_the_belt_127_min_grow_labs(self):
        # eval-4177156614 at 126.7 min: both labs working with packs, 128 red packs on the belt.
        scene = (
            Scene()
            .add("laboratories", "lab", 2, "working", inventory={"automation-science-pack": 4})
            .add("laboratories", "transport-belt", 1, "working", belt_inventory={"automation-science-pack": 128})
            .add("automation_science", "assembling-machine-1", 2, "full_output", recipe="automation-science-pack")
        )
        growth = {
            "grow": True,
            "labs": 2,
            "machines": 2,
            "target_labs": 4,
            "target_machines": 2,
            "chain": "labs",
            "reason": "labs-limited",
        }
        b = top(scene.run(BASE_JOBS, research="automation-2", science_growth=growth))
        self.assertEqual((b["group"], b["kind"], b["item"]), ("laboratories|research", "consumer_capacity", "research"))
        self.assertEqual(b["chain"][0]["belt_packs"], 128)
        self.assertEqual(b["lost_seconds_per_minute"], 120.0)  # two pack machines stand output-full
        self.assertEqual(b["proposal"]["nodes"], ["auto_science_growth_4_2"])

    def test_green_packs_short_for_eight_labs_185_min_names_green_and_the_missing_hook(self):
        # eval-4177156614 at ~185 min: green 3.7x short for 8 labs; no growth rule for green existed.
        scene = (
            Scene()
            .add("laboratories", "lab", 8, "missing_science_packs", inventory={"automation-science-pack": 5})
            .add("logistic_science", "assembling-machine-1", 3, "working", recipe="logistic-science-pack")
        )
        b = top(scene.run(BASE_JOBS, research="logistics-2"))
        self.assertEqual(
            (b["group"], b["kind"], b["item"]),
            ("logistic_science|logistic-science-pack", "at_capacity", "logistic-science-pack"),
        )
        self.assertEqual(b["lost_seconds_per_minute"], 480.0)
        self.assertEqual((b["proposal"]["gate"], b["proposal"]["provider"]), ("science_wait", "science"))
        # The science hook exists now (pack_growth); with no pack_growth receipt in this scene it waits and says so.
        self.assertIn("no growth receipt for logistic-science-pack", b["proposal"]["reason"])

    def test_a_science_growth_options_hook_takes_every_pack(self):
        scene = (
            Scene()
            .add("laboratories", "lab", 8, "missing_science_packs", inventory={"automation-science-pack": 5})
            .add("logistic_science", "assembling-machine-1", 3, "working", recipe="logistic-science-pack")
        )
        node = {
            "id": "auto_science_growth_green_6",
            "capability": "science_growth",
            "parameters": {"pack": "logistic-science-pack", "machines": 6},
            "priority": -600,
            "depends_on": [],
            "reason": "green",
        }

        def hook(bottleneck, observation, state):
            return [
                gp._option(
                    "science",
                    capability="science_growth",
                    parameters=node["parameters"],
                    nodes=[node],
                    seconds_saved=300,
                    reason="second green cluster",
                )
            ]

        from factorio_agent import science_growth

        with mock.patch.object(science_growth, "growth_options", hook, create=True):
            b = top(scene.run(BASE_JOBS, research="logistics-2"))
        self.assertEqual(b["proposal"]["status"], "propose")
        self.assertEqual(b["proposal"]["nodes"], ["auto_science_growth_green_6"])

    def test_steel_starved_while_district_backs_up_blames_the_steel_feed(self):
        # eval-4177156614 steel/brick block: raw side-loaded before the splitter, each row a quarter belt.
        scene = (
            Scene()
            .add("steel_production", "stone-furnace", 4, "no_ingredients", recipe="steel-plate", inventory={"coal": 5})
            .add("smelting_district", "stone-furnace", 6, "full_output", recipe="iron-plate")
        )
        audit = {
            "blocks": [
                {
                    "block": "steel_production",
                    "type": "furnace_block",
                    "findings": [
                        {
                            "kind": "single_lane_feed",
                            "item": "iron-plate",
                            "delivered_per_minute": 56.0,
                            "demand_per_minute": 112.0,
                        }
                    ],
                }
            ],
            "rework": [{"block": "steel_production", "cause": "side_load_before_splitter"}],
        }
        b = top(scene.run(BASE_JOBS, belt_bandwidth=audit))
        self.assertEqual(
            (b["group"], b["block"], b["kind"], b["item"]),
            ("steel_production|steel-plate", "steel_production", "transport", "iron-plate"),
        )
        self.assertEqual(b["evidence"]["belt_findings"][0]["kind"], "single_lane_feed")
        self.assertEqual(b["proposal"]["gate"], "steel_wait")
        self.assertIn("rework_feed_head", b["proposal"]["reason"])

    def test_brick_starved_for_stone_grows_the_stone_mine(self):
        scene = (
            Scene()
            .add("brick_production", "stone-furnace", 4, "no_ingredients", recipe="stone-brick", inventory={"coal": 5})
            .add("stone_mining", "electric-mining-drill", 2, "working", resource="stone")
        )
        trunks = {
            "trunks": [
                {
                    "trunk": "stone:1",
                    "resource": "stone",
                    "grow": True,
                    "demand_per_minute": 45.0,
                    "supply_per_minute": 30.0,
                    "target_per_minute": 45.0,
                    "second_lane_required": False,
                }
            ]
        }
        b = top(scene.run(BASE_JOBS, mine_trunks=trunks))
        self.assertEqual((b["group"], b["kind"]), ("stone_mining|stone", "at_capacity"))
        self.assertEqual(b["proposal"]["provider"], "mine")
        self.assertEqual(b["proposal"]["nodes"], ["auto_mine_trunk_stone_45"])

    def test_brick_furnaces_holding_a_full_brick_stack_are_full_not_a_stone_transport_limit(self):
        # eval-1575138435 (257 min): both brick furnaces "no_ingredients" with 100 bricks each (nothing
        # takes bricks yet), the stone drills output-full: the auditor named stone mining "transport".
        catalog = dict(
            CATALOG, items=dict(CATALOG["items"], **{"stone-brick": {"name": "stone-brick", "stack_size": 100}})
        )
        scene = (
            Scene()
            .add(
                "brick_production",
                "stone-furnace",
                2,
                "no_ingredients",
                recipe="stone-brick",
                inventory={"coal": 5, "stone": 1, "stone-brick": 100},
            )
            .add("stone_mining", "electric-mining-drill", 2, "waiting_for_space_in_destination", resource="stone")
        )
        rows = {r["group"]: r for r in ba.entity_rows(scene.observation(1), {"blocks": scene.blocks}, catalog)}
        furnaces = next(r for g, r in rows.items() if g.startswith("brick_production"))
        self.assertEqual((furnaces["state"], furnaces["missing"]), ("full", []))
        held = dict(scene.entities[0], inventory={"coal": 5, "stone": 1, "stone-brick": 40})
        (row,) = ba.entity_rows({"entities": [held]}, {"blocks": scene.blocks}, catalog)
        self.assertEqual(row["state"], "starved")  # a partial stack still starves on stone

    def test_labs_without_research_93_min_are_idle_not_a_bottleneck(self):
        # eval-1575138435 at 93.5 min (post-mortem observe-raw): 8 labs no_research_in_progress,
        # every red machine output-full.
        scene = (
            Scene()
            .add("laboratories", "lab", 8, "no_research_in_progress", inventory={"automation-science-pack": 10})
            .add("automation_science", "assembling-machine-1", 4, "full_output", recipe="automation-science-pack")
        )
        waiting = job("research", "waiting", reason="waiting native labs consume normally produced science")
        receipt = scene.run(BASE_JOBS + [waiting])
        self.assertEqual(receipt["bottlenecks"], [])  # "science" is no makeable item
        self.assertEqual(receipt["idle"][0]["kind"], "no_research")
        self.assertEqual(receipt["idle"][0]["seconds_per_minute"], 480.0)


class EvidenceTests(unittest.TestCase):
    def test_window_is_world_bound_spaced_and_tracks_the_last_action(self):
        with tempfile.TemporaryDirectory() as root:
            obs = {"world_id": WORLD, "tick": 1000, "entities": []}
            ba.record(root, obs, {"tick": 1000, "actions": 5, "groups": {}})
            ba.record(root, obs, {"tick": 1100, "actions": 5, "groups": {}})  # inside SAMPLE_TICKS: not kept
            state = ba.record(root, obs, {"tick": 1400, "actions": 7, "groups": {}})
            self.assertEqual([s["tick"] for s in state["samples"]], [1000, 1400])
            self.assertEqual(state["last_action"], {"actions": 7, "tick": 1400})
            state = ba.record(root, dict(obs, world_id="other"), {"tick": 2000, "actions": 7, "groups": {}})
            self.assertEqual([s["tick"] for s in state["samples"]], [2000])

    def test_too_few_samples_diagnose_nothing(self):
        scene = Scene().add("laboratories", "lab", 2, "missing_science_packs")
        receipt = scene.run(BASE_JOBS, research="automation-2", samples=2)
        self.assertEqual(receipt["bottlenecks"], [])
        self.assertTrue(receipt["status"].startswith("sampling"))

    def test_missing_ingredient_and_fuel(self):
        scene = (
            Scene()
            .add(
                "automation_science",
                "assembling-machine-1",
                1,
                "item_ingredient_shortage",
                recipe="automation-science-pack",
                inventory={"copper-plate": 3},
            )
            .add("steel_production", "stone-furnace", 1, "no_fuel", recipe="steel-plate", inventory={"iron-plate": 10})
        )
        rows = {r["group"]: r for r in ba.entity_rows(scene.observation(1), {"blocks": scene.blocks}, CATALOG)}
        self.assertEqual(rows["automation_science|automation-science-pack"]["missing"], ["iron-gear-wheel"])
        self.assertEqual(rows["steel_production|steel-plate"]["missing"], ["coal"])

    def test_a_furnace_without_recipe_takes_its_neighbours(self):
        scene = Scene().add("smelting_district", "stone-furnace", 1, "working", recipe="copper-plate")
        scene.add("smelting_district", "stone-furnace", 1, "no_ingredients")
        rows = ba.entity_rows(scene.observation(1), {"blocks": scene.blocks}, CATALOG)
        self.assertEqual({r["group"] for r in rows}, {"smelting_district|copper-plate"})


class IdleWatchdogTests(unittest.TestCase):
    def test_idle_actor_with_a_proposed_growth_names_the_queue_gate(self):
        scene = (
            Scene()
            .add("laboratories", "lab", 2, "missing_science_packs")
            .add("automation_science", "assembling-machine-1", 2, "working", recipe="automation-science-pack")
        )
        growth = {"grow": True, "target_labs": 2, "target_machines": 3, "reason": "packs"}
        queue = [
            {"id": "auto_a", "capability": "logistic_science", "parameters": {}, "status": "pending"},
            {"id": "auto_b", "capability": "steel_production", "parameters": {}, "status": "pending"},
        ]
        blocked = job(
            "produce", "blocked", reason="production_row_route_failed", next_remedy="site", needs={"kind": "site"}
        )
        receipt = scene.run(
            BASE_JOBS + [blocked], research="automation-2", science_growth=growth, queue=queue, samples=8, start=1000
        )
        watch = receipt["idle_with_work"]
        self.assertGreaterEqual(watch["idle_seconds"], ba.IDLE_SECONDS)
        gates = {w["kind"]: w["gate"] for w in watch["work"]}
        # The growth pass merges it this cycle; the queue (two pending nodes) no longer holds it.
        self.assertEqual(gates["bottleneck"], "growth_pass_next_cycle")
        self.assertFalse(watch["queue"]["queue_low"])
        self.assertEqual(gates["remedy"], "remedy_pending")
        fields = ba.event_fields(receipt)
        self.assertEqual(fields["top_gate"], "growth_pass_next_cycle")
        with mock.patch.object(ba, "GROWTH_PASS", False):  # without the pass: only the queue-gated autopilot
            watch = ba.idle_with_work({"last_action": {"tick": 0}}, receipt["bottlenecks"], BASE_JOBS, queue, 100000)
        self.assertEqual(watch["work"][0]["gate"], "queue_not_low")

    def test_supervisor_logs_one_event_per_episode_gate_and_minute(self):
        from types import SimpleNamespace

        from factorio_agent.supervisor import Supervisor

        events = []
        stub = SimpleNamespace(_event=lambda kind, **data: events.append((kind, data)))

        def cycle(idle, gate):
            watch = {
                "idle_seconds": idle,
                "since_tick": 100,
                "gates": {gate: 1},
                "work": [{"kind": "bottleneck", "item": "grow x", "gate": gate}],
            }
            Supervisor._idle_watch(stub, ({"tick": 1, "bottlenecks": {"idle_with_work": watch}}, None, None, None))

        cycle(70, "queue_not_low")
        cycle(100, "queue_not_low")  # same episode and gate, < 60 s later: no event
        cycle(140, "queue_not_low")
        cycle(150, "science_wait")
        Supervisor._idle_watch(stub, ({"tick": 2, "bottlenecks": {}}, None, None, None))
        self.assertEqual([d["idle_seconds"] for _k, d in events], [70, 140, 150])
        self.assertEqual({k for k, _d in events}, {"idle_with_work"})

    def test_no_watchdog_while_the_actor_acts(self):
        state = {"last_action": {"tick": 9000, "actions": 3}}
        self.assertIsNone(ba.idle_with_work(state, [{"lost_seconds_per_minute": 60}], [], [], 9600))

    def test_episodes_sum_seconds_per_gate(self):
        events = [
            {"event": "idle_with_work", "since_tick": 100, "idle_seconds": 90, "top_gate": "queue_not_low"},
            {"event": "idle_with_work", "since_tick": 100, "idle_seconds": 150, "top_gate": "queue_not_low"},
            {"event": "idle_with_work", "since_tick": 100, "idle_seconds": 200, "top_gate": "science_wait"},
            {"event": "idle_with_work", "since_tick": 9000, "idle_seconds": 70, "top_gate": "no_provider"},
        ]
        episodes = ba.idle_episodes(events)
        self.assertEqual([e["seconds"] for e in episodes], [140, 10])
        self.assertEqual(episodes[0]["gates"], {"queue_not_low": 90.0, "science_wait": 50.0})
        line = ba.format_idle(
            [
                {"idle_with_work": {"seconds": 150, "gates": {"queue_not_low": 90}}},
                {"idle_with_work": {"seconds": 0, "gates": {}}},
            ]
        )
        self.assertEqual(line, ["idle with work: 150s in 1/2 runs; top gates: queue_not_low 90s"])


class ProviderTests(unittest.TestCase):
    def test_transitional_blocks_are_not_grown(self):
        b = {
            "group": "startup_science|automation-science-pack",
            "block": "startup_science",
            "item": "automation-science-pack",
            "kind": "at_capacity",
            "consumers": [],
            "lost_seconds_per_minute": 60,
        }
        self.assertEqual(
            gp.assess(b, {}, {"jobs": [], "queue_nodes": [], "capabilities": CAPABILITIES})["gate"],
            "transitional_block",
        )

    def test_a_block_without_provider_names_it(self):
        b = {
            "group": "x|gizmo",
            "block": "x",
            "item": "gizmo",
            "kind": "at_capacity",
            "consumers": [{"block": "y"}],
            "lost_seconds_per_minute": 60,
        }
        self.assertEqual(
            gp.assess(b, {}, {"jobs": [], "queue_nodes": [], "capabilities": CAPABILITIES})["gate"], "no_provider"
        )

    def test_settle_window_after_a_growth_landed(self):
        row = dict(tg.decide(IRON))
        b = {
            "group": "smelting_district|iron-plate",
            "block": "smelting_district",
            "item": "iron-plate",
            "kind": "at_capacity",
            "consumers": [{"block": "job"}],
            "lost_seconds_per_minute": 60,
        }
        jobs = BASE_JOBS[:3] + [
            job("mining_outpost", parameters={"resource": "iron-ore", "rate_per_minute": 120}, last_tick=239000)
        ]
        proposal = gp.assess(
            b,
            {"tick": 240000, "throughput_growth": {"plates": [row]}},
            {"jobs": jobs, "queue_nodes": [], "capabilities": CAPABILITIES},
        )
        self.assertEqual(proposal["gate"], "settling")


PRODUCE_JOBS = BASE_JOBS + [
    job("produce", parameters={"item": "transport-belt", "rate_per_minute": 10.0}, last_tick=1000),
]


class GenericProduceRowTests(unittest.TestCase):
    def test_a_row_at_capacity_with_waiting_consumers_is_extended(self):
        # A job waits for belts; the belt row works all window: the generic provider extends the row.
        scene = Scene().add(
            "production_row_transport-belt", "assembling-machine-1", 2, "working", recipe="transport-belt"
        )
        waiting = job(
            "power_coal_feed", "waiting", reason="waiting for transport-belt", evidence={"item": "transport-belt"}
        )
        b = top(scene.run(PRODUCE_JOBS + [waiting]))
        self.assertEqual((b["group"], b["kind"]), ("production_row_transport-belt|transport-belt", "at_capacity"))
        proposal = b["proposal"]
        self.assertEqual((proposal["provider"], proposal["status"]), ("produce_row", "propose"), proposal)
        # The job starves the whole window: at most double the row per package.
        self.assertEqual(proposal["option"]["parameters"], {"item": "transport-belt", "rate_per_minute": 20.0})
        self.assertEqual(proposal["nodes"], ["auto_produce_transport-belt_20"])

    def test_partial_starvation_sizes_one_over_one_minus_share(self):
        b = {
            "block": "production_row_inserter",
            "item": "inserter",
            "kind": "at_capacity",
            "consumers": [{"block": "green", "n": 4, "lost_seconds_per_minute": 48.0}],
            "lost_seconds_per_minute": 48.0,
        }
        jobs = BASE_JOBS + [job("produce", parameters={"item": "inserter", "rate_per_minute": 12.0})]
        options = gp.produce_options(b, {}, {"jobs": jobs, "queue_nodes": [], "capabilities": CAPABILITIES})
        self.assertEqual(options[0]["parameters"]["rate_per_minute"], 15.0)  # 12 / (1 - 0.2)

    def test_an_open_produce_job_holds_the_lane(self):
        b = {
            "block": "production_row_inserter",
            "item": "inserter",
            "kind": "at_capacity",
            "consumers": [{"block": "green", "n": 1, "lost_seconds_per_minute": 60.0}],
            "lost_seconds_per_minute": 60.0,
        }
        jobs = BASE_JOBS + [
            job("produce", parameters={"item": "inserter", "rate_per_minute": 12.0}),
            job("produce", "running", key="belts", parameters={"item": "transport-belt", "rate_per_minute": 5.0}),
        ]
        proposal = gp.assess(b, {"tick": 50000}, {"jobs": jobs, "queue_nodes": [], "capabilities": CAPABILITIES})
        self.assertEqual(proposal["gate"], "growth_in_flight")


class GrowthPassTests(unittest.TestCase):
    def rows(self):
        scene = (
            Scene()
            .add("smelting_district", "stone-furnace", 6, "working", recipe="iron-plate", inventory={"iron-ore": 3})
            .add("iron-ore_mining", "electric-mining-drill", 4, "working", resource="iron-ore")
        )
        waiting = job("planned_main_bus", "waiting", reason="waiting for iron-plate", evidence={"item": "iron-plate"})
        self.jobs = BASE_JOBS + [waiting]
        self.observation = {"tick": 242000, "throughput_growth": {"plates": [tg.decide(IRON)]}}
        return scene.run(self.jobs, throughput_growth=self.observation["throughput_growth"])["bottlenecks"]

    def test_growth_nodes_one_package_per_bottleneck_and_dedupe(self):
        rows = self.rows()
        nodes = gp.growth_nodes(rows, self.jobs, [], self.observation, CAPABILITIES)
        self.assertEqual([n["capability"] for n in nodes], ["mining_outpost", "smelting_capacity"])
        queued = [dict(n, status="pending") for n in nodes]
        self.assertEqual(gp.growth_nodes(rows, self.jobs, queued, self.observation, CAPABILITIES), [])

    def test_supervisor_merges_growth_whatever_the_queue_holds(self):
        from types import SimpleNamespace

        from factorio_agent.supervisor import Supervisor

        rows = self.rows()
        merged, events = [], []
        work = SimpleNamespace(
            data={"nodes": [{"id": f"auto_step_{i}", "capability": "research", "status": "ready"} for i in range(5)]},
            refresh=lambda jobs, caps: None,
        )
        stub = SimpleNamespace(
            work=work,
            _merge=lambda nodes, caps, obs, source: merged.append((source, [n["id"] for n in nodes])),
            _event=lambda kind, **data: events.append(kind),
        )
        observation = dict(self.observation, bottlenecks={"bottlenecks": rows})
        status = {"jobs": {j["key"]: j for j in self.jobs}}
        self.assertTrue(Supervisor._growth(stub, (observation, status, CAPABILITIES, None)))
        self.assertEqual(merged[0][0], "bottleneck")
        self.assertEqual(len(merged[0][1]), 2)
        self.assertEqual(events, ["bottleneck_growth"])

    def test_summary_carries_the_rule_inputs(self):
        from factorio_agent.jobs import summary

        ledger = SimpleLedger()
        out = summary(
            {
                "world_id": WORLD,
                "tick": 1,
                "mine_trunks": {"trunks": []},
                "produce_plate_sources": {"iron-plate": True},
                "bottlenecks": {"bottlenecks": []},
            },
            ledger,
        )
        self.assertEqual(out["mine_trunks"], {"trunks": []})
        self.assertEqual(out["produce_plate_sources"], {"iron-plate": True})
        self.assertEqual(out["bottlenecks"], {"bottlenecks": []})


class SimpleLedger:
    data = {"jobs": {}}


if __name__ == "__main__":
    unittest.main()
