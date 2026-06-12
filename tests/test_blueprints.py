import unittest

from factorio_ai.blueprints import (
    BlueprintDecodeError,
    decode_blueprint_string,
    encode_blueprint_payload,
    infer_blueprint_lessons,
    summarize_blueprint_payload,
    training_examples_from_blueprint,
)


class BlueprintTests(unittest.TestCase):
    def test_decodes_and_summarizes_blueprint_exchange_string(self):
        payload = {
            "blueprint": {
                "label": "starter belts",
                "entities": [
                    {"entity_number": 1, "name": "transport-belt", "position": {"x": 0, "y": 0}},
                    {"entity_number": 2, "name": "transport-belt", "position": {"x": 1, "y": 0}},
                    {"entity_number": 3, "name": "inserter", "position": {"x": 2, "y": 0}},
                ],
            }
        }
        decoded = decode_blueprint_string(encode_blueprint_payload(payload))
        summaries = summarize_blueprint_payload(decoded)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].label, "starter belts")
        self.assertEqual(summaries[0].entity_counts["transport-belt"], 2)
        self.assertEqual(summaries[0].entity_counts["inserter"], 1)

    def test_rejects_invalid_blueprint_string(self):
        with self.assertRaises(BlueprintDecodeError):
            decode_blueprint_string("not-a-blueprint")

    def test_infers_smelting_lesson_and_training_example(self):
        payload = {
            "blueprint": {
                "label": "iron smelting",
                "entities": [
                    {"entity_number": 1, "name": "stone-furnace", "position": {"x": 0, "y": 0}},
                    {"entity_number": 2, "name": "stone-furnace", "position": {"x": 2, "y": 0}},
                    {"entity_number": 3, "name": "inserter", "position": {"x": 1, "y": 0}},
                    {"entity_number": 4, "name": "transport-belt", "position": {"x": 1, "y": 1}},
                ],
            }
        }
        lessons = infer_blueprint_lessons(payload)
        self.assertEqual(lessons[0].inferred_purpose, "smelting block")
        self.assertIn("ore input", lessons[0].bottlenecks)

        examples = training_examples_from_blueprint(payload, objective="expand iron smelting")
        self.assertEqual(examples[0]["messages"][0]["role"], "system")
        self.assertIn("smelting block", examples[0]["messages"][2]["content"])


if __name__ == "__main__":
    unittest.main()
