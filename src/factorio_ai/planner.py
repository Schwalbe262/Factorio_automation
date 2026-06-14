from __future__ import annotations

from collections import Counter
from math import ceil
from typing import Any

from .blueprints import decode_blueprint_string, encode_blueprint_entities
from .knowledge import RECIPES
from .monitor import estimate_factory_sites, estimate_logistics_links
from .models import (
    PlannerDecision,
    craftable_count,
    distance,
    entities_named,
    entity_fluid_count,
    entity_item_count,
    inventory_count,
    nearest_entity,
    nearest_resource,
    player_position,
    total_item_count,
)


NORTH = 0
EAST = 4
SOUTH = 8
WEST = 12
FURNACE_RESOURCE_RADIUS = 12.0
WALK_FUEL_LOGISTICS_LIMIT = 160.0
STARTER_SITE_RADIUS = 192.0
STARTER_POWER_SITE_RADIUS = 160.0
STARTER_ENTITY_CLUSTER_RADIUS = 224.0
SMELTING_LINE_FUEL_RESERVE = {
    "drill": 8,
    "inserter": 4,
    "furnace": 8,
}
SMELTING_LINE_FUEL_INSERT = {
    "drill": 16,
    "inserter": 8,
    "furnace": 16,
}
ASSEMBLER_ENTITY_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
POWER_CONNECTOR_NAMES = {"small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation"}
PROTECTED_RESOURCE_NAMES = {"iron-ore", "copper-ore", "coal", "stone", "uranium-ore"}
PRESERVED_STARTER_ARTIFACT_KEYWORDS = ("crash", "wreck", "spaceship")
SITE_GATE_INPUT_STOCK_FALLBACK = 20
SITE_GATE_LOCAL_LOGISTICS_RADIUS = 96.0
SITE_PLACEMENT_SEARCH_STEP = 8
SITE_PLACEMENT_SEARCH_RADIUS = 48
MANUAL_SITE_INPUT_RADIUS = 48.0
AUTOMATED_SITE_INPUT_ITEMS = {
    "iron-plate",
    "copper-plate",
    "iron-gear-wheel",
    "copper-cable",
    "electronic-circuit",
    "automation-science-pack",
    "logistic-science-pack",
}


class FactoryLayoutImprovementSkill:
    """Plan safe layout improvements before committing to more inefficient builds."""

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        issues = factory_layout_issues(observation)
        opportunities = factory_layout_opportunities(observation)
        candidates = factory_layout_simulation_candidates(observation)
        if not issues and not opportunities:
            return PlannerDecision(None, "factory layout has no high-priority issues or optimization opportunities", done=True)
        issue_text = "; ".join(
            f"{item['kind']}({item['severity']}): {item['recommendation']}"
            for item in issues[:5]
        )
        opportunity_text = "; ".join(
            f"{item['kind']}({item['severity']}): {item['recommendation']}"
            for item in opportunities[:5]
        )
        candidate_text = ""
        if candidates:
            best = candidates[0]
            simulation = best.get("simulation") if isinstance(best.get("simulation"), dict) else {}
            candidate_text = (
                f"best_candidate={best.get('candidate_id')} score={simulation.get('score')} "
                f"not_applied=true"
            )
        parts = [part for part in (issue_text, opportunity_text, candidate_text) if part]
        return PlannerDecision(None, f"layout improvement plan: {'; '.join(parts)}", done=True)


def factory_layout_structure(observation: dict[str, Any]) -> dict[str, Any]:
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    positions = [
        entity.get("position")
        for entity in entities
        if isinstance(entity, dict) and isinstance(entity.get("position"), dict)
    ]
    site_kind_counts = Counter(str(site.get("kind") or "") for site in sites)
    item_site_counts = Counter(str(site.get("item") or "") for site in sites if site.get("item"))
    link_status_counts = Counter(str(link.get("status") or "") for link in links)
    machine_counts = Counter(str(entity.get("name") or "") for entity in entities if isinstance(entity, dict))
    footprint = _layout_footprint(positions)
    return {
        "site_count": len(sites),
        "site_kinds": dict(site_kind_counts.most_common(12)),
        "site_items": dict(item_site_counts.most_common(12)),
        "link_status": dict(link_status_counts.most_common(12)),
        "machine_counts": dict(machine_counts.most_common(16)),
        "factory_centroid": _centroid(positions),
        "footprint": footprint,
        "estimated_density": _safe_density(len(positions), footprint),
    }


