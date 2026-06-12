local MOD = "factorio_ai_autoplayer"
local AGENT_NAME = "AI"
local OBSERVE_RADIUS = 512
local POWER_SITE_RADIUS = 512
local POWER_SITE_WATER_TILE_LIMIT = 1200
local LAB_SITE_RADIUS = 96
local POLE_WIRE_REACH = 7.5
local MOVE_STOP_DISTANCE = 0.35
local PLAYER_MOVE_STEP = 0.10

local VIRTUAL_STARTER_ITEMS = {
  ["burner-mining-drill"] = 1,
  ["stone-furnace"] = 1
}

local VIRTUAL_RECIPES = {
  ["firearm-magazine"] = {
    ingredients = { ["iron-plate"] = 4 },
    results = { ["firearm-magazine"] = 1 }
  },
  ["gun-turret"] = {
    ingredients = { ["iron-plate"] = 10, ["copper-plate"] = 5, ["iron-gear-wheel"] = 10 },
    results = { ["gun-turret"] = 1 }
  },
  ["stone-furnace"] = {
    ingredients = { stone = 5 },
    results = { ["stone-furnace"] = 1 }
  },
  ["iron-gear-wheel"] = {
    ingredients = { ["iron-plate"] = 2 },
    results = { ["iron-gear-wheel"] = 1 }
  },
  ["copper-cable"] = {
    ingredients = { ["copper-plate"] = 1 },
    results = { ["copper-cable"] = 2 }
  },
  ["transport-belt"] = {
    ingredients = { ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 },
    results = { ["transport-belt"] = 2 }
  },
  ["burner-inserter"] = {
    ingredients = { ["iron-plate"] = 1, ["iron-gear-wheel"] = 1 },
    results = { ["burner-inserter"] = 1 }
  },
  ["inserter"] = {
    ingredients = { ["electronic-circuit"] = 1, ["iron-gear-wheel"] = 1, ["iron-plate"] = 1 },
    results = { ["inserter"] = 1 }
  },
  ["pipe"] = {
    ingredients = { ["iron-plate"] = 1 },
    results = { ["pipe"] = 1 }
  },
  ["boiler"] = {
    ingredients = { pipe = 4, ["stone-furnace"] = 1 },
    results = { ["boiler"] = 1 }
  },
  ["steam-engine"] = {
    ingredients = { ["iron-gear-wheel"] = 8, pipe = 5, ["iron-plate"] = 10 },
    results = { ["steam-engine"] = 1 }
  },
  ["offshore-pump"] = {
    ingredients = { ["electronic-circuit"] = 2, pipe = 1, ["iron-gear-wheel"] = 1 },
    results = { ["offshore-pump"] = 1 }
  },
  ["small-electric-pole"] = {
    ingredients = { wood = 1, ["copper-cable"] = 2 },
    results = { ["small-electric-pole"] = 2 }
  },
  ["electronic-circuit"] = {
    ingredients = { ["iron-plate"] = 1, ["copper-cable"] = 3 },
    results = { ["electronic-circuit"] = 1 }
  },
  ["burner-mining-drill"] = {
    ingredients = { ["iron-plate"] = 3, ["iron-gear-wheel"] = 3, stone = 5 },
    results = { ["burner-mining-drill"] = 1 }
  },
  ["automation-science-pack"] = {
    ingredients = { ["copper-plate"] = 1, ["iron-gear-wheel"] = 1 },
    results = { ["automation-science-pack"] = 1 }
  },
  ["lab"] = {
    ingredients = { ["electronic-circuit"] = 10, ["iron-gear-wheel"] = 10, ["transport-belt"] = 4 },
    results = { ["lab"] = 1 }
  },
  ["assembling-machine-1"] = {
    ingredients = { ["electronic-circuit"] = 3, ["iron-gear-wheel"] = 5, ["iron-plate"] = 9 },
    results = { ["assembling-machine-1"] = 1 }
  }
}

local WATCHED_TECHNOLOGIES = {
  "steam-power",
  "electronics",
  "automation-science-pack",
  "automation",
  "logistics",
  "steel-processing",
  "automation-2",
  "railway",
  "oil-processing",
  "rocket-silo"
}

local function json_encode(value)
  return helpers.table_to_json(value)
end

local function json_decode(value)
  if value == nil or value == "" then
    return nil
  end
  return helpers.json_to_table(value)
end

local function reply(command, payload)
  local text = json_encode(payload)
  if rcon then
    rcon.print(text)
    return
  end
  if command and command.player_index then
    local player = game.get_player(command.player_index)
    if player then
      player.print(text)
      return
    end
  end
  game.print(text)
end

local function result_ok(extra)
  local result = extra or {}
  result.ok = true
  return result
end

local function result_err(reason, extra)
  local result = extra or {}
  result.ok = false
  result.reason = reason
  return result
end

local function round(value)
  return math.floor((value or 0) * 100 + 0.5) / 100
end

local function position_table(position)
  return { x = round(position.x or position[1]), y = round(position.y or position[2]) }
end

local function normalize_position(value)
  if type(value) ~= "table" then
    return nil
  end
  local x = value.x or value[1]
  local y = value.y or value[2]
  if type(x) ~= "number" or type(y) ~= "number" then
    return nil
  end
  return { x = x, y = y }
end

local function distance(a, b)
  local dx = (a.x or a[1]) - (b.x or b[1])
  local dy = (a.y or a[2]) - (b.y or b[2])
  return math.sqrt(dx * dx + dy * dy)
end

local function walking_direction(dx, dy)
  local x = 0
  local y = 0
  if dx > 0.15 then
    x = 1
  elseif dx < -0.15 then
    x = -1
  end
  if dy > 0.15 then
    y = 1
  elseif dy < -0.15 then
    y = -1
  end

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

local function set_character_walking(character, walking, direction)
  if not character or not character.valid then
    return false, "agent character is not valid"
  end
  local ok, err = pcall(function()
    character.walking_state = {
      walking = walking,
      direction = direction or defines.direction.north
    }
  end)
  if not ok then
    return false, tostring(err)
  end
  return true, nil
end

local function command_character_to(character, position)
  if not character or not character.valid then
    return false, "agent character is not valid"
  end
  if not character.commandable then
    return false, "agent character is not commandable"
  end
  local ok, err = pcall(function()
    character.commandable.set_command({
      type = defines.command.go_to_location,
      destination = position,
      distraction = defines.distraction.none,
      radius = MOVE_STOP_DISTANCE
    })
  end)
  if not ok then
    return false, tostring(err)
  end
  return true, nil
