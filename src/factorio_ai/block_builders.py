from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


SUPPORTED_BLOCK_BUILDERS: tuple[str, ...] = (
    "direct_feed_smelter_set",
    "coal_bootstrap_cluster",
    "steam_bank",
    "mining_array",
    "smelter_block",
    "feed_smelter_block",
    "main_bus",
    "assembly_line",
    "labs_row",
    "factory_map",
    "diagnose_factory",
    "repair_factory",
    "trace_belt_flow",
    "validate_route_policy",
)
SUPPORTED_BLOCK_BUILDER_SET = frozenset(SUPPORTED_BLOCK_BUILDERS)
SUPPORTED_BUILD_BLOCK_MODES = frozenset({"no_mod"})
IMPLEMENTED_NO_MOD_BUILDERS = frozenset(
    {
        "direct_feed_smelter_set",
        "coal_bootstrap_cluster",
        "factory_map",
        "diagnose_factory",
        "trace_belt_flow",
        "validate_route_policy",
    }
)

BUILDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "steam_bank": {
        "completion": "generating_engines_verified",
        "repair_skill": "setup_power",
        "requires": ["water_port", "dedicated_coal_feed"],
    },
    "mining_array": {
        "completion": "collector_belt_flow_verified",
        "repair_skill": "setup_power",
        "requires": ["energized_grid", "resource_patch"],
    },
    "smelter_block": {
        "completion": "diagnose_then_plate_output_flow",
        "repair_skill": "feed_smelter_block",
        "requires": ["ore_feed", "dedicated_coal_feed", "energized_grid"],
    },
    "feed_smelter_block": {
        "completion": "ore_and_coal_lane_flow_verified",
        "repair_skill": "trace_belt_flow",
        "requires": ["ore_source_belt", "dedicated_coal_source_belt", "open_staging_corridor"],
    },
    "main_bus": {
        "completion": "lane_terminals_preserved",
        "repair_skill": "validate_route_policy",
        "requires": ["clear_corridor", "material_sources"],
    },
    "assembly_line": {
        "completion": "recipe_inputs_and_output_flow_verified",
        "repair_skill": "build_site_input_logistic_line",
        "requires": ["recipe_unlocked", "item_sources", "energized_grid"],
    },
    "labs_row": {
        "completion": "science_pack_flow_verified",
        "repair_skill": "build_site_input_logistic_line",
        "requires": ["lab_unlocked", "science_pack_sources", "energized_grid"],
    },
}


@dataclass(frozen=True)
class BuilderResult:
    ok: bool
    builder: str
    placed: int = 0
    reused: int = 0
    failed: int = 0
    outputs: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    failure_root: str | None = None
    repair_skill: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outputs"] = dict(self.outputs)
        data["warnings"] = list(self.warnings)
        data["diagnostics"] = dict(self.diagnostics)
        return data

    @classmethod
    def pending(cls, builder: str, *, repair_skill: str = "diagnose_factory") -> "BuilderResult":
        validate_builder_name(builder)
        return cls(
            ok=False,
            builder=builder,
            failed=1,
            warnings=("builder substrate is registered but concrete placement is not implemented yet",),
            failure_root="builder_not_implemented",
            repair_skill=repair_skill,
        )


def validate_builder_name(builder: Any) -> str:
    if not isinstance(builder, str) or not builder:
        raise ValueError("build_block requires a non-empty builder")
    if builder not in SUPPORTED_BLOCK_BUILDER_SET:
        raise ValueError(f"unsupported build_block builder: {builder}")
    return builder


def validate_build_block_action(action: Mapping[str, Any]) -> None:
    if action.get("type") != "build_block":
        raise ValueError("validate_build_block_action requires type=build_block")
    validate_builder_name(action.get("builder"))
    params = action.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ValueError("build_block params must be an object")
    mode = str(action.get("mode") or "no_mod")
    if mode not in SUPPORTED_BUILD_BLOCK_MODES:
        raise ValueError(f"unsupported build_block mode for no-mod controller: {mode}")


def block_builder_capabilities(*, mode: str = "no_mod") -> list[dict[str, Any]]:
    if mode not in SUPPORTED_BUILD_BLOCK_MODES:
        raise ValueError(f"unsupported build_block mode: {mode}")
    return [
        {
            "builder": builder,
            "mode": mode,
            "implemented": builder in IMPLEMENTED_NO_MOD_BUILDERS,
            "contract": BUILDER_CONTRACTS.get(builder, {}),
        }
        for builder in SUPPORTED_BLOCK_BUILDERS
    ]
