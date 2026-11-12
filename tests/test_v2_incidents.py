from dataclasses import dataclass, field
import json
import unittest

from factorio_agent.incidents import (
    DOCUMENT_CHAR_CAP,
    TRUNCATED,
    IncidentConfig,
    IncidentTracker,
    Inputs,
    addable_capabilities,
    check_decision,
    document,
    factory_status,
    repeat_summary,
    roadmap_position,
)


@dataclass(frozen=True)
class Step:
    id: str
    capability: str
    parameters: dict = field(default_factory=dict)
    after: tuple = ()


CAPS = {
    "starter": {"parameters": {}, "requires": []},
    "power": {"parameters": {}, "requires": ["starter"]},
    "research": {"parameters": {"technology": "name"}, "requires": ["power"]},
    "smelting": {"parameters": {"item": "iron-plate"}, "requires": ["starter"]},
}
ROADMAP = (
    Step("starter", "starter"),
    Step("power", "power"),
    Step("research_automation", "research", {"technology": "automation"}, ("power",)),
)


def job(key, capability="starter", status="waiting", **extra):
    return dict(key=key, capability=capability, parameters={}, status=status, reason="r", **extra)


def node(name, key, capability="starter", status="pending", **extra):
    return dict(id=name, job_key=key, capability=capability, parameters={}, status=status, depends_on=[], **extra)


class Harness:
    def __init__(self, config=None):
        self.tracker = IncidentTracker(config or IncidentConfig(), started_at=0)
        self.jobs = []
        self.nodes = []
        self.observation = {"technologies": []}
        self.intents = []
        self.intent_reads = 0
        self.roadmap = ROADMAP

    def read(self):
        self.intent_reads += 1
        return list(self.intents)

    def at(self, now):
        return self.tracker.update(
            Inputs(
                now=now,
                jobs=self.jobs,
                nodes=self.nodes,
                observation=self.observation,
                capabilities=CAPS,
                intents=self.read,
                roadmap=self.roadmap,
            )
        )

    def kinds(self):
        return sorted(q.kind for q in self.tracker.open.values())


class BlockedJobTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.jobs = [job("s", status="succeeded"), job("b", "power", "blocked"), job("w", "smelting")]
        self.h.nodes = [node("n_b", "b", "power", "blocked"), node("n_w", "w", "smelting", "running")]

    def test_opens_after_the_grace_period_and_closes_resolved_when_the_job_leaves_blocked(self):
        self.assertEqual(self.h.at(0).opened, [])
        self.assertEqual(self.h.at(59).opened, [])
        opened = self.h.at(60).opened
        self.assertEqual([(q.kind, q.subject, q.since) for q in opened], [("blocked_job", "b", 0)])
        self.assertEqual(self.h.at(70).opened, [])
        self.h.jobs[1]["status"] = "waiting"
        closed = self.h.at(95).closed
        self.assertEqual(
            [(q.kind, outcome, seconds) for q, outcome, seconds in closed], [("blocked_job", "resolved", 35)]
        )

    def test_auto_retry_restarts_the_grace_timer(self):
        self.h.at(0)
        self.h.jobs[1]["status"] = "waiting"
        self.h.at(50)
        self.h.jobs[1]["status"] = "blocked"
        self.h.at(55)
        self.assertEqual(self.h.at(100).opened, [])
        self.assertEqual(len(self.h.at(115).opened), 1)

    def test_still_open_after_outcome_minutes_expires_and_a_new_episode_opens(self):
        self.h.at(0)
        first = self.h.at(60).opened[0]
        changes = self.h.at(660)
        self.assertEqual([outcome for _, outcome, _ in changes.closed], ["expired"])
        self.assertEqual([q.id for q in changes.opened], [first.id])
        self.assertEqual(changes.opened[0].opened_at, 660)

    def test_facts_and_menu(self):
        self.h.nodes.append(dict(node("n_dep", "x", "research"), depends_on=["n_b"]))
        self.h.at(0)
        incident = self.h.at(60).opened[0]
        facts = incident.facts
        self.assertEqual((facts["capability"], facts["task_id"], facts["auto_retry_eligible"]), ("power", "n_b", True))
        self.assertEqual(facts["dependent_task_ids"], ["n_dep"])
        menu = [r.to_dict() for r in incident.remedies]
        self.assertEqual([r["remedy"] for r in menu], ["retry", "add_nodes", "wait"])
        self.assertEqual(menu[0]["target_task_id"], "n_b")
        self.assertEqual(sorted(menu[1]["capabilities"]), ["power", "smelting", "starter"])
        self.assertEqual(menu[1]["max_nodes"], 3)

    def test_unknown_outcome_has_no_retry_remedy(self):
        self.h.jobs[1]["pending_action"] = {"type": "build"}
        self.h.at(0)
        incident = self.h.at(60).opened[0]
        self.assertTrue(incident.facts["unknown_outcome"])
        self.assertNotIn("retry", [r.kind for r in incident.remedies])


class EmptyQueueTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.jobs = [job("s", status="succeeded")]

    def test_opens_after_idle_grace_with_roadmap_position_and_closes_when_work_arrives(self):
        self.h.at(0)
        self.assertEqual(self.h.at(59).opened, [])
        incident = self.h.at(60).opened[0]
        self.assertEqual((incident.kind, incident.subject), ("empty_queue", "queue"))
        roadmap = incident.facts["roadmap"]
        self.assertEqual([r["step"] for r in roadmap], ["power", "research_automation"])
        self.assertTrue(roadmap[0]["ready"])
        self.assertIn("requires power", roadmap[1]["waiting_on"])
        self.assertIn("after power", roadmap[1]["waiting_on"])
        self.h.nodes = [node("n", "k", "power", "ready")]
        self.assertEqual([o for _, o, _ in self.h.at(70).closed], ["resolved"])

    def test_finished_roadmap_or_active_work_is_not_an_incident(self):
        self.h.jobs += [job("p", "power", "succeeded")]
        self.h.observation = {"technologies": ["automation"]}
        self.h.at(0)
        self.assertEqual(self.h.at(600).opened, [])
        self.h.observation = {"technologies": []}
        self.h.jobs.append(job("a", "smelting"))
        self.h.at(700)
        self.assertEqual(self.h.at(900).opened, [])


class NoProgressTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.jobs = [job("s", status="succeeded"), job("a", "smelting")]
        self.h.nodes = [node("n_a", "a", "smelting", "running")]
        self.h.intents = [
            dict(key="a", event="action_intent", time=500 + i, action={"type": "craft", "item": "gear"})
            for i in range(100)
        ]

    def test_opens_after_progress_minutes_with_repetitive_actions_and_resolves_on_success(self):
        self.h.at(0)
        self.assertEqual(self.h.at(599).opened, [])
        self.assertEqual(self.h.intent_reads, 0)
        incident = self.h.at(600).opened[0]
        self.assertEqual(incident.kind, "no_progress")
        self.assertEqual(incident.facts["top_repeated_action"]["item"], "gear")
        self.assertEqual(incident.facts["active_jobs"][0]["actions_in_window"], 100)
        self.assertEqual([r.kind for r in incident.remedies], ["pause", "add_nodes", "wait"])
        self.h.jobs.append(job("t", "power", "succeeded"))
        self.assertEqual([o for _, o, _ in self.h.at(610).closed], ["resolved"])

    def test_varied_actions_are_not_an_incident(self):
        self.h.intents = [dict(key="a", time=i, action={"type": "craft", "item": "i" + str(i)}) for i in range(100)]
        self.h.at(0)
        self.assertEqual(self.h.at(900).opened, [])


class ReviewAndSeverityTests(unittest.TestCase):
    def test_planner_review_is_off_by_default_and_periodic_when_enabled(self):
        h = Harness()
        h.jobs = [job("a", "smelting")]
        h.nodes = [node("n_a", "a", "smelting", "running")]
        h.at(0)
        self.assertEqual(h.at(7200).opened, [])
        h = Harness(IncidentConfig(review_minutes=30))
        h.jobs = [job("a", "smelting")]
        h.nodes = [node("n_a", "a", "smelting", "running")]
        h.at(0)
        self.assertEqual(h.at(1799).opened, [])
        self.assertEqual([q.kind for q in h.at(1800).opened], ["planner_review"])

    def test_most_severe_first_and_one_consultation_per_retry_window(self):
        h = Harness()
        h.jobs = [job("b", status="blocked")]
        h.nodes = [node("n_b", "b", "starter", "blocked")]
        h.at(0)
        h.at(60)
        self.assertEqual(h.kinds(), ["blocked_job", "empty_queue"])
        first = h.tracker.due(60)
        self.assertEqual(first.kind, "empty_queue")
        h.tracker.mark_consulted(first, 60)
        self.assertEqual(h.tracker.due(60).kind, "blocked_job")
        h.tracker.mark_consulted(h.tracker.due(60), 60)
        self.assertIsNone(h.tracker.due(359))
        self.assertEqual(h.tracker.due(360).kind, "empty_queue")


class MenuTests(unittest.TestCase):
    def setUp(self):
        h = Harness()
        h.jobs = [job("s", status="succeeded"), job("b", "power", "blocked"), job("w", "smelting")]
        h.nodes = [node("n_b", "b", "power", "blocked"), node("n_w", "w", "smelting", "running")]
        h.at(0)
        self.incident = h.at(60).opened[0]

    def decision(self, operation, **extra):
        return dict(dict(operation=operation, capability=None, parameters={}, reason="why", queue=[]), **extra)

    def test_only_menu_entries_pass(self):
        plan = [dict(id="x", capability="smelting", parameters={}, priority=0, depends_on=[], reason="r")]
        self.assertIsNone(check_decision(self.incident, self.decision("retry", target_task_id="n_b")))
        self.assertIsNone(check_decision(self.incident, self.decision("plan", queue=plan)))
        self.assertIsNone(check_decision(self.incident, self.decision("wait")))
        for value, reason in (
            (self.decision("retry", target_task_id="n_w"), "not on the menu"),
            (self.decision("pause", target_task_id="n_w"), "not on the menu"),
            (self.decision("request", capability="power"), "request is not on the menu"),
            (self.decision("continue"), "continue is not on the menu"),
            (self.decision("plan", queue=[dict(plan[0], capability="research")]), "research"),
            (self.decision("plan", queue=plan * 4), "1..3"),
            (self.decision("plan"), "1..3"),
            (self.decision("wait", reason=" "), "reason"),
            (self.decision("wait", queue=plan), "must not add"),
        ):
            with self.subTest(value=value):
                self.assertIn(reason, check_decision(self.incident, value))

    def test_addable_capabilities_respect_requires_and_locked_recipes(self):
        jobs = [job("s", status="succeeded")]
        gates = {"native_capability_gates": {"smelting": {"native_recipes_ready": False}}}
        self.assertEqual(sorted(addable_capabilities(jobs, CAPS, gates)), ["power", "starter"])


