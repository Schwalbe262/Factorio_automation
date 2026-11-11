"""Refresh derived bus source budgets after a verified physical source upgrade."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from factorio_ai.deterministic_state import _atomic_json
from .bus_tap import reconstruct_bus
from .main_bus import capacity_report
from .smelting import bank_capacities


def refresh_bus_capacity(game, catalog, bus_factory):
    """Caller owns the writer lock and has just verified the source in game.

    Only derived capacity fields change. Per-file interruption is safe to retry:
    profiles may differ, while every non-capacity contract must still agree.
    """
    root = Path(game.cfg.runtime_dir)
    source_path, board_path = root/'smelting-district.json', root/'pcb-board.json'
    factory_path = Path(bus_factory.path)
    if not all(p.exists() for p in (source_path, board_path, factory_path)):
        return None
    source, board, factory = [json.loads(p.read_text(encoding='utf-8'))
                              for p in (source_path, board_path, factory_path)]
    if 'shared_main_bus' not in board or 'bus' not in factory:
        return None
    world = source.get('world_id')
    if (not world or any(d.get('world_id') != world or d.get('catalog_fingerprint') != catalog.fingerprint
                         for d in (source, board, factory))):
        return {'ok': False, 'reason': 'capacity_profile_world_or_catalog_changed'}
    buses = (factory['bus'], board['shared_main_bus'])
    contracts = [{k:v for k,v in b.items() if k != 'capacity'} for b in buses]
    if contracts[0] != contracts[1]:
        return {'ok': False, 'reason': 'capacity_profile_bus_contracts_differ'}
    try:
        materials, taps, _, _, _, _ = reconstruct_bus(buses[0])
        capacities = bank_capacities(catalog, source['banks'])
        by_product = {b['product']: capacities[b['resource']] for b in source['banks']}
        refreshed = []
        for material in materials:
            if material.item in by_product:
                if len(material.source_rates) != 1:
                    return {'ok': False, 'reason': 'parallel_source_profiles_require_explicit_source_mapping'}
                material = replace(material, source_rates=(by_product[material.item],))
            refreshed.append(material)
        profile = capacity_report(refreshed, taps)
    except (KeyError, ValueError, TypeError) as exc:
        return {'ok': False, 'reason': 'capacity_profile_source_contract_invalid', 'detail': str(exc)}
    changed = False
    for document, key, path in ((factory, 'bus', factory_path), (board, 'shared_main_bus', board_path)):
        if document[key].get('capacity') != profile:
            document[key]['capacity'] = deepcopy(profile)
            _atomic_json(path, document)
            changed = True
    bus_factory.state = factory
    return {'ok': True, 'changed': changed, 'capacity': profile,
            'scope': 'derived catalog source budgets; live throughput is measured separately'}
