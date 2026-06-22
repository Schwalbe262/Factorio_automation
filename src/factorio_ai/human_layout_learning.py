from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .models import distance
from .planner import factory_layout_structure


STATE_FILE = "human-layout-learning-state.json"
TRACE_FILE = "operator-intervention-layout-learning.jsonl"
SCHEMA_VERSION = 1
MAX_PENDING_AGENT_ACTIONS = 8
MAX_DELTA_ROWS = 20
DIRECT_TRANSFER_ENTITY_NAMES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "stone-furnace",
    "steel-furnace",
    "electric-furnace",
    "wooden-chest",
    "iron-chest",
    "steel-chest",
}
INSERTER_NAMES = {"inserter", "fast-inserter", "long-handed-inserter", "bulk-inserter", "stack-inserter"}
FACTORY_ENTITY_NAMES = {
    "transport-belt",
    "underground-belt",
    "splitter",
    "inserter",
    "burner-inserter",
    "long-handed-inserter",
    "fast-inserter",
    "wooden-chest",
    "iron-chest",
    "steel-chest",
    "stone-furnace",
    "steel-furnace",
    "electric-furnace",
    "burner-mining-drill",
    "electric-mining-drill",
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
    "offshore-pump",
    "boiler",
    "steam-engine",
    "lab",
    "pipe",
    "pipe-to-ground",
}


def human_layout_learning_state_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / STATE_FILE


def human_layout_learning_trace_path(log_dir: Path) -> Path:
    return Path(log_dir) / TRACE_FILE


def remember_agent_layout_action(
    runtime_dir: Path,
    action: dict[str, Any],
    *,
    objective: str,
    active_skill: str,
    active_step: int,
) -> None:
    """Remember the last deterministic layout mutation so the next diff can ignore it."""

    if not _layout_mutating_action(action):
        return
    state = _read_state(runtime_dir)
    pending = state.get("pending_agent_actions") if isinstance(state.get("pending_agent_actions"), list) else []
    pending.append(
        {
            "time": _now(),
            "objective": objective,
            "active_skill": active_skill,
            "active_step": active_step,
            "action": _compact_action(action),
        }
    )
    state["pending_agent_actions"] = pending[-MAX_PENDING_AGENT_ACTIONS:]
    _write_state(runtime_dir, state)


def record_human_layout_observation(
    runtime_dir: Path,
    log_dir: Path,
    observation: dict[str, Any],
    *,
    objective: str,
    active_skill: str,
    active_step: int,
    source: str,
) -> dict[str, Any] | None:
    """Persist a pending human-layout learning sample when layout changes are not explained by AI actions.

    The sample is intentionally labelled pending_review. It is high-value training evidence, but it
    must not be promoted to insight/fine-tuning as "better" until normal layout metrics confirm it.
    """

    snapshot = _layout_snapshot(observation)
    state = _read_state(runtime_dir)
    previous = state.get("last_snapshot") if isinstance(state.get("last_snapshot"), dict) else None
    pending_actions = (
        state.get("pending_agent_actions") if isinstance(state.get("pending_agent_actions"), list) else []
    )
    event: dict[str, Any] | None = None
    if previous is not None and not _snapshot_session_reset(previous, snapshot):
        delta = _snapshot_delta(previous, snapshot)
        if _delta_is_significant(delta) and not _delta_explained_by_agent(delta, pending_actions):
            event = _learning_event(
                previous,
                snapshot,
                delta,
                pending_actions,
                objective=objective,
                active_skill=active_skill,
                active_step=active_step,
                source=source,
            )
            _append_event(log_dir, event)
    state["last_snapshot"] = snapshot
    # Once an observation has been compared, the latest deterministic actions have had a chance to
    # appear in the world. Keep the state short so future manual edits are not masked by stale actions.
    state["pending_agent_actions"] = []
    _write_state(runtime_dir, state)
    return event


def _layout_snapshot(observation: dict[str, Any]) -> dict[str, Any]:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    compact_entities = {
        key: row
        for entity in entities
        if isinstance(entity, dict)
        for key, row in [_entity_snapshot(entity)]
        if key
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "time": _now(),
        "tick": observation.get("tick"),
        "entity_count": len(compact_entities),
        "entities": compact_entities,
        "site_structure": factory_layout_structure(observation),
    }