end

local function stop_character_command(character)
  if not character or not character.valid then
    return
  end
  set_character_walking(character, false)
  if character.commandable then
    pcall(function()
      character.commandable.set_command({ type = defines.command.stop })
    end)
  end
end

local function create_agent_character(surface, position)
  local spawn_position = surface.find_non_colliding_position("character", position, 10, 0.5) or position
  local character = surface.create_entity({
    name = "character",
    position = spawn_position,
    force = game.forces.player
  })
  if character then
    character.destructible = false
  end
  return character
end

local function agent_move_status(agent)
  if not agent or not agent.move_goal or not agent.position then
    return { active = false }
  end
  return {
    active = true,
    goal = position_table(agent.move_goal),
    distance = round(distance(agent.position, agent.move_goal))
  }
end

local function player_move_status(player)
  if not player then
    return { active = false }
  end
  local goals = storage.ai_player_move_goals or {}
  local goal = goals[tostring(player.index)]
  if not goal then
    return { active = false }
  end
  return {
    active = true,
    goal = position_table(goal),
    distance = round(distance(player.position, goal))
  }
end

local function player_agent(player)
  return {
    kind = "player",
    name = player.name,
    player = player,
    surface = player.surface,
    force = player.force,
    position = player.position,
    inventory = player.get_main_inventory(),
    character_valid = player.character ~= nil and player.character.valid or false,
    move_status = player_move_status(player)
  }
end

local function ensure_virtual_agent()
  storage.ai_agent = storage.ai_agent or {}
  local agent = storage.ai_agent
  if not agent.inventory or not agent.inventory.valid then
    agent.inventory = game.create_inventory(200)
    for name, count in pairs(VIRTUAL_STARTER_ITEMS) do
      agent.inventory.insert({ name = name, count = count })
    end
  end
  if not agent.position then
    local surface = game.surfaces.nauvis or game.surfaces[1]
    local spawn = game.forces.player.get_spawn_position(surface)
    agent.position = { x = spawn.x or spawn[1], y = spawn.y or spawn[2] }
    agent.surface_name = surface.name
  end
  agent.surface_name = agent.surface_name or "nauvis"
  local surface = game.get_surface(agent.surface_name) or game.surfaces[1]
  if not agent.character or not agent.character.valid then
    agent.character = create_agent_character(surface, agent.position)
  end
  if agent.character and agent.character.valid then
    agent.position = { x = agent.character.position.x, y = agent.character.position.y }
    agent.surface_name = agent.character.surface.name
  end
  return {
    kind = "virtual",
    name = AGENT_NAME,
    surface = surface,
    force = game.forces.player,
    position = agent.position,
    inventory = agent.inventory,
    character = agent.character,
    character_valid = agent.character ~= nil and agent.character.valid or false,
    move_status = agent_move_status(agent)
  }
end

local function configure_freeplay()
  if not remote.interfaces or not remote.interfaces["freeplay"] then
    return
  end
  local freeplay = remote.interfaces["freeplay"]
  if freeplay["set_skip_intro"] then
    pcall(remote.call, "freeplay", "set_skip_intro", true)
  end
  if freeplay["set_disable_crashsite"] then
    pcall(remote.call, "freeplay", "set_disable_crashsite", true)
  end
end

local function focus_player_on_agent(player)
  if not player or not player.valid then
    return
  end
  configure_freeplay()
  if player.controller_type == defines.controllers.cutscene then
    pcall(function()
      player.exit_cutscene()
    end)
  end
  local agent = ensure_virtual_agent()
  local surface = agent.surface
  if not surface then
    return
  end
  local character = player.character
  if not player.character or not player.character.valid then
    character = surface.create_entity({
      name = "character",
      position = agent.position,
      force = player.force or game.forces.player
    })
    if character then
      player.character = character
    end
  end
  if player.controller_type ~= defines.controllers.character and character and character.valid then
    pcall(function()
      player.set_controller({ type = defines.controllers.character, character = character })
    end)
  end
  player.teleport(agent.position, surface)
  player.force.chart(surface, {
    { agent.position.x - 64, agent.position.y - 64 },
    { agent.position.x + 64, agent.position.y + 64 }
  })
  if player.gui and player.gui.screen and player.gui.screen.skip_cutscene_label then
    player.gui.screen.skip_cutscene_label.destroy()
  end
  player.print("[Factorio AI] Moved to the iron MVP factory. Check the burner drill and furnace nearby.")
end

local function ensure_agent(command, options)
  options = options or {}
  local requested_player_name = options.player_name
  if type(requested_player_name) == "string" and requested_player_name ~= "" then
    local requested_player = game.get_player(requested_player_name)
    if requested_player then
      return player_agent(requested_player)
    end
  end

  if command and command.player_index then
    local command_player = game.get_player(command.player_index)
    if command_player then
      return player_agent(command_player)
    end
  end

  local player = game.get_player(AGENT_NAME)
  if not player then
    return ensure_virtual_agent()
  end

  if player.force == nil then
    player.force = game.forces.player
  end

  if not player.character or not player.character.valid then
    local surface = game.surfaces.nauvis or game.surfaces[1]
    local spawn = game.forces.player.get_spawn_position(surface)
    local character = surface.create_entity({
      name = "character",
      position = spawn,
      force = game.forces.player
    })
    player.character = character
    player.teleport(spawn, surface)
  end

  return player_agent(player)
end

local function main_inventory(agent)
  if not agent then
    return nil
  end
  return agent.inventory
end

local function inventory_contents(inventory)
  local output = {}
  if not inventory or not inventory.valid then
    return output
  end
  local raw = inventory.get_contents()
  for name, count in pairs(raw) do
    if type(count) == "number" then
      output[name] = count
    elseif type(count) == "table" and count.name and count.count then
      output[count.name] = count.count
    end
  end
  return output
end

local function entity_inventory_snapshot(entity)
  local result = {}
  if not entity or not entity.valid then
    return result
  end
  for _, inventory_id in pairs(defines.inventory) do
    local ok, inventory = pcall(function()
      return entity.get_inventory(inventory_id)
    end)
    if ok and inventory and inventory.valid and not inventory.is_empty() then
      result[tostring(inventory_id)] = inventory_contents(inventory)
    end
  end
  return result
end

