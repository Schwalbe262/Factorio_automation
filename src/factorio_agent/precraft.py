"""Pre-craft hand items while other work waits: starter science, and the ingredients of research-locked plan items.

User rule: science packs take time to make, so make them before the lab
exists instead of after; items construction needs must still get through the
hand-craft queue quickly.

Rule (:func:`starter_science`):

* only before any lab exists (afterwards ``research.StarterResearch`` owns the
  packs and their accounting) and before the starter technology is researched;
* only when the native crafting queue is empty, in batches of
  ``PRECRAFT_BATCH`` packs, so a construction item queued next waits for at
  most one small batch (the native queue has no priorities);
* only up to the technology's pack count, counting packs already carried;
* only when the plates for the batch are in the inventory (a craft that cannot
  start would count as a failed action).

Rule (:func:`research_deferred`, called by ``FactoryBuilder.ensure_plan`` through
``early_start.Deferral.precraft`` when a declared block, ``"precraft": true`` in its
early start, is parked: everything else of its plan stands and the rest waits only
for coming research). The splitter and underground-belt recipes unlock with
logistics, but their ingredients (electronic circuits, belts) do not: crafting
those while logistics runs leaves only the final assembly after it
(aud-1114-0930: splitters and undergrounds were the last district entities in
every run, 0.3-5.5 min after logistics).

* only ingredients whose recipe is enabled now and that no machine row stocks
  (a supply cell product or a ``produce`` row: hand crafting stops there,
  ``HANDCRAFT_LIMIT``); never past what the deferred plan items still need,
  counting carried items and ingredients;
* only when the native crafting queue is empty, ``INTERMEDIATE_BATCH`` items at a time;
* only when every raw plate of the batch is carried and no other active job waits
  for it (``early_start.contested_plates``); a slack job's craft that spends an item
  the critical path claims is refused as before (``critical_path.slack_refusal``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math

PACK = "automation-science-pack"
STARTER_TECHNOLOGY = "automation"
PRECRAFT_BATCH = 2  # 2 packs = 10 s of hand crafting ahead of anything queued after them
# Ingredients of research-locked plan items per craft action: one splitter's circuits (about 5 s).
INTERMEDIATE_BATCH = 5


def _pack_plates(catalog) -> dict[str, float]:
    """Raw plates one pack costs, by recursive expansion of the catalog recipes."""
    from .progression import raw_cost

    return raw_cost(catalog, {PACK: 1})


def starter_science(catalog, observation: Mapping) -> dict | None:
    """A ``craft`` action for the next small batch of starter science packs, or None."""
    if not (observation.get("enabled_recipes") or {}).get(PACK):
        return None
    if (observation.get("technologies") or {}).get(STARTER_TECHNOLOGY):
        return None
    if observation.get("crafting_queue"):
        return None
    inventory = observation.get("inventory") or {}
    if inventory.get("lab") or any(e.get("name") == "lab" for e in observation.get("entities", [])):
        return None
    technology = (getattr(catalog, "technologies", None) or {}).get(STARTER_TECHNOLOGY) or {}
    units = technology.get("unit_count")
    if not isinstance(units, (int, float)) or units <= 0:
        return None
    missing = int(units) - int(inventory.get(PACK, 0))
    if missing <= 0:
        return None
    count = min(PRECRAFT_BATCH, missing)
    plates = _pack_plates(catalog)
    if not plates or any(inventory.get(item, 0) < amount * count for item, amount in plates.items()):
        return None
    return {
        "type": "craft",
        "recipe": PACK,
        "count": count,
        "reason": "pre-craft starter science before the lab exists",
    }


def _enabled(observation: Mapping) -> set[str]:
    value = observation.get("enabled_recipes")
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {name for name in value if isinstance(name, str)}
    return set()


def _item_ingredients(recipe: Mapping) -> list[tuple[str, float]]:
    rows = []
    for row in recipe.get("ingredients") or ():
        if not isinstance(row, Mapping) or row.get("type", "item") != "item" or not isinstance(row.get("name"), str):
            continue
        amount = row.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            rows.append((row["name"], float(amount)))
    return rows


def research_deferred(
    catalog,
    observation: Mapping,
    needs: Mapping[str, int],
    *,
    contested: Iterable[str] = (),
    stocked: Iterable[str] = (),
) -> dict | None:
    """A ``craft`` action for an enabled ingredient of plan items that wait for research, or None.

    ``needs``: unbuilt plan entities per item whose recipe waits for coming research.
    """
    from .progression import RAW_ITEMS, _product_amount, raw_cost

    if not needs or observation.get("crafting_queue"):
        return None
    enabled = _enabled(observation)
    inventory = observation.get("inventory") or {}
    contested, stocked = set(contested), set(stocked)
    wanted: dict[str, float] = {}
    for item, count in sorted(needs.items()):
        recipe = catalog.recipe_for_product(item) if hasattr(catalog, "recipe_for_product") else None
        if not isinstance(recipe, Mapping):
            continue
        crafts = math.ceil(max(0, count - int(inventory.get(item, 0))) / _product_amount(recipe, item))
        for name, amount in _item_ingredients(recipe):
            wanted[name] = wanted.get(name, 0.0) + crafts * amount
    for name in sorted(wanted):
        if name in RAW_ITEMS or name in stocked:
            continue
        missing = wanted[name] - int(inventory.get(name, 0))
        recipe = catalog.recipe_for_product(name)
        if missing <= 0 or not isinstance(recipe, Mapping) or recipe.get("name", name) not in enabled:
            continue
        per = _product_amount(recipe, name)
        crafts = max(1, min(math.ceil(missing / per), int(INTERMEDIATE_BATCH / per)))
        plates = raw_cost(catalog, {name: crafts * per})
        if not plates or set(plates) & contested:
            continue
        if any(inventory.get(item, 0) < math.ceil(amount - 1e-9) for item, amount in plates.items()):
            continue
        items = ",".join(sorted(needs))
        return {
            "type": "craft",
            "recipe": recipe.get("name", name),
            "count": crafts,
            "reason": f"pre-craft {name} for {items} while their research runs"[:300],
        }
    return None