def factory_layout_issues(observation: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    player = player_position(observation)
    spawn = observation.get("base", {}).get("spawn_position") if isinstance(observation.get("base"), dict) else None
    anchor = spawn if isinstance(spawn, dict) else {"x": 0.0, "y": 0.0}

    for site in sites:
        kind = str(site.get("kind") or "")
        status = str(site.get("status") or "")
        automation = str(site.get("automation_level") or "")
        machines_text = " ".join(str(item) for item in site.get("machines", []))
        position = site.get("position") if isinstance(site.get("position"), dict) else None
        if position and kind in {"plate_smelting_line", "build_item_mall", "assembler_cell", "circuit_automation", "research_lab_block"}:
            spawn_distance = distance(anchor, position)
            if spawn_distance > STARTER_SITE_RADIUS:
                issues.append(
                    _layout_issue(
                        "remote_starter_site",
                        90,
                        site,
                        f"{kind} is {spawn_distance:.0f} tiles from the starter base before rail logistics are available",
                        "stop expanding this remote block; build the next starter site near the base cluster or plan a rail outpost first",
                    )
                )
        if kind == "steam_power" and position:
            spawn_distance = distance(anchor, position)
            player_distance = distance(player, position)
            if spawn_distance > 160 and player_distance > 120:
                issues.append(
                    _layout_issue(
                        "remote_power_block",
                        92,
                        site,
                        f"starter steam power is {spawn_distance:.0f} tiles from spawn and {player_distance:.0f} tiles from the agent",
                        "plan a closer main power block or a clear pole corridor before expanding powered automation",
                    )
                )
            if "manual_fuel" in status:
                issues.append(
                    _layout_issue(
                        "manual_power_fuel",
                        76,
                        site,
                        "steam power still depends on manual coal insertion",
                        "route coal belt/inserter fuel feed to boiler before scaling electric machines",
                    )
                )
        if "burner-bootstrap" in automation and "burner-mining-drill" in machines_text:
            issues.append(
                _layout_issue(
                    "burner_bootstrap_upgrade",
                    58,
                    site,
                    "burner mining remains in a bootstrap layout",
                    "replace with electric miners and belt-fed smelting once power and build items are stable",
                )
            )
        if kind in {"research_lab_block", "assembler_cell", "circuit_automation"} and "manual" in automation:
            issues.append(
                _layout_issue(
                    "manual_feed_factory_block",
                    64,
                    site,
                    f"{kind} is still manually fed",
                    "reserve belt/chest feed lanes and convert the block to automated input/output logistics",
                )
            )

    for entity_issue in _resource_tile_blocking_issues(observation):
        issues.append(entity_issue)

    for link in links:
        status = str(link.get("status") or "")
        item = str(link.get("item") or "")
        try:
            length = float(link.get("length_tiles") or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        if (
            bool(_technology_state(observation, "automation").get("researched"))
            and status == "route_needed"
            and item in AUTOMATED_SITE_INPUT_ITEMS
            and length > MANUAL_SITE_INPUT_RADIUS
        ):
            issues.append(
                {
                    "kind": "manual_site_logistics_gap",
                    "severity": 88,
                    "site_id": link.get("link_id"),
                    "item": link.get("item"),
                    "detail": (
                        f"{item} must move {length:.0f} tiles from {link.get('from_site')} "
                        f"to {link.get('to_site')} but no site-to-site route is observed"
                    ),
                    "recommendation": (
                        "build or validate a belt/chest/logistic line between the related sites before "
                        "continuing repeated hand-carry production"
                    ),
                }
            )
        if item in AUTOMATED_SITE_INPUT_ITEMS and length > 96.0 and str(link.get("kind") or "") != "rail":
            issues.append(
                {
                    "kind": "distant_related_sites",
                    "severity": 84,
                    "site_id": link.get("link_id"),
                    "item": link.get("item"),
                    "detail": f"related {item} producer/consumer sites are {length:.0f} tiles apart without rail",
                    "recommendation": "co-locate related starter sites or reserve a trunk belt/rail corridor before scaling this flow",
                }
            )
        if any(token in status for token in ("incomplete", "missing", "blocked")):
            issues.append(
                {
                    "kind": "incomplete_logistics_link",
                    "severity": 82,
                    "site_id": link.get("link_id"),
                    "item": link.get("item"),
                    "detail": f"{link.get('from_site')} -> {link.get('to_site')} is {status}",
                    "recommendation": "finish producer-to-consumer belt or rail link before adding more consumers",
                }
            )
        if length > 180 and str(link.get("kind")) != "rail":
            issues.append(
                {
                    "kind": "long_nonrail_logistics",
                    "severity": 70,
                    "site_id": link.get("link_id"),
                    "item": link.get("item"),
                    "detail": f"{link.get('from_site')} -> {link.get('to_site')} is {length:.0f} tiles without rail",
                    "recommendation": "reserve a rail or trunk-belt corridor instead of extending ad-hoc local belts",
                }
            )

    issues.sort(key=lambda item: int(item.get("severity") or 0), reverse=True)
    return issues[:12]


def factory_layout_opportunities(observation: dict[str, Any]) -> list[dict[str, Any]]:
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    opportunities: list[dict[str, Any]] = []

    smelting_by_item: dict[str, list[dict[str, Any]]] = {}
    for site in sites:
        if site.get("kind") == "plate_smelting_line" and site.get("item"):
            smelting_by_item.setdefault(str(site.get("item")), []).append(site)
    for item, rows in sorted(smelting_by_item.items()):
        if len(rows) >= 3:
            positions = [row.get("position") for row in rows if isinstance(row.get("position"), dict)]
            footprint = _layout_footprint(positions)
            opportunities.append(
                {
                    "kind": "standardize_smelting_block",
                    "severity": min(88, 60 + len(rows) * 5),
                    "site_id": f"smelting:{item}",
                    "item": item,
                    "detail": f"{len(rows)} {item} smelting rows/sites are split across footprint {footprint.get('width', 0):.0f}x{footprint.get('height', 0):.0f}",
                    "recommendation": "migrate bootstrap rows toward repeatable parallel smelting columns with shared ore/fuel/input and plate output lanes",
                    "parameters": {
                        "target_pattern": "parallel_smelting_columns",
                        "grouped_rows": len(rows),
                        "reserve_input_lanes": ["ore", "coal_or_power"],
                        "reserve_output_lanes": ["plate"],
                    },
                }
            )

    recipe_counts = Counter(
        str(entity.get("recipe") or "")
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("recipe") or "")
    )
    circuit_assemblers = recipe_counts.get("electronic-circuit", 0)
    cable_assemblers = recipe_counts.get("copper-cable", 0)
    if circuit_assemblers > 0:
        ratio = cable_assemblers / max(circuit_assemblers, 1)
        if ratio < 1.3 or ratio > 1.8:
            opportunities.append(
                {
                    "kind": "rebalance_green_circuit_ratio",
                    "severity": 78,
                    "site_id": "circuit_automation:green",
                    "item": "electronic-circuit",
                    "detail": f"green circuit block has cable:circuit assembler ratio {cable_assemblers}:{circuit_assemblers}; target is about 3:2",
                    "recommendation": "rebuild or extend green circuits as a compact 3 cable to 2 circuit assembler pattern with short direct cable transfer",
                    "parameters": {
                        "target_pattern": "3_cable_to_2_circuit",
                        "target_cable_per_circuit_assembler": 1.5,
                        "current_cable_per_circuit_assembler": round(ratio, 2),
                    },
                }
            )
    elif any(site.get("kind") == "circuit_automation" for site in sites):
        opportunities.append(
            {
                "kind": "complete_green_circuit_block_pattern",
                "severity": 72,
                "site_id": "circuit_automation:green",
                "item": "electronic-circuit",
                "detail": "circuit automation exists but no complete electronic-circuit assembler pattern is visible",
                "recommendation": "convert ad-hoc circuit machines into a ratioed green-circuit cell before scaling red/green science",
                "parameters": {"target_pattern": "3_cable_to_2_circuit", "inputs": ["copper-plate", "iron-plate"]},
            }
        )

    local_intermediates = {"iron-plate", "copper-plate", "iron-gear-wheel", "copper-cable", "electronic-circuit"}
    for link in links:
        if not isinstance(link, dict) or link.get("item") not in local_intermediates:
            continue
        try:
            length = float(link.get("length_tiles") or 0.0)
        except (TypeError, ValueError):
            continue
        if 45.0 <= length <= 180.0 and str(link.get("kind") or "") != "rail":
            opportunities.append(
                {
                    "kind": "shorten_intermediate_flow",
                    "severity": 62,
                    "site_id": link.get("link_id"),
                    "item": link.get("item"),
                    "detail": f"{link.get('item')} travels {length:.0f} tiles between {link.get('from_site')} and {link.get('to_site')}",
                    "recommendation": "co-locate the consumer near the producer or move both onto a planned bus/trunk lane before the flow grows",
                    "parameters": {"max_pre_bus_distance": 45, "preferred_solution": "co_locate_or_trunk_belt"},
                }
            )

    lab_sites = [site for site in sites if site.get("kind") == "research_lab_block"]
    if lab_sites and any("manual" in str(site.get("automation_level") or "") for site in lab_sites):
        opportunities.append(
            {
                "kind": "upgrade_lab_feed_pattern",
                "severity": 66,
                "site_id": "research_lab_block",
                "item": "research",
                "detail": "labs are present but the feed pattern is still manual",
                "recommendation": "use a short lab daisy chain or multi-feed science belt before expanding research throughput",
                "parameters": {"target_pattern": "lab_daisy_chain", "max_chain_length": 8},
            }
        )

    build_item_sites = [site for site in sites if site.get("kind") == "build_item_mall"]
    if len(build_item_sites) >= 4:
        positions = [site.get("position") for site in build_item_sites if isinstance(site.get("position"), dict)]
        footprint = _layout_footprint(positions)
        if max(float(footprint.get("width") or 0.0), float(footprint.get("height") or 0.0)) > 36:
            opportunities.append(
                {
                    "kind": "compact_build_item_mall",
                    "severity": 64,
                    "site_id": "build_item_mall",
                    "item": "factory_build_items",
                    "detail": f"build-item assemblers are spread across {footprint.get('width', 0):.0f}x{footprint.get('height', 0):.0f} tiles",
                    "recommendation": "group mall cells near iron/circuit supply with shared input and chest output lanes",
                    "parameters": {"target_pattern": "starter_mall_row", "shared_inputs": ["iron-plate", "gear", "circuit"]},
                }
            )

    opportunities.sort(key=lambda item: int(item.get("severity") or 0), reverse=True)
    return opportunities[:12]


def factory_layout_simulation_candidates(observation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return simulation-only layout candidates; nothing here is applied to the game."""

    opportunities = factory_layout_opportunities(observation)
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    recipe_counts = Counter(
        str(entity.get("recipe") or "")
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("recipe") or "")
    )
    candidates: list[dict[str, Any]] = []

    opportunity_kinds = {str(item.get("kind") or "") for item in opportunities}
    if {"rebalance_green_circuit_ratio", "complete_green_circuit_block_pattern"} & opportunity_kinds:
        current_circuit_sites = [
            site
            for site in sites
            if site.get("kind") in {"circuit_automation", "assembler_cell"}
            and (site.get("item") in {None, "copper-cable", "electronic-circuit"} or site.get("kind") == "circuit_automation")
        ]
        candidates.append(
            _with_blueprint_variants(
                _green_circuit_layout_candidate(recipe_counts, observation, sites, current_circuit_sites),
                _combined_site_blueprint(
                    "before-green-circuit-block",
                    current_circuit_sites,
                    "Observed current green-circuit/cable machine footprint before ratio rebalance.",
                ),
            )
        )

    smelting_items = sorted(
        {
            str(site.get("item"))
            for site in sites
            if site.get("kind") == "plate_smelting_line" and site.get("item") in {"iron-plate", "copper-plate"}
        }
    )
    for item in smelting_items:
        rows = [site for site in sites if site.get("kind") == "plate_smelting_line" and site.get("item") == item]
        if len(rows) >= 2:
            candidates.append(
                _with_blueprint_variants(
                    _smelting_standardization_candidate(item, rows),
                    _combined_site_blueprint(
                        f"before-{item}-smelting-sites",
                        rows,
                        "Observed current smelting footprint before standardization.",
                    ),
                )
            )

    for link in links:
        if not isinstance(link, dict):
            continue
        try:
            length = float(link.get("length_tiles") or 0.0)
        except (TypeError, ValueError):
            continue
        if link.get("item") in {"iron-plate", "copper-plate", "iron-gear-wheel", "copper-cable", "electronic-circuit"} and length >= 45:
            candidates.append(_flow_shortening_candidate(link, length))

    candidates.extend(_belt_bottleneck_candidates(observation, sites, links))

    lab_count = sum(1 for entity in entities if isinstance(entity, dict) and entity.get("name") == "lab")
    if lab_count > 0:
        lab_sites = [site for site in sites if site.get("kind") == "research_lab_block"]
        if any("manual" in str(site.get("automation_level") or "") for site in lab_sites):
            candidates.append(
                _with_blueprint_variants(
                    _lab_daisy_chain_candidate(lab_count),
                    _combined_site_blueprint(
                        "before-research-lab-feed",
                        lab_sites,
                        "Observed current research lab footprint before daisy-chain feed improvement.",
                    ),
                )
            )

    mall_sites = [site for site in sites if site.get("kind") == "build_item_mall"]
    if len(mall_sites) >= 4:
        candidates.append(
            _with_blueprint_variants(
                _mall_compaction_candidate(mall_sites),
                _combined_site_blueprint(
                    "before-starter-mall-sites",
                    mall_sites,
                    "Observed current starter mall footprint before row compaction.",
                ),
            )
        )

    candidates = [candidate for candidate in candidates if candidate]
    candidates.sort(
        key=lambda item: float((item.get("simulation") if isinstance(item.get("simulation"), dict) else {}).get("score") or 0.0),
        reverse=True,
    )
    return candidates[:8]


def _with_blueprint_variants(
    candidate: dict[str, Any],
    before_blueprint: dict[str, Any] | None,
) -> dict[str, Any]:
    if not candidate:
        return candidate
    output = dict(candidate)
    after_blueprint = output.get("after_blueprint")
    if not isinstance(after_blueprint, dict):
        after_blueprint = output.get("blueprint") if isinstance(output.get("blueprint"), dict) else None
    if isinstance(before_blueprint, dict):
        output["before_blueprint"] = before_blueprint
    if isinstance(after_blueprint, dict):
        output["after_blueprint"] = after_blueprint
        output.setdefault("blueprint", after_blueprint)
    return output


def _combined_site_blueprint(
    label: str,
    sites: list[dict[str, Any]],
    description: str,
) -> dict[str, Any] | None:
    sites = _representative_blueprint_sites(sites)
    absolute_entities: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict) or not isinstance(site.get("position"), dict):
            continue
        blueprint = site.get("blueprint") if isinstance(site.get("blueprint"), dict) else {}
        exchange_string = blueprint.get("exchange_string")
        if not isinstance(exchange_string, str) or not exchange_string:
            continue
        try:
            payload = decode_blueprint_string(exchange_string)
        except Exception:
            continue
        raw_entities = payload.get("blueprint", {}).get("entities")
        if not isinstance(raw_entities, list):
            continue
        site_position = site["position"]
        for entity in raw_entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
                continue
            position = entity["position"]
            row: dict[str, Any] = {
                "name": str(entity.get("name") or ""),
                "position": {
                    "x": float(site_position.get("x") or 0.0) + float(position.get("x") or 0.0),
                    "y": float(site_position.get("y") or 0.0) + float(position.get("y") or 0.0),
                },
            }
            if entity.get("direction") is not None:
                row["direction"] = int(entity.get("direction") or 0)
            if isinstance(entity.get("recipe"), str):
                row["recipe"] = entity["recipe"]
            if isinstance(entity.get("items"), dict):
                row["items"] = entity["items"]
            absolute_entities.append(row)
    if not absolute_entities:
        return None
    center = _centroid([entity["position"] for entity in absolute_entities]) or {"x": 0.0, "y": 0.0}
    normalized_entities: list[dict[str, Any]] = []
    for entity in absolute_entities:
        position = entity["position"]
        normalized = dict(entity)
        normalized["position"] = {
            "x": round(float(position.get("x") or 0.0) - float(center.get("x") or 0.0), 3),
            "y": round(float(position.get("y") or 0.0) - float(center.get("y") or 0.0), 3),
        }
        normalized_entities.append(normalized)
    return _blueprint_export(label, normalized_entities, description)


def _representative_blueprint_sites(sites: list[dict[str, Any]], max_span: float = 54.0) -> list[dict[str, Any]]:
    positioned = [site for site in sites if isinstance(site, dict) and isinstance(site.get("position"), dict)]
    if len(positioned) <= 1:
        return positioned
    footprint = _layout_footprint([site["position"] for site in positioned])
    if max(float(footprint.get("width") or 0.0), float(footprint.get("height") or 0.0)) <= max_span:
        return positioned

    best_cluster: list[dict[str, Any]] = []
    best_area = float("inf")
    best_distance = float("inf")
    for seed in positioned:
        seed_position = seed["position"]
        cluster = [
            site
            for site in positioned
            if distance(seed_position, site["position"]) <= max_span
        ]
        cluster_footprint = _layout_footprint([site["position"] for site in cluster])
        area = float(cluster_footprint.get("area") or 0.0)
        origin_distance = distance(seed_position, {"x": 0.0, "y": 0.0})
        if (
            len(cluster) > len(best_cluster)
            or (len(cluster) == len(best_cluster) and area < best_area)
            or (len(cluster) == len(best_cluster) and area == best_area and origin_distance < best_distance)
        ):
            best_cluster = cluster
            best_area = area
            best_distance = origin_distance
    return best_cluster or [positioned[0]]


def _layout_issue(
    kind: str,
    severity: int,
    site: dict[str, Any],
    detail: str,
    recommendation: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "site_id": site.get("site_id"),
        "item": site.get("item"),
        "position": site.get("position"),
        "detail": detail,
        "recommendation": recommendation,
    }


def _resource_tile_blocking_issues(observation: dict[str, Any]) -> list[dict[str, Any]]:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    protected_resources = {"iron-ore", "copper-ore", "coal", "stone", "uranium-ore"}
    allowed_on_resource = {
        "burner-mining-drill",
        "electric-mining-drill",
        "big-mining-drill",
        "transport-belt",
        "underground-belt",
        "fast-transport-belt",
        "express-transport-belt",
    }
    blocking_names = {
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "boiler",
        "steam-engine",
        "lab",
    }
    resource_positions = [
        resource
        for resource in resources
        if isinstance(resource, dict) and str(resource.get("name") or "") in protected_resources
    ]
    issues: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name in allowed_on_resource or name not in blocking_names:
            continue
        position = entity.get("position") if isinstance(entity.get("position"), dict) else None
        if not position:
            continue
        nearest = min(
            (
                resource
                for resource in resource_positions
                if isinstance(resource.get("position"), dict)
            ),
            key=lambda resource: distance(position, resource["position"]),
            default=None,
        )
        if nearest is None or distance(position, nearest["position"]) > 2.0:
            continue
        issues.append(
            {
                "kind": "resource_tile_blocked",
                "severity": 74,
                "site_id": f"entity:{name}:{entity.get('unit_number') or position}",
                "item": nearest.get("name"),
                "position": position,
                "detail": f"{name} is built on or very near a starter {nearest.get('name')} patch",
                "recommendation": "avoid expanding production blocks over starter resources; reserve the patch for miner coverage unless no alternative remains",
            }
        )
    return issues[:8]


def _layout_footprint(positions: list[Any]) -> dict[str, float]:
    valid = [position for position in positions if isinstance(position, dict) and "x" in position and "y" in position]
    if not valid:
        return {"width": 0.0, "height": 0.0, "area": 0.0}
    xs = [float(position["x"]) for position in valid]
    ys = [float(position["y"]) for position in valid]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    return {"width": round(width, 1), "height": round(height, 1), "area": round(max(width, 1.0) * max(height, 1.0), 1)}


def _centroid(positions: list[Any]) -> dict[str, float] | None:
    valid = [position for position in positions if isinstance(position, dict) and "x" in position and "y" in position]
    if not valid:
        return None
    return {
        "x": round(sum(float(position["x"]) for position in valid) / len(valid), 1),
        "y": round(sum(float(position["y"]) for position in valid) / len(valid), 1),
    }


def _safe_density(count: int, footprint: dict[str, float]) -> float:
    try:
        area = float(footprint.get("area") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if area <= 0:
        return 0.0
    return round(count / area, 4)


def _site_prebuild_gate(
    observation: dict[str, Any],
    blueprint_entities: list[dict[str, Any]],
    *,
    target_item: str,
    required_inputs: tuple[str, ...],
    all_sites: list[dict[str, Any]],
    preferred_sites: list[dict[str, Any]],
    anchor: dict[str, float] | None = None,
    anchor_source: str | None = None,
) -> dict[str, Any]:
    if anchor is None:
        anchor, anchor_source = _site_gate_anchor(observation, preferred_sites)
    else:
        anchor = {"x": round(float(anchor.get("x") or 0.0), 1), "y": round(float(anchor.get("y") or 0.0), 1)}
        anchor_source = anchor_source or "explicit_anchor"
    planned_entities = _absolute_blueprint_entities(blueprint_entities, anchor)
    checks = {
        "build_items": _site_gate_build_item_check(observation, blueprint_entities),
        "collision": _site_gate_collision_check(observation, planned_entities),
        "resource_preservation": _site_gate_resource_check(observation, planned_entities),
        "power_reach": _site_gate_power_check(observation, planned_entities),
        "input_logistics": _site_gate_input_logistics_check(
            observation,
            all_sites,
            target_item=target_item,
            required_inputs=required_inputs,
            anchor=anchor,
        ),
    }
    errors: list[str] = []
    warnings: list[str] = []
    if anchor_source == "player_position":
        warnings.append("no current target site was found; pre-build gate anchored to player position")
    for check_name, check in checks.items():
        if not isinstance(check, dict):
            continue
        detail = str(check.get("summary") or check_name)
        if check.get("status") == "fail":
            errors.append(detail)
        elif check.get("status") == "warning":
            warnings.append(detail)
    status = "fail" if errors else ("warning" if warnings else "pass")
    return {
        "status": status,
        "build_ready": status == "pass",
        "summary": (
            "site-specific build gate passed"
            if status == "pass"
            else "sandbox-proven layout still needs site-specific build checks"
        ),
        "anchor": anchor,
        "anchor_source": anchor_source,
        "target_item": target_item,
        "checks": checks,
        "errors": errors[:8],
        "warnings": warnings[:8],
    }


def _site_placement_search(
    observation: dict[str, Any],
    blueprint_entities: list[dict[str, Any]],
    *,
    target_item: str,
    required_inputs: tuple[str, ...],
    all_sites: list[dict[str, Any]],
    preferred_sites: list[dict[str, Any]],
) -> dict[str, Any]:
    seed_anchor, seed_source = _site_gate_anchor(observation, preferred_sites)
    evaluated: list[dict[str, Any]] = []
    for anchor in _site_placement_anchor_candidates(seed_anchor):
        gate = _site_prebuild_gate(
            observation,
            blueprint_entities,
            target_item=target_item,
            required_inputs=required_inputs,
            all_sites=all_sites,
            preferred_sites=preferred_sites,
            anchor=anchor,
            anchor_source="placement_search_candidate",
        )
        score = _site_placement_score(seed_anchor, gate)
        evaluated.append(
            {
                "score": score,
                "distance_from_seed": round(distance(seed_anchor, gate["anchor"]), 1),
                "placement_ready": _site_gate_placement_ready(gate),
                "gate": gate,
            }
        )
    evaluated.sort(
        key=lambda row: (
            bool(row.get("placement_ready")),
            float(row.get("score") or 0.0),
            -float(row.get("distance_from_seed") or 0.0),
        ),
        reverse=True,
    )
    best = evaluated[0] if evaluated else {}
    selected_gate = dict(best.get("gate") if isinstance(best.get("gate"), dict) else {})
    if selected_gate:
        selected_gate["anchor_source"] = "placement_search"
    placement_ready = bool(best.get("placement_ready"))
    output = {
        "status": "found" if placement_ready else "blocked",
        "summary": (
            f"found candidate build anchor at {_position_key({'position': selected_gate.get('anchor', {})})}"
            if placement_ready and selected_gate
            else "no searched anchor clears collision, resource, power, and input logistics checks"
        ),
        "seed_anchor": seed_anchor,
        "seed_source": seed_source,
        "selected_anchor": selected_gate.get("anchor") if selected_gate else None,
        "selected_score": round(float(best.get("score") or 0.0), 1) if best else 0.0,
        "evaluated_anchors": len(evaluated),
        "search_radius": SITE_PLACEMENT_SEARCH_RADIUS,
        "search_step": SITE_PLACEMENT_SEARCH_STEP,
        "top_candidates": [_site_placement_search_row(row) for row in evaluated[:6]],
        "_selected_gate": selected_gate,
    }
    return output


def _site_placement_anchor_candidates(seed_anchor: dict[str, float]) -> list[dict[str, float]]:
    seen: set[tuple[float, float]] = set()
    anchors: list[dict[str, float]] = []
    step = max(1, SITE_PLACEMENT_SEARCH_STEP)
    radius = max(step, SITE_PLACEMENT_SEARCH_RADIUS)
    for dx in range(-radius, radius + 1, step):
        for dy in range(-radius, radius + 1, step):
            if (dx * dx + dy * dy) ** 0.5 > radius:
                continue
            x = round(float(seed_anchor.get("x") or 0.0) + dx, 1)
            y = round(float(seed_anchor.get("y") or 0.0) + dy, 1)
            key = (x, y)
            if key in seen:
                continue
            seen.add(key)
            anchors.append({"x": x, "y": y})
    anchors.sort(key=lambda anchor: distance(seed_anchor, anchor))
    return anchors


def _site_placement_score(seed_anchor: dict[str, float], gate: dict[str, Any]) -> float:
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    weights = {
        "collision": 35.0,
        "resource_preservation": 25.0,
        "power_reach": 20.0,
        "input_logistics": 20.0,
        "build_items": 8.0,
    }
    score = 0.0
    for check_name, weight in weights.items():
        check = checks.get(check_name) if isinstance(checks.get(check_name), dict) else {}
        if check.get("status") == "pass":
            score += weight
        elif check.get("status") == "warning":
            score += weight * 0.45
    anchor = gate.get("anchor") if isinstance(gate.get("anchor"), dict) else seed_anchor
    score -= min(24.0, distance(seed_anchor, anchor) / 2.0)
    errors = gate.get("errors") if isinstance(gate.get("errors"), list) else []
    score -= min(12.0, len(errors) * 1.5)
    return round(score, 3)


def _site_gate_placement_ready(gate: dict[str, Any]) -> bool:
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    for check_name in ("collision", "resource_preservation", "power_reach", "input_logistics"):
        check = checks.get(check_name) if isinstance(checks.get(check_name), dict) else {}
        if check.get("status") != "pass":
            return False
    return True


def _site_placement_search_row(row: dict[str, Any]) -> dict[str, Any]:
    gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    passed: list[str] = []
    failed: list[str] = []
    for check_name, check in checks.items():
        if not isinstance(check, dict):
            continue
        if check.get("status") == "pass":
            passed.append(str(check_name))
        elif check.get("status") == "fail":
            failed.append(str(check_name))
    return {
        "anchor": gate.get("anchor"),
        "score": round(float(row.get("score") or 0.0), 1),
        "distance_from_seed": row.get("distance_from_seed"),
        "placement_ready": bool(row.get("placement_ready")),
        "gate_status": gate.get("status"),
        "passed_checks": passed,
        "failed_checks": failed,
        "errors": (gate.get("errors") if isinstance(gate.get("errors"), list) else [])[:3],
    }


def _site_gate_anchor(
    observation: dict[str, Any],
    preferred_sites: list[dict[str, Any]],
) -> tuple[dict[str, float], str]:
    positions = [
        site.get("position")
        for site in preferred_sites
        if isinstance(site, dict) and isinstance(site.get("position"), dict)
    ]
    centroid = _centroid(positions)
    if centroid is not None:
        return centroid, "current_site_centroid"
    return player_position(observation), "player_position"


def _absolute_blueprint_entities(
    blueprint_entities: list[dict[str, Any]],
    anchor: dict[str, float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for entity in blueprint_entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        position = entity["position"]
        row = dict(entity)
        row["position"] = {
            "x": round(float(anchor.get("x") or 0.0) + float(position.get("x") or 0.0), 3),
            "y": round(float(anchor.get("y") or 0.0) + float(position.get("y") or 0.0), 3),
        }
        output.append(row)
    return output


def _site_gate_build_item_check(
    observation: dict[str, Any],
    blueprint_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    required = Counter(
        str(entity.get("name") or "")
        for entity in blueprint_entities
        if isinstance(entity, dict) and entity.get("name")
    )
    available = {name: total_item_count(observation, name) for name in sorted(required)}
    missing = {
        name: count - int(available.get(name) or 0)
        for name, count in sorted(required.items())
        if int(available.get(name) or 0) < count
    }
    status = "fail" if missing else "pass"
    return {
        "status": status,
        "required": dict(sorted(required.items())),
        "available": available,
        "missing": missing,
        "summary": _site_gate_missing_summary("missing build items", missing)
        if missing
        else "all blueprint build items are available",
    }


def _site_gate_collision_check(
    observation: dict[str, Any],
    planned_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    collisions: list[dict[str, Any]] = []
    for planned in planned_entities:
        planned_position = planned.get("position") if isinstance(planned.get("position"), dict) else None
        if planned_position is None:
            continue
        planned_radius = _site_gate_collision_radius(str(planned.get("name") or ""))
        for existing in existing_entities:
            if not isinstance(existing, dict) or not isinstance(existing.get("position"), dict):
                continue
            existing_radius = _site_gate_collision_radius(str(existing.get("name") or ""))
            if distance(planned_position, existing["position"]) > planned_radius + existing_radius:
                continue
            collisions.append(
                {
                    "planned": str(planned.get("name") or ""),
                    "existing": str(existing.get("name") or ""),
                    "position": planned_position,
                    "existing_position": existing["position"],
                }
            )
            break
        if len(collisions) >= 8:
            break
    return {
        "status": "fail" if collisions else "pass",
        "collisions": collisions,
        "summary": f"{len(collisions)} planned entities collide with current map entities"
        if collisions
        else "planned entity footprint has no observed collisions",
    }


def _site_gate_resource_check(
    observation: dict[str, Any],
    planned_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    protected = [
        resource
        for resource in resources
        if isinstance(resource, dict)
        and str(resource.get("name") or "") in PROTECTED_RESOURCE_NAMES
        and isinstance(resource.get("position"), dict)
    ]
    overlaps: list[dict[str, Any]] = []
    for planned in planned_entities:
        name = str(planned.get("name") or "")
        if not _site_gate_blocks_resource(name):
            continue
        planned_position = planned.get("position") if isinstance(planned.get("position"), dict) else None
        if planned_position is None:
            continue
        for resource in protected:
            if distance(planned_position, resource["position"]) > _site_gate_collision_radius(name) + 0.75:
                continue
            overlaps.append(
                {
                    "planned": name,
                    "resource": str(resource.get("name") or ""),
                    "position": planned_position,
                    "resource_position": resource["position"],
                }
            )
            break
        if len(overlaps) >= 8:
            break
    return {
        "status": "fail" if overlaps else "pass",
        "overlaps": overlaps,
        "summary": f"{len(overlaps)} planned entities overlap protected resource tiles"
        if overlaps
        else "no protected resource overlap detected",
    }


def _site_gate_power_check(
    observation: dict[str, Any],
    planned_entities: list[dict[str, Any]],
) -> dict[str, Any]:
    planned_poles = [
        entity
        for entity in planned_entities
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in POWER_CONNECTOR_NAMES
        and isinstance(entity.get("position"), dict)
    ]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    connected_poles = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in POWER_CONNECTOR_NAMES
        and entity.get("electric_network_connected") is not False
        and isinstance(entity.get("position"), dict)
    ]
    if not planned_poles:
        return {
            "status": "fail",
            "planned_poles": 0,
            "connected_poles": len(connected_poles),
            "summary": "blueprint has no power pole to connect the new cell",
        }
    if not connected_poles:
        return {
            "status": "fail",
            "planned_poles": len(planned_poles),
            "connected_poles": 0,
            "summary": "no connected existing power pole is in the observed site data",
        }
    nearest = _nearest_power_pair(planned_poles, connected_poles)
    if nearest is None:
        return {
            "status": "fail",
            "planned_poles": len(planned_poles),
            "connected_poles": len(connected_poles),
            "summary": "could not compare planned and existing power poles",
        }
    status = "pass" if nearest["distance"] <= nearest["wire_reach"] else "fail"
    return {
        "status": status,
        "planned_poles": len(planned_poles),
        "connected_poles": len(connected_poles),
        "nearest_connected_power": nearest,
        "summary": (
            f"nearest connected pole is {nearest['distance']:.1f} tiles away within {nearest['wire_reach']:.1f} reach"
            if status == "pass"
            else f"nearest connected pole is {nearest['distance']:.1f} tiles away, beyond {nearest['wire_reach']:.1f} reach"
        ),
    }


def _site_gate_input_logistics_check(
    observation: dict[str, Any],
    sites: list[dict[str, Any]],
    *,
    target_item: str,
    required_inputs: tuple[str, ...],
    anchor: dict[str, float],
) -> dict[str, Any]:
    inputs: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for item in required_inputs:
        source = _nearest_source_site(sites, item, anchor)
        stock = total_item_count(observation, item)
        local_source = bool(source and float(source.get("distance") or 0.0) <= SITE_GATE_LOCAL_LOGISTICS_RADIUS)
        stock_ready = stock >= SITE_GATE_INPUT_STOCK_FALLBACK
        status = "pass" if local_source or stock_ready else "fail"
        if status == "fail":
            missing.append(item)
        inputs[item] = {
            "status": status,
            "stock": stock,
            "stock_fallback": SITE_GATE_INPUT_STOCK_FALLBACK,
            "local_source_radius": SITE_GATE_LOCAL_LOGISTICS_RADIUS,
            "nearest_source": source,
        }
    return {
        "status": "fail" if missing else "pass",
        "target_item": target_item,
        "inputs": inputs,
        "summary": f"missing local input logistics for {', '.join(missing)}"
        if missing
        else f"input logistics are available for {target_item}",
    }


def _nearest_source_site(
    sites: list[dict[str, Any]],
    item: str,
    anchor: dict[str, float],
) -> dict[str, Any] | None:
    candidates = [
        site
        for site in sites
        if isinstance(site, dict)
        and site.get("item") == item
        and site.get("kind") in {"plate_smelting_line", "build_item_mall", "assembler_cell", "circuit_automation"}
        and isinstance(site.get("position"), dict)
    ]
    if not candidates:
        return None
    site = min(candidates, key=lambda row: distance(anchor, row["position"]))
    return {
        "site_id": site.get("site_id"),
        "kind": site.get("kind"),
        "position": site.get("position"),
        "distance": round(distance(anchor, site["position"]), 1),
        "status": site.get("status"),
    }


def _nearest_power_pair(
    planned_poles: list[dict[str, Any]],
    connected_poles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for planned in planned_poles:
        planned_name = str(planned.get("name") or "")
        planned_position = planned.get("position") if isinstance(planned.get("position"), dict) else None
        if planned_position is None:
            continue
        for existing in connected_poles:
            existing_position = existing.get("position") if isinstance(existing.get("position"), dict) else None
            if existing_position is None:
                continue
            existing_name = str(existing.get("name") or "")
            span = round(distance(planned_position, existing_position), 1)
            reach = max(_power_wire_reach(planned_name), _power_wire_reach(existing_name))
            row = {
                "distance": span,
                "wire_reach": reach,
                "planned": planned_name,
                "planned_position": planned_position,
                "existing": existing_name,
                "existing_position": existing_position,
            }
            if best is None or row["distance"] < best["distance"]:
                best = row
    return best


def _site_gate_collision_radius(name: str) -> float:
    if name in ASSEMBLER_ENTITY_NAMES or name in {"lab", "boiler"}:
        return 1.5
    if name in {"stone-furnace", "steel-furnace", "electric-furnace"}:
        return 1.0
    if name in {"steam-engine", "oil-refinery", "chemical-plant"}:
        return 2.0
    return 0.55


def _site_gate_blocks_resource(name: str) -> bool:
    return name in ASSEMBLER_ENTITY_NAMES or name in {
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "boiler",
        "steam-engine",
        "lab",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
    }


def _power_wire_reach(name: str) -> float:
    return {
        "small-electric-pole": 7.5,
        "medium-electric-pole": 9.0,
        "big-electric-pole": 30.0,
        "substation": 18.0,
    }.get(name, 7.5)


def _site_gate_missing_summary(prefix: str, missing: dict[str, int]) -> str:
    if not missing:
        return prefix
    parts = [f"{name} x{count}" for name, count in list(missing.items())[:5]]
    suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
    return f"{prefix}: {', '.join(parts)}{suffix}"


def _layout_candidate_build_ready_blockers(
    *,
    sandbox_validation: dict[str, Any] | None,
    site_gate: dict[str, Any],
    placement_search: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(sandbox_validation, dict) or sandbox_validation.get("status") != "pass":
        blockers.append("sandbox validation feedback must pass before build-ready")
    if site_gate.get("status") != "pass" or not site_gate.get("build_ready"):
        blockers.extend(str(item) for item in site_gate.get("errors", [])[:4] if item)
    if placement_search.get("status") != "found":
        summary = str(placement_search.get("summary") or "deterministic placement search did not find a ready anchor")
        if summary not in blockers:
            blockers.append(summary)
    return blockers[:8]


def layout_candidate_prerequisite_tasks(
    *,
    candidate_id: str,
    sandbox_validation: dict[str, Any] | None,
    site_gate: dict[str, Any],
    placement_search: dict[str, Any],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if not isinstance(sandbox_validation, dict) or sandbox_validation.get("status") != "pass":
        tasks.append(
            {
                "task_id": f"{candidate_id}:sandbox_validation",
                "kind": "sandbox_validation",
                "priority": 95,
                "status": "pending",
                "recommended_skill": "plan_factory_site",
                "detail": "prove the candidate on the disposable sandbox before allowing build execution",
                "command": (
                    "python -m factorio_ai.cli validate-layout-candidate "
                    f"--candidate-id {candidate_id} --variant after --ticks 3600"
                ),
            }
        )

    checks = site_gate.get("checks") if isinstance(site_gate.get("checks"), dict) else {}
    build_items = checks.get("build_items") if isinstance(checks.get("build_items"), dict) else {}
    missing_items = build_items.get("missing") if isinstance(build_items.get("missing"), dict) else {}
    if missing_items:
        tasks.append(
            {
                "task_id": f"{candidate_id}:supply_build_items",
                "kind": "supply_build_items",
                "priority": 90,
                "status": "pending",
                "recommended_skill": "bootstrap_build_item_mall",
                "items": dict(sorted((str(key), int(value)) for key, value in missing_items.items())),
                "detail": _site_gate_missing_summary("supply missing blueprint build items", missing_items),
            }
        )

    power = checks.get("power_reach") if isinstance(checks.get("power_reach"), dict) else {}
    if power.get("status") == "fail":
        tasks.append(
            {
                "task_id": f"{candidate_id}:extend_power",
                "kind": "extend_power_to_anchor",
                "priority": 86,
                "status": "pending",
                "recommended_skill": "setup_power",
                "target_anchor": site_gate.get("anchor"),
                "detail": str(power.get("summary") or "extend a connected pole corridor to the selected anchor"),
            }
        )

    input_logistics = checks.get("input_logistics") if isinstance(checks.get("input_logistics"), dict) else {}
    if input_logistics.get("status") == "fail":
        inputs = input_logistics.get("inputs") if isinstance(input_logistics.get("inputs"), dict) else {}
        missing_inputs = [
            str(item)
            for item, row in sorted(inputs.items())
            if isinstance(row, dict) and row.get("status") == "fail"
        ]
        tasks.append(
            {
                "task_id": f"{candidate_id}:connect_inputs",
                "kind": "connect_input_logistics",
                "priority": 84,
                "status": "pending",
                "recommended_skill": "plan_factory_site",
                "inputs": missing_inputs,
                "target_anchor": site_gate.get("anchor"),
                "detail": str(input_logistics.get("summary") or "connect required input logistics to the selected anchor"),
            }
        )

    collision = checks.get("collision") if isinstance(checks.get("collision"), dict) else {}
    resources = checks.get("resource_preservation") if isinstance(checks.get("resource_preservation"), dict) else {}
    if collision.get("status") == "fail" or resources.get("status") == "fail":
        tasks.append(
            {
                "task_id": f"{candidate_id}:clear_anchor",
                "kind": "select_clear_resource_safe_anchor",
                "priority": 82,
                "status": "pending",
                "recommended_skill": "plan_factory_site",
                "target_anchor": site_gate.get("anchor"),
                "detail": "; ".join(
                    part
                    for part in (
                        str(collision.get("summary") or "") if collision.get("status") == "fail" else "",
                        str(resources.get("summary") or "") if resources.get("status") == "fail" else "",
                    )
                    if part
                ),
            }
        )

    if placement_search.get("status") != "found":
        tasks.append(
            {
                "task_id": f"{candidate_id}:find_build_anchor",
                "kind": "find_build_anchor",
                "priority": 80,
                "status": "pending",
                "recommended_skill": "plan_factory_site",
                "selected_anchor": placement_search.get("selected_anchor"),
                "detail": str(placement_search.get("summary") or "find a collision-free, powered, input-connected build anchor"),
            }
        )

    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    return tasks[:8]


def _green_circuit_layout_candidate(
    recipe_counts: Counter[str],
    observation: dict[str, Any],
    all_sites: list[dict[str, Any]],
    current_circuit_sites: list[dict[str, Any]],
) -> dict[str, Any]:
    current_cable = int(recipe_counts.get("copper-cable", 0))
    current_circuit = int(recipe_counts.get("electronic-circuit", 0))
    current_circuit = max(current_circuit, 1)
    groups = max(1, (current_circuit + 1) // 2)
    proposed_cable = groups * 3
    proposed_circuit = groups * 2
    before_rate = _green_circuit_rate_per_minute(current_cable, current_circuit)
    after_rate = _green_circuit_rate_per_minute(proposed_cable, proposed_circuit)
    score = 70.0 + min(20.0, max(0.0, after_rate - before_rate) / 12.0)
    if current_cable == 0:
        score += 5.0
    after_entities = _green_circuit_blueprint_entities(groups)
    validation = _blueprint_operability_report(after_entities)
    placement_search = _site_placement_search(
        observation,
        after_entities,
        target_item="electronic-circuit",
        required_inputs=("iron-plate", "copper-plate"),
        all_sites=all_sites,
        preferred_sites=current_circuit_sites,
    )
    site_gate = placement_search.pop("_selected_gate", None)
    if not isinstance(site_gate, dict) or not site_gate:
        site_gate = _site_prebuild_gate(
            observation,
            after_entities,
            target_item="electronic-circuit",
            required_inputs=("iron-plate", "copper-plate"),
            all_sites=all_sites,
            preferred_sites=current_circuit_sites,
        )
    candidate_id = "green-circuit-3-cable-2-circuit-cell"
    return {
        "candidate_id": candidate_id,
        "simulation_only": True,
        "not_applied": True,
        "source": "rate-calculator-style static recipe throughput",
        "target_pattern": "3 copper-cable assemblers belt-feeding 2 electronic-circuit assemblers",
        "requires_build_command": True,
        "requires_site_prebuild_gate": True,
        "build_ready": False,
        "build_ready_blockers": _layout_candidate_build_ready_blockers(
            sandbox_validation=None,
            site_gate=site_gate,
            placement_search=placement_search,
        ),
        "blueprint": _blueprint_export(
            "green-circuit-3-cable-2-circuit-cell",
            after_entities,
            "Simulation-only 3:2 green circuit cell. Validate exact input belts, power, and collision before applying.",
        ),
        "validation": validation,
        "site_prebuild_gate": site_gate,
        "site_placement_search": placement_search,
        "prerequisite_tasks": layout_candidate_prerequisite_tasks(
            candidate_id=candidate_id,
            sandbox_validation=None,
            site_gate=site_gate,
            placement_search=placement_search,
        ),
        "simulation": {
            "before": {
                "copper_cable_assemblers": current_cable,
                "electronic_circuit_assemblers": current_circuit,
                "electronic_circuit_per_minute": round(before_rate, 1),
            },
            "after": {
                "copper_cable_assemblers": proposed_cable,
                "electronic_circuit_assemblers": proposed_circuit,
                "electronic_circuit_per_minute": round(after_rate, 1),
            },
            "delta": {
                "electronic_circuit_per_minute": round(after_rate - before_rate, 1),
                "ratio_error_reduced": True,
                "static_operability": validation["status"],
            },
            "score": round(min(score, 95.0), 1),
        },
        "notes": [
            "Simulation assumes assembling-machine-1 speed and recipe max rates.",
            "Real build still needs sandbox pass, site placement, power, build-item, and input-source validation.",
        ],
    }


def _green_circuit_rate_per_minute(cable_assemblers: int, circuit_assemblers: int) -> float:
    cable_recipe = RECIPES["copper-cable"]
    circuit_recipe = RECIPES["electronic-circuit"]
    assembler_speed = 0.5
    cable_per_minute = (
        cable_assemblers
        * float(cable_recipe.products["copper-cable"])
        / float(cable_recipe.time_seconds)
        * assembler_speed
        * 60.0
    )
    circuit_capacity = (
        circuit_assemblers
        * float(circuit_recipe.products["electronic-circuit"])
        / float(circuit_recipe.time_seconds)
        * assembler_speed
        * 60.0
    )
    cable_limited_circuits = cable_per_minute / float(circuit_recipe.ingredients["copper-cable"])
    return min(circuit_capacity, cable_limited_circuits)


def _green_circuit_blueprint_entities(groups: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    group_count = max(1, min(4, groups))
    for group in range(group_count):
        y = group * 14
        for offset in range(-2, 11):
            _add_entity(entities, "transport-belt", -3, y + offset, direction=SOUTH)
            _add_entity(entities, "transport-belt", 3, y + offset, direction=SOUTH)
            _add_entity(entities, "transport-belt", 9, y + offset, direction=SOUTH)
        for offset in (0, 4, 8):
            _add_entity(entities, "assembling-machine-1", 0, y + offset, recipe="copper-cable")
            _add_entity(entities, "inserter", -2, y + offset, direction=WEST)
            _add_entity(entities, "inserter", 2, y + offset, direction=WEST)
        for offset in (1, 7):
            _add_entity(entities, "assembling-machine-1", 6, y + offset, recipe="electronic-circuit")
            _add_entity(entities, "inserter", 4, y + offset, direction=WEST)
            _add_entity(entities, "inserter", 8, y + offset, direction=EAST)
            _add_entity(entities, "inserter", 6, y + offset + 2, direction=NORTH)
            _add_entity(entities, "iron-chest", 6, y + offset + 3)
        _add_entity(entities, "small-electric-pole", 2, y + 4)
        _add_entity(entities, "small-electric-pole", 7, y + 4)
    return entities


def _smelting_standardization_candidate(item: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    positions = [row.get("position") for row in rows if isinstance(row.get("position"), dict)]
    footprint = _layout_footprint(positions)
    furnace_count = sum(_machine_count(row, "stone-furnace") + _machine_count(row, "steel-furnace") for row in rows)
    if furnace_count <= 0:
        furnace_count = len(rows)
    before_rate = furnace_count * 18.75
    target_columns = max(1, min(4, (furnace_count + 11) // 12))
    after_footprint_area = max(16.0, float(footprint.get("area") or 16.0) * 0.72)
    area_reduction = max(0.0, float(footprint.get("area") or 0.0) - after_footprint_area)
    score = 58.0 + min(22.0, len(rows) * 4.0) + min(12.0, area_reduction / 20.0)
    return {
        "candidate_id": f"{item}-parallel-smelting-columns",
        "simulation_only": True,
        "not_applied": True,
        "source": "blueprint-pattern heuristic plus static furnace throughput",
        "target_pattern": "parallel smelting columns with shared ore/fuel/input and plate output lanes",
        "requires_build_command": True,
        "blueprint": _blueprint_export(
            f"{item}-parallel-smelting-columns",
            _smelting_column_blueprint_entities(item, furnace_count, target_columns),
            "Simulation-only smelting column block. Place near validated ore/fuel inputs; miners and long logistics are not included.",
        ),
        "simulation": {
            "before": {
                "sites": len(rows),
                "furnaces": furnace_count,
                "plate_per_minute": round(before_rate, 1),
                "footprint_area": footprint.get("area"),
            },
            "after": {
                "columns": target_columns,
                "furnaces": furnace_count,
                "plate_per_minute": round(before_rate, 1),
                "estimated_footprint_area": round(after_footprint_area, 1),
            },
            "delta": {
                "plate_per_minute": 0.0,
                "footprint_area": round(-area_reduction, 1),
                "expandability": "higher; repeatable columns can be copied without re-solving local layout",
            },
            "score": round(min(score, 92.0), 1),
        },
    }


def _smelting_column_blueprint_entities(item: str, furnace_count: int, columns: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    columns = max(1, columns)
    rows_per_column = max(1, ceil(max(1, furnace_count) / columns))
    for index in range(max(1, furnace_count)):
        column = index // rows_per_column
        row = index % rows_per_column
        x = column * 8
        y = row * 3
        _add_entity(entities, "transport-belt", x + 1, y - 1, direction=SOUTH)
        _add_entity(entities, "transport-belt", x + 1, y, direction=SOUTH)
        _add_entity(entities, "transport-belt", x + 1, y + 1, direction=SOUTH)
        _add_entity(entities, "inserter", x + 2, y, direction=EAST)
        _add_entity(entities, "stone-furnace", x + 4, y)
        _add_entity(entities, "inserter", x + 6, y, direction=EAST)
        _add_entity(entities, "transport-belt", x + 7, y, direction=SOUTH)
    return entities


def _flow_shortening_candidate(link: dict[str, Any], length: float) -> dict[str, Any]:
    target_length = 36.0 if link.get("item") in {"copper-cable", "iron-gear-wheel"} else 45.0
    saved = max(0.0, length - target_length)
    score = 55.0 + min(30.0, saved / 4.0)
    return {
        "candidate_id": f"shorten-{link.get('item')}-flow",
        "simulation_only": True,
        "not_applied": True,
        "source": "site graph distance optimization",
        "target_pattern": "co-locate producer/consumer or move both onto planned trunk belt",
        "requires_build_command": True,
        "simulation": {
            "before": {"length_tiles": round(length, 1), "status": link.get("status")},
            "after": {"target_length_tiles": target_length, "transport_mode": "local_belt_or_trunk_belt"},
            "delta": {"length_tiles": round(-saved, 1), "lower_buffering_needed": saved > 20},
            "score": round(min(score, 90.0), 1),
        },
    }


def _belt_bottleneck_candidates(
    observation: dict[str, Any],
    sites: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    production_by_item = _site_production_estimate_by_item(sites)
    target_status = observation.get("factory_monitor", {}).get("target_status") if isinstance(observation.get("factory_monitor"), dict) else None
    target_items = target_status.get("items") if isinstance(target_status, dict) and isinstance(target_status.get("items"), list) else []
    desired_by_item = {
        str(row.get("item")): float(row.get("target_per_minute") or 0.0)
        for row in target_items
        if isinstance(row, dict) and row.get("item")
    }
    candidates: list[dict[str, Any]] = []
    belt_capacity = 900.0
    for link in links:
        if not isinstance(link, dict) or str(link.get("kind") or "") != "belt":
            continue
        item = str(link.get("item") or "")
        if not item:
            continue
        required = max(float(production_by_item.get(item, 0.0)), float(desired_by_item.get(item, 0.0)))
        if required <= belt_capacity * 0.75:
            continue
        lanes_needed = max(1, int((required + belt_capacity - 1) // belt_capacity))
        overload = required / belt_capacity
        candidates.append(
            {
                "candidate_id": f"belt-capacity-{item}",
                "simulation_only": True,
                "not_applied": True,
                "source": "rate-calculator-style flow demand versus vanilla yellow-belt capacity",
                "target_pattern": "add parallel belt lanes, splitters, or a trunk bus before adding more consumers",
                "requires_build_command": True,
                "simulation": {
                    "before": {
                        "item": item,
                        "estimated_required_per_minute": round(required, 1),
                        "yellow_belt_capacity_per_minute": belt_capacity,
                        "utilization": round(overload, 2),
                    },
                    "after": {
                        "recommended_lanes": lanes_needed,
                        "estimated_capacity_per_minute": round(lanes_needed * belt_capacity, 1),
                    },
                    "delta": {
                        "capacity_per_minute": round((lanes_needed * belt_capacity) - belt_capacity, 1),
                        "prevents_transport_bottleneck": overload >= 1.0,
                    },
                    "score": round(min(90.0, 55.0 + overload * 20.0), 1),
                },
            }
        )
    return candidates[:6]


def _site_production_estimate_by_item(sites: list[dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for site in sites:
        item = str(site.get("item") or "")
        if not item:
            continue
        rate = 0.0
        if site.get("kind") == "plate_smelting_line":
            furnace_count = max(
                1,
                _machine_count(site, "stone-furnace")
                + _machine_count(site, "steel-furnace")
                + _machine_count(site, "electric-furnace"),
            )
            rate = furnace_count * 18.75
        elif site.get("kind") in {"circuit_automation", "build_item_mall", "assembler_cell"}:
            recipe = RECIPES.get(item)
            assembler_count = max(
                1,
                _machine_count(site, "assembling-machine-1")
                + _machine_count(site, "assembling-machine-2")
                + _machine_count(site, "assembling-machine-3"),
            )
            if recipe and recipe.products.get(item):
                rate = assembler_count * float(recipe.products[item]) / float(recipe.time_seconds) * 0.5 * 60.0
        elif site.get("kind") == "mining_patch":
            drill_count = max(1, _machine_count(site, "burner-mining-drill") + _machine_count(site, "electric-mining-drill"))
            rate = drill_count * 30.0
        if rate > 0:
            output[item] = output.get(item, 0.0) + rate
    return output


def _lab_daisy_chain_candidate(lab_count: int) -> dict[str, Any]:
    before_effective = lab_count * 0.5
    after_effective = lab_count * 0.85
    after_labs = max(1, lab_count)
    return {
        "candidate_id": "lab-short-daisy-chain-feed",
        "simulation_only": True,
        "not_applied": True,
        "source": "research feed pattern heuristic",
        "target_pattern": "short lab daisy chain or multi-feed science belt",
        "requires_build_command": True,
        "blueprint": _blueprint_export(
            "lab-short-daisy-chain-feed",
            _lab_daisy_chain_blueprint_entities(after_labs),
            "Simulation-only lab daisy-chain feed pattern. Validate science input belt and power before applying.",
        ),
        "simulation": {
            "before": {"labs": lab_count, "effective_lab_utilization": 0.5, "effective_labs": round(before_effective, 2)},
            "after": {"labs": lab_count, "effective_lab_utilization": 0.85, "effective_labs": round(after_effective, 2)},
            "delta": {"effective_labs": round(after_effective - before_effective, 2)},
            "score": round(62.0 + min(18.0, lab_count * 2.0), 1),
        },
    }


def _lab_daisy_chain_blueprint_entities(lab_count: int) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    count = max(1, min(8, lab_count))
    for index in range(count):
        x = index * 4
        _add_entity(entities, "lab", x, 0)
        _add_entity(entities, "small-electric-pole", x, -3)
        _add_entity(entities, "transport-belt", x, 3, direction=EAST)
        _add_entity(entities, "inserter", x, 2, direction=NORTH)
        if index < count - 1:
            _add_entity(entities, "inserter", x + 2, 0, direction=EAST)
    return entities


def _mall_compaction_candidate(mall_sites: list[dict[str, Any]]) -> dict[str, Any]:
    positions = [site.get("position") for site in mall_sites if isinstance(site.get("position"), dict)]
    footprint = _layout_footprint(positions)
    before_area = float(footprint.get("area") or 0.0)
    after_area = max(24.0, before_area * 0.65)
    return {
        "candidate_id": "starter-mall-row-compaction",
        "simulation_only": True,
        "not_applied": True,
        "source": "blueprint-pattern heuristic",
        "target_pattern": "starter mall row with shared iron/gear/circuit inputs and chest outputs",
        "requires_build_command": True,
        "blueprint": _blueprint_export(
            "starter-mall-row-compaction",
            _starter_mall_row_blueprint_entities(len(mall_sites)),
            "Simulation-only starter mall row. Validate input belts, recipes, power, and chest positions before applying.",
        ),
        "simulation": {
            "before": {"cells": len(mall_sites), "footprint_area": round(before_area, 1)},
            "after": {"cells": len(mall_sites), "estimated_footprint_area": round(after_area, 1)},
            "delta": {"footprint_area": round(after_area - before_area, 1), "shared_input_lanes": True},
            "score": round(60.0 + min(22.0, max(0.0, before_area - after_area) / 16.0), 1),
        },
    }


def _starter_mall_row_blueprint_entities(cell_count: int) -> list[dict[str, Any]]:
    recipes = [
        "transport-belt",
        "inserter",
        "burner-inserter",
        "stone-furnace",
        "burner-mining-drill",
        "assembling-machine-1",
        "small-electric-pole",
    ]
    entities: list[dict[str, Any]] = []
    count = max(1, min(len(recipes), cell_count or len(recipes)))
    for index, recipe in enumerate(recipes[:count]):
        x = index * 4
        _add_entity(entities, "assembling-machine-1", x, 0, recipe=recipe)
        _add_entity(entities, "inserter", x, -2, direction=SOUTH)
        _add_entity(entities, "transport-belt", x, -3, direction=EAST)
        _add_entity(entities, "inserter", x, 2, direction=NORTH)
        _add_entity(entities, "wooden-chest", x, 3)
        if index % 2 == 0:
            _add_entity(entities, "small-electric-pole", x + 1, 1)
    return entities


def _blueprint_operability_report(entities: list[dict[str, Any]]) -> dict[str, Any]:
    machine_reports: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for machine in entities:
        if not isinstance(machine, dict) or str(machine.get("name") or "") not in ASSEMBLER_ENTITY_NAMES:
            continue
        recipe = str(machine.get("recipe") or "")
        if recipe not in RECIPES:
            continue
        inbound = _inserters_feeding_entity(machine, entities)
        outbound = _inserters_unloading_entity(machine, entities)
        required_inbound = 2 if recipe == "electronic-circuit" else 1
        report = {
            "recipe": recipe,
            "position": machine.get("position"),
            "input_inserters": len(inbound),
            "output_inserters": len(outbound),
            "status": "pass",
        }
        if len(inbound) < required_inbound:
            report["status"] = "fail"
            errors.append(f"{recipe} assembler at {_position_key(machine)} has {len(inbound)}/{required_inbound} input inserters")
        if len(outbound) < 1:
            report["status"] = "fail"
            errors.append(f"{recipe} assembler at {_position_key(machine)} has no output inserter")
        machine_reports.append(report)
    if not machine_reports:
        warnings.append("no recipe assemblers were found for static operability validation")
    if errors:
        status = "fail"
    elif warnings:
        status = "warning"
    else:
        status = "pass"
    return {
        "status": status,
        "checked_machines": len(machine_reports),
        "errors": errors,
        "warnings": warnings,
        "machine_reports": machine_reports[:12],
    }


def _inserters_feeding_entity(machine: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for inserter in _blueprint_inserters(entities):
        endpoints = _inserter_endpoints(inserter)
        if not endpoints:
            continue
        pickup, drop = endpoints
        if _point_inside_machine(drop, machine) and _point_has_source(pickup, entities, exclude=machine):
            rows.append(inserter)
    return rows


def _inserters_unloading_entity(machine: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for inserter in _blueprint_inserters(entities):
        endpoints = _inserter_endpoints(inserter)
        if not endpoints:
            continue
        pickup, drop = endpoints
        if _point_inside_machine(pickup, machine) and _point_has_sink(drop, entities, exclude=machine):
            rows.append(inserter)
    return rows


def _blueprint_inserters(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entity
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("name") or "") in {"inserter", "burner-inserter", "fast-inserter"}
    ]


def _inserter_endpoints(entity: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]] | None:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if position is None:
        return None
    direction = int(entity.get("direction") or 0)
    vectors = {
        NORTH: (0.0, -1.0),
        EAST: (1.0, 0.0),
        SOUTH: (0.0, 1.0),
        WEST: (-1.0, 0.0),
    }
    vector = vectors.get(direction)
    if vector is None:
        return None
    x = float(position.get("x") or 0.0)
    y = float(position.get("y") or 0.0)
    dx, dy = vector
    pickup = {"x": x + dx, "y": y + dy}
    drop = {"x": x - dx, "y": y - dy}
    return pickup, drop


def _point_has_source(point: dict[str, float], entities: list[dict[str, Any]], *, exclude: dict[str, Any]) -> bool:
    return any(_point_entity_match(point, entity, exclude=exclude, source=True) for entity in entities)


def _point_has_sink(point: dict[str, float], entities: list[dict[str, Any]], *, exclude: dict[str, Any]) -> bool:
    return any(_point_entity_match(point, entity, exclude=exclude, source=False) for entity in entities)


def _point_entity_match(point: dict[str, float], entity: dict[str, Any], *, exclude: dict[str, Any], source: bool) -> bool:
    if entity is exclude or not isinstance(entity, dict):
        return False
    name = str(entity.get("name") or "")
    if name in {"transport-belt", "fast-transport-belt", "express-transport-belt", "wooden-chest", "iron-chest", "steel-chest"}:
        return _point_inside_tile_entity(point, entity)
    if name in ASSEMBLER_ENTITY_NAMES:
        recipe = str(entity.get("recipe") or "")
        return _point_inside_machine(point, entity) and (recipe in RECIPES or source)
    return False


def _point_inside_machine(point: dict[str, float], entity: dict[str, Any]) -> bool:
    center = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if center is None:
        return False
    return (
        abs(float(point.get("x") or 0.0) - float(center.get("x") or 0.0)) <= 1.5
        and abs(float(point.get("y") or 0.0) - float(center.get("y") or 0.0)) <= 1.5
    )


def _point_inside_tile_entity(point: dict[str, float], entity: dict[str, Any]) -> bool:
    center = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if center is None:
        return False
    return (
        abs(float(point.get("x") or 0.0) - float(center.get("x") or 0.0)) <= 0.5
        and abs(float(point.get("y") or 0.0) - float(center.get("y") or 0.0)) <= 0.5
    )


def _position_key(entity: dict[str, Any]) -> str:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    return f"{position.get('x')},{position.get('y')}"


def _machine_count(site: dict[str, Any], machine: str) -> int:
    total = 0
    for row in site.get("machines", []):
        text = str(row)
        if not text.startswith(machine):
            continue
        if " x" not in text:
            total += 1
            continue
        try:
            total += int(text.rsplit(" x", 1)[1])
        except ValueError:
            total += 1
    return total


def _blueprint_export(label: str, entities: list[dict[str, Any]], description: str) -> dict[str, Any] | None:
    if not entities:
        return None
    exchange_string = encode_blueprint_entities(label, entities, description=description)
    return {
        "label": label,
        "format": "factorio-blueprint-string",
        "entity_count": len(entities),
        "exchange_string": exchange_string,
    }


def _add_entity(
    entities: list[dict[str, Any]],
    name: str,
    x: float,
    y: float,
    *,
    direction: int | None = None,
    recipe: str | None = None,
    items: dict[str, Any] | None = None,
) -> None:
    entity: dict[str, Any] = {"name": name, "position": {"x": x, "y": y}}
    if direction is not None:
        entity["direction"] = direction
    if recipe:
        entity["recipe"] = recipe
    if items:
        entity["items"] = items
    entities.append(entity)


class IronPlateSkill:
    """Rule-based early-game skill that bootstraps iron plate production."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count

    def next_action(
        self,
        observation: dict[str, Any],
        target_count: int | None = None,
        inventory_only: bool = False,
    ) -> PlannerDecision:
        target = target_count or self.target_count
        iron_total = inventory_count(observation, "iron-plate") if inventory_only else total_item_count(observation, "iron-plate")
        if iron_total >= target:
            return PlannerDecision(None, f"iron plate target reached: {iron_total}/{target}", done=True)
        return _direct_plate_smelting_decision(
            observation,
            target_count=target,
            resource_name="iron-ore",
            product_name="iron-plate",
            support_skill=self,
            inventory_only=inventory_only,
            allow_support_plate=False,
        )

    def _mine_resource(
        self,
        player: dict[str, float],
        resource: dict[str, Any],
        name: str,
        count: int,
    ) -> PlannerDecision:
        pos = _position(resource)
        if distance(player, pos) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": pos, "tolerance": 7.5},
                f"move near {name}",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "target": "resource",
                "name": name,
                "near": pos,
                "radius": 8,
                "count": count,
            },
            f"mine {name}",
        )


