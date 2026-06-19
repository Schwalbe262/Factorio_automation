import unittest

from factorio_ai.code_agent import (
    ApiError,
    FactorioApi,
    ProgramResult,
    build_code_agent_prompt,
    extract_program,
    raw_ingredients,
    run_code_agent_step,
    run_program,
    tile_blocker,
)


def _obs():
    return {
        "tick": 1000,
        "player": {"position": {"x": -4.0, "y": 22.0}},
        "inventory": {"coal": 30, "iron-plate": 5, "burner-mining-drill": 1},
        "entities": [
            {"name": "burner-mining-drill", "position": {"x": 70.0, "y": 67.0}, "status_name": "no_minable_resources"},
            {"name": "stone-furnace", "position": {"x": 4.0, "y": 0.0}, "recipe": "iron-plate"},
        ],
        "resources": [
            {"name": "iron-ore", "position": {"x": 56.0, "y": 68.0}, "distance": 75.0},
            {"name": "coal", "position": {"x": 69.0, "y": -31.0}, "distance": 91.0},
        ],
        "research": {"current": "logistics"},
    }


class FakeBackend:
    """Records actions and serves a fixed observation."""

    def __init__(self):
        self.calls = []

    def act(self, action):
        self.calls.append(action)
        return {"ok": True, "echo": action.get("type")}

    def observe(self):
        return _obs()


class FactorioApiTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.api = FactorioApi(self.backend.act, self.backend.observe)

    def test_queries_read_observation(self):
        self.assertEqual(self.api.position(), {"x": -4.0, "y": 22.0})
        self.assertEqual(self.api.inventory("coal"), 30)
        self.assertEqual(self.api.inventory()["iron-plate"], 5)
        self.assertEqual(len(self.api.entities("burner-mining-drill")), 1)
        self.assertEqual(self.api.nearest_resource("iron-ore")["position"], {"x": 56.0, "y": 68.0})
        self.assertIsNone(self.api.nearest_resource("uranium-ore"))

    def test_action_verbs_emit_validated_actions(self):
        self.assertTrue(self.api.move_to(10, 20)["ok"])
        self.assertEqual(self.backend.calls[-1], {"type": "move_to", "position": {"x": 10.0, "y": 20.0}})
        self.api.build("burner-mining-drill", 56, 68, direction=4)
        self.assertEqual(self.backend.calls[-1]["type"], "build")
        self.assertEqual(self.backend.calls[-1]["position"], {"x": 56.0, "y": 68.0})
        self.api.insert("coal", 5, 56, 68)
        self.assertEqual(self.backend.calls[-1], {"type": "insert", "item": "coal", "count": 5, "position": {"x": 56.0, "y": 68.0}})

    def test_count_floors_to_one(self):
        # The API must never emit count<1 (which the executor rejects with "count must be positive").
        self.api.craft("iron-gear-wheel", 0)
        self.assertEqual(self.backend.calls[-1]["count"], 1)
        self.api.mine(1, 1, -5)
        self.assertEqual(self.backend.calls[-1]["count"], 1)

    def test_build_requires_name(self):
        with self.assertRaises(ApiError):
            self.api.build("", 1, 2)

    def test_log_records(self):
        self.api.log("hello")
        self.assertIn("hello", self.api.logs)

    def test_run_skill_hybrid_verb(self):
        calls = []

        def run_skill(name, max_steps):
            calls.append((name, max_steps))
            return {"ok": True, "reason": "did it", "skill": name}

        api = FactorioApi(self.backend.act, self.backend.observe,
                          run_skill=run_skill, skill_names=["produce_iron_plate", "setup_power"])
        out = api.run_skill("produce_iron_plate", max_steps=20)
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("produce_iron_plate", 20)])
        self.assertIn("produce_iron_plate", api.skill_names)

    def test_run_skill_unavailable_returns_not_ok(self):
        # No run_skill injected -> verb degrades gracefully, never raises.
        out = self.api.run_skill("produce_iron_plate")
        self.assertFalse(out["ok"])

    def test_run_skill_usable_from_program(self):
        names = []
        api = FactorioApi(self.backend.act, self.backend.observe,
                          run_skill=lambda n, m: names.append(n) or {"ok": True, "skill": n},
                          skill_names=["produce_iron_plate"])
        res = run_program("r = api.run_skill('produce_iron_plate', max_steps=5)\nlog('skill ok=' + str(r['ok']))\n", api)
        self.assertTrue(res.ok, res.error)
        self.assertIn("skill ok=True", res.output)
        self.assertEqual(names, ["produce_iron_plate"])


