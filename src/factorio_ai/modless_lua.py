from __future__ import annotations

import json
from textwrap import dedent
from typing import Any

from .config import AppConfig
from .rcon import FactorioRconClient, parse_json_response


OBSERVE_RADIUS = 768
ALLOWED_ACTION_TYPES = {
    "build",
    "chart",
    "connect_power",
    "craft",
    "insert",
    "mine",
    "move_to",
    "print",
    "research",
    "restore_character_controller",
    "set_recipe",
    "set_walking_state",
    "stop",
    "stop_walking",
    "take",
    "wait",
}
ALLOWED_DIRECTIONS = {
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
}
_CONFIRM_LUA_COMMAND = '/silent-command rcon.print("{\\"ok\\":true,\\"mode\\":\\"modless-rcon-lua\\",\\"luaConfirmed\\":true}")'


class ModlessLuaController:
    """RCON/Lua adapter for vanilla-compatible multiplayer servers without a custom mod."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    def observe(self, *, player_name: str | None = None, include_planning_sites: bool = True) -> dict[str, Any]:
        effective_player = self.cfg.agent_player_name if player_name is None else player_name
        command = build_modless_observe_command(effective_player, include_planning_sites=include_planning_sites)
        with self._client() as client:
            return execute_json_lua_command(client, command)

    def act(self, action: dict[str, Any], *, player_name: str | None = None) -> dict[str, Any]:
        effective_player = self.cfg.agent_player_name if player_name is None else player_name
        command = build_modless_action_command(action, player_name=effective_player)
        with self._client() as client:
            return execute_json_lua_command(client, command)

    def _client(self) -> FactorioRconClient:
        return FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password)


def execute_json_lua_command(client: FactorioRconClient, command: str) -> dict[str, Any]:
    confirm_lua_console(client)
    response = client.execute(command, drain_seconds=0.25)
    return parse_json_response(response)


def confirm_lua_console(client: FactorioRconClient) -> None:
    """Accept Factorio's one-time warning that Lua console commands disable achievements."""
    try:
        client.execute(_CONFIRM_LUA_COMMAND, drain_seconds=0.05)
        return
    except TimeoutError:
        pass
    client.execute(_CONFIRM_LUA_COMMAND, drain_seconds=0.05)


def build_modless_observe_command(player_name: str = "", *, include_planning_sites: bool = True) -> str:
    lua = (
        _COMMON_LUA
        + "\n"
        + _OBSERVE_LUA.replace("__PLAYER_NAME__", _lua_string(player_name)).replace(
            "__INCLUDE_PLANNING_SITES__",
            "true" if include_planning_sites else "false",
        )
    )
    return _silent_command(lua)


def build_modless_action_command(action: dict[str, Any], *, player_name: str = "") -> str:
    _validate_action(action)
    payload = json.dumps(action, ensure_ascii=False, separators=(",", ":"))
    lua = (
        _COMMON_LUA
        + "\nlocal action = helpers.json_to_table("
        + _lua_string(payload)
        + ")\nlocal default_player_name = "
        + _lua_string(player_name)
        + "\n"
        + _ACTION_LUA
    )
    return _silent_command(lua)


def _validate_action(action: dict[str, Any]) -> None:
    action_type = action.get("type")
    if action_type not in ALLOWED_ACTION_TYPES:
        raise ValueError(f"modless action is not allowlisted: {action_type}")
    if action_type == "set_walking_state":
        direction = str(action.get("direction") or "north")
        if direction not in ALLOWED_DIRECTIONS:
            raise ValueError(f"unsupported walking direction: {direction}")


def _lua_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _silent_command(lua: str) -> str:
    return "/silent-command " + _compact_lua(lua)


def _compact_lua(lua: str) -> str:
    return " ".join(line.strip() for line in dedent(lua).splitlines() if line.strip())