class AutomationScienceSkill:
    """Second milestone: produce automation science packs after iron smelting works."""

    def __init__(self, target_count: int = 5, iron_plate_floor: int = 10) -> None:
        self.target_count = target_count
        self.iron_plate_floor = iron_plate_floor
        self.iron_skill = IronPlateSkill(iron_plate_floor)
        self.copper_skill = CopperPlateSkill(target_count)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        science_total = total_item_count(observation, "automation-science-pack")
        if science_total >= self.target_count:
            return PlannerDecision(
                None,
                f"automation science target reached: {science_total}/{self.target_count}",
                done=True,
            )

        if total_item_count(observation, "iron-plate") < self.iron_plate_floor:
            decision = self.iron_skill.next_action(observation)
            if decision.action is not None:
                return decision

        copper_plate_inventory = inventory_count(observation, "copper-plate")
        gear_total = inventory_count(observation, "iron-gear-wheel")
        science_needed = self.target_count - science_total

        if craftable_count(observation, "automation-science-pack") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "automation-science-pack",
                    "count": min(science_needed, craftable_count(observation, "automation-science-pack")),
                },
                "craft automation science packs",
            )

        if gear_total < science_needed and craftable_count(observation, "iron-gear-wheel") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": min(science_needed - gear_total, craftable_count(observation, "iron-gear-wheel")),
                },
                "craft iron gear wheels for automation science",
            )

        if gear_total < science_needed:
            required_iron_for_gears = 2 * (science_needed - gear_total)
            if inventory_count(observation, "iron-plate") < required_iron_for_gears:
                decision = self.iron_skill.next_action(
                    observation,
                    target_count=required_iron_for_gears,
                    inventory_only=True,
                )
                if not decision.done:
                    return decision

        if copper_plate_inventory < science_needed:
            decision = self.copper_skill.next_action(observation, target_count=science_needed, inventory_only=True)
            if not decision.done:
                return decision

        if gear_total < science_needed:
            return PlannerDecision(None, "missing iron gear wheels and cannot craft them")

        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            "wait before rechecking automation science prerequisites",
        )


class CopperPlateSkill:
    """Reusable skill that bootstraps copper directly, then allows belt smelting after belt automation."""

    def __init__(self, target_count: int = 10) -> None:
        self.target_count = target_count
        self.support_skill = IronPlateSkill(target_count=10)

    def next_action(
        self,
        observation: dict[str, Any],
        target_count: int | None = None,
        inventory_only: bool = False,
    ) -> PlannerDecision:
        target = target_count or self.target_count
        copper_total = inventory_count(observation, "copper-plate") if inventory_only else total_item_count(observation, "copper-plate")
        if copper_total >= target:
            return PlannerDecision(None, f"copper plate target reached: {copper_total}/{target}", done=True)

        if _belt_smelting_ready(observation):
            return BeltSmeltingLineSkill(
                target_count=target,
                resource_name="copper-ore",
                product_name="copper-plate",
                inventory_only=inventory_only,
            ).next_action(observation)

        return _direct_plate_smelting_decision(
            observation,
            target_count=target,
            resource_name="copper-ore",
            product_name="copper-plate",
            support_skill=self.support_skill,
            inventory_only=inventory_only,
            allow_support_plate=True,
        )