def _planning_obs():
    return {
        "tick": 2000,
        "player": {"position": {"x": 0.0, "y": 0.0}},
        "inventory": {"iron-plate": 10, "copper-plate": 4},
        "craftable": {"iron-gear-wheel": 5, "electronic-circuit": 0},
        "entities": [
            {"name": "stone-furnace", "type": "furnace", "position": {"x": 4.0, "y": 0.0}},
            {"name": "rock-big", "type": "simple-entity", "position": {"x": -3.0, "y": 0.0}},
            {"name": "character", "position": {"x": 0.0, "y": 0.0}},
        ],
        "resources": [
            {"name": "iron-ore", "position": {"x": 50.0, "y": 60.0}, "distance": 78.0},
            {"name": "iron-ore", "position": {"x": 51.0, "y": 60.0}, "distance": 78.5},
            {"name": "iron-ore", "position": {"x": 50.0, "y": 61.0}, "distance": 79.0},
            {"name": "iron-ore", "position": {"x": 52.0, "y": 62.0}, "distance": 80.0},
        ],
        "research": {"current": "automation"},
    }


class PlanningQueryTests(unittest.TestCase):
    def setUp(self):
        self.api = FactorioApi(lambda a: {"ok": True}, _planning_obs)

    def test_recipe_lookup(self):
        r = self.api.recipe("iron-gear-wheel")
        self.assertIsNotNone(r)
        self.assertEqual(r["ingredients"], {"iron-plate": 2})
        self.assertIsNone(self.api.recipe("iron-ore"))  # raw resource has no crafting recipe

    def test_ingredients_for_scales_with_count(self):
        self.assertEqual(self.api.ingredients_for("iron-gear-wheel", 3), {"iron-plate": 6.0})
        # copper-cable yields 2 per craft -> 1 copper-plate makes 2 cables
        self.assertEqual(self.api.ingredients_for("copper-cable", 2), {"copper-plate": 1.0})
        self.assertEqual(self.api.ingredients_for("iron-ore"), {})  # raw

    def test_raw_ingredients_expands_to_base(self):
        # 1 gear -> 2 iron-plate -> 2 iron-ore
        self.assertEqual(self.api.raw_ingredients_for("iron-gear-wheel", 1), {"iron-ore": 2.0})

    def test_raw_ingredients_terminates_on_unknown(self):
        self.assertEqual(raw_ingredients("nonexistent-item-xyz", 3), {"nonexistent-item-xyz": 3.0})

    def test_craftable_reads_observation(self):
        self.assertEqual(self.api.craftable("iron-gear-wheel"), 5)
        self.assertEqual(self.api.craftable("electronic-circuit"), 0)

    def test_nearest_entity_and_nearest(self):
        self.assertEqual(self.api.nearest_entity("stone-furnace")["position"], {"x": 4.0, "y": 0.0})
        self.assertEqual(self.api.nearest("iron-ore")["position"], {"x": 50.0, "y": 60.0})
        self.assertEqual(self.api.nearest("stone-furnace")["position"], {"x": 4.0, "y": 0.0})

    def test_resource_patch_bounds_and_count(self):
        patch = self.api.resource_patch("iron-ore")
        self.assertEqual(patch["count"], 4)
        self.assertEqual(patch["bounds"], {"min_x": 50.0, "min_y": 60.0, "max_x": 52.0, "max_y": 62.0})
        self.assertEqual(patch["width"], 3.0)
        self.assertEqual(patch["nearest"], {"x": 50.0, "y": 60.0})
        self.assertIsNone(self.api.resource_patch("uranium-ore"))

    def test_production_chain_is_dependency_first(self):
        chain = self.api.production_chain("iron-gear-wheel")
        items = [step["item"] for step in chain]
        self.assertEqual(items, ["iron-plate", "iron-gear-wheel"])  # plate before gear
        gear = chain[-1]
        self.assertEqual(gear["ingredients"], {"iron-plate": 2})
        self.assertIn("machine", gear)  # crafting category present

    def test_missing_for_uses_inventory(self):
        # inventory has 10 iron-plate. 1 gear needs 2 plate -> nothing missing.
        self.assertEqual(self.api.missing_for("iron-gear-wheel", 1), {})
        # 10 gears need 20 plate; have 10 -> short 10 plate -> expands to 10 iron-ore.
        miss = self.api.missing_for("iron-gear-wheel", 10)
        self.assertEqual(miss, {"iron-ore": 10.0})


