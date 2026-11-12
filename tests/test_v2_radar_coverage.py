from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.defense_coverage import coverage_cells
from factorio_agent.radar_coverage import (
    RadarCoverage,
    CHUNK_SIZE,
    MAX_CANDIDATES,
    RADAR_COVERAGE_LUA,
    READY_POSTS_LUA,
    CANDIDATE_LAND_LUA,
    area_chunks,
    chunk_at,
    radar_chunks,
    factory_chunks,
    select_radar_cover,
    coverage_candidates,
)
from factorio_agent.radar_network import radar_core

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    return SimpleNamespace(
        fingerprint="c",
        entities={
            "radar": dict(
                type="radar",
                electric=True,
                burner=False,
                energy_usage=5000,
                collision_box={"left_top": {"x": -1.2, "y": -1.2}, "right_bottom": {"x": 1.2, "y": 1.2}},
            )
        },
    )


def geometry():
    return FactoryBuilder.__new__(FactoryBuilder)


def native_report(chunks, *, complete=False):
    return dict(
        ok=True,
        radars=[dict(unit_number=15071, position={"x": 0.5, "y": 0.5}, powered_working=True)],
        chunks=[
            dict(x=x, y=y, radar_units=[15071] if complete else [], native_visible=complete, charted=True)
            for x, y in chunks
        ],
        radius_chunks=3,
        target_chunks=len(chunks),
        radar_visible_chunks=len(chunks) if complete else 0,
        complete=complete,
    )


class ChunkTests(unittest.TestCase):
    def test_native_chunk32_uses_floor_at_negative_and_exact_boundaries(self):
        self.assertEqual(CHUNK_SIZE, 32)
        self.assertEqual(chunk_at((-0.001, -32)), (-1, -1))
        self.assertEqual(chunk_at((32, -32.001)), (1, -2))
        self.assertEqual(area_chunks([[-32, -32], [0, 0]]), {(-1, -1)})
        self.assertEqual(area_chunks([[31.5, -0.5], [32.5, 0.5]]), {(0, -1), (0, 0), (1, -1), (1, 0)})
        for bad in ([[-1, -1], [-1, 2]], [[0, 0], [float("inf"), 1]]):
            with self.assertRaises(ValueError):
                area_chunks(bad)

    def test_nearby_radius_three_is49_chunks_independent_of_charting_history(self):
        footprint = radar_chunks((-0.5, 0.5), 3)
        self.assertEqual(len(footprint), 49)
        self.assertIn((-4, 3), footprint)
        self.assertNotIn((3, 0), footprint)
        for radius in (True, 0, 17, 3.5):
            with self.assertRaises(ValueError):
                radar_chunks((0, 0), radius)

    def test_all_factory_mining_utility_bus_defense_and_typed_exit_chunks_are_targets(self):
        board = {
            "blocks": {
                "factory": {
                    "entities": [entity("assembling-machine-1", 0.5, 0.5)],
                    "reserved_area": [[0, 0], [64, 32]],
                },
                "mining": {
                    "entities": [entity("electric-mining-drill", 64.5, 64.5)],
                    "reserved_area": [[64, 64], [96, 96]],
                },
                "fluid": {"underground_pairs": [dict(first=[31.5, 96.5], last=[35.5, 96.5], direction=4, span=4)]},
            },
            "shared_main_bus": {
                "entities": [entity("transport-belt", -32.5, -32.5)],
                "reservations": [{"area": [[-64, -32], [-32, 0]]}],
            },
        }
        obs = {
            "entities": [
                dict(entity("steam-engine", 128.5, 0.5), type="generator"),
                dict(entity("character", 900.5, 900.5), type="character"),
            ]
        }
        posts = [{"position": {"x": 160, "y": 0}}]
        sources = [dict(item="coal", position={"x": 191.5, "y": 0.5}, direction="output", facing=4)]
        rows = factory_chunks(geometry(), obs, board, posts, sources)
        cells = {(q["x"], q["y"]): q["owners"] for q in rows}
        for p in ((0, 0), (1, 0), (2, 2), (-2, -2), (-2, -1), (4, 0), (5, 0), (6, 0), (0, 3), (1, 3)):
            self.assertIn(p, cells)
        self.assertNotIn((28, 28), cells)
        self.assertIn("mining", cells[(2, 2)])
        self.assertIn("defense_posts", cells[(5, 0)])
        self.assertIn("published_coal", cells[(6, 0)])

    def test_underworld_tunnel_or_unbounded_area_is_rejected(self):
        for block in (
            {"underground_pairs": [dict(input=[0, 0], output=[2, 2], direction=4, span=2)]},
            {"reserved_area": [[0, 0], [100000, 100000]]},
        ):
            with self.assertRaises(ValueError):
                factory_chunks(geometry(), {"entities": []}, {"blocks": {"bad": block}})


