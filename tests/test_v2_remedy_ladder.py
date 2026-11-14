"""The remedy ladder: the failure contract (plan_needs), the resolver (remedy) and the supervisor rule."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factorio_agent import plan_needs, planned_tap, remedy
from factorio_agent.supervisor import REMEDY_CHECK_SECONDS, REMEDY_RECHECK_SECONDS, CLIError

from test_v2_supervisor import Fixture, job

FIXTURE = Path(__file__).parent / "fixtures" / "remedy_eval_1575138435.json"


def cases():
    return {c["job"]["capability"]: c for c in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]}


class NeedsContractTests(unittest.TestCase):
    def test_route_failures_name_both_ends_and_the_change(self):
        result = {
            "status": "blocked",
            "reason": "no_green_science_site_with_clean_connections",
            "failures": [
                {
                    "reason": "no_clean_trunk_corridor",
                    "item": "logistic-science-pack",
                    "source": {"x": 64.5, "y": -117.5},
                    "destination": {"x": 149.5, "y": -66.5},
                }
            ],
        }
        needs = plan_needs.from_result("logistic_science", result)
        self.assertEqual(needs["kind"], "route")
        self.assertEqual((needs["from"], needs["to"]), ([64.5, -117.5], [149.5, -66.5]))
        self.assertIn("(64.5,-117.5)->(149.5,-66.5)", needs["change"])

    def test_tap_failures_mining_banks_corridors_and_preflight_get_their_kind(self):
        tap = plan_needs.from_result(
            "logistic_science",
            {
                "status": "blocked",
                "reason": "no_green_science_site_with_clean_connections",
                "failures": [
                    {
                        "reason": "planned_bus_tap_route_failed",
                        "item": "iron-plate",
                        "side": 1,
                        "failures": [{"reason": "no_clean_trunk_corridor"}] * 3,
                    }
                ],
            },
        )
        self.assertEqual((tap["kind"], tap["item"], tap["tried"]), ("tap", "iron-plate", 3))
        bank = plan_needs.from_result(
            "mining_outpost",
            {
                "status": "blocked",
                "reason": "no_policy_compliant_paired_bank_with_growth_and_power",
                "evidence": {
                    "candidate_failures": ["bank_meets_planned_hardware_of:x"],
                    "exit_sides_tried": ["E", "N"],
                    "needs": "planned_hardware_moved:x",
                },
            },
            {"resource": "iron-ore", "rate_per_minute": 300.0},
        )
        self.assertEqual(
            (bank["kind"], bank["resource"], bank["change"]), ("bank", "iron-ore", "planned_hardware_moved:x")
        )
        corridor = plan_needs.from_result(
            "smelting_district",
            {
                "status": "blocked",
                "reason": "no_clear_pcb_smelting_district_with_corridors",
                "corridor_failures_by_item": {"iron-ore": 4, "coal": 1},
            },
        )
        self.assertEqual((corridor["kind"], corridor["item"]), ("site", "iron-ore"))
        flight = plan_needs.from_result(
            "planned_main_bus",
            {
                "status": "blocked",
                "reason": "planned_bus_preflight_failed",
                "failed_check": "trunk",
                "first_blocker": {"name": "stone-furnace", "position": {"x": 1, "y": 2}},
            },
        )
        self.assertEqual(flight["kind"], "preflight")
        self.assertEqual(flight["blockers"][0], {"entity": "stone-furnace", "position": [1.0, 2.0]})

    def test_a_planner_given_record_wins_and_waits_are_no_planning_failure(self):
        given = plan_needs.from_result(
            "produce",
            {
                "status": "blocked",
                "reason": "x_route_failed",
                "plan_needs": {"kind": "route", "item": "gear", "limit": "turns 7>6"},
            },
        )
        self.assertEqual((given["kind"], given["limit"], given["block"]), ("route", "turns 7>6", "produce"))
        self.assertIsNone(
            plan_needs.from_result("produce", {"status": "blocked", "reason": "production_row_input_source_missing"})
        )
        self.assertIsNone(plan_needs.from_result("power", {"status": "blocked", "reason": "no_minable_resources_left"}))

    def test_signature_is_stable_and_changes_with_the_constraint(self):
        a = plan_needs.make("route", "no_clean_trunk_corridor", item="iron-plate", **{"from": [1, 2], "to": [3, 4]})
        b = plan_needs.make(
            "route", "no_clean_trunk_corridor", item="iron-plate", **{"from": [1, 2], "to": [3, 4]}, tried=9
        )
        c = plan_needs.make("route", "no_clean_trunk_corridor", item="copper-plate", **{"from": [1, 2], "to": [3, 4]})
        self.assertEqual(plan_needs.signature(a), plan_needs.signature(b))
        self.assertNotEqual(plan_needs.signature(a), plan_needs.signature(c))


class PlannerNeedsTests(unittest.TestCase):
    """Each blocking planner's failure shape (as its prepare/step returns it) yields needs of the right kind."""

    SHAPES = {
        # outposts.MiningOutposts.step (bank / extension)
        "mining_outpost": (
            {
                "status": "blocked",
                "reason": "no_policy_compliant_paired_bank_with_growth_and_power",
                "evidence": {
                    "candidate_count": 0,
                    "candidate_failures": [],
                    "exit_sides_tried": ["E"],
                    "attempts": [{"patch": "seed", "side": "E", "reason": "x"}],
                    "needs": "another_patch_or_smaller_row",
                },
            },
            "bank",
        ),
        # material_supply.MaterialSupply.prepare: trunk tap failed, then no new bank
        "dedicated_materials": (
            {
                "status": "blocked",
                "ok": False,
                "reason": "no_policy_compliant_paired_bank_with_growth_and_power",
                "candidate_count": 1,
                "candidate_failures": [],
                "attempted_banks": 1,
                "trunk_tap": {"reason": "planned_bus_tap_unavailable"},
            },
            "bank",
        ),
        # smelting.SmeltingDistrict.prepare
        "smelting_district": (
            {
                "status": "blocked",
                "reason": "no_clear_pcb_smelting_district_with_corridors",
                "legal_sites": 5,
                "corridor_failures_by_item": {"iron-ore": 5},
            },
            "site",
        ),
        # planned_bus.PlannedBus (feeds, trunks)
        "planned_main_bus": (
            {
                "status": "blocked",
                "reason": "planned_bus_feed_route_failed",
                "route_failures": [
                    {
                        "item": "iron-plate",
                        "source": {"x": 1, "y": 1},
                        "destination": {"x": 9, "y": 1},
                        "reason": "no_clean_trunk_corridor",
                    }
                ],
            },
            "route",
        ),
        # production_site.place_line (rows) with a tap that did not route
        "produce": (
            {
                "status": "blocked",
                "reason": "no_production_row_site_with_complete_connections",
                "candidates": 4,
                "failures": [
                    {
                        "allocation": "b1",
                        "reason": "planned_bus_tap_route_failed",
                        "item": "iron-plate",
                        "side": 1,
                        "failures": [],
                    }
                ],
            },
            "tap",
        ),
        # supply_cell.SupplyCell.prepare
        "supply_cell": ({"status": "blocked", "reason": "no_clear_supply_cell_site", "candidates": 3}, "site"),
        # steam_expansion.SteamExpansion.prepare
        "steam_expansion": (
            {
                "status": "blocked",
                "reason": "steam_expansion_layout_unroutable",
                "failures": [{"kind": "pair", "reason": "steam_no_coal_stub_variant_available"}],
            },
            "plan",
        ),
        # planned_furnaces.PlannedFurnaces.prepare (brick / steel)
        "brick_production": (
            {
                "status": "blocked",
                "reason": "no_brick_furnace_block_with_complete_connections",
                "failures": [
                    {"allocation": "b", "reason": "route_port_escape_reserved_by_unbuilt_plan", "item": "stone"}
                ],
            },
            "plan",
        ),
        # trunk_return_feed.TrunkReturnFeed.prepare
        "trunk_return_feed": ({"status": "blocked", "reason": "no_return_feed_site_at_the_trunk_head"}, "site"),
        # green_science.LogisticScience.prepare (taps for the science cluster)
        "logistic_science": (
            {
                "status": "blocked",
                "reason": "no_green_science_site_with_clean_connections",
                "failures": [
                    {
                        "ok": False,
                        "reason": "planned_bus_tap_route_failed",
                        "item": "iron-plate",
                        "side": -1,
                        "failures": [{"reason": "no_clean_trunk_corridor"}],
                    }
                ],
            },
            "tap",
        ),
    }

    def test_every_blocking_planner_returns_needs(self):
        for capability, (result, kind) in self.SHAPES.items():
            with self.subTest(capability=capability):
                needs = plan_needs.from_result(capability, result)
                self.assertEqual(needs["kind"], kind)
                self.assertTrue(needs["change"])
                self.assertTrue(remedy.ladder(capability))
                self.assertIn(capability, remedy.dry_runs())

    def test_the_trunk_tap_failure_is_the_named_change(self):
        result, _ = self.SHAPES["dedicated_materials"]
        self.assertIn("trunk", plan_needs.from_result("dedicated_materials", result)["change"])

    def test_bus_trunk_and_unbuilt_hardware_are_planning_failures(self):
        for reason in (
            "planned_bus_trunk_blocked",
            "furnace_block_meets_planned_hardware",
            "steam_expansion_no_buildable_bank_slot",
        ):
            self.assertTrue(plan_needs.is_planning_failure(reason), reason)