class StoneSupplySkill:
    """Build a starter stone drill with an output chest instead of repeated hand stone mining."""

    def __init__(self, target_count: int = 16) -> None:
        self.target_count = target_count
        self.support_skill = IronPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        stone_total = inventory_count(observation, "stone")
        if stone_total >= self.target_count:
            return PlannerDecision(None, f"stone target reached: {stone_total}/{self.target_count}", done=True)

        player = player_position(observation)
        layout = _find_stone_supply_layout(observation) or _select_stone_supply_layout(observation)
        if layout is None:
            stone = nearest_resource(observation, "stone")
            if stone is None:
                return PlannerDecision(None, "cannot find stone for starter stone supply")
            return self.support_skill._mine_resource(player, stone, "stone", max(8, self.target_count - stone_total))

        chest = layout.get("output_chest")
        if chest is not None and entity_item_count(chest, "stone") > 0:
            chest_pos = _position(chest)
            if distance(player, chest_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": chest_pos},
                    "move near stone output chest",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "stone",
                    "count": min(50, entity_item_count(chest, "stone")),
                    "unit_number": chest.get("unit_number"),
                    "name": chest.get("name") or "wooden-chest",
                    "position": chest_pos,
                },
                "take stone from starter stone supply chest",
            )

        missing = _stone_supply_missing_item(observation, layout)
        if missing:
            decision = self._ensure_item(observation, player, missing)
            if decision is not None:
                return decision

        if chest is None:
            chest_name = _available_stone_output_chest_name(observation)
            if chest_name is None:
                return PlannerDecision(None, "missing output chest for starter stone supply")
            position = layout["output_position"]
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near planned stone output chest",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": chest_name,
                    "position": position,
                },
                "place output chest for starter stone supply",
            )

        drill = layout.get("drill")
        if drill is None:
            position = layout["drill_position"]
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near planned stone burner mining drill",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "burner-mining-drill",
                    "position": position,
                    "direction": layout["drill_direction"],
                    "required_resource": "stone",
                    "allow_nearby": True,
                },
                "place burner mining drill for starter stone supply",
            )

        if entity_item_count(drill, "coal") < 3:
            return _fuel_burner_line_entity(
                observation,
                player,
                drill,
                entity_name="burner-mining-drill",
                threshold=3,
                insert_count=5,
                context="starter stone supply",
                support_skill=self.support_skill,
                far_fuel_reason="starter stone supply needs local fuel before the drill can run",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 240},
            "wait for starter stone drill to fill the output chest",
        )

    def _ensure_item(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
    ) -> PlannerDecision | None:
        if item == "burner-mining-drill":
            if craftable_count(observation, "burner-mining-drill") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                    "craft burner mining drill for starter stone supply",
                )
            if inventory_count(observation, "stone") < 5:
                stone = nearest_resource(observation, "stone")
                if stone is None:
                    return PlannerDecision(None, "cannot find bootstrap stone for burner drill")
                return self.support_skill._mine_resource(player, stone, "stone", 8)
            if inventory_count(observation, "iron-gear-wheel") < 3 and craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(3 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for stone supply drill",
                )
            return self.support_skill.next_action(observation, target_count=20, inventory_only=True)

        if item in {"wooden-chest", "iron-chest"}:
            if craftable_count(observation, item) > 0:
                return PlannerDecision({"type": "craft", "recipe": item, "count": 1}, f"craft {item} for stone output")
            if item == "wooden-chest" and inventory_count(observation, "wood") < 2:
                tree = _nearest_tree(observation)
                if tree is None:
                    if craftable_count(observation, "iron-chest") > 0:
                        return PlannerDecision({"type": "craft", "recipe": "iron-chest", "count": 1}, "craft iron chest for stone output")
                    return self.support_skill.next_action(observation, target_count=8, inventory_only=True)
                tree_pos = _position(tree)
                if distance(player, tree_pos) > 8:
                    return PlannerDecision({"type": "move_to", "position": tree_pos}, "move near tree for stone output chest")
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": tree.get("name"),
                        "position": tree_pos,
                        "count": 1,
                    },
                    "mine tree for stone output chest",
                )
            if item == "iron-chest":
                return self.support_skill.next_action(observation, target_count=8, inventory_only=True)
        return None


class ElectronicCircuitSkill:
    """Craft early electronic circuits by ensuring iron plates, copper plates, and copper cable."""

    def __init__(self, target_count: int = 5) -> None:
        self.target_count = target_count
        self.iron_skill = IronPlateSkill(max(10, target_count))
        self.copper_skill = CopperPlateSkill(max(10, _ceil_div(target_count * 3, 2)))

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        circuit_total = total_item_count(observation, "electronic-circuit")
        if circuit_total >= self.target_count:
            return PlannerDecision(
                None,
                f"electronic circuit target reached: {circuit_total}/{self.target_count}",
                done=True,
            )

        missing_circuits = self.target_count - circuit_total
        craftable_circuits = craftable_count(observation, "electronic-circuit")
        if craftable_circuits > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "electronic-circuit",
                    "count": min(missing_circuits, craftable_circuits),
                },
                "craft electronic circuits",
            )

        required_cables = missing_circuits * 3
        cable_inventory = inventory_count(observation, "copper-cable")
        if cable_inventory < required_cables:
            craftable_cable = craftable_count(observation, "copper-cable")
            if craftable_cable > 0:
                cable_crafts_needed = _ceil_div(required_cables - cable_inventory, 2)
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "copper-cable",
                        "count": min(cable_crafts_needed, craftable_cable),
                    },
                    "craft copper cable for electronic circuits",
                )

            copper_plates_needed = _ceil_div(required_cables - cable_inventory, 2)
            if inventory_count(observation, "copper-plate") < copper_plates_needed:
                decision = self.copper_skill.next_action(
                    observation,
                    target_count=copper_plates_needed,
                    inventory_only=True,
                )
                if not decision.done:
                    return decision

        iron_plates_needed = missing_circuits
        if inventory_count(observation, "iron-plate") < iron_plates_needed:
            decision = self.iron_skill.next_action(
                observation,
                target_count=iron_plates_needed,
                inventory_only=True,
            )
            if not decision.done:
                return decision

        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            "wait before rechecking electronic circuit prerequisites",
        )


class BeltSmeltingLineSkill:
    """Build a minimal belt-fed burner smelting line for early plate automation."""

    def __init__(
        self,
        target_count: int = 10,
        resource_name: str = "iron-ore",
        product_name: str = "iron-plate",
        inventory_only: bool = False,
    ) -> None:
        self.target_count = target_count
        self.resource_name = resource_name
        self.product_name = product_name
        self.inventory_only = inventory_only
        self.support_skill = IronPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        line = _find_belt_smelting_line(observation, self.resource_name)
        line_furnace = line.get("furnace") if line else None
        total_product = (
            inventory_count(observation, self.product_name)
            if self.inventory_only
            else total_item_count(observation, self.product_name)
        )
        if line_furnace and self._line_has_started(line_furnace) and total_product >= self.target_count:
            return PlannerDecision(
                None,
                f"belt smelting line produced {self.product_name}: {total_product}/{self.target_count}",
                done=True,
            )

        player = player_position(observation)
        layout = line or _select_belt_smelting_layout(observation, self.resource_name)
        if layout is None:
            return PlannerDecision(None, f"cannot find open {self.resource_name} site for belt smelting line")

        need = _line_missing_item(observation, layout)
        if need:
            decision = self._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt1_position", "belt_direction"),
            ("transport-belt", "belt2_position", "belt_direction"),
            ("burner-inserter", "inserter_position", "inserter_direction"),
            ("stone-furnace", "furnace_position", None),
            ("burner-mining-drill", "drill_position", "drill_direction"),
        ]:
            entity_key = _entity_key_for_layout(name, key)
            if layout.get(entity_key) is not None:
                continue
            position = layout[key]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position)},
                    f"move near planned {name} position",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": name in {"burner-mining-drill", "stone-furnace"},
            }
            if name == "burner-mining-drill":
                action["required_resource"] = self.resource_name
            direction = layout.get(direction_key) if direction_key else None
            if direction is not None:
                action["direction"] = direction
            return PlannerDecision(action, f"place {name} for belt smelting line")

        for entity_name, item, threshold, count in [
            ("burner-mining-drill", "coal", 3, 5),
            ("burner-inserter", "coal", 2, 3),
            ("stone-furnace", "coal", 3, 5),
        ]:
            entity = layout.get(_entity_key(entity_name))
            if entity and entity_item_count(entity, item) < threshold:
                return _fuel_burner_line_entity(
                    observation,
                    player,
                    entity,
                    entity_name=entity_name,
                    threshold=threshold,
                    insert_count=count,
                    context="belt smelting line",
                    support_skill=self.support_skill,
                    far_fuel_reason="burner smelting line needs fuel logistics before more walking refuels",
                )

        layout_furnace = layout.get("furnace")
        if layout_furnace and entity_item_count(layout_furnace, self.product_name) > 0:
            furnace_pos = _position(layout_furnace)
            if distance(player, furnace_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": furnace_pos},
                    "move near automated furnace output",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": self.product_name,
                    "count": min(50, entity_item_count(layout_furnace, self.product_name)),
                    "unit_number": layout_furnace.get("unit_number"),
                    "name": "stone-furnace",
                    "position": furnace_pos,
                },
                f"take {self.product_name} produced by belt smelting line",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for belt smelting line to move ore and smelt plates",
        )

    def _ensure_item(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
    ) -> PlannerDecision | None:
        if item == "stone-furnace":
            if craftable_count(observation, "stone-furnace") > 0:
                return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft furnace for line")
            decision = StoneSupplySkill(target_count=8).next_action(observation)
            if decision.done:
                return PlannerDecision(
                    {"type": "wait", "ticks": 60},
                    "stone supply is ready; wait for furnace craftability to refresh",
                )
            return decision

        if item == "burner-mining-drill":
            if craftable_count(observation, "burner-mining-drill") > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                    "craft burner mining drill for line",
                )
            if inventory_count(observation, "stone") < 5:
                decision = StoneSupplySkill(target_count=8).next_action(observation)
                if not decision.done:
                    return decision
            if inventory_count(observation, "iron-gear-wheel") < 3 and craftable_count(observation, "iron-gear-wheel") > 0:
                if bool(_technology_state(observation, "automation").get("researched")):
                    decision = BuildItemMallSkill("iron-gear-wheel", 3).next_action(observation)
                    if not decision.done:
                        return decision
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(3 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for line drill",
                )
            return self.support_skill.next_action(observation, target_count=20, inventory_only=True)

        if item in {"transport-belt", "burner-inserter"}:
            if inventory_count(observation, "iron-gear-wheel") < 1 and craftable_count(observation, "iron-gear-wheel") > 0:
                if bool(_technology_state(observation, "automation").get("researched")):
                    decision = BuildItemMallSkill("iron-gear-wheel", 3).next_action(observation)
                    if not decision.done:
                        return decision
                return PlannerDecision(
                    {"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
                    f"craft gear for {item}",
                )
            if (
                item == "transport-belt"
                and bool(_technology_state(observation, "automation").get("researched"))
                and not _belt_smelting_ready(observation)
            ):
                decision = BuildItemMallSkill("transport-belt", 20).next_action(observation)
                if not decision.done:
                    return decision
            if craftable_count(observation, item) > 0:
                return PlannerDecision({"type": "craft", "recipe": item, "count": 1}, f"craft {item} for line")
            return self.support_skill.next_action(observation, target_count=20, inventory_only=True)

        return None

    def _line_has_started(self, furnace: dict[str, Any]) -> bool:
        return entity_item_count(furnace, self.resource_name) > 0 or entity_item_count(furnace, self.product_name) > 0


class CoalSupplySkill:
    """Build a minimal burner coal supply site for early fuel logistics."""

    def __init__(self, target_count: int = 16) -> None:
        self.target_count = target_count
        self.support_skill = BeltSmeltingLineSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        layout = _find_coal_supply_layout(observation) or _select_coal_supply_layout(observation)
        if layout is None:
            return PlannerDecision(None, "cannot find open coal patch for coal supply site")

        need = _coal_supply_missing_item(observation, layout)
        if need:
            decision = self.support_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        belt = layout.get("output_belt")
        if belt is None:
            position = layout["output_position"]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing coal output belt",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing coal output belt",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near planned coal output belt",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "transport-belt",
                    "position": position,
                    "direction": layout["belt_direction"],
                },
                "place output belt for coal supply site",
            )

        drill = layout.get("drill")
        if drill is None:
            position = layout["drill_position"]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing coal drill",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing coal drill",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near planned coal burner mining drill",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "burner-mining-drill",
                    "position": position,
                    "direction": layout["drill_direction"],
                    "required_resource": "coal",
                    "allow_nearby": True,
                },
                "place burner mining drill on coal supply patch",
            )

        if entity_item_count(drill, "coal") < 12:
            return _fuel_burner_line_entity(
                observation,
                player,
                drill,
                entity_name="burner-mining-drill",
                threshold=12,
                insert_count=16,
                context="coal supply site",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="coal supply site is too far from available hand fuel; build closer coal logistics first",
            )

        belt_coal = entity_item_count(belt, "coal")
        if belt_coal > 0 and total_item_count(observation, "coal") < self.target_count:
            belt_position = _position(belt)
            if distance(player, belt_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": belt_position},
                    "move near coal supply output belt",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "coal",
                    "count": min(16, belt_coal),
                    "unit_number": belt.get("unit_number"),
                    "name": "transport-belt",
                    "position": belt_position,
                },
                "take coal from new supply belt for starter fuel stock",
            )

        return PlannerDecision(
            None,
            "coal supply site is active with fueled burner mining drill and output belt",
            done=True,
        )


class CoalFuelFeedSkill:
    """Connect a starter coal belt to a nearby burner fuel consumer."""

    def __init__(self) -> None:
        self.support_skill = BeltSmeltingLineSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        layout = _coal_fuel_feed_layout(observation)
        if layout is None:
            supply = CoalSupplySkill(target_count=16).next_action(observation)
            if not supply.done:
                return supply
            return PlannerDecision(None, "coal supply exists but no output belt is available for fuel feed")

        need = _coal_fuel_feed_missing_item(observation, layout)
        if need:
            decision = self.support_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt2_position", "belt_direction"),
            ("burner-inserter", "inserter_position", "inserter_direction"),
            ("stone-furnace", "consumer_position", None),
        ]:
            existing_key = _coal_fuel_feed_entity_key(name)
            if layout.get(existing_key) is not None:
                continue
            position = layout[key]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing coal fuel feed {name}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing coal fuel feed {name}",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    f"move near planned coal fuel feed {name}",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": name == "stone-furnace",
            }
            direction = layout.get(direction_key) if direction_key else None
            if direction is not None:
                action["direction"] = direction
            return PlannerDecision(action, f"place {name} for coal fuel feed")

        inserter = layout.get("inserter")
        if inserter and entity_item_count(inserter, "coal") < 1:
            return _fuel_burner_line_entity(
                observation,
                player,
                inserter,
                entity_name="burner-inserter",
                threshold=1,
                insert_count=1,
                context="coal fuel feed",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="coal fuel feed needs local starter fuel before the burner inserter can move coal",
            )

        source_drill = layout.get("source_drill")
        if source_drill and entity_item_count(source_drill, "coal") < 1:
            return _fuel_burner_line_entity(
                observation,
                player,
                source_drill,
                entity_name="burner-mining-drill",
                threshold=1,
                insert_count=2,
                context="coal fuel feed source drill",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="coal fuel feed source drill needs local starter fuel before the feed can stay active",
            )

        consumer = layout.get("consumer")
        if consumer and entity_item_count(consumer, "coal") > 0:
            return PlannerDecision(
                None,
                "coal fuel feed is active: belt and burner inserter are feeding a furnace fuel inventory",
                done=True,
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 180},
            "wait for coal fuel feed inserter to move coal into the fuel consumer",
        )


class _ExpandPlateSmeltingSkill:
    """Incrementally add belt-fed plate smelting capacity."""

    def __init__(self, resource_name: str, product_name: str, target_rate_per_minute: float) -> None:
        self.resource_name = resource_name
        self.product_name = product_name
        self.target_rate_per_minute = target_rate_per_minute
        self.line_skill = BeltSmeltingLineSkill(
            target_count=20,
            resource_name=resource_name,
            product_name=product_name,
        )

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        low_fuel_layout = _find_low_fuel_belt_smelting_line(observation, self.resource_name)
        if low_fuel_layout is not None:
            decision = self._fuel_line_to_reserve(observation, player, low_fuel_layout)
            if decision is not None:
                return decision

        estimated_rate = _estimated_plate_rate(observation, self.product_name, self.resource_name)
        if estimated_rate >= self.target_rate_per_minute:
            return PlannerDecision(
                None,
                f"{self.product_name} smelting capacity target reached: {estimated_rate}/{self.target_rate_per_minute}/min",
                done=True,
            )

        layout = (
            _find_unfueled_belt_smelting_line(observation, self.resource_name)
            or _find_incomplete_belt_smelting_line(observation, self.resource_name)
            or _select_belt_smelting_layout(
                observation,
                self.resource_name,
            )
        )
        if layout is None:
            return PlannerDecision(None, f"cannot find open {self.resource_name} site for another smelting line")

        need = _line_missing_item(observation, layout)
        if need:
            decision = self.line_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt1_position", "belt_direction"),
            ("transport-belt", "belt2_position", "belt_direction"),
            ("burner-inserter", "inserter_position", "inserter_direction"),
            ("stone-furnace", "furnace_position", None),
            ("burner-mining-drill", "drill_position", "drill_direction"),
        ]:
            entity_key = _entity_key_for_layout(name, key)
            if layout.get(entity_key) is not None:
                continue
            position = layout[key]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing {name}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing {name}",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    f"move near planned {name} position for {self.product_name} smelting expansion",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": name in {"burner-mining-drill", "stone-furnace"},
            }
            if name == "burner-mining-drill":
                action["required_resource"] = self.resource_name
            direction = layout.get(direction_key) if direction_key else None
            if direction is not None:
                action["direction"] = direction
            return PlannerDecision(action, f"place {name} for expanded {self.product_name} smelting")

        reserve_decision = self._fuel_line_to_reserve(observation, player, layout)
        if reserve_decision is not None:
            return reserve_decision

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            f"wait for expanded {self.product_name} smelting line to start",
        )

    def _fuel_line_to_reserve(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        layout: dict[str, Any],
    ) -> PlannerDecision | None:
        line_units = _smelting_line_fuel_unit_numbers(observation, self.resource_name)
        line_units.update(
            layout[key].get("unit_number")
            for key in ("drill", "inserter", "furnace")
            if isinstance(layout.get(key), dict)
        )
        for entity_name, layout_key in [
            ("burner-mining-drill", "drill"),
            ("burner-inserter", "inserter"),
            ("stone-furnace", "furnace"),
        ]:
            entity = layout.get(layout_key)
            if entity and entity_item_count(entity, "coal") < 1:
                return _fuel_burner_line_entity(
                    observation,
                    player,
                    entity,
                    entity_name=entity_name,
                    threshold=1,
                    insert_count=1,
                    context=f"expanded {self.product_name} smelting operating fuel",
                    support_skill=self.line_skill.support_skill,
                    far_fuel_reason=f"expanded {self.product_name} smelting needs fuel logistics before more walking refuels",
                    exclude_source_units=line_units,
                )

        for entity_name, layout_key in [
            ("burner-mining-drill", "drill"),
            ("burner-inserter", "inserter"),
            ("stone-furnace", "furnace"),
        ]:
            entity = layout.get(layout_key)
            threshold = SMELTING_LINE_FUEL_RESERVE[layout_key]
            if entity and entity_item_count(entity, "coal") < threshold:
                return _fuel_burner_line_entity(
                    observation,
                    player,
                    entity,
                    entity_name=entity_name,
                    threshold=threshold,
                    insert_count=SMELTING_LINE_FUEL_INSERT[layout_key],
                    context=f"expanded {self.product_name} smelting reserve",
                    support_skill=self.line_skill.support_skill,
                    far_fuel_reason=f"expanded {self.product_name} smelting needs fuel logistics before more walking refuels",
                    exclude_source_units=line_units,
                )
        return None


class ExpandIronSmeltingSkill(_ExpandPlateSmeltingSkill):
    """Incrementally add belt-fed iron smelting capacity."""

    def __init__(self, target_rate_per_minute: float = 90.0) -> None:
        super().__init__("iron-ore", "iron-plate", target_rate_per_minute)


class ExpandCopperSmeltingSkill(_ExpandPlateSmeltingSkill):
    """Incrementally add belt-fed copper smelting capacity."""

    def __init__(self, target_rate_per_minute: float = 75.0) -> None:
        super().__init__("copper-ore", "copper-plate", target_rate_per_minute)


class StarterDefenseSkill:
    """Build a minimal firearm-magazine gun turret defense around the starter factory."""

    def __init__(self, magazine_target: int = 10) -> None:
        self.magazine_target = magazine_target
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=10)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        enemy = _nearest_observed_enemy(observation)
        if enemy is None:
            return PlannerDecision(None, "no observed enemies in threat radius", done=True)

        player = player_position(observation)
        factory_center = _factory_defense_center(observation)
        turret = _nearest_loaded_or_empty_turret(observation, factory_center)
        if turret and _is_factory_defense_turret(turret, factory_center) and entity_item_count(turret, "firearm-magazine") >= 5:
            return PlannerDecision(
                None,
                f"starter factory perimeter has an armed gun turret; nearest enemy is {enemy.get('distance')} tiles away",
                done=True,
            )

        if inventory_count(observation, "firearm-magazine") < self.magazine_target:
            if craftable_count(observation, "firearm-magazine") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "firearm-magazine",
                        "count": min(self.magazine_target - inventory_count(observation, "firearm-magazine"), craftable_count(observation, "firearm-magazine")),
                    },
                    "craft firearm magazines for starter defense",
                )
            decision = self.iron_skill.next_action(observation, target_count=40, inventory_only=True)
            if not decision.done:
                return decision

        if turret is None:
            if inventory_count(observation, "gun-turret") <= 0:
                decision = self._ensure_turret_item(observation)
                if decision is not None:
                    return decision
            position = _defense_position(observation, _position(enemy))
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=-3.0)},
                    "move near planned starter factory perimeter turret position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "gun-turret",
                    "position": position,
                    "allow_nearby": True,
                },
                "place starter defense gun turret on the factory perimeter facing the threat",
            )

        turret_position = _position(turret)
        if distance(player, turret_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": turret_position},
                "move near starter factory defense turret to insert ammunition",
            )
        return PlannerDecision(
            {
                "type": "insert",
                "item": "firearm-magazine",
                "count": min(inventory_count(observation, "firearm-magazine"), self.magazine_target),
                "unit_number": turret.get("unit_number"),
                "name": "gun-turret",
                "position": turret_position,
            },
            "arm starter factory perimeter turret with firearm magazines",
        )

    def _ensure_turret_item(self, observation: dict[str, Any]) -> PlannerDecision | None:
        if craftable_count(observation, "gun-turret") > 0:
            return PlannerDecision({"type": "craft", "recipe": "gun-turret", "count": 1}, "craft gun turret for starter defense")
        if inventory_count(observation, "iron-gear-wheel") < 10 and craftable_count(observation, "iron-gear-wheel") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": min(10 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                },
                "craft gears for starter defense turret",
            )
        if inventory_count(observation, "copper-plate") < 5:
            decision = self.copper_skill.next_action(observation, target_count=5, inventory_only=True)
            if not decision.done:
                return decision
        if inventory_count(observation, "iron-plate") < 30:
            decision = self.iron_skill.next_action(observation, target_count=30, inventory_only=True)
            if not decision.done:
                return decision
        return PlannerDecision(None, "gun turret recipe is not craftable from current state")


