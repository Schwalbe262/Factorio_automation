"""Planned-world walk of the late (oil/chemistry) planners on the real catalog.

A fresh map with the planned main bus and no development-map files (no ``bus-factory.json``,
no fixed engine block): every late planner's pure ``prepare`` must yield a plan or a named
wait on an earlier roadmap/tail row, never a development-map assumption. Stage 1 runs them on
empty registries; stage 2 marks the earlier rows built (fluid sources published, plastic
verified) and checks the planners then reach their planned-world path (SiteService block,
planned-bus taps). Native routing, pipe survey and proofs are faked: only provable live.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_v2_planned_tap as tapfix
from factorio_agent.core.deterministic_builder import FactoryBuilder
from v2_blocks_fixture import real_catalog

CATALOG = real_catalog()

# Reasons that only a development-map world produces (bus-factory, fixed engine block, BusTap).
DEV_MAP = {
    "acid_requires_verified_shared_main_bus",
    "acid_paid_iron_tap_required",
    "acid_shared_bus_prerequisite_unavailable",
    "acid_bus_capacity_or_paid_frontier_extension_required",
    "advanced_circuit_preparation_context_unavailable",
    "advanced_circuit_bus_authority_identity_changed",
    "advanced_circuit_bus_authority_unavailable",
    "blue_science_planning_context_incompatible",
    "another_bus_tap_cutover_unfinished",
}
# Named waits on an earlier row: (reason, the key/item it names) -> the row that publishes it.
WAITS = {
    ("fluid_product_paid_source_required", "acid-water"): "rocket_water_acid_water",
    ("fluid_product_paid_source_required", "lubricant-heavy-oil"): "rocket_split_lubricant_heavy_oil",
    ("explicit_usable_chemical_source_missing", "petroleum-gas"): "basic_petroleum",
    ("commissioned_automatic_plastic_factory_required", None): "chemical_production_plastic",
    ("commissioned_automatic_blue_ingredient_producer_required", "engine-unit"): "engine_production",
    ("published_native_fluid_source_required", None): "the fluid's producer row",
    ("exact_eastbound_original_petroleum_output_required", None): "basic_petroleum",
    ("advanced_independent_fluid_source_required", None): "rocket_crude_advanced_crude",
}


@unittest.skipIf(CATALOG is None, "real catalog not available")
class PlannedWorldTailTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.harness = tapfix.Harness(self.root)
        board = self.harness.board()
        board["catalog_fingerprint"] = CATALOG.fingerprint
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        self.registry = {"world_id": "w", "catalog_fingerprint": CATALOG.fingerprint, "sources": {}, "handoffs": {}}
        self.write_registry()
        (self.root / "material-sources.json").write_text(json.dumps(self.registry))
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root),
            expected_world="w",
            query=Mock(return_value={"ok": False, "reason": "fake_native"}),
        )
        self.builder = SimpleNamespace(
            _occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan, ensure_plan=Mock()
        )
        self.perimeter = SimpleNamespace(prepare_coverage=Mock(return_value={"ok": True}))
        self.obs = {
            "ok": True,
            "world_id": "w",
            "tick": 1,
            "entities": list(self.harness.entities),
            "inventory": {},
            "technologies": {t: True for t in CATALOG.technologies},
            "enabled_recipes": {r: True for r in CATALOG.recipes},
        }
        self.site = SimpleNamespace(allocate_candidates=Mock(return_value=[]), keep_only=Mock())

    def write_registry(self):
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))

    def planners(self):
        from factorio_agent.advanced_circuits import AdvancedCircuitProduction
        from factorio_agent.advanced_petroleum import AdvancedPetroleum
        from factorio_agent.chemical_production import ChemicalProduction
        from factorio_agent.chemical_science import ChemicalScience
        from factorio_agent.fluid_distribution import FluidDistribution
        from factorio_agent.fluid_products import FluidProducts
        from factorio_agent.gas_distribution import GasDistribution

        g, b, c, p, s = self.game, self.builder, CATALOG, self.perimeter, self.site
        chem = ChemicalProduction(g, b, c, p, site=s)
        return chem, [
            ("advanced_petroleum", lambda: AdvancedPetroleum(g, b, c, p, site=s).prepare(self.obs)),
            (
                "fluid_product",
                lambda: FluidProducts(g, b, c, p, site=s).prepare(self.obs, {"product": "sulfuric-acid"}),
            ),
            ("fluid_product", lambda: FluidProducts(g, b, c, p, site=s).prepare(self.obs, {"product": "lubricant"})),
            ("chemical_production", lambda: chem.prepare(self.obs, {"product": "plastic-bar"})),
            ("chemical_production", lambda: chem.prepare(self.obs, {"product": "sulfur"})),
            (
                "advanced_circuit_production",
                lambda: AdvancedCircuitProduction(g, b, c, p, chemical=chem, site=s).prepare(self.obs),
            ),
            ("chemical_science", lambda: ChemicalScience(g, b, c, p, chemical=chem, site=s).prepare(self.obs)),
            (
                "fluid_distribution",
                lambda: FluidDistribution(g, b, c, p, source_key="sulfuric-acid", aliases=("x", "x-2")).prepare(
                    self.obs
                ),
            ),
            ("gas_distribution", lambda: GasDistribution(g, b, c, p).prepare(self.obs)),
        ]

    def test_stage_1_every_late_planner_waits_on_a_named_earlier_row(self):
        _, rows = self.planners()
        for capability, prepare in rows:
            result = prepare()
            reason = result.get("reason")
            self.assertNotIn(reason, DEV_MAP, (capability, result))
            if result.get("ok"):
                continue
            named = result.get("registry_key") or result.get("item")
            self.assertIn((reason, named), WAITS, (capability, result))

    def test_stage_2_built_sources_reach_the_planned_world_paths(self):
        # Earlier rows built: acid water/gas and lubricant heavy oil published, plastic verified.
        for key, fluid, n in (
            ("acid-water", "water", 0),
            ("acid-gas", "petroleum-gas", 1),
            ("lubricant-heavy-oil", "heavy-oil", 2),
        ):
            self.registry["sources"][key] = dict(
                kind="fluid",
                item=fluid,
                name="pipe",
                position={"x": 80.5, "y": -40.5 - 4 * n},
                facing=4,
                direction="output",
                fluidbox_index=1,
                world_id="w",
                unit_number=90 + n,
            )
        self.write_registry()
        snap = dict(
            amount=100, extent={"left_top": {"x": 80, "y": -50}, "right_bottom": {"x": 81, "y": -40}}, limit=320
        )
        self.game.query = Mock(
            return_value=dict(ok=True, snapshots={f: dict(snap) for f in ("water", "petroleum-gas", "heavy-oil")})
        )
        chem, rows = self.planners()
        plastic = {"kind": "item", "item": "plastic-bar", "position": {"x": 120.5, "y": -60.5}, "facing": 4}
        chem.state = {
            "world_id": "w",
            "catalog_fingerprint": CATALOG.fingerprint,
            "products": {"plastic-bar": {"phase": "verified", "plan": {"entities": []}}},
        }
        chem.proof = Mock(return_value={"source": dict(plastic, direction="output", world_id="w", unit_number=700)})
        self.obs["entities"].append(dict(plastic, name="transport-belt", direction=4, unit_number=700))
        wanted = {
            # The site service has no block for them here: the planned-world path names the site, not the bus.
            ("fluid_product", "fluid_product_no_site_allocation"),
            ("advanced_circuit_production", "advanced_circuit_no_site_allocation"),
        }
        seen = set()
        with patch("factorio_agent.planned_tap.max_span", return_value=5):
            for capability, prepare in rows:
                result = prepare()
                self.assertNotIn(result.get("reason"), DEV_MAP, (capability, result))
                seen.add((capability, result.get("reason")))
        self.assertLessEqual(wanted, seen, seen)
        asked = {call.kwargs["name"] for call in self.site.allocate_candidates.call_args_list}
        self.assertLessEqual(
            {"fluid_product_sulfuric-acid", "fluid_product_lubricant", "advanced_circuit_production"}, asked
        )


if __name__ == "__main__":
    unittest.main()
