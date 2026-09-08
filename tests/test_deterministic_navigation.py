from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import socket
import unittest
from unittest.mock import Mock, patch
import zipfile

from factorio_ai.deterministic_navigation import (CharacterNavigator, prepare_character_save,
    SCENARIO_INPUT_LUA, SCENARIO_MARKER, SCENARIO_MODULE, INSTALL_INPUT_LUA)
from factorio_ai.factorio import no_mod_save_path


class CharacterScenarioTests(unittest.TestCase):
    def config(self, root, port=27031):
        return SimpleNamespace(runtime_dir=Path(root), rcon_host="127.0.0.1", rcon_port=port)

    def save(self, cfg):
        path = no_mod_save_path(cfg)
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fixture/control.lua", "require('__base__/script/freeplay/control.lua')\n")
            archive.writestr("fixture/level.dat", b"preserve exact world data")
        return path

    def test_embedding_preserves_freeplay_world_bytes_and_original_archive(self):
        with TemporaryDirectory() as root:
            cfg = self.config(root)
            save = self.save(cfg)
            original = save.read_bytes()
            with patch("factorio_ai.deterministic_navigation.socket.create_connection", side_effect=OSError):
                result = prepare_character_save(cfg, newly_created=True)
            self.assertTrue(result["changed"])
            self.assertEqual(Path(result["backup"]).read_bytes(), original)
            with zipfile.ZipFile(save) as archive:
                control = archive.read("fixture/control.lua").decode()
                self.assertTrue(control.startswith("require('__base__/script/freeplay/control.lua')\n"))
                self.assertIn(SCENARIO_MARKER, control)
                self.assertEqual(archive.read("fixture/level.dat"), b"preserve exact world data")
                self.assertEqual(archive.read("fixture/" + SCENARIO_MODULE).decode(), SCENARIO_INPUT_LUA)

    def test_unmarked_existing_save_is_refused_without_any_write(self):
        with TemporaryDirectory() as root:
            cfg = self.config(root)
            save = self.save(cfg)
            original = save.read_bytes()
            with patch("factorio_ai.deterministic_navigation.socket.create_connection", side_effect=OSError):
                with self.assertRaisesRegex(ValueError, "unmarked existing save"):
                    prepare_character_save(cfg)
            self.assertEqual(save.read_bytes(), original)
            self.assertEqual(len(list(save.parent.iterdir())), 1)

    def test_resume_is_idempotent_and_does_not_add_duplicate_event_wrappers(self):
        with TemporaryDirectory() as root:
            cfg = self.config(root)
            save = self.save(cfg)
            with patch("factorio_ai.deterministic_navigation.socket.create_connection", side_effect=OSError):
                prepare_character_save(cfg, newly_created=True)
                before = save.read_bytes()
                result = prepare_character_save(cfg)
            self.assertFalse(result["changed"])
            self.assertEqual(save.read_bytes(), before)
            with zipfile.ZipFile(save) as archive:
                self.assertEqual(archive.read("fixture/control.lua").decode().count(SCENARIO_MARKER), 1)

    def test_running_server_is_refused_before_archive_mutation(self):
        with TemporaryDirectory() as root, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            cfg = self.config(root, listener.getsockname()[1])
            save = self.save(cfg)
            original = save.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "before the server starts"):
                prepare_character_save(cfg, newly_created=True)
            self.assertEqual(save.read_bytes(), original)

    def test_installer_validates_handlers_without_registering_transient_console_functions(self):
        self.assertNotIn("script.on_event", INSTALL_INPUT_LUA)
        self.assertIn("character_scenario_not_installed", INSTALL_INPUT_LUA)
        self.assertIn("if old_tick then old_tick(event) end", SCENARIO_INPUT_LUA)
        self.assertIn("if old_path then old_path(event) end", SCENARIO_INPUT_LUA)