class SetupPowerSkill:
    """Build the first steam power block: offshore pump, boiler, engine, and pole."""

    def __init__(self) -> None:
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=10)
        self.circuit_skill = ElectronicCircuitSkill(target_count=2)

    def next_action(
        self,
        observation: dict[str, Any],
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision:
        block = _find_steam_power_block(
            observation,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
        if _steam_power_ready(block):
            return PlannerDecision(None, "steam power block is producing usable steam power", done=True)

        player = player_position(observation)
        layout = block or _select_power_layout(observation)
        if layout is None:
            if _has_remote_power_site(observation):
                return PlannerDecision(
                    None,
                    "cannot use remote water for starter steam power until a connected power corridor or co-located remote factory site exists",
                )
            return PlannerDecision(None, "cannot find a buildable water site near the starter logistics area for steam power")
        layout = _power_layout_with_existing_entities(observation, layout)

        missing = _missing_power_item(observation, layout)
        if missing:
            decision = self._ensure_item_quantity(observation, player, missing, _power_item_required_count(missing))
            if decision is not None:
                return decision

        for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
            if layout.get(key) is not None:
                continue
            spec = layout[f"{key}_spec"]
            position = spec["position"]
            remote_prefix = "remote bootstrap " if layout.get("remote_bootstrap_power") else ""
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _power_stand_position(layout)},
                    f"move near planned {remote_prefix}{spec['name']} position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": spec["name"],
                    "position": position,
                    "direction": spec.get("direction", NORTH),
                },
                f"place {remote_prefix}{spec['name']} for first steam power block",
            )

        boiler = layout.get("boiler")
        if boiler and entity_item_count(boiler, "coal") < 3:
            if inventory_count(observation, "coal") < 5:
                coal = nearest_resource(observation, "coal")
                if coal is None:
                    return PlannerDecision(None, "cannot find coal to fuel boiler")
                return self.iron_skill._mine_resource(player, coal, "coal", 10)
            boiler_pos = _position(boiler)
            if distance(player, boiler_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": boiler_pos},
                    "move near boiler to insert coal",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "coal",
                    "count": min(10, inventory_count(observation, "coal")),
                    "unit_number": boiler.get("unit_number"),
                    "name": "boiler",
                    "position": boiler_pos,
                },
                "fuel boiler for first steam power block",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for offshore pump, boiler, and steam engine to fill with steam",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for steam power",
            )

        if item == "pipe":
            return self._ensure_iron_plates(observation, quantity - inventory_count(observation, "pipe"))
        if item == "iron-gear-wheel":
            return self._ensure_iron_plates(observation, 2 * (quantity - inventory_count(observation, "iron-gear-wheel")))
        if item == "copper-cable":
            return self._ensure_copper_plates(observation, _ceil_div(quantity - inventory_count(observation, "copper-cable"), 2))
        if item == "stone-furnace":
            if craftable_count(observation, "stone-furnace") > 0:
                return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft furnace for boiler")
            decision = StoneSupplySkill(target_count=8).next_action(observation)
            if decision.done:
                return PlannerDecision(
                    {"type": "wait", "ticks": 60},
                    "stone supply is ready; wait for boiler furnace craftability to refresh",
                )
            return decision
        if item == "small-electric-pole":
            if inventory_count(observation, "wood") < 1:
                tree = _nearest_tree(observation)
                if tree is None:
                    return PlannerDecision(None, "cannot find a tree for small electric poles")
                tree_pos = _position(tree)
                if distance(player, tree_pos) > 8:
                    return PlannerDecision({"type": "move_to", "position": tree_pos}, "move near tree for pole wood")
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": tree.get("name"),
                        "position": tree_pos,
                        "count": 1,
                    },
                    "mine tree for pole wood",
                )
            return self._ensure_item_quantity(observation, player, "copper-cable", 2)
        if item == "boiler":
            decision = self._ensure_item_quantity(observation, player, "stone-furnace", 1)
            if decision is not None:
                return decision
            return self._ensure_item_quantity(observation, player, "pipe", 4)
        if item == "steam-engine":
            for prerequisite, count in [("iron-gear-wheel", 8), ("pipe", 5), ("iron-plate", 10)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None
        if item == "offshore-pump":
            for prerequisite, count in [("electronic-circuit", 2), ("pipe", 1), ("iron-gear-wheel", 1)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None
        if item == "electronic-circuit":
            decision = self.circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None
        if item == "iron-plate":
            return self._ensure_iron_plates(observation, quantity)
        if item == "copper-plate":
            return self._ensure_copper_plates(observation, quantity)

        return PlannerDecision(None, f"missing {item} and no prerequisite path is implemented")

    def _ensure_iron_plates(self, observation: dict[str, Any], quantity: int) -> PlannerDecision | None:
        if inventory_count(observation, "iron-plate") >= quantity:
            return None
        decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
        if decision.done:
            return None
        return decision

    def _ensure_copper_plates(self, observation: dict[str, Any], quantity: int) -> PlannerDecision | None:
        if inventory_count(observation, "copper-plate") >= quantity:
            return None
        decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
        if decision.done:
            return None
        return decision


def _position(entity: dict[str, Any]) -> dict[str, float]:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    return {
        "x": float(position.get("x") or 0.0),
        "y": float(position.get("y") or 0.0),
    }


def _stand_position(target: dict[str, float], offset: float = 2.0) -> dict[str, float]:
    return {"x": float(target["x"]) + offset, "y": float(target["y"])}


def _select_copper_furnace(observation: dict[str, Any]) -> dict[str, Any] | None:
    furnaces = _entities_within_starter_area(observation, entities_named(observation, "stone-furnace"))
    for item in furnaces:
        if entity_item_count(item, "copper-plate") > 0 or entity_item_count(item, "copper-ore") > 0:
            return item
    copper = nearest_resource(observation, "copper-ore")
    if copper is None or not furnaces:
        return None
    iron_busy = [item for item in furnaces if _is_iron_busy_furnace(item)]
    if len(furnaces) == 1 and iron_busy:
        return None
    candidates = [item for item in furnaces if item not in iron_busy] or furnaces
    near = _near_position(candidates, _position(copper), FURNACE_RESOURCE_RADIUS)
    return _nearest_to(near, _position(copper)) if near else None


def _select_iron_furnace(observation: dict[str, Any]) -> dict[str, Any] | None:
    furnaces = _entities_within_starter_area(observation, entities_named(observation, "stone-furnace"))
    for item in furnaces:
        if _is_iron_busy_furnace(item):
            return item
    iron = nearest_resource(observation, "iron-ore")
    if iron is None or not furnaces:
        return None
    copper_busy = [item for item in furnaces if _is_copper_busy_furnace(item)]
    if len(furnaces) == 1 and copper_busy:
        return None
    candidates = [item for item in furnaces if item not in copper_busy] or furnaces
    near = _near_position(candidates, _position(iron), FURNACE_RESOURCE_RADIUS)
    return _nearest_to(near, _position(iron)) if near else None


def _is_iron_busy_furnace(entity: dict[str, Any]) -> bool:
    return entity_item_count(entity, "iron-plate") > 0 or entity_item_count(entity, "iron-ore") > 0


def _is_copper_busy_furnace(entity: dict[str, Any]) -> bool:
    return entity_item_count(entity, "copper-plate") > 0 or entity_item_count(entity, "copper-ore") > 0


def _is_busy_furnace_for(entity: dict[str, Any], resource_name: str, product_name: str) -> bool:
    has_material = entity_item_count(entity, product_name) > 0 or entity_item_count(entity, resource_name) > 0
    return has_material and entity_item_count(entity, "coal") > 0


def _near_position(
    entities: list[dict[str, Any]],
    position: dict[str, float],
    radius: float,
) -> list[dict[str, Any]]:
    return [item for item in entities if distance(_position(item), position) <= radius]


def _entities_within_starter_area(observation: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and isinstance(entity.get("position"), dict)
        and _within_starter_logistics_area(observation, _position(entity))
    ]


def _nearest_to(entities: list[dict[str, Any]], position: dict[str, float]) -> dict[str, Any] | None:
    if not entities:
        return None
    return min(entities, key=lambda item: distance(_position(item), position))


def _nearest_resource_to_position(
    observation: dict[str, Any],
    position: dict[str, float],
    resource_name: str,
) -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [item for item in resources if isinstance(item, dict) and item.get("name") == resource_name]
    return _nearest_to(candidates, position) if candidates else None


def _base_anchor_position(observation: dict[str, Any]) -> dict[str, float] | None:
    base = observation.get("base") if isinstance(observation.get("base"), dict) else {}
    for key in ("anchor_position", "spawn_position"):
        value = base.get(key)
        if isinstance(value, dict) and isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float)):
            return _xy_position(value)
    return None


def _starter_logistics_anchors(observation: dict[str, Any]) -> list[dict[str, float]]:
    base_anchor = _base_anchor_position(observation)
    anchors: list[dict[str, float]] = [base_anchor] if base_anchor is not None else [player_position(observation)]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    local_positions = [
        _position(entity)
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in _FACTORY_DEFENSE_ENTITY_NAMES
        and (
            base_anchor is None
            or distance(base_anchor, _position(entity)) <= STARTER_ENTITY_CLUSTER_RADIUS
        )
    ]
    if local_positions:
        centroid = _centroid(local_positions)
        if centroid is not None:
            anchors.append(centroid)
    return anchors


def _within_starter_logistics_area(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    radius: float = STARTER_SITE_RADIUS,
) -> bool:
    return any(distance(anchor, position) <= radius for anchor in _starter_logistics_anchors(observation))


def _within_allowed_factory_area(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
    radius: float = STARTER_SITE_RADIUS,
    reference_radius: float = 48.0,
) -> bool:
    if _within_starter_logistics_area(observation, position, radius=radius):
        return True
    return bool(
        allow_existing_remote
        and reference_position is not None
        and distance(position, reference_position) <= reference_radius
    )


def _distance_to_starter_logistics_area(observation: dict[str, Any], position: dict[str, float]) -> float:
    return min((distance(anchor, position) for anchor in _starter_logistics_anchors(observation)), default=999999.0)


def _layout_center_position(layout: dict[str, Any]) -> dict[str, float]:
    for key in (
        "furnace_position",
        "drill_position",
        "belt1_position",
        "lab_position",
        "assembler_position",
        "cable_assembler_position",
        "circuit_assembler_position",
        "pole_position",
    ):
        value = layout.get(key)
        if isinstance(value, dict):
            return _xy_position(value)
    for key in ("furnace", "drill", "belt1", "lab", "assembler", "cable_assembler", "circuit_assembler", "pole"):
        value = layout.get(key)
        if isinstance(value, dict):
            return _position(value)
    return player_position({})


def _layout_within_starter_area(
    observation: dict[str, Any],
    layout: dict[str, Any],
    *,
    radius: float = STARTER_SITE_RADIUS,
) -> bool:
    return _within_starter_logistics_area(observation, _layout_center_position(layout), radius=radius)


def _select_mining_drill_for_resource(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    raw_drills = entities_named(observation, "burner-mining-drill") + entities_named(observation, "electric-mining-drill")
    drills = []
    for item in _entities_within_starter_area(observation, raw_drills):
        nearest = _nearest_resource_to_position(observation, _position(item), resource_name)
        if nearest is not None and distance(_position(item), _position(nearest)) <= 4.5:
            drills.append(item)
    return _nearest_to(drills, _position(nearest_resource(observation, resource_name) or {"position": {"x": 0, "y": 0}})) if drills else None


def _nearest_observed_enemy(observation: dict[str, Any]) -> dict[str, Any] | None:
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        return None
    candidates = [item for item in enemies if isinstance(item, dict) and isinstance(item.get("position"), dict)]
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item.get("distance") or 999999))


_FACTORY_DEFENSE_ENTITY_NAMES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "boiler",
    "burner-inserter",
    "burner-mining-drill",
    "electric-mining-drill",
    "inserter",
    "lab",
    "offshore-pump",
    "small-electric-pole",
    "steam-engine",
    "stone-furnace",
    "transport-belt",
}
_FACTORY_DEFENSE_RADIUS = 18.0
_FACTORY_DEFENSE_TURRET_RADIUS = 32.0


def _nearest_loaded_or_empty_turret(
    observation: dict[str, Any],
    center: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    turrets = entities_named(observation, "gun-turret")
    if not turrets:
        return None
    target = center or player_position(observation)
    return min(turrets, key=lambda item: distance(target, _position(item)))


def _factory_defense_center(observation: dict[str, Any]) -> dict[str, float]:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return player_position(observation)
    positions = [
        _position(item)
        for item in entities
        if isinstance(item, dict) and item.get("name") in _FACTORY_DEFENSE_ENTITY_NAMES
    ]
    if not positions:
        return player_position(observation)
    return {
        "x": round(sum(item["x"] for item in positions) / len(positions), 2),
        "y": round(sum(item["y"] for item in positions) / len(positions), 2),
    }


def _is_factory_defense_turret(turret: dict[str, Any], factory_center: dict[str, float]) -> bool:
    return distance(_position(turret), factory_center) <= _FACTORY_DEFENSE_TURRET_RADIUS


def _defense_position(observation: dict[str, Any], enemy: dict[str, float]) -> dict[str, float]:
    center = _factory_defense_center(observation)
    dx = enemy["x"] - center["x"]
    dy = enemy["y"] - center["y"]
    length = max((dx * dx + dy * dy) ** 0.5, 0.001)
    return {
        "x": round(center["x"] + _FACTORY_DEFENSE_RADIUS * dx / length, 2),
        "y": round(center["y"] + _FACTORY_DEFENSE_RADIUS * dy / length, 2),
    }


def _resource_name_near_position(
    observation: dict[str, Any],
    position: dict[str, float],
    radius: float = 3.0,
) -> str | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [
        item
        for item in resources
        if isinstance(item, dict)
        and isinstance(item.get("position"), dict)
        and distance(_position(item), position) <= radius
    ]
    nearest = _nearest_to(candidates, position)
    return str(nearest.get("name")) if nearest is not None and nearest.get("name") else None


def _layout_matches_resource(layout: dict[str, Any], resource_name: str) -> bool:
    actual = layout.get("resource_name")
    return actual is None or actual == resource_name


def _direct_plate_smelting_decision(
    observation: dict[str, Any],
    *,
    target_count: int,
    resource_name: str,
    product_name: str,
    support_skill: IronPlateSkill,
    inventory_only: bool = False,
    allow_support_plate: bool = True,
) -> PlannerDecision:
    total_product = inventory_count(observation, product_name) if inventory_only else total_item_count(observation, product_name)
    if total_product >= target_count:
        return PlannerDecision(None, f"{product_name} target reached: {total_product}/{target_count}", done=True)

    player = player_position(observation)
    output_furnace = _select_plate_output_furnace(observation, resource_name, product_name)
    if output_furnace and entity_item_count(output_furnace, product_name) > 0:
        furnace_pos = _position(output_furnace)
        if distance(player, furnace_pos) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": furnace_pos},
                f"move near starter {product_name} furnace output",
            )
        return PlannerDecision(
            {
                "type": "take",
                "item": product_name,
                "count": min(50, entity_item_count(output_furnace, product_name)),
                "unit_number": output_furnace.get("unit_number"),
                "name": "stone-furnace",
                "position": furnace_pos,
            },
            f"take {product_name} from starter furnace output",
        )

    layout = _find_direct_smelting_cell(observation, resource_name) or _select_direct_smelting_layout(observation, resource_name)
    if layout is None:
        return PlannerDecision(None, f"cannot find open {resource_name} site for direct burner-drill smelting cell")

    furnace = layout.get("furnace")
    if furnace and entity_item_count(furnace, product_name) > 0:
        furnace_pos = _position(furnace)
        if distance(player, furnace_pos) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": furnace_pos},
                f"move near direct {product_name} furnace output",
            )
        return PlannerDecision(
            {
                "type": "take",
                "item": product_name,
                "count": min(50, entity_item_count(furnace, product_name)),
                "unit_number": furnace.get("unit_number"),
                "name": "stone-furnace",
                "position": furnace_pos,
            },
            f"take {product_name} from direct burner-drill smelting cell",
        )

    if inventory_count(observation, "coal") < 6:
        coal = nearest_resource(observation, "coal")
        if coal is None:
            return PlannerDecision(None, f"cannot find nearby coal for direct {product_name} smelting")
        return support_skill._mine_resource(player, coal, "coal", 8)

    missing = _direct_smelting_missing_item(observation, layout)
    if missing:
        decision = _ensure_direct_smelting_item(
            observation,
            player,
            missing,
            support_skill=support_skill,
            allow_support_plate=allow_support_plate,
        )
        if decision is not None:
            return decision

    drill = layout.get("drill")
    if drill is None:
        position = layout["drill_position"]
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position)},
                f"move near {resource_name.replace('-', ' ')} before placing direct burner mining drill",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "burner-mining-drill",
                "position": position,
                "direction": layout["drill_direction"],
                "allow_nearby": True,
                "required_resource": resource_name,
            },
            f"place burner mining drill for direct {product_name} smelting cell",
        )

    if furnace is None:
        position = layout["furnace_position"]
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position)},
                f"move near direct {product_name} furnace position",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "stone-furnace",
                "position": position,
                "allow_nearby": False,
            },
            f"place furnace at {resource_name} drill output",
        )

    for entity_name, layout_key in [
        ("burner-mining-drill", "drill"),
        ("stone-furnace", "furnace"),
    ]:
        entity = layout.get(layout_key)
        if entity and entity_item_count(entity, "coal") < 3:
            return _fuel_burner_line_entity(
                observation,
                player,
                entity,
                entity_name=entity_name,
                threshold=3,
                insert_count=5,
                context=f"direct {product_name} smelting cell",
                support_skill=support_skill,
                far_fuel_reason=f"direct {product_name} smelting needs local fuel before it can run",
            )

    return PlannerDecision(
        {"type": "wait", "ticks": 300},
        f"wait for direct {product_name} burner-drill smelting cell",
    )


def _ensure_direct_smelting_item(
    observation: dict[str, Any],
    player: dict[str, float],
    item: str,
    *,
    support_skill: IronPlateSkill,
    allow_support_plate: bool = True,
) -> PlannerDecision | None:
    if item == "stone-furnace":
        if craftable_count(observation, "stone-furnace") > 0:
            return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft furnace for direct smelting")
        decision = StoneSupplySkill(target_count=8).next_action(observation)
        if decision.done:
            return PlannerDecision(
                {"type": "wait", "ticks": 60},
                "stone supply is ready; wait for direct smelting furnace craftability to refresh",
            )
        return decision

    if item == "burner-mining-drill":
        if craftable_count(observation, "burner-mining-drill") > 0:
            return PlannerDecision(
                {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                "craft burner mining drill for direct smelting",
            )
        if inventory_count(observation, "stone") < 5:
            decision = StoneSupplySkill(target_count=8).next_action(observation)
            if not decision.done:
                return decision
        if inventory_count(observation, "iron-gear-wheel") < 3 and craftable_count(observation, "iron-gear-wheel") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": min(3 - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                },
                "craft gears for direct smelting drill",
            )
        if not allow_support_plate:
            return PlannerDecision(None, "missing burner mining drill and cannot bootstrap another iron drill from current inventory")
        return support_skill.next_action(observation, target_count=20, inventory_only=True)

    return None


def _select_plate_output_furnace(observation: dict[str, Any], resource_name: str, product_name: str) -> dict[str, Any] | None:
    if resource_name == "iron-ore" and product_name == "iron-plate":
        return _select_iron_furnace(observation)
    if resource_name == "copper-ore" and product_name == "copper-plate":
        return _select_copper_furnace(observation)
    furnaces = _entities_within_starter_area(observation, entities_named(observation, "stone-furnace"))
    for furnace in furnaces:
        if entity_item_count(furnace, product_name) > 0 or entity_item_count(furnace, resource_name) > 0:
            return furnace
    return None


def _direct_smelting_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    if layout.get("drill") is None and inventory_count(observation, "burner-mining-drill") <= 0:
        return "burner-mining-drill"
    if layout.get("furnace") is None and inventory_count(observation, "stone-furnace") <= 0:
        return "stone-furnace"
    return None


