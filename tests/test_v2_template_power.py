"""Science block templates must wire their own poles together.

The green core is exempt: ``manufacturing.connect_power_groups`` links its groups.

Live seed 1400895309: the red science gear cell had a pole 10 tiles from the
row poles (small-pole wire reach is 7.5), so it stayed unpowered and the job
ran into its deadline with everything built.
"""

import math
import unittest

from factorio_agent.science import lab_row, science_row

WIRE_REACH = 7.5


def pole_groups(entities):
    poles = [(e["position"]["x"], e["position"]["y"]) for e in entities if e["name"] == "small-electric-pole"]
    groups = []
    for pole in poles:
        joined = [g for g in groups if any(math.dist(pole, other) <= WIRE_REACH for other in g)]
        merged = {pole}.union(*joined) if joined else {pole}
        groups = [g for g in groups if g not in joined] + [merged]
    return groups


class TemplatePowerTests(unittest.TestCase):
    def test_science_row_poles_form_one_network(self):
        for count in range(1, 7):
            self.assertEqual(len(pole_groups(science_row(8.5, 22.5, count)["entities"])), 1, count)

    def test_lab_row_poles_form_one_network(self):
        self.assertEqual(len(pole_groups(lab_row(1.5, 8.5)["entities"])), 1)

    def test_staged_subsets_stay_one_network(self):
        """A staged block builds a prefix of slots (science_growth): 2 of 8 labs, 1..6 machines."""
        from factorio_agent.science_growth import lab_slots, machine_slots

        labs = lab_row(1.5, 8.5)
        for built in range(1, 9):
            rest = [e for slot in lab_slots(labs)[built:] for e in slot]
            entities = [e for e in labs["entities"] if e not in rest]
            self.assertEqual(len(pole_groups(entities)), 1, built)
        for count in range(1, 7):
            row = science_row(8.5, 22.5, count)
            for built in range(1, count + 1):
                rest = [e for slot in machine_slots(row)[built:] for e in slot]
                entities = [e for e in row["entities"] if e not in rest]
                self.assertEqual(len(pole_groups(entities)), 1, (count, built))


if __name__ == "__main__":
    unittest.main()
