from __future__ import annotations

from typing import Any

from .models import (
    PlannerDecision,
    craftable_count,
    distance,
    entity_item_count,
    inventory_count,
    nearest_entity,
    nearest_resource,
    player_position,
    total_item_count,
)


EAST = 2


class IronPlateSkill:
    """Rule-based early-game skill that bootstraps iron plate production."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        iron_total = total_item_count(observation, "iron-plate")
        if iron_total >= self.target_count:
            return PlannerDecision(None, f"iron plate target reached: {iron_total}/{self.target_count}", done=True)

        furnace = nearest_entity(observation, "stone-furnace")
        drill = nearest_entity(observation, "burner-mining-drill")
        player = player_position(observation)

        if furnace and entity_item_count(furnace, "iron-plate") > 0:
            furnace_pos = _position(furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near furnace to take produced iron plates",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "iron-plate",
                    "count": min(50, entity_item_count(furnace, "iron-plate")),
                    "unit_number": furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": _position(furnace),
                },
                "take produced iron plates from furnace",
            )

        if inventory_count(observation, "coal") < 6:
            coal = nearest_resource(observation, "coal")
            if coal is None:
                return PlannerDecision(None, "cannot find nearby coal")
            return self._mine_resource(player, coal, "coal", 8)

        if furnace is None and inventory_count(observation, "stone-furnace") <= 0:
            if craftable_count(observation, "stone-furnace") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "stone-furnace", "count": 1},
                    "craft stone furnace",
                )
            stone = nearest_resource(observation, "stone")
            if stone is not None:
                return self._mine_resource(player, stone, "stone", 8)

        if drill is None and inventory_count(observation, "burner-mining-drill") <= 0:
            if craftable_count(observation, "burner-mining-drill") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                    "craft burner mining drill",
                )
            return PlannerDecision(None, "missing burner mining drill and cannot craft it from current inventory")

        if drill is None:
            iron = nearest_resource(observation, "iron-ore")
            if iron is None:
                return PlannerDecision(None, "cannot find nearby iron ore")
            iron_pos = _position(iron)
            if distance(player, iron_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": iron_pos},
                    "move near iron ore before placing burner mining drill",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "burner-mining-drill",
                    "position": iron_pos,
                    "direction": EAST,
                },
                "place burner mining drill on iron ore",
            )

        if furnace is None:
            drill_pos = _position(drill)
            furnace_pos = {"x": drill_pos["x"] + 3, "y": drill_pos["y"]}
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near drill before placing furnace",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "stone-furnace",
                    "position": furnace_pos,
                    "allow_nearby": True,
                },
                "place furnace at drill output",
            )

        if furnace and entity_item_count(furnace, "iron-ore") < 5 and inventory_count(observation, "iron-ore") <= 0:
            iron = nearest_resource(observation, "iron-ore")
            if iron is None:
                return PlannerDecision(None, "cannot find nearby iron ore for furnace input")
            return self._mine_resource(player, iron, "iron-ore", 10)

        if furnace and inventory_count(observation, "iron-ore") > 0 and entity_item_count(furnace, "iron-ore") < 5:
            furnace_pos = _position(furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near furnace to insert iron ore",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "iron-ore",
                    "count": min(10, inventory_count(observation, "iron-ore")),
                    "unit_number": furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": _position(furnace),
                },
                "insert iron ore into furnace",
            )

        if drill and inventory_count(observation, "coal") > 0 and entity_item_count(drill, "coal") < 3:
            drill_pos = _position(drill)
            if distance(player, drill_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": drill_pos},
                    "move near drill to insert coal",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "coal",
                    "count": min(5, inventory_count(observation, "coal")),
                    "unit_number": drill.get("unit_number"),
                    "name": "burner-mining-drill",
                    "position": _position(drill),
                },
                "fuel burner mining drill",
            )

        if furnace and inventory_count(observation, "coal") > 0 and entity_item_count(furnace, "coal") < 3:
            furnace_pos = _position(furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near furnace to insert coal",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "coal",
                    "count": min(5, inventory_count(observation, "coal")),
                    "unit_number": furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": _position(furnace),
                },
                "fuel stone furnace",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for miner/furnace production",
        )

    def _mine_resource(
        self,
        player: dict[str, float],
        resource: dict[str, Any],
        name: str,
        count: int,
    ) -> PlannerDecision:
        pos = _position(resource)
        if distance(player, pos) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": pos},
                f"move near {name}",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "target": "resource",
                "name": name,
                "near": pos,
                "count": count,
            },
            f"mine {name}",
        )


def _position(entity: dict[str, Any]) -> dict[str, float]:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    return {
        "x": float(position.get("x") or 0.0),
        "y": float(position.get("y") or 0.0),
    }