def _find_direct_smelting_cell(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    candidates: list[tuple[bool, float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_position = _position(drill)
        target_resource = _entity_resource_name(observation, drill, radius=4.5)
        if target_resource != resource_name:
            continue
        if not _within_starter_logistics_area(observation, drill_position):
            continue
        orientation = _direction_to_orientation(int(drill.get("direction") or EAST))
        layout = _direct_smelting_layout_from_drill_position(drill_position, resource_name=resource_name, orientation=orientation)
        layout["drill"] = drill
        layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=0.75)
        candidates.append(
            (
                layout["furnace"] is not None,
                float(drill.get("distance") or distance(player_position(observation), drill_position)),
                layout,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (not item[0], item[1]))
    return candidates[0][2]


def _select_direct_smelting_layout(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, resource_name):
        for orientation in ("east", "west", "south", "north"):
            layout = _direct_smelting_layout_from_drill_position(_position(resource), resource_name=resource_name, orientation=orientation)
            layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=0.75)
            if not _direct_smelting_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _direct_smelting_layout_from_drill_position(
    drill_position: dict[str, float],
    resource_name: str,
    orientation: str = "east",
) -> dict[str, Any]:
    dx, dy, drill_direction, _belt_direction, _inserter_direction = _smelting_orientation(orientation)
    return {
        "drill_position": drill_position,
        "furnace_position": {"x": drill_position["x"] + 2 * dx, "y": drill_position["y"] + 2 * dy},
        "orientation": orientation,
        "resource_name": resource_name,
        "drill_direction": drill_direction,
        "drill": None,
        "furnace": None,
    }


def _direct_smelting_layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    layout_entities = {id(entity) for entity in (layout.get("drill"), layout.get("furnace")) if isinstance(entity, dict)}
    layout_units = {
        entity.get("unit_number")
        for entity in (layout.get("drill"), layout.get("furnace"))
        if isinstance(entity, dict) and entity.get("unit_number") is not None
    }
    footprint = [layout["drill_position"], layout["furnace_position"]]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if id(entity) in layout_entities or (entity.get("unit_number") is not None and entity.get("unit_number") in layout_units):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "stone-furnace", "burner-mining-drill"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name in {"stone-furnace", "burner-mining-drill"} else 2.0
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _belt_smelting_ready(observation: dict[str, Any]) -> bool:
    if not bool(_technology_state(observation, "automation").get("researched")):
        return False
    for name in ("assembling-machine-1", "assembling-machine-2", "assembling-machine-3"):
        for assembler in entities_named(observation, name):
            recipe = str(assembler.get("recipe") or assembler.get("recipe_name") or "")
            if recipe != "transport-belt":
                continue
            if assembler.get("electric_network_connected") is False:
                continue
            return True
    return False


def _find_stone_supply_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[bool, float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_position = _position(drill)
        target_resource = _entity_resource_name(observation, drill, radius=4.5)
        if target_resource != "stone":
            continue
        orientation = _direction_to_orientation(int(drill.get("direction") or EAST))
        layout = _stone_supply_layout_from_drill_position(drill_position, orientation=orientation)
        layout["drill"] = drill
        layout["output_chest"] = _stone_output_chest_near(observation, layout["output_position"])
        candidates.append(
            (
                layout["output_chest"] is not None,
                float(drill.get("distance") or distance(player_position(observation), drill_position)),
                layout,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (not item[0], item[1]))
    return candidates[0][2]


def _select_stone_supply_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, "stone"):
        for orientation in ("east", "west", "south", "north"):
            layout = _stone_supply_layout_from_drill_position(_position(resource), orientation=orientation)
            layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            layout["output_chest"] = _stone_output_chest_near(observation, layout["output_position"])
            if not _stone_supply_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _stone_supply_layout_from_drill_position(
    drill_position: dict[str, float],
    orientation: str = "east",
) -> dict[str, Any]:
    dx, dy, drill_direction, _belt_direction, _inserter_direction = _smelting_orientation(orientation)
    return {
        "drill_position": drill_position,
        "output_position": {"x": drill_position["x"] + 2 * dx, "y": drill_position["y"] + 2 * dy},
        "orientation": orientation,
        "resource_name": "stone",
        "drill_direction": drill_direction,
        "drill": None,
        "output_chest": None,
    }


def _stone_supply_layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    layout_entities = {id(entity) for entity in (layout.get("drill"), layout.get("output_chest")) if isinstance(entity, dict)}
    layout_units = {
        entity.get("unit_number")
        for entity in (layout.get("drill"), layout.get("output_chest"))
        if isinstance(entity, dict) and entity.get("unit_number") is not None
    }
    footprint = [layout["drill_position"], layout["output_position"]]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if id(entity) in layout_entities or (entity.get("unit_number") is not None and entity.get("unit_number") in layout_units):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "burner-mining-drill", "wooden-chest", "iron-chest", "steel-chest"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name == "burner-mining-drill" else 1.5
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _stone_supply_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    if layout.get("output_chest") is None and _available_stone_output_chest_name(observation) is None:
        if craftable_count(observation, "wooden-chest") > 0 or inventory_count(observation, "wood") >= 2 or _nearest_tree(observation) is not None:
            return "wooden-chest"
        return "iron-chest"
    if layout.get("drill") is None and inventory_count(observation, "burner-mining-drill") <= 0:
        return "burner-mining-drill"
    return None


def _available_stone_output_chest_name(observation: dict[str, Any]) -> str | None:
    for name in ("wooden-chest", "iron-chest", "steel-chest"):
        if inventory_count(observation, name) > 0:
            return name
    return None


def _stone_output_chest_near(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = []
    for name in ("wooden-chest", "iron-chest", "steel-chest"):
        entity = _entity_near(observation, name, position, radius=0.9)
        if entity is not None:
            candidates.append(entity)
    return _nearest_to(candidates, position) if candidates else None


def _find_coal_supply_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[bool, float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_position = _position(drill)
        target_resource = _entity_resource_name(observation, drill, radius=4.5)
        if target_resource != "coal":
            continue
        direction = _direction_to_orientation(int(drill.get("direction") or EAST))
        layout = _coal_supply_layout_from_drill_position(drill_position, orientation=direction)
        layout["drill"] = drill
        layout["output_belt"] = _entity_near(observation, "transport-belt", layout["output_position"], radius=0.75)
        candidates.append(
            (
                layout["output_belt"] is not None,
                float(drill.get("distance") or distance(player_position(observation), drill_position)),
                layout,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (not item[0], item[1]))
    return candidates[0][2]


def _entity_resource_name(observation: dict[str, Any], entity: dict[str, Any], radius: float = 3.0) -> str | None:
    direct = str(entity.get("mining_target") or entity.get("resource_name") or "")
    if direct:
        return direct
    return _resource_name_near_position(observation, _position(entity), radius=radius)


def _select_coal_supply_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, "coal"):
        for orientation in ("east", "west", "south", "north"):
            layout = _coal_supply_layout_from_drill_position(_position(resource), orientation=orientation)
            layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            layout["output_belt"] = _entity_near(observation, "transport-belt", layout["output_position"], radius=0.75)
            if not _coal_supply_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _coal_supply_layout_from_drill_position(
    drill_position: dict[str, float],
    orientation: str = "east",
) -> dict[str, Any]:
    dx, dy, drill_direction, belt_direction, _inserter_direction = _smelting_orientation(orientation)
    return {
        "drill_position": drill_position,
        "output_position": {"x": drill_position["x"] + 2 * dx, "y": drill_position["y"] + 2 * dy},
        "orientation": orientation,
        "resource_name": "coal",
        "drill_direction": drill_direction,
        "belt_direction": belt_direction,
        "drill": None,
        "output_belt": None,
    }


def _coal_supply_layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    layout_entities = {id(entity) for entity in (layout.get("drill"), layout.get("output_belt")) if isinstance(entity, dict)}
    layout_units = {
        entity.get("unit_number")
        for entity in (layout.get("drill"), layout.get("output_belt"))
        if isinstance(entity, dict) and entity.get("unit_number") is not None
    }
    footprint = [layout["drill_position"], layout["output_position"]]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if id(entity) in layout_entities or (entity.get("unit_number") is not None and entity.get("unit_number") in layout_units):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "transport-belt", "burner-mining-drill"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name == "burner-mining-drill" else 2.0
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _coal_supply_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    if layout.get("output_belt") is None and inventory_count(observation, "transport-belt") <= 0:
        return "transport-belt"
    if layout.get("drill") is None and inventory_count(observation, "burner-mining-drill") <= 0:
        return "burner-mining-drill"
    return None


def _direction_to_orientation(direction: int) -> str:
    if direction == WEST:
        return "west"
    if direction == SOUTH:
        return "south"
    if direction == NORTH:
        return "north"
    return "east"


def _coal_fuel_feed_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    supply = _find_coal_supply_layout(observation)
    if supply is None or not isinstance(supply.get("output_belt"), dict):
        return None
    output_belt = supply["output_belt"]
    output_position = _position(output_belt)
    orientation = _direction_to_orientation(int(output_belt.get("direction") or supply.get("belt_direction") or EAST))
    dx, dy, _drill_direction, belt_direction, inserter_direction = _smelting_orientation(orientation)
    belt2_position = {"x": output_position["x"] + dx, "y": output_position["y"] + dy}
    inserter_position = {"x": output_position["x"] + 2 * dx, "y": output_position["y"] + 2 * dy}
    consumer_position = {"x": output_position["x"] + 3 * dx, "y": output_position["y"] + 3 * dy}
    return {
        "orientation": orientation,
        "source_drill": supply.get("drill"),
        "source_belt": output_belt,
        "source_belt_position": output_position,
        "belt2_position": belt2_position,
        "inserter_position": inserter_position,
        "consumer_position": consumer_position,
        "belt_direction": belt_direction,
        "inserter_direction": inserter_direction,
        "belt2": _entity_near(observation, "transport-belt", belt2_position, radius=0.75),
        "inserter": _entity_near(observation, "burner-inserter", inserter_position, radius=1.0),
        "consumer": _nearest_fuel_consumer_near(observation, consumer_position),
    }


def _nearest_fuel_consumer_near(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = []
    for name in ("stone-furnace", "boiler"):
        for entity in entities_named(observation, name):
            if distance(_position(entity), position) <= 1.75:
                candidates.append(entity)
    return _nearest_to(candidates, position) if candidates else None


def _coal_fuel_feed_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    if layout.get("belt2") is None and inventory_count(observation, "transport-belt") <= 0:
        return "transport-belt"
    if layout.get("inserter") is None and inventory_count(observation, "burner-inserter") <= 0:
        return "burner-inserter"
    if layout.get("consumer") is None and inventory_count(observation, "stone-furnace") <= 0:
        return "stone-furnace"
    return None


def _coal_fuel_feed_entity_key(entity_name: str) -> str:
    if entity_name == "transport-belt":
        return "belt2"
    if entity_name == "burner-inserter":
        return "inserter"
    if entity_name in {"stone-furnace", "boiler"}:
        return "consumer"
    return entity_name


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _line_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    missing_belts = sum(1 for key in ("belt1", "belt2") if layout.get(key) is None)
    if missing_belts > inventory_count(observation, "transport-belt"):
        return "transport-belt"
    for item, entity_name in [
        ("burner-inserter", "burner-inserter"),
        ("stone-furnace", "stone-furnace"),
        ("burner-mining-drill", "burner-mining-drill"),
    ]:
        if layout.get(_entity_key(entity_name)) is None and inventory_count(observation, item) <= 0:
            return item
    return None


def _find_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    belts = entities_named(observation, "transport-belt")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for belt in belts:
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not _layout_within_starter_area(observation, layout):
                continue
            score = sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if layout.get(key) is not None)
            candidates.append((score, layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _find_incomplete_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not _layout_within_starter_area(observation, layout):
                continue
            if _layout_has_unrelated_blocker(observation, layout):
                continue
            score = sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if layout.get(key) is not None)
            if 0 < score < 5:
                candidates.append((score, float(belt.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _find_unfueled_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not _layout_within_starter_area(observation, layout):
                continue
            if all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")) and not _belt_line_fueled(layout):
                candidates.append((float(belt.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _find_low_fuel_belt_smelting_line(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not _layout_within_starter_area(observation, layout):
                continue
            if not all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")):
                continue
            if any(
                entity_item_count(layout[key], "coal") < SMELTING_LINE_FUEL_RESERVE[key]
                for key in ("drill", "inserter", "furnace")
            ):
                candidates.append((float(belt.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _smelting_line_fuel_unit_numbers(observation: dict[str, Any], resource_name: str = "iron-ore") -> set[Any]:
    units: set[Any] = set()
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")):
                continue
            for key in ("drill", "inserter", "furnace"):
                entity = layout.get(key)
                if isinstance(entity, dict):
                    units.add(entity.get("unit_number"))
    return units


def _belt_layout_from_anchor(observation: dict[str, Any], belt: dict[str, Any]) -> dict[str, Any]:
    layouts = _belt_layouts_from_anchor(observation, belt)
    return max(layouts, key=lambda item: sum(1 for key in ("belt1", "belt2", "inserter", "furnace", "drill") if item.get(key) is not None))


def _belt_layouts_from_anchor(observation: dict[str, Any], belt: dict[str, Any]) -> list[dict[str, Any]]:
    belt_pos = _position(belt)
    output: list[dict[str, Any]] = []
    for orientation in ("east", "west", "south", "north"):
        layout = _layout_from_belt1_position(belt_pos, orientation=orientation)
        if not _entity_direction_matches(belt, layout["belt_direction"]):
            continue
        layout["belt1"] = belt
        layout["belt2"] = _entity_near(observation, "transport-belt", layout["belt2_position"], radius=0.75)
        layout["inserter"] = _entity_near(observation, "burner-inserter", layout["inserter_position"], radius=1.0)
        layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=1.5)
        layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
        layout["resource_name"] = (
            _entity_resource_name(observation, layout["drill"])
            if isinstance(layout.get("drill"), dict)
            else _resource_name_near_position(observation, layout["drill_position"])
        )
        output.append(layout)
    return output


def _entity_direction_matches(entity: dict[str, Any], expected: int) -> bool:
    if "direction" not in entity:
        return True
    try:
        return int(entity.get("direction")) == expected
    except (TypeError, ValueError):
        return True


def _estimated_iron_plate_rate(observation: dict[str, Any]) -> float:
    return _estimated_plate_rate(observation, "iron-plate", "iron-ore")


def _estimated_copper_plate_rate(observation: dict[str, Any]) -> float:
    return _estimated_plate_rate(observation, "copper-plate", "copper-ore")


def _estimated_plate_rate(observation: dict[str, Any], product_name: str, resource_name: str) -> float:
    complete_lines = _complete_belt_smelting_line_count(observation, resource_name)
    return round(complete_lines * 18.75, 3)


def _complete_belt_smelting_line_count(observation: dict[str, Any], resource_name: str = "iron-ore") -> int:
    furnace_positions: set[tuple[float, float]] = set()
    for belt in entities_named(observation, "transport-belt"):
        for layout in _belt_layouts_from_anchor(observation, belt):
            if not _layout_matches_resource(layout, resource_name):
                continue
            if not _layout_within_starter_area(observation, layout):
                continue
            if all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")) and _belt_line_fueled(layout):
                furnace_pos = _position(layout["furnace"])
                furnace_positions.add((round(furnace_pos["x"], 2), round(furnace_pos["y"], 2)))
    return len(furnace_positions)


def _belt_line_fueled(layout: dict[str, Any]) -> bool:
    for key, minimum in [("drill", 1), ("inserter", 1), ("furnace", 1)]:
        entity = layout.get(key)
        if not isinstance(entity, dict) or entity_item_count(entity, "coal") < minimum:
            return False
    return True


def _fuel_burner_line_entity(
    observation: dict[str, Any],
    player: dict[str, float],
    entity: dict[str, Any],
    *,
    entity_name: str,
    threshold: int,
    insert_count: int,
    context: str,
    support_skill: IronPlateSkill,
    far_fuel_reason: str,
    exclude_source_units: set[Any] | None = None,
) -> PlannerDecision:
    position = _position(entity)
    inventory_coal = inventory_count(observation, "coal")
    current_fuel = entity_item_count(entity, "coal")
    desired_insert = min(insert_count, max(1, threshold - current_fuel))
    if inventory_coal <= 0:
        coal = _nearest_resource_to_position(observation, position, "coal")
        excluded_units = set(exclude_source_units or set())
        excluded_units.add(entity.get("unit_number"))
        source = _nearest_surplus_fuel_source(observation, position, exclude_units=excluded_units)
        source_surplus = _surplus_fuel_count(source) if source is not None else 0
        if coal is not None and distance(position, _position(coal)) <= WALK_FUEL_LOGISTICS_LIMIT and source_surplus < 8:
            return support_skill._mine_resource(player, coal, "coal", 16)
        local_coal = _nearest_resource_to_position(observation, player, "coal")
        if (
            local_coal is not None
            and distance(player, _position(local_coal)) <= 16.0
            and distance(player, position) > 20.0
            and source_surplus < max(8, desired_insert)
        ):
            return support_skill._mine_resource(player, local_coal, "coal", max(16, desired_insert))
        if source is not None:
            source_position = _position(source)
            if distance(player, source_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": source_position},
                    f"move near surplus coal source for {context}",
                )
            take_count = min(16, _surplus_fuel_count(source))
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "coal",
                    "count": max(1, take_count),
                    "unit_number": source.get("unit_number"),
                    "name": source.get("name"),
                    "position": source_position,
                },
                f"recover surplus coal from {source.get('name')} for {context}",
            )
        if coal is None:
            return PlannerDecision(None, f"cannot find coal for {context}")
        if distance(position, _position(coal)) > WALK_FUEL_LOGISTICS_LIMIT:
            return PlannerDecision(None, far_fuel_reason)
        return support_skill._mine_resource(player, coal, "coal", 16)

    if inventory_coal < desired_insert and distance(player, position) > 20.0:
        local_coal = _nearest_resource_to_position(observation, player, "coal")
        if local_coal is not None and distance(player, _position(local_coal)) <= 16.0:
            return support_skill._mine_resource(player, local_coal, "coal", max(16, desired_insert - inventory_coal))

    if distance(player, position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": position},
            f"move near {entity_name} to fuel {context}",
        )
    return PlannerDecision(
        {
            "type": "insert",
            "item": "coal",
            "count": min(insert_count, inventory_coal, desired_insert),
            "unit_number": entity.get("unit_number"),
            "name": entity_name,
            "position": position,
        },
        f"fuel {entity_name} in {context}",
    )


def _nearest_surplus_fuel_source(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    exclude_unit: Any = None,
    exclude_units: set[Any] | None = None,
) -> dict[str, Any] | None:
    excluded = set(exclude_units or set())
    if exclude_unit is not None:
        excluded.add(exclude_unit)
    candidates = []
    for entity_name in ("stone-furnace", "burner-mining-drill", "burner-inserter", "boiler"):
        for entity in entities_named(observation, entity_name):
            if entity.get("unit_number") in excluded:
                continue
            surplus = _surplus_fuel_count(entity)
            if surplus <= 0:
                continue
            entity_position = _position(entity)
            candidates.append((distance(target_position, entity_position), -surplus, entity))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _surplus_fuel_count(entity: dict[str, Any]) -> int:
    coal = entity_item_count(entity, "coal")
    reserve = _fuel_reserve_for_entity(str(entity.get("name") or ""))
    return max(0, coal - reserve)


def _fuel_reserve_for_entity(entity_name: str) -> int:
    if entity_name == "burner-inserter":
        return 2
    if entity_name == "boiler":
        return 5
    if entity_name in {"burner-mining-drill", "stone-furnace"}:
        return 3
    return 0


def _blocking_obstacle_near(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    blockers = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if _is_preserved_starter_artifact(observation, entity):
            continue
        if entity_type not in {"simple-entity", "tree", "cliff"} and not name.endswith("rock"):
            continue
        entity_position = _position(entity)
        if distance(entity_position, position) <= 4.0:
            blockers.append(entity)
    return _nearest_to(blockers, position) if blockers else None


def _is_preserved_starter_artifact(observation: dict[str, Any], entity: dict[str, Any]) -> bool:
    name = str(entity.get("name") or "").lower()
    if not any(keyword in name for keyword in PRESERVED_STARTER_ARTIFACT_KEYWORDS):
        return False
    return _within_starter_logistics_area(
        observation,
        _position(entity),
        radius=STARTER_ENTITY_CLUSTER_RADIUS,
    )


def _layout_has_unrelated_blocker(observation: dict[str, Any], layout: dict[str, Any]) -> bool:
    layout_units = {
        entity.get("unit_number")
        for entity in [layout.get("belt1"), layout.get("belt2"), layout.get("inserter"), layout.get("furnace"), layout.get("drill")]
        if isinstance(entity, dict)
    }
    footprint = [
        layout["drill_position"],
        layout["belt1_position"],
        layout["belt2_position"],
        layout["inserter_position"],
        layout["furnace_position"],
    ]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("unit_number") in layout_units:
            continue
        name = str(entity.get("name") or "")
        entity_type = str(entity.get("type") or "")
        if name not in {"character", "transport-belt", "burner-inserter", "stone-furnace", "burner-mining-drill"}:
            continue
        threshold = 3.0 if name in {"stone-furnace", "burner-mining-drill"} else 2.0
        entity_pos = _position(entity)
        if any(distance(entity_pos, pos) < threshold for pos in footprint):
            return True
    return False


def _select_belt_smelting_layout(observation: dict[str, Any], resource_name: str = "iron-ore") -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    candidates = _ranked_patch_drill_resources(observation, resource_name)
    for resource in candidates:
        for orientation in ("east", "west", "south", "north"):
            layout = _layout_from_drill_position(_position(resource), resource_name=resource_name, orientation=orientation)
            if not _layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _ranked_patch_drill_resources(observation: dict[str, Any], resource_name: str) -> list[dict[str, Any]]:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return []
    candidates = [
        item
        for item in resources
        if isinstance(item, dict)
        and item.get("name") == resource_name
        and isinstance(item.get("position"), dict)
    ]
    if not candidates:
        return []
    starter_candidates = [
        item
        for item in candidates
        if _within_starter_logistics_area(observation, _position(item))
    ]
    if starter_candidates:
        candidates = starter_candidates
    elif _base_anchor_position(observation) is not None:
        return []

    existing_drills = [
        item
        for item in entities_named(observation, "burner-mining-drill")
        if _resource_name_near_position(observation, _position(item)) == resource_name
    ]

    def rank(resource: dict[str, Any]) -> tuple[float, float]:
        pos = _position(resource)
        return (
            -_patch_drill_candidate_score(observation, resource, existing_drills),
            _distance_to_starter_logistics_area(observation, pos),
        )

    return sorted(candidates, key=rank)


def _patch_drill_candidate_score(
    observation: dict[str, Any],
    resource: dict[str, Any],
    existing_drills: list[dict[str, Any]],
) -> float:
    position = _position(resource)
    coverage = _resource_tile_coverage(observation, position, str(resource.get("name") or ""))
    if coverage <= 0:
        return -10000.0

    nearest_drill_distance = min((distance(position, _position(drill)) for drill in existing_drills), default=999999.0)
    if nearest_drill_distance < 2.5:
        return -10000.0

    distance_penalty = _distance_to_starter_logistics_area(observation, position) * 0.05
    alignment_bonus = _existing_patch_line_alignment_bonus(position, existing_drills)
    return coverage * 20.0 + alignment_bonus - distance_penalty


def _resource_tile_coverage(observation: dict[str, Any], center: dict[str, float], resource_name: str) -> int:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return 0
    covered = 0
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("name") != resource_name or not isinstance(resource.get("position"), dict):
            continue
        pos = _position(resource)
        if abs(pos["x"] - center["x"]) <= 1.5 and abs(pos["y"] - center["y"]) <= 1.5:
            covered += 1
    return covered


def _existing_patch_line_alignment_bonus(position: dict[str, float], existing_drills: list[dict[str, Any]]) -> float:
    bonus = 0.0
    for drill in existing_drills:
        drill_pos = _position(drill)
        if abs(position["x"] - drill_pos["x"]) <= 0.25:
            bonus += max(0.0, 6.0 - abs(position["y"] - drill_pos["y"]))
        if abs(position["y"] - drill_pos["y"]) <= 0.25:
            bonus += max(0.0, 6.0 - abs(position["x"] - drill_pos["x"]))
    return bonus


def _layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    footprint = [
        layout["drill_position"],
        layout["belt1_position"],
        layout["belt2_position"],
        layout["inserter_position"],
        layout["furnace_position"],
    ]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "transport-belt", "burner-inserter", "stone-furnace", "burner-mining-drill"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name in {"stone-furnace", "burner-mining-drill"} else 2.0
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _layout_from_drill_position(
    drill_position: dict[str, float],
    resource_name: str | None = None,
    orientation: str = "east",
) -> dict[str, Any]:
    dx, dy, drill_direction, belt_direction, inserter_direction = _smelting_orientation(orientation)
    return {
        "drill_position": drill_position,
        "belt1_position": {"x": drill_position["x"] + 2 * dx, "y": drill_position["y"] + 2 * dy},
        "belt2_position": {"x": drill_position["x"] + 3 * dx, "y": drill_position["y"] + 3 * dy},
        "inserter_position": {"x": drill_position["x"] + 4 * dx, "y": drill_position["y"] + 4 * dy},
        "furnace_position": {"x": drill_position["x"] + 5 * dx, "y": drill_position["y"] + 5 * dy},
        "orientation": orientation,
        "resource_name": resource_name,
        "drill_direction": drill_direction,
        "belt_direction": belt_direction,
        "inserter_direction": inserter_direction,
        "drill": None,
        "belt1": None,
        "belt2": None,
        "inserter": None,
        "furnace": None,
    }


def _layout_from_belt1_position(belt_position: dict[str, float], orientation: str = "east") -> dict[str, Any]:
    dx, dy, _drill_direction, _belt_direction, _inserter_direction = _smelting_orientation(orientation)
    return _layout_from_drill_position(
        {"x": belt_position["x"] - 2 * dx, "y": belt_position["y"] - 2 * dy},
        orientation=orientation,
    )


def _smelting_orientation(orientation: str) -> tuple[int, int, int, int, int]:
    if orientation == "west":
        return -1, 0, WEST, WEST, EAST
    if orientation == "south":
        return 0, 1, SOUTH, SOUTH, NORTH
    if orientation == "north":
        return 0, -1, NORTH, NORTH, SOUTH
    return 1, 0, EAST, EAST, WEST


def _entity_key_for_layout(entity_name: str, layout_key: str) -> str:
    if entity_name == "transport-belt" and layout_key == "belt1_position":
        return "belt1"
    if entity_name == "transport-belt" and layout_key == "belt2_position":
        return "belt2"
    if entity_name == "burner-mining-drill":
        return "drill"
    if entity_name == "stone-furnace":
        return "furnace"
    if entity_name == "burner-inserter":
        return "inserter"
    return entity_name


def _entity_key(entity_name: str) -> str:
    return _entity_key_for_layout(entity_name, "")


def _entity_near(
    observation: dict[str, Any],
    name: str,
    position: dict[str, float],
    radius: float,
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in entities_named(observation, name)
        if distance(_position(item), position) <= radius
    ]
    return _nearest_to(candidates, position)


def _select_power_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("power_sites")
    if not isinstance(sites, list):
        return None
    local_candidates: list[tuple[float, dict[str, Any]]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        layout = _layout_from_power_site(site)
        if layout is None:
            continue
        position = _power_layout_position(layout)
        distance_to_area = _distance_to_starter_logistics_area(observation, position)
        if _within_starter_logistics_area(observation, position, radius=STARTER_POWER_SITE_RADIUS):
            local_candidates.append((distance_to_area, layout))
    if local_candidates:
        local_candidates.sort(key=lambda item: item[0])
        return local_candidates[0][1]
    return None


def _has_remote_power_site(observation: dict[str, Any]) -> bool:
    sites = observation.get("power_sites")
    if not isinstance(sites, list):
        return False
    for site in sites:
        if not isinstance(site, dict):
            continue
        layout = _layout_from_power_site(site)
        if layout is None:
            continue
        position = _power_layout_position(layout)
        if not _within_starter_logistics_area(observation, position, radius=STARTER_POWER_SITE_RADIUS):
            return True
    return False


def _layout_from_power_site(site: dict[str, Any]) -> dict[str, Any] | None:
    raw_layout = site.get("layout")
    if not isinstance(raw_layout, dict):
        return None
    specs: dict[str, dict[str, Any]] = {}
    for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
        raw_spec = raw_layout.get(key)
        if not isinstance(raw_spec, dict) or not isinstance(raw_spec.get("position"), dict):
            return None
        specs[key] = {
            "name": str(raw_spec.get("name") or _power_spec_name(key)),
            "position": _position(raw_spec),
            "direction": int(raw_spec.get("direction") or NORTH),
        }
    return _power_layout_from_specs(specs)


def _power_layout_position(layout: dict[str, Any]) -> dict[str, float]:
    pump = layout.get("offshore_pump")
    if isinstance(pump, dict):
        return _position(pump)
    spec = layout.get("offshore_pump_spec")
    if isinstance(spec, dict) and isinstance(spec.get("position"), dict):
        return _xy_position(spec["position"])
    return player_position({})


def _power_layout_from_pump_position(position: dict[str, float], direction: int = WEST) -> dict[str, Any]:
    turns = _turns_from_west(direction)
    specs = {
        "offshore_pump": {
            "name": "offshore-pump",
            "position": position,
            "direction": direction,
        },
        "boiler": {
            "name": "boiler",
            "position": _offset_position(position, _rotate_offset({"x": 2, "y": -1}, turns)),
            "direction": _rotate_direction(NORTH, turns),
        },
        "steam_engine": {
            "name": "steam-engine",
            "position": _offset_position(position, _rotate_offset({"x": 2, "y": -4}, turns)),
            "direction": _rotate_direction(NORTH, turns),
        },
        "small_electric_pole": {
            "name": "small-electric-pole",
            "position": _offset_position(position, _rotate_offset({"x": 0, "y": -4}, turns)),
            "direction": NORTH,
        },
    }
    return _power_layout_from_specs(specs)


def _turns_from_west(direction: int) -> int:
    if direction == NORTH:
        return 1
    if direction == EAST:
        return 2
    if direction == SOUTH:
        return 3
    return 0


def _rotate_offset(offset: dict[str, float], turns: int) -> dict[str, float]:
    x = float(offset["x"])
    y = float(offset["y"])
    for _ in range(turns):
        x, y = -y, x
    return {"x": x, "y": y}


def _offset_position(position: dict[str, float], offset: dict[str, float]) -> dict[str, float]:
    return {"x": float(position["x"]) + float(offset["x"]), "y": float(position["y"]) + float(offset["y"])}


def _rotate_direction(direction: int, turns: int) -> int:
    directions = [NORTH, EAST, SOUTH, WEST]
    try:
        index = directions.index(direction)
    except ValueError:
        index = 0
    return directions[(index + turns) % len(directions)]


def _power_layout_from_specs(specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    layout: dict[str, Any] = {}
    for key, spec in specs.items():
        layout[key] = None
        layout[f"{key}_spec"] = spec
    return layout


def _power_layout_with_existing_entities(observation: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    merged = dict(layout)
    for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
        if merged.get(key) is not None:
            continue
        spec = merged.get(f"{key}_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("position"), dict):
            continue
        existing = _entity_near(
            observation,
            str(spec.get("name") or _power_spec_name(key)),
            _xy_position(spec["position"]),
            radius=1.0,
        )
        if existing is not None:
            merged[key] = existing
    return merged


def _find_steam_power_block(
    observation: dict[str, Any],
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for pump in entities_named(observation, "offshore-pump"):
        pump_position = _position(pump)
        starter_local = _within_starter_logistics_area(observation, pump_position, radius=STARTER_POWER_SITE_RADIUS)
        reference_distance = distance(pump_position, reference_position) if reference_position is not None else 999999.0
        if not starter_local and not (allow_existing_remote and reference_distance <= 48.0):
            continue
        layout = _power_layout_from_pump_position(pump_position, int(pump.get("direction") or WEST))
        layout["offshore_pump"] = pump
        layout["boiler"] = _entity_near(observation, "boiler", layout["boiler_spec"]["position"], radius=1.0)
        layout["steam_engine"] = _entity_near(observation, "steam-engine", layout["steam_engine_spec"]["position"], radius=1.0)
        layout["small_electric_pole"] = _entity_near(
            observation,
            "small-electric-pole",
            layout["small_electric_pole_spec"]["position"],
            radius=1.0,
        )
        score = sum(1 for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole") if layout.get(key) is not None)
        locality_penalty = 0.0 if starter_local else 1.0
        candidates.append((score, locality_penalty + reference_distance * 0.001, layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _missing_power_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    for key in ("offshore_pump", "boiler", "steam_engine", "small_electric_pole"):
        if layout.get(key) is None:
            item = _power_spec_name(key)
            if inventory_count(observation, item) <= 0:
                return item
    return None


def _power_spec_name(key: str) -> str:
    return {
        "offshore_pump": "offshore-pump",
        "boiler": "boiler",
        "steam_engine": "steam-engine",
        "small_electric_pole": "small-electric-pole",
    }[key]


def _power_item_required_count(item: str) -> int:
    return {
        "pipe": 5,
        "iron-gear-wheel": 8,
        "copper-cable": 2,
        "electronic-circuit": 2,
    }.get(item, 1)


def _steam_power_ready(layout: dict[str, Any] | None) -> bool:
    if not layout:
        return False
    return (
        layout.get("offshore_pump") is not None
        and layout.get("boiler") is not None
        and layout.get("steam_engine") is not None
        and layout.get("small_electric_pole") is not None
        and entity_fluid_count(layout["steam_engine"], "steam") > 0
        and int(layout["steam_engine"].get("status") or 0) != 5
    )


def _power_stand_position(layout: dict[str, Any]) -> dict[str, float]:
    pump_spec = layout.get("offshore_pump_spec") if isinstance(layout.get("offshore_pump_spec"), dict) else {}
    position = pump_spec.get("position") if isinstance(pump_spec.get("position"), dict) else {"x": 0.0, "y": 0.0}
    return {"x": float(position["x"]) + 5.0, "y": float(position["y"]) + 3.0}


def _nearest_tree(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    trees = [item for item in entities if isinstance(item, dict) and item.get("type") == "tree"]
    if not trees:
        return None
    return min(trees, key=lambda item: float(item.get("distance") or 999999))


class ResearchAutomationSkill:
    """Build and feed the first lab to unlock the Automation technology."""

    def __init__(self, technology: str = "automation") -> None:
        self.technology = technology
        self.power_skill = SetupPowerSkill()
        self.science_skill = AutomationScienceSkill(target_count=10)
        self._research_requested = False

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        technology = _technology_state(observation, self.technology)
        if bool(technology.get("researched")):
            return PlannerDecision(None, f"{self.technology} research completed", done=True)

        player = player_position(observation)
        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        lab = _find_research_lab(observation)
        if lab is None:
            decision = self._ensure_item_quantity(observation, player, "lab", 1)
            if decision is not None:
                return decision
            site = _select_lab_site(observation)
            if site is None:
                return PlannerDecision(None, "cannot find a powered or wireable lab site near the starter power block")
            if not site.get("pole_unit_number"):
                decision = self._ensure_item_quantity(observation, player, "small-electric-pole", 1)
                if decision is not None:
                    return decision
                pole_position = site["pole_position"]
                if distance(player, pole_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(pole_position)},
                        "move near planned research pole",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "small-electric-pole",
                        "position": pole_position,
                    },
                    "extend electric network for research lab",
                )
            lab_position = site["lab_position"]
            if distance(player, lab_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(lab_position)},
                    "move near planned lab position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "lab",
                    "position": lab_position,
                    "allow_nearby": True,
                },
                "place first research lab",
            )

        if not _lab_powered(lab):
            decision = self._ensure_lab_power(observation, player, lab)
            if decision is not None:
                return decision

        if not bool(_technology_state(observation, "automation-science-pack").get("researched")):
            return PlannerDecision(
                {"type": "research", "technology": "automation-science-pack"},
                "unlock automation science pack trigger technology after lab bootstrap",
            )

        current = _current_research(observation)
        if current == self.technology:
            self._research_requested = True
        elif not self._research_requested:
            self._research_requested = True
            return PlannerDecision(
                {"type": "research", "technology": self.technology},
                f"set current research to {self.technology}",
            )

        pack_name = "automation-science-pack"
        lab_pack_count = entity_item_count(lab, pack_name)
        research_progress = _research_progress(observation)
        pack_goal = _research_pack_goal(observation, self.technology, pack_name)
        packs_needed = max(1, pack_goal - int(research_progress * pack_goal))
        inventory_packs = inventory_count(observation, pack_name)
        if inventory_packs > 0:
            lab = _best_lab_for_pack_insert(observation, pack_name) or lab
            lab_pack_count = entity_item_count(lab, pack_name)
            lab_position = _position(lab)
            if distance(player, lab_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": lab_position},
                    "move near lab to insert automation science packs",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": pack_name,
                    "count": min(max(1, packs_needed - lab_pack_count), inventory_packs),
                    "unit_number": lab.get("unit_number"),
                    "name": "lab",
                    "position": lab_position,
                },
                "insert automation science packs into lab",
            )

        if _any_lab_has_pack(observation, pack_name):
            return PlannerDecision({"type": "wait", "ticks": 600}, "wait for powered lab chain to consume science packs")

        science_decision = AutomationScienceSkill(target_count=packs_needed).next_action(observation)
        if not science_decision.done:
            return science_decision

        return PlannerDecision({"type": "wait", "ticks": 600}, "wait for automation research progress")

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for automation research",
            )

        if item == "lab":
            for prerequisite, count in [
                ("electronic-circuit", 10),
                ("iron-gear-wheel", 10),
                ("transport-belt", 4),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "transport-belt":
            for prerequisite, count in [("iron-gear-wheel", 2), ("iron-plate", 2)]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            return None

        if item == "electronic-circuit":
            decision = ElectronicCircuitSkill(target_count=quantity).next_action(observation)
            if not decision.done:
                return decision
            return None

        return self.power_skill._ensure_item_quantity(observation, player, item, quantity)

    def _ensure_lab_power(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        lab: dict[str, Any],
    ) -> PlannerDecision | None:
        lab_position = _position(lab)
        nearby_pole = _nearest_power_pole_to_supply(observation, lab_position)
        if nearby_pole is not None:
            pole_position = _position(nearby_pole)
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, "move near lab power pole")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": nearby_pole.get("unit_number"),
                    "name": nearby_pole.get("name") or "small-electric-pole",
                    "position": pole_position,
                },
                "connect lab power pole to the starter electric network",
            )

        decision = self._ensure_item_quantity(observation, player, "small-electric-pole", 1)
        if decision is not None:
            return decision

        pole_position = _pole_position_to_supply_entity(observation, lab_position)
        if distance(player, pole_position) > 20:
            return PlannerDecision({"type": "move_to", "position": pole_position}, "move near unpowered lab to place supply pole")
        return PlannerDecision(
            {
                "type": "build",
                "name": "small-electric-pole",
                "position": pole_position,
            },
            "place small electric pole to power the lab",
        )


class ResearchTechnologySkill:
    """Research the next early technology using existing powered labs and red science."""

    def __init__(self, technology: str = "logistics") -> None:
        self.technology = technology
        self.bootstrap_skill = ResearchAutomationSkill()
        self._research_requested = False

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        technology = _technology_state(observation, self.technology)
        if bool(technology.get("researched")):
            return PlannerDecision(None, f"{self.technology} research completed", done=True)

        if not bool(_technology_state(observation, "automation").get("researched")) or _find_research_lab(observation) is None:
            decision = self.bootstrap_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for research bootstrap observation to settle")
            return decision

        current = _current_research(observation)
        if current == self.technology:
            self._research_requested = True
        elif not self._research_requested:
            self._research_requested = True
            return PlannerDecision(
                {"type": "research", "technology": self.technology},
                f"set current research to {self.technology}",
            )

        lab = _find_research_lab(observation)
        if lab is None:
            return PlannerDecision(None, "cannot find a lab for research")

        lab_position = _position(lab)
        power_block = _find_steam_power_block(
            observation,
            allow_existing_remote=True,
            reference_position=lab_position,
        )
        if not _steam_power_ready(power_block):
            decision = SetupPowerSkill().next_action(
                observation,
                allow_existing_remote=True,
                reference_position=lab_position,
            )
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        ingredients = technology.get("ingredients") if isinstance(technology.get("ingredients"), dict) else {}
        if not ingredients:
            return PlannerDecision({"type": "research", "technology": self.technology}, f"unlock trigger technology {self.technology}")

        player = player_position(observation)
        research_progress = _research_progress(observation)
        for pack_name in sorted(ingredients):
            if pack_name != "automation-science-pack":
                return PlannerDecision(None, f"research pack path is not implemented yet: {pack_name}")
            lab_pack_count = entity_item_count(lab, pack_name)
            pack_goal = _research_pack_goal(observation, self.technology, pack_name)
            packs_needed = max(1, pack_goal - int(research_progress * pack_goal))
            inventory_packs = inventory_count(observation, pack_name)
            if inventory_packs > 0:
                lab = _best_lab_for_pack_insert(observation, pack_name) or lab
                lab_pack_count = entity_item_count(lab, pack_name)
                lab_position = _position(lab)
                if distance(player, lab_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": lab_position}, f"move near lab to insert {pack_name}")
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": pack_name,
                        "count": min(max(1, packs_needed - lab_pack_count), inventory_packs),
                        "unit_number": lab.get("unit_number"),
                        "name": "lab",
                        "position": lab_position,
                    },
                    f"insert {pack_name} into lab for {self.technology}",
                )

            if _any_lab_has_pack(observation, pack_name):
                return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for lab chain to consume {pack_name}")

            science_decision = BuildItemMallSkill("automation-science-pack", packs_needed).next_action(
                observation,
                allow_existing_remote=True,
                reference_position=lab_position,
            )
            if not science_decision.done:
                return science_decision

        return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for {self.technology} research progress")


class CircuitAutomationSkill:
    """Build a minimal powered assembler cell that makes green circuits."""

    def __init__(self, target_count: int = 5) -> None:
        self.target_count = target_count
        self.power_skill = SetupPowerSkill()
        self.research_skill = ResearchAutomationSkill()
        self.hand_circuit_skill = ElectronicCircuitSkill(target_count=max(7, target_count))
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not bool(_technology_state(observation, "automation").get("researched")):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        power_block = _find_steam_power_block(observation)
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        line = _find_circuit_automation_cell(observation) or _select_circuit_automation_site(observation)
        if line is None:
            return PlannerDecision(None, "cannot find a powered or wireable site for the first circuit assembler cell")

        missing_item = _missing_circuit_cell_item(observation, line)
        if missing_item:
            decision = self._ensure_item_quantity(observation, player, missing_item, _circuit_cell_required_count(line, missing_item))
            if decision is not None:
                return decision

        if not line.get("pole_unit_number"):
            pole_position = line["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(pole_position)},
                    "move near planned circuit automation pole",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "extend power for circuit automation cell",
            )

        build_order = [
            ("cable_assembler", "assembling-machine-1", "cable_assembler_position"),
            ("circuit_assembler", "assembling-machine-1", "circuit_assembler_position"),
            ("transfer_inserter", "inserter", "transfer_inserter_position"),
        ]
        for key, name, position_key in build_order:
            if line.get(key) is not None:
                continue
            position = line[position_key]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position)},
                    f"move near planned {name} position for circuit automation",
                )
            action: dict[str, Any] = {
                "type": "build",
                "name": name,
                "position": position,
                "allow_nearby": False,
            }
            if key == "transfer_inserter":
                action["direction"] = int(line.get("transfer_inserter_direction") or EAST)
            return PlannerDecision(action, f"place {name} for circuit automation cell")

        if line.get("pole_unit_number") and not _circuit_cell_powered(line):
            pole_position = line["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, "move near circuit automation pole to connect power")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": line.get("pole_unit_number"),
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "connect circuit automation pole to nearby electric network",
            )

        cable_assembler = line.get("cable_assembler")
        circuit_assembler = line.get("circuit_assembler")
        if cable_assembler and cable_assembler.get("recipe") != "copper-cable":
            return self._set_recipe_decision(player, cable_assembler, "copper-cable")
        if circuit_assembler and circuit_assembler.get("recipe") != "electronic-circuit":
            return self._set_recipe_decision(player, circuit_assembler, "electronic-circuit")

        circuit_output = entity_item_count(circuit_assembler, "electronic-circuit") if circuit_assembler else 0
        if circuit_output > 0:
            circuit_pos = _position(circuit_assembler)
            if distance(player, circuit_pos) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": circuit_pos},
                    "move near circuit assembler to collect produced circuits",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "electronic-circuit",
                    "count": min(circuit_output, self.target_count),
                    "unit_number": circuit_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": circuit_pos,
                },
                "take electronic circuits from assembler output",
            )

        if _circuit_cell_ready(line) and total_item_count(observation, "electronic-circuit") >= self.target_count:
            return PlannerDecision(
                None,
                f"circuit automation cell is running and target reached: {total_item_count(observation, 'electronic-circuit')}/{self.target_count}",
                done=True,
            )

        if circuit_assembler and entity_item_count(circuit_assembler, "copper-cable") < 6 and inventory_count(observation, "copper-cable") > 0:
            circuit_pos = _position(circuit_assembler)
            if distance(player, circuit_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": circuit_pos}, "move near circuit assembler to seed copper cable")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "copper-cable",
                    "count": min(12, inventory_count(observation, "copper-cable")),
                    "unit_number": circuit_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": circuit_pos,
                },
                "seed circuit assembler with available copper cable",
            )

        if circuit_assembler and entity_item_count(circuit_assembler, "iron-plate") < 4:
            if inventory_count(observation, "iron-plate") > 0:
                circuit_pos = _position(circuit_assembler)
                if distance(player, circuit_pos) > 20:
                    return PlannerDecision({"type": "move_to", "position": circuit_pos}, "move near circuit assembler to insert iron")
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": min(20, inventory_count(observation, "iron-plate")),
                        "unit_number": circuit_assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": circuit_pos,
                    },
                    "insert iron plates into circuit assembler",
                )
            decision = self.iron_skill.next_action(observation, target_count=8, inventory_only=True)
            if not decision.done:
                return decision

        if (
            circuit_assembler
            and cable_assembler
            and entity_item_count(circuit_assembler, "copper-cable") < 6
            and entity_item_count(cable_assembler, "copper-cable") > 0
        ):
            cable_pos = _position(cable_assembler)
            if distance(player, cable_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": cable_pos}, "move near cable assembler to collect copper cable")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "copper-cable",
                    "count": min(24, entity_item_count(cable_assembler, "copper-cable")),
                    "unit_number": cable_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": cable_pos,
                },
                "take copper cable from cable assembler output",
            )

        if cable_assembler and entity_item_count(cable_assembler, "copper-plate") < 4:
            if inventory_count(observation, "copper-plate") < 8:
                decision = self.copper_skill.next_action(observation, target_count=8, inventory_only=True)
                if not decision.done:
                    return decision
            cable_pos = _position(cable_assembler)
            if distance(player, cable_pos) > 20:
                return PlannerDecision({"type": "move_to", "position": cable_pos}, "move near cable assembler to insert copper")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": "copper-plate",
                    "count": min(20, inventory_count(observation, "copper-plate")),
                    "unit_number": cable_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": cable_pos,
                },
                "insert copper plates into cable assembler",
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 600},
            "wait for assembler cell to make copper cable and electronic circuits",
        )

    def _set_recipe_decision(
        self,
        player: dict[str, float],
        assembler: dict[str, Any],
        recipe: str,
    ) -> PlannerDecision:
        position = _position(assembler)
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": position}, f"move near assembler to set {recipe}")
        return PlannerDecision(
            {
                "type": "set_recipe",
                "recipe": recipe,
                "unit_number": assembler.get("unit_number"),
                "name": "assembling-machine-1",
                "position": position,
            },
            f"set assembler recipe to {recipe}",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None

        if item == "assembling-machine-1":
            for prerequisite, count in [
                ("electronic-circuit", 3 * quantity),
                ("iron-gear-wheel", 5 * quantity),
                ("iron-plate", 9 * quantity),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            if bool(_technology_state(observation, "automation").get("researched")):
                decision = BuildItemMallSkill("assembling-machine-1", quantity).next_action(observation)
                if not decision.done:
                    return decision
            if craftable_count(observation, "assembling-machine-1") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "assembling-machine-1",
                        "count": min(
                            quantity - inventory_count(observation, "assembling-machine-1"),
                            craftable_count(observation, "assembling-machine-1"),
                        ),
                    },
                    "craft assembling-machine-1 for circuit automation bootstrap",
                )
            return None

        if item == "inserter":
            for prerequisite, count in [
                ("electronic-circuit", quantity),
                ("iron-gear-wheel", quantity),
                ("iron-plate", quantity),
            ]:
                decision = self._ensure_item_quantity(observation, player, prerequisite, count)
                if decision is not None:
                    return decision
            if craftable_count(observation, "inserter") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "inserter",
                        "count": min(quantity - inventory_count(observation, "inserter"), craftable_count(observation, "inserter")),
                    },
                    "craft inserter for circuit automation bootstrap",
                )
            return None

        if item == "electronic-circuit":
            decision = self.hand_circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None

        if item == "iron-gear-wheel":
            if bool(_technology_state(observation, "automation").get("researched")):
                decision = BuildItemMallSkill("iron-gear-wheel", max(quantity, 4)).next_action(observation)
                if not decision.done:
                    return decision
                return None
            if craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(quantity - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for circuit automation",
                )
            return self._ensure_item_quantity(observation, player, "iron-plate", 2 * (quantity - inventory_count(observation, "iron-gear-wheel")))

        if item == "iron-plate":
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "small-electric-pole":
            return self.power_skill._ensure_item_quantity(observation, player, item, quantity)

        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for circuit automation",
            )

        return PlannerDecision(None, f"missing {item} and no circuit automation prerequisite path is implemented")


