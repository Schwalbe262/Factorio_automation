"""Carried surplus: bulk fuel and ore the actor holds beyond known demand goes where it is used.

Live map eval-2874201674: coal starter retirement emptied every retiring coal
chest into the character (2643 coal in 56 takes) while ~1800 coal was ever
inserted as fuel; the full inventory then blocked construction pickups.
Rules (PLANNERS.md "Carried surplus"):

1. ``allowance(item)``: what the actor reasonably carries, from the trips the
   planners already ask for. Fuel: one hand-fed boiler fill
   (``power_coal_feed.boiler_sizing`` target) plus one burner fetch
   (``construction_supply.FUEL_FETCH``). Ore: one haul (``PLATE_BATCH``).
   Carried beyond it is surplus.
2. ``CarriedSurplus.step`` (starter upkeep, idle pass): once the surplus of an
   item reaches ``SURPLUS_STACKS`` stacks (catalog stack size), one paid
   action moves it, in this order:
   a. ``insert`` into a live consumer with room, up to its slot: fuel into
      boilers, then burner drills, then stone/steel furnaces that are smelting;
      ore into the source slot of a fuelled furnace smelting the same ore.
      Retiring starter cells (``StarterRetirement.cell_keys``) and stuck
      entities (no ore, output blocked) are never consumers.
   b. ``deposit`` into a depot: a paid storage chest registered by
      ``inventory_storage.InventoryStorage`` (``inventory-storage.json``).
      Fuel fetches take from a depot before any other chest (``depot_take``).
3. Sources respect the allowance: starter retirement takes at most
   ``trip_room`` (allowance + what consumers and depots accept - carried) and
   puts any excess into a consumer or depot first (``starter_retirement._collect``).

Wood (owner, 2026-11-13: wood is burned in the power plant) has no carry
allowance: all carried wood is surplus, boilers first. A base boiler has one
fuel slot, so wood goes directly only into a boiler whose slot is empty or
holds wood; a hand-fed boiler swaps its coal for wood
(``power_coal_feed.boiler_fuel_action``, which holds ``reserve`` wood for it);
a belt-fed plant takes wood through the wood feed chest
(``wood_burner_feed``), the next sink after the boilers. Burner drills and
furnaces follow, then depots. Wood is moved only once more than one stack is
carried (owner, 2026-11-13: not as it comes in), so a clearing batch is not
interrupted for every tree.

Plates are construction stock and stay with ``inventory_storage.KEEP``.
"""

from __future__ import annotations

import math

SURPLUS_STACKS = 1  # dispose once the surplus fills this many stacks
FUELS = ("coal", "wood")
ORE_PLATE = {"iron-ore": "iron-plate", "copper-ore": "copper-plate"}
ITEMS = FUELS + tuple(ORE_PLATE)
FURNACES = ("stone-furnace", "steel-furnace")
FUEL_CONSUMERS = {"boiler": 0, "burner-mining-drill": 1, "stone-furnace": 2, "steel-furnace": 2}  # name -> rank
STUCK = frozenset({"no_minable_resources", "waiting_for_space_in_destination", "full_output", "no_input_fluid"})
CHEST_SLOTS = {"wooden-chest": 16, "iron-chest": 32, "steel-chest": 48}  # base game container sizes
DEPOSIT_LIMIT = 1000  # storage_transfer.validate_deposit envelope
DEFAULT_STACK = 50
WOOD = "wood"
MIN_GIVE = 10  # a consumer takes surplus only with room for at least this many (owner: give 10 or a stack, not 1)


def stack_size(catalog, item) -> int:
    size = ((getattr(catalog, "items", None) or {}).get(item) or {}).get("stack_size")
    return int(size) if isinstance(size, int) and size > 0 else DEFAULT_STACK


def allowance(item, catalog) -> int | None:
    """Items of ``item`` the actor may carry for known demand (module rule 1); None for other items."""
    if item == WOOD:
        return 0  # wood is burned, never carried for later (module doc)
    if item in FUELS:
        from .construction_supply import FUEL_FETCH
        from .power_coal_feed import boiler_sizing

        return int(boiler_sizing(catalog)["target"]) + FUEL_FETCH
    if item in ORE_PLATE:
        from .construction_supply import PLATE_BATCH

        return PLATE_BATCH
    return None


