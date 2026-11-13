"""Obsolete startup science cell: retired by rule once permanent science runs; labs reused by growth (science_retirement)."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import obsolete
from factorio_agent import progression as pg
from factorio_agent import science_growth as sg
from factorio_agent import science_retirement as sr
from factorio_agent.board import lab_slot_entities, startup_science_cell
from factorio_agent.construction_supply import V2Bootstrap
from factorio_agent.handlers.smelting_and_science import science_growth_step
from factorio_agent.jobs import CAPABILITIES

from test_v2_science_growth import observed, state as science_state

WORLD = "w"
RED = sr.RED
CATALOG = SimpleNamespace(
    items={"lab": {"stack_size": 10}, "assembling-machine-1": {"stack_size": 50}, RED: {"stack_size": 200}}
)
STARTER_LAB = {"x": -60.5, "y": -7.5}


def job(capability, status="succeeded", **parameters):
    return {"capability": capability, "parameters": parameters, "status": status, "world_id": WORLD}


# Every temporary research step of the roadmap succeeded, then the permanent row.
TEMP_DONE = [
    job("starter_research", technology="automation"),
    job("bootstrap_research", technology="electric-mining-drill"),
    job("bootstrap_logistics"),
]
SCIENCE_DONE = TEMP_DONE + [job("automation_science", rate_per_minute=30)]
RESEARCHED = ["automation", "electric-mining-drill", "logistics"]


def receipt(*, working=True, grow=False, research="fast-inserter", technologies=RESEARCHED):
    evidence = {
        "labs_observed": 2,
        "labs_powered": 2,
        "lab_status": ["working" if working else "missing_science_packs"] * 2,
        "lab_packs": [3, 3],
        "machines_observed": 2,
        "machine_status": ["working", "working"],
        "belt_packs": 4,
    }
    return {
        "world_id": WORLD,
        "tick": 100,
        "research": research,
        "technologies": list(technologies),
        "science_growth": {"grow": grow, "target_labs": 4 if grow else 2, "target_machines": 2, "evidence": evidence},
    }


class PlannerTests(unittest.TestCase):
    def nodes(self, jobs, obs, queue=()):
        return sr.science_retirement_nodes(jobs, list(queue), obs, CAPABILITIES)

    def test_retired_once_permanent_science_is_verified(self):
        [node] = self.nodes(SCIENCE_DONE, receipt())
        self.assertEqual((node["id"], node["capability"], node["parameters"]), (sr.NODE_ID, sr.CAPABILITY, {}))
        self.assertEqual(node["priority"], obsolete.RETIRE_PRIORITY)
        self.assertEqual(self.nodes(SCIENCE_DONE + [job(sr.CAPABILITY, status="blocked")], receipt()), [])
        self.assertEqual(self.nodes(SCIENCE_DONE, receipt(), [{"capability": sr.CAPABILITY}]), [])

    def test_not_retired_without_the_successor_or_its_evidence(self):
        self.assertEqual(self.nodes(TEMP_DONE, receipt()), [])  # no automation_science yet
        self.assertEqual(self.nodes(SCIENCE_DONE, receipt(working=False)), [])  # labs not researching
        self.assertEqual(self.nodes(SCIENCE_DONE, receipt(research=None)), [])
        self.assertEqual(self.nodes(SCIENCE_DONE, dict(receipt(), science_growth=None)), [])
        self.assertEqual(self.nodes([job("automation_science")], receipt()), [])  # no temporary cell ever built

    def test_not_retired_while_a_bootstrap_batch_runs_or_a_cell_step_remains(self):
        running = SCIENCE_DONE + [job("bootstrap_research", status="running", technology="logistics-2")]
        self.assertEqual(self.nodes(running, receipt()), [])
        paused = SCIENCE_DONE + [job("bootstrap_logistics", status="paused")]
        self.assertEqual(self.nodes(paused, receipt()), [])
        # The logistics step is neither researched nor succeeded: the cell still has work.
        jobs = [j for j in SCIENCE_DONE if j["capability"] != "bootstrap_logistics"]
        self.assertEqual(self.nodes(jobs, receipt(technologies=["automation", "electric-mining-drill"])), [])
        self.assertEqual(len(self.nodes(jobs, receipt())), 1)  # researched natively: satisfied

    def test_growth_wanting_hardware_moves_it_before_growth(self):
        [node] = self.nodes(SCIENCE_DONE, receipt(grow=True))
        self.assertEqual(node["priority"], pg.SCIENCE_GROWTH_PRIORITY + 1)
        self.assertGreater(node["priority"], pg.SCIENCE_GROWTH_PRIORITY)

    def test_autopilot_proposes_it_after_the_roadmap(self):
        rows = pg.autopilot_nodes(SCIENCE_DONE, [], receipt(), CAPABILITIES, limit=50)
        self.assertIn(sr.NODE_ID, [r["id"] for r in rows])

    def test_registry_lists_the_three_retirements_and_later_ones(self):
        implemented = {o.capability for o in obsolete.REGISTRY if o.implemented}
        self.assertTrue({"supply_cell", "starter_retirement", sr.CAPABILITY} <= implemented)
        self.assertTrue(any(not o.implemented for o in obsolete.REGISTRY))
        sr.validate_parameters({})
        with self.assertRaises(ValueError):
            sr.validate_parameters({"x": 1})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=str(self.root)))
        self.science = science_state(machines=2)
        self.write("automation-science.json", self.science)
        self.cell = startup_science_cell(-90.5, 0.5)
        self.board = {
            "world_id": WORLD,
            "catalog_fingerprint": "catalog",
            "blocks": {"startup_science": self.cell, "laboratories": self.science["labs"]},
        }
        self.write("pcb-board.json", self.board)
        self.write("starter-research.json", {"world_id": WORLD, "lab_site": {"ok": True, "position": STARTER_LAB}})
        slot = lab_slot_entities(self.cell)[0]["lab"]
        self.batch = {
            "world_id": WORLD,
            "technology": "electric-mining-drill",
            "labs": [{"name": "lab", "position": STARTER_LAB, "unit_number": 5}, dict(slot, unit_number=6)],
        }
        self.write("bootstrap-research-electric-mining-drill.json", self.batch)
        self.jobs({})
        self.storage = Mock()
        self.storage.step.return_value = None
        self.ctl = sr.StartupScienceRetirement(self.game, CATALOG, self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def jobs(self, rows):
        self.write("jobs.json", {"world_id": WORLD, "jobs": rows})

    def temp_entities(self, *, lab_packs=7):
        rows, unit = [], 100
        cell = [e for e in self.cell["entities"]]
        labs = [{"name": "lab", "position": STARTER_LAB}, lab_slot_entities(self.cell)[0]["lab"]]
        for e in cell + labs:
            unit += 1
            row = dict(deepcopy(e), unit_number=unit, inventory={})
            if e["name"] == "lab":
                row["inventory"] = {RED: lab_packs}
            if e.get("recipe") == "iron-gear-wheel":
                row["inventory"] = {"iron-plate": 6, "iron-gear-wheel": 2}
            rows.append(row)
        return rows

    def obs(self, temp, *, empty=40, inventory=None):
        base = observed(self.science)
        base.update(
            entities=base["entities"] + list(temp),
            technologies={t: True for t in RESEARCHED},
            inventory=inventory or {},
            inventory_empty_slots=empty,
        )
        return base


class StepTests(Fixture):
    def test_never_dismantles_a_cell_with_a_funded_unfinished_batch(self):
        self.write("bootstrap-research-logistics-2.json", {"world_id": WORLD, "technology": "logistics-2"})
        result = self.ctl.step(self.obs(self.temp_entities()))
        self.assertEqual(result["reason"], "startup_science_retirement_batch_unfinished")
        self.assertEqual(result["evidence"]["batch"], "science_cell_extension_batch_unfinished")
        self.assertFalse((self.root / sr.STATE_FILE).exists())

    def test_waits_while_a_temporary_research_job_is_open(self):
        self.jobs({"b": job("bootstrap_research", status="waiting", technology="x")})
        result = self.ctl.step(self.obs(self.temp_entities()))
        self.assertEqual(result["reason"], "startup_science_retirement_waits_for_research_jobs")

    def test_waits_without_live_permanent_science(self):
        # No research and the permanent labs hold no red packs: a wait with its unblock named, never a block.
        base = observed(self.science, lab_status="missing_science_packs", lab_packs=0, research=None)
        obs = self.obs(self.temp_entities())
        obs.update(entities=base["entities"] + self.temp_entities(), research=None)
        result = self.ctl.step(obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "startup_science_retirement_waits_for_permanent_red_science_supply")
        self.assertIn("wait_for", result["evidence"])
        self.assertFalse((self.root / sr.STATE_FILE).exists())

    def test_idle_labs_stocked_with_red_packs_count_as_permanent_science(self):
        # Seeds 1024125955 / 4031765348 (long-1114-0039): research needs green packs, the permanent
        # labs idle with red packs from the row's machines; the cell's job is taken over.
        base = observed(self.science, lab_status="missing_science_packs", lab_packs=40, research="steel-processing")
        obs = self.obs(self.temp_entities())
        obs.update(entities=base["entities"] + self.temp_entities(), research="steel-processing")
        action = self.ctl.step(obs)
        self.assertEqual((action["type"], action["name"]), ("mine", "inserter"))

    def test_records_only_temporary_hardware_and_mines_it_guarded(self):
        temp = self.temp_entities()
        action = self.ctl.step(self.obs(temp))
        self.assertEqual((action["type"], action["name"]), ("mine", "inserter"))
        self.assertEqual(action["expected_entity_world_id"], WORLD)
        self.assertIsInstance(action["expected_entity_unit"], int)
        saved = json.loads((self.root / sr.STATE_FILE).read_text())
        self.assertEqual(saved["phase"], "dismantling")
        names = [e["name"] for e in saved["entities"]]
        self.assertNotIn("small-electric-pole", names)  # poles stay
        self.assertEqual(names.count("lab"), 4)  # starter lab + three slots, the batch labs deduplicated
        permanent = {sg._key(e) for e in self.science["labs"]["entities"]}
        self.assertFalse(permanent & {sg._key(e) for e in saved["entities"]})
        self.assertEqual(names, sorted(names, key=lambda n: sr.ORDER.get(n, 3)))  # inserters first, chests last
        self.assertTrue(sr.dismantled(self.root, WORLD))  # never reserved or built again from here

    def test_an_entity_of_another_block_is_never_recorded(self):
        self.board["blocks"]["laboratories"]["entities"].append({"name": "lab", "position": STARTER_LAB})
        self.write("pcb-board.json", self.board)
        self.ctl.step(self.obs(self.temp_entities()))
        saved = json.loads((self.root / sr.STATE_FILE).read_text())
        self.assertNotIn(("lab", STARTER_LAB["x"], STARTER_LAB["y"]), {sg._key(e) for e in saved["entities"]})

    def test_makes_inventory_room_first(self):
        self.storage.step.return_value = {"type": "deposit", "name": "wooden-chest"}
        action = self.ctl.step(self.obs(self.temp_entities(), empty=2))
        self.assertEqual(action["type"], "deposit")
        needed = self.storage.step.call_args.kwargs["minimum_slots"]
        self.assertGreater(needed, 2)
        bare = sr.StartupScienceRetirement(self.game, CATALOG)
        self.assertEqual(
            bare.step(self.obs(self.temp_entities(), empty=2))["reason"], "obsolete_block_needs_inventory_room"
        )

    def test_releases_the_block_delivers_leftover_packs_and_succeeds(self):
        self.ctl.step(self.obs(self.temp_entities()))
        # Every recorded entity mined: its 14 packs and plates are in the inventory now.
        poles = [e for e in self.temp_entities() if e["name"] == "small-electric-pole"]
        carried = {RED: 14, "lab": 4, "assembling-machine-1": 2}
        action = self.ctl.step(self.obs(poles, inventory=carried))
        self.assertEqual((action["type"], action["name"], action["inventory"]), ("insert", "lab", "lab_input"))
        self.assertEqual(action["item"], RED)
        self.assertEqual(action["count"], 14)
        built = {sg._key(e) for e in sg.built_labs(self.science)}
        self.assertIn(("lab", action["position"]["x"], action["position"]["y"]), built)
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertNotIn("startup_science", board["blocks"])
        self.assertIn("laboratories", board["blocks"])
        self.assertEqual(json.loads((self.root / sr.STATE_FILE).read_text())["phase"], "retired")
        done = self.ctl.step(self.obs(poles, inventory={"lab": 4, "assembling-machine-1": 2}))
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["evidence"]["dismantled"]["lab"], 4)
        self.assertIsNotNone(self.ctl.proof(self.obs(poles)))
        self.assertIsNone(self.ctl.proof(self.obs(self.temp_entities())))  # something stands again: not proven

    def test_packs_stay_carried_when_every_permanent_lab_is_full(self):
        self.ctl.step(self.obs(self.temp_entities()))
        obs = self.obs([], inventory={RED: 9})
        for e in obs["entities"]:
            if e["name"] == "lab":
                e["inventory"] = {RED: sr.LAB_PACK_STACK}
        self.assertEqual(self.ctl.step(obs)["status"], "succeeded")


class ReuseTests(Fixture):
    def test_growth_waits_while_labs_are_being_moved(self):
        temp = self.temp_entities()
        self.ctl.step(self.obs(temp))
        self.jobs({"r": job(sr.CAPABILITY, status="running")})
        ex = SimpleNamespace(science_retirement=self.ctl, science=Mock(), builder=Mock())
        ex.builder.ensure_power.return_value = {"status": "succeeded"}
        held = self.ctl.holds_for_growth(self.obs(temp))
        self.assertEqual(held["reason"], "science_growth_waits_for_moved_startup_hardware")
        with unittest.mock.patch(
            "factorio_agent.handlers.smelting_and_science.shared_upkeep", return_value=(None, None)
        ):
            self.assertEqual(science_growth_step(ex, self.obs(temp), {"labs": 4, "machines": 2}), held)
            ex.science.grow.assert_not_called()
            # Moved: no recorded lab or assembler stands, growth builds from the carried stock.
            rest = [e for e in temp if e["name"] not in sr.REUSED]
            science_growth_step(ex, self.obs(rest), {"labs": 4, "machines": 2})
            ex.science.grow.assert_called_once()

    def test_a_blocked_retirement_never_holds_growth(self):
        temp = self.temp_entities()
        self.ctl.step(self.obs(temp))
        self.jobs({"r": job(sr.CAPABILITY, status="blocked")})
        self.assertIsNone(self.ctl.holds_for_growth(self.obs(temp)))

    def test_recovered_labs_are_used_before_crafting(self):
        bootstrap = V2Bootstrap(Mock(backend="assisted", cfg=self.game.cfg))
        bootstrap.catalog = CATALOG
        result = bootstrap.ensure_item({"inventory": {"lab": 4}, "entities": []}, "lab", 2)
        self.assertNotEqual(result.get("type"), "craft")
        self.assertEqual(result.get("status"), "succeeded")


class SharedHelperTests(unittest.TestCase):
    def test_guarded_mine_and_slots(self):
        self.assertIsNone(obsolete.guarded_mine({"name": "lab", "position": {"x": 0, "y": 0}}, WORLD, "r"))
        action = obsolete.guarded_mine({"name": "lab", "position": {"x": 0, "y": 0}, "unit_number": 3}, WORLD, "r")
        self.assertEqual((action["count"], action["expected_entity_unit"]), (1, 3))
        labs = [{"name": "lab", "inventory": {RED: 150}} for _ in range(11)]
        self.assertEqual(obsolete.slots_needed(CATALOG, labs), 2 + 9)  # 11 labs = 2 stacks, 1650 packs = 9

    def test_active_jobs_and_once(self):
        rows = [
            dict(job("a", status="waiting"), key="1", reason="waiting for machine-made belt"),
            dict(job("b", status="succeeded"), key="2"),
        ]
        self.assertEqual(obsolete.active_jobs(rows, reason_markers=["machine-made belt"]), ["1"])
        self.assertEqual(obsolete.active_jobs(rows, exclude=("a",)), [])
        self.assertTrue(obsolete.proposed(rows, [], "b"))
        self.assertFalse(obsolete.proposed(rows, [], "c"))


if __name__ == "__main__":
    unittest.main()