class BuildItemMallSkill:
    """Build a small powered assembler cell for recurring factory-expansion items."""

    def __init__(self, target_item: str = "transport-belt", target_count: int = 20) -> None:
        self.target_item = target_item
        self.target_count = target_count
        self.power_skill = SetupPowerSkill()
        self.research_skill = ResearchAutomationSkill()
        self.iron_skill = IronPlateSkill(target_count=40)
        self.copper_skill = CopperPlateSkill(target_count=20)
        self.circuit_skill = ElectronicCircuitSkill(target_count=10)

    def next_action(
        self,
        observation: dict[str, Any],
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision:
        recipe = RECIPES.get(self.target_item)
        if recipe is None:
            return PlannerDecision(None, f"build item mall recipe is not known: {self.target_item}")

        player = player_position(observation)
        if not bool(_technology_state(observation, "automation").get("researched")):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        power_block = _find_steam_power_block(
            observation,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
        if not _steam_power_ready(power_block):
            decision = self.power_skill.next_action(
                observation,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for power observation to settle")
            return decision

        cell = _find_build_item_mall_cell(
            observation,
            self.target_item,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        ) or _select_build_item_mall_site(
            observation,
            self.target_item,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
        if cell is None:
            return PlannerDecision(None, "cannot find a powered or wireable site for the first build item mall assembler")

        missing_item = _missing_build_item_mall_item(observation, cell)
        if missing_item:
            decision = self._ensure_item_quantity(observation, player, missing_item, _build_item_mall_required_count(cell, missing_item))
            if decision is not None:
                return decision

        if not cell.get("pole_unit_number") and not _build_item_mall_assembler_powered(cell):
            pole_position = cell["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(pole_position)}, "move near planned mall pole")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "extend power for build item mall",
            )

        assembler = cell.get("assembler")
        if assembler is None:
            assembler_position = cell["assembler_position"]
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(assembler_position)}, "move near planned mall assembler")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                    "allow_nearby": False,
                },
                f"place assembler for {self.target_item} mall cell",
            )

        if not assembler.get("electric_network_connected"):
            pole_position = cell["pole_position"]
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, "move near mall pole to connect power")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": cell.get("pole_unit_number"),
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                "connect build item mall pole to nearby electric network",
            )

        if assembler.get("recipe") != self.target_item:
            return self._set_recipe_decision(player, assembler, self.target_item)

        output_count = entity_item_count(assembler, self.target_item)
        if output_count > 0:
            assembler_position = _position(assembler)
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to collect {self.target_item}")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": self.target_item,
                    "count": min(output_count, self.target_count),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"take {self.target_item} from build item mall assembler",
            )

        if _build_item_mall_cell_ready(cell, self.target_item) and total_item_count(observation, self.target_item) >= self.target_count:
            return PlannerDecision(
                None,
                f"build item mall is producing {self.target_item} and target reached: {total_item_count(observation, self.target_item)}/{self.target_count}",
                done=True,
            )

        assembler_position = _position(assembler)
        batch_count = _build_item_mall_batch_count(recipe.products.get(self.target_item, 1.0), self.target_count)
        for ingredient, amount in sorted(recipe.ingredients.items()):
            needed_in_assembler = max(1, int(amount * batch_count))
            if entity_item_count(assembler, ingredient) >= needed_in_assembler:
                continue
            logistics_blocker = _manual_site_input_logistics_blocker(
                observation,
                ingredient,
                _position(assembler),
                consumer_label=f"{self.target_item} mall assembler",
            )
            if logistics_blocker is not None:
                return logistics_blocker
            if inventory_count(observation, ingredient) <= 0:
                decision = self._ensure_item_quantity(
                    observation,
                    player,
                    ingredient,
                    needed_in_assembler,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=assembler_position,
                )
                if decision is not None:
                    return decision
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to insert {ingredient}")
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": ingredient,
                    "count": min(max(1, needed_in_assembler), inventory_count(observation, ingredient)),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"insert {ingredient} into {self.target_item} mall assembler",
            )

        return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for build item mall to produce {self.target_item}")

    def _set_recipe_decision(
        self,
        player: dict[str, float],
        assembler: dict[str, Any],
        recipe: str,
    ) -> PlannerDecision:
        position = _position(assembler)
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": position}, f"move near mall assembler to set {recipe}")
        return PlannerDecision(
            {
                "type": "set_recipe",
                "recipe": recipe,
                "unit_number": assembler.get("unit_number"),
                "name": "assembling-machine-1",
                "position": position,
            },
            f"set build item mall assembler recipe to {recipe}",
        )

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None

        if item == "assembling-machine-1":
            for prerequisite, count in [
                ("electronic-circuit", 3 * quantity),
                ("iron-gear-wheel", 5 * quantity),
                ("iron-plate", 9 * quantity),
            ]:
                decision = self._ensure_item_quantity(
                    observation,
                    player,
                    prerequisite,
                    count,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=reference_position,
                )
                if decision is not None:
                    return decision
            if craftable_count(observation, "assembling-machine-1") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "assembling-machine-1",
                        "count": min(
                            quantity - inventory_count(observation, "assembling-machine-1"),
                            craftable_count(observation, "assembling-machine-1"),
                        ),
                    },
                    "craft assembling-machine-1 for build item mall bootstrap",
                )
            return None

        if item == "iron-gear-wheel":
            if bool(_technology_state(observation, "automation").get("researched")):
                if self.target_item != "iron-gear-wheel":
                    decision = BuildItemMallSkill("iron-gear-wheel", max(quantity, 4)).next_action(
                        observation,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=reference_position,
                    )
                    if not decision.done:
                        return decision
                    return None
                return PlannerDecision(
                    None,
                    "iron gear wheels must be produced by an assembler; refusing hand-crafted gear for build item mall bootstrap",
                )
            if craftable_count(observation, "iron-gear-wheel") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "iron-gear-wheel",
                        "count": min(quantity - inventory_count(observation, "iron-gear-wheel"), craftable_count(observation, "iron-gear-wheel")),
                    },
                    "craft gears for build item mall",
                )
            return self._ensure_item_quantity(
                observation,
                player,
                "iron-plate",
                2 * (quantity - inventory_count(observation, "iron-gear-wheel")),
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )

        if item == "electronic-circuit":
            decision = self.circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None

        if item == "iron-plate":
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            decision = self.copper_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-cable":
            if craftable_count(observation, "copper-cable") > 0:
                return PlannerDecision(
                    {
                        "type": "craft",
                        "recipe": "copper-cable",
                        "count": min(quantity - inventory_count(observation, "copper-cable"), craftable_count(observation, "copper-cable")),
                    },
                    "craft copper cable for build item mall",
                )
            return self._ensure_item_quantity(
                observation,
                player,
                "copper-plate",
                _ceil_div(quantity - inventory_count(observation, "copper-cable"), 2),
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )

        if item == "small-electric-pole":
            return self.power_skill._ensure_item_quantity(observation, player, item, quantity)

        if craftable_count(observation, item) > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": item,
                    "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item)),
                },
                f"craft {item} for build item mall",
            )

        return PlannerDecision(None, f"missing {item} and no build item mall prerequisite path is implemented")


