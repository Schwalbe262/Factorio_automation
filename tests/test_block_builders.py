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

    def test_capabilities_mark_no_mod_builders_implemented_by_part(self):
        capabilities = block_builder_capabilities()
        by_name = {item["builder"]: item for item in capabilities}

        self.assertEqual(set(by_name), set(SUPPORTED_BLOCK_BUILDERS))
        self.assertTrue(by_name["direct_feed_smelter_set"]["implemented"])
        self.assertTrue(by_name["coal_bootstrap_cluster"]["implemented"])
        self.assertTrue(by_name["factory_map"]["implemented"])
        self.assertTrue(by_name["diagnose_factory"]["implemented"])
        self.assertTrue(by_name["trace_belt_flow"]["implemented"])
        self.assertTrue(by_name["validate_route_policy"]["implemented"])
        self.assertFalse(by_name["steam_bank"]["implemented"])
        self.assertEqual(by_name["steam_bank"]["contract"]["completion"], "generating_engines_verified")
        self.assertIn("dedicated_coal_feed", by_name["steam_bank"]["contract"]["requires"])
        self.assertEqual(by_name["feed_smelter_block"]["contract"]["repair_skill"], "trace_belt_flow")


if __name__ == "__main__":
    unittest.main()
