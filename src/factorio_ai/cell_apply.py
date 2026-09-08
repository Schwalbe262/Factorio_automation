"""Apply a stored cell-library design to the live game.

:mod:`cell_library` catalogues validated cell blueprints and the dashboard shows them, but nothing
ever BUILT them in the game -- the library was a dead catalogue. This module turns a chosen design
into the concrete sequence of primitive actions the autopilot already uses (``build`` + ``set_recipe``)
so the design can actually be CONSTRUCTED on the live surface at a chosen anchor.

``design_build_plan`` is pure (decode blueprint -> required item counts + offset build/recipe actions);
``apply_design`` is the opt-in executor that runs the plan through a controller's ``act`` (each action
self-validates -- missing item / collision / out-of-reach are returned, not raised -- so a partial or
mis-sited apply degrades gracefully rather than corrupting the game).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import blueprints, cell_library


def _design_entities(design: dict[str, Any] | None) -> list[dict[str, Any]]:
    if isinstance(design, dict) and isinstance(design.get("entities"), list):
        return [e for e in design["entities"] if isinstance(e, dict)]
    bp = (design or {}).get("blueprint") if isinstance(design, dict) else None
    exchange = bp.get("exchange_string") if isinstance(bp, dict) else None
    if not isinstance(exchange, str) or not exchange:
        return []
    decoded = blueprints.decode_blueprint_string(exchange)
    if not isinstance(decoded, dict):
        return []
    inner = decoded.get("blueprint") if isinstance(decoded.get("blueprint"), dict) else decoded
    entities = inner.get("entities") if isinstance(inner, dict) else None
    return [e for e in (entities or []) if isinstance(e, dict)]


def design_build_plan(design: dict[str, Any], anchor_x: float = 0.0, anchor_y: float = 0.0) -> dict[str, Any]:
    """Translate a library design into an ordered plan to BUILD it at ``(anchor_x, anchor_y)``:
    ``required_items`` (item -> count to have in inventory) plus ``actions`` = every entity as a
    ``build`` (offset to the anchor, preserving direction) followed by ``set_recipe`` for each recipe
    machine (furnaces auto-smelt, so they get no recipe). Pure; the executor consumes ``actions``."""
    entities = _design_entities(design)
    required: dict[str, int] = {}
    build_actions: list[dict[str, Any]] = []
    recipe_actions: list[dict[str, Any]] = []
    for entity in entities:
        name = str(entity.get("name") or "")
        if not name:
            continue
        pos = entity.get("position") if isinstance(entity.get("position"), dict) else {}
        x = round(anchor_x + float(pos.get("x", 0.0)), 3)
        y = round(anchor_y + float(pos.get("y", 0.0)), 3)
        required[name] = required.get(name, 0) + 1
        action: dict[str, Any] = {"type": "build", "name": name, "position": {"x": x, "y": y}}
        if entity.get("direction") is not None:
            action["direction"] = entity["direction"]
        build_actions.append(action)
        recipe = entity.get("recipe")
        if recipe and "furnace" not in name:  # furnaces auto-smelt the belt's ore; no recipe to set
            recipe_actions.append(
                {"type": "set_recipe", "name": name, "position": {"x": x, "y": y}, "recipe": str(recipe)}
            )
    return {
        "required_items": dict(sorted(required.items())),
        "actions": build_actions + recipe_actions,
        "anchor": {"x": anchor_x, "y": anchor_y},
        "entity_count": len(entities),
        "build_count": len(build_actions),
        "recipe_count": len(recipe_actions),
    }


def load_design_plan(runtime_dir: Path, key: str, anchor_x: float = 0.0, anchor_y: float = 0.0) -> dict[str, Any]:
    """Load a design by key and return its build plan (``ok=False`` with a reason if unknown)."""
    design = cell_library.get_design(Path(runtime_dir), key)
    if design is None:
        return {"ok": False, "reason": f"no library design with key '{key}'", "key": key}
    plan = design_build_plan(design, anchor_x, anchor_y)
    plan.update({"ok": plan["entity_count"] > 0, "key": key, "item": design.get("item"),
                 "required_machines": design.get("required_machines") or []})
    if not plan["ok"]:
        plan["reason"] = "design contains no buildable entities"
    return plan


def apply_design(
    controller: Any,
    runtime_dir: Path,
    key: str,
    anchor_x: float,
    anchor_y: float,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Apply (build) a library design at an anchor. Default ``execute=False`` returns the plan only
    (dry-run). Execution stops at the first failed action so recipe changes never follow
    missing construction. Reports the failed action index for a supervisor to reobserve
    and repair; the returned index alone is not evidence that earlier actions still exist."""
    plan = load_design_plan(runtime_dir, key, anchor_x, anchor_y)
    if not plan.get("ok"):
        return plan
    plan["executed"] = bool(execute)
    if not execute:
        return plan
    results: list[dict[str, Any]] = []
    placed = failed = applied = 0
    for index, action in enumerate(plan["actions"]):
        try:
            res = controller.act(action)
        except Exception as exc:  # noqa: BLE001 - report transport failures as failed actions
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(isinstance(res, dict) and res.get("ok", False))
        placed += 1 if ok and action.get("type") == "build" else 0
        applied += 1 if ok else 0
        failed += 0 if ok else 1
        detail = ((res.get("reason") or res.get("error") or res.get("status") or
                   ("ok" if ok else "action returned failure without a reason"))
                  if isinstance(res, dict) else "invalid action response")
        results.append({"action": action.get("type"), "name": action.get("name"),
                        "position": action.get("position"), "ok": ok,
                        "detail": detail, "index": index})
        if not ok:
            plan.update(reason=detail, failed_action_index=index)
            break
    plan.update({"ok": failed == 0, "results": results, "placed": placed, "failed": failed,
                 "applied": applied, "remaining": len(plan["actions"]) - applied})
    return plan
