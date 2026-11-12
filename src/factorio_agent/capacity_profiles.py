"""Refresh derived bus source budgets after a verified physical source upgrade."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .bus_tap import reconstruct_bus
from .main_bus import capacity_report
from .smelting import bank_capacities


def _source_capacities(game, catalog, source):
    capacities = bank_capacities(catalog, source["banks"])
    evidence = None
    from .depleted_drill_retirement import FILE as RETIREMENT_FILE, mining_capacity_summary

    if (Path(game.cfg.runtime_dir) / RETIREMENT_FILE).exists():
        evidence = mining_capacity_summary(game, catalog)
        if not evidence.get("ok"):
            raise ValueError("fresh active mining backing required: " + str(evidence.get("reason")))
        for bank in source["banks"]:
            resource, product = bank["resource"], bank["product"]
            recipe = catalog.recipes[product]
            ingredients = [q for q in recipe["ingredients"] if q["name"] == resource]
            products = [q for q in recipe["products"] if q["name"] == product]
            if (
                len(ingredients) != 1
                or len(products) != 1
                or ingredients[0].get("amount", 0) <= 0
                or products[0].get("amount", 0) <= 0
            ):
                raise ValueError("one deterministic ore-to-plate conversion required")
            backed = evidence["resources"].get(resource, 0) * products[0]["amount"] / ingredients[0]["amount"]
            capacities[resource] = min(capacities[resource], backed)
    return capacities, evidence


def rebase_tap_capacity(game, catalog, old_bus, new_bus, current_buses):
    """Rebase only derived profiles after a verified source upgrade, read-only."""
    root = Path(game.cfg.runtime_dir)
    try:
        source = json.loads((root / "smelting-district.json").read_text(encoding="utf-8"))
        upgrade = json.loads((root / "furnace-upgrade.json").read_text(encoding="utf-8"))
        if (
            upgrade.get("phase") != "verified"
            or upgrade.get("pending")
            or any(
                d.get("world_id") != getattr(game, "expected_world", None)
                or d.get("catalog_fingerprint") != catalog.fingerprint
                for d in (source, upgrade)
            )
        ):
            return {"ok": False, "reason": "tap_capacity_requires_verified_source_upgrade"}
        contracts = [{k: v for k, v in b.items() if k != "capacity"} for b in (old_bus, new_bus)]
        if any({k: v for k, v in b.items() if k != "capacity"} not in contracts for b in current_buses):
            return {"ok": False, "reason": "tap_capacity_cannot_rebase_changed_bus_contract"}
        capacities, mining = _source_capacities(game, catalog, source)
        by_product = {b["product"]: capacities[b["resource"]] for b in source["banks"]}

        def profile(bus):
            materials, taps, *_ = reconstruct_bus(bus)
            refreshed = []
            for material in materials:
                if material.item in by_product:
                    if len(material.source_rates) != 1:
                        raise ValueError("explicit parallel source mapping required")
                    material = replace(material, source_rates=(by_product[material.item],))
                refreshed.append(material)
            return capacity_report(refreshed, taps)

        if any(b.get("capacity") != profile(b) for b in current_buses):
            return {"ok": False, "reason": "tap_current_capacity_disagrees_with_verified_source"}
        old, new = deepcopy(old_bus), deepcopy(new_bus)
        old["capacity"] = profile(old)
        new["capacity"] = profile(new)
        return {"ok": True, "old_bus": old, "new_bus": new, "mining_source_potential": mining}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": "tap_capacity_rebase_unavailable", "detail": str(exc)}


def refresh_bus_capacity(game, catalog, bus_factory):
    """Caller owns the writer lock and has just verified the source in game.

    Only derived capacity fields change. Per-file interruption is safe to retry:
    profiles may differ, while every non-capacity contract must still agree.
    """
    root = Path(game.cfg.runtime_dir)
    source_path, board_path = root / "smelting-district.json", root / "pcb-board.json"
    factory_path = Path(bus_factory.path)
    if not all(p.exists() for p in (source_path, board_path, factory_path)):
        return None
    source, board, factory = [
        json.loads(p.read_text(encoding="utf-8")) for p in (source_path, board_path, factory_path)
    ]
    if "shared_main_bus" not in board or "bus" not in factory:
        return None
    world = source.get("world_id")
    if not world or any(
        d.get("world_id") != world or d.get("catalog_fingerprint") != catalog.fingerprint
        for d in (source, board, factory)
    ):
        return {"ok": False, "reason": "capacity_profile_world_or_catalog_changed"}
    buses = (factory["bus"], board["shared_main_bus"])
    contracts = [{k: v for k, v in b.items() if k != "capacity"} for b in buses]
    if contracts[0] != contracts[1]:
        return {"ok": False, "reason": "capacity_profile_bus_contracts_differ"}
    try:
        materials, taps, _, _, _, _ = reconstruct_bus(buses[0])
        capacities, mining = _source_capacities(game, catalog, source)
        by_product = {b["product"]: capacities[b["resource"]] for b in source["banks"]}
        refreshed = []
        for material in materials:
            if material.item in by_product:
                if len(material.source_rates) != 1:
                    return {"ok": False, "reason": "parallel_source_profiles_require_explicit_source_mapping"}
                material = replace(material, source_rates=(by_product[material.item],))
            refreshed.append(material)
        profile = capacity_report(refreshed, taps)
    except (KeyError, ValueError, TypeError) as exc:
        return {"ok": False, "reason": "capacity_profile_source_contract_invalid", "detail": str(exc)}
    changed = False
    for document, key, path in ((factory, "bus", factory_path), (board, "shared_main_bus", board_path)):
        if document[key].get("capacity") != profile:
            document[key]["capacity"] = deepcopy(profile)
            _atomic_json(path, document)
            changed = True
    bus_factory.state = factory
    return {
        "ok": True,
        "changed": changed,
        "capacity": profile,
        "mining_source_potential": mining,
        "scope": "derived furnace ceiling capped by fresh resource-backed powered mining after retirement; live throughput is measured separately",
    }