_COMMON_LUA = f"""
local OBSERVE_RADIUS = {OBSERVE_RADIUS}
local STARTER_RESOURCE_RADIUS = 224
local STARTER_RESOURCE_TILE_LIMIT = 1200
local REMOTE_RESOURCE_TILE_LIMIT = 220
local POWER_SITE_RADIUS = 1024
local POWER_SITE_WATER_TILE_LIMIT = 1600
local POWER_SITE_SCAN_RADII = {{ 96, 160, 224, 384, 640, POWER_SITE_RADIUS }}
local LAB_SITE_RADIUS = 96
local POLE_WIRE_REACH = 7.5
local STARTER_POLE_SUPPLY_REACH = 2.5
local GLOBAL_FORCE_ENTITY_LIMIT = 240
local AGENT_VISION_CHART_RADIUS = 96
local PRESERVED_STARTER_ARTIFACT_RADIUS = 192
local VIRTUAL_STARTER_ITEMS = {{
  ["burner-mining-drill"] = 1,
  ["stone-furnace"] = 1
}}
local VIRTUAL_RECIPES = {{
  ["firearm-magazine"] = {{ ingredients = {{ ["iron-plate"] = 4 }}, results = {{ ["firearm-magazine"] = 1 }} }},
  ["gun-turret"] = {{ ingredients = {{ ["iron-plate"] = 10, ["copper-plate"] = 5, ["iron-gear-wheel"] = 10 }}, results = {{ ["gun-turret"] = 1 }} }},
  ["stone-furnace"] = {{ ingredients = {{ stone = 5 }}, results = {{ ["stone-furnace"] = 1 }} }},
  ["wooden-chest"] = {{ ingredients = {{ wood = 2 }}, results = {{ ["wooden-chest"] = 1 }} }},
  ["iron-chest"] = {{ ingredients = {{ ["iron-plate"] = 8 }}, results = {{ ["iron-chest"] = 1 }} }},
  ["iron-gear-wheel"] = {{ ingredients = {{ ["iron-plate"] = 2 }}, results = {{ ["iron-gear-wheel"] = 1 }} }},
  ["copper-cable"] = {{ ingredients = {{ ["copper-plate"] = 1 }}, results = {{ ["copper-cable"] = 2 }} }},
  ["transport-belt"] = {{ ingredients = {{ ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["transport-belt"] = 2 }} }},
  ["burner-inserter"] = {{ ingredients = {{ ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["burner-inserter"] = 1 }} }},
  ["inserter"] = {{ ingredients = {{ ["electronic-circuit"] = 1, ["iron-gear-wheel"] = 1, ["iron-plate"] = 1 }}, results = {{ inserter = 1 }} }},
  ["long-handed-inserter"] = {{ ingredients = {{ ["iron-plate"] = 1, ["iron-gear-wheel"] = 1, inserter = 1 }}, results = {{ ["long-handed-inserter"] = 1 }} }},
  ["pipe"] = {{ ingredients = {{ ["iron-plate"] = 1 }}, results = {{ pipe = 1 }} }},
  ["boiler"] = {{ ingredients = {{ pipe = 4, ["stone-furnace"] = 1 }}, results = {{ boiler = 1 }} }},
  ["steam-engine"] = {{ ingredients = {{ ["iron-gear-wheel"] = 8, pipe = 5, ["iron-plate"] = 10 }}, results = {{ ["steam-engine"] = 1 }} }},
  ["offshore-pump"] = {{ ingredients = {{ ["electronic-circuit"] = 2, pipe = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["offshore-pump"] = 1 }} }},
  ["small-electric-pole"] = {{ ingredients = {{ wood = 1, ["copper-cable"] = 2 }}, results = {{ ["small-electric-pole"] = 2 }} }},
  ["electronic-circuit"] = {{ ingredients = {{ ["iron-plate"] = 1, ["copper-cable"] = 3 }}, results = {{ ["electronic-circuit"] = 1 }} }},
  ["burner-mining-drill"] = {{ ingredients = {{ ["iron-plate"] = 3, ["iron-gear-wheel"] = 3, stone = 5 }}, results = {{ ["burner-mining-drill"] = 1 }} }},
  ["electric-mining-drill"] = {{ ingredients = {{ ["electronic-circuit"] = 3, ["iron-gear-wheel"] = 5, ["iron-plate"] = 10 }}, results = {{ ["electric-mining-drill"] = 1 }} }},
  ["automation-science-pack"] = {{ ingredients = {{ ["copper-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["automation-science-pack"] = 1 }} }},
  ["lab"] = {{ ingredients = {{ ["electronic-circuit"] = 10, ["iron-gear-wheel"] = 10, ["transport-belt"] = 4 }}, results = {{ lab = 1 }} }},
  ["assembling-machine-1"] = {{ ingredients = {{ ["electronic-circuit"] = 3, ["iron-gear-wheel"] = 5, ["iron-plate"] = 9 }}, results = {{ ["assembling-machine-1"] = 1 }} }}
}}
local GEAR_HANDCRAFT_BLOCKING_ASSEMBLERS = {{ "assembling-machine-1", "assembling-machine-2", "assembling-machine-3" }}
local function json_reply(payload)
  local encode = helpers and helpers.table_to_json or nil
  if not encode and game and game.table_to_json then encode = game.table_to_json end
  if not encode then rcon.print('{{"ok":false,"reason":"json encoder unavailable"}}'); return end
  rcon.print(encode(payload))
end
local function round(value)
  return math.floor((value or 0) * 100 + 0.5) / 100
end
local function position_table(position)
  if not position then return {{ x = 0, y = 0 }} end
  return {{ x = round(position.x or position[1]), y = round(position.y or position[2]) }}
end
local function distance(a, b)
  local dx = (a.x or a[1]) - (b.x or b[1])
  local dy = (a.y or a[2]) - (b.y or b[2])
  return math.sqrt(dx * dx + dy * dy)
end
local function normalize_position(value)
  if type(value) ~= "table" then return nil end
  local x = value.x or value[1]
  local y = value.y or value[2]
  if type(x) ~= "number" or type(y) ~= "number" then return nil end
  return {{ x = x, y = y }}
end
local function surface_pollution(surface, position)
  local ok, value = pcall(function() return surface.get_pollution(position) end)
  if ok and type(value) == "number" then return round(value) end
  return 0
end
local function can_place_manual(surface, force, name, position, direction)
  return surface.can_place_entity({{
    name = name,
    position = position,
    direction = direction or defines.direction.north,
    force = force,
    build_check_type = defines.build_check_type.manual
  }})
end
local function resource_overlap_count(surface, position, radius)
  local found = surface.find_entities_filtered({{ position = position, radius = radius or 2.0, type = "resource", limit = 32 }})
  local count = 0
  for _, resource in pairs(found) do
    if resource.valid and (not resource.amount or resource.amount > 0) then count = count + 1 end
  end
  return count
end
local function clear_of_resources(surface, position, radius)
  return resource_overlap_count(surface, position, radius or 2.0) == 0
end
local function inventory_contents(inventory)
  local output = {{}}
  if not inventory or not inventory.valid then return output end
  local ok, raw = pcall(function() return inventory.get_contents() end)
  if not ok or not raw then return output end
  for name, count in pairs(raw) do
    if type(count) == "number" then
      output[name] = count
    elseif type(count) == "table" and count.name and count.count then
      output[count.name] = count.count
    end
  end
  return output
end
local function main_inventory(agent)
  if not agent then return nil end
  return agent.inventory
end
local function force_has_automation_researched(force)
  if not force or not force.technologies then return false end
  local technology = force.technologies["automation"]
  return technology ~= nil and technology.researched == true
end
local function inventory_has_gear_blocking_assembler(inventory)
  if not inventory or not inventory.valid then return false end
  for _, name in pairs(GEAR_HANDCRAFT_BLOCKING_ASSEMBLERS) do
    if inventory.get_item_count(name) > 0 then return true end
  end
  return false
end
local function surface_has_gear_blocking_assembler(surface, force)
  if not surface then return false end
  local ok, found = pcall(function()
    return surface.find_entities_filtered({{ name = GEAR_HANDCRAFT_BLOCKING_ASSEMBLERS, force = force, limit = 1 }})
  end)
  return ok and found and #found > 0
end
local function allow_first_assembler_bootstrap_gears(agent, inventory, action)
  if not action or action.allow_first_assembler_bootstrap ~= true then return false end
  if not agent or not inventory or not inventory.valid then return false end
  if inventory_has_gear_blocking_assembler(inventory) then return false end
  if surface_has_gear_blocking_assembler(agent.surface, agent.force) then return false end
  local count = math.max(1, math.min(action.count or 1, 100))
  if count > 5 then return false end
  local current_gears = inventory.get_item_count("iron-gear-wheel")
  if current_gears + count > 5 then return false end
  return inventory.get_item_count("electronic-circuit") >= 3 and inventory.get_item_count("iron-plate") >= 9 + (2 * count)
end
local function direct_gear_handcraft_guard(agent, inventory, recipe_name, action)
  if not agent or recipe_name ~= "iron-gear-wheel" then return nil end
  if allow_first_assembler_bootstrap_gears(agent, inventory, action) then return nil end
  if force_has_automation_researched(agent.force) then
    return "blocked direct iron-gear-wheel handcraft after Automation research; use a gear assembler, gear mall, or logistic line instead"
  end
  if inventory_has_gear_blocking_assembler(inventory) then
    return "blocked direct iron-gear-wheel handcraft because an assembler is already available; produce gears with the assembler instead"
  end
  if surface_has_gear_blocking_assembler(agent.surface, agent.force) then
    return "blocked direct iron-gear-wheel handcraft because assembler automation exists on the surface; use the gear mall instead"
  end
  return nil
end
local function entity_recipe_name(entity)
  if not entity or not entity.valid then return nil end
  local ok, recipe = pcall(function() return entity.get_recipe() end)
  if ok and recipe and recipe.name then return recipe.name end
  return nil
end
local function entity_connected_to_electric_network(entity)
  if not entity or not entity.valid then return nil end
  local ok, connected = pcall(function() return entity.is_connected_to_electric_network() end)
  if ok then return connected end
  return nil
end
local function entity_electric_network_id(entity)
  if not entity or not entity.valid then return nil end
  local ok, network_id = pcall(function() return entity.electric_network_id end)
  if ok then return network_id end
  return nil
end
local function force_spawn_position(surface, force)
  local ok, spawn = pcall(function() return force.get_spawn_position(surface) end)
  if ok and spawn then
    return {{ x = spawn.x or spawn[1] or 0, y = spawn.y or spawn[2] or 0 }}
  end
  return {{ x = 0, y = 0 }}
end
local function optional_entity_position(entity, property)
  if not entity or not entity.valid then return nil end
  local ok, value = pcall(function() return entity[property] end)
  if ok and value then return position_table(value) end
  return nil
end
local function entity_inventory_snapshot(entity)
  local result = {{}}
  if not entity or not entity.valid then return result end
  for _, inventory_id in pairs(defines.inventory) do
    local ok, inventory = pcall(function() return entity.get_inventory(inventory_id) end)
    if ok and inventory and inventory.valid and not inventory.is_empty() then
      result[tostring(inventory_id)] = inventory_contents(inventory)
    end
  end
  return result
end
local function entity_fluidbox_snapshot(entity)
  local result = {{}}
  if not entity or not entity.valid then return result end
  local ok, fluidbox = pcall(function() return entity.fluidbox end)
  if not ok or not fluidbox then return result end
  for i = 1, #fluidbox do
    local fluid = fluidbox[i]
    if fluid and fluid.name then
      result[tostring(i)] = {{ name = fluid.name, amount = round(fluid.amount or 0), temperature = round(fluid.temperature or 0) }}
    end
  end
  return result
end
local function entity_mining_target_name(entity)
  local ok, target = pcall(function() return entity.mining_target end)
  if ok and target and target.valid then return target.name end
  return nil
end
local function entity_status_name(entity)
  local status = entity.status
  if not status then return nil end
  for name, value in pairs(defines.entity_status) do
    if value == status then return name end
  end
  return nil
end
local function entity_snapshot(entity, origin)
  return {{
    unit_number = entity.unit_number,
    name = entity.name,
    type = entity.type,
    force = entity.force and entity.force.name or nil,
    health = entity.health,
    position = position_table(entity.position),
    direction = entity.direction,
    status = entity.status,
    status_name = entity_status_name(entity),
    recipe = entity_recipe_name(entity),
    electric_network_connected = entity_connected_to_electric_network(entity),
    electric_network_id = entity_electric_network_id(entity),
    mining_target = entity_mining_target_name(entity),
    drop_position = optional_entity_position(entity, "drop_position"),
    pickup_position = optional_entity_position(entity, "pickup_position"),
    distance = round(distance(origin, entity.position)),
    inventories = entity_inventory_snapshot(entity),
    fluids = entity_fluidbox_snapshot(entity)
  }}
end
local function add_unique_entity_snapshot(rows, seen, entity, origin)
  if not entity.valid then return end
  local key = entity.unit_number and tostring(entity.unit_number) or (entity.name .. ":" .. tostring(entity.position.x) .. ":" .. tostring(entity.position.y))
  if seen[key] then return end
  seen[key] = true
  table.insert(rows, entity_snapshot(entity, origin))
end
local function ensure_server_agent()
  storage.factorio_ai_agent = storage.factorio_ai_agent or {{}}
  local agent = storage.factorio_ai_agent
  local surface = game.get_surface(agent.surface_name or "nauvis") or game.surfaces[1]
  if not agent.position then
    local spawn = game.forces.player.get_spawn_position(surface)
    agent.position = {{ x = spawn.x or spawn[1], y = spawn.y or spawn[2] }}
    agent.surface_name = surface.name
  end
  if not agent.inventory or not agent.inventory.valid then
    agent.inventory = game.create_inventory(200)
    for name, count in pairs(VIRTUAL_STARTER_ITEMS) do
      agent.inventory.insert({{ name = name, count = count }})
    end
  end
  surface = game.get_surface(agent.surface_name or "nauvis") or surface
  return {{ kind = "server", name = "server", player = nil, surface = surface, force = game.forces.player, position = agent.position, inventory = agent.inventory, character_valid = false, move = {{ active = false }} }}
end
local function player_move_state(player)
  if not player or not player.character or not player.character.valid then return {{ active = false }} end
  local state = player.walking_state
  if type(state) ~= "table" then return {{ active = false }} end
  return {{ active = state.walking == true, direction = state.direction }}
end
local function player_actual_position(player)
  if player and player.character and player.character.valid then return player.character.position end
  return player.position
end
local function player_controller_is_character(player)
  return player and player.controller_type == defines.controllers.character
end
local function auto_select_player_name(player_name)
  if type(player_name) ~= "string" or player_name == "" then return true end
  local normalized = string.lower(player_name)
  return normalized == "auto" or normalized == "connected" or normalized == "first-connected" or normalized == "*"
end
local function find_agent(player_name)
  if not auto_select_player_name(player_name) then
    local named = game.get_player(player_name)
    if named and named.valid then
      return {{ kind = "player", name = named.name, player = named, surface = named.surface, force = named.force, position = player_actual_position(named), inventory = named.get_main_inventory(), character_valid = named.character ~= nil and named.character.valid or false, move = player_move_state(named), controller_type = named.controller_type, controller_is_character = player_controller_is_character(named) }}
    end
    return ensure_server_agent()
  end
  for _, player in pairs(game.connected_players) do
    if player and player.valid then
      return {{ kind = "player", name = player.name, player = player, surface = player.surface, force = player.force, position = player_actual_position(player), inventory = player.get_main_inventory(), character_valid = player.character ~= nil and player.character.valid or false, move = player_move_state(player), controller_type = player.controller_type, controller_is_character = player_controller_is_character(player) }}
    end
  end
  for _, player in pairs(game.players) do
    if player and player.valid and player.character and player.character.valid then
      return {{ kind = "player", name = player.name, player = player, surface = player.surface, force = player.force, position = player_actual_position(player), inventory = player.get_main_inventory(), character_valid = true, move = player_move_state(player), controller_type = player.controller_type, controller_is_character = player_controller_is_character(player) }}
    end
  end
  return ensure_server_agent()
end
local function marker_position(value, fallback)
  local position = normalize_position(value)
  if position then return position end
  return fallback
end
local function chart_area_around(agent, position)
  if not agent or not agent.force or not agent.surface or not position then return end
  local radius = AGENT_VISION_CHART_RADIUS
  pcall(function()
    agent.force.chart(agent.surface, {{ {{ x = position.x - radius, y = position.y - radius }}, {{ x = position.x + radius, y = position.y + radius }} }})
  end)
end
local function update_agent_chart_marker(agent, label, position)
  if not agent or not agent.force or not agent.surface then return nil end
  local target = marker_position(position, agent.position)
  chart_area_around(agent, agent.position)
  chart_area_around(agent, target)
  pcall(function()
    local tags = agent.force.find_chart_tags(agent.surface)
    for _, tag in pairs(tags or {{}}) do
      if tag and tag.valid and type(tag.text) == "string" and string.sub(tag.text, 1, 4) == "[AI]" then
        tag.destroy()
      end
    end
  end)
  local tag_number = nil
  pcall(function()
    local tag = agent.force.add_chart_tag(agent.surface, {{ position = target, text = "[AI] " .. tostring(label or "idle") }})
    if tag and tag.valid then tag_number = tag.tag_number end
  end)
  return tag_number
end
local function remember_agent_marker(agent, action_type, detail, target_position)
  storage.factorio_ai_agent = storage.factorio_ai_agent or {{}}
  local target = marker_position(target_position, agent.position)
  local label = tostring(action_type or "idle")
  if detail and detail ~= "" then label = label .. ": " .. tostring(detail) end
  local marker = {{
    name = agent.name,
    kind = agent.kind,
    surface = agent.surface and agent.surface.name or nil,
    position = position_table(agent.position),
    target_position = position_table(target),
    last_action = tostring(action_type or "idle"),
    detail = tostring(detail or ""),
    tick = game.tick
  }}
  storage.factorio_ai_agent.last_marker = marker
  marker.chart_tag_number = update_agent_chart_marker(agent, label, target)
  return marker
end
local function agent_marker_snapshot(agent)
  storage.factorio_ai_agent = storage.factorio_ai_agent or {{}}
  local marker = storage.factorio_ai_agent.last_marker
  if not marker then
    marker = remember_agent_marker(agent, "idle", "waiting for strategy", agent.position)
  else
    marker.position = position_table(agent.position)
    marker.kind = agent.kind
    marker.name = agent.name
    marker.surface = agent.surface and agent.surface.name or marker.surface
    update_agent_chart_marker(agent, marker.last_action or "idle", marker.target_position or agent.position)
  end
  return marker
end
"""


