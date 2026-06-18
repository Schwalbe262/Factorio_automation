"""Persistent library of compiled+validated optimal cell layouts (constraint C8).

Each design is saved with its spec (item/rate, machines, modules, belt tiers, power, footprint),
a one-line description, and the blueprint exchange string so the dashboard can show specs and a
copy-blueprint button. Designs are keyed by (item, rate, machine, modules) so the same cell is
validated/stored once and reused (amortising the expensive sandbox run, see cell_flow_check).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .cell_compiler import CellSpec

LIBRARY_DIRNAME = "cell-library"
INDEX_NAME = "index.jsonl"


def _library_dir(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / LIBRARY_DIRNAME


def _format_added_at(created_iso: str) -> str:
    """Human 'added to the library' timestamp, local time, down to the minute (YYYY/MM/DD HH:MM)."""
    try:
        dt = datetime.fromisoformat(created_iso)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # show in the operator's local time
    return dt.strftime("%Y/%m/%d %H:%M")


def design_key(spec: CellSpec) -> str:
    """Stable id for a design: item + rate + machine + module loadout."""
    raw = "|".join([
        str(spec.target_item),
        f"{spec.target_rate:g}",
        str(spec.machine),
        ",".join(sorted(spec.modules)),
    ])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    rate = f"{spec.target_rate:g}".replace(".", "p")
    return f"{spec.target_item}-{rate}-{digest}"


def describe(spec: CellSpec) -> str:
    """One-line human description of the cell."""
    mods = ""
    if spec.modules:
        from collections import Counter
        counts = Counter(spec.modules)
        mods = " + " + ", ".join(f"{n}x {m}" for m, n in sorted(counts.items()))
    subs = ""
    if spec.substages:
        subs = " (on-site: " + ", ".join(f"{s.machine_count}x {s.item}" for s in spec.substages) + ")"
    fp = spec.footprint or {}
    inputs = ", ".join(f"{i.item} {i.per_minute:g}/min" for i in spec.inputs)
    return (
        f"{spec.target_item} @ {spec.achieved_rate:g}/min: "
        f"{spec.machine_count}x {spec.machine}{mods}{subs}; "
        f"in: {inputs}; {spec.total_power_kw:g} kW; ~{fp.get('area', 0):g} tiles"
    )


def save_design(
    runtime_dir: Path,
    spec: CellSpec,
    *,
    blueprint_string: str,
    sandbox_status: str = "unknown",
    placed: Any = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write a design record to ``runtime/cell-library/<key>.json`` and append it to the index.
    Returns the record. Idempotent on the design key (overwrites the same key)."""

    key = design_key(spec)
    created_iso = created_at or datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "key": key,
        "item": spec.target_item,
        "target_rate": spec.target_rate,
        "achieved_rate": spec.achieved_rate,
        "machine": spec.machine,
        "machine_count": spec.machine_count,
        "modules": spec.modules,
        "substages": [s.to_dict() for s in spec.substages],
        "inputs": [i.to_dict() for i in spec.inputs],
        "outputs": [o.to_dict() for o in spec.outputs],
        "total_power_kw": spec.total_power_kw,
        "footprint": dict(spec.footprint),
        "archetype": spec.archetype,
        "description": describe(spec),
        "blueprint": {"format": "factorio-blueprint-string", "exchange_string": blueprint_string},
        "sandbox_status": sandbox_status,
        "created_at": created_iso,
        "added_at": _format_added_at(created_iso),
    }
    if placed is not None and hasattr(placed, "required_box"):
        record["required_box"] = placed.required_box
        record["power_coverage_ok"] = getattr(placed, "power_coverage_ok", None)
        # The footprint SIZE is the bounding rectangle (width x height) the cell actually occupies,
        # INCLUDING empty space between machines/belts -- not the sum of entity tiles. Use the placed
        # bounds so the dashboard shows a true area.
        rb = placed.required_box or {}
        w, h = rb.get("width"), rb.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)):
            record["footprint"] = {**record["footprint"], "width": w, "height": h, "area": round(w * h, 1)}

    lib = _library_dir(runtime_dir)
    lib.mkdir(parents=True, exist_ok=True)
    (lib / f"{key}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _rewrite_index(lib)
    return record


def _rewrite_index(lib: Path) -> None:
    rows = []
    for path in sorted(lib.glob("*.json")):
        if path.name == INDEX_NAME:
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    with (lib / INDEX_NAME).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_designs(runtime_dir: Path) -> list[dict[str, Any]]:
    """Load all saved designs (newest first)."""
    lib = _library_dir(runtime_dir)
    rows: list[dict[str, Any]] = []
    for path in lib.glob("*.json"):
        if path.name == INDEX_NAME:
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def get_design(runtime_dir: Path, key: str) -> dict[str, Any] | None:
    path = _library_dir(runtime_dir) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def library_summary(runtime_dir: Path) -> dict[str, Any]:
    designs = load_designs(runtime_dir)
    return {
        "designs": designs,
        "count": len(designs),
        "library_path": str(_library_dir(runtime_dir)),
    }
