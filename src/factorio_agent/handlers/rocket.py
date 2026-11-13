"""The rocket tail: ``rocket_silo``, ``rocket_parts``, ``rocket_launch`` (``rocket_stages``, ``rocket_feed``)."""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import defense_upkeep_while_waiting, shared_upkeep


def _no_parameters(parameters):
    if not isinstance(parameters, dict) or parameters:
        raise ValueError("this capability accepts no parameters")


def rocket_silo_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "rocket_silo")
    if early is not None:
        return early
    return defense_upkeep_while_waiting(ex, obs, ex._rocket_silo().step(obs, parameters or {}))


def rocket_silo_proof(ex, obs, result, parameters):
    return ex._rocket_silo().proof(obs, parameters or {})


def rocket_parts_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "rocket_parts")
    if early is not None:
        return early
    return defense_upkeep_while_waiting(ex, obs, ex._rocket_parts().step(obs, parameters or {}))


def rocket_parts_proof(ex, obs, result, parameters):
    return ex._rocket_parts().proof(obs, parameters or {})


def rocket_launch_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "rocket_launch")
    if early is not None:
        return early
    return defense_upkeep_while_waiting(ex, obs, ex._rocket_launch().step(obs, parameters or {}))


def rocket_launch_proof(ex, obs, result, parameters):
    return ex._rocket_launch().proof(obs, parameters or {})


def rocket_launch_record(ex, obs, action, receipt, parameters):
    ex._rocket_launch().record_action(obs, action, receipt)


register("rocket_silo", step=rocket_silo_step, proof=rocket_silo_proof, validate=_no_parameters)
register("rocket_parts", step=rocket_parts_step, proof=rocket_parts_proof, validate=_no_parameters)
register(
    "rocket_launch",
    step=rocket_launch_step,
    proof=rocket_launch_proof,
    validate=_no_parameters,
    record_action=rocket_launch_record,
)
