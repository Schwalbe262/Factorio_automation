"""``StarterExecutors``: the controllers of one ``run`` command and the capability dispatch.

``__init__`` and the lazy ``_xxx()`` factories construct the controllers.
``step``/``proof``/``record_action`` run a shared prelude, then the handler
registered for the capability (``capability_registry``, ``handlers/``).
"""

from __future__ import annotations

import json
from pathlib import Path

from .capability_registry import get_handler, handler_for
from .jobs import (
    CAPABILITIES,
    IDLE_RESEARCH_INTERVAL_TICKS,
    Ledger,
    _owns_research,
    _unresolved_world_effect,
    is_active,
)


def flow_proof(obs: dict, result: dict) -> dict | None:
    """Generic proof for a capability without its own: machine plates plus a verified flow of >= 1800 ticks."""
    produced = obs.get("production", {})
    if not all(produced.get(name, {}).get("produced", 0) > 0 for name in ("iron-plate", "copper-plate")):
        return None
    evidence = result.get("evidence", {})
    if evidence.get("flow_verified") and evidence.get("verification_ticks", 0) >= 1800:
        return {"tick": obs["tick"], **evidence}
    return None


class StarterExecutors:
    """Explicit temporary v1 executor reuse, with no v1 strategic supervisor."""

    def __init__(self, game, catalog):
        self.game, self.catalog = game, catalog
        from .construction_supply import V2Bootstrap
        from .power_layout import V2FactoryBuilder

        self.bootstrap = V2Bootstrap(game, catalog)
        self.builder = V2FactoryBuilder(game, self.bootstrap, catalog)
        from .mining import StarterCapacity
        from .research import StarterResearch

        self.capacity = StarterCapacity(game, self.bootstrap)
        self.research = StarterResearch(game, self.bootstrap, catalog)
        from .power_layout import RegularizePower

        self.power_layout = RegularizePower(game, self.builder)
        from .industry import BootstrapLogistics

        self.industry = BootstrapLogistics(game, self.bootstrap, self.builder, catalog)
        self.bootstrap_research_targets = {}
        from .electric_mining import ElectricCoal

        self.electric_coal = ElectricCoal(game, self.bootstrap, self.builder)
        from .site.service import SiteService

        # One map survey + master plan per runtime, shared by every site consumer.
        self.site = SiteService(game)
        from .outposts import MiningOutposts

        self.outposts = MiningOutposts(game, self.bootstrap, self.builder, site=self.site)
        from .smelting import SmeltingDistrict

        self.smelting = SmeltingDistrict(game, self.bootstrap, self.builder, catalog, site=self.site)
        from .paired_smelting import PairedSmelting

        self.paired = PairedSmelting(game, self.builder, catalog, self.smelting)
        from .paired_mining import PairedMining

        self.paired_mining = PairedMining(game, self.bootstrap, self.builder, catalog, self.outposts, self.smelting)
        from .science import AutomaticScience

        self.science = AutomaticScience(game, self.builder, catalog, site=self.site)
        from .green_science import LogisticScience

        self.green = LogisticScience(game, self.builder, catalog, self.smelting, self.science, site=self.site)
        from .steam_expansion import SteamExpansion

        self.steam_expansion = SteamExpansion(game, self.builder, catalog, self.outposts)
        from .defense_perimeter import DefensePerimeter

        self.perimeter = DefensePerimeter(game, self.bootstrap, catalog)
        from .defense_repairs import DefenseRepairs

        self.defense_repairs = DefenseRepairs(game, self.bootstrap, self.perimeter)
        from .defense_hardening import DefenseHardening

        self.hardening = DefenseHardening(game, self.bootstrap, self.builder, catalog, self.perimeter)
        self.hardening.wall_repair = self.defense_repairs.wall_action
        from .bastion_maintenance import BastionMaintenance

        self.bastion_maintenance = BastionMaintenance(game, self.bootstrap, catalog, self.perimeter, self.hardening)
        self.nest_sweep = None
        self.piercing_ammo = None
        self.piercing_path = Path(game.cfg.runtime_dir) / "piercing-ammo.json"
        self.construction_mall = None
        self.radar_coverage = None
        self.construction_mall_path = Path(game.cfg.runtime_dir) / "construction-mall.json"
        self.bootstrap.construction_stock_source = self._construction_stock
        self.base_coal_recovery = None
        self.water_distribution = None
        from .radar_network import RadarNetwork

        self.radars = RadarNetwork(game, self.builder, catalog, self.perimeter)
        self.outposts.perimeter = self.perimeter
        from .bus_factory import BusFactory

        self.bus_factory = BusFactory(game, self.builder, catalog, self.smelting, self.science, self.green)
        from .bus_tap import BusTap
        from .machine_stock import MachineStock
        from .production_rows import ProductionRows

        self.production_rows = ProductionRows(
            game,
            self.builder,
            catalog,
            site=self.site,
            tap_factory=lambda: BusTap(
                game, self.builder, catalog, bus_factory=self.bus_factory, science=self.science, green=self.green
            ),
        )
        # Finished produce rows are construction stock; large hand crafts wait for them.
        self.bootstrap.machine_stock = MachineStock(self.production_rows)
        from .inventory_storage import InventoryStorage

        self.storage = InventoryStorage(game, catalog)
        from .material_supply import DedicatedMaterials

        self.material_supply = DedicatedMaterials(game, self.builder, catalog, self.outposts, perimeter=self.perimeter)
        self.chemical_coal = DedicatedMaterials(
            game, self.builder, catalog, self.outposts, perimeter=self.perimeter, purpose="chemistry"
        )
        from .resource_reroute import SteamCoalReroute

        self.steam_coal_reroute = SteamCoalReroute(game, self.builder, catalog, self.steam_expansion, self.outposts)
        from .green_bus_reroute import GreenBusReroute

        self.green_bus_reroute = GreenBusReroute(
            game, self.builder, catalog, self.bus_factory, self.science, self.green, self.smelting
        )
        from .legacy_coal_cleanup import LegacyCoalCleanup

        self.legacy_coal_cleanup = LegacyCoalCleanup(
            game, self.bootstrap, self.builder, catalog, self.storage, self.steam_expansion, self.outposts
        )
        from .ore_transport_reroute import OreTransportReroute

        self.ore_transport_reroute = OreTransportReroute(
            game, self.builder, catalog, self.smelting, self.outposts, bus_factory=self.bus_factory, green=self.green
        )
        from .furnace_upgrade import FurnaceUpgrade

        self.furnace_upgrade = FurnaceUpgrade(
            game, self.builder, catalog, self.smelting, self.paired, outposts=self.outposts
        )
        from .industrial import IndustrialOperations

        self.industrial_operations = IndustrialOperations(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
        )
        from .shared_coal_tap import SharedCoalTap

        self.steel_coal_supply = SharedCoalTap(
            game, self.builder, catalog, self.industrial_operations.brick, industrial=self.industrial_operations
        )
        from .oil_gathering import OilGathering

        self.oil_gathering = OilGathering(game, self.builder, catalog, self.perimeter)
        from .water_gathering import WaterGathering

        self.water = WaterGathering(game, self.builder, catalog, self.perimeter)
        from .petroleum import BasicPetroleum

        self.petroleum = BasicPetroleum(game, self.builder, catalog, self.perimeter)
        from .ammo_production import AmmoProduction

        self.ammo = AmmoProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
        )
        self.perimeter.ammo_source = self.ammo
        from .engine_production import EngineProduction

        self.engines = EngineProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
            steel=self.industrial_operations.steel,
        )
        from .defense_relocation import DefenseRelocation

        self.engines.relocation = DefenseRelocation(
            game, self.bootstrap, self.builder, catalog, self.perimeter, self.engines
        )
        from .chemical_production import ChemicalProduction

        self.chemicals = ChemicalProduction(game, self.builder, catalog, self.perimeter)
        from .gas_distribution import GasDistribution

        self.gas_distribution = GasDistribution(
            game, self.builder, catalog, self.perimeter, petroleum=self.petroleum, chemical=self.chemicals
        )
        self.infrastructure_repair = None
        self.registered_belt_repair = None
        self.steel_capacity = None
        self.engine_capacity = None
        from .advanced_circuits import AdvancedCircuitProduction

        self.advanced_circuits = AdvancedCircuitProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
            chemical=self.chemicals,
        )
        from .chemical_science import ChemicalScience

        self.blue = ChemicalScience(
            game,
            self.builder,
            catalog,
            self.perimeter,
            engine=self.engines,
            advanced=self.advanced_circuits,
            chemical=self.chemicals,
            science=self.science,
            green=self.green,
            bus_factory=self.bus_factory,
        )
        from .normal_research import NormalResearch

        self.normal_research = NormalResearch(game, catalog, self.science, self.green, blue=self.blue)
        ledger = Ledger(Path(game.cfg.runtime_dir)).data
        self.radar_auto_enabled = any(
            j["capability"] == "radar_network" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )
        self.mall_auto_enabled = any(
            j["capability"] == "construction_mall" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )
        self.registered_belt_autodetect = (
            any(
                j["capability"] == "advanced_circuit_production" and j["status"] == "succeeded"
                for j in ledger["jobs"].values()
            )
            and (Path(game.cfg.runtime_dir) / "advanced-circuits.json").exists()
        )
        self.infrastructure_autodetect = any(
            j["capability"] == "basic_petroleum" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        ) and all(
            (Path(game.cfg.runtime_dir) / name).exists()
            for name in ("basic-petroleum.json", "pcb-board.json", "fluid-sources.json")
        )
        self.base_coal_autodetect = any(
            j["capability"] == "steam_expansion" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        ) and all(
            (Path(game.cfg.runtime_dir) / name).exists()
            for name in ("factory-builder.json", "steam-coal-reroute.json", "mining-outposts.json")
        )
        self.industrial_mode = any(
            j["capability"] == "smelting_district" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )

    def _piercing_factory(self):
        if self.piercing_ammo is None:
            from .piercing_ammo import PiercingAmmo

            self.piercing_ammo = PiercingAmmo(self.game, self.builder, self.catalog, self.perimeter)
        return self.piercing_ammo

    def _bootstrap_research(self, parameters):
        from .industry import BootstrapLogistics, validate_bootstrap_research

        validate_bootstrap_research(parameters)
        name = parameters["technology"]
        if not hasattr(self, "bootstrap_research_targets"):
            self.bootstrap_research_targets = {}
        if name not in self.bootstrap_research_targets:
            self.bootstrap_research_targets[name] = BootstrapLogistics(
                self.game, self.bootstrap, self.builder, self.catalog, technology=name
            )
        return self.bootstrap_research_targets[name]

    def record_action(self, capability, obs, action, receipt, parameters=None):
        pipeline = getattr(self, "science_pipeline", None)
        if pipeline is not None:
            pipeline.record_action(action, receipt)
            if pipeline.target_controller is not None:
                self.industry = pipeline.target_controller
        handler = get_handler(capability)
        if handler is not None and handler.record_action is not None:
            handler.record_action(self, obs, action, receipt, parameters)

    def _component_reserve(self):
        if getattr(self, "components", None) is None:
            from .component_reserve import ComponentReserve

            self.components = ComponentReserve(self.game, self.bootstrap, self.catalog)
        return self.components

    def _science_wait_action(self, obs, batch, result):
        if (
            result.get("status") != "waiting"
            or result.get("type")
            or not result.get("evidence", {}).get("autonomous_wait")
        ):
            return result
        from .science_pipeline import SciencePipeline

        if getattr(self, "science_pipeline", None) is None:
            self.science_pipeline = SciencePipeline(self.game, self.bootstrap, self.builder, self.catalog)
        action = self.science_pipeline.next_action(obs, batch)
        if action and self.science_pipeline.target_controller is not None:
            self.industry = self.science_pipeline.target_controller
        return action or result

    def _funded_startup_owner(self, obs):
        # Any finite red-only batch owns its running research; the state file
        # only exists after that batch passed native admission and was funded.
        from .industry import validate_bootstrap_research

        technology = obs.get("research")
        try:
            validate_bootstrap_research({"technology": technology})
        except ValueError:
            return None
        if technology == "logistics" or obs.get("technologies", {}).get(technology):
            return None
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if (
            not isinstance(root, (str, Path))
            or not (Path(root) / ("bootstrap-research-" + technology + ".json")).exists()
        ):
            return None
        batch = self._bootstrap_research({"technology": technology})
        allocated = batch.state.get("allocated_inputs", {})
        return batch if any(type(n) is int and n > 0 for n in allocated.values()) else None

    def _existing_startup_batch(self, parameters):
        if parameters.get("technology") != "electric-mining-drill":
            return None
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if not isinstance(root, (str, Path)):
            return None
        if not (Path(root) / "bootstrap-research-electric-mining-drill.json").exists():
            return None
        batch = self._bootstrap_research(parameters)
        # A real reserved batch, including an invalid binding, must be handled
        # by its owner rather than paying for a second research implementation.
        return batch if batch.state else None

    def _construction_factory(self):
        if self.construction_mall is None:
            from .construction_mall import ConstructionMall

            self.construction_mall = ConstructionMall(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                bus_factory=self.bus_factory,
                science=self.science,
                green=self.green,
                smelting=self.smelting,
            )
        return self.construction_mall

    def _construction_stock(self, obs, item, count):
        factory = getattr(self, "construction_mall", None)
        if factory is None:
            path = getattr(self, "construction_mall_path", None)
            if path is None or not path.exists():
                return None
            factory = self._construction_factory()
        return factory.ensure_stock(obs, item, count) if factory.has_stock_cell(item) else None

    def _coverage_factory(self):
        if getattr(self, "radar_coverage", None) is None:
            from .radar_coverage import RadarCoverage

            self.radar_coverage = RadarCoverage(
                self.game, self.builder, self.catalog, self.perimeter, radars=self.radars
            )
        return self.radar_coverage

    def _mall_pending(self):
        factory = getattr(self, "construction_mall", None)
        path = getattr(self, "construction_mall_path", None)
        if factory is None and path is not None and path.exists():
            factory = self._construction_factory()
        return tuple(getattr(factory, "pending_items", ())) if factory is not None else ()

    def _mall_queue_upkeep(self, obs, *, pending_only=False):
        pending = self._mall_pending()
        if not pending and (pending_only or not getattr(self, "mall_auto_enabled", False)):
            return None
        if obs.get("tick", 0) < getattr(self, "mall_retry_tick", 0):
            return getattr(self, "mall_diagnostic", None)
        factory = self._construction_factory()
        product = (
            pending[0]
            if pending
            else next(
                (
                    q
                    for q in ("underground-belt", "splitter", "assembling-machine-1", "inserter", "gun-turret")
                    if not factory.has_stock_cell(q)
                ),
                None,
            )
        )
        if product is None:
            return None
        result = factory.step(obs, {"product": product})
        self.mall_diagnostic = result if not result.get("type") else None
        if result.get("status") == "blocked":
            self.mall_retry_tick = obs.get("tick", 0) + 1800
        return None if result.get("status") == "succeeded" else result

    def _radar_upkeep(self, obs, *, active_only=False):
        factory = getattr(self, "radar_coverage", None)
        active = (
            factory is not None and bool(getattr(factory, "state", {})) and factory.state.get("phase") != "verified"
        )
        if active_only and not active:
            return None
        if not active and not getattr(self, "radar_auto_enabled", False):
            return None
        if obs.get("tick", 0) < getattr(self, "radar_retry_tick", 0):
            return getattr(self, "radar_diagnostic", None) if active else None
        factory = self._coverage_factory()
        result = factory.upkeep(obs)
        self.radar_diagnostic = result if not result.get("type") else None
        if not result.get("type"):
            self.radar_retry_tick = obs.get("tick", 0) + (
                1800 if result.get("status") in ("succeeded", "blocked") else 180
            )
        return None if result.get("status") == "succeeded" else result

    def _continue_background_construction(self, obs, capability):
        # A controller's board snapshot stays valid until that build completes.
        if capability in ("power", "starter", "research", "defense_perimeter", "steam_expansion", "electric_coal"):
            return None
        if self._mall_pending():
            return self._mall_queue_upkeep(obs, pending_only=True)
        return self._radar_upkeep(obs, active_only=True)

    def _coal_recovery(self):
        if self.base_coal_recovery is None:
            from .base_coal_recovery import BaseCoalRecovery

            self.base_coal_recovery = BaseCoalRecovery(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                steam=self.steam_expansion,
                outposts=self.outposts,
                reroute=self.steam_coal_reroute,
            )
        return self.base_coal_recovery

    def _water_distribution(self):
        if self.water_distribution is None:
            from .water_distribution import WaterDistribution

            self.water_distribution = WaterDistribution(
                self.game, self.builder, self.catalog, self.perimeter, water=self.water
            )
        return self.water_distribution

    def _critical_coal_upkeep(self, obs):
        if obs.get("tick", 0) < getattr(self, "coal_retry_tick", 0):
            return getattr(self, "coal_retry_diagnostic", None)
        factory = getattr(self, "base_coal_recovery", None)
        if factory is None:
            if not getattr(self, "base_coal_autodetect", False):
                return None
            factory = self._coal_recovery()
        if not factory.state:
            diagnostic = factory.depletion_diagnostic(obs)
            if not diagnostic.get("ok") or not diagnostic.get("depleted"):
                return None
        result = factory.step(obs)
        if result.get("status") == "blocked":
            self.coal_retry_tick = obs["tick"] + 1800
            self.coal_retry_diagnostic = result
        return None if result.get("status") == "succeeded" else result

    def _steel_capacity(self):
        if self.steel_capacity is None:
            from .steel_capacity import SteelCapacity

            self.steel_capacity = SteelCapacity(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                steel=self.industrial_operations.steel,
                industrial=self.industrial_operations,
            )
        return self.steel_capacity

    def _engine_capacity(self):
        if self.engine_capacity is None:
            from .engine_capacity import EngineCapacity

            self.engine_capacity = EngineCapacity(
                self.game, self.builder, self.catalog, self.perimeter, engine=self.engines
            )
        return self.engine_capacity

    def _piercing_upkeep(self, obs):
        factory = getattr(self, "piercing_ammo", None)
        if factory is None:
            path = getattr(self, "piercing_path", None)
            if path is None or not path.exists():
                return None
            factory = self._piercing_factory()
        return factory.upkeep(obs) if getattr(factory, "commissioned", False) else None

    def _infrastructure_upkeep(self, obs):
        """Survey registered pipes without making a failed survey stop defenses."""
        if obs.get("tick", 0) < getattr(self, "infrastructure_retry_tick", 0):
            return None
        factory = getattr(self, "infrastructure_repair", None)
        if factory is None:
            if not getattr(self, "infrastructure_autodetect", False):
                return None
            from .infrastructure_repair import InfrastructureRepair

            factory = self.infrastructure_repair = InfrastructureRepair(
                self.game, self.builder, self.catalog, petroleum=self.petroleum, gas=self.gas_distribution
            )
        result = factory.step(obs)
        self.infrastructure_diagnostic = factory.last_diagnostic
        if result and result.get("type"):
            return result
        # Material waits retry promptly; quiet or rejected surveys stay bounded.
        delay = (
            180
            if (factory.last_diagnostic or {}).get("reason") == "normally prepay registered pipe replacements"
            else 1800
        )
        self.infrastructure_retry_tick = obs.get("tick", 0) + delay
        return None

    def _registered_belt_upkeep(self, obs):
        if obs.get("tick", 0) < getattr(self, "belt_repair_retry_tick", 0):
            return None
        factory = getattr(self, "registered_belt_repair", None)
        if factory is None:
            if not getattr(self, "registered_belt_autodetect", False):
                return None
            from .registered_belt_repair import RegisteredBeltRepair

            factory = self.registered_belt_repair = RegisteredBeltRepair(
                self.game, self.builder, self.catalog, advanced=self.advanced_circuits
            )
        result = factory.step(obs)
        self.belt_repair_diagnostic = factory.last_diagnostic
        if result and result.get("type"):
            return result
        delay = (
            180
            if (factory.last_diagnostic or {}).get("reason") == "normally fund registered belt replacements"
            else 1800
        )
        self.belt_repair_retry_tick = obs.get("tick", 0) + delay
        return None

    def starter_observation(self, obs):
        return {
            **obs,
            "entities": [
                e
                for e in obs.get("entities", [])
                if not self.builder.owns_automated_burner(e)
                and not self.smelting.owns_furnace(e, obs)
                and not (getattr(self, "paired", None) and self.paired.owns_furnace(e, obs))
                and not (getattr(self, "paired_mining", None) and self.paired_mining.retires_legacy(e, obs))
                and not (getattr(self, "legacy_coal_cleanup", None) and self.legacy_coal_cleanup.retires_legacy(e, obs))
                and not (getattr(self, "steam_expansion", None) and self.steam_expansion.owns_burner(e, obs))
            ],
        }

    def _defense_with_fuel(self, obs, result):
        if result and not result.get("type") and result.get("status") == "waiting":
            fuel = self.bootstrap.maintain_fuel(self.starter_observation(obs))
            if fuel and fuel.get("type"):
                return fuel
        return result

    def _same_world_jobs(self, obs):
        """Persisted jobs of the observed world, or None when no matching ledger is readable."""
        game = getattr(self, "game", None)
        if game is None:
            return None
        path = Path(game.cfg.runtime_dir) / "jobs.json"
        if not path.exists():
            return None
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("schema_version") != 2 or ledger.get("world_id") != obs.get("world_id"):
            return None
        return [job for job in ledger.get("jobs", {}).values() if job.get("world_id") == obs["world_id"]]

    def idle_action(self, obs):
        """Start lab research while no job acts, or return None.

        None while research is running, while any job has an unresolved world
        effect or an active job owns research (a capability taking a
        ``technology``), within IDLE_RESEARCH_INTERVAL_TICKS of the previous
        attempt, or without a proven automatic lab science supply. Otherwise
        ``research_policy.choose_research`` picks a technology steered by the
        upcoming roadmap steps, and the ``research`` capability's own step
        produces the start action, so its shape and start record are unchanged.
        """
        if obs.get("research"):
            return None
        jobs = self._same_world_jobs(obs)
        if jobs is None or _unresolved_world_effect(jobs):
            return None
        if any(is_active(job) and _owns_research(job.get("capability")) for job in jobs):
            return None
        last = getattr(self, "idle_research_tick", None)
        if last is not None and 0 <= obs["tick"] - last < IDLE_RESEARCH_INTERVAL_TICKS:
            return None
        self.idle_research_tick = obs["tick"]
        supplied = self.normal_research.supplied_packs(obs)
        if not supplied:
            return None
        from . import progression, research_policy

        wanted = progression.roadmap_wanted_technologies(jobs, obs.get("technologies") or {})
        choice = research_policy.choose_research(
            self.catalog,
            obs,
            wanted_recipes=progression.roadmap_wanted_recipes(jobs, CAPABILITIES),
            wanted_technologies=[name for name in wanted if name in self.catalog.technologies],
            preferences=research_policy.legacy_upgrade_preferences(),
            supplied_packs=supplied,
        )
        if choice is None:
            return None
        action = self.normal_research.step(obs, {"technology": choice.technology})
        return action if action.get("type") == "research" else None

    def _approved_initial_defense(self, obs, *, maintain_armed=False):
        """Finish an already requested perimeter; never invent a defense job."""
        perimeter = getattr(self, "perimeter", None)
        if perimeter is None:
            return None
        adaptive = bool(getattr(perimeter, "adaptive_enabled", False))
        armed = getattr(perimeter, "state", {}).get("phase") == "armed"
        if armed and not maintain_armed and not adaptive:
            return None
        if not all(obs.get("enabled_recipes", {}).get(name) for name in ("gun-turret", "firearm-magazine")):
            return None
        same_world = self._same_world_jobs(obs)
        # No background defense mutation during any unresolved world effect,
        # even an empty durable intent or a clean older defense approval.
        if same_world is None or _unresolved_world_effect(same_world):
            return None
        rows = [job for job in same_world if job.get("capability") == "defense_perimeter"]
        known = {"pending", "running", "waiting", "paused", "blocked", "failed", "succeeded"}
        if not any(job.get("status") in known and job.get("parameters") == {} for job in rows):
            return None
        result = perimeter.step(obs) if adaptive or not armed else perimeter.upkeep(obs)
        if (
            adaptive
            and result.get("status") == "blocked"
            and str(result.get("reason", "")).startswith("adaptive_defense_")
        ):
            self.defense_policy_diagnostic = result
            result = perimeter.upkeep(obs)
        result = self._defense_with_fuel(obs, result)
        # Commissioning defense cannot complete the unrelated mining/build job.
        return result if result and (result.get("type") or result.get("status") != "succeeded") else None

    def starter_upkeep(self, obs):
        if getattr(self, "storage", None):
            storage = self.storage.step(obs)
            if storage:
                return storage
        if getattr(self, "bastion_maintenance", None):
            bastion = self.bastion_maintenance.step(obs)
            if bastion:
                return bastion
        if getattr(self, "perimeter", None):
            defense = self.perimeter.upkeep(obs)
            if defense:
                return defense
        if getattr(self, "defense_repairs", None):
            if getattr(self, "hardening", None):
                wall = self.hardening.upkeep(obs)
                if wall:
                    return wall
            repair = self.defense_repairs.next_action(obs)
            if repair:
                return repair
        filtered = self.starter_observation(obs)
        if not getattr(self, "industrial_mode", False):
            return self.bootstrap.next_action(filtered)
        if self.smelting.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "industrial_supply_world_changed"}
        required = {"steam-power", "electronics", "automation-science-pack"}
        if not required.issubset(obs.get("technologies", {})):
            return {"status": "blocked", "reason": "industrial_natural_unlocks_missing"}
        upkeep = self.bootstrap.maintain_fuel(filtered)
        return upkeep or {"status": "succeeded", "reason": "industrial supplies retain ordinary starter fuel upkeep"}

    def step(self, capability: str, obs: dict, parameters: dict | None = None) -> dict:
        """The prelude every capability shares, then its registered handler's step."""
        from factorio_agent.core.deterministic_repair_control import pending_repair

        cleanup = pending_repair(obs)
        if cleanup:
            return cleanup
        # Retired and not in CAPABILITIES; an old ledger row gets this stable answer.
        if capability == "nest_assault":
            return {"status": "blocked", "reason": "staged_assault_retired_use_concentrated_nest_sweep"}
        if capability == "starter_research" and obs.get("technologies", {}).get((parameters or {}).get("technology")):
            return self.research.step(obs, parameters or {})
        gun_bridge = capability == "bootstrap_research" and (parameters or {}).get("technology") == "gun-turret"
        if capability != "defense_perimeter" and not gun_bridge:
            initial_defense = self._approved_initial_defense(obs, maintain_armed=capability == "bootstrap_research")
            if initial_defense:
                return initial_defense
        return handler_for(capability).step(self, obs, parameters)

    def proof(self, capability: str, obs: dict, result: dict, parameters: dict | None = None) -> dict | None:
        handler = get_handler(capability)
        if handler is not None and handler.proof is not None:
            return handler.proof(self, obs, result, parameters)
        return flow_proof(obs, result)
