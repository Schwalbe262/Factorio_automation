from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from pathlib import Path
from typing import Any


WORLD_MAP_MEMORY_FILE = "world-map-memory.json"
WORLD_MAP_MEMORY_SCHEMA_VERSION = 1
PLANNING_SITE_KEYS = ("power_sites", "lab_sites", "automation_sites")
DEFAULT_WORLD_MEMORY_MAX_AGE_SECONDS = 180.0
SPATIAL_INDEX_CELL_SIZE = 64
RESOURCE_CLUSTER_DISTANCE = 14.0
FACTORY_ZONE_DISTANCE = 12.0
MAX_FEATURES_PER_KIND = 40


def world_map_memory_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / WORLD_MAP_MEMORY_FILE


def load_world_map_memory(runtime_dir: Path) -> dict[str, Any]:
    path = world_map_memory_path(runtime_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_world_map_memory(
    runtime_dir: Path,
    observation: dict[str, Any],
    *,
    include_planning_sites: bool,
    source: str,
) -> dict[str, Any]:
    memory = load_world_map_memory(runtime_dir)
    now = time.time()
    now_text = datetime.now(timezone.utc).isoformat()
    memory.update(
        {
            "schema_version": WORLD_MAP_MEMORY_SCHEMA_VERSION,
            "encoding": "sparse_feature_graph",
            "note": "Stores resource clusters, water access anchors, factory zones, and a sparse feature index; it does not store per-tile map cells.",
            "updated_at": now_text,
            "updated_at_epoch": now,
            "source": source,
            "tick": observation.get("tick"),
            "factory": _factory_memory(observation),
            "resources": _resource_memory(observation),
        }
    )
    if include_planning_sites:
        memory["planning_sites"] = {
            key: _compact_site_candidates(observation.get(key), key)
            for key in PLANNING_SITE_KEYS
        }
        memory["known_water_sites"] = _known_water_sites(memory["planning_sites"].get("power_sites", []))
        memory["planning_sites_updated_at"] = now_text
        memory["planning_sites_updated_at_epoch"] = now
        memory["planning_sites_tick"] = observation.get("tick")
        memory["planning_sites_source"] = source
    memory["spatial_index"] = _spatial_index(memory)
    _write_world_map_memory(runtime_dir, memory)
    return memory


def merge_world_map_memory_into_observation(
    observation: dict[str, Any],
    memory: dict[str, Any],
    *,
    max_age_seconds: float = DEFAULT_WORLD_MEMORY_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    if not memory:
        return observation
    merged = dict(observation)
    merged["world_map_memory"] = summarize_world_map_memory(memory)
    if not planning_sites_are_fresh(memory, max_age_seconds=max_age_seconds):
        return merged
    sites = memory.get("planning_sites") if isinstance(memory.get("planning_sites"), dict) else {}
    for key in PLANNING_SITE_KEYS:
        cached = sites.get(key)
        if isinstance(cached, list) and not merged.get(key):
            merged[key] = cached
    merged["planning_sites_cached_from_tick"] = memory.get("planning_sites_tick")
    merged["planning_sites_cache_age_seconds"] = round(_planning_site_age_seconds(memory), 3)
    return merged


def planning_sites_from_memory(memory: dict[str, Any]) -> dict[str, Any]:
    sites = memory.get("planning_sites") if isinstance(memory.get("planning_sites"), dict) else {}
    output: dict[str, Any] = {
        "tick": memory.get("planning_sites_tick"),
        "cached_at": memory.get("planning_sites_updated_at_epoch"),
    }
    for key in PLANNING_SITE_KEYS:
        value = sites.get(key)
        if isinstance(value, list):
            output[key] = value
    return output


def planning_sites_are_fresh(
    memory: dict[str, Any],
    *,
    max_age_seconds: float = DEFAULT_WORLD_MEMORY_MAX_AGE_SECONDS,
) -> bool:
    cached_at = memory.get("planning_sites_updated_at_epoch")
    return isinstance(cached_at, (int, float)) and time.time() - cached_at <= max_age_seconds


def summarize_world_map_memory(memory: dict[str, Any]) -> dict[str, Any]:
    planning_sites = memory.get("planning_sites") if isinstance(memory.get("planning_sites"), dict) else {}
    factory = memory.get("factory") if isinstance(memory.get("factory"), dict) else {}
    resources = memory.get("resources") if isinstance(memory.get("resources"), dict) else {}
    known_water = memory.get("known_water_sites") if isinstance(memory.get("known_water_sites"), list) else []
    return {
        "schema_version": memory.get("schema_version"),
        "encoding": memory.get("encoding"),
        "updated_at": memory.get("updated_at"),
        "updated_age_seconds": round(_age_seconds(memory.get("updated_at_epoch")), 3),
        "tick": memory.get("tick"),
        "planning_sites_updated_at": memory.get("planning_sites_updated_at"),
        "planning_sites_age_seconds": round(_planning_site_age_seconds(memory), 3),
        "planning_sites_tick": memory.get("planning_sites_tick"),
        "candidate_counts": {
            key: len(planning_sites.get(key) or []) if isinstance(planning_sites.get(key), list) else 0
            for key in PLANNING_SITE_KEYS
        },
        "known_water_sites": known_water[:8],
        "factory": factory,
        "resources": resources,
        "spatial_index": _spatial_index_summary(memory.get("spatial_index")),
    }


def _write_world_map_memory(runtime_dir: Path, memory: dict[str, Any]) -> None:
    runtime = Path(runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    world_map_memory_path(runtime).write_text(
        json.dumps(memory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _compact_site_candidates(value: Any, site_key: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = []
    seen: set[str] = set()
    for item in value[:40]:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["memory_kind"] = site_key
        key = _site_memory_key(row, site_key)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _site_memory_key(site: dict[str, Any], site_key: str) -> str:
    layout = site.get("layout") if isinstance(site.get("layout"), dict) else {}
    pump = layout.get("offshore_pump") if isinstance(layout.get("offshore_pump"), dict) else {}
    position = _position(pump.get("position")) or _position(site.get("position"))
    if position:
        return f"{site_key}:{round(position['x'], 1)}:{round(position['y'], 1)}"
    return f"{site_key}:{json.dumps(site, sort_keys=True, ensure_ascii=False)[:120]}"


def _known_water_sites(power_sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for site in power_sites[:20]:
        layout = site.get("layout") if isinstance(site.get("layout"), dict) else {}
        pump = layout.get("offshore_pump") if isinstance(layout.get("offshore_pump"), dict) else {}
        position = _position(pump.get("position"))
        if not position:
            continue
        rows.append(
            {
                "position": position,
                "direction": pump.get("direction"),
                "distance": site.get("distance"),
                "distance_from_agent": site.get("distance_from_agent"),
            }
        )
    return rows


def _factory_memory(observation: dict[str, Any]) -> dict[str, Any]:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    counts: dict[str, int] = {}
    factory_entities: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        if _factory_entity(entity):
            factory_entities.append(entity)
    zones = _cluster_positioned_rows(factory_entities, FACTORY_ZONE_DISTANCE, feature_prefix="factory_zone")
    return {
        "entity_count": len([entity for entity in entities if isinstance(entity, dict)]),
        "entity_counts": dict(sorted(counts.items())),
        "zone_count": len(zones),
        "zones": [
            {
                "id": zone["id"],
                "center": zone["center"],
                "bounds": zone["bounds"],
                "entity_count": zone["count"],
                "entity_counts": zone["counts"],
            }
            for zone in zones[:MAX_FEATURES_PER_KIND]
        ],
    }


def _resource_memory(observation: dict[str, Any]) -> dict[str, Any]:
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    counts: dict[str, int] = {}
    nearest: dict[str, dict[str, Any]] = {}
    resources_by_name: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        name = str(resource.get("name") or "")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        resources_by_name.setdefault(name, []).append(resource)
        current = nearest.get(name)
        distance = _safe_float(resource.get("distance"), 999999.0)
        if current is None or distance < _safe_float(current.get("distance"), 999999.0):
            nearest[name] = {
                "position": _position(resource.get("position")),
                "distance": round(distance, 3),
                "amount": resource.get("amount"),
            }
    patches = []
    for name, rows in sorted(resources_by_name.items()):
        clusters = _cluster_positioned_rows(rows, RESOURCE_CLUSTER_DISTANCE, feature_prefix=f"resource_patch:{name}")
        for index, cluster in enumerate(clusters[:MAX_FEATURES_PER_KIND], start=1):
            patches.append(
                {
                    "id": f"resource_patch:{name}:{index}",
                    "name": name,
                    "center": cluster["center"],
                    "bounds": cluster["bounds"],
                    "sample_count": cluster["count"],
                    "total_amount": round(cluster["total_amount"], 3),
                    "nearest_distance": cluster["nearest_distance"],
                }
            )
    return {
        "encoding": "cluster_bounds",
        "resource_counts": dict(sorted(counts.items())),
        "nearest": nearest,
        "patch_count": len(patches),
        "patches": patches[:MAX_FEATURES_PER_KIND],
    }


def _factory_entity(entity: dict[str, Any]) -> bool:
    entity_type = str(entity.get("type") or "")
    if entity_type in {"tree", "resource", "unit", "unit-spawner"}:
        return False
    name = str(entity.get("name") or "")
    if not name or name == "character":
        return False
    force = str(entity.get("force") or "")
    return force in {"", "player"}


def _cluster_positioned_rows(
    rows: list[dict[str, Any]],
    threshold: float,
    *,
    feature_prefix: str,
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for row in rows:
        position = _position(row.get("position"))
        if not position:
            continue
        best = None
        best_distance = threshold
        for cluster in clusters:
            cluster_distance = _distance(position, cluster["center"])
            if cluster_distance <= best_distance:
                best = cluster
                best_distance = cluster_distance
        if best is None:
            best = {
                "id": f"{feature_prefix}:{len(clusters) + 1}",
                "positions": [],
                "counts": {},
                "total_amount": 0.0,
                "nearest_distance": _safe_float(row.get("distance"), 999999.0),
            }
            clusters.append(best)
        best["positions"].append(position)
        name = str(row.get("name") or "unknown")
        best["counts"][name] = best["counts"].get(name, 0) + 1
        best["total_amount"] += _safe_float(row.get("amount"), 0.0)
        best["nearest_distance"] = min(best["nearest_distance"], _safe_float(row.get("distance"), 999999.0))
        best["center"] = _positions_center(best["positions"])
        best["bounds"] = _positions_bounds(best["positions"])
        best["count"] = len(best["positions"])
    clusters.sort(key=lambda item: (_safe_float(item.get("nearest_distance"), 999999.0), -int(item.get("count") or 0)))
    return clusters


def _spatial_index(memory: dict[str, Any]) -> dict[str, Any]:
    features = []
    resources = memory.get("resources") if isinstance(memory.get("resources"), dict) else {}
    for patch in resources.get("patches") or []:
        if isinstance(patch, dict):
            features.append((patch.get("id"), patch.get("center")))
    factory = memory.get("factory") if isinstance(memory.get("factory"), dict) else {}
    for zone in factory.get("zones") or []:
        if isinstance(zone, dict):
            features.append((zone.get("id"), zone.get("center")))
    for index, water in enumerate(memory.get("known_water_sites") or [], start=1):
        if isinstance(water, dict):
            features.append((f"water_anchor:{index}", water.get("position")))
    cells: dict[str, list[str]] = {}
    for feature_id, position in features:
        if not feature_id:
            continue
        pos = _position(position)
        if not pos:
            continue
        key = _cell_key(pos)
        cells.setdefault(key, []).append(str(feature_id))
    return {
        "encoding": "sparse_feature_index",
        "cell_size": SPATIAL_INDEX_CELL_SIZE,
        "cell_count": len(cells),
        "feature_count": sum(len(value) for value in cells.values()),
        "cells": dict(sorted(cells.items())),
    }


def _spatial_index_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "encoding": value.get("encoding"),
        "cell_size": value.get("cell_size"),
        "cell_count": value.get("cell_count"),
        "feature_count": value.get("feature_count"),
    }


def _cell_key(position: dict[str, float]) -> str:
    return f"{int(position['x'] // SPATIAL_INDEX_CELL_SIZE)}:{int(position['y'] // SPATIAL_INDEX_CELL_SIZE)}"


def _positions_center(positions: list[dict[str, float]]) -> dict[str, float]:
    return {
        "x": round(sum(position["x"] for position in positions) / len(positions), 3),
        "y": round(sum(position["y"] for position in positions) / len(positions), 3),
    }


def _positions_bounds(positions: list[dict[str, float]]) -> dict[str, Any]:
    xs = [position["x"] for position in positions]
    ys = [position["y"] for position in positions]
    return {
        "min_x": round(min(xs), 3),
        "max_x": round(max(xs), 3),
        "min_y": round(min(ys), 3),
        "max_y": round(max(ys), 3),
    }


def _distance(a: dict[str, float], b: dict[str, float]) -> float:
    return ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5


def _position(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    try:
        return {"x": round(float(value.get("x")), 3), "y": round(float(value.get("y")), 3)}
    except (TypeError, ValueError):
        return {}


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _planning_site_age_seconds(memory: dict[str, Any]) -> float:
    return _age_seconds(memory.get("planning_sites_updated_at_epoch"))


def _age_seconds(epoch: Any) -> float:
    if not isinstance(epoch, (int, float)):
        return 0.0
    return max(0.0, time.time() - epoch)
