"""The in-process kernel must behave like the one-shot CLI, minus the process starts."""

import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.core import deterministic_game
from factorio_agent.core.deterministic_game import DeterministicGame
from factorio_agent.core.rcon import FactorioRconClient
from factorio_agent.jobs import Ledger, Request, run_slice, submit
from factorio_agent.kernel import InProcessBridge, Session
from factorio_agent.supervisor import CLIError


class FakeClient:
    """Stands in for FactorioRconClient: records commands, can be closed by the peer or fail."""

    created = []

    def __init__(self, *args, **kwargs):
        self.commands = []
        self.dead = False
        self.fail = None
        self.closed = 0
        FakeClient.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        pass

    def close(self):
        self.closed += 1

    def peer_closed(self):
        return self.dead

    def execute(self, command, drain_seconds=0.15, complete=None):
        self.commands.append(command)
        if self.fail:
            raise self.fail
        return '{"ok":true}'


class PersistentConnectionTests(unittest.TestCase):
    def setUp(self):
        FakeClient.created = []
        patcher = patch.object(deterministic_game, "FactorioRconClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.game = DeterministicGame(SimpleNamespace(rcon_host="h", rcon_port=1, rcon_password="p"))
        self.game._confirmed = True

    def test_default_game_opens_a_connection_per_query(self):
        self.game.query("return {ok=true}")
        self.game.query("return {ok=true}")
        self.assertEqual(len(FakeClient.created), 2)

    def test_kept_connection_is_reused_and_counted(self):
        self.game.keep_connection = True
        for _ in range(3):
            self.assertEqual(self.game.query("return {ok=true}"), {"ok": True})
        self.assertEqual(len(FakeClient.created), 1)
        self.assertEqual(len(FakeClient.created[0].commands), 3)
        self.assertEqual(self.game.query_stats["count"], 3)

    def test_socket_closed_by_the_server_is_replaced_before_anything_is_sent(self):
        self.game.keep_connection = True
        self.game.query("return {ok=true}")
        FakeClient.created[0].dead = True
        self.game.query("return {ok=true}")
        self.assertEqual(len(FakeClient.created), 2)
        self.assertEqual(len(FakeClient.created[0].commands), 1)
        self.assertEqual(len(FakeClient.created[1].commands), 1)

    def test_failed_command_is_never_resent(self):
        self.game.keep_connection = True
        self.game.query("return {ok=true}")
        first = FakeClient.created[0]
        first.fail = TimeoutError("no reply")
        with self.assertRaises(TimeoutError):
            self.game.query("return {ok=true}")
        self.assertEqual(len(first.commands), 2)
        self.assertEqual(len(FakeClient.created), 1)
        self.assertIsNone(self.game._client)
        self.game.query("return {ok=true}")
        self.assertEqual(len(FakeClient.created), 2)


def packet(request_id, body):
    data = body.encode()
    return struct.pack("<iii", len(data) + 10, request_id, 0) + data + b"\x00\x00"


class RconReplyTests(unittest.TestCase):
    def setUp(self):
        self.near, self.far = socket.socketpair()
        self.addCleanup(self.near.close)
        self.addCleanup(self.far.close)
        self.client = FactorioRconClient("h", 1, "p", timeout=5)
        self.near.settimeout(5)
        self.client._socket = self.near

    def reply(self, *bodies, gap=0.0):
        def serve():
            self.far.recv(65536)
            for body in bodies:
                self.far.sendall(packet(1001, body))
                time.sleep(gap)

        threading.Thread(target=serve, daemon=True).start()

    def test_whole_reply_ends_the_wait_without_draining(self):
        self.reply('{"ok":true}')
        started = time.monotonic()
        text = self.client.execute("cmd", drain_seconds=3, complete=deterministic_game._whole_json)
        self.assertEqual(json.loads(text), {"ok": True})
        self.assertLess(time.monotonic() - started, 1)

    def test_split_reply_is_joined_before_completion(self):
        self.reply('{"ok":true,"rows":[1,', "2,3]}", gap=0.05)
        text = self.client.execute("cmd", drain_seconds=3, complete=deterministic_game._whole_json)
        self.assertEqual(json.loads(text), {"ok": True, "rows": [1, 2, 3]})

    def test_peer_closed_reports_a_dead_socket_without_sending(self):
        self.assertFalse(self.client.peer_closed())
        self.far.close()
        self.assertTrue(self.client.peer_closed())


class Game:
    def __init__(self):
        self.observation = {"ok": True, "world_id": "world", "tick": 100, "entities": [], "production": {}}
        self.saved = 0
        self.observed = 0

    def observe(self):
        self.observed += 1
        self.observation["tick"] += 1
        return json.loads(json.dumps(self.observation))

    def query(self, body):
        return {"ok": True, "world_id": "world", "tick": self.observation["tick"], "networks": []}

    def save(self):
        self.saved += 1
        self.last_save_at = time.monotonic()

    def close(self):
        pass


class Waiting:
    def step(self, capability, obs, parameters=None):
        return {"status": "waiting", "reason": "test wait"}


class SaveCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()
        submit(self.root, self.game, Request("test", "world", 100, "starter"))

    def test_one_shot_game_saves_every_slice(self):
        run_slice(self.root, self.game, Waiting(), "test", seconds=0.001)
        del self.game.last_save_at
        run_slice(self.root, self.game, Waiting(), "test", seconds=0.001)
        self.assertEqual(self.game.saved, 2)

    def test_long_lived_game_saves_at_most_once_per_interval(self):
        run_slice(self.root, self.game, Waiting(), "test", seconds=0.001)
        run_slice(self.root, self.game, Waiting(), "test", seconds=0.001)
        self.assertEqual(self.game.saved, 1)
        self.game.last_save_at -= 61
        run_slice(self.root, self.game, Waiting(), "test", seconds=0.001)
        self.assertEqual(self.game.saved, 2)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.game = Game()
        self.now = [0.0]
        patcher = patch("factorio_agent.runtime.game_for", return_value=self.game)
        self.game_for = patcher.start()
        self.addCleanup(patcher.stop)
        risk = patch("factorio_agent.defense_policy.DefensePolicy", side_effect=ValueError("no survey in this test"))
        risk.start()
        self.addCleanup(risk.stop)
        self.session = Session(self.root, clock=lambda: self.now[0])
        self.bridge = InProcessBridge(self.root, session=self.session)

    def test_game_is_built_once_and_keeps_its_connection(self):
        self.bridge.invoke(["observe"])
        self.bridge.invoke(["checkpoint"])
        self.assertEqual(self.game_for.call_count, 1)
        self.assertTrue(self.game.keep_connection)

    def test_observation_is_reused_briefly_and_never_across_a_mutation(self):
        first = self.bridge.invoke(["observe"])
        again = self.bridge.invoke(["observe"])
        self.assertEqual(first, again)
        self.assertEqual(self.game.observed, 1)
        again["tick"] = -1
        self.assertEqual(self.bridge.invoke(["observe"])["tick"], first["tick"])
        self.bridge.invoke(["status"])
        self.bridge.invoke(["observe"])
        self.assertEqual(self.game.observed, 1)
        self.bridge.invoke(["checkpoint"])
        self.assertGreater(self.bridge.invoke(["observe"])["tick"], first["tick"])
        self.now[0] += 2.5
        self.bridge.invoke(["observe"])
        self.assertEqual(self.game.observed, 3)

    def test_receipts_are_plain_copies(self):
        capabilities = self.bridge.invoke(["capabilities"])
        capabilities.clear()
        self.assertIn("starter", self.bridge.invoke(["capabilities"]))

    def test_typed_failures_and_error_receipts_become_cli_errors(self):
        with (
            patch("factorio_agent.runtime.catalog_for", return_value=object()),
            patch("factorio_agent.kernel.session.StarterExecutors", return_value=Waiting()),
        ):
            with self.assertRaises(CLIError) as raised:
                self.bridge.invoke(["run", "--key", "missing", "--seconds", "1"])
        self.assertIn("unknown job key", str(raised.exception))
        with self.assertRaises(CLIError) as raised:
            self.bridge.invoke(["status", "--key", "missing"])
        self.assertEqual(str(raised.exception), "unknown_job_key")
        with self.assertRaises(CLIError):
            self.bridge.invoke(["no-such-command"])

    def test_slice_runs_with_fresh_executors_and_reports_timing(self):
        calls = []
        self.bridge.on_call = lambda command, seconds, error: calls.append((command, error))
        self.bridge.invoke(["observe"])
        submit(self.root, self.game, Request("job", "world", self.game.observation["tick"], "starter"))
        built = []

        def executors(game, catalog):
            built.append(game)
            return Waiting()

        with (
            patch("factorio_agent.kernel.session.StarterExecutors", side_effect=executors),
            patch("factorio_agent.runtime.catalog_for", return_value=object()),
        ):
            for _ in range(2):
                receipt = self.bridge.invoke(["run", "--key", "job", "--seconds", "0.001"])
        self.assertEqual(receipt["status"], "waiting")
        self.assertEqual(built, [self.game, self.game])
        self.assertEqual(calls, [("observe", None), ("run", None), ("run", None)])
        self.assertEqual(self.session.metrics()["commands"]["run"]["count"], 2)

    def test_stop_request_is_signalled_once_while_the_slice_runs(self):
        release = threading.Event()
        stops = []

        def slow(args):
            if args[0] == "run":
                release.wait(5)
                return {"status": "waiting"}
            return {"ok": True}

        self.session.run = slow

        def on_stop():
            stops.append(self.bridge.invoke(["stop-worker"]))
            release.set()

        receipt = self.bridge.invoke(
            ["run", "--key", "job", "--seconds", "1"], stop_requested=lambda: True, on_stop=on_stop
        )
        self.assertEqual(receipt, {"status": "waiting"})
        self.assertEqual(stops, [{"ok": True}])

    def test_stop_marker_command_does_not_wait_for_the_session_lock(self):
        with self.session._lock:
            receipt = self.bridge.invoke(["stop-worker"])
        self.assertEqual(receipt["status"], "stop_requested")
        self.assertTrue((self.root / "stop.json").exists())

    def test_request_batches_are_validated_in_process(self):
        self.bridge.validate_requests([])
        good = dict(key="k", world_id="world", observed_tick=1, capability="starter", parameters={})
        self.bridge.validate_requests([good])
        with self.assertRaises(CLIError):
            self.bridge.validate_requests([dict(good, capability="not-a-capability")])
        with self.assertRaises(CLIError):
            self.bridge.validate_requests([good] * 65)


class LedgerCompatibilityTests(unittest.TestCase):
    def test_session_and_cli_share_the_same_ledger_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            game = Game()
            with patch("factorio_agent.runtime.game_for", return_value=game):
                bridge = InProcessBridge(root)
                submit(root, game, Request("job", "world", game.observation["tick"], "starter"))
                self.assertEqual(bridge.invoke(["status"])["jobs"]["job"]["status"], "pending")
                self.assertEqual(Ledger(root).data["jobs"]["job"]["capability"], "starter")


if __name__ == "__main__":
    unittest.main()
