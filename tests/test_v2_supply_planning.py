from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.supply_planning import supply_options, RESOURCES


def catalog():
    def r(name, inputs):
        return dict(
            name=name,
            ingredients=[dict(name=n, amount=q, type="item") for n, q in inputs],
            products=[dict(name=name, amount=1, type="item")],
        )

    rows = {
        "burner-mining-drill": r(
            "burner-mining-drill", [("iron-plate", 3), ("iron-gear-wheel", 3), ("stone-furnace", 1)]
        ),
        "iron-gear-wheel": r("iron-gear-wheel", [("iron-plate", 2)]),
        "stone-furnace": r("stone-furnace", [("stone", 5)]),
        "wooden-chest": r("wooden-chest", [("wood", 2)]),
    }
    for name, ore in (("iron-plate", "iron-ore"), ("copper-plate", "copper-ore")):
        rows[name] = r(name, [(ore, 1)])
        rows[name]["energy"] = 3.2
    entities = {"burner-mining-drill": {"mining_speed": 0.25}, "stone-furnace": {"crafting_speed": 1}}
    entities.update({n: dict(mining_time=1, mining_products=[dict(name=n, amount=1)]) for n in RESOURCES.values()})
    return SimpleNamespace(fingerprint="c", recipe_for_product=rows.get, recipes=rows, entities=entities)


class SupplyPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = catalog()
        self.game = SimpleNamespace(expected_world="w", cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=1000,
            inventory={"iron-plate": 100, "copper-plate": 100, "coal": 100, "stone": 100, "wood": 100},
            entities=[],
            enabled_recipes={n: True for n in self.c.recipes},
        )
        self.rates = dict(
            status="measured",
            window_game_seconds=60,
            rates_per_game_minute={
                n: dict(produced=30, consumed=0)
                for n in ("iron-plate", "copper-plate", "coal", "stone", "wood", "iron-ore", "copper-ore")
            },
        )
        self.surveys = {}
        for i, (item, ore) in enumerate(RESOURCES.items()):
            drill = dict(name="burner-mining-drill", position={"x": i * 10, "y": 0}, unit_number=i + 1)
            receiver = dict(
                name="stone-furnace" if item.endswith("-plate") else "wooden-chest",
                position={"x": i * 10, "y": -2},
                unit_number=i + 10,
            )
            self.surveys[ore] = dict(
                ok=True,
                cells=[dict(drill=drill, receiver=receiver, remaining=1000, burning=True, fuel=2, electric=False)],
            )
        self.calls = []

    def run_options(self, budget=None):
        def survey(obj, ore):
            self.calls.append(ore)
            return deepcopy(self.surveys[ore])

        with patch("factorio_agent.supply_planning.V2Bootstrap._existing_cells", survey):
            return supply_options(self.root, self.game, self.obs, self.c, budget or {"base": {}}, self.rates)

    def test_four_read_surveys_and_stock_sufficiency_precede_expansion(self):
        r = self.run_options()
        self.assertEqual(set(r), set(RESOURCES))
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(q["action"] == "advance" for q in r.values()))
        self.game.query.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_full_output_is_collectable_stock_while_fuel_and_reserved_inputs_are_not(self):
        self.obs["inventory"]["iron-plate"] = 0
        rec = self.surveys["iron-ore"]["cells"][0]["receiver"]
        self.obs["entities"] = [
            dict(
                rec,
                output_inventory={"iron-plate": 60},
                inventory={"coal": 99, "iron-ore": 10},
                status_name="full_output",
            )
        ]
        self.rates["rates_per_game_minute"]["iron-plate"]["produced"] = 0
        r = self.run_options()
        self.assertEqual(r["iron-plate"]["action"], "collect_stock")
        self.assertEqual(r["iron-plate"]["evidence"]["collectable"], 60)
        self.assertEqual(r["coal"]["evidence"]["collectable"], 0)
        board = dict(world_id="w", catalog_fingerprint="c", blocks={"startup_science": dict(entities=[rec], ports=[])})
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.assertEqual(self.run_options()["iron-plate"]["evidence"]["collectable"], 0)

    def test_known_funded_cost_yields_absolute_target_no_more_than_two_new_cells(self):
        r = self.run_options({"base": {"stone": 1000}})["stone"]
        self.assertEqual(r["action"], "expand_supply", r)
        self.assertEqual(r["capability"], "starter_capacity")
        self.assertEqual(r["parameters"]["resource"], "stone")
        self.assertIn(r["parameters"]["cells"], (2, 3))
        self.assertEqual(r["evidence"]["raw_cost_per_cell"], {"coal": 8, "iron-plate": 9, "stone": 5, "wood": 2})
        self.assertEqual(r["evidence"]["target_item_cost_per_cell"], 5)
        self.assertEqual(r["evidence"]["cell_capacity_per_minute"], 15)

    def test_sampling_unknown_recipe_and_unfunded_cost_never_expand(self):
        self.rates["status"] = "sampling"
        self.assertEqual(self.run_options({"base": {"stone": 1000}})["stone"]["action"], "wait")
        self.rates["status"] = "measured"
        self.rates["rates_per_game_minute"]["wood"]["produced"] = 0
        r = self.run_options({"base": {"stone": 1000}})["stone"]
        self.assertEqual(r["action"], "expand_supply")  # Real prefunded wood needs no fictional production rate.
        self.obs["enabled_recipes"]["wooden-chest"] = False
        r = self.run_options({"base": {"stone": 1000}})["stone"]
        self.assertEqual(r["action"], "wait")
        self.assertIsNone(r["evidence"]["target_item_cost_per_cell"])
        self.assertEqual(r["evidence"]["alternatives"], [])
        self.obs["enabled_recipes"]["wooden-chest"] = True
        self.obs["inventory"]["wood"] = 0
        self.assertEqual(
            self.run_options({"base": {"stone": 1000}})["stone"]["reason"], "expansion_raw_materials_not_funded"
        )

    def test_coal_negative_net_with_eight_real_seed_coal_can_fund_one_not_two_cells(self):
        self.obs["inventory"]["coal"] = 8
        self.rates["rates_per_game_minute"]["coal"]["consumed"] = 32
        r = self.run_options()["coal"]
        self.assertEqual(r["action"], "expand_supply", r)
        self.assertEqual(r["parameters"], {"resource": "coal", "cells": 2})
        self.assertEqual(r["evidence"]["extra_cells"], 1)
        self.assertIsNone(r["evidence"]["wait_seconds"])

    def test_unrelated_full_copper_furnace_does_not_hide_a_real_coal_deficit(self):
        self.obs["inventory"]["coal"] = 8
        self.rates["rates_per_game_minute"]["coal"]["consumed"] = 32
        receiver = self.surveys["copper-ore"]["cells"][0]["receiver"]
        self.obs["entities"] = [dict(receiver, output_inventory={"copper-plate": 100}, status_name="full_output")]
        result = self.run_options()["coal"]
        self.assertEqual(result["action"], "expand_supply", result)
        self.assertEqual(result["parameters"], {"resource": "coal", "cells": 2})

    def test_pending_same_resource_and_unknown_world_effect_suppress_duplicates(self):
        (self.root / "work_queue.json").write_text(
            json.dumps(
                dict(
                    world_id="w",
                    nodes=[
                        dict(
                            capability="starter_capacity", parameters={"resource": "stone", "cells": 2}, status="ready"
                        )
                    ],
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_options({"base": {"stone": 1000}})["stone"]["reason"], "existing_capacity_task")
        (self.root / "work_queue.json").unlink()
        (self.root / "jobs.json").write_text(
            json.dumps(
                dict(
                    world_id="w",
                    jobs={"x": dict(world_id="w", capability="research", status="blocked", pending_action={})},
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_options({"base": {"stone": 1000}})["stone"]["reason"], "unresolved_world_effect")

    def test_electric_unlock_prefers_outpost_and_ore_shortage_never_requests_idle_furnaces(self):
        self.obs["enabled_recipes"]["electric-mining-drill"] = True
        self.rates["rates_per_game_minute"]["iron-ore"]["produced"] = 300
        r = self.run_options({"base": {"iron-plate": 1000}})["iron-plate"]
        self.assertEqual(r["action"], "wait")
        self.assertEqual(r.get("preferred_capability"), "mining_outpost")
        self.assertIsNone(r["parameters"])
        self.rates["rates_per_game_minute"]["iron-ore"]["produced"] = 0
        r = self.run_options({"base": {"iron-plate": 1000}})["iron-plate"]
        self.assertEqual((r["action"], r["evidence"]["bottleneck"]), ("wait", "ore"))


if __name__ == "__main__":
    unittest.main()
