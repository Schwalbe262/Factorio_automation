"""Observation-driven starter cells and natural early research unlocks.

Only coal, stone and trees may be hand-mined during bootstrap. Iron and copper
always come from drills feeding real furnaces. All crafting uses live enabled
recipes and the character's engine crafting queue.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import math
from typing import Any


# Key on a starter-upkeep wait that only waits for burner fuel to pile up (no action is possible).
FUEL_WAIT = "fuel_wait"


def _report(status: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _at(entity: dict[str, Any], name: str, position: dict[str, float]) -> bool:
    p = entity.get("position") or {}
    return (
        entity.get("name") == name
        and abs(p.get("x", 1e9) - position["x"]) < 0.2
        and abs(p.get("y", 1e9) - position["y"]) < 0.2
    )


# Final items the character may have in the native crafting queue at once.
CRAFT_QUEUE_ITEMS = 3

# Freshness of a reused drill-cell survey, per caller (``_existing_cells``):
# game ticks the pass observation may be newer than the pass that took it,
# always only while no action ran (``act_epoch``) and the world is the same.
# Fuel, output and wait decisions read fuel/burning/operating: that pass only.
SURVEY_SAME_PASS = 0
# Site choice, cell discovery, retirement and supply planning read structure
# (drill, receiver, belt, remaining ore), which only an action changes.
SURVEY_LAYOUT_TICKS = 600

# Every drill that mines exactly one resource, grouped by that resource.
EXISTING_CELLS_LUA = """
local by={}
for _,e in pairs(s.find_entities_filtered{force=f,type="mining-drill"}) do
 local target_name=nil
 local good,t=pcall(function() return e.mining_target end)
 if good and t and t.valid then target_name=t.name end
 local counts={};local best=0;local kinds=0
 local radius=e.prototype.mining_drill_radius
 for _,ore in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},
      {e.position.x+radius,e.position.y+radius}},type="resource"}) do
  if math.abs(ore.position.x-e.position.x)<=radius and math.abs(ore.position.y-e.position.y)<=radius and ore.amount>0 then
   if not counts[ore.name] then kinds=kinds+1 end
   counts[ore.name]=(counts[ore.name] or 0)+ore.amount
   if counts[ore.name]>best then best=counts[ore.name];if not good or not t or not t.valid then target_name=ore.name end end
  end
 end
 if target_name and kinds==1 then
  local resource=target_name
  local receiver=nil;local direct_belt=false
  for _,candidate in pairs(s.find_entities_filtered{position=e.drop_position,radius=1.5,force=f}) do
   if candidate.type=="furnace" or candidate.type=="container" then
    local box=candidate.bounding_box;local p=e.drop_position
    if math.abs(p.x-candidate.position.x)<=candidate.prototype.tile_width/2
     and math.abs(p.y-candidate.position.y)<=candidate.prototype.tile_height/2 then receiver=candidate;break end
   elseif candidate.type=="transport-belt" or candidate.type=="underground-belt" then
    local b=candidate.bounding_box;local p=e.drop_position
    if p.x>=b.left_top.x and p.x<b.right_bottom.x and p.y>=b.left_top.y and p.y<b.right_bottom.y then direct_belt=true end
   end
  end
  local burner=e.burner
  local cells=by[resource] or {};by[resource]=cells
  cells[#cells+1]={drill={name=e.name,position=pos(e.position),direction=e.direction},drop_position=pos(e.drop_position),
   receiver=receiver and {name=receiver.name,position=pos(receiver.position)} or nil,direct_belt=direct_belt,
   fuel=e.get_fuel_inventory() and e.get_fuel_inventory().get_item_count("coal") or 0,
   burning=burner and burner.remaining_burning_fuel>0 or false,electric=burner==nil,
   operating=burner==nil and e.energy>0 and e.is_connected_to_electric_network() or false,
   remaining=counts[resource] or 0}
 end
