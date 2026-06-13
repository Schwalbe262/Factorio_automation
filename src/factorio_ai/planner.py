from __future__ import annotations

from typing import Any

from .knowledge import RECIPES
from .models import (
    PlannerDecision,
    craftable_count,
    distance,
    entities_named,
    entity_fluid_count,
    entity_item_count,
    inventory_count,
    nearest_entity,
    nearest_resource,
    player_position,
    total_item_count,
)


NORTH = 0
EAST = 4
SOUTH = 8
WEST = 12
FURNACE_RESOURCE_RADIUS = 12.0
WALK_FUEL_LOGISTICS_LIMIT = 160.0


class IronPlateSkill:
    """Rule-based early-game skill that bootstraps iron plate production."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count

    def next_action(
        self,
        observation: dict[str, Any],
        target_count: int | None = None,
        inventory_only: bool = False,
    ) -> PlannerDecision:
        target = target_count or self.target_count
        iron_total = inventory_count(observation, "iron-plate") if inventory_only else total_item_count(observation, "iron-plate")
        if iron_total >= target:
            return PlannerDecision(None, f"iron plate target reached: {iron_total}/{target}", done=True)

        furnace = _select_iron_furnace(observation)
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
            stand_pos = _stand_position(iron_pos)
            if distance(player, stand_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": stand_pos},
                    "move near iron ore before placing burner mining drill",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "burner-mining-drill",
                    "position": iron_pos,
                    "direction": EAST,
                    "allow_nearby": True,
                    "required_resource": "iron-ore",
                },
                "place burner mining drill on iron ore",
            )

        if furnace is None:
            drill_pos = _position(drill)
            furnace_pos = {"x": drill_pos["x"] + 3, "y": drill_pos["y"]}
            stand_pos = _stand_position(furnace_pos)
            if distance(player, stand_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": stand_pos},
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
                "radius": 8,
                "count": count,
            },
            f"mine {name}",
        )


class AutomationScienceSkill:
    """Second milestone: produce automation science packs after iron smelting works."""

    def __init__(self, target_count: int = 5, iron_plate_floor: int = 10) -> None:
        self.target_count = target_count
        self.iron_plate_floor = iron_plate_floor
        self.iron_skill = IronPlateSkill(iron_plate_floor)
        self.copper_skill = CopperPlateSkill(target_count)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        science_total = total_item_count(observation, "automation-science-pack")
        if science_total >= self.target_count:
            return PlannerDecision(
                None,
                f"automation science target reached: {science_total}/{self.target_count}",
                done=True,
            )

        if total_item_count(observation, "iron-plate") < self.iron_plate_floor:
            decision = self.iron_skill.next_action(observation)
            if decision.action is not None:
                return decision

        copper_plate_inventory = inventory_count(observation, "copper-plate")
        gear_total = inventory_count(observation, "iron-gear-wheel")
        science_needed = self.target_count - science_total

        if craftable_count(observation, "automation-science-pack") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "automation-science-pack",
                    "count": min(science_needed, craftable_count(observation, "automation-science-pack")),
                },
                "craft automation science packs",
            )

        if gear_total < science_needed and craftable_count(observation, "iron-gear-wheel") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": min(science_needed - gear_total, craftable_count(observation, "iron-gear-wheel")),
                },
                "craft iron gear wheels for automation science",
            )

        if copper_plate_inventory < science_needed:
            decision = self.copper_skill.next_action(observation, target_count=science_needed, inventory_only=True)
            if not decision.done:
                return decision

        if gear_total < science_needed:
            return PlannerDecision(None, "missing iron gear wheels and cannot craft them")

        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            "wait before rechecking automation science prerequisites",
        )


class CopperPlateSkill:
    """Reusable early-game skill that produces copper plates with hand mining and a stone furnace."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count
        self.support_skill = IronPlateSkill(target_count=10)

    def next_action(
        self,
        observation: dict[str, Any],
        target_count: int | None = None,
        inventory_only: bool = False,
    ) -> PlannerDecision:
        target = target_count or self.target_count
        copper_total = inventory_count(observation, "copper-plate") if inventory_only else total_item_count(observation, "copper-plate")
        if copper_total >= target:
            return PlannerDecision(None, f"copper plate target reached: {copper_total}/{target}", done=True)

        player = player_position(observation)
        copper_furnace = _select_copper_furnace(observation)
        copper = nearest_resource(observation, "copper-ore")
        if copper is None:
            return PlannerDecision(None, "cannot find nearby copper ore")

        if inventory_count(observation, "coal") < 6:
            coal = nearest_resource(observation, "coal")
            if coal is None:
                return PlannerDecision(None, "cannot find nearby coal for copper smelting")
            return self.support_skill._mine_resource(player, coal, "coal", 8)

        if copper_furnace is None:
            furnaces = entities_named(observation, "stone-furnace")
            free_furnaces = [item for item in furnaces if not _is_iron_busy_furnace(item)]
            if free_furnaces:
                copper_furnace = _nearest_to(free_furnaces, _position(copper))
            else:
                if inventory_count(observation, "stone-furnace") <= 0:
                    if craftable_count(observation, "stone-furnace") > 0:
                        return PlannerDecision(
                            {"type": "craft", "recipe": "stone-furnace", "count": 1},
                            "craft stone furnace for copper smelting",
                        )
                    stone = nearest_resource(observation, "stone")
                    if stone is None:
                        return PlannerDecision(None, "cannot find stone for copper furnace")
                    return self.support_skill._mine_resource(player, stone, "stone", 8)
                copper_pos = _position(copper)
                furnace_pos = {"x": copper_pos["x"] + 3, "y": copper_pos["y"]}
                if distance(player, furnace_pos) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": furnace_pos},
                        "move near copper patch before placing copper furnace",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "stone-furnace",
                        "position": furnace_pos,
                        "allow_nearby": True,
                    },
                    "place furnace for copper smelting",
                )

        if copper_furnace and entity_item_count(copper_furnace, "copper-plate") > 0:
            furnace_pos = _position(copper_furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near copper furnace to take copper plates",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "copper-plate",
                    "count": min(50, entity_item_count(copper_furnace, "copper-plate")),
                    "unit_number": copper_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                "take produced copper plates from furnace",
            )

        if inventory_count(observation, "copper-ore") <= 0:
            return self.support_skill._mine_resource(player, copper, "copper-ore", max(8, target - copper_total))

        furnace_pos = _position(copper_furnace)
        if entity_item_count(copper_furnace, "copper-ore") < target:
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near copper furnace to insert copper ore",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "copper-ore",
                    "count": min(max(8, target - copper_total), inventory_count(observation, "copper-ore")),
                    "unit_number": copper_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                "insert copper ore into copper furnace",
            )

        if entity_item_count(copper_furnace, "coal") < 3:
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near copper furnace to insert coal",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "coal",
                    "count": min(5, inventory_count(observation, "coal")),
                    "unit_number": copper_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                "fuel copper furnace",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for copper plates",
        )


class ElectronicCircuitSkill:
    """Craft early electronic circuits by ensuring iron plates, copper plates, and copper cable."""

    def __init__(self, target_count: int = 5) -> None:
        self.target_count = target_count
        self.iron_skill = IronPlateSkill(max(10, target_count))
        self.copper_skill = CopperPlateSkill(max(10, _ceil_div(target_count * 3, 2)))

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        circuit_total = total_item_count(observation, "electronic-circuit")
        if circuit_total >= self.target_count:
            return PlannerDecision(
                None,
                f"electronic circuit target reached: {circuit_total}/{self.target_count}",
                done=True,
            )

        missing_circuits = self.target_count - circuit_total
        craftable_circuits = craftable_count(observation, "electronic-circuit")
        if craftable_circuits > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "electronic-circuit",
                    "count": min(missing_circuits, craftable_circuits),
                },
                "craft electronic circuits",
            )

        required_cables = missing_circuits * 3
        cable_inventory = inventory_count(observation, "copper-cable")
        if cable_inventory < required_cables:
            craftable_cable = craftable_count(observation, "copper-cable")
            if craftable_cable > 0:
                cable_crafts_needed = _ceil_div(required_cables - cable_inventory, 2)
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "copper-cable",
                        "count": min(cable_crafts_needed, craftable_cable),
                    },
                    "craft copper cable for electronic circuits",
                )

            copper_plates_needed = _ceil_div(required_cables - cable_inventory, 2)
            if inventory_count(observation, "copper-plate") < copper_plates_needed:
                decision = self.copper_skill.next_action(
                    observation,
                    target_count=copper_plates_needed,
                    inventory_only=True,
                )
                if not decision.done:
                    return decision

        iron_plates_needed = missing_circuits
        if inventory_count(observation, "iron-plate") < iron_plates_needed:
            decision = self.iron_skill.next_action(
                observation,
                target_count=iron_plates_needed,
                inventory_only=True,
            )
            if not decision.done:
                return decision

        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            "wait before rechecking electronic circuit prerequisites",
        )