class CoverTests(unittest.TestCase):
    def test_deterministic_cover_prefers_gain_then_short_power_and_removes_redundancy(self):
        candidates = [
            dict(position={"x": 0, "y": 0}, chunks=[(0, 0)], power_distance=1),
            dict(position={"x": 1, "y": 0}, chunks=[(0, 0), (1, 0)], power_distance=3),
            dict(position={"x": 2, "y": 0}, chunks=[(1, 0), (2, 0)], power_distance=2),
        ]
        chosen, missing = select_radar_cover({(0, 0), (1, 0), (2, 0), (9, 9)}, candidates)
        self.assertEqual([q["position"]["x"] for q in chosen], [2, 0])
        self.assertEqual(missing, [(9, 9)])
        chosen, missing = select_radar_cover({(0, 0), (1, 0), (2, 0)}, candidates, maximum=1)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(missing, [(0, 0)])

    def test_candidates_need_two_real_service_disks_and_avoid_producer_growth_and_exits(self):
        posts = [dict(unit_number=1, position={"x": 0, "y": 0}), dict(unit_number=2, position={"x": 10, "y": 0})]
        obs = {"entities": [dict(entity("small-electric-pole", 5.5, -8.5), electric_network_id=2)]}
        board = {"blocks": {}}
        self.assertEqual(coverage_candidates(obs, board, posts[:1], {(0, 0)}, 3), [])
        rows = coverage_candidates(obs, board, posts, {(0, 0), (-1, -1)}, 3)
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), MAX_CANDIDATES)
        for r in rows:
            p = r["position"]
            corners = [(p["x"] + dx, p["y"] + dy) for dx in (-3.5, 3.5) for dy in (-3.5, 3.5)]
            for gun in posts:
                for x, y in corners:
                    self.assertLessEqual((x - gun["position"]["x"]) ** 2 + (y - gun["position"]["y"]) ** 2, 16**2)
        board["blocks"]["future"] = {"reserved_area": [[-100, -100], [100, 100]]}
        self.assertEqual(coverage_candidates(obs, board, posts, {(0, 0)}, 3), [])
        self.assertEqual(
            coverage_candidates(
                obs,
                {"blocks": {}},
                posts,
                {(0, 0)},
                3,
                hard={(x + 0.5, y + 0.5) for x in range(-30, 31) for y in range(-30, 31)},
            ),
            [],
        )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cat = catalog()
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.builder = geometry()
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.perimeter = SimpleNamespace(state={"posts": []})
        self.p = RadarCoverage(self.game, self.builder, self.cat, self.perimeter)
        self.board = dict(
            world_id="w",
            catalog_fingerprint="c",
            blocks={"factory": {"entities": [entity("transport-belt", 0.5, 0.5)]}},
        )
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.obs = dict(
            world_id="w", tick=1, entities=[dict(entity("radar", 0.5, 0.5), unit_number=15071, type="radar")]
        )
        self.game.query.return_value = native_report([(0, 0)])

    def test_charted_only_never_completes_global_proof_and_target_is_recomputed_after_growth(self):
        report = self.p.coverage_report(self.obs)
        self.assertTrue(report["ok"])
        self.assertFalse(report["complete"])
        self.assertTrue(report["chunks"][0]["charted"])
        self.assertIsNone(self.p.proof(self.obs))
        self.game.query.return_value = native_report([(0, 0)], complete=True)
        self.assertTrue(self.p.proof(self.obs)["complete"])
        self.board["blocks"]["new_factory"] = {"reserved_area": [[64, 0], [96, 32]]}
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.game.query.return_value = native_report([(0, 0), (2, 0)])
        report = self.p.coverage_report(self.obs)
        self.assertEqual(report["target_chunks"], 2)
        self.assertFalse(report["complete"])

    def test_original15071_is_never_silently_replaced_or_ignored_by_build_step(self):
        self.p.state = dict(
            world_id="w",
            catalog_fingerprint="c",
            phase="build",
            radars=[],
            preserved_radars=[dict(unit_number=15071, position={"x": 0.5, "y": 0.5})],
        )
        obs = dict(self.obs, entities=[dict(self.obs["entities"][0], unit_number=90000)])
        result = self.p.step(obs)
        self.assertEqual(result["reason"], "radar_coverage_original_radar_missing_or_replaced")
        self.game.query.assert_not_called()
        self.builder.ensure_plan.assert_not_called()

    def test_incomplete_or_duplicate_native_chunk_receipt_is_not_coverage(self):
        self.game.query.return_value = native_report([])
        self.assertEqual(self.p.coverage_report(self.obs)["reason"], "radar_coverage_native_receipt_incomplete")
        self.board["blocks"]["new_factory"] = {"entities": [entity("transport-belt", 64.5, 0.5)]}
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.game.query.return_value = native_report([(0, 0), (0, 0)], complete=True)
        self.assertEqual(self.p.coverage_report(self.obs)["reason"], "radar_coverage_native_receipt_incomplete")

    def test_changed_registered_radar_plan_cannot_pass_even_when_every_chunk_is_visible(self):
        core = radar_core(self.cat, 4.5, 4.5)
        self.p.state = dict(
            world_id="w",
            catalog_fingerprint="c",
            phase="verified",
            preserved_radars=[],
            radars=[dict(block="radar_coverage_1", core=core, plan=core, bindings={})],
        )
        self.game.query.return_value = native_report([(0, 0)], complete=True)
        self.assertIsNone(self.p.proof(self.obs))
        self.assertEqual(self.p.last_proof_failure["reason"], "radar_coverage_owned_plan_changed")

    def test_world_catalog_or_unpublished_source_frame_fail_before_any_build(self):
        self.board["world_id"] = "different"
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.assertEqual(self.p.coverage_report(self.obs)["reason"], "radar_coverage_context_invalid")
        self.builder.ensure_plan.assert_not_called()
        self.board["world_id"] = "w"
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        (self.root / "fluid-sources.json").write_text(
            json.dumps(dict(world_id="foreign", catalog_fingerprint="c", sources={}))
        )
        self.assertEqual(self.p.coverage_report(self.obs)["reason"], "radar_coverage_context_invalid")

    def test_registered_future_transport_and_fixed_apron_are_hard_for_new_radar(self):
        core = radar_core(self.cat, 4.5, 4.5)
        self.board["blocks"]["future"] = {"ports": [dict(position={"x": 4.5, "y": 4.5}, facing=4, direction="output")]}
        self.assertFalse(self.p._geometry(self.obs, self.board, [], core)["ok"])
        self.board["blocks"]["future"] = {"reserved_area": [[0, 0], [8, 8]]}
        self.assertFalse(self.p._geometry(self.obs, self.board, [], core)["ok"])

    def test_single_anchor_family_uses_remaining_gaps_instead_of_crowding_out_remote_oil(self):
        self.p.coverage_report = Mock(
            return_value=dict(
                ok=True,
                radars=[],
                uncovered_chunks=[{"x": 0, "y": 0}, {"x": 10, "y": 0}],
                radius_chunks=3,
                radar_visible_chunks=0,
            )
        )
        self.p._context = Mock(return_value=({"blocks": {}}, [], [{"x": 0, "y": 0}, {"x": 10, "y": 0}]))
        self.p._keepouts = Mock(return_value=(set(), []))
        self.p._geometry = Mock(return_value={"ok": True})
        self.p._site = Mock(return_value={"ok": True, "covering_posts": [{}, {}]})
        self.p._pair_preview = Mock(return_value={"ok": True, "required": False})
        self.p._power = Mock(return_value={"ok": True, "force_index": 1})

        def query(script):
            if "radar_coverage_defense_world_changed" in script:
                return {"ok": True, "effective_range": 16, "posts": []}
            if "radar_coverage_land_world_changed" in script:
                return {"ok": True, "legal": [1]}
            raise AssertionError("unexpected native query")

        self.game.query.side_effect = query
        obs = dict(
            self.obs,
            technologies={"radar": True},
            enabled_recipes={"radar": True, "small-electric-pole": True},
            entities=[dict(entity("small-electric-pole", 0.5, 0.5), unit_number=100, electric_network_id=2)],
        )
        groups = [
            [dict(position={"x": 10.5, "y": 0.5}, chunks=[(0, 0)], power_distance=1)],
            [dict(position={"x": 320.5, "y": 0.5}, chunks=[(10, 0)], power_distance=1)],
        ]
        seen = []

        def choose(*args, **kwargs):
            seen.append(set(args[3]))
            return groups[len(seen) - 1]

        with (
            patch("factorio_agent.radar_coverage.coverage_candidates", side_effect=choose) as candidates,
            patch("factorio_agent.radar_coverage.mining_reservations", return_value=[]),
            patch(
                "factorio_agent.radar_coverage.power_connection",
                return_value={
                    "ok": True,
                    "source_unit_number": 100,
                    "source_position": {"x": 0.5, "y": 0.5},
                    "entities": [],
                },
            ),
            patch("factorio_agent.radar_coverage.preflight", return_value={"ok": True, "clear_obstacles": []}),
        ):
            ready = self.p.prepare(obs)
        self.assertTrue(ready["ok"], ready)
        self.assertTrue(ready["expected_complete"])
        self.assertEqual(len(ready["radars"]), 2)
        self.assertEqual(seen, [{(0, 0), (10, 0)}, {(10, 0)}])
        self.assertEqual(candidates.call_args_list[1].kwargs["minimum_cover"], 1)
        self.assertFalse(self.p.path.exists())
        self.builder.ensure_plan.assert_not_called()


class PairAdoptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.builder = geometry()
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.c = catalog()
        self.core = radar_core(self.c, 4.5, 4.5)
        self.scope_id = "radar:coverage:1"
        self.anchor = dict(name="old_anchor", position={"x": 0, "y": 0}, unit_number=7, commissioned=True)
        self.base = dict(
            world_id="w",
            catalog_fingerprint="c",
            phase="armed",
            range=18,
            posts=[self.anchor, dict(name="far_post", position={"x": 100, "y": 100}, unit_number=8, commissioned=True)],
            coverage_scopes={"old": {"areas": [[[96, 96], [104, 104]]], "commissioned": True}},
        )
        self.perimeter = SimpleNamespace(state=deepcopy(self.base), bootstrap=Mock(), ensure_coverage=Mock())
        self.p = RadarCoverage(self.game, self.builder, self.c, self.perimeter)
        self.new = dict(
            name="coverage_" + self.scope_id + "_1",
            position={"x": 12, "y": 4},
            coverage_scope=self.scope_id,
            commissioned=False,
        )
        self.scope = dict(
            areas=[self.core["reserved_area"]],
            cells=coverage_cells([self.core["reserved_area"]]),
            effective_range=16,
            covering_posts=[self.new["name"], self.anchor["name"]],
            new_posts=[self.new["name"]],
            commissioned=False,
        )
        from factorio_agent.radar_coverage import _hash

        self.defense = dict(
            ok=True,
            required=True,
            scope_id=self.scope_id,
            scope=deepcopy(self.scope),
            new_posts=[self.new],
            base_state=deepcopy(self.base),
            base_revision=_hash(self.base),
            anchor=self.anchor,
            plan={"entities": [entity("gun-turret", 12, 4)]},
        )
        self.row = dict(block="radar_coverage_1", core=self.core, plan=self.core, bindings={}, defense=self.defense)
        self.p.state = dict(
            world_id="w", catalog_fingerprint="c", phase="build", radars=[self.row], preserved_radars=[]
        )
        self.obs = dict(world_id="w", entities=[], tick=1)
        self.path = self.root / "defense-perimeter.json"
        self.path.write_text(json.dumps(self.base))

    def test_readonly_clone_excludes_only_anchor_and_selects_second_distinct_whole_area_cover(self):
        clone = SimpleNamespace(state={}, _coverage_reservations=Mock(return_value=[]))

        def prepare(obs, scope, areas):
            self.assertEqual([q["name"] for q in clone.state["posts"]], ["far_post"])
            self.assertEqual(clone.state["coverage_scopes"], self.base["coverage_scopes"])
            self.assertTrue(clone._coverage_reservations(obs, areas))
            return dict(
                ok=True,
                new_posts=[deepcopy(self.new)],
                scope=dict(deepcopy(self.scope), covering_posts=[self.new["name"]]),
            )

        clone.prepare_coverage = prepare
        original = deepcopy(self.perimeter.state)
        with patch("factorio_agent.radar_coverage.DefensePerimeter", return_value=clone):
            result = self.p._pair_preview(
                self.game, self.obs, self.core, self.core, {"covering_posts": [self.anchor]}, self.scope_id
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["required"])
        self.assertEqual(len(result["new_posts"]), 1)
        self.assertNotIn("unit_number", result["new_posts"][0])
        self.assertEqual(self.perimeter.state, original)
        self.assertEqual(set(result["scope"]["covering_posts"]), {self.new["name"], self.anchor["name"]})
        self.assertFalse(self.p.path.exists())
        self.assertEqual(json.loads(self.path.read_text()), self.base)

    def test_pair_adoption_retains_every_old_post_scope_and_resumes_known_atomic_write(self):
        from factorio_agent import radar_coverage as module

        atomic = module._atomic_json

        def after_write(path, value):
            result = atomic(path, value)
            if Path(path) == self.path:
                raise OSError("known metadata write before acknowledgment")
            return result

        with patch.object(module, "_atomic_json", side_effect=after_write), self.assertRaises(OSError):
            self.p._adopt_pair(self.obs, self.row)
        saved = json.loads(self.p.path.read_text())
        self.assertIn("publication_intent", saved["radars"][0]["defense"])
        self.assertIsNone(self.p._adopt_pair(self.obs, self.row))
        self.assertTrue(self.defense["published"])
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["posts"][:-1], self.base["posts"])
        self.assertEqual(doc["coverage_scopes"]["old"], self.base["coverage_scopes"]["old"])
        self.assertEqual(len(doc["posts"]), 3)
        self.assertIsNone(self.p._adopt_pair(self.obs, self.row))
        self.assertEqual(len(json.loads(self.path.read_text())["posts"]), 3)
        # Normal binding/commissioning happens after adoption; retry must not
        # overwrite its actual unit and commissioned scope with the preview.
        doc["posts"][-1].update(unit_number=900, commissioned=True)
        doc["coverage_scopes"][self.scope_id].update(commissioned=True, commissioned_tick=20)
        self.path.write_text(json.dumps(doc))
        self.assertIsNone(self.p._adopt_pair(self.obs, self.row))
        self.assertEqual(self.perimeter.state["posts"][-1]["unit_number"], 900)

    def test_unknown_perimeter_change_blocks_adoption_and_spends_nothing(self):
        for mutate in (
            lambda d: d["posts"][0].update(unit_number=99),
            lambda d: d["coverage_scopes"]["old"].update(areas=[]),
            lambda d: d.update(foreign="new"),
        ):
            changed = deepcopy(self.base)
            mutate(changed)
            self.path.write_text(json.dumps(changed))
            result = self.p._adopt_pair(self.obs, self.row)
            self.assertEqual(result["reason"], "radar_coverage_perimeter_changed_before_pair_adoption")
        self.assertFalse(self.p.path.exists())
        self.builder.ensure_plan.assert_not_called()

    def test_normal_gun_and_ten_magazine_coverage_gate_precedes_any_radar_build(self):
        self.p._context = Mock(return_value=({"blocks": {}}, [], []))
        self.p.coverage_report = Mock(return_value={"ok": True, "complete": False})
        self.p._bind = Mock(return_value=None)
        self.p._reserve = Mock(return_value=None)
        self.p._adopt_pair = Mock(return_value=None)
        self.perimeter.ensure_coverage.return_value = dict(
            type="build", name="gun-turret", position=self.new["position"]
        )
        self.game.query.return_value = {"ok": True, "posts": [dict(unit_number=7)]}
        with patch("factorio_agent.radar_coverage.preflight", return_value={"ok": True}) as whole:
            result = self.p.step(self.obs)
        self.assertEqual(result["name"], "gun-turret")
        self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.assert_called_once_with(self.obs, self.scope_id, [self.core["reserved_area"]])
        self.assertIn(self.defense["plan"]["entities"][0], whole.call_args.args[3])

    def test_combined_guard_or_lost_native_anchor_blocks_before_scope_adoption(self):
        self.p._context = Mock(return_value=({"blocks": {}}, [], []))
        self.p.coverage_report = Mock(return_value={"ok": True, "complete": False})
        self.p._bind = Mock(return_value=None)
        self.p._reserve = Mock(return_value=None)
        self.p._adopt_pair = Mock(return_value=None)
        with patch(
            "factorio_agent.radar_coverage.preflight",
            return_value={"ok": False, "reason": "new_entity_over_resource_or_mining_reservation"},
        ):
            self.assertEqual(self.p.step(self.obs)["reason"], "new_entity_over_resource_or_mining_reservation")
        self.p._adopt_pair.assert_not_called()
        self.perimeter.ensure_coverage.assert_not_called()
        self.game.query.return_value = {"ok": True, "posts": []}
        with patch("factorio_agent.radar_coverage.preflight", return_value={"ok": True}):
            self.assertEqual(
                self.p.step(self.obs)["reason"], "radar_coverage_restore_exact_original_armed_anchor_first"
            )
        self.p._adopt_pair.assert_not_called()
        self.builder.ensure_plan.assert_not_called()

    def test_board_reservation_also_waits_for_fresh_combined_native_preflight(self):
        board = dict(world_id="w", catalog_fingerprint="c", blocks={})
        path = self.root / "pcb-board.json"
        path.write_text(json.dumps(board))
        self.p._site = Mock(return_value={"ok": True})
        with (
            patch("factorio_agent.radar_coverage.unbuilt_external_keepouts", return_value=set()),
            patch(
                "factorio_agent.radar_coverage.preflight",
                return_value={"ok": False, "reason": "new_entity_over_resource_or_mining_reservation"},
            ),
        ):
            result = self.p._reserve(self.obs, deepcopy(board), [])
        self.assertEqual(result["reason"], "new_entity_over_resource_or_mining_reservation")
        self.assertEqual(json.loads(path.read_text()), board)
        self.assertFalse(self.p.path.exists())


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("""helpers={json_to_table=function()return x end};d={world_id="w"};f={index=1};s={index=1};game={tick=10}
defines={entity_status={working=1},inventory={turret_ammo=3},build_check_type={manual_ghost=1}}
pos=function(p)return{x=p.x,y=p.y}end
prototypes={entity={radar={get_max_distance_of_nearby_sector_revealed=function(q)assert(q=="normal");return 3 end,get_max_energy_usage=function(q)assert(q=="normal");return 5000 end},["gun-turret"]={attack_parameters={range=18}}}}
f.is_chunk_charted=function()return true end;f.is_chunk_visible=function()return false end
e={position={x=-.5,y=.5},unit_number=15071,force=f,surface=s,quality={name="normal"},energy=5000,status=1,radar_scan_progress=.5,electric_network_id=2}
e.is_connected_to_electric_network=function()return true end;target=function()return e end
x={world_id="w",radar_watts=300000,radars={{unit_number=15071,position=e.position}},chunks={{x=-4,y=3},{x=2,y=0},{x=3,y=0}}}""")

    def test_exact_native_radius_power_scan_and_visibility_are_separate_from_history(self):
        r = self.lua.execute(RADAR_COVERAGE_LUA.replace("PAYLOAD", '"input"'))
        self.assertTrue(r["ok"])
        self.assertEqual(r["radius_chunks"], 3)
        self.assertEqual(r["geometric_covered_chunks"], 2)
        self.assertEqual(r["charted_chunks"], 3)
        self.assertEqual(r["radar_visible_chunks"], 0)
        self.assertFalse(r["complete"])
        self.lua.execute("f.is_chunk_visible=function()return true end;table.remove(x.chunks,3)")
        self.assertTrue(self.lua.execute(RADAR_COVERAGE_LUA.replace("PAYLOAD", '"input"'))["complete"])
        self.lua.execute("e.energy=0")
        r = self.lua.execute(RADAR_COVERAGE_LUA.replace("PAYLOAD", '"input"'))
        self.assertFalse(r["complete"])
        self.assertEqual(r["geometric_covered_chunks"], 0)

    def test_exact_existing_radar_and_world_or_native_radius_change_fail(self):
        for mutation in (
            "e.unit_number=15072",
            'e.quality.name="uncommon"',
            'd.world_id="different"',
            "prototypes.entity.radar.get_max_distance_of_nearby_sector_revealed=function()return 99 end",
        ):
            self.setUp()
            self.lua.execute(mutation)
            self.assertFalse(self.lua.execute(RADAR_COVERAGE_LUA.replace("PAYLOAD", '"input"'))["ok"])

    def test_red_cover_is_exact_tier_and_partial_magazine_is_not_counted_as_full(self):
        self.lua.execute("""e.health=400;e.max_health=400;inv={{valid_for_read=true,name="piercing-rounds-magazine",quality={name="normal"},count=5,ammo=10,prototype={magazine_size=10}}}
e.get_inventory=function(q)assert(q==defines.inventory.turret_ammo);return inv end
x={world_id="w",posts={{name="post",position=e.position,unit_number=15071,ammo_item="piercing-rounds-magazine"}}}""")
        script = READY_POSTS_LUA.replace("PAYLOAD", '"input"')
        r = self.lua.execute(script)
        self.assertEqual(len(r["posts"]), 1)
        self.assertEqual(r["effective_range"], 16)
        self.lua.execute("inv[1].ammo=3")
        self.assertEqual(len(self.lua.execute(script)["posts"]), 0)
        self.lua.execute('inv[1].ammo=10;x.posts[1].ammo_item="firearm-magazine"')
        self.assertEqual(len(self.lua.execute(script)["posts"]), 0)
        self.lua.execute('x.posts[1].ammo_item="arbitrary-item"')
        self.assertFalse(self.lua.execute(script)["ok"])

    def test_resource_patch_and_mining_growth_are_rejected_even_when_can_place_is_true(self):
        self.lua.execute("""s.count_entities_filtered=function()return 0 end;s.can_place_entity=function()return true end
x={world_id="w",cores={{reserved_area={{0,0},{7,7}},entities={{name="radar",position={x=3.5,y=3.5}}}}},zones={}}""")
        script = CANDIDATE_LAND_LUA.replace("PAYLOAD", '"input"')
        self.assertEqual(len(self.lua.execute(script)["legal"]), 1)
        self.lua.execute("s.count_entities_filtered=function()return 1 end")
        self.assertEqual(len(self.lua.execute(script)["legal"]), 0)
        self.lua.execute("s.count_entities_filtered=function()return 0 end;x.zones={{area={{6,6},{20,20}}}}")
        self.assertEqual(len(self.lua.execute(script)["legal"]), 0)


if __name__ == "__main__":
    unittest.main()
