"""The run report's incident section and the offline incident replay, on events a real supervisor wrote."""

import importlib.util
import json
from pathlib import Path
import queue
import unittest

from factorio_agent.supervisor import Supervisor
from test_v2_supervisor import CAPS, Fixture, decision, node

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_run = load("report_run")
replay_tool = load("luna_incident_replay")


class FakeClient:
    last_usage = {"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 2}

    def __init__(self, answer):
        self.answer = answer
        self.documents = []

    def decide(self, document):
        self.documents.append(document)
        return dict(self.answer)


class IncidentRunTests(Fixture):
    def run_blocked_incident(self, answer):
        """A blocked job is consulted once, then the job leaves the blocked state."""
        self.s.work.merge([node("b"), node("busy", "power")], CAPS, self.cli.validate_requests, 1)
        target = self.s.work.data["nodes"][0]
        self.cli.jobs[target["job_key"]] = dict(
            key=target["job_key"], world_id="w", capability="starter", parameters={}, status="blocked", reason="r"
        )
        self.s._check_incidents(self.s._snapshot())
        self.clock.advance(61)
        context = self.s._snapshot()
        inputs, roadmap = self.s._check_incidents(context)
        incident = self.s._incident_due()
        document = self.s._document(incident, inputs, roadmap)
        self.s._record_incident(incident, document)
        results = queue.Queue()
        results.put((True, answer))
        self.s.pending_decision = dict(result=results, context=context, started_at=self.clock(), incident=incident)
        self.clock.advance(3)
        self.s._poll_decision()
        self.clock.advance(7)
        self.cli.jobs[target["job_key"]]["status"] = "waiting"
        self.s._check_incidents(self.s._snapshot())
        return incident

    def test_report_counts_consultations_tokens_kinds_and_outcomes(self):
        self.run_blocked_incident(dict(decision("retry"), target_task_id="b"))
        data = report_run.report(self.root)
        self.assertEqual(data["planner_mode"], "luna")
        self.assertEqual(data["model_consultations"], 1)
        self.assertEqual(data["model_seconds"], 3.0)
        self.assertEqual(data["model_input_tokens"], 100)
        self.assertEqual(data["incidents_by_kind"], {"blocked_job": 1})
        self.assertEqual((data["incidents_resolved"], data["incidents_expired"]), (1, 0))
        self.assertEqual(data["median_seconds_to_resolution"], 10.0)
        self.assertEqual(
            data["incidents"],
            [dict(kind="blocked_job", capability="starter", remedy="retry", outcome="resolved", seconds=10.0)],
        )
        for key in ("milestones", "recent_repeat_share", "model_decisions", "per_job"):
            self.assertIn(key, data)

    def test_report_of_a_planner_none_run(self):
        self.s = Supervisor(self.root, None, cli=self.cli, planner="none", clock=self.clock, sleep=self.clock.advance)
        self.s._save()
        self.s._check_incidents(self.s._snapshot())
        self.clock.advance(61)
        self.s._check_incidents(self.s._snapshot())
        data = report_run.report(self.root)
        self.assertEqual(data["planner_mode"], "none")
        self.assertEqual(data["model_consultations"], 0)
        self.assertEqual(data["incidents"][0]["outcome"], "open")

    def test_replay_dry_run_prints_the_recorded_document_and_what_resolved_it(self):
        incident = self.run_blocked_incident(dict(decision("wait"), reason="later"))
        rows = replay_tool.replay(self.root)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["incident_id"], row["kind"], row["outcome"]), (incident.id, "blocked_job", "resolved"))
        self.assertEqual(row["resolved_by"], "condition_cleared")
        recorded = json.loads((self.root / "logs/incidents.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["document"], recorded["document"])
        self.assertNotIn("chosen_remedy", row)
        self.assertEqual(replay_tool.replay(self.root, limit=0), [])

    def test_replay_send_checks_the_answer_against_the_recorded_menu(self):
        self.run_blocked_incident(dict(decision("retry"), target_task_id="b"))
        client = FakeClient(dict(decision("retry"), target_task_id="b"))
        row = replay_tool.replay(self.root, send=True, client=client)[0]
        self.assertEqual((row["chosen_remedy"], row["rejected"], row["resolved_by"]), ("retry", None, "model:retry"))
        self.assertEqual(client.documents[0]["incident"]["kind"], "blocked_job")
        client = FakeClient(decision("request", "power"))
        row = replay_tool.replay(self.root, send=True, client=client)[0]
        self.assertIn("not on the menu", row["rejected"])


class BuildThroughputTests(unittest.TestCase):
    def test_entities_per_actor_minute_and_hand_logistics_share(self):
        def act(key, tick, action, result):
            return [
                {"key": key, "event": "action_intent", "tick": tick, "time": tick, "action": action},
                {"key": key, "event": "action_result", "tick": tick, "time": tick, "result": result},
            ]

        many = {"type": "build_many", "actions": [{}] * 4}
        events = (
            act("d", 0, many, {"ok": True, "status": "succeeded", "built": 3, "reused": 1})
            + act("d", 60, {"type": "build", "name": "stone-furnace"}, {"ok": True, "status": "succeeded"})
            + act(
                "d", 120, {"type": "take_many", "reason": "collect live construction stocks iron-plate"}, {"ok": True}
            )
            + act("s", 9000, {"type": "insert", "reason": "fuel starter production"}, {"ok": True})
            + act(
                "d",
                99000,
                {"type": "build", "name": "transport-belt"},
                {"ok": True, "status": "succeeded", "reused": True},
            )
        )
        data = report_run.build_throughput(events, {"d": "smelting_district", "s": "starter"})
        self.assertEqual((data["entities_placed"], data["build_many_share"]), (4, 0.75))
        # d's actor time: 60 + 60 + 3600 (capped gap to the next action) ticks = 62 s
        self.assertEqual(data["entities_per_actor_minute"], round(4 / (3720 / 3600), 1))
        self.assertEqual(data["by_capability"], {"smelting_district": 4})
        first = data["hand_logistics"][0]
        self.assertEqual((first["window"], first["actions"]), ("0-10", 4))
        self.assertEqual((first["hand_logistics_share"], first["building_share"]), (0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
