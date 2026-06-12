from __future__ import annotations

from typing import Any

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
WEST = 12
FURNACE_RESOURCE_RADIUS = 12.0


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
    """Build a minimal belt-fed burner smelting line for early iron automation."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count
        self.support_skill = IronPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        line = _find_belt_smelting_line(observation)
        line_furnace = line.get("furnace") if line else None
        total_iron = total_item_count(observation, "iron-plate")
        if line_furnace and self._line_has_started(line_furnace) and total_iron >= self.target_count:
            return PlannerDecision(
                None,
                f"belt smelting line produced iron plates: {total_iron}/{self.target_count}",
                done=True,
            )

        player = player_position(observation)
        layout = line or _select_belt_smelting_layout(observation)
        if layout is None:
            return PlannerDecision(None, "cannot find open iron ore site for belt smelting line")

        if inventory_count(observation, "coal") < 12:
            coal = nearest_resource(observation, "coal")
            if coal is None:
                return PlannerDecision(None, "cannot find coal for burner smelting line")
            return self.support_skill._mine_resource(player, coal, "coal", 16)

        need = _line_missing_item(observation, layout)
        if need:
            decision = self._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction in [
            ("transport-belt", "belt1_position", EAST),
            ("transport-belt", "belt2_position", EAST),
            ("burner-inserter", "inserter_position", WEST),
            ("stone-furnace", "furnace_position", None),
            ("burner-mining-drill", "drill_position", EAST),
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
                "allow_nearby": name == "burner-mining-drill",
            }
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
        if layout_furnace and entity_item_count(layout_furnace, "iron-plate") > 0:
            furnace_pos = _position(layout_furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near automated furnace output",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "iron-plate",
                    "count": min(50, entity_item_count(layout_furnace, "iron-plate")),
                    "unit_number": layout_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                "take iron plates produced by belt smelting line",
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

    @staticmethod
    def _line_has_started(furnace: dict[str, Any]) -> bool:
        return entity_item_count(furnace, "iron-ore") > 0 or entity_item_count(furnace, "iron-plate") > 0


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


def _find_belt_smelting_line(observation: dict[str, Any]) -> dict[str, Any] | None:
    belts = entities_named(observation, "transport-belt")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for belt in belts:
        belt_pos = _position(belt)
        layout = _layout_from_belt1_position(belt_pos)
        layout["belt1"] = belt
        layout["belt2"] = _entity_near(observation, "transport-belt", layout["belt2_position"], radius=0.75)
        layout["inserter"] = _entity_near(observation, "burner-inserter", layout["inserter_position"], radius=1.0)
        layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=1.5)
        layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
        score = sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if layout.get(key) is not None)
        candidates.append((score, layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _select_belt_smelting_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    candidates = [item for item in resources if isinstance(item, dict) and item.get("name") == "iron-ore"]
    candidates.sort(key=lambda item: float(item.get("distance") or 999999))
    for resource in candidates:
        layout = _layout_from_drill_position(_position(resource))
        footprint = [
            layout["drill_position"],
            layout["belt1_position"],
            layout["belt2_position"],
            layout["inserter_position"],
            layout["furnace_position"],
        ]
        blocked = False
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "")
            if name in {"character", "transport-belt", "burner-inserter", "stone-furnace", "burner-mining-drill"}:
                entity_pos = _position(entity)
                if any(distance(entity_pos, pos) < 2.0 for pos in footprint):
                    blocked = True
                    break
        if not blocked:
            return layout
    return None


def _layout_from_drill_position(drill_position: dict[str, float]) -> dict[str, Any]:
    return {
        "drill_position": drill_position,
        "belt1_position": {"x": drill_position["x"] + 2, "y": drill_position["y"]},
        "belt2_position": {"x": drill_position["x"] + 3, "y": drill_position["y"]},
        "inserter_position": {"x": drill_position["x"] + 4, "y": drill_position["y"]},
        "furnace_position": {"x": drill_position["x"] + 5, "y": drill_position["y"]},
        "drill": None,
        "belt1": None,
        "belt2": None,
        "inserter": None,
        "furnace": None,
    }


def _layout_from_belt1_position(belt_position: dict[str, float]) -> dict[str, Any]:
    return _layout_from_drill_position({"x": belt_position["x"] - 2, "y": belt_position["y"]})


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