class BeltSmeltingLineSkill:
    """Build a minimal belt-fed burner smelting line for early plate automation."""

    def __init__(
        self,
        target_count: int = 10,
        resource_name: str = "iron-ore",
        product_name: str = "iron-plate",
    ) -> None:
        self.target_count = target_count
        self.resource_name = resource_name
        self.product_name = product_name
        self.support_skill = IronPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        line = _find_belt_smelting_line(observation, self.resource_name)
        line_furnace = line.get("furnace") if line else None
        total_product = total_item_count(observation, self.product_name)
        if line_furnace and self._line_has_started(line_furnace) and total_product >= self.target_count:
            return PlannerDecision(
                None,
                f"belt smelting line produced {self.product_name}: {total_product}/{self.target_count}",
                done=True,
            )

        player = player_position(observation)
        layout = line or _select_belt_smelting_layout(observation, self.resource_name)
        if layout is None:
            return PlannerDecision(None, f"cannot find open {self.resource_name} site for belt smelting line")

        need = _line_missing_item(observation, layout)
        if need:
            decision = self._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt1_position", "belt_direction"),
            ("transport-belt", "belt2_position", "belt_direction"),
            ("burner-inserter", "inserter_position", "inserter_direction"),
            ("stone-furnace", "furnace_position", None),
            ("burner-mining-drill", "drill_position", "drill_direction"),
        ]:
            entity_key = _entity_key_for_layout(name, key)
            if layout.get(entity_key) is not None:
                continue
            position = layout[key]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position)},
                    f"move near planned {name} position",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": name in {"burner-mining-drill", "stone-furnace"},
            }
            if name == "burner-mining-drill":
                action["required_resource"] = self.resource_name
            direction = layout.get(direction_key) if direction_key else None
            if direction is not None:
                action["direction"] = direction
            return PlannerDecision(action, f"place {name} for belt smelting line")

        for entity_name, item, threshold, count in [
            ("burner-mining-drill", "coal", 3, 5),
            ("burner-inserter", "coal", 2, 3),
            ("stone-furnace", "coal", 3, 5),
        ]:
            entity = layout.get(_entity_key(entity_name))
            if entity and entity_item_count(entity, item) < threshold:
                position = _position(entity)
                if inventory_count(observation, item) <= 0:
                    coal = _nearest_resource_to_position(observation, position, "coal")
                    if coal is None:
                        return PlannerDecision(None, "cannot find coal for burner smelting line")
                    if distance(position, _position(coal)) > WALK_FUEL_LOGISTICS_LIMIT:
                        return PlannerDecision(None, "burner smelting line needs fuel logistics before more walking refuels")
                    return self.support_skill._mine_resource(player, coal, "coal", 16)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        f"move near {entity_name} to fuel belt smelting line",
                    )
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": item,
                        "count": min(count, inventory_count(observation, item)),
                        "unit_number": entity.get("unit_number"),
                        "name": entity_name,
                        "position": position,
                    },
                    f"fuel {entity_name} in belt smelting line",
                )

        layout_furnace = layout.get("furnace")
        if layout_furnace and entity_item_count(layout_furnace, self.product_name) > 0:
            furnace_pos = _position(layout_furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near automated furnace output",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": self.product_name,
                    "count": min(50, entity_item_count(layout_furnace, self.product_name)),
                    "unit_number": layout_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                f"take {self.product_name} produced by belt smelting line",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for belt smelting line to move ore and smelt plates",
        )

    def _ensure_item(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
    ) -> PlannerDecision | None:
        if item == "stone-furnace":
            if craftable_count(observation, "stone-furnace") > 0:
                return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft furnace for line")
            stone = nearest_resource(observation, "stone")
            if stone is None:
                return PlannerDecision(None, "cannot find stone for line furnace")
            return self.support_skill._mine_resource(player, stone, "stone", 8)

        if item == "burner-mining-drill":
            if craftable_count(observation, "burner-mining-drill") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                    "craft burner mining drill for line",
                )
            if inventory_count(observation, "stone") < 5:
                stone = nearest_resource(observation, "stone")
                if stone is None:
                    return PlannerDecision(None, "cannot find stone for line drill")
                return self.support_skill._mine_resource(player, stone, "stone", 8)
            if inventory_count(observation, "iron-gear-wheel") < 3 and craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(3 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for line drill",
                )
            return self.support_skill.next_action(observation, target_count=20, inventory_only=True)

        if item in {"transport-belt", "burner-inserter"}:
            if craftable_count(observation, item) > 0:
                return PlannerDecision({"type": "craft", "recipe": item, "count": 1}, f"craft {item} for line")
            if inventory_count(observation, "iron-gear-wheel") < 1 and craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
                    f"craft gear for {item}",
                )
            return self.support_skill.next_action(observation, target_count=20, inventory_only=True)

        return None

    def _line_has_started(self, furnace: dict[str, Any]) -> bool:
        return entity_item_count(furnace, self.resource_name) > 0 or entity_item_count(furnace, self.product_name) > 0


class _ExpandPlateSmeltingSkill:
    """Incrementally add belt-fed plate smelting capacity."""

    def __init__(self, resource_name: str, product_name: str, target_rate_per_minute: float) -> None:
        self.resource_name = resource_name
        self.product_name = product_name
        self.target_rate_per_minute = target_rate_per_minute
        self.line_skill = BeltSmeltingLineSkill(
            target_count=20,
            resource_name=resource_name,
            product_name=product_name,
        )

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        estimated_rate = _estimated_plate_rate(observation, self.product_name, self.resource_name)
        if estimated_rate >= self.target_rate_per_minute:
            return PlannerDecision(
                None,
                f"{self.product_name} smelting capacity target reached: {estimated_rate}/{self.target_rate_per_minute}/min",
                done=True,
        )

        player = player_position(observation)
        layout = (
            _find_unfueled_belt_smelting_line(observation, self.resource_name)
            or _find_incomplete_belt_smelting_line(observation, self.resource_name)
            or _select_belt_smelting_layout(
                observation,
                self.resource_name,
            )
        )
        if layout is None:
            return PlannerDecision(None, f"cannot find open {self.resource_name} site for another smelting line")

        need = _line_missing_item(observation, layout)
        if need:
            decision = self.line_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt1_position", "belt_direction"),
            ("transport-belt", "belt2_position", "belt_direction"),
            ("burner-inserter", "inserter_position", "inserter_direction"),
            ("stone-furnace", "furnace_position", None),
            ("burner-mining-drill", "drill_position", "drill_direction"),
        ]:
            entity_key = _entity_key_for_layout(name, key)
            if layout.get(entity_key) is not None:
                continue
            position = layout[key]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing {name}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing {name}",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    f"move near planned {name} position for {self.product_name} smelting expansion",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": name in {"burner-mining-drill", "stone-furnace"},
            }
            if name == "burner-mining-drill":
                action["required_resource"] = self.resource_name
            direction = layout.get(direction_key) if direction_key else None
            if direction is not None:
                action["direction"] = direction
            return PlannerDecision(action, f"place {name} for expanded {self.product_name} smelting")

        for entity_name, item, threshold, count in [
            ("burner-mining-drill", "coal", 3, 5),
            ("burner-inserter", "coal", 2, 3),
            ("stone-furnace", "coal", 3, 5),
        ]:
            entity = layout.get(_entity_key(entity_name))
            if entity and entity_item_count(entity, item) < threshold:
                position = _position(entity)
                if inventory_count(observation, item) <= 0:
                    coal = _nearest_resource_to_position(observation, position, "coal")
                    if coal is None:
                        return PlannerDecision(None, f"cannot find coal for expanded {self.product_name} smelting")
                    if distance(position, _position(coal)) > WALK_FUEL_LOGISTICS_LIMIT:
                        return PlannerDecision(
                            None,
                            f"expanded {self.product_name} smelting needs fuel logistics before more walking refuels",
                        )
                    return self.line_skill.support_skill._mine_resource(player, coal, "coal", 16)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        f"move near {entity_name} to fuel expanded {self.product_name} smelting",
                    )
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": item,
                        "count": min(count, inventory_count(observation, item)),
                        "unit_number": entity.get("unit_number"),
                        "name": entity_name,
                        "position": position,
                    },
                    f"fuel {entity_name} in expanded {self.product_name} smelting",
                )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            f"wait for expanded {self.product_name} smelting line to start",
        )


class ExpandIronSmeltingSkill(_ExpandPlateSmeltingSkill):
    """Incrementally add belt-fed iron smelting capacity."""

    def __init__(self, target_rate_per_minute: float = 90.0) -> None:
        super().__init__("iron-ore", "iron-plate", target_rate_per_minute)


class ExpandCopperSmeltingSkill(_ExpandPlateSmeltingSkill):
    """Incrementally add belt-fed copper smelting capacity."""

    def __init__(self, target_rate_per_minute: float = 75.0) -> None:
        super().__init__("copper-ore", "copper-plate", target_rate_per_minute)


