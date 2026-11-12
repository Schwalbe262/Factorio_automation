from types import SimpleNamespace
import unittest

from factorio_agent import research_policy as rp
from factorio_agent.upgrades import PRIORITIES

RED = {"name": "automation-science-pack", "amount": 1}
GREEN = {"name": "logistic-science-pack", "amount": 1}


def tech(prerequisites=(), ingredients=(RED,), unit_count=10, unlocks=(), **extra):
    row = {
        "prerequisites": list(prerequisites),
        "ingredients": list(ingredients),
        "unit_count": unit_count,
        "unlocks": list(unlocks),
    }
    row.update(extra)
    return row


def catalog(**technologies):
    return SimpleNamespace(technologies={name.replace("_", "-"): row for name, row in technologies.items()})


def diamond():
    # root -> left, right -> top
    return catalog(
        root=tech(unit_count=10),
        left=tech(["root"], unit_count=20),
        right=tech(["root"], unit_count=30),
        top=tech(["left", "right"], [RED, GREEN], 50, unlocks=["top-recipe"]),
    )


class UnlockPathTests(unittest.TestCase):
    def test_diamond_orders_prerequisites_first_and_ends_with_target(self):
        path = rp.unlock_path(diamond(), {}, "top")
        self.assertEqual(path.lab_order, ("root", "left", "right", "top"))
        self.assertEqual(path.trigger_technologies, ())

    def test_researched_technologies_are_skipped(self):
        path = rp.unlock_path(diamond(), {"root": True, "left": True, "right": False}, "top")
        self.assertEqual(path.lab_order, ("right", "top"))

    def test_accepts_a_set_of_names(self):
        path = rp.unlock_path(diamond(), {"root", "left", "right"}, "top")
        self.assertEqual(path.lab_order, ("top",))

    def test_researched_target_gives_empty_complete_path(self):
        path = rp.unlock_path(diamond(), {"top"}, "top")
        self.assertTrue(path.complete)

    def test_unknown_technology_raises(self):
        with self.assertRaises(ValueError):
            rp.unlock_path(diamond(), {}, "missing")
        cat = catalog(a=tech(["ghost"]))
        with self.assertRaises(ValueError):
            rp.unlock_path(cat, {}, "a")

    def test_trigger_technologies_reported_separately(self):
        cat = catalog(
            base=tech(),
            oil_processing=tech(["base"], [], None, research_trigger={"type": "mine-entity", "entity": "crude-oil"}),
            plastics=tech(["oil-processing"], [RED, GREEN], 100),
        )
        path = rp.unlock_path(cat, {}, "plastics")
        self.assertEqual(path.lab_order, ("base", "plastics"))
        self.assertEqual(path.trigger_technologies, ("oil-processing",))


class RecipeMappingTests(unittest.TestCase):
    def test_technologies_for_recipes(self):
        cat = catalog(
            a=tech(unlocks=["x", "y"]),
            b=tech(unlocks=["z"]),
            c=tech(unlocks=["y"]),
        )
        self.assertEqual(rp.technologies_for_recipes(cat, ["y"]), {"a", "c"})
        self.assertEqual(rp.technologies_for_recipes(cat, ["z", "x"]), {"a", "b"})
        self.assertEqual(rp.technologies_for_recipes(cat, ["nothing"]), set())


