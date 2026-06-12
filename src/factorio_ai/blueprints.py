from __future__ import annotations

import base64
import binascii
from collections import Counter
from dataclasses import dataclass
import json
import zlib
from typing import Any, Iterable


class BlueprintDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class BlueprintSummary:
    label: str
    entity_counts: dict[str, int]
    tile_counts: dict[str, int]

    @property
    def entity_total(self) -> int:
        return sum(self.entity_counts.values())


@dataclass(frozen=True)
class BlueprintLesson:
    label: str
    inferred_purpose: str
    design_principles: list[str]
    bottlenecks: list[str]
    entity_counts: dict[str, int]
    ratios: dict[str, float]

    def to_training_example(self, objective: str) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a Factorio strategy model. Explain what a blueprint is for, "
                        "which bottlenecks it solves, and when a skill planner should use it."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "objective": objective,
                            "blueprint": {
                                "label": self.label,
                                "entity_counts": self.entity_counts,
                                "ratios": self.ratios,
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "inferred_purpose": self.inferred_purpose,
                            "design_principles": self.design_principles,
                            "bottlenecks": self.bottlenecks,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
        }


def decode_blueprint_string(text: str) -> dict[str, Any]:
    """Decode a Factorio blueprint exchange string or raw blueprint JSON."""

    payload = text.strip()
    if not payload:
        raise BlueprintDecodeError("blueprint string is empty")

    if payload.startswith("{"):
        return _parse_json(payload)

    if not payload.startswith("0"):
        raise BlueprintDecodeError("blueprint exchange string must start with version byte '0'")

    try:
        compressed = base64.b64decode(payload[1:], validate=True)
        decoded = zlib.decompress(compressed).decode("utf-8")
    except (binascii.Error, zlib.error, UnicodeDecodeError) as exc:
        raise BlueprintDecodeError(f"invalid blueprint exchange string: {exc}") from exc
    return _parse_json(decoded)


def encode_blueprint_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "0" + base64.b64encode(zlib.compress(raw)).decode("ascii")


def iter_blueprints(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if isinstance(payload.get("blueprint"), dict):
        yield payload["blueprint"]
        return

    book = payload.get("blueprint_book")
    if not isinstance(book, dict):
        return
    entries = book.get("blueprints")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("blueprint"), dict):
            yield entry["blueprint"]


def summarize_blueprint_payload(payload: dict[str, Any]) -> list[BlueprintSummary]:
    return [summarize_blueprint(blueprint) for blueprint in iter_blueprints(payload)]


def infer_blueprint_lessons(payload: dict[str, Any]) -> list[BlueprintLesson]:
    return [infer_blueprint_lesson(summary) for summary in summarize_blueprint_payload(payload)]


def infer_blueprint_lesson(summary: BlueprintSummary) -> BlueprintLesson:
    counts = summary.entity_counts
    purpose = "general factory block"
    principles: list[str] = []
    bottlenecks: list[str] = []

    furnaces = _count_any(counts, ["stone-furnace", "steel-furnace", "electric-furnace"])
    miners = _count_any(counts, ["burner-mining-drill", "electric-mining-drill"])
    assemblers = _count_any(counts, ["assembling-machine-1", "assembling-machine-2", "assembling-machine-3"])
    labs = counts.get("lab", 0)
    refineries = counts.get("oil-refinery", 0)
    rocket_silos = counts.get("rocket-silo", 0)
    belts = _count_any(counts, ["transport-belt", "fast-transport-belt", "express-transport-belt"])
    inserters = _count_any(counts, ["burner-inserter", "inserter", "fast-inserter", "stack-inserter"])
    poles = _count_any(counts, ["small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation"])

    if rocket_silos:
        purpose = "rocket launch production endpoint"
        principles.extend(["converge rocket parts into a silo", "reserve high-throughput inputs for late-game demand"])
        bottlenecks.extend(["low density structures", "rocket fuel", "processing units"])
    elif refineries:
        purpose = "oil processing block"
        principles.extend(["separate fluid processing from belt logistics", "balance cracking and petroleum demand"])
        bottlenecks.extend(["petroleum gas", "water supply", "pipe throughput"])
    elif labs:
        purpose = "research block"
        principles.extend(["feed science packs into labs consistently", "scale lab count with science throughput"])
        bottlenecks.extend(["science pack supply", "electric power"])
    elif furnaces:
        purpose = "smelting block"
        principles.extend(["convert ore throughput into plate throughput", "pair fuel or power with furnace rows"])
        bottlenecks.extend(["ore input", "fuel or power", "plate output belts"])
    elif miners:
        purpose = "mining outpost"
        principles.extend(["cover resource patches with miners", "route output onto belts or into furnaces"])
        bottlenecks.extend(["resource patch coverage", "fuel or electric power"])
    elif assemblers:
        purpose = "assembly block"
        principles.extend(["combine ingredient belts with inserter access", "scale assemblers by recipe ratio"])
        bottlenecks.extend(["input ingredient throughput", "output belt capacity", "crafting speed"])

    if belts:
        principles.append("use belts to create repeatable throughput lanes")
    if inserters:
        principles.append("use inserters as the boundary between logistics and machines")
    if poles:
        principles.append("include power coverage as part of the build footprint")

    ratios = _ratios(
        {
            "belts_per_machine": belts / max(1, furnaces + assemblers + labs + miners),
            "inserters_per_machine": inserters / max(1, furnaces + assemblers + labs + miners),
            "poles_per_machine": poles / max(1, furnaces + assemblers + labs + miners),
        }
    )

    return BlueprintLesson(
        label=summary.label,
        inferred_purpose=purpose,
        design_principles=principles,
        bottlenecks=bottlenecks,
        entity_counts=summary.entity_counts,
        ratios=ratios,
    )


def training_examples_from_blueprint(payload: dict[str, Any], objective: str) -> list[dict[str, Any]]:
    return [lesson.to_training_example(objective) for lesson in infer_blueprint_lessons(payload)]


def summarize_blueprint(blueprint: dict[str, Any]) -> BlueprintSummary:
    entities = blueprint.get("entities")
    tiles = blueprint.get("tiles")
    entity_counts: Counter[str] = Counter()
    tile_counts: Counter[str] = Counter()

    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict) and isinstance(entity.get("name"), str):
                entity_counts[entity["name"]] += 1

    if isinstance(tiles, list):
        for tile in tiles:
            if isinstance(tile, dict) and isinstance(tile.get("name"), str):
                tile_counts[tile["name"]] += 1

    return BlueprintSummary(
        label=str(blueprint.get("label") or ""),
        entity_counts=dict(entity_counts),
        tile_counts=dict(tile_counts),
    )


def _parse_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlueprintDecodeError(f"invalid blueprint JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BlueprintDecodeError("blueprint payload must be a JSON object")
    return parsed


def _count_any(counts: dict[str, int], names: list[str]) -> int:
    return sum(int(counts.get(name) or 0) for name in names)


def _ratios(values: dict[str, float]) -> dict[str, float]:
    return {key: round(float(value), 3) for key, value in values.items()}
