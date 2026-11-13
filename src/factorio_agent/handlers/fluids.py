"""Fluids: oil and water gathering, water distribution, petroleum, chemistry, gas distribution."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import defense_upkeep_while_waiting, shared_upkeep


def oil_gathering_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "oil_gathering")
    if early is not None:
        return early
    return ex._oil_source((parameters or {}).get("source_key")).step(obs)


def oil_gathering_proof(ex, obs, result, parameters):
    return ex._oil_source((parameters or {}).get("source_key")).proof(obs)


def water_gathering_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "water_gathering")
    if early is not None:
        return early
    return ex._water_source((parameters or {}).get("source_key")).step(obs)


def water_gathering_proof(ex, obs, result, parameters):
    return ex._water_source((parameters or {}).get("source_key")).proof(obs)


def validate_source_profile(parameters):
    """``{}`` (the canonical source) or ``{"source_key": <registry name>}`` (an independent namespaced source)."""
    from .late_fluids import validate_source_key

    validate_source_key(parameters)


def water_distribution_step(ex, obs, parameters):
    # Runs without starter upkeep; only perimeter upkeep may act while it waits.
    ex.builder._sync(obs)
    result = ex._water_distribution().step(obs)
    return defense_upkeep_while_waiting(ex, obs, result, bastion=False)


def water_distribution_proof(ex, obs, result, parameters):
    return ex.water_distribution.proof(obs) if ex.water_distribution is not None else None


def basic_petroleum_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "basic_petroleum")
    if early is not None:
        return early
    return ex.petroleum.step(obs)


def basic_petroleum_proof(ex, obs, result, parameters):
    return ex.petroleum.proof(obs)


def chemical_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "chemical_production")
    if early is not None:
        return early
    return ex.chemicals.step(obs, parameters or {})


def chemical_production_proof(ex, obs, result, parameters):
    return ex.chemicals.proof(obs, parameters or {})


def validate_chemical_production(parameters):
    from ..chemical_production import validate_chemical

    validate_chemical(parameters)


def gas_distribution_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "gas_distribution")
    if early is not None:
        return early
    return ex.gas_distribution.step(obs)


def gas_distribution_proof(ex, obs, result, parameters):
    return ex.gas_distribution.proof(obs)


register("oil_gathering", step=oil_gathering_step, proof=oil_gathering_proof, validate=validate_source_profile)
register("water_gathering", step=water_gathering_step, proof=water_gathering_proof, validate=validate_source_profile)
register("water_distribution", step=water_distribution_step, proof=water_distribution_proof)
register("basic_petroleum", step=basic_petroleum_step, proof=basic_petroleum_proof)
register(
    "chemical_production",
    step=chemical_production_step,
    proof=chemical_production_proof,
    validate=validate_chemical_production,
)
register("gas_distribution", step=gas_distribution_step, proof=gas_distribution_proof)