class StarterDefenseSkill:
    """Build a minimal firearm-magazine gun turret defense near the current threat vector."""

    def __init__(self, magazine_target: int = 10) -> None:
        self.magazine_target = magazine_target
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=10)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        enemy = _nearest_observed_enemy(observation)
        if enemy is None:
            return PlannerDecision(None, "no observed enemies in threat radius", done=True)

        player = player_position(observation)
        turret = _nearest_loaded_or_empty_turret(observation)
        if turret and entity_item_count(turret, "firearm-magazine") >= 5:
            return PlannerDecision(
                None,
                f"starter defense turret is armed against nearest enemy at {enemy.get('distance')} tiles",
                done=True,
            )

        if inventory_count(observation, "firearm-magazine") < self.magazine_target:
            if craftable_count(observation, "firearm-magazine") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "firearm-magazine",
                        "count": min(self.magazine_target - inventory_count(observation, "firearm-magazine"), craftable_count(observation, "firearm-magazine")),
                    },
                    "craft firearm magazines for starter defense",
                )
            decision = self.iron_skill.next_action(observation, target_count=40, inventory_only=True)
            if not decision.done:
                return decision

        if turret is None:
            if inventory_count(observation, "gun-turret") <= 0:
                decision = self._ensure_turret_item(observation)
                if decision is not None:
                    return decision
            position = _defense_position(player, _position(enemy))
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=-3.0)},
                    "move near planned starter defense turret position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "gun-turret",
                    "position": position,
                    "allow_nearby": True,
                },
                "place starter defense gun turret between factory and enemies",
            )

        turret_position = _position(turret)
        if distance(player, turret_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": turret_position},
                "move near starter defense turret to insert ammunition",
            )
        return PlannerDecision(
            {
                "type": "insert",
                "item": "firearm-magazine",
                "count": min(inventory_count(observation, "firearm-magazine"), self.magazine_target),
                "unit_number": turret.get("unit_number"),
                "name": "gun-turret",
                "position": turret_position,
            },
            "arm starter defense turret with firearm magazines",
        )

    def _ensure_turret_item(self, observation: dict[str, Any]) -> PlannerDecision | None:
        if craftable_count(observation, "gun-turret") > 0:
            return PlannerDecision({"type": "craft", "recipe": "gun-turret", "count": 1}, "craft gun turret for starter defense")
        if inventory_count(observation, "iron-gear-wheel") < 10 and craftable_count(observation, "iron-gear-wheel") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": min(10 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                },
                "craft gears for starter defense turret",
            )
        if inventory_count(observation, "copper-plate") < 5:
            decision = self.copper_skill.next_action(observation, target_count=5, inventory_only=True)
            if not decision.done:
                return decision
        if inventory_count(observation, "iron-plate") < 30:
            decision = self.iron_skill.next_action(observation, target_count=30, inventory_only=True)
            if not decision.done:
                return decision
        return PlannerDecision(None, "gun turret recipe is not craftable from current state")


class SetupPowerSkill:
    """Build the first steam power block: offshore pump, boiler, engine, and pole."""

    def __init__(self) -> None:
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=10)
        self.circuit_skill = ElectronicCircuitSkill(target_count=2)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        block = _find_steam_power_block(observation)
        if _steam_power_ready(block):
            return PlannerDecision(None, "steam power block is producing usable steam power", done=True)

        player = player_position(observation)
        layout = block or _select_power_layout(observation)
        if layout is None:
            return PlannerDecision(None, "cannot find a buildable west-facing water site for steam power")

        missing = _missing_power_item(observation, layout)
        if missing:
            decision = self._ensure_item_quantity(observation, player, missing, _power_item_required_count(missing))
            if decision is not None:
                return decision

        for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
            if layout.get(key) is not None:
                continue
            spec = layout[f"{key}_spec"]
            position = spec["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _power_stand_position(layout)},
                    f"move near planned {spec['name']} position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": spec["name"],
                    "position": position,
                    "direction": spec.get("direction", NORTH),
                },
                f"place {spec['name']} for first steam power block",
            )

        boiler = layout.get("boiler")
        if boiler and entity_item_count(boiler, "coal") < 3:
            if inventory_count(observation, "coal") < 5:
                coal = nearest_resource(observation, "coal")
                if coal is None:
                    return PlannerDecision(None, "cannot find coal to fuel boiler")
                return self.iron_skill._mine_resource(player, coal, "coal", 10)
            boiler_pos = _position(boiler)
            if distance(player, boiler_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": boiler_pos},
                    "move near boiler to insert coal",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "coal",
                    "count": min(10, inventory_count(observation, "coal")),
                    "unit_number": boiler.get("unit_number"),
                    "name": "boiler",
                    "position": boiler_pos,
                },
                "fuel boiler for first steam power block",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for offshore pump, boiler, and steam engine to fill with steam",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for steam power",
            )

        if item == "pipe":
            return self._ensure_iron_plates(observation, quantity - inventory_count(observation, "pipe"))
        if item == "iron-gear-wheel":
            return self._ensure_iron_plates(observation, 2 * (quantity - inventory_count(observation, "iron-gear-wheel")))
        if item == "copper-cable":
            return self._ensure_copper_plates(observation, _ceil_div(quantity - inventory_count(observation, "copper-cable"), 2))
        if item == "stone-furnace":
            if craftable_count(observation, "stone-furnace") > 0:
                return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft furnace for boiler")
            stone = nearest_resource(observation, "stone")
            if stone is None:
                return PlannerDecision(None, "cannot find stone for boiler furnace prerequisite")
            return self.iron_skill._mine_resource(player, stone, "stone", 8)
        if item == "small-electric-pole":
            if inventory_count(observation, "wood") < 1:
                tree = _nearest_tree(observation)
                if tree is None:
                    return PlannerDecision(None, "cannot find a tree for small electric poles")
                tree_pos = _position(tree)
                if distance(player, tree_pos) > 8:
                    return PlannerDecision({"type": "move_to", "position": tree_pos}, "move near tree for pole wood")
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": tree.get("name"),
                        "position": tree_pos,
                        "count": 1,
                    },
                    "mine tree for pole wood",
                )
            return self._ensure_item_quantity(observation, player, "copper-cable", 2)
        if item == "boiler":
            decision = self._ensure_item_quantity(observation, player, "stone-furnace", 1)
            if decision is not None:
                return decision
            return self._ensure_item_quantity(observation, player, "pipe", 4)
        if item == "steam-engine":
            for prerequisite, count in [("iron-gear-wheel", 8), ("pipe", 5), ("iron-plate", 10)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None
        if item == "offshore-pump":
            for prerequisite, count in [("electronic-circuit", 2), ("pipe", 1), ("iron-gear-wheel", 1)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None
        if item == "electronic-circuit":
            decision = self.circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None
        if item == "iron-plate":
            return self._ensure_iron_plates(observation, quantity)
        if item == "copper-plate":
            return self._ensure_copper_plates(observation, quantity)

        return PlannerDecision(None, f"missing {item} and no prerequisite path is implemented")

    def _ensure_iron_plates(self, observation: dict[str, Any], quantity: int) -> PlannerDecision | None:
        if inventory_count(observation, "iron-plate") >= quantity:
            return None
        decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
        if decision.done:
            return None
        return decision

    def _ensure_copper_plates(self, observation: dict[str, Any], quantity: int) -> PlannerDecision | None:
        if inventory_count(observation, "copper-plate") >= quantity:
            return None
        decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
        if decision.done:
            return None
        return decision


def _position(entity: dict[str, Any]) -> dict[str, float]:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    return {
        "x": float(position.get("x") or 0.0),
        "y": float(position.get("y") or 0.0),
    }


def _stand_position(target: dict[str, float], offset: float = 2.0) -> dict[str, float]:
    return {"x": float(target["x"]) + offset, "y": float(target["y"])}


def _select_copper_furnace(observation: dict[str, Any]) -> dict[str, Any] | None:
    furnaces = entities_named(observation, "stone-furnace")
    for item in furnaces:
        if entity_item_count(item, "copper-plate") > 0 or entity_item_count(item, "copper-ore") > 0:
            return item
    copper = nearest_resource(observation, "copper-ore")
    if copper is None or not furnaces:
        return None
    iron_busy = [item for item in furnaces if _is_iron_busy_furnace(item)]
    if len(furnaces) == 1 and iron_busy:
        return None
    candidates = [item for item in furnaces if item not in iron_busy] or furnaces
    near = _near_position(candidates, _position(copper), FURNACE_RESOURCE_RADIUS)
    return _nearest_to(near, _position(copper)) if near else None


def _select_iron_furnace(observation: dict[str, Any]) -> dict[str, Any] | None:
    furnaces = entities_named(observation, "stone-furnace")
    for item in furnaces:
        if _is_iron_busy_furnace(item):
            return item
    iron = nearest_resource(observation, "iron-ore")
    if iron is None or not furnaces:
        return None
    copper_busy = [item for item in furnaces if _is_copper_busy_furnace(item)]
    if len(furnaces) == 1 and copper_busy:
        return None
    candidates = [item for item in furnaces if item not in copper_busy] or furnaces
    near = _near_position(candidates, _position(iron), FURNACE_RESOURCE_RADIUS)
    return _nearest_to(near, _position(iron)) if near else None


def _is_iron_busy_furnace(entity: dict[str, Any]) -> bool:
    return entity_item_count(entity, "iron-plate") > 0 or entity_item_count(entity, "iron-ore") > 0


def _is_copper_busy_furnace(entity: dict[str, Any]) -> bool:
    return entity_item_count(entity, "copper-plate") > 0 or entity_item_count(entity, "copper-ore") > 0


def _is_busy_furnace_for(entity: dict[str, Any], resource_name: str, product_name: str) -> bool:
    has_material = entity_item_count(entity, product_name) > 0 or entity_item_count(entity, resource_name) > 0
    return has_material and entity_item_count(entity, "coal") > 0


def _near_position(
    entities: list[dict[str, Any]],
    position: dict[str, float],
    radius: float,
) -> list[dict[str, Any]]:
    return [item for item in entities if distance(_position(item), position) <= radius]


def _nearest_to(entities: list[dict[str, Any]], position: dict[str, float]) -> dict[str, Any] | None:
    if not entities:
        return None
    return min(entities, key=lambda item: distance(_position(item), position))


def _nearest_resource_to_position(
    observation: dict[str, Any],
    position: dict[str, float],
    resource_name: str,
) -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [item for item in resources if isinstance(item, dict) and item.get("name") == resource_name]
    return _nearest_to(candidates, position) if candidates else None


def _nearest_observed_enemy(observation: dict[str, Any]) -> dict[str, Any] | None:
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        return None
    candidates = [item for item in enemies if isinstance(item, dict) and isinstance(item.get("position"), dict)]
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item.get("distance") or 999999))


