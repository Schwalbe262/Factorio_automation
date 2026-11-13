"""Early game: starter cells, steam power, starter capacity, component stock, supply cell, power regularization."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import shared_upkeep


# Iron cells the starter itself builds before it waits for the first unlocks.
# One cell (15 plates/min) made the 50-plate steam-power unlock and the boiler,
# engine and lab that follow a long wait (user rule: start with two).
STARTER_IRON_CELLS = 2


def starter_step(ex, obs, parameters):
    ex.builder._sync(obs)
    result = ex.starter_upkeep(obs)
    capacity = getattr(ex, "capacity", None)
    if capacity is not None and isinstance(result, dict) and result.get("status") == "waiting":
        # Everything the starter can do now is done; use the wait for the second iron cell.
        extra = capacity.step(obs, {"resource": "iron-ore", "cells": STARTER_IRON_CELLS})
        if isinstance(extra, dict) and extra.get("type"):
            return extra
    return _precraft_science(ex, obs, result)


def _precraft_science(ex, obs, result):
    """While ``result`` is only a wait, hand-craft a small batch of starter science packs (``precraft.py``)."""
    if isinstance(result, dict) and not result.get("type") and result.get("status") == "waiting":
        from ..precraft import starter_science

        action = starter_science(ex.catalog, obs)
        if action is not None:
            return action
    return result


def starter_proof(ex, obs, result, parameters):
    produced = obs.get("production", {})
    if not all(produced.get(name, {}).get("produced", 0) > 0 for name in ("iron-plate", "copper-plate")):
        return None
    techs = {"steam-power", "electronics", "automation-science-pack"}
    if not techs.issubset(obs.get("technologies", {})):
        return None
    return {
        "tick": obs["tick"],
        "natural_technologies": sorted(techs),
        "machine_production": {n: produced[n] for n in ("iron-plate", "copper-plate")},
        **result.get("evidence", {}),
    }


def power_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "power")
    if early is not None:
        return early
    # repair: only the power job diagnoses and repairs a built plant that never runs (built_block_repair).
    result = ex.builder.ensure_power(obs, repair=True)
    from ..power_coal_feed import publish_planned_feed  # reserved, unbuilt coal feed on the board

    return publish_planned_feed(ex, obs) or _precraft_science(ex, obs, result)


def power_coal_feed_step(ex, obs, parameters):
    """Phase 2 of starter power: the dedicated coal drill and belt (``power_coal_feed.py``)."""
    early, _ = shared_upkeep(ex, obs, "power_coal_feed")
    if early is not None:
        return early
    from ..power_coal_feed import publish_planned_feed

    result = ex.builder.ensure_coal_feed(obs)
    return publish_planned_feed(ex, obs) or result


def starter_capacity_step(ex, obs, parameters):
    early, filtered = shared_upkeep(ex, obs, "starter_capacity")
    if early is not None:
        return early
    return ex.capacity.step(filtered, parameters or {})


def starter_capacity_proof(ex, obs, result, parameters):
    return ex.capacity.proof(obs, parameters or {})


def validate_starter_capacity(parameters):
    from ..mining import validate_parameters

    validate_parameters(parameters)


def prepare_components_step(ex, obs, parameters):
    return ex._component_reserve().step(obs, parameters or {})


def prepare_components_proof(ex, obs, result, parameters):
    return ex._component_reserve().proof(obs, parameters or {})


def validate_prepare_components(parameters):
    from ..component_reserve import validate_parameters

    validate_parameters(parameters)


def supply_cell_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "supply_cell")
    if early is not None:
        return early
    return ex.supply_cell.step(obs, parameters or {})


def supply_cell_proof(ex, obs, result, parameters):
    if (parameters or {}).get("retire"):
        return ex.supply_cell.retire_proof(obs)
    if "retire_product" in (parameters or {}):
        return ex.supply_cell.retire_product_proof(obs, parameters["retire_product"])
    return ex.supply_cell.proof(obs)


def validate_supply_cell(parameters):
    from ..supply_cell import validate_parameters

    validate_parameters(parameters)


def starter_retirement_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "starter_retirement")
    if early is not None:
        return early
    return ex.starter_retirement.step(obs, parameters or {})


def starter_retirement_proof(ex, obs, result, parameters):
    return ex.starter_retirement.proof(obs, parameters or {})


def validate_starter_retirement(parameters):
    from ..starter_retirement import validate_parameters

    validate_parameters(parameters)


def wood_burner_feed_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "wood_burner_feed")
    if early is not None:
        return early
    return ex.wood_burner_feed.step(obs, parameters or {})


def wood_burner_feed_proof(ex, obs, result, parameters):
    return ex.wood_burner_feed.proof(obs)


def validate_wood_burner_feed(parameters):
    from ..wood_burner_feed import validate_parameters

    validate_parameters(parameters)


def regularize_power_layout_step(ex, obs, parameters):
    ex.builder._sync(obs)
    fuel_obs = ex.starter_observation(obs)
    fuel = ex.bootstrap.maintain_fuel(fuel_obs)
    if fuel is not None:
        return fuel
    return ex.power_layout.step(obs)


def regularize_power_layout_proof(ex, obs, result, parameters):
    return ex.power_layout.proof(obs)


register("starter", step=starter_step, proof=starter_proof)
# power has no own proof: the generic flow proof (executors.flow_proof) applies.
register("power", step=power_step)
# power_coal_feed: the generic flow proof applies as well (flow_verified, verification_ticks).
register("power_coal_feed", step=power_coal_feed_step)
register(
    "starter_capacity", step=starter_capacity_step, proof=starter_capacity_proof, validate=validate_starter_capacity
)
register(
    "prepare_components",
    step=prepare_components_step,
    proof=prepare_components_proof,
    validate=validate_prepare_components,
)
register("supply_cell", step=supply_cell_step, proof=supply_cell_proof, validate=validate_supply_cell)
register(
    "starter_retirement",
    step=starter_retirement_step,
    proof=starter_retirement_proof,
    validate=validate_starter_retirement,
)
register(
    "wood_burner_feed", step=wood_burner_feed_step, proof=wood_burner_feed_proof, validate=validate_wood_burner_feed
)
register("regularize_power_layout", step=regularize_power_layout_step, proof=regularize_power_layout_proof)
