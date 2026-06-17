import unittest

from factorio_ai import cell_compiler as cc
from factorio_ai import knowledge


class KnowledgeProfileTests(unittest.TestCase):
    def test_machine_profiles_load_with_fallbacks(self):
        am2 = knowledge.machine_profile("assembling-machine-2")
        self.assertIsNotNone(am2)
        self.assertEqual(am2.crafting_speed, 0.75)
        self.assertEqual(am2.module_slots, 2)
        self.assertEqual((am2.tile_width, am2.tile_height), (3, 3))

    def test_belt_and_module_and_footprint(self):
        self.assertEqual(knowledge.belt_profile("transport-belt").items_per_second, 15.0)
        self.assertEqual(knowledge.module_profile("speed-module-3").speed, 0.5)
        self.assertEqual(knowledge.entity_footprint("transport-belt"), (1.0, 1.0))

    def test_machines_for_category_orders_by_speed(self):
        crafting = knowledge.machines_for_category("crafting")
        self.assertEqual(crafting[0], "assembling-machine-1")  # slowest first
        self.assertIn("assembling-machine-3", crafting)


class CellCompilerTests(unittest.TestCase):
    def test_electronic_circuit_60_per_min_on_am2(self):
        spec = cc.compile_cell(
            "electronic-circuit", 60,
            available_machines=["assembling-machine-1", "assembling-machine-2"],
        )
        self.assertTrue(spec.ok)
        self.assertEqual(spec.machine, "assembling-machine-2")  # fastest available
        self.assertEqual(spec.machine_count, 1)  # 90/min per machine >= 60
        self.assertEqual(spec.per_machine_rate, 90.0)
        # Co-located copper-cable => external inputs are copper-PLATE + iron-plate (modular intent).
        inputs = {i.item: i for i in spec.inputs}
        self.assertEqual(inputs["iron-plate"].per_minute, 90.0)
        self.assertEqual(inputs["copper-plate"].per_minute, 135.0)
        self.assertNotIn("copper-cable", inputs)  # made on-site
        subs = {s.item: s for s in spec.substages}
        self.assertIn("copper-cable", subs)
        self.assertEqual(subs["copper-cable"].machine_count, 2)
        self.assertEqual(inputs["iron-plate"].belt_tier, "transport-belt")  # 1.5/s fits tier-1
        out = {o.item: o for o in spec.outputs}
        self.assertEqual(out["electronic-circuit"].per_minute, 90.0)
        self.assertAlmostEqual(spec.total_power_kw, 465.0)  # EC 155 + 2 cable 310

    def test_iron_gear_co_located_when_downstream(self):
        # A recipe needing iron-gear-wheel co-locates the gear stage; its input becomes iron-plate.
        spec = cc.compile_cell("automation-science-pack", 60, available_machines=["assembling-machine-2"])
        if spec.ok:
            items = {i.item for i in spec.inputs}
            self.assertNotIn("iron-gear-wheel", items)
            self.assertIn("iron-gear-wheel", {s.item for s in spec.substages})

    def test_machine_count_scales_with_rate(self):
        spec = cc.compile_cell("electronic-circuit", 600, available_machines=["assembling-machine-2"])
        self.assertEqual(spec.machine_count, 7)  # ceil(600/90)
        self.assertGreaterEqual(spec.achieved_rate, 600)

    def test_c2_power_vs_size_tradeoff(self):
        mods = ["speed-module-3", "efficiency-module-3"]
        size = cc.compile_cell("electronic-circuit", 600, available_machines=["assembling-machine-2"],
                               available_modules=mods, power_situation=cc.PowerSituation(size_vs_power_pref=0.0))
        powr = cc.compile_cell("electronic-circuit", 600, available_machines=["assembling-machine-2"],
                               available_modules=mods, power_situation=cc.PowerSituation(size_vs_power_pref=1.0))
        # Minimising footprint => fewer machines (speed modules); minimising power => more machines.
        self.assertLess(size.machine_count, powr.machine_count)
        self.assertLess(powr.total_power_kw, size.total_power_kw)
        self.assertIn("speed-module-3", size.modules)

    def test_power_headroom_rejects_costly_loadout(self):
        # copper-cable is single-stage (no co-located intermediate). 1800/min on AM2:
        # no-module = 10*155 = 1550 kW; speed-3 = 5*365 = 1825 kW. Headroom 1600 admits only
        # the no-module loadout even under size preference.
        spec = cc.compile_cell("copper-cable", 1800, available_machines=["assembling-machine-2"],
                               available_modules=["speed-module-3"],
                               power_situation=cc.PowerSituation(available_headroom_kw=1600, size_vs_power_pref=0.0))
        self.assertLessEqual(spec.total_power_kw, 1600.0)
        self.assertEqual(spec.modules, [])  # speed modules would blow the headroom

    def test_smelting_uses_furnace_and_archetype(self):
        # iron-plate's category resolves to 'smelting' from the dumped recipe data.
        spec = cc.compile_cell("iron-plate", 120)
        self.assertTrue(spec.ok)
        self.assertEqual(spec.category, "smelting")
        self.assertEqual(spec.archetype, "smelting_column")
        self.assertIn("furnace", spec.machine)

    def test_smelting_respects_available_machines(self):
        spec = cc.compile_cell("iron-plate", 120, available_machines=["stone-furnace", "steel-furnace"])
        self.assertIn(spec.machine, {"stone-furnace", "steel-furnace"})

    def test_raw_resource_returns_not_ok(self):
        spec = cc.compile_cell("iron-ore", 60)
        self.assertFalse(spec.ok)
        self.assertEqual(spec.machine_count, 0)

    def test_fluid_input_has_no_belt(self):
        # processing-unit consumes sulfuric-acid (a fluid) -> no belt tier, pipe instead.
        spec = cc.compile_cell("processing-unit", 30, available_machines=["assembling-machine-2", "assembling-machine-3"])
        if spec.ok:
            fluids = [i for i in spec.inputs if i.is_fluid]
            for f in fluids:
                self.assertIsNone(f.belt_tier)


if __name__ == "__main__":
    unittest.main()
