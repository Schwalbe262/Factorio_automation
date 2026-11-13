"""A Lua script is sent as one line: line comments must be removed before the lines are joined."""

import unittest

from factorio_agent.core.deterministic_game import _without_line_comments as strip


class LuaLineCommentTests(unittest.TestCase):
    def test_trailing_comment_is_removed(self):
        self.assertEqual(strip("local a=1 -- note"), "local a=1 ")
        self.assertEqual(strip("-- whole line"), "")

    def test_dashes_inside_quotes_stay(self):
        self.assertEqual(strip('x="a--b" y=2'), 'x="a--b" y=2')
        escaped = "s='it" + chr(92) + "'s -- in'"  # an escaped quote does not end the string
        self.assertEqual(strip(escaped + " -- out"), escaped + " ")

    def test_block_comments_stay(self):
        self.assertEqual(strip("--[[ keep ]] z=1"), "--[[ keep ]] z=1")

    def test_joined_script_keeps_the_code_after_a_comment(self):
        lua = "local a=1\n-- explain\nlocal b=2\nreturn a+b"
        joined = " ".join(strip(line) for line in lua.splitlines())
        self.assertIn("local b=2", joined)
        self.assertNotIn("explain", joined)


if __name__ == "__main__":
    unittest.main()
