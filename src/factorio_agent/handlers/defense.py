"""Defense: perimeter, watch, hardening, radars, nest sweep, piercing and normal ammunition.

Except ``ammo_production``, these run without starter upkeep. ``StarterExecutors.step``
skips the background initial-defense upkeep for ``defense_perimeter`` itself.
"""

from __future__ import annotations

from ..capability_registry import register
from .upkeep import bastion_restore, defense_upkeep_while_waiting, perimeter_restore, shared_upkeep


def defense_perimeter_step(ex, obs, parameters):
    ex.builder._sync(obs)
    return ex._defense_with_fuel(obs, ex.perimeter.step(obs))


def defense_perimeter_proof(ex, obs, result, parameters):
    return ex.perimeter.proof(obs)


def radar_network_step(ex, obs, parameters):
    ex.builder._sync(obs)
    result = ex.radars.step(obs, parameters or {})
    return defense_upkeep_while_waiting(ex, obs, result)


def radar_network_proof(ex, obs, result, parameters):
    return ex.radars.proof(obs)


def piercing_ammo_step(ex, obs, parameters):
    ex.builder._sync(obs)
    result = ex._piercing_factory().step(obs)
    return defense_upkeep_while_waiting(ex, obs, result)


def piercing_ammo_proof(ex, obs, result, parameters):
    return ex.piercing_ammo.proof(obs) if ex.piercing_ammo is not None else None


def radar_coverage_step(ex, obs, parameters):
    ex.builder._sync(obs)
    pending = ex._mall_queue_upkeep(obs, pending_only=True)
    if pending:
        return pending
    coverage = ex._coverage_factory()
    if not coverage.state or coverage.state.get("phase") == "verified":
        upkeep = ex.perimeter.upkeep(obs)
        if upkeep:
            return upkeep
    result = coverage.step(obs)
    if result.get("type") or result.get("status") != "waiting":
        return result
    # Unlike the other waits, only an upkeep action (not a block) interrupts coverage.
    upkeep = ex.perimeter.upkeep(obs)
    if upkeep and upkeep.get("type"):
        return upkeep
    return result


def radar_coverage_proof(ex, obs, result, parameters):
    return ex.radar_coverage.proof(obs) if getattr(ex, "radar_coverage", None) is not None else None


def nest_sweep_step(ex, obs, parameters):
    ex.builder._sync(obs)
    if ex.nest_sweep is None:
        from ..nest_sweep import NestSweep

        ex.nest_sweep = NestSweep(
            ex.game,
            ex.bootstrap,
            ex.builder,
            ex.catalog,
            ex.perimeter,
            ammo_source=ex._piercing_factory(),
        )
    restored = bastion_restore(ex, obs)
    if restored:
        return restored
    # Collect machine-produced raid stock before optional base topoffs.
    # Service factory inputs while stocking, then isolate committed combat.
    result = ex.nest_sweep.step(obs)
    if result.get("type") or result.get("status") != "waiting":
        return result
    state = getattr(ex.nest_sweep, "state", {})
    if not state.get("started_tick") and not state.get("funded_geometry_refresh_tick"):
        supplied = ex._piercing_upkeep(obs)
        if supplied and (supplied.get("type") or supplied.get("status") == "blocked"):
            return supplied
    upkeep = perimeter_restore(ex, obs)
    if upkeep:
        return upkeep
    return result


def nest_sweep_proof(ex, obs, result, parameters):
    return ex.nest_sweep.proof(obs) if ex.nest_sweep is not None else None


def defense_hardening_step(ex, obs, parameters):
    ex.builder._sync(obs)
    return ex.hardening.step(obs, parameters or {})


def defense_hardening_proof(ex, obs, result, parameters):
    return ex.hardening.proof(obs)


def defense_watch_step(ex, obs, parameters):
    ex.builder._sync(obs)
    coal = ex._critical_coal_upkeep(obs)
    if coal and coal.get("type"):
        return coal
    from ..upgrades import next_upgrade

    upgrade = next_upgrade(obs, ex.normal_research)
    if upgrade:
        return upgrade
    bastion = ex.bastion_maintenance.step(obs)
    if bastion:
        return bastion
    result = ex.perimeter.step(obs)
    if result.get("status") == "succeeded":
        infrastructure = ex._infrastructure_upkeep(obs)
        if infrastructure:
            return infrastructure
        belts = ex._registered_belt_upkeep(obs)
        if belts:
            return belts
        wall = ex.hardening.upkeep(obs)
        if wall:
            return wall
        repair = ex.defense_repairs.next_action(obs)
        if repair:
            return repair
        pending = ex._mall_queue_upkeep(obs, pending_only=True)
        if pending and pending.get("type"):
            return pending
        radar = None
        if not ex._mall_pending():
            radar = ex._radar_upkeep(obs)
            if radar and radar.get("type"):
                return radar
        active_radar = getattr(ex, "radar_coverage", None)
        if not pending and not (
            active_radar and getattr(active_radar, "state", {}).get("phase") not in (None, "verified")
        ):
            pending = ex._mall_queue_upkeep(obs)
            if pending and pending.get("type"):
                return pending
        supplied = ex._piercing_upkeep(obs)
        if supplied and supplied.get("type"):
            return supplied
        return {
            "status": "waiting",
            "reason": "watching armed defenses and replenishing normal ammunition",
            "evidence": {
                **result.get("evidence", {}),
                **({"piercing_factory": supplied} if supplied else {}),
                **(
                    {"infrastructure": ex.infrastructure_diagnostic}
                    if getattr(ex, "infrastructure_diagnostic", None)
                    else {}
                ),
                **(
                    {"registered_belts": ex.belt_repair_diagnostic}
                    if getattr(ex, "belt_repair_diagnostic", None)
                    else {}
                ),
                **({"radar_coverage": radar} if radar else {}),
                **({"construction_mall": pending} if pending else {}),
                **({"coal_recovery": coal} if coal else {}),
            },
        }
    return result


def ammo_production_step(ex, obs, parameters):
    early, _ = shared_upkeep(ex, obs, "ammo_production")
    if early is not None:
        return early
    return ex.ammo.step(obs)


def ammo_production_proof(ex, obs, result, parameters):
    return ex.ammo.proof(obs)


register("defense_perimeter", step=defense_perimeter_step, proof=defense_perimeter_proof)
# defense_watch never succeeds on its own; the generic flow proof applies.
register("defense_watch", step=defense_watch_step)
register("defense_hardening", step=defense_hardening_step, proof=defense_hardening_proof)
register("radar_network", step=radar_network_step, proof=radar_network_proof)
register("radar_coverage", step=radar_coverage_step, proof=radar_coverage_proof)
register("nest_sweep", step=nest_sweep_step, proof=nest_sweep_proof)
register("piercing_ammo", step=piercing_ammo_step, proof=piercing_ammo_proof)
register("ammo_production", step=ammo_production_step, proof=ammo_production_proof)