class FootprintTests(unittest.TestCase):
    def setUp(self):
        self.obs = _planning_obs()
        self.api = FactorioApi(lambda a: {"ok": True}, lambda: self.obs)

    def test_tile_blocker_detects_overlap(self):
        # placing a furnace on the existing furnace tile -> blocked
        self.assertIsNotNone(tile_blocker(self.obs, 4.0, 0.0, "stone-furnace"))
        # an open tile far from anything -> free
        self.assertIsNone(tile_blocker(self.obs, 20.0, 20.0, "stone-furnace"))

    def test_can_place_respects_blockers(self):
        self.assertFalse(self.api.can_place("stone-furnace", 4.0, 0.0))   # on furnace
        self.assertFalse(self.api.can_place("burner-mining-drill", -3.0, 0.0))  # on rock
        self.assertTrue(self.api.can_place("stone-furnace", 20.0, 20.0))

    def test_find_buildable_finds_nearby_free_tile(self):
        spot = self.api.find_buildable("stone-furnace", 4.0, 0.0, radius=6)
        self.assertIsNotNone(spot)
        self.assertIsNone(tile_blocker(self.obs, spot["x"], spot["y"], "stone-furnace"))

    def test_build_returns_position_handle(self):
        res = self.api.build("burner-mining-drill", 50, 60)
        self.assertEqual((res["x"], res["y"]), (50.0, 60.0))

    def test_place_next_to_offsets_from_reference(self):
        calls = []
        api = FactorioApi(lambda a: calls.append(a) or {"ok": True}, lambda: self.obs)
        ref = {"name": "stone-furnace", "position": {"x": 10.0, "y": 10.0}}
        api.place_next_to("burner-inserter", ref, "east")
        # furnace half=1.0, inserter half=0.5 -> offset 1.5 east of x=10
        self.assertEqual(calls[-1]["position"], {"x": 11.5, "y": 10.0})

    def test_place_next_to_rejects_bad_side(self):
        with self.assertRaises(ApiError):
            self.api.place_next_to("burner-inserter", {"x": 0, "y": 0}, "diagonal")

    def test_entities_near_filters_by_radius(self):
        near = self.api.entities_near(4.0, 0.0, radius=2.0)
        names = [e["name"] for e in near]
        self.assertIn("stone-furnace", names)      # at (4,0), distance 0
        self.assertNotIn("rock-big", names)        # at (-3,0), distance 7 > 2
        only_rock = self.api.entities_near(-3.0, 0.0, radius=1.0, name="rock-big")
        self.assertEqual(len(only_rock), 1)


