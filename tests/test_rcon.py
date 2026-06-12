import unittest

from factorio_ai.rcon import parse_json_response


class RconTests(unittest.TestCase):
    def test_parse_json_response_from_line(self):
        payload = parse_json_response('noise\n{"ok":true,"tick":12}\n')
        self.assertEqual(payload["tick"], 12)


if __name__ == "__main__":
    unittest.main()
