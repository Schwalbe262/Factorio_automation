"""Late fluid blocks wired for the rocket tail: registration, factories, resync, cracking balance."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import plan_needs, postmortem, remedy
from factorio_agent import rocket_plan as rp
from factorio_agent.capability_registry import handler_for, registered_names
from factorio_agent.core.world_catalog import _amount, _material, _yield
from factorio_agent.handlers.late_fluids import validate_fluid_distribution, validate_source_key
from factorio_agent.jobs import CAPABILITIES, Request
from test_v2_rocket_plan import RESEARCHED, space_age_catalog
from v2_blocks_fixture import real_catalog

LATE = ("advanced_petroleum", "fluid_product", "fluid_distribution")


class WiringTests(unittest.TestCase):
    def test_capabilities_handlers_dry_runs_and_rungs(self):
        for name in LATE:
            with self.subTest(name=name):
                self.assertIn(name, CAPABILITIES)
                self.assertIn(name, registered_names())
                self.assertIn(name, postmortem.DRY_RUN)
                self.assertTrue(any(r.kind == "site" for r in remedy.ladder(name)))
        Request("k", "w", 1, "advanced_petroleum", {}).validate()
        Request("k", "w", 1, "fluid_product", {"product": "lubricant"}).validate()
        Request("k", "w", 1, "fluid_distribution", {"source_key": "heavy-oil", "aliases": ["a-b", "c-d"]}).validate()
        Request("k", "w", 1, "oil_gathering", {"source_key": "advanced-crude"}).validate()
        Request("k", "w", 1, "water_gathering", {}).validate()
        for capability, bad in (
            ("fluid_product", {"product": "water"}),
            ("advanced_petroleum", {"x": 1}),
            ("oil_gathering", {"source_key": "water"}),
            ("fluid_distribution", {"source_key": "heavy-oil", "aliases": ["heavy-oil", "x"]}),
            ("fluid_distribution", {"source_key": "heavy-oil", "aliases": ["same", "same"]}),
        ):
            with self.subTest(capability=capability, bad=bad), self.assertRaises(ValueError):
                Request("k", "w", 1, capability, bad).validate()
        validate_source_key({})
        validate_fluid_distribution({"source_key": "advanced-petroleum-gas", "aliases": ["acid-gas", "gas-2"]})

    def test_executor_factories_build_one_instance_per_profile(self):
        from factorio_agent.executors import StarterExecutors

        with tempfile.TemporaryDirectory() as tmp:
            ex = StarterExecutors.__new__(StarterExecutors)
            ex.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp), query=Mock())
            ex.builder, ex.catalog, ex.perimeter = Mock(), Mock(fingerprint="f"), Mock()
            ex.bus_factory = ex.science = ex.green = None
            ex.oil_gathering, ex.water = object(), object()
            self.assertIs(ex._oil_source(None), ex.oil_gathering)
            self.assertIs(ex._water_source("water"), ex.water)
            crude = ex._oil_source("advanced-crude")
            self.assertEqual(crude.source_key, "advanced-crude")
            self.assertIs(ex._oil_source("advanced-crude"), crude)
            self.assertEqual(ex._water_source("acid-water").source_key, "acid-water")
            self.assertIs(ex._advanced_petroleum(), ex._advanced_petroleum())
            self.assertIs(ex._fluid_products(), ex._fluid_products())
            a = ex._fluid_distribution({"source_key": "heavy-oil", "aliases": ["lube-heavy", "heavy-2"]})
            self.assertIs(a, ex._fluid_distribution({"source_key": "heavy-oil", "aliases": ["lube-heavy", "heavy-2"]}))
            self.assertIsNot(a, ex._fluid_distribution({"source_key": "heavy-oil", "aliases": ["x-a", "x-b"]}))

    def test_handlers_route_to_the_namespaced_source(self):
        ex = SimpleNamespace(_oil_source=Mock(), _water_source=Mock())
        handler_for("oil_gathering").proof(ex, {}, {}, {"source_key": "advanced-crude"})
        ex._oil_source.assert_called_with("advanced-crude")
        handler_for("water_gathering").proof(ex, {}, {}, {})
        ex._water_source.assert_called_with(None)

    def test_planning_failures_read_as_needs(self):
        found = plan_needs.from_result(
            "fluid_product",
            {"reason": "no_fluid_product_site", "candidate_failures": [{"reason": "overlap"}]},
            {"product": "sulfuric-acid"},
        )
        self.assertIsNotNone(found)
        self.assertTrue(found["kind"])
        for capability in ("advanced_petroleum", "fluid_distribution"):
            needs = plan_needs.from_result(capability, {"reason": "no_candidate_site", "candidate_failures": []})
            self.assertIsNotNone(needs, capability)


class ResyncTests(unittest.TestCase):
    def test_step_rereads_the_checkpoint_the_build_handler_acknowledged(self):
        from factorio_agent.fluid_distribution import FluidDistribution

        with tempfile.TemporaryDirectory() as tmp:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp), query=Mock())
            catalog = Mock(fingerprint="fp")
            profile = {"source_key": "heavy-oil", "aliases": ["lube-heavy", "heavy-2"]}
            controller = FluidDistribution(
                game, Mock(), catalog, Mock(), source_key="heavy-oil", aliases=profile["aliases"]
            )
            state = {
                "profile": profile,
                "world_id": "w",
                "catalog_fingerprint": "fp",
                "pending_build": {"type": "build_fluid_distribution"},
                "bindings": {},
            }
            controller.state = dict(state)
            controller._save()
            obs = {"world_id": "w", "entities": []}
            blocked = controller.step(obs)
            self.assertEqual(blocked["reason"], "fluid_distribution_build_outcome_requires_reconciliation")
            # execute_build_fluid_distribution records the ACK through its own instance.
            acknowledged = dict(state, bindings={"pipe:1:1": 7}, build_phase="ready")
            acknowledged.pop("pending_build")
            controller.path.write_text(json.dumps(acknowledged), encoding="utf-8")
            after = controller.step(obs)
            self.assertNotEqual(after.get("reason"), "fluid_distribution_build_outcome_requires_reconciliation")
            self.assertEqual(controller.state["bindings"], {"pipe:1:1": 7})
            self.assertEqual(json.loads(Path(controller.path).read_text(encoding="utf-8"))["bindings"], {"pipe:1:1": 7})


class CrackingBalanceTests(unittest.TestCase):
    def bill(self, minutes=60):
        return rp.rocket_bill(space_age_catalog(), researched=RESEARCHED, minutes=minutes)

    def test_no_refinery_product_backs_up_at_the_bill_ratios(self):
        catalog = space_age_catalog()
        flow = rp.material_flow(catalog, {("item", "rocket-part"): 50.0, ("item", "rocket-silo"): 1.0})
        need = {k: flow["amounts"][k] - flow["surplus"].get(k, 0.0) for k in flow["amounts"]}
        before = flow["crafts"]["advanced-oil-processing"]
        rp.crack_balance(catalog, flow)
        refinery = catalog.recipes["advanced-oil-processing"]
        made = {}
        for product in refinery["products"]:
            made[_material(product)] = _yield(product) * flow["crafts"]["advanced-oil-processing"]
        for name, crafts in flow["cracking"].items():
            recipe = catalog.recipes[name]
            for ingredient in recipe["ingredients"]:
                if _material(ingredient) in made:
                    made[_material(ingredient)] -= _amount(ingredient["amount"]) * crafts
            for product in recipe["products"]:
                made[_material(product)] += _yield(product) * crafts
        for key, value in made.items():
            # made = need + reported surplus; heavy and light never back up (cracked down the chain)
            self.assertAlmostEqual(value, need.get(key, 0.0) + flow["surplus"].get(key, 0.0), delta=1e-3, msg=key)
        self.assertNotIn(("fluid", "heavy-oil"), flow["surplus"])
        self.assertNotIn(("fluid", "light-oil"), flow["surplus"])
        self.assertLess(flow["crafts"]["advanced-oil-processing"], before)  # cracking saves crude

    @unittest.skipIf(real_catalog() is None, "real catalog not available")
    def test_the_real_rocket_bill_has_no_fluid_surplus(self):
        catalog = real_catalog()
        if "rocket-silo" not in catalog.technologies:
            self.skipTest("catalog without a rocket silo")
        bill = rp.rocket_bill(catalog, researched=())
        self.assertEqual({k: v for k, v in bill["surplus"].items() if k.startswith("fluid:")}, {})
        self.assertTrue(all(row.get("fits_block", True) for row in bill["cracking"]))

    def test_cracking_rows_are_in_the_bill_and_the_tail_wires_the_fluid_blocks(self):
        bill = self.bill()
        self.assertEqual({row["recipe"] for row in bill["cracking"]}, {"heavy-oil-cracking", "light-oil-cracking"})
        rows = {
            r["id"]: r
            for r in rp.tail(
                bill,
                existing=[s.id for s in __import__("factorio_agent.progression").progression.ROADMAP],
                researched_by=rp.roadmap_research(),
            )
        }
        advanced = rows["rocket_advanced_petroleum"]
        self.assertIn("rocket_crude_advanced_crude", advanced["after"])
        self.assertIn("rocket_water_advanced_water", advanced["after"])
        self.assertEqual(rows["rocket_crude_advanced_crude"]["parameters"], {"source_key": "advanced-crude"})
        split = rows["rocket_split_acid_gas"]
        self.assertEqual(split["parameters"]["source_key"], "advanced-petroleum-gas")
        self.assertIn("rocket_advanced_petroleum", split["after"])
        acid = rows["rocket_fluid_product_sulfuric_acid"]
        self.assertTrue({"rocket_split_acid_gas", "rocket_water_acid_water"} <= set(acid["after"]))
        for row in rows.values():
            if row["capability"] in CAPABILITIES:
                Request("k", "w", 1, row["capability"], row["parameters"]).validate()

    def test_a_second_consumer_of_a_produced_fluid_takes_the_next_free_outlet(self):
        supply = rp.FluidSupply()
        first = supply.rows("heavy-oil", "lube-a", {"heavy-oil": "rocket_advanced_petroleum"}, set())[0]
        second = supply.rows("heavy-oil", "lube-b", {"heavy-oil": "rocket_advanced_petroleum"}, set())[0]
        self.assertEqual(first["parameters"], {"source_key": "heavy-oil", "aliases": ["lube-a", "heavy-oil-2"]})
        self.assertEqual(second["parameters"], {"source_key": "heavy-oil-2", "aliases": ["lube-b", "heavy-oil-3"]})
        self.assertIn(first["id"], second["after"])
        self.assertEqual(supply.rows("heavy-oil", "lube-a", {}, set()), [])


if __name__ == "__main__":
    unittest.main()