local function entity_fluidbox_snapshot(entity)
  local result = {}
  if not entity or not entity.valid then
    return result
  end
  local ok, fluidbox = pcall(function()
    return entity.fluidbox
  end)
  if not ok or not fluidbox then
    return result
  end
  for i = 1, #fluidbox do
    local fluid = fluidbox[i]
    if fluid and fluid.name then
      result[tostring(i)] = {
        name = fluid.name,
        amount = round(fluid.amount or 0),
        temperature = round(fluid.temperature or 0)
      }
    end
  end
  return result
end

local function optional_entity_position(entity, property)
  if not entity or not entity.valid then
    return nil
  end
  local ok, value = pcall(function()
    return entity[property]
  end)
  if ok and value then
    return position_table(value)
  end
  return nil
end

local function entity_recipe_name(entity)
  if not entity or not entity.valid then
    return nil
  end
  local ok, recipe = pcall(function()
    return entity.get_recipe()
  end)
  if ok and recipe and recipe.name then
    return recipe.name
  end
  return nil
end

local function entity_connected_to_electric_network(entity)
  if not entity or not entity.valid then
    return nil
  end
  local ok, connected = pcall(function()
    return entity.is_connected_to_electric_network()
  end)
  if ok then
    return connected
  end
  return nil
end

local function collect_resources(surface, position)
  local resources = {}
  local resource_names = { "iron-ore", "coal", "stone", "copper-ore", "uranium-ore", "crude-oil" }
  for _, resource_name in pairs(resource_names) do
    local entities = surface.find_entities_filtered({
      position = position,
      radius = OBSERVE_RADIUS,
      type = "resource",
      name = resource_name,
      limit = 80
    })
    for _, entity in pairs(entities) do
      if not entity.amount or entity.amount > 0 then
        table.insert(resources, {
          unit_number = entity.unit_number,
          name = entity.name,
          amount = entity.amount,
          position = position_table(entity.position),
          distance = round(distance(position, entity.position))
        })
      end
    end
  end
  table.sort(resources, function(a, b)
    return a.distance < b.distance
  end)
  return resources
end

local function entity_snapshot(entity, position)
  return {
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
    distance = round(distance(position, entity.position)),
    inventories = entity_inventory_snapshot(entity),
    fluids = entity_fluidbox_snapshot(entity)
  }
end

local function entity_identity_key(entity)
  if entity.unit_number then
    return tostring(entity.unit_number)
  end
  return entity.name .. ":" .. tostring(entity.position.x) .. ":" .. tostring(entity.position.y)
end

local function add_unique_entity_snapshot(rows, seen, entity, position)
  if not entity.valid then
    return
  end
  local key = entity_identity_key(entity)
  if seen[key] then
    return
  end
  seen[key] = true
  table.insert(rows, entity_snapshot(entity, position))
end

local function collect_entities(surface, position)
  local entities = {}
  local seen = {}
  local entity_names = {
    "burner-mining-drill",
    "stone-furnace",
    "electric-mining-drill",
    "assembling-machine-1",
    "assembling-machine-2",
    "lab",
    "boiler",
    "steam-engine",
    "offshore-pump",
    "pipe",
    "transport-belt",
    "burner-inserter",
    "inserter",
    "gun-turret",
    "small-electric-pole"
  }
  for _, entity_name in pairs(entity_names) do
    local found = surface.find_entities_filtered({
      position = position,
      radius = OBSERVE_RADIUS,
      name = entity_name,
      limit = 80
    })
    for _, entity in pairs(found) do
      add_unique_entity_snapshot(entities, seen, entity, position)
    end
  end
  local trees = surface.find_entities_filtered({
    position = position,
    radius = OBSERVE_RADIUS,
    type = "tree",
    limit = 80
  })
  for _, entity in pairs(trees) do
    add_unique_entity_snapshot(entities, seen, entity, position)
  end
  local simple_entities = surface.find_entities_filtered({
    position = position,
    radius = OBSERVE_RADIUS,
    type = "simple-entity",
    limit = 80
  })
  for _, entity in pairs(simple_entities) do
    add_unique_entity_snapshot(entities, seen, entity, position)
  end
  local misc = surface.find_entities_filtered({
    position = position,
    radius = 32,
    limit = 80
  })
  for _, entity in pairs(misc) do
    if entity.valid and entity.name ~= "character" and entity.type ~= "resource" then
      add_unique_entity_snapshot(entities, seen, entity, position)
    end
  end
  table.sort(entities, function(a, b)
    return a.distance < b.distance
  end)
  return entities
end

local function collect_enemies(surface, position, force)
  local enemies = {}
  local seen = {}
  local enemy_force = game.forces.enemy
  local enemy_types = { "unit", "unit-spawner", "turret" }
  for _, enemy_type in pairs(enemy_types) do
    local found = surface.find_entities_filtered({
      position = position,
      radius = OBSERVE_RADIUS,
      force = enemy_force,
      type = enemy_type,
      limit = 80
    })
    for _, entity in pairs(found) do
      if entity.valid and (not force or entity.force ~= force) then
        local key = entity_identity_key(entity)
        if not seen[key] then
          seen[key] = true
          table.insert(enemies, entity_snapshot(entity, position))
        end
      end
    end
  end
  table.sort(enemies, function(a, b)
    return a.distance < b.distance
  end)
  return enemies
end

local function can_place_manual(surface, force, name, position, direction)
  return surface.can_place_entity({
    name = name,
    position = position,
    direction = direction or defines.direction.north,
    force = force,
    build_check_type = defines.build_check_type.manual
  })
end

local function west_power_layout(position)
  return {
    offshore_pump = {
      name = "offshore-pump",
      position = position,
      direction = defines.direction.west
    },
    boiler = {
      name = "boiler",
      position = { x = position.x + 2, y = position.y - 1 },
      direction = defines.direction.north
    },
    steam_engine = {
      name = "steam-engine",
      position = { x = position.x + 2, y = position.y - 4 },
      direction = defines.direction.north
    },
    small_electric_pole = {
      name = "small-electric-pole",
      position = { x = position.x, y = position.y - 4 },
      direction = defines.direction.north
    }
  }
end

local function power_layout_can_place(surface, force, layout)
  return can_place_manual(surface, force, "offshore-pump", layout.offshore_pump.position, layout.offshore_pump.direction)
    and can_place_manual(surface, force, "boiler", layout.boiler.position, layout.boiler.direction)
    and can_place_manual(surface, force, "steam-engine", layout.steam_engine.position, layout.steam_engine.direction)
    and can_place_manual(surface, force, "small-electric-pole", layout.small_electric_pole.position, layout.small_electric_pole.direction)
