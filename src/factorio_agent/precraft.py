"""Pre-craft the starter science packs while other work waits.

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
"""

from __future__ import annotations

from collections.abc import Mapping

PACK = "automation-science-pack"
STARTER_TECHNOLOGY = "automation"
PRECRAFT_BATCH = 2  # 2 packs = 10 s of hand crafting ahead of anything queued after them


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
