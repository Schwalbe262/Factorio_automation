"""The inserter ``produce`` row (three solids, a two-level tree) is a cluster on two planned bus taps."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_v2_green_site import KINDS as WORLD_KINDS
from test_v2_green_site import WORLD, World
from test_v2_planned_furnaces import GEOMETRY, OK
from test_v2_planned_products import SITE, catalog, engine_rows, keepout_route, planned_obs

from factorio_agent.blocks.cluster_audit import layout_findings
from factorio_agent.production_layout import audit_line, is_cluster
from factorio_agent.production_rows import BLOCK_PREFIX
from factorio_agent.transport import xy

OWNER = BLOCK_PREFIX + "inserter"
REQUEST = {"item": "inserter", "rate_per_minute": 30}


class InserterRowTests(unittest.TestCase):
    def prepare(self, world, c, request=REQUEST):
        rows_ = engine_rows(world, c)
        with (
            patch(SITE + "plan_connection", side_effect=keepout_route(world)),
            patch(SITE + "connect_power_groups", return_value={"ok": True, "entities": []}),
            patch(SITE + "preflight", return_value=OK),
        ):
            return rows_, rows_.prepare(planned_obs(world, c), dict(request))

    def test_inserter_row_is_a_cluster_on_iron_and_copper_taps_in_every_orientation(self):
        c = catalog()
        for kind in WORLD_KINDS:
            with self.subTest(world=kind), TemporaryDirectory() as directory:
                world = World(Path(directory), kind)
                _rows, state = self.prepare(world, c)
                self.assertTrue(state.get("ok"), (kind, state.get("reason"), state.get("failures")))
                line = state["line"]
                self.assertTrue(is_cluster(line))
                self.assertEqual(audit_line(c, line), [])
                self.assertEqual(layout_findings(c, line["main"]), [])
                names = [e["name"] for e in line["entities"]]
                machines = [e for e in line["entities"] if e.get("recipe")]
                self.assertEqual(len(machines), 4)  # gear, cable, circuit, inserter
                self.assertNotIn("underground-belt", names)
                self.assertEqual(sorted(t["item"] for t in state["taps"]), ["copper-plate", "iron-plate"])
                self.assertTrue(all(t["owner"] == OWNER for t in state["taps"]))
                allocation = world.site.allocation(state["allocation"], WORLD)
                (x0, y0), (x1, y1) = line["reserved_area"]
                rect = allocation.rect
                self.assertTrue(rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1)
                # Only the taps stand in the bus band.
                bus = world.bus_rect
                tap = {t for tp in state["taps"] for e in tp["entities"] for t in GEOMETRY._occupied_by_plan([e])}
                taken = {t for e in state["plan"]["entities"] for t in GEOMETRY._occupied_by_plan([e])}
                self.assertFalse(any(bus.x0 < x < bus.x1 and bus.y0 < y < bus.y1 for x, y in taken - tap))

    def test_proof_needs_every_cluster_machine_and_the_chest_stock(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            rows_, state = self.prepare(world, c)
            rows_.save("inserter", dict(state, bar_set=True))
            live = []
            for index, e in enumerate(state["plan"]["entities"], 1):
                row = dict(e, unit_number=index, electric_network_connected=True, energy=1, products_finished=2)
                if e["name"] == "iron-chest":
                    row["inventory"] = {"inserter": 3}
                live.append(row)
            by_tile = {xy(r): r for r in live if r["name"] == "underground-belt"}
            for pair in state["plan"]["underground_pairs"]:
                first, last = by_tile[tuple(pair["input"])], by_tile[tuple(pair["output"])]
                first.update(underground_pair_verified=True, underground_neighbour={"unit_number": last["unit_number"]})
                last["underground_pair_verified"] = True
            obs = dict(planned_obs(world, c), entities=live)
            proof = rows_.proof(obs, dict(REQUEST))
            self.assertIsNotNone(proof, rows_.last_proof_failure)
            self.assertEqual(len(proof["machine_units"]), 4)
            cable = next(e for e in state["plan"]["entities"] if e.get("recipe") == "copper-cable")
            idle = [dict(r, products_finished=0) if xy(r) == xy(cable) and r.get("recipe") else r for r in live]
            self.assertIsNone(rows_.proof(dict(obs, entities=idle), dict(REQUEST)))
            self.assertEqual(rows_.last_proof_failure["reason"], "machine_has_not_produced")

    def test_a_higher_rate_blocks_instead_of_rerouting_the_built_cluster(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            rows_, state = self.prepare(world, c, {"item": "inserter", "rate_per_minute": 10})
            rows_.save("inserter", state)
            before = list(state["plan"]["entities"])
            result = rows_._extend(planned_obs(world, c), rows_.state("inserter"), 600)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(rows_.state("inserter")["plan"]["entities"], before)


if __name__ == "__main__":
    unittest.main()