def _nearest_loaded_or_empty_turret(observation: dict[str, Any]) -> dict[str, Any] | None:
    turrets = entities_named(observation, "gun-turret")
    if not turrets:
        return None
    player = player_position(observation)
    return min(turrets, key=lambda item: distance(player, _position(item)))


def _defense_position(player: dict[str, float], enemy: dict[str, float]) -> dict[str, float]:
    dx = enemy["x"] - player["x"]
    dy = enemy["y"] - player["y"]
    length = max((dx * dx + dy * dy) ** 0.5, 0.001)
    return {
        "x": round(player["x"] + 8.0 * dx / length, 2),
        "y": round(player["y"] + 8.0 * dy / length, 2),
    }


def _resource_name_near_position(
    observation: dict[str, Any],
    position: dict[str, float],
    radius: float = 3.0,
) -> str | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [
        item
        for item in resources
        if isinstance(item, dict)
        and isinstance(item.get("position"), dict)
        and distance(_position(item), position) <= radius
    ]
    nearest = _nearest_to(candidates, position)
    return str(nearest.get("name")) if nearest is not None and nearest.get("name") else None


def _layout_matches_resource(layout: dict[str, Any], resource_name: str) -> bool:
    actual = layout.get("resource_name")
    return actual is None or actual == resource_name


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _line_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    missing_belts = sum(1 for key in ("belt1", "belt2") if layout.get(key) is None)
    if missing_belts > inventory_count(observation, "transport-belt"):
        return "transport-belt"
    for item, entity_name in [
        ("burner-inserter", "burner-inserter"),
        ("stone-furnace", "stone-furnace"),
        ("burner-mining-drill", "burner-mining-drill"),
    ]:
        if layout.get(_entity_key(entity_name)) is None and inventory_count(observation, item) <= 0:
            return item
    return None


