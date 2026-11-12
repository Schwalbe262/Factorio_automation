"""Ordinary paid storage for v2 construction and recovery inventory headroom."""

from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json


KEEP = {
    "iron-ore": 0,
    "copper-ore": 0,
    "wood": 50,
    "stone": 50,
    "coal": 100,
    "burner-mining-drill": 1,
    "stone-furnace": 6,
    "transport-belt": 200,
    "underground-belt": 20,
    "splitter": 4,
    "iron-plate": 400,
    "copper-plate": 400,
    "steel-plate": 100,
    "iron-gear-wheel": 100,
    "electronic-circuit": 100,
    "copper-cable": 100,
    "inserter": 20,
    "fast-inserter": 20,
    "small-electric-pole": 30,
    "assembling-machine-1": 4,
}
CHESTS = ("iron-chest", "wooden-chest", "steel-chest")


def chest_craft_batch(catalog, slots, empty_slots, availability=None):
    """Find a paid chest batch whose ingredients free the needed output space.

    Simulate consuming the largest normal ingredient stack first so a smaller
    partial stack is never relied upon to free output space. Actual slot counts,
    not aggregate modulo arithmetic, determine whether the output can fit.
    """
    plain = [s for s in slots if s.get("quality") == "normal" and s.get("type") == "item"]
    for name in CHESTS:
        recipe = catalog.recipes.get(name)
        stack_size = catalog.items.get(name, {}).get("stack_size", 0)
        if availability is not None and not (
            availability.get(name, {}).get("enabled") and availability.get(name, {}).get("handcraftable")
        ):
            continue
        if not recipe or not stack_size or any(i.get("type", "item") != "item" for i in recipe["ingredients"]):
            continue
        outputs = recipe.get("products", [])
        if len(outputs) != 1 or outputs[0]["name"] != name or outputs[0].get("probability", 1) != 1:
            continue
        amount = outputs[0].get("amount", 0)
        if not amount:
            continue
        for cycles in range(1, min(50, int(stack_size / amount)) + 1):
            freed = 0
            enough = True
            for ingredient in recipe["ingredients"]:
                need = cycles * ingredient["amount"]
                for stack in sorted((s for s in plain if s["name"] == ingredient["name"]), key=lambda s: -s["count"]):
                    take = min(need, stack["count"])
                    freed += int(take == stack["count"])
                    need -= take
                    if need <= 0:
                        break
                if need > 0:
                    enough = False
                    break
            existing_space = sum(max(0, stack_size - s["count"]) for s in plain if s["name"] == name)
            if enough and cycles * amount <= existing_space + (empty_slots + freed) * stack_size:
                return {
                    "type": "craft",
                    "recipe": recipe.get("name", name),
                    "count": cycles,
                    "reason": "normally craft storage chests with ingredient/output inventory headroom",
                }
    return None