end

local function collect_power_sites(surface, position, force)
  local sites = {}
  local checked = {}
  local water_tiles = surface.find_tiles_filtered({
    position = position,
    radius = POWER_SITE_RADIUS,
    name = { "water", "deepwater", "water-green", "deepwater-green" },
    limit = POWER_SITE_WATER_TILE_LIMIT
  })
  for _, tile in pairs(water_tiles) do
    local tile_position = tile.position
    for dx = -8, 8 do
      for dy = -8, 8 do
        local pump_position = { x = tile_position.x + dx + 0.5, y = tile_position.y + dy + 0.5 }
        local key = tostring(pump_position.x) .. "," .. tostring(pump_position.y)
        if not checked[key] then
          checked[key] = true
          local layout = west_power_layout(pump_position)
          if power_layout_can_place(surface, force, layout) then
            table.insert(sites, {
              distance = round(distance(position, pump_position)),
              layout = layout
            })
            if #sites >= 20 then
              table.sort(sites, function(a, b)
                return a.distance < b.distance
              end)
              return sites
            end
          end
        end
      end
    end
  end
  table.sort(sites, function(a, b)
    return a.distance < b.distance
  end)
  return sites
end

local function technology_research_unit_ingredients(technology)
  local ingredients = {}
  local ok, raw = pcall(function()
    return technology.research_unit_ingredients
  end)
  if not ok or not raw then
    return ingredients
  end
  for _, ingredient in pairs(raw) do
    local name = ingredient.name or ingredient[1]
    local amount = ingredient.amount or ingredient[2]
    if name and amount then
      ingredients[name] = amount
    end
  end
  return ingredients
end

local function technology_snapshot(technology)
  if not technology or not technology.valid then
    return nil
  end
  local unit_count = nil
  local unit_energy = nil
  pcall(function()
    unit_count = technology.research_unit_count
  end)
  pcall(function()
    unit_energy = technology.research_unit_energy
  end)
  return {
    name = technology.name,
    researched = technology.researched,
    enabled = technology.enabled,
    research_unit_count = unit_count,
    research_unit_energy = unit_energy,
    ingredients = technology_research_unit_ingredients(technology)
  }
end

local function collect_research(force)
  local current = nil
  local progress = nil
  local queue = {}
  local ok_current, current_research = pcall(function()
    return force.current_research
  end)
  if ok_current and current_research then
    current = current_research.name
    pcall(function()
      progress = force.research_progress
    end)
  end
  pcall(function()
    for _, technology in pairs(force.research_queue) do
      if technology.name then
        table.insert(queue, technology.name)
      else
        table.insert(queue, tostring(technology))
      end
    end
  end)

  local technologies = {}
  for _, name in pairs(WATCHED_TECHNOLOGIES) do
    local technology = force.technologies[name]
    local snapshot = technology_snapshot(technology)
    if snapshot then
      technologies[name] = snapshot
    end
  end

  return {
    current = current,
    progress = progress,
    queue = queue,
    technologies = technologies
  }
end

local function research_queue_contains(force, technology_name)
  local found = false
  pcall(function()
    for _, technology in pairs(force.research_queue or {}) do
      local name = technology.name or tostring(technology)
      if name == technology_name then
        found = true
      end
    end
  end)
  return found
end

local function research_target_selected(force, technology_name)
  local selected = research_queue_contains(force, technology_name)
  pcall(function()
    local current = force.current_research
    if current and current.name == technology_name then
      selected = true
    end
  end)
  return selected
end

local function technology_has_research_packs(technology)
  return next(technology_research_unit_ingredients(technology)) ~= nil
end

local function unlock_virtual_trigger_prerequisites(force, technology, visited)
  if not technology then
    return
  end
  visited = visited or {}
  if visited[technology.name] then
    return
  end
  visited[technology.name] = true
  local ok, prerequisites = pcall(function()
    return technology.prerequisites
  end)
  if not ok or not prerequisites then
    return
  end
  for prerequisite, _ in pairs(prerequisites) do
    local prerequisite_technology = prerequisite
    if type(prerequisite_technology) == "string" then
      prerequisite_technology = force.technologies[prerequisite_technology]
    end
    if prerequisite_technology then
      unlock_virtual_trigger_prerequisites(force, prerequisite_technology, visited)
      if not prerequisite_technology.researched and not technology_has_research_packs(prerequisite_technology) then
        pcall(function()
          prerequisite_technology.researched = true
        end)
      end
    end
  end
end

local function add_lab_site(sites, checked, surface, force, pole, pole_position, lab_position, source_pole)
  if not can_place_manual(surface, force, "lab", lab_position, defines.direction.north) then
    return
  end
  local key = tostring(round(pole_position.x)) .. "," .. tostring(round(pole_position.y)) .. ":"
    .. tostring(round(lab_position.x)) .. "," .. tostring(round(lab_position.y))
  if checked[key] then
    return
  end
  checked[key] = true
  local site = {
    pole_position = position_table(pole_position),
    lab_position = position_table(lab_position),
    distance = round(distance(source_pole.position, lab_position)),
    powered = entity_connected_to_electric_network(source_pole)
  }
  if pole and pole.valid then
    site.pole_unit_number = pole.unit_number
  else
    site.source_pole_unit_number = source_pole.unit_number
  end
  table.insert(sites, site)
end

local function add_lab_sites_around_pole(sites, checked, surface, force, pole, pole_position, source_pole)
  for dx = -4, 4 do
    for dy = -4, 4 do
      local lab_position = { x = pole_position.x + dx, y = pole_position.y + dy }
      if distance(pole_position, lab_position) <= 5.5 then
        add_lab_site(sites, checked, surface, force, pole, pole_position, lab_position, source_pole)
        if #sites >= 40 then
          return
        end
      end
    end
  end
end

