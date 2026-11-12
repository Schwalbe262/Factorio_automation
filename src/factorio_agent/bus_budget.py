"""Account for additive consumer demand without changing pinned bus ports."""

import math


def declared_bus_demand(board, item):
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("bus demand must be finite and nonnegative")
        return value

    base = sum(
        number(q.get("demand_per_minute", 0))
        for q in board["shared_main_bus"]["ports"]
        if q.get("item") == item and q.get("priority")
    )
    rows = board.get("consumer_capacity_allocations", [])
    if not isinstance(rows, list):
        raise ValueError("additive bus demand requires an allocation list")
    seen = set()
    last = {}
    extra = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid additive bus allocation")
        receipt = row.get("receipt_id")
        consumer = row.get("consumer")
        material = row.get("item")
        if (
            not isinstance(receipt, str)
            or not receipt
            or not isinstance(consumer, str)
            or not consumer
            or not isinstance(material, str)
            or not material
        ):
            raise ValueError("additive bus allocation identity is missing or duplicated")
        allocation = (receipt, consumer, material)
        if allocation in seen:
            raise ValueError("additive bus allocation identity is missing or duplicated")
        seen.add(allocation)
        old = number(row.get("old_per_minute"))
        new = number(row.get("new_per_minute"))
        delta = number(row.get("delta_per_minute"))
        key = (consumer, material)
        if not math.isclose(new - old, delta, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("additive bus allocation delta does not match its old/new demand")
        if key in last and not math.isclose(last[key], old, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("additive bus allocation ancestry is discontinuous")
        last[key] = new
        if material == item:
            extra += delta
    return base + extra