class ResolverTests(unittest.TestCase):
    def test_rungs_come_in_ladder_order_once_per_needs_signature(self):
        needs = plan_needs.make("site", "no_green_science_site_with_clean_connections", block="logistic_science")
        order, tried = [], []
        while True:
            rung, _ = remedy.choose("logistic_science", needs, tried)
            if rung is None:
                break
            order.append(rung.kind)
            tried = remedy.record(tried, rung, needs, {"fits": False}, 0)
        self.assertEqual(order, ["replan", "layout", "site", "relax"])
        self.assertEqual(order, sorted(order, key=remedy.LADDER.index))
        # A different constraint opens the ladder again.
        other = plan_needs.make("route", "no_clean_trunk_corridor", item="iron-plate")
        self.assertEqual(remedy.choose("logistic_science", other, tried)[0].name, "replan")
        self.assertEqual(
            remedy.next_remedy("logistic_science", needs, tried), {"rung": None, "wait_for": needs["change"]}
        )

    def test_rungs_only_meet_their_needs_kinds(self):
        bank = plan_needs.make("bank", "no_policy_compliant_paired_bank_with_growth_and_power", resource="iron-ore")
        tried = remedy.record([], remedy.REPLAN, bank, {"fits": False}, 0)
        self.assertEqual(remedy.choose("mining_outpost", bank, tried)[0].kind, "shrink")
        route = plan_needs.make("route", "no_clean_trunk_corridor")
        tried = remedy.record([], remedy.REPLAN, route, {"fits": False}, 0)
        self.assertIsNone(remedy.choose("mining_outpost", route, tried)[0])

    def test_a_rung_that_would_starve_the_critical_bill_or_costs_more_than_waiting_is_skipped(self):
        costly = remedy.Rung(
            "clear:bridge_pole", "clear", {"clear": "pole"}, cost_seconds=90, items={"small-electric-pole": 2}
        )
        needs = plan_needs.make("route", "no_clean_trunk_corridor")
        tried = remedy.record([], remedy.REPLAN, needs, {"fits": False}, 0)
        with patch.dict(remedy.RELAXATIONS, {"probe": (costly,)}):
            rung, skipped = remedy.choose(
                "probe", needs, tried, claimed={"small-electric-pole": {"units": None, "jobs": ["power"]}}
            )
            self.assertIsNone(rung)
            self.assertIn("critical-path", skipped[0]["why"])
            rung, skipped = remedy.choose("probe", dict(needs, wait_seconds=30), tried)
            self.assertIsNone(rung)
            self.assertIn("cheaper", skipped[0]["why"])
            self.assertEqual(remedy.choose("probe", needs, tried)[0], costly)

    def test_options_are_visible_only_inside_the_remedy(self):
        self.assertEqual(remedy.scaled_count(8), 8)
        with remedy.applied({"count_scale": 0.5, "route_relax": 1}):
            self.assertEqual(remedy.scaled_count(8), 4)
            self.assertEqual(remedy.route_limits()["long_route_turns"], 12)
        with remedy.applied({"count_max": 2}):
            self.assertEqual(remedy.scaled_count(8), 2)
        self.assertIsNone(remedy.route_limits()["long_route_turns"])
        self.assertEqual(
            remedy.merged({"options": {"route_relax": 1}}, remedy.Rung("s", "shrink", {"count_max": 2})),
            {"route_relax": 1, "count_max": 2},
        )

    def test_unknown_outcomes_rollbacks_and_deadlines_never_enter(self):
        base = dict(
            capability="logistic_science", status="blocked", reason="no_green_science_site_with_clean_connections"
        )
        self.assertTrue(remedy.eligible(base))
        self.assertFalse(remedy.eligible(dict(base, pending_action={"type": "build"})))
        self.assertFalse(remedy.eligible(dict(base, reason="action_outcome_unknown_reconcile_required")))
        self.assertFalse(remedy.eligible(dict(base, reason="job_deadline_exceeded")))
        self.assertFalse(remedy.eligible(dict(base, status="waiting")))