def _research_root(observation: dict[str, Any]) -> dict[str, Any]:
    value = observation.get("research")
    return value if isinstance(value, dict) else {}


def _technology_state(observation: dict[str, Any], technology: str) -> dict[str, Any]:
    research = _research_root(observation)
    technologies = research.get("technologies")
    if not isinstance(technologies, dict):
        return {}
    value = technologies.get(technology)
    return value if isinstance(value, dict) else {}


def _current_research(observation: dict[str, Any]) -> str | None:
    research = _research_root(observation)
    current = research.get("current")
    if not current and isinstance(research.get("queue"), list) and research["queue"]:
        current = research["queue"][0]
    return current if isinstance(current, str) and current else None


def _research_progress(observation: dict[str, Any]) -> float:
    value = _research_root(observation).get("progress")
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _research_pack_goal(observation: dict[str, Any], technology: str, pack_name: str) -> int:
    state = _technology_state(observation, technology)
    try:
        unit_count = int(state.get("research_unit_count") or 10)
    except (TypeError, ValueError):
        unit_count = 10
    ingredients = state.get("ingredients") if isinstance(state.get("ingredients"), dict) else {}
    try:
        pack_amount = int(ingredients.get(pack_name) or 1)
    except (TypeError, ValueError):
        pack_amount = 1
    return max(1, unit_count * pack_amount)


def _find_research_lab(observation: dict[str, Any]) -> dict[str, Any] | None:
    labs = entities_named(observation, "lab")
    if not labs:
        return None
    labs.sort(
        key=lambda item: (
            0 if item.get("electric_network_connected") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return labs[0]


def _lab_powered(lab: dict[str, Any]) -> bool:
    return lab.get("electric_network_connected") is not False


def _power_poles(observation: dict[str, Any]) -> list[dict[str, Any]]:
    names = ["small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation"]
    poles: list[dict[str, Any]] = []
    for name in names:
        poles.extend(entities_named(observation, name))
    return poles


def _nearest_power_pole_to_supply(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = [pole for pole in _power_poles(observation) if distance(_position(pole), position) <= 2.5]
    return _nearest_to(candidates, position) if candidates else None


def _pole_position_to_supply_entity(observation: dict[str, Any], position: dict[str, float]) -> dict[str, float]:
    source = _nearest_to(_power_poles(observation), position)
    if source is None:
        return {"x": position["x"] + 2.0, "y": position["y"]}
    source_position = _position(source)
    dx = source_position["x"] - position["x"]
    dy = source_position["y"] - position["y"]
    length = max(0.001, (dx * dx + dy * dy) ** 0.5)
    return {
        "x": position["x"] + dx / length * 2.0,
        "y": position["y"] + dy / length * 2.0,
    }


def _research_labs(observation: dict[str, Any], *, powered_only: bool = False) -> list[dict[str, Any]]:
    labs = entities_named(observation, "lab")
    if powered_only:
        labs = [lab for lab in labs if _lab_powered(lab)]
    labs.sort(
        key=lambda item: (
            0 if item.get("electric_network_connected") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return labs


def _best_lab_for_pack_insert(observation: dict[str, Any], pack_name: str) -> dict[str, Any] | None:
    labs = _research_labs(observation, powered_only=True)
    if not labs:
        return None
    labs.sort(
        key=lambda item: (
            entity_item_count(item, pack_name),
            float(item.get("distance") or 999999),
        )
    )
    return labs[0]


def _any_lab_has_pack(observation: dict[str, Any], pack_name: str) -> bool:
    return any(entity_item_count(lab, pack_name) > 0 for lab in _research_labs(observation, powered_only=True))


def _select_lab_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("lab_sites")
    if not isinstance(sites, list):
        return None
    candidates = [
        site
        for site in sites
        if isinstance(site, dict)
        and isinstance(site.get("pole_position"), dict)
        and isinstance(site.get("lab_position"), dict)
        and _within_starter_logistics_area(observation, _xy_position(site["lab_position"]))
        and _within_starter_logistics_area(observation, _xy_position(site["pole_position"]))
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _find_circuit_automation_cell(observation: dict[str, Any]) -> dict[str, Any] | None:
    assemblers = entities_named(observation, "assembling-machine-1")
    cable_candidates = [item for item in assemblers if item.get("recipe") == "copper-cable"]
    for cable in cable_candidates:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        if not _layout_within_starter_area(observation, layout):
            continue
        return layout

    circuit_candidates = [item for item in assemblers if item.get("recipe") == "electronic-circuit"]
    for circuit in circuit_candidates:
        circuit_pos = _position(circuit)
        layout = _circuit_cell_layout_from_circuit_position(circuit_pos)
        layout["circuit_assembler"] = circuit
        layout["cable_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["cable_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        if not _layout_within_starter_area(observation, layout):
            continue
        return layout

    unassigned = [item for item in assemblers if not item.get("recipe")]
    for cable in unassigned:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        circuit = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        if circuit is None:
            continue
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = circuit
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        return layout

    for cable in unassigned:
        cable_pos = _position(cable)
        layout = _circuit_cell_layout_from_cable_position(cable_pos)
        layout["cable_assembler"] = cable
        layout["circuit_assembler"] = _entity_near(
            observation,
            "assembling-machine-1",
            layout["circuit_assembler_position"],
            radius=1.5,
        )
        layout["transfer_inserter"] = _entity_near(
            observation,
            "inserter",
            layout["transfer_inserter_position"],
            radius=0.75,
        )
        layout["pole"] = _entity_near(observation, "small-electric-pole", layout["pole_position"], radius=1.0)
        if layout["pole"] is not None:
            layout["pole_unit_number"] = layout["pole"].get("unit_number")
        if not _layout_within_starter_area(observation, layout):
            continue
        return layout

    return None


def _select_circuit_automation_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    sites = observation.get("automation_sites")
    if not isinstance(sites, list):
        return None
    candidates: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        required = [
            "pole_position",
            "cable_assembler_position",
            "circuit_assembler_position",
            "transfer_inserter_position",
        ]
        if not all(isinstance(site.get(key), dict) for key in required):
            continue
        if not all(_within_starter_logistics_area(observation, _xy_position(site[key])) for key in required):
            continue
        candidates.append(
            {
                "pole_position": _xy_position(site["pole_position"]),
                "cable_assembler_position": _xy_position(site["cable_assembler_position"]),
                "circuit_assembler_position": _xy_position(site["circuit_assembler_position"]),
                "transfer_inserter_position": _xy_position(site["transfer_inserter_position"]),
                "transfer_inserter_direction": int(site.get("transfer_inserter_direction") or EAST),
                "pole_unit_number": site.get("pole_unit_number"),
                "source_pole_unit_number": site.get("source_pole_unit_number"),
                "powered": site.get("powered"),
                "distance": site.get("distance"),
                "pole": None,
                "cable_assembler": None,
                "circuit_assembler": None,
                "transfer_inserter": None,
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _circuit_cell_layout_from_cable_position(cable_position: dict[str, float]) -> dict[str, Any]:
    return {
        "pole_position": {"x": cable_position["x"] + 2, "y": cable_position["y"] - 2},
        "cable_assembler_position": cable_position,
        "circuit_assembler_position": {"x": cable_position["x"] + 4, "y": cable_position["y"]},
        "transfer_inserter_position": {"x": cable_position["x"] + 2, "y": cable_position["y"]},
        "transfer_inserter_direction": EAST,
        "pole": None,
        "cable_assembler": None,
        "circuit_assembler": None,
        "transfer_inserter": None,
    }


def _circuit_cell_layout_from_circuit_position(circuit_position: dict[str, float]) -> dict[str, Any]:
    return _circuit_cell_layout_from_cable_position({"x": circuit_position["x"] - 4, "y": circuit_position["y"]})


def _missing_circuit_cell_item(observation: dict[str, Any], line: dict[str, Any]) -> str | None:
    if not line.get("pole_unit_number") and inventory_count(observation, "small-electric-pole") <= 0:
        return "small-electric-pole"
    missing_assemblers = _circuit_cell_required_count(line, "assembling-machine-1")
    if missing_assemblers > inventory_count(observation, "assembling-machine-1"):
        return "assembling-machine-1"
    if line.get("transfer_inserter") is None and inventory_count(observation, "inserter") <= 0:
        return "inserter"
    return None


def _circuit_cell_required_count(line: dict[str, Any], item: str) -> int:
    if item == "assembling-machine-1":
        return sum(1 for key in ("cable_assembler", "circuit_assembler") if line.get(key) is None)
    return 1


def _circuit_cell_ready(line: dict[str, Any]) -> bool:
    cable = line.get("cable_assembler")
    circuit = line.get("circuit_assembler")
    return bool(
        line.get("pole_unit_number")
        and cable
        and circuit
        and line.get("transfer_inserter")
        and cable.get("recipe") == "copper-cable"
        and circuit.get("recipe") == "electronic-circuit"
    )


def _circuit_cell_powered(line: dict[str, Any]) -> bool:
    for key in ("cable_assembler", "circuit_assembler", "transfer_inserter"):
        entity = line.get(key)
        if isinstance(entity, dict) and entity.get("electric_network_connected"):
            return True
    return False


def _find_build_item_mall_cell(
    observation: dict[str, Any],
    target_item: str,
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    assemblers = entities_named(observation, "assembling-machine-1")
    candidates = [
        item
        for item in assemblers
        if item.get("recipe") == target_item
        and _within_allowed_factory_area(
            observation,
            _position(item),
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
    ]
    if not candidates:
        candidates = [
            item
            for item in assemblers
            if _available_unassigned_mall_assembler(
                observation,
                item,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
        ]
    if not candidates and target_item == "iron-gear-wheel" and inventory_count(observation, "assembling-machine-1") <= 0:
        candidates = [
            item
            for item in assemblers
            if item.get("electric_network_connected")
            and item.get("recipe") not in {"copper-cable", "electronic-circuit"}
            and _within_allowed_factory_area(
                observation,
                _position(item),
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
        ]
    if not candidates and target_item == "assembling-machine-1" and inventory_count(observation, "assembling-machine-1") <= 0:
        candidates = [
            item
            for item in assemblers
            if item.get("electric_network_connected")
            and item.get("recipe") not in {"copper-cable", "electronic-circuit"}
            and _within_allowed_factory_area(
                observation,
                _position(item),
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
        ]
    if not candidates:
        return None
    assembler = min(candidates, key=lambda item: float(item.get("distance") or 999999))
    assembler_position = _position(assembler)
    pole = _nearest_to(entities_named(observation, "small-electric-pole"), assembler_position)
    pole_in_reach = pole is not None and distance(_position(pole), assembler_position) <= 7.5
    pole_position = _position(pole) if pole_in_reach else {
        "x": assembler_position["x"] + 2,
        "y": assembler_position["y"] - 2,
    }
    return {
        "pole_position": pole_position,
        "assembler_position": assembler_position,
        "pole": pole if pole_in_reach else None,
        "pole_unit_number": pole.get("unit_number") if pole_in_reach else None,
        "assembler": assembler,
        "powered": assembler.get("electric_network_connected"),
    }


def _available_unassigned_mall_assembler(
    observation: dict[str, Any],
    assembler: dict[str, Any],
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> bool:
    if assembler.get("recipe"):
        return False
    if not _within_allowed_factory_area(
        observation,
        _position(assembler),
        allow_existing_remote=allow_existing_remote,
        reference_position=reference_position,
    ):
        return False
    if not _near_recipe_assembler(observation, assembler, {"copper-cable", "electronic-circuit"}, radius=5.5):
        return True
    return bool(allow_existing_remote and reference_position is not None and assembler.get("electric_network_connected"))


def _select_build_item_mall_site(
    observation: dict[str, Any],
    target_item: str,
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    sites = observation.get("automation_sites")
    if not isinstance(sites, list):
        return None
    candidates: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        if not isinstance(site.get("pole_position"), dict) or not isinstance(site.get("cable_assembler_position"), dict):
            continue
        pole_position = _xy_position(site["pole_position"])
        assembler_position = _xy_position(site["cable_assembler_position"])
        if not (
            _within_allowed_factory_area(
                observation,
                pole_position,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
            and _within_allowed_factory_area(
                observation,
                assembler_position,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
        ):
            continue
        pole = _entity_near(observation, "small-electric-pole", pole_position, radius=1.0)
        assembler = _entity_near(observation, "assembling-machine-1", assembler_position, radius=1.5)
        source_pole_unit_number = site.get("source_pole_unit_number")
        pole_unit_number = site.get("pole_unit_number") or (pole.get("unit_number") if pole else None)
        if assembler is not None and assembler.get("recipe") not in (None, "", target_item):
            sidecar_position = _select_build_item_sidecar_position(
                observation,
                assembler_position,
                power_positions=[pole_position],
            )
            if sidecar_position is None:
                continue
            assembler_position = sidecar_position
            assembler = _entity_near(observation, "assembling-machine-1", assembler_position, radius=1.5)
            if not _position_is_supplied_by_small_pole(observation, assembler_position, extra_power_positions=[pole_position]):
                sidecar_pole_position = _select_build_item_sidecar_supply_pole_position(
                    observation,
                    assembler_position,
                    source_position=pole_position,
                )
                if sidecar_pole_position is None:
                    continue
                source_pole_unit_number = site.get("pole_unit_number") or source_pole_unit_number
                pole_position = sidecar_pole_position
                pole = _entity_near(observation, "small-electric-pole", pole_position, radius=1.0)
                pole_unit_number = pole.get("unit_number") if pole else None
        candidates.append(
            {
                "pole_position": pole_position,
                "assembler_position": assembler_position,
                "pole_unit_number": pole_unit_number,
                "source_pole_unit_number": source_pole_unit_number,
                "powered": bool(site.get("powered") or (assembler and assembler.get("electric_network_connected"))),
                "distance": site.get("distance"),
                "pole": pole,
                "assembler": assembler,
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _select_build_item_sidecar_position(
    observation: dict[str, Any],
    anchor_position: dict[str, float],
    *,
    power_positions: list[dict[str, float]] | None = None,
) -> dict[str, float] | None:
    offsets = [
        {"x": -3.0, "y": 0.0},
        {"x": 3.0, "y": 0.0},
        {"x": 0.0, "y": -3.0},
        {"x": 0.0, "y": 3.0},
        {"x": -3.0, "y": -3.0},
        {"x": 3.0, "y": -3.0},
        {"x": -3.0, "y": 3.0},
        {"x": 3.0, "y": 3.0},
    ]
    candidates: list[dict[str, float]] = []
    for offset in offsets:
        candidate = {
            "x": anchor_position["x"] + offset["x"],
            "y": anchor_position["y"] + offset["y"],
        }
        if _build_item_sidecar_position_clear(observation, candidate):
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=lambda item: _build_item_sidecar_power_score(observation, item, power_positions=power_positions))
    return candidates[0]


def _build_item_sidecar_position_clear(observation: dict[str, Any], position: dict[str, float]) -> bool:
    if not _within_starter_logistics_area(observation, position):
        return False
    large_entities = ASSEMBLER_ENTITY_NAMES | {"lab", "stone-furnace", "burner-mining-drill", "boiler", "steam-engine"}
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        if name == "character" or name in {"tree", "fish"}:
            continue
        threshold = 3.0 if name in large_entities else 2.0
        if distance(_position(entity), position) < threshold:
            return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("position"), dict):
            continue
        if str(resource.get("name") or "") in PROTECTED_RESOURCE_NAMES and distance(_position(resource), position) < 2.5:
            return False
    return True


def _select_build_item_sidecar_supply_pole_position(
    observation: dict[str, Any],
    assembler_position: dict[str, float],
    *,
    source_position: dict[str, float] | None = None,
) -> dict[str, float] | None:
    existing = _nearest_small_pole_supplying_position(observation, assembler_position)
    if existing is not None:
        return _position(existing)
    offsets = [
        {"x": 2.0, "y": -2.0},
        {"x": -2.0, "y": -2.0},
        {"x": 2.0, "y": 2.0},
        {"x": -2.0, "y": 2.0},
        {"x": 0.0, "y": -2.0},
        {"x": 0.0, "y": 2.0},
        {"x": 2.0, "y": 0.0},
        {"x": -2.0, "y": 0.0},
    ]
    candidates: list[dict[str, float]] = []
    for offset in offsets:
        candidate = {
            "x": assembler_position["x"] + offset["x"],
            "y": assembler_position["y"] + offset["y"],
        }
        if _build_item_sidecar_pole_position_clear(observation, candidate):
            candidates.append(candidate)
    if not candidates:
        return None
    if source_position is not None:
        candidates.sort(key=lambda item: distance(item, source_position))
    return candidates[0]


def _build_item_sidecar_pole_position_clear(observation: dict[str, Any], position: dict[str, float]) -> bool:
    if not _within_starter_logistics_area(observation, position):
        return False
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        if name == "character" or name in {"tree", "fish"}:
            continue
        if distance(_position(entity), position) < 1.5:
            return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("position"), dict):
            continue
        if str(resource.get("name") or "") in PROTECTED_RESOURCE_NAMES and distance(_position(resource), position) < 1.0:
            return False
    return True


def _build_item_sidecar_power_score(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    power_positions: list[dict[str, float]] | None = None,
) -> tuple[int, float]:
    poles = [_position(pole) for pole in _power_poles(observation)]
    poles.extend(_xy_position(item) for item in (power_positions or []) if isinstance(item, dict))
    if not poles:
        return (1, 999999.0)
    supplying = [pole_position for pole_position in poles if _small_pole_supplies_position(pole_position, position)]
    if supplying:
        return (0, min(distance(pole_position, position) for pole_position in supplying))
    return (1, min(distance(pole_position, position) for pole_position in poles))


def _position_is_supplied_by_small_pole(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    extra_power_positions: list[dict[str, float]] | None = None,
) -> bool:
    if _nearest_small_pole_supplying_position(observation, position) is not None:
        return True
    return any(_small_pole_supplies_position(_xy_position(item), position) for item in (extra_power_positions or []) if isinstance(item, dict))


def _nearest_small_pole_supplying_position(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = [
        pole
        for pole in _power_poles(observation)
        if str(pole.get("name") or "") == "small-electric-pole" and _small_pole_supplies_position(_position(pole), position)
    ]
    return _nearest_to(candidates, position) if candidates else None


def _small_pole_supplies_position(pole_position: dict[str, float], position: dict[str, float]) -> bool:
    return abs(float(pole_position["x"]) - float(position["x"])) <= 2.5 and abs(float(pole_position["y"]) - float(position["y"])) <= 2.5


def _missing_build_item_mall_item(observation: dict[str, Any], cell: dict[str, Any]) -> str | None:
    if not cell.get("pole_unit_number") and not _build_item_mall_assembler_powered(cell) and inventory_count(observation, "small-electric-pole") <= 0:
        return "small-electric-pole"
    if cell.get("assembler") is None and inventory_count(observation, "assembling-machine-1") <= 0:
        return "assembling-machine-1"
    return None


def _build_item_mall_required_count(cell: dict[str, Any], item: str) -> int:
    if item == "assembling-machine-1":
        return 1 if cell.get("assembler") is None else 0
    return 1


def _build_item_mall_cell_ready(cell: dict[str, Any], target_item: str) -> bool:
    assembler = cell.get("assembler")
    return bool(
        isinstance(assembler, dict)
        and assembler.get("electric_network_connected")
        and assembler.get("recipe") == target_item
    )


def _build_item_mall_assembler_powered(cell: dict[str, Any]) -> bool:
    assembler = cell.get("assembler")
    return bool(isinstance(assembler, dict) and assembler.get("electric_network_connected"))


def _build_item_mall_batch_count(product_count: float, target_count: int) -> int:
    try:
        per_batch = max(1, int(product_count))
    except (TypeError, ValueError):
        per_batch = 1
    return max(1, min(4, _ceil_div(max(1, target_count), per_batch)))


def _manual_site_input_logistics_blocker(
    observation: dict[str, Any],
    item: str,
    consumer_position: dict[str, float],
    *,
    consumer_label: str,
) -> PlannerDecision | None:
    if item not in AUTOMATED_SITE_INPUT_ITEMS:
        return None
    if not bool(_technology_state(observation, "automation").get("researched")):
        return None
    source = _nearest_factory_source_site(observation, item, consumer_position)
    if source is None:
        return None
    source_distance = distance(source.position, consumer_position)
    if source_distance <= MANUAL_SITE_INPUT_RADIUS:
        return None
    return PlannerDecision(
        None,
        (
            f"{consumer_label} needs a {item} logistic line from {source.site_id} "
            f"({source_distance:.0f} tiles); refusing repeated hand-carry between distant sites"
        ),
    )


def _nearest_factory_source_site(
    observation: dict[str, Any],
    item: str,
    consumer_position: dict[str, float],
) -> Any | None:
    source_kinds = {"mining_patch", "plate_smelting_line", "build_item_mall", "assembler_cell", "circuit_automation"}
    candidates = [
        site
        for site in estimate_factory_sites(observation)
        if site.item == item and site.kind in source_kinds
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda site: distance(site.position, consumer_position))


def _near_recipe_assembler(
    observation: dict[str, Any],
    assembler: dict[str, Any],
    recipes: set[str],
    radius: float,
) -> bool:
    position = _position(assembler)
    for other in entities_named(observation, "assembling-machine-1"):
        if other is assembler:
            continue
        if other.get("recipe") in recipes and distance(position, _position(other)) <= radius:
            return True
    return False


def _xy_position(value: dict[str, Any]) -> dict[str, float]:
    return {
        "x": float(value.get("x") or 0.0),
        "y": float(value.get("y") or 0.0),
    }
