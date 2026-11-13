"""Feed head rework of a built smelting district bank (head_rework.py)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from factorio_agent import belt_flow as bf
from factorio_agent import head_rework as hr
from factorio_agent.bandwidth_audit import port_sources
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.site.transform import apply, variants
from factorio_agent.smelting_site import district_candidate, oriented

COUNTS = {"iron-ore": 20, "copper-ore": 4}  # iron: 10 per row, the most the shared head feeds


def shared_state(t=None):
    candidate = district_candidate(8, 8, COUNTS, head="shared")
    if t is not None:
        candidate = oriented(apply(t, candidate), t)
    plan = _plan(
        candidate["entities"],
        [b["ports"][-1] for b in candidate["banks"]],
        reserved_area=candidate["area"],
        underground_pairs=[],
        **({"site_transform": candidate["site_transform"]} if "site_transform" in candidate else {}),
    )
    return {"world_id": "w", "plan": plan, "banks": candidate["banks"]}, candidate


def key(e):
    return hr._key(e)


class PlanTests(unittest.TestCase):
    def test_every_orientation_yields_the_split_bank_and_a_complete_order(self):
        for t in variants((24, 16)):
            state, _candidate = shared_state(t)
            for index, resource in enumerate(hr.RESOURCES):
                with self.subTest(t=t, resource=resource):
                    rework = hr.district_rework(state, resource)
                    self.assertTrue(rework["ok"], rework)
                    self.assertEqual(rework["bank"]["feed_head"], "split")
                    self.assertEqual(hr.head_supports(state["banks"][index]), 10)
                    self.assertEqual(hr.head_supports(rework["bank"]), 12)
                    built = {key(e) for e in rework["first"]} | {key(e) for g in rework["groups"] for e in g["build"]}
                    self.assertEqual(built, {key(e) for e in rework["add"]})
                    mined = {key(e) for e in rework["clear"]} | {key(e) for g in rework["groups"] for e in g["mine"]}
                    self.assertEqual(mined, {key(e) for e in rework["remove"]})
                    # The other bank and every slot, pole and output belt stay as built.
                    other = state["banks"][1 - index]
                    self.assertEqual(rework["banks"][1 - index], other)
                    kept = {key(e) for e in state["banks"][index]["entities"]} - mined
                    self.assertTrue(kept <= {key(e) for e in rework["bank"]["entities"]})
                    self.assertTrue(
                        set(map(json.dumps, rework["bank"]["underground_pairs"]))
                        <= set(map(json.dumps, rework["plan"]["underground_pairs"]))
                    )

    def test_a_split_or_changed_head_is_not_reworked(self):
        state, _ = shared_state()
        rework = hr.district_rework(state, "iron-ore")
        state2 = dict(state, banks=rework["banks"], plan=rework["plan"])
        self.assertEqual(hr.district_rework(state2, "iron-ore")["reason"], "feed_head_already_split")
        plan = dict(
            state["plan"], entities=[e for e in state["plan"]["entities"] if key(e) != key(rework["remove"][0])]
        )
        self.assertEqual(
            hr.district_rework(dict(state, plan=plan), "iron-ore")["reason"],
            "feed_head_rework_plan_differs_from_shared_head",
        )


class CutoverFlowTests(unittest.TestCase):
    """Every intermediate state through the flow model: coal never reaches an ore lane."""

    ROWS = {"iron-ore": ((8, 1), (8, 11)), "copper-ore": ((8, 30), (8, 20))}

    def lanes(self, entities, candidate, resource):
        bank = next(b for b in candidate["banks"] if b["resource"] == resource)
        sources, _sinks, _ports = port_sources([candidate["coal_input"], bank["ports"][0]])
        rows = self.ROWS[resource]
        flow = bf.solve(entities, sources, sinks=list(rows))
        return [flow.at(tile) for tile in rows]

    def test_stages_keep_lanes_and_end_with_full_ore_lanes(self):
        for resource in hr.RESOURCES:
            state, candidate = shared_state()
            rework = hr.district_rework(state, resource)
            current = {key(e): e for e in state["plan"]["entities"]}
            final = self.lanes(rework["plan"]["entities"], candidate, resource)
            ore_lane = [next(lane for lane, items in row.items() if resource in items) for row in final]
            coal_lane = [next(lane for lane, items in row.items() if "coal" in items) for row in final]
            for row, ore, coal in zip(final, ore_lane, coal_lane):
                self.assertNotEqual(ore, coal, resource)
                self.assertEqual(row[ore], {resource: 450.0}, resource)  # a full ore lane per row
                self.assertEqual(set(row[coal]), {"coal"}, resource)
            stages = [("first", rework["first"], [])]
            stages += [(f"group {i}", g["build"], g["mine"]) for i, g in enumerate(rework["groups"])]
            stages.append(("clear", [], rework["clear"]))
            for name, built, mined in stages:
                for e in mined:
                    current.pop(key(e))
                for e in built:
                    current[key(e)] = e
                rows = self.lanes(list(current.values()), candidate, resource)
                for row, ore, coal in zip(rows, ore_lane, coal_lane):
                    with self.subTest(resource=resource, stage=name):
                        # Coal never on the lane that carries ore at the end (it would clog it).
                        self.assertNotIn("coal", row.get(ore, {}))
            self.assertEqual({key(e) for e in current.values()}, {key(e) for e in rework["plan"]["entities"]})


class DecisionTests(unittest.TestCase):
    def test_rework_pays_against_a_second_bank_only_when_the_head_limits_growth(self):
        state, _ = shared_state()
        bank = state["banks"][0]
        rework = hr.district_rework(state, "iron-ore")
        idle = hr.decide(bank, rework, refused=False)
        self.assertEqual((idle["ready"], idle["reason"]), (False, "head_feeds_every_built_and_next_row"))
        refused = hr.decide(bank, rework, refused=True)
        self.assertTrue(refused["ready"], refused)
        self.assertLess(refused["spend_seconds"] + hr.MIN_SAVING_SECONDS, refused["second_bank_seconds"])
        self.assertEqual(refused["head_supports_per_row"], 10)

    def test_growth_past_the_head_is_refused_and_the_rule_proposes_the_rework(self):
        state, _ = shared_state()
        bank = state["banks"][0]
        self.assertIsNone(hr.bank_growth_check(bank, 10))
        refusal = hr.bank_growth_check(bank, 11)
        self.assertEqual(refusal["reason"], hr.GROWTH_REFUSED)
        self.assertEqual(refusal["evidence"]["head_supports_per_row"], 10)
        jobs = [
            {"capability": "smelting_district", "status": "succeeded"},
            {
                "capability": "smelting_capacity",
                "status": "blocked",
                "reason": hr.GROWTH_REFUSED,
                "parameters": {"resource": "iron-ore", "rate_per_minute": 400},
            },
        ]
        with TemporaryDirectory() as root:
            (Path(root) / "smelting-district.json").write_text(json.dumps(state), encoding="utf-8")
            receipt = hr.receipt(root, {"world_id": "w"}, jobs)
        self.assertTrue(receipt["banks"]["iron-ore"]["ready"])
        self.assertFalse(receipt["banks"]["copper-ore"]["ready"])
        obs = {"feed_head_rework": receipt, "enabled_recipes": {"splitter": True, "underground-belt": True}}
        nodes = hr.feed_head_rework_nodes(jobs, [], obs, {hr.CAPABILITY: {}})
        self.assertEqual([n["parameters"] for n in nodes], [{"block": "smelting_district", "resource": "iron-ore"}])
        self.assertEqual(
            hr.feed_head_rework_nodes(jobs + [dict(nodes[0], status="running")], [], obs, {hr.CAPABILITY: {}}), []
        )
        hr.validate_parameters(nodes[0]["parameters"])
        with self.assertRaises(ValueError):
            hr.validate_parameters({"block": "steel_production", "resource": "iron-ore"})


class ControllerTests(unittest.TestCase):
    """The job: republish first, build the new parts, stocked cut-over by guarded mines, clear, prove."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.state, _ = shared_state()
        board = {"world_id": "w", "blocks": {"smelting_district": self.state["plan"]}, "connections": []}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        smelting = SimpleNamespace(state=json.loads(json.dumps(self.state)), path=self.root / "smelting-district.json")
        self.built = []

        class Builder:
            bootstrap = SimpleNamespace(ensure_item=lambda obs, name, count: {"status": "succeeded"})

            def ensure_plan(inner, obs, plan):
                live = {key(e) for e in obs["entities"]}
                missing = [e for e in plan["entities"] if key(e) not in live]
                if missing:
                    self.built.append(missing[0])
                    return {"type": "build", "name": missing[0]["name"], "position": missing[0]["position"]}
                return {"status": "succeeded"}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=lambda script: {"ok": True})
        catalog = SimpleNamespace(items={})
        self.job = hr.FeedHeadRework(game, Builder(), catalog, smelting=smelting)
        self.smelting = smelting

    def tearDown(self):
        self.dir.cleanup()

    def test_the_job_runs_to_the_split_head_in_order(self):
        from unittest.mock import patch

        live = {}
        for i, e in enumerate(self.state["plan"]["entities"]):
            live[key(e)] = dict(e, unit_number=i + 1)
        furnaces = [k for k in live if k[0] == "stone-furnace"]
        for k in furnaces:
            live[k]["inventory"] = {"iron-ore": 5, "copper-ore": 5}
        actions = []
        lanes = {"ok": True, "banks": [{"resource": "iron-ore", "lanes": [{"iron-ore": 9}, {"coal": 4}]}]}
        with (
            patch("factorio_agent.planning.preflight", return_value={"ok": True}),
            patch("factorio_agent.smelting.native_feed_lanes", return_value=lanes),
        ):
            for _ in range(200):
                obs = {
                    "world_id": "w",
                    "tick": len(actions),
                    "entities": list(live.values()),
                    "inventory": {},
                    "inventory_empty_slots": 80,
                    "enabled_recipes": {"splitter": True, "underground-belt": True},
                }
                result = self.job.step(obs, {"block": "smelting_district", "resource": "iron-ore"})
                if result.get("status") == "succeeded":
                    break
                self.assertIn(result.get("type"), ("build", "mine"), result)
                actions.append(result)
                if result["type"] == "mine":
                    hit = next(
                        k
                        for k, e in live.items()
                        if e["position"] == result["position"] and e["name"] == result["name"]
                    )
                    self.assertEqual(live[hit]["unit_number"], result["expected_entity_unit"])
                    live.pop(hit)
                else:
                    e = self.built[-1]
                    live[key(e)] = dict(e, unit_number=1000 + len(actions))
        self.assertEqual(result["status"], "succeeded", result)
        # Plan republished before the first action; the board carries the split head.
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"]["smelting_district"], self.smelting.state["plan"])
        self.assertEqual(self.smelting.state["banks"][0]["feed_head"], "split")
        self.assertNotIn("head_rework", self.smelting.state)
        rework = self.job.state["reworks"]["iron-ore"]
        first_mine = next(i for i, a in enumerate(actions) if a["type"] == "mine")
        self.assertEqual(sum(a["type"] == "build" for a in actions[:first_mine]), len(rework["first"]))
        self.assertEqual(sum(a["type"] == "mine" for a in actions), len(rework["remove"]))
        self.assertEqual({key(e) for e in live.values()}, {key(e) for e in self.smelting.state["plan"]["entities"]})


if __name__ == "__main__":
    unittest.main()
