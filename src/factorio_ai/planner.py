from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import ceil, floor
from typing import Any

from .blueprints import decode_blueprint_string, encode_blueprint_entities
from .knowledge import RECIPES, TECHNOLOGIES
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
STARTER_BOILER_FUEL_FEED_ROUTE_LIMIT = 192.0
EMERGENCY_BOILER_BOOTSTRAP_FUEL_INSERT = 5
STEAM_POWER_BOILER_FUEL_RESERVE = 10
STARTER_FUEL_BATCH_COUNT = 30
DIRECT_SMELTING_FUEL_RESERVE = 20
DIRECT_SMELTING_CELL_TARGET_PLATES = 20
DIRECT_SMELTING_MIN_PARALLEL_CELLS = 2
DIRECT_SMELTING_MAX_PARALLEL_CELLS = 4
STARTER_COAL_SUPPLY_DRILL_TARGET = 4
SMELTING_LINE_FUEL_RESERVE = {
    "drill": 20,
    "inserter": 4,
    "furnace": 20,
}
SMELTING_LINE_FUEL_INSERT = {
    "drill": STARTER_FUEL_BATCH_COUNT,
    "inserter": 8,
    "furnace": STARTER_FUEL_BATCH_COUNT,
}
BURNER_FUEL_ITEMS = ("coal", "wood", "solid-fuel", "rocket-fuel")
ASSEMBLER_ENTITY_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
FURNACE_ENTITY_NAMES = {"stone-furnace", "steel-furnace", "electric-furnace"}
_STARTER_ANCHOR_CACHE: dict[int, tuple[int, int, list[dict[str, float]]]] = {}
POWER_CONNECTOR_NAMES = {"small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation"}
PROTECTED_RESOURCE_NAMES = {"iron-ore", "copper-ore", "coal", "stone", "uranium-ore"}
PRESERVED_STARTER_ARTIFACT_KEYWORDS = ("crash", "wreck", "spaceship")
SITE_GATE_INPUT_STOCK_FALLBACK = 20
SITE_GATE_LOCAL_LOGISTICS_RADIUS = 96.0
SITE_PLACEMENT_SEARCH_STEP = 8
SITE_PLACEMENT_SEARCH_RADIUS = 48
MANUAL_SITE_INPUT_RADIUS = 48.0
POWER_EXPANSION_CLEARANCE_RADIUS = 12.0
PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT = 128.0
GEAR_MALL_RELOCATION_FIXED_COST = 18.0
GEAR_MALL_RELOCATION_POWER_POLE_COST = 2.0
GEAR_MALL_PLATE_BELT_TILE_COST = 1.0
GEAR_MALL_RELOCATION_ADVANTAGE_RATIO = 0.75
GEAR_BELT_MALL_ASSEMBLER_SPACING = 4.0
AUTOMATED_SITE_INPUT_ITEMS = {
    "iron-plate",
    "copper-plate",
    "iron-gear-wheel",
    "copper-cable",
    "electronic-circuit",
    "automation-science-pack",
    "logistic-science-pack",
}
USER_OUTPUT_MALL_ITEMS = {
    "iron-gear-wheel",
    "transport-belt",
    "underground-belt",
    "splitter",
    "inserter",
    "long-handed-inserter",
    "fast-inserter",
    "small-electric-pole",
    "assembling-machine-1",
    "burner-mining-drill",
    "electric-mining-drill",
    "lab",
    "pipe",
    "pipe-to-ground",
    "stone-furnace",
    "steel-furnace",
}
BLUEPRINT_INSERTER_NAMES = {
    "burner-inserter",
    "inserter",
    "long-handed-inserter",
    "fast-inserter",
    "stack-inserter",
    "bulk-inserter",  # Factorio 2.0 successor to stack-inserter (reach 1); high-rate cell links use it
}


@dataclass(frozen=True)
class FactorySourceReference:
    site_id: str
    position: dict[str, float]


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

    for issue in _power_expansion_clearance_issues(sites):
        issues.append(issue)

    gear_mall_compaction_issue = _gear_mall_plate_compaction_issue(observation)
    if gear_mall_compaction_issue is not None:
        issues.append(gear_mall_compaction_issue)

    issues.sort(key=lambda item: int(item.get("severity") or 0), reverse=True)
    return issues[:12]


def _power_expansion_clearance_issues(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    power_sites = [
        site for site in sites if isinstance(site, dict) and str(site.get("kind") or "") == "steam_power"
    ]
    production_sites = [
        site
        for site in sites
        if isinstance(site, dict)
        and str(site.get("kind") or "") in {"plate_smelting_line", "build_item_mall", "assembler_cell", "circuit_automation", "research_lab_block"}
    ]
    issues: list[dict[str, Any]] = []
    for power_site in power_sites:
        power_position = power_site.get("position") if isinstance(power_site.get("position"), dict) else None
        if power_position is None:
            continue
        nearest: dict[str, Any] | None = None
        nearest_distance = 999999.0
        for site in production_sites:
            site_position = site.get("position") if isinstance(site.get("position"), dict) else None
            if site_position is None:
                continue
            gap = distance(power_position, site_position)
            if gap < nearest_distance:
                nearest = site
                nearest_distance = gap
        if nearest is None or nearest_distance > POWER_EXPANSION_CLEARANCE_RADIUS:
            continue
        issues.append(
            {
                "kind": "power_expansion_clearance_risk",
                "severity": 68,
                "site_id": power_site.get("site_id"),
                "item": "electric-power",
                "detail": (
                    f"{nearest.get('kind')} is {nearest_distance:.1f} tiles from steam power, reducing room for boiler/engine expansion"
                ),
                "recommendation": (
                    "treat power-block adjacency as a placement cost: reserve expansion lanes for extra boilers, engines, poles, "
                    "coal/water input, and later power rebuilds unless the nearby factory has a stronger locality benefit"
                ),
                "parameters": {
                    "power_site_id": power_site.get("site_id"),
                    "neighbor_site_id": nearest.get("site_id"),
                    "neighbor_kind": nearest.get("kind"),
                    "distance_tiles": round(nearest_distance, 1),
                    "clearance_radius": POWER_EXPANSION_CLEARANCE_RADIUS,
                },
            }
        )
    return issues


def _gear_mall_plate_compaction_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(_technology_state(observation, "automation").get("researched")):
        return None
    if total_item_count(observation, "transport-belt") > 0:
        return None
    layout = _find_iron_plate_logistic_line_to_gear_mall_layout(observation)
    if layout is None:
        layout = _find_compact_gear_belt_mall_relocation_layout(observation)
    if layout is None:
        return None
    source = layout.get("source")
    gear_assembler = layout.get("gear_assembler")
    if not isinstance(source, dict) or not isinstance(gear_assembler, dict):
        return None
    source_position = _position(source)
    gear_position = _position(gear_assembler)
    source_distance = distance(source_position, gear_position)
    route_cost = _gear_mall_plate_layout_cost_estimate(observation, source_position, gear_position)
    if route_cost.get("route_cost_preference") != "relocate_mall_to_iron_source":
        return None
    return {
        "kind": "distant_gear_mall_iron_source",
        "severity": 91,
        "site_id": f"gear:{gear_assembler.get('unit_number')}:iron-source:{source.get('unit_number')}",
        "item": "iron-plate",
        "detail": (
            f"gear/belt mall iron route cost favors relocation or a trunk corridor over a {source_distance:.0f}-tile pre-rail belt recovery"
        ),
        "recommendation": (
            "plan compact relocation of the gear/belt mall near iron-plate production or reserve a validated "
            "trunk corridor before extending the pre-rail belt route"
        ),
        "parameters": {
            "gear_assembler_unit": gear_assembler.get("unit_number"),
            "iron_source_unit": source.get("unit_number"),
            "source_distance_tiles": round(source_distance, 1),
            "transport_belts_available": False,
            **route_cost,
        },
    }


def _gear_mall_plate_layout_cost_estimate(
    observation: dict[str, Any],
    source_position: dict[str, float],
    gear_position: dict[str, float],
) -> dict[str, Any]:
    source_distance = distance(source_position, gear_position)
    belt_tiles = int(ceil(max(0.0, source_distance)))
    belt_cost = round(belt_tiles * GEAR_MALL_PLATE_BELT_TILE_COST, 1)
    power_distance = _nearest_power_anchor_distance(observation, source_position)
    if power_distance is None:
        return {
            "belt_route_tiles_estimate": belt_tiles,
            "belt_route_cost": belt_cost,
            "power_anchor_distance_tiles": None,
            "relocation_power_poles_estimate": None,
            "relocation_cost": None,
            "route_cost_preference": (
                "relocate_mall_to_iron_source"
                if source_distance > PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT
                else "build_belt_route"
            ),
        }

    power_poles = int(ceil(max(0.0, power_distance) / _power_wire_reach("small-electric-pole")))
    relocation_cost = round(
        GEAR_MALL_RELOCATION_FIXED_COST + power_poles * GEAR_MALL_RELOCATION_POWER_POLE_COST,
        1,
    )
    preference = (
        "relocate_mall_to_iron_source"
        if relocation_cost < belt_cost * GEAR_MALL_RELOCATION_ADVANTAGE_RATIO
        else "build_belt_route"
    )
    return {
        "belt_route_tiles_estimate": belt_tiles,
        "belt_route_cost": belt_cost,
        "power_anchor_distance_tiles": round(power_distance, 1),
        "relocation_power_poles_estimate": power_poles,
        "relocation_cost": relocation_cost,
        "route_cost_preference": preference,
    }


def _nearest_power_anchor_distance(observation: dict[str, Any], target_position: dict[str, float]) -> float | None:
    distances: list[float] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name not in POWER_CONNECTOR_NAMES and entity.get("electric_network_connected") is not True:
            continue
        position = entity.get("position")
        if not isinstance(position, dict):
            continue
        distances.append(distance(position, target_position))
    return min(distances) if distances else None


def _splitter_fanout_opportunities(observation: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        item = str(link.get("item") or "")
        source = str(link.get("from_site") or "")
        target = str(link.get("to_site") or "")
        if not item or not source or not target:
            continue
        if item not in AUTOMATED_SITE_INPUT_ITEMS:
            continue
        status = str(link.get("status") or "")
        if status not in {"route_needed", "missing_source", "incomplete", "blocked", "active"}:
            continue
        grouped.setdefault((source, item), []).append(link)

    researched = bool(_technology_state(observation, "logistics").get("researched"))
    opportunities: list[dict[str, Any]] = []
    for (source, item), rows in sorted(grouped.items()):
        targets = sorted({str(row.get("to_site") or "") for row in rows if row.get("to_site")})
        if len(targets) < 2:
            continue
        statuses = Counter(str(row.get("status") or "") for row in rows)
        opportunities.append(
            {
                "kind": "splitter_output_fanout_needed",
                "severity": 86 if researched else 66,
                "site_id": f"fanout:{source}:{item}",
                "item": item,
                "detail": f"{source} sends {item} to {len(targets)} consumers: {', '.join(targets[:4])}",
                "recommendation": (
                    "place a splitter near the source output and branch from that splitter; do not pull two separate belts directly "
                    "from the same assembler output"
                ),
                "parameters": {
                    "source_site": source,
                    "consumer_sites": targets[:8],
                    "consumer_count": len(targets),
                    "logistics_researched": researched,
                    "required_item": "splitter",
                    "link_status_counts": dict(statuses),
                },
            }
        )
    return opportunities


def factory_layout_opportunities(observation: dict[str, Any]) -> list[dict[str, Any]]:
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    opportunities: list[dict[str, Any]] = []

    unlock_opportunity = _unlock_retool_opportunity(observation, sites, entities)
    if unlock_opportunity is not None:
        opportunities.append(unlock_opportunity)
    opportunities.extend(_splitter_fanout_opportunities(observation, links))

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


def _unlock_retool_opportunity(
    observation: dict[str, Any],
    sites: list[dict[str, Any]],
    entities: list[Any],
) -> dict[str, Any] | None:
    unlocks = _layout_unlock_context(observation)
    considered = _layout_considered_unlocked_items(unlocks)
    if not considered or not sites:
        return None

    retool_tools: list[str] = []
    affected_site_ids: list[str] = []
    affected_kinds: set[str] = set()
    obsolete_patterns: list[str] = []

    assembler_sites = [
        site
        for site in sites
        if str(site.get("kind") or "") in {"assembler_cell", "circuit_automation", "build_item_mall"}
    ]
    smelting_sites = [site for site in sites if str(site.get("kind") or "") == "plate_smelting_line"]
    assembler_entities = [
        entity
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
    ]

    long_handed_state = unlocks.get("long_handed_inserter") if isinstance(unlocks.get("long_handed_inserter"), dict) else {}
    if bool(long_handed_state.get("available")) and assembler_sites:
        long_handed_count = _entity_count_for_layout(observation, "long-handed-inserter")
        if long_handed_count <= 0:
            retool_tools.append("long-handed-inserter")
            affected_site_ids.extend(str(site.get("site_id") or "") for site in assembler_sites[:6])
            affected_kinds.update(str(site.get("kind") or "") for site in assembler_sites)
            obsolete_patterns.append("one-tile-only inserter rows where a second shared input lane would reduce belt doglegs")

    preferred_assembler, preferred_speed = _preferred_assembler_for_layout(unlocks)
    if preferred_assembler != "assembling-machine-1" and any(
        isinstance(entity, dict) and str(entity.get("name") or "") == "assembling-machine-1"
        for entity in entities
    ):
        retool_tools.append(preferred_assembler)
        affected_site_ids.extend(str(site.get("site_id") or "") for site in assembler_sites[:6])
        affected_kinds.update(str(site.get("kind") or "") for site in assembler_sites)
        obsolete_patterns.append(f"assembling-machine-1 cells that can be rerated with {preferred_assembler} speed {preferred_speed}")

    preferred_furnace, furnace_rate, _ = _preferred_furnace_for_layout(unlocks)
    if preferred_furnace != "stone-furnace" and any(
        isinstance(entity, dict) and str(entity.get("name") or "") == "stone-furnace"
        for entity in entities
    ):
        retool_tools.append(preferred_furnace)
        affected_site_ids.extend(str(site.get("site_id") or "") for site in smelting_sites[:6])
        affected_kinds.update(str(site.get("kind") or "") for site in smelting_sites)
        obsolete_patterns.append(f"stone-furnace columns that can be rerated around {preferred_furnace} {furnace_rate}/min throughput")

    mining_drills = unlocks.get("mining_drills") if isinstance(unlocks.get("mining_drills"), dict) else {}
    electric_drill_state = (
        mining_drills.get("electric-mining-drill")
        if isinstance(mining_drills.get("electric-mining-drill"), dict)
        else {}
    )
    burner_drills_present = any(
        isinstance(entity, dict) and str(entity.get("name") or "") == "burner-mining-drill"
        for entity in entities
    )
    if bool(electric_drill_state.get("available")) and burner_drills_present:
        affected_smelting_sites = [
            site
            for site in smelting_sites
            if any("burner-mining-drill" in str(machine) for machine in (site.get("machines") or []))
        ] or smelting_sites
        retool_tools.append("electric-mining-drill")
        affected_site_ids.extend(str(site.get("site_id") or "") for site in affected_smelting_sites[:6])
        affected_kinds.update(str(site.get("kind") or "") for site in affected_smelting_sites)
        obsolete_patterns.append(
            "burner-mining-drill mining/smelting blocks that should be benchmarked for electric-drill rebuild or relocation"
        )

    module_group = unlocks.get("modules") if isinstance(unlocks.get("modules"), dict) else {}
    module_tools = [
        str(name)
        for name, state in module_group.items()
        if isinstance(state, dict) and bool(state.get("available")) and assembler_entities
    ]
    if module_tools:
        retool_tools.extend(module_tools)
        affected_site_ids.extend(str(site.get("site_id") or "") for site in assembler_sites[:6])
        affected_kinds.update(str(site.get("kind") or "") for site in assembler_sites)
        obsolete_patterns.append("unmoduled assembler counts whose optimal footprint, power draw, and pollution may change")

    beacon_state = unlocks.get("beacons") if isinstance(unlocks.get("beacons"), dict) else {}
    beacon_available = bool(
        isinstance(beacon_state.get("beacon"), dict)
        and beacon_state["beacon"].get("available")
        and assembler_entities
    )
    if beacon_available:
        retool_tools.append("beacon")
        affected_site_ids.extend(str(site.get("site_id") or "") for site in assembler_sites[:6])
        affected_kinds.update(str(site.get("kind") or "") for site in assembler_sites)
        obsolete_patterns.append("dense pre-beacon rows that may need reserved beacon spacing")

    retool_tools = sorted(dict.fromkeys(item for item in retool_tools if item))
    affected_site_ids = sorted(dict.fromkeys(item for item in affected_site_ids if item))
    if not retool_tools or not affected_site_ids:
        return None

    severity = min(89, 74 + len(retool_tools) * 3)
    if "long-handed-inserter" in retool_tools:
        severity += 2
    return {
        "kind": "unlock_layout_reassessment",
        "severity": min(92, severity),
        "site_id": "layout-capability:unlocked-tools",
        "item": "factory_layout",
        "detail": (
            "new layout-capable items are available but existing sites still match earlier layout assumptions: "
            + ", ".join(retool_tools)
        ),
        "recommendation": (
            "rerank affected site layouts with the newly available inserters, machines, modules, furnaces, or beacons "
            "before copying old patterns into more factory blocks"
        ),
        "parameters": {
            "considered_unlocked_items": considered,
            "retool_tools": retool_tools,
            "affected_site_ids": affected_site_ids[:8],
            "affected_site_kinds": sorted(affected_kinds),
            "obsolete_patterns": obsolete_patterns[:6],
        },
    }


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
    layout_unlocks = _layout_unlock_context(observation)
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
                _green_circuit_layout_candidate(
                    recipe_counts,
                    observation,
                    sites,
                    current_circuit_sites,
                    layout_unlocks=layout_unlocks,
                ),
                _combined_site_blueprint(
                    "before-green-circuit-block",
                    current_circuit_sites,
                    "Observed current green-circuit/cable machine footprint before ratio rebalance.",
                ),
            )
        )

    unlock_opportunity = next(
        (
            item
            for item in opportunities
            if isinstance(item, dict) and str(item.get("kind") or "") == "unlock_layout_reassessment"
        ),
        None,
    )
    if isinstance(unlock_opportunity, dict):
        candidates.append(
            _unlock_retooling_candidate(
                unlock_opportunity,
                sites,
                layout_unlocks=layout_unlocks,
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
                    _smelting_standardization_candidate(item, rows, observation, layout_unlocks=layout_unlocks),
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
                _mall_compaction_candidate(mall_sites, observation, layout_unlocks=layout_unlocks),
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


def _layout_unlock_context(observation: dict[str, Any]) -> dict[str, Any]:
    long_handed = {
        "available": _technology_researched_for_layout(observation, "long-inserters")
        or _recipe_unlocked_for_layout(observation, "long-handed-inserter")
        or total_item_count(observation, "long-handed-inserter") > 0
        or _recipe_assembler_exists_for_layout(observation, "long-handed-inserter"),
        "researched": _technology_researched_for_layout(observation, "long-inserters"),
        "recipe_unlocked": _recipe_unlocked_for_layout(observation, "long-handed-inserter"),
        "stock": total_item_count(observation, "long-handed-inserter"),
        "automated": _recipe_assembler_exists_for_layout(observation, "long-handed-inserter"),
    }
    modules = {
        name: {
            "available": _technology_researched_for_layout(observation, name)
            or _recipe_unlocked_for_layout(observation, name)
            or total_item_count(observation, name) > 0,
            "researched": _technology_researched_for_layout(observation, name),
            "recipe_unlocked": _recipe_unlocked_for_layout(observation, name),
            "stock": total_item_count(observation, name),
        }
        for name in ("speed-module", "productivity-module", "efficiency-module")
    }
    machine_techs = {"assembling-machine-2": "automation-2", "assembling-machine-3": "automation-3"}
    machines = {
        name: {
            "available": _technology_researched_for_layout(observation, technology)
            or _recipe_unlocked_for_layout(observation, name)
            or total_item_count(observation, name) > 0
            or _entity_exists_for_layout(observation, name),
            "researched": _technology_researched_for_layout(observation, technology),
            "recipe_unlocked": _recipe_unlocked_for_layout(observation, name),
            "stock": total_item_count(observation, name),
            "built": _entity_count_for_layout(observation, name),
            "layout_impact": "higher tier machines change recipe rate, module slot assumptions, power demand, and site footprint",
        }
        for name, technology in machine_techs.items()
    }
    furnace_techs = {"steel-furnace": "advanced-material-processing", "electric-furnace": "advanced-material-processing-2"}
    furnaces = {
        name: {
            "available": _technology_researched_for_layout(observation, technology)
            or _recipe_unlocked_for_layout(observation, name)
            or total_item_count(observation, name) > 0
            or _entity_exists_for_layout(observation, name),
            "researched": _technology_researched_for_layout(observation, technology),
            "recipe_unlocked": _recipe_unlocked_for_layout(observation, name),
            "stock": total_item_count(observation, name),
            "built": _entity_count_for_layout(observation, name),
            "layout_impact": "higher tier furnaces change smelting column length, power/fuel routing, and pollution tradeoffs",
        }
        for name, technology in furnace_techs.items()
    }
    mining_drills = {
        "electric-mining-drill": {
            "available": _technology_researched_for_layout(observation, "electric-mining-drill")
            or _recipe_unlocked_for_layout(observation, "electric-mining-drill")
            or total_item_count(observation, "electric-mining-drill") > 0
            or _entity_exists_for_layout(observation, "electric-mining-drill")
            or _recipe_assembler_exists_for_layout(observation, "electric-mining-drill"),
            "researched": _technology_researched_for_layout(observation, "electric-mining-drill"),
            "recipe_unlocked": _recipe_unlocked_for_layout(observation, "electric-mining-drill"),
            "stock": total_item_count(observation, "electric-mining-drill"),
            "built": _entity_count_for_layout(observation, "electric-mining-drill"),
            "automated": _recipe_assembler_exists_for_layout(observation, "electric-mining-drill"),
            "layout_impact": (
                "electric drills remove burner fuel logistics and can make starter burner-mining layouts obsolete"
            ),
        }
    }
    beacons = {
        "beacon": {
            "available": _technology_researched_for_layout(observation, "effect-transmission")
            or _recipe_unlocked_for_layout(observation, "beacon")
            or total_item_count(observation, "beacon") > 0
            or _entity_exists_for_layout(observation, "beacon"),
            "researched": _technology_researched_for_layout(observation, "effect-transmission"),
            "recipe_unlocked": _recipe_unlocked_for_layout(observation, "beacon"),
            "stock": total_item_count(observation, "beacon"),
            "built": _entity_count_for_layout(observation, "beacon"),
            "layout_impact": "beacons need reserved spacing and can make compact pre-beacon layouts obsolete",
        }
    }
    return {
        "long_handed_inserter": long_handed,
        "modules": modules,
        "machines": machines,
        "furnaces": furnaces,
        "mining_drills": mining_drills,
        "beacons": beacons,
        "rerank_trigger": bool(
            long_handed["available"]
            or any(row["available"] for row in modules.values())
            or any(row["available"] for row in machines.values())
            or any(row["available"] for row in furnaces.values())
            or any(row["available"] for row in mining_drills.values())
            or any(row["available"] for row in beacons.values())
        ),
    }


def _technology_researched_for_layout(observation: dict[str, Any], technology: str) -> bool:
    return bool(_technology_state(observation, technology).get("researched"))


def _recipe_unlocked_for_layout(observation: dict[str, Any], recipe: str) -> bool:
    recipes = observation.get("recipe_unlocks") if isinstance(observation.get("recipe_unlocks"), dict) else {}
    state = recipes.get(recipe)
    return bool(isinstance(state, dict) and state.get("enabled"))


def _recipe_assembler_exists_for_layout(observation: dict[str, Any], recipe: str) -> bool:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    return any(
        isinstance(entity, dict)
        and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
        and entity.get("recipe") == recipe
        and entity.get("electric_network_connected") is not False
        for entity in entities
    )


def _entity_exists_for_layout(observation: dict[str, Any], name: str) -> bool:
    return _entity_count_for_layout(observation, name) > 0


def _entity_count_for_layout(observation: dict[str, Any], name: str) -> int:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    return sum(1 for entity in entities if isinstance(entity, dict) and str(entity.get("name") or "") == name)


def _layout_considered_unlocked_items(unlocks: dict[str, Any]) -> list[str]:
    considered: list[str] = []
    long_handed = unlocks.get("long_handed_inserter") if isinstance(unlocks.get("long_handed_inserter"), dict) else {}
    if bool(long_handed.get("available")):
        considered.append("long-handed-inserter")
    for group_name in ("modules", "machines", "furnaces", "mining_drills", "beacons"):
        group = unlocks.get(group_name) if isinstance(unlocks.get(group_name), dict) else {}
        for name, state in group.items():
            if isinstance(state, dict) and bool(state.get("available")):
                considered.append(str(name))
    return sorted(dict.fromkeys(considered))


def _layout_unused_unlocked_items(considered: list[str], used: list[str]) -> list[str]:
    used_set = set(used)
    return [item for item in considered if item not in used_set]


def _layout_used_unlocked_item_state(unlocks: dict[str, Any], used: list[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in used:
        state: dict[str, Any] = {}
        if item == "long-handed-inserter":
            raw = unlocks.get("long_handed_inserter")
            if isinstance(raw, dict):
                state = raw
        else:
            for group_name in ("modules", "machines", "furnaces", "mining_drills", "beacons"):
                group = unlocks.get(group_name) if isinstance(unlocks.get(group_name), dict) else {}
                raw = group.get(item)
                if isinstance(raw, dict):
                    state = raw
                    break
        if state:
            output[str(item)] = {
                key: state.get(key)
                for key in ("available", "researched", "recipe_unlocked", "stock", "automated", "built", "layout_impact")
                if key in state
            }
    return output


def _layout_capability_available(unlocks: dict[str, Any], group_name: str, name: str) -> bool:
    group = unlocks.get(group_name) if isinstance(unlocks.get(group_name), dict) else {}
    state = group.get(name) if isinstance(group.get(name), dict) else {}
    return bool(state.get("available"))


def _preferred_assembler_for_layout(unlocks: dict[str, Any]) -> tuple[str, float]:
    if _layout_capability_available(unlocks, "machines", "assembling-machine-3"):
        return "assembling-machine-3", 1.25
    if _layout_capability_available(unlocks, "machines", "assembling-machine-2"):
        return "assembling-machine-2", 0.75
    return "assembling-machine-1", 0.5


def _preferred_furnace_for_layout(unlocks: dict[str, Any]) -> tuple[str, float, str]:
    if _layout_capability_available(unlocks, "furnaces", "electric-furnace"):
        return "electric-furnace", 37.5, "electric furnace column with ore input and plate output lanes"
    if _layout_capability_available(unlocks, "furnaces", "steel-furnace"):
        return "steel-furnace", 37.5, "steel furnace column with shared ore/fuel input and plate output lanes"
    return "stone-furnace", 18.75, "stone furnace column with shared ore/fuel input and plate output lanes"


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
                row["direction"] = _direction_or_default(entity.get("direction"), 0)
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


def _layout_build_item_supply(
    observation: dict[str, Any],
    blueprint_entities: list[dict[str, Any]],
    *,
    used_unlocked_items: list[str],
) -> dict[str, Any]:
    check = _site_gate_build_item_check(observation, blueprint_entities)
    required = check.get("required") if isinstance(check.get("required"), dict) else {}
    available = check.get("available") if isinstance(check.get("available"), dict) else {}
    missing = check.get("missing") if isinstance(check.get("missing"), dict) else {}
    used = [str(item) for item in used_unlocked_items if item]
    unlocked_supply = {}
    for item in used:
        need = int(required.get(item) or 0)
        have = int(available.get(item) or 0)
        unlocked_supply[item] = {
            "required": need,
            "available": have,
            "missing": max(0, need - have),
            "sufficient": have >= need,
        }
    return {
        "status": check.get("status"),
        "required": dict(sorted((str(key), int(value)) for key, value in required.items())),
        "available": dict(sorted((str(key), int(value)) for key, value in available.items())),
        "missing": dict(sorted((str(key), int(value)) for key, value in missing.items())),
        "used_unlocked_items": used,
        "used_unlocked_item_supply": unlocked_supply,
        "summary": check.get("summary"),
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
    *,
    layout_unlocks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unlocks = layout_unlocks if isinstance(layout_unlocks, dict) else {}
    long_handed_state = unlocks.get("long_handed_inserter") if isinstance(unlocks.get("long_handed_inserter"), dict) else {}
    use_long_handed = bool(long_handed_state.get("available"))
    used_unlocked_items = ["long-handed-inserter"] if use_long_handed else []
    considered_unlocked_items = _layout_considered_unlocked_items(unlocks)
    assembler_tier, assembler_speed = _preferred_assembler_for_layout(unlocks)
    if assembler_tier != "assembling-machine-1":
        used_unlocked_items.append(assembler_tier)
    current_cable = int(recipe_counts.get("copper-cable", 0))
    current_circuit = int(recipe_counts.get("electronic-circuit", 0))
    current_circuit = max(current_circuit, 1)
    groups = max(1, (current_circuit + 1) // 2)
    proposed_cable = groups * 3
    proposed_circuit = groups * 2
    before_rate = _green_circuit_rate_per_minute(current_cable, current_circuit)
    after_rate = _green_circuit_rate_per_minute(proposed_cable, proposed_circuit, assembler_speed=assembler_speed)
    score = 70.0 + min(20.0, max(0.0, after_rate - before_rate) / 12.0)
    if current_cable == 0:
        score += 5.0
    if use_long_handed:
        score += 6.0
    if assembler_tier != "assembling-machine-1":
        score += 4.0
    after_entities = _green_circuit_blueprint_entities(groups, use_long_handed=use_long_handed, assembler_name=assembler_tier)
    build_item_supply = _layout_build_item_supply(
        observation,
        after_entities,
        used_unlocked_items=used_unlocked_items,
    )
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
    candidate_id = (
        "green-circuit-long-handed-3-cable-2-circuit-cell"
        if use_long_handed
        else "green-circuit-3-cable-2-circuit-cell"
    )
    inserter_tier = "long-handed-inserter" if use_long_handed else "inserter"
    return {
        "candidate_id": candidate_id,
        "simulation_only": True,
        "not_applied": True,
        "source": "rate-calculator-style static recipe throughput plus unlocked layout capability ranking",
        "target_pattern": (
            "3 copper-cable assemblers feeding 2 electronic-circuit assemblers with long-handed inserter input lanes"
            if use_long_handed
            else "3 copper-cable assemblers belt-feeding 2 electronic-circuit assemblers"
        ),
        "requires_build_command": True,
        "requires_site_prebuild_gate": True,
        "build_ready": False,
        "build_ready_blockers": _layout_candidate_build_ready_blockers(
            sandbox_validation=None,
            site_gate=site_gate,
            placement_search=placement_search,
        ),
        "blueprint": _blueprint_export(
            candidate_id,
            after_entities,
            (
                "Simulation-only 3:2 green circuit cell using long-handed inserters for two-lane input reach. "
                "Validate exact input belts, power, and collision before applying."
                if use_long_handed
                else "Simulation-only 3:2 green circuit cell. Validate exact input belts, power, and collision before applying."
            ),
        ),
        "validation": validation,
        "layout_unlocks_considered": unlocks,
        "considered_unlocked_items": considered_unlocked_items,
        "uses_unlocked_items": used_unlocked_items,
        "unused_unlocked_items": _layout_unused_unlocked_items(considered_unlocked_items, used_unlocked_items),
        "used_unlocked_item_state": _layout_used_unlocked_item_state(unlocks, used_unlocked_items),
        "build_item_supply": build_item_supply,
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
                "inserter_tier": inserter_tier,
                "assembler_tier": assembler_tier,
                "input_lane_pattern": "long_reach_two_lane" if use_long_handed else "standard_adjacent_lanes",
            },
            "delta": {
                "electronic_circuit_per_minute": round(after_rate - before_rate, 1),
                "ratio_error_reduced": True,
                "unlock_aware_rerank": use_long_handed,
                "unlock_aware_considered": bool(considered_unlocked_items),
                "higher_tier_machine_used": assembler_tier != "assembling-machine-1",
                "belt_doglegs_reduced": use_long_handed,
                "static_operability": validation["status"],
            },
            "score": round(min(score, 95.0), 1),
        },
        "notes": [
            "Simulation assumes assembling-machine-1 speed and recipe max rates.",
            "Real build still needs sandbox pass, site placement, power, build-item, and input-source validation.",
        ],
    }


def _green_circuit_rate_per_minute(cable_assemblers: int, circuit_assemblers: int, *, assembler_speed: float = 0.5) -> float:
    cable_recipe = RECIPES["copper-cable"]
    circuit_recipe = RECIPES["electronic-circuit"]
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


def _green_circuit_blueprint_entities(
    groups: int,
    *,
    use_long_handed: bool = False,
    assembler_name: str = "assembling-machine-1",
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    group_count = max(1, min(4, groups))
    for group in range(group_count):
        y = group * 14
        if use_long_handed:
            for offset in range(-2, 11):
                _add_entity(entities, "transport-belt", -5, y + offset, direction=SOUTH)
                _add_entity(entities, "transport-belt", 2, y + offset, direction=SOUTH)
                _add_entity(entities, "transport-belt", 10, y + offset, direction=SOUTH)
            for offset in (0, 4, 8):
                _add_entity(entities, assembler_name, 0, y + offset, recipe="copper-cable")
                _add_entity(entities, "long-handed-inserter", -3, y + offset, direction=WEST)
                _add_entity(entities, "inserter", 1, y + offset, direction=WEST)
            for offset in (1, 7):
                _add_entity(entities, assembler_name, 6, y + offset, recipe="electronic-circuit")
                _add_entity(entities, "long-handed-inserter", 4, y + offset, direction=WEST)
                _add_entity(entities, "long-handed-inserter", 8, y + offset, direction=EAST)
                _add_entity(entities, "inserter", 6, y + offset + 2, direction=NORTH)
                _add_entity(entities, "iron-chest", 6, y + offset + 3)
            _add_entity(entities, "small-electric-pole", 1, y + 4)
            _add_entity(entities, "small-electric-pole", 7, y + 4)
        else:
            for offset in range(-2, 11):
                _add_entity(entities, "transport-belt", -3, y + offset, direction=SOUTH)
                _add_entity(entities, "transport-belt", 3, y + offset, direction=SOUTH)
                _add_entity(entities, "transport-belt", 9, y + offset, direction=SOUTH)
            for offset in (0, 4, 8):
                _add_entity(entities, assembler_name, 0, y + offset, recipe="copper-cable")
                _add_entity(entities, "inserter", -2, y + offset, direction=WEST)
                _add_entity(entities, "inserter", 2, y + offset, direction=WEST)
            for offset in (1, 7):
                _add_entity(entities, assembler_name, 6, y + offset, recipe="electronic-circuit")
                _add_entity(entities, "inserter", 4, y + offset, direction=WEST)
                _add_entity(entities, "inserter", 8, y + offset, direction=EAST)
                _add_entity(entities, "inserter", 6, y + offset + 2, direction=NORTH)
                _add_entity(entities, "iron-chest", 6, y + offset + 3)
            _add_entity(entities, "small-electric-pole", 2, y + 4)
            _add_entity(entities, "small-electric-pole", 7, y + 4)
    return entities


def _unlock_retooling_candidate(
    opportunity: dict[str, Any],
    sites: list[dict[str, Any]],
    *,
    layout_unlocks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unlocks = layout_unlocks if isinstance(layout_unlocks, dict) else {}
    params = opportunity.get("parameters") if isinstance(opportunity.get("parameters"), dict) else {}
    retool_tools = [
        str(item)
        for item in params.get("retool_tools", [])
        if isinstance(item, str) and item
    ]
    affected_site_ids = {
        str(item)
        for item in params.get("affected_site_ids", [])
        if isinstance(item, str) and item
    }
    affected_sites = [
        site
        for site in sites
        if not affected_site_ids or str(site.get("site_id") or "") in affected_site_ids
    ]
    positions = [site.get("position") for site in affected_sites if isinstance(site.get("position"), dict)]
    footprint = _layout_footprint(positions)
    before_area = float(footprint.get("area") or max(24.0, len(affected_sites) * 12.0))
    area_factor = 1.0
    throughput_factor = 1.0
    power_factor = 1.0
    if "long-handed-inserter" in retool_tools:
        area_factor *= 0.88
    if "assembling-machine-2" in retool_tools:
        throughput_factor *= 1.5
        power_factor *= 3.0
    if "assembling-machine-3" in retool_tools:
        throughput_factor *= 2.5
        power_factor *= 6.25
    if "steel-furnace" in retool_tools:
        throughput_factor *= 2.0
    if "electric-furnace" in retool_tools:
        throughput_factor *= 2.0
        power_factor *= 3.0
    if "speed-module" in retool_tools:
        throughput_factor *= 1.2
        power_factor *= 1.5
    if "productivity-module" in retool_tools:
        throughput_factor *= 1.04
        power_factor *= 1.4
    if "efficiency-module" in retool_tools:
        power_factor *= 0.7
    if "beacon" in retool_tools:
        area_factor *= 1.25
        throughput_factor *= 1.5
        power_factor *= 2.0
    after_area = max(16.0, before_area * area_factor)
    considered_unlocked_items = _layout_considered_unlocked_items(unlocks)
    candidate_suffix = "-".join(item.replace("_", "-") for item in retool_tools[:4]) or "unlocked-tools"
    score = float(opportunity.get("severity") or 75)
    if throughput_factor > 1.0:
        score += min(8.0, (throughput_factor - 1.0) * 4.0)
    if after_area < before_area:
        score += min(6.0, (before_area - after_area) / 20.0)
    return {
        "candidate_id": f"unlock-aware-site-rerank-{candidate_suffix}",
        "simulation_only": True,
        "not_applied": True,
        "source": "unlock-aware site graph re-evaluation; benchmark before rebuild",
        "target_pattern": (
            "rerank affected sites with the currently unlocked item set before extending the old footprint"
        ),
        "requires_build_command": True,
        "layout_unlocks_considered": unlocks,
        "considered_unlocked_items": considered_unlocked_items,
        "uses_unlocked_items": retool_tools,
        "unused_unlocked_items": _layout_unused_unlocked_items(considered_unlocked_items, retool_tools),
        "used_unlocked_item_state": _layout_used_unlocked_item_state(unlocks, retool_tools),
        "simulation": {
            "before": {
                "affected_sites": len(affected_sites),
                "affected_site_kinds": params.get("affected_site_kinds", []),
                "footprint_area": round(before_area, 1),
                "obsolete_patterns": params.get("obsolete_patterns", []),
            },
            "after": {
                "retool_tools": retool_tools,
                "estimated_footprint_area": round(after_area, 1),
                "throughput_factor_estimate": round(throughput_factor, 2),
                "power_factor_estimate": round(power_factor, 2),
            },
            "delta": {
                "footprint_area": round(after_area - before_area, 1),
                "unlock_aware_rerank": True,
                "unlock_aware_considered": bool(considered_unlocked_items),
                "requires_power_pollution_recheck": power_factor != 1.0,
                "requires_bottleneck_recheck": throughput_factor != 1.0 or "long-handed-inserter" in retool_tools,
            },
            "score": round(min(score, 96.0), 1),
        },
    }


def _smelting_standardization_candidate(
    item: str,
    rows: list[dict[str, Any]],
    observation: dict[str, Any],
    *,
    layout_unlocks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unlocks = layout_unlocks if isinstance(layout_unlocks, dict) else {}
    furnace_tier, furnace_rate, target_pattern = _preferred_furnace_for_layout(unlocks)
    used_unlocked_items = [furnace_tier] if furnace_tier != "stone-furnace" else []
    considered_unlocked_items = _layout_considered_unlocked_items(unlocks)
    positions = [row.get("position") for row in rows if isinstance(row.get("position"), dict)]
    footprint = _layout_footprint(positions)
    furnace_count = sum(_machine_count(row, "stone-furnace") + _machine_count(row, "steel-furnace") for row in rows)
    if furnace_count <= 0:
        furnace_count = len(rows)
    before_rate = furnace_count * 18.75
    after_rate = furnace_count * furnace_rate
    target_columns = max(1, min(4, (furnace_count + 11) // 12))
    after_footprint_area = max(16.0, float(footprint.get("area") or 16.0) * 0.72)
    area_reduction = max(0.0, float(footprint.get("area") or 0.0) - after_footprint_area)
    score = 58.0 + min(22.0, len(rows) * 4.0) + min(12.0, area_reduction / 20.0)
    if furnace_tier != "stone-furnace":
        score += 5.0
    after_entities = _smelting_column_blueprint_entities(item, furnace_count, target_columns, furnace_name=furnace_tier)
    return {
        "candidate_id": f"{item}-{furnace_tier}-parallel-smelting-columns",
        "simulation_only": True,
        "not_applied": True,
        "source": "blueprint-pattern heuristic plus unlocked furnace tier ranking",
        "target_pattern": target_pattern,
        "requires_build_command": True,
        "blueprint": _blueprint_export(
            f"{item}-{furnace_tier}-parallel-smelting-columns",
            after_entities,
            "Simulation-only smelting column block. Place near validated ore/fuel inputs; miners and long logistics are not included.",
        ),
        "layout_unlocks_considered": unlocks,
        "considered_unlocked_items": considered_unlocked_items,
        "uses_unlocked_items": used_unlocked_items,
        "unused_unlocked_items": _layout_unused_unlocked_items(considered_unlocked_items, used_unlocked_items),
        "used_unlocked_item_state": _layout_used_unlocked_item_state(unlocks, used_unlocked_items),
        "build_item_supply": _layout_build_item_supply(
            observation,
            after_entities,
            used_unlocked_items=used_unlocked_items,
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
                "furnace_tier": furnace_tier,
                "plate_per_minute": round(after_rate, 1),
                "estimated_footprint_area": round(after_footprint_area, 1),
            },
            "delta": {
                "plate_per_minute": round(after_rate - before_rate, 1),
                "footprint_area": round(-area_reduction, 1),
                "unlock_aware_considered": bool(considered_unlocked_items),
                "higher_tier_machine_used": furnace_tier != "stone-furnace",
                "expandability": "higher; repeatable columns can be copied without re-solving local layout",
            },
            "score": round(min(score, 92.0), 1),
        },
    }


def _smelting_column_blueprint_entities(
    item: str,
    furnace_count: int,
    columns: int,
    *,
    furnace_name: str = "stone-furnace",
) -> list[dict[str, Any]]:
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
        _add_entity(entities, furnace_name, x + 4, y)
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


def _mall_compaction_candidate(
    mall_sites: list[dict[str, Any]],
    observation: dict[str, Any],
    *,
    layout_unlocks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unlocks = layout_unlocks if isinstance(layout_unlocks, dict) else {}
    long_handed_state = unlocks.get("long_handed_inserter") if isinstance(unlocks.get("long_handed_inserter"), dict) else {}
    use_long_handed = bool(long_handed_state.get("available"))
    used_unlocked_items = ["long-handed-inserter"] if use_long_handed else []
    considered_unlocked_items = _layout_considered_unlocked_items(unlocks)
    positions = [site.get("position") for site in mall_sites if isinstance(site.get("position"), dict)]
    footprint = _layout_footprint(positions)
    before_area = float(footprint.get("area") or 0.0)
    after_factor = 0.58 if use_long_handed else 0.65
    after_area = max(24.0, before_area * after_factor)
    candidate_id = "starter-mall-row-long-handed-inputs" if use_long_handed else "starter-mall-row-compaction"
    score = 60.0 + min(22.0, max(0.0, before_area - after_area) / 16.0)
    if use_long_handed:
        score += 7.0
    after_entities = _starter_mall_row_blueprint_entities(len(mall_sites), use_long_handed=use_long_handed)
    return {
        "candidate_id": candidate_id,
        "simulation_only": True,
        "not_applied": True,
        "source": "blueprint-pattern heuristic plus unlocked layout capability ranking",
        "target_pattern": (
            "starter mall row with long-handed reach across a second shared input lane and chest outputs"
            if use_long_handed
            else "starter mall row with shared iron/gear/circuit inputs and chest outputs"
        ),
        "requires_build_command": True,
        "blueprint": _blueprint_export(
            candidate_id,
            after_entities,
            (
                "Simulation-only starter mall row using long-handed inserters for a second input lane. "
                "Validate input belts, recipes, power, and chest positions before applying."
                if use_long_handed
                else "Simulation-only starter mall row. Validate input belts, recipes, power, and chest positions before applying."
            ),
        ),
        "layout_unlocks_considered": unlocks,
        "considered_unlocked_items": considered_unlocked_items,
        "uses_unlocked_items": used_unlocked_items,
        "unused_unlocked_items": _layout_unused_unlocked_items(considered_unlocked_items, used_unlocked_items),
        "used_unlocked_item_state": _layout_used_unlocked_item_state(unlocks, used_unlocked_items),
        "build_item_supply": _layout_build_item_supply(
            observation,
            after_entities,
            used_unlocked_items=used_unlocked_items,
        ),
        "simulation": {
            "before": {"cells": len(mall_sites), "footprint_area": round(before_area, 1)},
            "after": {
                "cells": len(mall_sites),
                "estimated_footprint_area": round(after_area, 1),
                "inserter_tier": "long-handed-inserter" if use_long_handed else "inserter",
                "shared_input_lanes": 2 if use_long_handed else 1,
            },
            "delta": {
                "footprint_area": round(after_area - before_area, 1),
                "shared_input_lanes": True,
                "unlock_aware_rerank": use_long_handed,
                "unlock_aware_considered": bool(considered_unlocked_items),
                "belt_doglegs_reduced": use_long_handed,
            },
            "score": round(min(score, 95.0), 1),
        },
    }


def _starter_mall_row_blueprint_entities(cell_count: int, *, use_long_handed: bool = False) -> list[dict[str, Any]]:
    recipes = [
        "transport-belt",
        "inserter",
        "burner-inserter",
        "stone-furnace",
        "burner-mining-drill",
        "assembling-machine-1",
        "small-electric-pole",
    ]
    if use_long_handed:
        recipes.insert(2, "long-handed-inserter")
    entities: list[dict[str, Any]] = []
    count = max(1, min(len(recipes), cell_count or len(recipes)))
    for index, recipe in enumerate(recipes[:count]):
        x = index * 4
        _add_entity(entities, "assembling-machine-1", x, 0, recipe=recipe)
        if use_long_handed and index % 2 == 1:
            _add_entity(entities, "transport-belt", x, -4, direction=EAST)
            _add_entity(entities, "long-handed-inserter", x, -2, direction=NORTH)
        else:
            _add_entity(entities, "transport-belt", x, -3, direction=EAST)
            _add_entity(entities, "inserter", x, -2, direction=NORTH)
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
        if isinstance(entity, dict) and str(entity.get("name") or "") in BLUEPRINT_INSERTER_NAMES
    ]


def _inserter_endpoints(entity: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]] | None:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if position is None:
        return None
    direction = _direction_or_default(entity.get("direction"), 0)
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
    reach = 2.0 if str(entity.get("name") or "") == "long-handed-inserter" else 1.0
    pickup = {"x": x + dx * reach, "y": y + dy * reach}
    drop = {"x": x - dx * reach, "y": y - dy * reach}
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
    return _point_inside_machine_footprint(point, center)


def _point_inside_machine_footprint(point: dict[str, float], center: dict[str, float]) -> bool:
    return (
        abs(float(point.get("x") or 0.0) - float(center.get("x") or 0.0)) <= 1.5
        and abs(float(point.get("y") or 0.0) - float(center.get("y") or 0.0)) <= 1.5
    )


def _planned_machine_over_protected_resource(observation: dict[str, Any], position: dict[str, float]) -> bool:
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("position"), dict):
            continue
        if str(resource.get("name") or "") not in PROTECTED_RESOURCE_NAMES:
            continue
        if _point_inside_machine_footprint(_position(resource), position):
            return True
    return False


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


def _direct_iron_drill_stranded(observation: dict[str, Any]) -> bool:
    """True if a burner mining drill sits stranded (status ``no_minable_resources``) next to an
    iron-ore patch. Iron stock can read as 'done' while production is actually DEAD and the plates are
    stuck in a far furnace (observed live 2026-06-19: drill 2 tiles off the ore at 95 tiles, furnaces
    idle, assemblers starved while total iron looked >= target). Recover the drill before reporting
    done so the factory does not silently starve once the buffer drains. Scans entities/resources
    directly (deterministic) -- the cell-finder is cache/order-dependent and missed it intermittently."""
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    stranded = [
        e for e in entities
        if isinstance(e, dict) and str(e.get("name") or "") == "burner-mining-drill"
        and isinstance(e.get("position"), dict)
        and _entity_status_is(e, "no_minable_resources", 21)
    ]
    if not stranded:
        return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    iron_tiles = [
        r for r in resources
        if isinstance(r, dict) and r.get("name") == "iron-ore" and isinstance(r.get("position"), dict)
    ]
    if not iron_tiles:
        return False
    for drill in stranded:
        dp = drill["position"]
        if any(
            abs(float(dp.get("x", 0)) - float(r["position"].get("x", 0))) <= 3.0
            and abs(float(dp.get("y", 0)) - float(r["position"].get("y", 0))) <= 3.0
            for r in iron_tiles
        ):
            return True
    return False


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
        if iron_total >= target and not _direct_iron_drill_stranded(observation):
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
        science_assemblers = [
            entity
            for entity in observation.get("entities") or []
            if isinstance(entity, dict)
            and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
            and str(entity.get("recipe") or "") == "automation-science-pack"
        ]
        science_assembler = _nearest_to(science_assemblers, player_position(observation)) if science_assemblers else None
        science_reference = _position(science_assembler) if isinstance(science_assembler, dict) else None

        if _automation_researched(observation):
            return BuildItemMallSkill("automation-science-pack", self.target_count).next_action(
                observation,
                reference_position=science_reference,
            )

        if craftable_count(observation, "automation-science-pack") > 0:
            return PlannerDecision(
                {
                    "type": "craft",
                    "recipe": "automation-science-pack",
                    "count": min(science_needed, craftable_count(observation, "automation-science-pack")),
                },
                "craft automation science packs",
            )

        if gear_total < science_needed:
            decision = _ensure_iron_gears_without_post_automation_handcraft(
                observation,
                science_needed,
                pre_automation_reason="craft iron gear wheels for automation science",
                reference_position=science_reference,
            )
            if decision is not None:
                return decision

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

        if inventory_only:
            output_decision = _take_existing_plate_output_decision(
                observation,
                resource_name="copper-ore",
                product_name="copper-plate",
            )
            if output_decision is not None:
                return output_decision

        if _find_direct_smelting_cell(observation, "copper-ore") is not None:
            direct_decision = _direct_plate_smelting_decision(
                observation,
                target_count=min(target, DIRECT_SMELTING_CELL_TARGET_PLATES),
                resource_name="copper-ore",
                product_name="copper-plate",
                support_skill=self.support_skill,
                inventory_only=True,
                allow_support_plate=True,
            )
            if direct_decision.done:
                if copper_total >= target:
                    return direct_decision
            else:
                return direct_decision

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

        if isinstance(drill, dict) and _entity_status_is(drill, "no_minable_resources", 21):
            position = _position(drill)
            if distance(player, position) > 8:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                    "move near invalid starter stone mining drill before relocating it",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": drill.get("unit_number"),
                    "name": "burner-mining-drill",
                    "position": position,
                },
                "recover invalid starter stone mining drill with no minable resources",
            )

        if _entity_burner_fuel_count(drill) < 1:
            return _fuel_burner_line_entity(
                observation,
                player,
                drill,
                entity_name="burner-mining-drill",
                threshold=1,
                insert_count=1,
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
            if inventory_count(observation, "iron-gear-wheel") < 3:
                decision = _ensure_iron_gears_without_post_automation_handcraft(
                    observation,
                    3,
                    pre_automation_reason="craft gears for stone supply drill",
                    allow_assembler_output_gears=True,
                    infrastructure_reason="stone supply drill infrastructure",
                )
                if decision is not None:
                    return decision
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

        decision = self._line_construction_decision(observation, player, layout)
        if decision is not None:
            return decision

        for entity_name, layout_key, item, threshold, count in _smelting_line_fuel_requirements(layout, reserve=False):
            entity = layout.get(layout_key)
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

        incomplete_layout = _find_incomplete_belt_smelting_line(observation, self.resource_name)
        if incomplete_layout is not None:
            decision = self._line_construction_decision(observation, player, incomplete_layout)
            if decision is not None:
                return decision

        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            "wait for belt smelting line to move ore and smelt plates",
        )

    def _line_construction_decision(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        layout: dict[str, Any],
    ) -> PlannerDecision | None:
        need = _line_missing_item(observation, layout)
        if need:
            decision = self._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        for name, key, direction_key in [
            ("transport-belt", "belt1_position", "belt_direction"),
            ("transport-belt", "belt2_position", "belt_direction"),
            ("inserter", "inserter_position", "inserter_direction"),
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
        return None

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
            if inventory_count(observation, "iron-gear-wheel") < 3:
                decision = _ensure_iron_gears_without_post_automation_handcraft(
                    observation,
                    3,
                    pre_automation_reason="craft gears for line drill",
                    allow_assembler_output_gears=True,
                    infrastructure_reason="belt smelting drill infrastructure",
                )
                if decision is not None:
                    return decision
            decision = self.support_skill.next_action(observation, target_count=20, inventory_only=True)
            if not decision.done:
                return decision
            return PlannerDecision(None, "cannot obtain burner-mining-drill for belt smelting line yet")

        if item in {"transport-belt", "inserter", "burner-inserter"}:
            if inventory_count(observation, "iron-gear-wheel") < 1:
                decision = _ensure_iron_gears_without_post_automation_handcraft(
                    observation,
                    1,
                    pre_automation_reason=f"craft gear for {item}",
                    allow_assembler_output_gears=True,
                    infrastructure_reason=f"{item} infrastructure",
                )
                if decision is not None:
                    return decision
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
            decision = self.support_skill.next_action(observation, target_count=20, inventory_only=True)
            if not decision.done:
                return decision
            return PlannerDecision(None, f"cannot obtain {item} for belt smelting line yet")

        return None

    def _line_has_started(self, furnace: dict[str, Any]) -> bool:
        return entity_item_count(furnace, self.resource_name) > 0 or entity_item_count(furnace, self.product_name) > 0


class CoalSupplySkill:
    """Build a minimal burner coal supply site for early fuel logistics."""

    BUILD_STANDOFF_DISTANCE = 6.5

    def __init__(self, target_count: int = 16) -> None:
        self.target_count = target_count
        self.support_skill = BeltSmeltingLineSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        layout = _find_coal_supply_layout(observation) or _select_coal_supply_layout(observation)
        if layout is None:
            return PlannerDecision(None, "cannot find open coal patch for coal supply site")
        use_output_chest = _coal_supply_should_use_output_chest(observation, layout)

        need = _coal_supply_missing_item(observation, layout, use_output_chest=use_output_chest)
        if need:
            if need == "transport-belt":
                misplaced_belt = _find_misplaced_coal_supply_output_belt(observation, layout)
                if misplaced_belt is not None:
                    position = _position(misplaced_belt)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of misplaced coal supply output belt",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": misplaced_belt.get("unit_number"),
                            "name": "transport-belt",
                            "position": position,
                        },
                        "recover misplaced coal supply output belt before rebuilding the drill output",
                    )
                belt_chest = _transport_belt_output_chest(observation)
                if isinstance(belt_chest, dict) and entity_item_count(belt_chest, "transport-belt") > 0:
                    position = _position(belt_chest)
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            "move near belt mall output chest to collect a transport belt for coal supply construction",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "transport-belt",
                            "count": 1,
                            "unit_number": belt_chest.get("unit_number"),
                            "name": belt_chest.get("name") or "wooden-chest",
                            "position": position,
                        },
                        "take transport belt from belt mall output chest for coal supply construction",
                    )
                belt_assembler = _transport_belt_output_assembler(observation)
                if isinstance(belt_assembler, dict) and entity_item_count(belt_assembler, "transport-belt") > 0:
                    position = _position(belt_assembler)
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            "move near belt mall output to collect a transport belt for coal supply construction",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "transport-belt",
                            "count": 1,
                            "unit_number": belt_assembler.get("unit_number"),
                            "name": belt_assembler.get("name") or "assembling-machine-1",
                            "position": position,
                        },
                        "take transport belt from belt mall output for coal supply construction",
                    )
            if need == "burner-mining-drill":
                reusable = _find_relocatable_burner_drill_for_coal_supply(observation, layout["drill_position"])
                if reusable is not None:
                    position = _position(reusable)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of idle burner mining drill for coal supply relocation",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": "burner-mining-drill",
                            "position": position,
                        },
                        "relocate idle burner mining drill to coal supply instead of hand-carrying iron plates for a new drill",
                    )
            if need in {"wooden-chest", "iron-chest"}:
                decision = self._ensure_output_chest_item(observation, player, need)
            else:
                decision = self.support_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        drill = layout.get("drill")
        if isinstance(drill, dict) and _entity_status_is(drill, "no_minable_resources", 21):
            position = _position(drill)
            if distance(player, position) > 8:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                    "move near invalid coal supply mining drill before relocating it",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": drill.get("unit_number"),
                    "name": "burner-mining-drill",
                    "position": position,
                },
                "recover invalid coal supply mining drill with no minable resources",
            )
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
                        "unit_number": blocker.get("unit_number"),
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

        if use_output_chest:
            chest = layout.get("output_chest")
            if chest is None:
                chest_name = _available_coal_output_chest_name(observation)
                if chest_name is None:
                    return PlannerDecision(None, "missing output chest for starter coal supply")
                position = layout["output_position"]
                blocker = _blocking_obstacle_near(observation, position)
                if blocker is not None:
                    blocker_position = _position(blocker)
                    if distance(player, blocker_position) > 8:
                        return PlannerDecision(
                            {"type": "move_to", "position": blocker_position},
                            f"move near blocking {blocker.get('name')} before placing coal output chest",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "name": blocker.get("name"),
                            "position": blocker_position,
                            "count": 1,
                        },
                        f"clear blocking {blocker.get('name')} before placing coal output chest",
                    )
                if distance(player, position) > self.BUILD_STANDOFF_DISTANCE or distance(player, position) < 2.0:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                        "move near planned coal output chest",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": chest_name,
                        "position": position,
                    },
                    "place output chest for starter coal supply",
                )

        belt = layout.get("output_belt")
        if not use_output_chest and belt is None:
            position = layout["output_position"]
            chest = layout.get("output_chest")
            if isinstance(chest, dict):
                chest_position = _position(chest)
                if distance(player, chest_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": chest_position},
                        "move near coal output chest before replacing it with an output belt",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": chest.get("unit_number"),
                        "name": chest.get("name"),
                        "position": chest_position,
                    },
                    "recover coal output chest before placing coal output belt",
                )
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
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing coal output belt",
                )
            if distance(player, position) > self.BUILD_STANDOFF_DISTANCE or distance(player, position) < 2.0:
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
        if not use_output_chest and isinstance(belt, dict):
            expected_direction = int(layout["belt_direction"])
            if _direction_or_default(belt.get("direction"), expected_direction) != expected_direction:
                position = _position(belt)
                if distance(player, position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                        "move within reach of misoriented coal supply output belt",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": belt.get("unit_number"),
                        "name": "transport-belt",
                        "position": position,
                    },
                    "remove misoriented coal supply output belt before fueling the coal drill",
                )

        drill_fuel = _entity_burner_fuel_count(drill)
        drill_fuel_item = _entity_existing_burner_fuel_item(drill)
        matching_fuel_available = (
            inventory_count(observation, drill_fuel_item) > 0
            if drill_fuel_item is not None
            else _select_inventory_burner_fuel(observation)[1] > 0
        )
        if drill_fuel < DIRECT_SMELTING_FUEL_RESERVE and (drill_fuel <= 0 or matching_fuel_available):
            return _fuel_burner_line_entity(
                observation,
                player,
                drill,
                entity_name="burner-mining-drill",
                threshold=DIRECT_SMELTING_FUEL_RESERVE,
                insert_count=STARTER_FUEL_BATCH_COUNT,
                context="coal supply site",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="coal supply site is too far from available hand fuel; build closer coal logistics first",
                prefer_coal_supply=False,
            )

        expansion_decision = self._parallel_coal_supply_decision(observation, player)
        if expansion_decision is not None:
            return expansion_decision

        chest = layout.get("output_chest")
        if use_output_chest and chest is not None:
            chest_coal = entity_item_count(chest, "coal")
            if chest_coal > 0 and inventory_count(observation, "coal") < self.target_count:
                chest_position = _position(chest)
                if distance(player, chest_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": chest_position},
                        "move near starter coal output chest",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "coal",
                        "count": min(STARTER_FUEL_BATCH_COUNT, chest_coal),
                        "unit_number": chest.get("unit_number"),
                        "name": chest.get("name") or "wooden-chest",
                        "position": chest_position,
                    },
                    "take coal from starter coal supply chest",
                )
            if chest_coal <= 0:
                return PlannerDecision(
                    {"type": "wait", "ticks": 240},
                    "wait for starter coal drill to fill the output chest",
                )
            return PlannerDecision(
                None,
                "starter coal supply site is active with fueled burner mining drill and output chest",
                done=True,
            )

        belt_coal = entity_item_count(belt, "coal")
        if belt_coal > 0 and inventory_count(observation, "coal") < self.target_count:
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
                    "count": min(STARTER_FUEL_BATCH_COUNT, belt_coal),
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

    def _ensure_output_chest_item(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
    ) -> PlannerDecision | None:
        if item not in {"wooden-chest", "iron-chest"}:
            return None
        if craftable_count(observation, item) > 0:
            return PlannerDecision({"type": "craft", "recipe": item, "count": 1}, f"craft {item} for coal output")
        if item == "wooden-chest" and inventory_count(observation, "wood") < 2:
            tree = _nearest_tree(observation)
            if tree is None:
                if craftable_count(observation, "iron-chest") > 0:
                    return PlannerDecision({"type": "craft", "recipe": "iron-chest", "count": 1}, "craft iron chest for coal output")
                return IronPlateSkill(target_count=8).next_action(observation, target_count=8, inventory_only=True)
            tree_position = _position(tree)
            if distance(player, tree_position) > 8:
                return PlannerDecision({"type": "move_to", "position": tree_position}, "move near tree for coal output chest")
            return PlannerDecision(
                {
                    "type": "mine",
                    "name": tree.get("name"),
                    "position": tree_position,
                    "count": 1,
                },
                "mine tree for coal output chest",
            )
        if item == "iron-chest":
            return IronPlateSkill(target_count=8).next_action(observation, target_count=8, inventory_only=True)
        return None

    def _parallel_coal_supply_decision(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
    ) -> PlannerDecision | None:
        if _coal_supply_burner_drill_count(observation) >= _coal_supply_desired_parallel_drills(observation):
            return None
        layout = _select_coal_supply_expansion_layout(observation)
        if layout is None:
            return None
        use_output_chest = _coal_supply_should_use_output_chest(observation, layout)
        need = _coal_supply_missing_item(observation, layout, use_output_chest=use_output_chest)
        if need:
            if need == "burner-mining-drill":
                if craftable_count(observation, "burner-mining-drill") > 0:
                    return PlannerDecision(
                        {"type": "craft", "recipe": "burner-mining-drill", "count": 1},
                        "craft burner mining drill for parallel starter coal supply",
                    )
                reusable = _find_relocatable_burner_drill_for_coal_supply(observation, layout["drill_position"])
                if reusable is not None:
                    position = _position(reusable)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of idle temporary burner mining drill for parallel coal supply",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": "burner-mining-drill",
                            "position": position,
                        },
                        "relocate idle temporary burner mining drill to parallel coal supply",
                    )
            if need in {"wooden-chest", "iron-chest"}:
                decision = self._ensure_output_chest_item(observation, player, need)
            else:
                decision = self.support_skill._ensure_item(observation, player, need)
            if decision is not None:
                return decision

        drill = layout.get("drill")
        if drill is None:
            position = layout["drill_position"]
            blocker = _blocking_obstacle_near(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing parallel coal drill",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                        "count": 1,
                    },
                    f"clear blocking {blocker.get('name')} before placing parallel coal drill",
                )
            if distance(player, position) > 20 or distance(player, position) < 2.0:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near planned parallel coal burner mining drill",
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
                "place parallel burner mining drill on coal supply patch",
            )

        if use_output_chest:
            chest = layout.get("output_chest")
            if chest is None:
                chest_name = _available_coal_output_chest_name(observation)
                if chest_name is None:
                    return None
                position = layout["output_position"]
                blocker = _blocking_obstacle_near(observation, position)
                if blocker is not None:
                    blocker_position = _position(blocker)
                    if distance(player, blocker_position) > 8:
                        return PlannerDecision(
                            {"type": "move_to", "position": blocker_position},
                            f"move near blocking {blocker.get('name')} before placing parallel coal output chest",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "name": blocker.get("name"),
                            "position": blocker_position,
                            "count": 1,
                        },
                        f"clear blocking {blocker.get('name')} before placing parallel coal output chest",
                    )
                if distance(player, position) > self.BUILD_STANDOFF_DISTANCE or distance(player, position) < 2.0:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                        "move near planned parallel coal output chest",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": chest_name,
                        "position": position,
                    },
                    "place output chest for parallel starter coal supply",
                )
        else:
            belt = layout.get("output_belt")
            if belt is None:
                position = layout["output_position"]
                blocker = _blocking_obstacle_near(observation, position)
                if blocker is not None:
                    blocker_position = _position(blocker)
                    if distance(player, blocker_position) > 8:
                        return PlannerDecision(
                            {"type": "move_to", "position": blocker_position},
                            f"move near blocking {blocker.get('name')} before placing parallel coal output belt",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": blocker.get("unit_number"),
                            "name": blocker.get("name"),
                            "position": blocker_position,
                            "count": 1,
                        },
                        f"clear blocking {blocker.get('name')} before placing parallel coal output belt",
                    )
                if distance(player, position) > self.BUILD_STANDOFF_DISTANCE or distance(player, position) < 2.0:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                        "move near planned parallel coal output belt",
                    )
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "transport-belt",
                        "position": position,
                        "direction": layout["belt_direction"],
                    },
                    "place output belt for parallel coal supply site",
                )

        drill_fuel = _entity_burner_fuel_count(drill)
        if drill_fuel < DIRECT_SMELTING_FUEL_RESERVE:
            return _fuel_burner_line_entity(
                observation,
                player,
                drill,
                entity_name="burner-mining-drill",
                threshold=DIRECT_SMELTING_FUEL_RESERVE,
                insert_count=STARTER_FUEL_BATCH_COUNT,
                context="parallel coal supply site",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="parallel coal supply site is too far from available hand fuel",
                exclude_source_units=_coal_supply_fuel_unit_numbers(observation),
                prefer_coal_supply=False,
            )
        return None


class CoalFuelFeedSkill:
    """Connect a starter coal belt to burner fuel consumers without repeated hand feeding."""

    def __init__(self) -> None:
        self.support_skill = BeltSmeltingLineSkill(target_count=20)

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        boiler_layout = _coal_boiler_fuel_feed_layout(observation)
        local_layout = _coal_fuel_feed_layout(observation)
        if boiler_layout is not None and _boiler_feed_should_preempt_local_coal_feed(boiler_layout, local_layout):
            decision = self._next_boiler_feed_action(observation, player, boiler_layout)
            if decision is not None:
                boiler = boiler_layout.get("boiler")
                if isinstance(boiler, dict):
                    route_started = _boiler_feed_route_started(boiler_layout)
                    # The belt-route boiler feed refuses (returns action=None) when there is no belt
                    # automation/stock. On its own that lets steam power DIE (the boiler hits 0 fuel ->
                    # no power -> assemblers/labs stop). Keep power alive with the same fallbacks
                    # SetupPowerSkill uses: a one-time emergency insert, else a direct mine+hand-carry
                    # (the virtual agent moves instantly so coal within the walk limit is reachable).
                    emergency = None if route_started else _emergency_boiler_bootstrap_fuel_decision(observation, player, boiler, decision)
                    if emergency is not None:
                        return emergency
                    if (
                        decision.action is None
                        and not decision.done
                        and _entity_burner_fuel_count(boiler) < STEAM_POWER_BOILER_FUEL_RESERVE
                    ):
                        if route_started:
                            return PlannerDecision(
                                None,
                                f"{decision.reason}; boiler feed route already started, refusing repeated boiler hand-fueling",
                                metadata=decision.metadata,
                            )
                        handfeed = _fuel_burner_line_entity(
                            observation,
                            player,
                            boiler,
                            entity_name="boiler",
                            threshold=STEAM_POWER_BOILER_FUEL_RESERVE,
                            insert_count=STEAM_POWER_BOILER_FUEL_RESERVE,
                            context="steam power boiler coal feed fallback",
                            support_skill=IronPlateSkill(40),
                            far_fuel_reason="steam power boiler needs local fuel before it can run",
                            wait_for_existing_fuel=True,
                        )
                        if handfeed is not None and handfeed.action is not None:
                            return handfeed
                return decision

        if not _coal_supply_output_belt_sources(observation):
            supply = CoalSupplySkill(target_count=16).next_action(observation)
            if not supply.done:
                return supply

        layout = local_layout or _coal_fuel_feed_layout(observation)
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
            blocker = _coal_fuel_feed_position_blocker(observation, position)
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
                        "unit_number": blocker.get("unit_number"),
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
        if inserter and _entity_burner_fuel_count(inserter) < 1:
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
        if source_drill and _entity_burner_fuel_count(source_drill) < 1:
            return _fuel_burner_line_entity(
                observation,
                player,
                source_drill,
                entity_name="burner-mining-drill",
                threshold=8,
                insert_count=8,
                context="coal fuel feed source drill",
                support_skill=IronPlateSkill(target_count=20),
                far_fuel_reason="coal fuel feed source drill needs local starter fuel before the feed can stay active",
            )

        consumer = layout.get("consumer")
        if consumer and _entity_burner_fuel_count(consumer) > 0:
            return PlannerDecision(
                None,
                "coal fuel feed is active: belt and burner inserter are feeding a furnace fuel inventory",
                done=True,
            )

        return PlannerDecision(
            {"type": "wait", "ticks": 180},
            "wait for coal fuel feed inserter to move coal into the fuel consumer",
        )

    def _next_boiler_feed_action(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        layout: dict[str, Any],
    ) -> PlannerDecision | None:
        missing_belt_count = _missing_boiler_feed_belt_count(layout)
        missing_belt_segments = missing_belt_count > 0
        if (
            missing_belt_segments
            and not _transport_belt_assembler_exists(observation)
            and inventory_count(observation, "transport-belt") <= 0
        ):
            return PlannerDecision(
                None,
                "boiler coal feed needs automated transport-belt production or existing belt stock; refusing repeated boiler hand-fueling",
            )

        belt_assembler = _transport_belt_output_assembler(observation)
        available_belts = _available_boiler_feed_construction_belts(observation)
        if (
            missing_belt_segments
            and _transport_belt_assembler_exists(observation)
            and available_belts < missing_belt_count
        ):
            return PlannerDecision(
                None,
                (
                    f"boiler coal feed needs {missing_belt_count} transport belts for the remaining route; "
                    f"only {available_belts} available from inventory/belt mall, so bootstrap the belt mall "
                    "before partial route extension"
                ),
                metadata={
                    "failure_root": "belt_line_unbuildable",
                    "repair_skill": "bootstrap_build_item_mall",
                    "required_transport_belts": missing_belt_count,
                    "available_transport_belts": available_belts,
                },
            )
        if missing_belt_segments and inventory_count(observation, "transport-belt") <= 0:
            belt_chest = _transport_belt_output_chest(observation)
            if isinstance(belt_chest, dict) and entity_item_count(belt_chest, "transport-belt") > 0:
                position = _position(belt_chest)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        "move near belt mall output chest to collect belts for boiler coal feed",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "transport-belt",
                        "count": min(entity_item_count(belt_chest, "transport-belt"), len(layout["segments"])),
                        "unit_number": belt_chest.get("unit_number"),
                        "name": belt_chest.get("name") or "wooden-chest",
                        "position": position,
                    },
                    "take transport belts from the belt mall output chest as construction material for boiler coal feed",
                )
            if isinstance(belt_assembler, dict) and entity_item_count(belt_assembler, "transport-belt") > 0:
                position = _position(belt_assembler)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        "move near belt mall output to collect belts for boiler coal feed",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "transport-belt",
                        "count": min(entity_item_count(belt_assembler, "transport-belt"), len(layout["segments"])),
                        "unit_number": belt_assembler.get("unit_number"),
                        "name": belt_assembler.get("name") or "assembling-machine-1",
                        "position": position,
                    },
                    "take transport belts from the belt mall as construction material for boiler coal feed",
                )
            return PlannerDecision(
                None,
                "boiler coal feed needs transport belts from the belt mall; refusing hand-crafted belt workaround",
            )

        protected_units = {
            int(entity.get("unit_number"))
            for entity in (layout.get("boiler"), layout.get("source_drill"))
            if isinstance(entity, dict) and entity.get("unit_number") is not None
        }
        # Fast path (FLE-style connect_entities): when every remaining belt segment is clean
        # (nothing misoriented to remove, no blocker) and in reach, place the whole routed path in
        # ONE RCON call instead of one build step (+observe+sleep) per tile. Falls through to the
        # per-tile loop below whenever anything needs clearing / reorienting / relocating first.
        batch = self._batch_boiler_feed_segments(observation, player, layout["segments"], protected_units)
        if batch is not None:
            return batch
        for segment in layout["segments"]:
            existing = segment.get("entity")
            if isinstance(existing, dict):
                if _direction_or_default(existing.get("direction"), segment["direction"]) != int(segment["direction"]):
                    position = _position(existing)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of misoriented boiler coal feed belt",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": existing.get("unit_number"),
                            "name": "transport-belt",
                            "position": position,
                        },
                        "remove misoriented transport belt from the boiler coal feed",
                    )
                continue
            blocker = _belt_line_position_blocker(
                observation,
                segment["position"],
                protected_unit_numbers=protected_units,
            )
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before extending boiler coal feed",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} before extending boiler coal feed",
                )
            position = segment["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near next boiler coal feed belt segment",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "transport-belt",
                    "position": position,
                    "direction": segment["direction"],
                    "allow_nearby": False,
                },
                "extend coal belt toward boiler without player coal shuttle",
            )

        inserter_spec = layout["target_inserter"]
        inserter = inserter_spec.get("entity")
        if isinstance(inserter, dict):
            if _direction_or_default(inserter.get("direction"), 0) != int(inserter_spec["direction"]):
                position = _position(inserter)
                if distance(player, position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                        "move within reach of misoriented boiler coal feed inserter",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": inserter.get("unit_number"),
                        "name": inserter.get("name") or "burner-inserter",
                        "position": position,
                    },
                    "remove misoriented inserter before rebuilding boiler coal feed",
                )
            if str(inserter.get("name") or "") == "burner-inserter":
                replacement = _available_boiler_feed_inserter_item(observation)
                position = _position(inserter)
                if replacement is not None:
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of obsolete boiler coal feed burner inserter",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": inserter.get("unit_number"),
                            "name": "burner-inserter",
                            "position": position,
                        },
                        "replace boiler coal feed burner inserter with a powered inserter",
                    )
                try:
                    excluded_burner_units = {int(inserter.get("unit_number"))}
                except (TypeError, ValueError):
                    excluded_burner_units = set()
                reusable = _find_relocatable_inserter_for_iron_plate_line(
                    observation,
                    position,
                    exclude_unit_numbers=excluded_burner_units,
                    protected_positions=[position, _tile_center_position(position)],
                    allow_burner=False,
                )
                if reusable is not None:
                    reusable_position = _position(reusable)
                    if distance(player, reusable_position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(reusable_position, offset=1.5)},
                            "move within reach of reusable powered inserter before retiring boiler coal feed burner inserter",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": reusable.get("name") or "inserter",
                            "position": reusable_position,
                        },
                        "relocate existing powered inserter before retiring boiler coal feed burner inserter",
                    )
                decision = _logistics_line_inserter_material_decision(
                    observation,
                    player,
                    layout,
                    "boiler coal feed",
                )
                if decision is not None and (decision.done or decision.action is not None):
                    return decision
                decision = self.support_skill._ensure_item(observation, player, "inserter")
                if decision is not None and (decision.done or decision.action is not None):
                    return decision
                return PlannerDecision(
                    None,
                    "boiler coal feed needs a powered inserter; refusing to fuel burner inserter",
                )
            power_repair = _logistics_line_powered_inserter_decision(
                observation,
                player,
                inserter,
                "boiler coal feed",
            )
            if power_repair is not None:
                return power_repair
        else:
            item_name = _available_boiler_feed_inserter_item(observation)
            position = inserter_spec["position"]
            if item_name is None:
                reusable = _find_relocatable_inserter_for_iron_plate_line(
                    observation,
                    position,
                    protected_positions=[position, _tile_center_position(position)],
                    allow_burner=False,
                )
                if reusable is not None:
                    reusable_position = _position(reusable)
                    if distance(player, reusable_position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(reusable_position, offset=1.5)},
                            "move within reach of reusable inserter for boiler coal feed",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": reusable.get("name") or "inserter",
                            "position": reusable_position,
                        },
                        "relocate existing inserter for boiler coal feed instead of hand-fueling the boiler",
                    )
                decision = _logistics_line_inserter_material_decision(
                    observation,
                    player,
                    layout,
                    "boiler coal feed",
                )
                if decision is not None:
                    return decision
                decision = self.support_skill._ensure_item(observation, player, "inserter")
                if decision is not None:
                    return decision
                return PlannerDecision(None, "missing powered inserter for boiler coal feed; refusing burner inserter fallback")
            blocker = _build_position_blocker(observation, position, allowed_names={"burner-inserter", "inserter", "fast-inserter"})
            if blocker is not None and isinstance(layout.get("boiler"), dict):
                try:
                    same_boiler = int(blocker.get("unit_number")) == int(layout["boiler"].get("unit_number"))
                except (TypeError, ValueError):
                    same_boiler = False
                if same_boiler:
                    blocker = None
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing boiler coal feed inserter",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} before placing boiler coal feed inserter",
                )
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near boiler coal feed inserter position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": item_name,
                    "position": position,
                    "direction": inserter_spec["direction"],
                    "allow_nearby": False,
                },
                "place powered inserter for automated boiler coal feed",
            )

        if str(inserter.get("name") or "") == "burner-inserter" and _entity_burner_fuel_count(inserter) < 1:
            fuel_item, fuel_count = _select_inventory_burner_fuel(observation)
            if fuel_count <= 0:
                source = _nearest_boiler_feed_starter_belt_source(layout, _position(inserter))
                if source is not None:
                    source_position = _position(source)
                    if distance(player, source_position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": source_position},
                            "move near boiler feed belt coal to prime burner inserter",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "coal",
                            "count": 1,
                            "unit_number": source.get("unit_number"),
                            "name": source.get("name") or "transport-belt",
                            "position": source_position,
                        },
                        "take one coal from boiler feed belt to prime burner inserter; boiler remains belt-fed",
                    )
                if _boiler_feed_route_has_coal_upstream(layout):
                    return PlannerDecision(
                        {"type": "wait", "ticks": 180},
                        "wait for coal to reach boiler feed belt before priming burner inserter",
                    )
                return PlannerDecision(
                    None,
                    "boiler coal feed burner inserter needs one starter fuel item; refusing to mine or shuttle boiler fuel manually",
                )
            position = _position(inserter)
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": position},
                    "move near boiler coal feed burner inserter to prime it",
                )
            return PlannerDecision(
                {
                    "type": "insert",
                    "item": fuel_item,
                    "count": 1,
                    "unit_number": inserter.get("unit_number"),
                    "name": "burner-inserter",
                    "position": position,
                },
                "prime boiler coal feed burner inserter; boiler itself remains belt-fed",
            )

        boiler = layout.get("boiler")
        if isinstance(boiler, dict) and _entity_burner_fuel_count(boiler) > 0:
            return PlannerDecision(
                None,
                "boiler coal fuel feed is active: belt and inserter are feeding the boiler fuel inventory",
                done=True,
            )
        if _boiler_feed_needs_power_bootstrap_seed(layout):
            fuel_item, fuel_count = _select_inventory_burner_fuel(observation)
            if fuel_count > 0 and isinstance(boiler, dict):
                boiler_position = _position(boiler)
                if distance(player, boiler_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": boiler_position},
                        "move near boiler to seed the completed coal feed power loop",
                    )
                return _bootstrap_seed_decision(
                    {
                        "type": "insert",
                        "item": fuel_item,
                        "count": 1,
                        "unit_number": boiler.get("unit_number"),
                        "name": "boiler",
                        "position": boiler_position,
                    },
                    "seed boiler once so the completed electric coal feed can start moving coal",
                    seed_reason="boiler_coal_feed_power_seed",
                    expected_followup="boiler coal feed inserter powered and boiler receives coal",
                )
        return PlannerDecision(
            {"type": "wait", "ticks": 180},
            "wait for boiler coal feed inserter to move coal into the boiler",
        )

    def _batch_boiler_feed_segments(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        segments: list[dict[str, Any]],
        protected_units: set[int],
    ) -> PlannerDecision | None:
        """Collapse the routed coal-feed belt into a single ``connect_entities`` action when every
        remaining segment is clean and in reach. Returns ``None`` (defer to the per-tile loop) if
        any belt is misoriented, blocked, out of reach, or if belt stock is insufficient — so all
        the existing mine/clear/move recovery paths stay authoritative."""

        buildable: list[dict[str, Any]] = []
        for segment in segments:
            existing = segment.get("entity")
            if isinstance(existing, dict):
                if _direction_or_default(existing.get("direction"), segment["direction"]) != int(segment["direction"]):
                    break  # a misoriented belt must be mined first; defer to the per-tile loop
                continue  # already built correctly
            if _belt_line_position_blocker(observation, segment["position"], protected_unit_numbers=protected_units) is not None:
                break  # a blocker must be cleared first; defer to the per-tile loop
            if distance(player, segment["position"]) > 20:
                break  # must move closer first; defer to the per-tile loop
            buildable.append(segment)
        if len(buildable) < 2:
            return None
        if inventory_count(observation, "transport-belt") < len(buildable):
            return None
        tiles = [
            {"position": segment["position"], "direction": int(segment["direction"])}
            for segment in buildable
        ]
        return PlannerDecision(
            {
                "type": "connect_entities",
                "name": "transport-belt",
                "tiles": tiles,
                "skip_blocked": True,
                "allow_existing": True,
            },
            f"route {len(tiles)} boiler coal feed belt segments in one connect_entities call",
        )


def _boiler_coal_feed_missing_belt_count(observation: dict[str, Any]) -> int:
    layout = _coal_boiler_fuel_feed_layout(observation)
    if layout is None:
        return 0
    return _missing_boiler_feed_belt_count(layout)


def _missing_boiler_feed_belt_count(layout: dict[str, Any]) -> int:
    segments = layout.get("segments") if isinstance(layout.get("segments"), list) else []
    return sum(1 for segment in segments if isinstance(segment, dict) and not isinstance(segment.get("entity"), dict))


def _available_boiler_feed_construction_belts(observation: dict[str, Any]) -> int:
    total = inventory_count(observation, "transport-belt")
    belt_chest = _transport_belt_output_chest(observation)
    if isinstance(belt_chest, dict):
        total += entity_item_count(belt_chest, "transport-belt")
    belt_assembler = _transport_belt_output_assembler(observation)
    if isinstance(belt_assembler, dict):
        total += entity_item_count(belt_assembler, "transport-belt")
    return total


def _available_transport_belt_construction_stock(observation: dict[str, Any]) -> int:
    total = inventory_count(observation, "transport-belt")
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("name") or "") in {"wooden-chest", "iron-chest", "steel-chest"}:
            total += entity_item_count(entity, "transport-belt")
    belt_assembler = _transport_belt_output_assembler(observation)
    if isinstance(belt_assembler, dict):
        total += entity_item_count(belt_assembler, "transport-belt")
    return total


def _boiler_feed_should_preempt_local_coal_feed(
    boiler_layout: dict[str, Any],
    local_layout: dict[str, Any] | None,
) -> bool:
    boiler = boiler_layout.get("boiler")
    if not isinstance(boiler, dict):
        return False
    if _boiler_coal_fuel_feed_layout_complete(boiler_layout):
        return True
    if local_layout is None or _local_coal_fuel_feed_complete(local_layout):
        return True
    if _entity_status_is(boiler, "no_fuel", 52) or _entity_status_is(boiler, "no_input_fluid", 23):
        return True
    if _entity_burner_fuel_count(boiler) <= 0:
        return True
    return False


def _local_coal_fuel_feed_complete(layout: dict[str, Any]) -> bool:
    consumer = layout.get("consumer")
    return isinstance(consumer, dict) and _entity_burner_fuel_count(consumer) > 0


def _boiler_coal_fuel_feed_layout_complete(layout: dict[str, Any]) -> bool:
    boiler = layout.get("boiler")
    target_inserter = layout.get("target_inserter")
    return (
        isinstance(boiler, dict)
        and _entity_burner_fuel_count(boiler) > 0
        and all(isinstance(segment.get("entity"), dict) for segment in layout.get("segments") or [])
        and isinstance(target_inserter, dict)
        and isinstance(target_inserter.get("entity"), dict)
    )


def _boiler_feed_route_started(layout: dict[str, Any]) -> bool:
    segments = layout.get("segments") if isinstance(layout.get("segments"), list) else []
    # Segment 0 is usually the source drill output belt that existed before this route.
    if any(isinstance(segment.get("entity"), dict) for segment in segments[1:] if isinstance(segment, dict)):
        return True
    target_inserter = layout.get("target_inserter")
    return isinstance(target_inserter, dict) and isinstance(target_inserter.get("entity"), dict)


def _boiler_feed_route_built(layout: dict[str, Any]) -> bool:
    segments = layout.get("segments") if isinstance(layout.get("segments"), list) else []
    target_inserter = layout.get("target_inserter")
    return (
        bool(segments)
        and all(isinstance(segment.get("entity"), dict) for segment in segments if isinstance(segment, dict))
        and isinstance(target_inserter, dict)
        and isinstance(target_inserter.get("entity"), dict)
    )


def _boiler_feed_needs_power_bootstrap_seed(layout: dict[str, Any]) -> bool:
    boiler = layout.get("boiler")
    target_inserter = layout.get("target_inserter")
    inserter = target_inserter.get("entity") if isinstance(target_inserter, dict) else None
    return (
        isinstance(boiler, dict)
        and _entity_burner_fuel_count(boiler) <= 0
        and _boiler_feed_route_built(layout)
        and isinstance(inserter, dict)
        and str(inserter.get("name") or "") != "burner-inserter"
        and _entity_no_power(inserter)
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
                return self._direct_cell_recovery_before_fuel_failure(observation, decision)

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
            ("inserter", "inserter_position", "inserter_direction"),
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
            return self._direct_cell_recovery_before_fuel_failure(observation, reserve_decision)

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
        for entity_name, layout_key, _item, _threshold, _insert_count in _smelting_line_fuel_requirements(layout, reserve=False):
            entity = layout.get(layout_key)
            if entity and _entity_burner_fuel_count(entity) < 1:
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

        for entity_name, layout_key, _item, threshold, insert_count in _smelting_line_fuel_requirements(layout, reserve=True):
            entity = layout.get(layout_key)
            if entity and _entity_burner_fuel_count(entity) < threshold:
                return _fuel_burner_line_entity(
                    observation,
                    player,
                    entity,
                    entity_name=entity_name,
                    threshold=threshold,
                    insert_count=insert_count,
                    context=f"expanded {self.product_name} smelting reserve",
                    support_skill=self.line_skill.support_skill,
                    far_fuel_reason=f"expanded {self.product_name} smelting needs fuel logistics before more walking refuels",
                    exclude_source_units=line_units,
                )
        return None

    def _direct_cell_recovery_before_fuel_failure(
        self,
        observation: dict[str, Any],
        blocked: PlannerDecision,
    ) -> PlannerDecision:
        if blocked.action is not None or "fuel logistics" not in blocked.reason:
            return blocked
        if not _recoverable_direct_plate_cell_exists(observation, self.resource_name, self.product_name):
            return blocked
        target = inventory_count(observation, self.product_name) + DIRECT_SMELTING_CELL_TARGET_PLATES
        if self.product_name == "copper-plate":
            recovery = CopperPlateSkill(target_count=target).next_action(observation, target_count=target, inventory_only=True)
        else:
            recovery = IronPlateSkill(target_count=target).next_action(observation, target_count=target, inventory_only=True)
        if recovery.action is None:
            return blocked
        metadata = dict(recovery.metadata)
        metadata.update(
            {
                "expanded_smelting_recovery": True,
                "blocked_reason": blocked.reason,
                "product": self.product_name,
            }
        )
        return PlannerDecision(
            recovery.action,
            f"{recovery.reason}; recover direct {self.product_name} cell before retrying expanded smelting fuel logistics",
            done=False,
            metadata=metadata,
        )


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
        if inventory_count(observation, "iron-gear-wheel") < 10:
            decision = _ensure_iron_gears_without_post_automation_handcraft(
                observation,
                10,
                pre_automation_reason="craft gears for starter defense turret",
                allow_assembler_output_gears=True,
                infrastructure_reason="starter defense turret infrastructure",
            )
            if decision is not None:
                return decision
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
        allow_bootstrap_power_seed: bool = False,
    ) -> PlannerDecision:
        block = _find_steam_power_block(
            observation,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
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
            remote_prefix = "remote bootstrap " if layout.get("remote_bootstrap_power") else ""
            # Self-calibrating fluid placement (kills the per-pump-direction geometry bug class):
            # the boiler must connect to the pump and the engine to the boiler. Instead of hardcoded
            # rotated offsets (wrong for N/S pumps -> the "boiler needs a pipe / no water" bug), let
            # the game find the connecting tile via place_fluid_connected.
            upstream_key = {"boiler": "offshore_pump", "steam_engine": "boiler"}.get(key)
            if upstream_key is not None and isinstance(layout.get(upstream_key), dict):
                target_position = _position(layout[upstream_key])
                if distance(player, target_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": _power_stand_position(layout)},
                        f"move near {upstream_key} to place {remote_prefix}{spec['name']}",
                    )
                return PlannerDecision(
                    {
                        "type": "place_fluid_connected",
                        "name": spec["name"],
                        "target_position": target_position,
                        "search_radius": 4,
                    },
                    f"place {remote_prefix}{spec['name']} self-connected to {upstream_key} for first steam power block",
                )
            # The pole must sit within wire reach of the (self-calibrated, possibly relocated) engine
            # to plug it into the network -- anchor it on the built engine, not the stale spec tile.
            if key == "small_electric_pole" and isinstance(layout.get("steam_engine"), dict):
                position = _position(layout["steam_engine"])
            else:
                position = spec["position"]
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
                    # The pump lands on its exact water-edge tile; the pole only needs land within wire
                    # reach of the engine, so allow_nearby lets the Lua relocate it to the nearest
                    # buildable tile next to the engine.
                    "allow_nearby": key == "small_electric_pole",
                },
                f"place {remote_prefix}{spec['name']} for first steam power block",
            )

        boiler = layout.get("boiler")
        if boiler and _entity_burner_fuel_count(boiler) < STEAM_POWER_BOILER_FUEL_RESERVE:
            feed_layout = _coal_boiler_fuel_feed_layout(observation)
            if feed_layout is not None:
                feed_decision = CoalFuelFeedSkill()._next_boiler_feed_action(observation, player, feed_layout)
                if feed_decision is not None:
                    emergency = _emergency_boiler_bootstrap_fuel_decision(
                        observation,
                        player,
                        boiler,
                        feed_decision,
                        allow_without_critical_factory=allow_bootstrap_power_seed,
                    )
                    if emergency is not None:
                        return emergency
                    return feed_decision
            return _fuel_burner_line_entity(
                observation,
                player,
                boiler,
                entity_name="boiler",
                threshold=STEAM_POWER_BOILER_FUEL_RESERVE,
                insert_count=STEAM_POWER_BOILER_FUEL_RESERVE,
                context="first steam power block",
                support_skill=self.iron_skill,
                far_fuel_reason="steam power boiler needs local fuel before it can run",
                wait_for_existing_fuel=True,
            )

        if _steam_power_ready(layout):
            return PlannerDecision(None, "steam power block is producing usable steam power", done=True)

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
    return has_material and _entity_burner_fuel_count(entity) > 0


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
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    cache_key = id(observation)
    cache_signature = (id(entities), len(entities))
    cached = _STARTER_ANCHOR_CACHE.get(cache_key)
    if cached is not None and cached[0] == cache_signature[0] and cached[1] == cache_signature[1]:
        return [dict(anchor) for anchor in cached[2]]

    base_anchor = _base_anchor_position(observation)
    anchors: list[dict[str, float]] = [base_anchor] if base_anchor is not None else [player_position(observation)]
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
    if len(_STARTER_ANCHOR_CACHE) > 64:
        _STARTER_ANCHOR_CACHE.clear()
    _STARTER_ANCHOR_CACHE[cache_key] = (cache_signature[0], cache_signature[1], [dict(anchor) for anchor in anchors])
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
    return actual == resource_name


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
    # Iron stock can read as 'done' while a burner drill sits stranded off the ore -- production is
    # actually dead and the stock is stuck in a far furnace, so the assemblers silently starve once it
    # drains. When the iron drill is stranded, do NOT report done; fall through to recover it.
    iron_drill_stranded = resource_name == "iron-ore" and _direct_iron_drill_stranded(observation)
    if total_product >= target_count and not iron_drill_stranded:
        return PlannerDecision(None, f"{product_name} target reached: {total_product}/{target_count}", done=True)

    player = player_position(observation)
    output_furnace = _select_plate_output_furnace(observation, resource_name, product_name)
    if inventory_only and output_furnace and entity_item_count(output_furnace, product_name) > 0:
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

    direct_cells = _direct_smelting_cells(observation, resource_name)
    layout = (
        _find_low_fuel_direct_smelting_cell(observation, resource_name)
        or (direct_cells[0] if direct_cells else None)
        or _select_direct_smelting_layout(observation, resource_name)
    )
    if layout is None:
        if _inventory_burner_fuel_count(observation) <= 0:
            source = _nearest_surplus_fuel_source(observation, player)
            if source is not None and _surplus_fuel_source_is_logistic_output(source, observation):
                return _take_surplus_fuel_source_decision(player, source, f"direct {product_name} smelting")
            coal = nearest_resource(observation, "coal")
            if coal is not None:
                return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)
        if inventory_count(observation, "burner-mining-drill") <= 0:
            recovery = _recover_direct_smelting_drill_decision(
                observation,
                player,
                resource_name=resource_name,
                product_name=product_name,
            )
            if recovery is not None:
                return recovery
        return PlannerDecision(None, f"cannot find open {resource_name} site for direct burner-drill smelting cell")

    misplaced_drill = layout.get("misplaced_drill")
    if (
        not isinstance(misplaced_drill, dict)
        and layout.get("drill") is None
        and inventory_count(observation, "burner-mining-drill") <= 0
    ):
        misplaced_drill = _recoverable_unpaired_direct_smelting_drill(observation, resource_name)
    if isinstance(misplaced_drill, dict) and layout.get("drill") is None:
        position = _position(misplaced_drill)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near misplaced direct {product_name} mining drill before rebuilding exact smelting cell",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": misplaced_drill.get("unit_number"),
                "name": "burner-mining-drill",
                "position": position,
            },
            f"recover misplaced direct {product_name} mining drill before rebuilding exact smelting cell",
        )

    misplaced_furnace = layout.get("misplaced_furnace")
    if isinstance(misplaced_furnace, dict) and layout.get("furnace") is None:
        position = _position(misplaced_furnace)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near misplaced direct {product_name} furnace before rebuilding exact smelting cell",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": misplaced_furnace.get("unit_number"),
                "name": misplaced_furnace.get("name") or "stone-furnace",
                "position": position,
            },
            f"recover misplaced direct {product_name} furnace before rebuilding exact smelting cell",
        )

    furnace = layout.get("furnace")
    if inventory_only and furnace and entity_item_count(furnace, product_name) > 0:
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

    if _inventory_burner_fuel_count(observation) <= 0:
        source = _nearest_surplus_fuel_source(observation, player)
        if source is not None and _surplus_fuel_source_is_logistic_output(source, observation):
            return _take_surplus_fuel_source_decision(player, source, f"direct {product_name} smelting")
        coal = nearest_resource(observation, "coal")
        if coal is None:
            return PlannerDecision(None, f"cannot find nearby burner fuel for direct {product_name} smelting")
        return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)

    missing = _direct_smelting_missing_item(observation, layout)
    if missing:
        decision = _ensure_direct_smelting_item(
            observation,
            player,
            missing,
            resource_name=resource_name,
            product_name=product_name,
            support_skill=support_skill,
            allow_support_plate=allow_support_plate,
        )
        if decision is not None:
            return decision

    drill = layout.get("drill")
    if isinstance(drill, dict) and _entity_status_is(drill, "no_minable_resources", 21):
        position = _position(drill)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near invalid direct {product_name} mining drill before relocating it",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": drill.get("unit_number"),
                "name": "burner-mining-drill",
                "position": position,
            },
            f"recover invalid direct {product_name} mining drill with no minable resources",
        )

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
                "allow_nearby": False,
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
        if entity and _entity_burner_fuel_count(entity) < DIRECT_SMELTING_FUEL_RESERVE:
            return _fuel_burner_line_entity(
                observation,
                player,
                entity,
                entity_name=entity_name,
                threshold=DIRECT_SMELTING_FUEL_RESERVE,
                insert_count=STARTER_FUEL_BATCH_COUNT,
                context=f"direct {product_name} smelting cell",
                support_skill=support_skill,
                far_fuel_reason=f"direct {product_name} smelting needs local fuel before it can run",
                exclude_source_units=_direct_smelting_fuel_unit_numbers(observation, resource_name),
                wait_for_existing_fuel=True,
            )

    desired_cells = _direct_smelting_desired_parallel_cells(
        observation,
        target_count=target_count,
        inventory_only=inventory_only,
    )
    if len(direct_cells) < desired_cells:
        expansion_layout = _select_direct_smelting_expansion_layout(observation, resource_name)
        if expansion_layout is not None:
            decision = _direct_smelting_expansion_decision(
                observation,
                player,
                expansion_layout,
                resource_name=resource_name,
                product_name=product_name,
                support_skill=support_skill,
                allow_support_plate=allow_support_plate,
            )
            if decision is not None:
                return decision

    return PlannerDecision(
        {"type": "wait", "ticks": 120},
        f"wait for direct {product_name} burner-drill smelting cell",
    )


def _ensure_direct_smelting_item(
    observation: dict[str, Any],
    player: dict[str, float],
    item: str,
    *,
    resource_name: str,
    product_name: str,
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
        recovery = _recover_direct_smelting_drill_decision(
            observation,
            player,
            resource_name=resource_name,
            product_name=product_name,
        )
        if recovery is not None:
            return recovery
        if inventory_count(observation, "stone") < 5:
            decision = StoneSupplySkill(target_count=8).next_action(observation)
            if not decision.done:
                return decision
        if inventory_count(observation, "iron-gear-wheel") < 3:
            decision = _ensure_iron_gears_without_post_automation_handcraft(
                observation,
                3,
                pre_automation_reason="craft gears for direct smelting drill",
                allow_assembler_output_gears=True,
                infrastructure_reason="direct smelting drill infrastructure",
            )
            if decision is not None:
                return decision
        if not allow_support_plate:
            return PlannerDecision(None, "missing burner mining drill and cannot bootstrap another iron drill from current inventory")
        return support_skill.next_action(observation, target_count=20, inventory_only=True)

    return None


def _recover_direct_smelting_drill_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    *,
    resource_name: str,
    product_name: str,
) -> PlannerDecision | None:
    recoverable_drill = _recoverable_unpaired_direct_smelting_drill(observation, resource_name)
    temporary = False
    if recoverable_drill is None:
        recoverable_drill = _recoverable_temporary_burner_drill_for_direct_smelting(observation, resource_name)
        temporary = recoverable_drill is not None
    if recoverable_drill is None:
        return None
    position = _position(recoverable_drill)
    if distance(player, position) > 8:
        return PlannerDecision(
            {"type": "move_to", "position": _stand_position(position, offset=2.0)},
            f"move near recoverable burner mining drill before reallocating it to {product_name} production",
        )
    source_resource = _entity_resource_name(observation, recoverable_drill, radius=4.5) or "temporary resource"
    reason_prefix = "temporary" if temporary else "incomplete direct"
    return PlannerDecision(
        {
            "type": "mine",
            "unit_number": recoverable_drill.get("unit_number"),
            "name": "burner-mining-drill",
            "position": position,
        },
        f"recover {reason_prefix} {source_resource} burner mining drill for {product_name} production",
    )


def _select_plate_output_furnace(observation: dict[str, Any], resource_name: str, product_name: str) -> dict[str, Any] | None:
    output_furnace = _select_any_plate_output_furnace(observation, product_name)
    if output_furnace is not None:
        return output_furnace
    if resource_name == "iron-ore" and product_name == "iron-plate":
        return _select_iron_furnace(observation)
    if resource_name == "copper-ore" and product_name == "copper-plate":
        return _select_copper_furnace(observation)
    furnaces = _entities_within_starter_area(observation, entities_named(observation, "stone-furnace"))
    for furnace in furnaces:
        if entity_item_count(furnace, product_name) > 0 or entity_item_count(furnace, resource_name) > 0:
            return furnace
    return None


def _select_any_plate_output_furnace(observation: dict[str, Any], product_name: str) -> dict[str, Any] | None:
    furnaces: list[dict[str, Any]] = []
    for name in FURNACE_ENTITY_NAMES:
        furnaces.extend(entities_named(observation, name))
    output_furnaces = [furnace for furnace in furnaces if entity_item_count(furnace, product_name) > 0]
    return _nearest_to(output_furnaces, player_position(observation))


def _take_existing_plate_output_decision(
    observation: dict[str, Any],
    *,
    resource_name: str,
    product_name: str,
) -> PlannerDecision | None:
    furnace = _select_plate_output_furnace(observation, resource_name, product_name)
    if not isinstance(furnace, dict) or entity_item_count(furnace, product_name) <= 0:
        return None
    furnace_pos = _position(furnace)
    player = player_position(observation)
    if distance(player, furnace_pos) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": furnace_pos},
            f"move near existing {product_name} furnace output",
        )
    return PlannerDecision(
        {
            "type": "take",
            "item": product_name,
            "count": min(50, entity_item_count(furnace, product_name)),
            "unit_number": furnace.get("unit_number"),
            "name": furnace.get("name") or "stone-furnace",
            "position": furnace_pos,
        },
        f"take {product_name} from existing furnace output",
    )


def _direct_smelting_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    if layout.get("drill") is None and inventory_count(observation, "burner-mining-drill") <= 0:
        return "burner-mining-drill"
    if layout.get("furnace") is None and inventory_count(observation, "stone-furnace") <= 0:
        return "stone-furnace"
    return None


def _direct_smelting_desired_parallel_cells(
    observation: dict[str, Any],
    *,
    target_count: int,
    inventory_only: bool,
) -> int:
    if bool(_technology_state(observation, "electric-mining-drill").get("researched")):
        return 1
    if total_item_count(observation, "electric-mining-drill") > 0 or entities_named(observation, "electric-mining-drill"):
        return 1
    target_cells = max(1, ceil(max(1, target_count) / DIRECT_SMELTING_CELL_TARGET_PLATES))
    if not inventory_only and target_count >= 10:
        target_cells = max(DIRECT_SMELTING_MIN_PARALLEL_CELLS, target_cells)
    return min(DIRECT_SMELTING_MAX_PARALLEL_CELLS, target_cells)


def _direct_smelting_cells(observation: dict[str, Any], resource_name: str) -> list[dict[str, Any]]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_position = _position(drill)
        target_resource = _entity_resource_name(observation, drill, radius=4.5)
        if target_resource != resource_name:
            continue
        if not _within_starter_logistics_area(observation, drill_position):
            continue
        layout = _direct_smelting_layout_from_existing_drill(observation, drill, resource_name)
        if layout.get("furnace") is None:
            continue
        candidates.append((float(drill.get("distance") or distance(player_position(observation), drill_position)), layout))
    candidates.sort(key=lambda item: item[0])
    return [layout for _distance, layout in candidates]


def _find_low_fuel_direct_smelting_cell(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for layout in _direct_smelting_cells(observation, resource_name):
        fuel_counts = [
            _entity_burner_fuel_count(entity)
            for entity in (layout.get("drill"), layout.get("furnace"))
            if isinstance(entity, dict)
        ]
        if fuel_counts and min(fuel_counts) < DIRECT_SMELTING_FUEL_RESERVE:
            drill = layout.get("drill") if isinstance(layout.get("drill"), dict) else {}
            candidates.append((min(fuel_counts), float(drill.get("distance") or 999999), layout))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _direct_smelting_fuel_unit_numbers(observation: dict[str, Any], resource_name: str) -> set[Any]:
    units: set[Any] = set()
    for layout in _direct_smelting_cells(observation, resource_name):
        for key in ("drill", "furnace"):
            entity = layout.get(key)
            if isinstance(entity, dict):
                units.add(entity.get("unit_number"))
    return units


def _find_direct_smelting_cell(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    cells = _direct_smelting_cells(observation, resource_name)
    return cells[0] if cells else None


def _recoverable_direct_plate_cell_exists(
    observation: dict[str, Any],
    resource_name: str,
    product_name: str,
) -> bool:
    for layout in _direct_smelting_cells(observation, resource_name):
        furnace = layout.get("furnace")
        if isinstance(furnace, dict) and entity_item_count(furnace, product_name) > 0:
            return True
        if _inventory_burner_fuel_count(observation) <= 0:
            continue
        for entity in (layout.get("drill"), furnace):
            if isinstance(entity, dict) and _entity_burner_fuel_count(entity) < DIRECT_SMELTING_FUEL_RESERVE:
                return True
    return False


def _select_direct_smelting_layout(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, resource_name):
        drill_position = _direct_smelting_drill_center(_position(resource))
        for orientation in ("east", "west", "south", "north"):
            layout = _direct_smelting_layout_from_drill_position(drill_position, resource_name=resource_name, orientation=orientation)
            drill = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            if isinstance(drill, dict) and _entity_resource_name(observation, drill, radius=4.5) == resource_name:
                if distance(_position(drill), layout["drill_position"]) <= 0.75:
                    layout = _direct_smelting_layout_from_existing_drill(observation, drill, resource_name)
                else:
                    continue
            else:
                layout["drill"] = None
            layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=0.75)
            if isinstance(layout.get("drill"), dict) and isinstance(layout.get("furnace"), dict):
                if not _burner_drill_output_touches_machine(layout["drill"], layout["furnace"]):
                    layout["misplaced_furnace"] = layout["furnace"]
                    layout["furnace"] = None
            if layout.get("furnace") is None:
                near_furnace = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=2.5)
                if isinstance(near_furnace, dict):
                    layout["misplaced_furnace"] = near_furnace
            if not _direct_smelting_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _select_direct_smelting_expansion_layout(observation: dict[str, Any], resource_name: str) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, resource_name):
        drill_position = _direct_smelting_drill_center(_position(resource))
        for orientation in ("east", "west", "south", "north"):
            layout = _direct_smelting_layout_from_drill_position(
                drill_position,
                resource_name=resource_name,
                orientation=orientation,
            )
            drill = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            if isinstance(drill, dict) and _entity_resource_name(observation, drill, radius=4.5) == resource_name:
                if distance(_position(drill), layout["drill_position"]) <= 0.75:
                    layout = _direct_smelting_layout_from_existing_drill(observation, drill, resource_name)
                else:
                    continue
            else:
                layout["drill"] = None
            layout["furnace"] = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=0.75)
            if isinstance(layout.get("drill"), dict) and isinstance(layout.get("furnace"), dict):
                if _burner_drill_output_touches_machine(layout["drill"], layout["furnace"]):
                    continue
                layout["misplaced_furnace"] = layout["furnace"]
                layout["furnace"] = None
            if layout.get("furnace") is None:
                near_furnace = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=2.5)
                if isinstance(near_furnace, dict):
                    layout["misplaced_furnace"] = near_furnace
            if not _direct_smelting_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _direct_smelting_expansion_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    layout: dict[str, Any],
    *,
    resource_name: str,
    product_name: str,
    support_skill: IronPlateSkill,
    allow_support_plate: bool,
) -> PlannerDecision | None:
    misplaced_drill = layout.get("misplaced_drill")
    if isinstance(misplaced_drill, dict) and layout.get("drill") is None:
        position = _position(misplaced_drill)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near misplaced parallel {product_name} mining drill before rebuilding exact smelting cell",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": misplaced_drill.get("unit_number"),
                "name": "burner-mining-drill",
                "position": position,
            },
            f"recover misplaced parallel {product_name} mining drill before rebuilding exact smelting cell",
        )

    misplaced_furnace = layout.get("misplaced_furnace")
    if isinstance(misplaced_furnace, dict) and layout.get("furnace") is None:
        position = _position(misplaced_furnace)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near misplaced parallel {product_name} furnace before rebuilding exact smelting cell",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": misplaced_furnace.get("unit_number"),
                "name": misplaced_furnace.get("name") or "stone-furnace",
                "position": position,
            },
            f"recover misplaced parallel {product_name} furnace before rebuilding exact smelting cell",
        )

    missing = _direct_smelting_missing_item(observation, layout)
    if missing:
        decision = _ensure_direct_smelting_item(
            observation,
            player,
            missing,
            resource_name=resource_name,
            product_name=product_name,
            support_skill=support_skill,
            allow_support_plate=allow_support_plate,
        )
        if decision is not None:
            return decision

    drill = layout.get("drill")
    if isinstance(drill, dict) and _entity_status_is(drill, "no_minable_resources", 21):
        position = _position(drill)
        if distance(player, position) > 8:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=2.0)},
                f"move near invalid parallel {product_name} mining drill before relocating it",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": drill.get("unit_number"),
                "name": "burner-mining-drill",
                "position": position,
            },
            f"recover invalid parallel {product_name} mining drill with no minable resources",
        )

    if drill is None:
        position = layout["drill_position"]
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position)},
                f"move near {resource_name.replace('-', ' ')} before placing parallel direct burner mining drill",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "burner-mining-drill",
                "position": position,
                "direction": layout["drill_direction"],
                "allow_nearby": False,
                "required_resource": resource_name,
            },
            f"place parallel burner mining drill for direct {product_name} smelting cell",
        )

    if layout.get("furnace") is None:
        position = layout["furnace_position"]
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position)},
                f"move near parallel direct {product_name} furnace position",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "stone-furnace",
                "position": position,
                "allow_nearby": False,
            },
            f"place parallel furnace at {resource_name} drill output",
        )

    return None


def _direct_smelting_drill_center(position: dict[str, float]) -> dict[str, float]:
    return {
        "x": round(floor(float(position["x"]) + 0.5), 1),
        "y": round(floor(float(position["y"]) + 0.5), 1),
    }


def _direct_smelting_layout_from_existing_drill(
    observation: dict[str, Any],
    drill: dict[str, Any],
    resource_name: str,
) -> dict[str, Any]:
    drill_position = _position(drill)
    orientation = _direction_to_orientation(_direction_or_default(drill.get("direction"), EAST))
    layout = _direct_smelting_layout_from_drill_position(
        drill_position,
        resource_name=resource_name,
        orientation=orientation,
    )
    layout["drill"] = drill
    furnace = _entity_near(observation, "stone-furnace", layout["furnace_position"], radius=0.75)
    if isinstance(furnace, dict) and not _burner_drill_output_touches_machine(drill, furnace):
        furnace = None
    layout["furnace"] = furnace
    return layout


def _recoverable_unpaired_direct_smelting_drill(
    observation: dict[str, Any],
    resource_name: str,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    player = player_position(observation)
    for drill in entities_named(observation, "burner-mining-drill"):
        if _entity_resource_name(observation, drill, radius=4.5) != resource_name:
            continue
        if not _within_starter_logistics_area(observation, _position(drill)):
            continue
        layout = _direct_smelting_layout_from_existing_drill(observation, drill, resource_name)
        if isinstance(layout.get("furnace"), dict):
            continue
        candidates.append((float(drill.get("distance") or distance(player, _position(drill))), drill))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _recoverable_temporary_burner_drill_for_direct_smelting(
    observation: dict[str, Any],
    resource_name: str,
) -> dict[str, Any] | None:
    # Coal supply is the bootstrap fuel spine; do not tear it down to repair plate production.
    if resource_name != "iron-ore":
        return None
    player = player_position(observation)
    resource_priority = {"stone": 0, "copper-ore": 1}
    candidates: list[tuple[int, int, int, float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_resource = _entity_resource_name(observation, drill, radius=4.5)
        if drill_resource in {None, "", "coal", resource_name}:
            continue
        if drill_resource not in resource_priority:
            continue
        if not _within_starter_logistics_area(observation, _position(drill)):
            continue
        no_fuel_rank = 0 if _entity_status_is(drill, "no_fuel", 53) or _entity_burner_fuel_count(drill) <= 0 else 1
        candidates.append(
            (
                resource_priority[drill_resource],
                no_fuel_rank,
                _entity_burner_fuel_count(drill),
                float(drill.get("distance") or distance(player, _position(drill))),
                drill,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:4])
    return candidates[0][4]


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
        "misplaced_drill": None,
        "misplaced_furnace": None,
    }


def _direct_smelting_layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    planned_entities = (
        layout.get("drill"),
        layout.get("furnace"),
        layout.get("misplaced_drill"),
        layout.get("misplaced_furnace"),
    )
    layout_entities = {id(entity) for entity in planned_entities if isinstance(entity, dict)}
    layout_units = {
        entity.get("unit_number")
        for entity in planned_entities
        if isinstance(entity, dict) and entity.get("unit_number") is not None
    }
    footprint = [layout["drill_position"], layout["furnace_position"]]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if id(entity) in layout_entities or (entity.get("unit_number") is not None and entity.get("unit_number") in layout_units):
            continue
        name = str(entity.get("name") or "")
        entity_type = str(entity.get("type") or "")
        if not name or entity_type == "resource":
            continue
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
        orientation = _direction_to_orientation(_direction_or_default(drill.get("direction"), EAST))
        layout = _stone_supply_layout_from_drill_position(drill_position, orientation=orientation)
        layout["output_position"] = _burner_drill_output_position(drill)
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
        direction = _direction_to_orientation(_direction_or_default(drill.get("direction"), EAST))
        layout = _coal_supply_layout_from_drill_position(drill_position, orientation=direction)
        layout["output_position"] = _burner_drill_output_position(drill)
        layout["drill"] = drill
        layout["output_belt"] = _entity_at_build_position(
            observation,
            "transport-belt",
            layout["output_position"],
            radius=0.75,
        )
        layout["output_chest"] = _coal_output_chest_near(observation, layout["output_position"])
        candidates.append(
            (
                layout["output_belt"] is not None or layout["output_chest"] is not None,
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
            layout["output_belt"] = _entity_at_build_position(
                observation,
                "transport-belt",
                layout["output_position"],
                radius=0.75,
            )
            layout["output_chest"] = _coal_output_chest_near(observation, layout["output_position"])
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
        "output_chest": None,
    }


def _burner_drill_output_position(drill: dict[str, Any]) -> dict[str, float]:
    drop_position = drill.get("drop_position")
    if isinstance(drop_position, dict):
        return _tile_center_position(drop_position)
    position = _position(drill)
    direction = _direction_or_default(drill.get("direction"), EAST)
    integer_center = abs(position["x"] - round(position["x"])) < 0.01 and abs(position["y"] - round(position["y"])) < 0.01
    if not integer_center:
        orientation = _direction_to_orientation(direction)
        dx, dy, _drill_direction, _belt_direction, _inserter_direction = _smelting_orientation(orientation)
        return {"x": position["x"] + 2 * dx, "y": position["y"] + 2 * dy}
    if direction == WEST:
        return {"x": round(position["x"] - 1.5, 3), "y": round(position["y"] + 0.5, 3)}
    if direction == SOUTH:
        return {"x": round(position["x"] + 0.5, 3), "y": round(position["y"] + 1.5, 3)}
    if direction == NORTH:
        return {"x": round(position["x"] + 0.5, 3), "y": round(position["y"] - 1.5, 3)}
    return {"x": round(position["x"] + 1.5, 3), "y": round(position["y"] + 0.5, 3)}


def _find_misplaced_coal_supply_output_belt(observation: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any] | None:
    output_position = layout.get("output_position")
    if not isinstance(output_position, dict):
        return None
    candidates = []
    for belt in entities_named(observation, "transport-belt"):
        belt_position = _position(belt)
        if distance(belt_position, output_position) <= 0.35:
            continue
        if distance(belt_position, output_position) <= 1.6:
            candidates.append((distance(belt_position, output_position), belt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _coal_supply_layout_blocked_by_factory_entities(layout: dict[str, Any], entities: list[Any]) -> bool:
    layout_entities = {
        id(entity)
        for entity in (layout.get("drill"), layout.get("output_belt"), layout.get("output_chest"))
        if isinstance(entity, dict)
    }
    layout_units = {
        entity.get("unit_number")
        for entity in (layout.get("drill"), layout.get("output_belt"), layout.get("output_chest"))
        if isinstance(entity, dict) and entity.get("unit_number") is not None
    }
    footprint = [layout["drill_position"], layout["output_position"]]
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if id(entity) in layout_entities or (entity.get("unit_number") is not None and entity.get("unit_number") in layout_units):
            continue
        name = str(entity.get("name") or "")
        if name in {"character", "transport-belt", "burner-mining-drill", "wooden-chest", "iron-chest", "steel-chest"}:
            entity_pos = _position(entity)
            threshold = 3.0 if name == "burner-mining-drill" else 2.0
            if any(distance(entity_pos, pos) < threshold for pos in footprint):
                return True
    return False


def _coal_supply_should_use_output_chest(observation: dict[str, Any], layout: dict[str, Any]) -> bool:
    if layout.get("output_belt") is not None:
        return False
    if _find_misplaced_coal_supply_output_belt(observation, layout) is not None:
        return False
    if inventory_count(observation, "transport-belt") > 0 or _transport_belt_automation_output_ready(observation):
        return False
    if layout.get("output_chest") is not None:
        return True
    return not _transport_belt_automation_output_ready(observation)


def _coal_supply_desired_parallel_drills(observation: dict[str, Any]) -> int:
    if bool(_technology_state(observation, "electric-mining-drill").get("researched")):
        return 1
    if total_item_count(observation, "electric-mining-drill") > 0 or entities_named(observation, "electric-mining-drill"):
        return 1
    return STARTER_COAL_SUPPLY_DRILL_TARGET


def _coal_supply_burner_drill_count(observation: dict[str, Any]) -> int:
    return sum(
        1
        for drill in entities_named(observation, "burner-mining-drill")
        if _entity_resource_name(observation, drill, radius=4.5) == "coal"
        and _within_starter_logistics_area(observation, _position(drill))
    )


def _coal_supply_fuel_unit_numbers(observation: dict[str, Any]) -> set[Any]:
    return {
        drill.get("unit_number")
        for drill in entities_named(observation, "burner-mining-drill")
        if _entity_resource_name(observation, drill, radius=4.5) == "coal"
    }


def _select_coal_supply_expansion_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for resource in _ranked_patch_drill_resources(observation, "coal"):
        for orientation in ("east", "west", "south", "north"):
            layout = _coal_supply_layout_from_drill_position(_position(resource), orientation=orientation)
            layout["drill"] = _entity_near(observation, "burner-mining-drill", layout["drill_position"], radius=2.0)
            if isinstance(layout.get("drill"), dict) and _entity_resource_name(observation, layout["drill"], radius=4.5) == "coal":
                continue
            layout["output_belt"] = _entity_at_build_position(
                observation,
                "transport-belt",
                layout["output_position"],
                radius=0.75,
            )
            layout["output_chest"] = _coal_output_chest_near(observation, layout["output_position"])
            if not _coal_supply_layout_blocked_by_factory_entities(layout, entities):
                return layout
    return None


def _coal_supply_missing_item(
    observation: dict[str, Any],
    layout: dict[str, Any],
    *,
    use_output_chest: bool = False,
) -> str | None:
    if layout.get("drill") is None:
        if inventory_count(observation, "burner-mining-drill") <= 0:
            return "burner-mining-drill"
        return None

    if use_output_chest:
        if layout.get("output_chest") is None and _available_coal_output_chest_name(observation) is None:
            if (
                craftable_count(observation, "wooden-chest") > 0
                or inventory_count(observation, "wood") >= 2
                or _nearest_tree(observation) is not None
            ):
                return "wooden-chest"
            return "iron-chest"
    elif layout.get("output_belt") is None and inventory_count(observation, "transport-belt") <= 0:
        return "transport-belt"

    return None


def _available_coal_output_chest_name(observation: dict[str, Any]) -> str | None:
    return _available_stone_output_chest_name(observation)


def _find_relocatable_burner_drill_for_coal_supply(
    observation: dict[str, Any],
    target_position: dict[str, float],
) -> dict[str, Any] | None:
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    priority_by_resource = {"stone": 0}
    for drill in entities_named(observation, "burner-mining-drill"):
        if _entity_burner_fuel_count(drill) > 0:
            continue
        resource_name = _entity_resource_name(observation, drill, radius=4.5) or ""
        if resource_name == "coal":
            continue
        if resource_name and resource_name not in priority_by_resource:
            continue
        candidates.append(
            (
                priority_by_resource.get(resource_name, 3),
                distance(_position(drill), target_position),
                drill,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _coal_output_chest_near(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    return _stone_output_chest_near(observation, position)


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
    orientation = _direction_to_orientation(
        _direction_or_default(output_belt.get("direction"), _direction_or_default(supply.get("belt_direction"), EAST))
    )
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


def _coal_boiler_fuel_feed_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    boilers = [
        boiler
        for boiler in entities_named(observation, "boiler")
        if _within_allowed_factory_area(observation, _position(boiler))
        and (_entity_burner_fuel_count(boiler) < STEAM_POWER_BOILER_FUEL_RESERVE or not _boiler_has_belt_fuel_feed(observation, boiler))
    ]
    if not boilers:
        return None
    sources = _coal_supply_output_belt_sources(observation)
    if not sources:
        return None
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for boiler in boilers:
        boiler_position = _position(boiler)
        for source in sources:
            source_belt = source["belt"]
            source_position = _position(source_belt)
            route_distance = distance(source_position, boiler_position)
            if route_distance > STARTER_BOILER_FUEL_FEED_ROUTE_LIMIT:
                continue
            endpoints = _boiler_fuel_feed_endpoints(observation, source_position, boiler)
            segments = _coal_boiler_fuel_feed_route_segments(
                observation,
                source_belt,
                source.get("drill"),
                endpoints["target_belt"],
            )
            if not segments:
                continue
            route_score = _coal_boiler_fuel_feed_route_score(
                observation,
                segments,
                source_drill=source.get("drill"),
            )
            candidates.append(
                (
                    route_score,
                    route_distance,
                    {
                        "source_drill": source.get("drill"),
                        "source_belt": source_belt,
                        "boiler": boiler,
                        "segments": segments,
                        "target_belt_position": endpoints["target_belt"],
                        "target_inserter": {
                            "position": endpoints["target_inserter"],
                            "direction": endpoints["inserter_direction"],
                            "entity": _inserter_near(observation, endpoints["target_inserter"], radius=0.75),
                        },
                    },
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][2]


def _coal_boiler_fuel_feed_route_segments(
    observation: dict[str, Any],
    source_belt: dict[str, Any],
    source_drill: dict[str, Any] | None,
    target_belt: dict[str, float],
) -> list[dict[str, Any]]:
    source_position = _position(source_belt)
    candidates = _coal_boiler_existing_line_route_candidates(observation, source_belt, source_drill, target_belt)
    if not candidates:
        candidates = [_iron_plate_line_segments(observation, source_position, target_belt)]
    candidates = [candidate for candidate in candidates if candidate]
    if not candidates:
        return []
    return min(
        candidates,
        key=lambda segments: _coal_boiler_fuel_feed_route_score(
            observation,
            segments,
            source_drill=source_drill,
        ),
    )


def _coal_boiler_existing_line_route_candidates(
    observation: dict[str, Any],
    source_belt: dict[str, Any],
    source_drill: dict[str, Any] | None,
    target_belt: dict[str, float],
) -> list[list[dict[str, Any]]]:
    source_position = _position(source_belt)
    source_x = float(source_position["x"])
    source_y = float(source_position["y"])
    target_x = float(target_belt["x"])
    target_y = float(target_belt["y"])
    if abs(source_x - target_x) < 2.0:
        return []

    line_direction = WEST if target_x < source_x else EAST
    min_x = min(source_x, target_x) - 0.25
    max_x = max(source_x, target_x) + 0.25
    belts = entities_named(observation, "transport-belt")
    horizontal_lanes: dict[float, list[dict[str, Any]]] = {}
    for belt in belts:
        belt_position = _position(belt)
        belt_x = float(belt_position["x"])
        if belt_x < min_x or belt_x > max_x:
            continue
        if abs(float(belt_position["y"]) - source_y) > STARTER_BOILER_FUEL_FEED_ROUTE_LIMIT:
            continue
        if _direction_or_default(belt.get("direction"), line_direction) != line_direction:
            continue
        lane_y = round(float(belt_position["y"]), 3)
        horizontal_lanes.setdefault(lane_y, []).append(belt)

    candidates: list[list[dict[str, Any]]] = []
    ranked_lanes = sorted(horizontal_lanes.items(), key=lambda item: (-len(item[1]), abs(item[0] - source_y)))[:3]
    for lane_y, lane_belts in ranked_lanes:
        has_source_spur = _coal_boiler_lane_has_source_spur(
            observation,
            source_position,
            _direction_or_default(source_belt.get("direction"), EAST),
            lane_y,
        )
        if len(lane_belts) < 6 and not has_source_spur:
            continue
        join_belts = sorted(
            lane_belts,
            key=lambda belt: (
                abs(float(_position(belt)["x"]) - source_x),
                abs(float(_position(belt)["y"]) - source_y),
            ),
        )[:2]
        accepted_join_count = 0
        for join_belt in join_belts:
            join_position = _position(join_belt)
            join_x = float(join_position["x"])
            for detour_y in _coal_boiler_source_detour_y_candidates(source_y, lane_y)[:3]:
                if _coal_boiler_join_crosses_source_drill(join_x, detour_y, lane_y, source_drill):
                    continue
                waypoints = _coal_boiler_existing_line_waypoints(
                    source_position,
                    _direction_or_default(source_belt.get("direction"), EAST),
                    detour_y,
                    {"x": join_x, "y": lane_y},
                    {"x": target_x, "y": target_y},
                )
                segments = _iron_plate_segments_from_waypoints(observation, waypoints, center_tiles=False)
                if _existing_belt_segments_match_generated_directions(segments):
                    candidates.append(_preserve_existing_belt_segment_directions(segments))
                else:
                    candidates.append(segments)
                accepted_join_count += 1
                break
            if accepted_join_count >= 2:
                break
        if len(candidates) >= 3:
            break
    return candidates


def _coal_boiler_lane_has_source_spur(
    observation: dict[str, Any],
    source_position: dict[str, float],
    source_direction: int,
    lane_y: float,
) -> bool:
    source_x = float(source_position["x"])
    source_y = float(source_position["y"])
    if abs(source_y - float(lane_y)) < 0.01:
        return False
    if int(source_direction) == WEST:
        lead_x = source_x - 1.0
    elif int(source_direction) == EAST:
        lead_x = source_x + 1.0
    else:
        lead_x = source_x
    vertical_direction = SOUTH if float(lane_y) > source_y else NORTH
    for position, direction in _axis_route_positions(
        [{"x": lead_x, "y": source_y}, {"x": lead_x, "y": float(lane_y)}]
    ):
        belt = _entity_at_build_position(observation, "transport-belt", position, radius=0.75)
        if not isinstance(belt, dict):
            return False
        if _direction_or_default(belt.get("direction"), vertical_direction) != vertical_direction:
            return False
        if int(direction) != vertical_direction:
            return False
    return True


def _coal_boiler_join_crosses_source_drill(
    join_x: float,
    detour_y: float,
    lane_y: float,
    source_drill: dict[str, Any] | None,
) -> bool:
    if not isinstance(source_drill, dict):
        return False
    drill_position = _position(source_drill)
    if abs(float(join_x) - float(drill_position["x"])) >= 1.5:
        return False
    lower_y = min(float(detour_y), float(lane_y))
    upper_y = max(float(detour_y), float(lane_y))
    return lower_y <= float(drill_position["y"]) + 1.5 and upper_y >= float(drill_position["y"]) - 1.5


def _coal_boiler_source_detour_y_candidates(source_y: float, lane_y: float) -> list[float]:
    candidates: list[float] = []
    if abs(float(source_y) - float(lane_y)) < 0.01:
        candidates.append(round(float(lane_y), 3))
    for offset in (3.0, -3.0, 5.0, -5.0, 7.0, -7.0, 9.0, -9.0):
        candidates.append(round(float(source_y) + offset, 3))
    candidates.append(round(float(lane_y), 3))
    seen: set[float] = set()
    ordered: list[float] = []
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _coal_boiler_existing_line_waypoints(
    source_position: dict[str, float],
    source_direction: int,
    detour_y: float,
    join_position: dict[str, float],
    target_belt: dict[str, float],
) -> list[dict[str, float]]:
    source_x = float(source_position["x"])
    source_y = float(source_position["y"])
    direction = int(source_direction)
    if direction == WEST:
        lead = {"x": source_x - 1.0, "y": source_y}
    elif direction == NORTH:
        lead = {"x": source_x, "y": source_y - 1.0}
    elif direction == SOUTH:
        lead = {"x": source_x, "y": source_y + 1.0}
    else:
        lead = {"x": source_x + 1.0, "y": source_y}
    return [
        {"x": source_x, "y": source_y},
        lead,
        {"x": lead["x"], "y": detour_y},
        {"x": float(join_position["x"]), "y": detour_y},
        {"x": float(join_position["x"]), "y": float(join_position["y"])},
        {"x": float(target_belt["x"]), "y": float(join_position["y"])},
        {"x": float(target_belt["x"]), "y": float(target_belt["y"])},
    ]


def _preserve_existing_belt_segment_directions(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preserved: list[dict[str, Any]] = []
    for segment in segments:
        item = dict(segment)
        entity = item.get("entity")
        if isinstance(entity, dict):
            item["direction"] = _direction_or_default(entity.get("direction"), item["direction"])
        preserved.append(item)
    return preserved


def _existing_belt_segments_match_generated_directions(segments: list[dict[str, Any]]) -> bool:
    for segment in segments:
        entity = segment.get("entity")
        if not isinstance(entity, dict):
            continue
        if _direction_or_default(entity.get("direction"), segment["direction"]) != int(segment["direction"]):
            return False
    return True


def _coal_boiler_fuel_feed_route_score(
    observation: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    source_drill: dict[str, Any] | None = None,
) -> float:
    source_drill_unit = None
    if isinstance(source_drill, dict) and source_drill.get("unit_number") is not None:
        try:
            source_drill_unit = int(source_drill.get("unit_number"))
        except (TypeError, ValueError):
            source_drill_unit = None

    score = len(segments) / 100.0
    for segment in segments:
        entity = segment.get("entity")
        if isinstance(entity, dict):
            if _direction_or_default(entity.get("direction"), segment["direction"]) != int(segment["direction"]):
                score += 35.0
            else:
                score -= 0.25
            continue
        score += 10.0
        blocker = _belt_line_position_blocker(observation, segment["position"])
        if blocker is None:
            continue
        blocker_name = str(blocker.get("name") or "")
        try:
            blocker_unit = int(blocker.get("unit_number"))
        except (TypeError, ValueError):
            blocker_unit = None
        if source_drill_unit is not None and blocker_unit == source_drill_unit:
            score += 10000.0
        elif blocker_name in {"burner-mining-drill", "boiler", "steam-engine"} or blocker_name in ASSEMBLER_ENTITY_NAMES:
            score += 1000.0
        else:
            score += 150.0
    return score


def _coal_supply_output_belt_sources(observation: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for drill in entities_named(observation, "burner-mining-drill") + entities_named(observation, "electric-mining-drill"):
        drill_position = _position(drill)
        target_resource = _entity_resource_name(observation, drill, radius=4.5)
        if target_resource != "coal":
            continue
        direction = _direction_to_orientation(_direction_or_default(drill.get("direction"), EAST))
        layout = _coal_supply_layout_from_drill_position(drill_position, orientation=direction)
        layout["output_position"] = _burner_drill_output_position(drill)
        output_belt = _entity_at_build_position(
            observation,
            "transport-belt",
            layout["output_position"],
            radius=0.75,
        )
        if output_belt is None:
            continue
        if str(drill.get("name") or "") == "burner-mining-drill" and _entity_burner_fuel_count(drill) <= 0 and _entity_status_is(drill, "no_fuel", 53):
            continue
        if str(drill.get("name") or "") == "electric-mining-drill" and drill.get("electric_network_connected") is False:
            continue
        sources.append({"drill": drill, "belt": output_belt})
    return sources


def _boiler_fuel_feed_endpoints(
    observation: dict[str, Any],
    source_position: dict[str, float],
    boiler: dict[str, Any],
) -> dict[str, Any]:
    boiler_position = _position(boiler)
    side = _scalable_boiler_feed_side(observation, source_position, boiler)
    target_inserter = _offset_along_axis(boiler_position, side, 1.0)
    target_belt = _tile_center_position(_offset_along_axis(boiler_position, side, 2.0))
    return {
        "target_inserter": target_inserter,
        "target_belt": target_belt,
        "inserter_direction": _direction_from_axis_vector(side),
    }


def _scalable_boiler_feed_side(
    observation: dict[str, Any],
    source_position: dict[str, float],
    boiler: dict[str, Any],
) -> dict[str, float]:
    boiler_position = _position(boiler)
    source_side = _dominant_axis_vector(boiler_position, source_position)
    engine_side = _boiler_steam_engine_side(observation, boiler)
    preferred_bus_side = _opposite_axis_vector(engine_side) if engine_side is not None else None
    sides = [
        {"x": 1.0, "y": 0.0},
        {"x": -1.0, "y": 0.0},
        {"x": 0.0, "y": 1.0},
        {"x": 0.0, "y": -1.0},
    ]

    def score(side: dict[str, float]) -> tuple[float, float]:
        target_inserter = _offset_along_axis(boiler_position, side, 1.0)
        target_belt = _tile_center_position(_offset_along_axis(boiler_position, side, 2.0))
        value = distance(source_position, target_belt) * 0.01
        if _axis_vectors_equal(side, source_side):
            value -= 1.0
        if preferred_bus_side is not None and _axis_vectors_equal(side, preferred_bus_side):
            value -= 20.0
        if engine_side is not None and _axis_vectors_equal(side, engine_side):
            value += 50.0
        if _entity_at_build_position(observation, "transport-belt", target_belt, radius=0.75) is not None:
            value -= 100.0
        legacy_feed_belt = _entity_at_build_position(
            observation,
            "transport-belt",
            _tile_center_position(_offset_along_axis(boiler_position, side, 3.0)),
            radius=0.75,
        )
        if legacy_feed_belt is not None:
            value -= 150.0
        if _inserter_near(observation, target_inserter, radius=0.75) is not None:
            value -= 75.0
        blocker = _belt_line_position_blocker(observation, target_belt, protected_unit_numbers={int(boiler.get("unit_number") or -1)})
        if blocker is not None:
            value += 100.0
        return (value, distance(source_position, target_belt))

    return min(sides, key=score)


def _boiler_steam_engine_side(observation: dict[str, Any], boiler: dict[str, Any]) -> dict[str, float] | None:
    boiler_position = _position(boiler)
    engines = [
        engine
        for engine in entities_named(observation, "steam-engine")
        if distance(_position(engine), boiler_position) <= 8.0
    ]
    engine = _nearest_to(engines, boiler_position)
    if engine is None:
        return None
    return _dominant_axis_vector(boiler_position, _position(engine))


def _axis_vectors_equal(left: dict[str, float], right: dict[str, float]) -> bool:
    return (
        abs(float(left.get("x") or 0.0) - float(right.get("x") or 0.0)) < 0.01
        and abs(float(left.get("y") or 0.0) - float(right.get("y") or 0.0)) < 0.01
    )


def _boiler_has_belt_fuel_feed(observation: dict[str, Any], boiler: dict[str, Any]) -> bool:
    boiler_position = _position(boiler)
    inserters = [
        item
        for item in entities_named(observation, "burner-inserter")
        + entities_named(observation, "inserter")
        + entities_named(observation, "fast-inserter")
        if distance(_position(item), boiler_position) <= 3.0
    ]
    for inserter in inserters:
        if _entity_near(observation, "transport-belt", _position(inserter), radius=2.5) is not None:
            return True
    return False


def _nearest_boiler_feed_starter_belt_source(
    layout: dict[str, Any],
    target_position: dict[str, float],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for segment in layout.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        entity = segment.get("entity")
        if isinstance(entity, dict) and entity_item_count(entity, "coal") > 0:
            candidates.append(entity)
    source_belt = layout.get("source_belt")
    if isinstance(source_belt, dict) and distance(_position(source_belt), target_position) <= 12.0 and entity_item_count(source_belt, "coal") > 0:
        candidates.append(source_belt)
    return _nearest_to(candidates, target_position) if candidates else None


def _boiler_feed_route_has_coal_upstream(layout: dict[str, Any]) -> bool:
    source_belt = layout.get("source_belt")
    if isinstance(source_belt, dict) and entity_item_count(source_belt, "coal") > 0:
        return True
    for segment in layout.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        entity = segment.get("entity")
        if isinstance(entity, dict) and entity_item_count(entity, "coal") > 0:
            return True
    return False


def _available_boiler_feed_inserter_item(observation: dict[str, Any]) -> str | None:
    for item in ("inserter", "fast-inserter"):
        if inventory_count(observation, item) > 0:
            return item
    return None


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
    if entity_name in {"burner-inserter", "inserter", "fast-inserter"}:
        return "inserter"
    if entity_name in {"stone-furnace", "boiler"}:
        return "consumer"
    return entity_name


def _coal_fuel_feed_position_blocker(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    blocker = _blocking_obstacle_near(observation, position)
    if blocker is not None:
        return blocker
    for entity_name in ("wooden-chest", "iron-chest", "steel-chest"):
        for entity in entities_named(observation, entity_name):
            if distance(_position(entity), position) <= 0.45:
                return entity
    return None


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _line_missing_item(observation: dict[str, Any], layout: dict[str, Any]) -> str | None:
    missing_belts = sum(1 for key in ("belt1", "belt2") if layout.get(key) is None)
    if missing_belts > inventory_count(observation, "transport-belt"):
        return "transport-belt"
    for item, entity_name in [
        ("inserter", "inserter"),
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
                _entity_burner_fuel_count(layout[layout_key]) < threshold
                for _entity_name, layout_key, _item, threshold, _count in _smelting_line_fuel_requirements(layout, reserve=True)
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
            for _entity_name, layout_key, _item, _threshold, _count in _smelting_line_fuel_requirements(layout, reserve=True):
                entity = layout.get(layout_key)
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
        layout["inserter"] = _inserter_near(observation, layout["inserter_position"], radius=1.0)
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
    for _entity_name, layout_key, _item, minimum, _count in _smelting_line_fuel_requirements(layout, reserve=False):
        entity = layout.get(layout_key)
        if not isinstance(entity, dict) or _entity_burner_fuel_count(entity) < minimum:
            return False
    return True


def _smelting_line_fuel_requirements(
    layout: dict[str, Any],
    *,
    reserve: bool,
) -> list[tuple[str, str, str, int, int]]:
    requirements: list[tuple[str, str, str, int, int]] = []
    for entity_name, layout_key in [
        ("burner-mining-drill", "drill"),
        ("stone-furnace", "furnace"),
    ]:
        threshold = SMELTING_LINE_FUEL_RESERVE[layout_key] if reserve else 1
        insert_count = SMELTING_LINE_FUEL_INSERT[layout_key] if reserve else 1
        requirements.append((entity_name, layout_key, "coal", threshold, insert_count))
    inserter = layout.get("inserter")
    if isinstance(inserter, dict) and str(inserter.get("name") or "") == "burner-inserter":
        threshold = SMELTING_LINE_FUEL_RESERVE["inserter"] if reserve else 1
        insert_count = SMELTING_LINE_FUEL_INSERT["inserter"] if reserve else 1
        requirements.insert(1, ("burner-inserter", "inserter", "coal", threshold, insert_count))
    return requirements


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
    wait_for_existing_fuel: bool = False,
    prefer_coal_supply: bool = True,
    allow_bootstrap_seed: bool = False,
) -> PlannerDecision:
    position = _position(entity)
    current_fuel = _entity_burner_fuel_count(entity)
    desired_insert = min(insert_count, max(1, threshold - current_fuel))
    existing_fuel_item = _entity_existing_burner_fuel_item(entity)
    if existing_fuel_item is not None:
        inventory_fuel_item = existing_fuel_item
        inventory_fuel_count = inventory_count(observation, existing_fuel_item)
        if inventory_fuel_count <= 0 and current_fuel > 0:
            if not wait_for_existing_fuel:
                return PlannerDecision(None, far_fuel_reason)
            excluded_units = set(exclude_source_units or set())
            excluded_units.add(entity.get("unit_number"))
            matching_source = _nearest_surplus_fuel_source_with_item(
                observation,
                position,
                existing_fuel_item,
                exclude_units=excluded_units,
            )
            if matching_source is not None:
                return _take_surplus_fuel_source_decision(player, matching_source, context)
            if (
                prefer_coal_supply
                and existing_fuel_item == "coal"
                and _established_coal_supply_output_exists(observation)
            ):
                belt_source = _nearest_fuel_belt_source(observation, position, fuel_item=existing_fuel_item)
                if belt_source is not None:
                    return _take_surplus_fuel_source_decision(player, belt_source, context)
                if allow_bootstrap_seed:
                    seed = _bootstrap_fuel_seed_decision(observation, player, position, context, support_skill, desired_insert)
                    if seed is not None:
                        return seed
                return PlannerDecision(
                    {"type": "wait", "ticks": 240},
                    f"wait for established coal supply output before refueling {context}; refusing repeated hand-mining",
                )
            if existing_fuel_item == "coal" and entity_name == "burner-mining-drill":
                coal = _nearest_resource_to_position(observation, position, "coal")
                if coal is not None and distance(position, _position(coal)) <= WALK_FUEL_LOGISTICS_LIMIT:
                    return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)
            if existing_fuel_item == "coal":
                coal = _nearest_resource_to_position(observation, position, "coal")
                if coal is not None and distance(position, _position(coal)) <= WALK_FUEL_LOGISTICS_LIMIT:
                    return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)
            return PlannerDecision(
                {"type": "wait", "ticks": 180},
                f"wait for existing {existing_fuel_item} in {entity_name} before mixing burner fuel for {context}",
            )
    else:
        inventory_fuel_item, inventory_fuel_count = _select_inventory_burner_fuel(observation)
    if inventory_fuel_count <= 0:
        if prefer_coal_supply and _coal_supply_can_reduce_hand_mining(observation):
            supply = CoalSupplySkill(target_count=max(16, desired_insert)).next_action(observation)
            if supply.action is not None:
                return supply
        coal = _nearest_resource_to_position(observation, position, "coal")
        excluded_units = set(exclude_source_units or set())
        excluded_units.add(entity.get("unit_number"))
        source = _nearest_surplus_fuel_source(observation, position, exclude_units=excluded_units)
        source_surplus = _surplus_fuel_count(source) if source is not None else 0
        if source is not None and _surplus_fuel_source_is_logistic_output(source, observation):
            return _take_surplus_fuel_source_decision(player, source, context)
        if (
            prefer_coal_supply
            and _established_coal_supply_output_exists(observation)
        ):
            if source is not None:
                return _take_surplus_fuel_source_decision(player, source, context)
            belt_source = _nearest_fuel_belt_source(observation, position, fuel_item="coal")
            if belt_source is not None:
                return _take_surplus_fuel_source_decision(player, belt_source, context)
            if allow_bootstrap_seed:
                seed = _bootstrap_fuel_seed_decision(observation, player, position, context, support_skill, desired_insert)
                if seed is not None:
                    return seed
            return PlannerDecision(
                {"type": "wait", "ticks": 240},
                f"wait for established coal supply output before refueling {context}; refusing repeated hand-mining",
            )
        if coal is not None and distance(position, _position(coal)) <= WALK_FUEL_LOGISTICS_LIMIT and source_surplus < 8:
            return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)
        local_coal = _nearest_resource_to_position(observation, player, "coal")
        if (
            local_coal is not None
            and distance(player, _position(local_coal)) <= 16.0
            and distance(player, position) > 20.0
            and source_surplus < max(8, desired_insert)
        ):
            return support_skill._mine_resource(player, local_coal, "coal", max(STARTER_FUEL_BATCH_COUNT, desired_insert))
        if source is not None:
            return _take_surplus_fuel_source_decision(player, source, context)
        if coal is None:
            return PlannerDecision(None, f"cannot find burner fuel for {context}")
        if distance(position, _position(coal)) > WALK_FUEL_LOGISTICS_LIMIT:
            return PlannerDecision(None, far_fuel_reason)
        return support_skill._mine_resource(player, coal, "coal", STARTER_FUEL_BATCH_COUNT)

    if inventory_fuel_item == "coal" and inventory_fuel_count < desired_insert and distance(player, position) > 20.0:
        local_coal = _nearest_resource_to_position(observation, player, "coal")
        if local_coal is not None and distance(player, _position(local_coal)) <= 16.0:
            return support_skill._mine_resource(
                player,
                local_coal,
                "coal",
                max(STARTER_FUEL_BATCH_COUNT, desired_insert - inventory_fuel_count),
            )

    if distance(player, position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": position},
            f"move near {entity_name} to fuel {context}",
        )
    return PlannerDecision(
        {
            "type": "insert",
            "item": inventory_fuel_item,
            "count": min(insert_count, inventory_fuel_count, desired_insert),
            "unit_number": entity.get("unit_number"),
            "name": entity_name,
            "position": position,
        },
        f"fuel {entity_name} in {context}",
    )


def _bootstrap_fuel_seed_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    target_position: dict[str, float],
    context: str,
    support_skill: IronPlateSkill,
    desired_insert: int,
) -> PlannerDecision | None:
    if not _is_virtual_agent(observation):
        return None
    coal = _nearest_resource_to_position(observation, target_position, "coal")
    if coal is None:
        return None
    if distance(target_position, _position(coal)) > WALK_FUEL_LOGISTICS_LIMIT:
        return None
    decision = support_skill._mine_resource(player, coal, "coal", max(4, desired_insert))
    if decision.action is None:
        return None
    return _bootstrap_seed_decision(
        decision.action,
        f"{decision.reason} for one-time bootstrap fuel seed for {context}",
        seed_reason="gear_mall_source_fuel_seed",
        expected_followup=f"insert coal into {context} and verify automated output increases",
    )


def _nearest_fuel_belt_source(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    fuel_item: str | None = None,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for belt in entities_named(observation, "transport-belt"):
        if fuel_item:
            source_count = entity_item_count(belt, fuel_item)
            if source_count <= 0:
                continue
        else:
            _source_item, source_count = _select_surplus_fuel_item(belt)
            if source_count <= 0:
                continue
        candidates.append((distance(target_position, _position(belt)), -source_count, belt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _coal_supply_can_reduce_hand_mining(observation: dict[str, Any]) -> bool:
    return _find_coal_supply_layout(observation) is not None or _select_coal_supply_layout(observation) is not None


def _established_coal_supply_output_exists(observation: dict[str, Any]) -> bool:
    layout = _find_coal_supply_layout(observation)
    return bool(layout and (layout.get("output_belt") is not None or layout.get("output_chest") is not None))


def _surplus_fuel_source_is_logistic_output(entity: dict[str, Any], observation: dict[str, Any] | None = None) -> bool:
    name = str(entity.get("name") or "")
    if name in {"wooden-chest", "iron-chest", "steel-chest"}:
        return True
    if observation is not None and name == "burner-mining-drill":
        return _entity_resource_name(observation, entity, radius=4.5) == "coal"
    return False


def _take_surplus_fuel_source_decision(
    player: dict[str, float],
    source: dict[str, Any],
    context: str,
) -> PlannerDecision:
    source_position = _position(source)
    if distance(player, source_position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": source_position},
            f"move near surplus fuel source for {context}",
        )
    source_item, source_count = _select_surplus_fuel_item(source)
    take_count = min(STARTER_FUEL_BATCH_COUNT, source_count)
    source_label = "supply belt" if str(source.get("name") or "") == "transport-belt" else str(source.get("name") or "")
    return PlannerDecision(
        {
            "type": "take",
            "item": source_item,
            "count": max(1, take_count),
            "unit_number": source.get("unit_number"),
            "name": source.get("name"),
            "position": source_position,
        },
        f"recover surplus {source_item} from {source_label} for {context}",
    )


def _emergency_boiler_bootstrap_fuel_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    boiler: dict[str, Any],
    blocked_feed_decision: PlannerDecision,
    *,
    allow_without_critical_factory: bool = False,
) -> PlannerDecision | None:
    if blocked_feed_decision.action is not None or blocked_feed_decision.done:
        return None
    if _entity_burner_fuel_count(boiler) > 0:
        return None
    if not allow_without_critical_factory and not _critical_electric_factory_present_for_planner(observation):
        return None
    reason = str(blocked_feed_decision.reason or "")
    if not any(token in reason for token in ("needs automated transport-belt production", "needs transport belts", "missing burner inserter")):
        return None

    boiler_position = _position(boiler)
    fuel_item, fuel_count = _select_inventory_burner_fuel(observation)
    if fuel_count > 0:
        if distance(player, boiler_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": boiler_position},
                "move near boiler for one-time emergency power bootstrap fuel insert",
            )
        return PlannerDecision(
            {
                "type": "insert",
                "item": fuel_item,
                "count": min(EMERGENCY_BOILER_BOOTSTRAP_FUEL_INSERT, fuel_count),
                "unit_number": boiler.get("unit_number"),
                "name": "boiler",
                "position": boiler_position,
                "emergency_bootstrap": True,
            },
            "one-time emergency boiler fuel bootstrap to restore power for belt automation; do not use as repeated fuel logistics",
        )

    source = _nearest_surplus_fuel_source(
        observation,
        boiler_position,
        exclude_units={boiler.get("unit_number")},
    )
    if source is None and allow_without_critical_factory:
        source = _nearest_bootstrap_fuel_source(
            observation,
            boiler_position,
            exclude_units={boiler.get("unit_number")},
        )
    if source is None:
        return None
    source_position = _position(source)
    if distance(boiler_position, source_position) > STARTER_BOILER_FUEL_FEED_ROUTE_LIMIT:
        return None
    if distance(player, source_position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": source_position},
            "move near surplus fuel source for one-time emergency power bootstrap",
        )
    if allow_without_critical_factory:
        source_item, source_count = _select_bootstrap_fuel_item(source)
    else:
        source_item, source_count = _select_surplus_fuel_item(source)
    if source_count <= 0:
        return None
    return PlannerDecision(
        {
            "type": "take",
            "item": source_item,
            "count": min(EMERGENCY_BOILER_BOOTSTRAP_FUEL_INSERT, source_count),
            "unit_number": source.get("unit_number"),
            "name": source.get("name"),
            "position": source_position,
            "emergency_bootstrap": True,
        },
        "take surplus fuel from existing fuel source for one-time emergency boiler bootstrap",
    )


def _critical_electric_factory_present_for_planner(observation: dict[str, Any]) -> bool:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    critical_recipes = {
        "automation-science-pack",
        "logistic-science-pack",
        "chemical-science-pack",
        "copper-cable",
        "electronic-circuit",
        "iron-gear-wheel",
        "transport-belt",
        "small-electric-pole",
        "assembling-machine-1",
        "long-handed-inserter",
        "electric-mining-drill",
    }
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name == "lab":
            return True
        recipe = str(entity.get("recipe") or entity.get("recipe_name") or "")
        if name in ASSEMBLER_ENTITY_NAMES and recipe in critical_recipes:
            return True
    return False


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
    for entity_name in ("wooden-chest", "iron-chest", "steel-chest", "stone-furnace", "burner-mining-drill", "burner-inserter", "boiler"):
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


def _nearest_bootstrap_fuel_source(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    exclude_units: set[Any] | None = None,
) -> dict[str, Any] | None:
    excluded = set(exclude_units or set())
    candidates = []
    for entity_name in ("wooden-chest", "iron-chest", "steel-chest", "burner-mining-drill", "burner-inserter", "stone-furnace"):
        for entity in entities_named(observation, entity_name):
            if entity.get("unit_number") in excluded:
                continue
            _item, usable = _select_bootstrap_fuel_item(entity)
            if usable <= 0:
                continue
            entity_position = _position(entity)
            candidates.append((distance(target_position, entity_position), -usable, entity))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _nearest_surplus_fuel_source_with_item(
    observation: dict[str, Any],
    target_position: dict[str, float],
    fuel_item: str,
    *,
    exclude_units: set[Any] | None = None,
) -> dict[str, Any] | None:
    excluded = set(exclude_units or set())
    candidates = []
    for entity_name in ("wooden-chest", "iron-chest", "steel-chest", "stone-furnace", "burner-mining-drill", "burner-inserter", "boiler"):
        for entity in entities_named(observation, entity_name):
            if entity.get("unit_number") in excluded:
                continue
            source_item, source_count = _select_surplus_fuel_item(entity)
            if source_item != fuel_item or source_count <= 0:
                continue
            entity_position = _position(entity)
            candidates.append((distance(target_position, entity_position), -source_count, entity))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _surplus_fuel_count(entity: dict[str, Any]) -> int:
    fuel = _entity_burner_fuel_count(entity)
    reserve = _fuel_reserve_for_entity(str(entity.get("name") or ""))
    return max(0, fuel - reserve)


def _select_surplus_fuel_item(entity: dict[str, Any]) -> tuple[str, int]:
    reserve = _fuel_reserve_for_entity(str(entity.get("name") or ""))
    for item in BURNER_FUEL_ITEMS:
        count = entity_item_count(entity, item)
        if count <= 0:
            continue
        if reserve >= count:
            reserve -= count
            continue
        return item, count - reserve
    return "coal", 0


def _select_bootstrap_fuel_item(entity: dict[str, Any]) -> tuple[str, int]:
    reserve = 1 if str(entity.get("name") or "") in {"burner-mining-drill", "burner-inserter", "stone-furnace"} else 0
    for item in BURNER_FUEL_ITEMS:
        count = entity_item_count(entity, item)
        if count <= reserve:
            continue
        return item, count - reserve
    return "coal", 0


def _select_inventory_burner_fuel(observation: dict[str, Any]) -> tuple[str, int]:
    for item in BURNER_FUEL_ITEMS:
        count = inventory_count(observation, item)
        if count > 0:
            return item, count
    return "coal", 0


def _inventory_burner_fuel_count(observation: dict[str, Any]) -> int:
    return sum(inventory_count(observation, item) for item in BURNER_FUEL_ITEMS)


def _entity_burner_fuel_count(entity: dict[str, Any]) -> int:
    stored_fuel = sum(entity_item_count(entity, item) for item in BURNER_FUEL_ITEMS)
    burner = entity.get("burner") if isinstance(entity.get("burner"), dict) else {}
    if _burner_remaining_fuel(burner) > 0:
        return max(1, stored_fuel)
    return stored_fuel


def _entity_existing_burner_fuel_item(entity: dict[str, Any]) -> str | None:
    for item in BURNER_FUEL_ITEMS:
        if entity_item_count(entity, item) > 0:
            return item
    burner = entity.get("burner") if isinstance(entity.get("burner"), dict) else {}
    currently_burning = str(burner.get("currently_burning") or "")
    if currently_burning in BURNER_FUEL_ITEMS and _burner_remaining_fuel(burner) > 0:
        return currently_burning
    return None


def _burner_remaining_fuel(burner: dict[str, Any]) -> float:
    try:
        return float(burner.get("remaining_burning_fuel") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    if entity_name in {"burner-inserter", "inserter", "fast-inserter"}:
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


def _entity_at_build_position(
    observation: dict[str, Any],
    name: str,
    position: dict[str, float],
    radius: float = 0.25,
) -> dict[str, Any] | None:
    probe_positions = [position]
    try:
        x = float(position["x"])
        y = float(position["y"])
    except (KeyError, TypeError, ValueError):
        x = 0.0
        y = 0.0
    if abs(x - round(x)) < 0.001 and abs(y - round(y)) < 0.001:
        probe_positions.append({"x": x + 0.5, "y": y + 0.5})
    candidates: list[dict[str, Any]] = []
    for entity in entities_named(observation, name):
        entity_position = _position(entity)
        if any(distance(entity_position, probe) <= radius for probe in probe_positions):
            candidates.append(entity)
    return _nearest_to(candidates, position) if candidates else None


def _assembler_at_position(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for name in ASSEMBLER_ENTITY_NAMES:
        entity = _entity_at_build_position(observation, name, position, radius=0.75)
        if entity is not None:
            candidates.append(entity)
    return _nearest_to(candidates, position) if candidates else None


def _build_position_blocker(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    allowed_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    allowed = set(allowed_names or set())
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        if name in allowed and distance(_position(entity), position) <= 0.75:
            continue
        if name in ASSEMBLER_ENTITY_NAMES | {"lab", "stone-furnace", "burner-mining-drill", "boiler", "steam-engine"}:
            if _point_inside_machine(position, entity):
                return entity
            continue
        if distance(_position(entity), position) <= 0.55:
            return entity
    return None


def _machine_position_natural_blocker(
    observation: dict[str, Any],
    position: dict[str, float],
) -> dict[str, Any] | None:
    blockers: list[dict[str, Any]] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        if _is_preserved_starter_artifact(observation, entity):
            continue
        entity_type = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if entity_type == "tree" and distance(_position(entity), position) <= 2.4:
            blockers.append(entity)
            continue
        if (entity_type in {"simple-entity", "cliff"} or name.endswith("rock")) and distance(_position(entity), position) <= 2.4:
            blockers.append(entity)
    return _nearest_to(blockers, position) if blockers else None


def _entity_status_is(entity: dict[str, Any], status_name: str, status_code: int) -> bool:
    if str(entity.get("status_name") or "") == status_name:
        return True
    try:
        return int(entity.get("status")) == status_code
    except (TypeError, ValueError):
        return False


def _entity_no_power(entity: dict[str, Any]) -> bool:
    return _entity_status_is(entity, "no_power", 3) or _entity_status_is(entity, "no_power", 54)


def _entity_status_name_in(entity: dict[str, Any], names: set[str]) -> bool:
    return str(entity.get("status_name") or "") in names


def _find_iron_plate_logistic_line_to_gear_mall_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    gear_layout = _find_gear_belt_mall_logistics_layout(observation)
    if gear_layout is None:
        return None
    gear_assembler = gear_layout["gear_assembler"]
    gear_position = _position(gear_assembler)
    sources = _iron_plate_source_furnaces(observation)
    if not sources:
        return None
    source = min(sources, key=lambda item: distance(_position(item), gear_position))
    source_position = _position(source)
    source_belt_position = _tile_center_position({"x": source_position["x"] + 2.0, "y": source_position["y"]})
    source_inserter_position = _tile_center_position({"x": source_position["x"] + 1.0, "y": source_position["y"]})
    avoid_positions = _gear_mall_output_avoid_positions(gear_layout)
    endpoints = _gear_mall_iron_input_endpoints(
        observation,
        source_belt_position,
        gear_assembler,
        avoid_positions=avoid_positions,
    )
    target_belt_position = endpoints["target_belt"]
    target_inserter_position = endpoints["target_inserter"]
    segments = _iron_plate_line_segments(
        observation,
        source_belt_position,
        target_belt_position,
        center_tiles=True,
        avoid_positions=avoid_positions,
    )
    return {
        "source": source,
        "gear_assembler": gear_assembler,
        "belt_assembler": gear_layout.get("belt_assembler"),
        "segments": segments,
        "source_belt": source_belt_position,
        "target_belt": target_belt_position,
        "source_inserter": {
            "position": source_inserter_position,
            "direction": WEST,
            "entity": _inserter_near(observation, source_inserter_position),
        },
        "target_inserter": {
            "position": target_inserter_position,
            "direction": endpoints["target_direction"],
            "entity": _inserter_near(observation, target_inserter_position),
        },
    }


def _gear_mall_output_avoid_positions(gear_layout: dict[str, Any]) -> set[tuple[float, float]]:
    positions: set[tuple[float, float]] = set()
    for belt in gear_layout.get("gear_belts") or []:
        if isinstance(belt, dict) and isinstance(belt.get("position"), dict):
            positions.add(_position_tuple(belt["position"]))
    for key in ("gear_output_inserter", "belt_input_inserter", "direct_gear_transfer_inserter"):
        spec = gear_layout.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("position"), dict):
            positions.add(_position_tuple(spec["position"]))
    return positions


def _gear_mall_iron_input_endpoints(
    observation: dict[str, Any],
    source_belt_position: dict[str, float],
    gear_assembler: dict[str, Any],
    *,
    avoid_positions: set[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    gear_position = _position(gear_assembler)
    avoid = set(avoid_positions or set())
    candidates = [
        {
            "target_inserter": _tile_center_position({"x": gear_position["x"] + 1.0, "y": gear_position["y"] - 2.0}),
            "target_belt": _tile_center_position({"x": gear_position["x"] + 1.0, "y": gear_position["y"] - 3.0}),
            "target_direction": NORTH,
        },
        {
            "target_inserter": _tile_center_position({"x": gear_position["x"] + 1.0, "y": gear_position["y"] + 2.0}),
            "target_belt": _tile_center_position({"x": gear_position["x"] + 1.0, "y": gear_position["y"] + 3.0}),
            "target_direction": SOUTH,
        },
        {
            "target_inserter": _tile_center_position({"x": gear_position["x"] - 2.0, "y": gear_position["y"]}),
            "target_belt": _tile_center_position({"x": gear_position["x"] - 3.0, "y": gear_position["y"]}),
            "target_direction": WEST,
        },
        {
            "target_inserter": _tile_center_position({"x": gear_position["x"] + 2.0, "y": gear_position["y"]}),
            "target_belt": _tile_center_position({"x": gear_position["x"] + 3.0, "y": gear_position["y"]}),
            "target_direction": EAST,
        },
    ]
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        penalty = 0.0
        inserter = _inserter_near(observation, candidate["target_inserter"], radius=0.75)
        if isinstance(inserter, dict):
            if _direction_or_default(inserter.get("direction"), -1) != int(candidate["target_direction"]):
                penalty += 100.0
            else:
                penalty -= 1000.0
        belt = _entity_at_build_position(observation, "transport-belt", candidate["target_belt"], radius=0.75)
        if isinstance(belt, dict):
            penalty += 25.0
        for point in (candidate["target_inserter"], candidate["target_belt"]):
            if _position_tuple(point) in avoid:
                penalty += 2500.0
            blocker = _belt_line_position_blocker(observation, point)
            if blocker is not None and str(blocker.get("name") or "") != "transport-belt":
                penalty += 50.0
        segments = _iron_plate_line_segments(
            observation,
            source_belt_position,
            candidate["target_belt"],
            center_tiles=True,
            avoid_positions=avoid,
        )
        penalty += _iron_plate_line_route_score(observation, segments, avoid_positions=avoid)
        scored.append((penalty, candidate))
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def _find_site_input_logistic_line_layout(
    observation: dict[str, Any],
    *,
    item: str | None = None,
) -> dict[str, Any] | None:
    target_items = {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }
    if item:
        target_items &= {item}
    if not target_items:
        return None

    links = [link.to_dict() for link in estimate_logistics_links(observation)]
    link_priority: dict[tuple[str, str], int] = {}
    for link in links:
        if not isinstance(link, dict) or link.get("status") not in {"route_needed", "missing_source"}:
            continue
        link_item = str(link.get("item") or "")
        if link_item not in target_items:
            continue
        link_priority[(link_item, str(link.get("to_site") or ""))] = max(
            link_priority.get((link_item, str(link.get("to_site") or "")), 0),
            12 if link.get("status") == "route_needed" else 8,
        )

    candidates: list[dict[str, Any]] = []
    for consumer in _site_input_consumer_entities(observation, target_items):
        consumer_position = _position(consumer)
        recipe = str(consumer.get("recipe") or consumer.get("recipe_name") or "")
        recipe_obj = RECIPES.get(recipe)
        if recipe_obj is None:
            continue
        for required_item in sorted(target_items & set(recipe_obj.ingredients)):
            if entity_item_count(consumer, required_item) > 0 and not _entity_status_name_in(
                consumer,
                {"item_ingredient_shortage", "missing_required_fluid"},
            ):
                continue
            source = _nearest_site_input_source(observation, required_item, consumer_position)
            if source is None:
                continue
            source_position = _position(source)
            if distance(source_position, consumer_position) < 6.0:
                continue
            if _site_input_local_route_observed(observation, required_item, source, consumer):
                continue
            site_id = _consumer_site_id_for_entity(observation, consumer)
            protected_units = {
                int(entity.get("unit_number"))
                for entity in (source, consumer)
                if isinstance(entity, dict) and entity.get("unit_number") is not None
            }
            endpoint_candidates = _site_input_line_endpoint_candidates(source, consumer)
            if _logistics_researched_or_underground_unlocked(observation):
                endpoint_candidates = endpoint_candidates[:1]
            for endpoints in endpoint_candidates:
                source_inserter_position = _tile_center_position(endpoints["source_inserter"])
                target_inserter_position = _tile_center_position(endpoints["target_inserter"])
                endpoint_hard_blockers = _site_input_endpoint_hard_blockers(
                    observation,
                    endpoints,
                    source_inserter_position,
                    target_inserter_position,
                    protected_unit_numbers=protected_units,
                )
                if endpoint_hard_blockers:
                    continue
                segments = _site_input_line_segments(
                    observation,
                    endpoints,
                    avoid_positions={
                        _position_tuple(source_inserter_position),
                        _position_tuple(target_inserter_position),
                    },
                    protected_unit_numbers=protected_units,
                )
                if _site_input_hard_route_blockers(observation, segments, protected_unit_numbers=protected_units):
                    continue
                if (
                    not _logistics_researched_or_underground_unlocked(observation)
                    and _site_input_transport_belt_conflicts(segments)
                    and not _site_input_route_repairable_with_conflicts(segments)
                ):
                    continue
                fanout_consumer_count = _site_input_source_fanout_consumer_count(
                    observation,
                    required_item,
                    source,
                )
                splitter = None
                if _site_input_splitter_fanout_required(observation, fanout_consumer_count):
                    splitter = _site_input_splitter_fanout_plan(
                        observation,
                        segments,
                        direction=int(endpoints["source_belt_direction"]),
                    )
                    if splitter is None:
                        continue
                route_penalty = _site_input_line_route_score(
                    observation,
                    segments,
                    avoid_positions={
                        _position_tuple(source_inserter_position),
                        _position_tuple(target_inserter_position),
                    },
                    protected_unit_numbers=protected_units,
                )
                score = (
                    _site_input_item_priority(required_item)
                    + link_priority.get((required_item, site_id), 0)
                    + min(20.0, distance(source_position, consumer_position) / 8.0)
                    - float(endpoints.get("preference_penalty") or 0.0)
                    - route_penalty
                )
                candidates.append(
                    {
                        "item": required_item,
                        "source": source,
                        "consumer": consumer,
                        "consumer_recipe": recipe,
                        "consumer_site_id": site_id,
                        "distance": round(distance(source_position, consumer_position), 1),
                        "segments": segments,
                        "source_inserter": {
                            "position": source_inserter_position,
                            "direction": endpoints["source_direction"],
                            "entity": _inserter_near(observation, source_inserter_position),
                        },
                        "target_inserter": {
                            "position": target_inserter_position,
                            "direction": endpoints["target_direction"],
                            "entity": _inserter_near(observation, target_inserter_position),
                        },
                        "fanout_consumer_count": fanout_consumer_count,
                        "splitter": splitter,
                        "score": score,
                    }
                )
    if not candidates:
        return None
    candidates.sort(key=lambda row: (float(row.get("score") or 0.0), -float(row.get("distance") or 0.0)), reverse=True)
    return candidates[0]


def _site_input_consumer_entities(observation: dict[str, Any], target_items: set[str]) -> list[dict[str, Any]]:
    consumers: list[dict[str, Any]] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("name") or "") not in ASSEMBLER_ENTITY_NAMES:
            continue
        if entity.get("electric_network_connected") is False:
            continue
        recipe_name = str(entity.get("recipe") or entity.get("recipe_name") or "")
        recipe = RECIPES.get(recipe_name)
        if recipe is None or not (set(recipe.ingredients) & target_items):
            continue
        if recipe_name == "transport-belt":
            continue
        consumers.append(entity)
    return consumers


def _nearest_site_input_source(
    observation: dict[str, Any],
    item: str,
    consumer_position: dict[str, float],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        if not _entity_can_supply_site_input_item(entity, item):
            continue
        if str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES and entity.get("electric_network_connected") is False:
            continue
        candidates.append(entity)
    return _nearest_to(candidates, consumer_position)


def _entity_can_supply_site_input_item(entity: dict[str, Any], item: str) -> bool:
    if _entity_produces_site_input_item(entity, item):
        return True
    name = str(entity.get("name") or "")
    if name in {"wooden-chest", "iron-chest", "steel-chest"} and entity_item_count(entity, item) > 0:
        return True
    return False


def _entity_produces_site_input_item(entity: dict[str, Any], item: str) -> bool:
    name = str(entity.get("name") or "")
    recipe_name = str(entity.get("recipe") or entity.get("recipe_name") or "")
    if recipe_name == item:
        return True
    if item in {"iron-plate", "copper-plate"} and name in {"stone-furnace", "steel-furnace", "electric-furnace"}:
        return recipe_name == item
    recipe = RECIPES.get(recipe_name)
    return bool(recipe and item in recipe.products)


def _site_input_local_route_observed(
    observation: dict[str, Any],
    item: str,
    source: dict[str, Any],
    consumer: dict[str, Any],
) -> bool:
    endpoints = _site_input_line_endpoints(source, consumer)
    target_belt = _tile_center_position(endpoints["target_belt"])
    target_inserter = _tile_center_position(endpoints["target_inserter"])
    if not _entity_at_build_position(observation, "transport-belt", target_belt, radius=0.75):
        return False
    inserter = _inserter_near(observation, target_inserter)
    if not isinstance(inserter, dict):
        return False
    return _direction_or_default(inserter.get("direction"), endpoints["target_direction"]) == int(endpoints["target_direction"])


def _logistics_researched_or_underground_unlocked(observation: dict[str, Any]) -> bool:
    return bool(_technology_state(observation, "logistics").get("researched")) or _recipe_unlocked_for_layout(
        observation,
        "underground-belt",
    )


def _site_input_underground_bridge_plan(observation: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any] | None:
    segments = layout.get("segments") if isinstance(layout.get("segments"), list) else []
    if len(segments) < 3:
        return None
    for index in range(1, len(segments) - 1):
        segment = segments[index]
        existing = segment.get("entity") if isinstance(segment, dict) else None
        if not isinstance(existing, dict) or str(existing.get("name") or "") != "transport-belt":
            continue
        planned_direction = _direction_or_default(segment.get("direction"), EAST)
        existing_direction = _direction_or_default(existing.get("direction"), planned_direction)
        if _direction_axis(existing_direction) == _direction_axis(planned_direction):
            continue
        before = segments[index - 1]
        after = segments[index + 1]
        if _direction_or_default(before.get("direction"), planned_direction) != planned_direction:
            continue
        if _direction_or_default(after.get("direction"), planned_direction) != planned_direction:
            continue
        return {
            "crossing": segment,
            "entry": before,
            "exit": after,
            "direction": planned_direction,
            "existing_cross_direction": existing_direction,
        }
    return None


def _underground_belt_entity_at(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    return _entity_at_build_position(observation, "underground-belt", position, radius=0.75)


def _underground_belt_matches(entity: dict[str, Any], direction: int, underground_type: str) -> bool:
    return (
        str(entity.get("name") or "") == "underground-belt"
        and _direction_or_default(entity.get("direction"), direction) == int(direction)
        and str(entity.get("belt_to_ground_type") or "") == underground_type
    )


def _site_input_underground_bridge_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    bridge: dict[str, Any],
) -> PlannerDecision | None:
    if not _logistics_researched_or_underground_unlocked(observation):
        return PlannerDecision(
            None,
            "site input logistics crossing needs underground-belt bridge after logistics research; refusing to mine the crossing belt",
        )
    endpoints = [
        ("entrance", bridge["entry"], "input"),
        ("exit", bridge["exit"], "output"),
    ]
    required = 0
    for _label, segment, underground_type in endpoints:
        underground = _underground_belt_entity_at(observation, segment["position"])
        if isinstance(underground, dict) and _underground_belt_matches(underground, bridge["direction"], underground_type):
            continue
        required += 1
    if required > inventory_count(observation, "underground-belt"):
        if craftable_count(observation, "underground-belt") > 0:
            return PlannerDecision(
                {"type": "craft", "recipe": "underground-belt", "count": 1},
                "craft underground belts for crossing site input logistics bridge",
            )
        decision = BuildItemMallSkill("underground-belt", 2).next_action(observation, reference_position=bridge["entry"]["position"])
        if not decision.done:
            return decision
        return PlannerDecision(None, "site input logistics crossing needs two underground belts")

    for label, segment, underground_type in endpoints:
        position = segment["position"]
        underground = _underground_belt_entity_at(observation, position)
        if isinstance(underground, dict):
            if _underground_belt_matches(underground, bridge["direction"], underground_type):
                continue
            underground_position = _position(underground)
            if distance(player, underground_position) > 4.5:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(underground_position, offset=1.5)},
                    f"move within reach of wrong underground-belt {label} for site input bridge",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": underground.get("unit_number"),
                    "name": "underground-belt",
                    "position": underground_position,
                },
                f"remove wrong underground-belt {label} before rebuilding site input bridge",
            )
        existing = segment.get("entity") if isinstance(segment, dict) else None
        if isinstance(existing, dict):
            existing_position = _position(existing)
            if distance(player, existing_position) > 4.5:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(existing_position, offset=1.5)},
                    f"move within reach of belt segment to convert it into underground bridge {label}",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": existing.get("unit_number"),
                    "name": existing.get("name") or "transport-belt",
                    "position": existing_position,
                },
                f"replace regular site input belt with underground bridge {label}",
            )
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                f"move near underground-belt bridge {label} position",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "underground-belt",
                "position": position,
                "direction": bridge["direction"],
                "underground_type": underground_type,
                "allow_nearby": False,
            },
            f"build underground-belt bridge {label} so site input logistics crosses another belt line",
        )
    return None


def _site_input_line_segments(
    observation: dict[str, Any],
    endpoints: dict[str, Any],
    *,
    avoid_positions: set[tuple[float, float]] | None = None,
    protected_unit_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    start = endpoints["source_belt"]
    end = endpoints["target_belt"]
    start_direction = int(endpoints["source_belt_direction"])
    end_direction = int(endpoints["target_belt_direction"])
    direct_segments = _iron_plate_line_segments(
        observation,
        start,
        end,
        center_tiles=True,
        start_direction=start_direction,
        end_direction=end_direction,
        avoid_positions=avoid_positions,
    )
    if _logistics_researched_or_underground_unlocked(observation):
        return direct_segments
    direct_conflicts = _site_input_transport_belt_conflicts(direct_segments)
    direct_hard_blockers = _site_input_hard_route_blockers(
        observation,
        direct_segments,
        protected_unit_numbers=protected_unit_numbers,
    )
    if not direct_conflicts and not direct_hard_blockers:
        return direct_segments

    candidates: list[tuple[float, list[dict[str, Any]]]] = []
    for waypoints in _site_input_line_waypoint_candidates(start, end, start_direction, end_direction):
        segments = _iron_plate_segments_from_waypoints(observation, waypoints, center_tiles=True)
        conflicts = _site_input_transport_belt_conflicts(segments)
        if conflicts and not _site_input_route_repairable_with_conflicts(segments):
            continue
        if _site_input_hard_route_blockers(observation, segments, protected_unit_numbers=protected_unit_numbers):
            continue
        candidates.append(
            (
                _site_input_line_route_score(
                    observation,
                    segments,
                    avoid_positions=avoid_positions,
                    protected_unit_numbers=protected_unit_numbers,
                ),
                segments,
            )
        )
    if not candidates:
        return direct_segments
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _site_input_line_waypoint_candidates(
    start: dict[str, float],
    end: dict[str, float],
    start_direction: int,
    end_direction: int,
) -> list[list[dict[str, float]]]:
    start_point = {"x": float(start["x"]), "y": float(start["y"])}
    end_point = {"x": float(end["x"]), "y": float(end["y"])}
    candidates: list[list[dict[str, float]]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()

    def add(waypoints: list[dict[str, float]]) -> None:
        key = tuple(_position_tuple(point) for point in waypoints)
        if key in seen:
            return
        seen.add(key)
        candidates.append(waypoints)

    add(_role_aware_axis_line_waypoints(start_point, end_point, start_direction, end_direction))

    offsets = (1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 5.0, -5.0, 7.0, -7.0)

    def add_y_lanes() -> None:
        for offset in offsets:
            for base_y in (start_point["y"], end_point["y"]):
                lane_y = round(base_y + offset, 3)
                if abs(lane_y - start_point["y"]) <= 0.25 or abs(lane_y - end_point["y"]) <= 0.25:
                    continue
                add([start_point, {"x": start_point["x"], "y": lane_y}, {"x": end_point["x"], "y": lane_y}, end_point])

    def add_x_lanes() -> None:
        for offset in offsets:
            for base_x in (start_point["x"], end_point["x"]):
                lane_x = round(base_x + offset, 3)
                if abs(lane_x - start_point["x"]) <= 0.25 or abs(lane_x - end_point["x"]) <= 0.25:
                    continue
                add([start_point, {"x": lane_x, "y": start_point["y"]}, {"x": lane_x, "y": end_point["y"]}, end_point])

    if abs(start_point["x"] - end_point["x"]) <= 0.25:
        add_x_lanes()
    elif abs(start_point["y"] - end_point["y"]) <= 0.25:
        add_y_lanes()
    elif _direction_axis(start_direction) == _direction_axis(end_direction) == "x":
        add_y_lanes()
    elif _direction_axis(start_direction) == _direction_axis(end_direction) == "y":
        add_x_lanes()
    return candidates


def _site_input_transport_belt_conflicts(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for segment in segments:
        existing = segment.get("entity") if isinstance(segment, dict) else None
        if not isinstance(existing, dict) or str(existing.get("name") or "") != "transport-belt":
            continue
        planned_direction = _direction_or_default(segment.get("direction"), EAST)
        existing_direction = _direction_or_default(existing.get("direction"), planned_direction)
        if existing_direction != planned_direction:
            conflicts.append(segment)
    return conflicts


def _site_input_route_repairable_with_conflicts(segments: list[dict[str, Any]]) -> bool:
    if len(segments) < 4:
        return False
    matching = 0
    conflicts = 0
    for segment in segments:
        existing = segment.get("entity") if isinstance(segment, dict) else None
        if not isinstance(existing, dict) or str(existing.get("name") or "") != "transport-belt":
            continue
        planned_direction = _direction_or_default(segment.get("direction"), EAST)
        existing_direction = _direction_or_default(existing.get("direction"), planned_direction)
        if existing_direction == planned_direction:
            matching += 1
        else:
            conflicts += 1
    return conflicts > 0 and matching >= max(3, len(segments) - 2)


def _site_input_source_fanout_consumer_count(
    observation: dict[str, Any],
    item: str,
    source: dict[str, Any],
) -> int:
    source_key = _site_input_entity_key(source)
    consumers: set[str] = set()
    for consumer in _site_input_consumer_entities(observation, {item}):
        recipe_name = str(consumer.get("recipe") or consumer.get("recipe_name") or "")
        recipe = RECIPES.get(recipe_name)
        if recipe is None or item not in recipe.ingredients:
            continue
        if entity_item_count(consumer, item) > 0 and not _entity_status_name_in(
            consumer,
            {"item_ingredient_shortage", "missing_required_fluid"},
        ):
            continue
        consumer_position = _position(consumer)
        matched_source = _nearest_site_input_source(observation, item, consumer_position)
        if not isinstance(matched_source, dict):
            continue
        if _site_input_entity_key(matched_source) != source_key:
            continue
        if distance(_position(source), consumer_position) < 6.0:
            continue
        if _site_input_local_route_observed(observation, item, source, consumer):
            continue
        consumers.add(_site_input_entity_key(consumer))
    return len(consumers)


def _site_input_entity_key(entity: dict[str, Any]) -> str:
    unit_number = entity.get("unit_number")
    if unit_number is not None:
        return f"unit:{unit_number}"
    return f"{entity.get('name')}:{_position_key(entity)}"


def _site_input_splitter_fanout_required(observation: dict[str, Any], fanout_consumer_count: int) -> bool:
    if fanout_consumer_count < 2:
        return False
    return _site_input_splitter_available_or_unlocked(observation)


def _site_input_splitter_available_or_unlocked(observation: dict[str, Any]) -> bool:
    return (
        inventory_count(observation, "splitter") > 0
        or craftable_count(observation, "splitter") > 0
        or bool(_technology_state(observation, "logistics").get("researched"))
        or _recipe_unlocked_for_layout(observation, "splitter")
    )


def _site_input_splitter_fanout_plan(
    observation: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    direction: int,
) -> dict[str, Any] | None:
    for segment in segments[1:3]:
        if int(segment.get("direction", direction)) != int(direction):
            continue
        position = segment["position"]
        if _planned_machine_over_protected_resource(observation, position):
            continue
        existing_splitter = _entity_at_build_position(observation, "splitter", position, radius=0.75)
        existing_belt = _entity_at_build_position(observation, "transport-belt", position, radius=0.75)
        blocker = _build_position_blocker(
            observation,
            position,
            allowed_names={"splitter", "transport-belt"},
        )
        if blocker is not None and existing_splitter is None and existing_belt is None:
            continue
        return {
            "position": position,
            "direction": int(direction),
            "entity": existing_splitter,
            "existing_belt": existing_belt,
        }
    return None


def _site_input_segment_is_splitter(layout: dict[str, Any], segment: dict[str, Any]) -> bool:
    splitter = layout.get("splitter")
    if not isinstance(splitter, dict):
        return False
    return _position_tuple(splitter["position"]) == _position_tuple(segment["position"])


def _site_input_splitter_fanout_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    splitter: dict[str, Any],
) -> PlannerDecision | None:
    position = splitter["position"]
    direction = int(splitter["direction"])
    existing = _entity_at_build_position(observation, "splitter", position, radius=0.75)
    if isinstance(existing, dict) and _direction_or_default(existing.get("direction"), direction) == direction:
        return None

    if isinstance(existing, dict):
        existing_position = _position(existing)
        if distance(player, existing_position) > 4.5:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(existing_position, offset=1.5)},
                "move within reach of wrong splitter before rebuilding site input fanout",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": existing.get("unit_number"),
                "name": "splitter",
                "position": existing_position,
            },
            "remove misoriented splitter before rebuilding site input fanout",
        )

    if inventory_count(observation, "splitter") <= 0:
        if craftable_count(observation, "splitter") > 0:
            return PlannerDecision(
                {"type": "craft", "recipe": "splitter", "count": 1},
                "craft splitter before branching one site input source to multiple consumers",
            )
        decision = BuildItemMallSkill("splitter", 1).next_action(observation, reference_position=position)
        if not decision.done:
            return decision
        return PlannerDecision(
            None,
            "site input fanout needs a splitter before branching one source to multiple consumers",
        )

    existing_belt = _entity_at_build_position(observation, "transport-belt", position, radius=0.75)
    if isinstance(existing_belt, dict):
        belt_position = _position(existing_belt)
        if distance(player, belt_position) > 4.5:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(belt_position, offset=1.5)},
                "move within reach of source fanout belt before replacing it with splitter",
            )
        return PlannerDecision(
            {
                "type": "mine",
                "unit_number": existing_belt.get("unit_number"),
                "name": "transport-belt",
                "position": belt_position,
            },
            "replace source fanout belt with splitter instead of pulling two belts from the assembler output",
        )

    blocker = _build_position_blocker(
        observation,
        position,
        allowed_names={"splitter", "transport-belt"},
    )
    if blocker is not None:
        return PlannerDecision(
            None,
            f"site input splitter fanout is blocked by existing {blocker.get('name')}; needs a reroute",
        )

    if distance(player, position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": _stand_position(position, offset=3.0)},
            "move near site input splitter fanout position",
        )
    return PlannerDecision(
        {
            "type": "build",
            "name": "splitter",
            "position": position,
            "direction": direction,
            "allow_nearby": False,
        },
        "place splitter so one site input source can fan out to multiple consumers",
    )


def _site_input_hard_route_blockers(
    observation: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    protected_unit_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for segment in segments:
        blocker = _belt_line_position_blocker(
            observation,
            segment["position"],
            protected_unit_numbers=protected_unit_numbers,
        )
        if isinstance(blocker, dict) and _site_input_hard_route_blocker(blocker, segment["position"]):
            blockers.append(blocker)
    return blockers


def _site_input_hard_route_blocker(entity: dict[str, Any], position: dict[str, float] | None = None) -> bool:
    name = str(entity.get("name") or "")
    large_names = ASSEMBLER_ENTITY_NAMES | {"lab", "stone-furnace", "burner-mining-drill", "boiler", "steam-engine"}
    if name in POWER_CONNECTOR_NAMES:
        return True
    if name in large_names:
        if isinstance(position, dict) and isinstance(entity.get("position"), dict):
            entity_position = _position(entity)
            return (
                abs(float(position.get("x") or 0.0) - float(entity_position["x"])) < 1.45
                and abs(float(position.get("y") or 0.0) - float(entity_position["y"])) < 1.45
            )
        return True
    return name in {"wooden-chest", "iron-chest", "steel-chest"}


def _site_input_line_route_score(
    observation: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    avoid_positions: set[tuple[float, float]] | None = None,
    protected_unit_numbers: set[int] | None = None,
) -> float:
    score = _iron_plate_line_route_score(observation, segments, avoid_positions=avoid_positions)
    score += 5000.0 * len(
        _site_input_hard_route_blockers(observation, segments, protected_unit_numbers=protected_unit_numbers)
    )
    for segment in segments:
        if _planned_machine_over_protected_resource(observation, segment["position"]):
            score += 1500.0
    return score


def _site_input_line_endpoint_candidates(source: dict[str, Any], consumer: dict[str, Any]) -> list[dict[str, Any]]:
    source_position = _position(source)
    consumer_position = _position(consumer)
    preferred_source_side = _dominant_axis_vector(source_position, consumer_position)
    preferred_target_side = _opposite_axis_vector(preferred_source_side)
    source_radius = _site_input_endpoint_radius(source)
    target_radius = _site_input_endpoint_radius(consumer)

    source_sides = _ordered_endpoint_sides(preferred_source_side)
    target_sides = _ordered_endpoint_sides(preferred_target_side)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for source_index, source_side in enumerate(source_sides):
        for target_index, target_side in enumerate(target_sides):
            source_belt = _offset_along_axis(source_position, source_side, source_radius + 1.0)
            target_belt = _offset_along_axis(consumer_position, target_side, target_radius + 1.0)
            key = (_position_tuple(source_belt), _position_tuple(target_belt))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "source_inserter": _offset_along_axis(source_position, source_side, source_radius),
                    "source_belt": source_belt,
                    "source_direction": _direction_from_axis_vector(_opposite_axis_vector(source_side)),
                    "source_belt_direction": _direction_from_axis_vector(source_side),
                    "target_inserter": _offset_along_axis(consumer_position, target_side, target_radius),
                    "target_belt": target_belt,
                    "target_direction": _direction_from_axis_vector(target_side),
                    "target_belt_direction": _direction_from_axis_vector(_opposite_axis_vector(target_side)),
                    "preference_penalty": (source_index + target_index) * 1000.0,
                }
            )
    return candidates


def _ordered_endpoint_sides(preferred_side: dict[str, float]) -> list[dict[str, float]]:
    preferred_direction = _direction_from_axis_vector(preferred_side)
    ordered_directions = [preferred_direction]
    if _direction_axis(preferred_direction) == "x":
        ordered_directions.extend([NORTH, SOUTH])
    else:
        ordered_directions.extend([WEST, EAST])
    ordered_directions.append(_direction_from_axis_vector(_opposite_axis_vector(preferred_side)))
    seen: set[int] = set()
    sides: list[dict[str, float]] = []
    for direction in ordered_directions:
        if direction in seen:
            continue
        seen.add(direction)
        sides.append(_direction_vector(direction))
    return sides


def _site_input_endpoint_hard_blockers(
    observation: dict[str, Any],
    endpoints: dict[str, Any],
    source_inserter_position: dict[str, float],
    target_inserter_position: dict[str, float],
    *,
    protected_unit_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for position in (
        source_inserter_position,
        _tile_center_position(endpoints["source_belt"]),
        target_inserter_position,
        _tile_center_position(endpoints["target_belt"]),
    ):
        blocker = _belt_line_position_blocker(
            observation,
            position,
            protected_unit_numbers=protected_unit_numbers,
        )
        if isinstance(blocker, dict) and _site_input_hard_route_blocker(blocker, position):
            blockers.append(blocker)
    return blockers


def _site_input_line_endpoints(source: dict[str, Any], consumer: dict[str, Any]) -> dict[str, Any]:
    return _site_input_line_endpoint_candidates(source, consumer)[0]


def _dominant_axis_vector(source: dict[str, float], target: dict[str, float]) -> dict[str, float]:
    dx = float(target["x"]) - float(source["x"])
    dy = float(target["y"]) - float(source["y"])
    if abs(dx) >= abs(dy):
        return {"x": 1.0 if dx >= 0 else -1.0, "y": 0.0}
    return {"x": 0.0, "y": 1.0 if dy >= 0 else -1.0}


def _opposite_axis_vector(vector: dict[str, float]) -> dict[str, float]:
    return {"x": -float(vector.get("x") or 0.0), "y": -float(vector.get("y") or 0.0)}


def _offset_along_axis(origin: dict[str, float], vector: dict[str, float], amount: float) -> dict[str, float]:
    return {
        "x": round(float(origin["x"]) + float(vector["x"]) * amount, 3),
        "y": round(float(origin["y"]) + float(vector["y"]) * amount, 3),
    }


def _site_input_endpoint_radius(entity: dict[str, Any]) -> float:
    name = str(entity.get("name") or "")
    if name in ASSEMBLER_ENTITY_NAMES:
        return 2.0
    if name in {"stone-furnace", "steel-furnace", "electric-furnace"}:
        return 1.0
    return 1.0


def _direction_from_axis_vector(vector: dict[str, float]) -> int:
    if abs(float(vector.get("x") or 0.0)) >= abs(float(vector.get("y") or 0.0)):
        return EAST if float(vector.get("x") or 0.0) >= 0 else WEST
    return SOUTH if float(vector.get("y") or 0.0) >= 0 else NORTH


def _direction_or_default(value: Any, fallback: int) -> int:
    if value is None:
        return int(fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _direction_axis(direction: int) -> str:
    return "x" if int(direction) in {EAST, WEST} else "y"


def _direction_vector(direction: int) -> dict[str, float]:
    if int(direction) == EAST:
        return {"x": 1.0, "y": 0.0}
    if int(direction) == WEST:
        return {"x": -1.0, "y": 0.0}
    if int(direction) == SOUTH:
        return {"x": 0.0, "y": 1.0}
    return {"x": 0.0, "y": -1.0}


def _route_detour_coordinate(start_value: float, end_value: float, direction: int) -> float:
    sign = 1.0 if int(direction) in {EAST, SOUTH} else -1.0
    distance_tiles = abs(float(end_value) - float(start_value))
    if distance_tiles <= 1.25:
        return round(float(end_value), 3)
    whole_steps = max(1, int(round(distance_tiles)))
    offset_steps = max(1, min(whole_steps - 1, whole_steps // 2))
    return round(float(start_value) + sign * offset_steps, 3)


def _role_aware_axis_line_waypoints(
    start: dict[str, float],
    end: dict[str, float],
    start_direction: int,
    end_direction: int,
) -> list[dict[str, float]]:
    start_x = float(start["x"])
    start_y = float(start["y"])
    end_x = float(end["x"])
    end_y = float(end["y"])
    start_axis = _direction_axis(start_direction)
    end_axis = _direction_axis(end_direction)

    waypoints = [{"x": start_x, "y": start_y}]
    if start_axis == end_axis == "x":
        if abs(start_y - end_y) > 0.25:
            detour_x = _route_detour_coordinate(start_x, end_x, start_direction)
            waypoints.extend([{"x": detour_x, "y": start_y}, {"x": detour_x, "y": end_y}])
    elif start_axis == end_axis == "y":
        if abs(start_x - end_x) > 0.25:
            detour_y = _route_detour_coordinate(start_y, end_y, start_direction)
            waypoints.extend([{"x": start_x, "y": detour_y}, {"x": end_x, "y": detour_y}])
    elif start_axis == "x":
        waypoints.append({"x": end_x, "y": start_y})
    else:
        waypoints.append({"x": start_x, "y": end_y})
    waypoints.append({"x": end_x, "y": end_y})
    return waypoints


def _site_input_item_priority(item: str) -> int:
    return {
        "copper-plate": 98,
        "iron-plate": 95,
        "iron-gear-wheel": 92,
        "copper-cable": 90,
        "electronic-circuit": 86,
        "automation-science-pack": 82,
        "logistic-science-pack": 78,
    }.get(item, 60)


def _consumer_site_id_for_entity(observation: dict[str, Any], consumer: dict[str, Any]) -> str:
    consumer_position = _position(consumer)
    sites = [site.to_dict() for site in estimate_factory_sites(observation)]
    candidates = [
        site
        for site in sites
        if isinstance(site.get("position"), dict) and distance(site["position"], consumer_position) <= 3.5
    ]
    if not candidates:
        return f"consumer:{consumer.get('unit_number')}"
    candidates.sort(key=lambda site: distance(site["position"], consumer_position))
    return str(candidates[0].get("site_id") or f"consumer:{consumer.get('unit_number')}")


def _transport_belt_assembler_exists(observation: dict[str, Any]) -> bool:
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or str(entity.get("name") or "") not in ASSEMBLER_ENTITY_NAMES:
            continue
        if str(entity.get("recipe") or entity.get("recipe_name") or "") != "transport-belt":
            continue
        if entity.get("electric_network_connected") is False:
            continue
        if _entity_status_is(entity, "no_power", 3):
            continue
        return True
    return False


def _transport_belt_output_assembler(observation: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        entity
        for entity in observation.get("entities") or []
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
        and str(entity.get("recipe") or entity.get("recipe_name") or "") == "transport-belt"
        and entity_item_count(entity, "transport-belt") > 0
    ]
    return _nearest_to(candidates, player_position(observation))


def _transport_belt_output_chest(observation: dict[str, Any]) -> dict[str, Any] | None:
    cell = _find_build_item_mall_cell(observation, "transport-belt", allow_existing_remote=True)
    if not isinstance(cell, dict):
        return None
    chest = cell.get("output_chest")
    if not isinstance(chest, dict):
        return None
    if entity_item_count(chest, "transport-belt") <= 0:
        return None
    return chest


def _find_gear_belt_mall_relocation_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    existing_layout = _find_iron_plate_logistic_line_to_gear_mall_layout(observation)
    if existing_layout is None:
        compact_layout = _find_compact_gear_belt_mall_relocation_layout(observation)
        if compact_layout is not None:
            return compact_layout
        return _find_partial_gear_belt_mall_relocation_layout(observation)
    source = existing_layout.get("source")
    gear_assembler = existing_layout.get("gear_assembler")
    belt_assembler = existing_layout.get("belt_assembler")
    if not isinstance(source, dict) or not isinstance(gear_assembler, dict) or not isinstance(belt_assembler, dict):
        return _find_partial_gear_belt_mall_relocation_layout(observation)

    source_position = _position(source)
    gear_position = _position(gear_assembler)
    targets = _gear_belt_mall_relocation_target_positions(observation, source_position)
    if targets is None:
        return None
    target_gear_position, target_belt_position = targets
    target_gear_assembler = _assembler_at_position(observation, target_gear_position)
    target_belt_assembler = _assembler_at_position(observation, target_belt_position)
    target_rebuild_in_progress = isinstance(target_gear_assembler, dict) or isinstance(target_belt_assembler, dict)
    route_cost = _gear_mall_plate_layout_cost_estimate(observation, source_position, gear_position)
    if target_rebuild_in_progress:
        route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
    if route_cost.get("route_cost_preference") != "relocate_mall_to_iron_source":
        return None

    return {
        "source": source,
        "gear_assembler": gear_assembler,
        "belt_assembler": belt_assembler,
        "target_gear_position": target_gear_position,
        "target_belt_position": target_belt_position,
        "target_gear_assembler": target_gear_assembler,
        "target_belt_assembler": target_belt_assembler,
        "source_distance_tiles": round(distance(source_position, gear_position), 1),
        **route_cost,
    }


def _find_compact_gear_belt_mall_relocation_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    sources = _iron_plate_source_furnaces(observation)
    if not sources:
        return None
    candidates: list[dict[str, Any]] = []
    assemblers = entities_named(observation, "assembling-machine-1")
    gear_assemblers = [item for item in assemblers if item.get("recipe") == "iron-gear-wheel"]
    for gear_assembler in gear_assemblers:
        gear_position = _position(gear_assembler)
        for belt_assembler in assemblers:
            if belt_assembler is gear_assembler or belt_assembler.get("recipe") != "transport-belt":
                continue
            belt_position = _position(belt_assembler)
            same_row_legacy_pair = (
                abs(belt_position["y"] - gear_position["y"]) <= 0.1
                and abs(abs(belt_position["x"] - gear_position["x"]) - 3.0) <= 0.1
            )
            nearby_non_logistics_pair = distance(belt_position, gear_position) <= 8.0
            if not same_row_legacy_pair and not nearby_non_logistics_pair:
                continue
            source = min(sources, key=lambda item: distance(_position(item), gear_position))
            source_position = _position(source)
            targets = _gear_belt_mall_relocation_target_positions(observation, source_position)
            if targets is None:
                continue
            target_gear_position, target_belt_position = targets
            route_cost = _gear_mall_plate_layout_cost_estimate(observation, source_position, gear_position)
            route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
            candidates.append(
                {
                    "source": source,
                    "gear_assembler": gear_assembler,
                    "belt_assembler": belt_assembler,
                    "target_gear_position": target_gear_position,
                    "target_belt_position": target_belt_position,
                    "target_gear_assembler": _assembler_at_position(observation, target_gear_position),
                    "target_belt_assembler": _assembler_at_position(observation, target_belt_position),
                    "source_distance_tiles": round(distance(source_position, gear_position), 1),
                    **route_cost,
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("source_distance_tiles") or 999999.0), reverse=True)
    return candidates[0]


def _find_partial_gear_belt_mall_relocation_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    if inventory_count(observation, "assembling-machine-1") <= 0:
        return _find_relocated_gear_belt_mall_target_layout(observation)
    source = _nearest_to(_iron_plate_source_furnaces(observation), player_position(observation))
    if not isinstance(source, dict):
        return None
    source_position = _position(source)
    targets = _gear_belt_mall_relocation_target_positions(observation, source_position)
    if targets is None:
        return None
    target_gear_position, target_belt_position = targets
    target_gear_assembler = _assembler_at_position(observation, target_gear_position)
    target_belt_assembler = _assembler_at_position(observation, target_belt_position)
    target_units = {
        assembler.get("unit_number")
        for assembler in (target_gear_assembler, target_belt_assembler)
        if isinstance(assembler, dict) and assembler.get("unit_number") is not None
    }
    recoverable = _recoverable_relocation_assembler(observation, exclude_unit_numbers=target_units)
    inventory_rebuild = not isinstance(recoverable, dict)
    target_rebuild_in_progress = isinstance(target_gear_assembler, dict) or isinstance(target_belt_assembler, dict)
    relocation_in_progress = (
        inventory_count(observation, "assembling-machine-1") > 0
        and (
            target_rebuild_in_progress
            or (
                isinstance(recoverable, dict)
                and recoverable.get("recipe") in {"transport-belt", "iron-gear-wheel"}
            )
        )
    )
    anchor_position = _position(recoverable) if isinstance(recoverable, dict) else player_position(observation)
    route_cost = _gear_mall_plate_layout_cost_estimate(observation, source_position, anchor_position)
    if inventory_rebuild or relocation_in_progress:
        route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
    if (
        not inventory_rebuild
        and not relocation_in_progress
        and route_cost.get("route_cost_preference") != "relocate_mall_to_iron_source"
        and distance(source_position, anchor_position) < PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT
    ):
        return None
    return {
        "source": source,
        "gear_assembler": None,
        "belt_assembler": recoverable,
        "target_gear_position": target_gear_position,
        "target_belt_position": target_belt_position,
        "target_gear_assembler": target_gear_assembler,
        "target_belt_assembler": target_belt_assembler,
        "source_distance_tiles": round(distance(source_position, anchor_position), 1),
        **route_cost,
    }


def _find_relocated_gear_belt_mall_target_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    sources = _iron_plate_source_furnaces(observation)
    if not sources:
        return None
    for source in sources:
        source_position = _position(source)
        targets = _gear_belt_mall_relocation_target_positions(observation, source_position)
        if targets is None:
            continue
        target_gear_position, target_belt_position = targets
        target_gear_assembler = _assembler_at_position(observation, target_gear_position)
        target_belt_assembler = _assembler_at_position(observation, target_belt_position)
        compact_gear_assembler, compact_belt_assembler = _compact_relocated_gear_belt_assemblers(
            observation,
            source_position,
        )
        if compact_gear_assembler is target_gear_assembler:
            compact_gear_assembler = None
        if compact_belt_assembler is target_belt_assembler:
            compact_belt_assembler = None
        if (
            not isinstance(target_gear_assembler, dict)
            and not isinstance(target_belt_assembler, dict)
            and not isinstance(compact_gear_assembler, dict)
            and not isinstance(compact_belt_assembler, dict)
        ):
            continue
        route_cost = _gear_mall_plate_layout_cost_estimate(observation, source_position, player_position(observation))
        route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
        return {
            "source": source,
            "gear_assembler": compact_gear_assembler,
            "belt_assembler": compact_belt_assembler,
            "target_gear_position": target_gear_position,
            "target_belt_position": target_belt_position,
            "target_gear_assembler": target_gear_assembler,
            "target_belt_assembler": target_belt_assembler,
            "source_distance_tiles": round(distance(source_position, player_position(observation)), 1),
            **route_cost,
        }
    return None


def _gear_belt_mall_relocation_target_positions(
    observation: dict[str, Any],
    source_position: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]] | None:
    for gear_position in _gear_belt_mall_relocation_gear_position_candidates(source_position):
        belt_position = {
            "x": _round_entity_center(gear_position["x"] + GEAR_BELT_MALL_ASSEMBLER_SPACING),
            "y": gear_position["y"],
        }
        if _gear_belt_mall_relocation_positions_avoid_resources(observation, gear_position, belt_position):
            return gear_position, belt_position
    return None


def _gear_belt_mall_relocation_gear_position_candidates(source_position: dict[str, float]) -> list[dict[str, float]]:
    offsets = [
        (5.5, -5.0),
        (5.5, 5.0),
        (8.5, -6.0),
        (8.5, 6.0),
        (5.5, -8.0),
        (5.5, 8.0),
        (5.5, -11.0),
        (5.5, 11.0),
        (8.5, -11.0),
        (8.5, 11.0),
        (11.5, -5.0),
        (11.5, 5.0),
        (11.5, -8.0),
        (11.5, 8.0),
    ]
    return [
        {
            "x": _round_entity_center(source_position["x"] + dx),
            "y": _round_entity_center(source_position["y"] + dy),
        }
        for dx, dy in offsets
    ]


def _gear_belt_mall_relocation_positions_avoid_resources(
    observation: dict[str, Any],
    *positions: dict[str, float],
) -> bool:
    return all(not _planned_machine_over_protected_resource(observation, position) for position in positions)


def _compact_relocated_gear_belt_assemblers(
    observation: dict[str, Any],
    source_position: dict[str, float],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    compact_gear_position = {
        "x": _round_entity_center(source_position["x"] + 5.5),
        "y": _round_entity_center(source_position["y"] - 5.0),
    }
    compact_belt_position = {"x": _round_entity_center(compact_gear_position["x"] + 3.0), "y": compact_gear_position["y"]}
    compact_gear = _assembler_at_position(observation, compact_gear_position)
    compact_belt = _assembler_at_position(observation, compact_belt_position)
    if isinstance(compact_gear, dict) and compact_gear.get("recipe") != "iron-gear-wheel":
        compact_gear = None
    if isinstance(compact_belt, dict) and compact_belt.get("recipe") != "transport-belt":
        compact_belt = None
    return compact_gear, compact_belt


def _recoverable_relocation_assembler(
    observation: dict[str, Any],
    *,
    exclude_unit_numbers: set[Any] | None = None,
) -> dict[str, Any] | None:
    excluded = exclude_unit_numbers or set()
    candidates = [
        item
        for item in entities_named(observation, "assembling-machine-1")
        if item.get("unit_number") not in excluded
        and (
            item.get("electric_network_connected") is not False
            or item.get("recipe") in {"transport-belt", "iron-gear-wheel"}
        )
        and item.get("recipe") not in {"copper-cable", "electronic-circuit"}
        and _within_allowed_factory_area(observation, _position(item))
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item.get("recipe") in {"small-electric-pole", "transport-belt", "iron-gear-wheel"} else 1,
            float(item.get("distance") or distance(player_position(observation), _position(item))),
        )
    )
    return candidates[0]


def _iron_plate_source_furnaces(observation: dict[str, Any]) -> list[dict[str, Any]]:
    furnaces: list[dict[str, Any]] = []
    for name in ("stone-furnace", "steel-furnace", "electric-furnace"):
        for entity in entities_named(observation, name):
            if entity_item_count(entity, "iron-plate") > 0 or str(entity.get("recipe") or "") == "iron-plate":
                furnaces.append(entity)
            elif _furnace_has_plate_output_belt(observation, entity):
                furnaces.append(entity)
    return furnaces


def _furnace_has_plate_output_belt(observation: dict[str, Any], furnace: dict[str, Any]) -> bool:
    for name in ("inserter", "burner-inserter", "fast-inserter"):
        for inserter in entities_named(observation, name):
            endpoints = _inserter_endpoints(inserter)
            if endpoints is None:
                continue
            pickup, drop = endpoints
            if not _point_inside_machine(pickup, furnace) or _point_inside_machine(drop, furnace):
                continue
            if _entity_at_build_position(observation, "transport-belt", _tile_center_position(drop), radius=0.75):
                return True
    return False


def _iron_plate_line_segments(
    observation: dict[str, Any],
    start: dict[str, float],
    end: dict[str, float],
    *,
    center_tiles: bool = False,
    start_direction: int | None = None,
    end_direction: int | None = None,
    avoid_positions: set[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    start_x = float(start["x"])
    start_y = float(start["y"])
    end_x = float(end["x"])
    end_y = float(end["y"])
    if start_direction is not None and end_direction is not None:
        waypoint_candidates = [_role_aware_axis_line_waypoints(start, end, int(start_direction), int(end_direction))]
    else:
        waypoint_candidates = _iron_plate_line_waypoint_candidates(observation, start_x, start_y, end_x, end_y)

    scored: list[tuple[float, list[dict[str, Any]]]] = []
    avoid = set(avoid_positions or set())
    for waypoints in waypoint_candidates:
        segments = _iron_plate_segments_from_waypoints(observation, waypoints, center_tiles=center_tiles)
        scored.append((_iron_plate_line_route_score(observation, segments, avoid_positions=avoid), segments))
    scored.sort(key=lambda item: item[0])
    return scored[0][1] if scored else []


def connect_entities_tiles(
    observation: dict[str, Any],
    start: dict[str, float],
    end: dict[str, float],
    name: str,
    *,
    start_direction: int | None = None,
    end_direction: int | None = None,
    avoid_positions: set[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Ordered ``[{"position", "direction"}]`` placements that connect ``start`` to ``end``
    with ``name`` (transport-belt / pipe / *-electric-pole), reusing the proven belt/pipe
    router (``_iron_plate_line_segments``). Belt flow direction follows the consumer facing
    via ``end_direction`` (so a consumer to the west yields WEST-flowing tiles). Pipes are
    emitted undirected (direction 0). Power poles use a spaced stride under wire reach.

    This is the pure path computation behind the ``connect_entities`` action primitive: the
    whole list is placed in a single RCON call by the Lua ``action_connect_entities`` handler.
    """

    lowered = str(name).lower()
    if "electric-pole" in lowered or lowered.endswith("-pole"):
        return _pole_route_tiles(start, end)
    segments = _iron_plate_line_segments(
        observation,
        start,
        end,
        center_tiles=True,
        start_direction=start_direction,
        end_direction=end_direction,
        avoid_positions=avoid_positions,
    )
    is_pipe = lowered == "pipe"
    tiles: list[dict[str, Any]] = []
    for segment in segments:
        direction = 0 if is_pipe else int(segment.get("direction", EAST))
        tiles.append({"position": dict(segment["position"]), "direction": direction})
    return tiles


def _pole_route_tiles(
    start: dict[str, float],
    end: dict[str, float],
    *,
    stride: float = 6.0,
) -> list[dict[str, Any]]:
    """Spaced power-pole placements along the L-path from ``start`` to ``end``. ``stride``
    stays under the small-electric-pole wire reach (7.5) so consecutive poles connect; both
    endpoints are always included."""

    waypoints = [
        {"x": float(start["x"]), "y": float(start["y"])},
        {"x": float(end["x"]), "y": float(start["y"])},
        {"x": float(end["x"]), "y": float(end["y"])},
    ]
    raw = [dict(start)] + [dict(position) for position, _direction in _axis_route_positions(waypoints)] + [dict(end)]
    centered_path: list[dict[str, float]] = []
    seen_path: set[tuple[float, float]] = set()
    for position in raw:
        centered = _tile_center_position(position)
        key = _position_tuple(centered)
        if key in seen_path:
            continue
        seen_path.add(key)
        centered_path.append(centered)
    if not centered_path:
        return []
    tiles: list[dict[str, Any]] = [{"position": centered_path[0], "direction": 0}]
    last = centered_path[0]
    for centered in centered_path[1:-1]:
        if distance(centered, last) >= stride:
            tiles.append({"position": centered, "direction": 0})
            last = centered
    end_tile = centered_path[-1]
    if _position_tuple(end_tile) != _position_tuple(tiles[-1]["position"]):
        tiles.append({"position": end_tile, "direction": 0})
    return tiles


def _iron_plate_line_waypoint_candidates(
    observation: dict[str, Any],
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> list[list[dict[str, float]]]:
    candidates: list[list[dict[str, float]]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()

    def add(waypoints: list[dict[str, float]]) -> None:
        key = tuple(_position_tuple(point) for point in waypoints)
        if key not in seen:
            seen.add(key)
            candidates.append(waypoints)

    start_point = {"x": start_x, "y": start_y}
    end_point = {"x": end_x, "y": end_y}
    default_waypoints = [start_point]
    if end_x < start_x - 0.25:
        detour_x = start_x + 1.0
        detour_y = _select_iron_plate_line_detour_y(observation, start_x, start_y, end_x, end_y, detour_x)
        default_waypoints.extend(
            [
                {"x": detour_x, "y": start_y},
                {"x": detour_x, "y": detour_y},
                {"x": end_x, "y": detour_y},
            ]
        )
    default_waypoints.append(end_point)
    add(default_waypoints)

    if abs(start_x - end_x) > 0.25 and abs(start_y - end_y) > 0.25:
        add([start_point, {"x": end_x, "y": start_y}, end_point])
        add([start_point, {"x": start_x, "y": end_y}, end_point])
        for offset in (1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 5.0, -5.0, 7.0, -7.0, 9.0, -9.0, 11.0, -11.0):
            for base_x in (start_x, end_x):
                lane_x = round(base_x + offset, 3)
                if abs(lane_x - start_x) <= 0.25 or abs(lane_x - end_x) <= 0.25:
                    continue
                add([start_point, {"x": lane_x, "y": start_y}, {"x": lane_x, "y": end_y}, end_point])
            for base_y in (start_y, end_y):
                lane_y = round(base_y + offset, 3)
                if abs(lane_y - start_y) <= 0.25 or abs(lane_y - end_y) <= 0.25:
                    continue
                add([start_point, {"x": start_x, "y": lane_y}, {"x": end_x, "y": lane_y}, end_point])
    return candidates


def _iron_plate_segments_from_waypoints(
    observation: dict[str, Any],
    waypoints: list[dict[str, float]],
    *,
    center_tiles: bool,
) -> list[dict[str, Any]]:
    end_x = float(waypoints[-1]["x"])
    end_y = float(waypoints[-1]["y"])
    positions = _axis_route_positions(waypoints)
    if not positions:
        positions.append(({"x": end_x, "y": end_y}, EAST))
    elif distance(positions[-1][0], {"x": end_x, "y": end_y}) > 0.25:
        positions.append(({"x": end_x, "y": end_y}, positions[-1][1]))

    segments: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for position, direction in positions:
        rounded = _tile_center_position(position) if center_tiles else _rounded_position(position)
        key = _position_tuple(rounded)
        if key in seen:
            continue
        seen.add(key)
        segments.append(
            {
                "position": rounded,
                "direction": direction,
                "entity": _entity_at_build_position(observation, "transport-belt", rounded),
            }
        )
    return segments


def _iron_plate_line_route_score(
    observation: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    avoid_positions: set[tuple[float, float]] | None = None,
) -> float:
    score = len(segments) / 100.0
    avoid = set(avoid_positions or set())
    for segment in segments:
        if _position_tuple(segment["position"]) in avoid:
            score += 2500.0
        entity = segment.get("entity")
        if isinstance(entity, dict):
            if _direction_or_default(entity.get("direction"), segment["direction"]) != int(segment["direction"]):
                score += 500.0
            else:
                score -= 0.25
        blocker = _belt_line_position_blocker(observation, segment["position"])
        if blocker is None:
            continue
        blocker_name = str(blocker.get("name") or "")
        blocker_type = str(blocker.get("type") or "")
        if blocker_type == "tree":
            score += 2.0
        elif blocker_name in {"inserter", "burner-inserter", "fast-inserter"}:
            score += 200.0
        elif blocker_name == "small-electric-pole":
            score += 260.0
        elif blocker_name in ASSEMBLER_ENTITY_NAMES or blocker_name in {"stone-furnace", "burner-mining-drill", "boiler", "steam-engine"}:
            score += 400.0
        else:
            score += 650.0
    return score


def _rounded_position(position: dict[str, float]) -> dict[str, float]:
    return {
        "x": round(float(position["x"]), 3),
        "y": round(float(position["y"]), 3),
    }


def _tile_center_position(position: dict[str, float]) -> dict[str, float]:
    return {
        "x": round(floor(float(position["x"])) + 0.5, 3),
        "y": round(floor(float(position["y"])) + 0.5, 3),
    }


def _axis_route_positions(waypoints: list[dict[str, float]]) -> list[tuple[dict[str, float], int]]:
    positions: list[tuple[dict[str, float], int]] = []
    current = dict(waypoints[0])
    for waypoint in waypoints[1:]:
        waypoint_x = float(waypoint["x"])
        waypoint_y = float(waypoint["y"])
        if abs(current["x"] - waypoint_x) > 0.25:
            direction = EAST if waypoint_x > current["x"] else WEST
            while abs(current["x"] - waypoint_x) > 0.25:
                positions.append((dict(current), direction))
                step = min(1.0, abs(waypoint_x - current["x"]))
                current["x"] += step if waypoint_x > current["x"] else -step
        if abs(current["y"] - waypoint_y) > 0.25:
            direction = SOUTH if waypoint_y > current["y"] else NORTH
            while abs(current["y"] - waypoint_y) > 0.25:
                positions.append((dict(current), direction))
                step = min(1.0, abs(waypoint_y - current["y"]))
                current["y"] += step if waypoint_y > current["y"] else -step
    return positions


def _select_iron_plate_line_detour_y(
    observation: dict[str, Any],
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    detour_x: float,
) -> float:
    sign = 1.0 if end_y >= start_y else -1.0
    offsets = [3.0 * sign, -3.0 * sign, 5.0 * sign, -5.0 * sign, 7.0 * sign, -7.0 * sign, 9.0 * sign, -9.0 * sign]
    candidates: list[tuple[int, float, float]] = []
    for offset in offsets:
        detour_y = start_y + offset
        waypoints = [
            {"x": start_x, "y": start_y},
            {"x": detour_x, "y": start_y},
            {"x": detour_x, "y": detour_y},
            {"x": end_x, "y": detour_y},
            {"x": end_x, "y": end_y},
        ]
        blocker_score = 0
        for position, _direction in _axis_route_positions(waypoints):
            blocker = _belt_line_position_blocker(observation, position)
            if blocker is None:
                continue
            blocker_type = str(blocker.get("type") or "")
            blocker_name = str(blocker.get("name") or "")
            if blocker_type == "tree":
                blocker_score += 0
            elif blocker_name in ASSEMBLER_ENTITY_NAMES:
                blocker_score += 10
            else:
                blocker_score += 4
        candidates.append((blocker_score, abs(offset), detour_y))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else start_y + (3.0 * sign)


def _belt_line_position_blocker(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    protected_unit_numbers: set[int] | None = None,
) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    large_entities = ASSEMBLER_ENTITY_NAMES | {"lab", "stone-furnace", "burner-mining-drill", "boiler", "steam-engine", "offshore-pump"}
    protected_unit_numbers = protected_unit_numbers or set()
    blockers: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        try:
            if int(entity.get("unit_number")) in protected_unit_numbers:
                continue
        except (TypeError, ValueError):
            pass
        name = str(entity.get("name") or "")
        if name in {"character", "transport-belt"}:
            continue
        if _is_preserved_starter_artifact(observation, entity):
            continue
        if name in {"wooden-chest", "iron-chest", "steel-chest"} and distance(_position(entity), position) < 0.45:
            blockers.append(entity)
            continue
        if name in large_entities and _point_inside_machine(position, entity):
            blockers.append(entity)
            continue
        if name in {"inserter", "burner-inserter", "fast-inserter", "small-electric-pole"} and distance(_position(entity), position) < 0.45:
            blockers.append(entity)
            continue
        entity_type = str(entity.get("type") or "")
        if entity_type == "tree" and distance(_position(entity), position) < 1.25:
            blockers.append(entity)
            continue
        if (entity_type in {"simple-entity", "cliff"} or name.endswith("rock")) and distance(_position(entity), position) < 1.75:
            blockers.append(entity)
    return _nearest_to(blockers, position) if blockers else None


def _available_logistics_line_inserter_item(observation: dict[str, Any]) -> str | None:
    for item in ("inserter", "fast-inserter"):
        if inventory_count(observation, item) > 0:
            return item
    return None


def _logistics_line_powered_inserter_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    inserter: dict[str, Any],
    label: str,
) -> PlannerDecision | None:
    if str(inserter.get("name") or "") == "burner-inserter" or inserter.get("electric_network_connected") is not False:
        return None
    position = _position(inserter)
    existing = _nearest_connected_small_pole_supplying_position(observation, position)
    if existing is None:
        existing = _nearest_small_pole_supplying_position(observation, position)
    if existing is not None:
        pole_position = _position(existing)
        if distance(player, pole_position) > 20:
            return PlannerDecision({"type": "move_to", "position": pole_position}, f"move near supply pole for {label}")
        return PlannerDecision(
            {
                "type": "connect_power",
                "unit_number": existing.get("unit_number"),
                "name": existing.get("name") or "small-electric-pole",
                "position": pole_position,
            },
            f"connect supply pole for {label}",
        )

    if inventory_count(observation, "small-electric-pole") <= 0:
        decision = _ensure_small_power_pole_for_local_repair(observation, player, label)
        if decision is not None:
            return decision
        return PlannerDecision(None, f"{label} needs small-electric-pole power coverage")
    pole_position = _select_mall_inserter_power_pole_position(observation, position)
    if pole_position is None:
        corridor_decision = _logistics_line_power_corridor_decision(observation, player, position, label)
        if corridor_decision is not None:
            return corridor_decision
        return PlannerDecision(None, f"cannot find clear power pole position for {label}")
    if distance(player, pole_position) > 20:
        return PlannerDecision({"type": "move_to", "position": _stand_position(pole_position)}, f"move near power pole position for {label}")
    return PlannerDecision(
        {
            "type": "build",
            "name": "small-electric-pole",
            "position": pole_position,
            "allow_nearby": False,
        },
        f"place supply pole for {label}",
    )


def _logistics_line_power_corridor_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    inserter_position: dict[str, float],
    label: str,
) -> PlannerDecision | None:
    pole_position = _select_logistics_line_inserter_power_pole_position(observation, inserter_position)
    if pole_position is None:
        return None
    corridor_positions = _small_power_corridor_positions_to_target(observation, pole_position)
    if not corridor_positions:
        return PlannerDecision(None, f"{label} needs a connected power anchor before endpoint power corridor repair")

    missing_corridor_positions = _missing_power_corridor_positions(observation, corridor_positions)
    if missing_corridor_positions:
        if inventory_count(observation, "small-electric-pole") <= 0:
            decision = _ensure_small_power_pole_for_local_repair(observation, player, label)
            if decision is not None:
                return decision
            return PlannerDecision(None, f"{label} needs small-electric-pole for endpoint power corridor repair")
        position = missing_corridor_positions[0]
        build_position = _select_power_corridor_build_position(observation, corridor_positions, position)
        blocker = _power_corridor_position_blocker(observation, build_position)
        if blocker is not None:
            blocker_position = _position(blocker)
            if distance(player, blocker_position) > 8:
                return PlannerDecision(
                    {"type": "move_to", "position": blocker_position},
                    f"move near blocking {blocker.get('name')} before placing power corridor for {label}",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": blocker.get("unit_number"),
                    "name": blocker.get("name"),
                    "position": blocker_position,
                },
                f"clear blocking {blocker.get('name')} before placing power corridor for {label}",
            )
        if distance(player, build_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(build_position)},
                f"move near power corridor for {label}",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "small-electric-pole",
                "position": build_position,
                "allow_nearby": False,
            },
            f"build power corridor for {label}",
        )

    unconnected_pole = _first_unconnected_power_corridor_pole(observation, corridor_positions)
    if unconnected_pole is not None:
        pole_position = _position(unconnected_pole)
        source_network_id = _power_corridor_source_network_id(observation, corridor_positions)
        if distance(player, pole_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": pole_position},
                f"move near power corridor pole for {label}",
            )
        return PlannerDecision(
            {
                "type": "connect_power",
                "unit_number": unconnected_pole.get("unit_number"),
                "name": unconnected_pole.get("name") or "small-electric-pole",
                "position": pole_position,
                "source_network_id": source_network_id,
            },
            f"connect power corridor for {label}",
        )
    return PlannerDecision(None, f"{label} still lacks powered endpoint coverage after corridor check")


def _select_logistics_line_inserter_power_pole_position(
    observation: dict[str, Any],
    inserter_position: dict[str, float],
) -> dict[str, float] | None:
    anchor = _nearest_connected_power_anchor(observation, inserter_position)
    anchor_position = _power_corridor_wire_anchor_position(anchor) if isinstance(anchor, dict) else None
    candidates: list[dict[str, float]] = []
    for candidate in _nearby_power_pole_center_positions(inserter_position, radius=3.5):
        if not _small_pole_supplies_position(candidate, inserter_position):
            continue
        if _existing_power_connector_near_position(observation, candidate, radius=1.6) is not None:
            continue
        if _power_corridor_position_blocker(observation, candidate) is None:
            candidates.append(candidate)
    if not candidates:
        return None
    if anchor_position is not None:
        candidates.sort(key=lambda item: distance(item, anchor_position))
    return candidates[0]


def _ensure_small_power_pole_for_local_repair(
    observation: dict[str, Any],
    player: dict[str, float],
    label: str,
) -> PlannerDecision | None:
    decision = SetupPowerSkill()._ensure_item_quantity(observation, player, "small-electric-pole", 1)
    if decision is None:
        return None
    return PlannerDecision(
        decision.action,
        f"{decision.reason} for {label}",
        done=decision.done,
        metadata=decision.metadata,
    )


def _iron_plate_line_source_recovery_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    source: Any,
) -> PlannerDecision | None:
    return _plate_line_source_recovery_decision(observation, player, source, "iron-plate")


def _plate_line_source_recovery_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    source: Any,
    item: str,
) -> PlannerDecision | None:
    if not isinstance(source, dict):
        return None
    if item not in {"iron-plate", "copper-plate"}:
        return None
    resource_name = "iron-ore" if item == "iron-plate" else "copper-ore"
    item_label = "iron" if item == "iron-plate" else "copper"
    drill_context = (
        "iron source drill for gear mall plate logistics"
        if item == "iron-plate"
        else "copper source drill for site input plate logistics"
    )
    drill_far_fuel_reason = (
        "iron source drill needs local fuel before the gear mall plate line can stay active"
        if item == "iron-plate"
        else "copper source drill needs local fuel before the site input plate line can stay active"
    )
    furnace_context = (
        "iron source furnace for gear mall plate logistics"
        if item == "iron-plate"
        else "copper source furnace for site input plate logistics"
    )
    furnace_far_fuel_reason = (
        "iron source furnace needs local fuel before the gear mall plate line can output plates"
        if item == "iron-plate"
        else "copper source furnace needs local fuel before the site input plate line can output plates"
    )
    furnace_wait_reason = (
        "wait for iron source furnace to produce iron plates before endpoint inserter construction"
        if item == "iron-plate"
        else "wait for copper source furnace to produce copper plates before endpoint inserter construction"
    )
    if str(source.get("name") or "") not in FURNACE_ENTITY_NAMES:
        return None
    if str(source.get("recipe") or source.get("recipe_name") or "") != item:
        return None
    incompatible = _furnace_output_incompatible_item(source, item)
    if incompatible is not None:
        position = _position(source)
        if distance(player, position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": position},
                f"move near {item_label} source furnace to clear {incompatible} from output",
            )
        return PlannerDecision(
            {
                "type": "take",
                "item": incompatible,
                "count": entity_item_count(source, incompatible),
                "unit_number": source.get("unit_number"),
                "name": source.get("name") or "stone-furnace",
                "position": position,
            },
            f"clear {incompatible} from {item_label} source furnace output before building endpoint inserters",
        )
    source_output_count = entity_item_count(source, item)
    source_drill = _iron_plate_source_furnace_burner_drill(observation, source)
    if (
        isinstance(source_drill, dict)
        and source_output_count <= 0
        and _entity_burner_fuel_count(source_drill) < 1
        and _entity_status_is(source_drill, "no_fuel", 53)
    ):
        return _fuel_burner_line_entity(
            observation,
            player,
            source_drill,
            entity_name="burner-mining-drill",
            threshold=SMELTING_LINE_FUEL_RESERVE["drill"],
            insert_count=SMELTING_LINE_FUEL_INSERT["drill"],
            context=drill_context,
            support_skill=IronPlateSkill(),
            far_fuel_reason=drill_far_fuel_reason,
            exclude_source_units={source.get("unit_number")},
            prefer_coal_supply=True,
            allow_bootstrap_seed=True,
        )
    if (
        source_output_count <= 0
        and entity_item_count(source, resource_name) > 0
        and _entity_burner_fuel_count(source) < 1
        and _entity_status_is(source, "no_fuel", 52)
    ):
        return _fuel_burner_line_entity(
            observation,
            player,
            source,
            entity_name=str(source.get("name") or "stone-furnace"),
            threshold=SMELTING_LINE_FUEL_RESERVE["furnace"],
            insert_count=SMELTING_LINE_FUEL_INSERT["furnace"],
            context=furnace_context,
            support_skill=IronPlateSkill(),
            far_fuel_reason=furnace_far_fuel_reason,
            prefer_coal_supply=True,
            allow_bootstrap_seed=True,
        )
    if (
        source_output_count <= 0
        and entity_item_count(source, resource_name) > 0
        and _entity_burner_fuel_count(source) > 0
    ):
        return PlannerDecision(
            {"type": "wait", "ticks": 300},
            furnace_wait_reason,
        )
    return None


def _iron_plate_source_furnace_burner_drill(
    observation: dict[str, Any],
    furnace: dict[str, Any],
) -> dict[str, Any] | None:
    furnace_position = _position(furnace)
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for drill in entities_named(observation, "burner-mining-drill"):
        drill_position = _position(drill)
        if distance(drill_position, furnace_position) > 4.5:
            continue
        feeds_furnace = _burner_drill_output_touches_machine(drill, furnace)
        candidates.append((0 if feeds_furnace else 1, distance(drill_position, furnace_position), drill))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _burner_drill_output_touches_machine(drill: dict[str, Any], machine: dict[str, Any]) -> bool:
    drop_position = drill.get("drop_position")
    if isinstance(drop_position, dict):
        return _point_inside_machine(_tile_center_position(drop_position), machine)
    drill_position = _position(drill)
    vector = _direction_vector(_direction_or_default(drill.get("direction"), EAST))
    for amount in (1.0, 2.0, 3.0):
        if _point_inside_machine(_offset_along_axis(drill_position, vector, amount), machine):
            return True
    return False


def _furnace_output_incompatible_item(furnace: dict[str, Any], expected_item: str) -> str | None:
    inventories = furnace.get("inventories") if isinstance(furnace.get("inventories"), dict) else {}
    output = inventories.get("3") if isinstance(inventories.get("3"), dict) else {}
    for item, raw_count in sorted(output.items()):
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0 and str(item) != expected_item:
            return str(item)
    return None


def _logistics_line_inserter_material_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    layout: dict[str, Any],
    label: str,
) -> PlannerDecision | None:
    for item in ("inserter", "fast-inserter"):
        recipe = RECIPES.get(item)
        if recipe is None:
            continue
        for ingredient in ("iron-plate", "iron-gear-wheel", "electronic-circuit"):
            required = int(recipe.ingredients.get(ingredient) or 0)
            if required <= 0 or inventory_count(observation, ingredient) >= required:
                continue
            decision = _logistics_line_inserter_ingredient_decision(
                observation,
                player,
                layout,
                ingredient,
                required,
                label,
            )
            if decision is not None:
                return decision
            break
        else:
            craftable = craftable_count(observation, item)
            if craftable > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": item, "count": 1},
                    f"craft {item} for {label} from buffered construction materials",
                )
    return None


def _logistics_line_inserter_ingredient_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    layout: dict[str, Any],
    item: str,
    required: int,
    label: str,
) -> PlannerDecision | None:
    missing = max(0, required - inventory_count(observation, item))
    if missing <= 0:
        return None
    source: dict[str, Any] | None = None
    reason_source = "buffered"
    if item == "iron-plate":
        candidate = layout.get("source")
        if isinstance(candidate, dict) and entity_item_count(candidate, "iron-plate") > 0:
            source = candidate
            reason_source = "iron source"
    if source is None:
        source = _nearest_local_item_seed_source(observation, item, player)
    if not isinstance(source, dict) or entity_item_count(source, item) <= 0:
        return None
    position = _position(source)
    if distance(player, position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": position},
            f"move near {reason_source} {item} for {label} construction",
        )
    return PlannerDecision(
        {
            "type": "take",
            "item": item,
            "count": min(entity_item_count(source, item), missing),
            "unit_number": source.get("unit_number"),
            "name": source.get("name") or "wooden-chest",
            "position": position,
        },
        f"take {reason_source} {item} for {label} construction",
    )


def _find_relocatable_inserter_for_iron_plate_line(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    exclude_unit_numbers: set[int] | None = None,
    protected_positions: list[dict[str, float]] | None = None,
    allow_burner: bool = False,
) -> dict[str, Any] | None:
    excluded_units = set(exclude_unit_numbers or set())
    protected = list(protected_positions or [])
    protected_centers = [
        _position(entity)
        for entity in observation.get("entities", [])
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
        and str(entity.get("recipe") or "") in {"iron-gear-wheel", "transport-belt", "copper-cable", "electronic-circuit"}
    ]
    candidates: list[dict[str, Any]] = []
    names = ("inserter", "fast-inserter", "burner-inserter") if allow_burner else ("inserter", "fast-inserter")
    for name in names:
        for entity in entities_named(observation, name):
            try:
                if int(entity.get("unit_number")) in excluded_units:
                    continue
            except (TypeError, ValueError):
                pass
            position = _position(entity)
            if distance(position, target_position) <= 0.35:
                continue
            if any(distance(position, protected_position) <= 0.75 for protected_position in protected):
                continue
            if any(distance(position, center) <= 3.5 for center in protected_centers):
                continue
            candidates.append(entity)
    return _nearest_to(candidates, target_position)


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
            "direction": _direction_or_default(raw_spec.get("direction"), NORTH),
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
        # Offsets verified in-game (RCON fluidbox-connection test, see memory
        # power-block-placement-geometry): for a WEST-base pump these connect pump->boiler->engine;
        # the old {2,-1}/NORTH boiler + {2,-4}/NORTH engine produced a non-connecting, half-tile-
        # snapped layout (the user's 'water not connected' bug). The boiler base is SOUTH-facing so
        # rotation yields the correct per-direction water alignment.
        "boiler": {
            "name": "boiler",
            "position": _offset_position(position, _rotate_offset({"x": 2, "y": 0.5}, turns)),
            "direction": _rotate_direction(SOUTH, turns),
        },
        "steam_engine": {
            "name": "steam-engine",
            "position": _offset_position(position, _rotate_offset({"x": 2, "y": 4}, turns)),
            "direction": _rotate_direction(SOUTH, turns),
        },
        "small_electric_pole": {
            "name": "small-electric-pole",
            "position": _offset_position(position, _rotate_offset({"x": 0, "y": 4}, turns)),
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
        name = str(spec.get("name") or _power_spec_name(key))
        # The boiler/engine are placed by the self-calibrating place_fluid_connected action, so their
        # real tile is not the (stale) spec position -- match them by proximity to the upstream fluid
        # entity they connect to (pump for boiler, boiler for engine). pump/pole keep spec matching
        # (pole allow_nearby may shift it a few tiles, so use a wider radius).
        if key == "boiler" and isinstance(merged.get("offshore_pump"), dict):
            existing = _entity_near(observation, name, _position(merged["offshore_pump"]), radius=5.0)
        elif key == "steam_engine" and isinstance(merged.get("boiler"), dict):
            existing = _entity_near(observation, name, _position(merged["boiler"]), radius=7.0)
        elif key == "small_electric_pole" and isinstance(merged.get("steam_engine"), dict):
            existing = _entity_near(observation, name, _position(merged["steam_engine"]), radius=8.0)
        else:
            existing = _entity_near(
                observation,
                name,
                _xy_position(spec["position"]),
                radius=8.0 if key == "small_electric_pole" else 1.0,
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
        layout = _power_layout_from_pump_position(pump_position, _direction_or_default(pump.get("direction"), WEST))
        layout["offshore_pump"] = pump
        # Match boiler/engine/pole by PROXIMITY to their upstream entity (pump->boiler->engine->pole),
        # not the stale per-direction spec tile -- the self-calibrating place_fluid_connected action
        # relocates them, so spec-tile matching would miss them and falsely report power-not-ready
        # (the observed deadlock: ResearchAutomation waited forever for "power to settle").
        layout = _power_layout_with_existing_entities(observation, layout)
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
        if item == "iron-gear-wheel":
            decision = _ensure_iron_gears_without_post_automation_handcraft(
                observation,
                quantity,
                pre_automation_reason="craft iron-gear-wheel for automation research",
            )
            if decision is not None:
                return decision
            return self.power_skill._ensure_item_quantity(observation, player, item, quantity)
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

        ingredients = _technology_research_ingredients(observation, self.technology)
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
        scaling_mode = self.target_count > 5
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
            reference_position = line.get("circuit_assembler_position") or line.get("pole_position")
            decision = self._ensure_item_quantity(
                observation,
                player,
                missing_item,
                _circuit_cell_required_count(line, missing_item),
                reference_position=reference_position if isinstance(reference_position, dict) else None,
            )
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
                action["direction"] = _direction_or_default(line.get("transfer_inserter_direction"), EAST)
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

        if _circuit_cell_ready(line) and total_item_count(observation, "electronic-circuit") >= self.target_count:
            return PlannerDecision(
                None,
                f"circuit automation cell is running and target reached: {total_item_count(observation, 'electronic-circuit')}/{self.target_count}",
                done=True,
            )

        circuit_output = entity_item_count(circuit_assembler, "electronic-circuit") if circuit_assembler else 0
        if circuit_output > 0:
            if scaling_mode:
                return PlannerDecision(
                    {"type": "wait", "ticks": 300},
                    "wait for circuit assembler output to accumulate; refusing player-output collection during scaled circuit automation",
                )
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

        if circuit_assembler and entity_item_count(circuit_assembler, "copper-cable") < 6 and inventory_count(observation, "copper-cable") > 0:
            if not scaling_mode:
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
            if scaling_mode:
                blocker = self._scaled_input_blocker(observation, "iron-plate")
                if blocker is not None:
                    return blocker
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
            if scaling_mode:
                return PlannerDecision(
                    {"type": "wait", "ticks": 300},
                    "wait for transfer inserter to move cable from cable assembler to circuit assembler",
                )
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
            if scaling_mode:
                blocker = self._scaled_input_blocker(observation, "copper-plate")
                if blocker is not None:
                    return blocker
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

    def _scaled_input_blocker(self, observation: dict[str, Any], item: str) -> PlannerDecision | None:
        if not _belt_smelting_ready(observation):
            decision = GearBeltMallLogisticsSkill(20).next_action(observation)
            if decision is not None:
                return decision
        if item in {"iron-plate", "copper-plate"}:
            if not any(
                isinstance(entity, dict) and _entity_can_supply_site_input_item(entity, item)
                for entity in observation.get("entities") or []
            ):
                if item == "iron-plate":
                    return ExpandIronSmeltingSkill(max(40, self.target_count)).next_action(observation)
                return ExpandCopperSmeltingSkill(max(40, self.target_count)).next_action(observation)
            decision = SiteInputLogisticLineSkill(max(40, self.target_count), item=item).next_action(observation)
            if decision.action is not None or decision.done or "no executable repeated site input" not in decision.reason:
                return decision
        return PlannerDecision(
            None,
            (
                f"scaled circuit automation needs an automated {item} input line; "
                "refusing repeated player hand-carry into the circuit assemblers"
            ),
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
        *,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None

        if item == "assembling-machine-1":
            buffered_source = _nearest_buffered_chest_item_source(
                observation,
                item,
                reference_position or player,
            )
            if isinstance(buffered_source, dict):
                source_count = entity_item_count(buffered_source, item)
                if source_count > 0:
                    source_position = _position(buffered_source)
                    if distance(player, source_position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": source_position},
                            "move near buffered assembling-machine-1 output before circuit automation bootstrap",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": item,
                            "count": min(quantity - inventory_count(observation, item), source_count),
                            "unit_number": buffered_source.get("unit_number"),
                            "name": buffered_source.get("name") or "wooden-chest",
                            "position": source_position,
                        },
                        "take buffered assembling-machine-1 output before circuit automation bootstrap",
                    )
            if bool(_technology_state(observation, "automation").get("researched")) and _recipe_assembler_exists_for_layout(
                observation,
                "assembling-machine-1",
            ):
                decision = BuildItemMallSkill("assembling-machine-1", quantity).next_action(
                    observation,
                    reference_position=reference_position,
                )
                if not decision.done:
                    return decision
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
                    reference_position=reference_position,
                )
                if decision is not None:
                    return decision
            if bool(_technology_state(observation, "automation").get("researched")):
                decision = BuildItemMallSkill("assembling-machine-1", quantity).next_action(
                    observation,
                    reference_position=reference_position,
                )
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
            if bool(_technology_state(observation, "automation").get("researched")):
                decision = BuildItemMallSkill("inserter", max(quantity, 1)).next_action(observation)
                if not decision.done:
                    return decision
                return None
            for prerequisite, count in [
                ("electronic-circuit", quantity),
                ("iron-gear-wheel", quantity),
                ("iron-plate", quantity),
            ]:
                decision = self._ensure_item_quantity(
                    observation,
                    player,
                    prerequisite,
                    count,
                    reference_position=reference_position,
                )
                if decision is not None:
                    return decision
            return None

        if item == "electronic-circuit":
            decision = self.hand_circuit_skill.next_action(observation)
            if not decision.done:
                return decision
            return None

        if item == "iron-gear-wheel":
            if _gear_handcraft_automation_context_active(observation):
                decision = BuildItemMallSkill("iron-gear-wheel", max(quantity, 4)).next_action(
                    observation,
                    reference_position=reference_position,
                )
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
            return self._ensure_item_quantity(
                observation,
                player,
                "iron-plate",
                2 * (quantity - inventory_count(observation, "iron-gear-wheel")),
                reference_position=reference_position,
            )

        if item == "iron-plate":
            if reference_position is not None:
                logistics_blocker = _manual_site_input_logistics_blocker(
                    observation,
                    item,
                    reference_position,
                    consumer_label="electronic-circuit automation prerequisite",
                )
                if logistics_blocker is not None:
                    return logistics_blocker
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            if reference_position is not None:
                logistics_blocker = _manual_site_input_logistics_blocker(
                    observation,
                    item,
                    reference_position,
                    consumer_label="electronic-circuit automation prerequisite",
                )
                if logistics_blocker is not None:
                    return logistics_blocker
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


class GearBeltMallLogisticsSkill:
    """Build a short gear-to-belt mall link without player gear transfer."""

    def __init__(self, target_count: int = 20) -> None:
        self.target_count = target_count
        self.power_skill = SetupPowerSkill()
        self.research_skill = ResearchAutomationSkill()

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not _automation_researched(observation):
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

        layout = _find_gear_belt_mall_logistics_layout(observation)
        if layout is None:
            return PlannerDecision(
                None,
                "cannot find spaced powered gear and reusable belt assemblers for gear/belt mall logistics",
            )

        cleanup_decision = _obsolete_gear_belt_mall_buffer_cleanup_decision(observation, player, layout)
        if cleanup_decision is not None:
            return cleanup_decision

        belt_assembler = layout["belt_assembler"]
        incompatible = _first_incompatible_assembler_item(belt_assembler, "transport-belt")
        if incompatible is not None:
            position = _position(belt_assembler)
            if distance(player, position) > 20:
                return PlannerDecision({"type": "move_to", "position": position}, f"move near reusable belt assembler to clear {incompatible}")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": incompatible,
                    "count": entity_item_count(belt_assembler, incompatible),
                    "unit_number": belt_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": position,
                },
                f"clear {incompatible} from reusable belt assembler before setting transport-belt",
            )

        if belt_assembler.get("recipe") != "transport-belt":
            position = _position(belt_assembler)
            if distance(player, position) > 20:
                return PlannerDecision({"type": "move_to", "position": position}, "move near reusable assembler to set transport-belt")
            return PlannerDecision(
                {
                    "type": "set_recipe",
                    "recipe": "transport-belt",
                    "unit_number": belt_assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": position,
                },
                "set reusable mall assembler recipe to transport-belt",
            )

        direct_transfer = layout.get("direct_gear_transfer_inserter")
        direct_transfer_blocked = _direct_gear_transfer_blocked(layout)
        belt_lane_empty = all(belt.get("entity") is None for belt in layout["gear_belts"])
        belt_lane_has_transfer_inserter = _gear_belt_lane_has_transfer_inserter(layout)
        use_direct_transfer = (
            isinstance(direct_transfer, dict)
            and not direct_transfer_blocked
            and (belt_lane_empty or not belt_lane_has_transfer_inserter)
        )
        if use_direct_transfer:
            decision = self._ensure_inserter(
                observation,
                player,
                direct_transfer,
                prefer_burner=False,
                label="direct gear-to-belt transfer inserter",
                allow_bootstrap_craft=True,
            )
            if decision is not None:
                return decision
            decision = self._ensure_powered_inserter(
                observation,
                player,
                direct_transfer,
                label="direct gear-to-belt transfer inserter",
            )
            if decision is not None:
                return decision
        else:
            if inventory_count(observation, "transport-belt") <= 0 and belt_lane_empty:
                decision = self._ensure_bootstrap_transfer_belts(observation, player, layout)
                if decision is not None:
                    return decision
            for belt in layout["gear_belts"]:
                if belt.get("entity") is not None:
                    continue
                decision = self._ensure_inventory_item(observation, "transport-belt", 1)
                if decision is not None:
                    return decision
                position = belt["position"]
                if distance(player, position) > 20:
                    return PlannerDecision({"type": "move_to", "position": _stand_position(position)}, "move near gear mall belt lane")
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "transport-belt",
                        "position": position,
                        "direction": belt["direction"],
                        "allow_nearby": False,
                    },
                    "place gear-to-belt-mall transfer belt",
                )

            decision = self._ensure_inserter(
                observation,
                player,
                layout["gear_output_inserter"],
                prefer_burner=False,
                label="gear mall output inserter",
                allow_bootstrap_craft=True,
            )
            if decision is not None:
                return decision

            decision = self._ensure_inserter(
                observation,
                player,
                layout["belt_input_inserter"],
                prefer_burner=False,
                label="belt mall gear input inserter",
                allow_bootstrap_craft=True,
            )
            if decision is not None:
                return decision

            for spec, label in [
                (layout["gear_output_inserter"], "gear mall output inserter"),
                (layout["belt_input_inserter"], "belt mall gear input inserter"),
            ]:
                decision = self._ensure_powered_inserter(observation, player, spec, label=label)
                if decision is not None:
                    return decision

        available_belts = _available_transport_belt_construction_stock(observation)
        if available_belts >= self.target_count:
            return PlannerDecision(
                None,
                f"gear-fed belt mall logistics is running and available belt target reached: {available_belts}/{self.target_count}",
                done=True,
            )

        gear_assembler = layout["gear_assembler"]
        gear_assembler_plate_count = entity_item_count(gear_assembler, "iron-plate")
        if (
            entity_item_count(belt_assembler, "iron-gear-wheel") <= 0
            and entity_item_count(gear_assembler, "iron-gear-wheel") <= 0
            and gear_assembler_plate_count < 2
        ):
            if inventory_count(observation, "iron-plate") > 0:
                position = _position(gear_assembler)
                if distance(player, position) > 20:
                    return PlannerDecision({"type": "move_to", "position": position}, "move near gear assembler for one-time iron seed")
                return _bootstrap_seed_decision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": min(max(1, 2 - gear_assembler_plate_count), inventory_count(observation, "iron-plate")),
                        "unit_number": gear_assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": position,
                    },
                    "one-time bootstrap iron seed for gear mall; sustained iron input line is still required",
                    seed_reason="gear_mall_iron_plate_seed",
                    expected_followup="gear assembler produces iron-gear-wheel output for belt mall",
                )
            local_source = _nearest_local_item_seed_source(
                observation,
                "iron-plate",
                _position(gear_assembler),
                exclude_units={gear_assembler.get("unit_number"), belt_assembler.get("unit_number")},
            )
            if local_source is not None:
                source_position = _position(local_source)
                if distance(player, source_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": source_position},
                        "move near local iron-plate seed source for gear mall bootstrap",
                    )
                return _bootstrap_seed_decision(
                    {
                        "type": "take",
                        "item": "iron-plate",
                        "count": min(max(1, 2 - gear_assembler_plate_count), entity_item_count(local_source, "iron-plate")),
                        "unit_number": local_source.get("unit_number"),
                        "name": local_source.get("name"),
                        "position": source_position,
                    },
                    "recover local iron plates for one-time gear mall seed; sustained iron input line is still required",
                    seed_reason="local_iron_plate_recovery_for_gear_mall_seed",
                    expected_followup="recovered plates are inserted into the gear assembler and belt output starts",
                )
            return PlannerDecision(None, "gear mall logistics needs iron plates before gears can enter the belt mall")

        belt_assembler_plate_count = entity_item_count(belt_assembler, "iron-plate")
        if belt_assembler_plate_count < 2:
            if inventory_count(observation, "iron-plate") > 0:
                position = _position(belt_assembler)
                if distance(player, position) > 20:
                    return PlannerDecision({"type": "move_to", "position": position}, "move near belt assembler for one-time iron seed")
                return _bootstrap_seed_decision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": min(max(1, 2 - belt_assembler_plate_count), inventory_count(observation, "iron-plate")),
                        "unit_number": belt_assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": position,
                    },
                    "one-time bootstrap iron seed for belt mall; sustained iron input line is still required",
                    seed_reason="belt_mall_iron_plate_seed",
                    expected_followup="transport-belt assembler consumes buffered gears and produces belts",
                )
            local_source = _nearest_local_item_seed_source(
                observation,
                "iron-plate",
                _position(belt_assembler),
                exclude_units={gear_assembler.get("unit_number"), belt_assembler.get("unit_number")},
            )
            if local_source is not None:
                source_position = _position(local_source)
                if distance(player, source_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": source_position},
                        "move near local iron-plate seed source for belt mall bootstrap",
                    )
                return _bootstrap_seed_decision(
                    {
                        "type": "take",
                        "item": "iron-plate",
                        "count": min(max(1, 2 - belt_assembler_plate_count), entity_item_count(local_source, "iron-plate")),
                        "unit_number": local_source.get("unit_number"),
                        "name": local_source.get("name"),
                        "position": source_position,
                    },
                    "recover local iron plates for one-time belt mall seed; sustained iron input line is still required",
                    seed_reason="local_iron_plate_recovery_for_belt_mall_seed",
                    expected_followup="recovered plates are inserted into the belt assembler and belt output starts",
                )
            return PlannerDecision(None, "belt mall logistics needs an automated iron-plate input line")

        return PlannerDecision(
            {"type": "wait", "ticks": 600},
            f"wait for gear inserters and belt assembler to accumulate transport belts: {available_belts}/{self.target_count}",
        )

    def _ensure_inventory_item(self, observation: dict[str, Any], item: str, quantity: int) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None
        craftable = craftable_count(observation, item)
        if craftable > 0 and item != "iron-gear-wheel":
            return PlannerDecision(
                {"type": "craft", "recipe": item, "count": min(quantity - inventory_count(observation, item), craftable)},
                f"craft one-time bootstrap {item} for gear/belt mall logistics",
            )
        return PlannerDecision(None, f"missing {item} for gear/belt mall logistics")

    def _ensure_bootstrap_transfer_belts(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        layout: dict[str, Any],
    ) -> PlannerDecision | None:
        if craftable_count(observation, "transport-belt") > 0:
            return _bootstrap_seed_decision(
                {"type": "craft", "recipe": "transport-belt", "count": 1},
                "craft one-time bootstrap transfer belts for gear/belt mall logistics",
                seed_reason="gear_belt_transfer_belt_seed",
                expected_followup="short transfer lane is built and gear output feeds the belt assembler",
            )
        if inventory_count(observation, "iron-gear-wheel") <= 0:
            gear_assembler = layout["gear_assembler"]
            if entity_item_count(gear_assembler, "iron-gear-wheel") > 0:
                position = _position(gear_assembler)
                if distance(player, position) > 20:
                    return PlannerDecision({"type": "move_to", "position": position}, "move near gear assembler for bootstrap transfer belts")
                return _bootstrap_seed_decision(
                    {
                        "type": "take",
                        "item": "iron-gear-wheel",
                        "count": min(3, entity_item_count(gear_assembler, "iron-gear-wheel")),
                        "unit_number": gear_assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": position,
                    },
                    "recover one-time bootstrap gears for first gear/belt transfer belts; sustained transfer remains inserter-fed",
                    seed_reason="gear_output_recovery_for_transfer_belt_seed",
                    expected_followup="bootstrap transfer belts are crafted and replaced by inserter-fed logistics",
                )
        return None

    def _ensure_inserter(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        spec: dict[str, Any],
        *,
        prefer_burner: bool,
        label: str,
        allow_bootstrap_craft: bool = False,
    ) -> PlannerDecision | None:
        prefer_burner = False
        inserter = spec.get("entity")
        if isinstance(inserter, dict):
            if inserter.get("name") == "burner-inserter":
                position = _position(inserter)
                if _direction_or_default(inserter.get("direction"), 0) != int(spec["direction"]):
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            f"move within mining reach of misoriented {label}",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": inserter.get("unit_number"),
                            "name": "burner-inserter",
                            "position": position,
                        },
                        f"remove misoriented {label} before rebuilding the gear/belt mall line",
                    )
                if not _regular_inserter_can_be_used(observation):
                    if not _entity_status_is(inserter, "no_fuel", 53):
                        return None
                    return PlannerDecision(
                        None,
                        f"{label} needs a powered inserter; refusing to fuel burner inserter",
                    )
                if distance(player, position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                        f"move within mining reach of obsolete burner {label}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": inserter.get("unit_number"),
                        "name": "burner-inserter",
                        "position": position,
                    },
                    f"replace burner {label} now that regular inserters are usable",
                )
            if _direction_or_default(inserter.get("direction"), 0) != int(spec["direction"]):
                position = _position(inserter)
                if distance(player, position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                        f"move within mining reach of misoriented {label}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": inserter.get("unit_number"),
                        "name": inserter.get("name") or "inserter",
                        "position": position,
                    },
                    f"remove misoriented {label} before rebuilding the gear/belt mall line",
                )
            return None

        item_name = self._available_inserter_item(observation, prefer_burner=prefer_burner)
        if item_name is None:
            reusable = _find_relocatable_inserter_for_mall(
                observation,
                spec["position"],
                prefer_burner=prefer_burner,
                exclude_units=set(spec.get("exclude_reusable_unit_numbers") or set()),
            )
            if reusable is not None:
                position = _position(reusable)
                if distance(player, position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                        f"move within mining reach of reusable inserter for {label}",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": reusable.get("unit_number"),
                        "name": reusable.get("name") or "inserter",
                        "position": position,
                    },
                    f"relocate existing inserter for {label} instead of hand-crafting gears",
                )
            if allow_bootstrap_craft:
                decision = self._craft_bootstrap_inserter(observation, prefer_burner=prefer_burner, label=label)
                if decision is not None:
                    return decision
            return PlannerDecision(None, f"missing inserter for {label}")
        position = spec["position"]
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": _stand_position(position)}, f"move near {label} position")
        return PlannerDecision(
            {
                "type": "build",
                "name": item_name,
                "position": position,
                "direction": spec["direction"],
                "allow_nearby": False,
            },
            f"place {label}",
        )

    def _craft_bootstrap_inserter(
        self,
        observation: dict[str, Any],
        *,
        prefer_burner: bool,
        label: str,
    ) -> PlannerDecision | None:
        for item in ("inserter", "fast-inserter"):
            if craftable_count(observation, item) > 0:
                return _bootstrap_seed_decision(
                    {"type": "craft", "recipe": item, "count": 1},
                    f"craft one-time bootstrap {item} for {label}",
                    seed_reason=f"{label}_inserter_seed",
                    expected_followup=f"{label} is placed and moves gear or belt ingredients automatically",
                )
        if inventory_count(observation, "iron-gear-wheel") <= 0 and craftable_count(observation, "iron-gear-wheel") > 0:
            return _bootstrap_seed_decision(
                {
                    "type": "craft",
                    "recipe": "iron-gear-wheel",
                    "count": 1,
                    "allow_gear_belt_direct_transfer_bootstrap": True,
                },
                f"craft one-time bootstrap gear for regular {label}",
                seed_reason=f"{label}_gear_seed",
                expected_followup=f"crafted gear becomes one {label} inserter and automated transfer starts",
            )
        return None

    def _ensure_powered_inserter(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        spec: dict[str, Any],
        *,
        label: str,
    ) -> PlannerDecision | None:
        inserter = spec.get("entity")
        if not isinstance(inserter, dict) or inserter.get("name") == "burner-inserter":
            return None
        if inserter.get("electric_network_connected") is not False:
            return None

        position = _position(inserter)
        existing = _nearest_connected_small_pole_supplying_position(observation, position)
        if existing is None:
            existing = _nearest_small_pole_supplying_position(observation, position)
        if existing is not None:
            pole_position = _position(existing)
            if distance(player, pole_position) > 20:
                return PlannerDecision({"type": "move_to", "position": pole_position}, f"move near supply pole for {label}")
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": existing.get("unit_number"),
                    "name": "small-electric-pole",
                    "position": pole_position,
                },
                f"connect supply pole for {label}",
            )

        if inventory_count(observation, "small-electric-pole") <= 0:
            decision = _ensure_small_power_pole_for_local_repair(observation, player, label)
            if decision is not None:
                return decision
            return PlannerDecision(None, f"{label} needs small-electric-pole power coverage")
        pole_position = _select_mall_inserter_power_pole_position(observation, position)
        if pole_position is None:
            return PlannerDecision(None, f"cannot find clear power pole position for {label}")
        if distance(player, pole_position) > 20:
            return PlannerDecision({"type": "move_to", "position": _stand_position(pole_position)}, f"move near power pole position for {label}")
        return PlannerDecision(
            {
                "type": "build",
                "name": "small-electric-pole",
                "position": pole_position,
                "allow_nearby": False,
            },
            f"place supply pole for {label}",
        )

    def _available_inserter_item(self, observation: dict[str, Any], *, prefer_burner: bool) -> str | None:
        for item in ("inserter", "fast-inserter"):
            if inventory_count(observation, item) > 0:
                return item
        return None


class GearBeltMallRelocationSkill:
    """Relocate a too-distant gear/belt mall toward its iron-plate source."""

    def __init__(self, target_count: int = 20) -> None:
        self.target_count = target_count
        self.research_skill = ResearchAutomationSkill()

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not _automation_researched(observation):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        layout = _find_gear_belt_mall_relocation_layout(observation)
        if layout is None:
            return PlannerDecision(None, "no costed gear/belt mall relocation target was found")

        required_poles = layout.get("relocation_power_poles_estimate")
        available_poles = inventory_count(observation, "small-electric-pole")
        corridor_positions = _gear_belt_mall_relocation_power_corridor_positions(observation, layout)
        missing_corridor_positions = _missing_power_corridor_positions(observation, corridor_positions)
        if required_poles is None:
            return PlannerDecision(None, "gear/belt mall relocation needs a known power anchor before moving existing assemblers")
        if not corridor_positions:
            return PlannerDecision(
                None,
                "gear/belt mall relocation needs a connected power anchor before moving existing assemblers",
            )
        required_for_corridor = len(missing_corridor_positions)
        if available_poles < required_for_corridor:
            buffered_poles = _nearest_local_item_seed_source(
                observation,
                "small-electric-pole",
                _gear_belt_mall_relocation_power_target(layout),
                max_distance=256.0,
            )
            buffered_count = entity_item_count(buffered_poles, "small-electric-pole") if isinstance(buffered_poles, dict) else 0
            if buffered_count > 0:
                buffered_position = _position(buffered_poles)
                if distance(player, buffered_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": buffered_position},
                        "move near buffered small electric poles for gear/belt mall relocation corridor",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "small-electric-pole",
                        "count": min(buffered_count, required_for_corridor - available_poles),
                        "unit_number": buffered_poles.get("unit_number"),
                        "name": buffered_poles.get("name") or "wooden-chest",
                        "position": buffered_position,
                    },
                    "take buffered small electric poles for gear/belt mall relocation power corridor",
                )
            pole_target = max(self.target_count, required_for_corridor)
            bootstrap_decision = BuildItemMallSkill("small-electric-pole", pole_target).next_action(
                observation,
                allow_existing_remote=True,
                reference_position=_gear_belt_mall_relocation_power_target(layout),
            )
            if bootstrap_decision.action is not None:
                metadata = dict(bootstrap_decision.metadata)
                metadata.update(
                    {
                        "failure_root": "relocation_power_pole_shortage",
                        "repair_skill": "bootstrap_build_item_mall",
                        "target_item": "small-electric-pole",
                        "target_count": pole_target,
                        "required_for_corridor": required_for_corridor,
                        "available_poles": available_poles,
                    }
                )
                return PlannerDecision(
                    bootstrap_decision.action,
                    (
                        "bootstrap small-electric-pole for gear/belt mall relocation power corridor: "
                        f"{bootstrap_decision.reason}"
                    ),
                    metadata=metadata,
                )
            return PlannerDecision(
                None,
                (
                    f"gear/belt mall relocation needs {required_for_corridor} small-electric-pole for the power corridor "
                    f"before mining the existing mall; available {available_poles}"
                ),
            )

        target_specs = [
            ("target_gear_assembler", "target_gear_position", "iron-gear-wheel", "gear assembler"),
            ("target_belt_assembler", "target_belt_position", "transport-belt", "belt assembler"),
        ]
        placed_targets = sum(1 for entity_key, *_ in target_specs if isinstance(layout.get(entity_key), dict))
        if placed_targets < 2 and inventory_count(observation, "assembling-machine-1") + placed_targets < 2:
            target_units = {
                layout.get(entity_key, {}).get("unit_number")
                for entity_key, *_ in target_specs
                if isinstance(layout.get(entity_key), dict)
            }
            target_units.discard(None)
            for source_key, label in (("gear_assembler", "existing gear assembler"), ("belt_assembler", "existing belt assembler")):
                source = layout.get(source_key)
                if not isinstance(source, dict):
                    continue
                if source.get("unit_number") in target_units:
                    continue
                source_position = _position(source)
                if distance(player, source_position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(source_position, offset=1.5)},
                        f"move within reach of {label} for costed relocation",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": source.get("unit_number"),
                        "name": source.get("name") or "assembling-machine-1",
                        "position": source_position,
                    },
                    f"recover {label} for costed relocation after relocation power corridor materials are available",
                )
            return PlannerDecision(None, "gear/belt mall relocation needs two assembling machines before rebuilding near iron plates")

        if missing_corridor_positions:
            position = missing_corridor_positions[0]
            build_position = _select_power_corridor_build_position(observation, corridor_positions, position)
            blocker = _power_corridor_position_blocker(observation, build_position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before placing gear/belt mall relocation power corridor",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} before placing gear/belt mall relocation power corridor",
                )
            if distance(player, build_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(build_position)},
                    "move near gear/belt mall relocation power corridor",
                )
            reason = "build gear/belt mall relocation power corridor before mining existing mall"
            if _position_tuple(build_position) != _position_tuple(position):
                reason = "build detoured gear/belt mall relocation power corridor before mining existing mall"
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "small-electric-pole",
                    "position": build_position,
                    "allow_nearby": False,
                },
                reason,
            )

        unconnected_pole = _first_unconnected_power_corridor_pole(observation, corridor_positions)
        if unconnected_pole is not None:
            pole_position = _position(unconnected_pole)
            source_network_id = _power_corridor_source_network_id(observation, corridor_positions)
            if distance(player, pole_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": pole_position},
                    "move near gear/belt mall relocation power corridor pole",
                )
            return PlannerDecision(
                {
                    "type": "connect_power",
                    "unit_number": unconnected_pole.get("unit_number"),
                    "name": unconnected_pole.get("name") or "small-electric-pole",
                    "position": pole_position,
                    "source_network_id": source_network_id,
                },
                "connect gear/belt mall relocation power corridor before moving assemblers",
            )

        for entity_key, _position_key, recipe, label in target_specs:
            assembler = layout.get(entity_key)
            if isinstance(assembler, dict) and assembler.get("recipe") != recipe:
                position = _position(assembler)
                if distance(player, position) > 20:
                    return PlannerDecision({"type": "move_to", "position": position}, f"move near relocated {label} to set recipe")
                return PlannerDecision(
                    {
                        "type": "set_recipe",
                        "unit_number": assembler.get("unit_number"),
                        "name": assembler.get("name") or "assembling-machine-1",
                        "recipe": recipe,
                        "position": position,
                    },
                    f"set relocated {label} recipe to {recipe}",
                )

        for entity_key, position_key, recipe, label in target_specs:
            if isinstance(layout.get(entity_key), dict):
                continue
            position = layout[position_key]
            blocker = _build_position_blocker(observation, position, allowed_names=ASSEMBLER_ENTITY_NAMES)
            if blocker is None:
                blocker = _machine_position_natural_blocker(observation, position)
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before relocating gear/belt mall",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} from compact gear/belt mall relocation site",
                )
            if inventory_count(observation, "assembling-machine-1") <= 0:
                return PlannerDecision(None, "gear/belt mall relocation needs recovered assembling machines in inventory")
            if distance(player, position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(position)}, f"move near relocated {label} site")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "assembling-machine-1",
                    "position": position,
                    "allow_nearby": False,
                },
                f"place relocated {label} near iron-plate source",
            )

        return PlannerDecision(
            None,
            "gear/belt mall assemblers are relocated near the iron-plate source; next build local gear-to-belt logistics",
            done=True,
        )


def _gear_belt_mall_relocation_power_corridor_positions(
    observation: dict[str, Any],
    layout: dict[str, Any],
) -> list[dict[str, float]]:
    target = _gear_belt_mall_relocation_power_target(layout)
    return _small_power_corridor_positions_to_target(observation, target)


def _small_power_corridor_positions_to_target(
    observation: dict[str, Any],
    target: dict[str, float],
) -> list[dict[str, float]]:
    anchor = _nearest_connected_power_anchor(observation, target)
    if anchor is None:
        return []
    anchor_position = _power_corridor_wire_anchor_position(anchor)
    seed_positions: list[dict[str, float]] = []
    if str(anchor.get("name") or "") not in POWER_CONNECTOR_NAMES:
        seed_positions.append(anchor_position)
    span = distance(anchor_position, target)
    if span <= _power_wire_reach("small-electric-pole"):
        return _dedupe_positions(seed_positions + [target])
    step_reach = _power_wire_reach("small-electric-pole")
    steps = max(1, int(ceil(span / step_reach)))
    while True:
        positions: list[dict[str, float]] = list(seed_positions)
        for index in range(1, steps + 1):
            ratio = index / steps
            positions.append(
                {
                    "x": _round_power_pole_center(anchor_position["x"] + (target["x"] - anchor_position["x"]) * ratio),
                    "y": _round_power_pole_center(anchor_position["y"] + (target["y"] - anchor_position["y"]) * ratio),
                }
            )
        positions = _dedupe_positions(positions)
        if _power_corridor_gaps_within_reach(anchor_position, positions, step_reach):
            return positions
        steps += 1


def _power_corridor_wire_anchor_position(anchor: dict[str, Any]) -> dict[str, float]:
    position = _position(anchor)
    if str(anchor.get("name") or "") in POWER_CONNECTOR_NAMES:
        return position
    return {"x": _round_half(position["x"] - 2.0), "y": _round_half(position["y"])}


def _power_corridor_gaps_within_reach(
    anchor_position: dict[str, float],
    positions: list[dict[str, float]],
    reach: float,
) -> bool:
    previous = anchor_position
    for position in positions:
        if distance(previous, position) > reach:
            return False
        previous = position
    return True


def _gear_belt_mall_relocation_power_target(layout: dict[str, Any]) -> dict[str, float]:
    gear = layout.get("target_gear_position") if isinstance(layout.get("target_gear_position"), dict) else {}
    belt = layout.get("target_belt_position") if isinstance(layout.get("target_belt_position"), dict) else gear
    gear_position = _xy_position(gear) if gear else {"x": 0.0, "y": 0.0}
    belt_position = _xy_position(belt) if belt else gear_position
    return {
        "x": _round_half((gear_position["x"] + belt_position["x"]) / 2.0),
        "y": _round_half(gear_position["y"] + 2.0),
    }


def _nearest_connected_power_anchor(observation: dict[str, Any], target: dict[str, float]) -> dict[str, Any] | None:
    names = POWER_CONNECTOR_NAMES | {"steam-engine", "steam-turbine", "solar-panel", "accumulator"}
    candidates = [
        entity
        for entity in observation.get("entities") or []
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in names
        and isinstance(entity.get("position"), dict)
        and _entity_is_power_anchor(observation, entity)
    ]
    return _nearest_to(candidates, target) if candidates else None


def _entity_is_power_anchor(observation: dict[str, Any], entity: dict[str, Any]) -> bool:
    name = str(entity.get("name") or "")
    if name in POWER_CONNECTOR_NAMES:
        if entity.get("electric_network_connected") is True:
            return True
        network_id = entity.get("electric_network_id")
        return network_id is not None and _electric_network_has_power_source(observation, network_id)
    return entity.get("electric_network_connected") is True


def _electric_network_has_power_source(observation: dict[str, Any], network_id: Any) -> bool:
    if network_id is None:
        return False
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        if entity.get("electric_network_id") != network_id:
            continue
        if str(entity.get("name") or "") in {"steam-engine", "steam-turbine", "solar-panel", "accumulator"}:
            return entity.get("electric_network_connected") is not False
    return False


def _build_item_mall_power_bridge_gap_position(
    observation: dict[str, Any],
    target_position: dict[str, float],
    target_pole: dict[str, Any] | None = None,
) -> dict[str, float] | None:
    anchor = _nearest_connected_power_anchor(observation, target_position)
    if not isinstance(anchor, dict):
        return None
    source_network_id = anchor.get("electric_network_id")
    target_network_id = target_pole.get("electric_network_id") if isinstance(target_pole, dict) else None
    if source_network_id is None or target_network_id is None or source_network_id == target_network_id:
        return None

    reach = _power_wire_reach("small-electric-pole")
    power_entities = [
        entity
        for entity in observation.get("entities") or []
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in (POWER_CONNECTOR_NAMES | {"steam-engine", "steam-turbine", "solar-panel", "accumulator"})
        and isinstance(entity.get("position"), dict)
    ]
    source_candidates = [
        entity
        for entity in power_entities
        if entity is anchor
        or entity.get("electric_network_connected") is True
        or entity.get("electric_network_id") == source_network_id
    ]
    target_candidates = [
        entity
        for entity in power_entities
        if entity.get("electric_network_id") == target_network_id
        or (isinstance(target_pole, dict) and entity.get("unit_number") == target_pole.get("unit_number"))
    ]
    best: tuple[float, dict[str, float]] | None = None
    for source in source_candidates:
        source_position = _power_corridor_wire_anchor_position(source)
        for target in target_candidates:
            target_anchor = _power_corridor_wire_anchor_position(target)
            span = distance(source_position, target_anchor)
            if span <= reach or span > (reach * 2.0):
                continue
            bridge = _bridge_power_corridor_position(observation, source_position, target_anchor)
            if _existing_power_connector_near_position(observation, bridge, radius=1.6) is not None:
                continue
            if _power_corridor_position_blocker(observation, bridge) is not None:
                continue
            score = distance(bridge, target_position) + (span / 100.0)
            if best is None or score < best[0]:
                best = (score, bridge)
    return best[1] if best is not None else None


def _missing_power_corridor_positions(
    observation: dict[str, Any],
    positions: list[dict[str, float]],
) -> list[dict[str, float]]:
    if positions and _existing_power_corridor_reaches_position(observation, positions[-1]):
        return []
    missing: list[dict[str, float]] = []
    for position in positions:
        if any(
            _entity_at_build_position(observation, name, position, radius=1.6) is not None
            for name in POWER_CONNECTOR_NAMES
        ):
            continue
        missing.append(position)
    if missing:
        return missing
    gap_position = _first_power_corridor_gap_position(observation, positions)
    if gap_position is not None:
        return [gap_position]
    return missing


def _first_power_corridor_gap_position(
    observation: dict[str, Any],
    positions: list[dict[str, float]],
) -> dict[str, float] | None:
    if not positions:
        return None
    anchor = _nearest_connected_power_anchor(observation, positions[-1])
    if anchor is None:
        return None
    previous = _power_corridor_wire_anchor_position(anchor)
    reach = _power_wire_reach("small-electric-pole")
    for planned in positions:
        existing = _existing_power_connector_near_position(observation, planned, radius=1.6)
        actual = _position(existing) if isinstance(existing, dict) else planned
        if distance(previous, actual) <= reach:
            if isinstance(existing, dict):
                previous = actual
                continue
            return planned
        return _bridge_power_corridor_position(observation, previous, actual)
    return None


def _existing_power_connector_near_position(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    radius: float,
) -> dict[str, Any] | None:
    for name in POWER_CONNECTOR_NAMES:
        entity = _entity_at_build_position(observation, name, position, radius=radius)
        if entity is not None:
            return entity
    return None


def _bridge_power_corridor_position(
    observation: dict[str, Any],
    previous: dict[str, float],
    target: dict[str, float],
) -> dict[str, float]:
    midpoint = {"x": (previous["x"] + target["x"]) / 2.0, "y": (previous["y"] + target["y"]) / 2.0}
    reach = _power_wire_reach("small-electric-pole")
    for candidate in _nearby_power_pole_center_positions(midpoint, radius=4.0):
        if distance(candidate, previous) > reach or distance(candidate, target) > reach:
            continue
        if _power_corridor_position_blocker(observation, candidate) is None:
            return candidate
    return {"x": _round_power_pole_center(midpoint["x"]), "y": _round_power_pole_center(midpoint["y"])}


def _first_unconnected_power_corridor_pole(
    observation: dict[str, Any],
    positions: list[dict[str, float]],
) -> dict[str, Any] | None:
    if positions and _existing_power_corridor_reaches_position(observation, positions[-1]):
        return None
    source_network_id = _power_corridor_source_network_id(observation, positions)
    for position in positions:
        pole = _entity_at_build_position(observation, "small-electric-pole", position, radius=1.6)
        if not isinstance(pole, dict):
            continue
        pole_network_id = pole.get("electric_network_id")
        if source_network_id is not None and pole_network_id == source_network_id:
            continue
        if pole.get("electric_network_connected") is False:
            return pole
    return None


def _power_corridor_source_network_id(
    observation: dict[str, Any],
    positions: list[dict[str, float]],
) -> Any:
    if not positions:
        return None
    anchor = _nearest_connected_power_anchor(observation, positions[-1])
    if not isinstance(anchor, dict):
        return None
    return anchor.get("electric_network_id")


def _existing_power_corridor_reaches_position(
    observation: dict[str, Any],
    target: dict[str, float],
) -> bool:
    poles = [pole for pole in _power_poles(observation) if isinstance(pole.get("position"), dict)]
    if not poles:
        return False
    reach = _power_wire_reach("small-electric-pole")
    connected_sources = [
        entity
        for entity in observation.get("entities") or []
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in {"steam-engine", "steam-turbine", "solar-panel", "accumulator"}
        and entity.get("electric_network_connected") is True
        and isinstance(entity.get("position"), dict)
    ]
    visited_units: set[Any] = {
        pole.get("unit_number")
        for pole in poles
        if _entity_is_power_anchor(observation, pole)
        or any(distance(_position(pole), _position(source)) <= 3.0 for source in connected_sources)
    }
    if not visited_units:
        return False
    changed = True
    while changed:
        changed = False
        connected_poles = [pole for pole in poles if pole.get("unit_number") in visited_units]
        for pole in poles:
            unit = pole.get("unit_number")
            if unit in visited_units:
                continue
            if any(distance(_position(pole), _position(connected)) <= reach for connected in connected_poles):
                visited_units.add(unit)
                changed = True
    return any(
        pole.get("unit_number") in visited_units and distance(_position(pole), target) <= 1.6
        for pole in poles
    )


def _select_power_corridor_build_position(
    observation: dict[str, Any],
    corridor_positions: list[dict[str, float]],
    desired_position: dict[str, float],
) -> dict[str, float]:
    desired_key = _position_tuple(desired_position)
    try:
        desired_index = next(
            index for index, position in enumerate(corridor_positions) if _position_tuple(position) == desired_key
        )
    except StopIteration:
        desired_index = -1
    previous_anchor = _previous_power_corridor_anchor(observation, corridor_positions, desired_index)
    candidates = _nearby_power_pole_center_positions(desired_position, radius=6.0)
    reach = _power_wire_reach("small-electric-pole")
    for candidate in candidates:
        if previous_anchor is not None and distance(candidate, previous_anchor) > reach:
            continue
        if _existing_power_connector_near_position(observation, candidate, radius=0.75) is not None:
            continue
        if _power_corridor_position_blocker(observation, candidate) is None:
            return candidate
    return desired_position


def _previous_power_corridor_anchor(
    observation: dict[str, Any],
    corridor_positions: list[dict[str, float]],
    desired_index: int,
) -> dict[str, float] | None:
    if desired_index <= 0:
        target = corridor_positions[0] if corridor_positions else {"x": 0.0, "y": 0.0}
        anchor = _nearest_connected_power_anchor(observation, target)
        return _position(anchor) if anchor is not None else None
    for index in range(desired_index - 1, -1, -1):
        planned = corridor_positions[index]
        candidates = [
            entity
            for name in POWER_CONNECTOR_NAMES
            if (entity := _entity_at_build_position(observation, name, planned, radius=2.0)) is not None
        ]
        if candidates:
            return _position(candidates[0])
    return None


def _nearby_half_tile_positions(position: dict[str, float], *, radius: float) -> list[dict[str, float]]:
    base_x = float(position.get("x") or 0.0)
    base_y = float(position.get("y") or 0.0)
    offsets: list[tuple[float, float]] = []
    steps = int(radius * 2)
    for dx_step in range(-steps, steps + 1):
        for dy_step in range(-steps, steps + 1):
            dx = dx_step / 2.0
            dy = dy_step / 2.0
            if (dx * dx + dy * dy) ** 0.5 > radius:
                continue
            offsets.append((dx, dy))
    offsets.sort(key=lambda item: (item[0] * item[0] + item[1] * item[1], abs(item[1]), abs(item[0])))
    return [{"x": _round_half(base_x + dx), "y": _round_half(base_y + dy)} for dx, dy in offsets]


def _nearby_power_pole_center_positions(position: dict[str, float], *, radius: float) -> list[dict[str, float]]:
    base_x = _round_power_pole_center(float(position.get("x") or 0.0))
    base_y = _round_power_pole_center(float(position.get("y") or 0.0))
    offsets: list[tuple[float, float]] = []
    steps = int(radius)
    for dx in range(-steps, steps + 1):
        for dy in range(-steps, steps + 1):
            if (dx * dx + dy * dy) ** 0.5 > radius:
                continue
            offsets.append((float(dx), float(dy)))
    offsets.sort(key=lambda item: (item[0] * item[0] + item[1] * item[1], abs(item[1]), abs(item[0])))
    return [{"x": round(base_x + dx, 1), "y": round(base_y + dy, 1)} for dx, dy in offsets]


def _power_corridor_position_blocker(
    observation: dict[str, Any],
    position: dict[str, float],
) -> dict[str, Any] | None:
    blocker = _build_position_blocker(observation, position, allowed_names=POWER_CONNECTOR_NAMES)
    if blocker is not None:
        return blocker
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        if _is_preserved_starter_artifact(observation, entity) and distance(_position(entity), position) <= 6.0:
            return entity
        entity_type = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if (entity_type in {"simple-entity", "tree", "cliff"} or name.endswith("rock")) and distance(_position(entity), position) <= 1.25:
            return entity
    return None


def _dedupe_positions(positions: list[dict[str, float]]) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    seen: set[tuple[float, float]] = set()
    for position in positions:
        key = _position_tuple(position)
        if key in seen:
            continue
        seen.add(key)
        rows.append(position)
    return rows


def _round_half(value: float) -> float:
    return round(round(float(value) * 2.0) / 2.0, 1)


def _round_power_pole_center(value: float) -> float:
    return round(floor(float(value)) + 0.5, 1)


def _round_entity_center(value: float) -> float:
    return round(floor(float(value)) + 0.5, 1)


class IronPlateLogisticLineToGearMallSkill:
    """Incrementally build a belt route from iron-plate output to the gear mall."""

    def __init__(self, target_segments: int = 40) -> None:
        self.target_segments = target_segments
        self.research_skill = ResearchAutomationSkill()

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not _automation_researched(observation):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        layout = _find_iron_plate_logistic_line_to_gear_mall_layout(observation)
        if layout is None:
            return PlannerDecision(
                None,
                "cannot find both an iron-plate source furnace and a powered iron-gear mall assembler",
            )

        belt_assembler = layout.get("belt_assembler")
        missing_belt_segments = [segment for segment in layout["segments"] if not isinstance(segment.get("entity"), dict)]
        if missing_belt_segments and inventory_count(observation, "transport-belt") <= 0:
            belt_chest = _transport_belt_output_chest(observation)
            if isinstance(belt_chest, dict) and entity_item_count(belt_chest, "transport-belt") > 0:
                position = _position(belt_chest)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        "move near belt mall output chest to collect transport belts for iron-plate logistics construction",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "transport-belt",
                        "count": min(entity_item_count(belt_chest, "transport-belt"), max(1, self.target_segments)),
                        "unit_number": belt_chest.get("unit_number"),
                        "name": belt_chest.get("name") or "wooden-chest",
                        "position": position,
                    },
                    "take transport belts from the belt mall output chest as construction material for the iron-plate logistics line",
                )
            if isinstance(belt_assembler, dict) and entity_item_count(belt_assembler, "transport-belt") > 0:
                position = _position(belt_assembler)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        "move near belt mall output to collect transport belts for iron-plate logistics construction",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "transport-belt",
                        "count": min(entity_item_count(belt_assembler, "transport-belt"), max(1, self.target_segments)),
                        "unit_number": belt_assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": position,
                    },
                    "take transport belts from the belt mall as construction material for the iron-plate logistics line",
                )
            buffered_belts = _nearest_buffered_chest_item_source(observation, "transport-belt", player)
            if isinstance(buffered_belts, dict) and entity_item_count(buffered_belts, "transport-belt") > 0:
                position = _position(buffered_belts)
                if distance(player, position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": position},
                        "move near buffered transport-belt chest for iron-plate logistics construction",
                    )
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": "transport-belt",
                        "count": min(entity_item_count(buffered_belts, "transport-belt"), max(1, self.target_segments)),
                        "unit_number": buffered_belts.get("unit_number"),
                        "name": buffered_belts.get("name") or "wooden-chest",
                        "position": position,
                    },
                    "take buffered transport belts as construction material for the iron-plate logistics line",
                )
            return PlannerDecision(
                None,
                "iron-plate logistics line needs transport belts from the belt mall; refusing gear handcraft or iron-plate hand-carry",
            )

        protected_units = {
            int(layout["source"].get("unit_number"))
        } if isinstance(layout.get("source"), dict) and layout["source"].get("unit_number") is not None else set()
        for segment in layout["segments"]:
            existing = segment.get("entity")
            if isinstance(existing, dict):
                if _direction_or_default(existing.get("direction"), segment["direction"]) != int(segment["direction"]):
                    position = _position(existing)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of misoriented iron-plate logistics belt",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": existing.get("unit_number"),
                            "name": "transport-belt",
                            "position": position,
                        },
                        "remove misoriented transport belt from the iron-plate logistics line",
                    )
                continue
            blocker = _belt_line_position_blocker(
                observation,
                segment["position"],
                protected_unit_numbers=protected_units,
            )
            if blocker is not None:
                blocker_position = _position(blocker)
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before extending iron-plate logistics belt",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} before extending iron-plate logistics belt",
                )
            position = segment["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near next iron-plate logistics belt segment",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "transport-belt",
                    "position": position,
                    "direction": segment["direction"],
                    "allow_nearby": False,
                },
                "extend iron-plate belt logistics toward the gear mall without player plate shuttle",
            )

        source_recovery = _iron_plate_line_source_recovery_decision(observation, player, layout.get("source"))
        if source_recovery is not None:
            return source_recovery

        for spec, label in [
            (layout["source_inserter"], "iron source output inserter"),
            (layout["target_inserter"], "gear mall iron input inserter"),
        ]:
            inserter = spec.get("entity")
            if isinstance(inserter, dict):
                if _direction_or_default(inserter.get("direction"), 0) != int(spec["direction"]):
                    position = _position(inserter)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            f"move within reach of misoriented {label}",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": inserter.get("unit_number"),
                            "name": inserter.get("name") or "inserter",
                            "position": position,
                        },
                        f"remove misoriented {label} before rebuilding the iron-plate logistics endpoint",
                    )
                if (
                    str(inserter.get("name") or "") == "burner-inserter"
                ):
                    position = _position(inserter)
                    if _regular_inserter_can_be_used(observation):
                        if distance(player, position) > 4.5:
                            return PlannerDecision(
                                {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                                f"move within reach of obsolete {label} burner inserter",
                            )
                        return PlannerDecision(
                            {
                                "type": "mine",
                                "unit_number": inserter.get("unit_number"),
                                "name": "burner-inserter",
                                "position": position,
                            },
                            f"replace {label} burner inserter with a powered inserter",
                        )
                    if not _entity_status_is(inserter, "no_fuel", 53):
                        continue
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            f"move near obsolete {label} burner inserter",
                        )
                    return PlannerDecision(None, f"{label} needs a powered inserter; refusing to fuel burner inserter")
                if str(inserter.get("name") or "") != "burner-inserter" and inserter.get("electric_network_connected") is False:
                    power_decision = _logistics_line_powered_inserter_decision(
                        observation,
                        player,
                        inserter,
                        label,
                    )
                    if power_decision is not None:
                        return power_decision
                continue
            item_name = _available_logistics_line_inserter_item(observation)
            if item_name is None:
                material_decision = _logistics_line_inserter_material_decision(observation, player, layout, label)
                if material_decision is not None:
                    return material_decision
                protected_endpoint_units = {
                    int(endpoint.get("entity", {}).get("unit_number"))
                    for endpoint in (layout["source_inserter"], layout["target_inserter"])
                    if isinstance(endpoint.get("entity"), dict)
                    and endpoint.get("entity", {}).get("unit_number") is not None
                }
                protected_endpoint_positions = [
                    endpoint["position"]
                    for endpoint in (layout["source_inserter"], layout["target_inserter"])
                    if isinstance(endpoint.get("entity"), dict)
                ]
                reusable = _find_relocatable_inserter_for_iron_plate_line(
                    observation,
                    spec["position"],
                    exclude_unit_numbers=protected_endpoint_units,
                    protected_positions=protected_endpoint_positions,
                )
                if reusable is not None:
                    position = _position(reusable)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            f"move within reach of reusable inserter for {label}",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": reusable.get("name") or "inserter",
                            "position": position,
                        },
                        f"relocate existing inserter for {label} instead of hand-crafting gears",
                    )
                return PlannerDecision(None, f"missing inserter for {label}; refusing hand-crafted iron gears")
            position = spec["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    f"move near {label} position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": item_name,
                    "position": position,
                    "direction": spec["direction"],
                    "allow_nearby": False,
                },
                f"place {label} for automated iron-plate delivery to the gear mall",
            )

        return PlannerDecision(
            None,
            "iron-plate logistics line to the gear mall is built with belts and endpoint inserters",
            done=True,
        )


class SiteInputLogisticLineSkill:
    """Build a belt route for a repeated producer-to-consumer factory input."""

    def __init__(self, target_segments: int = 40, item: str | None = None) -> None:
        self.target_segments = target_segments
        self.item = item
        self.research_skill = ResearchAutomationSkill()

    def next_action(self, observation: dict[str, Any]) -> PlannerDecision:
        player = player_position(observation)
        if not _automation_researched(observation):
            decision = self.research_skill.next_action(observation)
            if decision.done:
                return PlannerDecision({"type": "wait", "ticks": 120}, "wait for automation unlock observation to settle")
            return decision

        if not _transport_belt_assembler_exists(observation):
            return PlannerDecision(
                None,
                "site input logistics need automated transport-belt production before building repeated site-to-site routes",
            )

        layout = _find_site_input_logistic_line_layout(observation, item=self.item)
        if layout is None:
            return PlannerDecision(None, "no executable repeated site input logistics route was found")

        belt_assembler = _transport_belt_output_assembler(observation)
        source_recovery: PlannerDecision | None = None
        if layout.get("item") in {"iron-plate", "copper-plate"}:
            source_recovery = _plate_line_source_recovery_decision(
                observation,
                player,
                layout.get("source"),
                str(layout["item"]),
            )
            if source_recovery is not None and (source_recovery.action or {}).get("type") != "wait":
                return source_recovery

        protected_units = {
            int(entity.get("unit_number"))
            for entity in (layout.get("source"), layout.get("consumer"))
            if isinstance(entity, dict) and entity.get("unit_number") is not None
        }
        bridge = _site_input_underground_bridge_plan(observation, layout)
        if bridge is not None:
            bridge_decision = _site_input_underground_bridge_decision(observation, player, bridge)
            if bridge_decision is not None:
                return bridge_decision
        splitter = layout.get("splitter")
        if isinstance(splitter, dict):
            splitter_decision = _site_input_splitter_fanout_decision(observation, player, splitter)
            if splitter_decision is not None:
                return splitter_decision
        for segment in layout["segments"]:
            if _site_input_segment_is_splitter(layout, segment):
                continue
            existing = segment.get("entity")
            if not isinstance(existing, dict):
                continue
            if _direction_or_default(existing.get("direction"), segment["direction"]) == int(segment["direction"]):
                continue
            position = _position(existing)
            if distance(player, position) > 4.5:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                    "move within reach of misoriented site input logistics belt",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": existing.get("unit_number"),
                    "name": "transport-belt",
                    "position": position,
                },
                "remove misoriented transport belt from the site input logistics line before spending scarce belts",
            )
        nearest_buildable_segment = _nearest_buildable_missing_site_input_segment(
            observation,
            layout,
            player,
            protected_units=protected_units,
        )
        for segment in layout["segments"]:
            if _site_input_segment_is_splitter(layout, segment):
                continue
            existing = segment.get("entity")
            if isinstance(existing, dict):
                if _direction_or_default(existing.get("direction"), segment["direction"]) != int(segment["direction"]):
                    position = _position(existing)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            "move within reach of misoriented site input logistics belt",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": existing.get("unit_number"),
                            "name": "transport-belt",
                            "position": position,
                        },
                        "remove misoriented transport belt from the site input logistics line",
                    )
                continue
            blocker = _belt_line_position_blocker(
                observation,
                segment["position"],
                protected_unit_numbers=protected_units,
            )
            if blocker is not None:
                blocker_position = _position(blocker)
                if _site_input_hard_route_blocker(blocker, segment["position"]):
                    return PlannerDecision(
                        None,
                        f"site input logistics route is blocked by existing {blocker.get('name')}; needs a reroute instead of mining production infrastructure",
                    )
                if distance(player, blocker_position) > 8:
                    return PlannerDecision(
                        {"type": "move_to", "position": blocker_position},
                        f"move near blocking {blocker.get('name')} before extending site input logistics belt",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": blocker.get("unit_number"),
                        "name": blocker.get("name"),
                        "position": blocker_position,
                    },
                    f"clear blocking {blocker.get('name')} before extending site input logistics belt",
                )
            if inventory_count(observation, "transport-belt") <= 0:
                belt_chest = _transport_belt_output_chest(observation)
                if isinstance(belt_chest, dict) and entity_item_count(belt_chest, "transport-belt") > 0:
                    position = _position(belt_chest)
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            "move near belt mall output chest to collect transport belts for site input logistics",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "transport-belt",
                            "count": min(entity_item_count(belt_chest, "transport-belt"), max(1, self.target_segments)),
                            "unit_number": belt_chest.get("unit_number"),
                            "name": belt_chest.get("name") or "wooden-chest",
                            "position": position,
                        },
                        "take transport belts from the belt mall output chest as construction material for site input logistics",
                    )
                if isinstance(belt_assembler, dict) and entity_item_count(belt_assembler, "transport-belt") > 0:
                    position = _position(belt_assembler)
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            "move near belt mall output to collect transport belts for site input logistics",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "transport-belt",
                            "count": min(entity_item_count(belt_assembler, "transport-belt"), max(1, self.target_segments)),
                            "unit_number": belt_assembler.get("unit_number"),
                            "name": belt_assembler.get("name") or "assembling-machine-1",
                            "position": position,
                        },
                        "take transport belts from the belt mall as construction material for site input logistics",
                    )
                buffered_belts = _nearest_buffered_chest_item_source(observation, "transport-belt", player)
                if isinstance(buffered_belts, dict) and entity_item_count(buffered_belts, "transport-belt") > 0:
                    position = _position(buffered_belts)
                    if distance(player, position) > 20:
                        return PlannerDecision(
                            {"type": "move_to", "position": position},
                            "move near buffered transport-belt chest for site input logistics",
                        )
                    return PlannerDecision(
                        {
                            "type": "take",
                            "item": "transport-belt",
                            "count": min(entity_item_count(buffered_belts, "transport-belt"), max(1, self.target_segments)),
                            "unit_number": buffered_belts.get("unit_number"),
                            "name": buffered_belts.get("name") or "wooden-chest",
                            "position": position,
                        },
                        "take buffered transport belts as construction material for site input logistics",
                    )
                return PlannerDecision(
                    None,
                    "site input logistics needs transport belts from the belt mall; refusing hand-crafted belts or item shuttle",
                )
            target_segment = nearest_buildable_segment or segment
            position = target_segment["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    "move near nearest buildable site input logistics belt segment",
                )
            if target_segment is not segment:
                return PlannerDecision(
                    {
                        "type": "build",
                        "name": "transport-belt",
                        "position": position,
                        "direction": target_segment["direction"],
                        "allow_nearby": False,
                    },
                    f"extend reachable {layout['item']} site input belt segment before walking to another route span",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": "transport-belt",
                    "position": position,
                    "direction": segment["direction"],
                    "allow_nearby": False,
                },
                f"extend {layout['item']} site input belt without player item shuttle",
            )

        for spec, label in [
            (layout["source_inserter"], "site source output inserter"),
            (layout["target_inserter"], "site consumer input inserter"),
        ]:
            inserter = spec.get("entity")
            if isinstance(inserter, dict):
                if str(inserter.get("name") or "") != "burner-inserter" and inserter.get("electric_network_connected") is False:
                    power_decision = _logistics_line_powered_inserter_decision(
                        observation,
                        player,
                        inserter,
                        label,
                    )
                    if power_decision is not None:
                        return power_decision
                if _direction_or_default(inserter.get("direction"), 0) != int(spec["direction"]):
                    position = _position(inserter)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            f"move within reach of misoriented {label}",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": inserter.get("unit_number"),
                            "name": inserter.get("name") or "inserter",
                            "position": position,
                        },
                        f"remove misoriented {label} before rebuilding the site input endpoint",
                    )
                continue
            item_name = _available_logistics_line_inserter_item(observation)
            if item_name is None:
                material_decision = _logistics_line_inserter_material_decision(observation, player, layout, label)
                if material_decision is not None:
                    return material_decision
                protected_endpoint_units = {
                    int(endpoint.get("entity", {}).get("unit_number"))
                    for endpoint in (layout["source_inserter"], layout["target_inserter"])
                    if isinstance(endpoint.get("entity"), dict)
                    and endpoint.get("entity", {}).get("unit_number") is not None
                }
                protected_endpoint_positions = [
                    endpoint["position"]
                    for endpoint in (layout["source_inserter"], layout["target_inserter"])
                    if isinstance(endpoint.get("entity"), dict)
                ]
                reusable = _find_relocatable_inserter_for_iron_plate_line(
                    observation,
                    spec["position"],
                    exclude_unit_numbers=protected_endpoint_units,
                    protected_positions=protected_endpoint_positions,
                )
                if reusable is not None:
                    position = _position(reusable)
                    if distance(player, position) > 4.5:
                        return PlannerDecision(
                            {"type": "move_to", "position": _stand_position(position, offset=1.5)},
                            f"move within reach of reusable inserter for {label}",
                        )
                    return PlannerDecision(
                        {
                            "type": "mine",
                            "unit_number": reusable.get("unit_number"),
                            "name": reusable.get("name") or "inserter",
                            "position": position,
                        },
                        f"relocate existing inserter for {label} instead of hand-crafting gears",
                    )
                return PlannerDecision(None, f"missing inserter for {label}; refusing hand-crafted gear workaround")
            position = spec["position"]
            if distance(player, position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": _stand_position(position, offset=3.0)},
                    f"move near {label} position",
                )
            return PlannerDecision(
                {
                    "type": "build",
                    "name": item_name,
                    "position": position,
                    "direction": spec["direction"],
                    "allow_nearby": False,
                },
                f"place {label} for automated {layout['item']} delivery",
            )

        if source_recovery is not None:
            return source_recovery

        return PlannerDecision(
            None,
            f"{layout['item']} site input logistics line is built with belts and endpoint inserters",
            done=True,
        )


def _nearest_buildable_missing_site_input_segment(
    observation: dict[str, Any],
    layout: dict[str, Any],
    player: dict[str, float],
    *,
    protected_units: set[int],
) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for segment in layout.get("segments") or []:
        if not isinstance(segment, dict) or _site_input_segment_is_splitter(layout, segment):
            continue
        if isinstance(segment.get("entity"), dict):
            continue
        position = segment.get("position") if isinstance(segment.get("position"), dict) else None
        if position is None:
            continue
        if _belt_line_position_blocker(
            observation,
            position,
            protected_unit_numbers=protected_units,
        ) is not None:
            continue
        score = distance(player, position)
        if best is None or score < best[0]:
            best = (score, segment)
    return best[1] if best is not None else None


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

    def _effective_target_count(self, observation: dict[str, Any]) -> int:
        if self.target_item != "transport-belt":
            return self.target_count
        missing_boiler_route_belts = _boiler_coal_feed_missing_belt_count(observation)
        if missing_boiler_route_belts <= 0:
            return self.target_count
        available_belts = _available_boiler_feed_construction_belts(observation)
        if available_belts >= missing_boiler_route_belts:
            return self.target_count
        return max(self.target_count, missing_boiler_route_belts + 4)

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
        target_count = self._effective_target_count(observation)

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
            decision = self._pre_power_recipe_retool_decision(
                observation,
                player,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
            if decision is not None:
                return decision
            decision = self.power_skill.next_action(
                observation,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
                allow_bootstrap_power_seed=(
                    self.target_item in {"transport-belt", "iron-gear-wheel", "assembling-machine-1", "small-electric-pole"}
                    and _is_virtual_agent(observation)
                ),
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
        ) or _select_build_item_mall_sidecar_from_existing_gear_cell(
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
            corridor_repair = self._power_corridor_repair_decision(observation, player, cell)
            if corridor_repair is not None:
                return corridor_repair
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

        assembler_position = _position(assembler)
        if assembler.get("recipe") != self.target_item:
            incompatible = _first_incompatible_assembler_item(assembler, self.target_item)
            if self.target_item == "transport-belt" and incompatible is not None:
                if distance(player, assembler_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to clear {incompatible}")
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": incompatible,
                        "count": entity_item_count(assembler, incompatible),
                        "unit_number": assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": assembler_position,
                    },
                    f"clear {incompatible} from reusable build item mall assembler before setting {self.target_item}",
                )
            return self._set_recipe_decision(player, assembler, self.target_item)
        if self.target_item == "transport-belt" and _transport_belt_mall_assembler_too_close_to_gear_mall(
            observation,
            assembler,
        ):
            if distance(player, assembler_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": assembler_position},
                    "move near cramped transport-belt mall assembler before relocating it",
                )
            return PlannerDecision(
                {
                    "type": "mine",
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                "relocate transport-belt mall assembler to leave a direct inserter gap from the gear assembler",
            )

        cleanup_decision = _obsolete_build_item_mall_buffer_cleanup_decision(observation, player, cell)
        if cleanup_decision is not None:
            return cleanup_decision

        output_count = entity_item_count(assembler, self.target_item)
        if output_count > 0:
            block_player_collection = _block_player_mall_output_collection_after_automation(observation, self.target_item)
            if block_player_collection and reference_position is not None:
                logistics_layout = _find_site_input_logistic_line_layout(observation, item="iron-gear-wheel")
                consumer = logistics_layout.get("consumer") if isinstance(logistics_layout, dict) else None
                if isinstance(consumer, dict) and distance(_position(consumer), reference_position) <= 4.5:
                    logistics_decision = SiteInputLogisticLineSkill(max(12, target_count), item="iron-gear-wheel").next_action(
                        observation
                    )
                    if not logistics_decision.done:
                        return logistics_decision
                    return PlannerDecision(
                        {"type": "wait", "ticks": 300},
                        f"wait for iron gear wheel site input logistics to feed {self.target_item} consumer",
                    )
            if _build_item_mall_should_use_output_chest(self.target_item):
                if not _build_item_mall_output_buffer_ready(cell, observation):
                    decision = self._ensure_output_buffer(observation, player, cell)
                    if decision is not None:
                        return decision
                if _build_item_mall_output_buffer_ready(cell, observation):
                    return PlannerDecision(
                        {"type": "wait", "ticks": 300},
                        f"wait for {self.target_item} mall output inserter to buffer items into chest",
                    )
            if block_player_collection:
                return PlannerDecision(
                    {"type": "wait", "ticks": 300},
                    (
                        f"wait for {self.target_item} mall output logistics; "
                        "refusing player collection of iron gear wheels after Automation"
                    ),
                )
            assembler_position = _position(assembler)
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near mall assembler to collect {self.target_item}")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": self.target_item,
                    "count": min(output_count, target_count),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"take {self.target_item} from build item mall assembler",
            )

        reached_count = _build_item_mall_available_product_count(
            observation,
            cell,
            self.target_item,
            reference_position=reference_position,
        )
        if _build_item_mall_should_use_output_chest(self.target_item):
            output_chest = cell.get("output_chest") if isinstance(cell, dict) else None
            if not isinstance(output_chest, dict) and reference_position is not None:
                output_chest = _nearest_buffered_chest_item_source(observation, self.target_item, reference_position)
            if isinstance(output_chest, dict) and (
                reference_position is None or distance(_position(output_chest), reference_position) <= 8.0
            ):
                reached_count = inventory_count(observation, self.target_item) + output_count + entity_item_count(
                    output_chest,
                    self.target_item,
                )

        if _build_item_mall_cell_ready(cell, self.target_item) and reached_count >= target_count:
            return PlannerDecision(
                None,
                f"build item mall is producing {self.target_item} and target reached: {reached_count}/{target_count}",
                done=True,
            )

        batch_count = _build_item_mall_batch_count(recipe.products.get(self.target_item, 1.0), target_count)
        if (
            self.target_item == "transport-belt"
            and entity_item_count(assembler, "iron-gear-wheel") > 0
            and entity_item_count(assembler, "iron-plate") <= 0
            and inventory_count(observation, "iron-plate") > 0
        ):
            logistics_blocker = _manual_site_input_logistics_blocker(
                observation,
                "iron-plate",
                _position(assembler),
                consumer_label=f"{self.target_item} mall assembler",
            )
            if logistics_blocker is not None:
                return logistics_blocker
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, "move near mall assembler to insert iron-plate")
            return _bootstrap_seed_decision(
                {
                    "type": "insert",
                    "item": "iron-plate",
                    "count": min(entity_item_count(assembler, "iron-gear-wheel"), inventory_count(observation, "iron-plate")),
                    "unit_number": assembler.get("unit_number"),
                    "name": "assembling-machine-1",
                    "position": assembler_position,
                },
                f"insert iron-plate into {self.target_item} mall assembler to consume buffered gears",
                seed_reason=f"{self.target_item}_mall_buffered_gear_plate_seed",
                expected_followup=f"{self.target_item} assembler produces output or exposes an output-buffer task",
            )

        if self.target_item == "assembling-machine-1":
            plates_per_craft = max(1, int(recipe.ingredients.get("iron-plate", 1)))
            gears_per_craft = max(1, int(recipe.ingredients.get("iron-gear-wheel", 1)))
            circuits_per_craft = max(1, int(recipe.ingredients.get("electronic-circuit", 1)))
            buffered_for_one = (
                entity_item_count(assembler, "iron-gear-wheel") >= gears_per_craft
                and entity_item_count(assembler, "electronic-circuit") >= circuits_per_craft
            )
            missing_plates = max(0, plates_per_craft - entity_item_count(assembler, "iron-plate"))
            if buffered_for_one and missing_plates > 0:
                if inventory_count(observation, "iron-plate") < missing_plates:
                    decision = self._ensure_item_quantity(
                        observation,
                        player,
                        "iron-plate",
                        missing_plates,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=assembler_position,
                    )
                    if decision is not None:
                        return decision
                logistics_blocker = _manual_site_input_logistics_blocker(
                    observation,
                    "iron-plate",
                    _position(assembler),
                    consumer_label=f"{self.target_item} mall assembler",
                )
                if logistics_blocker is not None:
                    return logistics_blocker
                if distance(player, assembler_position) > 20:
                    return PlannerDecision(
                        {"type": "move_to", "position": assembler_position},
                        "move near assembling-machine-1 mall assembler to insert first-craft iron plates",
                    )
                return _bootstrap_seed_decision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": min(missing_plates, inventory_count(observation, "iron-plate")),
                        "unit_number": assembler.get("unit_number"),
                        "name": "assembling-machine-1",
                        "position": assembler_position,
                    },
                    "insert iron-plate into assembling-machine-1 mall assembler to complete the first assembler craft",
                    seed_reason="assembling-machine-1_mall_first_craft_plate_seed",
                    expected_followup="assembling-machine-1 mall assembler produces output before requesting more gear seed",
                )

        ingredient_order = sorted(recipe.ingredients.items())
        if self.target_item == "automation-science-pack":
            copper_per_craft = max(1, int(recipe.ingredients.get("copper-plate", 1)))
            gear_per_craft = max(1, int(recipe.ingredients.get("iron-gear-wheel", 1)))
            if (
                entity_item_count(assembler, "copper-plate") >= copper_per_craft
                and entity_item_count(assembler, "iron-gear-wheel") < gear_per_craft
            ):
                ingredient_order.sort(key=lambda item: 0 if item[0] == "iron-gear-wheel" else 1)

        for ingredient, amount in ingredient_order:
            needed_in_assembler = max(1, int(amount * batch_count))
            if entity_item_count(assembler, ingredient) >= needed_in_assembler:
                continue
            if (
                self.target_item == "transport-belt"
                and ingredient == "iron-gear-wheel"
                and inventory_count(observation, ingredient) <= 0
            ):
                decision = _take_assembler_output_gears_for_infrastructure(
                    observation,
                    player,
                    needed_in_assembler,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=assembler_position,
                    exclude_units={assembler.get("unit_number")},
                    infrastructure_reason=f"{self.target_item} mall input bootstrap",
                )
                if decision is not None:
                    return decision
            logistics_blocker = _manual_site_input_logistics_blocker(
                observation,
                ingredient,
                _position(assembler),
                consumer_label=f"{self.target_item} mall assembler",
            )
            if logistics_blocker is not None:
                return logistics_blocker
            if inventory_count(observation, ingredient) <= 0:
                if self.target_item == "automation-science-pack" and ingredient == "iron-gear-wheel":
                    gear_chest = _nearest_buffered_chest_item_source(observation, ingredient, assembler_position)
                    if isinstance(gear_chest, dict) and distance(_position(gear_chest), assembler_position) <= 8.0:
                        position = _position(gear_chest)
                        if distance(player, position) > 20:
                            return PlannerDecision(
                                {"type": "move_to", "position": position},
                                "move near buffered gear chest for automation science input",
                            )
                        return PlannerDecision(
                            {
                                "type": "take",
                                "item": ingredient,
                                "count": min(entity_item_count(gear_chest, ingredient), needed_in_assembler),
                                "unit_number": gear_chest.get("unit_number"),
                                "name": gear_chest.get("name") or "wooden-chest",
                                "position": position,
                            },
                            "take chest-buffered iron gears for automation-science-pack mall input",
                        )
                allow_output_gears = (
                    ingredient == "iron-gear-wheel"
                    and self.target_item == "transport-belt"
                )
                decision = self._ensure_item_quantity(
                    observation,
                    player,
                    ingredient,
                    needed_in_assembler,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=assembler_position,
                    allow_assembler_output_gears=allow_output_gears,
                    exclude_assembler_output_units={assembler.get("unit_number")} if allow_output_gears else None,
                )
                if decision is not None:
                    return decision
                # Still nothing carried and we could not obtain it -> do NOT emit an insert with
                # count 0 (the executor raises ActionValidationError: "count must be positive", which
                # crashes the whole cycle). Yield so the strategy/watchdog can try another approach.
                return PlannerDecision(
                    None,
                    f"cannot obtain {ingredient} to feed the {self.target_item} mall assembler yet",
                )
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

        if _build_item_mall_should_use_output_chest(self.target_item):
            decision = self._ensure_output_buffer(observation, player, cell)
            if decision is not None:
                return decision

        return PlannerDecision({"type": "wait", "ticks": 600}, f"wait for build item mall to produce {self.target_item}")

    def _power_corridor_repair_decision(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        cell: dict[str, Any],
    ) -> PlannerDecision | None:
        target_pole = cell.get("pole") if isinstance(cell.get("pole"), dict) else None
        target_position = _position(target_pole) if isinstance(target_pole, dict) else cell.get("pole_position")
        if not isinstance(target_position, dict):
            assembler = cell.get("assembler")
            target_position = _position(assembler) if isinstance(assembler, dict) else None
        if not isinstance(target_position, dict):
            return None

        bridge_position = _build_item_mall_power_bridge_gap_position(observation, target_position, target_pole)
        if bridge_position is None:
            return None
        if inventory_count(observation, "small-electric-pole") <= 0:
            decision = self.power_skill._ensure_item_quantity(observation, player, "small-electric-pole", 1)
            if decision is not None:
                return decision
        if distance(player, bridge_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": _stand_position(bridge_position)},
                f"move near disconnected {self.target_item} mall power corridor gap",
            )
        return PlannerDecision(
            {
                "type": "build",
                "name": "small-electric-pole",
                "position": bridge_position,
                "allow_nearby": False,
            },
            f"bridge disconnected {self.target_item} mall power corridor before retrying power connection",
        )

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

    def _ensure_output_buffer(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        cell: dict[str, Any],
    ) -> PlannerDecision | None:
        if _build_item_mall_output_buffer_ready(cell, observation):
            return None

        chest_position = cell.get("output_chest_position")
        inserter_position = cell.get("output_inserter_position")
        if not isinstance(chest_position, dict) or not isinstance(inserter_position, dict):
            return PlannerDecision(None, f"cannot find clear output chest connection for {self.target_item} mall")

        chest = cell.get("output_chest")
        if not isinstance(chest, dict):
            chest_name = _available_stone_output_chest_name(observation)
            if chest_name is None:
                needed = _build_item_mall_missing_output_chest_item(observation)
                decision = self._ensure_item_quantity(observation, player, needed, 1)
                if decision is not None:
                    return decision
                return None
            if distance(player, chest_position) > 20:
                return PlannerDecision({"type": "move_to", "position": _stand_position(chest_position)}, f"move near {self.target_item} mall output chest")
            return PlannerDecision(
                {
                    "type": "build",
                    "name": chest_name,
                    "position": chest_position,
                    "allow_nearby": False,
                },
                f"place output chest for {self.target_item} mall",
            )

        decision = self._ensure_output_inserter(observation, player, cell)
        if decision is not None:
            return decision
        return None

    def _ensure_output_inserter(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        cell: dict[str, Any],
    ) -> PlannerDecision | None:
        inserter = cell.get("output_inserter")
        position = cell["output_inserter_position"]
        direction = int(cell["output_inserter_direction"])
        if isinstance(inserter, dict):
            if inserter.get("name") == "burner-inserter":
                inserter_position = _position(inserter)
                if not _regular_inserter_can_be_used(observation):
                    if not _entity_status_is(inserter, "no_fuel", 53):
                        return None
                    return PlannerDecision(
                        None,
                        f"{self.target_item} mall output needs a powered inserter; refusing to fuel burner inserter",
                    )
                if distance(player, inserter_position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(inserter_position, offset=1.5)},
                        f"move within mining reach of obsolete {self.target_item} mall output burner inserter",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": inserter.get("unit_number"),
                        "name": "burner-inserter",
                        "position": inserter_position,
                    },
                    f"replace {self.target_item} mall output burner inserter now that regular inserters are usable",
                )
            if _direction_or_default(inserter.get("direction"), 0) != direction:
                inserter_position = _position(inserter)
                if distance(player, inserter_position) > 4.5:
                    return PlannerDecision(
                        {"type": "move_to", "position": _stand_position(inserter_position, offset=1.5)},
                        f"move within mining reach of misoriented {self.target_item} mall output inserter",
                    )
                return PlannerDecision(
                    {
                        "type": "mine",
                        "unit_number": inserter.get("unit_number"),
                        "name": inserter.get("name") or "inserter",
                        "position": inserter_position,
                    },
                    f"remove misoriented {self.target_item} mall output inserter",
                )
            if inserter.get("name") != "burner-inserter" and inserter.get("electric_network_connected") is False:
                existing = _nearest_connected_small_pole_supplying_position(observation, _position(inserter))
                if existing is None:
                    existing = _nearest_small_pole_supplying_position(observation, _position(inserter))
                if existing is not None:
                    pole_position = _position(existing)
                    if distance(player, pole_position) > 20:
                        return PlannerDecision({"type": "move_to", "position": pole_position}, f"move near output inserter supply pole for {self.target_item} mall")
                    return PlannerDecision(
                        {
                            "type": "connect_power",
                            "unit_number": existing.get("unit_number"),
                            "name": "small-electric-pole",
                            "position": pole_position,
                        },
                        f"connect output inserter supply pole for {self.target_item} mall",
                    )
                pole_position = _select_mall_inserter_power_pole_position(observation, _position(inserter))
                if pole_position is None:
                    return PlannerDecision(None, f"cannot find clear output inserter power pole for {self.target_item} mall")
                decision = self._ensure_item_quantity(observation, player, "small-electric-pole", 1)
                if decision is not None:
                    return decision
                if distance(player, pole_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": _stand_position(pole_position)}, f"move near output inserter pole for {self.target_item} mall")
                return PlannerDecision(
                    {"type": "build", "name": "small-electric-pole", "position": pole_position, "allow_nearby": False},
                    f"place output inserter power pole for {self.target_item} mall",
                )
            return None

        item_name = _available_build_item_mall_output_inserter_name(observation)
        if item_name is None:
            needed = _build_item_mall_missing_output_inserter_item(observation)
            decision = self._ensure_item_quantity(observation, player, needed, 1)
            if decision is not None:
                return decision
            return None
        if distance(player, position) > 20:
            return PlannerDecision({"type": "move_to", "position": _stand_position(position)}, f"move near {self.target_item} mall output inserter")
        return PlannerDecision(
            {
                "type": "build",
                "name": item_name,
                "position": position,
                "direction": direction,
                "allow_nearby": False,
            },
            f"place output inserter from {self.target_item} mall assembler to chest",
        )

    def _pre_power_recipe_retool_decision(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision | None:
        if self.target_item != "transport-belt":
            return None
        belt_cell = _find_build_item_mall_cell(
            observation,
            "transport-belt",
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
        belt_assembler = belt_cell.get("assembler") if isinstance(belt_cell, dict) else None
        if not isinstance(belt_assembler, dict) or str(belt_assembler.get("recipe") or "") != "transport-belt":
            return None
        recipe = RECIPES.get("transport-belt")
        if recipe is None:
            return None
        target_count = self._effective_target_count(observation)
        batch_count = _build_item_mall_batch_count(recipe.products.get("transport-belt", 1.0), target_count)
        needed_gears = max(1, int(recipe.ingredients.get("iron-gear-wheel", 1) * batch_count))
        if entity_item_count(belt_assembler, "iron-gear-wheel") >= needed_gears:
            return None
        if inventory_count(observation, "iron-gear-wheel") > 0:
            return None
        gear_cell = _find_build_item_mall_cell(
            observation,
            "iron-gear-wheel",
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position or _position(belt_assembler),
        )
        gear_assembler = gear_cell.get("assembler") if isinstance(gear_cell, dict) else None
        if not isinstance(gear_assembler, dict):
            return None
        if str(gear_assembler.get("recipe") or "") in {"iron-gear-wheel", "transport-belt"}:
            return None
        return self._set_recipe_decision(player, gear_assembler, "iron-gear-wheel")

    def _ensure_item_quantity(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        item: str,
        quantity: int,
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
        allow_first_assembler_gear_bootstrap: bool = False,
        allow_assembler_output_gears: bool = False,
        exclude_assembler_output_units: set[Any] | None = None,
    ) -> PlannerDecision | None:
        if inventory_count(observation, item) >= quantity:
            return None

        if item == "assembling-machine-1":
            decision = self._ensure_assembler_bootstrap_gears(
                observation,
                player,
                quantity,
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
            if decision is not None:
                return decision
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
                    allow_first_assembler_gear_bootstrap=(
                        prerequisite == "iron-gear-wheel" and _first_assembler_bootstrap_needed(observation, quantity)
                    ),
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
            if _gear_handcraft_automation_context_active(observation):
                if allow_assembler_output_gears:
                    decision = self._take_assembler_output_gears_for_infrastructure(
                        observation,
                        player,
                        quantity,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=reference_position,
                        exclude_units=exclude_assembler_output_units,
                    )
                    if decision is not None:
                        return decision
                if allow_first_assembler_gear_bootstrap and _can_handcraft_first_assembler_gears(observation, quantity):
                    current_count = inventory_count(observation, "iron-gear-wheel")
                    return PlannerDecision(
                        {
                            "type": "craft",
                            "recipe": "iron-gear-wheel",
                            "count": min(
                                quantity - current_count,
                                craftable_count(observation, "iron-gear-wheel"),
                                max(0, 5 - current_count),
                            ),
                            "allow_first_assembler_bootstrap": True,
                        },
                        "craft one-time iron gears for first assembling-machine-1 bootstrap",
                    )
                if self.target_item != "iron-gear-wheel":
                    logistics_layout = _find_site_input_logistic_line_layout(observation, item="iron-gear-wheel")
                    if logistics_layout is not None:
                        consumer = logistics_layout.get("consumer")
                        if (
                            reference_position is None
                            or (isinstance(consumer, dict) and distance(_position(consumer), reference_position) <= 4.5)
                        ):
                            logistics_decision = SiteInputLogisticLineSkill(max(12, quantity), item="iron-gear-wheel").next_action(
                                observation
                            )
                            if not logistics_decision.done:
                                return logistics_decision
                            return PlannerDecision(
                                {"type": "wait", "ticks": 300},
                                f"wait for iron gear wheel site input logistics to feed {self.target_item} assembler",
                            )
                    decision = BuildItemMallSkill("iron-gear-wheel", max(quantity, 4)).next_action(
                        observation,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=reference_position,
                    )
                    if not decision.done:
                        return decision
                    # The gear mall cannot produce gears right now (done) and no gear logistic line
                    # exists. A walking player would wait for the mall, but that DEADLOCKS the whole
                    # factory for the VIRTUAL agent (observed live 2026-06-19: belt mall "cannot obtain
                    # iron-gear-wheel" -> no belts -> coal feed refuses -> power dies). The virtual agent
                    # teleports and hand-craft is free, so seed gears by hand to unblock the downstream
                    # mall; real gear logistics resume once a gear assembler produces.
                    if _is_virtual_agent(observation) and craftable_count(observation, "iron-gear-wheel") > 0:
                        current_gears = inventory_count(observation, "iron-gear-wheel")
                        return _bootstrap_seed_decision(
                            {
                                "type": "craft",
                                "recipe": "iron-gear-wheel",
                                "count": min(quantity - current_gears, craftable_count(observation, "iron-gear-wheel")),
                            },
                            f"virtual agent hand-craft gears to seed {self.target_item} mall (gear mall cannot produce yet)",
                            seed_reason=f"virtual_agent_{self.target_item}_mall_gear_seed",
                            expected_followup=f"{self.target_item} assembler receives gears and produces automated output",
                        )
                    return None
                # Genuine bootstrap: the post-Automation policy wants gears from an assembler, but if
                # NO assembler is producing gears there is nothing to supply them and refusing forever
                # wedges the autopilot (the observed 'rebuild gear mall every cycle, action=null' loop
                # -- which recurs once a lone belt assembler exists). Break the chicken-and-egg:
                # bootstrap enough gears to build the first GEAR assembler.
                if not _gear_producing_assembler_available(observation):
                    bootstrap = self._bootstrap_first_assembler_gears(
                        observation, player, quantity,
                        allow_existing_remote=allow_existing_remote, reference_position=reference_position,
                    )
                    if bootstrap is not None:
                        return bootstrap
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
            if reference_position is not None:
                logistics_layout = _find_site_input_logistic_line_layout(observation, item="iron-plate")
                consumer = logistics_layout.get("consumer") if isinstance(logistics_layout, dict) else None
                if isinstance(consumer, dict) and distance(_position(consumer), reference_position) <= 4.5:
                    logistics_decision = SiteInputLogisticLineSkill(max(12, quantity), item="iron-plate").next_action(observation)
                    if not logistics_decision.done:
                        return logistics_decision
                    return PlannerDecision(
                        {"type": "wait", "ticks": 300},
                        f"wait for iron plate site input logistics to feed {self.target_item} assembler",
                    )
            decision = self.iron_skill.next_action(observation, target_count=quantity, inventory_only=True)
            if not decision.done:
                return decision
            return None

        if item == "copper-plate":
            if reference_position is not None:
                logistics_layout = _find_site_input_logistic_line_layout(observation, item="copper-plate")
                consumer = logistics_layout.get("consumer") if isinstance(logistics_layout, dict) else None
                if isinstance(consumer, dict) and distance(_position(consumer), reference_position) <= 4.5:
                    logistics_decision = SiteInputLogisticLineSkill(max(12, quantity), item="copper-plate").next_action(observation)
                    if not logistics_decision.done:
                        return logistics_decision
                    return PlannerDecision(
                        {"type": "wait", "ticks": 300},
                        f"wait for copper plate site input logistics to feed {self.target_item} assembler",
                    )
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

        if item in {"wooden-chest", "iron-chest"}:
            if craftable_count(observation, item) > 0:
                return PlannerDecision({"type": "craft", "recipe": item, "count": 1}, f"craft {item} for build item mall output")
            if item == "wooden-chest" and inventory_count(observation, "wood") < 2:
                tree = _nearest_tree(observation)
                if tree is None:
                    if craftable_count(observation, "iron-chest") > 0:
                        return PlannerDecision({"type": "craft", "recipe": "iron-chest", "count": 1}, "craft iron chest for build item mall output")
                    return self._ensure_item_quantity(
                        observation,
                        player,
                        "iron-plate",
                        8,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=reference_position,
                    )
                tree_pos = _position(tree)
                if distance(player, tree_pos) > 8:
                    return PlannerDecision({"type": "move_to", "position": tree_pos}, "move near tree for build item mall output chest")
                return PlannerDecision(
                    {
                        "type": "mine",
                        "name": tree.get("name"),
                        "position": tree_pos,
                        "count": 1,
                    },
                    "mine tree for build item mall output chest",
                )
            if item == "iron-chest":
                return self._ensure_item_quantity(
                    observation,
                    player,
                    "iron-plate",
                    8,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=reference_position,
                )

        if item in {"burner-inserter", "inserter"}:
            recipe = RECIPES.get(item)
            if recipe is not None:
                for ingredient, amount in sorted(recipe.ingredients.items()):
                    needed = max(1, int(amount * max(1, quantity)))
                    if inventory_count(observation, ingredient) >= needed:
                        continue
                    decision = self._ensure_item_quantity(
                        observation,
                        player,
                        ingredient,
                        needed,
                        allow_existing_remote=allow_existing_remote,
                        reference_position=reference_position,
                        allow_assembler_output_gears=(ingredient == "iron-gear-wheel"),
                    )
                    if decision is not None:
                        return decision
            if craftable_count(observation, item) > 0:
                return PlannerDecision(
                    {"type": "craft", "recipe": item, "count": min(quantity - inventory_count(observation, item), craftable_count(observation, item))},
                    f"craft {item} for build item mall output",
                )

        if item == "wood":
            tree = _nearest_tree(observation)
            if tree is None:
                return PlannerDecision(None, "missing wood for build item mall and no nearby tree is visible")
            tree_pos = _position(tree)
            if distance(player, tree_pos) > 8:
                return PlannerDecision({"type": "move_to", "position": tree_pos}, "move near tree for build item mall wood")
            return PlannerDecision(
                {
                    "type": "mine",
                    "name": tree.get("name"),
                    "position": tree_pos,
                    "count": max(1, quantity - inventory_count(observation, "wood")),
                },
                "mine tree for build item mall wood",
            )

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

    def _bootstrap_first_assembler_gears(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        quantity: int,
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision | None:
        """No assembler exists yet, so the post-Automation 'gears must come from an assembler' rule
        cannot be satisfied — nothing can produce them. Break the chicken-and-egg deadlock by
        bootstrapping just enough gears to build the FIRST assembler (5 for assembling-machine-1):
        smelt more iron-plate if the reserve is short, then one-time craft the gears. This is the only
        way to get the first GEAR-producing assembler (true in both virtual/RCON mode, where 'craft'
        is instant, and character mode); once an assembler is set to make gears this path is disabled
        and gears come from it. Note: a lone assembler set to a different recipe (e.g. transport-belt)
        does NOT count -- it can't supply gears, so we still bootstrap to build the gear assembler."""
        if _gear_producing_assembler_available(observation):
            return None
        target = 5  # one assembling-machine-1 worth of gears (enough to build the first gear assembler)
        need = target - inventory_count(observation, "iron-gear-wheel")
        if need <= 0:
            return None  # enough gears to build the first assembler — let the mall build it
        iron_reserve = 2 * need + 9  # 2 iron-plate per gear + the assembler's own 9 iron-plate
        if inventory_count(observation, "iron-plate") < iron_reserve:
            return self._ensure_item_quantity(
                observation, player, "iron-plate", iron_reserve,
                allow_existing_remote=allow_existing_remote, reference_position=reference_position,
            )
        craftable = craftable_count(observation, "iron-gear-wheel")
        if craftable <= 0:
            # iron-plate present but not craftable yet (e.g. still in furnaces) — keep smelting.
            return self._ensure_item_quantity(
                observation, player, "iron-plate", iron_reserve,
                allow_existing_remote=allow_existing_remote, reference_position=reference_position,
            )
        return _bootstrap_seed_decision(
            {
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": min(need, craftable),
                "allow_first_assembler_bootstrap": True,
            },
            "bootstrap-craft gears for the first assembler (no assembler exists yet to produce them)",
            seed_reason="first_assembler_gear_seed",
            expected_followup="first gear-producing assembler is built or set to iron-gear-wheel",
        )

    def _ensure_assembler_bootstrap_gears(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        quantity: int,
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
    ) -> PlannerDecision | None:
        target_gears = 5 * quantity
        if inventory_count(observation, "iron-gear-wheel") >= target_gears:
            return None
        if not _assembler_automation_available(observation):
            return None
        cell = _find_build_item_mall_cell(
            observation,
            "iron-gear-wheel",
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
        )
        assembler = cell.get("assembler") if isinstance(cell, dict) else None
        if not isinstance(assembler, dict):
            return None
        assembler_position = _position(assembler)
        if not assembler.get("electric_network_connected"):
            pole_position = cell.get("pole_position") if isinstance(cell, dict) else None
            if isinstance(pole_position, dict):
                if distance(player, pole_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": pole_position}, "move near gear assembler pole")
                return PlannerDecision(
                    {
                        "type": "connect_power",
                        "unit_number": cell.get("pole_unit_number") if isinstance(cell, dict) else None,
                        "name": "small-electric-pole",
                        "position": pole_position,
                    },
                    "connect gear assembler before next assembling-machine-1 bootstrap",
                )
        if str(assembler.get("recipe") or "") != "iron-gear-wheel":
            incompatible = _first_incompatible_assembler_item(assembler, "iron-gear-wheel")
            if incompatible is not None:
                if distance(player, assembler_position) > 20:
                    return PlannerDecision({"type": "move_to", "position": assembler_position}, f"move near gear assembler to clear {incompatible}")
                return PlannerDecision(
                    {
                        "type": "take",
                        "item": incompatible,
                        "count": entity_item_count(assembler, incompatible),
                        "unit_number": assembler.get("unit_number"),
                        "name": assembler.get("name") or "assembling-machine-1",
                        "position": assembler_position,
                    },
                    f"clear {incompatible} before retooling assembler for bootstrap gears",
                )
            return self._set_recipe_decision(player, assembler, "iron-gear-wheel")
        output_decision = _take_assembler_output_gears_for_infrastructure(
            observation,
            player,
            target_gears,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position or assembler_position,
            infrastructure_reason="next assembling-machine-1 bootstrap",
        )
        if output_decision is not None:
            return output_decision
        output_count = entity_item_count(assembler, "iron-gear-wheel")
        if output_count > 0:
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, "move near gear assembler for next assembler bootstrap")
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "iron-gear-wheel",
                    "count": min(output_count, target_gears - inventory_count(observation, "iron-gear-wheel")),
                    "unit_number": assembler.get("unit_number"),
                    "name": assembler.get("name") or "assembling-machine-1",
                    "position": assembler_position,
                },
                "take assembler-produced gears for next assembling-machine-1 bootstrap",
            )
        if entity_item_count(assembler, "iron-plate") < 2:
            if inventory_count(observation, "iron-plate") <= 0:
                return self._ensure_item_quantity(
                    observation,
                    player,
                    "iron-plate",
                    2,
                    allow_existing_remote=allow_existing_remote,
                    reference_position=reference_position,
                )
            if distance(player, assembler_position) > 20:
                return PlannerDecision({"type": "move_to", "position": assembler_position}, "move near gear assembler to seed iron")
            return _bootstrap_seed_decision(
                {
                    "type": "insert",
                    "item": "iron-plate",
                    "count": min(10, inventory_count(observation, "iron-plate")),
                    "unit_number": assembler.get("unit_number"),
                    "name": assembler.get("name") or "assembling-machine-1",
                    "position": assembler_position,
                },
                "insert iron plates for assembler-produced bootstrap gears",
                seed_reason="assembler_produced_bootstrap_gear_iron_seed",
                expected_followup="gear assembler produces iron-gear-wheel for the next assembler bootstrap",
            )
        return PlannerDecision({"type": "wait", "ticks": 300}, "wait for assembler-produced bootstrap gears")

    def _take_assembler_output_gears_for_infrastructure(
        self,
        observation: dict[str, Any],
        player: dict[str, float],
        quantity: int,
        *,
        allow_existing_remote: bool = False,
        reference_position: dict[str, float] | None = None,
        exclude_units: set[Any] | None = None,
    ) -> PlannerDecision | None:
        return _take_assembler_output_gears_for_infrastructure(
            observation,
            player,
            quantity,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
            exclude_units=exclude_units,
            infrastructure_reason="build item mall infrastructure",
        )


def _take_assembler_output_gears_for_infrastructure(
    observation: dict[str, Any],
    player: dict[str, float],
    quantity: int,
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
    exclude_units: set[Any] | None = None,
    infrastructure_reason: str,
) -> PlannerDecision | None:
    target_gears = max(0, quantity - inventory_count(observation, "iron-gear-wheel"))
    if target_gears <= 0 or not _assembler_automation_available(observation):
        return None
    excluded_units = set(exclude_units or set())
    cell = _find_build_item_mall_cell(
        observation,
        "iron-gear-wheel",
        allow_existing_remote=allow_existing_remote,
        reference_position=reference_position,
    )
    assembler = cell.get("assembler") if isinstance(cell, dict) else None
    if not isinstance(assembler, dict) or str(assembler.get("recipe") or "") != "iron-gear-wheel":
        return None
    output_count = entity_item_count(assembler, "iron-gear-wheel")
    if output_count <= 0:
        chest = cell.get("output_chest") if isinstance(cell, dict) else None
        chest_count = entity_item_count(chest, "iron-gear-wheel") if isinstance(chest, dict) else 0
        if chest_count <= 0 and reference_position is not None:
            local_excluded_units = set(excluded_units) | {
                assembler.get("unit_number"),
            }
            local_excluded_units.discard(None)
            chest = _nearest_local_item_seed_source(
                observation,
                "iron-gear-wheel",
                reference_position,
                max_distance=8.0,
                exclude_units=local_excluded_units,
            )
            chest_count = entity_item_count(chest, "iron-gear-wheel") if isinstance(chest, dict) else 0
        if not isinstance(chest, dict) or chest_count <= 0:
            return None
        chest_position = _position(chest)
        if distance(player, chest_position) > 20:
            return PlannerDecision(
                {"type": "move_to", "position": chest_position},
                f"move near gear output chest for {infrastructure_reason}",
            )
        return PlannerDecision(
            {
                "type": "take",
                "item": "iron-gear-wheel",
                "count": min(chest_count, target_gears),
                "unit_number": chest.get("unit_number"),
                "name": chest.get("name") or "wooden-chest",
                "position": chest_position,
            },
            f"take chest-buffered assembler gears for {infrastructure_reason}",
        )
    assembler_position = _position(assembler)
    if distance(player, assembler_position) > 20:
        return PlannerDecision(
            {"type": "move_to", "position": assembler_position},
            f"move near gear assembler for {infrastructure_reason}",
        )
    return PlannerDecision(
        {
            "type": "take",
            "item": "iron-gear-wheel",
            "count": min(output_count, target_gears),
            "unit_number": assembler.get("unit_number"),
            "name": assembler.get("name") or "assembling-machine-1",
            "position": assembler_position,
        },
        f"take assembler-produced gears for {infrastructure_reason}",
    )


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


def _automation_researched(observation: dict[str, Any]) -> bool:
    return bool(_technology_state(observation, "automation").get("researched"))


def _gear_handcraft_automation_context_active(observation: dict[str, Any]) -> bool:
    if _automation_researched(observation):
        return True
    if inventory_count(observation, "assembling-machine-1") > 0:
        return True
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES:
            return True
    return False


def _assembler_automation_available(observation: dict[str, Any]) -> bool:
    if inventory_count(observation, "assembling-machine-1") > 0:
        return True
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES:
            return True
    return False


def _gear_producing_assembler_available(observation: dict[str, Any]) -> bool:
    """True if some built assembler is already set to make iron-gear-wheel, so gears can come from
    automation. An assembler set to a DIFFERENT recipe does NOT count -- that is the second-deadlock
    case: a lone belt/other assembler exists but nothing produces gears, so the post-Automation
    'gears must come from an assembler' rule still cannot be satisfied."""
    for entity in observation.get("entities") or []:
        if (
            isinstance(entity, dict)
            and str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES
            and str(entity.get("recipe") or "") == "iron-gear-wheel"
        ):
            return True
    return False


def _first_assembler_bootstrap_needed(observation: dict[str, Any], quantity: int) -> bool:
    return quantity <= 1 and not _assembler_automation_available(observation)


def _can_handcraft_first_assembler_gears(observation: dict[str, Any], target_gear_count: int) -> bool:
    if _assembler_automation_available(observation):
        return False
    current_gears = inventory_count(observation, "iron-gear-wheel")
    missing_gears = max(0, target_gear_count - current_gears)
    if target_gear_count > 5 or missing_gears <= 0:
        return False
    craft_count = min(missing_gears, craftable_count(observation, "iron-gear-wheel"), max(0, 5 - current_gears))
    if craft_count <= 0:
        return False
    return inventory_count(observation, "electronic-circuit") >= 3 and inventory_count(observation, "iron-plate") >= 9 + (
        2 * craft_count
    )


def _ensure_iron_gears_without_post_automation_handcraft(
    observation: dict[str, Any],
    target_count: int,
    *,
    pre_automation_reason: str,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
    allow_assembler_output_gears: bool = False,
    infrastructure_reason: str = "infrastructure",
) -> PlannerDecision | None:
    current_count = inventory_count(observation, "iron-gear-wheel")
    if current_count >= target_count:
        return None
    missing = target_count - current_count
    if not _gear_handcraft_automation_context_active(observation):
        craftable = craftable_count(observation, "iron-gear-wheel")
        if craftable <= 0:
            return None
        return PlannerDecision(
            {
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": min(missing, craftable),
            },
            pre_automation_reason,
        )

    if allow_assembler_output_gears:
        decision = _take_assembler_output_gears_for_infrastructure(
            observation,
            player_position(observation),
            target_count,
            allow_existing_remote=allow_existing_remote,
            reference_position=reference_position,
            infrastructure_reason=infrastructure_reason,
        )
        if decision is not None:
            return decision
        chest = _nearest_buffered_chest_item_source(
            observation,
            "iron-gear-wheel",
            reference_position or player_position(observation),
        )
        chest_count = entity_item_count(chest, "iron-gear-wheel") if isinstance(chest, dict) else 0
        if isinstance(chest, dict) and chest_count > 0:
            chest_position = _position(chest)
            player = player_position(observation)
            if distance(player, chest_position) > 20:
                return PlannerDecision(
                    {"type": "move_to", "position": chest_position},
                    f"move near buffered gear chest for {infrastructure_reason}",
                )
            return PlannerDecision(
                {
                    "type": "take",
                    "item": "iron-gear-wheel",
                    "count": min(chest_count, missing),
                    "unit_number": chest.get("unit_number"),
                    "name": chest.get("name") or "wooden-chest",
                    "position": chest_position,
                },
                f"take buffered gears for {infrastructure_reason}",
            )

    decision = BuildItemMallSkill("iron-gear-wheel", max(target_count, 4)).next_action(
        observation,
        allow_existing_remote=allow_existing_remote,
        reference_position=reference_position,
    )
    if not decision.done:
        return decision
    return PlannerDecision(
        {"type": "wait", "ticks": 120},
        "wait for iron gear wheel mall output; refusing hand-crafted iron gears after Automation",
    )


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


def _technology_research_ingredients(observation: dict[str, Any], technology: str) -> dict[str, int]:
    state = _technology_state(observation, technology)
    ingredients = state.get("ingredients") if isinstance(state.get("ingredients"), dict) else None
    if ingredients:
        return {str(item): int(count or 0) for item, count in ingredients.items()}
    known = TECHNOLOGIES.get(technology)
    if known is None:
        return {}
    return {str(pack_name): 1 for pack_name in known.science_packs}


def _technology_research_unit_count(observation: dict[str, Any], technology: str) -> int:
    state = _technology_state(observation, technology)
    try:
        value = int(state.get("research_unit_count") or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    known = TECHNOLOGIES.get(technology)
    if known is None:
        return 10
    return max(1, max(known.science_packs.values() or [1]))


def _research_pack_goal(observation: dict[str, Any], technology: str, pack_name: str) -> int:
    unit_count = _technology_research_unit_count(observation, technology)
    ingredients = _technology_research_ingredients(observation, technology)
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


def _connected_power_poles(observation: dict[str, Any]) -> list[dict[str, Any]]:
    return [pole for pole in _power_poles(observation) if _entity_is_power_anchor(observation, pole)]


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


def _science_pack_factory_positions(observation: dict[str, Any]) -> list[dict[str, float]]:
    positions: list[dict[str, float]] = []
    for entity_name in ASSEMBLER_ENTITY_NAMES:
        for entity in entities_named(observation, entity_name):
            recipe = str(entity.get("recipe") or "")
            if recipe.endswith("science-pack"):
                positions.append(_position(entity))
    return positions


def _nearest_power_generation_distance(observation: dict[str, Any], position: dict[str, float]) -> float:
    candidates: list[dict[str, float]] = []
    for entity_name in ("offshore-pump", "boiler", "steam-engine"):
        candidates.extend(_position(entity) for entity in entities_named(observation, entity_name))
    return min((distance(position, candidate) for candidate in candidates), default=999999.0)


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
    science_positions = _science_pack_factory_positions(observation)

    def sort_key(item: dict[str, Any]) -> tuple[float, ...]:
        lab_position = _xy_position(item["lab_position"])
        power_distance = _nearest_power_generation_distance(observation, lab_position)
        power_clearance_penalty = 1 if power_distance < POWER_EXPANSION_CLEARANCE_RADIUS else 0
        if science_positions:
            science_distance = min(distance(lab_position, position) for position in science_positions)
            return (
                0 if item.get("powered") else 1,
                0 if item.get("pole_unit_number") else 1,
                science_distance,
                power_clearance_penalty,
                float(item.get("distance") or 999999),
            )
        return (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            power_clearance_penalty,
            -min(power_distance, 999999.0),
            float(item.get("distance") or 999999),
        )

    candidates.sort(
        key=sort_key
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
    candidates: list[dict[str, Any]] = []
    if isinstance(sites, list):
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
                    "transfer_inserter_direction": _direction_or_default(site.get("transfer_inserter_direction"), EAST),
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
        return _select_circuit_automation_sidecar_site(observation)
    candidates.sort(
        key=lambda item: (
            0 if item.get("powered") else 1,
            0 if item.get("pole_unit_number") else 1,
            float(item.get("distance") or 999999),
        )
    )
    return candidates[0]


def _select_circuit_automation_sidecar_site(observation: dict[str, Any]) -> dict[str, Any] | None:
    anchors = _circuit_automation_sidecar_anchors(observation)
    if not anchors:
        return None
    candidates: list[dict[str, Any]] = []
    reach = _power_wire_reach("small-electric-pole")
    player = player_position(observation)
    for anchor in anchors:
        anchor_position = anchor["anchor_position"]
        source_pole = anchor["source_pole"]
        source_position = _position(source_pole)
        for cable_position in _circuit_automation_sidecar_cable_positions(anchor_position):
            layout = _circuit_cell_layout_from_cable_position(cable_position)
            if not _circuit_automation_layout_buildable(observation, layout):
                continue
            pole = _nearest_small_pole_supplying_positions(
                observation,
                [
                    layout["cable_assembler_position"],
                    layout["circuit_assembler_position"],
                    layout["transfer_inserter_position"],
                ],
            )
            if isinstance(pole, dict) and distance(_position(pole), source_position) <= reach:
                pole_position = _position(pole)
            else:
                pole = None
                pole_position = _select_circuit_automation_sidecar_pole_position(
                    observation,
                    layout,
                    source_position,
                    reach,
                )
                if pole_position is None:
                    continue
            layout["pole_position"] = pole_position
            layout["pole"] = pole
            layout["pole_unit_number"] = pole.get("unit_number") if isinstance(pole, dict) else None
            layout["source_pole_unit_number"] = source_pole.get("unit_number")
            layout["powered"] = bool(isinstance(pole, dict) and _entity_is_power_anchor(observation, pole))
            layout["distance"] = distance(player, layout["cable_assembler_position"])
            candidates.append(layout)
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


def _circuit_automation_sidecar_anchors(observation: dict[str, Any]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for assembler in entities_named(observation, "assembling-machine-1"):
        if assembler.get("electric_network_connected") is not True:
            continue
        recipe = str(assembler.get("recipe") or "")
        if recipe in {"copper-cable", "electronic-circuit"}:
            continue
        assembler_position = _position(assembler)
        if not _within_starter_logistics_area(observation, assembler_position):
            continue
        source_pole = _nearest_small_pole_supplying_position(observation, assembler_position)
        if source_pole is None:
            source_pole = _nearest_to(
                [pole for pole in entities_named(observation, "small-electric-pole") if distance(_position(pole), assembler_position) <= 7.5],
                assembler_position,
            )
        if not isinstance(source_pole, dict):
            continue
        anchors.append(
            {
                "anchor_position": assembler_position,
                "source_pole": source_pole,
                "distance": float(assembler.get("distance") or distance(player_position(observation), assembler_position)),
            }
        )
    anchors.sort(key=lambda item: float(item.get("distance") or 999999))
    return anchors


def _circuit_automation_sidecar_cable_positions(anchor_position: dict[str, float]) -> list[dict[str, float]]:
    offsets: list[dict[str, float]] = []
    for dy in (4.0, -4.0, 8.0, -8.0, 12.0, -12.0, 0.0, 16.0, -16.0):
        for dx in (0.0, -4.0, 4.0, -8.0, 8.0, -12.0, 12.0, -16.0, 16.0):
            offsets.append({"x": dx, "y": dy})
    positions: list[dict[str, float]] = []
    seen: set[tuple[float, float]] = set()
    for offset in offsets:
        position = {
            "x": _round_entity_center(anchor_position["x"] + offset["x"]),
            "y": _round_entity_center(anchor_position["y"] + offset["y"]),
        }
        key = _position_tuple(position)
        if key in seen:
            continue
        seen.add(key)
        positions.append(position)
    return positions


def _circuit_automation_layout_buildable(observation: dict[str, Any], layout: dict[str, Any]) -> bool:
    required = [
        "cable_assembler_position",
        "circuit_assembler_position",
        "transfer_inserter_position",
    ]
    if not all(_within_starter_logistics_area(observation, _xy_position(layout[key])) for key in required):
        return False
    if not _build_item_sidecar_position_clear(observation, layout["cable_assembler_position"]):
        return False
    if not _build_item_sidecar_position_clear(observation, layout["circuit_assembler_position"]):
        return False
    return _build_item_mall_output_position_clear(
        observation,
        layout["transfer_inserter_position"],
        allowed_names={"inserter", "fast-inserter"},
    )


def _nearest_small_pole_supplying_positions(
    observation: dict[str, Any],
    positions: list[dict[str, float]],
) -> dict[str, Any] | None:
    candidates = [
        pole
        for pole in entities_named(observation, "small-electric-pole")
        if all(_small_pole_supplies_position(_position(pole), position) for position in positions)
    ]
    return _nearest_to(candidates, positions[0]) if candidates else None


def _select_circuit_automation_sidecar_pole_position(
    observation: dict[str, Any],
    layout: dict[str, Any],
    source_position: dict[str, float],
    reach: float,
) -> dict[str, float] | None:
    cable_position = layout["cable_assembler_position"]
    candidates = [
        {"x": cable_position["x"] + 2.0, "y": cable_position["y"] - 2.0},
        {"x": cable_position["x"] + 2.0, "y": cable_position["y"] + 2.0},
    ]
    supply_positions = [
        layout["cable_assembler_position"],
        layout["circuit_assembler_position"],
        layout["transfer_inserter_position"],
    ]
    valid: list[dict[str, float]] = []
    for candidate in candidates:
        candidate = {"x": _round_power_pole_center(candidate["x"]), "y": _round_power_pole_center(candidate["y"])}
        if distance(candidate, source_position) > reach:
            continue
        if not all(_small_pole_supplies_position(candidate, position) for position in supply_positions):
            continue
        existing = _entity_near(observation, "small-electric-pole", candidate, radius=1.0)
        if existing is None and not _build_item_sidecar_pole_position_clear(observation, candidate):
            continue
        valid.append(candidate)
    if not valid:
        return None
    valid.sort(key=lambda item: distance(item, source_position))
    return valid[0]


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


def _find_gear_belt_mall_logistics_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    gear_assemblers = [
        item
        for item in entities_named(observation, "assembling-machine-1")
        if item.get("recipe") == "iron-gear-wheel"
        and item.get("electric_network_connected") is not False
        and _within_allowed_factory_area(observation, _position(item))
    ]
    if not gear_assemblers:
        return None
    candidates: list[dict[str, Any]] = []
    assemblers = entities_named(observation, "assembling-machine-1")
    for gear_assembler in gear_assemblers:
        gear_pos = _position(gear_assembler)
        belt_candidates = [
            item
            for item in assemblers
            if item is not gear_assembler
            and item.get("electric_network_connected") is not False
            and item.get("recipe") not in {"copper-cable", "electronic-circuit"}
            and _within_allowed_factory_area(observation, _position(item))
            and _gear_belt_pair_axis_distance(gear_pos, _position(item)) is not None
        ]
        if not belt_candidates:
            continue
        belt_candidates.sort(
            key=lambda item: (
                0 if item.get("recipe") == "transport-belt" else 1,
                distance(gear_pos, _position(item)),
            )
        )
        for belt_assembler in belt_candidates:
            layout = _gear_belt_mall_layout_for_pair(observation, gear_assembler, belt_assembler)
            if layout is not None:
                candidates.append(layout)
                break
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if item["belt_assembler"].get("recipe") == "transport-belt" else 1,
            float(item.get("distance") or 999999.0),
        )
    )
    return candidates[0]


def _gear_belt_mall_layout_for_pair(
    observation: dict[str, Any],
    gear_assembler: dict[str, Any],
    belt_assembler: dict[str, Any],
) -> dict[str, Any] | None:
    gear_pos = _position(gear_assembler)
    belt_pos = _position(belt_assembler)
    vertical_distance = abs(belt_pos["y"] - gear_pos["y"])
    if abs(belt_pos["x"] - gear_pos["x"]) <= 0.1 and abs(vertical_distance - GEAR_BELT_MALL_ASSEMBLER_SPACING) <= 0.25:
        vertical_sign = 1 if belt_pos["y"] >= gear_pos["y"] else -1
        direct_direction = NORTH if vertical_sign > 0 else SOUTH
        direct_position = {"x": gear_pos["x"], "y": gear_pos["y"] + vertical_sign * 2.0}
        if not _mall_logistics_positions_clear(observation, belt_positions=[], inserter_positions=[direct_position]):
            return None
        return {
            "gear_assembler": gear_assembler,
            "belt_assembler": belt_assembler,
            "distance": distance(gear_pos, belt_pos),
            "gear_belts": [],
            "gear_output_inserter": None,
            "belt_input_inserter": None,
            "direct_gear_transfer_inserter": {
                "position": direct_position,
                "direction": direct_direction,
                "entity": _inserter_near(observation, direct_position),
            },
        }

    direction_sign = 1 if belt_pos["x"] >= gear_pos["x"] else -1
    belt_direction = EAST if direction_sign > 0 else WEST
    horizontal_distance = abs(belt_pos["x"] - gear_pos["x"])
    if abs(belt_pos["y"] - gear_pos["y"]) > 0.1 or horizontal_distance < GEAR_BELT_MALL_ASSEMBLER_SPACING:
        return None
    direct_transfer = None
    if abs(horizontal_distance - GEAR_BELT_MALL_ASSEMBLER_SPACING) <= 0.25 and abs(belt_pos["y"] - gear_pos["y"]) <= 0.1:
        direct_position = {"x": gear_pos["x"] + direction_sign * 2.0, "y": gear_pos["y"]}
        if _mall_logistics_positions_clear(observation, belt_positions=[], inserter_positions=[direct_position]):
            direct_direction = WEST if direction_sign > 0 else EAST
            direct_transfer = {
                "position": direct_position,
                "direction": direct_direction,
                "entity": _inserter_near(observation, direct_position),
            }

    for vertical_sign, output_direction, input_direction in [(-1, SOUTH, NORTH), (1, NORTH, SOUTH)]:
        lane_y = gear_pos["y"] + (3.0 * vertical_sign)
        inserter_y = gear_pos["y"] + (2.0 * vertical_sign)
        output_x = gear_pos["x"] + direction_sign
        input_x = belt_pos["x"] - direction_sign
        output_position = {"x": output_x, "y": inserter_y}
        input_position = {"x": input_x, "y": inserter_y}
        belt_positions = []
        steps = max(1, int(round(horizontal_distance)) - 1)
        for step in range(1, steps + 1):
            belt_positions.append({"x": gear_pos["x"] + direction_sign * step, "y": lane_y})
        if not _mall_logistics_positions_clear(
            observation,
            belt_positions=belt_positions,
            inserter_positions=[output_position, input_position],
        ):
            continue
        gear_output_entity = _inserter_near(observation, output_position)
        belt_input_entity = _inserter_near(observation, input_position)
        protected_output_units = {
            gear_output_entity.get("unit_number")
            for gear_output_entity in [gear_output_entity]
            if isinstance(gear_output_entity, dict) and gear_output_entity.get("unit_number") is not None
        }
        return {
            "gear_assembler": gear_assembler,
            "belt_assembler": belt_assembler,
            "distance": distance(gear_pos, belt_pos),
            "gear_belts": [
                {
                    "position": position,
                    "direction": belt_direction,
                    "entity": _entity_near(observation, "transport-belt", position, radius=0.35),
                }
                for position in belt_positions
            ],
            "gear_output_inserter": {
                "position": output_position,
                "direction": output_direction,
                "entity": gear_output_entity,
            },
            "belt_input_inserter": {
                "position": input_position,
                "direction": input_direction,
                "entity": belt_input_entity,
                "exclude_reusable_unit_numbers": protected_output_units,
            },
            "direct_gear_transfer_inserter": direct_transfer,
        }
    return None


def _gear_belt_pair_axis_distance(
    gear_pos: dict[str, float],
    belt_pos: dict[str, float],
) -> float | None:
    horizontal = abs(belt_pos["x"] - gear_pos["x"])
    vertical = abs(belt_pos["y"] - gear_pos["y"])
    if vertical <= 0.1 and GEAR_BELT_MALL_ASSEMBLER_SPACING <= horizontal <= 8.0:
        return horizontal
    if horizontal <= 0.1 and abs(vertical - GEAR_BELT_MALL_ASSEMBLER_SPACING) <= 0.25:
        return vertical
    return None


def _direct_gear_transfer_blocked(layout: dict[str, Any]) -> bool:
    direct_transfer = layout.get("direct_gear_transfer_inserter")
    if not isinstance(direct_transfer, dict):
        return False
    inserter = direct_transfer.get("entity")
    if not isinstance(inserter, dict):
        return False
    status_name = str(inserter.get("status_name") or "")
    if status_name == "waiting_for_space_in_destination":
        return (
            entity_item_count(layout["gear_assembler"], "iron-gear-wheel") > 0
            and entity_item_count(layout["belt_assembler"], "iron-gear-wheel") <= 0
        )
    if status_name == "waiting_for_source_items":
        return entity_item_count(layout["gear_assembler"], "iron-gear-wheel") > 0
    if status_name:
        return False
    return False


def _gear_belt_lane_has_transfer_inserter(layout: dict[str, Any]) -> bool:
    for key in ("gear_output_inserter", "belt_input_inserter"):
        spec = layout.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("entity"), dict):
            return True
    return False


def _obsolete_gear_belt_mall_buffer_cleanup_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    layout: dict[str, Any],
) -> PlannerDecision | None:
    protected_units: set[Any] = {
        layout["gear_assembler"].get("unit_number"),
        layout["belt_assembler"].get("unit_number"),
    }
    protected_positions: set[tuple[float, float]] = set()
    for key in ("gear_output_inserter", "belt_input_inserter", "direct_gear_transfer_inserter"):
        spec = layout.get(key)
        if not isinstance(spec, dict):
            continue
        protected_positions.add(_position_tuple(spec["position"]))
        entity = spec.get("entity")
        if isinstance(entity, dict):
            protected_units.add(entity.get("unit_number"))
    for belt in layout.get("gear_belts") or []:
        if isinstance(belt, dict):
            protected_positions.add(_position_tuple(belt["position"]))
            entity = belt.get("entity")
            if isinstance(entity, dict):
                protected_units.add(entity.get("unit_number"))

    cleanup = _nearest_obsolete_empty_chest_or_inserter(
        observation,
        player,
        anchors=[_position(layout["gear_assembler"]), _position(layout["belt_assembler"])],
        protected_units=protected_units,
        protected_positions=protected_positions,
    )
    if cleanup is None:
        return None
    entity_position = _position(cleanup)
    if distance(player, entity_position) > 8:
        return PlannerDecision(
            {"type": "move_to", "position": _stand_position(entity_position, offset=2.0)},
            f"move near obsolete {cleanup.get('name')} left from temporary gear/belt mall buffering",
        )
    return PlannerDecision(
        {
            "type": "mine",
            "unit_number": cleanup.get("unit_number"),
            "name": cleanup.get("name"),
            "position": entity_position,
        },
        f"remove obsolete empty {cleanup.get('name')} left from temporary gear/belt mall buffering",
    )


def _obsolete_build_item_mall_buffer_cleanup_decision(
    observation: dict[str, Any],
    player: dict[str, float],
    cell: dict[str, Any],
) -> PlannerDecision | None:
    assembler = cell.get("assembler")
    if not isinstance(assembler, dict):
        return None
    protected_units: set[Any] = {assembler.get("unit_number")}
    protected_positions: set[tuple[float, float]] = set()
    for key in ("output_chest", "output_inserter"):
        entity = cell.get(key)
        if isinstance(entity, dict):
            protected_units.add(entity.get("unit_number"))
    for key in ("output_chest_position", "output_inserter_position"):
        position = cell.get(key)
        if isinstance(position, dict):
            protected_positions.add(_position_tuple(position))
    _protect_active_build_item_mall_output_buffers(observation, protected_units, protected_positions)
    cleanup = _nearest_obsolete_empty_chest_or_inserter(
        observation,
        player,
        anchors=[_position(assembler)],
        protected_units=protected_units,
        protected_positions=protected_positions,
    )
    if cleanup is None:
        return None
    entity_position = _position(cleanup)
    if distance(player, entity_position) > 8:
        return PlannerDecision(
            {"type": "move_to", "position": _stand_position(entity_position, offset=2.0)},
            f"move near obsolete {cleanup.get('name')} left from temporary item mall buffering",
        )
    return PlannerDecision(
        {
            "type": "mine",
            "unit_number": cleanup.get("unit_number"),
            "name": cleanup.get("name"),
            "position": entity_position,
        },
        f"remove obsolete empty {cleanup.get('name')} left from temporary item mall buffering",
    )


def _protect_active_build_item_mall_output_buffers(
    observation: dict[str, Any],
    protected_units: set[Any],
    protected_positions: set[tuple[float, float]],
) -> None:
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or str(entity.get("name") or "") not in ASSEMBLER_ENTITY_NAMES:
            continue
        recipe = str(entity.get("recipe") or "")
        if recipe not in USER_OUTPUT_MALL_ITEMS or not isinstance(entity.get("position"), dict):
            continue
        layout = _build_item_mall_output_layout(observation, _position(entity))
        if layout is None:
            continue
        for key in ("output_chest", "output_inserter"):
            buffered = layout.get(key)
            if isinstance(buffered, dict):
                protected_units.add(buffered.get("unit_number"))
        for key in ("output_chest_position", "output_inserter_position"):
            position = layout.get(key)
            if isinstance(position, dict):
                protected_positions.add(_position_tuple(position))


def _nearest_obsolete_empty_chest_or_inserter(
    observation: dict[str, Any],
    player: dict[str, float],
    *,
    anchors: list[dict[str, float]],
    protected_units: set[Any],
    protected_positions: set[tuple[float, float]],
) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    candidate_names = {"wooden-chest", "iron-chest", "steel-chest", "inserter", "burner-inserter", "fast-inserter"}
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        if name not in candidate_names:
            continue
        if entity.get("unit_number") in protected_units:
            continue
        position = _position(entity)
        if _position_tuple(position) in protected_positions:
            continue
        if min(distance(position, anchor) for anchor in anchors) > 6.0:
            continue
        if _entity_inventory_totals(entity):
            continue
        if name in {"inserter", "burner-inserter", "fast-inserter"} and _inserter_serves_any_machine(observation, entity):
            continue
        candidates.append((distance(player, position), entity))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _inserter_serves_any_machine(observation: dict[str, Any], inserter: dict[str, Any]) -> bool:
    endpoints = _inserter_endpoints(inserter)
    if endpoints is None:
        return False
    pickup, drop = endpoints
    machine_names = ASSEMBLER_ENTITY_NAMES | {"stone-furnace", "steel-furnace", "electric-furnace", "boiler", "lab"}
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        if str(entity.get("name") or "") not in machine_names:
            continue
        if _point_inside_machine(pickup, entity) or _point_inside_machine(drop, entity):
            return True
    return False


def _mall_logistics_positions_clear(
    observation: dict[str, Any],
    *,
    belt_positions: list[dict[str, float]],
    inserter_positions: list[dict[str, float]],
) -> bool:
    allowed_by_position: dict[tuple[float, float], set[str]] = {}
    for position in belt_positions:
        allowed_by_position[_position_tuple(position)] = {"transport-belt"}
    for position in inserter_positions:
        allowed_by_position[_position_tuple(position)] = {"inserter", "burner-inserter", "fast-inserter"}
    planned_positions = belt_positions + inserter_positions
    large_entities = ASSEMBLER_ENTITY_NAMES | {"lab", "stone-furnace", "burner-mining-drill", "boiler", "steam-engine"}
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        if name in large_entities:
            for position in planned_positions:
                if _point_inside_machine(position, entity):
                    return False
        key = _position_tuple(_position(entity))
        allowed = allowed_by_position.get(key)
        if not allowed:
            continue
        if name not in allowed:
            return False
    return True


def _position_tuple(position: dict[str, float]) -> tuple[float, float]:
    return (round(float(position.get("x") or 0.0), 3), round(float(position.get("y") or 0.0), 3))


def _inserter_near(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    radius: float = 0.35,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for name in ("inserter", "burner-inserter", "fast-inserter"):
        candidates.extend(entities_named(observation, name))
    return _nearest_to([item for item in candidates if distance(_position(item), position) <= radius], position)


def _nearest_local_item_seed_source(
    observation: dict[str, Any],
    item: str,
    target_position: dict[str, float],
    *,
    max_distance: float = 16.0,
    exclude_units: set[Any] | None = None,
) -> dict[str, Any] | None:
    excluded = set(exclude_units or set())
    excluded.discard(None)
    allowed_names = {
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
    }
    candidates: list[dict[str, Any]] = []
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("unit_number") in excluded:
            continue
        if str(entity.get("name") or "") not in allowed_names:
            continue
        if entity_item_count(entity, item) <= 0:
            continue
        entity_position = _position(entity)
        if distance(entity_position, target_position) > max_distance:
            continue
        candidates.append(entity)
    return _nearest_to(candidates, target_position)


def _nearest_buffered_chest_item_source(
    observation: dict[str, Any],
    item: str,
    target_position: dict[str, float],
) -> dict[str, Any] | None:
    candidates = [
        entity
        for entity in observation.get("entities") or []
        if isinstance(entity, dict)
        and str(entity.get("name") or "") in {"wooden-chest", "iron-chest", "steel-chest"}
        and entity_item_count(entity, item) > 0
    ]
    return _nearest_to(candidates, target_position)


def _find_relocatable_inserter_for_mall(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    prefer_burner: bool,
    exclude_units: set[Any] | None = None,
) -> dict[str, Any] | None:
    names = ("burner-inserter",) if prefer_burner else ("inserter", "fast-inserter")
    excluded = set(exclude_units or set())
    candidates: list[dict[str, Any]] = []
    for name in names:
        for entity in entities_named(observation, name):
            if entity.get("unit_number") in excluded:
                continue
            position = _position(entity)
            if distance(position, target_position) <= 0.35:
                continue
            if not _within_allowed_factory_area(observation, position):
                continue
            if name != "burner-inserter" and entity.get("electric_network_connected") is False:
                continue
            candidates.append(entity)
    return _nearest_to(candidates, target_position)


def _select_mall_inserter_power_pole_position(
    observation: dict[str, Any],
    inserter_position: dict[str, float],
) -> dict[str, float] | None:
    source = _nearest_to(_connected_power_poles(observation), inserter_position)
    if source is None:
        source = _nearest_to(_power_poles(observation), inserter_position)
    source_position = _position(source) if source is not None else None
    primary_offsets = [
        {"x": 2.0, "y": 0.0},
        {"x": -2.0, "y": 0.0},
        {"x": 2.0, "y": 2.0},
        {"x": -2.0, "y": 2.0},
        {"x": 2.0, "y": -2.0},
        {"x": -2.0, "y": -2.0},
        {"x": 0.0, "y": 2.0},
        {"x": 0.0, "y": -2.0},
    ]
    fallback_offsets: list[dict[str, float]] = []
    for dy in (0.0, -1.0, 1.0, -2.0, 2.0):
        for dx in (2.0, -2.0, 1.0, -1.0, 0.0):
            if dx == 0.0 and dy == 0.0:
                continue
            offset = {"x": dx, "y": dy}
            if offset not in primary_offsets:
                fallback_offsets.append(offset)
    for offsets in (primary_offsets, fallback_offsets):
        candidates: list[dict[str, float]] = []
        for offset in offsets:
            candidate = {
                "x": inserter_position["x"] + offset["x"],
                "y": inserter_position["y"] + offset["y"],
            }
            if source_position is not None and distance(candidate, source_position) > 7.5:
                continue
            if _mall_power_pole_position_clear(observation, candidate):
                candidates.append(candidate)
        if not candidates:
            continue
        if source_position is not None:
            candidates.sort(key=lambda item: distance(item, source_position))
        return candidates[0]
    return None


def _mall_power_pole_position_clear(observation: dict[str, Any], position: dict[str, float]) -> bool:
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
        if name == "offshore-pump" and _power_pole_candidate_on_offshore_pump_water_side(position, entity):
            return False
        if name in large_entities and _point_inside_machine(position, entity):
            return False
        if distance(_position(entity), position) < 0.75:
            return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("position"), dict):
            continue
        if str(resource.get("name") or "") in PROTECTED_RESOURCE_NAMES and distance(_position(resource), position) < 1.0:
            return False
    return True


def _power_pole_candidate_on_offshore_pump_water_side(
    position: dict[str, float],
    pump: dict[str, Any],
) -> bool:
    pump_position = _position(pump)
    if distance(position, pump_position) > 6.0:
        return False
    direction = _direction_or_default(pump.get("direction"), NORTH)
    x = float(position["x"])
    y = float(position["y"])
    pump_x = float(pump_position["x"])
    pump_y = float(pump_position["y"])
    if direction == WEST:
        return x <= pump_x + 0.1 and abs(y - pump_y) <= 5.0
    if direction == EAST:
        return x >= pump_x - 0.1 and abs(y - pump_y) <= 5.0
    if direction == NORTH:
        return y <= pump_y + 0.1 and abs(x - pump_x) <= 5.0
    if direction == SOUTH:
        return y >= pump_y - 0.1 and abs(x - pump_x) <= 5.0
    return False


def _first_incompatible_assembler_item(assembler: dict[str, Any], target_recipe: str) -> str | None:
    recipe = RECIPES.get(target_recipe)
    if recipe is None:
        return None
    allowed = set(recipe.ingredients) | set(recipe.products)
    for item, count in sorted(_entity_inventory_totals(assembler).items()):
        if count > 0 and item not in allowed:
            return item
    return None


def _entity_inventory_totals(entity: dict[str, Any]) -> dict[str, int]:
    inventories = entity.get("inventories")
    if not isinstance(inventories, dict):
        return {}
    totals: dict[str, int] = {}
    for inventory in inventories.values():
        if not isinstance(inventory, dict):
            continue
        for item, raw_count in inventory.items():
            try:
                count = int(raw_count or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                totals[str(item)] = totals.get(str(item), 0) + count
    return totals


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
        retool_candidates = [
            item
            for item in assemblers
            if item.get("electric_network_connected")
            and item.get("recipe") not in {"copper-cable", "electronic-circuit", "small-electric-pole"}
            and _within_allowed_factory_area(
                observation,
                _position(item),
                allow_existing_remote=allow_existing_remote,
                reference_position=reference_position,
            )
        ]
        non_belt_candidates = [item for item in retool_candidates if item.get("recipe") != "transport-belt"]
        candidates = non_belt_candidates or [
            item for item in retool_candidates if not _preserve_transport_belt_mall_assembler(observation, item)
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
    if not candidates and target_item == "small-electric-pole" and inventory_count(observation, "assembling-machine-1") <= 0:
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
    if not candidates and target_item == "transport-belt" and inventory_count(observation, "assembling-machine-1") <= 0:
        candidates = [
            item
            for item in assemblers
            if _transport_belt_mall_retool_candidate(
                observation,
                item,
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
    return _attach_build_item_mall_output_layout(
        observation,
        target_item,
        {
        "pole_position": pole_position,
        "assembler_position": assembler_position,
        "pole": pole if pole_in_reach else None,
        "pole_unit_number": pole.get("unit_number") if pole_in_reach else None,
        "assembler": assembler,
        "powered": assembler.get("electric_network_connected"),
        },
    )


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


def _transport_belt_mall_retool_candidate(
    observation: dict[str, Any],
    assembler: dict[str, Any],
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> bool:
    if not assembler.get("electric_network_connected"):
        return False
    if not _within_allowed_factory_area(
        observation,
        _position(assembler),
        allow_existing_remote=allow_existing_remote,
        reference_position=reference_position,
    ):
        return False
    recipe = str(assembler.get("recipe") or "")
    if recipe != "small-electric-pole":
        return False
    if total_item_count(observation, "small-electric-pole") < 8:
        return False
    return not _near_recipe_assembler(observation, assembler, {"copper-cable", "electronic-circuit"}, radius=3.0)


def _preserve_transport_belt_mall_assembler(observation: dict[str, Any], assembler: dict[str, Any]) -> bool:
    if str(assembler.get("recipe") or "") != "transport-belt":
        return False
    if assembler.get("electric_network_connected") is False:
        return False
    if entity_item_count(assembler, "transport-belt") > 0:
        return True
    belt_assembler_count = sum(
        1
        for item in entities_named(observation, "assembling-machine-1")
        if item.get("electric_network_connected") is not False and str(item.get("recipe") or "") == "transport-belt"
    )
    return belt_assembler_count <= 1 and not _transport_belt_automation_output_ready(observation)


def _transport_belt_automation_output_ready(observation: dict[str, Any]) -> bool:
    if inventory_count(observation, "transport-belt") > 0:
        return True
    return _transport_belt_automation_factory_output_ready(observation)


def _transport_belt_automation_factory_output_ready(observation: dict[str, Any]) -> bool:
    if isinstance(_transport_belt_output_chest(observation), dict):
        return True
    return any(
        item.get("electric_network_connected") is not False
        and str(item.get("recipe") or "") == "transport-belt"
        and entity_item_count(item, "transport-belt") > 0
        for name in ASSEMBLER_ENTITY_NAMES
        for item in entities_named(observation, name)
    )


def _build_item_mall_should_use_output_chest(target_item: str) -> bool:
    return target_item in USER_OUTPUT_MALL_ITEMS


def _build_item_mall_available_product_count(
    observation: dict[str, Any],
    cell: dict[str, Any],
    target_item: str,
    *,
    reference_position: dict[str, float] | None = None,
) -> int:
    if target_item not in USER_OUTPUT_MALL_ITEMS:
        return total_item_count(observation, target_item)

    total = inventory_count(observation, target_item)
    assembler = cell.get("assembler") if isinstance(cell, dict) else None
    if isinstance(assembler, dict):
        total += entity_item_count(assembler, target_item)

    output_chest = cell.get("output_chest") if isinstance(cell, dict) else None
    if not isinstance(output_chest, dict) and reference_position is not None:
        output_chest = _nearest_buffered_chest_item_source(observation, target_item, reference_position)
    if isinstance(output_chest, dict) and (
        reference_position is None or distance(_position(output_chest), reference_position) <= 8.0
    ):
        total += entity_item_count(output_chest, target_item)
    return total


def _attach_build_item_mall_output_layout(
    observation: dict[str, Any],
    target_item: str,
    cell: dict[str, Any],
) -> dict[str, Any]:
    if not _build_item_mall_should_use_output_chest(target_item):
        return cell
    layout = _build_item_mall_output_layout(observation, cell["assembler_position"])
    if layout is None:
        return cell
    cell.update(layout)
    return cell


def _build_item_mall_output_layout(
    observation: dict[str, Any],
    assembler_position: dict[str, float],
) -> dict[str, Any] | None:
    orientations = [
        (WEST, {"x": 2.0, "y": 0.0}, {"x": 3.0, "y": 0.0}),
        (EAST, {"x": -2.0, "y": 0.0}, {"x": -3.0, "y": 0.0}),
        (NORTH, {"x": 0.0, "y": 2.0}, {"x": 0.0, "y": 3.0}),
        (SOUTH, {"x": 0.0, "y": -2.0}, {"x": 0.0, "y": -3.0}),
    ]
    candidates: list[dict[str, Any]] = []
    for direction, inserter_offset, chest_offset in orientations:
        inserter_position = {
            "x": assembler_position["x"] + inserter_offset["x"],
            "y": assembler_position["y"] + inserter_offset["y"],
        }
        chest_position = {
            "x": assembler_position["x"] + chest_offset["x"],
            "y": assembler_position["y"] + chest_offset["y"],
        }
        if not _build_item_mall_output_position_clear(
            observation,
            inserter_position,
            allowed_names={"inserter", "burner-inserter", "fast-inserter"},
        ):
            continue
        if not _build_item_mall_output_position_clear(
            observation,
            chest_position,
            allowed_names={"wooden-chest", "iron-chest", "steel-chest"},
        ):
            continue
        chest = _stone_output_chest_near(observation, chest_position)
        inserter = _inserter_near(observation, inserter_position)
        candidates.append(
            {
                "output_chest_position": chest_position,
                "output_inserter_position": inserter_position,
                "output_inserter_direction": direction,
                "output_chest": chest,
                "output_inserter": inserter,
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if isinstance(item.get("output_chest"), dict) and isinstance(item.get("output_inserter"), dict) else 1,
            0 if isinstance(item.get("output_chest"), dict) else 1,
            0 if isinstance(item.get("output_inserter"), dict) else 1,
        )
    )
    return candidates[0]


def _build_item_mall_output_position_clear(
    observation: dict[str, Any],
    position: dict[str, float],
    *,
    allowed_names: set[str],
) -> bool:
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
        if name in large_entities and _point_inside_machine(position, entity):
            return False
        if name in allowed_names and distance(_position(entity), position) <= 0.9:
            continue
        if distance(_position(entity), position) <= 0.65:
            return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("position"), dict):
            continue
        if str(resource.get("name") or "") in PROTECTED_RESOURCE_NAMES and distance(_position(resource), position) < 1.0:
            return False
    return True


def _build_item_mall_output_buffer_ready(cell: dict[str, Any], observation: dict[str, Any] | None = None) -> bool:
    inserter = cell.get("output_inserter")
    if not (isinstance(cell.get("output_chest"), dict) and isinstance(inserter, dict)):
        return False
    direction = cell.get("output_inserter_direction")
    if direction is not None and _direction_or_default(inserter.get("direction"), -1) != int(direction):
        return False
    if observation is not None and inserter.get("name") == "burner-inserter":
        if _regular_inserter_can_be_used(observation):
            return False
        return not _entity_status_is(inserter, "no_fuel", 53)
    if inserter.get("name") != "burner-inserter" and inserter.get("electric_network_connected") is False:
        return False
    return True


def _regular_inserter_can_be_used(observation: dict[str, Any]) -> bool:
    if inventory_count(observation, "inserter") > 0 or inventory_count(observation, "fast-inserter") > 0:
        return True
    if craftable_count(observation, "inserter") > 0 or craftable_count(observation, "fast-inserter") > 0:
        return True
    if inventory_count(observation, "electronic-circuit") <= 0 or inventory_count(observation, "iron-plate") <= 0:
        return False
    if inventory_count(observation, "iron-gear-wheel") > 0 or craftable_count(observation, "iron-gear-wheel") > 0:
        return True
    return any(
        item.get("electric_network_connected") is not False
        and str(item.get("recipe") or "") == "iron-gear-wheel"
        and entity_item_count(item, "iron-gear-wheel") > 0
        for name in ASSEMBLER_ENTITY_NAMES
        for item in entities_named(observation, name)
    )


def _build_item_mall_missing_output_chest_item(observation: dict[str, Any]) -> str:
    if (
        craftable_count(observation, "wooden-chest") > 0
        or inventory_count(observation, "wood") >= 2
        or _nearest_tree(observation) is not None
    ):
        return "wooden-chest"
    return "iron-chest"


def _available_build_item_mall_output_inserter_name(observation: dict[str, Any]) -> str | None:
    for item in ("inserter", "fast-inserter"):
        if inventory_count(observation, item) > 0:
            return item
    return None


def _build_item_mall_missing_output_inserter_item(observation: dict[str, Any]) -> str:
    return "inserter"


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
            if target_item == "transport-belt" and assembler.get("recipe") == "iron-gear-wheel":
                sidecar_position = _select_transport_belt_mall_sidecar_position(
                    observation,
                    assembler_position,
                    power_positions=[pole_position],
                )
            else:
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
            _attach_build_item_mall_output_layout(
                observation,
                target_item,
                {
                "pole_position": pole_position,
                "assembler_position": assembler_position,
                "pole_unit_number": pole_unit_number,
                "source_pole_unit_number": source_pole_unit_number,
                "powered": bool(site.get("powered") or (assembler and assembler.get("electric_network_connected"))),
                "distance": site.get("distance"),
                "pole": pole,
                "assembler": assembler,
                },
            )
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


def _select_build_item_mall_sidecar_from_existing_gear_cell(
    observation: dict[str, Any],
    target_item: str,
    *,
    allow_existing_remote: bool = False,
    reference_position: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    if target_item != "transport-belt":
        return None
    gear_cell = _find_build_item_mall_cell(
        observation,
        "iron-gear-wheel",
        allow_existing_remote=allow_existing_remote,
        reference_position=reference_position,
    )
    gear_assembler = gear_cell.get("assembler") if isinstance(gear_cell, dict) else None
    if not isinstance(gear_assembler, dict) or str(gear_assembler.get("recipe") or "") != "iron-gear-wheel":
        return None
    if gear_assembler.get("electric_network_connected") is False:
        return None
    gear_position = _position(gear_assembler)
    source_pole = gear_cell.get("pole") if isinstance(gear_cell, dict) else None
    source_pole_position = _position(source_pole) if isinstance(source_pole, dict) else None
    if source_pole_position is None and isinstance(gear_cell, dict) and isinstance(gear_cell.get("pole_position"), dict):
        source_pole_position = _xy_position(gear_cell["pole_position"])
    power_positions = [source_pole_position] if source_pole_position is not None else []
    assembler_position = _select_transport_belt_mall_sidecar_position(
        observation,
        gear_position,
        power_positions=power_positions,
    )
    if assembler_position is None:
        return None
    pole_position = source_pole_position
    pole = source_pole if isinstance(source_pole, dict) else None
    pole_unit_number = pole.get("unit_number") if isinstance(pole, dict) else None
    if pole_position is None or not _position_is_supplied_by_small_pole(
        observation,
        assembler_position,
        extra_power_positions=[pole_position],
    ):
        pole_position = _select_build_item_sidecar_supply_pole_position(
            observation,
            assembler_position,
            source_position=source_pole_position,
        )
        if pole_position is None:
            return None
        pole = _entity_near(observation, "small-electric-pole", pole_position, radius=1.0)
        pole_unit_number = pole.get("unit_number") if isinstance(pole, dict) else None
    return _attach_build_item_mall_output_layout(
        observation,
        target_item,
        {
            "pole_position": pole_position,
            "assembler_position": assembler_position,
            "pole_unit_number": pole_unit_number,
            "source_pole_unit_number": (
                source_pole.get("unit_number") if isinstance(source_pole, dict) else None
            ),
            "powered": bool(pole_unit_number),
            "distance": distance(player_position(observation), assembler_position),
            "pole": pole,
            "assembler": _entity_near(observation, "assembling-machine-1", assembler_position, radius=1.5),
        },
    )


def _transport_belt_mall_assembler_too_close_to_gear_mall(
    observation: dict[str, Any],
    belt_assembler: dict[str, Any],
) -> bool:
    if entity_item_count(belt_assembler, "transport-belt") > 0:
        return False
    belt_position = _position(belt_assembler)
    for gear in entities_named(observation, "assembling-machine-1"):
        if gear is belt_assembler or gear.get("recipe") != "iron-gear-wheel":
            continue
        if gear.get("electric_network_connected") is False:
            continue
        gear_position = _position(gear)
        if abs(gear_position["y"] - belt_position["y"]) > 0.1:
            continue
        horizontal = abs(belt_position["x"] - gear_position["x"])
        if 0 < horizontal < GEAR_BELT_MALL_ASSEMBLER_SPACING:
            return True
    return False


def _select_transport_belt_mall_sidecar_position(
    observation: dict[str, Any],
    gear_position: dict[str, float],
    *,
    power_positions: list[dict[str, float]] | None = None,
) -> dict[str, float] | None:
    offsets = [
        {"x": GEAR_BELT_MALL_ASSEMBLER_SPACING, "y": 0.0},
        {"x": -GEAR_BELT_MALL_ASSEMBLER_SPACING, "y": 0.0},
        {"x": 0.0, "y": GEAR_BELT_MALL_ASSEMBLER_SPACING},
        {"x": 0.0, "y": -GEAR_BELT_MALL_ASSEMBLER_SPACING},
    ]
    candidates: list[dict[str, float]] = []
    for offset in offsets:
        candidate = {
            "x": gear_position["x"] + offset["x"],
            "y": gear_position["y"] + offset["y"],
        }
        if _build_item_sidecar_position_clear(observation, candidate):
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=lambda item: _build_item_sidecar_power_score(observation, item, power_positions=power_positions))
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


def _nearest_connected_small_pole_supplying_position(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = [
        pole
        for pole in _connected_power_poles(observation)
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


def _block_player_mall_output_collection_after_automation(observation: dict[str, Any], target_item: str) -> bool:
    return target_item == "iron-gear-wheel" and _gear_handcraft_automation_context_active(observation)


def _is_virtual_agent(observation: dict[str, Any]) -> bool:
    """True for the no-mod RCON 'server' agent (character_valid=false / execution.virtual=true).
    Its move_to is instant (just updates a stored position), so the anti-hand-shuttle refusal that
    protects a real walking player does not apply to it."""
    player = observation.get("player")
    if isinstance(player, dict) and player.get("character_valid") is False:
        return True
    execution = observation.get("execution")
    if isinstance(execution, dict) and execution.get("virtual") is True:
        return True
    return False


def _bootstrap_seed_decision(
    action: dict[str, Any],
    reason: str,
    *,
    seed_reason: str,
    expected_followup: str,
) -> PlannerDecision:
    seeded_action = dict(action)
    seeded_action["bootstrap_seed"] = True
    seeded_action["seed_reason"] = seed_reason
    seeded_action["expected_followup"] = expected_followup
    seeded_action.setdefault("post_seed_wait_ticks", 180)
    return PlannerDecision(
        seeded_action,
        reason,
        metadata={
            "bootstrap_seed": True,
            "seed_reason": seed_reason,
            "expected_followup": expected_followup,
            "post_seed_wait_ticks": seeded_action["post_seed_wait_ticks"],
        },
    )


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
    # Bootstrap escape for the virtual RCON agent. The virtual agent teleports, so hand-carry is
    # free; only defer to a belt logistic line when that line can ACTUALLY be built right now -- i.e.
    # enough spare transport-belts exist to span the gap to the source. When the belt mall is starved
    # (too few belts to build the very line that would feed it) refusing just DEADLOCKS the whole
    # factory (observed live: belt-mall refuses 139-tile iron hand-carry -> 0 belts produced -> coal
    # feed refuses hand-crafted belts -> power stalls). So let it hand-carry to seed production; once
    # belts accumulate past the span the refusal resumes and real logistics take over. Real (walking)
    # players always get the refusal (hand-shuttling distant sites is slow for them).
    if _is_virtual_agent(observation):
        spare_belts = total_item_count(observation, "transport-belt")
        if spare_belts < max(1, int(source_distance)):
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
        and _factory_source_site_usable(site)
    ]
    candidates.extend(_entity_output_source_references(observation, item))
    if not candidates:
        return None
    return min(candidates, key=lambda site: distance(site.position, consumer_position))


def _factory_source_site_usable(site: Any) -> bool:
    status = str(getattr(site, "status", "") or "")
    if any(token in status for token in ("incomplete", "missing", "blocked")):
        return False
    machines = [str(item) for item in (getattr(site, "machines", []) or [])]
    if machines and all(item.startswith("transport-belt") for item in machines):
        return False
    return True


def _entity_output_source_references(observation: dict[str, Any], item: str) -> list[FactorySourceReference]:
    references: list[FactorySourceReference] = []
    furnace_names = {"stone-furnace", "steel-furnace", "electric-furnace"}
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or not isinstance(entity.get("position"), dict):
            continue
        name = str(entity.get("name") or "")
        recipe = str(entity.get("recipe") or "")
        if name in furnace_names and item in {"iron-plate", "copper-plate"}:
            if entity_item_count(entity, item) > 0 or recipe == item:
                references.append(
                    FactorySourceReference(
                        site_id=f"entity-source:{item}:{name}:{entity.get('unit_number') or _position_key(entity)}",
                        position=_position(entity),
                    )
                )
                continue
        if name in ASSEMBLER_ENTITY_NAMES:
            recipe_spec = RECIPES.get(recipe)
            if recipe_spec is not None and item in recipe_spec.products and entity_item_count(entity, item) > 0:
                references.append(
                    FactorySourceReference(
                        site_id=f"entity-source:{item}:{name}:{entity.get('unit_number') or _position_key(entity)}",
                        position=_position(entity),
                    )
                )
    return references


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
