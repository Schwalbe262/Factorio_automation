"""Material-backed silo construction, automatic rocket parts, and first launch.

Only the one-off silo is hand crafted. Its ingredients and the starter pack
arrive from ordinary production machines through bounded collection chests;
the silo's repeating recipe receives all materials through automatic belts.
Factory reservations and the save's launch record provide restart persistence.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .deterministic_launch import LaunchStage


def _report(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


def _find(obs: dict, spec: dict) -> dict | None:
    return next((entity for entity in obs.get("entities", [])
                 if entity.get("name") == spec["name"]
                 and math.dist([entity["position"][a] for a in ("x", "y")],
                               [spec["position"][a] for a in ("x", "y")]) < .2), None)


def _enabled(obs: dict, recipe: str) -> bool:
    enabled = obs.get("enabled_recipes", {})
    return bool(enabled.get(recipe)) if isinstance(enabled, dict) else recipe in enabled


def _position(x: float, y: float) -> dict:
    return {"x": x, "y": y}


class DeterministicRocket:
    def __init__(self, game: Any, bootstrap: Any, builder: Any, catalog: Any, factory: Any):
        self.game, self.bootstrap, self.builder = game, bootstrap, builder
        self.catalog, self.factory = catalog, factory
        self.launch = LaunchStage(catalog)

    def _move(self, observation: dict, position: dict) -> dict | None:
        if self.game.backend != "character":
            return None
        actor = observation.get("position", {"x": 0, "y": 0})
        if math.dist([actor[a] for a in ("x", "y")], [position[a] for a in ("x", "y")]) > 4:
            return {"type": "move", "position": position}
        return None

    @staticmethod
    def buffer_plan(item: str) -> dict:
        return {"ok": True, "entities": [
            {"name": "iron-chest", "position": _position(.5, .5), "direction": 0},
            {"name": "inserter", "position": _position(-.5, .5), "direction": 12},
            {"name": "transport-belt", "position": _position(-1.5, .5), "direction": 4},
            {"name": "small-electric-pole", "position": _position(-.5, 2.5), "direction": 0}],
            "ports": [{"kind": "item", "item": item, "direction": "input", "facing": 4,
                       "position": _position(-1.5, .5)}]}

    def _buffer_state(self, chest: dict, item: str) -> dict:
        payload = json.dumps(json.dumps({"position": chest["position"], "name": chest["name"], "item": item}))
        return self.game.query('''
local x=helpers.json_to_table(''' + payload + ''')
local e=target(x.position,x.name)
if not e then return {ok=false,reason="buffer_missing"} end
local inventory=e.get_inventory(defines.inventory.chest)
if not inventory or not inventory.supports_bar() then return {ok=false,reason="buffer_inventory_unavailable"} end
return {ok=true,count=inventory.get_item_count(x.item),bar=inventory.get_bar(),size=#inventory}
''')

    def collect_product(self, obs: dict, item: str, count: int) -> dict:
        """Collect a finite machine-produced batch through a capped belt buffer."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("collection count must be a positive integer")
        self.factory._sync(obs)
        have = int(obs.get("inventory", {}).get(item, 0))
        if have >= count:
            return _report("succeeded", "manufactured batch is in character inventory", item=item, count=have)
        key = "rocket-buffer:" + item
        plan = self.factory.state["blocks"].get(key)
        if plan:
            chest = next(e for e in plan["entities"] if e["name"] == "iron-chest")
            if _find(obs, chest):
                stock = self._buffer_state(chest, item)
                if not stock.get("ok"):
                    return _report("blocked", "cannot inspect manufactured batch buffer", item=item, query_error=stock.get("reason"))
                stack = int(self.catalog.items.get(item, {}).get("stack_size") or 0)
                if stack < 1:
                    return _report("blocked", "catalog item stack size is missing", item=item)
                slots = math.ceil(count / stack)
                if slots > int(stock["size"]):
                    return _report("blocked", "manufactured batch exceeds buffer capacity", item=item, count=count,
                                   capacity=int(stock["size"]) * stack)
                if int(stock["bar"]) != slots + 1:
                    return self._move(obs, chest["position"]) or {"type": "bar", "name": chest["name"],
                        "position": chest["position"], "slots": slots}
                if int(stock["count"]) > 0:
                    return self._move(obs, chest["position"]) or {"type": "take", "name": chest["name"],
                        "position": chest["position"], "inventory": "chest", "item": item,
                        "count": min(count - have, int(stock["count"]))}
        source = self.factory.ensure_product(obs, item)
        if not _ready(source):
            return source
        output = next((p for p in source.get("evidence", {}).get("ports", [])
                       if p.get("kind") == "item" and p.get("item") == item), None)
        if output is None:
            return _report("blocked", "manufactured item has no collection output port", item=item)
        plan = self.factory.reserve_site(self.buffer_plan(item), key, obs, output["position"])
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "batch buffer site unavailable"), item=item)
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        # Cap the chest before powering its inserter or attaching the producer.
        chest = next(e for e in plan["entities"] if e["name"] == "iron-chest")
        if not _find(obs, chest):
            return _report("waiting", "waiting for observed collection chest", item=item)
        stock = self._buffer_state(chest, item)
        if not stock.get("ok"):
            return _report("blocked", "cannot inspect manufactured batch buffer", item=item, query_error=stock.get("reason"))
        stack = int(self.catalog.items.get(item, {}).get("stack_size") or 0)
        if stack < 1 or math.ceil(count / stack) > int(stock["size"]):
            return _report("blocked", "manufactured batch has unsupported buffer capacity", item=item, count=count)
        slots = math.ceil(count / stack)
        if int(stock["bar"]) != slots + 1:
            return self._move(obs, chest["position"]) or {"type": "bar", "name": chest["name"],
                "position": chest["position"], "slots": slots}
        result = self.factory.ensure_power_connection(obs, key, plan)
        if not _ready(result):
            return result
        result = self.factory.connect_input(obs, output, plan["ports"][0], key + ":input")
        if not _ready(result):
            return result
        return _report("waiting", "waiting for automatically manufactured batch", item=item, have=have, need=count,
                       buffer=chest["position"])

    def silo_plan(self) -> dict:
        recipe = self.catalog.recipe_for_product("rocket-part")
        if not recipe:
            return {"ok": False, "reason": "catalog rocket-part recipe is missing"}
        ingredients = recipe["ingredients"]
        if len(ingredients) > 3 or any(row.get("type", "item") != "item" for row in ingredients):
            return {"ok": False, "reason": "rocket recipe exceeds three solid silo supply ports"}
        prototype = self.catalog.entities["rocket-silo"]
        box = prototype.get("collision_box")
        if isinstance(box, dict):
            low, high = box["left_top"], box["right_bottom"]
        elif isinstance(box, list) and len(box) == 2:
            low, high = box
        else:
            return {"ok": False, "reason": "catalog silo collision box is missing"}
        coordinates = lambda point: (point["x"], point["y"]) if isinstance(point, dict) else point
        x1, y1 = coordinates(low)
        x2, y2 = coordinates(high)
        width, height = math.ceil(x2 - x1), math.ceil(y2 - y1)
        center = _position(.5 if width % 2 else 0, .5 if height % 2 else 0)
        entities = [{"name": "rocket-silo", "position": center, "direction": 0, "recipe": recipe["name"],
                     "_width": width, "_height": height}]
        ports = []
        for ingredient, (dx, dy, direction) in zip(ingredients, [(-1, 0, 12), (0, 1, 8), (1, 0, 4)]):
            half = width / 2 if dx else height / 2
            x = center["x"] + dx * (half + .5)
            y = center["y"] + dy * (half + .5)
            belt = _position(x + dx, y + dy)
            entities.extend([
                {"name": "inserter", "position": _position(x, y), "direction": direction},
                {"name": "transport-belt", "position": belt, "direction": (direction + 8) % 16},
                {"name": "small-electric-pole", "position": _position(x - dy * 2, y + dx * 2), "direction": 0}])
            ports.append({"kind": "item", "item": ingredient["name"], "direction": "input",
                          "position": belt, "facing": (direction + 8) % 16})
        return {"ok": True, "entities": entities, "ports": ports, "template": "first_rocket_silo"}

    def _ensure_silo_item(self, obs: dict) -> dict:
        if obs.get("inventory", {}).get("rocket-silo", 0) > 0:
            return _report("succeeded", "manufactured silo is available")
        if obs.get("crafting_queue"):
            return _report("waiting", "waiting for engine silo handcraft queue")
        recipe = self.catalog.recipe_for_product("rocket-silo")
        if not recipe or not _enabled(obs, recipe["name"]):
            return _report("blocked", "rocket silo recipe is not unlocked")
        character_categories = set(self.catalog.entities.get("character", {}).get("crafting_categories", []))
        if not character_categories.intersection(recipe["categories"]):
            return _report("blocked", "silo recipe is not handcraftable in this catalog")
        for ingredient in recipe["ingredients"]:
            if ingredient.get("type", "item") != "item":
                return _report("blocked", "silo handcraft recipe requires a fluid", ingredient=ingredient)
            result = self.collect_product(obs, ingredient["name"], math.ceil(ingredient["amount"]))
            if not _ready(result):
                return result
        return {"type": "craft", "recipe": recipe["name"], "count": 1,
                "reason": "engine craft silo from automatically manufactured ingredients"}

    def _launch_action(self, obs: dict) -> dict:
        result = self.launch.next_action(obs)
        action = result.get("action")
        if action:
            return self._move(obs, action["position"]) or action
        return _report(result["status"], result["reason"], requirements=result.get("requirements", []),
                       launch=obs.get("launch", {}))

    def next_action(self, obs: dict) -> dict:
        self.factory._sync(obs)
        if (obs.get("launch") or {}).get("ordered"):
            return self._launch_action(obs)
        researched = obs.get("technologies", {}).get("rocket-silo")
        if not researched:
            return _report("waiting", "rocket silo technology must finish through normal research")
        origin = self.silo_plan()
        if not origin.get("ok"):
            return _report("blocked", origin["reason"])
        plan = self.factory.reserve_site(origin, "rocket:silo", obs)
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "silo site unavailable"))
        silo = next(e for e in plan["entities"] if e["name"] == "rocket-silo")
        existing = _find(obs, silo)
        if not existing:
            result = self._ensure_silo_item(obs)
            if not _ready(result):
                return result
        result = self.builder.ensure_plan(obs, plan)
        if not _ready(result):
            return result
        result = self.factory.ensure_power_connection(obs, "rocket:silo", plan)
        if not _ready(result):
            return result
        for port in plan["ports"]:
            source = self.factory.ensure_product(obs, port["item"])
            if not _ready(source):
                return source
            output = next((p for p in source.get("evidence", {}).get("ports", [])
                           if p.get("kind") == "item" and p.get("item") == port["item"]), None)
            if output is None:
                return _report("blocked", "rocket material has no automatic output port", item=port["item"])
            result = self.factory.connect_input(obs, output, port, "rocket:silo:" + port["item"])
            if not _ready(result):
                return result
        existing = _find(obs, silo)
        if not existing:
            return _report("waiting", "waiting for observed constructed silo")
        pack = "space-platform-starter-pack"
        if not existing.get("inventory", {}).get(pack, 0):
            result = self.collect_product(obs, pack, 1)
            if not _ready(result):
                return result
        # The newly reserved silo, not an unrelated observer-built silo, owns this launch.
        launch_obs = {**obs, "entities": [existing]}
        return self._launch_action(launch_obs)
