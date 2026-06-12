import tempfile
import unittest
from pathlib import Path

from factorio_ai.targets import ProductionTargets, load_targets, parse_target_form, save_targets


class TargetTests(unittest.TestCase):
    def test_parse_target_form_keeps_positive_values(self):
        targets = parse_target_form({"iron-plate": ["30"], "copper-plate": ["0"], "steel-plate": ["bad"]})
        self.assertEqual(targets.per_minute, {"iron-plate": 30.0})

    def test_save_and_load_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            save_targets(runtime, ProductionTargets({"iron-plate": 42.0}))
            loaded = load_targets(runtime)
            self.assertEqual(loaded.per_minute["iron-plate"], 42.0)
            self.assertEqual(loaded.source, "user")


if __name__ == "__main__":
    unittest.main()