class RouteOrderTests(unittest.TestCase):
    def test_a_failed_input_is_planned_first_once(self):
        # eval-1575138435: the copper branch (5 tiles) walled off the iron input; iron first fits both.
        inputs = [{"item": "copper-plate"}, {"item": "iron-plate"}]
        seen = []

        def fake(*args, inputs, **kwargs):
            seen.append([p["item"] for p in inputs])
            if inputs[0]["item"] == "copper-plate":
                return {"ok": False, "reason": "planned_bus_tap_route_failed", "item": "iron-plate"}
            return {"ok": True, "routes": [], "taps": [], "entities": [], "ignored_units": [], "sources": {}}

        with patch.object(planned_tap, "_connect_in_order", fake):
            found = planned_tap.connect_inputs(
                None, None, {}, {}, owner="o", allocation=None, inputs=inputs, entities=[], demand={}, span=4
            )
        self.assertTrue(found["ok"])
        self.assertEqual(seen, [["copper-plate", "iron-plate"], ["iron-plate", "copper-plate"]])
        self.assertEqual(found["route_order"], ["iron-plate", "copper-plate"])

    def test_the_order_search_is_bounded(self):
        calls = []

        def fake(*args, inputs, **kwargs):
            calls.append(1)
            return {"ok": False, "reason": "planned_bus_tap_route_failed", "item": inputs[-1]["item"]}

        with patch.object(planned_tap, "_connect_in_order", fake):
            found = planned_tap.connect_inputs(
                None,
                None,
                {},
                {},
                owner="o",
                allocation=None,
                inputs=[{"item": "a"}, {"item": "b"}, {"item": "c"}],
                entities=[],
                demand={},
                span=4,
            )
        self.assertFalse(found["ok"])
        self.assertLessEqual(len(calls), 4)
        self.assertIn("orders_tried", found)


class LadderCLI:
    """FakeCLI extension: ``what-if`` answers from a script, ``retry --remedy`` is recorded."""

    def __init__(self, cli, answers):
        self.cli, self.answers = cli, answers
        self.what_ifs = []
        original = cli.invoke

        def invoke(args, **kwargs):
            if args[0] == "what-if":
                rung = args[args.index("--rung") + 1]
                self.what_ifs.append(rung)
                answer = self.answers(rung, json.loads(args[args.index("--options") + 1]))
                if isinstance(answer, Exception):
                    raise answer
                return dict(answer, check={"rung": rung, "signature": self.signature(args), "fits": answer.get("fits")})
            if args[0] == "retry" and "--remedy" in args:
                rung = json.loads(args[args.index("--remedy") + 1])
                job = cli.jobs[args[args.index("--key") + 1]]
                result = original(args[:3], **kwargs)
                cli.calls[-1] = list(args)
                job["remedy"] = rung
                return result
            return original(args, **kwargs)

        cli.invoke = invoke

    def signature(self, args):
        key = args[args.index("--key") + 1]
        return plan_needs.signature(remedy.needs_of(self.cli.jobs[key]))


class LadderFixture(Fixture):
    def blocked(self, key, capability, reason, evidence=None, parameters=None):
        self.cli.jobs[key] = job(
            key,
            capability,
            status="blocked",
            reason=reason,
            evidence=evidence or {},
            blocked_at_time=self.clock(),
            **({} if parameters is None else {}),
        )
        if parameters is not None:
            self.cli.jobs[key]["parameters"] = parameters
        self.s.work.data["nodes"].append(
            dict(
                id="auto_" + key,
                capability=capability,
                parameters=deepcopy(self.cli.jobs[key]["parameters"]),
                depends_on=[],
                priority=0,
                job_key=key,
                status="blocked",
                submitted=True,
                created_at=0,
            )
        )

    def retries(self):
        return [c for c in self.cli.calls if c[0] == "retry"]


