"""Mining and coal: electric coal, outposts, paired mining, dedicated materials, steam coal, coal recovery."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import defense_upkeep_while_waiting, shared_upkeep


def electric_coal_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "electric_coal")
    if early is not None:
        return early
    # During cutover the old source may be absent for one observed
    # action. Do not run ensure_power here and rebuild that old drill.
    return ex.electric_coal.step(obs)


def electric_coal_proof(ex, obs, result, parameters):
    return ex.electric_coal.proof(obs)


def mining_outpost_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "mining_outpost")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.outposts.step(obs, parameters or {})


def mining_outpost_proof(ex, obs, result, parameters):
    return ex.outposts.proof(obs, parameters or {})


def validate_mining_outpost(parameters):
    from ..outposts import validate_parameters

    validate_parameters(parameters)


def paired_mining_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "paired_mining")
    if early is not None:
        return early
    return ex.paired_mining.step(obs)


def paired_mining_proof(ex, obs, result, parameters):
    return ex.paired_mining.proof(obs)


def dedicated_materials_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "dedicated_materials")
    if early is not None:
        return early
    return ex.material_supply.step(obs, parameters or {})


def dedicated_materials_proof(ex, obs, result, parameters):
    return ex.material_supply.proof(obs, parameters or {})


def validate_dedicated_materials(parameters):
    from ..material_supply import validate_material

    validate_material(parameters)


def chemical_coal_supply_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "chemical_coal_supply")
    if early is not None:
        return early
    return ex.chemical_coal.step(obs, {"resource": "coal"})


def chemical_coal_supply_proof(ex, obs, result, parameters):
    return ex.chemical_coal.proof(obs, {"resource": "coal"})


def steam_expansion_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steam_expansion")
    if early is not None:
        return early
    return ex.steam_expansion.step(obs)


def steam_expansion_proof(ex, obs, result, parameters):
    return ex.steam_expansion.proof(obs)


def steam_coal_reroute_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steam_coal_reroute")
    if early is not None:
        return early
    return ex.steam_coal_reroute.step(obs, parameters or {})


def steam_coal_reroute_proof(ex, obs, result, parameters):
    return ex.steam_coal_reroute.proof(obs)


def legacy_coal_cleanup_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "legacy_coal_cleanup")
    if early is not None:
        return early
    return ex.legacy_coal_cleanup.step(obs, parameters or {})


def legacy_coal_cleanup_proof(ex, obs, result, parameters):
    return ex.legacy_coal_cleanup.proof(obs)


def ore_transport_reroute_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "ore_transport_reroute")
    if early is not None:
        return early
    return ex.ore_transport_reroute.step(obs, parameters or {})


def ore_transport_reroute_proof(ex, obs, result, parameters):
    return ex.ore_transport_reroute.proof(obs)


def base_coal_recovery_step(ex, obs, parameters):
    # Runs without starter upkeep; only perimeter upkeep may act while it waits.
    ex.builder._sync(obs)
    result = ex._coal_recovery().step(obs)
    return defense_upkeep_while_waiting(ex, obs, result, bastion=False)


def base_coal_recovery_proof(ex, obs, result, parameters):
    return ex.base_coal_recovery.proof(obs) if ex.base_coal_recovery is not None else None


register("electric_coal", step=electric_coal_step, proof=electric_coal_proof)
register("mining_outpost", step=mining_outpost_step, proof=mining_outpost_proof, validate=validate_mining_outpost)
register("paired_mining", step=paired_mining_step, proof=paired_mining_proof)
register(
    "dedicated_materials",
    step=dedicated_materials_step,
    proof=dedicated_materials_proof,
    validate=validate_dedicated_materials,
)
register("chemical_coal_supply", step=chemical_coal_supply_step, proof=chemical_coal_supply_proof)
register("steam_expansion", step=steam_expansion_step, proof=steam_expansion_proof)
register("steam_coal_reroute", step=steam_coal_reroute_step, proof=steam_coal_reroute_proof)
register("legacy_coal_cleanup", step=legacy_coal_cleanup_step, proof=legacy_coal_cleanup_proof)
register("ore_transport_reroute", step=ore_transport_reroute_step, proof=ore_transport_reroute_proof)
register("base_coal_recovery", step=base_coal_recovery_step, proof=base_coal_recovery_proof)
