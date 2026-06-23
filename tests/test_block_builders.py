import unittest

from factorio_ai.block_builders import (
    BuilderResult,
    SUPPORTED_BLOCK_BUILDERS,
    block_builder_capabilities,
    validate_build_block_action,
)


class BlockBuilderContractTests(unittest.TestCase):
    def test_builder_result_exports_common_contract_fields(self):
        result = BuilderResult.pending("direct_feed_smelter_set").to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["builder"], "direct_feed_smelter_set")
        for field in (
            "placed",
            "reused",
            "failed",
            "outputs",
            "warnings",
            "failure_root",
            "repair_skill",
            "diagnostics",
        ):
            self.assertIn(field, result)
        self.assertEqual(result["failure_root"], "builder_not_implemented")
        self.assertEqual(result["repair_skill"], "diagnose_factory")
        self.assertIsInstance(result["warnings"], list)

    def test_build_block_action_validation_accepts_supported_no_mod_builders(self):
        for builder in SUPPORTED_BLOCK_BUILDERS:
            validate_build_block_action(
                {
                    "type": "build_block",
                    "builder": builder,
                    "params": {"radius": 96},
                    "mode": "no_mod",
                }
            )

    def test_build_block_action_validation_rejects_unsafe_shapes(self):
        bad_actions = [
            {"type": "build", "builder": "direct_feed_smelter_set"},
            {"type": "build_block", "builder": "rocket_silo_direct_create"},
            {"type": "build_block", "builder": "direct_feed_smelter_set", "params": []},
            {"type": "build_block", "builder": "direct_feed_smelter_set", "mode": "experimental_mod"},
        ]

        for action in bad_actions:
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    validate_build_block_action(action)

    def test_capabilities_mark_only_read_only_no_mod_builders_implemented_for_substrate(self):
        capabilities = block_builder_capabilities()
        by_name = {item["builder"]: item for item in capabilities}

        self.assertEqual(set(by_name), set(SUPPORTED_BLOCK_BUILDERS))
        self.assertTrue(by_name["factory_map"]["implemented"])
        self.assertTrue(by_name["diagnose_factory"]["implemented"])
        self.assertFalse(by_name["direct_feed_smelter_set"]["implemented"])


if __name__ == "__main__":
    unittest.main()
