from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


TARGET_ITEMS = [
    "iron-plate",
    "copper-plate",
    "steel-plate",
    "iron-gear-wheel",
    "copper-cable",
    "electronic-circuit",
    "automation-science-pack",
    "logistic-science-pack",
    "advanced-circuit",
    "processing-unit",
    "plastic-bar",
    "sulfuric-acid",
    "rocket-fuel",
    "low-density-structure",
    "rocket-part",
]


DEFAULT_TARGETS_BY_OBJECTIVE: dict[str, dict[str, float]] = {
    "launch_rocket_program": {
        "iron-plate": 60.0,
        "copper-plate": 45.0,
        "steel-plate": 10.0,
        "electronic-circuit": 30.0,
        "automation-science-pack": 10.0,
    },
    "produce_automation_science_pack": {
        "iron-plate": 20.0,
        "copper-plate": 10.0,
        "automation-science-pack": 5.0,
    },
}


@dataclass(frozen=True)
class ProductionTargets:
    per_minute: dict[str, float]
    source: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return {"per_minute": dict(sorted(self.per_minute.items())), "source": self.source}


def target_path(runtime_dir: Path) -> Path:
    return runtime_dir / "production-targets.json"


def load_targets(runtime_dir: Path, objective: str = "launch_rocket_program") -> ProductionTargets:
    path = target_path(runtime_dir)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            per_minute = data.get("per_minute")
            if isinstance(per_minute, dict):
                return ProductionTargets(_sanitize_targets(per_minute), source=str(data.get("source") or "user"))
    return ProductionTargets(default_targets_for_objective(objective), source="objective_default")


def save_targets(runtime_dir: Path, targets: ProductionTargets) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = target_path(runtime_dir)
    path.write_text(json.dumps(targets.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def default_targets_for_objective(objective: str) -> dict[str, float]:
    objective_lower = objective.lower()
    if "rocket" in objective_lower:
        return dict(DEFAULT_TARGETS_BY_OBJECTIVE["launch_rocket_program"])
    if "automation" in objective_lower or "science" in objective_lower:
        return dict(DEFAULT_TARGETS_BY_OBJECTIVE["produce_automation_science_pack"])
    return dict(DEFAULT_TARGETS_BY_OBJECTIVE["launch_rocket_program"])


def parse_target_form(values: dict[str, list[str]]) -> ProductionTargets:
    output: dict[str, float] = {}
    for item in TARGET_ITEMS:
        raw_values = values.get(item) or values.get(f"target:{item}") or []
        raw = raw_values[0] if raw_values else ""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            output[item] = round(value, 3)
    return ProductionTargets(output, source="user")


def _sanitize_targets(raw: dict[Any, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for key, value in raw.items():
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            output[str(key)] = round(parsed, 3)
    return output