class SupervisorLadderTests(LadderFixture):
    def test_the_first_fitting_rung_is_applied_by_a_retry_one_check_per_cycle(self):
        self.s.max_seconds = None
        ladder = LadderCLI(
            self.cli,
            lambda rung, options: {
                "fits": rung == "layout:next_form",
                "reason": "plan" if rung == "layout:next_form" else "no",
            },
        )
        self.blocked("green", "logistic_science", "no_green_science_site_with_clean_connections")
        self.assertFalse(self.s._auto_retry(self.s._snapshot()))  # the block itself was the plain re-plan
        self.assertEqual(ladder.what_ifs, [])
        self.clock.advance(REMEDY_CHECK_SECONDS + 1)
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual(ladder.what_ifs, ["layout:next_form"])
        retry = self.retries()[-1]
        self.assertEqual(json.loads(retry[retry.index("--remedy") + 1])["options"], {"layout_rotate": 1})
        node = self.s.work.node_for_job("green")
        self.assertEqual(node["needs"]["kind"], "site")
        self.assertEqual(node["retry_plan"]["next_remedy"]["rung"], "site:longer_connection")

    def test_an_executor_failure_is_replanned_first_by_the_ladder(self):
        # eval-1575138435: oil_gathering blocked repeated_executor_failure:position_blocked (no needs then).
        self.s.max_seconds = None
        ladder = LadderCLI(self.cli, lambda rung, options: {"fits": True, "reason": "plan"})
        self.blocked("oil", "oil_gathering", "repeated_executor_failure:position_blocked")
        self.assertFalse(self.s._auto_retry(self.s._snapshot()))
        self.clock.advance(REMEDY_CHECK_SECONDS + 1)
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))
        self.assertEqual(ladder.what_ifs, ["replan"])
        self.assertEqual(json.loads(self.retries()[-1][-1])["rung"], "replan")

    def test_a_machine_only_item_no_machine_makes_gets_its_producer_proposed(self):
        import test_v2_supervisor

        caps = dict(test_v2_supervisor.CAPS, produce=dict(test_v2_supervisor.CAPS["power"]))
        patcher = patch.object(test_v2_supervisor, "CAPS", caps)
        patcher.start()
        self.addCleanup(patcher.stop)
        producer = {"capability": "produce", "parameters": {"item": "iron-gear-wheel"}, "recipe": "iron-gear-wheel"}
        self.blocked(
            "eng",
            "engine_production",
            "required item needs a production machine",
            evidence={"item": "iron-gear-wheel", "producer": producer},
        )
        self.assertTrue(self.s._missing_producers(self.s._snapshot()))
        node = next(n for n in self.s.work.data["nodes"] if n["id"].startswith("auto_producer_"))
        self.assertEqual(
            (node["capability"], node["parameters"]), ("produce", {"item": "iron-gear-wheel", "rate_per_minute": 6})
        )
        self.assertFalse(self.s._missing_producers(self.s._snapshot()))  # proposed once
        self.cli.jobs["eng"]["evidence"]["producer"] = {"capability": "furnace_block", "parameters": {"item": "x"}}
        self.assertFalse(self.s._missing_producers(self.s._snapshot()))  # unregistered: logged, not queued

    def test_a_spent_ladder_waits_for_new_facts_or_the_recheck_period(self):
        self.s.max_seconds = None
        ladder = LadderCLI(self.cli, lambda rung, options: {"fits": False, "reason": "no"})
        self.cli.jobs["busy"] = job("busy", "power", status="running")  # the run is not idle
        self.blocked(
            "mine",
            "mining_outpost",
            "no_policy_compliant_paired_bank_with_growth_and_power",
            {"candidate_failures": [], "exit_sides_tried": ["E", "N", "S", "W"]},
            {"resource": "iron-ore", "rate_per_minute": 300.0},
        )
        for _ in range(6):
            self.clock.advance(REMEDY_CHECK_SECONDS + 1)
            self.s._auto_retry(self.s._snapshot())
        self.assertEqual(ladder.what_ifs, ["shrink:half_drills", "shrink:two_drills"])
        plan = self.s.work.node_for_job("mine")["retry_plan"]
        self.assertIsNone(plan["rung"])
        self.assertIn("bank anchor", plan["wait_for"])
        self.assertEqual(self.retries(), [])
        self.clock.advance(REMEDY_RECHECK_SECONDS)
        self.s._auto_retry(self.s._snapshot())
        self.assertEqual(ladder.what_ifs[-1], "replan")

    def test_an_idle_run_replans_a_spent_ladder_sooner(self):
        # aud-1114-0436 seed 3242269094: the district re-planned every 5 min while nothing else ran (25 min idle).
        from factorio_agent.supervisor import REMEDY_IDLE_RECHECK_SECONDS

        self.s.max_seconds = None
        ladder = LadderCLI(self.cli, lambda rung, options: {"fits": False, "reason": "no"})
        self.blocked(
            "mine",
            "mining_outpost",
            "no_policy_compliant_paired_bank_with_growth_and_power",
            {"candidate_failures": [], "exit_sides_tried": ["E", "N", "S", "W"]},
            {"resource": "iron-ore", "rate_per_minute": 300.0},
        )
        for _ in range(3):
            self.clock.advance(REMEDY_CHECK_SECONDS + 1)
            self.s._auto_retry(self.s._snapshot())
        self.assertEqual(ladder.what_ifs, ["shrink:half_drills", "shrink:two_drills"])
        self.clock.advance(REMEDY_IDLE_RECHECK_SECONDS)
        self.s._auto_retry(self.s._snapshot())
        self.assertEqual(ladder.what_ifs[-1], "replan")
        self.assertLess(REMEDY_IDLE_RECHECK_SECONDS + REMEDY_CHECK_SECONDS, REMEDY_RECHECK_SECONDS)

    def test_a_deferred_retry_keeps_the_confirmed_rung_and_an_error_is_no_fit(self):
        self.s.max_seconds = None
        answers = iter([CLIError("what-if broke"), {"fits": True, "reason": "plan"}])
        ladder = LadderCLI(self.cli, lambda rung, options: next(answers))
        self.blocked(
            "coal",
            "mining_outpost",
            "no_policy_compliant_paired_bank_with_growth_and_power",
            {"attempted_banks": 1, "candidate_count": 1},
            {"resource": "coal", "rate_per_minute": 60.0},
        )
        self.s._auto_retry(self.s._snapshot())  # the block itself was the plain re-plan
        self.clock.advance(REMEDY_CHECK_SECONDS + 1)
        self.s._auto_retry(self.s._snapshot())  # error: fits None -> applied by a retry like before
        self.assertEqual(len(self.retries()), 1)
        self.cli.jobs["coal"].update(
            status="blocked",
            reason="no_policy_compliant_paired_bank_with_growth_and_power",
            blocked_at_time=self.clock(),
        )
        self.cli.jobs["busy"] = job("busy", "mining_outpost", status="waiting")
        self.s._auto_retry(self.s._snapshot())  # the new block is seen
        self.clock.advance(REMEDY_CHECK_SECONDS + 1)
        refused = self.cli.invoke

        def slot(args, **kwargs):
            if args[0] == "retry":
                raise CLIError("mining_outpost is already active")
            return refused(args, **kwargs)

        self.cli.invoke = slot
        self.s._auto_retry(self.s._snapshot())
        self.assertEqual(len(ladder.what_ifs), 2)
        self.cli.invoke = refused
        del self.cli.jobs["busy"]
        self.assertTrue(self.s._auto_retry(self.s._snapshot()))  # no new what-if: the confirmed rung retries
        self.assertEqual(len(ladder.what_ifs), 2)

    def test_unknown_outcomes_and_waits_stay_with_the_old_rules(self):
        LadderCLI(self.cli, lambda rung, options: {"fits": True})
        self.blocked("lost", "logistic_science", "action_outcome_unknown_reconcile_required")
        self.cli.jobs["lost"]["pending_action"] = {"type": "build"}
        self.clock.advance(REMEDY_RECHECK_SECONDS * 3)
        self.s._auto_retry(self.s._snapshot())
        self.assertEqual(self.retries(), [])