class ProduceUntilTests(unittest.TestCase):
    def test_waits_until_target_then_returns(self):
        # observe() reports more iron-plate after each wait, simulating production.
        state = {"plates": 0}
        waits = []

        def act(action):
            if action.get("type") == "wait":
                state["plates"] += 4
                waits.append(action["ticks"])
            return {"ok": True}

        def observe():
            return {"player": {"position": {"x": 0, "y": 0}},
                    "inventory": {"iron-plate": state["plates"]}, "entities": [], "resources": []}

        api = FactorioApi(act, observe)
        final = api.produce_until("iron-plate", 10, max_waits=5, ticks=120)
        self.assertGreaterEqual(final, 10)
        self.assertEqual(len(waits), 3)  # 0 -> 4 -> 8 -> 12 (>=10 after 3 waits)

    def test_stops_at_max_waits(self):
        api = FactorioApi(lambda a: {"ok": True},
                          lambda: {"player": {"position": {"x": 0, "y": 0}},
                                   "inventory": {"iron-plate": 0}, "entities": [], "resources": []})
        final = api.produce_until("iron-plate", 10, max_waits=2)
        self.assertEqual(final, 0)  # never produced; bounded by max_waits, does not hang


class MemoryTests(unittest.TestCase):
    def test_remember_and_recall(self):
        api = FactorioApi(lambda a: {"ok": True}, _planning_obs)
        api.remember("base", {"x": 5, "y": 6})
        self.assertEqual(api.recall("base"), {"x": 5, "y": 6})
        self.assertIsNone(api.recall("missing"))
        self.assertEqual(api.recall("missing", 42), 42)

    def test_memory_persists_across_steps(self):
        backend = FakeBackend()
        api = FactorioApi(backend.act, backend.observe)
        completions = [
            "```python\napi.remember('phase', 'mining')\nlog('stored')\n```",
            "```python\nlog('phase=' + str(api.recall('phase')))\n```",
        ]
        idx = {"i": 0}

        def complete(_prompt):
            out = completions[idx["i"]]
            idx["i"] += 1
            return out

        from factorio_ai.code_agent import run_code_agent_step
        run_code_agent_step("x", api, complete)
        _, result = run_code_agent_step("x", api, complete)
        self.assertIn("phase=mining", result.output)

    def test_researched_best_effort(self):
        obs = dict(_planning_obs())
        obs["research"] = {"current": "automation", "technologies": {"automation": {"researched": True}}}
        api = FactorioApi(lambda a: {"ok": True}, lambda: obs)
        self.assertTrue(api.researched("automation"))
        self.assertFalse(api.researched("logistics"))


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.api = FactorioApi(self.backend.act, self.backend.observe)

    def test_program_can_drive_api(self):
        prog = (
            "ore = api.nearest_resource('iron-ore')\n"
            "p = ore['position']\n"
            "api.move_to(p['x'], p['y'])\n"
            "api.build('burner-mining-drill', p['x'], p['y'], direction=4)\n"
            "api.insert('coal', 10, p['x'], p['y'])\n"
            "log('placed drill on iron')\n"
            "print('done', api.actions_run)\n"
        )
        result = run_program(prog, self.api)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.api.actions_run, 3)
        self.assertIn("done 3", result.output)
        self.assertIn("placed drill on iron", result.output)

    def test_import_is_blocked(self):
        result = run_program("import os\nos.system('echo hi')\n", self.api)
        self.assertFalse(result.ok)
        self.assertIn("import", result.error.lower())

    def test_open_is_blocked(self):
        result = run_program("open('x.txt','w')\n", self.api)
        self.assertFalse(result.ok)
        self.assertIn("open", result.error)

    def test_exception_is_captured_not_raised(self):
        result = run_program("api.build('drill', 1, 2)\nraise ValueError('boom')\n", self.api)
        self.assertFalse(result.ok)
        self.assertIn("boom", result.error)

    def test_timeout_guards_infinite_loop(self):
        result = run_program("while True:\n    pass\n", self.api, timeout_seconds=0.5)
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.error.lower())