def _find_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    belts = entities_named(observation, "transport-belt")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for belt in belts:
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            score = sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if layout.get(key) is not None)
            candidates.append((score, layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _find_incomplete_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if _layout_has_unrelated_blocker(observation, layout):
                continue
            score = sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if layout.get(key) is not None)
            if 0 < score < 5:
                candidates.append((score, float(belt.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _find_unfueled_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")) and not _belt_line_fueled(layout):
                candidates.append((float(belt.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _belt_layout_from_anchor(observation: dict[str, Any], belt: dict[str, Any]) -> dict[str, Any]:
    layouts = _belt_layouts_from_anchor(observation, belt)
    return max(layouts, key=lambda item: sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if item.get(key) is not None))


def _belt_layouts_from_anchor(observation: dict[str, Any], belt: dict[str, Any]) -> list[dict[str, Any]]:
    belt_pos = _position(belt)
    output: list[dict[str, Any]] = []
    for orientation in ("east", "west", "south", "north"):
        layout = _layout_from_belt1_position(belt_pos, orientation=orientation)
        if not _entity_direction_matches(belt, layout["belt_direction"]):
            continue
        layout["belt1"] = belt
        layout["belt2"] = _entity_near(observation, "transport-belt", layout["belt2_position"], radius=0.75)
        layout["inserter"] = _entity_near(observation, "burner-inserter", layout["inserter_position"], radius=1.0)
        layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=1.5)
        layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
        layout["resource_name"] = _resource_name_near_position(observation, layout["drill_position"])
        output.append(layout)
    return output


def _entity_direction_matches(entity: dict[str, Any], expected: int) -> bool:
    if "direction" not in entity:
        return True
    try:
        return int(entity.get("direction")) == expected
    except (TypeError, ValueError):
        return True


def _estimated_iron_plate_rate(observation: dict[str, Any]) -> float:
    return _estimated_plate_rate(observation, "iron-plate", "iron-ore")


def _estimated_copper_plate_rate(observation: dict[str, Any]) -> float:
    return _estimated_plate_rate(observation, "copper-plate", "copper-ore")


def _estimated_plate_rate(observation: dict[str, Any], product_name: str, resource_name: str) -> float:
    complete_lines = _complete_belt_smelting_line_count(observation, resource_name)
    return round(complete_lines * 18.75, 3)


def _complete_belt_smelting_line_count(observation: dict[str, Any], resource_name: str = "iron-ore") -> int:
    furnace_positions: set[tuple[float, float]] = set()
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")) and _belt_line_fueled(layout):
                furnace_pos = _position(layout["furnace"])
                furnace_positions.add((round(furnace_pos["x"], 2), round(furnace_pos["y"], 2)))
    return len(furnace_positions)


def _belt_line_fueled(layout: dict[str, Any]) -> bool:
    for key, minimum in [("drill", 1), ("inserter", 1), ("furnace", 1)]:
        entity = layout.get(key)
        if not isinstance(entity, dict) or entity_item_count(entity, "coal") < minimum:
            return False
    return True


def _blocking_obstacle_near(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    blockers = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if entity_type not in {"simple-entity", "tree", "cliff"} and not name.endswith("rock"):
            continue
        entity_position = _position(entity)
        if distance(entity_position, position) <= 4.0:
            blockers.append(entity)
    return _nearest_to(blockers, position) if blockers else None


def _layout_has_unrelated_blocker(observation: dict[str, Any], layout: dict[str, Any]) -> bool:
    layout_units = {
        entity.get("unit_number")
        for entity in [layout.get("belt1"), layout.get("belt2"), layout.get("inserter"), layout.get("furnace"), layout.get("drill")]
        if isinstance(entity, dict)
    }
    footprint = [
        layout["drill_position"],
        layout["belt1_position"],
        layout["belt2_position"],
        layout["inserter_position"],
        layout["furnace_position"],
    ]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("unit_number") in layout_units:
            continue
        name = str(entity.get("name") or "")
        entity_type = str(entity.get("type") or "")
        if name not in {"character", "transport-belt", "burner-inserter", "stone-furnace", "burner-mining-drill"}:
            continue
        threshold = 3.0 if name in {"stone-furnace", "burner-mining-drill"} else 2.0
        entity_pos = _position(entity)
        if any(distance(entity_pos, pos) < threshold for pos in footprint):
            return True
    return False


def _select_belt_smelting_layout(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    candidates = _ranked_patch_drill_resources(observation, resource_name)
    for resource in candidates:
        for orientation in ("east", "west", "south", "north"):
            layout = _layout_from_drill_position(_position(resource), resource_name=resource_name, orientation=orientation)
            if not _layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _ranked_patch_drill_resources(observation: dict[str, Any], resource_name: str) -> list[dict[str, Any]]:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return []
    candidates = [
        item
        for item in resources
        if isinstance(item, dict)
        and item.get("name") == resource_name
        and isinstance(item.get("position"), dict)
    ]
    if not candidates:
        return []

    existing_drills = [
        item
        for item in entities_named(observation, "burner-mining-drill")
        if _resource_name_near_position(observation, _position(item)) == resource_name
    ]

    def rank(resource: dict[str, Any]) -> tuple[float, float]:
        pos = _position(resource)
        return (
            -_patch_drill_candidate_score(observation, resource, existing_drills),
            float(resource.get("distance") or distance(player_position(observation), pos)),
        )

    return sorted(candidates, key=rank)


def _patch_drill_candidate_score(
    observation: dict[str, Any],
    resource: dict[str, Any],
    existing_drills: list[dict[str, Any]],
) -> float:
    position = _position(resource)
    coverage = _resource_tile_coverage(observation, position, str(resource.get("name") or ""))
    if coverage <= 0:
        return -10000.0

    nearest_drill_distance = min((distance(position, _position(drill)) for drill in existing_drills), default=999999.0)
    if nearest_drill_distance < 2.5:
        return -10000.0

    distance_penalty = float(resource.get("distance") or distance(player_position(observation), position)) * 0.05
    alignment_bonus = _existing_patch_line_alignment_bonus(position, existing_drills)
    return coverage * 20.0 + alignment_bonus - distance_penalty


def _resource_tile_coverage(observation: dict[str, Any], center: dict[str, float], resource_name: str) -> int:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return 0
    covered = 0
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("name") != resource_name or not isinstance(resource.get("position"), dict):
            continue
        pos = _position(resource)
        if abs(pos["x"] - center["x"]) <= 1.5 and abs(pos["y"] - center["y"]) <= 1.5:
            covered += 1
    return covered


def _existing_patch_line_alignment_bonus(position: dict[str, float], existing_drills: list[dict[str, Any]]) -> float:
    bonus = 0.0
    for drill in existing_drills:
        drill_pos = _position(drill)
        if abs(position["x"] - drill_pos["x"]) <= 0.25:
            bonus += max(0.0, 6.0 - abs(position["y"] - drill_pos["y"]))
        if abs(position["y"] - drill_pos["y"]) <= 0.25:
            bonus += max(0.0, 6.0 - abs(position["x"] - drill_pos["x"]))
    return bonus


def _layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    footprint = [
        layout["drill_position"],
        layout["belt1_position"],
        layout["belt2_position"],
        layout["inserter_position"],
        layout["furnace_position"],
    ]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "transport-belt", "burner-inserter", "stone-furnace", "burner-mining-drill"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name in {"stone-furnace", "burner-mining-drill"} else 2.0
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _layout_from_drill_position(
    drill_position: dict[str, float],
    resource_name: str | None = None,
    orientation: str = "east",
) -> dict[str, Any]:
    dx, dy, drill_direction, belt_direction, inserter_direction = _smelting_orientation(orientation)
    return {
        "drill_position": drill_position,
        "belt1_position": {"x": drill_position["x"] + 2 * dx, "y": drill_position["y"] + 2 * dy},
        "belt2_position": {"x": drill_position["x"] + 3 * dx, "y": drill_position["y"] + 3 * dy},
        "inserter_position": {"x": drill_position["x"] + 4 * dx, "y": drill_position["y"] + 4 * dy},
        "furnace_position": {"x": drill_position["x"] + 5 * dx, "y": drill_position["y"] + 5 * dy},
        "orientation": orientation,
        "resource_name": resource_name,
        "drill_direction": drill_direction,
        "belt_direction": belt_direction,
        "inserter_direction": inserter_direction,
        "drill": None,
        "belt1": None,
        "belt2": None,
        "inserter": None,
        "furnace": None,
    }


def _layout_from_belt1_position(belt_position: dict[str, float], orientation: str = "east") -> dict[str, Any]:
    dx, dy, _drill_direction, _belt_direction, _inserter_direction = _smelting_orientation(orientation)
    return _layout_from_drill_position(
        {"x": belt_position["x"] - 2 * dx, "y": belt_position["y"] - 2 * dy},
        orientation=orientation,
    )


def _smelting_orientation(orientation: str) -> tuple[int, int, int, int, int]:
    if orientation == "west":
        return -1, 0, WEST, WEST, EAST
    if orientation == "south":
        return 0, 1, SOUTH, SOUTH, NORTH
    if orientation == "north":
        return 0, -1, NORTH, NORTH, SOUTH
    return 1, 0, EAST, EAST, WEST


def _entity_key_for_layout(entity_name: str, layout_key: str) -> str:
    if entity_name == "transport-belt" and layout_key == "belt1_position":
        return "belt1"
    if entity_name == "transport-belt" and layout_key == "belt2_position":
        return "belt2"
    if entity_name == "burner-mining-drill":
        return "drill"
    if entity_name == "stone-furnace":
        return "furnace"
    if entity_name == "burner-inserter":
        return "inserter"
    return entity_name


def _entity_key(entity_name: str) -> str:
    return _entity_key_for_layout(entity_name, "")


def _entity_near(
    observation: dict[str, Any],
    name: str,
    position: dict[str, float],
    radius: float,
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in entities_named(observation, name)
        if distance(_position(item), position) <= radius
    ]
    return _nearest_to(candidates, position)


def _select_power_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("power_sites")
    if not isinstance(sites, list):
        return None
    for site in sites:
        if not isinstance(site, dict):
            continue
        layout = _layout_from_power_site(site)
        if layout is not None:
            return layout
    return None


def _layout_from_power_site(site: dict[str, Any]) -> dict[str, Any] | None:
    raw_layout = site.get("layout")
    if not isinstance(raw_layout, dict):
        return None
    specs: dict[str, dict[str, Any]] = {}
    for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
        raw_spec = raw_layout.get(key)
        if not isinstance(raw_spec, dict) or not isinstance(raw_spec.get("position"), dict):
            return None
        specs[key] = {
            "name": str(raw_spec.get("name") or _power_spec_name(key)),
            "position": _position(raw_spec),
            "direction": int(raw_spec.get("direction") or NORTH),
        }
    return _power_layout_from_specs(specs)


def _power_layout_from_pump_position(position: dict[str, float]) -> dict[str, Any]:
    specs = {
        "offshore_pump": {
            "name": "offshore-pump",
            "position": position,
            "direction": WEST,
        },
        "boiler": {
            "name": "boiler",
            "position": {"x": position["x"] + 2, "y": position["y"] - 1},
            "direction": NORTH,
        },
        "steam_engine": {
            "name": "steam-engine",
            "position": {"x": position["x"] + 2, "y": position["y"] - 4},
            "direction": NORTH,
        },
        "small_electric_pole": {
            "name": "small-electric-pole",
            "position": {"x": position["x"], "y": position["y"] - 4},
            "direction": NORTH,
        },
    }
    return _power_layout_from_specs(specs)


def _power_layout_from_specs(specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    layout: dict[str, Any] = {}
    for key, spec in specs.items():
        layout[key] = None
        layout[f"{key}_spec"] = spec
    return layout


def _find_steam_power_block(observation: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for pump in entities_named(observation, "offshore-pump"):
        layout = _power_layout_from_pump_position(_position(pump))
        layout["offshore_pump"] = pump
        layout["boiler"] = _entity_near(observation, "boiler", layout["boiler_spec"]["position"], radius=1.0)
        layout["steam_engine"] = _entity_near(observation, "steam-engine", layout["steam_engine_spec"]["position"], radius=1.0)
        layout["small_electric_pole"] = _entity_near(
            observation,
            "small-electric-pole",
            layout["small_electric_pole_spec"]["position"],
            radius=1.0,
        )
        score = sum(1 for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole") if layout.get(key) is not None)
        candidates.append((score, layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _missing_power_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
        if layout.get(key) is None:
            item = _power_spec_name(key)
            if inventory_count(observation, item) <= 0:
                return item
    return None


def _power_spec_name(key: str) -> str:
    return {
        "offshore_pump": "offshore-pump",
        "boiler": "boiler",
        "steam_engine": "steam-engine",
        "small_electric_pole": "small-electric-pole",
    }[key]


def _power_item_required_count(item: str) -> int:
    return {
        "pipe": 5,
        "iron-gear-wheel": 8,
        "copper-cable": 2,
        "electronic-circuit": 2,
    }.get(item, 1)


def _steam_power_ready(layout: dict[str, Any] | None) -> bool:
    if not layout:
        return False
    return (
        layout.get("offshore_pump") is not None
        and layout.get("boiler") is not None
        and layout.get("steam_engine") is not None
        and layout.get("small_electric_pole") is not None
        and entity_fluid_count(layout["steam_engine"], "steam") > 0
        and int(layout["steam_engine"].get("status") or 0) != 5
    )


def _power_stand_position(layout: dict[str, Any]) -> dict[str, float]:
    pump_spec = layout.get("offshore_pump_spec") if isinstance(layout.get("offshore_pump_spec"), dict) else {}
    position = pump_spec.get("position") if isinstance(pump_spec.get("position"), dict) else {"x": 0.0, "y": 0.0}
    return {"x": float(position["x"]) + 5.0, "y": float(position["y"]) + 3.0}


def _nearest_tree(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    trees = [item for item in entities if isinstance(item, dict) and item.get("type") == "tree"]
    if not trees:
        return None
    return min(trees, key=lambda item: float(item.get("distance") or 999999))


class ResearchAutomationSkill:
    """Build and feed the first lab to unlock the Automation technology."""

    def __init__(self, technology: str = "automation") -> None:
        self.technology = technology
        self.power_skill = SetupPowerSkill()
        self.science_skill = AutomationScienceSkill(target_count=10)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        technology = _technology_state(observation, self.technology)
        if bool(technology.get("researched")):
            return PlannerDecision(None, f"{self.technology} research completed", done=True)

        player = player_position(observation)
        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        if _current_research(observation) != self.technology:
            return PlannerDecision(
                {"type": "research", "technology": self.technology},
                f"set current research to {self.technology}",
            )

        lab = _find_research_lab(observation)
        if lab is None:
            decision = self._ensure_item_quantity(observation, player, "lab", 1)
            if decision is not None:
                return decision
            site = _select_lab_site(observation)
            if site is None:
                return PlannerDecision(None, "cannot find a powered or wireable lab site near the starter power block")
            if not site.get("pole_unit_number"):
                decision = self._ensure_item_quantity(observation, player, "small-electric-pole", 1)
                if decision is not None:
                    return decision
                pole_position = site["pole_position"]
                if distance(player, pole_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(pole_position)},
                        "move near planned research pole",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "small-electric-pole",
                        "position": pole_position,
                    },
                    "extend electric network for research lab",
                )
            lab_position = site["lab_position"]
            if distance(player, lab_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(lab_position)},
                    "move near planned lab position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "lab",
                    "position": lab_position,
                    "allow_nearby": True,
                },
                "place first research lab",
            )

        pack_name = "automation-science-pack"
        lab_pack_count = entity_item_count(lab, pack_name)
        research_progress = _research_progress(observation)
        pack_goal = _research_pack_goal(observation, self.technology, pack_name)
        packs_needed = max(1, pack_goal - int(research_progress * pack_goal))
        if lab_pack_count > 0:
            return PlannerDecision({"type": "wait", "ticks": 600}, "wait for powered lab to consume science packs")

        inventory_packs = inventory_count(observation, pack_name)
        if inventory_packs > 0:
            lab_position = _position(lab)
            if distance(player, lab_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": lab_position},
                    "move near lab to insert automation science packs",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": pack_name,
                    "count": min(packs_needed, inventory_packs),
                    "unit_number": lab.get("unit_number"),
                    "name": "lab",
                    "position": lab_position,
                },
                "insert automation science packs into lab",
            )

        science_decision = AutomationScienceSkill(target_count=packs_needed).next_action(observation)
        if not science_decision.done:
            return science_decision

        return PlannerDecision({"type": "wait", "ticks": 600}, "wait for automation research progress")

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for automation research",
            )

        if item == "lab":
            for prerequisite, count in [
                ("electronic-circuit", 10),
                ("iron-gear-wheel", 10),
                ("transport-belt", 4),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "transport-belt":
            for prerequisite, count in [("iron-gear-wheel", 2), ("iron-plate", 2)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "electronic-circuit":
            decision = ElectronicCircuitSkill(target_count=quantity).next_action(observation)
            if not decision.done:
                return decision
            return None

        return self.power_skill._ensure_item_quantity(observation, player, item, quantity)


class ResearchTechnologySkill:
    """Research the next early technology using existing powered labs and red science."""

    def __init__(self, technology: str = "logistics") -> None:
        self.technology = technology
        self.bootstrap_skill = ResearchAutomationSkill()

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        technology = _technology_state(observation, self.technology)
        if bool(technology.get("researched")):
            return PlannerDecision(None, f"{self.technology} research completed", done=True)

        if not bool(_technology_state(observation, "automation").get("researched")) or _find_research_lab(observation) is None:
            decision = self.bootstrap_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for research bootstrap observation to settle")
            return decision

        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = SetupPowerSkill().next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        if _current_research(observation) != self.technology:
            return PlannerDecision(
                {"type": "research", "technology": self.technology},
                f"set current research to {self.technology}",
            )

        lab = _find_research_lab(observation)
        if lab is None:
            return PlannerDecision(None, "cannot find a lab for research")

        ingredients = technology.get("ingredients") if isinstance(technology.get("ingredients"), dict) else {}
        if not ingredients:
            return PlannerDecision({"type": "research", "technology": self.technology}, f"unlock trigger technology {self.technology}")

        player = player_position(observation)
        research_progress = _research_progress(observation)
        for pack_name in sorted(ingredients):
            if pack_name != "automation-science-pack":
                return PlannerDecision(None, f"research pack path is not implemented yet: {pack_name}")
            lab_pack_count = entity_item_count(lab, pack_name)
            pack_goal = _research_pack_goal(observation, self.technology, pack_name)
            packs_needed = max(1, pack_goal - int(research_progress * pack_goal))
            if lab_pack_count > 0:
                return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for lab to consume {pack_name}")

            inventory_packs = inventory_count(observation, pack_name)
            if inventory_packs > 0:
                lab_position = _position(lab)
                if distance(player, lab_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": lab_position}, f"move near lab to insert {pack_name}")
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": pack_name,
                        "count": min(packs_needed, inventory_packs),
                        "unit_number": lab.get("unit_number"),
                        "name": "lab",
                        "position": lab_position,
                    },
                    f"insert {pack_name} into lab for {self.technology}",
                )

            science_decision = AutomationScienceSkill(target_count=packs_needed).next_action(observation)
            if not science_decision.done:
                return science_decision

        return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for {self.technology} research progress")


class CircuitAutomationSkill:
    """Build a minimal powered assembler cell that makes green circuits."""

    def __init__(self, target_count: int = 5) -> None:
        self.target_count = target_count
        self.power_skill = SetupPowerSkill()
        self.research_skill = ResearchAutomationSkill()
        self.hand_circuit_skill = ElectronicCircuitSkill(target_count=max(7, target_count))
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not bool(_technology_state(observation, "automation").get("researched")):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        line = _find_circuit_automation_cell(observation) or _select_circuit_automation_site(observation)
        if line is None:
            return PlannerDecision(None, "cannot find a powered or wireable site for the first circuit assembler cell")

        missing_item = _missing_circuit_cell_item(observation, line)
        if missing_item:
            decision = self._ensure_item_quantity(observation, player, missing_item, _circuit_cell_required_count(line, missing_item))
            if decision is not None:
                return decision

        if not line.get("pole_unit_number"):
            pole_position = line["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(pole_position)},
                    "move near planned circuit automation pole",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "extend power for circuit automation cell",
            )

        build_order = [
            ("cable_assembler", "assembling-machine-1", "cable_assembler_position"),
            ("circuit_assembler", "assembling-machine-1", "circuit_assembler_position"),
            ("transfer_inserter", "inserter", "transfer_inserter_position"),
        ]
        for key, name, position_key in build_order:
            if line.get(key) is not None:
                continue
            position = line[position_key]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position)},
                    f"move near planned {name} position for circuit automation",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": False,
            }
            if key == "transfer_inserter":
                action["direction"] = int(line.get("transfer_inserter_direction") or EAST)
            return PlannerDecision(action, f"place {name} for circuit automation cell")

        if line.get("pole_unit_number") and not _circuit_cell_powered(line):
            pole_position = line["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, "move near circuit automation pole to connect power")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": line.get("pole_unit_number"),
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "connect circuit automation pole to nearby electric network",
            )

        cable_assembler = line.get("cable_assembler")
        circuit_assembler = line.get("circuit_assembler")
        if cable_assembler and cable_assembler.get("recipe") != "copper-cable":
            return self._set_recipe_decision(player, cable_assembler, "copper-cable")
        if circuit_assembler and circuit_assembler.get("recipe") != "electronic-circuit":
            return self._set_recipe_decision(player, circuit_assembler, "electronic-circuit")

        circuit_output = entity_item_count(circuit_assembler, "electronic-circuit") if circuit_assembler else 0
        if circuit_output > 0:
            circuit_pos = _position(circuit_assembler)
            if distance(player, circuit_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": circuit_pos},
                    "move near circuit assembler to collect produced circuits",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "electronic-circuit",
                    "count": min(circuit_output, self.target_count),
                    "unit_number": circuit_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": circuit_pos,
                },
                "take electronic circuits from assembler output",
            )

        if _circuit_cell_ready(line) and total_item_count(observation, "electronic-circuit") >= self.target_count:
            return PlannerDecision(
                None,
                f"circuit automation cell is running and target reached: {total_item_count(observation, 'electronic-circuit')}/{self.target_count}",
                done=True,
            )

        if circuit_assembler and entity_item_count(circuit_assembler, "copper-cable") < 6 and inventory_count(observation, "copper-cable") > 0:
            circuit_pos = _position(circuit_assembler)
            if distance(player, circuit_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": circuit_pos}, "move near circuit assembler to seed copper cable")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "copper-cable",
                    "count": min(12, inventory_count(observation, "copper-cable")),
                    "unit_number": circuit_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": circuit_pos,
                },
                "seed circuit assembler with available copper cable",
            )

        if cable_assembler and entity_item_count(cable_assembler, "copper-plate") < 4:
            if inventory_count(observation, "copper-plate") < 8:
                decision = self.copper_skill.next_action(observation, target_count=8, inventory_only=True)
                if not decision.done:
                    return decision
            cable_pos = _position(cable_assembler)
            if distance(player, cable_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": cable_pos}, "move near cable assembler to insert copper")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "copper-plate",
                    "count": min(20, inventory_count(observation, "copper-plate")),
                    "unit_number": cable_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": cable_pos,
                },
                "insert copper plates into cable assembler",
            )

        if circuit_assembler and entity_item_count(circuit_assembler, "iron-plate") < 4:
            if inventory_count(observation, "iron-plate") < 8:
                decision = self.iron_skill.next_action(observation, target_count=8, inventory_only=True)
                if not decision.done:
                    return decision
            circuit_pos = _position(circuit_assembler)
            if distance(player, circuit_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": circuit_pos}, "move near circuit assembler to insert iron")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "iron-plate",
                    "count": min(20, inventory_count(observation, "iron-plate")),
                    "unit_number": circuit_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": circuit_pos,
                },
                "insert iron plates into circuit assembler",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 600},
            "wait for assembler cell to make copper cable and electronic circuits",
        )

    def _set_recipe_decision(
        self,
        player: dict[str, float],
        assembler: dict[str, Any],
        recipe: str,
    ) -> PlannerDecision:
        position = _position(assembler)
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": position}, f"move near assembler to set {recipe}")
        return PlannerDecision(
            {
                "type": "set_recipe",
                "recipe": recipe,
                "unit_number": assembler.get("unit_number"),
                "name": "assembling-machine-1",
                "position": position,
            },
            f"set assembler recipe to {recipe}",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for circuit automation",
            )

        if item == "assembling-machine-1":
            for prerequisite, count in [
                ("electronic-circuit", 3 * quantity),
                ("iron-gear-wheel", 5 * quantity),
                ("iron-plate", 9 * quantity),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "inserter":
            for prerequisite, count in [
                ("electronic-circuit", quantity),
                ("iron-gear-wheel", quantity),
                ("iron-plate", quantity),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "electronic-circuit":
            decision = self.hand_circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None

        if item == "iron-gear-wheel":
            if craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(quantity - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for circuit automation",
                )
            return self._ensure_item_quantity(observation, player, "iron-plate", 2 * (quantity - inventory_count(observation, "iron-gear-wheel")))

        if item == "iron-plate":
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "small-electric-pole":
            return self.power_skill._ensure_item_quantity(observation, player, item, quantity)

        return PlannerDecision(None, f"missing {item} and no circuit automation prerequisite path is implemented")


class BuildItemMallSkill:
    """Build a small powered assembler cell for recurring factory-expansion items."""

    def __init__(self, target_item: str = "transport-belt", target_count: int = 20) -> None:
        self.target_item = target_item
        self.target_count = target_count
        self.power_skill = SetupPowerSkill()
        self.research_skill = ResearchAutomationSkill()
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=20)
        self.circuit_skill = ElectronicCircuitSkill(target_count=10)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        recipe = RECIPES.get(self.target_item)
        if recipe is None:
            return PlannerDecision(None, f"build item mall recipe is not known: {self.target_item}")

        player = player_position(observation)
        if not bool(_technology_state(observation, "automation").get("researched")):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        cell = _find_build_item_mall_cell(observation, self.target_item) or _select_build_item_mall_site(observation)
        if cell is None:
            return PlannerDecision(None, "cannot find a powered or wireable site for the first build item mall assembler")

        missing_item = _missing_build_item_mall_item(observation, cell)
        if missing_item:
            decision = self._ensure_item_quantity(observation, player, missing_item, _build_item_mall_required_count(cell, missing_item))
            if decision is not None:
                return decision

        if not cell.get("pole_unit_number") and not _build_item_mall_assembler_powered(cell):
            pole_position = cell["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(pole_position)}, "move near planned mall pole")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "extend power for build item mall",
            )

        assembler = cell.get("assembler")
        if assembler is None:
            assembler_position = cell["assembler_position"]
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(assembler_position)}, "move near planned mall assembler")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                    "allow_nearby": False,
                },
                f"place assembler for {self.target_item} mall cell",
            )

        if not assembler.get("electric_network_connected"):
            pole_position = cell["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, "move near mall pole to connect power")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": cell.get("pole_unit_number"),
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "connect build item mall pole to nearby electric network",
            )

        if assembler.get("recipe") != self.target_item:
            return self._set_recipe_decision(player, assembler, self.target_item)

        output_count = entity_item_count(assembler, self.target_item)
        if output_count > 0:
            assembler_position = _position(assembler)
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to collect {self.target_item}")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": self.target_item,
                    "count": min(output_count, self.target_count),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"take {self.target_item} from build item mall assembler",
            )

        if _build_item_mall_cell_ready(cell, self.target_item) and total_item_count(observation, self.target_item) >= self.target_count:
            return PlannerDecision(
                None,
                f"build item mall is producing {self.target_item} and target reached: {total_item_count(observation, self.target_item)}/{self.target_count}",
                done=True,
            )

        batch_count = _build_item_mall_batch_count(recipe.products.get(self.target_item, 1.0), self.target_count)
        for ingredient, amount in sorted(recipe.ingredients.items()):
            needed_in_assembler = max(1, int(amount * batch_count))
            if entity_item_count(assembler, ingredient) >= needed_in_assembler:
                continue
            if inventory_count(observation, ingredient) <= 0:
                decision = self._ensure_item_quantity(observation, player, ingredient, needed_in_assembler)
                if decision is not None:
                    return decision
            assembler_position = _position(assembler)
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to insert {ingredient}")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": ingredient,
                    "count": min(max(1, needed_in_assembler), inventory_count(observation, ingredient)),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"insert {ingredient} into {self.target_item} mall assembler",
            )

        return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for build item mall to produce {self.target_item}")

    def _set_recipe_decision(
        self,
        player: dict[str, float],
        assembler: dict[str, Any],
        recipe: str,
    ) -> PlannerDecision:
        position = _position(assembler)
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": position}, f"move near mall assembler to set {recipe}")
        return PlannerDecision(
            {
                "type": "set_recipe",
                "recipe": recipe,
                "unit_number": assembler.get("unit_number"),
                "name": "assembling-machine-1",
                "position": position,
            },
            f"set build item mall assembler recipe to {recipe}",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for build item mall",
            )

        if item == "assembling-machine-1":
            for prerequisite, count in [
                ("electronic-circuit", 3 * quantity),
                ("iron-gear-wheel", 5 * quantity),
                ("iron-plate", 9 * quantity),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "iron-gear-wheel":
            if craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(quantity - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for build item mall",
                )
            return self._ensure_item_quantity(observation, player, "iron-plate", 2 * (quantity - inventory_count(observation, "iron-gear-wheel")))

        if item == "electronic-circuit":
            decision = self.circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None

        if item == "iron-plate":
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-cable":
            if craftable_count(observation, "copper-cable") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "copper-cable",
                        "count": min(quantity - inventory_count(observation, "copper-cable"), craftable_count(observation, "copper-cable")),
                    },
                    "craft copper cable for build item mall",
                )
            return self._ensure_item_quantity(observation, player, "copper-plate", _ceil_div(quantity - inventory_count(observation, "copper-cable"), 2))

        if item == "small-electric-pole":
            return self.power_skill._ensure_item_quantity(observation, player, item, quantity)

        return PlannerDecision(None, f"missing {item} and no build item mall prerequisite path is implemented")


