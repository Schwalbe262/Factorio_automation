"""Pollution in the catalog export: a metrics-only field that keeps every map's catalog fingerprint."""

import hashlib
import json
import unittest

from test_v2_planned_products import catalog

from factorio_agent import layout_variants as variants
from factorio_agent.core.world_catalog import _LUA_EXPORTERS, WorldCatalog


def historical_fingerprint(data):
    """The fingerprint as computed before ``emissions_per_minute`` existed (only force state left out)."""
    data = dict(data)
    for table, fields in (("recipes", ("enabled",)), ("technologies", ("enabled", "researched"))):
        data[table] = {n: {k: v for k, v in row.items() if k not in fields} for n, row in data[table].items()}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


class EmissionsTests(unittest.TestCase):
    def test_the_export_reads_emissions_per_joule_at_full_usage(self):
        lua = _LUA_EXPORTERS["entities"]
        self.assertIn("emissions_per_minute", lua)
        self.assertIn("emissions_per_joule", lua)
        self.assertIn("usage * 3600", lua)

    def test_fingerprint_is_unchanged_with_or_without_emissions(self):
        base = catalog().to_dict()
        plain = WorldCatalog.from_dict(base)
        self.assertEqual(plain.fingerprint, historical_fingerprint(plain.to_dict()))
        rich = json.loads(json.dumps(base))
        rich["entities"]["assembling-machine-1"]["emissions_per_minute"] = {"pollution": 4.0}
        rich["entities"]["inserter"]["emissions_per_minute"] = {"pollution": 0.0}
        self.assertEqual(WorldCatalog.from_dict(rich).fingerprint, plain.fingerprint)
        rich["entities"]["assembling-machine-1"]["crafting_speed"] = 0.75  # a real prototype change still counts
        self.assertNotEqual(WorldCatalog.from_dict(rich).fingerprint, plain.fingerprint)

    def test_layout_metrics_show_pollution_once_exported(self):
        data = catalog().to_dict()
        data["entities"]["assembling-machine-1"]["emissions_per_minute"] = {"pollution": 4.0}
        rich = WorldCatalog.from_dict(data)
        line = {"entities": [{"name": "assembling-machine-1"}, {"name": "assembling-machine-1"}, {"name": "inserter"}]}
        genome = variants.Genome("row", "assembling-machine-1")
        self.assertEqual(variants.energy_metrics(rich, genome, line)["pollution_per_min"], 8.0)
        self.assertIsNone(variants.energy_metrics(catalog(), genome, line)["pollution_per_min"])


if __name__ == "__main__":
    unittest.main()
