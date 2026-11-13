"""Main bus, steel, bricks, engines, advanced circuits, ``produce`` rows and the construction mall."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import defense_upkeep_while_waiting, shared_upkeep


def main_bus_factory_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "main_bus_factory")
    if early is not None:
        return early
    return ex.bus_factory.step(obs, parameters or {})


def main_bus_factory_proof(ex, obs, result, parameters):
    return ex.bus_factory.proof(obs)


def planned_main_bus_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "planned_main_bus")
    if early is not None:
        return early
    return ex.planned_bus.step(obs, parameters or {})


def planned_main_bus_proof(ex, obs, result, parameters):
    return ex.planned_bus.proof(obs)


def green_bus_reroute_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "green_bus_reroute")
    if early is not None:
        return early
    return ex.green_bus_reroute.step(obs, parameters or {})


def green_bus_reroute_proof(ex, obs, result, parameters):
    return ex.green_bus_reroute.proof(obs)


def steel_supply_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steel_supply")
    if early is not None:
        return early
    return ex.industrial_operations.steel_supply(obs)


def steel_supply_proof(ex, obs, result, parameters):
    return ex.industrial_operations.steel_supply_proof(obs)


def steel_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steel_production")
    if early is not None:
        return early
    return ex.industrial_operations.steel_production(obs)


def steel_production_proof(ex, obs, result, parameters):
    return ex.industrial_operations.steel_production_proof(obs)


def steel_capacity_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steel_capacity")
    if early is not None:
        return early
    return ex._steel_capacity().step(obs, parameters or {})


def steel_capacity_proof(ex, obs, result, parameters):
    return ex.steel_capacity.proof(obs) if ex.steel_capacity is not None else None


def validate_steel_capacity(parameters):
    from ..steel_capacity import validate_parameters

    validate_parameters(parameters)


def steel_coal_supply_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "steel_coal_supply")
    if early is not None:
        return early
    return ex.steel_coal_supply.step(obs)


def steel_coal_supply_proof(ex, obs, result, parameters):
    return ex.steel_coal_supply.proof(obs)


def brick_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "brick_production")
    if early is not None:
        return early
    return ex.industrial_operations.brick_production(obs)


def brick_production_proof(ex, obs, result, parameters):
    return ex.industrial_operations.brick_production_proof(obs)


def engine_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "engine_production")
    if early is not None:
        return early
    return ex.engines.step(obs)


def engine_production_proof(ex, obs, result, parameters):
    return ex.engines.proof(obs)


def engine_capacity_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "engine_capacity")
    if early is not None:
        return early
    return ex._engine_capacity().step(obs, parameters or {})


def engine_capacity_proof(ex, obs, result, parameters):
    return ex.engine_capacity.proof(obs) if ex.engine_capacity is not None else None


def validate_engine_capacity(parameters):
    from ..engine_capacity import validate_parameters

    validate_parameters(parameters)


def advanced_circuit_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "advanced_circuit_production")
    if early is not None:
        return early
    from ..advanced_circuits import count_for_rate

    rate = (parameters or {}).get("rate_per_minute")
    ex.advanced_circuits.count = count_for_rate(ex.catalog, rate) if rate else 2
    return ex.advanced_circuits.step(obs)


def validate_advanced_circuit_production(parameters):
    from ..advanced_circuits import validate_advanced_circuits

    validate_advanced_circuits(parameters)


def advanced_circuit_production_proof(ex, obs, result, parameters):
    return ex.advanced_circuits.proof(obs)


def produce_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "produce")
    if early is not None:
        return early
    return ex.production_rows.step(obs, parameters or {})


def produce_proof(ex, obs, result, parameters):
    return ex.production_rows.proof(obs, parameters or {})


def validate_produce(parameters):
    from ..production_rows import validate_parameters

    validate_parameters(parameters)


def construction_mall_step(ex, obs, parameters):
    # Runs without starter upkeep: radar and coal recovery come first.
    ex.builder._sync(obs)
    background = ex._radar_upkeep(obs, active_only=True)
    if background:
        return background
    recovery = getattr(ex, "base_coal_recovery", None)
    if recovery is not None and recovery.state and not recovery.verified:
        coal = ex._critical_coal_upkeep(obs)
        if coal:
            if coal.get("type") or coal.get("status") == "blocked":
                return coal
            return {
                "status": "waiting",
                "reason": "mall_waiting_for_coal_recovery_publication",
                "evidence": {"coal_recovery": coal},
            }
    result = ex._construction_factory().step(obs, parameters or {})
    if result.get("reason") == "mall_power_margin_changed":
        coal = ex._critical_coal_upkeep(obs)
        if coal and coal.get("type"):
            return coal
        if coal and coal.get("status") == "waiting":
            return {
                "status": "waiting",
                "reason": "mall_waiting_for_automatic_coal_power_recovery",
                "evidence": {"power": result.get("evidence", {}), "coal": coal},
            }
        if coal:
            result = {**result, "evidence": {**result.get("evidence", {}), "coal_recovery": coal}}
    return defense_upkeep_while_waiting(ex, obs, result)


def construction_mall_proof(ex, obs, result, parameters):
    return ex.construction_mall.proof(obs, parameters or {}) if ex.construction_mall is not None else None


def validate_construction_mall(parameters):
    from ..construction_mall import _product

    _product(parameters)


register("main_bus_factory", step=main_bus_factory_step, proof=main_bus_factory_proof)
register("planned_main_bus", step=planned_main_bus_step, proof=planned_main_bus_proof)
register("green_bus_reroute", step=green_bus_reroute_step, proof=green_bus_reroute_proof)
register("steel_supply", step=steel_supply_step, proof=steel_supply_proof)
register("steel_production", step=steel_production_step, proof=steel_production_proof)
register("steel_capacity", step=steel_capacity_step, proof=steel_capacity_proof, validate=validate_steel_capacity)
register("steel_coal_supply", step=steel_coal_supply_step, proof=steel_coal_supply_proof)
register("brick_production", step=brick_production_step, proof=brick_production_proof)
register("engine_production", step=engine_production_step, proof=engine_production_proof)
register("engine_capacity", step=engine_capacity_step, proof=engine_capacity_proof, validate=validate_engine_capacity)
register(
    "advanced_circuit_production",
    step=advanced_circuit_production_step,
    proof=advanced_circuit_production_proof,
    validate=validate_advanced_circuit_production,
)
register("produce", step=produce_step, proof=produce_proof, validate=validate_produce)
register(
    "construction_mall",
    step=construction_mall_step,
    proof=construction_mall_proof,
    validate=validate_construction_mall,
)