def _research_root(observation: dict[str, Any]) -> dict[str, Any]:
    value = observation.get("research")
    return value if isinstance(value, dict) else {}


def _technology_state(observation: dict[str, Any], technology: str) -> dict[str, Any]:
    research = _research_root(observation)
    technologies = research.get("technologies")
    if not isinstance(technologies, dict):
        return {}
    value = technologies.get(technology)
    return value if isinstance(value, dict) else {}


def _current_research(observation: dict[str, Any]) -> str | None:
    research = _research_root(observation)
    current = research.get("current")
    if not current and isinstance(research.get("queue"), list) and research["queue"]:
        current = research["queue"][0]
    return current if isinstance(current, str) and current else None


def _research_progress(observation: dict[str, Any]) -> float:
    value = _research_root(observation).get("progress")
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _research_pack_goal(observation: dict[str, Any], technology: str, pack_name: str) -> int:
    state = _technology_state(observation, technology)
    try:
        unit_count = int(state.get("research_unit_count") or 10)
    except (TypeError, ValueError):
        unit_count = 10
    ingredients = state.get("ingredients") if isinstance(state.get("ingredients"), dict) else {}
    try:
        pack_amount = int(ingredients.get(pack_name) or 1)
    except (TypeError, ValueError):
        pack_amount = 1
    return max(1, unit_count * pack_amount)


