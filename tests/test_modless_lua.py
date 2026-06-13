import unittest

from factorio_ai.modless_lua import build_modless_action_command, build_modless_observe_command


class ModlessLuaTests(unittest.TestCase):
    def test_observe_uses_silent_command_without_custom_mod_command(self):
        command = build_modless_observe_command("AI")
        self.assertTrue(command.startswith("/silent-command "))
        self.assertIn("rcon.print", command)
        self.assertIn("mode = \"modless-rcon-lua\"", command)
        self.assertIn("agent_marker", command)
        self.assertIn("add_chart_tag", command)
        self.assertIn("return ensure_server_agent()", command)
        self.assertIn("power_sites = collect_power_sites", command)
        self.assertIn("lab_sites = collect_lab_sites", command)
        self.assertIn("automation_sites = collect_automation_sites", command)
        self.assertIn("GLOBAL_FORCE_ENTITY_LIMIT", command)
        self.assertIn("force = agent.force", command)
        self.assertIn("base = { anchor_position", command)
        self.assertIn("clear_of_resources", command)
        self.assertIn("distance_from_agent", command)
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

    def test_connect_power_action_is_allowlisted_and_uses_pole_wire_connector(self):
        command = build_modless_action_command({"type": "connect_power", "unit_number": 10})
        self.assertIn("action_connect_power", command)
        self.assertIn("defines.wire_connector_id.pole_copper", command)
        self.assertIn("connect_to", command)

    def test_action_updates_agent_marker(self):
        command = build_modless_action_command({"type": "mine", "position": {"x": 1, "y": 2}})
        self.assertIn("remember_agent_marker", command)
        self.assertIn("agent_marker", command)
        self.assertIn("[AI]", command)

    def test_action_handles_virtual_lab_trigger_research(self):
        command = build_modless_action_command({"type": "craft", "recipe": "lab", "count": 1})
        self.assertIn('recipe_name == "lab"', command)
        self.assertIn('agent.force.technologies["automation-science-pack"]', command)

    def test_action_research_completes_trigger_technology(self):
        command = build_modless_action_command({"type": "research", "technology": "automation-science-pack"})
        self.assertIn("research_unit_ingredients", command)
        self.assertIn("trigger = true", command)


if __name__ == "__main__":
    unittest.main()