class LiveCaseTests(LadderFixture):
    """eval-1575138435 (75 game min): three jobs blocked 25+ minutes; the post-mortem dry run fitted all three."""

    def test_recorded_evidence_names_the_constraint(self):
        found = cases()
        green = found["logistic_science"]
        failed = [r for r in green["routes"] if r["ok"] is False]
        self.assertEqual(failed[0]["item"], "iron-plate")
        self.assertEqual(
            (failed[0]["source"], failed[0]["destination"]), ({"x": 80.5, "y": -95.5}, {"x": 77.5, "y": -90.5})
        )
        # The copper tap was routed first and is 5 tiles long; every iron tap row then had no corridor.
        self.assertEqual(green["routes"][0]["item"], "copper-plate")
        self.assertEqual(green["routes"][0]["route_metrics"]["length"], 5)
        self.assertTrue(green["routes"][-1]["ok"])  # the third site fitted: a plain re-plan resolves it
        mine = found["mining_outpost"]
        self.assertEqual([a["side"] for a in mine["bank_attempts"]], ["E", "N"])
        self.assertEqual([a["ok"] for a in mine["bank_attempts"]], [False, True])
        self.assertEqual(mine["dry_run_relaxed"], "single_pair_no_growth")
        self.assertTrue(found["dedicated_materials"]["dry_run_ok"])

    def test_each_live_job_is_resolved_by_the_replan_rung(self):
        self.s.max_seconds = None
        ladder = LadderCLI(self.cli, lambda rung, options: {"fits": rung == "replan", "reason": "plan"})
        for name, case in cases().items():
            row = case["job"]
            self.blocked(name, row["capability"], row["reason"], row["evidence"], row["parameters"])
            self.cli.jobs[name]["blocked_at_time"] = self.clock() - 1500  # blocked 25 min before this supervisor
        # A supervisor started after the block: no remedy history, the needs derived from the ledger row.
        for name in cases():
            self.s.__dict__.setdefault("_remedy", {})[name] = {
                "tried": [],
                "last": 0.0,
                "facts": None,
                "checks": 0,
                "blocked_at": self.cli.jobs[name]["blocked_at_time"],
            }
        for _ in range(3):
            self.s._auto_retry(self.s._snapshot())
        self.assertEqual(ladder.what_ifs, ["replan", "replan", "replan"])
        self.assertEqual(sorted(c[2] for c in self.retries()), sorted(cases()))
        self.assertTrue(all(json.loads(c[c.index("--remedy") + 1])["rung"] == "replan" for c in self.retries()))


class WhatIfTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pcb-board.json").write_text("{}", encoding="utf-8")
        from test_v2_postmortem import FakeLive

        self.live = FakeLive(self.root)

    def run_with(self, prepare, options):
        from types import SimpleNamespace

        bus = SimpleNamespace(prepare=prepare)
        job = dict(
            capability="planned_main_bus", parameters={}, status="blocked", reason="planned_bus_feed_route_failed"
        )
        return remedy.what_if(
            self.root,
            self.live,
            None,
            job,
            options,
            executors_factory=lambda game, catalog: SimpleNamespace(planned_bus=bus),
        )

    def test_the_planner_sees_the_rung_options_and_the_run_is_untouched(self):
        def prepare(obs):
            (self.root / "x").exists()
            return (
                {"ok": True}
                if remedy.option("route_relax") == 1
                else {
                    "ok": False,
                    "reason": "planned_bus_feed_route_failed",
                    "route_failures": [{"item": "iron-plate", "reason": "no_clean_trunk_corridor"}],
                }
            )

        before = sorted(p.name for p in self.root.iterdir())
        no = self.run_with(prepare, {})
        self.assertIs(no["fits"], False)
        self.assertEqual(no["needs"]["kind"], "route")
        yes = self.run_with(prepare, {"route_relax": 1})
        self.assertIs(yes["fits"], True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertEqual(self.live.writes, [])

    def saved_bus(self, step):
        """aud-1114-0614 seed 375147794: the bus blocked in its build phase; a fresh prepare finds
        its own half-built trunk in the way, the saved plan continues on retry."""
        from types import SimpleNamespace

        state = {"world_id": "w1", "phase": "build", "ok": True, "plan": {"entities": []}}
        calls = []

        def prepare(obs):
            calls.append("prepare")
            return {"ok": False, "reason": "planned_bus_trunk_blocked", "item": "iron-plate"}

        def stepper(obs, parameters=None):
            calls.append("step")
            return step(obs)

        bus = SimpleNamespace(prepare=prepare, step=stepper, state=state)
        job = dict(capability="planned_main_bus", parameters={}, status="blocked", reason="x")
        factory = lambda game, catalog: SimpleNamespace(planned_bus=bus)  # noqa: E731
        return remedy.what_if(self.root, self.live, None, job, {}, executors_factory=factory), calls, state

    def test_a_saved_build_answers_with_its_next_step_not_a_fresh_prepare(self):
        build = {"type": "build", "name": "transport-belt", "position": {"x": -82.5, "y": 56.5}}
        out, calls, _ = self.saved_bus(lambda obs: build)
        self.assertIs(out["fits"], True)
        self.assertEqual(out["reason"], "saved_state_continues:build")
        self.assertEqual(calls, ["step"])
        self.assertEqual(self.live.writes, [])

    def test_a_saved_build_whose_next_step_is_blocked_does_not_fit(self):
        blocked = {
            "status": "blocked",
            "reason": "reserved block placement is obstructed",
            "evidence": {"entity": {"name": "transport-belt", "position": {"x": -82.5, "y": 52.5}}},
        }
        out, calls, _ = self.saved_bus(lambda obs: blocked)
        self.assertIs(out["fits"], False)
        self.assertEqual(out["reason"], "reserved block placement is obstructed")
        self.assertIn("needs", out)
        self.assertEqual(calls, ["step"])

    def test_a_half_built_steam_stage_continues_its_saved_plan(self):
        # eval-1575138435 stage 3: one pipe tile was obstructed for a while; every ladder re-plan
        # from scratch found no bank slot (its own half-built bank in the way) and never resumed.
        from types import SimpleNamespace

        calls = []
        stage = SimpleNamespace(
            state={"world_id": "w1", "ok": True, "plan": {"entities": []}},
            prepare=lambda obs: (
                calls.append("prepare") or {"ok": False, "reason": "steam_expansion_no_buildable_bank_slot"}
            ),
            step=lambda obs: calls.append("step") or {"type": "build", "name": "pipe"},
            proof=lambda obs: None,
        )
        job = dict(capability="steam_expansion", parameters={"stage": 3}, status="blocked", reason="x")
        factory = lambda game, catalog: SimpleNamespace(_steam_stage=lambda p: stage)  # noqa: E731
        out = remedy.what_if(self.root, self.live, None, job, {}, executors_factory=factory)
        self.assertIs(out["fits"], True)
        self.assertEqual(calls, ["step"])
        stage.proof = lambda obs: {"ok": True}  # a proven stage is no build in progress
        calls.clear()
        out = remedy.what_if(self.root, self.live, None, job, {}, executors_factory=factory)
        self.assertEqual(calls, ["prepare"])

    def test_a_finished_or_foreign_saved_state_plans_afresh(self):
        for phase, world in (("verified", "w1"), ("build", "other-world")):
            from types import SimpleNamespace

            calls = []
            bus = SimpleNamespace(
                prepare=lambda obs: calls.append("prepare") or {"ok": True},
                step=lambda obs, p=None: calls.append("step") or {"type": "build"},
                state={"world_id": world, "phase": phase},
            )
            job = dict(capability="planned_main_bus", parameters={}, status="blocked", reason="x")
            out = remedy.what_if(
                self.root, self.live, None, job, {}, executors_factory=lambda g, c: SimpleNamespace(planned_bus=bus)
            )
            self.assertIs(out["fits"], True)
            self.assertEqual(calls, ["prepare"])

    def test_every_controller_with_a_saved_state_reader_has_a_step(self):
        from factorio_agent import postmortem

        for capability, spec in postmortem.DRY_RUN.items():
            if spec.saved is not None:
                self.assertIsNotNone(spec.step, capability)

    def test_a_planner_that_would_act_has_no_answer(self):
        def prepare(obs):
            return remedy_game[0].act({"type": "build"})

        remedy_game = []
        from types import SimpleNamespace

        def factory(game, catalog):
            remedy_game.append(game)
            return SimpleNamespace(planned_bus=SimpleNamespace(prepare=prepare))

        job = dict(
            capability="planned_main_bus", parameters={}, status="blocked", reason="planned_bus_feed_route_failed"
        )
        out = remedy.what_if(self.root, self.live, None, job, {}, executors_factory=factory)
        self.assertIsNone(out["fits"])
        self.assertIn("read_only_violation", out["reason"])
        self.assertEqual(self.live.writes, [])


class LedgerTests(unittest.TestCase):
    def test_retry_with_a_remedy_records_it_and_note_remedy_writes_only_ladder_fields(self):
        from factorio_agent import jobs

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = {
                "schema_version": 2,
                "world_id": "w",
                "last_tick": 100,
                "jobs": {
                    "k": dict(
                        key="k",
                        world_id="w",
                        capability="logistic_science",
                        parameters={"rate_per_minute": 30},
                        status="blocked",
                        reason="no_green_science_site_with_clean_connections",
                        blocked_at_tick=90,
                        blocked_at_time=1.0,
                        created_tick=1,
                        last_tick=90,
                        progress_at_tick=90,
                        progress_at_time=1.0,
                        needs={"kind": "site"},
                    )
                },
            }
            (root / "jobs.json").write_text(json.dumps(ledger), encoding="utf-8")
            (root / "job-events.jsonl").write_text("", encoding="utf-8")
            jobs.note_remedy(
                root, "k", check={"rung": "replan", "fits": False}, next_remedy={"rung": "layout:next_form"}
            )

            class Game:
                def observe(self):
                    return {"ok": True, "world_id": "w", "tick": 120}

            with patch.object(jobs.Ledger, "sync", lambda self, obs: None):
                jobs.control(
                    root,
                    Game(),
                    "k",
                    "retry",
                    remedy_rung={"rung": "layout:next_form", "kind": "layout", "options": {"layout_rotate": 1}},
                )
            row = json.loads((root / "jobs.json").read_text(encoding="utf-8"))["jobs"]["k"]
            self.assertEqual(row["remedy"]["options"], {"layout_rotate": 1})
            self.assertEqual(row["remedies_tried"][0]["rung"], "replan")
            self.assertEqual(row["next_remedy"], {"rung": "layout:next_form"})
            events = [json.loads(line)["event"] for line in (root / "job-events.jsonl").read_text().splitlines()]
            self.assertIn("remedy_checked", events)
            self.assertIn("remedy_applied", events)
            with self.assertRaises(ValueError):
                jobs.control(root, Game(), "k", "pause", remedy_rung={"options": {}})


class JobRunnerTests(unittest.TestCase):
    def test_a_blocked_planning_failure_stores_needs_and_the_retry_plans_with_the_rung(self):
        from factorio_agent.jobs import Ledger, control, run_slice
        from test_v2_concurrent_jobs import ConcurrentJobTests, Executor

        harness = ConcurrentJobTests("test_two_jobs_can_be_active_together")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.submit("power", "power")
        failure = {
            "status": "blocked",
            "reason": "no_clear_power_site",
            "failures": [
                {
                    "reason": "no_clean_trunk_corridor",
                    "item": "coal",
                    "source": {"x": 1, "y": 1},
                    "destination": {"x": 5, "y": 1},
                }
            ],
        }
        seen = []

        class Recording(Executor):
            def step(self, capability, obs, parameters=None):
                seen.append(remedy.option("route_relax"))
                return super().step(capability, obs, parameters)

        run_slice(harness.root, harness.game, Recording({"power": [failure]}), "power")
        row = Ledger(harness.root).data["jobs"]["power"]
        self.assertEqual((row["status"], row["needs"]["kind"]), ("blocked", "route"))
        terminal = [json.loads(line) for line in (harness.root / "job-events.jsonl").read_text().splitlines()]
        self.assertEqual(terminal[-1]["needs"]["kind"], "route")
        control(
            harness.root,
            harness.game,
            "power",
            "retry",
            remedy_rung={"rung": "site:longer_connection", "kind": "site", "options": {"route_relax": 1}},
        )
        run_slice(harness.root, harness.game, Recording({"power": [failure]}), "power")
        self.assertEqual(seen, [None, 1])
        self.assertEqual(Ledger(harness.root).data["jobs"]["power"]["remedy"]["rung"], "site:longer_connection")


class ExecutorFailureTests(unittest.TestCase):
    """eval-1575138435, 320 min: oil_gathering's scout move onto deep water failed position_blocked
    three times; the job blocked repeated_executor_failure with no needs, off the ladder, retried blind."""

    MOVE = {"type": "move", "position": {"x": 752.5, "y": -175.5}, "reason": "scout beside ungenerated chunk 24,-6"}

    def test_the_failing_action_and_position_are_preflight_needs(self):
        needs = plan_needs.from_result(
            "oil_gathering",
            {"status": "blocked", "reason": "repeated_executor_failure:position_blocked", "executor_action": self.MOVE},
        )
        self.assertEqual((needs["kind"], needs["check"], needs["at"]), ("preflight", "executor:move", [752.5, -175.5]))
        self.assertEqual(
            needs["blockers"], [{"entity": "move", "position": [752.5, -175.5], "reason": "position_blocked"}]
        )
        self.assertIn("does not fail position_blocked", needs["change"])
        # A job blocked before needs existed resolves from its reason alone.
        old = {
            "capability": "oil_gathering",
            "status": "blocked",
            "reason": "repeated_executor_failure:position_blocked",
        }
        self.assertEqual(remedy.needs_of(old)["kind"], "plan")
        self.assertTrue(remedy.eligible(old))
        rung, _ = remedy.choose("oil_gathering", needs)
        self.assertEqual(rung.name, "replan")

    def test_the_job_runner_stores_the_needs_and_the_supervisor_replans_it(self):
        from factorio_agent.jobs import Ledger, run_slice
        from factorio_agent.supervisor import _replannable
        from test_v2_concurrent_jobs import ConcurrentJobTests, Executor

        harness = ConcurrentJobTests("test_two_jobs_can_be_active_together")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.submit("oil", "power")
        harness.game.act = lambda action: {"ok": False, "reason": "position_blocked"}
        run_slice(harness.root, harness.game, Executor({"power": [dict(self.MOVE)]}), "oil")
        row = Ledger(harness.root).data["jobs"]["oil"]
        self.assertEqual(row["reason"], "repeated_executor_failure:position_blocked")
        self.assertEqual((row["needs"]["kind"], row["needs"]["at"]), ("preflight", [752.5, -175.5]))
        self.assertTrue(_replannable(row["reason"]))


class WasteReportTests(unittest.TestCase):
    def test_blocked_seconds_per_needs_kind_and_resolved_rungs(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "waste_report_t", Path(__file__).parents[1] / "tools" / "waste_report.py"
        )
        waste = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(waste)
        events = [
            {
                "key": "g",
                "event": "terminal",
                "status": "blocked",
                "reason": "no_x",
                "tick": 600,
                "needs": {"kind": "route"},
            },
            {"key": "g", "event": "remedy_checked", "rung": "relax:route_turns_max", "fits": True, "tick": 1200},
            {"key": "g", "event": "remedy_applied", "rung": "relax:route_turns_max", "tick": 1200},
            {"key": "g", "event": "retry", "tick": 1200},
            {"key": "g", "event": "terminal", "status": "succeeded", "tick": 3000},
            {
                "key": "m",
                "event": "terminal",
                "status": "blocked",
                "reason": "no_bank",
                "tick": 600,
                "needs": {"kind": "bank"},
            },
        ]
        out = waste.remedy_summary(events, 6600)
        self.assertEqual(out["blocked_by_needs_kind"]["route"], {"seconds": 10.0, "blocks": 1})
        self.assertEqual(out["blocked_by_needs_kind"]["bank"], {"seconds": 100.0, "blocks": 1})
        self.assertEqual(
            out["rungs"]["relax:route_turns_max"], {"checked": 1, "fitted": 1, "applied": 1, "resolved": 1}
        )


class CoverageTests(unittest.TestCase):
    """Sweep 2026-11-14: rungs without a pure prepare never reach the ladder."""

    def test_every_capability_with_rungs_has_a_dry_run(self):
        from factorio_agent import postmortem

        # REPAIRABLE blocks repair a live fault (built_block_repair): no pure planner needed.
        self.assertEqual(sorted(set(remedy.RELAXATIONS) - set(postmortem.DRY_RUN) - remedy.REPAIRABLE), [])
        for capability in ("steel_supply", "engine_production", "ammo_production"):
            self.assertIn(capability, postmortem.DRY_RUN)

    def test_later_site_searches_are_replanned_and_have_needs(self):
        from factorio_agent.supervisor import _replannable

        for reason in (
            "no_engine_site_with_complete_native_material_connections",
            "no_permanent_chemical_site_with_complete_typed_inputs",
            "no_chemical_science_site_with_complete_owned_lab_connection",
            "no_advanced_circuit_site_clear_of_reserved_and_unbuilt_external_plans",
            "no_isolated_two_outlet_gas_manifold",
        ):
            self.assertTrue(_replannable(reason), reason)
        self.assertTrue(plan_needs.is_planning_failure("industrial_planning_budget_exhausted"))

    def test_a_planner_fault_is_planned_again_without_another_success(self):
        from factorio_agent.supervisor import _replannable

        self.assertTrue(_replannable("planner_fault:ValueError:conflicting shared port configuration"))

    # Fleet long-1114-0612: the reasons the 4 runs' blocked jobs held from game minute 29-90 to ~240.
    LONG_FLEET_END_STATE = {
        "no_steel_furnace_block_with_complete_connections": "site",
        "no_brick_furnace_block_with_complete_connections": "site",
        "objective_progress_stalled": "other",
        "job_deadline_exceeded": "other",
        "replacement_electric_drill_has_no_power": "power",
        "electric_upgrade_footprint_obstructed": "site",
        "planned_bus_feed_route_failed": "route",
        "mine_trunk_tap_unavailable": "route",
    }

    def test_no_reason_of_the_long_fleet_end_state_is_terminal(self):
        from factorio_agent.supervisor import blocked_category, permanent_block

        for reason, category in self.LONG_FLEET_END_STATE.items():
            self.assertEqual(blocked_category(reason), category, reason)
            self.assertFalse(permanent_block(reason), reason)
        self.assertEqual(blocked_category("action_outcome_unknown_reconcile_required"), "unknown_outcome")
        self.assertEqual(blocked_category("world_rollback_requires_reconciliation"), "unknown_outcome")
        self.assertEqual(blocked_category("some_reason_nobody_named_yet"), "other")
        self.assertFalse(permanent_block("some_reason_nobody_named_yet"))
        self.assertTrue(permanent_block("supply_cell_retired"))


class LongFleetEndStateTests(LadderFixture):
    """Every long-1114-0612 end-state reason of a job off the ladder is retried past the quick retries."""

    def test_each_end_state_reason_is_retried_on_the_backoff_past_the_quick_retries(self):
        from factorio_agent.supervisor import AUTO_RETRY_LIMIT, BACKOFF_MAX_MINUTES

        self.s.max_seconds = None
        self.cli.jobs["busy"] = job("busy", "power", status="running")  # the run is not idle
        reasons = CoverageTests.LONG_FLEET_END_STATE
        for index, reason in enumerate(reasons):
            key = "j" + str(index)
            self.blocked(key, "smelting_capacity", reason)
            self.cli.jobs[key]["blocked_at_tick"] = 0
            self.s.__dict__.setdefault("_auto_retries", {})[key] = {"successes": 0, "count": AUTO_RETRY_LIMIT}
        for _ in range(len(reasons) + 1):
            self.cli.tick += BACKOFF_MAX_MINUTES * 3600
            self.s._auto_retry(self.s._snapshot())
        self.assertEqual(sorted(c[2] for c in self.retries()), sorted("j" + str(i) for i in range(len(reasons))))


if __name__ == "__main__":
    unittest.main()
