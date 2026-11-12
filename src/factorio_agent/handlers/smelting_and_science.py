"""Smelting district, smelting capacity, paired smelting, furnace upgrades, red/green/blue science."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import shared_upkeep


def smelting_district_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "smelting_district")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.smelting.step(obs)


def smelting_proof(ex, obs, result, parameters):
    return ex.smelting.proof(obs)


def smelting_capacity_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "smelting_capacity")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.smelting.capacity_step(obs, parameters or {})


def validate_smelting_capacity(parameters):
    from ..smelting import validate_capacity

    validate_capacity(parameters)


def paired_smelting_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "paired_smelting")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.paired.step(obs)


def paired_smelting_proof(ex, obs, result, parameters):
    return ex.paired.proof(obs)


def furnace_upgrade_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "furnace_upgrade")
    if early is not None:
        return early
    return ex.furnace_upgrade.step(obs, parameters or {})


def furnace_upgrade_proof(ex, obs, result, parameters):
    proof = ex.furnace_upgrade.proof(obs)
    if proof:
        from ..capacity_profiles import refresh_bus_capacity

        profile = refresh_bus_capacity(ex.game, ex.catalog, ex.bus_factory)
        if profile is not None and not profile.get("ok"):
            return None
        proof = {**proof, "bus_capacity_profile": profile}
    return proof


def validate_furnace_upgrade(parameters):
    from ..furnace_upgrade import validate_upgrade

    validate_upgrade(parameters)


def automation_science_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "automation_science")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.science.step(obs, parameters or {})


def automation_science_proof(ex, obs, result, parameters):
    return ex.science.proof(obs)


def validate_automation_science(parameters):
    from ..science import validate_rate

    validate_rate(parameters)


def logistic_science_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "logistic_science")
    if early is not None:
        return early
    return ex.green.step(obs, parameters or {})


def logistic_science_proof(ex, obs, result, parameters):
    return ex.green.proof(obs)


def validate_logistic_science(parameters):
    from ..green_science import validate_rate

    validate_rate(parameters)


def chemical_science_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "chemical_science")
    if early is not None:
        return early
    return ex.blue.step(obs)


def chemical_science_proof(ex, obs, result, parameters):
    return ex.blue.proof(obs)


register("smelting_district", step=smelting_district_step, proof=smelting_proof)
register("smelting_capacity", step=smelting_capacity_step, proof=smelting_proof, validate=validate_smelting_capacity)
register("paired_smelting", step=paired_smelting_step, proof=paired_smelting_proof)
register("furnace_upgrade", step=furnace_upgrade_step, proof=furnace_upgrade_proof, validate=validate_furnace_upgrade)
register(
    "automation_science",
    step=automation_science_step,
    proof=automation_science_proof,
    validate=validate_automation_science,
)
register(
    "logistic_science", step=logistic_science_step, proof=logistic_science_proof, validate=validate_logistic_science
)
register("chemical_science", step=chemical_science_step, proof=chemical_science_proof)
