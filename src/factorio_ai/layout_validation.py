from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from pathlib import Path
from typing import Any

from .blueprints import decode_blueprint_string
from .config import AppConfig
from .knowledge import RECIPES
from .modless_lua import execute_json_lua_command
from .planner import factory_layout_simulation_candidates
from .rcon import FactorioRconClient


VALIDATION_LOG_NAME = "layout-validation-feedback.jsonl"
_DIRECTION_VECTORS = {
    0: (0.0, -1.0),
    2: (1.0, -1.0),
    4: (1.0, 0.0),
    6: (1.0, 1.0),
    8: (0.0, 1.0),
    10: (-1.0, 1.0),
    12: (-1.0, 0.0),
    14: (-1.0, -1.0),
}
_ASSEMBLER_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
_BELT_NAMES = {"transport-belt", "fast-transport-belt", "express-transport-belt"}


def validate_layout_candidate(
    cfg: AppConfig,
    observation: dict[str, Any],
    *,
    candidate_id: str,
    variant: str = "after",
    ticks: int = 3600,
    cleanup: bool = True,
) -> dict[str, Any]:
    candidate = find_layout_candidate(observation, candidate_id)
    static_validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
    entities = candidate_blueprint_entities(candidate, variant=variant)
    payload = sandbox_payload_for_entities(
        candidate_id=candidate_id,
        variant=variant,
        entities=entities,
        ticks=ticks,
        cleanup=cleanup,
    )

    with FactorioRconClient(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, timeout=15.0) as client:
        setup = execute_json_lua_command(client, build_sandbox_setup_command(payload))
        wait_seconds = max(0.0, int(ticks) / 60.0)
        if wait_seconds:
            time.sleep(wait_seconds)
        inspect_payload = {
            "surface_name": setup.get("surface_name") or payload["surface_name"],
            "candidate_id": candidate_id,
            "variant": normalized_variant(variant),
            "ticks": int(ticks),
            "expected_outputs": payload["expected_outputs"],
            "cleanup": cleanup,
        }
        inspected = execute_json_lua_command(client, build_sandbox_inspect_command(inspect_payload))

    sandbox_validation = inspected.get("sandbox_validation") if isinstance(inspected.get("sandbox_validation"), dict) else {}
    for key in ("reasons", "warnings", "build_failures", "machine_statuses", "inserter_statuses"):
        if key in sandbox_validation and not isinstance(sandbox_validation.get(key), list):
            sandbox_validation[key] = []
    if inspected.get("surface_name") and "surface_name" not in sandbox_validation:
        sandbox_validation["surface_name"] = inspected.get("surface_name")
    feedback = layout_validation_feedback_row(
        candidate_id=candidate_id,
        variant=variant,
        static_validation=static_validation,
        sandbox_validation=sandbox_validation,
    )
    append_layout_validation_feedback(cfg.log_dir, feedback)
    return feedback


