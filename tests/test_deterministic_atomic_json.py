import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.deterministic_state import _atomic_json


def legacy_bytes(payload):
    output = io.StringIO()
    json.dump(payload, output, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return (output.getvalue() + "\n").replace("\n", os.linesep).encode("utf-8")


class AtomicJsonTests(unittest.TestCase):
    def test_written_bytes_match_legacy_nested_unicode_and_number_encoding(self):
        payloads = [
            {"world": "서울/世界🙂", "z": {"é": "e\u0301", "quotes": '\\"\n\t'},
             "rows": [{"position": {"y": -.5, "x": 18.5}, "active": True}, None, False, [], {}]},
            {"numbers": [0, -7, 2 ** 100, -0.0, .1, 1e-7, 1e20, sys.float_info.max,
                         float.fromhex("0x0.0000000000001p-1022")]},
            {"recipe": "copper-cable", "items": ("copper-plate", 2), "empty": ""},
        ]
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            for payload in payloads:
                with self.subTest(payload=payload):
                    _atomic_json(path, payload)
                    self.assertEqual(path.read_bytes(), legacy_bytes(payload))
                    self.assertEqual(list(Path(root).iterdir()), [path])

    def test_serialization_failures_preserve_old_bytes_and_remove_temporary_file(self):
        cycle = []
        cycle.append(cycle)
        failures = [(TypeError, {"a": 1, "z": object()}), (ValueError, cycle)]
        failures += [(ValueError, {"nested": [number]}) for number in (float("nan"), float("inf"), -float("inf"))]
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            old = b'{"last_good":true}\n'
            path.write_bytes(old)
            for expected, payload in failures:
                with self.subTest(expected=expected, kind=type(payload).__name__), \
                        patch("factorio_ai.deterministic_state.os.replace") as replace:
                    with self.assertRaises(expected):
                        _atomic_json(path, payload)
                    replace.assert_not_called()
                    self.assertEqual(path.read_bytes(), old)
                    self.assertEqual(list(Path(root).iterdir()), [path])

    def test_complete_new_bytes_are_flushed_and_synced_before_atomic_replacement(self):
        payload = {"world": "Nauvis 서울", "tick": 123, "entities": [{"x": .5, "y": -1.5}]}
        expected = legacy_bytes(payload)
        actual_fsync, actual_replace = os.fsync, os.replace
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            old = b'{"old":true}\n'
            path.write_bytes(old)
            events = []
            def fsync(fd):
                temporary, = Path(root).glob("*.tmp")
                self.assertEqual(temporary.read_bytes(), expected)
                self.assertEqual(path.read_bytes(), old)
                actual_fsync(fd)
                events.append("synced")
            def replace(source, destination):
                self.assertEqual(events, ["synced"])
                self.assertEqual(Path(source).read_bytes(), expected)
                actual_replace(source, destination)
                events.append("replaced")
            with patch("factorio_ai.deterministic_state.os.fsync", side_effect=fsync), \
                    patch("factorio_ai.deterministic_state.os.replace", side_effect=replace):
                _atomic_json(path, payload)
            self.assertEqual(events, ["synced", "replaced"])
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_fsync_and_replace_failures_leave_last_good_file_and_no_temporary_file(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            old = b'{"last_good":true}\n'
            path.write_bytes(old)
            for operation in ("fsync", "replace"):
                with self.subTest(operation=operation), patch("factorio_ai.deterministic_state.os." + operation,
                        side_effect=OSError("disk unavailable")):
                    with self.assertRaises(OSError):
                        _atomic_json(path, {"replacement": [1, 2, 3]})
                    self.assertEqual(path.read_bytes(), old)
                    self.assertEqual(list(Path(root).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
