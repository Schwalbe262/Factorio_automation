"""Produced rows as construction stock sources (hook for ``V2Bootstrap.ensure_item``).

``MachineStock(rows)(observation, item)`` describes the machine source of
``item`` or returns None when there is none (no ``produce`` row for it):

* ``status`` "producing": the row is verified, its stock chest and every
  machine are on the map, and its machines finished at least one product
  within the last ``STALL_TICKS`` game ticks (or the chest holds stock);
* "building": a row exists but has not been verified yet (it may itself be
  waiting for this item, so it never justifies waiting);
* "stalled": verified, but no machine progress for ``STALL_TICKS`` and an
  empty chest (starved input, lost power): callers fall back to hand crafting;
* "broken": verified, but the chest or a machine is missing from the map.

A belt-output row (``produce`` with ``output: belt``) has no chest: None.

The chest is published on the PCB board as the row's ``output`` port, so the
ordinary batched ``take_many`` collection (``construction_supply``) is
allowed to take the product from it; no new transfer action is needed.
"""

from __future__ import annotations

from .transport import xy

STALL_TICKS = 3600  # one game minute without a finished product


class MachineStock:
    def __init__(self, rows):
        self.rows = rows
        self._progress: dict[str, tuple[float, int]] = {}

    def __call__(self, observation: dict, item: str) -> dict | None:
        if not self.rows.path(item).exists():
            return None
        state = self.rows.state(item)
        if state.get("world_id") != observation.get("world_id"):
            return None
        if (state.get("line") or {}).get("tail", {}).get("mode") == "belt":
            return None  # a belt-output row feeds other rows; it holds no stock (production_layout.belt_tail)
        if state.get("phase") != "verified":
            return {"status": "building", "item": item}
        actual = {(e.get("name"), *xy(e)): e for e in observation.get("entities", []) if "position" in e}
        line = state["line"]
        chest_spec = line["tail"]["chest"]
        chest = actual.get((chest_spec["name"], *xy(chest_spec)))
        machines = [actual.get((e["name"], *xy(e))) for e in line["main"]["entities"] if e.get("recipe")]
        if chest is None or any(m is None for m in machines):
            return {"status": "broken", "item": item, "reason": "row chest or machine missing"}
        stock = int((chest.get("inventory") or {}).get(item, 0))
        finished = sum(int(m.get("products_finished") or 0) for m in machines)
        tick = int(observation.get("tick") or 0)
        seen = self._progress.get(item)
        if seen is None or finished > seen[0]:
            self._progress[item] = (finished, tick)
            seen = self._progress[item]
        idle = tick - seen[1]
        status = "producing" if stock > 0 or idle <= STALL_TICKS else "stalled"
        return {
            "status": status,
            "item": item,
            "chest": {"name": chest_spec["name"], "position": dict(chest_spec["position"])},
            "stock": stock,
            "products_finished": finished,
            "ticks_without_progress": idle,
        }
