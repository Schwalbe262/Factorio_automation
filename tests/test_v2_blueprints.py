import base64
import json
import unittest
import zlib
from pathlib import Path

from factorio_agent.blueprints import BlueprintDecodeError, decode_blueprint_string, summarize_blueprint


class BlueprintReferenceTests(unittest.TestCase):
    def test_decodes_public_reference_and_summarizes_with_catalog(self):
        path = Path("runtime/reference-imports/smelting-station.txt")
        if not path.exists():
            self.skipTest("ignored public reference asset is not present")
        catalog = json.loads(Path("runtime/v2/dev-101/catalog.json").read_text(encoding="utf-8"))
        summary = summarize_blueprint(decode_blueprint_string(path.read_text()), catalog)
        self.assertEqual(len(summary), 1)
        self.assertGreater(summary[0]["entity_total"], 0)
        self.assertEqual(summary[0]["verification"], "UNVERIFIED_STRUCTURE_ONLY")

    def test_decodes_early_automation_science_reference_and_recipes(self):
        path = Path("runtime/reference-imports/automation-science-am1.txt")
        if not path.exists():
            self.skipTest("ignored public reference asset is not present")
        payload = decode_blueprint_string(path.read_text(encoding="utf-8"))
        bp = payload["blueprint"]
        self.assertEqual(bp["label"], "Automation Science AM1")
        self.assertEqual(sum(e["name"] == "assembling-machine-1" for e in bp["entities"]), 6)
        self.assertEqual(sum(e["name"] == "lab" for e in bp["entities"]), 30)
        self.assertEqual(
            {e["recipe"] for e in bp["entities"] if e.get("recipe")},
            {"automation-science-pack", "iron-gear-wheel"},
        )
        catalog = json.loads(Path("runtime/v2/dev-101/catalog.json").read_text(encoding="utf-8"))
        summary = summarize_blueprint(payload, catalog)[0]
        self.assertEqual(summary["catalog_unknown_entities"], [])
        self.assertEqual(summary["verification"], "UNVERIFIED_STRUCTURE_ONLY")

    def test_flattens_nested_books(self):
        payload = {"blueprint_book": {"blueprints": [
            {"index": 0, "blueprint": {"label": "a", "entities": [{"name": "transport-belt"}]}},
            {"index": 1, "blueprint_book": {"blueprints": [
                {"blueprint": {"label": "b", "entities": []}}
            ]}},
        ]}}
        summaries = summarize_blueprint(payload)
        self.assertEqual([x["label"] for x in summaries], ["a", "b"])

    def test_rejects_bad_prefix_base64_and_truncated_zlib(self):
        for value in ("", "bad", "0%%%", "0" + base64.b64encode(b"not zlib").decode()):
            with self.subTest(value=value), self.assertRaises(BlueprintDecodeError):
                decode_blueprint_string(value)

    def test_rejects_decompression_bomb(self):
        bomb = zlib.compress(b" " * (8_000_001))
        value = "0" + base64.b64encode(bomb).decode()
        with self.assertRaisesRegex(BlueprintDecodeError, "decompressed.*limit"):
            decode_blueprint_string(value)

    def test_rejects_entity_count_over_limit(self):
        entities = [{"name": "belt"}] * 20_001
        payload = {"blueprint": {"entities": entities}}
        packed = "0" + base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode()
        with self.assertRaisesRegex(BlueprintDecodeError, "entity count"):
            decode_blueprint_string(packed)

    def test_rejects_non_blueprint_and_deep_json_without_unbounded_recursion(self):
        for raw in (b'{}', b'['*2000+b'0'+b']'*2000):
            packed='0'+base64.b64encode(zlib.compress(raw)).decode()
            with self.assertRaises(BlueprintDecodeError):
                decode_blueprint_string(packed)


if __name__ == "__main__":
    unittest.main()