local function collect_lab_sites(surface, position, force)
  local sites = {}
  local checked = {}
  local poles = surface.find_entities_filtered({
    position = position,
    radius = OBSERVE_RADIUS,
    name = "small-electric-pole",
    limit = 80
  })
  table.sort(poles, function(a, b)
    local ap = entity_connected_to_electric_network(a) and 0 or 1
    local bp = entity_connected_to_electric_network(b) and 0 or 1
    if ap ~= bp then
      return ap < bp
    end
    return distance(position, a.position) < distance(position, b.position)
  end)

  for _, source_pole in pairs(poles) do
    if source_pole.valid then
      local source_position = { x = source_pole.position.x, y = source_pole.position.y }
      add_lab_sites_around_pole(sites, checked, surface, force, source_pole, source_position, source_pole)
      if #sites >= 40 then
        break
      end
      for dx = -7, 7 do
        for dy = -7, 7 do
          local pole_position = { x = source_pole.position.x + dx, y = source_pole.position.y + dy }
          if distance(source_pole.position, pole_position) <= POLE_WIRE_REACH
            and distance(position, pole_position) <= LAB_SITE_RADIUS
            and can_place_manual(surface, force, "small-electric-pole", pole_position, defines.direction.north) then
            add_lab_sites_around_pole(sites, checked, surface, force, nil, pole_position, source_pole)
            if #sites >= 40 then
              break
            end
          end
        end
        if #sites >= 40 then
          break
        end
      end
    end
    if #sites >= 40 then
      break
    end
  end
  table.sort(sites, function(a, b)
    if a.powered ~= b.powered then
      return a.powered == true
    end
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
  if not pole and not can_place_manual(surface, force, "small-electric-pole", layout.pole_position, defines.direction.north) then
    return
  end
  if not can_place_manual(surface, force, "assembling-machine-1", layout.cable_assembler_position, defines.direction.north)
    or not can_place_manual(surface, force, "assembling-machine-1", layout.circuit_assembler_position, defines.direction.north)
    or not can_place_manual(surface, force, "inserter", layout.transfer_inserter_position, layout.transfer_inserter_direction) then
    return
  end
  local key = tostring(round(layout.pole_position.x)) .. "," .. tostring(round(layout.pole_position.y))
  if checked[key] then
    return
  end
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
  if pole and pole.valid then
    site.pole_unit_number = pole.unit_number
  else
    site.source_pole_unit_number = source_pole.unit_number
  end
  table.insert(sites, site)
end

local function collect_automation_sites(surface, position, force)
  local sites = {}
  local checked = {}
  local poles = surface.find_entities_filtered({
    position = position,
    radius = OBSERVE_RADIUS,
    name = "small-electric-pole",
    limit = 80
  })
  table.sort(poles, function(a, b)
    local ap = entity_connected_to_electric_network(a) and 0 or 1
    local bp = entity_connected_to_electric_network(b) and 0 or 1
    if ap ~= bp then
      return ap < bp
    end
    return distance(position, a.position) < distance(position, b.position)
  end)

  for _, source_pole in pairs(poles) do
    if source_pole.valid then
      local source_position = { x = source_pole.position.x, y = source_pole.position.y }
      add_automation_site(sites, checked, surface, force, source_pole, source_position, source_pole)
      if #sites >= 40 then
        break
      end
      for dx = -8, 8 do
        for dy = -8, 8 do
          local pole_position = { x = source_pole.position.x + dx, y = source_pole.position.y + dy }
          if distance(source_pole.position, pole_position) <= POLE_WIRE_REACH then
            add_automation_site(sites, checked, surface, force, nil, pole_position, source_pole)
            if #sites >= 40 then
              break
            end
          end
        end
        if #sites >= 40 then
          break
        end
      end
    end
    if #sites >= 40 then
      break
    end
  end
  table.sort(sites, function(a, b)
    if a.powered ~= b.powered then
      return a.powered == true
    end
    return (a.distance or 999999) < (b.distance or 999999)
  end)
  return sites
end

local function virtual_craftable(agent)
  local craftable = {}
  local inventory = main_inventory(agent)
  if not inventory or not inventory.valid then
    return craftable
  end
  for recipe_name, recipe in pairs(VIRTUAL_RECIPES) do
    local possible = 999999
    for ingredient, count in pairs(recipe.ingredients) do
      possible = math.min(possible, math.floor(inventory.get_item_count(ingredient) / count))
    end
    if possible > 0 and possible < 999999 then
      craftable[recipe_name] = possible
    end
  end
  return craftable
end

local function collect_craftable(agent)
  local craftable = {}
  if agent.kind == "virtual" then
    return virtual_craftable(agent)
  end
  for name, recipe in pairs(agent.force.recipes) do
    if recipe.enabled then
      local count = agent.player.get_craftable_count(name)
      if count > 0 then
        craftable[name] = count
      end
    end
  end
  return craftable
end

local function observe(command, options)
  local agent = ensure_agent(command, options)
  local surface = agent.surface
  local position = agent.position
  local inventory = main_inventory(agent)
  local response = result_ok({
    mod = MOD,
    tick = game.tick,
    player = {
      name = agent.name,
      kind = agent.kind,
      position = position_table(position),
      surface = surface.name,
      character_valid = agent.character_valid,
      move = agent.move_status or { active = false }
    },
    inventory = inventory_contents(inventory),
    craftable = collect_craftable(agent),
    resources = collect_resources(surface, position),
    entities = collect_entities(surface, position),
    enemies = collect_enemies(surface, position, agent.force),
    power_sites = collect_power_sites(surface, position, agent.force),
    lab_sites = collect_lab_sites(surface, position, agent.force),
    automation_sites = collect_automation_sites(surface, position, agent.force),
    research = collect_research(agent.force)
  })
  return response
end

local function find_resource(surface, action)
  local position = normalize_position(action.position)
  if position then
    local found = surface.find_entities_filtered({
      position = position,
      radius = action.radius or 1.0,
      type = "resource",
      name = action.name,
      limit = 1
    })
    return found[1]
  end
  local center = normalize_position(action.near)
  if not center then
    return nil
  end
  local found = surface.find_entities_filtered({
    position = center,
    radius = action.radius or OBSERVE_RADIUS,
    type = "resource",
    name = action.name,
    limit = 32
  })
  local available = {}
  for _, entity in pairs(found) do
    if not entity.amount or entity.amount > 0 then
      table.insert(available, entity)
    end
  end
  found = available
  table.sort(found, function(a, b)
    return distance(center, a.position) < distance(center, b.position)
  end)
  return found[1]
end

local function find_entity(surface, action)
  if action.unit_number then
    local entity = game.get_entity_by_unit_number(action.unit_number)
    if entity and entity.valid then
      return entity
    end
  end
  local position = normalize_position(action.position)
  if not position then
    return nil
  end
  local found = surface.find_entities_filtered({
    position = position,
    radius = action.radius or 1.0,
    name = action.name,
    limit = 1
  })
  return found[1]
end

local function action_move_to(agent, action)
  local position = normalize_position(action.position)
  if not position then
    return result_err("move_to requires numeric position")
  end
  local max_distance = action.max_distance or 4096
  if distance(agent.position, position) > max_distance then
    return result_err("move_to target is too far", { max_distance = max_distance })
  end
  if agent.kind == "virtual" then
    storage.ai_agent.surface_name = agent.surface.name
    storage.ai_agent.move_goal = { x = position.x, y = position.y }
    if distance(agent.position, position) <= MOVE_STOP_DISTANCE then
      storage.ai_agent.move_goal = nil
      stop_character_command(storage.ai_agent.character)
      return result_ok({
        action = "move_to",
        status = "arrived",
        position = position_table(agent.position),
        target = position_table(position)
      })
    end
    return result_ok({
      action = "move_to",
      status = "moving",
      position = position_table(agent.position),
      target = position_table(position),
      distance = round(distance(agent.position, position))
    })
  else
    storage.ai_player_move_goals = storage.ai_player_move_goals or {}
    storage.ai_player_move_goals[tostring(agent.player.index)] = { x = position.x, y = position.y }
    if distance(agent.position, position) <= MOVE_STOP_DISTANCE then
      set_character_walking(agent.player, false)
      storage.ai_player_move_goals[tostring(agent.player.index)] = nil
      return result_ok({ action = "move_to", status = "arrived", position = position_table(agent.position) })
    end
    local ok, err = set_character_walking(
      agent.player,
      true,
      walking_direction(position.x - agent.position.x, position.y - agent.position.y)
    )
    if not ok then
      return result_err("walking_state failed", { error = err })
    end
    agent.position = agent.player.position
    return result_ok({
      action = "move_to",
      status = "moving",
      position = position_table(agent.position),
      target = position_table(position),
      distance = round(distance(agent.position, position))
    })
  end
end

local function action_mine(agent, action)
  local surface = agent.surface
  local target
  if action.target == "resource" or action.resource then
    target = find_resource(surface, action)
  else
    target = find_entity(surface, action)
  end
  if not target or not target.valid then
    return result_err("mine target not found")
  end
  if distance(agent.position, target.position) > (action.reach or 10) then
    return result_err("mine target out of reach")
  end
  local inventory = main_inventory(agent)
  local mined = 0
  local count = math.max(1, math.min(action.count or 1, 50))
  if target.type == "resource" then
    local available = target.amount or count
    local requested = math.min(count, available)
    local inserted = inventory.insert({ name = target.name, count = requested })
    if inserted <= 0 then
      return result_err("inventory did not accept mined resource", { resource = target.name })
    end
    if target.valid and target.amount then
      local remaining = target.amount - inserted
      if remaining <= 0 and target.valid then
        target.destroy({ raise_destroy = true })
      elseif target.valid then
        target.amount = remaining
      end
    end
    return result_ok({ action = "mine", mined = inserted, inventory = inventory_contents(inventory) })
  end
  for _ = 1, count do
    if not target.valid then
      break
    end
    local ok = target.mine({ inventory = inventory, force = false })
    if ok then
      mined = mined + 1
    else
      break
    end
  end
  if mined <= 0 then
    return result_err("mine failed")
  end
  return result_ok({ action = "mine", mined = mined, inventory = inventory_contents(inventory) })
end

local function virtual_craft(agent, recipe_name, count)
  local recipe = VIRTUAL_RECIPES[recipe_name]
  if not recipe then
    return 0
  end
  local inventory = main_inventory(agent)
  local possible = count
  for ingredient, ingredient_count in pairs(recipe.ingredients) do
    possible = math.min(possible, math.floor(inventory.get_item_count(ingredient) / ingredient_count))
  end
  if possible <= 0 then
    return 0
  end
  for ingredient, ingredient_count in pairs(recipe.ingredients) do
    inventory.remove({ name = ingredient, count = ingredient_count * possible })
  end
  for result_name, result_count in pairs(recipe.results) do
    inventory.insert({ name = result_name, count = result_count * possible })
  end
  return possible
end

local function action_craft(agent, action)
  if type(action.recipe) ~= "string" then
    return result_err("craft requires recipe")
  end
  local count = math.max(1, math.min(action.count or 1, 100))
  if agent.kind == "virtual" then
    local crafted = virtual_craft(agent, action.recipe, count)
    if crafted <= 0 then
      return result_err("recipe is not craftable", { recipe = action.recipe })
    end
    return result_ok({ action = "craft", recipe = action.recipe, started = crafted, virtual = true })
  end
  local craftable = agent.player.get_craftable_count(action.recipe)
  if craftable <= 0 then
    return result_err("recipe is not craftable", { recipe = action.recipe })
  end
  local started = agent.player.begin_crafting({ count = math.min(count, craftable), recipe = action.recipe })
  if started <= 0 then
    return result_err("craft did not start", { recipe = action.recipe })
  end
  return result_ok({ action = "craft", recipe = action.recipe, started = started })
end

local function connect_pole_to_neighbours(surface, pole)
  if not pole or not pole.valid or pole.name ~= "small-electric-pole" then
    return 0
  end
  local ok_connector, connector = pcall(function()
    return pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
  end)
  if not ok_connector or not connector then
    return 0
  end

  local connected = 0
  local neighbours = surface.find_entities_filtered({
    position = pole.position,
    radius = POLE_WIRE_REACH,
    name = "small-electric-pole",
    limit = 16
  })
  table.sort(neighbours, function(a, b)
    return distance(pole.position, a.position) < distance(pole.position, b.position)
  end)

  for _, other in pairs(neighbours) do
    if other.valid and other.unit_number ~= pole.unit_number and distance(pole.position, other.position) <= POLE_WIRE_REACH then
      local ok_other, other_connector = pcall(function()
        return other.get_wire_connector(defines.wire_connector_id.pole_copper, true)
      end)
      if ok_other and other_connector then
        local already = false
        pcall(function()
          already = connector.is_connected_to(other_connector, defines.wire_origin.player)
        end)
        if already then
          connected = connected + 1
        else
          local ok_connect, made = pcall(function()
            return connector.connect_to(other_connector, true, defines.wire_origin.player)
          end)
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector, false, defines.wire_origin.player)
            end)
          end
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector, true)
            end)
          end
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector, false)
            end)
          end
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector)
            end)
          end
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector, true, defines.wire_origin.script)
            end)
          end
          if not ok_connect then
            ok_connect, made = pcall(function()
              return connector.connect_to(other_connector, false, defines.wire_origin.script)
            end)
          end
          local connected_now = false
          pcall(function()
            connected_now = connector.is_connected_to(other_connector, defines.wire_origin.player)
          end)
          if not connected_now then
            pcall(function()
              connected_now = connector.is_connected_to(other_connector, defines.wire_origin.script)
            end)
          end
          if ok_connect and (made ~= false or connected_now) then
            connected = connected + 1
          end
        end
      end
    end
  end
  return connected
