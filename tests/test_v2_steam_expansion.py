from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.board import entity
from factorio_agent.smelting import port
from factorio_agent.steam_expansion import SteamExpansion
from factorio_agent.steam_layout import plant_frame, water_connected, world_point
from test_v2_steam_layout import plant


def steam_pair():
    """The core a pre-v2 state saved: the fixed development-map pair (legacy data only)."""
    es = [
        entity("boiler", 37.5, 1, 8),
        entity("steam-engine", 37.5, 4.5, 8),
        entity("steam-engine", 37.5, 9.5, 8),
        entity("burner-inserter", 37.5, -0.5, 0),
        entity("transport-belt", 37.5, -1.5, 8),
    ]
    es += [entity("small-electric-pole", 35.5, y) for y in (1.5, 5.5, 9.5)]
    es += [entity("small-electric-pole", 40.5, 5.5)]
    es += [entity("pipe", 39.5, 0.5), entity("pipe-to-ground", 40.5, 0.5, 12), entity("pipe-to-ground", 44.5, 0.5, 4)]
    return _plan(
        es,
        [port("coal", 37.5, -1.5, 8)],
        reserved_area=[[35, -4], [45, 13]],
        template="additional_shared_water_steam_pair",
    )


CLEAR = {"ok": True, "clear_obstacles": []}


@contextmanager
def prepared(bank, power=None):
    """Offline prepare: live preflight, coal bank, belt and pole planners faked."""
    power = power or {"ok": True, "entities": [], "wire_hops": 1}
    with (
        patch("factorio_agent.steam_expansion.preflight", return_value=CLEAR),
        patch("factorio_agent.steam_siting.preflight", return_value=CLEAR),
        patch("factorio_agent.steam_expansion.prepare_new_bank", return_value=bank),
        patch(
            "factorio_agent.steam_expansion.plan_connection",
            return_value={"ok": True, "entities": [], "underground_pairs": []},
        ) as route,
        patch("factorio_agent.steam_expansion.power_connection", return_value=power) as poles,
    ):
        yield route, poles


class SteamExpansionTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        obj = SteamExpansion.__new__(SteamExpansion)
        obj.path = root / "steam-expansion.json"
        obj.catalog = SimpleNamespace(fingerprint="c", entities={"steam-engine": {"energy_production": 15000}})
        obj.game = Mock()
        _, power = plant(4)
        obj.site = None
        obj.builder = SimpleNamespace(
            ensure_plan=Mock(return_value={"type": "build", "name": "boiler"}), state={"power_plan": power}
        )
        source = port("coal", -7.5, 50.5, 4)
        bank = {
            "ok": True,
            "plan": _plan(
                [entity("transport-belt", -7.5, 50.5, 4)],
                [source],
                mining_reserved_area=[[-25, 47], [-13, 54]],
                layout_contract={"fixed_output_port": source},
            ),
            "layout_contract": {"fixed_output_port": source},
            "reserved_area": [[-25, 47], [-4, 54]],
            "mining_reserved_area": [[-25, 47], [-13, 54]],
            "nominal_rate_per_minute": 66,
        }
        obj.outposts = SimpleNamespace(
            path=root / "mining-outposts.json",
            state={"world_id": "w", "catalog_fingerprint": "c", "banks": []},
            publish_reservations=Mock(return_value=None),
        )
        obj.state = {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "bank": deepcopy(bank),
            "core": steam_pair(),
            "plan": _plan(bank["plan"]["entities"], []),
            "samples": [],
        }
        (root / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {"steam_expansion": steam_pair()}}))
        obs = {
            "world_id": "w",
            "tick": 1,
            "entities": [{"name": "steam-engine", "unit_number": 10, "energy": 100, "position": {"x": 0, "y": 0}}],
        }
        return obj, bank, obs

    def test_prepare_passes_the_ordinary_fixed_source_without_mining_annotation(self):
        obj, bank, obs = self.fixture()
        obj.state = {}
        with prepared(bank) as (route, _):
            result = obj.prepare(obs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(route.call_args.args[3], bank["plan"]["ports"][0])
        self.assertNotIn("mining_resource", route.call_args.args[3])
        self.assertEqual(result["bank"]["plan"]["mining_reserved_area"], bank["mining_reserved_area"])
        self.assertEqual(result["bank"]["layout_contract"], bank["layout_contract"])
        self.assertFalse(obj.outposts.path.exists())

    def test_contracted_bank_is_reserved_before_first_construction_and_resume_is_idempotent(self):
        obj, bank, obs = self.fixture()

        def build(*_):
            saved = json.loads(obj.outposts.path.read_text())
            self.assertEqual(len(saved["banks"]), 1)
            self.assertFalse(saved["banks"][0]["ready"])
            self.assertEqual(saved["banks"][0]["plan"]["ports"], bank["plan"]["ports"])
            obj.outposts.publish_reservations.assert_called_with(obs)
            return {"type": "build", "name": "boiler"}

        obj.builder.ensure_plan.side_effect = build
        self.assertEqual(obj.step(obs)["type"], "build")
        self.assertEqual(obj.step(obs)["type"], "build")
        self.assertEqual(len(obj.outposts.state["banks"]), 1)

    def test_same_fixed_port_with_changed_canonical_plan_blocks_before_build(self):
        obj, bank, obs = self.fixture()
        changed = deepcopy(bank)
        changed["plan"]["entities"][0]["direction"] = 8
        obj.outposts.state["banks"] = [changed]
        result = obj.step(obs)
        self.assertEqual(result["reason"], "steam_fixed_coal_bank_plan_changed")
        obj.builder.ensure_plan.assert_not_called()

    def test_prepare_places_the_next_bank_of_the_saved_plant(self):
        obj, bank, obs = self.fixture()
        obj.state = {}
        power = obj.builder.state["power_plan"]
        frame = plant_frame(power)
        pole = entity("small-electric-pole", *world_point(frame, 6, 0))
        with prepared(bank, {"ok": True, "entities": [pole], "wire_hops": 2}) as (route, poles):
            result = obj.prepare(obs)
        self.assertTrue(result["ok"], result)
        core = result["core"]
        self.assertEqual(core["template"], "steam_bank_next_pair")
        self.assertEqual(core["layout"]["kind"], "adjacent_bank")
        self.assertFalse(core["layout"]["degraded"])
        new = next(e for e in core["entities"] if e["name"] == "boiler")
        side = core["layout"]["side"]
        self.assertEqual((new["position"]["x"], new["position"]["y"]), world_point(frame, 8 * side, 0.5))
        self.assertEqual(new["direction"], 4)
        old = next(e for e in power["entities"] if e["name"] == "boiler")
        self.assertTrue(water_connected(power["entities"] + result["plan"]["entities"], old, new))
        # Coal enters the stub the bank's outlet faces; the belt ends on the core's coal port.
        self.assertEqual(route.call_args.args[4], core["ports"][0])
        self.assertEqual(core["ports"][0]["item"], "coal")
        self.assertIn(pole, result["plan"]["entities"])
        goal = next(p for p in core["entities"] if p["name"] == "small-electric-pole")
        self.assertNotIn("small-electric-pole", {e["name"] for e in poles.call_args.args[4]})
        self.assertTrue(any(goal["position"] == p["position"] for p in result["plan"]["entities"]))
        x0, y0 = core["reserved_area"][0]
        x1, y1 = core["reserved_area"][1]
        self.assertTrue(
            all(x0 <= e["position"]["x"] <= x1 and y0 <= e["position"]["y"] <= y1 for e in core["entities"])
        )

    def test_prepare_blocks_without_a_template_plant_or_free_site(self):
        obj, bank, obs = self.fixture()
        obj.state = {}
        obj.builder.state = {"power_plan": {"entities": []}}
        with prepared(bank):
            result = obj.prepare(obs)
        self.assertEqual(result["reason"], "steam_expansion_no_buildable_bank_slot")
        from factorio_agent import plan_needs

        needs = plan_needs.from_result("steam_expansion", dict(result, status="blocked"))
        self.assertEqual(needs["kind"], "site")
        self.assertIn("first_plant_plan_has_no_boiler", needs["rejected_by"])

    def test_an_unpowered_plant_is_a_wait_not_a_block(self):
        # Seed 1188507449 (74 min): the boiler had run out of coal; upkeep refuelled it the same step.
        obj, bank, obs = self.fixture()
        obj.state = {}
        obs["entities"][0]["energy"] = 0
        result = obj.step(obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["wait_for"], "running_source_plant_required")
        self.assertEqual(obj.state, {})

    def test_prepare_then_step_to_thirty_second_proof(self):
        obj, bank, obs = self.fixture()
        obj.state = {}
        (obj.path.parent / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {}}))
        with prepared(bank):
            first = obj.step(obs)
        self.assertEqual(first["type"], "build")
        board = json.loads((obj.path.parent / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"]["steam_expansion"]["reserved_area"], obj.state["core"]["reserved_area"])
        obj.builder.ensure_plan.return_value = {"status": "succeeded"}
        obj.sample = lambda o: {"ok": True, "tick": o["tick"], "engines": [{"unit_number": 21}, {"unit_number": 22}]}
        retained = patch("factorio_agent.mining_layout_policy.validate_retained_units", return_value={"ok": True})
        with retained:
            results = []
            for tick in (100, 700, 1300, 1900):
                obs["tick"] = tick
                results.append(obj.step(obs))
        self.assertEqual([r["status"] for r in results], ["waiting"] * 3 + ["succeeded"])
        evidence = results[-1]["evidence"]
        self.assertEqual(evidence["layout"]["kind"], "adjacent_bank")
        self.assertEqual(evidence["from_tick"], 100)
        self.assertTrue(obj.outposts.state["banks"][0]["ready"])

    def test_old_fixed_layout_state_keeps_validating(self):
        obj, bank, obs = self.fixture()
        startup = {"reserved_area": [[20, -6], [48, 14]], "entities": []}
        (obj.path.parent / "pcb-board.json").write_text(
            json.dumps({"world_id": "w", "blocks": {"startup_science": startup}})
        )
        obj.prepare = Mock(side_effect=AssertionError("a saved state is never re-planned"))
        self.assertEqual(obj.step(obs)["type"], "build")
        board = json.loads((obj.path.parent / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"]["startup_science"]["reserved_area"][1][0], 35)
        self.assertEqual(board["blocks"]["steam_expansion"]["reserved_area"], [[35, -4], [45, 13]])
        self.assertTrue(obj.owns_burner(entity("boiler", 37.5, 1, 8), obs))
        obj.state["samples"] = [
            {"tick": 0, "engines": [{"unit_number": 2}]},
            {"tick": 1800, "engines": [{"unit_number": 2}]},
        ]
        obj.state["preserved_engine_units"] = [10]
        obj.state["added_nominal_kw"] = 1800
        obj.state["bank"]["nominal_rate_per_minute"] = 60
        obj.sample = lambda _: {"ok": True, "tick": 1801, "engines": [{"unit_number": 2}]}
        obs["entities"][0]["unit_number"] = 10
        self.assertEqual(obj.proof(obs)["layout"], {"kind": "legacy_fixed_layout", "degraded": False})

    def test_history_cannot_replace_a_fresh_native_generation_check(self):
        obj = SteamExpansion.__new__(SteamExpansion)
        obj.state = {
            "world_id": "w",
            "samples": [
                {"tick": 100, "engines": [{"unit_number": 2}]},
                {"tick": 1900, "engines": [{"unit_number": 2}]},
            ],
            "preserved_engine_units": [1],
            "added_nominal_kw": 1800,
            "bank": {"nominal_rate_per_minute": 60},
        }
        obs = {"world_id": "w", "tick": 1901, "entities": [{"name": "steam-engine", "unit_number": 1}]}
        obj.sample = lambda _: None
        self.assertIsNone(obj.proof(obs))
        obj.sample = lambda _: {"ok": True, "engines": [{"unit_number": 3}]}
        self.assertIsNone(obj.proof(obs))
        obj.sample = lambda _: {"ok": True, "tick": 1902, "engines": [{"unit_number": 2}]}
        self.assertIsNotNone(obj.proof(obs))
        obs["tick"] = 1899
        self.assertIsNotNone(obj.proof(obs))


if __name__ == "__main__":
    unittest.main()
