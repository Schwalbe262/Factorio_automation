"""Early game: starter cells, steam power, starter capacity, component stock, power regularization."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import shared_upkeep


def starter_step(ex, obs, parameters):
    ex.builder._sync(obs)
    return ex.starter_upkeep(obs)


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
    return ex.builder.ensure_power(obs)


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
register(
    "starter_capacity", step=starter_capacity_step, proof=starter_capacity_proof, validate=validate_starter_capacity
)
register(
    "prepare_components",
    step=prepare_components_step,
    proof=prepare_components_proof,
    validate=validate_prepare_components,
)
register("regularize_power_layout", step=regularize_power_layout_step, proof=regularize_power_layout_proof)
