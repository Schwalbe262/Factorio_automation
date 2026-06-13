from __future__ import annotations

import json
from textwrap import dedent
from typing import Any

from .config import AppConfig
from .rcon import FactorioRconClient, parse_json_response


OBSERVE_RADIUS = 512
ALLOWED_ACTION_TYPES = {"chart", "print", "set_walking_state", "stop_walking", "wait"}
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

    def observe(self, *, player_name: str | None = None) -> dict[str, Any]:
        command = build_modless_observe_command(player_name or self.cfg.agent_player_name)
        with self._client() as client:
            return execute_json_lua_command(client, command)

    def act(self, action: dict[str, Any], *, player_name: str | None = None) -> dict[str, Any]:
        command = build_modless_action_command(action, player_name=player_name or self.cfg.agent_player_name)
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


def build_modless_observe_command(player_name: str = "") -> str:
    lua = _COMMON_LUA + "\n" + _OBSERVE_LUA.replace("__PLAYER_NAME__", _lua_string(player_name))
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
local function surface_pollution(surface, position)
  local ok, value = pcall(function() return surface.get_pollution(position) end)
  if ok and type(value) == "number" then return round(value) end
  return 0
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
    recipe = entity_recipe_name(entity),
    electric_network_connected = entity_connected_to_electric_network(entity),
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
local function find_agent(player_name)
  if type(player_name) == "string" and player_name ~= "" then
    local named = game.get_player(player_name)
    if named and named.valid then
      return {{ kind = "player", name = named.name, player = named, surface = named.surface, force = named.force, position = named.position, inventory = named.get_main_inventory(), character_valid = named.character ~= nil and named.character.valid or false }}
    end
  end
  for _, player in pairs(game.connected_players) do
    if player and player.valid then
      return {{ kind = "player", name = player.name, player = player, surface = player.surface, force = player.force, position = player.position, inventory = player.get_main_inventory(), character_valid = player.character ~= nil and player.character.valid or false }}
    end
  end
  for _, player in pairs(game.players) do
    if player and player.valid and player.character and player.character.valid then
      return {{ kind = "player", name = player.name, player = player, surface = player.surface, force = player.force, position = player.position, inventory = player.get_main_inventory(), character_valid = true }}
    end
  end
  local surface = game.surfaces.nauvis or game.surfaces[1]
  local spawn = game.forces.player.get_spawn_position(surface)
  return {{ kind = "server", name = "server", player = nil, surface = surface, force = game.forces.player, position = spawn, inventory = nil, character_valid = false }}
end
"""


_OBSERVE_LUA = """
local agent = find_agent(__PLAYER_NAME__)
local surface = agent.surface
local origin = agent.position
local function collect_craftable()
  local craftable = {}
  if not agent.player then return craftable end
  for name, recipe in pairs(agent.force.recipes) do
    if recipe.enabled then
      local ok, count = pcall(function() return agent.player.get_craftable_count(name) end)
      if ok and count and count > 0 then craftable[name] = count end
    end
  end
  return craftable
end
local function collect_resources()
  local resources = {}
  for _, resource_name in pairs({ "iron-ore", "coal", "stone", "copper-ore", "uranium-ore", "crude-oil" }) do
    local found = surface.find_entities_filtered({ position = origin, radius = OBSERVE_RADIUS, type = "resource", name = resource_name, limit = 160 })
    for _, entity in pairs(found) do
      if not entity.amount or entity.amount > 0 then
        table.insert(resources, { unit_number = entity.unit_number, name = entity.name, amount = entity.amount, position = position_table(entity.position), distance = round(distance(origin, entity.position)) })
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
  local watched = { "steam-power", "electronics", "automation-science-pack", "automation", "logistics", "steel-processing", "automation-2", "railway", "oil-processing", "rocket-silo" }
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
json_reply({
  ok = true,
  mode = "modless-rcon-lua",
  tick = game.tick,
  player = { name = agent.name, kind = agent.kind, position = position_table(origin), surface = surface.name, character_valid = agent.character_valid },
  inventory = inventory_contents(agent.inventory),
  craftable = collect_craftable(),
  resources = collect_resources(),
  entities = collect_entities(),
  enemies = collect_enemies(),
  pollution = { at_player = surface_pollution(surface, origin) },
  factory_events = {},
  research = collect_research()
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
if action.type == "wait" then
  json_reply(ok({ action = "wait", ticks = action.ticks or 0 }))
elseif action.type == "print" then
  game.print(tostring(action.message or "[factorio-ai]"))
  json_reply(ok({ action = "print" }))
elseif action.type == "chart" then
  local agent = find_agent(action.player_name or default_player_name)
  local radius = action.radius or 128
  local center = action.position or agent.position
  agent.force.chart(agent.surface, { { x = center.x - radius, y = center.y - radius }, { x = center.x + radius, y = center.y + radius } })
  json_reply(ok({ action = "chart", radius = radius, position = position_table(center) }))
elseif action.type == "set_walking_state" then
  local player = action_player()
  if not player or not player.character or not player.character.valid then
    json_reply(err("connected player character not found"))
  else
    player.character.walking_state = { walking = action.walking ~= false, direction = directions[action.direction or "north"] or defines.direction.north }
    json_reply(ok({ action = "set_walking_state", direction = action.direction or "north", walking = action.walking ~= false }))
  end
elseif action.type == "stop_walking" then
  local player = action_player()
  if not player or not player.character or not player.character.valid then
    json_reply(err("connected player character not found"))
  else
    player.character.walking_state = { walking = false, direction = defines.direction.north }
    json_reply(ok({ action = "stop_walking" }))
  end
else
  json_reply(err("action is not implemented", { action_type = action.type }))
end
"""
