"""Capabilities that own the single research slot (``jobs.RESEARCH_CAPABILITIES``).

``StarterExecutors.step`` handles two research special cases before these
handlers run: an already researched ``starter_research`` technology goes
straight to ``ex.research.step``, and ``bootstrap_research`` maintains an
armed perimeter (and skips it entirely for ``gun-turret``).
"""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import shared_upkeep


def starter_research_step(ex, obs, parameters):
    batch = ex._existing_startup_batch(parameters or {})
    if batch is not None:
        ex.builder._sync(obs)
        return ex._science_wait_action(obs, batch, batch.step(obs))
    early, _ = shared_upkeep(ex, obs, "starter_research")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return prebuild_science_cell(ex, obs, parameters, ex.research.step(obs, parameters or {}))


def prebuild_science_cell(ex, obs, parameters, result):
    """While the starter research only waits, build the next bootstrap batch's science cell (early start)."""
    from ..industry import CELL_UNLOCK_TECHNOLOGY

    if result.get("type") or result.get("status") != "waiting":
        return result
    if (parameters or {}).get("technology") != CELL_UNLOCK_TECHNOLOGY:
        return result
    try:
        target = ex._next_bootstrap_target(obs)
        action = target.prebuild(obs) if target is not None else None
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        return result
    return action or result


def starter_research_proof(ex, obs, result, parameters):
    if obs.get("technologies", {}).get((parameters or {}).get("technology")):
        return ex.research.proof(obs, parameters or {})
    batch = ex._existing_startup_batch(parameters or {})
    if batch is not None:
        return batch.proof(obs)
    return ex.research.proof(obs, parameters or {})


def starter_research_record_action(ex, obs, action, receipt, parameters):
    batch = ex._existing_startup_batch(parameters or {})
    if batch is not None:
        batch.record_action(action, receipt)


def validate_starter_research(parameters):
    from ..research import validate_parameters

    validate_parameters(parameters)


def bootstrap_research_step(ex, obs, parameters):
    # Early defense research must not first rebuild remote damaged
    # starter/coal assets. The controller checks its own live power.
    ex.builder._sync(obs)
    batch = ex._bootstrap_research(parameters or {})
    return ex._science_wait_action(obs, batch, batch.step(obs))


def bootstrap_research_proof(ex, obs, result, parameters):
    return ex._bootstrap_research(parameters or {}).proof(obs)


def bootstrap_research_record_action(ex, obs, action, receipt, parameters):
    ex._bootstrap_research(parameters or {}).record_action(action, receipt)


def validate_bootstrap_research(parameters):
    from ..industry import validate_bootstrap_research

    validate_bootstrap_research(parameters)


def bootstrap_logistics_step(ex, obs, parameters):
    batch = ex._funded_startup_owner(obs)
    if batch is not None:
        ex.builder._sync(obs)
        result = ex._science_wait_action(obs, batch, batch.step(obs))
        # Shared machines must finish their paid current owner before
        # a second science target can use them. Its proof does not
        # complete this logistics job.
        if result.get("status") == "succeeded":
            return {"status": "waiting", "reason": "funded_science_prerequisite_finished", "evidence": result}
        return result
    if obs.get("technologies", {}).get("logistics") or ex.industry.local_power_ready(obs):
        ex.builder._sync(obs)
        # Back to back: a funded logistics batch funds the next bootstrap batch while it waits.
        return ex._science_wait_action(obs, ex.industry, ex.industry.step(obs))
    early, _ = shared_upkeep(ex, obs, "bootstrap_logistics")
    if early is not None:
        return early
    result = ex.builder.ensure_power(obs)
    if result.get("status") != "succeeded":
        return result
    return ex.industry.step(obs)


def bootstrap_logistics_proof(ex, obs, result, parameters):
    return ex.industry.proof(obs)


def bootstrap_logistics_record_action(ex, obs, action, receipt, parameters):
    batch = ex._funded_startup_owner(obs)
    if batch is not None:
        batch.record_action(action, receipt)
    else:
        ex.industry.record_action(action, receipt)


def research_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "research")
    if early is not None:
        return early
    parameters = parameters or {}
    plan = None
    if hasattr(ex, "research_queue_plan") and isinstance(parameters.get("technology"), str):
        plan = ex.research_queue_plan(obs, first=parameters["technology"])
    return ex.normal_research.step(obs, parameters, plan=plan)


def research_proof(ex, obs, result, parameters):
    return ex.normal_research.proof(obs, parameters or {})


def validate_research(parameters):
    from ..normal_research import validate_parameters

    validate_parameters(parameters)


register(
    "starter_research",
    step=starter_research_step,
    proof=starter_research_proof,
    record_action=starter_research_record_action,
    validate=validate_starter_research,
)
register(
    "bootstrap_research",
    step=bootstrap_research_step,
    proof=bootstrap_research_proof,
    record_action=bootstrap_research_record_action,
    validate=validate_bootstrap_research,
)
register(
    "bootstrap_logistics",
    step=bootstrap_logistics_step,
    proof=bootstrap_logistics_proof,
    record_action=bootstrap_logistics_record_action,
)
register("research", step=research_step, proof=research_proof, validate=validate_research)
