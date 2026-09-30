"""Comparable construction metrics; entity counts are not material prices."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .world_catalog import WorldCatalog


def layout_metrics(entities: list[dict], catalog: WorldCatalog) -> dict[str, Any]:
    unique = {}
    for entity in entities:
        p = entity["position"]
        identity = (entity["name"], p["x"], p["y"], entity.get("direction", 0),
                    entity.get("belt_to_ground_type"))
        unique[identity] = entity
    items = Counter()
    transport = Counter()
    rectangles = []
    for entity in unique.values():
        name, p = entity["name"], entity["position"]
        prototype = catalog.entities.get(name, {})
        placement = prototype.get("items_to_place_this", [])
        item = entity.get("item") or (placement[0]["name"] if placement else name)
        items[item] += 1
        if name.endswith(("transport-belt", "underground-belt", "splitter")) or name in {"pipe", "pipe-to-ground"}:
            transport[item] += 1
        width, height = prototype.get("tile_width", 1), prototype.get("tile_height", 1)
        if entity.get("direction", 0) in (4, 12):
            width, height = height, width
        rectangles.append((p["x"] - width / 2, p["y"] - height / 2,
                           p["x"] + width / 2, p["y"] + height / 2))

    def price(counts):
        if not counts:
            return {"raw_materials": [], "raw_item_units": 0}
        bom = catalog.bill_of_materials(dict(counts))
        raw = bom["raw_materials"]
        return {"raw_materials": raw,
                "raw_item_units": sum(row["amount"] for row in raw if row["type"] == "item")}

    area = 0
    if rectangles:
        area = ((max(r[2] for r in rectangles) - min(r[0] for r in rectangles)) *
                (max(r[3] for r in rectangles) - min(r[1] for r in rectangles)))
    return {"entities": len(unique), "area": area, "construction_items": dict(sorted(items.items())),
            "transport_items": dict(sorted(transport.items())),
            "construction": price(items), "transport": price(transport),
            "cost_basis": "live recipes, integer batches, normal quality; operating fuel excluded"}


def checkpoint_metrics(state: dict, catalog: WorldCatalog) -> dict:
    if state.get("catalog_fingerprint") not in (None, catalog.fingerprint):
        raise ValueError("checkpoint and benchmark catalog fingerprints differ")
    links = state.get("links", {})
    rows = []
    assembly = []
    for key, plan in links.items():
        entities = plan.get("entities", [])
        if key.startswith(("recipe:", "lab:")):
            assembly.extend(entities)
        source = plan.get("source_port", {}).get("position")
        consumer = plan.get("consumer_port", {}).get("position")
        distance = (abs(source["x"] - consumer["x"]) + abs(source["y"] - consumer["y"])) if source and consumer else None
        rows.append({"key": key, "entities": len(entities), "straight_distance": distance})
    all_entities = [entity for category in ("blocks", "links", "power_links")
                    for plan in state.get(category, {}).values() for entity in plan.get("entities", [])]
    return {"world_id": state.get("world_id"), "catalog_fingerprint": catalog.fingerprint,
            "assembly_transport": layout_metrics(assembly, catalog),
            "factory": layout_metrics(all_entities, catalog),
            "longest_links": sorted(rows, key=lambda row: (-row["entities"], row["key"]))[:10],
            "sustained_flow_proved": False}
