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
)
SUPPORTED_BLOCK_BUILDER_SET = frozenset(SUPPORTED_BLOCK_BUILDERS)
SUPPORTED_BUILD_BLOCK_MODES = frozenset({"no_mod"})
IMPLEMENTED_NO_MOD_BUILDERS = frozenset(
    {"direct_feed_smelter_set", "coal_bootstrap_cluster", "factory_map", "diagnose_factory"}
)


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
        }
        for builder in SUPPORTED_BLOCK_BUILDERS
    ]