end
return {ok=true,cells_by_resource=by}
"""


class DeterministicBootstrap:
    def __init__(self, game: Any, catalog: Any = None):
        self.game = game
        self.catalog = catalog
        self._recipes: dict[str, dict[str, Any]] = {}
        self._world_id: str | None = None
        self.construction_buffers: dict[str, dict[str, Any]] = {}
        self._cell_surveys: dict[str, Any] | None = None  # survey_slice
        self._survey_pass: tuple[str, int] | None = None  # begin_survey_pass

    @contextmanager
    def survey_slice(self):
        """Let ``_existing_cells`` reuse one survey inside this block (``jobs.run_jobs``, one slice).

        The cache starts empty and is dropped on exit, so nothing outlives the
        slice. Reuse also needs a pass (``begin_survey_pass``) and a game that
        counts its actions (``act_epoch``); otherwise every call queries.
        """
        self._cell_surveys = {}
        self._survey_pass = None
        try:
            yield
        finally:
            self._cell_surveys = None
            self._survey_pass = None

    def begin_survey_pass(self, observation: dict[str, Any]) -> None:
        """The observation a ``run_jobs`` pass plans against: its world and tick bound reuse."""
        if getattr(self, "_cell_surveys", None) is None:
            return
        tick, world = observation.get("tick"), observation.get("world_id")
        self._survey_pass = (world, tick) if type(tick) is int and isinstance(world, str) else None

    def next_action(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not observation.get("ok", True):
            return _report("blocked", observation.get("reason", "observation_failed"))
        world_id = observation.get("world_id")
        if world_id != self._world_id:
            self._recipes.clear()
            self._world_id = world_id
        for entity in observation.get("entities", []):
            if entity.get("name") == "burner-mining-drill" and entity.get("status_name") in {
                "no_minable_resources",
                "no_mineable_resources",
            }:
                return {
                    "type": "mine",
                    "name": entity["name"],
                    "position": entity["position"],
                    "count": 1,
                    "reason": "recover depleted drill for a live ore patch",
                }
        fuel = self.maintain_fuel(observation)
        if fuel is not None:
            if not fuel.get("type") and fuel.get("status") == "waiting":
                # Marked so a job's shared upkeep keeps building while the coal piles up
                # (handlers.upkeep.shared_upkeep, FUEL_WAIT).
                return {**fuel, FUEL_WAIT: True}
            return fuel
        for resource, receiver in [
            ("iron-ore", "stone-furnace"),
            ("coal", "wooden-chest"),
            ("copper-ore", "stone-furnace"),
            ("stone", "wooden-chest"),
        ]:
            action = self._ensure_cell(observation, resource, receiver)
            if action is not None:
                return action
        techs = observation.get("technologies") or {}
        if not techs.get("steam-power"):
            return self._collect_or_wait(
                observation, "iron-plate", 60, "waiting for natural 50-iron-plate steam-power unlock"
            )
        if not techs.get("electronics"):
            return self._collect_or_wait(
                observation, "copper-plate", 20, "waiting for natural 10-copper-plate electronics unlock"
            )
        if not self._count(observation, "lab") and not any(
            e.get("name") == "lab" for e in observation.get("entities", [])
        ):
            return self.ensure_item(observation, "lab", 1)
        if not techs.get("automation-science-pack"):
            # The client detach (actor_client) happens only after this trigger
            # fired; past it a hand craft needs no credit. A detached actor
            # here means the transition rule was broken: never craft uncredited.
            actor = self.game.query(
                "local r=d.detached_actor;local c=nil;for i=#(d.trigger_crafts or {}),1,-1 do "
                'local q=d.trigger_crafts[i];if q.technology=="automation-science-pack" and q.item=="lab" then c=q;break end end '
                "local queued=false;for _,q in pairs(a.crafting_queue or {}) do if c and q.recipe==c.recipe then queued=true end end "
                "return {ok=true,associated=a.player~=nil,detached=r~=nil,client_free=r~=nil and r.mode=='none',"
                "craft=c and {tick=c.tick,min_craft_ticks=c.min_craft_ticks,item_count_before=c.item_count_before,"
                "consumed_any=c.consumed_any} or nil,queued=queued,tick=game.tick}"
            )
            if actor.get("client_free"):
                return self._client_free_lab_trigger(observation, actor)
            if actor.get("detached"):
                return _report(
                    "blocked",
                    "actor detached before the lab trigger: hand crafts are no longer credited",
                    dependency="crafting_player",
                )
            if not actor.get("associated"):
                return _report(
                    "blocked",
                    "engine crafting requires an associated player for research credit",
                    dependency="crafting_player",
                    query_error=actor.get("reason"),
                )
            # A resumed prototype may contain a lab crafted by an unassociated
            # character. Craft another through the associated player's engine;
            # owning the old item cannot substitute for the natural craft event.
            return self.ensure_item(observation, "lab", self._count(observation, "lab") + 1)
        return _report(
            "succeeded",
            "starter mining cells and natural early unlocks verified",
            technologies=["steam-power", "electronics", "automation-science-pack"],
            lab_count=self._count(observation, "lab"),
        )

    def _client_free_lab_trigger(self, observation: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        """Client-free test mode (actor_client ``none``): the one script-credited trigger.

        The lab is crafted for real by the normal craft action (recorded in the
        world with what it consumed); once it is finished and in the inventory
        the trigger the engine would have fired for a connected player is
        credited by ``credit_hand_craft_trigger``. Nothing else is granted."""
        craft = actor.get("craft") or {}
        have = self._count(observation, "lab")
        if not craft or not craft.get("consumed_any") or have < craft.get("item_count_before", 0) + 1:
            # No recorded real craft of a lab that is still in hand: craft one.
            return self.ensure_item(observation, "lab", have + 1)
        if actor.get("queued") or actor.get("tick", 0) < craft["tick"] + craft.get("min_craft_ticks", 0):
            return _report("waiting", "waiting for the real lab hand craft to finish", craft_tick=craft["tick"])
        return {
            "type": "credit_hand_craft_trigger",
            "technology": "automation-science-pack",
            "item": "lab",
            "reason": "test mode client_free: credit the craft-1-lab trigger after the real craft",
        }

    @staticmethod
    def _count(observation: dict[str, Any], item: str) -> int:
        return int((observation.get("inventory") or {}).get(item, 0))

    def _recipe(self, item: str) -> dict[str, Any]:
        if item in self._recipes:
            return self._recipes[item]
        # Querying force recipes keeps the engine's exact categories and amounts;
        # enabled flags are rechecked against each fresh observation below.
        body = (
            """
