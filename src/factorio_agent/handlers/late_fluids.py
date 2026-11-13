"""Late fluids (rocket tail): advanced petroleum with cracking, acid/lubricant cells, fluid headers.

Controllers: ``advanced_petroleum.AdvancedPetroleum`` (one block, ``advanced-petroleum.json``),
``fluid_products.FluidProducts`` (``sulfuric-acid`` | ``lubricant``), and
``fluid_distribution.FluidDistribution`` (one instance per ``source_key`` + two aliases).
Their inputs are namespaced fluid sources (``oil_gathering`` / ``water_gathering`` with a
``source_key``, or a distribution alias); ``rocket_plan.fluid_supply_rows`` emits them.
"""

from __future__ import annotations

import re

from ..capability_registry import register
from .upkeep import shared_upkeep

NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")
# Canonical registry names another controller owns (oil_gathering / water_gathering refuse them).
CANONICAL = frozenset(("water", "crude-oil", "petroleum-gas", "heavy-oil", "light-oil", "sulfuric-acid", "lubricant"))


def validate_source_key(parameters):
    """``{}`` or ``{"source_key": <bounded registry name, not another canonical fluid>}``."""
    if not isinstance(parameters, dict) or set(parameters) - {"source_key"}:
        raise ValueError("a fluid source takes {} or {source_key}")
    if "source_key" in parameters:
        key = parameters["source_key"]
        if not isinstance(key, str) or not NAME.fullmatch(key) or key in CANONICAL:
            raise ValueError("source_key must be a bounded non-canonical registry name")


def validate_advanced_petroleum(parameters):
    if parameters not in (None, {}):
        raise ValueError("advanced_petroleum takes no parameters")


def validate_fluid_product(parameters):
    from ..fluid_products import validate_fluid_product as check

    check(parameters)


def validate_fluid_distribution(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"source_key", "aliases"}:
        raise ValueError("fluid_distribution requires source_key and aliases")
    key, aliases = parameters["source_key"], parameters["aliases"]
    if (
        not isinstance(key, str)
        or not NAME.fullmatch(key)
        or not isinstance(aliases, list)
        or len(aliases) != 2
        or len(set(aliases)) != 2
        or key in aliases
        or any(not isinstance(n, str) or not NAME.fullmatch(n) or n in CANONICAL for n in aliases)
    ):
        raise ValueError("fluid_distribution needs a registry source key and two distinct new aliases")


def advanced_petroleum_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "advanced_petroleum")
    if early is not None:
        return early
    return ex._advanced_petroleum().step(obs)


def advanced_petroleum_proof(ex, obs, result, parameters):
    return ex._advanced_petroleum().proof(obs)


def fluid_product_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "fluid_product")
    if early is not None:
        return early
    return ex._fluid_products().step(obs, parameters or {})


def fluid_product_proof(ex, obs, result, parameters):
    return ex._fluid_products().proof(obs, parameters or {})


def fluid_distribution_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "fluid_distribution")
    if early is not None:
        return early
    return ex._fluid_distribution(parameters).step(obs)


def fluid_distribution_proof(ex, obs, result, parameters):
    return ex._fluid_distribution(parameters).proof(obs)


register(
    "advanced_petroleum",
    step=advanced_petroleum_step,
    proof=advanced_petroleum_proof,
    validate=validate_advanced_petroleum,
)
register("fluid_product", step=fluid_product_step, proof=fluid_product_proof, validate=validate_fluid_product)
register(
    "fluid_distribution",
    step=fluid_distribution_step,
    proof=fluid_distribution_proof,
    validate=validate_fluid_distribution,
)
