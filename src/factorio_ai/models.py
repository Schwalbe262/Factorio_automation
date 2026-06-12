from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_ACTION_TYPES = {"move_to", "mine", "craft", "build", "insert", "take", "wait"}


class ActionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PlannerDecision:
    action: dict[str, Any] | None
    reason: str
    done: bool = False


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ActionValidationError("action must be a JSON object")
    action_type = action.get("type")
    if action_type not in ALLOWED_ACTION_TYPES:
        raise ActionValidationError(f"unsupported action type: {action_type}")

    if action_type == "move_to":
        _require_position(action, "position")
    elif action_type == "mine":
        if "position" not in action and "near" not in action and "unit_number" not in action:
            raise ActionValidationError("mine requires position, near, or unit_number")
    elif action_type == "craft":
        _require_string(action, "recipe")
        _require_positive_count(action)
    elif action_type == "build":
        _require_string(action, "name")
        _require_position(action, "position")
    elif action_type in {"insert", "take"}:
        _require_string(action, "item")
        if "position" not in action and "unit_number" not in action:
            raise ActionValidationError(f"{action_type} requires position or unit_number")
        _require_positive_count(action)
    elif action_type == "wait":
        ticks = int(action.get("ticks", 60))
        if ticks < 1 or ticks > 36000:
            raise ActionValidationError("wait ticks must be between 1 and 36000")

    return action


def _require_string(action: dict[str, Any], key: str) -> None:
    if not isinstance(action.get(key), str) or not action[key]:
        raise ActionValidationError(f"{key} must be a non-empty string")


def _require_positive_count(action: dict[str, Any]) -> None:
    count = int(action.get("count", 1))
    if count < 1:
        raise ActionValidationError("count must be positive")


def _require_position(action: dict[str, Any], key: str) -> None:
    value = action.get(key)
    if not isinstance(value, dict):
        raise ActionValidationError(f"{key} must be an object with x/y")
    if not isinstance(value.get("x"), (int, float)) or not isinstance(value.get("y"), (int, float)):
        raise ActionValidationError(f"{key}.x and {key}.y must be numeric")


def inventory_count(observation: dict[str, Any], item: str) -> int:
    inventory = observation.get("inventory")
    if not isinstance(inventory, dict):
        return 0
    try:
        return int(inventory.get(item) or 0)
    except (TypeError, ValueError):
        return 0


def nested_item_count(value: Any, item: str) -> int:
    if isinstance(value, dict):
        total = 0
        for key, child in value.items():
            if key == item and isinstance(child, (int, float)):
                total += int(child)
            else:
                total += nested_item_count(child, item)
        return total
    if isinstance(value, list):
        return sum(nested_item_count(child, item) for child in value)
    return 0


def total_item_count(observation: dict[str, Any], item: str) -> int:
    return inventory_count(observation, item) + nested_item_count(observation.get("entities", []), item)


def craftable_count(observation: dict[str, Any], recipe: str) -> int:
    craftable = observation.get("craftable")
    if not isinstance(craftable, dict):
        return 0
    try:
        return int(craftable.get(recipe) or 0)
    except (TypeError, ValueError):
        return 0


def player_position(observation: dict[str, Any]) -> dict[str, float]:
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    position = player.get("position") if isinstance(player.get("position"), dict) else {}
    return {
        "x": float(position.get("x") or 0.0),
        "y": float(position.get("y") or 0.0),
    }


def distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return ((float(a["x"]) - float(b["x"])) ** 2 + (float(a["y"]) - float(b["y"])) ** 2) ** 0.5


def nearest_resource(observation: dict[str, Any], name: str) -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [item for item in resources if isinstance(item, dict) and item.get("name") == name]
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item.get("distance") or 999999))


def nearest_entity(observation: dict[str, Any], name: str) -> dict[str, Any] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    candidates = [item for item in entities if isinstance(item, dict) and item.get("name") == name]
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item.get("distance") or 999999))


def entity_item_count(entity: dict[str, Any], item: str) -> int:
    return nested_item_count(entity.get("inventories", {}), item)