def threshold(item, catalog) -> int:
    if item == WOOD:
        # Owner, 2026-11-13: wood is burned only once more than one stack is carried, not as it
        # comes in; the whole carried amount then goes in one trip.
        return stack_size(catalog, WOOD) + 1
    return SURPLUS_STACKS * stack_size(catalog, item)


def carried(obs, item) -> int:
    return int((obs.get("inventory") or {}).get(item, 0))


def _key(entity):
    p = entity.get("position") or {}
    return (entity.get("name"), p.get("x"), p.get("y"))


def _fuelled(entity) -> bool:
    held = entity.get("inventory") or {}
    return sum(int(held.get(f, 0)) for f in FUELS) > 0 or float(entity.get("remaining_burning_fuel") or 0) > 0


def _smelting(entity) -> bool:
    held = entity.get("inventory") or {}
    return entity.get("status_name") == "working" or any(held.get(o, 0) for o in (*ORE_PLATE, "stone"))


def _distance(obs, entity) -> float:
    a, p = obs.get("position") or {"x": 0, "y": 0}, entity.get("position") or {}
    return math.hypot(p.get("x", 0) - a.get("x", 0), p.get("y", 0) - a.get("y", 0))


def consumers(obs, item, catalog, excluded=frozenset()) -> list[dict]:
    """Live consumers of ``item`` with room, best first: ``{"entity", "inventory", "room"}``."""
    stack = stack_size(catalog, item)
    rows = []
    for e in obs.get("entities", []):
        name = e.get("name")
        if type(e.get("unit_number")) is not int or _key(e) in excluded or e.get("status_name") in STUCK:
            continue
        held = e.get("inventory") or {}
        if item in FUELS:
            if name not in FUEL_CONSUMERS or (name in FURNACES and not _smelting(e)):
                continue
            if any(held.get(f, 0) for f in FUELS if f != item):
                continue  # one fuel slot holds one fuel kind
            rank, inventory, room = FUEL_CONSUMERS[name], "fuel", stack - int(held.get(item, 0))
        elif item in ORE_PLATE:
            if name not in FURNACES or not _fuelled(e):
                continue
            if any(held.get(o, 0) for o in (*ORE_PLATE, "stone") if o != item):
                continue  # smelting something else
            if any(n and k != ORE_PLATE[item] for k, n in (e.get("output_inventory") or {}).items()):
                continue  # another product would block the output
            rank, inventory, room = 0, "furnace_source", stack - int(held.get(item, 0))
        else:
            continue
        if room >= min(MIN_GIVE, stack):  # never trickle 1-2 items into a nearly full slot
            rows.append(
                (rank, _distance(obs, e), e["unit_number"], {"entity": e, "inventory": inventory, "room": room})
            )
    rows.sort(key=lambda r: r[:3])
    return [r[3] for r in rows]


def chest_room(chest, item, catalog) -> int:
    """Free room for ``item`` in an observed chest (other items counted in full stacks of their own size)."""
    slots = CHEST_SLOTS.get(chest.get("name"), 0)
    held = chest.get("inventory") or {}
    used = sum(math.ceil(n / stack_size(catalog, k)) for k, n in held.items() if k != item and n > 0)
    return max(0, (slots - used) * stack_size(catalog, item) - int(held.get(item, 0)))