class PromptAndStepTests(unittest.TestCase):
    def test_extract_program_handles_fences(self):
        self.assertEqual(extract_program("```python\napi.wait()\n```"), "api.wait()")
        self.assertEqual(extract_program("```\nx=1\n```"), "x=1")
        self.assertEqual(extract_program("api.wait()"), "api.wait()")

    def test_prompt_contains_state_and_api(self):
        api = FactorioApi(FakeBackend().act, FakeBackend().observe)
        prompt = build_code_agent_prompt("launch_rocket_program", api)
        self.assertIn("api.connect", prompt)
        self.assertIn("iron-ore@(56.0,68.0)", prompt)
        self.assertIn("current_research: logistics", prompt)
        self.assertIn("burner-mining-drill:1", prompt)

    def test_prompt_includes_previous_feedback(self):
        api = FactorioApi(FakeBackend().act, FakeBackend().observe)
        prev = ProgramResult(False, "out", "Traceback: boom", 2)
        prompt = build_code_agent_prompt("x", api, previous_program="api.build('a',1,2)", previous_result=prev)
        self.assertIn("PREVIOUS PROGRAM", prompt)
        self.assertIn("boom", prompt)

    def test_prompt_lists_skill_catalog_with_descriptions(self):
        api = FactorioApi(FakeBackend().act, FakeBackend().observe,
                          run_skill=lambda n, m: {"ok": True},
                          skill_names=["produce_iron_plate", "setup_power"])
        prompt = build_code_agent_prompt("x", api)
        self.assertIn("produce_iron_plate:", prompt)
        self.assertIn("burner drill", prompt)  # from SKILL_DESCRIPTIONS
        self.assertIn("setup_power:", prompt)

    def test_run_step_completes_and_executes(self):
        backend = FakeBackend()
        api = FactorioApi(backend.act, backend.observe)
        completions = ["```python\napi.move_to(1,2)\nlog('moved')\n```"]
        program, result = run_code_agent_step("x", api, lambda _p: completions[0])
        self.assertTrue(result.ok, result.error)
        self.assertIn("moved", result.output)
        self.assertEqual(backend.calls[-1]["type"], "move_to")

    def test_run_step_handles_empty_program(self):
        backend = FakeBackend()
        api = FactorioApi(backend.act, backend.observe)
        program, result = run_code_agent_step("x", api, lambda _p: "no code here, sorry")
        # falls back to whole text -> not valid python -> error captured, never raises
        self.assertFalse(result.ok)


class LoopTests(unittest.TestCase):
    def test_loop_runs_n_cycles_and_feeds_back(self):
        from factorio_ai.code_agent import run_code_agent_loop

        backend = FakeBackend()
        seen_prompts = []

        def complete(prompt):
            seen_prompts.append(prompt)
            # First program errors; the loop must feed that error into the next prompt.
            if len(seen_prompts) == 1:
                return "```python\nraise ValueError('first boom')\n```"
            return "```python\napi.move_to(1,1)\nlog('ok step')\n```"

        steps = []
        completed = run_code_agent_loop(
            backend.act, backend.observe, complete, cycles=2,
            on_step=lambda i, prog, res: steps.append(res),
        )
        self.assertEqual(completed, 2)
        self.assertFalse(steps[0].ok)
        self.assertTrue(steps[1].ok, steps[1].error)
        # second prompt must contain the first program's error as feedback
        self.assertIn("first boom", seen_prompts[1])

    def test_loop_survives_complete_exception(self):
        from factorio_ai.code_agent import run_code_agent_loop

        backend = FakeBackend()

        def complete(_prompt):
            raise RuntimeError("LLM down")

        results = []
        completed = run_code_agent_loop(
            backend.act, backend.observe, complete, cycles=1,
            on_step=lambda i, prog, res: results.append(res),
        )
        self.assertEqual(completed, 1)
        self.assertFalse(results[0].ok)
        self.assertIn("LLM down", results[0].error)


if __name__ == "__main__":
    unittest.main()