_OBSERVE_LUA = """
local agent = find_agent(__PLAYER_NAME__)
local surface = agent.surface
local origin = agent.position
local function collect_craftable()
  local craftable = {}
  if not agent.player then
    local inventory = main_inventory(agent)
    if not inventory or not inventory.valid then return craftable end
    for recipe_name, recipe in pairs(VIRTUAL_RECIPES) do
      local possible = 999999
      for ingredient, count in pairs(recipe.ingredients) do
        possible = math.min(possible, math.floor(inventory.get_item_count(ingredient) / count))
      end
      if possible > 0 and possible < 999999 then craftable[recipe_name] = possible end
    end
    return craftable
  end
  for name, recipe in pairs(agent.force.recipes) do
    if recipe.enabled then
      local ok, count = pcall(function() return agent.player.get_craftable_count(name) end)
      if ok and count and count > 0 then craftable[name] = count end
    end
  end
  return craftable
end
local function collect_resources(anchor_position)
  local resources = {}
  local seen = {}
  local source_specs = {}
  if anchor_position then
    table.insert(source_specs, { position = anchor_position, radius = STARTER_RESOURCE_RADIUS, limit = STARTER_RESOURCE_TILE_LIMIT, source = "base" })
  end
  table.insert(source_specs, { position = origin, radius = OBSERVE_RADIUS, limit = REMOTE_RESOURCE_TILE_LIMIT, source = "agent" })
  for _, resource_name in pairs({ "iron-ore", "coal", "stone", "copper-ore", "uranium-ore", "crude-oil" }) do
    for _, source_spec in pairs(source_specs) do
      local found = surface.find_entities_filtered({ position = source_spec.position, radius = source_spec.radius, type = "resource", name = resource_name, limit = source_spec.limit })
      for _, entity in pairs(found) do
        if not entity.amount or entity.amount > 0 then
          local key = tostring(entity.unit_number or "") .. ":" .. entity.name .. ":" .. tostring(entity.position.x) .. "," .. tostring(entity.position.y)
          if not seen[key] then
            seen[key] = true
            table.insert(resources, {
              unit_number = entity.unit_number,
              name = entity.name,
              amount = entity.amount,
              position = position_table(entity.position),
              distance = round(distance(origin, entity.position)),
              distance_from_agent = round(distance(origin, entity.position)),
              distance_from_base = anchor_position and round(distance(anchor_position, entity.position)) or nil,
              observed_from = source_spec.source
            })
          end
        end
      end
    end
  end
  table.sort(resources, function(a, b) return a.distance < b.distance end)
  return resources
end
local function collect_entities()
  local rows = {}
  local seen = {}
  local names = {
    "burner-mining-drill", "electric-mining-drill", "stone-furnace", "steel-furnace", "electric-furnace",
    "wooden-chest", "iron-chest", "steel-chest",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "lab",
    "boiler", "steam-engine", "steam-turbine", "offshore-pump", "solar-panel", "accumulator",
    "pipe", "pipe-to-ground", "pump", "storage-tank",
    "transport-belt", "fast-transport-belt", "express-transport-belt",
    "underground-belt", "fast-underground-belt", "express-underground-belt",
    "splitter", "fast-splitter", "express-splitter",
    "burner-inserter", "inserter", "long-handed-inserter", "fast-inserter", "stack-inserter",
    "small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation",
    "gun-turret", "laser-turret", "flamethrower-turret", "stone-wall", "gate",
    "straight-rail", "curved-rail-a", "curved-rail-b", "rail-signal", "rail-chain-signal", "train-stop",
    "locomotive", "cargo-wagon", "fluid-wagon",
    "pumpjack", "oil-refinery", "chemical-plant", "centrifuge", "rocket-silo", "roboport"
  }
  for _, name in pairs(names) do
    local found = surface.find_entities_filtered({ position = origin, radius = OBSERVE_RADIUS, name = name, limit = 160 })
    for _, entity in pairs(found) do add_unique_entity_snapshot(rows, seen, entity, origin) end
  end
  local global_force_names = {
    "burner-mining-drill", "electric-mining-drill", "stone-furnace", "steel-furnace", "electric-furnace",
    "wooden-chest", "iron-chest", "steel-chest",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "lab",
    "boiler", "steam-engine", "steam-turbine", "offshore-pump", "solar-panel", "accumulator",
    "transport-belt", "fast-transport-belt", "express-transport-belt",
    "burner-inserter", "inserter", "long-handed-inserter", "fast-inserter", "stack-inserter",
    "small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation",
    "gun-turret", "laser-turret", "flamethrower-turret", "stone-wall", "gate",
    "straight-rail", "curved-rail-a", "curved-rail-b", "rail-signal", "rail-chain-signal", "train-stop",
    "locomotive", "cargo-wagon", "fluid-wagon",
    "pumpjack", "oil-refinery", "chemical-plant", "centrifuge", "rocket-silo", "roboport"
  }
  for _, name in pairs(global_force_names) do
    local ok_global, found = pcall(function()
      return surface.find_entities_filtered({ force = agent.force, name = name, limit = GLOBAL_FORCE_ENTITY_LIMIT })
    end)
    if ok_global and found then
      for _, entity in pairs(found) do add_unique_entity_snapshot(rows, seen, entity, origin) end
    end
  end
  local immediate_trees = surface.find_entities_filtered({ position = origin, radius = 12, type = "tree", limit = 160 })
  for _, entity in pairs(immediate_trees) do add_unique_entity_snapshot(rows, seen, entity, origin) end
  local nearby_trees = surface.find_entities_filtered({ position = origin, radius = 48, type = "tree", limit = 160 })
  for _, entity in pairs(nearby_trees) do add_unique_entity_snapshot(rows, seen, entity, origin) end
  local trees = surface.find_entities_filtered({ position = origin, radius = OBSERVE_RADIUS, type = "tree", limit = 80 })
  for _, entity in pairs(trees) do add_unique_entity_snapshot(rows, seen, entity, origin) end
  local nearby = surface.find_entities_filtered({ position = origin, radius = 32, limit = 120 })
  for _, entity in pairs(nearby) do
    if entity.valid and entity.name ~= "character" and entity.type ~= "resource" then add_unique_entity_snapshot(rows, seen, entity, origin) end
  end
  table.sort(rows, function(a, b) return a.distance < b.distance end)
  return rows
end
local function collect_enemies()
  local rows = {}
  local enemy_force = game.forces.enemy
  for _, enemy_type in pairs({ "unit", "unit-spawner", "turret" }) do
    local found = surface.find_entities_filtered({ position = origin, radius = OBSERVE_RADIUS, force = enemy_force, type = enemy_type, limit = 160 })
    for _, entity in pairs(found) do
      if entity.valid and entity.force ~= agent.force then
        local row = entity_snapshot(entity, origin)
        row.pollution = surface_pollution(surface, entity.position)
        table.insert(rows, row)
      end
    end
  end
  table.sort(rows, function(a, b) return a.distance < b.distance end)
  return rows
end
local function technology_snapshot(technology)
  if not technology or not technology.valid then return nil end
  local ingredients = {}
  local ok_ingredients, raw_ingredients = pcall(function() return technology.research_unit_ingredients end)
  if ok_ingredients and raw_ingredients then
    for _, ingredient in pairs(raw_ingredients) do
      local name = ingredient.name or ingredient[1]
      local amount = ingredient.amount or ingredient[2]
      if name and amount then ingredients[name] = amount end
    end
  end
  local unit_count = nil
  local unit_energy = nil
  pcall(function() unit_count = technology.research_unit_count end)
  pcall(function() unit_energy = technology.research_unit_energy end)
  return { name = technology.name, researched = technology.researched, enabled = technology.enabled, research_unit_count = unit_count, research_unit_energy = unit_energy, ingredients = ingredients }
end
local function collect_research()
  local watched = {
    "steam-power", "electronics", "automation-science-pack", "automation", "logistics", "long-inserters",
    "steel-processing", "advanced-material-processing", "advanced-material-processing-2",
    "automation-2", "automation-3", "fast-inserter", "stack-inserter",
    "modules", "speed-module", "speed-module-2", "speed-module-3",
    "productivity-module", "productivity-module-2", "productivity-module-3",
    "efficiency-module", "efficiency-module-2", "efficiency-module-3",
    "effect-transmission", "electric-energy-distribution-1", "electric-energy-distribution-2",
    "railway", "oil-processing", "rocket-silo"
  }
  local technologies = {}
  for _, name in pairs(watched) do
    local snapshot = technology_snapshot(agent.force.technologies[name])
    if snapshot then technologies[name] = snapshot end
  end
  local current = nil
  local progress = nil
  pcall(function() if agent.force.current_research then current = agent.force.current_research.name end end)
  pcall(function() progress = agent.force.research_progress end)
  return { current = current, progress = progress, technologies = technologies }
end
local function collect_recipe_unlocks()
  local watched = {
    "long-handed-inserter", "fast-inserter", "stack-inserter", "bulk-inserter",
    "assembling-machine-2", "assembling-machine-3",
    "steel-furnace", "electric-furnace",
    "speed-module", "speed-module-2", "speed-module-3",
    "productivity-module", "productivity-module-2", "productivity-module-3",
    "efficiency-module", "efficiency-module-2", "efficiency-module-3",
    "beacon", "medium-electric-pole", "big-electric-pole", "substation"
  }
  local recipes = {}
  for _, name in pairs(watched) do
    local recipe = agent.force.recipes[name]
    if recipe then
      recipes[name] = { name = recipe.name, enabled = recipe.enabled }
    end
  end
  return recipes
end
local function rotate_offset(offset, turns)
  local x = offset.x
  local y = offset.y
  for _ = 1, turns do
    x, y = -y, x
  end
  return { x = x, y = y }
end
local function rotate_direction(direction, turns)
  local dirs = { defines.direction.north, defines.direction.east, defines.direction.south, defines.direction.west }
  local index = 1
  for i, value in pairs(dirs) do
    if value == direction then index = i end
  end
  return dirs[((index - 1 + turns) % 4) + 1]
end
local function turns_from_west(direction)
  if direction == defines.direction.north then return 1 end
  if direction == defines.direction.east then return 2 end
  if direction == defines.direction.south then return 3 end
  return 0
end
local function offset_position(position, offset)
  return { x = position.x + offset.x, y = position.y + offset.y }
end
local function power_layout(position, direction)
  local turns = turns_from_west(direction)
  return {
    offshore_pump = {
      name = "offshore-pump",
      position = position,
      direction = direction
    },
    boiler = {
      name = "boiler",
      position = offset_position(position, rotate_offset({ x = 2, y = -1 }, turns)),
      direction = rotate_direction(defines.direction.north, turns)
    },
    steam_engine = {
      name = "steam-engine",
      position = offset_position(position, rotate_offset({ x = 2, y = -4 }, turns)),
      direction = rotate_direction(defines.direction.north, turns)
    },
    small_electric_pole = {
      name = "small-electric-pole",
      position = offset_position(position, rotate_offset({ x = 0, y = -4 }, turns)),
      direction = defines.direction.north
    }
  }
end
local function power_layout_can_place(surface, force, layout)
  return can_place_manual(surface, force, "offshore-pump", layout.offshore_pump.position, layout.offshore_pump.direction)
    and can_place_manual(surface, force, "boiler", layout.boiler.position, layout.boiler.direction)
    and can_place_manual(surface, force, "steam-engine", layout.steam_engine.position, layout.steam_engine.direction)
    and can_place_manual(surface, force, "small-electric-pole", layout.small_electric_pole.position, layout.small_electric_pole.direction)
    and clear_of_resources(surface, layout.boiler.position, 2.0)
    and clear_of_resources(surface, layout.steam_engine.position, 3.0)
    and clear_of_resources(surface, layout.small_electric_pole.position, 1.0)
end
local function collect_power_sites(surface, position, force, player_position)
  local sites = {}
  local checked = {}
  for _, scan_radius in pairs(POWER_SITE_SCAN_RADII) do
    local water_tiles = surface.find_tiles_filtered({
      position = position,
      radius = scan_radius,
      name = { "water", "deepwater", "water-green", "deepwater-green" },
      limit = POWER_SITE_WATER_TILE_LIMIT
    })
    table.sort(water_tiles, function(a, b) return distance(position, a.position) < distance(position, b.position) end)
    for _, tile in pairs(water_tiles) do
      local tile_position = tile.position
      for dx = -7, 7 do
        for dy = -7, 7 do
          local pump_position = { x = tile_position.x + dx + 0.5, y = tile_position.y + dy + 0.5 }
          for _, direction in pairs({ defines.direction.west, defines.direction.north, defines.direction.east, defines.direction.south }) do
            local key = tostring(pump_position.x) .. "," .. tostring(pump_position.y) .. ":" .. tostring(direction)
            if not checked[key] then
              checked[key] = true
              local layout = power_layout(pump_position, direction)
              if power_layout_can_place(surface, force, layout) then
                table.insert(sites, {
                  distance = round(distance(position, pump_position)),
                  distance_from_agent = player_position and round(distance(player_position, pump_position)) or nil,
                  anchor_position = position_table(position),
                  layout = layout
                })
                if #sites >= 20 then
                  table.sort(sites, function(a, b) return a.distance < b.distance end)
                  return sites
                end
              end
            end
          end
        end
      end
    end
    if #sites > 0 then
      table.sort(sites, function(a, b) return a.distance < b.distance end)
      return sites
    end
  end
  table.sort(sites, function(a, b) return a.distance < b.distance end)
  return sites
end
local function add_lab_site(sites, checked, surface, force, pole, pole_position, lab_position, source_pole)
  if not can_place_manual(surface, force, "lab", lab_position, defines.direction.north) then return end
  if not clear_of_resources(surface, lab_position, 2.5) then return end
  local key = tostring(round(pole_position.x)) .. "," .. tostring(round(pole_position.y)) .. ":" .. tostring(round(lab_position.x)) .. "," .. tostring(round(lab_position.y))
  if checked[key] then return end
  checked[key] = true
  local site = {
    pole_position = position_table(pole_position),
    lab_position = position_table(lab_position),
    distance = round(distance(source_pole.position, lab_position)),
    powered = entity_connected_to_electric_network(source_pole)
  }
  if pole and pole.valid then site.pole_unit_number = pole.unit_number else site.source_pole_unit_number = source_pole.unit_number end
  table.insert(sites, site)
end
local function add_lab_sites_around_pole(sites, checked, surface, force, pole, pole_position, source_pole)
  for dx = -2, 2 do
    for dy = -2, 2 do
      local lab_position = { x = pole_position.x + dx, y = pole_position.y + dy }
      if distance(pole_position, lab_position) <= STARTER_POLE_SUPPLY_REACH then
        add_lab_site(sites, checked, surface, force, pole, pole_position, lab_position, source_pole)
        if #sites >= 40 then return end
      end
    end
  end
end
local function collect_electric_source_poles(surface, position, force)
  local poles = {}
  local seen = {}
  local names = { "small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation" }
  local function add_pole(entity)
    if not entity or not entity.valid then return end
    local key = entity.unit_number or (entity.name .. ":" .. tostring(entity.position.x) .. "," .. tostring(entity.position.y))
    if seen[key] then return end
    seen[key] = true
    table.insert(poles, entity)
  end
  for _, name in pairs(names) do
    local found = surface.find_entities_filtered({ position = position, radius = OBSERVE_RADIUS, name = name, limit = 80 })
    for _, entity in pairs(found) do add_pole(entity) end
  end
  for _, name in pairs(names) do
    local ok_global, found = pcall(function()
      return surface.find_entities_filtered({ force = force, name = name, limit = GLOBAL_FORCE_ENTITY_LIMIT })
    end)
    if ok_global and found then
      for _, entity in pairs(found) do add_pole(entity) end
    end
  end
  table.sort(poles, function(a, b)
    local ap = entity_connected_to_electric_network(a) and 0 or 1
    local bp = entity_connected_to_electric_network(b) and 0 or 1
    if ap ~= bp then return ap < bp end
    return distance(position, a.position) < distance(position, b.position)
  end)
  return poles
end
local function collect_lab_sites(surface, position, force)
  local sites = {}
  local checked = {}
  local poles = collect_electric_source_poles(surface, position, force)
  for _, source_pole in pairs(poles) do
    if source_pole.valid then
      local source_position = { x = source_pole.position.x, y = source_pole.position.y }
      add_lab_sites_around_pole(sites, checked, surface, force, source_pole, source_position, source_pole)
      if #sites >= 40 then break end
      for dx = -7, 7 do
        for dy = -7, 7 do
          local pole_position = { x = source_pole.position.x + dx, y = source_pole.position.y + dy }
          if distance(source_pole.position, pole_position) <= POLE_WIRE_REACH
            and can_place_manual(surface, force, "small-electric-pole", pole_position, defines.direction.north) then
            add_lab_sites_around_pole(sites, checked, surface, force, nil, pole_position, source_pole)
            if #sites >= 40 then break end
          end
        end
        if #sites >= 40 then break end
      end
    end
    if #sites >= 40 then break end
  end
  table.sort(sites, function(a, b)
    if a.powered ~= b.powered then return a.powered == true end
    return (a.distance or 999999) < (b.distance or 999999)
  end)
  return sites
end
local function automation_cell_layout(pole_position)
  return {
    pole_position = { x = pole_position.x, y = pole_position.y },
    cable_assembler_position = { x = pole_position.x - 2, y = pole_position.y + 2 },
    circuit_assembler_position = { x = pole_position.x + 2, y = pole_position.y + 2 },
    transfer_inserter_position = { x = pole_position.x, y = pole_position.y + 2 },
    transfer_inserter_direction = defines.direction.east
  }
end
local function add_automation_site(sites, checked, surface, force, pole, pole_position, source_pole)
  local layout = automation_cell_layout(pole_position)
  if not pole and not can_place_manual(surface, force, "small-electric-pole", layout.pole_position, defines.direction.north) then return end
  if not clear_of_resources(surface, layout.pole_position, 1.0)
    or not clear_of_resources(surface, layout.cable_assembler_position, 2.5)
    or not clear_of_resources(surface, layout.circuit_assembler_position, 2.5)
    or not clear_of_resources(surface, layout.transfer_inserter_position, 1.0) then
    return
  end
  if not can_place_manual(surface, force, "assembling-machine-1", layout.cable_assembler_position, defines.direction.north)
    or not can_place_manual(surface, force, "assembling-machine-1", layout.circuit_assembler_position, defines.direction.north)
    or not can_place_manual(surface, force, "inserter", layout.transfer_inserter_position, layout.transfer_inserter_direction) then
    return
  end
  local key = tostring(round(layout.pole_position.x)) .. "," .. tostring(round(layout.pole_position.y))
  if checked[key] then return end
  checked[key] = true
  local site = {
    pole_position = position_table(layout.pole_position),
    cable_assembler_position = position_table(layout.cable_assembler_position),
    circuit_assembler_position = position_table(layout.circuit_assembler_position),
    transfer_inserter_position = position_table(layout.transfer_inserter_position),
    transfer_inserter_direction = layout.transfer_inserter_direction,
    distance = round(distance(source_pole.position, layout.transfer_inserter_position)),
    powered = entity_connected_to_electric_network(source_pole)
  }
  if pole and pole.valid then site.pole_unit_number = pole.unit_number else site.source_pole_unit_number = source_pole.unit_number end
  table.insert(sites, site)
end
local function collect_automation_sites(surface, position, force)
  local sites = {}
  local checked = {}
  local poles = collect_electric_source_poles(surface, position, force)
  for _, source_pole in pairs(poles) do
    if source_pole.valid then
      local source_position = { x = source_pole.position.x, y = source_pole.position.y }
      add_automation_site(sites, checked, surface, force, source_pole, source_position, source_pole)
      if #sites >= 40 then break end
      for dx = -8, 8 do
        for dy = -8, 8 do
          local pole_position = { x = source_pole.position.x + dx, y = source_pole.position.y + dy }
          if distance(source_pole.position, pole_position) <= POLE_WIRE_REACH then
            add_automation_site(sites, checked, surface, force, nil, pole_position, source_pole)
            if #sites >= 40 then break end
          end
        end
        if #sites >= 40 then break end
      end
    end
    if #sites >= 40 then break end
  end
  table.sort(sites, function(a, b)
    if a.powered ~= b.powered then return a.powered == true end
    return (a.distance or 999999) < (b.distance or 999999)
  end)
  return sites
end
local base_anchor = force_spawn_position(surface, agent.force)
local include_planning_sites = __INCLUDE_PLANNING_SITES__
local power_sites = {}
local lab_sites = {}
local automation_sites = {}
if include_planning_sites then
  power_sites = collect_power_sites(surface, base_anchor, agent.force, origin)
  lab_sites = collect_lab_sites(surface, origin, agent.force)
  automation_sites = collect_automation_sites(surface, origin, agent.force)
end
json_reply({
  ok = true,
  mode = "modless-rcon-lua",
  tick = game.tick,
  player = { name = agent.name, kind = agent.kind, position = position_table(origin), surface = surface.name, character_valid = agent.character_valid, move = agent.move or { active = false }, controller_type = agent.controller_type, controller_is_character = agent.controller_is_character },
  execution = { mode = agent.kind == "server" and "virtual" or "player", agent_kind = agent.kind, agent_name = agent.name, character_valid = agent.character_valid, virtual = agent.kind == "server", controller_type = agent.controller_type, controller_is_character = agent.controller_is_character },
  base = { anchor_position = position_table(base_anchor), spawn_position = position_table(base_anchor) },
  inventory = inventory_contents(agent.inventory),
  agent_marker = agent_marker_snapshot(agent),
  craftable = collect_craftable(),
  resources = collect_resources(base_anchor),
  entities = collect_entities(),
  enemies = collect_enemies(),
  power_sites = power_sites,
  lab_sites = lab_sites,
  automation_sites = automation_sites,
  pollution = { at_player = surface_pollution(surface, origin) },
  factory_events = {},
  research = collect_research(),
  recipe_unlocks = collect_recipe_unlocks()
})
"""