class InventoryStorage:
    def __init__(self, game, catalog):
        self.game, self.catalog = game, catalog
        self.path = Path(game.cfg.runtime_dir) / "inventory-storage.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _snapshot(self):
        payload = json.dumps(
            json.dumps(
                {"chests": self.state.get("chests", []), "items": list(KEEP), "recipes": list(CHESTS)},
                separators=(",", ":"),
            )
        )
        return self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """)
if not a or not a.valid then return failure("agent_dead") end
local inv=a.get_main_inventory();if not inv or not inv.valid then return failure("main_inventory_missing") end
local slots={};local empty=0
for index=1,#inv do local stack=inv[index]
 if stack.valid_for_read then slots[#slots+1]={slot=index,name=stack.name,count=stack.count,
  quality=stack.quality.name,type=stack.prototype.type,stack_size=stack.prototype.stack_size}
 else empty=empty+1 end
end
local chests={}
for _,spec in ipairs(args.chests) do local e=target(spec.position,spec.name)
 if e and e.unit_number==spec.unit_number and e.force==f and e.type=="container" then
  local dest=e.get_inventory(defines.inventory.chest);local space={}
  for _,item in ipairs(args.items) do space[item]=dest.get_insertable_count{name=item,quality="normal"} end
  chests[#chests+1]={name=e.name,position=pos(e.position),unit_number=e.unit_number,space=space}
 end
end
local recipes={}
for _,name in ipairs(args.recipes) do local recipe=f.recipes[name]
 if recipe then
  local categories={};local good,values=pcall(function() return recipe.categories end)
  if good and values then categories=values else
   local old_good,category=pcall(function() return recipe.category end)
   if old_good and category then categories={category} end
  end
  local hand=false;for k,v in pairs(categories) do local category=type(k)=="string" and k or v
   if prototypes.entity.character.crafting_categories[category] then hand=true end
  end
  recipes[name]={enabled=recipe.enabled,handcraftable=hand}
 end
end
return {ok=true,world_id=d.world_id,actor_unit_number=a.unit_number,inventory_size=#inv,slots=slots,empty_slots=empty,
 crafting=a.crafting_queue_size>0,chests=chests,recipes=recipes}
"""
        )

    def _site(self, obs):
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
        if board and board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "storage_board_world_changed"}
        areas = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
        areas += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", [])]
        origin = obs.get("position", {"x": 0, "y": 0})
        candidates = []
        for radius in (8, 16, 24, 32, 48, 64, 96):
            for dx, dy in ((-1, -1), (-1, 0), (0, -1), (1, -1), (1, 0), (0, 1), (-1, 1), (1, 1)):
                x, y = math.floor(origin["x"] + dx * radius) + 0.5, math.floor(origin["y"] + dy * radius) + 0.5
                if not any(
                    x + 0.5 > area[0][0] and x - 0.5 < area[1][0] and y + 0.5 > area[0][1] and y - 0.5 < area[1][1]
                    for area in areas
                ):
                    candidates.append({"x": x, "y": y})
        payload = json.dumps(json.dumps(candidates, separators=(",", ":")))
        return self.game.query(
            """
local candidates=helpers.json_to_table("""
            + payload
            + """)
for _,p in ipairs(candidates) do
 if s.count_entities_filtered{area={{p.x-2,p.y-2},{p.x+2,p.y+2}},type="resource"}==0
  and s.count_entities_filtered{area={{p.x-1.5,p.y-1.5},{p.x+1.5,p.y+1.5}}}==0
  and s.can_place_entity{name="iron-chest",position=p,force=f} then return {ok=true,position=p} end
end
return {ok=false,reason="no_clear_storage_site_outside_reserved_blocks"}
"""
        )

    def step(self, obs, *, minimum_slots=4):
        if type(minimum_slots) is not int or minimum_slots < 1:
            raise ValueError("minimum inventory headroom must be a positive integer")
        if self.state and self.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "inventory_storage_world_changed"}
        if obs.get("inventory_empty_slots", 0) >= minimum_slots and not self.state.get("pending_build"):
            return None
        snapshot = self._snapshot()
        if not snapshot.get("ok") or snapshot.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": snapshot.get("reason", "storage_snapshot_world_changed")}
        if not self.state:
            self.state = {"world_id": obs["world_id"], "chests": []}
        pending = self.state.get("pending_build")
        if pending:
            actual = next(
                (
                    e
                    for e in obs.get("entities", [])
                    if e["name"] == pending["name"] and e["position"] == pending["position"]
                ),
                None,
            )
            if actual:
                if type(actual.get("unit_number")) is not int:
                    return {"status": "blocked", "reason": "storage_chest_identity_missing"}
                self.state["chests"].append({k: actual[k] for k in ("name", "position", "unit_number")})
                self.state.pop("pending_build")
                self.state.pop("pending_craft", None)
                self._save()
                snapshot = self._snapshot()
                if not snapshot.get("ok") or snapshot.get("world_id") != obs["world_id"]:
                    return {"status": "blocked", "reason": snapshot.get("reason", "storage_snapshot_world_changed")}
        if snapshot.get("inventory_size") is not None and minimum_slots > snapshot["inventory_size"]:
            return {
                "status": "blocked",
                "reason": "requested_inventory_headroom_exceeds_capacity",
                "minimum_slots": minimum_slots,
            }
        if snapshot["empty_slots"] >= minimum_slots:
            return None
        slots = snapshot.get("slots", []) or []
        held = {}
        for stack in slots:
            if stack.get("quality") == "normal" and stack.get("type") == "item":
                held[stack["name"]] = held.get(stack["name"], 0) + stack["count"]
        chests = snapshot.get("chests", []) or []
        surplus_items = {item: held.get(item, 0) - keep for item, keep in KEEP.items() if held.get(item, 0) > keep}
        if not surplus_items and not self.state.get("pending_build"):
            return {
                "status": "blocked",
                "reason": "insufficient_unreserved_plain_items_for_requested_headroom",
                "minimum_slots": minimum_slots,
            }
        for item, keep in KEEP.items():
            surplus = held.get(item, 0) - keep
            if surplus <= 0:
                continue
            for chest in chests:
                amount = min(1000, surplus, chest["space"].get(item, 0))
                if amount > 0:
                    return {
                        "type": "deposit",
                        "name": chest["name"],
                        "position": chest["position"],
                        "item": item,
                        "count": int(amount),
                        "expected_entity_unit": chest["unit_number"],
                        "expected_entity_world_id": obs["world_id"],
                        "reason": "store normal construction surplus to preserve mining/recovery inventory headroom",
                    }
        if snapshot.get("crafting"):
            return {"status": "waiting", "reason": "waiting for ordinary storage chest crafting"}
        name = next((name for name in CHESTS if held.get(name, 0) > 0), None)
        if name:
            pending = self.state.get("pending_build")
            if not pending:
                site = self._site(obs)
                if not site.get("ok"):
                    return {"status": "blocked", "reason": site.get("reason", "storage_site_unavailable")}
                pending = {"name": name, "position": site["position"]}
                self.state["pending_build"] = pending
                self._save()
            return {
                "type": "build",
                "name": pending["name"],
                "item": pending["name"],
                "position": pending["position"],
                "direction": 0,
                "reason": "normally build dedicated construction/recovery storage outside factory reservations",
            }
        craft = chest_craft_batch(self.catalog, slots, snapshot["empty_slots"], snapshot.get("recipes", {}))
        if craft:
            self.state["pending_craft"] = {"recipe": craft["recipe"], "count": craft["count"]}
            self._save()
            return craft
        return {"status": "blocked", "reason": "inventory_full_without_safe_paid_storage_chest_batch"}
