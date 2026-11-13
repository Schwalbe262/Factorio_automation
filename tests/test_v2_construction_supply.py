from copy import deepcopy
from types import SimpleNamespace
import unittest
import unittest.mock
from unittest.mock import Mock, patch

from factorio_agent.construction_supply import (
    V2Bootstrap,
    execute_take_many,
    validate_take_many,
    TAKE_MANY_LUA,
)
from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap


def source(unit=1, name="transport-belt", kind="belt", x=1.5):
    return {"name": name, "kind": kind, "unit_number": unit, "position": {"x": x, "y": 0.5}}


def action():
    return {"type": "take_many", "item": "iron-plate", "count": 50, "world_id": "world", "sources": [source()]}


def entity(unit, stock, name="transport-belt", field="belt_inventory"):
    return {"unit_number": unit, "name": name, "position": {"x": unit + 0.5, "y": 0.5}, field: {"iron-plate": stock}}


def setUpModule():
    # These fixtures hold a handful of plates; the plate batch has its own test.
    import factorio_agent.construction_supply as supply

    supply._PLATE_BATCH_SAVED = supply.PLATE_BATCH
    supply.PLATE_BATCH = supply.COLLECT_BATCH


def tearDownModule():
    import factorio_agent.construction_supply as supply

    supply.PLATE_BATCH = supply._PLATE_BATCH_SAVED


