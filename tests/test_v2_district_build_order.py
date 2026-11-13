"""The smelting district plan is built producer first: one complete running bank before the next (smelting.producer_first_order)."""

import unittest

from factorio_agent.smelting import merge_entities, producer_first_order


def e(name, x, y=0, **extra):
    return {"name": name, "position": {"x": x, "y": y}, "direction": 0, **extra}


class ProducerFirstOrderTests(unittest.TestCase):
    def setUp(self):
        # banks as the site search lists them: copper first, so the order really changes
        self.copper = {"resource": "copper-ore", "entities": [e("stone-furnace", 100), e("transport-belt", 101)]}
        self.iron = {"resource": "iron-ore", "entities": [e("stone-furnace", 0), e("transport-belt", 1)]}
        self.coal_net = e("splitter", 50)
        self.site = {
            "banks": [self.copper, self.iron],
            "entities": self.copper["entities"] + self.iron["entities"] + [self.coal_net],
        }
        # connections: coal branch per bank, ore route per bank, coal source -> coal input
        self.routes = [
            {"entities": [e("transport-belt", 110)]},  # copper coal branch
            {"entities": [e("transport-belt", 10)]},  # iron coal branch
            {"entities": [e("transport-belt", 120, 5)]},  # copper ore route
            {"entities": [e("transport-belt", 20, 5), e("transport-belt", 1)]},  # iron ore route (shares the port belt)
            {"entities": [e("transport-belt", 60, 9)]},  # coal source route
        ]
        self.powers = [[e("small-electric-pole", 130)], [e("small-electric-pole", 30)]]
        self.connections = [None] * 5
        es = self.site["entities"]
        for route in self.routes:
            es = merge_entities(es, route["entities"])
        for power in self.powers:
            es = merge_entities(es, power)
        self.entities = es

    def order(self):
        return producer_first_order(self.entities, self.site, self.connections, self.routes, self.powers)

    def test_the_iron_bank_runs_before_anything_of_copper_is_built(self):
        ordered = [(r["name"], r["position"]["x"]) for r in self.order()]
        self.assertEqual(
            ordered,
            [
                ("stone-furnace", 0),
                ("transport-belt", 1),
                ("splitter", 50),  # shared coal network
                ("transport-belt", 60),  # coal source route
                ("transport-belt", 10),  # iron coal branch
                ("transport-belt", 20),  # iron ore route
                ("small-electric-pole", 30),  # iron power
                ("stone-furnace", 100),
                ("transport-belt", 101),
                ("transport-belt", 110),
                ("transport-belt", 120),
                ("small-electric-pole", 130),
            ],
        )

    def test_same_entities_and_values(self):
        ordered = self.order()
        key = lambda r: (r["name"], r["position"]["x"], r["position"]["y"])
        self.assertEqual(sorted(map(key, ordered)), sorted(map(key, self.entities)))
        values = {key(r): r for r in self.entities}
        self.assertTrue(all(values[key(r)] is r for r in ordered))

    def test_unexpected_shape_keeps_the_plan_order(self):
        self.assertIs(
            producer_first_order(self.entities, self.site, [None] * 4, self.routes, self.powers), self.entities
        )


if __name__ == "__main__":
    unittest.main()
