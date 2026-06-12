local MOD = "factorio_ai_autoplayer"
local AGENT_NAME = "AI"
local OBSERVE_RADIUS = 512
local MOVE_STOP_DISTANCE = 0.35
local PLAYER_MOVE_STEP = 0.10

local VIRTUAL_STARTER_ITEMS = {
  ["burner-mining-drill"] = 1,
  ["stone-furnace"] = 1
}

local VIRTUAL_RECIPES = {
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
  }
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

local function collect_entities(surface, position)
  local entities = {}
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
    "transport-belt",
    "inserter",
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
      if entity.valid then
        table.insert(entities, {
          unit_number = entity.unit_number,
          name = entity.name,
          type = entity.type,
          position = position_table(entity.position),
          direction = entity.direction,
          status = entity.status,
          distance = round(distance(position, entity.position)),
          inventories = entity_inventory_snapshot(entity)
        })
      end
    end
  end
  local misc = surface.find_entities_filtered({
    position = position,
    radius = 32,
    limit = 80
  })
  for _, entity in pairs(misc) do
    if entity.valid and entity.name ~= "character" and entity.type ~= "resource" then
      table.insert(entities, {
        unit_number = entity.unit_number,
        name = entity.name,
        type = entity.type,
        position = position_table(entity.position),
        direction = entity.direction,
        status = entity.status,
        distance = round(distance(position, entity.position)),
        inventories = entity_inventory_snapshot(entity)
      })
    end
  end
  table.sort(entities, function(a, b)
    return a.distance < b.distance
  end)
  return entities
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
    entities = collect_entities(surface, position)
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
    force = agent.force
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
              force = agent.force
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
  return result_ok({
    action = "build",
    name = entity.name,
    unit_number = entity.unit_number,
    position = position_table(entity.position)
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
  elseif action_type == "insert" then
    return action_insert(agent, action)
  elseif action_type == "take" then
    return action_take(agent, action)
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