class DocumentTests(unittest.TestCase):
    def incident(self, reason):
        h = Harness()
        h.tracker.summarize = lambda job: {"reason": job["reason"], "evidence": job.get("evidence")}
        h.jobs = [job("b", status="blocked", evidence={"rows": [{"n": i} for i in range(500)]}), job("x", "power")]
        h.jobs[0]["reason"] = reason
        h.nodes = [node("n_b", "b", "starter", "blocked"), node("n_x", "x", "power", "running")]
        h.at(0)
        return h.at(60).opened[0]

    def observation(self):
        return {
            "technologies": ["t" + str(i) for i in range(40)],
            "entities": {"e" + str(i): i for i in range(40)},
            "inventory": {"iron-plate": 1},
            "resource_supply_options": {
                "iron-plate": {
                    "action": "advance",
                    "evidence": dict(
                        shortfall=4, produced_per_minute=30, consumed_per_minute=10, net_per_minute=20, roi_seconds=9
                    ),
                }
            },
        }

    def test_factory_status_is_short_and_selective(self):
        jobs = [job("a"), job("b", "power", "blocked"), job("c", "power", "succeeded")]
        status = factory_status(self.observation(), jobs, [], ["t1", "t2"])
        self.assertEqual(status["researched_technologies"], 40)
        self.assertEqual(len(status["entity_counts"]), 15)
        self.assertEqual(next(iter(status["entity_counts"])), "e39")
        self.assertEqual(
            status["supply"]["iron-plate"],
            dict(shortfall=4, produced_per_minute=30, consumed_per_minute=10, net_per_minute=20),
        )
        self.assertEqual(status["jobs"], ["starter waiting r", "power blocked r"])
        self.assertNotIn("inventory", status)

    def test_document_is_capped_with_truncation_markers_and_keeps_the_menu(self):
        incident = self.incident("x" * 50000)
        doc = document(incident, factory_status(self.observation(), [], [], []))
        encoded = json.dumps(doc, separators=(",", ":"))
        self.assertLessEqual(len(encoded), DOCUMENT_CHAR_CAP)
        self.assertIn(TRUNCATED, encoded)
        self.assertEqual(doc["remedies"], [r.to_dict() for r in incident.remedies])
        small = document(incident, factory_status(self.observation(), [], [], []), cap=900)
        self.assertLessEqual(len(json.dumps(small, separators=(",", ":"))), 900)
        self.assertEqual(small["incident"]["kind"], "blocked_job")

    def test_ordinary_incident_document_needs_no_truncation(self):
        incident = self.incident("coal_bank_required")
        incident.facts["evidence"] = {"item": "coal", "have": 1, "need": 10}
        doc = document(incident, factory_status(self.observation(), [], [], []))
        self.assertNotIn(TRUNCATED, json.dumps(doc))


class SharedHelperTests(unittest.TestCase):
    def test_repeat_summary_matches_the_report_definition(self):
        rows = [dict(key="a", action={"type": "craft", "item": "gear", "count": 1})] * 6
        rows += [dict(key="b", action={"type": "mine", "name": "coal"})] * 3 + [dict(key="b", action={"type": "walk"})]
        top, share = repeat_summary(rows, {"a": "starter", "b": "power"})
        self.assertEqual(top[0], {"job": "starter", "type": "craft", "item": "gear", "count": 1, "times": 6})
        self.assertEqual(share, 0.9)
        self.assertEqual(repeat_summary([], {}), ([], 0.0))

    def test_roadmap_position_names_locked_recipes_and_unavailable_research(self):
        jobs = [job("s", status="succeeded"), job("p", "power", "succeeded")]
        observation = {
            "technologies": [],
            "available_research": {"technologies": [{"technology": "logistics"}]},
            "native_capability_gates": {"research": {"native_recipes_ready": False}},
        }
        rows = roadmap_position(jobs, [], observation, CAPS, ROADMAP)
        self.assertEqual(rows[0]["step"], "research_automation")
        self.assertIn("native recipes locked", rows[0]["waiting_on"])
        self.assertIn("technology automation not researchable yet", rows[0]["waiting_on"])


if __name__ == "__main__":
    unittest.main()
