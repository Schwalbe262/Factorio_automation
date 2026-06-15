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
        self.assertIn("AGENT_VISION_CHART_RADIUS = 96", command)
        self.assertIn("force = agent.force", command)
        self.assertIn("base = { anchor_position", command)
        self.assertIn("clear_of_resources", command)
        self.assertIn('["wooden-chest"]', command)
        self.assertIn('["iron-chest"]', command)
        self.assertIn('"wooden-chest"', command)
        self.assertIn('"iron-chest"', command)
        self.assertIn('"steel-chest"', command)
        self.assertIn("STARTER_RESOURCE_RADIUS", command)
        self.assertIn("STARTER_RESOURCE_TILE_LIMIT", command)
        self.assertIn("REMOTE_RESOURCE_TILE_LIMIT", command)
        self.assertIn("POWER_SITE_RADIUS = 1024", command)
        self.assertIn("POWER_SITE_WATER_TILE_LIMIT = 1600", command)
        self.assertIn("POWER_SITE_SCAN_RADII", command)
        self.assertIn("for _, scan_radius in pairs(POWER_SITE_SCAN_RADII)", command)
        self.assertIn("table.sort(water_tiles", command)
        self.assertIn("local include_planning_sites = true", command)
        self.assertIn("if include_planning_sites then", command)
        self.assertIn("distance_from_agent", command)
        self.assertIn("distance_from_base", command)
        self.assertIn("player_move_state", command)
        self.assertIn("player_actual_position", command)
        self.assertIn("controller_is_character", command)
        self.assertIn("auto_select_player_name", command)
        self.assertIn('observed_from = source_spec.source', command)
        self.assertIn("resources = collect_resources(base_anchor)", command)
        self.assertIn('execution = { mode = agent.kind == "server" and "virtual" or "player"', command)
        self.assertNotIn("ai_observe", command)
        self.assertNotIn("factorio_ai_autoplayer", command)

    def test_dashboard_lightweight_observe_can_skip_planning_site_collectors(self):
        command = build_modless_observe_command("AI", include_planning_sites=False)

        self.assertIn("local include_planning_sites = false", command)
        self.assertIn("if include_planning_sites then", command)
        self.assertIn("power_sites = power_sites", command)
        self.assertIn("lab_sites = lab_sites", command)
        self.assertIn("automation_sites = automation_sites", command)

    def test_auto_player_name_falls_through_to_connected_players(self):
        command = build_modless_observe_command("auto")
        named_lookup = 'if not auto_select_player_name(player_name) then local named = game.get_player(player_name)'
        connected_lookup = "for _, player in pairs(game.connected_players) do"

        self.assertIn('return normalized == "auto"', command)
        self.assertIn(named_lookup, command)
        self.assertIn(connected_lookup, command)
        self.assertLess(command.index(named_lookup), command.index(connected_lookup))

    def test_rejects_unallowlisted_world_mutation_action(self):
        with self.assertRaises(ValueError):
            build_modless_action_command({"type": "create_entity", "name": "rocket-silo"})

    def test_walking_action_is_allowlisted_and_direction_checked(self):
        command = build_modless_action_command({"type": "set_walking_state", "direction": "east"})
        self.assertIn("walking_state", command)
        self.assertIn("defines.direction.east", command)
        with self.assertRaises(ValueError):
            build_modless_action_command({"type": "set_walking_state", "direction": "up"})

    def test_move_to_reports_player_move_state(self):
        command = build_modless_action_command({"type": "move_to", "position": {"x": 1, "y": 2}})
        self.assertIn("RCON Lua walking_state moves only one tick", command)
        self.assertIn("use GUI input movement executor", command)

    def test_restore_character_controller_action_is_allowlisted(self):
        command = build_modless_action_command({"type": "restore_character_controller"})

        self.assertIn("action_restore_character_controller", command)
        self.assertIn("player.set_controller", command)
        self.assertIn("defines.controllers.character", command)

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
        self.assertIn("result.execution", command)
        self.assertIn("[AI]", command)
        self.assertIn("chart_area_around(agent, agent.position)", command)

    def test_mine_action_protects_starter_crash_site_artifacts_by_default(self):
        command = build_modless_action_command({"type": "mine", "position": {"x": 1, "y": 2}})

        self.assertIn("PRESERVED_STARTER_ARTIFACT_RADIUS = 192", command)
        self.assertIn("is_preserved_starter_artifact", command)
        self.assertIn("preserved starter artifact is protected", command)
        self.assertIn("allow_preserved_artifact", command)

    def test_action_handles_virtual_lab_trigger_research(self):
        command = build_modless_action_command({"type": "craft", "recipe": "lab", "count": 1})
        self.assertIn('recipe_name == "lab"', command)
        self.assertIn('agent.force.technologies["automation-science-pack"]', command)

    def test_action_blocks_direct_gear_handcraft_when_automation_context_exists(self):
        command = build_modless_action_command({"type": "craft", "recipe": "iron-gear-wheel", "count": 1})

        self.assertIn("direct_gear_handcraft_guard", command)
        self.assertIn("force_has_automation_researched", command)
        self.assertIn("inventory_has_gear_blocking_assembler", command)
        self.assertIn("surface_has_gear_blocking_assembler", command)
        self.assertIn("blocked direct iron-gear-wheel handcraft", command)

    def test_action_research_completes_trigger_technology(self):
        command = build_modless_action_command({"type": "research", "technology": "automation-science-pack"})
        self.assertIn("research_unit_ingredients", command)
        self.assertIn("trigger = true", command)

    def test_build_action_is_idempotent_for_existing_entities(self):
        command = build_modless_action_command({"type": "build", "name": "offshore-pump", "position": {"x": 1, "y": 2}})
        action_build = command[command.index("local function action_build") :]

        self.assertIn("existing_built_entity", command)
        self.assertIn("expected_build_positions", command)
        self.assertIn("distance(entity.position, probe) <= 0.25", command)
        self.assertIn('status = "already_exists"', command)
        self.assertLess(action_build.index("existing_built_entity(agent.surface"), action_build.index("inventory.get_item_count"))


if __name__ == "__main__":
    unittest.main()