def _entity_snapshot(entity: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = str(entity.get("name") or "")
    position = _rounded_position(entity.get("position"))
    if not name or position is None:
        return "", {}
    if not _is_factory_entity_name(name):
        return "", {}
    unit_number = entity.get("unit_number")
    key = f"unit:{unit_number}" if unit_number is not None else f"{name}@{position['x']},{position['y']}"
    row: dict[str, Any] = {
        "name": name,
        "unit_number": unit_number,
        "position": position,
        "direction": entity.get("direction"),
    }
    if name.startswith("assembling-machine"):
        row["recipe"] = entity.get("recipe")
    return key, row


def _is_factory_entity_name(name: str) -> bool:
    return name.startswith("assembling-machine") or name in FACTORY_ENTITY_NAMES


def _snapshot_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    before = previous.get("entities") if isinstance(previous.get("entities"), dict) else {}
    after = current.get("entities") if isinstance(current.get("entities"), dict) else {}
    before_keys = set(before)
    after_keys = set(after)
    added = [after[key] for key in sorted(after_keys - before_keys)]
    removed = [before[key] for key in sorted(before_keys - after_keys)]
    changed: list[dict[str, Any]] = []
    for key in sorted(before_keys & after_keys):
        if before[key] != after[key]:
            changed.append({"before": before[key], "after": after[key]})
    return {
        "added": added[:MAX_DELTA_ROWS],
        "removed": removed[:MAX_DELTA_ROWS],
        "changed": changed[:MAX_DELTA_ROWS],
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
    }


def _snapshot_session_reset(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_tick = _int_or_none(previous.get("tick"))
    current_tick = _int_or_none(current.get("tick"))
    return previous_tick is not None and current_tick is not None and current_tick < previous_tick


def _delta_is_significant(delta: dict[str, Any]) -> bool:
    return int(delta.get("added_count") or 0) + int(delta.get("removed_count") or 0) + int(delta.get("changed_count") or 0) > 0


def _delta_explained_by_agent(delta: dict[str, Any], pending_actions: list[Any]) -> bool:
    actions = [item.get("action") for item in pending_actions if isinstance(item, dict) and isinstance(item.get("action"), dict)]
    return any(_action_explains_delta(action, delta) for action in actions)


def _action_explains_delta(action: dict[str, Any], delta: dict[str, Any]) -> bool:
    action_type = str(action.get("type") or "")
    added = [item for item in delta.get("added") or [] if isinstance(item, dict)]
    removed = [item for item in delta.get("removed") or [] if isinstance(item, dict)]
    changed = [item for item in delta.get("changed") or [] if isinstance(item, dict)]
    if action_type == "build":
        radius = 8.0 if action.get("allow_nearby") is True else 4.0
        return (
            len(added) == 1
            and not removed
            and not changed
            and added[0].get("name") == action.get("name")
            and _positions_close(added[0].get("position"), action.get("position"), radius=radius)
        )
    if action_type == "connect_entities":
        tiles = action.get("tiles") if isinstance(action.get("tiles"), list) else []
        tile_positions = [tile.get("position") for tile in tiles if isinstance(tile, dict)]
        return (
            bool(added)
            and not removed
            and not changed
            and len(added) <= len(tile_positions)
            and all(item.get("name") == action.get("name") for item in added)
            and all(
                any(_positions_close(item.get("position"), position, radius=1.25) for position in tile_positions)
                for item in added
            )
        )
    if action_type == "mine":
        return len(removed) == 1 and not added and not changed and _entity_matches_action(removed[0], action)
    if action_type in {"set_recipe", "rotate"}:
        if len(changed) != 1 or added or removed:
            return False
        after = changed[0].get("after") if isinstance(changed[0].get("after"), dict) else {}
        return _entity_matches_action(after, action)
    return False


def _learning_event(
    previous: dict[str, Any],
    current: dict[str, Any],
    delta: dict[str, Any],
    pending_actions: list[Any],
    *,
    objective: str,
    active_skill: str,
    active_step: int,
    source: str,
) -> dict[str, Any]:
    layout_features = _layout_features(current, delta)
    skill_candidate = _skill_candidate(layout_features, active_skill)
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "operator_intervention_candidate",
        "time": _now(),
        "objective": objective,
        "active_skill": active_skill,
        "active_step": active_step,
        "source": source,
        "learning_label": "pending_human_review",
        "promotion_policy": "promote only after throughput/footprint/power/bottleneck metrics improve",
        "before_tick": previous.get("tick"),
        "after_tick": current.get("tick"),
        "delta_summary": {
            "added": delta.get("added_count"),
            "removed": delta.get("removed_count"),
            "changed": delta.get("changed_count"),
        },
        "delta": {
            "added": delta.get("added") or [],
            "removed": delta.get("removed") or [],
            "changed": delta.get("changed") or [],
        },
        "expected_agent_actions": pending_actions[-MAX_PENDING_AGENT_ACTIONS:],
        "before_structure": previous.get("site_structure"),
        "after_structure": current.get("site_structure"),
        "layout_features": layout_features,
        "skill_candidate": skill_candidate,
    }


def _layout_features(current: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    delta_entities = _delta_present_entities(delta)
    after_entities = [
        item
        for item in (current.get("entities") or {}).values()
        if isinstance(item, dict)
    ]
    entity_counts = Counter(str(item.get("name") or "") for item in delta_entities if item.get("name"))
    recipes = Counter(
        str(item.get("recipe") or "")
        for item in delta_entities
        if str(item.get("recipe") or "")
    )
    cluster = _cluster_features(delta_entities)
    direct_transfers = _direct_transfer_features(after_entities, delta_entities)
    return {
        "delta_entity_counts": dict(sorted(entity_counts.items())),
        "delta_recipes": dict(sorted(recipes.items())),
        "cluster": cluster,
        "direct_transfers": direct_transfers,
    }


def _delta_present_entities(delta: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [item for item in delta.get("added") or [] if isinstance(item, dict)]
    for item in delta.get("changed") or []:
        if not isinstance(item, dict):
            continue
        after = item.get("after")
        if isinstance(after, dict):
            rows.append(after)
    return rows


def _cluster_features(entities: list[dict[str, Any]]) -> dict[str, Any]:
    positions = [_rounded_position(item.get("position")) for item in entities]
    positions = [item for item in positions if item is not None]
    if not positions:
        return {"entity_count": 0}
    xs = [float(item["x"]) for item in positions]
    ys = [float(item["y"]) for item in positions]
    width = round(max(xs) - min(xs), 2)
    height = round(max(ys) - min(ys), 2)
    if width >= max(2.0, height * 2):
        axis = "horizontal"
    elif height >= max(2.0, width * 2):
        axis = "vertical"
    else:
        axis = "compact"
    return {
        "entity_count": len(positions),
        "bounds": {
            "min_x": round(min(xs), 2),
            "max_x": round(max(xs), 2),
            "min_y": round(min(ys), 2),
            "max_y": round(max(ys), 2),
            "width": width,
            "height": height,
        },
        "dominant_axis": axis,
        "x_pitch": _common_pitch(xs),
        "y_pitch": _common_pitch(ys),
    }


def _direct_transfer_features(
    after_entities: list[dict[str, Any]],
    delta_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not delta_entities:
        return []
    inserters = [
        item
        for item in after_entities
        if str(item.get("name") or "") in INSERTER_NAMES and _near_any_delta(item, delta_entities, radius=6.0)
    ]
    endpoints = [
        item
        for item in after_entities
        if str(item.get("name") or "") in DIRECT_TRANSFER_ENTITY_NAMES
    ]
    rows: list[dict[str, Any]] = []
    for inserter in inserters:
        ipos = _rounded_position(inserter.get("position"))
        if ipos is None:
            continue
        nearby = sorted(
            (
                (distance(ipos, _rounded_position(entity.get("position"))), entity)
                for entity in endpoints
                if _rounded_position(entity.get("position")) is not None
                and distance(ipos, _rounded_position(entity.get("position"))) <= 3.5
            ),
            key=lambda item: item[0],
        )
        if len(nearby) < 2:
            continue
        pair = [nearby[0][1], nearby[1][1]]
        recipes = sorted(
            str(entity.get("recipe") or "")
            for entity in pair
            if str(entity.get("recipe") or "")
        )
        rows.append(
            {
                "pattern": "direct_inserter_transfer",
                "inserter": {
                    "name": inserter.get("name"),
                    "position": ipos,
                    "direction": inserter.get("direction"),
                },
                "endpoints": [
                    {
                        "name": entity.get("name"),
                        "recipe": entity.get("recipe"),
                        "position": _rounded_position(entity.get("position")),
                    }
                    for entity in pair
                ],
                "recipes": recipes,
            }
        )
    return rows[:5]


def _skill_candidate(layout_features: dict[str, Any], active_skill: str) -> dict[str, Any]:
    targets: list[str] = []
    related_skills = {active_skill}
    direct_transfers = layout_features.get("direct_transfers") if isinstance(layout_features.get("direct_transfers"), list) else []
    if direct_transfers:
        targets.append("direct assembler-to-assembler inserter transfer")
    recipes = {
        recipe
        for row in direct_transfers
        if isinstance(row, dict)
        for recipe in row.get("recipes", [])
        if isinstance(recipe, str)
    }
    if {"iron-gear-wheel", "transport-belt"}.issubset(recipes):
        related_skills.add("build_gear_belt_mall_logistics")
        targets.append("gear-to-belt direct insertion")
    counts = layout_features.get("delta_entity_counts") if isinstance(layout_features.get("delta_entity_counts"), dict) else {}
    cluster = layout_features.get("cluster") if isinstance(layout_features.get("cluster"), dict) else {}
    if counts.get("transport-belt") and cluster.get("dominant_axis") in {"horizontal", "vertical"}:
        targets.append("linear belt-aligned site routing")
    if counts.get("stone-furnace") or counts.get("electric-mining-drill") or counts.get("burner-mining-drill"):
        targets.append("parallel mining and smelting bootstrap")
        related_skills.add("expand_iron_smelting")
    return {
        "status": "pending_human_review",
        "name": f"operator_layout_pattern_for_{active_skill}",
        "targets": sorted(set(targets)),
        "related_skills": sorted(skill for skill in related_skills if skill),
        "promotion_required": "confirm improved throughput, fewer blocked prerequisites, or lower walking/build churn before skill promotion",
    }


def _near_any_delta(entity: dict[str, Any], delta_entities: list[dict[str, Any]], *, radius: float) -> bool:
    pos = _rounded_position(entity.get("position"))
    if pos is None:
        return False
    for changed in delta_entities:
        changed_pos = _rounded_position(changed.get("position"))
        if changed_pos is not None and distance(pos, changed_pos) <= radius:
            return True
    return False


def _common_pitch(values: list[float]) -> float | None:
    rounded = sorted(set(round(value, 2) for value in values))
    if len(rounded) < 2:
        return None
    diffs = [
        round(right - left, 2)
        for left, right in zip(rounded, rounded[1:])
        if 0.0 < round(right - left, 2) <= 12.0
    ]
    if not diffs:
        return None
    return Counter(diffs).most_common(1)[0][0]


def _layout_mutating_action(action: dict[str, Any]) -> bool:
    return str(action.get("type") or "") in {"build", "mine", "set_recipe", "rotate", "connect_entities"}


def _compact_action(action: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "type",
        "name",
        "recipe",
        "unit_number",
        "position",
        "direction",
        "tiles",
        "required_resource",
        "reason",
        "bootstrap_seed",
        "seed_reason",
        "expected_followup",
        "allow_nearby",
    }
    return {key: action.get(key) for key in keep if key in action}


def _entity_matches_action(entity: dict[str, Any], action: dict[str, Any]) -> bool:
    unit_number = action.get("unit_number")
    if unit_number is not None and entity.get("unit_number") == unit_number:
        return True
    name = action.get("name")
    if name is not None and entity.get("name") != name:
        return False
    return _positions_close(entity.get("position"), action.get("position"), radius=2.0)


def _positions_close(left: Any, right: Any, *, radius: float) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        return distance(left, right) <= radius
    except (TypeError, ValueError):
        return False


def _rounded_position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {"x": round(float(value.get("x") or 0.0), 2), "y": round(float(value.get("y") or 0.0), 2)}
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append_event(log_dir: Path, event: dict[str, Any]) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    path = human_layout_learning_trace_path(log_dir)
    with path.open("a", encoding="utf-8") as file:
        json.dump(event, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")


def _read_state(runtime_dir: Path) -> dict[str, Any]:
    path = human_layout_learning_state_path(runtime_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(runtime_dir: Path, state: dict[str, Any]) -> None:
    Path(runtime_dir).mkdir(parents=True, exist_ok=True)
    human_layout_learning_state_path(runtime_dir).write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
