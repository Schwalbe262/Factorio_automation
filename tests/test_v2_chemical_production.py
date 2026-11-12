from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.chemical_production import (
    ChemicalProduction,
    CHEMICAL_PROOF_LUA,
    CHEMICAL_PIPE_SURVEY_LUA,
    CHEMICAL_CROSSING_PROOF_LUA,
    _key,
    _payload,
    chemical_core,
    chemical_rates,
    chemical_fluid_ports,
    blue_chemistry_demand,
    source_owned_apron,
    plan_typed_fluid_connection,
    chemical_plan_isolation,
    typed_fluid_edges,
    fluid_crossing_proof,
    _handoff,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    boxes = []
    for index, (x, y) in enumerate(((-1, -1), (1, -1), (-1, 1), (1, 1)), 1):
        positions = []
        for rotation in range(4):
            positions.append({"x": x, "y": y})
            x, y = -y, x
        boxes.append(
            {
                "index": index,
                "production_type": "input" if index <= 2 else "output",
                "pipe_connections": [
                    {"connection_type": "normal", "direction": 0 if index <= 2 else 8, "positions": positions}
                ],
            }
        )

    def item(name, amount):
        return {"type": "item", "name": name, "amount": amount}

    def fluid(name, amount):
        return {"type": "fluid", "name": name, "amount": amount}

    recipes = {
        "plastic-bar": {
            "category": "chemistry",
            "energy": 1,
            "ingredients": [item("coal", 1), fluid("petroleum-gas", 20)],
            "products": [item("plastic-bar", 2)],
        },
        "sulfur": {
            "category": "chemistry",
            "energy": 1,
            "ingredients": [fluid("water", 30), fluid("petroleum-gas", 30)],
            "products": [item("sulfur", 2)],
        },
        "chemical-science-pack": {
            "energy": 24,
            "ingredients": [item("sulfur", 1), item("advanced-circuit", 3), item("engine-unit", 2)],
            "products": [item("chemical-science-pack", 2)],
        },
        "advanced-circuit": {
            "energy": 6,
            "ingredients": [item("electronic-circuit", 2), item("plastic-bar", 2), item("copper-cable", 4)],
            "products": [item("advanced-circuit", 1)],
        },
        "engine-unit": {
            "energy": 10,
            "ingredients": [item("steel-plate", 1), item("iron-gear-wheel", 1), item("pipe", 2)],
            "products": [item("engine-unit", 1)],
        },
        "electronic-circuit": {
            "energy": 0.5,
            "ingredients": [item("iron-plate", 1), item("copper-cable", 3)],
            "products": [item("electronic-circuit", 1)],
        },
        "copper-cable": {
            "energy": 0.5,
            "ingredients": [item("copper-plate", 1)],
            "products": [item("copper-cable", 2)],
        },
        "iron-gear-wheel": {
            "energy": 0.5,
            "ingredients": [item("iron-plate", 2)],
            "products": [item("iron-gear-wheel", 1)],
        },
        "pipe": {"energy": 0.5, "ingredients": [item("iron-plate", 1)], "products": [item("pipe", 1)]},
        "basic-oil-processing": {
            "category": "oil-processing",
            "energy": 5,
            "ingredients": [fluid("crude-oil", 100)],
            "products": [fluid("petroleum-gas", 45)],
        },
    }
    return SimpleNamespace(
        fingerprint="c",
        recipes=recipes,
        recipe_for_product=lambda name: recipes.get(name),
        entities={
            "chemical-plant": {"crafting_speed": 1, "crafting_categories": ["chemistry"], "fluidbox_prototypes": boxes},
            "oil-refinery": {"crafting_speed": 1, "energy_usage": 7000, "crafting_categories": ["oil-processing"]},
        },
    )


class ChemicalGeometryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()
        self.builder = FactoryBuilder.__new__(FactoryBuilder)

    def test_recipe_relative_fluid_ordinals_match_native_physical_inputs(self):
        self.assertEqual(
            chemical_fluid_ports(self.catalog, "plastic-bar"),
            {"petroleum-gas": {"fluidbox_index": 1, "offset": {"x": -1, "y": -2}}},
        )
        self.assertEqual(
            chemical_fluid_ports(self.catalog, "sulfur")["petroleum-gas"],
            {"fluidbox_index": 2, "offset": {"x": 1, "y": -2}},
        )
        self.catalog.entities["chemical-plant"]["fluidbox_prototypes"][0]["pipe_connections"][0]["positions"][0][
            "x"
        ] = 0
        with self.assertRaises(ValueError):
            chemical_core(self.catalog, "plastic-bar", 0.5, 0.5)

    def test_catalog_nameplate_and_blue_recipe_demand_are_separate(self):
        self.assertEqual(chemical_rates(self.catalog, "plastic-bar")["items_per_minute"], 120)
        demand = blue_chemistry_demand(self.catalog)
        self.assertEqual(demand["products_per_minute"], {"plastic-bar": 60, "sulfur": 10})
        self.assertEqual(demand["inputs_per_minute"], {"coal": 30, "petroleum-gas": 750, "water": 150})
        self.assertEqual(demand["basic_refineries_required_at_nameplate"], 2)
        self.catalog.recipes["plastic-bar"]["products"][0]["probability"] = 0.5
        with self.assertRaises(ValueError):
            chemical_rates(self.catalog, "plastic-bar")
        with self.assertRaises(ValueError):
            blue_chemistry_demand(self.catalog, float("nan"))

    def test_complete_core_footprints_and_fixed_output_preserve_empty_expansion_slot(self):
        for product in ("plastic-bar", "sulfur"):
            core = chemical_core(self.catalog, product, 160.5, -57.5)
            boxes = [
                self.builder._occupied_by_plan([e])
                for e in core["entities"] + core["expansion_keepouts"] + core["output_access"]
            ]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertEqual(core["required_items"]["chemical-plant"], 1)
            self.assertEqual(core["output_port"]["position"], {"x": 169.5, "y": -54.5})
            self.assertTrue(all(e["name"] == "fast-inserter" for e in core["entities"] if "inserter" in e["name"]))
        with self.assertRaises(ValueError):
            chemical_core(self.catalog, "plastic-bar", 0, 0.5)

    def test_sulfur_prospective_water_and_gas_do_not_touch(self):
        core = chemical_core(self.catalog, "sulfur", 0.5, 0.5)
        plans = {
            name: {
                "nodes": nodes,
                "source": {"unit_number": index},
                "route": {"maximum_underground_span": 10, "underground_pairs": []},
            }
            for index, (name, nodes) in enumerate(core["fluid_groups"].items(), 1)
        }
        game = SimpleNamespace(query=Mock(return_value={"ok": True}))
        self.assertTrue(chemical_plan_isolation(game, plans, core)["ok"])
        plans["petroleum-gas"]["nodes"].append(entity("pipe", -5.5, -2.5))
        game.query.reset_mock()
        self.assertEqual(
            chemical_plan_isolation(game, plans, core)["reason"], "chemical_different_fluid_networks_touch"
        )
        game.query.assert_not_called()

    def test_prospective_buried_pair_cannot_steal_another_fluid_mouth(self):
        core = chemical_core(self.catalog, "sulfur", 30.5, 30.5)
        game = SimpleNamespace(query=Mock())

        def group(name, start, end):
            return {
                "nodes": [entity("pipe-to-ground", start, 0.5, 12), entity("pipe-to-ground", end, 0.5, 4)],
                "source": {"unit_number": start},
                "route": {
                    "maximum_underground_span": 10,
                    "underground_pairs": [{"first": [start, 0.5], "last": [end, 0.5]}],
                },
            }

        plans = {"water": group("water", 0.5, 8.5), "petroleum-gas": group("petroleum-gas", 2.5, 6.5)}
        self.assertEqual(
            chemical_plan_isolation(game, plans, core)["reason"], "chemical_prospective_buried_pair_changed"
        )
        game.query.assert_not_called()


class ChemicalApronTests(unittest.TestCase):
    def setUp(self):
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.source = dict(
            kind="fluid",
            item="petroleum-gas",
            direction="output",
            facing=4,
            name="pipe",
            fluidbox_index=1,
            position={"x": 150.5, "y": -61.5},
            unit_number=1,
            world_id="w",
            defense_scope_id="petroleum",
        )
        self.block = {
            "reserved_area": [[128, -75], [153, -53]],
            "ports": [
                {k: v for k, v in self.source.items() if k not in ("world_id", "unit_number", "defense_scope_id")}
            ],
            "entities": [entity("pipe", 150.5, -61.5)],
            "defense_scope_id": "petroleum",
        }

    def test_only_exact_producer_approved_straight_empty_apron_is_opened(self):
        result = source_owned_apron(self.builder, self.source, [self.block])
        self.assertTrue(result["ok"])
        self.assertEqual(result["owner_index"], 1)
        self.assertEqual(result["path"], [{"x": x, "y": -61.5} for x in (150.5, 151.5, 152.5, 153.5)])
        self.assertEqual(self.block["reserved_area"], [[128, -75], [153, -53]])

    def test_wrong_owner_wrong_typed_port_foreign_block_and_producer_obstacle_reject(self):
        wrong = dict(self.source, defense_scope_id="foreign")
        self.assertFalse(source_owned_apron(self.builder, wrong, [self.block])["ok"])
        wrong = dict(self.source, item="water")
        self.assertFalse(source_owned_apron(self.builder, wrong, [self.block])["ok"])
        foreign = {"reserved_area": [[151, -62], [152, -61]]}
        self.assertEqual(
            source_owned_apron(self.builder, self.source, [self.block, foreign])["reason"],
            "chemical_source_apron_hits_foreign_reservation",
        )
        self.block["entities"].append(entity("pipe", 151.5, -61.5))
        self.assertEqual(
            source_owned_apron(self.builder, self.source, [self.block])["reason"],
            "chemical_source_apron_prospective_producer_obstruction",
        )

    def test_bound_and_actual_ore_or_occupied_tile_are_not_exempted(self):
        self.block["reserved_area"][1][0] = 160
        self.assertFalse(source_owned_apron(self.builder, self.source, [self.block])["ok"])
        with self.assertRaises(ValueError):
            source_owned_apron(self.builder, self.source, [self.block], 5)
        self.block["reserved_area"][1][0] = 153
        with tempfile.TemporaryDirectory() as root:
            Path(root, "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {"producer": self.block}}))
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "spans": [10]})
            )
            core = chemical_core(catalog(), "plastic-bar", 160.5, -57.5)
            for reason in ("new_nonmining_entity_on_resource", "block_protected_entity_obstruction"):
                with patch(
                    "factorio_agent.chemical_production.preflight", return_value={"ok": False, "reason": reason}
                ) as check:
                    r = plan_typed_fluid_connection(
                        game, self.builder, {"world_id": "w"}, self.source, core["fluid_ports"]["petroleum-gas"], core
                    )
                self.assertEqual(r["reason"], "chemical_source_apron_native_obstruction")
                self.assertEqual(check.call_args.args[3], [entity("pipe", x, -61.5) for x in (151.5, 152.5, 153.5)])

    def test_real_router_keeps_approved_apron_straight_and_above_ground(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {"producer": self.block}}))
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root),
                query=Mock(side_effect=[{"ok": True, "spans": [10]}, {"ok": True, "hard": [], "soft": []}]),
            )
            core = chemical_core(catalog(), "plastic-bar", 160.5, -57.5)
            with patch("factorio_agent.chemical_production.preflight", return_value={"ok": True}):
                result = plan_typed_fluid_connection(
                    game, self.builder, {"world_id": "w"}, self.source, core["fluid_ports"]["petroleum-gas"], core
                )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["full_path"], [{"x": x, "y": -61.5} for x in (150.5, 151.5, 152.5, 153.5, 154.5)])
            self.assertEqual(result["first_neighbor"], dict(entity("pipe", 151.5, -61.5), fluidbox_index=1))
            self.assertEqual(result["underground_pairs"], [])

    def test_fluid_destination_cannot_erase_unbuilt_foreign_external_transport(self):
        with tempfile.TemporaryDirectory() as root:
            foreign = {
                "reserved_area": [[180, -80], [190, -70]],
                "ports": [],
                "entities": [entity("transport-belt", 154.5, -60.5, 4)],
            }
            Path(root, "pcb-board.json").write_text(
                json.dumps({"world_id": "w", "blocks": {"producer": self.block, "foreign": foreign}})
            )
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root),
                query=Mock(side_effect=[{"ok": True, "spans": [10]}, {"ok": True, "hard": [], "soft": []}]),
            )
            core = chemical_core(catalog(), "plastic-bar", 160.5, -57.5)
            with patch("factorio_agent.chemical_production.preflight", return_value={"ok": True}):
                result = plan_typed_fluid_connection(
                    game, self.builder, {"world_id": "w"}, self.source, core["fluid_ports"]["petroleum-gas"], core
                )
            self.assertEqual(result["reason"], "chemical_fluid_endpoint_conflicts_with_unbuilt_external_plan")

    def water_crossing(
        self,
        *,
        survey=None,
        observed=None,
        preflight_result=None,
        core_position=(108.5, -21.5),
        crossing_x=93.5,
        owner_right=93,
    ):
        source = dict(
            self.source, item="water", position={"x": 90.5, "y": -25.5}, unit_number=6989, defense_scope_id="water"
        )
        block = {
            "reserved_area": [[86.0117, -26.0977], [owner_right, -24.9023]],
            "ports": [source],
            "entities": [entity("pipe", 90.5, -25.5)],
            "defense_scope_id": "water",
        }
        core = chemical_core(catalog(), "sulfur", *core_position)
        witness = dict(
            entity("transport-belt", crossing_x, -25.5),
            type="transport-belt",
            unit_number=6439,
            world_id="w",
            force_index=1,
        )
        if survey is None:
            survey = {
                "ok": True,
                "hard": [],
                "soft": [witness["position"]],
                "crossings": [{"position": witness["position"], "assets": [witness]}],
            }
        if observed is None:
            observed = [witness]
        if preflight_result is None:
            preflight_result = {
                "ok": False,
                "reason": "block_terrain_or_immovable_obstruction",
                "entity": "pipe",
                "position": witness["position"],
            }
        with tempfile.TemporaryDirectory() as root:
            Path(root, "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {"water": block}}))
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root), query=Mock(side_effect=[{"ok": True, "spans": [10]}, survey])
            )
            with patch("factorio_agent.chemical_production.preflight", return_value=preflight_result):
                result = plan_typed_fluid_connection(
                    game,
                    self.builder,
                    {"world_id": "w", "entities": observed},
                    source,
                    core["fluid_ports"]["water"],
                    core,
                )
        return result, witness, block, game

    def test_real_router_buries_exact_observed_belt_beyond_unchanged_water_first_neighbor(self):
        result, witness, block, _ = self.water_crossing()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_apron_exit"]["path"], [{"x": x, "y": -25.5} for x in (90.5, 91.5, 92.5, 93.5)])
        self.assertEqual(result["first_neighbor"], dict(entity("pipe", 91.5, -25.5), fluidbox_index=1))
        self.assertEqual(
            result["underground_pairs"], [{"first": [92.5, -25.5], "last": [94.5, -25.5], "direction": 4, "span": 2}]
        )
        self.assertIn(entity("pipe-to-ground", 92.5, -25.5, 12), result["entities"])
        self.assertIn(entity("pipe-to-ground", 94.5, -25.5, 4), result["entities"])
        self.assertFalse(any(e["position"] == witness["position"] for e in result["entities"]))
        self.assertEqual(result["preserved_fluid_crossings"], [witness])
        self.assertEqual(block["reserved_area"], [[86.0117, -26.0977], [93, -24.9023]])

    def test_apron_crossing_rejects_missing_observation_changed_native_unit_resource_and_unknown(self):
        for change in ("unobserved", "replaced", "resource", "unknown", "foreign"):
            with self.subTest(change=change):
                witness = dict(
                    entity("transport-belt", 93.5, -25.5),
                    type="transport-belt",
                    unit_number=6439,
                    world_id="w",
                    force_index=1,
                )
                observed = [witness]
                fresh = deepcopy(witness)
                hard = []
                if change == "unobserved":
                    observed = []
                elif change == "replaced":
                    fresh["unit_number"] = 9999
                elif change in ("resource", "foreign"):
                    hard = [witness["position"]]
                elif change == "unknown":
                    observed = [dict(witness, name="assembling-machine-1", type="assembling-machine")]
                survey = {
                    "ok": True,
                    "hard": hard,
                    "soft": [witness["position"]],
                    "crossings": [{"position": witness["position"], "assets": [fresh]}],
                }
                result, _, _, game = self.water_crossing(survey=survey, observed=observed)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason"], "chemical_source_apron_native_obstruction")
                if change in ("unobserved", "unknown"):
                    self.assertEqual(game.query.call_count, 1)

    def test_first_neighbor_belt_resource_preflight_and_unwitnessed_soft_cells_remain_blocked(self):
        first = dict(entity("transport-belt", 91.5, -25.5), type="transport-belt", unit_number=1)
        failure = {
            "ok": False,
            "reason": "block_terrain_or_immovable_obstruction",
            "entity": "pipe",
            "position": first["position"],
        }
        result, _, _, _ = self.water_crossing(observed=[first], preflight_result=failure)
        self.assertEqual(result["reason"], "chemical_source_apron_native_obstruction")
        failure.update(reason="new_nonmining_entity_on_resource", position={"x": 93.5, "y": -25.5})
        result, _, _, _ = self.water_crossing(preflight_result=failure)
        self.assertEqual(result["reason"], "chemical_source_apron_native_obstruction")
        result, _, _, _ = self.water_crossing(survey={"ok": True, "hard": [], "soft": [{"x": 93.5, "y": -25.5}]})
        self.assertEqual(result["reason"], "chemical_source_apron_native_obstruction")

    def test_normal_apron_retains_every_later_buried_asset_identity(self):
        witnesses = [
            dict(entity(name, x, -25.5), type=type_, unit_number=unit, world_id="w", force_index=1)
            for name, type_, x, unit in (
                ("transport-belt", "transport-belt", 97.5, 1),
                ("small-electric-pole", "electric-pole", 98.5, 2),
            )
        ]
        survey = {
            "ok": True,
            "hard": [],
            "soft": [q["position"] for q in witnesses],
            "crossings": [{"position": q["position"], "assets": [q]} for q in witnesses],
        }
        result, _, _, _ = self.water_crossing(survey=survey, observed=witnesses, preflight_result={"ok": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["preserved_fluid_crossings"], witnesses)
        self.assertEqual(
            result["underground_pairs"], [{"first": [96.5, -25.5], "last": [99.5, -25.5], "direction": 4, "span": 3}]
        )
        self.assertTrue(all(entity("pipe", x, -25.5) in result["entities"] for x in (91.5, 92.5, 93.5)))

    def test_guarded_crossing_minimum_straight_run_follows_exact_native_apron_index(self):
        for crossing_x, owner_right, minimum in ((93.5, 93, 5), (94.5, 94, 6)):
            with patch(
                "factorio_agent.chemical_production.route_trunks", return_value={"ok": False, "reason": "fixture"}
            ) as route:
                self.water_crossing(crossing_x=crossing_x, owner_right=owner_right)
            self.assertEqual(route.call_args.kwargs["minimum_start_run"], minimum)
        with patch(
            "factorio_agent.chemical_production.route_trunks", return_value={"ok": False, "reason": "fixture"}
        ) as route:
            self.water_crossing(preflight_result={"ok": True}, survey={"ok": True, "hard": [], "soft": []})
        self.assertIsNone(route.call_args.kwargs["minimum_start_run"])

    def test_long_route_surveys_every_quality_legal_excursion_with_same_router_budget(self):
        with patch(
            "factorio_agent.chemical_production.route_trunks", return_value={"ok": False, "reason": "fixture"}
        ) as route:
            _, _, _, game = self.water_crossing(core_position=(165.5, -102.5))
        args = route.call_args.kwargs
        expected = {"min_x": 62.5, "max_x": 187.5, "min_y": -134.5, "max_y": 2.5}
        self.assertEqual(args["bounds"], expected)
        self.assertEqual((args["max_nodes"], args["max_turns"], args["max_detour_ratio"]), (200000, 6, 1.35))
        self.assertEqual(args["minimum_start_run"], 5)
        script = game.query.call_args.args[0]
        raw = script.split("local args=helpers.json_to_table(", 1)[1].split(")\n", 1)[0]
        self.assertEqual(json.loads(json.loads(raw))["bounds"], expected)
        self.assertEqual(
            (expected["max_x"] - expected["min_x"] + 1) * (expected["max_y"] - expected["min_y"] + 1), 17388
        )

    def test_survey_cell_budget_rejects_before_any_native_query_or_router(self):
        with patch("factorio_agent.chemical_production.route_trunks") as route:
            result, _, _, game = self.water_crossing(core_position=(500.5, -500.5))
        self.assertEqual(result["reason"], "chemical_pipe_survey_budget_exceeded")
        game.query.assert_not_called()
        route.assert_not_called()


class ChemicalControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = catalog()
        self.game = SimpleNamespace(
            expected_world="w", cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={"ok": True})
        )
        self.builder = SimpleNamespace(
            _occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={"type": "craft", "recipe": "chemical-plant", "count": 1}),
        )
        self.perimeter = SimpleNamespace(
            prepare_coverage=Mock(return_value={"ok": True}),
            ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={"complete": True}),
        )
        self.c = ChemicalProduction(self.game, self.builder, self.catalog, self.perimeter)
        self.obs = {
            "ok": True,
            "world_id": "w",
            "tick": 10,
            "entities": [],
            "technologies": {"plastics": True, "sulfur-processing": True},
            "enabled_recipes": {name: True for name in ("plastic-bar", "sulfur", "chemical-plant", "fast-inserter")},
        }
        self.params = {"product": "plastic-bar"}
        self.gas = dict(
            kind="fluid",
            item="petroleum-gas",
            name="pipe",
            position={"x": 0.5, "y": -30.5},
            direction="output",
            facing=4,
            fluidbox_index=1,
            world_id="w",
            unit_number=1,
            defense_scope_id="gas",
        )
        self.coal = dict(
            kind="item",
            item="coal",
            name="transport-belt",
            position={"x": -10.5, "y": -10.5},
            direction="output",
            facing=4,
            world_id="w",
            unit_number=2,
        )
        self.registry = {"world_id": "w", "catalog_fingerprint": "c", "sources": {"petroleum-gas": self.gas}}
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        (self.root / "material-sources.json").write_text(
            json.dumps({"world_id": "w", "catalog_fingerprint": "c", "sources": {"plastic-coal": self.coal}})
        )
        (self.root / "pcb-board.json").write_text(
            json.dumps({"world_id": "w", "catalog_fingerprint": "c", "blocks": {}})
        )
        self.ready = {
            "ok": True,
            "sources": {"petroleum-gas": self.gas, "coal": self.coal},
            "snapshots": {
                "petroleum-gas": {
                    "amount": 10,
                    "extent": {"left_top": {"x": 0, "y": -31}, "right_bottom": {"x": 1, "y": -30}},
                    "limit": 320,
                }
            },
        }
        self.sources_patch = patch.object(self.c, "_sources", return_value=deepcopy(self.ready))
        self.sources = self.sources_patch.start()
        self.survey = patch(
            "factorio_agent.chemical_production.survey_slots", return_value={"ok": True, "legal": [1]}
        ).start()
        self.check = patch(
            "factorio_agent.chemical_production.preflight", return_value={"ok": True, "clear_obstacles": []}
        ).start()
        patch(
            "factorio_agent.chemical_production.connect_power_groups", return_value={"ok": True, "entities": []}
        ).start()
        patch("factorio_agent.chemical_production.chemical_plan_isolation", return_value={"ok": True}).start()
        patch(
            "factorio_agent.chemical_production.belt_connection",
            return_value={"ok": True, "entities": [], "underground_pairs": []},
        ).start()

        def connection(game, builder, obs, source, destination, core, prior):
            points = [entity("pipe", x, source["position"]["y"]) for x in (1.5, 2.5, 3.5, 4.5)]
            return {
                "ok": True,
                "entities": points,
                "underground_pairs": [],
                "maximum_underground_span": 10,
                "first_neighbor": dict(points[0], fluidbox_index=1),
            }

        self.connection = patch(
            "factorio_agent.chemical_production.plan_typed_fluid_connection", side_effect=connection
        ).start()
        self.handoff_patch = patch("factorio_agent.chemical_production._handoff", return_value={"ok": True})
        self.handoff = self.handoff_patch.start()
        self.addCleanup(patch.stopall)

    def install(self):
        entry = self.c.prepare(self.obs, self.params)
        self.assertTrue(entry["ok"], entry)
        self.c.state = {"world_id": "w", "catalog_fingerprint": "c", "products": {"plastic-bar": entry}}
        return entry

    def test_unbuilt_products_choose_their_explicit_gas_aliases(self):
        self.sources_patch.stop()
        plastic = dict(self.gas, unit_number=10, position={"x": 10.5, "y": -30.5})
        sulfur = dict(self.gas, unit_number=20, position={"x": 20.5, "y": -30.5})
        water = dict(self.gas, item="water", unit_number=30, position={"x": 30.5, "y": -30.5})
        self.registry["sources"].update({"plastic-gas": plastic, "sulfur-gas": sulfur, "water": water})
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.assertEqual(self.c._sources(self.obs, "plastic-bar")["sources"]["petroleum-gas"], plastic)
        self.assertEqual(self.c._sources(self.obs, "sulfur")["sources"]["petroleum-gas"], sulfur)

    def test_new_alias_does_not_retarget_a_previously_bound_product(self):
        self.sources_patch.stop()
        alias = dict(self.gas, unit_number=10, position={"x": 10.5, "y": -30.5})
        self.registry["sources"]["plastic-gas"] = alias
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.c.state = {"products": {"plastic-bar": {"sources": {"petroleum-gas": self.gas, "coal": self.coal}}}}
        self.assertEqual(self.c._sources(self.obs, "plastic-bar")["sources"]["petroleum-gas"], self.gas)
        self.c.overrides["petroleum-gas"] = alias
        self.assertEqual(self.c._sources(self.obs, "plastic-bar")["reason"], "chemical_bound_source_override_changed")

    def test_core_candidates_preserve_other_factories_unbuilt_external_feeds(self):
        es = [
            entity("transport-belt", 10.5 + dx, -26.5 + dy, 4)
            for dx, dy in (
                (0, 0),
                (0, 14),
                (22, 0),
                (22, 14),
                (0, -14),
                (22, -14),
                (0, -28),
                (22, -28),
                (0, -42),
                (22, -42),
            )
        ]
        (self.root / "pcb-board.json").write_text(
            json.dumps(
                {
                    "world_id": "w",
                    "catalog_fingerprint": "c",
                    "blocks": {"future_owner": {"reserved_area": [[500, 500], [510, 510]], "entities": es}},
                }
            )
        )
        result = self.c.prepare(self.obs, self.params)
        self.assertFalse(result["ok"])
        self.survey.assert_not_called()
        self.builder.ensure_plan.assert_not_called()
        self.assertFalse(self.c.path.exists())

    def test_prepare_readonly_paid_core_complete_sources_and_natural_technology(self):
        with patch("factorio_agent.chemical_production._atomic_json") as write:
            entry = self.c.prepare(self.obs, self.params)
            write.assert_not_called()
        self.assertTrue(entry["ok"], entry)
        self.assertEqual(self.c.state, {})
        self.assertFalse(self.c.path.exists())
        self.builder.ensure_plan.assert_not_called()
        self.handoff.assert_not_called()
        self.assertEqual(entry["plan"]["entities"][0], entity("pipe", 1.5, -30.5))
        self.assertEqual(
            self.c.prepare({**self.obs, "technologies": {}}, self.params)["reason"],
            "natural_chemical_technology_and_recipes_required",
        )

    def test_original_candidate_priority_precedes_two_finite_additional_north_rows(self):
        original = self.connection.side_effect
        seen = []

        def probe(game, builder, obs, source, destination, core, prior):
            seen.append(core["machine"]["position"])
            if len(seen) < 9:
                return {"ok": False, "reason": "fixture_route_blocked"}
            return original(game, builder, obs, source, destination, core, prior)

        self.connection.side_effect = probe
        result = self.c.prepare(self.obs, self.params)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            seen,
            [
                {"x": x, "y": y}
                for x, y in (
                    (10.5, -26.5),
                    (10.5, -12.5),
                    (32.5, -26.5),
                    (32.5, -12.5),
                    (10.5, -40.5),
                    (32.5, -40.5),
                    (10.5, -54.5),
                    (32.5, -54.5),
                    (10.5, -68.5),
                )
            ],
        )
        self.assertEqual(result["planning_evidence"]["attempted_candidates"], 9)

    def test_previously_accepted_plastic_keeps_exact_plan_without_new_geometry_search(self):
        entry = self.install()
        self.connection.reset_mock()
        self.survey.reset_mock()
        result = self.c.prepare(self.obs, self.params)
        self.assertEqual(result, entry)
        self.assertIsNot(result, entry)
        self.connection.assert_not_called()
        self.survey.assert_not_called()

    def test_malformed_request_rejects_before_io(self):
        for parameters in (None, {}, {"product": "coal"}, {"product": []}, {"product": "plastic-bar", "extra": 1}):
            with self.assertRaises(ValueError):
                self.c.prepare(self.obs, parameters)
        self.sources.assert_not_called()
        self.game.query.assert_not_called()

    def test_missing_chemical_feed_and_finite_route_failure_never_latch_state(self):
        self.sources.return_value = {"ok": False, "reason": "explicit_usable_chemical_source_missing", "item": "coal"}
        self.assertFalse(self.c.prepare(self.obs, self.params)["ok"])
        self.assertEqual(self.c.state, {})
        self.sources.return_value = self.ready
        self.connection.side_effect = None
        self.connection.return_value = {"ok": False, "reason": "no_clean_trunk_corridor"}
        self.assertFalse(self.c.prepare(self.obs, self.params)["ok"])
        self.assertEqual(self.connection.call_count, 10)

    def test_failed_coal_stage_exposes_exact_candidate_sources_and_visited_without_full_path(self):
        failure = {
            "ok": False,
            "reason": "no_clean_trunk_corridor",
            "visited": 400220,
            "path": [{"x": 0, "y": 0}] * 1000,
        }
        with patch("factorio_agent.chemical_production.belt_connection", return_value=failure):
            result = self.c.prepare(self.obs, self.params)
            blocked = self.c.step(self.obs, self.params)
        self.assertEqual(result["evidence"]["attempted_candidates"], 10)
        self.assertEqual(result["evidence"]["legal_sites"], 10)
        row = result["evidence"]["candidate_failures"][0]
        self.assertEqual((row["stage"], row["item"], row["visited"]), ("coal_route", "coal", 400220))
        self.assertEqual(row["source"]["position"], self.coal["position"])
        self.assertEqual(row["source"]["unit_number"], 2)
        self.assertEqual(row["destination"]["position"], {"x": row["candidate"]["x"] - 6, "y": row["candidate"]["y"]})
        self.assertNotIn("path", row)
        self.assertEqual(blocked["evidence"]["candidate_failures"], result["evidence"]["candidate_failures"])
        self.assertEqual(self.c.state, {})
        self.assertFalse(self.c.path.exists())
        self.builder.ensure_plan.assert_not_called()

    def test_filtered_land_candidates_and_expired_budget_have_durable_explicit_stage(self):
        self.survey.return_value = {"ok": True, "legal": []}
        result = self.c.prepare(self.obs, self.params)
        self.assertEqual(result["evidence"]["legal_sites"], 0)
        self.assertTrue(all(row["stage"] == "core_land" for row in result["evidence"]["candidate_failures"]))
        self.connection.assert_not_called()
        self.survey.return_value = {"ok": True, "legal": [1]}
        self.connection.side_effect = None
        self.connection.return_value = {"ok": False, "reason": "no_clean_trunk_corridor", "visited": 200000}
        with patch("factorio_agent.chemical_production.time.monotonic", side_effect=[0, 0, 21, 21]):
            result = self.c.prepare(self.obs, self.params)
        self.assertTrue(result["evidence"]["budget_exhausted"])
        self.assertEqual(result["evidence"]["planning_budget_seconds"], 20)
        self.assertEqual(result["evidence"]["attempted_candidates"], 1)
        self.assertEqual(result["evidence"]["candidate_failures"][0]["stage"], "fluid_route")
        self.assertFalse(self.c.path.exists())

    def test_early_source_failure_details_survive_the_public_blocked_receipt(self):
        self.sources.return_value = {
            "ok": False,
            "reason": "chemical_fluid_source_requires_independent_authorized_branch",
            "item": "petroleum-gas",
            "existing_consumer": "other",
        }
        result = self.c.step(self.obs, self.params)
        self.assertEqual(result["evidence"]["item"], "petroleum-gas")
        self.assertEqual(result["evidence"]["existing_consumer"], "other")
        self.assertFalse(self.c.path.exists())
        self.builder.ensure_plan.assert_not_called()

    def test_crossed_existing_pipe_identity_is_guarded_before_build_and_native_acceptance(self):
        entry = self.install()
        failure = {"ok": False, "reason": "preserved_surface_pipe_peer_identity_changed", "unit_number": 123}
        with patch("factorio_agent.chemical_production.surface_pipe_proof", return_value=failure):
            result = self.c.step(self.obs, self.params)
            self.assertEqual(result["reason"], "chemical_preserved_surface_pipe_changed")
            self.assertEqual(result["evidence"], failure)
            self.builder.ensure_plan.assert_not_called()
            self.handoff.assert_not_called()
            self.obs["entities"] = [dict(e, unit_number=i + 10) for i, e in enumerate(entry["plan"]["entities"])]
            self.c._bind(self.obs, entry)
            self.assertIsNone(self.c.proof(self.obs, self.params))
            self.assertEqual(self.c.last_proof_failure, failure)

    def test_fluid_crossing_guard_blocks_handoff_build_and_commissioning(self):
        entry = self.install()
        failure = {"ok": False, "reason": "chemical_fluid_crossing_unrecorded_asset"}
        with patch("factorio_agent.chemical_production.fluid_crossing_proof", return_value=failure):
            result = self.c.step(self.obs, self.params)
            self.assertEqual(result["reason"], "chemical_preserved_fluid_crossing_changed")
            self.assertEqual(result["evidence"], failure)
            self.builder.ensure_plan.assert_not_called()
            self.handoff.assert_not_called()
            self.obs["entities"] = [dict(e, unit_number=i + 10) for i, e in enumerate(entry["plan"]["entities"])]
            self.c._bind(self.obs, entry)
            self.assertIsNone(self.c.proof(self.obs, self.params))
            self.assertEqual(self.c.last_proof_failure, failure)

    def test_prepared_fluid_pairs_and_foreign_witnesses_are_separate_from_belt_pairs_and_owned_assets(self):
        crossing = dict(
            entity("transport-belt", 3.5, -30.5), type="transport-belt", unit_number=99, world_id="w", force_index=1
        )
        pair = {"first": [2.5, -30.5], "last": [4.5, -30.5], "direction": 4, "span": 2}
        points = [
            entity("pipe", 1.5, -30.5),
            entity("pipe-to-ground", 2.5, -30.5, 12),
            entity("pipe-to-ground", 4.5, -30.5, 4),
        ]
        self.connection.side_effect = None
        self.connection.return_value = {
            "ok": True,
            "entities": points,
            "underground_pairs": [pair],
            "maximum_underground_span": 10,
            "first_neighbor": dict(points[0], fluidbox_index=1),
            "preserved_fluid_crossings": [crossing],
        }
        entry = self.install()
        self.assertEqual(entry["plan"]["underground_pairs"], [])
        self.assertEqual(entry["plan"]["fluid_underground_pairs"], [pair])
        self.assertEqual(entry["plan"]["preserved_fluid_crossings"], [crossing])
        self.assertFalse(
            any(
                e["name"] == "transport-belt" and e["position"] == crossing["position"]
                for e in entry["plan"]["entities"]
            )
        )

    def test_legacy_plan_without_fluid_crossing_witnesses_makes_no_new_claim_or_query(self):
        game = SimpleNamespace(query=Mock())
        self.assertEqual(
            fluid_crossing_proof(game, self.obs, {}), {"ok": True, "preserved_assets": 0, "fluid_pairs": 0}
        )
        game.query.assert_not_called()

    def test_defense_gate_then_declaration_before_any_builder_action(self):
        self.install()
        self.perimeter.ensure_coverage.return_value = {"status": "waiting", "reason": "arm post"}
        self.assertEqual(self.c.step(self.obs, self.params)["reason"], "arm post")
        self.handoff.assert_not_called()
        self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.return_value = None
        self.handoff.return_value = {"ok": False, "reason": "source occupied"}
        self.assertEqual(self.c.step(self.obs, self.params)["reason"], "chemical_fluid_handoff_not_authorized")
        self.builder.ensure_plan.assert_not_called()
        self.handoff.return_value = {"ok": True}
        self.c.step(self.obs, self.params)
        self.builder.ensure_plan.assert_called_once()
        self.assertNotIn("unit_number", self.handoff.call_args.args[6])

    def test_first_neighbor_bind_uses_saved_layout_unit_and_replaced_asset_blocks(self):
        entry = self.install()
        neighbor = entry["fluid_handoffs"]["petroleum-gas"]["first_neighbor"]
        self.obs["entities"] = [dict({k: v for k, v in neighbor.items() if k != "fluidbox_index"}, unit_number=90)]
        self.c.step(self.obs, self.params)
        self.assertTrue(self.handoff.call_args.kwargs["bind"])
        self.assertEqual(self.handoff.call_args.args[6]["unit_number"], 90)
        self.builder.ensure_plan.reset_mock()
        self.obs["entities"][0]["unit_number"] = 91
        self.assertEqual(self.c.step(self.obs, self.params)["reason"], "chemical_owned_asset_missing_or_replaced")
        self.builder.ensure_plan.assert_not_called()

    def test_resume_never_retargets_changed_source(self):
        self.install()
        self.sources.return_value = deepcopy(self.ready)
        self.sources.return_value["sources"]["coal"]["unit_number"] = 99
        self.assertEqual(
            self.c.step(self.obs, self.params)["reason"], "prepared_chemical_source_changed_or_unavailable"
        )
        self.builder.ensure_plan.assert_not_called()

    def test_real_source_selection_never_borrows_consumed_coal_or_second_gas_edge(self):
        self.sources_patch.stop()
        self.assertTrue(self.c._sources(self.obs, "plastic-bar")["ok"])
        material = json.loads((self.root / "material-sources.json").read_text())
        material["sources"] = {"coal": self.coal}
        (self.root / "material-sources.json").write_text(json.dumps(material))
        self.assertEqual(self.c._sources(self.obs, "plastic-bar")["registry_key"], "plastic-coal")
        self.registry["handoffs"] = {"chemical:plastic-bar": {"petroleum-gas": {"source": self.gas}}}
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.assertEqual(
            self.c._sources(self.obs, "sulfur")["reason"],
            "chemical_fluid_source_requires_independent_authorized_branch",
        )
        self.registry.pop("handoffs")
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.assertEqual(self.c._sources(self.obs, "sulfur")["item"], "water")
        self.c.overrides["coal"] = dict(self.coal, unit_number=999)
        self.assertEqual(self.c._sources(self.obs, "plastic-bar")["reason"], "chemical_source_not_exactly_published")

    def test_handoff_context_exception_is_a_blocker(self):
        self.handoff_patch.stop()
        with patch("factorio_agent.fluid_handoffs.declare_handoff", side_effect=ValueError("changed world")):
            self.assertEqual(
                _handoff(self.game, self.catalog, self.obs, "chemical:plastic-bar", "petroleum-gas", self.gas, {})[
                    "reason"
                ],
                "chemical_fluid_handoff_context_invalid",
            )

    def test_controller_proof_requires_bound_saved_neighbor_and_every_owned_asset(self):
        entry = self.install()
        self.obs["entities"] = [dict(e, unit_number=i + 10) for i, e in enumerate(entry["plan"]["entities"])]
        self.c._bind(self.obs, entry)
        h = entry["fluid_handoffs"]["petroleum-gas"]
        neighbor = dict(h["first_neighbor"], unit_number=entry["bindings"][_key(h["first_neighbor"])])
        self.registry["handoffs"] = {
            "chemical:plastic-bar": {
                "petroleum-gas": dict(
                    world_id="w", catalog_fingerprint="c", source=self.gas, first_neighbor=neighbor, phase="bound"
                )
            }
        }
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.game.query.return_value = {
            "ok": True,
            "tick": 30,
            "output_unit": entry["bindings"][_key(dict(entry["core"]["output_port"], name="transport-belt"))],
        }
        self.assertIsNotNone(self.c.proof(self.obs, self.params))
        self.registry["handoffs"]["chemical:plastic-bar"]["petroleum-gas"]["first_neighbor"]["unit_number"] = 999
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.assertIsNone(self.c.proof(self.obs, self.params))
        self.registry["handoffs"]["chemical:plastic-bar"]["petroleum-gas"]["first_neighbor"] = neighbor
        (self.root / "fluid-sources.json").write_text(json.dumps(self.registry))
        self.obs["entities"][0]["unit_number"] = 999
        self.assertIsNone(self.c.proof(self.obs, self.params))


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeChemicalCrossingTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(a) for a in v])
            return v

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute("""f={index=1,name="player"};s={};a={};d={world_id="w"};resource=false;terrain=false;extra=nil
prototypes={entity={pipe={collision_mask={layers={object=true}}},["pipe-to-ground"]={fluidbox_prototypes={{
 pipe_connections={{connection_type="underground",max_underground_distance=10}}}}}}}
e={name="transport-belt",type="transport-belt",valid=true,unit_number=6439,force=f,surface=s,
 position={x=93.5,y=-25.5},direction=0,minable=true,prototype={collision_mask={colliding_with_tiles_only=false},fluidbox_prototypes={}}}
function pos(p)return{x=p.x,y=p.y}end
function target(p,name)if e and e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end
local function inside(q,area)return q.position.x+.49>area[1][1]and q.position.x-.49<area[2][1]
 and q.position.y+.49>area[1][2]and q.position.y-.49<area[2][2]end
s.count_entities_filtered=function(q)return resource and 1 or 0 end
s.find_tiles_filtered=function(q)return{{collides_with=function(layer)return terrain end}}end
s.find_entities_filtered=function(q)local rows={};if e and inside(e,q.area)then rows[#rows+1]=e end
 if extra and inside(extra,q.area)then rows[#rows+1]=extra end;return rows end""")
        self.witness = dict(
            entity("transport-belt", 93.5, -25.5), type="transport-belt", unit_number=6439, world_id="w", force_index=1
        )
        self.obs = {"ok": True, "world_id": "w", "entities": [self.witness]}
        self.plan = {
            "fluid_underground_pairs": [{"first": [92.5, -25.5], "last": [94.5, -25.5], "direction": 4, "span": 2}],
            "preserved_fluid_crossings": [self.witness],
        }
        self.game = SimpleNamespace(query=Mock(side_effect=self.execute))

    def execute(self, script):
        return dict(self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()").items())

    def survey(self):
        args = {
            "world_id": "w",
            "bounds": {"min_x": 93.5, "max_x": 93.5, "min_y": -25.5, "max_y": -25.5},
            "source": {"unit_number": 6989, "position": {"x": 90.5, "y": -25.5}},
            "apron_path": [],
            "owner_index": None,
            "zones": [],
            "reservations": [],
        }
        return self.execute(CHEMICAL_PIPE_SURVEY_LUA.replace("PAYLOAD", _payload(args)))

    def test_flattened_native_survey_captures_owned_exact_asset_and_rejects_unknown_foreign_resource_terrain(self):
        result = self.survey()
        self.assertEqual(len(result["soft"]), 1)
        self.assertEqual(len(result["hard"]), 0)
        witness = result["crossings"][1]["assets"][1]
        self.assertEqual(witness["unit_number"], 6439)
        self.assertEqual(witness["force_index"], 1)
        for change, restore in (
            ('e.type="splitter"', 'e.type="transport-belt"'),
            (
                'e.type="rail";e.prototype.collision_mask.colliding_with_tiles_only=true',
                'e.type="transport-belt";e.prototype.collision_mask.colliding_with_tiles_only=false',
            ),
            ('e.force={index=2,name="enemy"}', "e.force=f"),
            ("resource=true", "resource=false"),
            ("terrain=true", "terrain=false"),
        ):
            self.lua.execute(change)
            result = self.survey()
            self.assertEqual(len(result["hard"]), 1, change)
            self.assertEqual(len(result["soft"]), 0, change)
            self.lua.execute(restore)

    def test_flattened_native_guard_checks_identity_force_direction_world_resource_and_extra_assets(self):
        self.assertTrue(fluid_crossing_proof(self.game, self.obs, self.plan)["ok"])
        changes = [
            ("e.unit_number=9999", "e.unit_number=6439"),
            ("e.direction=4", "e.direction=0"),
            ('e.force={index=2,name="enemy"}', "e.force=f"),
            ("e.position.x=94.5", "e.position.x=93.5"),
            ('d.world_id="other"', 'd.world_id="w"'),
            ("resource=true", "resource=false"),
            ("terrain=true", "terrain=false"),
            (
                'extra={name="assembler",type="assembling-machine",valid=true,unit_number=9999,force=f,surface=s,position=e.position,prototype=e.prototype}',
                "extra=nil",
            ),
        ]
        for change, restore in changes:
            self.lua.execute(change)
            self.assertFalse(fluid_crossing_proof(self.game, self.obs, self.plan)["ok"], change)
            self.lua.execute(restore)

    def test_guard_rejects_changed_observation_or_malformed_pairs_before_native_query(self):
        for change in ("unit", "direction", "world", "pair"):
            obs = deepcopy(self.obs)
            plan = deepcopy(self.plan)
            self.game.query.reset_mock()
            if change == "unit":
                obs["entities"][0]["unit_number"] = 9999
            elif change == "direction":
                obs["entities"][0]["direction"] = 4
            elif change == "world":
                plan["preserved_fluid_crossings"][0]["world_id"] = "other"
            else:
                plan["fluid_underground_pairs"][0]["last"][1] += 1
            self.assertFalse(fluid_crossing_proof(self.game, obs, plan)["ok"], change)
            self.game.query.assert_not_called()

    def test_guard_retains_mining_reservations_and_native_span_limit(self):
        with patch(
            "factorio_agent.chemical_production.mining_reservations", return_value=[{"area": [[93, -26], [94, -25]]}]
        ):
            self.assertEqual(
                fluid_crossing_proof(self.game, self.obs, self.plan)["reason"],
                "chemical_fluid_crossing_mining_reservation_changed",
            )
        self.lua.execute(
            'prototypes.entity["pipe-to-ground"].fluidbox_prototypes[1].pipe_connections[1].max_underground_distance=1'
        )
        self.assertEqual(
            fluid_crossing_proof(self.game, self.obs, self.plan)["reason"],
            "chemical_fluid_crossing_native_span_changed",
        )


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeChemicalProofTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.core = chemical_core(catalog(), "plastic-bar", 10.5, 10.5)
        self.nodes = self.core["fluid_groups"]["petroleum-gas"]
        self.machine = self.core["machine"]
        self.edges = typed_fluid_edges(
            self.nodes, [], self.machine, 1, next(e for e in self.nodes if e["position"] == {"x": 9.5, "y": 8.5})
        )
        specs = self.core["entities"]
        bindings = {_key(e): i + 10 for i, e in enumerate(specs)}
        self.args = {
            "product": "plastic-bar",
            "groups": {"petroleum-gas": [dict(e, unit_number=bindings[_key(e)]) for e in self.nodes]},
            "prototype_box_count": 4,
            "edges": self.edges,
            "machine": dict(self.machine, unit_number=bindings[_key(self.machine)]),
            "source_units": [],
            "physical_boxes": {"1": "petroleum-gas"},
            "arms": [dict(e, unit_number=bindings[_key(e)]) for e in (self.core["input_arm"], self.core["output_arm"])],
            "output": dict(
                self.core["output_port"],
                name="transport-belt",
                unit_number=bindings[_key(dict(self.core["output_port"], name="transport-belt"))],
            ),
        }

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(a) for a in v])
            return v

        self.table = table
        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.globals().specs = table([dict(e, unit_number=bindings[_key(e)]) for e in specs])
        self.lua.globals().edges = table(self.edges)
        self.lua.execute("""f={};s={};game={tick=30};prototypes={utility_constants={default_pipeline_extent=320}};powered=true;contaminated=false;wrong_recipe=false;wrong_box=false;one_way=false;wrong_index=false;foreign=false;oversize=false;empty=false;dry=false;shift=false;missing_prototype=false;merged_prototype=false
local function k(q)return q.name..":"..q.position.x..":"..q.position.y end
local function runtime(q,index)if q.name=="chemical-plant"and index==1 then return shift and 2 or 1 end;return index end
entities={};for _,q in ipairs(specs)do entities[k(q)]=q end
for _,q in ipairs(specs)do
 q.force=f;q.surface=s;q.energy=100;q.products_finished=1
 q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"sulfur"or"plastic-bar"}end
 q.get_fluid_box_prototype=function(index)
  if missing_prototype then return nil end
  if merged_prototype and index==1 then return{{index=1},{index=2}}end
  if index==runtime(q,1)then return{index=1}end
  if shift and index==1 then return{index=3}end
  error("runtime storage out of bounds")end
 q.get_fluid=function(index)
  if wrong_box then return{name="water",amount=1}end
  if index==runtime(q,1)then return{name="petroleum-gas",amount=10}end
  if not(shift and index==1)then error("runtime storage out of bounds")end end
 q.get_fluid_contents=function()if dry then return{}end;return{[contaminated and"water"or"petroleum-gas"]=10}end
 q.has_fluid_segment=function()return true end
 q.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=10,y=oversize and 321 or 10}}end
 q.get_fluid_box_neighbours=function(index)local rows={}
  for _,edge in ipairs(edges)do
   if k(edge.a)==k(q)and runtime(edge.a,edge.a.fluidbox_index)==index then rows[#rows+1]={entity=entities[k(edge.b)],index=wrong_index and 3 or runtime(edge.b,edge.b.fluidbox_index)}end
   if not one_way and k(edge.b)==k(q)and runtime(edge.b,edge.b.fluidbox_index)==index then rows[#rows+1]={entity=entities[k(edge.a)],index=runtime(edge.a,edge.a.fluidbox_index)}end
  end
  if foreign then rows[#rows+1]={entity={unit_number=999},index=1}end;return rows end
 q.get_transport_line=function(index)return{get_contents=function()if empty then return{}end;return{{name=q.position.y==10.5 and"coal"or"plastic-bar",count=2}}end}end
 if q.name=="fast-inserter"then
  if q.direction==12 then q.pickup_position={x=q.position.x-1,y=q.position.y};q.drop_position={x=q.position.x+1,y=q.position.y}
  else q.pickup_position={x=q.position.x,y=q.position.y-1};q.drop_position={x=q.position.x,y=q.position.y+1}end
 end
end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end""")

    def run_proof(self):
        result = self.lua.execute(
            "return(function() "
            + " ".join(CHEMICAL_PROOF_LUA.replace("PAYLOAD", _payload(self.args)).splitlines())
            + " end)()"
        )
        return dict(result.items())

    def test_flattened_native_proof_requires_actual_product_fluid_recipe_and_reciprocal_edges(self):
        self.assertTrue(self.run_proof()["ok"])
        for change, restore in (
            ("powered=false", "powered=true"),
            ("contaminated=true", "contaminated=false"),
            ("wrong_recipe=true", "wrong_recipe=false"),
            ("wrong_box=true", "wrong_box=false"),
            ("one_way=true", "one_way=false"),
            ("wrong_index=true", "wrong_index=false"),
            ("foreign=true", "foreign=false"),
            ("oversize=true", "oversize=false"),
            ("empty=true", "empty=false"),
            ("dry=true", "dry=false"),
        ):
            self.lua.execute(change)
            self.assertFalse(self.run_proof()["ok"], change)
            self.lua.execute(restore)
        self.lua.globals().entities[_key(self.machine)].products_finished = 0
        self.assertEqual(self.run_proof()["reason"], "native_chemical_product_required")

    def test_changed_exact_unit_or_native_inserter_feed_is_rejected(self):
        machine = self.lua.globals().entities[_key(self.machine)]
        machine.unit_number = 999
        self.assertFalse(self.run_proof()["ok"])
        machine.unit_number = self.args["machine"]["unit_number"]
        arm = self.lua.globals().entities[_key(self.core["input_arm"])]
        arm.pickup_position.x = 0
        self.assertEqual(self.run_proof()["reason"], "chemical_native_coal_feed_missing")

    def test_active_runtime_storage_mapping_supports_compressed_and_merged_native_boxes(self):
        self.assertTrue(self.run_proof()["ok"])
        self.lua.execute("shift=true")
        self.assertTrue(self.run_proof()["ok"])
        self.lua.execute("shift=false;merged_prototype=true")
        self.assertTrue(self.run_proof()["ok"])
        self.args["physical_boxes"]["2"] = "water"
        self.assertEqual(self.run_proof()["reason"], "chemical_different_fluids_merged_into_storage")
        self.args["physical_boxes"].pop("2")
        self.lua.execute("merged_prototype=false;missing_prototype=true")
        self.assertEqual(self.run_proof()["reason"], "chemical_required_physical_fluidbox_inactive")

    def test_lua_survives_native_flattening_and_has_no_game_mutation(self):
        for script in (CHEMICAL_PROOF_LUA, CHEMICAL_PIPE_SURVEY_LUA, CHEMICAL_CROSSING_PROOF_LUA):
            self.assertNotIn("--", script)
            for forbidden in ("insert_fluid", "set_fluid", "create_entity", ".researched=true", "insert{", "remove{"):
                self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
