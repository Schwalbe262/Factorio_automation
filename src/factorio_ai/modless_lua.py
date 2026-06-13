from __future__ import annotations

import json
from textwrap import dedent
from typing import Any

from .config import AppConfig
from .rcon import FactorioRconClient, parse_json_response


OBSERVE_RADIUS = 512
ALLOWED_ACTION_TYPES = {
    "build",
    "chart",
    "craft",
    "insert",
    "mine",
    "move_to",
    "print",
    "research",
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
local VIRTUAL_STARTER_ITEMS = {{
  ["burner-mining-drill"] = 1,
  ["stone-furnace"] = 1
}}
local VIRTUAL_RECIPES = {{
  ["firearm-magazine"] = {{ ingredients = {{ ["iron-plate"] = 4 }}, results = {{ ["firearm-magazine"] = 1 }} }},
  ["gun-turret"] = {{ ingredients = {{ ["iron-plate"] = 10, ["copper-plate"] = 5, ["iron-gear-wheel"] = 10 }}, results = {{ ["gun-turret"] = 1 }} }},
  ["stone-furnace"] = {{ ingredients = {{ stone = 5 }}, results = {{ ["stone-furnace"] = 1 }} }},
  ["iron-gear-wheel"] = {{ ingredients = {{ ["iron-plate"] = 2 }}, results = {{ ["iron-gear-wheel"] = 1 }} }},
  ["copper-cable"] = {{ ingredients = {{ ["copper-plate"] = 1 }}, results = {{ ["copper-cable"] = 2 }} }},
  ["transport-belt"] = {{ ingredients = {{ ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["transport-belt"] = 2 }} }},
  ["burner-inserter"] = {{ ingredients = {{ ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["burner-inserter"] = 1 }} }},
  ["inserter"] = {{ ingredients = {{ ["electronic-circuit"] = 1, ["iron-gear-wheel"] = 1, ["iron-plate"] = 1 }}, results = {{ inserter = 1 }} }},
  ["pipe"] = {{ ingredients = {{ ["iron-plate"] = 1 }}, results = {{ pipe = 1 }} }},
  ["boiler"] = {{ ingredients = {{ pipe = 4, ["stone-furnace"] = 1 }}, results = {{ boiler = 1 }} }},
  ["steam-engine"] = {{ ingredients = {{ ["iron-gear-wheel"] = 8, pipe = 5, ["iron-plate"] = 10 }}, results = {{ ["steam-engine"] = 1 }} }},
  ["offshore-pump"] = {{ ingredients = {{ ["electronic-circuit"] = 2, pipe = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["offshore-pump"] = 1 }} }},
  ["small-electric-pole"] = {{ ingredients = {{ wood = 1, ["copper-cable"] = 2 }}, results = {{ ["small-electric-pole"] = 2 }} }},
  ["electronic-circuit"] = {{ ingredients = {{ ["iron-plate"] = 1, ["copper-cable"] = 3 }}, results = {{ ["electronic-circuit"] = 1 }} }},
  ["burner-mining-drill"] = {{ ingredients = {{ ["iron-plate"] = 3, ["iron-gear-wheel"] = 3, stone = 5 }}, results = {{ ["burner-mining-drill"] = 1 }} }},
  ["automation-science-pack"] = {{ ingredients = {{ ["copper-plate"] = 1, ["iron-gear-wheel"] = 1 }}, results = {{ ["automation-science-pack"] = 1 }} }},
  ["lab"] = {{ ingredients = {{ ["electronic-circuit"] = 10, ["iron-gear-wheel"] = 10, ["transport-belt"] = 4 }}, results = {{ lab = 1 }} }},
  ["assembling-machine-1"] = {{ ingredients = {{ ["electronic-circuit"] = 3, ["iron-gear-wheel"] = 5, ["iron-plate"] = 9 }}, results = {{ ["assembling-machine-1"] = 1 }} }}
}}
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
  return ensure_server_agent()
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
  player = { name = agent.name, kind = agent.kind, position = position_table(origin), surface = surface.name, character_valid = agent.character_valid, move = agent.move or { active = false } },
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
local agent = find_agent(action.player_name or default_player_name)
local function set_server_agent_position(position)
  if agent.kind ~= "server" then return end
  storage.factorio_ai_agent = storage.factorio_ai_agent or {}
  storage.factorio_ai_agent.position = { x = position.x, y = position.y }
  storage.factorio_ai_agent.surface_name = agent.surface.name
  agent.position = storage.factorio_ai_agent.position
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
local function build_candidate_valid(surface, force, request, position, direction)
  if not surface.can_place_entity({ name = request.name, position = position, direction = direction, force = force, build_check_type = defines.build_check_type.manual }) then
    return false
  end
  if request.required_resource then
    return nearest_resource_name(surface, position, request.required_resource_radius or 4) == request.required_resource
  end
  return true
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
  local dx = position.x - player.position.x
  local dy = position.y - player.position.y
  player.character.walking_state = { walking = true, direction = walking_direction(dx, dy) }
  return ok({ action = "move_to", status = "moving", position = position_table(player.position), target = position_table(position), distance = round(distance(player.position, position)) })
end
local function action_stop()
  if agent.kind == "server" then return ok({ action = "stop", status = "stopped", position = position_table(agent.position) }) end
  local player = action_player()
  if player and player.character and player.character.valid then player.character.walking_state = { walking = false, direction = defines.direction.north } end
  return ok({ action = "stop", status = "stopped", position = position_table(agent.position) })
end
local function action_mine()
  local target = nil
  if action.target == "resource" or action.resource then target = find_resource(agent.surface, action) else target = find_entity(agent.surface, action) end
  if not target or not target.valid then return err("mine target not found") end
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
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then return err("agent inventory is not valid") end
  if inventory.get_item_count(action.name) < 1 then return err("missing item", { item = action.name }) end
  local direction = action.direction or defines.direction.north
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
local function action_research()
  if type(action.technology) ~= "string" then return err("research requires technology") end
  local technology = agent.force.technologies[action.technology]
  if not technology then return err("technology not found", { technology = action.technology }) end
  if technology.researched then return ok({ action = "research", technology = technology.name, researched = true }) end
  local set_ok = pcall(function() agent.force.research_queue = { technology.name } end)
  if not set_ok then return err("setting research failed", { technology = technology.name }) end
  return ok({ action = "research", technology = technology.name, current_research = technology.name })
end
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
elseif action.type == "move_to" then
  json_reply(action_move_to())
elseif action.type == "stop" then
  json_reply(action_stop())
elseif action.type == "mine" then
  json_reply(action_mine())
elseif action.type == "craft" then
  json_reply(action_craft())
elseif action.type == "build" then
  json_reply(action_build())
elseif action.type == "insert" then
  json_reply(action_insert())
elseif action.type == "take" then
  json_reply(action_take())
elseif action.type == "set_recipe" then
  json_reply(action_set_recipe())
elseif action.type == "research" then
  json_reply(action_research())
else
  json_reply(err("action is not implemented", { action_type = action.type }))
end
"""
