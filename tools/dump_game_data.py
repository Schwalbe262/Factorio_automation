"""Dump authoritative recipe + technology prototypes from the running Factorio server.

Usage:  set PYTHONPATH=src && python tools/dump_game_data.py [out.json]

Connects over RCON/Lua (Factorio 2.0 `prototypes.*`) and writes the full base-game
dependency data so knowledge.py can be regenerated from ground truth instead of memory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from factorio_ai.config import load_config
from factorio_ai.modless_lua import _silent_command, execute_json_lua_command

# Lua runs inside /silent-command (game + helpers + prototypes are available in 2.0).
_DUMP_LUA = r"""
local out = { ok = true, recipes = {}, technologies = {}, items = {}, fluids = {} }
for name, proto in pairs(prototypes.recipe) do
  local ings = {}
  for _, ing in pairs(proto.ingredients) do ings[ing.name] = ing.amount end
  local prods = {}
  for _, pr in pairs(proto.products) do
    local amt = pr.amount
    if amt == nil and pr.amount_min ~= nil then amt = (pr.amount_min + (pr.amount_max or pr.amount_min)) / 2 end
    prods[pr.name] = (amt or 1) * (pr.probability or 1)
  end
  out.recipes[name] = {
    energy = proto.energy,
    category = proto.category,
    enabled = proto.enabled,
    group = proto.group and proto.group.name or nil,
    subgroup = proto.subgroup and proto.subgroup.name or nil,
    ingredients = ings,
    products = prods,
  }
end
for name, tech in pairs(prototypes.technology) do
  local prereq = {}
  for pname, _ in pairs(tech.prerequisites or {}) do prereq[#prereq+1] = pname end
  local packs = {}
  local count = 0
  for _, ing in pairs(tech.research_unit_ingredients or {}) do packs[ing.name] = ing.amount end
  if tech.research_unit_count then count = tech.research_unit_count end
  local unlocks = {}
  for _, eff in pairs(tech.effects or {}) do
    if eff.type == "unlock-recipe" and eff.recipe then unlocks[#unlocks+1] = eff.recipe end
  end
  out.technologies[name] = {
    prerequisites = prereq,
    science_packs = packs,
    unit_count = count,
    unlocks = unlocks,
  }
end
-- fluids (authoritative): used to flag whether an item/ingredient is a fluid
for name, _ in pairs(prototypes.fluid) do out.fluids[#out.fluids+1] = name end
-- crafting machines -> full physical profile (crafting_speed, power, module slots, footprint,
-- categories) for the deterministic cell compiler/placer. Filter to machine entity types only
-- (cheap) instead of scanning every prototype (that timed out RCON). Fields are nil-safe: any
-- prototype attribute that doesn't exist on this version just comes back nil and is dropped.
out.machines = {}
local machine_types = { ["assembling-machine"] = true, ["furnace"] = true, ["rocket-silo"] = true }
for name, proto in pairs(prototypes.entity) do
  if machine_types[proto.type] then
    local cats = {}
    if proto.crafting_categories then
      for c, _ in pairs(proto.crafting_categories) do cats[#cats+1] = c end
    end
    out.machines[name] = {
      crafting_speed = proto.crafting_speed,
      energy_usage = proto.energy_usage,            -- W, active draw
      module_inventory_size = proto.module_inventory_size,
      tile_width = proto.tile_width,
      tile_height = proto.tile_height,
      crafting_categories = cats,
    }
  end
end
-- modules: speed/productivity/consumption/pollution effects (effect values are numbers in 2.0;
-- guard for the older {bonus=x} table shape too).
local function eff_val(e)
  if type(e) == "number" then return e end
  if type(e) == "table" then return e.bonus or 0 end
  return 0
end
out.modules = {}
for name, proto in pairs(prototypes.item) do
  if proto.type == "module" then
    local m = proto.module_effects or {}
    out.modules[name] = {
      effect = {
        speed = eff_val(m.speed), productivity = eff_val(m.productivity),
        consumption = eff_val(m.consumption), pollution = eff_val(m.pollution),
      },
      tier = proto.tier,
      category = proto.category,
    }
  end
end
-- transport belts: belt_speed is tiles/tick; items/s = belt_speed * 480 (computed in knowledge.py).
out.belts = {}
for name, proto in pairs(prototypes.entity) do
  if proto.type == "transport-belt" then
    out.belts[name] = { belt_speed = proto.belt_speed }
  end
end
-- electric poles: supply area + wire reach for the placer's power-coverage solver.
out.poles = {}
for name, proto in pairs(prototypes.entity) do
  if proto.type == "electric-pole" then
    out.poles[name] = {
      supply_area_distance = proto.supply_area_distance,
      maximum_wire_distance = proto.max_wire_distance,
      tile_width = proto.tile_width,
      tile_height = proto.tile_height,
    }
  end
end
helpers.write_file("game_data_dump.json", helpers.table_to_json(out), false)
rcon.print(helpers.table_to_json({ ok = true, fluids = #out.fluids, written = "script-output/game_data_dump.json" }))
"""


def main() -> int:
    cfg = load_config()
    command = _silent_command(_DUMP_LUA)
    client = __import__("factorio_ai.rcon", fromlist=["FactorioRconClient"]).FactorioRconClient(
        cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, timeout=60.0
    )
    with client:
        result = execute_json_lua_command(client, command)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