class ConstructionSupplyTests(unittest.TestCase):
    def setUp(self):
        self.game = Mock(backend="assisted", expected_world="world")
        self.game.query.return_value = {
            "ok": True,
            "status": "succeeded",
            "moved": 8,
            "sources": [{"unit_number": 1, "moved": 8}],
        }
        self.bootstrap = V2Bootstrap(self.game)

    def test_commissioned_mall_stock_and_wait_precede_generic_collection_or_handcraft(self):
        obs = {"inventory": {"transport-belt": 150}, "entities": []}
        pickup = {"type": "take_mall_stock", "item": "transport-belt", "count": 50}
        provider = Mock(return_value=pickup)
        self.bootstrap.construction_stock_source = provider
        with patch.object(DeterministicBootstrap, "ensure_item") as fallback:
            self.assertEqual(self.bootstrap.ensure_item(obs, "transport-belt", 200), pickup)
            provider.assert_called_once_with(obs, "transport-belt", 200)
            provider.return_value = {"status": "waiting", "reason": "normally_producing_mall_stock"}
            self.assertEqual(self.bootstrap.ensure_item(obs, "transport-belt", 200)["status"], "waiting")
            fallback.assert_not_called()

    def test_missing_mall_allows_bootstrap_and_held_inventory_needs_no_provider(self):
        provider = Mock(return_value=None)
        self.bootstrap.construction_stock_source = provider
        with patch.object(DeterministicBootstrap, "ensure_item", return_value={"status": "succeeded"}) as fallback:
            self.bootstrap.ensure_item({"inventory": {"transport-belt": 200}}, "transport-belt", 200)
            provider.assert_not_called()
            self.bootstrap.ensure_item({"inventory": {}}, "transport-belt", 1)
            provider.assert_called_once()
            self.assertEqual(fallback.call_count, 2)

    def test_stock_provider_cannot_substitute_handcraft_raw_inputs_or_oversized_pickups(self):
        for invalid in (
            {"type": "craft", "recipe": "transport-belt", "count": 200},
            {"type": "supply_piercing_input"},
            {"type": "take_mall_stock", "item": "gun-turret", "count": 1},
            {"type": "take_mall_stock", "item": "transport-belt", "count": True},
            {"type": "take_mall_stock", "item": "transport-belt", "count": 51},
        ):
            with self.subTest(action=invalid), self.assertRaises(ValueError):
                self.bootstrap.construction_stock_source = Mock(return_value=invalid)
                self.bootstrap.ensure_item({"inventory": {}}, "transport-belt", 200)

    def test_global_request_does_not_shrink_to_single_stale_tile_stock(self):
        obs = {
            "world_id": "world",
            "entities": [entity(1, 1), entity(2, 8), entity(3, 12, "stone-furnace", "output_inventory")],
        }
        result = self.bootstrap._take_output(obs, "iron-plate", 120)
        self.assertEqual(result["count"], 50)
        self.assertEqual([s["unit_number"] for s in result["sources"]], [3, 2, 1])
        self.assertEqual([s["kind"] for s in result["sources"]], ["output", "belt", "belt"])
        validate_take_many(result)
        self.game.query.assert_not_called()

    def test_pipe_construction_pools_finished_machine_and_belt_stock_in_one_bounded_pickup(self):
        obs = {
            "world_id": "world",
            "entities": [
                dict(entity(1, 0, "assembling-machine-1", "output_inventory"), output_inventory={"pipe": 4}),
                dict(entity(2, 0), belt_inventory={"pipe": 8}),
                dict(entity(3, 0), belt_inventory={"pipe": 8}),
            ],
        }
        result = self.bootstrap._take_output(obs, "pipe", 50)
        self.assertEqual((result["type"], result["item"], result["count"]), ("take_many", "pipe", 50))
        self.assertEqual({q["unit_number"] for q in result["sources"]}, {1, 2, 3})
        validate_take_many(result)
        self.game.query.assert_not_called()

    def test_verified_buffer_precedes_fuller_candidates_but_does_not_exclude_them(self):
        chest = entity(1, 0, "wooden-chest", "inventory")
        self.bootstrap.construction_buffers["iron-plate"] = dict(source(1, "wooden-chest", "chest"), world_id="world")
        obs = {"world_id": "world", "entities": [entity(2, 12), chest]}
        result = self.bootstrap._take_output(obs, "iron-plate", 30)
        self.assertEqual([s["unit_number"] for s in result["sources"]], [1, 2])
        self.assertEqual(result["count"], 30)
        self.bootstrap.construction_buffers["iron-plate"]["world_id"] = "stale"
        self.assertEqual([s["unit_number"] for s in self.bootstrap._take_output(obs, "iron-plate", 30)["sources"]], [2])

    def test_machine_ingredient_inventory_and_labs_are_never_candidates(self):
        obs = {
            "world_id": "world",
            "entities": [
                entity(1, 200, "assembling-machine-1", "inventory"),
                entity(2, 200, "lab", "inventory"),
                entity(3, 10),
            ],
        }
        result = self.bootstrap._take_output(obs, "iron-plate", 50)
        self.assertEqual([s["unit_number"] for s in result["sources"]], [3])

    def test_single_items_are_left_to_pile_up_into_a_batch(self):
        obs = {"world_id": "world", "entities": [entity(3, 4), entity(4, 5)]}
        self.assertIsNone(self.bootstrap._take_output(obs, "iron-plate", 50))
        obs["entities"].append(entity(5, 1))
        self.assertEqual(len(self.bootstrap._take_output(obs, "iron-plate", 50)["sources"]), 3)
        small = {"world_id": "world", "entities": [entity(3, 4)]}
        self.assertEqual(self.bootstrap._take_output(small, "iron-plate", 3)["count"], 3)

    def test_plates_wait_for_a_larger_batch_than_other_items(self):
        # Seed 2099054105: 110 trips of 10 iron plates in 28 minutes.
        import factorio_agent.construction_supply as supply

        obs = {"world_id": "world", "entities": [entity(3, 12), entity(4, 12)]}
        with unittest.mock.patch.object(supply, "PLATE_BATCH", 30):
            self.assertIsNone(self.bootstrap._take_output(obs, "iron-plate", 50))
            obs["entities"].append(entity(5, 6))
            self.assertEqual(len(self.bootstrap._take_output(obs, "iron-plate", 50)["sources"]), 3)
            self.assertEqual(self.bootstrap._take_output(obs, "iron-plate", 5)["count"], 5)

    def test_at_most_sixteen_distinct_sources_ranked_by_stock(self):
        rows = [entity(i, i) for i in range(1, 22)]
        result = self.bootstrap._take_output({"world_id": "world", "entities": rows + rows}, "iron-plate", 50)
        self.assertEqual([s["unit_number"] for s in result["sources"]], list(range(21, 5, -1)))
        validate_take_many(result)

    def test_fuel_raw_items_and_science_keep_parent_behavior(self):
        for item in ("coal", "wood", "iron-ore", "automation-science-pack", "logistic-science-pack"):
            with (
                self.subTest(item=item),
                patch.object(DeterministicBootstrap, "_take_output", return_value={"parent": True}) as parent,
            ):
                self.assertEqual(self.bootstrap._take_output({"world_id": "world"}, item, 5), {"parent": True})
                parent.assert_called_once()

    def test_partial_ammo_is_never_recreated_from_chests_belts_or_machine_outputs(self):
        rows = [
            entity(1, 20),
            entity(2, 20, "wooden-chest", "inventory"),
            entity(3, 20, "assembling-machine-1", "output_inventory"),
        ]
        for row in rows:
            for field in ("inventory", "output_inventory", "belt_inventory"):
                if field in row:
                    row[field] = {"firearm-magazine": 20}
            row["ammo_remaining"] = 1
        obs = {"world_id": "world", "entities": rows}
        with patch.object(DeterministicBootstrap, "_take_output") as parent:
            self.assertIsNone(self.bootstrap._take_output(obs, "firearm-magazine", 50))
            parent.assert_not_called()
        request = action()
        request["item"] = "firearm-magazine"
        with self.assertRaises(ValueError):
            execute_take_many(self.game, request)
        self.game.query.assert_not_called()

    def test_catalog_ammo_types_are_protected_and_guns_cannot_use_batch_transfer(self):
        self.bootstrap.catalog = SimpleNamespace(items={"future-ammo": {"type": "ammo"}})
        self.assertIsNone(self.bootstrap._take_output({"world_id": "world"}, "future-ammo", 1))
        request = action()
        request["item"] = "submachine-gun"
        with self.assertRaises(ValueError):
            execute_take_many(self.game, request)
        self.game.query.assert_not_called()

    def test_malformed_envelopes_are_rejected_before_any_io(self):
        variants = []
        for field, values in {
            "count": [0, 51, True, 1.5],
            "world_id": [None, ""],
            "item": ["coal", "iron-ore", "logistic-science-pack", "sulfuric-acid"],
            "sources": [[], "bad", [source(i, x=i + 0.5) for i in range(1, 18)]],
        }.items():
            for value in values:
                request = action()
                request[field] = value
                variants.append(request)
        request = action()
        request["inventory"] = "input"
        variants.append(request)
        request = action()
        request["reason"] = 2
        variants.append(request)
        for request in variants:
            with self.subTest(request=request), self.assertRaises(ValueError):
                execute_take_many(self.game, request)
        self.game.query.assert_not_called()

    def test_finite_positions_exact_identity_and_source_kinds_are_mandatory(self):
        variants = []
        for field, values in {
            "unit_number": [True, 0, "1"],
            "kind": ["input", "fuel", "science"],
            "name": ["lab", "electric-mining-drill", "underground-belt"],
        }.items():
            for value in values:
                request = action()
                request["sources"][0][field] = value
                variants.append(request)
        for value in (float("inf"), float("nan"), True, "1"):
            request = action()
            request["sources"][0]["position"]["x"] = value
            variants.append(request)
        request = action()
        request["sources"][0]["position"]["z"] = 1
        variants.append(request)
        request = action()
        request["sources"][0]["count"] = 50
        variants.append(request)
        request = action()
        request["sources"].append(deepcopy(request["sources"][0]))
        variants.append(request)
        for request in variants:
            with self.subTest(request=request), self.assertRaises(ValueError):
                execute_take_many(self.game, request)
        self.game.query.assert_not_called()

    def test_python_world_mismatch_stops_before_native_io(self):
        request = action()
        request["world_id"] = "other"
        with self.assertRaises(ValueError):
            execute_take_many(self.game, request)
        self.game.query.assert_not_called()

    def test_one_native_sweep_receipt_uses_aggregate_not_per_source_limit(self):
        request = action()
        request["sources"] = [source(1), source(2, x=2.5), source(3, x=3.5)]
        receipt = {
            "ok": True,
            "status": "succeeded",
            "moved": 50,
            "requested": 50,
            "sources": [
                {"unit_number": 1, "moved": 0, "reason": "source_identity_changed"},
                {"unit_number": 2, "moved": 32},
                {"unit_number": 3, "moved": 18},
            ],
        }
        self.game.query.return_value = receipt
        self.assertEqual(execute_take_many(self.game, request), receipt)
        self.game.query.assert_called_once()

    def test_inconsistent_or_overdrawn_mocked_receipts_are_not_silently_accepted(self):
        for moved, rows in (
            (51, [{"unit_number": 1, "moved": 51}]),
            (8, [{"unit_number": 1, "moved": 7}]),
            (8, [{"unit_number": 2, "moved": 8}]),
            (8, [{"unit_number": 1, "moved": 4}, {"unit_number": 1, "moved": 4}]),
        ):
            with self.subTest(moved=moved, rows=rows):
                self.game.query.return_value = {"ok": True, "status": "succeeded", "moved": moved, "sources": rows}
                with self.assertRaisesRegex(RuntimeError, "reconcile"):
                    execute_take_many(self.game, action())

    def test_full_inventory_or_empty_stocks_returns_waiting_without_fake_movement(self):
        self.game.query.return_value = {"ok": True, "status": "waiting", "moved": 0, "sources": {}}
        self.assertEqual(execute_take_many(self.game, action())["sources"], [])

    def test_uncertain_mutation_is_never_retried(self):
        self.game.query.side_effect = TimeoutError("unknown native outcome")
        with self.assertRaises(TimeoutError):
            execute_take_many(self.game, action())
        self.game.query.assert_called_once()

    def test_native_query_line_flattening_cannot_turn_comments_into_missing_code(self):
        execute_take_many(self.game, action())
        body = self.game.query.call_args.args[0]
        flattened = " ".join(body.splitlines())
        self.assertNotIn("--", TAKE_MANY_LUA)
        self.assertNotIn("--", flattened)
        self.assertIn("return success{status=total>0", flattened)


if __name__ == "__main__":
    unittest.main()
