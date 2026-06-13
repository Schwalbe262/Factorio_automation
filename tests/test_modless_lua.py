import unittest

from factorio_ai.modless_lua import build_modless_action_command, build_modless_observe_command


class ModlessLuaTests(unittest.TestCase):
    def test_observe_uses_silent_command_without_custom_mod_command(self):
        command = build_modless_observe_command("AI")
        self.assertTrue(command.startswith("/silent-command "))
        self.assertIn("rcon.print", command)
        self.assertIn("mode = \"modless-rcon-lua\"", command)
        self.assertNotIn("ai_observe", command)
        self.assertNotIn("factorio_ai_autoplayer", command)

    def test_rejects_unallowlisted_world_mutation_action(self):
        with self.assertRaises(ValueError):
            build_modless_action_command({"type": "create_entity", "name": "rocket-silo"})

    def test_walking_action_is_allowlisted_and_direction_checked(self):
        command = build_modless_action_command({"type": "set_walking_state", "direction": "east"})
        self.assertIn("walking_state", command)
        self.assertIn("defines.direction.east", command)
        with self.assertRaises(ValueError):
            build_modless_action_command({"type": "set_walking_state", "direction": "up"})

    def test_chart_action_is_allowlisted(self):
        command = build_modless_action_command({"type": "chart", "radius": 64})
        self.assertIn("agent.force.chart", command)
        self.assertIn("local radius = action.radius or 128", command)


if __name__ == "__main__":
    unittest.main()
