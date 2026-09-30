"""Saved world policy. A legacy save never implicitly switches planners."""
import json
from pathlib import Path

from .deterministic_state import _atomic_json


def resolve_layout_policy(root: Path, requested: str | None, *, new_world: bool) -> str:
    path = root / "layout-policy.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if saved is not None:
        if saved.get("schema_version") != 1 or saved.get("policy") not in {"legacy", "arrays-v2"}:
            raise ValueError("unsupported saved layout policy")
        if requested is not None and requested != saved["policy"]:
            raise ValueError("layout policy cannot be changed on a saved world")
        return saved["policy"]
    if requested not in {None, "legacy", "arrays-v2"}:
        raise ValueError("unknown layout policy")
    if not new_world and requested == "arrays-v2":
        raise ValueError("arrays-v2 requires a new isolated world; existing saves retain legacy")
    return requested or ("arrays-v2" if new_world else "legacy")


def save_layout_policy(root: Path, policy: str) -> None:
    path = root / "layout-policy.json"
    if path.exists():
        resolve_layout_policy(root, policy, new_world=False)
        return
    _atomic_json(path, {"schema_version": 1, "policy": policy})