class ChooseResearchTests(unittest.TestCase):
    def test_none_while_research_running(self):
        self.assertIsNone(rp.choose_research(diamond(), {"technologies": {}, "research": "root"}))

    def test_none_when_nothing_researchable(self):
        cat = catalog(a=tech(["b"]), b=tech(enabled=False))
        self.assertIsNone(rp.choose_research(cat, {"technologies": {}, "research": None}))

    def test_wanted_recipe_picks_first_researchable_path_element(self):
        obs = {"technologies": {"root": True, "left": True}, "research": None}
        choice = rp.choose_research(diamond(), obs, wanted_recipes=["top-recipe"])
        self.assertEqual(choice, rp.ResearchChoice("right", rp.REASON_WANTED, "top"))

    def test_already_enabled_wanted_recipe_is_ignored(self):
        obs = {"technologies": {}, "research": None, "enabled_recipes": {"top-recipe": True}}
        choice = rp.choose_research(diamond(), obs, wanted_recipes=["top-recipe"])
        self.assertEqual(choice.reason, rp.REASON_FALLBACK)

    def test_recipe_with_two_unlockers_prefers_shorter_path(self):
        cat = catalog(
            base=tech(),
            long=tech(["base"], unlocks=["r"]),
            short=tech(unit_count=999, unlocks=["r"]),
        )
        choice = rp.choose_research(cat, {"technologies": {}}, wanted_recipes=["r"])
        self.assertEqual((choice.technology, choice.target), ("short", "short"))

    def test_ranking_wanted_over_preference_over_fallback(self):
        cat = catalog(
            cheap=tech(unit_count=1),
            preferred=tech(unit_count=50),
            wanted=tech(unit_count=100),
        )
        obs = {"technologies": {}, "research": None}
        full = rp.choose_research(cat, obs, wanted_technologies=["wanted"], preferences=["preferred"])
        self.assertEqual(full.reason, rp.REASON_WANTED)
        self.assertEqual(full.technology, "wanted")
        pref = rp.choose_research(cat, obs, preferences=["preferred"])
        self.assertEqual((pref.technology, pref.reason), ("preferred", rp.REASON_PREFERENCE))
        fallback = rp.choose_research(cat, obs)
        self.assertEqual((fallback.technology, fallback.reason), ("cheap", rp.REASON_FALLBACK))

    def test_blocked_wanted_falls_through_to_preference(self):
        cat = catalog(
            gated=tech(ingredients=[RED, GREEN]),
            preferred=tech(unit_count=50),
            cheap=tech(unit_count=1),
        )
        obs = {"technologies": {}}
        choice = rp.choose_research(
            cat,
            obs,
            wanted_technologies=["gated"],
            preferences=["unknown-name", "preferred"],
            supplied_packs=["automation-science-pack"],
        )
        self.assertEqual((choice.technology, choice.reason), ("preferred", rp.REASON_PREFERENCE))

    def test_preference_uses_path_logic(self):
        obs = {"technologies": {"root": True}}
        choice = rp.choose_research(diamond(), obs, preferences=["top"])
        self.assertEqual(choice, rp.ResearchChoice("left", rp.REASON_PREFERENCE, "top"))

    def test_unknown_wanted_technology_raises(self):
        with self.assertRaises(ValueError):
            rp.choose_research(diamond(), {"technologies": {}}, wanted_technologies=["ghost"])

    def test_fallback_cheapest_by_units_times_packs(self):
        cat = catalog(
            two_packs=tech(ingredients=[RED, GREEN], unit_count=10),  # cost 20
            one_pack=tech(unit_count=15),  # cost 15
        )
        self.assertEqual(rp.choose_research(cat, {"technologies": {}}).technology, "one-pack")

    def test_fallback_ties_break_by_name(self):
        cat = catalog(zeta=tech(unit_count=10), alpha=tech(unit_count=10), mid=tech(unit_count=10))
        self.assertEqual(rp.choose_research(cat, {"technologies": {}}).technology, "alpha")

    def test_infinite_technology_skipped_unless_only_option(self):
        cat = catalog(
            aaa_infinite=tech(unit_count=None, count_formula="2^(L-1)*1000"),
            finite=tech(unit_count=5000),
        )
        self.assertEqual(rp.choose_research(cat, {"technologies": {}}).technology, "finite")
        obs = {"technologies": {"finite": True}}
        self.assertEqual(rp.choose_research(cat, obs).technology, "aaa-infinite")

    def test_supplied_packs_filter_researchable_set(self):
        cat = catalog(green_only=tech(ingredients=[RED, GREEN], unit_count=1), red=tech(unit_count=100))
        obs = {"technologies": {}}
        self.assertEqual(rp.choose_research(cat, obs).technology, "green-only")
        limited = rp.choose_research(cat, obs, supplied_packs={"automation-science-pack"})
        self.assertEqual(limited.technology, "red")
        self.assertIsNone(rp.choose_research(cat, obs, supplied_packs=set()))

    def test_researchable_now_matches_ready_options(self):
        from factorio_agent.normal_research import ready_options

        cat = diamond()
        obs = {"technologies": {"root": True}}
        expected = {row["technology"] for row in ready_options(cat, obs)["technologies"]}
        self.assertEqual(rp.researchable_now(cat, obs), expected)


class BlockerTests(unittest.TestCase):
    def test_missing_pack_supply_reported(self):
        obs = {"technologies": {"root": True, "left": True, "right": True}}
        blockers = rp.research_blockers(
            diamond(), obs, wanted_technologies=["top"], supplied_packs=["automation-science-pack"]
        )
        self.assertEqual(
            blockers,
            [rp.ResearchBlocker("top", "top", rp.BLOCKER_PACK_SUPPLY, ("logistic-science-pack",))],
        )

    def test_no_blockers_when_path_is_progressable(self):
        blockers = rp.research_blockers(diamond(), {"technologies": {}}, wanted_recipes=["top-recipe"])
        self.assertEqual(blockers, [])

    def test_trigger_and_prerequisite_blockers(self):
        trigger = {"type": "mine-entity", "entity": "crude-oil"}
        cat = catalog(
            oil=tech([], [], None, research_trigger=trigger),
            plastics=tech(["oil"], unlocks=["plastic-bar"]),
        )
        blockers = rp.research_blockers(cat, {"technologies": {}}, wanted_recipes=["plastic-bar"])
        self.assertEqual(
            blockers,
            [
                rp.ResearchBlocker("plastics", "oil", rp.BLOCKER_TRIGGER, trigger=trigger),
                rp.ResearchBlocker("plastics", "plastics", rp.BLOCKER_PREREQUISITE, ("oil",)),
            ],
        )

    def test_recipe_without_unlocker_and_disabled_technology(self):
        cat = catalog(off=tech(enabled=False))
        blockers = rp.research_blockers(
            cat, {"technologies": {}}, wanted_recipes=["orphan"], wanted_technologies=["off"]
        )
        self.assertEqual(
            [(b.kind, b.technology) for b in blockers],
            [(rp.BLOCKER_NO_UNLOCKER, None), (rp.BLOCKER_DISABLED, "off")],
        )


class CompatibilityTests(unittest.TestCase):
    def test_legacy_preferences_match_upgrade_priorities(self):
        self.assertEqual(rp.legacy_upgrade_preferences(), tuple(PRIORITIES))
        cat = catalog(stone_wall=tech(), radar=tech(unit_count=1))
        choice = rp.choose_research(cat, {"technologies": {}}, preferences=rp.legacy_upgrade_preferences())
        self.assertEqual((choice.technology, choice.reason), ("stone-wall", rp.REASON_PREFERENCE))


if __name__ == "__main__":
    unittest.main()