_ACTION_LUA = """
local function ok(extra)
  extra = extra or {}
  extra.ok = true
  extra.mode = "modless-rcon-lua"
  return extra
end
local function err(reason, extra)
  extra = extra or {}
  extra.ok = false
  extra.reason = reason
  extra.mode = "modless-rcon-lua"
  return extra
end
local function action_player()
  local name = action.player_name or default_player_name
  local agent = find_agent(name)
  if agent and agent.player then return agent.player end
  return nil
end
local directions = {
  north = defines.direction.north,
  northeast = defines.direction.northeast,
  east = defines.direction.east,
  southeast = defines.direction.southeast,
  south = defines.direction.south,
  southwest = defines.direction.southwest,
  west = defines.direction.west,
  northwest = defines.direction.northwest
}
local agent = find_agent(action.player_name or default_player_name)
local function set_server_agent_position(position)
  if agent.kind ~= "server" then return end
  storage.factorio_ai_agent = storage.factorio_ai_agent or {}
  storage.factorio_ai_agent.position = { x = position.x, y = position.y }
  storage.factorio_ai_agent.surface_name = agent.surface.name
  agent.position = storage.factorio_ai_agent.position
end
local function action_marker_target(result)
  if action.position then return normalize_position(action.position) end
  if action.near then return normalize_position(action.near) end
  if result and result.target then return normalize_position(result.target) end
  if result and result.position then return normalize_position(result.position) end
  return agent.position
end
local function action_marker_detail(result)
  if action.name then return tostring(action.name) end
  if action.item then return tostring(action.item) end
  if action.recipe then return tostring(action.recipe) end
  if action.technology then return tostring(action.technology) end
  if result and result.reason then return tostring(result.reason) end
  if result and result.status then return tostring(result.status) end
  return ""
end
local function reply_action(result)
  result = result or err("action returned no result")
  result.agent = { name = agent.name, kind = agent.kind, character_valid = agent.character_valid }
  result.execution = { mode = agent.kind == "server" and "virtual" or "player", agent_kind = agent.kind, agent_name = agent.name, character_valid = agent.character_valid, virtual = agent.kind == "server" or result.virtual == true }
  result.agent_marker = remember_agent_marker(agent, action.type, action_marker_detail(result), action_marker_target(result))
  json_reply(result)
end
local function walking_direction(dx, dy)
  local x = 0
  local y = 0
  if dx > 0.15 then x = 1 elseif dx < -0.15 then x = -1 end
  if dy > 0.15 then y = 1 elseif dy < -0.15 then y = -1 end
  if x == 0 and y < 0 then return defines.direction.north end
  if x > 0 and y < 0 then return defines.direction.northeast end
  if x > 0 and y == 0 then return defines.direction.east end
  if x > 0 and y > 0 then return defines.direction.southeast end
  if x == 0 and y > 0 then return defines.direction.south end
  if x < 0 and y > 0 then return defines.direction.southwest end
  if x < 0 and y == 0 then return defines.direction.west end
  if x < 0 and y < 0 then return defines.direction.northwest end
  return defines.direction.north
end
local function find_resource(surface, request)
  local position = normalize_position(request.position)
  if position then
    local found = surface.find_entities_filtered({ position = position, radius = request.radius or 1.0, type = "resource", name = request.name, limit = 1 })
    return found[1]
  end
  local center = normalize_position(request.near)
  if not center then return nil end
  local found = surface.find_entities_filtered({ position = center, radius = request.radius or OBSERVE_RADIUS, type = "resource", name = request.name, limit = 32 })
  local available = {}
  for _, entity in pairs(found) do
    if entity.valid and (not entity.amount or entity.amount > 0) then table.insert(available, entity) end
  end
  table.sort(available, function(a, b) return distance(center, a.position) < distance(center, b.position) end)
  return available[1]
end
local function find_entity(surface, request)
  if request.unit_number then
    local entity = game.get_entity_by_unit_number(request.unit_number)
    if entity and entity.valid then return entity end
  end
  local position = normalize_position(request.position)
  if not position then return nil end
  local found = surface.find_entities_filtered({ position = position, radius = request.radius or 1.0, name = request.name, limit = 1 })
  return found[1]
end
local function entity_wire_connector(entity)
  if not entity or not entity.valid then return nil end
  local ok, connector = pcall(function() return entity.get_wire_connector(defines.wire_connector_id.pole_copper, true) end)
  if ok and connector and connector.valid then return connector end
  return nil
end
local function connector_connection_count(connector)
  if not connector or not connector.valid then return 0 end
  local ok, count = pcall(function() return connector.real_connection_count end)
  if ok and count then return count end
  return 0
end
local function connectors_connected(a, b)
  if not a or not b or not a.valid or not b.valid then return false end
  local ok, connected = pcall(function() return a.is_connected_to(b) end)
  return ok and connected == true
end
local function connectors_can_reach(a, b)
  if not a or not b or not a.valid or not b.valid then return false end
  local ok, can_reach = pcall(function() return a.can_wire_reach(b) end)
  return ok and can_reach == true
end
local function nearest_resource_name(surface, position, radius)
  local found = surface.find_entities_filtered({ position = position, radius = radius or 4, type = "resource", limit = 80 })
  local nearest = nil
  local nearest_distance = nil
  for _, resource in pairs(found) do
    if resource.valid then
      local resource_distance = distance(position, resource.position)
      if not nearest or resource_distance < nearest_distance then
        nearest = resource
        nearest_distance = resource_distance
      end
    end
  end
  return nearest and nearest.name or nil
end
local function is_preserved_starter_artifact(agent, entity)
  if not entity or not entity.valid then return false end
  local name = string.lower(tostring(entity.name or ""))
  if not (string.find(name, "crash", 1, true) or string.find(name, "wreck", 1, true) or string.find(name, "spaceship", 1, true)) then
    return false
  end
  local spawn = force_spawn_position(entity.surface or agent.surface, agent.force)
  return distance(spawn, entity.position) <= PRESERVED_STARTER_ARTIFACT_RADIUS
end
local function build_candidate_valid(surface, force, request, position, direction)
  if not surface.can_place_entity({ name = request.name, position = position, direction = direction, force = force, build_check_type = defines.build_check_type.manual }) then
    return false
  end
  if request.required_resource then
    return nearest_resource_name(surface, position, request.required_resource_radius or 4) == request.required_resource
  end
  return true
end
local function expected_build_positions(position)
  local positions = { position }
  local rounded_x = math.floor(position.x + 0.5)
  local rounded_y = math.floor(position.y + 0.5)
  if math.abs(position.x - rounded_x) < 0.001 and math.abs(position.y - rounded_y) < 0.001 then
    table.insert(positions, { x = position.x + 0.5, y = position.y + 0.5 })
  end
  return positions
end
local function existing_built_entity(surface, force, name, position)
  local found = surface.find_entities_filtered({ position = position, radius = 1.25, name = name, force = force, limit = 16 })
  local probes = expected_build_positions(position)
  for _, entity in pairs(found) do
    if entity.valid then
      for _, probe in pairs(probes) do
        if distance(entity.position, probe) <= 0.25 then return entity end
      end
    end
  end
  return nil
end
local function virtual_craft(recipe_name, count)
  local recipe = VIRTUAL_RECIPES[recipe_name]
  if not recipe then return 0 end
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return 0 end
  local possible = count
  for ingredient, ingredient_count in pairs(recipe.ingredients) do
    possible = math.min(possible, math.floor(inventory.get_item_count(ingredient) / ingredient_count))
  end
  if possible <= 0 then return 0 end
  for ingredient, ingredient_count in pairs(recipe.ingredients) do
    inventory.remove({ name = ingredient, count = ingredient_count * possible })
  end
  for result_name, result_count in pairs(recipe.results) do
    inventory.insert({ name = result_name, count = result_count * possible })
  end
  if recipe_name == "lab" then
    local trigger_technology = agent.force.technologies["automation-science-pack"]
    if trigger_technology and not trigger_technology.researched then
      trigger_technology.researched = true
    end
  end
  return possible
end
local function action_move_to()
  local position = normalize_position(action.position)
  if not position then return err("move_to requires numeric position") end
  if distance(agent.position, position) > (action.max_distance or 4096) then return err("move_to target is too far") end
  if agent.kind == "server" then
    set_server_agent_position(position)
    return ok({ action = "move_to", status = "arrived", position = position_table(agent.position), target = position_table(position) })
  end
  local player = action_player()
  if not player or not player.character or not player.character.valid then return err("connected player character not found") end
  return err("RCON Lua walking_state moves only one tick; use GUI input movement executor for real-player move_to", { action = "move_to", position = position_table(player.position), target = position_table(position), distance = round(distance(player.position, position)) })
end
local function action_stop()
  if agent.kind == "server" then return ok({ action = "stop", status = "stopped", position = position_table(agent.position) }) end
  local player = action_player()
  if player and player.character and player.character.valid then player.walking_state = { walking = false, direction = defines.direction.north } end
  return ok({ action = "stop", status = "stopped", position = position_table(agent.position) })
end
local function action_restore_character_controller()
  local player = action_player()
  if not player or not player.valid then return err("connected player not found") end
  if not player.character or not player.character.valid then return err("connected player character not found") end
  local restored = false
  if player.controller_type ~= defines.controllers.character then
    local ok_restore = pcall(function()
      player.set_controller({ type = defines.controllers.character, character = player.character })
    end)
    if not ok_restore then return err("restore character controller failed") end
    restored = true
  end
  player.walking_state = { walking = false, direction = defines.direction.north }
  return ok({
    action = "restore_character_controller",
    restored = restored,
    controller_type = player.controller_type,
    controller_is_character = player_controller_is_character(player),
    position = position_table(player_actual_position(player))
  })
end
local function action_mine()
  local target = nil
  if action.target == "resource" or action.resource then target = find_resource(agent.surface, action) else target = find_entity(agent.surface, action) end
  if not target or not target.valid then return err("mine target not found") end
  if action.allow_preserved_artifact ~= true and is_preserved_starter_artifact(agent, target) then return err("preserved starter artifact is protected from default mining", { target = target.name }) end
  if distance(agent.position, target.position) > (action.reach or 10) then return err("mine target out of reach") end
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return err("agent inventory is not valid") end
  local count = math.max(1, math.min(action.count or 1, 50))
  if target.type == "resource" then
    local requested = math.min(count, target.amount or count)
    local inserted = inventory.insert({ name = target.name, count = requested })
    if inserted <= 0 then return err("inventory did not accept mined resource", { resource = target.name }) end
    if target.valid and target.amount then
      local remaining = target.amount - inserted
      if remaining <= 0 then target.destroy({ raise_destroy = true }) else target.amount = remaining end
    end
    return ok({ action = "mine", mined = inserted, inventory = inventory_contents(inventory) })
  end
  local mined = 0
  for _ = 1, count do
    if not target.valid then break end
    local mined_ok = target.mine({ inventory = inventory, force = false })
    if mined_ok then mined = mined + 1 else break end
  end
  if mined <= 0 then return err("mine failed") end
  return ok({ action = "mine", mined = mined, inventory = inventory_contents(inventory) })
end
local function action_craft()
  if type(action.recipe) ~= "string" then return err("craft requires recipe") end
  local count = math.max(1, math.min(action.count or 1, 100))
  local inventory = main_inventory(agent)
  local gear_guard_reason = direct_gear_handcraft_guard(agent, inventory, action.recipe, action)
  if gear_guard_reason then return err(gear_guard_reason, { recipe = action.recipe }) end
  if agent.kind == "server" then
    local crafted = virtual_craft(action.recipe, count)
    if crafted <= 0 then return err("recipe is not craftable", { recipe = action.recipe }) end
    return ok({ action = "craft", recipe = action.recipe, started = crafted, virtual = true })
  end
  local player = action_player()
  if not player then return err("connected player not found") end
  local craftable = player.get_craftable_count(action.recipe)
  if craftable <= 0 then return err("recipe is not craftable", { recipe = action.recipe }) end
  local started = player.begin_crafting({ count = math.min(count, craftable), recipe = action.recipe })
  if started <= 0 then return err("craft did not start", { recipe = action.recipe }) end
  return ok({ action = "craft", recipe = action.recipe, started = started })
end
local function action_build()
  if type(action.name) ~= "string" then return err("build requires entity/item name") end
  local position = normalize_position(action.position)
  if not position then return err("build requires position") end
  if distance(agent.position, position) > (action.reach or 32) then return err("build target out of reach") end
  local direction = action.direction or defines.direction.north
  local existing = existing_built_entity(agent.surface, agent.force, action.name, position)
  if existing then
    return ok({ action = "build", name = existing.name, unit_number = existing.unit_number, position = position_table(existing.position), status = "already_exists" })
  end
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return err("agent inventory is not valid") end
  if inventory.get_item_count(action.name) < 1 then return err("missing item", { item = action.name }) end
  local place_position = position
  if not build_candidate_valid(agent.surface, agent.force, action, place_position, direction) then
    if action.allow_nearby then
      local found = nil
      for radius = 1, 8 do
        for dx = -radius, radius do
          for dy = -radius, radius do
            local candidate = { x = position.x + dx, y = position.y + dy }
            if build_candidate_valid(agent.surface, agent.force, action, candidate, direction) then found = candidate; break end
          end
          if found then break end
        end
        if found then break end
      end
      if found then place_position = found else return err("cannot place entity", { name = action.name, position = position_table(position), required_resource = action.required_resource }) end
    else
      existing = existing_built_entity(agent.surface, agent.force, action.name, position)
      if existing then
        return ok({ action = "build", name = existing.name, unit_number = existing.unit_number, position = position_table(existing.position), status = "already_exists" })
      end
      return err("cannot place entity", { name = action.name, position = position_table(position), required_resource = action.required_resource })
    end
  end
  inventory.remove({ name = action.name, count = 1 })
  local entity = agent.surface.create_entity({ name = action.name, position = place_position, direction = direction, force = agent.force })
  if not entity then
    inventory.insert({ name = action.name, count = 1 })
    return err("create_entity failed", { name = action.name })
  end
  return ok({ action = "build", name = entity.name, unit_number = entity.unit_number, position = position_table(entity.position) })
end
local function action_insert()
  if type(action.item) ~= "string" then return err("insert requires item") end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then return err("insert target not found") end
  if distance(agent.position, target.position) > (action.reach or 32) then return err("insert target out of reach") end
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return err("agent inventory is not valid") end
  local count = math.max(1, math.min(action.count or 1, 100))
  local available = inventory.get_item_count(action.item)
  if available <= 0 then return err("missing item", { item = action.item }) end
  local inserted = target.insert({ name = action.item, count = math.min(count, available) })
  if inserted <= 0 then return err("target did not accept item", { target = target.name, item = action.item }) end
  inventory.remove({ name = action.item, count = inserted })
  return ok({ action = "insert", item = action.item, inserted = inserted, target = target.name, target_unit_number = target.unit_number })
end
local function action_take()
  if type(action.item) ~= "string" then return err("take requires item") end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then return err("take target not found") end
  if distance(agent.position, target.position) > (action.reach or 32) then return err("take target out of reach") end
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return err("agent inventory is not valid") end
  local count = math.max(1, math.min(action.count or 1, 100))
  local taken = target.remove_item({ name = action.item, count = count })
  if taken <= 0 then return err("target does not have item", { target = target.name, item = action.item }) end
  local inserted = inventory.insert({ name = action.item, count = taken })
  if inserted < taken then target.insert({ name = action.item, count = taken - inserted }) end
  return ok({ action = "take", item = action.item, taken = inserted, target = target.name, target_unit_number = target.unit_number })
end
local function action_set_recipe()
  if type(action.recipe) ~= "string" then return err("set_recipe requires recipe") end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then return err("set_recipe target not found") end
  if distance(agent.position, target.position) > (action.reach or 32) then return err("set_recipe target out of reach") end
  local recipe = agent.force.recipes[action.recipe]
  if not recipe then return err("recipe not found", { recipe = action.recipe }) end
  if not recipe.enabled then return err("recipe is not enabled", { recipe = action.recipe }) end
  local set_ok, result = pcall(function() return target.set_recipe(action.recipe) end)
  if not set_ok or result == false then set_ok, result = pcall(function() return target.set_recipe(recipe) end) end
  if not set_ok or result == false then return err("set_recipe failed", { target = target.name, recipe = action.recipe }) end
  return ok({ action = "set_recipe", target = target.name, target_unit_number = target.unit_number, recipe = action.recipe })
end
local function action_connect_power()
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then return err("connect_power target not found") end
  if distance(agent.position, target.position) > (action.reach or 32) then return err("connect_power target out of reach") end
  local target_connector = entity_wire_connector(target)
  if not target_connector then return err("connect_power target has no electric pole connector", { target = target.name }) end
  local candidates = agent.surface.find_entities_filtered({
    position = target.position,
    radius = action.radius or 16,
    type = "electric-pole",
    force = agent.force,
  })
  table.sort(candidates, function(a, b)
    local a_count = connector_connection_count(entity_wire_connector(a))
    local b_count = connector_connection_count(entity_wire_connector(b))
    if a_count ~= b_count then return a_count > b_count end
    return distance(target.position, a.position) < distance(target.position, b.position)
  end)
  for _, source in pairs(candidates) do
    if source.valid and source ~= target then
      local source_connector = entity_wire_connector(source)
      if source_connector and connectors_can_reach(target_connector, source_connector) then
        if connectors_connected(target_connector, source_connector) then
          return ok({ action = "connect_power", status = "already_connected", target_unit_number = target.unit_number, source_unit_number = source.unit_number })
        end
        local connected_ok, connected = pcall(function() return target_connector.connect_to(source_connector, false, defines.wire_origin.player) end)
        if connected_ok and (connected == true or connectors_connected(target_connector, source_connector)) then
          return ok({ action = "connect_power", status = "connected", target_unit_number = target.unit_number, source_unit_number = source.unit_number })
        end
      end
    end
  end
  if connector_connection_count(target_connector) > 0 then
    return ok({ action = "connect_power", status = "already_connected", target_unit_number = target.unit_number })
  end
  return err("no reachable electric pole found for connect_power", { target = target.name, target_unit_number = target.unit_number })
end
local function action_research()
  if type(action.technology) ~= "string" then return err("research requires technology") end
  local technology = agent.force.technologies[action.technology]
  if not technology then return err("technology not found", { technology = action.technology }) end
  if technology.researched then return ok({ action = "research", technology = technology.name, researched = true }) end
  local ingredients = {}
  local ok_ingredients, raw_ingredients = pcall(function() return technology.research_unit_ingredients end)
  if ok_ingredients and raw_ingredients then ingredients = raw_ingredients end
  if #ingredients == 0 then
    technology.researched = true
    return ok({ action = "research", technology = technology.name, researched = true, trigger = true })
  end
  local set_ok = pcall(function() agent.force.research_queue = { technology.name } end)
  if not set_ok then return err("setting research failed", { technology = technology.name }) end
  return ok({ action = "research", technology = technology.name, current_research = technology.name })
end
if action.type == "wait" then
  reply_action(ok({ action = "wait", ticks = action.ticks or 0 }))
elseif action.type == "print" then
  game.print(tostring(action.message or "[factorio-ai]"))
  reply_action(ok({ action = "print" }))
elseif action.type == "chart" then
  local agent = find_agent(action.player_name or default_player_name)
  local radius = action.radius or 128
  local center = action.position or agent.position
  agent.force.chart(agent.surface, { { x = center.x - radius, y = center.y - radius }, { x = center.x + radius, y = center.y + radius } })
  reply_action(ok({ action = "chart", radius = radius, position = position_table(center) }))
elseif action.type == "set_walking_state" then
  local player = action_player()
  if not player or not player.character or not player.character.valid then
    reply_action(err("connected player character not found"))
  else
    reply_action(err("RCON walking_state is disabled for real-player movement; use GUI input movement executor", { action = "set_walking_state", direction = action.direction or "north" }))
  end
elseif action.type == "stop_walking" then
  local player = action_player()
  if not player or not player.character or not player.character.valid then
    reply_action(err("connected player character not found"))
  else
    player.walking_state = { walking = false, direction = defines.direction.north }
    reply_action(ok({ action = "stop_walking" }))
  end
elseif action.type == "move_to" then
  reply_action(action_move_to())
elseif action.type == "stop" then
  reply_action(action_stop())
elseif action.type == "restore_character_controller" then
  reply_action(action_restore_character_controller())
elseif action.type == "mine" then
  reply_action(action_mine())
elseif action.type == "craft" then
  reply_action(action_craft())
elseif action.type == "build" then
  reply_action(action_build())
elseif action.type == "insert" then
  reply_action(action_insert())
elseif action.type == "take" then
  reply_action(action_take())
elseif action.type == "set_recipe" then
  reply_action(action_set_recipe())
elseif action.type == "connect_power" then
  reply_action(action_connect_power())
elseif action.type == "research" then
  reply_action(action_research())
else
  reply_action(err("action is not implemented", { action_type = action.type }))
end
"""