end

local function action_build(agent, action)
  if type(action.name) ~= "string" then
    return result_err("build requires entity/item name")
  end
  local position = normalize_position(action.position)
  if not position then
    return result_err("build requires position")
  end
  if distance(agent.position, position) > (action.reach or 32) then
    return result_err("build target out of reach")
  end
  local inventory = main_inventory(agent)
  if inventory.get_item_count(action.name) < 1 then
    return result_err("missing item", { item = action.name })
  end
  local direction = action.direction or defines.direction.north
  local surface = agent.surface
  local place_position = position
  if not surface.can_place_entity({
    name = action.name,
    position = place_position,
    direction = direction,
    force = agent.force,
    build_check_type = defines.build_check_type.manual
  }) then
    if action.allow_nearby then
      local found = nil
      for radius = 1, 8 do
        for dx = -radius, radius do
          for dy = -radius, radius do
            local candidate = { x = position.x + dx, y = position.y + dy }
            if surface.can_place_entity({
              name = action.name,
              position = candidate,
              direction = direction,
              force = agent.force,
              build_check_type = defines.build_check_type.manual
            }) then
              found = candidate
              break
            end
          end
          if found then
            break
          end
        end
        if found then
          break
        end
      end
      if found then
        place_position = found
      else
        return result_err("cannot place entity", { name = action.name, position = position_table(position) })
      end
    else
      return result_err("cannot place entity", { name = action.name, position = position_table(position) })
    end
  end
  inventory.remove({ name = action.name, count = 1 })
  local create_params = {
    name = action.name,
    position = place_position,
    direction = direction,
    force = agent.force
  }
  if agent.player then
    create_params.player = agent.player
  end
  local entity = surface.create_entity(create_params)
  if not entity then
    inventory.insert({ name = action.name, count = 1 })
    return result_err("create_entity failed", { name = action.name })
  end
  local connected_wires = connect_pole_to_neighbours(surface, entity)
  return result_ok({
    action = "build",
    name = entity.name,
    unit_number = entity.unit_number,
    position = position_table(entity.position),
    connected_wires = connected_wires
  })
