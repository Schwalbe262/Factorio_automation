import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_character as module


def config(root):
    return SimpleNamespace(runtime_dir=Path(root), factorio_exe=Path('C:/Factorio/bin/x64/factorio.exe'),
                           rcon_host='127.0.0.1', server_port=34213)


class DedicatedCraftingClientTests(unittest.TestCase):
    def tearDown(self):
        module._PROCESSES.clear()

    def test_fresh_profile_has_lan_identity_without_auth_or_cloud_sync(self):
        with TemporaryDirectory() as directory:
            command, root = module.prepare_client(config(directory))
            data = json.loads((root / 'data/player-data.json').read_text(encoding='utf-8'))
            self.assertEqual(data, {'service-username': 'FactoryAutomaton', 'service-token': ''})
            self.assertIn('enable-blueprint-storage-cloud-sync=false', (root / 'client-config.ini').read_text())
            self.assertEqual((root / 'steam_appid.txt').read_text(), '427520')
            self.assertEqual(command[command.index('--port') + 1], '0')
            self.assertEqual(command[-1], '127.0.0.1:34213')
            self.assertIn(str(root / 'mods'), command)
            self.assertTrue(root.is_relative_to(Path(directory).resolve()))

    def test_existing_private_profile_is_not_replaced_or_copied(self):
        with TemporaryDirectory() as directory:
            cfg = config(directory)
            _, root = module.prepare_client(cfg)
            path = root / 'data/player-data.json'
            content = json.dumps({'service-username': 'FactoryAutomaton', 'tips': {'example': True}})
            path.write_text(content)
            module.prepare_client(cfg)
            self.assertEqual(path.read_text(), content)

    def test_ready_player_does_not_start_another_gui(self):
        with TemporaryDirectory() as directory:
            game = SimpleNamespace(cfg=config(directory), query=Mock(return_value={'status': 'ready', 'player_name': module.PLAYER_NAME}))
            with patch.object(module.subprocess, 'Popen') as spawn:
                self.assertEqual(module.ensure_crafting_player(game)['status'], 'ready')
                spawn.assert_not_called()

    def test_unrelated_character_is_a_hard_bind_failure_without_spawning(self):
        with TemporaryDirectory() as directory:
            game = SimpleNamespace(cfg=config(directory), query=Mock(return_value={'status': 'blocked', 'reason': 'automation_character_owned_by_another_player'}))
            with patch.object(module.subprocess, 'Popen') as spawn:
                self.assertEqual(module.ensure_crafting_player(game)['reason'], 'automation_character_owned_by_another_player')
                spawn.assert_not_called()

    def test_running_owned_client_is_hidden_by_pid_and_never_duplicated(self):
        with TemporaryDirectory() as directory:
            cfg = config(directory)
            _, root = module.prepare_client(cfg)
            (root / 'client-process.json').write_text(json.dumps({'pid': 345, 'creation_time': 123, 'started_at': 1000}))
            game = SimpleNamespace(cfg=cfg, query=Mock(return_value={'status': 'running', 'reason': 'dedicated_client_connecting'}))
            with patch.object(module, '_identity', return_value=123), patch.object(module.time, 'time', return_value=1010), patch.object(module, '_hide_client_windows') as hide, patch.object(module.subprocess, 'Popen') as spawn:
                self.assertEqual(module.ensure_crafting_player(game)['pid'], 345)
                hide.assert_called_once_with(345)
                spawn.assert_not_called()

    def test_new_client_records_identity_and_uses_hidden_startup(self):
        with TemporaryDirectory() as directory:
            cfg = config(directory)
            game = SimpleNamespace(cfg=cfg, query=Mock(return_value={'status': 'running', 'reason': 'dedicated_client_connecting'}))
            process = Mock(pid=345)
            with patch.object(module, '_identity', return_value=123), patch.object(module.subprocess, 'Popen', return_value=process) as spawn:
                result = module.ensure_crafting_player(game)
            self.assertEqual(result['reason'], 'dedicated_client_started')
            self.assertEqual(result['creation_time'], 123)
            self.assertEqual(spawn.call_args.kwargs['cwd'], str((Path(directory) / 'agent-client').resolve()))
            if module.os.name == 'nt':
                self.assertEqual(spawn.call_args.kwargs['startupinfo'].wShowWindow, 0)

    def test_timeout_is_reported_with_private_log_path(self):
        with TemporaryDirectory() as directory:
            cfg = config(directory)
            _, root = module.prepare_client(cfg)
            (root / 'client-process.json').write_text(json.dumps({'pid': 345, 'creation_time': 123, 'started_at': 1000}))
            game = SimpleNamespace(cfg=cfg, query=Mock(return_value={'status': 'running'}))
            with patch.object(module, '_identity', return_value=123), patch.object(module.time, 'time', return_value=1200), patch.object(module, '_hide_client_windows'):
                result = module.ensure_crafting_player(game)
            self.assertEqual(result['reason'], 'dedicated_client_connection_timeout')
            self.assertIn('agent-client', result['log_path'])

    def test_stop_refuses_reused_pid(self):
        with TemporaryDirectory() as directory:
            cfg = config(directory)
            _, root = module.prepare_client(cfg)
            (root / 'client-process.json').write_text(json.dumps({'pid': 345, 'creation_time': 123}))
            with patch.object(module, '_identity', return_value=999):
                self.assertEqual(module.stop_crafting_client(cfg)['reason'], 'owned_client_not_running')


if __name__ == '__main__':
    unittest.main()