class CharacterNavigationTests(unittest.TestCase):
    def setUp(self):
        self.game = SimpleNamespace(backend="character", query=Mock(return_value={"ok": True, "status": "running"}),
                                    act=Mock(return_value={"ok": True, "status": "succeeded"}))
        self.navigator = CharacterNavigator(self.game)
        self.navigator.pending_action = Mock(return_value=None)

    def test_assisted_backend_cannot_install_strict_input_handlers(self):
        with self.assertRaises(ValueError):
            CharacterNavigator(SimpleNamespace(backend="assisted"))

    def test_distant_inventory_action_moves_before_transferring_any_items(self):
        self.game.query.side_effect = [{"ok": True, "within": False}, {"ok": True, "status": "running"}]
        action = {"type": "take", "name": "iron-chest", "position": {"x": 25, "y": 0}, "item": "iron-plate", "count": 10}
        result = self.navigator.execute(action, {})
        self.assertEqual(result["status"], "running")
        self.game.act.assert_not_called()
        self.assertIn('"move"', self.game.query.call_args.args[0].replace('\\"', '"'))

    def test_reachable_action_releases_motion_before_normal_adapter_mutation(self):
        calls = []
        self.game.query.side_effect = lambda body: calls.append("query") or {"ok": True, "within": True}
        self.game.act.side_effect = lambda action: calls.append("act") or {"ok": True}
        action = {"type": "insert", "name": "stone-furnace", "position": {"x": 1, "y": 1}, "item": "coal", "count": 1}
        self.navigator.execute(action, {})
        self.assertEqual(calls, ["query", "query", "act"])
        self.game.act.assert_called_once_with(action)

    def test_failed_reach_observation_prevents_action(self):
        self.game.query.return_value = {"ok": False, "reason": "character_missing"}
        result = self.navigator.execute({"type": "build", "name": "stone-furnace", "position": {"x": 1, "y": 1}}, {})
        self.assertFalse(result["ok"])
        self.game.act.assert_not_called()

    def test_build_moves_outside_future_machine_footprint_before_placement(self):
        self.game.query.side_effect = [{"ok": True, "within": False, "approach": {"x": 3, "y": 1}},
                                      {"ok": True, "status": "running"}]
        with patch.object(self.navigator, "_input", return_value={"ok": True}) as move:
            self.navigator.execute({"type": "build", "name": "stone-furnace", "position": {"x": 1, "y": 1}}, {})
        move.assert_called_once_with({"type": "move", "position": {"x": 3, "y": 1}})
        self.game.act.assert_not_called()

    def test_stop_clears_persistent_motion_before_releasing_engine_inputs(self):
        self.navigator.stop()
        self.assertIn("d.motion=nil", self.game.query.call_args.args[0])
        self.game.act.assert_called_once_with({"type": "stop"})

    def test_partial_mining_stock_cannot_interrupt_requested_batch_for_one_item_delivery(self):
        mining = {"type": "mine", "name": "coal", "position": {"x": 20, "y": 5}, "count": 24}
        self.navigator.pending_action.return_value = mining
        delivery = {"type": "insert", "name": "stone-furnace", "position": {"x": 1, "y": 1}, "item": "coal", "count": 1}
        with patch.object(self.navigator, "_input", return_value={"ok": True, "status": "running"}) as advance:
            result = self.navigator.execute(delivery, {"inventory": {"coal": 1}})
        advance.assert_called_once_with(mining)
        self.assertEqual(result["continued_action"], mining)
        self.game.act.assert_not_called()

    def test_operator_stop_cancels_even_an_unfinished_mining_batch(self):
        self.navigator.pending_action.return_value = {"type": "mine", "count": 24}
        self.navigator.execute({"type": "stop"}, {})
        self.navigator.pending_action.assert_not_called()
        self.game.act.assert_called_once_with({"type": "stop"})

    def test_invalid_mining_count_never_starts_engine_input(self):
        for value in (True, 0, -1, 1.5):
            with self.assertRaises(ValueError):
                self.navigator._input({"type": "mine", "count": value, "position": {"x": 1, "y": 1}})
        self.game.query.assert_not_called()

    def test_partial_source_upgrade_guard_never_starts_engine_input(self):
        with self.assertRaisesRegex(ValueError, "invalid exhausted source mining guard"):
            self.navigator._input({"type": "mine", "name": "burner-mining-drill",
                                   "position": {"x": 1, "y": 1}, "expected_world_id": "world"})
        self.game.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