end

local function action_connect_power(agent, action)
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then
    return result_err("connect_power target not found")
  end
  if target.name ~= "small-electric-pole" then
    return result_err("connect_power target must be a small electric pole", { target = target.name })
  end
  if distance(agent.position, target.position) > (action.reach or 64) then
    return result_err("connect_power target out of reach")
  end
  local connected_wires = connect_pole_to_neighbours(agent.surface, target)
  if connected_wires <= 0 then
    return result_err("connect_power did not connect any neighbouring pole", { target = target.unit_number })
  end
  return result_ok({
    action = "connect_power",
    target = target.name,
    target_unit_number = target.unit_number,
    connected_wires = connected_wires,
    position = position_table(target.position)
  })
end

local function action_research(agent, action)
  if type(action.technology) ~= "string" then
    return result_err("research requires technology")
  end
  local technology = agent.force.technologies[action.technology]
  if not technology then
    return result_err("technology not found", { technology = action.technology })
  end
  if technology.researched then
    return result_ok({ action = "research", technology = technology.name, researched = true })
  end
  if not technology.enabled then
    return result_err("technology is not enabled", { technology = technology.name })
  end
  unlock_virtual_trigger_prerequisites(agent.force, technology)
  if not technology_has_research_packs(technology) then
    local ok_unlock = pcall(function()
      technology.researched = true
    end)
    if ok_unlock then
      return result_ok({ action = "research", technology = technology.name, researched = true, virtual_trigger = true })
    end
  end
  pcall(function()
    agent.force.research_queue_enabled = true
  end)
  local ok = pcall(function()
    agent.force.research_queue = { technology.name }
  end)
  if ok and not research_target_selected(agent.force, technology.name) then
    ok = pcall(function()
      agent.force.research_queue = { technology }
    end)
  end
  if ok and not research_target_selected(agent.force, technology.name) then
    ok = false
  end
  if not ok then
    pcall(function()
      local current = agent.force.current_research
      if current and current.name ~= technology.name then
        agent.force.cancel_current_research()
      end
    end)
    local added
    ok, added = pcall(function()
      return agent.force.add_research(technology.name)
    end)
    if ok and added == false then
      ok = false
    end
  end
  if ok and not research_target_selected(agent.force, technology.name) then
    ok = false
  end
  if not ok then
    return result_err("setting research failed", { technology = technology.name })
  end
  return result_ok({ action = "research", technology = technology.name, current_research = technology.name })
end

local function action_set_recipe(agent, action)
  if type(action.recipe) ~= "string" then
    return result_err("set_recipe requires recipe")
  end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then
    return result_err("set_recipe target not found")
  end
  if distance(agent.position, target.position) > (action.reach or 32) then
    return result_err("set_recipe target out of reach")
  end
  local recipe = agent.force.recipes[action.recipe]
  if not recipe then
    return result_err("recipe not found", { recipe = action.recipe })
  end
  if not recipe.enabled then
    return result_err("recipe is not enabled", { recipe = action.recipe })
  end
  local ok, result = pcall(function()
    return target.set_recipe(action.recipe)
  end)
  if not ok or result == false then
    ok, result = pcall(function()
      return target.set_recipe(recipe)
    end)
  end
  if not ok or result == false then
    return result_err("set_recipe failed", { target = target.name, recipe = action.recipe })
  end
  return result_ok({
    action = "set_recipe",
    target = target.name,
    target_unit_number = target.unit_number,
    recipe = action.recipe
  })
end

local function action_insert(agent, action)
  if type(action.item) ~= "string" then
    return result_err("insert requires item")
  end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then
    return result_err("insert target not found")
  end
  if distance(agent.position, target.position) > (action.reach or 32) then
    return result_err("insert target out of reach")
  end
  local count = math.max(1, math.min(action.count or 1, 100))
  local inventory = main_inventory(agent)
  local available = inventory.get_item_count(action.item)
  if available <= 0 then
    return result_err("missing item", { item = action.item })
  end
  local transfer = math.min(count, available)
  local inserted = target.insert({ name = action.item, count = transfer })
  if inserted <= 0 then
    return result_err("target did not accept item", { target = target.name, item = action.item })
  end
  inventory.remove({ name = action.item, count = inserted })
  return result_ok({
    action = "insert",
    item = action.item,
    inserted = inserted,
    target = target.name,
    target_unit_number = target.unit_number
  })
