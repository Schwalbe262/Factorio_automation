from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .config import AppConfig


ITEM_ICON_FALLBACK = "unknown"


def resolve_item_icon(cfg: AppConfig, item_name: str) -> Path | None:
    factorio_root = cfg.factorio_exe.parent.parent.parent
    return _resolve_icon(str(factorio_root), _safe_item_name(item_name))


@lru_cache(maxsize=512)
def _resolve_icon(factorio_root: str, item_name: str) -> Path | None:
    if not item_name:
        return None

    root = Path(factorio_root)
    data_dir = root / "data"
    candidates = [
        data_dir / "base" / "graphics" / "icons" / f"{item_name}.png",
        data_dir / "base" / "graphics" / "icons" / "fluid" / f"{item_name}.png",
        data_dir / "space-age" / "graphics" / "icons" / f"{item_name}.png",
        data_dir / "space-age" / "graphics" / "icons" / "fluid" / f"{item_name}.png",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    if data_dir.exists():
        matches = list(data_dir.rglob(f"{item_name}.png"))
        for match in matches:
            if "\\graphics\\icons\\" in str(match).lower() or "/graphics/icons/" in str(match).lower():
                return match
        if matches:
            return matches[0]

    if item_name != ITEM_ICON_FALLBACK:
        return _resolve_icon(factorio_root, ITEM_ICON_FALLBACK)
    return None


def _safe_item_name(value: str) -> str:
    return "".join(character for character in value.strip().lower() if character.isalnum() or character in {"-", "_"})