local item="""
            + json.dumps(item)
            + """
local r=f.recipes[item]
if not r then
 for _,candidate in pairs(f.recipes) do
  if not candidate.hidden then for _,p in pairs(candidate.products) do
   if p.type=="item" and p.name==item then r=candidate;break end
  end end
  if r then break end
 end
end
if not r then return {ok=false,reason="no_recipe"} end
local categories={}
local good,values=pcall(function() return r.categories end)
if good and values then categories=values else
 local old_good,category=pcall(function() return r.category end)
 if old_good and category then categories={category} end
end
local hand=false
for k,v in pairs(categories) do
 local category=type(k)=="string" and k or v
 if prototypes.entity.character.crafting_categories[category] then hand=true end
end
local ingredients={};local products={}
for _,p in pairs(r.ingredients) do ingredients[#ingredients+1]={name=p.name,type=p.type,amount=p.amount} end
for _,p in pairs(r.products) do products[#products+1]={name=p.name,type=p.type,amount=p.amount,probability=p.probability} end
return {ok=true,name=r.name,enabled=r.enabled,handcraftable=hand,ingredients=ingredients,products=products}
"""
        )
        recipe = self.game.query(body)
        if recipe.get("ok"):
            self._recipes[item] = recipe
        return recipe

    def _handcraft_collection_targets(
        self, observation: dict[str, Any], item: str, count: int
    ) -> dict[str, int] | None:
        """Reserve shared inventory for one batch before collecting its raw inputs.

        Unsupported recipe trees decline this optimization. The ordinary recipe
        path remains authoritative, including machine-only ingredient handling.
        """
        initial = dict(observation.get("inventory") or {})
        stock = dict(initial)
        deficits: dict[str, int] = {}
        leaf_order: list[str] = []
        output_reserved: dict[str, int] = {}
        enabled = observation.get("enabled_recipes") or {}
        visits = 0

        def require(name: str, amount: int, path: tuple[str, ...]) -> bool:
            nonlocal visits
            raw = name in {"iron-plate", "copper-plate", "coal", "stone", "wood"}
            if raw and name not in leaf_order:
                leaf_order.append(name)
            held = min(int(stock.get(name, 0)), amount)
            stock[name] = int(stock.get(name, 0)) - held
            amount -= held
            if amount <= 0:
                return True
            if raw:
                deficits[name] = deficits.get(name, 0) + amount
                return True
            reserved = output_reserved.get(name, 0)
            produced = self._take_output(observation, name, amount + reserved)
            available = max(0, int(produced["count"]) - reserved) if produced else 0
            if available:
                available = min(available, amount)
                output_reserved[name] = reserved + available
                deficits[name] = deficits.get(name, 0) + available
                if name not in leaf_order:
                    leaf_order.append(name)
                amount -= available
                if amount <= 0:
                    return True
            visits += 1
            if name in path or len(path) >= 32 or visits > 128 or name in {"iron-ore", "copper-ore"}:
                return False
            recipe = self._recipe(name)
            if not recipe.get("ok") or not recipe.get("handcraftable") or not enabled.get(recipe["name"]):
                return False
            products = recipe.get("products", [])
            ingredients = recipe.get("ingredients", [])
            for row in products + ingredients:
                quantity = float(row.get("amount") or 0)
                if (
                    row.get("type", "item") != "item"
                    or not math.isfinite(quantity)
                    or quantity <= 0
                    or row.get("probability", 1) not in {None, 1}
                ):
                    return False
            if any(not float(row["amount"]).is_integer() for row in products):
                return False
            output = sum(float(row["amount"]) for row in products if row["name"] == name)
            if output <= 0:
                return False
            runs = math.ceil(amount / output)
            for ingredient in ingredients:
                if not require(ingredient["name"], math.ceil(float(ingredient["amount"]) * runs), (*path, name)):
                    return False
            for product in products:
                stock[product["name"]] = stock.get(product["name"], 0) + int(float(product["amount"]) * runs)
            stock[name] -= amount
            return True

        if not require(item, count, ()):
            return None
        return {name: int(initial.get(name, 0)) + deficits[name] for name in leaf_order if name in deficits}

    def _collect_handcraft_batch(self, observation: dict[str, Any], item: str, count: int) -> dict[str, Any] | None:
        targets = self._handcraft_collection_targets(observation, item, count)
        for material, required in (targets or {}).items():
            missing = required - self._count(observation, material)
            if missing <= 0:
                continue
            collect = self._take_output(observation, material, missing)
            if collect is not None:
                return collect
            if material in {"coal", "stone"}:
                cells = self._existing_cells(material)
                if not cells.get("ok"):
                    return _report(
                        "blocked",
                        "cannot verify automated raw-material supply",
                        item=material,
                        query_error=cells.get("reason"),
                    )
                if cells.get("cells"):
                    return _report("waiting", f"waiting for automated {material} output")
            return self.ensure_item(observation, material, required, (item,))
        return None

    def ensure_item(
        self, observation: dict[str, Any], item: str, count: int, _stack: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Return one material-acquisition/crafting action, or an explicit report."""
        have = self._count(observation, item)
        if have >= count:
            return _report("succeeded", "required inventory available", item=item, count=have)
        if item == "firearm-magazine":
            # Engine crafting can put magazines in the equipped ammunition slot.
            # Recover an observed full normal stack before paying for more; the
            # adapter rechecks identity, metadata and space before moving it.
            for stack in observation.get("recoverable_equipped_ammo") or []:
                if (
                    stack.get("item") != item
                    or type(stack.get("slot")) is not int
                    or stack["slot"] < 1
                    or type(stack.get("count")) is not int
                    or stack["count"] < 1
                ):
                    continue
                unit = observation.get("actor_unit_number")
                world = observation.get("world_id")
                if type(unit) is not int or unit < 1 or not isinstance(world, str) or not world:
                    return _report("blocked", "equipped ammunition recovery requires the observed actor identity")
                return {
                    "type": "recover_equipped_ammo",
                    "item": item,
                    "slot": stack["slot"],
                    "count": min(count - have, stack["count"], 100),
                    "expected_actor_world_id": world,
                    "expected_actor_unit_number": unit,
                    "reason": "recover normally crafted equipped ammunition for production defense",
                }
        collect = self._take_output(observation, item, count - have)
        if collect is not None:
            return collect
        if item in {"iron-plate", "copper-plate"}:
            return _report("waiting", f"waiting for drill-fed {item} production", item=item, have=have, need=count)
        if item in {"iron-ore", "copper-ore"}:
            return _report("blocked", "iron and copper ore require a mining drill", item=item)
        if item in {"coal", "stone", "wood"}:
            return self._raw_material(observation, item, count - have)
        queue = observation.get("crafting_queue") or []
        if len(queue) >= CRAFT_QUEUE_ITEMS:
            return _report("waiting", "engine crafting queue is busy", item=item)
        if item in _stack:
            return _report("blocked", "handcraft dependency cycle", item=item)
        recipe = self._recipe(item)
        if not recipe.get("ok"):
            return _report(
                "blocked",
                "no live recipe for required item",
                item=item,
                query_error=recipe.get("reason", "recipe_query_failed"),
            )
        enabled = observation.get("enabled_recipes") or {}
        if not enabled.get(recipe["name"]):
            return _report("blocked", "required recipe is locked", item=item, recipe=recipe["name"])
        if not recipe.get("handcraftable"):
            producers = self._live_producers(observation, recipe["name"], item)
            if producers:
                # A machine-only item (steel-plate) that a built machine on the map makes is
                # a wait for its output, not a dead end (eval-1575138435, 320 min: the engine
                # row's steel chest needed 8 steel while the commissioned steel furnaces had
                # nothing out yet; the job blocked "required item needs a production machine").
                return _report(
                    "waiting",
                    f"waiting for automated {item} output",
                    item=item,
                    recipe=recipe["name"],
                    producers=producers,
                    have=have,
                    need=count,
                )
            # No machine makes it anywhere: name the block that would (rocket_plan.producer_for);
            # the supervisor proposes it (Supervisor._missing_producers) instead of a dead end.
            producer = None
            if self.catalog is not None:
                from ..rocket_plan import producer_for

                producer = producer_for(self.catalog, item)
            return _report(
                "blocked",
                "required item needs a production machine",
                item=item,
                recipe=recipe["name"],
                have=have,
                need=count,
                **({"producer": producer} if producer else {}),
            )
        output = sum(
            float(p.get("amount") or 0)
            for p in recipe["products"]
            if p.get("name") == item and p.get("type", "item") == "item" and p.get("probability", 1) in {None, 1}
        )
        if output <= 0:
            return _report("blocked", "recipe has no deterministic item output", item=item)
        # Several different items may be in the native queue at once; the same
        # item is not queued twice, its queued runs count toward the request.
        queued = sum(
            int(row.get("count") or 0)
            for row in queue
            if isinstance(row, dict) and row.get("recipe") == recipe["name"] and not row.get("prerequisite")
        )
        runs = math.ceil((count - have) / output) - queued
        if runs <= 0:
            return _report("waiting", "engine crafting queue is busy", item=item, queued_runs=queued)
        if not _stack and self._handcraft_collection_targets(observation, item, count) == {}:
            # The full enabled handcraft tree is reserved from held inventory.
            # Factorio queues and pays for its intermediates at normal speed;
            # the requested count is still only the final recipe's missing runs.
            return {
                "type": "craft",
                "recipe": recipe["name"],
                "count": runs,
                "reason": f"engine craft {item} with normal prerequisite queue",
            }
        if not _stack:
            collect = self._collect_handcraft_batch(observation, item, count)
            if collect is not None:
                return collect
        for ingredient in recipe["ingredients"]:
            if ingredient.get("type", "item") != "item":
                return _report("blocked", "handcraft recipe requires fluid", item=item)
            amount = math.ceil(float(ingredient["amount"]) * runs)
            if self._count(observation, ingredient["name"]) < amount:
                return self.ensure_item(observation, ingredient["name"], amount, (*_stack, item))
        return {
            "type": "craft",
            "recipe": recipe["name"],
            "count": runs,
            "reason": f"engine craft {item} from live recipe",
        }

    @staticmethod
    def _live_producers(observation: dict[str, Any], recipe: str, item: str | None = None) -> int:
        """Built machines that make ``recipe``, not unpowered: the recipe is set, or (an idle furnace
        reports no recipe) the machine holds ``item`` (eval-1575138435, 330 min: both steel furnaces
        ``no_ingredients`` with 85 and 78 steel inside, no recipe in the observation; the slack view of
        a job hides output the critical path claims, ``critical_path.slack_view``)."""
        return sum(
            1
            for e in observation.get("entities", [])
            if e.get("type") in {"furnace", "assembling-machine"}
            and e.get("status_name") not in {"no_power", "disabled", "marked_for_deconstruction"}
            and (
                e.get("recipe") == recipe
                or (
                    item is not None
                    and e.get("type") == "furnace"
                    and int((e.get("inventory") or {}).get(item, 0) or 0) > 0
                )
            )
        )

    def _take_output(self, observation: dict[str, Any], item: str, count: int) -> dict[str, Any] | None:
        buffer = self.construction_buffers.get(item)
        if buffer and buffer.get("world_id") == observation.get("world_id"):
            chest = next(
                (
                    e
                    for e in observation.get("entities", [])
                    if e.get("unit_number") == buffer["unit_number"]
                    and e.get("name") == buffer["name"]
                    and e.get("position") == buffer["position"]
                ),
                None,
            )
            stock = int((chest or {}).get("inventory", {}).get(item, 0))
            if stock > 0:
                return {
                    "type": "take",
                    "name": buffer["name"],
                    "position": buffer["position"],
                    "item": item,
                    "count": min(count, stock, 50),
                    "reason": f"collect buffered construction {item}",
                }
        candidates = []
        for entity in observation.get("entities", []):
            name = entity.get("name", "")
            if name not in {
                "stone-furnace",
                "steel-furnace",
                "electric-furnace",
                "wooden-chest",
                "iron-chest",
                "steel-chest",
                "assembling-machine-1",
                "assembling-machine-2",
                "assembling-machine-3",
            }:
                continue
            if item in {"coal", "wood"} and "chest" not in name:
                continue  # Never count an operating machine's fuel as output.
            if "furnace" in name and item not in {"iron-plate", "copper-plate", "steel-plate", "stone-brick"}:
                continue
            machine = "furnace" in name or name.startswith("assembling-machine")
            inventory = (
                entity.get("output_inventory") if machine and "output_inventory" in entity else entity.get("inventory")
            )
            stock = int((inventory or {}).get(item, 0))
            if stock > 0 and name.startswith("assembling-machine") and "output_inventory" not in entity:
                result = self.game.query(
                    "local e=target({x="
                    + str(float(entity["position"]["x"]))
                    + ",y="
                    + str(float(entity["position"]["y"]))
                    + "},"
                    + json.dumps(name)
                    + "); "
                    "local output=e and e.get_output_inventory();return {count=output and output.get_item_count("
                    + json.dumps(item)
                    + ") or 0}"
                )
                stock = int(result.get("count", 0))
            if stock > 0:
                candidates.append(
                    {
                        "type": "take",
                        "name": name,
                        "position": entity["position"],
                        "item": item,
                        "count": min(count, stock, 50),
                        "reason": f"collect produced {item}",
                    }
                )
        if candidates:
            actor = observation.get("position") or {"x": 0, "y": 0}
            # Verified construction buffers were handled above. Prefer actual
            # producer output over incidental, unbound chest stock.
            return min(
                candidates,
                key=lambda action: (
                    "chest" in action["name"],
                    -action["count"],
                    (action["position"]["x"] - actor["x"]) ** 2 + (action["position"]["y"] - actor["y"]) ** 2,
                    action["name"],
                    action["position"]["x"],
                    action["position"]["y"],
                ),
            )
        # Finite construction batches can be picked from the actual conveyor
        # after an output inserter has moved them out of the producing machine.
        # Power coal remains reserved for the continuous burner supply.
        if item not in {"coal", "wood"}:
            for entity in observation.get("entities", []):
                stock = int((entity.get("belt_inventory") or {}).get(item, 0))
                if entity.get("name") == "transport-belt" and stock > 0:
                    return {
                        "type": "take",
                        "name": entity["name"],
                        "position": entity["position"],
                        "item": item,
                        "count": min(count, stock, 50),
                        "reason": f"collect constructed supply belt {item}",
                    }
        return None

    def _collect_or_wait(self, observation: dict[str, Any], item: str, count: int, reason: str) -> dict[str, Any]:
        collect = self._take_output(observation, item, max(1, count - self._count(observation, item)))
        return collect or _report("waiting", reason)

    def maintain_fuel(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        for entity in observation.get("entities", []):
            if entity.get("name") not in {"burner-mining-drill", "stone-furnace"}:
                continue
            fuel = (entity.get("inventory") or {}).get("coal", 0) + (entity.get("inventory") or {}).get("wood", 0)
            if fuel >= 2:
                continue
            coal = self._count(observation, "coal")
            if coal <= 0:
                return self.ensure_item(observation, "coal", 24)
            return {
                "type": "insert",
                "name": entity["name"],
                "position": entity["position"],
                "item": "coal",
                "count": min(8, coal),
                "inventory": "fuel",
                "reason": "fuel starter production",
            }
        return None

    def _raw_material(self, observation: dict[str, Any], item: str, count: int) -> dict[str, Any]:
        if item == "wood":
            tree = self.game.query("""
local best=nil;local distance=math.huge
for _,e in pairs(s.find_entities_filtered{position=a.position,radius=384,type="tree"}) do
 local ds=(e.position.x-a.position.x)^2+(e.position.y-a.position.y)^2
 if ds<distance then best=e;distance=ds end
end
return best and {ok=true,name=best.name,position=pos(best.position)} or {ok=false,reason="no_tree"}
""")
            if not tree.get("ok"):
                return _report("blocked", "no reachable bootstrap wood source")
            return {
                "type": "mine",
                "name": tree["name"],
                "position": tree["position"],
                "count": 1,
                "reason": "collect starter wood from a real tree",
            }
        cells = self._existing_cells(item)
        if not cells.get("ok"):
            return _report(
                "blocked", "cannot verify automated raw-material supply", item=item, query_error=cells.get("reason")
            )
        if cells.get("cells"):
            # A coal drill with no fuel cannot bootstrap itself. A small emergency
            # seed may restart it; once operating, wait for its actual chest output.
            # Only a drill that fills a chest can end this wait. A drill that
            # drops straight onto a belt (the dedicated power feed) never
            # produces collectable coal, so it must not count as supply.
            if item != "coal" or any(
                (c.get("receiver") or not c.get("direct_belt"))
                and (c.get("electric") or c.get("fuel", 0) > 0 or c.get("burning"))
                for c in cells["cells"]
            ):
                return _report("waiting", f"waiting for automated {item} output")
            count = min(count, 8)
        resource = (observation.get("resources") or {}).get(item)
        if not resource:
            return _report("blocked", "starter resource not observed", item=item)
        return {
            "type": "mine",
            "name": item,
            "position": resource["position"],
            "count": min(50, max(1, count)),
            "reason": f"bootstrap {item} until drill supply is operating",
        }

    def _existing_cells(self, resource: str, *, max_age_ticks: int = SURVEY_SAME_PASS) -> dict[str, Any]:
        """Drill -> receiver cells of ``resource`` that mine only that resource.

        One query surveys every resource at once (the drill scan is the same
        for each). Inside ``survey_slice`` the survey is reused while the
        world is the same, no ``act`` ran since it was taken, and the pass
        observation is at most ``max_age_ticks`` newer than the pass that took
        it (``SURVEY_SAME_PASS``: that pass only; never across a rollback).
        """
        entry = self._fresh_survey(max_age_ticks)
        if entry is None:
            epoch = getattr(self.game, "act_epoch", None)
            result = self.game.query(EXISTING_CELLS_LUA)
            if result.get("ok") is not True:
                return result
            by_resource = result.get("cells_by_resource")
            by_resource = by_resource if isinstance(by_resource, dict) else {}
            entry = {"by_resource": by_resource}
            current = getattr(self, "_survey_pass", None)
            if getattr(self, "_cell_surveys", None) is not None and current is not None and type(epoch) is int:
                entry.update(world=current[0], tick=current[1], epoch=epoch)
                self._cell_surveys["entry"] = entry
        cells = entry["by_resource"].get(resource) or []
        return {"ok": True, "cells": deepcopy(list(cells) if isinstance(cells, (list, tuple)) else [])}

    def _fresh_survey(self, max_age_ticks: int) -> dict[str, Any] | None:
        cache = getattr(self, "_cell_surveys", None)
        current = getattr(self, "_survey_pass", None)
        if not cache or current is None:
            return None
        entry = cache.get("entry")
        epoch = getattr(self.game, "act_epoch", None)
        if (
            entry is None
            or type(epoch) is not int
            or entry["epoch"] != epoch
            or entry["world"] != current[0]
            or not 0 <= current[1] - entry["tick"] <= max_age_ticks
        ):
            return None
        return entry

    def discover_cell(
        self,
        resource: str,
        receiver_name: str,
        *,
        preferred_receiver: dict[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A complete existing cell, an existing drill's missing receiver, or a new site.

        With ``observation`` a new site avoids every board block and
        planned-but-unbuilt entity (``board.starter_keepouts``), e.g. the
        reserved coal feed.
        """
        existing = self._existing_cells(resource, max_age_ticks=SURVEY_LAYOUT_TICKS)
        if not existing.get("ok"):
            return existing
        cells = existing.get("cells", [])
        complete = [cell for cell in cells if cell.get("receiver") and cell["receiver"]["name"] == receiver_name]
        if complete:
            complete.sort(
                key=lambda cell: (
                    cell["receiver"] != preferred_receiver,
                    not (cell.get("electric") and cell.get("operating")),
                    cell["receiver"]["position"]["x"],
                    cell["receiver"]["position"]["y"],
                )
            )
            return {"ok": True, "complete": True, **complete[0]}
        for cell in cells:
            if cell["drill"].get("direction") == 0 and not cell.get("direct_belt"):
                p = cell["drill"]["position"]
                drop = cell["drop_position"]
                receiver = (
                    {"x": p["x"], "y": p["y"] - 2}
                    if receiver_name == "stone-furnace"
                    else {"x": math.floor(drop["x"]) + 0.5, "y": math.floor(drop["y"]) + 0.5}
                )
                return {
                    "ok": True,
                    "complete": False,
                    "drill": cell["drill"],
                    "receiver": {"name": receiver_name, "position": receiver},
                }
        keepouts = {"areas": [], "tiles": []}
        root = getattr(getattr(self.game, "cfg", None), "runtime_dir", None)
        if observation is not None and root is not None:
            from factorio_agent.board import starter_keepouts

            keepouts = starter_keepouts(root, observation)
        from factorio_agent.board import KEEPOUT_LUA

        return self.game.query(
            """
local resource="""
            + json.dumps(resource)
            + """;local receiver_name="""
            + json.dumps(receiver_name)
            + """;local keepouts=helpers.json_to_table("""
            + json.dumps(json.dumps(keepouts, separators=(",", ":")))
            + ")"
            + KEEPOUT_LUA
            + """
local strict="""
            + ("true" if getattr(self.game, "backend", None) == "character" else "false")
            + """
local function can_plan(name,p)
 local spec={name=name,position=p,direction=0,force=f}
 if s.can_place_entity(spec) then return true end
 if not strict then return false end
 local b=prototypes.entity[name].collision_box
 local area={{p.x+b.left_top.x,p.y+b.left_top.y},{p.x+b.right_bottom.x,p.y+b.right_bottom.y}}
 local own_actor=false
 for _,e in pairs(s.find_entities_filtered{area=area}) do
  if e==a then own_actor=true elseif e.type~="resource" then return false end
 end
 if not own_actor then return false end
 spec.build_check_type=defines.build_check_type.script;spec.forced=false
 return s.can_place_entity(spec)
end
local seen={};local best=nil;local best_score=-math.huge
for _,ore in pairs(s.find_entities_filtered{position={0,0},radius=384,name=resource}) do
 local x=math.floor(ore.position.x+0.5);local y=math.floor(ore.position.y+0.5)
 local key=x..","..y
 if not seen[key] then
  seen[key]=true
  local p={x=x,y=y};local receiver={x=x,y=y-2}
  if receiver_name=="wooden-chest" then receiver={x=x-0.5,y=y-1.5} end
  local old_receiver=target(receiver,receiver_name)
  local half=receiver_name=="wooden-chest" and .5 or 1
  local kept_out=keepout_hit(x-1,y-1,x+1,y+1) or (not old_receiver
   and keepout_hit(receiver.x-half,receiver.y-half,receiver.x+half,receiver.y+half))
  if not kept_out and can_plan("burner-mining-drill",p)
   and (old_receiver or can_plan(receiver_name,receiver)) then
   local richness=0;local tiles=0;local mixed=false
   for _,r in pairs(s.find_entities_filtered{area={{x-1,y-1},{x+1,y+1}},type="resource"}) do
    if r.name==resource then richness=richness+r.amount;tiles=tiles+1 else mixed=true end
   end
   local score=(old_receiver and 1000000000 or 0)+math.min(tiles,4)*1000000+math.min(richness,1000)-(x*x+y*y)
   if not mixed and richness>0 and score>best_score then
    best_score=score;best={ok=true,complete=false,drill={name="burner-mining-drill",position=p,direction=0},
      receiver={name=receiver_name,position=receiver}}
   end
  end
 end
end
return best or {ok=false,reason="no_clear_direct_mining_cell_site"}
"""
        )

    def _ensure_cell(self, observation: dict[str, Any], resource: str, receiver_name: str) -> dict[str, Any] | None:
        cell = self.discover_cell(resource, receiver_name, observation=observation)
        if not cell.get("ok"):
            return _report("blocked", cell.get("reason", "cell_site_query_failed"), resource=resource)
        if cell.get("complete"):
            return None
        for part in [cell["receiver"], cell["drill"]]:
            if any(_at(entity, part["name"], part["position"]) for entity in observation.get("entities", [])):
                continue
            if self._count(observation, part["name"]) < 1:
                return self.ensure_item(observation, part["name"], 1)
            return {
                "type": "build",
                **part,
                "direction": part.get("direction", 0),
                "reason": f"build direct {resource} supply cell",
            }
        return _report("waiting", "waiting for direct cell drop-point verification", resource=resource)