def find_layout_candidate(observation: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for candidate in factory_layout_simulation_candidates(observation):
        if isinstance(candidate, dict) and str(candidate.get("candidate_id") or "") == candidate_id:
            return candidate
    raise ValueError(f"layout candidate not found: {candidate_id}")


def candidate_blueprint_entities(candidate: dict[str, Any], *, variant: str = "after") -> list[dict[str, Any]]:
    variant = normalized_variant(variant)
    keys = ["before_blueprint"] if variant == "before" else ["after_blueprint", "blueprint"]
    for key in keys:
        blueprint = candidate.get(key)
        if not isinstance(blueprint, dict):
            continue
        exchange_string = blueprint.get("exchange_string")
        if not isinstance(exchange_string, str) or not exchange_string:
            continue
        payload = decode_blueprint_string(exchange_string)
        entities = payload.get("blueprint", {}).get("entities")
        if isinstance(entities, list):
            return [entity for entity in entities if isinstance(entity, dict)]
    raise ValueError(f"{variant} blueprint not found for candidate {candidate.get('candidate_id')}")


def sandbox_payload_for_entities(
    *,
    candidate_id: str,
    variant: str,
    entities: list[dict[str, Any]],
    ticks: int,
    cleanup: bool = True,
) -> dict[str, Any]:
    normalized = [_sandbox_entity(entity) for entity in entities if isinstance(entity, dict)]
    terminal_inputs = _terminal_recipe_inputs(normalized)
    expected_outputs = _terminal_recipe_outputs(normalized)
    return {
        "candidate_id": candidate_id,
        "variant": normalized_variant(variant),
        "surface_name": _sandbox_surface_name(candidate_id, variant),
        "entities": normalized,
        "bounds": _entity_bounds(normalized),
        "input_feeds": _input_feeds(normalized, terminal_inputs),
        "expected_outputs": expected_outputs,
        "ticks": int(ticks),
        "cleanup": cleanup,
    }


def build_sandbox_setup_command(payload: dict[str, Any]) -> str:
    return _silent_command(
        _COMMON_LUA
        + "\nlocal payload = helpers.json_to_table("
        + _lua_string(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        + ")\n"
        + _SETUP_LUA
    )


def build_sandbox_inspect_command(payload: dict[str, Any]) -> str:
    return _silent_command(
        _COMMON_LUA
        + "\nlocal payload = helpers.json_to_table("
        + _lua_string(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        + ")\n"
        + _INSPECT_LUA
    )


def layout_validation_feedback_row(
    *,
    candidate_id: str,
    variant: str,
    static_validation: dict[str, Any],
    sandbox_validation: dict[str, Any],
    timestamp: str | None = None,
) -> dict[str, Any]:
    status = str(sandbox_validation.get("status") or "warning")
    reasons = sandbox_validation.get("reasons") if isinstance(sandbox_validation.get("reasons"), list) else []
    if status == "pass":
        lesson = (
            f"{candidate_id} produced expected outputs in sandbox ticks; it may be considered build-ready "
            "only after site-specific collision, resource, and logistics checks."
        )
    else:
        reason_text = "; ".join(str(item) for item in reasons[:3]) or "sandbox validation did not prove operation"
        lesson = (
            f"Do not mark {candidate_id} build-ready until sandbox ticks prove input flow, power, "
            f"and output movement. Latest failure: {reason_text}"
        )
    return {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "candidate_id": candidate_id,
        "variant": normalized_variant(variant),
        "static_validation": static_validation,
        "sandbox_validation": sandbox_validation,
        "lesson": lesson,
    }


def append_layout_validation_feedback(log_dir: Path, row: dict[str, Any]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / VALIDATION_LOG_NAME
    with path.open("a", encoding="utf-8") as file:
        json.dump(row, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")
    return path


def layout_validation_feedback_summary(log_dir: Path, *, limit: int = 50) -> dict[str, Any]:
    path = log_dir / VALIDATION_LOG_NAME
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict):
                    rows.append(raw)
    entries = rows[-limit:] if limit >= 0 else rows
    latest = latest_sandbox_validation_by_candidate(rows)
    return {
        "entries": entries,
        "entry_count": len(rows),
        "latest": entries[-1] if entries else None,
        "latest_by_candidate": latest,
        "log_path": str(path),
    }


def latest_sandbox_validation_by_candidate(rows: list[Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        latest[candidate_id] = row
    return latest


def merge_sandbox_validation_feedback(layout: dict[str, Any], feedback: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(layout) if isinstance(layout, dict) else {}
    latest = feedback.get("latest_by_candidate") if isinstance(feedback.get("latest_by_candidate"), dict) else {}
    candidates = output.get("simulation_candidates") if isinstance(output.get("simulation_candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        row = latest.get(str(candidate.get("candidate_id") or ""))
        if not isinstance(row, dict):
            continue
        sandbox_validation = row.get("sandbox_validation")
        if isinstance(sandbox_validation, dict):
            candidate["sandbox_validation"] = sandbox_validation
            candidate["sandbox_validation_timestamp"] = row.get("timestamp")
            candidate["sandbox_validation_lesson"] = row.get("lesson")
    return output


def normalized_variant(value: str) -> str:
    return "before" if str(value).strip().lower() in {"before", "current"} else "after"


def _sandbox_entity(entity: dict[str, Any]) -> dict[str, Any]:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    output: dict[str, Any] = {
        "name": str(entity.get("name") or ""),
        "position": {
            "x": float(position.get("x") or 0.0),
            "y": float(position.get("y") or 0.0),
        },
    }
    if entity.get("direction") is not None:
        output["direction"] = int(entity.get("direction") or 0)
    if isinstance(entity.get("recipe"), str) and entity.get("recipe"):
        output["recipe"] = str(entity["recipe"])
    return output


def _terminal_recipe_inputs(entities: list[dict[str, Any]]) -> set[str]:
    produced = _recipe_products(entities)
    inputs: set[str] = set()
    for entity in entities:
        recipe = RECIPES.get(str(entity.get("recipe") or ""))
        if not recipe:
            continue
        inputs.update(name for name in recipe.ingredients if name not in produced)
    return inputs


def _terminal_recipe_outputs(entities: list[dict[str, Any]]) -> list[str]:
    products = _recipe_products(entities)
    consumed = set()
    for entity in entities:
        recipe = RECIPES.get(str(entity.get("recipe") or ""))
        if recipe:
            consumed.update(recipe.ingredients)
    terminal = sorted(name for name in products if name not in consumed)
    return terminal or sorted(products)


def _recipe_products(entities: list[dict[str, Any]]) -> set[str]:
    products: set[str] = set()
    for entity in entities:
        recipe = RECIPES.get(str(entity.get("recipe") or ""))
        if recipe:
            products.update(recipe.products)
    return products


def _input_feeds(entities: list[dict[str, Any]], terminal_inputs: set[str]) -> list[dict[str, Any]]:
    belts = {_position_key(entity["position"]): entity for entity in entities if entity.get("name") in _BELT_NAMES}
    output_belt_lanes = _assembler_output_belt_lanes(entities, belts)
    feeds: dict[tuple[str, float], dict[str, Any]] = {}
    for assembler in [entity for entity in entities if entity.get("name") in _ASSEMBLER_NAMES]:
        recipe = RECIPES.get(str(assembler.get("recipe") or ""))
        if not recipe:
            continue
        ingredients = [name for name in recipe.ingredients if name in terminal_inputs]
        if not ingredients:
            continue
        source_belts = _source_belts_for_assembler(assembler, entities, belts)
        preferred = [item for item in source_belts if _belt_lane(item) not in output_belt_lanes]
        selected_sources = preferred or source_belts
        for index, source in enumerate(selected_sources[: len(ingredients)]):
            item = ingredients[min(index, len(ingredients) - 1)]
            axis, coordinate = _belt_lane(source)
            feeds[(item, coordinate)] = {
                "item": item,
                "axis": axis,
                "coordinate": coordinate,
                "count_per_belt_line": 3,
            }
    return sorted(feeds.values(), key=lambda item: (str(item["item"]), float(item["coordinate"])))


def _assembler_output_belt_lanes(entities: list[dict[str, Any]], belts: dict[tuple[int, int], dict[str, Any]]) -> set[tuple[str, float]]:
    output_lanes: set[tuple[str, float]] = set()
    assemblers = [entity for entity in entities if entity.get("name") in _ASSEMBLER_NAMES]
    for inserter in [entity for entity in entities if str(entity.get("name") or "").endswith("inserter")]:
        direction = int(inserter.get("direction") or 0)
        vector = _DIRECTION_VECTORS.get(direction, (0.0, -1.0))
        position = inserter["position"]
        pickup = {"x": position["x"] + vector[0], "y": position["y"] + vector[1]}
        drop = {"x": position["x"] - vector[0], "y": position["y"] - vector[1]}
        if any(_near_machine(pickup, assembler["position"]) for assembler in assemblers):
            drop_key = _position_key(drop)
            if drop_key in belts:
                output_lanes.add(_belt_lane(belts[drop_key]))
    return output_lanes


def _source_belts_for_assembler(
    assembler: dict[str, Any],
    entities: list[dict[str, Any]],
    belts: dict[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for inserter in [entity for entity in entities if str(entity.get("name") or "").endswith("inserter")]:
        direction = int(inserter.get("direction") or 0)
        vector = _DIRECTION_VECTORS.get(direction, (0.0, -1.0))
        position = inserter["position"]
        drop = {"x": position["x"] - vector[0], "y": position["y"] - vector[1]}
        if not _near_machine(drop, assembler["position"]):
            continue
        pickup = {"x": position["x"] + vector[0], "y": position["y"] + vector[1]}
        source = belts.get(_position_key(pickup))
        if source:
            sources.append(source)
    sources.sort(key=lambda item: (float(item["position"]["x"]), float(item["position"]["y"])))
    return sources


def _belt_lane(entity: dict[str, Any]) -> tuple[str, float]:
    direction = int(entity.get("direction") or 0)
    position = entity["position"]
    if direction in {0, 8}:
        return "x", float(position["x"])
    return "y", float(position["y"])


def _near_machine(position: dict[str, float], center: dict[str, float]) -> bool:
    return abs(float(position["x"]) - float(center["x"])) <= 1.6 and abs(float(position["y"]) - float(center["y"])) <= 1.6


def _position_key(position: dict[str, float]) -> tuple[int, int]:
    return (int(round(float(position["x"]))), int(round(float(position["y"]))))


def _entity_bounds(entities: list[dict[str, Any]]) -> dict[str, float]:
    xs = [float(entity.get("position", {}).get("x") or 0.0) for entity in entities]
    ys = [float(entity.get("position", {}).get("y") or 0.0) for entity in entities]
    if not xs or not ys:
        return {"min_x": -8.0, "max_x": 8.0, "min_y": -8.0, "max_y": 8.0}
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}


def _sandbox_surface_name(candidate_id: str, variant: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", candidate_id).strip("-")[:42] or "candidate"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"codex-layout-{safe}-{normalized_variant(variant)}-{timestamp}"


def _lua_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _silent_command(lua: str) -> str:
    return "/silent-command " + " ".join(line.strip() for line in lua.splitlines() if line.strip())


_COMMON_LUA = """
local function json_reply(payload)
  local encode = helpers and helpers.table_to_json or nil
  if not encode and game and game.table_to_json then encode = game.table_to_json end
  if not encode then rcon.print('{"ok":false,"reason":"json encoder unavailable"}'); return end
  rcon.print(encode(payload))
end
local function round2(value)
  return math.floor((value or 0) * 100 + 0.5) / 100
end
local function state_root()
  if storage then return storage end
  return global
end
local function ensure_validation_root()
  local root = state_root()
  root.factorio_ai_layout_validation = root.factorio_ai_layout_validation or {}
  return root.factorio_ai_layout_validation
end
local function position_key(position)
  return tostring(math.floor((position.x or position[1] or 0) + 0.5)) .. "," .. tostring(math.floor((position.y or position[2] or 0) + 0.5))
end
local function inventory_contents(inventory)
  local output = {}
  if not inventory or not inventory.valid then return output end
  local ok, raw = pcall(function() return inventory.get_contents() end)
  if not ok or not raw then return output end
  for name, count in pairs(raw) do
    if type(count) == "number" then
      output[name] = (output[name] or 0) + count
    elseif type(count) == "table" and count.name and count.count then
      output[count.name] = (output[count.name] or 0) + count.count
    end
  end
  return output
end
local function add_counts(target, source)
  for name, count in pairs(source or {}) do
    if type(count) == "number" then
      target[name] = (target[name] or 0) + count
    elseif type(count) == "table" and count.name and count.count then
      target[count.name] = (target[count.name] or 0) + count.count
    end
  end
end
local function entity_recipe_name(entity)
  local ok, recipe = pcall(function() return entity.get_recipe() end)
  if ok and recipe and recipe.name then return recipe.name end
  return nil
end
local function entity_status_name(entity)
  local status_names = {}
  if defines and defines.entity_status then
    for name, value in pairs(defines.entity_status) do status_names[value] = name end
  end
  local ok, status = pcall(function() return entity.status end)
  if ok and status then return status_names[status] or tostring(status) end
  return nil
end
local function connected_to_power(entity)
  local ok, connected = pcall(function() return entity.is_connected_to_electric_network() end)
  if ok then return connected end
  return nil
end
local function has_entity_prototype(name)
  local ok, value = pcall(function()
    if prototypes and prototypes.entity then return prototypes.entity[name] end
    if game.entity_prototypes then return game.entity_prototypes[name] end
    return nil
  end)
  return ok and value ~= nil
end
"""


_SETUP_LUA = """
local root = ensure_validation_root()
local surface_name = payload.surface_name
if game.surfaces[surface_name] then
  game.delete_surface(game.surfaces[surface_name])
end
local surface = game.create_surface(surface_name, { width = 96, height = 96 })
surface.always_day = true
surface.request_to_generate_chunks({0, 0}, 4)
surface.force_generate_chunk_requests()
local force = game.forces.player
local build_failures = {}
local created = {}
for _, spec in pairs(payload.entities or {}) do
  local entity = surface.create_entity({
    name = spec.name,
    position = spec.position,
    direction = spec.direction or 0,
    force = force,
    create_build_effect_smoke = false,
    raise_built = false
  })
  if entity and entity.valid then
    if spec.recipe then
      local ok, err = pcall(function() entity.set_recipe(spec.recipe) end)
      if not ok then
        table.insert(build_failures, "set_recipe failed for " .. spec.name .. " at " .. position_key(spec.position) .. ": " .. tostring(err))
      end
    end
    table.insert(created, { name = entity.name, unit_number = entity.unit_number, position = { x = round2(entity.position.x), y = round2(entity.position.y) } })
  else
    table.insert(build_failures, "create_entity failed for " .. tostring(spec.name) .. " at " .. position_key(spec.position))
  end
end
local bounds = payload.bounds or { min_x = -8, max_x = 8, min_y = -8, max_y = 8 }
local power_y = ((bounds.min_y or -8) + (bounds.max_y or 8)) / 2
if has_entity_prototype("substation") and has_entity_prototype("solar-panel") then
  local center_x = ((bounds.min_x or -8) + (bounds.max_x or 8)) / 2
  local anchors = {
    { x = (bounds.min_x or -8) - 6, y = power_y, sx = -1, sy = 0 },
    { x = (bounds.max_x or 8) + 6, y = power_y, sx = 1, sy = 0 },
    { x = center_x, y = (bounds.min_y or -8) - 6, sx = 0, sy = -1 },
    { x = center_x, y = (bounds.max_y or 8) + 6, sx = 0, sy = 1 },
    { x = center_x, y = power_y, sx = 1, sy = 0 }
  }
  for _, anchor in pairs(anchors) do
    pcall(function() surface.create_entity({ name = "substation", position = anchor, force = force }) end)
    for dx = 0, 2 do
      for dy = 0, 2 do
        pcall(function()
          local solar_x = anchor.x + (anchor.sx or 0) * (5 + dx * 3)
          local solar_y = anchor.y + (anchor.sy or 0) * (5 + dy * 3)
          if (anchor.sx or 0) ~= 0 then solar_y = anchor.y - 4 + dy * 3 end
          if (anchor.sy or 0) ~= 0 then solar_x = anchor.x - 4 + dx * 3 end
          surface.create_entity({ name = "solar-panel", position = { x = solar_x, y = solar_y }, force = force })
        end)
      end
    end
  end
end
if has_entity_prototype("small-electric-pole") then
  for x = math.floor((bounds.min_x or -8) - 2), math.ceil((bounds.max_x or 8) + 2), 4 do
    for y = math.floor((bounds.min_y or -8) - 2), math.ceil((bounds.max_y or 8) + 2), 4 do
      pcall(function() surface.create_entity({ name = "small-electric-pole", position = { x = x, y = y }, force = force }) end)
    end
  end
end
local input_insertions = {}
for _, belt in pairs(surface.find_entities_filtered({ type = "transport-belt" })) do
  for _, feed in pairs(payload.input_feeds or {}) do
    local coordinate = feed.coordinate or 0
    local matches = false
    if feed.axis == "x" then
      matches = math.abs((belt.position.x or 0) - coordinate) <= 0.75
    else
      matches = math.abs((belt.position.y or 0) - coordinate) <= 0.75
    end
    if matches then
      for line_index = 1, 2 do
        local line = belt.get_transport_line(line_index)
        local inserted_here = 0
        for slot = 0, 8 do
          local offset = slot / 10
          local inserted = false
          local ok = pcall(function() inserted = line.insert_at(offset, feed.item) end)
          if not ok or not inserted then
            ok = pcall(function() inserted = line.insert_at_back({ name = feed.item, count = 1 }) end)
          end
          if ok and inserted then
            input_insertions[feed.item] = (input_insertions[feed.item] or 0) + 1
            inserted_here = inserted_here + 1
          end
          if inserted_here >= (feed.count_per_belt_line or 6) then break end
        end
      end
    end
  end
end
root[surface_name] = {
  candidate_id = payload.candidate_id,
  variant = payload.variant,
  started_tick = game.tick,
  build_failures = build_failures,
  created_count = #created,
  input_insertions = input_insertions,
  expected_outputs = payload.expected_outputs or {}
}
json_reply({
  ok = true,
  surface_name = surface_name,
  created_count = #created,
  build_failures = build_failures,
  input_insertions = input_insertions,
  expected_outputs = payload.expected_outputs or {}
})
"""


_INSPECT_LUA = """
local root = ensure_validation_root()
local surface_name = payload.surface_name
local state = root[surface_name]
local surface = game.surfaces[surface_name]
if not state or not surface then
  json_reply({ ok = false, sandbox_validation = { status = "fail", reasons = { "sandbox surface state was not found" }, ticks = 0 } })
  return
end
local observed_outputs = {}
local machine_statuses = {}
local inserter_statuses = {}
local checked_machines = 0
local powered_machines = 0
local checked_inserters = 0
local powered_inserters = 0
for _, entity in pairs(surface.find_entities_filtered({})) do
  if entity.valid then
    if entity.type == "assembling-machine" then
      checked_machines = checked_machines + 1
      local powered = connected_to_power(entity)
      if powered then powered_machines = powered_machines + 1 end
      table.insert(machine_statuses, {
        name = entity.name,
        recipe = entity_recipe_name(entity),
        position = { x = round2(entity.position.x), y = round2(entity.position.y) },
        status = entity_status_name(entity),
        powered = powered
      })
    elseif entity.type == "inserter" then
      checked_inserters = checked_inserters + 1
      local powered = connected_to_power(entity)
      if powered then powered_inserters = powered_inserters + 1 end
      table.insert(inserter_statuses, {
        name = entity.name,
        position = { x = round2(entity.position.x), y = round2(entity.position.y) },
        direction = entity.direction,
        status = entity_status_name(entity),
        powered = powered
      })
    end
    if entity.type == "container" or entity.type == "logistic-container" or entity.type == "assembling-machine" then
      for _, inventory_id in pairs(defines.inventory) do
        local ok, inventory = pcall(function() return entity.get_inventory(inventory_id) end)
        if ok and inventory and inventory.valid then
          add_counts(observed_outputs, inventory_contents(inventory))
        end
      end
    elseif entity.type == "transport-belt" then
      for line_index = 1, 2 do
        local line = entity.get_transport_line(line_index)
        local ok, contents = pcall(function() return line.get_contents() end)
        if ok and contents then add_counts(observed_outputs, contents) end
      end
    end
  end
end
local reasons = {}
local warnings = {}
for _, failure in pairs(state.build_failures or {}) do
  table.insert(reasons, failure)
end
if checked_machines == 0 then
  table.insert(reasons, "no assembling machines were available for sandbox validation")
end
if checked_machines > 0 and powered_machines == 0 then
  table.insert(reasons, "no assembling machines were connected to an electric network")
end
if checked_inserters > 0 and powered_inserters == 0 then
  table.insert(reasons, "no inserters were connected to an electric network")
end
for _, item in pairs(payload.expected_outputs or state.expected_outputs or {}) do
  if (observed_outputs[item] or 0) <= 0 then
    table.insert(reasons, "expected output " .. item .. " was not observed after sandbox ticks")
  end
end
local fed_input_count = 0
for _, count in pairs(state.input_insertions or {}) do fed_input_count = fed_input_count + (count or 0) end
local waiting_source_inserters = 0
for _, row in pairs(inserter_statuses) do
  if row.status == "waiting_for_source_items" then waiting_source_inserters = waiting_source_inserters + 1 end
end
if fed_input_count > 0 and waiting_source_inserters > 0 then
  local message = "sandbox fed input items, but some inserters still waited for source items; this may indicate intermittent supply, pickup lane, inserter orientation, or belt-side risk"
  local any_expected_output = false
  for _, item in pairs(payload.expected_outputs or state.expected_outputs or {}) do
    if (observed_outputs[item] or 0) > 0 then any_expected_output = true end
  end
  if any_expected_output then
    table.insert(warnings, message)
  else
    table.insert(reasons, message)
  end
end
local status = "pass"
if #reasons > 0 then status = "fail" end
local result = {
  status = status,
  reasons = reasons,
  warnings = warnings,
  observed_outputs = observed_outputs,
  ticks = math.max(0, game.tick - (state.started_tick or game.tick)),
  requested_ticks = payload.ticks,
  checked_machines = checked_machines,
  powered_machines = powered_machines,
  checked_inserters = checked_inserters,
  powered_inserters = powered_inserters,
  build_failures = state.build_failures or {},
  input_insertions = state.input_insertions or {},
  machine_statuses = machine_statuses,
  inserter_statuses = inserter_statuses
}
if payload.cleanup then
  root[surface_name] = nil
  if game.surfaces[surface_name] then game.delete_surface(game.surfaces[surface_name]) end
end
json_reply({ ok = status == "pass", surface_name = surface_name, sandbox_validation = result })
"""
