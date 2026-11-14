"""Shared upkeep that capability handlers run around their own controller.

Not a capability module: nothing here registers a handler.
"""

from __future__ import annotations

from ..core.deterministic_bootstrap import FUEL_WAIT

# These capabilities also survey registered pipes and belts before their own step.
INFRASTRUCTURE_REPAIR_CAPABILITIES = (
    "basic_petroleum",
    "gas_distribution",
    "chemical_production",
    "advanced_circuit_production",
    "chemical_science",
)
# These capabilities never let an equipment upgrade run first.
NO_UPGRADE_CAPABILITIES = ("research", "bootstrap_research", "defense_perimeter")


def shared_upkeep(ex, obs, capability):
    """Starter upkeep, background construction, pipe/belt repair and upgrades before a capability's own step.

    Returns ``(early, filtered)``: ``early`` is the result to return instead of
    the capability's own step, or None to continue; ``filtered`` is the starter
    observation (computed before upkeep, as the starter capacity step needs it).
    """
    # Retain starter supplies during power construction without handfeeding
    # burners now owned by the automatic power plan.
    ex.builder._sync(obs)
    filtered = ex.starter_observation(obs)
    result = ex.starter_upkeep(obs)
    # A plain wait for burner fuel to pile up (no take or insert is possible yet) does not stop the
    # job: in fleet aud-1114-0841 "waiting for automated coal output" held the district and the bus
    # 4.6 min per run on average (the mining outposts 5.1 min) while coal cells refilled. The next pass
    # checks fuel first again, so the coal trip still happens as soon as a batch is there.
    if result.get("status") != "succeeded" and not result.get(FUEL_WAIT):
        return result, filtered
    background = ex._continue_background_construction(obs, capability)
    if background:
        return background, filtered
    cell = getattr(ex, "supply_cell", None)
    feed = cell.upkeep(obs) if cell is not None else None
    if isinstance(feed, dict) and feed.get("type"):  # top up the supply cell from plates in hand
        return feed, filtered
    if capability in INFRASTRUCTURE_REPAIR_CAPABILITIES:
        infrastructure = ex._infrastructure_upkeep(obs)
        if infrastructure:
            return infrastructure, filtered
        belts = ex._registered_belt_upkeep(obs)
        if belts:
            return belts, filtered
    if capability not in NO_UPGRADE_CAPABILITIES:
        from ..upgrades import next_upgrade

        upgrade = next_upgrade(obs, ex.normal_research)
        if upgrade:
            return upgrade, filtered
    return None, filtered


def bastion_restore(ex, obs):
    """Bastion maintenance's result when it acts or is blocked, else None."""
    restored = ex.bastion_maintenance.step(obs)
    if restored and (restored.get("type") or restored.get("status") == "blocked"):
        return restored
    return None


def perimeter_restore(ex, obs):
    """Perimeter upkeep's result when it acts or is blocked, else None."""
    upkeep = ex.perimeter.upkeep(obs)
    if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
        return upkeep
    return None


def defense_upkeep_while_waiting(ex, obs, result, *, bastion=True):
    """``result`` unless it is a plain wait; while waiting, bastion maintenance and perimeter upkeep go first."""
    if result.get("type") or result.get("status") != "waiting":
        return result
    if bastion:
        restored = bastion_restore(ex, obs)
        if restored:
            return restored
    upkeep = perimeter_restore(ex, obs)
    if upkeep:
        return upkeep
    return result