class CarriedSurplus:
    def __init__(self, catalog, storage=None, retirement=None, wood_feed=None, reserve=None):
        self.catalog, self.storage, self.retirement = catalog, storage, retirement
        self.wood_feed = wood_feed  # wood_burner_feed.WoodBurnerFeed: its chest feeds the boilers
        self.reserve = reserve  # reserve(obs, item) -> carried items held for a hand-fed boiler

    def held_back(self, obs, item) -> int:
        return max(0, int(self.reserve(obs, item) or 0)) if callable(self.reserve) else 0

    def wood_chest(self, obs, item) -> dict | None:
        """The wood feed chest when ``item`` is wood and it stands, else None."""
        if item != WOOD or self.wood_feed is None:
            return None
        return self.wood_feed.chest(obs)

    def _excluded(self, obs):
        return self.retirement.cell_keys(obs) if self.retirement is not None else set()

    def depots(self, obs) -> list[dict]:
        """Registered paid storage chests standing in this world, nearest first."""
        state = getattr(self.storage, "state", None) or {}
        if state.get("world_id") != obs.get("world_id"):
            return []
        wanted = {(c.get("unit_number"), c.get("name")) for c in state.get("chests", [])}
        found = [e for e in obs.get("entities", []) if (e.get("unit_number"), e.get("name")) in wanted]
        return sorted(found, key=lambda e: (_distance(obs, e), e["unit_number"]))

    def sink_room(self, obs, item) -> int:
        """Items of ``item`` that consumers and depots accept now."""
        room = sum(r["room"] for r in consumers(obs, item, self.catalog, self._excluded(obs)))
        feed = self.wood_chest(obs, item)
        room += chest_room(feed, item, self.catalog) if feed else 0
        return room + sum(chest_room(c, item, self.catalog) for c in self.depots(obs))

    def trip_room(self, obs, item) -> int:
        """Items of ``item`` the actor may still take from a source: allowance + sink room - carried."""
        allowed = allowance(item, self.catalog)
        if allowed is None:
            return 0
        return max(0, allowed + self.sink_room(obs, item) - carried(obs, item))

    def sink_action(self, obs, item, amount, reason) -> dict | None:
        """One paid move of up to ``amount`` carried ``item``: consumer first (rule 2a), else a depot (2b).

        Wood: boilers, then the wood feed chest, then the other consumers.
        """
        amount = min(int(amount), carried(obs, item))
        if amount <= 0:
            return None
        rows = consumers(obs, item, self.catalog, self._excluded(obs))
        feed = self.wood_chest(obs, item)
        if feed is not None and chest_room(feed, item, self.catalog) > 0:
            boilers = [r for r in rows if r["entity"].get("name") == "boiler"]
            rows = boilers + [{"chest": feed}] + [r for r in rows if r not in boilers]
        for row in rows:
            if "chest" in row:
                return self._deposit(obs, row["chest"], item, amount, reason + " (wood feed)")
            e = row["entity"]
            return {
                "type": "insert",
                "name": e["name"],
                "position": dict(e["position"]),
                "item": item,
                "count": min(amount, row["room"]),
                "inventory": row["inventory"],
                "reason": reason,
            }
        for chest in self.depots(obs):
            action = self._deposit(obs, chest, item, amount, reason + " (depot)")
            if action is not None:
                return action
        return None

    def _deposit(self, obs, chest, item, amount, reason) -> dict | None:
        room = min(amount, chest_room(chest, item, self.catalog), DEPOSIT_LIMIT)
        if room <= 0:
            return None
        return {
            "type": "deposit",
            "name": chest["name"],
            "position": dict(chest["position"]),
            "item": item,
            "count": int(room),
            "expected_entity_unit": chest["unit_number"],
            "expected_entity_world_id": obs["world_id"],
            "reason": reason,
        }

    def step(self, obs, *, idle=False) -> dict | None:
        """Upkeep: one disposal action for the first item whose surplus reaches the threshold, else None.

        ``idle``: a pass in which no job acted (kept for callers; wood moves only
        above one carried stack in every pass, see ``threshold``).
        """
        if not obs.get("world_id"):
            return None
        for item in ITEMS:
            allowed = allowance(item, self.catalog) + self.held_back(obs, item)
            excess = carried(obs, item) - allowed
            if excess >= threshold(item, self.catalog):
                action = self.sink_action(obs, item, excess, f"put carried surplus {item} where it is used")
                if action is not None:
                    return action
        return None

    def depot_take(self, obs, item, count) -> dict | None:
        """A fetch of ``item`` from a depot that holds it (fetched before any other chest), else None."""
        if item not in ITEMS or type(count) is not int or count < 1:
            return None
        for chest in self.depots(obs):
            stock = int((chest.get("inventory") or {}).get(item, 0))
            if stock > 0:
                return {
                    "type": "take",
                    "name": chest["name"],
                    "position": dict(chest["position"]),
                    "item": item,
                    "count": min(count, stock, 50),
                    "reason": f"collect depot {item}",
                }
        return None
