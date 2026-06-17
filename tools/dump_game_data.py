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
helpers.write_file("game_data_dump.json", helpers.table_to_json(out), false)
rcon.print(helpers.table_to_json({ ok = true, recipes = 0, written = "script-output/game_data_dump.json" }))
"""


def main() -> int:
    cfg = load_config()
    command = _silent_command(_DUMP_LUA)
    client = __import__("factorio_ai.rcon", fromlist=["FactorioRconClient"]).FactorioRconClient(
        cfg.rcon_host, cfg.rcon_port, cfg.rcon_password
    )
    with client:
        result = execute_json_lua_command(client, command)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