end

local function action_take(agent, action)
  if type(action.item) ~= "string" then
    return result_err("take requires item")
  end
  local target = find_entity(agent.surface, action)
  if not target or not target.valid then
    return result_err("take target not found")
  end
  if distance(agent.position, target.position) > (action.reach or 32) then
    return result_err("take target out of reach")
  end
  local count = math.max(1, math.min(action.count or 1, 100))
  local taken = target.remove_item({ name = action.item, count = count })
  if taken <= 0 then
    return result_err("target does not have item", { target = target.name, item = action.item })
  end
  local inventory = main_inventory(agent)
  local inserted = inventory.insert({ name = action.item, count = taken })
  if inserted < taken then
    target.insert({ name = action.item, count = taken - inserted })
  end
  return result_ok({
    action = "take",
    item = action.item,
    taken = inserted,
    target = target.name,
    target_unit_number = target.unit_number
  })
end

local function update_virtual_agent_movement()
  local agent = storage.ai_agent
  if not agent or not agent.move_goal then
    return
  end
  local surface = game.get_surface(agent.surface_name or "nauvis") or game.surfaces[1]
  if not surface then
    return
  end
  if not agent.character or not agent.character.valid then
    agent.character = create_agent_character(surface, agent.position or agent.move_goal)
  end
  local character = agent.character
  if not character or not character.valid then
    agent.last_move_error = "agent character is not valid"
    return
  end

  agent.position = { x = character.position.x, y = character.position.y }
  agent.surface_name = character.surface.name

  local remaining = distance(agent.position, agent.move_goal)
  if remaining <= MOVE_STOP_DISTANCE then
    agent.move_goal = nil
    agent.last_move_error = nil
    stop_character_command(character)
    return
  end
  local dx = agent.move_goal.x - agent.position.x
  local dy = agent.move_goal.y - agent.position.y
  local step = math.min(PLAYER_MOVE_STEP, remaining)
  local next_position = {
    x = agent.position.x + dx / remaining * step,
    y = agent.position.y + dy / remaining * step
  }
  local ok, moved = pcall(function()
    return character.teleport(next_position, surface)
  end)
  if not ok or moved == false then
    agent.last_move_error = "agent character teleport step failed"
    agent.move_goal = nil
    stop_character_command(character)
    return
  end
  agent.position = { x = character.position.x, y = character.position.y }
  set_character_walking(character, true, walking_direction(dx, dy))
end

local function update_player_agent_movement()
  local goals = storage.ai_player_move_goals
  if not goals then
    return
  end
  for index, goal in pairs(goals) do
    local player = game.get_player(tonumber(index))
    if not player or not player.valid or not player.character or not player.character.valid then
      goals[index] = nil
    else
      local remaining = distance(player.position, goal)
      if remaining <= MOVE_STOP_DISTANCE then
        goals[index] = nil
        set_character_walking(player, false)
      else
        local dx = goal.x - player.position.x
        local dy = goal.y - player.position.y
        local step = math.min(PLAYER_MOVE_STEP, remaining)
        local next_position = {
          x = player.position.x + dx / remaining * step,
          y = player.position.y + dy / remaining * step
        }
        local ok, moved = pcall(function()
          return player.teleport(next_position, player.surface)
        end)
        if not ok or moved == false then
          goals[index] = nil
          set_character_walking(player, false)
        else
          set_character_walking(
            player,
            true,
            walking_direction(dx, dy)
          )
        end
      end
    end
  end
end

local function execute_action(command, action)
  if type(action) ~= "table" then
    return result_err("action payload must be a json object")
  end
  local action_type = action.type
  if type(action_type) ~= "string" then
    return result_err("action.type is required")
  end
  local agent = ensure_agent(command, action)
  if action_type == "move_to" then
    return action_move_to(agent, action)
  elseif action_type == "mine" then
    return action_mine(agent, action)
  elseif action_type == "craft" then
    return action_craft(agent, action)
  elseif action_type == "build" then
    return action_build(agent, action)
  elseif action_type == "connect_power" then
    return action_connect_power(agent, action)
  elseif action_type == "insert" then
    return action_insert(agent, action)
  elseif action_type == "take" then
    return action_take(agent, action)
  elseif action_type == "set_recipe" then
    return action_set_recipe(agent, action)
  elseif action_type == "research" then
    return action_research(agent, action)
  elseif action_type == "wait" then
    return result_ok({ action = "wait", ticks = action.ticks or 60, target_tick = game.tick + (action.ticks or 60) })
  end
  return result_err("unsupported action type", { action_type = action_type })
end

script.on_init(function()
  configure_freeplay()
end)

script.on_configuration_changed(function()
  configure_freeplay()
end)

script.on_event(defines.events.on_tick, function()
  update_virtual_agent_movement()
  update_player_agent_movement()
end)

script.on_event(defines.events.on_player_created, function(event)
  local player = game.get_player(event.player_index)
  focus_player_on_agent(player)
end)

script.on_event(defines.events.on_player_joined_game, function(event)
  local player = game.get_player(event.player_index)
  focus_player_on_agent(player)
end)

commands.add_command("ai_observe", "Return Factorio AI observation JSON.", function(command)
  local ok, payload = pcall(function()
    local options = json_decode(command.parameter) or {}
    return observe(command, options)
  end)
  if ok then
    reply(command, payload)
  else
    reply(command, result_err("observe error", { error = tostring(payload) }))
  end
end)

commands.add_command("ai_action", "Execute an allowlisted Factorio AI action from JSON.", function(command)
  local ok, payload = pcall(function()
    local action = json_decode(command.parameter)
    return execute_action(command, action)
  end)
  if ok then
    reply(command, payload)
  else
    reply(command, result_err("action error", { error = tostring(payload) }))
  end
end)

commands.add_command("ai_wait", "Return a wait target tick.", function(command)
  local ticks = tonumber(command.parameter) or 60
  if ticks < 1 then
    ticks = 1
  end
  if ticks > 36000 then
    ticks = 36000
  end
  reply(command, result_ok({ action = "wait", ticks = ticks, target_tick = game.tick + ticks }))
end)
