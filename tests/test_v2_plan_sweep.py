"""Offline planner sweep (factorio_agent.plan_sweep): a few seeds, failure counts pinned as upper bounds.

The bounds are the CURRENT failures of these seeds. A planner change that fails more seeds
fails this test; an improvement must lower the bound here (python -B tools/plan_sweep.py
--seeds <SEEDS> prints the counts).
"""

import json
import unittest

from factorio_agent import plan_sweep as ps

FULL = (3,)  # the whole pipeline (about 5 s per seed)
EARLY = (0, 1, 2)  # master plan and mining banks only (about 1 s per seed)
# Upper bounds of failed seeds per stage (stages not listed: 0).
BOUNDS = {}

_ROWS = None


def rows():
    global _ROWS
    if _ROWS is None:
        _ROWS = ps.sweep(FULL) + ps.sweep(EARLY, upto="mine")
    return _ROWS


class PlanSweepGateTests(unittest.TestCase):
    def test_failure_counts_do_not_grow(self):
        counts = ps.failure_counts(rows())
        for stage, failed in counts.items():
            with self.subTest(stage=stage):
                self.assertLessEqual(failed, BOUNDS.get(stage, 0), [r["stages"].get(stage) for r in rows()])

    def test_every_stage_ran_or_was_skipped_for_a_named_reason(self):
        for row in rows()[: len(FULL)]:
            for stage in ps._order():
                self.assertIn(stage, row["stages"], (row["seed"], stage))
        for row in rows():
            for entry in row["stages"].values():
                if entry["ok"] is None:
                    self.assertTrue(entry["reason"].startswith("skipped: "))
                elif not entry["ok"]:
                    self.assertTrue(entry["reason"])

    def test_same_seed_same_row(self):
        def plain(row):
            row = json.loads(json.dumps({k: v for k, v in row.items() if k not in ("seconds", "_sweep")}))
            for entry in row["stages"].values():
                entry.pop("seconds", None)
            return row

        first = next(r for r in rows() if r["seed"] == EARLY[1] and "smelting_district" not in r["stages"])
        self.assertEqual(plain(first), plain(ps.run_seed(EARLY[1], upto="mine")))

    def test_report_names_the_coverage_gaps(self):
        text = ps.report(ps.aggregate(rows()))
        self.assertIn("not offline-capable", text)
        self.assertIn("tidiness per seed", text)

    def test_block_list_matches_the_core_builders(self):
        names = tuple(name for name, _spec in ps._core_builders(ps.load_catalog()))
        self.assertEqual(names, ps._CORE_NAMES)

    def test_offline_game_answers_unknown_queries_as_unsupported(self):
        game = ps.OfflineGame(".", None, ps._builder())
        self.assertEqual(game.query("return {ok=true,foo=s.bar}")["reason"], "offline_query_unsupported")
        self.assertEqual(sum(game.unsupported.values()), 1)
        self.assertEqual(game.query(ps_max_span())["max_span"], ps.MAX_SPAN)


def ps_max_span():
    from factorio_agent.planned_tap import MAX_SPAN_LUA

    return MAX_SPAN_LUA


if __name__ == "__main__":
    unittest.main()
