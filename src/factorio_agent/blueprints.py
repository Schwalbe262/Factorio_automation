"""Bounded reader for Factorio blueprint exchange strings (analysis only)."""
from __future__ import annotations

import base64
import binascii
from collections import Counter
import json
import zlib
from typing import Any, Iterable, Mapping

MAX_COMPRESSED_BYTES = 2_000_000
MAX_DECOMPRESSED_BYTES = 8_000_000
MAX_ENTITIES = 20_000
MAX_BOOK_DEPTH = 12


class BlueprintDecodeError(ValueError):
    """Invalid or oversized blueprint exchange payload."""


def decode_blueprint_string(text: str) -> dict[str, Any]:
    """Decode a version-0 Factorio exchange string under strict resource limits."""
    if not isinstance(text, str) or not text:
        raise BlueprintDecodeError("blueprint string is empty or not text")
    payload = text.strip()
    if not payload.startswith("0"):
        raise BlueprintDecodeError("expected blueprint exchange version prefix '0'")
    encoded = payload[1:]
    if len(encoded) > ((MAX_COMPRESSED_BYTES + 2) // 3) * 4:
        raise BlueprintDecodeError("compressed blueprint exceeds size limit")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BlueprintDecodeError("invalid base64 payload") from exc
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise BlueprintDecodeError("compressed blueprint exceeds size limit")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES or inflater.unconsumed_tail:
            raise BlueprintDecodeError("decompressed blueprint exceeds size limit")
        raw += inflater.flush(MAX_DECOMPRESSED_BYTES + 1 - len(raw))
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise BlueprintDecodeError("decompressed blueprint exceeds size limit")
        if not inflater.eof or inflater.unused_data:
            raise BlueprintDecodeError("truncated or trailing compressed data")
        value = json.loads(raw.decode("utf-8"))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BlueprintDecodeError("malformed compressed JSON payload") from exc
    if not isinstance(value, dict):
        raise BlueprintDecodeError("blueprint root must be a JSON object")
    if not (isinstance(value.get("blueprint"),dict) or isinstance(value.get("blueprint_book"),dict)):
        raise BlueprintDecodeError("expected a blueprint or blueprint book root")
    # Validate all nested blueprint entity lists before returning the payload.
    list(iter_blueprints(value))
    return value


def iter_blueprints(payload: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield blueprint leaves in a blueprint/book tree, bounded by depth and count."""
    total = 0

    def walk(node: Any, depth: int) -> Iterable[dict[str, Any]]:
        nonlocal total
        if depth > MAX_BOOK_DEPTH:
            raise BlueprintDecodeError("blueprint book nesting exceeds limit")
        if not isinstance(node, dict):
            return
        bp = node.get("blueprint")
        if isinstance(bp, dict):
            entities = bp.get("entities", [])
            if not isinstance(entities, list):
                raise BlueprintDecodeError("blueprint entities must be a list")
            total += len(entities)
            if total > MAX_ENTITIES:
                raise BlueprintDecodeError("blueprint entity count exceeds limit")
            yield bp
            return
        book = node.get("blueprint_book")
        if isinstance(book, dict):
            entries = book.get("blueprints", [])
            if not isinstance(entries, list):
                raise BlueprintDecodeError("blueprint book entries must be a list")
            for entry in entries:
                yield from walk(entry, depth + 1)

    yield from walk(dict(payload), 0)


def summarize_blueprint(payload: Mapping[str, Any], catalog: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return structural counts and optional catalog resolution; never asserts functionality."""
    result = []
    entities_seen = 0
    catalog_entities = catalog.get("entities", {}) if isinstance(catalog, Mapping) else {}
    for bp in iter_blueprints(payload):
        counts: Counter[str] = Counter()
        for entity in bp.get("entities", []):
            if not isinstance(entity, dict) or not isinstance(entity.get("name"), str):
                raise BlueprintDecodeError("entity must be an object with a name")
            counts[entity["name"]] += 1
            entities_seen += 1
            if entities_seen > MAX_ENTITIES:
                raise BlueprintDecodeError("blueprint entity count exceeds limit")
        names = sorted(counts)
        result.append({
            "label": bp.get("label", ""),
            "entity_total": sum(counts.values()),
            "entity_counts": dict(sorted(counts.items())),
            "catalog_unknown_entities": [name for name in names if name not in catalog_entities],
            "verification": "UNVERIFIED_STRUCTURE_ONLY",
        })
    return result
