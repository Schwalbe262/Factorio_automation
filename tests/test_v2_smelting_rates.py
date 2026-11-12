from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.bus_factory import BusFactory
from factorio_agent.paired_smelting import PairedSmelting, paired_bank
from factorio_agent.smelting import SmeltingDistrict, bank_capacities, merge_entities, port


def catalog():
    return SimpleNamespace(
        fingerprint="catalog",
        entities={
            name: {"type": "furnace", "crafting_speed": speed}
            for name, speed in (("stone-furnace", 1), ("steel-furnace", 2), ("electric-furnace", 2))
        },
        recipes={
            name: {"energy": 3.2, "products": [{"name": name, "amount": 1}]} for name in ("iron-plate", "copper-plate")
        },
    )


def banks(iron_count=6):
    return [paired_bank(-40, -32, iron_count, "iron-ore"), paired_bank(-40, -16, 3, "copper-ore")]


class SmeltingRateTests(unittest.TestCase):
    def test_actual_furnaces_override_stale_single_side_and_count_metadata(self):
        plans = banks()
        for plan in plans:
            plan["count"] = 1
            plan["per_side_count"] = 1
        self.assertEqual(bank_capacities(catalog(), plans), {"iron-ore": 225, "copper-ore": 112.5})

    def test_unique_entity_prototype_speed_and_recipe_yield_determine_capacity(self):
        data = catalog()
        data.recipes["iron-plate"] = {"energy": 4, "products": [{"name": "iron-plate", "amount": 2}]}
        furnaces = [
            entity("stone-furnace", 0.5, 0.5),
            entity("steel-furnace", 4.5, 0.5),
            entity("electric-furnace", 8.5, 0.5),
        ]
        bank = {"resource": "iron-ore", "product": "iron-plate", "entities": furnaces + deepcopy(furnaces)}
        self.assertEqual(bank_capacities(data, [bank, deepcopy(bank)]), {"iron-ore": 150})

    def test_missing_furnaces_or_nondeterministic_yield_cannot_use_a_magic_fallback(self):
        with self.assertRaises(ValueError):
            bank_capacities(catalog(), [{"resource": "iron-ore", "product": "iron-plate", "entities": []}])
        data = catalog()
        data.recipes["iron-plate"]["products"][0]["probability"] = 0.5
        with self.assertRaises(ValueError):
            bank_capacities(data, banks())

    def test_paired_publication_replaces_both_old_single_side_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plans = banks(5)
            plan = _plan(merge_entities(*(b["entities"] for b in plans)), [b["ports"][-1] for b in plans])
            routine = PairedSmelting.__new__(PairedSmelting)
            routine.path = root / "paired-smelting.json"
            routine.catalog = catalog()
            routine.state = {"phase": "build", "plan": plan, "banks": plans}
            routine.smelting = SimpleNamespace(
                path=root / "smelting-district.json",
                state={"nominal_smelting_capacity_per_minute": {"iron-ore": 93.75, "copper-ore": 56.25}},
            )
            (root / "pcb-board.json").write_text(json.dumps({"blocks": {}}))
            routine.publish({"world_id": "w"})
            persisted = json.loads(routine.smelting.path.read_text())
            self.assertEqual(
                persisted["nominal_smelting_capacity_per_minute"], {"iron-ore": 187.5, "copper-ore": 112.5}
            )

    def test_iron_capacity_refreshes_copper_even_when_iron_requires_no_expansion(self):
        for initial_count in (5, 6):
            with self.subTest(initial_count=initial_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plans = banks(initial_count)
                routine = SmeltingDistrict.__new__(SmeltingDistrict)
                routine.path = root / "smelting-district.json"
                routine.catalog = catalog()
                routine.state = {
                    "world_id": "w",
                    "catalog_fingerprint": "catalog",
                    "paired": True,
                    "banks": plans,
                    "plan": _plan(merge_entities(*(b["entities"] for b in plans)), [b["ports"][-1] for b in plans]),
                    "nominal_smelting_capacity_per_minute": {"iron-ore": initial_count * 37.5, "copper-ore": 56.25},
                }
                geometry = FactoryBuilder.__new__(FactoryBuilder)
                routine.builder = SimpleNamespace(
                    _occupied_by_plan=geometry._occupied_by_plan,
                    can_place=lambda _: {"ok": True},
                    ensure_plan=lambda *_: {"status": "waiting"},
                )
                (root / "mining-outposts.json").write_text(
                    json.dumps(
                        {
                            "world_id": "w",
                            "banks": [
                                {"ready": True, "nominal_rate_per_minute": 300, "plan": {"resource": "iron-ore"}}
                            ],
                        }
                    )
                )
                (root / "pcb-board.json").write_text(
                    json.dumps({"world_id": "w", "catalog_fingerprint": "catalog", "blocks": {}})
                )
                result = routine.capacity_step({"world_id": "w"}, {"resource": "iron-ore", "rate_per_minute": 225})
                self.assertEqual(result["status"], "waiting")
                persisted = json.loads(routine.path.read_text())
                self.assertEqual(
                    persisted["nominal_smelting_capacity_per_minute"], {"iron-ore": 225, "copper-ore": 112.5}
                )

    def test_bus_preparation_derives_rates_instead_of_reading_stale_dictionary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = BusFactory.__new__(BusFactory)
            c.path = root / "bus-factory.json"
            c.catalog = catalog()
            c.builder = SimpleNamespace(_occupied_by_plan=lambda _: set())
            c.game = SimpleNamespace()
            inputs = [port("iron-plate", -9.5, -29.5, 4, "output"), port("copper-plate", -9.5, -17.5, 4, "output")]
            splitters = [entity("splitter", p["position"]["x"] - 1, p["position"]["y"] - 0.5, 4) for p in inputs]
            common = {"world_id": "w", "catalog_fingerprint": "catalog"}
            c.smelting = SimpleNamespace(
                state=dict(
                    common,
                    banks=banks(),
                    nominal_smelting_capacity_per_minute={"iron-ore": 1, "copper-ore": 56.25},
                    plan={"ports": inputs, "entities": splitters},
                )
            )
            red_inputs = [dict(p, direction="input") for p in inputs]
            c.science = SimpleNamespace(state=dict(common, science={"ports": red_inputs}, plan={}, labs={}))
            output = port("logistic-science-pack", -60.5, -40.5, 4, "output")
            c.green = SimpleNamespace(state=dict(common, core={"ports": red_inputs + [output]}, plan={}))
            (root / "pcb-board.json").write_text(json.dumps(dict(common, blocks={})))
            trace = {"ok": True, "entities": [entity("transport-belt", 0.5, 0.5, 4)]}
            with (
                patch("factorio_agent.bus_factory.trace_corridor", return_value=trace),
                patch("factorio_agent.bus_factory.lab_green_entry", return_value={"port": output}),
                patch(
                    "factorio_agent.bus_factory.plan_main_bus", return_value={"ok": False, "reason": "captured"}
                ) as compile_bus,
            ):
                result = c.prepare(
                    {
                        "world_id": "w",
                        "entities": [],
                        "enabled_recipes": {"logistic-science-pack": True, "fast-inserter": True},
                    }
                )
            self.assertEqual(result["reason"], "captured")
            materials = compile_bus.call_args.args[0]
            self.assertEqual({m.item: m.source_rates[0] for m in materials}, {"iron-plate": 225, "copper-plate": 112.5})


if __name__ == "__main__":
    unittest.main()