def _find_research_lab(observation: dict[str, Any]) -> dict[str, Any] | None:
    labs = entities_named(observation, "lab")
    if not labs:
        return None
    labs.sort(
        key=lambda item: (
            0 if item.get("electric_network_connected") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return labs[0]


def _select_lab_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("lab_sites")
    if not isinstance(sites, list):
        return None
    candidates = [
        site
        for site in sites
        if isinstance(site, dict)
        and isinstance(site.get("pole_position"), dict)
        and isinstance(site.get("lab_position"), dict)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _find_circuit_automation_cell(observation: dict[str, Any]) -> dict[str, Any] | None:
    assemblers = entities_named(observation, "assembling-machine-1")
    cable_candidates = [item for item in assemblers if item.get("recipe") == "copper-cable"]
    for cable in cable_candidates:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        return layout

    circuit_candidates = [item for item in assemblers if item.get("recipe") == "electronic-circuit"]
    for circuit in circuit_candidates:
        circuit_pos = _position(circuit)
        layout = _circuit_cell_layout_from_circuit_position(circuit_pos)
        layout["circuit_assembler"] = circuit
        layout["cable_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["cable_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        return layout

    unassigned = [item for item in assemblers if not item.get("recipe")]
    for cable in unassigned:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        circuit = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        if circuit is None:
            continue
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = circuit
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        return layout

    for cable in unassigned:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        return layout

    return None


def _select_circuit_automation_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("automation_sites")
    if not isinstance(sites, list):
        return None
    candidates: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        required = [
            "pole_position",
            "cable_assembler_position",
            "circuit_assembler_position",
            "transfer_inserter_position",
        ]
        if not all(isinstance(site.get(key), dict) for key in required):
            continue
        candidates.append(
            {
                "pole_position": _xy_position(site["pole_position"]),
                "cable_assembler_position": _xy_position(site["cable_assembler_position"]),
                "circuit_assembler_position": _xy_position(site["circuit_assembler_position"]),
                "transfer_inserter_position": _xy_position(site["transfer_inserter_position"]),
                "transfer_inserter_direction": int(site.get("transfer_inserter_direction") or EAST),
                "pole_unit_number": site.get("pole_unit_number"),
                "source_pole_unit_number": site.get("source_pole_unit_number"),
                "powered": site.get("powered"),
                "distance": site.get("distance"),
                "pole": None,
                "cable_assembler": None,
                "circuit_assembler": None,
                "transfer_inserter": None,
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _circuit_cell_layout_from_cable_position(cable_position: dict[str, float]) -> dict[str, Any]:
    return {
        "pole_position": {"x": cable_position["x"] + 2, "y": cable_position["y"] - 2},
        "cable_assembler_position": cable_position,
        "circuit_assembler_position": {"x": cable_position["x"] + 4, "y": cable_position["y"]},
        "transfer_inserter_position": {"x": cable_position["x"] + 2, "y": cable_position["y"]},
        "transfer_inserter_direction": EAST,
        "pole": None,
        "cable_assembler": None,
        "circuit_assembler": None,
        "transfer_inserter": None,
    }


def _circuit_cell_layout_from_circuit_position(circuit_position: dict[str, float]) -> dict[str, Any]:
    return _circuit_cell_layout_from_cable_position({"x": circuit_position["x"] - 4, "y": circuit_position["y"]})


def _missing_circuit_cell_item(observation: dict[str, Any], line: dict[str, Any]) -> str | None:
    if not line.get("pole_unit_number") and inventory_count(observation, "small-electric-pole") <= 0:
        return "small-electric-pole"
    missing_assemblers = _circuit_cell_required_count(line, "assembling-machine-1")
    if missing_assemblers > inventory_count(observation, "assembling-machine-1"):
        return "assembling-machine-1"
    if line.get("transfer_inserter") is None and inventory_count(observation, "inserter") <= 0:
        return "inserter"
    return None


def _circuit_cell_required_count(line: dict[str, Any], item: str) -> int:
    if item == "assembling-machine-1":
        return sum(1 for key in ("cable_assembler", "circuit_assembler") if line.get(key) is None)
    return 1


def _circuit_cell_ready(line: dict[str, Any]) -> bool:
    cable = line.get("cable_assembler")
    circuit = line.get("circuit_assembler")
    return bool(
        line.get("pole_unit_number")
        and cable
        and circuit
        and line.get("transfer_inserter")
        and cable.get("recipe") == "copper-cable"
        and circuit.get("recipe") == "electronic-circuit"
    )


def _circuit_cell_powered(line: dict[str, Any]) -> bool:
    for key in ("cable_assembler", "circuit_assembler", "transfer_inserter"):
        entity = line.get(key)
        if isinstance(entity, dict) and entity.get("electric_network_connected"):
            return True
    return False


def _find_build_item_mall_cell(observation: dict[str, Any], target_item: str) -> dict[str, Any] | None:
    assemblers = entities_named(observation, "assembling-machine-1")
    candidates = [item for item in assemblers if item.get("recipe") == target_item]
    if not candidates:
        candidates = [
            item
            for item in assemblers
            if not item.get("recipe")
            and not _near_recipe_assembler(observation, item, {"copper-cable", "electronic-circuit"}, radius=5.5)
        ]
    if not candidates:
        return None
    assembler = min(candidates, key=lambda item: float(item.get("distance") or 999999))
    assembler_position = _position(assembler)
    pole = _nearest_to(entities_named(observation, "small-electric-pole"), assembler_position)
    pole_in_reach = pole is not None and distance(_position(pole), assembler_position) <= 7.5
    pole_position = _position(pole) if pole_in_reach else {
        "x": assembler_position["x"] + 2,
        "y": assembler_position["y"] - 2,
    }
    return {
        "pole_position": pole_position,
        "assembler_position": assembler_position,
        "pole": pole if pole_in_reach else None,
        "pole_unit_number": pole.get("unit_number") if pole_in_reach else None,
        "assembler": assembler,
        "powered": assembler.get("electric_network_connected"),
    }


def _select_build_item_mall_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("automation_sites")
    if not isinstance(sites, list):
        return None
    candidates: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        if not isinstance(site.get("pole_position"), dict) or not isinstance(site.get("cable_assembler_position"), dict):
            continue
        pole_position = _xy_position(site["pole_position"])
        assembler_position = _xy_position(site["cable_assembler_position"])
        pole = _entity_near(observation, "small-electric-pole", pole_position, radius=1.0)
        assembler = _entity_near(observation, "assembling-machine-1", assembler_position, radius=1.5)
        candidates.append(
            {
                "pole_position": pole_position,
                "assembler_position": assembler_position,
                "pole_unit_number": site.get("pole_unit_number") or (pole.get("unit_number") if pole else None),
                "source_pole_unit_number": site.get("source_pole_unit_number"),
                "powered": bool(site.get("powered") or (assembler and assembler.get("electric_network_connected"))),
                "distance": site.get("distance"),
                "pole": pole,
                "assembler": assembler,
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _missing_build_item_mall_item(observation: dict[str, Any], cell: dict[str, Any]) -> str | None:
    if not cell.get("pole_unit_number") and not _build_item_mall_assembler_powered(cell) and inventory_count(observation, "small-electric-pole") <= 0:
        return "small-electric-pole"
    if cell.get("assembler") is None and inventory_count(observation, "assembling-machine-1") <= 0:
        return "assembling-machine-1"
    return None


def _build_item_mall_required_count(cell: dict[str, Any], item: str) -> int:
    if item == "assembling-machine-1":
        return 1 if cell.get("assembler") is None else 0
    return 1


def _build_item_mall_cell_ready(cell: dict[str, Any], target_item: str) -> bool:
    assembler = cell.get("assembler")
    return bool(
        isinstance(assembler, dict)
        and assembler.get("electric_network_connected")
        and assembler.get("recipe") == target_item
    )


def _build_item_mall_assembler_powered(cell: dict[str, Any]) -> bool:
    assembler = cell.get("assembler")
    return bool(isinstance(assembler, dict) and assembler.get("electric_network_connected"))


def _build_item_mall_batch_count(product_count: float, target_count: int) -> int:
    try:
        per_batch = max(1, int(product_count))
    except (TypeError, ValueError):
        per_batch = 1
    return max(1, min(4, _ceil_div(max(1, target_count), per_batch)))


def _near_recipe_assembler(
    observation: dict[str, Any],
    assembler: dict[str, Any],
    recipes: set[str],
    radius: float,
) -> bool:
    position = _position(assembler)
    for other in entities_named(observation, "assembling-machine-1"):
        if other is assembler:
            continue
        if other.get("recipe") in recipes and distance(position, _position(other)) <= radius:
            return True
    return False


def _xy_position(value: dict[str, Any]) -> dict[str, float]:
    return {
        "x": float(value.get("x") or 0.0),
        "y": float(value.get("y") or 0.0),
    }
